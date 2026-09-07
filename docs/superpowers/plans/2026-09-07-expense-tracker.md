# Expense Tracker Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the `expense_tracker` Home Assistant custom integration — SQLite-backed expense logging with type management, insight sensors, and a native dashboard entry form — in a new repo `expense-tracker-home-assistant`.

**Architecture:** A blocking `ExpenseDB` (plain `sqlite3`) does all persistence, wrapped by an `ExpenseTrackerRuntime` that marshals every call through `hass.async_add_executor_job` and pushes fresh state to two sensors after each write. Four services (`add_expense`, `add_type`, `remove_type`, `remove_expense`) are the only mutation path. A dedicated, integration-owned YAML file plus the public `script.reload` service keeps a wrapper script's `type` dropdown in sync so the dashboard gets a live, atomic entry form with zero custom frontend code.

**Tech Stack:** Python 3, Home Assistant custom component APIs (`homeassistant.core`, `homeassistant.config_entries`, `homeassistant.helpers`), `sqlite3` (stdlib), `pytest` + `pytest-homeassistant-custom-component`.

**Spec:** `docs/superpowers/specs/2026-09-07-expense-tracker-design.md`

## Global Constraints

- Domain is `expense_tracker`. Single config entry only (`single_instance_only`).
- **Every SQLite call MUST run via `hass.async_add_executor_job(...)`.** A direct blocking call on the event loop is a bug, not a style nit (spec: Concurrency requirement).
- `expenses.type_name` is a plain `TEXT` column — **never** a `REFERENCES expense_types(name)` foreign key. Validate existence in application code at insert time only (spec: Data model).
- No `DataUpdateCoordinator`, no scan interval. Every write pushes state immediately (spec: event-driven).
- DB file lives at `hass.config.path(DOMAIN, "expenses.db")`. Receipts live under `hass.config.path("www/expense_tracker/receipts")`.
- No `edit_type`, no per-type sensors, no cascading delete on `remove_type`, no multi-currency, no custom Lovelace card — all explicitly out of scope in the spec. Don't add them.
- The wrapper script's `type` field options are kept live by rewriting the integration's own dedicated YAML file and calling the public `script.reload` service — never by reaching into the `script` component's internal storage objects, never by touching the user's own `scripts.yaml`.

---

### Task 1: Repo scaffold and test harness

**Files:**
- Create: `custom_components/expense_tracker/__init__.py`
- Create: `custom_components/expense_tracker/manifest.json`
- Create: `custom_components/expense_tracker/const.py`
- Create: `tests/conftest.py`
- Create: `tests/test_init.py`
- Create: `requirements_test.txt`
- Create: `pytest.ini`
- Create: `.gitignore`

**Interfaces:**
- Produces: `DOMAIN = "expense_tracker"` (`const.py`), importable by every later file.

- [ ] **Step 1: Create the new repo**

```bash
mkdir -p ~/Documents/projects/personal/expense-tracker-home-assistant
cd ~/Documents/projects/personal/expense-tracker-home-assistant
git init -q
mkdir -p custom_components/expense_tracker tests
```

- [ ] **Step 2: Write `const.py`**

```python
"""Constants for the Expense Tracker integration."""

DOMAIN = "expense_tracker"
DB_FILENAME = "expenses.db"
RECEIPTS_DIR = "www/expense_tracker/receipts"
SCRIPT_OBJECT_ID = "expense_tracker_add_expense"
SCRIPT_CONFIG_FILENAME = "expense_tracker_scripts.yaml"
SCHEMA_VERSION = 1

DEFAULT_TYPES = [
    ("Groceries", "mdi:cart"),
    ("Transport", "mdi:car"),
    ("Utilities", "mdi:flash"),
    ("Health", "mdi:medical-bag"),
    ("Entertainment", "mdi:movie"),
    ("Other", "mdi:dots-horizontal"),
]
```

- [ ] **Step 3: Write a stub `__init__.py`** (later tasks replace the body)

```python
"""The Expense Tracker integration."""
from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    return True
```

- [ ] **Step 4: Write `manifest.json`**

```json
{
  "domain": "expense_tracker",
  "name": "Expense Tracker",
  "version": "0.1.0",
  "config_flow": true,
  "documentation": "https://github.com/armendkadrija/expense-tracker-home-assistant",
  "requirements": [],
  "codeowners": ["@armendkadrija"],
  "iot_class": "local_push"
}
```

- [ ] **Step 5: Write the test harness files**

`requirements_test.txt`:
```
pytest-homeassistant-custom-component
pytest-asyncio
```

`pytest.ini`:
```ini
[pytest]
asyncio_mode = auto
```

`tests/conftest.py`:
```python
import pytest

pytest_plugins = "pytest_homeassistant_custom_component"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    yield
```

`.gitignore`:
```
__pycache__/
*.pyc
.pytest_cache/
.venv/
```

- [ ] **Step 6: Write the smoke test**

```python
# tests/test_init.py
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.expense_tracker.const import DOMAIN


async def test_setup_entry_loads(hass):
    entry = MockConfigEntry(domain=DOMAIN)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state.recoverable
```

- [ ] **Step 7: Install deps and run**

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements_test.txt
.venv/bin/pytest tests/test_init.py -v
```

Expected: PASS (a bare `async_setup_entry` returning `True` is enough for this smoke test).

- [ ] **Step 8: Commit**

```bash
git add -A
git commit -m "Scaffold expense_tracker integration and test harness"
```

---

### Task 2: DB schema, migrations, seeding

**Files:**
- Create: `custom_components/expense_tracker/db.py`
- Test: `tests/test_db_schema.py`

**Interfaces:**
- Produces: `class ExpenseDB.__init__(self, db_path: str)`, `.initialize() -> None`, `.list_types() -> list[tuple[str, str]]`. All methods are **blocking** — callers elsewhere in this plan always wrap them in `hass.async_add_executor_job`.
- Produces: exceptions `DuplicateTypeError`, `UnknownTypeError`, `UnknownExpenseError` (all `ValueError` subclasses), used by later tasks.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_db_schema.py
from custom_components.expense_tracker.const import DEFAULT_TYPES
from custom_components.expense_tracker.db import ExpenseDB


def test_initialize_creates_tables_and_seeds_defaults(tmp_path):
    db = ExpenseDB(str(tmp_path / "expenses.db"))
    db.initialize()

    types = db.list_types()

    assert types == sorted(DEFAULT_TYPES)


def test_initialize_is_idempotent_and_does_not_reseed(tmp_path):
    db = ExpenseDB(str(tmp_path / "expenses.db"))
    db.initialize()
    db.add_type("Custom", "mdi:star")

    db.initialize()  # simulate a restart

    types = db.list_types()
    assert ("Custom", "mdi:star") in types
    assert len(types) == len(DEFAULT_TYPES) + 1
```

- [ ] **Step 2: Run to verify it fails**

```bash
.venv/bin/pytest tests/test_db_schema.py -v
```

Expected: FAIL — `db.py` doesn't exist yet.

- [ ] **Step 3: Write `db.py`**

```python
"""Blocking SQLite access for the Expense Tracker integration.

Every method here does file I/O and must only ever be called via
hass.async_add_executor_job — never directly from the event loop.
"""
from __future__ import annotations

import os
import sqlite3

from .const import DEFAULT_TYPES, SCHEMA_VERSION


class DuplicateTypeError(ValueError):
    """Raised when adding a type whose name already exists."""


class UnknownTypeError(ValueError):
    """Raised when referencing a type name that does not exist."""


class UnknownExpenseError(ValueError):
    """Raised when referencing an expense id that does not exist."""


class ExpenseDB:
    def __init__(self, db_path: str) -> None:
        self._db_path = db_path

    def initialize(self) -> None:
        os.makedirs(os.path.dirname(self._db_path), exist_ok=True)
        conn = sqlite3.connect(self._db_path)
        try:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS expense_types "
                "(name TEXT PRIMARY KEY, icon TEXT NOT NULL)"
            )
            conn.execute(
                "CREATE TABLE IF NOT EXISTS expenses ("
                "id TEXT PRIMARY KEY, amount REAL NOT NULL, "
                "type_name TEXT NOT NULL, user TEXT NOT NULL, "
                "receipt_path TEXT, note TEXT, timestamp TEXT NOT NULL)"
            )
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if version < SCHEMA_VERSION:
                conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            count = conn.execute(
                "SELECT COUNT(*) FROM expense_types"
            ).fetchone()[0]
            if count == 0:
                conn.executemany(
                    "INSERT INTO expense_types (name, icon) VALUES (?, ?)",
                    DEFAULT_TYPES,
                )
            conn.commit()
        finally:
            conn.close()

    def list_types(self) -> list[tuple[str, str]]:
        conn = sqlite3.connect(self._db_path)
        try:
            rows = conn.execute(
                "SELECT name, icon FROM expense_types ORDER BY name"
            ).fetchall()
            return [(row[0], row[1]) for row in rows]
        finally:
            conn.close()

    def add_type(self, name: str, icon: str) -> None:
        conn = sqlite3.connect(self._db_path)
        try:
            existing = conn.execute(
                "SELECT 1 FROM expense_types WHERE name = ?", (name,)
            ).fetchone()
            if existing:
                raise DuplicateTypeError(f"Type '{name}' already exists")
            conn.execute(
                "INSERT INTO expense_types (name, icon) VALUES (?, ?)",
                (name, icon),
            )
            conn.commit()
        finally:
            conn.close()
```

- [ ] **Step 4: Run to verify it passes**

```bash
.venv/bin/pytest tests/test_db_schema.py -v
```

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add custom_components/expense_tracker/db.py tests/test_db_schema.py
git commit -m "Add ExpenseDB schema, migration hook, and default seeding"
```

---

### Task 3: DB type management (remove_type)

**Files:**
- Modify: `custom_components/expense_tracker/db.py`
- Test: `tests/test_db_types.py`

**Interfaces:**
- Consumes: `ExpenseDB` from Task 2 (`add_type`, `list_types` already exist).
- Produces: `ExpenseDB.remove_type(self, name: str) -> None`, raising `UnknownTypeError` if `name` doesn't exist.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_db_types.py
import pytest

from custom_components.expense_tracker.db import (
    DuplicateTypeError,
    ExpenseDB,
    UnknownTypeError,
)


def test_add_type_rejects_duplicate_name(tmp_path):
    db = ExpenseDB(str(tmp_path / "expenses.db"))
    db.initialize()

    with pytest.raises(DuplicateTypeError):
        db.add_type("Groceries", "mdi:cart-outline")


def test_remove_type_deletes_it(tmp_path):
    db = ExpenseDB(str(tmp_path / "expenses.db"))
    db.initialize()
    db.add_type("Subscriptions", "mdi:credit-card")

    db.remove_type("Subscriptions")

    names = [name for name, _icon in db.list_types()]
    assert "Subscriptions" not in names


def test_remove_type_rejects_unknown_name(tmp_path):
    db = ExpenseDB(str(tmp_path / "expenses.db"))
    db.initialize()

    with pytest.raises(UnknownTypeError):
        db.remove_type("Nonexistent")
```

- [ ] **Step 2: Run to verify it fails**

```bash
.venv/bin/pytest tests/test_db_types.py -v
```

Expected: FAIL — `remove_type` not defined (the duplicate-name test passes already since Task 2's `add_type` handles it; the other two fail).

- [ ] **Step 3: Add `remove_type` to `db.py`** (append to the `ExpenseDB` class)

```python
    def remove_type(self, name: str) -> None:
        conn = sqlite3.connect(self._db_path)
        try:
            cursor = conn.execute(
                "DELETE FROM expense_types WHERE name = ?", (name,)
            )
            if cursor.rowcount == 0:
                raise UnknownTypeError(f"Type '{name}' does not exist")
            conn.commit()
        finally:
            conn.close()
```

- [ ] **Step 4: Run to verify it passes**

```bash
.venv/bin/pytest tests/test_db_types.py -v
```

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add custom_components/expense_tracker/db.py tests/test_db_types.py
git commit -m "Add ExpenseDB.remove_type"
```

---

### Task 4: DB expense management and totals

**Files:**
- Modify: `custom_components/expense_tracker/db.py`
- Test: `tests/test_db_expenses.py`

**Interfaces:**
- Consumes: `ExpenseDB` from Tasks 2-3.
- Produces:
  - `ExpenseDB.add_expense(self, expense_id: str, amount: float, type_name: str, user: str, timestamp: str, receipt_path: str | None = None, note: str | None = None) -> None` — raises `UnknownTypeError` if `type_name` doesn't exist. **`expense_id` is generated by the caller (services layer, Task 9), not by this method** — the caller needs the id up front to name the receipt file before this insert happens.
  - `ExpenseDB.remove_expense(self, expense_id: str) -> str | None` — deletes the row, returns its `receipt_path` (or `None`) so the caller can delete the file. Raises `UnknownExpenseError` if not found.
  - `ExpenseDB.get_totals(self, since: str | None = None) -> dict` — returns `{"total": float, "count": int, "by_type": dict, "by_user": dict}`. `since=None` means all-time; otherwise filters `WHERE timestamp >= since`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_db_expenses.py
import pytest

from custom_components.expense_tracker.db import (
    ExpenseDB,
    UnknownExpenseError,
    UnknownTypeError,
)


def _seeded_db(tmp_path):
    db = ExpenseDB(str(tmp_path / "expenses.db"))
    db.initialize()
    return db


def test_add_expense_rejects_unknown_type(tmp_path):
    db = _seeded_db(tmp_path)

    with pytest.raises(UnknownTypeError):
        db.add_expense(
            "id-1", 12.50, "NotAType", "person.armend", "2026-09-07T10:00:00+00:00"
        )


def test_add_and_total_expenses(tmp_path):
    db = _seeded_db(tmp_path)
    db.add_expense(
        "id-1", 12.50, "Groceries", "person.armend", "2026-09-07T10:00:00+00:00"
    )
    db.add_expense(
        "id-2", 7.25, "Transport", "person.partner", "2026-09-07T11:00:00+00:00"
    )

    totals = db.get_totals()

    assert totals["total"] == pytest.approx(19.75)
    assert totals["count"] == 2
    assert totals["by_type"] == {"Groceries": 12.50, "Transport": 7.25}
    assert totals["by_user"] == {"person.armend": 12.50, "person.partner": 7.25}


def test_get_totals_filters_by_since(tmp_path):
    db = _seeded_db(tmp_path)
    db.add_expense(
        "id-old", 100.0, "Groceries", "person.armend", "2025-01-01T00:00:00+00:00"
    )
    db.add_expense(
        "id-new", 5.0, "Groceries", "person.armend", "2026-09-07T00:00:00+00:00"
    )

    totals = db.get_totals(since="2026-09-01T00:00:00+00:00")

    assert totals["total"] == pytest.approx(5.0)
    assert totals["count"] == 1


def test_remove_expense_returns_receipt_path_and_deletes_row(tmp_path):
    db = _seeded_db(tmp_path)
    db.add_expense(
        "id-1",
        12.50,
        "Groceries",
        "person.armend",
        "2026-09-07T10:00:00+00:00",
        receipt_path="www/expense_tracker/receipts/id-1.jpg",
    )

    receipt_path = db.remove_expense("id-1")

    assert receipt_path == "www/expense_tracker/receipts/id-1.jpg"
    assert db.get_totals()["count"] == 0


def test_remove_expense_rejects_unknown_id(tmp_path):
    db = _seeded_db(tmp_path)

    with pytest.raises(UnknownExpenseError):
        db.remove_expense("nonexistent")
```

- [ ] **Step 2: Run to verify it fails**

```bash
.venv/bin/pytest tests/test_db_expenses.py -v
```

Expected: FAIL — none of these methods exist yet.

- [ ] **Step 3: Add the methods to `db.py`** (append to the `ExpenseDB` class; add `UnknownExpenseError` import is already in the same file)

```python
    def add_expense(
        self,
        expense_id: str,
        amount: float,
        type_name: str,
        user: str,
        timestamp: str,
        receipt_path: str | None = None,
        note: str | None = None,
    ) -> None:
        conn = sqlite3.connect(self._db_path)
        try:
            existing = conn.execute(
                "SELECT 1 FROM expense_types WHERE name = ?", (type_name,)
            ).fetchone()
            if not existing:
                raise UnknownTypeError(f"Type '{type_name}' does not exist")
            conn.execute(
                "INSERT INTO expenses "
                "(id, amount, type_name, user, receipt_path, note, timestamp) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (expense_id, amount, type_name, user, receipt_path, note, timestamp),
            )
            conn.commit()
        finally:
            conn.close()

    def remove_expense(self, expense_id: str) -> str | None:
        conn = sqlite3.connect(self._db_path)
        try:
            row = conn.execute(
                "SELECT receipt_path FROM expenses WHERE id = ?", (expense_id,)
            ).fetchone()
            if row is None:
                raise UnknownExpenseError(f"Expense '{expense_id}' does not exist")
            conn.execute("DELETE FROM expenses WHERE id = ?", (expense_id,))
            conn.commit()
            return row[0]
        finally:
            conn.close()

    def get_totals(self, since: str | None = None) -> dict:
        conn = sqlite3.connect(self._db_path)
        try:
            where = "WHERE timestamp >= ?" if since else ""
            params = (since,) if since else ()
            total = conn.execute(
                f"SELECT COALESCE(SUM(amount), 0) FROM expenses {where}", params
            ).fetchone()[0]
            count = conn.execute(
                f"SELECT COUNT(*) FROM expenses {where}", params
            ).fetchone()[0]
            by_type = dict(
                conn.execute(
                    f"SELECT type_name, SUM(amount) FROM expenses {where} "
                    "GROUP BY type_name",
                    params,
                ).fetchall()
            )
            by_user = dict(
                conn.execute(
                    f"SELECT user, SUM(amount) FROM expenses {where} "
                    "GROUP BY user",
                    params,
                ).fetchall()
            )
            return {
                "total": total,
                "count": count,
                "by_type": by_type,
                "by_user": by_user,
            }
        finally:
            conn.close()
```

- [ ] **Step 4: Run to verify it passes**

```bash
.venv/bin/pytest tests/test_db_expenses.py -v
```

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add custom_components/expense_tracker/db.py tests/test_db_expenses.py
git commit -m "Add ExpenseDB expense CRUD and totals aggregation"
```

---

### Task 5: Receipt file storage helper

**Files:**
- Create: `custom_components/expense_tracker/receipts.py`
- Test: `tests/test_receipts.py`

**Interfaces:**
- Consumes: nothing from earlier tasks (pure helper module).
- Produces: `async_save_receipt(hass, expense_id: str, image_data_url: str) -> str` (returns the relative path to store in `receipt_path`), `async_delete_receipt(hass, relative_path: str) -> None`. Both are async and internally executor-wrapped — callers never touch blocking I/O directly.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_receipts.py
import base64
import os

from custom_components.expense_tracker.receipts import (
    async_delete_receipt,
    async_save_receipt,
)


async def test_save_receipt_decodes_data_url_and_writes_file(hass, tmp_path):
    hass.config.config_dir = str(tmp_path)
    image_bytes = b"fake-jpeg-bytes"
    data_url = "data:image/jpeg;base64," + base64.b64encode(image_bytes).decode()

    relative_path = await async_save_receipt(hass, "expense-1", data_url)

    assert relative_path == "www/expense_tracker/receipts/expense-1.jpeg"
    full_path = os.path.join(str(tmp_path), relative_path)
    with open(full_path, "rb") as f:
        assert f.read() == image_bytes


async def test_save_receipt_handles_bare_base64(hass, tmp_path):
    hass.config.config_dir = str(tmp_path)
    image_bytes = b"more-bytes"
    encoded = base64.b64encode(image_bytes).decode()

    relative_path = await async_save_receipt(hass, "expense-2", encoded)

    assert relative_path == "www/expense_tracker/receipts/expense-2.jpg"


async def test_delete_receipt_removes_file(hass, tmp_path):
    hass.config.config_dir = str(tmp_path)
    relative_path = await async_save_receipt(
        hass, "expense-3", base64.b64encode(b"x").decode()
    )
    full_path = os.path.join(str(tmp_path), relative_path)
    assert os.path.exists(full_path)

    await async_delete_receipt(hass, relative_path)

    assert not os.path.exists(full_path)


async def test_delete_receipt_missing_file_is_a_noop(hass, tmp_path):
    hass.config.config_dir = str(tmp_path)

    await async_delete_receipt(hass, "www/expense_tracker/receipts/nope.jpg")
```

- [ ] **Step 2: Run to verify it fails**

```bash
.venv/bin/pytest tests/test_receipts.py -v
```

Expected: FAIL — `receipts.py` doesn't exist yet.

- [ ] **Step 3: Write `receipts.py`**

```python
"""Receipt file storage for the Expense Tracker integration."""
from __future__ import annotations

import base64
import os

from homeassistant.core import HomeAssistant

from .const import RECEIPTS_DIR


def _write_receipt_file(
    receipts_root: str, expense_id: str, image_bytes: bytes, ext: str
) -> str:
    os.makedirs(receipts_root, exist_ok=True)
    filename = f"{expense_id}.{ext}"
    with open(os.path.join(receipts_root, filename), "wb") as f:
        f.write(image_bytes)
    return f"{RECEIPTS_DIR}/{filename}"


def _delete_receipt_file(config_root: str, relative_path: str) -> None:
    full_path = os.path.join(config_root, relative_path)
    if os.path.exists(full_path):
        os.remove(full_path)


async def async_save_receipt(
    hass: HomeAssistant, expense_id: str, image_data_url: str
) -> str:
    """image_data_url is the raw value from HA's `image` selector: a
    base64 string, optionally prefixed 'data:image/<ext>;base64,'."""
    if image_data_url.startswith("data:") and "," in image_data_url:
        header, encoded = image_data_url.split(",", 1)
        ext = header.split("/")[1].split(";")[0] if "/" in header else "jpg"
    else:
        encoded = image_data_url
        ext = "jpg"
    image_bytes = base64.b64decode(encoded)
    receipts_root = hass.config.path(RECEIPTS_DIR)
    return await hass.async_add_executor_job(
        _write_receipt_file, receipts_root, expense_id, image_bytes, ext
    )


async def async_delete_receipt(hass: HomeAssistant, relative_path: str) -> None:
    config_root = hass.config.path()
    await hass.async_add_executor_job(_delete_receipt_file, config_root, relative_path)
```

- [ ] **Step 4: Run to verify it passes**

```bash
.venv/bin/pytest tests/test_receipts.py -v
```

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add custom_components/expense_tracker/receipts.py tests/test_receipts.py
git commit -m "Add receipt file storage helper"
```

---

### Task 6: Config flow

**Files:**
- Create: `custom_components/expense_tracker/config_flow.py`
- Create: `custom_components/expense_tracker/strings.json`
- Create: `custom_components/expense_tracker/translations/en.json`
- Test: `tests/test_config_flow.py`

**Interfaces:**
- Consumes: `DOMAIN` from `const.py`.
- Produces: `ExpenseTrackerConfigFlow` registered for `DOMAIN`, single-instance only.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_config_flow.py
from homeassistant import config_entries, data_entry_flow

from custom_components.expense_tracker.const import DOMAIN


async def test_user_flow_creates_single_entry(hass):
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] == data_entry_flow.FlowResultType.FORM

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input={}
    )
    assert result["type"] == data_entry_flow.FlowResultType.CREATE_ENTRY
    assert result["title"] == "Expense Tracker"


async def test_second_flow_aborts_single_instance(hass):
    await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    first = hass.config_entries.flow.async_progress()[0]
    await hass.config_entries.flow.async_configure(first["flow_id"], user_input={})

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] == data_entry_flow.FlowResultType.ABORT
    assert result["reason"] == "single_instance_allowed"
```

- [ ] **Step 2: Run to verify it fails**

```bash
.venv/bin/pytest tests/test_config_flow.py -v
```

Expected: FAIL — `config_flow.py` doesn't exist.

- [ ] **Step 3: Write `config_flow.py`**

```python
"""Config flow for the Expense Tracker integration."""
from __future__ import annotations

import voluptuous as vol

from homeassistant import config_entries

from .const import DOMAIN


class ExpenseTrackerConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input: dict | None = None):
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()
        if user_input is not None:
            return self.async_create_entry(title="Expense Tracker", data={})
        return self.async_show_form(step_id="user", data_schema=vol.Schema({}))
```

- [ ] **Step 4: Write `strings.json` and `translations/en.json`** (identical content)

```json
{
  "config": {
    "step": {
      "user": {
        "title": "Set up Expense Tracker",
        "description": "Track personal/household expenses locally. Nothing to configure."
      }
    },
    "abort": {
      "single_instance_allowed": "Only one Expense Tracker instance is allowed."
    }
  }
}
```

- [ ] **Step 5: Run to verify it passes**

```bash
.venv/bin/pytest tests/test_config_flow.py -v
```

Expected: PASS

- [ ] **Step 6: Commit**

```bash
mkdir -p custom_components/expense_tracker/translations
git add custom_components/expense_tracker/config_flow.py \
  custom_components/expense_tracker/strings.json \
  custom_components/expense_tracker/translations/en.json \
  tests/test_config_flow.py
git commit -m "Add single-instance config flow"
```

---

### Task 7: Integration runtime and setup/unload

**Files:**
- Create: `custom_components/expense_tracker/runtime.py`
- Modify: `custom_components/expense_tracker/__init__.py`
- Modify: `tests/test_init.py`

**Interfaces:**
- Consumes: `ExpenseDB` (Tasks 2-4), `DOMAIN`/`DB_FILENAME` (`const.py`).
- Produces: `class ExpenseTrackerRuntime` with `hass`, `db`, `sensors: list` attributes and:
  - `async def async_initialize(self) -> None`
  - `async def async_list_types(self) -> list[tuple[str, str]]`
  - `async def async_add_type(self, name: str, icon: str) -> None`
  - `async def async_remove_type(self, name: str) -> None`
  - `async def async_add_expense(self, expense_id, amount, type_name, user, timestamp, receipt_path=None, note=None) -> None`
  - `async def async_remove_expense(self, expense_id: str) -> str | None`
  - `async def async_get_totals(self, since: str | None = None) -> dict`
  - Stored at `hass.data[DOMAIN][entry.entry_id]`, consumed by `sensor.py` (Task 8) and `services.py` (Task 9).
- `_async_notify_types_changed` is a no-op stub in this task — Task 10 fills it in. Don't skip defining it; Task 9's tests don't touch it, but Task 10 depends on its exact name.

- [ ] **Step 1: Write the failing test** (extends `tests/test_init.py` from Task 1)

```python
# tests/test_init.py — replace the whole file
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.expense_tracker.const import DOMAIN


async def test_setup_entry_creates_runtime_and_initializes_db(hass, tmp_path):
    hass.config.config_dir = str(tmp_path)
    entry = MockConfigEntry(domain=DOMAIN)
    entry.add_to_hass(hass)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    runtime = hass.data[DOMAIN][entry.entry_id]
    types = await runtime.async_list_types()
    assert len(types) == 6  # the seeded defaults


async def test_unload_entry_removes_runtime(hass, tmp_path):
    hass.config.config_dir = str(tmp_path)
    entry = MockConfigEntry(domain=DOMAIN)
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.entry_id not in hass.data.get(DOMAIN, {})
```

- [ ] **Step 2: Run to verify it fails**

```bash
.venv/bin/pytest tests/test_init.py -v
```

Expected: FAIL — `runtime` doesn't exist in `hass.data[DOMAIN]` yet.

- [ ] **Step 3: Write `runtime.py`**

```python
"""Runtime coordination layer: marshals ExpenseDB calls off the event
loop and pushes fresh state to sensors after every write."""
from __future__ import annotations

from homeassistant.core import HomeAssistant

from .db import ExpenseDB


class ExpenseTrackerRuntime:
    def __init__(self, hass: HomeAssistant, db: ExpenseDB) -> None:
        self.hass = hass
        self.db = db
        self.sensors: list = []

    async def async_initialize(self) -> None:
        await self.hass.async_add_executor_job(self.db.initialize)

    async def async_list_types(self) -> list[tuple[str, str]]:
        return await self.hass.async_add_executor_job(self.db.list_types)

    async def async_add_type(self, name: str, icon: str) -> None:
        await self.hass.async_add_executor_job(self.db.add_type, name, icon)
        await self._async_notify_types_changed()

    async def async_remove_type(self, name: str) -> None:
        await self.hass.async_add_executor_job(self.db.remove_type, name)
        await self._async_notify_types_changed()

    async def async_add_expense(
        self,
        expense_id: str,
        amount: float,
        type_name: str,
        user: str,
        timestamp: str,
        receipt_path: str | None = None,
        note: str | None = None,
    ) -> None:
        await self.hass.async_add_executor_job(
            self.db.add_expense,
            expense_id,
            amount,
            type_name,
            user,
            timestamp,
            receipt_path,
            note,
        )
        await self._async_refresh_sensors()

    async def async_remove_expense(self, expense_id: str) -> str | None:
        receipt_path = await self.hass.async_add_executor_job(
            self.db.remove_expense, expense_id
        )
        await self._async_refresh_sensors()
        return receipt_path

    async def async_get_totals(self, since: str | None = None) -> dict:
        return await self.hass.async_add_executor_job(self.db.get_totals, since)

    async def _async_refresh_sensors(self) -> None:
        for sensor in self.sensors:
            await sensor.async_refresh()

    async def _async_notify_types_changed(self) -> None:
        """Filled in by Task 10 (script_sync)."""
```

- [ ] **Step 4: Rewrite `__init__.py`**

```python
"""The Expense Tracker integration."""
from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from .const import DB_FILENAME, DOMAIN
from .db import ExpenseDB
from .runtime import ExpenseTrackerRuntime

PLATFORMS = [Platform.SENSOR]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    db_path = hass.config.path(DOMAIN, DB_FILENAME)
    db = ExpenseDB(db_path)
    runtime = ExpenseTrackerRuntime(hass, db)
    await runtime.async_initialize()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = runtime
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id)
    return unload_ok
```

Note: `PLATFORMS = [Platform.SENSOR]` will fail to forward until Task 8 adds `sensor.py`. Run the test now to confirm exactly that failure mode before moving on.

- [ ] **Step 5: Run to verify it still fails the same way, for the right reason**

```bash
.venv/bin/pytest tests/test_init.py -v
```

Expected: FAIL — platform forwarding looks for `custom_components/expense_tracker/sensor.py`, which doesn't exist until Task 8. This is expected; do not treat it as this task's failure.

- [ ] **Step 6: Commit**

```bash
git add custom_components/expense_tracker/runtime.py \
  custom_components/expense_tracker/__init__.py tests/test_init.py
git commit -m "Add ExpenseTrackerRuntime and wire it into setup/unload"
```

---

### Task 8: Sensor platform

**Files:**
- Create: `custom_components/expense_tracker/sensor.py`
- Modify: `tests/test_init.py` (no change needed — it now passes)
- Test: `tests/test_sensor.py`

**Interfaces:**
- Consumes: `ExpenseTrackerRuntime` (Task 7) via `hass.data[DOMAIN][entry.entry_id]`.
- Produces: `sensor.expense_tracker_total`, `sensor.expense_tracker_total_this_month`, both `ExpenseTrackerTotalSensor` instances registered into `runtime.sensors`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_sensor.py
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.expense_tracker.const import DOMAIN


async def test_sensors_start_at_zero_with_seeded_types(hass, tmp_path):
    hass.config.config_dir = str(tmp_path)
    entry = MockConfigEntry(domain=DOMAIN)
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    total = hass.states.get("sensor.expense_tracker_total")
    month = hass.states.get("sensor.expense_tracker_total_this_month")

    assert total.state == "0"
    assert total.attributes["count"] == 0
    assert month.state == "0"


async def test_sensor_updates_after_add_expense(hass, tmp_path):
    hass.config.config_dir = str(tmp_path)
    entry = MockConfigEntry(domain=DOMAIN)
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    runtime = hass.data[DOMAIN][entry.entry_id]
    await runtime.async_add_expense(
        expense_id="id-1",
        amount=9.5,
        type_name="Groceries",
        user="person.armend",
        timestamp="2026-09-07T10:00:00+00:00",
    )
    await hass.async_block_till_done()

    total = hass.states.get("sensor.expense_tracker_total")
    assert float(total.state) == 9.5
    assert total.attributes["by_type"] == {"Groceries": 9.5}
```

- [ ] **Step 2: Run to verify it fails**

```bash
.venv/bin/pytest tests/test_sensor.py tests/test_init.py -v
```

Expected: FAIL — `sensor.py` doesn't exist; Task 7's `test_init.py` also still fails for the same missing-platform reason.

- [ ] **Step 3: Write `sensor.py`**

```python
"""Sensor platform for the Expense Tracker integration."""
from __future__ import annotations

from datetime import datetime, timezone

from homeassistant.components.sensor import SensorEntity, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .runtime import ExpenseTrackerRuntime


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    runtime: ExpenseTrackerRuntime = hass.data[DOMAIN][entry.entry_id]
    total_sensor = ExpenseTrackerTotalSensor(runtime, this_month=False)
    month_sensor = ExpenseTrackerTotalSensor(runtime, this_month=True)
    runtime.sensors = [total_sensor, month_sensor]
    async_add_entities([total_sensor, month_sensor])
    await total_sensor.async_refresh()
    await month_sensor.async_refresh()


def _start_of_month_iso() -> str:
    now = datetime.now(timezone.utc)
    return now.replace(
        day=1, hour=0, minute=0, second=0, microsecond=0
    ).isoformat()


class ExpenseTrackerTotalSensor(SensorEntity):
    _attr_should_poll = False
    _attr_state_class = SensorStateClass.TOTAL

    def __init__(self, runtime: ExpenseTrackerRuntime, this_month: bool) -> None:
        self._runtime = runtime
        self._this_month = this_month
        suffix = "total_this_month" if this_month else "total"
        self._attr_unique_id = f"{DOMAIN}_{suffix}"
        self.entity_id = f"sensor.{DOMAIN}_{suffix}"
        self._attr_name = (
            "Expenses this month" if this_month else "Expenses total"
        )

    @property
    def native_unit_of_measurement(self) -> str | None:
        return self._runtime.hass.config.currency

    async def async_refresh(self) -> None:
        since = _start_of_month_iso() if self._this_month else None
        totals = await self._runtime.async_get_totals(since=since)
        self._attr_native_value = totals["total"]
        self._attr_extra_state_attributes = {
            "by_type": totals["by_type"],
            "by_user": totals["by_user"],
            "count": totals["count"],
        }
        self.async_write_ha_state()
```

- [ ] **Step 4: Run to verify it passes**

```bash
.venv/bin/pytest tests/test_sensor.py tests/test_init.py -v
```

Expected: PASS (both files)

- [ ] **Step 5: Commit**

```bash
git add custom_components/expense_tracker/sensor.py tests/test_sensor.py
git commit -m "Add expense total sensors, push-updated after every write"
```

---

### Task 9: Services (add_expense, add_type, remove_type, remove_expense)

**Files:**
- Create: `custom_components/expense_tracker/services.py`
- Create: `custom_components/expense_tracker/services.yaml`
- Modify: `custom_components/expense_tracker/__init__.py`
- Test: `tests/test_services.py`

**Interfaces:**
- Consumes: `ExpenseTrackerRuntime` (Task 7), `async_save_receipt`/`async_delete_receipt` (Task 5), `DuplicateTypeError`/`UnknownTypeError`/`UnknownExpenseError` (Task 2-4).
- Produces: `async_register_services(hass, runtime) -> None`, `async_unregister_services(hass) -> None`. Registers `expense_tracker.add_expense`, `.add_type`, `.remove_type`, `.remove_expense`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_services.py
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.exceptions import ServiceValidationError

from custom_components.expense_tracker.const import DOMAIN


async def _setup(hass, tmp_path):
    hass.config.config_dir = str(tmp_path)
    entry = MockConfigEntry(domain=DOMAIN)
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def test_add_expense_service_inserts_and_updates_sensor(hass, tmp_path):
    await _setup(hass, tmp_path)

    await hass.services.async_call(
        DOMAIN,
        "add_expense",
        {"amount": 12.5, "type": "Groceries", "user": "person.armend"},
        blocking=True,
    )

    total = hass.states.get("sensor.expense_tracker_total")
    assert float(total.state) == 12.5


async def test_add_expense_service_rejects_unknown_type(hass, tmp_path):
    await _setup(hass, tmp_path)

    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN,
            "add_expense",
            {"amount": 1.0, "type": "NotAType", "user": "person.armend"},
            blocking=True,
        )


async def test_add_type_then_remove_type_service(hass, tmp_path):
    await _setup(hass, tmp_path)

    await hass.services.async_call(
        DOMAIN, "add_type", {"name": "Subscriptions", "icon": "mdi:credit-card"},
        blocking=True,
    )
    await hass.services.async_call(
        DOMAIN, "add_expense",
        {"amount": 9.99, "type": "Subscriptions", "user": "person.armend"},
        blocking=True,
    )

    await hass.services.async_call(
        DOMAIN, "remove_type", {"name": "Subscriptions"}, blocking=True
    )

    total = hass.states.get("sensor.expense_tracker_total")
    assert total.attributes["by_type"]["Subscriptions"] == 9.99  # history survives


async def test_remove_type_rejects_unknown_name(hass, tmp_path):
    await _setup(hass, tmp_path)

    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN, "remove_type", {"name": "Nonexistent"}, blocking=True
        )


async def test_remove_expense_service_deletes_and_updates_sensor(hass, tmp_path):
    await _setup(hass, tmp_path)
    runtime = hass.data[DOMAIN][list(hass.data[DOMAIN])[0]]
    await runtime.async_add_expense(
        expense_id="id-1",
        amount=5.0,
        type_name="Groceries",
        user="person.armend",
        timestamp="2026-09-07T10:00:00+00:00",
    )

    await hass.services.async_call(
        DOMAIN, "remove_expense", {"id": "id-1"}, blocking=True
    )

    total = hass.states.get("sensor.expense_tracker_total")
    assert total.attributes["count"] == 0
```

- [ ] **Step 2: Run to verify it fails**

```bash
.venv/bin/pytest tests/test_services.py -v
```

Expected: FAIL — services aren't registered yet.

- [ ] **Step 3: Write `services.py`**

```python
"""Service (action) handlers for the Expense Tracker integration."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

import voluptuous as vol

from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv

from .const import DOMAIN
from .db import DuplicateTypeError, UnknownExpenseError, UnknownTypeError
from .receipts import async_delete_receipt, async_save_receipt
from .runtime import ExpenseTrackerRuntime

SERVICE_ADD_EXPENSE = "add_expense"
SERVICE_ADD_TYPE = "add_type"
SERVICE_REMOVE_TYPE = "remove_type"
SERVICE_REMOVE_EXPENSE = "remove_expense"

ADD_EXPENSE_SCHEMA = vol.Schema(
    {
        vol.Required("amount"): vol.All(vol.Coerce(float), vol.Range(min=0)),
        vol.Required("type"): cv.string,
        vol.Required("user"): cv.entity_id,
        vol.Optional("date"): cv.string,
        vol.Optional("receipt"): cv.string,
        vol.Optional("note"): cv.string,
    }
)
ADD_TYPE_SCHEMA = vol.Schema(
    {vol.Required("name"): cv.string, vol.Required("icon"): cv.icon}
)
REMOVE_TYPE_SCHEMA = vol.Schema({vol.Required("name"): cv.string})
REMOVE_EXPENSE_SCHEMA = vol.Schema({vol.Required("id"): cv.string})


def async_register_services(hass: HomeAssistant, runtime: ExpenseTrackerRuntime) -> None:
    async def handle_add_expense(call: ServiceCall) -> None:
        expense_id = str(uuid.uuid4())
        timestamp = call.data.get("date") or datetime.now(timezone.utc).isoformat()
        receipt_path = None
        if call.data.get("receipt"):
            receipt_path = await async_save_receipt(
                hass, expense_id, call.data["receipt"]
            )
        try:
            await runtime.async_add_expense(
                expense_id=expense_id,
                amount=call.data["amount"],
                type_name=call.data["type"],
                user=call.data["user"],
                timestamp=timestamp,
                receipt_path=receipt_path,
                note=call.data.get("note"),
            )
        except UnknownTypeError as err:
            raise ServiceValidationError(str(err)) from err

    async def handle_add_type(call: ServiceCall) -> None:
        try:
            await runtime.async_add_type(call.data["name"], call.data["icon"])
        except DuplicateTypeError as err:
            raise ServiceValidationError(str(err)) from err

    async def handle_remove_type(call: ServiceCall) -> None:
        try:
            await runtime.async_remove_type(call.data["name"])
        except UnknownTypeError as err:
            raise ServiceValidationError(str(err)) from err

    async def handle_remove_expense(call: ServiceCall) -> None:
        try:
            receipt_path = await runtime.async_remove_expense(call.data["id"])
        except UnknownExpenseError as err:
            raise ServiceValidationError(str(err)) from err
        if receipt_path:
            await async_delete_receipt(hass, receipt_path)

    hass.services.async_register(
        DOMAIN, SERVICE_ADD_EXPENSE, handle_add_expense, schema=ADD_EXPENSE_SCHEMA
    )
    hass.services.async_register(
        DOMAIN, SERVICE_ADD_TYPE, handle_add_type, schema=ADD_TYPE_SCHEMA
    )
    hass.services.async_register(
        DOMAIN, SERVICE_REMOVE_TYPE, handle_remove_type, schema=REMOVE_TYPE_SCHEMA
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_REMOVE_EXPENSE,
        handle_remove_expense,
        schema=REMOVE_EXPENSE_SCHEMA,
    )


def async_unregister_services(hass: HomeAssistant) -> None:
    for service in (
        SERVICE_ADD_EXPENSE,
        SERVICE_ADD_TYPE,
        SERVICE_REMOVE_TYPE,
        SERVICE_REMOVE_EXPENSE,
    ):
        hass.services.async_remove(DOMAIN, service)
```

- [ ] **Step 4: Write `services.yaml`**

```yaml
add_expense:
  name: Add expense
  description: Log a new expense.
  fields:
    amount:
      required: true
      selector:
        number:
          min: 0
          mode: box
    type:
      required: true
      selector:
        text:
    user:
      required: true
      selector:
        entity:
          domain: person
    date:
      required: false
      selector:
        date:
    receipt:
      required: false
      selector:
        image:
    note:
      required: false
      selector:
        text:

add_type:
  name: Add expense type
  description: Register a new expense type.
  fields:
    name:
      required: true
      selector:
        text:
    icon:
      required: true
      selector:
        icon:

remove_type:
  name: Remove expense type
  description: >
    Remove an expense type. Existing expenses keep their historical type
    name.
  fields:
    name:
      required: true
      selector:
        text:

remove_expense:
  name: Remove expense
  description: Delete an expense and its receipt file, if any.
  fields:
    id:
      required: true
      selector:
        text:
```

- [ ] **Step 5: Wire registration into `__init__.py`**

```python
# custom_components/expense_tracker/__init__.py — add these lines
from .services import async_register_services, async_unregister_services  # add to imports

# inside async_setup_entry, after hass.data.setdefault(...):
    async_register_services(hass, runtime)

# inside async_unload_entry, after a successful unload:
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id)
        async_unregister_services(hass)
```

- [ ] **Step 6: Run to verify it passes**

```bash
.venv/bin/pytest tests/test_services.py -v
```

Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add custom_components/expense_tracker/services.py \
  custom_components/expense_tracker/services.yaml \
  custom_components/expense_tracker/__init__.py tests/test_services.py
git commit -m "Add add_expense/add_type/remove_type/remove_expense services"
```

---

### Task 10: Script sync (dashboard entry form stays live)

**Files:**
- Create: `custom_components/expense_tracker/script_sync.py`
- Modify: `custom_components/expense_tracker/runtime.py`
- Modify: `custom_components/expense_tracker/__init__.py`
- Test: `tests/test_script_sync.py`

**Interfaces:**
- Consumes: `SCRIPT_OBJECT_ID`, `SCRIPT_CONFIG_FILENAME` (`const.py`); `ExpenseTrackerRuntime` (Task 7).
- Produces: `async_sync_script(hass, type_names: list[str]) -> None`. Called from `runtime._async_notify_types_changed` (on every `add_type`/`remove_type`) and once from `__init__.py` at startup.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_script_sync.py
import os
from unittest.mock import AsyncMock

import yaml

from custom_components.expense_tracker.const import SCRIPT_CONFIG_FILENAME, SCRIPT_OBJECT_ID
from custom_components.expense_tracker.script_sync import async_sync_script


async def test_sync_script_writes_valid_yaml_with_current_types(hass, tmp_path):
    hass.config.config_dir = str(tmp_path)
    hass.services.async_call = AsyncMock()

    await async_sync_script(hass, ["Groceries", "Transport"])

    path = os.path.join(str(tmp_path), SCRIPT_CONFIG_FILENAME)
    with open(path) as f:
        config = yaml.safe_load(f)

    script_def = config[SCRIPT_OBJECT_ID]
    assert script_def["fields"]["type"]["selector"]["select"]["options"] == [
        "Groceries",
        "Transport",
    ]
    assert script_def["sequence"][0]["action"] == "expense_tracker.add_expense"


async def test_sync_script_calls_script_reload(hass, tmp_path):
    hass.config.config_dir = str(tmp_path)
    hass.services.async_call = AsyncMock()

    await async_sync_script(hass, ["Groceries"])

    hass.services.async_call.assert_awaited_once_with(
        "script", "reload", blocking=True
    )


async def test_sync_script_overwrites_previous_options(hass, tmp_path):
    hass.config.config_dir = str(tmp_path)
    hass.services.async_call = AsyncMock()
    await async_sync_script(hass, ["Groceries"])

    await async_sync_script(hass, ["Groceries", "Subscriptions"])

    path = os.path.join(str(tmp_path), SCRIPT_CONFIG_FILENAME)
    with open(path) as f:
        config = yaml.safe_load(f)
    options = config[SCRIPT_OBJECT_ID]["fields"]["type"]["selector"]["select"]["options"]
    assert options == ["Groceries", "Subscriptions"]
```

- [ ] **Step 2: Run to verify it fails**

```bash
.venv/bin/pytest tests/test_script_sync.py -v
```

Expected: FAIL — `script_sync.py` doesn't exist.

- [ ] **Step 3: Write `script_sync.py`**

```python
"""Keeps the expense_tracker_add_expense helper script's `type` field
options in sync with the current list of expense types.

The integration owns SCRIPT_CONFIG_FILENAME exclusively — it never reads
or writes the user's own scripts.yaml, and never reaches into the script
component's internal storage objects. It only rewrites its own file and
calls the public script.reload service.
"""
from __future__ import annotations

import os

import yaml

from homeassistant.core import HomeAssistant

from .const import SCRIPT_CONFIG_FILENAME, SCRIPT_OBJECT_ID


def _build_script_config(type_names: list[str]) -> dict:
    return {
        SCRIPT_OBJECT_ID: {
            "alias": "Add expense",
            "fields": {
                "type": {
                    "required": True,
                    "selector": {"select": {"options": type_names}},
                },
                "amount": {
                    "required": True,
                    "selector": {"number": {"min": 0, "mode": "box"}},
                },
                "user": {
                    "required": True,
                    "selector": {"entity": {"domain": "person"}},
                },
                "date": {"required": False, "selector": {"date": {}}},
                "receipt": {"required": False, "selector": {"image": {}}},
                "note": {"required": False, "selector": {"text": {}}},
            },
            "sequence": [
                {
                    "action": "expense_tracker.add_expense",
                    "data": {
                        "amount": "{{ amount }}",
                        "type": "{{ type }}",
                        "user": "{{ user }}",
                        "date": "{{ date | default(None) }}",
                        "receipt": "{{ receipt | default(None) }}",
                        "note": "{{ note | default(None) }}",
                    },
                }
            ],
        }
    }


def _write_script_file(path: str, config: dict) -> None:
    tmp_path = f"{path}.tmp"
    with open(tmp_path, "w") as f:
        yaml.safe_dump(config, f, default_flow_style=False, sort_keys=False)
    os.replace(tmp_path, path)


async def async_sync_script(hass: HomeAssistant, type_names: list[str]) -> None:
    path = hass.config.path(SCRIPT_CONFIG_FILENAME)
    config = _build_script_config(type_names)
    await hass.async_add_executor_job(_write_script_file, path, config)
    await hass.services.async_call("script", "reload", blocking=True)
```

- [ ] **Step 4: Run to verify it passes**

```bash
.venv/bin/pytest tests/test_script_sync.py -v
```

Expected: PASS

- [ ] **Step 5: Wire it into `runtime.py`**

```python
# custom_components/expense_tracker/runtime.py — replace the stub method
    async def _async_notify_types_changed(self) -> None:
        from .script_sync import async_sync_script

        type_names = [name for name, _icon in await self.async_list_types()]
        await async_sync_script(self.hass, type_names)
```

- [ ] **Step 6: Sync once at startup in `__init__.py`**

```python
# custom_components/expense_tracker/__init__.py — add after runtime.async_initialize()
    from .script_sync import async_sync_script

    type_names = [name for name, _icon in await runtime.async_list_types()]
    await async_sync_script(hass, type_names)
```

- [ ] **Step 7: Run the full suite to confirm nothing broke**

```bash
.venv/bin/pytest tests/ -v
```

Expected: PASS across every test file.

- [ ] **Step 8: Commit**

```bash
git add custom_components/expense_tracker/script_sync.py \
  custom_components/expense_tracker/runtime.py \
  custom_components/expense_tracker/__init__.py tests/test_script_sync.py
git commit -m "Sync add-expense script's type options via owned YAML + script.reload"
```

**Manual verification (not covered by the automated suite — the test harness doesn't render frontend dialogs):** after installing on the real instance (Task 11), add `script expense_tracker: !include expense_tracker_scripts.yaml` under a `script` key in `configuration.yaml`, restart, call `expense_tracker.add_type` from Developer Tools → Actions, then open the `script.expense_tracker_add_expense` entity's more-info dialog and confirm the new type appears in the `type` dropdown.

---

### Task 11: Dashboard view and README

**Files:**
- Create: `dashboards/expense_tracker_view.yaml`
- Create: `README.md`

**Interfaces:**
- Consumes: nothing (this task is not tested by pytest — it's a documentation/config deliverable).

- [ ] **Step 1: Write the example dashboard view**

```yaml
# dashboards/expense_tracker_view.yaml
title: Expenses
path: expenses
icon: mdi:cash-multiple
cards:
  - type: button
    name: Add expense
    icon: mdi:plus-circle
    tap_action:
      action: perform-action
      perform_action: script.turn_on
      target:
        entity_id: script.expense_tracker_add_expense
    hold_action:
      action: more-info
      entity: script.expense_tracker_add_expense
  - type: entities
    title: Expense insights
    entities:
      - entity: sensor.expense_tracker_total
        name: All-time total
      - entity: sensor.expense_tracker_total_this_month
        name: This month
```

Note: the button's `hold_action: more-info` is what actually opens the fill-in form dialog (tapping a button card only fires its `tap_action`; the auto-generated field dialog is the script entity's more-info view). Simplest reliable alternative: add a second, plain entity row for `script.expense_tracker_add_expense` itself — tapping an entity row (not a button card) opens more-info by default:

```yaml
  - type: entities
    title: ""
    entities:
      - entity: script.expense_tracker_add_expense
        name: Add expense
```

Use the entities-row form; drop the button card's dual-action version above — it's included only to explain why a plain button card is the wrong choice here (no built-in more-info tap target).

- [ ] **Step 2: Write `README.md`**

```markdown
# Expense Tracker for Home Assistant

Private, personal-use custom integration for tracking household expenses:
amount, type, who spent it, optional receipt photo. See
`docs/superpowers/specs/2026-09-07-expense-tracker-design.md` in the
home-assistant repo for the full design rationale.

## Install

1. HACS → Custom repositories → add this repo's URL, category "Integration".
2. Install "Expense Tracker" from HACS, restart Home Assistant.
3. Settings → Devices & Services → Add Integration → "Expense Tracker"
   (no configuration needed).

## One-time setup: wire in the entry-form script

The integration maintains a dedicated script config file so the "Add
expense" dashboard form always shows your current expense types. Add this
line under a `script` key in `configuration.yaml`:

```yaml
script expense_tracker: !include expense_tracker_scripts.yaml
```

Restart Home Assistant once after adding this line.

## Dashboard

Copy the contents of `dashboards/expense_tracker_view.yaml` into a new
Lovelace view (Edit Dashboard → Add View → raw YAML editor).

## Managing expense types

Types ship seeded with Groceries, Transport, Utilities, Health,
Entertainment, Other. Add or remove your own from Developer Tools →
Actions:

- `expense_tracker.add_type` — `name`, `icon` (mdi icon string)
- `expense_tracker.remove_type` — `name` (existing expenses keep their
  historical type name even after it's removed)

## Fixing a mistake

`expense_tracker.remove_expense` with the expense's `id` deletes the row
and its receipt file. Find the `id` via the SQLite file directly:
`<config>/expense_tracker/expenses.db`.
```

- [ ] **Step 3: Commit**

```bash
mkdir -p dashboards
git add dashboards/expense_tracker_view.yaml README.md
git commit -m "Add example dashboard view and README setup instructions"
```

---

## Self-review notes (fixed inline while writing this plan)

- **Spec coverage:** every spec section has a task — data model & migrations (2), concurrency requirement (baked into every DB-touching task), config entry (6), all four services (9), both sensors (8), receipts (5, 9), dashboard/script sync (10, 11), build & test approach (1). No gaps found.
- **Consistency fix made while drafting:** the spec doesn't say who generates `expense_id`. Originally drafted with `ExpenseDB.add_expense` generating its own UUID internally — that breaks receipt storage, which needs the id *before* the row exists (the receipt filename is `<expense_id>.<ext>`). Fixed by making the caller (the `add_expense` service handler, Task 9) generate the id and pass it into `ExpenseDB.add_expense` as an explicit first argument (Task 4). This is reflected consistently across Tasks 4, 7, and 9 — check any future edit to `add_expense`'s signature against all three.
- **Type consistency check:** `ExpenseTrackerRuntime.async_add_expense`'s parameter order (`expense_id, amount, type_name, user, timestamp, receipt_path, note`) matches `ExpenseDB.add_expense` exactly (Task 4 defines it, Task 7 calls it positionally in that order) — verified, no drift.

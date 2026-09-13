# Expense Tracker Bank CSV Import — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the user upload a bank-exported CSV from a new "Import" dashboard tab, classify each transaction against a user-editable, drag-reorderable rule table (merchant pattern → expense type), and commit the matched rows as real expenses — with structural bank-noise (round-ups, ATM withdrawals, income) excluded automatically and unmatched rows surfaced for a one-time manual pick instead of ever being guessed.

**Architecture:** Two new pure/DB layers (`import_engine.py` for stateless CSV-parsing/classification, new methods on the existing `ExpenseDB` class for the `import_rules` table and expense dedupe) wired together by one new `runtime.py` orchestrator method, exposed as five new `expense_tracker.*` services following the exact pattern `add_type`/`remove_type`/`reorder_types`/`list_types` already established, surfaced on a new dashboard tab whose card reuses the drag-to-reorder implementation already shipped and fixed on the Types tab.

**Tech Stack:** Python 3.13, Home Assistant custom integration (`pytest-homeassistant-custom-component` test harness), SQLite (blocking, always via `hass.async_add_executor_job`), vanilla-JS Web Component dashboard card (no build step, no framework), HACS for release/distribution.

**Spec:** `docs/superpowers/specs/2026-09-12-expense-import-design.md`

## Global Constraints

- Every SQLite call (read or write) MUST run via `hass.async_add_executor_job` — never directly on the event loop. (Spec: "Verified facts"/db.py's own docstring; this exact bug class was hit live in `lovelace_setup.py` earlier in this project.)
- Structural exclusions (round-up, ATM withdrawal, credit rows) are hardcoded Python conditionals, never `import_rules` table entries.
- Unmatched rows are never guessed at a type — they always land in `needs_review`, full stop.
- `import_csv`'s `dry_run` field has no default; callers must always be explicit.
- Rule matching is case-insensitive substring matching against the normalized (uppercased) `Description` field, first match in `sort_order` wins.
- The dedupe key is SHA-256 of the tuple `(Value Date, Reference, Description, Debit, Credit)` exactly as they appear in the CSV — verified unique across the full 764-row real sample.
- All new services/schemas must validate against `homeassistant.helpers.service._SERVICES_SCHEMA` (enforced today by `tests/test_services_yaml_schema.py` — do not weaken or skip that test).
- Repo for all code in this plan: `~/Documents/projects/personal/expense-tracker-home-assistant`. Working directory for every command below is that repo root unless stated otherwise.

---

## Task 1: DB schema — `import_rules` table, `expenses.import_key`, new exceptions

**Files:**
- Modify: `custom_components/expense_tracker/const.py`
- Modify: `custom_components/expense_tracker/db.py`
- Test: `tests/test_db_schema.py`
- Test: `tests/test_db_expenses.py`

**Interfaces:**
- Produces: `db.DuplicateImportError`, `db.UnknownImportRuleError`, `db.DuplicateImportRuleError`, `db.ImportRuleSetMismatchError` (all `ValueError` subclasses). `ExpenseDB.add_expense(..., import_key: str | None = None)` (new optional trailing kwarg on the existing method). `ExpenseDB.expense_exists_by_import_key(import_key: str) -> bool`.

- [ ] **Step 1: Write the failing schema test**

Add to `tests/test_db_schema.py` (new test, appended to the file):

```python
def test_initialize_creates_import_rules_table_and_import_key_column(tmp_path):
    db = ExpenseDB(str(tmp_path / "expenses.db"))
    db.initialize()

    conn = sqlite3.connect(str(tmp_path / "expenses.db"))
    import_rules_columns = [row[1] for row in conn.execute("PRAGMA table_info(import_rules)")]
    expenses_columns = [row[1] for row in conn.execute("PRAGMA table_info(expenses)")]
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    conn.close()

    assert set(import_rules_columns) == {"pattern", "type_name", "sort_order"}
    assert "import_key" in expenses_columns
    assert version == 5
```

- [ ] **Step 2: Run it, confirm it fails**

Run: `.venv/bin/python -m pytest tests/test_db_schema.py::test_initialize_creates_import_rules_table_and_import_key_column -v`
Expected: FAIL — `import_rules` table doesn't exist yet / `version == 5` is false (currently 3).

- [ ] **Step 3: Bump `SCHEMA_VERSION` and add the two migration steps**

In `custom_components/expense_tracker/const.py`, change:

```python
SCHEMA_VERSION = 3
```
to:
```python
SCHEMA_VERSION = 5
```

In `custom_components/expense_tracker/db.py`, extend the `MIGRATIONS` list (append two entries after the existing three, keeping the comment style already established for each entry):

```python
MIGRATIONS: list[str] = [
    # v0 -> v1: no-op placeholder. ...
    "SELECT 1",
    # v1 -> v2: add the column expense-type ordering ...
    "ALTER TABLE expense_types ADD COLUMN sort_order INTEGER NOT NULL DEFAULT 0",
    # v2 -> v3: backfill sort_order for rows that predate the column ...
    "UPDATE expense_types SET sort_order = ("
    "SELECT COUNT(*) FROM expense_types e2 WHERE e2.name < expense_types.name)",
    # v3 -> v4: add the column bank-CSV-imported expenses store their
    # dedupe key in (see add_expense's import_key parameter and
    # DuplicateImportError). NULL for every expense added the normal way.
    "ALTER TABLE expenses ADD COLUMN import_key TEXT",
    # v4 -> v5: index the new column -- import_csv looks up by it once per
    # row of a multi-hundred-row statement, every time a file is imported.
    "CREATE INDEX idx_expenses_import_key ON expenses(import_key)",
]
```

Add the new table's `CREATE TABLE IF NOT EXISTS`, in `initialize()`, immediately after the existing `expenses` table creation (this is a brand-new table, not an alteration to an existing one, so — like `expense_types` and `expenses` themselves — it's created unconditionally every startup, not via the versioned `MIGRATIONS` list):

```python
            conn.execute(
                "CREATE TABLE IF NOT EXISTS expenses ("
                "id TEXT PRIMARY KEY, amount REAL NOT NULL, "
                "type_name TEXT NOT NULL, user TEXT NOT NULL, "
                "receipt_path TEXT, note TEXT, timestamp TEXT NOT NULL)"
            )
            conn.execute(
                "CREATE TABLE IF NOT EXISTS import_rules "
                "(pattern TEXT PRIMARY KEY, type_name TEXT NOT NULL, "
                "sort_order INTEGER NOT NULL)"
            )
```

- [ ] **Step 4: Run the test again, confirm it passes**

Run: `.venv/bin/python -m pytest tests/test_db_schema.py -v`
Expected: all PASS, including the pre-existing migration tests (they assert exact `SCHEMA_VERSION`-driven behavior — re-read `test_initialize_migrates_pre_sort_order_db_alphabetically` and `test_initialize_runs_pending_migrations_and_bumps_user_version` before moving on; neither should need edits, since they either monkeypatch `SCHEMA_VERSION`/`MIGRATIONS` themselves or don't assert an exact version number).

- [ ] **Step 5: Add the new exception classes**

In `custom_components/expense_tracker/db.py`, immediately after the existing `TypeSetMismatchError` class:

```python
class TypeSetMismatchError(ValueError):
    """Raised when reorder_types isn't given exactly the current set of
    type names. A partial list would leave the omitted types at whatever
    sort_order they already had, silently interleaving them with the
    reordered ones in a way nothing in the UI would explain."""


class DuplicateImportError(ValueError):
    """Raised by add_expense when import_key is given and an expense with
    that key already exists. This is what makes re-importing an
    overlapping bank statement safe -- see import_csv."""


class UnknownImportRuleError(ValueError):
    """Raised when referencing an import_rules pattern that does not
    exist."""


class DuplicateImportRuleError(ValueError):
    """Raised when adding an import rule whose pattern already exists."""


class ImportRuleSetMismatchError(ValueError):
    """Raised when reorder_import_rules isn't given exactly the current
    set of patterns -- same reasoning as TypeSetMismatchError."""
```

- [ ] **Step 6: Write the failing test for `add_expense`'s new `import_key` parameter**

Add to `tests/test_db_expenses.py`:

```python
def test_add_expense_with_import_key_then_duplicate_raises(tmp_path):
    db = ExpenseDB(str(tmp_path / "expenses.db"))
    db.initialize()

    db.add_expense(
        "id-1", 10.0, "Groceries", "person.armend",
        "2026-09-07T10:00:00+00:00", import_key="abc123",
    )

    assert db.expense_exists_by_import_key("abc123") is True
    assert db.expense_exists_by_import_key("does-not-exist") is False
    with pytest.raises(DuplicateImportError):
        db.add_expense(
            "id-2", 5.0, "Groceries", "person.armend",
            "2026-09-08T10:00:00+00:00", import_key="abc123",
        )


def test_add_expense_without_import_key_never_collides(tmp_path):
    """Manually-added expenses (import_key=None, the default) must never
    trip the duplicate check against each other."""
    db = ExpenseDB(str(tmp_path / "expenses.db"))
    db.initialize()

    db.add_expense("id-1", 10.0, "Groceries", "person.armend", "2026-09-07T10:00:00+00:00")
    db.add_expense("id-2", 5.0, "Groceries", "person.armend", "2026-09-08T10:00:00+00:00")

    types = db.list_expenses()
    assert len(types) == 2
```

Add the needed imports at the top of `tests/test_db_expenses.py` (check the file first — if `pytest`, `ExpenseDB`, or `DuplicateImportError` aren't already imported there, add them: `import pytest` and `from custom_components.expense_tracker.db import DuplicateImportError, ExpenseDB`).

- [ ] **Step 7: Run it, confirm it fails**

Run: `.venv/bin/python -m pytest tests/test_db_expenses.py -k import_key -v`
Expected: FAIL — `add_expense()` doesn't accept `import_key` yet / `expense_exists_by_import_key` doesn't exist.

- [ ] **Step 8: Implement `import_key` on `add_expense` and `expense_exists_by_import_key`**

Replace the existing `add_expense` method in `db.py`:

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
        import_key: str | None = None,
    ) -> None:
        conn = sqlite3.connect(self._db_path)
        try:
            existing_type = conn.execute(
                "SELECT 1 FROM expense_types WHERE name = ?", (type_name,)
            ).fetchone()
            if not existing_type:
                raise UnknownTypeError(f"Type '{type_name}' does not exist")
            if import_key is not None:
                existing_import = conn.execute(
                    "SELECT 1 FROM expenses WHERE import_key = ?", (import_key,)
                ).fetchone()
                if existing_import:
                    raise DuplicateImportError(
                        f"An expense with import_key '{import_key}' already exists"
                    )
            conn.execute(
                "INSERT INTO expenses "
                "(id, amount, type_name, user, receipt_path, note, timestamp, import_key) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    expense_id, amount, type_name, user, receipt_path, note,
                    timestamp, import_key,
                ),
            )
            conn.commit()
        finally:
            conn.close()

    def expense_exists_by_import_key(self, import_key: str) -> bool:
        conn = sqlite3.connect(self._db_path)
        try:
            row = conn.execute(
                "SELECT 1 FROM expenses WHERE import_key = ?", (import_key,)
            ).fetchone()
            return row is not None
        finally:
            conn.close()
```

- [ ] **Step 9: Run the tests, confirm they pass**

Run: `.venv/bin/python -m pytest tests/test_db_expenses.py tests/test_db_schema.py -v`
Expected: all PASS.

- [ ] **Step 10: Run the full suite to check for regressions**

Run: `.venv/bin/python -m pytest -q`
Expected: all PASS (existing `add_expense` callers never pass `import_key`, so its default `None` keeps them unaffected).

- [ ] **Step 11: Commit**

```bash
git add custom_components/expense_tracker/const.py custom_components/expense_tracker/db.py tests/test_db_schema.py tests/test_db_expenses.py
git commit -m "Add import_rules table, expenses.import_key column, and dedupe-aware add_expense"
```

---

## Task 2: `import_rules` CRUD + guard `remove_type` against in-use rules

**Files:**
- Modify: `custom_components/expense_tracker/db.py`
- Test: `tests/test_db_import_rules.py` (new file)
- Test: `tests/test_db_types.py`

**Interfaces:**
- Consumes: `db.DuplicateImportRuleError`, `db.UnknownImportRuleError`, `db.ImportRuleSetMismatchError` (Task 1).
- Produces: `ExpenseDB.list_import_rules() -> list[tuple[str, str]]` (pattern, type_name, in `sort_order`), `ExpenseDB.add_import_rule(pattern: str, type_name: str) -> None`, `ExpenseDB.remove_import_rule(pattern: str) -> None`, `ExpenseDB.reorder_import_rules(ordered_patterns: list[str]) -> None`.

This task is a near-exact mirror of `expense_types`'/`reorder_types`'s existing methods — same shape, same error-handling style, applied to `import_rules`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_db_import_rules.py`:

```python
import pytest

from custom_components.expense_tracker.db import (
    DuplicateImportRuleError,
    ExpenseDB,
    ImportRuleSetMismatchError,
    TypeInUseError,
    UnknownImportRuleError,
)


def test_add_import_rule_then_list_returns_it_in_order(tmp_path):
    db = ExpenseDB(str(tmp_path / "expenses.db"))
    db.initialize()

    db.add_import_rule("SPAR", "Groceries")
    db.add_import_rule("HIB PETROL", "Transport")

    assert db.list_import_rules() == [("SPAR", "Groceries"), ("HIB PETROL", "Transport")]


def test_add_import_rule_rejects_duplicate_pattern(tmp_path):
    db = ExpenseDB(str(tmp_path / "expenses.db"))
    db.initialize()
    db.add_import_rule("SPAR", "Groceries")

    with pytest.raises(DuplicateImportRuleError):
        db.add_import_rule("SPAR", "Utilities")


def test_add_import_rule_rejects_unknown_type(tmp_path):
    db = ExpenseDB(str(tmp_path / "expenses.db"))
    db.initialize()

    from custom_components.expense_tracker.db import UnknownTypeError

    with pytest.raises(UnknownTypeError):
        db.add_import_rule("SPAR", "NotARealType")


def test_remove_import_rule_deletes_it(tmp_path):
    db = ExpenseDB(str(tmp_path / "expenses.db"))
    db.initialize()
    db.add_import_rule("SPAR", "Groceries")

    db.remove_import_rule("SPAR")

    assert db.list_import_rules() == []


def test_remove_import_rule_rejects_unknown_pattern(tmp_path):
    db = ExpenseDB(str(tmp_path / "expenses.db"))
    db.initialize()

    with pytest.raises(UnknownImportRuleError):
        db.remove_import_rule("NOPE")


def test_reorder_import_rules_persists_new_order(tmp_path):
    db = ExpenseDB(str(tmp_path / "expenses.db"))
    db.initialize()
    db.add_import_rule("SPAR", "Groceries")
    db.add_import_rule("HIB PETROL", "Transport")
    db.add_import_rule("NETFLIX", "Entertainment")

    db.reorder_import_rules(["NETFLIX", "SPAR", "HIB PETROL"])

    assert [p for p, _t in db.list_import_rules()] == ["NETFLIX", "SPAR", "HIB PETROL"]


def test_reorder_import_rules_rejects_partial_list(tmp_path):
    db = ExpenseDB(str(tmp_path / "expenses.db"))
    db.initialize()
    db.add_import_rule("SPAR", "Groceries")
    db.add_import_rule("HIB PETROL", "Transport")

    with pytest.raises(ImportRuleSetMismatchError):
        db.reorder_import_rules(["SPAR"])


def test_remove_type_rejects_when_referenced_by_import_rule(tmp_path):
    db = ExpenseDB(str(tmp_path / "expenses.db"))
    db.initialize()
    db.add_import_rule("SPAR", "Groceries")

    with pytest.raises(TypeInUseError):
        db.remove_type("Groceries")

    # Rejected removal must not have touched the type row.
    names = [name for name, _icon in db.list_types()]
    assert "Groceries" in names
```

- [ ] **Step 2: Run them, confirm they fail**

Run: `.venv/bin/python -m pytest tests/test_db_import_rules.py -v`
Expected: FAIL — none of the new methods exist yet.

- [ ] **Step 3: Implement the CRUD methods**

Add to `db.py`, immediately after `reorder_types` and before `remove_type`:

```python
    def list_import_rules(self) -> list[tuple[str, str]]:
        conn = sqlite3.connect(self._db_path)
        try:
            rows = conn.execute(
                "SELECT pattern, type_name FROM import_rules ORDER BY sort_order"
            ).fetchall()
            return rows
        finally:
            conn.close()

    def add_import_rule(self, pattern: str, type_name: str) -> None:
        conn = sqlite3.connect(self._db_path)
        try:
            existing_type = conn.execute(
                "SELECT 1 FROM expense_types WHERE name = ?", (type_name,)
            ).fetchone()
            if not existing_type:
                raise UnknownTypeError(f"Type '{type_name}' does not exist")
            existing_rule = conn.execute(
                "SELECT 1 FROM import_rules WHERE pattern = ?", (pattern,)
            ).fetchone()
            if existing_rule:
                raise DuplicateImportRuleError(
                    f"Import rule for '{pattern}' already exists"
                )
            next_order = conn.execute(
                "SELECT COALESCE(MAX(sort_order), -1) + 1 FROM import_rules"
            ).fetchone()[0]
            conn.execute(
                "INSERT INTO import_rules (pattern, type_name, sort_order) "
                "VALUES (?, ?, ?)",
                (pattern, type_name, next_order),
            )
            conn.commit()
        finally:
            conn.close()

    def remove_import_rule(self, pattern: str) -> None:
        conn = sqlite3.connect(self._db_path)
        try:
            existing = conn.execute(
                "SELECT 1 FROM import_rules WHERE pattern = ?", (pattern,)
            ).fetchone()
            if not existing:
                raise UnknownImportRuleError(f"Import rule for '{pattern}' does not exist")
            conn.execute("DELETE FROM import_rules WHERE pattern = ?", (pattern,))
            conn.commit()
        finally:
            conn.close()

    def reorder_import_rules(self, ordered_patterns: list[str]) -> None:
        conn = sqlite3.connect(self._db_path)
        try:
            existing_patterns = {
                row[0]
                for row in conn.execute("SELECT pattern FROM import_rules").fetchall()
            }
            if (
                set(ordered_patterns) != existing_patterns
                or len(ordered_patterns) != len(existing_patterns)
            ):
                raise ImportRuleSetMismatchError(
                    "Reorder must include every existing import rule exactly once"
                )
            conn.executemany(
                "UPDATE import_rules SET sort_order = ? WHERE pattern = ?",
                [(i, pattern) for i, pattern in enumerate(ordered_patterns)],
            )
            conn.commit()
        finally:
            conn.close()
```

- [ ] **Step 4: Extend `remove_type`'s in-use check to cover `import_rules`**

Replace the body of `remove_type` (keep the method signature and docstring position the same, only the in-use check changes):

```python
    def remove_type(self, name: str) -> None:
        conn = sqlite3.connect(self._db_path)
        try:
            existing = conn.execute(
                "SELECT 1 FROM expense_types WHERE name = ?", (name,)
            ).fetchone()
            if not existing:
                raise UnknownTypeError(f"Type '{name}' does not exist")
            in_use_count = conn.execute(
                "SELECT COUNT(*) FROM expenses WHERE type_name = ?", (name,)
            ).fetchone()[0]
            if in_use_count > 0:
                raise TypeInUseError(
                    f"Cannot remove '{name}': {in_use_count} expense(s) "
                    "still use this type"
                )
            rule_count = conn.execute(
                "SELECT COUNT(*) FROM import_rules WHERE type_name = ?", (name,)
            ).fetchone()[0]
            if rule_count > 0:
                raise TypeInUseError(
                    f"Cannot remove '{name}': {rule_count} import rule(s) "
                    "still target this type"
                )
            conn.execute("DELETE FROM expense_types WHERE name = ?", (name,))
            conn.commit()
        finally:
            conn.close()
```

This closes a real gap: without it, deleting a type that an import rule still points at would let a future import try to insert an expense with a type that no longer exists.

- [ ] **Step 5: Run the tests, confirm they pass**

Run: `.venv/bin/python -m pytest tests/test_db_import_rules.py tests/test_db_types.py -v`
Expected: all PASS.

- [ ] **Step 6: Run the full suite**

Run: `.venv/bin/python -m pytest -q`
Expected: all PASS.

- [ ] **Step 7: Commit**

```bash
git add custom_components/expense_tracker/db.py tests/test_db_import_rules.py
git commit -m "Add import_rules CRUD; block type removal while an import rule targets it"
```

---

## Task 3: `import_engine.py` — pure CSV parsing and classification

**Files:**
- Create: `custom_components/expense_tracker/import_engine.py`
- Test: `tests/test_import_engine.py` (new file)

**Interfaces:**
- Produces: `ParsedRow`, `MatchedExpense`, `ReviewRow`, `ClassifyResult` (dataclasses), `parse_csv_text(csv_text: str) -> list[ParsedRow]`, `classify_row(row: ParsedRow, rules: list[tuple[str, str]]) -> ClassifyResult`, `compute_import_key(row: ParsedRow) -> str`, `CsvFormatError` (raised by `parse_csv_text` on a header mismatch).
- No dependency on `hass`, `sqlite3`, or any other module in this package — deliberately pure and independently testable (see spec's "Data flow" and the file-structure guidance to design isolated, single-purpose units).

- [ ] **Step 1: Write the failing tests**

Create `tests/test_import_engine.py`:

```python
from custom_components.expense_tracker.import_engine import (
    ClassifyResult,
    CsvFormatError,
    classify_row,
    compute_import_key,
    parse_csv_text,
)
import pytest

HEADER = (
    '"Nr. / No.","Data e transaksionit / Value Date",'
    '"Perfituesi / Beneficiary(Sender)","Pershkrimi / Description",'
    '"Referenca / Reference","Data e procesimit / Processing Date",'
    '"Kreditim / Credit","Debitim / Debit"\n'
)


def _row(**overrides):
    defaults = dict(
        nr="1",
        date="04.09.2026",
        beneficiary="POS OFF US DOMESTIC",
        description="POS TRANSACTION SPAR KOSOVA PRISHTINE QZ SPAR KOSOVA PRISHTINE QZ Original time: 04/09/2026 14:08",
        reference="FTL26247RG8LX",
        processing_date="04.09.2026",
        credit="0",
        debit="12.50",
    )
    defaults.update(overrides)
    return (
        f'"{defaults["nr"]}","{defaults["date"]}","{defaults["beneficiary"]}",'
        f'"{defaults["description"]}","{defaults["reference"]}",'
        f'"{defaults["processing_date"]}","{defaults["credit"]}","{defaults["debit"]}"'
    )


def test_parse_csv_text_parses_rows():
    csv_text = HEADER + _row() + "\n"

    rows = parse_csv_text(csv_text)

    assert len(rows) == 1
    assert rows[0].value_date == "04.09.2026"
    assert rows[0].debit == 12.50
    assert rows[0].credit == 0.0


def test_parse_csv_text_rejects_wrong_header():
    with pytest.raises(CsvFormatError):
        parse_csv_text("wrong,header\n1,2\n")


def test_classify_row_excludes_credit_rows():
    csv_text = HEADER + _row(credit="500", debit="0", description="Prej dikujt") + "\n"
    row = parse_csv_text(csv_text)[0]

    result = classify_row(row, rules=[])

    assert result.outcome == "excluded_credit"


def test_classify_row_excludes_round_up_payments():
    csv_text = HEADER + _row(
        description="TRANSFER ARMEND KADRIJA  ROUND UP PAYMENT Parent Reference :FTL26247YXXYY"
    ) + "\n"
    row = parse_csv_text(csv_text)[0]

    result = classify_row(row, rules=[("SPAR", "Groceries")])

    assert result.outcome == "excluded_roundup"


def test_classify_row_excludes_atm_withdrawal_but_not_its_fee():
    csv_text = HEADER + (
        _row(description="ATM TRANSACTION Withdrawal through ATM BT", debit="700") + "\n"
        + _row(description="ATM TRANSACTION ATM charge Withdrawal through ATM BT", debit="0.25") + "\n"
    )
    withdrawal, fee = parse_csv_text(csv_text)

    withdrawal_result = classify_row(withdrawal, rules=[("ATM charge", "Bank Fees")])
    fee_result = classify_row(fee, rules=[("ATM charge", "Bank Fees")])

    assert withdrawal_result.outcome == "excluded_atm_withdrawal"
    assert fee_result.outcome == "matched"
    assert fee_result.matched.type_name == "Bank Fees"


def test_classify_row_matches_case_insensitive_substring_first_match_wins():
    csv_text = HEADER + _row(
        description="POS TRANSACTION Albi Market 43 PRISHTINE QZ Albi Market 43 PRISHTINE QZ"
    ) + "\n"
    row = parse_csv_text(csv_text)[0]
    rules = [("ALBI MARKET", "Groceries"), ("ALBI", "Shopping")]

    result = classify_row(row, rules=rules)

    assert result.outcome == "matched"
    assert result.matched.type_name == "Groceries"
    assert result.matched.amount == 12.50
    assert result.matched.date == "2026-09-04"


def test_classify_row_unmatched_goes_to_review():
    csv_text = HEADER + _row(description="POS TRANSACTION Some New Shop QZ") + "\n"
    row = parse_csv_text(csv_text)[0]

    result = classify_row(row, rules=[("SPAR", "Groceries")])

    assert result.outcome == "needs_review"
    assert result.review.amount == 12.50
    assert result.review.description.startswith("POS TRANSACTION Some New Shop")


def test_compute_import_key_is_stable_and_distinguishes_atm_pair():
    csv_text = HEADER + (
        _row(reference="SHARED_REF", description="ATM TRANSACTION Withdrawal through ATM BT", debit="700") + "\n"
        + _row(reference="SHARED_REF", description="ATM TRANSACTION ATM charge Withdrawal through ATM BT", debit="0.25") + "\n"
    )
    withdrawal, fee = parse_csv_text(csv_text)

    key_a = compute_import_key(withdrawal)
    key_b = compute_import_key(fee)
    key_a_again = compute_import_key(withdrawal)

    assert key_a != key_b  # same Reference, different Description/Debit -> distinct keys
    assert key_a == key_a_again  # deterministic
```

- [ ] **Step 2: Run them, confirm they fail**

Run: `.venv/bin/python -m pytest tests/test_import_engine.py -v`
Expected: FAIL — `custom_components.expense_tracker.import_engine` doesn't exist yet.

- [ ] **Step 3: Implement `import_engine.py`**

Create `custom_components/expense_tracker/import_engine.py`:

```python
"""Pure CSV parsing and transaction classification for the bank-CSV
import feature. Deliberately has no dependency on Home Assistant, sqlite,
or any other module in this package -- every function here is a plain
function over plain data, testable in full isolation and without the
`hass` fixture.

Column names, date format, and the round-up/ATM exclusion patterns below
are specific to this project's bank's CSV export -- verified against a
real 764-row, 6-month statement (see the design spec's "Verified facts"
section). This is not a generic bank-CSV parser.
"""
from __future__ import annotations

import csv
import hashlib
import io
from dataclasses import dataclass

EXPECTED_HEADER = {
    "Nr. / No.",
    "Data e transaksionit / Value Date",
    "Perfituesi / Beneficiary(Sender)",
    "Pershkrimi / Description",
    "Referenca / Reference",
    "Data e procesimit / Processing Date",
    "Kreditim / Credit",
    "Debitim / Debit",
}

# Round-up transfers to the user's own savings sub-account -- verified
# against all 296 round-up rows in the real sample with zero misses (each
# one's "Parent Reference :FTLxxxxx" also matches the Reference column of
# the purchase that triggered it, confirming these are purely internal).
ROUND_UP_MARKER = "ROUND UP PAYMENT"

# ATM withdrawal lines are excluded (nothing downstream tells us what the
# cash was spent on); the paired ATM fee line is a real, itemizable
# expense and must NOT be excluded. The only reliable way to tell them
# apart is this substring -- amount alone isn't a safe signal.
ATM_PREFIX = "ATM TRANSACTION"
ATM_FEE_MARKER = "ATM charge"


class CsvFormatError(ValueError):
    """Raised when the uploaded file's header doesn't match this bank's
    expected export format. Raised before any row is processed -- no
    partial import from a file that isn't even the right shape."""


@dataclass(frozen=True)
class ParsedRow:
    value_date: str  # "DD.MM.YYYY", as printed by the bank
    beneficiary: str
    description: str
    reference: str
    credit: float
    debit: float


@dataclass(frozen=True)
class MatchedExpense:
    amount: float
    type_name: str
    date: str  # "YYYY-MM-DD", ready for dt_helpers.local_date_string_to_utc_iso
    note: str
    import_key: str


@dataclass(frozen=True)
class ReviewRow:
    date: str
    amount: float
    description: str
    import_key: str


@dataclass(frozen=True)
class ClassifyResult:
    outcome: str  # "excluded_credit" | "excluded_roundup" | "excluded_atm_withdrawal" | "matched" | "needs_review"
    matched: MatchedExpense | None = None
    review: ReviewRow | None = None


def parse_csv_text(csv_text: str) -> list[ParsedRow]:
    reader = csv.DictReader(io.StringIO(csv_text))
    if reader.fieldnames is None or set(reader.fieldnames) != EXPECTED_HEADER:
        raise CsvFormatError(
            "CSV header doesn't match the expected bank export format "
            f"(got columns: {reader.fieldnames})"
        )
    rows = []
    for raw in reader:
        rows.append(
            ParsedRow(
                value_date=raw["Data e transaksionit / Value Date"],
                beneficiary=raw["Perfituesi / Beneficiary(Sender)"],
                description=raw["Pershkrimi / Description"],
                reference=raw["Referenca / Reference"],
                credit=float(raw["Kreditim / Credit"]),
                debit=float(raw["Debitim / Debit"]),
            )
        )
    return rows


def compute_import_key(row: ParsedRow) -> str:
    """SHA-256 of the tuple verified unique across the full real 764-row
    sample: (Value Date, Reference, Description, Debit, Credit). Reference
    alone is NOT unique (an ATM withdrawal and its paired fee share one)."""
    raw = f"{row.value_date}|{row.reference}|{row.description}|{row.debit}|{row.credit}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _bank_date_to_iso(value_date: str) -> str:
    day, month, year = value_date.split(".")
    return f"{year}-{month}-{day}"


def classify_row(row: ParsedRow, rules: list[tuple[str, str]]) -> ClassifyResult:
    if row.credit > 0:
        return ClassifyResult(outcome="excluded_credit")
    if ROUND_UP_MARKER in row.description:
        return ClassifyResult(outcome="excluded_roundup")
    if row.description.startswith(ATM_PREFIX) and ATM_FEE_MARKER not in row.description:
        return ClassifyResult(outcome="excluded_atm_withdrawal")

    import_key = compute_import_key(row)
    normalized = row.description.upper()
    for pattern, type_name in rules:
        if pattern.upper() in normalized:
            return ClassifyResult(
                outcome="matched",
                matched=MatchedExpense(
                    amount=row.debit,
                    type_name=type_name,
                    date=_bank_date_to_iso(row.value_date),
                    note=row.description[:200],
                    import_key=import_key,
                ),
            )

    return ClassifyResult(
        outcome="needs_review",
        review=ReviewRow(
            date=_bank_date_to_iso(row.value_date),
            amount=row.debit,
            description=row.description,
            import_key=import_key,
        ),
    )
```

- [ ] **Step 4: Run the tests, confirm they pass**

Run: `.venv/bin/python -m pytest tests/test_import_engine.py -v`
Expected: all PASS.

- [ ] **Step 5: Run the full suite**

Run: `.venv/bin/python -m pytest -q`
Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
git add custom_components/expense_tracker/import_engine.py tests/test_import_engine.py
git commit -m "Add pure CSV parsing and transaction classification (import_engine.py)"
```

---

## Task 4: `runtime.py` orchestration — rule CRUD wrappers + `async_import_csv`

**Files:**
- Modify: `custom_components/expense_tracker/runtime.py`
- Test: `tests/test_runtime_import.py` (new file)

**Interfaces:**
- Consumes: `db.py`'s Task 1/2 methods; `import_engine.parse_csv_text`, `import_engine.classify_row` (Task 3); `homeassistant.components.file_upload.process_uploaded_file` (same API `receipts.py` already uses); `dt_helpers.local_date_string_to_utc_iso` (already exists).
- Produces: `ExpenseTrackerRuntime.async_list_import_rules() -> list[tuple[str, str]]`, `async_add_import_rule(pattern, type_name) -> None`, `async_remove_import_rule(pattern) -> None`, `async_reorder_import_rules(ordered_patterns) -> None`, `async_import_csv(file_id: str, dry_run: bool, user: str) -> dict` returning exactly the shape:
  ```python
  {
      "imported": int, "skipped_duplicate": int, "excluded_roundup": int,
      "excluded_atm_withdrawal": int, "excluded_credit": int,
      "needs_review": list[dict],  # each {"date", "amount", "description", "import_key"}
  }
  ```

- [ ] **Step 1: Write the failing tests**

Create `tests/test_runtime_import.py`:

```python
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.expense_tracker.const import DOMAIN
from custom_components.expense_tracker.db import ExpenseDB
from custom_components.expense_tracker.runtime import ExpenseTrackerRuntime

HEADER = (
    '"Nr. / No.","Data e transaksionit / Value Date",'
    '"Perfituesi / Beneficiary(Sender)","Pershkrimi / Description",'
    '"Referenca / Reference","Data e procesimit / Processing Date",'
    '"Kreditim / Credit","Debitim / Debit"\n'
)


def _row(nr, description, debit, credit="0", reference=None):
    reference = reference or f"REF{nr}"
    return (
        f'"{nr}","04.09.2026","POS OFF US DOMESTIC","{description}",'
        f'"{reference}","04.09.2026","{credit}","{debit}"'
    )


async def _setup_runtime(hass, tmp_path):
    hass.config.config_dir = str(tmp_path)
    db = ExpenseDB(str(tmp_path / "expenses.db"))
    runtime = ExpenseTrackerRuntime(hass, db)
    await runtime.async_initialize()
    return runtime


async def test_import_rule_crud_round_trips(hass, tmp_path):
    runtime = await _setup_runtime(hass, tmp_path)

    await runtime.async_add_import_rule("SPAR", "Groceries")
    await runtime.async_add_import_rule("HIB PETROL", "Transport")
    assert await runtime.async_list_import_rules() == [
        ("SPAR", "Groceries"), ("HIB PETROL", "Transport"),
    ]

    await runtime.async_reorder_import_rules(["HIB PETROL", "SPAR"])
    assert [p for p, _t in await runtime.async_list_import_rules()] == ["HIB PETROL", "SPAR"]

    await runtime.async_remove_import_rule("SPAR")
    assert await runtime.async_list_import_rules() == [("HIB PETROL", "Transport")]


async def test_import_csv_dry_run_classifies_without_writing(hass, tmp_path, stage_uploaded_file):
    runtime = await _setup_runtime(hass, tmp_path)
    await runtime.async_add_import_rule("SPAR", "Groceries")
    csv_text = HEADER + (
        _row(1, "POS TRANSACTION SPAR KOSOVA PRISHTINE QZ", "12.50") + "\n"
        + _row(2, "TRANSFER ARMEND KADRIJA  ROUND UP PAYMENT Parent Reference :X", "0.50") + "\n"
        + _row(3, "ATM TRANSACTION Withdrawal through ATM BT", "100") + "\n"
        + _row(4, "Salary", "0", credit="1000") + "\n"
        + _row(5, "POS TRANSACTION Unknown New Shop QZ", "9.99") + "\n"
    )
    file_id = await stage_uploaded_file("statement.csv", csv_text.encode("utf-8"))

    result = await runtime.async_import_csv(file_id, dry_run=True, user="person.armend")

    assert result["imported"] == 1
    assert result["skipped_duplicate"] == 0
    assert result["excluded_roundup"] == 1
    assert result["excluded_atm_withdrawal"] == 1
    assert result["excluded_credit"] == 1
    assert len(result["needs_review"]) == 1
    assert result["needs_review"][0]["description"].startswith("POS TRANSACTION Unknown New Shop")
    assert (await runtime.async_list_expenses()) == []  # nothing written


async def test_import_csv_commit_writes_and_is_idempotent_on_rerun(hass, tmp_path, stage_uploaded_file):
    runtime = await _setup_runtime(hass, tmp_path)
    await runtime.async_add_import_rule("SPAR", "Groceries")
    csv_text = HEADER + _row(1, "POS TRANSACTION SPAR KOSOVA PRISHTINE QZ", "12.50") + "\n"
    file_id = await stage_uploaded_file("statement.csv", csv_text.encode("utf-8"))

    first = await runtime.async_import_csv(file_id, dry_run=False, user="person.armend")
    assert first["imported"] == 1
    expenses = await runtime.async_list_expenses()
    assert len(expenses) == 1
    assert expenses[0]["amount"] == 12.50
    assert expenses[0]["type"] == "Groceries"
    assert expenses[0]["user"] == "person.armend"

    file_id_2 = await stage_uploaded_file("statement2.csv", csv_text.encode("utf-8"))
    second = await runtime.async_import_csv(file_id_2, dry_run=False, user="person.armend")
    assert second["imported"] == 0
    assert second["skipped_duplicate"] == 1
    assert len(await runtime.async_list_expenses()) == 1  # still just one row
```

- [ ] **Step 2: Run them, confirm they fail**

Run: `.venv/bin/python -m pytest tests/test_runtime_import.py -v`
Expected: FAIL — none of the new `runtime.py` methods exist yet.

- [ ] **Step 3: Implement the runtime additions**

In `custom_components/expense_tracker/runtime.py`, update the imports and add the new methods. Replace the top of the file:

```python
"""Runtime coordination layer: marshals ExpenseDB calls off the event
loop and pushes fresh state to sensors after every write."""
from __future__ import annotations

import uuid

from homeassistant.components.file_upload import process_uploaded_file
from homeassistant.core import HomeAssistant

from .db import DuplicateImportError, ExpenseDB, UnknownTypeError
from .import_engine import classify_row, parse_csv_text
```

Add the new methods immediately after `async_reorder_types` and before `async_add_expense`:

```python
    async def async_list_import_rules(self) -> list[tuple[str, str]]:
        return await self.hass.async_add_executor_job(self.db.list_import_rules)

    async def async_add_import_rule(self, pattern: str, type_name: str) -> None:
        await self.hass.async_add_executor_job(
            self.db.add_import_rule, pattern, type_name
        )

    async def async_remove_import_rule(self, pattern: str) -> None:
        await self.hass.async_add_executor_job(self.db.remove_import_rule, pattern)

    async def async_reorder_import_rules(self, ordered_patterns: list[str]) -> None:
        await self.hass.async_add_executor_job(
            self.db.reorder_import_rules, ordered_patterns
        )

    async def async_import_csv(self, file_id: str, dry_run: bool, user: str) -> dict:
        """Parse an uploaded bank CSV, classify every row against the
        current import_rules, and either report what would happen
        (dry_run=True) or actually write the matched rows (dry_run=False).

        Reading the file and every DB call below are blocking I/O and run
        via hass.async_add_executor_job; parse_csv_text/classify_row are
        pure in-memory functions over already-decoded text (no I/O), so
        they run directly in this coroutine like any other CPU-only work.
        """
        csv_text = await self.hass.async_add_executor_job(
            _read_uploaded_csv_text, self.hass, file_id
        )
        rules = await self.async_list_import_rules()
        rows = parse_csv_text(csv_text)
        classified = [classify_row(row, rules) for row in rows]

        result = {
            "imported": 0,
            "skipped_duplicate": 0,
            "excluded_roundup": 0,
            "excluded_atm_withdrawal": 0,
            "excluded_credit": 0,
            "needs_review": [],
        }
        wrote_anything = False
        for outcome in classified:
            if outcome.outcome in (
                "excluded_roundup", "excluded_atm_withdrawal", "excluded_credit",
            ):
                result[outcome.outcome] += 1
            elif outcome.outcome == "needs_review":
                result["needs_review"].append(
                    {
                        "date": outcome.review.date,
                        "amount": outcome.review.amount,
                        "description": outcome.review.description,
                        "import_key": outcome.review.import_key,
                    }
                )
            elif outcome.outcome == "matched":
                m = outcome.matched
                if dry_run:
                    exists = await self.hass.async_add_executor_job(
                        self.db.expense_exists_by_import_key, m.import_key
                    )
                    if exists:
                        result["skipped_duplicate"] += 1
                    else:
                        result["imported"] += 1
                else:
                    try:
                        await self.hass.async_add_executor_job(
                            self.db.add_expense,
                            str(uuid.uuid4()),
                            m.amount,
                            m.type_name,
                            user,
                            _local_midnight_utc_iso(m.date),
                            None,
                            m.note,
                            m.import_key,
                        )
                        result["imported"] += 1
                        wrote_anything = True
                    except DuplicateImportError:
                        result["skipped_duplicate"] += 1
                    except UnknownTypeError:
                        # Defensive only: add_import_rule already requires
                        # an existing type, and remove_type now refuses to
                        # delete a type an import rule targets -- this
                        # shouldn't be reachable, but if it ever is, treat
                        # it as a row needing a human decision rather than
                        # aborting the whole batch.
                        result["needs_review"].append(
                            {
                                "date": m.date,
                                "amount": m.amount,
                                "description": m.note,
                                "import_key": m.import_key,
                            }
                        )

        if wrote_anything:
            await self._async_refresh_sensors()
        return result
```

Add the two small module-level helpers used above, at the bottom of the file (after the class, or right below the imports — either is fine, this project has no fixed convention for helper placement in this file yet, so put them directly below the imports for visibility):

```python
def _read_uploaded_csv_text(hass: HomeAssistant, file_id: str) -> str:
    with process_uploaded_file(hass, file_id) as file_path:
        return file_path.read_text(encoding="utf-8-sig")


def _local_midnight_utc_iso(bare_date: str) -> str:
    # Deferred import to avoid a module-level cycle: dt_helpers is a leaf
    # module with no dependency on runtime.py, but importing it at the top
    # alongside everything else here isn't necessary either -- this is the
    # only call site.
    from .dt_helpers import local_date_string_to_utc_iso

    return local_date_string_to_utc_iso(bare_date)
```

- [ ] **Step 4: Run the tests, confirm they pass**

Run: `.venv/bin/python -m pytest tests/test_runtime_import.py -v`
Expected: all PASS.

- [ ] **Step 5: Run the full suite**

Run: `.venv/bin/python -m pytest -q`
Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
git add custom_components/expense_tracker/runtime.py tests/test_runtime_import.py
git commit -m "Add import_csv orchestration and import_rules wrappers to runtime.py"
```

---

## Task 5: Services — `add_import_rule`, `remove_import_rule`, `reorder_import_rules`, `list_import_rules`, `import_csv`

**Files:**
- Modify: `custom_components/expense_tracker/services.py`
- Modify: `custom_components/expense_tracker/services.yaml`
- Test: `tests/test_services_import.py` (new file)

**Interfaces:**
- Consumes: `runtime.py`'s Task 4 methods; `services.py`'s existing `_resolve_user_from_context` helper.
- Produces: registered HA services `expense_tracker.add_import_rule`, `expense_tracker.remove_import_rule`, `expense_tracker.reorder_import_rules`, `expense_tracker.list_import_rules`, `expense_tracker.import_csv`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_services_import.py`:

```python
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.exceptions import ServiceValidationError

from custom_components.expense_tracker.const import DOMAIN

HEADER = (
    '"Nr. / No.","Data e transaksionit / Value Date",'
    '"Perfituesi / Beneficiary(Sender)","Pershkrimi / Description",'
    '"Referenca / Reference","Data e procesimit / Processing Date",'
    '"Kreditim / Credit","Debitim / Debit"\n'
)
ROW = (
    '"1","04.09.2026","POS OFF US DOMESTIC",'
    '"POS TRANSACTION SPAR KOSOVA PRISHTINE QZ","REF1","04.09.2026","0","12.50"\n'
)


async def _setup(hass, tmp_path):
    hass.config.config_dir = str(tmp_path)
    entry = MockConfigEntry(domain=DOMAIN)
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def test_add_list_reorder_remove_import_rule_services(hass, tmp_path):
    await _setup(hass, tmp_path)

    await hass.services.async_call(
        DOMAIN, "add_import_rule", {"pattern": "SPAR", "type": "Groceries"}, blocking=True,
    )
    await hass.services.async_call(
        DOMAIN, "add_import_rule", {"pattern": "HIB PETROL", "type": "Transport"}, blocking=True,
    )
    result = await hass.services.async_call(
        DOMAIN, "list_import_rules", {}, blocking=True, return_response=True,
    )
    assert result["rules"] == [
        {"pattern": "SPAR", "type": "Groceries"},
        {"pattern": "HIB PETROL", "type": "Transport"},
    ]

    await hass.services.async_call(
        DOMAIN, "reorder_import_rules",
        {"patterns": ["HIB PETROL", "SPAR"]}, blocking=True,
    )
    result = await hass.services.async_call(
        DOMAIN, "list_import_rules", {}, blocking=True, return_response=True,
    )
    assert [r["pattern"] for r in result["rules"]] == ["HIB PETROL", "SPAR"]

    await hass.services.async_call(
        DOMAIN, "remove_import_rule", {"pattern": "SPAR"}, blocking=True,
    )
    result = await hass.services.async_call(
        DOMAIN, "list_import_rules", {}, blocking=True, return_response=True,
    )
    assert [r["pattern"] for r in result["rules"]] == ["HIB PETROL"]


async def test_add_import_rule_rejects_unknown_type(hass, tmp_path):
    await _setup(hass, tmp_path)

    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN, "add_import_rule",
            {"pattern": "SPAR", "type": "NotARealType"}, blocking=True,
        )


async def test_import_csv_service_resolves_user_from_context_and_writes(
    hass, tmp_path, stage_uploaded_file, hass_admin_user,
):
    await _setup(hass, tmp_path)
    await hass.services.async_call(
        DOMAIN, "add_import_rule", {"pattern": "SPAR", "type": "Groceries"}, blocking=True,
    )
    file_id = await stage_uploaded_file("statement.csv", (HEADER + ROW).encode("utf-8"))

    from homeassistant.core import Context

    result = await hass.services.async_call(
        DOMAIN, "import_csv", {"file_id": file_id, "dry_run": False},
        blocking=True, return_response=True,
        context=Context(user_id=hass_admin_user.id),
    )

    assert result["imported"] == 1
    expenses = await hass.services.async_call(
        DOMAIN, "list_expenses", {}, blocking=True, return_response=True,
    )
    assert len(expenses["expenses"]) == 1


async def test_import_csv_dry_run_true_writes_nothing(hass, tmp_path, stage_uploaded_file):
    await _setup(hass, tmp_path)
    await hass.services.async_call(
        DOMAIN, "add_import_rule", {"pattern": "SPAR", "type": "Groceries"}, blocking=True,
    )
    file_id = await stage_uploaded_file("statement.csv", (HEADER + ROW).encode("utf-8"))

    result = await hass.services.async_call(
        DOMAIN, "import_csv", {"file_id": file_id, "dry_run": True},
        blocking=True, return_response=True,
    )

    assert result["imported"] == 1
    expenses = await hass.services.async_call(
        DOMAIN, "list_expenses", {}, blocking=True, return_response=True,
    )
    assert len(expenses["expenses"]) == 0
```

`hass_admin_user` is a standard fixture provided by `pytest_homeassistant_custom_component` (verified present in this project's pinned version) — no setup needed beyond adding it as a test parameter, same as `hass`/`tmp_path`.

- [ ] **Step 2: Run them, confirm they fail**

Run: `.venv/bin/python -m pytest tests/test_services_import.py -v`
Expected: FAIL — none of the new services are registered yet.

- [ ] **Step 3: Implement the service handlers**

In `custom_components/expense_tracker/services.py`, update the imports:

```python
from .const import DOMAIN
from .db import (
    DuplicateImportRuleError,
    DuplicateTypeError,
    ImportRuleSetMismatchError,
    TypeInUseError,
    TypeSetMismatchError,
    UnknownExpenseError,
    UnknownImportRuleError,
    UnknownTypeError,
)
```

Add the new service name constants, after the existing ones:

```python
SERVICE_ADD_EXPENSE = "add_expense"
SERVICE_ADD_TYPE = "add_type"
SERVICE_REMOVE_TYPE = "remove_type"
SERVICE_REMOVE_EXPENSE = "remove_expense"
SERVICE_LIST_EXPENSES = "list_expenses"
SERVICE_LIST_TYPES = "list_types"
SERVICE_REORDER_TYPES = "reorder_types"
SERVICE_ADD_IMPORT_RULE = "add_import_rule"
SERVICE_REMOVE_IMPORT_RULE = "remove_import_rule"
SERVICE_REORDER_IMPORT_RULES = "reorder_import_rules"
SERVICE_LIST_IMPORT_RULES = "list_import_rules"
SERVICE_IMPORT_CSV = "import_csv"
```

Add the new schemas, after `REORDER_TYPES_SCHEMA`:

```python
ADD_IMPORT_RULE_SCHEMA = vol.Schema(
    {vol.Required("pattern"): cv.string, vol.Required("type"): cv.string}
)
REMOVE_IMPORT_RULE_SCHEMA = vol.Schema({vol.Required("pattern"): cv.string})
REORDER_IMPORT_RULES_SCHEMA = vol.Schema(
    {vol.Required("patterns"): vol.All([cv.string], vol.Length(min=1))}
)
IMPORT_CSV_SCHEMA = vol.Schema(
    {vol.Required("file_id"): cv.string, vol.Required("dry_run"): cv.boolean}
)
```

Add the new handlers inside `async_register_services`, after `handle_reorder_types` and before `handle_remove_expense`:

```python
    async def handle_add_import_rule(call: ServiceCall) -> None:
        try:
            await runtime.async_add_import_rule(call.data["pattern"], call.data["type"])
        except (DuplicateImportRuleError, UnknownTypeError) as err:
            raise ServiceValidationError(str(err)) from err

    async def handle_remove_import_rule(call: ServiceCall) -> None:
        try:
            await runtime.async_remove_import_rule(call.data["pattern"])
        except UnknownImportRuleError as err:
            raise ServiceValidationError(str(err)) from err

    async def handle_reorder_import_rules(call: ServiceCall) -> None:
        try:
            await runtime.async_reorder_import_rules(call.data["patterns"])
        except ImportRuleSetMismatchError as err:
            raise ServiceValidationError(str(err)) from err

    async def handle_list_import_rules(call: ServiceCall) -> dict:
        rules = await runtime.async_list_import_rules()
        return {"rules": [{"pattern": p, "type": t} for p, t in rules]}

    async def handle_import_csv(call: ServiceCall) -> dict:
        user = _resolve_user_from_context(hass, call.context.user_id)
        if user is None:
            raise ServiceValidationError(
                "Could not determine who these expenses belong to: no "
                "person is linked to your Home Assistant user account. "
                "Link your account under Settings > People and try again."
            )
        try:
            return await runtime.async_import_csv(
                call.data["file_id"], call.data["dry_run"], user
            )
        except CsvFormatError as err:
            raise ServiceValidationError(str(err)) from err
```

Add the `CsvFormatError` import alongside the other `.db` imports at the top — actually it lives in `import_engine`, not `db`, so add a separate import line:

```python
from .import_engine import CsvFormatError
```

Register the five new services, after the existing `reorder_types` registration and before `async_unregister_services`:

```python
    hass.services.async_register(
        DOMAIN, SERVICE_ADD_IMPORT_RULE, handle_add_import_rule, schema=ADD_IMPORT_RULE_SCHEMA
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_REMOVE_IMPORT_RULE,
        handle_remove_import_rule,
        schema=REMOVE_IMPORT_RULE_SCHEMA,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_REORDER_IMPORT_RULES,
        handle_reorder_import_rules,
        schema=REORDER_IMPORT_RULES_SCHEMA,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_LIST_IMPORT_RULES,
        handle_list_import_rules,
        schema=vol.Schema({}),
        supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_IMPORT_CSV,
        handle_import_csv,
        schema=IMPORT_CSV_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )
```

Add the five new service names to `async_unregister_services`'s tuple:

```python
def async_unregister_services(hass: HomeAssistant) -> None:
    for service in (
        SERVICE_ADD_EXPENSE,
        SERVICE_ADD_TYPE,
        SERVICE_REMOVE_TYPE,
        SERVICE_REMOVE_EXPENSE,
        SERVICE_LIST_EXPENSES,
        SERVICE_LIST_TYPES,
        SERVICE_REORDER_TYPES,
        SERVICE_ADD_IMPORT_RULE,
        SERVICE_REMOVE_IMPORT_RULE,
        SERVICE_REORDER_IMPORT_RULES,
        SERVICE_LIST_IMPORT_RULES,
        SERVICE_IMPORT_CSV,
    ):
        hass.services.async_remove(DOMAIN, service)
```

- [ ] **Step 4: Add the new services to `services.yaml`**

Append to `custom_components/expense_tracker/services.yaml`:

```yaml
add_import_rule:
  name: Add import rule
  description: >
    Register a rule that maps a case-insensitive substring of a bank
    transaction description to an expense type, used by import_csv.
  fields:
    pattern:
      required: true
      selector:
        text:
    type:
      required: true
      selector:
        text:

remove_import_rule:
  name: Remove import rule
  description: Remove an import rule. Does not affect past imports.
  fields:
    pattern:
      required: true
      selector:
        text:

reorder_import_rules:
  name: Reorder import rules
  description: >
    Set the match-priority order of import rules (first match wins).
    `patterns` must list every existing rule's pattern exactly once.
  fields:
    patterns:
      required: true
      example: '["SPAR", "HIB PETROL"]'
      selector:
        object:

list_import_rules:
  name: List import rules
  description: >
    Read-only: returns every import rule (pattern, target type) in match
    order.

import_csv:
  name: Import bank CSV
  description: >
    Classify every row of an uploaded bank statement CSV against the
    current import rules and either preview or commit the result.
    Structural exclusions (round-up transfers, ATM withdrawals, income)
    are applied automatically; unmatched rows are returned under
    needs_review and never guessed at.
  fields:
    file_id:
      required: true
      selector:
        file:
          accept: ".csv"
    dry_run:
      required: true
      description: >
        true previews without writing anything; false actually inserts
        the matched rows as expenses.
      selector:
        boolean:
```

- [ ] **Step 5: Run the tests, confirm they pass**

Run: `.venv/bin/python -m pytest tests/test_services_import.py tests/test_services_yaml_schema.py -v`
Expected: all PASS. (`test_services_yaml_schema.py` needs no changes — it re-validates whatever is currently in `services.yaml` against HA's real schema, so it automatically covers the new entries.)

- [ ] **Step 6: Run the full suite**

Run: `.venv/bin/python -m pytest -q`
Expected: all PASS.

- [ ] **Step 7: Commit**

```bash
git add custom_components/expense_tracker/services.py custom_components/expense_tracker/services.yaml tests/test_services_import.py
git commit -m "Register add/remove/reorder/list_import_rule and import_csv services"
```

---

## Task 6: Dashboard card — "Import" tab (rules list + CSV upload/preview/commit)

**Files:**
- Create: `custom_components/expense_tracker/www/expense-tracker-import-card.js`
- Modify: `custom_components/expense_tracker/const.py`

**Interfaces:**
- Consumes: `expense_tracker.list_import_rules`, `add_import_rule`, `remove_import_rule`, `reorder_import_rules`, `import_csv`, `add_expense` (all Task 5); the WebSocket `call_service`/`return_response` pattern and the Pointer-Events drag-to-reorder implementation already shipped in `expense-tracker-types-card.js`; the `/api/file_upload` POST pattern already shipped in `expense-tracker-add-card.js`.
- No automated test harness exists for the JS cards in this project (verified manually against the live instance instead, matching how the Types tab was built and verified) — this task's "tests" are a manual verification checklist, not pytest.

- [ ] **Step 1: Add the new file to `CARD_FILES`**

In `custom_components/expense_tracker/const.py`:

```python
CARD_FILES = [
    "expense-tracker-add-card.js",
    "expense-tracker-list-card.js",
    "expense-tracker-types-card.js",
    "expense-tracker-import-card.js",
]
```

This alone is enough for the existing `_async_register_static_files` (`__init__.py`) and `async_register_dashboard_resources` (`lovelace_setup.py`) mechanisms to serve and register the new file automatically on next restart — neither file needs any other change, they both already iterate `CARD_FILES` generically.

- [ ] **Step 2: Create the card**

Create `custom_components/expense_tracker/www/expense-tracker-import-card.js`:

```javascript
/**
 * expense-tracker-import-card
 *
 * Two sections in one card:
 *   1. Import: pick a CSV, Preview (dry_run) shows counts + a
 *      needs_review list with an inline type picker per row and a
 *      "remember this pattern" checkbox; Import actually commits.
 *   2. Rules: the current merchant-pattern -> type table, drag-to-reorder
 *      (verbatim copy of expense-tracker-types-card.js's Pointer Events
 *      implementation -- window-level pointermove/pointerup, NOT
 *      setPointerCapture, since capture is silently released by
 *      insertBefore's implicit remove-then-reinsert -- see that card's
 *      file header for the full incident this was fixed from), add-rule
 *      form, delete button per row.
 *
 * CSV upload reuses the exact /api/file_upload POST + Authorization
 * bearer-token pattern already verified working end-to-end in
 * expense-tracker-add-card.js's receipt upload.
 */
class ExpenseTrackerImportCard extends HTMLElement {
  setConfig(config) {
    this._config = config || {};
  }

  set hass(hass) {
    const first = !this._hass;
    this._hass = hass;
    if (first) {
      this._render();
      this._fetchRules();
    }
  }

  getCardSize() {
    return 10;
  }

  // ---- rules data ----

  async _fetchRules() {
    try {
      const result = await this._hass.connection.sendMessagePromise({
        type: "call_service",
        domain: "expense_tracker",
        service: "list_import_rules",
        service_data: {},
        return_response: true,
      });
      this._rules = (result.response && result.response.rules) || [];
      this._renderRuleRows();
    } catch (err) {
      this._el.ruleRows.innerHTML = `<div class="empty">Couldn't load rules: ${
        (err && err.message) || err
      }</div>`;
    }
  }

  async _fetchTypes() {
    const state = this._hass.states["sensor.expense_tracker_total"];
    return (state && state.attributes && state.attributes.types) || [];
  }

  // ---- render ----

  _render() {
    const root = this.attachShadow({ mode: "open" });
    root.innerHTML = `
      <style>
        ha-card { padding: 8px 0 16px; }
        .section-title {
          padding: 16px 16px 8px;
          font-size: 13px;
          font-weight: 500;
          color: var(--secondary-text-color);
          text-transform: uppercase;
          letter-spacing: 0.05em;
        }
        .import-section {
          display: flex;
          flex-direction: column;
          gap: 12px;
          padding: 0 16px 16px;
          border-bottom: 1px solid var(--divider-color);
        }
        .file-row { display: flex; align-items: center; gap: 12px; }
        .file-btn {
          display: flex;
          align-items: center;
          gap: 8px;
          padding: 10px 14px;
          font-size: 14px;
          border: 1px solid var(--divider-color);
          border-radius: 8px;
          background: var(--card-background-color);
          color: var(--primary-text-color);
          cursor: pointer;
        }
        .btn {
          padding: 10px 16px;
          font-size: 14px;
          font-weight: 500;
          border: none;
          border-radius: 8px;
          cursor: pointer;
          color: var(--text-primary-color, #fff);
          background: var(--primary-color);
        }
        .btn:disabled { opacity: 0.5; cursor: default; }
        .btn.secondary {
          background: none;
          color: var(--primary-color);
          border: 1px solid var(--primary-color);
        }
        .summary {
          display: grid;
          grid-template-columns: repeat(2, 1fr);
          gap: 6px 16px;
          font-size: 13px;
          color: var(--secondary-text-color);
        }
        .summary b { color: var(--primary-text-color); }
        .review-row {
          display: flex;
          align-items: center;
          gap: 10px;
          padding: 8px 0;
          border-top: 1px solid var(--divider-color);
          flex-wrap: wrap;
        }
        .review-desc { flex: 1; min-width: 180px; font-size: 13px; }
        .review-amount { font-size: 13px; font-variant-numeric: tabular-nums; }
        .review-type-select {
          font-size: 13px;
          padding: 6px 8px;
          border-radius: 6px;
          border: 1px solid var(--divider-color);
          background: var(--card-background-color);
          color: var(--primary-text-color);
        }
        .review-remember { display: flex; align-items: center; gap: 4px; font-size: 12px; }
        .review-save { flex-shrink: 0; }
        .status { padding: 4px 16px 0; min-height: 18px; font-size: 13px; text-align: center; }
        .status.error { color: var(--error-color, #db4437); }
        .status.ok { color: var(--success-color, #43a047); }

        .rows { display: flex; flex-direction: column; }
        .row {
          display: flex;
          align-items: center;
          gap: 12px;
          padding: 10px 16px;
          border-bottom: 1px solid var(--divider-color);
        }
        .row:last-child { border-bottom: none; }
        .row.dragging { opacity: 0.6; background: var(--secondary-background-color); }
        .row ha-icon.grip {
          color: var(--secondary-text-color);
          flex-shrink: 0;
          cursor: grab;
          touch-action: none;
        }
        .row .pattern { flex: 1; font-size: 14px; color: var(--primary-text-color); }
        .row .type { font-size: 12px; color: var(--secondary-text-color); }
        .delete {
          flex-shrink: 0;
          color: var(--secondary-text-color);
          background: none;
          border: none;
          cursor: pointer;
          display: flex;
        }
        .delete:hover { color: var(--error-color, #db4437); }
        .empty { padding: 20px 16px; text-align: center; color: var(--secondary-text-color); font-size: 14px; }

        .add-rule-section {
          display: flex;
          align-items: center;
          gap: 12px;
          padding: 16px;
        }
        .add-rule-section input[type="text"] {
          flex: 1;
          box-sizing: border-box;
          padding: 10px;
          font-size: 14px;
          border: 1px solid var(--divider-color);
          border-radius: 8px;
          background: var(--card-background-color);
          color: var(--primary-text-color);
        }
        ha-icon-picker { width: 200px; }
        .add-btn {
          flex-shrink: 0;
          display: flex;
          align-items: center;
          justify-content: center;
          width: 44px;
          height: 44px;
          color: var(--text-primary-color, #fff);
          background: var(--primary-color);
          border: none;
          border-radius: 50%;
          cursor: pointer;
        }
      </style>
      <ha-card header="${this._config.title || "Import expenses"}">
        <div class="section-title">Import</div>
        <div class="import-section">
          <div class="file-row">
            <label class="file-btn">
              <ha-icon icon="mdi:file-upload-outline"></ha-icon>
              <span id="fileLabel">Choose CSV</span>
              <input type="file" id="csvFile" accept=".csv" hidden>
            </label>
            <button class="btn secondary" id="previewBtn" disabled>Preview</button>
            <button class="btn" id="commitBtn" disabled>Import</button>
          </div>
          <div id="summary"></div>
          <div id="reviewList"></div>
          <div class="status" id="importStatus"></div>
        </div>

        <div class="section-title">Rules</div>
        <div class="rows" id="ruleRows"><div class="empty">Loading…</div></div>
        <div class="add-rule-section">
          <input type="text" id="newPattern" placeholder="Pattern, e.g. SPAR">
          <select id="newRuleType" class="review-type-select"></select>
          <button class="add-btn" id="addRuleBtn" title="Add rule">
            <ha-icon icon="mdi:plus"></ha-icon>
          </button>
        </div>
        <div class="status" id="ruleStatus"></div>
      </ha-card>
    `;

    this._el = {
      csvFile: root.getElementById("csvFile"),
      fileLabel: root.getElementById("fileLabel"),
      previewBtn: root.getElementById("previewBtn"),
      commitBtn: root.getElementById("commitBtn"),
      summary: root.getElementById("summary"),
      reviewList: root.getElementById("reviewList"),
      importStatus: root.getElementById("importStatus"),
      ruleRows: root.getElementById("ruleRows"),
      newPattern: root.getElementById("newPattern"),
      addRuleBtn: root.getElementById("addRuleBtn"),
      ruleStatus: root.getElementById("ruleStatus"),
    };

    // The add-rule form needs to pick a type by NAME (not by icon), so
    // this builds a plain <select> from the current type list rather
    // than reusing ha-icon-picker (that's the right tool for the Types
    // tab's icon field, not for picking an existing type here).
    this._populateRuleTypeOptions();

    this._el.csvFile.addEventListener("change", () => this._onFileChosen());
    this._el.previewBtn.addEventListener("click", () => this._runImport(true));
    this._el.commitBtn.addEventListener("click", () => this._runImport(false));
    this._el.addRuleBtn.addEventListener("click", () => this._addRule());
  }

  async _populateRuleTypeOptions() {
    const types = await this._fetchTypes();
    const select = this._el.newRuleType;
    select.innerHTML = "";
    for (const [name] of types) {
      const opt = document.createElement("option");
      opt.value = name;
      opt.textContent = name;
      select.appendChild(opt);
    }
  }

  // ---- rules list ----

  _renderRuleRows() {
    if (!this._rules || this._rules.length === 0) {
      this._el.ruleRows.innerHTML = `<div class="empty">No rules yet.</div>`;
      return;
    }
    this._el.ruleRows.innerHTML = "";
    for (const r of this._rules) {
      const row = document.createElement("div");
      row.className = "row";
      row.dataset.pattern = r.pattern;
      row.innerHTML = `
        <ha-icon class="grip" icon="mdi:drag"></ha-icon>
        <span class="pattern">${this._escape(r.pattern)}</span>
        <span class="type">${this._escape(r.type)}</span>
        <button class="delete" title="Delete">
          <ha-icon icon="mdi:delete-outline"></ha-icon>
        </button>
      `;
      row.querySelector(".delete").addEventListener("click", () => this._deleteRule(r.pattern));
      this._wireDrag(row.querySelector(".grip"), row);
      this._el.ruleRows.appendChild(row);
    }
  }

  // Verbatim copy of expense-tracker-types-card.js's _wireDrag /
  // _persistOrder pair, targeting import rules instead of types. See that
  // file's header comment for why window-level listeners are used instead
  // of setPointerCapture.
  _wireDrag(grip, row) {
    grip.addEventListener("pointerdown", (e) => {
      e.preventDefault();
      row.classList.add("dragging");
      this._drag = { pointerId: e.pointerId };

      const onMove = (ev) => {
        if (!this._drag || this._drag.pointerId !== ev.pointerId) return;
        const children = Array.from(this._el.ruleRows.children);
        const currentIndex = children.indexOf(row);
        if (currentIndex === -1) return;
        for (let i = 0; i < children.length; i++) {
          if (i === currentIndex) continue;
          const rect = children[i].getBoundingClientRect();
          const midpoint = rect.top + rect.height / 2;
          const movingDown = i > currentIndex;
          if (movingDown && ev.clientY >= midpoint) {
            this._el.ruleRows.insertBefore(row, children[i].nextSibling);
            break;
          }
          if (!movingDown && ev.clientY < midpoint) {
            this._el.ruleRows.insertBefore(row, children[i]);
            break;
          }
        }
      };

      const onUp = (ev) => {
        if (!this._drag || this._drag.pointerId !== ev.pointerId) return;
        window.removeEventListener("pointermove", onMove);
        window.removeEventListener("pointerup", onUp);
        window.removeEventListener("pointercancel", onUp);
        this._drag = null;
        row.classList.remove("dragging");
        this._persistRuleOrder();
      };

      window.addEventListener("pointermove", onMove);
      window.addEventListener("pointerup", onUp);
      window.addEventListener("pointercancel", onUp);
    });
  }

  async _persistRuleOrder() {
    const patterns = Array.from(this._el.ruleRows.children)
      .map((r) => r.dataset.pattern)
      .filter(Boolean);
    if (patterns.length < 2) return;
    try {
      await this._hass.callService("expense_tracker", "reorder_import_rules", { patterns });
    } catch (err) {
      this._el.ruleStatus.textContent = (err && err.message) || "Failed to save order.";
      this._el.ruleStatus.className = "status error";
    } finally {
      await this._fetchRules();
    }
  }

  async _addRule() {
    const pattern = this._el.newPattern.value.trim();
    const type = this._el.newRuleType.value;
    if (!pattern) {
      this._el.ruleStatus.textContent = "Enter a pattern.";
      this._el.ruleStatus.className = "status error";
      return;
    }
    try {
      await this._hass.callService("expense_tracker", "add_import_rule", { pattern, type });
      this._el.newPattern.value = "";
      this._el.ruleStatus.textContent = "";
      this._el.ruleStatus.className = "status";
      await this._fetchRules();
    } catch (err) {
      this._el.ruleStatus.textContent = (err && err.message) || "Failed to add rule.";
      this._el.ruleStatus.className = "status error";
    }
  }

  async _deleteRule(pattern) {
    if (!window.confirm(`Delete rule "${pattern}"?`)) return;
    try {
      await this._hass.callService("expense_tracker", "remove_import_rule", { pattern });
      await this._fetchRules();
    } catch (err) {
      window.alert((err && err.message) || "Failed to delete rule.");
    }
  }

  _escape(text) {
    const div = document.createElement("div");
    div.textContent = text;
    return div.innerHTML;
  }

  // ---- CSV upload + preview/commit ----

  _onFileChosen() {
    const file = this._el.csvFile.files[0];
    this._el.fileLabel.textContent = file ? file.name : "Choose CSV";
    this._el.previewBtn.disabled = !file;
    this._el.commitBtn.disabled = true;
    this._el.summary.innerHTML = "";
    this._el.reviewList.innerHTML = "";
  }

  async _uploadCsv(file) {
    const formData = new FormData();
    formData.append("file", file);
    const resp = await fetch("/api/file_upload", {
      method: "POST",
      headers: { Authorization: `Bearer ${this._hass.auth.data.access_token}` },
      body: formData,
    });
    if (!resp.ok) {
      throw new Error(`CSV upload failed (HTTP ${resp.status})`);
    }
    const body = await resp.json();
    return body.file_id;
  }

  async _runImport(dryRun) {
    const file = this._el.csvFile.files[0];
    if (!file) return;
    this._el.previewBtn.disabled = true;
    this._el.commitBtn.disabled = true;
    this._el.importStatus.textContent = dryRun ? "Previewing…" : "Importing…";
    this._el.importStatus.className = "status";
    try {
      const fileId = await this._uploadCsv(file);
      const result = await this._hass.connection.sendMessagePromise({
        type: "call_service",
        domain: "expense_tracker",
        service: "import_csv",
        service_data: { file_id: fileId, dry_run: dryRun },
        return_response: true,
      });
      this._renderImportResult(result.response, dryRun);
    } catch (err) {
      this._el.importStatus.textContent = (err && err.message) || "Import failed.";
      this._el.importStatus.className = "status error";
    } finally {
      this._el.previewBtn.disabled = false;
    }
  }

  _renderImportResult(result, wasDryRun) {
    this._el.importStatus.textContent = wasDryRun ? "Preview complete." : "Import complete.";
    this._el.importStatus.className = "status ok";
    this._el.summary.innerHTML = `
      <div><b>${result.imported}</b> ${wasDryRun ? "would import" : "imported"}</div>
      <div><b>${result.skipped_duplicate}</b> duplicate</div>
      <div><b>${result.excluded_roundup}</b> round-up (excluded)</div>
      <div><b>${result.excluded_atm_withdrawal}</b> ATM withdrawal (excluded)</div>
      <div><b>${result.excluded_credit}</b> income (excluded)</div>
      <div><b>${result.needs_review.length}</b> need review</div>
    `;
    this._el.commitBtn.disabled = !wasDryRun; // only offer Import right after a Preview
    this._renderReviewList(result.needs_review);
  }

  _renderReviewList(reviewRows) {
    this._el.reviewList.innerHTML = "";
    for (const row of reviewRows) {
      const el = document.createElement("div");
      el.className = "review-row";
      el.innerHTML = `
        <span class="review-desc">${this._escape(row.description)}</span>
        <span class="review-amount">${row.amount.toFixed(2)}</span>
        <select class="review-type-select"></select>
        <label class="review-remember">
          <input type="checkbox" checked> remember
        </label>
        <button class="btn secondary review-save">Save</button>
      `;
      this._fetchTypes().then((types) => {
        const select = el.querySelector(".review-type-select");
        for (const [name] of types) {
          const opt = document.createElement("option");
          opt.value = name;
          opt.textContent = name;
          select.appendChild(opt);
        }
      });
      el.querySelector(".review-save").addEventListener("click", () =>
        this._resolveReviewRow(row, el)
      );
      this._el.reviewList.appendChild(el);
    }
  }

  async _resolveReviewRow(row, el) {
    const type = el.querySelector(".review-type-select").value;
    const remember = el.querySelector(".review-remember input").checked;
    try {
      if (remember) {
        // Derive a pattern from the description the same simple way a
        // human would pick one -- the first word is usually enough
        // (e.g. "POS TRANSACTION SPAR..." -> "SPAR" isn't derivable
        // generically, so default to letting the merchant name stand:
        // use the full description as the pattern here, and leave
        // shortening it to a manual edit in the Rules list afterward).
        await this._hass.callService("expense_tracker", "add_import_rule", {
          pattern: row.description,
          type,
        });
      }
      await this._hass.callService("expense_tracker", "add_expense", {
        amount: row.amount,
        type,
        date: row.date,
        note: row.description,
      });
      el.remove();
      if (remember) await this._fetchRules();
    } catch (err) {
      window.alert((err && err.message) || "Failed to save this row.");
    }
  }
}

customElements.define("expense-tracker-import-card", ExpenseTrackerImportCard);
```

- [ ] **Step 3: Bump the manifest version and ship**

This mirrors the release ritual already established earlier in this project (see e.g. v0.2.0 through v0.2.3's commits). In `custom_components/expense_tracker/manifest.json`, bump `"version"` to the next value after whatever is currently shipped (check the file first — do not assume a specific prior value, since other work may have landed on this repo since this plan was written).

```bash
git add custom_components/expense_tracker/const.py custom_components/expense_tracker/www/expense-tracker-import-card.js custom_components/expense_tracker/manifest.json
git commit -m "Add the Import tab's dashboard card (rules list + CSV upload/preview/commit)"
git push origin main
```

Then, using the `home-assistant` MCP tools against the live instance (same sequence used for every prior release this project):
1. `gh release create vX.Y.Z ...` (or the `gh` command directly) with a short summary.
2. `ha_manage_hacs(action="update_information", repository_id="armendkadrija/expense-tracker-home-assistant")`.
3. `ha_manage_hacs(action="download", repository_id="armendkadrija/expense-tracker-home-assistant", version="vX.Y.Z")`.
4. This release touches Python (`const.py`, plus everything from Tasks 1-5) — a full HA restart is required, not a config-entry reload (see this project's own README section on exactly why a reload isn't sufficient, and confirm with the user before restarting, same as every prior restart this project has done).

- [ ] **Step 4: Manual verification against the live instance**

After the restart:
1. Open the dashboard. Confirm a "Types" tab still works normally (regression check) and that the new Import card renders with no console errors (`read_console_messages`, pattern `error|Error`).
2. Add two or three import rules through the Rules section; confirm they appear, confirm drag-to-reorder actually moves a rule more than one position in a single drag (this is the exact bug class fixed on the Types tab — verify it didn't reappear here since this is a second, separately-written copy of that logic).
3. Build a small test CSV (a handful of rows covering: one matched-by-rule POS row, one round-up row, one ATM withdrawal + its fee, one credit/income row, one row matching nothing) using the real header from `expenses.csv`, upload it, click Preview, and confirm the summary counts and the review list match expectations by hand.
4. Resolve one review-list row (pick a type, leave "remember" checked, Save) and confirm: the row disappears from the list, a matching new rule now shows up in the Rules section, and the expense appears in the existing "All Expenses" tab.
5. Click Import on the same file and confirm the summary now shows the previously-reviewed row's pattern matching automatically (since the rule now exists) rather than needing review again.
6. Re-upload the exact same file and Import again; confirm the summary reports everything as duplicate and the All Expenses count doesn't change.

---

## Task 7: Ship the real 6-month statement and document the feature

**Files:**
- Modify: `README.md` (in `expense-tracker-home-assistant`)
- Modify: `dashboards/expense_tracker_dashboard.yaml`

**Interfaces:**
- Consumes: everything from Tasks 1-6, already shipped and restarted.

- [ ] **Step 1: Add the Import view to the dashboard YAML**

In `dashboards/expense_tracker_dashboard.yaml`, add a fifth view after the existing "Types" view, following that file's existing indentation and structure:

```yaml
  - type: sections
    title: Import
    path: import
    icon: mdi:file-upload-outline
    max_columns: 2
    sections:
      - type: grid
        cards:
          - type: custom:expense-tracker-import-card
```

- [ ] **Step 2: Update the live dashboard to match**

Using the `home-assistant` MCP tools, apply the same view addition to the live `expense-tracker` dashboard with `ha_config_set_dashboard`'s `python_transform` mode (the same approach used to add the Types view earlier in this project) — append the view above to `config["views"]`.

- [ ] **Step 3: Update the README**

In the `expense-tracker-home-assistant` repo's `README.md`, add a new section (placed after the existing "Managing expense types" section, matching that section's tone and level of detail):

```markdown
## Importing a bank statement

The "Import" tab classifies a bank-exported CSV against a rule table you
maintain (pattern -> expense type, drag-to-reorder, same interaction as
the Types tab). Structural bank noise — round-up transfers to your own
savings sub-account, ATM withdrawals (the paired ATM fee still imports
normally), and income/credit rows — is excluded automatically and isn't
configurable, since it isn't a business rule, it's a fixed fact about
this bank's export format.

Anything that matches no rule is never guessed at — it lands in a review
list with an inline type picker. Resolving a review row optionally saves
it as a permanent rule (checked by default), so each month's statement
needs less manual review than the last.

Always **Preview** before **Import** — Preview classifies and shows the
full summary without writing anything. Re-importing the same or an
overlapping file is always safe: every imported row's exact (date,
reference, description, amount) combination is fingerprinted, and a
repeat is reported as a duplicate and skipped rather than double-counted.

Currently supports one bank's CSV export format (see the column names in
`import_engine.py` if you need to adapt it to another bank).
```

- [ ] **Step 4: Run the CSV import against the real file, dry-run first**

This is the actual payoff of this whole plan — do it manually, not as an automated test, since it's a one-time operational task against real financial data:

1. Seed a handful of `import_rules` covering the highest-frequency merchants from the real profiling data gathered during brainstorming (SPAR/Albi Market/Viva Fresh/Bau Market/Tregu Group → Groceries; HIB Petrol/Shell/BP → Transport; pharmacies → Health; KESCO/KRU/eKosova/VALA → Utilities; Netflix/Steam/AllDebrid/Spotify → Entertainment; Parking → Transport; DP Partners/Home Property Management → Rent; Maintenance Charge/ACCOUNT TRANSFER CHARGES/ATM charge → Bank Fees) through the Rules UI. Any type referenced here that doesn't exist yet (Rent, Bank Fees) needs adding via the Types tab first.
2. Upload the real `expenses.csv`, click **Preview**, and sanity-check the summary against the verified counts from brainstorming (296 round-up, 24 credit, 13 ATM withdrawals excluded; the rest split between imported and needs_review).
3. Resolve the needs_review rows that are worth categorizing (the rent-adjacent personal transfers, per the spec's scope decision, may reasonably be skipped rather than resolved — that's a judgment call for whoever is doing this run, not something to automate).
4. Click **Import**.

- [ ] **Step 5: Commit the docs changes**

```bash
git add README.md dashboards/expense_tracker_dashboard.yaml
git commit -m "Document the Import tab and add it to the shipped dashboard YAML"
git push origin main
```

---

## Plan self-review notes

(Kept here for the record, not something an executor needs to act on.)

- **Spec coverage:** every spec section has a task — data model (Task 1), services (Tasks 2, 4, 5), CSV parsing/classification algorithm (Task 3), dashboard tab (Task 6), error handling (Tasks 1/5's `ServiceValidationError` wrapping, `CsvFormatError`), testing approach (a test file per task). The spec's "explicitly out of scope" items have no tasks, correctly.
- **Beyond-spec addition, called out explicitly:** `remove_type` now also guards against `import_rules` usage (Task 2, Step 4) — the spec didn't call this out, but leaving it out would let a type get deleted out from under a live rule, so it's included with its own justification inline rather than silently slipped in.
- **Beyond-spec addition #2:** `needs_review` entries carry `import_key` (not in the spec's example JSON) — without it, resolving the same review row twice (or resolving it and then re-importing the same file) would create a duplicate expense with no dedupe protection, since the manual "Save" path calls `add_expense` directly rather than going back through `import_csv`'s dedupe check. Flagged here so a reviewer can see it was a deliberate fix, not an inconsistency.

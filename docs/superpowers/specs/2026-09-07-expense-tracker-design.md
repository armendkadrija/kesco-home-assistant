# Expense Tracker — Home Assistant Custom Integration

Status: approved for implementation planning
Date: 2026-09-07

## Purpose

Track personal/household expenses inside Home Assistant: log an expense (amount,
type, who spent it, optional receipt) via a dashboard view, and get running
insight totals (overall and this-month) broken down by type and by user.

## Scope decisions (already made, don't relitigate without new information)

- **New, separate repo** — `expense-tracker-home-assistant` (sibling to
  `kesco-home-assistant`), not added to this repo. This repo's release
  workflow is hardcoded to `custom_components/kesco/manifest.json` and its
  `hacs.json` declares this repo as the single "KESCO Energy" integration;
  adding a second component here would fire spurious releases and pollute a
  public repo with unrelated data model.
- **Private / personal use only** — no `hacs.json`, no release workflow, no
  public-facing config_flow polish. Installed on the user's own instance via
  HACS "custom repository" pointing at the private repo (or a manual copy).
- **Entry UI: native only** — a `script` entity with `fields:` + `selector:`
  types, opened from a dashboard button/tile. Home Assistant auto-renders the
  fill-in dialog; no custom Lovelace card, no JS, no frontend build step. A
  fully custom card (icon-grid type picker, etc.) was explicitly deferred as
  a separate future project.
- **Storage: SQLite**, not the `Store` JSON helper. Chosen because the whole
  point of this integration is ad-hoc aggregation/insight, which SQL does
  far better than hand-rolled Python over a JSON blob, and because a plain
  `.db` file is directly usable by other tools (Grafana, Metabase, a raw
  `sqlite3` shell) if the user wants that later.
- **Event-driven, not polled** — no `DataUpdateCoordinator`/scan interval
  (unlike the `kesco` integration, which polls a cloud API). Every write
  immediately pushes new entity state.

## Data model

Two SQLite tables in `<config>/expense_tracker/expenses.db`:

```sql
CREATE TABLE expense_types (
    name  TEXT PRIMARY KEY,   -- also the display id; must be unique
    icon  TEXT NOT NULL       -- mdi icon string, e.g. "mdi:cart"
);

CREATE TABLE expenses (
    id           TEXT PRIMARY KEY,   -- uuid4
    amount       REAL NOT NULL,
    type_name    TEXT NOT NULL REFERENCES expense_types(name),
    user         TEXT NOT NULL,      -- person entity_id, e.g. "person.armend"
    receipt_path TEXT,               -- relative path under www/, nullable
    note         TEXT,               -- nullable
    timestamp    TEXT NOT NULL       -- ISO 8601 UTC
);
```

Design note: an expense type's `name` **is** its identifier — no separate
slug/uuid. Renaming a type is remove + re-add (no `edit_type` action in v1;
YAGNI until it's actually annoying in practice).

On first setup, seed `expense_types` with: Groceries (`mdi:cart`), Transport
(`mdi:car`), Utilities (`mdi:flash`), Health (`mdi:medical-bag`),
Entertainment (`mdi:movie`), Other (`mdi:dots-horizontal`). Fully editable
after.

## Migrations

Use `PRAGMA user_version` on the db file. On integration setup, read the
current version, run any migration steps between it and the target version
in order, bump the pragma. Keep migrations as plain SQL strings in a list
indexed by version — no ORM, no migration framework.

## Concurrency requirement (hard requirement, not optional)

`sqlite3` is blocking. Every DB call (read or write) MUST run via
`hass.async_add_executor_job(...)`. Never call `sqlite3` directly on the
event loop — this is the standard way a first custom integration causes it
to hang under load, and reviewers should treat a direct call as a bug, not
a style nit.

## Config entry

`config_flow.py`: single confirmation step, no fields, `single_instance_only`
(mirrors the shape of local-storage-only integrations like `local_todo` —
there is nothing to configure, so don't ask for anything).

## Services (Actions)

Domain: `expense_tracker`

- `expense_tracker.add_expense`
  - `amount` (required, number selector, `min: 0`)
  - `type` (required, string — must match an existing `expense_types.name`;
    validate and raise `ServiceValidationError` if unknown)
  - `user` (required, entity selector, `domain: person`; stored as the
    entity_id string, not a live reference — historical records shouldn't
    break if the person entity is later removed)
  - `receipt` (optional — selector TBD at implementation time, `file` vs
    `image`; verify against current HA selector docs, don't guess the API
    shape here)
  - `note` (optional, text selector)
  - Effect: insert row, save receipt file if present (see Receipts below),
    then push updated state to both sensors and to
    `select.expense_tracker_type`'s options (in case this was a new type;
    in practice types don't change here, but keep the update path uniform).

- `expense_tracker.add_type`
  - `name` (required, text selector)
  - `icon` (required, icon selector — HA's native mdi icon picker)
  - Effect: insert row (fail with `ServiceValidationError` on duplicate
    name), refresh `select.expense_tracker_type` options.

- `expense_tracker.remove_type`
  - `name` (required, string — must match an existing type)
  - Effect: delete the type row. Existing expenses keep their historical
    `type_name` value even though it no longer resolves to a live type
    (don't cascade-delete or null out past records). Refresh the `select`
    entity's options.

## Entities

- `select.expense_tracker_type` — `options` = current `expense_types.name`
  list, kept in sync on every add/remove. This is the "which type am I
  logging next" control, standalone on the dashboard.

  **Known limitation:** HA's native `select` dropdown renders option text
  only — no per-option icon. Icons are still stored and available on every
  expense record and sensor attribute for use elsewhere; they just won't
  show inside the dropdown widget itself. Accepted trade-off of going
  fully native (see custom-card discussion above) — revisit only if this
  turns out to actually bother the user in practice.

- `sensor.expense_tracker_total` — state: all-time sum of `amount`,
  `unit_of_measurement`: `hass.config.currency`, `state_class: total`.
  Attributes: `by_type` (dict, type name → sum), `by_user` (dict, entity_id
  → sum), `count` (int).

- `sensor.expense_tracker_total_this_month` — same shape, `WHERE timestamp`
  scoped to the current calendar month. Recomputed on every write and on
  HA restart; not a `total_increasing` counter, just a derived value.

No per-type sensors. If a finer cut is wanted later (e.g. "Groceries this
month" as its own graphable entity), that's a two-line template sensor off
`by_type` — not something this integration should pre-build for every type
that will ever exist.

## Receipts

Uploaded file saved to
`<config>/www/expense_tracker/receipts/<expense_id>.<ext>`, relative path
stored in `expenses.receipt_path`. Served at
`/local/expense_tracker/receipts/<expense_id>.<ext>` for display in a
dashboard picture/image card. Exact input selector (`file` vs `image`) to
be confirmed against current HA docs during implementation — flagged, not
decided, in this spec.

## Dashboard view

One Lovelace view, native cards only:

1. Entity card for `select.expense_tracker_type`.
2. A helper script `script.expense_tracker_add_expense` with `fields`:
   `amount` (number selector), `user` (person entity selector), `receipt`
   (optional), `note` (optional text). Sequence: single action calling
   `expense_tracker.add_expense` with
   `type: "{{ states('select.expense_tracker_type') }}"` — type is read
   from the select entity's current state, not asked for again in the
   form. A button/tile on the view opens this script's auto-generated
   fill-in dialog.
3. Stat/tile cards for `sensor.expense_tracker_total` and
   `sensor.expense_tracker_total_this_month`.

## Build & test approach

- Scaffold from the `integration_blueprint` cookiecutter (community
  standard starting point for non-trivial custom components) — brings
  `pytest-homeassistant-custom-component` test harness plus hassfest/lint
  CI without hand-wiring it.
- Unit/integration tests run against that harness, not the user's live HA
  instance.
- Deploy to the live instance via HACS "custom repository" pointing at the
  private repo once it works in the test harness.

## Explicitly out of scope (YAGNI, revisit only on real friction)

- Editing a type's name/icon in place (remove + re-add instead).
- Per-type sensors.
- Multi-currency support (single currency, `hass.config.currency`).
- Custom Lovelace card / any frontend build step.
- Public HACS distribution, release automation.
- Cascading delete or reassignment of expenses when a type is removed.

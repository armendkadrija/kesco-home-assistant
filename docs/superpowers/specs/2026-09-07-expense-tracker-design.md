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
    type_name    TEXT NOT NULL,      -- NOT a real FK, see note below
    user         TEXT NOT NULL,      -- person entity_id, e.g. "person.armend"
    receipt_path TEXT,               -- relative path under www/, nullable
    note         TEXT,               -- nullable
    timestamp    TEXT NOT NULL       -- ISO 8601 UTC, backdatable (see add_expense)
);
```

Design note: an expense type's `name` **is** its identifier — no separate
slug/uuid. Renaming a type is remove + re-add (no `edit_type` action in v1;
YAGNI until it's actually annoying in practice).

`type_name` is deliberately **not** a real `REFERENCES expense_types(name)`
foreign key. It can't be: `remove_type` is specified to leave existing
expenses pointing at the now-deleted name (no cascade, no nulling out
history) — an enforced FK would make that delete fail with a constraint
violation instead. Existence is validated in application code, only at
insert time (`add_expense` rejects an unknown `type`), which is exactly
where it needs to hold.

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
    validate and raise `ServiceValidationError` if unknown). This is a
    plain string on the service itself — the *live, dropdown* experience
    for humans is provided by the wrapper script below, not by this
    selector.
  - `user` (required, entity selector, `domain: person`; stored as the
    entity_id string, not a live reference — historical records shouldn't
    break if the person entity is later removed)
  - `date` (optional, date selector; defaults to now if omitted) — logging
    an expense a day or two after the fact is normal, not an edge case.
  - `receipt` (optional — selector TBD at implementation time, `file` vs
    `image`; verify against current HA selector docs, don't guess the API
    shape here)
  - `note` (optional, text selector)
  - Effect: insert row, save receipt file if present (see Receipts below),
    then push updated state to both sensors.

- `expense_tracker.add_type`
  - `name` (required, text selector)
  - `icon` (required, icon selector — HA's native mdi icon picker)
  - Effect: insert row (fail with `ServiceValidationError` on duplicate
    name), then update the wrapper script's `type` field options (see
    Dashboard view) via HA's script config API — not by generating YAML.

- `expense_tracker.remove_type`
  - `name` (required, string — must match an existing type)
  - Effect: delete the type row. Existing expenses keep their historical
    `type_name` value even though it no longer resolves to a live type
    (don't cascade-delete or null out past records). Update the wrapper
    script's `type` field options the same way as `add_type`.

- `expense_tracker.remove_expense`
  - `id` (required, string)
  - Effect: delete the row, and delete its receipt file from disk if one
    exists — otherwise receipt files accumulate forever with nothing
    pointing at them. Fixing a fat-fingered amount or a duplicate entry
    is common enough that a v1 without this would be a real gap, not a
    YAGNI cut.

## Entities

No standalone `select` entity for type — see "Dashboard view" below for
why that was cut and where type selection actually lives now.

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

1. A helper script `script.expense_tracker_add_expense`, owned and kept in
   sync by the integration, with `fields`: `type` (select selector, options
   = current `expense_types.name` list), `amount` (number selector), `user`
   (person entity selector), `date` (optional date selector), `receipt`
   (optional), `note` (optional text). Sequence: single action calling
   `expense_tracker.add_expense` passing all of the above straight through.
   A button/tile on the view opens this script's auto-generated fill-in
   dialog — one atomic submission, all fields captured together.
2. Stat/tile cards for `sensor.expense_tracker_total` and
   `sensor.expense_tracker_total_this_month`.

**Why `type` is a field on the script, not a separate `select` entity:** an
earlier version of this spec put type selection in its own
`select.expense_tracker_type` entity, read by the script at submit time.
That's global mutable state doubling as an implicit form parameter — two
people submitting near-simultaneously could have the type change out from
under the first person's in-flight form, silently logging their expense
under the wrong category. Folding `type` into the same form as
amount/user/receipt makes one submission atomic; there's nothing left to
race.

**Keeping the `type` field's options live:** every `add_type`/`remove_type`
call updates the script's field options through HA's **script config API**
(the same API the Settings → Scripts UI editor uses to persist changes) —
never by generating/hand-writing `scripts.yaml`. Per this HA instance's
best-practices guidance, hand-generated YAML for scripts is a named
anti-pattern specifically because it skips config validation and requires
manual reloads; the config API validates and updates the live script in
place. This also means zero manual `configuration.yaml` setup for the
user — the script is created and maintained entirely by the integration.

**Known limitation:** HA's native `select` selector renders option text
only, no per-option icon — so the type field in the fill-in form will show
names, not icons, even though every type has one. Icons are still stored
and available on every expense record and sensor attribute for use
elsewhere. Accepted trade-off of staying fully native (see the custom-card
discussion above) — revisit only if this turns out to actually bother the
user in practice.

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

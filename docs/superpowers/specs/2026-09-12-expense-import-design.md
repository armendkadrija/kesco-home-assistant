# Expense Tracker — Bank CSV Import

Status: approved for implementation planning
Date: 2026-09-12

## Purpose

Turn a bank-exported CSV of transaction descriptions into `expenses` rows
automatically, instead of hand-entering months of history one line at a
time. Classification rules (which merchant maps to which expense type) must
be editable from the dashboard, with zero Home Assistant restarts, since
the rule table will keep growing every time a new merchant shows up in a
statement.

## Scope decisions (already made, don't relitigate without new information)

- **Rules live in the DB, not a file.** An earlier draft of this put the
  ruleset in an external script + YAML file, reasoning that "rules change
  every month, and this project's `.py` changes need a HA restart to take
  effect." That reasoning conflated two different things: restart pain
  applies to *code* changes, not *data* changes. A merchant→type mapping is
  data. It gets its own table (`import_rules`), edited the same
  restart-free way `expense_types` already is — through services and a
  dashboard tab, not a file on disk.
- **One bank's CSV format**, not a generic importer. Column names, date
  format (`DD.MM.YYYY`), delimiter (`,` with `"`-quoting), and the specific
  exclusion patterns below (round-up sub-account, ATM withdrawal lines) are
  all verified against the user's real 6-month, 764-row export. Building
  multi-bank format detection now would be speculative generality — YAGNI
  until a second bank/format is actually in play.
- **Income (credit rows) is out of scope.** 24 of 764 rows in the sample
  are credits (salary, dividends, a refund). `expenses` has no income
  concept and this feature doesn't add one — credit rows are always
  excluded from import, full stop.
- **ATM withdrawals are excluded; the paired ATM fee is not.** A withdrawal
  just moves money to cash with no visibility into what it was later spent
  on — importing it as a lump "Cash withdrawal" expense was considered and
  rejected (user decision). The 0.25 fee line the bank generates alongside
  every withdrawal is a real, itemizable expense and is imported normally
  like any other transaction.
- **Unmatched rows are never guessed.** A row that matches no rule and
  isn't structurally excluded goes into a `needs_review` list returned by
  the preview/import call. The UI surfaces it for a one-time manual type
  pick (with an option to save that pick as a new rule). Nothing gets
  auto-categorized by fuzzy/best-effort matching.
- **Structural exclusions are hardcoded, not rules.** Round-up rows, ATM
  withdrawal lines, and credit rows are facts about this bank's export
  format, not business rules that grow over time — they're plain Python
  conditionals in the parser, not `import_rules` table entries. Only the
  merchant→type mapping, which genuinely grows every month, gets the
  DB-table-plus-UI treatment.

## Verified facts about the source data

(All verified by parsing the user's actual `expenses.csv`, not assumed.)

- Header row (Albanian/English bilingual):
  `"Nr. / No.","Data e transaksionit / Value Date","Perfituesi / Beneficiary(Sender)","Pershkrimi / Description","Referenca / Reference","Data e procesimit / Processing Date","Kreditim / Credit","Debitim / Debit"`
- Encoding has a UTF-8 BOM (`utf-8-sig` when reading with Python's `csv`
  module). Delimiter `,`, values `"`-quoted. Date format `DD.MM.YYYY`.
- Amount is two columns (`Credit`, `Debit`), not one signed column. Exactly
  one of the two is non-zero per row in the full sample (764/764).
- Round-up transfers are identifiable by `Description` containing the
  literal string `ROUND UP PAYMENT` — sufficient on its own, verified
  against all 296 round-up rows in the sample with zero misses. (The
  `Beneficiary` column also happens to carry a fixed internal sub-account
  code on these rows, and cross-checking each round-up row's
  `Parent Reference :FTLxxxxx` against the `Reference` column of the
  purchase that triggered it confirms these are 100% derived/internal,
  never independent spending — but that's corroborating evidence from
  analysis, not a second exclusion path the code needs: matching the one
  substring is enough.)
- `Reference` is **not** a unique key on its own (731 distinct values across
  764 rows — e.g. an ATM withdrawal and its paired fee share one
  reference). The tuple `(Value Date, Reference, Description, Debit,
  Credit)` **is** unique across all 764 rows — verified, zero collisions.
  This is the dedupe key.
- ATM rows: `Description` starts with `ATM TRANSACTION`. The fee line's
  description contains the literal substring `ATM charge`; the withdrawal
  line does not. That substring is the only reliable way to tell them
  apart — amount alone isn't (a withdrawal could coincidentally be a round
  number that isn't obviously distinguishable from a fee by magnitude
  alone in every locale, so don't rely on "small amount = fee").
- Merchant names for `POS TRANSACTION` rows repeat the merchant name twice
  concatenated (bank's own formatting: `"<name> <name> Original time: ..."`),
  and the *same* merchant appears with inconsistent capitalization/branch
  suffixes across statements (`"Albi Market 43 PRISHTINA QZ"` /
  `"Albi Market 43 PRISHTINE QZ"` / `"ALBI MARKET 43 Prishtine QZ"` — one
  real shop, three spellings). Rule matching must be case-insensitive
  substring matching against a normalized description, not exact string
  equality, or the rule table would need 3x the entries and still miss the
  next spelling variant.

## Data model changes

New table, `<config>/expense_tracker/expenses.db`:

```sql
CREATE TABLE import_rules (
    pattern     TEXT PRIMARY KEY,   -- case-insensitive substring to match
    type_name   TEXT NOT NULL,      -- target expense_types.name (not FK, same reasoning as expenses.type_name)
    sort_order  INTEGER NOT NULL    -- match priority, first match wins; same convention as expense_types.sort_order
);
```

`expenses` gains one nullable column:

```sql
ALTER TABLE expenses ADD COLUMN import_key TEXT;
CREATE INDEX idx_expenses_import_key ON expenses(import_key);
```

`import_key` is `NULL` for every expense added through the existing
add-expense card (manual entry never sets it). It's populated only by the
import path, holding the SHA-256 hex digest of the verified dedupe tuple
above. No `UNIQUE` constraint at the SQLite level — SQLite can't add one to
an existing table without a full table rebuild, and the same guarantee is
enforced in application code the same way `add_type`'s duplicate-name check
already is: a `SELECT` before the `INSERT`, inside the same executor-job
call, no separate migration-time table rebuild needed.

Migration: `SCHEMA_VERSION` bumps by 2 (one step for the column, one for
the index — consistent with this project's existing one-statement-per-step
convention, see `db.py`'s `MIGRATIONS` list). No backfill needed —
`import_key IS NULL` is the correct value for every pre-existing row.

## Services (Actions)

All in the existing `expense_tracker` domain, following the exact pattern
`add_type`/`remove_type`/`reorder_types` already established.

- `expense_tracker.add_import_rule`
  - `pattern` (required, text) — rejected with `ServiceValidationError` on
    duplicate (same shape as `add_type`'s duplicate-name check).
  - `type` (required, text) — must match an existing `expense_types.name`,
    same validation `add_expense` already does.
  - Effect: insert at the end of sort order (mirrors `add_type` appending
    new types to the end).

- `expense_tracker.remove_import_rule`
  - `pattern` (required, text).
  - No in-use guard needed (unlike `remove_type`) — a rule isn't referenced
    by any historical record, removing it only affects future imports.

- `expense_tracker.reorder_import_rules`
  - `patterns` (required, list of strings, must be every existing pattern
    exactly once) — identical contract and identical
    `TypeSetMismatchError`-style validation to `reorder_types`.

- `expense_tracker.list_import_rules` — `SupportsResponse.ONLY`, returns
  `{"rules": [{"pattern": ..., "type": ..., }]}` in sort order. Same shape
  as `list_types`.

- `expense_tracker.import_csv`
  - `file_id` (required, text) — id returned by `/api/file_upload`, the
    exact mechanism the add-expense card's receipt field already uses.
  - `dry_run` (required, boolean) — no default; the calling card is always
    explicit about which mode it's in, on purpose (a silent default here is
    exactly the kind of thing that turns a preview click into a real
    import by accident).
  - `SupportsResponse.ONLY`. Returns:
    ```json
    {
      "imported": 123,
      "skipped_duplicate": 4,
      "excluded_roundup": 296,
      "excluded_atm_withdrawal": 13,
      "excluded_credit": 24,
      "needs_review": [
        {"date": "2026-08-30", "amount": 12.49, "description": "..."}
      ]
    }
    ```
    `dry_run: true` computes everything and writes nothing. `dry_run:
    false` actually calls the DB insert path (skipping duplicates,
    same as a resolved dry run would report) and returns the same
    shape reflecting what was actually written.
  - Rows in `needs_review` are never inserted, matched or not — the
    dashboard's follow-up flow is a second `add_import_rule` call (to make
    the pattern permanent) followed by a normal `add_expense` call for that
    one row, both already-existing services doing the work; `import_csv`
    itself stays a pure classify-and-insert pass, no partial/manual-pick
    branch inside it.

## CSV parsing & classification algorithm

Runs inside `import_csv`'s handler, off the event loop
(`hass.async_add_executor_job`) since it's file I/O + a loop over
potentially thousands of rows — same hard rule this project has held
everywhere else (`db.py`'s docstring, `lovelace_setup.py`'s
`_read_integration_version` fix earlier this project).

For each row:

1. Parse `Value Date` (`DD.MM.YYYY`) → reuse `dt_helpers.py`'s existing
   local-date-to-UTC-ISO normalization, the same path `add_expense`'s
   `date` field already goes through — no second date-handling
   implementation.
2. If `Credit` (parsed as float) `> 0` → `excluded_credit`, stop.
3. If `Description` contains `ROUND UP PAYMENT` → `excluded_roundup`, stop.
4. If `Description` starts with `ATM TRANSACTION` and does **not** contain
   `ATM charge` → `excluded_atm_withdrawal`, stop. (The fee line falls
   through to normal matching below — it's a real expense.)
5. Compute `import_key` = SHA-256 of
   `(Value Date, Reference, Description, Debit, Credit)`. If an expense
   with this `import_key` already exists → `skipped_duplicate`, stop.
6. Normalize `Description` (uppercase, collapse whitespace) and check it
   against `import_rules` in `sort_order`, case-insensitive substring
   match, first hit wins → call the existing `add_expense` path
   internally with `type=<matched rule's type>`, `amount=Debit`,
   `note=<original Description, truncated>`, `import_key=<computed key>`,
   `user` left to auto-resolve the same way `add_expense` already does
   for a normal card submission.
7. No rule matched → append to `needs_review`, insert nothing.

## Dashboard tab

New view, `custom:expense-tracker-import-card`, added the same way the
Types tab was: a new file in `CARD_FILES`, auto-served and
auto-registered as a Lovelace resource by the existing mechanism, no new
manual setup step.

Two sections on one card, reusing existing visual/interaction patterns
rather than inventing new ones:

1. **Import**: a file picker (same upload-button pattern as the
   add-expense card's receipt field, POSTing to `/api/file_upload`), a
   "Preview" button (calls `import_csv` with `dry_run: true`) showing the
   count summary and the `needs_review` list, each review row with an
   inline type picker (same dropdown-with-icons component the add-expense
   card already has) and a "remember this pattern" checkbox. An "Import"
   button (visible once a preview has run) calls `import_csv` with
   `dry_run: false`.
2. **Rules**: a list of existing `import_rules`, visually identical to the
   Types tab's row list — grip handle, pattern text, target type with
   icon, delete button — with the exact same Pointer-Events-based
   drag-to-reorder implementation already fixed and verified there (see
   the `window`-level-listener fix; this is a second consumer of that
   exact pattern, not a new one to get right). An add-rule form (pattern
   text input + type picker) at the bottom, same shape as the Types tab's
   add-type form.

## Error handling

- Malformed/unparseable CSV (wrong header, undecodable file) →
  `ServiceValidationError` with the parse failure message, same as any
  other bad-input rejection in this integration. No partial import from a
  bad file — fail before processing row 1.
- A `needs_review` row's user-picked type must exist in `expense_types` —
  enforced by reusing `add_expense`'s existing validation when the
  follow-up call is made; `import_csv` never needs its own copy of that
  check since it never inserts a `needs_review` row directly.
- `add_import_rule` referencing a type that doesn't exist →
  `ServiceValidationError`, same validation `add_expense`/`add_type`
  already apply elsewhere for type references.

## Testing approach

Same harness as the rest of the project
(`pytest-homeassistant-custom-component`, no live-instance testing).
Required coverage, mirroring the rigor already applied to `reorder_types`:

- `import_rules` CRUD + reorder, including the same duplicate/mismatch
  rejection tests `expense_types` already has.
- Dedupe-key derivation: verified unique on the real sample structure;
  test with synthetic rows that share a `Reference` but differ in
  `Description`/amount (the ATM-pair shape) to prove they don't collide.
- Re-running `import_csv` on the same file twice → second run reports
  everything as `skipped_duplicate`, zero new rows.
- Structural exclusion tests: one row of each excluded kind (credit,
  round-up, ATM withdrawal) plus one ATM fee row that must NOT be
  excluded, in the same fixture file, asserting exact counts.
- `needs_review` for a row matching no rule; matched once a rule covering
  it is added and the same file is re-previewed.
- Blocking-call regression: the CSV-processing path (file parsing, DB
  reads/writes) must run under `hass.async_add_executor_job` — this is the
  exact bug class just hit live in `lovelace_setup.py` (HA's blocking-call
  detector catching a direct `.open()` on the event loop). Confirm the test
  harness's blocking-call detection is enabled for this test (it's active
  by default in `pytest-homeassistant-custom-component` for I/O calls made
  directly on the event loop) so a regression here fails CI, not just a
  future live restart.

## Explicitly out of scope (YAGNI, revisit only on real friction)

- Multi-bank / generic CSV format support.
- Income/credit tracking of any kind.
- Automatic recurring import (e.g. watching a folder, a scheduled fetch
  from the bank). This spec covers manual upload-and-classify only; a
  scheduled pull would need the bank to expose a programmatic export
  (Open Banking API or similar), which hasn't been confirmed to exist.
- Editing an `import_rules` pattern in place (remove + re-add, same
  YAGNI call already made for `expense_types`).
- Fuzzy/best-effort matching for `needs_review` rows (e.g. suggesting a
  likely type). Every review row gets an explicit human decision.

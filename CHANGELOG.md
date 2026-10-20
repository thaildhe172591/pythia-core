# Changelog

## 0.3.0.dev0 — unreleased

Phase 2 of the plan in DESIGN.md: the write gate, pythia's Do movement.

- **`apply` runs the six steps for engines whose DDL runs in a
  transaction.** Snapshot to the journal, impact, rehearse (run, verify,
  roll back), preview, commit, report. The preview diffs the definition
  before and after as the database prints both, shows what the rehearsal
  broke, the row set, the warnings, and a token.
- **A write commits only as approved.** `--confirm <token>` needs the
  developer's grant, which is checked before anything runs. The confirm run
  rehearses again and commits only if the token, the row count and the
  verification still match the approved preview; otherwise it rolls back
  and names what moved. A breakage the developer saw and approved lands,
  and exits 3.
- **`approve` is the developer's, at a real console.** It prints the card
  (object, rehearsal, rows, warnings) from the preview's journal entry and
  mints a single-use, 15-minute grant bound to the connection. `--card`
  prints the card and mints nothing. The chat doors arrive in Phase 3.
- **`policy`:** pythia's groups and defaults, under
  `.pythia/<engine>/policy.json`. Loosening needs a person at a console.
  `allow` is refused for every group but `session`.
- **`journal list | show | restore`, `history`:** every preview and write
  on disk. A restore is a write: previewed, approved, confirmed.
- **The write tier is settled:** `classify`, `prepare_statement`,
  `parse_object`, `destructive_reason`, `dml_probe`, `measure_row_set`,
  `snapshot`, `rehearse`, `restore_statement`, `privilege_warning`.
- **SECURITY.md** says what each gate stops and what it does not.
- **`tests/test_gate.py`** attacks every invariant through a fake engine
  with a real transaction. Writing it found a hole before it shipped: a
  confirm run compared its verification with its own journal entry rather
  than the approved preview, so a verification that moved would have
  passed. The run now excludes its own entry.

## 0.2.0.dev0 — unreleased

Phase 1 of the plan in DESIGN.md: the read commands, pythia's Learn movement.

- **The read tier is settled:** `object_names`, `source`, `columns`, `ddl`,
  `grep`, `dependencies`, `dependents`, `compile_errors`. `compile_errors`
  moved here from the write draft, and `object_source` (the snapshot) moved
  to the write draft.
- **Read commands every engine inherits:** `ls`, `src`, `cols`, `ddl`,
  `grep`, `deps`, `impact`, `errors`. Each needs the whole connect and read
  tiers and is refused by name before connecting otherwise.
- **Output never cuts short silently.** `--limit`, `--max-lines` and
  `--offset` window every list and document. Any truncation says how to get
  the rest, and list queries ask for one row beyond the window so
  truncation is detected, not guessed.
- **`impact` counts and warns.** It counts distinct objects and those only a
  text match vouches for (`[text]`). It lists dependents in other schemas.
  At 10 or more dependents, or any in another schema, it says to show the
  list to the developer before writing code. Every blind spot the engine
  names is printed, including when nothing depends on the object.
- **`errors` says "NOT VERIFIED" plainly** when the engine's checker could
  not cover everything, and `--json` carries `verified` and `blind_spot`.

## 0.1.0.dev0 — unreleased

Phase 0 of the plan in DESIGN.md.

- **The engine contract.** `pythia_core.engine` declares what core asks of a
  database, in three tiers: connect, read and write. `missing` and
  `refuse_unless` refuse a command by tier before anything connects.
  `engine_problems` is the conformance check every engine package runs.
- **Per-engine connections.** `.pythia/<engine>/connections.json`, found by
  walking up from the working directory or named by `<PREFIX>_CONFIG`.
  Resolution follows pythia's order: `--conn`, `<PREFIX>_CONNECTION`, a single
  entry, the path segment under the project root, then `"default"`.
  Ambiguity is an error. pythia for Oracle's `.pythia/connections.json` is
  never read.
- **The query contract lint.** `contract_problems` checks files against
  declared binds and the `-- Purpose / -- Binds / -- Returns` header. It knows
  `named` and `pyformat` binds, and flags a lone `%` that psycopg would read
  as a placeholder.
- **Commands every engine inherits:** `version`, `connections` (never a
  secret) and `check`. `check` opens a read-only session and reports when the
  current schema is not the configured one.

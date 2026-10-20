# Changelog

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

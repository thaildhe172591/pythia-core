# The pythia family — design

Status: **Phase 1** (October 2026). This document is the plan; the code in
this repository is its first two steps. Where the two disagree, the code is what
runs and this document is wrong — fix it in the same change.

## 1. What is being built, and what is not

[pythia](https://github.com/thaildhe172591/pythia) proved one idea on Oracle:
an AI agent developing database code is safe and useful when it **Learns**
from the live catalog instead of a dump, **Asks** a person at the moments
only a person can decide, and **Does** writes only through one gate. That
gate snapshots before writing, refuses what it cannot classify, needs a
person's approval, verifies after, and says "written but broken" (exit 3)
instead of "done".

The family takes the same idea to PostgreSQL, SQL Server and MongoDB.

**pythia for Oracle is not touched.** It stays in its own repository, under
its own name (`pythia-plsql` on PyPI, command `pythia`), self-contained, with
no dependency on anything here. It is the reference implementation: when this
document says "as pythia does", it means that code, and it is ported by hand.
The rule runs both ways: no package here imports pythia, and pythia never
learns that these packages exist.

| Repository | PyPI name | Module | Command | Driver (its only dependency besides core) | State folder |
|---|---|---|---|---|---|
| pythia | `pythia-plsql` | `pythia` | `pythia` | python-oracledb | `.pythia/` (unchanged) |
| pythia-core | `pythia-core` | `pythia_core` | — | none: stdlib only | — |
| pythia-postgres | `pythia-postgres` | `pythia_postgres` | `pythia-pg` | psycopg 3 (`psycopg[binary]`) | `.pythia/postgres/` |
| pythia-mssql | `pythia-mssql` | `pythia_mssql` | `pythia-mssql` | decided in Phase 4 (§8) | `.pythia/mssql/` |
| pythia-mongo | `pythia-mongo` | `pythia_mongo` | `pythia-mongo` | pymongo | `.pythia/mongo/` |

All repositories are private while pre-alpha. None is on PyPI yet; install
from git (each README says how).

## 2. Living next to pythia for Oracle

A project can use Oracle and PostgreSQL at the same time, so every name the
family adds must not collide with one pythia already uses:

- **State folder per engine.** pythia reads `.pythia/connections.json` and
  writes `.pythia/journal/`, `grants/`, `policy.json`, `autopilot/`. A family
  engine keeps everything under `.pythia/<engine>/`. If it shared the Oracle
  files, pythia would try to open an Oracle session to a PostgreSQL entry,
  and `pythia history` would list another tool's writes. A test in
  pythia-core fails if an engine ever reads the Oracle file.
- **Command per engine.** `pythia` is taken. Each engine package installs
  its own command (`pythia-pg`, ...), so installing several never makes one
  overwrite another's console script.
- **Environment per engine.** `PYTHIA_CONNECTION` belongs to Oracle. An
  engine reads `<PREFIX>_CONNECTION` and `<PREFIX>_CONFIG`
  (`PYTHIA_PG_CONNECTION`, ...), so a variable set for one tool never picks
  a connection in another. pythia's `PYTHIA_USER/PASSWORD/DSN` shortcut is
  not carried over yet: credential fields differ per engine.
- **Skills per engine** (Phase 3). Skill names are global in an agent, and pythia
  ships `pythia-apply`, `using-pythia` and the rest. A family engine's skills
  are prefixed: `pythia-pg-apply`, `using-pythia-pg`.
- **Hooks per engine** (Phase 3). pythia's PreToolUse guard denies raw writes through
  `sqlplus`/`sql`/SQLcl MCP. Each engine adds a guard for its own clients
  (`psql`, `sqlcmd`, `mongosh`). Guards only deny, so two of them in one
  agent compose safely.
- **Gitignore per engine** (Phase 3). pythia's install ignores `.pythia/connections.json`
  only. That line does not cover `.pythia/postgres/connections.json`, so each
  engine's install writes its own ignore line, and its doctor checks it.

## 3. Core and engine: where the line is

The rule: **if the answer changes with the database, it belongs to the
engine.** Everything else is core.

| Core (pythia-core) | Engine (pythia-<engine>) |
|---|---|
| connection resolution, the state folder, secret-free listing | driver, connect, read-only session, connect-error hints |
| output contract: JSON envelope, truncation markers, exit codes 0/1/3 | identifier folding (Oracle upper, PostgreSQL lower, SQL Server per collation, MongoDB none) |
| the query contract lint (`queries/`, headers, declared binds) | the queries themselves, and their bind style |
| policy groups, approval tokens, grants, the approval doors | statement classification into those groups |
| the journal, the six apply steps, restore as a write | snapshot (what "the current source" is), restore rendering |
| deploy batching, autopilot leases | script splitting (`/` + q-quotes, `$$`, `GO`, one document per file) |
| hook framework, MCP approver, install, doctor framework | the raw clients the guard denies, doctor's database checks |
| lint framework (severities, conventions file) | lint rules |
| skill lint, routing-table checks | the skills' content |

Phases 0 and 1 brought the first two rows of core, the query lint, and the
read commands with their output windowing. §7 says when the rest moves.

## 4. The engine contract

An engine is a class that declares its identity and implements the methods
of each **tier** it supports. `pythia_core.engine` holds the authoritative
list; this section explains it.

**Identity:** `name` (folder name), `label`, `command`, `env_prefix`,
`version`, `bind_style`.

**Connect tier**, needed by every command that opens a session:
`driver_errors`, `connect`, `session(handle, readonly)`, `close`,
`connect_failure`, `schema_of`, `target_of`, `server_version`,
`current_schema`.

**Read tier** (settled in Phase 1): `object_names`, `source`, `columns`,
`ddl`, `grep`, `dependencies`, `dependents`, `compile_errors`. Names arrive as
the person typed them; folding them is the engine's business. `dependencies`
and `dependents` return rows `(level, schema, name, type, how)` in walk order
plus a list of blind spots; `how` is `text` when only a name match in source
vouches for a row. `compile_errors` returns its rows and one blind spot,
which is `None` only when the check covered everything. Core turns these
into the commands `ls`, `src`, `cols`, `ddl`, `grep`, `deps`, `impact` and
`errors`. It windows every list (`--limit`, `--max-lines`, `--offset`) and
marks any truncation. It counts what `impact` found, and says when the list
must go to the developer before code is written: 10 or more dependents, or
one in another schema (pythia's rule 2).

**Write tier** (Phase 2, draft): statement reading — `classify`,
`prepare_statement`, `parse_object`, `name_conflicts`, `reconcile_statement`,
`create_prefix`, `destructive_reason`, `dml_probe`, `lint`; catalog questions
around the write — `occupants`, `object_source` (the snapshot),
`privilege_warning`, `measure_row_set`, `invalid_objects`, `before_write`;
verifying reuses the read tier's `compile_errors`. This list comes from
pythia's own `run_apply`: each method is one place where that function asks
Oracle something. Phase 2 settles it against PostgreSQL, which is the second
implementation, and two implementations are the least that can show what
the abstraction really is.

Three rules hold for every tier:

1. **Refuse by tier, before connecting.** A command whose tier the engine
   does not fully implement exits 1 and names the missing methods
   (`refuse_unless`). A command never runs halfway.
2. **Uncertainty is typed, never silent.** When an engine cannot see part of
   an answer, the answer says so. PostgreSQL cannot see a PL/pgSQL body's
   references in `pg_depend`. SQL Server's dependencies skip dynamic SQL.
   MongoDB cannot see application code at all. An impact report with a
   blind spot that does not name it is a false all-clear, and that is the
   worst failure this family can have.
3. **The database is the second line of defence, where it has one.** A
   read-only command asks the database to refuse writes (Oracle `SET
   TRANSACTION READ ONLY`, PostgreSQL `default_transaction_read_only`).
   Where the engine has no such switch, the docs and `doctor` say so plainly
   and point at a read-only account instead (§5).

`pythia_core.engine.engine_problems(engine, tiers)` is the conformance
check. Every engine package asserts it in its own suite. From Phase 2,
pythia-core also ships the adversarial write-gate scenarios (pythia's
`test_phase3`/`test_phase6` ported to a fake connection), and every engine
runs them against its own engine.

## 5. Each concept, per database

| Concept | Oracle (pythia) | PostgreSQL | SQL Server | MongoDB |
|---|---|---|---|---|
| Code that lives in the database | packages, procedures, functions, triggers, types, views | functions, procedures (11+), triggers, views, materialized views | procedures, functions, triggers, views | none worth managing: server-side JavaScript is deprecated. What is managed is validators, indexes and views (pipelines) |
| Current source (the snapshot) | `ALL_SOURCE` | `pg_get_functiondef`, `pg_get_viewdef`, `pg_get_triggerdef` | `sys.sql_modules` / `OBJECT_DEFINITION` (NULL `WITH ENCRYPTION`: refuse) | `listCollections` options (validator, `viewOn`, `pipeline`), `listIndexes` |
| Replace in place | `CREATE OR REPLACE` | `CREATE OR REPLACE`. A changed signature creates a second overload and leaves the old one; a changed return type needs a DROP | `CREATE OR ALTER` (2016 SP1+) | `collMod` for validators and views. On an index `collMod` changes only TTL and `hidden`; anything else is drop and create |
| Who depends on it | `ALL_DEPENDENCIES` (+ PL/Scope) | `pg_depend` / `pg_rewrite` for views and SQL-standard bodies (`BEGIN ATOMIC`, 14+). PL/pgSQL bodies are not tracked: text search plus `plpgsql_check`, and the gap is named | `sys.sql_expression_dependencies`; dynamic SQL and deferred names are blind spots | view pipelines (`viewOn`, `$lookup`, `$unionWith`); application code is invisible, and the report says so |
| "Broken" after a write | `INVALID` status, `ALL_ERRORS` | no status. Syntax is checked at CREATE; semantics only by `plpgsql_check` when installed, or at run time | no status. `sp_refreshsqlmodule` re-binds a module, run inside the write's transaction | after a validator change: count documents the new validator rejects |
| DDL inside a transaction | no (implicit commit) | yes, except a few (`CREATE INDEX CONCURRENTLY`, `VACUUM`, `CREATE DATABASE`, ...) | yes, except a few (`ALTER DATABASE`, full-text, ...) | no for `collMod` and indexes |
| Read-only session | `SET TRANSACTION READ ONLY` | `default_transaction_read_only` (psycopg `read_only`). `dblink`/FDW reach past it | none for an ordinary database: least-privilege login (`db_datareader`) is the line | none: a user holding only the `read` role |
| Row-set check before DML | count + `ORA_HASH` fingerprint | count + order-independent aggregate of `md5(row::text)` | count + `CHECKSUM_AGG(BINARY_CHECKSUM(*))` | `countDocuments` + fingerprint of matched `_id`s |
| Plan without running | `EXPLAIN PLAN` + `DBMS_XPLAN` | `EXPLAIN` without `ANALYZE` (`ANALYZE` executes: refused) | `SET SHOWPLAN_XML ON` | `explain("queryPlanner")` (`executionStats` executes: refused) |
| Script splitting | `;` and `/`, q-quotes, SQL*Plus directives refused | `;`, dollar quotes `$tag$`, `E''` strings; psql meta-commands (`\i`, `\gexec`) refused | `GO` separator (a client construct, not T-SQL); `CREATE PROCEDURE` must open its batch | one command document per file (Extended JSON). JavaScript is refused, as pythia refuses anonymous PL/SQL blocks |
| Comments on objects | `COMMENT ON` | `COMMENT ON` (`obj_description`, `col_description`) | extended property `MS_Description` | none: a `$jsonSchema` `description` |
| Raw clients the guard denies | `sqlplus`, `sql`, SQLcl MCP `run-sql` | `psql` (and `pg_restore` into a database) | `sqlcmd`, `Invoke-Sqlcmd`, `osql`, `bcp` in | `mongosh`, `mongo`, `mongoimport`, `mongorestore` |
| Identifier case | unquoted folds to upper | unquoted folds to lower | per collation (usually case-insensitive) | case-sensitive |
| "Schema" | user / schema | schema (inside a database); the default is `public` | schema (`dbo` by default) | database |

### What PostgreSQL makes stronger than Oracle

DDL runs inside a transaction. The PostgreSQL write gate can therefore offer
a **verified preview**: run the statement, ask the verify questions
(dependent views still compile, `plpgsql_check` clean, row count as
approved), then `ROLLBACK`. The person approves a result that was observed,
not one that was predicted. On confirm, the same statement runs again and
commits only if the verification matches the preview. Statements that refuse
a transaction block get no such preview, and the preview says so.

### What PostgreSQL makes weaker, and the gate must cover

- **Overloads.** `CREATE OR REPLACE FUNCTION f(int)` when `f(text)` exists
  creates a second function and leaves the first one live. The gate reads
  the signature, lists the existing overloads, and asks.
- **Late binding.** No `INVALID` status exists to compare before and after.
  Without `plpgsql_check`, "nothing broke" cannot be shown, and the report
  says "not verified", never "clean".
- **`search_path`.** An unqualified name resolves by `search_path`, not by
  the configured schema. `check` already reports a mismatch. Phase 2 makes
  it a refusal, as pythia's `refuse_schema_mismatch` does.
- **Checking depends on who checks.** Phase 1 found two ways `errors` could
  report errors that are not there. `plpgsql_check` resolves names by the
  checking session's `search_path`, so the engine puts the configured schema
  first for that transaction. And an account without `USAGE` on the schema
  has it left off the path silently, so every name looks missing. Then the
  engine reports nothing and says why. A false list of errors is a guess
  presented as a finding.

## 6. The standing rules, per engine

pythia's thirteen standing rules (its AGENTS.md) carry over unchanged in
meaning. Only their nouns change: `pythia-pg apply` instead of `pythia
apply`, `.pythia/postgres/connections.json` instead of
`.pythia/connections.json`, `psql` instead of `sqlplus`. Each engine's
AGENTS.md states them in its own words, and its skills carry the workflows.
MongoDB rewrites two rules: "impact before change" has no catalog graph to
lean on, and "lint before apply" means validator and index rules, not SQL.

## 7. Roadmap

Each phase ends with every suite green on Ubuntu, Windows and macOS on
Python 3.9 and 3.13. From Phase 1 on, it also ends with a recorded run
against a real database (a disposable container, never a shared one).

| Phase | Core | Engine | Done when |
|---|---|---|---|
| 0 · done | engine contract, tiers, conformance check, per-engine config, query lint, `version` / `connections` / `check` | pythia-postgres: identity, connect tier, two queries, `pythia-pg check` | both repositories' suites green; `pythia-pg check` against a real PostgreSQL |
| **1 · read (now)** | output windowing and truncation, read commands, impact summary and the rule-2 review line | PostgreSQL read tier: `ls`, `src`, `cols`, `ddl`, `grep`, `deps`, `impact` (blind spots named), `errors` via `plpgsql_check` when present; CI job against `postgres` in a service container | an agent can do pythia's Learn phase on PostgreSQL: every read command passes against a fixture on a real server, `ddl` rebuilds what it describes, `errors` is checked with and without `plpgsql_check` |
| 2 · write | policy, tokens, grants, approval doors, journal, the six steps, restore, adversarial test kit | PostgreSQL write tier, verified preview, overload and `search_path` refusals | the ported adversarial suite passes for PostgreSQL |
| 3 · harness | hooks, install, MCP approver, doctor framework, leases, deploy | `psql` guard, `pythia-pg-*` skills, doctor checks | an agent in Claude Code and Codex is held to the gate the way pythia's is |
| 4 · SQL Server | — | pythia-mssql: all tiers, `GO` splitting, `sp_refreshsqlmodule` verify | as Phase 1–3, for SQL Server |
| 5 · MongoDB | groups that fit documents (validator, index, view, data) | pythia-mongo: command-document classification, validator verify by counting rejected documents | as Phase 1–3, for MongoDB |

Not planned: moving pythia for Oracle onto pythia-core. That would be the
owner's decision, taken after Phase 3, when there is a core worth moving to.

## 8. Open questions

Each is asked when its phase starts, not guessed now:

1. **SQL Server driver.** pyodbc (mature, needs Microsoft's ODBC driver
   installed), pymssql (wheels bundle FreeTDS), python-tds (pure Python,
   less active) or Microsoft's newer `mssql-python`. Settle with a spike at
   Phase 4 against Windows, macOS and Linux CI.
2. **Minimum versions.** PostgreSQL 13 or 14 (14 brings `BEGIN ATOMIC`
   dependency tracking and `CREATE OR REPLACE TRIGGER`). SQL Server 2016 SP1
   or 2019. MongoDB 6 or 7.
3. **`plpgsql_check`.** Optional and detected (the current plan), or
   required for any write the gate calls "verified"?
4. **Publishing.** When the repositories go public, and whether `pythia-core`
   and the engine names are free on PyPI at that point.
5. **Vietnamese documentation.** pythia ships README.vi.md and GUIDE.vi.md.
   The family should too, from its first release.

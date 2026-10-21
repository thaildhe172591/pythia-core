# The pythia family — design

Status: **0.1.1** (October 2026). This document is the
plan; the code in this repository is every phase of it so far.
pythia-postgres, pythia-mssql, pythia-mongo and pythia-mysql run on all of
it, and run side by side in one project.
Where the two disagree, the
code is what runs and this document is wrong — fix it in the same change.

## 1. What is being built, and what is not

[pythia](https://github.com/thaildhe172591/pythia) proved one idea on Oracle:
an AI agent developing database code is safe and useful when it **Learns**
from the live catalog instead of a dump, **Asks** a person at the moments
only a person can decide, and **Does** writes only through one gate. That
gate snapshots before writing, refuses what it cannot classify, needs a
person's approval, verifies after, and says "written but broken" (exit 3)
instead of "done".

The family takes the same idea to PostgreSQL, SQL Server, MongoDB and
MySQL.

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
| pythia-mssql | `pythia-mssql` | `pythia_mssql` | `pythia-mssql` | pymssql (settled in Phase 4, §8) | `.pythia/mssql/` |
| pythia-mongo | `pythia-mongo` | `pythia_mongo` | `pythia-mongo` | pymongo | `.pythia/mongo/` |
| pythia-mysql | `pythia-mysql` | `pythia_mysql` | `pythia-mysql` | PyMySQL (settled in Phase 9, §8) | `.pythia/mysql/` |

pythia-core and the first three engines are on PyPI since 0.1.0 (§8,
question 4); the second release (Phase 10) brings pythia-mysql there with
its 0.1.0, which asks for pythia-core 0.1.1.

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
- **Skills per engine.** Skill names are global in an agent, and pythia
  ships `pythia-apply`, `using-pythia` and the rest. A family engine's skills
  are prefixed: `pythia-pg-apply`, `using-pythia-pg`.
- **Hooks per engine.** pythia's PreToolUse guard denies raw writes through
  `sqlplus`/`sql`/SQLcl MCP. Each engine adds a guard for its own clients
  (`psql`, `sqlcmd`, `mongosh`), spelled `<command> hook pre-tool`. A guard
  denies, allows or stays out; Claude Code lets any deny win, so two in one
  agent compose safely: neither can wave through what the other denies.
  Each allows only its own commands, and a guard that finds an MCP tool by
  its SQL-sounding name — which pythia-pg's and pythia-mssql's both do —
  denies a write through it without claiming the tool: it cannot tell
  which database the tool reaches.
- **Answers per engine.** Every engine's AskUserQuestion answer hook runs on
  every question. A hook answers only its own engine's card: a question that
  names another pythia tool's `approve` (another engine's card, pythia's
  `pythia approve`) is left to that tool, so the agent never hears "no
  pending preview carries that token" from the wrong engine beside the
  right one's "approved". pythia for Oracle is not built on this core: its
  own hook still answers a family engine's card that way, and its doctor
  warns that the family's hooks are launchers it does not run. Both are
  noise, not a way around a gate, and pythia for Oracle stays as it is.
- **Settings per engine.** Each install replaces its own hooks where they
  stood in `.claude/settings.local.json` (appending them the first time),
  so installing or re-installing engines in any order changes nothing that
  is not its own. Phase 6 holds all of this in `tests/test_family.py` (two
  fake engines and pythia for Oracle's block and hooks) and in CI's `family`
  job (`tests/family_check.py`, the four real engines installed after
  pythia for Oracle).
- **Instructions per engine.** Each engine's install writes its own marked
  block into CLAUDE.md and AGENTS.md (`<!-- pythia-pg:begin ... -->`); pythia's
  `<!-- pythia:begin ... -->` block is a different marker and is left alone.
- **Gitignore per engine.** pythia's install ignores `.pythia/connections.json`
  only. That line does not cover `.pythia/postgres/connections.json`, so each
  engine's install writes its own ignore lines, and its doctor checks them.

## 3. Core and engine: where the line is

The rule: **if the answer changes with the database, it belongs to the
engine.** Everything else is core.

| Core (pythia-core) | Engine (pythia-<engine>) |
|---|---|
| connection resolution, the state folder, secret-free listing | driver, connect, read-only session, connect-error hints |
| output contract: JSON envelope, truncation markers, exit codes 0/1/3 | identifier folding (Oracle upper, PostgreSQL lower, SQL Server per collation, MongoDB none, MySQL as `lower_case_table_names` says) |
| the query contract lint (`queries/`, headers, declared binds) | the queries themselves, and their bind style |
| policy groups, approval tokens, grants, the approval doors | statement classification into those groups |
| the journal, the six apply steps, restore as a write | snapshot (what "the current source" is), restore rendering |
| deploy batching, autopilot leases | script splitting (`/` + q-quotes, `$$`, `GO`, one document per file, `DELIMITER`) |
| hook framework, MCP approver, install, doctor framework | the raw clients the guard denies, doctor's database checks |
| lint framework (severities, conventions file) | lint rules |
| skill lint, routing-table checks | the skills' content |

Phases 0–2 brought connections, the output contract, the query lint, the
policy, tokens and grants (with the console door only), the journal and the
six steps. Phase 3a brought the harness: the guard, the session brief,
install, doctor and the skill lint. Phase 3b brought the chat doors,
Phase 3c the release (`deploy`), Phase 3d autopilot leases and Phase 3e the
lint framework (§7).

## 4. The engine contract

An engine is a class that declares its identity and implements the methods
of each **tier** it supports. `pythia_core.engine` holds the authoritative
list; this section explains it.

**Identity:** `name` (folder name), `label`, `command`, `env_prefix`,
`version`, `bind_style`.

**Connect tier**, needed by every command that opens a session:
`driver_errors`, `connect`, `session(handle, readonly)`, `close`,
`connect_failure`, `schema_of`, `target_of`, `server_version`,
`current_schema`. One optional method, in no tier (Phase 8):
`floor_warning(conn)` — None, or why the server is older than any version
the engine is tested on. `check` prints it as a `!` line (and as
`below_floor` under `--json`); nothing is refused, since an older server
often still works and the developer decides.

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

**Write tier** (settled in Phase 2, for engines whose DDL runs in a
transaction): `classify`, `prepare_statement`, `parse_object`,
`destructive_reason`, `dml_probe`, `measure_row_set`, `snapshot`,
`rehearse`, `restore_statement`, `privilege_warning`. The Phase 1 draft
took its list from pythia's `run_apply`, which asks Oracle a question at
every step because Oracle cannot take a write back. PostgreSQL showed that
most of those questions collapse into one: **rehearse** the statement inside
a transaction and report what happened. That covers which overload it
replaced, what it looks like after, how many rows it touched, and what
`plpgsql_check` says broke compared with just before. Then roll back.
`name_conflicts`, `reconcile_statement`, `create_prefix`, `occupants`,
`invalid_objects` and `before_write` are gone. `lint` moves to Phase 3 with
the lint framework. MongoDB cannot rehearse (§5); it will need a second
write path, and Phase 5 designs it against this one.

Core owns the transaction and the six steps (`gate.py`):

1. **Snapshot** — everything the statement could replace, written to the
   journal before anything runs.
2. **Impact** — the read tier's `dependents`, with its blind spots.
3. **Rehearse** — run, verify, roll back. A statement that fails here has
   nothing to approve. A rehearsal that **escaped** — the engine saw its
   transaction end inside the statement, as a SQL Server trigger's `COMMIT`
   ends it — is never "nothing was written": core rolls back what is left,
   journals the entry as `escaped`, spends the approval, pauses a lease,
   prints the restore and exits 3. In a release it names every statement
   up to that one, since one transaction held them all.
4. **Preview** — the diff between the definition before and after (both as
   the database prints them, so formatting is no noise), the rehearsal, the
   rows, the warnings, and a token over the file, the object as it stood
   and the row set.
5. **Commit** — only on `--confirm <token>` with a person's grant, or
   `--yes` at a real console. The confirm run is a second rehearsal, and it
   commits only if it still matches what was approved: the same token (so
   the same object state and the same rows), the same row count, and
   nothing broken that the approved preview did not show. Otherwise it rolls
   back and says what moved.
6. **Report** — exit 0 clean, exit 3 written but broken (a breakage the
   person saw and approved still lands, and is still reported), with the
   restore command. A restore is a write like any other: previewed,
   approved, confirmed.

The policy groups are `source`, `comments`, `data_dml`, `structural`,
`grants` and `session`, with pythia's defaults. `allow` is accepted only for
`session`: a write that needs no approval is a write no person saw.

**Predict tier** (settled in Phase 5b): `execute`, `verify_written`. Some
statements cannot be rehearsed: MongoDB runs a validator, index or view
change outside any transaction. For those, `rehearse` returns
`predicted: True` — nothing ran, and its findings are a prediction from the
data as it stands (the documents a new validator would reject, the
duplicates a unique index would meet, a view's first documents). The six
steps keep their order and their checks; only two change:

- **Preview** says "predicted — nothing ran", on the card too, and that the
  prediction is checked again after the write.
- **Commit** runs the statement once, through `execute`, after the token,
  the grant and the moved-prediction check. A database that refused it
  whole wrote nothing, and the approval stays unspent; one that cannot say
  whether it landed is reported `WRITTEN?` (exit 3). Then `verify_written`
  reads what landed: anything broken, and every way it differs from the
  approved prediction, is reported, and exits 3 with the restore.

A predicted statement has no place in a release: all or nothing needs one
transaction, so `deploy` refuses it before anything is written. An engine
that predicts without the tier is refused before its preview.

**Harness tier** (settled in Phase 3a): `raw_client_pattern`,
`write_sql_pattern`, `script_pattern`, `connections_template`,
`skills_dir`. Core turns them into four commands that touch no database
(`harness.py`, `install.py`, `skills.py`). The chat doors need them too:
an engine without this tier has no `install`, so no hook reads a chat
answer and no MCP approver is registered, and its previews name only the
developer's terminal.

- `hook pre-tool`, the PreToolUse guard. It **denies** the engine's raw
  client (named anywhere in the command) given write SQL or a script (its
  flags read with quoted SQL emptied — and read again inside a quoted
  string that itself starts the client, as `bash -c '...'` hands a shell a
  command), an MCP SQL tool given write SQL (its input read both for the
  strings it holds and as the JSON document it is), a
  grant written by hand or minted from a one-liner, the agent setting
  `<PREFIX>_CI`, reading `connections.json`, and editing the policy or the
  journal by hand. It **allows** without a prompt one plain command of the
  engine that cannot write (a read, a preview, `approve --card`) and a
  confirm whose grant is valid on that connection. It **stays out** of
  everything else, so the client's own prompt decides: `approve`, `--yes`,
  `policy set`, and anything a shell could turn into two commands. Commands
  are classified by the CLI's own parser, so the guard and the command can
  never read one argv two ways. It fails silent: a crash is no decision.

  Two optional engine methods, in no tier (Phase 5c), serve a database
  whose MCP servers do not take SQL: `mcp_tool_pattern()` says which MCP
  tools are its database's (`mongo` for MongoDB's servers; without it, a
  tool is a database's when its name says `sql`, `query`, `exec`,
  `statement`, `database` or `db`), and
  `mcp_write_tool_pattern()` which of those write by their name alone
  (`insert-many`, `drop-collection`). An engine's own MCP approver is never
  denied.
- `hook session-start`, the brief: the rules in Learn/Ask/Do form, how this
  machine spells the command, and the project's state (connections,
  policy).
- `install` scaffolds `connections.json` (mode 600, never rewritten), the
  `.gitignore` lines, the hooks and deny rules in
  `.claude/settings.local.json` (spelled with this interpreter's absolute
  path, so a hook never fails to start for want of PATH), the marked
  instruction block in CLAUDE.md and AGENTS.md, and the engine's skills in
  `.claude/skills` and `.agents/skills`. A second run changes nothing.
- `doctor` checks each of those and exits 1 on a fail; `skill lint` holds
  SKILL.md files to pythia's skill contract.

**The approval doors** (Phase 3b, `doors.py`). A grant is a person's
Approve on pythia's own card for one token, and every door ends in
`gate.grant_approval`, so none grants more than another. The agent tries
them in order, and moves to the next without asking when one fails:

1. **AskUserQuestion** (Claude Code). The question is the output of
   `approve --card <token>`, verbatim; the options are Approve and Reject.
   Claude Code then runs `approve --hook` (PostToolUse, matcher
   `AskUserQuestion`). It mints only when the question carried the card for
   that token and the developer chose Approve. It refuses, naming why: the
   agent's own words, an answer the agent wrote into its call (read from the
   session transcript, since current Claude Code moves the person's answer
   into the tool input), a dialog that closed with nobody at the keyboard, a
   typed reply, and no answer at all. The guard and a deny rule keep an
   agent from piping a payload into `approve --hook` itself.
2. **The MCP approver** (`mcp`, server `<command with _>`, tool
   `<server>_approve`). An elicitation shows the card; only an explicit
   accept of Approve mints. A decline back in under 1.5 s is the client
   answering for nobody, and says so. `install` registers it only in this
   machine's files: Claude Code's local scope for the project
   (`~/.claude.json`) and Codex's `~/.codex/config.toml`.
3. **The terminal:** `approve <token>` at a real console.

`doctor` warns about the settings that answer AskUserQuestion before the
developer sees it: `dontAsk` mode, an allow rule naming it, a PreToolUse hook
with an empty or `*` matcher.

**Release tier** (settled in Phase 3c): `split_script(text) -> [(line,
statement)]`. Core turns it into `deploy <files>` (`release.py`): a release
through the gate as **one transaction**, which pythia on Oracle cannot do
(there, each DDL statement commits on its own, so a release is a sequence of
approved single writes).

1. **Refused before anything runs:** a statement the engine cannot
   classify, a session setting (it would change how every later statement
   runs), a group the policy denies.
2. **Rehearsed as one.** Every statement in order, in one transaction: its
   snapshot written to the journal before it runs, its rows measured and its
   verification taken at its own point — so an UPDATE of a column the
   release adds is rehearsed against that column. Then everything rolls
   back. A statement that fails stops the preview; nothing is kept.
3. **One card, one approval.** The card lists every statement in order (in
   full for the groups with no snapshot, with the rows for DML) and what the
   rehearsal of the whole found. Its 10-digit token goes through the same
   doors as a statement's; a member's own token is refused there, because a
   release is approved as a whole.
4. **The confirm is all or nothing.** Before anything runs: the grant, the
   same files, every statement byte-identical to the approved preview. Then
   the release runs again in one transaction and commits only if every
   member matches what was approved — its token (object and rows), its row
   count, nothing newly broken. Anything else, or any statement failing,
   rolls the whole release back with the approval unspent.
5. **Report:** exit 0, or 3 with what broke; the restores, newest first, for
   the statements that have one.

**Autopilot leases** (Phase 3d, `lease.py`): the developer delegating a
session once instead of approving every write. `autopilot request` writes a
card — the connection, the groups (default `source` and `comments`), how
long, how many writes, whether destructive statements are included — with
an 8-digit token. Nothing is delegated until the developer approves that
card through any door, or runs `autopilot on` at a console. While the lease
is active, `apply` and `deploy` land what it covers in one run, still
snapshotted, rehearsed, verified and journaled. It never covers another
connection, a connection marked protected (read fresh on every write), a
group the card did not name, a destructive statement the card did not
include, or an UPDATE or DELETE of more than 1000 rows. A lease that names a
group lifts that group's `deny` while it is active, as its card says. It
ends when it expires, is spent, is revoked (`autopilot off`, anyone's) or
pauses itself: a write that breaks something, or five failures in a row.
`autopilot report` lists every write under it, with the restores. A lease
file that is missing, corrupt, unapproved or past its time is no lease.

**Lint tier** (settled in Phase 3e): `lint_rules() -> [rule]`, each rule
`{id, severity, summary, check(statement, group, run) -> [(line, message)]}`.
Core (`lint.py`) runs them over a change — `lint <files>` lints the files as
one run, so a rule sees across them — and adds a `naming` rule from the
team's patterns. `.pythia/<engine>/lint.json` sets severities (`error`,
`warn`, `info`, `off`) and naming patterns per object type; an unknown key,
rule, severity or pattern is refused, as the policy is. Findings appear in
every apply and deploy preview, a statement's errors on its card, and
autopilot never lands a lint error. A rule that crashes is reported as a
warning and never blocks. The brief points at the team's
`.pythia/<engine>/conventions.md` when it exists.

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
check. Every engine package asserts it in its own suite. The gate's
invariants are proven twice. pythia-core's `tests/test_gate.py` drives the
gate through a fake engine with a real transaction, and each test tries to
get one write past one invariant. Each engine then proves its own write tier
end to end against a real server: pythia-postgres's `tests/live_check.py`
does this for PostgreSQL.

## 5. Each concept, per database

| Concept | Oracle (pythia) | PostgreSQL | SQL Server | MongoDB | MySQL |
|---|---|---|---|---|---|
| Code that lives in the database | packages, procedures, functions, triggers, types, views | functions, procedures (11+), triggers, views, materialized views | procedures, functions, triggers, views | none worth managing: server-side JavaScript is deprecated. What is managed is validators, indexes and views (pipelines) | procedures, functions, triggers, events, views |
| Current source (the snapshot) | `ALL_SOURCE` | `pg_get_functiondef`, `pg_get_viewdef`, `pg_get_triggerdef` | `sys.sql_modules` / `OBJECT_DEFINITION` (NULL `WITH ENCRYPTION`: refuse) | `listCollections` options (validator, `viewOn`, `pipeline`), `listIndexes` | information_schema (`ROUTINES`, `VIEWS`, `TRIGGERS`, `EVENTS`): the body as written, the CREATE header rebuilt (a view's SELECT only as MySQL rewrote it); `SHOW CREATE` is the reference. A body is NULL to an account without `SHOW_ROUTINE` that did not define it |
| Replace in place | `CREATE OR REPLACE` | `CREATE OR REPLACE`. A changed signature creates a second overload and leaves the old one; a changed return type needs a DROP | `CREATE OR ALTER` (2016 SP1+) | `collMod` for validators and views. On an index `collMod` changes only TTL and `hidden`; anything else is drop and create | `CREATE OR REPLACE VIEW` only. For a procedure, function, trigger or event pythia-mysql replaces: a routine is created under a scratch name first, so MySQL's verdict costs nothing; then the old one is dropped, the new one created, and the old one put back from the snapshot if MySQL refuses it (`ALTER PROCEDURE` changes characteristics, never the body) |
| Who depends on it | `ALL_DEPENDENCIES` (+ PL/Scope) | `pg_depend` / `pg_rewrite` for views and SQL-standard bodies (`BEGIN ATOMIC`, 14+). PL/pgSQL bodies are not tracked: text search plus `plpgsql_check`, and the gap is named | `sys.sql_expression_dependencies`; dynamic SQL and deferred names are blind spots | views only: `viewOn` and every collection a pipeline reads (`$lookup`, `$graphLookup`, `$unionWith`, `$facet`, nested pipelines). Application code, other databases, `$merge`/`$out` jobs and change streams are invisible, and every report says so | `VIEW_TABLE_USAGE` and `VIEW_ROUTINE_USAGE` (views), triggers, foreign keys. Procedure, function, trigger and event bodies are recorded nowhere: text search in every schema, and the gap is named. What the account may not see (no `SHOW_ROUTINE`, `TRIGGER`, `EVENT`) is read from `SHOW GRANTS` and named |
| "Broken" after a write | `INVALID` status, `ALL_ERRORS` | no status. Syntax is checked at CREATE; semantics only by `plpgsql_check` when installed, or at run time | no status. `sys.dm_sql_referenced_entities` binds a module now and raises what is missing (207, or 2020 with the missing names in `sys.sql_expression_dependencies`) without ending the open transaction. `sp_refreshsqlmodule` is not usable inside a rehearsal: when it fails it rolls back the caller's whole transaction | no status. `errors` counts the documents each validator rejects as it stands (with `$nor`), and runs each view over its first documents; after a validator change, the same count, before and after | a view is marked invalid when what it reads goes (information_schema shows error 1356). A routine, trigger or event resolves names only when it runs: never verified, and said |
| DDL inside a transaction | no (implicit commit) | yes, except a few (`CREATE INDEX CONCURRENTLY`, `VACUUM`, `CREATE DATABASE`, ...) | yes, except a few (`ALTER DATABASE`, full-text, ...) | no for `collMod` and indexes | no: every DDL statement commits implicitly, pending DML included. DDL is predicted, written once, then read back; data is rehearsed in a transaction, first in a savepoint, and MySQL's warning 1196 on rolling that back (a trigger writing MyISAM) is `escaped` |
| A trigger can end the caller's transaction | no: a trigger cannot commit (an autonomous-transaction trigger commits only its own work) | no: a trigger cannot `COMMIT` | yes: a trigger's `COMMIT` or `ROLLBACK` ends it (error 3609). pythia-mssql rehearses one transaction level down, so a single `COMMIT` commits nothing; a transaction that ended anyway is `escaped` (exit 3) unless the database's triggers show it was rolled back | not applicable | no: a trigger cannot `COMMIT` |
| Read-only session | `SET TRANSACTION READ ONLY` | `default_transaction_read_only` (psycopg `read_only`). `dblink`/FDW reach past it | none (`SET TRANSACTION READ ONLY` is a syntax error): a least-privilege login (`db_datareader`, `VIEW DEFINITION`) is the line, and a read command's transaction is never committed | none: a user holding only the `read` role is the line, and every command a read sends is a file the suite holds to reads only | `SET SESSION TRANSACTION READ ONLY`: MySQL refuses DML and DDL alike (1792), the implicit transaction after a commit included |
| Row-set check before DML | count + `ORA_HASH` fingerprint | count + order-independent aggregate of `md5(row::text)` | count + `CHECKSUM_AGG(BINARY_CHECKSUM(*))` | `countDocuments` + fingerprint of matched `_id`s | count + an order-free sum of `md5(json_array(...))` over every column; the connection asks for the rows an UPDATE matched (`CLIENT_FOUND_ROWS`) |
| Plan without running | `EXPLAIN PLAN` + `DBMS_XPLAN` | `EXPLAIN` without `ANALYZE` (`ANALYZE` executes: refused) | `SET SHOWPLAN_XML ON` | `explain("queryPlanner")` (`executionStats` executes: refused) | `EXPLAIN` (`EXPLAIN ANALYZE` executes: refused) |
| Script splitting | `;` and `/`, q-quotes, SQL*Plus directives refused | `;`, dollar quotes `$tag$`, `E''` strings; psql meta-commands (`\i`, `\gexec`) refused | `GO` separator (a client construct, not T-SQL); `CREATE PROCEDURE` must open its batch | one command document per file (Extended JSON). JavaScript is refused, as pythia refuses anonymous PL/SQL blocks | `;`, and `DELIMITER` (a client construct) around bodies that hold `;`, as the mysql client and Flyway read them; a stored program takes its whole file. A release holds data only: MySQL commits DDL as it runs it |
| Comments on objects | `COMMENT ON` | `COMMENT ON` (`obj_description`, `col_description`) | extended property `MS_Description` | none: a `$jsonSchema` `description` | `COMMENT` clauses on tables, columns, indexes, routines and events; none on views |
| Raw clients the guard denies | `sqlplus`, `sql`, SQLcl MCP `run-sql` | `psql` (and `pg_restore` into a database) | `sqlcmd`, `osql`, `isql`, `mssql-cli`, `Invoke-Sqlcmd`; `bcp ... in`; `sqlpackage` Publish and Import. A procedure first in a batch needs no `EXEC`, so any `sp_`/`xp_` name counts as a write | `mongosh` and `mongo` given a write method, `$out`, `$merge`, `load()` or a script; `mongoimport`, `mongorestore`, `mongofiles`; an MCP tool named for a write (`insert-many`, `drop-collection`) or given a writing command document | `mysql`, `mysqlsh` and `mariadb` given write SQL (`CALL`, `DO`, `PREPARE`/`EXECUTE`, `LOAD DATA`, `SET GLOBAL`, `INTO OUTFILE`, `source`) or a script (`< file`, a pipe, `mysqlsh -f`), MySQL Shell's `util.load*`; `mysqlimport`; `mysqladmin`'s writing subcommands |
| The tool's own statements | `queries/`, one file each | `queries/*.sql`, pyformat binds | `queries/*.sql`, pyformat binds (`%%` adapted for pymssql) | `commands/*.json`: a command document, each bind a `{"$bind": ...}` value | `queries/*.sql`, pyformat binds (PyMySQL formats with `%`), the same under any `sql_mode` |
| Identifier case | unquoted folds to upper | unquoted folds to lower | per collation (usually case-insensitive) | case-sensitive | table and schema names as `lower_case_table_names` says (case-sensitive on Linux by default); routine and column names case-insensitive |
| "Schema" | user / schema | schema (inside a database); the default is `public` | schema (`dbo` by default) | database | database |

### What PostgreSQL makes stronger than Oracle

DDL runs inside a transaction, so the write gate offers a **verified
preview**: run the statement, verify it, `ROLLBACK`. The person approves a
result that was observed, not one that was predicted. An `ALTER TABLE ...
DROP COLUMN` shows the PL/pgSQL function it would break before anything
commits. Oracle's pythia can only report that breakage after the fact. A
rehearsal is bounded (`statement_timeout` 30 s, `lock_timeout` 5 s) so a
preview never rewrites a big table or waits on a lock for long. Statements
that refuse a transaction block (`CREATE INDEX CONCURRENTLY`, `VACUUM`, ...)
are refused outright: with no rehearsal and no rollback, nothing about them
could be shown honestly.

### What PostgreSQL makes weaker, and the gate must cover

- **Overloads.** `CREATE OR REPLACE FUNCTION f(int)` when `f(text)` exists
  creates a second function and leaves the first one live. The rehearsal
  sees which object was created and which replaced, and the preview says
  plainly that the old overload stays callable.
- **Late binding.** No `INVALID` status exists to compare before and after.
  Without `plpgsql_check`, "nothing broke" cannot be shown, and the report
  says "not verified", never "clean".
- **`search_path`.** An unqualified name resolves by `search_path`, not by
  the configured schema. `check` reports a mismatch, and `apply` refuses
  one, as pythia's `refuse_schema_mismatch` does. `ddl` is rebuilt with
  only `pg_catalog` on the path, as pg_dump does, so its names come out
  qualified and it runs the same from any session (found by the DDL round
  trip, not by reasoning).
- **A rehearsal is not free.** It runs triggers, takes locks for its
  duration, and advances any sequence it draws from (`nextval` is not rolled
  back). The preview of a DML statement says so, and SECURITY.md lists it.
  The row-set probe also runs the statement's own predicate before approval,
  as pythia's does.
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
| 1 · read · done | output windowing and truncation, read commands, impact summary and the rule-2 review line | PostgreSQL read tier: `ls`, `src`, `cols`, `ddl`, `grep`, `deps`, `impact` (blind spots named), `errors` via `plpgsql_check` when present; CI job against `postgres` in a service container | an agent can do pythia's Learn phase on PostgreSQL: every read command passes against a fixture on a real server, `ddl` rebuilds what it describes, `errors` is checked with and without `plpgsql_check` |
| 2 · write · done | policy, tokens, grants, the console approval door, journal, the six steps, restore, the adversarial suite (`test_gate.py`) | PostgreSQL write tier: a dollar-quote-aware reader, the verified preview, overloads named, `search_path` refused, rows revalidated by ctid | every gate invariant is attacked in `test_gate.py`, and `live_check.py` runs preview → approve → confirm → restore on a real server: rows that moved and a verification that moved are rolled back, and a DROP COLUMN shows the function it would break |
| 3a · harness · done | harness tier; the PreToolUse guard and the session brief, `install`, `doctor`, `skill lint`; the adversarial guard suite (`test_harness.py`) | `psql` guard patterns, the `pythia-pg-*` skills, install field-tested in a scratch project | every way around the gate the guard knows is denied in `test_harness.py`, and a scratch project installed by `pythia-pg install` passes `doctor` |
| 3b · chat doors · done | the AskUserQuestion card and its answer hook, the MCP approver, local registration, doctor's door checks; the adversarial door suite (`test_doors.py`) | the doors in the skills and AGENTS.md | every way to mint without a person's Approve on the card is refused in `test_doors.py`, and the MCP server answers a real JSON-RPC session |
| 3c · release · done | release tier, `deploy`: a release rehearsed and committed as one transaction under one approval; the adversarial suite (`test_release.py`) | `split_script` (dollar quotes, line numbers), the `pythia-pg-deploy` skill, a live release that adds a column and fills it | every way to land part of a release that was not approved as a whole is refused in `test_release.py`, and `live_check.py` lands and rolls back a release on a real server |
| 3d · leases · done | autopilot leases: request, card, any door, coverage, pause, report; the adversarial suite (`test_lease.py`) | the `pythia-pg-autopilot` skill, a live write under a lease | every way to land a write a lease does not cover is refused in `test_lease.py` |
| 3e · lint · done | lint tier and framework (rules, severities, naming patterns in lint.json, the conventions file), lint in every preview and card, autopilot never lands a lint error; `test_lint.py` | PostgreSQL rules: SECURITY DEFINER without `search_path`, `SELECT *` in stored code, `timestamp` without time zone, `char(n)`, `money`, `serial`, `NOT IN (subquery)`, a table without a comment in the change (a RAISE argument mismatch is left to the rehearsal: PostgreSQL rejects it itself) | lint errors are shown in every preview and never landed unasked |
| 4a · SQL Server reads · done | — | pythia-mssql: the driver spike (§8), connect and read tiers, `errors` through `sys.dm_sql_referenced_entities`, tables rebuilt by `ddl`; CI against SQL Server 2022 in a service container | every read command passes against a fixture on a real server as a least-privilege login, and `ddl` rebuilds what it describes |
| 4b · SQL Server writes · done | `escaped`: a rehearsal whose transaction ended inside the statement is never called unwritten — exit 3, the approval spent, a lease paused; previews name only the doors an engine has | the write tier: a T-SQL reader (`N''`, `[ ]`, nested comments, `GO`), classification, the rehearsal verified by binding modules before and after and run one transaction level down (a trigger's `COMMIT` commits nothing), rows revalidated by `CHECKSUM_AGG` | as Phase 2, for SQL Server — and a trigger that commits past the rehearsal is reported as written, on a real server |
| 4c · SQL Server harness · done | — | harness (`sqlcmd`, `osql`, `Invoke-Sqlcmd`, `bcp in`, `sqlpackage` Publish), release (`GO` batches, a module's body to the end of its batch) and lint tiers (ten T-SQL rules), the `pythia-mssql-*` skills | as Phase 3, for SQL Server: `install` field-tested, and a release, a lint error and a lease write on a real server |
| 5a · MongoDB reads · done | `cols` may name a blind spot (columns inferred from a sample); the `document` bind style | pythia-mongo: pymongo, every command a reviewable file (`commands/`), connect and read tiers — `cols` from a sample, `impact` through view pipelines, `errors` as the documents a validator rejects and views that fail; CI against MongoDB 7.0 and 8.0 replica sets | every read command passes against a fixture on a real server as a `read`-role user, and `ddl` rebuilds what it describes |
| 5b · MongoDB writes · done | a second write path for what cannot be rehearsed: predicted before approval, written, verified after — exit 3 when the result differs from the prediction | data rehearsed in a transaction (a replica set is required); validators, indexes and views predicted (the documents a validator would reject, the duplicates a unique index would meet, a view's first documents); snapshot and restore | as Phase 2, for MongoDB, with every prediction checked against what the write did |
| 5c · MongoDB harness · done | the MCP hooks (`mcp_tool_pattern`, `mcp_write_tool_pattern`); an MCP tool's input read as a document too, keys included; a script read inside a quoted command (`bash -c '...'`); a predicted write's report says it was read back | harness (`mongosh`, `mongo`, `mongoimport`, `mongorestore`, `mongofiles`, MongoDB's MCP tools by name), release (a data release: one command document or a JSON array of them per file) and lint tiers (seven rules over validators, indexes and updates), the `pythia-mongo-*` skills | as Phase 3, for MongoDB: `install` field-tested, and a release, a lint finding and a lease write on a real server |
| 6 · several engines in one project · done | an answer hook leaves another tool's card alone; a SQL-sounding MCP tool is denied without being claimed; an install replaces its hooks where they stood; `test_family.py` and the `family` CI job | — | pythia for Oracle and the three engines install into one project in any order, a second run changes nothing, each doctor passes, and each guard and answer hook acts for its own engine only |
| 7 · first release · done | version 0.1.0; `release.yml` (the tag checked against the version and the changelog, then trusted publishing); README.vi.md held to README.md by a test; PyPI metadata | the same, for each engine; engines ask for `pythia-core>=0.1.0,<0.2` | each repository's tag publishes it to PyPI, core first, and `pip install pythia-postgres` (or `-mssql`, `-mongo`) from PyPI installs a working command |
| 8 · minimum versions · done | `floor_warning` (optional, in no tier): `check` says when the server is older than the engine's floor, and refuses nothing | the live check on PostgreSQL 14 to 18, SQL Server 2017 to 2025 and MongoDB 7.0 and 8.0; each engine's floor (14, 2017, 7.0) read from the server's own version number | every version CI runs passes the whole live check, and `check` warns below each floor |
| 9a · MySQL reads · done | — | pythia-mysql: the driver spike (§8), connect and read tiers — read sessions `READ ONLY`; `impact` through view usage, triggers and foreign keys, then routine, trigger and event bodies by name; what the account cannot see read from `SHOW GRANTS` and named; `errors` as the views MySQL marked invalid; `src` and `ddl` rebuilt; CI against MySQL 8.4, 9.4 and 9.5 | every read command passes against a fixture on a real server as a least-privilege account, with and without `TRIGGER` and `EVENT`, and every `ddl` runs into a copy that matches its original |
| 9b · MySQL writes · done | — (the predict tier of Phase 5b) | data rehearsed in a transaction, first inside a savepoint so a write MySQL cannot roll back (warning 1196) is `escaped`; rows checked by count and an order-free sum of `md5(json_array(...))`; DDL predicted — a view's SELECT run with `LIMIT 0`, the views MySQL would mark invalid read from information_schema — then written once and read back (exit 3 when it differs); routines `NOT VERIFIED`; a CREATE for an existing routine replaces it, checked under a scratch name first, the old one put back if MySQL refuses the new; snapshot and restore | as Phase 2, for MySQL, with every prediction checked against what the write did, a MyISAM leak reported `WRITTEN?`, and a restore that needs `SET_ANY_DEFINER` refused with nothing written |
| 9c · MySQL harness · done | — | the harness tier (the `mysql`, `mysqlsh`, `mariadb` clients, `mysqlimport`, `mysqladmin`), the release tier (`DELIMITER` and stored-program bodies; a release is data only, its DDL refused with nothing written), ten lint rules (`mysql-non-transactional` the one error), the seven `pythia-mysql-*` skills, the `family` job with four engines, 0.1.0 prepared | as Phase 3, for MySQL: the live check lands a release whole, rolls one back whole when its rows move after approval, shows a lint error in a real preview, and lands a predicted write under a lease |
| 10 · second release | version 0.1.1: `floor_warning`, and DESIGN and SECURITY for MySQL | pythia-postgres, pythia-mssql and pythia-mongo 0.1.1 (their floors), pythia-mysql 0.1.0, each asking for `pythia-core>=0.1.1,<0.2` | the five wheels install together from one index, core first; each tag publishes, and `pip install pythia-mysql` from PyPI installs a working command |

Not planned: moving pythia for Oracle onto pythia-core. That would be the
owner's decision, taken after Phase 3, when there is a core worth moving to.

## 8. Open questions

Each is asked when its phase starts, not guessed now:

1. **SQL Server driver — settled: pymssql.** The Phase 4 spike, against
   SQL Server 2022:
   - pymssql 2.4: wheels with FreeTDS inside for Windows, macOS and Linux
     on Python 3.9–3.13, so nothing else needs installing; every server
     error raised with its number; pyformat binds; TLS with `encryption`.
     It never turns `%%` into `%`, and it reads `%(` as a placeholder even
     inside a literal: pythia-mssql's loader adapts, and its suite checks.
   - python-tds 1.17 (pure Python): an error from `sp_refreshsqlmodule`
     was not raised — it sat in `cursor.messages` while the transaction it
     ended was gone. A gate that must never call a broken result clean
     cannot use it.
   - mssql-python: wheels only for Python 3.10 and later; the family floor
     is 3.9.
   - pyodbc: wheels everywhere, but every machine needs Microsoft's ODBC
     driver installed by hand.
2. **Minimum versions — settled (Phase 8), each by a CI run on every
   version.** PostgreSQL 14: 14 to 18 pass the whole live check; 13 is past
   its end of life, and lacks `BEGIN ATOMIC`, whose bodies pg_depend tracks.
   SQL Server 2017: 2017, 2019, 2022 and 2025 pass; 2016 has no Linux image
   to test on, and its extended support ended in July 2026. MongoDB 7.0
   (6.0 is past its end of life); 7.0 and 8.0 pass, each a single-node
   replica set with authentication. CI keeps every one of these versions;
   below its floor an engine's `check` warns, and refuses nothing.
3. **`plpgsql_check` — settled: optional and detected.** With it, a preview
   checks PL/pgSQL and names what broke; without it, the preview says
   `NOT VERIFIED` and never calls a change clean. Requiring it would shut
   pythia-pg out of the managed services that do not offer the extension.
4. **Publishing — settled: PyPI**, as `pythia-core`, `pythia-postgres`,
   `pythia-mssql` and `pythia-mongo` (all four were free in October 2026).
   A `vX.Y.Z` tag publishes by trusted publishing, as pythia does; the core
   goes first, and each engine asks for the core's release series
   (`pythia-core>=0.1.1,<0.2` since 0.1.1, when `floor_warning` arrived).
   `pythia-mysql` joins with its 0.1.0, in the second release.
5. **Vietnamese documentation — settled for the README.** Each repository
   ships README.vi.md from 0.1.0, held to README.md's headings and commands
   by a test. pythia's GUIDE has no family counterpart yet.
6. **MySQL — settled (Phase 9): PyMySQL, MySQL 8.4 LTS, DDL predicted.**
   The Phase 9 spike, against MySQL 8.4:
   - PyMySQL 1.2: pure Python, on Python 3.9 and later; pyformat binds,
     formatted by Python's `%` with a dict, so `%%` arrives as `%` with no
     adapter; TLS whenever the server offers it. mysql-connector-python and
     mysqlclient need Python 3.10 (mysqlclient a C library too).
   - Every DDL statement commits implicitly, pending DML with it, so DDL
     cannot be rehearsed: it takes Phase 5b's predict path, and data, in an
     InnoDB transaction, is rehearsed.
   - `SET SESSION TRANSACTION READ ONLY` refuses DDL as well as DML (1792):
     the read commands' second line. `START TRANSACTION READ ONLY` alone
     does not — a `CREATE TABLE` commits it and runs.
   - information_schema records which tables, views and functions a view
     reads, and marks a view invalid when one goes; it records nothing a
     routine, trigger or event body uses. It hides routines, triggers and
     events from an account without `SHOW_ROUTINE`, `TRIGGER` or `EVENT`,
     and `SHOW GRANTS` (active roles merged in) says which.
   - The floor is 8.4 LTS (8.0 reached its end of life in April 2026); CI
     runs 8.4 and the innovation releases 9.4 and 9.5. MariaDB, whose
     catalog differs, is a question for a later phase; `check` names it.
   - `pythia-mysql` was free on PyPI in October 2026.

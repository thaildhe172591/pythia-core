# Changelog

## 0.11.0.dev0 — unreleased

Phase 5b of the plan in DESIGN.md: a second write path, for what cannot be
rehearsed.

- **The predict tier: `execute`, `verify_written`.** An engine's
  `rehearse` may return `predicted: True` — nothing ran. The preview and
  the card say "predicted — nothing ran"; after the approval and the same
  checks (token, grant, a prediction that moved), core runs the statement
  once and asks the engine what landed. A refusal wrote nothing and keeps
  the approval; a write that may have landed is `WRITTEN?` (exit 3); a
  result unlike the prediction is reported and exits 3 with the restore,
  and pauses an autopilot lease. The journal keeps `verified_after`.
- **`deploy` refuses a predicted statement** before anything is written:
  all or nothing needs one transaction.
- **A COMMIT that did not answer is `WRITTEN?`, never an error.** A
  connection that fails during COMMIT (any engine) left the write landed or
  not, and nobody can tell which: `apply` and `deploy` now report it as
  escaped — exit 3, the approval spent, every restore printed — where they
  used to exit 1 with "error". Each kind of escape says its own cause.
- **An engine that predicts without the predict tier is refused** before
  its preview.
- Adversarial tests in `test_gate.py`, `test_release.py`, `test_lease.py`,
  mutation-checked.

## 0.10.0.dev0 — unreleased

Phase 5a of the plan in DESIGN.md: what MongoDB's reads needed from the core.

- **`cols` may say its answer is partial.** An engine that infers columns —
  MongoDB reads them from a sample of documents — returns `(cols, rows,
  blind_spot)`; `cols` prints `-- blind spot: ...`, and its JSON carries
  `verified` and `blind_spot`.
- **The `document` bind style**, for an engine whose statements are command
  documents with `{"$bind": ...}` values.
- README.md names every engine; DESIGN.md marks Phase 5 begun (5a now, then
  5b and 5c), settles MongoDB 7.0 as the floor, and fills in MongoDB's
  column where 5a learned it.

## 0.9.0.dev0 — unreleased

Phase 4b of the plan in DESIGN.md: what SQL Server's writes needed from the
core.

- **A rehearsal that escaped its transaction is never called unwritten.** An
  engine's `rehearse` may return `escaped` (why) when its transaction ended
  inside the statement — on SQL Server a trigger's `COMMIT` can end it, and
  commit what ran before anyone approved it. `apply` and `deploy` then roll
  back what is left, journal the entries as `escaped`, spend the approval,
  pause an autopilot lease, print every restore and exit 3. A release names
  each statement up to the one that escaped. `lease.pause()` is new.
  Adversarial tests in `test_gate.py`, `test_release.py`, `test_lease.py`.
- **An engine without the harness tier offers only the terminal door.** Its
  previews, refusals and `approve` no longer name AskUserQuestion or the
  MCP tool: without `install` nothing turns those answers into a grant, and
  a developer clicking Approve in a dialog that does nothing is told a lie.
- **DESIGN.md §8: the SQL Server driver is settled — pymssql**, with the
  spike's evidence for each candidate. §5 now verifies SQL Server modules
  with `sys.dm_sql_referenced_entities`: `sp_refreshsqlmodule` rolls back the
  caller's whole transaction when it fails. The roadmap splits Phase 4 into
  reads (4a), writes (4b) and the harness (4c), and the 3e row no longer
  lists a RAISE rule the PostgreSQL engine leaves to the rehearsal.
- `queries.py` says how an engine whose driver never turns `%%` into `%`
  keeps the one query contract.
- DESIGN.md §5 says where SQL Server lets a trigger end the caller's
  transaction, and which SQL Server clients the guard denies; the roadmap
  marks Phase 4 done.

## 0.8.0.dev0 — unreleased

Phase 3e of the plan in DESIGN.md: the lint framework. Phase 3 is complete.

- **The lint tier:** `lint_rules()`, each rule an id, a default severity, a
  summary and a check over one statement that can see the whole change.
- **`lint <files>`** lints a change's files as one run and exits 1 on an
  error; **`lint --rules`** lists every rule with its effective severity.
- **`.pythia/<engine>/lint.json`**: severities (`error`, `warn`, `info`,
  `off`) and naming patterns per object type, refused when it holds an
  unknown key, rule, severity or a pattern that does not compile.
- **Lint in the gate:** findings in every apply and deploy preview, errors on
  the card, and autopilot never lands a lint error. A rule that crashes is a
  warning, never a block.
- The brief asks for `lint` before any apply, and names
  `.pythia/<engine>/conventions.md` when the team keeps one.
- **`tests/test_lint.py`**, mutation-checked.

## 0.7.0.dev0 — unreleased

Phase 3d of the plan in DESIGN.md: autopilot leases.

- **`autopilot request`** writes a card — connection, groups, hours, writes,
  destructive or not, the task — with an 8-digit token. Nothing is
  delegated until the developer approves it through any door, or runs
  **`autopilot on`** at a console.
- **While active, `apply` and `deploy` land what the lease covers** in one
  run, still snapshotted, rehearsed, verified and journaled. Never another
  or a protected connection (read fresh each write), a group or a
  destructive statement the card did not name, or more than 1000 rows. A
  preview says why a write was not covered.
- **A lease ends** when it expires, is spent, is revoked (`autopilot off`,
  anyone's) or pauses itself — on a write that breaks something, or five
  failures in a row. A missing, corrupt or unapproved lease file is no lease.
- **A lease naming a denied group lifts the deny while it lasts**, as its
  card says.
- **`autopilot report`** lists every write under a lease, with restores.
- The guard denies writing the lease file by hand; the session brief shows
  the lease.
- **`tests/test_lease.py`**, adversarial and mutation-checked.

## 0.6.0.dev0 — unreleased

Phase 3c of the plan in DESIGN.md: the release.

- **`deploy <files>`:** a release — scripts holding many statements —
  through the gate as one transaction. Every statement is rehearsed in
  order inside it, snapshotted to the journal before it runs, its rows
  measured and its verification taken at its own point, then everything
  rolls back. One card lists them all; one approval (a 10-digit token,
  through any door) covers them.
- **The confirm is all or nothing.** The grant, the files and every
  statement's bytes are checked before anything runs; the release then runs
  again in one transaction and commits only if every member still matches
  what was approved — token, row count, verification. Anything else, or a
  failure at any statement, rolls the whole release back and leaves the
  approval unspent.
- **Refused before anything runs:** a statement the engine cannot classify,
  a session setting inside a release, a denied group.
- **The release tier:** `split_script`.
- A member's own token is refused at every door: a release is approved as a
  whole.
- The guard lets a deploy preview through, and a confirm with a valid grant.
- **`tests/test_release.py`**, adversarial and mutation-checked.

## 0.5.0.dev0 — unreleased

Phase 3b of the plan in DESIGN.md: approval from chat.

- **AskUserQuestion, answered through `approve --hook`.** The agent asks with
  the output of `approve --card <token>` verbatim. Claude Code's PostToolUse
  hook mints a grant (approver `chat`) only when the question carried that
  card and the developer chose Approve. It refuses, naming why: the agent's
  own words, an answer the agent wrote into its own call (checked against
  the session transcript), a dialog that closed unattended, a typed reply,
  no answer. Each refusal that is not the developer's no names the next
  door.
- **The MCP approver, `mcp`:** a stdio JSON-RPC server with one tool,
  `<server>_approve`, marked as needing the user. An elicitation shows the
  card; only an explicit Approve mints (approver `mcp`). A decline faster
  than anyone could read is the client answering for nobody, and says so.
- **One grant path.** The console, the hook and the MCP tool all end in
  `gate.grant_approval`. The journal records the confirm command, so every
  door says exactly what to run next.
- **The preview and the refusals name the doors in order:** AskUserQuestion,
  the MCP tool, the terminal. So do the session brief and the instruction
  block.
- **`install`** adds the PostToolUse hook and a deny rule for
  `approve --hook`, and registers the approver in this machine's files
  only: Claude Code's local scope for the project (`~/.claude.json`, written
  atomically, every other key kept) and Codex's `~/.codex/config.toml` (a
  marked block; a table of that name that is not ours is left alone).
- **`doctor`** checks both doors, and warns about `dontAsk`, an allow rule
  for AskUserQuestion, and a PreToolUse hook with an empty or `*` matcher.
- **The guard** denies `approve --hook` from the agent's shell.
- **`tests/test_doors.py`** tries each way to mint without a person's
  Approve on the card, and runs a JSON-RPC session against the MCP server.

## 0.4.0.dev0 — unreleased

Phase 3a of the plan in DESIGN.md: the harness.

- **`hook pre-tool`, the PreToolUse guard.** It denies the ways around the
  gate it knows: the engine's raw client given write SQL or a script (named
  anywhere in the command, so also behind `env`, `sudo`, `docker exec`,
  `ssh`, a pipe or `bash -c`; flags and redirections are read with quoted
  SQL emptied, so a `<` in a WHERE clause is not a script), an MCP SQL
  tool given write
  SQL, a grant written by hand, the console seam or `grants.mint` from a
  one-liner, the agent setting `<PREFIX>_CI`, reading `connections.json`,
  and editing the policy or the journal by hand. It allows without a prompt
  one plain engine command that cannot write and a confirm with a valid
  grant on that connection, and stays out of the rest. Commands are read
  with the CLI's own parser. A crash is no decision.
- **`hook session-start`:** the rules, how this machine spells the command,
  and the project's connections and policy.
- **`install`:** `connections.json` scaffolded once with mode 600, the
  `.gitignore` lines, the hooks and deny rules in
  `.claude/settings.local.json` (this interpreter's absolute path), a marked
  block in CLAUDE.md and AGENTS.md that leaves the developer's text and
  pythia for Oracle's block alone, and the engine's skills in
  `.claude/skills` and `.agents/skills`. A second run changes nothing.
- **`doctor`** checks each piece, including a hook whose interpreter is
  gone, and exits 1 on a fail. **`skill lint`** is pythia's skill contract.
- **The harness tier is settled:** `raw_client_pattern`,
  `write_sql_pattern`, `script_pattern`, `connections_template`,
  `skills_dir`.
- **`tests/test_harness.py`** tries each way around the guard: a path
  spelled with `..`, a capital letter or a symlink, a client behind a
  wrapper, `&&` after an allowed command, a stranger's `-m` package.

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

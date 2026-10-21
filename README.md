# pythia-core

**English** · [Tiếng Việt](https://github.com/thaildhe172591/pythia-core/blob/main/README.vi.md)

The part of pythia that is the same for every database. Engine packages
build on it: [pythia-postgres](https://github.com/thaildhe172591/pythia-postgres),
[pythia-mssql](https://github.com/thaildhe172591/pythia-mssql),
[pythia-mongo](https://github.com/thaildhe172591/pythia-mongo) and
[pythia-mysql](https://github.com/thaildhe172591/pythia-mysql).

**Alpha (0.1.1).** What is here today:
- the engine contract and its conformance check;
- per-engine connection resolution;
- the query-contract lint;
- the commands every engine's command line inherits:
  - `version`, `connections` and `check` (which says when the server is
    older than the engine's floor);
  - the read commands `ls`, `src`, `cols`, `ddl`, `grep`, `deps`, `impact`
    and `errors`, windowed and never truncated silently;
  - the write gate: `apply` (snapshot, rehearse inside a transaction and
    roll back — or, for what cannot be rehearsed, predict, then write once
    and check what landed — preview), `approve` (the developer: at a console, or from
    chat through AskUserQuestion and `approve --hook`, or the MCP approver
    `mcp`),
    `apply --confirm` (commits only what was approved), `policy`,
    `journal list | show | restore` and `history`;
  - `deploy <files>`: a release rehearsed and committed as one transaction,
    under one approval — all of it lands, or none of it;
  - `autopilot request | on | off | status | report`: a lease the developer
    approves once, covering only the connection, groups and limits its card
    names;
  - `lint <files>` (and `lint --rules`): the engine's rules and the team's
    severities and naming patterns over a change, shown in every preview;
  - the harness: `hook pre-tool` (the guard that denies the ways around the
    gate and waves through what cannot write), `hook session-start` (the
    rules and the project's state), `install` (connections scaffold,
    `.gitignore`, hooks, the MCP approver in this machine's own files,
    instruction blocks, skills — a second run changes nothing), `doctor` and
    `skill lint`.

What each gate stops, and what it does not, is in
[SECURITY.md](https://github.com/thaildhe172591/pythia-core/blob/main/SECURITY.md). The plan, and how each idea maps to each
database, is in [DESIGN.md](https://github.com/thaildhe172591/pythia-core/blob/main/DESIGN.md).

[pythia](https://github.com/thaildhe172591/pythia) for Oracle is a separate,
self-contained tool. It does not use this package and is not changed by it.

## Install

```bash
pip install pythia-core
```

pythia-core alone has no command: install an engine package —
`pythia-postgres`, `pythia-mssql` or `pythia-mongo` — and it comes with it.
It depends on nothing outside the Python standard library, by contract. The
development version is the main branch:

```bash
pip install "git+https://github.com/thaildhe172591/pythia-core"
```

## Writing an engine

```python
from pythia_core import cli
from pythia_core.engine import engine_problems

class PostgresEngine:
    name, label, command = "postgres", "PostgreSQL", "pythia-pg"
    env_prefix, version, bind_style = "PYTHIA_PG", "0.1.0", "pyformat"
    # ...the connect tier: driver_errors, connect, session, close,
    # connect_failure, schema_of, target_of, server_version, current_schema
    # ...the read tier: object_names, source, columns (which may name a
    # blind spot, when its columns are inferred), ddl, grep, dependencies,
    # dependents, compile_errors
    # ...and the write tier: classify, prepare_statement, parse_object,
    # destructive_reason, dml_probe, measure_row_set, snapshot, rehearse,
    # restore_statement, privilege_warning
    # ...the predict tier, for statements that cannot be rehearsed:
    # execute, verify_written
    # ...the harness tier: raw_client_pattern, write_sql_pattern,
    # script_pattern, connections_template, skills_dir
    # ...the release tier: split_script
    # ...and the lint tier: lint_rules

assert engine_problems(PostgresEngine(),
                       ("connect", "read", "write", "harness", "release",
                        "lint")) == []

def main(argv=None):
    return cli.main(PostgresEngine(), argv)          # your console script
```

Each engine keeps its connections in `.pythia/<engine>/connections.json`.
That is never pythia for Oracle's `.pythia/connections.json`, so one project
can use both safely.

## Tests (no database, no driver)

```bash
python tests/test_core.py
python tests/test_cli.py
python tests/test_read.py
python tests/test_gate.py
python tests/test_harness.py
python tests/test_doors.py
python tests/test_release.py
python tests/test_lease.py
python tests/test_lint.py
python tests/test_family.py     # several engines in one project, with two fakes
```

CI runs them on Ubuntu, Windows and macOS, Python 3.9 and 3.13. Its
`family` job also installs pythia for Oracle and the four engines into one
project and runs `tests/family_check.py`: every install keeps the others'
hooks and blocks, a second run changes nothing, each doctor passes, each
guard and answer hook acts for its own engine only.

## License

MIT

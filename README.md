# pythia-core

The part of pythia that is the same for every database. Engine packages
build on it: [pythia-postgres](https://github.com/thaildhe172591/pythia-postgres)
first, then SQL Server and MongoDB.

**Pre-alpha (Phase 3a).** What is here today:
- the engine contract and its conformance check;
- per-engine connection resolution;
- the query-contract lint;
- the commands every engine's command line inherits:
  - `version`, `connections` and `check`;
  - the read commands `ls`, `src`, `cols`, `ddl`, `grep`, `deps`, `impact`
    and `errors`, windowed and never truncated silently;
  - the write gate: `apply` (snapshot, rehearse inside a transaction and
    roll back, preview), `approve` (the developer, at a console),
    `apply --confirm` (commits only what was approved), `policy`,
    `journal list | show | restore` and `history`;
  - the harness: `hook pre-tool` (the guard that denies the ways around the
    gate and waves through what cannot write), `hook session-start` (the
    rules and the project's state), `install` (connections scaffold,
    `.gitignore`, hooks, instruction blocks, skills — a second run changes
    nothing), `doctor` and `skill lint`.

What each gate stops, and what it does not, is in
[SECURITY.md](SECURITY.md). The plan, and how each idea maps to each
database, is in [DESIGN.md](DESIGN.md).

[pythia](https://github.com/thaildhe172591/pythia) for Oracle is a separate,
self-contained tool. It does not use this package and is not changed by it.

## Install (from git while pre-alpha)

```bash
pip install "git+https://github.com/thaildhe172591/pythia-core"
```

pythia-core alone has no command: install an engine package. It depends on
nothing outside the Python standard library, by contract.

## Writing an engine

```python
from pythia_core import cli
from pythia_core.engine import engine_problems

class PostgresEngine:
    name, label, command = "postgres", "PostgreSQL", "pythia-pg"
    env_prefix, version, bind_style = "PYTHIA_PG", "0.1.0.dev0", "pyformat"
    # ...the connect tier: driver_errors, connect, session, close,
    # connect_failure, schema_of, target_of, server_version, current_schema
    # ...the read tier: object_names, source, columns, ddl, grep,
    # dependencies, dependents, compile_errors
    # ...and the write tier: classify, prepare_statement, parse_object,
    # destructive_reason, dml_probe, measure_row_set, snapshot, rehearse,
    # restore_statement, privilege_warning
    # ...and the harness tier: raw_client_pattern, write_sql_pattern,
    # script_pattern, connections_template, skills_dir

assert engine_problems(PostgresEngine(),
                       ("connect", "read", "write", "harness")) == []

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
```

CI runs them on Ubuntu, Windows and macOS, Python 3.9 and 3.13.

## License

MIT

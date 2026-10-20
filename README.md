# pythia-core

The part of pythia that is the same for every database. Engine packages
build on it: [pythia-postgres](https://github.com/thaildhe172591/pythia-postgres)
first, then SQL Server and MongoDB.

**Pre-alpha (Phase 0).** What is here today: the engine contract and its
conformance check, per-engine connection resolution, the query-contract lint,
and the `version` / `connections` / `check` commands that every engine's
command line inherits. The write gate arrives in Phase 2. The plan, and how
each idea maps to each database, is in [DESIGN.md](DESIGN.md).

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

assert engine_problems(PostgresEngine()) == []      # in your test suite

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
```

CI runs them on Ubuntu, Windows and macOS, Python 3.9 and 3.13.

## License

MIT

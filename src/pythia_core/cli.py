"""The command line every engine package runs: `pythia-pg`, `pythia-mssql`,
`pythia-mongo` are each this, handed their engine.

    def main(argv=None):
        return pythia_core.cli.main(PostgresEngine(), argv)

Phase 0 ships the commands that prove the wiring end to end — `version`,
`connections` and `check`; the read and write commands arrive with the tiers
they need (DESIGN.md).
"""
import argparse
import json
import os
import pathlib
import sys

from . import __version__
from .config import config_rel, connection_summary, find_config, resolve_connection
from .engine import refuse_unless

# The tier each database command needs; version and connections need none.
COMMAND_TIERS = {"check": "connect"}


def json_envelope(command, connection, schema, cols, rows, truncated, **extra):
    payload = {"ok": True, "command": command, "connection": connection,
               "schema": schema, "rows": [dict(zip(cols, r)) for r in rows],
               "truncated": bool(truncated), **extra}
    return json.dumps(payload, default=str, ensure_ascii=False)


def build_parser(engine):
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--conn", metavar="NAME",
                        help=f"connection name in {config_rel(engine)}")
    common.add_argument("--json", action="store_true",
                        help="structured output")
    p = argparse.ArgumentParser(
        prog=engine.command,
        description=f"pythia for {engine.label} (pre-alpha). Each engine "
                    f"keeps its connections in {config_rel(engine)}.")
    sub = p.add_subparsers(dest="command", metavar="<command>")
    sub.required = True
    sub.add_parser("version", parents=[common],
                   help="engine and core versions")
    sub.add_parser("connections", parents=[common],
                   help="configured connections, never their secrets")
    sub.add_parser("check", parents=[common],
                   help="connect read-only and say where unqualified names land")
    return p


def cmd_version(engine, ns):
    if ns.json:
        print(json.dumps({"ok": True, "command": "version", "engine": engine.name,
                          "engine_version": engine.version,
                          "core_version": __version__}))
    else:
        print(f"{engine.command} {engine.version} — {engine.label} "
              f"(pythia-core {__version__})")
    return 0


def cmd_connections(engine, ns, cfg):
    rows = connection_summary(cfg, engine)
    if ns.json:
        print(json.dumps(rows, indent=2))
        return 0
    if not rows:
        print(f"No {engine.label} connections configured. Create "
              f"{config_rel(engine)} in the project root.")
        return 0
    print(f"{'':1} {'NAME':<16} {'USER':<26} {'SCHEMA':<20} TARGET")
    for r in rows:
        mark = "*" if r["default"] else " "
        print(f"{mark} {r['name']:<16} {r['user']:<26} {r['schema']:<20} "
              f"{r['target']}")
    print("\n* default. Passwords are never printed — this command reads the "
          "config so you\n  do not have to open it. Pick one with --conn NAME.")
    return 0


def cmd_check(engine, conn, name, schema, ns):
    version = engine.server_version(conn)
    current = engine.current_schema(conn)
    drift = current is not None and current != schema
    cols = ("ITEM", "VALUE")
    rows = [("server", version), ("schema", schema),
            ("current_schema", current if current is not None else "")]
    if ns.json:
        print(json_envelope("check", name, schema, cols, rows, False,
                            engine=engine.name, schema_drift=drift))
        return 0
    for item, value in rows:
        print(f"{item:<16} {value}")
    if drift:
        print(f"\n! This session's current schema is {current!r}, but connection "
              f"{name!r} is configured for {schema!r}:\n  an unqualified name "
              f"lands in {current!r}. Set \"schema\": \"{current}\" in "
              f"{config_rel(engine)}, or fix the session's search path.")
    elif current is None:
        print(f"\n! {engine.label} did not name a current schema for this "
              "session — an unqualified CREATE would fail.")
    return 0


COMMANDS = {"check": cmd_check}


def main(engine, argv=None):
    argv = sys.argv[1:] if argv is None else argv
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", line_buffering=True)
        except (AttributeError, ValueError):
            pass
    ns = build_parser(engine).parse_args(argv)
    if ns.command == "version":
        return cmd_version(engine, ns)
    cwd = pathlib.Path.cwd()
    cfg, root = find_config(cwd, os.environ, engine)
    if ns.command == "connections":
        return cmd_connections(engine, ns, cfg)
    # refusals that need no database come first — an unreachable database
    # must never mask them
    refuse_unless(engine, COMMAND_TIERS[ns.command], ns.command)
    name, entry = resolve_connection(cfg, ns.conn, os.environ, cwd, root, engine)
    schema = engine.schema_of(entry)
    print(f"-- connection={name} schema={schema}", file=sys.stderr)
    errors = engine.driver_errors()
    try:
        handle = engine.connect(entry)
        conn = engine.session(handle, True)   # every Phase 0 command reads
    except errors + (OSError,) as e:
        # OSError too: a DNS or socket failure can arrive raw from the socket
        # layer instead of wrapped as the driver's own error
        sys.exit(engine.connect_failure(e, name, entry))
    try:
        return COMMANDS[ns.command](engine, conn, name, schema, ns)
    except errors as e:
        sys.exit(f"{engine.label} error: {e}")
    finally:
        engine.close(handle)

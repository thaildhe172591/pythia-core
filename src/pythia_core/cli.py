"""The command line every engine package runs: `pythia-pg`, `pythia-mssql`,
`pythia-mongo` are each this, handed their engine.

    def main(argv=None):
        return pythia_core.cli.main(PostgresEngine(), argv)

Phase 1 ships the read commands — the Learn movement of pythia's model: ask
the live catalog, never a dump, and know the blast radius before proposing
anything. The write commands arrive with the write tier (DESIGN.md).
"""
import argparse
import json
import os
import pathlib
import sys

from . import __version__
from .config import config_rel, connection_summary, find_config, resolve_connection
from .engine import refuse_unless
from .output import (ERROR_COLS, TREE_COLS, clip, emit_source, emit_table,
                     emit_text, fetch_n, format_errors, impact_summary,
                     json_envelope, render_tree, summary_lines)

# The tiers each database command needs; version and connections need none.
READ_TIERS = ("connect", "read")
COMMAND_TIERS = {"check": ("connect",), "ls": READ_TIERS, "src": READ_TIERS,
                 "cols": READ_TIERS, "ddl": READ_TIERS, "grep": READ_TIERS,
                 "deps": READ_TIERS, "impact": READ_TIERS, "errors": READ_TIERS}


def build_parser(engine):
    def common():
        """A fresh set of shared options per subcommand: argparse's parents
        share action objects, so one subcommand's defaults would otherwise
        rewrite another's."""
        c = argparse.ArgumentParser(add_help=False)
        c.add_argument("--conn", metavar="NAME",
                       help=f"connection name in {config_rel(engine)}")
        c.add_argument("--json", action="store_true", help="structured output")
        c.add_argument("--limit", type=int, default=200,
                       help="max rows for list output, 0 = no cap (default 200)")
        c.add_argument("--max-lines", type=int, default=2000, dest="max_lines",
                       help="max source/DDL lines, 0 = no cap (default 2000)")
        c.add_argument("--offset", type=int, default=0,
                       help="skip N lines/rows first (continue truncated output)")
        c.add_argument("--raw", action="store_true",
                       help="plain source, no line numbers or unit headers")
        return c

    p = argparse.ArgumentParser(
        prog=engine.command, allow_abbrev=False,
        description=f"pythia for {engine.label} (pre-alpha). Each engine "
                    f"keeps its connections in {config_rel(engine)}.")
    sub = p.add_subparsers(dest="command", metavar="<command>")
    sub.required = True
    sub.add_parser("version", parents=[common()],
                   help="engine and core versions")
    sub.add_parser("connections", parents=[common()],
                   help="configured connections, never their secrets")
    sub.add_parser("check", parents=[common()],
                   help="connect read-only and say where unqualified names land")
    s = sub.add_parser("ls", parents=[common()], help="find objects by name")
    s.add_argument("pattern", help='LIKE pattern, e.g. "t_order%%"')
    s = sub.add_parser("src", parents=[common()],
                       help="source with the database's own line numbers")
    s.add_argument("name")
    s = sub.add_parser("cols", parents=[common()],
                       help="columns of a table or view")
    s.add_argument("name")
    s = sub.add_parser("ddl", parents=[common()],
                       help="the statement that would create an object")
    s.add_argument("name")
    s = sub.add_parser("grep", parents=[common()],
                       help="every source line holding a text")
    s.add_argument("text")
    for cmd, what in (("deps", "what an object depends on"),
                      ("impact", "what depends on an object — run this before "
                                 "changing it")):
        s = sub.add_parser(cmd, parents=[common()], help=what)
        s.add_argument("name")
        s.add_argument("--depth", type=int, default=3,
                       help="levels to walk (default 3)")
    s = sub.add_parser("errors", parents=[common()],
                       help="what the database's checker reports, and what it "
                            "could not check")
    s.add_argument("name", nargs="?", default=None,
                   help="object name; omit for the whole schema")
    return p


# --- offline -------------------------------------------------------------------

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


# --- connect tier ---------------------------------------------------------------

def cmd_check(engine, conn, ns):
    name, schema = ns.conn_name, ns.schema
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


# --- read tier --------------------------------------------------------------------

def cmd_ls(engine, conn, ns):
    cols, rows = engine.object_names(conn, ns.schema, ns.pattern, fetch_n(ns.limit))
    rows, truncated = clip(rows, ns.limit)
    emit_table(ns, cols, rows, truncated)
    return 0


def cmd_src(engine, conn, ns):
    rows = engine.source(conn, ns.schema, ns.name)
    if not rows:
        sys.exit(f"No source found for {ns.name!r} in schema {ns.schema}.")
    shown, truncated = clip(rows, ns.max_lines, ns.offset)
    emit_source(ns, shown, truncated, len(rows))
    return 0


def cmd_cols(engine, conn, ns):
    found = engine.columns(conn, ns.schema, ns.name)
    if found is None:
        sys.exit(f"No table or view named {ns.name!r} in schema {ns.schema}.")
    cols, rows = found
    emit_table(ns, cols, rows, False)
    return 0


def cmd_ddl(engine, conn, ns):
    text = engine.ddl(conn, ns.schema, ns.name)
    if not text:
        sys.exit(f"No DDL for {ns.name!r} in schema {ns.schema}.")
    emit_text(ns, text)
    return 0


def cmd_grep(engine, conn, ns):
    cols, rows = engine.grep(conn, ns.schema, ns.text, fetch_n(ns.limit))
    rows, truncated = clip(rows, ns.limit)
    emit_table(ns, cols, rows, truncated)
    return 0


def _tree(engine, conn, ns, forward):
    walk = engine.dependencies if forward else engine.dependents
    found = walk(conn, ns.schema, ns.name, ns.depth)
    if found is None:
        sys.exit(f"No object named {ns.name!r} in schema {ns.schema}.")
    rows, blind = found
    shown, truncated = clip(rows, ns.limit)
    summary = None if forward else impact_summary(rows, ns.schema)
    if ns.json:
        extra = {"blind_spots": blind}
        if summary is not None:
            extra["summary"] = summary
        print(json_envelope(ns.command, ns.conn_name, ns.schema, TREE_COLS,
                            shown, truncated, **extra))
        return 0
    if not rows:
        what = (f"{ns.name} depends on nothing" if forward
                else f"nothing depends on {ns.name}")
        print(f"-- {what} (in what the catalog and a text search can see, "
              f"depth {ns.depth})")
    else:
        sys.stdout.write(render_tree(shown, f"{ns.schema}.{ns.name}"))
        if truncated:
            print(f"-- truncated at {len(shown)} rows (raise --limit, or "
                  "--limit 0 for no cap)")
        if summary is not None:
            for line in summary_lines(summary):
                print(line)
    for note in blind:
        print(f"-- blind spot: {note}")
    return 0


def cmd_deps(engine, conn, ns):
    return _tree(engine, conn, ns, forward=True)


def cmd_impact(engine, conn, ns):
    return _tree(engine, conn, ns, forward=False)


def cmd_errors(engine, conn, ns):
    found = engine.compile_errors(conn, ns.schema, ns.name)
    if found is None:
        sys.exit(f"No object named {ns.name!r} in schema {ns.schema}.")
    rows, blind = found
    shown, truncated = clip(rows, ns.limit)
    if ns.json:
        print(json_envelope(ns.command, ns.conn_name, ns.schema, ERROR_COLS,
                            shown, truncated, verified=blind is None,
                            blind_spot=blind))
        return 0
    sys.stdout.write(format_errors(shown))
    if truncated:
        print(f"-- truncated at {len(shown)} rows (raise --limit, or --limit 0 "
              "for no cap)")
    if blind is not None:
        print(f"-- NOT VERIFIED: {blind}")
    elif not rows:
        print(f"-- no errors for {ns.name or 'schema ' + ns.schema}")
    return 0


COMMANDS = {"check": cmd_check, "ls": cmd_ls, "src": cmd_src, "cols": cmd_cols,
            "ddl": cmd_ddl, "grep": cmd_grep, "deps": cmd_deps,
            "impact": cmd_impact, "errors": cmd_errors}


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
    for tier in COMMAND_TIERS[ns.command]:
        refuse_unless(engine, tier, ns.command)
    name, entry = resolve_connection(cfg, ns.conn, os.environ, cwd, root, engine)
    ns.conn_name, ns.schema = name, engine.schema_of(entry)
    print(f"-- connection={name} schema={ns.schema}", file=sys.stderr)
    errors = engine.driver_errors()
    try:
        handle = engine.connect(entry)
        conn = engine.session(handle, True)   # every command so far only reads
    except errors + (OSError,) as e:
        # OSError too: a DNS or socket failure can arrive raw from the socket
        # layer instead of wrapped as the driver's own error
        sys.exit(engine.connect_failure(e, name, entry))
    try:
        return COMMANDS[ns.command](engine, conn, ns)
    except errors as e:
        sys.exit(f"{engine.label} error: {e}")
    finally:
        engine.close(handle)

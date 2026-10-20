"""The command line every engine package runs: `pythia-pg`, `pythia-mssql`,
`pythia-mongo` are each this, handed their engine.

    def main(argv=None):
        return pythia_core.cli.main(PostgresEngine(), argv)

The read commands are pythia's Learn movement: ask the live catalog, never a
dump, and know the blast radius before proposing anything. The write
commands are its Do movement, through one gate (gate.py): `apply` previews,
the developer `approve`s, `apply --confirm` writes, and `journal restore` is
a write like any other.
"""
import argparse
import json
import os
import pathlib
import sys

from . import __version__, gate, journal, policy
from .config import (config_rel, connection_summary, find_config,
                     resolve_connection, state_dir)
from .engine import refuse_unless
from .human import human_at_the_keyboard
from .output import (ERROR_COLS, TREE_COLS, clip, emit_source, emit_table,
                     emit_text, fetch_n, format_errors, impact_summary,
                     json_envelope, render_tree, summary_lines)

# The tiers each database command needs. version, connections, approve,
# policy, history and journal list/show touch no database.
READ_TIERS = ("connect", "read")
WRITE_TIERS = ("connect", "read", "write")
COMMAND_TIERS = {"check": ("connect",), "ls": READ_TIERS, "src": READ_TIERS,
                 "cols": READ_TIERS, "ddl": READ_TIERS, "grep": READ_TIERS,
                 "deps": READ_TIERS, "impact": READ_TIERS, "errors": READ_TIERS,
                 "apply": WRITE_TIERS, "restore": WRITE_TIERS}
OFFLINE = ("version", "connections", "approve", "policy", "history", "journal")


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

    def write_flags(s):
        s.add_argument("--confirm", metavar="TOKEN",
                       help="write what the preview with this token showed — "
                            "only after the developer approved it")
        s.add_argument("--yes", action="store_true",
                       help="preview and write in one run: the developer's flag, "
                            "at a real console")
        s.add_argument("--depth", type=int, default=3,
                       help="impact levels in the preview (default 3)")
    s = sub.add_parser("apply", parents=[common()],
                       help="preview one statement (rehearsed, rolled back); "
                            "with --confirm, write it")
    s.add_argument("file")
    write_flags(s)
    s = sub.add_parser("approve", parents=[common()],
                       help="the developer approves a previewed token (a real "
                            "console only)")
    s.add_argument("token")
    s.add_argument("--card", action="store_true",
                   help="print what would be approved; mint nothing")
    s = sub.add_parser("policy", parents=[common()],
                       help="the write policy per statement group")
    s.add_argument("action", nargs="?", choices=("show", "set"), default="show")
    s.add_argument("group", nargs="?", choices=policy.GROUPS)
    s.add_argument("value", nargs="?", choices=policy.VALUES)
    s = sub.add_parser("journal", parents=[common()],
                       help="every preview and write; restore one (it previews "
                            "first)")
    s.add_argument("action", choices=("list", "show", "restore"))
    s.add_argument("id", nargs="?")
    write_flags(s)
    s = sub.add_parser("history", parents=[common()],
                       help="every version of one object the journal holds")
    s.add_argument("name")
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


# --- write tier ---------------------------------------------------------------------

def cmd_apply(engine, conn, ns):
    path = pathlib.Path(ns.file)
    if not path.is_file():
        sys.exit(f"No such file: {path}")
    return gate.run_apply(engine, conn, ns, path.read_text(encoding="utf-8"))


def cmd_restore(engine, conn, ns):
    """A restore is itself a write: the saved statement goes back through the
    same six steps — previewed, approved, confirmed. No silent restore."""
    entry = journal.read_entry(ns.state, ns.id)
    group = entry["meta"].get("group", "")
    if group not in policy.UNDOABLE or not entry["restore"].strip():
        sys.exit(f"Refused: entry {ns.id} is a {group or 'statement'} entry, and "
                 "the journal holds no undo for it — only the statement that "
                 "ran.\nReversing it means writing the opposite statement "
                 "by hand.")
    ns.file = f"journal:{ns.id}"
    print(f"Restoring from {ns.id} — this is itself a write and goes through "
          "the full six steps.", file=sys.stderr)
    return gate.run_apply(engine, conn, ns, entry["restore"],
                          origin={"restored_from": ns.id},
                          then=f"{engine.command} journal restore {ns.id}")


COMMANDS = {"check": cmd_check, "ls": cmd_ls, "src": cmd_src, "cols": cmd_cols,
            "ddl": cmd_ddl, "grep": cmd_grep, "deps": cmd_deps,
            "impact": cmd_impact, "errors": cmd_errors, "apply": cmd_apply,
            "restore": cmd_restore}


# --- offline: the developer's half and the record ------------------------------------

def cmd_approve(engine, ns):
    return gate.approve(engine, ns.state, ns.token, card_only=ns.card,
                        as_json=ns.json)


def cmd_policy(engine, ns):
    state = ns.state
    if ns.action == "set":
        if not ns.group or not ns.value:
            sys.exit(f"Usage: {engine.command} policy set <group> <value>\n"
                     f"Groups: {', '.join(policy.GROUPS)}; values: "
                     f"{', '.join(policy.VALUES)}.")
        eff = policy.load(state)
        if (policy.RANK[ns.value] > policy.RANK[eff[ns.group][0]]
                and not human_at_the_keyboard(engine)):
            sys.exit(f"Loosening the write policy ({ns.group}: "
                     f"{eff[ns.group][0]} → {ns.value}) is the developer's "
                     "decision, and no terminal is attached to this session.\n"
                     "Ask the developer to run this themselves:\n"
                     f"  {engine.command} policy set {ns.group} {ns.value}\n"
                     "(Tightening is always allowed.)")
        eff[ns.group] = (ns.value, "policy.json")
        policy.effective({g: v for g, (v, _) in eff.items()})   # validates
        print(f"Wrote {policy.save(state, eff)}")
    eff = policy.load(state)
    notes = getattr(engine, "rollback_notes", {}) or {}
    if ns.json:
        print(json.dumps({g: {"value": v, "source": src, "undo": notes.get(g)}
                          for g, (v, src) in eff.items()}))
        return 0
    print("Effective write policy:")
    for g, (v, src) in eff.items():
        print(f"  {g:<11} {v:<8} ({src})")
    if notes:
        print(f"\nIs rollback real? ({engine.label})")
        for g in policy.GROUPS:
            print(f"  {g:<11} {notes.get(g, '')}")
    return 0


def _entry_rows(state):
    rows = []
    for eid in journal.list_entries(state):
        meta = journal.read_entry(state, eid)["meta"]
        rows.append({"entry": eid, "kind": journal.kind(meta),
                     "group": meta.get("group", ""), "object": meta.get("object", ""),
                     "token": meta.get("token")})
    return rows


def cmd_journal(engine, ns):
    state = ns.state
    if ns.action == "list":
        rows = _entry_rows(state)
        shown, truncated = clip(rows, ns.limit)
        if ns.json:
            print(json.dumps({"entries": shown, "truncated": truncated}))
            return 0
        if not rows:
            print("-- the journal is empty: every apply writes an entry here.")
            return 0
        for r in shown:
            print(f"  {r['entry']:<48} {r['kind']:<8} {r['group']:<11} {r['object']}")
        if truncated:
            print(f"-- truncated at {len(shown)} entries (raise --limit, or "
                  "--limit 0 for no cap)")
        return 0
    if not ns.id:
        sys.exit(f"Usage: {engine.command} journal {ns.action} <entry>")
    entry = journal.read_entry(state, ns.id)
    if ns.json:
        print(json.dumps({**entry["meta"], "files": str(entry["path"])}, default=str))
        return 0
    m = entry["meta"]
    print(f"{ns.id}  {journal.kind(m)}  {m.get('group')}  {m.get('object')} "
          f"({m.get('type')}) on {m.get('connection')}")
    for f in ("before.sql", "after.sql", "restore.sql"):
        print(f"  {entry['path'] / f}")
    return 0


def cmd_history(engine, ns):
    """Every version of one object the journal holds, newest first."""
    want = str(ns.name).strip('"').split("(", 1)[0].lower()
    rows = []
    for eid in journal.list_entries(ns.state):
        e = journal.read_entry(ns.state, eid)
        m = e["meta"]
        if str(m.get("object", "")).split("(", 1)[0].lower() != want:
            continue
        real = e["after"] if m.get("applied") else e["before"]
        rows.append({"entry": eid, "kind": journal.kind(m),
                     "object": m.get("object"), "lines": len(real.splitlines()),
                     "restore_file": str(e["path"] / "restore.sql")})
    if ns.json:
        print(json.dumps({"object": ns.name, "versions": rows}))
        return 0
    if not rows:
        print(f"-- no journal history for {ns.name}")
        return 0
    for r in rows:
        print(f"  {r['entry']:<48} {r['kind']:<8} {r['lines']:>5} lines")
    print(f"\n-- through the gate (previews first, the developer approves):\n"
          f"     {engine.command} journal restore <entry>")
    return 0


OFFLINE_COMMANDS = {"approve": cmd_approve, "policy": cmd_policy,
                    "journal": cmd_journal, "history": cmd_history}


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
    ns.state = state_dir(root if root is not None else cwd, engine)
    if ns.command == "connections":
        return cmd_connections(engine, ns, cfg)
    key = ns.command
    if key == "journal" and ns.action == "restore":
        key = "restore"
        if not ns.id:
            sys.exit(f"Usage: {engine.command} journal restore <entry>")
    if key in OFFLINE_COMMANDS:
        return OFFLINE_COMMANDS[key](engine, ns)
    # refusals that need no database come first — an unreachable database
    # must never mask them
    for tier in COMMAND_TIERS[key]:
        refuse_unless(engine, tier, ns.command)
    if getattr(ns, "yes", False) and not human_at_the_keyboard(engine):
        sys.exit(gate.headless_yes(engine))
    name, entry = resolve_connection(cfg, ns.conn, os.environ, cwd, root, engine)
    ns.conn_name, ns.schema = name, engine.schema_of(entry)
    print(f"-- connection={name} schema={ns.schema}", file=sys.stderr)
    errors = engine.driver_errors()
    try:
        handle = engine.connect(entry)
        # reads run in a session the database itself keeps read-only; the
        # write path's defences are the gate's, not a transaction attribute
        conn = engine.session(handle, key not in ("apply", "restore"))
    except errors + (OSError,) as e:
        # OSError too: a DNS or socket failure can arrive raw from the socket
        # layer instead of wrapped as the driver's own error
        sys.exit(engine.connect_failure(e, name, entry))
    try:
        return COMMANDS[key](engine, conn, ns)
    except errors as e:
        sys.exit(f"{engine.label} error: {e}")
    finally:
        engine.close(handle)

"""The engine contract: what pythia-core asks of a database.

pythia-core holds what is the same whatever database sits behind a
connection: resolving the connection, the output contract, and (from Phase 2)
the write gate. An engine package — pythia-postgres, pythia-mssql,
pythia-mongo — answers the questions below for its own database. DESIGN.md
is the full plan.

The contract comes in tiers. A command needs every method of its tier, and an
engine that does not answer a tier yet is refused by name before anything
connects (`refuse_unless`) — half a command is worse than none. An engine that
cannot answer a question honestly must say so; a clean empty answer it cannot
stand behind is the worst failure available to a tool whose rule is "no
proposal before the blast radius is known".
"""
import re
import sys

# Identity every engine declares, as class attributes.
IDENTITY = (
    "name",         # registry key and state folder: "postgres" -> .pythia/postgres/
    "label",        # how a person names the database: "PostgreSQL"
    "command",      # the console script: "pythia-pg"
    "env_prefix",   # "PYTHIA_PG" -> PYTHIA_PG_CONFIG, PYTHIA_PG_CONNECTION
    "version",      # the engine package's own version
    "bind_style",   # "named" (:s) or "pyformat" (%(s)s) — see queries.py
)

CONNECT = (
    "driver_errors",    # () -> tuple of exception classes; imports the driver
                        #    or exits naming the package to install
    "connect",          # (entry) -> handle (a connection or a pool)
    "session",          # (handle, readonly) -> connection for one command;
                        #    readonly asks the database itself to refuse writes
    "close",            # (handle) -> None
    "connect_failure",  # (exc, conn_name, entry) -> message naming the next step
    "schema_of",        # (entry) -> configured schema, folded as the engine folds
    "target_of",        # (entry) -> where it points; never a secret
    "server_version",   # (conn) -> one line
    "current_schema",   # (conn) -> where an unqualified name lands, or None
)

# READ is settled (Phase 1); WRITE is a draft that Phase 2 settles
# (DESIGN.md). Both are listed so a command can be refused by tier, and so an
# engine author sees the whole road. Names are passed as the person typed
# them: folding them is the engine's business.
READ = (
    "object_names",     # (conn, schema, pattern, fetch) -> (cols, rows);
                        #    pattern is LIKE-style, at most `fetch` rows
    "source",           # (conn, schema, name) -> rows (unit, line, text);
                        #    [] when there is none
    "columns",          # (conn, schema, name) -> (cols, rows), None if no such
                        #    table or view
    "ddl",              # (conn, schema, name) -> text, '' if none
    "grep",             # (conn, schema, text, fetch) -> (cols, rows): each
                        #    source line holding text, at most `fetch` rows
    "dependencies",     # (conn, schema, name, depth) -> (rows, blind_spots)
    "dependents",       # (conn, schema, name, depth) -> (rows, blind_spots);
                        #    rows (level, schema, name, type, how) in walk
                        #    order, how "text" when only a name match vouches
                        #    for it; None when no object has the name
    "compile_errors",   # (conn, schema, name or None) -> (rows, blind_spot);
                        #    rows (name, type, line, position, severity,
                        #    message); blind_spot None only when the check
                        #    covered everything
)

WRITE = (
    # reading the statement: pure, testable without a database
    "classify", "prepare_statement", "parse_object", "name_conflicts",
    "reconcile_statement", "create_prefix", "destructive_reason", "dml_probe",
    "lint",
    # asking the catalog around the write
    "occupants", "object_source", "privilege_warning", "measure_row_set",
    "invalid_objects", "before_write",
)

TIERS = {"connect": CONNECT, "read": READ, "write": WRITE}

BIND_STYLES = ("named", "pyformat")


def missing(engine, tier):
    """Contract methods of `tier` the engine does not implement, in order."""
    return [m for m in TIERS[tier] if not callable(getattr(engine, m, None))]


def refuse_unless(engine, tier, command):
    """Exit before connecting when the engine cannot answer the whole tier a
    command needs, naming exactly what is missing."""
    gap = missing(engine, tier)
    if gap:
        sys.exit(f"{engine.command} {command}: {engine.label} does not answer "
                 f"the {tier} tier yet (missing: {', '.join(gap)}).\n"
                 "Refused before connecting: a command half-run against a "
                 "database is worse than one not run.")


def engine_problems(engine, tiers=("connect",)):
    """Everything wrong with an engine as a list of messages; [] when it
    declares its identity and answers every method of `tiers`. Engine
    packages assert this in their own suites."""
    problems = []
    for attr in IDENTITY:
        if not str(getattr(engine, attr, "") or "").strip():
            problems.append(f"{attr} is not declared")
    name = str(getattr(engine, "name", "") or "")
    if name and not re.fullmatch(r"[a-z][a-z0-9]*", name):
        problems.append(f"name {name!r} must be lower-case letters and digits "
                        "(it is a folder name)")
    prefix = str(getattr(engine, "env_prefix", "") or "")
    if prefix and not re.fullmatch(r"PYTHIA_[A-Z0-9]+", prefix):
        problems.append(f"env_prefix {prefix!r} must look like PYTHIA_PG")
    style = getattr(engine, "bind_style", None)
    if style and style not in BIND_STYLES:
        problems.append(f"bind_style {style!r} is not one of {BIND_STYLES}")
    for tier in tiers:
        gap = missing(engine, tier)
        if gap:
            problems.append(f"{tier} tier missing: {', '.join(gap)}")
    return problems

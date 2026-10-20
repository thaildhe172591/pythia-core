"""The query contract every engine package keeps.

Every statement an engine runs lives in its own `queries/` folder, one per
file, named binds only, with a three-line header (`-- Purpose:` /
`-- Binds:` / `-- Returns:`), and its binds declared in the engine's
QUERY_BINDS. `contract_problems` is the lint that holds a package to it, so
queries stay reviewable in a pull request without a database.

Two bind styles, because drivers differ: `named` (`:s`, python-oracledb and
most DB-API drivers) and `pyformat` (`%(s)s`, psycopg). A named-style driver
parses SQL, so a bind inside a comment or a string literal is not one. A
pyformat driver does not: it reads every `%` in the text, comments and
literals included, so the lint reads the text as the driver does, and a
literal percent sign is written `%%`. The danger is not only an error:
psycopg 3.3 runs `select 'a%b'` with an empty dict as `select 'a$1'`, and
says nothing. run_query always passes a dict, even
an empty one, so that rule holds in every file whether it has binds or not.
A driver that never turns `%%` back into `%` (pymssql) gets the file through
its engine's loader, which does it once; the files keep one contract.
"""
import re
import sys

HEADER_FIELDS = ("-- Purpose:", "-- Binds:", "-- Returns:")

_BIND_RE = {
    "named": re.compile(r"(?<![:\w]):([a-z_][a-z0-9_]*)", re.I),
    "pyformat": re.compile(r"%\(([a-z_][a-z0-9_]*)\)s", re.I),
}
_LONE_PERCENT = re.compile(r"%(?!\([a-z_][a-z0-9_]*\)s)", re.I)


def _strip(sql):
    """The statement without comments and string literals: a date format
    like 'hh24:mi:ss' is not two binds, and a comment may name one."""
    sql = re.sub(r"--[^\n]*", " ", sql)
    sql = re.sub(r"/\*.*?\*/", " ", sql, flags=re.S)
    return re.sub(r"'(?:[^']|'')*'", " ", sql)


def query_binds(sql, style="named"):
    """Bind names the driver will see in a statement."""
    text = _strip(sql) if style == "named" else sql
    return set(_BIND_RE[style].findall(text))


def load_query(query_dir, name):
    path = query_dir / name
    if not path.is_file():
        sys.exit(f"Missing query file: {path}")
    return path.read_text(encoding="utf-8")


def contract_problems(query_dir, declared, style):
    """Every drift between the files in query_dir and the declared binds,
    as messages; [] when the contract holds."""
    problems = []
    on_disk = {p.name for p in query_dir.glob("*.sql")}
    for name in sorted(on_disk - set(declared)):
        problems.append(f"{name} is on disk but not declared")
    for name in sorted(set(declared) - on_disk):
        problems.append(f"{name} is declared but not on disk")
    for name in sorted(on_disk & set(declared)):
        text = (query_dir / name).read_text(encoding="utf-8")
        for field in HEADER_FIELDS:
            if field not in text:
                problems.append(f"{name} is missing a '{field}' header line")
        used = query_binds(text, style)
        if used != set(declared[name]):
            problems.append(f"{name}: file uses {sorted(used)}, "
                            f"declared {sorted(declared[name])}")
        other = [s for s in _BIND_RE if s != style]
        for s in other:
            if query_binds(text, s):
                problems.append(f"{name} uses {s} binds; this engine binds "
                                f"{style}")
        if style == "pyformat" and _LONE_PERCENT.search(text.replace("%%", "")):
            problems.append(f"{name} has a lone '%' — the driver reads it as "
                            "a placeholder; write a literal percent as %%")
    return problems


def run_query(conn, sql, binds=None):
    """(column names, rows) for one statement on a DB-API connection."""
    with conn.cursor() as cur:
        cur.execute(sql, binds or {})
        cols = [d[0] for d in cur.description] if cur.description else []
        return cols, cur.fetchall() if cur.description else []

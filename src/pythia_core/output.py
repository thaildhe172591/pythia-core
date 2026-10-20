"""How results reach the person and the agent: windowed, and never cut
short without saying so.

Rule 7 of pythia holds here for every engine: if output lacks a truncation
marker, the reader saw everything; if it has one, the marker says how to
fetch the rest. List commands ask the engine for one row beyond the window
(`fetch_n`), so truncation is detected, never guessed.
"""
import json
import sys

INT_MAX = 2147483647


def cell(v):
    if v is None:
        return ""
    if hasattr(v, "read"):  # a LOB-like value
        v = v.read()
    return str(v).replace("\t", "    ")


def clip(rows, limit, offset=0):
    """Window a result; truncated=True whenever anything beyond it exists.
    limit 0 means no cap."""
    end = offset + limit if limit else len(rows)
    return rows[offset:end], len(rows) > end


def fetch_n(limit):
    """How many rows to ask for: one beyond the limit, so truncation shows."""
    return limit + 1 if limit else INT_MAX


def json_envelope(command, connection, schema, cols, rows, truncated, **extra):
    payload = {"ok": True, "command": command, "connection": connection,
               "schema": schema, "rows": [dict(zip(cols, r)) for r in rows],
               "truncated": bool(truncated), **extra}
    return json.dumps(payload, default=str, ensure_ascii=False)


def emit_table(ns, cols, rows, truncated, **extra):
    if ns.json:
        print(json_envelope(ns.command, ns.conn_name, ns.schema, cols, rows,
                            truncated, **extra))
        return
    print("\t".join(cols))
    for r in rows:
        print("\t".join(cell(v) for v in r))
    if truncated:
        print(f"-- truncated at {len(rows)} rows (raise --limit, or --limit 0 "
              "for no cap)")
    print(f"-- {len(rows)} rows", file=sys.stderr)


def format_source(rows, raw):
    """rows: (unit, line, text). Each unit is numbered from its own lines, so
    a number maps to what the database reports; a header names each unit
    when there is more than one (overloads, say)."""
    if raw:
        return "".join(cell(t).rstrip("\n") + "\n" for _, _, t in rows)
    multi = len({u for u, _, _ in rows}) > 1
    out, seen = [], None
    for unit, line, text in rows:
        if multi and unit != seen:
            if seen is not None:
                out.append("\n")
            out.append(f"-- {unit}\n")
            seen = unit
        out.append(f"{line:>6}  {cell(text).rstrip(chr(10))}\n")
    return "".join(out)


def emit_source(ns, rows, truncated, total):
    if ns.json:
        clean = [(u, ln, cell(t).rstrip("\n")) for u, ln, t in rows]
        print(json_envelope(ns.command, ns.conn_name, ns.schema,
                            ("UNIT", "LINE", "TEXT"), clean, truncated,
                            total_lines=total))
        return
    sys.stdout.write(format_source(rows, ns.raw))
    if truncated:
        print(f"-- truncated: showing {len(rows)} of {total} source lines (use "
              f"--offset {ns.offset + len(rows)} to continue, or --max-lines 0 "
              "for all)")


def emit_text(ns, text):
    """A document (DDL) as lines, windowed like source."""
    lines = text.split("\n")
    shown, truncated = clip(lines, ns.max_lines, ns.offset)
    if ns.json:
        print(json_envelope(ns.command, ns.conn_name, ns.schema, ("DDL",),
                            [("\n".join(shown),)], truncated,
                            total_lines=len(lines)))
        return
    print("\n".join(shown))
    if truncated:
        print(f"-- truncated: showing {len(shown)} of {len(lines)} lines (use "
              f"--offset {ns.offset + len(shown)} to continue, or --max-lines 0 "
              "for all)")


TREE_COLS = ("LEVEL", "SCHEMA", "NAME", "TYPE", "HOW")
TEXT_HOW = "text"       # matched by name in source text, not by the catalog


def render_tree(rows, root):
    """rows: (level, schema, name, type, how) in walk order. The same object
    can appear on more than one path — that is the graph, not a bug. A row
    the catalog did not vouch for is marked [text]."""
    out = [f"{root}\n"]
    for level, schema, name, otype, how in rows:
        mark = " [text]" if how == TEXT_HOW else ""
        out.append(f"{'  ' * int(level)}{schema}.{name} ({otype}){mark}\n")
    return "".join(out)


REVIEW_AT = 10          # pythia's rule 2: this many dependents goes to a person


def impact_summary(rows, schema):
    """Distinct objects (three paths to one object are one object), how many
    rest only on a text match, which live in other schemas, and whether the
    list must go to the developer before any code is written."""
    hows = {}
    for _level, owner, name, otype, how in rows:
        hows.setdefault((owner, name, otype), set()).add(how)
    text_only = sum(1 for h in hows.values() if h == {TEXT_HOW})
    other = sorted(f"{o}.{n}" for o, n, _ in hows if o != schema)
    return {"objects": len(hows), "text_matches": text_only,
            "other_schemas": other,
            "needs_review": len(hows) >= REVIEW_AT or bool(other)}


def summary_lines(s):
    n, text = s["objects"], s["text_matches"]
    line = f"-- impact: {n} dependent object{'' if n == 1 else 's'}"
    if text:
        count = "" if text == n == 1 else f"{text} "
        line += f" ({count}matched by name as text, not by the catalog)"
    out = [line]
    if s["other_schemas"]:
        out.append(f"-- {len(s['other_schemas'])} in other schemas: "
                   f"{', '.join(s['other_schemas'])}")
    if s["needs_review"]:
        out.append(f"! show this list to the developer before writing code — "
                   f"{REVIEW_AT} or more dependents, or one in another schema")
    return out


ERROR_COLS = ("NAME", "TYPE", "LINE", "POSITION", "SEVERITY", "MESSAGE")


def format_errors(rows):
    """rows: (name, type, line, position, severity, message), one header per
    object, line:position as the database reported them — `-` for a finding
    about the object as a whole."""
    out, seen = [], None
    for name, otype, line, pos, severity, message in rows:
        if (name, otype) != seen:
            out.append(f"{name} ({otype})\n")
            seen = (name, otype)
        where = "-" if line in (None, "") else (
            f"{line}:{pos}" if pos not in (None, "") else f"{line}")
        out.append(f"  {where} {severity} {cell(message).strip()}\n")
    return "".join(out)

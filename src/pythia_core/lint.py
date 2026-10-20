"""The lint framework: an engine's rules over a change's statements, with
the severities and naming patterns the team keeps in
`.pythia/<engine>/lint.json`. Ported from pythia's lint, engine-neutral.

A rule is a dict an engine's `lint_rules()` returns:

    {"id": "pg-security-definer-path", "severity": "error",
     "summary": "SECURITY DEFINER needs SET search_path",
     "check": check}          # (statement, group, run) -> [(line, message)]

`line` counts inside the statement; `run` is every statement of the change
being linted, so a rule can see across files (a COMMENT ON in one file
counts for the CREATE TABLE in another). A rule that crashes is reported
and never blocks: a lint bug must not stop a write.

lint.json is a trust boundary, like the policy: an unknown key, rule or
severity, or a pattern that does not compile, is refused with the accepted
spelling — never ignored.

    {"severity": {"pg-select-star": "error", "naming": "error"},
     "naming": {"FUNCTION": "^f_[a-z0-9_]+$", "VIEW": "^v_[a-z0-9_]+$"}}
"""
import json
import re
import sys

SEVERITIES = ("error", "warn", "info", "off")
NAMING = "naming"
NAMING_DEFAULT = "warn"
CONFIG = "lint.json"
KEYS = ("severity", "naming")


def path(state):
    return state / CONFIG


def rules(engine):
    return list(engine.lint_rules() or [])


def load(engine, state):
    """{"severity": {rule: severity}, "naming": {TYPE: (pattern, compiled)}}."""
    p = path(state)
    if not p.is_file():
        return {"severity": {}, "naming": {}}
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except ValueError as e:
        sys.exit(f"Cannot parse {p}: {e}")
    if not isinstance(raw, dict):
        sys.exit(f"{p} must hold an object with {', '.join(KEYS)}.")
    extra = [k for k in raw if k not in KEYS]
    if extra:
        sys.exit(f"{p} holds {', '.join(map(repr, extra))}; it takes "
                 f"{', '.join(KEYS)}.")
    known = [r["id"] for r in rules(engine)] + [NAMING]
    sev = raw.get("severity") or {}
    for rule, value in sev.items():
        if rule not in known:
            sys.exit(f"{p}: no lint rule {rule!r}. Rules: {', '.join(known)}.")
        if value not in SEVERITIES:
            sys.exit(f"{p}: severity of {rule!r} must be one of "
                     f"{', '.join(SEVERITIES)} — got {value!r}.")
    naming = {}
    for otype, pattern in (raw.get("naming") or {}).items():
        try:
            naming[str(otype).upper()] = (pattern, re.compile(str(pattern)))
        except re.error as e:
            sys.exit(f"{p}: the naming pattern for {otype} is not a valid "
                     f"pattern ({e}): {pattern!r}")
    return {"severity": dict(sev), "naming": naming}


def effective(engine, cfg):
    """(id, severity, summary) for every rule, the naming rule last."""
    out = [(r["id"], cfg["severity"].get(r["id"], r.get("severity", "warn")),
            r.get("summary", "")) for r in rules(engine)]
    out.append((NAMING, cfg["severity"].get(NAMING, NAMING_DEFAULT),
                "object names match the team's patterns in " + CONFIG))
    return out


def run(engine, cfg, statements):
    """Every finding over a change: statements are dicts with file, line,
    text and group (and n, in a release). Findings come back in statement
    order: {rule, severity, file, line, message, n}."""
    texts = [s["text"] for s in statements]
    sev = dict((rid, level) for rid, level, _ in effective(engine, cfg))
    findings = []
    for s in statements:
        def add(rule, at, message, level=None):
            findings.append({"rule": rule, "severity": level or sev[rule],
                             "file": s.get("file"), "line": s["line"] + at - 1,
                             "message": message, "n": s.get("n")})
        for rule in rules(engine):
            if sev[rule["id"]] == "off":
                continue
            try:
                hits = rule["check"](s["text"], s["group"], texts) or []
            except Exception as e:            # noqa: BLE001 — never blocks
                add(rule["id"], 1, f"lint rule crashed: {type(e).__name__}: {e}",
                    level="warn")
                continue
            for at, message in hits:
                add(rule["id"], at, message)
        if cfg["naming"] and sev[NAMING] != "off" and s["group"] == "source":
            try:
                ident = engine.parse_object(
                    engine.prepare_statement(s["text"], s["group"]), s["group"])
            except Exception:                 # noqa: BLE001 — a name we cannot read
                ident = None
            want = cfg["naming"].get(str((ident or {}).get("type", "")).upper())
            if ident and want and not want[1].fullmatch(str(ident["name"])):
                add(NAMING, 1, f"{ident['type']} {ident['name']} does not match "
                    f"the team's pattern {want[0]}")
    return findings


def errors(findings):
    return [f for f in findings if f["severity"] == "error"]


def summary(findings):
    counts = [(sum(1 for f in findings if f["severity"] == level), level)
              for level in ("error", "warn", "info")]
    words = {"error": "error", "warn": "warning", "info": "note"}
    return ", ".join(f"{n} {words[level]}{'s' if n != 1 else ''}"
                     for n, level in counts if n) or "clean"


def render(findings, indent="  "):
    return [f"{indent}{f['file'] or '-'}:{f['line']}  {f['severity']:<5}  "
            f"{f['rule']}  {f['message']}" for f in findings]

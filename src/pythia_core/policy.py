"""The write policy: which groups of statements may be written, and how.

Each engine sorts a statement into one group (classify); the policy says
allow, confirm or deny for each. allow means "no approval needed", so it is
only accepted for session, whose statements end with the connection. The
defaults are pythia's: code and comments need a person's approval, and
everything with no undo is denied until the developer says otherwise.
Config is a trust boundary: an unknown group or value is refused with the
accepted spelling, never ignored.
"""
import json
import sys

GROUPS = ("source", "comments", "data_dml", "structural", "grants", "session")
DEFAULTS = {"source": "confirm", "comments": "confirm", "data_dml": "deny",
            "structural": "deny", "grants": "deny", "session": "allow"}
VALUES = ("allow", "confirm", "deny")
RANK = {"deny": 0, "confirm": 1, "allow": 2}
# groups whose journal entry holds a real way back
UNDOABLE = ("source", "comments")


def path(state):
    return state / "policy.json"


def effective(raw):
    """{group: (value, where it came from)}"""
    eff = {g: (v, "default") for g, v in DEFAULTS.items()}
    if raw is not None and not isinstance(raw, dict):
        sys.exit("policy.json must hold an object of group: value.")
    for k, v in (raw or {}).items():
        if k not in DEFAULTS:
            sys.exit(f"Unknown policy group {k!r} in policy.json. "
                     f"Groups: {', '.join(GROUPS)}.")
        if v not in VALUES:
            sys.exit(f"Policy {k!r} must be allow, confirm or deny — got {v!r}.")
        if v == "allow" and k != "session":
            sys.exit(f"Policy {k!r} cannot be allow: allow is only for session. "
                     "A write that needs no approval is a write no person saw; "
                     "confirm is the loosest a writing group goes.")
        eff[k] = (v, "policy.json")
    return eff


def load(state):
    p = path(state)
    if not p.is_file():
        return effective(None)
    try:
        return effective(json.loads(p.read_text(encoding="utf-8")))
    except ValueError as e:
        sys.exit(f"Cannot parse {p}: {e}")


def save(state, eff):
    p = path(state)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({g: v for g, (v, _) in eff.items()}, indent=2) + "\n",
                 encoding="utf-8")
    return p

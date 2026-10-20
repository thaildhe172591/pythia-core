"""Autopilot: the developer delegating a session once instead of approving
every write. Ported from pythia.

The agent asks with `autopilot request`; the request is a card naming the
connection, the groups, how long and how many writes. Nothing is delegated
until the developer approves that card through any door (or runs `autopilot
on` at a console). While the lease is active, `apply` and `deploy` land what
it covers without a per-write approval — still snapshotted, rehearsed,
verified and journaled. Everything else takes the normal gate:

- another connection, or one marked protected (`"protected": true`, or an
  `"env"` of prod / production / prd / live) — read fresh on every write;
- a group the card did not name; a destructive statement, unless the card
  said so; an UPDATE or DELETE of more rows than a person should see first;
- a lease that expired, was spent, was revoked, or paused itself — it pauses
  when a write breaks something, or after repeated failures.

State lives in `.pythia/<engine>/autopilot/`. A missing, corrupt or
half-written lease reads as no lease: absence is never permission.
"""
import datetime
import json
import re
import secrets
import sys

from . import grants, policy

GROUPS_DEFAULT = ("source", "comments")
GROUPS_ALL = ("source", "comments", "structural", "data_dml", "grants")
HOURS_DEFAULT, HOURS_MAX = 8, 24
WRITES_DEFAULT, WRITES_MAX = 200, 2000
FAILS_TO_PAUSE = 5
DML_MAX_ROWS = 1000          # a bigger row set goes to a person first
PROTECTED_ENVS = {"prod", "production", "prd", "live"}
TOKEN_RE = re.compile(r"[0-9a-f]{8}")


def root(state):
    return state / "autopilot"


def lease_path(state):
    return root(state) / "lease.json"


def request_path(state, token):
    return root(state) / "requests" / f"{token}.json"


def _read(path):
    try:
        rec = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return rec if isinstance(rec, dict) else None


def _write(path, rec):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(rec, indent=2, default=str) + "\n", encoding="utf-8")


def _iso(dt):
    return dt.isoformat(timespec="seconds")


def _parse(text):
    try:
        return datetime.datetime.fromisoformat(str(text))
    except ValueError:
        return None


def read(state):
    return _read(lease_path(state))


def write(state, rec):
    _write(lease_path(state), rec)


def protected(entry):
    e = entry if isinstance(entry, dict) else {}
    return bool(e.get("protected")) or \
        str(e.get("env", "")).strip().lower() in PROTECTED_ENVS


def parse_groups(text):
    if not text:
        return list(GROUPS_DEFAULT)
    groups = []
    for g in str(text).replace(" ", "").split(","):
        if not g:
            continue
        if g == "all":
            groups.extend(GROUPS_ALL)
        elif g in GROUPS_ALL:
            groups.append(g)
        else:
            sys.exit(f"Autopilot cannot cover {g!r}. Groups: "
                     f"{', '.join(GROUPS_ALL)} (or all).")
    return sorted(set(groups), key=GROUPS_ALL.index)


def new_request(state, conn_name, entry, groups=None, hours=None,
                max_writes=None, destructive=False, note="", now=None):
    """A pending request: a card waiting for the developer's Approve."""
    if protected(entry):
        sys.exit(f"Refused: connection {conn_name!r} is marked protected. "
                 "Autopilot never covers it — every write there is approved "
                 "one at a time.")
    hours = HOURS_DEFAULT if hours is None else hours
    if not 0 < hours <= HOURS_MAX:
        sys.exit(f"--hours must be more than 0 and at most {HOURS_MAX}.")
    max_writes = WRITES_DEFAULT if max_writes is None else max_writes
    if not 0 < max_writes <= WRITES_MAX:
        sys.exit(f"--max-writes must be between 1 and {WRITES_MAX}.")
    now = now or datetime.datetime.now()
    req = {"kind": "lease-request", "token": secrets.token_hex(4),
           "connection": conn_name, "groups": list(groups or GROUPS_DEFAULT),
           "hours": hours, "max_writes": int(max_writes),
           "destructive": bool(destructive), "note": note or "",
           "requested_at": _iso(now),
           "request_expires_at": _iso(now + datetime.timedelta(
               minutes=grants.TTL_MINUTES))}
    _write(request_path(state, req["token"]), req)
    return req


def card(engine, state, req):
    """Every consequence of the lease, named."""
    eff = policy.load(state)
    notes = getattr(engine, "rollback_notes", {}) or {}
    span = f"{req['hours']:g} hour{'s' if req['hours'] != 1 else ''}"
    lines = [f"Autopilot on connection {req['connection']}: the agent applies "
             f"without asking, for {span} or {req['max_writes']} writes, "
             "whichever ends first."]
    for g in req["groups"]:
        extra = (" — policy says deny; this lease overrides it while active"
                 if eff[g][0] == "deny" else "")
        lines.append(f"  {g:<11} undo: {notes.get(g, '?')}{extra}")
    if req.get("destructive"):
        lines.append("  DESTRUCTIVE statements INCLUDED (DROP, TRUNCATE, "
                     "a DELETE without WHERE...) — none of them can be undone.")
    else:
        lines.append("  Destructive statements (DROP, TRUNCATE, a DELETE "
                     "without WHERE...) still need your approval each time.")
    lines.append(f"Every write still snapshots, rehearses, verifies and is "
                 f"journaled; an UPDATE or DELETE of more than {DML_MAX_ROWS} "
                 "rows comes to you first; a write that breaks something "
                 "pauses the lease.")
    if req.get("note"):
        lines.append(f"Task, as the agent described it: {req['note']}")
    lines.append(f"Requested {str(req['requested_at']).replace('T', ' ')}. "
                 f"Revoke any time: {engine.command} autopilot off")
    return lines


def approval_card(engine, state, token):
    req = _read(request_path(state, token)) if TOKEN_RE.fullmatch(str(token)) else None
    if not req:
        sys.exit(f"No pending autopilot request carries token {token!r}. Ask "
                 f"again:\n  {engine.command} autopilot request")
    exp = _parse(req.get("request_expires_at"))
    if exp is None or datetime.datetime.now() > exp:
        sys.exit(f"Autopilot request {token} expired before it was approved. "
                 f"Ask again:\n  {engine.command} autopilot request")
    return f"autopilot:{token}", req, card(engine, state, req)


def activate(engine, state, req, approver, session=None, now=None):
    """The developer approved the card: the lease starts now, and the one
    before it, if any, moves to history with its report."""
    now = now or datetime.datetime.now()
    old = read(state)
    if old and old.get("token"):
        _write(root(state) / "history" / f"{old['token']}.json", old)
    rec = {k: v for k, v in req.items() if k not in ("kind", "request_expires_at")}
    rec.update(kind="lease", approved_at=_iso(now), approver=approver,
               session=session, writes=0, fails_in_a_row=0, paused=None,
               revoked_at=None,
               expires_at=_iso(now + datetime.timedelta(hours=req["hours"])))
    write(state, rec)
    try:
        request_path(state, req["token"]).unlink()
    except OSError:
        pass
    return (f"autopilot ACTIVE on {rec['connection']} until "
            f"{rec['expires_at'][11:16]} — {', '.join(rec['groups'])}, up to "
            f"{rec['max_writes']} writes. Apply directly now: "
            f"{engine.command} apply <file> or {engine.command} deploy <files> "
            "(no --confirm while it lasts), and report every write: "
            f"{engine.command} autopilot report")


def status(rec, now=None):
    """active, paused, expired, spent, revoked — or None for no lease. An
    unreadable field ends the lease rather than extending it."""
    if not rec:
        return None
    if rec.get("revoked_at"):
        return "revoked"
    exp = _parse(rec.get("expires_at"))
    if exp is None or (now or datetime.datetime.now()) > exp:
        return "expired"
    try:
        if int(rec.get("writes", 0)) >= int(rec.get("max_writes", 0)):
            return "spent"
    except (TypeError, ValueError):
        return "spent"
    if rec.get("paused"):
        return "paused"
    if not rec.get("approved_at") or not rec.get("approver"):
        return "revoked"                 # a lease nobody approved is none
    return "active"


def line(rec, now=None):
    st = status(rec, now)
    if st is None:
        return "autopilot: off"
    text = (f"autopilot: {st.upper()} on {rec.get('connection')} — "
            f"{', '.join(rec.get('groups') or [])}"
            f"{' + destructive' if rec.get('destructive') else ''}; "
            f"{rec.get('writes', 0)}/{rec.get('max_writes')} writes; "
            f"until {str(rec.get('expires_at', '?'))[11:16]}")
    if st == "paused":
        text += f" — paused: {rec['paused'].get('reason')}"
    return text


def _same(a, b):
    return str(a or "").upper() == str(b or "").upper()


def names_group(state, conn_name, entry, group, now=None):
    """An active lease on this connection names the group: the developer's
    override of a deny for it, as the card said."""
    rec = read(state)
    return (status(rec, now) == "active" and _same(rec.get("connection"), conn_name)
            and not protected(entry) and group in (rec.get("groups") or []))


def covers(state, conn_name, entry, group, destructive=None, rows=None, now=None):
    """(covered, why not): whether the lease lands this write without a
    per-write approval. why is None when there is no lease at all."""
    rec = read(state)
    st = status(rec, now)
    if st is None:
        return False, None
    if st != "active":
        why = f"autopilot is {st}"
        if st == "paused":
            why += f" ({rec['paused'].get('reason')})"
        return False, why
    if not _same(rec.get("connection"), conn_name):
        return False, (f"autopilot covers connection {rec.get('connection')}, "
                       f"not {conn_name}")
    if protected(entry):
        return False, f"connection {conn_name} is marked protected — autopilot never covers it"
    if group not in (rec.get("groups") or []):
        return False, f"autopilot does not cover {group} statements"
    if destructive and not rec.get("destructive"):
        return False, (f"destructive — {destructive}; the lease leaves that to "
                       "the developer, one approval at a time")
    if rows is not None and rows > DML_MAX_ROWS:
        return False, (f"{rows} rows — more than {DML_MAX_ROWS} rows go to the "
                       "developer first")
    return True, None


def note_write(state, ok, broken=0, entry=None, written=True, count=1, now=None):
    """Count writes, and pause when the evidence says a person should look:
    something broke, or failures keep repeating."""
    rec = read(state)
    if not rec:
        return None
    now = now or datetime.datetime.now()
    if written:
        rec["writes"] = int(rec.get("writes", 0)) + count
    rec["fails_in_a_row"] = 0 if ok else int(rec.get("fails_in_a_row", 0)) + 1
    if broken:
        rec["paused"] = {"reason": f"a write broke {broken} object(s)",
                         "entry": entry, "at": _iso(now)}
    elif rec["fails_in_a_row"] >= FAILS_TO_PAUSE:
        rec["paused"] = {"reason": f"{rec['fails_in_a_row']} failed writes in a row",
                         "entry": entry, "at": _iso(now)}
    write(state, rec)
    return rec


def revoke(state, now=None):
    rec = read(state)
    if not rec or rec.get("revoked_at"):
        return None
    rec["revoked_at"] = _iso(now or datetime.datetime.now())
    write(state, rec)
    return rec


def pending(state):
    d = root(state) / "requests"
    return sorted(p.stem for p in d.glob("*.json")) if d.is_dir() else []

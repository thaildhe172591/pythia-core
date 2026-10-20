"""A developer's approval, on disk: single use, short-lived, bound to the
connection the preview ran on. The token proves the content did not move;
the grant proves a person approved it. Ported from pythia.

The grant file is a trust boundary: a token that is not 6-10 lowercase
letters and digits never names a file, and a corrupt or half-written grant
reads as absent, which refuses.
"""
import datetime
import json
import re

TTL_MINUTES = 15
TOKEN_RE = re.compile(r"[0-9a-z]{6,10}")


def valid_token(token):
    return bool(TOKEN_RE.fullmatch(str(token or "")))


def root(state):
    return state / "grants"


def path(state, token):
    if not valid_token(token):
        raise ValueError(f"not a pythia token: {token!r}")
    return root(state) / f"{token}.json"


def mint(state, token, conn_name, now=None, revalidate=None, approver="console",
         session=None):
    now = now or datetime.datetime.now()
    rec = {"token": token,
           "conn": conn_name or "",
           "minted_at": now.isoformat(timespec="seconds"),
           "expires_at": (now + datetime.timedelta(
               minutes=TTL_MINUTES)).isoformat(timespec="seconds"),
           "used_at": None,
           "revalidate": revalidate or None,
           "approver": approver,
           "session": session}
    root(state).mkdir(parents=True, exist_ok=True)
    path(state, token).write_text(json.dumps(rec, indent=2) + "\n",
                                  encoding="utf-8")
    return rec


def read(state, token):
    if not valid_token(token):
        return None
    p = path(state, token)
    if not p.is_file():
        return None
    try:
        rec = json.loads(p.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return None
    return rec if isinstance(rec, dict) else None


def status(grant, conn_name, now=None):
    """ok, missing, expired, spent or wrong_conn — each refused with its own
    sentence, because "no" without the reason teaches nothing."""
    if not grant:
        return "missing"
    if grant.get("used_at"):
        return "spent"
    now = now or datetime.datetime.now()
    try:
        expires = datetime.datetime.fromisoformat(str(grant.get("expires_at")))
    except ValueError:
        return "expired"                    # an unreadable stamp is no licence
    if now > expires:
        return "expired"
    # DEV and dev are the same connection; an approval must not hinge on case
    if str(grant.get("conn", "")).upper() != str(conn_name or "").upper():
        return "wrong_conn"
    return "ok"


def spend(state, token, now=None):
    """One write per approval, stamped in the run that wrote."""
    rec = read(state, token)
    if not rec:
        return
    rec["used_at"] = (now or datetime.datetime.now()).isoformat(timespec="seconds")
    path(state, token).write_text(json.dumps(rec, indent=2) + "\n",
                                  encoding="utf-8")


def prune(state, now=None):
    """Expired and unreadable grants are litter, not state: approve sweeps
    them on the way past."""
    now = now or datetime.datetime.now()
    d = root(state)
    if not d.is_dir():
        return 0
    gone = 0
    for p in sorted(d.glob("*.json")):
        rec = read(state, p.stem)
        if rec is None or status(rec, rec.get("conn"), now=now) == "expired":
            try:
                p.unlink()
                gone += 1
            except OSError:
                pass
    return gone

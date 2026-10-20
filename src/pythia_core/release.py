"""deploy: a release — one or more scripts, many statements — through the
gate as ONE transaction, under one approval. For engines whose DDL runs in a
transaction this is stronger than pythia's deploy on Oracle, where each
statement commits on its own:

- the whole release is rehearsed in one transaction, each statement
  snapshotted, measured and verified at its own point — so an UPDATE of a
  column the release adds is rehearsed against that column — then rolled
  back;
- one card shows every statement in order, and one approval covers them;
- the confirm runs them again in one transaction and commits only if every
  statement still matches what was approved — its token (the object as it
  stood, its rows), its row count, nothing newly broken. Anything else rolls
  the whole release back: all of it lands, or none of it.

What cannot be placed is refused before anything runs: a statement the
engine cannot classify, a session setting (it would change how every later
statement runs), a group the policy denies, a script edited since the
approved preview.
"""
import datetime
import hashlib
import json
import pathlib
import re
import sys

from . import gate, grants, journal, lease, policy
from .human import human_at_the_keyboard
from .output import impact_summary, summary_lines

TOKEN_RE = re.compile(r"[0-9a-f]{10}")


def root(state):
    return state / "deploys"


def batch_path(state, token):
    return root(state) / f"{token}.json"


def read_batch(state, token):
    """The batch a token names, or None: a token that is not 10 hex never
    names a file, and a corrupt record reads as absent."""
    if not TOKEN_RE.fullmatch(str(token or "")):
        return None
    try:
        rec = json.loads(batch_path(state, token).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return rec if isinstance(rec, dict) else None


def write_batch(state, rec):
    path = batch_path(state, rec["token"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(rec, indent=2, default=str) + "\n", encoding="utf-8")


def sha(text):
    return hashlib.sha256(text.replace("\r\n", "\n").encode("utf-8")).hexdigest()


def batch_token(conn_name, schema, files, member_tokens):
    seed = "\n".join([conn_name or "", schema or "", *files, *member_tokens])
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()[:10]


# --- reading the release ------------------------------------------------------------

def collect(engine, paths):
    """Every statement of the scripts, in order: {n, file, line, text, group}.
    Exits on a file that cannot be read or a release with no statement."""
    out = []
    for name in paths:
        path = pathlib.Path(name)
        if not path.is_file():
            sys.exit(f"No such file: {path}")
        text = path.read_text(encoding="utf-8")
        for line, stmt in engine.split_script(text):
            group, why = engine.classify(stmt)
            out.append({"n": len(out) + 1, "file": str(name), "line": line,
                        "text": stmt, "group": group, "why": why})
    if not out:
        sys.exit("Nothing to deploy: the scripts hold no statement.")
    return out


def _first_line(text):
    return str(text).strip().splitlines()[0][:80] if str(text).strip() else ""


def preflight(engine, state, statements, lifted=lambda group: False):
    """Refuse, before anything connects or runs, what the gate cannot place.
    `lifted(group)` is true for a group an active lease names."""
    bad = [s for s in statements if s["group"] is None]
    if bad:
        sys.exit("Refused before anything ran — these statements cannot go "
                 "through the gate:\n" + "\n".join(
                     f"  {s['file']}:{s['line']} {_first_line(s['text'])}\n"
                     f"    {s['why']}" for s in bad[:10]))
    sets = [s for s in statements if s["group"] == "session"]
    if sets:
        sys.exit("Refused before anything ran — a session setting in a release "
                 "would change how every later statement runs, which no "
                 "preview of a single statement shows:\n" + "\n".join(
                     f"  {s['file']}:{s['line']} {_first_line(s['text'])}"
                     for s in sets[:10]) + "\nSet it for the connection instead "
                 "(the role's defaults), or apply it alone.")
    eff = policy.load(state)
    denied = [s for s in statements if eff[s["group"]][0] == "deny"
              and not lifted(s["group"])]
    if denied:
        groups = sorted({s["group"] for s in denied})
        sys.exit(f"Refused before anything ran: {', '.join(groups)} "
                 f"{'is' if len(groups) == 1 else 'are'} set to deny, and the "
                 "release holds:\n" + "\n".join(
                     f"  {s['file']}:{s['line']} [{s['group']}] "
                     f"{_first_line(s['text'])}" for s in denied[:10])
                 + "\nThe developer decides: " + ", ".join(
                     f"`{engine.command} policy set {g} confirm`" for g in groups))


# --- one pass over the release, inside one transaction ---------------------------------

def run_members(engine, conn, ns, statements, then, batch=None):
    """Snapshot, measure, rehearse and verify every statement in order, in
    the open transaction; never commits or rolls back itself. Returns the
    members, or ({...}, error) for the first statement that failed."""
    state, schema = ns.state, ns.schema
    members = []
    for st in statements:
        stmt = engine.prepare_statement(st["text"], st["group"])
        ident = engine.parse_object(stmt, st["group"])
        where = f"statement {st['n']} ({st['file']}:{st['line']})"
        if ident and ident.get("schema") and ident["schema"] != schema:
            return members, (f"{where} names schema {ident['schema']!r}, but "
                             f"this connection targets {schema!r} — writing "
                             "across schemas hides which object changes.")
        snap = engine.snapshot(conn, schema, st["group"], ident) if ident else {}
        probe = engine.dml_probe(stmt) if st["group"] == "data_dml" else None
        row_set = engine.measure_row_set(conn, *probe) if probe else None
        otype = ident["type"] if ident else st["group"].upper()
        name = ident["name"] if ident else "STATEMENT"
        meta = {"schema": schema, "connection": ns.conn_name, "group": st["group"],
                "applied": False, "row_set": row_set, "then": then,
                "release": {"n": st["n"], "file": st["file"], "line": st["line"]},
                "batch": batch}
        # the snapshot is on disk before the statement runs
        eid = journal.write_entry(state, otype, name, "\n\n".join(snap.values()),
                                  st["text"], meta)
        summary = None
        if st["group"] == "source" and snap:
            found = engine.dependents(conn, schema, ident["name"], ns.depth)
            if found is not None:
                summary = impact_summary(found[0], schema)
        outcome = engine.rehearse(conn, schema, st["group"], stmt, ident, snap)
        if outcome.get("error"):
            journal.update_entry(state, eid, refused=True, error=outcome["error"])
            return members, f"{where}: {outcome['error']}"
        obj = outcome.get("object") or name
        otype = outcome.get("type") or otype
        before = snap.get(obj, "") if ident else ""
        token = gate.apply_token(otype, obj, st["text"],
                                 before + gate.fingerprint_text(row_set))
        rehearsal = {"verified": bool(outcome.get("verified")),
                     "blind": outcome.get("blind"),
                     "broken": gate._findings(outcome.get("broken")),
                     "own_errors": gate._findings(outcome.get("own_errors")),
                     "rows": outcome.get("rows"),
                     "notes": list(outcome.get("notes") or [])}
        after = outcome.get("after") or stmt
        restore = (engine.restore_statement(st["group"], ident, before, outcome)
                   if st["group"] in policy.UNDOABLE else "")
        created = st["group"] == "source" and obj not in snap
        hurt = engine.destructive_reason(stmt, st["group"])
        journal.update_entry(state, eid, before=before if ident else "",
                             after=after, restore=restore, object=obj, type=otype,
                             token=token, created=created, rehearsal=rehearsal,
                             destructive=hurt, summary=summary)
        members.append({
            "n": st["n"], "file": st["file"], "line": st["line"],
            "group": st["group"], "object": obj, "type": otype, "token": token,
            "entry": eid, "created": created,
            "changed": gate.render_diff(before, after)[1] if ident else None,
            "summary": summary, "destructive": hurt, "row_set": row_set,
            "rehearsal": rehearsal, "sha": sha(st["text"]),
            "statement": None if st["group"] == "source" else stmt})
    return members, None


# --- the card ---------------------------------------------------------------------------------

def batch_card(rec):
    """What a person approves for a release: every statement in order — in
    full for every group without a snapshot, with its rows for DML — and what
    the rehearsal of the whole found."""
    ms = rec["members"]
    lines = [f"Approving a release of {len(ms)} statement{'s' if len(ms) != 1 else ''} "
             f"on {rec.get('schema')}, in one transaction — all or nothing:"]
    for m in ms:
        if m["group"] == "source":
            what = (" — a new object" if m.get("created") else
                    f" — {m['changed']} lines changed" if m.get("changed") is not None
                    else "")
            lines.append(f"  {m['n']:>2}. [source] {m['object']} ({m['type']}){what}"
                         f"   {m['file']}:{m['line']}")
            if m.get("summary"):
                lines += [f"      {ln.lstrip('- ')}" for ln in summary_lines(m["summary"])]
        else:
            lines.append(f"  {m['n']:>2}. [{m['group']}]   {m['file']}:{m['line']}")
            lines += [f"      {ln}" for ln in str(m.get("statement") or "").splitlines()]
            if m.get("row_set"):
                lines += gate.render_row_set(m["row_set"], indent="      ")
        if m.get("destructive"):
            lines.append(f"      ! DESTRUCTIVE: {m['destructive']}.")
    broken = [(m["n"], b) for m in ms for b in
              m["rehearsal"]["own_errors"] + m["rehearsal"]["broken"]]
    blind = [(m["n"], m["rehearsal"]["blind"]) for m in ms
             if not m["rehearsal"]["verified"] and m["rehearsal"]["blind"]]
    if broken:
        lines.append("Rehearsed: this BREAKS — " + "; ".join(
            f"statement {n}: {b[0]}: {b[2]}" for n, b in broken))
    elif blind:
        lines.append("Rehearsed: ran and rolled back as one; NOT VERIFIED — "
                     + "; ".join(f"statement {n}: {why}" for n, why in blind[:3]))
    else:
        lines.append("Rehearsed: ran and rolled back as one; nothing broke.")
    no_undo = sorted({m["group"] for m in ms} - set(policy.UNDOABLE))
    if no_undo:
        lines.append(f"No snapshot can undo {', '.join(no_undo)} after commit; "
                     f"{' and '.join(policy.UNDOABLE)} are restorable per statement.")
    lines.append(f"Previewed {str(rec['created_at']).replace('T', ' ')} on "
                 f"connection {rec.get('connection') or '?'}.")
    return lines


def approval_card(engine, state, token):
    rec = read_batch(state, token)
    if not rec:
        sys.exit(f"No pending preview carries token {token!r}.\nApprove what "
                 "you have seen: preview first, then approve the token it "
                 f"prints:\n  {engine.command} deploy <files>")
    if rec.get("applied"):
        sys.exit(f"Release {token} was already applied — one approval, one "
                 f"run.\nPreview again for a fresh token:\n  {rec.get('then')}")
    return f"deploy:{token}", rec, batch_card(rec)


# --- the command ------------------------------------------------------------------------------

def run_deploy(engine, conn, ns):
    state, cmd = ns.state, engine.command
    then = f"{cmd} deploy {' '.join(ns.paths)}"
    statements = collect(engine, ns.paths)
    entry = getattr(ns, "conn_entry", None)
    preflight(engine, state, statements, lambda g: lease.names_group(
        state, ns.conn_name, entry, g))
    if ns.yes and not human_at_the_keyboard(engine):
        sys.exit(gate.headless_yes(engine))
    confirm = str(ns.confirm or "").strip().lower() or None
    approved = None
    if confirm:
        approved = _check_approval(engine, state, ns, confirm, statements, then)
    writing = bool(confirm or ns.yes)

    members, error = run_members(engine, conn, ns, statements, then,
                                 batch=confirm)
    if error:
        conn.rollback()
        if writing:
            sys.exit(f"FAILED — {error}\nThe release rolled back as one: nothing "
                     "was written, and the approval is unspent. Fix the cause and "
                     "run the same command again, or fix the scripts and preview "
                     "again.")
        sys.exit(f"Preview stopped — {error}\nNothing was written: the release "
                 "is rehearsed as one, so there is nothing to approve.")
    token = batch_token(ns.conn_name, ns.schema, list(ns.paths),
                        [m["token"] for m in members])
    auto_why = None
    if not writing:
        for m in members:
            covered, why = lease.covers(state, ns.conn_name, entry, m["group"],
                                        m["destructive"],
                                        m["row_set"]["count"] if m["row_set"] else None)
            if not covered:
                auto_why = why and f"statement {m['n']}: {why}"
                break
        else:
            writing = "autopilot"

    if approved is not None:
        moved = _moved(approved, members)
        if moved:
            conn.rollback()
            for m in members:
                journal.update_entry(state, m["entry"], refused=True, error=moved)
            sys.exit(f"Rolled back: {moved}\nNothing was written — the release "
                     f"rolls back as one. Preview again:\n  {then}")
    if not writing:
        conn.rollback()
        rec = {"kind": "release", "token": token, "connection": ns.conn_name,
               "schema": ns.schema, "files": list(ns.paths), "members": members,
               "then": then, "applied": False,
               "created_at": datetime.datetime.now().isoformat(timespec="seconds")}
        write_batch(state, rec)
        for m in members:
            journal.update_entry(state, m["entry"], batch=token)
        _print_preview(engine, ns, rec, auto_why)
        return 0

    conn.commit()
    if confirm:
        grants.spend(state, confirm)
        approved["applied"] = True
        write_batch(state, approved)
    via = "grant" if confirm else "autopilot" if writing == "autopilot" else "yes"
    tag = lease.read(state).get("token") if via == "autopilot" else None
    for m in members:
        journal.update_entry(state, m["entry"], applied=True, confirmed_via=via,
                             batch=confirm or token, autopilot=tag)
    code = _report(engine, ns, members)
    if via == "autopilot":
        broken = sum(len(m["rehearsal"]["broken"]) + len(m["rehearsal"]["own_errors"])
                     for m in members)
        after = lease.note_write(state, not broken, broken, members[-1]["entry"],
                                 count=len(members))
        if not ns.json:
            print(f"\n  Landed under the developer's autopilot lease — {lease.line(after)}")
    return code


def _check_approval(engine, state, ns, confirm, statements, then):
    """Everything about the approval, checked before anything runs."""
    if not TOKEN_RE.fullmatch(confirm):
        sys.exit(f"Not a release token: {confirm!r} — a deploy preview prints 10 "
                 "hex digits.")
    grant = grants.read(state, confirm)
    st = grants.status(grant, ns.conn_name)
    if st != "ok":
        sys.exit(gate.grant_refusal(engine, st, confirm, grant, ns.conn_name, then))
    rec = read_batch(state, confirm)
    if not rec:
        sys.exit(f"No release preview carries token {confirm}. Preview first:\n"
                 f"  {then}")
    if rec.get("applied"):
        sys.exit(f"Release {confirm} was already applied — one approval, one "
                 f"run. Preview again:\n  {then}")
    if list(rec.get("files") or []) != list(ns.paths):
        sys.exit("The approved release lists different files:\n  "
                 + " ".join(rec.get("files") or []) + "\nNothing ran. Preview "
                 "again with the files you mean to run.")
    shas = [sha(s["text"]) for s in statements]
    approved = [m.get("sha") for m in rec["members"]]
    if shas != approved:
        first = next((i + 1 for i, (a, b) in enumerate(zip(shas, approved))
                      if a != b), min(len(shas), len(approved)) + 1)
        sys.exit(f"The scripts changed since that preview (statement {first} "
                 "first). Nothing ran. Preview again:\n  " + then)
    return rec


def _moved(rec, members):
    """Why this run is not the approved one, or None."""
    for old, new in zip(rec["members"], members):
        n = new["n"]
        if new["token"] != old["token"]:
            rows = old.get("row_set")
            if rows and new.get("row_set") and \
                    gate.fingerprint_text(rows) != gate.fingerprint_text(new["row_set"]):
                return (f"statement {n}: its row set moved — {rows['count']} rows "
                        f"when it was approved, {new['row_set']['count']} now.")
            return (f"statement {n}: {new['object']} changed since that preview — "
                    "what would be written is not what was approved.")
        if new["row_set"] is not None and new["rehearsal"]["rows"] != new["row_set"]["count"]:
            return (f"statement {n}: it touched {new['rehearsal']['rows']} rows, but "
                    f"{new['row_set']['count']} were approved.")
        seen = {tuple(b) for b in old["rehearsal"].get("broken", [])}
        fresh = [b for b in new["rehearsal"]["broken"] if tuple(b) not in seen]
        if fresh:
            return (f"statement {n}: the verification moved since the approved "
                    "preview — these break now and did not then: " + "; ".join(
                        f"{b[0]} ({b[1]}): {b[2]}" for b in fresh))
    return None


def _print_preview(engine, ns, rec, auto_why=None):
    if ns.json:
        print(json.dumps({"ok": True, "batch": rec["token"], "members": rec["members"],
                          "will_apply": False}, default=str))
        return
    print(f"\n  deploy preview — nothing was written. {len(rec['members'])} "
          f"statements from {len(rec['files'])} file(s).\n")
    for ln in batch_card(rec):
        print(f"  {ln}")
    for m in rec["members"]:
        if m["group"] != "source":
            continue
        e = journal.read_entry(ns.state, m["entry"])
        diff = gate.render_diff(e["before"], e["after"])[0].splitlines()
        print(f"\n  {m['n']}. diff {m['object']}:")
        for ln in diff[:40]:
            print(f"    {ln}")
        if len(diff) > 40:
            print(f"    -- truncated: {len(diff) - 40} more lines in "
                  f"{journal.root(ns.state) / m['entry']}")
    print()
    if auto_why:
        print(f"  ! not landed under autopilot — {auto_why}; the release needs "
              "its own approval.")
    for ln in gate.door_lines(engine, rec["token"], rec["then"]):
        print(f"  {ln}")


def _report(engine, ns, members):
    broken = [(m["n"], b) for m in members for b in
              m["rehearsal"]["own_errors"] + m["rehearsal"]["broken"]]
    undo = [f"{engine.command} journal restore {m['entry']}" for m in members
            if m["group"] in policy.UNDOABLE]
    code = 3 if broken else 0
    if ns.json:
        print(json.dumps({"ok": not broken, "applied": True,
                          "members": [{"n": m["n"], "object": m["object"],
                                       "group": m["group"], "entry": m["entry"]}
                                      for m in members],
                          "broken": broken, "restore": list(reversed(undo)),
                          "exit": code}, default=str))
        return code
    print(f"\n  Applied a release of {len(members)} statements in one transaction.")
    for m in members:
        print(f"  {m['n']:>3}. [{m['group']}] {m['object']}   {m['file']}:{m['line']}")
    if broken:
        print("\n  BROKEN (exit 3) — written, but these do not work:")
        for n, b in broken:
            print(f"    statement {n}: {b[0]} ({b[1]}): {b[2]}")
    if undo:
        print("\n  To undo what can be undone, newest first:")
        for u in reversed(undo):
            print(f"    {u}")
    rest = sorted({m["group"] for m in members} - set(policy.UNDOABLE))
    if rest:
        print(f"  {', '.join(rest)} statements have no undo.")
    return code

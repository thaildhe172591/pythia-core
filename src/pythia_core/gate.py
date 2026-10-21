"""The write gate: pythia's six steps, for an engine whose DDL runs inside a
transaction (PostgreSQL, SQL Server).

  1. SNAPSHOT   what the statement would replace, on disk before anything runs
  2. IMPACT     what depends on it (the read tier's dependents)
  3. REHEARSE   run it inside a transaction and verify — then roll back. The
                person approves a result that was observed, not predicted.
  4. PREVIEW    the diff, the rehearsal, the rows, the warnings, the token
  5. COMMIT     only on --confirm with a person's grant (or --yes at a real
                console): run it again, and commit only if it still matches
                what was approved — same object state, same rows, nothing
                newly broken that the preview did not show
  6. REPORT     exit 0 clean · 3 written but broken, with the restore command

A refusal is exit 1 and touches nothing it does not have to: a denied group
is refused before any read; an approval is checked before anything runs.

A statement the engine cannot rehearse — MongoDB runs a validator, index or
view change outside any transaction — is PREDICTED in step 3 instead:
nothing runs, and the preview says so. Step 5 then runs it once, after the
approval and the same checks, and the engine reports what landed; anything
unlike the prediction is reported, and exits 3 (the predict tier).
"""
import difflib
import hashlib
import json
import sys

from . import grants, journal, lease, lint, policy
from .engine import missing
from .human import ci_variable, console_session, human_at_the_keyboard
from .output import impact_summary, summary_lines


def apply_token(otype, name, file_text, db_state):
    """6 hex characters binding the write to what was previewed: the file or
    the database moving since the preview yields another token, so what gets
    applied is exactly what was seen. A consistency check, not a secret."""
    payload = "\n".join([otype, name, file_text.replace("\r\n", "\n"),
                         db_state.replace("\r\n", "\n")])
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:6]


def fingerprint_text(row_set):
    """What the token hashes of a row set; empty when there is none."""
    if not row_set:
        return ""
    return f"rows={row_set['count']} hash={row_set['hash']}"


def render_diff(before, after):
    lines = list(difflib.unified_diff(before.splitlines(), after.splitlines(),
                                      lineterm=""))
    changed = sum(1 for ln in lines
                  if ln[:1] in "+-" and not ln.startswith(("+++", "---")))
    return "\n".join(lines), changed


def render_row_set(row_set, indent="  "):
    n = row_set["count"]
    where = f" where {row_set['predicate']}" if row_set.get("predicate") else ""
    out = [f"{indent}{n} rows in {row_set['target']}{where}"]
    if n > 1000:
        out.append(f"{indent}! more rows than anyone can read one by one — the "
                   "count and the fingerprint are the whole of what you are "
                   "approving")
    if row_set.get("sample"):
        out.append(indent + "\t".join(map(str, row_set["columns"])))
        out.extend(indent + "\t".join(map(str, r)) for r in row_set["sample"])
        if row_set.get("truncated"):
            out.append(f"{indent}-- truncated at {len(row_set['sample'])} of {n} rows")
    return out


def headless_yes(engine):
    return ("--yes is the developer's flag, and no terminal is attached to this "
            "session.\nAgents preview, relay the preview verbatim, STOP, and "
            "pass --confirm <token> only after the developer has approved it.\n"
            f"(Real pipelines set {ci_variable(engine)}=1.)")


def has_doors(engine):
    """The chat doors need `install`: a hook that reads the AskUserQuestion
    answer, an MCP approver registered with the client. An engine without
    the harness tier has neither, so only the terminal door can approve."""
    return not missing(engine, "harness")


def approve_headless(engine):
    from .doors import mcp_names
    if not has_doors(engine):
        return (f"approve is the developer's command — it needs a real console.\n"
                "The developer runs it in their own terminal:\n"
                f"  {engine.command} approve <token>")
    return (f"approve is the developer's command — it needs a real console.\n"
            "From chat, the developer approves through a door instead — the "
            "first that works, the next unasked if one fails: AskUserQuestion "
            f"with the text of `{engine.command} approve --card <token>` "
            f"(Claude Code), then the {mcp_names(engine)[1]} tool (MCP), then "
            f"their own terminal:\n  {engine.command} approve <token>")


def door_lines(engine, token, then, confirm=True):
    from .doors import mcp_names
    if not has_doors(engine):
        return ["To apply, the developer approves in their own terminal:",
                f"  {engine.command} approve {token}",
                f"then the agent:  {then}" + (f" --confirm {token}" if confirm else "")]
    return ["To apply, the developer approves — the first door that works, the "
            "next unasked if one fails:",
            f"  1. AskUserQuestion (Claude Code), its text exactly the output of "
            f"`{engine.command} approve --card {token}`, options Approve / Reject",
            f"  2. the {mcp_names(engine)[1]} tool (MCP), with token {token}",
            f"  3. the developer's own terminal:  {engine.command} approve {token}",
            f"  then the agent:  {then}" + (f" --confirm {token}" if confirm else "")]


def grant_refusal(engine, st, token, grant, conn_name, then):
    """Why this write is refused, and the exact way to fix it."""
    if st == "missing":
        return ("The confirmation token matches, but no developer approval is "
                "on file.\n" + "\n".join(door_lines(engine, token, then))
                + "\nThen re-run this command.")
    if st == "expired":
        return (f"That approval expired ({grants.TTL_MINUTES} minutes after it "
                f"was given).\nAsk the developer to approve again:\n"
                f"  {engine.command} approve {token}")
    if st == "spent":
        return ("That approval was already used — one approval, one write.\n"
                f"Preview again and ask for the new token:\n  {then}")
    if st == "wrong_conn":
        return (f"That approval was given on connection {grant.get('conn')!r}, "
                f"but this session targets {conn_name!r}.\nApproving on one "
                "database does not approve another. On the right connection:\n"
                f"  {then} --conn {conn_name}")
    return "Refused: the developer approval on file is not valid."


def _token_mismatch(state, token, row_set, then):
    base = ("The confirmation token does not match: the file or the database "
            "object changed since that preview. Nothing was written. Preview "
            f"again:\n  {then}")
    _, meta = journal.find_by_token(state, token)
    if (meta or {}).get("applied"):
        return (f"Token {token} was already applied — one approval, one write.\n"
                f"Preview again, then get the new token approved:\n  {then}")
    then_rows = (meta or {}).get("row_set") or {}
    if not row_set or not then_rows or \
            fingerprint_text(then_rows) == fingerprint_text(row_set):
        return base
    if then_rows["count"] != row_set["count"]:
        moved = (f"{then_rows['count']} rows when you approved it, "
                 f"{row_set['count']} now")
    else:
        moved = (f"still {row_set['count']} rows, but not the same ones — the "
                 "row fingerprint changed")
    return ("Refused: the row set moved — " + moved + ".\nNothing was written. "
            "The approval covered the rows as they were.\nPreview again to see "
            f"them as they are now:\n  {then}")


def _findings(rows):
    return [list(map(str, r)) for r in (rows or [])]


def run_apply(engine, conn, ns, text, origin=None, then=None):
    """The six steps. Returns the exit code; refusals raise SystemExit."""
    state, schema, cmd = ns.state, ns.schema, engine.command
    then = then or f"{cmd} apply {ns.file}"
    group, why = engine.classify(text)
    if group is None:
        sys.exit(why)
    action = policy.load(state)[group][0]
    entry = getattr(ns, "conn_entry", None)
    if action == "deny" and lease.names_group(state, ns.conn_name, entry, group):
        action = "confirm"          # the lease card named it; it lasts while the lease does
    if action == "deny":
        extra = ("no snapshot can undo it after commit"
                 if group not in policy.UNDOABLE else "policy forbids it")
        sys.exit(f"Refused: {group} is set to deny — {extra}.\nTo allow it once "
                 f"you have weighed that: {cmd} policy set {group} confirm")
    if ns.yes and not human_at_the_keyboard(engine):
        sys.exit(headless_yes(engine))
    stmt = engine.prepare_statement(text, group)
    findings = lint_one(engine, state, ns.file, text, group)

    if group == "session":
        out = engine.rehearse(conn, schema, group, stmt, None, {})
        if out.get("error"):
            conn.rollback()
            sys.exit(f"FAILED — {out['error']}")
        conn.commit()
        print("Session setting applied — it lasts only for this invocation's "
              "connection, which is now over.")
        return 0

    ident = engine.parse_object(stmt, group)
    if ident and ident.get("schema") and ident["schema"] != schema:
        sys.exit(f"The statement names schema {ident['schema']!r} but this "
                 f"connection targets {schema!r}. Refused: writing across "
                 "schemas hides which object actually changes.\nUse --conn to "
                 f"select a connection for {ident['schema']!r}.")
    current = engine.current_schema(conn)
    if current is not None and current != schema:
        sys.exit(f"Refused: this session's schema is {current!r}, but connection "
                 f"{ns.conn_name!r} is configured for {schema!r}.\nAn unqualified "
                 f"name would land in {current!r} while pythia snapshots and "
                 f"verifies {schema!r}. Set \"schema\": \"{current}\" for this "
                 "connection, or fix the session's search path, then preview "
                 "again.")

    # the approval is checked before anything runs
    confirm = str(ns.confirm or "").strip().lower() or None
    grant, via = None, None
    if confirm:
        if not grants.valid_token(confirm):
            sys.exit(f"Not a pythia token: {confirm!r}.")
        grant = grants.read(state, confirm)
        st = grants.status(grant, ns.conn_name)
        if st != "ok":
            sys.exit(grant_refusal(engine, st, confirm, grant, ns.conn_name, then))
        via = "grant"
    elif ns.yes:
        via = "yes"
    writing = via is not None

    # 1. SNAPSHOT — on disk before anything runs
    snap = engine.snapshot(conn, schema, group, ident) if ident else {}
    probe = engine.dml_probe(stmt) if group == "data_dml" else None
    row_set = engine.measure_row_set(conn, *probe) if probe else None
    otype = ident["type"] if ident else group.upper()
    name = ident["name"] if ident else "STATEMENT"
    meta = {"schema": schema, "connection": ns.conn_name, "group": group,
            "applied": False, "row_set": row_set, "then": then,
            "lint": findings, **(origin or {})}
    eid = journal.write_entry(state, otype, name, "\n\n".join(snap.values()),
                              text, meta)
    auto_why = None
    if not writing:
        covered, auto_why = lease.covers(
            state, ns.conn_name, entry, group, engine.destructive_reason(stmt, group),
            row_set["count"] if row_set else None)
        if covered and lint.errors(findings):
            covered, auto_why = False, lint_refusal(findings)
        if covered:
            via, writing = "autopilot", True
            journal.update_entry(state, eid, autopilot=lease.read(state).get("token"))

    # 2. IMPACT — what depends on what is about to change
    summary = blind_impact = None
    if group == "source" and snap:
        found = engine.dependents(conn, schema, ident["name"], ns.depth)
        if found is not None:
            rows, blind_impact = found
            summary = impact_summary(rows, schema)

    # 3. REHEARSE — inside a transaction; rolled back unless this run writes
    outcome = engine.rehearse(conn, schema, group, stmt, ident, snap)
    predicted = bool(outcome.get("predicted"))
    if predicted and missing(engine, "predict"):
        conn.rollback()
        journal.update_entry(state, eid, refused=True, error="no predict tier")
        sys.exit(f"Refused: {engine.label} predicted this statement instead of "
                 "rehearsing it, but the engine has no predict tier ("
                 f"{', '.join(missing(engine, 'predict'))}) to run it once and "
                 "check what landed. Nothing was written.")
    if outcome.get("escaped"):
        return report_escape(engine, conn, ns, eid, group, ident, snap, outcome,
                             name, confirm)
    if outcome.get("error"):
        conn.rollback()
        journal.update_entry(state, eid, refused=True, error=outcome["error"])
        if via == "autopilot":
            lease.note_write(state, False, entry=eid, written=False)
        if writing:
            sys.exit(f"FAILED — nothing was written.\n  {outcome['error']}\nThe "
                     "approval is unspent: fix the cause and retry the same "
                     "command, or fix the file and preview again.")
        what = "Prediction" if predicted else "Rehearsal"
        sys.exit(f"{what} failed — {engine.label} would refuse the statement, so "
                 f"there is nothing to approve. Nothing was written.\n"
                 f"  {outcome['error']}")
    if not writing:
        conn.rollback()
    obj = outcome.get("object") or name
    otype = outcome.get("type") or otype
    before = snap.get(obj, "") if ident else ""
    created = group == "source" and obj not in snap
    token = apply_token(otype, obj, text, before + fingerprint_text(row_set))
    hurt = engine.destructive_reason(stmt, group)
    restore = (engine.restore_statement(group, ident, before, outcome)
               if group in policy.UNDOABLE else "")
    rehearsal = {"verified": bool(outcome.get("verified")),
                 "blind": outcome.get("blind"),
                 "broken": _findings(outcome.get("broken")),
                 "own_errors": _findings(outcome.get("own_errors")),
                 "rows": outcome.get("rows"),
                 "notes": list(outcome.get("notes") or []),
                 "predicted": predicted}
    if predicted:
        rehearsal["notes"].append(PREDICTED_NOTE)
    after = outcome.get("after") or stmt
    journal.update_entry(state, eid, before=before if ident else "", after=after,
                         restore=restore, object=obj, type=otype, token=token,
                         created=created, rehearsal=rehearsal, destructive=hurt,
                         summary=summary)

    def refuse(message):
        conn.rollback()
        journal.update_entry(state, eid, refused=True, error=message)
        sys.exit(message)

    # the write must still be the one that was approved
    if confirm:
        if token != confirm:
            refuse(_token_mismatch(state, confirm, row_set, then))
        _, approved = journal.find_by_token(state, token, kinds=("preview",),
                                            exclude=eid)
        if approved is None:
            refuse(f"No preview carries token {token}: approve what was seen. "
                   f"Preview again:\n  {then}")
        seen = {tuple(b) for b in (approved.get("rehearsal") or {}).get("broken", [])}
        moved = [b for b in rehearsal["broken"] if tuple(b) not in seen]
        if moved:
            refuse(("Refused: the prediction moved since the approved preview — "
                    if predicted else
                    "Rolled back: the verification moved since the approved "
                    "preview — ")
                   + "these break now and did not then:\n"
                   + "\n".join(f"  {b[0]} ({b[1]}): {b[2]}" for b in moved)
                   + f"\nNothing was written. Preview again:\n  {then}")
    if row_set is not None and writing and rehearsal["rows"] != row_set["count"]:
        refuse(f"Rolled back: the statement touched {rehearsal['rows']} rows, but "
               f"{row_set['count']} were approved.\nNothing was committed — the "
               "rows moved between the check and the write. Preview again.")

    # 4. PREVIEW
    diff_text, changed = render_diff(before, after if ident else stmt)
    warn = engine.privilege_warning(conn, schema)
    entry_path = journal.root(state) / eid
    if ns.json and not writing:
        print(json.dumps({"ok": True, "object": obj, "type": otype,
                          "group": group, "created": created,
                          "changed_lines": changed, "summary": summary,
                          "impact_blind_spots": blind_impact,
                          "rehearsal": rehearsal, "row_set": row_set,
                          "destructive": hurt, "warning": warn, "token": token,
                          "journal": eid, "will_apply": False, "lint": findings,
                          "autopilot": {"covered": False, "why": auto_why}},
                         default=str))
    elif not ns.json:
        _print_preview(group, obj, otype, schema, created, changed, summary,
                       blind_impact, rehearsal, row_set, hurt, warn, diff_text,
                       entry_path)
        print_lint(findings)
    if not writing:
        if not ns.json:
            print()
            if auto_why:
                print(f"  ! not landed under autopilot — {auto_why}; this one "
                      "needs its own approval.")
            for ln in door_lines(engine, token, then):
                print(f"  {ln}")
        return 0

    # 5. COMMIT — a predicted statement runs now, once, outside any transaction
    if predicted:
        done = engine.execute(conn, schema, group, stmt, ident) or {}
        if done.get("escaped"):
            return report_escape(engine, conn, ns, eid, group, ident, snap,
                                 {**outcome, **done}, name, confirm)
        if done.get("error"):
            conn.rollback()
            journal.update_entry(state, eid, refused=True, error=done["error"])
            if via == "autopilot":
                lease.note_write(state, False, entry=eid, written=False)
            sys.exit(f"FAILED — {engine.label} refused it, and nothing was "
                     f"written.\n  {done['error']}\nThe approval is unspent: fix "
                     "the cause and retry the same command, or fix the file and "
                     "preview again.")
    conn.commit()
    if via == "grant":
        grants.spend(state, confirm)
    journal.update_entry(state, eid, applied=True, confirmed_via=via)
    differs = []
    if predicted:
        found = engine.verify_written(conn, schema, group, stmt, ident, outcome) or {}
        rehearsal["broken"] = _findings(found.get("broken", rehearsal["broken"]))
        rehearsal["own_errors"] = _findings(found.get("own_errors",
                                                      rehearsal["own_errors"]))
        differs = [str(d) for d in found.get("differs") or []]
        journal.update_entry(state, eid, verified_after={
            "broken": rehearsal["broken"], "own_errors": rehearsal["own_errors"],
            "differs": differs})

    # 6. REPORT
    broken, own = rehearsal["broken"], rehearsal["own_errors"]
    ok = not broken and not own and not differs
    after_lease = (lease.note_write(state, ok, len(broken) + len(own) + len(differs),
                                    eid)
                   if via == "autopilot" else None)
    if after_lease and not ns.json:
        print(f"\n  Landed under the developer's autopilot lease — "
              f"{lease.line(after_lease)}")
    undo = f"{cmd} journal restore {eid}" if group in policy.UNDOABLE else None
    drop_denied = created and policy.load(state)["structural"][0] == "deny"
    if ns.json:
        print(json.dumps({"ok": ok, "applied": True, "object": obj, "type": otype,
                          "rows": rehearsal["rows"], "broken": broken,
                          "errors": own, "verified": rehearsal["verified"],
                          "blind_spot": rehearsal["blind"], "restore": undo,
                          "restore_is_drop": created,
                          "restore_blocked_by_policy": drop_denied,
                          "predicted": predicted, "differs": differs,
                          "confirmed_via": via, "exit": 0 if ok else 3}))
        return 0 if ok else 3
    if ok and group == "data_dml":
        print(f"\n  Applied — {rehearsal['rows']} rows changed and committed.")
    elif ok:
        print(f"\n  Applied {obj} ({otype}).")
    elif own or broken:
        print(f"\n  Applied {obj} ({otype}) — but it is broken:")
        for name_, type_, msg in own + broken:
            print(f"    {name_} ({type_}): {msg}")
    if differs:
        print(f"\n  Applied {obj} ({otype}) — but what landed differs from the "
              "prediction the developer approved:")
        for d in differs:
            print(f"    {d}")
    if ok and not rehearsal["verified"]:
        print(f"  NOT VERIFIED: {rehearsal['blind']}")
    if undo is None:
        print("\n  There is no undo: the journal kept the statement that ran, "
              "not a way back.")
    else:
        if created:
            print("\n  To undo — note: undo means DROPPING it (it did not exist "
                  "before):")
        else:
            print("\n  To undo (a write like any other — it previews first):")
        print(f"    {undo}")
        if drop_denied:
            print("  ...but that restore is a DROP, and structural is set to "
                  "deny, so it will be refused.\n  The developer decides: "
                  f"{cmd} policy set structural confirm")
    return 0 if ok else 3


def mark_escape(engine, state, eid, group, ident, snap, outcome, name):
    """Journal a statement whose rehearsal escaped the transaction, with the
    restore its snapshot allows; the restore command, or None."""
    obj = outcome.get("object") or name
    before = snap.get(obj, "") if ident else ""
    restore = ""
    if group in policy.UNDOABLE and ident:
        restore = engine.restore_statement(group, ident, before, outcome)
    known = {"type": outcome["type"]} if outcome.get("type") else {}
    journal.update_entry(state, eid, before=before if ident else None,
                         restore=restore, object=obj, escaped=outcome["escaped"],
                         error=outcome.get("error"), **known)
    return f"{engine.command} journal restore {eid}" if restore.strip() else None


def report_escape(engine, conn, ns, eid, group, ident, snap, outcome, name,
                  confirm):
    """The engine's transaction ended inside the statement — a trigger that
    commits, say. What ran may already be committed, approved or not, so it
    is never reported as unwritten: exit 3, with the way back."""
    conn.rollback()
    state = ns.state
    undo = mark_escape(engine, state, eid, group, ident, snap, outcome, name)
    if confirm:
        grants.spend(state, confirm)          # one approval, one run
    lease.pause(state, f"a statement escaped the transaction ({outcome['escaped']})",
                eid)
    if ns.json:
        print(json.dumps({"ok": False, "applied": None, "escaped": outcome["escaped"],
                          "error": outcome.get("error"), "object": outcome.get("object") or name,
                          "restore": undo, "journal": eid, "exit": 3}))
        return 3
    print(f"\n  WRITTEN? — {engine.label} ended pythia's transaction inside the "
          f"statement: {outcome['escaped']}.")
    if outcome.get("error"):
        print(f"    {outcome['error']}")
    print(f"  What ran may already be committed{'' if confirm else ' — nobody approved that'}, "
          "and pythia can no longer roll it back. Look at it before anything else.")
    escape_footer(engine, [undo] if undo else [], journal.root(state) / eid)
    return 3


def escape_footer(engine, undos, where):
    if len(undos) > 1:
        print("\n  If they landed, these put back the snapshots taken before they "
              "ran, newest first (each a write like any other — it previews first):")
    elif undos:
        print("\n  If it landed, this puts back the snapshot taken before it ran "
              "(a write like any other — it previews first):")
    else:
        print(f"\n  There is no snapshot to restore from — the journal kept the "
              f"statement that ran, in {where}.")
    for u in undos:
        print(f"    {u}")


def lint_one(engine, state, name, text, group):
    """The lint findings for one statement file; [] for an engine with no
    rules. A team's lint.json that cannot be read is refused here, before
    anything runs."""
    if group == "session" or missing(engine, "lint"):
        return []
    line = 1
    if not missing(engine, "release"):
        parts = engine.split_script(text)
        line = parts[0][0] if parts else 1
    return lint.run(engine, lint.load(engine, state),
                    [{"file": name, "line": line, "text": text, "group": group}])


def lint_refusal(findings):
    return (f"{len(lint.errors(findings))} lint error(s) — autopilot does not land "
            "them; fix the file, or the developer approves it as it is")


def lint_card_lines(findings, prefix=""):
    bad = lint.errors(findings)
    if not bad:
        return []
    return [f"! {prefix}{len(bad)} lint error{'s' if len(bad) != 1 else ''}: "
            + "; ".join(f"{f['rule']} (line {f['line']}): {f['message']}" for f in bad)]


def print_lint(findings):
    if findings:
        print(f"\n  lint: {lint.summary(findings)}")
        for ln in lint.render(findings, indent="    "):
            print(ln)


def _print_preview(group, obj, otype, schema, created, changed, summary,
                   blind_impact, rehearsal, row_set, hurt, warn, diff_text,
                   entry_path):
    if group in ("source", "comments"):
        head = ("new object" if created else
                "no change" if changed == 0 else f"{changed} lines changed")
        print(f"\n  {obj} ({otype}) in {schema} — {head}")
    else:
        print(f"\n  {group} statement on {schema}")
    if summary:
        for ln in summary_lines(summary):
            print(f"  {ln.lstrip('- ')}")
        for note in blind_impact or []:
            print(f"  blind spot: {note}")
    if rehearsal.get("predicted"):
        _print_prediction(rehearsal)
    elif rehearsal["broken"] or rehearsal["own_errors"]:
        print("\n  ! rehearsed — ran and rolled back, and this would break:")
        for name_, type_, msg in rehearsal["own_errors"] + rehearsal["broken"]:
            print(f"    {name_} ({type_}): {msg}")
    elif group == "data_dml":
        print(f"\n  rehearsed — ran and rolled back: {rehearsal['rows']} rows")
    elif group == "comments":
        print("\n  rehearsed — ran and rolled back")
    elif rehearsal["verified"]:
        print("\n  rehearsed — ran and rolled back: it compiles, and nothing "
              "else broke")
    else:
        print("\n  rehearsed — ran and rolled back, NOT VERIFIED: "
              f"{rehearsal['blind']}")
    for note in rehearsal["notes"]:
        print(f"  ! {note}")
    if row_set is not None:
        print()
        for ln in render_row_set(row_set):
            print(ln)
    elif group == "data_dml":
        print(f"\n  {rehearsal['rows']} rows in the rehearsal; no row set to "
              "revalidate (an INSERT has no rows beforehand).")
    if warn:
        print(f"\n  {warn}")
    if hurt:
        print(f"\n  ! DESTRUCTIVE: {hurt}.")
    if diff_text:
        print()
        for ln in diff_text.splitlines():
            print(f"  {ln}")
    print(f"\n  Snapshot saved: {entry_path}")


PREDICTED_NOTE = ("nothing ran: this is a prediction from the data as it "
                  "stands. It is checked again after the write; if what lands "
                  "differs, the report says so and exits 3")


def _print_prediction(rehearsal):
    if rehearsal["broken"] or rehearsal["own_errors"]:
        print("\n  ! predicted — nothing ran, and this would break:")
        for name_, type_, msg in rehearsal["own_errors"] + rehearsal["broken"]:
            print(f"    {name_} ({type_}): {msg}")
    elif rehearsal["verified"]:
        print("\n  predicted — nothing ran: nothing would break")
    else:
        print(f"\n  predicted — nothing ran, NOT VERIFIED: {rehearsal['blind']}")


def _card_verdict(reh):
    what = "Predicted (nothing ran)" if reh.get("predicted") else "Rehearsed"
    if reh.get("broken") or reh.get("own_errors"):
        return (f"{what}: this BREAKS — "
                + "; ".join(f"{b[0]}: {b[2]}" for b in
                            reh.get("own_errors", []) + reh.get("broken", [])))
    if reh.get("predicted"):
        return (f"{what}; nothing would break." if reh.get("verified") else
                f"{what}, NOT VERIFIED — {reh.get('blind')}")
    if reh.get("verified"):
        return "Rehearsed: ran and rolled back; nothing broke."
    return f"Rehearsed: ran and rolled back, NOT VERIFIED — {reh.get('blind')}"


# --- the developer's half -----------------------------------------------------

def approval_card(engine, state, token):
    """What a person approves, read from the preview's journal entry and never
    recomputed: the object, the rehearsal, the rows, the warnings. A 10-digit
    token names a release (release.py), approved on its own card."""
    if len(str(token)) == 10:
        from . import release
        return release.approval_card(engine, state, token)
    if len(str(token)) == 8:
        return lease.approval_card(engine, state, token)
    entry, meta = journal.find_by_token(state, token)
    if not meta:
        sys.exit(f"No pending preview carries token {token!r}.\nApprove what "
                 "you have seen: preview first, then approve the token it "
                 f"prints:\n  {engine.command} apply <file>")
    if meta.get("applied"):
        sys.exit(f"Token {token!r} belongs to a preview that was already "
                 f"applied.\nPreview again for a fresh token:\n  "
                 f"{engine.command} apply <file>")
    if meta.get("batch"):
        sys.exit(f"Token {token!r} is one statement of release "
                 f"{meta['batch']}: a release is approved as a whole, on its "
                 f"own card:\n  {engine.command} approve --card {meta['batch']}")
    group = meta.get("group", "")
    reh = meta.get("rehearsal") or {}
    lines = []
    if group in ("source", "comments"):
        lines.append(f"Approving: {meta.get('object')} ({meta.get('type')}) in "
                     f"{meta.get('schema')}"
                     + (" — a new object" if meta.get("created") else ""))
        if meta.get("summary"):
            lines.extend(ln.lstrip("- ") for ln in summary_lines(meta["summary"]))
    else:
        stmt = journal.read_entry(state, entry)["after"].strip()
        lines.append(f"Approving a {group} statement on {meta.get('schema')}:")
        lines += [f"  {ln}" for ln in stmt.splitlines()]
        if meta.get("row_set"):
            lines.append("")
            lines += render_row_set(meta["row_set"])
    lines.append(_card_verdict(reh))
    if meta.get("destructive"):
        lines.append(f"! DESTRUCTIVE: {meta['destructive']}.")
    lines += lint_card_lines(meta.get("lint") or [])
    if group not in policy.UNDOABLE:
        lines.append("There is no snapshot for this group — after commit it "
                     "cannot be undone from the journal.")
    lines.append(f"Previewed {entry[:19].replace('T', ' ')} on connection "
                 f"{meta.get('connection') or '?'}.")
    return entry, meta, lines


def grant_approval(engine, state, token, meta, approver, session=None):
    """Record a person's Approve and say what the agent may do now. The
    console, the chat hook and the MCP elicitation all end here, so no door
    grants more than another."""
    if meta.get("kind") == "lease-request":
        return lease.activate(engine, state, meta, approver, session)
    grants.prune(state)
    grants.mint(state, token, meta.get("connection") or "",
                revalidate=fingerprint_text(meta.get("row_set")),
                approver=approver, session=session)
    then = meta.get("then") or f"{engine.command} apply <file>"
    return (f"grant minted, single use, expires in {grants.TTL_MINUTES} minutes. "
            f"Run: {then} --confirm {token}")


def approve(engine, state, token, card_only=False, as_json=False):
    token = str(token or "").strip().lower()
    if not grants.valid_token(token):
        sys.exit(f"Not a pythia token: {token!r} — tokens are the hex digits "
                 "a preview prints (6 for apply, 8 for an autopilot request, "
                 "10 for deploy).")
    if not card_only and not console_session():
        sys.exit(approve_headless(engine))
    entry, meta, lines = approval_card(engine, state, token)
    if card_only:
        if as_json:
            print(json.dumps({"ok": True, "token": token, "entry": entry,
                              "card": "\n".join(lines)}))
        else:
            print(f"{engine.command} approve {token}")
            print("\n".join(lines))
        return 0
    msg = grant_approval(engine, state, token, meta, "console")
    if meta.get("kind") == "lease-request":
        if as_json:
            print(json.dumps({"ok": True, "token": token, "lease": lease.read(state)}))
            return 0
        print()
        for ln in lines:
            print(f"  {ln}")
        print(f"\n  {msg}")
        return 0
    rec = grants.read(state, token)
    if as_json:
        print(json.dumps({"ok": True, "token": token, "entry": entry,
                          "card": "\n".join(lines), "minted_at": rec["minted_at"],
                          "expires_at": rec["expires_at"]}))
        return 0
    print()
    for ln in lines:
        print(f"  {ln}")
    print(f"\n  Grant minted — single use, expires in {grants.TTL_MINUTES} "
          "minutes.")
    print(f"  The agent may now run the command the preview printed, with "
          f"--confirm {token}")
    return 0

#!/usr/bin/env python3
"""Adversarial self-checks for deploy: a release — scripts holding many
statements — through the gate as ONE transaction, under one approval. Each
test tries to get part of a release written that was not approved as a
whole: a statement the gate cannot place, a script edited after approval, a
member whose object, rows or verification moved, a failure halfway through.

Run: python tests/test_release.py
"""
import json
import os
import re
import sys

import fakes  # noqa: F401 — puts src/ on sys.path
from fakes import FakeHarnessEngine, FakePredictEngine
from test_gate import OLD, expect, project

from pythia_core import doors, grants, harness, human, journal, release  # noqa: E402

FN = "create or replace function f_total() returns int as 'select 2'"
COMMENT = "comment on table t_order is 'orders'"
DML = "delete from t_order where id < 3"
DDL = "alter table t_order add x int"


def engine():
    e = FakeHarnessEngine()
    e.conn.objects["f_total()"] = OLD
    e.conn.rows["t_order"] = [1, 2, 3, 4]
    return e


def scripts(p, *files):
    """Write each (name, [statements]) and return the names."""
    names = []
    for name, stmts in files:
        (p.root / name).write_text("".join(s + ";\n" for s in stmts), encoding="utf-8")
        names.append(name)
    return names


def preview(p, *names):
    code, out, why = p.run("deploy", *names, "--json")
    assert code == 0, (why, out)
    return json.loads(out.strip().splitlines()[-1])


# --- the preview: every statement rehearsed in one transaction, nothing kept ---------

def test_a_release_is_rehearsed_as_one_transaction_and_kept_by_none():
    e = engine()
    with project(e) as p:
        p.policy(data_dml="confirm", structural="confirm")
        names = scripts(p, ("01_code.sql", [FN, COMMENT]), ("02_data.sql", [DDL, DML]))
        doc = preview(p, *names)
        assert re.fullmatch(r"[0-9a-f]{10}", doc["batch"])
        assert [m["n"] for m in doc["members"]] == [1, 2, 3, 4]
        assert [m["group"] for m in doc["members"]] == [
            "source", "comments", "structural", "data_dml"]
        assert [(m["file"], m["line"]) for m in doc["members"]] == [
            ("01_code.sql", 1), ("01_code.sql", 2), ("02_data.sql", 1), ("02_data.sql", 2)]
        assert e.conn.rehearsed == [FN, COMMENT, DDL, DML]
        assert e.conn.commits == 0 and e.conn.objects["f_total()"] == OLD
        assert e.conn.pending == [] and e.conn.rollbacks >= 1   # rolled back, not left open
        assert e.conn.rows["t_order"] == [1, 2, 3, 4]
        assert doc["members"][3]["row_set"]["count"] == 2
        member = doc["members"][0]["token"]
        expect(p.run("approve", "--card", member), 1, "approved as a whole",
               f"approve --card {doc['batch']}")
        entries = [journal.read_entry(p.state, x)["meta"] for x in p.entries()]
        assert sorted(m["batch"] for m in entries) == [doc["batch"]] * 4
        code, out, _ = p.run("deploy", *names)
        assert code == 0
        for needle in ("in one transaction", "all or nothing", "f_total()",
                       DDL, DML, "2 rows in t_order", f"--confirm {doc['batch']}",
                       f"pythia-fake approve --card {doc['batch']}"):
            assert needle in out, needle


def test_what_the_gate_cannot_place_is_refused_before_anything_runs():
    e = engine()
    with project(e) as p:
        for stmts, needles in (
                ([FN, "do $$ begin end $$"], ("rel.sql:2", "anonymous")),
                ([FN, "select 1"], ("rel.sql:2", "cannot classify")),
                (["set search_path to other", FN], ("rel.sql:1", "set", "every later statement")),
                ([FN, DML], ("rel.sql:2", "data_dml is set to deny", "policy set data_dml confirm"))):
            names = scripts(p, ("rel.sql", stmts))
            expect(p.run("deploy", *names), 1, *needles)
        assert e.conn.rehearsed == [] and p.entries() == []
        expect(p.run("deploy", "missing.sql"), 1, "no such file")
        (p.root / "empty.sql").write_text("\n  \n", encoding="utf-8")
        expect(p.run("deploy", "empty.sql"), 1, "no statement")


def test_a_statement_that_fails_in_rehearsal_stops_the_preview_and_keeps_nothing():
    e = engine()
    with project(e) as p:
        names = scripts(p, ("rel.sql", [FN, COMMENT]))

        def fail_on_comment(stmt):
            e.conn.fail = 'relation "t_order" does not exist' if stmt == COMMENT else None
        e.conn.on_execute = fail_on_comment
        expect(p.run("deploy", *names), 1, "rel.sql:2", "does not exist",
               "nothing was written")
        assert e.conn.commits == 0 and e.conn.objects["f_total()"] == OLD


# --- the confirm: all of it as approved, or none of it -----------------------------------

def test_one_approval_lands_the_whole_release_in_one_commit():
    e = engine()
    with project(e) as p:
        names = scripts(p, ("rel.sql", [FN, COMMENT]))
        batch = preview(p, *names)["batch"]
        expect(p.run("deploy", *names, "--confirm", batch), 1,
               "no developer approval is on file")
        assert e.conn.commits == 0
        p.approve(batch)
        code, out, why = p.run("deploy", *names, "--confirm", batch)
        assert code == 0, why
        assert e.conn.commits == 1                      # one transaction, one commit
        assert e.conn.objects["f_total()"] == FN and e.conn.comments["t_order"] == "orders"
        assert grants.read(p.state, batch)["used_at"]
        assert "newest first" in out and "journal restore" in out
        expect(p.run("deploy", *names, "--confirm", batch), 1)   # one approval, one run
        assert e.conn.commits == 1


def test_a_script_edited_after_approval_runs_nothing():
    e = engine()
    with project(e) as p:
        names = scripts(p, ("rel.sql", [FN, COMMENT]))
        batch = preview(p, *names)["batch"]
        p.approve(batch)
        scripts(p, ("rel.sql", [FN, "comment on table t_order is 'other'"]))
        expect(p.run("deploy", *names, "--confirm", batch), 1, "changed since",
               "statement 2", "nothing ran")
        scripts(p, ("rel.sql", [FN]))
        expect(p.run("deploy", *names, "--confirm", batch), 1, "changed since")
        scripts(p, ("rel.sql", [FN, COMMENT]), ("more.sql", [COMMENT]))
        expect(p.run("deploy", "rel.sql", "more.sql", "--confirm", batch), 1,
               "different files")
        assert e.conn.rehearsed == [FN, COMMENT] and e.conn.commits == 0


def test_a_member_that_moved_rolls_back_the_whole_release():
    for moved, needles in (("object", ("statement 1", "changed since that preview")),
                           ("rows", ("statement 2", "row set moved")),
                           ("verification", ("statement 1", "v_report", "verification moved")),
                           ("rowcount", ("statement 2", "touched 9 rows"))):
        e = engine()
        with project(e) as p:
            p.policy(data_dml="confirm")
            names = scripts(p, ("rel.sql", [FN, DML]))
            batch = preview(p, *names)["batch"]
            p.approve(batch)
            if moved == "object":
                e.conn.objects["f_total()"] = OLD + " -- edited by hand"
            elif moved == "rows":
                e.conn.rows["t_order"] = [0, 1, 2, 3, 4]
            elif moved == "verification":
                e.conn.broken_after = [("v_report", "VIEW", "column total does not exist")]
            else:
                e.conn.rowcount = 9
            expect(p.run("deploy", *names, "--confirm", batch), 1, "rolled back",
                   "nothing was written", *needles)
            assert e.conn.commits == 0, moved
            assert grants.read(p.state, batch)["used_at"] is None, moved   # unspent


def test_a_failure_halfway_through_the_confirm_keeps_nothing_of_the_release():
    e = engine()
    with project(e) as p:
        names = scripts(p, ("rel.sql", [FN, COMMENT]))
        batch = preview(p, *names)["batch"]
        p.approve(batch)

        def fail_second(stmt):
            e.conn.fail = "deadlock detected" if stmt == COMMENT else None
        e.conn.on_execute = fail_second
        expect(p.run("deploy", *names, "--confirm", batch), 1, "FAILED",
               "statement 2", "deadlock detected", "nothing was written")
        assert e.conn.commits == 0 and e.conn.objects["f_total()"] == OLD
        assert "t_order" not in e.conn.comments
        e.conn.on_execute, e.conn.fail = None, None
        assert p.run("deploy", *names, "--confirm", batch)[0] == 0   # still approved
        assert e.conn.commits == 1


def test_a_member_that_escaped_the_transaction_takes_the_release_with_it():
    # a trigger's COMMIT at statement 2 commits statement 1 too: the
    # release is never reported as rolled back
    e = engine()
    with project(e) as p:
        names = scripts(p, ("rel.sql", [COMMENT, FN]))
        batch = preview(p, *names)["batch"]
        p.approve(batch)
        e.conn.escape = "a trigger ended the transaction"
        code, out, why = p.run("deploy", *names, "--confirm", batch)
        assert code == 3, (code, why, out)
        assert "nothing was written" not in (out + why).lower()
        for needle in ("WRITTEN?", "statement 2", "rel.sql:2",
                       "a trigger ended the transaction", "statements 1-2",
                       "may already be committed", "journal restore"):
            assert needle in out, needle
        assert grants.read(p.state, batch)["used_at"]            # one approval, one run
        metas = [journal.read_entry(p.state, x)["meta"] for x in p.entries()]
        escaped = [m for m in metas if m.get("escaped")]
        assert len(escaped) == 2 and all(m.get("batch") == batch for m in escaped)
        assert out.index("journal restore") < len(out)
        assert e.conn.pending == []
        code, out, _ = p.run("deploy", *names, "--json")
        doc = json.loads(out.strip().splitlines()[-1])
        assert code == 3 and doc["ok"] is False and doc["escaped"]["n"] == 2
        assert len(doc["restore"]) == 2


def test_a_statement_that_can_only_be_predicted_has_no_place_in_a_release():
    # all or nothing needs one transaction; a predicted write runs outside it
    e = FakePredictEngine()
    e.conn.objects["f_total()"] = OLD
    e.conn.rows["t_order"] = [1, 2, 3, 4]
    with project(e) as p:
        names = scripts(p, ("rel.sql", [COMMENT, FN]))
        expect(p.run("deploy", *names), 1, "statement 2", "cannot be rehearsed",
               "apply it on its own")
        assert e.conn.executed == [] and e.conn.commits == 0
        assert e.conn.pending == [] and "t_order" not in e.conn.comments


def test_an_approved_breakage_lands_and_exits_3():
    e = engine()
    e.conn.broken_after = [("v_report", "VIEW", "column total does not exist")]
    with project(e) as p:
        names = scripts(p, ("rel.sql", [FN]))
        doc = preview(p, *names)
        assert doc["members"][0]["rehearsal"]["broken"]
        p.approve(doc["batch"])
        code, out, _ = p.run("deploy", *names, "--confirm", doc["batch"])
        assert code == 3 and e.conn.commits == 1 and "v_report" in out


def test_yes_is_still_the_developers_flag():
    e = engine()
    with project(e) as p:
        names = scripts(p, ("rel.sql", [FN]))
        expect(p.run("deploy", *names, "--yes"), 1, "--yes is the developer's flag")
        assert e.conn.rehearsed == []
        human.FORCE_CONSOLE = True
        code, _, why = p.run("deploy", *names, "--yes")
        assert code == 0 and e.conn.commits == 1, why


def test_the_release_itself_refuses_a_headless_yes():
    """Defence in depth: the command line refuses first, but anything else
    that calls run_deploy meets the same refusal."""
    from argparse import Namespace
    e = engine()
    with project(e) as p:
        names = scripts(p, ("rel.sql", [FN]))
        ns = Namespace(state=p.state, schema="app", conn_name="dev", paths=names,
                       confirm=None, yes=True, json=False, depth=3)
        try:
            release.run_deploy(e, e.conn, ns)
        except SystemExit as stop:
            assert "--yes is the developer's" in str(stop)
        else:
            raise AssertionError("a headless --yes reached the database")
        assert e.conn.rehearsed == [] and e.conn.commits == 0


def test_a_release_is_applied_once_whatever_grants_say_and_never_across_schemas():
    e = engine()
    with project(e) as p:
        names = scripts(p, ("rel.sql", [COMMENT]))
        batch = preview(p, *names)["batch"]
        p.approve(batch)
        assert p.run("deploy", *names, "--confirm", batch)[0] == 0
        e.conn.comments.clear()                      # the database put back by hand
        p.approve(batch)                             # and a fresh grant on file
        expect(p.run("deploy", *names, "--confirm", batch), 1, "already applied")
        assert e.conn.commits == 1
        names = scripts(p, ("other.sql", [
            "create or replace function other.f_total() returns int as 'select 2'"]))
        expect(p.run("deploy", *names), 1, "names schema 'other'", "nothing was written")


# --- every door approves a release the way it approves one statement ------------------

def test_the_doors_approve_a_release_on_its_own_card():
    e = engine()
    with project(e) as p:
        names = scripts(p, ("rel.sql", [FN, COMMENT]))
        batch = preview(p, *names)["batch"]
        code, card, why = p.run("approve", "--card", batch)
        assert code == 0 and "in one transaction" in card and COMMENT in card, why
        payload = {"tool_name": "AskUserQuestion", "cwd": str(p.root),
                   "tool_input": {"questions": [{"question": card.strip()}]},
                   "tool_response": {"answers": {card.strip(): "Approve"}}}
        note = doors.hook_approve(e, p.state, payload)["hookSpecificOutput"][
            "additionalContext"]
        assert f"pythia-fake deploy rel.sql --confirm {batch}" in note, note
        assert grants.read(p.state, batch)["approver"] == "chat"
        out = harness.pre_tool_decision(e, {
            "tool_name": "Bash", "cwd": str(p.root),
            "tool_input": {"command": f"pythia-fake deploy rel.sql --confirm {batch}"}})
        assert out["hookSpecificOutput"]["permissionDecision"] == "allow"
        out = harness.pre_tool_decision(e, {
            "tool_name": "Bash", "cwd": str(p.root),
            "tool_input": {"command": "pythia-fake deploy rel.sql"}})
        assert out["hookSpecificOutput"]["permissionDecision"] == "allow"


def test_a_batch_token_names_a_release_and_nothing_else():
    e = engine()
    with project(e) as p:
        names = scripts(p, ("rel.sql", [FN]))
        batch = preview(p, *names)["batch"]
        p.approve(batch)
        p.file(FN)
        expect(p.run("apply", "change.sql", "--confirm", batch), 1)   # not a statement token
        assert e.conn.commits == 0
        assert release.read_batch(p.state, "../" + batch) is None
        assert release.read_batch(p.state, "0" * 10) is None


def main():
    failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except (Exception, SystemExit) as e:  # noqa: BLE001 — keep going
                failed += 1
                print(f"FAIL {name}: {e!r}")
    if failed:
        sys.exit(f"{failed} test(s) failed")
    print("OK")


if __name__ == "__main__":
    main()

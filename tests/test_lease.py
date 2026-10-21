#!/usr/bin/env python3
"""Adversarial self-checks for autopilot leases: the developer delegating a
session once instead of approving every write. Each test tries to get a
write landed without a per-write approval that the lease does not cover: a
group, a connection, a destructive statement or a row count it never named;
a lease that expired, was spent, revoked or paused; a lease nobody approved.

Run: python tests/test_lease.py
"""
import datetime
import json
import os
import sys

import fakes  # noqa: F401 — puts src/ on sys.path
from fakes import FakeHarnessEngine, FakePredictEngine
from test_gate import NEW, OLD, expect, project

from pythia_core import doors, harness, human, journal, lease  # noqa: E402

DROP = "drop table t_old"
ADD = "alter table t_order add x int"


def engine():
    e = FakeHarnessEngine()
    e.conn.objects["f_total()"] = OLD
    e.conn.rows["t_order"] = [1, 2, 3, 4]
    return e


def connections(p, **entries):
    (p.state / "connections.json").write_text(json.dumps(entries), encoding="utf-8")


def request(p, *args):
    code, out, why = p.run("autopilot", "request", "--json", *args)
    assert code == 0, why
    return json.loads(out.strip().splitlines()[-1])


def at_console(p, *argv):
    human.FORCE_CONSOLE = True
    try:
        return p.run(*argv)
    finally:
        human.FORCE_CONSOLE = False


def grant_lease(p, *args):
    doc = request(p, *args)
    code, _, why = at_console(p, "approve", doc["token"])
    assert code == 0, why
    return doc


# --- nothing is delegated until the developer approves the card ------------------------

def test_a_request_delegates_nothing_until_the_developer_approves_it():
    e = engine()
    with project(e) as p:
        doc = request(p, "--note", "rename the order functions")
        assert len(doc["token"]) == 8 and int(doc["token"], 16) >= 0
        for needle in ("connection dev", "source", "comments", "8 hours",
                       "200 writes", "rename the order functions",
                       "Destructive statements", "autopilot off"):
            assert needle in doc["card"], needle
        code, out, _ = p.run("autopilot", "status")
        assert "autopilot: off" in out and doc["token"] in out
        p.file(NEW)
        assert p.run("apply", "change.sql")[0] == 0
        assert e.conn.commits == 0                      # a request is not a lease
        code, _, why = at_console(p, "approve", doc["token"])
        assert code == 0, why
        assert "ACTIVE" in p.run("autopilot", "status")[1]
        code, out, why = p.run("apply", "change.sql")
        assert code == 0 and e.conn.commits == 1, why
        assert e.conn.objects["f_total()"] == NEW and "autopilot" in out.lower()
        meta = journal.read_entry(p.state, p.entries()[0])["meta"]
        assert meta["applied"] and meta["confirmed_via"] == "autopilot"
        assert lease.read(p.state)["writes"] == 1


def test_approving_a_lease_is_the_developers_act():
    e = engine()
    with project(e) as p:
        expect(p.run("autopilot", "on"), 1, "developer's command",
               "autopilot request")
        assert lease.read(p.state) is None
        assert at_console(p, "autopilot", "on")[0] == 0
        assert lease.status(lease.read(p.state)) == "active"
        assert lease.read(p.state)["approver"] == "console"


# --- a lease covers only what it names ---------------------------------------------------

def test_a_lease_covers_only_its_groups_its_connection_and_no_destruction():
    e = engine()
    with project(e) as p:
        connections(p, default="dev", dev={"host": "h", "user": "u", "schema": "app"},
                    test={"host": "h", "user": "u", "schema": "app"})
        p.policy(structural="confirm", data_dml="confirm")
        grant_lease(p)                                    # source, comments
        p.file(ADD)
        code, out, _ = p.run("apply", "change.sql")
        assert code == 0 and e.conn.commits == 0
        assert "does not cover structural" in out
        p.file(NEW)
        code, out, _ = p.run("apply", "change.sql", "--conn", "test")
        assert code == 0 and e.conn.commits == 0 and "covers connection dev" in out
        grant_lease(p, "--groups", "source,structural")
        p.file(DROP)
        code, out, _ = p.run("apply", "change.sql")
        assert code == 0 and e.conn.commits == 0 and "destructive" in out
        grant_lease(p, "--groups", "data_dml")
        e.conn.rows["t_order"] = list(range(1, 1502))
        p.file("delete from t_order where id < 1500")
        code, out, _ = p.run("apply", "change.sql")
        assert code == 0 and e.conn.commits == 0 and "1000 rows" in out
        grant_lease(p, "--groups", "source,structural", "--destructive")
        p.file(DROP)
        assert p.run("apply", "change.sql")[0] == 0 and e.conn.commits == 1


def test_a_protected_connection_is_never_covered():
    e = engine()
    with project(e) as p:
        connections(p, dev={"host": "h", "user": "u", "schema": "app", "env": "prod"})
        expect(p.run("autopilot", "request"), 1, "protected")
        connections(p, dev={"host": "h", "user": "u", "schema": "app"})
        grant_lease(p)
        connections(p, dev={"host": "h", "user": "u", "schema": "app", "protected": True})
        p.file(NEW)
        code, out, _ = p.run("apply", "change.sql")
        assert code == 0 and e.conn.commits == 0 and "protected" in out


def test_a_lease_naming_a_denied_group_lifts_the_deny_only_while_it_lasts():
    e = engine()
    with project(e) as p:
        p.file("delete from t_order where id < 3")
        expect(p.run("apply", "change.sql"), 1, "data_dml is set to deny")
        grant_lease(p)                       # a lease that does not name data_dml
        expect(p.run("apply", "change.sql"), 1, "data_dml is set to deny")
        doc = grant_lease(p, "--groups", "data_dml")
        assert "policy says deny" in doc["card"]
        code, _, why = p.run("apply", "change.sql")
        assert code == 0 and e.conn.commits == 1, why
        assert p.run("autopilot", "off")[0] == 0           # anyone may tighten
        p.file("delete from t_order where id < 4")
        expect(p.run("apply", "change.sql"), 1, "data_dml is set to deny")
        assert e.conn.commits == 1


# --- a lease ends: by time, by count, by the developer, by the evidence ----------------

def test_an_expired_spent_or_revoked_lease_lands_nothing():
    e = engine()
    with project(e) as p:
        grant_lease(p, "--max-writes", "1")
        p.file(NEW)
        assert p.run("apply", "change.sql")[0] == 0 and e.conn.commits == 1
        p.file("comment on table t_order is 'x'")
        code, out, _ = p.run("apply", "change.sql")
        assert e.conn.commits == 1 and "spent" in out
        grant_lease(p)
        rec = lease.read(p.state)
        rec["expires_at"] = (datetime.datetime.now() - datetime.timedelta(minutes=1)
                             ).isoformat(timespec="seconds")
        lease.write(p.state, rec)
        code, out, _ = p.run("apply", "change.sql")
        assert e.conn.commits == 1 and "expired" in out
        grant_lease(p)
        p.run("autopilot", "off")
        code, out, _ = p.run("apply", "change.sql")
        assert e.conn.commits == 1 and "revoked" in out


def test_breakage_and_repeated_failure_pause_the_lease():
    e = engine()
    with project(e) as p:
        grant_lease(p)
        e.conn.broken_after = [("v_report", "VIEW", "column total does not exist")]
        p.file(NEW)
        code, out, _ = p.run("apply", "change.sql")
        assert code == 3 and e.conn.commits == 1          # landed, broken, said so
        assert lease.status(lease.read(p.state)) == "paused"
        e.conn.broken_after = []
        p.file("comment on table t_order is 'x'")
        code, out, _ = p.run("apply", "change.sql")
        assert e.conn.commits == 1 and "paused" in out
        grant_lease(p)
        e.conn.fail = "deadlock detected"
        for _ in range(lease.FAILS_TO_PAUSE):
            p.run("apply", "change.sql")
        assert lease.status(lease.read(p.state)) == "paused"
        assert "failed writes in a row" in lease.line(lease.read(p.state))


def test_a_write_that_escaped_the_transaction_pauses_the_lease():
    e = engine()
    with project(e) as p:
        grant_lease(p)
        e.conn.escape = "a trigger ended the transaction"
        code, out, _ = p.run("apply", p.file(NEW))
        assert code == 3 and "WRITTEN?" in out
        rec = lease.read(p.state)
        assert lease.status(rec) == "paused"
        assert "escaped" in lease.line(rec)


def test_a_predicted_write_lands_under_a_lease_and_a_surprise_pauses_it():
    e = FakePredictEngine()
    with project(e) as p:
        grant_lease(p)
        code, out, _ = p.run("apply", p.file(NEW))
        assert code == 0 and e.conn.executed == [NEW] and "Landed under" in out
        e.conn.differs = ["the view fails on 2 documents the prediction did not see"]
        code, out, _ = p.run("apply", p.file(OLD))
        assert code == 3 and lease.status(lease.read(p.state)) == "paused"


def test_an_unapproved_or_forged_lease_file_lands_nothing():
    e = engine()
    with project(e) as p:
        doc = request(p)
        lease.write(p.state, {"token": doc["token"], "conn": "dev",
                              "groups": ["source"], "writes": 0,
                              "max_writes": 5, "expires_at": "not a date"})
        p.file(NEW)
        assert p.run("apply", "change.sql")[0] == 0 and e.conn.commits == 0
        later = (datetime.datetime.now() + datetime.timedelta(hours=1)).isoformat()
        lease.write(p.state, {"token": doc["token"], "connection": "dev",
                              "groups": ["source"], "writes": 0, "max_writes": 5,
                              "expires_at": later})       # well-formed, never approved
        assert p.run("apply", "change.sql")[0] == 0 and e.conn.commits == 0
        (p.state / "autopilot" / "lease.json").write_text("{ broken", encoding="utf-8")
        assert p.run("apply", "change.sql")[0] == 0 and e.conn.commits == 0
        out = harness.pre_tool_decision(e, {
            "tool_name": "Bash", "cwd": str(p.root), "tool_input": {
                "command": "echo '{}' > .pythia/fake/autopilot/lease.json"}})
        assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
        assert "forge" in out["hookSpecificOutput"]["permissionDecisionReason"]
        out = harness.pre_tool_decision(e, {
            "tool_name": "Write", "cwd": str(p.root),
            "tool_input": {"file_path": str(p.state / "autopilot" / "lease.json")}})
        assert out["hookSpecificOutput"]["permissionDecision"] == "deny"


# --- a release, the doors, the report and the guard -------------------------------------

def test_a_release_lands_under_a_lease_only_when_every_statement_is_covered():
    e = engine()
    with project(e) as p:
        p.policy(structural="confirm")
        (p.root / "rel.sql").write_text(
            f"{NEW};\ncomment on table t_order is 'orders';\n", encoding="utf-8")
        (p.root / "mixed.sql").write_text(f"{NEW};\n{ADD};\n", encoding="utf-8")
        grant_lease(p)
        code, out, _ = p.run("deploy", "mixed.sql")
        assert code == 0 and e.conn.commits == 0 and "statement 2" in out
        code, out, why = p.run("deploy", "rel.sql")
        assert code == 0 and e.conn.commits == 1, why
        assert lease.read(p.state)["writes"] == 2


def test_every_door_approves_a_lease_on_its_card():
    e = engine()
    with project(e) as p:
        doc = request(p)
        payload = {"tool_name": "AskUserQuestion", "cwd": str(p.root),
                   "tool_input": {"questions": [{"question": doc["card"]}]},
                   "tool_response": {"answers": {doc["card"]: "Approve"}}}
        note = doors.hook_approve(e, p.state, payload)["hookSpecificOutput"][
            "additionalContext"]
        assert "autopilot ACTIVE" in note, note
        assert lease.read(p.state)["approver"] == "chat"
        doc = request(p, "--groups", "comments")
        result = doors.mcp_approve_decision(
            e, p.state, doc["token"], lambda m, s: ("accept", {"decision": "Approve"}),
            iter(range(0, 100, 5)).__next__)
        assert not result["isError"] and lease.read(p.state)["groups"] == ["comments"]


def test_the_report_lists_every_write_and_its_undo():
    e = engine()
    with project(e) as p:
        grant_lease(p)
        p.file(NEW)
        p.run("apply", "change.sql")
        code, out, _ = p.run("autopilot", "report")
        assert code == 0 and "f_total" in out and "journal restore" in out
        doc = json.loads(p.run("autopilot", "report", "--json")[1])
        assert [w["verdict"] for w in doc["writes"]] == ["ok"]


def test_the_guard_and_the_brief_know_the_lease():
    e = engine()
    with project(e) as p:
        def decide(cmd):
            out = harness.pre_tool_decision(e, {"tool_name": "Bash", "cwd": str(p.root),
                                                "tool_input": {"command": cmd}})
            return out and out["hookSpecificOutput"]
        assert decide("pythia-fake autopilot request --groups source")["permissionDecision"] == "allow"
        assert decide("pythia-fake autopilot off")["permissionDecision"] == "allow"
        assert decide("pythia-fake autopilot on") is None
        assert "writes nothing" in decide("pythia-fake apply change.sql")["permissionDecisionReason"]
        grant_lease(p)
        assert "lease" in decide("pythia-fake apply change.sql")["permissionDecisionReason"]
        assert any("autopilot: ACTIVE" in ln for ln in harness.state_lines(e, p.root))


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

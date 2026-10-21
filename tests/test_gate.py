#!/usr/bin/env python3
"""Adversarial self-checks for the write gate, through a fake engine with a
real transaction (commits and rollbacks are counted). Each test tries to get
one write past one of pythia's invariants:

  snapshot on disk before anything runs · a refused statement touches nothing ·
  no commit without a person's approval, and one approval is one write · what
  is written is what was previewed · exit 3 for written-but-broken · a restore
  is a write like any other · --yes, approve and loosening policy need a
  person at a real console

Run: python tests/test_gate.py
"""
import contextlib
import datetime
import io
import json
import os
import pathlib
import re
import sys
import tempfile

import fakes  # noqa: F401 — puts src/ on sys.path
from fakes import FakePredictEngine, FakeWriteEngine, NoPredictTier

from pythia_core import cli, grants, human, journal  # noqa: E402

OLD = "create or replace function f_total() returns int as 'select 1'"
NEW = "create or replace function f_total() returns int as 'select 2'"


@contextlib.contextmanager
def project(engine):
    keep_env = {k: os.environ.pop(k) for k in list(os.environ)
                if k.startswith("PYTHIA_")}
    keep_cwd = os.getcwd()
    with tempfile.TemporaryDirectory() as td:
        root = pathlib.Path(td)
        cfg = root / ".pythia" / "fake" / "connections.json"
        cfg.parent.mkdir(parents=True)
        cfg.write_text(json.dumps({"dev": {"host": "localhost", "user": "u",
                                           "schema": "app"}}), encoding="utf-8")
        if os.name == "posix":
            os.chmod(cfg, 0o600)
        os.chdir(td)
        human.FORCE_CONSOLE = False            # no person here unless a test says so
        try:
            yield Project(engine, root)
        finally:
            os.chdir(keep_cwd)
            for k in [k for k in os.environ if k.startswith("PYTHIA_")]:
                del os.environ[k]
            os.environ.update(keep_env)
            human.FORCE_CONSOLE = None


class Project:
    def __init__(self, engine, root):
        self.e, self.root = engine, root
        self.state = root / ".pythia" / "fake"

    def file(self, text, name="change.sql"):
        (self.root / name).write_text(text + "\n", encoding="utf-8")
        return name

    def run(self, *argv):
        """(exit code, stdout, the refusal message or '')"""
        out = io.StringIO()
        try:
            with contextlib.redirect_stdout(out), \
                    contextlib.redirect_stderr(io.StringIO()):
                code = cli.main(self.e, list(argv))
            return code or 0, out.getvalue(), ""
        except SystemExit as e:
            if isinstance(e.code, int) or e.code is None:
                return e.code or 0, out.getvalue(), ""
            return 1, out.getvalue(), str(e)

    def preview(self, text, *extra):
        name = self.file(text)
        code, out, why = self.run("apply", name, "--json", *extra)
        assert code == 0, (code, why, out)
        return json.loads(out.strip().splitlines()[-1])

    def approve(self, token):
        grants.mint(self.state, token, "dev")

    def policy(self, **groups):
        (self.state / "policy.json").write_text(json.dumps(groups), encoding="utf-8")

    def entries(self):
        return journal.list_entries(self.state)


def expect(result, code, *needles):
    got, out, why = result
    assert got == code, (got, code, why, out)
    text = (why + "\n" + out).lower()
    for n in needles:
        assert n.lower() in text, f"missing {n!r} in: {why or out}"


# --- nothing runs before the snapshot, nothing is written without approval ---

def test_preview_rehearses_rolls_back_and_writes_nothing():
    e = FakeWriteEngine()
    e.conn.objects["f_total()"] = OLD
    with project(e) as p:
        doc = p.preview(NEW)
        assert doc["will_apply"] is False and re.fullmatch(r"[0-9a-f]{6}", doc["token"])
        assert doc["rehearsal"]["verified"] is True
        assert e.conn.commits == 0 and e.conn.rollbacks >= 1
        assert e.conn.objects["f_total()"] == OLD
        entry = journal.read_entry(p.state, p.entries()[0])
        assert entry["before"] == OLD and entry["meta"]["token"] == doc["token"]
        code, out, _ = p.run("apply", "change.sql")
        assert code == 0 and f"pythia-fake approve {doc['token']}" in out
        assert f"pythia-fake apply change.sql --confirm {doc['token']}" in out


def test_an_engine_without_install_offers_only_the_terminal_door():
    # no harness tier: no hook reads a chat answer, no MCP approver is
    # registered — a door that cannot mint a grant is never offered
    e = FakeWriteEngine()
    with project(e) as p:
        p.file(NEW)
        code, out, _ = p.run("apply", "change.sql")
        assert code == 0 and "pythia-fake approve " in out, out
        assert "AskUserQuestion" not in out and "MCP" not in out, out
        token = p.preview(NEW)["token"]
        code, _, why = p.run("apply", "change.sql", "--confirm", token)
        assert code == 1 and "AskUserQuestion" not in why and f"approve {token}" in why
        code, _, why = p.run("approve", token)
        assert code == 1 and "AskUserQuestion" not in why and "approve <token>" in why, why


def test_snapshot_lands_on_disk_before_the_rehearsal_runs():
    e = FakeWriteEngine()
    e.conn.objects["f_total()"] = OLD
    seen = []
    with project(e) as p:
        def check(stmt):
            ids = journal.list_entries(p.state)
            seen.append(bool(ids) and
                        journal.read_entry(p.state, ids[0])["before"] == OLD)
        e.conn.on_execute = check
        p.preview(NEW)
        e.conn.fail = "boom"                  # even when the statement explodes
        expect(p.run("apply", p.file(NEW)), 1, "rehearsal failed", "boom")
    assert seen == [True, True]


def test_a_failing_statement_has_nothing_to_approve():
    e = FakeWriteEngine()
    e.conn.fail = 'syntax error at or near "retrun"'
    with project(e) as p:
        expect(p.run("apply", p.file(NEW)), 1, "rehearsal failed",
               'syntax error at or near "retrun"', "nothing was written")
        assert e.conn.commits == 0


def test_a_refused_statement_touches_nothing():
    e = FakeWriteEngine()
    e.conn.rows["t_order"] = [1, 2]
    with project(e) as p:
        expect(p.run("apply", p.file("delete from t_order")), 1,
               "data_dml is set to deny", "policy set data_dml confirm")
        expect(p.run("apply", p.file("do $$ begin end $$")), 1, "anonymous")
        expect(p.run("apply", p.file("select 1")), 1, "cannot classify")
        assert e.conn.rehearsed == [] and p.entries() == []


def test_confirm_without_approval_runs_nothing():
    e = FakeWriteEngine()
    with project(e) as p:
        token = p.preview(NEW)["token"]
        expect(p.run("apply", "change.sql", "--confirm", token), 1,
               "no developer approval is on file", f"pythia-fake approve {token}")
        assert len(e.conn.rehearsed) == 1 and e.conn.commits == 0


def test_one_approval_is_one_write():
    e = FakeWriteEngine()
    e.conn.objects["f_total()"] = OLD
    with project(e) as p:
        token = p.preview(NEW)["token"]
        p.approve(token)
        code, out, why = p.run("apply", "change.sql", "--confirm", token)
        assert code == 0, why
        assert e.conn.commits == 1 and e.conn.objects["f_total()"] == NEW
        assert grants.read(p.state, token)["used_at"]
        assert "journal restore" in out
        # the same token again: the object moved, and the grant is spent
        expect(p.run("apply", "change.sql", "--confirm", token), 1)
        assert e.conn.commits == 1


def test_expired_and_foreign_approvals_are_refused():
    e = FakeWriteEngine()
    with project(e) as p:
        token = p.preview(NEW)["token"]
        long_ago = datetime.datetime.now() - datetime.timedelta(hours=1)
        grants.mint(p.state, token, "dev", now=long_ago)
        expect(p.run("apply", "change.sql", "--confirm", token), 1, "expired")
        grants.mint(p.state, token, "prod")
        expect(p.run("apply", "change.sql", "--confirm", token), 1,
               "approving on one database does not approve another")
        assert e.conn.commits == 0


def test_what_is_written_is_what_was_previewed():
    e = FakeWriteEngine()
    e.conn.objects["f_total()"] = OLD
    with project(e) as p:
        token = p.preview(NEW)["token"]
        p.approve(token)
        e.conn.objects["f_total()"] = OLD + " -- edited by hand"
        expect(p.run("apply", "change.sql", "--confirm", token), 1,
               "changed since that preview")
        assert e.conn.commits == 0 and e.conn.objects["f_total()"].endswith("hand")


def test_a_verification_that_moved_is_rolled_back():
    e = FakeWriteEngine()
    with project(e) as p:
        token = p.preview(NEW)["token"]
        p.approve(token)
        e.conn.broken_after = [("v_report", "VIEW", "column total does not exist")]
        expect(p.run("apply", "change.sql", "--confirm", token), 1,
               "rolled back", "v_report", "preview again")
        assert e.conn.commits == 0
        assert grants.read(p.state, token)["used_at"] is None   # still unspent


def test_approved_breakage_lands_as_written_but_broken():
    e = FakeWriteEngine()
    e.conn.objects["f_total()"] = OLD
    e.conn.broken_after = [("v_report", "VIEW", "column total does not exist")]
    with project(e) as p:
        doc = p.preview(NEW)
        assert doc["rehearsal"]["broken"] == [["v_report", "VIEW",
                                               "column total does not exist"]]
        p.approve(doc["token"])
        code, out, _ = p.run("apply", "change.sql", "--confirm", doc["token"])
        assert code == 3 and e.conn.commits == 1
        assert "broke" in out and "journal restore" in out


def test_a_statement_that_escaped_the_transaction_is_never_called_unwritten():
    # a trigger that commits ends the engine's transaction inside the
    # statement: what ran may already be committed, approved or not
    e = FakeWriteEngine()
    e.conn.objects["f_total()"] = OLD
    e.conn.escape = "a trigger ended the transaction"
    with project(e) as p:
        code, out, why = p.run("apply", p.file(NEW))
        assert code == 3, (code, why, out)
        assert "nothing was written" not in (out + why).lower()
        for needle in ("WRITTEN?", "a trigger ended the transaction",
                       "may already be committed", "nobody approved",
                       "journal restore"):
            assert needle in out, needle
        eid = p.entries()[0]
        entry = journal.read_entry(p.state, eid)
        assert entry["meta"]["escaped"] == "a trigger ended the transaction"
        assert journal.kind(entry["meta"]) == "escaped"
        assert entry["restore"] == OLD and entry["before"] == OLD
        assert e.conn.pending == [] and e.conn.rollbacks >= 1
        code, out, _ = p.run("apply", "change.sql", "--json")
        doc = json.loads(out.strip().splitlines()[-1])
        assert code == 3 and doc["ok"] is False and doc["exit"] == 3
        assert doc["escaped"] and doc["restore"].startswith("pythia-fake journal restore")
        assert journal.find_by_token(p.state, doc.get("token") or "-")[0] is None
        # the approval of a write that escaped is spent: one approval, one run
        e.conn.escape = None
        token = p.preview(NEW)["token"]
        p.approve(token)
        e.conn.escape = "a trigger ended the transaction"
        code, out, _ = p.run("apply", "change.sql", "--confirm", token)
        assert code == 3 and "WRITTEN?" in out
        assert grants.read(p.state, token)["used_at"]


# --- a statement that cannot be rehearsed: predicted, written once, checked after ---

def test_a_prediction_runs_nothing_and_says_so():
    e = FakePredictEngine()
    e.conn.objects["f_total()"] = OLD
    with project(e) as p:
        code, out, why = p.run("apply", p.file(NEW))
        assert code == 0, why
        assert e.conn.predicted == [NEW] and e.conn.executed == []
        assert e.conn.objects["f_total()"] == OLD
        assert "predicted — nothing ran" in out and "ran and rolled back" not in out
        assert "checked again after the write" in out
        doc = p.preview(NEW)
        assert doc["rehearsal"]["predicted"] is True and e.conn.executed == []
        code, card, _ = p.run("approve", "--card", doc["token"])
        assert code == 0 and "Predicted (nothing ran)" in card and "rolled back" not in card
        e.conn.broken_after = [("v_report", "VIEW", "would read what does not exist")]
        code, out, _ = p.run("apply", "change.sql")
        assert "! predicted — nothing ran, and this would break:" in out


def test_a_predicted_write_runs_once_and_only_after_approval():
    e = FakePredictEngine()
    e.conn.objects["f_total()"] = OLD
    with project(e) as p:
        token = p.preview(NEW)["token"]
        expect(p.run("apply", "change.sql", "--confirm", token), 1,
               "no developer approval is on file")
        assert e.conn.executed == []
        p.approve(token)
        code, out, why = p.run("apply", "change.sql", "--confirm", token)
        assert code == 0, why
        assert e.conn.executed == [NEW] and e.conn.objects["f_total()"] == NEW
        assert grants.read(p.state, token)["used_at"] and "journal restore" in out
        meta = journal.read_entry(p.state, p.entries()[0])["meta"]
        assert meta["applied"] and meta["rehearsal"]["predicted"] is True
        assert meta["verified_after"] == {"broken": [], "own_errors": [], "differs": []}
        expect(p.run("apply", "change.sql", "--confirm", token), 1)
        assert e.conn.executed == [NEW]                  # one approval, one write


def test_a_prediction_that_moved_since_approval_writes_nothing():
    e = FakePredictEngine()
    with project(e) as p:
        token = p.preview(NEW)["token"]
        p.approve(token)
        e.conn.broken_after = [("v_report", "VIEW", "3 documents fail the new validator")]
        expect(p.run("apply", "change.sql", "--confirm", token), 1, "moved",
               "v_report", "nothing was written")
        assert e.conn.executed == []
        assert grants.read(p.state, token)["used_at"] is None


def test_a_refused_execute_wrote_nothing_and_keeps_the_approval():
    e = FakePredictEngine()
    with project(e) as p:
        token = p.preview(NEW)["token"]
        p.approve(token)
        e.conn.execute_result = {"error": "11000: duplicate key error"}
        expect(p.run("apply", "change.sql", "--confirm", token), 1, "FAILED",
               "duplicate key", "nothing was written", "unspent")
        assert grants.read(p.state, token)["used_at"] is None
        meta = journal.read_entry(p.state, p.entries()[0])["meta"]
        assert meta["refused"] and not meta.get("applied")


def test_an_execute_that_may_have_landed_is_never_called_unwritten():
    e = FakePredictEngine()
    with project(e) as p:
        token = p.preview(NEW)["token"]
        p.approve(token)
        e.conn.execute_result = {"escaped": "the connection dropped during the write",
                                 "error": "connection reset"}
        code, out, why = p.run("apply", "change.sql", "--confirm", token)
        assert code == 3 and "WRITTEN?" in out and "connection dropped" in out, (code, why)
        assert "nothing was written" not in (out + why).lower()
        assert grants.read(p.state, token)["used_at"]


def test_what_landed_is_checked_against_the_prediction():
    e = FakePredictEngine()
    e.conn.objects["f_total()"] = OLD
    with project(e) as p:
        token = p.preview(NEW)["token"]
        p.approve(token)
        e.conn.differs = ["3 documents fail the validator now; the prediction said 0"]
        code, out, _ = p.run("apply", "change.sql", "--confirm", token)
        assert code == 3 and "differs from the prediction" in out, out
        assert "the prediction said 0" in out and "journal restore" in out
        meta = journal.read_entry(p.state, p.entries()[0])["meta"]
        assert meta["verified_after"]["differs"] == e.conn.differs
        e.conn.differs = []
        token = p.preview(OLD)["token"]
        p.approve(token)
        code, out, _ = p.run("apply", "change.sql", "--confirm", token, "--json")
        doc = json.loads(out.strip().splitlines()[-1])
        assert code == 0 and doc["ok"] and doc["differs"] == []


def test_an_engine_that_predicts_needs_the_predict_tier():
    e = NoPredictTier()
    with project(e) as p:
        expect(p.run("apply", p.file(NEW)), 1, "predict tier", "execute")
        assert e.conn.executed == [] and e.conn.commits == 0


def test_dml_revalidates_the_rows_and_the_rowcount():
    e = FakeWriteEngine()
    e.conn.rows["t_order"] = [1, 2, 3]
    with project(e) as p:
        p.policy(data_dml="confirm")
        doc = p.preview("delete from t_order where id < 3")
        assert doc["row_set"]["count"] == 2 and doc["rehearsal"]["rows"] == 2
        _, text, _ = p.run("apply", "change.sql")
        assert "rehearsed — ran and rolled back: 2 rows" in text
        assert "compiles" not in text                  # says nothing untrue about DML
        p.approve(doc["token"])
        e.conn.rows["t_order"] = [0, 1, 2, 3]          # a row joined the set
        expect(p.run("apply", "change.sql", "--confirm", doc["token"]), 1,
               "row set moved", "2 rows when you approved it, 3 now")
        e.conn.rows["t_order"] = [1, 2, 3]
        e.conn.rowcount = 5                             # the write disagrees
        expect(p.run("apply", "change.sql", "--confirm", doc["token"]), 1,
               "rolled back", "touched 5 rows, but 2 were approved")
        assert e.conn.commits == 0
        e.conn.rowcount = None
        code, out, why = p.run("apply", "change.sql", "--confirm", doc["token"])
        assert code == 0 and e.conn.rows["t_order"] == [3], why
        assert "no undo" in out


def test_yes_is_for_a_person_or_a_pipeline():
    e = FakeWriteEngine()
    with project(e) as p:
        expect(p.run("apply", p.file(NEW), "--yes"), 1, "--yes is the developer's")
        assert e.conn.commits == 0
        os.environ["PYTHIA_FAKE_CI"] = "1"
        assert p.run("apply", "change.sql", "--yes")[0] == 0
        assert e.conn.commits == 1


def test_the_gate_itself_refuses_a_headless_yes():
    """Defence in depth: the command line refuses first, but anything else
    that calls run_apply (deploy, in Phase 3) meets the same refusal."""
    from argparse import Namespace
    from pythia_core import gate
    e = FakeWriteEngine()
    with project(e) as p:
        ns = Namespace(state=p.state, schema="app", conn_name="dev",
                       file="change.sql", confirm=None, yes=True, json=False,
                       depth=3)
        try:
            gate.run_apply(e, e.conn, ns, NEW)
        except SystemExit as stop:
            assert "--yes is the developer's" in str(stop)
        else:
            raise AssertionError("a headless --yes reached the database")
        assert e.conn.rehearsed == [] and e.conn.commits == 0


def test_approve_needs_a_console_and_a_real_preview():
    e = FakeWriteEngine()
    with project(e) as p:
        token = p.preview(NEW)["token"]
        os.environ["PYTHIA_FAKE_CI"] = "1"         # a pipeline is not a person
        expect(p.run("approve", token), 1, "needs a real console")
        assert grants.read(p.state, token) is None
        code, out, _ = p.run("approve", "--card", token)  # the card mints nothing
        assert code == 0 and "f_total" in out and grants.read(p.state, token) is None
        human.FORCE_CONSOLE = True
        expect(p.run("approve", "abc123"), 1, "no pending preview")
        expect(p.run("approve", "../x"), 1, "not a pythia token")
        assert p.run("approve", token)[0] == 0
        assert grants.status(grants.read(p.state, token), "dev") == "ok"


def test_an_applied_token_cannot_be_approved_again():
    e = FakeWriteEngine()
    with project(e) as p:
        token = p.preview(NEW)["token"]
        p.approve(token)
        assert p.run("apply", "change.sql", "--confirm", token)[0] == 0
        human.FORCE_CONSOLE = True
        expect(p.run("approve", token), 1, "already applied")


def test_loosening_policy_needs_a_person_tightening_does_not():
    e = FakeWriteEngine()
    with project(e) as p:
        expect(p.run("policy", "set", "data_dml", "confirm"), 1,
               "developer's decision")
        assert p.run("policy", "set", "source", "deny")[0] == 0
        expect(p.run("apply", p.file(NEW)), 1, "source is set to deny")
        os.environ["PYTHIA_FAKE_CI"] = "1"
        # a write nobody approves is a write nobody saw: allow is for session
        expect(p.run("policy", "set", "source", "allow"), 1,
               "allow is only for session")
        del os.environ["PYTHIA_FAKE_CI"]
        os.environ["PYTHIA_FAKE_CI"] = "1"
        assert p.run("policy", "set", "data_dml", "confirm")[0] == 0
        (p.state / "policy.json").write_text('{"everything": "allow"}', encoding="utf-8")
        expect(p.run("policy"), 1, "unknown policy group 'everything'")
        (p.state / "policy.json").write_text('{"data_dml": "allow"}', encoding="utf-8")
        expect(p.run("apply", p.file("delete from t_order")), 1,
               "allow is only for session")


def test_a_restore_is_a_write_that_previews_first():
    e = FakeWriteEngine()
    e.conn.objects["f_total()"] = OLD
    with project(e) as p:
        token = p.preview(NEW)["token"]
        p.approve(token)
        p.run("apply", "change.sql", "--confirm", token)
        applied = p.entries()[0]
        code, out, _ = p.run("journal", "restore", applied, "--json")
        doc = json.loads(out.strip().splitlines()[-1])
        assert code == 0 and doc["will_apply"] is False
        assert e.conn.commits == 1 and e.conn.objects["f_total()"] == NEW
        p.approve(doc["token"])
        assert p.run("journal", "restore", applied, "--confirm", doc["token"])[0] == 0
        assert e.conn.commits == 2 and e.conn.objects["f_total()"] == OLD
        meta = journal.read_entry(p.state, p.entries()[0])["meta"]
        assert meta["restored_from"] == applied


def test_undoing_a_new_object_is_a_drop_and_says_so():
    e = FakeWriteEngine()
    with project(e) as p:
        token = p.preview(NEW)["token"]
        p.approve(token)
        code, out, _ = p.run("apply", "change.sql", "--confirm", token)
        assert code == 0 and "undo means dropping it" in out.lower()
        assert "structural is set to deny" in out
        expect(p.run("journal", "restore", p.entries()[0]), 1,
               "structural is set to deny")


def test_cross_schema_and_a_drifted_session_are_refused():
    e = FakeWriteEngine()
    with project(e) as p:
        expect(p.run("apply", p.file(NEW.replace("f_total", "other.f_total"))),
               1, "names schema 'other'", "connection targets 'app'")
    drifted = FakeWriteEngine(current="public")
    with project(drifted) as p:
        expect(p.run("apply", p.file(NEW)), 1, "session's schema is 'public'")
        assert drifted.conn.rehearsed == []


def test_a_session_statement_says_it_ends_with_the_connection():
    e = FakeWriteEngine()
    with project(e) as p:
        code, out, _ = p.run("apply", p.file("set work_mem = '64MB'"))
        assert code == 0 and "only for this invocation" in out


def test_a_comment_is_snapshotted_and_restorable():
    e = FakeWriteEngine()
    e.conn.comments["t_order"] = "orders"
    with project(e) as p:
        doc = p.preview("comment on table t_order is 'customer orders'")
        p.approve(doc["token"])
        assert p.run("apply", "change.sql", "--confirm", doc["token"])[0] == 0
        assert e.conn.comments["t_order"] == "customer orders"
        restore = journal.read_entry(p.state, p.entries()[0])["restore"]
        assert restore == "comment on table t_order is 'orders'"


def test_destruction_is_said_out_loud_and_on_the_card():
    e = FakeWriteEngine()
    with project(e) as p:
        p.policy(structural="confirm")
        doc = p.preview("drop table t_order")
        assert doc["destructive"].startswith("DROP removes")
        code, out, _ = p.run("apply", "change.sql")
        assert "! DESTRUCTIVE: DROP removes" in out
        _, card, _ = p.run("approve", "--card", doc["token"])
        assert "DESTRUCTIVE" in card and "no snapshot" in card


def test_state_lives_under_the_engine_folder():
    e = FakeWriteEngine()
    with project(e) as p:
        token = p.preview(NEW)["token"]
        p.approve(token)
        assert (p.root / ".pythia" / "fake" / "journal").is_dir()
        assert (p.root / ".pythia" / "fake" / "grants" / f"{token}.json").is_file()
        assert not (p.root / ".pythia" / "journal").exists()


def test_journal_list_and_history_name_every_version():
    e = FakeWriteEngine()
    e.conn.objects["f_total()"] = OLD
    with project(e) as p:
        token = p.preview(NEW)["token"]
        p.approve(token)
        p.run("apply", "change.sql", "--confirm", token)
        code, out, _ = p.run("journal", "list")
        assert code == 0 and "applied" in out and "preview" in out
        code, out, _ = p.run("history", "F_TOTAL", "--json")
        kinds = [v["kind"] for v in json.loads(out)["versions"]]
        assert kinds == ["applied", "preview"]


# --- the grant file is a trust boundary ---------------------------------------

def test_tokens_are_validated_and_corrupt_grants_do_not_approve():
    with tempfile.TemporaryDirectory() as td:
        state = pathlib.Path(td)
        assert not grants.valid_token("../lease")
        assert not grants.valid_token("") and grants.valid_token("a1b2c3")
        grants.mint(state, "a1b2c3", "dev")
        (state / "grants" / "a1b2c3.json").write_text("{half", encoding="utf-8")
        assert grants.read(state, "a1b2c3") is None
        assert grants.status(None, "dev") == "missing"
        old = datetime.datetime.now() - datetime.timedelta(hours=2)
        grants.mint(state, "d4e5f6", "dev", now=old)
        assert grants.prune(state) == 2
        assert grants.read(state, "d4e5f6") is None


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

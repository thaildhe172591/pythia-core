#!/usr/bin/env python3
"""Adversarial self-checks for the chat approval doors. A grant may come
from three places — the developer's console, Claude Code's AskUserQuestion
dialog (through the `approve --hook` PostToolUse hook) and an MCP
elicitation — and each test tries to get one minted without a person
choosing Approve on pythia's own card: the agent's words instead of the
card, an answer the agent wrote itself, a dialog that closed on its own, a
client that declines for nobody, a hook payload piped in by hand.

Run: python tests/test_doors.py
"""
import contextlib
import io
import json
import os
import pathlib
import sys

import fakes  # noqa: F401 — puts src/ on sys.path
from fakes import FakeHarnessEngine
from test_gate import NEW, project

from pythia_core import cli, doors, grants, harness, human, install  # noqa: E402


def card_of(p, token):
    code, out, why = p.run("approve", "--card", token)
    assert code == 0, why
    return out.strip()


def asked(question, answer=None, **response):
    """An AskUserQuestion PostToolUse payload as Claude Code sends it."""
    resp = dict(response)
    if answer is not None:
        resp["answers"] = {question: answer}
    return {"tool_name": "AskUserQuestion", "session_id": "s1",
            "tool_input": {"questions": [{
                "question": question, "header": "Approve", "multiSelect": False,
                "options": [{"label": "Approve"}, {"label": "Reject"}]}]},
            "tool_response": resp}


def chat(p, payload):
    """Run the hook the way Claude Code does; returns the context it adds."""
    payload = {"cwd": str(p.root), **payload}
    keep = sys.stdin
    sys.stdin = io.StringIO(json.dumps(payload))
    out = io.StringIO()
    try:
        with contextlib.redirect_stdout(out):
            assert cli.main(p.e, ["approve", "--hook"]) == 0
    finally:
        sys.stdin = keep
    text = out.getvalue().strip()
    if not text:
        return ""
    doc = json.loads(text)
    assert doc["hookSpecificOutput"]["hookEventName"] == "PostToolUse"
    return doc["hookSpecificOutput"]["additionalContext"]


# --- the chat door: AskUserQuestion + the approve --hook PostToolUse hook ----------

def test_the_developers_approve_on_pythias_own_card_mints_a_chat_grant():
    with project(FakeHarnessEngine()) as p:
        token = p.preview(NEW)["token"]
        note = chat(p, asked(card_of(p, token), "Approve"))
        assert "approved in chat" in note and f"--confirm {token}" in note
        g = grants.read(p.state, token)
        assert g["approver"] == "chat" and grants.status(g, "dev") == "ok"
        code, out, why = p.run("apply", "change.sql", "--confirm", token)
        assert code == 0, why
        assert p.run("apply", "change.sql", "--confirm", token)[0] == 1   # one approval, one write


def test_the_agents_own_words_are_not_the_card():
    with project(FakeHarnessEngine()) as p:
        token = p.preview(NEW)["token"]
        for question in (f"Shall I apply it? pythia-fake approve {token}",
                         card_of(p, token).replace("f_total", "f_other")):
            note = chat(p, asked(question, "Approve"))
            assert "did not carry pythia's approval card" in note, note
        assert grants.read(p.state, token) is None


def test_anything_but_the_developer_choosing_approve_mints_nothing():
    with project(FakeHarnessEngine()) as p:
        token = p.preview(NEW)["token"]
        card = card_of(p, token)
        cases = ((asked(card, "Reject"), "did NOT approve"),
                 (asked(card, response="why does it drop that?"), "typed a reply"),
                 (asked(card, "Approve", afkTimeoutMs=60000), "closed itself"),
                 (asked(card), "never reached the developer"))
        for payload, needle in cases:
            note = chat(p, payload)
            assert needle in note, (needle, note)
        assert grants.read(p.state, token) is None
        assert "pythia_fake_approve" in chat(p, asked(card))   # the next door, named


def test_an_answer_the_agent_wrote_itself_is_not_the_developers():
    with project(FakeHarnessEngine()) as p:
        token = p.preview(NEW)["token"]
        card = card_of(p, token)
        payload = asked(card)
        payload["tool_input"]["answers"] = {card: "Approve"}   # where Claude Code puts it
        payload["tool_use_id"] = "tu1"

        def call(tid, with_answers, kind="assistant"):
            sent = {"questions": payload["tool_input"]["questions"]}
            if with_answers:
                sent["answers"] = {card: "Approve"}
            return {"type": kind, "message": {"content": [
                {"type": "tool_use", "id": tid, "name": "AskUserQuestion",
                 "input": sent}]}}

        def transcript(model_answers, after=()):
            path = p.root / "transcript.jsonl"
            path.write_text("\n".join(json.dumps(x) for x in (
                {"type": "user", "message": {"content": "go"}},
                call("tu1", model_answers), *after)) + "\n", encoding="utf-8")
            return str(path)

        note = chat(p, payload)                      # no transcript to check
        assert "could not be read" in note and grants.read(p.state, token) is None
        note = chat(p, {**payload, "transcript_path": transcript(True)})
        assert "your own call carried answers" in note
        # a later line is not this call: another tool_use, or a user echo
        note = chat(p, {**payload, "transcript_path": transcript(
            True, after=(call("tu2", False), call("tu1", False, kind="user")))})
        assert "your own call carried answers" in note, note
        assert grants.read(p.state, token) is None
        note = chat(p, {**payload, "transcript_path": transcript(
            False, after=(call("tu2", True),))})
        assert "approved in chat" in note, note
        assert grants.read(p.state, token)["approver"] == "chat"


def test_the_hook_says_nothing_about_other_questions_and_never_crashes():
    with project(FakeHarnessEngine()) as p:
        assert chat(p, asked("Which colour should the button be?", "Blue")) == ""
        assert chat(p, {"tool_name": "Bash", "tool_input": {"command": "ls"}}) == ""
        keep = sys.stdin
        sys.stdin = io.StringIO("{ not json")
        try:
            with contextlib.redirect_stdout(io.StringIO()) as out:
                assert cli.main(p.e, ["approve", "--hook"]) == 0
        finally:
            sys.stdin = keep
        assert out.getvalue() == ""
        note = chat(p, asked("pythia-fake approve abc123\nApproving: x", "Approve"))
        assert "no pending preview" in note.lower()


def test_an_applied_token_cannot_be_approved_again_in_chat():
    with project(FakeHarnessEngine()) as p:
        token = p.preview(NEW)["token"]
        card = card_of(p, token)
        chat(p, asked(card, "Approve"))
        assert p.run("apply", "change.sql", "--confirm", token)[0] == 0
        note = chat(p, asked(card, "Approve"))
        assert "already applied" in note


def test_the_hook_is_the_clients_door_never_the_agents():
    with project(FakeHarnessEngine()) as p:
        exe = pathlib.Path(sys.executable).as_posix()
        for cmd in ("pythia-fake approve --hook < payload.json",
                    "echo '{}' | pythia-fake approve --hook",
                    f'"{exe}" -m fakes approve a1b2c3 --hook < p.json'):
            out = harness.pre_tool_decision(p.e, {
                "tool_name": "Bash", "cwd": str(p.root), "tool_input": {"command": cmd}})
            assert out["hookSpecificOutput"]["permissionDecision"] == "deny", cmd
            assert "forge" in out["hookSpecificOutput"]["permissionDecisionReason"]


# --- the MCP door: an elicitation the developer answers --------------------------------

class Clock:
    def __init__(self, step):
        self.t, self.step = 0.0, step

    def __call__(self):
        self.t += self.step
        return self.t


def test_an_mcp_approve_after_the_card_was_shown_mints_an_mcp_grant():
    with project(FakeHarnessEngine()) as p:
        token = p.preview(NEW)["token"]
        shown = {}

        def elicit(message, schema):
            shown["message"], shown["schema"] = message, schema
            return "accept", {"decision": "Approve"}

        result = doors.mcp_approve_decision(p.e, p.state, token, elicit, Clock(3))
        assert not result["isError"], result
        assert shown["message"] == card_of(p, token)        # the card, verbatim
        assert shown["schema"]["properties"]["decision"]["enum"] == ["Approve", "Reject"]
        assert f"--confirm {token}" in result["content"][0]["text"]
        assert grants.read(p.state, token)["approver"] == "mcp"


def test_an_mcp_prompt_nobody_saw_or_nobody_approved_mints_nothing():
    with project(FakeHarnessEngine()) as p:
        token = p.preview(NEW)["token"]

        def answering(action, decision=None):
            return lambda message, schema: (action, {"decision": decision} if decision else {})

        def broken(message, schema):
            raise RuntimeError("client closed")

        cases = ((answering("decline"), Clock(0.01), "before anyone could read"),
                 (answering("decline"), Clock(5), "did not approve"),
                 (answering("accept", "Reject"), Clock(5), "did not approve"),
                 (answering("accept", "approve"), Clock(5), "did not approve"),
                 (answering("cancel"), Clock(5), "did not approve"),
                 (broken, Clock(5), "could not be shown"))
        for elicit, clock, needle in cases:
            result = doors.mcp_approve_decision(p.e, p.state, token, elicit, clock)
            assert result["isError"] and needle in result["content"][0]["text"], needle
        assert grants.read(p.state, token) is None
        result = doors.mcp_approve_decision(p.e, p.state, "abc123",
                                            answering("accept", "Approve"), Clock(5))
        assert result["isError"] and grants.read(p.state, "abc123") is None


def test_the_mcp_server_speaks_json_rpc_over_stdio():
    with project(FakeHarnessEngine()) as p:
        token = p.preview(NEW)["token"]
        server, tool = doors.mcp_names(p.e)
        assert (server, tool) == ("pythia_fake", "pythia_fake_approve")
        lines = [{"jsonrpc": "2.0", "id": 1, "method": "initialize",
                  "params": {"protocolVersion": "2025-06-18"}},
                 {"jsonrpc": "2.0", "method": "notifications/initialized"},
                 {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                 {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                  "params": {"name": tool, "arguments": {"token": token}}},
                 {"jsonrpc": "2.0", "id": 1001,
                  "result": {"action": "accept", "content": {"decision": "Approve"}}},
                 {"jsonrpc": "2.0", "id": 4, "method": "nope"},
                 {"jsonrpc": "2.0", "id": 5, "method": "tools/call",
                  "params": {"name": "other_tool", "arguments": {"token": token}}}]
        reader = io.StringIO("".join(json.dumps(x) + "\n" for x in lines))
        writer = io.StringIO()
        doors.mcp_serve(p.e, p.state, reader, writer)
        sent = [json.loads(x) for x in writer.getvalue().splitlines()]
        assert sent[0]["result"]["serverInfo"]["name"] == server
        tools = sent[1]["result"]["tools"]
        assert [t["name"] for t in tools] == [tool]
        assert tools[0]["_meta"]["anthropic/requiresUserInteraction"] is True
        assert sent[2]["method"] == "elicitation/create" and sent[2]["id"] == 1001
        assert not sent[3]["result"]["isError"] and sent[3]["id"] == 3
        assert sent[4]["error"]["code"] == -32601
        assert sent[5]["id"] == 5 and "no such tool" in sent[5]["error"]["message"]
        assert grants.read(p.state, token)["approver"] == "mcp"


# --- the console door says where the others are ------------------------------------------

def test_the_preview_and_the_headless_refusal_name_every_door():
    with project(FakeHarnessEngine()) as p:
        token = p.preview(NEW)["token"]
        code, out, _ = p.run("apply", "change.sql")
        for needle in ("AskUserQuestion", f"pythia-fake approve --card {token}",
                       "pythia_fake_approve", f"pythia-fake approve {token}"):
            assert needle in out, needle
        code, out, why = p.run("approve", token)
        assert code == 1 and "AskUserQuestion" in why and "Phase 3" not in why
        human.FORCE_CONSOLE = True                 # the developer, at a console
        assert p.run("approve", token)[0] == 0
        assert grants.read(p.state, token)["approver"] == "console"


# --- install wires the doors, and doctor sees what would break them -----------------------

@contextlib.contextmanager
def home():
    import tempfile
    keep = {k: os.environ.get(k) for k in ("HOME", "USERPROFILE")}
    with tempfile.TemporaryDirectory() as td:
        os.environ["HOME"] = os.environ["USERPROFILE"] = td
        try:
            yield pathlib.Path(td)
        finally:
            for k, v in keep.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v


def run(p, *argv):
    out = io.StringIO()
    try:
        with contextlib.redirect_stdout(out):
            code = cli.main(p.e, list(argv))
    except SystemExit as e:
        code = e.code
    return code, out.getvalue()


def test_install_wires_the_answer_hook_and_registers_the_approver_locally():
    with project(FakeHarnessEngine()) as p, home() as h:
        claude = h / ".claude.json"
        claude.write_text(json.dumps({"theme": "dark", "projects": {
            "/elsewhere": {"mcpServers": {"x": {"command": "x"}}}}}), encoding="utf-8")
        codex = h / ".codex" / "config.toml"
        codex.parent.mkdir()
        codex.write_text('model = "o4"\n', encoding="utf-8")
        assert run(p, "install")[0] == 0
        settings = json.loads((p.root / ".claude" / "settings.local.json")
                              .read_text(encoding="utf-8"))
        post = settings["hooks"]["PostToolUse"]
        assert [e["matcher"] for e in post] == ["AskUserQuestion"]
        assert "approve --hook" in post[0]["hooks"][0]["command"]
        doc = json.loads(claude.read_text(encoding="utf-8"))
        assert doc["theme"] == "dark" and "/elsewhere" in doc["projects"]
        entry = doc["projects"][install.project_key(p.root)]["mcpServers"]["pythia_fake"]
        assert entry["command"] == sys.executable and entry["args"] == ["-m", "fakes", "mcp"]
        toml = codex.read_text(encoding="utf-8")
        assert toml.startswith('model = "o4"\n') and "[mcp_servers.pythia_fake]" in toml
        before = (claude.read_bytes(), codex.read_bytes())
        code, out = run(p, "install")
        assert code == 0 and (claude.read_bytes(), codex.read_bytes()) == before
        again = [ln for ln in out.splitlines() if "~/." in ln]
        assert again and all(ln.startswith("unchanged") for ln in again), again
        code, out = run(p, "doctor", "--json")
        rows = {r["what"]: r for r in json.loads(out)["checks"]}
        assert code == 0, out
        assert rows["chat door"]["status"] == "ok" and rows["mcp approver"]["status"] == "ok"


def test_install_never_overwrites_what_it_cannot_merge():
    with project(FakeHarnessEngine()) as p, home() as h:
        (h / ".claude.json").write_text("{ broken", encoding="utf-8")
        codex = h / ".codex" / "config.toml"
        codex.parent.mkdir()
        codex.write_text('[mcp_servers.pythia_fake]\ncommand = "mine"\n', encoding="utf-8")
        code, out = run(p, "install")
        assert code == 0
        assert (h / ".claude.json").read_text(encoding="utf-8") == "{ broken"
        assert codex.read_text(encoding="utf-8") == '[mcp_servers.pythia_fake]\ncommand = "mine"\n'
        assert "SKIPPED" in out and "~/.codex/config.toml" in out


def test_doctor_names_the_settings_that_answer_for_the_developer():
    with project(FakeHarnessEngine()) as p, home():
        run(p, "install")
        shared = p.root / ".claude" / "settings.json"
        for settings, needle in (
                ({"permissions": {"defaultMode": "dontAsk"}}, "dontAsk"),
                ({"permissions": {"allow": ["AskUserQuestion"]}}, "allow"),
                ({"hooks": {"PreToolUse": [{"matcher": "*", "hooks": [
                    {"type": "command", "command": "audit"}]}]}}, "matcher")):
            shared.write_text(json.dumps(settings), encoding="utf-8")
            code, out = run(p, "doctor", "--json")
            row = {r["what"]: r for r in json.loads(out)["checks"]}["chat door"]
            assert row["status"] == "warn" and needle in row["detail"], (needle, row)
        shared.unlink()
        assert {r["what"]: r for r in json.loads(run(p, "doctor", "--json")[1])["checks"]}[
            "chat door"]["status"] == "ok"


def test_the_brief_names_the_doors_in_order():
    with project(FakeHarnessEngine()) as p:
        text = harness.brief(p.e)
        order = [text.index(s) for s in ("AskUserQuestion", "pythia_fake_approve",
                                         "pythia-fake approve <token>")]
        assert order == sorted(order), text
        assert "pythia-fake approve --card <token>" in text
        assert install.instructions_block(p.e).count("AskUserQuestion") >= 1


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

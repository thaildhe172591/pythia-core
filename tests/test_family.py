"""Several engines in one project (Phase 6).

Each engine's install, doctor, guard and answer hook is written for one
engine, and a project may run pythia-pg, pythia-mssql and pythia-mongo side
by side — next to pythia for Oracle, which is not built on this core and
writes its own block and hooks. Nothing one engine does may undo another's
files, deny another's commands, or answer another's approval card: Claude
Code runs every hook on every tool call, and a word from the wrong engine
("no pending preview carries that token") contradicts the right one.

Two fake engines stand in for the family: FakeHarnessEngine, and OtherEngine
below — a separate package, as every real engine is.
"""
import contextlib
import io
import json
import os
import pathlib
import sys
import tempfile

import fakes  # noqa: F401 — puts src/ on sys.path
from fakes import FakeHarnessEngine
from test_gate import NEW

from pythia_core import cli, grants, harness, human, install  # noqa: E402

ORACLE_BLOCK = ("<!-- pythia:begin (managed by `pythia install`; edit outside this "
                "block) -->\n## pythia — Oracle\n\nThe Oracle rules.\n<!-- pythia:end -->\n")
ORACLE_HOOK = '"/opt/py/bin/python" "/opt/pythia/scripts/pythia.py" '
ORACLE_SETTINGS = {
    "hooks": {
        "SessionStart": [{"hooks": [{"type": "command",
                                     "command": ORACLE_HOOK + "hook session-start"}]}],
        "PreToolUse": [{"matcher": "Bash", "hooks": [
            {"type": "command", "command": ORACLE_HOOK + "hook pre-tool"}]}],
        "PostToolUse": [{"matcher": "AskUserQuestion", "hooks": [
            {"type": "command", "command": ORACLE_HOOK + "approve --hook"}]}]},
    "permissions": {"deny": ["Bash(pythia approve --hook*)"]},
    "model": "the developer's own setting",
}


class OtherEngine(FakeHarnessEngine):
    name = "other"
    label = "OtherDB"
    command = "pythia-other"
    env_prefix = "PYTHIA_OTHER"
    pack = None                      # set per test: its own skill pack

    def raw_client_pattern(self):
        return r"othersql"

    def skills_dir(self):
        return self.pack


ENGINES = (FakeHarnessEngine(), OtherEngine())


@contextlib.contextmanager
def family():
    """A project with the team's own CLAUDE.md, pythia for Oracle's block and
    hooks, and a filled-in connection for each fake engine."""
    keep_env = {k: os.environ.pop(k) for k in list(os.environ)
                if k.startswith("PYTHIA_") or k in ("HOME", "USERPROFILE")}
    keep_cwd = os.getcwd()
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as home, \
            tempfile.TemporaryDirectory() as packs:
        os.environ["HOME"] = os.environ["USERPROFILE"] = home
        root = pathlib.Path(td).resolve()
        pack = pathlib.Path(packs) / "other-explore"
        pack.mkdir()
        (pack / "SKILL.md").write_text(
            "---\nname: other-explore\ndescription: Use when exploring the other "
            "database.\n---\n\n# other-explore\n\n**Phase:** Learn\n", encoding="utf-8")
        OtherEngine.pack = pack.parent
        (root / "CLAUDE.md").write_text("# Team notes\n\nOur own rules.\n\n" + ORACLE_BLOCK,
                                        encoding="utf-8")
        (root / ".claude").mkdir()
        (root / ".claude" / "settings.local.json").write_text(
            json.dumps(ORACLE_SETTINGS, indent=2), encoding="utf-8")
        for e in ENGINES:
            cfg = root / ".pythia" / e.name / "connections.json"
            cfg.parent.mkdir(parents=True)
            cfg.write_text(json.dumps({"default": "dev", "dev": {
                "host": "localhost", "user": "u", "schema": "app"}}), encoding="utf-8")
            if os.name == "posix":
                os.chmod(cfg, 0o600)
        os.chdir(root)
        human.FORCE_CONSOLE = False
        try:
            yield root
        finally:
            os.chdir(keep_cwd)
            human.FORCE_CONSOLE = None
            for k in [k for k in os.environ if k.startswith("PYTHIA_")] + ["HOME", "USERPROFILE"]:
                os.environ.pop(k, None)
            os.environ.update(keep_env)


def run(engine, *argv, stdin=None):
    """(exit code, stdout, the refusal message or '')"""
    out = io.StringIO()
    keep = sys.stdin
    if stdin is not None:
        sys.stdin = io.StringIO(stdin)
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            code = cli.main(engine, list(argv))
        return code or 0, out.getvalue(), ""
    except SystemExit as e:
        if isinstance(e.code, int) or e.code is None:
            return e.code or 0, out.getvalue(), ""
        return 1, out.getvalue(), str(e)
    finally:
        sys.stdin = keep


def install_all(*engines):
    for e in engines:
        code, out, why = run(e, "install")
        assert code == 0, (e.command, why, out)


def snapshot(root):
    return {p.relative_to(root).as_posix(): p.read_bytes()
            for p in sorted(root.rglob("*")) if p.is_file()}


def hook_commands(root):
    s = json.loads((root / ".claude" / "settings.local.json").read_text(encoding="utf-8"))
    return [(event, entry.get("matcher"), h["command"])
            for event, entries in s["hooks"].items() for entry in entries
            for h in entry["hooks"]], s


# --- install and doctor ---------------------------------------------------------------

def test_installs_compose_with_each_other_and_with_pythia_for_oracle():
    with family() as root:
        before, _ = hook_commands(root)
        install_all(*ENGINES)
        hooks, settings = hook_commands(root)
        assert all(h in hooks for h in before), "an install dropped pythia for Oracle's hooks"
        for e in ENGINES:
            mine = [h for h in hooks if install._ours(e, h[2])]
            assert len(mine) == 4, (e.command, mine)   # session, two guards, the answer hook
            assert all(r in settings["permissions"]["deny"] for r in install.deny_rules(e))
        assert len(hooks) == len(before) + 8
        assert settings["model"] == "the developer's own setting"
        assert "Bash(pythia approve --hook*)" in settings["permissions"]["deny"]
        claude = (root / "CLAUDE.md").read_text(encoding="utf-8")
        assert claude.startswith("# Team notes\n\nOur own rules.\n\n" + ORACLE_BLOCK)
        for e in ENGINES:
            assert claude.count(install.markers(e)[0]) == 1, e.command
            assert install.instructions_block(e) in claude
        ignored = (root / ".gitignore").read_text(encoding="utf-8")
        for e in ENGINES:
            assert all(line in ignored for line in install.ignore_lines(e))
        for home in install.SKILL_HOMES:
            assert (root / home / "fake-explore" / "SKILL.md").is_file()
            assert (root / home / "other-explore" / "SKILL.md").is_file()


def test_a_second_install_in_either_order_changes_nothing():
    with family() as root:
        install_all(*ENGINES)
        first = snapshot(root)
        for order in (list(reversed(ENGINES)), list(ENGINES), [ENGINES[0]]):
            install_all(*order)        # an engine's hooks stay where they stood
            assert snapshot(root) == first, [e.command for e in order]


def test_each_doctor_passes_beside_the_others():
    with family() as root:
        install_all(*ENGINES)
        for e in ENGINES:
            code, out, why = run(e, "doctor")
            assert code == 0, (e.command, why, out)
            assert "fail" not in out.lower(), out


# --- the guards --------------------------------------------------------------------------

def decide(engine, root, command):
    out = harness.pre_tool_decision(engine, {
        "tool_name": "Bash", "tool_input": {"command": command}, "cwd": str(root)})
    return out and out["hookSpecificOutput"]["permissionDecision"]


def test_each_guard_denies_its_own_client_and_leaves_the_others_commands_alone():
    fake, other = ENGINES
    with family() as root:
        install_all(*ENGINES)
        cases = {
            'fakesql -c "drop table t_order"': ("deny", None),
            'othersql -c "drop table t_order"': (None, "deny"),
            "pythia-fake ls t_order": ("allow", None),
            "pythia-other ls t_order": (None, "allow"),
            "pythia-other grep fakesql": (None, "allow"),      # a mention, no write
            "pythia-other apply change.sql": (None, "allow"),
            "pythia-other approve abc123": (None, None),
        }
        for command, (want_fake, want_other) in cases.items():
            assert decide(fake, root, command) == want_fake, (fake.command, command)
            assert decide(other, root, command) == want_other, (other.command, command)
        for e in ENGINES:      # each guard keeps its own engine's passwords
            secret = root / ".pythia" / e.name / "connections.json"
            out = harness.pre_tool_decision(e, {"tool_name": "Read", "cwd": str(root),
                                                "tool_input": {"file_path": str(secret)}})
            assert out["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_each_session_brief_speaks_only_for_its_own_engine():
    fake, other = ENGINES
    with family() as root:
        install_all(*ENGINES)
        payload = json.dumps({"hook_event_name": "SessionStart", "cwd": str(root)})
        briefs = {e.command: run(e, "hook", "session-start", stdin=payload)[1] for e in ENGINES}
        assert "pythia-fake" in briefs["pythia-fake"] and "pythia-other" not in briefs["pythia-fake"]
        assert "pythia-other" in briefs["pythia-other"] and "pythia-fake" not in briefs["pythia-other"]


# --- the chat door: every answer hook sees every question ----------------------------

def asked(question, answer=None):
    resp = {"answers": {question: answer}} if answer is not None else {}
    return json.dumps({"tool_name": "AskUserQuestion", "session_id": "s1",
                       "cwd": os.getcwd(), "tool_response": resp,
                       "tool_input": {"questions": [{
                           "question": question, "header": "Approve", "multiSelect": False,
                           "options": [{"label": "Approve"}, {"label": "Reject"}]}]}})


def answer_hook(engine, payload):
    code, out, _ = run(engine, "approve", "--hook", stdin=payload)
    assert code == 0
    return out.strip()


def preview(engine, root):
    (root / "change.sql").write_text(NEW + "\n", encoding="utf-8")
    code, out, why = run(engine, "apply", "change.sql", "--json")
    assert code == 0, why
    token = json.loads(out.strip().splitlines()[-1])["token"]
    code, card, why = run(engine, "approve", "--card", token)
    assert code == 0, why
    return token, card.strip()


def test_an_answer_hook_leaves_another_engines_card_alone():
    fake, other = ENGINES
    with family() as root:
        install_all(*ENGINES)
        token, card = preview(other, root)
        for answer in ("Reject", None, "Approve"):
            assert answer_hook(fake, asked(card, answer)) == "", answer
        assert grants.read(root / ".pythia" / "fake", token) is None
        note = json.loads(answer_hook(other, asked(card, "Approve")))
        assert "approved in chat" in note["hookSpecificOutput"]["additionalContext"]
        assert grants.read(root / ".pythia" / "other", token)["approver"] == "chat"


def test_a_card_from_pythia_for_oracle_is_left_to_pythia():
    with family() as root:
        install_all(*ENGINES)
        for spelled in ("pythia approve 1a2b3c", '"/opt/py/bin/python" pythia.py approve 1a2b3c',
                        "python -m pythia approve --card 1a2b3c"):
            for e in ENGINES:
                assert answer_hook(e, asked(spelled + "\nApproving: PKG_ORDER", "Approve")) == "", \
                    (e.command, spelled)


def test_a_question_that_names_no_tool_is_still_answered():
    # the agent's own words around a token: each engine says why it cannot
    # count — that feedback is how the agent learns to ask with the card
    fake, _ = ENGINES
    with family() as root:
        install_all(*ENGINES)
        token, _card = preview(fake, root)
        note = answer_hook(fake, asked(f"Shall I go ahead? approve {token}", "Approve"))
        assert "did not carry pythia's approval card" in note, note
        assert grants.read(root / ".pythia" / "fake", token) is None


def test_the_engine_spelled_as_a_module_still_owns_its_card():
    fake, _ = ENGINES
    with family() as root:
        install_all(*ENGINES)
        token, card = preview(fake, root)
        spelled = card.replace(f"{fake.command} approve", f"python -m {harness.module_of(fake)} approve", 1)
        assert spelled != card
        note = answer_hook(fake, asked(spelled, "Approve"))
        assert "approved in chat" in note, note      # the card's body is what was approved


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
    print("OK" if not failed else f"{failed} test(s) failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

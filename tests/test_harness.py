#!/usr/bin/env python3
"""Self-checks for the harness: the PreToolUse guard (each test tries to get
a write, a forged approval or a secret past it), the SessionStart brief,
install and doctor. No database, no agent: hook payloads are built by hand.

Run: python tests/test_harness.py
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

from pythia_core import cli, grants, harness, skills  # noqa: E402


@contextlib.contextmanager
def project(config=True):
    keep_env = {k: os.environ.pop(k) for k in list(os.environ)
                if k.startswith("PYTHIA_")}
    keep_cwd = os.getcwd()
    with tempfile.TemporaryDirectory() as td:
        root = pathlib.Path(td).resolve()
        if config:
            cfg = root / ".pythia" / "fake" / "connections.json"
            cfg.parent.mkdir(parents=True)
            cfg.write_text(json.dumps({"default": "dev", "dev": {
                "host": "localhost", "user": "u", "schema": "app"}}),
                encoding="utf-8")
            if os.name == "posix":
                os.chmod(cfg, 0o600)
        os.chdir(root)
        try:
            yield root
        finally:
            os.chdir(keep_cwd)
            os.environ.update(keep_env)


def bash(root, command):
    return harness.pre_tool_decision(FakeHarnessEngine(), {
        "tool_name": "Bash", "tool_input": {"command": command}, "cwd": str(root)})


def tool(root, name, **tool_input):
    return harness.pre_tool_decision(FakeHarnessEngine(), {
        "tool_name": name, "tool_input": tool_input, "cwd": str(root)})


def verdict(out):
    if out is None:
        return None
    return out["hookSpecificOutput"]["permissionDecision"]


def reason(out):
    return out["hookSpecificOutput"]["permissionDecisionReason"]


# --- the guard: writes around the gate ----------------------------------------------

def test_raw_client_writes_are_denied_and_reads_are_left_alone():
    with project() as root:
        out = bash(root, "fakesql -c 'drop table t_order'")
        assert verdict(out) == "deny" and "pythia-fake apply" in reason(out)
        assert verdict(bash(root, "cd x && /usr/bin/fakesql -c \"update t set x=1\"")) == "deny"
        assert verdict(bash(root, "fakesql -f release.sql")) == "deny"
        assert verdict(bash(root, "fakesql app < release.sql")) == "deny"
        assert bash(root, "fakesql -c 'select 1'") is None
        assert bash(root, "grep -r 'drop table' docs/") is None   # not a client


def test_raw_sql_tools_are_denied_writes():
    with project() as root:
        out = tool(root, "mcp__db__run_sql", sql="DROP TABLE t_order")
        assert verdict(out) == "deny"
        assert tool(root, "mcp__db__run_sql", sql="select 1") is None
        assert tool(root, "mcp__pythia_fake__approve", token="abc123") is None


# --- the guard: approvals, policy and secrets ----------------------------------------

def test_grants_cannot_be_written_by_hand():
    with project() as root:
        for cmd in ("echo '{}' > .pythia/fake/grants/a1b2c3.json",
                    f"cp x.json {root}/.pythia/fake/grants/a1b2c3.json",
                    "python -c \"from pythia_core import grants; grants.mint(p, t, c)\"",
                    "python -c 'import pythia_core.human as h; h.FORCE_CONSOLE = True'"):
            out = bash(root, cmd)
            assert verdict(out) == "deny", cmd
            assert "forg" in reason(out), cmd
        for name in ("Write", "Edit", "MultiEdit"):
            out = tool(root, name, file_path=str(root / ".pythia/fake/grants/a1b2c3.json"))
            assert verdict(out) == "deny" and "forg" in reason(out), name


def test_the_pipeline_escape_is_not_the_agents():
    with project() as root:
        for cmd in ("PYTHIA_FAKE_CI=1 pythia-fake apply change.sql --yes",
                    "export PYTHIA_FAKE_CI=1",
                    "env PYTHIA_FAKE_CI=yes pythia-fake policy set data_dml confirm"):
            out = bash(root, cmd)
            assert verdict(out) == "deny" and "pipeline" in reason(out), cmd


def test_the_policy_file_is_changed_only_through_policy_set():
    with project() as root:
        out = tool(root, "Write", file_path=str(root / ".pythia/fake/policy.json"),
                   content='{"data_dml": "confirm"}')
        assert verdict(out) == "deny" and "pythia-fake policy set" in reason(out)
        assert verdict(bash(root, "echo '{}' > .pythia/fake/policy.json")) == "deny"
        assert tool(root, "Read", file_path=str(root / ".pythia/fake/policy.json")) is None


def test_connections_are_never_read_by_the_agent():
    with project() as root:
        out = tool(root, "Read", file_path=str(root / ".pythia/fake/connections.json"))
        assert verdict(out) == "deny" and "pythia-fake connections" in reason(out)
        assert verdict(bash(root, "cat .pythia/fake/connections.json")) == "deny"
        assert verdict(bash(root, "pythia-fake connections")) == "allow"


def test_a_path_spelled_another_way_is_the_same_file():
    with project() as root:
        state = root / ".pythia" / "fake"
        (state / "grants").mkdir()
        for spelled in (".pythia/fake/../fake/grants/a1b2c3.json",
                        "./.pythia/./fake/grants/a1b2c3.json",
                        ".PYTHIA/Fake/Grants/a1b2c3.json"):
            assert verdict(tool(root, "Write", file_path=spelled)) == "deny", spelled
        assert verdict(tool(root, "Read", file_path=".pythia/fake/../fake/connections.json")) == "deny"
        if os.name == "posix":
            (root / "notes.txt").symlink_to(state / "connections.json")
            assert verdict(tool(root, "Read", file_path=str(root / "notes.txt"))) == "deny"
        sub = root / "app"
        sub.mkdir()
        out = harness.pre_tool_decision(FakeHarnessEngine(), {
            "tool_name": "Read", "cwd": str(sub),
            "tool_input": {"file_path": "../.pythia/fake/connections.json"}})
        assert verdict(out) == "deny"
        assert tool(root, "Write", file_path="src/pkg_order.sql") is None
        assert tool(root, "Write", file_path=".pythia/other/policy.json") is None


def test_a_client_behind_a_wrapper_is_still_the_client():
    with project() as root:
        for cmd in ("bash -c 'fakesql -c \"drop table t\"'",
                    "sudo fakesql -f x.sql", "env A=1 fakesql -f x.sql",
                    "echo 'delete from t' | fakesql",
                    "fakesql <<EOF\ndrop table t;\nEOF",
                    "FAKESQL.EXE -c 'drop table t'"):
            assert verdict(bash(root, cmd)) == "deny", cmd


def test_only_sql_tools_are_read_for_sql():
    with project() as root:
        assert tool(root, "mcp__github__create_issue",
                    body="This migration will DROP TABLE t_order_old") is None
        assert verdict(tool(root, "mcp__warehouse__execute_query",
                            statement="delete from t_order")) == "deny"
        assert verdict(tool(root, "mcp__warehouse__run_sql",
                            params={"batch": ["select 1", "drop table t_order"]})) == "deny"


def test_an_engine_without_the_harness_tier_is_refused_and_its_hook_is_silent():
    from fakes import FakeWriteEngine
    with project() as root:
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                cli.main(FakeWriteEngine(), ["install"])
            raise AssertionError("install ran without the harness tier")
        except SystemExit as e:
            assert "harness tier" in str(e)
        keep = sys.stdin
        sys.stdin = io.StringIO(json.dumps({"tool_name": "Bash", "cwd": str(root),
                                            "tool_input": {"command": "fakesql -f x"}}))
        out = io.StringIO()
        try:
            with contextlib.redirect_stdout(out):
                assert cli.main(FakeWriteEngine(), ["hook", "pre-tool"]) == 0
        finally:
            sys.stdin = keep
        assert out.getvalue() == ""
        assert not (root / ".claude").exists()


# --- the guard: what it lets through without asking ----------------------------------

def test_reads_and_previews_pass_and_approved_writes_pass():
    with project() as root:
        assert verdict(bash(root, "pythia-fake ls 't_%'")) == "allow"
        assert verdict(bash(root, "pythia-fake impact t_order --json")) == "allow"
        assert verdict(bash(root, "pythia-fake apply change.sql")) == "allow"
        assert verdict(bash(root, "pythia-fake journal restore x")) == "allow"
        assert bash(root, "pythia-fake apply change.sql --confirm a1b2c3") is None
        grants.mint(root / ".pythia" / "fake", "a1b2c3", "dev")
        out = bash(root, "pythia-fake apply change.sql --confirm a1b2c3")
        assert verdict(out) == "allow" and "approved" in reason(out)
        grants.mint(root / ".pythia" / "fake", "d4e5f6", "prod")
        assert bash(root, "pythia-fake apply change.sql --confirm d4e5f6") is None


def test_plan_mode_gets_no_preview_or_write_waved_through():
    with project() as root:
        grants.mint(root / ".pythia" / "fake", "a1b2c3", "dev")
        for cmd in ("pythia-fake apply change.sql",
                    "pythia-fake apply change.sql --confirm a1b2c3"):
            out = harness.pre_tool_decision(FakeHarnessEngine(), {
                "tool_name": "Bash", "cwd": str(root), "permission_mode": "plan",
                "tool_input": {"command": cmd}})
            assert out is None, cmd


def test_anything_a_shell_could_turn_into_two_commands_is_left_to_the_client():
    with project() as root:
        for cmd in ("pythia-fake ls x; rm -rf build", "pythia-fake ls x && make",
                    "pythia-fake ls $(whoami)", "pythia-fake ls `id`",
                    "pythia-fake ls x # comment", "pythia-fake ls x\nrm y",
                    "pythia-fake approve a1b2c3", "pythia-fake apply f.sql --yes",
                    "pythia-fake policy set data_dml confirm",
                    "pythia-fake --help-me-please", "./pythia-fake ls x",
                    "pythia-fake skill lint && touch x",
                    "pythia-fake skill lint > x"):
            assert bash(root, cmd) is None, cmd


def test_the_interpreter_spelling_is_this_interpreter_and_this_package():
    with project() as root:
        exe = pathlib.Path(sys.executable).as_posix()
        assert verdict(bash(root, f'"{exe}" -m fakes ls x')) == "allow"
        assert bash(root, f'"{exe}" -m fakes_other ls x') is None
        rel = os.path.relpath(sys.executable, root).replace(os.sep, "/")
        assert bash(root, f"{rel} -m fakes ls x") is None
        (root / "fakes.py").write_text("print('not the engine')", encoding="utf-8")
        assert bash(root, f'"{exe}" -m fakes ls x') is None


def test_a_broken_payload_never_decides():
    with project() as root:
        assert harness.pre_tool_decision(FakeHarnessEngine(), "not a dict") is None
        assert harness.pre_tool_decision(FakeHarnessEngine(), {"tool_name": "Bash"}) is None
        assert tool(root, "Glob", pattern="**/*.sql") is None


# --- the hook entry point --------------------------------------------------------------

def run_hook(event, payload):
    keep = sys.stdin
    sys.stdin = io.StringIO(json.dumps(payload))
    out = io.StringIO()
    try:
        with contextlib.redirect_stdout(out):
            cli.main(FakeHarnessEngine(), ["hook", event])
    finally:
        sys.stdin = keep
    return out.getvalue()


def test_the_hook_command_speaks_claude_codes_protocol():
    with project() as root:
        printed = run_hook("pre-tool", {"tool_name": "Bash", "cwd": str(root),
                                        "tool_input": {"command": "fakesql -c 'drop table t'"}})
        doc = json.loads(printed)
        assert doc["hookSpecificOutput"]["hookEventName"] == "PreToolUse"
        assert doc["hookSpecificOutput"]["permissionDecision"] == "deny"
        assert run_hook("pre-tool", {"tool_name": "Bash", "cwd": str(root),
                                     "tool_input": {"command": "ls"}}) == ""


def test_the_session_brief_carries_the_rules_and_the_state():
    with project() as root:
        text = run_hook("session-start", {"cwd": str(root)})
        assert text.startswith("PYTHIA-FAKE HARNESS ACTIVE")
        for needle in ("impact before any change", "pythia-fake apply",
                       "pythia-fake approve <token>", "exit 3",
                       "connections: dev*", "data_dml deny"):
            assert needle in text, needle
    with project(config=False) as root:
        assert "pythia-fake install" in run_hook("session-start", {"cwd": str(root)})


# --- install and doctor --------------------------------------------------------------------

def install(root, *extra):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = cli.main(FakeHarnessEngine(), ["install", *extra])
    return code, out.getvalue()


def doctor(root):
    out = io.StringIO()
    try:
        with contextlib.redirect_stdout(out):
            code = cli.main(FakeHarnessEngine(), ["doctor", "--json"])
    except SystemExit as e:
        code = e.code
    return code, json.loads(out.getvalue())


def test_install_wires_everything_and_leaves_what_is_not_its_own():
    with project(config=False) as root:
        (root / "CLAUDE.md").write_text("# My project\n\nOur own notes.\n\n"
                                        "<!-- pythia:begin (managed by `pythia install`; "
                                        "edit outside this block) -->\nOracle's block\n"
                                        "<!-- pythia:end -->\n", encoding="utf-8")
        (root / ".gitignore").write_text("node_modules/\n", encoding="utf-8")
        code, _ = install(root)
        assert code == 0
        cfg = root / ".pythia" / "fake" / "connections.json"
        assert json.loads(cfg.read_text(encoding="utf-8"))["default"] == "dev"
        if os.name == "posix":
            assert cfg.stat().st_mode & 0o777 == 0o600
        ignore = (root / ".gitignore").read_text(encoding="utf-8")
        assert ".pythia/fake/connections.json" in ignore and "node_modules/" in ignore
        settings = json.loads((root / ".claude" / "settings.local.json").read_text(encoding="utf-8")) \
            if (root / ".claude" / "settings.local.json").is_file() else \
            json.loads((root / ".claude" / "settings.json").read_text(encoding="utf-8"))
        hooks = json.dumps(settings["hooks"])
        assert "hook pre-tool" in hooks and "hook session-start" in hooks
        assert "Read(./.pythia/fake/connections.json)" in settings["permissions"]["deny"]
        claude = (root / "CLAUDE.md").read_text(encoding="utf-8")
        assert claude.startswith("# My project\n\nOur own notes.")
        assert "Oracle's block" in claude and "<!-- pythia-fake:begin" in claude
        assert "<!-- pythia-fake:begin" in (root / "AGENTS.md").read_text(encoding="utf-8")
        for base in (".claude", ".agents"):
            skill = root / base / "skills" / "fake-explore"
            assert (skill / "SKILL.md").is_file() and (skill / "reference" / "more.md").is_file()
        before = {p: p.read_bytes() for p in root.rglob("*") if p.is_file()}
        cfg.write_text('{"dev": {"password": "real"}}', encoding="utf-8")
        before[cfg] = cfg.read_bytes()
        code, printed = install(root)
        assert code == 0
        after = {p: p.read_bytes() for p in root.rglob("*") if p.is_file()}
        assert after == before          # a second install changes nothing, secrets least
        acted = [ln for ln in printed.splitlines()
                 if ln.split(" ", 1)[0] in ("created", "updated", "added")]
        assert not acted, acted


def test_doctor_says_what_is_wired_and_what_is_not():
    with project(config=False) as root:
        code, doc = doctor(root)
        assert code == 1 and any(c["status"] == "fail" for c in doc["checks"])
        install(root)
        code, doc = doctor(root)
        bad = [c for c in doc["checks"] if c["status"] == "fail"]
        assert code == 0 and not bad, bad
        local = root / ".claude" / "settings.local.json"
        moved = local.read_text(encoding="utf-8").replace(
            json.dumps(pathlib.Path(sys.executable).as_posix())[1:-1],
            (root / "gone" / "python").as_posix())
        local.write_text(moved, encoding="utf-8")
        code, doc = doctor(root)
        assert code == 1
        assert any("cannot start" in c["detail"] for c in doc["checks"]), doc
        install(root)
        assert doctor(root)[0] == 0         # a re-install respells the hook
        for path in (root / ".claude").glob("settings*.json"):
            path.write_text("{}", encoding="utf-8")
        code, doc = doctor(root)
        assert code == 1
        assert any("hook" in c["what"] and c["status"] == "fail" for c in doc["checks"])


# --- skills ---------------------------------------------------------------------------------

def test_skill_lint_holds_the_contract():
    good = FakeHarnessEngine().skills_dir() / "fake-explore" / "SKILL.md"
    assert skills.lint(good) == []
    with tempfile.TemporaryDirectory() as td:
        bad = pathlib.Path(td) / "wrong-name" / "SKILL.md"
        bad.parent.mkdir()
        bad.write_text("---\nname: other\ndescription: Explores things: a lot\n---\n"
                       "see reference/missing.md\n" + "x\n" * 160, encoding="utf-8")
        found = "\n".join(skills.lint(bad))
        for needle in ("must match the folder", "unquoted ': '", "Use when",
                       "max 150", "Phase:", "reference/missing.md"):
            assert needle in found, needle


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

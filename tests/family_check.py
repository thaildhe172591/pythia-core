#!/usr/bin/env python3
"""The real engines in one project: what tests/test_family.py proves with
two fakes, checked with pythia-pg, pythia-mssql, pythia-mongo and
pythia-mysql installed side by side — and pythia for Oracle's install
before them, when its checkout is named.

    python tests/family_check.py [--oracle PATH/TO/pythia]

Needs the four engines installed in this interpreter (CI's `family` job
installs them from the branch of the same name, or main). Touches no
database: everything runs in a temporary project and a temporary home.
"""
import argparse
import json
import os
import pathlib
import subprocess
import sys
import tempfile

ENGINES = {"pythia-pg": ("pythia_postgres", "postgres"),
           "pythia-mssql": ("pythia_mssql", "mssql"),
           "pythia-mongo": ("pythia_mongo", "mongo"),
           "pythia-mysql": ("pythia_mysql", "mysql")}
PY = sys.executable


def ok(what):
    print(f"PASS {what}")


def run(argv, root, stdin=None):
    r = subprocess.run(argv, cwd=root, input=stdin, capture_output=True, text=True,
                       timeout=300)
    return r.returncode, r.stdout, r.stderr


def engine(command, *argv):
    return [PY, "-m", ENGINES[command][0], *argv]


def snapshot(root):
    return {p.relative_to(root).as_posix(): p.read_bytes()
            for p in sorted(pathlib.Path(root).rglob("*"))
            if p.is_file() and ".git" not in p.parts}


def hooks_of(root):
    s = json.loads((pathlib.Path(root) / ".claude" / "settings.local.json")
                   .read_text(encoding="utf-8"))
    return [(event, e.get("matcher"), h["command"])
            for event, entries in s.get("hooks", {}).items() for e in entries
            for h in e.get("hooks", [])]


def check_install(root, oracle):
    before = []
    if oracle:
        code, out, err = run([PY, str(pathlib.Path(oracle) / "scripts" / "pythia.py"),
                              "install"], root)
        assert code == 0, (out, err)
        before = hooks_of(root)
        assert before, "pythia for Oracle wrote no hooks"
    for command in ENGINES:
        code, out, err = run(engine(command, "install"), root)
        assert code == 0, (command, out, err)
    hooks = hooks_of(root)
    assert all(h in hooks for h in before), "an engine's install dropped pythia's hooks"
    for command, (module, _) in ENGINES.items():
        mine = [h for h in hooks if f"-m {module} " in h[2]]
        assert len(mine) == 4, (command, mine)
    claude = (pathlib.Path(root) / "CLAUDE.md").read_text(encoding="utf-8")
    for command in ENGINES:
        assert claude.count(f"<!-- {command}:begin") == 1, command
    if oracle:
        assert claude.count("<!-- pythia:begin") == 1
    ok(f"{len(ENGINES)} engines install into one project"
       + (" after pythia for Oracle, whose hooks all survive" if oracle else ""))

    first = snapshot(root)
    for command in reversed(list(ENGINES)):
        assert run(engine(command, "install"), root)[0] == 0
    assert snapshot(root) == first, "a second install changed a file"
    ok("a second install of each, in the other order, changes nothing")

    for command in ENGINES:
        code, out, err = run(engine(command, "doctor"), root)
        assert code == 0 and " fail " not in out, (command, out, err)
    ok("each doctor passes beside the others")


def decide(root, command, tool, tool_input):
    payload = json.dumps({"hook_event_name": "PreToolUse", "cwd": str(root),
                          "tool_name": tool, "tool_input": tool_input})
    code, out, err = run(engine(command, "hook", "pre-tool"), root, payload)
    assert code == 0, (command, err)
    if not out.strip():
        return None, ""
    o = json.loads(out)["hookSpecificOutput"]
    return o["permissionDecision"], o.get("permissionDecisionReason", "")


def check_guards(root):
    bash = lambda c: ("Bash", {"command": c})        # noqa: E731
    cases = [
        (bash('psql -d app -c "drop table t_order"'), {"pythia-pg": "deny"}),
        (bash('bash -c "psql -d app -f fix.sql"'), {"pythia-pg": "deny"}),
        (bash('sqlcmd -S db -Q "drop table app_owner.t_order"'), {"pythia-mssql": "deny"}),
        (bash('mongosh app --eval "db.t_order.drop()"'), {"pythia-mongo": "deny"}),
        (bash("mongoimport --db app --collection t_order --file x.json"),
         {"pythia-mongo": "deny"}),
        (bash('mysql app_owner -e "drop table t_order"'), {"pythia-mysql": "deny"}),
        (bash("docker exec db mysql app_owner < fix.sql"), {"pythia-mysql": "deny"}),
        (bash(f"{PY} -m pythia_postgres ls t_order"), {"pythia-pg": "allow"}),
        (bash("pythia-mssql cols t_order"), {"pythia-mssql": "allow"}),
        (bash("pythia-mongo impact t_order"), {"pythia-mongo": "allow"}),
        (bash("pythia-mysql src p_order_paid"), {"pythia-mysql": "allow"}),
        (bash("pythia-pg grep mongosh"), {"pythia-pg": "allow"}),
        (bash("pythia-mysql grep sqlcmd"), {"pythia-mysql": "allow"}),
        (("mcp__mongodb__delete-many", {"collection": "t_order", "filter": {}}),
         {"pythia-mongo": "deny"}),
        (("mcp__mongodb__aggregate", {"pipeline": [{"$merge": "t_copy"}]}),
         {"pythia-mongo": "deny"}),
        # a SQL-sounding tool may reach any SQL database: each denies it,
        # and none claims it
        (("mcp__postgres__query", {"sql": "delete from t_order"}),
         {"pythia-pg": "deny", "pythia-mssql": "deny", "pythia-mysql": "deny"}),
    ]
    for (tool, tin), want in cases:
        for command in ENGINES:
            got, why = decide(root, command, tool, tin)
            assert got == want.get(command), (command, tool, tin, got, why)
            if tool == "mcp__postgres__query" and got:
                assert "cannot tell which database" in why, (command, why)
    for command, (_, name) in ENGINES.items():
        secret = pathlib.Path(root) / ".pythia" / name / "connections.json"
        for other in ENGINES:
            got, _ = decide(root, other, "Read", {"file_path": str(secret)})
            assert got == ("deny" if other == command else None), (other, secret)
    ok("each guard denies its own client and its own secrets, and leaves the "
       "others' commands alone")


def check_doors(root):
    for owner in list(ENGINES) + ["pythia"]:
        card = f"{owner} approve 1a2b3c4d\nApproving: T_ORDER (TABLE)"
        for answer in ("Approve", "Reject", None):
            payload = json.dumps({
                "hook_event_name": "PostToolUse", "cwd": str(root),
                "tool_name": "AskUserQuestion", "session_id": "s1",
                "tool_input": {"questions": [{"question": card, "header": "Approve",
                                              "multiSelect": False, "options": [
                                                  {"label": "Approve"}, {"label": "Reject"}]}]},
                "tool_response": {"answers": {card: answer}} if answer else {}})
            for command in ENGINES:
                code, out, err = run(engine(command, "approve", "--hook"), root, payload)
                assert code == 0, (command, err)
                if command != owner:
                    assert out.strip() == "", (command, owner, answer, out)
                else:
                    assert out.strip(), (command, answer)       # its own card: answered
    ok("each answer hook answers its own engine's card and leaves the others' "
       "— pythia for Oracle's included — alone")


def check_briefs(root):
    payload = json.dumps({"hook_event_name": "SessionStart", "cwd": str(root)})
    for command in ENGINES:
        code, out, err = run(engine(command, "hook", "session-start"), root, payload)
        assert code == 0 and f"{command} " in out, (command, err)
        for other in ENGINES:
            if other != command:
                assert f"{other} " not in out, (command, other)
    ok("each session brief speaks for its own engine only")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--oracle", help="a checkout of pythia for Oracle, installed first")
    ns = ap.parse_args()
    oracle = pathlib.Path(ns.oracle).resolve() if ns.oracle else None
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as home:
        os.environ["HOME"] = os.environ["USERPROFILE"] = home
        for k in [k for k in os.environ if k.startswith("PYTHIA_")]:
            del os.environ[k]
        root = pathlib.Path(td).resolve()
        (root / "CLAUDE.md").write_text("# Team notes\n\nOur own rules.\n", encoding="utf-8")
        check_install(root, oracle)
        check_guards(root)
        check_doors(root)
        check_briefs(root)
        assert (root / "CLAUDE.md").read_text(encoding="utf-8").startswith(
            "# Team notes\n\nOur own rules.\n")
    print("OK")


if __name__ == "__main__":
    main()

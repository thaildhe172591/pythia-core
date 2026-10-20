#!/usr/bin/env python3
"""Self-checks for the lint framework: an engine's rules run over a change's
statements, severities and naming patterns come from the team's lint.json,
findings are shown in every preview and on the card, and autopilot never
lands a lint error unasked.

Run: python tests/test_lint.py
"""
import json
import sys

import fakes  # noqa: F401 — puts src/ on sys.path
from fakes import FakeHarnessEngine, FakeWriteEngine
from test_gate import OLD, expect, project

from pythia_core import harness, human, lint  # noqa: E402

VIEWISH = "create or replace function f_report() returns int as 'select * from t'"
DEFINER = "create or replace function f_total() returns int as 'select 2' security definer"
CLEAN = "create or replace function f_total() returns int as 'select 2'"
TABLE = "create table t_note (id int)"
TABLE_COMMENT = "comment on table t_note is 'notes'"


def engine():
    e = FakeHarnessEngine()
    e.conn.objects["f_total()"] = OLD
    return e


def write(p, name, *stmts):
    (p.root / name).write_text("".join(s + ";\n" for s in stmts), encoding="utf-8")
    return name


def config(p, doc):
    (p.state / "lint.json").write_text(json.dumps(doc), encoding="utf-8")


def test_lint_reports_each_finding_where_it_is_and_errors_fail_the_run():
    with project(engine()) as p:
        name = write(p, "a.sql", CLEAN, VIEWISH, DEFINER)
        code, out, why = p.run("lint", name)
        assert code == 1, (out, why)
        assert "a.sql:2" in out and "fake-select-star" in out and "warn" in out
        assert "a.sql:3" in out and "fake-definer-path" in out and "error" in out
        assert "a.sql:1" not in out
        code, out, _ = p.run("lint", write(p, "b.sql", CLEAN, VIEWISH))
        assert code == 0 and "fake-select-star" in out          # warnings pass
        assert p.run("lint", write(p, "c.sql", CLEAN))[0] == 0
        code, out, _ = p.run("lint", "a.sql", "--json")
        assert code == 1
        doc = json.loads(out)
        assert [(f["rule"], f["line"], f["severity"]) for f in doc["findings"]] == [
            ("fake-select-star", 2, "warn"), ("fake-definer-path", 3, "error")]
        assert doc["errors"] == 1


def test_a_change_is_linted_as_one_so_a_comment_in_another_file_counts():
    with project(engine()) as p:
        t = write(p, "t.sql", TABLE)
        c = write(p, "c.sql", TABLE_COMMENT)
        assert "fake-table-comment" in p.run("lint", t)[1]
        assert "fake-table-comment" not in p.run("lint", t, c)[1]


def test_the_team_sets_severities_and_naming_and_a_bad_config_is_refused():
    with project(engine()) as p:
        name = write(p, "a.sql", VIEWISH, DEFINER)
        config(p, {"severity": {"fake-select-star": "error", "fake-definer-path": "off"}})
        code, out, _ = p.run("lint", name)
        assert code == 1 and "fake-definer-path" not in out and "a.sql:1" in out
        config(p, {"naming": {"FUNCTION": "^fn_[a-z_]+$"}})
        code, out, _ = p.run("lint", write(p, "n.sql", CLEAN))
        assert "naming" in out and "fn_" in out and "f_total" in out
        assert code == 0                                       # naming warns by default
        config(p, {"naming": {"FUNCTION": "^fn_[a-z_]+$"}, "severity": {"naming": "error"}})
        assert p.run("lint", "n.sql")[0] == 1
        for bad, needle in (({"severity": {"no-such-rule": "warn"}}, "fake-select-star"),
                            ({"severity": {"fake-select-star": "loud"}}, "error, warn, info"),
                            ({"naming": {"FUNCTION": "("}}, "not a valid pattern"),
                            ({"nonsense": 1}, "severity, naming")):
            config(p, bad)
            expect(p.run("lint", "n.sql"), 1, needle)
        (p.state / "lint.json").write_text("{ broken", encoding="utf-8")
        expect(p.run("lint", "n.sql"), 1, "cannot parse")


def test_rules_lists_every_rule_with_its_effective_severity():
    with project(engine()) as p:
        config(p, {"severity": {"fake-select-star": "off"}})
        code, out, _ = p.run("lint", "--rules")
        assert code == 0
        for needle in ("fake-select-star", "off", "fake-definer-path", "error",
                       "naming", "SECURITY DEFINER needs SET search_path"):
            assert needle in out, needle


def test_a_rule_that_crashes_is_reported_and_never_blocks():
    e = engine()
    with project(e) as p:
        rules = e.lint_rules()
        rules.append({"id": "fake-crash", "severity": "error", "summary": "boom",
                      "check": lambda stmt, group, run: {}["no such key"]})
        e.lint_rules = lambda: rules
        code, out, _ = p.run("lint", write(p, "a.sql", CLEAN))
        assert code == 0 and "fake-crash" in out and "KeyError" in out


def test_every_preview_shows_lint_and_the_card_carries_its_errors():
    e = engine()
    with project(e) as p:
        p.file(DEFINER)
        code, out, _ = p.run("apply", "change.sql")
        assert code == 0 and "fake-definer-path" in out and "lint" in out
        doc = p.preview(DEFINER)
        assert [f["rule"] for f in doc["lint"]] == ["fake-definer-path"]
        code, card, _ = p.run("approve", "--card", doc["token"])
        assert "1 lint error" in card and "fake-definer-path" in card
        write(p, "rel.sql", CLEAN, VIEWISH)
        code, out, _ = p.run("deploy", "rel.sql")
        assert code == 0 and "rel.sql:2" in out and "fake-select-star" in out


def test_autopilot_never_lands_a_lint_error():
    e = engine()
    with project(e) as p:
        human.FORCE_CONSOLE = True
        assert p.run("autopilot", "on")[0] == 0
        human.FORCE_CONSOLE = False
        p.file(DEFINER)
        code, out, _ = p.run("apply", "change.sql")
        assert code == 0 and e.conn.commits == 0 and "lint error" in out
        write(p, "rel.sql", CLEAN, DEFINER)
        code, out, _ = p.run("deploy", "rel.sql")
        assert code == 0 and e.conn.commits == 0 and "statement 2" in out
        p.file(VIEWISH)                              # a warning does not stop it
        assert p.run("apply", "change.sql")[0] == 0 and e.conn.commits == 1


def test_an_engine_without_rules_still_writes_and_its_lint_is_refused():
    e = FakeWriteEngine()
    with project(e) as p:
        assert "lint" not in p.preview(CLEAN) or p.preview(CLEAN)["lint"] == []
        expect(p.run("lint", write(p, "a.sql", CLEAN)), 1, "lint tier")


def test_the_guard_lets_lint_through_and_the_brief_points_at_conventions():
    e = engine()
    with project(e) as p:
        out = harness.pre_tool_decision(e, {"tool_name": "Bash", "cwd": str(p.root),
                                            "tool_input": {"command": "pythia-fake lint a.sql b.sql"}})
        assert out["hookSpecificOutput"]["permissionDecision"] == "allow"
        assert not any("conventions" in ln for ln in harness.state_lines(e, p.root))
        (p.state / "conventions.md").write_text("# House style\n", encoding="utf-8")
        assert any("conventions.md" in ln and "read it" in ln
                   for ln in harness.state_lines(e, p.root))
        assert "pythia-fake lint" in harness.brief(e)
        assert lint.SEVERITIES == ("error", "warn", "info", "off")


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

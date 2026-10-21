#!/usr/bin/env python3
"""Self-checks for the read tier: output windowing, truncation markers, the
dependency tree and its summary, and the read commands through a fake engine.
No database, no driver.

Run: python tests/test_read.py
"""
import contextlib
import io
import json
import os
import pathlib
import sys
import tempfile
from argparse import Namespace

import fakes  # noqa: F401 — puts src/ on sys.path
from fakes import FakeEngine, FakeReadEngine

from pythia_core import cli, engine, output  # noqa: E402


def expect_exit(fn, *needles):
    try:
        fn()
    except SystemExit as e:
        msg = str(e).lower()
        for n in needles:
            assert n.lower() in msg, f"exit message missing {n!r}: {e}"
        return str(e)
    raise AssertionError("expected SystemExit, none raised")


def capture(fn):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        fn()
    return out.getvalue(), err.getvalue()


# --- pure output helpers -----------------------------------------------------

def test_clip_reports_anything_beyond_the_window():
    rows = list(range(10))
    assert output.clip(rows, 3) == ([0, 1, 2], True)
    assert output.clip(rows, 3, offset=8) == ([8, 9], False)
    assert output.clip(rows, 0) == (rows, False)          # 0 = no cap
    assert output.clip(rows, 10) == (rows, False)
    assert output.fetch_n(200) == 201 and output.fetch_n(0) == output.INT_MAX


def test_cell_flattens_what_a_driver_returns():
    class Lob:
        def read(self):
            return "big\ttext"
    assert output.cell(None) == "" and output.cell(3) == "3"
    assert output.cell(Lob()) == "big    text"


def ns(**kw):
    base = dict(json=False, command="ls", conn_name="dev", schema="app",
                limit=200, offset=0, max_lines=2000, raw=False, depth=3)
    base.update(kw)
    return Namespace(**base)


def test_emit_table_always_announces_truncation():
    out, err = capture(lambda: output.emit_table(
        ns(), ("A",), [(1,), (2,)], True))
    assert out.splitlines() == ["A", "1", "2",
                                "-- truncated at 2 rows (raise --limit, or "
                                "--limit 0 for no cap)"]
    assert "-- 2 rows" in err
    out, _ = capture(lambda: output.emit_table(ns(json=True), ("A",), [(1,)], True))
    doc = json.loads(out)
    assert doc["truncated"] is True and doc["rows"] == [{"A": 1}]


def test_format_source_numbers_each_unit_from_its_own_lines():
    rows = [("FUNCTION f(integer)", 1, "a\n"), ("FUNCTION f(integer)", 2, "b\n"),
            ("FUNCTION f(text)", 1, "c\n")]
    text = output.format_source(rows, raw=False)
    assert text.splitlines() == ["-- FUNCTION f(integer)", "     1  a", "     2  b",
                                 "", "-- FUNCTION f(text)", "     1  c"]
    assert output.format_source(rows, raw=True) == "a\nb\nc\n"


def test_render_tree_marks_what_the_catalog_did_not_say():
    rows = [(1, "app", "v_total", "VIEW", "catalog"),
            (1, "app", "f_total(integer)", "FUNCTION", "text"),
            (2, "app", "v_report", "VIEW", "catalog")]
    assert output.render_tree(rows, "app.t_order").splitlines() == [
        "app.t_order",
        "  app.v_total (VIEW)",
        "  app.f_total(integer) (FUNCTION) [text]",
        "    app.v_report (VIEW)"]


def test_impact_summary_counts_objects_and_flags_review():
    rows = [(1, "app", "v_total", "VIEW", "catalog"),
            (2, "app", "v_total", "VIEW", "catalog"),          # second path
            (1, "app", "f_total(integer)", "FUNCTION", "text")]
    s = output.impact_summary(rows, "app")
    assert s == {"objects": 2, "text_matches": 1, "other_schemas": [],
                 "needs_review": False}
    many = [(1, "app", f"v_{i}", "VIEW", "catalog") for i in range(10)]
    assert output.impact_summary(many, "app")["needs_review"] is True
    cross = [(1, "reporting", "v_sales", "VIEW", "catalog")]
    s = output.impact_summary(cross, "app")
    assert s["needs_review"] is True and s["other_schemas"] == ["reporting.v_sales"]


# --- the read commands through a fake engine -----------------------------------

@contextlib.contextmanager
def project(cfg=None):
    cfg = cfg or {"dev": {"host": "localhost", "dbname": "app", "user": "u",
                          "password": "s3cret", "schema": "app"}}
    keep_env = {k: os.environ.pop(k) for k in list(os.environ)
                if k.startswith("PYTHIA_")}
    keep_cwd = os.getcwd()
    with tempfile.TemporaryDirectory() as td:
        path = pathlib.Path(td) / ".pythia" / "fake" / "connections.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps(cfg), encoding="utf-8")
        if os.name == "posix":
            os.chmod(path, 0o600)
        os.chdir(td)
        try:
            yield
        finally:
            os.chdir(keep_cwd)
            os.environ.update(keep_env)


def run(e, *argv):
    code = [None]

    def go():
        code[0] = cli.main(e, list(argv))
    with project():
        out, err = capture(go)
    return code[0], out, err


def test_read_commands_need_the_whole_read_tier():
    e = FakeEngine()
    for argv in (["ls", "%"], ["src", "x"], ["cols", "x"], ["ddl", "x"],
                 ["grep", "x"], ["deps", "x"], ["impact", "x"], ["errors"]):
        with project():
            expect_exit(lambda: cli.main(e, argv), "read tier", "object_names")
    assert e.calls == []                      # refused before connecting


def test_ls_windows_and_announces():
    code, out, _ = run(FakeReadEngine(), "ls", "t_%", "--limit", "3")
    lines = out.splitlines()
    assert code == 0 and lines[0] == "NAME\tTYPE\tDETAIL" and len(lines) == 5
    assert lines[-1].startswith("-- truncated at 3 rows")
    _, out, _ = run(FakeReadEngine(), "ls", "t_%", "--limit", "0")
    assert "truncated" not in out and len(out.splitlines()) == 6


def test_src_windows_source_lines_and_says_how_to_continue():
    _, out, _ = run(FakeReadEngine(), "src", "f_total", "--max-lines", "2")
    assert "-- FUNCTION f_total(integer)" not in out     # one unit: no header
    assert "     1  line 1" in out and "     3  line 3" not in out
    assert "-- truncated: showing 2 of 5 source lines (use --offset 2" in out
    _, out, _ = run(FakeReadEngine(), "src", "f_total", "--offset", "2", "--json")
    doc = json.loads(out)
    assert doc["total_lines"] == 5 and [r["LINE"] for r in doc["rows"]] == [3, 4, 5]
    expect_exit(lambda: run(FakeReadEngine(), "src", "nope"),
                "no source found for 'nope' in schema app")


def test_cols_says_when_its_answer_is_partial():
    # an engine that infers columns (MongoDB samples documents) says so
    _, out, _ = run(FakeReadEngine(), "cols", "c_order")
    assert out.splitlines() == ["FIELD\tTYPE", "_id\tobjectId",
                                "-- blind spot: fields come from 100 sampled documents of 2000"], out
    _, out, _ = run(FakeReadEngine(), "cols", "c_order", "--json")
    doc = json.loads(out)
    assert doc["blind_spot"] == "fields come from 100 sampled documents of 2000"
    assert doc["verified"] is False
    _, out, _ = run(FakeReadEngine(), "cols", "t_order", "--json")
    doc = json.loads(out)
    assert doc["blind_spot"] is None and doc["verified"] is True


def test_cols_and_ddl_refuse_what_is_not_there():
    _, out, _ = run(FakeReadEngine(), "cols", "t_order")
    assert out.splitlines()[:2] == ["COLUMN\tTYPE", "id\tinteger"]
    expect_exit(lambda: run(FakeReadEngine(), "cols", "t_none"),
                "no table or view named 't_none' in schema app")
    _, out, _ = run(FakeReadEngine(), "ddl", "t_order", "--max-lines", "3")
    assert out.splitlines()[:3] == [f"-- ddl line {i}" for i in (1, 2, 3)]
    assert "-- truncated: showing 3 of 7 lines (use --offset 3" in out
    expect_exit(lambda: run(FakeReadEngine(), "ddl", "t_none"),
                "no ddl for 't_none' in schema app")


def test_grep_lists_matching_lines():
    _, out, _ = run(FakeReadEngine(), "grep", "t_order", "--json")
    assert json.loads(out)["rows"][0]["LINE"] == 3


def test_impact_tree_summary_and_blind_spots():
    deps = [(1, "app", "v_total", "VIEW", "catalog"),
            (1, "reporting", "v_sales", "VIEW", "catalog"),
            (1, "app", "f_total(integer)", "FUNCTION", "text")]
    e = FakeReadEngine(dependents=deps, notes=["bodies are matched as text"])
    _, out, _ = run(e, "impact", "t_order")
    assert out.startswith("app.t_order\n  app.v_total (VIEW)\n")
    assert "-- impact: 3 dependent objects (1 matched by name as text, not by " \
           "the catalog)" in out
    assert "-- 1 in other schemas: reporting.v_sales" in out
    assert "! show this list to the developer before writing code" in out
    assert "-- blind spot: bodies are matched as text" in out
    _, out, _ = run(e, "impact", "t_order", "--json")
    doc = json.loads(out)
    assert doc["summary"]["needs_review"] is True
    assert doc["blind_spots"] == ["bodies are matched as text"]
    assert doc["rows"][2]["HOW"] == "text"


def test_impact_with_nothing_depending_still_names_its_blind_spots():
    e = FakeReadEngine(dependents=[], notes=["dynamic SQL is invisible"])
    _, out, _ = run(e, "impact", "t_order")
    assert "-- nothing depends on t_order (in what the catalog and a text " \
           "search can see, depth 3)" in out
    assert "-- blind spot: dynamic SQL is invisible" in out
    expect_exit(lambda: run(FakeReadEngine(dependents=None), "impact", "t_x"),
                "no object named 't_x' in schema app")


def test_impact_summary_counts_in_english():
    one = [(1, "app", "f_total(integer)", "FUNCTION", "text")]
    _, out, _ = run(FakeReadEngine(dependents=one), "impact", "t_order")
    assert "-- impact: 1 dependent object (matched by name as text, not by " \
           "the catalog)" in out


def test_impact_truncation_keeps_the_summary_whole():
    deps = [(1, "app", f"v_{i:02d}", "VIEW", "catalog") for i in range(12)]
    _, out, _ = run(FakeReadEngine(dependents=deps), "impact", "t_order",
                    "--limit", "5")
    assert "-- truncated at 5 rows" in out
    assert "-- impact: 12 dependent objects" in out


def test_deps_walks_forward():
    _, out, _ = run(FakeReadEngine(), "deps", "f_total")
    assert out.splitlines()[:2] == ["app.f_total", "  app.t_order (TABLE) [text]"]
    expect_exit(lambda: run(FakeReadEngine(), "deps", "nope"), "no object named")


def test_errors_say_not_verified_plainly():
    e = FakeReadEngine(error_note="the checker is not installed")
    code, out, _ = run(e, "errors", "f_total")
    assert code == 0
    assert "-- NOT VERIFIED: the checker is not installed" in out
    assert "no errors" not in out
    _, out, _ = run(e, "errors", "f_total", "--json")
    doc = json.loads(out)
    assert doc["verified"] is False and doc["blind_spot"] == "the checker is not installed"
    clean = FakeReadEngine()
    _, out, _ = run(clean, "errors", "f_total")
    assert "-- no errors for f_total" in out
    bad = FakeReadEngine(errors=[("f_total", "FUNCTION", 4, 9, "error",
                                  'relation "t_gone" does not exist')])
    _, out, _ = run(bad, "errors")
    assert out.splitlines()[:2] == ["f_total (FUNCTION)",
                                    '  4:9 error relation "t_gone" does not exist']


def test_errors_on_a_missing_object_and_a_function_level_finding():
    class Missing(FakeReadEngine):
        def compile_errors(self, conn, schema, name):
            return None
    expect_exit(lambda: run(Missing(), "errors", "t_x"),
                "no object named 't_x' in schema app")
    whole = FakeReadEngine(errors=[("f_total", "FUNCTION", None, None,
                                    "warning", "never read")])
    _, out, _ = run(whole, "errors")
    assert "  - warning never read" in out


def test_read_tier_is_listed_in_order():
    assert engine.READ == ("object_names", "source", "columns", "ddl", "grep",
                           "dependencies", "dependents", "compile_errors")
    assert engine.missing(FakeReadEngine(), "read") == []


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

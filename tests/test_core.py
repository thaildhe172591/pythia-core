#!/usr/bin/env python3
"""Self-checks for pythia-core — the engine contract, the query contract and
connection resolution. No database, no driver.

Run: python tests/test_core.py
"""
import json
import os
import pathlib
import re
import sys
import tempfile

import fakes  # noqa: F401 — puts src/ on sys.path
from fakes import ROOT, FakeEngine

import pythia_core  # noqa: E402
from pythia_core import config, engine, queries  # noqa: E402


def expect_exit(fn, *needles):
    """fn must raise SystemExit whose message holds every needle (any case)."""
    try:
        fn()
    except SystemExit as e:
        msg = str(e).lower()
        for n in needles:
            assert n.lower() in msg, f"exit message missing {n!r}: {e}"
        return str(e)
    raise AssertionError("expected SystemExit, none raised")


# --- versions ----------------------------------------------------------------

def test_version_matches_pyproject():
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    declared = re.search(r'^version\s*=\s*"([^"]+)"', text, re.M).group(1)
    assert pythia_core.__version__ == declared


def test_no_runtime_dependencies():
    # the core is stdlib only: a driver belongs to its engine package
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert re.search(r"^dependencies\s*=\s*\[\s*\]", text, re.M), \
        "pythia-core must declare dependencies = []"


# --- engine contract ---------------------------------------------------------

def test_fake_engine_answers_the_connect_tier():
    assert engine.engine_problems(FakeEngine()) == []


def test_missing_names_each_absent_method_in_order():
    e = FakeEngine()
    assert engine.missing(e, "connect") == []
    assert engine.missing(e, "read") == list(engine.READ)


def test_tiers_do_not_overlap():
    seen = {}
    for tier, methods in engine.TIERS.items():
        for m in methods:
            assert m not in seen, f"{m} is in both {seen[m]} and {tier}"
            seen[m] = tier


def test_refuse_unless_names_the_gap():
    e = FakeEngine()
    engine.refuse_unless(e, "connect", "check")      # complete: no exit
    expect_exit(lambda: engine.refuse_unless(e, "read", "ls"),
                "pythia-fake ls", "FakeDB", "read tier", "object_names",
                "before connecting")


def test_engine_problems_reports_identity_and_gaps():
    class Half:
        name, label, command = "Bad-Name", "X", "pythia-x"
        env_prefix, version, bind_style = "x_prefix", "1", "qmark"
    problems = engine.engine_problems(Half())
    joined = "\n".join(problems)
    assert "name 'Bad-Name'" in joined
    assert "env_prefix 'x_prefix'" in joined
    assert "bind_style 'qmark'" in joined
    assert "connect tier missing: driver_errors" in joined
    empty = engine.engine_problems(object())
    assert "name is not declared" in empty

    class Documents:          # MongoDB: binds are values inside a command document
        name, label, command = "mongo", "MongoDB", "pythia-mongo"
        env_prefix, version, bind_style = "PYTHIA_MONGO", "1", "document"
    assert not any("bind_style" in m for m in engine.engine_problems(Documents()))


# --- query contract ----------------------------------------------------------

def test_query_binds_ignores_comments_and_literals():
    sql = """-- Purpose: demo, mentions :notabind in a comment
             /* and :another */
             select to_char(d, 'hh24:mi:ss') txt, x::text
               from t where owner = :s and name = upper(:n)"""
    assert queries.query_binds(sql) == {"s", "n"}
    # psycopg reads placeholders everywhere, comments and literals included,
    # so the lint must read the text the same way
    pg = ("select %(s)s, '%(lit)s', relname::text from pg_class\n"
          " where x = %(n)s -- %(c)s")
    assert queries.query_binds(pg, "pyformat") == {"s", "lit", "n", "c"}


def write(d, name, text):
    (d / name).write_text(text, encoding="utf-8")


HEADER = "-- Purpose: p\n-- Binds:   b\n-- Returns: r\n"


def test_contract_problems_catches_every_drift():
    with tempfile.TemporaryDirectory() as td:
        d = pathlib.Path(td)
        write(d, "ok.sql", HEADER + "select 1 from t where a = %(s)s")
        assert queries.contract_problems(d, {"ok.sql": {"s"}}, "pyformat") == []
        write(d, "stray.sql", HEADER + "select 1")
        write(d, "bare.sql", "select 1")
        write(d, "wrongstyle.sql", HEADER + "select 1 from t where a = :s")
        write(d, "pct.sql", HEADER + "select 1 from t where a like 'x%'")
        # the quiet one: psycopg turns 'a%b' into 'a$1' and raises nothing
        write(d, "pct_b.sql", HEADER + "select 'a%b' from t")
        write(d, "pct_ok.sql", HEADER + "select 1 from t where a like 'x%%'")
        declared = {"ok.sql": {"s", "extra"}, "bare.sql": set(),
                    "wrongstyle.sql": set(), "pct.sql": set(),
                    "pct_ok.sql": set(), "pct_b.sql": set(), "gone.sql": set()}
        joined = "\n".join(queries.contract_problems(d, declared, "pyformat"))
    assert "stray.sql is on disk but not declared" in joined
    assert "gone.sql is declared but not on disk" in joined
    assert "bare.sql is missing a '-- Purpose:' header line" in joined
    assert "ok.sql: file uses ['s'], declared ['extra', 's']" in joined
    assert "wrongstyle.sql uses named binds" in joined
    assert "pct.sql has a lone '%'" in joined
    assert "pct_b.sql has a lone '%'" in joined
    assert "pct_ok.sql" not in joined


def test_run_query_always_passes_a_dict():
    # under pyformat the driver reads %% as % only when given parameters
    from fakes import FakeConn
    conn = FakeConn({"from t": (("A",), [(1,)])})
    cols, rows = queries.run_query(conn, "select a from t")
    assert (cols, rows) == (["A"], [(1,)])
    assert conn.executed[-1][1] == {}


# --- configuration -----------------------------------------------------------

def project(td, cfg, engine_name="fake", mode=0o600):
    path = pathlib.Path(td) / ".pythia" / engine_name / "connections.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(cfg), encoding="utf-8")
    if os.name == "posix":
        os.chmod(path, mode)
    return path


def test_find_config_walks_up_to_the_engine_folder():
    e = FakeEngine()
    with tempfile.TemporaryDirectory() as td:
        project(td, {"dev": {"user": "u"}})
        deep = pathlib.Path(td) / "a" / "b"
        deep.mkdir(parents=True)
        cfg, root = config.find_config(deep, {}, e)
        assert cfg == {"dev": {"user": "u"}}
        assert pathlib.Path(root).resolve() == pathlib.Path(td).resolve()


def test_oracle_pythia_config_is_never_read():
    """pythia for Oracle keeps .pythia/connections.json. An engine of this
    family must not mistake it for its own — that would point a PostgreSQL
    driver at an Oracle entry, or the other way round."""
    e = FakeEngine()
    with tempfile.TemporaryDirectory() as td:
        legacy = pathlib.Path(td) / ".pythia" / "connections.json"
        legacy.parent.mkdir()
        legacy.write_text(json.dumps({"dev": {"user": "oracle_user"}}),
                          encoding="utf-8")
        assert config.find_config(pathlib.Path(td), {}, e) == (None, None)
        expect_exit(lambda: config.resolve_connection(
            None, None, {}, pathlib.Path(td), None, e),
            "no fakedb connection", ".pythia/fake/connections.json",
            "not read here")


def test_config_override_and_root():
    e = FakeEngine()
    with tempfile.TemporaryDirectory() as td:
        path = project(td, {"dev": {"user": "u"}})
        _, root = config.find_config(pathlib.Path("/"), {"PYTHIA_FAKE_CONFIG": str(path)}, e)
        assert pathlib.Path(root).resolve() == pathlib.Path(td).resolve()
        loose = pathlib.Path(td) / "elsewhere.json"
        loose.write_text("{}", encoding="utf-8")
        if os.name == "posix":
            os.chmod(loose, 0o600)
        _, root = config.find_config(pathlib.Path("/"), {"PYTHIA_FAKE_CONFIG": str(loose)}, e)
        assert pathlib.Path(root).resolve() == pathlib.Path(td).resolve()
        expect_exit(lambda: config.find_config(
            pathlib.Path("/"), {"PYTHIA_FAKE_CONFIG": str(loose) + ".x"}, e),
            "PYTHIA_FAKE_CONFIG", "missing")


def test_config_must_be_an_object():
    e = FakeEngine()
    with tempfile.TemporaryDirectory() as td:
        path = project(td, [])
        expect_exit(lambda: config.find_config(pathlib.Path(td), {}, e),
                    "json object")
        path.write_text("{not json", encoding="utf-8")
        expect_exit(lambda: config.find_config(pathlib.Path(td), {}, e),
                    "cannot parse")


def test_resolve_explicit_env_and_path():
    e = FakeEngine()
    cfg = {"ALPHA": {"user": "a"}, "BETA": {"user": "b"}}
    root = pathlib.Path(tempfile.gettempdir()) / "proj"
    name, c = config.resolve_connection(cfg, "beta", {}, root / "ALPHA", root, e)
    assert name == "BETA" and c["user"] == "b"          # explicit beats path
    expect_exit(lambda: config.resolve_connection(cfg, "GAMMA", {}, root, root, e),
                "ALPHA", "BETA")
    name, _ = config.resolve_connection(
        cfg, None, {"PYTHIA_FAKE_CONNECTION": "alpha"}, root, root, e)
    assert name == "ALPHA"
    # another engine's variable is not this engine's choice
    expect_exit(lambda: config.resolve_connection(
        cfg, None, {"PYTHIA_CONNECTION": "alpha"}, root, root, e),
        "cannot infer", "PYTHIA_FAKE_CONNECTION")
    name, _ = config.resolve_connection(cfg, None, {}, root / "BETA" / "x", root, e)
    assert name == "BETA"


def test_resolve_default_and_its_mistakes():
    e = FakeEngine()
    root = pathlib.Path(tempfile.gettempdir()) / "proj"
    cfg = {"default": "alpha", "ALPHA": {"user": "a"}, "BETA": {"user": "b"}}
    name, _ = config.resolve_connection(cfg, None, {}, root, root, e)
    assert name == "ALPHA"
    expect_exit(lambda: config.resolve_connection(
        {"default": "nope", "A": {}, "B": {}}, None, {}, root, root, e),
        "not a connection")
    expect_exit(lambda: config.resolve_connection(
        {"default": 3, "A": {}}, None, {}, root, root, e), "must name")
    expect_exit(lambda: config.resolve_connection(
        {"a": {}, "A": {}}, None, {}, root, root, e), "collide")
    expect_exit(lambda: config.resolve_connection(
        {"a": "text"}, None, {}, root, root, e), "must be an object")


def test_connection_summary_never_carries_a_secret():
    e = FakeEngine()
    cfg = {"default": "dev",
           "dev": {"host": "localhost", "dbname": "app", "user": "app_agent",
                   "password": "s3cret", "schema": "APP", "token": "s3cret"},
           "bad": "not an object"}
    rows = config.connection_summary(cfg, e)
    assert rows == [{"name": "dev", "user": "app_agent", "schema": "app",
                     "target": "localhost/app", "default": True}]
    assert "s3cret" not in json.dumps(rows)


def main():
    failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            # SystemExit is a BaseException: an unexpected sys.exit inside a
            # test would otherwise end the run and skip everything after it
            except (Exception, SystemExit) as e:  # noqa: BLE001 — keep going
                failed += 1
                print(f"FAIL {name}: {e!r}")
    if failed:
        sys.exit(f"{failed} test(s) failed")
    print("OK")


if __name__ == "__main__":
    main()

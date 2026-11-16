#!/usr/bin/env python3
"""Self-checks for pythia-core's command line, driven through a fake engine.
No database, no driver.

Run: python tests/test_cli.py
"""
import contextlib
import io
import json
import os
import pathlib
import sys
import tempfile

import fakes  # noqa: F401 — puts src/ on sys.path
from fakes import FakeEngine

from pythia_core import __version__, cli  # noqa: E402


def expect_exit(fn, *needles):
    try:
        fn()
    except SystemExit as e:
        msg = str(e).lower()
        for n in needles:
            assert n.lower() in msg, f"exit message missing {n!r}: {e}"
        return str(e)
    raise AssertionError("expected SystemExit, none raised")


@contextlib.contextmanager
def project(cfg, engine_name="fake"):
    """A project holding cfg as this engine's connections, as cwd and as
    the only configuration the run can see."""
    keep_env = {k: os.environ.pop(k) for k in list(os.environ)
                if k.startswith("PYTHIA_")}
    keep_cwd = os.getcwd()
    with tempfile.TemporaryDirectory() as td:
        path = pathlib.Path(td) / ".pythia" / engine_name / "connections.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps(cfg), encoding="utf-8")
        if os.name == "posix":
            os.chmod(path, 0o600)
        os.chdir(td)
        try:
            yield pathlib.Path(td)
        finally:
            os.chdir(keep_cwd)
            for k in [k for k in os.environ if k.startswith("PYTHIA_")]:
                del os.environ[k]
            os.environ.update(keep_env)


def run(engine, *argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = cli.main(engine, list(argv))
    return code, out.getvalue(), err.getvalue()


DEV = {"dev": {"host": "localhost", "dbname": "app", "user": "app_agent",
               "password": "s3cret", "schema": "app"}}


def test_version_needs_no_project():
    code, out, _ = run(FakeEngine(), "version")
    assert code == 0 and "pythia-fake 0.0.1" in out and __version__ in out
    code, out, _ = run(FakeEngine(), "version", "--json")
    assert json.loads(out)["core_version"] == __version__


def test_connections_lists_without_connecting_or_leaking():
    e = FakeEngine()
    with project(DEV):
        code, out, _ = run(e, "connections")
        assert code == 0 and "dev" in out and "localhost/app" in out
        assert "s3cret" not in out
        code, out, _ = run(e, "connections", "--json")
        assert json.loads(out)[0]["target"] == "localhost/app"
        assert "s3cret" not in out
    assert e.calls == []                       # nothing connected


def test_check_reads_through_a_readonly_session():
    e = FakeEngine(current="app")
    with project(DEV):
        code, out, err = run(e, "check", "--json")
    doc = json.loads(out)
    assert code == 0 and doc["ok"] and doc["command"] == "check"
    assert doc["connection"] == "dev" and doc["schema"] == "app"
    assert {"ITEM": "server", "VALUE": "FakeDB 1.0"} in doc["rows"]
    assert doc["schema_drift"] is False and doc["truncated"] is False
    assert "-- connection=dev schema=app" in err
    assert e.conn.readonly is True and e.conn.closed is True
    assert e.calls == ["driver_errors", "connect", "session"]


def test_check_says_where_unqualified_names_land():
    with project(DEV):
        code, out, _ = run(FakeEngine(current="public"), "check")
    assert code == 0 and "current schema is 'public'" in out
    assert '"schema": "public"' in out
    with project(DEV):
        _, out, _ = run(FakeEngine(current=None), "check")
    assert "did not name a current schema" in out


def test_check_warns_below_the_engines_floor_and_refuses_nothing():
    # an engine may say its server is older than any version it is tested
    # on (floor_warning, in no tier): check says so, and nothing is refused
    class OldServer(FakeEngine):
        def floor_warning(self, conn):
            return "FakeDB 0.9 is below pythia-fake's floor, 1.0: it is not tested there."
    with project(DEV):
        code, out, _ = run(OldServer(), "check")
    assert code == 0 and "! FakeDB 0.9 is below pythia-fake's floor, 1.0" in out
    with project(DEV):
        code, out, _ = run(OldServer(), "check", "--json")
    assert code == 0 and json.loads(out)["below_floor"].startswith("FakeDB 0.9")

    class Current(FakeEngine):
        def floor_warning(self, conn):
            return None
    for e in (Current(), FakeEngine()):             # at the floor, or no floor at all
        with project(DEV):
            code, out, _ = run(e, "check", "--json")
        assert code == 0 and json.loads(out)["below_floor"] is None
        with project(DEV):
            _, out, _ = run(e, "check")
        assert "floor" not in out


def test_check_refuses_an_engine_without_the_tier_before_connecting():
    class NoVersion(FakeEngine):
        server_version = None
    e = NoVersion()
    with project(DEV):
        expect_exit(lambda: run(e, "check"), "connect tier", "server_version")
    assert e.calls == []


def test_connect_failure_is_the_engines_message():
    e = FakeEngine(fail="no route to host")
    with project(DEV):
        expect_exit(lambda: run(e, "check"), "could not reach 'dev'",
                    "no route to host")


def test_driver_error_mid_command_is_named_and_the_handle_closed():
    from fakes import DriverError

    class Breaks(FakeEngine):
        def server_version(self, conn):
            raise DriverError("relation does not exist")
    e = Breaks()
    with project(DEV):
        expect_exit(lambda: run(e, "check"), "FakeDB error",
                    "relation does not exist")
    assert e.conn.closed is True


def test_no_connection_names_this_engines_folder():
    keep = os.getcwd()
    with tempfile.TemporaryDirectory() as td:
        os.chdir(td)
        try:
            expect_exit(lambda: run(FakeEngine(), "check"),
                        ".pythia/fake/connections.json")
        finally:
            os.chdir(keep)


# --- query: rows, read-only (pythia-mysql#1) ------------------------------------------

class QueryEngine(FakeEngine):
    """The query tier: refuses what is not a read, answers a scripted table."""
    ROWS = [(i, f"n{i}") for i in range(1, 8)]

    def query_refusal(self, sql):
        return None if sql.lower().startswith("select") else "only SELECT is a read"

    def run_query(self, conn, schema, sql, fetch):
        self.calls.append(("run_query", sql, fetch))
        return ("ID", "NAME"), self.ROWS[:fetch]


def test_query_reads_rows_in_a_readonly_session_and_announces_truncation():
    e = QueryEngine()
    with project(DEV):
        code, out, err = run(e, "query", "select id, name from t_order", "--limit", "3")
    assert code == 0 and out.splitlines()[0] == "ID\tNAME"
    assert "1\tn1" in out and "4\tn4" not in out
    assert "-- truncated at 3 rows" in out
    assert e.conn.readonly is True and e.conn.closed is True
    assert ("run_query", "select id, name from t_order", 4) in e.calls
    with project(DEV):
        code, out, _ = run(QueryEngine(), "query", "select 1", "--json", "--limit", "0")
    doc = json.loads(out)
    assert doc["command"] == "query" and len(doc["rows"]) == 7 and not doc["truncated"]


def test_query_refuses_what_is_not_a_read_before_connecting():
    e = QueryEngine()
    with project(DEV):
        msg = expect_exit(lambda: run(e, "query", "delete from t_order"),
                          "only SELECT is a read", "pythia-fake apply")
    assert "connect" not in e.calls, msg
    with project(DEV):                    # an engine without the tier is refused by name
        expect_exit(lambda: run(FakeEngine(), "query", "select 1"), "query tier")


def test_query_reads_a_file_and_redacts_columns():
    with project(DEV) as root:
        (root / "q.sql").write_text("select id, name from t_order\n", encoding="utf-8")
        code, out, _ = run(QueryEngine(), "query", "-f", "q.sql", "--redact", "name")
    assert code == 0 and "n1" not in out and "1\t***" in out


def test_the_guard_waves_a_query_through_as_a_read():
    from pythia_core import harness
    kind, _ = harness.classify_argv(QueryEngine(), ["query", "select 1"])
    assert kind == "read"


# --- check: the engine's own status lines (pythia-mysql#7) ----------------------------

def test_check_adds_the_engines_status_lines():
    class Status(FakeEngine):
        def status(self, conn):
            return [("user", "app_agent@%", None),
                    ("tls", "none", "this session is not encrypted")]
    with project(DEV):
        code, out, _ = run(Status(), "check")
    assert code == 0 and "user" in out and "app_agent@%" in out
    assert "! tls: this session is not encrypted" in out
    with project(DEV):
        _, out, _ = run(Status(), "check", "--json")
    doc = json.loads(out)
    assert {"ITEM": "tls", "VALUE": "none"} in doc["rows"]
    assert doc["warnings"] == ["tls: this session is not encrypted"]


# --- connections add / remove / test (pythia-mysql#6) ---------------------------------

class ConfigEngine(FakeEngine):
    def connections_template(self):
        return {"default": "dev", "dev": {"host": "", "port": 5432, "dbname": "app",
                                          "user": "", "password": "", "tls": "request"}}


def _console(on):
    from pythia_core import human
    human.FORCE_CONSOLE = on


def test_connections_add_writes_the_entry_never_echoing_the_secret():
    import getpass
    keep = getpass.getpass
    getpass.getpass = lambda prompt="": "t0p-secret"
    _console(True)
    try:
        with project(DEV) as root:
            code, out, err = run(ConfigEngine(), "connections", "add", "uat",
                                 "--set", "host=db.example.test", "--set", "port=33062",
                                 "--set", "user=app_agent", "--default")
            assert code == 0, err
            assert "t0p-secret" not in out + err
            path = root / ".pythia" / "fake" / "connections.json"
            cfg = json.loads(path.read_text(encoding="utf-8"))
            assert cfg["uat"] == {"host": "db.example.test", "port": 33062,
                                  "user": "app_agent", "password": "t0p-secret"}
            assert cfg["default"] == "uat" and cfg["dev"] == DEV["dev"]
            if os.name == "posix":
                assert path.stat().st_mode & 0o777 == 0o600
            msg = expect_exit(lambda: run(ConfigEngine(), "connections", "add", "x",
                                          "--set", "hots=typo"), "hots", "host")
            assert msg
            expect_exit(lambda: run(ConfigEngine(), "connections", "add", "uat",
                                    "--set", "host=h"), "already", "remove")
            code, out, _ = run(ConfigEngine(), "connections", "remove", "uat")
            cfg = json.loads(path.read_text(encoding="utf-8"))
            assert code == 0 and "uat" not in cfg and "default" not in cfg
    finally:
        getpass.getpass = keep
        _console(None)


def test_connections_add_and_remove_are_the_developers_at_a_console():
    _console(False)
    try:
        with project(DEV) as root:
            before = (root / ".pythia" / "fake" / "connections.json").read_bytes()
            for argv in (("add", "uat", "--set", "host=h"), ("remove", "dev")):
                expect_exit(lambda: run(ConfigEngine(), "connections", *argv),
                            "console")
            assert (root / ".pythia" / "fake" / "connections.json").read_bytes() == before
        from pythia_core import harness
        assert harness.classify_argv(ConfigEngine(), ["connections", "add", "x"])[0] == "other"
        assert harness.classify_argv(ConfigEngine(), ["connections", "test"])[0] == "read"
    finally:
        _console(None)


def test_connections_test_connects_and_names_the_failure():
    with project(DEV):
        code, out, _ = run(FakeEngine(), "connections", "test")
    assert code == 0 and "dev" in out and "FakeDB 1.0" in out
    with project(DEV):
        code, out, _ = run(FakeEngine(fail="timed out"), "connections", "test", "dev")
    assert code == 1 and "could not reach 'dev': timed out" in out


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

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

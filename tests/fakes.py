"""Doubles shared by the suites: an engine and a DB-API connection that need
no driver and no database."""
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))


class DriverError(Exception):
    pass


class FakeCursor:
    def __init__(self, conn):
        self.conn, self.description, self._rows = conn, None, []

    def execute(self, sql, binds=None):
        self.conn.executed.append((sql, binds))
        for key, (cols, rows) in self.conn.script.items():
            if key in sql.lower():
                self.description = [(c,) for c in cols]
                self._rows = list(rows)
                return
        self.description, self._rows = None, []

    def fetchall(self):
        return list(self._rows)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeConn:
    def __init__(self, script=None):
        self.script, self.executed = dict(script or {}), []
        self.readonly, self.closed = None, False

    def cursor(self):
        return FakeCursor(self)


class FakeEngine:
    """Answers the connect tier from a scripted FakeConn."""
    name = "fake"
    label = "FakeDB"
    command = "pythia-fake"
    env_prefix = "PYTHIA_FAKE"
    version = "0.0.1"
    bind_style = "named"

    def __init__(self, current="app", version="FakeDB 1.0", fail=None):
        self.conn = FakeConn()
        self._current, self._version, self.fail = current, version, fail
        self.calls = []

    def driver_errors(self):
        self.calls.append("driver_errors")
        return (DriverError,)

    def connect(self, entry):
        self.calls.append("connect")
        if self.fail:
            raise DriverError(self.fail)
        return self.conn

    def session(self, handle, readonly):
        self.calls.append("session")
        handle.readonly = readonly
        return handle

    def close(self, handle):
        handle.closed = True

    def connect_failure(self, exc, conn_name, entry):
        return f"could not reach {conn_name!r}: {exc}"

    def schema_of(self, entry):
        return str(entry.get("schema") or "public").lower()

    def target_of(self, entry):
        return f"{entry.get('host', '')}/{entry.get('dbname', '')}"

    def server_version(self, conn):
        return self._version

    def current_schema(self, conn):
        return self._current


class FakeReadEngine(FakeEngine):
    """FakeEngine plus the read tier, answering from canned data."""

    SOURCE = [("FUNCTION f_total(integer)", i, f"line {i}\n") for i in range(1, 6)]

    def __init__(self, dependents=None, notes=None, errors=None, error_note=None,
                 **kw):
        super().__init__(**kw)
        self._dependents = dependents
        self._notes = list(notes or [])
        self._errors = list(errors or [])
        self._error_note = error_note

    def object_names(self, conn, schema, pattern, fetch):
        rows = [(f"t_{i:02d}", "TABLE", "") for i in range(5)]
        return ("NAME", "TYPE", "DETAIL"), rows[:fetch]

    def source(self, conn, schema, name):
        return list(self.SOURCE) if name == "f_total" else []

    def columns(self, conn, schema, name):
        if name != "t_order":
            return None
        return ("COLUMN", "TYPE"), [("id", "integer"), ("total", "numeric")]

    def ddl(self, conn, schema, name):
        return "\n".join(f"-- ddl line {i}" for i in range(1, 8)) if name == "t_order" else ""

    def grep(self, conn, schema, text, fetch):
        rows = [("f_total", "FUNCTION", 3, "select total from t_order")]
        return ("NAME", "TYPE", "LINE", "TEXT"), rows[:fetch]

    def dependencies(self, conn, schema, name, depth):
        if name != "f_total":
            return None
        return [(1, schema, "t_order", "TABLE", "text")], list(self._notes)

    def dependents(self, conn, schema, name, depth):
        if self._dependents is None:
            return None
        return list(self._dependents), list(self._notes)

    def compile_errors(self, conn, schema, name):
        return list(self._errors), self._error_note

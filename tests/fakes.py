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
        if name == "c_order":          # a collection: fields read from a sample
            return ("FIELD", "TYPE"), [("_id", "objectId")], \
                "fields come from 100 sampled documents of 2000"
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


class WriteConn(FakeConn):
    """A database holding code objects and table rows, with one transaction:
    what a rehearsal stages lands on commit and vanishes on rollback."""

    def __init__(self):
        super().__init__()
        self.objects = {}          # label -> definition, committed
        self.comments = {}         # table -> comment, committed
        self.rows = {}             # table -> [ids], committed
        self.pending = []          # staged changes
        self.commits = self.rollbacks = 0
        self.rehearsed = []        # every statement a rehearsal ran
        self.broken_after = []     # findings the next rehearsal reports
        self.own_after = []
        self.fail = None           # error text the next rehearsal raises
        self.rowcount = None       # a rowcount that differs from the probe
        self.on_execute = None     # hook run as a rehearsal executes
        self.escape = None         # a trigger that commits: the next rehearsal escapes
        self.predicted = []        # every statement a prediction read (nothing ran)
        self.executed = []         # every statement the predict path ran for real
        self.execute_result = None  # {"error"} or {"escaped"} the next execute returns
        self.differs = []          # what verify_written finds unlike the prediction
        self.commit_fails = False  # COMMIT raises: whether it landed is unknown

    def commit(self):
        if self.commit_fails:          # the connection drops during COMMIT
            raise DriverError("server closed the connection unexpectedly")
        self.commits += 1
        for kind, key, value in self.pending:
            getattr(self, kind)[key] = value
        self.pending = []

    def rollback(self):
        self.rollbacks += 1
        self.pending = []


class FakeWriteEngine(FakeReadEngine):
    """FakeReadEngine plus the write tier, over a WriteConn."""
    rollback_notes = {"source": "Yes — the definition is snapshotted.",
                      "comments": "Yes — the comment is snapshotted.",
                      "data_dml": "No.", "structural": "No.",
                      "grants": "By hand.", "session": "Not needed."}

    def __init__(self, **kw):
        super().__init__(**kw)
        self.conn = WriteConn()

    def classify(self, text):
        import re
        low = " ".join(text.lower().split())
        if low.startswith("create or replace function"):
            return "source", None
        if low.startswith("comment on table"):
            return "comments", None
        if re.match(r"(delete|update|insert)\b", low):
            return "data_dml", None
        if re.match(r"(drop|alter|create)\b", low):
            return "structural", None
        if low.startswith("grant"):
            return "grants", None
        if low.startswith("set "):
            return "session", None
        if low.startswith("do "):
            return None, "Anonymous blocks are refused."
        return None, "Cannot classify this statement, so it is refused."

    def prepare_statement(self, text, group):
        return text.strip().rstrip(";").strip()

    def parse_object(self, stmt, group):
        import re
        if group == "source":
            m = re.match(r"create or replace function\s+(?:(\w+)\.)?(\w+)", stmt, re.I)
            return {"type": "FUNCTION", "name": m.group(2).lower(),
                    "schema": (m.group(1) or "").lower() or None}
        if group == "comments":
            m = re.match(r"comment on table\s+(\w+)\s+is\s+'(.*)'", stmt, re.I)
            return {"type": "COMMENT", "name": m.group(1).lower(), "schema": None}
        return None

    def destructive_reason(self, stmt, group):
        low = stmt.lower()
        if group == "data_dml" and low.startswith("delete") and " where " not in low:
            return "no WHERE clause — it touches every row in the table"
        if group == "structural" and low.startswith("drop"):
            return "DROP removes the object and everything in it"
        return None

    def dml_probe(self, stmt):
        import re
        m = re.match(r"delete from (\w+)(?: where (.+))?$", stmt, re.I)
        return (m.group(1), m.group(2)) if m else None

    def _selected(self, conn, target, pred):
        ids = conn.rows.get(target, [])
        if pred and pred.startswith("id <"):
            ids = [i for i in ids if i < int(pred.split("<")[1])]
        return ids

    def measure_row_set(self, conn, target, pred):
        ids = self._selected(conn, target, pred)
        return {"target": target, "predicate": pred or "", "count": len(ids),
                "hash": ",".join(map(str, sorted(ids))), "columns": ["ID"],
                "sample": [[i] for i in ids[:10]], "truncated": len(ids) > 10}

    def snapshot(self, conn, schema, group, ident):
        if group == "source":
            return {k: v for k, v in conn.objects.items()
                    if k.split("(")[0] == ident["name"]}
        if group == "comments":
            return {ident["name"]: conn.comments.get(ident["name"], "")}
        return {}

    def rehearse(self, conn, schema, group, stmt, ident, snapshot):
        if conn.on_execute:
            conn.on_execute(stmt)
        conn.rehearsed.append(stmt)
        if conn.fail:
            return {"error": conn.fail}
        out = {"error": None, "object": None, "type": None, "after": stmt,
               "rows": None, "broken": list(conn.broken_after),
               "own_errors": list(conn.own_after), "verified": True,
               "blind": None, "notes": []}
        if group == "source":
            label = next(iter(snapshot), f"{ident['name']}()")
            conn.pending.append(("objects", label, stmt))
            out.update(object=label, type="FUNCTION")
            if conn.escape:            # committed by the trigger, outside any approval
                conn.objects[label] = stmt
                conn.pending = [x for x in conn.pending if x[1] != label]
                return {"error": "the transaction ended in the trigger",
                        "escaped": conn.escape, "object": label, "type": "FUNCTION"}
        elif group == "comments":
            text = stmt.split("'")[1]
            conn.pending.append(("comments", ident["name"], text))
            out.update(object=ident["name"], type="COMMENT", after=text)
        elif group == "data_dml":
            probe = self.dml_probe(stmt)
            gone = self._selected(conn, *probe) if probe else []
            left = [i for i in conn.rows.get(probe[0], []) if i not in gone] if probe else []
            if probe:
                conn.pending.append(("rows", probe[0], left))
            out["rows"] = conn.rowcount if conn.rowcount is not None else len(gone)
        return out

    def restore_statement(self, group, ident, before, outcome):
        if group == "comments":
            return f"comment on table {ident['name']} is '{before}'"
        if before:
            return before
        return f"drop function {outcome['object']}"

    def privilege_warning(self, conn, schema):
        return None


class FakeHarnessEngine(FakeWriteEngine):
    """FakeWriteEngine plus the harness and release tiers."""

    def split_script(self, text):
        """(line, statement) for each ';'-ended statement; the fake's
        statements hold no ';' of their own."""
        out, line = [], 1
        for part in text.split(";"):
            body = part.strip()
            if body:
                lead = part[:len(part) - len(part.lstrip())]
                out.append((line + lead.count("\n"), body))
            line += part.count("\n")
        return out

    def raw_client_pattern(self):
        return r"fakesql"

    def write_sql_pattern(self):
        return r"\b(create|drop|alter|insert|update|delete|grant|revoke)\b"

    def script_pattern(self):
        return r"(^|\s)(-f|--file)(\s|=)|\s<\s*\S"

    def lint_rules(self):
        """Two rules, enough to drive the framework: an error and a warning,
        each with the line inside the statement it found."""
        import re

        def star(stmt, group, run):
            return [(i + 1, "SELECT * freezes the column list")
                    for i, ln in enumerate(stmt.splitlines())
                    if re.search(r"select\s+\*", ln, re.I)]

        def definer(stmt, group, run):
            if re.search(r"security\s+definer", stmt, re.I) and \
                    not re.search(r"set\s+search_path", stmt, re.I):
                return [(1, "SECURITY DEFINER without SET search_path")]
            return []

        def uncommented(stmt, group, run):
            m = re.match(r"create table (\w+)", stmt, re.I)
            if m and not any(re.match(rf"comment on table {m.group(1)}\b", s, re.I)
                             for s in run):
                return [(1, f"table {m.group(1)} has no COMMENT ON TABLE in this change")]
            return []

        return [{"id": "fake-select-star", "severity": "warn",
                 "summary": "SELECT * in stored code", "check": star},
                {"id": "fake-definer-path", "severity": "error",
                 "summary": "SECURITY DEFINER needs SET search_path", "check": definer},
                {"id": "fake-table-comment", "severity": "warn",
                 "summary": "every table carries a comment", "check": uncommented}]

    def connections_template(self):
        return {"default": "dev", "dev": {"host": "", "user": "", "password": "",
                                          "schema": ""}}

    def skills_dir(self):
        return ROOT / "tests" / "fixtures" / "skills"


class FakePredictEngine(FakeHarnessEngine):
    """An engine that cannot rehearse its source statements (MongoDB's views
    and validators run outside any transaction): it predicts them, core runs
    each once after approval, and the engine checks what landed."""

    def rehearse(self, conn, schema, group, stmt, ident, snapshot):
        if group != "source":
            return super().rehearse(conn, schema, group, stmt, ident, snapshot)
        conn.predicted.append(stmt)
        if conn.fail:
            return {"predicted": True, "error": conn.fail}
        label = next(iter(snapshot), f"{ident['name']}()")
        return {"predicted": True, "error": None, "object": label, "type": "FUNCTION",
                "after": stmt, "rows": None, "broken": list(conn.broken_after),
                "own_errors": list(conn.own_after), "verified": True, "blind": None,
                "notes": ["predicted from the data as it stands"]}

    def execute(self, conn, schema, group, stmt, ident):
        conn.executed.append(stmt)
        if conn.execute_result:
            return dict(conn.execute_result)
        label = next((k for k in conn.objects if k.split("(")[0] == ident["name"]),
                     f"{ident['name']}()")
        conn.objects[label] = stmt         # outside any transaction: it lands now
        return {}

    def verify_written(self, conn, schema, group, stmt, ident, outcome):
        return {"broken": list(outcome.get("broken") or []),
                "own_errors": list(outcome.get("own_errors") or []),
                "differs": list(conn.differs)}


class NoPredictTier(FakePredictEngine):
    """Predicts, but cannot run what it predicted: the gate must refuse."""
    execute = None
    verify_written = None


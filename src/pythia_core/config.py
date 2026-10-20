"""Where a project's connections live, and which one a command uses.

Each engine keeps its own state under `.pythia/<engine>/` — connections,
and from Phase 2 the journal, grants and policy. The folder is per engine on
purpose: pythia for Oracle reads `.pythia/connections.json` and
`.pythia/journal/`, and a project that uses both must never have an Oracle
session pointed at a PostgreSQL entry, or one tool's journal listed as the
other's history.
"""
import json
import os
import pathlib
import sys

STATE_DIR = ".pythia"
CONFIG_NAME = "connections.json"

_MODE_WARNED = set()


def config_rel(engine):
    """The config file's path relative to the project root, for messages."""
    return f"{STATE_DIR}/{engine.name}/{CONFIG_NAME}"


def state_dir(root, engine):
    return pathlib.Path(root) / STATE_DIR / engine.name


def find_config(cwd, env, engine):
    """Return (config dict, project root) or (None, None).

    <PREFIX>_CONFIG points straight at a JSON file; otherwise walk upward from
    cwd for .pythia/<engine>/connections.json. The project root is the
    directory holding .pythia/ — path-based connection inference is anchored
    to it.
    """
    override = env.get(f"{engine.env_prefix}_CONFIG")
    if override:
        path = pathlib.Path(override)
        if not path.is_file():
            sys.exit(f"{engine.env_prefix}_CONFIG points to a missing file: {path}")
        if path.parent.name == engine.name and path.parent.parent.name == STATE_DIR:
            root = path.parent.parent.parent
        else:
            root = path.parent
        return _load_config(path), root
    for d in [cwd, *cwd.parents]:
        path = d / STATE_DIR / engine.name / CONFIG_NAME
        if path.is_file():
            return _load_config(path), d
    return None, None


def _load_config(path):
    if os.name == "posix":
        mode = path.stat().st_mode & 0o777
        if mode & 0o077 and str(path) not in _MODE_WARNED:
            _MODE_WARNED.add(str(path))
            print(f"-- warning: {path} is readable by other users "
                  f"(mode {mode:o}); chmod 600 recommended", file=sys.stderr)
    try:
        cfg = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as e:
        sys.exit(f"Cannot parse {path}: {e}")
    if not isinstance(cfg, dict):
        sys.exit(f"{path} must hold a JSON object of connections.")
    return cfg


def resolve_connection(cfg, explicit, env, cwd, root, engine):
    """Pick a connection. Precedence: --conn, <PREFIX>_CONNECTION, then the
    config file (single entry, the path segment directly under the project
    root, or the entry named by the top-level "default"). Ambiguity is an
    error, never a guess."""
    rel = config_rel(engine)
    cfg = dict(cfg or {})
    fallback = cfg.pop("default", None)
    if fallback is not None and not isinstance(fallback, str):
        example = next((k for k in cfg), "dev")
        sys.exit(f'"default" must name a connection, for example '
                 f'"default": "{example}" — got {json.dumps(fallback)}.')

    lookup = {}
    for k, v in cfg.items():
        if not isinstance(v, dict):
            sys.exit(f"Connection {k!r} in {rel} must be an object of "
                     f"settings, got {json.dumps(v)}.")
        if k.upper() in lookup:
            sys.exit(f"Connection names collide ignoring case in {rel}: "
                     f"{lookup[k.upper()][0]!r} vs {k!r}")
        lookup[k.upper()] = (k, v)
    names = ", ".join(sorted(k for k, _ in lookup.values()))

    if fallback and fallback.upper() not in lookup:
        sys.exit(f'"default" names {fallback!r}, which is not a connection. '
                 f"Available: {names or 'none'}.")

    wanted = explicit or env.get(f"{engine.env_prefix}_CONNECTION")
    if wanted:
        hit = lookup.get(wanted.upper())
        if not hit:
            sys.exit(f"Unknown connection {wanted!r}. Available: {names or 'none'}.")
        return hit

    if not lookup:
        sys.exit(f"No {engine.label} connection configured. Create {rel} in "
                 "the project root (each pythia engine keeps its own "
                 "connections, so Oracle's .pythia/connections.json is not "
                 "read here).")

    if len(lookup) == 1:
        return next(iter(lookup.values()))

    if root is not None:
        try:
            seg = cwd.resolve().relative_to(pathlib.Path(root).resolve()).parts
        except ValueError:
            seg = ()
        if seg:
            hit = lookup.get(seg[0].upper())
            if hit:
                return hit

    # The top-level "default" is a choice the user wrote down rather than a
    # guess, so it is safe to fall back to — and the connection actually
    # used is always echoed on stderr.
    if fallback:
        return lookup[fallback.upper()]

    sys.exit(f"Cannot infer a connection from {cwd}. Available: {names}.\n"
             f"Use --conn NAME, set {engine.env_prefix}_CONNECTION, or name "
             f'one at the top level of {rel}: "default": "<name>"')


def connection_summary(cfg, engine):
    """Everything about the configured connections except the secrets.

    Fields are copied in by name, never by iterating the entry, so a key
    added to the config later cannot leak through here by accident; the
    target comes from the engine, which knows which of its fields are
    secret (a MongoDB URI can carry a password).
    """
    cfg = dict(cfg or {})
    default = cfg.pop("default", None)
    rows = []
    for name, entry in cfg.items():
        if not isinstance(entry, dict):
            continue
        try:
            schema = engine.schema_of(entry) or "—"
        except Exception:                  # noqa: BLE001 — a listing never dies
            schema = "—"
        rows.append({
            "name": name,
            "user": str(entry.get("user") or "—"),
            "schema": schema,
            "target": engine.target_of(entry) or "—",
            "default": isinstance(default, str) and default.upper() == name.upper(),
        })
    return rows

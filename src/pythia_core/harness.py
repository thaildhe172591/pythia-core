"""The harness: the hooks a coding agent's client runs around every tool call.

`hook pre-tool` (Claude Code's PreToolUse) decides one tool call:

- deny a write that would skip the gate: the engine's raw client given
  write SQL or a script, or an MCP SQL tool given write SQL;
- deny a forged approval: a grant written by hand, the console seam or
  grants.mint called from a one-liner, or the pipeline's <PREFIX>_CI
  escape set by the agent;
- deny reading the passwords in connections.json, and editing the policy or
  the journal by hand;
- allow, without a prompt that carries no decision, one plain command of
  this engine that cannot write (a read, a preview, `approve --card`), and a
  confirm the developer has already approved on this connection;
- stay out of everything else (None): the client's own prompt decides.

`hook session-start` prints the brief: the rules and the project's state.

Every hook fails silent. A hook that crashes must never block the
developer's work, so an unreadable payload or config means no decision.
The guard raises the bar; it is not a wall (SECURITY.md): a shell can spell
a command in more ways than any pattern lists, and the gate's own checks —
the token, the grant, the re-rehearsal — hold whatever the hook decided.
"""
import contextlib
import io
import json
import os
import pathlib
import re
import shlex
import shutil
import sys

from . import grants, lease, policy
from .config import (CONFIG_NAME, STATE_DIR, config_rel, connection_summary,
                     find_config, resolve_connection, state_dir)

FILE_TOOLS = ("Read", "Edit", "Write", "MultiEdit", "NotebookEdit")
WRITE_TOOLS = FILE_TOOLS[1:]
# an MCP tool that runs SQL is named like one; any other tool's text (an
# issue body, a document) may mention DROP TABLE and write nothing
SQL_TOOL_RE = re.compile(r"sql|query|exec|statement|database|(^|_)db(_|$)", re.I)


def module_of(engine):
    """The importable package that runs this engine: `python -m <it>`."""
    return type(engine).__module__.split(".")[0]


def _deny(reason):
    return _out("deny", reason)


def _allow(reason):
    return _out("allow", reason)


def _out(decision, reason):
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                   "permissionDecision": decision,
                                   "permissionDecisionReason": reason}}


# --- how this machine runs the engine -------------------------------------------------

def invocation(engine):
    """The engine command as this machine can type it: the bare name when it
    is on PATH, else the launcher beside this interpreter, else the
    interpreter itself with -m. The brief names it, and the guard accepts
    exactly these spellings."""
    if shutil.which(engine.command):
        return engine.command
    launcher = _own_launcher(engine)
    if launcher.is_file():
        return _quote(launcher.as_posix())
    return f"{_quote(pathlib.Path(sys.executable).as_posix())} -m {module_of(engine)}"


def _own_launcher(engine):
    name = engine.command + (".exe" if os.name == "nt" else "")
    return pathlib.Path(sys.executable).with_name(name)


def _quote(path):
    return f'"{path}"' if " " in path else path


def _same_path(a, b):
    try:
        return os.path.normcase(os.path.realpath(a)) == \
            os.path.normcase(os.path.realpath(b))
    except (OSError, ValueError):
        return False


def parse_engine_command(engine, command, cwd):
    """The argv after the invocation for ONE plain command of this engine —
    no operators, substitutions, newlines or comments — else None. Allowing
    a command means allowing all of it, so anything a shell could turn into
    a second command is left to the client's prompt. So is any executable
    that might not be this engine: a path to a file that merely shares its
    name, or `-m` where the working directory would shadow the package."""
    if not command or any(ch in command for ch in "\n\r`#") or re.search(
            r"\$\(|\$\{|<\(|>\(", command):
        return None                   # '#': bash and shlex disagree on it
    try:
        lex = shlex.shlex(command, posix=True, punctuation_chars=True)
        lex.whitespace_split = True
        lex.commenters = ""
        toks = list(lex)
    except ValueError:
        return None
    if not toks or any(t and all(c in "();<>|&" for c in t) for t in toks):
        return None
    first, rest = toks[0], toks[1:]
    if re.sub(r"\.exe$", "", first.lower()) == engine.command.lower():
        return rest                   # by name: PATH decides, not the agent
    if not os.path.isabs(first):
        return None
    if _same_path(first, _own_launcher(engine)):
        return rest
    module = module_of(engine)
    if _same_path(first, sys.executable) and rest[:2] == ["-m", module]:
        base = pathlib.Path(cwd)
        if (base / f"{module}.py").exists() or (base / module).is_dir():
            return None               # -m would import that one
        return rest[2:]
    return None


def classify_argv(engine, argv):
    """read | preview | write | other, decided by the CLI's own parser, so an
    option value never passes for a subcommand and an abbreviation never
    hides a flag. Returns (kind, namespace or None)."""
    from .cli import build_parser
    try:
        with contextlib.redirect_stderr(io.StringIO()), \
                contextlib.redirect_stdout(io.StringIO()):
            ns = build_parser(engine).parse_args(argv)
    except SystemExit as e:
        # exit 0 inside the parser is --help: it prints and stops before any
        # command runs. A parse error is the client's to ask about.
        return ("read" if e.code == 0 else "other"), None
    sub = ns.command
    if getattr(ns, "yes", False):
        return "other", ns            # the developer's flag, at a console
    if sub in READ_COMMANDS:
        return "read", ns
    if sub == "approve":
        return ("read" if ns.card and not ns.hook else "other"), ns
    if sub == "policy":
        return ("read" if ns.action == "show" else "other"), ns
    if sub == "skill":
        return ("read" if ns.action == "lint" else "other"), ns
    if sub == "autopilot":
        # a request delegates nothing and off only tightens; on is the
        # developer's, at a console
        return ("other" if ns.action == "on" else "read"), ns
    if sub == "journal":
        if ns.action in ("list", "show"):
            return "read", ns
        return ("write" if ns.confirm else "preview"), ns
    if sub in ("apply", "deploy"):
        return ("write" if ns.confirm else "preview"), ns
    return "other", ns


READ_COMMANDS = ("version", "connections", "check", "ls", "src", "cols", "ddl",
                 "grep", "deps", "impact", "errors", "history", "doctor", "lint")


# --- what the guard protects -----------------------------------------------------------

def _state_rel(engine):
    return f"{STATE_DIR}/{engine.name}"


def _protected(engine, root, file_path, cwd):
    """'secret' (connections.json), 'grant', 'policy', 'state' (anything
    else under the engine's folder) or None."""
    try:
        target = pathlib.Path(os.path.realpath(pathlib.Path(cwd) / file_path))
        state = pathlib.Path(os.path.realpath(state_dir(root, engine)))
    except (OSError, ValueError):
        return None
    # compared case-blind: Windows and macOS file systems are, and a deny
    # that a capital letter walks past is no deny
    t, s = str(target).casefold(), str(state).casefold().rstrip(os.sep)
    if t != s and not t.startswith(s + os.sep):
        return None
    head = t[len(s):].lstrip(os.sep).split(os.sep)[0]
    if head == CONFIG_NAME:
        return "secret"
    if head == "grants":
        return "grant"
    if head == "policy.json":
        return "policy"
    return "state"


def _reasons(engine):
    run = engine.command
    return {
        "secret": (f"{config_rel(engine)} holds passwords and is never read "
                   f"by an agent. `{run} connections` lists the names, users, "
                   "schemas and targets with no secrets."),
        "grant": ("A grant is the developer's approval; one written by anyone "
                  f"but `{run} approve` at their console is a forged approval. "
                  f"Ask the developer to run `{run} approve <token>`."),
        "policy": (f"The write policy changes only through `{run} policy set "
                   "<group> <value>` — loosening it is the developer's act at "
                   f"a console. `{run} policy` shows it."),
        "state": (f"{_state_rel(engine)}/ is the gate's record (journal, "
                  f"snapshots): it changes only through {run}. "
                  f"`{run} journal list` / `show` read it."),
        "hook": (f"`{run} approve --hook` is the client's door: it reads the "
                 "AskUserQuestion answer Claude Code hands it. Piping a "
                 "payload in by hand would forge the developer's approval."),
        "lease": ("An autopilot lease is the developer's delegation: it is "
                  f"requested with `{run} autopilot request` and approved on its "
                  f"card. Writing the file by hand would forge it; "
                  f"`{run} autopilot status` reads it."),
        "ci": (f"{engine.env_prefix}_CI is the pipeline's escape (a CI job "
               "with no console). An agent setting it would be approving its "
               "own write; the developer approves at their console."),
        "forge": ("This reaches into the gate's approval machinery (the "
                  "console seam, grants.mint) from a one-liner — that would "
                  f"forge the developer's approval. Ask them to run "
                  f"`{run} approve <token>`."),
        "raw": (f"Writes go through `{run} apply <file>` (one statement): "
                "snapshot, rehearsal, preview, approval, journal. The raw "
                f"{engine.label} client here would write with none of them. "
                f"If the developer wants to run it by hand, hand them the file "
                f"and the restore from `{run} history`."),
        "mcp": (f"Writes go through `{run} apply <file>` (one statement): "
                "snapshot, rehearsal, preview, approval, journal. A SQL tool "
                "would write with none of them; it is for reads here."),
    }


def _bash_protected(engine, command):
    """Which protected file a shell command names, if any. A shell command
    cannot be read for intent, so naming the file is enough: `connections`,
    `policy`, `journal list/show` answer every read an agent needs."""
    folder = re.escape(engine.name)
    sep = r"[\\/]+(?:\.[\\/]+)*"
    near = rf"\.pythia{sep}{folder}{sep}"
    if re.search(near + re.escape(CONFIG_NAME), command, re.I):
        return "secret"
    if re.search(near + r"grants", command, re.I) or (
            re.search(r"\.pythia\b", command, re.I)
            and re.search(r"\bgrants\b", command, re.I)):
        return "grant"
    if re.search(near + r"policy\.json", command, re.I):
        return "policy"
    if re.search(near + r"autopilot", command, re.I):
        return "lease"
    return None


FORGE_RE = re.compile(r"FORCE_CONSOLE|_real_console|console_session|"
                      r"human_at_the_keyboard|\bmint\s*\(|\bgrants\s*\.\s*mint\b|"
                      r"\bimport\s+mint\b", re.I)


QUOTED_RE = re.compile(r"'[^']*'" r'|"(?:[^"\\]|\\.)*"')


def unquoted(command):
    """The shell command with every quoted string emptied: what the shell
    itself reads as flags and redirections, not the SQL handed to a client."""
    return QUOTED_RE.sub(lambda m: m.group(0)[0] * 2, command or "")


def client_invoked(pattern, command):
    """The engine's raw client is named as a word anywhere in this shell
    command: at its start, after an operator, behind a wrapper (`env`,
    `sudo`, `docker exec db`, `ssh host`), after variable assignments, or
    opening a quoted command (`bash -c '...'`). Any word, because a shell
    has more ways to start a program than a pattern can list; a mention in
    an echo or a grep costs a denied harmless command, never a missed
    write."""
    return re.search(
        rf"(?:^|[\s;&|(\"'`=])(?:\S*[/\\])?(?:{pattern})(?:\.exe)?"
        r"(?=$|[\s;&|)\"'`])", command or "", re.I) is not None


def quoted_commands(command):
    """Each quoted string in a shell command, unquoted, and the strings
    quoted inside it: where `bash -c '...'` or `ssh host "..."` hands a
    shell a command of its own."""
    for m in QUOTED_RE.finditer(command or ""):
        inner = m.group(0)[1:-1]
        if m.group(0)[0] == '"':
            inner = re.sub(r"\\(.)", r"\1", inner)
        yield inner
        yield from quoted_commands(inner)


def raw_write(engine, command):
    """The client with write SQL anywhere in the command (it is usually
    quoted), or reading a script the way the shell hands it one (`-f`,
    `<`, a pipe) — matched with quoted text emptied, so SQL that holds a
    `<` is not taken for a redirection. A quoted string that itself starts
    the client is a command a shell will run, and is read the same way."""
    client = engine.raw_client_pattern()
    if not client_invoked(client, command):
        return False
    if re.search(engine.write_sql_pattern(), command, re.I):
        return True
    return any(re.search(engine.script_pattern(), unquoted(c), re.I)
               for c in [command] + [q for q in quoted_commands(command)
                                     if client_invoked(client, q)])


# --- the decision -------------------------------------------------------------------------

def pre_tool_decision(engine, payload, now=None):
    """The PreToolUse verdict for one tool call, or None to stay out of it.
    Quiet on stderr, and silent on any failure: config warnings belong to
    the commands, not to a hook that runs before every tool call."""
    try:
        with contextlib.redirect_stderr(io.StringIO()):
            return _decide(engine, payload, now)
    except (Exception, SystemExit):   # noqa: BLE001 — never block on a crash
        return None


def _strings(value):
    """Every string in a tool's input, however deeply it is nested."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for v in value.values():
            yield from _strings(v)
    elif isinstance(value, (list, tuple)):
        for v in value:
            yield from _strings(v)


def _project(engine, cwd):
    try:
        cfg, root = find_config(cwd, os.environ, engine)
    except SystemExit:
        cfg, root = None, None
    return cfg, pathlib.Path(root) if root is not None else cwd


def _decide(engine, payload, now):
    if not isinstance(payload, dict):
        return None
    tool = str(payload.get("tool_name") or "")
    tin = payload.get("tool_input")
    if not tool or not isinstance(tin, dict):
        return None
    cwd = pathlib.Path(payload.get("cwd") or os.getcwd())
    cfg, root = _project(engine, cwd)
    why = _reasons(engine)

    if tool in FILE_TOOLS:
        target = tin.get("file_path") or tin.get("notebook_path")
        if not target:
            return None
        kind = _protected(engine, root, str(target), cwd)
        if kind == "secret" or (kind and tool in WRITE_TOOLS):
            return _deny(why[kind])
        return None

    if tool.startswith("mcp__"):
        own = f"mcp__{engine.command.replace('-', '_')}__"
        rest = tool[len("mcp__"):]
        # an engine whose database takes no SQL names its own MCP tools, and
        # those whose name alone is a write (MongoDB's insert-many)
        named = getattr(engine, "mcp_tool_pattern", None)
        ours = re.search(named(), rest, re.I) if named else SQL_TOOL_RE.search(rest)
        if tool.startswith(own) or not ours:
            return None
        writes = getattr(engine, "mcp_write_tool_pattern", None)
        if writes and re.search(writes(), rest.split("__", 1)[-1], re.I):
            return _deny(why["mcp"])
        # the strings it holds, and the document it is: a command document's
        # keys ({"drop": ...}, $merge) are where a document database writes
        pattern = engine.write_sql_pattern()
        if re.search(pattern, " ".join(_strings(tin)), re.I) or \
                re.search(pattern, json.dumps(tin, ensure_ascii=False, default=str), re.I):
            return _deny(why["mcp"])
        return None

    if tool != "Bash":
        return None
    cmd = str(tin.get("command") or "")
    if re.search(rf"\b{re.escape(engine.env_prefix)}_CI\b", cmd):
        return _deny(why["ci"])
    if FORGE_RE.search(cmd):
        return _deny(why["forge"])
    if re.search(r"\bapprove\b[^\n;&|]*\s--hook\b", cmd) and (
            engine.command in cmd or module_of(engine) in cmd):
        return _deny(why["hook"])
    kind = _bash_protected(engine, cmd)
    if kind:
        return _deny(why[kind])
    if raw_write(engine, cmd):
        return _deny(why["raw"])

    argv = parse_engine_command(engine, cmd, cwd)
    if argv is None:
        return None
    kind, ns = classify_argv(engine, argv)
    if kind == "read":
        return _allow(f"{engine.command} read-only command — it cannot write")
    if payload.get("permission_mode") == "plan" or kind not in ("preview", "write"):
        return None
    conn_name = ns.conn
    if not conn_name:
        try:
            conn_name = resolve_connection(cfg, None, os.environ, cwd, root,
                                           engine)[0]
        except SystemExit:
            conn_name = None
    state = state_dir(root, engine)
    if kind == "preview":
        rec = lease.read(state)
        if lease.status(rec) == "active" and conn_name and \
                str(rec.get("connection", "")).upper() == conn_name.upper():
            return _allow(f"{engine.command} under the developer's autopilot "
                          "lease: it lands only what the lease covers, and "
                          "re-checks the scope itself")
        return _allow(f"{engine.command} preview — rehearsed and rolled back; "
                      "it writes nothing")
    if not conn_name:
        return None
    token = str(ns.confirm or "").strip().lower()
    grant = grants.read(state, token)
    if grant and grants.status(grant, conn_name, now) == "ok":
        return _allow(f"the developer approved {token} ({grant.get('approver')}); "
                      f"{engine.command} re-verifies it before writing")
    return None


# --- the session brief ------------------------------------------------------------------------

def brief(engine):
    """The rules, in the words every engine's session gets. Engine-neutral:
    the engine's command and label are the only blanks."""
    from .doors import mcp_names
    run, label, tool = engine.command, engine.label, mcp_names(engine)[1]
    return f"""\
{run.upper()} HARNESS ACTIVE - Learn, Ask, Do. Before any {label} action:

LEARN  impact before any change: `{run} impact <name>`. Ask the live
       database, never a dump (`ls`, `src`, `cols`, `ddl`, `grep`).
       `{run} lint <files>` before any apply - a change's files in one run;
       errors are fixed, not argued. Read the team's conventions.md if the
       STATE line names it.
       `{run} connections` - never open {config_rel(engine)}.
ASK    relay every `{run} apply` preview verbatim and wait for a real yes.
       >=10 dependents or another schema: show the developer first.
       Refusals and exit 3 (written-but-broken) are relayed, never routed
       around. Never read exit codes through a pipe.
DO     writes only via `{run} apply <file>` (one statement) or `{run}
       deploy <files>` (a release, one transaction), never the raw client. Approval doors - the first that works, the next unasked if
       one fails: AskUserQuestion, its text exactly the output of
       `{run} approve --card <token>`, options Approve / Reject (Claude
       Code); the `{tool}` tool (MCP; Codex starts here);
       `{run} approve <token>` in the developer's terminal. Never mint it.
       Then `{run} apply <file> --confirm <token>`. Evidence before done:
       `{run} errors`; NOT VERIFIED is said plainly. A lease the developer
       approved (`{run} autopilot request`) lands covered writes unasked:
       report each (`{run} autopilot report`); paused means stop and show
       the developer. Skills: `using-{run}`."""


def state_lines(engine, root):
    """The project's live state for the brief — no database."""
    lines = []
    run = engine.command
    cfg_path = state_dir(root, engine) / CONFIG_NAME
    cfg = None
    if cfg_path.is_file():
        try:
            import json
            cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            cfg = None
    if cfg is None:
        lines.append(f"connections: none — `{run} install` scaffolds "
                     f"{config_rel(engine)}")
    else:
        rows = connection_summary(cfg, engine)
        names = ", ".join(r["name"] + ("*" if r["default"] else "") for r in rows)
        lines.append(f"connections: {names or 'none filled in'} (* = default; "
                     f"`{run} connections` for details)")
    lines.append(lease.line(lease.read(state_dir(root, engine))))
    conv = state_dir(root, engine) / "conventions.md"
    if conv.is_file():
        lines.append(f"conventions: {STATE_DIR}/{engine.name}/conventions.md "
                     "exists — read it before writing any object")
    try:
        eff = policy.load(state_dir(root, engine))
        lines.append("policy: " + ", ".join(f"{g} {v}" for g, (v, _) in eff.items()))
    except SystemExit as e:
        lines.append(f"policy: unreadable — {e}")
    return lines


def session_brief(engine, payload):
    cwd = pathlib.Path((payload or {}).get("cwd") or os.getcwd()) \
        if isinstance(payload, dict) else pathlib.Path.cwd()
    _, root = _project(engine, cwd)
    out = [brief(engine)]
    run = invocation(engine)
    if run != engine.command:
        out.append(f"RUN    `{engine.command}` here is `{run}` — spell every "
                   f"{engine.command} command that way, as written, one command "
                   "per call: a shell variable or `;` makes it a compound "
                   "command the guard asks about.")
    out.append("STATE  " + "\n       ".join(state_lines(engine, root)))
    return "\n".join(out) + "\n"


def run_hook(engine, event, stdin=None):
    """Entry point for `<command> hook <event>`; returns what to print."""
    import json
    stdin = sys.stdin if stdin is None else stdin
    try:
        raw = stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
    except (ValueError, OSError):
        payload = {}
    try:
        if event == "session-start":
            return session_brief(engine, payload)
        if event == "pre-tool":
            out = pre_tool_decision(engine, payload)
            return json.dumps(out) + "\n" if out else ""
    except (Exception, SystemExit):   # noqa: BLE001 — fail silent, always
        return ""
    return ""

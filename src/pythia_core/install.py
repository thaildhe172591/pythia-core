"""`install` wires the harness into a project; `doctor` says whether it is
wired. Neither touches a database.

install writes, and leaves everything that is not its own byte-identical:

- the engine's connections.json, scaffolded once with mode 600 and never
  rewritten (it holds passwords the developer filled in);
- .gitignore lines that keep the passwords, this machine's approvals and
  this machine's settings out of every clone;
- .claude/settings.local.json: the hooks (the guard, the session brief and
  the AskUserQuestion answer), spelled with this interpreter's absolute path
  (a hook that cannot start fails silently, and the guard it was meant to be
  goes with it), and deny rules for the engine's files — local, because the
  path is this machine's;
- the MCP approver, in this machine's own files only: Claude Code's local
  scope for this project (~/.claude.json) and Codex's ~/.codex/config.toml,
  each when that client has run here; never in a file the team shares;
- a marked block in CLAUDE.md and AGENTS.md, so the rules load in every
  session whether or not a hook runs; another engine's block (pythia for
  Oracle's among them) is not this one's to touch;
- the engine's skills, in .claude/skills (Claude Code) and .agents/skills
  (AGENTS.md agents).

A second run changes nothing.
"""
import json
import os
import pathlib
import re
import shlex
import sys

from . import doors, harness, skills
from .config import CONFIG_NAME, STATE_DIR, config_rel, state_dir

SETTINGS = pathlib.Path(".claude") / "settings.local.json"
PRE_TOOL_MATCHERS = ("Bash|Read|Edit|Write|MultiEdit|NotebookEdit", "mcp__.*")
SKILL_HOMES = (pathlib.Path(".claude") / "skills", pathlib.Path(".agents") / "skills")


def _interpreter():
    return pathlib.Path(sys.executable).as_posix()


def _launcher(engine):
    return f'"{_interpreter()}" -m {harness.module_of(engine)}'


def hook_command(engine, event):
    """The hook as text, the way doctor shows it."""
    return f"{_launcher(engine)} hook {event}"


def answer_hook_command(engine):
    return f"{_launcher(engine)} approve --hook"


def hook_entry(engine, *argv):
    """One hook in exec form: Claude Code starts the interpreter itself, with
    no shell. A shell-form hook is run through Git Bash on Windows, so every
    tool call paid for a bash start and inherited its stalls (a slow domain
    controller held each one 10-30 s, past the hook's timeout, and the guard
    was skipped). Exec form needs no quoting and expands no $VAR."""
    return {"type": "command", "command": _interpreter(),
            "args": ["-m", harness.module_of(engine), *argv], "timeout": 15}


def hook_text(h):
    """A hook as one line of text, in either form: `command` alone (shell
    form, earlier installs) or `command` plus `args` (exec form)."""
    if not isinstance(h, dict):
        return ""
    cmd = str(h.get("command") or "")
    args = h.get("args")
    if not isinstance(args, list) or not args:
        return cmd
    head = f'"{cmd}"' if (" " in cmd and not cmd.startswith('"')) or args else cmd
    return " ".join([head] + [shlex.quote(str(a)) for a in args])


def _ours(engine, command):
    """A hook this engine wrote, in any spelling: by what it runs. Takes the
    text of a hook (hook_text), so both forms are recognised."""
    cmd = hook_text(command) if isinstance(command, dict) else str(command)
    return bool(re.search(
        rf"(-m\s+{re.escape(harness.module_of(engine))}|"
        rf"{re.escape(engine.command)}(\.exe)?[\"']?)\s+(hook\s|approve\s+--hook\b)",
        cmd))


def wanted_hooks(engine):
    def entry(argv, matcher=None):
        e = {"hooks": [hook_entry(engine, *argv)]}
        if matcher:
            e = {"matcher": matcher, **e}
        return e
    return {"SessionStart": [entry(("hook", "session-start"))],
            "PreToolUse": [entry(("hook", "pre-tool"), m)
                           for m in PRE_TOOL_MATCHERS],
            # the chat door; never "" or "*": a hook that catches every tool
            # makes AskUserQuestion return empty
            "PostToolUse": [entry(("approve", "--hook"), "AskUserQuestion")]}


def deny_rules(engine):
    rel = f"./{STATE_DIR}/{engine.name}"
    return [f"Read({rel}/{CONFIG_NAME})", f"Edit({rel}/**)",
            f"Bash({engine.command} approve --hook:*)"]


def ignore_lines(engine):
    rel = f"{STATE_DIR}/{engine.name}"
    return [f"{rel}/{CONFIG_NAME}", f"{rel}/grants/", SETTINGS.as_posix()]


def markers(engine):
    return (f"<!-- {engine.command}:begin (managed by `{engine.command} install`; "
            "edit outside this block) -->", f"<!-- {engine.command}:end -->")


def instructions_block(engine):
    begin, end = markers(engine)
    run = engine.command
    return (f"{begin}\n"
            f"## {run} — {engine.label} through pythia\n\n"
            f"{harness.brief(engine)}\n\n"
            f"HOOKS NOT RUNNING? {run}'s SessionStart hook prints a `STATE` "
            f"line. With no STATE line this session, the guard is off on this "
            "machine: say so to the developer before any write and ask them "
            f"to run `{run} doctor`. Every rule here still binds.\n"
            f"{end}")


# --- the pieces ---------------------------------------------------------------------

def write_config(engine, root):
    path = state_dir(root, engine) / CONFIG_NAME
    if path.exists():
        return path, "kept"
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(engine.connections_template(), indent=2) + "\n"
    # created 600 in one step: there is no moment it is readable by others
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    return path, "created"


def _ignored(text, line):
    names = {line.rstrip("/"), line.rstrip("/") + "/"}
    hit = False
    for ln in text.splitlines():
        s = ln.strip()
        neg = s.startswith("!")
        body = (s[1:] if neg else s).lstrip("/")
        if body in names:
            hit = not neg
    return hit


def write_gitignore(engine, root):
    path = pathlib.Path(root) / ".gitignore"
    data = path.read_bytes() if path.is_file() else b""
    text = data.decode("utf-8", errors="replace")
    gaps = [ln for ln in ignore_lines(engine) if not _ignored(text, ln)]
    if not gaps:
        return path, "unchanged"
    with open(path, "ab") as f:                 # their bytes stay theirs
        f.write((b"\n" if data and not data.endswith(b"\n") else b"")
                + f"# {engine.command}: passwords, this machine's approvals and "
                  "settings — never committed\n".encode("utf-8")
                + "".join(g + "\n" for g in gaps).encode("utf-8"))
    return path, "updated" if data else "created"


def merge_settings(engine, settings):
    """Our hooks and deny rules into Claude Code's settings, in place.
    Every key and hook that is not ours survives in its order; ours are
    replaced where they stood (appended the first time), so a re-run with a
    moved interpreter respells them, and a re-run among other engines' hooks
    moves nothing."""
    hooks = settings.setdefault("hooks", {})
    for event, ours in wanted_hooks(engine).items():
        kept, at = [], None
        for entry in hooks.get(event) or []:
            if not isinstance(entry, dict):
                kept.append(entry)
                continue
            inner = [h for h in entry.get("hooks") or []
                     if not (isinstance(h, dict) and _ours(engine, hook_text(h)))]
            if at is None and len(inner) != len(entry.get("hooks") or []):
                at = len(kept)
            if inner:
                kept.append({**entry, "hooks": inner})
        at = len(kept) if at is None else at
        hooks[event] = kept[:at] + ours + kept[at:]
    deny = settings.setdefault("permissions", {}).setdefault("deny", [])
    deny.extend(r for r in deny_rules(engine) if r not in deny)
    return settings


def write_settings(engine, root):
    path = pathlib.Path(root) / SETTINGS
    try:
        old = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
        if not isinstance(old, dict):
            raise ValueError("not an object")
    except ValueError:
        return path, "unreadable"               # theirs: never clobbered
    new = merge_settings(engine, json.loads(json.dumps(old)))
    if new == old and path.is_file():
        return path, "unchanged"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(new, indent=2, ensure_ascii=False) + "\n",
                    encoding="utf-8")
    return path, "updated" if old else "created"


def merge_block(path, block, begin, end):
    """Only the marked block is ours: replaced in place when present,
    appended below the developer's text when not."""
    path = pathlib.Path(path)
    old = path.read_text(encoding="utf-8") if path.is_file() else None
    if old is None:
        new, action = block + "\n", "created"
    elif begin in old and end in old[old.index(begin):]:
        pre = old[:old.index(begin)]
        post = old[old.index(end, old.index(begin)) + len(end):]
        new = pre + block + post
        action = "unchanged" if new == old else "updated"
    else:
        tail = "" if old.endswith("\n\n") else "\n" if old.endswith("\n") else "\n\n"
        new, action = old + tail + block + "\n", "added"
    if new != old:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(new, encoding="utf-8")
    return path, action


def copy_skills(engine, root):
    """The engine's pack into each skill home; a file is written only when
    its bytes differ."""
    written = 0
    names = []
    for skill in skills.pack(engine.skills_dir()):
        names.append(skill.name)
        for home in SKILL_HOMES:
            for rel in skills.files_of(skill):
                dst = pathlib.Path(root) / home / skill.name / rel
                data = (skill / rel).read_bytes()
                if dst.is_file() and dst.read_bytes() == data:
                    continue
                dst.parent.mkdir(parents=True, exist_ok=True)
                dst.write_bytes(data)
                written += 1
    return names, written


# --- the MCP approver, registered where only this machine reads it ------------------

def project_key(root):
    """How Claude Code names a project in ~/.claude.json: its absolute path,
    with forward slashes on Windows."""
    path = pathlib.Path(root).resolve()
    return path.as_posix() if os.name == "nt" else str(path)


def mcp_entry(engine):
    return {"type": "stdio", "command": sys.executable,
            "args": ["-m", harness.module_of(engine), "mcp"], "env": {}}


def _home(home=None):
    return pathlib.Path(home) if home else pathlib.Path.home()


def register_claude_mcp(engine, root, home=None):
    """The approver in Claude Code's local scope for this project: the
    file is this machine's, so the interpreter's path is right there."""
    path = _home(home) / ".claude.json"
    if not path.is_file():
        return path, "absent"
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(doc, dict):
            raise ValueError("not an object")
    except (ValueError, OSError):
        return path, "unreadable"
    server, _ = doors.mcp_names(engine)
    projects = doc.setdefault("projects", {})
    if not isinstance(projects, dict):
        return path, "unreadable"
    proj = projects.setdefault(project_key(root), {})
    servers = proj.setdefault("mcpServers", {})
    if servers.get(server) == mcp_entry(engine):
        return path, "unchanged"
    servers[server] = mcp_entry(engine)
    tmp = path.with_name(path.name + f".{engine.name}.tmp")
    tmp.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n",
                   encoding="utf-8")
    os.replace(str(tmp), str(path))           # never a half-written file
    return path, "updated"


def codex_markers(engine):
    return (f"# {engine.command}:begin (managed by `{engine.command} install`)",
            f"# {engine.command}:end")


def codex_block(engine):
    server, _ = doors.mcp_names(engine)
    begin, end = codex_markers(engine)
    return (f"{begin}\n[mcp_servers.{server}]\n"
            f"command = {json.dumps(sys.executable)}\n"
            f'args = ["-m", "{harness.module_of(engine)}", "mcp"]\n{end}')


def register_codex_mcp(engine, home=None):
    """The approver in Codex's own config. The stdlib cannot write TOML, so
    the block is text between markers; a table of that name written by
    anyone else is a conflict, reported and left alone."""
    base = _home(home) / ".codex"
    path = base / "config.toml"
    if not base.is_dir():
        return path, "absent"
    begin, end = codex_markers(engine)
    server, _ = doors.mcp_names(engine)
    old = path.read_text(encoding="utf-8") if path.is_file() else ""
    if begin not in old and re.search(rf"^\s*\[mcp_servers\.{re.escape(server)}\]",
                                      old, re.M):
        return path, "conflict"
    _, action = merge_block(path, codex_block(engine), begin, end)
    return path, action


def install(engine, root):
    """Wire the project; returns the lines to print."""
    root = pathlib.Path(root)
    lines = []
    path, action = write_config(engine, root)
    lines.append(f"{action:<10} {_rel(path, root)}"
                 + (" (mode 600 — the developer fills in the passwords; an "
                    "agent never opens it)" if action == "created" else ""))
    path, action = write_gitignore(engine, root)
    lines.append(f"{action:<10} {_rel(path, root)}")
    path, action = write_settings(engine, root)
    if action == "unreadable":
        lines.append(f"SKIPPED    {_rel(path, root)} does not parse as JSON — "
                     "fix it and re-run; the hooks are NOT wired")
    else:
        lines.append(f"{action:<10} {_rel(path, root)} (hooks: session-start, "
                     "pre-tool guard; deny rules for the engine's files)")
    begin, end = markers(engine)
    for name in ("CLAUDE.md", "AGENTS.md"):
        path, action = merge_block(root / name, instructions_block(engine),
                                   begin, end)
        lines.append(f"{action:<10} {name} ({engine.command} block)")
    for label, (path, action) in (
            ("~/.claude.json", register_claude_mcp(engine, root)),
            ("~/.codex/config.toml", register_codex_mcp(engine))):
        server, tool = doors.mcp_names(engine)
        if action == "absent":
            lines.append(f"{'-':<10} {label}: not found (that client has not "
                         "run here) — re-run install after it has")
        elif action in ("unreadable", "conflict"):
            lines.append(f"SKIPPED    {label}: "
                         + ("does not parse — fix it and re-run"
                            if action == "unreadable" else
                            f"[mcp_servers.{server}] is already there and is not "
                            "ours — left alone")
                         + f"; the {tool} door is not registered there")
        else:
            lines.append(f"{action:<10} {label} (MCP server {server}, "
                         f"tool {tool})")
    names, written = copy_skills(engine, root)
    lines.append(f"{'updated' if written else 'unchanged':<10} skills "
                 f"({', '.join(names) or 'none shipped'}) in "
                 + " and ".join(h.as_posix() for h in SKILL_HOMES))
    lines.append(f"\nNext: fill in {config_rel(engine)} (the developer, not the "
                 f"agent), then `{engine.command} doctor` and `{engine.command} "
                 "check`. Restart the agent's session so the hooks load.")
    return lines


def _rel(path, root):
    try:
        return pathlib.Path(path).relative_to(root).as_posix()
    except ValueError:
        return str(path)


# --- doctor -------------------------------------------------------------------------

def _check(status, what, detail):
    return {"status": status, "what": what, "detail": detail}


def _settings_files(root):
    base = pathlib.Path(root) / ".claude"
    return [base / "settings.json", base / "settings.local.json"]


def _hook_entries(engine, root):
    """(event, matcher, command) of every hook of ours in the project."""
    found = []
    for path in _settings_files(root):
        try:
            s = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
        except (ValueError, OSError):
            continue
        for event, entries in ((s.get("hooks") or {}) if isinstance(s, dict)
                               else {}).items():
            for entry in entries if isinstance(entries, list) else []:
                if not isinstance(entry, dict):
                    continue
                for h in entry.get("hooks") or []:
                    if isinstance(h, dict) and _ours(engine, hook_text(h)):
                        found.append((event, entry.get("matcher"), hook_text(h)))
    return found


def _starts(command):
    """Whether the interpreter a hook names exists on this machine."""
    try:
        first = shlex.split(command)[0]
    except (ValueError, IndexError):
        return False
    if os.path.isabs(first):
        return os.path.isfile(first)
    import shutil
    return shutil.which(first) is not None


def _breakers(root, home=None):
    """Settings that answer AskUserQuestion before the developer sees it."""
    found = []
    files = _settings_files(root) + [_home(home) / ".claude" / "settings.json",
                                     _home(home) / ".claude" / "settings.local.json"]
    for path in files:
        try:
            s = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
        except (ValueError, OSError):
            continue
        if not isinstance(s, dict):
            continue
        perms = s.get("permissions") if isinstance(s.get("permissions"), dict) else {}
        where = path.name if path.parent.parent == pathlib.Path(root) else str(path)
        if perms.get("defaultMode") == "dontAsk":
            found.append(f"defaultMode is dontAsk in {where} — it denies "
                         "AskUserQuestion and the MCP approver")
        if any(str(r).split("(", 1)[0] == "AskUserQuestion"
               for r in perms.get("allow") or []):
            found.append(f"an allow rule for AskUserQuestion in {where} "
                         "answers it without the developer")
        for entry in (s.get("hooks") or {}).get("PreToolUse") or []:
            if isinstance(entry, dict) and entry.get("matcher") in (None, "", "*"):
                found.append(f"a PreToolUse hook with an empty or '*' matcher in "
                             f"{where} makes AskUserQuestion return empty")
                break
    return found


def _chat_door(engine, root, hooks):
    mine = [c for e, m, c in hooks if e == "PostToolUse"
            and m == "AskUserQuestion" and "approve --hook" in c]
    if not mine:
        return _check("warn", "chat door", "the AskUserQuestion answer hook is "
                      f"not wired — `{engine.command} install` writes it")
    if not _starts(mine[0]):
        return _check("warn", "chat door", f"cannot start: {mine[0]} — "
                      f"`{engine.command} install` respells it")
    broken = _breakers(root)
    if os.environ.get("CLAUDE_CODE_CHILD_SESSION"):
        _, tool = doors.mcp_names(engine)
        broken.append(f"{doors.CHILD_SESSION_NOTE} — use the {tool} door")
    if broken:
        return _check("warn", "chat door", "; ".join(broken))
    return _check("ok", "chat door", "AskUserQuestion with the card, answered "
                  "through `approve --hook`")


def _chat_door_last(engine, root):
    """The last real outcome of the chat door, or None: a door wired
    correctly that never mints is otherwise invisible to doctor."""
    last = doors.last_outcome(state_dir(root, engine))
    if not last:
        return None
    _, tool = doors.mcp_names(engine)
    if last.get("minted"):
        return _check("ok", "chat door last", f"{last.get('at')}: token "
                      f"{last.get('token')} minted in chat")
    detail = str(last.get("detail") or "")
    return _check("warn", "chat door last",
                  f"{last.get('at')}: token {last.get('token')} — no grant "
                  "minted: " + (detail or str(last.get("outcome") or ""))[:300]
                  + (f" — if this repeats, approvals go through {tool}"
                     if detail else ""))


def _mcp_door(engine, root, home=None):
    server, tool = doors.mcp_names(engine)
    path = _home(home) / ".claude.json"
    try:
        doc = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
        entry = doc["projects"][project_key(root)]["mcpServers"][server]
    except (ValueError, OSError, KeyError, TypeError):
        return _check("warn", "mcp approver", f"{tool} is not registered for "
                      f"this project in ~/.claude.json — `{engine.command} "
                      "install` registers it once Claude Code has run here")
    if not os.path.isfile(str(entry.get("command") or "")):
        return _check("warn", "mcp approver", f"{tool} cannot start: "
                      f"{entry.get('command')} is gone — `{engine.command} "
                      "install` respells it")
    return _check("ok", "mcp approver", f"{tool} (MCP server {server})")


def doctor(engine, root):
    root = pathlib.Path(root)
    run = engine.command
    out = []
    cfg = state_dir(root, engine) / CONFIG_NAME
    if not cfg.is_file():
        out.append(_check("fail", "connections", f"{config_rel(engine)} is missing "
                          f"— `{run} install` scaffolds it"))
    else:
        try:
            data = json.loads(cfg.read_text(encoding="utf-8"))
            entries = {k: v for k, v in data.items()
                       if k != "default" and isinstance(v, dict)}
            empty = [k for k, v in entries.items()
                     if not str(v.get("host") or v.get("uri") or "").strip()]
            if empty:
                out.append(_check("warn", "connections", f"{', '.join(empty)} has "
                                  "no target yet — the developer fills it in"))
            else:
                out.append(_check("ok", "connections",
                                  f"{len(entries)} in {config_rel(engine)}"))
        except (ValueError, AttributeError):
            out.append(_check("fail", "connections",
                              f"{config_rel(engine)} does not parse as a JSON object"))
        if os.name == "posix" and cfg.stat().st_mode & 0o077:
            out.append(_check("warn", "connections", f"{config_rel(engine)} is "
                              f"readable by other users — chmod 600 {config_rel(engine)}"))
    gi = root / ".gitignore"
    text = gi.read_text(encoding="utf-8", errors="replace") if gi.is_file() else ""
    if _ignored(text, f"{STATE_DIR}/{engine.name}/{CONFIG_NAME}"):
        out.append(_check("ok", "credentials", f"{config_rel(engine)} is git-ignored"))
    else:
        out.append(_check("warn", "credentials", f"{config_rel(engine)} is not in "
                          f".gitignore — one `git add -A` commits the passwords; "
                          f"`{run} install` adds it"))
    hooks = _hook_entries(engine, root)
    for event, label in (("SessionStart", "session-start"), ("PreToolUse", "pre-tool")):
        mine = [(m, c) for e, m, c in hooks if e == event and f"hook {label}" in c]
        shown = mine[0][1] if mine else ""
        if not mine:
            out.append(_check("fail", f"hook {label}", f"not wired in .claude/ — "
                              f"`{run} install` writes it"))
        elif not all(_starts(c) for _, c in mine):
            bad = next(c for _, c in mine if not _starts(c))
            out.append(_check("fail", f"hook {label}", f"cannot start: {bad} — the "
                              f"interpreter is gone; `{run} install` respells it"))
        elif event == "PreToolUse" and not any(
                m and re.search(m, "Bash") for m, _ in mine):
            out.append(_check("fail", f"hook {label}", "does not see Bash calls — "
                              f"`{run} install` restores its matchers"))
        else:
            out.append(_check("ok", f"hook {label}", shown))
    out.append(_chat_door(engine, root, hooks))
    last = _chat_door_last(engine, root)
    if last:
        out.append(last)
    out.append(_mcp_door(engine, root))
    deny = []
    for path in _settings_files(root):
        try:
            s = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
            deny += list(((s.get("permissions") or {}).get("deny")) or [])
        except (ValueError, OSError, AttributeError):
            pass
    gap = [r for r in deny_rules(engine) if r not in deny]
    out.append(_check("warn" if gap else "ok", "deny rules",
                      f"missing {', '.join(gap)} — `{run} install` adds them"
                      if gap else ", ".join(deny_rules(engine))))
    begin, _ = markers(engine)
    for name in ("CLAUDE.md", "AGENTS.md"):
        p = root / name
        body = p.read_text(encoding="utf-8") if p.is_file() else ""
        if instructions_block(engine) in body:
            out.append(_check("ok", "instructions", f"{name} carries the {run} block"))
        elif begin in body:
            out.append(_check("warn", "instructions", f"{name}'s {run} block is from "
                              f"another version — `{run} install` refreshes it"))
        else:
            out.append(_check("warn", "instructions", f"{name} has no {run} block — "
                              f"`{run} install` adds it"))
    stale = []
    for skill in skills.pack(engine.skills_dir()):
        for home in SKILL_HOMES:
            dst = root / home / skill.name
            if any(not (dst / rel).is_file()
                   or (dst / rel).read_bytes() != (skill / rel).read_bytes()
                   for rel in skills.files_of(skill)):
                stale.append(f"{home.as_posix()}/{skill.name}")
    out.append(_check("warn" if stale else "ok", "skills",
                      f"missing or stale: {', '.join(stale)} — `{run} install` "
                      "refreshes them" if stale else "the pack is installed"))
    team = sorted({p for home in SKILL_HOMES
                   for p in (root / home).glob("*/SKILL.md")})
    broken = []
    for name, path, differ in skills.unique(team):
        if skills.lint(path):
            order = [h.as_posix() for h in SKILL_HOMES]
            rel = sorted((_rel(d, root) for d in differ or []),
                         key=lambda r: order.index(r) if r in order else len(order))
            broken.append(name + (f" ({' and '.join(rel)} differ)" if differ else ""))
    if broken:
        # a count and a few names: a team's third-party packs rarely follow
        # pythia's skill style, and a wall of findings buries every other row
        out.append(_check("warn", "skill lint",
                          f"{len(broken)} skill{'s' if len(broken) != 1 else ''} "
                          "do not follow the pythia skill style, e.g. "
                          + ", ".join(sorted(broken, key=lambda b: ("differ" not in b, b))[:3])
                          + f" — `{run} skill lint` lists all"))
    out.append(_hooks_active(engine, root, hooks))
    return out


# --- wired is not active: the session stamp ------------------------------------------

def hooks_fingerprint(engine, root):
    """A short hash of this engine's hook entries as the settings hold them."""
    import hashlib
    text = json.dumps(sorted(_hook_entries(engine, root), key=str))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def stamp_session(engine, root):
    """Called by the session-start hook: the hooks it started under."""
    try:
        path = state_dir(root, engine) / "session-hooks.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        import time
        path.write_text(json.dumps({"fingerprint": hooks_fingerprint(engine, root),
                                    "at": time.strftime("%Y-%m-%d %H:%M:%S")}),
                        encoding="utf-8")
    except OSError:
        pass


def _hooks_active(engine, root, hooks):
    """Wired in settings is not active in a session that is already running:
    Claude Code reads hooks when a session starts."""
    if not hooks:
        return _check("warn", "hooks active", "no hooks wired, so none is active")
    try:
        stamp = json.loads((state_dir(root, engine) / "session-hooks.json")
                           .read_text(encoding="utf-8"))
    except (OSError, ValueError):
        stamp = None
    if not isinstance(stamp, dict):
        return _check("warn", "hooks active", "wired — they take effect in new "
                      "agent sessions; no session has started under them yet, so "
                      "restart any agent session already running")
    if stamp.get("fingerprint") != hooks_fingerprint(engine, root):
        return _check("warn", "hooks active", f"the hooks changed after the last "
                      f"session started ({stamp.get('at')}) — a session running "
                      "since then is guarded by the old ones, or none: restart it")
    return _check("ok", "hooks active", f"the last session started under these "
                  f"hooks ({stamp.get('at')})")

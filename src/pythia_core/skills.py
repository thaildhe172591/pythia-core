"""The skill contract: what stops a SKILL.md from triggering or loading.
Ported from pythia's skill lint; each engine's suite holds its shipped pack
to it, and `<command> skill lint` holds a team's own skills to it too.
"""
import pathlib
import re

MAX_LINES = 150
MAX_DESCRIPTION = 1024


def lint(path):
    """Every problem with one SKILL.md, as messages; [] when it holds."""
    path = pathlib.Path(path)
    out = []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        return [f"cannot read: {e}"]
    m = re.match(r"^---\n(.*?)\n---\n", text.replace("\r\n", "\n"), re.S)
    if not m:
        return ["no YAML frontmatter (--- name/description --- at the top)"]
    fields = {}
    for line in m.group(1).splitlines():
        km = re.match(r"^([A-Za-z_][\w.-]*):\s+(.*)$", line)
        if km:
            fields[km.group(1)] = km.group(2)
            if km.group(2)[:1] not in "\"'" and ": " in km.group(2):
                out.append(f"frontmatter {km.group(1)!r} holds an unquoted "
                           "': ' — invalid YAML, the skill loses its trigger")
    folder = path.parent.name
    if fields.get("name") != folder:
        out.append(f"name {fields.get('name')!r} must match the folder {folder!r}")
    desc = fields.get("description", "")
    if not desc:
        out.append("description missing — the agent cannot know when to fire")
    elif not desc.lower().startswith(("use when", "use before")):
        out.append("description must open with its trigger: 'Use when ...' "
                   "or 'Use before ...'")
    if len(desc) > MAX_DESCRIPTION:
        out.append(f"description is {len(desc)} chars (max {MAX_DESCRIPTION})")
    n = text.count("\n") + 1
    if n > MAX_LINES:
        out.append(f"{n} lines (max {MAX_LINES}) — push detail into reference/")
    if "**Phase:**" not in text:
        out.append("no **Phase:** line — say which of Learn / Ask / Do it owns")
    for ref in re.findall(r"`?(reference/[\w./-]+\.md)`?", text):
        if not (path.parent / ref).is_file():
            out.append(f"links {ref}, which does not exist")
    return out


def pack(skills_dir):
    """The skill folders a pack ships, by name, in order."""
    base = pathlib.Path(skills_dir)
    if not base.is_dir():
        return []
    return sorted(p.parent for p in base.glob("*/SKILL.md"))


def files_of(skill):
    """Every file of one skill folder, relative, in order."""
    skill = pathlib.Path(skill)
    return sorted(p.relative_to(skill) for p in skill.rglob("*")
                  if p.is_file() and "__pycache__" not in p.parts)

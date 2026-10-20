"""The journal: every preview and every write, on disk before anything runs.

One folder per run under `.pythia/<engine>/journal/`: before.sql (the
snapshot), after.sql (what the statement makes), restore.sql (the statement
that puts things back, when there is one) and meta.json. Nothing turns it
off. Ported from pythia.
"""
import datetime
import json
import re
import sys


def root(state):
    return state / "journal"


def write_entry(state, otype, name, before, after, meta, restore="", now=None):
    now = now or datetime.datetime.now()
    # quoted identifiers can hold '/', ':' or '..' — a folder name must not
    safe = re.sub(r"[^A-Za-z0-9_$#-]", "_", str(name))[:100] or "_"
    eid = now.strftime("%Y-%m-%dT%H-%M-%S") + f"_{safe}_{otype.replace(' ', '-')}"
    d = root(state) / eid
    n = 1
    while d.exists():           # apply and restore can land in one second;
        n += 1                  # overwriting an entry destroys an undo
        d = root(state) / f"{eid}-{n}"
    d.mkdir(parents=True)
    (d / "before.sql").write_text(before, encoding="utf-8")
    (d / "after.sql").write_text(after, encoding="utf-8")
    (d / "restore.sql").write_text(restore, encoding="utf-8")
    full = {"object": name, "type": otype, "entry": d.name, **meta}
    (d / "meta.json").write_text(json.dumps(full, indent=2, default=str) + "\n",
                                 encoding="utf-8")
    return d.name


def update_entry(state, eid, before=None, after=None, restore=None, **meta):
    """Rewrite one entry in place: one run is one entry, however many steps
    it takes."""
    d = root(state) / eid
    for name, text in (("before.sql", before), ("after.sql", after),
                       ("restore.sql", restore)):
        if text is not None:
            (d / name).write_text(text, encoding="utf-8")
    full = json.loads((d / "meta.json").read_text(encoding="utf-8"))
    full.update(meta)
    (d / "meta.json").write_text(json.dumps(full, indent=2, default=str) + "\n",
                                 encoding="utf-8")
    return full


def read_entry(state, eid):
    d = root(state) / eid
    if not re.fullmatch(r"[A-Za-z0-9_$#.-]+", str(eid)) or ".." in str(eid) \
            or not d.is_dir():
        recent = ", ".join(list_entries(state)[:5]) or "none"
        sys.exit(f"No journal entry {eid!r}. Recent: {recent}.")
    return {"before": (d / "before.sql").read_text(encoding="utf-8"),
            "after": (d / "after.sql").read_text(encoding="utf-8"),
            "restore": (d / "restore.sql").read_text(encoding="utf-8"),
            "meta": json.loads((d / "meta.json").read_text(encoding="utf-8")),
            "path": d}


def list_entries(state):
    d = root(state)
    if not d.is_dir():
        return []
    return sorted((p.name for p in d.iterdir() if p.is_dir()), reverse=True)


def find_by_token(state, token, kinds=("preview", "applied"), exclude=None):
    """The newest entry of one of `kinds` carrying a token. A person approves
    an object and a diff, never a bare hash — so no entry means nothing to
    approve. A refused confirm carries the token too, and is skipped: it
    neither replaces the preview nor spends it. A confirm run excludes its own
    entry: compared with itself, every verification would match."""
    for eid in list_entries(state):
        if eid == exclude:
            continue
        try:
            meta = read_entry(state, eid)["meta"]
        except SystemExit:
            continue
        if str(meta.get("token", "")) == token and kind(meta) in kinds:
            return eid, meta
    return None, None


def kind(meta):
    if meta.get("applied"):
        return "applied"
    if meta.get("refused"):
        return "refused"
    return "preview"

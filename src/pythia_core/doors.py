"""The approval doors a chat can open. Ported from pythia.

A grant is a person's Approve on pythia's own card for one token. Three
doors lead to it, and the agent tries them in order, moving to the next
without asking when one fails:

1. **AskUserQuestion** (Claude Code). The agent asks with the text of
   `<command> approve --card <token>` verbatim, options Approve / Reject.
   Claude Code then runs `<command> approve --hook` (PostToolUse), which
   mints only when the question carried the card for that token AND the
   developer chose Approve themselves. An answer the agent wrote into its
   own call, a dialog that closed with nobody at the keyboard, a typed
   reply and no answer at all are each named, and mint nothing.
2. **The MCP approver** (`<command> mcp`, tool `<server>_approve`). An
   elicitation shows the card and mints on an explicit Approve. A decline
   back faster than anyone could read the card is the client answering for
   nobody, and is said so.
3. **The developer's terminal:** `<command> approve <token>` (gate.py).

Every door ends in gate.grant_approval, so no door grants more than
another. None of them touches the database.
"""
import json
import re
import sys
import time

from . import __version__, gate

TOKEN_IN_QUESTION = re.compile(r"\bapprove\s+(?:--card\s+)?([0-9a-z]{6,10})\b", re.I)
MCP_PROTOCOL = "2025-06-18"
MCP_ELICIT_METHOD = "elicitation/create"
# no one reads a card and answers it faster than this: a quicker non-accept
# is the client answering for nobody (a host with no prompt UI declines at once)
MCP_HUMAN_FLOOR_S = 1.5


def mcp_names(engine):
    """(server, tool): `pythia_pg` and `pythia_pg_approve` for pythia-pg."""
    server = engine.command.replace("-", "_")
    return server, f"{server}_approve"


def squash(text):
    """Whitespace- and case-blind form, for 'did the question carry the card'."""
    return " ".join(str(text).split()).lower()


def card_message(engine, token, body):
    return f"{engine.command} approve {token}\n" + "\n".join(body)


def next_door_after_chat(engine, token):
    _, tool = mcp_names(engine)
    return (f"Next door, now — no need to ask the developer first: call the "
            f"{tool} tool (MCP) with token {token}; if it reports that the "
            "client did not show its card, or it is not available, relay "
            f"`{engine.command} approve {token}` for the developer's terminal.")


def next_door_after_mcp(engine, token):
    _, tool = mcp_names(engine)
    return (f"Next door, now — no need to ask the developer first: if you have "
            f"not yet asked with AskUserQuestion for this token, do it — its "
            f"text exactly the output of `{engine.command} approve --card "
            f"{token}`, options Approve / Reject; otherwise, or with no "
            f"AskUserQuestion (Codex), relay `{engine.command} approve {token}` "
            f"for the developer's terminal. Do not call {tool} again this "
            "session.")


# --- door 1: AskUserQuestion, answered through the PostToolUse hook --------------------

def _tool_input_in(raw, tool_use_id, asked):
    """The input of an AskUserQuestion tool_use in one transcript line, as
    the model sent it (the wire copy when the line keeps one), or None."""
    if b"AskUserQuestion" not in raw:
        return None
    try:
        d = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(d, dict) or d.get("type") != "assistant":
        return None
    wire = d.get("wireToolInputs") if isinstance(d.get("wireToolInputs"), dict) else {}
    for c in (d.get("message") or {}).get("content") or []:
        if not (isinstance(c, dict) and c.get("type") == "tool_use"
                and c.get("name") == "AskUserQuestion"):
            continue
        sent = wire.get(c.get("id"))
        sent = sent if isinstance(sent, dict) else c.get("input")
        if not isinstance(sent, dict):
            continue
        if tool_use_id:
            if c.get("id") == tool_use_id:
                return sent
        elif [q.get("question") for q in sent.get("questions") or []
              if isinstance(q, dict)] == asked:
            return sent
    return None


def transcript_tool_input(payload, asked, chunk=1 << 20, limit=64 << 20,
                          retries=2, pause=0.25):
    """What the model itself sent in this AskUserQuestion call, read from
    the session transcript; None when it cannot be found. Current Claude
    Code writes the person's answer into tool_input (and drops any answers
    the model wrote before it shows the dialog), so tool_input alone cannot
    say who answered; the transcript keeps the call as the model sent it.
    Read from the end, where the call is; bounded, and retried briefly in
    case the line is still being written."""
    path = payload.get("transcript_path")
    tid = payload.get("tool_use_id") if isinstance(payload.get("tool_use_id"), str) else None
    if not isinstance(path, str) or not path:
        return None
    for attempt in range(retries + 1):
        try:
            with open(path, "rb") as f:
                f.seek(0, 2)
                pos, tail, read = f.tell(), b"", 0
                while pos > 0 and read < limit:
                    step = min(chunk, pos)
                    pos -= step
                    read += step
                    f.seek(pos)
                    lines = (f.read(step) + tail).split(b"\n")
                    tail = lines.pop(0) if pos > 0 else b""
                    for raw in reversed(lines):
                        found = _tool_input_in(raw, tid, asked)
                        if found is not None:
                            return found
        except OSError:
            return None
        if attempt < retries:
            time.sleep(pause)
    return None


def hook_approve(engine, state, payload):
    """The chat door's verdict on one AskUserQuestion result: the PostToolUse
    output to print, or None. Silent for every question that names no token:
    a hook that talks on every question is noise."""
    if not isinstance(payload, dict) or payload.get("tool_name") != "AskUserQuestion":
        return None
    tin = payload.get("tool_input") if isinstance(payload.get("tool_input"), dict) else {}
    resp = payload.get("tool_response") if isinstance(payload.get("tool_response"), dict) else {}
    in_input = tin.get("answers") if isinstance(tin.get("answers"), dict) else {}
    answers = resp.get("answers") if isinstance(resp.get("answers"), dict) else {}
    answers = answers or in_input
    idle = resp.get("afkTimeoutMs")
    typed = str(resp.get("response") or "").strip()
    asked = [q.get("question", "") for q in (tin.get("questions") or [])
             if isinstance(q, dict)]
    sent = transcript_tool_input(payload, asked) if in_input else None
    notes, seen = [], set()
    for question in list(answers) + [q for q in asked if q not in answers]:
        m = TOKEN_IN_QUESTION.search(str(question))
        if not m:
            continue
        token = m.group(1).lower()
        if token in seen:
            continue
        seen.add(token)
        notes.append(f"{token}: " + _one_answer(engine, state, payload, token,
                                                question, answers, in_input,
                                                sent, idle, typed))
    if not notes:
        return None
    return {"hookSpecificOutput": {
        "hookEventName": "PostToolUse",
        "additionalContext": f"{engine.command}: " + "\n".join(notes)}}


def _one_answer(engine, state, payload, token, question, answers, in_input,
                sent, idle, typed):
    if idle:
        return (f"the dialog closed itself after {round(float(idle) / 1000)} s "
                "with nobody at the keyboard — no one answered. No grant "
                "minted. " + next_door_after_chat(engine, token))
    if in_input and sent is None:
        return ("the answer is in the tool input, and the session transcript "
                "that shows whether you wrote it yourself could not be read — "
                "so it cannot count. No grant minted. "
                + next_door_after_chat(engine, token))
    if in_input and sent.get("answers"):
        return ("your own call carried answers — that is you choosing, not the "
                "developer. No grant minted. Ask again without an answers field.")
    answer = answers.get(question)
    if answer is None or not str(answer).strip():
        if typed:
            return (f"the developer typed a reply instead of choosing: {typed!r}. "
                    "That is not an approval and no grant was minted — answer "
                    "them, then ask again.")
        return ("no answer came back — the question never reached the "
                "developer (an allow rule, dontAsk mode, or a PreToolUse hook "
                "with an empty or '*' matcher can answer it first; "
                f"`{engine.command} doctor` names such a setting). No grant "
                "minted. Do not ask the same way again. "
                + next_door_after_chat(engine, token))
    if squash(answer) != "approve":
        return (f"the developer did NOT approve (answered {str(answer)!r}). No "
                "grant minted — do not apply; ask what should change.")
    try:
        _, meta, body = gate.approval_card(engine, state, token)
    except SystemExit as e:
        return str(e)
    if squash("\n".join(body)) not in squash(question):
        return ("the question did not carry pythia's approval card verbatim, so "
                "the developer approved your words, not the preview. No grant "
                f"minted. Run `{engine.command} approve --card {token}` and ask "
                "again with exactly that text.")
    return "approved in chat — " + gate.grant_approval(
        engine, state, token, meta, "chat", payload.get("session_id"))


# --- door 2: the MCP approver -------------------------------------------------------------

def tool_definition(engine):
    _, tool = mcp_names(engine)
    return {
        "name": tool,
        # Claude Code prompts for a tool marked like this on every call, and
        # no allow rule skips it; the elicitation is still the decision
        "_meta": {"anthropic/requiresUserInteraction": True},
        "description": (f"Ask the developer to approve a {engine.label} change "
                        f"that `{engine.command} apply` previewed and printed a "
                        "token for. Shows pythia's own approval card and records "
                        "the approval only if the developer picks Approve. Then "
                        f"run `{engine.command} apply <file> --confirm <token>` "
                        "yourself."),
        "inputSchema": {"type": "object",
                        "properties": {"token": {"type": "string"}},
                        "required": ["token"]},
    }


def _text(text, is_error=False):
    return {"content": [{"type": "text", "text": text}], "isError": is_error}


def mcp_approve_decision(engine, state, token, elicit, clock=None):
    """Mint on a developer's Approve, delivered through an elicitation.
    Transport-free: `elicit(message, schema) -> (action, content)` does the
    round trip. The card is built from the journal, so there is nothing for
    the agent to paraphrase. Fail-closed: the one path that mints is an
    explicit accept whose decision is exactly Approve."""
    clock = clock or time.monotonic
    token = str(token or "").strip().lower()
    try:
        _, meta, body = gate.approval_card(engine, state, token)
    except SystemExit as e:
        return _text(str(e), is_error=True)
    schema = {"type": "object",
              "properties": {"decision": {"type": "string",
                                          "enum": ["Approve", "Reject"]}},
              "required": ["decision"]}
    started = clock()
    try:
        action, content = elicit(card_message(engine, token, body), schema)
    except Exception as e:                        # noqa: BLE001 — fail closed
        return _text(f"No grant minted for {token}: the approval prompt could "
                     f"not be shown ({e}). " + next_door_after_mcp(engine, token),
                     is_error=True)
    took = clock() - started
    if action != "accept" and took < MCP_HUMAN_FLOOR_S:
        return _text(f"No grant minted for {token}: the prompt came back "
                     f"{action!r} after {round(took * 1000)} ms — before anyone "
                     "could read the card, so the client did not show it. That "
                     "is not the developer's answer. "
                     + next_door_after_mcp(engine, token), is_error=True)
    decision = (content or {}).get("decision")
    if action != "accept" or decision != "Approve":
        return _text(f"No grant minted for {token}: the developer did not approve "
                     f"(answered {decision or action!r}). Do not apply; ask what "
                     "should change.", is_error=True)
    msg = gate.grant_approval(engine, state, token, meta, "mcp")
    return _text(f"Approved {token} — {msg} Run it yourself now; do not hand "
                 "the developer a command to type.")


def _result(mid, result):
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def _error(mid, code, message):
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}


def mcp_handle(engine, msg):
    """Answer one message that is not a tools/call; [] for a notification."""
    method, mid = msg.get("method"), msg.get("id")
    if method == "initialize":
        proto = (msg.get("params") or {}).get("protocolVersion") or MCP_PROTOCOL
        return [_result(mid, {"protocolVersion": proto,
                              "capabilities": {"tools": {}},
                              "serverInfo": {"name": mcp_names(engine)[0],
                                             "version": __version__}})]
    if method == "tools/list":
        return [_result(mid, {"tools": [tool_definition(engine)]})]
    if mid is None:
        return []                                  # a notification
    return [_error(mid, -32601, f"method not found: {method}")]


def mcp_serve(engine, state, reader, writer):
    """Newline-delimited JSON-RPC on stdio. A tools/call runs the
    elicitation round trip inline: send elicitation/create, then read until
    the response with its id. One call in flight at a time."""
    counter = [1000]

    def send(msg):
        writer.write(json.dumps(msg) + "\n")
        writer.flush()

    def read_msg():
        line = reader.readline()
        if not line:
            return None
        try:
            msg = json.loads(line)
        except ValueError:
            return {}                              # a malformed line is skipped
        return msg if isinstance(msg, dict) else {}

    def elicit(message, schema):
        counter[0] += 1
        eid = counter[0]
        send({"jsonrpc": "2.0", "id": eid, "method": MCP_ELICIT_METHOD,
              "params": {"message": message, "requestedSchema": schema}})
        while True:
            m = read_msg()
            if m is None:
                raise RuntimeError("the client closed before answering")
            if m.get("id") == eid and ("result" in m or "error" in m):
                if "error" in m:
                    raise RuntimeError(str(m["error"]))
                r = m["result"] or {}
                return r.get("action"), r.get("content") or {}

    _, tool = mcp_names(engine)
    while True:
        msg = read_msg()
        if msg is None:
            return
        if msg.get("method") == "tools/call":
            params = msg.get("params") or {}
            if params.get("name") != tool:
                send(_error(msg.get("id"), -32601, f"no such tool: {params.get('name')}"))
                continue
            token = (params.get("arguments") or {}).get("token", "")
            send(_result(msg.get("id"),
                         mcp_approve_decision(engine, state, token, elicit)))
            continue
        for out in mcp_handle(engine, msg):
            send(out)


def run_answer_hook(engine, state_for, stdin=None):
    """Entry point for `<command> approve --hook`: never raises, prints at
    most one JSON document."""
    stdin = sys.stdin if stdin is None else stdin
    try:
        payload = json.loads(stdin.read() or "{}")
        out = hook_approve(engine, state_for(payload), payload)
    except (Exception, SystemExit):               # noqa: BLE001 — fail silent
        return ""
    return json.dumps(out) + "\n" if out else ""

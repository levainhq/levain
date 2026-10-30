"""The question band's MCP door: a stdio MCP server with two tools: ``ask``, which files a question exactly as
``levain ask`` does (:func:`levain.consent.band.file_question`), for an agent that reaches tools more readily than its
shell; and ``note``, which files a note (:class:`levain.consent.notes.NoteStore`): something the operator reads and
dismisses, with no answer and nothing delivered back. Stdlib only: JSON-RPC 2.0, one JSON message per line. Run as
``<python> -P -m levain.consent.band_mcp --agent <claude_code|codex>``.

The session is read per call: the ``tools/call`` request's ``params._meta`` (``threadId``, then ``sessionId``) when it
names a well-formed main-session id, else the server's own environment (``CLAUDE_CODE_SESSION_ID`` /
``CODEX_THREAD_ID``). Per call because Codex gives an MCP child no ``CODEX_*`` environment and sends its thread id
only in ``_meta`` (QMCP R-q19 amended, measured on codex-cli 0.162.1 ``codex exec``), and because an environment is
fixed when the server starts while a client's session can change under it. The agent's process is the nearest
``claude`` / ``codex`` ancestor (:func:`levain.consent.band.ask_resume_meta`). With no session from either, the question
names :data:`levain.consent.band.NO_SESSION` and the tool's text says so. An answer authorises nothing."""
from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any

from levain.consent import band
from levain.consent.questions import _SESSION_MAX, _has_control

__all__ = ["TOOL", "handle", "main", "serve"]

_PROTOCOL = "2025-06-18"
PARSE_ERROR, INVALID_REQUEST, METHOD_NOT_FOUND, INVALID_PARAMS, INTERNAL_ERROR = -32700, -32600, -32601, -32602, -32603
_DESCRIPTION = (
    "Ask the operator a question and keep working: file it with 2-6 options, the one you recommend and what it "
    "blocks. The operator is pinged and this returns at once; the answer arrives in your context at a later tool "
    "call. An answer authorises nothing: a gated call still goes through levain's gate. Do not do what the question "
    "blocks until the answer comes.")
TOOL: dict[str, Any] = {
    "name": "ask",
    "description": _DESCRIPTION,
    "inputSchema": {
        "type": "object",
        "properties": {
            "what": {"type": "string", "description": "The question, one line."},
            "options": {"type": "array", "maxItems": 6,
                        "description": "The choices (2-6; none only with open: true). key: lowercase a-z, 0-9, _ or "
                                       "-, up to 16 characters.",
                        "items": {"type": "object",
                                  "properties": {"key": {"type": "string"}, "label": {"type": "string"}},
                                  "required": ["key", "label"], "additionalProperties": False}},
            "rec": {"type": "string", "description": "The key of the option you recommend (omit with open: true)."},
            "blocks": {"type": "string", "description": "What waits on the answer, one line."},
            "open": {"type": "boolean",
                     "description": "true files an OPEN question: no options and no rec, answered only in the "
                                    "operator's own words. Default false."},
        },
        "required": ["what", "blocks"],
        "additionalProperties": False,
    },
}
# a note (Phill 10-22): told, read, dismissed; no answer comes back and nothing waits on it (levain.consent.notes)
NOTE_TOOL: dict[str, Any] = {
    "name": "note",
    "description": (
        "Tell the operator something that needs no answer: a reply, a result, a heads-up. File what (one line) and, "
        "if useful, detail. The operator reads it and dismisses it; nothing comes back to you and nothing waits on "
        "it. A note authorises nothing and is not a question: if you need a decision, use ask."),
    "inputSchema": {
        "type": "object",
        "properties": {
            "what": {"type": "string", "description": "What you are telling the operator, one line (up to 200 "
                                                      "characters)."},
            "detail": {"type": "string", "description": "Optional: more, up to 4000 characters; line breaks are "
                                                        "kept."},
        },
        "required": ["what"],
        "additionalProperties": False,
    },
}


class _RpcError(Exception):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code, self.message = code, message


def _arguments(raw: Any) -> tuple[str, list[tuple[str, str]], str, str, bool]:
    """The ``ask`` arguments, shape-checked (a wrong shape is the caller's protocol error; whether the options are
    acceptable is the store's call, and its refusal comes back as the tool's own error). The last is ``open``: an
    open question carries no options and no rec."""
    if not isinstance(raw, dict):
        raise _RpcError(INVALID_PARAMS, "arguments must be an object")
    is_open = raw.get("open", False)
    if not isinstance(is_open, bool):
        raise _RpcError(INVALID_PARAMS, "open must be true or false")
    need = ("what", "blocks") if is_open else ("what", "options", "rec", "blocks")
    missing = [k for k in need if k not in raw]
    if missing:
        raise _RpcError(INVALID_PARAMS, f"missing arguments: {', '.join(missing)}")
    for k in ("what", "rec", "blocks"):
        if k in raw and not isinstance(raw[k], str):
            raise _RpcError(INVALID_PARAMS, f"{k} must be a string")
    opts = raw.get("options", [])
    if not isinstance(opts, list) or not all(
            isinstance(o, dict) and isinstance(o.get("key"), str) and isinstance(o.get("label"), str) for o in opts):
        raise _RpcError(INVALID_PARAMS, "options must be an array of {key, label} strings")
    # key and label both stripped, as `levain ask --option KEY=LABEL` strips them (band._options)
    return (raw["what"], [(o["key"].strip(), o["label"].strip()) for o in opts], raw.get("rec", ""), raw["blocks"],
            is_open)


_META_SESSION_KEYS = ("threadId", "sessionId")
_META_SOURCE = "the session in this call's _meta"


def _meta_session(params: dict[str, Any]) -> str | None:
    """The main session a ``tools/call`` names in ``params._meta`` (the first of :data:`_META_SESSION_KEYS` that is
    a well-formed main-session id: the store's length and control-character rule, no subagent part, not
    :data:`levain.consent.band.NO_SESSION`), or ``None``: anything else falls through to the environment."""
    meta = params.get("_meta")
    if not isinstance(meta, dict):
        return None
    for key in _META_SESSION_KEYS:
        value = meta.get(key)
        if (isinstance(value, str) and 0 < len(value) <= _SESSION_MAX and not _has_control(value)
                and band._SUBAGENT not in value and value != band.NO_SESSION):
            return value
    return None


def _ask(agent: str, params: dict[str, Any]) -> dict[str, Any]:
    what, options, rec, blocks, is_open = _arguments(params.get("arguments"))
    session, source = _meta_session(params), _META_SOURCE
    if session is None:
        env = os.environ
        session = (env.get("CLAUDE_CODE_SESSION_ID") if agent == "claude_code" else env.get("CODEX_THREAD_ID")) \
            or band.NO_SESSION
        source = "the session in this server's environment"
    from levain.consent.queue import default_queue_dir

    q, text = band.file_question(agent, session, what, options, rec, blocks, default_queue_dir(),
                                 no_session_hint="(neither this call nor this server's environment names a session)",
                                 session_source=source, own_session=source == _META_SOURCE, open_ended=is_open)
    return {"content": [{"type": "text", "text": text}], "isError": q is None}


def _session(agent: str, params: dict[str, Any]) -> str:
    """The session a call names (``_meta``), else the server's environment, else :data:`band.NO_SESSION`."""
    session = _meta_session(params)
    if session is not None:
        return session
    env = os.environ
    return (env.get("CLAUDE_CODE_SESSION_ID") if agent == "claude_code" else env.get("CODEX_THREAD_ID")) \
        or band.NO_SESSION


def _note(agent: str, params: dict[str, Any]) -> dict[str, Any]:
    """File a note. It pings nobody and nothing is ever delivered back: a note's session is provenance only."""
    import sqlite3

    from levain.cockpit.consent import ConsentError
    from levain.consent.notes import NoteStore, NoteStoreError

    raw = params.get("arguments")
    if not isinstance(raw, dict):
        raise _RpcError(INVALID_PARAMS, "arguments must be an object")
    extra = set(raw) - {"what", "detail"}
    if "what" not in raw or extra:
        raise _RpcError(INVALID_PARAMS, "a note takes what (required) and detail (optional) only"
                        + (f": not {', '.join(sorted(extra))}" if extra else ""))
    detail = raw.get("detail")      # optional: absent and an explicit null are the same, no detail
    if not isinstance(raw["what"], str) or not (detail is None or isinstance(detail, str)):
        raise _RpcError(INVALID_PARAMS, "what and detail must be strings")
    try:
        n = NoteStore().file(agent=agent, agent_session_id=_session(agent, params), what=raw["what"],
                             detail=detail, cwd=os.path.abspath(os.getcwd()))
    except (ConsentError, NoteStoreError, OSError) as exc:
        return {"content": [{"type": "text", "text": f"levain note: refused, nothing was filed: {exc}"}],
                "isError": True}
    except sqlite3.Error as exc:
        return {"content": [{"type": "text", "text": f"levain note: the note store could not be written, so nothing "
                                                     f"was filed ({exc}); try again"}], "isError": True}
    return {"content": [{"type": "text", "text": f"levain note: note #{n.n} filed ({n.id}); the operator reads it and "
                                                 "dismisses it. Nothing comes back to you: do not wait for it."}],
            "isError": False}


def handle(agent: str, msg: Any) -> dict[str, Any] | None:
    """The reply to one decoded message, or ``None`` for a notification (a message with no ``id``)."""
    if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0" or not isinstance(msg.get("method"), str):
        return _error(None, INVALID_REQUEST, "not a JSON-RPC 2.0 request")
    if "id" not in msg:
        return None
    rid, method, params = msg["id"], msg["method"], msg.get("params")
    if not isinstance(rid, (str, int)) or isinstance(rid, bool):
        return _error(None, INVALID_REQUEST, "id must be a string or an integer")
    try:
        if method == "ping":
            return _ok(rid, {})
        if params is not None and not isinstance(params, dict):
            raise _RpcError(INVALID_PARAMS, "params must be an object")
        params = params or {}
        if method == "initialize":
            version = params.get("protocolVersion")
            return _ok(rid, {"protocolVersion": version if isinstance(version, str) else _PROTOCOL,
                             "capabilities": {"tools": {}}, "serverInfo": {"name": "levain_band", "version": "1"}})
        if method == "tools/list":
            return _ok(rid, {"tools": [TOOL, NOTE_TOOL]})
        if method == "tools/call":
            if params.get("name") == NOTE_TOOL["name"]:
                return _ok(rid, _note(agent, params))
            if params.get("name") != TOOL["name"]:
                raise _RpcError(INVALID_PARAMS, f"unknown tool {params.get('name')!r}")
            return _ok(rid, _ask(agent, params))
        raise _RpcError(METHOD_NOT_FOUND, f"method not found: {method}")
    except _RpcError as exc:
        return _error(rid, exc.code, exc.message)
    except Exception as exc:  # noqa: BLE001 - a server that dies mid-conversation loses the agent's tool for the session
        return _error(rid, INTERNAL_ERROR, f"{type(exc).__name__}: {exc}")


def _ok(rid: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": rid, "result": result}


def _error(rid: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}}


def serve(agent: str, stdin: Any = None, stdout: Any = None) -> None:
    """Answer one message per line until stdin ends."""
    stdin = stdin if stdin is not None else sys.stdin
    stdout = stdout if stdout is not None else sys.stdout
    for raw in stdin:
        if not raw.strip():
            continue
        try:
            msg = json.loads(raw)
        except (ValueError, RecursionError):  # nesting deep enough to overflow the decoder is a parse error too,
            # never the end of the server (the agent would lose the tool for the session)
            reply: dict[str, Any] | None = _error(None, PARSE_ERROR, "parse error")
        else:
            reply = handle(agent, msg)
        if reply is not None:
            # ASCII-escaped: a raw U+2028 / U+2029 / U+0085 inside a string splits the line for a client that
            # splits on every line break (str.splitlines), and one reply would become two broken ones
            stdout.write(json.dumps(reply, ensure_ascii=True) + "\n")
            stdout.flush()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="levain.consent.band_mcp", description="The question band's MCP server.")
    parser.add_argument("--agent", required=True, choices=("claude_code", "codex"))
    args = parser.parse_args(argv)
    for stream in (sys.stdin, sys.stdout):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    try:
        serve(args.agent)
    except (BrokenPipeError, KeyboardInterrupt):
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""The question band's MCP door: a stdio MCP server with one tool, ``ask``, that files a question exactly as
``levain ask`` does (:func:`levain.consent.band.file_question`), for an agent that reaches tools more readily than its
shell. Stdlib only: JSON-RPC 2.0, one JSON message per line. Run as
``<python> -P -m levain.consent.band_mcp --agent <claude_code|codex>``.

The server is a child of the agent, so the agent's session comes from the server's own environment
(``CLAUDE_CODE_SESSION_ID`` / ``CODEX_THREAD_ID``) and the agent's process is the nearest ``claude`` / ``codex``
ancestor (:func:`levain.consent.band.ask_resume_meta`). With no Codex thread in the environment the question names
:data:`levain.consent.band.NO_SESSION` and the tool's text says so. An answer authorises nothing."""
from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any

from levain.consent import band

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
            "options": {"type": "array", "minItems": 2, "maxItems": 6,
                        "description": "The choices (2-6). key: lowercase a-z, 0-9, _ or -, up to 16 characters.",
                        "items": {"type": "object",
                                  "properties": {"key": {"type": "string"}, "label": {"type": "string"}},
                                  "required": ["key", "label"], "additionalProperties": False}},
            "rec": {"type": "string", "description": "The key of the option you recommend."},
            "blocks": {"type": "string", "description": "What waits on the answer, one line."},
        },
        "required": ["what", "options", "rec", "blocks"],
        "additionalProperties": False,
    },
}


class _RpcError(Exception):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code, self.message = code, message


def _arguments(raw: Any) -> tuple[str, list[tuple[str, str]], str, str]:
    """The ``ask`` arguments, shape-checked (a wrong shape is the caller's protocol error; whether the options are
    acceptable is the store's call, and its refusal comes back as the tool's own error)."""
    if not isinstance(raw, dict):
        raise _RpcError(INVALID_PARAMS, "arguments must be an object")
    missing = [k for k in ("what", "options", "rec", "blocks") if k not in raw]
    if missing:
        raise _RpcError(INVALID_PARAMS, f"missing arguments: {', '.join(missing)}")
    for k in ("what", "rec", "blocks"):
        if not isinstance(raw[k], str):
            raise _RpcError(INVALID_PARAMS, f"{k} must be a string")
    opts = raw["options"]
    if not isinstance(opts, list) or not all(
            isinstance(o, dict) and isinstance(o.get("key"), str) and isinstance(o.get("label"), str) for o in opts):
        raise _RpcError(INVALID_PARAMS, "options must be an array of {key, label} strings")
    # key and label both stripped, as `levain ask --option KEY=LABEL` strips them (band._options)
    return raw["what"], [(o["key"].strip(), o["label"].strip()) for o in opts], raw["rec"], raw["blocks"]


def _ask(agent: str, arguments: Any) -> dict[str, Any]:
    what, options, rec, blocks = _arguments(arguments)
    env = os.environ
    session = (env.get("CLAUDE_CODE_SESSION_ID") if agent == "claude_code" else env.get("CODEX_THREAD_ID")) \
        or band.NO_SESSION
    from levain.consent.queue import default_queue_dir

    q, text = band.file_question(agent, session, what, options, rec, blocks, default_queue_dir(),
                                 no_session_hint="(this server's environment names no session)",
                                 session_source="the session in this server's environment")
    return {"content": [{"type": "text", "text": text}], "isError": q is None}


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
            return _ok(rid, {"tools": [TOOL]})
        if method == "tools/call":
            if params.get("name") != TOOL["name"]:
                raise _RpcError(INVALID_PARAMS, f"unknown tool {params.get('name')!r}")
            return _ok(rid, _ask(agent, params.get("arguments")))
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

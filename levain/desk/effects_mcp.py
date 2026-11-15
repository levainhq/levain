"""``levain_effects``: the night desk's effect-verb MCP server (ND-3a, board levain#11). A stdio MCP server (JSON-RPC
2.0, one message per line, stdlib only), the shape of :mod:`levain.seat_gh_mcp`: it runs outside the seat's sandbox and
is the only path to :mod:`levain.desk.effects`. Two tools and no others, with FIXED schemas:

* ``restart_service``: ``order_id``, ``host`` (enum), ``service`` (enum).
* ``send_ack``: ``order_id``, ``event_id``. There is no recipient and no text field: the recipient is derived inside the
  verb and the text comes from the sealed drafter inside it.

A refusal is a fixed sentence returned as a tool error. Any argument outside its shape or enum, a missing or an extra
one, is refused by :mod:`levain.desk.effects` before the order is even read. ``main`` wires NO executor yet (the night
key is ND-8, the mail credential O8, the drafter ND-4, the budgets ND-3b): every call that passes the order gate then
answers "not wired". Wiring is done by constructing :class:`~levain.desk.effects.Effects` in the process that owns the
credentials, not by anything a seat says."""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from levain.desk import effects as fx

__all__ = ["SERVER_NAME", "TOOLS", "TOOL_NAMES", "handle", "main", "serve"]

SERVER_NAME = "levain_effects"
_PROTOCOL = "2025-06-18"
PARSE_ERROR, INVALID_REQUEST, METHOD_NOT_FOUND, INVALID_PARAMS, INTERNAL_ERROR = -32700, -32600, -32601, -32602, -32603
MAX_FRAME = 1_000_000

_ORDER_ID = {"type": "string", "pattern": "^so-[0-9a-f]{12}$", "description": "The standing order's id."}
RESTART_SERVICE: dict[str, Any] = {
    "name": "restart_service",
    "description": ("Restart one allowed service on the monitored host under a standing order that allows it. Takes "
                    "only the order id, the host and the service, each from its fixed list. Returns the exit code only."),
    "inputSchema": {
        "type": "object",
        "properties": {"order_id": _ORDER_ID,
                       "host": {"type": "string", "enum": list(fx.HOSTS)},
                       "service": {"type": "string", "enum": list(fx.SERVICES)}},
        "required": ["order_id", "host", "service"],
        "additionalProperties": False,
    },
}
SEND_ACK: dict[str, Any] = {
    "name": "send_ack",
    "description": ("Send the short acknowledgement for one authenticated mail event under a standing order that allows "
                    "it. Takes only the order id and the event id: the recipient is the event's verified sender, "
                    "derived in code, and the text is checked before it is sent."),
    "inputSchema": {
        "type": "object",
        "properties": {"order_id": _ORDER_ID,
                       "event_id": {"type": "string", "pattern": "^evt-[0-9a-f]{32}$"}},
        "required": ["order_id", "event_id"],
        "additionalProperties": False,
    },
}
TOOLS = [RESTART_SERVICE, SEND_ACK]
TOOL_NAMES = frozenset(t["name"] for t in TOOLS)
_PARAMS = {"restart_service": ("order_id", "host", "service"), "send_ack": ("order_id", "event_id")}


class _RpcError(Exception):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code, self.message = code, message


def _text(text: str, *, error: bool = False) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}], "isError": error}


def _call(effects: fx.Effects, params: dict[str, Any]) -> dict[str, Any]:
    name = params.get("name")
    args = params.get("arguments")
    if name not in TOOL_NAMES:
        raise _RpcError(INVALID_PARAMS, "unknown tool")
    if not isinstance(args, dict) or set(args) != set(_PARAMS[name]):      # no extra, no missing: refused in words
        return _text(fx.R_ARGS, error=True)
    try:
        verb = getattr(effects, name)
        result = verb(*(args[p] for p in _PARAMS[name]))
    except fx.EffectRefused as exc:
        return _text(str(exc), error=True)
    except Exception:                                   # noqa: BLE001 - a fault never carries its text out
        return _text(fx.R_FAILED, error=True)
    return _text(result.text, error=not result.ok)


def handle(effects: fx.Effects, msg: Any) -> dict[str, Any] | None:
    """The reply to one decoded message, or ``None`` for a notification."""
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
                             "capabilities": {"tools": {}}, "serverInfo": {"name": SERVER_NAME, "version": "1"}})
        if method == "tools/list":
            return _ok(rid, {"tools": TOOLS})
        if method == "tools/call":
            return _ok(rid, _call(effects, params))
        raise _RpcError(METHOD_NOT_FOUND, f"method not found: {method}")
    except _RpcError as exc:
        return _error(rid, exc.code, exc.message)
    except Exception as exc:  # noqa: BLE001 - the type only, no text
        return _error(rid, INTERNAL_ERROR, f"{type(exc).__name__}")


def _ok(rid: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": rid, "result": result}


def _error(rid: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}}


def serve(effects: fx.Effects, stdin: Any = None, stdout: Any = None) -> None:
    stdin = stdin if stdin is not None else sys.stdin
    stdout = stdout if stdout is not None else sys.stdout
    while True:
        raw = stdin.readline(MAX_FRAME + 1)
        if not raw:
            break
        if len(raw) > MAX_FRAME and not raw.endswith("\n"):
            while True:
                more = stdin.readline(MAX_FRAME + 1)
                if not more or more.endswith("\n"):
                    break
            reply: dict[str, Any] | None = _error(None, PARSE_ERROR, "frame too large")
        elif not raw.strip():
            continue
        else:
            try:
                msg = json.loads(raw)
            except (ValueError, RecursionError):
                reply = _error(None, PARSE_ERROR, "parse error")
            else:
                reply = handle(effects, msg)
        if reply is not None:
            stdout.write(json.dumps(reply, ensure_ascii=True) + "\n")
            stdout.flush()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="levain.desk.effects_mcp", description="The night desk's effect-verb server.")
    parser.add_argument("--data-root", required=True, help="the cockpit whose authority log every call replays")
    args = parser.parse_args(argv)
    for stream in (sys.stdin, sys.stdout):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    try:
        serve(fx.Effects(Path(args.data_root), lambda: datetime.now(timezone.utc)))     # no executor wired: fail closed
    except (BrokenPipeError, KeyboardInterrupt):
        pass
    return 0


if __name__ == "__main__":
    from levain.home_guard import mark_live
    mark_live()
    sys.exit(main())

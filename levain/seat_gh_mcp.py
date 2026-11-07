"""``levain_gh``: the seat's one GitHub MCP server (PATHZERO Z2b). A stdio MCP server, JSON-RPC 2.0, one message per
line, stdlib only, run by the seat profile's own ``--mcp-config`` as
``<python> -EP -B -m levain.seat_gh_mcp --home <levain home>``. It runs outside the seat's sandbox and holds the only
path to the read tokens (:mod:`levain.seat_gh`); it has two tools and no others:

* ``gh_read``: GET a path of an allowed repository (``path``, optional ``query``).
* ``git_fetch``: fetch an allowed repository's branches into the Levain-owned bare mirror, then read it from there.

A refusal is the executor's fixed sentence, returned as a tool error. The tool schemas expose no method, host, token
or URL: a seat can name only a path (judged by :func:`levain.seat_gh.path_refusal`) and an allowed repository."""
from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from levain import seat_gh

__all__ = ["SERVER_NAME", "TOOLS", "TOOL_NAMES", "handle", "serve", "main"]

SERVER_NAME = "levain_gh"
_PROTOCOL = "2025-06-18"
PARSE_ERROR, INVALID_REQUEST, METHOD_NOT_FOUND, INVALID_PARAMS, INTERNAL_ERROR = -32700, -32600, -32601, -32602, -32603

GH_READ: dict[str, Any] = {
    "name": "gh_read",
    "description": (
        "Read GitHub (GET only) for levainhq/levain or phillipclapham/flow. path: /repos/<owner>/<repo> optionally "
        "followed by /contents, /pulls, /issues, /actions, /commits or /branches and more (for example "
        "/repos/levainhq/levain/pulls/12). query: optional ref, per_page, page, state, sha, path, branch and the like. "
        "Anything else is refused."),
    "inputSchema": {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "The API path, starting /repos/."},
            "query": {"type": "object", "description": "Optional query parameters (string values).",
                      "additionalProperties": {"type": "string"}},
        },
        "required": ["path"],
        "additionalProperties": False,
    },
}
GIT_FETCH: dict[str, Any] = {
    "name": "git_fetch",
    "description": (
        "Fetch every branch of levainhq/levain or phillipclapham/flow into Levain's local bare mirror. The result "
        "names the mirror's path: then `git fetch <that path> <branch>` from your clone (it needs no network)."),
    "inputSchema": {
        "type": "object",
        "properties": {"repo": {"type": "string", "description": "levainhq/levain or phillipclapham/flow."}},
        "required": ["repo"],
        "additionalProperties": False,
    },
}
TOOLS = [GH_READ, GIT_FETCH]
TOOL_NAMES = frozenset(t["name"] for t in TOOLS)


class _RpcError(Exception):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code, self.message = code, message


def _text(text: str, *, error: bool = False) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}], "isError": error}


def _call(home: str, params: dict[str, Any]) -> dict[str, Any]:
    name = params.get("name")
    args = params.get("arguments") or {}
    if name not in TOOL_NAMES:
        raise _RpcError(INVALID_PARAMS, f"unknown tool {name!r}")
    if not isinstance(args, dict):
        raise _RpcError(INVALID_PARAMS, "arguments must be an object")
    allowed = {"gh_read": {"path", "query"}, "git_fetch": {"repo"}}[name]
    if set(args) - allowed:                        # no method, host, token or url field is ever read
        return _text(seat_gh.R_PATH if name == "gh_read" else seat_gh.R_REPO, error=True)
    try:
        if name == "gh_read":
            status, body = seat_gh.gh_read(home, args.get("path"), args.get("query"))
            return _text(f"HTTP {status}\n{body}", error=not 200 <= status < 300)
        return _text(seat_gh.git_fetch(home, args.get("repo")))
    except seat_gh.GhRefused as exc:
        return _text(str(exc), error=True)


def handle(home: str, msg: Any) -> dict[str, Any] | None:
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
            return _ok(rid, _call(home, params))
        raise _RpcError(METHOD_NOT_FOUND, f"method not found: {method}")
    except _RpcError as exc:
        return _error(rid, exc.code, exc.message)
    except Exception as exc:  # noqa: BLE001 - a dead server loses the seat's tool for the session; the type only, no text
        return _error(rid, INTERNAL_ERROR, f"{type(exc).__name__}")


def _ok(rid: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": rid, "result": result}


def _error(rid: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}}


#: The longest frame read (L3 z2b r1 codex 9): a longer line is drained to its newline and answered as a parse error,
#: never buffered whole (a seat's oversized argument would otherwise grow the server without bound).
MAX_FRAME = 1_000_000


def serve(home: str, stdin: Any = None, stdout: Any = None) -> None:
    stdin = stdin if stdin is not None else sys.stdin
    stdout = stdout if stdout is not None else sys.stdout
    while True:
        raw = stdin.readline(MAX_FRAME + 1)
        if not raw:
            break
        if len(raw) > MAX_FRAME and not raw.endswith("\n"):
            while True:                                  # drain the rest of the oversized frame
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
                reply = handle(home, msg)
        if reply is not None:
            stdout.write(json.dumps(reply, ensure_ascii=True) + "\n")
            stdout.flush()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="levain.seat_gh_mcp", description="The seat's GitHub read MCP server.")
    parser.add_argument("--home", required=True)
    args = parser.parse_args(argv)
    for stream in (sys.stdin, sys.stdout):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    try:
        serve(args.home)
    except (BrokenPipeError, KeyboardInterrupt):
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())

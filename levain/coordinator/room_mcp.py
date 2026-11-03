"""The room's MCP door (DESIGN_ROOM §4.3, §4.6): a stdio MCP server with ONE tool, ``propose_drop``, on the question
band's door pattern (:mod:`levain.consent.band_mcp`: JSON-RPC 2.0, one message per line, its error shape). Stdlib plus
levain. Run as ``<python> -P -m levain.coordinator.room_mcp [--launch <launch id>]``.

The door carries no authority. It never commits, never writes a drop, and takes no words from the model: a drop's words
are the operator's own quoted messages, named by id and copied from the store by :func:`levain.coordinator.room.propose`
(the tool has no field for words, and a call that carries one is refused). The room's own phrasing is the ``summary``,
which only ever rides beside a drop as a labelled note. The reply says a card is waiting for his tap and nothing more.

The launch is bound PER PROCESS, never per call: Claude starts one stdio door per vendor session and a ``tools/call``
carries no session id, so the launcher names the launch once, in the server's mcp-config entry (``LEVAIN_ROOM_LAUNCH``
in its environment, or ``--launch <id>``). The door reads it at start and refuses to start without a well-formed one. A
call never names its own launch: a ``launch`` or ``session`` field in its arguments or beside them is refused as a spoof,
and ``_meta`` is not read. The launch must be a room launch (role compared as text) whose latest event is ``active``;
:func:`room.propose` itself checks the role and that the launch belongs to the open room, not that the launch is still
live, so the door checks that here.

A refusal comes back as the tool's own error; a protocol mistake (a bad shape, an unknown tool or method) as a JSON-RPC
error; a machine fault as the tool's own error leading with that it was one. A frame over :data:`FRAME_MAX` bytes is
refused and drained, and the server goes on. The store is the default consent directory's, opened without creating one."""
from __future__ import annotations

import json
import sqlite3
import sys
from typing import Any

from levain.consent.band_mcp import (INTERNAL_ERROR, INVALID_PARAMS, INVALID_REQUEST, METHOD_NOT_FOUND, PARSE_ERROR,
                                     _RpcError, _error, _ok)

__all__ = ["TOOL", "FRAME_MAX", "LAUNCH_ENV", "handle", "launch_from", "main", "serve"]

LAUNCH_ENV = "LEVAIN_ROOM_LAUNCH"

_PROTOCOL = "2025-06-18"
# [seat] one JSON-RPC frame's byte cap
FRAME_MAX = 256 * 1024
# [seat] how much of one over-cap frame is read looking for its line end before the stream is given up on
_DRAIN_MAX = 16 * FRAME_MAX
_NAME = "propose_drop"
_ARGS = frozenset({"quote_ids", "summary", "about"})
# keys a model might reach for to hand the door words of its own: refused by name so the refusal says why
_WORDS_KEYS = frozenset({"words", "body", "text", "content", "message", "drop", "quote", "quotes"})
_ID_MAX = 64
# fields that name a launch or session: a call never says who it is, so one riding the call is a spoof attempt
_IDENTITY_KEYS = frozenset({"launch", "launch_id", "launchid", "session", "session_id", "sessionid", "thread_id",
                            "threadid", "harness_session_id"})
_DESCRIPTION = (
    "Propose a drop to the operator as a card in the room. Name the operator's OWN messages in this room, by id "
    "(quote_ids, in order), that are the words to drop, and give a short summary of why (the room's own words, shown "
    "beside the drop as a labelled note, never as the drop's words). about is an optional work-thread id the "
    "conversation was about. You cannot supply the drop's words: they are his quoted messages, copied by id. "
    "Nothing is sent until he taps the card; this authorises nothing.")
TOOL: dict[str, Any] = {
    "name": _NAME,
    "description": _DESCRIPTION,
    "inputSchema": {
        "type": "object",
        "properties": {
            "quote_ids": {"type": "array", "minItems": 1, "items": {"type": "string"},
                          "description": "Ids (cm-...) of the operator's own messages in this room, in order."},
            "summary": {"type": "string", "description": "Why this is being proposed, in your own words."},
            "about": {"type": "string", "description": "Optional: a work thread id (th-...) it concerns."},
        },
        "required": ["quote_ids", "summary"],
        "additionalProperties": False,
    },
}


def _has_control(text: str) -> bool:
    return any(ord(c) < 32 and c not in "\n\t" or ord(c) == 127 for c in text)


def _arguments(raw: Any) -> tuple[list[str], str, str | None]:
    """The call's arguments, shape-checked (a wrong shape is the caller's protocol error; whether the ids name his
    messages is the room's call, and its refusal comes back as the tool's own error)."""
    from levain.coordinator import room

    if not isinstance(raw, dict):
        raise _RpcError(INVALID_PARAMS, "arguments must be an object")
    spoof = sorted(k for k in raw if isinstance(k, str) and k.lower() in _IDENTITY_KEYS)
    if spoof:
        raise _RpcError(INVALID_PARAMS, f"refused: a call never names its own launch or session ({', '.join(spoof)})")
    words = sorted(set(raw) & _WORDS_KEYS)
    if words:
        raise _RpcError(INVALID_PARAMS, f"this door takes no words of its own ({', '.join(words)}): the drop's words "
                                        "are the operator's quoted messages, named by quote_ids")
    extra = sorted(set(raw) - _ARGS)
    if extra:
        raise _RpcError(INVALID_PARAMS, f"propose_drop takes quote_ids, summary and about only: not {', '.join(extra)}")
    missing = [k for k in ("quote_ids", "summary") if k not in raw]
    if missing:
        raise _RpcError(INVALID_PARAMS, f"missing arguments: {', '.join(missing)}")
    ids = raw["quote_ids"]
    if not isinstance(ids, list) or not 1 <= len(ids) <= room.QUOTE_MAX:
        raise _RpcError(INVALID_PARAMS, f"quote_ids must be a list of 1 to {room.QUOTE_MAX} message ids")
    if not all(isinstance(i, str) and 0 < len(i) <= _ID_MAX and not _has_control(i) for i in ids):
        raise _RpcError(INVALID_PARAMS, "every quote id must be a short string")
    summary = raw["summary"]
    if not isinstance(summary, str):
        raise _RpcError(INVALID_PARAMS, "summary must be a string")
    if len(summary) > room.SUMMARY_MAX:
        raise _RpcError(INVALID_PARAMS, f"summary is over {room.SUMMARY_MAX} characters")
    about = raw.get("about")        # optional: absent and an explicit null are the same
    if about is not None and not (isinstance(about, str) and 0 < len(about) <= _ID_MAX and not _has_control(about)):
        raise _RpcError(INVALID_PARAMS, "about must be a thread id string")
    return list(ids), summary, about


def _text(text: str, error: bool) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}], "isError": error}


def _refused(why: str) -> dict[str, Any]:
    return _text(f"levain room: refused, nothing was proposed: {why}", True)


def _propose(launch_id: str, params: dict[str, Any]) -> dict[str, Any]:
    from levain.consent.queue import default_queue_dir
    from levain.coordinator import lease, room
    from levain.coordinator.envelopes import EnvelopeStore
    from levain.coordinator.store import CoordError, StoreAbsent
    from levain.dispatch.jobs import JobStoreError

    ids, summary, about = _arguments(params.get("arguments"))
    try:
        with EnvelopeStore(default_queue_dir())._write(create=False) as (conn, now):
            ln = lease.launch(conn, launch_id)
            if ln is None:
                return _refused("the launch this door was started for does not exist")
            if ln.role != "room":
                return _refused("this door's launch is not a room launch; only the room proposes a drop")
            if ln.state != "active":
                return _refused(f"the room launch is {ln.state}, not active")
            proposal = room.propose(conn, ln.launch_id, ids, summary, about, now=now)
    except (CoordError, StoreAbsent) as exc:       # StoreAbsent is a CoordError, named for what it says
        return _refused(str(exc))
    except (sqlite3.Error, JobStoreError, OSError) as exc:
        return _text(f"levain room: a machine fault, nothing was proposed ({type(exc).__name__}: {exc}); try again",
                     True)
    return _text(f"levain room: proposal {proposal.proposal_id} made. A card is waiting for your tap; nothing is sent "
                 "until you tap.", False)


def launch_from(value: Any) -> str | None:
    """``value`` when it is a well-formed launch id, else None."""
    from levain.coordinator.lease import _LAUNCH_ID

    return value if isinstance(value, str) and _LAUNCH_ID.fullmatch(value) is not None else None


def handle(launch_id: str, msg: Any) -> dict[str, Any] | None:
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
        spoof = sorted(k for k in params if isinstance(k, str) and k.lower() in _IDENTITY_KEYS)
        if spoof:
            raise _RpcError(INVALID_PARAMS, f"refused: a call never names its own launch or session ({', '.join(spoof)})")
        if method == "initialize":
            # the version this door implements, whatever the client asked for: the client decides whether it can speak it
            return _ok(rid, {"protocolVersion": _PROTOCOL,
                             "capabilities": {"tools": {}}, "serverInfo": {"name": "levain_room", "version": "1"}})
        if method == "tools/list":
            return _ok(rid, {"tools": [TOOL]})
        if method == "tools/call":
            if params.get("name") != _NAME:
                raise _RpcError(INVALID_PARAMS, f"unknown tool {str(params.get('name'))[:60]!r}")
            return _ok(rid, _propose(launch_id, params))
        raise _RpcError(METHOD_NOT_FOUND, f"method not found: {method[:60]}")
    except _RpcError as exc:
        return _error(rid, exc.code, exc.message)
    except Exception as exc:  # noqa: BLE001 - a server that dies mid-conversation loses the room's tool for the session
        return _error(rid, INTERNAL_ERROR, f"{type(exc).__name__}: {exc}")


def _frames(stream: Any):
    """Each line of ``stream`` as text, or ``None`` for a line over :data:`FRAME_MAX` bytes (read to its end and
    dropped, never held whole)."""
    reader = getattr(stream, "buffer", stream)
    while True:
        line = reader.readline(FRAME_MAX + 1)
        if not line:
            return
        if len(line) > FRAME_MAX and not line.endswith(b"\n" if isinstance(line, bytes) else "\n"):
            drained = 0
            while drained < _DRAIN_MAX:
                rest = reader.readline(FRAME_MAX)
                if not rest:
                    break
                if rest.endswith(b"\n" if isinstance(rest, bytes) else "\n"):
                    break
                drained += len(rest)
            else:
                yield None      # no line end in _DRAIN_MAX bytes: a wedged or hostile stream, the session ends
                return
            yield None
            continue
        yield line.decode("utf-8", errors="replace") if isinstance(line, bytes) else line


def serve(launch_id: str, stdin: Any = None, stdout: Any = None) -> None:
    """Answer one message per line until stdin ends."""
    stdin = stdin if stdin is not None else sys.stdin
    stdout = stdout if stdout is not None else sys.stdout
    for raw in _frames(stdin):
        if raw is None:
            reply: dict[str, Any] | None = _error(None, INVALID_REQUEST, f"frame over {FRAME_MAX} bytes")
        elif not raw.strip():
            continue
        else:
            try:
                msg = json.loads(raw)
            except (ValueError, RecursionError):     # nesting deep enough to overflow the decoder is a parse error too
                reply = _error(None, PARSE_ERROR, "parse error")
            else:
                reply = handle(launch_id, msg)
        if reply is not None:
            # ASCII-escaped: a raw U+2028 / U+2029 / U+0085 inside a string splits the line for a client that splits on
            # every line break, and one reply would become two broken ones
            stdout.write(json.dumps(reply, ensure_ascii=True) + "\n")
            stdout.flush()


def main(argv: list[str] | None = None) -> int:
    import argparse
    import os

    parser = argparse.ArgumentParser(prog="levain.coordinator.room_mcp", description="The room's MCP door.")
    parser.add_argument("--launch", help=f"the room launch this door serves (else ${LAUNCH_ENV})")
    args = parser.parse_args(argv)
    launch_id = launch_from(args.launch if args.launch is not None else os.environ.get(LAUNCH_ENV))
    if launch_id is None:
        print(f"levain room door: not started: name the room launch with --launch or ${LAUNCH_ENV} (a launch id, ln-...)",
              file=sys.stderr)
        return 2
    for stream in (sys.stdin, sys.stdout):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    try:
        serve(launch_id)
    except (BrokenPipeError, KeyboardInterrupt):
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())

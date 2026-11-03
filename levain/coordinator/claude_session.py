"""levain.coordinator.claude_session — the Claude stream-json :class:`~levain.coordinator.driver.DriverSession` (RM-C), the
room's driver in 1.0, plus the pure helpers the room launcher builds the child with.

The session is one ``claude -p --input-format stream-json --output-format stream-json --verbose`` child seen through a
:class:`Transport` (write one line, poll for lines, close). A test hands it a fake transport that replays synthetic
frames; :class:`PipeTransport` wraps a ``Popen`` the launcher spawned. **This module spawns nothing.** Like every
driver session it holds no authority, store or verdict (see :mod:`levain.coordinator.driver`): it parses the vendor's
lines into events and liveness evidence, writes what the service hands it, and reports what the vendor said.

What the recorded runs (RM0, RM0B) fixed, and this module keeps:

* ``init`` is re-emitted on EVERY turn. Each one is liveness evidence and a ``permissionMode`` report; the first of a
  turn also opens it (``turn_started``); a later one inside an open turn opens nothing. None is a new session.
* A user line sent while a turn runs FOLDS into it and gets no result of its own, and the vendor echoes no user line.
  A line is therefore counted into the turn it joins (:meth:`ClaudeSession.turn_lines`), never as a turn of its own.
* An interrupt (``control_request`` subtype ``interrupt``) gets a ``control_response`` and then a ``result`` carrying
  ``error_during_execution``; the process lives on. That ``result`` is a ``turn_ended`` like any other.
* A tool the child denies shows as an ``is_error`` ``tool_result`` and, on the turn's ``result``, a
  ``permission_denials`` entry. :func:`canary_denied` requires both, because the ``init`` cannot show a deny rule.

Positions are ``"<conn %08d>:<seq %012d>"``: ``seq`` is allocated as the session ingests a line, in stream order, and
``conn`` is fixed per spawn, so a resumed child's positions sort after the old child's. A read never returns more than
one turn boundary and ends on it. No hook's ``output`` and no ``thinking`` block is ever copied into an event: a hook
event carries its name and outcome only.

The helpers (:func:`room_settings_json`, :func:`room_argv`, :func:`open_probe`, :func:`canary_denied`) are pure.
Stdlib only.
"""
from __future__ import annotations

import hashlib
import json
import os
import queue
import re
import threading
import time
import unicodedata
from collections import deque
from collections.abc import Callable, Iterable, Iterator, Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, ClassVar, NamedTuple, Protocol, runtime_checkable

from levain.coordinator.driver import (APPROVAL_SUBTYPE, ApprovalAnswer, Capabilities, CommandResult, Event,
                                       InterruptCommand, LivenessEvidence, NotSent, ReadBatch, SendCommand,
                                       SendResult, SessionMeta, event)

__all__ = [
    "CONN_MAX", "ROOM_DENIED_TOOLS", "ROOM_MCP_SERVER", "ROOM_MCP_TOOL", "ROOM_TOOLS", "SEQ_MAX", "CanaryVerdict",
    "ClaudeSession", "NotLive", "PipeTransport", "Probe", "Transport", "TransportClosed", "canary_denied",
    "default_deny_paths", "open_probe", "room_argv", "room_settings_json",
]

CONN_MAX = 10 ** 8 - 1
SEQ_MAX = 10 ** 12 - 1
_BODY_MAX = 16 * 1024           # chat.EVENT_BODY_MAX (a test holds the two equal); this module imports no chat code
_POS = re.compile(r"(\d{8}):(\d{12})")
_RECENT_FRAMES = 256

ROOM_MCP_SERVER = "levain_room"
ROOM_MCP_TOOL = "mcp__levain_room__propose_drop"
ROOM_TOOLS = ("Read", "Grep", "Glob", "WebSearch")
ROOM_DENIED_TOOLS = ("Bash", "Edit", "Write", "NotebookEdit", "WebFetch")
_ROOM_INIT_TOOLS = frozenset((*ROOM_TOOLS, ROOM_MCP_TOOL))
_INIT_KEEP = ("type", "subtype", "session_id", "cwd", "tools", "mcp_servers", "model", "permissionMode",
              "claude_code_version", "uuid")


class NotLive(Exception):
    """The session can no longer serve the launch (an exhausted position space). The message is the cause."""


class TransportClosed(Exception):
    """Raised by :meth:`Transport.write` only when NO byte of the line was written (the child is gone or the transport
    closed): the session turns it into :class:`~levain.coordinator.driver.NotSent`. Any other exception from a write is
    a write whose bytes may have left."""


@runtime_checkable
class Transport(Protocol):
    """The seam between the session and a child. ``poll`` returns the lines available now (waiting up to ``wait``
    seconds for the first), each as the vendor wrote it; ``[]`` when there are none."""

    def write(self, data: str) -> None: ...
    def poll(self, wait: float) -> list[str]: ...
    def close(self) -> None: ...


class PipeTransport:
    """A :class:`Transport` over a ``Popen`` the caller spawned with ``stdin``/``stdout`` pipes in text mode. A reader
    thread queues stdout lines; :meth:`close` ends stdin, then terminates, then kills the child. The caller owns the
    spawn (argv, cwd, env)."""

    def __init__(self, proc: Any) -> None:
        self._proc = proc
        self._q: queue.Queue[str | None] = queue.Queue()
        self._closed = False
        self.eof = False
        threading.Thread(target=self._read, daemon=True, name="claude-session-reader").start()

    def _read(self) -> None:
        try:
            for line in self._proc.stdout:
                self._q.put(line)
        except (OSError, ValueError):
            pass
        finally:
            self._q.put(None)

    def write(self, data: str) -> None:
        if self._closed or self._proc.poll() is not None:
            raise TransportClosed("the child has exited or the transport is closed")
        self._proc.stdin.write(data)
        self._proc.stdin.flush()

    def poll(self, wait: float) -> list[str]:
        out: list[str] = []
        try:
            first = self._q.get(timeout=wait) if wait > 0 else self._q.get_nowait()
        except queue.Empty:
            return out
        item: str | None = first
        while True:
            if item is None:
                self.eof = True
                break
            out.append(item)
            try:
                item = self._q.get_nowait()
            except queue.Empty:
                break
        return out

    @property
    def returncode(self) -> int | None:
        return self._proc.poll()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._proc.stdin.close()
        except (OSError, ValueError):
            pass
        for stop in (lambda: self._proc.wait(3), self._proc.terminate, lambda: self._proc.wait(3), self._proc.kill):
            if self._proc.poll() is not None:
                return
            try:
                stop()
            except Exception:  # noqa: BLE001 - a child that will not die is the caller's to find
                pass


def _str(value: Any) -> str:
    return value if isinstance(value, str) else json.dumps(value, sort_keys=True, default=str)


def _cap(text: str) -> str:
    raw = text.encode("utf-8")
    return text if len(raw) <= _BODY_MAX else raw[:_BODY_MAX].decode("utf-8", "ignore")


def _control_error(request_id: str, text: str) -> dict[str, Any]:
    """The error literal :func:`~levain.coordinator.driver._control_error` writes (a test holds the two equal)."""
    return {"type": "control_response", "response": {"subtype": "error", "request_id": request_id, "error": text}}


def _control_deny(request_id: str, text: str) -> dict[str, Any]:
    """A ``can_use_tool`` deny: a success control_response whose body is ``behavior: deny``."""
    return {"type": "control_response", "response": {"subtype": "success", "request_id": request_id,
                                                      "response": {"behavior": "deny", "message": text}}}


def _blocks(content: Any) -> list[Mapping[str, Any]]:
    return [b for b in content if isinstance(b, Mapping)] if isinstance(content, list) else []


def _text_of(content: Any) -> str:
    if isinstance(content, str):
        return content
    return "\n".join(b["text"] for b in _blocks(content) if b.get("type") == "text" and isinstance(b.get("text"), str))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class ClaudeSession:
    """A :class:`~levain.coordinator.driver.DriverSession` over one stream-json child (see the module docstring).

    ``conn`` is the spawn's number (fixed per launch); ``session_id`` is what the launcher passed as ``--session-id``
    (or ``--resume``) and is what :meth:`meta` reports until the first ``init`` says otherwise; ``read_wait`` is how long
    a :meth:`read` waits for a first line; ``interrupt_wait`` is how long :meth:`interrupt` waits for the
    ``control_response`` before it answers ``ambiguous``."""

    harness: ClassVar[str] = "claude"

    def __init__(self, transport: Transport, *, conn: int = 1, session_id: str = "", read_wait: float = 0.0,
                 interrupt_wait: float = 5.0, clock: Callable[[], float] = time.monotonic) -> None:
        if not isinstance(conn, int) or isinstance(conn, bool) or not 0 <= conn <= CONN_MAX:
            raise NotLive("position space exhausted: conn is outside its width")
        self._t = transport
        self._conn = conn
        self._session_id = session_id
        self._read_wait = read_wait
        self._interrupt_wait = interrupt_wait
        self._clock = clock
        self._io = threading.RLock()            # one writer or one pumper at a time: stream order is arrival order
        self._shown: list[Event] = []
        self._seq = 0
        self._read_through = 0
        self._init: dict[str, Any] | None = None
        self._init_at = ""
        self._open: str | None = None           # the open turn's id
        self._turns = 0                         # turns opened (an orphan result opens and ends one)
        self._awaiting = 0                      # lines written that no turn has claimed yet
        self._turn_lines: dict[str, int] = {}
        self._unclaimed = 0                     # turns that opened with no line of ours awaiting (an outside line)
        self._sent = 0
        self._calls: dict[str, str] = {}        # tool_use id -> tool name
        self._inbound: dict[str, str] = {}      # request_id -> subtype, until answered or cancelled
        self._tools_of: dict[str, str | None] = {}
        self._replying: dict[str, str] = {}
        self._acks: dict[str, tuple[str, Any]] = {}
        self._denied: list[tuple[str, str, str | None]] = []
        self._recent: deque[dict[str, Any]] = deque(maxlen=_RECENT_FRAMES)
        self._ids = 0
        self.closed = False

    # --- ingest -----------------------------------------------------------------------------------------------------
    def _emit(self, kind: str, vendor_id: str, *, body: str = "", turn_id: str | None = None,
              call_id: str | None = None, **kw: Any) -> Event:
        if self._seq >= SEQ_MAX:
            raise NotLive("position space exhausted: seq is past its width")
        self._seq += 1
        pos = f"{self._conn:08d}:{self._seq:012d}"
        e = event(kind, vendor_id, pos, body=_cap(body), turn_id=turn_id, call_id=call_id, **kw)
        self._shown.append(e)
        return e

    def _pump(self, wait: float) -> None:
        """Take what the transport has and fold it in, in arrival order. Holds the io lock."""
        with self._io:
            for raw in self._t.poll(wait):
                self._ingest(raw)

    def _new_turn(self) -> str:
        self._turns += 1
        tid = f"t{self._conn}-{self._turns}"
        claimed, self._awaiting = self._awaiting, 0
        self._turn_lines[tid] = claimed
        if claimed == 0:
            self._unclaimed += 1
        return tid

    def _ingest(self, raw: str) -> None:
        text = raw.strip()
        if not text:
            return
        digest = "sha256:" + hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()
        try:
            frame = json.loads(text)
        except ValueError:
            frame = None
        if not isinstance(frame, dict):
            self._emit("error", f"unparseable:{digest[7:23]}", turn_id=self._open,
                       body=f"a line that is not a JSON object ({len(text)} chars)", raw_sha256=digest)
            return
        kind, sub = frame.get("type"), frame.get("subtype")
        uid = frame.get("uuid") if isinstance(frame.get("uuid"), str) and frame.get("uuid") else digest[7:23]
        if kind == "system" and sub == "init":
            self._on_init(frame, uid)
        elif kind == "system" and sub in ("hook_started", "hook_response"):
            self._on_hook(frame, uid, sub)
        elif kind == "assistant":
            self._recent.append(frame)
            self._on_assistant(frame, uid)
        elif kind == "user":
            self._recent.append(frame)
            self._on_user(frame, uid)
        elif kind == "result":
            self._recent.append(frame)
            self._on_result(frame, uid)
        elif kind == "control_request":
            self._on_control_request(frame, digest)
        elif kind == "control_cancel_request":
            self._on_cancel(frame)
        elif kind == "control_response":
            resp = frame.get("response")
            if isinstance(resp, Mapping) and isinstance(resp.get("request_id"), str):
                self._acks[resp["request_id"]] = (_str(resp.get("subtype")), resp.get("response", resp.get("error")))
        elif kind == "system" and sub == "permission_denied":
            self._recent.append(frame)
        # every other line (rate_limit_event, hook_progress, status, stream deltas) is no event

    def _on_init(self, frame: Mapping[str, Any], uid: str) -> None:
        self._init = {k: frame[k] for k in _INIT_KEEP if k in frame}
        self._init_at = _now()
        self._recent.append(dict(self._init))
        sid = frame.get("session_id")
        if isinstance(sid, str) and sid:
            self._session_id = sid
        opened = self._open is None
        if opened:
            self._open = self._new_turn()
        mode = frame.get("permissionMode")
        if isinstance(mode, str) and mode:
            self._emit("error", f"mode:{uid}", body=mode, turn_id=self._open)
        if opened:
            self._emit("turn_started", uid, turn_id=self._open)

    def _on_hook(self, frame: Mapping[str, Any], uid: str, sub: str) -> None:
        name = frame.get("hook_name")
        name = name if isinstance(name, str) and name else "hook"
        hid = frame.get("hook_id") if isinstance(frame.get("hook_id"), str) else uid
        outcome = frame.get("outcome") if isinstance(frame.get("outcome"), str) else ""
        self._emit("error", f"{sub}:{hid}", turn_id=self._open, body=f"{name} {outcome}".strip(),
                   hook={"event": sub, "name": name})

    def _on_assistant(self, frame: Mapping[str, Any], uid: str) -> None:
        message = frame.get("message")
        agent = frame.get("parent_tool_use_id") if isinstance(frame.get("parent_tool_use_id"), str) else None
        for i, block in enumerate(_blocks(message.get("content") if isinstance(message, Mapping) else None)):
            if block.get("type") == "text" and isinstance(block.get("text"), str):
                self._emit("agent_text", f"{uid}.{i}", turn_id=self._open, body=block["text"], agent_id=agent)
            elif block.get("type") == "tool_use" and isinstance(block.get("id"), str):
                name = block.get("name") if isinstance(block.get("name"), str) else None
                self._calls[block["id"]] = name or ""
                self._emit("tool_call", block["id"], turn_id=self._open, call_id=block["id"], tool_name=name,
                           tool_input=block.get("input"), agent_id=agent)
            # a thinking block (or any other) is not copied

    def _on_user(self, frame: Mapping[str, Any], uid: str) -> None:
        message = frame.get("message")
        content = message.get("content") if isinstance(message, Mapping) else None
        agent = frame.get("parent_tool_use_id") if isinstance(frame.get("parent_tool_use_id"), str) else None
        decisions: dict[str, dict[str, str]] = {}
        for meta in frame.get("tool_result_meta") or ():
            d = meta.get("permission_decision") if isinstance(meta, Mapping) else None
            if isinstance(d, Mapping) and isinstance(meta.get("id"), str):
                decisions[meta["id"]] = {k: v for k, v in d.items() if isinstance(k, str) and isinstance(v, str)}
        results = [b for b in _blocks(content) if b.get("type") == "tool_result" and isinstance(b.get("tool_use_id"), str)]
        for block in results:
            cid = block["tool_use_id"]
            self._emit("tool_result", cid, turn_id=self._open, call_id=cid, tool_name=self._calls.get(cid) or None,
                       body=_text_of(block.get("content")), agent_id=agent, decision_source=decisions.get(cid))
            self._calls.pop(cid, None)
        text = _text_of(content)
        if not results and text:
            self._emit("user_item", uid, turn_id=self._open, body=text, agent_id=agent)

    def _on_result(self, frame: Mapping[str, Any], uid: str) -> None:
        tid = self._open if self._open is not None else self._new_turn()     # an aborted turn can end with no init
        parts = [_str(frame.get("subtype")), _str(frame.get("terminal_reason"))]
        parts = [p for p in parts if p and p != "null"]
        if frame.get("is_error") is True:
            parts.append("is_error")
        self._emit("turn_ended", uid, turn_id=tid, body=" ".join(parts))
        self._open = None
        self._calls.clear()

    def _on_control_request(self, frame: Mapping[str, Any], digest: str) -> None:
        rid = frame.get("request_id")
        req = frame.get("request")
        subtype = req.get("subtype") if isinstance(req, Mapping) else None
        if not isinstance(rid, str) or not rid or not isinstance(subtype, str):
            self._emit("error", f"unparseable:{digest[7:23]}", turn_id=self._open, raw_sha256=digest,
                       body="a control_request with no request_id or subtype")
            return
        # recorded before anything else sees the request; a replayed id keeps its first subtype (§8.1)
        self._inbound.setdefault(rid, subtype)
        subtype = self._inbound[rid]
        if subtype == APPROVAL_SUBTYPE:
            tool = req.get("tool_name") if isinstance(req.get("tool_name"), str) else None
            self._tools_of[rid] = tool
            reason, blocked = req.get("decision_reason_type"), req.get("blocked_path")
            self._emit("approval_request", rid, turn_id=self._open, request_key=rid, host=True, tool_name=tool,
                       tool_input=req.get("input"), reason_type=reason if isinstance(reason, str) else None,
                       blocked_path=blocked if isinstance(blocked, str) else None, control_subtype=subtype,
                       raw_sha256=digest, call_id=req.get("tool_use_id") if isinstance(req.get("tool_use_id"), str)
                       else None)
        else:
            self._emit("error", f"control:{rid}", turn_id=self._open, body=f"inbound {subtype[:80]}",
                       control_subtype=subtype, raw_sha256=digest)

    def _on_cancel(self, frame: Mapping[str, Any]) -> None:
        rid = frame.get("request_id")
        if isinstance(rid, str) and rid:
            self._inbound.pop(rid, None)
            self._emit("error", f"cancel:{rid}", turn_id=self._open, body="control request cancelled", cancel_of=rid)

    # --- DriverSession ----------------------------------------------------------------------------------------------
    def capabilities(self) -> Capabilities:
        self._pump(0.0)
        version = self._init.get("claude_code_version") if self._init else None
        return Capabilities(steer=False, queue=True, interrupt_by_turn=False, idempotency_key=False,
                            correlation_field=None, catch_up=False, replays_pending_requests=False,
                            vendor_version=version if isinstance(version, str) else "")

    def liveness_evidence(self) -> LivenessEvidence:
        """The latest ``init`` (the fields the probe and meta read; no slash-command, plugin or memory lists), or
        ``raw=None`` before any. Every turn's ``init`` replaces it."""
        self._pump(0.0)
        with self._io:
            return LivenessEvidence("claude init", dict(self._init) if self._init is not None else None, self._init_at)

    def meta(self) -> SessionMeta:
        """From the latest ``init``; before one, only the session id the launcher named, with no mode (no line has
        reported one)."""
        self._pump(0.0)
        with self._io:
            i = self._init
            if i is None:
                return SessionMeta(self._session_id, None, None, None, None, (), ())
            servers = tuple(s.get("name") if isinstance(s, Mapping) else s for s in i.get("mcp_servers") or ())
            return SessionMeta(self._session_id, i.get("permissionMode"), i.get("model"), i.get("claude_code_version"),
                               i.get("cwd"), tuple(str(t) for t in i.get("tools") or ()),
                               tuple(str(s) for s in servers))

    def origin(self) -> str:
        return f"{self._conn:08d}:{0:012d}"

    def position(self) -> str | None:
        with self._io:
            return f"{self._conn:08d}:{self._read_through:012d}" if self._read_through else self.origin()

    def send(self, command: SendCommand) -> SendResult:
        """One stream-json user line. While a turn runs the vendor folds it in: the result names that turn and the
        line is counted into it (:meth:`turn_lines`); otherwise it names the turn the vendor will open for it. No steer
        and no attachment is written (``not_delivered`` / :class:`NotSent`, before any byte)."""
        if command.mode == "steer":
            return SendResult("not_delivered", None, None, "the room's Claude session takes no steer")
        if command.attachments:
            raise NotSent("the room's Claude session takes no attachment")
        line = json.dumps({"type": "user", "message": {"role": "user", "content": command.text}}) + "\n"
        with self._io:
            if self.closed:
                raise NotSent("the session is closed")
            self._pump(0.0)
            folded = self._open is not None
            tid = self._open if folded else f"t{self._conn}-{self._turns + 1}"
            try:
                self._t.write(line)
            except TransportClosed as exc:
                raise NotSent(str(exc)) from exc
            self._sent += 1
            if folded:
                self._turn_lines[tid] += 1
            else:
                self._awaiting += 1
            item = f"u{self._conn}-{self._sent}"
        return SendResult("accepted", tid, item, "folded into the running turn" if folded else "written; the vendor "
                                                                                                "echoes no user line")

    def answer(self, command: ApprovalAnswer) -> CommandResult:
        """The room answers no approval; nothing is written. A request this session holds is ``rejected`` (the way to
        refuse it is :meth:`reject_control`), any other is ``no_such_request``."""
        with self._io:
            if self._inbound.get(command.raw_vendor_key) is None:
                return CommandResult("no_such_request", "the session holds no such request")
        return CommandResult("rejected", "the room answers no approval; reject_control denies it")

    def reject_control(self, request_id: str, text: str) -> CommandResult:
        """Write the one refusal to a recorded inbound request: an error control_response for any subtype but
        ``can_use_tool``, and for that a ``behavior: deny`` (recorded in :meth:`denials`). An id not recorded raises
        ``ValueError("no such inbound request")``. One reply per request: a write that failed after its bytes may have
        left, or one in flight, answers ``ambiguous`` and writes no second line."""
        with self._io:
            if request_id in self._replying:
                return CommandResult("ambiguous", f"a reply to request {request_id[:40]} is being written or may "
                                                  "already have reached the vendor")
            subtype = self._inbound.get(request_id)
            if subtype is None:
                raise ValueError("no such inbound request")
            deny = subtype == APPROVAL_SUBTYPE
            data = json.dumps((_control_deny if deny else _control_error)(request_id, text), sort_keys=True) + "\n"
            self._replying[request_id] = "writing"
            try:
                self._t.write(data)
            except TransportClosed as exc:
                del self._replying[request_id]
                return CommandResult("rejected", f"not written: {exc}")
            except Exception as exc:  # noqa: BLE001 - the line reached the transport; whether it left is unknown
                self._replying[request_id] = "ambiguous"
                return CommandResult("ambiguous", f"the reply may have reached the vendor ({type(exc).__name__})")
            del self._replying[request_id]
            del self._inbound[request_id]
            if deny:
                self._denied.append((request_id, subtype, self._tools_of.pop(request_id, None)))
        return CommandResult("confirmed", "denied" if deny else "rejected with the error literal")

    def denials(self) -> tuple[tuple[str, str, str | None], ...]:
        """``(request_id, subtype, tool_name)`` of every ``can_use_tool`` this session denied."""
        with self._io:
            return tuple(self._denied)

    def interrupt(self, command: InterruptCommand) -> CommandResult:
        """The stream-json ``interrupt`` control request, then wait up to ``interrupt_wait`` for its response. A
        ``turn_id`` that is not the open turn writes nothing (a late interrupt must not abort the next turn). The
        aborted turn's ``result`` arrives through :meth:`read` as a ``turn_ended``."""
        rid = f"int-{command.op_id}"[:128]
        with self._io:
            if self.closed:
                return CommandResult("rejected", "the session is closed")
            self._pump(0.0)
            if command.turn_id is not None and command.turn_id != self._open:
                return CommandResult("rejected", f"turn {command.turn_id} is not the open turn")
            data = json.dumps({"type": "control_request", "request_id": rid,
                               "request": {"subtype": "interrupt"}}, sort_keys=True) + "\n"
            try:
                self._t.write(data)
            except TransportClosed as exc:
                return CommandResult("rejected", f"not written: {exc}")
            except Exception as exc:  # noqa: BLE001 - the bytes may have left
                return CommandResult("ambiguous", f"the interrupt may have reached the vendor ({type(exc).__name__})")
            deadline = self._clock() + self._interrupt_wait
            while rid not in self._acks:
                left = deadline - self._clock()
                if left <= 0:
                    return CommandResult("ambiguous", "no control_response to the interrupt in time")
                self._pump(min(left, 0.05))
            subtype, body = self._acks.pop(rid)
        if subtype == "success":
            return CommandResult("confirmed", "interrupted")
        return CommandResult("rejected", f"the vendor answered {subtype[:40]}: {_str(body)[:120]}")

    def read(self, cursor: str | None) -> ReadBatch:
        """Every event after ``cursor`` (a position; None = from the start, all of it), ending at the first turn
        boundary (``turn_started`` or ``turn_ended``) when ``cursor`` is given (the :class:`ReadBatch` contract)."""
        after = self._seq_of(cursor) if cursor else 0
        self._pump(self._read_wait)
        with self._io:
            got = tuple(e for e in self._shown if self._seq_of(e.pos) > after)
            if cursor is not None:
                end = next((i for i, e in enumerate(got) if e.kind in ("turn_started", "turn_ended")), None)
                got = got if end is None else got[:end + 1]
            through = max([after, *(self._seq_of(e.pos) for e in got)])
            self._read_through = max(self._read_through, through)
            pos = f"{self._conn:08d}:{through:012d}"
            return ReadBatch(got, pos, pos, False)

    def events(self) -> Iterator[Event]:
        return iter(self.read(None).events)

    def pos_lt(self, a: str, b: str) -> bool:
        return self._key(a) < self._key(b)

    @staticmethod
    def _key(pos: str) -> tuple[int, int]:
        m = _POS.fullmatch(pos) if isinstance(pos, str) else None
        if m is None:
            raise ValueError("a position is '<conn %08d>:<seq %012d>'")
        return int(m.group(1)), int(m.group(2))

    def _seq_of(self, pos: str) -> int:
        conn, seq = self._key(pos)
        if conn != self._conn:
            raise ValueError(f"position {pos} is from connection {conn}, not {self._conn}")
        return seq

    def normalize(self, text: str) -> str:
        return " ".join(unicodedata.normalize("NFC", text).split())

    def close(self) -> None:
        with self._io:
            if self.closed:
                return
            self.closed = True
        self._t.close()

    # --- accounting the launcher and tests read ----------------------------------------------------------------------
    def turn_lines(self, turn_id: str) -> int:
        """How many user lines this session wrote into ``turn_id`` (a fold counts in the turn it joined)."""
        with self._io:
            return self._turn_lines.get(turn_id, 0)

    def pending_lines(self) -> int:
        """Lines written whose turn has not ended: the open turn's, plus any no turn has claimed yet."""
        with self._io:
            return self._awaiting + (self._turn_lines.get(self._open, 0) if self._open is not None else 0)

    def unclaimed_turns(self) -> int:
        """Turns that opened with none of this session's lines awaiting (a line it could not count, or one typed
        outside it)."""
        with self._io:
            return self._unclaimed

    def recent_frames(self) -> tuple[dict[str, Any], ...]:
        """The last parsed frames (assistant, user, result, permission_denied, a trimmed init): the source
        :func:`canary_denied` reads. A hook frame is never kept."""
        with self._io:
            return tuple(self._recent)


# ---- containment helpers (pure; RM0B verdicts) ------------------------------------------------------------------------
_GLOB = re.compile(r"[*?\[{]")


def default_deny_paths(home: str | os.PathLike[str]) -> tuple[str, ...]:
    """The credential paths the room's Read/Grep/Glob must not reach (absolute ones under ``home``, plus the
    ``**/`` globs for dotenv files and keychain exports)."""
    h = str(home)
    if not os.path.isabs(h):
        raise ValueError("home must be absolute")
    return (f"{h}/.ssh", f"{h}/.aws", f"{h}/.gnupg", f"{h}/.config/gcloud", f"{h}/Library/Keychains",
            "**/.env*", "**/*.keychain", "**/*.keychain-db", "**/*.p12")


def _rules(path: str) -> list[str]:
    """The Read-family rule bodies for one deny path. An absolute path is spelled ``//abs`` (a single leading slash is
    RELATIVE to the settings source and silently matches nothing); a plain one denies itself and everything under it.
    A relative path is refused unless it is a ``**/`` glob."""
    if os.path.isabs(path):
        body = "/" + path.rstrip("/") if path != "/" else None
        if body is None:
            raise ValueError("denying the filesystem root is not a rule")
        return [body] if _GLOB.search(path) else [body, body + "/**"]
    if path.startswith("**/"):
        return [path]
    raise ValueError(f"deny path {path[:80]!r} is relative: a relative rule matches nothing here; use an absolute "
                     "path or a '**/' glob")


def room_settings_json(workdir: str | os.PathLike[str], deny_paths: Iterable[str | os.PathLike[str]]) -> str:
    """The ``--settings`` JSON for the room child: ``permissions.deny`` with a Read, Grep and Glob rule for every
    deny path, and ``permissions.blockReadsOutsideWorkingDirectories``. ``workdir`` must be absolute and not inside any
    deny path (that would blind the room, not confine it)."""
    wd = str(workdir)
    if not os.path.isabs(wd):
        raise ValueError("workdir must be absolute")
    deny: list[str] = []
    for p in deny_paths:
        p = str(p)
        if os.path.isabs(p) and not _GLOB.search(p):
            root = p.rstrip("/")
            if wd == root or wd.startswith(root + "/"):
                raise ValueError(f"workdir {wd[:120]!r} is inside deny path {p[:120]!r}")
        for body in _rules(p):
            deny.extend(f"{tool}({body})" for tool in ("Read", "Grep", "Glob"))
    seen: set[str] = set()
    deny = [r for r in deny if not (r in seen or seen.add(r))]
    return json.dumps({"permissions": {"deny": deny, "blockReadsOutsideWorkingDirectories": True}}, sort_keys=True)


def room_argv(*, mcp_config: str | os.PathLike[str], settings: str | os.PathLike[str], model: str | None = None,
              session_id: str | None = None, resume: str | None = None, claude_bin: str = "claude") -> list[str]:
    """The §2.4 argv: read-only file tools, web search and the room's one MCP door, nothing else, ``dontAsk`` so a
    call outside the allow list is denied without a prompt. Exactly one of ``session_id`` (``--session-id``) and
    ``resume`` (``--resume``). No ``--dangerously-skip-permissions`` and no ``--bare``: the child runs with his own
    config chain, and the containment is these flags, the ``--settings`` rules and the open probe."""
    if (session_id is None) == (resume is None):
        raise ValueError("exactly one of session_id and resume")
    for name, p in (("mcp_config", mcp_config), ("settings", settings)):
        if not os.path.isabs(str(p)):
            raise ValueError(f"{name} must be an absolute path")
    argv = [claude_bin, "-p", "--input-format", "stream-json", "--output-format", "stream-json", "--verbose"]
    if model:
        argv += ["--model", model]
    argv += ["--tools", ",".join(ROOM_TOOLS),
             "--allowedTools", ",".join((*ROOM_TOOLS, ROOM_MCP_TOOL)),
             "--disallowedTools", ",".join(ROOM_DENIED_TOOLS),
             "--strict-mcp-config", "--mcp-config", str(mcp_config),
             "--settings", str(settings),
             "--permission-mode", "dontAsk"]
    argv += ["--session-id", session_id] if session_id is not None else ["--resume", resume or ""]
    return argv


class Probe(NamedTuple):
    live: bool
    reason: str


def open_probe(init: Mapping[str, Any] | None) -> Probe:
    """Judge one ``init`` (a frame, or :attr:`LivenessEvidence.raw`): live only for EXACTLY the room's tools and the
    room's one MCP door, connected, under ``dontAsk``. Anything else, or no ``init``, is not live with the reason. The
    ``init`` cannot show deny rules: :func:`canary_denied` is that check."""
    if not isinstance(init, Mapping):
        return Probe(False, "no init has been read")
    tools = init.get("tools")
    if not isinstance(tools, list) or not all(isinstance(t, str) for t in tools):
        return Probe(False, "the init names no tool list")
    if len(set(tools)) != len(tools) or set(tools) != _ROOM_INIT_TOOLS:
        extra, missing = sorted(set(tools) - _ROOM_INIT_TOOLS), sorted(_ROOM_INIT_TOOLS - set(tools))
        return Probe(False, f"the tool set is not the room's (extra {extra[:6]}, missing {missing[:6]})")
    servers = init.get("mcp_servers")
    if not isinstance(servers, list) or len(servers) != 1 or not isinstance(servers[0], Mapping):
        return Probe(False, "the init does not name exactly one MCP server")
    if servers[0].get("name") != ROOM_MCP_SERVER:
        return Probe(False, f"the one MCP server is {_str(servers[0].get('name'))[:60]!r}, not {ROOM_MCP_SERVER!r}")
    if servers[0].get("status") != "connected":
        return Probe(False, f"the room's MCP door is {_str(servers[0].get('status'))[:40]!r}, not connected")
    if init.get("permissionMode") != "dontAsk":
        return Probe(False, f"permissionMode is {_str(init.get('permissionMode'))[:40]!r}, not 'dontAsk'")
    return Probe(True, "exactly the room's tools, its one connected MCP door, dontAsk")


class CanaryVerdict(NamedTuple):
    denied: bool
    reason: str


def canary_denied(frames: Iterable[Mapping[str, Any]], decoy_path: str) -> CanaryVerdict:
    """The open-time deny check over raw frames (see :meth:`ClaudeSession.recent_frames`) after the launcher sent a
    Read of ``decoy_path``, a file under a denied rule, and the turn ended: denied only when the Read was attempted,
    its tool_result is ``is_error`` (and carries no accept decision), and the turn's ``result`` lists it in
    ``permission_denials``. A refusal by the model, a returned content or a missing result is not denied."""
    ids: set[str] = set()
    errored: set[str] = set()
    accepted: set[str] = set()
    listed: set[str] = set()
    for f in frames:
        if not isinstance(f, Mapping):
            continue
        t = f.get("type")
        msg = f.get("message") if isinstance(f.get("message"), Mapping) else {}
        if t == "assistant":
            for b in _blocks(msg.get("content")):
                inp = b.get("input")
                if (b.get("type") == "tool_use" and b.get("name") == "Read" and isinstance(inp, Mapping)
                        and inp.get("file_path") == decoy_path and isinstance(b.get("id"), str)):
                    ids.add(b["id"])
        elif t == "user":
            for b in _blocks(msg.get("content")):
                if b.get("type") == "tool_result" and b.get("is_error") is True and isinstance(b.get("tool_use_id"), str):
                    errored.add(b["tool_use_id"])
            for m in f.get("tool_result_meta") or ():
                d = m.get("permission_decision") if isinstance(m, Mapping) else None
                if isinstance(d, Mapping) and d.get("decision") == "accept" and isinstance(m.get("id"), str):
                    accepted.add(m["id"])
        elif t == "result":
            for d in f.get("permission_denials") or ():
                if isinstance(d, Mapping) and d.get("tool_name") == "Read" and isinstance(d.get("tool_use_id"), str):
                    listed.add(d["tool_use_id"])
    if not ids:
        return CanaryVerdict(False, "the canary Read was never attempted")
    if ids & accepted:
        return CanaryVerdict(False, "the canary Read was accepted by a permission decision")
    if not ids & errored:
        return CanaryVerdict(False, "the canary Read has no is_error tool_result")
    if not ids & listed:
        return CanaryVerdict(False, "the turn's result lists no permission_denials entry for the canary Read")
    return CanaryVerdict(True, "the canary Read is an is_error result and a permission_denials entry")

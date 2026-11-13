"""levain.desk.claude_session — the Claude stream-json :class:`~levain.desk.driver.DriverSession` (RM-C), the
room's driver in 1.0, plus the pure helpers the room launcher builds the child with.

The session is one ``claude -p --input-format stream-json --output-format stream-json --verbose`` child seen through a
:class:`Transport` (write one line, poll for lines, close). A test hands it a fake transport that replays synthetic
frames; :class:`PipeTransport` wraps a ``Popen`` the launcher spawned. **This module spawns nothing.** Like every
driver session it holds no authority, store or verdict (see :mod:`levain.desk.driver`): it parses the vendor's
lines into events and liveness evidence, writes what the service hands it, and reports what the vendor said.

What the recorded runs (RM0, RM0B) fixed, and this module keeps:

* ``init`` is re-emitted on EVERY turn. Each one is liveness evidence and a ``permissionMode`` report; the first of a
  turn also opens it (``turn_started``); a later one inside an open turn opens nothing. None is a new session.
* A user line sent while a turn runs FOLDS into it and gets no result of its own, and the vendor echoes no user line.
  A line is therefore counted into the turn it joins (:meth:`ClaudeSession.turn_lines`), never as a turn of its own.
* An interrupt (``control_request`` subtype ``interrupt``) gets a ``control_response`` and then a ``result`` carrying
  ``error_during_execution``; the process lives on. That ``result`` is a ``turn_ended`` like any other.
* A tool the child denies shows as an ``is_error`` ``tool_result`` and, on the turn's ``result``, a
  ``permission_denials`` entry. :func:`canary_denied` requires both, under one ``tool_use`` id in the last ended turn,
  because the ``init`` cannot show a deny rule.
* A ``can_use_tool`` is answered only by :meth:`ClaudeSession.answer` (the desk's reply, verbatim); every other
  inbound control request only by :meth:`ClaudeSession.reject_control`. A replayed request id is one event for the
  life of the session.

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
import secrets
import shlex
import signal
import subprocess
import threading
import time
import unicodedata
from collections import deque
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, ClassVar, NamedTuple, Protocol, runtime_checkable

from levain import pathid
from levain.desk.room_readguard import HOOK_MATCHER, ROOM_READ_REFUSAL
from levain.desk.driver import (APPROVAL_SUBTYPE, ApprovalAnswer, Capabilities, CommandResult, Event,
                                       InterruptCommand, LivenessEvidence, NotSent, ReadBatch, SendCommand,
                                       SendResult, SessionMeta, event)

__all__ = [
    "CLOSE_BOUND", "CONN_MAX", "ROOM_DENIED_TOOLS", "ROOM_MCP_SERVER", "ROOM_MCP_TOOL", "ROOM_TOOLS", "SEQ_MAX", "CanaryVerdict",
    "ClaudeSession", "NotLive", "PipeTransport", "Probe", "Transport", "TransportClosed", "canary_denied",
    "default_deny_paths", "open_probe", "room_argv", "room_door_tools", "ROOM_DOOR_NAMES", "room_hook_command", "room_settings_json",
    "ROOM_READ_REFUSAL",
]

CONN_MAX = 10 ** 8 - 1
SEQ_MAX = 10 ** 12 - 1
_BODY_MAX = 16 * 1024           # chat.EVENT_BODY_MAX (a test holds the two equal); this module imports no chat code
_POS = re.compile(r"(\d{8}):(\d{12})")
_RECENT_FRAMES = 256
_POLL_SLICE = 0.02              # the longest a waiting pump sleeps between non-blocking polls

ROOM_MCP_SERVER = "levain_room"
ROOM_MCP_TOOL = "mcp__levain_room__propose_drop"
# WebSearch is an EGRESS channel, kept by design (roomfix1 C4, routed to the operator): the room can send search queries
# built from what it has read (a file under its cwd, a search result), so those words can reach a search provider. It
# has no fetch (WebFetch is denied), no shell and no write. `levain serve --room`'s help says this in plain words.
ROOM_TOOLS = ("Read", "Grep", "Glob", "WebSearch")
ROOM_DENIED_TOOLS = ("Bash", "Edit", "Write", "NotebookEdit", "WebFetch")
# D2: the door's tool names a host may configure; the full name is what the vendor's init and --allowedTools carry
ROOM_DOOR_NAMES = ("propose_drop", "relay_to_desk", "desk_replies", "note_to_desk")


def room_door_tools(names: Sequence[str] | None = None) -> tuple[str, ...]:
    """The vendor-side names (``mcp__levain_room__<name>``) of the door tools ``names`` (short names, default just
    ``propose_drop``). A name outside :data:`ROOM_DOOR_NAMES` or a repeat raises ValueError: the room's tool set is a
    closed list, never a string a caller composed."""
    names = ("propose_drop",) if names is None else tuple(names)
    if not names or len(set(names)) != len(names) or any(n not in ROOM_DOOR_NAMES for n in names):
        raise ValueError(f"the door's tools are drawn from {ROOM_DOOR_NAMES}, each once, at least one")
    return tuple(f"mcp__{ROOM_MCP_SERVER}__{n}" for n in names)
_INIT_KEEP = ("type", "subtype", "session_id", "cwd", "tools", "mcp_servers", "model", "permissionMode",
              "claude_code_version", "uuid")


class NotLive(Exception):
    """The session can no longer serve the launch (an exhausted position space). The message is the cause."""


class TransportClosed(Exception):
    """Raised by :meth:`Transport.write` only when NO byte of the line was written (the child is gone or the transport
    closed): the session turns it into :class:`~levain.desk.driver.NotSent`. Any other exception from a write is
    a write whose bytes may have left."""


@runtime_checkable
class Transport(Protocol):
    """The seam between the session and a child. ``poll`` returns the lines available now (waiting up to ``wait``
    seconds for the first), each as the vendor wrote it; ``[]`` when there are none."""

    def write(self, data: str) -> None: ...
    def poll(self, wait: float) -> list[str]: ...
    def close(self) -> None: ...


_EOF_GRACE = 1.0        # an idle child's own exit after stdin EOF (node flushing the transcript, ending its MCP server)
_BUSY_GRACE = 0.25      # a write in flight to a child that reads ends in well under this; one still going is hung
_SIGNAL_WAIT = 3.0      # after each signal, for the child to exit
_WRITER_DRAIN = 2.0     # after the child is gone, for a blocked write to fail with EPIPE (two halves, see close)
CLOSE_BOUND = _BUSY_GRACE + _EOF_GRACE + 2 * _SIGNAL_WAIT + _WRITER_DRAIN + 1.0   # one close()'s waits, +1 s of slack
_ATOMIC = 512           # POSIX _POSIX_PIPE_BUF: a blocking pipe write of at most this many bytes is all or nothing


def _leads_own_group(proc: Any) -> bool:
    """Whether ``proc`` (an unreaped child) leads a process group of its own, not this process's. Only then is its
    group signalled; anything else (a stand-in with no pid, a child sharing our group) gets the plain signal."""
    pid = getattr(proc, "pid", None)
    if type(pid) is not int or not hasattr(os, "killpg") or getattr(proc, "returncode", None) is not None:
        return False
    try:
        return os.getpgid(pid) == pid and pid != os.getpgrp()
    except OSError:
        return False


class PipeTransport:
    """A :class:`Transport` over a ``Popen`` the caller spawned with ``stdin``/``stdout`` pipes in text mode. A reader
    thread queues stdout lines. The caller owns the spawn (argv, cwd, env) and SHOULD spawn with
    ``start_new_session=True``: a child that leads its own process group is signalled as a GROUP, so a descendant
    that inherited stdin (an MCP server, a tool) dies with it and cannot hold the pipe open under a blocked send.

    :meth:`close` (bounded by :data:`CLOSE_BOUND`; a concurrent second call waits for the first, up to that bound):

    * No write in flight (or one that ends within ``_BUSY_GRACE``): stdin is closed FIRST, and the child gets
      ``_EOF_GRACE`` seconds to exit on that EOF by itself (a clean exit flushes its transcript). Only a child still
      running after that is signalled. No write can start once close has begun, so closing stdin waits on nothing.
    * A write still in flight (a child that stopped reading): the child (its group) is signalled first, terminate then
      kill, each followed by a ``_SIGNAL_WAIT`` wait; the dead reader end fails the write with EPIPE. stdin is closed
      only after that write has returned. If a write is still blocked after the child is gone, something still holds
      the pipe's read end: the group is killed, and if the write is STILL blocked after ``_WRITER_DRAIN`` (a
      descendant that left the group), stdin is ABANDONED, not closed: closing it would wait behind the write, or
      close a descriptor a write is using. It closes when the blocked writer finally returns and the transport is
      collected.

    stdout is read as BYTES when the text stream exposes ``.buffer`` and each line is decoded UTF-8 with
    ``errors="replace"``, so one bad byte costs that line's fidelity, never the rest of the output (L3 r2). When the
    stream ends, or fails, :attr:`eof` is set and :attr:`error` names a failure; the session reports both once."""

    def __init__(self, proc: Any) -> None:
        self._proc = proc
        self._q: queue.Queue[str | None] = queue.Queue()
        self._closed = False
        self._wlock = threading.Condition()     # guards _closed and _writers; notified as each write ends
        self._writers = 0
        self._close_done = threading.Event()
        self._group = _leads_own_group(proc)
        self.eof = False
        self.error: str | None = None
        threading.Thread(target=self._read, daemon=True, name="claude-session-reader").start()

    def _read(self) -> None:
        stream = self._proc.stdout
        raw = getattr(stream, "buffer", None)
        try:
            if raw is not None:
                for line in raw:
                    self._q.put(line.decode("utf-8", "replace") if isinstance(line, bytes) else line)
            else:
                for line in stream:
                    self._q.put(line.decode("utf-8", "replace") if isinstance(line, bytes) else line)
        except (OSError, ValueError) as exc:
            self.error = f"the child's stdout failed ({type(exc).__name__})"
        finally:
            self._q.put(None)

    def write(self, data: str) -> None:
        """Write one line. :class:`TransportClosed` only when NO byte left: the transport is closed or the child has
        exited, stdin is closed, or the FIRST chunk failed. That chunk is at most ``_ATOMIC`` bytes, and a blocking
        pipe write that small is all or nothing, so its failure proves nothing was written (a larger write that hits
        EPIPE part-way reports no count on macOS). Once it was written, any failure (a ``BrokenPipeError`` from a child
        that died mid-line) propagates as itself: bytes left, so the send is ambiguous, never ``NotSent``.

        stdin's descriptor is written directly (the text wrapper's buffers are never used), encoded with the stream's
        encoding. A stand-in stream with no descriptor is written through ``write``/``flush``, every failure of which
        is :class:`TransportClosed` (a stand-in only)."""
        with self._wlock:
            if self._closed or self._proc.poll() is not None:
                raise TransportClosed("the child has exited or the transport is closed")
            self._writers += 1
        try:
            stdin = self._proc.stdin
            try:
                fd = stdin.fileno()
            except ValueError as exc:
                raise TransportClosed(f"the child's stdin is closed ({exc})"[:200]) from exc
            except (AttributeError, OSError):
                fd = None
            if fd is None:
                try:
                    stdin.write(data)
                    stdin.flush()
                except BrokenPipeError as exc:
                    raise TransportClosed("the child's stdin is gone (broken pipe)") from exc
                except ValueError as exc:
                    raise TransportClosed(f"the child's stdin is closed ({exc})"[:200]) from exc
                return
            raw = memoryview(data.encode(getattr(stdin, "encoding", None) or "utf-8"))
            try:
                sent = os.write(fd, raw[:_ATOMIC])
            except OSError as exc:
                raise TransportClosed(f"nothing written: the child's stdin failed ({type(exc).__name__})") from exc
            while sent < len(raw):
                try:
                    sent += os.write(fd, raw[sent:])
                except OSError as exc:
                    raise type(exc)(exc.errno, f"the child's stdin failed after {sent} of {len(raw)} bytes were "
                                               f"written ({exc.strerror})") from exc
        finally:
            with self._wlock:
                self._writers -= 1
                self._wlock.notify_all()

    def poll(self, wait: float) -> list[str]:
        out: list[str] = []
        if self.eof and self._q.empty():
            return out                          # the end was already read: never wait on a queue nothing feeds
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
        """End the child; the order is the class docstring's. Idempotent; a concurrent second call returns once the
        first has finished (or :data:`CLOSE_BOUND` has passed)."""
        with self._wlock:
            if self._closed:
                first = False
            else:
                first = self._closed = True     # from here no write starts
                self._wlock.wait_for(lambda: self._writers == 0, _BUSY_GRACE)
                busy = self._writers > 0
        if not first:
            self._close_done.wait(CLOSE_BOUND)
            return
        try:
            stdin_open = True
            if not busy:
                self._close_stdin()             # EOF: an idle child's own clean exit
                stdin_open = False
                self._wait(_EOF_GRACE)
            for sig, plain in ((signal.SIGTERM, self._proc.terminate), (signal.SIGKILL, self._proc.kill)):
                if self._proc.poll() is not None:
                    break
                self._signal(sig, plain)
                self._wait(_SIGNAL_WAIT)
            if stdin_open:
                with self._wlock:
                    drained = self._wlock.wait_for(lambda: self._writers == 0, _WRITER_DRAIN / 2)
                if not drained and self._group:
                    # the leader is reaped, but a member still holding the read end keeps the group id in use, so it
                    # names no other process; an empty group is ESRCH (pid reuse in that gap is a known edge)
                    self._killpg(signal.SIGKILL)
                    with self._wlock:
                        drained = self._wlock.wait_for(lambda: self._writers == 0, _WRITER_DRAIN / 2)
                if drained:
                    self._close_stdin()
                # else: abandoned, never closed under a blocked write (class docstring)
        finally:
            self._close_done.set()

    def _close_stdin(self) -> None:
        try:
            self._proc.stdin.close()
        except (OSError, ValueError):
            pass

    def _wait(self, timeout: float) -> None:
        try:
            self._proc.wait(timeout)
        except Exception:  # noqa: BLE001 - TimeoutExpired, or a child that will not die: the next step handles it
            pass

    def _signal(self, sig: int, plain: Callable[[], None]) -> None:
        """Signal the child's group when it leads one (it is unreaped here, so its pid, and the group's id, are its
        own), else the child alone."""
        if self._group and self._killpg(sig):
            return
        try:
            plain()
        except Exception:  # noqa: BLE001 - a child that will not die is the caller's to find
            pass

    def _killpg(self, sig: int) -> bool:
        try:
            os.killpg(self._proc.pid, sig)
        except ProcessLookupError:
            return True                         # nothing left in the group
        except OSError:
            return False
        return True


def _str(value: Any) -> str:
    return value if isinstance(value, str) else json.dumps(value, sort_keys=True, default=str)


def _cap(text: str) -> str:
    """At most ``_BODY_MAX`` UTF-8 bytes, always encodable (a lone surrogate is replaced, never raised)."""
    raw = text.encode("utf-8", "replace")
    return raw[:_BODY_MAX].decode("utf-8", "ignore")


def _cap_input(value: Any) -> Any:
    """A vendor ``tool_input`` kept as given when its JSON is at most ``_BODY_MAX`` UTF-8 bytes; past that, a marker
    holding the full JSON's digest and size and its capped head (the body cap's rule), never the whole value."""
    if value is None:
        return None
    raw = json.dumps(value, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8", "replace")
    if len(raw) <= _BODY_MAX:
        return value
    return {"levain_truncated": True, "sha256": "sha256:" + hashlib.sha256(raw).hexdigest(), "bytes": len(raw),
            "head": raw[:_BODY_MAX].decode("utf-8", "ignore")}


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _malformed(frame: Mapping[str, Any]) -> str | None:
    """Why a parsed frame is not one this module can read (a container of the wrong type, or a string UTF-8 cannot
    hold), or None. Checked before any handler sees the frame."""
    for key in ("tool_result_meta", "permission_denials"):
        if frame.get(key) is not None and not isinstance(frame.get(key), list):
            return f"{key} is not a list"
    message = frame.get("message")
    # only an assistant or user frame carries ``message`` as an object a handler reads; a system frame (a permission_denied
    # one) carries a sentence there, which is not a malformed frame
    if message is not None and frame.get("type") in ("assistant", "user"):
        if not isinstance(message, Mapping):
            return "message is not an object"
        content = message.get("content")
        if content is not None and not isinstance(content, (list, str)):
            return "message.content is not a list or a string"
    if frame.get("type") == "system" and frame.get("subtype") == "init":
        for key in ("tools", "mcp_servers"):
            if frame.get(key) is not None and not isinstance(frame.get(key), list):
                return f"init {key} is not a list"
    try:
        json.dumps(frame, ensure_ascii=False).encode("utf-8")
    except RecursionError:
        return "the frame is nested too deeply"
    except (UnicodeEncodeError, ValueError, TypeError):
        return "a string in the frame is not encodable as UTF-8"
    return None


def _control_error(request_id: str, text: str) -> dict[str, Any]:
    """The error literal :func:`~levain.desk.driver._control_error` writes (a test holds the two equal)."""
    return {"type": "control_response", "response": {"subtype": "error", "request_id": request_id, "error": text}}


def _blocks(content: Any) -> list[Mapping[str, Any]]:
    return [b for b in content if isinstance(b, Mapping)] if isinstance(content, list) else []


def _text_of(content: Any) -> str:
    if isinstance(content, str):
        return content
    return "\n".join(b["text"] for b in _blocks(content) if b.get("type") == "text" and isinstance(b.get("text"), str))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class ClaudeSession:
    """A :class:`~levain.desk.driver.DriverSession` over one stream-json child (see the module docstring).

    ``conn`` is the spawn's number (fixed per launch); ``session_id`` is what the launcher passed as ``--session-id``
    (or ``--resume``) and is what :meth:`meta` reports until the first ``init`` says otherwise; ``read_wait`` is how long
    a :meth:`read` waits for a first line; ``interrupt_wait`` is how long :meth:`interrupt` waits for the
    ``control_response`` before it answers ``ambiguous``."""

    harness: ClassVar[str] = "claude"

    def __init__(self, transport: Transport, *, conn: int = 1, session_id: str = "", read_wait: float = 0.0,
                 interrupt_wait: float = 5.0, clock: Callable[[], float] = time.monotonic,
                 live: Any = None) -> None:
        if not isinstance(conn, int) or isinstance(conn, bool) or not 0 <= conn <= CONN_MAX:
            raise NotLive("position space exhausted: conn is outside its width")
        self._t = transport
        # ``live``: an optional ephemeral sink (``room_live.Sink``: ``delta(turn, text)`` / ``clear()``) for the text
        # deltas a ``--include-partial-messages`` child streams. None: stream_event frames are ignored, as before.
        self._live = live
        self._conn = conn
        self._session_id = session_id
        self._read_wait = read_wait
        self._interrupt_wait = interrupt_wait
        self._clock = clock
        self._close_lock = threading.Lock()     # guards only the closed flag, so close() never waits behind a hung send
        self._close_done = threading.Event()    # set once the first close() has finished; a second one waits on it
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
        # request_id -> (subtype, digest of the request body) of every control_request ever emitted, for the life of
        # the session: an identical replay is suppressed, a changed one is a protocol error (never a second event key)
        self._seen_ctl: dict[str, tuple[str, str]] = {}
        self._altered: dict[str, set[tuple[str, str]]] = {}    # request_id -> the altered replays already reported
        self._cancelled: set[str] = set()
        self._conflicts = 0
        self._tools_of: dict[str, str | None] = {}
        self._replying: dict[str, str] = {}
        self._int_n = 0
        self._int_lock = threading.Lock()       # one interrupt at a time; its wait holds this, never the io lock
        self._int_rid: str | None = None        # the one interrupt awaiting its control_response
        self._int_ack: tuple[str, Any] | None = None
        self._denied: list[tuple[str, str, str | None]] = []
        self._recent: deque[dict[str, Any]] = deque(maxlen=_RECENT_FRAMES)
        self._ids = 0
        self._eof_seen = False
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
        """Take what the transport has and fold it in, in arrival order. The io lock is held only for a non-blocking
        poll and its ingest; the wait between polls (at most ``_POLL_SLICE`` seconds each) is spent OUTSIDE it, so a
        long :meth:`read` cannot hold off :meth:`interrupt` (L3 r2). Ends on the first lines, at ``wait``, or once the
        transport reports its end. A caller already holding the io lock keeps it across the wait, so only a ``0.0``
        pump is ever called that way."""
        deadline = time.monotonic() + max(wait, 0.0)
        while True:
            with self._io:
                got = self._t.poll(0.0)
                for raw in got:
                    self._ingest(raw)
                self._note_eof()
            left = deadline - time.monotonic()
            if got or left <= 0 or getattr(self._t, "eof", False):
                return
            time.sleep(min(left, _POLL_SLICE))

    def _note_eof(self) -> None:
        """Once the transport's output has ended, one ``error`` event says so with the child's exit status (a child
        whose output ends is no longer readable, whether or not it lives). Called holding the io lock."""
        if self._eof_seen or not getattr(self._t, "eof", False):
            return
        self._eof_seen = True
        self._live_clear()
        rc =getattr(self._t, "returncode", None)
        why = getattr(self._t, "error", None) or "the child's output ended"
        self._emit("error", f"transport:{self._conn}", turn_id=self._open,
                   body=f"{why}; returncode {rc if isinstance(rc, int) else 'unknown'}"[:300])

    def _new_turn(self) -> str:
        self._turns += 1
        tid = f"t{self._conn}-{self._turns}"
        claimed, self._awaiting = self._awaiting, 0
        self._turn_lines[tid] = claimed
        if claimed == 0:
            self._unclaimed += 1
        return tid

    def _unparseable(self, digest: str, why: str) -> None:
        """A digest-only error event. Its id carries the position it takes, so two identical bad lines are two keys."""
        self._emit("error", f"unparseable:{digest[7:23]}:{self._seq + 1}", turn_id=self._open, body=why,
                   raw_sha256=digest)

    def _ingest(self, raw: str) -> None:
        text = raw.strip()
        if not text:
            return
        digest = "sha256:" + hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()
        try:
            frame = json.loads(text)
        except (ValueError, RecursionError):
            frame = None
        if not isinstance(frame, dict):
            self._unparseable(digest, f"a line that is not a JSON object ({len(text)} chars)")
            return
        why = _malformed(frame)
        if why is not None:
            self._unparseable(digest, f"a frame this session cannot read: {why}")
            return
        shown, seq = len(self._shown), self._seq
        last = self._recent[-1] if self._recent else None
        try:
            self._dispatch(frame, digest)
        except NotLive:
            raise
        except Exception as exc:  # noqa: BLE001 - one bad frame must not stop ingestion; its partial events go too
            del self._shown[shown:]
            self._seq = seq
            if self._recent and self._recent[-1] is not last:
                self._recent.pop()
            self._unparseable(digest, f"a frame this session cannot read ({type(exc).__name__})")

    def _dispatch(self, frame: dict[str, Any], digest: str) -> None:
        kind, sub = frame.get("type"), frame.get("subtype")
        uid = (frame.get("uuid") if isinstance(frame.get("uuid"), str) and frame.get("uuid")
               else f"{digest[7:23]}:{self._seq + 1}")
        if kind == "system" and sub == "init":
            self._on_init(frame, uid)
        elif kind == "system" and sub in ("hook_started", "hook_response"):
            self._on_hook(frame, uid, sub)
        elif kind == "stream_event":
            self._on_stream_event(frame)
        elif kind == "assistant":
            self._recent.append(frame)
            if frame.get("parent_tool_use_id") is None:
                self._live_clear()          # the whole frame is the record; the streamed copy of it ends here
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
            # only the response to the interrupt being awaited is kept; a late or unknown one is discarded
            resp = frame.get("response")
            if (isinstance(resp, Mapping) and self._int_rid is not None and resp.get("request_id") == self._int_rid
                    and self._int_ack is None):
                self._int_ack = (_str(resp.get("subtype")), resp.get("response", resp.get("error")))
        elif kind == "system" and sub == "permission_denied":
            self._recent.append(frame)
        # every other line (rate_limit_event, hook_progress, status, stream deltas) is no event

    def _live_clear(self) -> None:
        if self._live is not None:
            try:
                self._live.clear()
            except Exception:  # noqa: BLE001 - the live copy is a convenience; it never fails an ingest
                pass

    def _on_stream_event(self, frame: Mapping[str, Any]) -> None:
        """A ``--include-partial-messages`` frame: ``{"type": "stream_event", "event": {<Messages-API stream event>}}``.
        Only the main agent's ``content_block_delta`` / ``text_delta`` text is kept, in the ephemeral live sink; a
        ``message_start`` or a text ``content_block_start`` starts a fresh buffer. No event is emitted and nothing is
        recorded, and every other event or shape (thinking, input_json, ping, a sub-agent's) is ignored."""
        if self._live is None or frame.get("parent_tool_use_id") is not None:
            return
        ev = frame.get("event")
        if not isinstance(ev, Mapping):
            return
        et = ev.get("type")
        try:
            if et == "message_start" or (et == "content_block_start" and isinstance(ev.get("content_block"), Mapping)
                                         and ev["content_block"].get("type") == "text"):
                self._live.clear()
            elif et == "content_block_delta":
                d = ev.get("delta")
                if isinstance(d, Mapping) and d.get("type") == "text_delta" and isinstance(d.get("text"), str):
                    self._live.delta(self._open, d["text"])
        except Exception:  # noqa: BLE001 - the live copy never fails an ingest
            pass

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
                           tool_input=_cap_input(block.get("input")), agent_id=agent)
            # a thinking block (or any other) is not copied

    def _on_user(self, frame: Mapping[str, Any], uid: str) -> None:
        message = frame.get("message")
        content = message.get("content") if isinstance(message, Mapping) else None
        agent = frame.get("parent_tool_use_id") if isinstance(frame.get("parent_tool_use_id"), str) else None
        decisions: dict[str, dict[str, str]] = {}
        for meta in _list(frame.get("tool_result_meta")):
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
        self._live_clear()
        self._open = None
        self._calls.clear()

    def _on_control_request(self, frame: Mapping[str, Any], digest: str) -> None:
        rid = frame.get("request_id")
        req = frame.get("request")
        subtype = req.get("subtype") if isinstance(req, Mapping) else None
        if not isinstance(rid, str) or not rid or not isinstance(subtype, str):
            self._unparseable(digest, "a control_request with no request_id or subtype")
            return
        body_digest = hashlib.sha256(json.dumps(req, sort_keys=True, default=str).encode("utf-8")).hexdigest()
        seen = self._seen_ctl.get(rid)
        if seen is not None:
            # a replayed id: an identical request is suppressed (one event per id for the life of the session); a
            # changed one never moves the original across the one door (§8.1): it stays pending as it was, and the
            # change is a protocol error under its own key, once per distinct alteration (a retransmitted altered copy
            # is suppressed too, L3 r3)
            if seen != (subtype, body_digest) and (subtype, body_digest) not in self._altered.get(rid, ()):
                self._conflicts += 1
                self._emit("error", f"control_changed:{rid}:{self._conn}-{self._conflicts}", turn_id=self._open,
                           body=f"request {rid[:40]} replayed changed: {seen[0][:40]} -> {subtype[:40]}",
                           control_subtype=subtype, raw_sha256=digest)
                self._altered.setdefault(rid, set()).add((subtype, body_digest))   # after the event exists
            return
        # recorded before anything else sees the request
        self._seen_ctl[rid] = (subtype, body_digest)
        self._inbound[rid] = subtype
        if subtype == APPROVAL_SUBTYPE:
            tool = req.get("tool_name") if isinstance(req.get("tool_name"), str) else None
            self._tools_of[rid] = tool
            reason, blocked = req.get("decision_reason_type"), req.get("blocked_path")
            self._emit("approval_request", rid, turn_id=self._open, request_key=rid, host=True, tool_name=tool,
                       tool_input=_cap_input(req.get("input")), reason_type=reason if isinstance(reason, str) else None,
                       blocked_path=blocked if isinstance(blocked, str) else None, control_subtype=subtype,
                       raw_sha256=digest, call_id=req.get("tool_use_id") if isinstance(req.get("tool_use_id"), str)
                       else None)
        else:
            self._emit("error", f"control:{rid}", turn_id=self._open, body=f"inbound {subtype[:80]}",
                       control_subtype=subtype, raw_sha256=digest)

    def _on_cancel(self, frame: Mapping[str, Any]) -> None:
        rid = frame.get("request_id")
        if isinstance(rid, str) and rid and rid not in self._cancelled:
            self._cancelled.add(rid)
            self._inbound.pop(rid, None)
            self._tools_of.pop(rid, None)
            # an ambiguous reply's marker ends with the request (L3 r2 finding 5): a later reply finds no request, and
            # the permanent _seen_ctl keeps a replay from making it answerable again, so nothing is written twice
            self._replying.pop(rid, None)
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
            servers = tuple(s.get("name") if isinstance(s, Mapping) else s for s in _list(i.get("mcp_servers")))
            return SessionMeta(self._session_id, i.get("permissionMode"), i.get("model"), i.get("claude_code_version"),
                               i.get("cwd"), tuple(str(t) for t in _list(i.get("tools"))),
                               tuple(str(s) for s in servers))

    def origin(self) -> str:
        return f"{self._conn:08d}:{0:012d}"

    def position(self) -> str | None:
        with self._io:
            return f"{self._conn:08d}:{self._read_through:012d}" if self._read_through else self.origin()

    def send(self, command: SendCommand) -> SendResult:
        """One stream-json user line. While a turn runs the vendor folds it in: the result names that turn and the
        line is counted into it (:meth:`turn_lines`); otherwise it names the turn the vendor will open for it. No steer
        and no attachment is written (``not_delivered`` / :class:`NotSent`, before any byte). Known edge: ``accepted``
        means the line was flushed into the pipe, so a child that dies before reading it never runs it and no turn
        ever ends with it (it stays in :meth:`pending_lines`)."""
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
        """The one door (the contract of :meth:`~levain.desk.driver.ScriptedSession.answer`): frame the
        service's reply (``command.reply``, verbatim) as the success ``control_response`` to the recorded
        ``can_use_tool`` ``raw_vendor_key`` and write it once. An id this session holds no request for is
        ``no_such_request``; one whose recorded subtype is not an approval raises ``ValueError`` (nothing is written).
        The room's argv runs ``dontAsk``, so an approval is not expected; one that arrives is surfaced as an
        ``approval_request`` and this is how the service's answer (a deny) reaches the vendor. A ``behavior: deny``
        reply is recorded in :meth:`denials`."""
        key = command.raw_vendor_key
        reply = dict(command.reply)
        line = {"type": "control_response", "response": {"subtype": "success", "request_id": key, "response": reply}}

        def check(subtype: str | None) -> CommandResult | None:
            if subtype is None:
                return CommandResult("no_such_request", "the session holds no such request")
            if subtype != APPROVAL_SUBTYPE:
                raise ValueError(f"request {key[:40]} is {subtype[:40]}, not an approval")
            return None

        with self._io:
            tool = self._tools_of.get(key)
            done = self._reply_once(key, line, check, CommandResult("confirmed", "answered"))
            if done.outcome == "confirmed":
                self._tools_of.pop(key, None)
                if reply.get("behavior") == "deny":
                    self._denied.append((key, APPROVAL_SUBTYPE, tool))
            return done

    def reject_control(self, request_id: str, text: str) -> CommandResult:
        """Write the error literal (:func:`_control_error`) to a recorded NON-approval request, once. An id not
        recorded raises ``ValueError("no such inbound request")``; a recorded ``can_use_tool`` raises too: no
        approval, a deny included, leaves through this door (§8.1); :meth:`answer` is its one door."""
        def check(subtype: str | None) -> CommandResult | None:
            if subtype is None:
                raise ValueError("no such inbound request")
            if subtype == APPROVAL_SUBTYPE:
                raise ValueError("a can_use_tool request is answered only through the one door, never rejected here")
            return None

        with self._io:
            return self._reply_once(request_id, _control_error(request_id, text), check,
                                    CommandResult("confirmed", "rejected with the error literal"))

    def _reply_once(self, request_id: str, line: dict[str, Any], check: Callable[[str | None], CommandResult | None],
                    done: CommandResult) -> CommandResult:
        """The one reply to an inbound request (the shape of ScriptedSession's). A request whose reply is being
        written, or whose write failed after its bytes may have left, is ``ambiguous`` and gets no second write.
        Otherwise ``check`` (on the recorded subtype) may refuse or raise; a line JSON cannot hold raises before any
        byte and leaves the request answerable; a write refused before any byte (:class:`TransportClosed`) is
        ``rejected`` and leaves it answerable; any other write failure leaves it ``ambiguous``. The request is
        forgotten only after its write succeeded. Called holding the io lock."""
        if request_id in self._replying:
            return CommandResult("ambiguous", f"a reply to request {request_id[:40]} is being written or may "
                                              "already have reached the vendor")
        refused = check(self._inbound.get(request_id))
        if refused is not None:
            return refused
        data = json.dumps(line, sort_keys=True) + "\n"
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
        return done

    def denials(self) -> tuple[tuple[str, str, str | None], ...]:
        """``(request_id, subtype, tool_name)`` of every ``can_use_tool`` :meth:`answer` wrote a ``behavior: deny``
        reply to."""
        with self._io:
            return tuple(self._denied)

    def interrupt(self, command: InterruptCommand) -> CommandResult:
        """The stream-json ``interrupt`` control request, then wait up to ``interrupt_wait`` for its response. A
        ``turn_id`` that is neither the open turn nor the one :meth:`send` predicted for a line no turn has claimed yet
        writes nothing (a late interrupt must not abort the next turn). The
        aborted turn's ``result`` arrives through :meth:`read` as a ``turn_ended``. Each write mints its own request id
        (never derived from ``op_id``), and only the response to that id confirms it: a late response to an earlier
        interrupt is discarded.

        Interrupts are serialised by their own lock; the io lock is held only to check, write and ingest, never across
        the wait, so a :meth:`read` or :meth:`send` issued meanwhile waits at most one poll slice (L3 r3). Once the
        child's output has ended no response can arrive, so the answer is ``ambiguous`` at once (L3 r3)."""
        with self._int_lock:
            with self._io:
                if self.closed:
                    return CommandResult("rejected", "the session is closed")
                self._pump(0.0)
                # before its init the turn is the one send() predicted (a line awaits, no turn is open): that id is live
                pending = (f"t{self._conn}-{self._turns + 1}" if self._open is None and self._awaiting > 0 else None)
                if command.turn_id is not None and command.turn_id not in (self._open, pending):
                    return CommandResult("rejected", f"turn {command.turn_id} is not the open turn")
                self._int_n += 1
                rid = f"int-{self._conn}-{self._int_n}-{secrets.token_hex(8)}"
                data = json.dumps({"type": "control_request", "request_id": rid,
                                   "request": {"subtype": "interrupt"}}, sort_keys=True) + "\n"
                self._int_rid, self._int_ack = rid, None
                try:
                    self._t.write(data)
                except TransportClosed as exc:
                    self._int_rid = None
                    return CommandResult("rejected", f"not written: {exc}")
                except Exception as exc:  # noqa: BLE001 - the bytes may have left
                    self._int_rid = None
                    return CommandResult("ambiguous",
                                         f"the interrupt may have reached the vendor ({type(exc).__name__})")
            try:
                deadline = self._clock() + self._interrupt_wait
                while True:
                    with self._io:
                        ack, ended, closed = self._int_ack, self._eof_seen, self.closed
                    if ack is not None:
                        break
                    if ended:       # every line the child wrote is already ingested: no response can follow
                        return CommandResult("ambiguous", "the child's output ended with no control_response to the "
                                                          "interrupt")
                    if closed:
                        return CommandResult("ambiguous", "the session closed with no control_response to the "
                                                          "interrupt")
                    left = deadline - self._clock()
                    if left <= 0:
                        return CommandResult("ambiguous", "no control_response to the interrupt in time")
                    self._pump(min(left, _POLL_SLICE))
                subtype, body = ack
            finally:
                with self._io:
                    self._int_rid, self._int_ack = None, None
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
        """Mark the session closed and stop the child. It never takes ``_io``: a send blocked writing to a child that no
        longer reads holds that lock, and the transport's close is what ends that write (:class:`PipeTransport` holds
        the order and its bound). The flag flips under its own short lock. Idempotent: a concurrent second call returns
        only once the first call's transport close has finished, waiting at most :data:`CLOSE_BOUND` seconds."""
        with self._close_lock:
            first = not self.closed
            self.closed = True
        if not first:
            self._close_done.wait(CLOSE_BOUND)
            return
        try:
            self._t.close()
        finally:
            self._live_clear()
            self._close_done.set()

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


# the credential and agent-config paths under the user's home the room's file tools must not reach (roomfix1 C1 added
# the token files of git hosts, package registries and container/cluster tools, and the agents' own config and
# transcripts: ~/.claude.json, ~/.claude, ~/.codex)
_HOME_DENY = (".ssh", ".aws", ".gnupg", ".config/gcloud", "Library/Keychains", ".netrc", ".npmrc", ".pypirc",
              ".git-credentials", ".config/gh", ".docker", ".kube", ".claude.json", ".claude", ".codex")


def default_deny_paths(home: str | os.PathLike[str],
                       levain_home: str | os.PathLike[str] | None = None) -> tuple[str, ...]:
    """The credential paths the room's Read/Grep/Glob must not reach: :data:`_HOME_DENY` under ``home``, levain's own
    home when given (its stores, tokens, keys and consent queue), plus the ``**/`` globs for dotenv files and keychain
    exports."""
    h = str(home)
    if not os.path.isabs(h):
        raise ValueError("home must be absolute")
    own: tuple[str, ...] = ()
    if levain_home is not None:
        lh = str(levain_home)
        if not os.path.isabs(lh):
            raise ValueError("levain_home must be absolute")
        own = (lh,)
    return (*(f"{h}/{p}" for p in _HOME_DENY), *own, "**/.env*", "**/*.keychain", "**/*.keychain-db", "**/*.p12")


def _norm(path: str) -> str:
    """An absolute path, ``..`` and repeated slashes resolved lexically, with exactly ONE leading slash (POSIX keeps a
    leading ``//``; the rule spelling adds its own)."""
    return "/" + os.path.normpath(path).lstrip("/")


def _spellings(path: str) -> list[str]:
    """The absolute spellings one deny path is denied under: its lexical form and, for a literal path, its
    ``realpath`` too when that differs (macOS ``/tmp`` is ``/private/tmp``; a symlink names its target)."""
    out = [_norm(path)]
    if not _GLOB.search(path):
        real = _norm(os.path.realpath(path))
        if real != out[0]:
            out.append(real)
    return out


def _check_rule_text(path: str) -> None:
    """A ``)`` would close the rule early (a malformed rule that denies something else); a control character is never
    a path we mean. Both are refused, never escaped. The message names the spelling refused (a symlink's target can
    be the offending form)."""
    if ")" in path:
        raise ValueError(f"deny path {path[:80]!r} contains ')': the rule would be malformed")
    if any(unicodedata.category(c) == "Cc" for c in path):
        raise ValueError(f"deny path {path[:80]!r} contains a control character")


def _rules(path: str) -> list[str]:
    """The Read-family rule bodies for one deny path. An absolute path is spelled ``//abs`` (a single leading slash is
    RELATIVE to the settings source and silently matches nothing), once per :func:`_spellings` form; a plain one
    denies itself and everything under it. A relative path is refused unless it is a ``**/`` glob. EVERY spelled form
    is checked as rule text, not only the path as given: a symlink's target becomes a rule too (L3 r3)."""
    _check_rule_text(path)
    if os.path.isabs(path):
        out: list[str] = []
        for spelled in _spellings(path):
            _check_rule_text(spelled)
            if spelled == "/":
                raise ValueError("denying the filesystem root is not a rule")
            body = "/" + spelled
            out += [body] if _GLOB.search(spelled) else [body, body + "/**"]
        return out
    if path.startswith("**/"):
        return [path]
    raise ValueError(f"deny path {path[:80]!r} is relative: a relative rule matches nothing here; use an absolute "
                     "path or a '**/' glob")


def _inside(child: str, root: str) -> bool:
    """``child`` is ``root`` or below it. A DENY site (a True refuses the room), so the lexical test is joined by
    :func:`levain.pathid.maybe_inside`: a workdir or deny path spelled in another letter case (this Mac's volume is
    case-insensitive) is the same place, and a stat that cannot answer counts as inside, not as outside."""
    return (child == root or child.startswith(root.rstrip("/") + "/") or pathid.maybe_inside(root, child))


def room_hook_command(python: str, script: str | os.PathLike[str]) -> str:
    """The shell command of the room's read-guard hook: ``python -I <script>`` (isolated mode; the script is
    :mod:`levain.desk.room_readguard` copied as it is, standard library only). Both paths must be absolute."""
    if not os.path.isabs(python) or not os.path.isabs(str(script)):
        raise ValueError("the hook's python and script must be absolute paths")
    return f"{shlex.quote(python)} -I {shlex.quote(str(script))}"


def room_settings_json(workdir: str | os.PathLike[str], deny_paths: Iterable[str | os.PathLike[str]],
                       hook_command: str | None = None) -> str:
    """The ``--settings`` JSON for the room child: ``permissions.deny`` with a Read, Grep and Glob rule for every
    deny path, and ``permissions.blockReadsOutsideWorkingDirectories``. ``workdir`` must be absolute and not inside any
    deny path (that would blind the room, not confine it), compared lexically, after ``realpath`` on both sides, AND by
    identity (:func:`_inside`: a case alias of either path counts).
    ``hook_command`` (:func:`room_hook_command`) adds the room's own ``hooks.PreToolUse`` entry
    (:mod:`levain.desk.room_readguard`): it can only add a refusal, with :data:`ROOM_READ_REFUSAL` as the model's
    text; the vendor's block stays on beside it. None leaves the hook out (the vendor's own sentence is then the
    refusal's text, a cosmetic difference and no hole)."""
    wd = str(workdir)
    if not os.path.isabs(wd):
        raise ValueError("workdir must be absolute")
    wds = {_norm(wd), _norm(os.path.realpath(wd))}
    deny: list[str] = []
    for p in deny_paths:
        p = str(p)
        bodies = _rules(p)                      # refuses a malformed or relative path before anything else
        if os.path.isabs(p) and not _GLOB.search(p):
            for root in _spellings(p):
                if any(_inside(w, root) for w in wds):
                    raise ValueError(f"workdir {wd[:120]!r} is inside deny path {p[:120]!r}")
        for body in bodies:
            deny.extend(f"{tool}({body})" for tool in ("Read", "Grep", "Glob"))
    seen: set[str] = set()
    deny = [r for r in deny if not (r in seen or seen.add(r))]
    doc: dict[str, Any] = {"permissions": {"deny": deny, "blockReadsOutsideWorkingDirectories": True}}
    if hook_command is not None:
        if not isinstance(hook_command, str) or not hook_command.strip():
            raise ValueError("hook_command must be a non-empty string")
        doc["hooks"] = {"PreToolUse": [{"matcher": HOOK_MATCHER, "hooks": [
            {"type": "command", "command": hook_command, "timeout": 10}]}]}
    return json.dumps(doc, sort_keys=True)


def room_argv(*, mcp_config: str | os.PathLike[str], settings: str | os.PathLike[str], model: str | None = None,
              session_id: str | None = None, resume: str | None = None, claude_bin: str = "claude",
              door_tools: Sequence[str] | None = None) -> list[str]:
    """The §2.4 argv: read-only file tools, web search and the room's one MCP door, nothing else, ``dontAsk`` so a
    call outside the allow list is denied without a prompt. Exactly one of ``session_id`` (``--session-id``) and
    ``resume`` (``--resume``). No ``--dangerously-skip-permissions``, no ``--bare`` and no ``--restricted``: the child
    runs with his own USER config chain, and the containment is these flags, the ``--settings`` rules and the open
    probe. ``--setting-sources user`` keeps a project or local ``.claude/settings*.json`` in the room's cwd from
    installing hooks, plugins or rules (L3 r1: a repository-controlled settings file is code the room would run)."""
    if (session_id is None) == (resume is None):
        raise ValueError("exactly one of session_id and resume")
    for name, p in (("mcp_config", mcp_config), ("settings", settings)):
        if not os.path.isabs(str(p)):
            raise ValueError(f"{name} must be an absolute path")
    argv = [claude_bin, "-p", "--input-format", "stream-json", "--output-format", "stream-json", "--verbose",
            "--include-partial-messages"]
    if model:
        argv += ["--model", model]
    argv += ["--tools", ",".join(ROOM_TOOLS),
             "--allowedTools", ",".join((*ROOM_TOOLS, *room_door_tools(door_tools))),
             "--disallowedTools", ",".join(ROOM_DENIED_TOOLS),
             "--strict-mcp-config", "--mcp-config", str(mcp_config),
             "--setting-sources", "user",
             "--settings", str(settings),
             "--permission-mode", "dontAsk"]
    argv += ["--session-id", session_id] if session_id is not None else ["--resume", resume or ""]
    return argv


class Probe(NamedTuple):
    live: bool
    reason: str


def open_probe(init: Mapping[str, Any] | None, door_tools: Sequence[str] | None = None) -> Probe:
    """Judge one ``init`` (a frame, or :attr:`LivenessEvidence.raw`): live only for EXACTLY the room's tools and the
    room's one MCP door, connected, under ``dontAsk``. Anything else, or no ``init``, is not live with the reason. The
    ``init`` cannot show deny rules: :func:`canary_denied` is that check."""
    if not isinstance(init, Mapping):
        return Probe(False, "no init has been read")
    tools = init.get("tools")
    if not isinstance(tools, list) or not all(isinstance(t, str) for t in tools):
        return Probe(False, "the init names no tool list")
    want = frozenset((*ROOM_TOOLS, *room_door_tools(door_tools)))
    if len(set(tools)) != len(tools) or set(tools) != want:
        extra, missing = sorted(set(tools) - want), sorted(want - set(tools))
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
    Read of ``decoy_path``, a file under a denied rule, and the turn ended. Only the LAST ended turn is judged (the
    frames after the previous ``result`` through the last one); an earlier turn's denial proves nothing about this
    attempt. Denied only when that turn attempted the Read, at least one attempt carries BOTH proofs under its own
    ``tool_use`` id (an ``is_error`` tool_result, and a ``permission_denials`` entry on the turn's ``result``), and
    no attempt in the turn was accepted by a permission decision or returned a non-error result. A refusal by the
    model, a returned content, a canary attempted after the last ``result`` (its turn has not ended) or a missing
    result is not denied."""
    seq = [f for f in frames if isinstance(f, Mapping)]
    ends = [i for i, f in enumerate(seq) if f.get("type") == "result"]

    def attempts(window: Iterable[Mapping[str, Any]]) -> list[str]:
        out: list[str] = []
        for f in window:
            msg = f.get("message") if isinstance(f.get("message"), Mapping) else {}
            if f.get("type") != "assistant":
                continue
            for b in _blocks(msg.get("content")):
                inp = b.get("input")
                if (b.get("type") == "tool_use" and b.get("name") == "Read" and isinstance(inp, Mapping)
                        and inp.get("file_path") == decoy_path and isinstance(b.get("id"), str)):
                    out.append(b["id"])
        return out

    if attempts(seq[ends[-1] + 1:] if ends else seq):
        return CanaryVerdict(False, "a canary Read was attempted in a turn that has not ended")
    if not ends:
        return CanaryVerdict(False, "the canary Read was never attempted")
    window = seq[(ends[-2] + 1 if len(ends) > 1 else 0):ends[-1] + 1]
    ids = set(attempts(window))
    if not ids:
        return CanaryVerdict(False, "the canary Read was never attempted")
    errored: set[str] = set()
    returned: set[str] = set()
    accepted: set[str] = set()
    for f in window:
        if f.get("type") != "user":
            continue
        msg = f.get("message") if isinstance(f.get("message"), Mapping) else {}
        for b in _blocks(msg.get("content")):
            if b.get("type") == "tool_result" and isinstance(b.get("tool_use_id"), str):
                (errored if b.get("is_error") is True else returned).add(b["tool_use_id"])
        for m in _list(f.get("tool_result_meta")):
            d = m.get("permission_decision") if isinstance(m, Mapping) else None
            if isinstance(d, Mapping) and d.get("decision") == "accept" and isinstance(m.get("id"), str):
                accepted.add(m["id"])
    listed = {d["tool_use_id"] for d in _list(window[-1].get("permission_denials"))
              if isinstance(d, Mapping) and d.get("tool_name") == "Read" and isinstance(d.get("tool_use_id"), str)}
    if ids & accepted:
        return CanaryVerdict(False, "a canary Read was accepted by a permission decision")
    if not ids & errored:
        return CanaryVerdict(False, "the canary Read has no is_error tool_result")
    if ids & returned:
        return CanaryVerdict(False, "a canary Read returned a non-error result")
    if not ids & errored & listed:
        return CanaryVerdict(False, "the turn's result lists no permission_denials entry for an errored canary Read")
    return CanaryVerdict(True, "the canary Read is an is_error result and a permission_denials entry under one id")

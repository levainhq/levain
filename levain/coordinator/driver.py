"""levain.coordinator.driver — the chat driver contract (CH-D1 rev 6 §10) and :class:`ScriptedSession`.

A :class:`DriverSession` is one vendor session of one worker launch, seen through a transport: it parses the vendor's
lines into :class:`Event` s and :class:`LivenessEvidence`, writes what the service hands it (a send, an approval reply
the SERVICE built, an interrupt) and reports what the vendor said back. **It holds no authority, no store and no
verdict**: it never writes ``dispatch.db``, never reads the turn lease, ``queue.db`` or ``ledger.db``, imports nothing
from :mod:`levain.consent`, and returns no boolean about liveness (:class:`LivenessEvidence` has no verdict field; the
service folds the evidence in :mod:`levain.coordinator.chat`). A session bug can lose or misparse evidence; it cannot
enable a launch the service's own checks did not pass.

Positions (``Event.pos``, ``ReadBatch.complete_through``, :meth:`DriverSession.position`) are strings in the vendor's
own order, compared only with the session's :meth:`~DriverSession.pos_lt`. No Levain ``gseq`` is vendor evidence.

:class:`ScriptedSession` is the reference session the service's state-machine tests drive (§12's first layer): a
scripted vendor with no process behind it, whose every written line is kept in order and, given ``log_path``,
appended and fsynced to a file so a test can count the bytes a killed process wrote. Stdlib only.
"""
from __future__ import annotations

import json
import os
import threading
from collections.abc import Callable, Iterator, Mapping
from pathlib import Path
from typing import Any, ClassVar, Literal, NamedTuple, Protocol, runtime_checkable

__all__ = [
    "ApprovalAnswer", "Capabilities", "CommandResult", "DriverSession", "Event", "InterruptCommand",
    "LivenessEvidence", "NotSent", "ReadBatch", "ScriptedSession", "SendCommand", "SendResult", "SessionMeta",
]


class NotSent(Exception):
    """Raised by :meth:`DriverSession.send` only when the session PROVES no byte of the send reached the vendor (the
    transport refused the write before it began). The service records ``not_delivered``; any other exception is
    ``ambiguous``."""


class Capabilities(NamedTuple):
    steer: bool
    queue: bool
    interrupt_by_turn: bool
    idempotency_key: bool
    correlation_field: str | None     # the vendor field that echoes a send's op_id on its item; None: no echo
    catch_up: bool
    replays_pending_requests: bool
    vendor_version: str


class LivenessEvidence(NamedTuple):
    """Raw vendor data the service computes liveness from (§4.1). NO verdict field, by design."""
    source: str                       # "codex hooks/list" | "claude init" | "claude stream"
    raw: Any                          # the vendor's own data, unparsed
    read_at: str


class SessionMeta(NamedTuple):
    """From the vendor's init (O9); changed only by a vendor line that reports a change."""
    vendor_session_id: str
    permission_mode: str | None
    model: str | None
    vendor_version: str | None
    cwd: str | None
    tools: tuple[str, ...]
    mcp_servers: tuple[str, ...]


class SendCommand(NamedTuple):
    op_id: str                        # the service's, minted in the attempt transaction
    text: str
    attachments: tuple
    mode: str                         # queue | steer
    expected_turn_id: str | None      # a steer's turn; the vendor refuses a steer into any other


class ApprovalAnswer(NamedTuple):
    op_id: str
    vendor_request_key: str           # the service's durable key (§2.5)
    raw_vendor_key: str               # the key the session itself emitted in Event.request_key; it is addressed by this
    reply: Mapping[str, Any]          # the exact vendor reply the SERVICE built; the session only frames it


class InterruptCommand(NamedTuple):
    op_id: str
    turn_id: str | None


class SendResult(NamedTuple):
    outcome: Literal["accepted", "ambiguous", "not_delivered"]
    vendor_turn_id: str | None
    vendor_item_id: str | None
    detail: str


class CommandResult(NamedTuple):
    outcome: Literal["confirmed", "ambiguous", "rejected", "no_such_request"]
    detail: str


class Event(NamedTuple):
    key: str                          # "<kind>:<vendor id>" (the store refuses any other shape)
    kind: str                         # one of levain.coordinator.chat.EVENT_KINDS
    pos: str
    turn_id: str | None
    call_id: str | None
    request_key: str | None
    approval_id: str | None
    host: bool
    tool_name: str | None
    tool_input: Any
    agent_id: str | None
    body: str
    available_decisions: tuple | None
    decision_source: Mapping[str, str] | None     # a tool_result's permission_decision (O5)
    hook: Mapping[str, str] | None                # hook_started / hook_response {event, name}
    cancel_of: str | None                         # a control_cancel_request's key
    # a CH-D1b addition to rev 6 §10: the value the item carried in Capabilities.correlation_field, verbatim;
    # None when the vendor carried none. §7.3 step 1's proof reads only this.
    correlation: str | None = None

    @property
    def vendor_id(self) -> str:
        """The key with its kind prefix taken off: the id a ``SendResult.vendor_item_id`` names for a user item."""
        return self.key[len(self.kind) + 1:] if self.key.startswith(self.kind + ":") else self.key


class ReadBatch(NamedTuple):
    events: tuple[Event, ...]
    next_cursor: str | None
    complete_through: str | None      # the vendor position up to which the session asserts it read everything
    gap: bool


@runtime_checkable
class DriverSession(Protocol):
    harness: ClassVar[str]

    def capabilities(self) -> Capabilities: ...
    def liveness_evidence(self) -> LivenessEvidence: ...
    def meta(self) -> SessionMeta: ...
    def origin(self) -> str: ...
    def position(self) -> str | None: ...
    def send(self, command: SendCommand) -> SendResult: ...
    def answer(self, command: ApprovalAnswer) -> CommandResult: ...
    def interrupt(self, command: InterruptCommand) -> CommandResult: ...
    def read(self, cursor: str | None) -> ReadBatch: ...
    def events(self) -> Iterator[Event]: ...
    def pos_lt(self, a: str, b: str) -> bool: ...
    def normalize(self, text: str) -> str: ...
    def close(self) -> None: ...


def event(kind: str, vendor_id: str, pos: str, *, body: str = "", turn_id: str | None = None,
          call_id: str | None = None, correlation: str | None = None, **kw: Any) -> Event:
    """An :class:`Event` with the contract's defaults for every field a kind does not use."""
    fields: dict[str, Any] = {"request_key": None, "approval_id": None, "host": False, "tool_name": None,
                              "tool_input": None, "agent_id": None, "available_decisions": None,
                              "decision_source": None, "hook": None, "cancel_of": None}
    fields.update(kw)
    return Event(f"{kind}:{vendor_id}", kind, pos, turn_id, call_id, body=body, correlation=correlation, **fields)


# what a scripted send does: "accept" (the vendor acks with a turn and item id and echoes the line as a user item),
# "accept_silent" (acks; no echo), "ambiguous" (the bytes went out, the reply is unknown), "not_sent" (raises NotSent
# before writing), "reject" (a definitive vendor error after the write: not delivered), "raise" (writes, then a
# transport error), "lose" (writes, the vendor never shows it, the reply is ambiguous)
SEND_SCRIPTS = ("accept", "accept_silent", "ambiguous", "not_sent", "reject", "raise", "lose")


class ScriptedSession:
    """A :class:`DriverSession` over a scripted vendor. Positions are ``"%08d"`` ordinals in one sequence shared by
    every event the vendor shows; ``complete_through`` is the last one a read returned.

    ``sends`` is consumed one entry per :meth:`send` (the last entry repeats); each is one of :data:`SEND_SCRIPTS`.
    An echoed user item carries the send's ``op_id`` in ``correlation`` only when ``correlation_field`` is set.
    ``log_path``: every line the session writes is appended there as JSON and fsynced before ``send`` returns, so a
    process killed after the write still shows it."""

    harness: ClassVar[str] = "scripted"

    def __init__(self, *, sends: tuple[str, ...] = ("accept",), steer: bool = True,
                 correlation_field: str | None = None, idempotency_key: bool = False, turn_id: str | None = "t-1",
                 log_path: Path | None = None, normalize: Callable[[str], str] | None = None) -> None:
        for s in sends:
            if s not in SEND_SCRIPTS:
                raise ValueError(f"a scripted send is one of {', '.join(SEND_SCRIPTS)}")
        self._sends = list(sends) or ["accept"]
        self._caps = Capabilities(steer=steer, queue=True, interrupt_by_turn=True, idempotency_key=idempotency_key,
                                  correlation_field=correlation_field, catch_up=True,
                                  replays_pending_requests=False, vendor_version="scripted-1")
        self.turn_id = turn_id
        self.written: list[dict[str, Any]] = []
        self._log = log_path
        self._norm = normalize
        self._shown: list[Event] = []
        self._read_through = 0
        self._n = 0
        self._items = 0
        self._lock = threading.Lock()
        self.closed = False

    # --- the scripted vendor -------------------------------------------------------------------------------------
    def _pos(self) -> str:
        self._n += 1
        return f"{self._n:08d}"

    def show(self, kind: str, vendor_id: str | None = None, *, body: str = "", turn_id: str | None = None,
             correlation: str | None = None, **kw: Any) -> Event:
        """The vendor shows one event (an outside line typed in its app, a turn start, ...); the next read returns it."""
        with self._lock:
            pos = self._pos()
            e = event(kind, vendor_id or f"v{pos}", pos, body=body, turn_id=turn_id, correlation=correlation, **kw)
            self._shown.append(e)
            return e

    def _write(self, command: SendCommand) -> None:
        line = {"op_id": command.op_id, "text": command.text, "mode": command.mode,
                "expected_turn_id": command.expected_turn_id}
        self.written.append(line)
        if self._log is not None:
            fd = os.open(self._log, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
            try:
                os.write(fd, (json.dumps(line, sort_keys=True) + "\n").encode("utf-8"))
                os.fsync(fd)
            finally:
                os.close(fd)

    # --- DriverSession ---------------------------------------------------------------------------------------------
    def capabilities(self) -> Capabilities:
        return self._caps

    def liveness_evidence(self) -> LivenessEvidence:
        return LivenessEvidence("scripted", {"hooks": "scripted"}, "")

    def meta(self) -> SessionMeta:
        return SessionMeta("scripted-session", "default", None, "scripted-1", None, (), ())

    def origin(self) -> str:
        return f"{0:08d}"

    def position(self) -> str | None:
        return f"{self._read_through:08d}" if self._read_through else self.origin()

    def send(self, command: SendCommand) -> SendResult:
        script = self._sends.pop(0) if len(self._sends) > 1 else self._sends[0]
        if script == "not_sent":
            raise NotSent("the scripted transport refused the write before it began")
        if command.mode == "steer" and (not self._caps.steer or command.expected_turn_id != self.turn_id):
            self._write(command)
            return SendResult("not_delivered", None, None, f"no such turn; the live turn is {self.turn_id}")
        self._write(command)
        if script == "raise":
            raise ConnectionError("the scripted transport broke after the write")
        if script in ("ambiguous", "lose"):
            if script == "ambiguous":
                self._echo(command)
            return SendResult("ambiguous", None, None, "no reply from the scripted vendor")
        if script == "reject":
            return SendResult("not_delivered", None, None, "the scripted vendor refused the line")
        item = self._echo(command) if script == "accept" else None
        with self._lock:
            self._items += 1
            item_id = item.vendor_id if item is not None else f"i-silent-{self._items}"
        return SendResult("accepted", self.turn_id, item_id, "accepted by the scripted vendor")

    def _echo(self, command: SendCommand) -> Event:
        corr = command.op_id if self._caps.correlation_field else None
        return self.show("user_item", body=command.text, turn_id=self.turn_id, correlation=corr)

    def answer(self, command: ApprovalAnswer) -> CommandResult:
        return CommandResult("no_such_request", "the scripted vendor holds no request")

    def interrupt(self, command: InterruptCommand) -> CommandResult:
        return CommandResult("confirmed", "interrupted")

    def read(self, cursor: str | None) -> ReadBatch:
        """Every shown event after ``cursor`` (an ordinal position; None = from the start)."""
        after = int(cursor) if cursor else 0
        with self._lock:
            got = tuple(e for e in self._shown if int(e.pos) > after)
            through = max([after, *(int(e.pos) for e in got)])
            self._read_through = max(self._read_through, through)
            return ReadBatch(got, f"{through:08d}", f"{through:08d}", False)

    def events(self) -> Iterator[Event]:
        return iter(self.read(None).events)

    def pos_lt(self, a: str, b: str) -> bool:
        return int(a) < int(b)

    def normalize(self, text: str) -> str:
        return self._norm(text) if self._norm is not None else " ".join(text.split())

    def close(self) -> None:
        self.closed = True

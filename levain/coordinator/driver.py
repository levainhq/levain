"""levain.coordinator.driver — the chat driver contract (CH-D1 rev 6 §10, with rev 7 / rev 8's Event fields and
``reject_control``, rev 15 §8.1 and §10) and :class:`ScriptedSession`.

A :class:`DriverSession` is one vendor session of one worker launch, seen through a transport: it parses the vendor's
lines into :class:`Event` s and :class:`LivenessEvidence`, writes what the service hands it (a send, an approval reply
the SERVICE built, an interrupt, the error literal to an inbound control request that is not an approval) and reports
what the vendor said back. **It holds no authority, no store and no verdict**: it
never writes ``dispatch.db``, never reads the turn lease, ``queue.db`` or ``ledger.db``, imports nothing from
:mod:`levain.consent`, and returns no boolean about liveness (:class:`LivenessEvidence` has no verdict field; the
service folds the evidence in :mod:`levain.coordinator.chat`). A session bug can lose or misparse evidence; it cannot
enable a launch the service's own checks did not pass.

Positions (``Event.pos``, ``ReadBatch.complete_through``, :meth:`DriverSession.position`) are strings in the vendor's
own order, compared only with the session's :meth:`~DriverSession.pos_lt`. No Levain ``gseq`` is vendor evidence.

:class:`ScriptedSession` is the reference session the service's state-machine tests drive (§12's first layer): a
scripted vendor with no process behind it, whose every written line is kept in order and, given ``log_path``,
appended and fsynced to a file so a test can count the bytes a killed process wrote. Stdlib only.
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
from collections.abc import Callable, Iterator, Mapping
from pathlib import Path
from typing import Any, ClassVar, Literal, NamedTuple, Protocol, runtime_checkable

__all__ = [
    "APPROVAL_SUBTYPE", "ApprovalAnswer", "Capabilities", "CommandResult", "DriverSession", "Event", "InterruptCommand",
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
    reason_type: str | None = None                # rev 7 (R7-2): a can_use_tool's decision_reason_type
    blocked_path: str | None = None               # rev 7: a can_use_tool's blocked_path (a safetyCheck ask)
    control_subtype: str | None = None            # rev 7 (R7-7): an inbound control_request's request.subtype
    raw_sha256: str | None = None                 # rev 7 (R7-7): "sha256:<hex>" of the raw line, as read

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
    def reject_control(self, request_id: str, text: str) -> CommandResult: ...
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
                              "decision_source": None, "hook": None, "cancel_of": None, "reason_type": None,
                              "blocked_path": None, "control_subtype": None, "raw_sha256": None}
    fields.update(kw)
    return Event(f"{kind}:{vendor_id}", kind, pos, turn_id, call_id, body=body, correlation=correlation, **fields)


APPROVAL_SUBTYPE = "can_use_tool"       # the one inbound subtype that is an approval (§8.1): answer() only


def _control_error(request_id: str, text: str) -> dict[str, Any]:
    """The ONLY reply :meth:`DriverSession.reject_control` writes (§8.1): an error control_response. It carries no
    key an approval could ride on."""
    return {"type": "control_response", "response": {"subtype": "error", "request_id": request_id, "error": text}}


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
        # request_id -> the request.subtype of every inbound control_request not yet answered or rejected, recorded
        # on arrival before anything else sees the request (§8.1); the scripted vendor's raw key IS its request_id
        self._inbound: dict[str, str] = {}
        # request_id -> "writing" while its one reply is being written, "ambiguous" once a write failed after its
        # bytes may have left; either way no second reply is written for it
        self._replying: dict[str, str] = {}
        self.received: list[str] = []       # every raw inbound line, as the vendor sent it

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

    def inbound(self, request_id: str, subtype: str, *, tool_name: str | None = None, tool_input: Any = None,
                reason_type: str | None = None, blocked_path: str | None = None,
                turn_id: str | None = None) -> Event:
        """The vendor sends one ``control_request``. Its subtype is recorded in ``_inbound`` first; then a
        ``can_use_tool`` is shown as a host ``approval_request`` keyed by its request_id, and any other subtype as an
        ``error`` event keyed ``error:control:<request_id>``. Both carry the subtype and the raw line's digest."""
        request: dict[str, Any] = {"subtype": subtype}
        if subtype == APPROVAL_SUBTYPE:
            request.update(tool_name=tool_name, input=tool_input, decision_reason_type=reason_type,
                           blocked_path=blocked_path)
        line = json.dumps({"type": "control_request", "request_id": request_id, "request": request}, sort_keys=True)
        digest = "sha256:" + hashlib.sha256(line.encode("utf-8")).hexdigest()
        with self._lock:
            # a replayed id keeps its first subtype: re-registering it would move it across the one door (§8.1)
            if self._inbound.get(request_id, subtype) != subtype:
                raise ValueError(f"request {request_id[:40]} is already pending as {self._inbound[request_id][:40]}")
            self._inbound[request_id] = subtype
            self.received.append(line)
        if subtype == APPROVAL_SUBTYPE:
            return self.show("approval_request", request_id, turn_id=turn_id, request_key=request_id, host=True,
                             tool_name=tool_name, tool_input=tool_input, reason_type=reason_type,
                             blocked_path=blocked_path, control_subtype=subtype, raw_sha256=digest)
        return self.show("error", f"control:{request_id}", turn_id=turn_id, body=f"inbound {subtype}",
                         control_subtype=subtype, raw_sha256=digest)

    def _write(self, command: SendCommand) -> None:
        self._write_line({"op_id": command.op_id, "text": command.text, "mode": command.mode,
                          "expected_turn_id": command.expected_turn_id})

    @staticmethod
    def _frame(line: dict[str, Any]) -> str:
        """``line`` as the transport writes it; a line JSON cannot hold raises here, before any byte leaves."""
        return json.dumps(line, sort_keys=True) + "\n"

    def _write_line(self, line: dict[str, Any], text: str | None = None) -> None:
        """Hand ``line`` (framed as ``text``, else framed here) to the transport (``written``), then append it to the
        log. A raise after the framing is a write whose bytes may have left."""
        text = self._frame(line) if text is None else text
        self.written.append(line)
        if self._log is not None:
            fd = os.open(self._log, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
            try:
                os.write(fd, text.encode("utf-8"))
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
        """Frame the service's reply with the transport id and write it once. An id this session holds no request
        for is ``no_such_request``; one whose recorded subtype is not an approval raises (nothing is written). The
        request is forgotten only after its write succeeded (:meth:`_reply_once`)."""
        key = command.raw_vendor_key
        line = {"type": "control_response", "response": {"subtype": "success", "request_id": key,
                                                          "response": dict(command.reply)}}

        def check(subtype: str | None) -> CommandResult | None:
            if subtype is None:
                return CommandResult("no_such_request", "the scripted vendor holds no such request")
            if subtype != APPROVAL_SUBTYPE:
                raise ValueError(f"request {key[:40]} is {subtype[:40]}, not an approval")
            return None

        return self._reply_once(key, line, check, CommandResult("confirmed", "answered"))

    def reject_control(self, request_id: str, text: str) -> CommandResult:
        """Write the error literal (:func:`_control_error`) to a recorded non-approval request and forget it once the
        write succeeded. An id not recorded raises ``ValueError("no such inbound request")``; a recorded
        ``can_use_tool`` raises too: no approval, a deny included, leaves the one door (§8.1)."""
        def check(subtype: str | None) -> CommandResult | None:
            if subtype is None:
                raise ValueError("no such inbound request")
            if subtype == APPROVAL_SUBTYPE:
                raise ValueError("a can_use_tool request is answered only through the one door, never rejected here")
            return None

        return self._reply_once(request_id, _control_error(request_id, text), check,
                                CommandResult("confirmed", "rejected with the error literal"))

    def _reply_once(self, request_id: str, line: dict[str, Any], check: Callable[[str | None], CommandResult | None],
                    done: CommandResult) -> CommandResult:
        """The one reply to an inbound request. ``line`` is already built (a reply that cannot be read raised before
        this). A request whose reply is being written, or whose write failed after its bytes may have left, is
        ``ambiguous`` and gets no second write. Otherwise ``check`` (the recorded subtype) may refuse; the request is
        marked writing, the line written, and only then forgotten. A line that cannot be framed leaves it answerable;
        a write that raised after the framing leaves it ``ambiguous``."""
        with self._lock:
            if request_id in self._replying:
                return CommandResult("ambiguous", f"a reply to request {request_id[:40]} is being written or may "
                                                  "already have reached the vendor")
            refused = check(self._inbound.get(request_id))
            if refused is not None:
                return refused
            self._replying[request_id] = "writing"
        try:
            text = self._frame(line)
        except BaseException:
            with self._lock:
                del self._replying[request_id]
            raise
        try:
            self._write_line(line, text)
        except Exception as exc:  # noqa: BLE001 - the line reached the transport; whether it left is unknown
            with self._lock:
                self._replying[request_id] = "ambiguous"
            return CommandResult("ambiguous", f"the reply may have reached the vendor ({type(exc).__name__})")
        with self._lock:
            del self._replying[request_id]
            del self._inbound[request_id]
        return done

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

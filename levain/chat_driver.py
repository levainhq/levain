"""levain.chat_driver — the HARNESS DRIVER CONTRACT behind the chat host, and the OpenHands driver.

The chat host (:mod:`levain.chat`) used to talk straight to an :class:`levain.session.EntitySession`.
That fixes the harness: the primary entity of a Claude Code or Codex install cannot be driven through
it. A :class:`HarnessDriver` is the seam instead. The host holds one driver per (entity, conversation),
keyed by the entity's name, so N entities (a constellation) is a matter of which driver each name maps
to. The driver owns everything harness-specific: how a session is opened and torn down, how a turn is
sent, how the harness's own consent surfaces, how to stop a turn. The host keeps everything that is
not: the propose, job and poll runtime, the session cap, the deadline watcher, the decision id and the
digest an approval binds to.

**What a driver MUST do, and what the host enforces about it** (:func:`check_outcome`, run on every
outcome before the host records it):

* A turn that holds an efferent action reports ``gated`` AND carries the held calls in ``pending``,
  each with the raw call in ``full`` (``""`` when the call could not be read: the host refuses to
  approve a hold it cannot show in full, so that one is reject-only). The two are the same fact; one
  without the other is a contract violation, not a result. A driver that runs an action while reporting nothing held cannot be told
  apart from one that did not run it, so what is checked here is the half the host can see: the
  consent row is never empty when the driver says it halted, and a halt is never silent.
* A halted turn carries ``held_digest``, a string naming those exact bytes, and the driver's
  :meth:`~HarnessDriver.held_digest` reads the same thing again later. The host binds an approval to
  it and refuses an approve when the two differ or either is missing, so a driver with no digest has a
  hold that can only be rejected (not a contract violation: it fails closed).
* The driver's :attr:`~HarnessDriver.state` agrees with the outcome: ``awaiting_approval`` exactly
  when the outcome halted, ``idle`` otherwise.
* The outcome is read ONCE (:func:`snapshot_outcome` returns a :class:`TurnSnapshot`); the host records
  that snapshot, never the driver's live result.

**``approval_timing`` is where this contract is deliberately not finished.** ``after_turn`` is what
OpenHands does and what the host implements: the turn RETURNS halted, the session is not busy, and a
separate approve or reject runs the next job. ``in_turn`` is what Claude Code and Codex do: the harness
sends a synchronous request while the turn is still running and blocked, so the answer must travel
concurrently with the turn. That state machine (``active`` to ``awaiting_approval`` to ``active`` or
denied, with a one-shot request id, refusal of stale responses and reap on timeout, disconnect and
shutdown) is slice S10 of the chat-to-primary build. The type reserves it; the host REFUSES a driver
that declares ``in_turn`` rather than driving it through a path that cannot carry it.

**What S10 adds, written down now so the S1 signatures do not have to move** (L2 review, 1006+18):
the in-turn answer path is NEW members beside :meth:`~HarnessDriver.approve` / :meth:`~HarnessDriver.reject`
(which stay the after-turn path): a request object carrying a one-shot request id, the raw call and its
digest; an event that delivers it while the turn runs; an answer method that takes the request id and
the digest the human saw; a stale-answer refusal. The driver owns the native request id, its one-shot
consumption and the response write; the host owns its decision id, the digest the human saw, human
authority and the timeouts. Rules S10 must keep: :meth:`~HarnessDriver.interrupt` and
:meth:`~HarnessDriver.close` resolve every outstanding request as denied before they return, and a late
answer to a reaped request never runs; the decision vocabulary is closed to one-shot forms (a cockpit
approval of one raw call must never become a standing rule such as Codex's execpolicy amendment or an
"always allow"); the host's deadline must not run while a turn is parked on a human; the digest is
per request. Never emulate in-turn consent as after-turn (deny, record, rerun on approve): the model
re-issues the call, so the bytes that run would not be the bytes approved.

**Driver-author rules.** The host reads :meth:`~HarnessDriver.held_digest` and :attr:`~HarnessDriver.state`
while holding its own lock, and its event sink takes that lock: a driver must never call ``on_event``
while holding a lock that ``held_digest`` or ``state`` also takes, and neither may block on a reader
loop. :meth:`~HarnessDriver.interrupt` is repeated about once a second until the job ends, so it must
be idempotent. :meth:`~HarnessDriver.set_model` and :meth:`~HarnessDriver.set_effort` apply to the NEXT
turn and are for an idle driver; per-turn choices travel in :class:`TurnOptions`. ``exit_code``,
``nudged`` and ``tool_activity`` come from the OpenHands loop: a driver with no meaning for one reports
the neutral value (``0`` or ``3`` by ``ok``, ``False``, an empty list), never an invented one.
"""
from __future__ import annotations

import abc
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Literal, Protocol, Sequence, runtime_checkable

__all__ = [
    "ApprovalTiming",
    "DriverCaps",
    "DriverContractError",
    "DriverEvent",
    "DriverState",
    "DriverUnsupported",
    "HarnessDriver",
    "OpenHandsDriver",
    "PendingApproval",
    "TurnOptions",
    "TurnOutcome",
    "TurnSnapshot",
    "check_outcome",
    "snapshot_outcome",
]

DriverState = Literal["idle", "active", "awaiting_approval", "closed"]
"""``active``: a turn, an approval or a refusal is running. ``awaiting_approval``: the harness holds an
action for a human. The host's own session states (opening, busy, gated, broken, ...) are a layer
above this one and are what the HTTP routes report."""

ApprovalTiming = Literal["after_turn", "in_turn"]
"""When a consent request exists: ``after_turn`` (the turn returned halted) or ``in_turn`` (the turn is
still running and blocked on the answer). The module docstring says why only the first is driven."""


class DriverUnsupported(Exception):
    """The driver's harness cannot do this (see :class:`DriverCaps`); callers check the capability first."""


class DriverContractError(Exception):
    """A driver broke the contract (:func:`check_outcome`). The host records the text and breaks the
    session: a driver that cannot be trusted to report a hold is not driven further."""


@dataclass(frozen=True)
class DriverEvent:
    """One normalized event a driver streams while a turn runs. ``kind`` is ``activity`` (a tool action
    was issued) today; a backend may add kinds, and a consumer must ignore a kind it does not know."""

    kind: str
    text: str


@dataclass(frozen=True)
class DriverCaps:
    """What a driver can do beyond the core turn loop. Everything here defaults to refused."""

    approval_timing: ApprovalTiming = "after_turn"
    can_resume: bool = False
    can_set_model: bool = False
    can_set_effort: bool = False
    can_list_models: bool = False


@dataclass(frozen=True)
class TurnOptions:
    """Per-turn choices. Codex takes model and effort on each turn and Claude Code per process (per turn
    under process-per-turn), so they travel with the turn rather than as racing session state. ``None``
    leaves the harness default or the last :meth:`~HarnessDriver.set_model`."""

    model: str | None = None
    effort: str | None = None


@runtime_checkable
class TurnOutcome(Protocol):
    """What a driver returns from a turn, an approval or a refusal. :class:`levain.session.TurnResult`
    satisfies it as it stands, and so does a plain dataclass, so :func:`levain.chat._turn_payload`
    reads every harness's outcome the same way. The host also reads ``unreadable_call`` where present
    (:func:`levain.chat._turn_payload`); the attributes listed are the ones it requires."""

    reply: str | None
    tool_activity: Sequence[Any]
    error: str | None
    nudged: bool
    gated: bool
    timed_out: bool
    pending: Sequence[Any]
    held_digest: str | None

    @property
    def ok(self) -> bool: ...

    @property
    def exit_code(self) -> int: ...


@dataclass(frozen=True)
class PendingApproval:
    """One held call as the host shows it. ``tool_name`` is never empty (a consent row that names no tool
    cannot be judged); ``full`` is the raw call, ``""`` when it could not be read."""

    tool_name: str
    detail: str
    full: str
    reason: str
    recognized: bool


@dataclass(frozen=True)
class TurnSnapshot:
    """A turn's outcome read ONCE, into plain immutable values. The host validates this object and then
    serialises this same object, so what was checked is what is recorded: nothing is read from the
    driver's live result a second time (a result that changed between the check and the record could otherwise pass one and differ at the other;
    ``ok`` and ``exit_code`` are the result's own derived values, read once with the rest and not re-derived here)."""

    reply: str | None
    tool_activity: tuple[Any, ...]
    error: str | None
    nudged: bool
    gated: bool
    timed_out: bool
    pending: tuple[PendingApproval, ...]
    held_digest: str | None
    unreadable_call: bool
    ok: bool
    exit_code: int


_ABSENT = object()
_OUTCOME_FIELDS = ("reply", "tool_activity", "error", "nudged", "gated", "timed_out", "pending",
                   "held_digest", "ok", "exit_code")


def _pending_row(p: Any) -> PendingApproval:
    """One held call, each attribute read ONCE into a local and validated from that local."""
    name, detail, full = getattr(p, "tool_name", None), getattr(p, "detail", ""), getattr(p, "full", "")
    reason, recognized = getattr(p, "reason", ""), getattr(p, "recognized", False)
    return PendingApproval(
        tool_name=name if isinstance(name, str) else "",
        detail=str(detail),
        full=full if isinstance(full, str) else "",
        reason=str(reason),
        recognized=bool(recognized),
    )


def read_outcome(outcome: Any, *, strict: bool = True) -> TurnSnapshot:
    """Read ``outcome`` once. ``strict`` (the driver path) requires every field of :class:`TurnOutcome`;
    otherwise a missing ``held_digest`` reads as ``None`` (a hold that can only be rejected)."""
    got: dict[str, Any] = {n: getattr(outcome, n, _ABSENT) for n in _OUTCOME_FIELDS}   # ONE read of each field
    missing = [n for n, v in got.items() if v is _ABSENT and (strict or n != "held_digest")]
    if missing:
        raise DriverContractError(
            "the driver's outcome does not carry the fields the host reads (reply, tool_activity, error, "
            "nudged, gated, timed_out, pending, held_digest, ok, exit_code); a missing `gated` or "
            f"`pending` would read as 'nothing held' (missing: {', '.join(missing)})")
    ok, exit_code, error = got["ok"], got["exit_code"], got["error"]
    digest = got["held_digest"] if got["held_digest"] is not _ABSENT else None
    unreadable = getattr(outcome, "unreadable_call", False)
    if not isinstance(ok, bool) or isinstance(exit_code, bool) or not isinstance(exit_code, int):
        raise DriverContractError(
            "the driver's outcome reports `ok` / `exit_code` as something other than a bool and an int "
            "(a method where a value was meant)")
    if error is not None and not isinstance(error, str):
        raise DriverContractError("the driver's outcome reports `error` as something other than text")
    pending = tuple(_pending_row(p) for p in (got["pending"] or ()))
    return TurnSnapshot(
        reply=got["reply"],
        tool_activity=tuple(got["tool_activity"] or ()),
        error=error,
        nudged=bool(got["nudged"]),
        gated=bool(got["gated"]),
        timed_out=bool(got["timed_out"]),
        pending=pending,
        held_digest=digest if isinstance(digest, str) else None,
        unreadable_call=bool(unreadable),
        ok=ok,
        exit_code=exit_code,
    )


def snapshot_outcome(driver: "HarnessDriver", outcome: Any) -> TurnSnapshot:
    """Read ``outcome`` once and raise :class:`DriverContractError` unless that snapshot keeps the
    contract's guarantees about a hold (module docstring); return the snapshot for the host to record.
    An outcome that already failed (``error`` set) is not held to them: the host breaks that session and
    decides nothing on it."""
    snap = read_outcome(outcome)
    if snap.error is not None:
        return snap
    if snap.gated and not snap.pending:
        raise DriverContractError(
            "the driver reported a halted turn with no held action: there is no consent row to show")
    if snap.pending and not snap.gated:
        raise DriverContractError(
            "the driver returned held actions on a turn it did not report as halted: "
            "nothing would stop them being run")
    if any(not p.tool_name for p in snap.pending):
        raise DriverContractError("a held action names no tool: there is no consent row to judge")
    state = driver.state
    if snap.gated and state != "awaiting_approval":
        raise DriverContractError(f"the outcome is halted but the driver reads {state!r}")
    if not snap.gated and state != "idle":
        raise DriverContractError(f"the outcome is not halted but the driver reads {state!r}, not 'idle'")
    return snap


def check_outcome(driver: "HarnessDriver", outcome: Any) -> None:
    """:func:`snapshot_outcome` for a caller that only wants the verdict."""
    snapshot_outcome(driver, outcome)


class HarnessDriver(abc.ABC):
    """One conversation with one entity through one harness. See the module docstring.

    Threading: the host calls :meth:`open`, :meth:`send_turn`, :meth:`approve` and :meth:`reject` from
    one worker thread at a time (a session runs at most one job), and :meth:`interrupt` and
    :meth:`close` from other threads while a job runs, which every implementation must tolerate."""

    harness: str = "unnamed"
    """The harness's name, for display and logs (``openhands``, later ``claude-code`` and ``codex``)."""

    caps: DriverCaps = DriverCaps()

    # -- life cycle ------------------------------------------------------------

    @abc.abstractmethod
    def open(self, on_event: Callable[[DriverEvent], None], *, resume: str | None = None) -> None:
        """Open the conversation, or resume ``resume`` where :attr:`caps` says that is possible. A
        refusal or a failure raises; the host keeps the message text and drops the exception, and then
        calls :meth:`close` (idempotent) so a failed open releases whatever it had built."""

    @abc.abstractmethod
    def close(self) -> None:
        """Release everything the conversation holds (a shell, a process, a socket). Idempotent."""

    @property
    @abc.abstractmethod
    def state(self) -> DriverState: ...

    @abc.abstractmethod
    def describe(self) -> dict[str, Any]:
        """What the operator's banner says about this conversation: JSON-shaped, from the session's
        own resolved fields, never a second resolution."""

    # -- turns and consent -------------------------------------------------------

    @abc.abstractmethod
    def send_turn(self, message: str, *, options: TurnOptions | None = None) -> TurnOutcome:
        """Send one operator message and run it to a result. A held action comes back as a halted
        outcome carrying the raw calls (``after_turn``), never as an executed action. ``options`` a
        driver's :attr:`caps` do not allow raise :class:`DriverUnsupported`."""

    def approve(self) -> TurnOutcome:
        """AFTER-TURN path: run what the last halted outcome held. The host has already bound this to the
        digest it showed; a driver that cannot run exactly that raises. An ``in_turn`` driver never
        gets this call."""
        raise DriverUnsupported(f"{self.harness} has no after-turn approval")

    def reject(self, reason: str) -> TurnOutcome:
        """AFTER-TURN path: refuse what the last halted outcome held, telling the entity ``reason``;
        nothing runs."""
        raise DriverUnsupported(f"{self.harness} has no after-turn refusal")

    @abc.abstractmethod
    def held_digest(self) -> str | None:
        """A string naming the held action's exact bytes as they stand NOW, or ``None`` when nothing
        is held or it cannot be read. The host compares it with the one the screen was shown."""

    @abc.abstractmethod
    def interrupt(self) -> None:
        """Ask the running turn to stop. Called repeatedly, from another thread, until the job ends, so
        it is idempotent and tolerates a closed driver."""

    # -- optional, by capability -------------------------------------------------

    def set_model(self, model: str) -> None:
        raise DriverUnsupported(f"{self.harness} cannot switch model")

    def set_effort(self, effort: str) -> None:
        raise DriverUnsupported(f"{self.harness} has no effort setting")

    def list_models(self) -> tuple[str, ...]:
        raise DriverUnsupported(f"{self.harness} cannot list models")

    @property
    def native(self) -> Any:
        """The harness's own session object where one exists (tests read it through
        :attr:`levain.chat._Session.session`). ``None`` otherwise."""
        return None


# -- OpenHands --------------------------------------------------------------------------------------

SessionOpener = Callable[..., Any]
"""``opener(entity_dir, on_event=<str sink>)`` returns an opened :class:`~levain.session.EntitySession`
(or anything shaped like one). It is where this entity's refusals live."""


_Phase = Literal["new", "opening", "open", "closing", "closed"]


@dataclass
class OpenHandsDriver(HarnessDriver):
    """Today's chat turn loop behind the contract: an :class:`~levain.session.EntitySession` driven
    through ``run_turn`` / ``resume_turn`` / ``reject_turn``. The opener builds the hands itself from
    the operator's command line; this class never sees a client-supplied agent or spec.

    **Life cycle is a forward-only state machine, and the phase is the guard** (LSP's
    initialize/shutdown/exit, ACP's cancel-ends-the-turn): ``new -> opening -> open -> closing -> closed``,
    every phase transition made once under one condition lock, and a turn's start or refusal decided in the same hold.
    :meth:`open` is accepted only from ``new``; it publishes the session it built only if the phase is
    still ``opening`` when it arrives, otherwise a :meth:`close` won and the session is released there.
    A turn starts only from ``open`` and at most one runs. :meth:`close` never releases a session under a
    running turn: it stops the turn and waits for the turn's own return, then releases (the turn is ended,
    not torn down). When :meth:`close` returns, nothing is held and no turn runs."""

    entity_dir: Path
    opener: SessionOpener
    harness: str = field(default="openhands", init=False)
    caps: DriverCaps = field(default=DriverCaps(), init=False)
    _session: Any = field(default=None, init=False, repr=False)
    _phase: _Phase = field(default="new", init=False, repr=False)
    _running: bool = field(default=False, init=False, repr=False)
    _halted: bool = field(default=False, init=False, repr=False)
    _cond: Any = field(default_factory=threading.Condition, init=False, repr=False)

    def open(self, on_event: Callable[[DriverEvent], None], *, resume: str | None = None) -> None:
        if resume is not None:
            raise DriverUnsupported(
                "openhands conversations live in server memory only; resume across a restart is not offered")
        with self._cond:
            if self._phase != "new":
                raise RuntimeError(f"the driver cannot open from {self._phase!r}: it opens once")
            self._phase = "opening"

        def _sink(line: str) -> None:
            on_event(DriverEvent("activity", line))

        try:
            session = self.opener(self.entity_dir, on_event=_sink)
        except BaseException:
            with self._cond:
                self._phase = "closed"
                self._cond.notify_all()
            raise
        with self._cond:
            won = self._phase == "opening"
            if won:
                self._session, self._phase = session, "open"
        if not won:
            # close() ran while the opener was building: it is waiting for this release.
            try:
                session.close()
            finally:
                with self._cond:
                    self._phase = "closed"
                    self._cond.notify_all()
            raise RuntimeError("the driver was closed while it opened")

    @property
    def native(self) -> Any:
        return self._session

    @property
    def state(self) -> DriverState:
        with self._cond:
            if self._phase in ("open", "closing") and self._running:
                return "active"     # a turn being ended by close() is still a turn
            if self._phase != "open":
                return "closed"
            return "active" if self._running else ("awaiting_approval" if self._halted else "idle")

    def _run(self, call: Callable[[Any], Any]) -> Any:
        with self._cond:
            if self._phase != "open":
                raise RuntimeError("the driver is not open")
            if self._running:
                raise RuntimeError("a turn is already running on this driver")
            session = self._session
            self._running = True
        result: Any = None
        try:
            result = call(session)
            return result
        finally:
            try:
                gated = bool(getattr(result, "gated", False))   # an errored halt still holds its actions
            except Exception:  # noqa: BLE001 — a result that cannot say reads as held: fail closed
                gated = True
            with self._cond:    # nothing above can keep the guard from being released
                self._running, self._halted = False, gated
                self._cond.notify_all()

    def send_turn(self, message: str, *, options: TurnOptions | None = None) -> TurnOutcome:
        if options is not None and (options.model is not None or options.effort is not None):
            raise DriverUnsupported("openhands takes its model from the operator's command line, per server")
        return self._run(lambda s: s.run_turn(message))

    def approve(self) -> TurnOutcome:
        return self._run(lambda s: s.resume_turn())

    def reject(self, reason: str) -> TurnOutcome:
        return self._run(lambda s: s.reject_turn(reason))

    def held_digest(self) -> str | None:
        s = self._session
        return s.held_digest() if s is not None else None

    def interrupt(self) -> None:
        # The stop request runs OUTSIDE the lock: it may block (the SDK's pause waits for a step's state
        # lock), and `state` / `held_digest` must not wait on it.
        with self._cond:
            session = self._session
        if session is not None:
            session.request_stop()

    def close(self) -> None:
        with self._cond:
            if self._phase == "new":
                self._phase = "closed"
                return
            if self._phase in ("opening", "closing"):
                # an opener in flight releases its own session on arrival (open()); another closer
                # is mid-release. Either way, wait until the release is done.
                if self._phase == "opening":
                    self._phase = "closing"
                self._cond.wait_for(lambda: self._phase == "closed")
                return
            if self._phase == "closed":
                return
            self._phase = "closing"      # no new turn can start from here
        while True:
            with self._cond:
                if not self._running:
                    break
            try:
                self.interrupt()
            except Exception:  # noqa: BLE001 — keep asking; the turn's own return is what we wait for
                pass
            with self._cond:
                if self._running:
                    # repeated: a stop request landing before the run loop starts is undone by it
                    self._cond.wait(1.0)
        with self._cond:
            session, self._session = self._session, None
        try:
            session.close()
        finally:
            with self._cond:
                self._phase = "closed"
                self._cond.notify_all()

    def describe(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        session = self._session
        for name in ("label", "model_label", "gate_mode", "bash_ok", "deny_standard_creds",
                     "bash_offline", "ssh_mode"):
            value = getattr(session, name, None)
            if isinstance(value, (str, bool)):
                out[name] = value
        workspace = getattr(session, "workspace", None)
        if workspace is not None:
            out["workspace"] = str(workspace)
        return out

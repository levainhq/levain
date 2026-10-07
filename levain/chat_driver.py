"""levain.chat_driver — the HARNESS DRIVER CONTRACT behind the chat host, and the OpenHands driver.

The chat host (:mod:`levain.chat`) used to talk straight to an :class:`levain.session.EntitySession`.
That fixes the harness: the primary entity of a Claude Code or Codex install cannot be driven through
it. A :class:`HarnessDriver` is the seam instead. The host holds one driver per (entity, conversation),
keyed by the entity's name, so N entities (a constellation) is a matter of which driver each name maps
to. The driver owns everything harness-specific: how a session is opened and torn down, how a turn is
sent, how the harness's own consent surfaces, how to stop a turn. The host keeps everything that is
not: the propose, job and poll runtime, the session cap, the deadline watcher, the decision id and the
digest an approval binds to.

**What a driver MUST do, and what makes it hold** (every check is made where the outcome is built, so a
snapshot that breaks one cannot exist):

* A turn, approval or refusal returns ONE :class:`TurnSnapshot`, built once by the driver from its
  harness's result (:func:`read_outcome` reads a :class:`TurnOutcome`-shaped result once) and immutable.
  The host records that object and nothing else: it never reads the driver's live result or its
  :attr:`~HarnessDriver.state` to decide about a turn. A return that is not a :class:`TurnSnapshot` is a
  contract violation.
* A turn that holds an efferent action reports ``gated`` AND carries the held calls in ``pending``,
  each with the raw call in ``full`` (``""`` when the call could not be read: the host refuses to
  approve a hold it cannot show in full, so that one is reject-only). The two are the same fact; one
  without the other is a contract violation, not a result. An outcome that already failed (``error``
  set) is not held to that pairing (the host breaks that session and decides nothing on it), but every
  held call it carries must still be a well-formed row that names its tool. A driver
  that runs an action while reporting nothing held cannot be told apart from one that did not run it,
  so what is checked here is the half the host can see: the consent row is never empty when the
  driver says it halted, and a halt is never silent.
* A halted turn carries ``held_digest``, a string naming those exact bytes, and the driver's
  :meth:`~HarnessDriver.held_digest` reads the same thing again later. The host binds an approval to
  it and refuses an approve when the two differ or either is missing, so a driver with no digest has a
  hold that can only be rejected (not a contract violation: it fails closed).
* The driver's :attr:`~HarnessDriver.state` is derived from the snapshot it returns, in the same step
  that ends the turn: ``awaiting_approval`` exactly when that snapshot halted, ``idle`` otherwise. It is
  for display and for the driver's own guards; the host does not check it against the outcome, because
  a state read after the turn returned can already have moved on (a close landing in between).

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

**Driver-author rules.** The host reads :meth:`~HarnessDriver.held_digest` while holding its own lock,
and its event sink takes that lock: a driver must never call ``on_event`` while holding a lock that
``held_digest`` also takes, and ``held_digest`` may not block on a reader loop. :meth:`~HarnessDriver.interrupt` is repeated about once a second until the job ends, so it must
be idempotent. :meth:`~HarnessDriver.set_model` and :meth:`~HarnessDriver.set_effort` apply to the NEXT
turn and are for an idle driver; per-turn choices travel in :class:`TurnOptions`. ``exit_code``,
``nudged`` and ``tool_activity`` come from the OpenHands loop: a driver with no meaning for one reports
the neutral value (``0`` or ``3`` by ``ok``, ``False``, an empty list), never an invented one.
"""
from __future__ import annotations

import abc
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Literal, Protocol, Sequence, runtime_checkable

__all__ = [
    "ApprovalTiming",
    "DEFAULT_CLOSE_WAIT_SECONDS",
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
    "read_outcome",
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
    """A driver broke the contract (module docstring; checked where a :class:`TurnSnapshot` is built, and by
    the host for a return that is not one). The host records the text and breaks the
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
    """The shape :func:`read_outcome` reads a harness's result from. :class:`levain.session.TurnResult`
    satisfies it as it stands, and so does a plain dataclass. ``unreadable_call`` is read where present;
    the attributes listed are the ones it requires."""

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


def _is_text(value: Any) -> bool:
    return isinstance(value, str)


@dataclass(frozen=True)
class PendingApproval:
    """One held call as the host shows it. ``tool_name`` is never empty (a consent row that names no tool
    cannot be judged); ``full`` is the raw call, ``""`` when it could not be read. Checked at
    construction: a row of the wrong shape is a :class:`DriverContractError`, never coerced into one."""

    tool_name: str
    detail: str
    full: str
    reason: str
    recognized: bool

    def __post_init__(self) -> None:
        if not _is_text(self.tool_name) or not self.tool_name:
            raise DriverContractError("a held action names no tool: there is no consent row to judge")
        if not (_is_text(self.detail) and _is_text(self.full) and _is_text(self.reason)):
            raise DriverContractError("a held action's detail, full call or reason is not text")
        if not isinstance(self.recognized, bool):
            raise DriverContractError("a held action's `recognized` is not a bool")


@dataclass(frozen=True)
class TurnSnapshot:
    """A turn's outcome, built ONCE by the driver and immutable: the one terminal record of a turn (ACP's
    single stop reason, Codex's ``turn/completed``). Every guarantee of the contract (module docstring) is
    checked here, at construction, so a snapshot that exists keeps them; the host serialises this same
    object, and nothing of the outcome is read a second time (the live :meth:`~HarnessDriver.held_digest`
    is re-read at approve on purpose, to compare with the one recorded here). ``ok`` and ``exit_code`` are the harness's
    own classification, carried, not re-derived."""

    reply: str | None
    tool_activity: tuple[str, ...]
    error: str | None
    nudged: bool
    gated: bool
    timed_out: bool
    pending: tuple[PendingApproval, ...]
    held_digest: str | None
    unreadable_call: bool
    ok: bool
    exit_code: int

    def __post_init__(self) -> None:
        for name in ("reply", "error", "held_digest"):
            if getattr(self, name) is not None and not _is_text(getattr(self, name)):
                raise DriverContractError(f"the outcome's `{name}` is neither text nor None")
        for name in ("nudged", "gated", "timed_out", "unreadable_call", "ok"):
            if not isinstance(getattr(self, name), bool):
                raise DriverContractError(
                    f"the outcome's `{name}` is not a bool (a method or a value where a flag was meant)")
        if isinstance(self.exit_code, bool) or not isinstance(self.exit_code, int):
            raise DriverContractError("the outcome's `exit_code` is not an int")
        if not isinstance(self.tool_activity, tuple) or not all(_is_text(x) for x in self.tool_activity):
            raise DriverContractError("the outcome's `tool_activity` is not a sequence of text lines")
        if not isinstance(self.pending, tuple) or not all(isinstance(p, PendingApproval) for p in self.pending):
            raise DriverContractError("the outcome's `pending` is not a tuple of PendingApproval rows")
        if self.error is not None:
            return      # failed: the host decides nothing on it (module docstring); its rows were checked
        if self.gated and not self.pending:
            raise DriverContractError(
                "the driver reported a halted turn with no held action: there is no consent row to show")
        if self.pending and not self.gated:
            raise DriverContractError(
                "the driver returned held actions on a turn it did not report as halted: "
                "nothing would stop them being run")


_ABSENT = object()
_OUTCOME_FIELDS = ("reply", "tool_activity", "error", "nudged", "gated", "timed_out", "pending",
                   "held_digest", "ok", "exit_code")


def _lines(value: Any, what: str) -> tuple[Any, ...]:
    """A list or tuple as a tuple. A string is refused rather than split into characters, and so is any
    other iterable: the shape is checked, never coerced."""
    if not isinstance(value, (list, tuple)):
        raise DriverContractError(f"the outcome's `{what}` is not a list or tuple")
    return tuple(value)


def _pending_row(p: Any) -> PendingApproval:
    """One held call, each attribute read ONCE and handed to :class:`PendingApproval`, which checks it.
    The one value normalised rather than refused is a ``full`` that is not text: it reads as ``""``, the
    contract's "could not be read", which makes the hold reject-only (fail closed, never open)."""
    full: Any = getattr(p, "full", "")
    name: Any = getattr(p, "tool_name", None)    # checked by PendingApproval, never coerced
    return PendingApproval(
        tool_name=name,
        detail=getattr(p, "detail", ""),
        full=full if isinstance(full, str) else "",
        reason=getattr(p, "reason", ""),
        recognized=getattr(p, "recognized", False),
    )


def read_outcome(outcome: Any, *, strict: bool = True) -> TurnSnapshot:
    """Read a harness's ``outcome`` once into a :class:`TurnSnapshot` (which checks it). ``strict`` (the
    driver path) requires every field of :class:`TurnOutcome`; otherwise a missing ``held_digest`` reads
    as ``None`` (a hold that can only be rejected)."""
    got: dict[str, Any] = {n: getattr(outcome, n, _ABSENT) for n in _OUTCOME_FIELDS}   # ONE read of each field
    missing = [n for n, v in got.items() if v is _ABSENT and (strict or n != "held_digest")]
    if missing:
        raise DriverContractError(
            "the driver's outcome does not carry the fields the host reads (reply, tool_activity, error, "
            "nudged, gated, timed_out, pending, held_digest, ok, exit_code); a missing `gated` or "
            f"`pending` would read as 'nothing held' (missing: {', '.join(missing)})")
    return TurnSnapshot(
        reply=got["reply"],
        tool_activity=_lines(got["tool_activity"], "tool_activity"),
        error=got["error"],
        nudged=got["nudged"],
        gated=got["gated"],
        timed_out=got["timed_out"],
        pending=tuple(_pending_row(p) for p in _lines(got["pending"], "pending")),
        held_digest=None if got["held_digest"] is _ABSENT else got["held_digest"],
        unreadable_call=getattr(outcome, "unreadable_call", False),
        ok=got["ok"],
        exit_code=got["exit_code"],
    )


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
        """Release everything the conversation holds (a shell, a process, a socket). Idempotent, and
        bounded: it never releases under a running turn, it stops the turn and waits for its return, and a
        release it could not wait for is made by whoever still holds the conversation when they let go
        (the turn on its return, an opener on its arrival). No new turn starts once it is called."""

    @property
    @abc.abstractmethod
    def state(self) -> DriverState: ...

    @abc.abstractmethod
    def describe(self) -> dict[str, Any]:
        """What the operator's banner says about this conversation: JSON-shaped, from the session's
        own resolved fields, never a second resolution."""

    # -- turns and consent -------------------------------------------------------

    @abc.abstractmethod
    def send_turn(self, message: str, *, options: TurnOptions | None = None) -> TurnSnapshot:
        """Send one operator message and run it to a result. A held action comes back as a halted
        snapshot carrying the raw calls (``after_turn``), never as an executed action. ``options`` a
        driver's :attr:`caps` do not allow raise :class:`DriverUnsupported`."""

    def approve(self) -> TurnSnapshot:
        """AFTER-TURN path: run what the last halted outcome held. The host has already bound this to the
        digest it showed; a driver that cannot run exactly that raises. An ``in_turn`` driver never
        gets this call."""
        raise DriverUnsupported(f"{self.harness} has no after-turn approval")

    def reject(self, reason: str) -> TurnSnapshot:
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

DEFAULT_CLOSE_WAIT_SECONDS = 30.0
"""How long :meth:`OpenHandsDriver.close` waits for a running turn to end before it hands the release to
that turn's own return. The SDK stops at a step boundary, so a step longer than this outlives the wait."""


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
    A turn starts only from ``open`` and at most one runs; it ends in one lock hold that releases the turn
    guard and sets the state from the snapshot it returns, whatever it raised. :meth:`close` never
    releases a session under a running turn: it stops the turn and waits up to ``close_wait`` seconds for
    the turn's own return, then releases (the turn is ended, not torn down). A turn still running when
    that wait runs out, or when a stop request raises out of :meth:`close`, releases the session itself on
    its return: the session is released exactly once and the phase then reads ``closed``, either way."""

    entity_dir: Path
    opener: SessionOpener
    close_wait: float = DEFAULT_CLOSE_WAIT_SECONDS
    harness: str = field(default="openhands", init=False)
    caps: DriverCaps = field(default=DriverCaps(), init=False)
    _session: Any = field(default=None, init=False, repr=False)
    _phase: _Phase = field(default="new", init=False, repr=False)
    _running: bool = field(default=False, init=False, repr=False)
    _halted: bool = field(default=False, init=False, repr=False)
    _turn_releases: bool = field(default=False, init=False, repr=False)   # close() handed the release to the turn
    _cond: Any = field(default_factory=threading.Condition, init=False, repr=False)

    def __post_init__(self) -> None:
        if not (isinstance(self.close_wait, (int, float)) and 0 < self.close_wait <= threading.TIMEOUT_MAX):
            raise ValueError("close_wait must be a finite number of seconds above 0: close() is bounded")

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
            # close() ran while the opener was building: this arrival is the release.
            self._release(session)
            raise RuntimeError("the driver was closed while it opened")

    def _release(self, session: Any) -> None:
        """Close ``session`` (the caller has already taken it off the driver) and mark the driver closed,
        whatever the close raises."""
        try:
            if session is not None:
                session.close()
        finally:
            with self._cond:
                self._phase = "closed"
                self._cond.notify_all()

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

    def _run(self, call: Callable[[Any], Any]) -> TurnSnapshot:
        with self._cond:
            if self._phase != "open":
                raise RuntimeError("the driver is not open")
            if self._running:
                raise RuntimeError("a turn is already running on this driver")
            session = self._session
            self._running = True
        snap: TurnSnapshot | None = None
        try:
            snap = read_outcome(call(session))    # the turn's one terminal record, read once
            return snap
        finally:
            # One hold, reached on any raise (BaseException included): the guard is released and the state
            # is set from the snapshot being returned. No snapshot (the call or the read raised) reads as
            # held: what the harness holds is unknown, so fail closed.
            owned: Any = None
            with self._cond:
                self._running = False
                self._halted = snap.gated if snap is not None else True
                release = self._turn_releases
                if release:
                    owned, self._session = self._session, None
                self._cond.notify_all()
            if release:
                self._release(owned)

    def send_turn(self, message: str, *, options: TurnOptions | None = None) -> TurnSnapshot:
        if options is not None and (options.model is not None or options.effort is not None):
            raise DriverUnsupported("openhands takes its model from the operator's command line, per server")
        return self._run(lambda s: s.run_turn(message))

    def approve(self) -> TurnSnapshot:
        return self._run(lambda s: s.resume_turn())

    def reject(self, reason: str) -> TurnSnapshot:
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
            if self._phase in ("new", "closed"):
                self._phase = "closed"
                return
            if self._phase in ("opening", "closing"):
                # an opener in flight releases its own session on arrival (open()); another closer or a
                # turn is mid-release. Either way, wait (bounded) until the release is done.
                if self._phase == "opening":
                    self._phase = "closing"
                self._cond.wait_for(lambda: self._phase == "closed", timeout=self.close_wait)
                return
            self._phase = "closing"      # no new turn can start from here
        deadline = time.monotonic() + self.close_wait
        owned: Any = None
        try:
            while True:
                with self._cond:
                    if not self._running or time.monotonic() >= deadline:
                        break
                try:
                    self.interrupt()
                except Exception:  # noqa: BLE001 — keep asking; the turn's own return is what we wait for
                    pass
                with self._cond:
                    if self._running:
                        # repeated: a stop request landing before the run loop starts is undone by it
                        self._cond.wait(max(0.0, min(1.0, deadline - time.monotonic())))
        finally:
            # Reached when the wait runs out AND on a raise out of the stop request: whoever holds the
            # session releases it, never both and never under a running turn.
            with self._cond:
                mine = not self._running
                if mine:
                    owned, self._session = self._session, None
                else:
                    self._turn_releases = True   # _run's own return releases it
            if mine:
                self._release(owned)

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

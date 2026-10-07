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
  that ends the turn: ``awaiting_approval`` when that snapshot halted, or when no snapshot could be built
  (the call or the read raised: what the harness holds is unknown, so it fails closed), ``idle`` otherwise. It is
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

**Driver-author rules.** The chat host runs every call into a driver (each attribute it reads included)
on worker threads of that driver's own, started when the session opens, through one boundary
(:func:`_call_driver`), and waits for each with a deadline. Nothing a driver raises reaches the host, no
call is made under the host's lock (the host's event sink takes that lock, so a driver may call
``on_event`` and ``on_released`` from any thread), and a driver that does not answer in time is not
waited for: the call fails closed (an approve is refused, a turn fails) and the session reads
``unresponsive`` until it is closed. Every value is checked for its exact declared type. A release is
REPORTED by the driver (``on_released``, :meth:`~HarnessDriver.open`), never polled.
:meth:`~HarnessDriver.interrupt` is repeated about once a second until the job ends, so it must
be idempotent, and it may reach a harness session that is being released concurrently (the stop request
is made outside the driver's lock), so it must tolerate one. :meth:`~HarnessDriver.set_model` and :meth:`~HarnessDriver.set_effort` apply to the NEXT
turn and are for an idle driver; per-turn choices travel in :class:`TurnOptions`. ``exit_code``,
``nudged`` and ``tool_activity`` come from the OpenHands loop: a driver with no meaning for one reports
the neutral value (``0`` or ``3`` by ``ok``, ``False``, an empty list), never an invented one.
"""
from __future__ import annotations

import abc
import logging
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

    def __post_init__(self) -> None:
        # Exact types (codex r5, RAN): a str subclass naming "in_turn" whose `!=` lies would be opened as an
        # after-turn driver, and its first consent request would wait on a path the host does not have.
        if type(self.approval_timing) is not str or self.approval_timing not in ("after_turn", "in_turn"):
            raise DriverContractError("approval_timing is not exactly 'after_turn' or 'in_turn'")
        for name in ("can_resume", "can_set_model", "can_set_effort", "can_list_models"):
            if type(getattr(self, name)) is not bool:
                raise DriverContractError(f"the capability `{name}` is not a bool")


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
    satisfies it as it stands, and so does a plain dataclass. ``unreadable_call`` and ``unreadable_unchecked``
    are read where present; the attributes listed are the ones it requires."""

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
    """EXACTLY ``str``. A subclass is driver code wearing text's type: it can show ``""`` to the client while
    its ``__iter__`` or ``__eq__`` says otherwise to the host's checks (codex L3 r4, run: a blank-looking
    held call passed ``shown_in_full`` and was approved)."""
    return type(value) is str


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
        if not _is_text(self.tool_name) or not self.tool_name.strip():
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
    unreadable_unchecked: bool = False
    """The harness could not check whether ``reply`` is an unreadable tool call (it fails closed with
    ``unreadable_call`` set too); a client shows its own notice for it."""

    def __post_init__(self) -> None:
        for name in ("reply", "error", "held_digest"):
            if getattr(self, name) is not None and not _is_text(getattr(self, name)):
                raise DriverContractError(f"the outcome's `{name}` is neither text nor None")
        for name in ("nudged", "gated", "timed_out", "unreadable_call", "unreadable_unchecked", "ok"):
            if not isinstance(getattr(self, name), bool):
                raise DriverContractError(
                    f"the outcome's `{name}` is not a bool (a method or a value where a flag was meant)")
        if type(self.exit_code) is not int:
            raise DriverContractError("the outcome's `exit_code` is not an int")
        if type(self.tool_activity) is not tuple or not all(_is_text(x) for x in self.tool_activity):
            raise DriverContractError("the outcome's `tool_activity` is not a sequence of text lines")
        if type(self.pending) is not tuple or not all(type(p) is PendingApproval for p in self.pending):
            raise DriverContractError("the outcome's `pending` is not a tuple of PendingApproval rows")
        if self.held_digest is not None and not self.held_digest.strip():
            raise DriverContractError("the outcome's `held_digest` is blank: it names no bytes an approval could bind to")
        if self.timed_out and self.error is None:
            raise DriverContractError(
                "the outcome is timed out but carries no error: a stopped turn did not complete, and an "
                "outcome without an error would leave the session open for another turn")
        if self.error is not None:
            return      # failed: the host decides nothing on it (module docstring); its rows were checked
        if self.gated and not self.pending:
            raise DriverContractError(
                "the driver reported a halted turn with no held action: there is no consent row to show")
        if self.pending and not self.gated:
            raise DriverContractError(
                "the driver returned held actions on a turn it did not report as halted: "
                "nothing would stop them being run")


_log = logging.getLogger(__name__)
_ABSENT = object()


@dataclass(frozen=True)
class DriverCall:
    """What one call into driver code came to (:func:`_call_driver`): its value, or why it did not give
    one, as TEXT (never the exception: its traceback would keep the frames of the failed call alive)."""

    ok: bool
    value: Any = None
    error: str | None = None      # "<class>: <text>", for a record and a log
    message: str | None = None    # the exception's own text, or its class name when it has none
    unanswered: bool = False      # the host stopped waiting: the driver did not answer within its deadline


def _call_driver(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> DriverCall:
    """THE one way to run driver code (ruled 2026-10-07): it catches ``BaseException`` (a ``TurnTimeout`` or
    any other non-``Exception`` included), so nothing the call raises escapes into the caller, and returns
    a :class:`DriverCall` whose error is TEXT read by :func:`_exc_text`, which cannot raise. The call site
    decides what a failure means. It runs ``fn`` on the calling thread and does not bound it: the chat host
    runs it on the driver's own call workers and waits for them with a deadline (:mod:`levain.chat`), and
    an OpenHands driver runs it on its own release worker and stop thread."""
    try:
        return DriverCall(True, fn(*args, **kwargs))
    except BaseException as exc:  # noqa: BLE001 — the boundary: nothing a driver raises crosses it
        name, text = _exc_text(exc)
        return DriverCall(False, None, f"{name}: {text}", text or name)


def _exc_text(exc: BaseException) -> tuple[str, str]:
    """An exception's class name and text, read so that neither can raise: both are driver code (a
    metaclass ``__name__``, an ``__str__``) and this runs inside the boundary's own ``except``."""
    try:
        name = str(type(exc).__name__)
    except BaseException:  # noqa: BLE001
        name = "BaseException"
    try:
        text = str(exc)
    except BaseException:  # noqa: BLE001
        text = f"<unprintable {name}>"
    return name, text
_OUTCOME_FIELDS = ("reply", "tool_activity", "error", "nudged", "gated", "timed_out", "pending",
                   "held_digest", "ok", "exit_code")


def _lines(value: Any, what: str) -> tuple[Any, ...]:
    """A sequence as a tuple (what :class:`TurnOutcome` declares). Text is refused rather than split into
    characters, and so is a non-sequence iterable (a set, a generator): the shape is checked, never coerced."""
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise DriverContractError(f"the outcome's `{what}` is not a sequence (a list or tuple)")
    return tuple(value)


def _pending_row(p: Any) -> PendingApproval:
    """One held call, each attribute read ONCE and handed to :class:`PendingApproval`, which checks it.
    A missing ``tool_name``, ``detail`` or ``reason`` is refused (a row the harness did not describe is not
    one an operator can judge). Present but blank ``detail`` or ``reason`` text is carried as it is: the
    bytes an approval runs are ``full``, which the host refuses to approve when blank, and ``detail`` is a
    one-line convenience parsed from it that can legitimately be empty. Two values fail closed instead of being refused: a ``full`` that is
    missing or not exactly text reads as ``""``, the contract's "could not be read" (the hold is then reject-only),
    and a missing ``recognized`` reads as ``False``."""
    got: dict[str, Any] = {n: getattr(p, n, _ABSENT) for n in ("tool_name", "detail", "reason", "full", "recognized")}
    missing = [n for n in ("tool_name", "detail", "reason") if got[n] is _ABSENT]
    if missing:
        raise DriverContractError(f"a held action does not carry {', '.join(missing)}: there is no consent row to judge")
    full, recognized = got["full"], got["recognized"]
    return PendingApproval(
        tool_name=got["tool_name"],
        detail=got["detail"],
        full=full if _is_text(full) else "",
        reason=got["reason"],
        recognized=False if recognized is _ABSENT else recognized,
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
        unreadable_unchecked=getattr(outcome, "unreadable_unchecked", False),
        ok=got["ok"],
        exit_code=got["exit_code"],
    )


class HarnessDriver(abc.ABC):
    """One conversation with one entity through one harness. See the module docstring.

    Threading: the chat host calls :meth:`open`, :meth:`send_turn`, :meth:`approve` and :meth:`reject` on
    one thread (the session's turn lane, one call at a time), :meth:`interrupt`, :meth:`held_digest`,
    :meth:`describe` and every attribute read on another (its control lane), and :meth:`close` on a third
    (its release lane), so :meth:`interrupt` and :meth:`close` can arrive while a turn runs, which every
    implementation must tolerate. It waits for each with a deadline and does not wait past it."""

    harness: str = "unnamed"
    """The harness's name, for display and logs (``openhands``, later ``claude-code`` and ``codex``)."""

    caps: DriverCaps = DriverCaps()

    # -- life cycle ------------------------------------------------------------

    @abc.abstractmethod
    def open(self, on_event: Callable[[DriverEvent], None], *, on_released: Callable[[str | None], None],
             resume: str | None = None) -> None:
        """Open the conversation, or resume ``resume`` where :attr:`caps` says that is possible. A
        refusal or a failure raises; the host keeps the message text and drops the exception, and then
        calls :meth:`close` (idempotent) so a failed open releases whatever it had built.

        ``on_released`` is how the driver REPORTS its release, from any thread (inside :meth:`open` or
        :meth:`close` included; the host holds none of its locks while driver code runs):

        * ``None`` as soon as nothing the conversation held is still live, WHATEVER the cause: a close, a
          failed open, or a harness that died on its own (a crashed process is released). Nothing may be
          reported after ``None``.
        * text (exactly ``str``) ONLY when something may still be live (a process that would not die),
          after any escalation of the driver's own (a kill after a stop) has failed. A failure may be
          followed by one ``None`` if the driver later confirms the release; that frees the slot.

        The host never asks: it keeps the conversation counted until a ``None`` report, and one with no
        report by its deadline reads ``release_failed`` until one comes. A report that is neither ``None``
        nor exactly ``str`` is a contract violation, recorded as a failed release.

        Acquire nothing before :meth:`open` (not in ``__init__``): a driver the host made but did not open
        (it declares a consent timing the host does not drive, or its caps cannot be read, or the factory
        answered too late) is dropped without :meth:`close`."""

    @abc.abstractmethod
    def close(self) -> None:
        """Start the release of everything the conversation holds (a shell, a process, a socket).
        Idempotent, and bounded: it never releases under a running turn, it stops the turn and waits for
        its return, and a release it could not wait for is made by whoever still holds the conversation
        when they let go (the turn on its return, an opener on its arrival, a native release still
        running). No new turn starts once it is called. The release's outcome is reported through
        ``on_released`` (:meth:`open`); a close that raises is recorded as a failed release unless a
        ``None`` report follows."""

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
"""How long :meth:`OpenHandsDriver.close` waits, in all, for a running turn to end and the session's release
to return, before it leaves the rest to that turn's return or the release thread. The SDK stops at a step
boundary, so a step longer than this outlives the wait."""


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
    the turn's own return, then releases (the turn is ended, not torn down). The stop requests and the
    native release each run on a thread of their own, so neither a blocking stop request nor a blocking
    native close holds :meth:`close` past ``close_wait`` (one bound for both). A turn still running when the
    wait runs out releases the session itself on its return: the session is released exactly once and
    the phase reads ``closed`` once that release has returned (:meth:`wait_closed`), either way."""

    entity_dir: Path
    opener: SessionOpener
    close_wait: float = DEFAULT_CLOSE_WAIT_SECONDS
    harness: str = field(default="openhands", init=False)
    caps: DriverCaps = field(default=DriverCaps(), init=False)
    _session: Any = field(default=None, init=False, repr=False)
    _phase: _Phase = field(default="new", init=False, repr=False)
    _running: object | None = field(default=None, init=False, repr=False)   # the running turn's guard token
    _halted: bool = field(default=False, init=False, repr=False)
    _turn_releases: bool = field(default=False, init=False, repr=False)   # close() handed the release to the turn
    _release_error: str | None = field(default=None, init=False, repr=False)   # text only (DriverCall)
    _worker: threading.Thread | None = field(default=None, init=False, repr=False)   # the release worker
    _handed: Any = field(default=_ABSENT, init=False, repr=False)         # what the worker is to release
    _on_released: Callable[[str | None], None] | None = field(default=None, init=False, repr=False)
    _cond: Any = field(default_factory=threading.Condition, init=False, repr=False)

    def __post_init__(self) -> None:
        wait = self.close_wait
        if isinstance(wait, bool) or not (isinstance(wait, (int, float)) and 0 < wait <= threading.TIMEOUT_MAX):
            raise ValueError("close_wait must be a finite number of seconds above 0: close() is bounded")

    def open(self, on_event: Callable[[DriverEvent], None], *, on_released: Callable[[str | None], None],
             resume: str | None = None) -> None:
        with self._cond:
            if self._on_released is None:
                self._on_released = on_released   # first, so a release of any open that began is reported
        if resume is not None:
            raise DriverUnsupported(
                "openhands conversations live in server memory only; resume across a restart is not offered")

        def _sink(line: str) -> None:
            on_event(DriverEvent("activity", line))

        # Every state change sits inside the try, so whatever raises at whatever point (the opener, or an
        # asynchronous BaseException between statements), the finally sees the phase this call took and
        # leaves the driver closed with anything built released, or published, never stuck at `opening`.
        began = published = False
        session: Any = None
        try:
            with self._cond:
                if self._phase != "new":
                    raise RuntimeError(f"the driver cannot open from {self._phase!r}: it opens once")
                began = True
                self._phase = "opening"
            # The release worker is acquired HERE, before anything exists for it to release (the kubelet is
            # there before the pod): no release ever has to create a thread, so none can fail to.
            worker = threading.Thread(target=self._release_worker, daemon=True, name="levain-driver-release")
            worker.start()
            with self._cond:
                self._worker = worker
            session = self.opener(self.entity_dir, on_event=_sink)
            with self._cond:
                if self._phase == "opening":
                    self._session, self._phase = session, "open"
                    published = True
        finally:
            if began and not published:
                # The opener raised, or close() ran while it built: this is the release either way.
                self._hand_release(session)
        if not published:
            raise RuntimeError("the driver was closed while it opened")

    def _release(self, session: Any) -> None:
        """Close ``session`` (the caller has already taken it off the driver) and end the phase at
        ``closed`` whatever happens. Every call into the session goes through :func:`_call_driver`. A failed
        close escalates once where the driver can (:meth:`_escalate_release`); one that still failed is NOT a
        release: it is logged, kept as text and REPORTED (``on_released``), so :meth:`wait_closed` stays ``False``, :meth:`release_error`
        says why, and :meth:`close` raises it when it is still waiting (this runs on the release worker,
        where nothing else would see it)."""
        failure: str | None = "the release did not complete"     # fail closed: cleared only by a release
        try:
            if session is None:
                failure = None
            else:
                closed = _call_driver(lambda: session.close())     # the lookup is the session's code too
                failure = None if closed.ok else f"close: {closed.error}"
                if failure is None:
                    # EntitySession.close never raises; a failed release is recorded on it instead, and a
                    # close that returned normally is not a release until that says so (codex L3 r5).
                    said = _call_driver(lambda: getattr(session, "release_error", None))
                    if not said.ok:
                        failure = f"close: release_error could not be read: {said.error}"
                    elif said.value is not None:
                        failure = (f"close: {said.value}" if type(said.value) is str
                                   else "close: release_error is not text")
                if failure is not None:
                    forced = _call_driver(self._escalate_release, session)
                    if forced.ok and forced.value is True:
                        failure = None
                    elif not forced.ok:
                        failure = f"{failure}; escalation: {forced.error}"
        finally:
            with self._cond:
                if failure is not None:
                    _log.error("openhands driver: releasing the session failed: %s", failure)
                    self._release_error = failure
                self._phase = "closed"
                self._cond.notify_all()
                report = self._on_released
            if report is not None:
                # The host's sink: host code, but called through the boundary all the same, so nothing it
                # raises ends this worker before the phase above is published.
                reported = _call_driver(report, failure)
                if not reported.ok:
                    _log.error("openhands driver: reporting the release failed: %s", reported.error)

    def _escalate_release(self, session: Any) -> bool:
        """The escalation past a graceful close of ``session`` that failed (for a process, the kill after
        the stop), run once, on the release worker, never by a host. ``True``: escalated and released.
        ``False``: there is no escalation, and the failure stands. A raise: the escalation failed too. An
        :class:`~levain.session.EntitySession` has nothing further to kill (its close never raises), so
        OpenHands has none; a subclass that holds a process overrides this."""
        return False

    def _hand_release(self, session: Any) -> None:
        """Hand ``session`` to the release worker: a native close may block (a process or socket that will
        not shut), and neither close() nor a turn's return may wait on it past its bound. Without a worker
        (open() never got that far) there is nothing native to release, and the driver is marked closed here."""
        with self._cond:
            if self._worker is not None:
                if self._handed is _ABSENT:      # one release per driver: a later hand-off finds it in flight
                    self._handed = session
                    self._cond.notify_all()
                return
        self._release(session)      # session is None here: the opener runs only after the worker started

    def _release_worker(self) -> None:
        """The driver's one release, run on the thread open() started. It exits once the phase reaches
        ``closed``: after the release it was handed, or at once when the driver closed with nothing handed
        to it (an open that failed before it could record the worker)."""
        with self._cond:
            self._cond.wait_for(lambda: self._handed is not _ABSENT or self._phase == "closed")
            session = self._handed
        if session is not _ABSENT:
            self._release(session)

    def wait_closed(self, timeout: float | None) -> bool:
        """``True`` once the release is confirmed, waiting at most ``timeout`` seconds. For a caller that holds
        this driver directly; the chat host is told through ``on_released`` and never asks."""
        with self._cond:
            return self._cond.wait_for(lambda: self._phase == "closed", timeout=timeout) \
                and self._release_error is None

    def release_error(self) -> str | None:
        """Why the release failed, or ``None`` (not failed, or not finished). What ``on_released`` reported."""
        with self._cond:
            return self._release_error if self._phase == "closed" else None

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
        turn = object()     # the guard names THIS turn: set in one assignment, cleared only if still ours
        snap: TurnSnapshot | None = None
        try:
            with self._cond:
                if self._phase != "open":
                    raise RuntimeError("the driver is not open")
                if self._running:
                    raise RuntimeError("a turn is already running on this driver")
                session = self._session
                self._running = turn
            snap = read_outcome(call(session))    # the turn's one terminal record, read once
            return snap
        finally:
            self._end_turn(turn, snap)

    def _end_turn(self, turn: object, snap: TurnSnapshot | None) -> None:
        """The end of ``turn``, reached on any raise (BaseException included). One hold releases the guard
        and sets the state from the snapshot being returned; no snapshot (the call or the read raised) reads
        as held: what the harness holds is unknown, so fail closed. A turn that never took the guard (it
        was refused, or a raise landed before the assignment) changes nothing."""
        owned: Any = None
        with self._cond:
            if self._running is not turn:
                return
            self._running = None
            self._halted = snap.gated if snap is not None else True
            release = self._turn_releases
            if release:
                owned, self._session = self._session, None
            self._cond.notify_all()
        if release:
            # The turn's own outcome is what this call returns (or raises): the release runs on its own
            # thread, logs its own failure and never replaces or delays that outcome.
            self._hand_release(owned)

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
        # As in open(): every state change inside the try, so an asynchronous raise between two statements
        # still reaches the finally that hands the session to whoever releases it.
        took = False
        owned: Any = None
        deadline = time.monotonic() + self.close_wait     # one bound for the turn's end and the release
        try:
            with self._cond:
                if self._phase in ("new", "closed"):
                    # From "new" nothing was built, and an open that registered a sink before refusing (a
                    # resume) is told so: the release is reported exactly once on every path (L1 r5).
                    report = self._on_released if self._phase == "new" else None
                    self._phase = "closed"
                    self._cond.notify_all()
                elif self._phase in ("opening", "closing"):
                    # an opener in flight releases its own session on arrival (open()); another closer or a
                    # turn is mid-release. Either way, wait (bounded) until the release is done.
                    if self._phase == "opening":
                        self._phase = "closing"
                    self._cond.wait_for(lambda: self._phase == "closed", timeout=self.close_wait)
                    return
                else:
                    report = _ABSENT
                    took = True
                    self._phase = "closing"      # no new turn can start from here
                    running = self._running
            if report is not _ABSENT:
                if report is not None:
                    _call_driver(report, None)
                return
            if running:
                # The stop requests run on their own thread: one may block (the SDK's pause waits for a
                # step's state lock), and close() waits only on the turn guard, with a deadline.
                threading.Thread(target=self._stop_until_ended, daemon=True,
                                 name="levain-driver-stop").start()
            with self._cond:
                self._cond.wait_for(lambda: not self._running, timeout=max(0.0, deadline - time.monotonic()))
        finally:
            mine = False
            # Reached when the wait runs out AND on a raise (a stopper that could not start, an interrupt
            # of this thread): whoever holds the session releases it, never both and never under a
            # running turn.
            if took:
                with self._cond:
                    mine = not self._running
                    if mine:
                        owned, self._session = self._session, None
                    else:
                        self._turn_releases = True   # _run's own return releases it
                if mine:
                    self._hand_release(owned)
        if mine and not self.wait_closed(max(0.0, deadline - time.monotonic())):
            # What is left of the bound went to the release. Still in flight: the release worker finishes
            # it. Failed within the bound: this call reports it (the driver stays unreleased).
            with self._cond:
                failed = self._release_error if self._phase == "closed" else None
            if failed is not None:
                raise RuntimeError(f"the session's release failed: {failed}")

    def _stop_until_ended(self) -> None:
        """Ask the running turn to stop, about once a second, until it ends (a stop request landing before
        the run loop starts is undone by it). Outlives a close() that stopped waiting: the turn still
        has to end for its own return to release the session."""
        while True:
            with self._cond:
                if not self._running:
                    return
            stopped = _call_driver(self.interrupt)      # keep asking; the turn's own return is what ends this
            if not stopped.ok:
                _log.error("openhands driver: stop request failed: %s", stopped.error)
            with self._cond:
                if self._running:
                    self._cond.wait(1.0)

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

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
* ``ok`` and ``exit_code`` never contradict the outcome they come with: the snapshot derives them from it
  in :attr:`levain.session.TurnResult.exit_code`'s order (timeout 5, error 3, gated 4, no reply 1, an
  unreadable call 7, else 0; ``ok`` exactly at 0), and a harness whose own claims differ is refused.
  A client that read ``ok`` alone would otherwise show a call that never ran as the entity's answer.
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
import os
import platform
import signal
import subprocess
import functools
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Literal, Protocol, Sequence, runtime_checkable

__all__ = [
    "ApprovalTiming",
    "CA_REMINT_MARGIN",
    "CHAT_HARNESSES",
    "CLI_GROUP_WAIT_S",
    "CLI_TERM_WAIT_S",
    "ChatEgressService",
    "ChatLaunch",
    "CliEnd",
    "DEFAULT_CLOSE_WAIT_SECONDS",
    "DriverCaps",
    "DriverContractError",
    "DriverEvent",
    "DriverState",
    "DriverUnsupported",
    "HarnessDriver",
    "LaunchEnd",
    "OpenHandsDriver",
    "PendingApproval",
    "RENEW_WAIT_S",
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


# levain.session's exit codes, mirrored so this module stays harness-agnostic (it imports no session
# code); test_the_snapshot_codes_are_sessions pins them equal.
_EXIT_OK = 0
_EXIT_NO_REPLY = 1
_EXIT_TURN_FAILED = 3
_EXIT_GATED = 4
_EXIT_TIMEOUT = 5
_EXIT_UNREADABLE_CALL = 7


def _outcome_code(*, reply: str | None, error: str | None, gated: bool, timed_out: bool,
                  unreadable_call: bool) -> tuple[bool, int]:
    """THE one derivation of a turn's ``(ok, exit_code)`` from its outcome, in
    :attr:`levain.session.TurnResult.exit_code`'s order: timeout 5, error 3, gated 4, no reply 1,
    unreadable call 7, else 0; ``ok`` exactly when the code is 0 (verify r2, codex M5)."""
    if timed_out:
        code = _EXIT_TIMEOUT
    elif error is not None:
        code = _EXIT_TURN_FAILED
    elif gated:
        code = _EXIT_GATED
    elif not reply:
        code = _EXIT_NO_REPLY
    else:
        code = _EXIT_UNREADABLE_CALL if unreadable_call else _EXIT_OK
    return code == _EXIT_OK, code


@dataclass(frozen=True)
class TurnSnapshot:
    """A turn's outcome, built ONCE by the driver and immutable: the one terminal record of a turn (ACP's
    single stop reason, Codex's ``turn/completed``). Every guarantee of the contract (module docstring) is
    checked here, at construction, so a snapshot that exists keeps them; the host serialises this same
    object, and nothing of the outcome is read a second time (the live :meth:`~HarnessDriver.held_digest`
    is re-read at approve on purpose, to compare with the one recorded here). ``ok`` and ``exit_code`` are not
    passed: they are DERIVED from the outcome by :func:`_outcome_code`, so they cannot contradict it; a harness's
    own claims are compared with them in :func:`read_outcome` (verify r2, codex M5)."""

    reply: str | None
    tool_activity: tuple[str, ...]
    error: str | None
    nudged: bool
    gated: bool
    timed_out: bool
    pending: tuple[PendingApproval, ...]
    held_digest: str | None
    unreadable_call: bool
    unreadable_unchecked: bool = False
    """The harness could not check whether ``reply`` is an unreadable tool call. It requires
    ``unreadable_call`` (fail closed: the reply is not shown as an answer); a client shows its own notice."""
    ok: bool = field(init=False)
    exit_code: int = field(init=False)

    def __post_init__(self) -> None:
        for name in ("reply", "error", "held_digest"):
            if getattr(self, name) is not None and not _is_text(getattr(self, name)):
                raise DriverContractError(f"the outcome's `{name}` is neither text nor None")
        for name in ("nudged", "gated", "timed_out", "unreadable_call", "unreadable_unchecked"):
            if not isinstance(getattr(self, name), bool):
                raise DriverContractError(
                    f"the outcome's `{name}` is not a bool (a method or a value where a flag was meant)")
        if self.unreadable_unchecked and not self.unreadable_call:
            # Fail closed: a reply nobody could check is never shown as the entity's answer (codex, closing pass)
            raise DriverContractError("the outcome's reply is `unreadable_unchecked` but not `unreadable_call`")
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
        ok, code = _outcome_code(reply=self.reply, error=self.error, gated=self.gated,
                                 timed_out=self.timed_out, unreadable_call=self.unreadable_call)
        object.__setattr__(self, "ok", ok)
        object.__setattr__(self, "exit_code", code)
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
    snap = TurnSnapshot(
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
    )
    # The harness's own claims are compared, never carried: a claim that is not the derived value is a
    # driver that classifies its turns differently from the host (verify r2, codex M5)
    if (type(got["ok"]) is not bool or type(got["exit_code"]) is not int
            or (got["ok"], got["exit_code"]) != (snap.ok, snap.exit_code)):
        # Shown only when exactly bool / int: anything else is driver code, whose repr could raise
        said = [repr(v) if type(v) in (bool, int) else f"<{type(v).__name__}>" for v in (got["ok"], got["exit_code"])]
        raise DriverContractError(
            f"the outcome claims `ok` {said[0]}, `exit_code` {said[1]}, but its outcome is "
            f"`ok` {snap.ok}, `exit_code` {snap.exit_code} (timeout 5, error 3, gated 4, no reply 1, unreadable "
            "call 7, else 0): a claimed `ok` would let a client show it as the entity's answer")
    return snap


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
        (it declares a consent timing the host does not drive, or its name or caps cannot be read in time) is
        dropped without :meth:`close`. One the factory answered after the host gave up is closed anyway, on its
        own lane, since nobody holds it."""

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


# -- the chat launch's egress: the driver's call sites into levain.chat_egress (CH-D2) ----------------------------

CLI_TERM_WAIT_S = 5.0
"""How long :meth:`ChatLaunch.end` waits for the CLI leader to exit on SIGTERM before it SIGKILLs it, and again for
the SIGKILLed leader to exit. [judged by lane-chatdriver, 10-25, against a CLI's own shutdown on SIGTERM]"""

CLI_GROUP_WAIT_S = 5.0
"""How long :meth:`ChatLaunch.end` then waits for the CLI's process group to hold no live member."""


@dataclass(frozen=True)
class CliEnd:
    exit_code: int | None       # the leader's, once seen exited (negative: the signal); None: not seen exited
    group_gone: bool            # no live member was left in its process group within CLI_GROUP_WAIT_S
    reaped: bool = False        # the leader reaped by the launch, which it is only once its group is gone
    reaped_outside: bool = False  # reaped by other code (attach_cli's contract broken): nothing more is safe to send,
                                  # so the teardown is over without a group known gone (chatdriver r2 item 3)


@dataclass(frozen=True)
class LaunchEnd:
    """What :meth:`ChatLaunch.end` came to, step by step (each step is attempted whatever an earlier one raised)."""
    cli: CliEnd | None              # None: no CLI was attached, or its teardown raised (named in ``failures``)
    egress: Any                     # ``chat_egress.launch.StopResult`` (the last one returned), or None when
                                    # ``stop_egress`` raised
    tmp_leftovers: tuple[str, ...]  # what ``tmp_clean.remove_tree`` could not remove from ``chat_tmp`` (empty: gone);
                                    # ``(chat_tmp,)`` when it was kept, the CLI's group not confirmed gone
    failures: tuple[str, ...] = ()  # "<step>: <exception type or reason>" for each step that failed
    complete: bool = False          # the CLI teardown (if a CLI was attached), stop_egress and the chat_tmp step all
                                    # done: end() returns this from now on; False: end() must be called again


def _remove_chat_tmp(chat_tmp: Path) -> list[str]:
    from levain.chat_egress import tmp_clean

    try:
        return tmp_clean.remove_tree(chat_tmp)
    except Exception as exc:  # noqa: BLE001 - a leftover, named, never a crash of the launch end
        _log.warning("chat_tmp removal failed: %s", type(exc).__name__)
        return [f"{chat_tmp}: {type(exc).__name__}"]


def _not_a_stop(stop: Any) -> list[str]:
    """Why a ``StopResult`` is not a stop (chatint L3 r1 item 2): ``stop_egress`` catches each step's failure into it,
    so its return alone proves nothing. Empty only when no step failed, the decider and the SOCKS refuser closed clean
    and the glue was seen to exit."""
    why = [f"egress {f}" for f in stop.failures]
    if not stop.decider_clean:
        why.append("egress: the decider did not close clean")
    if not stop.socks_clean:
        why.append("egress: the SOCKS refuser did not close clean")
    if stop.glue_exit is None:
        why.append("egress: the glue was not seen to exit")
    return why


def _leads_group(pid: int) -> bool:
    """Whether ``pid``, a child of this process not yet reaped, leads its own process group. A zombie's ``getpgid``
    is refused on macOS (ESRCH, measured), so a leader that already exited is known by its group instead: an unreaped
    pid cannot be reused, so a group with its id is the one it led (``killpg(pid, 0)`` then finds the zombie, EPERM
    on macOS, or a live member)."""
    try:
        return os.getpgid(pid) == pid
    except ProcessLookupError:
        pass
    try:
        os.killpg(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass
    return True


def _send(send: Callable[[], None]) -> OSError | None:
    """One signal; the OSError it raised, or None. ESRCH is None: there was nothing left to signal."""
    try:
        send()
    except ProcessLookupError:
        return None
    except OSError as exc:
        return exc
    return None


_REAP_RAN = "_chat_reap_ran"
"""The attribute :func:`_end_cli` sets on its leader before it calls ``reap``: the launch's own reap was attempted.
``confinement._Leader.reaped`` is set only after its ``Popen.wait`` returns, so a reap that raised after the kernel
reap leaves it False with ``Popen.returncode`` set; this flag tells that reap from one outside the launch (chatint L3 r1 item 3)."""


def _release_watch(leader: Any, failures: list[str], *, reaped_outside: bool) -> None:
    """Close the leader's watch (kqueue / pidfd) once nothing more is read from it (chatint L3 r1 item 4), so no
    launch leaves a descriptor open. An exited leader kept unreaped: ``release_watch``. One reaped outside the launch:
    ``reap``, which for a ``Popen`` already holding its returncode makes no wait syscall (``Popen.wait`` returns that
    returncode); it marks the leader reaped and closes the watch under its reap lock."""
    try:
        if reaped_outside:
            leader.reap(timeout=0)
        else:
            leader.release_watch()
    except Exception as exc:  # noqa: BLE001 - named; a watch left open is a descriptor, never a wrong signal
        failures.append(f"cli watch release: {type(exc).__name__}")


def _end_cli(leader: Any, failures: list[str]) -> CliEnd:
    """C11, on ``confinement._Leader`` (chatdriver L3 r1 FIX 1): the leader alone gets SIGTERM, then SIGKILL past
    :data:`CLI_TERM_WAIT_S`, and its exit is read WITHOUT reaping it; its group is SIGKILLed while the zombie leader
    still reserves the group's number (an unreaped pid is never reused, so the signal can reach no other group), then
    polled by ``confinement.group_gone_proc`` (which counts no zombie, on Linux from /proc, and answers live whenever
    its view may be incomplete); the leader is reaped LAST, and only once its group is gone. Every substep is
    attempted; what failed is appended to ``failures``. A group kill refused with EPERM is not a failure when the
    poll then finds the group gone: macOS answers EPERM for a group holding only zombies (measured), which is the
    clean case here, the unreaped leader being one.

    The leader is never signalled after a reap: this is the only reap, under :class:`ChatLaunch`'s lock; a leader it
    already reaped is answered from what the reap saw (chatdriver r2 item 1: its number may be another process's
    now), and a leader some other code reaped (the :meth:`ChatLaunch.attach_cli` contract broken) gets no signal
    at all. "Reaped outside" is decided from the launch's own state, never from ``Popen.returncode`` alone: a
    returncode that appears once this function attempted its reap is that reap's (:data:`_REAP_RAN`)."""
    from levain.firing.confinement import _proc_hides_pids, group_gone_proc

    proc, pid = leader.proc, leader.pid                 # pid == its pgid: attach_cli checked it leads its own group
    if leader.reaped:                                   # only this function reaps it, once its group was gone
        return CliEnd(exit_code=leader.status, group_gone=True, reaped=True)
    if getattr(leader, _REAP_RAN, False) and proc.returncode is not None:
        # our reap ran (it is attempted only once the group was seen gone) and raised after the kernel reap: the
        # returncode is that reap's, never one outside (chatint L3 r1 item 3); that reap may have raised before it
        # closed the watch, so the watch is released here (verify r2 item a)
        _release_watch(leader, failures, reaped_outside=False)
        return CliEnd(exit_code=leader.status if leader.status is not None else proc.returncode, group_gone=True,
                      reaped=True)
    if proc.returncode is not None:                     # only a refusal to signal: its number may be another's now;
                                                        # a permanent verdict, ChatLaunch.end keeps chat_tmp for good
        failures.append("cli: reaped outside the launch; chat_tmp kept")
        _log.warning("chat launch end: the CLI was reaped outside the launch; its group is not signalled")
        _release_watch(leader, failures, reaped_outside=True)
        return CliEnd(exit_code=proc.returncode, group_gone=False, reaped_outside=True)

    def exited(timeout: float) -> bool:
        try:
            return bool(leader.wait(timeout))
        except OSError as exc:
            failures.append(f"cli wait: {type(exc).__name__}")
            return False

    def note(step: str, exc: OSError | None) -> None:
        if exc is not None:
            failures.append(f"cli {step}: {type(exc).__name__}")

    if not exited(0):
        note("term", _send(lambda: os.kill(pid, signal.SIGTERM)))
        if not exited(CLI_TERM_WAIT_S):
            note("kill", _send(lambda: os.kill(pid, signal.SIGKILL)))
            exited(CLI_TERM_WAIT_S)
    # ALWAYS, before the gone poll, whether or not the leader exited on TERM (chatint L3 r1 item 1): the /proc scan
    # reads one entry at a time, so a member that keeps forking can slip past it. A SIGKILLed group cannot fork, so
    # two clean scans after this kill are sound.
    refused = _send(lambda: os.killpg(pid, signal.SIGKILL))
    try:
        gone = group_gone_proc(pid, timeout=CLI_GROUP_WAIT_S)
    except Exception as exc:  # noqa: BLE001 - named; the group is then not known gone
        failures.append(f"cli group poll: {type(exc).__name__}")
        gone = False
    if not gone:
        note("killpg", refused)
        try:
            hidden = platform.system() != "Darwin" and _proc_hides_pids()
        except Exception:  # noqa: BLE001 - the cause is then unnamed; the group is still not known gone
            hidden = False
        if hidden:                                      # group_live_proc answers live for every group then
            failures.append("cli group poll: /proc may hide members: chat_tmp kept")
    reaped = False
    if gone and leader.exited:
        setattr(leader, _REAP_RAN, True)                # before the reap: a returncode seen from here on is this reap's
        try:
            reaped = bool(leader.reap(timeout=CLI_TERM_WAIT_S))
        except Exception as exc:  # noqa: BLE001
            failures.append(f"cli reap: {type(exc).__name__}")
    elif leader.exited:                                 # kept unreaped for a retry: its exit is known
        _release_watch(leader, failures, reaped_outside=False)
    return CliEnd(exit_code=leader.status, group_gone=gone, reaped=reaped)


@dataclass
class ChatLaunch:
    """One chat launch as the driver holds it: its egress (``chat_egress.launch.EgressLaunch``), its ``chat_tmp``
    (the launch's private write root, design §3.3), its ``trust_env`` (``chat_egress.ca.trust_env`` of the CA its egress
    was started with: the names its chat env carries) and, once attached, its CLI child. :meth:`end` releases them."""

    egress: Any
    chat_tmp: Path
    trust_env: dict[str, str] = field(default_factory=dict)
    _cli: Any = field(default=None, init=False, repr=False)       # confinement._Leader over the CLI's Popen
    _ending: bool = field(default=False, init=False, repr=False)  # end() has been called
    _ended: LaunchEnd | None = field(default=None, init=False, repr=False)    # set only by a complete end
    _stopped: bool = field(default=False, init=False, repr=False)             # it returned a clean stop
    _stop: Any = field(default=None, init=False, repr=False)                  # what it returned
    _cli_end: CliEnd | None = field(default=None, init=False, repr=False)     # the CLI teardown's, once finished
    _cli_failures: tuple[str, ...] = field(default=(), init=False, repr=False)  # what that finishing call named
    _tmp: tuple[str, ...] | None = field(default=None, init=False, repr=False)  # chat_tmp's leftovers, once done
    _lock: Any = field(default_factory=threading.Lock, init=False, repr=False)

    def attach_cli(self, proc: subprocess.Popen) -> None:
        """Hand the launch its CLI child, so :meth:`end` ends it. It must lead its own process group
        (``start_new_session=True``): its group is what :meth:`end` kills. Raises ValueError for one that does not or
        is already reaped (its pid may be another process's by now), RuntimeError for a second CLI or a launch whose
        :meth:`end` has been called.

        From here the launch owns the CLI's reap (chatdriver L3 r1 FIX 2): nothing else may reap it, so its pid stays
        its own until :meth:`end` has seen its group gone. ``proc.wait``, ``poll``, ``communicate``, ``terminate``,
        ``kill`` and ``send_signal`` each may reap it; use :meth:`cli_exit` to watch it. Enforced as far as a Popen
        allows: :meth:`end` finds a leader reaped elsewhere by its ``returncode`` and then signals nothing."""
        from levain.firing.confinement import _Leader

        with self._lock:
            if self._ending:
                raise RuntimeError("the launch has ended: nothing would reap this CLI")
            if self._cli is not None:
                raise RuntimeError("the launch already has its CLI")
            if proc.returncode is not None:
                raise ValueError("the CLI is already reaped: its pid may be another process's now")
            if not _leads_group(proc.pid):
                raise ValueError("the CLI must lead its own process group (start_new_session=True), unreaped")
            self._cli = _Leader(proc)

    def cli_exit(self, timeout: float = 0.0) -> int | None:
        """The attached CLI's exit status (``Popen.returncode`` style) once its watch has seen it exit, waiting up to
        ``timeout`` seconds, read WITHOUT reaping it; after :meth:`end` reaped it, the status the reap read. None
        while it runs and when none is attached. On macOS, None also for a CLI that had already exited, unreaped,
        when it was attached (:meth:`attach_cli` refuses only a reaped one): the kqueue watch is set after that exit
        and never sees it, so its status is read only by :meth:`end`'s reap (chatdriver r2 item 5). An OSError or
        ValueError from ``confinement._Leader.wait`` is not caught here."""
        leader = self._cli
        if leader is None or not leader.wait(timeout):
            return None
        return leader.status

    def end(self) -> LaunchEnd:
        """The launch end, in order: the CLI (:func:`_end_cli`, C11), then ``stop_egress`` (its client is gone, so
        it drains), then ``chat_tmp`` removed by ``tmp_clean.remove_tree``, ONLY once the CLI's group is confirmed gone
        and its leader reaped (chatdriver L3 r1 FIX 3) AND ``stop_egress`` has returned a clean stop (chatint 5a: a
        glue still running may write into it; chatint L3 r1 item 2: :func:`_not_a_stop`), so nothing is removed under
        a writer. Every step is attempted whatever an earlier one raised.

        Retryable until complete (chatdriver r2 item 2): the end is complete only once all three parts are done, the
        CLI teardown finished (its leader reaped, or found reaped outside the launch: a permanent verdict, chat_tmp
        then kept for good, chatdriver r2 item 3), ``stop_egress`` returned a clean stop, and the ``chat_tmp`` step
        ran. Each part is done once and its result kept; a later call runs only the parts not done, so a reaped leader
        is never signalled again and ``chat_tmp`` is removed once. While the CLI teardown has not finished, or
        ``stop_egress`` has not returned a clean stop (what was not clean is named in ``failures``), ``chat_tmp`` is
        kept and named in ``tmp_leftovers``, and the next call retries what is not done (a leader not yet reaped keeps
        its group's number its own). The returned end describes every part as it stands: a done part's result and the
        failures named by the call that finished it, the others as this call found them. A complete end is returned
        by every later call.

        It blocks, up to the sum of its bounds (chatint L3 r1 item 7): :data:`CLI_TERM_WAIT_S` for the TERM, again
        for the KILL and again for the reap; :data:`CLI_GROUP_WAIT_S` for the group poll, plus the probe running at
        its deadline; then ``stop_egress``'s: ``launch.GLUE_EXIT_TIMEOUT_S`` for the glue, again after its kill, the
        decider's close bound and the SOCKS refuser's; then ``chat_tmp``'s removal, which has no bound. A second
        call also waits for the first, on the launch's lock. Never call it on an event loop: run it on a worker
        thread (``asyncio.to_thread``)."""
        from levain.chat_egress import launch as lm

        with self._lock:
            if self._ended is not None:
                return self._ended
            self._ending = True
            cli, cli_failures = self._cli_end, list(self._cli_failures)
            if self._cli is not None and cli is None:
                cli_failures = []
                try:
                    cli = _end_cli(self._cli, cli_failures)
                except Exception as exc:  # noqa: BLE001 - named, and the egress still ends
                    cli_failures.append(f"cli: {type(exc).__name__}")
                    _log.warning("chat launch end: the CLI teardown failed: %s", type(exc).__name__)
                if cli is not None and (cli.reaped or cli.reaped_outside):
                    self._cli_end, self._cli_failures = cli, tuple(cli_failures)
            egress_failures: list[str] = []
            if not self._stopped:
                try:
                    self._stop = lm.stop_egress(self.egress)
                    egress_failures = _not_a_stop(self._stop)
                except Exception as exc:  # noqa: BLE001
                    self._stop = None
                    egress_failures = [f"egress: {type(exc).__name__}"]
                    _log.warning("chat launch end: stop_egress failed: %s", type(exc).__name__)
                else:
                    self._stopped = not egress_failures
                    if egress_failures:
                        _log.warning("chat launch end: the egress is not confirmed stopped: %s", egress_failures)
            cli_done = self._cli is None or self._cli_end is not None
            tmp_failures: list[str] = []
            if self._tmp is None:
                if self._cli_end is not None and self._cli_end.reaped_outside:
                    self._tmp = (str(self.chat_tmp),)     # its group was never confirmed gone, and never can be
                elif cli_done and self._stopped:
                    self._tmp = tuple(_remove_chat_tmp(self.chat_tmp))
                else:
                    if cli_done:
                        why = "the egress is not confirmed stopped"
                    elif cli is None:                  # the teardown raised: no group verdict exists
                        why = "the CLI teardown did not finish"
                    else:
                        why = "the CLI's process group is not confirmed gone"
                    tmp_failures.append(f"chat_tmp: kept, {why}")
                    _log.warning("chat launch end: chat_tmp kept, %s", why)
            complete = cli_done and self._stopped and self._tmp is not None
            out = LaunchEnd(cli=cli, egress=self._stop,
                            tmp_leftovers=self._tmp if self._tmp is not None else (str(self.chat_tmp),),
                            failures=tuple(cli_failures + egress_failures + tmp_failures), complete=complete)
            if complete:
                self._ended = out
            return out


RENEW_WAIT_S = 10.0
"""How long a launch's renew-due callback waits for another launch's in-flight ping of the same record before it
takes that send over. [judged by lane-cdfix, 10-25, against notify spawning a detached sender]"""

CHAT_HARNESSES = ("claude-code", "codex")
"""The harnesses a chat launch names; each has its own consent adapter settings file, which its renew ping reads
(``consent.adapters.claude_code`` / ``consent.adapters.codex`` ``load_settings``)."""

CA_REMINT_MARGIN = timedelta(days=1)
"""A launch this close to the service CA's ``not_valid_after`` (or past it) gets a newly minted CA
(``1025-23/chatdriver/RULINGS_OBS.md`` 1: "within 1 day of expiry")."""


def renew_message(due: Any) -> str:
    """The operator's renew-due ping for one ``chat_egress.renew.RenewDue``."""
    when = due.expires_at.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    return (f"levain: the chat key for {due.host} (record {due.record_id}) expires {when}; renew it with "
            "`levain chat key setup --replace`.")


class ChatEgressService:
    """The chat egress of one chat-service start (CH-D2 S8's call sites): ONE CA, minted here by
    ``chat_egress.ca.new_egress_ca`` and handed to every launch (never one per launch, never reused across starts;
    re-minted by the first launch within :data:`CA_REMINT_MARGIN` of its expiry, see :meth:`_ca_for`),
    the srt install every launch verifies (``start_egress`` runs ``srt_pin.check`` over ``srt_prefix`` itself), and
    19a's renew-due ping (:meth:`renew_due`). :meth:`close` ends the start and removes its CA certs."""

    def __init__(self, *, state_dir: Path, store: Any, queue_dir: Path, node: str = "node",
                 srt_prefix: Path | None = None, now: datetime | None = None) -> None:
        from levain.chat_egress import ca, srt_pin

        self.store = store
        self.queue_dir = Path(queue_dir)
        self.node = node
        self.srt_prefix = Path(srt_prefix) if srt_prefix is not None else srt_pin.default_prefix()
        self.state_dir = Path(state_dir)
        self.ca = ca.new_egress_ca(self.state_dir, now=now or datetime.now(timezone.utc))
        self._certs: list[Path] = [self.ca.cert_path]                    # every cert this start minted
        self._ca_lock = threading.Lock()
        self._closed = False
        self._pinged: set[tuple[str, datetime]] = set()                    # notify returned success
        self._in_flight: dict[tuple[str, datetime], threading.Event] = {}  # a launch is sending it now
        self._lock = threading.Lock()

    def launch(self, *, launch_id: str, chat_tmp: Path, events: Path, routes: Any, liveness: Any, credentials: Any,
               cards: Any, mode: Any, authority: Any, harness: str, limits: Any = None, seams: Any = None,
               now: datetime | None = None) -> ChatLaunch:
        """Start one launch's egress with this service's CA and srt install. ``chat_tmp`` is the launch's from this
        call: removed at :meth:`ChatLaunch.end`, or here, before the refusal propagates, when the start refuses.
        ``authority`` (the replay ``routes`` were built from) is required: ``start_egress`` reads the renew-due
        records from it and refuses a None. ``harness`` (one of :data:`CHAT_HARNESSES`, else a ValueError) is the
        harness that drives the chat: its renew ping reads that harness's settings (:meth:`renew_due`)."""
        from levain.chat_egress import launch as lm

        chat_tmp = Path(chat_tmp)
        extra = {} if limits is None else {"limits": limits}
        try:
            if harness not in CHAT_HARNESSES:
                raise ValueError(f"no chat harness {harness!r}: one of {', '.join(CHAT_HARNESSES)}")
            the_ca = self._ca_for(now or datetime.now(timezone.utc))
            ca_pem = the_ca.glue_pem()
            egress = lm.start_egress(store=self.store, launch_id=launch_id, node=self.node,
                                     srt_prefix=self.srt_prefix, events=events, ca_pem=ca_pem,
                                     routes=routes, liveness=liveness, credentials=credentials, cards=cards,
                                     mode=mode, seams=seams, authority=authority,
                                     on_renew_due=functools.partial(self.renew_due, harness=harness),
                                     now=now, **extra)
        except BaseException:
            _remove_chat_tmp(chat_tmp)
            raise
        return ChatLaunch(egress=egress, chat_tmp=chat_tmp, trust_env=the_ca.trust_env())

    def _ca_for(self, now: datetime) -> Any:
        """The CA a launch at ``now`` gets: this start's, first re-minted when ``now`` is within
        :data:`CA_REMINT_MARGIN` of its ``not_valid_after``. The earlier cert stays on disk (``prune=False``):
        launches started on it may still be running. A naive ``now`` is a ValueError."""
        from levain.chat_egress import ca

        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError(f"the CA check needs an aware now, got {now!r}")
        with self._ca_lock:
            if self._closed:
                raise RuntimeError("the chat egress service is closed: its CA certs are removed")
            if now >= self.ca.not_valid_after - CA_REMINT_MARGIN:
                self.ca = ca.new_egress_ca(self.state_dir, now=now, prune=False)
                self._certs.append(self.ca.cert_path)
            return self.ca

    def close(self) -> tuple[str, ...]:
        """End this start: remove every CA cert it minted (re-minted ones too) and refuse any later launch. Call
        it once every launch has ended, since a running launch's chat env trusts its cert. Idempotent; the certs
        that could not be removed are returned (and logged), and a later call tries them again."""
        with self._ca_lock:
            self._closed = True
            left = []
            for cert in self._certs:
                try:
                    cert.unlink(missing_ok=True)       # unlink removes a symlink itself, never its target
                except OSError as exc:
                    _log.warning("chat egress close: %s not removed: %s", cert, type(exc).__name__)
                    left.append(cert)
            self._certs = left
            return tuple(str(c) for c in left)

    def renew_due(self, launch_id: str, due: Sequence[Any], *, harness: str) -> None:
        """19a's ``on_renew_due``: ping the operator about each record due, through the consent gate's own path
        (``consent.adapters.claude_code.notify``: the notify command, the ping record, a push to a paired phone),
        ``kind`` ``renew``, ``ref`` ``renew:<record id>``, with the settings of the ``harness`` that drives the chat
        (``codex.json`` for ``codex``, ``claude_code.json`` for ``claude-code``; another name is a ValueError). Once
        per record and expiry for this service: a record pinged is not pinged again by a later launch (a renewed
        record's new expiry is a new ping). A ping with nowhere to go is not counted, so a phone paired later still
        gets the next launch's.

        A record counts as pinged only once ``notify`` returned success (chatdriver L3 r1 FIX 4). While one launch is
        sending it, the record is in flight: a concurrent launch waits for that send (up to :data:`RENEW_WAIT_S`)
        and rechecks, so it sends the ping itself when the first send failed, and takes the send over when it is
        still in flight past that wait (r2 item 4: the record may then be pinged twice). After :meth:`close` it logs and returns without pinging. Nothing ``load_settings``
        or ``notify`` raises leaves this callback: it is logged, and the record stays un-pinged for the next launch."""
        from levain.consent.adapters import claude_code, codex

        if harness not in CHAT_HARNESSES:
            raise ValueError(f"no chat harness {harness!r}: one of {', '.join(CHAT_HARNESSES)}")
        with self._ca_lock:
            closed = self._closed
        if closed:                                     # close() says every launch has ended: a late tick pings no one
            _log.info("renew-due for launch %s after the service closed: not pinged", launch_id)
            return
        adapter = codex if harness == "codex" else claude_code

        unloaded = object()
        settings: Any = unloaded
        for d in due:
            key = (d.record_id, d.expires_at)
            mine = self._claim_ping(key, launch_id)
            if mine is None:
                continue
            sent = False
            try:
                if settings is unloaded:
                    settings = adapter.load_settings(self.queue_dir)
                sent = bool(claude_code.notify(settings, renew_message(d), cwd=self.queue_dir, kind="renew",
                                               ref=f"renew:{d.record_id}"))
                if not sent:
                    _log.info("renew-due ping for %s (launch %s) had nowhere to go", d.record_id, launch_id)
            except Exception as exc:  # noqa: BLE001 - a ping failure never reaches start_egress
                _log.warning("renew-due ping for %s (launch %s) failed: %s", d.record_id, launch_id,
                             type(exc).__name__)
            finally:
                with self._lock:
                    if sent:
                        self._pinged.add(key)
                    if self._in_flight.get(key) is mine:   # a waiter may have taken a hung claim over
                        del self._in_flight[key]
                    mine.set()

    def _claim_ping(self, key: tuple[str, datetime], launch_id: str) -> threading.Event | None:
        """The claim (an Event, set when its send ends) when this caller is now the one sending ``key``'s ping;
        None when it was pinged. A send in flight is waited for, up to :data:`RENEW_WAIT_S`, then rechecked; one
        still in flight past that is taken over (chatdriver r2 item 4), so a hung ``notify`` never silences the
        record for the service's lifetime. A newer claim than the one waited for gets its own full wait."""
        claim = None
        deadline = 0.0
        while True:
            with self._lock:
                if key in self._pinged:
                    return None
                sending = self._in_flight.get(key)
                if sending is None or (sending is claim and time.monotonic() >= deadline):
                    if sending is not None:
                        _log.info("renew-due ping for %s (launch %s): another launch's send is still in flight "
                                  "after %ss; sending it here", key[0], launch_id, RENEW_WAIT_S)
                    mine = self._in_flight[key] = threading.Event()
                    return mine
                if sending is not claim:
                    claim, deadline = sending, time.monotonic() + RENEW_WAIT_S
            sending.wait(max(0.0, deadline - time.monotonic()))

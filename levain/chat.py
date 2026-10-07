"""levain.chat — the CHAT HOST: entity conversations held in server memory (K1 part 2).

``levain serve --chat <entity>`` is the third surface over :class:`levain.session.EntitySession`
(after the REPL and ``levain run --task``), reached through the OpenHands driver. This module is the server-side half that has no HTTP in
it: a registry of live sessions, the propose→job→poll runtime that drives their turns off the
request thread, and the state machine that says which operations a session accepts right now.

**The host talks to a** :class:`~levain.chat_driver.HarnessDriver`, **never to a harness.** Each session
holds one driver, made by entity name (:class:`~levain.chat_driver.OpenHandsDriver` is today's
:class:`~levain.session.EntitySession` loop behind the contract). The job runtime, the
session cap, the deadline watcher and the decision id and digest an approval binds to stay here, because
they are the same whatever the harness is; how a session is opened, how a turn is sent and how the
harness's own consent surfaces are the driver's. A driver returns each outcome as one checked, immutable
:class:`~levain.chat_driver.TurnSnapshot`, and that object is what is recorded.

**The client never supplies an agent, a tool spec, a model or a mode.** It names an entity the
OPERATOR registered at startup, and it sends message text. Everything that shapes the agent comes
from the operator's command line and from :meth:`EntitySession.open`, which builds the agent itself
through ``build_entity_agent``. This is the K1-part-2 requirement of the approved binding design
(``spore-438``, Phill 2026-10-02): the crown-jewels floor lives in the hands' tool-spec params, so a
client-supplied spec would be a client writing its own floor. There is no code path here that
accepts one, which is a stronger claim than a validator that refuses one.

**The drive mode is ``headless``, decided by the server, and a chat token does not change that.**
The binding design (section 7.1) gives ``interactive`` to an *authenticated human at a chat client*.
The routes take a per-launch chat token (``spore-1310``), but a token authenticates a holder, not a
person: whatever read it from the server's output (a script, a supervisor, a log reader) can drive a
session, and activity is pull-only, so nothing guarantees a human reads it. The precondition is not
met, so the server does not claim it, and a client that presents the token does not earn
``interactive``. In ``headless`` an entity whose ``efferent_gate`` is ``"auto"`` is GATED: a turn that
proposes an efferent action halts, and the client approves or rejects it. The choice also moves the
credential floor: an entity that does not declare ``deny_standard_creds`` has the standard credential
stores denied in ``headless``, as in ``unattended``, and readable only in ``interactive``, where a human
watches the read happen (:func:`levain.firing.drive.resolve_cred_floor`). A chat entity whose work needs
one of those stores opts in with ``deny_standard_creds: false``. (L1 review 2026-10-03; revisit only if
a chat client can show that a present human, not just a token holder, is driving.)

**An entity that may reach this server is refused.** With ``allow_localhost_outbound`` its shell
could call these routes itself and approve its own held actions; with ``allow_container_sockets`` it
could do the same from a container it starts on the host network. Every other entity's shell is
denied loopback and the container daemon sockets its floor lists. (L2 review, codex L3 r1,
2026-10-03.)
The refusal fires twice: early from the config, and
after the session opens, from the floor its hands actually enforce, because the config can change
between the two reads (codex L3 r1) and only the second is the floor that will run.

**A session id is a capability.** ``/chat.json`` lists sessions by entity and state, never by id: the
id is returned only to the caller that opened the session, and every operation on a session names
it. Every client of one launch presents the same chat token, so the token cannot tell them apart;
the id is what keeps one caller from approving another's held actions without being handed it (L3
r1, complement).

**Activity is what was ISSUED; the result is what RAN.** A job's ``activity`` grows as the entity
issues tool actions, which is before the gate holds one and before a stop request skips one, so while
the turn runs a poll can show a line for an action that never runs. When the turn finishes, however
it finishes (normally, gated, timed out or with an error), ``activity`` is replaced by the result's
``tool_activity``, which leaves out held actions (they are in ``pending``) and actions a stop request
skipped. After a fault it keeps an action that may have been in flight, since that one may have run.
A job that failed without a result keeps what was streamed, and carries an ``error`` instead.

**A turn's wall-clock bound is a STOP REQUEST, honoured at the next step boundary.** Each turn,
approval or refusal job has a watcher; at ``turn_seconds`` it marks the job ``deadline_hit`` and
asks the driver to stop (:meth:`~levain.chat_driver.HarnessDriver.interrupt`; for OpenHands,
:meth:`EntitySession.request_stop`) until the job ends. ``deadline_hit`` is never cleared: it
says the deadline passed while the job ran, and the result's ``timed_out`` says whether the stop
ended the turn. A stopped turn comes back ``timed_out``, uncaptured, and the host breaks the session
and releases its shell. The SDK's synchronous run cannot be cancelled inside a step, so a step
already in flight finishes first: a shell command within its own timeout, a model call within the
SDK's HTTP timeout and retries. If that step finishes the turn and every tool call the turn made ran
to an outcome, the turn keeps its ordinary result and the session stays usable. Otherwise it is
stopped: a call that was cancelled, errored or has no outcome, a status other than finished or idle,
or an event log that cannot be read (:meth:`EntitySession.request_stop`). So the bound is the
deadline plus at most one step, not the deadline, and it does NOT cover capture: a deadline that
passes during ``capture_turn`` does not stop it, and a capture that hangs holds the job past the
deadline (not bounded here). A session stays ``busy`` (and counted) until its worker returns.
Workers are daemon threads, so stopping the server does not wait for one; the SDK closes every live
conversation, and its shell, at interpreter exit.

**A session is counted toward the cap until its shell is released.** ``close`` marks it ``closing``
(still counted, refusing every operation), starts the teardown outside the lock, and only then publishes
``closed``; an open that lands after shutdown is torn down while it still reads ``opening``. So a
slow teardown can never let an ``open`` exceed ``max_sessions`` (codex L3 r2). "Released" is what the
driver REPORTS (``on_released``, :meth:`~levain.chat_driver.HarnessDriver.open`): the host never asks.
A job's own outcome is published before its session's close starts, so a client polling the job never
waits on a teardown.

**A release that is not confirmed is not a release** (ruled 2026-10-07). When a driver's close raises,
it reports a failed release (after its own escalation, where it has one, failed too), it reports
something that is neither ``None`` nor text, or it has not reported within :data:`_REPORT_SECONDS`, the
session reads ``release_failed``: still counted, refusing every operation, and listed in ``/chat.json``
with its error and ``release_failed_since``. Its shell may still be live, so its slot is not handed to a
new session. The state means "not confirmed YET": a release reported later frees the slot, and the
session then reads ``closed`` (or how it ended) with ``released_late``. Otherwise restarting
``levain serve`` ends it (the process that held the shell exits). An OpenHands session reaches it only
through a fault: :meth:`EntitySession.close` never raises.

**No host thread runs driver code, and none waits on it without a deadline** (ruled 2026-10-07). The host
holds a driver only as a :class:`_DriverProxy`, which exposes none of the driver's methods. Every
attribute it reads and every call it makes runs on one of that driver's own lanes (threads started
when the session opens: a turn lane, a control lane, a stop lane, a release lane), through the one boundary
(:func:`levain.chat_driver._call_driver`), with its result checked for its exact declared type there.
The host waits for each with a deadline (:data:`_CALL_SECONDS`, :data:`_OPEN_SECONDS`, the turn's bound
plus :data:`_TURN_GRACE_SECONDS`). A driver that does not answer in time fails the call closed (an
approve is refused, a turn or an open fails), and a session it leaves stranded reads ``unresponsive``,
which ``close`` and ``shutdown`` act on. Nothing a driver raises (any ``BaseException``) reaches a host
thread, and nothing driver-made is read under the host lock. The reaper never calls a driver: it
enforces the report deadline, starts with the first open and exits once no session can start a release.
No host teardown starts a thread (a driver's own close may: OpenHands asks a running turn to stop from
a thread of its own, and a stop that cannot start hands the release to the turn's return).

**Why the job registry is in memory, unlike** :mod:`levain.jobs` **(which is on disk).** A turn job is
meaningful only while its conversation exists, and the conversation lives in this process (nothing
is persisted, the same as ``levain run``). A job record that outlived the process would describe a
turn whose conversation is gone. So a restart forgets every session and every job, and a poll for
one answers ``unknown``. Resume across a restart is not offered; when it is built it must follow the
binding design's section 7 (resume by ``(entity, conversation_id)``, a persistence dir the hands
cannot write, and never an agent rebuilt from the persisted record).

**What the host keeps from a failed start or a failed turn is TEXT, never the exception.** An
exception's traceback holds the frames of the failed start, and those frames hold whatever the start
had built: hands whose editor history directory is removed only when they are collected (codex L3
r1 on the binding branch). A server that stored the exception would keep them alive for as long as
it kept the record. Dropping it is not enough on its own: the SDK's tool build keeps the exception in
a reference cycle (a Future held by a frame inside its own traceback; L1 review 2026-10-03, run), so
a failed start also runs one cyclic collection.
"""
from __future__ import annotations

import copy
import gc
import logging
import math
import secrets
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Literal, Mapping

from levain.chat_driver import (
    DriverCall,
    DriverCaps,
    DriverContractError,
    DriverEvent,
    HarnessDriver,
    OpenHandsDriver,
    TurnSnapshot,
    _call_driver,
    _exc_text,
    read_outcome,
)
from levain.firing.gate import shown_in_full

if TYPE_CHECKING:
    from levain.firing.drive import DriveMode

__all__ = [
    "CHAT_DRIVE_MODE",
    "DEFAULT_TURN_SECONDS",
    "ChatError",
    "ChatHost",
    "MAX_ACTIVITY_LINES",
    "MAX_LINE_CHARS",
    "MAX_MESSAGE_CHARS",
    "chat_refusal",
]

_log = logging.getLogger("levain.chat")

SessionState = Literal["opening", "idle", "busy", "gated", "unresponsive", "closing", "release_failed",
                       "broken", "failed", "closed"]
JobKind = Literal["open", "turn", "approve", "reject"]
JobStatus = Literal["running", "done", "failed"]

CHAT_DRIVE_MODE: "DriveMode" = "headless"
"""The drive mode every chat session opens in. See the module docstring for why not interactive."""

MAX_MESSAGE_CHARS = 64_000
"""A message longer than this is refused with 413. It bounds what one request can put into the
model's context; the transport's own body limit may refuse a long escaped message first."""

MAX_ACTIVITY_LINES = 500
"""A job keeps at most this many activity lines; past it the oldest are dropped and the job says how
many. A result's ``tool_activity`` is cut to the same count."""

MAX_LINE_CHARS = 2_000
"""An activity line longer than this is cut, ending in " …"."""

_ENDED_SESSIONS_KEPT = 100
"""How many ended sessions (closed, failed, broken) stay readable. Older ones are forgotten."""

_LIVE_STATES = ("opening", "idle", "busy", "gated", "unresponsive", "closing", "release_failed")
"""The states that hold, or are about to hold, a conversation. Only these count toward the cap. A
broken session's shell is released by the worker BEFORE it is marked broken."""

_RELEASING_STATES = ("opening", "idle", "busy", "gated", "unresponsive", "closing")
"""The states whose session can still START a release. The reaper runs while any record is in one of them
(or a report deadline is pending), so no teardown ever has to start it."""

_FINISHED_JOBS_KEPT = 200
"""How many finished jobs stay pollable. Older finished jobs are forgotten (a poll answers
``unknown``); a running job is never dropped."""

DEFAULT_MAX_SESSIONS = 4
"""Live sessions per server. Each holds a conversation and, once used, a sandboxed shell."""

DEFAULT_TURN_SECONDS = 1800.0
"""A job's wall-clock deadline (``levain serve --turn-seconds``): long enough for real multi-step
work. It bounds a turn that keeps taking steps, not one stuck inside a step: a model call that
stalls holds the session past the deadline until the SDK's own HTTP timeout and retries give up
(run against an endpoint that never answers: the deadline fired and the job kept running). Cutting
a stalled call short needs the SDK's async run, which this host does not use."""

_WATCHER_JOIN_SECONDS = 10.0
"""How long a worker waits for its deadline watcher to exit after the job returns. The watcher's
stop request blocks only while a step runs, and none is running by then, so this is a backstop."""

_CALL_SECONDS = 10.0
"""How long the host waits for a short call into a driver (an attribute, ``describe``, ``held_digest``,
``interrupt``). Past it the call fails closed and the session reads ``unresponsive``."""

_OPEN_SECONDS = 600.0
"""How long the host waits for the driver to be made, and then again for it to open (building the hands
can be slow)."""

_TURN_GRACE_SECONDS = 600.0
"""How long past ``turn_seconds`` the host waits for a turn call to return: the stop request lands at a
step boundary, and a step in flight (a model call within the SDK's own timeout) finishes first."""

_UNBOUNDED_TURN_SECONDS = 86_400.0
"""How long the host waits for a turn when ``turn_seconds`` is ``None`` (no stop request is ever sent):
a day, so even then no host thread waits on driver code without a deadline."""

_CLOSE_ROUTE_SECONDS = 5.0
"""How long ``close()`` waits for the driver's close call before it answers ``closing``. The close goes on
on the driver's release lane; this bounds only the route."""

_REPORT_SECONDS = 180.0
"""How long after a close starts the host waits for the driver to REPORT its release. Past it the session
reads ``release_failed`` (still counted); a ``None`` report that comes later still releases it."""

_SHUTDOWN_JOIN_SECONDS = 60.0
"""How long :meth:`ChatHost.shutdown` waits for its closes (all started at once, each on its driver's own
release lane)."""

_REAP_IDLE_SECONDS = 1.0
"""How often the reaper re-checks whether it is still needed when no deadline is pending."""


class _FailedStart(Exception):
    """A failed open whose text is already plain (read through the boundary)."""


class ChatError(Exception):
    """A refused chat operation: ``code`` is machine-readable, ``http_status`` is the route's answer."""

    def __init__(self, code: str, message: str, http_status: int) -> None:
        super().__init__(message)
        self.code = code
        self.http_status = http_status


def chat_refusal(entity_dir: Path) -> str | None:
    """Why ``entity_dir`` may not be hosted for chat, or ``None``. A config that does not load is not
    refused HERE: opening the session reports it in full."""
    from levain.firing.confinement import ConfinementError, load_confinement_config

    try:
        cfg = load_confinement_config(entity_dir)
    except (ConfinementError, OSError, ValueError):
        return None
    if cfg.allow_localhost_outbound:
        return (
            f"{entity_dir} allows its shell to connect to localhost (allow_localhost_outbound), so "
            "it could call this server's chat routes itself, approve its own held actions and drive "
            "the other entities. Chat refuses to host it; use `levain run` for this entity."
        )
    if cfg.allow_container_sockets:
        return (
            f"{entity_dir} allows its shell to reach the container daemon sockets "
            "(allow_container_sockets), so it could start a container on the host network and call "
            "this server's chat routes from there, approving its own held actions. Chat refuses to "
            "host it; use `levain run` for this entity."
        )
    return None


def _names_bytes(digest: Any) -> bool:
    """A digest an approval can bind to: non-blank text. A blank one names nothing, so two blanks would
    "match" across two different holds (codex L3)."""
    return isinstance(digest, str) and bool(digest.strip())


def _cap_line(line: Any) -> str:
    text = str(line)
    return text if len(text) <= MAX_LINE_CHARS else text[:MAX_LINE_CHARS] + " …"


@dataclass
class _Job:
    job_id: str
    session_id: str
    kind: JobKind
    status: JobStatus = "running"
    deadline_hit: bool = False
    activity: list[str] = field(default_factory=list)
    dropped: int = 0
    result: dict[str, Any] | None = None
    error: str | None = None


@dataclass
class _Session:
    session_id: str
    entity: str
    state: SessionState = "opening"
    release_failed_since: str | None = None   # when its release failed (UTC, ISO 8601), for release_failed
    released_late: str | None = None   # when a release first recorded as failed was confirmed after all
    decision_id: str | None = None   # single-use: names ONE gated halt; spent the moment a decision starts
    approvable: bool = False         # that halt may be approved (else reject-only); the id still names it
    pending: list[dict[str, Any]] = field(default_factory=list)   # the held set that id names, for a re-read
    held_digest: str | None = None   # what an approve of that id binds to (levain.firing.openhands.gate.held_digest)
    last_job_id: str | None = None   # the most recent job that STARTED on this session, for a page that lost it
    driver: _DriverProxy | None = None   # the conversation's driver once open; None before and after
    error: str | None = None     # why it failed or broke, as text (see the module docstring)
    job_id: str | None = None    # the job currently driving it, if any
    info: dict[str, Any] = field(default_factory=dict)

    @property
    def session(self) -> Any:
        """The harness's own session object (an EntitySession for OpenHands), for tests and the
        floor check. The host itself only ever talks to :attr:`driver`."""
        if self.driver is None:
            return None
        got = self.driver.read("native")
        return got.value if got.ok else None   # through the boundary, on the driver's own lane


def _turn_payload(result: Any) -> dict[str, Any]:
    """A :class:`~levain.session.TurnResult` as JSON-shaped data: it reads ``result`` once
    (:func:`~levain.chat_driver.read_outcome`) and serialises that snapshot. ``ok`` and ``exit_code`` are
    the result's own derived properties, so a client reads the harness's classification rather than
    re-deriving it."""
    return _snapshot_payload(read_outcome(result, strict=False))


def _snapshot_payload(snap: TurnSnapshot) -> dict[str, Any]:
    activity = [_cap_line(x) for x in snap.tool_activity]
    pending = [
        {"tool": p.tool_name, "detail": p.detail, "full": p.full, "reason": p.reason,
         "recognized": p.recognized}
        for p in snap.pending
    ]
    return {
        "reply": snap.reply,
        "unreadable_call": snap.unreadable_call,
        "tool_activity": activity[-MAX_ACTIVITY_LINES:],
        "error": snap.error,
        "nudged": snap.nudged,
        "gated": snap.gated,
        "timed_out": snap.timed_out,
        "pending": pending,
        "ok": snap.ok,
        "exit_code": snap.exit_code,
    }


def _hands_reach_this_server(session: Any) -> str | None:
    """What lets ``session``'s shell reach this server, read from the floor the hands enforce, or
    ``None``. Two ways: connecting to localhost, and a container daemon socket (a container on the
    host network connects to localhost). The floor carries no socket sources only when the
    entity opted out of the socket denies. An unreadable floor on a session with a shell counts
    as reaching it (fail-closed)."""
    if not getattr(session, "bash_ok", False):
        return None
    try:
        floor = session.conversation.agent.tools_map["file_editor"].executor._policy
        if not bool(floor.deny_localhost_outbound):
            return "may connect to localhost"
        if not tuple(floor.socket_sources):
            return "may reach the container daemon sockets"
        return None
    except Exception:  # noqa: BLE001 — cannot read the floor: assume the worst
        return "has a floor that cannot be read"


def _default_factory(
    *, model: str, base_url: str, api_key: str | None, max_iterations: int | None
) -> Callable[..., Any]:
    def _open(entity_dir: Path, *, on_event: Callable[[str], None]) -> Any:
        from levain.session import EntitySession

        refusal = chat_refusal(entity_dir)
        if refusal is not None:
            raise ChatError("refused_entity", refusal, 403)
        session = EntitySession.open(
            entity_dir,
            model=model,
            base_url=base_url,
            api_key=api_key,
            with_tools=True,
            on_event=on_event,
            max_iterations=max_iterations,
            mode=CHAT_DRIVE_MODE,
        )
        reach = _hands_reach_this_server(session)
        if reach is not None:
            session.close()
            raise ChatError(
                "refused_entity",
                f"{entity_dir}: the opened session's shell {reach}, so it could call this "
                "server's chat routes itself. Refused (its confinement.json changed, or allows it).",
                403,
            )
        return session

    return _open


def _plain_banner(described: Any) -> dict[str, Any]:
    """A driver's banner (a ``dict``, a subclass read through ``dict``'s own methods) as plain JSON data: exact ``str`` keys, and values of exactly ``str``, ``bool``,
    ``int``, a finite ``float``, or ``None``. Anything else is dropped, never converted (a conversion runs
    the driver's own code; JSON has no NaN)."""
    if not isinstance(described, dict):
        raise DriverContractError(f"describe() gave {type(described).__name__}, not a dict")
    return {k: v for k, v in dict.items(described)
            if type(k) is str and (v is None or type(v) in (str, bool, int)
                                   or (type(v) is float and math.isfinite(v)))}


_HOST_VIEW_KEYS = frozenset({"session_id", "entity", "state", "job_id", "error", "release_failed_since",
                             "released_late", "last_job", "pending", "decision_id", "approvable"})
"""The keys a session view takes from the host only, never from a driver's :meth:`describe`."""


class _Lane:
    """One worker thread of a driver's, started when its session opens, that runs host->driver calls one at
    a time. The host waits for each with a deadline and never runs driver code itself. A call that outlives
    its deadline makes the lane STUCK until it returns: a later call fails at once rather than queue behind
    it."""

    def __init__(self, name: str) -> None:
        self._cond = threading.Condition()
        self._queue: deque[tuple[str, Callable[[], DriverCall], "_Pending"]] = deque()
        self._stuck: _Pending | None = None     # the call past its deadline that has not returned
        self._running_box: _Pending | None = None
        self._retired = False
        self._thread = threading.Thread(target=self._run, daemon=True, name=name)

    def start(self) -> None:
        self._thread.start()

    def submit(self, what: str, fn: Callable[[], DriverCall],
               on_done: Callable[[DriverCall], None] | None = None) -> "_Pending | DriverCall":
        with self._cond:
            if self._retired:
                return DriverCall(False, None, f"DriverClosed: {what}: the driver is closed", "the driver is closed")
            if self._stuck is not None:
                text = f"{what}: an earlier call ({self._stuck.what}) has not returned"
                return DriverCall(False, None, f"DriverUnresponsive: {text}", text, unanswered=True)
            box = _Pending(on_done, what)
            self._queue.append((what, fn, box))
            self._cond.notify_all()
            return box

    def call(self, what: str, fn: Callable[[], DriverCall], timeout: float | None) -> DriverCall:
        box = self.submit(what, fn)
        if isinstance(box, DriverCall):
            return box
        if timeout is not None:
            timeout = min(timeout, threading.TIMEOUT_MAX)    # past it a wait raises instead of waiting
        if box.done.wait(timeout):
            assert box.result is not None
            return box.result
        with self._cond:
            if box.result is not None:      # it landed between the wait and this lock
                return box.result
            box.abandoned = True
            if self._running_box is box:
                self._stuck = box           # still running: later calls fail at once until it returns
        text = f"{what}: the driver did not answer within {timeout:g}s"
        return DriverCall(False, None, f"DriverUnresponsive: {text}", text, unanswered=True)

    def idle(self) -> bool:
        """Nothing is queued or running on this lane."""
        with self._cond:
            return not self._queue and self._running_box is None

    def retire(self) -> None:
        """Run what is queued, then exit."""
        with self._cond:
            self._retired = True
            self._cond.notify_all()

    def _run(self) -> None:
        while True:
            with self._cond:
                self._cond.wait_for(lambda: self._queue or self._retired)
                if not self._queue:
                    return
                what, fn, box = self._queue.popleft()
                if box.abandoned:
                    # its caller was told it failed; running it now would act on an answer nobody waits for
                    box.result = DriverCall(False, None, f"DriverUnresponsive: {what}: abandoned", "abandoned",
                                            unanswered=True)
                    box.done.set()
                    continue
                self._running_box = box
            result = fn()        # _call_driver: never raises
            with self._cond:
                box.result = result
                self._running_box = None
                if self._stuck is box:
                    self._stuck = None
            box.done.set()
            if box.on_done is not None:
                try:
                    box.on_done(result)
                except BaseException as exc:  # noqa: BLE001 — host code on a driver's thread
                    _log.error("chat: handling %s failed: %s", what, ": ".join(_exc_text(exc)))


@dataclass(eq=False)
class _Pending:
    on_done: Callable[[DriverCall], None] | None
    what: str = ""
    done: threading.Event = field(default_factory=threading.Event)
    result: DriverCall | None = None
    abandoned: bool = False


_ANY: tuple[type, ...] = ()


def _invoke(driver: Any, name: str, args: tuple[Any, ...], kwargs: dict[str, Any], expect: tuple[type, ...],
            call: bool, convert: Callable[[Any], Any] | None) -> DriverCall:
    """The body of every host->driver call, run on the driver's lane: the attribute lookup, the call, the
    EXACT type check and any copy into plain data, all inside the boundary."""

    def body() -> Any:
        value = getattr(driver, name)
        if call:
            value = value(*args, **kwargs)
        if expect and type(value) not in expect:
            raise DriverContractError(
                f"{name} gave {type(value).__name__}, not {' or '.join(t.__name__ for t in expect)}")
        return convert(value) if convert is not None else value

    return _call_driver(body)


class _DriverProxy:
    """How the host holds a driver: never raw. Every attribute read and every call runs on one of the
    driver's own lanes (a turn lane for open and the turn calls, a control lane for short calls, a stop lane
    for stop requests, a release lane for close), started when the session opens, through the boundary, with the host waiting a deadline
    for it. It exposes none of :class:`~levain.chat_driver.HarnessDriver`'s methods, so a direct call cannot
    be written (ruled 2026-10-07)."""

    __slots__ = ("_driver", "_lanes", "harness", "caps", "release", "early_report")

    def __init__(self, name: str) -> None:
        """Start the lanes. Raises ``RuntimeError`` when a thread cannot start: this is the OPEN path, so a
        session that cannot get its workers is not opened, and no teardown ever has to start one."""
        self._driver: Any = None
        self._lanes = {k: _Lane(f"levain-chat-{k}-{name}") for k in ("turn", "control", "stop", "release")}
        started = []
        try:
            for lane in self._lanes.values():
                lane.start()
                started.append(lane)
        except BaseException:
            for lane in started:
                lane.retire()
            raise
        self.harness = "unnamed"
        self.caps = DriverCaps()
        self.release: _Release | None = None        # the host's record of this driver's release, once closed
        self.early_report: tuple[str | None] | None = None   # a release reported before close was called

    def make(self, factory: Callable[..., Any], *args: Any, timeout: float) -> DriverCall:
        """Build the driver (the factory is code that makes driver code), then read its name and caps."""
        made = self._lanes["turn"].call("make", lambda: _call_driver(factory, *args), timeout)
        if not made.ok:
            return made
        self._driver = made.value
        harness = self.read("harness", expect=(str,))
        caps = self.read("caps", expect=(DriverCaps,))
        if harness.ok:
            self.harness = harness.value
        if caps.ok:
            self.caps = caps.value
        return caps if not caps.ok else made

    def call(self, method: str, *args: Any, lane: str = "control", timeout: float | None = _CALL_SECONDS,
             expect: tuple[type, ...] = _ANY, convert: Callable[[Any], Any] | None = None,
             **kwargs: Any) -> DriverCall:
        drv = self._driver
        return self._lanes[lane].call(
            method, lambda: _invoke(drv, method, args, kwargs, expect, True, convert), timeout)

    def read(self, attribute: str, *, expect: tuple[type, ...] = _ANY, timeout: float | None = None) -> DriverCall:
        drv = self._driver
        return self._lanes["control"].call(
            attribute, lambda: _invoke(drv, attribute, (), {}, expect, False, None),
            _CALL_SECONDS if timeout is None else timeout)

    def submit_close(self, on_done: Callable[[DriverCall], None]) -> "_Pending | DriverCall":
        drv = self._driver
        if drv is None:
            return DriverCall(True, None)
        return self._lanes["release"].submit(
            "close", lambda: _invoke(drv, "close", (), {}, _ANY, True, None), on_done)

    def stop_idle(self) -> bool:
        """No stop request is queued or still running (one that outlived its deadline could land in the
        NEXT turn)."""
        return self._lanes["stop"].idle()

    def retire(self, *, keep_release: bool = False) -> None:
        for name, lane in self._lanes.items():
            if not (keep_release and name == "release"):
                lane.retire()


@dataclass(eq=False)
class _Release:
    """A closed driver's release, as the host waits for its report."""

    rec: _Session
    proxy: _DriverProxy
    settle: Callable[[], None]
    publish_job: Callable[[], None] | None
    deadline: float                   # when "never reported" becomes release_failed
    closed: threading.Event = field(default_factory=threading.Event)   # the close call returned (or failed)
    settled: bool = False             # a None report was handled: released
    failed: bool = False              # recorded as release_failed (a later None report still settles it)


class ChatHost:
    """Live entity sessions for one server process, driven by jobs.

    ``entities`` maps a NAME to an entity directory; it is fixed at construction and is the whole
    of what a client can address. ``session_factory`` is a test seam; production passes ``None`` and
    gets :meth:`EntitySession.open` with the operator's model settings and :data:`CHAT_DRIVE_MODE`.
    ``driver_factory(name, entity_dir)`` makes the :class:`~levain.chat_driver.HarnessDriver` for one
    session of that entity; the two seams are exclusive, and a ``session_factory`` is wrapped in an
    :class:`~levain.chat_driver.OpenHandsDriver`.
    """

    def __init__(
        self,
        entities: Mapping[str, Path],
        *,
        model: str = "glm-5.2:cloud",
        base_url: str = "http://localhost:11434",
        api_key: str | None = None,
        max_iterations: int | None = None,
        max_sessions: int = DEFAULT_MAX_SESSIONS,
        turn_seconds: float | None = DEFAULT_TURN_SECONDS,
        session_factory: Callable[..., Any] | None = None,
        driver_factory: Callable[[str, Path], HarnessDriver] | None = None,
    ) -> None:
        if session_factory is not None and driver_factory is not None:
            raise ValueError("pass a session_factory or a driver_factory, not both")
        if not entities:
            raise ValueError("a chat host needs at least one entity")
        if max_sessions < 1:
            raise ValueError("max_sessions must be at least 1")
        if max_iterations is not None and max_iterations < 1:
            raise ValueError("max_iterations must be at least 1")
        if turn_seconds is not None and not (
                math.isfinite(turn_seconds) and 0 < turn_seconds <= threading.TIMEOUT_MAX):
            # `nan > 0` is False and `inf` never fires, so both read as bounded and are not. Past
            # TIMEOUT_MAX the watcher's wait raises instead of waiting, and the turn has no bound.
            raise ValueError(
                "turn_seconds must be a finite number of seconds above 0 and at most "
                f"{threading.TIMEOUT_MAX:.0f}, or None")
        self._entities = {name: Path(p) for name, p in entities.items()}
        # One driver per (entity, conversation), made by entity name. The default is OpenHands for every
        # entity; a constellation of mixed harnesses supplies its own mapping here (and then no OpenHands
        # opener is built).
        if driver_factory is None:
            opener = session_factory or _default_factory(
                model=model, base_url=base_url, api_key=api_key, max_iterations=max_iterations
            )
            driver_factory = lambda name, entity_dir: OpenHandsDriver(entity_dir, opener)  # noqa: E731
        self._driver_factory: Callable[[str, Path], HarnessDriver] = driver_factory
        self._model = model
        self._max_sessions = max_sessions
        self._turn_seconds = turn_seconds
        self._lock = threading.Lock()
        self._sessions: dict[str, _Session] = {}
        self._jobs: dict[str, _Job] = {}
        self._shut = False
        # The reaper enforces the report deadlines. It is started LAZILY, by the first open (an open path:
        # one that cannot start it is refused), and it exits once no session can start a release and no
        # deadline is pending, so a host nobody shut down holds no thread. While any record can still
        # start a release it runs, so no teardown ever has to start it (ruled 2026-10-07).
        self._reap_cond = threading.Condition(self._lock)
        self._reports: list[_Release] = []       # releases whose report deadline has not passed
        self._reaper_thread: threading.Thread | None = None

    # -- reads ---------------------------------------------------------------

    def listing(self) -> dict[str, Any]:
        with self._lock:
            return {
                "entities": sorted(self._entities),
                "model": self._model,
                "drive_mode": CHAT_DRIVE_MODE,
                "max_sessions": self._max_sessions,
                "turn_seconds": self._turn_seconds,
                "sessions": [
                    {k: v for k, v in self._session_view(s).items()
                     if k not in ("session_id", "job_id")}
                    for s in self._sessions.values()
                ],
            }

    def session_status(self, session_id: Any) -> dict[str, Any]:
        """One session's state. For a ``gated`` session it also carries the CURRENT ``decision_id`` and the
        ``pending`` set that id names, so a caller that lost the turn's result (a reloaded page, a lost
        poll) can decide again. Only here, never in :meth:`listing`: the id is addressed by a session id. A
        gated session with no current id (an approve found the held calls changed) still reports its
        ``pending`` set, so it can be shown and rejected; without an id it cannot be approved. ``last_job`` is the
        most recent job that started on the session (its kind, status, activity and result, as
        :meth:`job_status` gives them), so a page that lost track of a turn or decision can see what it did."""
        with self._lock:
            rec = self._get(session_id)
            out = self._session_view(rec)
            if rec.last_job_id is not None:
                out["last_job"] = self._job_view(rec.last_job_id)
            if rec.state == "gated":
                out["pending"] = [dict(p) for p in rec.pending]   # rows of plain values: one level is a full copy
                if rec.decision_id is not None:
                    out["decision_id"] = rec.decision_id
                    out["approvable"] = rec.approvable
            return out

    def job_status(self, job_id: str) -> dict[str, Any]:
        with self._lock:
            return self._job_view(job_id)

    def _job_view(self, job_id: str) -> dict[str, Any]:
        """One job as JSON-shaped data; ``status: unknown`` once it is no longer held. Caller holds the lock."""
        job = self._jobs.get(job_id)
        if job is None:
            return {"job_id": job_id, "status": "unknown"}
        out: dict[str, Any] = {
            "job_id": job.job_id,
            "session_id": job.session_id,
            "kind": job.kind,
            "status": job.status,
            "activity": list(job.activity),
            "activity_dropped": job.dropped,
            "deadline_hit": job.deadline_hit,
        }
        if job.result is not None:
            # A deep copy: a view's nested rows (``pending`` and its dicts) must not be the stored ones, or a
            # consumer that edits a view changes what the next viewer is shown under the same decision id
            # (codex release-range review, run).
            out["result"] = copy.deepcopy(job.result)
        if job.error is not None:
            out["error"] = job.error
        return out

    # -- operations ----------------------------------------------------------

    def open(self, entity: Any) -> dict[str, Any]:
        """Start opening a session on a registered entity. Returns the session id and the open job."""
        if not isinstance(entity, str) or entity not in self._entities:
            raise ChatError("unknown_entity", "no such entity on this server", 404)
        with self._lock:
            self._refuse_if_shut()
            live = sum(1 for s in self._sessions.values() if s.state in _LIVE_STATES)
            if live >= self._max_sessions:
                raise ChatError(
                    "too_many_sessions",
                    f"this server holds at most {self._max_sessions} live sessions; close one first",
                    429,
                )
            ended = [s for s in self._sessions.values() if s.state not in _LIVE_STATES]
            for old in ended[: max(0, len(ended) - _ENDED_SESSIONS_KEPT)]:
                del self._sessions[old.session_id]
            if not self._ensure_reaper():
                raise ChatError("busy", "could not start the reaper; try again", 503)
            sid = secrets.token_hex(8)
            rec = _Session(session_id=sid, entity=entity)
            self._sessions[sid] = rec
            job = self._new_job(rec, "open")
            if not self._spawn(self._run_open, rec, job):
                del self._sessions[sid]
                del self._jobs[job.job_id]
                raise ChatError("busy", "could not start a worker; try again", 503)
        return {"session_id": sid, "job_id": job.job_id, "state": "opening"}

    def turn(self, session_id: Any, message: Any) -> dict[str, Any]:
        if not isinstance(message, str) or not message.strip():
            raise ChatError("bad_message", "message must be a non-empty string", 400)
        if len(message) > MAX_MESSAGE_CHARS:
            raise ChatError("too_large", f"message exceeds {MAX_MESSAGE_CHARS} characters", 413)
        return self._start(session_id, "turn", ("idle",), ("send_turn", message))

    def approve(self, session_id: Any, expect: Any = None) -> dict[str, Any]:
        """Run the held actions. ``expect`` (the current decision id) is REQUIRED: an approval is bound to
        the set the operator was shown, for every caller, API included (Phill 2026-10-05)."""
        return self._start(session_id, "approve", ("gated",), ("approve",), expect=expect,
                           require_expect=True)

    def reject(self, session_id: Any, reason: Any = None, expect: Any = None) -> dict[str, Any]:
        if reason is None:
            reason = "the operator declined this action"
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 2000:
            raise ChatError("bad_reason", "reason must be a non-empty string under 2000 chars", 400)
        return self._start(session_id, "reject", ("gated",), ("reject", reason), expect=expect,
                           require_expect=True)

    def close(self, session_id: Any) -> dict[str, Any]:
        """Close a session. Refused while a job is driving it (the turn would be torn down under
        itself) and while another close is tearing it down; an ``unresponsive`` session is closed. A failed
        session stays ``failed``; any other ends ``closed``, published only once its driver REPORTS the
        release (module docstring). This waits for the driver's close call at most
        :data:`_CLOSE_ROUTE_SECONDS`, then answers ``closing``."""
        with self._lock:
            rec = self._get(session_id)
            if rec.state in ("opening", "busy", "closing"):
                raise ChatError(
                    "busy", f"the session is {rec.state}; close it when that finishes", 409)
            driver, rec.driver = rec.driver, None
            if driver is None:
                if rec.state not in ("failed", "release_failed"):
                    rec.state = "closed"      # a release that failed stays counted until one is reported
                return self._session_view(rec)
            rec.state = "closing"
        self._close_then(rec, driver, lambda: self._settle(rec, "closed")).wait(_CLOSE_ROUTE_SECONDS)
        with self._lock:
            return self._session_view(rec)

    def shutdown(self) -> None:
        """Close every idle, gated or unresponsive session and stop accepting work. Every close starts at
        once, each on its own driver's release lane (no thread is started here), and this waits for them
        together at most :data:`_SHUTDOWN_JOIN_SECONDS`; a release reported later settles its record then. A
        job still running is not interrupted; its worker closes the session when the job ends, and if the
        process exits first the SDK closes the conversation at interpreter exit."""
        with self._lock:
            self._shut = True
            to_close = []
            for rec in self._sessions.values():
                if rec.state not in ("opening", "busy", "closing") and rec.driver is not None:
                    to_close.append((rec, rec.driver))
                    rec.driver = None
                    rec.state = "closing"
        started = [self._close_then(rec, d, lambda rec=rec: self._settle(rec, "closed")) for rec, d in to_close]
        give_up = time.monotonic() + _SHUTDOWN_JOIN_SECONDS
        for closed in started:
            closed.wait(max(0.0, give_up - time.monotonic()))

    # -- internals -----------------------------------------------------------

    def _settle(self, rec: _Session, state: SessionState) -> None:
        with self._lock:
            rec.state = state

    def _close_then(self, rec: _Session, driver: _DriverProxy, settle: Callable[[], None],
                    publish_job: Callable[[], None] | None = None) -> threading.Event:
        """Start closing ``driver`` and return at once; ``settle`` (it takes the lock itself) runs when the
        driver REPORTS its release (``on_released``, ruled 2026-10-07). The caller leaves the record in a
        counted state until then. The close call runs on the driver's release lane; nothing here waits on
        driver code. A close that raised, a failed report, a report that breaks the contract, or no report
        by :data:`_REPORT_SECONDS` ends the record ``release_failed``, still counted; a ``None`` report that
        comes after that still releases it. Returns an event set once the close call has returned.

        ``publish_job`` publishes the job's own outcome, BEFORE the close: a client polling the job never
        waits on a teardown (concurrent.futures' shutdown(wait=False) shape; glm L3 r2)."""
        if publish_job is not None:
            try:
                publish_job()         # idempotent: the settle that ends the record runs it again, harmlessly
            except BaseException as exc:  # noqa: BLE001 — the driver is still closed below
                _log.error("chat session %s: publishing the job failed: %s", rec.session_id,
                           ": ".join(_exc_text(exc)))
        try:
            item = _Release(rec, driver, settle, publish_job, time.monotonic() + _REPORT_SECONDS)
            with self._lock:
                driver.release = item
                early = driver.early_report
                self._reports.append(item)
                self._reap_cond.notify_all()
            if early is not None:
                self._on_report(driver, early[0])

            def _closed(result: DriverCall) -> None:
                try:
                    if not result.ok:
                        self._release_failed(item, f"close: {result.error}")
                finally:
                    item.closed.set()
                    driver.retire()

            submitted = driver.submit_close(_closed)
            driver.retire(keep_release=True)   # nothing is called after close: the other lanes exit now
            if isinstance(submitted, DriverCall):
                _closed(submitted)
            return item.closed
        except BaseException as exc:  # noqa: BLE001 — host code; a fault here must still end the record
            self._mark_release_failed(rec, f"closing failed: {': '.join(_exc_text(exc))}", publish_job)
            done = threading.Event()
            done.set()
            return done

    def _released_sink(self, proxy_box: list[_DriverProxy]) -> Callable[..., None]:
        """The ``on_released`` a driver is opened with. Driver threads call it; it is host code and never
        raises into them."""

        def on_released(error: Any = None) -> None:
            try:
                proxy = proxy_box[0]
                if error is not None and type(error) is not str:
                    error = (f"driver contract: on_released was given {type(error).__name__}, "
                             "not text or None")
                self._on_report(proxy, error)
            except BaseException as exc:  # noqa: BLE001
                _log.error("chat: handling a release report failed: %s", ": ".join(_exc_text(exc)))

        return on_released

    def _on_report(self, proxy: _DriverProxy, error: str | None) -> None:
        with self._lock:
            item = proxy.release
            if item is None:
                proxy.early_report = (error,)      # before close was called: handled when it is
                return
            if item.settled:
                return
            if error is None:
                item.settled = True
                late = item.failed
                if item in self._reports:
                    self._reports.remove(item)
        if error is not None:
            self._release_failed(item, error)
            return
        self._run_settle(item.rec, item.settle, item.publish_job)
        if late:
            # release_failed means "not confirmed YET": a confirmed release frees the slot (ruled 2026-10-07)
            stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
            _log.warning("chat session %s: its release was confirmed after it was recorded as failed",
                         item.rec.session_id)
            with self._lock:
                item.rec.release_failed_since, item.rec.released_late = None, stamp

    def _release_failed(self, item: _Release, failure: str) -> None:
        with self._lock:
            if item.settled:
                return
            if item in self._reports:
                self._reports.remove(item)
        self._mark_release_failed(item.rec, failure, item.publish_job, item)

    def _mark_release_failed(self, rec: _Session, failure: str,
                             publish_job: Callable[[], None] | None = None,
                             item: _Release | None = None) -> None:
        if publish_job is not None:
            self._run_settle(rec, publish_job)    # the job keeps its own outcome
        with self._lock:
            # Decided in ONE hold with the report's own check (L1 + L2 r5, RAN): a None report that landed
            # since the caller looked has released the session, and must not be overwritten.
            if item is not None:
                if item.settled:
                    return
                item.failed = True
            _log.error("chat session %s: its release failed, so it stays counted until one is reported: %s",
                       rec.session_id, failure)
            job = self._jobs.get(rec.job_id) if rec.job_id else None
            if job is not None and job.status == "running":
                job.status, job.error = "failed", f"the session's release failed: {failure}"
            rec.job_id = None
            # the error that ended the session (a broken turn's, a failed open's) is kept, not replaced
            if rec.state != "release_failed":
                rec.error = failure if rec.error is None else f"{rec.error}; then its release failed: {failure}"
                rec.release_failed_since = datetime.now(timezone.utc).isoformat(timespec="seconds")
            rec.state = "release_failed"

    def _run_settle(self, rec: _Session, settle: Callable[[], None],
                    publish_job: Callable[[], None] | None = None) -> None:
        """Run ``publish_job`` then ``settle``; one that raises still ends the record, so nothing is left
        running or busy."""
        try:
            if publish_job is not None:
                publish_job()
            settle()
        except BaseException as exc:  # noqa: BLE001 — no worker may be left to settle this record
            text = ": ".join(_exc_text(exc))
            _log.error("chat session %s: settling after its release failed: %s", rec.session_id, text)
            with self._lock:
                job = self._jobs.get(rec.job_id) if rec.job_id else None
                if job is not None and job.status == "running":
                    job.status, job.error = "failed", text
                rec.job_id = None
                if rec.state in ("opening", "busy", "closing", "release_failed"):
                    ended: SessionState = "failed" if job is not None and job.kind == "open" else "broken"
                    rec.state, rec.error = ended, text

    def _ensure_reaper(self) -> bool:
        """Start the reaper if it is not running. Caller holds the lock; called on the OPEN path only."""
        if self._reaper_thread is not None:
            return True
        thread = threading.Thread(target=self._reaper, daemon=True, name="levain-chat-reaper")
        try:
            thread.start()
        except RuntimeError:
            return False
        self._reaper_thread = thread
        return True

    def _reaper(self) -> None:
        try:
            self._reap()
        finally:
            with self._lock:
                if self._reaper_thread is threading.current_thread():
                    self._reaper_thread = None     # an open starts another, even after a fault here

    def _reap(self) -> None:
        """The host's reaper: it never calls a driver. It enforces one deadline per release (a driver that
        has not REPORTED by :data:`_REPORT_SECONDS` reads ``release_failed``), and exits once no record can
        start a release and no deadline is pending; the next open starts it again."""
        while True:
            with self._reap_cond:
                now = time.monotonic()
                expired = [i for i in self._reports if i.deadline <= now]
                for item in expired:
                    self._reports.remove(item)
                if not expired:
                    if not self._reports and not any(
                            r.state in _RELEASING_STATES for r in self._sessions.values()):
                        self._reaper_thread = None      # under the lock: an open sees it gone and starts one
                        return
                    nearest = min((i.deadline for i in self._reports), default=None)
                    wait = _REAP_IDLE_SECONDS if nearest is None else min(_REAP_IDLE_SECONDS, max(0.0, nearest - now))
                    self._reap_cond.wait(wait)
                    continue
            for item in expired:
                # Handled once (it left _reports above), never retried: a fault that repeats would spin.
                failure = f"the driver did not report its release within {_REPORT_SECONDS:.0f}s"
                try:
                    self._release_failed(item, failure)
                except BaseException as exc:  # noqa: BLE001 — host code; one bad record must not stop the reaper
                    _log.error("chat session %s: reaping failed: %s", item.rec.session_id,
                               ": ".join(_exc_text(exc)))
                    try:
                        self._mark_release_failed(item.rec, f"{failure} (and handling it failed)", item=item)
                    except BaseException:  # noqa: BLE001
                        pass

    def _refuse_if_shut(self) -> None:
        if self._shut:
            raise ChatError("shutting_down", "the server is shutting down", 503)

    def _get(self, session_id: Any) -> _Session:
        rec = self._sessions.get(session_id) if isinstance(session_id, str) else None
        if rec is None:
            raise ChatError("unknown_session", "no such session", 404)
        return rec

    def _new_job(self, rec: _Session, kind: JobKind) -> _Job:
        """Register a running job for ``rec``. Caller holds the lock."""
        job = _Job(job_id=secrets.token_hex(8), session_id=rec.session_id, kind=kind)
        self._jobs[job.job_id] = job
        rec.job_id = job.job_id
        rec.last_job_id = job.job_id
        finished = [j for j in self._jobs.values() if j.status != "running"]
        for old in finished[: max(0, len(finished) - _FINISHED_JOBS_KEPT)]:
            del self._jobs[old.job_id]
        return job

    def _spawn(self, target: Callable[..., None], *args: Any) -> bool:
        """Start ``target`` on its own daemon thread. Caller holds the lock and has already checked
        ``_shut``, so a shutdown cannot land between that check and the start. A session runs at most
        one job, so there are never more workers than live sessions."""
        thread = threading.Thread(
            target=self._guarded, args=(target, *args), daemon=True, name="levain-chat")
        try:
            thread.start()
        except RuntimeError:
            return False
        return True

    def _guarded(self, target: Callable[..., None], rec: _Session, job: _Job, *args: Any) -> None:
        """Run a worker so that nothing escaping it leaves a job ``running`` or a session ``busy``.
        The workers settle their own results; this only catches a fault in that settling."""
        try:
            target(rec, job, *args)
        except BaseException as exc:  # noqa: BLE001 — a worker must always settle its records
            text = ": ".join(_exc_text(exc))
            _log.error("chat %s job %s escaped its worker: %s", job.kind, job.job_id, text)
            to_close: Any = None
            ended: SessionState = "failed" if job.kind == "open" else "broken"
            with self._lock:
                if job.status == "running":
                    job.status, job.error = "failed", text
                if rec.job_id == job.job_id:
                    rec.job_id = None
                    to_close, rec.driver = rec.driver, None
                    rec.error = text
                    # Still counted while its shell is released (L2 review: publishing the ended
                    # state first let an open exceed the cap during the teardown).
                    rec.state = "closing" if to_close is not None else ended
            if to_close is not None:
                self._close_then(rec, to_close, lambda: self._settle(rec, ended))

    def _start(
        self,
        session_id: Any,
        kind: JobKind,
        accepts: tuple[SessionState, ...],
        call: tuple[Any, ...],
        *,
        expect: Any = None,
        require_expect: bool = False,
    ) -> dict[str, Any]:
        with self._lock:
            self._refuse_if_shut()
            rec = self._get(session_id)
            if rec.state not in accepts:
                raise ChatError(
                    "wrong_state",
                    f"the session is {rec.state}; {kind} needs it {' or '.join(accepts)}",
                    409,
                )
            # ``expect`` is the decision id the operator's screen was shown, and EVERY decision requires it,
            # from every caller: approving by session id alone would run whatever is held now, which may not
            # be what any screen showed, and a reject by session id alone could land on a LATER halt than the
            # one its caller saw, advancing the conversation past a decision nobody made (codex
            # release-range review, run). The id is per halt, never per content, so two textually identical
            # holds cannot share one. A halt that cannot be approved keeps its id, for the reject.
            if not expect:
                expect = None   # "", 0, [] and the like are a missing id, not a wrong one
            if require_expect and expect is None:
                raise ChatError(
                    "decision_id_required",
                    f"{kind} needs the current decision id in the `expect` field; read it from "
                    "GET /chat/session.json?id=<session_id> (`decision_id`) while the session is gated; a turn that "
                    "halts also returns it as `decision_id` in its result",
                    400,
                )
            if expect is not None and (not isinstance(expect, str) or expect != rec.decision_id):
                raise ChatError(
                    "stale_decision",
                    f"the held action is not the one this {kind} was made on; read the session again",
                    409,
                )
            if kind == "approve" and not rec.approvable:
                raise ChatError(
                    "undecidable",
                    "this hold cannot be shown in full, or what it holds no longer matches what was shown, so "
                    "it can only be rejected (with the same decision id)",
                    409,
                )
            if kind != "approve":
                job = self._launch(rec, kind, call)
                return {"session_id": rec.session_id, "job_id": job.job_id, "state": "busy"}
            # The approval binds to the held calls' bytes, not only to the halt: the digest recorded with the
            # screen's set must equal the digest of what the next run() would execute. held_digest is driver
            # code, so it is read OUTSIDE the host lock (a driver that computes it over IPC must not stall every
            # route); meanwhile the record reads busy, so nothing else can act on it, and the id is held aside,
            # so a second decision on the same id is stale.
            driver, shown, held_id = rec.driver, rec.held_digest, rec.decision_id
            rec.state, rec.decision_id = "busy", None
        got = (driver.call("held_digest", timeout=_CALL_SECONDS, expect=(str, type(None)))
               if driver is not None else None)
        # unreadable, or not exactly text (a subclass could override `==`): no match
        live = got.value if got is not None and got.ok else None
        to_close: _DriverProxy | None = None
        with self._lock:
            rec.state = "gated"
            if got is not None and got.unanswered and not self._shut:
                # Fail closed: nothing ran. The session reads unresponsive, which close and shutdown act on.
                rec.state, rec.decision_id, rec.approvable = "unresponsive", None, False
                rec.error = f"the driver did not answer: {got.message}"
                raise ChatError("unresponsive", "the driver did not answer; nothing ran; close the session",
                                503)
            if self._shut:
                # shutdown() skipped this record (it read busy), so it is closed here.
                to_close, rec.driver, rec.state = rec.driver, None, "closing"
            elif not _names_bytes(shown) or not _names_bytes(live) or live != shown:
                # Never re-armed for approval: no screen holds a set that matches, so this halt is
                # reject-only. The id still names it, so the reject that follows binds to this halt.
                rec.decision_id, rec.approvable = held_id, False
                raise ChatError(
                    "stale_decision",
                    "what the gate holds is not what was shown, or cannot be read; nothing ran; this hold "
                    "can now only be rejected, with the same decision id",
                    409,
                )
            else:
                rec.decision_id = held_id
                job = self._launch(rec, kind, call)
                return {"session_id": rec.session_id, "job_id": job.job_id, "state": "busy"}
        if to_close is not None:
            self._close_then(rec, to_close, lambda: self._settle(rec, "closed"))
        raise ChatError("shutting_down", "the server is shutting down", 503)

    def _launch(self, rec: _Session, kind: JobKind, call: tuple[Any, ...]) -> _Job:
        """Start ``kind`` on ``rec``, which accepts it. Caller holds the lock."""
        spent = rec.decision_id
        rec.decision_id = None   # spent: whatever this decision does, no screen can decide this halt again
        before = rec.state
        rec.state = "busy"
        prev_last = rec.last_job_id
        job = self._new_job(rec, kind)
        done = threading.Event()
        watcher = None
        if self._turn_seconds is not None:
            # Started BEFORE the worker, so a worker never runs without its bound: if the
            # watcher cannot start, nothing has run yet and the job is refused.
            watcher = threading.Thread(
                target=self._watch, args=(job, rec.driver, done), daemon=True,
                name="levain-chat-deadline")
            try:
                watcher.start()
            except RuntimeError:
                watcher = None
                started = False
            else:
                started = True
        else:
            started = True
        if not started or not self._spawn(self._run_job, rec, job, call, done, watcher):
            done.set()
            rec.state, rec.job_id = before, None
            rec.last_job_id = prev_last   # this job never started, so it is not "what happened"
            rec.decision_id = spent   # nothing was decided: the halt is still held and still undecided
            del self._jobs[job.job_id]
            raise ChatError("busy", "could not start a worker; try again", 503)
        return job

    def _watch(self, job: _Job, driver: _DriverProxy | None, done: threading.Event) -> None:
        """A job's wall-clock bound: at the deadline, mark it and ask the session to stop until the
        job returns. The stop request is repeated because one that lands before the SDK's run loop
        starts is undone by it (:meth:`EntitySession.request_stop`)."""
        assert self._turn_seconds is not None
        if done.wait(self._turn_seconds):
            return
        with self._lock:
            # `done`, not the job's status: the status changes only after the worker has joined
            # this thread, and a refused job's `done` is set under this lock (L2 review: a refused
            # job's watcher stopped the session's NEXT turn).
            if done.is_set():
                return
            job.deadline_hit = True
        _log.warning("chat %s job %s passed its %ss deadline; stopping it",
                     job.kind, job.job_id, self._turn_seconds)
        if driver is None:
            return      # nothing to stop: _start drives only a session that holds a driver
        while True:
            got = driver.call("interrupt", lane="stop", timeout=_CALL_SECONDS)
            if not got.ok:     # keep asking; a dead watcher is no bound
                _log.error("chat job %s: stop request failed: %s", job.job_id, got.error)
            if done.wait(1.0):
                return

    def _run_open(self, rec: _Session, job: _Job) -> None:
        error: str | None = None
        driver: _DriverProxy | None = None
        opening = False         # open() was called: from here on the driver may hold something to release
        info: dict[str, Any] = {}
        try:
            try:
                lanes = _DriverProxy(rec.session_id)
            except RuntimeError:
                raise _FailedStart("could not start the driver's workers; try again") from None
            # The factory makes driver code, so it runs on the driver's lane and through the boundary too; a
            # raise from it is an ordinary failed open (its text kept, read so that it cannot raise).
            made = lanes.make(self._driver_factory, rec.entity, self._entities[rec.entity], timeout=_OPEN_SECONDS)
            if not made.ok:
                lanes.retire()
                raise _FailedStart(made.message or "the driver could not be made")
            driver = lanes
            if driver.caps.approval_timing != "after_turn":
                # Reserved in the contract, not driven here: an in-turn consent request needs the approval
                # state machine (chat_driver module docstring, slice S10). Never opened, it is not closed
                # either: its lanes are retired below (a driver acquires nothing before open()).
                raise DriverContractError(
                    f"{driver.harness}: this host drives only after-turn consent; "
                    f"{driver.caps.approval_timing!r} needs the approval state machine, which is not built")
            opening = True
            opened = driver.call("open", self._route_events(rec), lane="turn", timeout=_OPEN_SECONDS,
                                 on_released=self._released_sink([driver]))
            if not opened.ok:
                error = opened.message
            else:
                # A banner that cannot be read fails the open: the operator would be shown a session whose
                # floor nobody could state. It is copied into plain data on the driver's lane: the view is
                # built under the host lock, and a dict subclass (or a str subclass in it) would run driver
                # code there.
                described = driver.call("describe", timeout=_CALL_SECONDS, convert=_plain_banner)
                if not described.ok:
                    error = f"{driver.harness}: describe() could not be read: {described.message}"
                else:
                    info = described.value
        except BaseException as exc:  # noqa: BLE001 — a failed start is a RESULT; keep its TEXT only
            name, text = _exc_text(exc)
            error = text or name
        if error is not None and driver is not None and not opening:
            driver.retire()          # made but never opened: it holds nothing to release
            driver = None
        if error is not None and driver is not None:
            # A failed open releases what it built. The job's outcome is published before the close (a
            # client polling it does not wait on a teardown); the record reads closing, counted, until the
            # release is confirmed (concurrent.futures' shutdown(wait=False): return now, free on completion).
            failed = error
            gc.collect()      # see _settle_open
            with self._lock:
                rec.state = "closing"     # still counted while its shell is released

            def _job_failed() -> None:
                with self._lock:
                    if job.status == "running":
                        job.status, job.error = "failed", failed
                    if rec.job_id == job.job_id:
                        rec.job_id = None

            def _failed() -> None:
                with self._lock:
                    rec.state, rec.error = "failed", failed

            self._close_then(rec, driver, _failed, _job_failed)
            return
        self._settle_open(rec, job, error, driver, info)

    def _settle_open(self, rec: _Session, job: _Job, error: str | None, driver: _DriverProxy | None,
                     info: dict[str, Any]) -> None:
        # `exc` is unbound by now (Python deletes it at the end of the except clause), so nothing in
        # this frame still references the traceback of the failed start. The SDK keeps that failure
        # in a reference cycle (module docstring), so collect it now, BEFORE the failure is published:
        # a client that sees "failed" must not still have the failed hands alive behind it.
        if error is not None:
            gc.collect()
        with self._lock:
            # ONE decision, under the lock, so it cannot disagree with a shutdown that lands
            # alongside it (r2, all three seats: an open finishing as shutdown landed reported
            # done/idle for a session already closed).
            accepted = error is None and not self._shut
            if error is not None:
                rec.state, rec.error = "failed", error
                job.status, job.error = "failed", error
                rec.job_id = None
            elif accepted:
                assert driver is not None
                rec.driver = driver
                rec.state = "idle"
                rec.info = info
                rec.job_id = None
                job.status, job.result = "done", {"session": self._session_view(rec)}
            # else: shut while opening. shutdown() skipped this record (it was opening), so the worker
            # closes it, and the record keeps reading "opening" (counted) until the release is confirmed.
        if error is None and not accepted:
            assert driver is not None

            def _job_shut() -> None:
                with self._lock:
                    if job.status == "running":
                        job.status, job.error = "failed", "the server shut down while the session opened"
                    if rec.job_id == job.job_id:
                        rec.job_id = None

            self._close_then(rec, driver, lambda: self._settle(rec, "closed"), _job_shut)

    def _route_events(self, rec: _Session) -> Callable[[DriverEvent], None]:
        """The driver's event sink. Bound once at open, it forwards each tool-activity line
        to whichever job is driving the session at that moment, so streaming works for every turn.
        A kind of event this host does not know is ignored (the contract says a consumer must)."""

        def _emit(event: DriverEvent) -> None:
            # Exact types, read and capped OUTSIDE the lock: a str subclass's __str__ is driver code (L1 r5).
            if type(event) is not DriverEvent:
                return
            kind, text = event.kind, event.text
            if type(kind) is not str or kind != "activity" or type(text) is not str:
                return
            line = _cap_line(text)
            with self._lock:
                job = self._jobs.get(rec.job_id) if rec.job_id else None
                if job is None or job.status != "running":
                    return
                job.activity.append(line)
                if len(job.activity) > MAX_ACTIVITY_LINES:
                    del job.activity[0]
                    job.dropped += 1

        return _emit

    def _run_job(
        self,
        rec: _Session,
        job: _Job,
        call: tuple[Any, ...],
        done: threading.Event,
        watcher: threading.Thread | None,
    ) -> None:
        payload: dict[str, Any] | None = None
        error: str | None = None
        digest: str | None = None
        cut = 0
        try:
            try:
                driver = rec.driver
                assert driver is not None
                # `call` is (method, *args); the turn's one terminal record, exactly a TurnSnapshot (a subclass
                # could run driver code in the host). The host waits past the turn's own bound by a grace for
                # the step in flight; a driver that does not answer by then fails the turn.
                limit = (self._turn_seconds or _UNBOUNDED_TURN_SECONDS) + _TURN_GRACE_SECONDS
                got = driver.call(*call, lane="turn", timeout=limit, expect=(TurnSnapshot,))
                snap = got.value           # checked where the driver built it
                if not got.ok:
                    error = got.error
                else:
                    payload = _snapshot_payload(snap)
                    digest = snap.held_digest
                    cut = max(0, len(snap.tool_activity) - MAX_ACTIVITY_LINES)
            except BaseException as exc:  # noqa: BLE001 — the turn methods return results; this is a backstop
                error = ": ".join(_exc_text(exc))
        finally:
            done.set()
        if watcher is not None:
            # The next job must not start while this one's stop request can still arrive: it
            # would land in that job's turn. A watcher that does not exit breaks the session.
            watcher.join(_WATCHER_JOIN_SECONDS)
            if watcher.is_alive() and error is None:
                payload, error = None, "the turn's deadline watcher did not exit"
        stopping = rec.driver
        if stopping is not None and not stopping.stop_idle() and error is None:
            # A stop request that outlived its deadline is still with the driver: it would land in the NEXT
            # turn (L1 r5, RAN). The same rule as a watcher that did not exit.
            payload, error = None, "the driver did not answer a stop request"
        broken = payload is None or payload["error"] is not None
        dead: _DriverProxy | None = None
        if broken:
            # Release the shell BEFORE the session reads broken (and stops counting toward the cap),
            # so the cap can never be exceeded by a teardown still in progress (codex L3 r1): the
            # record reads busy until the driver confirms the release.
            with self._lock:
                dead, rec.driver = rec.driver, None
                if dead is not None:
                    # why it broke, kept if its release then fails too (complement r4)
                    rec.error = error if payload is None else payload["error"]

        def _publish() -> None:
            to_close: _DriverProxy | None = None
            final: SessionState = "closed"
            with self._lock:
                if payload is None:
                    rec.state, rec.error = "broken", error
                    job.status, job.error = "failed", error
                else:
                    job.status, job.result = "done", payload
                    # The result's tool_activity leaves out held and stop-skipped actions; it replaces
                    # what was streamed on every finish (module docstring).
                    job.activity, job.dropped = list(payload["tool_activity"]), cut
                    rec.decision_id, rec.pending, rec.held_digest, rec.approvable = None, [], None, False
                    if payload["gated"] and payload["error"] is None:
                        rec.state = "gated"
                        rec.decision_id = secrets.token_hex(16)
                        rec.pending = [dict(p) for p in payload["pending"]]
                        rec.held_digest = digest if isinstance(digest, str) else None
                        # Approvable only when every held call is shown in full and the hold names its bytes; a
                        # client offers Approve only then (complement release-range review: an id that could only
                        # be refused offered a dead-end Approve).
                        rec.approvable = bool(rec.pending) and _names_bytes(rec.held_digest) and all(
                            shown_in_full(p.get("full")) for p in rec.pending)
                        payload["decision_id"] = rec.decision_id
                        payload["approvable"] = rec.approvable
                    elif payload["error"] is not None:
                        # A turn that raised or could not read its own gate leaves the conversation in a
                        # state a later turn would resume FROM (EXIT_TURN_FAILED's contract), and a
                        # refusal that did not take is still holding actions. Either way the session
                        # takes no further turn, and its shell is released now.
                        rec.state, rec.error = "broken", payload["error"]
                    else:
                        rec.state = "idle"
                if rec.state == "broken" or self._shut:
                    to_close, rec.driver = rec.driver, None
                    final = "closed" if self._shut else rec.state
                    if self._shut and to_close is not None:
                        rec.state = "closing"     # counted until the shell is released, below
                    elif self._shut:
                        rec.state = "closed"
                rec.job_id = None
            if to_close is not None:
                self._close_then(rec, to_close, lambda: self._settle(rec, final))

        if dead is None:
            _publish()
            return
        # The job's outcome is published before the close (a client polling it does not wait on a teardown); the record
        # reads busy, counted, until the release is confirmed, then broken (closed if the server shut).
        text = error if payload is None else payload["error"]

        def _job_ended() -> None:
            with self._lock:
                if job.status == "running":
                    if payload is None:
                        job.status, job.error = "failed", error
                    else:
                        job.status, job.result = "done", payload
                        job.activity, job.dropped = list(payload["tool_activity"]), cut
                rec.decision_id, rec.pending, rec.held_digest, rec.approvable = None, [], None, False
                if rec.job_id == job.job_id:
                    rec.job_id = None

        def _broken() -> None:
            with self._lock:
                rec.state, rec.error = ("closed" if self._shut else "broken"), text

        self._close_then(rec, dead, _broken, _job_ended)

    @staticmethod
    def _session_view(rec: _Session) -> dict[str, Any]:
        out: dict[str, Any] = {
            "session_id": rec.session_id,
            "entity": rec.entity,
            "state": rec.state,
            "job_id": rec.job_id,
        }
        # The driver's banner fields never overwrite the host's own (a driver's `state` or `session_id`
        # key would otherwise make a client read, or address, the wrong thing).
        out.update((k, v) for k, v in rec.info.items() if k not in _HOST_VIEW_KEYS)
        if rec.error is not None:
            out["error"] = rec.error
        if rec.release_failed_since is not None:
            out["release_failed_since"] = rec.release_failed_since
        if rec.released_late is not None:
            out["released_late"] = rec.released_late
        return out

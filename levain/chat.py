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
(still counted, refusing every operation), tears it down outside the lock, and only then publishes
``closed``; an open that lands after shutdown is torn down while it still reads ``opening``. So a
slow teardown can never let an ``open`` exceed ``max_sessions`` (codex L3 r2). "Released" is what the
driver CONFIRMS (:meth:`~levain.chat_driver.HarnessDriver.wait_closed`): a release still in flight past
the driver's close bound is waited for by the host's reaper, and the record stays counted meanwhile.

**A release that failed is not a release** (ruled 2026-10-07). When a driver's close raises, or it reports
a failed release (after its own escalation, where it has one, failed too), or its release cannot be
confirmed (a probe that raises or blocks past its bound), the session ends ``release_failed``: still
counted, refusing every operation, and listed in ``/chat.json`` with its error and
``release_failed_since``. Its shell may still be live, so its slot is not handed to a new session.
Restarting ``levain serve`` ends it (the process that held the shell exits); nothing else does. An
OpenHands session reaches this state only through a fault: :meth:`EntitySession.close` never raises.

**Every call into driver code goes through ONE boundary** (:func:`levain.chat_driver._call_driver`, ruled
2026-10-07). The host holds a driver only as a :class:`_DriverProxy`, which exposes none of the driver's
methods, so a call that bypasses the boundary cannot be written. Nothing a driver raises (any
``BaseException``) escapes into a request, worker, shutdown or reaper thread; the call site records what
the failure means. No release depends on a thread starting: the reaper is started with the host and an
OpenHands driver's release worker with its open. The threads a teardown does start (shutdown's closers,
an OpenHands close's stop requests) each have a failure path that leaves the session counted, and a close
is never run in place of a closer that could not start.

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
import functools
import gc
import logging
import math
import secrets
import threading
import time
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

SessionState = Literal["opening", "idle", "busy", "gated", "closing", "release_failed", "broken", "failed",
                       "closed"]
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

_LIVE_STATES = ("opening", "idle", "busy", "gated", "closing", "release_failed")
"""The states that hold, or are about to hold, a conversation. Only these count toward the cap. A
broken session's shell is released by the worker BEFORE it is marked broken."""

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

_REAP_POLL_SECONDS = 60.0
"""How often the reaper logs a release that is still in flight."""

_REAP_SWEEP_SECONDS = 0.25
"""How often the reaper asks each pending driver whether it has released (a non-blocking ask)."""

_PROBE_SECONDS = 5.0
"""The deadline on one release probe (``wait_closed(0)``, ``release_error()``), which the contract says
does not block. It classifies, it does not bound: a probe runs on the asking thread (on the reaper, the one
every session shares). One that answers later than this without confirming the release counts as failed
(``release_failed``) and is not asked again; a late confirmation still settles, since the driver says the
shell is gone. One that never answers holds that thread (the contract forbids it)."""

_SHUTDOWN_JOIN_SECONDS = 60.0
"""How long :meth:`ChatHost.shutdown` waits for its side-by-side closes. Each is bounded by its driver;
this bounds a driver that does not keep that promise (its record stays ``closing``, counted)."""


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
        return got.value if got.ok else None


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


def _plain_banner(described: dict[Any, Any]) -> dict[str, Any]:
    """A driver's banner as plain JSON data: exact ``str`` keys, and values of exactly ``str``, ``bool``,
    ``int``, a finite ``float``, or ``None``. Anything else is dropped, never converted (a conversion runs
    the driver's own code; JSON has no NaN)."""
    return {k: v for k, v in dict.items(described)
            if type(k) is str and (v is None or type(v) in (str, bool, int)
                                   or (type(v) is float and math.isfinite(v)))}


_HOST_VIEW_KEYS = frozenset({"session_id", "entity", "state", "job_id", "error", "release_failed_since",
                             "last_job"})
"""The keys a session view takes from the host only, never from a driver's :meth:`describe`."""


class _DriverProxy:
    """How the host holds a driver. It exposes none of :class:`~levain.chat_driver.HarnessDriver`'s methods,
    so a direct call (one that could let a driver's exception, a ``TurnTimeout`` included, escape into host
    or reaper code) cannot be written: it would be an ``AttributeError``. :meth:`call` and :meth:`read` are
    :func:`~levain.chat_driver._call_driver` and nothing else (ruled 2026-10-07)."""

    __slots__ = ("_driver", "harness", "caps")

    def __init__(self, driver: HarnessDriver) -> None:
        self._driver = driver
        harness, caps = self.read("harness"), self.read("caps")
        self.harness: str = harness.value if harness.ok and isinstance(harness.value, str) else "unnamed"
        self.caps: DriverCaps = caps.value if caps.ok and isinstance(caps.value, DriverCaps) else DriverCaps()

    def call(self, method: str, *args: Any, deadline: float | None = None) -> DriverCall:
        bound = self.read(method)     # the lookup is driver code too (a __getattr__, a property)
        return _call_driver(bound.value, *args, deadline=deadline) if bound.ok else bound

    def read(self, attribute: str) -> DriverCall:
        return _call_driver(getattr, self._driver, attribute)


@dataclass(eq=False)
class _Reaping:
    """A closed driver whose release is still in flight, and what to run once it is confirmed."""

    rec: _Session
    driver: _DriverProxy
    settle: Callable[[], None]
    logged: float
    failure: str | None = None    # the close itself failed: final, whatever the driver says later
    publish_job: Callable[[], None] | None = None   # the job's own outcome: idempotent (acts while running)


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
        # The reaper is acquired HERE, before any session exists for it to wait on: no release has to
        # create a thread, so none can fail to (a host that cannot start it is not built).
        self._reap_cond = threading.Condition()
        self._reaping: list[_Reaping] = []
        self._reaper_thread = threading.Thread(target=self._reaper, daemon=True, name="levain-chat-reaper")
        self._reaper_thread.start()

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
        itself) and while another close is tearing it down. A failed session stays ``failed``; any
        other ends ``closed``, published only after its shell is released (module docstring)."""
        with self._lock:
            rec = self._get(session_id)
            if rec.state in ("opening", "busy", "closing"):
                raise ChatError(
                    "busy", f"the session is {rec.state}; close it when that finishes", 409)
            driver, rec.driver = rec.driver, None
            if driver is None:
                if rec.state not in ("failed", "release_failed"):
                    rec.state = "closed"      # a release that failed stays counted: only a restart ends it
                return self._session_view(rec)
            rec.state = "closing"
        self._close_then(rec, driver, lambda: self._settle(rec, "closed"))
        with self._lock:
            return self._session_view(rec)

    def shutdown(self) -> None:
        """Close every idle or gated session and stop accepting work. The closes run side by side, so
        this returns within about one driver's close bound, not one per session; a release that outlives
        it is finished by the reaper and the record reads ``closing`` until then. A job still running
        is not interrupted; its worker closes the session when the job ends, and if the process exits
        first the SDK closes the conversation at interpreter exit."""
        with self._lock:
            self._shut = True
            to_close = []
            for rec in self._sessions.values():
                if rec.state not in ("opening", "busy", "closing") and rec.driver is not None:
                    to_close.append((rec, rec.driver))
                    rec.driver = None
                    rec.state = "closing"
        closers = []
        for rec, s in to_close:
            closer = threading.Thread(target=self._close_then,
                                      args=(rec, s, functools.partial(self._settle, rec, "closed")),
                                      daemon=True, name="levain-chat-shutdown")
            try:
                closer.start()
            except RuntimeError:
                # Never closed inline (a driver's close is outside this method's bound, and the next
                # session's close would wait on it): the session is left unclosed and counted, and says why.
                self._mark_release_failed(rec, "closer could not start (shutdown could not start a thread to close it)")
            else:
                closers.append(closer)
        with self._reap_cond:
            self._reap_cond.notify_all()      # the reaper re-checks whether it may exit
        give_up = time.monotonic() + _SHUTDOWN_JOIN_SECONDS
        for closer in closers:
            # Each close is bounded by its driver and they run together; this bounds one that is not.
            closer.join(max(0.0, give_up - time.monotonic()))

    # -- internals -----------------------------------------------------------

    def _settle(self, rec: _Session, state: SessionState) -> None:
        with self._lock:
            rec.state = state

    def _close_then(self, rec: _Session, driver: _DriverProxy, settle: Callable[[], None],
                    publish_job: Callable[[], None] | None = None) -> None:
        """Close ``driver``, then run ``settle`` (it takes the lock itself) once the driver CONFIRMS the
        release (:meth:`~levain.chat_driver.HarnessDriver.wait_closed`). The caller leaves the record in a
        counted state until then. ``close()`` is bounded by the driver; a release still in flight is
        handed to the reaper, never waited for on the caller's thread. A release that FAILED is not a
        release (ruled 2026-10-07): a close that raised, or a driver that reports a failed release or
        cannot answer, ends ``release_failed``, still counted (module docstring). Every call into the
        driver goes through the boundary, so nothing a driver raises leaves this method.

        ``publish_job`` publishes the job's own outcome, BEFORE the close: a client polling the job never
        waits on a teardown (concurrent.futures' shutdown(wait=False) shape; glm L3 r2). The record is
        settled when the release ends."""
        if publish_job is not None:
            publish_job()         # idempotent: the settle that ends the record runs it again, harmlessly
        closed = driver.call("close")
        item = _Reaping(rec, driver, settle, time.monotonic(),
                        failure=None if closed.ok else f"close: {closed.error}", publish_job=publish_job)
        if not self._after_close(item):
            with self._reap_cond:
                self._reaping.append(item)
                self._reap_cond.notify_all()

    def _after_close(self, item: _Reaping) -> bool:
        """One non-blocking look at a closed driver. ``True`` when ``item`` is finished: settled (the
        release is confirmed) or marked ``release_failed``. ``False`` while the release is in flight. It
        never escalates a failed release: a driver that can does so on its own thread, never on the reaper
        every session shares. Each probe carries a deadline (:data:`_PROBE_SECONDS`, a classifier: a probe
        that never answers holds the asking thread, which the contract forbids). Every value a driver
        returns is checked for its type here, outside any lock: a probe that answers with the wrong type is
        a failed release, never "still releasing"."""
        if item.failure is not None:
            # A close that raised is not a release, whatever the driver says afterwards (ruled 2026-10-07).
            self._mark_release_failed(item.rec, item.failure, item.publish_job)
            return True
        driver = item.driver
        released = driver.call("wait_closed", 0, deadline=time.monotonic() + _PROBE_SECONDS)
        if released.value is True:
            # Confirmed, even if late (``ok`` False): the driver says the shell is gone, so the slot is free.
            if not released.ok:
                _log.error("chat session %s: %s", item.rec.session_id, released.error)
            self._run_settle(item.rec, item.settle, item.publish_job)
            return True
        if not released.ok:
            failure: str | None = f"wait_closed: {released.error}"
        elif released.value is not False:
            failure = (f"wait_closed: DriverContractError: returned {type(released.value).__name__}, "
                       "not a bool")
        else:
            reported = driver.call("release_error", deadline=time.monotonic() + _PROBE_SECONDS)
            if not reported.ok:
                failure = f"release_error: {reported.error}"
            elif reported.value is None or isinstance(reported.value, str):
                failure = None if reported.value is None else str.__str__(reported.value)   # no subclass __str__
            else:
                failure = (f"release_error: DriverContractError: returned {type(reported.value).__name__}, "
                           "not text")
        if failure is None:
            return False                      # in flight, and nothing has failed
        self._mark_release_failed(item.rec, failure, item.publish_job)
        return True

    def _mark_release_failed(self, rec: _Session, failure: str,
                             publish_job: Callable[[], None] | None = None) -> None:
        if publish_job is not None:
            self._run_settle(rec, publish_job)    # the job keeps its own outcome
        _log.error("chat session %s: its release failed, so it stays counted until the server restarts: %s",
                   rec.session_id, failure)
        with self._lock:
            job = self._jobs.get(rec.job_id) if rec.job_id else None
            if job is not None and job.status == "running":
                job.status, job.error = "failed", f"the session's release failed: {failure}"
            rec.job_id = None
            # the error that ended the session (a broken turn's, a failed open's) is kept, not replaced
            rec.state = "release_failed"
            rec.error = failure if rec.error is None else f"{rec.error}; then its release failed: {failure}"
            rec.release_failed_since = datetime.now(timezone.utc).isoformat(timespec="seconds")

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
                if rec.state in ("opening", "busy", "closing"):
                    ended: SessionState = "failed" if job is not None and job.kind == "open" else "broken"
                    rec.state, rec.error = ended, text

    def _reaper_may_exit(self) -> bool:
        """Shut, nothing queued, and no record that could still hand the reaper a driver. Caller holds
        ``_reap_cond``; this takes the host lock (the order everywhere is reap_cond, then the host lock)."""
        if self._reaping:
            return False
        with self._lock:
            return self._shut and all(
                rec.driver is None and rec.state not in ("opening", "busy", "closing")
                for rec in self._sessions.values())

    def _reaper(self) -> None:
        """The host's one reaper: asks every driver whose release is in flight, every
        ``_REAP_SWEEP_SECONDS``, without blocking on any of them (every ask goes through the driver
        boundary, so nothing a driver raises can stop it), and settles each as it ends. Never gives up on
        one: a release that never ends stays counted, which is the true state of the machine. It exits
        once the host is shut and nothing is left that could hand it a driver."""
        while True:
            with self._reap_cond:
                while not self._reaping:
                    if self._reaper_may_exit():
                        return
                    # shut hosts re-check on a timer: a job finishing after shutdown may close its driver
                    # without queueing anything (a release confirmed at once)
                    self._reap_cond.wait(_REAP_SWEEP_SECONDS if self._shut else None)
                pending = list(self._reaping)
            done = []
            for item in pending:
                try:
                    finished = self._after_close(item)
                except BaseException as exc:  # noqa: BLE001 — host code; one bad record must not stop the reaper
                    # Never retried: a fault that repeats would spin here forever (3237fea's class). The
                    # record ends release_failed, counted, which is all that is known.
                    _log.error("chat session %s: reaping failed: %s", item.rec.session_id, type(exc).__name__)
                    finished = True
                    try:
                        self._mark_release_failed(item.rec, f"reaping failed: {type(exc).__name__}")
                    except BaseException:  # noqa: BLE001
                        pass
                if finished:
                    done.append(item)
                elif time.monotonic() - item.logged >= _REAP_POLL_SECONDS:
                    _log.warning("chat session %s: still releasing its shell", item.rec.session_id)
                    item.logged = time.monotonic()
            with self._reap_cond:
                self._reaping = [i for i in self._reaping if i not in done]
                if self._reaping:
                    self._reap_cond.wait(_REAP_SWEEP_SECONDS)

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
        got = driver.call("held_digest") if driver is not None else None
        # unreadable, or not exactly text (a subclass could override `==`): reject-only
        live = got.value if got is not None and got.ok and type(got.value) is str else None
        to_close: _DriverProxy | None = None
        with self._lock:
            rec.state = "gated"
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
            got = driver.call("interrupt")
            if not got.ok:     # keep asking; a dead watcher is no bound
                _log.error("chat job %s: stop request failed: %s", job.job_id, got.error)
            if done.wait(1.0):
                return

    def _run_open(self, rec: _Session, job: _Job) -> None:
        error: str | None = None
        driver: _DriverProxy | None = None
        info: dict[str, Any] = {}
        try:
            # The factory makes driver code, so it is called through the boundary too; a raise from it is an
            # ordinary failed open (its text kept, read so that it cannot raise).
            made = _call_driver(self._driver_factory, rec.entity, self._entities[rec.entity])
            if not made.ok:
                raise _FailedStart(made.message or "the driver could not be made")
            driver = _DriverProxy(made.value)
            if driver.caps.approval_timing != "after_turn":
                # Reserved in the contract, not driven here: an in-turn consent request needs the approval
                # state machine (chat_driver module docstring, slice S10). It is closed below, unopened.
                raise DriverContractError(
                    f"{driver.harness}: this host drives only after-turn consent; "
                    f"{driver.caps.approval_timing!r} needs the approval state machine, which is not built")
            opened = driver.call("open", self._route_events(rec))
            if not opened.ok:
                error = opened.message
            else:
                # Read here, outside the host lock (describe() is driver code). A banner that cannot be read
                # fails the open: the operator would be shown a session whose floor nobody could state.
                described = driver.call("describe")
                if not described.ok:
                    error = described.message
                elif not isinstance(described.value, dict):
                    error = f"{driver.harness}: describe() returned {type(described.value).__name__}, not a dict"
                else:
                    # Copied into plain data inside the boundary: the view is built under the host lock, and
                    # a dict subclass (or a str subclass in it) would run driver code there.
                    banner = _call_driver(_plain_banner, described.value)
                    if banner.ok:
                        info = banner.value
                    else:
                        error = f"{driver.harness}: describe() could not be read: {banner.error}"
        except BaseException as exc:  # noqa: BLE001 — a failed start is a RESULT; keep its TEXT only
            name, text = _exc_text(exc)
            error = text or name
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
            if not isinstance(event, DriverEvent) or event.kind != "activity":
                return
            line = event.text
            with self._lock:
                job = self._jobs.get(rec.job_id) if rec.job_id else None
                if job is None or job.status != "running":
                    return
                job.activity.append(_cap_line(line))
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
                got = driver.call(*call)   # `call` is (method, *args); the turn's one terminal record
                snap = got.value           # checked where the driver built it
                if not got.ok:
                    error = got.error
                elif type(snap) is not TurnSnapshot:      # a subclass could run driver code in the host
                    raise DriverContractError(
                        f"{driver.harness}: a turn returned {type(snap).__name__}, not a TurnSnapshot")
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
        return out

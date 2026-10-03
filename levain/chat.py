"""levain.chat — the CHAT HOST: entity conversations held in server memory (K1 part 2).

``levain serve --chat <entity>`` is the third driver over :class:`levain.session.EntitySession`
(after the REPL and ``levain run --task``). This module is the server-side half that has no HTTP in
it: a registry of live sessions, the propose→job→poll runtime that drives their turns off the
request thread, and the state machine that says which operations a session accepts right now.

**The client never supplies an agent, a tool spec, a model or a mode.** It names an entity the
OPERATOR registered at startup, and it sends message text. Everything that shapes the agent comes
from the operator's command line and from :meth:`EntitySession.open`, which builds the agent itself
through ``build_entity_agent``. This is the K1-part-2 requirement of the approved binding design
(``spore-438``, Phill 2026-10-02): the crown-jewels floor lives in the hands' tool-spec params, so a
client-supplied spec would be a client writing its own floor. There is no code path here that
accepts one, which is a stronger claim than a validator that refuses one.

**The drive mode is ``headless``, decided by the server.** The binding design (section 7.1) gives
``interactive`` to an *authenticated human at a chat client*. These routes authenticate nobody (any
local process that passes the loopback, Host and CSRF checks can drive a session, and activity is
pull-only, so nothing guarantees anyone reads it), so the precondition is not met and the server
does not claim it. In ``headless`` an entity whose ``efferent_gate`` is ``"auto"`` is GATED: a turn
that proposes an efferent action halts, and the client approves or rejects it. The crown-jewels cred
floor is the same for ``headless`` as for ``interactive`` (:mod:`levain.firing.drive`), so this
choice moves the gate and nothing else. (L1 review 2026-10-03; if the chat routes gain an
authenticated client, that is the moment to revisit it.)

**An entity that may connect to localhost is refused.** With ``allow_localhost_outbound`` its shell
could call these routes itself: list the sessions, approve its own held actions, drive the other
entities. Every other entity's shell is denied loopback by its floor. (L2 review 2026-10-03.)

**Activity is what was ISSUED; the result is what RAN.** A job's ``activity`` grows as the entity
issues tool actions, which for a gated action is before the gate stops it, so while the turn runs a
poll can show a line for an action that will be held. When the turn finishes gated, ``activity`` is
replaced by the result's ``tool_activity`` (held actions removed); the held ones are in ``pending``.

**A turn has no wall-clock bound here.** ``--max-iterations`` bounds its steps and the SDK's HTTP
timeout bounds each model call, but a turn is not cancelled at a deadline: the SDK's synchronous run
can only be paused between steps, and a paused turn needs the session to classify it as timed out
and not capture it, which is not built yet. A turn that never returns keeps its session ``busy``.
Workers are daemon threads, so stopping the server does not wait for one; the SDK closes every live
conversation, and its shell, at interpreter exit.

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

import gc
import logging
import secrets
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Literal, Mapping

if TYPE_CHECKING:
    from levain.firing.drive import DriveMode

__all__ = [
    "CHAT_DRIVE_MODE",
    "ChatError",
    "ChatHost",
    "MAX_ACTIVITY_LINES",
    "MAX_LINE_CHARS",
    "MAX_MESSAGE_CHARS",
    "chat_refusal",
]

_log = logging.getLogger("levain.chat")

SessionState = Literal["opening", "idle", "busy", "gated", "broken", "failed", "closed"]
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

_LIVE_STATES = ("opening", "idle", "busy", "gated")
"""The states that hold, or are about to hold, a conversation. Only these count toward the cap; a
broken session's conversation is closed by the worker right after it is marked."""

_FINISHED_JOBS_KEPT = 200
"""How many finished jobs stay pollable. Older finished jobs are forgotten (a poll answers
``unknown``); a running job is never dropped."""

DEFAULT_MAX_SESSIONS = 4
"""Live sessions per server. Each holds a conversation and, once used, a sandboxed shell."""


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
    except ConfinementError:
        return None
    if cfg.allow_localhost_outbound:
        return (
            f"{entity_dir} allows its shell to connect to localhost (allow_localhost_outbound), so "
            "it could call this server's chat routes itself, approve its own held actions and drive "
            "the other entities. Chat refuses to host it; use `levain run` for this entity."
        )
    return None


def _cap_line(line: Any) -> str:
    text = str(line)
    return text if len(text) <= MAX_LINE_CHARS else text[:MAX_LINE_CHARS] + " …"


@dataclass
class _Job:
    job_id: str
    session_id: str
    kind: JobKind
    status: JobStatus = "running"
    activity: list[str] = field(default_factory=list)
    dropped: int = 0
    result: dict[str, Any] | None = None
    error: str | None = None


@dataclass
class _Session:
    session_id: str
    entity: str
    state: SessionState = "opening"
    session: Any = None          # the EntitySession once open; None before and after
    error: str | None = None     # why it failed or broke, as text (see the module docstring)
    job_id: str | None = None    # the job currently driving it, if any
    info: dict[str, Any] = field(default_factory=dict)


def _turn_payload(result: Any) -> dict[str, Any]:
    """A :class:`~levain.session.TurnResult` as JSON-shaped data. ``ok`` and ``exit_code`` are the
    result's own derived properties, so a client reads the harness's classification rather than
    re-deriving it."""
    activity = [_cap_line(x) for x in result.tool_activity]
    return {
        "reply": result.reply,
        "tool_activity": activity[-MAX_ACTIVITY_LINES:],
        "error": result.error,
        "nudged": result.nudged,
        "gated": result.gated,
        "timed_out": result.timed_out,
        "pending": [
            {"tool": p.tool_name, "detail": p.detail, "reason": p.reason,
             "recognized": p.recognized}
            for p in result.pending
        ],
        "ok": result.ok,
        "exit_code": result.exit_code,
    }


def _default_factory(
    *, model: str, base_url: str, api_key: str | None, max_iterations: int | None
) -> Callable[..., Any]:
    def _open(entity_dir: Path, *, on_event: Callable[[str], None]) -> Any:
        from levain.session import EntitySession

        refusal = chat_refusal(entity_dir)
        if refusal is not None:
            raise ChatError("refused_entity", refusal, 403)
        return EntitySession.open(
            entity_dir,
            model=model,
            base_url=base_url,
            api_key=api_key,
            with_tools=True,
            on_event=on_event,
            max_iterations=max_iterations,
            mode=CHAT_DRIVE_MODE,
        )

    return _open


class ChatHost:
    """Live entity sessions for one server process, driven by jobs.

    ``entities`` maps a NAME to an entity directory; it is fixed at construction and is the whole
    of what a client can address. ``session_factory`` is a test seam; production passes ``None`` and
    gets :meth:`EntitySession.open` with the operator's model settings and :data:`CHAT_DRIVE_MODE`.
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
        session_factory: Callable[..., Any] | None = None,
    ) -> None:
        if not entities:
            raise ValueError("a chat host needs at least one entity")
        if max_sessions < 1:
            raise ValueError("max_sessions must be at least 1")
        self._entities = {name: Path(p) for name, p in entities.items()}
        self._factory = session_factory or _default_factory(
            model=model, base_url=base_url, api_key=api_key, max_iterations=max_iterations
        )
        self._model = model
        self._max_sessions = max_sessions
        self._lock = threading.Lock()
        self._sessions: dict[str, _Session] = {}
        self._jobs: dict[str, _Job] = {}
        self._shut = False

    # -- reads ---------------------------------------------------------------

    def listing(self) -> dict[str, Any]:
        with self._lock:
            return {
                "entities": sorted(self._entities),
                "model": self._model,
                "drive_mode": CHAT_DRIVE_MODE,
                "max_sessions": self._max_sessions,
                "sessions": [self._session_view(s) for s in self._sessions.values()],
            }

    def session_status(self, session_id: Any) -> dict[str, Any]:
        with self._lock:
            return self._session_view(self._get(session_id))

    def job_status(self, job_id: str) -> dict[str, Any]:
        with self._lock:
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
            }
            if job.result is not None:
                out["result"] = dict(job.result)
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
            for old in ended[: max(0, len(ended) - _ENDED_SESSIONS_KEPT + 1)]:
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
        return self._start(session_id, "turn", ("idle",), lambda s: s.run_turn(message))

    def approve(self, session_id: Any) -> dict[str, Any]:
        return self._start(session_id, "approve", ("gated",), lambda s: s.resume_turn())

    def reject(self, session_id: Any, reason: Any = None) -> dict[str, Any]:
        if reason is None:
            reason = "the operator declined this action"
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 2000:
            raise ChatError("bad_reason", "reason must be a non-empty string under 2000 chars", 400)
        return self._start(session_id, "reject", ("gated",), lambda s: s.reject_turn(reason))

    def close(self, session_id: Any) -> dict[str, Any]:
        """Close a session. Refused while a job is driving it (the turn would be torn down under
        itself). A failed session stays ``failed``; any other ends ``closed``."""
        with self._lock:
            rec = self._get(session_id)
            if rec.state in ("opening", "busy"):
                raise ChatError("busy", "the session is running a job; close it when it finishes", 409)
            session, rec.session = rec.session, None
            if rec.state != "failed":
                rec.state = "closed"
            view = self._session_view(rec)
        if session is not None:
            session.close()
        return view

    def shutdown(self) -> None:
        """Close every idle or gated session and stop accepting work. A job still running is not
        interrupted; its worker closes the session when the job ends, and if the process exits first
        the SDK closes the conversation at interpreter exit."""
        with self._lock:
            self._shut = True
            to_close = []
            for rec in self._sessions.values():
                if rec.state not in ("opening", "busy") and rec.session is not None:
                    to_close.append(rec.session)
                    rec.session = None
                    rec.state = "closed"
        for s in to_close:
            s.close()

    # -- internals -----------------------------------------------------------

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
            text = f"{type(exc).__name__}: {exc}"
            _log.error("chat %s job %s escaped its worker: %s", job.kind, job.job_id, text)
            to_close: Any = None
            with self._lock:
                if job.status == "running":
                    job.status, job.error = "failed", text
                if rec.job_id == job.job_id:
                    rec.job_id = None
                    to_close, rec.session = rec.session, None
                    rec.state, rec.error = ("failed" if job.kind == "open" else "broken"), text
            if to_close is not None:
                to_close.close()

    def _start(
        self,
        session_id: Any,
        kind: JobKind,
        accepts: tuple[SessionState, ...],
        call: Callable[[Any], Any],
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
            before = rec.state
            rec.state = "busy"
            job = self._new_job(rec, kind)
            if not self._spawn(self._run_job, rec, job, call):
                rec.state, rec.job_id = before, None
                del self._jobs[job.job_id]
                raise ChatError("busy", "could not start a worker; try again", 503)
        return {"session_id": rec.session_id, "job_id": job.job_id, "state": "busy"}

    def _run_open(self, rec: _Session, job: _Job) -> None:
        error: str | None = None
        session: Any = None
        try:
            session = self._factory(self._entities[rec.entity], on_event=self._route_events(rec))
        except BaseException as exc:  # noqa: BLE001 — a failed start is a RESULT; keep its TEXT only
            error = str(exc) or type(exc).__name__
        # `exc` is unbound here (Python deletes it at the end of the except clause), so nothing in
        # this frame still references the traceback of the failed start. The SDK keeps that failure
        # in a reference cycle (module docstring), so collect it now, BEFORE the failure is published:
        # a client that sees "failed" must not still have the failed hands alive behind it.
        if error is not None:
            gc.collect()
        with self._lock:
            if error is not None:
                rec.state, rec.error = "failed", error
                job.status, job.error = "failed", error
            elif self._shut:
                rec.state = "closed"
                job.status, job.error = "failed", "the server shut down while the session opened"
            else:
                rec.session = session
                rec.state = "idle"
                rec.info = self._describe(session)
                job.status, job.result = "done", {"session": self._session_view(rec)}
                session = None
            rec.job_id = None
        if session is not None:
            session.close()

    def _route_events(self, rec: _Session) -> Callable[[str], None]:
        """The session's ``on_event`` sink. Bound once at open, it forwards each tool-activity line
        to whichever job is driving the session at that moment, so streaming works for every turn."""

        def _emit(line: str) -> None:
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
        self, rec: _Session, job: _Job, call: Callable[[Any], Any]
    ) -> None:
        payload: dict[str, Any] | None = None
        error: str | None = None
        try:
            result = call(rec.session)
            payload = _turn_payload(result)
        except BaseException as exc:  # noqa: BLE001 — the turn methods return results; this is a backstop
            error = f"{type(exc).__name__}: {exc}"
        to_close: Any = None
        with self._lock:
            if payload is None:
                rec.state, rec.error = "broken", error
                job.status, job.error = "failed", error
            else:
                job.status, job.result = "done", payload
                if payload["gated"]:
                    # The result's tool_activity has the held actions removed; it replaces what was
                    # streamed, so nothing held stays listed as work (module docstring).
                    job.activity, job.dropped = list(payload["tool_activity"]), 0
                if payload["gated"] and payload["error"] is None:
                    rec.state = "gated"
                elif payload["error"] is not None:
                    # A turn that raised or could not read its own gate leaves the conversation in a
                    # state a later turn would resume FROM (EXIT_TURN_FAILED's contract), and a
                    # refusal that did not take is still holding actions. Either way the session
                    # takes no further turn, and its shell is released now.
                    rec.state, rec.error = "broken", payload["error"]
                else:
                    rec.state = "idle"
            if rec.state == "broken" or self._shut:
                to_close, rec.session = rec.session, None
                if self._shut:
                    rec.state = "closed"
            rec.job_id = None
        if to_close is not None:
            to_close.close()

    @staticmethod
    def _describe(session: Any) -> dict[str, Any]:
        """What the operator's banner would say about this session's floor, from the session's own
        resolved fields (never a second resolution)."""
        out: dict[str, Any] = {}
        for name in ("label", "model_label", "gate_mode", "bash_ok", "deny_standard_creds",
                     "bash_offline", "ssh_mode"):
            value = getattr(session, name, None)
            if isinstance(value, (str, bool)):
                out[name] = value
        workspace = getattr(session, "workspace", None)
        if workspace is not None:
            out["workspace"] = str(workspace)
        return out

    @staticmethod
    def _session_view(rec: _Session) -> dict[str, Any]:
        out: dict[str, Any] = {
            "session_id": rec.session_id,
            "entity": rec.entity,
            "state": rec.state,
            "job_id": rec.job_id,
            **rec.info,
        }
        if rec.error is not None:
            out["error"] = rec.error
        return out

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

**The drive mode is ``interactive``, decided by the server.** A person at a chat client reads the
turn's tool activity as it happens (the job's ``activity`` list grows during the turn and every poll
returns it), which is the fan-in the REPL's interactive mode stands for. The entity's own
``confinement.json`` still decides the efferent gate: an entity that declares ``"gated"`` halts here
exactly as in the REPL, and the routes expose approve and reject.

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
it kept the record.
"""
from __future__ import annotations

import logging
import secrets
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Literal, Mapping

__all__ = [
    "ChatError",
    "ChatHost",
    "MAX_ACTIVITY_LINES",
    "MAX_MESSAGE_CHARS",
]

_log = logging.getLogger("levain.chat")

SessionState = Literal["opening", "idle", "busy", "gated", "broken", "failed", "closed"]
JobKind = Literal["open", "turn", "approve", "reject"]
JobStatus = Literal["running", "done", "failed"]

MAX_MESSAGE_CHARS = 64_000
"""A message longer than this is refused with 413: it bounds what reaches the model's context from
one request, whatever the transport allows."""

MAX_ACTIVITY_LINES = 500
"""A job keeps at most this many streamed activity lines; past it the oldest are dropped and the
job says how many. A runaway turn must not grow server memory without bound."""

_ENDED_SESSIONS_KEPT = 100
"""How many ended sessions (closed, failed, broken) stay readable. Older ones are forgotten."""

_LIVE_STATES = ("opening", "idle", "busy", "gated")
"""The states that hold, or are about to hold, a conversation. Only these count toward the cap: a
broken session has already released its conversation and its shell."""

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
    return {
        "reply": result.reply,
        "tool_activity": list(result.tool_activity),
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

        return EntitySession.open(
            entity_dir,
            model=model,
            base_url=base_url,
            api_key=api_key,
            with_tools=True,
            on_event=on_event,
            max_iterations=max_iterations,
            mode="interactive",
        )

    return _open


class ChatHost:
    """Live entity sessions for one server process, driven by jobs.

    ``entities`` maps a NAME to an entity directory; it is fixed at construction and is the whole
    of what a client can address. ``session_factory`` is a test seam; production passes ``None`` and
    gets :meth:`EntitySession.open` with the operator's model settings and ``mode="interactive"``.
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
        # One worker per possible session: a session runs one job at a time, so no job ever waits
        # behind another session's turn.
        self._pool = ThreadPoolExecutor(max_workers=max_sessions, thread_name_prefix="levain-chat")
        self._shut = False

    # -- reads ---------------------------------------------------------------

    def listing(self) -> dict[str, Any]:
        with self._lock:
            return {
                "entities": sorted(self._entities),
                "model": self._model,
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
        self._pool.submit(self._run_open, rec, job)
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
        itself); a closed, failed or broken session closes as a no-op."""
        with self._lock:
            rec = self._get(session_id)
            if rec.state in ("opening", "busy"):
                raise ChatError("busy", "the session is running a job; close it when it finishes", 409)
            session, rec.session = rec.session, None
            if rec.state not in ("failed",):
                rec.state = "closed"
            view = self._session_view(rec)
        if session is not None:
            session.close()
        return view

    def shutdown(self) -> None:
        """Close every session and stop accepting work. Running jobs are not interrupted; their
        sessions close when they finish."""
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
        # Not cancel_futures: a cancelled job would leave its session open with nobody to close it.
        self._pool.shutdown(wait=False)

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
            rec.state = "busy"
            job = self._new_job(rec, kind)
        self._pool.submit(self._run_job, rec, job, call)
        return {"session_id": rec.session_id, "job_id": job.job_id, "state": "busy"}

    def _run_open(self, rec: _Session, job: _Job) -> None:
        error: str | None = None
        session: Any = None
        try:
            session = self._factory(self._entities[rec.entity], on_event=self._route_events(rec))
        except Exception as exc:  # noqa: BLE001 — a failed start is a RESULT; keep its TEXT only
            error = str(exc) or type(exc).__name__
        # `exc` is unbound here (Python deletes it at the end of the except clause), so nothing in
        # this frame still references the traceback of the failed start.
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
                job.activity.append(str(line))
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
        except Exception as exc:  # noqa: BLE001 — EntitySession's turn methods do not raise; this is a backstop
            error = f"{type(exc).__name__}: {exc}"
        to_close: Any = None
        with self._lock:
            if payload is None:
                rec.state, rec.error = "broken", error
                job.status, job.error = "failed", error
            else:
                job.status, job.result = "done", payload
                if payload["gated"] and payload["error"] is None:
                    rec.state = "gated"
                elif payload["error"] is not None:
                    # A turn that raised, timed out or could not read its own gate leaves the
                    # conversation in a state a later turn would resume FROM (EXIT_TURN_FAILED's
                    # contract), and a refusal that did not take is still holding actions. Either
                    # way the session takes no further turn. Its shell is released now rather than
                    # held until someone closes it.
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

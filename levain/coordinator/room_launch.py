"""levain.coordinator.room_launch — the room's launcher (RM-E, DESIGN_ROOM §2.3, §2.4, §7).

It turns the room setting into a running, attached, live Claude child and keeps it that way: build the argv and the
two config files, open the room launch, spawn, run the open check, record the activation, attach the session to the
:class:`~levain.coordinator.chat.ChatService`, and end, resume or recycle it later. It decides nothing about what a
message may do (that is the service's) and writes no drop (that is the room's door).

**The open.** :meth:`RoomLauncher.ensure` opens a launch on the open room thread (``lease.open_room_launch``), writes
the room's MCP config (the door bound to THIS launch) and its ``--settings`` deny rules into a 0700 directory of its
own, spawns ``claude`` through :func:`~levain.coordinator.claude_session.room_argv` and then runs the open check: one
fixed turn asking the child to Read a decoy file the deny rules cover. That one turn is both the child's first
``init`` (the child reports none before a turn starts, UNMEASURED beyond RM0's runs) and the canary
(:func:`~levain.coordinator.claude_session.canary_denied`) the chat service requires of a Claude ``live`` row. The
child is live only if :func:`~levain.coordinator.claude_session.open_probe` accepts that ``init`` AND the canary was
denied; otherwise the launch ends ``failed`` with a ``not_live`` activation row and nothing was ever attached. The
canary proves the ``--settings`` file took effect (the decoy is outside the room's cwd and matches the dotenv rule, so
either the outside-cwd block or the deny rule answers it); it does not tell which, and it does not exercise each deny
path. The check's own events (the canary Read, its denial, the child's reply) are in the room's stream like any turn.

**What the room's TripwireContext accepts**, exactly (:func:`room_tripwire_context`): for each of the room's four
file/search tools and its one MCP tool, an ``accept`` decision of reason ``rule`` whose source is ``user_permanent``
(his user allow list) or ``config`` (the argv's allow list), and no PreToolUse count owed for them. Both shapes were
recorded (RM0, RM0B). Why: the room's containment is the tool set, the deny rules and the probe, and its allow list IS
its argv, so an allow rule answering is the expected path; a worker's rule against allow rules exists because a worker
has none. Everything else latches as it does for a worker: a deny passes, any other decision shape for these tools, any
other source, and every other tool (``expected_pre_hooks`` refuses them, so they read as a ``deficit``).

**The cached ``permission_mode``** (``ChatService.session_meta``) is read at ``attach``. The room has no path that
refreshes it in place, because there is nothing to refresh: a mode that changes inside a live session is a latched
``mode_changed`` (the room's own rule, ``chat._room_mode_trip``), and a latched or dead room is ended and reopened by
:meth:`RoomLauncher.ensure`, which is a new child, a new session and a new attach with a new snapshot. A reopen never
reuses a session.

**Closing without the session's lock.** ``ClaudeSession.close`` takes the session's I/O lock, which a ``send`` blocked
in a full stdin pipe holds, and ``PipeTransport.close`` closes stdin before it signals the child, which also waits
behind that write. :class:`RoomSession` therefore stops the CHILD first (terminate, then kill, bounded, touching no
session lock), which ends the blocked write with a broken pipe, and only then runs the ordinary close.
:meth:`RoomLauncher.end` does the same before it asks the service to detach, because ``detach`` waits for the launch's
mutex and a hung send holds it. ``claude_session.py`` is untouched; its ``close`` taking the lock stays an RM-G
follow-up.

**Resume and lapse.** A new launch resumes the room thread's newest recorded conversation (``--resume <id>``; the child
reports the id it was resumed from, which :func:`~levain.coordinator.lease.activate_room` accepts for the room's own
ended launch). :meth:`RoomLauncher.recycle_idle` ends a quiet child (no turn open, no unsettled line, nothing recorded
for :data:`IDLE_SECONDS`); the next ``ensure`` resumes. ``ensure`` also replaces a launch whose child exited, one this
process holds no session for (a restarted ``levain serve``), and one the service latched ``not_live``. The service's
own ``recover()`` settles lines in flight; run it before the first ``ensure`` after a restart.

Stdlib plus levain. The spawn is a seam (``spawn``) so a test can supply a child of its own."""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import shutil
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, NamedTuple, Optional

from levain.cockpit.consent import parse_stamp
from levain.coordinator import lease
from levain.coordinator.chat import (ROOM_PERMISSION_MODE, ChatService, ChatStore, Verdict, _launch_state, _open_room,
                                     liveness)
from levain.coordinator.chat_tripwires import TripwireContext
from levain.coordinator.claude_session import (ROOM_DENIED_TOOLS, ROOM_MCP_SERVER, ROOM_MCP_TOOL, ROOM_TOOLS,
                                               ClaudeSession, PipeTransport, canary_denied, default_deny_paths,
                                               open_probe, room_argv, room_settings_json)
from levain.coordinator.driver import LivenessEvidence, SendCommand
from levain.coordinator.room_mcp import LAUNCH_ENV
from levain.coordinator.store import CoordError

__all__ = [
    "IDLE_SECONDS", "KILL_WAIT", "OPEN_DEADLINE", "RoomCanary", "RoomHandle", "RoomLaunchError", "RoomLauncher",
    "RoomSession", "RoomSetting", "make_probe", "make_tripwires", "room_probe", "room_tripwire_context",
]

# [seat] the open check must finish well inside lease.LAUNCH_TTL (the launch is pending until it does)
OPEN_DEADLINE = 120.0
# [seat, UNMEASURED] flowConnect's IDLE_SECONDS (DESIGN_ROOM §2.3): a quiet child is recycled after this long
IDLE_SECONDS = 1200.0
# [seat] how long each of terminate and kill is given before the next step
KILL_WAIT = 3.0
_POLL = 0.05
_STDERR_TAIL = 400
ROOM_ENV = "LEVAIN_ROOM"
CANARY_PROMPT = "Open check. Use your Read tool once on this exact path, then reply with the single word done: {path}"
_CANARY_NAME = ".env.levain-room-canary"
_SOURCES = ("user_permanent", "config")


class RoomLaunchError(CoordError):
    """The room could not be opened or kept; the message says why. Nothing was attached."""


@dataclass(frozen=True)
class RoomSetting:
    """What the room runs as. ``cwd`` is the directory its file tools see first (absolute, an existing directory).
    ``harness`` is ``claude``: the Codex room profile is RM-D3's."""
    cwd: str
    model: Optional[str] = None
    harness: str = "claude"
    claude_bin: str = "claude"


@dataclass(frozen=True)
class RoomCanary:
    """The accepted open-check verdict the chat service records the id of (it requires a dataclass with ``ok``)."""
    ok: bool
    decoy: str
    reason: str


class RoomHandle(NamedTuple):
    launch_id: str
    room_thread: str
    harness_session_id: str
    resumed: bool


def _stop_child(proc: Any) -> None:
    """Terminate, then kill, the child, each bounded by :data:`KILL_WAIT`. Takes no lock, so it works while another
    thread is blocked writing to the child. Never raises."""
    for step in (proc.terminate, lambda: proc.wait(KILL_WAIT), proc.kill, lambda: proc.wait(KILL_WAIT)):
        try:
            if proc.poll() is not None:
                return
            step()
        except Exception:  # noqa: BLE001 - a child that will not die is the caller's to find; this never raises
            pass


class RoomSession(ClaudeSession):
    """A :class:`~levain.coordinator.claude_session.ClaudeSession` whose :meth:`close` stops the child before it takes
    the session's lock (see the module docstring)."""

    def __init__(self, transport: Any, *, child: Any, **kw: Any) -> None:
        super().__init__(transport, **kw)
        self._child = child

    @property
    def output_ended(self) -> bool:
        """The transport has read the end of the child's output."""
        return bool(getattr(self._t, "eof", False))

    def close(self) -> None:
        _stop_child(self._child)
        super().close()


# ---- the service's two seams for a room launch ------------------------------------------------------------------------
def room_probe(launch_id: str, evidence: LivenessEvidence) -> Verdict:
    """The room's verdict on one ``init``: live only for exactly its tools, its one connected door, under ``dontAsk``."""
    got = open_probe(evidence.raw if isinstance(evidence, LivenessEvidence) else None)
    return Verdict(got.live, got.reason)


def room_tripwire_context(cwd: str) -> TripwireContext:
    """The room's TripwireContext (what it accepts: the module docstring)."""
    tools = (*ROOM_TOOLS, ROOM_MCP_TOOL)

    def expected(tool: str) -> int:
        raise ValueError(f"{str(tool)[:60]} is not one of the room's tools")

    return TripwireContext(cwd, expected, lambda _call, _input: False,
                           frozenset((t, s) for t in tools for s in _SOURCES))


def make_probe(store: ChatStore, other: Optional[Callable[[str, LivenessEvidence], Verdict]] = None
               ) -> Callable[[str, LivenessEvidence], Verdict]:
    """The one ``probe`` a ChatService takes: a room launch gets :func:`room_probe`, any other launch gets ``other``
    (no verdict at all, which latches, when there is none)."""
    def probe(launch_id: str, evidence: LivenessEvidence) -> Verdict:
        if store._read(lambda conn: _launch_state(conn, launch_id)[0]) == "room":
            return room_probe(launch_id, evidence)
        if other is None:
            return Verdict(False, "no probe is configured for this launch")
        return other(launch_id, evidence)
    return probe


def make_tripwires(store: ChatStore, other: Optional[Callable[[str], Optional[TripwireContext]]] = None
                   ) -> Callable[[str], Optional[TripwireContext]]:
    """The one ``tripwires`` a ChatService takes: a room launch gets :func:`room_tripwire_context` over its recorded
    cwd, any other launch gets ``other`` (None when there is none)."""
    def tripwires(launch_id: str) -> Optional[TripwireContext]:
        row = store._read(lambda conn: conn.execute("SELECT role, cwd FROM launches WHERE launch_id = ?",
                                                    (launch_id,)).fetchone())
        if row is not None and row[0] == "room":
            return room_tripwire_context(row[1] or "")
        return other(launch_id) if other is not None else None
    return tripwires


# ---- small pure helpers -----------------------------------------------------------------------------------------------
def _one_line(text: str, limit: int = 300) -> str:
    return "".join(c if 32 <= ord(c) < 127 else " " for c in " ".join(str(text).split()))[:limit].strip() or "no detail"


def mcp_config_json(launch_id: str, python: str) -> str:
    """The room's ``--mcp-config``: one stdio server, the door bound to ``launch_id`` in its own environment (a call
    carries no session id; RM0 finding 8)."""
    return json.dumps({"mcpServers": {ROOM_MCP_SERVER: {
        "command": python, "args": ["-P", "-m", "levain.coordinator.room_mcp", "--launch", launch_id],
        "env": {LAUNCH_ENV: launch_id, ROOM_ENV: "1"}}}}, sort_keys=True)


def containment_digest(settings_json: str) -> str:
    """64 hex: the containment policy a spawn ran under (tool flags, mode, sources, deny rules, door name)."""
    policy = {"tools": list(ROOM_TOOLS), "denied": list(ROOM_DENIED_TOOLS), "mode": ROOM_PERMISSION_MODE,
              "setting_sources": "user", "settings": json.loads(settings_json), "door": ROOM_MCP_TOOL}
    return hashlib.sha256(json.dumps(policy, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _spawn(argv: list, *, cwd: str, env: dict, stderr: Any) -> Any:
    return subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=stderr,
                            text=True, encoding="utf-8", errors="replace")


class _Live:
    """What the launcher holds for one running room launch."""

    def __init__(self, launch_id: str, session: RoomSession, proc: Any, workdir: Path) -> None:
        self.launch_id, self.session, self.proc, self.workdir = launch_id, session, proc, workdir


class RoomLauncher:
    """See the module docstring. ``store`` and ``service`` are the ChatStore and the ChatService built with
    :func:`make_probe` and :func:`make_tripwires`. ``run_dir`` is where each launch's private directory is made.
    ``spawn(argv, cwd=, env=, stderr=)`` returns a Popen-like child; ``now()`` is the wall clock for the idle test."""

    def __init__(self, store: ChatStore, service: ChatService, setting: RoomSetting, *, run_dir: str,
                 home: Optional[str] = None, python: Optional[str] = None, spawn: Callable[..., Any] = _spawn,
                 idle_seconds: float = IDLE_SECONDS, open_deadline: float = OPEN_DEADLINE,
                 now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
                 environ: Optional[dict] = None) -> None:
        if setting.harness != "claude":
            raise RoomLaunchError(f"the room opens on claude here; a {str(setting.harness)[:20]} room profile is not "
                                  "built (RM-D3)")
        for name, p in (("the room's cwd", setting.cwd), ("run_dir", run_dir)):
            if not isinstance(p, str) or not os.path.isabs(p):
                raise RoomLaunchError(f"{name} must be an absolute path")
        if not os.path.isdir(setting.cwd):
            raise RoomLaunchError(f"the room's cwd {setting.cwd[:120]!r} is not a directory")
        self._store, self._svc, self._setting = store, service, setting
        self._run_dir = Path(run_dir)
        self._home = home if home is not None else os.path.expanduser("~")
        self._python = python or sys.executable
        self._spawn = spawn
        self._idle = idle_seconds
        self._deadline = open_deadline
        self._now = now
        self._environ = environ
        self._lock = threading.RLock()                 # one open, end or recycle at a time
        self._live: dict = {}
        self._conn = 0
        # refused early, before any launch row: a bad cwd/deny pair is the operator's to fix
        try:
            self._settings_json = room_settings_json(setting.cwd, default_deny_paths(self._home))
        except ValueError as exc:
            raise RoomLaunchError(f"the room's settings could not be built: {exc}") from exc

    # --- reads ------------------------------------------------------------------------------------------------------
    def _newest(self, room_thread: str) -> Optional[tuple]:
        """``(launch_id, state, harness_session_id)`` of the room thread's newest room launch, or None."""
        def read(conn: Any) -> Optional[tuple]:
            row = conn.execute(
                "SELECT l.launch_id, (SELECT e.kind FROM launch_events e WHERE e.launch_id = l.launch_id ORDER BY "
                "e.seq DESC LIMIT 1) FROM launches l, json_each(l.threads) j WHERE l.role = 'room' AND j.value = ? "
                "ORDER BY l.gseq DESC LIMIT 1", (room_thread,)).fetchone()
            return None if row is None else row
        return self._store._read(read)

    def _resume_id(self, room_thread: str) -> Optional[str]:
        """The newest conversation id any room launch of this thread recorded."""
        row = self._store._read(lambda conn: conn.execute(
            "SELECT e.harness_session_id FROM launch_events e JOIN launches l ON l.launch_id = e.launch_id, "
            "json_each(l.threads) j WHERE l.role = 'room' AND j.value = ? AND e.kind = 'active' AND "
            "e.harness_session_id IS NOT NULL ORDER BY e.gseq DESC LIMIT 1", (room_thread,)).fetchone())
        return None if row is None else row[0]

    def _last_activity(self, launch_id: str) -> datetime:
        row = self._store._read(lambda conn: conn.execute(
            "SELECT MAX(t) FROM (SELECT MAX(at) t FROM launch_events WHERE launch_id = ? AND kind = 'active' "
            "UNION ALL SELECT MAX(at) FROM chat_messages WHERE launch_id = ? "
            "UNION ALL SELECT MAX(at) FROM chat_events WHERE launch_id = ? AND backfill = 0)",
            (launch_id, launch_id, launch_id)).fetchone())
        return parse_stamp(row[0]) if row is not None and row[0] else self._now()

    def _unsettled(self, launch_id: str) -> int:
        """Lines of the launch whose latest outbox row is neither a final one nor accepted."""
        return self._store._read(lambda conn: conn.execute(
            "SELECT COUNT(*) FROM chat_messages m WHERE m.launch_id = ? AND (SELECT o.state FROM chat_outbox_events o "
            "WHERE o.message_id = m.message_id ORDER BY o.seq DESC LIMIT 1) IN ('pending', 'attempted')",
            (launch_id,)).fetchone()[0])

    def child_alive(self, launch_id: str) -> bool:
        with self._lock:
            held = self._live.get(launch_id)
        return held is not None and held.proc.poll() is None

    # --- the open ---------------------------------------------------------------------------------------------------
    def ensure(self, *, fresh: bool = False) -> RoomHandle:
        """The open room's running launch, opened (or reopened, resuming the room thread's newest conversation unless
        ``fresh``) when it has none. A launch that is active but whose child exited, that this process holds no session
        for, or that the service latched is ended first. Raises :class:`RoomLaunchError` when there is no open room or
        the open check fails; the failed launch is ended and nothing stays attached."""
        with self._lock:
            room_thread = self._store._read(_open_room)
            if room_thread is None:
                raise RoomLaunchError("there is no open room; nothing was opened")
            newest = self._newest(room_thread)
            if newest is not None and newest[1] in ("pending", "active"):
                held = self._live.get(newest[0])
                why = None
                if held is None:
                    why = "room launch not held by this process"
                elif held.proc.poll() is not None:
                    why = "room child exited"
                elif self._store._read(lambda conn: liveness(conn, newest[0])).latched:
                    why = "room latched not live"
                if why is None and newest[1] == "active":
                    sid = self._store._read(lambda conn: self._resume_for(conn, newest[0]))
                    return RoomHandle(newest[0], room_thread, sid or "", False)
                self.end(newest[0], why or "room launch replaced")
            resume = None if fresh else self._resume_id(room_thread)
            return self._open(room_thread, resume)

    @staticmethod
    def _resume_for(conn: Any, launch_id: str) -> Optional[str]:
        row = conn.execute("SELECT harness_session_id FROM launch_events WHERE launch_id = ? AND kind = 'active' "
                           "ORDER BY seq DESC LIMIT 1", (launch_id,)).fetchone()
        return None if row is None else row[0]

    def _next_conn(self) -> int:
        self._conn += 1
        return self._conn

    def _open(self, room_thread: str, resume: Optional[str]) -> RoomHandle:
        with self._store._write() as (conn, now):
            ln = lease.open_room_launch(conn, now, harness="claude", model=self._setting.model,
                                        cwd=self._setting.cwd, room_thread=room_thread, pid=os.getpid())
        launch_id = ln.launch_id
        nonce = secrets.token_hex(16)
        workdir = self._run_dir / launch_id
        proc = session = None
        try:
            argv, decoy, stderr_path = self._write_files(workdir, launch_id, resume)
            env = dict(self._environ if self._environ is not None else os.environ)
            env[ROOM_ENV] = "1"
            with open(stderr_path, "ab") as errf:
                proc = self._spawn(argv, cwd=self._setting.cwd, env=env, stderr=errf)
            session = RoomSession(PipeTransport(proc), child=proc, conn=self._next_conn(),
                                  session_id=resume or argv[argv.index("--session-id") + 1])
            self._live[launch_id] = _Live(launch_id, session, proc, workdir)
            self._svc.activate(launch_id, "probing", "room spawned", spawn_nonce=nonce)
            init = self._open_check(session, proc, decoy, stderr_path)
            sid = init.raw.get("session_id")
            digest = "sha256:" + hashlib.sha256(json.dumps(argv, sort_keys=True).encode()).hexdigest()
            with self._store._write() as (conn, now):
                got, why = lease.activate_room(conn, now, launch_id, harness_session_id=sid, argv_sha256=digest)
            if why is not None:
                raise RoomLaunchError(f"the room launch could not be activated: {why}")
            self._svc.attach(launch_id, session)
            self._svc.activate(launch_id, "live", "room open check passed", evidence=init, spawn_nonce=nonce,
                               canary=RoomCanary(True, decoy, "the canary Read was denied"),
                               containment_digest=containment_digest(self._settings_json))
            # the service sends nothing to a launch with no recorded vendor position: record the open check's events
            # (the first read ends at its turn boundary) and make sure they did not latch the room they opened
            self._svc.read_next(launch_id)
            fold = self._store._read(lambda conn: liveness(conn, launch_id))
            if not fold.enabled:
                raise RoomLaunchError(f"the room was turned off by its own open check: {fold.why_not}")
            return RoomHandle(launch_id, room_thread, sid, resume is not None)
        except BaseException as exc:
            if proc is not None and launch_id not in self._live:
                _stop_child(proc)                        # spawned, but not yet held: nothing else will stop it
            self._fail(launch_id, nonce, exc)
            if isinstance(exc, RoomLaunchError):
                raise
            if isinstance(exc, Exception):
                raise RoomLaunchError(f"the room could not be opened: {_one_line(f'{type(exc).__name__}: {exc}')}"
                                      ) from exc
            raise

    def _write_files(self, workdir: Path, launch_id: str, resume: Optional[str]) -> tuple:
        self._run_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        workdir.mkdir(mode=0o700)
        os.chmod(workdir, 0o700)

        def put(name: str, text: str) -> Path:
            p = workdir / name
            fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(text)
            return p

        mcp = put("mcp.json", mcp_config_json(launch_id, self._python))
        settings = put("settings.json", self._settings_json)
        stderr = put("child.stderr", "")
        decoy_dir = workdir / "decoy"
        decoy_dir.mkdir(mode=0o700)
        decoy = put("decoy/" + _CANARY_NAME, "DECOY\n")
        argv = room_argv(mcp_config=mcp, settings=settings, model=self._setting.model, claude_bin=self._setting.claude_bin,
                         **({"resume": resume} if resume else {"session_id": str(uuid.uuid4())}))
        return argv, str(decoy), stderr

    def _open_check(self, session: RoomSession, proc: Any, decoy: str, stderr_path: Path) -> LivenessEvidence:
        """Send the canary turn, wait for it to end, and return the ``init`` it produced once the probe and the canary
        accept it. Raises :class:`RoomLaunchError` with the reason otherwise."""
        try:
            sent = session.send(SendCommand("room-open-check", CANARY_PROMPT.format(path=decoy), (), "queue", None))
        except Exception as exc:  # noqa: BLE001
            raise RoomLaunchError(f"the open check could not be sent: {_one_line(f'{type(exc).__name__}: {exc}')}"
                                  ) from exc
        if sent.outcome != "accepted":
            raise RoomLaunchError(f"the open check was not accepted by the child: {_one_line(sent.detail)}")
        deadline = time.monotonic() + self._deadline
        while True:
            init = session.liveness_evidence()
            ended = any(f.get("type") == "result" for f in session.recent_frames()) and session.pending_lines() == 0
            if ended:
                break
            if proc.poll() is not None and session.output_ended:
                raise RoomLaunchError(f"the room child exited (code {proc.returncode}) before its open check ended"
                                      f"{self._tail(stderr_path)}")
            if time.monotonic() > deadline:
                raise RoomLaunchError(f"the room's open check did not end within {self._deadline:g} s"
                                      f"{self._tail(stderr_path)}")
            time.sleep(_POLL)
        probe = open_probe(init.raw)
        if not probe.live:
            raise RoomLaunchError(f"the room could not open with its read-only tools: {probe.reason}")
        verdict = canary_denied(session.recent_frames(), decoy)
        if not verdict.denied:
            raise RoomLaunchError(f"the room's open check found its deny rules not holding: {verdict.reason}")
        sid = init.raw.get("session_id")
        if not isinstance(sid, str) or not sid:
            raise RoomLaunchError("the room child reported no session id")
        return init

    @staticmethod
    def _tail(path: Path) -> str:
        try:
            raw = path.read_bytes()[-_STDERR_TAIL:].decode("utf-8", "replace")
        except OSError:
            return ""
        return f"; stderr: {_one_line(raw)}" if raw.strip() else ""

    def _fail(self, launch_id: str, nonce: str, exc: BaseException) -> None:
        """An open that did not complete: stop the child, detach, record ``not_live`` and end the launch. Best effort at
        each step; the first error is the caller's, never one raised here."""
        why = _one_line(f"{exc}" if isinstance(exc, RoomLaunchError) else f"{type(exc).__name__}: {exc}")
        self._end(launch_id, f"room open failed: {why}", note=why, nonce=nonce)

    # --- the end ----------------------------------------------------------------------------------------------------
    def end(self, launch_id: str, cause: str) -> None:
        """Stop the room's child, detach its session and end the launch. Safe against a ``send`` hung in the pipe: the
        child is stopped first, with no lock held."""
        with self._lock:
            self._end(launch_id, cause)

    def _end(self, launch_id: str, cause: str, *, note: Optional[str] = None, nonce: Optional[str] = None) -> None:
        held = self._live.pop(launch_id, None)
        if held is not None:
            _stop_child(held.proc)                       # first, with no lock: it frees a send blocked in the pipe
        try:
            self._svc.detach(launch_id)
        except Exception:  # noqa: BLE001 - the launch must still end
            pass
        if note is not None and nonce is not None:
            try:
                self._svc.activate(launch_id, "not_live", note[:200], spawn_nonce=nonce)
            except Exception:  # noqa: BLE001
                pass
        try:
            with self._store._write() as (conn, now):
                state = _launch_state(conn, launch_id)[1]
                if state in ("pending", "active"):
                    lease.end_launch(conn, now, launch_id, "ended" if state == "active" else "failed",
                                     _one_line(cause, 200))
        except Exception:  # noqa: BLE001
            pass
        if held is not None:
            shutil.rmtree(held.workdir, ignore_errors=True)
        else:
            shutil.rmtree(self._run_dir / launch_id, ignore_errors=True)

    # --- lapse ------------------------------------------------------------------------------------------------------
    def recycle_idle(self) -> list:
        """End the room's child if it has been quiet for the idle time: no turn open, no unsettled line, nothing recorded
        for the launch since. The next :meth:`ensure` resumes. The ids ended."""
        out = []
        with self._lock:
            for launch_id, held in list(self._live.items()):
                if held.proc.poll() is not None or held.session.pending_lines() or self._unsettled(launch_id):
                    continue
                if self._now() - self._last_activity(launch_id) < timedelta(seconds=self._idle):
                    continue
                self._end(launch_id, "room idle")
                out.append(launch_id)
        return out

    def close(self) -> None:
        """End every launch this launcher holds (a service closing)."""
        with self._lock:
            for launch_id in list(self._live):
                self._end(launch_id, "room closed")


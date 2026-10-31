"""levain.coordinator.codex_launch — start and watch a Codex launch's session (coordinator C3c; C0 §7 for Codex).

A Codex session Levain starts is a THREAD on the user's own Codex app-server daemon, reached through the daemon's
control socket ``<CODEX_HOME>/app-server-control/app-server-control.sock``. Measured on codex-cli 0.162.1 by C3c's
runs (1020+50, 1021+18): the socket answers a plain HTTP/1.1 WebSocket Upgrade, every message is a JSON-RPC object
WITHOUT the ``"jsonrpc"`` field, ``initialize`` names the daemon's ``codexHome``, and a thread started there keeps
running its turn after the client that started it disconnects. ``codex queue --thread <name>`` delivers a message
into it from anywhere, and ``codex resume --remote unix://<socket> <thread id>`` opens it in the TUI. OpenAI marks the
app-server and its WebSocket transport experimental and not supported for production workloads; the fallback this
module does not take is ``codex exec --json``, attachable only after it exits.

:func:`launch` (called by :func:`levain.coordinator.launch.launch_session` for harness ``codex``) does, in order, the
shape C3b gives Claude Code:

  1. the checks that need no spawn: the daemon answering, with the CODEX_HOME this process checks; Codex's consent
     hook live (:func:`global_hook_live`: doctor's own Codex check of the install that wrote that CODEX_HOME's one
     hooks file); the launch's directory a coordinator's workspace with its rule (:func:`setup` makes one); the
     briefs directory. A refusal here writes nothing: the launch stays pending and expires unspawned (no death);
  2. the one-spawner compare-and-set (:func:`levain.coordinator.lease.claim_spawn`);
  3. the brief file (the module :mod:`levain.coordinator.launch` writes it, the same file a Claude Code launch gets);
  4. the spawn: ``thread/start {cwd}`` (plus ``model`` when the setting names one; no approval or sandbox override,
     so the user's own Codex config decides), then the thread recorded under the launch (the spawn claim and the
     lease token re-checked in that write, :func:`levain.coordinator.lease.record_thread`, with the daemon's own
     codexHome and its socket) BEFORE ``thread/name/set`` and ``turn/start`` with the prompt naming the brief, and
     again with the turn's id once it is accepted; the client then disconnects. A refused record starts no turn; a
     ``turn/start`` answered without a turn id fails the launch, its thread's turn in progress interrupted;
  5. the read back: a fresh connection's ``thread/read``, which must list the thread under our name in the launch's
     realpath directory, ``active`` (a turn running). The thread id is the harness session id the launch records;
  6. activation, the token re-checked (:func:`levain.coordinator.lease.activate`). Refused: the session is stopped.

"Stop" is ``turn/interrupt`` of the thread's turn in progress; Levain never archives or deletes a thread (its rollout
is the user's record), and never starts or stops the daemon (it is the user's, and its updater outlives a stop).

:func:`watch_once` is the Codex half of ``levain coord watch``: every read and interrupt goes to the daemon the
launch recorded and checks its codexHome; an active Codex launch whose thread the daemon no longer knows in two reads
ends; the coordinator holding the lease is beaten only while its thread is active (a turn in progress); a launch that
is over and recorded a thread (a starter killed before activation included) has its turn in progress interrupted,
and leaves the watcher's list once its thread is seen not running.

[seat] choices (C3c, 1021+18 lane c3c): the daemon socket over ``codex exec --json``; Levain never starts or stops the
daemon; the hook check held against the daemon's own ``codexHome``; Codex's own trust of the gate required (it skips an
untrusted hook silently); no approval or sandbox override on ``thread/start``; stop is ``turn/interrupt``, never an
archive or delete; ``levain coord agent codex`` never runs ``levain init`` (it would rewire the machine's one Codex
hooks file); the timeouts below; "active" (a turn in progress) is what earns a service beat, as a Claude Code row that
is not ``done`` does. Stdlib only.

The coordinator's workspace (np-6843ca07 (a)): a directory Levain owns (by default :func:`default_directory`), never
the operator's own Codex install. :func:`setup` writes the coordinator role hint in it (``levain coord agent`` then
puts it in the coordinator record the door reads, :mod:`levain.coordinator.door`), and writes Codex's own rule file into its ``.codex/rules/``, which lets exactly ``<this interpreter> -P -m levain`` coord / drops / thread run without asking
(Codex runs an allowed command outside its sandbox: its own ``coord claim`` writes the consent directory). Measured on
codex-cli 0.162.1 (1021+18 lane c3cpre): with the rule in a trusted directory the first ``coord claim`` ran unasked;
without it the same claim failed in the sandbox and then waited on approval; ``touch`` outside the workspace and
``levain drop`` by the same spelling still asked.

Known edges: Codex loads the rule only while the operator trusts the directory (Levain never writes that trust), and
it names the interpreter :func:`setup` ran under, so a coordinator launched from another levain asks. Outside the
door, a coordinator runs under the user's own approval and sandbox settings: a command that needs approval waits until
someone opens the thread (measured: it never fails the turn), and such a thread reads ``active`` with no flag to a
client that has not resumed it, so the watcher cannot tell it from one working. A starter killed between
``thread/start`` and its record leaves a thread no row names, with no turn started on it. The workspace's
``.levain/`` is inside what Codex's workspace-write sandbox lets the session write, unlike ``.codex/``. An over
launch whose thread keeps answering with an error other than "not loaded" stays on the watcher's list.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import shlex
import socket
import sqlite3
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from levain.contracts.canon import canonical_bytes
from levain.coordinator import lease as lease_mod
from levain.coordinator.codex_wire import _CALL_TIMEOUT, _CONNECT_TIMEOUT, CodexError, WsFraming
from levain.coordinator.lease import LeaseRefused
from levain.coordinator.store import CoordError, CoordStore

__all__ = ["CONSENT_ROW", "DAEMON_REMEDY", "GATE_ROW", "CodexError", "codex_home", "control_socket",
           "default_directory", "global_hook_live", "hook_live", "launch", "read_thread", "setup", "stop",
           "watch_once"]

CONSENT_ROW = "Codex consent hook"
GATE_ROW = "Codex consent gate (PreToolUse)"
_LIVE = "wired to this install"
_GATE_TRUSTED = "wired to this install and trusted by Codex"
_HOOKS_ROW = "Codex hooks"
_HOOKS_OFF = "turned off by"
DAEMON_REMEDY = "codex app-server daemon start"
UNLISTED_RECHECK = 2.0


def codex_home() -> Path:
    """The CODEX_HOME doctor checks: ``$CODEX_HOME``, else ``~/.codex``."""
    return Path(os.environ.get("CODEX_HOME") or os.path.expanduser("~/.codex"))


def control_socket(home: Path | None = None) -> Path:
    return (home or codex_home()) / "app-server-control" / "app-server-control.sock"


# --- the client: WebSocket (RFC 6455) over the unix socket, JSON-RPC without the "jsonrpc" field ----------------
class _Client(WsFraming):
    """The framing lives in :mod:`levain.coordinator.codex_wire` (CH-D3 S1 moved it there for the chat wire); this
    single-shot client keeps its calls."""

    def __init__(self, path: Path, timeout: float = _CONNECT_TIMEOUT) -> None:
        super().__init__(path, timeout)
        self._n = 0

    def _message(self) -> dict[str, Any]:
        while True:
            msg = self.recv_message()
            try:
                got = json.loads(msg.decode("utf-8"))
            except (UnicodeDecodeError, ValueError) as exc:
                raise CodexError(f"the daemon sent a message that is not JSON ({exc})") from None
            if isinstance(got, dict):
                return got

    def notify(self, method: str, params: dict[str, Any]) -> None:
        self._send(1, json.dumps({"method": method, "params": params}).encode())

    def call(self, method: str, params: dict[str, Any], timeout: float = _CALL_TIMEOUT) -> Any:
        """Send one request and return its ``result``; notifications and the daemon's own requests in between are
        read and dropped (Levain never answers an approval request). An error response raises :class:`CodexError`."""
        self._n += 1
        rid = self._n
        self._send(1, json.dumps({"id": rid, "method": method, "params": params}).encode())
        end = time.monotonic() + timeout
        while True:
            left = end - time.monotonic()
            if left <= 0:
                raise CodexError(f"the daemon did not answer {method} in {timeout:g}s")
            self._s.settimeout(left)
            try:
                m = self._message()
            except (socket.timeout, TimeoutError):
                raise CodexError(f"the daemon did not answer {method} in {timeout:g}s") from None
            if m.get("id") == rid and "method" not in m:
                if "error" in m:
                    err = m["error"]
                    text = err.get("message") if isinstance(err, dict) else err
                    raise CodexError(f"{method}: {str(text)[:300]}", err)
                return m.get("result")


@contextlib.contextmanager
def _session(path: Path) -> Iterator[tuple[_Client, dict[str, Any]]]:
    """A connected, initialized client and the ``initialize`` result. Any socket fault is a :class:`CodexError`."""
    try:
        c = _Client(path)
    except OSError as exc:
        raise CodexError(f"the Codex daemon is not answering on {path} ({type(exc).__name__}: {exc})") from None
    try:
        init = c.call("initialize", {"clientInfo": {"name": "levain", "title": "Levain coordinator", "version": "1"}})
        c.notify("initialized", {})
        yield c, init if isinstance(init, dict) else {}
    except OSError as exc:
        raise CodexError(f"the Codex daemon connection failed ({type(exc).__name__}: {exc})") from None
    finally:
        c.close()


def _same(a: object, b: Path) -> bool:
    try:
        return isinstance(a, str) and os.path.realpath(a) == os.path.realpath(b)
    except (OSError, ValueError):
        return False


# --- the hook check (L2) ----------------------------------------------------------------------------------------
def hook_live(directory: Path) -> tuple[bool, str]:
    """Doctor's own Codex check of ``directory`` (never a re-derivation): ``(True, detail)`` when its
    ``Codex consent hook`` row reads "wired to this install", its ``Codex consent gate (PreToolUse)`` row reads
    "wired to this install and trusted by Codex" (Codex skips an untrusted hook silently, so an unconfirmed trust
    refuses), and no ``Codex hooks`` row fails "turned off by"; else ``(False, why)``. Doctor reads the CODEX_HOME of
    this process; :func:`launch` holds it against the daemon's own."""
    from levain import doctor

    if not (Path(directory) / "AGENTS.md").is_file():
        return False, f"{directory} is not a Levain install for Codex (no AGENTS.md)"
    try:
        rows = doctor._check_codex(Path(directory))
    except Exception as exc:  # noqa: BLE001 - a check that cannot run is a refusal, never a pass
        return False, f"doctor could not check {directory} ({type(exc).__name__}: {str(exc)[:200]})"
    off = next((r for r in rows if r.name == _HOOKS_ROW and not r.ok and r.detail.startswith(_HOOKS_OFF)), None)
    if off is not None:
        return False, f"{_HOOKS_ROW}: {off.detail}"
    consent = next((r for r in rows if r.name == CONSENT_ROW), None)
    gate = next((r for r in rows if r.name == GATE_ROW), None)
    if consent is None or gate is None:
        failed = next((r for r in rows if not r.ok), None)
        why = f"{failed.name}: {failed.detail}" if failed is not None else "doctor reports no consent hook row"
        return False, f"{directory}: the consent hook is not checked ({why})"
    if not consent.detail.startswith(_LIVE):
        return False, f"{CONSENT_ROW}: {consent.detail}"
    if not gate.ok or not gate.detail.startswith(_GATE_TRUSTED):
        return False, f"{GATE_ROW}: {gate.detail}"
    return True, consent.detail


def default_directory() -> Path:
    """``<levain home>/coordinator-codex``, beside the Claude Code coordinator's own directory: the Codex
    coordinator's workspace when the setting names none."""
    from levain.coordinator.launch import default_coordinator_dir

    return default_coordinator_dir().parent / "coordinator-codex"


def global_hook_live(workspace: Path) -> tuple[bool, str]:
    """Whether Codex's consent hook is live for a session in ``workspace``. Codex has one hooks file per CODEX_HOME,
    so a Codex session anywhere runs the hooks of the install that wrote it: that install is found by Levain's own
    record of the write (:func:`levain.host_guard.codex_hooks_owner`) and checked by doctor (:func:`hook_live`).
    ``(True, detail)`` or ``(False, why)``; nothing is written."""
    from levain import host_guard

    owner = host_guard.codex_hooks_owner(workspace, codex_home())
    if owner is None:
        return False, (f"{codex_home() / 'hooks.json'} is not one a Levain install wrote, so no Levain consent hook "
                       "runs in a Codex session: `levain init --adapter codex` in your own install first")
    live, detail = hook_live(owner)
    return live, f"{owner}: {detail}"


def _workspace_ready(directory: Path) -> tuple[bool, str]:
    """The launch's directory holds the coordinator's rule (written again if it was removed, never over a file that
    says anything else), so ``.codex/`` exists before the thread starts and Codex keeps it read-only. That it is a
    coordinator's workspace at all is checked before, by the launch's one confinement check for both harnesses."""
    from levain import install as inst

    ok, detail = inst.write_codex_coordinator_rules(directory, sys.executable)
    return ok, (detail if ok else f"{detail}; a coordinator there would wait on approvals (remove the file)")


def setup(directory: Path | None = None) -> tuple[Path, bool, str]:
    """``levain coord agent codex``'s step: ``(resolved directory, ready, detail)``. Checks that Codex's consent hook
    is live (:func:`global_hook_live`; never installs it: ``levain init --adapter codex`` rewires the machine's one
    Codex hooks file, which stays the operator's step), then makes ``directory`` (default :func:`default_directory`)
    the coordinator's workspace, a directory Levain owns (missing, empty, or already a coordinator's):
    the coordinator role the door reads, and Codex's own rule pre-approving exactly this interpreter's
    ``-P -m levain`` coord / drops / thread (np-6843ca07 (a)). Codex loads that rule only while the operator trusts
    the directory, which stays the operator's step."""
    from levain import install as inst

    target = Path(directory if directory is not None else default_directory()).expanduser().resolve()
    live, detail = global_hook_live(target)
    if not live:
        return target, False, f"Codex's consent hook is not live: {detail}"
    if os.path.islink(target) or (os.path.lexists(target) and not target.is_dir()):
        return target, False, f"{target} is not a plain directory; no coordinator workspace was made there"
    if not inst._is_safe_install_target(target) and inst.persisted_role(target) != "coordinator":
        return target, False, (f"{target} holds files and is not a coordinator's workspace; pick an empty or "
                               "missing directory")
    try:
        target.mkdir(mode=0o700, parents=True, exist_ok=True)
        inst._write_role(target, "coordinator", sys.executable, lambda _line: None)
    except (OSError, inst.InitError) as exc:
        return target, False, f"{target} could not be made a coordinator workspace ({exc})"
    ok, rules = inst.write_codex_coordinator_rules(target, sys.executable)
    if not ok:
        return target, False, f"{rules}; a coordinator there would wait on approvals (remove the file)"
    made = f"{target} is the coordinator's workspace (role recorded); {rules}"
    return target, True, (f"Codex's consent hook is live ({detail}); {made}. Codex reads the rule only in a "
                          f"directory you trust: run `codex` in {target} once and trust it")


# --- reading and stopping a thread -------------------------------------------------------------------------------
def _not_found(exc: CodexError) -> bool:
    """The daemon's answer for a thread it does not have (measured, codex-cli 0.162.1: ``-32600`` "thread not loaded:
    <id>" for an id that never existed, while a real thread the daemon unloaded when idle still reads, status
    ``notLoaded``). Any other error is no evidence the thread is gone."""
    msg = exc.error.get("message") if isinstance(exc.error, dict) else None
    return isinstance(msg, str) and msg.startswith("thread not loaded")


def read_thread(thread_id: str, *, sock: Path | None = None) -> dict[str, Any] | None:
    """``thread/read`` of ``thread_id``: the thread, or None when the daemon answers that it has no such thread.
    A daemon that cannot be read raises :class:`CodexError` (an unread list is no evidence)."""
    with _session(sock or control_socket()) as (c, _init):
        return _read(c, thread_id)


def _read(c: _Client, thread_id: str) -> dict[str, Any] | None:
    try:
        got = c.call("thread/read", {"threadId": thread_id})
    except CodexError as exc:
        if exc.error is not None and _not_found(exc):
            return None
        raise
    thread = (got or {}).get("thread") if isinstance(got, dict) else None
    if not isinstance(thread, dict):
        raise CodexError("thread/read answered without a thread")
    return thread


def _interrupt(c: _Client, thread_id: str) -> str:
    got = c.call("thread/turns/list", {"threadId": thread_id, "limit": 5, "itemsView": "notLoaded"})
    data = got.get("data") if isinstance(got, dict) else None
    if not isinstance(data, list):
        raise CodexError("thread/turns/list answered without a list of turns")
    turns = [t for t in data if isinstance(t, dict) and t.get("status") == "inProgress"]
    if not turns:
        return f"thread {thread_id}: no turn in progress"
    for t in turns:
        if not isinstance(t.get("id"), str):
            raise CodexError("thread/turns/list lists a turn in progress without an id")
        c.call("turn/interrupt", {"threadId": thread_id, "turnId": t["id"]})
    return f"thread {thread_id}: {len(turns)} turn(s) interrupted"


def _check_home(init: dict[str, Any], sock: Path, home: str | Path | None) -> None:
    """Refuse a daemon that is not the one the launch started on: its ``initialize`` names another codexHome."""
    if home is not None and not _same(init.get("codexHome"), Path(home)):
        raise CodexError(f"the daemon on {sock} runs CODEX_HOME {str(init.get('codexHome'))[:300]!r}, not {home}, "
                         "the one the launch started on")


def stop(thread_id: str, *, sock: Path | None = None, home: str | Path | None = None) -> str:
    """Interrupt ``thread_id``'s turn in progress on the daemon at ``sock`` (whose codexHome must be ``home`` when
    given); say what happened (never raises)."""
    path = sock or control_socket()
    try:
        with _session(path) as (c, init):
            _check_home(init, path, home)
            return _interrupt(c, thread_id)
    except CodexError as exc:
        return f"thread {thread_id} could not be interrupted ({exc})"


def _observe(ref: lease_mod.LaunchThread) -> dict[str, Any] | None:
    """``thread/read`` of a recorded thread on the daemon it was started on: the thread, None when that daemon says
    it has none, :class:`CodexError` otherwise (another daemon answering on the socket included)."""
    path = Path(ref.socket)
    with _session(path) as (c, init):
        _check_home(init, path, ref.codex_home)
        return _read(c, ref.thread_id)


def _ref(store: CoordStore, ln: Any) -> lease_mod.LaunchThread:
    """The thread ``ln``'s starter recorded, or, for an active row with none, its session id on this process's
    daemon."""
    from levain.autonomic.durable_sqlite import read_transaction

    with read_transaction(store._connect) as conn:
        got = lease_mod.thread_of(conn, ln.launch_id)
    if got is not None:
        return got
    return lease_mod.LaunchThread(ln.harness_session_id, None, os.path.realpath(codex_home()), str(control_socket()))


# --- the launch --------------------------------------------------------------------------------------------------
def launch(store: CoordStore, ln: Any) -> Any:
    """Start pending Codex launch ``ln`` (see the module docstring); returns a
    :class:`levain.coordinator.launch.Outcome`. The caller has checked it is a pending coordinator launch."""
    from levain.coordinator import launch as cl

    launch_id = ln.launch_id

    def left(cause: str, remedy: str | None = None) -> Any:
        return cl.Outcome(launch_id, False, refusal=cl._plain(f"{cause} (launch {launch_id} left pending, until "
                                                              f"{ln.until})"), remedy=remedy)

    if not ln.cwd or not Path(ln.cwd).is_dir():
        return left(f"the launch's directory {ln.cwd!r} is not a directory")
    real = os.path.realpath(ln.cwd)
    home = codex_home()
    sock = control_socket(home)
    try:
        with _session(sock) as (_c, init):
            daemon_home = init.get("codexHome")
    except CodexError as exc:
        return left(f"the Codex app-server daemon is not answering ({exc})", DAEMON_REMEDY)
    if not _same(daemon_home, home):
        return left(f"the Codex daemon on {sock} runs CODEX_HOME {str(daemon_home)[:300]!r}, not {home}, whose "
                    "hooks doctor checks")
    live, detail = global_hook_live(Path(real))
    if not live:
        return left(f"Codex's consent hook is not live: {detail}")
    ready, detail = _workspace_ready(Path(real))
    if not ready:
        return left(detail)
    try:
        cl._brief_dir(ln)
    except (CoordError, OSError) as exc:
        return left(f"the launch's briefs directory refused ({type(exc).__name__}: {str(exc)[:300]})")
    got, why = store.launch_claim_spawn(launch_id, os.getpid())
    if why is not None:
        return cl.Outcome(launch_id, False, refusal=why)
    recorded_home = os.path.realpath(str(daemon_home))
    tid: str | None = None
    settled = False

    def fail(cause: str) -> Any:
        if tid:
            cause += f" (thread {tid} stays in your Codex daemon: Levain never archives one)"
        cause = cl._plain(cause)
        try:
            store.launch_end(launch_id, "failed", cause)
        except (CoordError, OSError, sqlite3.Error) as exc:
            cause = cl._plain(f"{cause} (and the launch could not be written failed: {exc})")
        return cl.Outcome(launch_id, False, refusal=cause)

    def stop_ours() -> str:
        nonlocal settled
        settled = True
        return stop(tid, sock=sock, home=recorded_home) if tid else "no thread was started"

    def record(turn_id: str | None) -> str | None:
        # the thread is Levain's to stop only once a row names it: written before the turn starts and again with
        # the turn's id, fenced by the spawn claim and the lease token, so a starter killed past this point leaves
        # the watcher a thread to interrupt
        with store._write() as (conn, now):
            return lease_mod.record_thread(conn, now, launch_id, os.getpid(), thread_id=tid, turn_id=turn_id,
                                           codex_home=recorded_home, socket=str(sock))

    try:
        brief = cl._write_brief(store, got)
        name = cl.session_name(got)
        start = {"cwd": real, **({"model": got.model} if got.model else {})}
        turn = {"input": [{"type": "text", "text": cl._prompt(got, brief)}]}
        argv_sha = "sha256:" + hashlib.sha256(canonical_bytes(
            [["thread/start", start], ["thread/name/set", name], ["turn/start", turn]])).hexdigest()
        try:
            with _session(sock) as (c, _init):
                started = c.call("thread/start", start)
                th = (started or {}).get("thread") if isinstance(started, dict) else None
                tid = th.get("id") if isinstance(th, dict) and isinstance(th.get("id"), str) else None
                if tid is None:
                    return fail("thread/start answered without a thread id")
                why = record(None)
                if why is not None:
                    settled = True                  # no turn was started: nothing runs to interrupt
                    return fail(f"its turn was not started: {why}")
                c.call("thread/name/set", {"threadId": tid, "name": name})
                begun = c.call("turn/start", {"threadId": tid, **turn})
                turn_id = ((begun or {}).get("turn") or {}).get("id") if isinstance(begun, dict) else None
                if not isinstance(turn_id, str):
                    return fail(f"turn/start answered without a turn id; {stop_ours()}")
                why = record(turn_id)
                if why is not None:
                    return fail(f"its turn could not be recorded: {why}; {stop_ours()}")
        except CodexError as exc:
            return fail(f"the Codex launch failed ({exc}); {stop_ours()}")
        try:
            th = read_thread(tid, sock=sock)
        except CodexError as exc:
            return fail(f"the thread {tid} could not be read back ({exc}); {stop_ours()}")
        if th is None:
            return fail(f"the daemon does not list thread {tid} it just started; {stop_ours()}")
        if th.get("name") != name:
            return fail(f"thread {tid} reads back named {str(th.get('name'))[:120]!r}, not {name}; {stop_ours()}")
        if not _same(th.get("cwd"), Path(real)):
            return fail(f"thread {tid} reads back in {str(th.get('cwd'))[:300]!r}, not {real}; {stop_ours()}")
        status = th.get("status") if isinstance(th.get("status"), dict) else {}
        if status.get("type") != "active":
            # an idle thread runs no prompt: activating it would record a coordinator that is not running
            return fail(f"turn {turn_id} read back {str(status.get('type') or 'no status')[:60]} on thread {tid}, "
                        f"not active (a turn that ended before the read back, or never ran); {stop_ours()}")
        try:
            _active, refused = store.launch_activate(launch_id, harness_session_id=tid, argv_sha256=argv_sha)
        except LeaseRefused as exc:
            return fail(f"activation refused ({exc}); {stop_ours()}")
        if refused is not None:
            return cl.Outcome(launch_id, False, refusal=cl._plain(f"{refused}; {stop_ours()}"))
        settled = True
        return cl.Outcome(launch_id, True, attach=f"codex resume --remote {shlex.quote(f'unix://{sock}')} "
                                                  f"{shlex.quote(tid)}", harness_session_id=tid)
    except (CoordError, OSError, sqlite3.Error) as exc:
        return fail(f"the launch faulted ({type(exc).__name__}: {str(exc)[:300]})"
                    + (f"; {stop_ours()}" if tid and not settled else ""))
    finally:
        if tid and not settled:            # a BaseException on the way out (KeyboardInterrupt, SystemExit)
            stop_ours()


# --- the watcher's Codex half ------------------------------------------------------------------------------------
def watch_once(store: CoordStore) -> list[str]:
    """The Codex half of one ``levain coord watch`` pass (the caller has already started the pending launches and
    written the expiries). Every thread is read on the daemon its launch recorded, whose ``initialize`` must name the
    codexHome recorded with it (another daemon on that socket observes nothing), one thread at a time: a thread that
    cannot be read is a line, and every other thread is still read.

    (b) an ``active`` Codex launch whose thread the daemon answers it does not have, in two reads
    :data:`UNLISTED_RECHECK` seconds apart, ends (``launch_end_unlisted``); (c) the coordinator holding the live
    lease, a Codex launch, gets a service beat only while its thread is active (a turn in progress); (d) a launch of
    this store that is over and recorded a thread (activated, or started by a starter that died before activation)
    has its turn in progress interrupted, and once its thread is seen not running it leaves the list for good
    (:func:`levain.coordinator.lease.mark_swept`), so finished launches never hide an older one."""
    lines: list[str] = []
    active = [ln for ln in store.launches_active() if ln.harness == "codex" and ln.harness_session_id]
    over = _over_codex(store)
    if not active and not over:
        return lines
    held = store.coord_lease()
    refs = {ln.launch_id: _ref(store, ln) for ln in active}
    seen: dict[str, dict[str, Any] | None] = {}

    def look(launch_id: str, ref: lease_mod.LaunchThread) -> None:
        try:
            seen[launch_id] = _observe(ref)
        except CodexError as exc:
            seen.pop(launch_id, None)
            lines.append(f"launch {launch_id}: thread {ref.thread_id} could not be read ({exc}): not observed")

    for ln in active:
        look(ln.launch_id, refs[ln.launch_id])
    missing = [ln for ln in active if ln.launch_id in seen and seen[ln.launch_id] is None]
    if missing:
        # one read that misses a thread is not enough to call a death: a launch ends only when a second agrees
        time.sleep(UNLISTED_RECHECK)
        for ln in missing:
            look(ln.launch_id, refs[ln.launch_id])
    for ln in active:
        if ln.launch_id not in seen or seen[ln.launch_id] is not None:
            continue
        try:
            ended = store.launch_end_unlisted(ln.launch_id)
        except (CoordError, sqlite3.Error) as exc:
            lines.append(f"launch {ln.launch_id}: its thread is gone, and it could not be written ended ({exc})")
            continue
        if ended is not None:
            lines.append(f"launch {ln.launch_id} ended: {ended.cause} (the Codex daemon no longer has its thread)")
    if held is not None and held.live and held.holder != "operator":
        holder = next((ln for ln in active if ln.session_key == held.holder and ln.role == "coordinator"), None)
        th = None if holder is None else seen.get(holder.launch_id)
        if th is not None:
            status = th.get("status") if isinstance(th.get("status"), dict) else {}
            if status.get("type") != "active":
                lines.append(f"lease {held.token} held by {holder.launch_id}: its thread is "
                             f"{status.get('type') or 'unknown'}, not beaten (the lease lapses at {held.until})")
            else:
                try:
                    got = store.coord_service_beat(held.token)
                    lines.append(f"lease {held.token} held by {holder.launch_id}: beat to {got.until}" if got else
                                 f"lease {held.token} held by {holder.launch_id}: it wrote nothing as coordinator in "
                                 f"the last {int(lease_mod.SERVICE_BEAT_WINDOW.total_seconds() // 60)} min (the "
                                 f"watcher beats only a holder that wrote within that window), not beaten "
                                 f"(the lease lapses at {held.until})")
                except (CoordError, sqlite3.Error) as exc:
                    lines.append(f"lease {held.token}: not beaten ({exc})")
    for ln, ref in over:
        ref = ref or lease_mod.LaunchThread(ln.harness_session_id, None, os.path.realpath(codex_home()),
                                            str(control_socket()))
        try:
            th = _observe(ref)
        except CodexError as exc:
            lines.append(f"launch {ln.launch_id} {ln.state}: thread {ref.thread_id} could not be read ({exc})")
            continue
        status = th.get("status") if isinstance(th, dict) and isinstance(th.get("status"), dict) else {}
        if status.get("type") == "active":
            lines.append(f"launch {ln.launch_id} {ln.state} ({ln.cause}): "
                         f"{stop(ref.thread_id, sock=Path(ref.socket), home=ref.codex_home)}")
            continue
        seen_as = "gone" if th is None else f"thread {status.get('type') or 'unknown'}"
        try:
            with store._write() as (conn, now):
                lease_mod.mark_swept(conn, now, ln.launch_id, seen_as)
        except (CoordError, sqlite3.Error) as exc:
            lines.append(f"launch {ln.launch_id}: its thread is not running, and it could not be marked ({exc})")
    return lines


def _over_codex(store: CoordStore) -> list[tuple[Any, lease_mod.LaunchThread | None]]:
    """The Codex launches of this store that are over, recorded a thread and were not yet seen finished."""
    from levain.autonomic.durable_sqlite import read_transaction

    with read_transaction(store._connect) as conn:
        return lease_mod.over_unswept(conn, "codex")

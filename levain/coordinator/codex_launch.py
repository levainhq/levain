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

  1. the checks that need no spawn: the launch's directory, the hook check (doctor's own Codex check of the
     directory, held against the CODEX_HOME the daemon itself reports, :func:`hook_live`), the daemon answering, the
     briefs directory. A refusal here writes nothing: the launch stays pending and expires unspawned (no death);
  2. the one-spawner compare-and-set (:func:`levain.coordinator.lease.claim_spawn`);
  3. the brief file (the module :mod:`levain.coordinator.launch` writes it, the same file a Claude Code launch gets);
  4. the spawn: ``thread/start {cwd}`` (plus ``model`` when the setting names one; no approval or sandbox override,
     so the user's own Codex config decides), ``thread/name/set`` to the launch's session name, ``turn/start`` with
     the prompt naming the brief; the client disconnects once the turn is accepted;
  5. the read back: a fresh connection's ``thread/read``, which must list the thread under our name in the launch's
     realpath directory. The thread id is the harness session id the launch records;
  6. activation, the token re-checked (:func:`levain.coordinator.lease.activate`). Refused: the session is stopped.

"Stop" is ``turn/interrupt`` of the thread's turn in progress; Levain never archives or deletes a thread (its rollout
is the user's record), and never starts or stops the daemon (it is the user's, and its updater outlives a stop).

:func:`watch_once` is the Codex half of ``levain coord watch``: an active Codex launch whose thread the daemon no
longer knows in two reads ends; the coordinator holding the lease is beaten only while its thread is active (a turn in
progress); a launch that is over has its turn in progress interrupted.

[seat] choices (C3c, 1021+18 lane c3c): the daemon socket over ``codex exec --json``; Levain never starts or stops the
daemon; the hook check held against the daemon's own ``codexHome``; Codex's own trust of the gate required (it skips an
untrusted hook silently); no approval or sandbox override on ``thread/start``; stop is ``turn/interrupt``, never an
archive or delete; ``levain coord agent codex`` never runs ``levain init`` (it would rewire the machine's one Codex
hooks file); the timeouts below; "active" (a turn in progress) is what earns a service beat, as a Claude Code row that
is not ``done`` does. Stdlib only.

Known edges: a Codex coordinator runs under the user's own approval and sandbox settings, so a command outside its
workspace (its own ``levain coord claim`` writes the consent directory) may ask for approval, and an approval request
waits until someone opens the thread (measured: it never fails the turn). Such a thread reads ``active`` with no flag
to a client that has not resumed it, so the watcher cannot tell it from one working. The watcher reads threads only by
the id an ``active`` row recorded: a thread a killed starter left before activation is not swept by its name (the
daemon's ``thread/list`` can find it by name; that sweep is not built), and its one turn ends on its own.
"""
from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import os
import socket
import sqlite3
import struct
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from levain.contracts.canon import canonical_bytes
from levain.coordinator.lease import LeaseRefused
from levain.coordinator.store import CoordError, CoordStore

__all__ = ["CONSENT_ROW", "DAEMON_REMEDY", "GATE_ROW", "CodexError", "codex_home", "control_socket", "hook_live",
           "launch", "read_thread", "setup", "watch_once"]

CONSENT_ROW = "Codex consent hook"
GATE_ROW = "Codex consent gate (PreToolUse)"
_LIVE = "wired to this install"
_GATE_TRUSTED = "wired to this install and trusted by Codex"
_HOOKS_ROW = "Codex hooks"
_HOOKS_OFF = "turned off by"
DAEMON_REMEDY = "codex app-server daemon start"
_CONNECT_TIMEOUT = 10.0
_CALL_TIMEOUT = 60.0
_MAX_MESSAGE = 16 * 1024 * 1024 + 4096     # the daemon's own header names 16777216 bytes unfragmented
_GUID = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
UNLISTED_RECHECK = 2.0


class CodexError(Exception):
    """The daemon could not be reached, or answered with an error (``.error`` holds its error object then)."""

    def __init__(self, message: str, error: Any = None) -> None:
        super().__init__(message)
        self.error = error


def codex_home() -> Path:
    """The CODEX_HOME doctor checks: ``$CODEX_HOME``, else ``~/.codex``."""
    return Path(os.environ.get("CODEX_HOME") or os.path.expanduser("~/.codex"))


def control_socket(home: Path | None = None) -> Path:
    return (home or codex_home()) / "app-server-control" / "app-server-control.sock"


# --- the client: WebSocket (RFC 6455) over the unix socket, JSON-RPC without the "jsonrpc" field ----------------
class _Client:
    def __init__(self, path: Path, timeout: float = _CONNECT_TIMEOUT) -> None:
        self._s = socket.socket(socket.AF_UNIX)
        try:
            self._s.settimeout(timeout)
            self._s.connect(str(path))
            key = base64.b64encode(os.urandom(16))
            self._s.sendall(b"GET / HTTP/1.1\r\nHost: localhost\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                            b"Sec-WebSocket-Key: " + key + b"\r\nSec-WebSocket-Version: 13\r\n\r\n")
            buf = b""
            while b"\r\n\r\n" not in buf:
                chunk = self._s.recv(4096)
                if not chunk or len(buf) > 65536:
                    raise CodexError("the daemon closed the connection during the WebSocket handshake")
                buf += chunk
            head, self._buf = buf.split(b"\r\n\r\n", 1)
            lines = head.split(b"\r\n")
            if b" 101 " not in lines[0] + b" ":
                raise CodexError(f"the daemon refused the WebSocket upgrade: {lines[0][:120].decode(errors='replace')}")
            want = base64.b64encode(hashlib.sha1(key + _GUID).digest())
            accept = next((ln.split(b":", 1)[1].strip() for ln in lines[1:]
                           if ln.lower().startswith(b"sec-websocket-accept:")), None)
            if accept != want:
                raise CodexError("the daemon's WebSocket accept key does not match")
        except BaseException:
            self._s.close()
            raise
        self._n = 0

    def close(self) -> None:
        with contextlib.suppress(OSError):
            self._send(8, b"")
        self._s.close()

    def _send(self, op: int, payload: bytes) -> None:
        n = len(payload)
        head = bytes([0x80 | op]) + (bytes([0x80 | n]) if n < 126 else
                                     bytes([0x80 | 126]) + struct.pack(">H", n) if n < 65536 else
                                     bytes([0x80 | 127]) + struct.pack(">Q", n))
        mask = os.urandom(4)
        self._s.sendall(head + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(payload)))

    def _need(self, n: int) -> bytes:
        while len(self._buf) < n:
            chunk = self._s.recv(65536)
            if not chunk:
                raise CodexError("the daemon closed the connection")
            self._buf += chunk
        out, self._buf = self._buf[:n], self._buf[n:]
        return out

    def _message(self) -> dict[str, Any]:
        msg = b""
        while True:
            h = self._need(2)
            fin, op, n = h[0] & 0x80, h[0] & 0x0F, h[1] & 0x7F
            if n == 126:
                n = struct.unpack(">H", self._need(2))[0]
            elif n == 127:
                n = struct.unpack(">Q", self._need(8))[0]
            if h[1] & 0x80 or len(msg) + n > _MAX_MESSAGE:
                raise CodexError("the daemon sent a frame this client does not take (masked, or too large)")
            payload = self._need(n)
            if op == 9:
                self._send(10, payload)
            elif op == 8:
                raise CodexError("the daemon closed the connection")
            elif op in (0, 1, 2):
                msg += payload
                if fin:
                    try:
                        got = json.loads(msg.decode("utf-8"))
                    except (UnicodeDecodeError, ValueError) as exc:
                        raise CodexError(f"the daemon sent a message that is not JSON ({exc})") from None
                    if isinstance(got, dict):
                        return got
                    msg = b""

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


def setup(directory: Path) -> tuple[Path, bool, str]:
    """``levain coord agent codex``'s check: ``(resolved directory, hook live, detail)``. Never installs: ``levain init
    --adapter codex`` rewires the machine's one Codex hooks file, so the directory must already be your live Codex
    install."""
    target = Path(directory).expanduser().resolve()
    live, detail = hook_live(target)
    if not live:
        detail += ("; a Codex coordinator runs in your Codex install (Codex has one per machine), which Levain never "
                   "makes for you: `levain init --adapter codex` there first")
    return target, live, detail


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
    turns = [t for t in (got or {}).get("data") or [] if isinstance(t, dict) and t.get("status") == "inProgress"]
    if not turns:
        return f"thread {thread_id}: no turn in progress"
    for t in turns:
        c.call("turn/interrupt", {"threadId": thread_id, "turnId": t.get("id")})
    return f"thread {thread_id}: {len(turns)} turn(s) interrupted"


def stop(thread_id: str, *, sock: Path | None = None) -> str:
    """Interrupt ``thread_id``'s turn in progress; say what happened (never raises)."""
    try:
        with _session(sock or control_socket()) as (c, _init):
            return _interrupt(c, thread_id)
    except CodexError as exc:
        return f"thread {thread_id} could not be interrupted ({exc})"


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
    live, detail = hook_live(Path(ln.cwd))
    if not live:
        return left(f"the consent hook is not live in {ln.cwd}: {detail}")
    try:
        cl._brief_dir(ln)
    except (CoordError, OSError) as exc:
        return left(f"the launch's briefs directory refused ({type(exc).__name__}: {str(exc)[:300]})")
    got, why = store.launch_claim_spawn(launch_id, os.getpid())
    if why is not None:
        return cl.Outcome(launch_id, False, refusal=why)

    def fail(cause: str) -> Any:
        cause = cl._plain(cause)
        try:
            store.launch_end(launch_id, "failed", cause)
        except (CoordError, OSError, sqlite3.Error) as exc:
            cause = cl._plain(f"{cause} (and the launch could not be written failed: {exc})")
        return cl.Outcome(launch_id, False, refusal=cause)

    tid: str | None = None
    settled = False

    def stop_ours() -> str:
        nonlocal settled
        settled = True
        return stop(tid, sock=sock) if tid else "no thread was started"

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
                c.call("thread/name/set", {"threadId": tid, "name": name})
                c.call("turn/start", {"threadId": tid, **turn})
        except CodexError as exc:
            return fail(f"the Codex launch failed ({exc}); {stop_ours()}")
        try:
            th = read_thread(tid, sock=sock)
        except CodexError as exc:
            return fail(f"the thread {tid} could not be read back ({exc}); {stop_ours()}")
        if th is None:
            return fail(f"the daemon does not list thread {tid} it just started")
        if th.get("name") != name:
            return fail(f"thread {tid} reads back named {str(th.get('name'))[:120]!r}, not {name}; {stop_ours()}")
        if not _same(th.get("cwd"), Path(real)):
            return fail(f"thread {tid} reads back in {str(th.get('cwd'))[:300]!r}, not {real}; {stop_ours()}")
        try:
            _active, refused = store.launch_activate(launch_id, harness_session_id=tid, argv_sha256=argv_sha)
        except LeaseRefused as exc:
            return fail(f"activation refused ({exc}); {stop_ours()}")
        if refused is not None:
            return cl.Outcome(launch_id, False, refusal=cl._plain(f"{refused}; {stop_ours()}"))
        settled = True
        return cl.Outcome(launch_id, True, attach=f"codex resume --remote unix://{sock} {tid}",
                          harness_session_id=tid)
    except (CoordError, OSError, sqlite3.Error) as exc:
        return fail(f"the launch faulted ({type(exc).__name__}: {str(exc)[:300]})"
                    + (f"; {stop_ours()}" if tid and not settled else ""))
    finally:
        if tid and not settled:            # a BaseException on the way out (KeyboardInterrupt, SystemExit)
            stop_ours()


# --- the watcher's Codex half ------------------------------------------------------------------------------------
def watch_once(store: CoordStore) -> list[str]:
    """The Codex half of one ``levain coord watch`` pass (the caller has already started the pending launches):
    (b) an ``active`` Codex launch whose thread the daemon answers it does not have, in two reads
    :data:`UNLISTED_RECHECK` seconds apart, ends (``launch_end_unlisted``); (c) the coordinator holding the live
    lease, a Codex launch, gets a service beat only while its thread is active (a turn in progress); (d) a launch of
    this store that is over and whose thread is active has its turn interrupted. A daemon that cannot be read
    observes nothing."""
    from levain.coordinator import lease as lease_mod

    lines: list[str] = []
    active = [ln for ln in store.launches_active() if ln.harness == "codex" and ln.harness_session_id]
    held = store.coord_lease()
    over = _over_codex(store)
    if not active and not over:
        return lines
    sock = control_socket()
    seen: dict[str, dict[str, Any] | None] = {}
    try:
        with _session(sock) as (c, _init):
            for ln in active + over:
                seen[ln.harness_session_id] = _read(c, ln.harness_session_id)
        missing = [ln for ln in active if seen[ln.harness_session_id] is None]
        if missing:
            # one read that misses a thread is not enough to call a death: a launch ends only when a second agrees
            time.sleep(UNLISTED_RECHECK)
            with _session(sock) as (c, _init):
                for ln in missing:
                    seen[ln.harness_session_id] = _read(c, ln.harness_session_id)
    except CodexError as exc:
        return [f"the Codex daemon could not be read ({exc}): no Codex session observed"]
    for ln in active:
        if seen[ln.harness_session_id] is not None:
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
        th = None if holder is None else seen.get(holder.harness_session_id)
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
                                 f"{int(lease_mod.SERVICE_BEAT_WINDOW.total_seconds() // 60)} min, not beaten "
                                 f"(the lease lapses at {held.until})")
                except (CoordError, sqlite3.Error) as exc:
                    lines.append(f"lease {held.token}: not beaten ({exc})")
    for ln in over:
        th = seen.get(ln.harness_session_id)
        status = th.get("status") if isinstance(th, dict) and isinstance(th.get("status"), dict) else {}
        if status.get("type") == "active":
            lines.append(f"launch {ln.launch_id} {ln.state} ({ln.cause}): {stop(ln.harness_session_id, sock=sock)}")
    return lines


def _over_codex(store: CoordStore) -> list[Any]:
    """The Codex launches of this store that are over and recorded a thread (they went active once)."""
    from levain.autonomic.durable_sqlite import read_transaction
    from levain.coordinator import lease as lease_mod

    with read_transaction(store._connect) as conn:
        return lease_mod.over_with_session(conn, "codex")

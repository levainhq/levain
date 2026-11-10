"""The ``--setting-sources ""`` tripwire's probe. The empty source list is undocumented in Claude Code's own help, so
what it does is a measured fact about one version. This probe re-measures it with no model call and nothing off the loopback: a
scratch project plants a SessionStart hook that touches a marker, ``claude -p`` runs once WITHOUT the flag (the control:
the hook must fire) and once WITH ``--setting-sources ""`` (the hook must not), each with a fake key, its own scratch HOME
and config dir (never the operator's), and its own local listener as the API address.

The evidence is positive, never an absence. The listener never answers; it reads each connection's request line. A
connection alone proves nothing: the client sends a preflight before it runs any hook. The anchor is the first model
request, a ``POST`` to ``/v1/messages``, which the client sends only after its SessionStart hooks. The control validates
that order on this binary from times, not from one sample: its marker must have been seen, and stamped, earlier than its
model request's arrival by more than the poll interval. Only then does the empty-list run's model request without a
marker read ``held``, and only after a short settle in which its marker is still absent. A rejected flag, a crash, a
run that never sends its model request, or a timeout is ``inconclusive``; the empty-list marker appearing at any moment
is ``leaked``. Every return, whatever its verdict, first kills both runs as whole process groups and then re-checks the
empty-list marker, so a hook that lands during an early return still reads ``leaked``."""
from __future__ import annotations

import json
import os
import selectors
import shlex
import shutil
import signal
import socket
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from levain.host_guard import SEAT_SOURCE_ARGS
from levain.slot.probe import exited_unreaped

__all__ = ["OVERRUN", "ProbeResult", "run_probe"]

_STDERR_CAP = 300
_LINE_CAP = 4096
_MODEL_PATH = "/v1/messages"
_POLL = 0.02  # the loop's longest sleep: a socket or stderr event wakes it sooner
_ORDER_MARGIN = 0.05  # the control's marker must precede its model request by more than this (above _POLL)
_SETTLE = 0.5  # the bounded wait before a held verdict, and before an early inconclusive return
_REAP_WAIT = 0.5  # after SIGKILL, how long one run's teardown waits for its leader to be reaped
_ERR_CHUNK = 65536  # one stderr read
_FINAL_READS = 4  # the stderr reads after the group is killed, so a reject's last line reaches the detail
OVERRUN = _SETTLE + 2 * _REAP_WAIT  # how far past ``timeout`` the probe can run: one settle plus both teardowns

_LEAKED_DETAIL = '--setting-sources "" still loaded the project settings (its hook fired)'


def _is_model_request(head: bytes) -> bool:
    """True when ``head`` holds a complete request line that POSTs to the model endpoint (any query string)."""
    end = head.find(b"\r\n")
    if end < 0:
        return False
    parts = head[:end].decode("latin-1").split(" ")
    if len(parts) != 3 or parts[0] != "POST":
        return False
    return parts[1].split("?", 1)[0] == _MODEL_PATH


@dataclass(frozen=True)
class ProbeResult:
    verdict: str  # "held" | "leaked" | "inconclusive"
    detail: str


class _Run:
    """One ``claude -p`` run: its project, marker, listener, bounded stderr tail and process group. Its listener, its
    connections and its stderr pipe are registered on the probe's shared selector."""

    def __init__(self, root: Path, name: str, binary: str, extra: tuple[str, ...],
                 sel: selectors.BaseSelector) -> None:
        self.name = name
        self.sel = sel
        self.proj = root / name
        (self.proj / ".claude").mkdir(parents=True)
        self.marker = root / f"{name}.marker"
        hook = {"hooks": {"SessionStart": [{"hooks": [{"type": "command",
                                                       "command": f"touch {shlex.quote(str(self.marker))}"}]}]}}
        (self.proj / ".claude" / "settings.json").write_text(json.dumps(hook))
        home = root / f"{name}-home"
        (home / "cfg").mkdir(parents=True)
        self.heads: dict[socket.socket, bytes] = {}
        self.err_tail = b""
        self.err_open = False
        self.post_mono: float | None = None  # when the model request's line was read (monotonic)
        self.post_wall: float | None = None  # the same moment on the wall clock, for the marker's mtime
        self.marker_seen: float | None = None  # the first poll that found the marker (monotonic)
        self.proc: subprocess.Popen[bytes] | None = None
        self._stopped = False
        self._killed = False
        self.listener = socket.create_server(("127.0.0.1", 0))
        self.listener.setblocking(False)
        port = self.listener.getsockname()[1]
        env = {"PATH": os.environ.get("PATH", ""), "HOME": str(home), "CLAUDE_CONFIG_DIR": str(home / "cfg"),
               "ANTHROPIC_API_KEY": "sk-ant-levain-probe-not-a-key", "ANTHROPIC_BASE_URL": f"http://127.0.0.1:{port}",
               "DISABLE_AUTOUPDATER": "1"}
        try:
            self.proc = subprocess.Popen([binary, "-p", "levain probe", *extra], cwd=self.proj, env=env,
                                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                         start_new_session=True)
            assert self.proc.stderr is not None
            os.set_blocking(self.proc.stderr.fileno(), False)
            self.err_open = True
            sel.register(self.listener, selectors.EVENT_READ, (self, "accept"))
            sel.register(self.proc.stderr, selectors.EVENT_READ, (self, "err"))
        except BaseException:
            self.stop()
            raise

    def handle(self, kind: str, obj: object) -> None:
        if kind == "accept":
            self._accept()
        elif kind == "conn":
            assert isinstance(obj, socket.socket)
            self._read_conn(obj)
        else:
            self._drain_err()

    def _accept(self) -> None:
        while True:
            try:
                conn, _addr = self.listener.accept()
            except (BlockingIOError, InterruptedError):
                return
            conn.setblocking(False)
            self.heads[conn] = b""
            self.sel.register(conn, selectors.EVENT_READ, (self, "conn"))

    def _read_conn(self, conn: socket.socket) -> None:
        """Read what a connection has sent so far; the first complete model request line stamps its arrival. Never
        answers, so the client cannot reuse a connection for a second request."""
        try:
            data = conn.recv(_LINE_CAP - len(self.heads[conn]))
        except (BlockingIOError, InterruptedError):
            return
        except OSError:
            data = b""
        self.heads[conn] += data
        if self.post_mono is None and _is_model_request(self.heads[conn]):
            self.post_mono, self.post_wall = time.monotonic(), time.time()
        if not data or len(self.heads[conn]) >= _LINE_CAP:
            self._unregister(conn)

    def _drain_err(self, reads: int = 1) -> None:
        """Move up to ``reads`` chunks of the stderr pipe into the bounded tail: the LAST ``_STDERR_CAP`` bytes are
        kept. Bounded per call, so a pipe that never runs dry cannot keep the judge from its deadline; the selector
        reports what is left on the next poll."""
        if not self.err_open or self.proc is None or self.proc.stderr is None:
            return
        fd = self.proc.stderr.fileno()
        for _ in range(reads):
            try:
                chunk = os.read(fd, _ERR_CHUNK)
            except (BlockingIOError, InterruptedError):
                return
            except OSError:
                chunk = b""
            if not chunk:
                self._unregister(self.proc.stderr)
                self.proc.stderr.close()
                self.err_open = False
                return
            self.err_tail = (self.err_tail + chunk)[-_STDERR_CAP:]

    def _unregister(self, obj) -> None:
        try:
            self.sel.unregister(obj)
        except (KeyError, ValueError):
            pass

    def observe(self) -> None:
        if self.marker_seen is None and self.marker.exists():
            self.marker_seen = time.monotonic()

    def ordered(self) -> bool:
        """The control's order holds: its marker was seen, and stamped, before its model request arrived, each by more
        than the poll interval."""
        if self.post_mono is None or self.post_wall is None or self.marker_seen is None:
            return False
        if self.marker_seen + _ORDER_MARGIN >= self.post_mono:
            return False
        try:
            stamped = self.marker.stat().st_mtime
        except OSError:
            return False
        return stamped + _ORDER_MARGIN < self.post_wall

    def exited(self) -> bool:
        """True once the leader has exited. It is not reaped here: it stays a zombie until ``_kill``, so its pid, which
        is the group's id, cannot be reused before the group is signalled."""
        if self.proc is None:
            return False
        return self.proc.returncode is not None or exited_unreaped(self.proc.pid, 0.0)

    def why_exited(self) -> str:
        self._kill()
        self._drain_err(_FINAL_READS)
        err = self.err_tail.decode("utf-8", "replace").strip()
        code = self.proc.returncode if self.proc is not None else None
        return f"exited {code}" + (f": {err}" if err else " with no stderr")

    def _kill(self) -> None:
        """Kill the run's process group once, then reap its leader within a short fixed wait. Only this method reaps,
        and it signals first, so the group is never signalled by a number whose leader was already reaped."""
        if self._killed or self.proc is None:
            return
        self._killed = True
        try:
            os.killpg(self.proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        try:
            self.proc.wait(timeout=_REAP_WAIT)
        except (OSError, subprocess.SubprocessError):
            pass

    def stop(self) -> None:
        """Kill the run's process group (once, via ``_kill``), close its sockets and pipe. Idempotent."""
        if self._stopped:
            return
        self._stopped = True
        if self.proc is not None:
            self._kill()
            self._drain_err(_FINAL_READS)
            if self.err_open and self.proc.stderr is not None:
                self._unregister(self.proc.stderr)
                self.proc.stderr.close()
                self.err_open = False
        for s in (*self.heads, self.listener):
            self._unregister(s)
            try:
                s.close()
            except OSError:
                pass


def _pump(sel: selectors.BaseSelector, runs: tuple[_Run, ...], wait: float) -> None:
    for key, _mask in sel.select(wait):
        run, kind = key.data
        run.handle(kind, key.fileobj)
    for r in runs:
        r.observe()


def _settle(sel: selectors.BaseSelector, ctl: _Run, empty: _Run, *, until_empty_acts: bool) -> None:
    """Keep polling for up to ``_SETTLE``; with ``until_empty_acts``, stop early once the empty-list run has sent its
    model request or exited. Returns early as soon as the empty-list marker is seen."""
    end = time.monotonic() + _SETTLE
    while time.monotonic() < end:
        _pump(sel, (ctl, empty), min(_POLL, max(0.0, end - time.monotonic())))
        if empty.marker_seen is not None:
            return
        if until_empty_acts and (empty.post_mono is not None or empty.exited()):
            return


def _judge(ctl: _Run, empty: _Run, sel: selectors.BaseSelector, timeout: float) -> ProbeResult:
    t0 = time.monotonic()
    ctl_ok: bool | None = None
    leaked = ProbeResult("leaked", _LEAKED_DETAIL)

    def early(detail: str) -> ProbeResult:
        _settle(sel, ctl, empty, until_empty_acts=True)
        return leaked if empty.marker_seen is not None else ProbeResult("inconclusive", detail[:400])

    while True:
        _pump(sel, (ctl, empty), _POLL)
        if empty.marker_seen is not None:
            return leaked
        if ctl_ok is None and ctl.post_mono is not None:
            ctl_ok = ctl.ordered()
            if not ctl_ok:
                return early("the control's project hook did not fire clearly before its model request, so the order "
                             "this probe relies on does not hold on this version")
        if ctl_ok is None and ctl.exited():
            return early(f"the control run {ctl.why_exited()}")
        if empty.post_mono is not None:
            if ctl_ok:
                _settle(sel, ctl, empty, until_empty_acts=False)
                if empty.marker_seen is not None:
                    return leaked
                return ProbeResult("held", f"the control fired its project hook before its POST {_MODEL_PATH}; "
                                   f"the empty-list run sent its POST {_MODEL_PATH} with no hook, in "
                                   f"{time.monotonic() - t0:.1f}s")
        elif empty.exited():
            return ProbeResult("inconclusive", (f'the --setting-sources "" run {empty.why_exited()} before its '
                                                "model request")[:400])
        if time.monotonic() - t0 > timeout:
            which = "the empty-list run never sent its model request" if ctl_ok else \
                "the control run never fired its project hook and sent its model request"
            return ProbeResult("inconclusive", f"{which} within {timeout:.0f}s, so the probe proves nothing")


def run_probe(binary: str, *, timeout: float = 20.0) -> ProbeResult:
    """``held``: the control fired its hook clearly before sending its model request, and the empty-list run sent its
    model request with no hook, still absent after a settle and after both runs are killed. ``leaked``: the empty-list
    run's hook fired, at any moment up to that final check. ``inconclusive``: anything else, including a rejected flag,
    a crash, an early control exit, a run that could not start and the overall ``timeout``. The whole call can run past
    ``timeout`` by at most ``OVERRUN`` seconds plus scheduling slack."""
    root: Path | None = None
    runs: list[_Run] = []
    sel: selectors.BaseSelector | None = None
    leaked_at_end = False
    try:
        sel = selectors.DefaultSelector()
        root = Path(tempfile.mkdtemp(prefix="levain-ssprobe-"))
        ctl = _Run(root, "ctl", binary, (), sel)
        runs.append(ctl)
        empty = _Run(root, "empty", binary, SEAT_SOURCE_ARGS, sel)
        runs.append(empty)
        res = _judge(ctl, empty, sel, timeout)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        res = ProbeResult("inconclusive", f"the probe could not run: {exc}"[:200])
    finally:
        for r in runs:
            r.stop()
        leaked_at_end = len(runs) == 2 and runs[1].marker.exists()
        if sel is not None:
            sel.close()
        if root is not None:
            shutil.rmtree(root, ignore_errors=True)
    return ProbeResult("leaked", _LEAKED_DETAIL) if leaked_at_end else res

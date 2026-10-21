"""levain.coordinator.awake: keep the host from idle-sleeping only while a WORK thread works (C7; gap 6).

The rule: an assertion is held while at least one thread of kind ``work`` is in state ``working``, and released when
none is. It is an IDLE-sleep assertion only (macOS ``caffeinate -i``, Linux ``systemd-inhibit --what=idle``); Levain
never writes the machine's power settings and never uses sudo. Whether it keeps a closed lid awake on battery is a
separate claim that this module does not make (C0 §10, run owed).

The holder is a child process that expires by itself (``-t``), so a Levain that dies releases the host within
:data:`HOLD_SECONDS` with no cleanup. :func:`sync` is the one reconcile, meant to follow every committed store write:
it runs under an exclusive lock beside the store, reads the working set under that lock, and renews a holder that is
running low. The holder is recorded in ``awake.json`` by pid, start time and exact argv, and is signalled only
when a process matches all three: a pid the system has since given to something else is never ours to stop.
"""
from __future__ import annotations

import fcntl
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

__all__ = ["HOLD_SECONDS", "describe", "holder_command", "sync"]

HOLD_SECONDS = 1800
FILE = "awake.json"
LOCK = "awake.lock"
# a test seam: a test run never holds the host unless a test turns this on (and points the holder at a harmless argv)
_IN_TESTS = False
# the holders this process spawned, reaped on every sync so a long-lived process leaves no zombie behind
_children: list[subprocess.Popen] = []


def holder_command(seconds: int | None = None, *, platform: str | None = None) -> list[str] | None:
    """The argv of an idle-sleep holder for this platform, or None when the host has no such tool."""
    seconds = HOLD_SECONDS if seconds is None else seconds
    platform = platform or sys.platform
    if platform == "darwin":
        exe = shutil.which("caffeinate")
        return [exe, "-i", "-t", str(seconds)] if exe else None
    if platform.startswith("linux"):
        exe = shutil.which("systemd-inhibit")
        return [exe, "--what=idle", "--who=levain", "--why=a work thread is working", "--mode=block",
                "sleep", str(seconds)] if exe else None
    return None


def _ps(pid: int, field: str) -> str | None:
    try:
        cp = subprocess.run(["ps", "-ww", "-p", str(pid), "-o", f"{field}="], capture_output=True, text=True,
                            timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    return cp.stdout.strip() if cp.returncode == 0 and cp.stdout.strip() else None


def _identity(pid: int) -> tuple[str, str] | None:
    """(start time, command line) of a live process, or None when it is gone or cannot be read."""
    start, cmd = _ps(pid, "lstart"), _ps(pid, "command")
    return (start, cmd) if start and cmd else None


def _ours(rec: dict[str, Any]) -> bool:
    """Is the process ``rec`` names still the holder we started: same pid, same start time, same argv."""
    pid, argv = rec.get("pid"), rec.get("argv")
    if not (isinstance(pid, int) and pid > 1 and isinstance(argv, list) and isinstance(rec.get("started"), str)):
        return False
    got = _identity(pid)
    return got is not None and got[0] == rec["started"] and got[1] == " ".join(argv)


def _read(path: Path) -> dict[str, Any]:
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _write(path: Path, rec: dict[str, Any]) -> None:
    fd, tmp = tempfile.mkstemp(prefix=".awake-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(rec, fh)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _stop(rec: dict[str, Any]) -> None:
    """Signal the recorded holder, only if it is still the very process we started, and reap it if it is our child."""
    if not _ours(rec):
        return
    try:
        os.kill(rec["pid"], signal.SIGTERM)
    except OSError:
        return
    for proc in _children:
        if proc.pid == rec["pid"]:
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass


def _reap() -> None:
    _children[:] = [p for p in _children if p.poll() is None]


def working(store: Any) -> list[str]:
    """Ids of the threads that hold the host awake: kind work, state working."""
    return [t.thread_id for t in store.threads() if t.kind == "work" and t.state == "working"]


def sync(store: Any) -> dict[str, Any]:
    """Make the assertion match the threads: held while a work thread works, released otherwise. Never raises: a host
    that cannot hold one is reported by :func:`describe`, and a thread's own write is never failed by this."""
    if os.environ.get("LEVAIN_NO_IDLE_ASSERTION") or (os.environ.get("PYTEST_CURRENT_TEST") and not _IN_TESTS):
        # a test run that moves threads must not leave a real holder on the developer's machine
        return {"held": False, "why": "disabled by the environment (LEVAIN_NO_IDLE_ASSERTION or a test run)"}
    try:
        _reap()
        directory = Path(store.directory)
        fd = os.open(directory / LOCK, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            return _reconcile(store, directory / FILE)
        finally:
            os.close(fd)        # closing the descriptor releases the lock
    except Exception as exc:      # noqa: BLE001 - see the docstring
        return {"held": False, "why": f"could not sync: {exc}"}


def _reconcile(store: Any, path: Path) -> dict[str, Any]:
    ids = working(store)        # read under the lock: a reconcile never acts on a working set another one moved
    cur = _read(path)
    if not ids:
        if cur:
            _stop(cur)
            path.unlink(missing_ok=True)
        return {"held": False, "why": "no work thread is working"}
    argv = holder_command()
    if argv is None:
        return {"held": False, "why": "this host has no idle-sleep tool (caffeinate / systemd-inhibit)"}
    if _ours(cur) and cur.get("until", 0) - time.time() > HOLD_SECONDS / 2:
        return {"held": True, "pid": cur["pid"], "threads": ids, "why": "a work thread is working"}
    proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            start_new_session=True)
    _children.append(proc)
    ident = _identity(proc.pid)
    rec = {"pid": proc.pid, "started": ident[0] if ident else None, "argv": argv,
           "until": time.time() + HOLD_SECONDS, "threads": ids}
    _write(path, rec)
    _stop(cur)                  # the old holder goes only once the new one is recorded
    return {"held": True, "pid": proc.pid, "threads": ids, "why": "a work thread is working"}


def describe(directory: Path | str, *, working_count: int | None = None) -> tuple[bool, str]:
    """(ok, one line) for ``levain doctor``: is the assertion held, and why or why not. Read-only."""
    cur = _read(Path(directory) / FILE)
    pid = cur.get("pid")
    held = _ours(cur) and cur.get("until", 0) > time.time()
    if held:
        n = len(cur.get("threads") or [])
        return True, (f"idle sleep is held off (pid {pid}, {n} work thread(s) working at its last sync); it "
                      f"releases when none works, and by itself within {HOLD_SECONDS // 60} minutes if Levain stops")
    if working_count:
        if holder_command() is None:
            return False, (f"{working_count} work thread(s) are working but this host has no idle-sleep tool, so it "
                           "may sleep under them (macOS: caffeinate; Linux: systemd-inhibit)")
        return False, f"{working_count} work thread(s) are working and no assertion is held (it is set on the next sync)"
    return True, ("no work thread is working, so nothing holds the host awake; it sleeps by your own power settings"
                  + ("" if holder_command() else " (and this host has no idle-sleep tool for when one works)"))

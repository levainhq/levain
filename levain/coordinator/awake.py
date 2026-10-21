"""levain.coordinator.awake: keep the host from idle-sleeping only while a WORK thread works (C7; gap 6).

The rule: an assertion is held while at least one thread of kind ``work`` is in state ``working``, and released when
none is. It is an IDLE-sleep assertion only (macOS ``caffeinate -i``, Linux ``systemd-inhibit --what=idle``); Levain
never writes the machine's power settings and never uses sudo. Whether it keeps a closed lid awake on battery is a
separate claim that this module does not make (C0 §10, run owed).

The holder is a child process that expires by itself (``-t``), so a Levain that dies releases the host within
:data:`HOLD_SECONDS` with no cleanup. :func:`sync` is idempotent and is called wherever a thread's state or the
coordinator's lease moves; each call renews a holder that is running low. The holder's pid is kept in ``awake.json``
beside the store so a later process can find, renew or release it, and so ``levain doctor`` can say why.
"""
from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

__all__ = ["HOLD_SECONDS", "describe", "holder_command", "sync"]

HOLD_SECONDS = 1800
_RENEW_BELOW = HOLD_SECONDS // 2
FILE = "awake.json"


def holder_command(seconds: int = HOLD_SECONDS, *, platform: str | None = None) -> list[str] | None:
    """The argv of an idle-sleep holder for this platform, or None when the host has no such tool."""
    platform = platform or sys.platform
    if platform == "darwin":
        exe = shutil.which("caffeinate")
        return [exe, "-i", "-t", str(seconds)] if exe else None
    if platform.startswith("linux"):
        exe = shutil.which("systemd-inhibit")
        return [exe, "--what=idle", "--who=levain", "--why=a work thread is working", "--mode=block",
                "sleep", str(seconds)] if exe else None
    return None


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    # a pid that was reused by something else is not our holder
    cp = subprocess.run(["ps", "-p", str(pid), "-o", "command="], capture_output=True, text=True)
    cmd = cp.stdout.strip()
    return "caffeinate" in cmd or "systemd-inhibit" in cmd or (cmd.startswith("sleep") and "levain" in cmd)


def _read(path: Path) -> dict[str, Any]:
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _release(path: Path, pid: Any) -> None:
    if isinstance(pid, int) and pid > 1 and _alive(pid):
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
    path.unlink(missing_ok=True)


def working(store: Any) -> list[str]:
    """Ids of the threads that hold the host awake: kind work, state working."""
    return [t.thread_id for t in store.threads() if t.kind == "work" and t.state == "working"]


def sync(store: Any) -> dict[str, Any]:
    """Make the assertion match the threads: held while a work thread works, released otherwise. Never raises: a host
    that cannot hold one is reported by :func:`describe`, and a thread's own write is never failed by this."""
    if os.environ.get("LEVAIN_NO_IDLE_ASSERTION") or os.environ.get("PYTEST_CURRENT_TEST"):
        # a test run that moves threads must not leave a real holder on the developer's machine
        return {"held": False, "why": "disabled by the environment (LEVAIN_NO_IDLE_ASSERTION or a test run)"}
    try:
        ids = working(store)
        path = Path(store.directory) / FILE
        cur = _read(path)
        pid = cur.get("pid")
        live = isinstance(pid, int) and _alive(pid)
        if not ids:
            if cur:
                _release(path, pid)
            return {"held": False, "why": "no work thread is working"}
        argv = holder_command()
        if argv is None:
            return {"held": False, "why": "this host has no idle-sleep tool (caffeinate / systemd-inhibit)"}
        if live and cur.get("until", 0) - time.time() > _RENEW_BELOW:
            return {"held": True, "pid": pid, "threads": ids, "why": "a work thread is working"}
        proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                start_new_session=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"pid": proc.pid, "until": time.time() + HOLD_SECONDS, "threads": ids,
                                   "argv": argv[0]}), encoding="utf-8")
        os.replace(tmp, path)
        if live:
            _release_pid_only(pid)
        return {"held": True, "pid": proc.pid, "threads": ids, "why": "a work thread is working"}
    except Exception as exc:      # noqa: BLE001 - see the docstring
        return {"held": False, "why": f"could not sync: {exc}"}


def _release_pid_only(pid: int) -> None:
    if _alive(pid):
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass


def describe(directory: Path | str, *, working_count: int | None = None) -> tuple[bool, str]:
    """(ok, one line) for ``levain doctor``: is the assertion held, and why or why not. Read-only."""
    cur = _read(Path(directory) / FILE)
    pid = cur.get("pid")
    held = isinstance(pid, int) and _alive(pid) and cur.get("until", 0) > time.time()
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

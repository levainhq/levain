"""levain.coordinator.awake: keep the host from idle-sleeping while a WORK thread works or an agent occurrence is live
(C7; gap 6; P6s).

The rule: ONE reconcile, :func:`sync`, reads BOTH sources under the lock beside the coordinator store: the coordinator's
threads (kind ``work``, state ``working``) and the agents' live occurrences (state ``admitted`` or ``launched``, A0
contract 2.5). An assertion is taken when either has work and released only by a sync that finds both empty. Either
side can run the sync (the coordinator store after a committed write; the agent side with ``directory=`` the
coordinator queue directory that holds ``awake.json``). A source that cannot be read is UNKNOWN, never empty: the hold
is still taken when the other source has work, and the holder is never released while a source is unknown.

The holder is held only as long as it lives (:data:`HOLD_SECONDS`) and is renewed only by a sync that lands in the
last half of that life, from either side: there is no timer, so a hold whose stores stay quiet for that stretch (a
beat cadence longer than half the hold, with no ``levain coord watch`` pass writing, no agent tick syncing) loses the
hold until the next sync, while ``levain doctor`` reports the gap honestly. Nothing here closes a stale ``admitted`` /
``launched`` row (the reaper and the tick do, A0 2.5), so a stuck one holds the host until they run; doctor shows each
live occurrence's state and age.

It is an IDLE-sleep assertion only (macOS ``caffeinate -i``, Linux ``systemd-inhibit --what=idle``); Levain
never writes the machine's power settings and never uses sudo. Whether it keeps a closed lid awake on battery is a
claim this module does not make: it does not, because an idle-sleep assertion is by Apple's own header not a hold
against lid-close sleep (see :data:`LID_NOTE`).

The holder is a process that expires by itself (``-t``), so a Levain that dies releases the host within
:data:`HOLD_SECONDS` with no cleanup. It is started through a short-lived intermediate that exits at once, so the
holder belongs to the system's init, never to the Levain process that asked for it: a long-lived Levain process never
carries a finished holder as a zombie. :func:`sync` runs under an exclusive lock beside the store and re-reads both
sources under that lock (nothing the caller saw is trusted). The holder is recorded in ``awake.json`` by pid, start
time and exact argv, and is signalled only when a process matches all three: a pid the system has since given to
something else is never ours to stop.

A host without ``fcntl`` file locks gets no assertion: :func:`sync` says so and :func:`describe` names it.
"""
from __future__ import annotations

try:
    import fcntl
except ImportError:          # a platform without POSIX file locks: no assertion, and no write is failed by it
    fcntl = None  # type: ignore[assignment]
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

from levain.launch import child_env

__all__ = ["HOLD_SECONDS", "describe", "live_occurrences", "live_occurrence_details", "holder_command",
           "process_started", "sync"]

HOLD_SECONDS = 1800
FILE = "awake.json"
LOCK = "awake.lock"
# a test seam: a test run never holds the host unless a test turns this on (and points the holder at a harmless argv)
_IN_TESTS = False
_NO_LOCKS = "this host has no POSIX file locks (fcntl), so Levain does not hold it awake"
# what doctor always says beside the hold; the premise verdict (an idle-sleep assertion does not outlast a closed lid)
LID_NOTE = ("levain holds off idle sleep only; a closed lid can still sleep this Mac (always on battery), "
            "as an idle-sleep assertion does not prevent lid-close sleep")
# the intermediate: starts the holder in its own session, prints its pid, and exits, so the holder is never our child
_LAUNCH = ("import subprocess, sys; p = subprocess.Popen(sys.argv[1:], stdin=subprocess.DEVNULL, "
           "stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True); print(p.pid)")
# how long a just-started holder may take to show up in ps before the sync gives it up
_IDENTIFY_TRIES, _IDENTIFY_GAP = 20, 0.05


def holder_command(seconds: int | None = None, *, platform: str | None = None) -> list[str] | None:
    """The argv of an idle-sleep holder for this platform, or None when the host has no such tool."""
    seconds = HOLD_SECONDS if seconds is None else seconds
    platform = platform or sys.platform
    if platform == "darwin":
        exe = shutil.which("caffeinate")
        return [exe, "-i", "-t", str(seconds)] if exe else None
    if platform.startswith("linux"):
        exe = shutil.which("systemd-inhibit")
        return [exe, "--what=idle", "--who=levain", "--why=a work thread is working or an agent occurrence is live", "--mode=block",
                "sleep", str(seconds)] if exe else None
    return None


def _ps(pid: int, field: str) -> str | None:
    try:
        cp = subprocess.run(["ps", "-ww", "-p", str(pid), "-o", f"{field}="], capture_output=True, text=True,
                            timeout=10, env=child_env())
    except (OSError, subprocess.SubprocessError):
        return None
    return cp.stdout.strip() if cp.returncode == 0 and cp.stdout.strip() else None


def process_started(pid: int) -> str | None:
    """The start time ``ps`` gives a live process, or None when it is gone or cannot be read. A pid and this together
    name one process: a pid the system hands out again starts at another time."""
    return _ps(pid, "lstart")


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
    """Signal the recorded holder, only if it is still the very process we started (it is init's child, not ours, so
    there is nothing to reap)."""
    if not _ours(rec):
        return
    try:
        os.kill(rec["pid"], signal.SIGTERM)
    except OSError:
        return


def _spawn(argv: list[str]) -> int:
    """Start a holder through the intermediate and return its pid; the intermediate is waited for here."""
    cp = subprocess.run([sys.executable, "-I", "-c", _LAUNCH, *argv], stdin=subprocess.DEVNULL, capture_output=True,
                        text=True, timeout=10, env=child_env())
    pid = cp.stdout.strip()
    if cp.returncode != 0 or not pid.isdigit():
        raise OSError(f"the holder did not start ({(cp.stderr or '').strip()[-200:] or 'no pid'})")
    return int(pid)


def _identify(pid: int) -> tuple[str, str] | None:
    """The identity of a holder just started: ps may not list it at once, so it is asked again for a short while."""
    for _ in range(_IDENTIFY_TRIES):
        got = _identity(pid)
        if got is not None:
            return got
        time.sleep(_IDENTIFY_GAP)
    return None


def working(store: Any) -> list[str]:
    """Ids of the threads that hold the host awake: kind work, state working."""
    return [t.thread_id for t in store.threads() if t.kind == "work" and t.state == "working"]


def live_occurrence_details(agent_dir: Path | str | None = None) -> list[dict[str, Any]]:
    """The agents' live occurrences (state ``admitted`` or ``launched``, A0 2.5) in the autonomic store under
    ``agent_dir`` (default: the levain home's), as ``{"id", "state", "since"}`` in occurrence-id order; ``since`` is the
    row's ``launched_at`` for a launched one, else its ``admitted_at``. Writes no row. The open is read-only
    (``mode=ro``), but a read-only open of a WAL store (the real agents store is one) may create its ``-wal`` / ``-shm``
    sidecars, as any reader does; no other file is created. No store, or one with no agent table, is an empty list. A
    store that exists and cannot be read raises, so a caller never takes unreadable for empty: only a confirmed
    ``FileNotFoundError`` is empty, while any other lookup failure (an unsearchable parent, a broken symlink, EACCES) or a
    path that is not a regular file raises. A doctor run as another uid can leave root-owned WAL sidecars that the normal
    user then cannot open."""
    import sqlite3
    import stat
    from urllib.parse import quote

    from levain.autonomic.db import DB_NAME, default_store_dir

    path = (Path(agent_dir) if agent_dir is not None else default_store_dir()) / DB_NAME
    try:
        os.lstat(path)                 # absence is confirmed on the name itself, never on where a symlink leads
    except FileNotFoundError:
        return []
    mode = os.stat(path).st_mode       # a broken symlink, an unsearchable parent, EACCES: all raise
    if not stat.S_ISREG(mode):
        raise OSError(f"{path} exists but is not a regular file")
    conn = sqlite3.connect(f"file:{quote(str(path))}?mode=ro", uri=True, timeout=5.0, isolation_level=None)
    try:
        if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='agent_occurrences'").fetchone() is None:
            return []
        return [{"id": r[0], "state": r[1], "since": (r[3] if r[1] == "launched" and r[3] else r[2])}
                for r in conn.execute("SELECT occurrence_id, state, admitted_at, launched_at FROM agent_occurrences "
                                      "WHERE state IN ('admitted','launched') ORDER BY occurrence_id")]
    finally:
        conn.close()


def live_occurrences(agent_dir: Path | str | None = None) -> list[str]:
    """Ids of the agents' live occurrences: :func:`live_occurrence_details` without the state and age (same reads, same
    sidecar note, same raise on an unreadable store)."""
    return [o["id"] for o in live_occurrence_details(agent_dir)]


def _coordinator_working(store: Any, directory: Path) -> list[str]:
    if store is not None:
        return working(store)
    from levain.coordinator.door import readonly_store

    ro = readonly_store(directory)
    return working(ro) if ro is not None else []


def sync(store: Any = None, *, directory: Path | str | None = None,
         agent_dir: Path | str | None = None) -> dict[str, Any]:
    """The ONE reconcile over both sources: held while a work thread works OR an agent occurrence is live, released
    only when both are empty. Both sides call it (the coordinator store after a write with ``store``; the agent side
    with ``directory``, the coordinator queue directory that holds ``awake.json``); whichever calls, both sources are
    re-read here under the lock and nothing the caller saw is trusted. An unreadable source is UNKNOWN, never empty:
    the hold is still taken when the other source has work, and the holder is never released (nor renewed) while a
    source is unknown and the other has none. ``directory=`` is REQUIRED whenever ``store`` is None: with neither, the
    sync takes no lock, writes nothing and returns ``held`` False with a reason naming the missing directory, because a
    silent fall back to the default queue directory could take a different lock and ``awake.json`` than the
    coordinator. ``agent_dir=`` is a TEST SEAM: production callers use the default agent store (the queue directory and
    the agent store are one pairing in 1.0). Never raises: a host that cannot hold one is reported by
    :func:`describe`."""
    if os.environ.get("LEVAIN_NO_IDLE_ASSERTION") or (os.environ.get("PYTEST_CURRENT_TEST") and not _IN_TESTS):
        # a test run that moves threads must not leave a real holder on the developer's machine
        return {"held": False, "why": "disabled by the environment (LEVAIN_NO_IDLE_ASSERTION or a test run)"}
    if fcntl is None:
        return {"held": False, "why": _NO_LOCKS}
    if store is None and directory is None:
        return {"held": False, "why": "could not sync: no coordinator queue directory was given (pass directory= "
                                      "when there is no store), so no lock or awake.json was touched"}
    try:
        where = Path(store.directory) if store is not None else Path(directory)
        fd = os.open(where / LOCK, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            return _reconcile(store, where, agent_dir)
        finally:
            os.close(fd)        # closing the descriptor releases the lock
    except Exception as exc:      # noqa: BLE001 - see the docstring
        return {"held": False, "why": f"could not sync: {exc}"}


def _reconcile(store: Any, directory: Path, agent_dir: Path | str | None) -> dict[str, Any]:
    path = directory / FILE
    unknown: list[str] = []        # a source that cannot be read is UNKNOWN, never empty
    errors: list[str] = []
    try:                           # both read under the lock: never a set another reconcile moved
        ids = _coordinator_working(store, directory)
    except Exception as exc:       # noqa: BLE001 - an unreadable source is unknown, not a failure of the whole sync
        ids = []
        unknown.append("coordinator")
        errors.append(f"coordinator: {exc}")
    try:
        occ = live_occurrences(agent_dir)
    except Exception as exc:       # noqa: BLE001
        occ = []
        unknown.append("agent")
        errors.append(f"agent: {exc}")
    cur = _read(path)
    if not ids and not occ:
        if unknown:                # nothing known to hold for, and not known to be free: leave the holder as it is
            return {"held": bool(_ours(cur) and cur.get("until", 0) > time.time()), "unknown": unknown,
                    "why": f"could not sync: the {' and '.join(unknown)} source could not be read ({'; '.join(errors)[:300]}), "
                           "so the holder is left as it was"}
        if cur:
            _stop(cur)
            path.unlink(missing_ok=True)
        return {"held": False, "why": "no work thread is working"}
    source = "+".join(n for n, v in (("coordinator", ids), ("agent", occ)) if v)
    why = {"coordinator": "a work thread is working", "agent": "an agent occurrence is live"}.get(
        source, "a work thread is working and an agent occurrence is live")
    if unknown:
        why += f" (the {' and '.join(unknown)} source could not be read)"
    extra = {"unknown": unknown} if unknown else {}
    argv = holder_command()
    if argv is None:
        return {"held": False, "why": "this host has no idle-sleep tool (caffeinate / systemd-inhibit)"}
    if _ours(cur) and cur.get("until", 0) - time.time() > HOLD_SECONDS / 2:
        if cur.get("threads") != ids or cur.get("occurrences") != occ or (cur.get("unknown") or []) != unknown:
            _write(path, {**{k: v for k, v in cur.items() if k != "unknown"}, "threads": ids, "occurrences": occ,
                          "source": source, **extra})      # keep the record naming the source
        return {"held": True, "pid": cur["pid"], "threads": ids, "occurrences": occ, "source": source, "why": why,
                **extra}
    pid = _spawn(argv)
    ident = _identify(pid)
    if ident is None:
        # a holder we cannot name is one we could never stop or recognise again: give it up, keep the old record
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
        return {"held": False, "why": f"could not sync: the new holder (pid {pid}) could not be identified by ps"}
    rec = {"pid": pid, "started": ident[0], "argv": argv, "until": time.time() + HOLD_SECONDS, "threads": ids,
           "occurrences": occ, "source": source, **extra}
    try:
        _write(path, rec)
    except BaseException:
        _stop(rec)              # an unrecorded holder could never be stopped or recognised by a later sync
        raise
    _stop(cur)                  # the old holder goes only once the new one is recorded
    return {"held": True, "pid": pid, "threads": ids, "occurrences": occ, "source": source, "why": why, **extra}


def _age(since: Any, now: float) -> str | None:
    """How long ago an ISO timestamp (``...Z``) was, as ``42s`` / ``7m`` / ``3h12m`` / ``2d4h``; None when unreadable."""
    from datetime import datetime, timezone

    try:
        dt = datetime.fromisoformat(str(since).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        secs = max(0, int(now - dt.timestamp()))
    except (ValueError, OverflowError, OSError):
        return None
    if secs < 60:
        return f"{secs}s"
    if secs < 3600:
        return f"{secs // 60}m"
    if secs < 86400:
        return f"{secs // 3600}h{secs % 3600 // 60:02d}m"
    return f"{secs // 86400}d{secs % 86400 // 3600}h"


def _occ_text(occurrences: list[dict[str, Any]], now: float) -> str:
    """Up to three live occurrences as ``id (state, age)`` plus the oldest one's age, so a stuck row shows."""
    aged = [(o, _age(o.get("since"), now)) for o in occurrences]
    shown = ", ".join(f"{o.get('id')} ({o.get('state')}, {a or 'age unknown'})" for o, a in aged[:3])
    if len(aged) > 3:
        shown += f" +{len(aged) - 3} more"
    known = [(now - _epoch(o.get("since")), a) for o, a in aged if a is not None]
    oldest = max(known)[1] if known else None
    return f"{shown}; oldest live: {oldest}" if oldest else shown


def _epoch(since: Any) -> float:
    from datetime import datetime, timezone

    dt = datetime.fromisoformat(str(since).replace("Z", "+00:00"))
    return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).timestamp()


def describe(directory: Path | str, *, working_count: int | None = None, occurrence_count: int | None = None,
             occurrences: list[dict[str, Any]] | None = None, now: float | None = None) -> tuple[bool, str]:
    """(ok, one line) for ``levain doctor``: is the assertion held, and why or why not. Read-only. ``occurrences`` (the
    rows of :func:`live_occurrence_details`) lets the line name each live occurrence's state and age and the oldest one's,
    so a row stuck in ``admitted`` / ``launched`` is visible; without it only the recorded ids are named."""
    ok, line, holding = _describe(directory, working_count, occurrence_count, occurrences,
                                  time.time() if now is None else now)
    return ok, f"{line}; {LID_NOTE}" if holding and _is_darwin() else line


def _is_darwin() -> bool:
    """A seam: the lid note is a Mac statement, so a test sets the platform without touching the host."""
    return sys.platform == "darwin"


def _describe(directory: Path | str, working_count: int | None, occurrence_count: int | None,
              occurrences: list[dict[str, Any]] | None, now: float) -> tuple[bool, str, bool]:
    """(ok, line, whether a hold is held or about to be): only then does the lid note apply."""
    cur = _read(Path(directory) / FILE)
    pid = cur.get("pid")
    held = _ours(cur) and cur.get("until", 0) > now
    n_occ = occurrence_count if occurrence_count is not None else (len(occurrences) if occurrences is not None else 0)
    if held:
        n = len(cur.get("threads") or [])
        if occurrences is not None:
            live = occurrences                                  # what is live now: state and age
            occ_text = _occ_text(live, now) if live else ""
        else:
            ids = cur.get("occurrences") or []                  # the ids the last sync recorded
            occ_text = ", ".join(map(str, ids[:3])) + (f" +{len(ids) - 3} more" if len(ids) > 3 else "")
        who = (f"{n} work thread(s) working" if n else "") + (" and " if n and occ_text else "")
        if occ_text:
            who += f"agent occurrence(s) live: {occ_text}"
        by = f"pid {pid}, held by {cur.get('source') or 'coordinator'}"
        tail = f"it releases when none is left, and by itself within {HOLD_SECONDS // 60} minutes if Levain stops"
        if not who:                  # alive on a record whose work has since gone (a crash before the closing sync)
            return True, (f"idle sleep is held off ({by}): holder alive with no current work seen (no live work seen "
                          f"now); the next sync releases it, and it ends by itself within {HOLD_SECONDS // 60} minutes"), True
        return True, f"idle sleep is held off ({by}: {who} at its last sync); {tail}", True
    parts = []
    if working_count:
        parts.append(f"{working_count} work thread(s) are working")
    if n_occ:
        parts.append(f"{n_occ} agent occurrence(s) are live" + (f" ({_occ_text(occurrences, now)})" if occurrences else ""))
    if parts:
        what = " and ".join(parts)
        if fcntl is None:
            return False, f"{what} but {_NO_LOCKS}; it may sleep under them", False
        if holder_command() is None:
            return False, (f"{what} but this host has no idle-sleep tool, so it may sleep under them "
                           "(macOS: caffeinate; Linux: systemd-inhibit)"), False
        return False, f"{what} and no assertion is held (it is set on the next sync)", True
    return True, ("no work thread is working and no agent occurrence is live, so nothing holds the host awake; it sleeps "
                  "by your own power settings"
                  + ("" if holder_command() else " (and this host has no idle-sleep tool for when one works)")), False

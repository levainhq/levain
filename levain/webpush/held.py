"""levain.webpush.held — pings the presence rule held back from the phone, and their release.

A ping the rule holds (:func:`levain.webpush.presence.decide`) is recorded as usual and queued in the push store's
``held`` table (:meth:`levain.webpush.store.PushStore.hold`) with the reason it was held: ``quiet``, ``sleep`` or
``active`` (the desk was in use). :func:`flush` releases what the rule now lets through:

- an ``active`` ping goes when the rule says push for it now (the desk has been idle for the window, or an override);
- a ping held by ``quiet`` or ``sleep`` goes when the rule no longer mutes it (the quiet ran out or was cleared, the
  operator typed here after ``sleep``, or a ``loud`` is on), whatever the desk shows, preceded by ONE summary push for the
  batch.

A flush takes a held ping with a lease (:meth:`PushStore.claim_held`) and marks it released only after a push to the phone
succeeded (:meth:`PushStore.release_held`): a failed push, a raised error or a flush that dies leaves it held, and it is
taken again once the lease (:data:`levain.webpush.store.HELD_LEASE_SECS`) ends, so delivery is at-least-once and a
duplicate is bounded by the lease. One flush runs at a time (:func:`_single_flight`).

Release is event-driven; no periodic thread runs it. The gate hook runs :func:`flush_if_due` on every call and a
detached send child runs :func:`flush` after each push, so the next notify or tool call delivers what is due. A ping
whose hold ends with no event (a quiet that runs out, the desk going idle) waits for the next gate-hook call or push:
with no agent calling, it stays held. The periodic ticker that once covered that gap (:func:`tick`, hosted by the
phone port's server) is retired with that server; nothing calls :func:`tick`.

Stdlib only.
"""
from __future__ import annotations

import fcntl
import os
import site
import sqlite3
import stat
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path

from levain.webpush import presence

__all__ = ["TICK_SECS", "count", "due", "flush", "flush_if_due", "plan", "tick"]

SUMMARY_URL = "/?view=needs"
TICK_SECS = 30.0
SPAWN_GAP_SECS = 30.0   # :func:`flush_if_due` starts a flush child at most this often
LOCK_NAME = "flush.lock"
STAMP_NAME = "flush.spawned"
SPAWN_LOCK_NAME = "flush.spawn.lock"


def _db(queue_dir: Path | str) -> Path:
    from levain.webpush.store import DB_NAME, DIR_NAME

    return Path(queue_dir) / DIR_NAME / DB_NAME


def count(queue_dir: Path | str | None) -> int:
    """How many pings are held now. Read-only: it never creates the push store; any fault reads as 0."""
    if queue_dir is None:
        return 0
    path = _db(queue_dir)
    try:
        if not stat.S_ISREG(os.lstat(path).st_mode):
            return 0
        conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=0.25)
        try:
            return int(conn.execute("SELECT COUNT(*) FROM held WHERE released_at IS NULL").fetchone()[0])
        finally:
            conn.close()
    except (OSError, sqlite3.Error, ValueError):
        return 0


def plan(items: list[dict], word: dict | None, idle: float | None, now: float) -> tuple[list[dict], list[dict]]:
    """``(active, muted)``: which held ``items`` the rule releases at ``now``, as pings held for an active desk and
    pings held by a mute. Pure."""
    active: list[dict] = []
    muted: list[dict] = []
    for item in items:
        d = presence.decide(now, word, idle, urgent=item["kind"] in presence.URGENT_KINDS)
        if item["reason"] == "active":
            if d.push:
                active.append(item)
        elif d.state != "muted":
            muted.append(item)
    return active, muted


def _idle_source(idle: Callable[[], float | None] | None) -> Callable[[], float | None]:
    if idle is not None:
        return idle
    from levain.consent.auto_away import input_idle_seconds

    return input_idle_seconds


def _plan_now(queue_dir: Path | str, at: float, idle: Callable[[], float | None] | None):
    from levain.webpush.store import PushStore

    items = PushStore(queue_dir).held_items(now=at)
    if not items:
        return [], []
    return plan(items, presence.read_word(queue_dir), _idle_source(idle)(), at)


def _inside_spawn_gap(queue_dir: Path | str) -> bool:
    """True when a flush child was started within :data:`SPAWN_GAP_SECS` (the stamp's age). Any fault reads as False."""
    try:
        return time.time() - os.lstat(Path(queue_dir) / "webpush" / STAMP_NAME).st_mtime < SPAWN_GAP_SECS
    except OSError:
        return False


def due(queue_dir: Path | str | None, *, now: float | None = None,
        idle: Callable[[], float | None] | None = None, throttled: bool = False) -> bool:
    """True when :func:`flush` would release something now (a ping under a live claim is not due). Cheap in order: the
    held count, then (``throttled``) the spawn gap, then whether a phone is live, and only then the desk's idle reading;
    any fault reads as False. With no live subscription nothing is due, so no flush child is started for it."""
    if not count(queue_dir):
        return False
    if throttled and _inside_spawn_gap(queue_dir):
        return False
    try:
        from levain.webpush import live

        if not live(queue_dir):
            return False
        active, muted = _plan_now(queue_dir, time.time() if now is None else now, idle)
        return bool(active or muted)
    except Exception:  # noqa: BLE001 - checked again at the next call
        return False


def _pushed(results: list[tuple[int, int]]) -> bool:
    """A held ping is done when the push reached the phones: there was at least one subscription, one answered 2xx, and
    every other one answered 2xx too or is retired (:func:`levain.webpush.sender.retires`). A device that failed, gave no
    answer, or was refused for a reason that does not retire it keeps the ping held for the retry."""
    from levain.webpush import sender

    return (bool(results) and any(200 <= status < 300 for _, status in results)
            and all(200 <= status < 300 or sender.retires(status) for _, status in results))


def _single_flight(queue_dir: Path | str) -> int | None:
    """An exclusive lock on ``flush.lock`` for the flush that holds it: its file descriptor, ``-1`` when the lock cannot
    be had for a reason other than another flush holding it (the lease still keeps two flushes from pushing one ping
    twice), ``None`` when another flush is running."""
    try:
        fd = os.open(Path(queue_dir) / "webpush" / LOCK_NAME, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    except OSError:
        return -1
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        return None
    except OSError:
        os.close(fd)
        return -1
    return fd


def flush(queue_dir: Path | str, *, now: float | None = None,
          idle: Callable[[], float | None] | None = None) -> int:
    """Push what the rule now lets through; returns how many held pings reached the phone (the summary not counted). A
    summary push goes first when pings held by a mute are released. Never raises, holds everything while no phone is
    paired, and stands down while another flush runs. A ping is released only after its push succeeded; any other
    outcome leaves it held for a retry after its lease."""
    if not count(queue_dir):
        return 0
    fd = _single_flight(queue_dir)
    if fd is None:
        return 0
    try:
        return _flush(queue_dir, time.time() if now is None else now, idle)
    except Exception:  # noqa: BLE001 - what stays held is checked again at the next call
        return 0
    finally:
        if fd >= 0:
            os.close(fd)


def _flush(queue_dir: Path | str, at: float, idle: Callable[[], float | None] | None) -> int:
    from levain.webpush import live, sender
    from levain.webpush.store import PushStore

    if not live(queue_dir):
        return 0
    active, muted = _plan_now(queue_dir, at, idle)
    if not (active or muted):
        return 0
    store = PushStore(queue_dir)
    claimed = set(store.claim_held([i["ping_id"] for i in active + muted], now=at))
    muted = [i for i in muted if i["ping_id"] in claimed]
    active = [i for i in active if i["ping_id"] in claimed]
    qdir = Path(queue_dir)
    if muted:
        by = {i["reason"] for i in muted}
        what = "quiet" if by == {"quiet"} else "asleep" if by == {"sleep"} else "muted"
        n = len(muted)
        try:
            sender.send(qdir, kind="summary", ref=None, title="levain", url=SUMMARY_URL,
                        body=f"{n} ping{'' if n == 1 else 's'} held while you were {what}; they follow.")
        except Exception:  # noqa: BLE001 - the pings themselves still go
            pass
    pushed = 0
    for item in muted + active:
        try:
            results = sender.deliver(qdir, store, item["ping_id"], kind=item["kind"], ref=item["ref"],
                                     title=item["title"], body=item["body"], url=item["url"])
            if _pushed(results):
                store.release_held(item["ping_id"])
                pushed += 1
        except Exception:  # noqa: BLE001 - this ping stays held; the rest still go
            continue
    return pushed


def _spawn_child(queue_dir: Path | str) -> bool:
    from levain.launch import child_env

    env = child_env()
    root = str(Path(__file__).resolve().parents[2])   # a source checkout: -P leaves the child no other way to import levain
    getsite = getattr(site, "getsitepackages", None)   # absent in some older virtualenv builds
    installed = [d for d in (getsite() if getsite else []) + [site.getusersitepackages()] if isinstance(d, str)]
    if not any(root == str(Path(d).resolve()) for d in installed):
        env["PYTHONPATH"] = os.pathsep.join(p for p in (root, env.get("PYTHONPATH")) if p)
    subprocess.Popen([sys.executable, "-P", "-m", "levain.webpush", "flush", "--queue-dir",
                      str(Path(queue_dir).resolve())], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, start_new_session=True, close_fds=True, env=env, cwd=os.sep)
    return True


def _spawn_throttled(queue_dir: Path | str) -> bool:
    """Start a flush child unless one was started within :data:`SPAWN_GAP_SECS`. The stamp check, the spawn and the stamp
    run under a non-blocking ``flock`` on ``flush.spawn.lock``: a process that finds the lock held reads as not due
    (another is spawning now), and the stamp is written only after the child started, so a failed spawn is retried at the
    next call. A lock or stamp that cannot be had for another reason spawns without the throttle."""
    base = Path(queue_dir) / "webpush"
    try:
        fd = os.open(base / SPAWN_LOCK_NAME, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    except OSError:
        return _spawn_child(queue_dir)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        except OSError:
            return _spawn_child(queue_dir)
        stamp = base / STAMP_NAME
        try:
            if time.time() - os.lstat(stamp).st_mtime < SPAWN_GAP_SECS:
                return False
        except FileNotFoundError:
            pass
        except OSError:
            return _spawn_child(queue_dir)
        _spawn_child(queue_dir)
        try:
            os.close(os.open(stamp, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600))
            os.utime(stamp)
        except OSError:
            pass
        return True
    finally:
        os.close(fd)


def flush_if_due(queue_dir: Path | str | None, *, throttle: bool = True) -> bool:
    """Start a detached ``python -m levain.webpush flush`` when something held is due (:func:`due`); True when it
    started. The gate hook calls this on every call, so it costs one read-only count when nothing is held, and a due
    check that finds something starts a child at most every :data:`SPAWN_GAP_SECS` (:func:`_spawn_throttled`), so a burst
    of hooks does not spawn a burst of processes (``throttle=False``, for a word the operator just said, skips that gap:
    one running flush still stands any second one down)."""
    if queue_dir is None or not due(queue_dir, throttled=throttle):
        return False
    try:
        return _spawn_child(queue_dir) if not throttle else _spawn_throttled(queue_dir)
    except Exception:  # noqa: BLE001 - a failed flush never fails the gate
        return False


def tick(queue_dir: Path | str, stop, *, every: float = TICK_SECS) -> None:
    """Run :func:`flush` every ``every`` seconds until the ``threading.Event`` ``stop`` is set. Retired: its one host
    (the phone port's server) is gone and nothing calls it; release is event-driven (the module docstring)."""
    while not stop.wait(every):
        flush(queue_dir)

"""levain.webpush.held — pings the presence rule held back from the phone, and their release.

A ping the rule holds (:func:`levain.webpush.intent.decide`) is recorded as usual and queued in the push store's
``held`` table (:meth:`levain.webpush.store.PushStore.hold`) with the reason it was held: a preset that muted it (``quiet``,
``sleep``) or ``active`` (his devices were off, e.g. ``follow`` at the laptop). :func:`flush` releases what the rule now lets
through, and only that:

- an ``active`` ping goes when the rule says push for it now (``follow`` turned devices on, or a setting or preset did);
- a ping held by a mute goes when the rule says push for it (the mute ended or widened AND his devices are on), preceded by
  ONE summary push for the batch;
- an approval-class ping goes whenever a signed consent away mark stands, whatever the record says (the mark-aware
  decider a new ping meets, :func:`levain.consent.auto_away.marked_push`), and otherwise as soon as no mute holds it (it is
  about a request filed and held for a signed answer: :func:`levain.webpush.intent.decide`'s ``filed``).

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


NOTICES_SUMMARY_URL = "/?view=notices"
_NOTICE_KINDS = ("notify", "notify_urgent")
_SUMMARY_SOURCES = 4     # the sources a notice summary names before "and N more"


def _is_notice(item: dict) -> bool:
    return item.get("kind") in _NOTICE_KINDS


def notice_summary(items: list[dict]) -> str:
    """The one line that goes before released notices: how many, and from which sources (the ref's source, in the order
    each first appeared), e.g. ``2 notices held: nightly-backup, site-monitor``; an empty string when there are none."""
    if not items:
        return ""
    names: list[str] = []
    for i in items:
        ref = i.get("ref") or ""
        name = ref[len("notify:"):] if ref.startswith("notify:") else "unknown"
        if name not in names:
            names.append(name)
    shown = ", ".join(names[:_SUMMARY_SOURCES]) + (f" and {len(names) - _SUMMARY_SOURCES} more"
                                                   if len(names) > _SUMMARY_SOURCES else "")
    n = len(items)
    return f"{n} notice{'' if n == 1 else 's'} held: {shown}"


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


def plan(items: list[dict], rec: dict, reading: float | None, now: float, remote: dict | None = None,
         desk_msg_at: float | None = None, mark: str | None = None) -> tuple[list[dict], list[dict]]:
    """``(active, muted)``: which held ``items`` the record releases at ``now``, as pings held while his devices were off
    (reason ``active``) and pings held by a mute (a preset's ``lets_through``). ``rec`` is :func:`levain.webpush.intent.read`;
    ``reading`` the laptop idle seconds with a desk message counted as input. A held ping is released exactly when the rule
    now says push for its kind: lifting a mute does not push a ping while his devices are still off. ``mark`` (why a consent
    away mark stands, :func:`levain.consent.auto_away.any_mark_stands`) releases an approval-class ping whatever the record
    says, by the same decider a new one meets (:func:`levain.consent.auto_away.marked_push`). A held approval-class ping is
    one :func:`levain.consent.adapters.claude_code.notify` sent (a request filed and held, a limit hold, a ruling-delivery
    notice: each waits on him, and only that function holds one), so it is decided with
    ``filed`` as a new one is (:func:`levain.consent.auto_away.approval_push`): released once no mute holds it. Pure."""
    from levain.consent.auto_away import marked_push
    from levain.webpush import intent

    active: list[dict] = []
    muted: list[dict] = []
    for item in items:
        d = marked_push(mark, item["kind"]) or intent.decide(
            now, rec, reading, class_name=presence.kind_class(item["kind"]), remote=remote, desk_msg_at=desk_msg_at,
            filed=True)
        if d.push:
            (active if item["reason"] == "active" else muted).append(item)
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
    from levain.webpush import intent

    # one sample, exactly as the rule a delivery reads (intent._core): a desk message is laptop input too
    s = intent._sample(queue_dir, at, _idle_source(idle))
    mark = None
    if any(presence.kind_class(i["kind"]) == "approval" for i in items):   # the consent store is read only when it can decide
        from levain.consent.auto_away import any_mark_stands

        mark = any_mark_stands(queue_dir)
    return plan(items, s["rec"], s["reading"], at, s["remote"], s["msg_at"], mark)


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
    # a held notice is summarised on its own, naming its sources, and opens the notices view: it is never counted among
    # the pings that wait on him, and the needs view is never where a notice leads
    seat_muted = [i for i in muted if not _is_notice(i)]
    notice_muted = [i for i in muted if _is_notice(i)]
    if seat_muted:
        by = {i["reason"] for i in seat_muted}
        what = "quiet" if by == {"quiet"} else "asleep" if by == {"sleep"} else "muted"
        n = len(seat_muted)
        try:
            sender.send(qdir, kind="summary", ref=None, title="levain", url=SUMMARY_URL,
                        body=f"{n} ping{'' if n == 1 else 's'} held while you were {what}; they follow.")
        except Exception:  # noqa: BLE001 - the pings themselves still go
            pass
    summary = notice_summary(notice_muted)
    if summary:
        try:
            sender.send(qdir, kind="summary", ref=None, title="levain", url=NOTICES_SUMMARY_URL, body=summary)
        except Exception:  # noqa: BLE001 - the notices themselves still go
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

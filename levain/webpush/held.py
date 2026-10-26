"""levain.webpush.held — pings the presence rule held back from the phone, and their release.

A ping the rule holds (:func:`levain.webpush.presence.decide`) is recorded as usual and queued in the push store's
``held`` table (:meth:`levain.webpush.store.PushStore.hold`) with the reason it was held: ``quiet``, ``sleep`` or
``active`` (the desk was in use). :func:`flush` releases what the rule now lets through:

- an ``active`` ping goes when the rule says push for it now (the desk has been idle for the window, or an override);
- a ping held by ``quiet`` or ``sleep`` goes when the rule no longer mutes it (the quiet ran out or was cleared, the
  operator typed here after ``sleep``, or a ``loud`` is on), whatever the desk shows, preceded by ONE summary push for the
  batch.

Each held ping is released by exactly one caller (:meth:`PushStore.claim_held`), so it is pushed at most once however many
flushes race; a flush that dies after claiming loses what it claimed, which is the price of never pushing twice.

Nothing here needs a daemon: the gate hook runs :func:`flush_if_due` on every call and a detached send child runs
:func:`flush` after each push, so the next notify or tool call delivers what is due; the phone server's tick
(:func:`levain.phone_server.run_phone_server`, through :func:`tick`) only makes that prompt when no agent is calling.

Stdlib only.
"""
from __future__ import annotations

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


def _plan_now(queue_dir: Path | str, now: float | None, idle: Callable[[], float | None] | None):
    from levain.webpush.store import PushStore

    at = time.time() if now is None else now
    items = PushStore(queue_dir).held_items()
    if not items:
        return [], [], items
    active, muted = plan(items, presence.read_word(queue_dir), _idle_source(idle)(), at)
    return active, muted, items


def due(queue_dir: Path | str | None, *, now: float | None = None,
        idle: Callable[[], float | None] | None = None) -> bool:
    """True when :func:`flush` would release something now. Cheap when nothing is held; any fault reads as False."""
    if not count(queue_dir):
        return False
    try:
        active, muted, _ = _plan_now(queue_dir, now, idle)
        return bool(active or muted)
    except Exception:  # noqa: BLE001 - checked again at the next call
        return False


def flush(queue_dir: Path | str, *, now: float | None = None,
          idle: Callable[[], float | None] | None = None) -> int:
    """Push what the rule now lets through; returns how many held pings were pushed (the summary not counted). A
    summary push goes first when pings held by a mute are released. Never raises, and holds everything while no phone is
    paired. Each ping is released by one caller only."""
    if not count(queue_dir):
        return 0
    try:
        from levain.webpush import live, sender
        from levain.webpush.store import PushStore

        if not live(queue_dir):
            return 0
        active, muted, _ = _plan_now(queue_dir, now, idle)
        if not (active or muted):
            return 0
        store = PushStore(queue_dir)
        claimed = set(store.claim_held([i["ping_id"] for i in active + muted]))
        muted = [i for i in muted if i["ping_id"] in claimed]
        active = [i for i in active if i["ping_id"] in claimed]
        qdir = Path(queue_dir)
        if muted:
            by = {i["reason"] for i in muted}
            what = "quiet" if by == {"quiet"} else "asleep" if by == {"sleep"} else "muted"
            n = len(muted)
            sender.send(qdir, kind="summary", ref=None, title="levain", url=SUMMARY_URL,
                        body=f"{n} ping{'' if n == 1 else 's'} held while you were {what}; they follow.")
        pushed = 0
        for item in muted + active:
            sender.deliver(qdir, store, item["ping_id"], kind=item["kind"], ref=item["ref"], title=item["title"],
                           body=item["body"], url=item["url"])
            pushed += 1
        return pushed
    except Exception:  # noqa: BLE001 - what stays held is checked again at the next call
        return 0


def flush_if_due(queue_dir: Path | str | None) -> bool:
    """Start a detached ``python -m levain.webpush flush`` when something held is due (:func:`due`); True when it
    started. The gate hook calls this on every call, so it costs one read-only count when nothing is held."""
    if queue_dir is None or not due(queue_dir):
        return False
    from levain.launch import child_env

    env = child_env()
    root = str(Path(__file__).resolve().parents[2])   # a source checkout: -P leaves the child no other way to import levain
    if not any(root == str(Path(d).resolve()) for d in site.getsitepackages() + [site.getusersitepackages()]):
        env["PYTHONPATH"] = os.pathsep.join(p for p in (root, env.get("PYTHONPATH")) if p)
    try:
        subprocess.Popen([sys.executable, "-P", "-m", "levain.webpush", "flush", "--queue-dir",
                          str(Path(queue_dir).resolve())], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True, close_fds=True, env=env, cwd=os.sep)
    except Exception:  # noqa: BLE001 - a failed flush never fails the gate
        return False
    return True


def tick(queue_dir: Path | str, stop, *, every: float = TICK_SECS) -> None:
    """Run :func:`flush` every ``every`` seconds until the ``threading.Event`` ``stop`` is set: the long-lived phone
    server's own thread, so a held ping is delivered when the desk goes idle even if no agent calls the gate."""
    while not stop.wait(every):
        flush(queue_dir)

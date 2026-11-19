"""levain.cockpit.last_seen: the paired-device ``last_seen`` stamp, taken off the request thread (levain#157 D15b).

A request that a paired token authenticated calls :meth:`LastSeenStamper.note`, which decides from memory alone (no
sqlite open, no lock but its own) whether that device is due, and if so hands ``(device, now)`` to ONE background daemon
thread. That thread coalesces what is waiting (one entry per device) and calls ``CockpitDB.stamp_last_seen`` for each.

Nothing here reaches an answer: a request never waits on the thread, and a stamp that fails is dropped (the device is
tried again after the next window). ``last_seen`` is a display field.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Any

from levain.cockpit import firejournal

__all__ = ["LastSeenStamper"]

_log = logging.getLogger("levain.cockpit.last_seen")

_MAX_DEVICES = 1024     # devices tracked at once, and queued at once; a note past it is skipped, fail-soft
_JOIN_S = 2.0           # how long close() waits for the thread; the thread is a daemon, so a longer stamp never holds exit


class LastSeenStamper:
    """The per-device throttle (in memory, seeded by a device's first note in this process) and the one writer thread.

    The window between two notes that reach the thread for one device is ``firejournal.LAST_SEEN_EVERY``, read at call
    time. The first note for a device since this process started always goes through."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._next_ok: "dict[str, float]" = {}              # device -> monotonic time its next note may go through
        self._pending: "dict[str, tuple[Any, Any]]" = {}    # device -> (journal, request time), coalesced
        self._thread: "threading.Thread | None" = None
        self._stopped = False

    def note(self, db: Any, device_id: str, now: Any) -> bool:
        """Called on the request thread. True iff this call queued a stamp for the thread. Takes one short lock and
        opens nothing."""
        mono = time.monotonic()
        with self._lock:
            if self._stopped:
                return False
            if mono < self._next_ok.get(device_id, 0.0):
                return False
            if len(self._pending) >= _MAX_DEVICES and device_id not in self._pending:
                return False                            # the thread is behind: the queue holds _MAX_DEVICES at most
            if len(self._next_ok) >= _MAX_DEVICES and device_id not in self._next_ok:
                self._next_ok = {d: t for d, t in self._next_ok.items() if t > mono}
                if len(self._next_ok) >= _MAX_DEVICES:
                    return False
            self._next_ok[device_id] = mono + firejournal.LAST_SEEN_EVERY.total_seconds()
            self._pending[device_id] = (db, now)
            if self._thread is None:
                try:
                    thread = threading.Thread(target=self._run, name="levain-last-seen", daemon=True)
                    thread.start()
                except Exception:  # noqa: BLE001 - a thread that cannot start loses a display stamp, nothing else
                    self._pending.pop(device_id, None)
                    self._next_ok.pop(device_id, None)
                    return False
                self._thread = thread
            self._wake.set()
        return True

    def _run(self) -> None:
        while True:
            self._wake.wait()
            if not self._stamp_pending():
                return

    def _stamp_pending(self) -> bool:
        """Take what is waiting and stamp it. False once stopped. Its own frame holds the batch, so no journal it
        stamped through is kept alive while the thread waits for the next note."""
        with self._lock:
            if self._stopped:
                return False
            batch, self._pending = self._pending, {}
            self._wake.clear()
        for device_id, (db, now) in batch.items():
            if self._stopped:
                return False
            try:
                db.stamp_last_seen(device_id, now=now)
            except Exception:  # noqa: BLE001 - a display stamp never fails anything; the device is retried next window
                _log.debug("paired device last_seen stamp skipped", exc_info=True)
        return True

    def close(self) -> None:
        """Stop the thread: no further note queues, and the thread ends after the stamp it is on (waited for at most
        ``_JOIN_S``). Safe to call twice."""
        with self._lock:
            self._stopped = True
            self._pending.clear()
            thread = self._thread
            self._wake.set()
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=_JOIN_S)

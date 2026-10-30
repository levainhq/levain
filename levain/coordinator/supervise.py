"""levain.coordinator.supervise — the coordinator watcher inside ``levain serve`` (C10 S1).

The watcher (:func:`levain.coordinator.launch.watch_once`) is what starts a pending coordinator launch nobody started,
ends a launch whose session is gone, beats the live holder's lease and stops the session of a launch that is over.
:class:`Watcher` runs it on a daemon thread every :data:`DEFAULT_EVERY` seconds for as long as the serve is up, so
"the cockpit is up" means "the coordinator is supervised", with no terminal watch to remember.

- One pass at a time: every pass takes the store's single-flight lock inside ``watch_once``, the lock a hand-run
  ``levain coord watch`` takes too, so the serve's watcher and yours never stop or end the same thing twice.
- A pass that faults, whatever it raised, is one line in the serve log and the next pass runs on time.
- :meth:`Watcher.stop` ends the thread: a wait in progress ends at once and no pass starts after it. A pass already
  running is waited for up to a bound; the thread is a daemon, so a pass stuck past it never holds the process open.
- No coordinator store yet: nothing is watched and nothing is made (a drop or ``levain coord agent`` makes the
  store, and the next pass sees it).
- Lines go to the serve's stderr, its log under a login unit. A pass whose lines repeat the previous pass's is not
  written again, so a standing refusal ("claude is not on PATH") is said when it starts and when it changes, and the
  log does not grow by the same line every pass.

Stdlib only; ``levain.coordinator.launch`` is imported on the thread, so a platform without it faults a pass (a line in
the log) and never the serve's start.
"""
from __future__ import annotations

import sys
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

__all__ = ["DEFAULT_EVERY", "Watcher", "default_store"]

DEFAULT_EVERY = 30.0          # seconds between passes: the hand-run watch's own default
_STOP_WAIT = 30.0             # seconds stop() waits for a pass in flight before leaving it to the daemon thread
_THREAD_NAME = "levain-coord-watcher"


def default_store(directory: Any = None) -> Any:
    """The coordinator store in ``directory`` (None: the default a hand-run ``levain coord watch`` watches), or None
    while the coordinator's tables are not in its file. ``dispatch.db`` is the job store's file too, so the file proves
    nothing: the store's own read-only opener is the probe, and it creates nothing."""
    from levain.coordinator.envelopes import EnvelopeStore
    from levain.coordinator.store import StoreAbsent

    store = EnvelopeStore(None if directory is None else Path(directory))
    try:
        store.open_readonly().close()
    except StoreAbsent:
        return None
    return store


def _to_log(line: str) -> None:
    print(f"levain coord watch: {line}", file=sys.stderr, flush=True)


class Watcher:
    """``watch_once`` on a thread, every ``every`` seconds, over the store ``opener`` returns (None: no pass)."""

    def __init__(self, opener: Callable[[], Any] | None = None, *, every: float = DEFAULT_EVERY,
                 emit: Callable[[str], None] = _to_log) -> None:
        if every <= 0:
            raise ValueError("every is a positive number of seconds")
        self._opener = opener
        self.every = every
        self._emit = emit
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last: list[str] | None = None
        self._migrated = False
        self._passes = 0
        self._counted = threading.Condition()

    @property
    def passes(self) -> int:
        """How many passes have finished (faulted ones included)."""
        with self._counted:
            return self._passes

    def wait_passes(self, n: int, timeout: float) -> bool:
        """Block until ``n`` passes have finished, or ``timeout`` seconds; whether they had."""
        with self._counted:
            return self._counted.wait_for(lambda: self._passes >= n, timeout)

    def start(self) -> "Watcher":
        if self._thread is not None:
            raise RuntimeError("this watcher was started already")
        self._thread = threading.Thread(target=self._run, name=_THREAD_NAME, daemon=True)
        self._thread.start()
        return self

    def stop(self, timeout: float = _STOP_WAIT) -> bool:
        """End the thread; whether it ended within ``timeout``. At most one pass can begin after the event is set (the
        loop checked it just before); that pass is the pass in flight, and is waited for up to ``timeout``."""
        self._stop.set()
        t = self._thread
        if t is None or t is threading.current_thread():
            return True
        t.join(timeout)
        return not t.is_alive()

    def _run(self) -> None:
        # the first pass runs at once: a serve restarted mid-run picks up the coordinator it left without a wait
        while not self._stop.is_set():
            self.run_pass()
            if self._stop.wait(self.every):
                break

    def run_pass(self) -> list[str] | None:
        """One pass, its lines written to the log (unless they repeat the previous pass's); None when it faulted."""
        try:
            from levain.coordinator import launch

            store = (self._opener or default_store)()
            lines: list[str] = []
            if store is not None:
                lines += self._migrate_once(launch, store)
                lines += launch.watch_once(store)
        except Exception as exc:  # noqa: BLE001 - one bad pass never ends the watcher (the serve's exit still does)
            fault = f"watch pass faulted ({type(exc).__name__}: {str(exc)[:300]}); next pass in {self.every:g}s"
            if self._last != [fault]:
                self._safe_emit(fault)
            self._last = [fault]
            lines = None
        else:
            if lines and lines != self._last:
                for line in lines:
                    self._safe_emit(line)
            self._last = lines
        finally:
            with self._counted:
                self._passes += 1
                self._counted.notify_all()
        return lines

    def _migrate_once(self, launch: Any, store: Any) -> list[str]:
        """What a hand-run ``levain coord watch`` does before its first pass: check the coordinator setting's workspace,
        once per watcher and not before a store exists. A fault in it is a line, the passes still run, and the check is tried
        again on the next pass."""
        if self._migrated:
            return []
        try:
            out = list(launch.migrate_setting(store))
            self._migrated = True
            return out
        except Exception as exc:  # noqa: BLE001 - the passes still run; the launch re-checks the record anyway
            return [f"the coordinator setting's workspace could not be checked ({type(exc).__name__}: {str(exc)[:300]})"]

    def _safe_emit(self, line: str) -> None:
        try:
            self._emit(line)
        except Exception:  # noqa: BLE001 - a log that cannot be written never ends the watcher
            pass

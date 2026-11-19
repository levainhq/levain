"""A stall watchdog for the cockpit process: it names who held the interpreter when every thread was late at once.

One daemon thread sleeps :data:`INTERVAL_S` and measures how late each wake is against a monotonic clock. Every thread in
the process waits on the same interpreter, so a wake later than :data:`LATE_S` means the whole process stalled (a GIL
convoy, a long collection, a blocked call that holds the interpreter). Such a wake prints one stderr line and writes
the stack of every thread to a file in the dump directory, so the next stall carries the name of the code that held it.

The loop does nothing but sleep, compare and, on a late wake, report. Below the threshold it walks no stack.

The dump takes no lock a worker thread holds across slow work: ``sys._current_frames`` is read under the interpreter
alone, and the thread names come from ``threading.enumerate`` (a lock held only across a dictionary copy, never across
work). The dump is plain file writes; it does not use ``logging``, whose handler locks a stalled thread could hold.
A failed write (a full disk, a missing directory) is one stderr line and never raises.
"""
from __future__ import annotations

import sys
import threading
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

INTERVAL_S = 0.1      # how long each sleep asks for; nothing in the loop runs faster than this
LATE_S = 1.0          # a wake later than this (past the INTERVAL_S it asked for) is a stall
MIN_DUMP_GAP_S = 30.0  # at most one dump file per this span; a late wake inside it still prints its stderr line
KEEP_DUMPS = 20       # the newest this-many dump files stay; older ones are deleted after each new dump
JOIN_S = 2.0          # how long stop() waits for the thread before it gives up and returns

DUMP_PREFIX = "stall-"
DUMP_SUFFIX = ".txt"


class StallWatch:
    """Watch for whole-process stalls. ``start()`` and ``stop()`` are idempotent; ``dumps()`` lists the dump files.

    ``clock`` is the monotonic clock the lateness and the dump gap are read from. ``sleep`` takes a number of seconds;
    left None it is the stop event's wait, so ``stop()`` ends the thread at once instead of after the current sleep.
    A test passes both to drive the loop with no real time (``run`` is the loop, callable on the calling thread).
    """

    def __init__(self, dump_dir: Path | str, *, clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], object] | None = None) -> None:
        self._dir = Path(dump_dir)
        self._clock = clock
        self._stop = threading.Event()
        self._sleep = sleep if sleep is not None else self._stop.wait
        self._thread: threading.Thread | None = None
        self._state = threading.Lock()       # guards only start/stop's view of the thread; the loop never takes it
        self._last_dump: float | None = None  # clock reading of the last dump attempt; None before the first
        self._seq = 0                         # tiebreak so two dumps in one timestamp tick keep distinct, ordered names

    # ---- lifecycle -------------------------------------------------------------------------------------------

    def start(self) -> None:
        """Start the daemon thread; a second call while it runs does nothing."""
        with self._state:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(target=self.run, name="cockpit-stall-watch", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        """Ask the thread to end and wait for it, at most :data:`JOIN_S`. Safe to call twice, or before ``start``."""
        with self._state:
            thread, self._thread = self._thread, None
        self._stop.set()
        if thread is not None and thread is not threading.current_thread():
            thread.join(JOIN_S)

    def dumps(self) -> list[Path]:
        """The dump files in the directory, oldest first. An unreadable or missing directory lists none."""
        try:
            found = [p for p in self._dir.iterdir() if p.name.startswith(DUMP_PREFIX) and p.name.endswith(DUMP_SUFFIX)]
        except OSError:
            return []
        return sorted(found, key=lambda p: p.name)

    # ---- the loop --------------------------------------------------------------------------------------------

    def run(self) -> None:
        """Sleep, measure the lateness of each wake, report a late one; return when stopped. Nothing here raises."""
        while not self._stop.is_set():
            try:
                due = self._clock() + INTERVAL_S
                self._sleep(INTERVAL_S)
                if self._stop.is_set():
                    return
                now = self._clock()
                late = now - due
                if late > LATE_S:
                    self._report(late, now)
            except Exception as exc:     # the watchdog must outlive anything it meets
                self._say(f"cockpit stall: watch error: {type(exc).__name__}: {exc}")

    def _report(self, late: float, now: float) -> None:
        self._say(f"cockpit stall: late {late:.2f} s")
        if self._last_dump is not None and now - self._last_dump < MIN_DUMP_GAP_S:
            return
        self._last_dump = now        # set before the write: a broken disk is tried once per gap, not once per wake
        try:
            self._write_dump(late)
        except Exception as exc:
            self._say(f"cockpit stall: dump not written: {type(exc).__name__}: {exc}")

    # ---- the dump --------------------------------------------------------------------------------------------

    def _write_dump(self, late: float) -> None:
        text = _stacks_text(late)
        self._seq += 1
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        path = self._dir / f"{DUMP_PREFIX}{stamp}-{self._seq:06d}{DUMP_SUFFIX}"
        with open(path, "x", encoding="utf-8") as handle:       # "x": never overwrite an earlier dump
            handle.write(text)
        for old in self.dumps()[:-KEEP_DUMPS]:
            try:
                old.unlink()
            except OSError:
                pass                 # one file that will not go does not stop the rest, or the next dump

    @staticmethod
    def _say(line: str) -> None:
        try:
            print(line, file=sys.stderr, flush=True)
        except (OSError, ValueError):
            pass                     # a closed or broken stderr is not worth the thread


def _stacks_text(late: float) -> str:
    """The stack of every thread as text, each under its thread's name."""
    names = {t.ident: t.name for t in threading.enumerate()}
    frames = sys._current_frames()
    parts = [f"cockpit stall: late {late:.2f} s, {len(frames)} threads\n"]
    try:
        for ident, frame in sorted(frames.items(), key=lambda item: str(names.get(item[0], ""))):
            parts.append(f"\n--- thread {names.get(ident, 'unknown')} (ident {ident}) ---\n")
            parts.append("".join(traceback.format_stack(frame)))
    finally:
        del frames               # a frame held past the dump keeps its locals alive
    return "".join(parts)

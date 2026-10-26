"""The follower's runner: one :class:`levain.dispatch.follow.EventFollower` stepped in a loop until it is stopped.

:class:`FollowRunner` calls :meth:`EventFollower.step` again whenever a step ends. A step that moved the cursor, or
ended on its read deadline, is followed at once; a step that ended with the cursor where it was (CONTROL closed the
stream, a broken frame) waits first, as a stop does. A stop (:class:`FollowStopped`, or :class:`Refused` from opening
the stream: ``control-down`` while CONTROL is not up) is logged by its name and waited out before the next step:
a cause in :data:`TRANSIENT` waits :data:`BACKOFF_FIRST_S`, doubling while it repeats with no progress, up to
:data:`BACKOFF_MAX_S`; any other name (``ticket-unresolved``, ``record-conflict``, ``event-shape``, a refusal of the
verb or the ACK) names a cause no retry mends, so it waits :data:`LASTING_S` every time. :meth:`FollowRunner.stop`
ends the loop from another thread or a signal handler: a step in progress ends ``CANCELLED`` (the follower's own
:meth:`EventFollower.close`), a wait ends at once. Any other exception (a broken job store, say) closes the follower
and reaches the caller, and :func:`main` exits with it.

``python -I -m levain.dispatch.follow_run --store-dir <dir>``: the follower of CONTROL at ``R/control/control.sock``,
taking artifacts from ``L/export/dispatch`` into the job store at ``--store-dir``, until SIGTERM or SIGINT. It never
runs as root: CONTROL answers ``SESSION-EVENTS`` and ``ARTIFACT-ACK`` to the ``_levain_dispatch`` peer only, so it
runs as that uid. One runner per store: it holds ``<store-dir>/follow.lock`` while it runs and exits
:data:`EX_INSTANCE_HELD` when another holds it.
"""
from __future__ import annotations

import argparse
import fcntl
import logging
import os
import signal
import sys
import threading
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence

from levain.dispatch.follow import _PERMANENT_TAKES, EventFollower, FollowStopped, VerifierCall
from levain.dispatch.jobs import JobStore
from levain.slot import Refused
from levain.slot import control_client as CC
from levain.slot.layout import RUN, STORE

log = logging.getLogger("levain.dispatch.follow_run")

#: DECISION: the first wait after a transient stop, doubled while stops repeat with no progress
BACKOFF_FIRST_S = 1.0
#: DECISION: the longest wait after a transient stop (CONTROL down for a while is retried once a minute)
BACKOFF_MAX_S = 60.0
#: DECISION: the wait after a stop whose cause stays until the store, the export or CONTROL's journal changes
LASTING_S = 300.0
#: stop names a retry can mend on its own: CONTROL coming back, the machine's I/O, and the takes the follower parks
#: after ``follow.TAKE_FAILURE_LIMIT`` failures (each failure is one step, so they must come round again)
TRANSIENT = frozenset({"control-down", "artifact-io", "take-failed", *_PERMANENT_TAKES})

LOCK_NAME = "follow.lock"
EX_USAGE = 64
EX_INSTANCE_HELD = 75      # EX_TEMPFAIL: another runner holds the store; the unit manager starts this one again


class FollowRunner:
    """Steps ``follower`` until :meth:`stop`. ``wait(seconds)`` is the wait between steps, True when the runner was
    stopped during it (default: the stop event's own wait)."""

    def __init__(self, follower: EventFollower, *, first_s: float = BACKOFF_FIRST_S, max_s: float = BACKOFF_MAX_S,
                 lasting_s: float = LASTING_S, wait: Callable[[float], bool] | None = None) -> None:
        self.follower = follower
        self._first_s, self._max_s, self._lasting_s = first_s, max_s, lasting_s
        self._stopped = threading.Event()
        self._wait = wait if wait is not None else self._stopped.wait

    @property
    def stopped(self) -> bool:
        return self._stopped.is_set()

    def stop(self) -> None:
        """End :meth:`run`: the step in progress ends ``CANCELLED``, a wait ends now. Idempotent."""
        self._stopped.set()
        self.follower.close()

    def run(self) -> None:
        """Step until :meth:`stop`; the follower is closed when this returns or raises."""
        repeats = 0                                     # consecutive steps that ended without moving the cursor
        try:
            while not self._stopped.is_set():
                before = self.follower.jobs.event_cursor()
                try:
                    stepped = self.follower.step()
                except (FollowStopped, Refused) as e:
                    if self.follower.jobs.event_cursor() != before:
                        repeats = 0
                    delay = self._delay(e.name, repeats)
                    repeats += 1
                    log.warning("the follower stopped: %s; the next step in %.0f s", e, delay)
                else:
                    if stepped.outcome == CC.CANCELLED:
                        return
                    if stepped.outcome == CC.DEADLINE or self.follower.jobs.event_cursor() != before:
                        repeats = 0
                        continue
                    delay = self._delay("", repeats)
                    repeats += 1
                    log.info("the event stream ended %s with nothing read; the next step in %.0f s",
                             stepped.outcome, delay)
                if self._wait(delay):
                    return
        finally:
            self.follower.close()

    def _delay(self, name: str, repeats: int) -> float:
        if name and name not in TRANSIENT:
            return self._lasting_s
        return min(self._first_s * (2 ** min(repeats, 32)), self._max_s)


def no_minted_runs(_event: Mapping[str, Any]) -> Optional[VerifierCall]:
    """The ``resolve`` of an install whose dispatch mints no verifier ticket: no terminal verifier event is one of
    dispatch's, so the follower stops at one by name (``ticket-unresolved``), unacked, and waits :data:`LASTING_S`."""
    return None


def take_instance_lock(store_dir: Path) -> Optional[int]:
    """The held ``<store_dir>/follow.lock`` fd (close-on-exec), or None when another runner holds it."""
    fd = os.open(str(store_dir / LOCK_NAME), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        return None
    except BaseException:
        os.close(fd)
        raise
    return fd


def _host() -> str:
    return "darwin" if sys.platform == "darwin" else "linux"


def _parser(host: str) -> argparse.ArgumentParser:
    L, R = STORE[host], RUN[host]
    ap = argparse.ArgumentParser(prog="python -I -m levain.dispatch.follow_run",
                                 description="dispatch's SESSION-EVENTS follower; runs as _levain_dispatch.")
    ap.add_argument("--store-dir", required=True, help="the dispatch job store's directory (dispatch.db)")
    ap.add_argument("--socket", default=str(R / "control" / "control.sock"), help="the socket CONTROL serves")
    ap.add_argument("--export-dir", default=str(L / "export" / "dispatch"), help="root's export/dispatch directory")
    return ap


def main(argv: Optional[Sequence[str]] = None, *, resolve: Callable[[Mapping[str, Any]], Optional[VerifierCall]]
         = no_minted_runs) -> int:
    if os.geteuid() == 0:
        print("levain dispatch follower: refusing to run as root (it runs as _levain_dispatch)", file=sys.stderr)
        return EX_USAGE
    ap = _parser(_host())
    a = ap.parse_args(sys.argv[1:] if argv is None else list(argv))
    for flag, v in (("--store-dir", a.store_dir), ("--socket", a.socket), ("--export-dir", a.export_dir)):
        if not os.path.isabs(v):
            ap.error(f"{flag} must be an absolute path")
    logging.basicConfig(level=logging.INFO, stream=sys.stderr,
                        format=f"levain dispatch follower[{os.getpid()}]: %(name)s: %(message)s")
    jobs = JobStore(a.store_dir)
    jobs.event_cursor()                                 # makes and checks the store directory before the lock in it
    fd = take_instance_lock(Path(a.store_dir))
    if fd is None:
        print(f"levain dispatch follower: another runner holds {Path(a.store_dir) / LOCK_NAME}", file=sys.stderr)
        return EX_INSTANCE_HELD
    try:
        runner = FollowRunner(EventFollower.at(jobs, a.socket, resolve, export_dir=a.export_dir))
        old = {s: signal.signal(s, lambda _n, _f: runner.stop()) for s in (signal.SIGTERM, signal.SIGINT)}
        # the steps run on a worker so a signal is handled on this thread while a step blocks on its stream
        failed: list[BaseException] = []
        worker = threading.Thread(target=_run_into, args=(runner, failed), name="follow", daemon=True)
        worker.start()
        while worker.is_alive():
            worker.join(1.0)
        for s, h in old.items():
            signal.signal(s, h)
        if failed:
            raise failed[0]
        return 0
    finally:
        os.close(fd)


def _run_into(runner: FollowRunner, failed: list[BaseException]) -> None:
    try:
        runner.run()
    except BaseException as e:   # noqa: BLE001 - re-raised on the main thread
        failed.append(e)


__all__ = ["BACKOFF_FIRST_S", "BACKOFF_MAX_S", "EX_INSTANCE_HELD", "FollowRunner", "LASTING_S", "TRANSIENT", "main",
           "no_minted_runs", "take_instance_lock"]


if __name__ == "__main__":
    sys.exit(main())

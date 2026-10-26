"""The follower's runner: one :class:`levain.dispatch.follow.EventFollower` stepped in a loop until it is stopped.

:class:`FollowRunner` calls :meth:`EventFollower.step` again whenever a step ends. A step that moved the cursor, or
ended on its read deadline, is followed at once; a step that ended with the cursor where it was (CONTROL closed the
stream, a broken frame) waits first, as a stop does. A stop (:class:`FollowStopped`, or :class:`Refused` from opening
the stream: ``control-down`` while CONTROL is not up) is logged by its name and waited out before the next step:
a cause in :data:`RETRYABLE` waits :data:`BACKOFF_FIRST_S`, doubling while it repeats with no progress, up to
:data:`BACKOFF_MAX_S`; any other name (``ticket-unresolved``, ``record-conflict``, ``event-shape``, a refusal of the
verb or the ACK) names a cause no retry mends, so it waits :data:`LASTING_S` every time. :meth:`FollowRunner.stop`
ends the loop from another thread: a step in progress ends ``CANCELLED`` (the follower's own
:meth:`EventFollower.close`), a wait ends at once. A step that raises once stop is set ends the run as a stop does;
any other exception (a broken job store, say) closes the follower and reaches the caller, and :func:`main` exits
with it. A step that ends with an outcome this module does not know waits :data:`LASTING_S`. The signal handlers
:func:`main` installs only record the request; the main thread calls :meth:`FollowRunner.stop` after it.

``python -I -m levain.dispatch.follow_run``: the follower of CONTROL at ``R/control/control.sock``, taking artifacts
from ``L/export/dispatch`` into the job store at ``--store-dir`` (default ``L/dispatch``, the dispatch store row),
until SIGTERM or SIGINT. It never runs as root: CONTROL answers ``SESSION-EVENTS`` and ``ARTIFACT-ACK`` to the
``_levain_dispatch`` peer only, so it runs as that uid. One runner per store: it holds ``<store-dir>/follow.lock``
while it runs and exits :data:`EX_INSTANCE_HELD` when another holds it.
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

from levain.dispatch.follow import PERMANENT_TAKES, EventFollower, FollowStopped, VerifierCall
from levain.dispatch.jobs import JobStore
from levain.slot import Refused
from levain.slot import control_client as CC
from levain.slot.layout import RUN, STORE, dispatch_store_dir

log = logging.getLogger("levain.dispatch.follow_run")

#: DECISION: the first wait after a transient stop, doubled while stops repeat with no progress
BACKOFF_FIRST_S = 1.0
#: DECISION: the longest wait after a transient stop (CONTROL down for a while is retried once a minute)
BACKOFF_MAX_S = 60.0
#: DECISION: the wait after a stop whose cause stays until the store, the export or CONTROL's journal changes
LASTING_S = 300.0
#: stop names a later attempt can succeed at: CONTROL coming back, the machine's I/O, and the takes the follower parks
#: after ``follow.TAKE_FAILURE_LIMIT`` failures (the bundle may arrive; they come round at the backoff, up to
#: :data:`BACKOFF_MAX_S`)
RETRYABLE = frozenset({"control-down", "artifact-io", "take-failed", *PERMANENT_TAKES})
#: step outcomes the loop knows: a stream that ended (``levain.slot.control_client`` outcomes) and the follower's two
#: cursor drops; ``DEADLINE`` and ``CANCELLED`` are handled before this set is consulted
_ENDED = frozenset({CC.CLOSED, CC.RESET, CC.ERROR, CC.BROKEN, "journal-changed", "reset"})

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
                if self._stopped.is_set():              # a stop that landed during the read enters no step
                    return
                try:
                    stepped = self.follower.step()
                except (FollowStopped, Refused) as e:
                    if self._stopped.is_set():
                        return
                    if self.follower.jobs.event_cursor() != before:
                        repeats = 0
                    delay = self._delay(e.name, repeats)
                    repeats += 1
                    log.warning("the follower stopped: %s; the next step in %.0f s", e, delay)
                except Exception:
                    # a stop between the loop's check and the step leaves the step on a closed follower
                    if self._stopped.is_set():
                        return
                    raise
                else:
                    if stepped.outcome == CC.CANCELLED:
                        return
                    if stepped.outcome == CC.DEADLINE:
                        repeats = 0
                        continue
                    if stepped.outcome not in _ENDED:
                        delay = self._lasting_s
                        log.warning("the step ended with outcome %r, which the runner does not know; the next step "
                                    "in %.0f s", stepped.outcome, delay)
                    elif self.follower.jobs.event_cursor() != before:
                        repeats = 0
                        continue
                    else:
                        delay = self._delay("", repeats)
                        repeats += 1
                        log.info("the event stream ended %s with nothing read; the next step in %.0f s",
                                 stepped.outcome, delay)
                if self._wait(delay):
                    return
        finally:
            self.follower.close()

    def _delay(self, name: str, repeats: int) -> float:
        if name and name not in RETRYABLE:
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
    ap.add_argument("--store-dir", default=str(dispatch_store_dir(host)),
                    help="the dispatch job store's directory (dispatch.db)")
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
    jobs._check_directory()                             # makes and checks the store directory; opens no database
    fd = take_instance_lock(Path(a.store_dir))
    if fd is None:
        print(f"levain dispatch follower: another runner holds {Path(a.store_dir) / LOCK_NAME}", file=sys.stderr)
        return EX_INSTANCE_HELD
    try:
        jobs.event_cursor()                             # opens the database under the lock: a broken store ends here
        runner = FollowRunner(EventFollower.at(jobs, a.socket, resolve, export_dir=a.export_dir))
        requested: list[int] = []
        old: dict[int, Any] = {}
        failed: list[BaseException] = []
        worker = threading.Thread(target=_run_into, args=(runner, failed), name="follow", daemon=True)
        try:
            # the steps run on a worker so a signal is handled on this thread while a step blocks on its stream
            for s in (signal.SIGTERM, signal.SIGINT):
                old[s] = signal.signal(s, _stop_request_handler(requested))
            worker.start()
            while worker.is_alive():
                worker.join(0.2)
                if requested:
                    runner.stop()                       # off the handler: it takes the follower's locks
        finally:
            runner.stop()
            if worker.is_alive():
                worker.join()
            _restore_handlers(old, signal.signal)       # last: a signal during the final step is still ours
        if failed:
            raise failed[0]
        return 0
    finally:
        os.close(fd)


def _restore_handlers(old: Mapping[int, Any], install: Callable[[int, Any], Any]) -> None:
    """Put the previous handlers back; one installed from C reads back as None, which ``signal.signal`` refuses."""
    for s, h in old.items():
        install(s, signal.SIG_DFL if h is None else h)


def _stop_request_handler(requested: list[int]) -> Callable[[int, Any], None]:
    """A signal handler that only records the request: it runs on the main thread between any two bytecodes, so it
    takes no lock (a second signal during the first would find it held)."""
    def handler(signum: int, _frame: Any) -> None:
        requested.append(signum)
    return handler


def _run_into(runner: FollowRunner, failed: list[BaseException]) -> None:
    try:
        runner.run()
    except BaseException as e:   # noqa: BLE001 - re-raised on the main thread
        failed.append(e)


__all__ = ["BACKOFF_FIRST_S", "BACKOFF_MAX_S", "EX_INSTANCE_HELD", "FollowRunner", "LASTING_S", "RETRYABLE", "main",
           "no_minted_runs", "take_instance_lock"]


if __name__ == "__main__":
    sys.exit(main())

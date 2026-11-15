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
import json
import logging
import os
import signal
import stat
import sys
import threading
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence

from levain.dispatch.follow import PERMANENT_TAKES, EventFollower, FollowStopped, VerifierCall
from levain.dispatch.jobs import JobStore
from levain.dispatch.verifier_report import MAX_ENVELOPE_BYTES
from levain.slot import Refused
from levain.slot import control_client as CC
from levain.slot.layout import RUN, STORE, dispatch_store_dir
from levain.slot.lineage import SINK

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


#: the CONTROL verbs :class:`SupervisorFacts` asks, read-only, as the dispatch peer
SUPERVISOR_OPS = frozenset({"RUN-TICKET-FACTS", "ROUTEMAP-ACTIVE"})


class _ActiveMap:
    """The active route map as ``ROUTEMAP-ACTIVE`` answers it, in the shape
    :func:`levain.dispatch.jobs.resolve_backend_identity` reads (``generation``, ``routes[id].backends``, ``digests``)."""

    class _Route:
        def __init__(self, backends: Mapping[str, Any]) -> None:
            self.backends = dict(backends)

    def __init__(self, generation: int, routes: Mapping[str, Any], digests: Mapping[str, Any]) -> None:
        self.generation = generation
        self.routes = {rid: self._Route(r["backends"]) for rid, r in routes.items()}
        self.digests = dict(digests)


class SupervisorFacts:
    """PRE-W: the F9a follower's two supervisor readers, over CONTROL's socket as the dispatch peer (``port`` is a
    :class:`levain.slot.dispatch_runtime.DispatchControlPort` limited to :data:`SUPERVISOR_OPS`).
    :meth:`ticket_for` is ``RUN-TICKET-FACTS`` (the run's executor ticket from CONTROL's own journal) and
    :meth:`routemap` is ``ROUTEMAP-ACTIVE`` (the active generation; another generation is None). Nothing a follower's
    ``resolve`` returns reaches either. Every refusal, an unreachable CONTROL and any reply of the wrong shape is None
    (the run is unresolved: fail closed); neither method raises."""

    def __init__(self, port: Any) -> None:
        self._port = port

    def ticket_for(self, job_id: str, run_index: int) -> Optional[Mapping[str, Any]]:
        try:
            ans = self._port.call("RUN-TICKET-FACTS", job_id=job_id, run_index=run_index)
            facts = ans.get("facts") if isinstance(ans, Mapping) else None
            return dict(facts) if isinstance(facts, Mapping) else None
        except Exception as exc:  # noqa: BLE001 - unreachable or refused: unresolved, never a label
            log.warning("RUN-TICKET-FACTS for run %r of job %r: %s", run_index, job_id, getattr(exc, "name", type(exc).__name__))
            return None

    def routemap(self, generation: int) -> Optional[_ActiveMap]:
        try:
            ans = self._port.call("ROUTEMAP-ACTIVE")
            # an int on both sides before the compare: `True == 1` and `1.0 == 1`, a wrong-shaped reply (PRE-W L3 r1)
            if not isinstance(ans, Mapping) or type(generation) is not int or type(ans.get("generation")) is not int \
                    or ans["generation"] != generation:
                return None
            routes, digests = ans.get("routes"), ans.get("digests")
            if not isinstance(routes, Mapping) or not isinstance(digests, Mapping) \
                    or not all(isinstance(r, Mapping) and isinstance(r.get("backends"), Mapping) for r in routes.values()):
                return None
            return _ActiveMap(generation, routes, digests)
        except Exception as exc:  # noqa: BLE001 - unreachable or refused: unresolved, never a label
            log.warning("ROUTEMAP-ACTIVE: %s", getattr(exc, "name", type(exc).__name__))
            return None


def supervisor_facts(socket_path: "Path | str") -> SupervisorFacts:
    """The production :class:`SupervisorFacts` on CONTROL's socket at ``socket_path``."""
    from levain.slot.dispatch_runtime import DispatchControlPort
    return SupervisorFacts(DispatchControlPort(socket_path, ops=SUPERVISOR_OPS))


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
    ap.add_argument("--lineage-sink", default=str(L / "lineage" / SINK),
                    help="CONTROL's lineage sink (the answered model of a run is read from it)")
    return ap


#: the CONTROL verbs the production ``resolve`` asks, read-only, as the dispatch peer (S14 R3)
RESOLVE_OPS = frozenset({"RUN-TICKET-VERIFIER", "RUN-TICKET-STATUS"})


def control_resolve(socket_path: "Path | str", jobs: JobStore, readers: Any = None) -> Any:
    """The PRODUCTION ``resolve`` (S14 R3): :class:`levain.dispatch.verify_stage.ControlResolve` on CONTROL's socket at
    ``socket_path``, so a verifier event's run, entry and steps are CONTROL's own record of its ticket and never a file the
    runner writes. ``readers`` supply what CONTROL does not hold (the artifact tree, the verifier envelope bytes, the
    executor's answered model); the default :func:`unsourced_readers` answers nothing for them, so a report event (no
    envelope) fails its job ``report-missing`` and is acked (S14 L3 F1: never a wedge of the events behind it)."""
    from levain.dispatch.verify_stage import ControlResolve
    from levain.slot.dispatch_runtime import DispatchControlPort
    return ControlResolve(DispatchControlPort(socket_path, ops=RESOLVE_OPS), jobs,
                          readers if readers is not None else unsourced_readers())


def unsourced_readers() -> Any:
    """:class:`levain.dispatch.verify_stage.Readers` that answer nothing: no S1 code writes the verifier's envelope yet,
    and no source of an artifact's tree or an executor's answered model is built into the follower. Honest, not a stub of
    success: a report event fails its job ``report-missing``."""
    from levain.dispatch.verify_stage import Readers
    return Readers(tree=lambda _job, _run: None, envelope=lambda _session, _ticket: None,
                   answered=lambda _job, _run: None)


#: the verifier envelope root writes under ``<export>/<session>/``
ENVELOPE_NAME = "verifier-report.json"


def _read_envelope(export_dir: "Path | str", session: object) -> Optional[bytes]:
    """``<export_dir>/<session>/verifier-report.json``'s bytes, or None. Every component is opened ``O_NOFOLLOW`` from
    its parent's fd (a link at any depth reads as None), the file must be a regular file (a fifo is never opened for
    blocking: ``O_NONBLOCK``), and at most ``MAX_ENVELOPE_BYTES + 1`` bytes are read, an over-cap file reading as None.
    ``session`` must be one plain name."""
    if type(session) is not str or not session or session in (".", "..") or "/" in session or "\0" in session:
        return None
    root = str(export_dir)
    if not os.path.isabs(root) or "\0" in root:
        return None
    parts = [p for p in root.split("/") if p] + [session]
    dflags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    cur = -1
    try:
        cur = os.open("/", dflags)
        for p in parts:
            nxt = os.open(p, dflags, dir_fd=cur)
            os.close(cur)
            cur = nxt
        fd = os.open(ENVELOPE_NAME, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=cur)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                return None
            data = b""
            while len(data) <= MAX_ENVELOPE_BYTES:
                chunk = os.read(fd, MAX_ENVELOPE_BYTES + 1 - len(data))
                if not chunk:
                    break
                data += chunk
        finally:
            os.close(fd)
    except OSError:
        return None
    finally:
        if cur >= 0:
            os.close(cur)
    return data if len(data) <= MAX_ENVELOPE_BYTES else None


#: DECISION (S14 fix 4): the largest lineage sink :func:`_read_answered` reads; a larger one answers None (the model
#: is optional, D5: the record then says "answering model unknown"), so a long-lived sink never costs the follower
#: its size in memory per event
ANSWERED_SINK_MAX = 128 << 20
#: DECISION: the read size, and the longest line considered (a longer one is skipped to its newline, never held)
_ANSWERED_CHUNK = 1 << 20
_ANSWERED_LINE_MAX = 1 << 20


def _answered_of(raw: bytes, needle: bytes, job_id: str, run_index: int) -> Optional[str]:
    """One sink line's ``model_answered`` for ``(job_id, run_index)`` when it names one, else None."""
    if not raw or needle not in raw:
        return None
    try:
        row = json.loads(raw.decode("utf-8"))
    except (ValueError, RecursionError):
        return None
    if not isinstance(row, dict) or row.get("job_id") != job_id or type(row.get("run_index")) is not int \
            or row["run_index"] != run_index:
        return None
    m = row.get("model_answered")
    return m if isinstance(m, str) and m and m != "unknown" else None


def _read_answered(sink: "Path | str", job_id: object, run_index: object) -> Optional[str]:
    """The last ``model_answered`` of the lineage sink's rows for ``(job_id, run_index)`` that is a non-empty string and
    not ``"unknown"``, else None (the model is optional, D5). FAIL CLOSED to None: the sink is opened ``O_NOFOLLOW |
    O_NONBLOCK`` (a link is not followed, a fifo never blocks the follower), it must be a regular file of at most
    :data:`ANSWERED_SINK_MAX` bytes, and it is read in bounded chunks (a sink that grows past the cap while read is
    None too), never whole. A line that does not parse is skipped, as is a torn last one and a line longer than
    :data:`_ANSWERED_LINE_MAX`; no state is kept."""
    if type(job_id) is not str or not job_id or type(run_index) is not int:
        return None
    try:
        fd = os.open(os.fspath(sink), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except (OSError, TypeError, ValueError):
        return None
    needle = job_id.encode("utf-8")
    answered: Optional[str] = None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_size > ANSWERED_SINK_MAX:
            return None
        tail, total, skipping = b"", 0, False
        while True:
            chunk = os.read(fd, _ANSWERED_CHUNK)
            if not chunk:
                break
            total += len(chunk)
            if total > ANSWERED_SINK_MAX:
                return None
            lines = (tail + chunk).split(b"\n")
            tail = lines.pop()                          # the part after the last newline: a line still being read
            if skipping and lines:
                lines.pop(0)                            # the rest of an over-long line, up to its newline
                skipping = False
            for raw in lines:
                m = _answered_of(raw, needle, job_id, run_index)
                if m is not None:
                    answered = m
            if len(tail) > _ANSWERED_LINE_MAX:
                tail, skipping = b"", True
        if tail and not skipping:
            m = _answered_of(tail, needle, job_id, run_index)    # a torn last line does not parse: skipped
            if m is not None:
                answered = m
    except OSError:
        return None
    finally:
        os.close(fd)
    return answered


def production_readers(export_dir: "Path | str", sink: "Path | str") -> Any:
    """S14 A5: :class:`levain.dispatch.verify_stage.Readers` for two of the three sources. ``envelope(session, ticket)``
    is root's exported envelope (:func:`_read_envelope`; ``check_envelope`` later binds it to the event's digest, session
    and ticket); ``answered(job, run)`` is the lineage sink's last named model for the run (:func:`_read_answered`);
    ``tree`` stays unsourced (None) until the artifact tree is built (A6), so until then a report event fails its job
    ``verifier-tree-unavailable`` and is acked (HEAD DEFAULT, S14 fix 1: never a wedge of the events behind it)."""
    from levain.dispatch.verify_stage import Readers
    return Readers(tree=lambda _job, _run: None,
                   envelope=lambda session, _ticket: _read_envelope(export_dir, session),
                   answered=lambda job, run: _read_answered(sink, getattr(job, "id", None), run))


def main(argv: Optional[Sequence[str]] = None, *, resolve: Optional[Callable[[Mapping[str, Any]], Optional[VerifierCall]]]
         = None, routemap: Any = None, ticket_for: Any = None,
         supervisor: Any = "socket", readers: Any = None) -> int:
    """Run the follower until SIGTERM/SIGINT. ``resolve`` defaults, in production, to :func:`control_resolve` on
    ``--socket`` (S14 R3; :func:`no_minted_runs` is no longer the production resolve, it stays for an install that wants
    to refuse every verifier event). ``routemap`` and ``ticket_for`` (F9a) default to the SUPERVISOR's own
    readers on ``--socket`` (:func:`supervisor_facts`, PRE-W) when ``supervisor`` is ``"socket"``; ``supervisor=None``
    wires none (every run unresolved), and an explicit reader replaces its default. Never anything ``resolve`` says."""
    if os.geteuid() == 0:
        print("levain dispatch follower: refusing to run as root (it runs as _levain_dispatch)", file=sys.stderr)
        return EX_USAGE
    ap = _parser(_host())
    a = ap.parse_args(sys.argv[1:] if argv is None else list(argv))
    for flag, v in (("--store-dir", a.store_dir), ("--socket", a.socket), ("--export-dir", a.export_dir),
                    ("--lineage-sink", a.lineage_sink)):
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
        # F9a / PRE-W: the run's executor ticket and the route map come from CONTROL's own records over its socket
        # (RUN-TICKET-FACTS, ROUTEMAP-ACTIVE), never from resolve; an unreachable CONTROL resolves nothing
        if supervisor == "socket":
            facts = supervisor_facts(a.socket)
            routemap = facts.routemap if routemap is None else routemap
            ticket_for = facts.ticket_for if ticket_for is None else ticket_for
        if resolve is None:
            resolve = control_resolve(a.socket, jobs,
                                      readers if readers is not None else production_readers(a.export_dir, a.lineage_sink))
        runner = FollowRunner(EventFollower.at(jobs, a.socket, resolve, export_dir=a.export_dir, routemap=routemap,
                                               ticket_for=ticket_for))
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
           "no_minted_runs", "production_readers", "take_instance_lock", "SUPERVISOR_OPS", "SupervisorFacts", "supervisor_facts"]


if __name__ == "__main__":
    from levain.home_guard import mark_live
    mark_live()
    sys.exit(main())

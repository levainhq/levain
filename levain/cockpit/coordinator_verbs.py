"""levain.cockpit.coordinator_verbs: the Coordinator panel's Start, Stop and Hold (C10 S3).

Each is an act the coordinator already has, fired from the panel; nothing here adds state or a rule.

- **Start** does what a drop with no holder does: :meth:`CoordStore.coord_wake` writes a ``pending`` coordinator launch
  and its ``bootstrap`` claim, and a thread runs :func:`levain.coordinator.launch.launch_session` on it, the
  shape :meth:`levain.coordinator.phone.CoordPhone._wake` has (the reply never waits on a spawn). Start first reads the
  state the panel draws (:func:`levain.cockpit.coordinator_status._read_status`, on the read-only connection): any word
  but ``wake`` is refused with the panel's own line (``<word>: <why>``) and nothing is written, so Start in ``backoff``
  or ``stopped`` says why, and a second Start while a launch is pending or the lease is held starts nothing. The
  wake's own transaction decides again under its write lock, so two Starts racing past the read still ask for one
  launch. How the start went is the launch's own record (``active``, or ``failed`` with its cause), drawn by the panel;
  a start refused before its spawn claim leaves the launch pending, which the serve's watcher retries, and its words go
  to the serve log.
- **Stop** is your ``release`` (:meth:`CoordStore.coord_release`, ``by="operator"``): the lease ends, pending launches
  are voided and every running coordinator launch is ended. The session itself is stopped by the watcher's next pass
  (``claude stop``), which runs inside the serve.
- **Hold it myself** is your ``take`` for :data:`HOLD` (:meth:`CoordStore.coord_take`): pending launches are voided,
  running coordinator launches end, and drops queue for you until the hold runs out.

Each is C1 on the cockpit's write token and binds to the panel. None is a device verb (``device_ok`` stays off): the
design's phone path for them is not built here. A refusal is an ``EditError`` raised before anything is written, so
the same click may try again. None makes a coordinator store: with none there is no panel to fire from.
"""
from __future__ import annotations

import sqlite3
import sys
import threading
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from levain.cockpit.coordinator_status import HOLD_VERB, START_VERB, STOP_VERB
from levain.cockpit.verbs import VerbSpec, _const
from levain.writes import EditError

__all__ = ["HOLD", "CoordinatorVerbs"]

# the hold the design names for "Hold it myself": the lease's own TTL
HOLD = timedelta(minutes=30)
_LOG_PREFIX = "levain coord start: "


def _to_log(line: str) -> None:
    print(f"{_LOG_PREFIX}{line}", file=sys.stderr, flush=True)


def _faults() -> tuple[type[BaseException], ...]:
    from levain.dispatch.jobs import JobStoreError

    return (sqlite3.Error, OSError, JobStoreError)


class CoordinatorVerbs:
    """Start, Stop and Hold over the coordinator store in ``directory()`` (the dispatch directory the panel reads, named on
    each fire). ``clock`` is the instant the panel's state is judged at and the store's clock; ``launcher`` starts a
    launch (:func:`levain.coordinator.launch.launch_session` unless a test hands in another); ``emit`` takes the start
    thread's line (the serve's stderr by default)."""

    def __init__(self, directory: Callable[[], Path | str | None], *,
                 clock: Callable[[], datetime] | None = None,
                 launcher: Callable[[Any, str], Any] | None = None,
                 emit: Callable[[str], None] = _to_log) -> None:
        self._directory = directory
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._launcher = launcher
        self._emit = emit
        self._lock = threading.Lock()
        self._starts: list[threading.Thread] = []

    # --- the store ---------------------------------------------------------------------------------------------------
    def _store(self) -> Any:
        """The store the panel reads, or a refusal: the probe is the panel's own (it creates nothing), and the writer is
        the store class the drop path and the watcher write through."""
        from levain.cockpit.coordinator_panels import _store_at
        from levain.coordinator.envelopes import EnvelopeStore

        try:
            probe = _store_at(self._directory)
        except _faults() as exc:
            raise EditError("unavailable", 503, f"the coordinator store cannot be read now ({type(exc).__name__}: "
                                                f"{str(exc)[:200]}); nothing was written") from None
        if probe is None:
            raise EditError("refused", 409, "there is no coordinator store here (levain coord agent claude makes one); "
                                            "nothing was written")
        return EnvelopeStore(probe.directory, clock=self._clock)

    def _state(self, store: Any) -> tuple[str, str]:
        """The panel's state word and its ``why``, read as the panel reads them (read-only)."""
        from levain.cockpit.coordinator_status import _read_status
        from levain.coordinator.store import StoreAbsent

        try:
            st = _read_status(store, self._clock())
        except StoreAbsent:
            raise EditError("refused", 409, "there is no coordinator store here; nothing was written") from None
        except _faults() as exc:
            raise EditError("unavailable", 503, f"the coordinator store cannot be read now ({type(exc).__name__}: "
                                                f"{str(exc)[:200]}); nothing was written") from None
        return str(st["wake"]), str(st["why"])

    def _write(self, act: Callable[[], Any], what: str) -> Any:
        from levain.coordinator.store import CoordError, StoreAbsent

        try:
            return act()
        except StoreAbsent:
            raise EditError("refused", 409, f"there is no coordinator store here; {what}") from None
        except CoordError as exc:
            raise EditError("refused", 409, f"{exc}; {what}") from None
        except _faults() as exc:
            raise EditError("unavailable", 503, f"the coordinator store cannot answer now ({type(exc).__name__}: "
                                                f"{str(exc)[:200]}); {what}") from None

    # --- the verbs ---------------------------------------------------------------------------------------------------
    def start(self, _scope: Any, _params: dict[str, Any], _target: dict[str, Any], _confirm: bool) -> dict[str, Any]:
        store = self._store()
        word, why = self._state(store)
        if word != "wake":
            # the panel's own line, so the refusal and the panel never say two different things
            raise EditError("refused", 409, f"{word}: {why}", {"state": word, "why": why})
        launch = self._write(store.coord_wake, "nothing was started")
        if launch is None:
            # another writer moved the store between the read and the wake's own transaction
            word, why = self._state(store)
            if word == "wake":
                # it has moved back to a state that would accept: a refusal naming "wake" would contradict itself
                raise EditError("unavailable", 503, "the coordinator store changed under the start; try again; "
                                                    "nothing was started", {"state": word, "why": why})
            raise EditError("refused", 409, f"{word}: {why}", {"state": word, "why": why})
        started = self._start_thread(store, launch.launch_id)
        said = (f"coordinator launch {launch.launch_id} asked for; it is starting now and the panel shows how it went"
                if started else
                f"coordinator launch {launch.launch_id} asked for; it could not be started here, the serve's watcher "
                f"starts it on its next pass (pending until {launch.until})")
        return {"state": "launching", "launch_id": launch.launch_id, "until": launch.until, "said": said}

    def stop(self, _scope: Any, _params: dict[str, Any], _target: dict[str, Any], _confirm: bool) -> dict[str, Any]:
        store = self._store()
        ended = self._write(store.coord_release, "nothing was released")
        held = None if ended is None else {"holder": ended.holder, "token": ended.token}
        # the lease's holder is the launch's session key, not a person, so the line names the token only; the
        # release voids and ends launches whether or not a lease was held
        said = (("the coordinator lease was released" if ended is None else
                 f"the coordinator lease (token {ended.token}) was released")
                + "; any pending coordinator launch is voided, any running one ended, and the watcher stops its "
                  "session on its next pass")
        return {"released": held, "said": said}

    def hold(self, _scope: Any, _params: dict[str, Any], _target: dict[str, Any], _confirm: bool) -> dict[str, Any]:
        store = self._store()
        token = self._write(lambda: store.coord_take(hold=HOLD), "nothing was taken")
        # The take has committed. Nothing below raises: a refusal releases the fire's idempotency reservation, so a
        # retry under the same key would take a second lease and void this one. A follow-up read that fails or shows
        # another lease is said as it is, with no claim of a hold this fire could not confirm.
        try:
            held = store.coord_lease()
        except Exception as exc:  # noqa: BLE001 - the take stands whatever this read does
            return {"token": token, "until": None,
                    "said": f"the coordinator lease was taken (token {token}); it could not be read back now "
                            f"({type(exc).__name__}), so the panel shows how long it holds"}
        if held is None or getattr(held, "token", None) != token:
            gone = "released" if held is None else "superseded by another take or claim"
            return {"token": token, "until": None,
                    "said": f"the coordinator lease was taken (token {token}) and is already {gone}; drops do not "
                            "queue for this hold"}
        until = getattr(held, "until", None)
        return {"token": token, "until": until,
                "said": f"you hold the coordinator lease until {until} (token {token}); drops queue for you"}

    # --- the start thread --------------------------------------------------------------------------------------------
    def _start_thread(self, store: Any, launch_id: str) -> bool:
        """Start ``launch_id`` on its own thread; whether the thread started. A thread that cannot start leaves the
        launch pending for the watcher, which starts every pending launch nobody claimed."""
        launcher = self._launcher
        if launcher is None:
            from levain.coordinator.launch import launch_session

            launcher = launch_session

        def run() -> None:
            try:
                got = launcher(store, launch_id)
                if got.started:
                    line = f"launch {launch_id} started: {got.attach}"
                else:
                    line = f"launch {launch_id} not started: {got.refusal}"
                    if getattr(got, "remedy", None):
                        line += f" (run once on the laptop: {got.remedy})"
            except Exception as exc:  # noqa: BLE001 - the launch keeps its own record; this thread only reports
                line = f"launch {launch_id} not started: {type(exc).__name__}: {str(exc)[:200]}"
            try:
                self._emit(line)
            except Exception:  # noqa: BLE001 - a log that cannot be written changes nothing the launch recorded
                pass

        try:
            # Not a daemon: every harness call a launch makes is bounded by its own timeout, so a process exiting
            # mid-start waits for the launch to settle (active, or failed with its session stopped) instead of being
            # killed between a spawn and its record. Started under the lock and listed only once running, so the
            # list never holds a thread that a join would refuse or that the prune would drop while it is starting.
            t = threading.Thread(target=run, name=f"levain-coord-start-{launch_id}", daemon=False)
            with self._lock:
                t.start()
                self._starts = [w for w in self._starts if w.is_alive()] + [t]
        except Exception:  # noqa: BLE001 - the wake has committed; the watcher is the launch's other starter
            with self._lock:
                self._starts = [w for w in self._starts if w.is_alive()]
            return False
        return True

    def wait_starts(self, timeout: float) -> None:
        """Wait for the start threads begun so far (tests, and a server shutting down)."""
        with self._lock:
            starts = list(self._starts)
        for t in starts:
            if t.ident is None:      # never started: there is nothing to wait for
                continue
            t.join(timeout)

    # --- the registration --------------------------------------------------------------------------------------------
    def specs(self) -> dict[str, VerbSpec]:
        """The downstream panel verbs, keyed by name. Idempotent: a retry under one key (a reply lost on the way)
        is answered from its first fire, never a second take, release or wake."""
        c1 = {"binding": "panel", "fields": (), "floor": "C1", "tier_fn": _const("C1"), "idempotent": True}
        return {
            START_VERB: VerbSpec(START_VERB, "Start", "asks for a coordinator now, by the rules a drop runs (it waits "
                                 "out a backoff and starts nothing while stopped), and starts it", fire=self.start,
                                 **c1),
            STOP_VERB: VerbSpec(STOP_VERB, "Stop", "ends the coordinator lease and its launch as your release; the "
                                "serve's watcher then stops the session", reversible=False, fire=self.stop, **c1),
            HOLD_VERB: VerbSpec(HOLD_VERB, "Hold it myself", f"you take the coordinator lease for "
                                f"{int(HOLD.total_seconds() // 60)} minutes: a pending launch is voided, a running "
                                "coordinator ends, and drops queue for you", reversible=False, fire=self.hold, **c1),
        }

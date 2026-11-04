"""levain.agents.tick_driver — P7-1 S11: the process that runs the controller tick and its reaper (S10 DESIGN section 5).

``levain agents tick`` is run by a launchd *user agent* (operator uid, ``StartInterval = TICK_INTERVAL_S``), exits after
one pass, and is the only thing that calls :meth:`levain.agents.supervise.Supervisor.reap` in production: it renews the
leases of ``launched`` rows, stops the rows past ``stop_at`` or STALE, and then claims scheduled windows.

THE BOUND, as arithmetic (a test pins it from these constants, never from a restated number):

* a graceful SIGALRM bound fires at :data:`TICK_SOFT_BOUND_S`; the hard-exit watchdog (``levain.firing.deadline``,
  layer 2: ``os._exit``) fires at :data:`TICK_HARD_BOUND_S` whatever the main thread is doing;
* ``TICK_HARD_BOUND_S >= STOP_BUDGET_S + STOP_DEADLINE_S``: a full stop phase fits inside one tick;
* the reaper renews every launched row at the TOP of a tick, so the worst gap between two renewals of one row is one
  interval plus a tick that ran to its hard bound: :data:`WORST_RENEW_GAP_S` = ``TICK_INTERVAL_S + TICK_HARD_BOUND_S``
  = 660 s, under the 900 s lease (``controller.LEASE_S``): a row renewed at the top of tick k is renewed again before
  it lapses even when tick k hung until the watchdog took the process down.

A sleeping laptop coalesces missed launchd ticks, so leases lapse during sleep and the first tick on wake runs lapse
recovery (``takeover``). That is designed behaviour, not an alarm.

What the tick wires (and what it does not): the ``executor_of`` router (:func:`executor_of_factory`: ``session`` rows to
:class:`levain.agents.session.RowSessionExecutor`, ``dream`` rows to the dream's own executor, anything else None, which
the supervisor reads as "nothing to ask" and raises its attention row), the production ``binding_of`` (the live link's
binding, from the authority log) and the attention sink (:func:`attention_sink`: one stderr line per key; the agent store
holds no attention table yet). It changes no stop rule: which rows a reaper ENDs live in ``session.py`` (S11b).
"""
from __future__ import annotations

import logging
import sys
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from levain.agents import controller as _ctl
from levain.agents import supervise as _sup
from levain.agents.store import AgentStore, open_agent_store

_log = logging.getLogger("levain.agents.tick_driver")

#: [judged, S10 DESIGN 5] the schedule's minute granularity (``controller.tick`` floors the clock to the minute) and the
#: root reaper's ``V11_S``; 1/15 of the lease, because the same tick also claims scheduled windows.
TICK_INTERVAL_S = 60
#: the hard-exit watchdog (``os._exit``): the tick is over by here whatever it is doing.
TICK_HARD_BOUND_S = 600
#: the graceful SIGALRM bound (``TurnTimeout``), a grace before the hard one so a healthy unwind is never cut short.
TICK_SOFT_BOUND_S = 540
#: the worst gap between two renewals of one row (the invariant the lease needs to exceed).
WORST_RENEW_GAP_S = TICK_INTERVAL_S + TICK_HARD_BOUND_S
#: exit codes: 0 a pass ran; 1 refused (store, install); 3 the graceful bound fired; 70 the watchdog took the process.
EXIT_OK, EXIT_REFUSED, EXIT_REAPER_ERROR, EXIT_SOFT_TIMEOUT, EXIT_HARD_TIMEOUT = 0, 1, 2, 3, 70

__all__ = ["TICK_INTERVAL_S", "TICK_HARD_BOUND_S", "TICK_SOFT_BOUND_S", "WORST_RENEW_GAP_S", "EXIT_OK", "EXIT_REFUSED", "EXIT_REAPER_ERROR",
           "EXIT_SOFT_TIMEOUT", "EXIT_HARD_TIMEOUT", "bounded", "executor_of_factory", "binding_of_factory",
           "attention_sink", "run_tick"]


def _hard_report(seconds: float) -> str:
    return (f"levain agents tick: still running {seconds:.0f}s after it started (the graceful bound did not land); "
            "the process is exiting now. launchd starts the next tick; a child already asked to stop is not signalled "
            "again by this exit, and no lease can lapse inside one tick (the worst renewal gap is under the lease).")


def bounded(fn: Callable[[], Any], *, soft_s: float, hard_s: float, stream=None) -> Any:
    """Run ``fn()`` under a graceful bound at ``soft_s`` (raises :class:`levain.firing.deadline.TurnTimeout`) and a
    hard-exit watchdog at ``hard_s`` (``os._exit(EXIT_HARD_TIMEOUT)``, armed first). ``hard_s`` must exceed ``soft_s``."""
    from levain.firing.deadline import TurnDeadline

    if not hard_s > soft_s > 0:
        raise ValueError("the hard bound must exceed the graceful bound, and both be positive")
    with TurnDeadline(soft_s, grace=hard_s - soft_s, hard_exit_code=EXIT_HARD_TIMEOUT, stream=stream,
                      hard_report=_hard_report):
        return fn()


# --- the executor_of router ---------------------------------------------------------------------------------------

def executor_of_factory(store: AgentStore, *, launcher_of: Callable[[], Any],
                        dream_executor_of: Callable[[Mapping[str, Any]], Any] | None = None,
                        pinned_of: Callable[[AgentStore, str], tuple[str | None, str | None, str]] | None = None,
                        ) -> Callable[[dict], Any]:
    """The ``executor_of`` for ``Supervisor.reap``. A ``launched`` row's executor is read from the role its own SIGNED
    spec pins (``controller._pinned_executor``: re-hashed, never the mutable ``agents.name`` row):

    * ``session`` -> ``RowSessionExecutor(store, row, launcher)`` (it re-checks the row, role, agent and key itself);
      ``launcher_of()`` is called once, lazily, and a failure to build it is None (no executor: the supervisor raises
      its attention row and asks nothing);
    * ``dream`` -> ``dream_executor_of(row)``;
    * anything else, anything unreadable, a row with no session key (for a session role), or a builder that raises -> None.
    """
    from levain.agents.session import RowSessionExecutor

    pinned = pinned_of or _ctl._pinned_executor
    launcher_cell: list = []

    def launcher() -> Any:
        if not launcher_cell:
            try:
                launcher_cell.append(launcher_of())
            except Exception as exc:  # noqa: BLE001 - a launcher that cannot be built ends nothing
                _log.warning("agent tick: no session launcher (%s: %s)", type(exc).__name__, exc)
                launcher_cell.append(None)
        return launcher_cell[0]

    def executor_of(row: dict) -> Any:
        try:
            _name, kind, _why = pinned(store, row["occurrence_id"])
            if kind == "session":
                if type(row.get("session_key")) is not str:
                    return None
                got = launcher()
                return None if got is None else RowSessionExecutor(store, row, got)
            if kind == "dream" and dream_executor_of is not None:
                return dream_executor_of(row)
        except Exception as exc:  # noqa: BLE001 - a row that cannot be routed is asked nothing, and says so
            _log.warning("agent tick: row %s has no executor (%s: %s)", row.get("occurrence_id"), type(exc).__name__, exc)
        return None

    return executor_of


def session_launcher_of(store: AgentStore) -> Callable[[], Any]:
    """``launcher_of`` for the store's ``session_install.json`` (``session_wire.resolve_launcher``)."""
    def build() -> Any:
        from levain.agents import session_wire as _wire
        return _wire.resolve_launcher(store, _wire.read_install(store.directory))
    return build


def dream_executor_of(store: AgentStore) -> Callable[[Mapping[str, Any]], Any]:
    """The dream's executor for a ``launched`` dream row, built the way its fire builds it
    (``levain.dream.agent``: ``DreamExecutor`` over ``<store>/fires`` and ``<scratch>/dream/<occurrence_id>``)."""
    def build(row: Mapping[str, Any]) -> Any:
        from levain.dream import agent as _da, executor as _ex
        inst = _da.read_install(store.directory)
        oid = row["occurrence_id"]
        return _ex.DreamExecutor(agent_store_dir=store.directory, occurrence_id=oid,
                                 occ_dir=inst.store_path.parent / "dream" / oid, route="claude", model=inst.model,
                                 facts_path=inst.facts_path)
    return build


# --- binding_of ---------------------------------------------------------------------------------------------------

def binding_of_factory(store: AgentStore, authority: Any, clock: Callable[[], datetime]) -> Callable[[str, str], str | None]:
    """``binding_of(agent_id, spec_hash)``: the ``binding_id`` of the agent's highest-born LIVE link of that spec (the
    rule ``controller._pick_link`` applies at launch), or None (not armed). The authority log is replayed once per
    driver; a log that cannot be replayed arms nothing (None, logged): the window is missed visibly, never claimed on a
    guess."""
    from levain import authority as _auth

    cell: list = []

    def state() -> Any:
        if not cell:
            try:
                cell.append(authority.refresh())
            except Exception as exc:  # noqa: BLE001
                _log.warning("agent tick: the authority log could not be replayed (%s: %s); nothing is armed this tick",
                             type(exc).__name__, exc)
                cell.append(None)
        return cell[0]

    def binding_of(agent_id: str, spec_hash: str) -> str | None:
        snap = state()
        if snap is None:
            return None
        with store.read() as conn:
            store_id = _ctl._store_id_of(conn)
            closed = conn.execute("SELECT MAX(revoke_seq) FROM agent_bars WHERE agent_id = ? AND closed_at IS NOT NULL",
                                  (agent_id,)).fetchone()[0] or 0
        now = clock()
        live = [gid for gid in _auth.agent_links(snap, agent_id, store_id)
                if snap.policy.grants[gid]["spec_hash"] == spec_hash
                and _auth.agent_link_arms(snap, gid, now=now, closed_bar_revoke_seq=closed)]
        if not live:
            return None
        top = max(live, key=lambda gid: snap.grant_born.get(gid) or 0)
        binding = snap.policy.grants[top].get("binding_id")
        return binding if type(binding) is str and binding else None

    return binding_of


# --- the attention sink -------------------------------------------------------------------------------------------

def attention_sink(stream=None) -> tuple[Callable[[str, str], None], Callable[[str], None]]:
    """``(attention, resolve)`` for ``Supervisor.reap``: a printed, keyed line each (the agent store holds no attention
    table yet; ``levain agents revoke --abandon`` prints the same way). Never raises."""
    def attention(key: str, text: str) -> None:
        print(f"attention [{key}]: {text}", file=stream or sys.stderr, flush=True)

    def resolve(key: str) -> None:
        print(f"attention resolved [{key}]", file=stream or sys.stderr, flush=True)

    return attention, resolve


# --- one pass -----------------------------------------------------------------------------------------------------

def _cockpit_root(store: AgentStore) -> Path:
    """The install's cockpit root: the session install's, else the dream install's (a store with only a dream). Raises
    ``ValueError`` (``InstallRefused`` is one) when neither file can be read."""
    from levain.agents import session_wire as _wire
    from levain.dream import agent as _da
    try:
        root = _wire.read_install(store.directory).cockpit_root
    except _wire.InstallRefused as session_exc:
        try:
            return _da.read_install(store.directory).cockpit_root
        except _da.InstallRefused:
            raise session_exc from None
    try:
        other = _da.read_install(store.directory).cockpit_root
    except _da.InstallRefused:
        return root
    if other.resolve() != root.resolve():
        raise _wire.InstallRefused(f"the session install names cockpit {root} and the dream install {other}: one "
                                   "store has one authority log, so the tick will not guess")
    return root


def _install_coord_dir(store: AgentStore) -> Path | None:
    """The coordinator queue directory the dream install names (its dispatch.db feeds the slot-lease probes), else None."""
    from levain.dream import agent as _da
    try:
        return _da.read_install(store.directory).coord_dir
    except _da.InstallRefused:
        return None


def _utc() -> datetime:
    return datetime.now(timezone.utc)


def run_tick(store: AgentStore | Path | str | None, *, coord_dir: Path | str | None = None,
             clock: Callable[[], datetime] | None = None, authority: Any = None,
             launcher_of: Callable[[], Any] | None = None,
             dream_of: Callable[[Mapping[str, Any]], Any] | None = None,
             soft_s: float = TICK_SOFT_BOUND_S, hard_s: float = TICK_HARD_BOUND_S, stream=None) -> int:
    """One bounded pass: ``controller.tick`` with this module's reaper. Returns an exit code (see :data:`EXIT_OK`).
    The seams (``clock``, ``authority``, ``launcher_of``, ``dream_of``) are tests'; production passes none."""
    from levain.agent_authority import AgentAuthority
    from levain.firing.deadline import TurnTimeout

    clock = clock or _utc

    def work() -> Any:
        # everything that can block (store open, install reads, the probes' dispatch.db) runs INSIDE the bound
        nonlocal coord_dir
        st = store if isinstance(store, AgentStore) else open_agent_store(store)
        auth = authority if authority is not None else AgentAuthority(_cockpit_root(st))
        attention, resolve = attention_sink(stream)
        sup = _sup.Supervisor(st, authority=auth, clock=clock)
        executor_of = executor_of_factory(st, launcher_of=launcher_of or session_launcher_of(st),
                                          dream_executor_of=dream_of or dream_executor_of(st))
        if coord_dir is None:
            coord_dir = _install_coord_dir(st)
        probes: tuple = ()
        if coord_dir is not None:
            launch_over, session_alive = _ctl.dispatch_probes(coord_dir)   # (launch_over, session_alive)
            probes = (session_alive, launch_over)                          # tick takes (session_alive, launch_over)
        return _ctl.tick(st, clock, binding_of_factory(st, auth, clock), *probes,
                         reaper=lambda: sup.reap(executor_of, attention=attention, resolve=resolve))

    try:
        out = bounded(work, soft_s=soft_s, hard_s=hard_s, stream=stream)
    except TurnTimeout:
        print(f"levain agents tick: the graceful bound ({soft_s:.0f}s) fired; the next tick resumes.",
              file=stream or sys.stderr, flush=True)
        return EXIT_SOFT_TIMEOUT
    if getattr(out, "reaper_error", None):
        print(f"levain agents tick: the reaper raised {out.reaper_error}; claims ran, the pass is not clean.",
              file=stream or sys.stderr, flush=True)
        return EXIT_REAPER_ERROR
    return EXIT_OK

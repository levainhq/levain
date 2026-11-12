"""levain.agents.tick_driver — P7-1 S11: the process that runs the controller tick and its reaper (S10 DESIGN section 5).

``levain agents tick`` is run by a launchd *user agent* (operator uid, ``StartInterval = TICK_INTERVAL_S``), exits after
one pass, and is the only thing that calls :meth:`levain.agents.supervise.Supervisor.reap` in production: it renews the
leases of ``launched`` rows, stops the rows past ``stop_at`` or STALE, and then claims scheduled windows.

THE BOUND, as arithmetic (a test pins it from these constants, never from a restated number):

* a graceful SIGALRM bound fires at :data:`TICK_SOFT_BOUND_S`; the hard-exit watchdog (``levain.firing.deadline``,
  layer 2: ``os._exit``) fires at :data:`TICK_HARD_BOUND_S` whatever the main thread is doing;
* ``TICK_HARD_BOUND_S >= STOP_BUDGET_S + STOP_DEADLINE_S``: a full stop phase fits inside one tick;
* the renewals are the first thing a tick does after the store opens: before them run only the store open, the two
  install-file reads that name the cockpit and the supervisor's construction (the desk directory, its
  ``dispatch.db`` and the claims all come AFTER the reaper). That phase is held to :data:`TICK_PRE_RENEW_BOUND_S`, and
  a tick that overran it says so on stderr (a gauge: the renewals still run). So the worst gap between two renewals of
  one non-withheld row is one interval, a tick that ran to its hard bound and the next tick's pre-renewal phase:
  :data:`WORST_RENEW_GAP_S` = ``TICK_INTERVAL_S + TICK_HARD_BOUND_S + TICK_PRE_RENEW_BOUND_S`` = 720 s, under the
  900 s lease (``controller.LEASE_S``). A dead-fire session row whose renewal is withheld until its own stop answers
  (P7-1 S3) is outside this sum by design: it may lapse to takeover.
* inside the reaper, a session row's dead-fire probe (P7-1 S3) runs just before that row's renewal (on a refused
  install, so does every row's executor build), so a slow probe delays the renewals after it; the authority replay
  runs after every renewal. That in-reap time is not in the sum.
* an install that names no usable cockpit runs the reaper alone (no authority: nothing is classified STALE, rows past
  ``stop_at`` are still stopped) over ONLY the rows this process has an executor to stop; a row it could not stop is
  not renewed (renewing it would keep it launched with nothing able to end it), so its lease lapses to takeover, and
  the tick names it. It claims nothing and exits :data:`EXIT_REFUSED`.

STARVATION: the supervisor's "deferred rows first" memory is process-local and this process lives for one pass, so the
tick rotates the launched rows' order by the tick's minute instead (:class:`_RotatingSupervisor`: the row at
``minute % len(rows)`` leads), so when the stop budget runs out no fixed set of rows leads every pass; with a tick
every minute and a stable row set each row leads once in ``len(rows)`` minutes. ``Supervisor.reap`` keeps that order
within its stable partition (proven-dead-fire rows first), so rows outside that group can still wait while that group
alone fills the stop budget. Nothing new is stored.

A sleeping laptop coalesces missed launchd ticks, so leases lapse during sleep and the first tick on wake runs lapse
recovery (``takeover``). That is designed behaviour, not an alarm.

What the tick wires (and what it does not): the ``executor_of`` router (:func:`executor_of_factory`: ``session`` rows to
:class:`levain.agents.session.RowSessionExecutor`, ``dream`` rows to the dream's own executor, anything else None, which
the supervisor reads as "nothing to ask" and raises its attention row), the production ``binding_of`` (the live link's
binding, from the authority log) and the attention sink (:func:`attention_sink`: one stderr line per key; the agent store
holds no attention table yet). It changes no stop rule: which rows a reaper ENDs live in ``session.py`` (S11b).

THE NIGHT'S NOTICES (D1-N): after the claims, a store whose install names a dream runs
``levain.dream.agent.tick_notices``: the did-not-consolidate card for each closed window (one per window and class,
however many ticks), the pause notice, the link-renewal card and the weekly restore drill. They reach a reader through
the ``agent`` feed of the desk's review store (``levain desk review list``), beside the tick's stderr line. They start
only while :data:`TICK_NOTICE_RESERVE_S` of the graceful bound is left, run after the renewals (so
:data:`WORST_RENEW_GAP_S` is unchanged), and a failed or skipped notice is :data:`EXIT_NOTICES` plus a stderr line.
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
#: what the phase before the renewals (store open, the cockpit's install reads, the supervisor) may take; past it the
#: tick names the overrun on stderr.
TICK_PRE_RENEW_BOUND_S = 60
#: the worst gap between two renewals of one non-withheld row (the invariant the lease needs to exceed).
WORST_RENEW_GAP_S = TICK_INTERVAL_S + TICK_HARD_BOUND_S + TICK_PRE_RENEW_BOUND_S
#: the night's notices (D1-N) start only while at least this much of the graceful bound is left. They run after the
#: renewals and the claims, so they cannot move the lease arithmetic above; this keeps them from being the thing that
#: runs a tick into the graceful bound. The one slow step is the weekly restore drill (a store copy and a rollback).
TICK_NOTICE_RESERVE_S = 120
#: exit codes: 0 a pass ran; 1 refused (store, install: with an install refusal the renewals of rows it could stop
#: still ran, the claims did not); 2 the reaper raised; 3 the graceful bound fired; 4 a launched session row had no
#: session launcher to END it, so it was not renewed and its lease lapses to takeover (an install that names none, or
#: whose launcher cannot be built); 5 a night notice failed, raised, or was skipped for time (the tick itself ran;
#: the stderr line says which); 70 the watchdog took the process.
EXIT_OK, EXIT_REFUSED, EXIT_REAPER_ERROR, EXIT_SOFT_TIMEOUT, EXIT_NO_LAUNCHER, EXIT_NOTICES, EXIT_HARD_TIMEOUT = (
    0, 1, 2, 3, 4, 5, 70)

__all__ = ["TICK_INTERVAL_S", "TICK_HARD_BOUND_S", "TICK_SOFT_BOUND_S", "TICK_PRE_RENEW_BOUND_S", "TICK_NOTICE_RESERVE_S",
           "WORST_RENEW_GAP_S", "EXIT_OK", "EXIT_REFUSED", "EXIT_REAPER_ERROR", "EXIT_SOFT_TIMEOUT", "EXIT_NO_LAUNCHER",
           "EXIT_NOTICES", "EXIT_HARD_TIMEOUT", "bounded", "executor_of_factory", "binding_of_factory", "attention_sink",
           "run_tick"]


def _hard_report(seconds: float) -> str:
    # ``seconds`` is the GRACEFUL bound (TurnDeadline passes its own), so the text names it as that
    return (f"levain agents tick: still running past its graceful bound ({seconds:.0f}s), at its hard bound; the "
            "process is exiting now. launchd starts the next tick; a child already asked to stop is not signalled "
            "again by this exit.")


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

    A session row met with no usable launcher (the builder raised, gave None, or gave the unwired default) is recorded
    on the returned callable's ``no_launcher`` list (``(occurrence_id, why)``): :func:`run_tick` names those rows and
    exits :data:`EXIT_NO_LAUNCHER`, so a tick that met a session row it could not END is never a clean exit.
    """
    from levain.agents.session import RowSessionExecutor, UnwiredLauncher

    pinned = pinned_of or _ctl._pinned_executor
    launcher_cell: list = []      # [(launcher or None, why)]
    no_launcher: list[tuple[str, str]] = []

    def launcher() -> tuple[Any, str]:
        if not launcher_cell:
            try:
                got = launcher_of()
                why = ("no launcher was built" if got is None else
                       "the install names no launcher (the unwired default)" if isinstance(got, UnwiredLauncher) else
                       "the launcher has no callable end_session, so nothing could stop the row"
                       if not callable(getattr(got, "end_session", None)) else "")
                launcher_cell.append((None if why else got, why))
            except Exception as exc:  # noqa: BLE001 - a launcher that cannot be built ends nothing
                launcher_cell.append((None, f"{type(exc).__name__}: {exc}"))
            if launcher_cell[0][0] is None:
                _log.warning("agent tick: no session launcher (%s)", launcher_cell[0][1])
        return launcher_cell[0]

    def executor_of(row: dict) -> Any:
        try:
            _name, kind, _why = pinned(store, row["occurrence_id"])
            if kind == "session":
                if type(row.get("session_key")) is not str:
                    return None
                got, why = launcher()
                if got is None:
                    if all(oid != row["occurrence_id"] for oid, _ in no_launcher):
                        no_launcher.append((row["occurrence_id"], why))
                    return None
                return RowSessionExecutor(store, row, got)
            if kind == "dream" and dream_executor_of is not None:
                return dream_executor_of(row)
        except Exception as exc:  # noqa: BLE001 - a row that cannot be routed is asked nothing, and says so
            _log.warning("agent tick: row %s has no executor (%s: %s)", row.get("occurrence_id"), type(exc).__name__, exc)
        return None

    executor_of.no_launcher = no_launcher  # type: ignore[attr-defined]
    return executor_of


def session_launcher_of(store: AgentStore) -> Callable[[], Any]:
    """``launcher_of`` for the store's ``session_install.json`` (``session_wire.resolve_launcher``). The production launcher
    is registered by importing ``session_wire`` (S10a); an install that names none gives the unwired default, and one
    whose launcher cannot be built refuses: either way the router records the session rows it met and :func:`run_tick`
    exits :data:`EXIT_NO_LAUNCHER`."""
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
    """``binding_of(agent_id, spec_hash)``: the ``binding_id`` of the agent's highest-born LIVE link of that spec, or
    None (not armed). LIVE is judged as ``controller._pick_link`` judges it at launch: through ``authority.peek``
    (unexpired, born after the last revoke and closed bar, AND both its born and current signers still active keys),
    with the peek's incarnation checked against this driver's replay. So a link whose signer was revoked is unarmed
    here (the agent is reported unarmed) instead of claimed and then refused at launch as ``signer_revoked``. The
    authority log is replayed once per driver; a log that cannot be replayed, a cover that moved, or a grant that
    lacks its fields arms nothing (None, logged): the window is missed visibly, never claimed on a guess."""
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
        grants = snap.policy.grants
        candidates = sorted((gid for gid in _auth.agent_links(snap, agent_id, store_id)
                             if (grants.get(gid) or {}).get("spec_hash") == spec_hash),
                            key=lambda gid: snap.grant_born.get(gid) or 0, reverse=True)
        for gid in candidates:
            try:
                live, incarnation = authority.peek(gid, agent_id=agent_id, store_id=store_id, now=now,
                                                   closed_bar_revoke_seq=closed)
            except Exception as exc:  # noqa: BLE001 - a cover that moved or cannot be read arms nothing this tick
                _log.warning("agent tick: link %s could not be peeked (%s: %s); %s is not armed this tick", gid,
                             type(exc).__name__, exc, agent_id)
                return None
            if incarnation != _auth.agent_link_incarnation(snap, gid):
                _log.warning("agent tick: link %s moved under this tick's replay; %s is not armed this tick", gid,
                             agent_id)
                return None
            if live is True:
                binding = grants[gid].get("binding_id")
                return binding if type(binding) is str and binding else None
        return None

    def latest_link(agent_id: str, spec_hash: str) -> dict[str, str] | None:
        """``{"id", "expires_at"}`` of the agent's link of that spec that expires LAST, or None (no link). Read from the
        same one replay :func:`binding_of` uses, so the notice phase adds no second read of the log. The last expiry
        is the one that decides when the agent stops being armed. A log that could not be replayed RAISES: the renewal
        check must say it could not look, never read it as "no link"."""
        snap = state()
        if snap is None:
            raise RuntimeError("the authority log could not be replayed, so no link's expiry could be read")
        with store.read() as conn:
            store_id = _ctl._store_id_of(conn)
        grants = snap.policy.grants
        best: dict[str, str] | None = None
        for gid in _auth.agent_links(snap, agent_id, store_id):
            grant = grants.get(gid) or {}
            expires = grant.get("expires_at")
            if grant.get("spec_hash") == spec_hash and type(expires) is str and (
                    best is None or expires > best["expires_at"]):      # the one stamp form is fixed-width: it sorts as time
                best = {"id": gid, "expires_at": expires}
        return best

    binding_of.latest_link = latest_link  # type: ignore[attr-defined]
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
    """The install's cockpit root: the session install's, else the dream install's (a store with only a dream). An
    install is absent ONLY when its file does not exist; one that exists and cannot be read raises its refusal
    (``InstallRefused``, a ``ValueError``), as does a store with neither file or two files naming two cockpits."""
    from levain.agents import session_wire as _wire
    from levain.dream import agent as _da
    d = Path(store.directory)
    roots = []
    if (d / _wire.INSTALL_FILE).exists():
        roots.append(_wire.read_install(d).cockpit_root)
    if (d / _da.INSTALL_FILE).exists():
        roots.append(_da.read_install(d).cockpit_root)
    if not roots:
        raise _wire.InstallRefused(f"no install file ({_wire.INSTALL_FILE} or {_da.INSTALL_FILE}) in {d}")
    if len(roots) == 2 and roots[0].resolve() != roots[1].resolve():
        raise _wire.InstallRefused(f"the session install names cockpit {roots[0]} and the dream install {roots[1]}: one "
                                   "store has one authority log, so the tick will not guess")
    return roots[0]


def _install_coord_dir(store: AgentStore, notes: list[str]) -> Path | None:
    """The desk queue directory the dream install names (its dispatch.db feeds the slot-lease probes), else
    None. No install file is a plain None; a file that exists but cannot be read (refused, corrupt, unreadable) is
    also None, and its cause goes in ``notes`` so the tick never reports it as merely absent."""
    from levain.dream import agent as _da
    try:
        if not (Path(store.directory) / _da.INSTALL_FILE).exists():
            return None
        return _da.read_install(store.directory).coord_dir
    except Exception as exc:  # noqa: BLE001 - named, never read as "no install"
        notes.append(f"the dream install cannot be read for its desk directory ({type(exc).__name__}: {exc})")
        return None


def _coord_dir_for(store: AgentStore, given: Path | str | None, notes: list[str] | None = None) -> Path | None:
    """The desk directory for the probes: ``--coord-dir``, else the dream install's, else, for the DEFAULT
    agent store only, the default consent queue (``<levain home>/consent``, where the desk keeps
    ``dispatch.db``) when a ``dispatch.db`` is there. None when none of these names one."""
    if given is not None:
        return Path(given)
    found = _install_coord_dir(store, notes if notes is not None else [])
    if found is not None:
        return found
    from levain.autonomic.db import default_store_dir
    from levain.consent.queue import default_queue_dir
    try:
        if Path(store.directory).expanduser().resolve() == Path(default_store_dir()).expanduser().resolve():
            queue = default_queue_dir()
            if (queue / "dispatch.db").is_file():
                return queue
    except Exception:  # noqa: BLE001 - an environment that cannot be read names no directory
        pass
    return None


def _lazy_probes(store: AgentStore, given: Path | str | None, notes: list[str]) -> tuple[Callable, Callable]:
    """``(session_alive, launch_over)`` for ``controller.tick``, resolved on their FIRST call (the claims phase, after the
    reaper's renewals), never before. With no desk directory both answer None ("cannot tell": the expired
    launch slot is kept, never freed on a guess) and the tick names that in ``notes``."""
    cell: list = []

    def probes() -> tuple | None:
        if not cell:
            where = _coord_dir_for(store, given, notes)
            if where is None:
                notes.append("an expired desk launch slot cannot be probed: no desk directory (pass "
                             "--coord-dir, or install the dream, which names one); the slot is kept")
                cell.append(None)
            else:
                cell.append(_ctl.dispatch_probes(where))     # (launch_over, session_alive)
        return cell[0]

    def session_alive(launch_id: str) -> bool | None:
        got = probes()
        return None if got is None else got[1](launch_id)

    def launch_over(launch_id: str) -> bool | None:
        got = probes()
        return None if got is None else got[0](launch_id)

    return session_alive, launch_over


class _RotatingSupervisor(_sup.Supervisor):
    """The supervisor for one tick process, its launched rows rotated by ``rotation`` (the tick's minute). The
    supervisor keeps "rows deferred past the stop budget go first next pass" in process memory, which a process that
    lives one pass loses; the rotation is the stateless stand-in: the stop order starts at a different row each minute,
    so no row stays behind rows that keep using the budget up. Renewals happen in the same rotated order."""

    def __init__(self, *args: Any, rotation: int, keep: Callable[[dict], bool] | None = None, **kw: Any) -> None:
        super().__init__(*args, **kw)
        self._rotation = rotation
        self._keep = keep       # a row it rejects is neither renewed nor stopped this pass (the refused-install tick)

    def _launched(self) -> list[dict]:
        rows = super()._launched()
        if self._keep is not None:
            rows = [r for r in rows if self._keep(r)]
        if not rows:
            return rows
        k = self._rotation % len(rows)
        return rows[k:] + rows[:k]


class _NoAuthority:
    """The authority stand-in for a tick whose install names no usable cockpit: every read raises the refusal, which
    the supervisor reads as "cannot tell" (UNCLASSIFIED: no STALE stop) and the tick never claims through."""

    def __init__(self, refusal: Exception) -> None:
        self._refusal = refusal

    def refresh(self) -> Any:
        raise self._refusal

    def peek(self, *a: Any, **k: Any) -> Any:
        raise self._refusal


def _utc() -> datetime:
    return datetime.now(timezone.utc)


def _run_notices(st: AgentStore, result: Any, clock: Callable[[], datetime], started: datetime, soft_s: float,
                 link_of: Callable[[str, str], Any], stream: Any, notes: list[str], seen: dict) -> None:
    """The night's notices (D1-N, Q6 (a)): ``levain.dream.agent.tick_notices`` once a tick has finished its claims. It
    starts only while at least :data:`TICK_NOTICE_RESERVE_S` of the graceful bound is left (the restore drill, once a
    week, is the one slow step); a tick already later than that says so and leaves them to the next, which repeats
    them harmlessly (every step is once-per-key). A step that failed, a pass that raised, or a skip sets ``seen["notices"]``,
    which :func:`run_tick` turns into :data:`EXIT_NOTICES`; each is one line on stderr, never swallowed."""
    from levain.dream import agent as _da

    spent = (clock() - started).total_seconds()
    if spent > soft_s - TICK_NOTICE_RESERVE_S:
        notes.append(f"the night's notices were not run: the tick was already {spent:.0f}s in, with under "
                     f"{TICK_NOTICE_RESERVE_S}s of its {soft_s:.0f}s graceful bound left for them; the next tick runs them")
        seen["notices"] = "late"
        return
    try:
        report = _da.tick_notices(st, now=clock(), missed_windows=result.missed_windows,
                                  link_of=link_of, stream=stream)
    except Exception as exc:  # noqa: BLE001 - tick_notices guards each step; this is a defect in the pass itself
        notes.append(f"the night's notices raised {type(exc).__name__}: {exc}")
        seen["notices"] = "raised"
        return
    for line in report.notes:
        notes.append(line)
    for problem in report.problems:
        notes.append(f"night notice failed: {problem}")
    if report.problems:
        seen["notices"] = "problems"


def run_tick(store: AgentStore | Path | str | None, *, coord_dir: Path | str | None = None,
             clock: Callable[[], datetime] | None = None, authority: Any = None,
             launcher_of: Callable[[], Any] | None = None,
             dream_of: Callable[[Mapping[str, Any]], Any] | None = None,
             soft_s: float = TICK_SOFT_BOUND_S, hard_s: float = TICK_HARD_BOUND_S, stream=None) -> int:
    """One bounded pass: ``controller.tick`` with this module's reaper. Returns an exit code (see :data:`EXIT_OK`).
    The seams (``clock``, ``authority``, ``launcher_of``, ``dream_of``) are tests'; production passes none.

    Order (the renewal-gap arithmetic in the module docstring rests on it): open the store, name the cockpit, build the
    supervisor, then the reaper (renewals first); the desk directory and its ``dispatch.db`` are read only by the
    claims phase, lazily. Every line the pass has to say (an install refusal, a late renewal phase, session rows with
    no launcher, probes with no desk directory) goes to stderr as ``levain agents tick: ...``."""
    from levain.agent_authority import AgentAuthority
    from levain.agents import session_wire as _wire
    from levain.dream import agent as _da
    from levain.firing.deadline import TurnTimeout

    clock = clock or _utc
    notes: list[str] = []
    seen: dict = {}

    def work() -> Any:
        # everything that can block runs INSIDE the bound; only what the renewals need runs before them
        started = clock()
        st = store if isinstance(store, AgentStore) else open_agent_store(store)
        refusal = None
        if authority is not None:
            auth = authority
        else:
            try:
                auth = AgentAuthority(_cockpit_root(st))
            except (_wire.InstallRefused, _da.InstallRefused, OSError) as exc:
                refusal, auth = exc, _NoAuthority(exc)
        attention, resolve = attention_sink(stream)
        executor_of = executor_of_factory(st, launcher_of=launcher_of or session_launcher_of(st),
                                          dream_executor_of=dream_of or dream_executor_of(st))
        seen["executor_of"] = executor_of
        unstoppable: list[str] = []

        def stoppable(row: dict) -> bool:
            # the tick renews only a row it could stop: a renewal of a row nothing can stop would keep it launched
            # forever, where an unrenewed lease lapses to takeover (refused install or not)
            if executor_of(row) is not None:
                return True
            unstoppable.append(row["occurrence_id"])
            return False

        sup = _RotatingSupervisor(st, authority=auth, clock=clock,
                                  rotation=int(started.timestamp()) // TICK_INTERVAL_S,
                                  keep=stoppable)

        def reaper() -> Any:
            lag = (clock() - started).total_seconds()
            if lag > TICK_PRE_RENEW_BOUND_S:
                notes.append(f"the renewals began {lag:.0f}s into the tick, past the {TICK_PRE_RENEW_BOUND_S}s the "
                             f"renewal-gap bound allows ({WORST_RENEW_GAP_S}s against the {_ctl.LEASE_S}s lease): a "
                             "lease the previous tick renewed may have lapsed")
            try:
                return sup.reap(executor_of, attention=attention, resolve=resolve)
            finally:                # named on the graceful bound too
                if unstoppable:
                    why = dict(executor_of.no_launcher).get(unstoppable[0], "no executor could be built for it")
                    notes.append(f"{len(unstoppable)} launched row(s) have no executor that could stop them ({why}; "
                                 f"first: {unstoppable[0]}): they were not renewed, and their leases lapse to takeover")

        if refusal is not None:
            # the install names no usable cockpit: renew the rows this process could stop and stop what the store
            # alone shows due; leave every other row's lease to lapse; claim nothing
            notes.append(f"{refusal}; the renewals of stoppable rows ran, no window was claimed")
            seen["refused"] = True
            try:
                reaper()            # TurnTimeout is a BaseException: the graceful bound passes through this handler
            except Exception as exc:  # noqa: BLE001
                _log.warning("agent tick: the reaper raised %s: %s", type(exc).__name__, exc)
                seen["reaper_error"] = type(exc).__name__
            return None
        session_alive, launch_over = _lazy_probes(st, coord_dir, notes)
        binding_of = binding_of_factory(st, auth, clock)
        result = _ctl.tick(st, clock, binding_of, session_alive, launch_over, reaper=reaper)
        # AFTER the renewals and the claims, so the lease arithmetic above is untouched: the night's notices
        _run_notices(st, result, clock, started, soft_s, binding_of.latest_link, stream, notes, seen)
        return result

    def say(text: str) -> None:
        print(f"levain agents tick: {text}", file=stream or sys.stderr, flush=True)

    def report() -> list:
        """Every note, then the rows with no session launcher (exit 4's case). A refused-install tick names its own
        unstoppable rows in the notes, so 1 never hides a 4."""
        for note in notes:
            say(note)
        rows = [] if seen.get("refused") else list(getattr(seen.get("executor_of"), "no_launcher", []))
        if rows:
            say(f"{len(rows)} launched session row(s) have no session launcher, so they were not renewed (a "
                f"row nothing can END lapses to takeover) ({rows[0][1]}; first: {rows[0][0]}). Name a launcher in the install file.")
        return rows

    try:
        out = bounded(work, soft_s=soft_s, hard_s=hard_s, stream=stream)
    except TurnTimeout:
        report()
        say(f"the graceful bound ({soft_s:.0f}s) fired; the next tick resumes.")
        return EXIT_SOFT_TIMEOUT
    unlaunchable = report()
    error = seen.get("reaper_error") or getattr(out, "reaper_error", None)
    if error:
        say(f"the reaper raised {error}; the pass is not clean.")
        return EXIT_REAPER_ERROR
    if seen.get("refused"):
        return EXIT_REFUSED
    if unlaunchable:
        return EXIT_NO_LAUNCHER
    if seen.get("notices"):
        return EXIT_NOTICES
    return EXIT_OK

"""levain.agents.supervise — the run supervisor, fire side (A0 r8 §2.3; BUILD_PLAN P6-4a).

This module is the only caller of the agent ``RunJournal.effect`` path: every effect a fire makes for an occurrence goes
through :meth:`Supervisor.effect`, which builds the occurrence's ``(fence, fence_now)`` with
:func:`levain.agent_authority.agent_fence` and passes ``needs_decision=False``. ``tests/test_agents_supervise_fire.py``
walks the tree and asserts that no other module does.

What the fire side owns:

* **The UNCLASSIFIED retry.** An effect whose cover cannot be read (``fence_now`` raised) wrote nothing. The supervisor
  refreshes :class:`~levain.agent_authority.AgentAuthority` outside any transaction, at most once per tick per
  supervisor (one process, one snapshot shared by every occurrence it supervises), and tries the effect again, up to
  :data:`MAX_TRIES` attempts in all. After that the effect waits for the next pass and the occurrence stays
  ``launched``. A moved head alone is an UNCLASSIFIED, never a stop. A tick is the caller's: it calls
  :meth:`Supervisor.begin_tick` once per pass, and a supervisor that never does keeps its first snapshot for ever.
* **The STALE end.** Any STALE ends the occurrence ``stopped``. The expected fence is built from the launch columns on
  every call and never replaced, so nothing here re-reads and adopts a new one. The row is written only by the
  stop-then-settle rule: the executor's ``stop()`` must answer ``StopResult(terminated=True)`` exactly, and then
  :func:`levain.agents.controller.settle_terminal` writes the end, frees the slot and settles the reservation in one
  ``BEGIN IMMEDIATE``. An answer that is not that leaves the row ``launched``, holding its slot and reservation; the
  reaper (the tick side) asks again. The supervisor reads a stop only through that ``StopResult``; it holds no gate
  ``returncode``, so :func:`levain.agents.controller.gate_proves_termination` is the executor's to apply to its own
  child.
* **The hook points** ``on_end`` and ``verify`` (:class:`Hooks`, the module's :data:`HOOKS`), called by
  :meth:`Supervisor.finish`, the run's natural end. Both default to nothing. P8b registers into :data:`HOOKS`.

The reaper, ``stop_at``, the tick's order and the unconfirmed-stop attention row are the tick side's.
"""
from __future__ import annotations

import logging
import sqlite3
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone

from levain.agent_authority import AgentAuthority, AgentAuthorityError, agent_fence
from levain.agents import accounting, controller as _ctl
from levain.agents.store import AgentStore
from levain.autonomic.journal import EffectOutcome, EffectStatus, RunJournal

__all__ = ["MAX_TRIES", "STOP_DEADLINE_S", "Effected", "End", "Hooks", "HOOKS", "Supervisor"]

_log = logging.getLogger("levain.agents.supervise")

MAX_TRIES = 3          # A0 §2.3: the effect is tried at most this many times in one pass
STOP_DEADLINE_S = 10   # A0 r6 §2.3 [judged by A0]: what a stop() may wait

_OCCURRENCE_COLUMNS = ("occurrence_id", "agent_id", "run_id", "state", "epoch", "launch_epoch", "link_id",
                       "link_incarnation", "spec_hash", "binding_id")


@dataclass(frozen=True)
class Effected:
    """What one :meth:`Supervisor.effect` did.

    ``outcome`` is the journal's last answer and ``tries`` how many times it was asked. ``waits`` is True when the cover
    stayed unreadable through the tries (the occurrence is still ``launched`` and the next pass retries). On a STALE,
    ``stop`` is the executor's answer (None when the row had already ended and nothing was asked), and ``ended`` what
    the settle wrote (None when ``stop`` did not confirm, so nothing was written)."""
    outcome: EffectOutcome
    tries: int
    waits: bool = False
    stop: accounting.StopResult | None = None
    ended: _ctl.Ended | None = None

    @property
    def stale(self) -> bool:
        return self.outcome.status is EffectStatus.STALE


@dataclass(frozen=True)
class End:
    """What an ended occurrence tells the hooks: the state and reason the settle wrote (``verify`` receives the state
    and reason the run asked for, before the settle), the executor's reading and the settle's result."""
    occurrence_id: str
    state: str
    reason: str
    usage: accounting.Usage
    ended: _ctl.Ended | None = None


@dataclass
class Hooks:
    """The two points P8b fills. ``verify(end)`` runs before the end is written, outside any transaction, and returns
    None to accept the end or a reason (a non-empty string) that turns a ``done`` into ``failed``; a verifier that
    raises fails the run as ``verify_error``. ``on_end(end)`` runs after the end is committed; what it raises is
    logged and never undoes the end."""
    verify: Callable[[End], str | None] | None = None
    on_end: Callable[[End], None] | None = None


HOOKS = Hooks()


def _executor_stop(executor: accounting.Executor, deadline_s: float) -> accounting.StopResult:
    try:
        result = executor.stop(deadline_s)
    except Exception as exc:  # noqa: BLE001 - the contract is that stop() never raises; one that does confirmed nothing
        return accounting.StopResult(False, f"stop() raised {type(exc).__name__}")
    return result if type(result) is accounting.StopResult else accounting.StopResult(False, "stop() returned no StopResult")


def _executor_usage(executor: accounting.Executor) -> accounting.Usage:
    try:
        usage = executor.usage()
    except Exception:  # noqa: BLE001 - unreadable is Usage(None, ...): the whole reservation is debited
        usage = None
    return usage if type(usage) is accounting.Usage else accounting.Usage(None, None, None, "unreadable")


class Supervisor:
    """The fire side of the supervisor for one process: its authority, its store, its journal and its tick."""

    def __init__(self, store: AgentStore, *, authority: AgentAuthority, clock: Callable[[], datetime] | None = None,
                 hooks: Hooks | None = None, stop_deadline_s: float = STOP_DEADLINE_S,
                 journal: RunJournal | None = None) -> None:
        self._store = store
        self._authority = authority
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._hooks = HOOKS if hooks is None else hooks
        self._stop_deadline_s = stop_deadline_s
        self._journal = journal if journal is not None else RunJournal(store.directory)
        with store.read() as conn:
            self._store_id = _ctl._store_id_of(conn)
        self._lock = threading.Lock()
        self._refreshed: bool | None = None   # this tick's one refresh: None not yet, True read, False failed

    # --- the tick's shared refresh ---------------------------------------------------------------------------------

    def begin_tick(self) -> None:
        """Start a pass: the next unreadable cover may refresh the authority once again."""
        with self._lock:
            self._refreshed = None

    def refresh_once(self) -> bool:
        """Refresh the authority, outside any transaction, unless this tick already did; True when this tick's refresh
        read. A refresh that failed is remembered for the tick: it is not tried again, and it is False."""
        with self._lock:
            if self._refreshed is None:
                try:
                    self._authority.refresh()
                    self._refreshed = True
                except Exception as exc:  # noqa: BLE001 - a log that cannot be replayed is never "nothing live"
                    _log.warning("agent supervisor: the authority refresh failed (%s): %s", type(exc).__name__, exc)
                    self._refreshed = False
            return self._refreshed

    # --- the effect path -------------------------------------------------------------------------------------------

    def _occurrence(self, occurrence_id: str) -> dict:
        with self._store.read() as conn:
            row = conn.execute(f"SELECT {', '.join(_OCCURRENCE_COLUMNS)} FROM agent_occurrences"
                               " WHERE occurrence_id = ?", (occurrence_id,)).fetchone()
        if row is None:
            raise AgentAuthorityError("the occurrence is not in the store")
        return dict(zip(_OCCURRENCE_COLUMNS, row))

    def effect(self, occurrence_id: str, effect_id: str, *, digest: str, fn: Callable[[], object],
               executor: accounting.Executor) -> Effected:
        """Run one effect of a launched occurrence at most once, under its fence (the one caller of the agent
        ``RunJournal.effect``, ``needs_decision=False``). ``fn`` runs only when the fence holds; ``executor`` is asked to
        stop on a STALE. Raises :class:`~levain.agent_authority.AgentAuthorityError` for an occurrence that was never
        launched."""
        occ = self._occurrence(occurrence_id)
        fence, fence_now = agent_fence(occ, authority=self._authority, store_id=self._store_id, now=self._clock)
        tries = 0
        while True:
            tries += 1
            outcome = self._journal.effect(occ["run_id"], effect_id, digest=digest, fence=fence,
                                           fence_now=fence_now, fn=fn, needs_decision=False)
            if outcome.status is EffectStatus.STALE:
                return self._end_stale(occ, outcome, tries, executor)
            if outcome.status is not EffectStatus.UNCLASSIFIED:
                return Effected(outcome, tries)
            if tries >= MAX_TRIES or not self.refresh_once():
                return Effected(outcome, tries, waits=True)

    # --- the STALE end, by the stop rule ---------------------------------------------------------------------------

    def _end_stale(self, occ: dict, outcome: EffectOutcome, tries: int, executor: accounting.Executor) -> Effected:
        with self._store.read() as conn:
            live = conn.execute("SELECT 1 FROM agent_occurrences WHERE occurrence_id = ? AND state = 'launched'",
                                (occ["occurrence_id"],)).fetchone()
        if live is None:      # it ended on its own: nothing to stop, nothing to settle twice
            return Effected(outcome, tries)
        stop = _executor_stop(executor, self._stop_deadline_s)
        if stop.terminated is not True:
            return Effected(outcome, tries, stop=stop)
        ended = self._settle(occ["occurrence_id"], "stopped", None, _executor_usage(executor), occ)
        return Effected(outcome, tries, stop=stop, ended=ended)

    def _stale_reason(self, conn: sqlite3.Connection, occ: dict, epoch: int) -> str:
        """The reason nameable from the current side. Every difference only narrows, so no cause is needed to end."""
        if conn.execute("SELECT 1 FROM agent_bars WHERE agent_id = ? AND closed_at IS NULL",
                        (occ["agent_id"],)).fetchone() is not None:
            return "barred"
        if epoch != occ["launch_epoch"]:
            return "fenced"
        return "link_not_live"

    def _settle(self, occurrence_id: str, state: str, reason: str | None, usage: accounting.Usage,
                occ: dict) -> _ctl.Ended:
        """One ``BEGIN IMMEDIATE``: the end under the epoch the row holds now (a revoke has raised it past the launch's,
        which the fence already refused), so a row that ended first is not found and nothing is written twice."""
        with self._store.write() as conn:
            row = conn.execute("SELECT epoch FROM agent_occurrences WHERE occurrence_id = ? AND state = 'launched'",
                               (occurrence_id,)).fetchone()
            if row is None:
                return _ctl.Ended(False)
            return _ctl.settle_terminal(conn, occurrence_id, row[0], state,
                                        reason or self._stale_reason(conn, occ, row[0]), usage, self._clock())

    # --- the run's natural end: the hook points --------------------------------------------------------------------

    def finish(self, occurrence_id: str, executor: accounting.Executor, *, ok: bool = True,
               reason: str | None = None) -> End:
        """End a run that finished on its own: ``verify`` may turn a ``done`` into ``failed``, the settle writes the end
        under the epoch the row holds, and ``on_end`` runs after the commit. ``ok=False`` ends ``failed`` with
        ``reason`` (default ``run_failed``) and still runs both hooks. The caller says the executor's run is over; a
        row that already ended gets no second settle."""
        occ = self._occurrence(occurrence_id)
        usage = _executor_usage(executor)
        state, why = ("done", "completed") if ok else ("failed", reason or "run_failed")
        if self._hooks.verify is not None:
            try:
                verdict = self._hooks.verify(End(occurrence_id, state, why, usage))
            except Exception as exc:  # noqa: BLE001 - a verifier that cannot answer has not verified
                _log.warning("agent supervisor: the verify hook raised %s", type(exc).__name__)
                verdict = "verify_error"
            if verdict is not None and state == "done":
                state, why = "failed", verdict if type(verdict) is str and verdict else "verify_error"
        ended = self._settle(occurrence_id, state, why, usage, occ)
        end = End(occurrence_id, ended.state or state, ended.reason or why, usage, ended)
        if ended.ended and self._hooks.on_end is not None:
            try:
                self._hooks.on_end(end)
            except Exception as exc:  # noqa: BLE001 - the end is committed; a gauge that fails undoes nothing
                _log.warning("agent supervisor: the on_end hook raised %s", type(exc).__name__)
        return end

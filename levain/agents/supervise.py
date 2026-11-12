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
  :meth:`Supervisor.finish`, the run's natural end. ``on_end`` defaults to nothing; ``verify`` defaults to P8b's win verifier
  (:func:`levain.agents.gauges.win_verifier`).

The tick side is :meth:`Supervisor.reap` (P6-4b), the second writer of this module, called from
:func:`levain.agents.controller.tick`: lease renewal first (a dead-fire session row's waits for its own stop, P7-1
S3), the ``stop_at`` pass from ``autonomic.db`` and the clock alone, then the shared cover refresh and the STALE
classification, then ``stop(STOP_DEADLINE_S)`` serially, each end through the guarded settle and only on
``StopResult(terminated=True)``. A stop's outcome is read only as that exact ``StopResult`` (the executor applies
:func:`levain.agents.controller.gate_proves_termination` to its own child; the result carries no returncode for the
reaper to judge). Not covered: a row with neither ``session_key`` nor ``pid`` is
asked nothing (lapse recovery's), and a dead supervisor is lapse recovery's too.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from levain.agent_authority import AgentAuthority, AgentAuthorityError, agent_fence
from levain.agents import accounting, controller as _ctl, gauges as _gauges, role as _role, spec as _spec
from levain.agents.store import AgentStore
from levain.autonomic.journal import EffectOutcome, EffectStatus, RunJournal

__all__ = ["MAX_TRIES", "LEASE_S", "STOP_DEADLINE_S", "STOP_BUDGET_S", "ATTENTION_PREFIX", "Effected", "End", "Hooks",
           "HOOKS", "Reaped", "Supervisor"]

_log = logging.getLogger("levain.agents.supervise")

MAX_TRIES = 3          # A0 §2.3: the effect is tried at most this many times in one pass
STOP_DEADLINE_S = 10   # A0 r6 §2.3 [judged by A0]: what a stop() may wait
LEASE_S = _ctl.LEASE_S  # A0 r6 §2.3 [judged by A0], 900: defined once, in the controller, which the heartbeat shares
# [judged, L3 r1]: the stop phase's wall time, from just after the renewals; no stop() is begun past it, so a stop
# begun inside it and honouring its deadline ends inside the lease the renewals just wrote
STOP_BUDGET_S = LEASE_S // 2
ATTENTION_PREFIX = "agent-stop-unconfirmed:"
STOP_AT_REASON = "stop_at"

_OCCURRENCE_COLUMNS = ("occurrence_id", "agent_id", "run_id", "state", "epoch", "launch_epoch", "link_id",
                       "link_incarnation", "spec_hash", "binding_id")


@dataclass
class Reaped:
    """What one :meth:`Supervisor.reap` pass did, by occurrence id. ``renewed`` are the leases it extended, ``due`` the
    rows past ``stop_at``, ``stale`` the rows whose fence moved, ``unclassified`` the rows whose cover could not be
    read this tick (nothing done for them), ``asked`` the rows ``stop()`` was called for, ``ended`` those it settled,
    ``unconfirmed`` those left ``launched`` after an unconfirmed stop (or with no executor to ask), ``settle_errors`` a
    settle that raised, or wrote nothing while the row stayed ``launched`` (``not_settled``), after a confirmed stop
    (the next pass asks again), ``deferred`` the rows to stop that were not asked because the pass was past
    :data:`STOP_BUDGET_S` (the next pass asks them first), ``frozen`` the session rows whose stop this pass proved the
    fire dead and whose end then failed to confirm: their lease was not renewed, so it lapses to takeover (P7-1 S3)."""
    renewed: list[str] = field(default_factory=list)
    due: list[str] = field(default_factory=list)
    stale: list[str] = field(default_factory=list)
    unclassified: list[str] = field(default_factory=list)
    asked: list[str] = field(default_factory=list)
    ended: list[str] = field(default_factory=list)
    unconfirmed: list[str] = field(default_factory=list)
    settle_errors: dict[str, str] = field(default_factory=dict)
    deferred: list[str] = field(default_factory=list)
    frozen: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class Effected:
    """What one :meth:`Supervisor.effect` did.

    ``outcome`` is the journal's last answer and ``tries`` how many times it was asked. ``waits`` is True when the cover
    stayed unreadable through the tries (the occurrence is still ``launched`` and the next pass retries). On a STALE,
    ``stop`` is the executor's answer (None when the row had already ended and nothing was asked), and ``ended`` what
    the settle wrote (None when ``stop`` did not confirm, or when the settle raised: nothing was written).
    ``settle_error`` names the exception of a settle that raised after a confirmed stop; the row is then still
    ``launched`` with its slot and reservation, for the tick side's reaper."""
    outcome: EffectOutcome
    tries: int
    waits: bool = False
    stop: accounting.StopResult | None = None
    ended: _ctl.Ended | None = None
    settle_error: str | None = None

    @property
    def stale(self) -> bool:
        return self.outcome.status is EffectStatus.STALE


@dataclass(frozen=True)
class End:
    """What an ended occurrence tells the hooks: the state and reason the settle wrote (``verify`` receives the state
    and reason the run asked for, before the settle), the executor's reading and the settle's result. When this
    finish wrote nothing (``ended.ended`` False), ``state`` and ``reason`` are the row's as re-read after the settle
    (a terminal row's end, or ``launched`` and None for a run a bar fenced), never the ones asked for."""
    occurrence_id: str
    state: str
    reason: str | None
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
                 journal: RunJournal | None = None, session_grace_s: int | None = _ctl.SESSION_GRACE_S) -> None:
        self._store = store
        self._authority = authority
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._hooks = HOOKS if hooks is None else hooks
        self._win_verify = _gauges.win_verifier(store, clock=clock)   # P8b: the verify hook's default
        self._stop_deadline_s = stop_deadline_s
        self._journal = journal if journal is not None else RunJournal(store.directory)
        with store.read() as conn:
            self._store_id = _ctl._store_id_of(conn)
        self._lock = threading.Lock()
        self._refreshed: bool | None = None   # this tick's one refresh: None not yet, True read, False failed
        self._session_grace_s = session_grace_s  # what a row whose role is not read as a dream (sessionless) passes
        self._raised: set[str] = set()        # attention keys this process raised and has not resolved
        self._deferred: set[str] = set()      # rows the last pass left unasked past STOP_BUDGET_S

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
        try:
            ended = self._settle_stopped(occ, _executor_usage(executor))
        except Exception as exc:  # noqa: BLE001 - the stop is confirmed; the reaper settles the row it left launched
            _log.warning("agent supervisor: the settle after a confirmed stop of %s raised %s: %s; the row stays"
                         " launched for the reaper", occ["occurrence_id"], type(exc).__name__, exc)
            return Effected(outcome, tries, stop=stop, settle_error=type(exc).__name__)
        return Effected(outcome, tries, stop=stop, ended=ended)

    def _stale_reason(self, conn: sqlite3.Connection, occ: dict, epoch: int) -> str:
        """The reason nameable from the current side. Every difference only narrows, so no cause is needed to end."""
        if conn.execute("SELECT 1 FROM agent_bars WHERE agent_id = ? AND closed_at IS NULL",
                        (occ["agent_id"],)).fetchone() is not None:
            return "barred"
        if epoch != occ["launch_epoch"]:
            return "fenced"
        return "link_not_live"

    def _settle_stopped(self, occ: dict, usage: accounting.Usage, reason: str | None = None) -> _ctl.Ended:
        """The STALE path's settle, and the only one that adopts the epoch the row holds NOW. It runs only after
        ``stop()`` answered ``StopResult(terminated=True)``: the process is gone, so the end it writes is the
        revocation's own (``stopped``), and a revoke has usually raised the epoch past the launch's, which is why
        the launch epoch would not find the row. One ``BEGIN IMMEDIATE``; a row that ended first is not found and
        nothing is written twice."""
        occurrence_id = occ["occurrence_id"]
        with self._store.write() as conn:
            row = conn.execute("SELECT epoch FROM agent_occurrences WHERE occurrence_id = ? AND state = 'launched'",
                               (occurrence_id,)).fetchone()
            if row is None:
                return _ctl.Ended(False)
            return _ctl.settle_terminal(conn, occurrence_id, row[0], "stopped",
                                        reason or self._stale_reason(conn, occ, row[0]), usage, self._clock())

    def _settle_held(self, occurrence_id: str, epoch: int, state: str, reason: str,
                     usage: accounting.Usage) -> _ctl.Ended:
        """The fire's own end, under the epoch the fire launched with: a bar or takeover that raised the epoch since
        fences this write (``Ended(False)``, nothing written), so a natural completion never outruns a revocation."""
        with self._store.write() as conn:
            return _ctl.settle_terminal(conn, occurrence_id, epoch, state, reason, usage, self._clock())

    def _held(self, occurrence_id: str, epoch: object) -> bool:
        with self._store.read() as conn:
            return conn.execute("SELECT 1 FROM agent_occurrences WHERE occurrence_id = ? AND state = 'launched'"
                                " AND epoch = ?", (occurrence_id, epoch)).fetchone() is not None

    def _as_stands(self, occurrence_id: str, usage: accounting.Usage) -> End:
        """A finish that wrote nothing: the row's state and reason as they stand, never the ones asked for."""
        with self._store.read() as conn:
            state, reason = conn.execute("SELECT state, reason FROM agent_occurrences WHERE occurrence_id = ?",
                                         (occurrence_id,)).fetchone()
        return End(occurrence_id, state, reason, usage, _ctl.Ended(False))

    # --- the tick side: the reaper (P6-4b) -------------------------------------------------------------------------

    def _launched(self) -> list[dict]:
        columns = _OCCURRENCE_COLUMNS + ("launched_at", "session_key", "pid")
        with self._store.read() as conn:
            rows = conn.execute(f"SELECT {', '.join(columns)} FROM agent_occurrences WHERE state = 'launched'"
                                " ORDER BY launched_at, occurrence_id").fetchall()
        return [dict(zip(columns, row)) for row in rows]

    def _session_grace(self, row: dict, seen: dict) -> int | None:
        """The heartbeat's ``session_grace_s`` for this row, from its role's executor: None only for a role read as
        ``dream`` (a sessionless run); a session role, or one that cannot be read (an unregistered spawned worker's,
        a corrupt row), keeps the bound. ``seen`` caches it by ``spec_hash`` for one pass."""
        digest = row["spec_hash"]
        if digest not in seen:
            bound = self._session_grace_s if self._session_grace_s is not None else _ctl.SESSION_GRACE_S
            try:
                with self._store.read() as conn:
                    found = conn.execute("SELECT spec FROM agent_specs WHERE spec_hash = ?", (digest,)).fetchone()
                # the stored text must still hash to its spec_hash, canonical form and all, before its role is trusted
                # (the check spec._check_row makes); the role lookup is its own read, outside the one above
                spec = _spec.parse_spec_text(found[0]) if found else None
                trusted = (spec is not None and _spec.spec_hash(row["agent_id"], spec) == digest
                           and _spec._canonical(spec) == found[0])
                role = _role.role_by_hash(self._store, spec["role"]["role_hash"]) if trusted else None
                dream = role is not None and role["executor"] == "dream"
            except Exception:  # noqa: BLE001 - an unreadable role is not evidence of a sessionless run
                dream = False
            seen[digest] = None if dream else bound
        return seen[digest]

    def _renew(self, row: dict, session_grace_s: int | None) -> bool:
        try:
            with self._store.write() as conn:
                # a sessionless (dream) row is renewed only once a fire claimed it: an unclaimed one keeps its launch
                # lease and lapses to controller.take_over_unclaimed (D1-Q fix round)
                return _ctl.heartbeat(conn, row["occurrence_id"], row["epoch"], self._clock(), LEASE_S,
                                      session_grace_s=session_grace_s, unclaimed_lapses=session_grace_s is None)
        except Exception as exc:  # noqa: BLE001 - one row's renewal failing never stops the others'
            _log.warning("agent supervisor: the lease renewal of %s raised %s: %s", row["occurrence_id"],
                         type(exc).__name__, exc)
            return False

    def _past_stop_at(self, row: dict, now: datetime) -> bool:
        """From ``autonomic.db`` and the clock alone: ``launched_at + stop_at`` (the spec whose hash is the
        occurrence's) is at or before ``now``. A spec or stamp that cannot be read is not evidence of a deadline."""
        launched = _ctl._lease(row["launched_at"])
        if launched is None:
            return False
        try:
            with self._store.read() as conn:
                found = conn.execute("SELECT spec FROM agent_specs WHERE spec_hash = ?", (row["spec_hash"],)).fetchone()
            stop_at = json.loads(found[0])["stop_at"] if found else None
        except Exception:  # noqa: BLE001
            return False
        if type(stop_at) is not int or stop_at < 0:
            return False
        return now >= launched + timedelta(seconds=stop_at)

    def _is_stale(self, row: dict) -> bool | None:
        """True when the fence an effect would build differs from the one now; None when the cover cannot be read."""
        try:
            fence, fence_now = agent_fence(row, authority=self._authority, store_id=self._store_id, now=self._clock)
            with self._store.read() as conn:
                return fence_now(conn) != fence
        except Exception:  # noqa: BLE001 - AgentCoverMoved, AgentAuthorityError, a store error: all "cannot tell"
            return None

    def reap(self, executor_of: Callable[[dict], accounting.Executor | None], *,
             attention: Callable[[str, str], None] | None = None,
             resolve: Callable[[str], None] | None = None) -> Reaped:
        """One tick's reaper (A0 r6 §2.3, §5.10), in this order: (1) renew the lease of every ``launched`` row, except
        a session row whose executor proves its fire dead (P7-1 S3): that renewal is withheld until the row's own stop
        has answered in this pass, given back when the row is not one to stop, is deferred, or its stop failed before
        the end was asked, and kept back (``frozen``) only when the fire was proven dead and the end then failed to
        confirm; nothing about it is remembered between passes;
        (2) pick the rows past ``stop_at``, from ``autonomic.db`` and the clock alone, so it holds while the cover is
        unreadable; (3) the shared refresh and the STALE classification (UNCLASSIFIED does nothing); (4) ``stop()``
        for each row to stop, serially, and the guarded settle only on ``StopResult(terminated=True)``.

        ``executor_of(row)`` gives the executor for a ``launched`` row (a mapping with its ``occurrence_id``,
        ``session_key`` and ``pid``); a row with neither key is never asked. ``attention(key, text)`` upserts on its key
        and is called once per key per process; ``resolve(key)`` is called, idempotently, when the row is no longer
        ``launched`` after the attempt, and again on a later pass for a key this process raised whose row has since
        left ``launched`` by any path, or whose resolve raised. No ``stop()`` is begun once the pass is
        :data:`STOP_BUDGET_S` past the renewals; those rows are ``deferred`` and asked first on the next pass."""
        self.begin_tick()
        out = Reaped()
        rows = self._launched()
        graces: dict = {}
        withheld: set[str] = set()   # P7-1 S3: session rows whose fire is proven dead; renewed after their stop answers
        for row in rows:
            if self._fire_proven_dead(row, executor_of):
                withheld.add(row["occurrence_id"])
                continue
            self._renew_into(row, graces, out)
        now = started = self._clock()
        reasons: dict[str, str] = {}
        for row in rows:
            if self._past_stop_at(row, now):
                out.due.append(row["occurrence_id"])
                reasons[row["occurrence_id"]] = STOP_AT_REASON
        if self.refresh_once():
            for row in rows:
                stale = self._is_stale(row)
                if stale is None:
                    out.unclassified.append(row["occurrence_id"])
                elif stale:
                    out.stale.append(row["occurrence_id"])
        else:
            out.unclassified.extend(r["occurrence_id"] for r in rows)
        wanted = set(out.due) | set(out.stale)
        for row in rows:                  # a withheld row not to be stopped this pass is renewed now, as before S3
            if row["occurrence_id"] in withheld and row["occurrence_id"] not in wanted:
                self._renew_into(row, graces, out)
        # stable sort, launched order within each group: last pass's deferred rows first, then the withheld ones
        for row in sorted(rows, key=lambda r: (r["occurrence_id"] not in self._deferred,
                                               r["occurrence_id"] not in withheld)):
            oid = row["occurrence_id"]
            if oid not in wanted or (row["session_key"] is None and row["pid"] is None):
                continue
            if self._clock() - started >= timedelta(seconds=STOP_BUDGET_S):
                out.deferred.append(oid)
                if oid in withheld:       # nothing was asked: its renewal is given back
                    self._renew_into(row, graces, out)
                continue
            unproven = self._stop_and_settle(row, reasons.get(oid), executor_of, attention, resolve, out)
            if oid in withheld:
                if unproven:
                    out.frozen.append(oid)            # fire proven dead, end unconfirmed: the lease lapses to takeover
                elif oid not in out.ended:
                    self._renew_into(row, graces, out)
        self._deferred = set(out.deferred)
        live = {r["occurrence_id"] for r in rows}
        for key in sorted(self._raised):          # raised here, and its row left launched since (or a resolve raised)
            if key[len(ATTENTION_PREFIX):] not in live:
                self._resolve(key, resolve)
        return out

    def _resolve(self, key: str, resolve) -> None:
        """Resolve ``key``, keeping it for the next pass until a resolve succeeds."""
        if resolve is None:
            self._raised.discard(key)
            return
        try:
            resolve(key)
        except Exception as exc:  # noqa: BLE001 - kept: the next pass tries again
            self._raised.add(key)
            _log.warning("agent supervisor: resolving %s raised %s", key, type(exc).__name__)
            return
        self._raised.discard(key)

    def _renew_into(self, row: dict, graces: dict, out: Reaped) -> None:
        if self._renew(row, self._session_grace(row, graces)):
            out.renewed.append(row["occurrence_id"])

    def _fire_proven_dead(self, row: dict, executor_of) -> bool:
        """P7-1 S3: True only for a session row (it has a key) whose executor offers ``fire_proven_dead`` and answers it
        exactly True (:class:`levain.agents.session.RowSessionExecutor`). Read again on every pass from what that
        executor reads (the store, the fire's identity record, the process table); nothing is remembered here, so a
        Supervisor built per tick or a restarted reaper decides the same. Any failure is False: the row is renewed."""
        if row["session_key"] is None:
            return False
        try:
            probe = getattr(executor_of(row), "fire_proven_dead", None)
            return callable(probe) and probe(self._stop_deadline_s) is True
        except Exception:  # noqa: BLE001 - a probe that fails proved nothing
            return False

    def _stop_and_settle(self, row: dict, reason: str | None, executor_of, attention, resolve, out: Reaped) -> bool:
        """Ask one row's stop and settle it on a confirmed one. True only when the executor says that stop PROVED the
        fire dead and the end then failed to confirm (``end_unproven is True``); the reaper leaves only such a row's
        lease unrenewed."""
        oid = row["occurrence_id"]
        key = ATTENTION_PREFIX + oid
        try:
            executor = executor_of(row)
        except Exception as exc:  # noqa: BLE001
            _log.warning("agent supervisor: no executor for %s (%s)", oid, type(exc).__name__)
            executor = None
        if executor is None:
            stop = accounting.StopResult(False, "no executor to ask")
        else:
            stop = _executor_stop(executor, self._stop_deadline_s)
            out.asked.append(oid)
        if stop.terminated is not True:
            out.unconfirmed.append(oid)
            if attention is not None and key not in self._raised:
                self._raised.add(key)
                try:
                    attention(key, f"occurrence {oid}: asked to stop; not confirmed ended ({stop.detail})")
                except Exception as exc:  # noqa: BLE001 - a missing row is retried next tick
                    self._raised.discard(key)
                    _log.warning("agent supervisor: the attention row for %s raised %s", oid, type(exc).__name__)
            return executor is not None and getattr(executor, "end_unproven", None) is True
        try:
            occ = self._occurrence(oid)
            usage = _executor_usage(executor)
            ended = self._settle_stopped(occ, usage, reason)
        except Exception as exc:  # noqa: BLE001 - confirmed, but the row stays launched; the next pass asks again
            _log.warning("agent supervisor: the settle after a confirmed stop of %s raised %s: %s; the row stays"
                         " launched", oid, type(exc).__name__, exc)
            out.settle_errors[oid] = type(exc).__name__
            return False
        with self._store.read() as conn:
            still = conn.execute("SELECT 1 FROM agent_occurrences WHERE occurrence_id = ? AND state = 'launched'",
                                 (oid,)).fetchone()
        if still is not None:          # confirmed, yet the guarded settle wrote nothing: named, asked again next pass
            out.settle_errors[oid] = "not_settled"
            return False
        if ended.ended:
            out.ended.append(oid)
        self._resolve(key, resolve)    # ended by this settle or by another path: the attention row is over either way
        return False

    # --- the run's natural end: the hook points --------------------------------------------------------------------

    def finish(self, occurrence_id: str, executor: accounting.Executor, *, ok: bool = True,
               reason: str | None = None) -> End:
        """End a run that finished on its own: ``verify`` may turn a ``done`` into ``failed``, the settle writes the end
        under the epoch the fire launched with, and ``on_end`` runs after the commit. ``ok=False`` ends ``failed`` with
        ``reason`` (default ``run_failed``; 'completed' is done's alone, so it is ``run_failed`` too) and still runs
        both hooks. The caller says the executor's run is over. A row that already ended, or that a bar or takeover
        fenced since the launch, gets no settle and no hooks, and the End it returns is the row as it stands."""
        occ = self._occurrence(occurrence_id)
        epoch = occ["launch_epoch"]
        usage = _executor_usage(executor)
        if not self._held(occurrence_id, epoch):      # verify may have effects: it never runs for a run already over
            return self._as_stands(occurrence_id, usage)
        if ok:
            state, why = "done", "completed"
        else:
            state, why = "failed", reason if type(reason) is str and reason and reason != "completed" else "run_failed"
        verify = self._hooks.verify or self._win_verify
        if verify is not None:
            try:
                verdict = verify(End(occurrence_id, state, why, usage))
            except Exception as exc:  # noqa: BLE001 - a verifier that cannot answer has not verified
                _log.warning("agent supervisor: the verify hook raised %s", type(exc).__name__)
                verdict = "verify_error"
            if verdict is not None and state == "done":
                good = type(verdict) is str and verdict and verdict != "completed"
                state, why = "failed", verdict if good else "verify_error"
        ended = self._settle_held(occurrence_id, epoch, state, why, usage)
        if not ended.ended:
            return self._as_stands(occurrence_id, usage)
        end = End(occurrence_id, ended.state, ended.reason, usage, ended)
        if ended.ended and self._hooks.on_end is not None:
            try:
                self._hooks.on_end(end)
            except Exception as exc:  # noqa: BLE001 - the end is committed; a gauge that fails undoes nothing
                _log.warning("agent supervisor: the on_end hook raised %s", type(exc).__name__)
        return end

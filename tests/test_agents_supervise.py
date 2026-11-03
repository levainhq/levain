"""P6-4b: the run supervisor, tick side (A0 r6 §2.3, §5.10; BUILD_PLAN P6-4b Done cases 1-8).

Every store is a real ``/2`` agent store with real launched occurrences and a real authority log, under a fake clock.
The executors are stubs that return a ``StopResult`` and signal nothing.
"""
from __future__ import annotations

import shutil
from datetime import timedelta

import pytest

from levain.agents import controller as ctl
from levain.agents import supervise
from levain.agents.accounting import StopResult, Usage
from levain.autonomic.journal import RunJournal

from tests.test_agents_launch import Rig
from tests.test_agents_supervise_fire import Flaky, _fire, _second_fire
from tests.test_authority_agent_link import revoke_agent

pytestmark = pytest.mark.skipif(shutil.which("ssh-keygen") is None, reason="needs ssh-keygen")


class Ex:
    """A stub executor: counts stop() asks, answers ``terminated``, signals nothing. ``on_stop`` runs inside stop()."""
    max_exposure_usd_micros = 100_000
    action_exposure = None

    def __init__(self, terminated=True, on_stop=None):
        self.terminated, self.on_stop, self.stops, self.deadlines = terminated, on_stop, 0, []

    def usage(self):
        return Usage(5_000, None, None, "test")

    def stop(self, deadline_s):
        self.stops += 1
        self.deadlines.append(deadline_s)
        if self.on_stop:
            self.on_stop()
        return StopResult(self.terminated, "stub")


class Attention:
    """An upserting attention store keyed like the cockpit's: one row per key."""

    def __init__(self):
        self.rows, self.calls, self.resolved = {}, 0, []

    def upsert(self, key, text):
        self.calls += 1
        self.rows[key] = text

    def resolve(self, key):
        self.resolved.append(key)
        self.rows.pop(key, None)


class Clock:
    def __init__(self, rig):
        self.rig, self.later = rig, timedelta(0)

    def __call__(self):
        return self.rig.now(self.later)

    def advance(self, seconds):
        self.later += timedelta(seconds=seconds)


@pytest.fixture
def rig(tmp_path) -> Rig:
    r = Rig(tmp_path)
    r.link()
    return r


def _sup(rig, clock, authority=None, **kw):
    journal = RunJournal(rig.store.directory)
    return supervise.Supervisor(rig.store, authority=authority or rig.authority, clock=clock, journal=journal,
                                **kw), journal


def _claimed(rig, oid, n=1, session=True):
    """A fire that has claimed its row (its pid) and opened its session (its key), as P6-5 and P7-1 would."""
    with rig.store.write() as c:
        assert ctl.claim_fire(c, oid, 4000 + n, rig.now()) is not None
        if session:
            assert ctl.record_session_key(c, oid, rig.row(oid)["launch_epoch"], f"s-{n:016x}", rig.now())


def _lease(rig, oid):
    return rig.row(oid)["lease_until"]


def _revoke(rig):
    rig.world.link(revoke_agent(rig.agent, rig.sid), "laptop")


def _reap(sup, ex, att=None, **kw):
    att = att or Attention()
    return sup.reap(lambda row: ex, attention=att.upsert, resolve=att.resolve, **kw), att


# --- 1: a revoke mid-run ends stopped only after terminated=True --------------------------------------------------

def test_a_revoke_mid_run_ends_stopped_only_after_a_confirmed_stop(rig):
    # behaviour: the run makes no further effect; the next tick whose cover reads asks stop(STOP_DEADLINE_S) and
    # writes stopped/link_not_live, frees the slot and settles the reservation, only on terminated=True
    clock = Clock(rig)
    sup, journal = _sup(rig, clock)
    oid = _fire(rig, "a", journal)
    _claimed(rig, oid)
    ex = Ex(True)
    quiet, _ = _reap(sup, ex)                      # nothing revoked: a healthy row is neither asked nor ended
    assert ex.stops == 0 and quiet.ended == [] and rig.row(oid)["state"] == "launched"
    _revoke(rig)
    clock.advance(60)
    done, att = _reap(sup, ex)
    row = rig.row(oid)
    assert ex.stops == 1 and ex.deadlines == [supervise.STOP_DEADLINE_S] == [10]
    assert done.ended == [oid] and (row["state"], row["reason"]) == ("stopped", "link_not_live")
    assert rig.taken(oid) == ([], [("settled",)]) and att.rows == {}


# --- 2: a stop that never confirms ---------------------------------------------------------------------------------

def test_a_never_confirming_stop_stays_launched_holds_the_slot_renews_the_lease_and_raises_one_attention_row(rig):
    # behaviour: terminated=False leaves the row launched with slot and reservation, the lease renewed every tick,
    # stop() asked again each tick, and exactly one attention row naming the occurrence
    clock = Clock(rig)
    sup, journal = _sup(rig, clock)
    oid = _fire(rig, "a", journal)
    _claimed(rig, oid)
    _revoke(rig)
    ex, att = Ex(False), Attention()
    leases = []
    for _ in range(3):
        clock.advance(400)       # past the first lease: only a renewal each tick keeps the row from lapsing
        out, _ = _reap(sup, ex, att)
        assert out.unconfirmed == [oid] and out.ended == []
        leases.append(_lease(rig, oid))
    assert leases == sorted(set(leases)) and len(leases) == 3     # moved forward every tick
    assert rig.row(oid)["state"] == "launched" and rig.taken(oid) == ([(1,)], [("open",)])
    assert ex.stops == 3
    assert list(att.rows) == [supervise.ATTENTION_PREFIX + oid] and att.calls == 1 and oid in att.rows[
        supervise.ATTENTION_PREFIX + oid]


# --- 3: cockpit.db locked ------------------------------------------------------------------------------------------

def test_a_locked_cover_leaves_a_revoked_row_launched_that_tick_and_asks_nothing(rig):
    # behaviour: an unreadable cover (UNCLASSIFIED) does nothing that tick; the next tick that reads stops it
    clock = Clock(rig)
    flaky = Flaky(rig.authority, fail=10)
    sup, journal = _sup(rig, clock, flaky)
    oid = _fire(rig, "a", journal)
    _claimed(rig, oid)
    _revoke(rig)
    ex = Ex(True)
    locked, _ = _reap(sup, ex)
    assert locked.unclassified == [oid] and ex.stops == 0 and rig.row(oid)["state"] == "launched"
    flaky.fail = 0
    clock.advance(60)
    again, _ = _reap(sup, ex)
    assert again.ended == [oid] and ex.stops == 1 and rig.row(oid)["state"] == "stopped"


def test_a_refresh_that_fails_is_unclassified_for_every_row(rig):
    # behaviour: the shared refresh raising (cockpit.db locked, AnchorError) stops nothing
    class Broken(Flaky):
        def refresh(self):
            raise OSError("locked")

    clock = Clock(rig)
    sup, journal = _sup(rig, clock, Broken(rig.authority))
    oid = _fire(rig, "a", journal)
    _claimed(rig, oid)
    _revoke(rig)
    ex = Ex(True)
    out, _ = _reap(sup, ex)
    assert out.unclassified == [oid] and ex.stops == 0 and rig.row(oid)["state"] == "launched"


# --- 4: a row no fire claimed --------------------------------------------------------------------------------------

def test_a_launched_row_no_fire_claimed_gets_no_stop(rig):
    # behaviour: neither session_key nor pid means no executor is asked, even for a STALE row; lapse recovery
    # settles it
    clock = Clock(rig)
    sup, journal = _sup(rig, clock)
    oid = _fire(rig, "a", journal)
    _revoke(rig)
    ex = Ex(True)
    asked = []
    out = sup.reap(lambda row: asked.append(row) or ex)
    assert out.stale == [oid] and out.asked == [] and ex.stops == 0 and asked == []
    assert rig.row(oid)["state"] == "launched"
    clock.advance(ctl.LEASE_S + 5)           # the lease lapses (no session by the grace: not renewed) -> takeover
    with rig.store.write() as c:
        assert ctl.takeover(c, oid, clock())
    assert (rig.row(oid)["state"], rig.row(oid)["reason"]) == ("stopped", "crash_recovered")


# --- 5: past stop_at with the cover locked -------------------------------------------------------------------------

def test_a_row_past_stop_at_is_asked_to_stop_that_tick_with_the_cover_locked(rig):
    # behaviour: stop_at is read from autonomic.db and the clock before the cover read, so an unreadable cover does
    # not hold the deadline off; the end carries reason stop_at
    clock = Clock(rig)
    flaky = Flaky(rig.authority, fail=10_000)
    sup, journal = _sup(rig, clock, flaky)
    oid = _fire(rig, "a", journal)
    _claimed(rig, oid)
    ex = Ex(True)
    for _ in range(5):                        # 5 x 600 s: renewed on the way; the spec's stop_at is 3600 s
        clock.advance(600)
        out, _ = _reap(sup, ex)
        assert out.due == [] and ex.stops == 0
    clock.advance(600)
    out, _ = _reap(sup, ex)
    assert out.due == [oid] and out.unclassified == [oid] and ex.stops == 1 and out.ended == [oid]
    assert (rig.row(oid)["state"], rig.row(oid)["reason"]) == ("stopped", "stop_at")
    assert rig.taken(oid) == ([], [("settled",)])


# --- 6: a row that ended on its own --------------------------------------------------------------------------------

def test_a_row_that_ended_on_its_own_is_not_settled_twice(rig):
    # behaviour: the run finishes while stop() is in flight; the guarded settle finds no launched row and writes
    # nothing, so the natural end stands
    clock = Clock(rig)
    sup, journal = _sup(rig, clock)
    oid = _fire(rig, "a", journal)
    _claimed(rig, oid)
    _revoke(rig)
    ex = Ex(True, on_stop=lambda: rig.finish(oid))
    att = Attention()
    out, _ = _reap(sup, ex, att)
    row = rig.row(oid)
    assert ex.stops == 1 and (row["state"], row["reason"]) == ("done", "completed")
    assert out.settle_errors == {} and rig.taken(oid) == ([], [("settled",)])
    assert out.ended == []                                          # the reaper settled nothing: the end is the run's
    assert att.resolved == [supervise.ATTENTION_PREFIX + oid]       # the attention key is cleared either way
    again, _ = _reap(sup, ex, att)
    assert ex.stops == 1 and again.asked == []


# --- 7: a slow stop does not let a healthy row lapse ---------------------------------------------------------------

def test_a_slow_stop_does_not_let_a_healthy_rows_lease_lapse(rig):
    # behaviour: leases are renewed first, so a healthy row that sorts after a row whose stop() stalls has its lease
    # extended before the stall, however long the stall runs
    clock = Clock(rig)
    sup, journal = _sup(rig, clock)
    first = _fire(rig, "a", journal)
    healthy = _second_fire(rig, journal)
    _claimed(rig, first, 1)
    _claimed(rig, healthy, 2)
    clock.advance(700)                        # both leases (900 s) are close to the end (a world record ticks 60 s)
    before = _lease(rig, healthy)

    def stall():
        clock.advance(3 * ctl.LEASE_S)       # a stop() that takes far longer than a lease

    # first's agent is revoked; the healthy one belongs to a second agent
    _revoke(rig)
    renewed_at = clock()                      # the revoke is a world record, which moves the clock
    ex = Ex(False, on_stop=stall)
    out = sup.reap(lambda row: ex)
    assert set(out.renewed) == {first, healthy}
    assert _lease(rig, healthy) > before
    # renewed at the clock reading before the stall (a renewal after it would read 3 leases later)
    assert _lease(rig, healthy) == ctl._stamp(ctl._expiry(renewed_at, ctl.LEASE_S))
    assert rig.row(healthy)["state"] == "launched"
    with rig.store.write() as c:             # at the clock the stall ended on, the renewed lease had been extended
        assert ctl.takeover(c, healthy, clock() - timedelta(seconds=3 * ctl.LEASE_S - 100)) is False


# --- 8: one attention row per key across ticks and processes ---------------------------------------------------

def test_one_attention_row_per_key_across_ticks(rig):
    # behaviour: the same unconfirmed occurrence over five ticks calls attention once; a fresh supervisor (a new
    # process) calls it again, which an upserting store folds into the same row; the settle resolves the key
    clock = Clock(rig)
    sup, journal = _sup(rig, clock)
    oid = _fire(rig, "a", journal)
    _claimed(rig, oid)
    _revoke(rig)
    att = Attention()
    for _ in range(5):
        clock.advance(100)
        _reap(sup, Ex(False), att)
    assert att.calls == 1 and len(att.rows) == 1
    sup2, _ = _sup(rig, clock)
    clock.advance(100)
    _reap(sup2, Ex(False), att)
    assert att.calls == 2 and len(att.rows) == 1
    clock.advance(100)
    done, _ = _reap(sup2, Ex(True), att)
    assert done.ended == [oid] and att.rows == {} and att.resolved == [supervise.ATTENTION_PREFIX + oid]


# --- the rest of the contract ---------------------------------------------------------------------------------------

def test_constants_and_reading_a_stop_only_through_a_stopresult(rig):
    # behaviour: LEASE_S and STOP_DEADLINE_S have one home each; a stop() that answers a truthy non-True, raises or
    # returns something that is no StopResult confirms nothing
    assert (supervise.LEASE_S, supervise.STOP_DEADLINE_S) == (900, 10) and supervise.LEASE_S is ctl.LEASE_S
    clock = Clock(rig)
    sup, journal = _sup(rig, clock)
    oid = _fire(rig, "a", journal)
    _claimed(rig, oid)
    _revoke(rig)

    class Truthy(Ex):
        def stop(self, deadline_s):
            return StopResult(1, "x")      # type: ignore[arg-type]

    class Raises(Ex):
        def stop(self, deadline_s):
            raise RuntimeError("no")

    class Bare(Ex):
        def stop(self, deadline_s):
            return True                    # type: ignore[return-value]

    for ex in (Truthy(), Raises(), Bare()):
        clock.advance(10)
        out, _ = _reap(sup, ex)
        assert out.unconfirmed == [oid] and rig.row(oid)["state"] == "launched"


def test_a_tick_runs_the_reaper_first_and_survives_a_reaper_that_raises(rig):
    # behaviour: controller.tick calls the reaper before anything else; its failure is recorded, never fatal
    clock = Clock(rig)
    seen = []
    out = ctl.tick(rig.store, clock, lambda a, d: None, reaper=lambda: seen.append("reaped") or "R")
    assert seen == ["reaped"] and out.reaped == "R" and out.reaper_error is None

    def boom():
        raise RuntimeError("x")

    out = ctl.tick(rig.store, clock, lambda a, d: None, reaper=boom)
    assert out.reaper_error == "RuntimeError"


# --- r1 review fixes (L3_p64b_r1) -----------------------------------------------------------------------------------

def _revoke_also(rig, oid):
    rig.world.link(revoke_agent(rig.row(oid)["agent_id"], rig.sid), "laptop")


def test_the_stop_phase_stops_asking_past_its_budget_and_a_deferred_row_goes_first_next_pass(rig):
    # behaviour (codex r1 #1): one stop() that overruns its deadline past STOP_BUDGET_S leaves the other rows to
    # stop unasked this pass (their leases already renewed, still inside them) and asks them first on the next
    clock = Clock(rig)
    sup, journal = _sup(rig, clock)
    first = _fire(rig, "a", journal)
    second = _second_fire(rig, journal)
    _claimed(rig, first, 1)
    _claimed(rig, second, 2)
    _revoke(rig)
    _revoke_also(rig, second)
    asked = []

    def executor_of(row):
        def overrun():
            asked.append(row["occurrence_id"])
            if row["occurrence_id"] == first:
                clock.advance(supervise.STOP_BUDGET_S + 1)
        return Ex(False, on_stop=overrun)

    out = sup.reap(executor_of)
    assert asked == [first] and out.deferred == [second] and out.asked == [first]
    assert supervise.STOP_BUDGET_S + supervise.STOP_DEADLINE_S < ctl.LEASE_S
    with rig.store.write() as c:              # the deferred row's lease, renewed before the stops, still holds
        assert ctl.takeover(c, second, clock()) is False
    asked.clear()
    again = sup.reap(executor_of)
    assert asked[0] == second and again.asked[0] == second


def test_the_session_grace_is_per_row_from_the_roles_executor(rig):
    # behaviour (codex r1 #2): a dream (sessionless) row with a pid and no session_key keeps being renewed past
    # SESSION_GRACE_S; in the same store, a row whose role is a session executor (or cannot be read: the bound holds)
    # is refused at the grace and lapses to takeover
    from levain.agents import role as role_mod
    from tests.test_agents_launch import ROLES
    role_mod.add_role(rig.store, ROLES["notify-role"])          # the rig agent's role, executor "dream"
    clock = Clock(rig)
    sup, journal = _sup(rig, clock)
    dream = _fire(rig, "a", journal)
    unread = _second_fire(rig, journal)                         # its role (local-role) is not registered
    _claimed(rig, dream, 1, session=False)
    _claimed(rig, unread, 2, session=False)
    clock.advance(ctl.SESSION_GRACE_S + 60)
    out = sup.reap(lambda row: Ex(True))
    assert out.renewed == [dream]


def test_a_stored_spec_that_no_longer_hashes_keeps_the_session_bound(rig):
    # behaviour (codex r2, introduced MED): a spec text that parses and names a dream role but no longer hashes to its
    # spec_hash is not trusted for its role: the row keeps the bound and lapses
    from levain.agents import role as role_mod
    from tests.test_agents_launch import ROLES
    role_mod.add_role(rig.store, ROLES["notify-role"])
    clock = Clock(rig)
    sup, journal = _sup(rig, clock)
    dream = _fire(rig, "a", journal)
    _claimed(rig, dream, 1, session=False)
    with rig.store.write() as c:                                 # corruption stand-in: the store's own trigger refuses this
        c.execute("DROP TRIGGER agent_specs_frozen")
        c.execute("UPDATE agent_specs SET spec = spec || ' '")
    clock.advance(ctl.SESSION_GRACE_S + 60)
    assert sup.reap(lambda row: Ex(True)).renewed == []


def test_an_explicit_none_grace_still_bounds_a_session_role(rig):
    # behaviour (codex r2 #2): session_grace_s=None never means "renew a sessionless-looking row forever"
    from tests.test_agents_launch import ROLES  # noqa: F401
    clock = Clock(rig)
    sup, journal = _sup(rig, clock)
    sup._session_grace_s = None
    unread = _fire(rig, "a", journal)                            # its role is not a registered dream role
    _claimed(rig, unread, 1, session=False)
    clock.advance(ctl.SESSION_GRACE_S + 60)
    assert sup.reap(lambda row: Ex(True)).renewed == []


def test_a_tick_reads_its_clock_again_after_the_reaper(rig, monkeypatch):
    # behaviour (codex/deepseek r1 #3): the claims and the expiry pass use the clock as it reads after the reaper
    clock = Clock(rig)
    seen = []
    real = ctl.slots.expired_launches
    monkeypatch.setattr(ctl.slots, "expired_launches", lambda conn, now: seen.append(now) or real(conn, now))
    ctl.tick(rig.store, clock, lambda a, d: None, reaper=lambda: clock.advance(900))
    assert seen == [clock()]


def test_an_unconfirmed_rows_attention_is_resolved_when_it_ends_on_its_own(rig):
    # behaviour (complement r1 #1, codex r1 #4): the row the attention named finishes between passes; the next pass
    # resolves the key although the row is no longer launched
    clock = Clock(rig)
    sup, journal = _sup(rig, clock)
    oid = _fire(rig, "a", journal)
    _claimed(rig, oid)
    _revoke(rig)
    att = Attention()
    _reap(sup, Ex(False), att)
    assert list(att.rows) == [supervise.ATTENTION_PREFIX + oid]
    rig.finish(oid)
    clock.advance(60)
    _reap(sup, Ex(False), att)
    assert att.rows == {} and att.resolved == [supervise.ATTENTION_PREFIX + oid]


def test_a_resolve_that_raises_is_tried_again_next_pass(rig):
    # behaviour (codex r1 #4, deepseek r1 #4): the key is kept until its resolve succeeds
    clock = Clock(rig)
    sup, journal = _sup(rig, clock)
    oid = _fire(rig, "a", journal)
    _claimed(rig, oid)
    _revoke(rig)
    att = Attention()
    _reap(sup, Ex(False), att)
    fails = [1]

    def resolve(key):
        if fails:
            fails.pop()
            raise OSError("cockpit busy")
        att.resolve(key)

    clock.advance(60)
    done = sup.reap(lambda row: Ex(True), attention=att.upsert, resolve=resolve)
    assert done.ended == [oid] and list(att.rows) == [supervise.ATTENTION_PREFIX + oid]
    clock.advance(60)
    sup.reap(lambda row: Ex(True), attention=att.upsert, resolve=resolve)
    assert att.rows == {}


def test_no_executor_is_not_asked_and_a_settle_that_writes_nothing_is_named(rig, monkeypatch):
    # behaviour (codex r1 #5, complement r1 #6): `asked` holds only rows stop() was called for; a confirmed stop whose
    # settle wrote nothing while the row stays launched is a settle error, not a silent drop
    clock = Clock(rig)
    sup, journal = _sup(rig, clock)
    oid = _fire(rig, "a", journal)
    _claimed(rig, oid)
    _revoke(rig)
    out = sup.reap(lambda row: None)
    assert out.asked == [] and out.unconfirmed == [oid]
    monkeypatch.setattr(ctl, "settle_terminal", lambda *a, **k: ctl.Ended(False))
    clock.advance(60)
    out = sup.reap(lambda row: Ex(True))
    assert out.asked == [oid] and out.ended == [] and out.settle_errors == {oid: "not_settled"}
    assert rig.row(oid)["state"] == "launched"

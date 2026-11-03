"""P6-2b: overlap, the miss streak per window, silence. Each case was first run as a script on a scratch store."""
from __future__ import annotations

from datetime import timedelta

from levain.agents import controller as ctl
from tests import test_agents_claim as tc
from tests.test_agents_claim import T0, _armed, _clock, _set_cursor, _store

LATE = T0 + timedelta(minutes=19)  # 12:00 is past the 15 minute grace, so every slot of the stretch is missed


def _ended(store, key, state, reason, *, launched):
    """One occurrence ended by hand: a run goes admitted -> launched -> done, a no-input skip ends admitted."""
    with store.write() as c:
        oid, outcome = ctl.claim(c, agent_id=tc.AG, kind="manual", key=key, spec_hash="h1", binding_id="b1",
                                 window_key="m:x", now=T0)
        assert outcome == ctl.CLAIMED
        if launched:
            c.execute("UPDATE agent_occurrences SET state='launched', launch_epoch=epoch, link_id='l1',"
                      " link_incarnation='i1', launched_at=? WHERE occurrence_id=?", (T0.strftime("%Y-%m-%dT%H:%M:%SZ"), oid))
        c.execute("UPDATE agent_occurrences SET state=?, reason=?, ended_at=? WHERE occurrence_id=?",
                  (state, reason, "2026-11-02T13:00:00Z", oid))


def _streak(store):
    with store.read() as c:
        return ctl.miss_streak(c, tc.AG)


def test_a_closed_lid_night_of_21_slots_is_one_miss_and_a_second_pass_adds_nothing(tmp_path):
    store = _store(tmp_path)
    _set_cursor(store, tc.AG, "2026-11-02T05:00:00Z")  # 05:20 .. 12:00 is 21 slots
    out = ctl.tick(store, _clock([LATE]), _armed)
    assert out.missed[tc.AG] == 21 and out.missed_windows[tc.AG] == ["d:2026-11-02"]
    assert _streak(store) == 1 and tc._rows(store) == []  # a missed slot is counted, never fired
    again = ctl.tick(store, _clock([LATE]), _armed)
    assert again.missed == {} and _streak(store) == 1


def test_the_streak_counts_windows_and_only_a_done_run_clears_it(tmp_path):
    store = _store(tmp_path)
    _set_cursor(store, tc.AG, "2026-11-01T05:00:00Z")  # two local dates of missed slots
    out = ctl.tick(store, _clock([LATE]), _armed)
    assert out.missed_windows[tc.AG] == ["d:2026-11-01", "d:2026-11-02"] and _streak(store) == 2
    _ended(store, "k-skip", "skipped-no-input", "no_input", launched=False)
    assert _streak(store) == 2  # nothing to do neither counts nor resets
    _ended(store, "k-done", "done", "completed", launched=True)
    assert _streak(store) == 0
    _ended(store, "k-skip2", "skipped-no-input", "no_input", launched=False)
    assert _streak(store) == 0  # a skipped-no-input after a success is not a miss


def test_a_new_key_while_the_previous_occurrence_is_live_is_a_skip_not_a_miss(tmp_path):
    store = _store(tmp_path)
    _set_cursor(store, tc.AG, "2026-11-02T11:40:00Z")
    first = ctl.tick(store, _clock([T0]), _armed)
    assert first.claimed == [(tc.AG, "2026-11-02T12:00:00Z")] and first.overlapped == []
    second = ctl.tick(store, _clock([T0 + timedelta(minutes=20)]), _armed)
    assert second.overlapped == [(tc.AG, "2026-11-02T12:20:00Z")] and second.claimed == []
    assert tc._rows(store)[-1] == ("2026-11-02T12:20:00Z", "skipped-overlap", "overlap")
    assert second.missed == {} and _streak(store) == 0


def test_silence_is_found_in_one_pass_and_only_for_an_agent_that_fired_nothing(tmp_path):
    store = _store(tmp_path)
    _set_cursor(store, tc.AG, "2026-11-02T05:00:00Z")
    quiet = ctl.tick(store, _clock([LATE]), _armed)
    assert quiet.silent == [tc.AG]
    _set_cursor(store, tc.AG, "2026-11-02T05:00:00Z")  # the same stretch, but the clock is back inside 12:00's grace
    heard = ctl.tick(store, _clock([T0]), _armed)
    assert heard.claimed == [(tc.AG, "2026-11-02T12:00:00Z")] and heard.missed[tc.AG] == 20 and heard.silent == []

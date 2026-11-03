"""P6-2b: overlap, the miss streak per window, silence. Each case was first run as a script on a scratch store."""
from __future__ import annotations

from datetime import timedelta

from levain.agents import controller as ctl
from levain.agents import slots
from tests import test_agents_claim as tc
from tests.test_agents_claim import T0, _armed, _clock, _set_cursor, _store

LATE = T0 + timedelta(minutes=19)  # 12:00 is past the 15 minute grace, so every slot of the stretch is missed


def _ended(store, key, state, reason, *, launched, at=T0):
    """One occurrence ended by hand: a run goes admitted -> launched -> done, a no-input skip ends admitted."""
    with store.write() as c:
        oid, outcome = ctl.claim(c, agent_id=tc.AG, kind="manual", key=key, spec_hash="h1", binding_id="b1",
                                 window_key="m:x", now=at)
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


def test_a_walked_agent_that_missed_nothing_is_not_silent_and_has_no_missed_windows_key(tmp_path):
    store = _store(tmp_path)
    _set_cursor(store, tc.AG, "2026-11-02T11:50:00Z")  # the walk sees no slot at all before now
    idle = ctl.tick(store, _clock([T0 - timedelta(minutes=5)]), _armed)
    assert idle.silent == [] and tc.AG not in idle.missed_windows and idle.claimed == []


AFTER_NOV_1 = T0.replace(day=2, hour=4, minute=50)  # local 23:50 on Nov 1: after its last slot, before Nov 2's first


def test_a_done_run_inside_the_backlog_resets_the_streak_for_the_windows_before_it(tmp_path):
    store = _store(tmp_path)
    _set_cursor(store, tc.AG, "2026-11-01T05:00:00Z")
    _ended(store, "k-done", "done", "completed", launched=True, at=AFTER_NOV_1)
    out = ctl.tick(store, _clock([LATE]), _armed)
    assert out.missed_windows[tc.AG] == ["d:2026-11-01", "d:2026-11-02"]  # both are still reported
    assert _streak(store) == 1  # only Nov 2 came after the done


def test_a_done_between_two_windows_of_one_batch_resets_a_prior_streak(tmp_path):
    store = _store(tmp_path)
    _set_cursor(store, tc.AG, "2026-11-01T05:00:00Z")
    ctl.tick(store, _clock([T0.replace(day=1, hour=6, minute=19)]), _armed)  # 05:20 .. 06:00 on Nov 1 missed
    assert _streak(store) == 1
    _ended(store, "k-done", "done", "completed", launched=True, at=AFTER_NOV_1)
    out = ctl.tick(store, _clock([LATE]), _armed)  # the rest of Nov 1, then Nov 2, in one batch
    assert out.missed_windows[tc.AG] == ["d:2026-11-01", "d:2026-11-02"] and _streak(store) == 1


def test_a_no_slot_close_reaches_the_streak_and_a_manual_one_counts_by_its_admission(tmp_path):
    store = _store(tmp_path)
    _set_cursor(store, tc.AG, "2026-11-02T11:40:00Z")
    assert ctl.tick(store, _clock([T0]), _armed).claimed == [(tc.AG, "2026-11-02T12:00:00Z")]
    with store.write() as c:  # the cap is full, so 12:00 stays admitted until its grace runs out
        for i in (1, 2):
            slots.take(c, slots.OCCURRENCE, f"held-{i}", T0)
    out = ctl.tick(store, _clock([T0 + timedelta(minutes=16)]), _armed)
    assert out.no_slot == [(tc.AG, "2026-11-02T12:00:00Z")] and out.missed_windows[tc.AG] == ["d:2026-11-02"]
    assert out.silent == [tc.AG] and _streak(store) == 1  # the walk had no slot to see, the close is the miss
    with store.write() as c:
        ctl.claim(c, agent_id=tc.AG, kind="manual", key="k-man", spec_hash="h1", binding_id="b1",
                  window_key="m:2026-11-02", now=T0 + timedelta(minutes=17))
    late = ctl.tick(store, _clock([T0 + timedelta(minutes=39)]), _armed)  # k-man past grace; 12:20 a walk miss
    assert late.no_slot == [(tc.AG, "k-man")] and late.missed_windows[tc.AG] == ["m:2026-11-02", "d:2026-11-02"]
    assert _streak(store) == 2  # the manual miss counts once and leaves d:2026-11-02 counted once
    _ended(store, "k-done", "done", "completed", launched=True, at=T0 + timedelta(minutes=33))
    assert _streak(store) == 0  # compared with the misses' instants, never with the manual key's text


def test_silence_is_judged_once_per_agent_after_every_walk(tmp_path):
    store = _store(tmp_path)
    other = tc._register(store, name="job_push_check_2")
    for ag in (tc.AG, other):
        _set_cursor(store, ag, "2026-11-02T05:00:00Z")
    out = ctl.tick(store, _clock([LATE]), _armed)
    assert sorted(out.silent) == sorted([tc.AG, other]) and len(out.silent) == 2


NOV_3 = T0 + timedelta(hours=18)  # 06:00Z on Nov 3, local 01:00: the second local date's first hour


def test_a_late_no_slot_close_of_an_already_counted_window_counts_once_and_keeps_the_order(tmp_path):
    # codex r2 MED, reproduced: 12:00 on Nov 2 stays admitted while a slot is free, the walks count Nov 2 and Nov 3,
    # then the cap fills and the old 12:00 closes as no_slot with a marker older than every one already counted
    store = _store(tmp_path)
    _set_cursor(store, tc.AG, "2026-11-02T11:40:00Z")
    assert ctl.tick(store, _clock([T0]), _armed).claimed == [(tc.AG, "2026-11-02T12:00:00Z")]
    walked = ctl.tick(store, _clock([NOV_3 + timedelta(minutes=19)]), _armed)
    assert walked.no_slot == [] and walked.missed_windows[tc.AG] == ["d:2026-11-02", "d:2026-11-03"]
    assert _streak(store) == 2
    with store.write() as c:
        for i in (1, 2):
            slots.take(c, slots.OCCURRENCE, f"held-{i}", NOV_3 + timedelta(minutes=19))
    late = ctl.tick(store, _clock([NOV_3 + timedelta(minutes=19, seconds=30)]), _armed)  # before 06:20 is claimed
    assert late.no_slot == [(tc.AG, "2026-11-02T12:00:00Z")] and late.claimed == [] and _streak(store) == 2
    _ended(store, "k-done", "done", "completed", launched=True, at=NOV_3 - timedelta(hours=2))
    assert _streak(store) == 1  # the done sits between the two windows: Nov 2 is still the older one


def test_one_window_seen_by_a_walk_and_then_by_a_no_slot_close_counts_once(tmp_path):
    store = _store(tmp_path)
    _set_cursor(store, tc.AG, "2026-11-02T11:40:00Z")
    ctl.tick(store, _clock([T0]), _armed)  # 12:00 admitted, a slot free, so it stays admitted past its grace
    assert ctl.tick(store, _clock([T0 + timedelta(minutes=39)]), _armed).missed_windows[tc.AG] == ["d:2026-11-02"]
    with store.write() as c:
        for i in (1, 2):
            slots.take(c, slots.OCCURRENCE, f"held-{i}", T0 + timedelta(minutes=39))
    out = ctl.tick(store, _clock([T0 + timedelta(minutes=39)]), _armed)
    assert out.no_slot == [(tc.AG, "2026-11-02T12:00:00Z")] and _streak(store) == 1


def test_an_unreadable_streak_value_reads_as_0_and_the_next_miss_starts_over(tmp_path):
    store = _store(tmp_path)
    for junk in (b"\xff\x00", 7, "2|d:2026-11-02|2026-11-02T12:00:00Z", '[["2026-11-02T12:00:00Z"]]'):
        with store.write() as c:
            c.execute("INSERT INTO agent_settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET"
                      " value = excluded.value", (ctl.STREAK_PREFIX + tc.AG, junk))
        assert _streak(store) == 0
    _set_cursor(store, tc.AG, "2026-11-02T05:00:00Z")
    ctl.tick(store, _clock([LATE]), _armed)
    assert _streak(store) == 1

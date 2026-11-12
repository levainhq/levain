"""The 1111-38 fix round of the dream stack (D1-N, D1-M, D1-O): the L3 review's two codex MEDs on the hand start, and
the L2 trust review's text findings (F4 the signing card, F5 the wrap-open card's enable line, F6/F7 the remedies that
said "nothing needs doing by hand" for classes that need a person, F9 the own-wrap admission note).

Every store is real, as in ``tests/test_dream_start.py`` (whose rig this reuses); nothing composes and no model is
called."""
from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from levain.dream import agent as da
from levain.dream import ready as rd
from levain.dream import start as st
from tests.test_dream_start import ZONE, _claim, _go, _live_rig, _rows


# --------------------------------------------- A (codex MED1): an admitted manual row of an earlier day is not resumed

def test_a_stale_day_admitted_manual_row_is_ended_and_a_fresh_one_launches_under_todays_key(tmp_path):
    rig = _live_rig(tmp_path)
    yesterday = st.manual_window_key(rig.now() - timedelta(days=1), ZONE)
    today = st.manual_window_key(rig.now(), ZONE)
    assert yesterday != today
    old = _claim(rig, "left-admitted-yesterday", window_key=yesterday)     # a start hard-killed after its claim, day D
    got, spawned = _go(rig)                                                 # the operator starts again on day D+1
    assert (got.code, got.outcome, got.resumed) == (0, "launched", False), got
    assert got.occurrence_id != old, got
    by_id = {r["occurrence_id"]: r for r in _rows(rig)}
    assert (by_id[old]["state"], by_id[old]["reason"]) == ("stopped", st.REASON_INTERRUPTED), by_id[old]
    assert by_id[old]["launched_at"] is None and by_id[old]["window_key"] == yesterday
    fresh = by_id[got.occurrence_id]
    assert (fresh["state"], fresh["kind"], fresh["window_key"]) == ("launched", "manual", today), fresh
    assert spawned == [(rig.store.directory, got.occurrence_id)]


def _next_local(after: datetime, hour: int, minute: int, second: int) -> datetime:
    local = after.astimezone(ZoneInfo(ZONE))
    t = local.replace(hour=hour, minute=minute, second=second, microsecond=0)
    return t if t > local else t + timedelta(days=1)


def test_b_the_input_check_judges_the_instant_the_start_claims_and_launches_at(tmp_path, monkeypatch):
    # 13:59:59 local is the last second whose manual run consolidates the previous dream-day; a check that read the
    # wall clock (or any later instant: 14:00:01) would judge a different dream-day than the launch is attributed to
    rig = _live_rig(tmp_path)
    t0 = _next_local(rig.now() + timedelta(minutes=1), 13, 59, 59)
    later = t0 + timedelta(seconds=2)
    assert rd.manual_day_at(t0, ZONE) != rd.manual_day_at(later, ZONE)
    calls = []

    def clock():
        calls.append(None)
        return t0 if len(calls) == 1 else later

    seen = []
    real = da.day_ready
    monkeypatch.setattr(da, "day_ready", lambda *a, **k: seen.append(k["now"]) or real(*a, **k))
    spawned = []
    got = st.start(rig.store, clock=clock, authority=rig.authority, spawn=lambda d, oid: spawned.append(oid))
    assert seen, got
    assert seen == [t0], (seen, t0, got)
    row = next(r for r in _rows(rig) if r["occurrence_id"] == got.occurrence_id)
    assert row["window_key"] == st.manual_window_key(t0, ZONE)


def test_a_todays_admitted_manual_row_is_still_the_one_resumed(tmp_path):
    rig = _live_rig(tmp_path)
    left = _claim(rig, "left-admitted-today")
    got, spawned = _go(rig)
    assert (got.outcome, got.resumed, got.occurrence_id) == ("launched", True, left), got
    assert len(_rows(rig)) == 1

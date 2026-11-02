from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from levain.agents import schedule as sch

NY = "America/New_York"
DREAM = {"kind": "schedule", "cron": "*/20 22-23,0-4 * * *", "zone": NY, "window": {"start": "22:00", "end": "05:00"}}


def _utc(y, mo, d, h=0, mi=0):
    return datetime(y, mo, d, h, mi, tzinfo=timezone.utc)


def _tick(trigger, zone, after, upto):
    # a fake clock advancing every 5 minutes, as the daemon's tick does; each tick looks back to the previous one
    seen, prev, now = [], after, after
    while now < upto:
        now += timedelta(minutes=5)
        seen += [s for s, state in sch.due_slots(trigger, zone, prev, now) if state == sch.DUE]
        prev = now
    return seen


def test_one_instant_per_slot_across_both_dst_edges():
    # dream-night windows: evening before the edge, local 18:00 to the next local 18:00
    fall = _tick(DREAM, NY, _utc(2026, 11, 1, 0),_utc(2026, 11, 1, 23))
    spring = _tick(DREAM, NY, _utc(2027, 3, 13, 20), _utc(2027, 3, 14, 12))
    for slots, expect in ((fall, 24), (spring, 18)):
        keys = [sch.slot_key(s) for s in slots]
        assert len(keys) == len(set(keys)) == expect
    # fall-back: the 01:00 wall hour repeats, so 01:00 local appears twice, as two distinct UTC instants
    ny = ZoneInfo(NY)
    local_01 = [s.astimezone(ny) for s in fall if s.astimezone(ny).strftime("%H:%M") == "01:00"]
    assert len(local_01) == 2 and local_01[0].utcoffset() != local_01[1].utcoffset()
    # spring-forward: no slot lands in the 02:xx wall hour that does not exist
    assert not [s for s in spring if s.astimezone(ny).hour == 2]


def test_windowed_schedule_yields_its_slots():
    slots = sch.slots_between(DREAM, NY, _utc(2026, 10, 20, 12), _utc(2026, 10, 21, 12))  # an ordinary day
    local = [s.astimezone(ZoneInfo(NY)).strftime("%H:%M") for s in slots]
    assert len(local) == 21 and local[0] == "22:00" and local[-1] == "04:40" and "05:00" not in local
    assert len({sch.window_key(DREAM, NY, s) for s in slots}) == 1
    assert sch.window_key(DREAM, NY, slots[0]) == "w:2026-10-20T22:00"


def test_grace_due_versus_missed():
    slot = _utc(2026, 10, 20, 2, 0)
    assert sch.classify(slot, slot + timedelta(minutes=15)) == sch.DUE
    assert sch.classify(slot, slot + timedelta(minutes=15, seconds=1)) == sch.MISSED
    assert sch.classify(slot, slot - timedelta(seconds=1)) == sch.NOT_YET
    # a closed lid: first seen two hours late, every slot of the gap is missed except any inside the grace
    states = [st for _, st in sch.due_slots(DREAM, NY, _utc(2026, 10, 21, 2, 0), _utc(2026, 10, 21, 4, 5))]
    assert states.count(sch.DUE) == 1 and states.count(sch.MISSED) == len(states) - 1


def test_zone_is_read_from_the_stored_zone_not_the_host(monkeypatch):
    daily = {"kind": "schedule", "cron": "0 9 * * *", "zone": None, "window": None}
    span = (_utc(2026, 10, 20, 0), _utc(2026, 10, 21, 0))
    monkeypatch.setenv("TZ", "Asia/Tokyo")
    import time
    time.tzset()
    try:
        ny = sch.slots_between(daily, NY, *span)
        la = sch.slots_between(daily, "America/Los_Angeles", *span)
    finally:
        monkeypatch.undo()
        time.tzset()
    assert [s.hour for s in ny] == [13] and [s.hour for s in la] == [16]  # 09:00 EDT / PDT in UTC
    try:
        sch.slots_between(daily, None, *span)
    except ValueError:
        pass
    else:
        raise AssertionError("a missing stored zone must refuse, not fall back to the host")

"""D1-Q (board row 1689), the controller rule: an ``admitted`` row older than its grace is ended.

The tick CLAIMS a scheduled slot into an ``admitted`` row; a row nothing launched (the process died between the claim
and the launch, the cover could not be read for the whole grace, a hand start was killed) used to stay ``admitted``
for ever: the next slot was filed ``skipped-overlap`` behind it and ``levain dream start`` refused it as "in flight".
:func:`levain.agents.controller.close_unlaunched` ends it, with the one grace every other close reads.
"""
from __future__ import annotations

import shutil
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from levain.agents import controller as ctl
from levain.agents import schedule, slots
from tests.test_agents_launch import BINDING, Rig

pytestmark = pytest.mark.skipif(shutil.which("ssh-keygen") is None, reason="needs ssh-keygen")

UTC = timezone.utc
SLOT = datetime(2026, 11, 12, 5, 0, tzinfo=UTC)          # 00:00 EST, a */30 slot of the rig's agent
MIN = timedelta(minutes=1)


def _rows(rig):
    with rig.store.read() as c:
        c.row_factory = sqlite3.Row
        return [dict(r) for r in c.execute("SELECT * FROM agent_occurrences ORDER BY admitted_at, key")]


def _claim(rig, key, *, kind="scheduled", at=SLOT, window="d:2026-11-12"):
    with rig.store.write() as c:
        oid, outcome = ctl.claim(c, agent_id=rig.agent, kind=kind, key=key, spec_hash=rig.spec_hash,
                                 binding_id=BINDING, window_key=window, now=at)
    assert outcome == ctl.CLAIMED
    return oid


def _tick(rig, at, armed=False):
    return ctl.tick(rig.store, lambda: at, lambda agent_id, digest: BINDING if armed else None)


def test_a_scheduled_row_left_admitted_past_its_grace_is_ended_missed_and_counted(tmp_path):
    rig = Rig(tmp_path)
    oid = _claim(rig, schedule.slot_key(SLOT))
    out = _tick(rig, SLOT + schedule.GRACE + MIN)
    (row,) = _rows(rig)
    assert (row["occurrence_id"], row["state"], row["reason"]) == (oid, "missed", ctl.NEVER_LAUNCHED), row
    assert row["ended_at"]
    assert out.unlaunched == [(rig.agent, schedule.slot_key(SLOT))]
    with rig.store.read() as c:
        assert ctl.miss_streak(c, rig.agent) == 1          # a night the dream did not run is a missed night


def test_a_manual_row_left_admitted_past_its_grace_is_ended_stopped_and_not_a_missed_night(tmp_path):
    rig = Rig(tmp_path)
    _claim(rig, "hand-1", kind="manual", window="m:2026-11-11")
    out = _tick(rig, SLOT + schedule.GRACE + MIN)
    (row,) = _rows(rig)
    assert (row["state"], row["reason"]) == ("stopped", ctl.NEVER_LAUNCHED), row
    with rig.store.read() as c:
        assert ctl.miss_streak(c, rig.agent) == 0          # a hand start is not "the laptop slept"
    assert out.unlaunched == [(rig.agent, "hand-1")] and out.missed == {}


def test_an_admitted_row_inside_its_grace_is_left_for_the_launch_to_retry(tmp_path):
    rig = Rig(tmp_path)
    _claim(rig, schedule.slot_key(SLOT))
    out = _tick(rig, SLOT + schedule.GRACE)               # exactly the grace: still due, not missed
    (row,) = _rows(rig)
    assert row["state"] == "admitted" and out.unlaunched == []


def test_the_retry_slot_is_claimed_not_skipped_overlap_behind_a_row_nothing_launched(tmp_path):
    # the measured failure: 00:00 claimed and never launched, so the 00:20 retry slot filed skipped-overlap behind it
    rig = Rig(tmp_path)
    _tick(rig, SLOT - 10 * MIN, armed=True)                # settles the cursor
    out = _tick(rig, SLOT + MIN, armed=True)               # claims the 00:00 slot
    assert out.claimed == [(rig.agent, schedule.slot_key(SLOT))]
    retry = SLOT + timedelta(minutes=30)                   # the rig's next slot (a */30 agent)
    out = _tick(rig, retry + MIN, armed=True)
    assert out.overlapped == [], out.overlapped
    assert out.claimed == [(rig.agent, schedule.slot_key(retry))]
    by_key = {r["key"]: (r["state"], r["reason"]) for r in _rows(rig)}
    assert by_key == {schedule.slot_key(SLOT): ("missed", ctl.NEVER_LAUNCHED),
                      schedule.slot_key(retry): ("admitted", None)}


def test_a_full_cap_still_ends_the_row_no_slot_and_not_never_launched(tmp_path):
    rig = Rig(tmp_path)
    _claim(rig, schedule.slot_key(SLOT))
    with rig.store.write() as c:
        slots.take(c, slots.LAUNCH, "ln-a", SLOT + 20 * MIN)
        slots.take(c, slots.LAUNCH, "ln-b", SLOT + 20 * MIN)
    out = _tick(rig, SLOT + 20 * MIN)
    (row,) = _rows(rig)
    assert (row["state"], row["reason"]) == ("missed", "no_slot") and out.unlaunched == []
    assert out.no_slot == [(rig.agent, schedule.slot_key(SLOT))]

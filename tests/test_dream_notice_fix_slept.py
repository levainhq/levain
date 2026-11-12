"""D1-N fix round, finding 2: the laptop-slept card of a wake inside the window.

A walk's missed slots are never written as rows; the only record of them is the controller's miss-streak record. The
tick that sees the miss can run BEFORE the window closes (the laptop wakes at 04:57, the window closes at 05:00), and
the filer answers ``waiting``; a later tick has an empty ``TickResult.missed_windows`` and no rows. The windows the
record holds are what a later tick reads.
"""
from __future__ import annotations

from datetime import timedelta

from levain.agents import controller as ctl
from levain.agents import tick_driver as T
from levain.dream import ready as rd
from tests.test_dream_agent import ZONE
from tests.test_dream_notices import Night, night  # noqa: F401  (the fixture)


def test_a_wake_before_the_window_closes_files_the_laptop_slept_card_once_it_has(night):
    """Reproduced in a scratch store: the laptop sleeps through every slot of the night and wakes at 04:57 (the last slot
    is 04:40, 15 minutes of grace, the window closes at 05:00). That tick's walk sees the miss; the window is still
    open, so the filer answers ``waiting``; no row was written; the next tick (05:01) has an empty ``missed_windows``
    and read ``no-occurrences``: the card was never filed."""
    night.sign_link(days_left=20)
    night_of = night.last_closed
    closes = rd.day_start(night_of + timedelta(days=1), ZONE)
    assert night.tick(clock=lambda: closes - timedelta(hours=7, minutes=30)) == T.EXIT_OK, night.err   # 21:30, cursor set
    woke = night.tick(clock=lambda: closes - timedelta(minutes=3))                                   # 04:57
    assert woke == T.EXIT_OK, night.err
    assert rd._read_occurrences(night.rig.store.directory) == []           # a walk's missed slots are never written as rows
    with night.rig.store.read() as conn:
        assert ctl.miss_streak(conn, night.reg.agent_id) == 1               # but the controller did record the miss
    assert night.items(include_cleared=True) == []                          # and the window was still open
    for minute in (1, 2, 3):                                                # the window has closed
        assert night.tick(clock=lambda m=minute: closes + timedelta(minutes=m)) == T.EXIT_OK, night.err
    cards = night.items()
    assert [c.body["kind"] for c in cards] == ["did-not-consolidate"], night.err
    assert cards[0].body["reason"].startswith("laptop-slept")
    assert cards[0].body["day"] == night_of.isoformat()


def test_the_card_of_a_slept_night_is_filed_once_not_once_per_tick(night):
    night.sign_link(days_left=20)
    closes = rd.day_start(night.last_closed + timedelta(days=1), ZONE)
    night.tick(clock=lambda: closes - timedelta(hours=7, minutes=30))
    night.tick(clock=lambda: closes - timedelta(minutes=3))
    for minute in range(1, 6):
        night.tick(clock=lambda m=minute: closes + timedelta(minutes=m))
    assert [c.body["kind"] for c in night.items(include_cleared=True)] == ["did-not-consolidate"]


def test_the_controller_names_the_windows_its_streak_holds(night):
    night.sign_link(days_left=20)
    closes = rd.day_start(night.last_closed + timedelta(days=1), ZONE)
    night.tick(clock=lambda: closes - timedelta(hours=7, minutes=30))
    night.tick(clock=lambda: closes - timedelta(minutes=3))
    with night.rig.store.read() as conn:
        assert ctl.miss_windows(conn, night.reg.agent_id) == [rd.window_key_for(night.last_closed)]
        assert ctl.miss_windows(conn, "ag-nobody") == []

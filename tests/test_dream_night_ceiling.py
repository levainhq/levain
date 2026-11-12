"""1690: a hand start counts against the night it falls inside (Phill's ruling np-8ec243e5 (a), 11-12).

"if it launches within 22:00-05:00 it uses that night's allowance, a daytime one counts against the calendar day only".
One function, :func:`levain.agents.accounting.count_span`, says which allowance an instant counts against; the launch's
ceiling and budget (``accounting.Limits``, built by ``levain.dream.start.window_for``) and the day-ready check 1 both read
it. Each is shown here on its own: the accounting through a real launch with no input hook, check 1 through the
predicate over a real occurrence table.
"""
from __future__ import annotations

import shutil
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from levain.agents import accounting
from levain.agents import controller as ctl
from levain.dream import ready as rd
from levain.dream import start as st
from tests.test_dream_ready import Entity

ZONE = "America/New_York"
NY = ZoneInfo(ZONE)
UTC = timezone.utc
NIGHT = {"start": "22:00", "end": "05:00"}


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


# ------------------------------------------------------------------------------------------- the one counting rule

def test_count_span_puts_each_instant_in_its_night_or_its_daytime():
    night = (datetime(2026, 11, 12, 22, 0, tzinfo=NY), datetime(2026, 11, 13, 5, 0, tzinfo=NY))
    for local in (datetime(2026, 11, 12, 22, 0, tzinfo=NY), datetime(2026, 11, 12, 23, 50, tzinfo=NY),
                  datetime(2026, 11, 13, 0, 10, tzinfo=NY), datetime(2026, 11, 13, 4, 59, tzinfo=NY)):
        s = accounting.count_span(NIGHT, ZONE, local)
        assert (s.window_key, s.start, s.end) == ("w:2026-11-12T22:00", *night), local
    day = (datetime(2026, 11, 13, 5, 0, tzinfo=NY), datetime(2026, 11, 13, 22, 0, tzinfo=NY))
    for local in (datetime(2026, 11, 13, 5, 0, tzinfo=NY), datetime(2026, 11, 13, 12, 0, tzinfo=NY),
                  datetime(2026, 11, 13, 21, 59, tzinfo=NY)):
        s = accounting.count_span(NIGHT, ZONE, local)
        assert (s.window_key, s.start, s.end) == (None, *day), local
    s = accounting.count_span(NIGHT, ZONE, datetime(2026, 11, 12, 21, 59, tzinfo=NY))
    assert (s.window_key, s.start, s.end) == (None, datetime(2026, 11, 12, 5, 0, tzinfo=NY), night[0])


def test_count_span_on_the_fall_back_night_holds_both_01_30s_and_closes_at_05_00_est():
    first = datetime(2026, 11, 1, 1, 30, tzinfo=NY)                 # EDT
    second = datetime(2026, 11, 1, 1, 30, fold=1, tzinfo=NY)        # EST, an hour later
    assert second.astimezone(UTC) - first.astimezone(UTC) == timedelta(hours=1)
    for local in (datetime(2026, 10, 31, 23, 30, tzinfo=NY), first, second):
        s = accounting.count_span(NIGHT, ZONE, local)
        assert s.window_key == "w:2026-10-31T22:00"
        assert (s.start, s.end) == (datetime(2026, 11, 1, 2, 0, tzinfo=UTC), datetime(2026, 11, 1, 10, 0, tzinfo=UTC))


def test_count_span_with_no_window_is_the_calendar_day():
    s = accounting.count_span(None, ZONE, datetime(2026, 11, 12, 23, 0, tzinfo=NY))
    assert (s.window_key, s.start, s.end) == ("d:2026-11-12", datetime(2026, 11, 12, tzinfo=NY),
                                              datetime(2026, 11, 13, tzinfo=NY))


def test_the_hand_starts_window_is_the_count_span_of_its_instant():
    for local in (datetime(2026, 11, 12, 23, 30, tzinfo=NY), datetime(2026, 11, 13, 0, 10, tzinfo=NY),
                  datetime(2026, 11, 13, 12, 0, tzinfo=NY)):
        s = accounting.count_span(rd.WINDOW, ZONE, local)
        assert st.window_for(local, ZONE) == ctl.Window(_iso(s.start), _iso(s.end), s.window_key)
        assert rd.window_holding(local, ZONE) == s.window_key


# ------------------------------------------------------------------------------- check 1 counts by the same rule

@pytest.fixture
def ent(tmp_path):
    return Entity(tmp_path)


def _hand(ent, local: datetime, state: str = "failed") -> None:
    ent.occurrence(state, kind="manual", window=st.manual_window_key(local, ZONE), launched=_iso(local),
                   ended=_iso(local + timedelta(minutes=5)))


def _check1(v: rd.Verdict) -> rd.Check:
    return next(c for c in v.checks if c.n == 1)


@pytest.mark.parametrize("earlier, at", [
    ((datetime(2026, 11, 12, 23, 30), datetime(2026, 11, 12, 23, 50)), datetime(2026, 11, 13, 0, 10)),
    ((datetime(2026, 11, 12, 23, 50), datetime(2026, 11, 13, 0, 10)), datetime(2026, 11, 13, 0, 20)),
])
def test_check1_hand_starts_across_midnight_count_one_night(ent, earlier, at):
    for e in earlier:
        _hand(ent, e.replace(tzinfo=NY))
    now = at.replace(tzinfo=NY)
    v = ent.evaluate(now=now, day=rd.manual_day_at(now, ZONE), manual=True)
    c = _check1(v)
    assert (c.status, c.kind) == (rd.QUIET, "ceiling"), c.detail


def test_check1_a_daytime_hand_start_counts_the_calendar_day_only(ent):
    _hand(ent, datetime(2026, 11, 12, 23, 30, tzinfo=NY))
    _hand(ent, datetime(2026, 11, 13, 0, 10, tzinfo=NY))
    noon = datetime(2026, 11, 13, 12, 0, tzinfo=NY)
    v = ent.evaluate(now=noon, day=rd.manual_day_at(noon, ZONE), manual=True)
    assert _check1(v).status == "pass", _check1(v).detail
    _hand(ent, datetime(2026, 11, 13, 9, 0, tzinfo=NY))
    _hand(ent, datetime(2026, 11, 13, 10, 0, tzinfo=NY))
    v = ent.evaluate(now=noon, day=rd.manual_day_at(noon, ZONE), manual=True)
    assert (_check1(v).status, _check1(v).kind) == (rd.QUIET, "ceiling")


def test_check1_a_daytime_start_is_not_held_to_a_nights_run_of_its_own_calendar_date(ent):
    """The old rule counted the whole local date, so one daytime run plus the 00:10 night run read as two and refused
    a second daytime start; the night is its own allowance, so the daytime start still passes."""
    _hand(ent, datetime(2026, 11, 13, 0, 10, tzinfo=NY))
    _hand(ent, datetime(2026, 11, 13, 9, 0, tzinfo=NY))
    noon = datetime(2026, 11, 13, 12, 0, tzinfo=NY)
    v = ent.evaluate(now=noon, day=rd.manual_day_at(noon, ZONE), manual=True)
    assert _check1(v).status == "pass", _check1(v).detail


def test_the_night_check_1_derives_for_a_dream_day_is_the_key_check_6_filters_by():
    """check 1 re-derives a scheduled night's span from the dream-day and check 6 filters by the key the caller passes;
    they are one night only while the dream's window opens on the dream-day, so a move of the window that broke that
    fails here instead of letting the two checks disagree."""
    day = date(2026, 11, 12)
    opens = datetime(day.year, day.month, day.day, int(rd.WINDOW["start"][:2]), int(rd.WINDOW["start"][3:]), tzinfo=NY)
    assert accounting.count_span(rd.WINDOW, ZONE, opens).window_key == rd.window_key_for(day)


def test_check1_counts_the_fall_back_night_as_one_night(ent):
    _hand(ent, datetime(2026, 10, 31, 23, 30, tzinfo=NY))
    _hand(ent, datetime(2026, 11, 1, 1, 30, tzinfo=NY))                   # EDT
    now = datetime(2026, 11, 1, 1, 30, fold=1, tzinfo=NY)                # EST
    v = ent.evaluate(now=now, day=rd.manual_day_at(now, ZONE), manual=True)
    assert (_check1(v).status, _check1(v).kind) == (rd.QUIET, "ceiling"), _check1(v).detail


def test_check1_a_scheduled_slot_counts_the_hand_starts_inside_its_night(ent):
    _hand(ent, datetime(2026, 11, 2, 23, 0, tzinfo=NY))
    _hand(ent, datetime(2026, 11, 3, 0, 10, tzinfo=NY))
    v = ent.evaluate(now=datetime(2026, 11, 3, 1, 0, tzinfo=NY), day=date(2026, 11, 2))
    assert (_check1(v).status, _check1(v).kind) == (rd.QUIET, "ceiling"), _check1(v).detail


def test_check1_an_unreadable_launch_stamp_counts(ent):
    ent.occurrence("failed", kind="manual", window="m:2026-11-12", launched="not a time")
    _hand(ent, datetime(2026, 11, 12, 23, 30, tzinfo=NY))
    now = datetime(2026, 11, 13, 0, 10, tzinfo=NY)
    v = ent.evaluate(now=now, day=rd.manual_day_at(now, ZONE), manual=True)
    assert (_check1(v).status, _check1(v).kind) == (rd.QUIET, "ceiling"), _check1(v).detail


# ------------------------------------------------------------------- the launch's signed ceiling, through a real launch

needs_keygen = pytest.mark.skipif(shutil.which("ssh-keygen") is None, reason="needs ssh-keygen")


def _night_of(rig) -> datetime:
    """22:00 local tomorrow in the rig's zone: every instant used below is after the link was signed."""
    local = rig.now().astimezone(NY)
    return datetime(local.year, local.month, local.day, 22, tzinfo=NY) + timedelta(days=1)


def _launch(rig, at: datetime):
    from tests.test_agents_tick_launch import _claim, _exec

    oid = _claim(rig, f"hand-{_iso(at)}", kind="manual", at=at, window=st.manual_window_key(at, ZONE))
    return oid, ctl.launch(rig.store, oid, authority=rig.authority, window=st.window_for(at, ZONE), now=at,
                           executor=_exec(rig, oid), on_input=None)


def _launched(rig, at: datetime) -> None:
    from tests.test_agents_tick_launch import _finish

    oid, res = _launch(rig, at)
    assert res.outcome == "launched", res
    _finish(rig, oid)


@needs_keygen
@pytest.mark.parametrize("earlier, third", [((90, 110), 130), ((110, 130), 140)])   # minutes after 22:00
def test_the_signed_ceiling_of_two_refuses_a_third_hand_start_across_midnight(tmp_path, earlier, third):
    from tests.test_dream_start import _live_rig

    rig = _live_rig(tmp_path, run_ceiling=2)
    ten = _night_of(rig)
    for m in earlier:
        _launched(rig, ten + timedelta(minutes=m))
    _, res = _launch(rig, ten + timedelta(minutes=third))
    assert (res.outcome, res.reason) == (ctl.SKIPPED_BUDGET, "ceiling"), res


@needs_keygen
def test_a_daytime_hand_start_is_not_held_to_the_nights_runs(tmp_path):
    from tests.test_dream_start import _live_rig

    rig = _live_rig(tmp_path, run_ceiling=2)
    ten = _night_of(rig)
    _launched(rig, ten + timedelta(minutes=90))                        # 23:30
    _launched(rig, ten + timedelta(minutes=130))                       # 00:10
    _launched(rig, ten + timedelta(hours=14))                          # 12:00 the next day
    _launched(rig, ten + timedelta(hours=15))                          # 13:00
    _, res = _launch(rig, ten + timedelta(hours=16))                   # 14:00: the day's two are spent
    assert (res.outcome, res.reason) == (ctl.SKIPPED_BUDGET, "ceiling"), res

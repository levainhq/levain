"""Row 184: a same-day timed snooze. A future minute on the host's local day is accepted, a past or present minute refused,
judged on the HOST's clock (the store's injected clock here), to the minute, in the host's LOCAL zone."""
from __future__ import annotations

import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from levain.cockpit.consent import ConsentError
from levain.consent.questions import INTENTION_CLOSE, local_minute, until_refusal
from tests.test_intention_native import _ringing, _wall_clock_far_away  # noqa: F401  (the autouse far-wall fixture)


def _local_now(qs) -> datetime:
    return qs._clock().astimezone()


def _minute(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M")


def test_a_timed_snooze_to_a_later_minute_today_is_accepted(tmp_path: Path) -> None:
    """THE REPRODUCED REFUSAL: on the unfixed code this raised 'pick a later day' for any timed value on today."""
    root, spores, qdir, sp, sid, qs, q = _ringing(tmp_path)
    until = _minute(_local_now(qs) + timedelta(minutes=1))
    said = qs.intention_act(q.id, "snooze", until=until, channel="cockpit")
    assert f"snoozed to {until}" in said
    assert sp.get(sid)["next"] == until and not sp.get(sid).get("germinated")
    closed = qs.get(q.id)
    assert closed.answer is not None and closed.answer.value == INTENTION_CLOSE + f"snoozed to {until}"


def test_the_same_minute_or_an_earlier_one_today_is_refused_in_plain_words(tmp_path: Path) -> None:
    root, spores, qdir, sp, sid, qs, q = _ringing(tmp_path)
    now = _local_now(qs)
    for until in (_minute(now), _minute(now - timedelta(minutes=1))):
        with pytest.raises(ConsentError) as exc:
            qs.intention_act(q.id, "snooze", until=until, channel="cockpit")
        assert str(exc.value) == "pick a time after now"
    assert sp.get(sid)["next"] == "2026-11-06" and sp.get(sid)["germinated"] and qs.get(q.id).answer is None


def test_a_date_only_today_and_a_timed_past_day_keep_pick_a_later_day(tmp_path: Path) -> None:
    root, spores, qdir, sp, sid, qs, q = _ringing(tmp_path)
    today = _local_now(qs).date()
    for until in (today.isoformat(), _minute(_local_now(qs) - timedelta(days=1))):
        with pytest.raises(ConsentError) as exc:
            qs.intention_act(q.id, "snooze", until=until, channel="cockpit")
        assert str(exc.value) == "pick a later day"
    assert qs.get(q.id).answer is None


def test_a_timed_value_on_a_future_day_and_the_shape_only_call_pass_as_before() -> None:
    now = datetime.fromisoformat("2026-11-07T12:00:00+00:00")
    assert until_refusal("2026-11-30T00:00", now=now) is None
    assert until_refusal("2026-11-30T00:00", "2026-11-07") is None
    assert until_refusal("2026-11-07T00:00") is None                       # no clock: shape only
    assert until_refusal("2026-11-07", "2026-11-07") == "pick a later day"  # the old call, unchanged
    assert "pick a day" in until_refusal("2026-11-20T25:99", now=now)


def test_today_and_now_together_are_refused_and_now_is_keyword_only() -> None:
    now = datetime.fromisoformat("2026-11-07T12:00:00+00:00")
    with pytest.raises(ValueError):
        until_refusal("2026-11-30T00:00", "2026-11-07", now=now)     # one judge of the day, never two
    with pytest.raises(TypeError):
        until_refusal("2026-11-30T00:00", "2026-11-07", now)          # a positional clock is a call-site mistake


@pytest.fixture
def _kiritimati(monkeypatch):
    monkeypatch.setenv("TZ", "Pacific/Kiritimati")                         # UTC+14: the local day is a day ahead of UTC's
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


def test_the_minute_is_judged_in_the_hosts_local_zone(tmp_path: Path, _kiritimati) -> None:
    root, spores, qdir, sp, sid, qs, q = _ringing(tmp_path)
    now = _local_now(qs)
    assert local_minute(qs._clock()) == _minute(now) and now.utcoffset() == timedelta(hours=14)
    assert now.date().isoformat() == "2026-11-08"                          # UTC says 11-07: a UTC judgement would differ
    with pytest.raises(ConsentError) as exc:
        qs.intention_act(q.id, "snooze", until=_minute(now - timedelta(minutes=1)), channel="cockpit")
    assert str(exc.value) == "pick a time after now"
    until = _minute(now + timedelta(minutes=1))
    qs.intention_act(q.id, "snooze", until=until, channel="cockpit")
    assert sp.get(sid)["next"] == until

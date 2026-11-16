"""The night lease rule and the 50-minute keepalive (ND-5b): pure functions over an injected clock."""
from __future__ import annotations

import ast
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from levain.desk import night_lease as nl
from levain.desk.lease import SERVICE_BEAT_WINDOW

T0 = datetime(2026, 11, 16, 3, 0, tzinfo=timezone.utc)


def _beat(*, night, alive=True, done=False, silent=timedelta(0)):
    return nl.beat_due(night_open=night, session_alive=alive, session_done=done, last_write_at=T0 - silent, now=T0)


def test_night_window_alive_session_is_not_lapsed_for_silence():
    assert _beat(night=True, silent=SERVICE_BEAT_WINDOW * 10) is True


def test_day_rule_still_lapses_a_silent_holder():
    assert _beat(night=False, silent=SERVICE_BEAT_WINDOW + timedelta(seconds=1)) is False
    assert _beat(night=False, silent=SERVICE_BEAT_WINDOW) is True


def test_night_does_not_hold_a_dead_or_done_session():
    assert _beat(night=True, alive=False) is False
    assert _beat(night=True, done=True) is False


def test_never_written_holder_is_held_at_night_not_by_day():
    assert nl.beat_due(night_open=True, session_alive=True, session_done=False, last_write_at=None, now=T0) is True
    assert nl.beat_due(night_open=False, session_alive=True, session_done=False, last_write_at=None, now=T0) is False


def test_keepalive_due_at_50_minutes_not_49():
    assert nl.keepalive_due(T0 - timedelta(minutes=49), T0) is False
    assert nl.keepalive_due(T0 - timedelta(minutes=49, seconds=59), T0) is False
    assert nl.keepalive_due(T0 - timedelta(minutes=50), T0) is True
    assert nl.keepalive_due(T0 - timedelta(hours=3), T0) is True


def test_keepalive_due_with_no_turn_yet():
    assert nl.keepalive_due(None, T0) is True


def test_keepalive_period_is_50_minutes():
    assert nl.KEEPALIVE_PERIOD == timedelta(minutes=50)


def test_keepalive_turn_is_not_a_write():
    turns = [(T0 - timedelta(hours=2), nl.WRITE), (T0 - timedelta(minutes=10), nl.KEEPALIVE)]
    assert nl.last_write_at(turns) == T0 - timedelta(hours=2)
    assert nl.last_turn_at(turns) == T0 - timedelta(minutes=10)
    assert nl.keepalive_due(nl.last_turn_at(turns), T0) is False
    assert nl.last_write_at([(T0, nl.KEEPALIVE)]) is None
    assert nl.last_turn_at([]) is None


def test_unknown_turn_kind_refused():
    with pytest.raises(ValueError):
        nl.last_write_at([(T0, "nap")])


NAIVE = datetime(2026, 11, 16, 3, 0)


def _day_beat(last_write_at):
    return nl.beat_due(night_open=False, session_alive=True, session_done=False, last_write_at=last_write_at, now=T0)


def test_clock_stepped_back_two_hours_does_not_keep_a_silent_holder_beaten():
    assert _day_beat(T0 + timedelta(hours=2)) is False


def test_clock_stepped_back_one_second_is_jitter_and_still_recent():
    assert _day_beat(T0 + timedelta(seconds=1)) is True
    assert _day_beat(T0 + nl.CLOCK_JITTER) is True
    assert _day_beat(T0 + nl.CLOCK_JITTER + timedelta(seconds=1)) is False


def test_clock_stepped_back_never_lapses_a_night_holder():
    assert nl.beat_due(night_open=True, session_alive=True, session_done=False,
                       last_write_at=T0 + timedelta(hours=2), now=T0) is True


def test_keepalive_is_due_when_the_clock_stepped_back_two_hours():
    assert nl.keepalive_due(T0 + timedelta(hours=2), T0) is True


def test_keepalive_one_second_of_jitter_is_not_due():
    assert nl.keepalive_due(T0 + timedelta(seconds=1), T0) is False
    assert nl.keepalive_due(T0 + nl.CLOCK_JITTER + timedelta(seconds=1), T0) is True


@pytest.mark.parametrize("fn", [nl.last_write_at, nl.last_turn_at])
def test_an_iterator_is_refused_not_silently_drained(fn):
    turns = [(T0, nl.WRITE)]
    with pytest.raises(TypeError, match="turns"):
        fn(iter(turns))
    with pytest.raises(TypeError, match="turns"):
        fn(t for t in turns)
    assert fn(tuple(turns)) == fn(list(turns)) == T0


def test_a_naive_datetime_is_refused_naming_the_argument():
    with pytest.raises(TypeError, match="now"):
        nl.keepalive_due(T0, NAIVE)
    with pytest.raises(TypeError, match="last_turn_at"):
        nl.keepalive_due(NAIVE, T0)
    with pytest.raises(TypeError, match="now"):
        nl.keepalive_due(None, NAIVE)
    with pytest.raises(TypeError, match="now"):
        nl.beat_due(night_open=True, session_alive=True, session_done=False, last_write_at=None, now=NAIVE)
    with pytest.raises(TypeError, match="last_write_at"):
        nl.beat_due(night_open=True, session_alive=True, session_done=False, last_write_at=NAIVE, now=T0)
    with pytest.raises(TypeError, match="now"):
        nl.beat_due(night_open=False, session_alive=True, session_done=False, last_write_at=NAIVE, now=NAIVE)
    for fn in (nl.last_write_at, nl.last_turn_at):
        with pytest.raises(TypeError, match="turn"):
            fn([(NAIVE, nl.WRITE)])
        with pytest.raises(TypeError, match="turn"):
            fn([(T0, nl.WRITE), (NAIVE, nl.KEEPALIVE)])


def test_a_non_positive_window_is_refused():
    with pytest.raises(ValueError, match="window"):
        nl.beat_due(night_open=False, session_alive=True, session_done=False, last_write_at=T0, now=T0,
                    window=timedelta(seconds=-1))


def test_module_reads_no_store_and_imports_no_io():
    src = Path(nl.__file__).read_text(encoding="utf-8")
    mods = set()
    for n in ast.walk(ast.parse(src)):
        if isinstance(n, ast.Import):
            mods |= {a.name for a in n.names}
        elif isinstance(n, ast.ImportFrom):
            mods.add(n.module or "")
    assert not {m for m in mods if m.split(".")[0] in {"sqlite3", "subprocess", "os", "time"} or m.startswith("levain.desk.store")}
    assert "datetime.now" not in src and "time.time" not in src

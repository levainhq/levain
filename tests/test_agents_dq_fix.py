"""1111-38 D1-Q fix round (board row 1689): the review findings on the tick's dream launch, each written red first.

* the ceiling span of a SCHEDULED launch is the row's own window (codex HIGH, L2 MED, deepseek MED);
* the launch's set-up is guarded like each row (complement MED, codex MED, L2 LOW);
* a launch that answers ``unclassified`` is not a clean pass (all three lineages);
* a launched dream row no fire claimed has an owner: re-spawned past its start grace, ended through ``takeover`` once
  its launch lease lapses (L2 HIGH).

The rig is the one ``tests/test_agents_tick_launch.py`` uses: a real agent store, a real signed link, the dream's install.
"""
from __future__ import annotations

import shutil
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

import tests.test_agents_launch as launch_tests
from levain.agents import controller as ctl
from levain.agents import schedule
from levain.agents import tick_driver as T
from levain.dream import agent as da
from levain.dream import start as st
from tests.test_agents_tick_launch import Spawns, _claim, _exec, _finish, _rows, hook  # noqa: F401 - hook is a fixture
from tests.test_dream_start import ZONE, _live_rig

pytestmark = pytest.mark.skipif(shutil.which("ssh-keygen") is None, reason="needs ssh-keygen")

UTC = timezone.utc
MIN = timedelta(minutes=1)
NIGHT = {"start": "22:00", "end": "05:00"}


def _tick(rig, at, spawn, **kw):
    return T.run_tick(rig.store, clock=lambda: at, authority=rig.authority, spawn=spawn, **kw)


# ---------------------------------------------------- (2) a scheduled launch counts its ceiling over its own night

@pytest.fixture
def nightly(monkeypatch):
    """The rig's spec scheduled the dream's way: hourly, inside the 22:00-05:00 window, so a slot's window key is the
    night it belongs to (``w:<date>T22:00``)."""
    real = launch_tests._spec

    def spec(cron="0 * * * *", name=None):
        d = real(cron="0 * * * *", name=name)
        d["trigger"]["window"] = dict(NIGHT)
        return d

    monkeypatch.setattr(launch_tests, "_spec", spec)


def _tomorrow(rig) -> datetime:
    """Local midnight starting tomorrow in the rig's zone: every instant used below is after the link was signed."""
    local = rig.now().astimezone(ZoneInfo(ZONE))
    return datetime(local.year, local.month, local.day, tzinfo=ZoneInfo(ZONE)) + timedelta(days=1)


def _hand_run(rig, at):
    oid = _claim(rig, f"hand-{at:%H%M}", kind="manual", at=at, window=st.manual_window_key(at, ZONE))
    res = ctl.launch(rig.store, oid, authority=rig.authority, window=st.window_for(at, ZONE), now=at,
                     executor=_exec(rig, oid), on_input=None)
    assert res.outcome == "launched", res
    _finish(rig, oid)


def _scheduled(rig):
    return [r for r in _rows(rig) if r["kind"] == "scheduled"]


def test_a_hand_run_at_23_counts_against_the_midnight_slot_of_the_same_night(tmp_path, nightly, hook):
    rig = _live_rig(tmp_path, run_ceiling=1)
    day = _tomorrow(rig)
    _hand_run(rig, day + timedelta(hours=23))                      # 23:00 local, inside the night that opens at 22:00
    midnight = day + timedelta(days=1)
    spawn = Spawns()
    assert _tick(rig, midnight - 10 * MIN, spawn) == T.EXIT_OK
    _tick(rig, midnight + MIN, spawn)
    (row,) = _scheduled(rig)
    assert row["window_key"] == f"w:{day.date().isoformat()}T22:00", row
    assert (row["state"], row["reason"]) == ("skipped-budget", "ceiling"), row
    assert spawn.calls == []


def test_a_daytime_hand_run_does_not_count_against_the_night(tmp_path, nightly, hook):
    rig = _live_rig(tmp_path, run_ceiling=1)
    day = _tomorrow(rig)
    _hand_run(rig, day + timedelta(hours=10))                      # 10:00 local: outside 22:00-05:00
    eleven = day + timedelta(hours=23)
    spawn = Spawns()
    assert _tick(rig, eleven - 10 * MIN, spawn) == T.EXIT_OK
    _tick(rig, eleven + MIN, spawn)
    (row,) = _scheduled(rig)
    assert row["state"] == "launched", row
    assert len(spawn.calls) == 1


def test_the_window_a_scheduled_launch_is_given_is_the_one_its_window_key_names(tmp_path, nightly, hook, monkeypatch):
    rig = _live_rig(tmp_path)
    seen = []
    real = ctl.launch

    def spy(store, oid, **kw):
        seen.append(kw["window"])
        return real(store, oid, **kw)

    monkeypatch.setattr(ctl, "launch", spy)
    day = _tomorrow(rig)
    one = day + timedelta(days=1, hours=1)                         # 01:00 local: the night that opened at 22:00 before
    _tick(rig, one - 10 * MIN, Spawns())
    _tick(rig, one + MIN, Spawns())
    (window,) = seen
    z = ZoneInfo(ZONE)
    opened = datetime(day.year, day.month, day.day, 22, tzinfo=z).astimezone(UTC)
    closes = datetime((day + timedelta(days=1)).year, (day + timedelta(days=1)).month, (day + timedelta(days=1)).day, 5,
                      tzinfo=z).astimezone(UTC)
    assert window == ctl.Window(opened.strftime("%Y-%m-%dT%H:%M:%SZ"), closes.strftime("%Y-%m-%dT%H:%M:%SZ"), None)


def test_window_span_is_window_keys_inverse():
    trigger = {"kind": "schedule", "cron": "0 * * * *", "zone": ZONE, "window": dict(NIGHT)}
    z = ZoneInfo(ZONE)
    for local in (datetime(2026, 11, 12, 22, 0, tzinfo=z), datetime(2026, 11, 13, 4, 59, tzinfo=z),
                  datetime(2026, 3, 8, 1, 0, tzinfo=z)):                  # a spring-forward night too
        key = schedule.window_key(trigger, ZONE, local)
        lo, hi = schedule.window_span(trigger, ZONE, key)
        assert lo <= local < hi and hi - lo <= timedelta(hours=8)
        assert schedule.window_key(trigger, ZONE, lo) == key
    plain = {"kind": "schedule", "cron": "*/30 * * * *", "zone": ZONE, "window": None}
    lo, hi = schedule.window_span(plain, ZONE, "d:2026-11-12")
    assert (lo, hi) == (datetime(2026, 11, 12, tzinfo=z).astimezone(UTC), datetime(2026, 11, 13, tzinfo=z).astimezone(UTC))
    for bad in ("m:2026-11-12", "w:2026-11-12T21:00", "w:2026-13-12T22:00", "d:2026-11-12"):
        with pytest.raises(ValueError):
            schedule.window_span(trigger, ZONE, bad)


# ------------------------------------------------- (3) the launch's set-up is guarded like each row is

class Notices:
    """``tick_notices`` replaced by one that records that it ran and reports nothing."""

    def __init__(self):
        self.ran = 0

    def __call__(self, *a, **kw):
        from types import SimpleNamespace

        self.ran += 1
        return SimpleNamespace(notes=[], problems=[])


def _slot(rig) -> datetime:
    t = rig.now().astimezone(UTC).replace(second=0, microsecond=0) + timedelta(minutes=10)
    return t + timedelta(minutes=(30 - t.minute % 30) % 30)


def test_a_set_up_failure_before_the_rows_is_named_and_costs_the_pass_nothing_else(tmp_path, hook, monkeypatch, capsys):
    from levain.agents import spec as spec_mod

    rig = _live_rig(tmp_path)
    slot, spawn = _slot(rig), Spawns()
    assert _tick(rig, slot - 10 * MIN, spawn) == T.EXIT_OK
    notices = Notices()
    monkeypatch.setattr(da, "tick_notices", notices)
    real = spec_mod.get_agent

    def locked(store, ref, version=None):
        if ref == da.NAME:
            raise sqlite3.OperationalError("database is locked")
        return real(store, ref, version)

    monkeypatch.setattr(spec_mod, "get_agent", locked)
    code = _tick(rig, slot + MIN, spawn)                     # before the fix: the OperationalError escaped run_tick
    assert code == T.EXIT_LAUNCH
    assert notices.ran == 1                                  # the notices still ran
    err = capsys.readouterr().err
    assert "database is locked" in err and "OperationalError" in err
    (row,) = _rows(rig)
    assert row["state"] == "admitted" and spawn.calls == []  # retried next tick, until its grace


# ------------------------------------------------- (4) an unclassified launch is not a clean pass; a full cap is

def test_an_install_zone_this_host_does_not_know_is_a_named_launch_failure_not_a_traceback(tmp_path, monkeypatch,
                                                                                           capsys):
    import json

    rig = _live_rig(tmp_path)
    path = Path(rig.store.directory) / da.INSTALL_FILE
    doc = json.loads(path.read_text())
    doc["zone"] = "Mars/Olympus"
    path.write_text(json.dumps(doc))
    monkeypatch.setattr(da, "tick_notices", Notices())
    slot, spawn = _slot(rig), Spawns()
    assert _tick(rig, slot - 10 * MIN, spawn) == T.EXIT_OK
    code = _tick(rig, slot + MIN, spawn)
    assert code == T.EXIT_LAUNCH, code
    (row,) = _rows(rig)
    assert row["state"] == "admitted" and spawn.calls == []
    err = capsys.readouterr().err
    assert row["occurrence_id"] in err and "unclassified" in err


def test_a_launch_that_answers_unclassified_exits_6_and_a_full_cap_does_not(tmp_path, monkeypatch):
    from levain.agents import slots

    rig = _live_rig(tmp_path)

    def dead(agent_dir, *, clock=None):
        def check(row):
            raise RuntimeError("the wrap cannot be read")
        return check

    monkeypatch.setattr(da, "install_on_input", dead)
    monkeypatch.setattr(da, "tick_notices", Notices())
    slot, spawn = _slot(rig), Spawns()
    assert _tick(rig, slot - 10 * MIN, spawn) == T.EXIT_OK
    assert _tick(rig, slot + MIN, spawn) == T.EXIT_LAUNCH            # the dream never ran: exit 6
    monkeypatch.setattr(da, "install_on_input", lambda agent_dir, *, clock=None: (lambda row: True))
    with rig.store.write() as c:
        slots.take(c, slots.LAUNCH, "ln-a", slot)
        slots.take(c, slots.LAUNCH, "ln-b", slot)
    assert _tick(rig, slot + 2 * MIN, spawn) == T.EXIT_OK            # a full cap is normal: quiet
    (row,) = _rows(rig)
    assert row["state"] == "admitted" and spawn.calls == []

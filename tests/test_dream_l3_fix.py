"""The 1111-38 fix round of the dream stack (D1-N, D1-M, D1-O): the L3 review's two codex MEDs on the hand start, and
the L2 trust review's text findings (F4 the signing card, F5 the wrap-open card's enable line, F6/F7 the remedies that
said "nothing needs doing by hand" for classes that need a person, F9 the own-wrap admission note).

Every store is real, as in ``tests/test_dream_start.py`` (whose rig this reuses); nothing composes and no model is
called."""
from __future__ import annotations

import re
import shlex
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from levain import cli as levain_cli
from levain.dream import agent as da
from levain.dream import cli as dcli
from levain.dream import ready as rd
from levain.dream import start as st
from tests.test_cockpit_anchor_reset import _verdict
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


def test_c_the_signing_card_says_which_open_wraps_the_signed_run_clears():
    # what the operator signs said the dream runs only when "no other consolidation is in progress", while the signed
    # run clears a wrap it proves dead and, under an unsigned age floor, a seat's wrap
    card = " ".join(da.signing_card_text(ZONE, per_run_usd=20.0, night_usd=40.0, expires="E", store_dir="/s").split())
    assert "It clears an open wrap it proves dead" in card, card
    assert "older than the executor's age bound" in card, card
    assert "a seat's wrap only if you have set an age floor" in card, card
    assert "which is not part of what you sign" in card, card


def test_c_the_card_text_is_not_part_of_the_signed_spec():
    from levain.agents import role as _role
    from levain.agents import spec as _spec

    role = _role.parse_role(da.dream_role())
    spec = da.dream_spec(_role.role_hash(role))
    words = " ".join(map(str, _walk(spec)))
    assert "proves dead" not in words and "age floor" not in words


def _walk(obj):
    if isinstance(obj, dict):
        for v in obj.values():
            yield from _walk(v)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            yield from _walk(v)
    else:
        yield obj


# ------------------------------------------- D (L2 F5): the wrap-open card's enable line names THIS install's flags

def _enable_cmd(text: str) -> str:
    (cmd,) = [c for c in re.findall(r"`([^`]+)`", text) if c.startswith("levain dream enable ")]
    return cmd


def test_d_a_non_default_install_gets_an_enable_line_that_keeps_its_facts(tmp_path, monkeypatch):
    monkeypatch.setenv("LEVAIN_HOME", str(tmp_path / "home"))       # the defaults are elsewhere than this install
    rig = _live_rig(tmp_path)
    inst = da.read_install(rig.store.directory)
    cmd = _enable_cmd(da.window_remedy("wrap-open", rig.db, agent_dir=rig.store.directory))
    assert _verdict(cmd, {}) is None, cmd
    args = levain_cli.build_parser().parse_args(shlex.split(cmd)[1:])
    assert args.orphan_floor_hours == "<hours>"
    assert Path(args.agent_store_dir).resolve() == Path(rig.store.directory).resolve(), cmd
    got = dcli._install_kwargs(args, ZONE, da)                       # what the pasted line would write
    assert got["store_path"] == inst.store_path.resolve(), cmd
    assert got["cockpit_root"] == inst.cockpit_root.resolve(), cmd
    assert got["coord_dir"] == inst.coord_dir.resolve(), cmd


def test_d_the_default_install_keeps_the_short_line(tmp_path, monkeypatch):
    from anneal_memory import FLOW_SCHEMA, Store

    from levain.desk.launch import default_desk_dir

    home = tmp_path / "home"
    monkeypatch.setenv("LEVAIN_HOME", str(home))
    agent_dir = home / "autonomic"
    agent_dir.mkdir(parents=True)
    (home / "cockpit").mkdir()
    default_desk_dir().mkdir(mode=0o700)
    db = tmp_path / "st" / "memory.db"
    db.parent.mkdir()
    with Store(str(db), section_schema=FLOW_SCHEMA):
        pass
    da.write_install(agent_dir, store_path=db, cockpit_root=home / "cockpit", coord_dir=default_desk_dir(), zone=ZONE)
    cmd = _enable_cmd(da.window_remedy("wrap-open", db, agent_dir=agent_dir))
    assert cmd == f"levain dream enable --store-dir {shlex.quote(str(db.parent))} --orphan-floor-hours <hours>", cmd


def test_d_the_tick_gives_the_card_the_install_it_closes_for(tmp_path, monkeypatch):
    rig = _live_rig(tmp_path)
    seen = []
    monkeypatch.setattr(da, "close_window", lambda **kw: seen.append(kw) or da.CloseResult("waiting", "w:x"))
    da.tick_notices(rig.store, now=rig.now(), attention=lambda k, t: None, resolve=lambda k: None,
                    drill=lambda *a, **k: None)
    assert seen and all(Path(kw["agent_dir"]) == Path(rig.store.directory) for kw in seen), seen
    # the facts the tick read once, not a second read per card
    assert all(kw["install"] == da.read_install(rig.store.directory) for kw in seen), seen


def test_a_todays_admitted_manual_row_is_still_the_one_resumed(tmp_path):
    rig = _live_rig(tmp_path)
    left = _claim(rig, "left-admitted-today")
    got, spawned = _go(rig)
    assert (got.outcome, got.resumed, got.occurrence_id) == ("launched", True, left), got
    assert len(_rows(rig)) == 1

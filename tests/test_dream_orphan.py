"""D1-O: orphan wrap healing at admission (D1_design2 row 4; Phill's signed Q4 (a)).

The failure is the one the L4 run reproduced (scratch store ``j2``): a seat killed between ``prepare_wrap`` and its
save leaves its wrap open, admission is QUIET ``wrap-open`` on every night, and the executor, which already clears a
dead wrap, is never reached. Every test here makes that condition with the real pieces (a real anneal store, a real
wrap aged in the store's metadata the way the L4 run aged it, the real executor with a stand-in compose mind) and never
touches ``~/.anneal-memory``, an app or a live seat.

The rule under test: admission passes an open wrap only when the executor's own age function says it is dead.
* a wrap the dream did NOT open: only while the operator has set the age floor (it ships UNSET, so unset is today's
  behaviour), and only when the wrap is past both the executor's bound and the floor;
* a wrap the dream itself opened (an occurrence's ``progress.json`` names its token): when the executor's bound says
  it is dead, no floor needed.
The executor stays the only code that discards.
"""
from __future__ import annotations

import argparse
import json
import math
import shlex
import sqlite3
from datetime import date, datetime, timedelta, timezone

import pytest

from levain.agents import store as agent_store
from levain.dream import agent as da
from levain.dream import executor as ex
from levain.dream import ready as rd
from tests.test_dream_agent import hold_a_wrap_open
from tests.test_dream_l4_fixes import age_the_open_wrap
from tests.test_dream_ready import DAY, NOW, ZONE, Entity, ent  # noqa: F401
from tests.test_dream_snapshot import _VALID, dream

HOUR = 3600.0
#: the executor's own horizon, read from the code (wrap._orphan_horizon): DREAM_BUDGET_S + grace + margin
HORIZON_S = ex.DREAM_BUDGET_S + 30.0 + 300.0


def real_now() -> datetime:
    """The wrap is aged against the REAL clock in the store's metadata, so admission is asked at the real clock."""
    return datetime.now(timezone.utc)


def evaluate(ent, floor_hours=None, *, manual=False, now=None):
    """The scheduled admission over the entity, with the operator's floor (hours) the way day_ready passes it."""
    now = now or real_now()
    src, close = rd.sources_for(ent.db, agent_dir=ent.agent_dir, coord_dir=ent.coord_dir, now=now)
    try:
        return rd.evaluate(src, day=DAY, now=now, zone=ZONE, manual=manual,
                           orphan_floor_s=None if floor_hours is None else floor_hours * HOUR)
    finally:
        close()


def age_the_wrap_to(db, seconds_ago: float) -> None:
    conn = sqlite3.connect(str(db))
    conn.create_function("anneal_writer_schema", -1, lambda *a: 2)
    old = (datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    assert conn.execute("update metadata set value=? where key='wrap_started_at'", (old,)).rowcount == 1
    conn.commit()
    conn.close()


def wrap_state(db):
    from anneal_memory import Store

    with Store(str(db), section_schema=None) as s:
        return s.get_wrap_started_at(), s.last_wrap_id(), len(s.episodes_since_wrap())


# ------------------------------------------------------------------------------------ the reproduced L4 case (j2)

def test_unset_floor_a_seats_aged_orphan_keeps_the_dream_quiet_as_today(ent):
    hold_a_wrap_open(ent)
    age_the_open_wrap(ent.db, days=3)
    for manual in (False, True):
        v = evaluate(ent, None, manual=manual)
        assert (v.verdict, v.check, v.kind) == (rd.QUIET, 2, "wrap-open"), v.reason
        # the L4 fix lane's honest text is still the head of the reason; the tail says why nothing passed here
        assert "the dream does not start while one is open, a dead seat's included" in v.reason
        assert "no age floor is set" in v.reason and "--orphan-floor-hours" in v.reason, v.reason


def test_a_floor_below_the_wraps_age_passes_admission_and_the_run_clears_it_and_consolidates(ent):
    hold_a_wrap_open(ent)
    age_the_open_wrap(ent.db, days=3)
    v = evaluate(ent, 24)                                    # a 24-hour floor; the seat's wrap is 72 hours old
    assert v.verdict == rd.READY, v.reason
    c2 = next(c for c in v.checks if c.n == 2)
    assert c2.status == "pass" and "proven dead" in c2.detail and "the run clears it" in c2.detail, c2.detail
    started, last_wrap, _ = wrap_state(ent.db)
    assert started is not None                               # admission discarded NOTHING: the wrap is still open
    r = dream(ent.db, "o1", _VALID)                          # the executor, the only judge that acts
    assert r.outcome == "saved", r
    started_after, last_wrap_after, since = wrap_state(ent.db)
    assert started_after is None and last_wrap_after == (last_wrap or 0) + 1 and since == 0
    # the next night finds nothing open and nothing new
    assert evaluate(ent, 24).kind in ("", "nothing-to-consolidate", "already-dreamed", "consolidated-today")


def test_a_wrap_younger_than_the_floor_still_refuses(ent):
    hold_a_wrap_open(ent)
    age_the_open_wrap(ent.db, days=2)                        # 48 hours: past the executor's bound, under a 72-hour floor
    v = evaluate(ent, 72)
    assert (v.verdict, v.check, v.kind) == (rd.QUIET, 2, "wrap-open"), v.reason
    assert "not yet" in v.reason and "72" in v.reason, v.reason
    assert wrap_state(ent.db)[0] is not None


def test_a_wrap_younger_than_the_executors_own_bound_refuses_even_at_the_lowest_floor(ent):
    hold_a_wrap_open(ent)
    age_the_wrap_to(ent.db, HORIZON_S - 600)                 # ten minutes short of the executor's bound
    v = evaluate(ent, HORIZON_S / HOUR)                      # the lowest floor the setting allows
    assert (v.verdict, v.check, v.kind) == (rd.QUIET, 2, "wrap-open"), v.reason
    age_the_wrap_to(ent.db, HORIZON_S + 600)                 # ten minutes past it
    assert evaluate(ent, HORIZON_S / HOUR).verdict == rd.READY


def test_a_floor_never_lowers_the_executors_bound(ent):
    """A floor under the executor's horizon would pass a wrap the executor then refuses (a failed run that spends the
    window's one attempt): the effective floor is the larger of the two, so even a hand-written 0.1 hour passes nothing
    the executor would refuse."""
    hold_a_wrap_open(ent)
    age_the_wrap_to(ent.db, 1800)                            # 30 minutes
    assert evaluate(ent, 0.1).verdict == rd.QUIET


def test_an_unreadable_start_time_never_passes(ent):
    hold_a_wrap_open(ent)
    age_the_open_wrap(ent.db, days=3)

    def patch(src):
        src.open_wrap = lambda: ("a" * 32, "not a time")
    now = real_now()
    src, close = rd.sources_for(ent.db, agent_dir=ent.agent_dir, coord_dir=ent.coord_dir, now=now)
    try:
        patch(src)
        v = rd.evaluate(src, day=DAY, now=now, zone=ZONE, orphan_floor_s=24 * HOUR)
    finally:
        close()
    assert (v.verdict, v.check, v.kind) == (rd.QUIET, 2, "wrap-open"), v.reason


# ------------------------------------------------------------------------- a wrap the dream itself opened

def dream_left_a_wrap(ent, token="a" * 32, occurrence="occ-dead"):
    """An occurrence whose progress.json (the real file shape, written by the executor's own Progress class) names the
    wrap now open: the dream's compose died after prepare_wrap and its cancel did not land."""
    prog = ex.Progress(ent.store_dir / "dream" / occurrence)
    (ent.store_dir / "dream" / occurrence).mkdir(parents=True)
    assert prog.update(wrap_token=token, phase="composing")


def test_a_wrap_the_dream_opened_passes_on_the_executors_bound_without_any_floor(ent):
    hold_a_wrap_open(ent)                                    # token "a" * 32
    dream_left_a_wrap(ent)
    age_the_wrap_to(ent.db, HORIZON_S + 600)
    v = evaluate(ent, None)                                  # the floor is UNSET
    assert v.verdict == rd.READY, v.reason
    c2 = next(c for c in v.checks if c.n == 2)
    assert "the dream's own" in c2.detail and "occ-dead" in c2.detail, c2.detail
    r = dream(ent.db, "o2", _VALID)
    assert r.outcome == "saved", r


def test_a_wrap_the_dream_opened_but_younger_than_the_bound_still_refuses(ent):
    hold_a_wrap_open(ent)
    dream_left_a_wrap(ent)
    age_the_wrap_to(ent.db, HORIZON_S - 600)
    v = evaluate(ent, None)
    assert (v.verdict, v.check, v.kind) == (rd.QUIET, 2, "wrap-open"), v.reason
    assert "the dream's own" in v.reason and "younger" in v.reason, v.reason


def test_a_progress_file_naming_another_token_does_not_make_a_seats_wrap_the_dreams(ent):
    hold_a_wrap_open(ent)                                    # the open wrap's token is "a" * 32
    dream_left_a_wrap(ent, token="b" * 32)                   # an old dream's progress: a different wrap
    age_the_open_wrap(ent.db, days=3)
    v = evaluate(ent, None)
    assert (v.verdict, v.check, v.kind) == (rd.QUIET, 2, "wrap-open"), v.reason
    assert evaluate(ent, 24).verdict == rd.READY             # the floor, not the dream's attribution, passed it


def test_an_unreadable_progress_file_is_no_attribution(ent):
    hold_a_wrap_open(ent)
    (ent.store_dir / "dream" / "occ-x").mkdir(parents=True)
    (ent.store_dir / "dream" / "occ-x" / "progress.json").write_text("{not json", encoding="utf-8")
    age_the_open_wrap(ent.db, days=3)
    assert evaluate(ent, None).verdict == rd.QUIET


# ------------------------------------------------------------------------ the setting: one named install fact

def facts(ent, tmp_path, **kw):
    cockpit = tmp_path / "cockpit"
    cockpit.mkdir(exist_ok=True)
    return da.write_install(ent.agent_dir, store_path=ent.db, cockpit_root=cockpit, coord_dir=ent.coord_dir, zone=ZONE,
                            **kw)


def test_the_floor_ships_unset(ent, tmp_path):
    assert da.DreamInstall.__dataclass_fields__["orphan_floor_hours"].default is None
    facts(ent, tmp_path)
    assert da.read_install(ent.agent_dir).orphan_floor_hours is None
    assert da.orphan_floor_s(ent.agent_dir) is None
    assert "orphan_floor_hours" not in json.loads((ent.agent_dir / da.INSTALL_FILE).read_text(encoding="utf-8"))


def test_the_floor_is_written_read_kept_and_cleared(ent, tmp_path):
    facts(ent, tmp_path, orphan_floor_hours=24)
    assert da.read_install(ent.agent_dir).orphan_floor_hours == 24
    assert da.orphan_floor_s(ent.agent_dir) == 24 * HOUR
    facts(ent, tmp_path)                                     # a re-run that does not pass it keeps it
    assert da.orphan_floor_s(ent.agent_dir) == 24 * HOUR
    facts(ent, tmp_path, orphan_floor_hours=da.ORPHAN_FLOOR_OFF)
    assert da.orphan_floor_s(ent.agent_dir) is None


@pytest.mark.parametrize("bad", [0, -1, 0.5, 0.9, math.nan, math.inf, True, "24"])
def test_a_floor_that_cannot_work_is_refused_before_anything_is_written(ent, tmp_path, bad):
    with pytest.raises(da.InstallRefused) as exc:
        facts(ent, tmp_path, orphan_floor_hours=bad)
    assert "orphan_floor_hours" in str(exc.value)
    assert not (ent.agent_dir / da.INSTALL_FILE).exists()


def test_a_hand_written_bad_floor_reads_as_unset_for_admission(ent, tmp_path):
    facts(ent, tmp_path)
    path = ent.agent_dir / da.INSTALL_FILE
    doc = json.loads(path.read_text(encoding="utf-8"))
    for bad in (0.1, -5, "24", True, None):
        path.write_text(json.dumps({**doc, "orphan_floor_hours": bad}), encoding="utf-8")
        assert da.orphan_floor_s(ent.agent_dir) is None, bad


def test_day_ready_reads_the_floor_from_the_install_beside_the_agent_store(ent, tmp_path):
    hold_a_wrap_open(ent)
    age_the_open_wrap(ent.db, days=3)
    occ_row = {"agent_id": "dream", "occurrence_id": "occ-now", "kind": "scheduled", "window_key": rd.window_key_for(DAY)}
    kw = dict(now=real_now(), zone=ZONE, agent_dir=ent.agent_dir, coord_dir=ent.coord_dir)
    facts(ent, tmp_path)
    assert da.day_ready(ent.db, occ_row, **kw).ok is False
    facts(ent, tmp_path, orphan_floor_hours=24)
    v = da.day_ready(ent.db, occ_row, **kw)
    assert v.ok is True, v.reason
    assert da.bool_on_input(lambda row: da.day_ready(ent.db, row, **kw))(occ_row) is True
    facts(ent, tmp_path, orphan_floor_hours=da.ORPHAN_FLOOR_OFF)
    assert da.day_ready(ent.db, occ_row, **kw).ok is False


# --------------------------------------------------------------- the person-run command, the number in its output

def run_enable(tmp_path, capsys, *extra, as_json=False):
    from levain.dream.cli import add_parsers

    store_dir = tmp_path / "ag"
    agent_store.set_default_zone(agent_store.open_agent_store(store_dir), ZONE)
    p = argparse.ArgumentParser()
    add_parsers(p.add_subparsers())
    args = p.parse_args(["dream", "enable", "--agent-store-dir", str(store_dir), *(["--json"] if as_json else []), *extra])
    code = args.func(args)
    cap = capsys.readouterr()
    return code, cap.out, cap.err, store_dir


def enable_args(ent, tmp_path):
    cockpit = tmp_path / "cockpit"
    cockpit.mkdir(exist_ok=True)
    return ["--store-dir", str(ent.store_dir), "--cockpit-root", str(cockpit), "--coord-dir", str(ent.coord_dir)]


def test_enable_sets_shows_keeps_and_clears_the_floor(ent, tmp_path, capsys):
    base = enable_args(ent, tmp_path)
    code, out, err, ag = run_enable(tmp_path, capsys, *base)
    assert code == 0, err
    assert "orphan wrap floor: unset" in out and "off" in out, out          # shipped unset, and it says what that means
    code, out, err, ag = run_enable(tmp_path, capsys, *base, "--orphan-floor-hours", "24")
    assert code == 0, err
    assert "orphan wrap floor: 24 hours" in out, out
    code, out, err, ag = run_enable(tmp_path, capsys, *base, as_json=True)    # a re-run keeps it, and the JSON carries it
    assert code == 0 and json.loads(out)["orphan_floor_hours"] == 24, (out, err)
    assert da.orphan_floor_s(ag) == 24 * HOUR
    code, out, err, ag = run_enable(tmp_path, capsys, *base, "--orphan-floor-hours", "off")
    assert code == 0 and "orphan wrap floor: unset" in out, (out, err)
    assert da.orphan_floor_s(ag) is None


def test_enable_refuses_a_floor_below_the_executors_bound_and_writes_nothing(ent, tmp_path, capsys):
    code, out, err, ag = run_enable(tmp_path, capsys, *enable_args(ent, tmp_path), "--orphan-floor-hours", "0.5")
    assert code == 1 and "orphan_floor_hours" in err and "nothing was registered" in err, (out, err)
    assert not (ag / da.INSTALL_FILE).exists()


def test_enable_refuses_a_floor_without_store_dir(tmp_path, capsys):
    code, out, err, ag = run_enable(tmp_path, capsys, "--orphan-floor-hours", "24")
    assert code == 1 and "--store-dir" in err and "nothing was registered" in err, (out, err)
    assert not (ag / da.INSTALL_FILE).exists()


def test_the_ready_verb_shows_the_floor_it_was_given_from_the_agent_dir(ent, tmp_path, capsys):
    from levain.dream.cli import add_parsers

    hold_a_wrap_open(ent)
    age_the_open_wrap(ent.db, days=3)
    facts(ent, tmp_path, orphan_floor_hours=24)
    p = argparse.ArgumentParser()
    add_parsers(p.add_subparsers())
    args = p.parse_args(["dream", "ready", "--store-dir", str(ent.store_dir), "--agent-dir", str(ent.agent_dir),
                         "--coord-dir", str(ent.coord_dir), "--zone", ZONE, "--day", DAY.isoformat(),
                         "--now", real_now().isoformat()])
    assert args.func(args) == 0
    out = capsys.readouterr().out
    assert "ready for dream-day" in out and "proven dead" in out and "24" in out, out

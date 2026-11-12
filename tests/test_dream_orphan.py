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

#: check 2's refusal detail for an open wrap, byte for byte as the branch before D1-O printed it
#: (``git show seat/1111-36-d1l1-fix:levain/dream/ready.py``, ``_check2``). With the floor UNSET a seat's wrap must read
#: exactly this, in the card body and in ``levain dream ready --json``: unset is the old behaviour, text included.
OLD_OPEN_WRAP_DETAIL = ("a wrap is open on the store: the dream does not start while one is open, a dead seat's included, "
                        "and nothing on this path clears it (anneal's wrap-status and wrap-cancel do)")
OLD_OPEN_WRAP_REASON = "check 2 (baton free, no open wrap): " + OLD_OPEN_WRAP_DETAIL


def test_unset_floor_a_seats_aged_orphan_keeps_the_dream_quiet_as_today(ent):
    hold_a_wrap_open(ent)
    age_the_open_wrap(ent.db, days=3)
    for manual in (False, True):
        v = evaluate(ent, None, manual=manual)
        assert (v.verdict, v.check, v.kind) == (rd.QUIET, 2, "wrap-open"), v.reason
        # byte for byte: no tail, no new head (what a seat or the card reads when the floor is unset)
        assert v.reason == OLD_OPEN_WRAP_REASON
        assert next(c for c in v.checks if c.n == 2).detail == OLD_OPEN_WRAP_DETAIL
        assert json.dumps(v.as_dict(), sort_keys=True).count(OLD_OPEN_WRAP_DETAIL) == 2   # reason + check detail


def test_unset_floor_an_open_wrap_that_cannot_be_read_is_still_the_old_text_and_still_quiet(ent):
    """Whatever cannot be judged, with the floor unset the answer and its words are the old ones."""
    hold_a_wrap_open(ent)
    age_the_open_wrap(ent.db, days=3)
    now = real_now()
    for how in ("raises", "no source", "bad time"):
        src, close = rd.sources_for(ent.db, agent_dir=ent.agent_dir, coord_dir=ent.coord_dir, now=now)
        try:
            if how == "raises":
                def boom():
                    raise RuntimeError("wrap metadata unreadable")
                src.open_wrap = boom
            elif how == "no source":
                src.open_wrap = None
            else:
                src.open_wrap = lambda: ("a" * 32, "not a time")
            v = rd.evaluate(src, day=DAY, now=now, zone=ZONE)
        finally:
            close()
        assert (v.verdict, v.check, v.kind) == (rd.QUIET, 2, "wrap-open"), (how, v.reason)
        assert v.reason == OLD_OPEN_WRAP_REASON, how


def test_with_a_floor_set_the_refusal_does_not_say_nothing_here_clears_it(ent):
    """The old head ("a dead seat's included, and nothing on this path clears it") is false once a floor is set: the run
    clears a wrap past the floor. A wrap still under the floor must read true, in the head as well as the tail."""
    hold_a_wrap_open(ent)
    age_the_open_wrap(ent.db, days=2)
    v = evaluate(ent, 72)
    assert (v.verdict, v.check, v.kind) == (rd.QUIET, 2, "wrap-open"), v.reason
    assert "nothing on this path clears it" not in v.reason and "a dead seat's included" not in v.reason, v.reason
    assert "must be older than 72 h" in v.reason and "its seat may still be alive" in v.reason, v.reason
    assert v.reason.startswith("check 2 (baton free, no open wrap): a wrap is open on the store"), v.reason


def test_the_dreams_own_young_wrap_does_not_say_nothing_here_clears_it_either(ent):
    hold_a_wrap_open(ent)
    dream_left_a_wrap(ent)
    age_the_wrap_to(ent.db, HORIZON_S - 600)
    for floor in (None, 24):
        v = evaluate(ent, floor)
        assert (v.verdict, v.check, v.kind) == (rd.QUIET, 2, "wrap-open"), v.reason
        assert "nothing on this path clears it" not in v.reason and "a dead seat's included" not in v.reason, v.reason
        assert "the dream's own" in v.reason and "occ-dead" in v.reason, v.reason


@pytest.mark.parametrize("floor", [None, 24])
@pytest.mark.parametrize("own", [False, True])
def test_a_failure_outside_the_guard_is_quiet_never_unreadable(ent, monkeypatch, floor, own):
    """The executor's bound is read lazily (``levain.dream.executor``). If that read raises, the open wrap is simply not
    proven dead: QUIET ``wrap-open``, never a class change to UNREADABLE from a bug in the new code."""
    hold_a_wrap_open(ent)
    if own:
        dream_left_a_wrap(ent)
    age_the_open_wrap(ent.db, days=3)

    def boom():
        raise ImportError("the executor module could not be loaded")
    monkeypatch.setattr(rd, "executor_orphan_bound_s", boom)
    v = evaluate(ent, floor)
    assert (v.verdict, v.check, v.kind) == (rd.QUIET, 2, "wrap-open"), v.reason
    assert next(c for c in v.checks if c.n == 2).status == rd.QUIET


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
    # the same night read again: check 2 now passes with no wrap open, and the answer is the one the saved wrap earns
    # (DAY is the fixed 11-02 and the wrap was made now, so check 3 sees a wrap since that day's start)
    again = evaluate(ent, 24)
    c2 = next(c for c in again.checks if c.n == 2)
    assert c2.status == "pass" and c2.detail == "no wrap open; the store does not require the baton", c2
    assert (again.verdict, again.check, again.kind) == (rd.QUIET, 3, "consolidated-today"), again.reason


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


def _own_wrap_past_the_bound(ent):
    """The control for the attribution tests below: a dream-left wrap, past the executor's bound, floor unset. It must
    read READY, so a QUIET after the progress file is damaged is the damage's doing and not a wrap that never qualified."""
    hold_a_wrap_open(ent)                                    # token "a" * 32
    dream_left_a_wrap(ent)
    age_the_wrap_to(ent.db, HORIZON_S + 600)
    v = evaluate(ent, None)
    assert v.verdict == rd.READY, v.reason
    return ent.store_dir / "dream" / "occ-dead" / "progress.json"


def test_a_progress_file_that_names_the_token_but_does_not_parse_is_no_attribution(ent):
    """The file really does name the open wrap's token, in text, and is cut off mid-field: a reader that scraped the token
    out of unparseable text would attribute the wrap and pass it. It must not."""
    path = _own_wrap_past_the_bound(ent)
    whole = path.read_text(encoding="utf-8")
    assert "a" * 32 in whole
    cut = whole[:whole.index("a" * 32) + 12]                 # the token cut off mid-value, inside an unclosed object
    assert "a" * 12 in cut and "a" * 32 not in cut
    path.write_text(cut, encoding="utf-8")
    v = evaluate(ent, None)
    assert (v.verdict, v.check, v.kind) == (rd.QUIET, 2, "wrap-open"), v.reason
    path.write_text(whole[:whole.rindex("}")], encoding="utf-8")          # whole token, object left unclosed
    v = evaluate(ent, None)
    assert (v.verdict, v.check, v.kind) == (rd.QUIET, 2, "wrap-open"), v.reason
    assert v.reason == OLD_OPEN_WRAP_REASON                  # a seat's wrap, floor unset: the old words
    path.write_text(whole, encoding="utf-8")                 # and the intact file attributes again
    assert evaluate(ent, None).verdict == rd.READY


def test_a_progress_file_that_is_a_symlink_is_no_attribution(ent, tmp_path):
    path = _own_wrap_past_the_bound(ent)
    real = tmp_path / "elsewhere.json"
    real.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")   # a readable file naming the live token
    path.unlink()
    path.symlink_to(real)
    v = evaluate(ent, None)
    assert (v.verdict, v.check, v.kind) == (rd.QUIET, 2, "wrap-open"), v.reason
    assert v.reason == OLD_OPEN_WRAP_REASON
    age_the_wrap_to(ent.db, 30 * HOUR)
    assert evaluate(ent, 24).verdict == rd.READY             # a floor still lets the operator's number pass it


def test_an_occurrence_directory_that_is_a_symlink_is_no_attribution(ent, tmp_path):
    path = _own_wrap_past_the_bound(ent)
    real_dir = tmp_path / "elsewhere"
    real_dir.mkdir()
    (real_dir / "progress.json").write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
    occ_dir = path.parent
    (occ_dir / "progress.json").unlink()
    occ_dir.rmdir()
    occ_dir.symlink_to(real_dir, target_is_directory=True)
    v = evaluate(ent, None)
    assert (v.verdict, v.check, v.kind) == (rd.QUIET, 2, "wrap-open"), v.reason


def test_a_progress_json_that_is_a_directory_is_no_attribution(ent):
    path = _own_wrap_past_the_bound(ent)
    path.unlink()
    path.mkdir()
    assert evaluate(ent, None).verdict == rd.QUIET


def test_a_dream_left_wrap_with_a_floor_set_passes_on_the_bound_not_the_floor(ent):
    """The floor is for a wrap the dream did NOT open. The dream's own wrap past the executor's bound but far younger than
    the floor still passes (the floor does not apply to it); one younger than the bound still refuses (the floor does not
    pass it)."""
    hold_a_wrap_open(ent)
    dream_left_a_wrap(ent)
    age_the_wrap_to(ent.db, 2 * HOUR)                        # past the 55.5 min bound, far under a 24-hour floor
    v = evaluate(ent, 24)
    assert v.verdict == rd.READY, v.reason
    assert "the dream's own" in next(c for c in v.checks if c.n == 2).detail
    age_the_wrap_to(ent.db, HORIZON_S - 600)                 # under the bound: not dead, floor or no floor
    for floor in (24, HORIZON_S / HOUR):
        v = evaluate(ent, floor)
        assert (v.verdict, v.check, v.kind) == (rd.QUIET, 2, "wrap-open"), v.reason
    age_the_wrap_to(ent.db, 2 * HOUR)
    assert da_run_saves(ent)                                 # and the run clears it and saves


def da_run_saves(ent) -> bool:
    return dream(ent.db, "o-own", _VALID).outcome == "saved"


def test_known_edge_a_progress_json_forged_into_any_directory_attributes_a_wrap_to_the_dream(ent):
    """KNOWN EDGE (D1-O L1 F1, ruled: not fixed here). Attribution reads plain files, and so does the age floor
    (``dream_install.json``): any process running as the same user can write one. A file under ``<store dir>/dream/<x>/``
    that names a live wrap's token makes admission treat that wrap as the dream's own and pass it past the executor's
    bound with no floor set. Reaching it needs a process already running as the user, past the boundary this code
    defends; signing the record is not built. This test pins the behaviour so closing the edge is a visible change."""
    hold_a_wrap_open(ent)
    forged = ent.store_dir / "dream" / "not-an-occurrence"
    forged.mkdir(parents=True)
    (forged / "progress.json").write_text(json.dumps({"wrap_token": "a" * 32}), encoding="utf-8")
    age_the_wrap_to(ent.db, HORIZON_S + 600)
    v = evaluate(ent, None)
    assert v.verdict == rd.READY and "not-an-occurrence" in next(c for c in v.checks if c.n == 2).detail, v.reason


def test_the_replaced_wrap_race_the_executor_refuses_and_the_attempt_is_spent(ent):
    """Admission passes a dead seat's old wrap (floor 24 h). Before the run takes ``wrap.lock`` the seat cancels it and
    opens a NEW wrap (younger than the bound). The executor, which judges again, refuses it and leaves it alone: the run
    ends failed (the occurrence row is simulated below: this composes the executor's refusal with check 6, it is not a
    run through the controller), and a failed launched run is the window's one scheduled attempt (check 6)."""
    from anneal_memory import Store
    from anneal_memory.continuity import prepare_wrap

    from levain import wrap as _w

    hold_a_wrap_open(ent)
    age_the_open_wrap(ent.db, days=3)
    assert evaluate(ent, 24).verdict == rd.READY
    with Store(str(ent.db), section_schema=None) as s:       # the seat's cancel, then its fresh wrap
        old_token, _ = _w._observe_prior_wrap(s)
        assert _w._discard_prior_wrap(s, old_token)
        assert prepare_wrap(s, wrap_token="b" * 32)["status"] == "ready"
    r = dream(ent.db, "o-race", _VALID)
    # not saved, not empty: run_body ends the occurrence failed for any other outcome (ok only for saved or empty)
    assert (r.outcome, r.exit_code, r.detail) == ("precondition", 2, "prior wrap in progress"), r
    with Store(str(ent.db), section_schema=None) as s:
        token, started = _w._observe_prior_wrap(s)
    assert token == "b" * 32 and started                      # the seat's new wrap was not touched
    ent.occurrence("failed", window=rd.window_key_for(DAY), launched="2026-11-02T22:00:00Z", ended="2026-11-02T22:01:00Z")
    v = evaluate(ent, 24)
    c6 = next(c for c in v.checks if c.n == 6)
    assert c6.status == rd.QUIET and c6.kind == "attempt-spent", c6


def test_the_fire_cannot_reach_the_executor_later_than_the_lease_which_is_under_the_executors_horizon():
    """F3: the executor discards at its own 55.5 minutes whatever floor admission applied. That matters only if a wrap
    opened AFTER admission could be 55.5 minutes old when the run reads it, i.e. an admission-to-lock gap of that long.
    The chain: ``launch`` runs the input check and then, in one transaction, writes ``launched`` with a lease of
    ``LEASE_S``; ``fire`` claims the row with ``lease_until > now`` (``claim_fire``), so a fire later than the lease
    claims nothing and runs nothing; a lapsed row is ended by ``takeover``, never re-fired, and a new attempt is a new
    admission. So the gap is bounded by the lease. Pin the bound."""
    from levain.agents import controller

    assert controller.LEASE_S < HORIZON_S - 600, (controller.LEASE_S, HORIZON_S)


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


def test_a_floor_too_big_for_a_float_is_refused_and_never_raises_an_overflow(ent, tmp_path):
    """``math.isfinite(10**400)`` raises OverflowError; ``orphan_floor_state`` says it never raises."""
    facts(ent, tmp_path)
    path = ent.agent_dir / da.INSTALL_FILE
    doc = json.loads(path.read_text(encoding="utf-8"))
    path.write_text(json.dumps({**doc, "orphan_floor_hours": 10 ** 400}), encoding="utf-8")
    hours, problem = da.orphan_floor_state(ent.agent_dir)
    assert hours is None and problem and "orphan_floor_hours" in problem
    assert da.orphan_floor_s(ent.agent_dir) is None
    with pytest.raises(da.InstallRefused):
        da.read_install(ent.agent_dir)
    with pytest.raises(da.InstallRefused):
        da.validate_orphan_floor_hours(10 ** 400)


def test_a_stored_floor_that_stops_validating_is_refused_by_name_and_the_named_command_repairs_it(ent, tmp_path, capsys):
    """F2. The scheduled input check reads the install first, so a stored floor that no longer validates (hand-edited, or
    made invalid by a later release that raises the executor's bound) is dead input to the controller every night until
    it is rewritten. KNOWN EDGE: the controller records only the exception's class for that miss, not its message. What
    this pins is that the refusal is never silent where a person looks: ``read_install`` and a plain ``levain dream
    enable`` name the file and the fact and the command that clears it, and that command works."""
    base = enable_args(ent, tmp_path)
    code, out, err, ag = run_enable(tmp_path, capsys, *base, "--orphan-floor-hours", "24")
    assert code == 0, err
    path = ag / da.INSTALL_FILE
    doc = json.loads(path.read_text(encoding="utf-8"))
    path.write_text(json.dumps({**doc, "orphan_floor_hours": 0.1}), encoding="utf-8")
    with pytest.raises(da.InstallRefused) as exc:
        da.read_install(ag)
    assert "orphan_floor_hours" in str(exc.value) and "--orphan-floor-hours off" in str(exc.value), str(exc.value)
    with pytest.raises(da.InstallRefused):                   # the scheduled hook: a raise is dead input, never a quiet day
        da.install_on_input(ag)({"agent_id": "dream", "occurrence_id": "occ-1", "kind": "scheduled",
                                 "window_key": rd.window_key_for(DAY)})
    assert da.orphan_floor_s(ag) is None                      # the manual verb reads it as unset, not as a crash
    code, out, err, _ = run_enable(tmp_path, capsys, *base)
    assert code != 0 and "orphan_floor_hours" in err, (code, out, err)
    code, out, err, _ = run_enable(tmp_path, capsys, *base, "--orphan-floor-hours", "off")
    assert code == 0, err
    assert da.read_install(ag).orphan_floor_hours is None


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
    assert "ready for dream-day" in out and "proven dead" in out, out
    assert "the operator's floor of 24 h: proven dead" in out, out         # the floor as given, not any 24 in the output

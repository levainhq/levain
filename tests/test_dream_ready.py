"""levain.dream.ready (D1-1c): the six day-ready checks, each from a condition made on a scratch entity.

Every test builds its condition with the real pieces on a scratch dir under ``tmp_path``: a real anneal store, a real
agent store (``agent_occurrences`` with its triggers, moved through its own state machine), a real desk store
(a real pending or active launch) and a real ``awake.json`` shape. Nothing here reads ``~/.anneal-memory`` or starts a
process. Check 5's "hooked session" is a STUB: a marker file in anneal's session registry. Whether a real Claude Code
session leaves such a marker in a Levain install is not shown here (anneal's MCP server does not register one).
"""
from __future__ import annotations

import io
import json
import sqlite3
import time
from contextlib import redirect_stdout
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from anneal_memory import FLOW_SCHEMA, Store, sessions
from anneal_memory.continuity import prepare_wrap

from levain.autonomic.agent_tables import create_agent_objects
from levain.autonomic.db import DB_NAME
from levain.dream import cli as dcli
from levain.dream import ready as rd
from levain.dream.cli import add_parsers
from tests.test_dream_snapshot import Fake, add_episode, dream, text_n

ZONE = "America/New_York"
NY = ZoneInfo(ZONE)
DAY = date(2026, 11, 2)
NOW = datetime(2026, 11, 3, 1, 0, tzinfo=NY)            # 01:00 local, inside the 11-02 window


def utc(y, mo, d, h, mi=0) -> str:
    return datetime(y, mo, d, h, mi, tzinfo=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ------------------------------------------------------------------------------------------------------- the entity

class Entity:
    """A scratch entity: store, agent dir and desk dir, plus helpers that make each condition."""

    def __init__(self, root: Path, episodes: int = 3) -> None:
        self.root = root
        root.mkdir(exist_ok=True)
        self.store_dir = root / "store"
        self.agent_dir = root / "agent"
        self.coord_dir = root / "coord"
        for d in (self.store_dir, self.agent_dir, self.coord_dir):
            d.mkdir()
        self.coord_dir.chmod(0o700)
        self.db = self.store_dir / "memory.db"
        with Store(str(self.db), section_schema=FLOW_SCHEMA) as s:
            for i in range(episodes):
                s.record(content=f"Turn {i}: helped the operator.", episode_type="observation", source="t")
        con = sqlite3.connect(self.agent_dir / DB_NAME)
        create_agent_objects(con)
        con.commit()
        con.close()
        self._n = 0

    # occurrences, moved through the real state machine
    def occurrence(self, state: str, *, window: str | None = None, kind: str = "scheduled", launched: str | None = None,
                   ended: str | None = None, oid: str | None = None, agent: str = "dream") -> str:
        self._n += 1
        oid = oid or f"occ-{self._n}"
        window = window or rd.window_key_for(DAY)
        con = sqlite3.connect(self.agent_dir / DB_NAME, isolation_level=None)
        try:
            con.execute("INSERT INTO agent_occurrences (occurrence_id, agent_id, kind, key, state, window_key, "
                        "spec_hash, binding_id, run_id, admitted_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                        (oid, agent, kind, f"k-{oid}", "admitted", window, "sh", "b", f"run-{oid}",
                         launched or "2026-11-02T22:00:00Z"))
            never_launched = state.startswith("skipped") or state == "missed"
            if state != "admitted" and not never_launched:
                con.execute("UPDATE agent_occurrences SET state='launched', launch_epoch=0, link_id='l', "
                            "link_incarnation='i', launched_at=? WHERE occurrence_id=?",
                            (launched or "2026-11-02T22:00:00Z", oid))
            if state not in ("admitted", "launched"):
                con.execute("UPDATE agent_occurrences SET state=?, reason='r', ended_at=? WHERE occurrence_id=?",
                            (state, ended or launched or "2026-11-02T22:10:00Z", oid))
        finally:
            con.close()
        return oid

    def awake(self, threads: list[str], *, until: float | None = None) -> None:
        (self.coord_dir / "awake.json").write_text(json.dumps(
            {"pid": 1, "until": until if until is not None else NOW.timestamp() + 900, "threads": threads,
             "occurrences": []}), encoding="utf-8")

    def pending_launch(self) -> str:
        from levain.desk.store import CoordStore

        cs = CoordStore(self.coord_dir)
        cs.coord_set_agent("claude", cwd=str(self.root))
        cs.accept_drop("fix the build", surface="cli")
        return cs.pending_desk().launch_id

    def evaluate(self, *, now: datetime = NOW, day: date = DAY, manual: bool = False, with_dirs: bool = True,
                 sources_patch=None) -> rd.Verdict:
        src, close = rd.sources_for(self.db, agent_dir=self.agent_dir if with_dirs else None,
                                    coord_dir=self.coord_dir if with_dirs else None, now=now)
        try:
            if sources_patch:
                sources_patch(src)
            return rd.evaluate(src, day=day, now=now, zone=ZONE, manual=manual)
        finally:
            close()


@pytest.fixture
def ent(tmp_path):
    return Entity(tmp_path)


def status(v: rd.Verdict, n: int) -> str:
    return next(c.status for c in v.checks if c.n == n)


# ---------------------------------------------------------------------------------------------------- the baseline

def test_a_clean_entity_is_ready_and_all_six_checks_are_recorded(ent):
    v = ent.evaluate()
    assert v.verdict == rd.READY and v.check is None, v.reason
    assert [c.n for c in v.checks] == [1, 2, 3, 4, 5, 6] and {c.status for c in v.checks} == {"pass"}
    assert v.window_key == "w:2026-11-02T22:00" and v.zone == ZONE


def test_the_session_id_is_the_one_the_install_claims(ent):
    from levain.manifest import ANNEAL_SESSION_ID

    assert rd.SESSION_ID == ANNEAL_SESSION_ID


def test_the_day_boundaries_are_05_00_local_and_dst_aware():
    assert rd.dream_day_at(datetime(2026, 11, 3, 4, 59, tzinfo=NY), ZONE) == date(2026, 11, 2)
    assert rd.dream_day_at(datetime(2026, 11, 3, 5, 0, tzinfo=NY), ZONE) == date(2026, 11, 3)
    # 11-01 is the fall-back night: 05:00 on 11-02 is EST (UTC-5), 05:00 on 10-31 is EDT (UTC-4)
    assert rd.day_start(date(2026, 11, 2), ZONE) == datetime(2026, 11, 2, 10, 0, tzinfo=timezone.utc)
    assert rd.day_start(date(2026, 10, 31), ZONE) == datetime(2026, 10, 31, 9, 0, tzinfo=timezone.utc)
    assert rd.window_holding(datetime(2026, 11, 3, 1, 0, tzinfo=NY), ZONE) == "w:2026-11-02T22:00"
    assert rd.window_holding(datetime(2026, 11, 3, 8, 0, tzinfo=NY), ZONE) is None


# --------------------------------------------------------------------------------------- check 1: already dreamed

def test_check1_a_done_occurrence_for_the_window_is_quiet(ent):
    ent.occurrence("done", launched=utc(2026, 11, 3, 3, 0), ended=utc(2026, 11, 3, 3, 20))
    v = ent.evaluate()
    assert (v.verdict, v.check, v.kind) == (rd.QUIET, 1, "already-dreamed"), v.reason


def test_check1_a_run_in_flight_in_the_window_is_quiet(ent):
    ent.occurrence("launched", launched=utc(2026, 11, 3, 3, 0))
    v = ent.evaluate()
    assert (v.verdict, v.check, v.kind) == (rd.QUIET, 1, "in-flight")


def test_check1_the_run_ceiling_is_a_backstop_at_two_launched(ent):
    ent.occurrence("stopped", launched=utc(2026, 11, 3, 3, 0))
    ent.occurrence("failed", launched=utc(2026, 11, 3, 3, 20))
    v = ent.evaluate()
    assert (v.check, v.kind) == (1, "ceiling") and status(v, 6) == "quiet"     # check 6 says it too; 1 names it


def test_check1_another_agents_occurrence_and_other_windows_do_not_count(ent):
    ent.occurrence("done", agent="someone-else", launched=utc(2026, 11, 3, 3, 0))
    ent.occurrence("done", window="w:2026-11-01T22:00", launched=utc(2026, 11, 2, 3, 0), ended=utc(2026, 11, 2, 3, 20))
    assert ent.evaluate().verdict == rd.READY


def test_check1_a_manual_catch_up_before_14_00_consolidated_the_previous_dream_day(ent):
    # a run-now at 08:00 on 11-03 consolidates dream-day 11-02: checks 1 refuses a second consolidation of 11-02
    ent.occurrence("done", kind="manual", window="m:2026-11-03", launched=utc(2026, 11, 3, 13, 0),
                   ended=utc(2026, 11, 3, 13, 20))
    assert ent.evaluate().check == 1
    assert ent.evaluate(day=date(2026, 11, 3), now=datetime(2026, 11, 3, 23, 0, tzinfo=NY)).verdict == rd.READY


def test_check1_a_manual_launch_counts_the_days_manual_runs_plus_the_window_holding_now(ent):
    now = datetime(2026, 11, 3, 8, 0, tzinfo=NY)                   # no window holds 08:00
    ent.occurrence("failed", kind="manual", window="m:2026-11-03", launched=utc(2026, 11, 3, 12, 0))
    ent.occurrence("failed", kind="manual", window="m:2026-11-03", launched=utc(2026, 11, 3, 12, 30))
    ent.occurrence("failed", window="w:2026-11-02T22:00", launched=utc(2026, 11, 3, 3, 0))   # not held by 08:00
    v = ent.evaluate(now=now, manual=True)
    assert (v.check, v.kind) == (1, "ceiling")
    assert status(v, 5) == "pass"                                   # no longer waived: it never gates (D1-L1)


# ---------------------------------------------------------------------------------------- check 2: baton, open wrap

def test_check2_an_open_wrap_is_quiet(ent):
    with Store(str(ent.db), section_schema=None) as s:
        assert prepare_wrap(s, wrap_token="a" * 32)["status"] == "ready"
    v = ent.evaluate()
    assert (v.verdict, v.check, v.kind) == (rd.QUIET, 2, "wrap-open")


def _require_baton(ent, holder: str | None) -> None:
    with Store(str(ent.db), section_schema=None) as s:
        s.set_consolidate_requires_baton(True)
        if holder:
            sessions.claim_baton(s.continuity_path, holder)


def test_check2_a_required_baton_unheld_or_held_by_another_session_is_quiet(ent):
    _require_baton(ent, None)
    v = ent.evaluate()
    assert (v.check, v.kind) == (2, "baton") and "unheld" in v.reason
    with Store(str(ent.db), section_schema=None) as s:
        sessions.claim_baton(s.continuity_path, "flow-seat")
    v = ent.evaluate()
    assert (v.check, v.kind) == (2, "baton") and "flow-seat" in v.reason


def test_check2_the_baton_held_by_this_install_passes_and_a_store_without_the_policy_needs_none(ent):
    _require_baton(ent, "levain")
    assert ent.evaluate().verdict == rd.READY
    plain = Entity(ent.root / "plain").evaluate()
    assert plain.verdict == rd.READY and "does not require" in next(c.detail for c in plain.checks if c.n == 2)


def test_check2_a_corrupt_baton_file_is_unreadable_not_a_pass(ent):
    _require_baton(ent, "levain")
    with Store(str(ent.db), section_schema=None) as s:
        baton = Path(str(sessions._baton_path(s.continuity_path)))
    baton.write_text("not json", encoding="utf-8")
    v = ent.evaluate()
    assert (v.verdict, v.check) == (rd.UNREADABLE, 2) and status(v, 2) == "unreadable"


# ------------------------------------------------------------------------------- check 3: a wrap since day start

def _wrap_now(ent) -> datetime:
    """One real consolidate (the wrap's wrapped_at is the real clock); then a new episode so check 4 passes."""
    assert dream(ent.db, "w1", text_n(1)).outcome == "saved"
    add_episode(ent.db, "after the wrap")
    return datetime.now(timezone.utc)


def test_check3_a_wrap_since_the_dream_days_start_is_quiet(ent):
    now = _wrap_now(ent)
    day = rd.dream_day_at(now, ZONE)
    v = ent.evaluate(now=now + timedelta(minutes=5), day=day)
    assert (v.verdict, v.check, v.kind) == (rd.QUIET, 3, "consolidated-today"), v.reason


def test_check3_a_wrap_before_the_days_start_does_not_count(ent):
    now = _wrap_now(ent)
    day = rd.dream_day_at(now, ZONE) + timedelta(days=2)               # a later dream-day starts after the wrap
    assert ent.evaluate(now=now + timedelta(days=2), day=day).verdict == rd.READY


def test_check3_the_agents_own_done_occurrence_for_another_day_is_excluded(ent):
    now = _wrap_now(ent)
    day = rd.dream_day_at(now, ZONE)
    wrapped = now - timedelta(seconds=30)
    stamp = lambda t: t.strftime("%Y-%m-%dT%H:%M:%SZ")  # noqa: E731
    # the same wrap, attributed to this agent's done run of the PREVIOUS dream-day's window
    ent.occurrence("done", window=rd.window_key_for(day - timedelta(days=1)), launched=stamp(wrapped - timedelta(minutes=2)),
                   ended=stamp(now + timedelta(minutes=1)))
    v = ent.evaluate(now=now + timedelta(minutes=5), day=day)
    assert v.verdict == rd.READY, v.reason


def test_check3_a_done_occurrence_with_no_end_time_cannot_be_attributed_so_it_is_unreadable(ent):
    now = _wrap_now(ent)
    day = rd.dream_day_at(now, ZONE)
    row = {"occurrence_id": "o", "agent_id": "dream", "kind": "scheduled", "state": "done",
           "window_key": rd.window_key_for(day - timedelta(days=1)), "launched_at": "2026-11-02T22:00:00Z",
           "ended_at": None}                         # the schema forbids this row; the predicate must not trust that
    v = ent.evaluate(now=now + timedelta(minutes=5), day=day, sources_patch=lambda src: setattr(src, "occurrences",
                                                                                              lambda: [row]))
    assert status(v, 3) == "unreadable" and v.verdict == rd.UNREADABLE


# ----------------------------------------------------------------------------- check 4: something to consolidate

def test_check4_no_episodes_is_quiet_and_names_nothing_to_consolidate(tmp_path):
    e = Entity(tmp_path, episodes=0)
    v = e.evaluate()
    assert (v.verdict, v.check, v.kind) == (rd.QUIET, 4, "nothing-to-consolidate")


def test_check4_a_quiet_for_a_known_fact_outranks_an_unreadable_source(ent):
    def break_wraps(src):
        src.wraps = lambda: (_ for _ in ()).throw(OSError("the wrap history is unreadable"))

    v = Entity(ent.root / "b", episodes=0).evaluate(sources_patch=break_wraps)
    assert (v.verdict, v.check) == (rd.QUIET, 4) and status(v, 3) == "unreadable"


# ------------------------------------------------------------- check 5: live seats are noted, never a gate (D1-L1)
# Written from the run-first exercise on a scratch store (task 1111-25): before this change each live seat below made
# the predicate answer quiet at check 5 (kind live-seat); the dream now runs with seats live, so each one is READY and
# named in check 5's detail, while every other check still refuses with the seat live.

def c5(v: rd.Verdict) -> rd.Check:
    return next(c for c in v.checks if c.n == 5)


def test_check5_a_pending_desk_launch_is_noted_and_the_day_is_ready(ent):
    lid = ent.pending_launch()
    v = ent.evaluate()
    assert v.verdict == rd.READY and v.check is None, v.reason
    assert c5(v).status == "pass" and lid in c5(v).detail and "seats live" in c5(v).detail


def test_check5_an_active_desk_launch_is_noted_and_the_day_is_ready(ent):
    from levain.desk.store import CoordStore

    lid = ent.pending_launch()
    cs = CoordStore(ent.coord_dir)
    assert cs.launch_claim_spawn(lid, 4242)[1] is None
    cs.launch_activate(lid, harness_session_id="sess-1")
    assert lid in [ln.launch_id for ln in cs.launches_active()]
    v = ent.evaluate()
    assert v.verdict == rd.READY and lid in c5(v).detail


def test_check5_a_work_thread_in_awake_json_is_noted_and_an_expired_hold_names_nothing(ent):
    ent.awake(["thr-1"])
    v = ent.evaluate()
    assert v.verdict == rd.READY and "thr-1" in c5(v).detail
    ent.awake(["thr-1"], until=NOW.timestamp() - 60)
    v = ent.evaluate()
    assert v.verdict == rd.READY and "thr-1" not in c5(v).detail and "no live desk launch" in c5(v).detail


def test_check5_a_hooked_session_stub_is_noted_and_the_day_is_ready(ent):
    # STUB: a registry marker such as a session hook writes; no process exists
    with Store(str(ent.db), section_schema=None) as s:
        sessions.register_session(s.continuity_path, "claude-code-7f3a", label="hooked session (stub)")
    v = ent.evaluate()
    assert v.verdict == rd.READY and "claude-code-7f3a" in c5(v).detail


def test_check5_the_installs_own_session_marker_is_not_another_seat(ent):
    with Store(str(ent.db), section_schema=None) as s:
        sessions.register_session(s.continuity_path, "levain")
    assert ent.evaluate().verdict == rd.READY


def test_check5_a_stale_session_marker_is_not_live(ent):
    import os

    with Store(str(ent.db), section_schema=None) as s:
        sessions.register_session(s.continuity_path, "claude-code-old")
        f = next(sessions._registry_dir(s.continuity_path).iterdir())
    old = time.time() - sessions.DEFAULT_TTL_SECONDS - 600
    os.utime(f, (old, old))
    assert ent.evaluate().verdict == rd.READY


def test_check5_an_unreadable_seat_source_is_named_and_gates_nothing(ent):
    """The list is informational now, so a source that cannot be read is named, not turned into a refusal."""
    (ent.coord_dir / "awake.json").write_text("{not json", encoding="utf-8")
    v = ent.evaluate()
    assert v.verdict == rd.READY and c5(v).status == "pass", v.reason
    assert "awake.json work thread" in c5(v).detail and "not every source could be read" in c5(v).detail


def test_check5_a_live_launch_and_an_unreadable_source_are_both_named(ent):
    lid = ent.pending_launch()
    (ent.coord_dir / "awake.json").write_text("{not json", encoding="utf-8")
    v = ent.evaluate()
    assert v.verdict == rd.READY and lid in c5(v).detail and "not every source could be read" in c5(v).detail


def test_check5_is_the_same_for_a_manual_run_it_no_longer_waives(ent):
    lid = ent.pending_launch()
    v = ent.evaluate(manual=True)
    assert v.verdict == rd.READY and c5(v).status == "pass" and lid in c5(v).detail


def test_check5_names_a_store_without_the_baton_policy_when_seats_are_live(ent):
    """A store without the baton policy is named when seats are live (stated, not refused: D1_design 3.4), and the
    detail says what keeps a seat's consolidate out (the open wrap) and what the policy adds (other ids only)."""
    ent.pending_launch()
    v = ent.evaluate()
    d = c5(v).detail
    assert v.verdict == rd.READY and "does not require the consolidate baton" in d
    assert "by that open wrap" in d and "another session id" in d
    _require_baton(ent, rd.SESSION_ID)
    v = ent.evaluate()
    assert v.verdict == rd.READY and "does not require" not in c5(v).detail


def test_check5_an_unreadable_baton_policy_is_named_when_seats_are_live_and_never_refused_on(ent):
    """The policy note is produced only with seats live; when that read fails the detail says so, like any other
    unreadable source, and check 5 still passes."""
    ent.pending_launch()

    def broken(src):
        def boom():
            raise RuntimeError("policy read broke")
        src.requires_baton = boom

    v = ent.evaluate(sources_patch=broken)
    assert c5(v).status == "pass", c5(v).detail
    assert "the baton policy could not be read" in c5(v).detail and "policy read broke" in c5(v).detail
    assert "not every source could be read" in c5(v).detail


def test_the_open_wrap_not_the_baton_keeps_a_seat_under_this_installs_id_out(ent):
    """The module docstring's safety argument, run: on a store with the policy and the baton held by this install, the
    dream's open wrap refuses a second prepare_wrap under the SAME id (a Levain seat), while another id is downgraded to
    capture-only by the policy."""
    from anneal_memory import WrapInProgressError

    _require_baton(ent, rd.SESSION_ID)
    with Store(str(ent.db), section_schema=None) as dream, Store(str(ent.db), section_schema=None) as seat:
        assert prepare_wrap(dream, wrap_token="a" * 32, session_id=rd.SESSION_ID)["status"] == "ready"
        with pytest.raises(WrapInProgressError):
            prepare_wrap(seat, wrap_token="b" * 32, session_id=rd.SESSION_ID)
        assert prepare_wrap(seat, wrap_token="c" * 32, session_id="some-seat")["status"].startswith("downgraded")


@pytest.mark.parametrize("seat", ["launch", "thread", "hooked"])
def test_a_live_seat_does_not_hide_any_other_refusal(tmp_path, seat):
    """Each other check still refuses beside a live seat: the seat changes nothing about them."""
    def live(e: Entity) -> None:
        if seat == "launch":
            e.pending_launch()
        elif seat == "thread":
            e.awake(["thr-1"])
        else:
            with Store(str(e.db), section_schema=None) as s:
                sessions.register_session(s.continuity_path, "claude-code-7f3a")

    e = Entity(tmp_path / "wrap")                                  # a wrap open (check 2)
    live(e)
    with Store(str(e.db), section_schema=None) as s:
        assert prepare_wrap(s, wrap_token="a" * 32)["status"] == "ready"
    v = e.evaluate()
    assert (v.verdict, v.check, v.kind) == (rd.QUIET, 2, "wrap-open")

    e = Entity(tmp_path / "baton")                                 # the baton is another session's (check 2)
    live(e)
    _require_baton(e, "flow-seat")
    v = e.evaluate()
    assert (v.verdict, v.check, v.kind) == (rd.QUIET, 2, "baton")

    e = Entity(tmp_path / "done")                                  # already dreamed (check 1)
    live(e)
    e.occurrence("done", launched=utc(2026, 11, 3, 3, 0), ended=utc(2026, 11, 3, 3, 20))
    assert (e.evaluate().check, e.evaluate().kind) == (1, "already-dreamed")

    e = Entity(tmp_path / "wrapped")                               # a wrap since the dream-day's start (check 3)
    live(e)
    now = _wrap_now(e)
    v = e.evaluate(now=now + timedelta(minutes=5), day=rd.dream_day_at(now, ZONE))
    assert (v.verdict, v.check, v.kind) == (rd.QUIET, 3, "consolidated-today")

    e = Entity(tmp_path / "empty", episodes=0)                     # nothing to consolidate (check 4)
    live(e)
    v = e.evaluate()
    assert (v.verdict, v.check, v.kind) == (rd.QUIET, 4, "nothing-to-consolidate")

    e = Entity(tmp_path / "spent")                                 # the window's attempt is spent (check 6)
    live(e)
    e.occurrence("failed", launched=utc(2026, 11, 3, 3, 0))
    v = e.evaluate()
    assert (v.verdict, v.check, v.kind) == (rd.QUIET, 6, "attempt-spent")


def test_a_capture_during_a_wrap_is_the_next_wraps_input_and_a_live_seat_does_not_stop_the_dream(ent):
    """The PLAN row's test, through the real dream path (``consolidate_store``): a stand-in seat records two episodes
    through its own connection while the dream's wrap is open (seats are live: a desk launch and a work thread). The
    admission is READY with the seats live; the frozen set holds only what was recorded before ``prepare_wrap``; the
    two captures are not lost and are exactly what ``episodes_since_wrap()`` lists afterwards, and the NEXT dream's
    admission (check 4) and its prepared input both carry them. Reproduced first by hand on a scratch store."""
    from levain.dream import executor as ex

    ent.pending_launch()
    ent.awake(["thr-1"])
    assert ent.evaluate().verdict == rd.READY
    with Store(str(ent.db), section_schema=None) as s:
        before = {e.id for e in s.episodes_since_wrap()}
    made: list[str] = []
    frozen: list[list[str]] = []

    class SeatCapturing(Fake):
        def compose(self, req):                                      # the seat keeps capturing while the mind composes
            with Store(str(ent.db), section_schema=None) as s:
                frozen.append(list(s.load_wrap_snapshot()["episode_ids"]))
                made.extend(s.record(content=f"seat capture {i} during the wrap", episode_type="observation",
                                     source="seat").id for i in range(2))
            return super().compose(req)

    r = ex.consolidate_store(ent.db, mind=SeatCapturing(text_n(1)), occurrence_dir=ent.store_dir / "dream" / "o1",
                             max_seconds=3000)
    assert r.outcome == "saved" and len(made) == 2
    assert set(frozen[0]) == before and not set(made) & set(frozen[0])       # the set was frozen at prepare
    with Store(str(ent.db), section_schema=None) as s:
        assert {e.id for e in s.episodes_since_wrap()} == set(made)          # nothing lost, nothing re-offered
    # the next dream's admission sees them (the next dream-day: no wrap since its start, seats still live) ...
    # (anneal stamps the wrap with the real clock, so the next day is taken from it)
    nxt = rd.dream_day_at(datetime.now(timezone.utc), ZONE) + timedelta(days=1)
    v = ent.evaluate(now=rd.day_start(nxt, ZONE) + timedelta(hours=19), day=nxt)
    assert v.verdict == rd.READY and "2 episode(s)" in next(c for c in v.checks if c.n == 4).detail
    # ... and its prepared input carries both
    with Store(str(ent.db), section_schema=FLOW_SCHEMA) as s:
        pkg = str(prepare_wrap(s, wrap_token="b" * 32)["package"])
        assert set(s.load_wrap_snapshot()["episode_ids"]) == set(made)
        assert all(f"seat capture {i} during the wrap" in pkg for i in range(2))


# --------------------------------------------------------------------------------------- check 6: attempt spent

@pytest.mark.parametrize("day, first_id, second_id", [
    (date(2026, 10, 15), "2026-10-15-22d35afc", "2026-10-15-985705a3"),
    (date(2026, 10, 17), "2026-10-17-d5542434", "2026-10-17-4dd99d35"),
])
def test_check6_the_double_night_attempts_of_flows_ledger_launch_once(ent, day, first_id, second_id):
    """Flow's ledger: two attempts per window (launched 03:37 and 03:47 the next morning), both refused. One scheduled
    launch per window means the second slot's predicate says quiet."""
    window = rd.window_key_for(day)
    nxt = day + timedelta(days=1)
    first = datetime(nxt.year, nxt.month, nxt.day, 3, 37, tzinfo=NY).astimezone(timezone.utc)
    ent.occurrence("failed", window=window, oid=first_id, launched=first.strftime("%Y-%m-%dT%H:%M:%SZ"),
                   ended=(first + timedelta(minutes=2)).strftime("%Y-%m-%dT%H:%M:%SZ"))
    second_slot = datetime(nxt.year, nxt.month, nxt.day, 3, 47, tzinfo=NY)
    v = ent.evaluate(day=day, now=second_slot)
    assert (v.verdict, v.check, v.kind) == (rd.QUIET, 6, "attempt-spent"), v.reason
    assert first_id in v.reason
    # had the backstop been the only guard, a second launch would have been allowed up to the ceiling of two
    ent.occurrence("failed", window=window, oid=second_id,
                   launched=(first + timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M:%SZ"))
    v = ent.evaluate(day=day, now=second_slot + timedelta(minutes=20))
    assert (v.check, v.kind) == (1, "ceiling")


def test_check6_a_stopped_run_also_spends_the_window_and_a_skipped_slot_does_not(ent):
    other = Entity(ent.root / "c")
    other.occurrence("skipped-no-input")
    other.occurrence("missed")
    assert other.evaluate().verdict == rd.READY                   # slots that never launched spend nothing
    ent.occurrence("stopped", launched=utc(2026, 11, 3, 3, 0))
    assert ent.evaluate().check == 6


# --------------------------------------------------------------------------------------------------- fail-closed

def test_an_unreadable_occurrence_table_is_unreadable_not_ready(ent):
    (ent.agent_dir / DB_NAME).write_bytes(b"this is not a sqlite database at all" * 10)
    v = ent.evaluate()
    assert v.verdict == rd.UNREADABLE and status(v, 1) == "unreadable" and status(v, 6) == "unreadable"
    assert not v.ready


def test_every_check_is_recorded_even_when_the_first_is_quiet(ent):
    ent.occurrence("done", launched=utc(2026, 11, 3, 3, 0), ended=utc(2026, 11, 3, 3, 20))
    v = ent.evaluate()
    assert v.check == 1 and len(v.checks) == 6


def test_a_naive_clock_is_refused(ent):
    src, close = rd.sources_for(ent.db)
    try:
        with pytest.raises(ValueError):
            rd.evaluate(src, day=DAY, now=datetime(2026, 11, 3, 1, 0), zone=ZONE)
    finally:
        close()


# ---------------------------------------------------------------------------------------------------------- the verb

def run_verb(*argv: str) -> tuple[int, dict]:
    import argparse

    p = argparse.ArgumentParser()
    add_parsers(p.add_subparsers(dest="cmd"))
    args = p.parse_args(["dream", "ready", *argv])
    buf = io.StringIO()
    with redirect_stdout(buf):
        code = args.func(args)
    out = buf.getvalue()
    return code, (json.loads(out) if "--json" in argv else {"text": out})


def common(e: Entity) -> list[str]:
    return ["--store-dir", str(e.store_dir), "--agent-dir", str(e.agent_dir), "--coord-dir", str(e.coord_dir),
            "--zone", ZONE, "--day", DAY.isoformat(), "--now", NOW.isoformat(), "--json"]


def test_verb_prints_ready_with_exit_0(ent):
    code, doc = run_verb(*common(ent))
    assert code == 0 and doc["verdict"] == "ready" and len(doc["checks"]) == 6


def test_verb_prints_quiet_with_exit_1_and_the_reason(ent):
    ent.pending_launch()                                           # a live seat beside the refusal changes nothing
    with Store(str(ent.db), section_schema=None) as s:
        assert prepare_wrap(s, wrap_token="a" * 32)["status"] == "ready"
    code, doc = run_verb(*common(ent))
    assert code == 1 and doc["verdict"] == "quiet" and doc["check"] == 2 and doc["kind"] == "wrap-open"


def test_verb_a_live_seat_is_ready_with_exit_0_and_named(ent):
    lid = ent.pending_launch()
    code, doc = run_verb(*common(ent))
    assert code == 0 and doc["verdict"] == "ready" and lid in next(c["detail"] for c in doc["checks"] if c["n"] == 5)


def test_verb_prints_unreadable_with_exit_3(ent):
    (ent.agent_dir / DB_NAME).write_bytes(b"this is not a sqlite database at all" * 10)
    code, doc = run_verb(*common(ent))
    assert code == 3 and doc["verdict"] == "unreadable" and doc["check"] == 1


def test_verb_on_a_bare_store_copy_runs_and_says_which_sources_were_absent(ent):
    code, doc = run_verb("--store-dir", str(ent.store_dir), "--zone", ZONE, "--day", DAY.isoformat(), "--now",
                         NOW.isoformat(), "--json")
    assert code == 0
    assert "no agent store given" in next(c["detail"] for c in doc["checks"] if c["n"] == 5)


def test_verb_manual_flag_notes_the_live_seat_as_a_scheduled_run_does(ent):
    ent.pending_launch()
    code, doc = run_verb(*common(ent), "--manual")
    assert code == 0 and next(c["status"] for c in doc["checks"] if c["n"] == 5) == "pass"


def test_verb_refuses_a_naive_now_and_an_unknown_zone(ent):
    import argparse

    for bad in (["--now", "2026-11-03T01:00:00"], ["--zone", "Mars/Olympus"]):
        argv = [a for a in common(ent) if a != "--json"]
        for i, a in enumerate(argv):
            if a == bad[0]:
                argv[i + 1] = bad[1]
        p = argparse.ArgumentParser()
        add_parsers(p.add_subparsers(dest="cmd"))
        args = p.parse_args(["dream", "ready", *argv])
        assert args.func(args) == 2


def test_verb_refuses_the_operators_live_store(tmp_path, monkeypatch):
    import argparse

    live = tmp_path / "home" / ".anneal-memory"
    live.mkdir(parents=True)
    monkeypatch.setattr("levain.firing.isolation.flow_store_dir", lambda: live)
    p = argparse.ArgumentParser()
    add_parsers(p.add_subparsers(dest="cmd"))
    args = p.parse_args(["dream", "ready", "--store-dir", str(live), "--zone", ZONE])
    assert args.func(args) == 2


# ------------------------------------------------------------------------------------------- review fixes (L3 round 1)

def test_a_manual_run_at_01_00_consolidated_the_night_that_is_still_running_not_the_one_before(ent):
    """Reproduced: ``occurrence_dream_day`` took the 05:00-shifted day and then subtracted another day for the hour, so
    a run-now at 01:00 on 11-03 (dream-day 11-02) was attributed to 11-01."""
    row = {"occurrence_id": "m1", "kind": "manual", "launched_at": utc(2026, 11, 3, 6, 0)}      # 01:00 NY
    assert rd.occurrence_dream_day(row, ZONE) == date(2026, 11, 2)
    assert rd.manual_day_at(datetime(2026, 11, 3, 8, 0, tzinfo=NY), ZONE) == date(2026, 11, 2)
    assert rd.manual_day_at(datetime(2026, 11, 3, 13, 59, tzinfo=NY), ZONE) == date(2026, 11, 2)
    assert rd.manual_day_at(datetime(2026, 11, 3, 14, 0, tzinfo=NY), ZONE) == date(2026, 11, 3)
    ent.occurrence("done", kind="manual", window="m:2026-11-03", launched=utc(2026, 11, 3, 6, 0),
                   ended=utc(2026, 11, 3, 6, 20))
    v = ent.evaluate(manual=True)
    assert (v.check, v.kind) == (1, "already-dreamed"), v.reason


def test_a_manual_catch_up_after_the_scheduled_attempt_failed_is_not_blocked_by_check_6(ent):
    """Reproduced: check 6 refused the run-now the did-not-consolidate card names as the remedy."""
    ent.occurrence("failed", launched=utc(2026, 11, 3, 8, 37), ended=utc(2026, 11, 3, 8, 39))   # 03:37 NY
    now = datetime(2026, 11, 3, 8, 0, tzinfo=NY)
    v = ent.evaluate(now=now, manual=True)
    assert v.verdict == rd.READY and status(v, 6) == "waived", v.reason
    assert ent.evaluate(now=NOW + timedelta(hours=3)).check == 6           # the scheduled path still holds the rule


def test_a_manual_run_in_flight_is_in_flight_for_the_next_manual_run(ent):
    """Reproduced: the in-flight test matched the scheduled window key only, so a second run-now read ready."""
    now = datetime(2026, 11, 3, 8, 0, tzinfo=NY)
    ent.occurrence("launched", kind="manual", window="m:2026-11-03", launched=utc(2026, 11, 3, 12, 30))
    v = ent.evaluate(now=now, manual=True)
    assert (v.check, v.kind) == (1, "in-flight"), v.reason


def test_check3_excludes_only_the_one_wrap_an_own_occurrence_made(ent):
    """Reproduced: every wrap inside an own occurrence's interval (plus slack) was excluded, so a foreign wrap landing
    just after the run read as the run's own and the predicate said ready."""
    ent.occurrence("done", window=rd.window_key_for(DAY - timedelta(days=1)), launched=utc(2026, 11, 2, 15, 0),
                   ended=utc(2026, 11, 2, 15, 10))
    own, foreign = utc(2026, 11, 2, 15, 8), utc(2026, 11, 2, 15, 14)
    v = ent.evaluate(sources_patch=lambda s: setattr(s, "wraps", lambda: [(1, own), (2, foreign)]))
    assert (v.verdict, v.check, v.kind) == (rd.QUIET, 3, "consolidated-today"), v.reason
    assert "#2" in v.reason
    v = ent.evaluate(sources_patch=lambda s: setattr(s, "wraps", lambda: [(1, own)]))
    assert v.verdict == rd.READY, v.reason


def test_check5_a_pending_launch_whose_spawn_is_claimed_is_still_named_as_a_live_seat(ent):
    """Reproduced: ``launches_unspawned`` drops a claimed spawn and ``launches_active`` does not hold it yet."""
    from levain.desk.store import CoordStore

    lid = ent.pending_launch()
    assert CoordStore(ent.coord_dir).launch_claim_spawn(lid, 4242)[1] is None
    v = ent.evaluate()
    assert v.verdict == rd.READY and lid in c5(v).detail


def test_the_ready_verb_opens_the_store_without_writing_it(ent):
    """Reproduced: a default ``Store`` open runs schema init, which fails ("attempt to write a readonly database") on a
    store copy that is only readable; a read-only handle needs no write at all."""
    files = [p for p in ent.store_dir.iterdir() if p.is_file()]
    for p in files:
        p.chmod(0o444)
    try:
        code, doc = run_verb(*common(ent))
    finally:
        for p in files:
            p.chmod(0o644)
    assert code == 0 and doc["verdict"] == "ready"


def test_verb_default_day_for_a_manual_run_is_the_one_a_run_now_would_consolidate(ent):
    argv = ["--store-dir", str(ent.store_dir), "--agent-dir", str(ent.agent_dir), "--coord-dir", str(ent.coord_dir),
            "--zone", ZONE, "--now", datetime(2026, 11, 3, 8, 0, tzinfo=NY).isoformat(), "--json"]
    assert run_verb(*argv, "--manual")[1]["day"] == "2026-11-02"
    assert run_verb(*argv)[1]["day"] == "2026-11-03"


def test_verb_a_store_that_will_not_open_is_unreadable_exit_3(ent):
    """The comment in the verb says an unopenable store is unreadable; the exit code now agrees."""
    (ent.store_dir / "memory.db").write_bytes(b"not a database" * 100)
    code, _ = run_verb(*[a for a in common(ent) if a != "--json"])
    assert code == 3


def test_check1_a_scheduled_evaluation_sees_a_manual_run_in_flight_by_kind_and_by_key_prefix(ent):
    """Reproduced by review: the manual in-flight match applied to manual evaluations only, so a scheduled one read ready
    beside a launched manual run; the match is by ``kind`` or the ``m:`` key prefix, whichever the row carries."""
    ent.occurrence("launched", kind="manual", window="m:2026-11-03", launched=utc(2026, 11, 3, 12, 0))
    v = ent.evaluate(manual=False)
    assert (v.verdict, v.check, v.kind) == (rd.QUIET, 1, "in-flight")


def test_check1_a_manual_evaluation_sees_the_previous_windows_scheduled_run_in_flight(ent):
    ent.occurrence("launched", window="w:2026-11-02T22:00", launched=utc(2026, 11, 3, 2, 0))
    v = ent.evaluate(manual=True)
    assert (v.verdict, v.check, v.kind) == (rd.QUIET, 1, "in-flight")


def test_a_manual_row_with_no_launched_at_is_ready_through_the_agent_check(ent):
    """G2 strengthened (D1 L3 r3): not merely 'launched_at is absent from the reason', the verdict is READY."""
    from levain.dream import agent as da

    row = {"occurrence_id": "m-new", "agent_id": "dream", "kind": "manual", "state": "admitted", "launched_at": None}
    v = da.day_ready(ent.db, row, now=NOW, zone=ZONE, agent_dir=ent.agent_dir, coord_dir=ent.coord_dir)
    assert v.state == rd.READY and "launched_at" not in v.reason, v.reason

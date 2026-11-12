"""1112-14 row 1699 (D1-Q follow-up): check 6 and a run taken over before any fire claimed it.

Phill's ruling (np-11fe7532 option (b)): a launched dream row that lapse recovery ended before any fire claimed it
does not spend the night's one scheduled attempt, so the next slot may launch; at most one such extra launch per
night, still inside the signed run ceiling (check 1) and the window budget. A run a fire DID claim and then lost still
spends the attempt. The fact the readiness check reads is on the occurrence row itself: ``reason`` is
``crash_recovered`` (takeover's reason) with neither ``pid`` nor ``session_key`` ever written.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

from levain.autonomic.db import DB_NAME
from levain.dream import ready as rd
from tests.test_dream_ready import DAY, NOW, NY, Entity, ent, status, utc  # noqa: F401 - ent is a fixture

SLOT_1 = utc(2026, 11, 3, 3, 0)
SLOT_2 = utc(2026, 11, 3, 3, 30)


def _mark(e: Entity, oid: str, **cols) -> None:
    sets = ", ".join(f"{k} = ?" for k in cols)
    con = sqlite3.connect(e.agent_dir / DB_NAME, isolation_level=None)
    try:
        con.execute(f"UPDATE agent_occurrences SET {sets} WHERE occurrence_id = ?", (*cols.values(), oid))
    finally:
        con.close()


def _taken_over(e: Entity, *, oid: str | None = None, launched: str = SLOT_1, pid: int | None = None,
                session_key: str | None = None, reason: str = "crash_recovered", state: str = "stopped") -> str:
    """A run that ended ``state`` / ``reason``; a terminal row is immutable, so the claim columns are written while it
    is still launched, as a fire's claim does."""
    oid = e.occurrence("launched", launched=launched, oid=oid)
    _mark(e, oid, pid=pid, session_key=session_key)
    _mark(e, oid, state=state, reason=reason, ended_at=launched)
    return oid


def test_a_run_taken_over_before_any_fire_claimed_it_leaves_the_attempt_unspent_and_the_next_slot_launches(ent):
    first = _taken_over(ent)
    v = ent.evaluate(now=datetime(2026, 11, 3, 3, 40, tzinfo=NY).astimezone(timezone.utc))
    assert v.verdict == rd.READY, v.reason
    c6 = next(c for c in v.checks if c.n == 6)
    assert c6.status == "pass" and first in c6.detail


def test_a_run_a_fire_claimed_and_then_lost_still_spends_the_attempt(ent):
    """The control: the rule did not just switch check 6 off. A claimed pid, taken over later, is the fire's run."""
    ent_oid = _taken_over(ent, pid=4242)
    v = ent.evaluate()
    assert (v.verdict, v.check, v.kind) == (rd.QUIET, 6, "attempt-spent"), v.reason
    assert ent_oid in v.reason


def test_a_session_claimed_run_taken_over_still_spends_the_attempt(ent):
    _taken_over(ent, session_key="s:1")
    assert ent.evaluate().check == 6


def test_an_unclaimed_run_that_ended_for_another_reason_still_spends_the_attempt(ent):
    """No pid is not the whole fact: a stop the reaper or the ceiling wrote is not a takeover of an unclaimed row."""
    _taken_over(ent, reason="stop_at")
    assert ent.evaluate().check == 6


def test_an_unclaimed_run_that_failed_outright_still_spends_the_attempt(ent):
    _taken_over(ent, state="failed", reason="crash_recovered")
    assert ent.evaluate().check == 6


def test_the_extra_launch_is_one_per_night_a_second_taken_over_run_spends_the_attempt(ent):
    """One forgiven take-over buys one extra launch; the run after it that is taken over too is not forgiven again,
    whatever the ceiling's number is."""
    _taken_over(ent, oid="occ-a", launched=SLOT_1)
    _taken_over(ent, oid="occ-b", launched=SLOT_2)
    v = ent.evaluate(sources_patch=lambda src: None)
    assert status(v, 6) == "quiet", [c.as_dict() for c in v.checks]


def test_the_extra_launch_cap_holds_with_a_ceiling_that_would_have_allowed_more(ent, monkeypatch):
    monkeypatch.setattr(rd, "RUN_CEILING", 5)
    _taken_over(ent, oid="occ-a", launched=SLOT_1)
    _taken_over(ent, oid="occ-b", launched=SLOT_2)
    v = ent.evaluate()
    assert (v.verdict, v.check, v.kind) == (rd.QUIET, 6, "attempt-spent"), v.reason


def test_the_signed_run_ceiling_still_stops_the_extra_launch(ent):
    """A forgiven take-over plus the ceiling's own count: check 1 answers first, as it always did."""
    _taken_over(ent, oid="occ-a", launched=SLOT_1)
    _taken_over(ent, oid="occ-b", launched=SLOT_2, pid=77)
    v = ent.evaluate()
    assert (v.verdict, v.check, v.kind) == (rd.QUIET, 1, "ceiling"), v.reason


def test_a_forgiven_take_over_beside_a_claimed_failure_does_not_forgive_the_failure(ent):
    _taken_over(ent, oid="occ-a", launched=SLOT_1)
    _taken_over(ent, oid="occ-b", launched=SLOT_2, pid=77)
    v = ent.evaluate()
    assert status(v, 6) == "quiet"


def test_a_manual_run_is_unchanged(ent):
    _taken_over(ent, pid=4242)
    assert status(ent.evaluate(manual=True), 6) == "waived"


def test_a_real_takeover_of_an_unclaimed_row_writes_the_fact_check_6_reads(ent):
    """Pins the writer to the reader: the controller's own take_over_unclaimed leaves reason crash_recovered with no pid
    and no session key on the row, and the readiness check forgives exactly that row."""
    from levain.agents import controller as ctl

    oid = ent.occurrence("launched", launched=SLOT_1)
    _mark(ent, oid, lease_until=utc(2026, 11, 3, 3, 15), epoch=0)
    con = sqlite3.connect(ent.agent_dir / DB_NAME, isolation_level=None)
    try:
        con.execute("BEGIN IMMEDIATE")
        ended = ctl.take_over_unclaimed(con, datetime(2026, 11, 3, 3, 20, tzinfo=timezone.utc))
        con.execute("COMMIT")
        row = con.execute("SELECT state, reason, pid, session_key FROM agent_occurrences WHERE occurrence_id = ?",
                          (oid,)).fetchone()
    finally:
        con.close()
    assert [o for _, o, _ in ended] == [oid]
    assert row == ("stopped", "crash_recovered", None, None)
    v = ent.evaluate(now=datetime(2026, 11, 3, 3, 40, tzinfo=NY).astimezone(timezone.utc))
    assert status(v, 6) == "pass", [c.as_dict() for c in v.checks]


def test_the_reason_check_6_reads_is_the_one_takeover_writes():
    from levain.agents import controller as ctl

    assert rd._TAKEN_OVER_REASON == ctl.CRASH_RECOVERED

"""P10b (levain#215): P7's cases for a session agent, run through the ``levain`` CLI as a subprocess with a stub launcher.

The cases are the ones P10b names: register, sign, tick, fire, ``done``; a vendor exit that is not zero is not ``done``; END
unproven stays ``launched``; a revoke mid-session ends the row ``stopped``; expiry mid-run; duplicate delivery; unreadable
usage refused before any start (``no_exposure``); a manual run against the ceiling. They run on ``tests/_p10b_rig.py`` (the
P10a install, a session agent registered on it) and ``tests/_p10b_entry.py`` (the stub launcher, in the CLI process and in
the fire child). The stub's vendor is a real process behind the product's own ``GatedChild`` gate: the exit status a run reads
is a real exit status. What this proves is the STUB path on this machine: the real launcher, a real vendor and a real S1 END
are the VM's and Gate 2's own nights.

Every case was RUN by hand on a tmp install first and is pinned to what the run gave. A case that needs a launched row
launches it in-process (see ``tests/_p10b_rig.py``); ``test_p10b_the_tick_claims_a_session_window_and_launches_nothing`` is
the case that says why.
"""
from __future__ import annotations

import shutil
import subprocess
from datetime import timedelta

import pytest

pytest.importorskip("cryptography")
from tests._p10a_rig import real_now, utc  # noqa: E402
from tests._p10b_entry import STUB_LAUNCHER, STUB_USAGE_USD_MICROS  # noqa: E402
from tests._p10b_rig import EXPOSURE_USD_MICROS, NoExposure, Session, until  # noqa: E402

pytestmark = pytest.mark.skipif(shutil.which("ssh-keygen") is None, reason="needs ssh-keygen")


@pytest.fixture
def s(tmp_path):
    """The case's session; its teardown runs on a failure too (``hold`` removed, the fires it started killed)."""
    session = Session(tmp_path)
    try:
        yield session
    finally:
        session.close()


def _after(minutes: int) -> str:
    """A tick time ``minutes`` past the REAL clock, as the subprocess reads it (a launch happens at the real now)."""
    return (real_now() + timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _running(s: Session, oid: str) -> None:
    """Wait until the stub's launch for ``oid`` is complete: the row holds its session key, one spawn is counted, and the
    gate's pid file (the last thing the launcher writes) is there."""
    until("the stub's vendor to start", lambda: s.started() == 1 and s.row(oid)["session_key"] is not None
          and (s.stubdir / "gate-1.pid").exists())


def _ended_clean(s: Session, oid: str) -> None:
    """The run holds nothing: no slot, and its reservation settled."""
    assert s.held(oid) == ([], [("settled",)])


# ----------------------------------------------------------------------------------- the fire needs the entry's gate

def test_p10b_a_fire_without_the_entry_cannot_reach_the_stub(s):
    """The install names the stub; a fire started the product's own way (``spawn_fire``'s default argv, a fresh
    interpreter) knows no such launcher: the run is not built, nothing is started, the row stays launched. Only the
    entry's fire (``_p10b_entry.py _fire``) registers it, which is why every other case starts its fire there."""
    from levain.agents import controller as ctl

    s.register()
    oid = s.launched()
    proc = ctl.spawn_fire(s.store_dir, oid, env=s.env())
    s._fires.append(proc)                                   # the case's teardown kills it if it runs on
    assert proc.wait(120) == 1
    assert (s.row(oid)["state"], s.started()) == ("launched", 0)
    assert f"the install names launcher {STUB_LAUNCHER!r}, which is not registered" in s.fire_log(oid)


# ---------------------------------------------------------------------------- register, sign, tick, fire, done

def test_p10b_the_tick_claims_a_session_window_and_launches_nothing(s):
    """The scheduled half of the path. The tick, run as a CLI process at two fixed instants either side of the agent's
    :05 slot, claims the window once and only once (a second tick at the same instant claims nothing more), and leaves the
    row ``admitted``: no launch, no fire, no vendor. A row nothing launches is the one gap the in-process launch of the other
    cases stands in for."""
    s.register()
    day = real_now() + timedelta(days=1)
    first = s.cli("agents", "tick", now=utc(day.year, day.month, day.day, 12, 4, 0))
    assert (first.rc, s.rows()) == (0, [])                  # the first pass only sets the agent's cursor
    for _ in range(2):                                      # the same instant twice: one claim
        got = s.cli("agents", "tick", now=utc(day.year, day.month, day.day, 12, 6, 0))
        assert got.rc == 0, got.text
    (row,) = s.rows()
    assert (row["kind"], row["state"], row["key"], row["launched_at"]) == (
        "scheduled", "admitted", f"{day:%Y-%m-%d}T12:05:00Z", None)
    assert (s.held(row["occurrence_id"]), s.started()) == (([], []), 0)
    assert [(o["state"], o["launched_at"]) for o in s.occurrences()] == [("admitted", None)]


def test_p10b_register_sign_tick_fire_done(s):
    """invariant 7's whole path for a session agent. Registered and signed by the CLI, claimed by the CLI's tick, launched
    (in-process), fired as a real detached process, ``done`` as ``levain agents show --json`` reads it, with the one episode
    under the agent's id. The vendor was asked once, for this occurrence, on the brokered route with the reserved dollars.
    The stub states the usage it read, zero dollars, so the reservation is settled on that actual and debits none of it."""
    s.register()
    day = real_now() + timedelta(days=1)
    s.cli("agents", "tick", now=utc(day.year, day.month, day.day, 12, 4, 0))
    s.cli("agents", "tick", now=utc(day.year, day.month, day.day, 12, 6, 0))
    (claimed,) = s.rows()
    oid = claimed["occurrence_id"]
    assert s.launch(oid).outcome == "launched"
    assert s.row(oid)["state"] == "launched" and s.started() == 0

    assert s.fired(oid) == 0, s.fire_log(oid)
    shown = s.shown()
    (occ,) = shown["occurrences"]
    assert (occ["occurrence_id"], occ["state"], occ["reason"], occ["kind"]) == (oid, "done", "completed", "scheduled")
    assert occ["launched_at"] is not None and occ["ended_at"] is not None
    (ep,) = shown["episodes"]
    assert ep["metadata"]["agent"] == shown["agent"]["agent_id"] and ep["metadata"]["run"] == oid
    assert "ended done: completed" in ep["content"]
    (asked,) = s.start_records()
    assert (asked["occurrence_id"], asked["session_key"], asked["auth"], asked["usd_micros"]) == (
        oid, s.row(oid)["session_key"], "brokered", EXPOSURE_USD_MICROS)
    _ended_clean(s, oid)
    res = s.reservation(oid)
    assert (res["reserved_usd_micros"], res["settled_reason"], res["debited_usd_micros"]) == (
        EXPOSURE_USD_MICROS, "actual", STUB_USAGE_USD_MICROS)


# ----------------------------------------------------------------------------------- a vendor exit that is not zero

@pytest.mark.parametrize("code", [1, 3])
def test_p10b_a_nonzero_vendor_exit_is_not_done(s, code):
    """The vendor exits ``code``: the fire exits 1 and the row is ``failed`` ``session_exit_<code>``, never ``done``, its
    slot freed and its reservation settled; the episode says ``failed``."""
    s.register()
    s.vendor_exit(code)
    oid = s.launched()
    assert s.fired(oid) == 1
    shown = s.shown()
    (occ,) = shown["occurrences"]
    assert (occ["state"], occ["reason"]) == ("failed", f"session_exit_{code}")
    assert [e["content"] for e in shown["episodes"]] == [
        f"session run {oid} of agent {shown['agent']['agent_id']} ended failed: session_exit_{code}"]
    assert s.started() == 1
    _ended_clean(s, oid)


# -------------------------------------------------------------------------------------------- END unproven

def test_p10b_an_unproven_end_stays_launched(s):
    """The vendor itself exits with ``GATE_UNPROVEN``'s status (the gate passes a child's exit through), and the fire reads
    that status as one that proves nothing about the group: the row is NOT ended. It stays ``launched`` with its
    session key, its slot and its open reservation (for the reaper and lapse recovery), writes no episode, and the next
    claim of the agent is held off by it (``skipped-overlap``), as by any live run."""
    from levain.agents import controller as ctl

    s.register()
    s.vendor_exit(ctl.GATE_UNPROVEN)
    oid = s.launched()
    assert s.fired(oid) == 1
    row = s.row(oid)
    assert (row["state"], row["reason"], row["ended_at"]) == ("launched", None, None)
    assert row["session_key"] is not None
    slots, reservations = s.held(oid)
    assert len(slots) == 1 and reservations == [("open",)]
    shown = s.shown()
    assert [(o["state"], o["ended_at"]) for o in shown["occurrences"]] == [("launched", None)]
    assert shown["episodes"] == []
    assert s.claim()[1] == "skipped-overlap"


# ---------------------------------------------------------------------------------------- revoke mid-session

def test_p10b_a_revoke_mid_session_ends_the_row_stopped(s):
    """``levain agents revoke`` (a real laptop-key signature, a CLI process) while the vendor is running: the revoke lands
    and the row is still ``launched`` (the bar only fences it); the NEXT tick (a CLI process whose reaper asks the stub's END)
    stops the session and ends the row ``stopped`` / ``fenced``, its slot freed and its reservation settled, and the fire,
    whose own end the revoke fenced, exits 1 without writing one. The run was never ``done``."""
    s.register()
    s.hold()
    oid = s.launched()
    fire = s.fire(oid)
    _running(s, oid)
    agent = s.agent()["agent_id"]
    next_seq = s.world.state().seq + 1                       # the log's head once the link is signed, plus the revoke

    revoked = s.cli("agents", "revoke", agent, "--data-root", str(s.world.root), "--signing-key", str(s.key))
    assert revoked.rc == 0 and f"revoke landed at seq {next_seq}; the bar is closed" in revoked.out, revoked.text
    assert s.row(oid)["state"] == "launched" and fire.poll() is None

    ticked = s.cli("agents", "tick")
    assert ticked.rc == 0, ticked.text
    assert fire.wait(60) == 1
    row = s.row(oid)
    assert (row["state"], row["reason"]) == ("stopped", "fenced") and row["ended_at"] is not None
    _ended_clean(s, oid)
    assert [(o["state"], o["reason"]) for o in s.occurrences()] == [("stopped", "fenced")]
    assert s.started() == 1


# --------------------------------------------------------------------------------------------- expiry mid-run

def _expiring_run(s: Session) -> tuple[str, subprocess.Popen]:
    """A run whose link expires 30 minutes from now, its vendor held and running, and the tick inside the link's life
    (10 minutes on) having changed nothing: ``(occurrence id, the running fire)``."""
    from levain.cockpit.consent import utc_stamp

    s.register(expires_at=utc_stamp(real_now() + timedelta(minutes=30)))
    s.hold()
    oid = s.launched()
    fire = s.fire(oid)
    _running(s, oid)
    inside = s.cli("agents", "tick", now=_after(10))
    assert inside.rc == 0, inside.text
    assert (s.row(oid)["state"], fire.poll()) == ("launched", None)
    return oid, fire


def _claim_after_expiry_is_stopped(s: Session) -> None:
    """A claim made once the link has expired is refused at the launch for the link (``expired``), not launched and not
    skipped."""
    later = real_now() + timedelta(minutes=41)
    res = s.launch(s.claimed(now=later), now=later)
    assert (res.outcome, res.reason) == ("stopped", "expired"), res


def test_p10b_expiry_mid_run_the_reaper_alone_ends_the_session(s):
    """A link that expires while the vendor runs, the fire already dead (killed, so nothing but the reaper can write an
    end): the tick past the expiry has its reaper end the session ``stopped`` / ``link_not_live``, its slot freed and its
    reservation settled, never ``done``; a claim made after the expiry is refused for the link."""
    oid, fire = _expiring_run(s)
    fire.kill()
    fire.wait(30)
    assert s.row(oid)["state"] == "launched"                 # the dead fire wrote nothing

    past = s.cli("agents", "tick", now=_after(40))
    assert past.rc == 0, past.text
    row = s.row(oid)
    assert (row["state"], row["reason"]) == ("stopped", "link_not_live") and row["ended_at"] is not None
    _ended_clean(s, oid)
    assert [(o["state"], o["reason"]) for o in s.occurrences()] == [("stopped", "link_not_live")]
    _claim_after_expiry_is_stopped(s)
    assert s.started() == 1


def test_p10b_expiry_mid_run_with_the_fire_alive_ends_the_session(s):
    """The same expiry with the fire alive: the tick past it has its reaper stop the session (the vendor is held, so only
    that stop can end it), and the end is written by whichever of the fire (its gate exited 143) or the reaper settles
    first. Each is pinned to what it leaves: the fire's end is ``failed`` / ``session_exit_143`` with its episode, the
    reaper's is ``stopped`` / ``link_not_live`` with none."""
    oid, fire = _expiring_run(s)

    past = s.cli("agents", "tick", now=_after(40))
    assert past.rc == 0, past.text
    assert fire.wait(60) == 1
    row = s.row(oid)
    assert row["ended_at"] is not None
    shown = s.shown()
    if row["state"] == "failed":
        assert row["reason"] == "session_exit_143"
        assert [e["content"] for e in shown["episodes"]] == [
            f"session run {oid} of agent {shown['agent']['agent_id']} ended failed: session_exit_143"]
    else:
        assert (row["state"], row["reason"]) == ("stopped", "link_not_live")
        assert shown["episodes"] == []
    _ended_clean(s, oid)
    _claim_after_expiry_is_stopped(s)
    assert s.started() == 1


# ------------------------------------------------------------------------------------------- duplicate delivery

def test_p10b_duplicate_delivery_runs_the_session_once(s):
    """Two fires of one occurrence while the first runs, then a third after the end: only the first runs, the others exit
    0 having done nothing, the vendor is started once and the row ends ``done`` once with one episode."""
    s.register()
    s.hold()
    oid = s.launched()
    first = s.fire(oid)
    _running(s, oid)
    second = s.fire(oid)
    assert second.wait(60) == 0 and first.poll() is None       # the duplicate exits having started nothing, the first still runs
    s.release()
    assert first.wait(60) == 0
    third = s.fire(oid)
    assert third.wait(60) == 0
    assert s.started() == 1
    shown = s.shown()
    assert [(o["state"], o["reason"]) for o in shown["occurrences"]] == [("done", "completed")]
    assert len(shown["episodes"]) == 1
    _ended_clean(s, oid)


# ------------------------------------------------------------------------------------------ unreadable usage

def test_p10b_an_exposure_that_cannot_be_read_is_refused_before_any_start(s):
    """An executor that cannot state its exposure is refused at the launch, ``skipped-budget`` / ``no_exposure``, before
    anything is taken: no slot, no reservation, no session key. A fire handed the row holds nothing and starts nothing."""
    s.register()
    oid = s.claimed()
    res = s.launch(oid, executor=NoExposure())
    assert (res.outcome, res.reason) == ("skipped-budget", "no_exposure")
    row = s.row(oid)
    assert (row["state"], row["reason"], row["launched_at"], row["session_key"]) == (
        "skipped-budget", "no_exposure", None, None)
    assert s.held(oid) == ([], [])
    assert s.fired(oid) == 0 and s.started() == 0
    assert [(o["state"], o["reason"], o["launched_at"]) for o in s.occurrences()] == [
        ("skipped-budget", "no_exposure", None)]


def test_p10b_usage_unreadable_at_the_end_debits_the_whole_reservation(s):
    """The launcher can say nothing of the session's usage once it is over: the run still ends ``done``, and its
    reservation is settled for the WHOLE reserved figure (``unreadable``), never the zero a readable run debits
    (``test_p10b_register_sign_tick_fire_done`` holds that side)."""
    s.register()
    s.usage_unreadable()
    oid = s.launched()
    assert s.fired(oid) == 0
    res = s.reservation(oid)
    assert (res["state"], res["settled_reason"], res["reserved_usd_micros"], res["debited_usd_micros"]) == (
        "settled", "unreadable", EXPOSURE_USD_MICROS, EXPOSURE_USD_MICROS)
    assert EXPOSURE_USD_MICROS != STUB_USAGE_USD_MICROS
    assert s.row(oid)["state"] == "done"


# ------------------------------------------------------------------------------ a manual run against the ceiling

def test_p10b_a_manual_run_against_the_ceiling(s):
    """With the link's signed ``run_ceiling`` at 1: a second claim while the first is in flight is held off
    (``skipped-overlap``); once the first has run to ``done``, the next is refused by the CEILING, ends ``skipped-budget``,
    and takes no slot, no reservation and no fire: the vendor was started for the first only."""
    s.register(run_ceiling=1)
    first = s.launched()
    assert s.claim()[1] == "skipped-overlap"
    assert s.fired(first) == 0 and s.row(first)["state"] == "done"

    second = s.claimed()
    res = s.launch(second)
    assert (res.outcome, res.reason) == ("skipped-budget", "ceiling")
    row = s.row(second)
    assert (row["kind"], row["state"], row["reason"], row["launched_at"]) == ("manual", "skipped-budget", "ceiling", None)
    assert s.held(second) == ([], [])
    assert s.fired(second) == 0 and s.started() == 1
    assert sorted(o["state"] for o in s.occurrences() if o["kind"] == "manual") == [
        "done", "skipped-budget", "skipped-overlap"]

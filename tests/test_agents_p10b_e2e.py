"""P10b (levain#215): P7's cases for a session agent, run through the ``levain`` CLI as a subprocess with a stub launcher.

The cases are the ones P10b names: register, sign, tick, fire, ``done``; a vendor exit that is not zero is not ``done``; END
unproven stays ``launched``; a revoke mid-session ends the row ``stopped``; expiry mid-run; duplicate delivery; unreadable
usage refused before any start (``no_exposure``); a manual run against the ceiling. They run on ``tests/_p10b_rig.py`` (the
P10a install, a session agent registered on it) and ``tests/_p10b_entry.py`` (the stub launcher, in the CLI process and in
the fire child). The stub's vendor is a real process behind the product's own ``GatedChild`` gate: the exit status a run reads
is a real exit status. What this proves is the STUB path on this machine: the real launcher, a real vendor and a real S1 END
are the VM's and Gate 2's own nights.

Every case was RUN by hand on a tmp install first and is pinned to what the run gave. A SCHEDULED row is claimed and launched
by the CLI's tick (levain#219), its fire's spawn recorded by the spawn gate and the fire then started by the case; a MANUAL
row is claimed and launched in-process (``levain agents`` has no hand start; see ``tests/_p10b_rig.py``).
"""
from __future__ import annotations

import io
import json
import shutil
import subprocess
from datetime import datetime, timedelta, timezone

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


def test_p10b_a_claimed_row_whose_run_was_never_built_is_recovered_once_its_lease_lapses(s):
    """codex #1 (L3 r1): a fire that CLAIMED its row (its ``pid`` written) and then could not build its run (here the
    launcher is not registered in the fresh fire process) exits 1 and leaves the row ``launched`` with a pid and no
    session key: no session was opened and no vendor ran. The first tick past the launch lease ends it ``stopped`` /
    ``crash_recovered`` through the fenced takeover (epoch raised, slot freed, reservation settled as unread), names it
    and exits 6, and, since that runs before the claims, the same tick claims and launches the agent's next slot rather
    than filing it ``skipped-overlap``. A second tick at that instant finds nothing more to end."""
    from levain.agents import controller as ctl

    s.register()
    assert s.cli("agents", "tick", now=_after(0)).rc == 0          # sets the agent's cursor
    oid = s.launched()
    proc = ctl.spawn_fire(s.store_dir, oid, env=s.env())
    s._fires.append(proc)
    assert proc.wait(120) == 1
    assert f"the install names launcher {STUB_LAUNCHER!r}, which is not registered" in s.fire_log(oid)
    before = s.row(oid)
    assert (before["state"], before["session_key"], s.started()) == ("launched", None, 0)
    assert type(before["pid"]) is int
    slot = (real_now() + timedelta(seconds=ctl.LEASE_S + 60)).replace(second=0, microsecond=0)
    while slot.minute != 5:
        slot += timedelta(minutes=1)
    lapsed = (slot + timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    past = s.cli("agents", "tick", now=lapsed)
    row = s.row(oid)
    assert (row["state"], row["reason"], row["epoch"]) == ("stopped", "crash_recovered", before["epoch"] + 1), past.text
    assert past.rc == 6 and f"{oid} ({before['key']}) was launched as a session run" in past.err, past.text
    assert s.held(oid) == ([], [("settled",)])
    res = s.reservation(oid)
    assert (res["settled_reason"], res["debited_usd_micros"]) == ("unreadable", res["reserved_usd_micros"])
    (nxt,) = [r for r in s.rows() if r["key"] == slot.strftime("%Y-%m-%dT%H:%M:%SZ")]
    assert nxt["state"] == "launched" and s.spawned() == [nxt["occurrence_id"]], past.text
    again = s.cli("agents", "tick", now=lapsed)
    assert again.rc == 0 and oid not in again.err, again.text


# ---------------------------------------------------------------------------- register, sign, tick, fire, done

RUN_FIGURE = 20_000_000      # the link's signed budget_run_usd_micros (``Session.link_doc``): what a tick launch reserves


def _at(day, hh: int, mm: int) -> str:
    return utc(day.year, day.month, day.day, hh, mm, 0)


def _tick(s: Session, now: str, rc: int = 0):
    got = s.cli("agents", "tick", now=now)
    assert got.rc == rc, got.text
    return got


def _install(s: Session, launcher: str | None) -> None:
    """Set-up: the install file naming ``launcher`` (None: no launcher, the unwired default)."""
    from levain.agents import session_wire

    session_wire.write_install(s.store_dir, cockpit_root=s.world.root, launcher=launcher, episodic_path=s.episodic)


def _admitted_unlaunched(s: Session, day, *, launcher: str | None = None) -> str:
    """The agent's 12:05 slot claimed by the tick with the install naming ``launcher`` (none, or one not registered), so
    nothing could launch it: the row stays ``admitted``, holding nothing, the tick says so and exits 6 (np-4c157a8f (a)).
    Its id."""
    _install(s, launcher)
    _tick(s, _at(day, 12, 4))
    got = _tick(s, _at(day, 12, 6), rc=6)
    (row,) = s.rows()
    oid = row["occurrence_id"]
    assert (row["kind"], row["state"], row["launched_at"]) == ("scheduled", "admitted", None)
    assert s.held(oid) == ([], []) and s.spawned() == [] and s.started() == 0
    assert "no usable session launcher, so nothing was launched" in got.err, got.text
    return oid


def test_p10b_the_tick_launches_a_session_window(s):
    """The scheduled path (levain#219). The tick, run as a CLI process at two fixed instants either side of the agent's
    :05 slot, claims the window and LAUNCHES it under the signed link: the row is ``launched``, its reservation open at
    the link's whole signed per-run figure (np-fe3e2ebd (a)), one slot held, and the fire's spawn asked once for this
    occurrence (recorded by the spawn gate). No vendor has run: only a fire starts one."""
    s.register()
    day = real_now() + timedelta(days=1)
    first = _tick(s, _at(day, 12, 4))
    assert s.rows() == [], first.text                       # the first pass only sets the agent's cursor
    _tick(s, _at(day, 12, 6))
    (row,) = s.rows()
    oid = row["occurrence_id"]
    assert (row["kind"], row["state"], row["key"], row["launched_at"]) == (
        "scheduled", "launched", f"{day:%Y-%m-%d}T12:05:00Z", f"{day:%Y-%m-%d}T12:06:00Z")
    assert row["link_id"] is not None and row["session_key"] is None
    slots, reservations = s.held(oid)
    assert len(slots) == 1 and reservations == [("open",)]
    res = s.reservation(oid)
    assert (res["reserved_usd_micros"], res["billing"]) == (RUN_FIGURE, None)    # an api_key reservation names no billing
    assert (s.spawned(), s.started()) == ([oid], 0)
    assert [(o["state"], o["launched_at"]) for o in s.occurrences()] == [("launched", f"{day:%Y-%m-%d}T12:06:00Z")]


def test_p10b_a_second_tick_at_the_same_instant_launches_nothing_twice(s):
    """A tick at the very instant of the tick that launched the row: nothing moves. The row keeps the launch columns the
    first tick wrote, one reservation and one slot, and the fire's spawn was asked once, not twice."""
    s.register()
    day = real_now() + timedelta(days=1)
    _tick(s, _at(day, 12, 4))
    _tick(s, _at(day, 12, 6))
    (before,) = s.rows()
    oid = before["occurrence_id"]
    assert before["state"] == "launched" and s.spawned() == [oid]
    held = s.held(oid)
    _tick(s, _at(day, 12, 6))
    (after,) = s.rows()
    assert after == {**before, "lease_until": after["lease_until"]}   # the reaper may renew the lease; nothing else
    assert s.held(oid) == held and len(held[0]) == 1 and held[1] == [("open",)]
    assert s.spawned() == [oid] and s.started() == 0


def test_p10b_register_sign_tick_fire_done(s):
    """invariant 7's whole path for a session agent. Registered and signed by the CLI, claimed and launched by the CLI's
    tick, fired as a real detached process, ``done`` as ``levain agents show --json`` reads it, with the one episode
    under the agent's id. The vendor was asked once, for this occurrence, on the brokered route with the reserved dollars
    (the link's signed per-run figure). The stub states the usage it read, zero dollars, so the reservation is settled on
    that actual and debits none of it."""
    s.register()
    day = real_now() + timedelta(days=1)
    _tick(s, _at(day, 12, 4))
    _tick(s, _at(day, 12, 6))
    (claimed,) = s.rows()
    oid = claimed["occurrence_id"]
    assert s.row(oid)["state"] == "launched" and s.spawned() == [oid] and s.started() == 0

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
        oid, s.row(oid)["session_key"], "brokered", RUN_FIGURE)
    _ended_clean(s, oid)
    res = s.reservation(oid)
    assert (res["reserved_usd_micros"], res["settled_reason"], res["debited_usd_micros"]) == (
        RUN_FIGURE, "actual", STUB_USAGE_USD_MICROS)


# ------------------------------------------------------------------------- what keeps the tick from launching

@pytest.mark.parametrize("launcher", [None, "p10b-not-registered"])
def test_p10b_no_usable_launcher_leaves_the_row_admitted_until_its_grace(s, launcher):
    """np-4c157a8f (a): an install that names no launcher, or one this process has not registered, launches nothing. The
    row stays ``admitted`` holding no slot and no reservation, the tick says so and exits 6, and it is retried each tick
    until its grace is gone, when it ends ``missed`` / ``never_launched``."""
    s.register()
    day = real_now() + timedelta(days=1)
    oid = _admitted_unlaunched(s, day, launcher=launcher)
    named = "the install names no launcher" if launcher is None else f"launcher {launcher!r}, which is not registered"
    again = _tick(s, _at(day, 12, 10), rc=6)
    assert named in again.err, again.text
    assert (s.row(oid)["state"], s.held(oid), s.spawned()) == ("admitted", ([], []), [])
    _tick(s, _at(day, 12, 25))
    row = s.row(oid)
    assert (row["state"], row["reason"], row["launched_at"]) == ("missed", "never_launched", None)
    assert s.held(oid) == ([], []) and s.spawned() == [] and s.started() == 0


def test_p10b_a_paused_agent_s_admitted_row_ends_stopped_paused(s):
    """An admitted row whose agent is paused (``agents pause``) before the tick that could launch it: the launch ends it
    ``stopped`` / ``paused``, taking nothing, and no fire is asked for."""
    s.register()
    day = real_now() + timedelta(days=1)
    oid = _admitted_unlaunched(s, day)
    paused = s.cli("agents", "pause", "p10b-agent")
    assert paused.rc == 0, paused.text
    _install(s, STUB_LAUNCHER)
    _tick(s, _at(day, 12, 7))
    row = s.row(oid)
    assert (row["state"], row["reason"], row["launched_at"]) == ("stopped", "paused", None)
    assert s.held(oid) == ([], []) and s.spawned() == []


def test_p10b_a_revoked_agent_s_admitted_row_ends_stopped_revoked(s):
    """``levain agents revoke`` after the claim and before the tick that could launch it: the launch finds no live link
    and ends the row ``stopped`` / ``revoked``, taking nothing; no fire is asked for."""
    s.register()
    day = real_now() + timedelta(days=1)
    oid = _admitted_unlaunched(s, day)
    revoked = s.cli("agents", "revoke", s.agent()["agent_id"], "--data-root", str(s.world.root), "--signing-key",
                    str(s.key))
    assert revoked.rc == 0, revoked.text
    assert s.row(oid)["state"] == "admitted"
    _install(s, STUB_LAUNCHER)
    _tick(s, _at(day, 12, 7))
    row = s.row(oid)
    assert (row["state"], row["reason"], row["launched_at"]) == ("stopped", "revoked", None)
    assert s.held(oid) == ([], []) and s.spawned() == []


def test_p10b_an_admitted_row_whose_link_expired_ends_stopped_expired(s):
    """A link that expires between the claim of a slot and the tick that could launch it (inside the slot's grace): the
    launch ends the row ``stopped`` / ``expired``, taking nothing; no fire is asked for. The ticks are fixed instants
    near the real clock, since a link is signed at the real clock."""
    from levain.cockpit.consent import utc_stamp

    slot = (real_now() + timedelta(minutes=3)).replace(second=0, microsecond=0)
    while slot.minute != 5:
        slot += timedelta(minutes=1)

    def at(minutes: int) -> str:
        return (slot + timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")

    s.register(expires_at=utc_stamp(slot + timedelta(minutes=5)))
    _install(s, None)
    _tick(s, at(-1))
    _tick(s, at(1), rc=6)
    (row,) = s.rows()
    oid = row["occurrence_id"]
    assert row["state"] == "admitted"
    _install(s, STUB_LAUNCHER)
    _tick(s, at(7))
    row = s.row(oid)
    assert (row["state"], row["reason"], row["launched_at"]) == ("stopped", "expired", None)
    assert s.held(oid) == ([], []) and s.spawned() == []


def test_p10b_the_run_ceiling_refuses_the_next_tick_launch(s):
    """With the link's signed ``run_ceiling`` at 1: the 12:05 slot is launched by the tick and run to ``done``; the 13:05
    slot of the same day's window is refused by the CEILING at its launch, ``skipped-budget`` / ``ceiling``, taking no slot
    and no reservation, and no fire is asked for it."""
    s.register(run_ceiling=1)
    day = real_now() + timedelta(days=1)
    _tick(s, _at(day, 12, 4))
    _tick(s, _at(day, 12, 6))
    (first,) = s.rows()
    assert s.fired(first["occurrence_id"]) == 0, s.fire_log(first["occurrence_id"])
    assert s.row(first["occurrence_id"])["state"] == "done"
    _tick(s, _at(day, 13, 6))
    second = [r for r in s.rows() if r["occurrence_id"] != first["occurrence_id"]]
    assert [(r["key"], r["state"], r["reason"], r["launched_at"]) for r in second] == [
        (f"{day:%Y-%m-%d}T13:05:00Z", "skipped-budget", "ceiling", None)]
    assert s.held(second[0]["occurrence_id"]) == ([], [])
    assert s.spawned() == [first["occurrence_id"]] and s.started() == 1


def test_p10b_the_tick_launches_a_subscription_window_and_its_fire_runs_on_the_reserved_caps(s):
    """A SUBSCRIPTION link through the tick: the launch reserves no dollars and the link's whole per-run caps (its calls
    and bytes) with the signed spec's ``stop_at`` as the seconds, and the fire, started on that row, accepts those caps
    as its own (``session.launch_refusal``) and runs the vendor once on the passthrough route to ``done``."""
    from levain.agents.session import AUTH_FOR_BILLING

    s.serve_subscription()
    s.register(billing="subscription", budget_run_usd_micros=0, budget_window_usd_micros=0, budget_run_calls=40,
               budget_run_tunnel_bytes=9000)
    day = real_now() + timedelta(days=1)
    _tick(s, _at(day, 12, 4))
    _tick(s, _at(day, 12, 6))
    (row,) = s.rows()
    oid = row["occurrence_id"]
    assert row["state"] == "launched" and s.spawned() == [oid]
    res = s.reservation(oid)
    assert (res["billing"], res["reserved_usd_micros"], res["cap_actions"], res["cap_tunnel_bytes"],
            res["cap_seconds"]) == ("subscription", 0, 40, 9000, s.shown()["spec"]["stop_at"])
    assert s.fired(oid) == 0, s.fire_log(oid)
    assert (s.row(oid)["state"], s.row(oid)["reason"]) == ("done", "completed")
    (asked,) = s.start_records()
    assert (asked["occurrence_id"], asked["auth"], asked["usd_micros"]) == (
        oid, AUTH_FOR_BILLING["subscription"], None)
    _ended_clean(s, oid)


def test_p10b_a_link_whose_per_run_figure_is_zero_is_not_launched_and_the_tick_names_it(s):
    """np-a7c2a08c (a): an api_key link signed with a per-run figure of 0 states no exposure a session run could spend
    under, so the tick launches nothing: the row stays ``admitted`` holding nothing, no fire is asked for, and the tick
    exits 6 with the cause named (the zero figure), not a generic refusal."""
    s.register(budget_run_usd_micros=0)
    day = real_now() + timedelta(days=1)
    _tick(s, _at(day, 12, 4))
    got = _tick(s, _at(day, 12, 6), rc=6)
    (row,) = s.rows()
    assert row["state"] == "admitted" and s.held(row["occurrence_id"]) == ([], []) and s.spawned() == []
    assert "signs a per-run figure (budget_run_usd_micros) of 0" in got.err, got.text


# -------------------------------------------- the tick's session launch in this process, so a case can move a seam

@pytest.fixture
def here(s, monkeypatch):
    """``tick_driver.run_tick`` run in THIS process at a fixed instant (or on a case's ``clock``), with the stub launcher
    registered in this process. A fire's spawn is recorded in ``here.spawned`` (or a case passes its own ``spawn``).
    Answers ``(exit code, stderr text)``."""
    from levain.agents import session_wire
    from levain.agents import tick_driver as T
    from tests._p10b_entry import StubLauncher

    monkeypatch.setitem(session_wire.LAUNCHERS, STUB_LAUNCHER, lambda store: StubLauncher(s.stubdir))
    spawned: list[str] = []

    def tick(now: str, *, spawn=None, clock=None) -> tuple[int, str]:
        at = datetime.strptime(now, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        out = io.StringIO()
        rc = T.run_tick(s.store_dir, clock=clock or (lambda: at), stream=out,
                        spawn=spawn or (lambda directory, oid: spawned.append(oid)))
        return rc, out.getvalue()

    tick.spawned = spawned
    return tick


def _second_agent(s: Session) -> str:
    """A second session agent on the same role and schedule, registered and signed through the CLI: its agent id."""
    from tests._p10b_rig import _spec as rig_spec

    spec = rig_spec(s.zone)
    spec["name"] = "p10b-agent-b"
    path = s.tmp / "spec-b.json"
    path.write_text(json.dumps(spec), encoding="utf-8")
    registered = s.cli("agents", "register", "--from", str(path))
    assert registered.rc == 0, registered.text
    shown = s.cli("agents", "show", "p10b-agent-b", "--json")
    assert shown.rc == 0, shown.text
    agent = json.loads(shown.out)["agent"]
    signed = s.sign({**s.link_doc(), "agent_id": agent["agent_id"], "spec_hash": agent["spec_hash"],
                     "binding_id": "bind-fedcba9876543210"})
    assert signed.rc == 0, signed.text
    return agent["agent_id"]


def test_p10b_a_link_signed_between_the_pick_and_the_launch_ends_the_row_and_starts_no_fire(s, here, monkeypatch):
    """Codex #2 (L3 r1), the pick-versus-launch race: the tick reads its exposure from link A (the run figure), then a
    newer link B with a larger per-run figure is signed before the launch replays the authority, so the launch admits B
    while reserving A's figure. That row is never run: no fire is asked for, and it is ended ``stopped`` / ``link_moved``
    at once, its reservation settled on the zero it spent and its slot freed, the agent's failure streak untouched; the
    tick names it and exits 6."""
    from levain.agents import controller as ctl

    s.register()
    day = real_now() + timedelta(days=1)
    assert here(_at(day, 12, 4))[0] == 0
    real_launch = ctl.launch

    def launch_after_a_newer_link(*args, **kw):
        signed = s.sign(s.link_doc(budget_run_usd_micros=RUN_FIGURE + 10_000_000))
        assert signed.rc == 0, signed.text
        return real_launch(*args, **kw)

    monkeypatch.setattr(ctl, "launch", launch_after_a_newer_link)
    rc, err = here(_at(day, 12, 6))
    (row,) = s.rows()
    oid = row["occurrence_id"]
    assert (row["state"], row["reason"], rc) == ("stopped", "link_moved", 6), err
    assert row["launched_at"] is not None and f"{oid} was launched under link {row['link_id']}, not link " in err
    assert here.spawned == [] and s.started() == 0
    assert s.held(oid) == ([], [("settled",)])
    res = s.reservation(oid)
    assert (res["reserved_usd_micros"], res["debited_usd_micros"], res["settled_reason"]) == (RUN_FIGURE, 0, "actual")
    (agent,) = s.rows(f"SELECT failure_streak, operation FROM agents WHERE agent_id = '{row['agent_id']}'")
    assert (agent["failure_streak"], agent["operation"]) == (0, "active")


@pytest.mark.parametrize("move", ["smaller", "live_after_a_refusal"])
def test_p10b_the_known_edge_a_link_move_the_launch_refuses_ends_the_row_unlaunched(s, here, monkeypatch, move):
    """The pick-versus-launch race's KNOWN EDGE: when the link moves
    between the tick's pick and the launch's, and the launch REFUSES rather than admits, the launch's refusal is what the
    row ends with, never launched and not retried: a newer link with a smaller per-run figure refuses the stated figure
    ``run_budget``; a link the launch finds live after the tick's own pick refused (here the tick's pick is made to
    refuse once) meets the executor that states nothing and refuses it ``no_exposure``. Nothing is held, no fire is
    asked for."""
    from levain.agents import controller as ctl

    s.register()
    day = real_now() + timedelta(days=1)
    assert here(_at(day, 12, 4))[0] == 0
    if move == "smaller":
        real_launch = ctl.launch

        def launch_after_a_smaller_link(*args, **kw):
            signed = s.sign(s.link_doc(budget_run_usd_micros=RUN_FIGURE // 2))
            assert signed.rc == 0, signed.text
            return real_launch(*args, **kw)

        monkeypatch.setattr(ctl, "launch", launch_after_a_smaller_link)
        want = ("skipped-budget", "run_budget")
    else:
        real_pick = ctl._pick_link
        calls = [0]

        def refuses_once(*args, **kw):
            calls[0] += 1
            if calls[0] == 1:
                raise ctl._Refusal(ctl.STOPPED, "no_link")
            return real_pick(*args, **kw)

        monkeypatch.setattr(ctl, "_pick_link", refuses_once)
        want = ("skipped-budget", "no_exposure")
    rc, err = here(_at(day, 12, 6))
    (row,) = s.rows()
    oid = row["occurrence_id"]
    assert (row["state"], row["reason"], row["launched_at"], rc) == (*want, None, 0), err
    assert s.held(oid) == ([], []) and here.spawned == []


def test_p10b_a_link_that_cannot_be_read_at_the_pick_leaves_the_row_admitted_for_the_next_tick(s, here, monkeypatch):
    """The tick's own pick finds the authority cover moved (``controller._Retry``): nothing is launched, the row stays
    ``admitted`` holding nothing, the tick exits 6 naming it, and the next tick, the cover readable again, launches it."""
    from levain.agents import controller as ctl

    s.register()
    day = real_now() + timedelta(days=1)
    assert here(_at(day, 12, 4))[0] == 0
    real_pick = ctl._pick_link

    def moved(*args, **kw):
        raise ctl._Retry(ctl.UNCLASSIFIED, "the authority cover moved: a case says so")

    monkeypatch.setattr(ctl, "_pick_link", moved)
    rc, err = here(_at(day, 12, 6))
    (row,) = s.rows()
    oid = row["occurrence_id"]
    assert (row["state"], rc) == ("admitted", 6) and s.held(oid) == ([], []) and here.spawned == [], err
    assert f"{oid} was not launched: its link could not be read (the authority cover moved: a case says so)" in err
    monkeypatch.setattr(ctl, "_pick_link", real_pick)
    rc, err = here(_at(day, 12, 7))
    assert (s.row(oid)["state"], rc, here.spawned) == ("launched", 0, [oid]), err


def test_p10b_a_fire_that_cannot_be_spawned_is_named_and_its_row_is_ended_once_its_lease_lapses(s, here):
    """A launched row whose fire cannot be spawned: the tick names it and exits 6, and the row (launched, no fire, no
    pid) holds its slot until its launch lease lapses, when the next tick ends it ``stopped`` / ``crash_recovered``, its
    slot freed and its reservation settled."""
    s.register()
    day = real_now() + timedelta(days=1)
    assert here(_at(day, 12, 4))[0] == 0

    def cannot(directory, oid):
        raise OSError("no fire for a case")

    rc, err = here(_at(day, 12, 6), spawn=cannot)
    (row,) = s.rows()
    oid = row["occurrence_id"]
    assert (row["state"], row["pid"], rc) == ("launched", None, 6), err
    assert f"{oid} launched but its fire could not be started (OSError: no fire for a case)" in err
    lapsed = datetime.strptime(row["lease_until"], "%Y-%m-%dT%H:%M:%SZ") + timedelta(minutes=1)
    rc, err = here(lapsed.strftime("%Y-%m-%dT%H:%M:%SZ"))
    row = s.row(oid)
    assert (row["state"], row["reason"], rc) == ("stopped", "crash_recovered", 6), err
    assert s.held(oid) == ([], [("settled",)])


@pytest.mark.parametrize("how", ["raises", "unreadable"])
def test_p10b_one_row_that_cannot_be_classified_costs_the_other_agent_s_row_nothing(s, here, monkeypatch, how):
    """Two session agents' slots admitted in one pass; the role of the FIRST cannot be read (the classification raises,
    or answers no executor). That row is named and left ``admitted``, the tick exits 6, and the second agent's row is
    launched and its fire asked for in the same pass."""
    from levain.agents import controller as ctl

    s.register()
    _second_agent(s)
    day = real_now() + timedelta(days=1)
    assert here(_at(day, 12, 4))[0] == 0
    real_pinned = ctl._pinned_executor
    first: list[str] = []

    def pinned(store, oid):
        if not first:
            first.append(oid)
        if oid == first[0]:
            if how == "raises":
                raise RuntimeError("a spec this case broke")
            return None, None, "a spec this case broke"
        return real_pinned(store, oid)

    monkeypatch.setattr(ctl, "_pinned_executor", pinned)
    rc, err = here(_at(day, 12, 6))
    rows = {r["occurrence_id"]: r for r in s.rows()}
    assert len(rows) == 2 and rc == 6, err
    (good,) = [oid for oid in rows if oid != first[0]]
    assert (rows[first[0]]["state"], rows[good]["state"], here.spawned) == ("admitted", "launched", [good]), err
    assert f"{first[0]} is admitted but its signed spec or pinned role cannot be read (" in err
    assert "a spec this case broke" in err


def test_p10b_two_rows_in_one_pass_each_launch_at_their_own_clock_reading(s, here):
    """Two session agents' slots admitted in one pass, on a clock that moves a second each time it is read: both are
    launched and spawned, each at the clock read for its own row, so the later row's launch instant is the later one."""
    s.register()
    _second_agent(s)
    day = real_now() + timedelta(days=1)
    assert here(_at(day, 12, 4))[0] == 0
    start = datetime.strptime(_at(day, 12, 6), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    reads = [0]

    def moving():
        reads[0] += 1
        return start + timedelta(seconds=reads[0])

    rc, err = here(_at(day, 12, 6), clock=moving)
    rows = sorted(s.rows(), key=lambda r: r["admitted_at"] + r["occurrence_id"])
    assert rc == 0 and [r["state"] for r in rows] == ["launched", "launched"], err
    assert sorted(here.spawned) == sorted(r["occurrence_id"] for r in rows)
    assert rows[0]["launched_at"] < rows[1]["launched_at"], rows


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

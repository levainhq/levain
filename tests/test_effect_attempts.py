"""Execution-time checks and the effect_attempts journal (ND-3b, board levain#12). DONE line, written first: a revoke
between request and run refuses; a budget spent refuses; a crash mid-effect recovers to one recorded attempt. Real signed
orders in a real log (the verbs' own ``World``) and a real journal file; the executors are stubs, except the crash test,
which runs the effect in a child process and kills it with SIGKILL while it is mid-effect."""
from __future__ import annotations

import hashlib
import os
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
from datetime import timedelta
from pathlib import Path

import pytest

from levain.cockpit.consent import utc_stamp
from levain.desk import effects as fx
from levain.desk import standing_orders as so
from levain.desk.effect_attempts import BudgetSpent, Journal, idem_key, night_key

pytest.importorskip("cryptography")
from tests.test_effect_verbs import (ACK_LINE, EVR, EVT, FROM, INC, D, Rig, call, event_for, monitor_event,  # noqa: E402,F401
                                     raw_mail, signed, world)
from tests.test_standing_order_signing import LINE, make, sign  # noqa: E402

pytestmark = pytest.mark.skipif(shutil.which("ssh-keygen") is None, reason="needs ssh-keygen")
REPO = Path(__file__).resolve().parent.parent
EVR2, EVR3 = "evt-" + "d" * 32, "evt-" + "e" * 32
INC2, INC3 = "inc-" + "1" * 32, "inc-" + "2" * 32
EVT2 = "evt-" + "f" * 32


def restart(rig: Rig, g: dict, event_id: str = EVR, service: str = "apache2", **over):
    return rig.effects(**over).restart_service(g["id"], "taoserv", service, event_id=event_id, expect_sha256=D(g))


def refused(call_):
    with pytest.raises(fx.EffectRefused) as exc:
        call_()
    return str(exc.value)


def serve_events(rig: Rig, **by_id):
    """An event lookup that knows several monitor events (incidents) and the mail event."""
    def events(event_id):
        rig.looked.append(event_id)
        if event_id in by_id:
            return by_id[event_id]
        return rig.monitor if event_id == EVR else rig.event
    return events


# ---- DONE 1: a revoke between request and run refuses ------------------------------------------------------------------
def test_a_revoke_between_the_request_and_the_run_refuses_a_restart(world):
    g = signed(world)
    rig = Rig(world)

    def events(event_id):                                    # the request is in flight: the order is revoked here
        world.link({"op": "revoke_grant", "grant_id": g["id"]}, today=True)
        return rig.monitor

    assert refused(lambda: restart(rig, g, events=events)) == fx.R_ORDER
    assert rig.runs == []                                    # the process never ran
    assert rig.journal.refusals() == [(g["id"], "restart_service", EVR, fx.R_ORDER, 1)]


def test_a_revoke_between_the_reservation_and_the_run_refuses_and_gives_the_budget_back(world):
    """The second replay is AFTER the reservation, so the window that is left is the dispatch itself: the attempt is
    settled ``refused`` (nothing ran), is not counted against the budget, and is journalled with its reason."""
    g = signed(world)
    rig = Rig(world)
    real = fx.Effects.gate
    seen = {"n": 0}

    def gate_then_revoke(self, order_id, verb, *, expect_sha256):
        out = real(self, order_id, verb, expect_sha256=expect_sha256)
        seen["n"] += 1
        if seen["n"] == 1:                                   # after the first replay, before the reservation's replay
            world.link({"op": "revoke_grant", "grant_id": g["id"]}, today=True)
        return out

    fx.Effects.gate = gate_then_revoke
    try:
        assert refused(lambda: restart(rig, g)) == fx.R_ORDER
    finally:
        fx.Effects.gate = real
    assert rig.runs == []
    [row] = rig.journal.rows()
    assert row.status == "refused" and row.verb == "restart_service"
    assert rig.journal.refusals() == [(g["id"], "restart_service", EVR, fx.R_ORDER, 1)]


def test_a_revoke_between_the_request_and_the_send_refuses_an_ack(world):
    g = signed(world)
    rig = Rig(world)

    def drafter(event_id):                                   # the slow part: the drafter runs, the order is revoked
        rig.drafts.append(event_id)
        world.link({"op": "revoke_grant", "grant_id": g["id"]}, today=True)
        return fx.Draft("Thank you for your message. It arrived and has been passed on.", "stand_in")

    assert refused(lambda: rig.effects(drafter=drafter).send_ack(g["id"], EVT, expect_sha256=D(g))) == fx.R_ORDER
    assert rig.sent == [] and [r.status for r in rig.journal.rows()] == ["refused"]


def test_an_expired_widening_between_the_request_and_the_run_refuses(world):
    base = signed(world, ACK_LINE)
    wide = make(world, widens=base["id"], widens_sha256=so.base_sha256(world.state(), base["id"], world.clock.now),
                effect_lines=[LINE], life=timedelta(minutes=30))
    sign(world, wide)
    rig = Rig(world)

    def events(event_id):
        world.clock.now += timedelta(minutes=31)             # the widening lapses while the request is in flight
        return rig.monitor

    assert refused(lambda: rig.effects(events=events).restart_service(
        wide["id"], "taoserv", "apache2", event_id=EVR, expect_sha256=D(wide))) == fx.R_ORDER
    assert rig.runs == []


def test_the_effect_runs_only_as_the_version_it_was_requested_under(world):
    """A record revoked and signed again, differently, under the same id between the request and the run is not the
    version asked for: refused before anything ran."""
    g = signed(world)
    rig = Rig(world)

    def events(event_id):
        world.link({"op": "revoke_grant", "grant_id": g["id"]}, today=True)
        g2 = make(world, order_id=g["id"], effect_lines=[LINE, ACK_LINE])
        sign(world, g2)
        return rig.monitor

    assert refused(lambda: restart(rig, g, events=events)) in (fx.R_VERSION, fx.R_ORDER)
    assert rig.runs == []


# ---- DONE 2: a budget spent refuses (checked and consumed with the claim) -------------------------------------------
def test_the_per_night_budget_is_spent_after_two_restarts(world):
    g = signed(world)                                        # LINE: 1 per incident, 2 per night
    rig = Rig(world)
    by = {EVR2: monitor_event(event_id=EVR2, incident_id=INC2), EVR3: monitor_event(event_id=EVR3, incident_id=INC3)}
    ev = serve_events(rig, **by)
    assert restart(rig, g, EVR, events=ev).ok
    assert restart(rig, g, EVR2, "php8.1-fpm", events=ev).ok
    assert refused(lambda: restart(rig, g, EVR3, events=ev)) == fx.R_BUDGET_NIGHT
    assert len(rig.runs) == 2 and len(rig.journal.rows()) == 2          # the refused third wrote no reservation
    assert (g["id"], "restart_service", EVR3, fx.R_BUDGET_NIGHT, 1) in rig.journal.refusals()
    world.clock.now += timedelta(days=1)                                # the next night: events are fresh again
    fresh = {EVR3: monitor_event(event_id=EVR3, incident_id=INC3, received_at=utc_stamp(world.clock.now))}
    assert restart(rig, g, EVR3, events=serve_events(rig, **fresh)).ok and len(rig.runs) == 3


def test_the_per_incident_budget_is_one_per_service(world):
    g = signed(world)
    rig = Rig(world)
    other = monitor_event(event_id=EVR2)                                # a second event, the SAME incident
    ev = serve_events(rig, **{EVR2: other})
    assert restart(rig, g, EVR, events=ev).ok
    assert refused(lambda: restart(rig, g, EVR2, events=ev)) == fx.R_BUDGET_INCIDENT      # same service, same incident
    assert restart(rig, g, EVR2, "php8.1-fpm", events=ev).ok                               # the other service is its own
    assert len(rig.runs) == 2


def test_a_failed_or_unknown_attempt_still_spends_the_budget(world):
    g = signed(world)
    rig = Rig(world)
    ev = serve_events(rig, **{EVR2: monitor_event(event_id=EVR2, incident_id=INC2),
                              EVR3: monitor_event(event_id=EVR3, incident_id=INC3)})
    assert not restart(rig, g, EVR, events=ev, runner=lambda a: 1).ok                       # failed
    assert not restart(rig, g, EVR2, events=ev, runner=lambda a: -1).ok                     # a timeout: unknown
    assert refused(lambda: restart(rig, g, EVR3, events=ev)) == fx.R_BUDGET_NIGHT


def test_the_ack_budget_is_one_per_address_per_night(world):
    g = signed(world)                                        # ACK_LINE: 1 per incident, 1 per night
    rig = Rig(world)
    second = event_for(rig.raw, event_id=EVT2, received_at=utc_stamp(world.clock.now))
    ev = serve_events(rig, **{EVT2: second})
    assert rig.effects(events=ev).send_ack(g["id"], EVT, expect_sha256=D(g)).ok
    assert refused(lambda: rig.effects(events=ev).send_ack(g["id"], EVT2, expect_sha256=D(g))) == fx.R_BUDGET_NIGHT
    assert len(rig.sent) == 1


def test_the_budget_is_claimed_atomically_under_a_race(world, tmp_path):
    """Twelve threads reserve twelve different effects against a budget of two: exactly two reservations are written."""
    journal = Journal(tmp_path / "race.db")
    wins, spent, errors = [], [], []
    barrier = threading.Barrier(12)

    def one(i: int) -> None:
        barrier.wait()
        try:
            row, fresh = journal.reserve(key=idem_key(f"evt-{i:032x}", "restart_service", "x"), order_id="so-" + "a" * 12,
                                         order_sha256="0" * 64, incarnation=1, verb="restart_service",
                                         event_id=f"evt-{i:032x}", args_digest="x", recipient=None,
                                         incident_scope=f"apache2|inc-{i}", night_scope="n", per_incident=1, per_night=2,
                                         now="2026-11-15T00:00:00Z")
            wins.append(fresh)
        except BudgetSpent:
            spent.append(i)
        except Exception as exc:                             # noqa: BLE001
            errors.append(repr(exc))

    threads = [threading.Thread(target=one, args=(i,)) for i in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    assert not errors and len(wins) == 2 and len(spent) == 10 and len(journal.rows()) == 2


def test_two_racing_requests_for_one_effect_are_one_attempt(tmp_path):
    journal = Journal(tmp_path / "same.db")
    key = idem_key(EVR, "restart_service", "x")
    fresh_flags = []
    barrier = threading.Barrier(8)

    def one() -> None:
        barrier.wait()
        _row, fresh = journal.reserve(key=key, order_id="so-" + "a" * 12, order_sha256="0" * 64, incarnation=1,
                                      verb="restart_service", event_id=EVR, args_digest="x", recipient=None,
                                      incident_scope="s", night_scope="n", per_incident=5, per_night=5,
                                      now="2026-11-15T00:00:00Z")
        fresh_flags.append(fresh)

    threads = [threading.Thread(target=one) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    assert sorted(fresh_flags) == [False] * 7 + [True] and len(journal.rows()) == 1


# ---- DONE 3: a crash mid-effect recovers to one recorded attempt ----------------------------------------------------
def _crash(world, g, verb: str, tmp_path: Path) -> tuple[Path, Path]:
    journal_path, marker = tmp_path / "crash.db", tmp_path / "marker"
    child = subprocess.Popen(
        [sys.executable, str(REPO / "tests" / "_effect_crash_child.py"), str(REPO), str(world.root), str(journal_path),
         g["id"], D(g), verb, str(marker), world.clock.now.isoformat()],
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    try:
        deadline = time.monotonic() + 90
        while not marker.exists():
            if child.poll() is not None:
                pytest.fail("the child ended before the effect started: " + child.stderr.read().decode()[-800:])
            if time.monotonic() > deadline:
                pytest.fail("the effect never started")
            time.sleep(0.05)
        child.kill()                                         # SIGKILL: the effect is in flight, nothing cleans up
        child.wait(30)
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(30)
        child.stderr.close()
    assert child.returncode == -9
    return journal_path, marker


@pytest.mark.parametrize("verb", ["restart_service", "send_ack"])
def test_a_crash_mid_effect_leaves_one_unknown_attempt_that_is_never_retried(world, tmp_path, verb):
    g = signed(world)
    journal_path, _marker = _crash(world, g, verb, tmp_path)
    journal = Journal(journal_path)
    [row] = journal.rows()
    assert row.status == "unknown" and row.verb == verb and row.order_id == g["id"]
    raw = sqlite3.connect(journal_path).execute("SELECT count(*) FROM effect_attempts").fetchone()[0]
    assert raw == 1
    # a new process meets the journal: the effect is NOT run again, and the answer is the recorded unknown
    rig = Rig(world)
    rig.journal = journal
    if verb == "restart_service":
        res = restart(rig, g)
    else:
        res = rig.effects().send_ack(g["id"], EVT, expect_sha256=D(g))
    assert not res.ok and res.text == f"{verb if verb == 'send_ack' else 'restart_service taoserv apache2'}: {fx.R_UNKNOWN}"
    assert rig.runs == [] and rig.sent == [] and [r.status for r in journal.rows()] == ["unknown"]


# ---- idempotence: a settled effect answers its record; a transport timeout is unknown, never resent ----------------------
def test_a_retry_of_a_settled_restart_returns_the_recorded_result_and_runs_nothing(world):
    g = signed(world)
    rig = Rig(world)
    first = restart(rig, g)
    again = restart(rig, g)
    assert first == again and first.ok and len(rig.runs) == 1 and len(rig.journal.rows()) == 1
    assert rig.journal.rows()[0].status == "ok"


def test_a_retry_of_a_failed_restart_returns_failed_and_does_not_rerun(world):
    g = signed(world)
    rig = Rig(world)
    assert restart(rig, g, runner=lambda a: 7).text == f"restart_service taoserv apache2: {fx.R_FAILED}"
    assert restart(rig, g).text == f"restart_service taoserv apache2: {fx.R_FAILED}" and rig.runs == []


def test_a_transport_timeout_records_unknown_and_a_retry_never_sends_twice(world):
    g = signed(world)
    rig = Rig(world)

    class Slow:
        sender = "thirdmind@levain.example"
        calls = 0

        def send(self, message):
            Slow.calls += 1
            rig.sent.append(message)                         # delivered ...
            raise TimeoutError("the reply timed out")        # ... but the reply was lost

    first = rig.effects(transport=Slow()).send_ack(g["id"], EVT, expect_sha256=D(g))
    assert not first.ok and first.text == f"send_ack: {fx.R_UNKNOWN}"
    again = rig.effects().send_ack(g["id"], EVT, expect_sha256=D(g))      # a good transport this time
    assert again == first and len(rig.sent) == 1 and Slow.calls == 1
    assert [r.status for r in rig.journal.rows()] == ["unknown"]


def test_a_retry_of_a_sent_ack_returns_sent_without_a_second_draft_or_send(world):
    g = signed(world)
    rig = Rig(world)
    assert rig.effects().send_ack(g["id"], EVT, expect_sha256=D(g)).ok
    rig.drafts.clear()
    assert rig.effects().send_ack(g["id"], EVT, expect_sha256=D(g)).text == "send_ack: sent"
    assert len(rig.sent) == 1 and rig.drafts == []


def test_a_retry_after_the_order_is_revoked_refuses_even_though_it_was_settled(world):
    g = signed(world)
    rig = Rig(world)
    assert restart(rig, g).ok
    world.link({"op": "revoke_grant", "grant_id": g["id"]}, today=True)
    assert refused(lambda: restart(rig, g)) == fx.R_ORDER


# ---- the event age cap ------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("delta,ok", [(timedelta(hours=11, minutes=59), True), (timedelta(hours=12, seconds=1), False),
                                      (timedelta(days=3), False), (-timedelta(minutes=4), True),
                                      (-timedelta(minutes=6), False)])
def test_an_event_older_than_the_window_or_from_the_future_refuses(world, delta, ok):
    g = signed(world)
    rig = Rig(world)
    stamp = utc_stamp(world.clock.now - delta)
    rig.monitor = monitor_event(received_at=stamp)
    rig.event = event_for(rig.raw, received_at=stamp)
    if ok:
        assert restart(rig, g).ok
        assert rig.effects().send_ack(g["id"], EVT, expect_sha256=D(g)).ok      # the ack path shares the check
    else:
        assert refused(lambda: restart(rig, g)) == fx.R_EVENT_AGE and rig.runs == []
        assert refused(lambda: rig.effects().send_ack(g["id"], EVT, expect_sha256=D(g))) == fx.R_EVENT_AGE
        assert rig.sent == [] and rig.drafts == []


def test_an_unreadable_event_time_refuses(world):
    g = signed(world)
    rig = Rig(world)
    rig.monitor = monitor_event(received_at="yesterday")
    assert refused(lambda: restart(rig, g)) == fx.R_EVENT_AGE and rig.runs == []


# ---- the sender cross-check ---------------------------------------------------------------------------------------------
def test_the_sender_in_the_journal_row_must_be_the_derived_pinned_recipient(world):
    g = signed(world)
    rig = Rig(world)
    empty = hashlib.sha256(b"").hexdigest()
    key = idem_key(EVT, "send_ack", empty)
    rig.journal.reserve(key=key, order_id=g["id"], order_sha256=D(g), incarnation=1, verb="send_ack", event_id=EVT,
                        args_digest=empty, recipient="mallory@evil.example",
                        incident_scope="x", night_scope="y", per_incident=9, per_night=9, now="2026-11-15T00:00:00Z")
    assert refused(lambda: rig.effects().send_ack(g["id"], EVT, expect_sha256=D(g))) == fx.R_SENDER
    assert rig.sent == [] and rig.drafts == []


# ---- what a restart may answer, and the journal itself ----------------------------------------------------------------------
@pytest.mark.parametrize("over", [dict(kind="mail"), dict(trust="untrusted"), dict(incident_id=None),
                                  dict(incident_id="not-an-incident"), dict(event_id=EVR2)])
def test_a_restart_answers_only_a_trusted_monitor_event_in_an_incident(world, over):
    g = signed(world)
    rig = Rig(world)
    rig.monitor = monitor_event(**over)
    assert refused(lambda: restart(rig, g)) == fx.R_EVENT and rig.runs == [] and rig.journal.rows() == []


def test_without_a_journal_nothing_runs(world):
    g = signed(world)
    rig = Rig(world)
    assert refused(lambda: restart(rig, g, journal=None)) == fx.R_NOT_WIRED
    assert refused(lambda: rig.effects(journal=None).send_ack(g["id"], EVT, expect_sha256=D(g))) == fx.R_NOT_WIRED
    assert rig.runs == [] and rig.sent == [] and rig.drafts == []


def test_a_journal_that_cannot_record_stops_the_effect(world, tmp_path):
    g = signed(world)
    rig = Rig(world)
    journal = Journal(tmp_path / "gone.db")
    journal.path = tmp_path / "no-such-dir" / "x.db"        # the file can no longer be opened
    assert refused(lambda: restart(rig, g, journal=journal)) == fx.R_FAILED and rig.runs == []


def test_the_journal_refuses_to_forget_or_rewrite(world):
    g = signed(world)
    rig = Rig(world)
    assert restart(rig, g).ok
    conn = sqlite3.connect(rig.journal.path)
    with pytest.raises(sqlite3.DatabaseError):
        conn.execute("DELETE FROM effect_attempts")
    with pytest.raises(sqlite3.DatabaseError):               # ok never goes back to unknown, and identity never moves
        conn.execute("UPDATE effect_attempts SET status = 'unknown'")
    with pytest.raises(sqlite3.DatabaseError):
        conn.execute("UPDATE effect_attempts SET event_id = 'evt-x'")
    with pytest.raises(sqlite3.DatabaseError):
        conn.execute("INSERT INTO effect_attempts (idem_key, order_id, order_sha256, incarnation, verb, event_id, "
                     "args_digest, incident_scope, night_scope, status, reserved_at) "
                     "VALUES ('k', 'o', 's', 1, 'v', 'e', 'a', 'i', 'n', 'bogus', 't')")
    conn.close()
    assert [r.status for r in rig.journal.rows()] == ["ok"]


def test_the_recorded_reasons_are_only_this_modules_fixed_sentences(world):
    g = signed(world)
    rig = Rig(world)
    refused(lambda: rig.effects().restart_service(g["id"], "taoserv", "apache2", event_id=EVR, expect_sha256="1" * 64))
    refused(lambda: rig.effects(events=lambda e: (_ for _ in ()).throw(fx.EffectRefused("secret: hunter2"))
                                ).restart_service(g["id"], "taoserv", "apache2", event_id=EVR, expect_sha256=D(g)))
    reasons = {r[3] for r in rig.journal.refusals()}
    assert reasons <= fx.REFUSALS and "hunter2" not in " ".join(reasons)


def test_the_mcp_restart_names_its_event_and_is_idempotent(world):
    g = signed(world)
    rig = Rig(world)
    args = {"order_id": g["id"], "order_sha256": D(g), "host": "taoserv", "service": "apache2", "event_id": EVR}
    one, two = call(rig.effects(), "restart_service", args), call(rig.effects(), "restart_service", args)
    assert one == two and one["isError"] is False and len(rig.runs) == 1


# ---- review fixes (ND-3b fix 1) -----------------------------------------------------------------------------------------
def _reserve(journal: Journal, key: str, night: str, now: str, *, per_night: int = 1, event_id: str = EVR,
             incident: str = "apache2|inc-1"):
    return journal.reserve(key=key, order_id="so-" + "a" * 12, order_sha256="0" * 64, incarnation=1,
                           verb="restart_service", event_id=event_id, args_digest="x", recipient=None,
                           incident_scope=incident, night_scope=night, per_incident=9, per_night=per_night, now=now)


def test_a_refused_row_retaken_in_the_next_night_is_charged_to_that_night(tmp_path):
    """A row refused before the cut and taken again after it is an effect of the NEW night: it is counted there (a second
    effect that night is over a budget of one) and no longer in the old night."""
    journal = Journal(tmp_path / "retake.db")
    row, _ = _reserve(journal, "k1", "2026-11-14", "2026-11-15T11:50:00Z")
    journal.settle(row.attempt_id, "refused", "2026-11-15T11:51:00Z")
    again, fresh = _reserve(journal, "k1", "2026-11-15", "2026-11-15T12:10:00Z")
    assert fresh and again.attempt_id == row.attempt_id and again.status == "unknown"
    with pytest.raises(BudgetSpent):
        _reserve(journal, "k2", "2026-11-15", "2026-11-15T12:20:00Z", event_id=EVR2, incident="apache2|inc-2")
    _reserve(journal, "k3", "2026-11-14", "2026-11-15T11:55:00Z", event_id=EVR3, incident="apache2|inc-3")
    nights = sqlite3.connect(journal.path).execute("SELECT idem_key, night_scope FROM effect_attempts ORDER BY 1").fetchall()
    assert nights == [("k1", "2026-11-15"), ("k3", "2026-11-14")]


def test_the_night_scope_moves_only_on_a_retake(tmp_path):
    """The trigger still refuses any other rewrite of the night: a settled row, or an unknown one, keeps its night."""
    journal = Journal(tmp_path / "edges.db")
    row, _ = _reserve(journal, "k1", "2026-11-14", "2026-11-15T11:50:00Z")
    conn = sqlite3.connect(journal.path)
    with pytest.raises(sqlite3.DatabaseError):               # unknown -> unknown with a new night
        conn.execute("UPDATE effect_attempts SET night_scope = 'x'")
    with pytest.raises(sqlite3.DatabaseError):               # unknown -> ok with a new night
        conn.execute("UPDATE effect_attempts SET night_scope = 'x', status = 'ok'")
    with pytest.raises(sqlite3.DatabaseError):               # nor the incident outside the retake edge (fix 2: it
        conn.execute("UPDATE effect_attempts SET incident_scope = 'x'")      # moves with the row on a retake)
    conn.close()


def test_a_flood_of_refusals_never_blinds_the_record(tmp_path, monkeypatch):
    """Past the cap the record rolls: the oldest refusal is dropped, so the newest is always kept and the table stays
    at the cap."""
    from levain.desk import effect_attempts as ea
    monkeypatch.setattr(ea, "_REFUSALS_MAX", 20)
    journal = Journal(tmp_path / "flood.db")
    for i in range(30):
        journal.note_refusal("so-" + "a" * 12, "send_ack", f"evt-{i:032x}", fx.R_ORDER, f"2026-11-15T00:00:{i:02d}Z")
    journal.note_refusal("so-" + "a" * 12, "send_ack", EVT, fx.R_BUDGET_NIGHT, "2026-11-15T00:01:00Z")
    kept = journal.refusals()
    assert len(kept) == 20
    assert ("so-" + "a" * 12, "send_ack", EVT, fx.R_BUDGET_NIGHT, 1) in kept
    assert ("so-" + "a" * 12, "send_ack", f"evt-{0:032x}", fx.R_ORDER, 1) not in kept     # the oldest went first


def test_a_second_settle_writes_nothing_and_says_so(tmp_path):
    journal = Journal(tmp_path / "settle.db")
    row, _ = _reserve(journal, "k1", "n", "2026-11-15T00:00:00Z")
    assert journal.settle(row.attempt_id, "ok", "2026-11-15T00:00:01Z") is True
    assert journal.settle(row.attempt_id, "failed", "2026-11-15T00:00:02Z") is False
    assert journal.lookup("k1").status == "ok"


def test_one_effect_per_event_whichever_order_asks(world):
    """Two orders covering the same event: the first acts, the second answers the recorded row and acts on nothing (a
    double restart or a double ack is the worse failure)."""
    g, h = signed(world), signed(world)
    rig = Rig(world)
    first = restart(rig, g)
    second = restart(rig, h)
    assert first.ok and second == first and len(rig.runs) == 1
    assert rig.effects().send_ack(g["id"], EVT, expect_sha256=D(g)).ok
    rig.drafts.clear()
    assert rig.effects().send_ack(h["id"], EVT, expect_sha256=D(h)).text == "send_ack: sent"
    assert len(rig.sent) == 1 and rig.drafts == []
    assert [(r.verb, r.order_id) for r in rig.journal.rows()] == [("restart_service", g["id"]), ("send_ack", g["id"])]


def test_two_orders_cannot_double_the_night_budget(world):
    """The budget bounds the box and the address, not the order (design 3.1: 2 restarts per night across all incidents;
    3.2: 1 send per address per night): a second order covering the same effect shares the count."""
    g, h = signed(world), signed(world)
    rig = Rig(world)
    by = {EVR2: monitor_event(event_id=EVR2, incident_id=INC2), EVR3: monitor_event(event_id=EVR3, incident_id=INC3)}
    ev = serve_events(rig, **by)
    assert restart(rig, g, EVR, events=ev).ok
    assert restart(rig, h, EVR2, events=ev).ok
    assert refused(lambda: restart(rig, h, EVR3, events=ev)) == fx.R_BUDGET_NIGHT
    assert refused(lambda: restart(rig, g, EVR3, events=ev)) == fx.R_BUDGET_NIGHT
    assert len(rig.runs) == 2
    second = event_for(rig.raw, event_id=EVT2, received_at=utc_stamp(world.clock.now))
    ev = serve_events(rig, **{EVT2: second})
    assert rig.effects(events=ev).send_ack(g["id"], EVT, expect_sha256=D(g)).ok
    assert refused(lambda: rig.effects(events=ev).send_ack(h["id"], EVT2, expect_sha256=D(h))) == fx.R_BUDGET_NIGHT
    assert len(rig.sent) == 1


def test_two_orders_cannot_double_the_incident_budget(world):
    g, h = signed(world), signed(world)
    rig = Rig(world)
    ev = serve_events(rig, **{EVR2: monitor_event(event_id=EVR2)})       # a second event, the SAME incident
    assert restart(rig, g, EVR, events=ev).ok
    assert refused(lambda: restart(rig, h, EVR2, events=ev)) == fx.R_BUDGET_INCIDENT
    assert len(rig.runs) == 1


def test_a_refused_row_taken_again_by_another_order_runs_under_that_order(tmp_path):
    journal = Journal(tmp_path / "other.db")
    row, _ = _reserve(journal, "k1", "n", "2026-11-15T00:00:00Z")
    journal.settle(row.attempt_id, "refused", "2026-11-15T00:00:01Z")
    again, fresh = journal.reserve(key="k1", order_id="so-" + "b" * 12, order_sha256="1" * 64, incarnation=1,
                                   verb="restart_service", event_id=EVR, args_digest="x", recipient=None,
                                   incident_scope="apache2|inc-1", night_scope="n", per_incident=9, per_night=9,
                                   now="2026-11-15T00:00:02Z")
    assert fresh and again.attempt_id == row.attempt_id and again.order_id == "so-" + "b" * 12
    conn = sqlite3.connect(journal.path)
    with pytest.raises(sqlite3.DatabaseError):               # outside the retake edge the order never moves
        conn.execute("UPDATE effect_attempts SET order_id = 'so-cccccccccccc'")
    conn.close()


@pytest.mark.parametrize("code", [None, True, False, "0", 0.0])
def test_a_runner_that_returns_no_exit_code_is_unknown(world, code):
    g = signed(world)
    rig = Rig(world)
    res = restart(rig, g, runner=lambda a: code)
    assert res.text == f"restart_service taoserv apache2: {fx.R_UNKNOWN}"
    assert [r.status for r in rig.journal.rows()] == ["unknown"]


def test_night_key_cuts_at_the_hour_not_at_midnight():
    from datetime import datetime, timezone
    a = night_key(datetime(2026, 11, 15, 11, 59, tzinfo=timezone.utc), 12)
    b = night_key(datetime(2026, 11, 14, 12, 0, tzinfo=timezone.utc), 12)
    c = night_key(datetime(2026, 11, 15, 12, 0, tzinfo=timezone.utc), 12)
    assert a == b and a != c
    with pytest.raises(ValueError):
        night_key(datetime(2026, 11, 15, 12, 0), 12)


# ---- review fixes (ND-3b fix 2) -----------------------------------------------------------------------------------------
def test_a_refusal_of_our_own_raised_inside_the_runner_is_unknown_and_never_run_again(world):
    """The runner may have started before it raised (``subprocess_runner`` itself raises this module's refusal): the
    attempt is ``unknown``, not ``refused``, so it is never taken again and run a second time."""
    g = signed(world)
    rig = Rig(world)

    def runner(argv):
        rig.runs.append(list(argv))
        raise fx._OwnRefusal(fx.R_ARGS)

    first = restart(rig, g, runner=runner)
    assert not first.ok and first.text == f"restart_service taoserv apache2: {fx.R_UNKNOWN}"
    assert restart(rig, g) == first and len(rig.runs) == 1
    assert [r.status for r in rig.journal.rows()] == ["unknown"]


def test_a_refusal_of_our_own_raised_inside_the_transport_is_unknown_and_never_resent(world):
    g = signed(world)
    rig = Rig(world)

    class Raising:
        sender = "thirdmind@levain.example"

        def send(self, message):
            rig.sent.append(message)
            raise fx._OwnRefusal(fx.R_FAILED)

    first = rig.effects(transport=Raising()).send_ack(g["id"], EVT, expect_sha256=D(g))
    assert first.text == f"send_ack: {fx.R_UNKNOWN}"
    assert rig.effects().send_ack(g["id"], EVT, expect_sha256=D(g)) == first and len(rig.sent) == 1


def test_a_result_the_journal_could_not_write_is_never_answered_ok(world):
    """The effect ran, then the settle failed: the row stays ``unknown`` and the caller is told ``unknown`` too (a
    retry would answer it), never ``ok``."""
    g = signed(world)
    rig = Rig(world)

    def broken_settle(attempt_id, status, now):
        raise sqlite3.OperationalError("disk I/O error")

    rig.journal.settle = broken_settle
    res = restart(rig, g)
    assert not res.ok and res.text == f"restart_service taoserv apache2: {fx.R_UNKNOWN}" and len(rig.runs) == 1
    assert [r.status for r in rig.journal.rows()] == ["unknown"]
    rig.journal.settle = lambda attempt_id, status, now: False             # settled by no one: still not ok
    ack = rig.effects().send_ack(g["id"], EVT, expect_sha256=D(g))
    assert not ack.ok and ack.text == f"send_ack: {fx.R_UNKNOWN}"


def _mallory_row(rig: Rig, g: dict, status: str):
    empty = hashlib.sha256(b"").hexdigest()
    row, _ = rig.journal.reserve(key=idem_key(EVT, "send_ack", empty), order_id=g["id"], order_sha256=D(g),
                                 incarnation=1, verb="send_ack", event_id=EVT, args_digest=empty,
                                 recipient="mallory@evil.example", incident_scope="x", night_scope="y",
                                 per_incident=9, per_night=9, now="2026-11-15T00:00:00Z")
    if status != "unknown":
        rig.journal.settle(row.attempt_id, status, "2026-11-15T00:00:01Z")


def test_a_refused_row_for_another_sender_is_never_taken_again_for_this_one(world):
    g = signed(world)
    rig = Rig(world)
    _mallory_row(rig, g, "refused")
    assert refused(lambda: rig.effects().send_ack(g["id"], EVT, expect_sha256=D(g))) == fx.R_SENDER
    assert rig.sent == [] and rig.drafts == []
    assert [(r.status, r.recipient) for r in rig.journal.rows()] == [("refused", "mallory@evil.example")]


def test_a_racer_that_missed_the_lookup_still_checks_the_recorded_sender(world):
    """The lookup ran before the other process reserved (simulated: the lookup sees nothing); the reservation then
    returns that process's row, whose recipient is checked too."""
    g = signed(world)
    rig = Rig(world)
    _mallory_row(rig, g, "unknown")
    rig.journal.lookup = lambda key: None
    assert refused(lambda: rig.effects().send_ack(g["id"], EVT, expect_sha256=D(g))) == fx.R_SENDER
    assert rig.sent == [] and rig.drafts == []


def test_of_two_racing_acks_only_the_one_holding_the_reservation_drafts(world):
    """The second call arrives while the first is drafting: it answers the first's reservation and drafts nothing."""
    g = signed(world)
    rig = Rig(world)
    inner = []

    def drafter(event_id):
        rig.drafts.append(event_id)
        if len(rig.drafts) == 1:
            inner.append(rig.effects(drafter=drafter).send_ack(g["id"], EVT, expect_sha256=D(g)))
        return fx.Draft("Thank you for your message. It arrived and has been passed on.", "stand_in")

    assert rig.effects(drafter=drafter).send_ack(g["id"], EVT, expect_sha256=D(g)).ok
    assert rig.drafts == [EVT] and len(rig.sent) == 1
    assert inner[0].text == f"send_ack: {fx.R_UNKNOWN}"                    # the first was still in flight


def test_a_looser_order_never_lifts_a_stricter_orders_ceiling(world):
    """Order A allows one restart a night and order B two: after A's restart, B's is over A's ceiling for the box."""
    strict = {**LINE, "per_night": 1}
    a, b = signed(world, strict), signed(world)
    rig = Rig(world)
    ev = serve_events(rig, **{EVR2: monitor_event(event_id=EVR2, incident_id=INC2)})
    assert restart(rig, a, EVR, events=ev).ok
    assert refused(lambda: restart(rig, b, EVR2, events=ev)) == fx.R_BUDGET_NIGHT
    assert len(rig.runs) == 1


def test_a_retake_moves_the_incident_with_the_row(tmp_path):
    """A row first reserved under one incident scope and taken again under another is charged to the new one: a second
    effect there is over a budget of one (before, the row kept the old scope and the new incident counted it nowhere)."""
    journal = Journal(tmp_path / "inc.db")
    row, _ = _reserve(journal, "k1", "n", "2026-11-15T00:00:00Z", incident="a|evt-x")
    journal.settle(row.attempt_id, "refused", "2026-11-15T00:00:01Z")
    again, fresh = journal.reserve(key="k1", order_id="so-" + "a" * 12, order_sha256="0" * 64, incarnation=1,
                                   verb="restart_service", event_id=EVR, args_digest="x", recipient=None,
                                   incident_scope="a|inc-b", night_scope="n", per_incident=1, per_night=9,
                                   now="2026-11-15T00:00:02Z")
    assert fresh
    with pytest.raises(BudgetSpent):
        journal.reserve(key="k2", order_id="so-" + "a" * 12, order_sha256="0" * 64, incarnation=1,
                        verb="restart_service", event_id=EVR2, args_digest="x", recipient=None,
                        incident_scope="a|inc-b", night_scope="n", per_incident=1, per_night=9,
                        now="2026-11-15T00:00:03Z")


def test_an_event_that_ages_out_while_the_ack_is_drafted_is_not_sent(world):
    g = signed(world)
    rig = Rig(world)
    rig.event = event_for(rig.raw, received_at=utc_stamp(world.clock.now - timedelta(hours=11, minutes=59)))

    def drafter(event_id):                                   # a slow drafter: the event passes the window meanwhile
        rig.drafts.append(event_id)
        world.clock.now += timedelta(minutes=2)
        return fx.Draft("Thank you for your message. It arrived and has been passed on.", "stand_in")

    assert refused(lambda: rig.effects(drafter=drafter).send_ack(g["id"], EVT, expect_sha256=D(g))) == fx.R_EVENT_AGE
    assert rig.sent == [] and [r.status for r in rig.journal.rows()] == ["refused"]


def test_an_event_that_ages_out_before_the_restart_runs_is_not_run(world):
    g = signed(world)
    rig = Rig(world)
    rig.monitor = monitor_event(received_at=utc_stamp(world.clock.now - timedelta(hours=11, minutes=59)))
    real = fx.Effects.gate
    seen = {"n": 0}

    def gate_then_wait(self, order_id, verb, *, expect_sha256):
        seen["n"] += 1
        if seen["n"] == 2:                                   # the replay after the reservation: time has passed
            world.clock.now += timedelta(minutes=2)
        return real(self, order_id, verb, expect_sha256=expect_sha256)

    fx.Effects.gate = gate_then_wait
    try:
        assert refused(lambda: restart(rig, g)) == fx.R_EVENT_AGE
    finally:
        fx.Effects.gate = real
    assert rig.runs == [] and [r.status for r in rig.journal.rows()] == ["refused"]


def test_a_recorded_result_is_answered_with_nothing_wired_to_act(world):
    g = signed(world)
    rig = Rig(world)
    first = restart(rig, g)
    assert restart(rig, g, runner=None) == first and first.ok
    assert rig.effects().send_ack(g["id"], EVT, expect_sha256=D(g)).ok
    again = rig.effects(drafter=None, transport=None).send_ack(g["id"], EVT, expect_sha256=D(g))
    assert again.ok and again.text == "send_ack: sent"
    assert len(rig.runs) == 1 and len(rig.sent) == 1


def test_a_result_names_the_verb_alone(world):
    g = signed(world)
    rig = Rig(world)
    res = restart(rig, g)
    assert res.verb == "restart_service" and res.text == "restart_service taoserv apache2: exit 0"
    assert restart(rig, g, EVR, "php8.1-fpm", runner=lambda a: 3).verb == "restart_service"


@pytest.mark.parametrize("code", [-9, -15, -2])
def test_a_signal_death_is_unknown_not_failed(world, code):
    g = signed(world)
    rig = Rig(world)
    assert restart(rig, g, runner=lambda a: code).text == f"restart_service taoserv apache2: {fx.R_UNKNOWN}"
    assert [r.status for r in rig.journal.rows()] == ["unknown"]


@pytest.mark.parametrize("column,value", [("order_sha256", "'f'"), ("incarnation", "99"), ("per_night", "99"),
                                          ("per_incident", "99"), ("incident_scope", "'x'")])
def test_the_version_incarnation_and_limits_move_only_on_a_retake(tmp_path, column, value):
    journal = Journal(tmp_path / "ver.db")
    row, _ = _reserve(journal, "k1", "n", "2026-11-15T00:00:00Z")
    conn = sqlite3.connect(journal.path)
    with pytest.raises(sqlite3.DatabaseError):               # unknown -> unknown
        conn.execute(f"UPDATE effect_attempts SET {column} = {value}")
    with pytest.raises(sqlite3.DatabaseError):               # unknown -> ok
        conn.execute(f"UPDATE effect_attempts SET {column} = {value}, status = 'ok'")
    conn.close()
    assert journal.lookup("k1").status == "unknown"


def test_a_journal_file_from_before_the_limits_gets_them(tmp_path):
    """A file whose table predates the per-row limits is given the columns; its old rows count with no limit of
    their own (the caller's applies)."""
    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.execute("CREATE TABLE effect_attempts (attempt_id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "idem_key TEXT NOT NULL UNIQUE, order_id TEXT NOT NULL, order_sha256 TEXT NOT NULL, "
                "incarnation INTEGER NOT NULL, verb TEXT NOT NULL, event_id TEXT NOT NULL, args_digest TEXT NOT NULL, "
                "recipient TEXT, incident_scope TEXT NOT NULL, night_scope TEXT NOT NULL, "
                "status TEXT NOT NULL CHECK (status IN ('unknown', 'ok', 'failed', 'refused')), "
                "reserved_at TEXT NOT NULL, settled_at TEXT)")
    old.execute("INSERT INTO effect_attempts (idem_key, order_id, order_sha256, incarnation, verb, event_id, "
                "args_digest, incident_scope, night_scope, status, reserved_at) "
                "VALUES ('k0', 'so-aaaaaaaaaaaa', 's', 1, 'restart_service', 'e', 'x', 'i0', 'n', 'ok', 't')")
    old.commit()
    old.close()
    journal = Journal(path)
    _reserve(journal, "k1", "n", "2026-11-15T00:00:00Z", per_night=2)
    with pytest.raises(BudgetSpent):
        _reserve(journal, "k2", "n", "2026-11-15T00:00:01Z", per_night=2, event_id=EVR2, incident="apache2|inc-2")

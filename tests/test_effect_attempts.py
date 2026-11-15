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


def test_the_sender_check_refuses_when_the_row_and_the_message_differ_after_the_reservation(world):
    """The reservation's recipient is checked against the message at the send: a journal whose reserve writes another
    address (a fault or a tamper between the two) never sends."""
    g = signed(world)
    rig = Rig(world)
    real = Journal.reserve

    def tampered(self, **kw):
        return real(self, **{**kw, "recipient": "mallory@evil.example"})

    Journal.reserve = tampered
    try:
        assert refused(lambda: rig.effects().send_ack(g["id"], EVT, expect_sha256=D(g))) == fx.R_SENDER
    finally:
        Journal.reserve = real
    assert rig.sent == [] and [r.status for r in rig.journal.rows()] == ["refused"]


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


def test_night_key_cuts_at_the_hour_not_at_midnight():
    from datetime import datetime, timezone
    a = night_key(datetime(2026, 11, 15, 11, 59, tzinfo=timezone.utc), 12)
    b = night_key(datetime(2026, 11, 14, 12, 0, tzinfo=timezone.utc), 12)
    c = night_key(datetime(2026, 11, 15, 12, 0, tzinfo=timezone.utc), 12)
    assert a == b and a != c
    with pytest.raises(ValueError):
        night_key(datetime(2026, 11, 15, 12, 0), 12)

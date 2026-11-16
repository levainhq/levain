"""The night desk's fallback (ND-3c, board levain#13). DONE line, written first: an uncovered effect writes the row and an
open ping. Real signed orders in a real log (the verbs' own ``World``), a real would-have-acted file; the push is a stub."""
from __future__ import annotations

import shutil

import pytest

from levain.desk import effects as fx
from levain.desk import would_have_acted as wha
from levain.desk.effect_attempts import night_key
from levain.desk.would_have_acted import WouldHaveActed

pytest.importorskip("cryptography")
from tests.test_effect_verbs import (ACK_LINE, EVR, EVT, FROM, INC, D, Rig, event_for, monitor_event,  # noqa: E402,F401
                                     raw_mail, signed, world)
from tests.test_standing_order_signing import LINE  # noqa: E402

pytestmark = pytest.mark.skipif(shutil.which("ssh-keygen") is None, reason="needs ssh-keygen")
EVR2 = "evt-" + "d" * 32
APACHE_ONLY = {"verb": "restart_service", "args": {"host": ["taoserv"], "service": ["apache2"]},
               "per_incident": 1, "per_night": 2}


class Pings:
    def __init__(self, answer=((1, 201),)):
        self.sent: list[dict] = []
        self.answer = list(answer)

    def __call__(self, queue_dir, **kw):
        self.sent.append(dict(kw, queue_dir=queue_dir))
        return list(self.answer)


def make_wha(rig: Rig, pings: Pings | None = None, name: str = "wha.db", **kw) -> tuple[WouldHaveActed, Pings]:
    pings = pings if pings is not None else Pings()
    return WouldHaveActed(rig.world.tmp / name, queue_dir=rig.world.tmp, pinger=pings, **kw), pings


def refused(call_):
    with pytest.raises(fx.EffectRefused) as exc:
        call_()
    return str(exc.value)


def restart(rig, g, w, service="apache2", event_id=EVR, **over):
    return rig.effects(would_have_acted=w, **over).restart_service(g["id"], "taoserv", service, event_id=event_id,
                                                                  expect_sha256=D(g))


def night(rig):
    return night_key(rig.world.clock.now, fx.NIGHT_CUT_HOUR_UTC)


# ---- DONE: an uncovered effect writes the row and an open ping ----------------------------------------------------------
def test_an_uncovered_effect_writes_the_row_and_an_open_ping(world):
    g = signed(world, APACHE_ONLY)                           # no line covers php8.3-fpm
    rig = Rig(world)
    w, pings = make_wha(rig, diagnose=lambda verb, event_id: "php8.3-fpm: inactive (dead)")
    assert refused(lambda: restart(rig, g, w, service="php8.3-fpm")) == fx.R_LINE
    assert rig.runs == []                                    # nothing ran: read-only
    (row,) = w.morning_list(night(rig))
    assert (row["verb"], row["args"], row["reason"], row["event_id"], row["order_id"], row["ping_state"]) == (
        "restart_service", "host=taoserv service=php8.3-fpm", "no_line", EVR, g["id"], "open")
    assert row["diagnosis_untrusted"] == "php8.3-fpm: inactive (dead)" and row["ping_pushed"] is True
    (ping,) = pings.sent
    assert ping["title"] == wha.PING_TITLE and ping["kind"] == "notify_urgent" and ping["ref"] == row["notice_ref"]
    assert "had no effect line covering it" in ping["body"]
    assert EVR in ping["body"] and "untrusted" in ping["body"] and "inactive (dead)" in ping["body"]


# ---- each coverage reason writes a row; the refusal itself is unchanged -------------------------------------------------
def test_a_revoked_or_missing_order_writes_a_no_order_row(world):
    g = signed(world)
    rig = Rig(world)
    w, _ = make_wha(rig)
    world.link({"op": "revoke_grant", "grant_id": g["id"]}, today=True)
    assert refused(lambda: restart(rig, g, w)) == fx.R_ORDER
    (row,) = w.morning_list()
    assert row["reason"] == "no_order" and row["order_id"] == g["id"]


def test_a_spent_budget_writes_a_budget_row(world):
    g = signed(world)                                        # per_incident 1, per_night 2
    rig = Rig(world)
    w, _ = make_wha(rig)
    rig.monitor2 = monitor_event(event_id=EVR2)             # the same incident, another event

    def events(event_id):
        return rig.monitor2 if event_id == EVR2 else rig.monitor

    assert restart(rig, g, w, events=events).ok
    assert refused(lambda: restart(rig, g, w, event_id=EVR2, events=events)) == fx.R_BUDGET_INCIDENT
    assert [r["reason"] for r in w.morning_list()] == ["budget_incident"] and len(rig.runs) == 1


def test_a_sender_on_no_line_writes_a_row_but_a_malformed_message_does_not(world):
    g = signed(world)
    rig = Rig(world, raw=raw_mail(frm="stranger@elsewhere.example.org", rp="<stranger@elsewhere.example.org>"))
    w, _ = make_wha(rig)
    ack = lambda: rig.effects(would_have_acted=w).send_ack(g["id"], EVT, expect_sha256=D(g))   # noqa: E731
    assert refused(ack) == fx.R_FROM
    assert [(r["verb"], r["reason"]) for r in w.morning_list()] == [("send_ack", "sender_not_listed")]
    bad = Rig(world, raw=raw_mail(extra="Auto-Submitted: auto-generated\r\n"))      # an auto-reply: noise, not a gap
    w2, p2 = make_wha(bad, name="wha2.db")
    assert refused(lambda: bad.effects(would_have_acted=w2).send_ack(g["id"], EVT, expect_sha256=D(g))) == fx.R_FROM
    assert w2.morning_list() == [] and p2.sent == [] and bad.sent == []


# ---- what is NOT recorded: attacker noise and faults ---------------------------------------------------------------------
def test_argument_shape_and_other_noise_refusals_write_no_row(world):
    g = signed(world)
    rig = Rig(world)
    w, pings = make_wha(rig)
    eff = rig.effects(would_have_acted=w)
    for call in (lambda: eff.restart_service(g["id"], "evil", "apache2", event_id=EVR, expect_sha256=D(g)),
                 lambda: eff.restart_service(g["id"], "taoserv", "mysql; rm", event_id=EVR, expect_sha256=D(g)),
                 lambda: eff.restart_service("so-nothex", "taoserv", "apache2", event_id=EVR, expect_sha256=D(g)),
                 lambda: eff.restart_service(g["id"], "taoserv", "apache2", event_id=EVR, expect_sha256="0" * 64),
                 lambda: rig.effects(would_have_acted=w, events=lambda _e: None).restart_service(
                     g["id"], "taoserv", "apache2", event_id=EVR, expect_sha256=D(g)),
                 lambda: rig.effects(would_have_acted=w, runner=None).restart_service(
                     g["id"], "taoserv", "apache2", event_id=EVR, expect_sha256=D(g))):
        with pytest.raises(fx.EffectRefused):
            call()
    assert w.morning_list() == [] and pings.sent == [] and rig.runs == []


# ---- idempotence, bounds, the ping's faults ------------------------------------------------------------------------------
def test_a_repeat_is_one_row_and_one_ping(world):
    g = signed(world, APACHE_ONLY)
    rig = Rig(world)
    w, pings = make_wha(rig)
    for _ in range(3):
        refused(lambda: restart(rig, g, w, service="php8.3-fpm"))
    (row,) = w.morning_list()
    assert row["times"] == 3 and len(pings.sent) == 1


def test_a_different_reason_for_the_same_event_is_its_own_row(world):
    g = signed(world, APACHE_ONLY)
    rig = Rig(world)
    w, _ = make_wha(rig)
    refused(lambda: restart(rig, g, w, service="php8.3-fpm"))
    world.link({"op": "revoke_grant", "grant_id": g["id"]}, today=True)
    refused(lambda: restart(rig, g, w, service="php8.3-fpm"))
    assert sorted(r["reason"] for r in w.morning_list()) == ["no_line", "no_order"]


def test_a_push_that_reaches_no_phone_leaves_the_ping_open_and_retries_a_bounded_number_of_times(world):
    g = signed(world, APACHE_ONLY)
    rig = Rig(world)
    w, pings = make_wha(rig, pings=Pings(answer=()))
    refused(lambda: restart(rig, g, w, service="php8.3-fpm"))
    refused(lambda: restart(rig, g, w, service="php8.3-fpm"))
    assert len(pings.sent) == 1                              # a retry waits PUSH_RETRY_AFTER after the last push
    for _ in range(5):
        world.clock.now += wha.PUSH_RETRY_AFTER
        rig.monitor = monitor_event(received_at=fx.utc_stamp(world.clock.now))   # the event stays inside its window
        refused(lambda: restart(rig, g, w, service="php8.3-fpm"))
    (row,) = w.morning_list()
    assert row["ping_state"] == "open" and row["ping_pushed"] is False
    assert len(pings.sent) == wha.PUSH_ATTEMPTS_MAX


def test_a_pinger_that_raises_or_a_diagnosis_that_raises_never_changes_the_refusal(world):
    g = signed(world, APACHE_ONLY)
    rig = Rig(world)

    def boom(*a, **k):
        raise RuntimeError("push down")

    def bad_diag(verb, event_id):
        raise RuntimeError("ssh down")

    w, _ = make_wha(rig, pings=boom, diagnose=bad_diag)
    assert refused(lambda: restart(rig, g, w, service="php8.3-fpm")) == fx.R_LINE
    (row,) = w.morning_list()
    assert row["diagnosis_untrusted"] == "" and row["ping_state"] == "open" and row["ping_pushed"] is False


def test_the_diagnosis_is_untrusted_text_stripped_and_bounded(world):
    g = signed(world, APACHE_ONLY)
    rig = Rig(world)
    nasty = "ok‮\x00\x1b[31m" + "x" * 5000 + "​"
    w, pings = make_wha(rig, diagnose=lambda v, e: nasty)
    refused(lambda: restart(rig, g, w, service="php8.3-fpm"))
    (row,) = w.morning_list()
    text = row["diagnosis_untrusted"]
    assert len(text) <= wha.DIAGNOSIS_MAX and text.startswith("ok") and "‮" not in text and "\x00" not in text
    assert "​" not in text and "\x1b" not in text
    assert len(pings.sent[0]["body"]) < 600 and pings.sent[0]["title"] == wha.PING_TITLE   # the title never carries it


def test_the_table_is_bounded_per_night_and_a_row_is_never_deleted(world, monkeypatch):
    g = signed(world, APACHE_ONLY)
    rig = Rig(world)
    w, _ = make_wha(rig)
    monkeypatch.setattr(wha, "ROWS_PER_NIGHT_MAX", 1)
    refused(lambda: restart(rig, g, w, service="php8.3-fpm"))
    refused(lambda: restart(rig, g, w, service="php8.3-fpm", event_id=EVR2,
                            events=lambda e: monitor_event(event_id=EVR2, received_at=rig.monitor.received_at)))
    assert [r["event_id"] for r in w.morning_list()] == [EVR]           # the old row is kept, the new one counted
    assert w.overflow(night(rig)) == 1
    import sqlite3
    with pytest.raises(sqlite3.DatabaseError):
        with sqlite3.connect(w.path) as c:
            c.execute("DELETE FROM would_have_acted")


def test_the_morning_list_is_per_night_and_prints(world):
    g = signed(world, APACHE_ONLY)
    rig = Rig(world)
    w, _ = make_wha(rig, diagnose=lambda v, e: "inactive")
    refused(lambda: restart(rig, g, w, service="php8.3-fpm"))
    assert len(w.morning_list(night(rig))) == 1 and w.morning_list("1999-01-01") == []
    (line,) = w.morning_lines(night(rig))
    assert "would have acted: restart_service host=taoserv service=php8.3-fpm" in line and "untrusted" in line


def test_without_a_record_wired_a_refusal_is_exactly_as_before(world):
    g = signed(world, APACHE_ONLY)
    rig = Rig(world)
    assert refused(lambda: rig.effects().restart_service(g["id"], "taoserv", "php8.3-fpm", event_id=EVR,
                                                         expect_sha256=D(g))) == fx.R_LINE


def test_a_covered_effect_writes_nothing(world):
    g = signed(world)
    rig = Rig(world)
    w, pings = make_wha(rig)
    assert restart(rig, g, w).ok
    assert w.morning_list() == [] and pings.sent == []


# ---- review fixes (L1/L2 on c80e2fb8a) -----------------------------------------------------------------------------------
def real_events(world):
    """An event lookup that answers a fresh, authenticated monitor event for ANY id: every id is a real event."""
    from levain.cockpit.consent import utc_stamp
    return lambda event_id: monitor_event(event_id=event_id, received_at=utc_stamp(world.clock.now))


def fake(i: int) -> str:
    return "evt-" + f"{i:032x}"


def test_a_coverage_refusal_on_an_event_nobody_can_find_writes_no_row_and_no_push(world):
    """(a) restart_service refused for coverage BEFORE its event was looked up: a made-up id is noise, not a gap."""
    g = signed(world, APACHE_ONLY)
    rig = Rig(world)
    w, pings = make_wha(rig)
    for i in range(30):                                       # no line covers php8.3-fpm, and no such event exists
        assert refused(lambda: restart(rig, g, w, service="php8.3-fpm", event_id=fake(i),
                                       events=lambda _e: None)) == fx.R_LINE
    world.link({"op": "revoke_grant", "grant_id": g["id"]}, today=True)
    assert refused(lambda: restart(rig, g, w, event_id=fake(99), events=lambda _e: None)) == fx.R_ORDER
    assert refused(lambda: restart(rig, g, w, event_id=fake(98))) == fx.R_ORDER     # the rig's lookup: a mail, not a monitor event
    ack = rig.effects(would_have_acted=w, events=lambda _e: None)
    assert refused(lambda: ack.send_ack(g["id"], fake(97), expect_sha256=D(g))) == fx.R_ORDER
    assert w.morning_list() == [] and pings.sent == [] and rig.runs == []


def test_a_flood_of_real_events_pushes_at_most_the_nights_ceiling_and_every_row_is_kept(world):
    """(b) a per-night push ceiling: rows past it are written and stay open, but no phone is pushed again tonight."""
    g = signed(world, APACHE_ONLY)
    rig = Rig(world)
    w, pings = make_wha(rig)
    events = real_events(world)
    for i in range(20):
        refused(lambda: restart(rig, g, w, service="php8.3-fpm", event_id=fake(i), events=events))
    rows = w.morning_list(night(rig))
    assert len(rows) == 20 and all(r["ping_state"] == "open" for r in rows)
    assert len(pings.sent) == getattr(wha, "PUSHES_PER_NIGHT_MAX", 5)          # 20 on c80e2fb8a


def test_the_row_bound_is_per_night_so_a_flood_never_hides_the_next_nights_gap(world, monkeypatch):
    """(b) the row bound is per night: past it a new combination is COUNTED (visible), and the next night is clean."""
    from datetime import timedelta
    monkeypatch.setattr(wha, "ROWS_PER_NIGHT_MAX", 2, raising=False)
    g = signed(world, APACHE_ONLY)
    rig = Rig(world)
    w, pings = make_wha(rig)
    events = real_events(world)
    for i in range(5):
        refused(lambda: restart(rig, g, w, service="php8.3-fpm", event_id=fake(i), events=events))
    first = night(rig)
    assert len(w.morning_list(first)) == 2 and w.overflow(first) == 3
    assert any("3 more" in line for line in w.morning_lines(first))
    world.clock.now += timedelta(days=1)
    pushed = len(pings.sent)
    refused(lambda: restart(rig, g, w, service="php8.3-fpm", event_id=fake(50), events=events))
    (row,) = w.morning_list(night(rig))
    assert row["event_id"] == fake(50) and len(pings.sent) == pushed + 1 and w.overflow(night(rig)) == 0


def test_a_second_caller_while_the_first_push_is_in_flight_pushes_nothing(world):
    """(c) the push is claimed in the transaction that records it: a repeat while the first push is still out (the
    pinger re-enters, as a second process would) is counted and pushes no second ping."""
    g = signed(world, APACHE_ONLY)
    rig = Rig(world)
    calls: list[int] = []

    class Slow(Pings):
        def __call__(self, queue_dir, **kw):
            calls.append(1)
            if len(calls) == 1:
                refused(lambda: restart(rig, g, w, service="php8.3-fpm"))
            return super().__call__(queue_dir, **kw)

    w, pings = make_wha(rig, pings=Slow())
    refused(lambda: restart(rig, g, w, service="php8.3-fpm"))
    (row,) = w.morning_list()
    assert row["times"] == 2 and len(calls) == 1


def test_racing_threads_on_one_gap_push_once(world):
    """(c) eight concurrent callers on one (event, verb, reason, args) with a slow push: one row, one push."""
    import threading
    import time
    g = signed(world, APACHE_ONLY)
    rig = Rig(world)
    lock = threading.Lock()
    sent: list[int] = []

    def slow(queue_dir, **kw):
        time.sleep(0.2)
        with lock:
            sent.append(1)
        return [(1, 201)]

    w = WouldHaveActed(world.tmp / "race.db", queue_dir=world.tmp, pinger=slow)
    eff = rig.effects(would_have_acted=w)
    errors: list[BaseException] = []

    def one():
        try:
            eff.restart_service(g["id"], "taoserv", "php8.3-fpm", event_id=EVR, expect_sha256=D(g))
        except fx.EffectRefused:
            pass
        except BaseException as exc:                          # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=one) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert errors == []
    (row,) = w.morning_list()
    assert row["times"] == 8 and len(sent) == 1


def test_the_diagnosis_runs_outside_the_write_transaction(world):
    """(c) a slow diagnosis never holds the file's write lock: another writer can begin while it runs."""
    import sqlite3
    g = signed(world, APACHE_ONLY)
    rig = Rig(world)
    seen: list[str] = []

    def diagnose(verb, event_id):
        conn = sqlite3.connect(world.tmp / "wha.db", timeout=0, isolation_level=None)
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("ROLLBACK")
            seen.append("free")
        except sqlite3.OperationalError:
            seen.append("locked")
        finally:
            conn.close()
        return "inactive"

    w, _ = make_wha(rig, diagnose=diagnose)
    refused(lambda: restart(rig, g, w, service="php8.3-fpm"))
    assert seen == ["free"] and w.morning_list()[0]["diagnosis_untrusted"] == "inactive"


def test_two_uncovered_calls_on_one_event_with_different_args_are_two_rows(world):
    """(d) the dedupe key carries the arguments: apache2 and php8.3-fpm on one event are two gaps, not one."""
    g = signed(world)
    rig = Rig(world)
    w, pings = make_wha(rig)
    world.link({"op": "revoke_grant", "grant_id": g["id"]}, today=True)
    refused(lambda: restart(rig, g, w, service="apache2"))
    refused(lambda: restart(rig, g, w, service="php8.3-fpm"))
    refused(lambda: restart(rig, g, w, service="php8.3-fpm"))
    rows = w.morning_list()
    assert sorted((r["args"], r["times"]) for r in rows) == [("host=taoserv service=apache2", 1),
                                                             ("host=taoserv service=php8.3-fpm", 2)]
    assert len(pings.sent) == 2


def test_a_row_carries_the_ref_ack_last_push_and_next_due_nd6_will_need(world):
    """(e) the ping's state ND-6 will own: a stable notice ref (the push names it), no ack yet, the last push and the
    next due time; a reader lists the open, unacknowledged pings."""
    from levain.cockpit.consent import utc_stamp
    g = signed(world, APACHE_ONLY)
    rig = Rig(world)
    w, pings = make_wha(rig)
    refused(lambda: restart(rig, g, w, service="php8.3-fpm"))
    (row,) = w.morning_list()
    assert isinstance(row["notice_ref"], str) and row["notice_ref"].startswith("wha-")
    assert row["acked_at"] is None and row["last_push_at"] == utc_stamp(world.clock.now)
    assert row["next_due_at"] > row["last_push_at"]
    assert pings.sent[0]["ref"] == row["notice_ref"]
    (open_,) = w.open_pings()
    assert (open_["notice_ref"], open_["acked_at"], open_["next_due_at"]) == (row["notice_ref"], None, row["next_due_at"])


# ---- review fixes (L3 r1 on b0e43bc95) -----------------------------------------------------------------------------------
def test_an_auto_reply_with_no_order_in_force_writes_no_row_and_no_push(world):
    """(A) an ack refused for its order on an auto-reply or a malformed message: the event is not verified (the raw
    message fails the sender derivation), so nothing is written and nothing pushed."""
    g = signed(world)
    world.link({"op": "revoke_grant", "grant_id": g["id"]}, today=True)     # the gate answers "no order"
    for name, raw in (("auto.db", raw_mail(extra="Auto-Submitted: auto-generated\r\n")),
                      ("noret.db", raw_mail(rp=None))):
        rig = Rig(world, raw=raw)
        w, pings = make_wha(rig, name=name)
        assert refused(lambda: rig.effects(would_have_acted=w).send_ack(g["id"], EVT, expect_sha256=D(g))) == fx.R_ORDER
        assert w.morning_list() == [] and pings.sent == [] and rig.sent == []
    good = Rig(world)                                         # the same refusal on a well-formed mail is a gap
    w, _ = make_wha(good, name="good.db")
    assert refused(lambda: good.effects(would_have_acted=w).send_ack(g["id"], EVT, expect_sha256=D(g))) == fx.R_ORDER
    assert [r["reason"] for r in w.morning_list()] == ["no_order"]


def test_a_night_at_its_row_bound_runs_no_diagnosis_for_a_new_call(world, monkeypatch):
    """(B) the row bound is checked before the diagnosis: a full night never runs the (slow) diagnosis again, and the
    overflow line counts calls."""
    monkeypatch.setattr(wha, "ROWS_PER_NIGHT_MAX", 1)
    g = signed(world, APACHE_ONLY)
    rig = Rig(world)
    ran: list[str] = []
    w, _ = make_wha(rig, diagnose=lambda v, e: ran.append(e) or "inactive")
    events = real_events(world)
    refused(lambda: restart(rig, g, w, service="php8.3-fpm", event_id=fake(0), events=events))
    for _ in range(3):
        refused(lambda: restart(rig, g, w, service="php8.3-fpm", event_id=fake(1), events=events))
    refused(lambda: restart(rig, g, w, service="php8.3-fpm", event_id=fake(0), events=events))   # the held row
    assert ran == [fake(0)] and w.overflow(night(rig)) == 3
    assert w.morning_list()[0]["times"] == 2
    assert any("3 more uncovered calls" in line for line in w.morning_lines(night(rig)))


def test_the_same_gap_on_two_nights_is_two_rows_each_in_its_own_morning_list(world):
    """(C) the dedupe key carries the night: a gap that recurs on a later night is that night's row."""
    from datetime import timedelta
    g = signed(world, APACHE_ONLY)
    rig = Rig(world)
    w, pings = make_wha(rig)
    events = real_events(world)
    refused(lambda: restart(rig, g, w, service="php8.3-fpm", events=events))
    first = night(rig)
    world.clock.now += timedelta(days=1)
    refused(lambda: restart(rig, g, w, service="php8.3-fpm", events=events))
    second = night(rig)
    assert first != second
    (a,) = w.morning_list(first)
    (b,) = w.morning_list(second)
    assert (a["event_id"], a["times"], b["event_id"], b["times"]) == (EVR, 1, EVR, 1)
    assert a["notice_ref"] != b["notice_ref"] and len(pings.sent) == 2


def test_a_push_with_no_queue_is_a_no_op_not_an_assert(world):
    """(D) the push's guard is a real check (an ``assert`` vanishes under -O)."""
    rig = Rig(world)
    pings = Pings()
    w = WouldHaveActed(world.tmp / "noq.db", queue_dir=None, pinger=pings)
    w._push(1, "restart_service", "no_line", EVR, "host=taoserv service=apache2", "", "wha-x")
    assert pings.sent == [] and rig.sent == []


def test_a_morning_line_keeps_the_diagnosis_text_as_stored(world):
    """(D) the line is built without a blanket double-space replace: the stored diagnosis is shown as it is, and an ack
    (no enum arguments) prints no doubled space."""
    g = signed(world, APACHE_ONLY)
    rig = Rig(world)
    w, _ = make_wha(rig, diagnose=lambda v, e: "a    b  c")
    refused(lambda: restart(rig, g, w, service="php8.3-fpm"))
    (line,) = w.morning_lines(night(rig))
    assert "'a    b  c'" in line
    ack = Rig(world, raw=raw_mail(frm="stranger@elsewhere.example.org", rp="<stranger@elsewhere.example.org>"))
    w2, _ = make_wha(ack, name="ack.db")
    g2 = signed(world)
    refused(lambda: ack.effects(would_have_acted=w2).send_ack(g2["id"], EVT, expect_sha256=D(g2)))
    (line2,) = w2.morning_lines()
    assert "would have acted: send_ack for " in line2 and "  " not in line2

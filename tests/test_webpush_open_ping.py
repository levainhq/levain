"""The open-ping state table (night desk ND-6a, board levain#37): a ping stays open until a tap or an answer, is re-pushed
on a backoff up to a ceiling, and then writes a 'he did not wake' row. A fake sender and a fake clock; no network."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from levain.webpush import notify
from levain.webpush.open_ping import ACK_SOURCES, BACKOFF, OpenPings

T0 = datetime(2026, 11, 16, 3, 0, tzinfo=timezone.utc)


class Clock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        return self.now

    def advance(self, delta: timedelta) -> None:
        self.now += delta


class Sender:
    """Stands in for levain.webpush.sender.send: records each call, answers like a phone that took it."""

    def __init__(self, status: int = 201) -> None:
        self.calls: list[dict] = []
        self.status = status

    def __call__(self, queue_dir, **kw):
        self.calls.append({"queue_dir": queue_dir, **kw})
        return [(1, self.status)]


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def sender():
    return Sender()


def make(tmp_path, clock, sender, ceiling=4):
    return OpenPings(tmp_path / "open_pings.sqlite3", queue_dir=tmp_path, sender=sender, clock=clock, ceiling=ceiling)


def open_one(pings, ref="notify:watcher", priority="urgent"):
    return pings.open(ref, priority=priority, title="TAOSERV is down", body="No diagnosis yet.")


def test_open_pushes_once_now_and_not_again_before_the_backoff(tmp_path, clock, sender):
    pings = make(tmp_path, clock, sender)
    open_one(pings)
    assert len(sender.calls) == 1
    assert pings.tick() == 0
    clock.advance(BACKOFF[0] - timedelta(seconds=1))
    assert pings.tick() == 0 and len(sender.calls) == 1


def test_no_ack_is_re_pushed_on_a_growing_backoff_with_the_same_ref(tmp_path, clock, sender):
    pings = make(tmp_path, clock, sender, ceiling=5)
    open_one(pings)
    gaps = []
    last = clock.now
    for _ in range(4):
        before = len(sender.calls)
        # walk forward a second at a time to the next push: the gap is the backoff itself
        while len(sender.calls) == before:
            clock.advance(timedelta(seconds=30))
            pings.tick()
        gaps.append(clock.now - last)
        last = clock.now
    assert len(sender.calls) == 5
    assert {c["ref"] for c in sender.calls} == {"notify:watcher"}
    assert all(g >= b for g, b in zip(gaps, BACKOFF))
    assert BACKOFF == tuple(sorted(BACKOFF)) and BACKOFF[-1] > BACKOFF[0]


def test_ceiling_then_a_he_did_not_wake_row_and_no_more_pushes(tmp_path, clock, sender):
    pings = make(tmp_path, clock, sender, ceiling=3)
    open_one(pings)
    for _ in range(200):
        clock.advance(timedelta(minutes=1))
        pings.tick()
    assert len(sender.calls) == 3
    rows = pings.band_rows()
    assert len(rows) == 1
    assert rows[0]["kind"] == "did_not_wake" and rows[0]["ref"] == "notify:watcher" and rows[0]["pushes"] == 3
    assert pings.open_pings() == []
    clock.advance(timedelta(days=1))
    pings.tick()
    assert len(sender.calls) == 3 and len(pings.band_rows()) == 1      # written once, and the ping is over


@pytest.mark.parametrize("source", ["tap", "answer"])
def test_a_tap_or_an_answer_closes_it(tmp_path, clock, sender, source):
    pings = make(tmp_path, clock, sender)
    open_one(pings)
    assert pings.ack("notify:watcher", source) is True
    assert pings.open_pings() == []
    clock.advance(timedelta(hours=3))
    pings.tick()
    assert len(sender.calls) == 1 and pings.band_rows() == []
    assert pings.ping("notify:watcher")["state"] == "acked"
    assert pings.ping("notify:watcher")["ack_source"] == source


@pytest.mark.parametrize("source", ["shown", "received"])
def test_a_shown_receipt_does_not_close_it(tmp_path, clock, sender, source):
    pings = make(tmp_path, clock, sender)
    open_one(pings)
    assert pings.ack("notify:watcher", source) is False
    assert [p["ref"] for p in pings.open_pings()] == ["notify:watcher"]
    clock.advance(BACKOFF[0])
    assert pings.tick() == 1 and len(sender.calls) == 2


def test_only_tap_and_answer_are_ack_sources_and_a_made_up_one_is_refused(tmp_path, clock, sender):
    assert ACK_SOURCES == ("tap", "answer")
    pings = make(tmp_path, clock, sender)
    open_one(pings)
    with pytest.raises(ValueError):
        pings.ack("notify:watcher", "carrier-pigeon")
    assert len(pings.open_pings()) == 1


def test_an_ack_of_an_unknown_or_closed_ping_changes_nothing(tmp_path, clock, sender):
    pings = make(tmp_path, clock, sender)
    assert pings.ack("notify:nobody", "tap") is False
    open_one(pings)
    assert pings.ack("notify:watcher", "tap") is True
    assert pings.ack("notify:watcher", "answer") is False
    assert pings.ping("notify:watcher")["ack_source"] == "tap"


def test_a_restart_resumes_the_open_pings_from_the_table(tmp_path, clock, sender):
    first = make(tmp_path, clock, sender, ceiling=3)
    open_one(first, ref="notify:a")
    clock.advance(BACKOFF[0])
    first.tick()
    assert len(sender.calls) == 3 - 1                                   # a: first push and one re-push
    del first
    second = make(tmp_path, clock, sender, ceiling=3)                    # a new process: nothing held in memory
    assert [p["ref"] for p in second.open_pings()] == ["notify:a"]
    assert second.ping("notify:a")["pushes"] == 2
    assert second.tick() == 0                                           # its next push is not yet due
    clock.advance(BACKOFF[1])
    assert second.tick() == 1 and len(sender.calls) == 3
    clock.advance(timedelta(days=1))
    second.tick()
    assert len(second.band_rows()) == 1


def test_a_restart_does_not_reopen_an_acked_ping_or_lose_a_written_row(tmp_path, clock, sender):
    first = make(tmp_path, clock, sender, ceiling=1)
    open_one(first, ref="notify:a")
    open_one(first, ref="notify:b")
    first.ack("notify:b", "tap")
    clock.advance(timedelta(days=1))
    first.tick()
    second = make(tmp_path, clock, sender, ceiling=1)
    assert second.open_pings() == []
    assert [r["ref"] for r in second.band_rows()] == ["notify:a"]


def test_a_failed_push_still_counts_toward_the_ceiling(tmp_path, clock):
    dead = Sender(status=0)
    pings = make(tmp_path, clock, dead, ceiling=2)
    open_one(pings)
    for _ in range(100):
        clock.advance(timedelta(minutes=1))
        pings.tick()
    assert len(dead.calls) == 2 and len(pings.band_rows()) == 1


def test_a_sender_that_raises_leaves_the_ping_open_and_counted(tmp_path, clock):
    def boom(queue_dir, **kw):
        raise RuntimeError("push service on fire")

    pings = make(tmp_path, clock, boom, ceiling=3)
    open_one(pings)
    assert pings.ping("notify:watcher")["pushes"] == 1 and pings.ping("notify:watcher")["state"] == "open"


def test_opening_an_open_ping_again_is_one_ping_not_a_second_push(tmp_path, clock, sender):
    pings = make(tmp_path, clock, sender)
    a = open_one(pings)
    b = open_one(pings)
    assert a == b and len(sender.calls) == 1 and len(pings.open_pings()) == 1


def test_priorities_stay_low_normal_urgent(tmp_path, clock, sender):
    pings = make(tmp_path, clock, sender)
    for priority in notify.PRIORITIES:
        open_one(pings, ref=f"notify:{priority}", priority=priority)
    assert [c["kind"] for c in sender.calls] == [notify.kind_for(p) for p in notify.PRIORITIES]
    assert notify.PRIORITIES == ("low", "normal", "urgent")
    with pytest.raises(ValueError):
        open_one(pings, ref="notify:high", priority="high")
    assert pings.ping("notify:high") is None


def test_the_title_and_body_go_through_the_notice_sanitiser(tmp_path, clock, sender):
    pings = make(tmp_path, clock, sender)
    with pytest.raises(notify.NotifyRefused):
        pings.open("notify:x", priority="urgent", title="[levain] fake seat", body="b")
    with pytest.raises(notify.NotifyRefused):
        pings.open("notify:x", priority="urgent", title="t", body="")
    assert sender.calls == [] and pings.ping("notify:x") is None


def test_the_ping_carries_no_action_and_the_fixed_title_and_body_are_what_was_given(tmp_path, clock, sender):
    pings = make(tmp_path, clock, sender)
    open_one(pings)
    call = sender.calls[0]
    assert call["title"] == "TAOSERV is down" and call["body"] == "No diagnosis yet."
    assert call["url"] == notify.NOTICES_URL and "extra" not in call and "data" not in call


def test_the_default_sender_is_the_push_sender(tmp_path, clock):
    from levain.webpush import sender as real

    pings = OpenPings(tmp_path / "p.sqlite3", queue_dir=tmp_path, clock=clock)
    assert pings.sender is real.send


# --- fix round 1 (F1-F6) -------------------------------------------------------------------------------------------
def _five_due(tmp_path, clock, sender, ceiling=6):
    pings = make(tmp_path, clock, sender, ceiling=ceiling)
    for n in range(5):
        open_one(pings, ref=f"notify:r{n}")
    sender.calls.clear()
    clock.advance(BACKOFF[0])
    return pings


def test_f1_a_tick_pass_is_bounded_by_a_row_count(tmp_path, clock, sender, monkeypatch):
    from levain.webpush import open_ping

    pings = _five_due(tmp_path, clock, sender)
    monkeypatch.setattr(open_ping, "MAX_PER_PASS", 2)
    assert pings.tick() == 2
    assert sorted(p["pushes"] for p in pings.open_pings()) == [1, 1, 1, 2, 2]     # the rest were not claimed
    assert pings.tick() == 2 and pings.tick() == 1 and pings.tick() == 0


def test_f1_a_tick_pass_stops_at_its_wall_budget(tmp_path, clock, sender, monkeypatch):
    from levain.webpush import open_ping

    pings = _five_due(tmp_path, clock, sender)
    monkeypatch.setattr(open_ping, "PASS_WALL", 0.0)
    assert pings.tick() == 1
    assert sorted(p["pushes"] for p in pings.open_pings()) == [1, 1, 1, 1, 2]


def test_f1_a_crash_mid_pass_loses_at_most_the_one_in_flight_attempt(tmp_path, clock):
    class Crash(BaseException):
        pass

    def die(queue_dir, **kw):
        raise Crash()

    quiet = Sender()
    pings = make(tmp_path, clock, quiet, ceiling=6)
    for n in range(3):
        open_one(pings, ref=f"notify:r{n}")
    clock.advance(BACKOFF[0])
    pings.sender = die
    with pytest.raises(Crash):
        pings.tick()
    assert sorted(p["pushes"] for p in pings.open_pings()) == [1, 1, 2]           # one in flight, two untouched


def test_f2_a_ping_acked_after_the_pass_began_is_not_pushed(tmp_path, clock):
    seen: list[str] = []

    def sender(queue_dir, **kw):
        seen.append(kw["ref"])
        if kw["ref"] == "notify:r0":
            pings.ack("notify:r1", "tap")                      # he taps r1 while r0's push is out
        return [(1, 201)]

    pings = make(tmp_path, clock, sender, ceiling=6)
    open_one(pings, ref="notify:r0")
    open_one(pings, ref="notify:r1")
    seen.clear()
    clock.advance(BACKOFF[0])
    assert pings.tick() == 1 and seen == ["notify:r0"]
    assert pings.ping("notify:r1")["state"] == "acked"


def test_f2_a_ping_closed_between_claim_and_send_is_skipped(tmp_path, clock):
    calls: list[str] = []
    pings = make(tmp_path, clock, lambda q, **kw: calls.append(kw["ref"]) or [(1, 201)], ceiling=6)
    open_one(pings)
    clock.advance(BACKOFF[0])
    real = pings._still_open

    def closing(ping_id):
        pings.ack("notify:watcher", "tap")                     # the tap lands in the claim-to-send window
        return real(ping_id)

    pings._still_open = closing
    assert pings.tick() == 0 and calls == ["notify:watcher"]    # only the first push, at open


def test_f3_a_ping_no_phone_took_is_unreachable_not_he_did_not_wake(tmp_path, clock):
    dead = Sender(status=0)
    pings = make(tmp_path, clock, dead, ceiling=2)
    open_one(pings)
    for _ in range(100):
        clock.advance(timedelta(minutes=1))
        pings.tick()
    row = pings.band_rows()[0]
    assert row["kind"] == "unreachable" and row["delivered"] == 0 and row["pushes"] == 2
    assert pings.ping("notify:watcher")["state"] == "did_not_wake"     # the ping row's own state; the band row's kind differs
    assert "no push was delivered" in row["text"] and "did not wake" not in row["text"]


def test_f3_one_delivered_push_makes_it_he_did_not_wake(tmp_path, clock):
    statuses = iter([201, 0, 0])

    def sender(queue_dir, **kw):
        return [(1, next(statuses))]

    pings = make(tmp_path, clock, sender, ceiling=3)
    open_one(pings)
    for _ in range(100):
        clock.advance(timedelta(minutes=1))
        pings.tick()
    row = pings.band_rows()[0]
    assert row["kind"] == "did_not_wake" and row["delivered"] == 1 and "did not wake" in row["text"]


@pytest.mark.parametrize("answer", [None, "ok", [], [(1, -1)], [(1, 404)], [(1, 199), (2, 300)]])
def test_f3_only_a_2xx_counts_as_delivered(tmp_path, clock, answer):
    pings = make(tmp_path, clock, lambda q, **kw: answer, ceiling=1)
    open_one(pings)
    assert pings.ping("notify:watcher")["delivered"] == 0
    clock.advance(timedelta(days=1))
    pings.tick()
    assert pings.band_rows()[0]["kind"] == "unreachable"


def test_f4_reopening_an_open_ref_raises_priority_and_takes_the_new_body(tmp_path, clock, sender):
    pings = make(tmp_path, clock, sender)
    a = pings.open("notify:x", priority="low", title="Old title", body="old body")
    b = pings.open("notify:x", priority="urgent", title="New title", body="new body")
    assert a == b and len(pings.open_pings()) == 1
    p = pings.ping("notify:x")
    assert p["priority"] == "urgent" and p["title"] == "New title" and p["body"] == "new body"
    assert len(sender.calls) == 2 and sender.calls[1]["kind"] == notify.kind_for("urgent")
    assert sender.calls[1]["title"] == "New title"


def test_f4_reopening_never_lowers_the_priority(tmp_path, clock, sender):
    pings = make(tmp_path, clock, sender)
    pings.open("notify:x", priority="urgent", title="T one", body="b one")
    pings.open("notify:x", priority="low", title="T two", body="b two")
    p = pings.ping("notify:x")
    assert p["priority"] == "urgent" and p["title"] == "T two" and p["body"] == "b two"


def test_f4_a_refused_reopen_changes_nothing(tmp_path, clock, sender):
    pings = make(tmp_path, clock, sender)
    pings.open("notify:x", priority="low", title="T one", body="b one")
    with pytest.raises(ValueError):
        pings.open("notify:x", priority="high", title="T two", body="b two")
    assert pings.ping("notify:x")["title"] == "T one"


@pytest.mark.parametrize("ref", ["", None, 7, b"notify:x"])
def test_f5_a_ref_is_a_non_empty_string(tmp_path, clock, sender, ref):
    pings = make(tmp_path, clock, sender)
    with pytest.raises(ValueError):
        pings.open(ref, priority="urgent", title="t", body="b")
    assert pings.open_pings() == [] and sender.calls == []


def test_f6_a_raising_sender_leaves_its_error_on_the_row(tmp_path, clock):
    def boom(queue_dir, **kw):
        raise TypeError("send() got an unexpected keyword argument 'url'")

    pings = make(tmp_path, clock, boom, ceiling=3)
    open_one(pings)
    p = pings.ping("notify:watcher")
    assert p["pushes"] == 1 and p["last_error"].startswith("TypeError") and "unexpected keyword" in p["last_error"]


def test_f6_the_error_is_short_and_a_good_push_clears_it(tmp_path, clock):
    state = {"n": 0}

    def flaky(queue_dir, **kw):
        state["n"] += 1
        if state["n"] == 1:
            raise RuntimeError("x" * 5000)
        return [(1, 201)]

    pings = make(tmp_path, clock, flaky, ceiling=3)
    open_one(pings)
    assert 0 < len(pings.ping("notify:watcher")["last_error"]) <= 200
    clock.advance(BACKOFF[0])
    pings.tick()
    assert pings.ping("notify:watcher")["last_error"] is None


def test_two_handles_on_one_file_opening_one_ref_get_one_id(tmp_path, clock, sender):
    a = make(tmp_path, clock, sender)
    b = make(tmp_path, clock, sender)
    assert open_one(a) == open_one(b) and len(a.open_pings()) == 1 and len(sender.calls) == 1

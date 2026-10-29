"""Coordinator C4 backend: the review queue, the push records, "waiting on you" and the feeds (C0 §3.3, §3.6).

Real stores throughout: the coordinator store, a real consent queue (real ssh-keygen keys, a real signed authority log),
a real question band, a real limits store with a halt, a real ledger. Synthetic words. The envelope fixtures are the
C2 tests' own (a signed structured envelope bound to a real thread)."""
from __future__ import annotations

import sqlite3
import time
from datetime import datetime, timedelta, timezone

import pytest

from levain.consent import ConsentQueue
from levain.consent import limits
from levain.consent.ledger import Ledger
from levain.consent.questions import QuestionStore
from levain.coordinator import review
from levain.coordinator.store import CoordError, CoordStore, Part
from tests.test_consent_queue import authority, keys  # noqa: F401  (module fixtures)
from tests.test_coordinator_envelope import ctx, keys as env_keys, repo, snapshot, structured_thread  # noqa: F401
from tests.test_coordinator_envelope import HOUR, NOW as ENV_NOW
from tests._h4 import h4_home

T0 = datetime(2026, 10, 22, 20, 0, tzinfo=timezone.utc)


class Clock:
    def __init__(self, now=T0):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, **kw):
        self.now += timedelta(**kw)


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def qdir(tmp_path):
    d = tmp_path / "consent"
    d.mkdir(mode=0o700)
    return d


@pytest.fixture
def store(qdir, clock):
    return CoordStore(qdir, clock=clock)


def _token(store):
    held = store.coord_status()["lease"]
    if held is not None and held["live"] and held["holder"] == "operator":
        return held["token"]
    return store.coord_take()


def _raw(store, sql, *args):
    c = sqlite3.connect(store.path)
    try:
        c.execute(sql, args)
        c.commit()
    finally:
        c.close()


def _thread(store, words="fix the build"):
    d, _ = store.accept_drop(words, surface="cli")
    (t,) = store.split(d.drop_id, [Part("work", "fix")])
    return d, t


# --- the queue and its modes ----------------------------------------------------------------------------------
def test_a_key_files_one_item_however_often_the_feed_runs(store):
    a, new_a = review.file_item(store, "doubt", {"text": "x"}, dedupe_key="doubt:1", by="coordinator", token=_token(store))
    b, new_b = review.file_item(store, "doubt", {"text": "x"}, dedupe_key="doubt:1", by="coordinator", token=_token(store))
    assert new_a and not new_b and a.item_id == b.item_id
    assert len(review.list_items(store)) == 1


def test_a_cleared_item_is_not_refiled_by_its_own_condition(store):
    item, _ = review.file_item(store, "seat", {"text": "x"}, dedupe_key="seat:1")
    review.clear_item(store, item.item_id)
    assert review.file_item(store, "seat", {"text": "x"}, dedupe_key="seat:1")[1] is False
    assert review.list_items(store) == []
    assert [i.state for i in review.list_items(store, include_cleared=True)] == ["cleared"]
    assert review.reopen_item(store, item.item_id).state == "open"
    assert len(review.list_items(store)) == 1


def test_an_item_alternates_and_clearing_twice_is_refused(store):
    item, _ = review.file_item(store, "seat", {"text": "x"}, dedupe_key="s")
    review.clear_item(store, item.item_id)
    with pytest.raises(CoordError, match="already cleared"):
        review.clear_item(store, item.item_id)
    review.reopen_item(store, item.item_id)
    with pytest.raises(CoordError, match="already open"):
        review.reopen_item(store, item.item_id)


def test_the_coordinator_never_clears_an_authority_relevant_item_in_its_own_mode(store):
    tok = _token(store)
    rel, _ = review.file_item(store, "envelope", {"text": "x"}, dedupe_key="e", authority_relevant=True)
    with pytest.raises(CoordError, match="authority-relevant"):
        review.clear_item(store, rel.item_id, mode="coordinator", token=tok)
    assert review.get_item(store, rel.item_id).state == "open"
    # you, or a fresh session you asked, can
    assert review.clear_item(store, rel.item_id, mode="fresh").state == "cleared"
    assert review.get_item(store, rel.item_id).last[:3] == ("cleared", "fresh", "operator")
    plain, _ = review.file_item(store, "doubt", {"text": "x"}, dedupe_key="d", by="coordinator", token=tok)
    review.clear_item(store, plain.item_id, mode="coordinator", token=tok)
    assert review.get_item(store, plain.item_id).last[:3] == ("cleared", "coordinator", "coordinator")


def test_coordinator_mode_needs_the_lease_token(store):
    item, _ = review.file_item(store, "doubt", {"text": "x"}, dedupe_key="d", by="coordinator", token=_token(store))
    with pytest.raises(CoordError):
        review.clear_item(store, item.item_id, mode="coordinator")          # no token
    with pytest.raises(CoordError):
        review.clear_item(store, item.item_id, mode="coordinator", token=999)


@pytest.mark.parametrize("mode,by", [("coordinator", "operator"), ("manual", "coordinator"), ("fresh", "coordinator")])
def test_the_database_refuses_a_mode_that_names_the_wrong_reviewer(store, mode, by):
    item, _ = review.file_item(store, "doubt", {"text": "x"}, dedupe_key="d")
    with pytest.raises(sqlite3.IntegrityError, match="coordinator mode"):
        _raw(store, "INSERT INTO review_events (item_id, seq, op, mode, by, at) VALUES (?, 1, 'cleared', ?, ?, 'x')",
             item.item_id, mode, by)


def test_the_database_refuses_the_coordinator_clearing_an_authority_item_by_raw_sql(store):
    rel, _ = review.file_item(store, "envelope", {"text": "x"}, dedupe_key="e", authority_relevant=True)
    with pytest.raises(sqlite3.IntegrityError, match="not cleared in coordinator mode"):
        _raw(store, "INSERT INTO review_events (item_id, seq, op, mode, by, at) "
                    "VALUES (?, 1, 'cleared', 'coordinator', 'coordinator', 'x')", rel.item_id)


def test_review_rows_are_append_only(store):
    item, _ = review.file_item(store, "doubt", {"text": "x"}, dedupe_key="d")
    for sql in ("UPDATE review_items SET body = 'x'", "DELETE FROM review_items"):
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            _raw(store, sql)


# --- KNOW: a reply and its item, never a push -----------------------------------------------------------------
def test_a_know_is_a_reply_and_an_open_item_in_one_write(store):
    _d, t = _thread(store)
    item, reply_id = review.file_know(store, t.thread_id, "the fix touches two files", token=_token(store))
    assert item.feed == "know" and item.by == "coordinator" and item.thread_id == t.thread_id
    assert item.body["reply_id"] == reply_id
    rows = sqlite3.connect(store.path).execute("SELECT kind, thread_id FROM replies WHERE reply_id = ?",
                                               (reply_id,)).fetchall()
    assert rows == [("know", t.thread_id)]
    # a KNOW from nobody holding the lease writes nothing at all
    n = sqlite3.connect(store.path).execute("SELECT COUNT(*) FROM review_items").fetchone()[0]
    with pytest.raises(CoordError):
        review.file_know(store, t.thread_id, "forged", token=12345)
    assert sqlite3.connect(store.path).execute("SELECT COUNT(*) FROM review_items").fetchone()[0] == n
    assert sqlite3.connect(store.path).execute("SELECT COUNT(*) FROM replies WHERE kind = 'know'").fetchone()[0] == 1


# --- SIGN / ANSWER records and "waiting on you" ---------------------------------------------------------------
@pytest.fixture
def cq(qdir, authority, clock):  # noqa: F811
    return ConsentQueue(qdir, authority=authority, clock=clock, home=h4_home())


def _request(cq, command="touch /tmp/x"):
    return cq.file(agent="claude_code", agent_session_id="sess-1", tool_name="Bash", tool_input={"command": command},
                   cwd="/work/proj", hold=timedelta(minutes=5), tier="T2")


def test_waiting_on_you_is_read_from_the_queue_and_the_band_not_stored(store, qdir, cq, clock):
    _d, t = _thread(store)
    req = _request(cq)
    qs = QuestionStore(qdir)
    q = qs.file(agent="claude_code", agent_session_id="sess-1", what="which branch?",
                options=[("a", "main"), ("b", "other")], rec="a", blocks="the land", cwd="/work/proj")
    review.push_sign(store, t.thread_id, req.id, "land the fix onto main", onto="main", rec=review.NO_REC)
    review.push_answer(store, t.thread_id, q.id, "which branch?", rec=review.NO_REC)
    sign_open, answer_open = review.queue_request_open(qdir, clock=clock), review.question_open(qdir)
    w = review.waiting_on_you(store, sign_open=sign_open, answer_open=answer_open)
    assert sorted(r["kind"] for r in w["threads"][t.thread_id]) == ["answer", "sign"] and not w["unresolved"]
    # the queue closes the request (withdrawn) and the band gets its answer: the same records read as not waiting
    assert cq.withdraw(req.id)
    qs.answer(q.id, option="a", channel="cli")
    w = review.waiting_on_you(store, sign_open=sign_open, answer_open=answer_open)
    assert w == {"threads": {}, "loose": [], "unresolved": []}


def test_a_record_whose_state_cannot_be_read_is_unresolved_never_closed(store, qdir, clock):
    _d, t = _thread(store)
    review.push_sign(store, t.thread_id, "no-such-request", "land it", rec=review.NO_REC)
    w = review.waiting_on_you(store, sign_open=review.queue_request_open(qdir, clock=clock),
                              answer_open=review.question_open(qdir))
    assert [r["ref_id"] for r in w["unresolved"]] == ["no-such-request"] and w["threads"] == {}


def test_the_same_request_is_recorded_once(store, qdir, cq):
    _d, t = _thread(store)
    req = _request(cq)
    first = review.push_sign(store, t.thread_id, req.id, "land it", rec=review.NO_REC)
    assert review.push_sign(store, t.thread_id, req.id, "land it", rec=review.NO_REC) == (first[0], False)
    assert sqlite3.connect(store.path).execute("SELECT COUNT(*) FROM replies WHERE kind = 'sign'").fetchone()[0] == 1


def test_one_open_land_sign_per_thread_and_onto(store, qdir, cq, clock):
    _d, t = _thread(store)
    old, new = _request(cq, "git push 1"), _request(cq, "git push 2")
    sign_open = review.queue_request_open(qdir, clock=clock)
    review.push_sign(store, t.thread_id, old.id, "land", onto="main", still_open=sign_open, rec=review.NO_REC)
    with pytest.raises(CoordError, match="already has an open land SIGN"):
        review.push_sign(store, t.thread_id, new.id, "land again", onto="main", still_open=sign_open, rec=review.NO_REC)
    review.push_sign(store, t.thread_id, new.id, "another branch", onto="other", still_open=sign_open, rec=review.NO_REC)   # other onto
    assert cq.withdraw(old.id)                       # MOVED BASE: the re-file withdraws the older one first
    assert review.push_sign(store, t.thread_id, _request(cq, "git push 3").id, "land", onto="main",
                            still_open=sign_open, rec=review.NO_REC)[1] is True


# --- the feeds -----------------------------------------------------------------------------------------------
def test_a_drop_with_no_split_after_ten_minutes_is_one_item_and_a_split_drop_is_none(store, clock):
    stuck, _ = store.accept_drop("note this and that", surface="cli")
    fine, _ = store.accept_drop("fix the other thing", surface="cli")
    store.split(fine.drop_id, [Part("work", "fix")])
    clock.advance(minutes=9)
    assert review.feed_no_split(store) == []
    clock.advance(minutes=2)
    (item,) = review.feed_no_split(store)
    assert item.feed == "receipt" and item.body["drop_id"] == stuck.drop_id
    assert review.feed_no_split(store)[0].item_id == item.item_id and len(review.list_items(store)) == 1


def test_a_dead_seat_is_an_item_and_a_launch_nobody_started_is_not(store, clock):
    store.coord_set_agent("claude", cwd=str(store.directory.parent))
    store.accept_drop("fix the build", surface="cli")
    ln = store.pending_coordinator()
    assert store.launch_claim_spawn(ln.launch_id, 1)[1] is None
    clock.advance(hours=1)
    items = review.feed_dead_seats(store)                  # the feed runs the lazy expiry the read path runs
    assert [(i.feed, i.body["launch_id"]) for i in items] == [("seat", ln.launch_id)]
    assert items[0].body["state"] in ("expired", "failed") and not items[0].authority_relevant
    assert review.feed_dead_seats(store) == []             # filed once: a second run files and returns nothing


@pytest.mark.parametrize("act", ["coord_take", "coord_release"])
def test_a_launch_you_voided_by_taking_or_releasing_the_role_is_no_dead_seat(store, act):
    store.coord_set_agent("claude", cwd=str(store.directory.parent))
    store.accept_drop("fix the build", surface="cli")
    ln = store.pending_coordinator()
    assert store.launch_claim_spawn(ln.launch_id, 1)[1] is None
    store.coord_take()
    if act == "coord_release":
        store.coord_release()
    assert store.launch(ln.launch_id).state == "failed"
    assert review.feed_dead_seats(store) == []


def test_an_unspawned_launch_that_expires_is_no_dead_seat(store, clock):
    store.coord_set_agent("claude", cwd=str(store.directory.parent))
    store.accept_drop("fix the build", surface="cli")
    clock.advance(hours=1)
    store.coord_status()
    assert review.feed_dead_seats(store) == []


def test_an_envelope_near_its_stop_is_an_authority_item_and_a_killed_one_is_none(ctx, structured_thread):  # noqa: F811
    _d, b, t = structured_thread
    assert review.feed_envelopes(ctx.store, near=timedelta(minutes=30)) == []        # stops in two hours
    (item,) = review.feed_envelopes(ctx.store, near=3 * HOUR)
    assert item.feed == "envelope" and item.authority_relevant and item.thread_id == t.thread_id
    assert item.body["envelope_id"] == b["envelope_id"]
    # an earlier stop is a new fact: it files its own item under its own key
    ctx.store.attenuate(b["envelope_id"], "stop_earlier", review.utc_stamp(ENV_NOW + timedelta(minutes=20)))
    again = review.feed_envelopes(ctx.store, near=timedelta(minutes=30))
    assert len(again) == 1 and again[0].item_id != item.item_id
    ctx.store.attenuate(b["envelope_id"], "kill")
    assert review.feed_envelopes(ctx.store, near=10 * HOUR) == []


def test_an_end_in_the_limits_store_is_an_item_and_an_undecided_halt_is_not(store, qdir):
    limits._halt_on(qdir, "claude_code", "sess-end", "tool_calls", "req-end", None, time.time())
    limits._halt_on(qdir, "claude_code", "sess-gap", "tokens", "req-gap", 7, time.time())
    (item,) = review.feed_end(store, qdir)
    assert item.feed == "end" and item.body["root"] == "sess-end" and item.body["kind"] == "tool_calls"
    assert review.feed_end(store, qdir)[0].item_id == item.item_id
    assert review.feed_end(store, qdir / "nowhere") == []


def test_the_unseen_digest_is_one_item_counted_by_agent_and_stamp(store, qdir):
    assert review.feed_digest(store, qdir) is None                                    # no ledger yet
    led = Ledger(qdir)
    for stamp, n in (("grant", 2), ("ruling", 1), ("answer", 3)):
        for i in range(n):
            led.record_call(agent="claude_code", agent_session_id=f"s-{stamp}", tool_name="Bash", tool_input={"i": i},
                            cwd="/w", phase="pre", stamp=stamp, request_id="rq" if stamp != "grant" else None)
    now = datetime.now(timezone.utc)
    item = review.feed_digest(store, qdir, until=now + timedelta(minutes=1))
    assert item.feed == "digest" and item.body["calls"] == 3
    assert item.body["counts"] == {"claude_code/grant": 2, "claude_code/ruling": 1}
    # the next window starts where this one ended: the same calls are never counted twice
    assert review.feed_digest(store, qdir, until=now + timedelta(minutes=2)) is None


def test_collect_feeds_reports_a_failing_feed_by_name_and_runs_the_rest(store, qdir, monkeypatch):
    store.accept_drop("note this", surface="cli")

    def boom(*a, **k):
        raise RuntimeError("limits unreadable")

    monkeypatch.setattr(review, "feed_end", boom)
    out = review.collect_feeds(store, queue_dir=qdir)
    assert out["end"].startswith("error: RuntimeError") and out["receipt"] == [] and out["digest"] == []


# --- L3 r1 fixes (c4fix lane) ----------------------------------------------------------------------------------
def _calls(led, n, at, stamp="grant"):
    for i in range(n):
        led.record_call(agent="claude_code", agent_session_id=f"s-{at}-{i}", tool_name="Bash", tool_input={"i": i, "at": at},
                        cwd="/w", phase="pre", stamp=stamp, received_at=at)


def test_f1_digest_windows_do_not_overlap_and_a_day_later_files_only_the_new_calls(store, qdir):
    led = Ledger(qdir)
    t1 = datetime(2026, 10, 22, 12, 0, tzinfo=timezone.utc)
    _calls(led, 2, t1 - timedelta(hours=1))
    one = review.feed_digest(store, qdir, until=t1)
    assert one.body["calls"] == 2
    day = t1 + timedelta(days=1)
    assert review.feed_digest(store, qdir, until=day) is None            # nothing new: no daily re-filing
    _calls(led, 1, day - timedelta(hours=1))
    two = review.feed_digest(store, qdir, until=day)
    assert two.item_id != one.item_id and two.body["calls"] == 1 and two.body["since"] == one.body["until"]
    assert len(review.list_items(store, feed="digest")) == 2


def test_f2_a_corrupt_limits_store_is_an_error_not_no_ends(store, qdir):
    (qdir / "limits.db").write_bytes(b"this is not a database" * 50)
    with pytest.raises(Exception):
        review.feed_end(store, qdir)
    out = review.collect_feeds(store, queue_dir=qdir)
    assert out["end"].startswith("error: ")


def _lapse(c, hours):
    c.store._clock = lambda: ENV_NOW + timedelta(hours=hours)


def test_f3_a_lapsed_envelope_with_no_item_is_filed_and_a_bad_stamp_skips_only_that_row(ctx, structured_thread):  # noqa: F811
    _d, b, t = structured_thread
    _lapse(ctx, 3)                                           # its stop (two hours out) has passed
    (item,) = review.feed_envelopes(ctx.store, near=timedelta(minutes=30))
    assert item.body["envelope_id"] == b["envelope_id"] and item.authority_relevant
    assert [i.item_id for i in review.feed_envelopes(ctx.store, near=timedelta(minutes=30))] == [item.item_id]
    assert len(review.list_items(ctx.store, feed="envelope")) == 1
    _lapse(ctx, 0)
    errors: list[str] = []
    c = sqlite3.connect(ctx.store.path)
    c.execute("INSERT INTO envelope_attenuations (envelope_id, thread_id, kind, value, by, gseq, at) "
              "VALUES (?, NULL, 'stop_earlier', 'not-a-stamp', 'operator', 99999999, 'x')", (b["envelope_id"],))
    c.commit()
    c.close()
    got = review.feed_envelopes(ctx.store, near=3 * HOUR, errors=errors)
    assert [i.item_id for i in got] and errors and "stop_earlier" in errors[0]


def test_f4_a_land_sign_is_refused_unless_the_state_reader_says_closed(store, qdir, cq, clock):
    _d, t = _thread(store)
    old, new, third = _request(cq, "git push 1"), _request(cq, "git push 2"), _request(cq, "git push 3")
    review.push_sign(store, t.thread_id, old.id, "land", onto="main", still_open=review.queue_request_open(qdir, clock=clock), rec=review.NO_REC)
    with pytest.raises(CoordError):                                     # no reader at all
        review.push_sign(store, t.thread_id, new.id, "land again", onto="main", rec=review.NO_REC)
    with pytest.raises(CoordError):                                     # a reader that cannot tell
        review.push_sign(store, t.thread_id, new.id, "land again", onto="main", still_open=lambda _r: None, rec=review.NO_REC)
    assert cq.withdraw(old.id)
    assert review.push_sign(store, t.thread_id, third.id, "land", onto="main",
                            still_open=review.queue_request_open(qdir, clock=clock), rec=review.NO_REC)[1] is True


def test_f4_the_reader_runs_outside_the_dispatch_write_transaction(store, qdir, cq, clock):
    _d, t = _thread(store)
    old, new = _request(cq, "git push 1"), _request(cq, "git push 2")
    review.push_sign(store, t.thread_id, old.id, "land", onto="main", still_open=review.queue_request_open(qdir, clock=clock), rec=review.NO_REC)
    cq.withdraw(old.id)
    seen = []

    def reader(ref):
        c = sqlite3.connect(store.path, timeout=0.2)             # a writer here would fail if the txn were open
        try:
            c.execute("BEGIN IMMEDIATE")
            c.execute("ROLLBACK")
            seen.append("free")
        except sqlite3.OperationalError:
            seen.append("locked")
        finally:
            c.close()
        return False
    review.push_sign(store, t.thread_id, new.id, "land", onto="main", still_open=reader, rec=review.NO_REC)
    assert seen == ["free"]


def test_f5_a_coordinator_that_lost_the_lease_without_writing_is_a_dead_seat_by_the_leases_own_rule(store, clock):
    from tests.test_coordinator_lease import boot, drop
    from levain.coordinator import lease
    store.coord_set_agent("claude", cwd=str(store.directory.parent))
    ln = boot(store)
    clock.advance(seconds=(lease.LEASE_TTL + lease.IDLE_GRACE + lease.BACKOFF[0]).total_seconds())
    drop(store, "A died idle")
    items = review.feed_dead_seats(store)
    assert ln.launch_id in [i.body["launch_id"] for i in items]


def test_f6_a_death_older_than_the_newest_five_hundred_launches_is_still_found(store, clock):
    store.coord_set_agent("claude", cwd=str(store.directory.parent))
    store.accept_drop("fix the build", surface="cli")
    first = store.pending_coordinator()
    assert store.launch_claim_spawn(first.launch_id, 1)[1] is None
    clock.advance(hours=1)
    store.coord_status()
    c = sqlite3.connect(store.path)
    g = c.execute("SELECT MAX(gseq) FROM launches").fetchone()[0]
    for i in range(520):                                       # healthy ended launches, newer than the death
        lid = "launch-" + f"{i:032x}"
        base = g + 10 + 10 * i
        c.execute("INSERT INTO launches (launch_id, session_key, harness, role, threads, model, cwd, token, placement, at, gseq) "
                  "SELECT ?, ?, harness, 'worker', '[]', model, cwd, token, placement, at, ? FROM launches WHERE launch_id = ?",
                  (lid, "sk-" + lid, base, first.launch_id))
        for seq, kind, cause in ((1, "pending", "asked"), (2, "active", "started"), (3, "ended", "finished")):
            c.execute("INSERT INTO launch_events (launch_id, seq, gseq, kind, until, harness_session_id, cause, at) "
                      "VALUES (?, ?, ?, ?, ?, ?, ?, '2026-10-22T21:00:00.000000Z')",
                      (lid, seq, base + seq, kind, "2026-10-22T21:05:00.000000Z" if seq == 1 else None,
                       "hs-" + lid if seq == 2 else None, cause))
    c.commit()
    c.close()
    ids = [i.body["launch_id"] for i in review.feed_dead_seats(store)]
    assert first.launch_id in ids


def test_f7_a_split_that_lands_between_scan_and_insert_files_nothing(store, clock, monkeypatch):
    stuck, _ = store.accept_drop("note this and that", surface="cli")
    clock.advance(minutes=11)
    orig_scan = review._stuck_drops

    def scan_then_split(store_, after):
        found = orig_scan(store_, after)
        store.split(stuck.drop_id, [Part("work", "fix")])          # the split lands after the scan
        return found
    monkeypatch.setattr(review, "_stuck_drops", scan_then_split)
    assert review.feed_no_split(store) == []
    assert review.list_items(store) == []


def test_f8_a_bad_questions_store_leaves_the_other_sign_records_showing(store, qdir, cq, clock):
    _d, t = _thread(store)
    req = _request(cq)
    review.push_sign(store, t.thread_id, req.id, "land it", rec=review.NO_REC)
    review.push_answer(store, t.thread_id, "q-1", "which?", rec=review.NO_REC)
    (qdir / "questions.db").write_bytes(b"garbage" * 200)
    w = review.waiting_on_you(store, sign_open=review.queue_request_open(qdir, clock=clock), answer_open=review.question_open(qdir))
    assert [r["kind"] for r in w["threads"][t.thread_id]] == ["sign"]
    assert [r["kind"] for r in w["unresolved"]] == ["answer"]


def test_f9_an_expired_request_is_not_waiting_on_you(store, qdir, cq, clock):
    _d, t = _thread(store)
    req = _request(cq)                                          # a five minute hold
    review.push_sign(store, t.thread_id, req.id, "land it", rec=review.NO_REC)
    so = review.queue_request_open(qdir, clock=clock)
    w = review.waiting_on_you(store, sign_open=so, answer_open=review.question_open(qdir))
    assert t.thread_id in w["threads"]
    clock.advance(minutes=5, seconds=30)                        # past the hold, inside the undecided grace: still waiting
    assert so(req.id) is True
    clock.advance(minutes=1)                                    # past the grace: the queue would expire it on a read
    assert so(req.id) is False
    w = review.waiting_on_you(store, sign_open=so, answer_open=review.question_open(qdir))
    assert w == {"threads": {}, "loose": [], "unresolved": []}


def test_f10_one_bad_row_files_the_rest_and_is_reported(store, clock, monkeypatch):
    a, _ = store.accept_drop("note one", surface="cli")
    b, _ = store.accept_drop("note two", surface="cli")
    clock.advance(minutes=11)
    real = review._insert

    def flaky(conn, now, feed, body, **kw):
        if kw["dedupe_key"] == f"receipt:{a.drop_id}":
            raise CoordError("a bad row")
        return real(conn, now, feed, body, **kw)
    monkeypatch.setattr(review, "_insert", flaky)
    errors: list[str] = []
    got = review.feed_no_split(store, errors=errors)
    assert [i.body["drop_id"] for i in got] == [b.drop_id] and len(errors) == 1 and "a bad row" in errors[0]
    with pytest.raises(CoordError, match="a bad row"):          # without a sink the error still surfaces, after the rest
        review.feed_no_split(store)


def test_f10_an_oversized_field_is_clipped_not_fatal(store, qdir):
    limits._halt_on(qdir, "claude_code", "x" * 20000, "tool_calls", "req-big", None, time.time())
    limits._halt_on(qdir, "claude_code", "sess-ok", "tool_calls", "req-ok", None, time.time())
    errors: list[str] = []
    items = review.feed_end(store, qdir, errors=errors)
    assert len(items) == 2 and not errors
    assert all(len(i.body["root"]) <= review._FIELD_MAX for i in items)


# --- L3 r2 fixes (c4fix2 lane) ---------------------------------------------------------------------------------
def _healthy_launches(store, template_id, n, *, top):
    """``n`` healthy ended launches above gseq ``top``, copied from a real launch row (the F6 fixture, as a helper)."""
    c = sqlite3.connect(store.path)
    for i in range(n):
        lid = "launch-" + f"{top + i:032x}"
        base = top + 10 + 10 * i
        c.execute("INSERT INTO launches (launch_id, session_key, harness, role, threads, model, cwd, token, placement, at, gseq) "
                  "SELECT ?, ?, harness, 'worker', '[]', model, cwd, token, placement, at, ? FROM launches WHERE launch_id = ?",
                  (lid, "sk-" + lid, base, template_id))
        for seq, kind, cause in ((1, "pending", "asked"), (2, "active", "started"), (3, "ended", "finished")):
            c.execute("INSERT INTO launch_events (launch_id, seq, gseq, kind, until, harness_session_id, cause, at) "
                      "VALUES (?, ?, ?, ?, ?, ?, ?, '2026-10-22T21:00:00.000000Z')",
                      (lid, seq, base + seq, kind, "2026-10-22T21:05:00.000000Z" if seq == 1 else None,
                       "hs-" + lid if seq == 2 else None, cause))
    c.commit()
    top_now = c.execute("SELECT MAX(gseq) FROM launch_events").fetchone()[0]
    c.close()
    return top_now


def _spawned_launch(store):
    store.coord_set_agent("claude", cwd=str(store.directory.parent))
    store.accept_drop("fix the build", surface="cli")
    first = store.pending_coordinator()
    assert store.launch_claim_spawn(first.launch_id, 1)[1] is None
    c = sqlite3.connect(store.path)
    g = c.execute("SELECT MAX(gseq) FROM launches").fetchone()[0]
    c.close()
    return first, g


def test_g1_a_death_below_more_than_two_thousand_healthy_launches_is_filed_and_every_launch_is_read_once(store, clock, monkeypatch):
    from levain.coordinator import lease
    first, g = _spawned_launch(store)
    clock.advance(hours=1)
    store.coord_status()
    _healthy_launches(store, first.launch_id, 2100, top=g)
    seen: dict[str, int] = {}
    real = lease.launch

    def counting(conn, launch_id):
        seen[launch_id] = seen.get(launch_id, 0) + 1
        return real(conn, launch_id)
    monkeypatch.setattr(lease, "launch", counting)
    ids: list[str] = []
    for _run in range(4):                                       # bounded: the page bound only delays
        ids += [i.body["launch_id"] for i in review.feed_dead_seats(store)]
    assert ids == [first.launch_id]
    assert max(seen.values()) == 1 and len(seen) == 2101        # each terminal launch evaluated exactly once, ever


def _allocator_past(store, top):
    """Move the store's gseq allocator past ``top``: the fixtures write gseq values by hand, and a real event is
    always numbered by the allocator (commit order = gseq order)."""
    c = sqlite3.connect(store.path)
    c.execute("INSERT INTO gseq (gseq, at) VALUES (?, 'x')", (top + 100,))
    c.commit()
    c.close()


def _dead_launch_above(store, template_id, top, *, cause="0 turns"):
    """One launch that ended at ``0 turns`` (a death by the lease's rule), its events above gseq ``top``."""
    lid = "ln-" + f"{(0xdead << 80) + top:032x}"
    c = sqlite3.connect(store.path)
    c.execute("INSERT INTO launches (launch_id, session_key, harness, role, threads, model, cwd, token, placement, at, gseq) "
              "SELECT ?, ?, harness, 'worker', '[]', model, cwd, token, placement, at, ? FROM launches WHERE launch_id = ?",
              (lid, "sk-" + lid, top + 1, template_id))
    for seq, kind, c_ in ((1, "pending", "asked"), (2, "active", "started"), (3, "ended", cause)):
        c.execute("INSERT INTO launch_events (launch_id, seq, gseq, kind, until, harness_session_id, cause, at) "
                  "VALUES (?, ?, ?, ?, ?, ?, ?, '2026-10-22T21:00:00.000000Z')",
                  (lid, seq, top + 1 + seq, kind, "2026-10-22T21:05:00.000000Z" if seq == 1 else None,
                   "hs-" + lid if seq == 2 else None, c_))
    c.commit()
    c.close()
    return lid


def test_g1_one_long_running_launch_pins_nothing_and_every_terminal_event_is_read_once(store, clock, monkeypatch):
    from levain.coordinator import lease
    first, g = _spawned_launch(store)                           # still pending at the lowest gseq: never terminal here
    top = _healthy_launches(store, first.launch_id, 2100, top=g)
    _allocator_past(store, top + 10)
    seen: dict[str, int] = {}
    real = lease.launch

    def counting(conn, launch_id):
        seen[launch_id] = seen.get(launch_id, 0) + 1
        return real(conn, launch_id)
    monkeypatch.setattr(lease, "launch", counting)
    for _run in range(4):
        assert review.feed_dead_seats(store) == []
    dead = _dead_launch_above(store, first.launch_id, top)
    ids = [i.body["launch_id"] for i in review.feed_dead_seats(store)]
    assert ids == [dead]                                        # filed on the first run after it died
    for _run in range(3):
        review.feed_dead_seats(store)
    assert max(seen.values()) == 1 and len(seen) == 2101        # 2100 healthy + the death; the pinned one never read
    clock.advance(hours=1)                                      # the pinned launch dies later
    assert [i.body["launch_id"] for i in review.feed_dead_seats(store)] == [first.launch_id]


def test_g1_a_launch_that_dies_after_the_offset_passed_its_start_is_still_filed(store, clock):
    first, g = _spawned_launch(store)                           # pending: it can still die
    _allocator_past(store, _healthy_launches(store, first.launch_id, 30, top=g) + 10)
    assert review.feed_dead_seats(store) == []
    clock.advance(hours=1)                                      # now it has expired without a start
    assert [i.body["launch_id"] for i in review.feed_dead_seats(store)] == [first.launch_id]


def test_g1_the_watermark_is_persisted_and_never_moves_back(store):
    _spawned_launch(store)
    store.accept_drop("note this", surface="cli")
    review.feed_dead_seats(store)
    c = sqlite3.connect(store.path)
    value = c.execute("SELECT value FROM review_marks WHERE feed = 'seat' ORDER BY seq DESC LIMIT 1").fetchone()[0]
    seq = c.execute("SELECT MAX(seq) FROM review_marks WHERE feed = 'seat'").fetchone()[0]
    with pytest.raises(sqlite3.IntegrityError, match="moves forward"):
        c.execute("INSERT INTO review_marks (feed, seq, value, at) VALUES ('seat', ?, ?, 'x')", (seq + 1, value - 1))
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        c.execute("UPDATE review_marks SET value = 0")
    c.close()


def test_g2_a_gap_longer_than_the_window_loses_no_calls_and_a_boundary_call_counts_once(store, qdir):
    led = Ledger(qdir)
    t1 = datetime(2026, 10, 22, 12, 0, tzinfo=timezone.utc)
    _calls(led, 1, t1 - timedelta(hours=1))
    _calls(led, 1, t1)                                          # exactly on the first window's end
    one = review.feed_digest(store, qdir, until=t1)
    assert one.body["calls"] == 1                               # half-open: the boundary call is the next window's
    _calls(led, 1, t1 + timedelta(days=1))                      # then nobody looks for twenty days (> since)
    two = review.feed_digest(store, qdir, until=t1 + timedelta(days=20))
    assert two.body["since"] == one.body["until"] and two.body["calls"] == 2


def test_g3_a_bad_stop_earlier_without_an_error_sink_still_files_the_valid_envelope(ctx, structured_thread):  # noqa: F811
    _d, b, t = structured_thread
    c = sqlite3.connect(ctx.store.path)
    c.execute("INSERT INTO envelope_attenuations (envelope_id, thread_id, kind, value, by, gseq, at) "
              "VALUES (?, NULL, 'stop_earlier', 'not-a-stamp', 'operator', 99999999, 'x')", (b["envelope_id"],))
    c.commit()
    c.close()
    with pytest.raises(CoordError, match="stop_earlier"):
        review.feed_envelopes(ctx.store, near=3 * HOUR)
    assert len(review.list_items(ctx.store, feed="envelope")) == 1


def test_g4_a_dead_seat_run_with_nothing_to_expire_opens_no_write_transaction_for_the_expiry(store, monkeypatch):
    opened = []
    real = store._write
    monkeypatch.setattr(store, "_write", lambda *a, **k: (opened.append(1), real(*a, **k))[1])
    assert review.feed_dead_seats(store) == []
    assert opened == []

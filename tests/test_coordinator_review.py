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
    return ConsentQueue(qdir, authority=authority, clock=clock)


def _request(cq, command="touch /tmp/x"):
    return cq.file(agent="claude_code", agent_session_id="sess-1", tool_name="Bash", tool_input={"command": command},
                   cwd="/work/proj", hold=timedelta(minutes=5), tier="T2")


def test_waiting_on_you_is_read_from_the_queue_and_the_band_not_stored(store, qdir, cq):
    _d, t = _thread(store)
    req = _request(cq)
    qs = QuestionStore(qdir)
    q = qs.file(agent="claude_code", agent_session_id="sess-1", what="which branch?",
                options=[("a", "main"), ("b", "other")], rec="a", blocks="the land", cwd="/work/proj")
    review.push_sign(store, t.thread_id, req.id, "land the fix onto main", onto="main")
    review.push_answer(store, t.thread_id, q.id, "which branch?")
    sign_open, answer_open = review.queue_request_open(qdir), review.question_open(qdir)
    w = review.waiting_on_you(store, sign_open=sign_open, answer_open=answer_open)
    assert sorted(r["kind"] for r in w["threads"][t.thread_id]) == ["answer", "sign"] and not w["unresolved"]
    # the queue closes the request (withdrawn) and the band gets its answer: the same records read as not waiting
    assert cq.withdraw(req.id)
    qs.answer(q.id, option="a", channel="cli")
    w = review.waiting_on_you(store, sign_open=sign_open, answer_open=answer_open)
    assert w == {"threads": {}, "loose": [], "unresolved": []}


def test_a_record_whose_state_cannot_be_read_is_unresolved_never_closed(store, qdir):
    _d, t = _thread(store)
    review.push_sign(store, t.thread_id, "no-such-request", "land it")
    w = review.waiting_on_you(store, sign_open=review.queue_request_open(qdir),
                              answer_open=review.question_open(qdir))
    assert [r["ref_id"] for r in w["unresolved"]] == ["no-such-request"] and w["threads"] == {}


def test_the_same_request_is_recorded_once(store, qdir, cq):
    _d, t = _thread(store)
    req = _request(cq)
    first = review.push_sign(store, t.thread_id, req.id, "land it")
    assert review.push_sign(store, t.thread_id, req.id, "land it") == (first[0], False)
    assert sqlite3.connect(store.path).execute("SELECT COUNT(*) FROM replies WHERE kind = 'sign'").fetchone()[0] == 1


def test_one_open_land_sign_per_thread_and_onto(store, qdir, cq):
    _d, t = _thread(store)
    old, new = _request(cq, "git push 1"), _request(cq, "git push 2")
    sign_open = review.queue_request_open(qdir)
    review.push_sign(store, t.thread_id, old.id, "land", onto="main", still_open=sign_open)
    with pytest.raises(CoordError, match="already has an open land SIGN"):
        review.push_sign(store, t.thread_id, new.id, "land again", onto="main", still_open=sign_open)
    review.push_sign(store, t.thread_id, new.id, "another branch", onto="other", still_open=sign_open)   # other onto
    assert cq.withdraw(old.id)                       # MOVED BASE: the re-file withdraws the older one first
    assert review.push_sign(store, t.thread_id, _request(cq, "git push 3").id, "land", onto="main",
                            still_open=sign_open)[1] is True


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
    store.coord_status()                                   # a read that expires the pending launch
    items = review.feed_dead_seats(store)
    assert [(i.feed, i.body["launch_id"]) for i in items] == [("seat", ln.launch_id)]
    assert items[0].body["state"] in ("expired", "failed") and not items[0].authority_relevant
    assert review.feed_dead_seats(store)[0].item_id == items[0].item_id


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
    assert review.feed_digest(store, qdir, until=now + timedelta(minutes=2)).item_id == item.item_id


def test_collect_feeds_reports_a_failing_feed_by_name_and_runs_the_rest(store, qdir, monkeypatch):
    store.accept_drop("note this", surface="cli")

    def boom(*a, **k):
        raise RuntimeError("limits unreadable")

    monkeypatch.setattr(review, "feed_end", boom)
    out = review.collect_feeds(store, queue_dir=qdir)
    assert out["end"].startswith("error: RuntimeError") and out["receipt"] == [] and out["digest"] == []

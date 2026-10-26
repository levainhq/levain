"""Coordinator C4-a: the delivery pass, the feed runner and ``levain coord review`` (C0 §3.3, §3.6).

Real stores throughout, the first-reader tests' own band (a thread under a live signed envelope, a worker launch pinned
to it, an active coordinator launch, a real question store) plus a real consent queue with a signed authority log.
Synthetic words."""
from __future__ import annotations

import io
import sys
import json
import sqlite3
from argparse import Namespace
from datetime import timedelta

import pytest

from levain.consent import ConsentQueue
from levain.coordinator import deliver, review
from levain.coordinator import first_reader as fr
from levain.coordinator.cli import run_coord
from tests.test_consent_queue import Log, keys  # noqa: F401 - fixtures
from tests.test_coordinator_envelope import ctx, repo, snapshot  # noqa: F401 - fixtures
from tests.test_coordinator_first_reader import SEAT, Band
from tests._h4 import h4_home

HOLD = timedelta(hours=3)


@pytest.fixture
def band(ctx):  # noqa: F811
    return Band(ctx)


@pytest.fixture
def cq(band, keys, tmp_path):  # noqa: F811
    (tmp_path / "auth").mkdir()
    lg = Log(tmp_path / "auth", keys)
    lg.enrol("laptop", "laptop-presence")
    return ConsentQueue(band.store.directory, authority=lg.source, home=h4_home())


def _file(cq, band, *, session=SEAT, command="git push origin main"):
    return cq.file(agent="claude_code", agent_session_id=session, tool_name="Bash", tool_input={"command": command},
                   cwd=band.c.path, hold=HOLD)


def _waiting(band):
    d = band.store.directory
    return review.waiting_on_you(band.store, sign_open=review.queue_request_open(d),
                                 answer_open=review.question_open(d))


def _args(band, cmd, **kw):
    return Namespace(coord_cmd="review", review_cmd=cmd, queue_dir=band.store.directory, json=kw.pop("json", False),
                     **kw)


def _replies(band):
    return band.c.rows("SELECT kind, ref_id, thread_id FROM replies WHERE kind IN ('sign', 'answer') ORDER BY rowid")


def test_a_request_of_a_thread_seat_gets_one_sign_and_a_second_pass_files_nothing(band, cq):
    req = _file(cq, band)
    first = deliver.deliver(band.store)
    assert [(r, n) for r, _rp, n in first.signs] == [(req.id, True)] and first.errors == []
    assert _replies(band) == [("sign", req.id, band.thread.thread_id)]
    waiting = _waiting(band)
    assert [r["ref_id"] for r in waiting["threads"][band.thread.thread_id]] == [req.id]
    again = deliver.deliver(band.store)
    assert [(r, n) for r, _rp, n in again.signs] == [(req.id, False)] and again.created == 0
    assert _replies(band) == [("sign", req.id, band.thread.thread_id)]


def test_a_request_of_a_session_levain_did_not_start_files_nothing(band, cq):
    _file(cq, band, session="sess-the-operators-own")
    got = deliver.deliver(band.store)
    assert got.signs == [] and got.errors == [] and _replies(band) == []


def test_an_undecided_request_is_pointed_at_too(band, cq):
    req = _file(cq, band)
    cq.mark_undecided(req.id)
    assert cq.get(req.id)[1] == "undecided"
    assert [r for r, _rp, _n in deliver.deliver(band.store).signs] == [req.id]
    assert [r["ref_id"] for r in _waiting(band)["threads"][band.thread.thread_id]] == [req.id]


def test_a_decided_request_is_not_pointed_at_and_a_pointed_one_stops_waiting(band, cq):
    gone = _file(cq, band)
    cq.withdraw(gone.id)
    assert deliver.deliver(band.store).signs == [] and _replies(band) == []
    req = _file(cq, band)
    deliver.deliver(band.store)
    assert _waiting(band)["threads"]
    cq.withdraw(req.id)                         # closed in the queue: the record stays, the waiting goes
    assert _waiting(band)["threads"] == {} and len(_replies(band)) == 1


def test_a_question_of_a_thread_seat_gets_one_answer(band):
    q = band.ask()
    first = deliver.deliver(band.store)
    assert [(r, n) for r, _rp, n in first.answers] == [(q.id, True)]
    assert _replies(band) == [("answer", q.id, band.thread.thread_id)]
    assert [r["ref_id"] for r in _waiting(band)["threads"][band.thread.thread_id]] == [q.id]
    assert deliver.deliver(band.store).created == 0
    band.qs.answer(q.id, option="b", channel="cli")      # his answer: the record stays, the waiting goes
    assert _waiting(band)["threads"] == {} and len(_replies(band)) == 1


def test_a_question_the_coordinator_holds_or_answered_is_not_pushed(band):
    held = band.ask("held first?")
    # the filing ping's hold is first_reader's record (hold_ping itself reads the real clock, the fixture's is fixed)
    sqlite3.connect(band.store.path).execute(
        "INSERT INTO held_pings (question_id, lease_token, at) VALUES (?, ?, ?)",
        (held.id, band.token, "2026-10-20T12:00:00.000000Z")).connection.commit()
    known = band.ask("answered as a KNOW?")
    band.answer(known)
    assert deliver.deliver(band.store).answers == [] and _replies(band) == []
    # the held ping's own release files the ANSWER (escalated by the coordinator), and the pass then reaches that record
    fr.escalate(band.store, held.id, "his call", token=band.token)
    got = deliver.deliver(band.store)
    assert [(r, n) for r, _rp, n in got.answers] == [(held.id, False)] and got.created == 0


def test_a_question_of_an_unstarted_session_files_nothing(band):
    band.ask(session="sess-the-operators-own")
    assert deliver.deliver(band.store).answers == [] and _replies(band) == []


def test_the_runner_runs_the_feeds_and_the_delivery_and_a_second_run_creates_nothing(band, cq):
    _file(cq, band)
    band.ask()
    count = lambda: band.c.rows("SELECT (SELECT COUNT(*) FROM review_items), (SELECT COUNT(*) FROM replies)")  # noqa: E731
    first = deliver.run(band.store)
    assert set(first) == {"feeds", "delivery"} and first["delivery"]["created"] == 2
    assert not [v for v in first["feeds"].values() if isinstance(v, str)]
    mid = count()
    second = deliver.run(band.store)
    assert second["delivery"]["created"] == 0 and count() == mid


def test_a_band_that_cannot_be_read_is_named_and_the_other_half_still_runs(band, tmp_path):
    band.ask()
    got = deliver.deliver(band.store, queue_dir=band.store.directory)
    assert got.errors == [] and len(got.answers) == 1
    (band.store.directory / "queue.db").write_text("not a database")
    broken = deliver.deliver(band.store)
    assert any(e.startswith("requests:") for e in broken.errors) and broken.created == 0


def test_cli_list_shows_the_sign_and_the_waiting_line_and_a_second_list_adds_nothing(band, cq):
    req = _file(cq, band)
    out = io.StringIO()
    assert run_coord(_args(band, "list", all=False, feed=None), out) == 0
    text = out.getvalue()
    assert "waiting on you: 1" in text and f"[{band.thread.thread_id}] SIGN:" in text and "git push origin main" in text
    n = band.c.rows("SELECT COUNT(*) FROM replies")
    out = io.StringIO()
    assert run_coord(_args(band, "list", all=False, feed=None), out) == 0
    assert out.getvalue() == text and band.c.rows("SELECT COUNT(*) FROM replies") == n
    assert req.id in [r["ref_id"] for r in _waiting(band)["threads"][band.thread.thread_id]]


def test_cli_clear_reopen_and_feeds(band):
    item, _rp = review.file_know(band.store, None, "a note for his review", by="operator")
    out = io.StringIO()
    assert run_coord(_args(band, "list", all=False, feed="know"), out) == 0
    assert item.item_id in out.getvalue()
    out = io.StringIO()
    assert run_coord(_args(band, "clear", item_id=item.item_id, mode="manual", token=None), out) == 0
    assert "cleared (manual)" in out.getvalue() and review.get_item(band.store, item.item_id).state == "cleared"
    out = io.StringIO()
    assert run_coord(_args(band, "list", all=False, feed=None), out) == 0
    assert item.item_id not in out.getvalue()
    out = io.StringIO()
    assert run_coord(_args(band, "list", all=True, feed=None, json=True), out) == 0
    assert [r["state"] for r in json.loads(out.getvalue())["items"] if r["item_id"] == item.item_id] == ["cleared"]
    out = io.StringIO()
    assert run_coord(_args(band, "reopen", item_id=item.item_id, mode="manual", token=None), out) == 0
    assert review.get_item(band.store, item.item_id).state == "open"
    out = io.StringIO()
    assert run_coord(_args(band, "feeds", json=True), out) == 0
    assert json.loads(out.getvalue())["delivery"]["created"] == 0


def test_cli_clear_in_coordinator_mode_needs_the_lease_token_and_refuses_when_it_has_none(band, capsys):
    item, _rp = review.file_know(band.store, None, "authority", authority_relevant=True, by="operator")
    code = run_coord(_args(band, "clear", item_id=item.item_id, mode="coordinator", token=None), io.StringIO())
    assert code == 1 and "lease token" in capsys.readouterr().err
    assert review.get_item(band.store, item.item_id).state == "open"


def test_the_parser_takes_the_review_verbs():
    import argparse

    from levain.coordinator.cli import add_parsers

    root = argparse.ArgumentParser()
    add_parsers(root.add_subparsers(dest="cmd"))
    for argv in (["coord", "review", "list"], ["coord", "review", "list", "--all", "--feed", "seat"],
                 ["coord", "review", "clear", "rv-x"], ["coord", "review", "reopen", "rv-x", "--mode", "fresh"],
                 ["coord", "review", "feeds", "--json"]):
        ns = root.parse_args(argv)
        assert ns.coord_cmd == "review" and ns.review_cmd == argv[2] and ns.func.__name__ == "run_coord"


# --- C4-a L3 r1 fixes (RULINGS_R1) -------------------------------------------------------------------------------------
def _holds(band):
    return band.c.rows("SELECT question_id FROM held_pings")


def _live_hold(band, monkeypatch):
    """hold_ping on the band's own store (its clock is fixed, hold_ping's default reads the real one)."""
    monkeypatch.setattr(fr, "_coord_store", lambda d: band.store, raising=False)
    monkeypatch.setattr(fr, "_coordinator_token", lambda conn, now: band.token)   # the band's lease is the operator's


def test_an_answer_pushed_in_the_gap_before_the_hold_declines_the_hold(band, monkeypatch):
    """Item 1, the pass wins: between its snapshot and its push the question is not yet held; once the ANSWER exists a
    hold would also suppress the operator's ping, so hold_ping declines."""
    _live_hold(band, monkeypatch)
    q = band.ask()
    assert deliver.deliver(band.store).answers[0][2] is True
    assert fr.hold_ping(band.store.directory, q.id) is False and _holds(band) == []


def test_hold_ping_finds_the_answer_record_under_the_reference_stores_one_name(band, monkeypatch):
    """C4 integration item 9: the lookup reads review._REF_STORE["answer"], not a literal of its own."""
    _live_hold(band, monkeypatch)
    monkeypatch.setitem(review._REF_STORE, "answer", "renamed-questions.db")
    q = band.ask()
    assert deliver.deliver(band.store).answers[0][2] is True
    assert fr.hold_ping(band.store.directory, q.id) is False and _holds(band) == []


def test_a_hold_that_lands_between_the_snapshot_and_the_push_wins_and_no_answer_is_filed(band, monkeypatch):
    """Item 1, the hold wins: hold_ping runs after the pass read the question as not held and before it pushes."""
    _live_hold(band, monkeypatch)
    q = band.ask()
    real = review._push
    seen = []

    def interleaved(store, kind, *a, **kw):
        if kind == "answer" and not seen:
            seen.append(fr.hold_ping(store.directory, q.id))
        return real(store, kind, *a, **kw)

    monkeypatch.setattr(review, "_push", interleaved)
    got = deliver.deliver(band.store)
    assert seen == [True] and _holds(band) == [(q.id,)]
    assert got.errors == [] and got.answers == [] and _replies(band) == []


def test_a_truncated_or_unreadable_listing_is_a_named_error_and_the_cli_exits_1(band, cq, monkeypatch):
    """Item 2: overflow and skipped are not a quiet "complete"."""
    import dataclasses

    from levain.consent import listing as listing_mod

    _file(cq, band)
    real = listing_mod.read_listing
    monkeypatch.setattr(listing_mod, "read_listing",
                        lambda *a, **kw: dataclasses.replace(real(*a, **kw), overflow=True, skipped=2))
    got = deliver.deliver(band.store)
    assert [e for e in got.errors if e.startswith("requests:") and "not all" in e]
    assert [e for e in got.errors if e.startswith("requests:") and "2" in e and "skipped" in e]
    assert len(got.signs) == 1                                  # what could be read is still pointed at
    out = io.StringIO()
    assert run_coord(_args(band, "list", all=False, feed=None), out) == 1
    assert "could not read: delivery: requests:" in out.getvalue()


def test_one_row_that_raises_is_an_error_line_and_the_other_rows_are_still_filed(band, cq, monkeypatch):
    """Item 4: discovery is per row, as the push loop is."""
    first = _file(cq, band)
    second = _file(cq, band, command="git push origin dev")
    real = deliver._one_thread

    def flaky(conn, agent, session):
        if not flaky.hit:
            flaky.hit = True
            raise ValueError("a row that does not read")
        return real(conn, agent, session)

    flaky.hit = False
    monkeypatch.setattr(deliver, "_one_thread", flaky)
    got = deliver.deliver(band.store)
    assert len(got.signs) == 1 and len(got.errors) == 1 and "ValueError" in got.errors[0]
    assert got.signs[0][0] in (first.id, second.id)


def test_an_unreadable_stamp_is_a_named_error_and_the_questions_are_still_read(band, monkeypatch):
    """Item 6: the stamp read is inside the pass's error handling."""
    band.ask()
    monkeypatch.setattr(deliver, "parse_stamp", lambda s: (_ for _ in ()).throw(ValueError("bad stamp")))
    got = deliver.deliver(band.store)
    assert [e for e in got.errors if e.startswith("stamp:")] and len(got.answers) == 1


def test_a_feeds_run_counts_only_what_it_created_not_what_another_writer_filed(band, monkeypatch):
    """Item 7: "N new" sums the ids this run's feeds created; a concurrent writer's item is not this run's."""
    real = deliver.run
    neighbour = []

    def with_a_neighbour(store, **kw):
        item, _rp = review.file_know(store, None, "filed by a panel read mid-run", by="operator")
        neighbour.append(item.item_id)
        return real(store, **kw)

    monkeypatch.setattr(deliver, "run", with_a_neighbour)
    out = io.StringIO()
    assert run_coord(_args(band, "feeds", json=True), out) == 0
    got = json.loads(out.getvalue())
    assert neighbour[0] not in got["feeds"]["created"] and got["new_items"] == len(got["feeds"]["created"])
    assert band.c.rows("SELECT COUNT(*) FROM review_items")[0][0] == got["new_items"] + 1
    out = io.StringIO()
    assert run_coord(_args(band, "feeds"), out) == 0
    assert out.getvalue().splitlines()[0] == "feeds: 0 new review item(s)"


def test_a_feeds_run_that_creates_an_item_counts_it_once_and_the_rerun_counts_none(band):
    band.store.accept_drop("note this", surface="cli")
    band.store._clock = lambda: review.parse_stamp("2099-01-01T00:00:00.000000Z")      # the drop is long stuck
    first = deliver.run(band.store)["feeds"]
    assert len(first["created"]) >= 1 and set(first["created"]) <= {i for v in first.values() if isinstance(v, list)
                                                                     for i in v}
    assert deliver.run(band.store)["feeds"]["created"] == []


def test_review_list_says_open_items_by_name(band):
    out = io.StringIO()
    assert run_coord(_args(band, "list", all=False, feed=None), out) == 0
    head = out.getvalue().splitlines()[0]
    assert head.startswith("review queue: ") and head.endswith(" open item(s)")


def test_a_feeds_errors_that_is_a_string_never_reaches_items(band, monkeypatch):
    """Item 3: one error shape reaches the CLI whatever collect_feeds returns."""
    monkeypatch.setattr(deliver, "run", lambda store, **kw: {
        "feeds": {"seat": [], "errors": "could not read the lot"},
        "delivery": {"signs": [], "answers": [], "created": 0, "errors": []}})
    out = io.StringIO()
    assert run_coord(_args(band, "feeds"), out) == 1
    assert "could not read: errors: could not read the lot" in out.getvalue()
    out = io.StringIO()
    assert run_coord(_args(band, "list", all=False, feed=None), out) == 1
    assert "could not read the lot" in out.getvalue()


def test_two_passes_that_both_saw_the_request_unpointed_file_one_sign(band, cq, monkeypatch):
    """Item 5: both passes snapshot before either pushes; the push's check and insert share one BEGIN IMMEDIATE
    transaction, so the second push finds the first's record (no extra uniqueness constraint was needed)."""
    req = _file(cq, band)
    real = review.push_sign
    inner = []
    started = []

    def other_pass_first(store, thread, request_id, text, **kw):
        if not started:
            started.append(True)
            inner.append(deliver.deliver(band.store))
        return real(store, thread, request_id, text, **kw)

    monkeypatch.setattr(review, "push_sign", other_pass_first)
    outer = deliver.deliver(band.store)
    assert [(r, n) for r, _rp, n in inner[0].signs] == [(req.id, True)]
    assert [(r, n) for r, _rp, n in outer.signs] == [(req.id, False)]
    assert _replies(band) == [("sign", req.id, band.thread.thread_id)]


def test_a_panel_read_of_the_queue_and_the_waiting_set_never_takes_the_schema_creating_connect(band, monkeypatch):
    """Item 9 (c4b RULINGS_OBS 2): list_items and waiting_on_you read through _read_connect on a fresh store object."""
    review.file_know(band.store, None, "a note", by="operator")
    fresh = type(band.store)(band.store.directory)

    def boom():
        raise AssertionError("a read took the schema-creating connect")

    monkeypatch.setattr(fresh, "_connect", boom)
    assert [i.body["text"] for i in review.list_items(fresh)] == ["a note"]
    d = band.store.directory
    assert review.waiting_on_you(fresh, sign_open=review.queue_request_open(d),
                                 answer_open=review.question_open(d))["threads"] == {}


def test_the_morning_card_words_the_waiting_set_with_the_one_formatter(band, cq):
    """Item 8: morning.render and `coord review list` print the same waiting lines (deliver.waiting_lines)."""
    from levain.coordinator import morning

    _file(cq, band)
    deliver.deliver(band.store)
    card = morning.card(band.store)
    lines = deliver.waiting_lines(card["waiting"])
    assert lines[0] == "waiting on you: 1" and all(ln in morning.render(card) for ln in lines)


# --- the scheduled run: one at a time, bounded -------------------------------------------------------------------------

def _hold_lock(band):
    import fcntl
    import os

    fd = os.open(band.store.directory / deliver.FEEDS_LOCK, os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    return fd


def test_a_feeds_run_while_another_holds_the_lock_skips_and_files_nothing(band):
    import os

    fd = _hold_lock(band)
    try:
        assert deliver.run_exclusive(band.store) is None
        out = io.StringIO()
        assert run_coord(_args(band, "feeds", json=True), out) == 0
        assert "skipped" in json.loads(out.getvalue())
    finally:
        os.close(fd)
    assert set(deliver.run_exclusive(band.store)) == {"feeds", "delivery"}      # released: the next run goes


def test_a_second_exclusive_run_after_the_first_ends_is_not_blocked_by_a_stale_lock_file(band):
    assert deliver.run_exclusive(band.store) is not None
    assert (band.store.directory / deliver.FEEDS_LOCK).exists()
    assert deliver.run_exclusive(band.store) is not None


def _record_alarm(monkeypatch):
    """Record the alarm calls and the handler the kernel would act on, with the real alarm replaced by a recorder."""
    import signal

    calls: list[int] = []
    monkeypatch.setattr(signal, "alarm", lambda n: calls.append(n) or 0)
    handlers: list[object] = []
    real = signal.signal
    monkeypatch.setattr(signal, "signal", lambda sig, h: handlers.append((sig, h)) or real(sig, h))
    return calls, handlers


def test_a_bounded_feeds_run_arms_the_kernel_alarm_at_the_default_action_and_disarms_after_the_output(band, monkeypatch):
    import signal

    calls, handlers = _record_alarm(monkeypatch)
    wrote_when_disarmed: list[str] = []
    before = signal.getsignal(signal.SIGALRM)

    class Out(io.StringIO):
        def write(self, text):
            wrote_when_disarmed.append(f"{text.strip()[:1]}|{calls[-1] if calls else None}")
            return super().write(text)

    assert run_coord(_args(band, "feeds", json=True, max_seconds=7), Out()) == 0
    assert calls == [7, 0]                                         # armed with the integer, disarmed once
    assert handlers[0] == (signal.SIGALRM, signal.SIG_DFL)         # no Python handler: the kernel ends the process
    assert signal.getsignal(signal.SIGALRM) == before              # and the caller's disposition is put back
    assert wrote_when_disarmed and all(w.endswith("|7") for w in wrote_when_disarmed)   # output written while armed


def test_the_alarm_is_armed_before_the_store_is_opened_and_through_the_error_line(band, monkeypatch, capsys):
    from levain.coordinator import cli as coord_cli

    calls, _handlers = _record_alarm(monkeypatch)
    seen: dict[str, object] = {}
    real_store = coord_cli._store

    def store_when(args):
        seen["armed_at_open"] = list(calls)
        return real_store(args)

    monkeypatch.setattr(coord_cli, "_store", store_when)
    assert run_coord(_args(band, "feeds", json=True, max_seconds=9), io.StringIO()) == 0
    assert seen["armed_at_open"] == [9]

    calls.clear()

    def refuse(store, **kw):
        raise deliver.CoordError("the feeds lock cannot be taken")

    monkeypatch.setattr(deliver, "run_exclusive", refuse)
    err_when: list[int | None] = []
    real_print = print

    def spy_print(*a, **kw):
        if kw.get("file") is sys.stderr:
            err_when.append(calls[-1] if calls else None)
        return real_print(*a, **kw)

    monkeypatch.setattr("builtins.print", spy_print)
    assert run_coord(_args(band, "feeds", json=True, max_seconds=9), io.StringIO()) == 1
    assert err_when == [9] and calls == [9, 0]                     # the error line was written while armed


def test_an_in_process_caller_keeps_its_alarm_disposition_and_outstanding_alarm(band, monkeypatch):
    import signal

    calls: list[int] = []
    monkeypatch.setattr(signal, "alarm", lambda n: calls.append(n) or (30 if len(calls) == 1 else 0))
    mine = lambda *a: None  # noqa: E731
    old = signal.signal(signal.SIGALRM, mine)
    try:
        assert run_coord(_args(band, "feeds", json=True, max_seconds=7), io.StringIO()) == 0
        assert signal.getsignal(signal.SIGALRM) is mine
        assert calls == [7, 0, 30]                                 # armed, cancelled, the caller's 30 s put back
    finally:
        signal.signal(signal.SIGALRM, old)


def test_an_unbounded_feeds_run_arms_nothing(band, monkeypatch):
    calls, handlers = _record_alarm(monkeypatch)
    assert run_coord(_args(band, "feeds", json=True), io.StringIO()) == 0
    assert calls == [] and handlers == []


@pytest.mark.parametrize("bad", [-1, 1.5, "3"])
def test_a_bad_bound_is_refused(band, bad):
    assert run_coord(_args(band, "feeds", json=True, max_seconds=bad), io.StringIO()) == 1


def test_the_skip_payload_keeps_the_runs_key_set(band):
    import os

    fd = _hold_lock(band)
    try:
        out = io.StringIO()
        assert run_coord(_args(band, "feeds", json=True), out) == 0
        got = json.loads(out.getvalue())
    finally:
        os.close(fd)
    assert got["skipped"] is True and got["feeds"] == {} and got["delivery"] == {} and got["new_items"] == 0


def test_a_lock_failure_that_is_not_contention_is_an_error_not_a_skip(band, monkeypatch):
    import errno
    import fcntl

    def boom(fd, op):
        raise OSError(errno.EIO, "disk")

    monkeypatch.setattr(fcntl, "flock", boom)
    out = io.StringIO()
    with pytest.raises(deliver.CoordError, match="cannot be taken"):
        deliver.run_exclusive(band.store)
    assert run_coord(_args(band, "feeds", json=True), out) == 1


def test_contention_errnos_are_the_only_skips(band, monkeypatch):
    import errno
    import fcntl

    for code in (errno.EAGAIN, errno.EACCES):
        monkeypatch.setattr(fcntl, "flock", lambda fd, op, c=code: (_ for _ in ()).throw(OSError(c, "held")))
        assert deliver.run_exclusive(band.store) is None


def test_a_lock_that_is_a_symlink_is_a_named_error(band):
    import os

    lock = band.store.directory / deliver.FEEDS_LOCK
    target = band.store.directory / "elsewhere"
    target.write_text("")
    os.symlink(target, lock)
    out = io.StringIO()
    with pytest.raises(deliver.CoordError, match="cannot be opened"):
        deliver.run_exclusive(band.store)
    assert run_coord(_args(band, "feeds", json=True), out) == 1


def test_a_store_directory_that_is_a_symlink_is_refused_before_a_lock_is_made(tmp_path):
    import os

    from levain.coordinator.store import CoordStore

    real = tmp_path / "real"
    real.mkdir(mode=0o700)
    link = tmp_path / "link"
    os.symlink(real, link)
    with pytest.raises(deliver.CoordError, match="cannot be opened"):
        deliver.run_exclusive(CoordStore(link))
    assert not (real / deliver.FEEDS_LOCK).exists()


def test_the_lock_is_made_where_a_fresh_home_has_no_store_directory_yet(tmp_path, monkeypatch):
    """Reproduced on a fresh LEVAIN_HOME: the first scheduled run died on a missing directory before it read anything."""
    from levain.coordinator.store import CoordStore

    ran = {"feeds": {}, "delivery": {}}
    store = CoordStore(tmp_path / "fresh" / "consent")
    monkeypatch.setattr(deliver, "run", lambda store, queue_dir=None: ran)
    assert deliver.run_exclusive(store) is ran
    assert (store.directory / deliver.FEEDS_LOCK).exists()


def _pass_rows(band):
    return band.c.rows("SELECT feed, seq FROM review_marks WHERE feed = 'pass' ORDER BY seq")


def test_every_fault_free_run_on_an_existing_store_leaves_one_mark(band, cq):
    deliver.run(band.store)                    # a quiet pass on a store with nothing waiting
    assert len(_pass_rows(band)) == 1
    deliver.run(band.store)                    # still nothing new: a healthy idle scheduler still reads as having run
    assert len(_pass_rows(band)) == 2
    _file(cq, band)
    deliver.run(band.store)                    # delivers a SIGN pointer
    assert len(_pass_rows(band)) == 3

"""Row 156 L3 round 1: the head's rulings (C1-C6, X1-X7), each test reproducing its finding's scenario. Synthetic items;
scratch stores; no real ``claude``."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from levain.coordinator import launch, lease, ready, status_import, work_attach
from levain.coordinator.store import CoordError, CoordStore

SHA = "c" * 40


class _Clock:
    def __init__(self) -> None:
        self.now = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(hours=3)

    def __call__(self):
        return self.now

    def advance(self, **kw: float) -> None:
        self.now += timedelta(**kw)

    def iso(self, **delta: float) -> str:
        return (self.now + timedelta(**delta)).isoformat()


def _rows(board, sql):
    c = sqlite3.connect(board / "dispatch.db")
    try:
        return c.execute(sql).fetchall()
    finally:
        c.close()


def _q(board, rid, at, **kw):
    base = {"report_id": rid, "item_id": "levain:a", "worker": "seat-a", "worker_kind": "seat", "event": "attach",
            "at": at}
    work_attach.enqueue_report(board, **{**base, **kw})


@pytest.fixture
def clocked(tmp_path):
    """A board store on a test clock with items a and b, and the operator's take (its token) still held."""
    board = tmp_path / "board"
    board.mkdir(mode=0o700)
    clock = _Clock()
    store = CoordStore(board, clock=clock)
    token = store.coord_take()
    for item in ("levain:a", "levain:b"):
        ready.add(store, token, item, repo="levain", title=item, who="desk", why="seed", to="ready")
    return board, clock, store, token


def _free(store, token):
    store.coord_release(token, by="coordinator")


def _apply(board, clock, live, every=work_attach.PASS_INTERVAL_S):
    """One service pass on the test clock: claim, apply, release."""
    svc = CoordStore(board, role="service", clock=clock)
    with svc._write("service", None, create=False) as (conn, now):
        tok, held = lease.service_claim(conn, now, cause="test")
    assert held is None, held
    try:
        return work_attach.apply_reports(svc, tok, live_sessions=live, every=every)
    finally:
        with svc._write("service", None, create=False) as (conn, now):
            lease.service_release(conn, now, tok, cause="test done")


def _seen(board, clock, *sessions):
    svc = CoordStore(board, role="service", clock=clock)
    with svc._write("service", None, create=False) as (conn, now):
        work_attach._record_seen(conn, now, frozenset(sessions))


# ------------------------------------------------------------------------------------------------ C1
def test_c1_a_session_seen_one_and_a_half_passes_before_the_report_is_accepted(clocked):
    board, clock, store, token = clocked
    _free(store, token)
    _seen(board, clock, "sess-a")
    clock.advance(seconds=1.5 * work_attach.PASS_INTERVAL_S)       # a pass interval plus a slow pass's runtime
    _q(board, "r-att", clock.iso(), harness_session_id="sess-a")
    got = _apply(board, clock, frozenset())
    assert got.applied == ["r-att"] and got.refused == []


# ------------------------------------------------------------------------------------------------ C2 = X1
def test_c2_a_crashed_spawn_item_launch_no_longer_holds_its_item(tmp_path):
    board = tmp_path / "board"
    board.mkdir(mode=0o700)
    store = CoordStore(board)
    token = store.coord_take()
    for item in ("levain:a", "levain:b"):
        ready.add(store, token, item, repo="levain", title=item, who="desk", why="seed", to="ready")
    dead = store.launch_request(token, harness="claude", role="coordinator", attach_item="levain:a")
    alive = store.launch_request(token, harness="claude", role="coordinator", attach_item="levain:b")
    with store._write() as (conn, now):
        lease.end_launch(conn, now, dead.launch_id, "failed", "the spawn crashed")
    _free(store, token)
    now = datetime.now(timezone.utc).isoformat()
    _q(board, "r-a", now, worker="seat-n", harness_session_id="sess-n")
    _q(board, "r-b", now, worker="seat-m", harness_session_id="sess-m", item_id="levain:b")
    svc = CoordStore(board, role="service")
    with svc._write("service", None, create=False) as (conn, at):
        tok, _ = lease.service_claim(conn, at, cause="test")
    got = work_attach.apply_reports(svc, tok, live_sessions=frozenset({"sess-n", "sess-m"}))
    assert got.applied == ["r-a"]                                  # the failed launch's attach holds nothing
    assert got.refused == [("r-b", "held-by-other")]               # a pending one still holds its item
    assert alive.launch_id in _rows(board, "SELECT detail FROM report_refusals WHERE report_id = 'r-b'")[0][0]


def test_c2_a_stale_session_only_attach_no_longer_holds_and_a_live_one_still_does(clocked):
    board, clock, store, token = clocked
    _free(store, token)
    _q(board, "r-a", clock.iso(), worker="seat-a", harness_session_id="sess-a")
    _q(board, "r-c", clock.iso(), worker="seat-c", harness_session_id="sess-c", item_id="levain:b")
    assert _apply(board, clock, frozenset({"sess-a", "sess-c"})).applied == ["r-a", "r-c"]
    clock.advance(minutes=work_attach.DEFER_DEADLINE.total_seconds() / 60 + 1)   # sess-a unseen since; sess-c live
    _q(board, "r-b", clock.iso(), worker="seat-b", harness_session_id="sess-b")
    _q(board, "r-d", clock.iso(), worker="seat-d", harness_session_id="sess-d", item_id="levain:b")
    got = _apply(board, clock, frozenset({"sess-b", "sess-c", "sess-d"}))
    assert got.applied == ["r-b"] and got.refused == [("r-d", "held-by-other")]


# ------------------------------------------------------------------------------------------------ X2
def test_x2_a_watch_on_a_custom_board_dir_applies_that_boards_reports(tmp_path, monkeypatch):
    monkeypatch.setenv("LEVAIN_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(launch, "_harness", lambda: (None, {}))
    board = tmp_path / "my-board"
    board.mkdir(mode=0o700)
    store = CoordStore(board)
    token = store.coord_take()
    ready.add(store, token, "levain:a", repo="levain", title="a", who="desk", why="seed", to="ready")
    ln = store.launch_request(token, harness="claude", role="coordinator")
    _free(store, token)
    _q(board, "r-1", datetime.now(timezone.utc).isoformat(), worker=ln.launch_id, launch_id=ln.launch_id)
    lines = launch._apply_board_reports(store)
    assert any("applied 1" in line for line in lines), lines
    assert _rows(board, "SELECT report_id FROM worker_attach WHERE report_id IS NOT NULL") == [("r-1",)]
    assert not (tmp_path / "board").exists()


# ------------------------------------------------------------------------------------------------ X3
def test_x3_a_future_dated_report_is_refused(clocked):
    board, clock, store, token = clocked
    _free(store, token)
    _q(board, "r-future", clock.iso(minutes=10), harness_session_id="sess-a")
    got = _apply(board, clock, frozenset({"sess-a"}))
    assert got.refused == [("r-future", "malformed-report")] and got.applied == []


def test_x3_a_report_written_before_the_first_sighting_within_the_deadline_is_accepted(clocked):
    """A seat that attached while no watch ran is normal use: first sighting up to the evidence deadline after."""
    board, clock, store, token = clocked
    _free(store, token)
    _seen(board, clock, "sess-a")
    clock.advance(minutes=1)
    _q(board, "r-early", clock.iso(minutes=-20), harness_session_id="sess-a")
    got = _apply(board, clock, frozenset({"sess-a"}))
    assert got.applied == ["r-early"] and got.refused == []


def test_x3_a_report_older_than_the_deadline_before_the_first_sighting_is_refused(clocked):
    board, clock, store, token = clocked
    _free(store, token)
    _seen(board, clock, "sess-a")
    clock.advance(minutes=1)
    _q(board, "r-back", clock.iso(minutes=-45), harness_session_id="sess-a")
    _q(board, "r-now", clock.iso(), harness_session_id="sess-a")
    got = _apply(board, clock, frozenset({"sess-a"}))
    assert got.refused == [("r-back", "worker-not-a-launch")] and got.applied == ["r-now"]


def test_x3_the_boundary_is_exactly_the_deadline(clocked):
    board, clock, store, token = clocked
    _free(store, token)
    _seen(board, clock, "sess-a")
    first = clock.now
    d = work_attach.DEFER_DEADLINE
    _q(board, "r-edge", (first - d).isoformat(), harness_session_id="sess-a")
    _q(board, "r-over", (first - d - timedelta(seconds=1)).isoformat(), harness_session_id="sess-a",
       event="progress")
    got = _apply(board, clock, frozenset({"sess-a"}))
    assert got.applied == ["r-edge"] and got.refused == [("r-over", "worker-not-a-launch")]


# ------------------------------------------------------------------------------------------------ X4
def test_x4_a_report_resolving_to_a_launch_is_stored_as_a_launch_whatever_kind_it_names(clocked):
    board, clock, store, token = clocked
    ln = store.launch_request(token, harness="claude", role="coordinator")
    _free(store, token)
    _q(board, "r-1", clock.iso(), worker=ln.launch_id, launch_id=ln.launch_id, worker_kind="seat")
    assert _apply(board, clock, frozenset()).applied == ["r-1"]
    assert _rows(board, "SELECT worker_kind, launch_id FROM worker_attach WHERE report_id = 'r-1'") == [
        ("launch", ln.launch_id)]


def test_x4_a_session_workers_later_event_must_keep_its_attachs_kind(clocked):
    board, clock, store, token = clocked
    _free(store, token)
    _q(board, "r-att", clock.iso(), worker_kind="lane", harness_session_id="sess-a")
    _q(board, "r-seat", clock.iso(), worker_kind="seat", harness_session_id="sess-a", event="progress")
    _q(board, "r-lane", clock.iso(), worker_kind="lane", harness_session_id="sess-a", event="progress")
    got = _apply(board, clock, frozenset({"sess-a"}))
    assert got.applied == ["r-att", "r-lane"] and got.refused == [("r-seat", "kind-mismatch")]


# ------------------------------------------------------------------------------------------------ X5
def test_x5_a_status_file_that_is_not_utf8_is_a_refusal_naming_it(tmp_path, capsys):
    from levain.cli import main
    qdir = tmp_path / "q"
    qdir.mkdir(mode=0o700)
    store = CoordStore(qdir)
    token = store.coord_take()
    bad = tmp_path / "status.md"
    bad.write_bytes(b"## LANE MENU\n\n1. **row** \xff\xfe DONE: x.\n")
    with pytest.raises(CoordError, match="status.md"):
        status_import.import_status(store, token, bad)
    code = main(["coord", "import-status", str(bad), "--token", str(token), "--queue-dir", str(qdir)])
    cap = capsys.readouterr()
    assert code == 2 and "status.md" in cap.err and "Traceback" not in cap.err


# ------------------------------------------------------------------------------------------------ X6
def test_x6_watch_once_passes_its_every_and_every_is_checked_in_both_modes(tmp_path, monkeypatch, capsys):
    from levain.cli import main
    qdir = tmp_path / "q"
    qdir.mkdir(mode=0o700)
    CoordStore(qdir).coord_status()
    seen = []
    monkeypatch.setattr(launch, "migrate_setting", lambda store: [])
    monkeypatch.setattr(launch, "watch_once", lambda store, **kw: seen.append(kw) or [])
    assert main(["coord", "watch", "--once", "--every", "300", "--queue-dir", str(qdir)]) == 0
    assert seen == [{"every": 300.0}]
    assert main(["coord", "watch", "--once", "--every", "0", "--queue-dir", str(qdir)]) != 0
    assert len(seen) == 1 and "--every" in capsys.readouterr().err


# ------------------------------------------------------------------------------------------------ X7
def test_x7_a_pass_reads_only_the_reports_not_yet_applied_or_refused(clocked):
    board, clock, store, token = clocked
    ln = store.launch_request(token, harness="claude", role="coordinator")
    _free(store, token)
    _q(board, "r-1", clock.iso(), worker=ln.launch_id, launch_id=ln.launch_id)
    _q(board, "r-2", clock.iso(), worker=ln.launch_id, launch_id=ln.launch_id, event="progress")
    _q(board, "r-gone", clock.iso(), item_id="levain:nowhere", worker=ln.launch_id, launch_id=ln.launch_id)
    _q(board, "r-wait", clock.iso(), worker="seat-w", harness_session_id="sess-w", item_id="levain:b")
    q = sqlite3.connect(board / "reports.db")                  # a row the door refuses, refused as bad-seq-<seq>
    try:
        q.execute("INSERT INTO reports (report_id, item_id, worker, worker_kind, event, at) VALUES "
                  "('', 'levain:a', 'w', 'seat', 'attach', 'x')")
        q.commit()
    finally:
        q.close()
    got = _apply(board, clock, None)
    assert got.applied == ["r-1", "r-2"] and dict(got.refused) == {"r-gone": "unknown-item",
                                                                   "bad-seq-5": "malformed-report"}
    assert got.deferred == ["r-wait"]
    assert [r["report_id"] for r in work_attach._unresolved(board)] == ["r-wait"]
    again = _apply(board, clock, None)
    assert (again.applied, again.refused, again.deferred) == ([], [], ["r-wait"])
    assert work_attach.report_status(board, "r-1")["state"] == "applied"
    assert work_attach.report_status(board, "r-wait")["state"] == "queued"


# ------------------------------------------------------------------------------------------------ C4
def test_c4_sessions_long_unseen_are_pruned_and_one_a_waiting_report_names_is_kept(tmp_path):
    from levain.cockpit.consent import utc_stamp
    board = tmp_path / "board"
    board.mkdir(mode=0o700)
    store = CoordStore(board)
    token = store.coord_take()
    ready.add(store, token, "levain:a", repo="levain", title="a", who="desk", why="seed", to="ready")
    _free(store, token)
    now = datetime.now(timezone.utc)
    old = utc_stamp(now - timedelta(hours=2))
    c = sqlite3.connect(board / "dispatch.db")
    try:
        for sid, when in (("sess-old", old), ("sess-waiting", old),
                          ("sess-recent", utc_stamp(now - timedelta(minutes=10)))):
            c.execute("INSERT INTO sessions_seen (session_id, first_seen, last_seen) VALUES (?, ?, ?)", (sid, when, when))
        c.commit()
    finally:
        c.close()
    # the seat reported, then exited while no watch ran: its report is judged on the sighting, which is kept for it
    _q(board, "r-w", (now - timedelta(hours=2)).isoformat(), worker="seat-w", harness_session_id="sess-waiting")
    lines = work_attach.watch_apply(board, sessions=lambda _s: frozenset({"sess-new"}))
    assert any("applied 1" in line for line in lines), lines
    assert {r[0] for r in _rows(board, "SELECT session_id FROM sessions_seen")} == {"sess-recent", "sess-waiting",
                                                                                   "sess-new"}
    work_attach.watch_apply(board, sessions=lambda _s: frozenset({"sess-new"}))     # nothing waits on it now
    assert {r[0] for r in _rows(board, "SELECT session_id FROM sessions_seen")} == {"sess-recent", "sess-new"}


def test_c4_a_seen_session_still_cannot_be_deleted_while_recent(clocked):
    board, clock, store, token = clocked
    _seen(board, clock, "sess-a")
    c = sqlite3.connect(board / "dispatch.db")
    try:
        with pytest.raises(sqlite3.IntegrityError, match="seen session"):
            c.execute("DELETE FROM sessions_seen WHERE session_id = 'sess-a'")
    finally:
        c.close()


# ------------------------------------------------------------------------------------------------ C6
def test_c6_a_done_marker_inside_a_bracketed_note_is_not_the_done_line():
    text = ("## LANE MENU\n\n"
            "1. **One** (tier: sonnet) a lead [history: DONE: the wrong line.] DONE: the right line.\n"
            "2. **Two** (tier: sonnet) a lead [history: DONE: only in a note.]\n"
            # the real board's shape: DONE after the notes; a stray [ never closed swallows nothing
            "3. **Three** (tier: sonnet) a [stray bracket, then [a note [nested]: DONE: no.] DONE: kept.\n")
    rows, unparsed = status_import.parse_rows(text)
    assert unparsed == []
    assert [r.done_line for r in rows] == ["the right line.", None, "kept."]


# ------------------------------------------------------------------------------------------------ C3
def test_c3_a_report_naming_a_launch_from_another_session_is_refused(clocked):
    board, clock, store, token = clocked
    ln = store.launch_request(token, harness="claude", role="coordinator")
    assert store.launch_activate(ln.launch_id, harness_session_id="sess-L")[1] is None
    _free(store, token)
    _q(board, "r-spoof", clock.iso(), worker=ln.launch_id, launch_id=ln.launch_id, harness_session_id="sess-x")
    _q(board, "r-bare", clock.iso(), worker=ln.launch_id, launch_id=ln.launch_id)
    _q(board, "r-own", clock.iso(), worker=ln.launch_id, launch_id=ln.launch_id, harness_session_id="sess-L")
    got = _apply(board, clock, frozenset({"sess-x", "sess-L"}))
    assert dict(got.refused) == {"r-spoof": "worker-mismatch", "r-bare": "worker-mismatch"}
    assert got.applied == ["r-own"]

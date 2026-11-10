"""Row 156 step 2A: the worker-attach table, the report queue and the service applier. The ruled tests: a decide-class
write under the service role is refused; an unknown-item report and an unreal-worker report are each refused AND leave
a ``report_refusals`` row. Synthetic items; a scratch store."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from levain.desk import ready, work_attach
from levain.desk.store import CoordError, CoordStore
from levain.desk.legacy import ROLE_DESK

SHA = "a" * 40


@pytest.fixture
def board(tmp_path):
    d = tmp_path / "board"
    d.mkdir(mode=0o700)
    return d


@pytest.fixture
def coord(board):
    store = CoordStore(board)
    token = store.coord_take()
    ready.add(store, token, "levain:a", repo="levain", title="item a", who="desk", why="seed", to="ready")
    ln = store.launch_request(token, harness="claude", role=ROLE_DESK)
    return store, token, ln.launch_id


def _now(**ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(**ago)).isoformat()


def _report(board, rid, **kw):
    base = {"report_id": rid, "item_id": "levain:a", "worker": "wa2a", "worker_kind": "lane", "event": "attach",
            "at": _now()}
    work_attach.enqueue_report(board, **{**base, **kw})


def _svc(board, store, token):
    """The service's own claim on the board: the holder gives the lease back (its launches stay as they are) and the
    service claims it, as the watch does."""
    from levain.desk import lease
    store.coord_release(token, by=ROLE_DESK)
    svc = CoordStore(board, role="service")
    with svc._write("service", None, create=False) as (conn, now):
        got, held = lease.service_claim(conn, now, cause="test")
    assert held is None
    return svc, got


def _rows(board, sql):
    c = sqlite3.connect(board / "dispatch.db")
    try:
        return c.execute(sql).fetchall()
    finally:
        c.close()


# (a) THE ROLE SPLIT: the service records facts, a decide-class write under its role is refused
def test_service_role_is_refused_every_deciding_write(board, coord):
    store, token, _ = coord
    svc = CoordStore(board, role="service")
    for attempt in (lambda: ready.add(svc, token, "levain:b", repo="levain", title="b", who="svc", why="x"),
                    lambda: ready.move(svc, token, "levain:a", to="running", who="svc", why="x"),
                    lambda: ready.correct(svc, token, "levain:a", to="done", who="svc", why="x"),
                    lambda: svc.coord_claim("ln-none", "key")):
        with pytest.raises(CoordError, match="service records facts only"):
            attempt()
    assert [r[0] for r in _rows(board, "SELECT category FROM ready_items WHERE id = 'levain:a'")] == ["ready"]
    # and the schema keeps even a service-labelled row from moving an item, whoever writes it
    c = sqlite3.connect(board / "dispatch.db")
    try:
        with pytest.raises(sqlite3.IntegrityError, match="service records facts only"):
            c.execute("INSERT INTO ready_journal (id, ver, gseq, ts, iso, at, repo, from_status, to_status, "
                      "from_category, to_category, who, why, source, applied_by, lease_token) VALUES ('levain:a', 2, 9999, "
                      "0, 'x', 'x', 'levain', 'ready', 'done', 'ready', 'done', 'service', 'x', 'correction', NULL, 1)")
    finally:
        c.close()


# (b) THE VALIDATION: an unknown item and an unreal worker are each refused, each leaving a visible record
def test_invalid_reports_are_refused_and_leave_a_refusal_row(board, coord):
    store, token, launch_id = coord
    _report(board, "r-unknown", item_id="levain:nowhere", worker=launch_id, launch_id=launch_id)
    _report(board, "r-unreal", harness_session_id="not-listed")
    _report(board, "r-nobody")
    _report(board, "r-bad", event="promote", worker=launch_id, launch_id=launch_id)
    _report(board, "r-real", worker=launch_id, launch_id=launch_id)
    svc, stoken = _svc(board, store, token)
    got = work_attach.apply_reports(svc, stoken, live_sessions=frozenset({"other"}))
    assert got.applied == ["r-real"]
    assert dict(got.refused) == {"r-unknown": "unknown-item", "r-unreal": "worker-not-a-launch",
                                 "r-nobody": "worker-not-a-launch", "r-bad": "malformed-report"}
    refusals = dict(_rows(board, "SELECT report_id, reason FROM report_refusals"))
    assert refusals == dict(got.refused)
    assert _rows(board, "SELECT report_id, by FROM worker_attach") == [("r-real", "service")]


# (c) the new ready columns round-trip, and an OLD store (made before them) opens, migrates and keeps its rows
def test_item_facts_round_trip_and_an_old_store_migrates(tmp_path, monkeypatch):
    old_dir = tmp_path / "old"
    old_dir.mkdir(mode=0o700)
    with monkeypatch.context() as m:
        m.setattr(ready, "LIFECYCLE_SCHEMA", tuple(s.replace(ready.ITEM_FACTS_SQL, "") for s in ready.LIFECYCLE_SCHEMA
                                                   if "service_facts" not in s))
        m.setattr(work_attach, "WORK_SCHEMA", ())
        m.setattr(ready, "migrate_item_facts", lambda conn: None)       # the code before the migration existed
        old = CoordStore(old_dir)
        token = old.coord_take()
    c = sqlite3.connect(old_dir / "dispatch.db")                    # the one row the old writer made, as it wrote it
    try:
        g = c.execute("INSERT INTO gseq (at) VALUES ('x')").lastrowid
        c.execute("INSERT INTO ready_journal (id, ver, gseq, ts, iso, at, repo, from_status, to_status, from_category, "
                  "to_category, who, why, source, applied_by, lease_token) VALUES ('levain:old', 1, ?, 0, 'x', 'x', "
                  "'levain', NULL, 'backlog', NULL, 'backlog', 'desk', 'seed', 'add', NULL, ?)", (g, token))
        c.execute("INSERT INTO ready_items (id, ver, repo, title, status, category, tier, done_line, depends_on, rank, "
                  "set_by, set_at) VALUES ('levain:old', 1, 'levain', 'made before the columns', 'backlog', 'backlog', "
                  "NULL, NULL, '[]', 1, 'desk', 'x')")
        c.commit()
    finally:
        c.close()
    assert "hold" not in {r[1] for r in _rows(old_dir, "PRAGMA table_info(ready_items)")}
    store = CoordStore(old_dir)                                   # the new code opens it
    assert ready.item_facts(store, "levain:old") == dict.fromkeys(ready.FACT_COLS)
    ready.move(store, token, "levain:old", to="waiting", who="desk", why="held", hold="waits on Phill",
               residue="np-1", ref="STATUS_1_0.md#12@abc")
    ready.move(store, token, "levain:old", to="ready", who="desk", why="back")      # a move carries every fact
    assert ready.item_facts(store, "levain:old") == {"hold": "waits on Phill", "residue": "np-1",
                                                      "ref": "STATUS_1_0.md#12@abc", "landed_sha": None, "branch": None}
    assert [r[0] for r in _rows(old_dir, "SELECT name FROM sqlite_master WHERE name = 'worker_attach'")] == ["worker_attach"]
    # landed: the service sets the sha and branch (and nothing else); a bad sha is refused with a record
    with pytest.raises(CoordError, match="40 or 64 hex"):
        with store._write(ROLE_DESK, token, create=False) as (conn, now):
            ready.record_landed(conn, now, token, "levain:old", "abc", "main", by=ROLE_DESK, why="x")
    ln = store.launch_request(token, harness="claude", role=ROLE_DESK)
    assert store.launch_activate(ln.launch_id, harness_session_id="sess-L")[1] is None   # a landed needs it (R3-B)
    _report(old_dir, "r-att", item_id="levain:old", worker=ln.launch_id, launch_id=ln.launch_id,
            harness_session_id="sess-L")
    _report(old_dir, "r-land", item_id="levain:old", worker=ln.launch_id, launch_id=ln.launch_id, event="landed",
            sha=SHA, branch="seat/x", harness_session_id="sess-L")
    svc, stoken = _svc(old_dir, store, token)
    got = work_attach.apply_reports(svc, stoken, live_sessions=frozenset())
    assert got.applied == ["r-att", "r-land"] and got.refused == []
    facts = ready.item_facts(store, "levain:old")
    assert (facts["landed_sha"], facts["branch"], facts["hold"]) == (SHA, "seat/x", "waits on Phill")
    (cat,) = _rows(old_dir, "SELECT category FROM ready_items WHERE id = 'levain:old' ORDER BY ver DESC LIMIT 1")
    assert cat == ("ready",)                                       # landing moved nothing


# (d) apply_reports is idempotent on report_id; an unreadable listing defers a session-only report, never refuses it
def test_apply_is_idempotent_and_defers_on_an_unreadable_listing(board, coord):
    store, token, launch_id = coord
    assert store.launch_activate(launch_id, harness_session_id="sess-L")[1] is None     # a progress needs it (R3-B)
    _report(board, "r1", worker=launch_id, launch_id=launch_id, harness_session_id="sess-L")
    _report(board, "r2", event="progress", worker=launch_id, launch_id=launch_id, why="halfway",
            harness_session_id="sess-L")
    _report(board, "r-gone", item_id="levain:nowhere", worker=launch_id, launch_id=launch_id,
            harness_session_id="sess-L")
    _report(board, "r-sess", worker="other", harness_session_id="s-1", item_id="levain:b")
    ready.add(store, token, "levain:b", repo="levain", title="item b", who="desk", why="seed", to="ready")
    svc, token = _svc(board, store, token)
    first = work_attach.apply_reports(svc, token, live_sessions=None)
    assert first.applied == ["r1", "r2"] and first.refused == [("r-gone", "unknown-item")]
    assert first.deferred == ["r-sess"]
    counts = lambda: (_rows(board, "SELECT count(*) FROM worker_attach"), _rows(board, "SELECT count(*) FROM report_refusals"))  # noqa: E731
    before = counts()
    again = work_attach.apply_reports(svc, token, live_sessions=None)
    assert (again.applied, again.refused) == ([], []) and counts() == before
    # the listing comes back showing the session: the deferred report applies, once
    later = work_attach.apply_reports(svc, token, live_sessions=frozenset({"s-1"}))
    assert later.applied == ["r-sess"] and counts()[0] == [(before[0][0][0] + 1,)]
    assert [r[0] for r in _rows(board, f"SELECT kind FROM worker_attach WHERE worker = '{launch_id}' ORDER BY seq")] \
        == ["attach", "progress"]


# ------------------------------------------------------------------------------------------------ L1/L2 round 1 (r156fA)
def test_f1_the_service_role_refuses_by_what_a_write_does_not_by_its_label(board, coord):
    from levain.desk.store import Part
    store, token, _ = coord
    d = store.accept_drop("a thread on the board store", surface="cli")[0]
    t = store.split(d.drop_id, [Part("work", "x")], by="operator")[0]
    svc = CoordStore(board, role="service")
    with pytest.raises(CoordError):
        svc.set_state(t.thread_id, "closed", by="service")
    # the store's own close, labelled the service, through the service's own door: refused by what it writes
    with pytest.raises(CoordError, match="service records facts only"):
        with svc._write("service", None, create=False) as (conn, now):
            svc._close(conn, t.thread_id, "closed by a bookkeeping pass", "service", now)
    with pytest.raises(CoordError, match="service records facts only"):
        with svc._write("service", None, create=False) as (conn, now):
            conn.execute("DELETE FROM gseq")
    assert store.thread(t.thread_id).state == "open"


def test_f2_a_service_row_after_a_version_gap_is_refused(board, coord):
    store, token, _ = coord
    c = sqlite3.connect(board / "dispatch.db")
    try:
        c.execute("DROP TRIGGER ready_journal_next")      # a hand-edited store: the one way a version gap is built
        for ver in (2, 3):                       # the journal moves on twice; the item's version 2 is never written
            g = c.execute("INSERT INTO gseq (at) VALUES ('x')").lastrowid
            c.execute("INSERT INTO ready_journal (id, ver, gseq, ts, iso, at, repo, from_status, to_status, "
                      "from_category, to_category, who, why, source, applied_by, lease_token) VALUES ('levain:a', ?, ?, "
                      "0, 'x', 'x', 'levain', 'ready', 'ready', 'ready', 'ready', 'service', 'gap', 'correction', NULL, "
                      "?)", (ver, g, token))
        with pytest.raises(sqlite3.IntegrityError, match="service records facts only"):
            c.execute("INSERT INTO ready_items (id, ver, repo, title, status, category, tier, done_line, depends_on, "
                      "rank, set_by, set_at) VALUES ('levain:a', 3, 'levain', 'retitled past a gap', 'ready', 'ready', "
                      "NULL, NULL, '[]', 1, 'service', 'x')")
    finally:
        c.close()


def _refusals(board):
    return dict(_rows(board, "SELECT report_id, reason FROM report_refusals"))


def test_f3_a_second_workers_attach_is_refused_and_the_first_stays_current(board, coord):
    store, token, _ = coord
    _report(board, "r-a", worker="seat-a", harness_session_id="sess-a")
    _report(board, "r-b", worker="seat-b", harness_session_id="sess-b")
    svc, stoken = _svc(board, store, token)
    got = work_attach.apply_reports(svc, stoken, live_sessions=frozenset({"sess-a", "sess-b"}))
    assert got.applied == ["r-a"] and got.refused == [("r-b", "held-by-other")]
    (detail,) = _rows(board, "SELECT detail FROM report_refusals WHERE report_id = 'r-b'")[0]
    assert "seat-a" in detail
    assert _rows(board, "SELECT worker FROM worker_attach WHERE kind = 'attach'") == [("seat-a",)]


def test_f4_a_report_resolving_to_a_launch_must_name_that_launchs_worker(board, coord):
    store, token, launch_id = coord
    _report(board, "r-name", worker="1108-43", launch_id=launch_id)                 # a seat name over a launch id
    _report(board, "r-mine", worker=launch_id, launch_id=launch_id)
    svc, stoken = _svc(board, store, token)
    got = work_attach.apply_reports(svc, stoken, live_sessions=frozenset())
    assert got.refused == [("r-name", "worker-mismatch")] and got.applied == ["r-mine"]


def test_f4_a_seat_is_bound_to_the_session_of_its_first_attach(board, coord):
    store, token, _ = coord
    ready.add(store, token, "levain:b", repo="levain", title="item b", who="desk", why="seed", to="ready")
    _report(board, "r-att", worker="seat-a", harness_session_id="sess-a")
    _report(board, "r-forged", worker="seat-a", harness_session_id="sess-z", event="ended")   # another session, same name
    _report(board, "r-alias", worker="seat-q", harness_session_id="sess-a", item_id="levain:b")  # same session, new name
    _report(board, "r-own", worker="seat-a", harness_session_id="sess-a", event="progress")
    svc, stoken = _svc(board, store, token)
    got = work_attach.apply_reports(svc, stoken, live_sessions=frozenset({"sess-a", "sess-z"}))
    assert got.applied == ["r-att", "r-own"]
    assert dict(got.refused) == {"r-forged": "worker-mismatch", "r-alias": "worker-mismatch"}
    assert _refusals(board) == dict(got.refused)


def test_f4_the_session_binding_holds_only_while_the_workers_attach_is_open(board, coord):
    store, token, _ = coord
    svc, stoken = _svc(board, store, token)
    live = frozenset({"sess-1", "sess-2"})
    _report(board, "r-att1", worker="seat-w", harness_session_id="sess-1")
    _report(board, "r-forged", worker="seat-w", harness_session_id="sess-2", event="progress")   # attach open: refused
    got = work_attach.apply_reports(svc, stoken, live_sessions=live)
    assert got.applied == ["r-att1"] and dict(got.refused) == {"r-forged": "worker-mismatch"}
    _report(board, "r-end", worker="seat-w", harness_session_id="sess-1", event="ended")
    _report(board, "r-att2", worker="seat-w", harness_session_id="sess-2")                      # ended: a new session rebinds
    _report(board, "r-prog2", worker="seat-w", harness_session_id="sess-2", event="progress")
    got = work_attach.apply_reports(svc, stoken, live_sessions=live)
    assert got.applied == ["r-end", "r-att2", "r-prog2"] and got.refused == []


def test_l1_service_apply_needs_the_services_own_claim(board, coord):
    store, token, launch_id = coord
    _report(board, "r-1", worker=launch_id, launch_id=launch_id)
    with pytest.raises(CoordError, match="service claim"):
        work_attach.apply_reports(CoordStore(board, role="service"), token, live_sessions=frozenset())   # the take's
    assert _rows(board, "SELECT count(*) FROM worker_attach WHERE report_id IS NOT NULL") == [(0,)]


def test_l1_a_deferred_attach_holds_back_that_workers_later_launch_report(board, coord):
    store, token, launch_id = coord
    _report(board, "r-att", worker=launch_id, harness_session_id="sess-ln", launch_id="ln-" + "0" * 32)
    _report(board, "r-prog", worker=launch_id, launch_id=launch_id, event="progress")
    svc, stoken = _svc(board, store, token)
    # the attach rests on its session (the launch id it names is no launch here); the listing is unreadable
    got = work_attach.apply_reports(svc, stoken, live_sessions=None)
    assert got.deferred == ["r-att", "r-prog"] and got.refused == [] and got.applied == []


def test_l1_an_empty_report_id_is_refused_at_the_door_and_a_planted_one_never_wedges_the_queue(board, coord):
    store, token, launch_id = coord
    with pytest.raises(CoordError, match="report_id"):
        work_attach.enqueue_report(board, report_id="", item_id="levain:a", worker="w", worker_kind="seat",
                                   event="attach", at=_now())
    with pytest.raises(CoordError, match="report_id"):
        work_attach.queue_report(board, report_id="", item_id="levain:a", worker="w", worker_kind="seat",
                                 event="attach", at=_now())
    assert store.launch_activate(launch_id, harness_session_id="sess-L")[1] is None     # a progress needs it (R3-B)
    _report(board, "r-1", worker=launch_id, launch_id=launch_id, harness_session_id="sess-L")
    q = sqlite3.connect(board / "reports.db")                  # a seat-writable file: plant what the door refuses
    try:
        q.execute("INSERT INTO reports (report_id, item_id, worker, worker_kind, event, at) VALUES "
                  "('', 'levain:a', 'w', 'seat', 'attach', 'x')")
        q.execute("INSERT INTO reports (report_id, item_id, worker, worker_kind, event, at) VALUES "
                  "(?, 'levain:a', 'w', 'seat', 'attach', 'x')", ("z" * 150,))
        q.commit()
    finally:
        q.close()
    _report(board, "r-4", worker=launch_id, launch_id=launch_id, event="progress", harness_session_id="sess-L")
    svc, stoken = _svc(board, store, token)
    got = work_attach.apply_reports(svc, stoken, live_sessions=frozenset())
    assert got.applied == ["r-1", "r-4"] and got.faults == []
    assert [r for r, _ in got.refused] == ["bad-seq-2", "bad-seq-3"]
    assert set(_refusals(board).values()) == {"malformed-report"}
    again = work_attach.apply_reports(svc, stoken, live_sessions=frozenset())
    assert (again.applied, again.refused) == ([], [])


def test_l1_a_refusal_that_cannot_be_recorded_is_a_fault_and_the_pass_goes_on(board, coord, monkeypatch):
    store, token, launch_id = coord
    _report(board, "r-bad", item_id="levain:nowhere", worker=launch_id, launch_id=launch_id)
    _report(board, "r-ok", worker=launch_id, launch_id=launch_id)
    svc, stoken = _svc(board, store, token)
    real = work_attach._refuse

    def broken(conn, now, by, rid, *a):
        if rid == "r-bad":
            raise sqlite3.IntegrityError("the refusal record is unwritable")
        return real(conn, now, by, rid, *a)

    monkeypatch.setattr(work_attach, "_refuse", broken)
    got = work_attach.apply_reports(svc, stoken, live_sessions=frozenset())
    assert got.applied == ["r-ok"] and got.refused == [] and [r for r, _ in got.faults] == ["r-bad"]
    assert _rows(board, "SELECT count(*) FROM report_refusals") == [(0,)]


def test_l1_a_desk_writing_as_who_service_is_told_the_name_is_reserved(board, coord):
    store, token, _ = coord
    with pytest.raises((CoordError, sqlite3.IntegrityError), match="names the service alone"):
        ready.add(store, token, "levain:c", repo="levain", title="c", who="service", why="x")

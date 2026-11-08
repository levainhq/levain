"""Row 156 step 2A: the worker-attach table, the report queue and the service applier. The ruled tests: a decide-class
write under the service role is refused; an unknown-item report and an unreal-worker report are each refused AND leave
a ``report_refusals`` row. Synthetic items; a scratch store."""
from __future__ import annotations

import sqlite3

import pytest

from levain.coordinator import ready, work_attach
from levain.coordinator.store import CoordError, CoordStore

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
    ln = store.launch_request(token, harness="claude", role="coordinator")
    return store, token, ln.launch_id


def _report(board, rid, **kw):
    base = {"report_id": rid, "item_id": "levain:a", "worker": "wa2a", "worker_kind": "lane", "event": "attach",
            "at": "2026-11-08T12:00:00-05:00"}
    work_attach.enqueue_report(board, **{**base, **kw})


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
    _report(board, "r-unknown", item_id="levain:nowhere", launch_id=launch_id)
    _report(board, "r-unreal", harness_session_id="not-listed")
    _report(board, "r-nobody")
    _report(board, "r-bad", event="promote", launch_id=launch_id)
    _report(board, "r-real", launch_id=launch_id)
    got = work_attach.apply_reports(CoordStore(board, role="service"), token, live_sessions=frozenset({"other"}))
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
        with store._write("coordinator", token, create=False) as (conn, now):
            ready.record_landed(conn, now, token, "levain:old", "abc", "main", by="coordinator", why="x")
    ln = store.launch_request(token, harness="claude", role="coordinator")
    _report(old_dir, "r-att", item_id="levain:old", launch_id=ln.launch_id)
    _report(old_dir, "r-land", item_id="levain:old", launch_id=ln.launch_id, event="landed", sha=SHA, branch="seat/x")
    got = work_attach.apply_reports(CoordStore(old_dir, role="service"), token, live_sessions=frozenset())
    assert got.applied == ["r-att", "r-land"] and got.refused == []
    facts = ready.item_facts(store, "levain:old")
    assert (facts["landed_sha"], facts["branch"], facts["hold"]) == (SHA, "seat/x", "waits on Phill")
    (cat,) = _rows(old_dir, "SELECT category FROM ready_items WHERE id = 'levain:old' ORDER BY ver DESC LIMIT 1")
    assert cat == ("ready",)                                       # landing moved nothing


# (d) apply_reports is idempotent on report_id; an unreadable listing defers a session-only report, never refuses it
def test_apply_is_idempotent_and_defers_on_an_unreadable_listing(board, coord):
    store, token, launch_id = coord
    _report(board, "r1", launch_id=launch_id)
    _report(board, "r2", event="progress", launch_id=launch_id, why="halfway")
    _report(board, "r-gone", item_id="levain:nowhere", launch_id=launch_id)
    _report(board, "r-sess", worker="other", harness_session_id="s-1")
    svc = CoordStore(board, role="service")
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
    assert [r[0] for r in _rows(board, "SELECT kind FROM worker_attach WHERE worker = 'wa2a' ORDER BY seq")] == [
        "attach", "progress"]


# ------------------------------------------------------------------------------------------------ L1/L2 round 1 (r156fA)
def test_f1_the_service_role_refuses_by_what_a_write_does_not_by_its_label(board, coord):
    from levain.coordinator.store import Part
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

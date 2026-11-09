"""Row 156 L3 round 2: the head's rulings R3-A to R3-D, each test reproducing its finding's scenario. Synthetic items;
scratch stores; no real ``claude``."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from levain.cli import main
from levain.consent.queue import queue_dir_for
from levain.coordinator import launch, lease, ready, work_attach
from levain.coordinator.store import CoordError, CoordStore

SHA = "d" * 40


def _rows(directory, sql):
    c = sqlite3.connect(Path(directory) / "dispatch.db")
    try:
        return c.execute(sql).fetchall()
    finally:
        c.close()


def _files(directory: Path) -> dict[str, bytes]:
    """Every byte of a store's ``dispatch.db`` and of its write-ahead log, an absent log read as empty. The ``-shm``
    index is left out: a reader opening the database rewrites it, and it holds no row."""
    wal = directory / "dispatch.db-wal"
    return {"dispatch.db": (directory / "dispatch.db").read_bytes(),
            "dispatch.db-wal": wal.read_bytes() if wal.exists() else b""}


def _plant_queue(directory: Path, **report) -> None:
    """A ``reports.db`` written straight into ``directory``, the way a seat's own process could, around the door."""
    conn = sqlite3.connect(work_attach.reports_path(directory))
    try:
        for stmt in work_attach.REPORT_SCHEMA:
            conn.execute(stmt)
        cols = list(report)
        conn.execute(f"INSERT INTO reports ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                     [report[c] for c in cols])
        conn.commit()
    finally:
        conn.close()


@pytest.fixture
def live_home(tmp_path, monkeypatch):
    """A Levain home whose live consent store holds item a and a failed coordinator launch attached to it, lease
    released."""
    home = tmp_path / "home"
    monkeypatch.setenv("LEVAIN_HOME", str(home))
    monkeypatch.setattr(launch, "_harness", lambda: (None, {}))
    consent_dir = queue_dir_for({"LEVAIN_HOME": str(home)})
    consent = CoordStore(consent_dir)
    token = consent.coord_take()
    ready.add(consent, token, "levain:a", repo="levain", title="item a", who="desk", why="seed", to="ready")
    ln = consent.launch_request(token, harness="claude", role="coordinator", attach_item="levain:a")
    with consent._write() as (conn, now):           # over, so a watch pass has nothing of its own to do here
        lease.end_launch(conn, now, ln.launch_id, "failed", "test")
    consent.coord_release(token, by="coordinator")
    return home, consent_dir, consent, ln


# ------------------------------------------------------------------------------------------------ R3-A
def test_r3a_a_queue_planted_in_the_live_consent_store_is_never_applied_by_the_default_watch(live_home):
    home, consent_dir, consent, ln = live_home
    _plant_queue(consent_dir, report_id="r-planted", item_id="levain:a", worker=ln.launch_id, worker_kind="launch",
                 event="landed", launch_id=ln.launch_id, sha=SHA, branch="main",
                 at=datetime.now(timezone.utc).isoformat())
    before = _files(consent_dir)
    lines = launch.watch_once(consent)
    assert _files(consent_dir) == before, lines
    assert not (home / "board").exists()            # the home's board was not made either


def test_r3a_an_explicit_queue_dir_naming_the_live_consent_store_is_not_a_board(live_home):
    """``coord watch --queue-dir <levain home>/consent`` is the default watch spelled out: its board is the home's."""
    home, consent_dir, consent, ln = live_home
    _plant_queue(consent_dir, report_id="r-planted", item_id="levain:a", worker=ln.launch_id, worker_kind="launch",
                 event="attach", launch_id=ln.launch_id, at=datetime.now(timezone.utc).isoformat())
    before = _files(consent_dir)
    lines = launch._apply_board_reports(CoordStore(Path(str(consent_dir) + "/.")))
    assert _files(consent_dir) == before, lines
    assert work_attach.watch_board(consent_dir) == home.resolve() / "board"


def test_r3a_watch_apply_refuses_the_live_consent_store_as_its_board(live_home):
    _home, consent_dir, _consent, ln = live_home
    _plant_queue(consent_dir, report_id="r-planted", item_id="levain:a", worker=ln.launch_id, worker_kind="launch",
                 event="attach", launch_id=ln.launch_id, at=datetime.now(timezone.utc).isoformat())
    before = _files(consent_dir)
    lines = work_attach.watch_apply(consent_dir, sessions=lambda _s: frozenset())
    assert _files(consent_dir) == before
    assert len(lines) == 1 and "live consent store" in lines[0], lines


def test_r3a_the_default_watch_still_applies_the_homes_board(live_home):
    home, _consent_dir, consent, _ln = live_home
    board = home / "board"
    board.mkdir(mode=0o700)
    seed = CoordStore(board)
    token = seed.coord_take()
    ready.add(seed, token, "levain:a", repo="levain", title="item a", who="desk", why="seed", to="ready")
    ln = seed.launch_request(token, harness="claude", role="coordinator")
    seed.coord_release(token, by="coordinator")
    work_attach.enqueue_report(board, report_id="r-1", item_id="levain:a", worker=ln.launch_id, worker_kind="launch",
                               event="attach", launch_id=ln.launch_id, at=datetime.now(timezone.utc).isoformat())
    lines = launch.watch_once(consent)
    assert any("applied 1" in line for line in lines), lines
    assert _rows(board, "SELECT report_id FROM worker_attach WHERE report_id IS NOT NULL") == [("r-1",)]


def test_r3a_a_watch_over_another_directory_applies_only_that_directory(tmp_path, monkeypatch):
    """A directory the watch was started with that is not the live consent store is its board, and nothing beside it
    is opened: a scratch store's sibling ``board`` is not inferred."""
    monkeypatch.setenv("LEVAIN_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(launch, "_harness", lambda: (None, {}))
    sibling = tmp_path / "scratch" / "board"
    sibling.mkdir(parents=True, mode=0o700)
    seed = CoordStore(sibling)
    token = seed.coord_take()
    ready.add(seed, token, "levain:a", repo="levain", title="item a", who="desk", why="seed", to="ready")
    ln = seed.launch_request(token, harness="claude", role="coordinator")
    seed.coord_release(token, by="coordinator")
    work_attach.enqueue_report(sibling, report_id="r-1", item_id="levain:a", worker=ln.launch_id, worker_kind="launch",
                               event="attach", launch_id=ln.launch_id, at=datetime.now(timezone.utc).isoformat())
    scratch = CoordStore(tmp_path / "scratch" / "consent")
    scratch.coord_status()
    launch.watch_once(scratch)
    assert _rows(sibling, "SELECT count(*) FROM worker_attach") == [(0,)]
    assert work_attach.watch_board(tmp_path / "scratch" / "consent") == (tmp_path / "scratch" / "consent").resolve()


def test_r3a_report_refuses_to_queue_inside_the_live_consent_store(live_home, capsys):
    _home, consent_dir, _consent, ln = live_home
    with pytest.raises(CoordError, match="live consent store"):
        work_attach.enqueue_report(consent_dir, report_id="r-x", item_id="levain:a", worker=ln.launch_id,
                                   worker_kind="launch", event="attach", at=datetime.now(timezone.utc).isoformat())
    assert not work_attach.reports_path(consent_dir).exists()
    code = main(["coord", "report", "levain:a", "--worker", ln.launch_id, "--kind", "launch", "--event", "attach",
                 "--launch", ln.launch_id, "--board-store", str(consent_dir) + "/../consent"])
    cap = capsys.readouterr()
    assert code == 2 and cap.out == "" and "live consent store" in cap.err
    assert not work_attach.reports_path(consent_dir).exists()


# ------------------------------------------------------------------------------------------------ R3-B
@pytest.fixture
def board(tmp_path):
    d = tmp_path / "board"
    d.mkdir(mode=0o700)
    store = CoordStore(d)
    token = store.coord_take()
    ready.add(store, token, "levain:a", repo="levain", title="a", who="desk", why="seed", to="ready")
    return d, store, token


def _apply(d, live):
    svc = CoordStore(d, role="service")
    with svc._write("service", None, create=False) as (conn, now):
        tok, held = lease.service_claim(conn, now, cause="test")
    assert held is None, held
    try:
        return work_attach.apply_reports(svc, tok, live_sessions=live)
    finally:
        with svc._write("service", None, create=False) as (conn, now):
            lease.service_release(conn, now, tok, cause="test done")


def _q(d, rid, **kw):
    base = {"report_id": rid, "item_id": "levain:a", "worker_kind": "launch",
            "at": datetime.now(timezone.utc).isoformat()}
    work_attach.enqueue_report(d, **{**base, **kw})


@pytest.mark.parametrize("state", ["pending", "failed", "expired"])
def test_r3b_a_landed_report_naming_a_launch_that_is_not_active_is_refused(board, state):
    d, store, token = board
    ln = store.launch_request(token, harness="claude", role="coordinator", attach_item="levain:a")
    if state != "pending":
        with store._write() as (conn, now):
            lease.end_launch(conn, now, ln.launch_id, state, "test")
    store.coord_release(token, by="coordinator")
    _q(d, "r-land", worker=ln.launch_id, launch_id=ln.launch_id, event="landed", sha=SHA, branch="main")
    got = _apply(d, frozenset())
    assert got.applied == [] and [rid for rid, _why in got.refused] == ["r-land"]
    assert _rows(d, "SELECT landed_sha FROM ready_items WHERE id = 'levain:a' ORDER BY ver DESC LIMIT 1") == [(None,)]


@pytest.mark.parametrize("event", ["progress", "blocked", "ended"])
def test_r3b_every_non_attach_event_from_a_pending_launch_is_refused(board, event):
    d, store, token = board
    ln = store.launch_request(token, harness="claude", role="coordinator", attach_item="levain:a")
    store.coord_release(token, by="coordinator")
    extra = {"why": "x"} if event == "blocked" else {}
    _q(d, "r-ev", worker=ln.launch_id, launch_id=ln.launch_id, event=event, **extra)
    got = _apply(d, frozenset())
    assert got.applied == [] and [rid for rid, _why in got.refused] == ["r-ev"]


def test_r3b_an_active_launch_reporting_from_its_own_session_still_lands_and_a_pending_attach_still_applies(board):
    d, store, token = board
    ready.add(store, token, "levain:b", repo="levain", title="b", who="desk", why="seed", to="ready")
    ln = store.launch_request(token, harness="claude", role="coordinator", attach_item="levain:a")
    assert store.launch_activate(ln.launch_id, harness_session_id="sess-L")[1] is None
    other = store.launch_request(token, harness="claude", role="coordinator")
    store.coord_release(token, by="coordinator")
    _q(d, "r-land", worker=ln.launch_id, launch_id=ln.launch_id, event="landed", sha=SHA, branch="main",
       harness_session_id="sess-L")
    _q(d, "r-att", worker=other.launch_id, launch_id=other.launch_id, event="attach", item_id="levain:b")
    got = _apply(d, frozenset({"sess-L"}))
    assert got.refused == [] and got.applied == ["r-land", "r-att"]
    assert _rows(d, "SELECT landed_sha FROM ready_items WHERE id = 'levain:a' ORDER BY ver DESC LIMIT 1") == [(SHA,)]


def _active_launch(store, token, sess="sess-L"):
    ln = store.launch_request(token, harness="claude", role="coordinator", attach_item="levain:a")
    assert store.launch_activate(ln.launch_id, harness_session_id=sess)[1] is None
    return ln


def test_r3b_rerule_a_final_landed_is_accepted_when_the_pass_ended_its_launch_first(board):
    d, store, token = board
    ln = _active_launch(store, token)
    store.coord_release(token, by="coordinator")
    with store._write() as (conn, now):
        lease.end_launch(conn, now, ln.launch_id, "ended", "test")
    _q(d, "r-land", worker=ln.launch_id, launch_id=ln.launch_id, event="landed", sha=SHA, branch="main",
       harness_session_id="sess-L")
    got = _apply(d, frozenset())
    assert got.refused == [] and got.applied == ["r-land"]
    assert _rows(d, "SELECT landed_sha FROM ready_items WHERE id = 'levain:a' ORDER BY ver DESC LIMIT 1") == [(SHA,)]


def test_r3b_rerule_a_pending_launch_with_no_session_refuses_landed(board):
    d, store, token = board
    ln = store.launch_request(token, harness="claude", role="coordinator", attach_item="levain:a")
    store.coord_release(token, by="coordinator")
    _q(d, "r-land", worker=ln.launch_id, launch_id=ln.launch_id, event="landed", sha=SHA, branch="main",
       harness_session_id="sess-X")
    got = _apply(d, frozenset({"sess-X"}))
    assert got.applied == [] and [rid for rid, _why in got.refused] == ["r-land"]


def test_r3b_rerule_an_ended_launch_refuses_a_report_from_a_different_session(board):
    d, store, token = board
    ln = _active_launch(store, token)
    store.coord_release(token, by="coordinator")
    with store._write() as (conn, now):
        lease.end_launch(conn, now, ln.launch_id, "ended", "test")
    _q(d, "r-land", worker=ln.launch_id, launch_id=ln.launch_id, event="landed", sha=SHA, branch="main",
       harness_session_id="sess-OTHER")
    got = _apply(d, frozenset({"sess-OTHER"}))
    assert got.applied == [] and [rid for rid, _why in got.refused] == ["r-land"]


# ------------------------------------------------------------------------------------------------ R3-C
def test_r3c_import_status_without_a_token_is_a_usage_error(tmp_path, capsys):
    status = tmp_path / "status.md"
    status.write_text("# head\n")
    with pytest.raises(SystemExit) as got:
        main(["coord", "import-status", str(status), "--queue-dir", str(tmp_path / "b")])
    assert got.value.code == 2
    assert "--token" in capsys.readouterr().err
    assert not (tmp_path / "b").exists()

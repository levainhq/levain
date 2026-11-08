"""Row 156 step 2D: ``levain coord watch`` applies queued reports as the ``service`` (facts only). Synthetic items;
scratch stores; no real ``claude``."""
from __future__ import annotations

import sqlite3

import pytest

from levain.coordinator import launch, ready, work_attach
from levain.coordinator.store import CoordError, CoordStore


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
    base = {"report_id": rid, "item_id": "levain:a", "worker": "wa2d", "worker_kind": "lane",
            "event": "attach", "at": "2026-11-08T12:00:00-05:00"}
    work_attach.enqueue_report(board, **{**base, **kw})


def _rows(board, sql):
    c = sqlite3.connect(board / "dispatch.db")
    try:
        return c.execute(sql).fetchall()
    finally:
        c.close()


def _none_live(_store):
    return frozenset()


def test_a_queued_attach_is_applied_by_one_pass_and_the_lease_is_given_back(board, coord):
    store, token, launch_id = coord
    store.coord_release()                                  # no coordinator holds the board now
    _report(board, "r-1", launch_id=launch_id)
    lines = work_attach.watch_apply(board, sessions=_none_live)
    assert [r[0] for r in _rows(board, "SELECT report_id FROM worker_attach")] == ["r-1"]
    assert _rows(board, "SELECT by FROM worker_attach") == [("service",)]
    assert any("applied 1" in ln for ln in lines)
    assert store.coord_lease() is None                      # claimed for the pass, released after it
    assert _rows(board, "SELECT holder, by FROM lease_events WHERE kind = 'claim' ORDER BY seq DESC LIMIT 1") \
        == [("service", "service")]
    assert work_attach.watch_apply(board, sessions=_none_live) == []        # nothing left unapplied: no lease taken
    assert _rows(board, "SELECT count(*) FROM lease_events WHERE holder = 'service'") == [(2,)]


def test_a_live_coordinator_lease_makes_the_pass_skip_and_say_why(board, coord):
    store, token, launch_id = coord                          # the take above is live
    _report(board, "r-1", launch_id=launch_id)
    lines = work_attach.watch_apply(board, sessions=_none_live)
    assert len(lines) == 1 and "held" in lines[0] and "1 report" in lines[0]
    assert _rows(board, "SELECT count(*) FROM worker_attach") == [(0,)]
    assert store.coord_lease().token == token and store.coord_lease().holder == "operator"
    assert _rows(board, "SELECT count(*) FROM lease_events WHERE holder = 'service'") == [(0,)]


def test_an_unknown_item_is_refused_with_a_visible_record(board, coord):
    store, token, launch_id = coord
    store.coord_release()
    _report(board, "r-bad", item_id="levain:nowhere", launch_id=launch_id)
    lines = work_attach.watch_apply(board, sessions=_none_live)
    assert _rows(board, "SELECT count(*) FROM worker_attach") == [(0,)]
    assert [r[0] for r in _rows(board, "SELECT report_id FROM report_refusals")] == ["r-bad"]
    assert any("refused 1" in ln for ln in lines)


def test_a_session_listing_that_cannot_be_read_defers_and_still_gives_the_lease_back(board, coord):
    store, token, _ = coord
    store.coord_release()
    _report(board, "r-1", harness_session_id="sess-1")
    lines = work_attach.watch_apply(board, sessions=lambda _s: None)
    assert _rows(board, "SELECT count(*) FROM worker_attach") == [(0,)]
    assert _rows(board, "SELECT count(*) FROM report_refusals") == [(0,)]
    assert any("deferred 1" in ln for ln in lines) and store.coord_lease() is None


def test_the_service_role_refuses_a_deciding_write(board, coord):
    store, token, _ = coord
    svc = CoordStore(board, role="service")
    with pytest.raises(CoordError, match="service records facts only"):
        ready.move(svc, token, "levain:a", to="running", who="service", why="x")
    assert [r[0] for r in _rows(board, "SELECT category FROM ready_items WHERE id = 'levain:a'")] == ["ready"]


def test_watch_once_applies_beside_its_own_work_and_is_unchanged_without_reports(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("LEVAIN_HOME", str(home))
    monkeypatch.setattr(launch, "_harness", lambda: (None, {}))
    consent = CoordStore(home / "consent")
    before = launch.watch_once(consent)
    assert not (home / "board").exists()                    # nothing queued: no board store is made or opened
    board = home / "board"
    board.mkdir(mode=0o700)
    seed = CoordStore(board)
    token = seed.coord_take()
    ready.add(seed, token, "levain:a", repo="levain", title="item a", who="desk", why="seed", to="ready")
    ln = seed.launch_request(token, harness="claude", role="coordinator")
    seed.coord_release()
    _report(board, "r-1", launch_id=ln.launch_id)
    after = launch.watch_once(consent)
    assert after[:len(before)] == before and any("applied 1" in line for line in after[len(before):])
    assert [r[0] for r in _rows(board, "SELECT report_id FROM worker_attach")] == ["r-1"]

"""Row 162: a coordinator holding the board lease applies the queued reports itself, as the coordinator, facts only,
through the validator the watch's service pass uses. Synthetic items; scratch stores; no real ``claude``."""
from __future__ import annotations

import shutil
import sqlite3
from datetime import datetime, timezone

import pytest

from levain.cli import main
from levain.coordinator import ready, work_attach
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
    base = {"report_id": rid, "item_id": "levain:a", "worker": kw.get("launch_id") or "w162", "worker_kind": "lane",
            "event": "attach", "at": datetime.now(timezone.utc).isoformat()}
    work_attach.enqueue_report(board, **{**base, **kw})


def _rows(board, sql):
    c = sqlite3.connect(board / "dispatch.db")
    try:
        return c.execute(sql).fetchall()
    finally:
        c.close()


def _none_live(_store):
    return frozenset()


def test_the_watch_starves_while_a_coordinator_holds_the_lease_and_the_coordinator_pass_applies(board, coord):
    store, token, launch_id = coord
    _report(board, "r-1", launch_id=launch_id)
    for _ in range(3):                                     # the starvation: pass after pass applies nothing
        lines = work_attach.watch_apply(board, sessions=_none_live)
        assert len(lines) == 1 and "applied nothing" in lines[0]
    assert _rows(board, "SELECT count(*) FROM worker_attach") == [(0,)]
    lines = work_attach.coordinator_apply(CoordStore(board), token, sessions=_none_live)
    assert any("applied 1" in ln for ln in lines)
    assert _rows(board, "SELECT report_id, by FROM worker_attach") == [("r-1", "coordinator")]
    assert store.coord_lease().token == token and store.coord_lease().live       # the coordinator keeps its lease
    assert work_attach.report_status(board, "r-1")["state"] == "applied"
    assert work_attach.coordinator_apply(CoordStore(board), token, sessions=_none_live) == []    # nothing left


def test_the_same_validator_refuses_the_same_way_as_the_service(tmp_path, board, coord):
    store, token, launch_id = coord
    # a landed report from a launch that never recorded a session: refused, whoever applies
    _report(board, "r-spoof", launch_id=launch_id, event="landed", sha="a" * 40, branch="seat/x")
    work_attach.coordinator_apply(CoordStore(board), token, sessions=_none_live)
    by_coordinator = _rows(board, "SELECT reason FROM report_refusals WHERE report_id = 'r-spoof'")
    assert by_coordinator and not _rows(board, "SELECT 1 FROM worker_attach")

    other = tmp_path / "other"
    other.mkdir(mode=0o700)
    s2 = CoordStore(other)
    t2 = s2.coord_take()
    ready.add(s2, t2, "levain:a", repo="levain", title="item a", who="desk", why="seed", to="ready")
    ln2 = s2.launch_request(t2, harness="claude", role="coordinator")
    s2.coord_release(t2, by="coordinator")
    _report(other, "r-spoof", launch_id=ln2.launch_id, event="landed", sha="a" * 40, branch="seat/x")
    work_attach.watch_apply(other, sessions=_none_live)
    assert _rows(other, "SELECT reason FROM report_refusals WHERE report_id = 'r-spoof'") == by_coordinator
    assert _rows(board, "SELECT landed_sha FROM ready_items WHERE id = 'levain:a'") == [(None,)]


def test_a_landed_report_of_a_launch_with_no_session_is_refused_by_the_coordinator_pass_too(board, coord):
    store, token, launch_id = coord
    _report(board, "r-att", launch_id=launch_id)
    work_attach.coordinator_apply(CoordStore(board), token, sessions=_none_live)
    before = _rows(board, "SELECT category FROM ready_items WHERE id = 'levain:a' ORDER BY ver DESC LIMIT 1")
    _report(board, "r-land", launch_id=launch_id, event="landed", sha="b" * 40, branch="seat/x",
            harness_session_id="s-162")
    work_attach.coordinator_apply(CoordStore(board), token, sessions=_none_live)
    assert _rows(board, "SELECT report_id FROM worker_attach ORDER BY gseq") == [("r-att",)]
    assert _rows(board, "SELECT category FROM ready_items WHERE id = 'levain:a' ORDER BY ver DESC LIMIT 1") == before


def test_a_wrong_or_lapsed_token_applies_nothing(board, coord):
    store, token, launch_id = coord
    _report(board, "r-1", launch_id=launch_id)
    with pytest.raises(CoordError):
        work_attach.coordinator_apply(CoordStore(board), token + 1, sessions=_none_live)
    store.coord_release(token, by="coordinator")
    with pytest.raises(CoordError):
        work_attach.coordinator_apply(CoordStore(board), token, sessions=_none_live)
    assert _rows(board, "SELECT count(*) FROM worker_attach") == [(0,)]
    assert _rows(board, "SELECT count(*) FROM report_refusals") == [(0,)]


def test_the_live_consent_store_is_never_a_board_to_the_coordinator_pass(tmp_path, monkeypatch):
    monkeypatch.setenv("LEVAIN_HOME", str(tmp_path / "home"))
    consent = work_attach.live_consent_dir()
    consent.mkdir(parents=True, mode=0o700)
    store = CoordStore(consent)
    token = store.coord_take()
    scratch = tmp_path / "scratch"
    scratch.mkdir(mode=0o700)
    _report(scratch, "r-1")
    shutil.copy(scratch / "reports.db", consent / "reports.db")            # planted: the queue door refuses it
    lines = work_attach.coordinator_apply(store, token, sessions=_none_live)
    assert len(lines) == 1 and "live consent store" in lines[0]
    assert _rows(consent, "SELECT count(*) FROM worker_attach") == [(0,)]


def test_the_verb_applies_with_the_token_and_refuses_without_the_lease(board, coord, monkeypatch, capsys):
    store, token, launch_id = coord
    monkeypatch.setattr(work_attach, "live_session_ids", _none_live)
    _report(board, "r-1", launch_id=launch_id)
    code = main(["coord", "apply-reports", "--token", str(token + 1), "--queue-dir", str(board)])
    assert code == 1 and _rows(board, "SELECT count(*) FROM worker_attach") == [(0,)]
    capsys.readouterr()
    code = main(["coord", "apply-reports", "--token", str(token), "--queue-dir", str(board)])
    out = capsys.readouterr().out
    assert code == 0 and "applied 1" in out
    assert _rows(board, "SELECT by FROM worker_attach") == [("coordinator",)]


def test_import_status_applies_the_queue_in_the_same_pass(board, coord, tmp_path, monkeypatch, capsys):
    store, token, launch_id = coord
    monkeypatch.setattr(work_attach, "live_session_ids", _none_live)
    _report(board, "r-1", launch_id=launch_id)
    status = tmp_path / "status.md"
    status.write_text("# head\n\n## LANE MENU\n\n1. **First item** (tier: sonnet) DONE: it works.\n")
    code = main(["coord", "import-status", str(status), "--token", str(token), "--queue-dir", str(board)])
    assert code == 0 and "applied 1" in capsys.readouterr().out
    assert _rows(board, "SELECT report_id, by FROM worker_attach") == [("r-1", "coordinator")]

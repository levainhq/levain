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
    by_coordinator = _rows(board, "SELECT reason, by FROM report_refusals WHERE report_id = 'r-spoof'")
    assert by_coordinator == [("worker-not-a-launch", "coordinator")] and not _rows(board, "SELECT 1 FROM worker_attach")

    other = tmp_path / "other"
    other.mkdir(mode=0o700)
    s2 = CoordStore(other)
    t2 = s2.coord_take()
    ready.add(s2, t2, "levain:a", repo="levain", title="item a", who="desk", why="seed", to="ready")
    ln2 = s2.launch_request(t2, harness="claude", role="coordinator")
    s2.coord_release(t2, by="coordinator")
    _report(other, "r-spoof", launch_id=ln2.launch_id, event="landed", sha="a" * 40, branch="seat/x")
    work_attach.watch_apply(other, sessions=_none_live)
    assert _rows(other, "SELECT reason FROM report_refusals WHERE report_id = 'r-spoof'") == [(by_coordinator[0][0],)]
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
    assert _rows(board, "SELECT reason, by FROM report_refusals WHERE report_id = 'r-land'") == [
        ("worker-not-a-launch", "coordinator")]
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


# ------------------------------------------------------------------------------------------------ row 162 L3 r1
def _live(*sids):
    return lambda _store: frozenset(sids)


def _seat(board, rid, event="attach", **kw):
    _report(board, rid, worker="seat-a", worker_kind="seat", event=event, harness_session_id="sess-a", **kw)


def _land_after_exit(board, apply):
    """A flow seat attaches while its session is live, lands, and exits before the next pass."""
    _seat(board, "r-att")
    apply(_live("sess-a"))
    _seat(board, "r-land", event="landed", sha="c" * 40, branch="seat/a")
    apply(_none_live)


def test_a_coordinator_pass_records_its_sightings_so_a_landing_after_exit_is_kept(tmp_path, board, coord):
    store, token, _ = coord
    _land_after_exit(board, lambda live: work_attach.coordinator_apply(CoordStore(board), token, sessions=live))
    assert _rows(board, "SELECT report_id, by FROM worker_attach ORDER BY gseq") == [
        ("r-att", "coordinator"), ("r-land", "coordinator")]
    assert _rows(board, "SELECT count(*) FROM report_refusals") == [(0,)]
    assert _rows(board, "SELECT landed_sha FROM ready_items WHERE id = 'levain:a' ORDER BY ver DESC LIMIT 1") == [
        ("c" * 40,)]
    assert _rows(board, "SELECT session_id FROM sessions_seen") == [("sess-a",)]

    other = tmp_path / "other"                             # the service's pass, same sequence, same verdict
    other.mkdir(mode=0o700)
    s2 = CoordStore(other)
    t2 = s2.coord_take()
    ready.add(s2, t2, "levain:a", repo="levain", title="item a", who="desk", why="seed", to="ready")
    s2.coord_release(t2, by="coordinator")
    _land_after_exit(other, lambda live: work_attach.watch_apply(other, sessions=live))
    assert _rows(other, "SELECT report_id, by FROM worker_attach ORDER BY gseq") == [
        ("r-att", "service"), ("r-land", "service")]
    assert _rows(other, "SELECT count(*) FROM report_refusals") == [(0,)]


def test_a_landed_report_with_its_session_live_is_applied_by_the_coordinator_facts_only(board, coord):
    store, token, _ = coord
    before = _rows(board, "SELECT category FROM ready_items WHERE id = 'levain:a' ORDER BY ver DESC LIMIT 1")
    _seat(board, "r-att")
    _seat(board, "r-land", event="landed", sha="d" * 40, branch="seat/a")
    lines = work_attach.coordinator_apply(CoordStore(board), token, sessions=_live("sess-a"))
    assert lines == ["board reports: applied 2, refused 0, deferred 0"]
    assert _rows(board, "SELECT kind, by FROM worker_attach ORDER BY gseq") == [
        ("attach", "coordinator"), ("landed", "coordinator")]
    assert _rows(board, "SELECT category, landed_sha, branch FROM ready_items WHERE id = 'levain:a' ORDER BY ver DESC "
                        "LIMIT 1") == [(before[0][0], "d" * 40, "seat/a")]


def test_a_wrong_token_on_an_empty_queue_is_refused(board, coord):
    store, token, _ = coord
    with pytest.raises(CoordError):                        # no reports.db yet
        work_attach.coordinator_apply(CoordStore(board), token + 1, sessions=_none_live)
    _report(board, "r-1", launch_id=coord[2])
    work_attach.coordinator_apply(CoordStore(board), token, sessions=_none_live)
    with pytest.raises(CoordError):                        # a queue with nothing left in it
        work_attach.coordinator_apply(CoordStore(board), token + 1, sessions=_none_live)
    store.coord_release(token, by="coordinator")
    with pytest.raises(CoordError):
        work_attach.coordinator_apply(CoordStore(board), token, sessions=_none_live)


def test_the_verb_exits_2_on_a_fault_and_on_the_live_consent_store(board, coord, tmp_path, monkeypatch, capsys):
    store, token, _ = coord

    def boom(*_a, **_k):
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(work_attach, "coordinator_apply", boom)
    code = main(["coord", "apply-reports", "--token", str(token), "--queue-dir", str(board)])
    err = capsys.readouterr().err
    assert code == 2 and err.count("\n") == 1 and "disk on fire" in err and "Traceback" not in err
    monkeypatch.undo()

    monkeypatch.setenv("LEVAIN_HOME", str(tmp_path / "home"))
    consent = work_attach.live_consent_dir()
    consent.mkdir(parents=True, mode=0o700)
    t = CoordStore(consent).coord_take()
    scratch = tmp_path / "scratch"
    scratch.mkdir(mode=0o700)
    _report(scratch, "r-1")
    shutil.copy(scratch / "reports.db", consent / "reports.db")
    code = main(["coord", "apply-reports", "--token", str(t), "--queue-dir", str(consent)])
    assert code == 2 and "live consent store" in capsys.readouterr().err


def test_import_status_applies_nothing_and_says_nothing_for_a_caller_not_holding_the_lease(board, coord, monkeypatch):
    from argparse import Namespace

    from levain.coordinator import cli

    store, token, launch_id = coord
    monkeypatch.setattr(work_attach, "live_session_ids", _none_live)
    _report(board, "r-1", launch_id=launch_id)
    assert cli._apply_queue(Namespace(token=token + 1), CoordStore(board)) == []
    assert cli._apply_queue(Namespace(token=None), CoordStore(board)) == []
    assert cli._apply_queue(Namespace(), CoordStore(board)) == []
    store.coord_release(token, by="coordinator")
    assert cli._apply_queue(Namespace(token=token), CoordStore(board)) == []
    assert _rows(board, "SELECT count(*) FROM worker_attach") == [(0,)]


def test_import_status_json_always_carries_the_reports_key(board, coord, tmp_path, monkeypatch, capsys):
    import json

    store, token, _ = coord
    monkeypatch.setattr(work_attach, "live_session_ids", _none_live)
    status = tmp_path / "status.md"
    status.write_text("# head\n\n## LANE MENU\n\n1. **First item** (tier: sonnet) DONE: it works.\n")
    code = main(["coord", "import-status", str(status), "--token", str(token), "--queue-dir", str(board), "--json"])
    assert code == 0 and json.loads(capsys.readouterr().out)["reports"] == []

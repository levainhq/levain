"""Row 156 step 2B: ``levain coord report`` (the seat's queue door) and ``coord spawn --item`` (the attach rides the
spawn's own transaction). Synthetic items; scratch stores; no real ``claude``."""
from __future__ import annotations

import json
import sqlite3

import pytest

from levain.agents import instantiate as inst
from levain.cli import main
from levain.coordinator import ready, work_attach
from levain.coordinator.store import CoordStore
from tests.test_agents_spawn import a_thread, registry, role  # noqa: F401


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


def _rows(board, sql):
    c = sqlite3.connect(board / "dispatch.db")
    try:
        return c.execute(sql).fetchall()
    finally:
        c.close()


def _svc(board, store, token):
    """The service's own claim on the board, as the watch takes it: the holder gives the lease back first."""
    from levain.coordinator import lease
    store.coord_release(token, by="coordinator")
    svc = CoordStore(board, role="service")
    with svc._write("service", None, create=False) as (conn, now):
        got, held = lease.service_claim(conn, now, cause="test")
    assert held is None
    return svc, got


def _report(board, capsys, *argv):
    code = main(["coord", "report", *argv, "--board-store", str(board), "--json"])
    cap = capsys.readouterr()
    return code, cap.out, cap.err


def test_report_queues_and_a_resend_says_so(board, coord, capsys, monkeypatch):
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    store, token, launch_id = coord
    args = ("levain:a", "--worker", launch_id, "--event", "attach", "--launch", launch_id, "--report-id", "r-1")
    code, out, err = _report(board, capsys, *args)
    assert (code, err) == (0, "") and json.loads(out)["queued"] is True
    code, out, _ = _report(board, capsys, *args)
    assert code == 0 and json.loads(out)["queued"] is False
    c = sqlite3.connect(board / "reports.db")
    try:
        assert c.execute("SELECT report_id, worker, event FROM reports").fetchall() == [("r-1", launch_id, "attach")]
    finally:
        c.close()
    assert _rows(board, "SELECT count(*) FROM worker_attach") == [(0,)]           # the verb wrote no board row
    svc, stoken = _svc(board, store, token)
    got = work_attach.apply_reports(svc, stoken, live_sessions=frozenset())
    assert got.applied == ["r-1"]
    again = work_attach.apply_reports(svc, stoken, live_sessions=frozenset())
    assert again.applied == [] and _rows(board, "SELECT count(*) FROM worker_attach") == [(1,)]


def test_report_for_an_unknown_item_is_refused_with_a_visible_record(board, coord, capsys):
    store, token, launch_id = coord
    code, out, _ = _report(board, capsys, "levain:nowhere", "--worker", launch_id, "--event", "attach", "--launch",
                           launch_id, "--report-id", "r-unknown")
    assert code == 0 and json.loads(out)["queued"] is True          # the verb never judges against the store
    svc, stoken = _svc(board, store, token)
    got = work_attach.apply_reports(svc, stoken, live_sessions=frozenset())
    assert got.applied == [] and got.refused == [("r-unknown", "unknown-item")]
    assert _rows(board, "SELECT report_id, item_id, reason FROM report_refusals") == [
        ("r-unknown", "levain:nowhere", "unknown-item")]
    assert _rows(board, "SELECT count(*) FROM worker_attach") == [(0,)]


def test_report_from_a_session_that_is_not_a_real_launch_is_refused(board, coord, capsys, monkeypatch):
    store, token, _ = coord
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "sess-not-ours")
    code, _out, _ = _report(board, capsys, "levain:a", "--worker", "some-seat", "--event", "attach",
                            "--report-id", "r-unreal")
    assert code == 0
    svc, stoken = _svc(board, store, token)
    got = work_attach.apply_reports(svc, stoken, live_sessions=frozenset({"other"}))
    assert got.refused == [("r-unreal", "worker-not-a-launch")]
    assert _rows(board, "SELECT report_id, reason FROM report_refusals") == [("r-unreal", "worker-not-a-launch")]
    c = sqlite3.connect(board / "reports.db")
    try:
        assert c.execute("SELECT harness_session_id FROM reports").fetchall() == [("sess-not-ours",)]
    finally:
        c.close()


def test_report_with_a_malformed_field_queues_nothing(board, capsys):
    code, _out, err = _report(board, capsys, "levain:a", "--worker", "w", "--event", "landed", "--sha", "abc",
                              "--report-id", "r-bad")
    assert code != 0 and "40 or 64 hex" in err
    assert not (board / "reports.db").exists()


# --- coord spawn --item: the attach is in the spawn's own transaction -------------------------------------------------
def test_spawn_item_records_the_attach_with_the_launch(board, coord, registry):  # noqa: F811
    store, token, _ = coord
    _d, t = a_thread(store)
    ln = inst.spawn_worker(store, token, thread_id=t.thread_id, cwd=str(board), inline=role(),
                           slots={"topic": "x"}, agent_store=registry, item="levain:a")
    assert _rows(board, "SELECT item_id, kind, worker, worker_kind, launch_id, by, lease_token FROM worker_attach") == [
        ("levain:a", "attach", ln.launch_id, "launch", ln.launch_id, "coordinator", token)]
    # the launch is a real worker for the item: its own progress report applies
    work_attach.enqueue_report(board, report_id="r-prog", item_id="levain:a", worker=ln.launch_id,
                               worker_kind="launch", event="progress", at="2026-11-08T12:00:00Z",
                               launch_id=ln.launch_id)
    svc, stoken = _svc(board, store, token)
    got = work_attach.apply_reports(svc, stoken, live_sessions=frozenset())
    assert got.applied == ["r-prog"]


def test_a_failed_spawn_leaves_no_attach_and_no_launch(board, coord, registry, monkeypatch):  # noqa: F811
    store, token, _ = coord
    _d, t = a_thread(store)
    before = _rows(board, "SELECT count(*) FROM launches")
    with pytest.raises(inst.SpawnRefused, match="not a ready item"):
        inst.spawn_worker(store, token, thread_id=t.thread_id, cwd=str(board), inline=role(), slots={"topic": "x"},
                          agent_store=registry, item="levain:nowhere")
    assert _rows(board, "SELECT count(*) FROM worker_attach") == [(0,)]
    assert _rows(board, "SELECT count(*) FROM launches") == before
    # a failure AFTER the attach row is written rolls the attach back with the launch
    real = work_attach.attach_launch

    def write_then_fail(conn, now, tok, launch_id, item_id):
        real(conn, now, tok, launch_id, item_id)
        raise RuntimeError("boom after the attach row")

    monkeypatch.setattr(work_attach, "attach_launch", write_then_fail)
    with pytest.raises(RuntimeError, match="boom"):
        inst.spawn_worker(store, token, thread_id=t.thread_id, cwd=str(board), inline=role(), slots={"topic": "x"},
                          agent_store=registry, item="levain:a")
    assert _rows(board, "SELECT count(*) FROM worker_attach") == [(0,)]
    assert _rows(board, "SELECT count(*) FROM launches") == before


# --- row 156 L2 F16: the seat learns what became of its report ------------------------------------------------------
def _reports(board, capsys, rid):
    code = main(["coord", "reports", rid, "--board-store", str(board), "--json"])
    cap = capsys.readouterr()
    return code, json.loads(cap.out) if cap.out.strip() else None


def test_f16_report_says_queued_and_the_next_pass_and_reports_reads_the_outcome(board, coord, capsys, monkeypatch):
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    store, token, launch_id = coord
    code = main(["coord", "report", "levain:a", "--worker", launch_id, "--event", "attach", "--launch", launch_id,
                 "--report-id", "r-ok", "--board-store", str(board)])
    words = capsys.readouterr().out
    assert code == 0 and "r-ok" in words and "queued" in words and "coord watch" in words
    code, st = _reports(board, capsys, "r-ok")
    assert code == 0 and (st["state"], st["item_id"]) == ("queued", "levain:a")
    _report(board, capsys, "levain:nowhere", "--worker", launch_id, "--event", "attach", "--launch", launch_id,
            "--report-id", "r-no")
    svc, stoken = _svc(board, store, token)
    work_attach.apply_reports(svc, stoken, live_sessions=frozenset())
    code, st = _reports(board, capsys, "r-ok")
    assert code == 0 and st["state"] == "applied" and st["kind"] == "attach"
    code, st = _reports(board, capsys, "r-no")
    assert code == 0 and (st["state"], st["reason"]) == ("refused", "unknown-item")
    code, st = _reports(board, capsys, "r-never")
    assert st["state"] == "unknown" and code == 1
    # a resend of the refused report says it was refused, and why
    main(["coord", "report", "levain:nowhere", "--worker", launch_id, "--event", "attach", "--launch", launch_id,
          "--report-id", "r-no", "--board-store", str(board)])
    words = capsys.readouterr().out
    assert "refused" in words and "unknown-item" in words and "already queued" not in words

"""T7 fix 3 (levain#101), from L3 round 1: state_doc without global stream redirection, claim cells read with the write's
validator, the durable append, the bounded marker, a drained bad-seq report, the lease check in apply_reports, and
``ready show``'s scope. Scratch stores only."""
from __future__ import annotations

import argparse
import io
import json
import sqlite3
import sys
import threading
import time
from pathlib import Path

import pytest

from levain.desk import board_cli, lease, work_attach
from levain.desk.legacy import ROLE_DESK
from levain.desk.store import CoordError, CoordStore
from tests.test_t7_fix1 import _mark, _plant


@pytest.fixture
def desk_board(tmp_path):
    store = CoordStore(tmp_path / "consent")
    return store, store.coord_take()


# --- 1: state_doc never touches the process's streams ----------------------------------------------------------------
def test_state_doc_from_threads_while_another_prints_keeps_every_doc_and_the_real_stdout(monkeypatch):
    # P2 made board_cli.state_doc(args, say) stream-free (it returns the doc and hands refusals to ``say``); read_board
    # is that call plus the text, so it never redirects a process stream and threads cannot capture each other.
    def fake_state(args, say=None, *a, **kw):
        tag = str(args.consent_dir)
        for _ in range(20):
            time.sleep(0.001)                       # yield: another thread runs while this one is mid-read
        say("err-" + tag)
        return 0, {"tag": tag}

    monkeypatch.setattr(board_cli, "state_doc", fake_state)
    original = sys.stdout, sys.stderr
    got: dict[str, tuple] = {}
    stop = threading.Event()

    def noise():
        while not stop.is_set():
            print("NOISE-on-stdout")
            print("NOISE-on-stderr", file=sys.stderr)
            time.sleep(0.0005)

    def call(name):
        got[name] = board_cli.read_board(consent_dir=name)

    n = threading.Thread(target=noise)
    n.start()
    ts = [threading.Thread(target=call, args=(f"t{i}",)) for i in range(8)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    stop.set()
    n.join()
    assert (sys.stdout, sys.stderr) == original
    for name, (code, doc, err) in got.items():
        assert code == 0 and doc["tag"] == name and err == "err-" + name        # nobody's output leaked into anybody's


def test_read_board_gives_none_for_a_doc_that_is_not_an_object(monkeypatch):
    monkeypatch.setattr(board_cli, "state_doc", lambda args, say=None, *a, **kw: (0, ["not", "an", "object"]))
    code, doc, err = board_cli.read_board()
    assert code == 0 and doc is None


def test_run_state_still_prints_to_the_real_streams(monkeypatch, capsys):
    monkeypatch.setattr(board_cli, "state_doc", lambda args, say=None, *a, **kw: (3, {"schema": 3, "as_of": None, "items": [], "audit": []}))
    assert board_cli.run_state(argparse.Namespace(json=True)) == 3
    cap = capsys.readouterr()
    assert '"schema": 3' in cap.out and cap.err == ""


# --- 2: the claim cell is read with the write validator --------------------------------------------------------------
def test_check_board_items_refuses_bad_input_and_never_raises_a_type_error():
    for bad in ([["a#1"]], [{"x": 1}], "levain#1", {"levain#1": 1}, None, 5, [None], ["levain#1", "levain#1"], []):
        with pytest.raises(lease.LeaseRefused):
            lease.check_board_items(bad)
    assert lease.check_board_items(("levain#1", "levain#2")) == ["levain#1", "levain#2"]


def test_a_cell_the_write_would_refuse_reads_as_an_unknown_claim(desk_board):
    store, token = desk_board
    one = store.launch_request(token, harness="claude", role=ROLE_DESK, attach_item="levain#1")
    c = sqlite3.connect(store.path)
    try:
        for (name,) in c.execute("SELECT name FROM sqlite_master WHERE type = 'trigger' AND tbl_name = 'launch_board_items'"
                                 ).fetchall():
            c.execute(f"DROP TRIGGER {name}")
        cells = ['[["levain#1"]]', '{"a": 1}', '["levain#1", "levain#1"]', '["not an id"]', "[]", '"levain#1"', "[" * 200000,
                 json.dumps(["levain#%d" % i for i in range(20000)]), "[[1]]"]
        for cell in cells:
            c.execute("UPDATE launch_board_items SET items = ? WHERE launch_id = ?", (cell, one.launch_id))
            got = lease.launch(c, one.launch_id)
            assert got.board_items is None and "damaged" in got.board_items_why, cell[:40]
        c.execute("UPDATE launch_board_items SET items = ? WHERE launch_id = ?", ('["levain#1"]', one.launch_id))
        got = lease.launch(c, one.launch_id)
        assert got.board_items == ("levain#1",) and got.board_items_why is None
    finally:
        c.close()


# --- 3: the append writes every byte ---------------------------------------------------------------------------------
def test_the_durable_append_loops_over_short_writes_and_refuses_a_zero_write(tmp_path, monkeypatch):
    real = work_attach.os.write
    sizes = []

    def short(fd, data):
        sizes.append(len(data))
        return real(fd, bytes(data)[:3])

    p = tmp_path / "f"
    fd = work_attach._open_beside(p)
    try:
        monkeypatch.setattr(work_attach.os, "write", short)
        work_attach._append_durable(fd, "hello world\n")
        monkeypatch.setattr(work_attach.os, "write", lambda fd, data: 0)
        with pytest.raises(OSError, match="zero-byte"):
            work_attach._append_durable(fd, "more")
    finally:
        monkeypatch.undo()
        import os
        os.close(fd)
    assert p.read_text() == "hello world\n" and len(sizes) == 4


# --- 4 + 5: the marker is bounded, recursion-safe, and never read through a symlink ----------------------------------
def test_marker_lines_over_the_bound_or_too_deep_are_corrupt_like_a_torn_line():
    long = json.dumps({"key": "r-long", "line": "x" * (work_attach.MARKER_LINE_MAX + 1)})
    deep = "[" * 100000 + "]" * 100000
    good = json.dumps({"key": "r-ok", "line": "ok"})
    assert work_attach._marker_keys("\n".join([long, deep, good]) + "\n") == {"r-ok"}


def test_a_symlinked_marker_reads_as_absent_in_both_paths(tmp_path):
    board = tmp_path / "board"
    board.mkdir()
    target = tmp_path / "elsewhere"
    target.write_text(json.dumps({"key": "r-1", "line": "x"}) + "\n")
    work_attach.drained_path(board).symlink_to(target)
    assert work_attach._drained_keys(board) == set()
    _plant(board, report_id="r-1")
    with pytest.raises(OSError):                       # the drain refuses to open it (as before)
        work_attach.drain_reports(board)
    assert work_attach.report_status(board, "r-1")["state"] == "queued"


# --- 6: a drained bad-seq report reads drained -----------------------------------------------------------------------
def test_a_drained_bad_seq_report_reads_drained(tmp_path):
    board = tmp_path / "board"
    board.mkdir()
    _plant(board, report_id="")                       # an id the record cannot hold
    row = work_attach._queued_one(board, "bad-seq-1")
    assert row is not None and row["seq"] == 1
    assert work_attach.report_status(board, "bad-seq-1")["state"] == "queued"
    got = work_attach.drain_reports(board)
    assert got.drained == ["bad-seq-1"]
    assert work_attach.report_status(board, "bad-seq-1")["state"] == "drained"
    assert work_attach.report_status(board, "bad-seq-99")["state"] == "unknown"
    assert work_attach.report_status(board, "bad-seq-notanumber")["state"] == "unknown"


# --- 7: apply_reports checks the live lease --------------------------------------------------------------------------
def test_apply_reports_refuses_a_wrong_token_and_drains_nothing(desk_board):
    store, token = desk_board
    d = store.directory
    _plant(d, report_id="r-1")
    with pytest.raises(CoordError, match="not held by this token"):
        work_attach.apply_reports(store, token + 1000)
    assert not work_attach.drained_path(d).exists()
    assert work_attach.apply_reports(store, token).drained == ["r-1"]


# --- 8 + 9: ready show scope, --all, queue-dir; the seam's audit truncation ------------------------------------------
def _doc():
    return {"schema": "ready-v3", "as_of": None,
            "items": [{"id": "a#1", "state": "ready", "reason": "r"}, {"id": "b#1", "state": "ready", "reason": "r"}],
            "audit": [{"kind": "k", "id": "a#1", "why": "w"}, {"kind": "k", "id": "b#1", "why": "w"},
                      {"kind": "k", "id": None, "why": "store-wide"}],
            "problems": ["repo b not read: 1 item(s) left off the board (b#1)", "repo a not read: x", "line 3: torn: x"],
            "reader": {"diverged": ["a:main", "b:main"]}}


def test_only_repo_filters_items_and_audit_by_id_keeps_store_wide_entries_and_leaves_problems_and_reader_whole():
    out = board_cli.only_repo(_doc(), "a")
    assert [i["id"] for i in out["items"]] == ["a#1"]
    assert [a["why"] for a in out["audit"]] == ["w", "store-wide"]
    assert out["problems"] == _doc()["problems"]
    assert out["reader"] == _doc()["reader"]
    assert board_cli.only_repo(_doc(), None) == _doc()
    text = "\n".join(board_cli.table_lines(_doc(), "a"))
    assert "reader (whole board): diverged: b:main" in text and "b#1" not in text


def test_ready_show_passes_queue_dir_filters_by_repo_and_notes_all(monkeypatch, capsys, tmp_path):
    from levain.desk import cli

    seen = {}
    monkeypatch.setattr(board_cli, "read_board", lambda items=None, **kw: (seen.update(kw), (3, _doc(), ""))[1])
    args = type("A", (), {"json": True, "repo": "a", "all": True, "queue_dir": tmp_path})()
    out = io.StringIO()
    assert cli._coord_ready(args, out, None) == 0               # a readable board exits 0, as the v1 show did
    shown = json.loads(out.getvalue())
    assert seen["consent_dir"] == tmp_path and shown["exit"] == 3
    assert [i["id"] for i in shown["board"]["items"]] == ["a#1"] and shown["board"]["reader"]["diverged"] == ["a:main", "b:main"]
    assert shown["board"]["reader_scope"] == "board"
    assert "--all is ignored" in capsys.readouterr().err


def test_the_seam_board_block_says_how_much_audit_it_does_not_list(desk_board, monkeypatch):
    from levain.desk import seam

    store, _token = desk_board
    audit = [{"kind": "k", "id": f"a#{i}", "why": "w"} for i in range(seam._LIST_LINES + 3)]
    monkeypatch.setattr(board_cli, "read_board", lambda items=None, **kw: (0, {"as_of": None, "items": [], "audit": audit}, ""))
    lines = seam._board(store, None, "now")
    assert lines[-1] == "  ... 3 more audit not listed"

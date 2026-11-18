"""T7 fix 7 (levain#101), from the restack review: ``ready show --queue-dir`` expands a quoted ``~``, a claim longer than the
read's cell bound is refused at the write, and the open's Board block never reads a failed read as an empty board.
Scratch stores only (``HOME`` is a scratch dir)."""
from __future__ import annotations

import io
import json
import sqlite3

import pytest

from levain.desk import board_cli, lease
from levain.desk.legacy import ROLE_DESK
from levain.desk.store import CoordStore


@pytest.fixture
def desk_board(tmp_path):
    store = CoordStore(tmp_path / "consent")
    return store, store.coord_take()


# --- 2: a quoted ~/desk is the user's desk -----------------------------------------------------------------------------
def test_ready_show_expands_a_quoted_tilde_queue_dir_like_store_does(monkeypatch, tmp_path):
    from pathlib import Path

    from levain.desk import cli

    monkeypatch.setenv("HOME", str(tmp_path))
    seen = {}
    monkeypatch.setattr(board_cli, "read_board", lambda items=None, **kw: (seen.update(kw), (0, None, "x"))[1])
    args = type("A", (), {"json": True, "repo": None, "all": False, "queue_dir": Path("~/desk")})()
    cli._coord_ready(args, io.StringIO(), None)
    assert seen["consent_dir"] == tmp_path / "desk" and "~" not in str(seen["consent_dir"])


def test_ready_show_without_a_queue_dir_hands_none(monkeypatch):
    from levain.desk import cli

    seen = {}
    monkeypatch.setattr(board_cli, "read_board", lambda items=None, **kw: (seen.update(kw), (0, None, "x"))[1])
    args = type("A", (), {"json": True, "repo": None, "all": False, "queue_dir": None})()
    cli._coord_ready(args, io.StringIO(), None)
    assert seen["consent_dir"] is None


# --- 4: the write never makes a cell the read calls damaged -------------------------------------------------------------
def test_a_claim_longer_than_the_read_bound_is_refused_and_writes_nothing(desk_board):
    store, token = desk_board
    ln = store.launch_request(token, harness="claude", role=ROLE_DESK)
    big = [f"levain#{n}" for n in range(1, 9000)]
    assert len(json.dumps(big)) > lease.BOARD_ITEMS_CELL_MAX
    with pytest.raises(lease.LeaseRefused, match="serialised"):
        with store._write() as (conn, now):
            lease.claim_board_items(conn, now, ln.launch_id, big)
    conn = sqlite3.connect(store.path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM launch_board_items").fetchone()[0] == 0
    finally:
        conn.close()


def test_a_claim_within_the_bound_still_reads_back_whole(desk_board):
    store, token = desk_board
    ln = store.launch_request(token, harness="claude", role=ROLE_DESK)
    ids = [f"levain#{n}" for n in range(1, 3000)]
    assert len(json.dumps(ids)) <= lease.BOARD_ITEMS_CELL_MAX
    with store._write() as (conn, now):
        lease.claim_board_items(conn, now, ln.launch_id, ids)
    conn = store.open_readonly()
    try:
        got = lease.launch(conn, ln.launch_id)
        assert got.board_items == tuple(ids) and got.board_items_why is None
    finally:
        conn.close()


# --- 5: the open's Board block names a failed read ----------------------------------------------------------------------
def test_the_seam_board_block_prints_problems_when_there_are_no_items(desk_board, monkeypatch):
    from levain.desk import seam

    store, _token = desk_board
    doc = {"as_of": None, "items": [], "audit": [], "problems": ["line 3: torn: not json"]}
    monkeypatch.setattr(board_cli, "read_board", lambda items=None, **kw: (0, doc, ""))
    lines = seam._board(store, None, "now")
    assert lines[0] != "Board: 0 items" and "  problem: line 3: torn: not json" in lines


def test_the_seam_board_block_says_how_many_problems_it_does_not_list(desk_board, monkeypatch):
    from levain.desk import seam

    store, _token = desk_board
    doc = {"as_of": None, "items": [], "audit": [], "problems": [f"line {i}: torn" for i in range(seam._LIST_LINES + 2)]}
    monkeypatch.setattr(board_cli, "read_board", lambda items=None, **kw: (0, doc, ""))
    assert seam._board(store, None, "now")[-1] == "  ... 2 more problems not listed"


def test_the_seam_board_block_with_no_problems_and_no_items_is_still_zero_items(desk_board, monkeypatch):
    from levain.desk import seam

    store, _token = desk_board
    monkeypatch.setattr(board_cli, "read_board", lambda items=None, **kw: (0, {"as_of": None, "items": [], "audit": [], "problems": []}, ""))
    assert seam._board(store, None, "now") == ["Board: 0 items"]

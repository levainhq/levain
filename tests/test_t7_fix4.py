"""T7 fix 4 (levain#101), from L3 round 2: the marker read fails like the drain does, the drain bounds what it writes,
bad-seq keys are canonical, ``--repo`` filters the whole reader block, and ``run_state`` always shows what it collected.
Scratch stores only."""
from __future__ import annotations

import errno
import json
import os
import sqlite3
import sys

import pytest

from levain.desk import board_cli, work_attach
from tests.test_t7_fix1 import _plant


# --- 1: only ENOENT and ELOOP read as no marker ----------------------------------------------------------------------
def test_an_unreadable_marker_propagates_and_only_enoent_and_eloop_read_as_absent(tmp_path, monkeypatch):
    board = tmp_path / "board"
    board.mkdir()
    assert work_attach._drained_keys(board) == set()                       # ENOENT
    real = os.open

    def deny(path, flags, *a, **k):
        if str(path).endswith(work_attach.DRAINED_NAME):
            raise OSError(errno.EACCES, "denied")
        return real(path, flags, *a, **k)

    monkeypatch.setattr(work_attach.os, "open", deny)
    with pytest.raises(OSError):
        work_attach._drained_keys(board)
    _plant(board, report_id="r-1")
    with pytest.raises(OSError):
        work_attach.report_status(board, "r-1")


# --- 2: the drain never writes a record over the bound ---------------------------------------------------------------
def test_a_record_is_cut_to_the_bound_and_marked():
    rec = work_attach._record("r-1", "x" * 10000)
    assert len(rec) - 1 <= work_attach.MARKER_LINE_MAX and rec.endswith("\n") and "...[cut]" in rec
    assert work_attach._marker_keys(rec) == {"r-1"}
    wide = work_attach._record("k" * 100, "\U0001F600" * 5000)               # non-BMP escapes to 12 bytes each
    assert len(wide) - 1 <= work_attach.MARKER_LINE_MAX and work_attach._marker_keys(wide) == {"k" * 100}
    short = work_attach._record("r-2", "small")
    assert json.loads(short) == {"key": "r-2", "line": "small"}


def test_a_report_with_a_huge_description_drains_once_and_reads_drained(tmp_path, monkeypatch):
    board = tmp_path / "board"
    board.mkdir()
    _plant(board, report_id="r-big", item_id="i" * 10000, event="e" * 10000)
    seen = []
    real = work_attach._record

    def spy(key, line):
        seen.append(len(line))
        return real(key, "y" * 10000 if len(line) < 10000 else line)     # a description far over the bound reaches the write

    monkeypatch.setattr(work_attach, "_record", spy)
    assert work_attach.drain_reports(board).drained == ["r-big"]
    assert work_attach.drain_reports(board).drained == []
    assert work_attach.report_status(board, "r-big")["state"] == "drained"
    for ln in work_attach.drained_path(board).read_text().split("\n")[:-1]:
        assert len(ln) <= work_attach.MARKER_LINE_MAX


# --- 3: bad-seq keys are canonical -----------------------------------------------------------------------------------
def _plant_seq(board, seq, report_id):
    conn = sqlite3.connect(work_attach.reports_path(board))
    try:
        for stmt in work_attach.REPORT_SCHEMA:
            conn.execute(stmt)
        conn.execute("INSERT INTO reports (seq, report_id, item_id, worker, worker_kind, event, at) "
                     "VALUES (?, ?, 'levain:a', 'w', 'seat', 'progress', '2026-11-16T12:00:00Z')", (seq, report_id))
        conn.commit()
    finally:
        conn.close()


def test_a_nineteen_digit_seq_drains_and_reads_drained(tmp_path):
    board = tmp_path / "board"
    board.mkdir()
    _plant_seq(board, 1000000000000000000, "")
    key = "bad-seq-1000000000000000000"
    assert work_attach.report_status(board, key)["state"] == "queued"
    assert work_attach.drain_reports(board).drained == [key]
    assert work_attach.report_status(board, key)["state"] == "drained"


def test_a_bad_seq_key_with_a_leading_zero_or_past_the_integer_range_is_unknown(tmp_path):
    board = tmp_path / "board"
    board.mkdir()
    _plant(board, report_id="")
    assert work_attach.report_status(board, "bad-seq-1")["state"] == "queued"
    for key in ("bad-seq-01", "bad-seq-001", "bad-seq-0", "bad-seq-9999999999999999999", "bad-seq-"):
        assert work_attach.report_status(board, key)["state"] == "unknown", key


def test_report_status_of_an_empty_id_is_unknown_even_when_an_empty_id_is_queued(tmp_path):
    board = tmp_path / "board"
    board.mkdir()
    _plant(board, report_id="")
    got = work_attach.report_status(board, "")
    assert got["state"] == "unknown" and got["item_id"] is None


# --- 4: --repo filters the whole reader block ------------------------------------------------------------------------
def _whole():
    sm = lambda name, **kw: {"repo": name, "since": "s", "main_ref": "main", "merged": kw.get("merged", 0), "diverged": kw.get("d", [])}
    return {"schema": "ready-v3", "as_of": None,
            "items": [{"id": "a#1", "state": "ready", "reason": "r"}, {"id": "b#1", "state": "ready", "reason": "r"}],
            "audit": [{"kind": "unknown", "id": None, "why": "b: main unreadable: x"},
                      {"kind": "unknown", "id": None, "why": "a: chain unreadable: y"},
                      {"kind": "unknown", "id": None, "why": "the registry file is torn"}],
            "problems": ["b: main unreadable: x", "a: chain unreadable: y", "line 3: torn: x"],
            "reader": {"repo": "a, b", "since": "s", "main_ref": "main", "merged": 5, "diverged": ["a:main", "b:dev"],
                       "repos": [sm("a", merged=2, d=["main"]), sm("b", merged=3, d=["dev"])]}}


def test_only_repo_leaves_the_reader_byte_identical_and_labels_it_the_whole_boards():
    whole = _whole()
    out = board_cli.only_repo(whole, "a")
    assert json.dumps(out["reader"], sort_keys=True) == json.dumps(_whole()["reader"], sort_keys=True)
    assert out["reader_scope"] == "board" and out["repo_filter"] == "a"
    assert out["problems"] == whole["problems"]
    unfiltered = board_cli.only_repo(_whole(), None)
    assert "reader_scope" not in unfiltered and "repo_filter" not in unfiltered
    none = board_cli.only_repo(_whole(), "zzz")
    assert none["reader"] == _whole()["reader"] and none["items"] == []


def test_only_repo_keeps_every_idless_audit_entry_even_one_naming_another_repo_and_drops_other_repos_ids():
    doc = _whole()
    doc["audit"].append({"kind": "k", "id": "b#1", "why": "w"})
    doc["audit"].append({"kind": "k", "id": "a#1", "why": "w"})
    out = board_cli.only_repo(doc, "a")
    assert [a["why"] for a in out["audit"]] == ["b: main unreadable: x", "a: chain unreadable: y", "the registry file is torn", "w"]
    assert [a["id"] for a in out["audit"]] == [None, None, None, "a#1"]
    assert [i["id"] for i in out["items"]] == ["a#1"]


def test_table_lines_labels_the_reader_whole_board_under_a_repo_filter_and_counts_what_it_keeps():
    text = "\n".join(board_cli.table_lines({**_whole(), "reader": {"diverged": ["a:main", "b:dev"]}}, "a"))
    assert "1 items, 3 audit entries" in text and "b#1" not in text
    assert "reader (whole board): diverged: a:main" in text and "reader (whole board): diverged: b:dev" in text
    plain = "\n".join(board_cli.table_lines({**_whole(), "reader": {"diverged": ["a:main"]}}))
    assert "reader (whole board)" not in plain and "diverged: a:main" in plain


# --- 5: run_state shows what was collected before an exception -------------------------------------------------------
def test_run_state_writes_the_collected_streams_when_the_read_raises(monkeypatch, capsys):
    def boom(args, out, err):
        out.write("partial out\n")
        err.write("earlier stderr line\n")
        raise RuntimeError("partway")

    monkeypatch.setattr(board_cli, "_state", boom)
    with pytest.raises(RuntimeError):
        board_cli.run_state(object())
    cap = capsys.readouterr()
    assert cap.out == "partial out\n" and cap.err == "earlier stderr line\n"
    assert sys.stdout is not None

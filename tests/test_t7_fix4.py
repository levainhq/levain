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


def test_only_repo_filters_reader_repos_recomputes_counts_and_drops_other_repos_idless_audit():
    out = board_cli.only_repo(_whole(), "a")
    assert [s["repo"] for s in out["reader"]["repos"]] == ["a"]
    assert out["reader"]["merged"] == 2 and out["reader"]["repo"] == "a" and out["reader"]["diverged"] == ["a:main"]
    assert [a["why"] for a in out["audit"]] == ["a: chain unreadable: y", "the registry file is torn"]   # a no-repo entry stays
    assert out["problems"] == ["a: chain unreadable: y", "line 3: torn: x"]
    assert "b" not in json.dumps(out["reader"]).replace('"main_ref"', "")
    none = board_cli.only_repo(_whole(), "zzz")
    assert none["reader"]["repos"] == [] and none["reader"]["merged"] == 0 and none["reader"]["diverged"] == []


def test_only_repo_with_an_also_reader_keeps_the_named_repos_summary():
    doc = _whole()
    doc["reader"] = {"repo": "a", "merged": 2, "diverged": [], "also": [{"repo": "b", "merged": 3, "diverged": []}]}
    assert board_cli.only_repo(doc, "a")["reader"] == {"repo": "a", "merged": 2, "diverged": [], "also": []}
    got = board_cli.only_repo(doc, "b")["reader"]
    assert got["repo"] == "b" and got["merged"] == 3 and got["also"] == []


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

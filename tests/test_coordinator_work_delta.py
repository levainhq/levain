"""C11 S3: the work delta in the coordinator's reply block (1030-2-c11/DESIGN.md section 3.4). Built from the first-spend
RUN over a scratch coordinator store (launches, a KNOW, drop receipts, ready moves, a lease): every source row carries a
``gseq`` except a reply written before ``replies`` had the column; ``chat_messages`` is absent from a plain coordinator
store. The unit tests read a scratch store; the hook tests run the installed hook ENTRY scripts as subprocesses with
real hook JSON (the S1/S2 rig)."""
from __future__ import annotations

import json
import sqlite3

import pytest

from levain import band_view
from levain.coordinator import ready, work_delta
from levain.coordinator.store import CoordStore
from tests.test_coordinator_wake_silent import SID, WORK_HEAD, Rig, block_of

V = "coordinator"


# ------------------------------------------------------------------------------------------------ the unit
@pytest.fixture
def store(tmp_path):
    d = tmp_path / "dispatch"
    d.mkdir(mode=0o700)
    return CoordStore(d)


@pytest.fixture
def token(store):
    return store.coord_take()


def _gmax(store) -> int:
    c = sqlite3.connect(store.path)
    try:
        return c.execute("SELECT MAX(gseq) FROM gseq").fetchone()[0]
    finally:
        c.close()


def _cursor_to_now(store) -> int:
    """Put the operator's cursor at the end of everything written so far."""
    g = _gmax(store)
    assert work_delta.advance(store.directory, V, g, 5.0)
    return g


def _moves(store, token, n, start=0):
    for i in range(start, start + n):
        ready.add(store, token, f"levain:item{i}", repo="levain", title=f"Item {i}", who="desk", why="queued.", to="ready")


class _Busy(CoordStore):
    def open_readonly(self, busy_s=None):
        raise sqlite3.OperationalError("database is locked")


def _read(store) -> work_delta.WorkBlock:
    return work_delta.read_block(store.directory, V, 5.0)


def test_four_records_after_the_cursor_are_four_lines_in_gseq_order(store, token):
    _cursor_to_now(store)
    _moves(store, token, 4)
    b = _read(store)
    lines = b.text.splitlines()
    assert lines[0] == "since your last message (Levain's record, 4 of 4):"
    assert [ln.strip().split(": ")[0] for ln in lines[1:]] == [f"levain:item{i}" for i in range(4)]
    assert len(lines) == 5 and "more" not in b.text
    assert b.through == _gmax(store)


def test_seven_records_are_five_lines_plus_two_more_and_the_command(store, token):
    _cursor_to_now(store)
    _moves(store, token, 7)
    b = _read(store)
    lines = b.text.splitlines()
    assert lines[0] == "since your last message (Levain's record, 5 of 7):"
    assert [ln.strip().split(": ")[0] for ln in lines[1:6]] == [f"levain:item{i}" for i in range(5)]
    first_gseq = sqlite3.connect(store.path).execute("SELECT gseq FROM ready_journal WHERE id = 'levain:item4'").fetchone()[0]
    assert lines[6] == f"  +2 more: levain coord log --since {first_gseq}"
    assert len(lines) == 7
    assert b.through == _gmax(store)           # the cursor covers the +2: they are reachable by the command, not re-listed


def test_a_lease_beat_is_not_news(store, token):
    _cursor_to_now(store)
    store.coord_beat(token)
    assert _read(store) == work_delta.WorkBlock("", 0)
    store.coord_release()
    assert "lease release" in _read(store).text


def test_nothing_after_the_cursor_is_no_block(store, token):
    _cursor_to_now(store)
    assert _read(store) == work_delta.WorkBlock("", 0)


def test_a_cursor_never_moves_back_or_repeats(store, token):
    g = _cursor_to_now(store)
    assert not work_delta.advance(store.directory, V, g, 5.0)          # the same place again writes nothing
    assert not work_delta.advance(store.directory, V, g - 1, 5.0)      # an older reply's cursor (two sessions)
    c = sqlite3.connect(store.path)
    assert c.execute("SELECT COUNT(*) FROM operator_views").fetchone()[0] == 1
    with pytest.raises(sqlite3.IntegrityError, match="only moves forward"):
        c.execute("INSERT INTO operator_views (viewer, gseq, at) VALUES ('coordinator', 1, 'x')")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        c.execute("UPDATE operator_views SET gseq = 0")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        c.execute("DELETE FROM operator_views")
    c.close()


def test_old_replies_with_no_gseq_are_excluded_and_counted(store, token):
    _cursor_to_now(store)
    c = sqlite3.connect(store.path)
    c.execute("INSERT INTO replies (reply_id, thread_id, kind, ref_store, ref_id, body, at) VALUES "
              "('rp-old', NULL, 'drop_receipt', 'drops', 'd-old', '{}', '2026-01-01T00:00:00Z')")
    c.commit()
    c.close()
    assert _read(store).text == ""                                      # alone, an unordered reply is no news
    _moves(store, token, 1)
    text = _read(store).text
    assert text.splitlines()[0] == ("since your last message (Levain's record, 1 of 1; 1 older reply carries no order "
                                    "and is not listed):")
    assert "d-old" not in text


def test_a_store_that_cannot_be_read_says_so_and_does_not_move_the_cursor(store, token, monkeypatch):
    monkeypatch.setattr(work_delta, "CoordStore", _Busy)      # only the work delta's read: the launch row still reads
    b = _read(store)
    assert b.text == "since your last message (Levain's record): ! not read (the coordinator store could not be read just now)"
    assert b.through == 0


def test_a_store_before_the_cursor_table_reads_as_zero_and_writes_nothing(store, token):
    c = sqlite3.connect(store.path)
    c.execute("DROP TABLE operator_views")
    c.commit()
    c.close()
    b = _read(store)
    assert b.text.startswith("since your last message (Levain's record, ") and b.through > 0
    assert not work_delta.advance(store.directory, V, b.through, 5.0)      # no table: the next reply repeats the delta


def test_the_text_of_a_record_is_one_bounded_line(store, token):
    _cursor_to_now(store)
    ready.add(store, token, "levain:x", repo="levain", title="t", who="desk " + "w" * 150, why="queued.", to="ready")
    line = _read(store).text.splitlines()[1]
    assert len(line) < 140 and line.endswith("…)")
    cleaned = work_delta._clean("a\n\x1b[31m  b\t" + "c" * 100, 20)
    assert "\n" not in cleaned and "\x1b" not in cleaned and len(cleaned) == 20


def test_coord_log_lists_what_more_points_at(store, token, tmp_path, capsys):
    from levain.cli import main

    _moves(store, token, 3)
    qd = ["--queue-dir", str(store.directory)]
    assert main(["coord", "log", "--since", "0", *qd]) == 0
    out = capsys.readouterr().out
    assert [ln.split("  ", 1)[1].split(": ")[0] for ln in out.splitlines() if "levain:item" in ln] == [
        "levain:item0", "levain:item1", "levain:item2"]
    assert main(["coord", "log", "--since", str(_gmax(store)), "--json", *qd]) == 0
    assert json.loads(capsys.readouterr().out) == {"records": [], "total": 0, "through": _gmax(store)}


# ------------------------------------------------------------------------------------------------ the hooks
@pytest.fixture(params=["claude_code", "codex"])
def rig(request, tmp_path):
    return Rig(tmp_path, request.param)


def _rows(rig) -> list[tuple[str, int]]:
    c = sqlite3.connect(CoordStore(rig.qdir).path)
    try:
        return c.execute("SELECT viewer, gseq FROM operator_views ORDER BY rowid").fetchall()
    finally:
        c.close()


def _top(rig) -> int:
    c = sqlite3.connect(CoordStore(rig.qdir).path)
    try:
        return c.execute("SELECT MAX(gseq) FROM gseq").fetchone()[0]
    finally:
        c.close()


def test_the_block_leads_the_band_and_the_cursor_moves_only_at_a_verified_stop(rig):
    rig.ask("Pick a DB")
    ctx = rig.ups("hello coordinator")
    assert WORK_HEAD in ctx and ctx.index(WORK_HEAD) < ctx.index("NEEDS YOU")
    assert _rows(rig) == []                                              # delivered, not yet verified
    assert json.loads(rig.stop("done"))["decision"] == "block"           # the reply left out the work lines and the band
    assert _rows(rig) == []                                              # an unverified reply moves nothing
    assert rig.stop(block_of(ctx)) == ""                                 # the reply that carries the block
    assert [v for v, _g in _rows(rig)] == [V] and _rows(rig)[0][1] > 0
    again = rig.ups("anything new?")
    assert WORK_HEAD not in again                                        # spent by the verified reply
    CoordStore(rig.qdir).coord_release()                                 # a lease record after the cursor
    now = rig.ups("and now?")
    assert "lease release" in now and "(Levain's record, " in now


def test_a_turn_that_never_reached_stop_repeats_the_block(rig):
    rig.ask("Pick a DB")
    first = rig.ups("hello coordinator")
    again = rig.ups("hello? (resent)")                                   # delivered; no Stop between (interrupted)
    assert block_of(first).split("NEEDS YOU")[0] == block_of(again).split("NEEDS YOU")[0]
    assert _rows(rig) == []
    assert rig.stop(block_of(again)) == ""
    assert len(_rows(rig)) == 1


def test_a_block_of_work_with_an_empty_band_is_still_verified(rig):
    ctx = rig.ups("hello coordinator")                                   # no question is open
    assert WORK_HEAD in ctx and "NEEDS YOU" not in ctx
    assert json.loads(rig.stop("nothing to report"))["decision"] == "block"   # the early return on an empty band never applied
    assert _rows(rig) == []
    assert rig.stop(block_of(ctx)) == ""
    assert len(_rows(rig)) == 1


def test_a_reprint_after_the_one_block_commits_the_cursor(rig):
    ctx = rig.ups("hello coordinator")
    assert json.loads(rig.stop("done"))["decision"] == "block"
    out = rig.run("stop.py", {"last_assistant_message": block_of(ctx), "cwd": str(rig.install), "stop_hook_active": True})
    assert out == "" and len(_rows(rig)) == 1


def test_a_wake_leaves_the_cursor_alone(rig):
    ctx = rig.ups("hello coordinator")
    rig.ups(rig.wake)
    assert rig.stop("split drop d1") == ""                               # the wake's reply: nobody is waiting on it
    assert _rows(rig) == []
    assert WORK_HEAD in rig.ups("hi again")                              # his next reply still carries the delta


def test_a_store_that_cannot_be_read_is_said_in_the_block_and_moves_nothing(rig, monkeypatch):
    monkeypatch.setattr(work_delta, "CoordStore", _Busy)      # only the work delta's read: the launch row still reads
    payload = {"session_id": SID, "prompt": "hello coordinator", "cwd": str(rig.install)}
    text, commit = band_view.hook_prompt(payload, rig.env, rig.agent)
    assert "! not read (the coordinator store could not be read just now)" in text
    commit()
    monkeypatch.undo()
    assert band_view.hook_stop({"session_id": SID, "last_assistant_message": text, "cwd": str(rig.install),
                                "stop_hook_active": False}, rig.env, rig.agent) is None
    assert _rows(rig) == []
    assert WORK_HEAD in rig.ups("next")                                  # the real block, once the store reads

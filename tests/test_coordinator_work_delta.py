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


def _store_id(store) -> str:
    c = sqlite3.connect(store.path)
    try:
        return c.execute("SELECT value FROM meta WHERE key = 'store_id'").fetchone()[0]
    finally:
        c.close()


def _replace_with_another_store(store, tmp_path, name="other") -> None:
    """Restore a different coordinator store (its own ``store_id``, a lower head) over ``store``'s file."""
    d = tmp_path / name
    d.mkdir(mode=0o700)
    other = CoordStore(d)
    other.coord_take()
    src, dst = sqlite3.connect(other.path), sqlite3.connect(store.path)
    try:
        src.backup(dst)
    finally:
        src.close()
        dst.close()


def _cursor_to_now(store) -> int:
    """Put the operator's cursor at the end of everything written so far."""
    g = _gmax(store)
    assert work_delta.advance(store.directory, V, g, 5.0, _store_id(store))
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
    assert _read(store)[:2] == ("", 0)
    store.coord_release()
    assert "lease release" in _read(store).text


def test_nothing_after_the_cursor_is_no_block(store, token):
    _cursor_to_now(store)
    assert _read(store)[:2] == ("", 0)


def test_a_cursor_never_moves_back_or_repeats(store, token):
    g, sid = _cursor_to_now(store), _store_id(store)
    assert not work_delta.advance(store.directory, V, g, 5.0, sid)          # the same place again writes nothing
    assert not work_delta.advance(store.directory, V, g - 1, 5.0, sid)      # an older reply's cursor (two sessions)
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


def _without_cursor_table(store) -> None:
    """The store as every store made before S3 is: no ``operator_views`` and none of its triggers."""
    c = sqlite3.connect(store.path)
    c.execute("DROP TABLE operator_views")                # its triggers go with it
    c.commit()
    c.close()


def _names(store) -> set[str]:
    c = sqlite3.connect(store.path)
    try:
        return {r[0] for r in c.execute("SELECT name FROM sqlite_master")}
    finally:
        c.close()


def test_an_upgraded_store_gets_the_cursor_table_at_the_first_verified_advance(store, token):
    """codex MED + L1 NOTE: a store from before S3 has no ``operator_views``; the reader never makes it, and an advance
    that refused for want of it left the delta repeating from 0 on every reply. ``advance`` makes it, in its own
    write transaction, and the cursor moves."""
    _without_cursor_table(store)
    b = _read(store)
    assert b.text.startswith("since your last message (Levain's record, ") and b.through > 0
    assert "operator_views" not in _names(store)                            # the reader path made nothing
    assert work_delta.advance(store.directory, V, b.through, 5.0, b.store_id)
    assert {"operator_views", "operator_views_frozen", "operator_views_kept", "operator_views_forward"} <= _names(store)
    assert _read(store) == work_delta.WorkBlock("", 0, b.store_id)           # spent: no longer from watermark 0
    c = sqlite3.connect(store.path)
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        c.execute("DELETE FROM operator_views")
    c.close()
    _moves(store, token, 1)
    assert work_delta.advance(store.directory, V, _read(store).through, 5.0, b.store_id)   # made once, used after


def test_an_advance_into_another_store_writes_nothing(store, token, tmp_path):
    """codex HIGH: ``dispatch.db`` replaced between the prompt's read and the Stop by a store of another ``store_id``
    (a different store restored over it) must not take the first store's cursor."""
    _cursor_to_now(store)
    _moves(store, token, 2)
    b = _read(store)
    assert b.through > 0 and b.store_id == _store_id(store)
    _replace_with_another_store(store, tmp_path)
    assert _store_id(store) != b.store_id
    assert not work_delta.advance(store.directory, V, b.through, 5.0, b.store_id)
    assert not work_delta.advance(store.directory, V, b.through, 5.0, "")   # a read that named no store moves nothing
    c = sqlite3.connect(store.path)
    assert c.execute("SELECT COUNT(*) FROM operator_views").fetchone()[0] == 0
    c.close()


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


@pytest.mark.parametrize("since, limit", [(str(2 ** 63), "50"), ("-1", "50"), ("x", "50"), ("0", "-1"), ("0", "0"),
                                          ("0", "501"), ("0", str(2 ** 63))])
def test_coord_log_refuses_a_number_out_of_range_in_one_line(store, token, capsys, since, limit):
    """codex MED, L1 NOTE, deepseek MED: ``--since 2**63`` raised OverflowError in SQLite's bind, ``--limit -1`` and
    ``0`` listed one record. Each is argparse's refusal (exit 2, one line), never a traceback."""
    from levain.cli import main

    with pytest.raises(SystemExit) as exc:
        main(["coord", "log", "--since", since, "--limit", limit, "--queue-dir", str(store.directory)])
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "Traceback" not in err and err.strip().splitlines()[-1].startswith("levain coord log: error: argument")


def test_coord_log_more_hint_carries_the_values_used(store, token, capsys):
    from levain.cli import main

    _moves(store, token, 3)
    qd = ["--queue-dir", str(store.directory)]
    assert main(["coord", "log", "--since", "0", "--limit", "500", *qd]) == 0       # the cap itself is allowed
    assert main(["coord", "log", "--since", str(2 ** 63 - 1), *qd]) == 0            # and the top of the range
    capsys.readouterr()
    assert main(["coord", "log", "--since", "0", "--limit", "2", *qd]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == 3
    second = int(lines[1].split("  ", 1)[0])
    assert lines[2].endswith(f" more: levain coord log --since {second} --limit 2")


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


def _sid_of(rig) -> str:
    return _store_id(CoordStore(rig.qdir))


def _replace(rig, tmp_path) -> None:
    _replace_with_another_store(CoordStore(rig.qdir), tmp_path, "restored")


def test_the_block_leads_the_band_and_the_cursor_moves_only_at_a_verified_stop(rig):
    rig.ask("Pick a DB")
    ctx = rig.ups("hello coordinator")
    assert WORK_HEAD in ctx and ctx.index(WORK_HEAD) < ctx.index("NEEDS YOU")
    assert _rows(rig) == []                                              # delivered, not yet verified
    assert json.loads(rig.stop("done"))["decision"] == "block"           # the reply left out the work lines and the band
    assert _rows(rig) == []                                              # an unverified reply moves nothing
    assert rig.stop(block_of(ctx, rig)) == ""                                 # the reply that carries the block
    assert [v for v, _g in _rows(rig)] == [V] and _rows(rig)[0][1] > 0
    again = rig.ups("anything new?")
    assert WORK_HEAD not in again                                        # spent by the verified reply
    CoordStore(rig.qdir).coord_release()                                 # a lease record after the cursor
    now = rig.ups("and now?")
    assert "lease release" in now and "(Levain's record, " in now


def test_a_band_only_reprint_is_told_to_reprint_the_work_lines_too(rig):
    """L1 WARNING: the retry reason named only the band@ line and the numbered items, so a model that reprinted exactly
    those failed the same check and the delta repeated every turn. With work lines staged, the reason names them."""
    rig.ask("Pick a DB")
    ctx = rig.ups("hello coordinator")
    band_only = ctx[ctx.index("NEEDS YOU"):]
    got = json.loads(rig.stop(band_only))
    assert got["decision"] == "block"
    assert "work lines" in got["reason"] and WORK_HEAD in got["reason"], got["reason"]
    assert _rows(rig) == []
    through = json.loads(band_view._session_file(rig.qdir, SID, "staged").read_text())["work"]
    assert rig.stop(block_of(ctx, rig)) == ""                            # both: the view commits, the cursor moves
    assert _rows(rig) == [(V, through)] and through > 0


def test_a_band_only_block_keeps_the_band_reason(rig):
    """A block with no work lines (nothing after the cursor) keeps the band's own words."""
    rig.ask("Pick a DB")
    first = rig.ups("hello coordinator")
    assert rig.stop(block_of(first, rig)) == ""
    rig.ask("Ship it?")
    ctx = rig.ups("anything?")
    assert WORK_HEAD not in block_of(ctx, rig)
    reason = json.loads(rig.stop("done"))["reason"]
    assert "work lines" not in reason and "band@ line" in reason


def test_a_store_replaced_before_stop_does_not_take_the_cursor(rig, tmp_path):
    """codex HIGH: the prompt read store A; ``dispatch.db`` is replaced by a store of another ``store_id`` before the
    Stop. The verified reply commits the band's view but the work cursor is not written into the other store."""
    ctx = rig.ups("hello coordinator")
    staged = json.loads(band_view._session_file(rig.qdir, SID, "staged").read_text())
    store_id = _sid_of(rig)
    assert staged["work"] > 0 and staged["store_id"] == store_id
    _replace(rig, tmp_path)
    assert _sid_of(rig) != store_id
    # the block it was shown (no rig: the pending check would read the store now there, not the one read)
    assert rig.stop(block_of(ctx)) == ""                                 # the reply verified
    assert _rows(rig) == []                                              # ... and the other store's cursor is untouched


def test_a_turn_that_never_reached_stop_repeats_the_block(rig):
    rig.ask("Pick a DB")
    first = rig.ups("hello coordinator")
    again = rig.ups("hello? (resent)")                                   # delivered; no Stop between (interrupted)
    assert block_of(first, rig).split("NEEDS YOU")[0] == block_of(again, rig).split("NEEDS YOU")[0]
    assert _rows(rig) == []
    assert rig.stop(block_of(again, rig)) == ""
    assert len(_rows(rig)) == 1


def test_a_block_of_work_with_an_empty_band_is_still_verified(rig):
    ctx = rig.ups("hello coordinator")                                   # no question is open
    assert WORK_HEAD in ctx and "NEEDS YOU" not in ctx
    assert json.loads(rig.stop("nothing to report"))["decision"] == "block"   # the early return on an empty band never applied
    assert _rows(rig) == []
    assert rig.stop(block_of(ctx, rig)) == ""
    assert len(_rows(rig)) == 1


def test_a_reprint_after_the_one_block_commits_the_cursor(rig):
    ctx = rig.ups("hello coordinator")
    assert json.loads(rig.stop("done"))["decision"] == "block"
    out = rig.run("stop.py", {"last_assistant_message": block_of(ctx, rig), "cwd": str(rig.install),
                              "stop_hook_active": True})
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

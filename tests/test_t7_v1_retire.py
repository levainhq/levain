"""T7 (levain#101): the v1 ready queue retires. Every write path to it refuses; the launch's attach is the v2 claim.
Scratch stores only; no real ``claude``."""
from __future__ import annotations

import sqlite3

import pytest

from levain.desk import lease
from levain.desk.legacy import ROLE_DESK
from levain.desk.store import CoordError, CoordStore


@pytest.fixture
def desk(tmp_path):
    store = CoordStore(tmp_path / "consent")
    return store, store.coord_take()


def _count(store: CoordStore, table: str) -> int:
    conn = sqlite3.connect(store.path)
    try:
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    finally:
        conn.close()


# --- 2: launch_request(attach_item=) is the v2 claim -----------------------------------------------------------------
def test_attach_item_writes_board_items_on_the_launch_record_with_no_v1_item(desk):
    store, token = desk
    ln = store.launch_request(token, harness="claude", role=ROLE_DESK, attach_item="levain#101")
    assert ln.board_items == ("levain#101",)
    conn = store.open_readonly()
    try:
        assert lease.launch(conn, ln.launch_id).board_items == ("levain#101",)
    finally:
        conn.close()
    assert _count(store, "ready_items") == 0 and _count(store, "worker_attach") == 0


def test_no_attach_item_leaves_board_items_none(desk):
    store, token = desk
    assert store.launch_request(token, harness="claude", role=ROLE_DESK).board_items is None


@pytest.mark.parametrize("bad", ["levain:a", "levain#0", "Levain#1", "levain#007", ""])
def test_a_malformed_claim_refuses_the_whole_request_and_leaves_no_launch(desk, bad):
    store, token = desk
    with pytest.raises(CoordError):
        store.launch_request(token, harness="claude", role=ROLE_DESK, attach_item=bad)
    assert _count(store, "launches") == 0 and _count(store, "launch_board_items") == 0


def test_a_claim_is_written_once_per_launch(desk):
    store, token = desk
    ln = store.launch_request(token, harness="claude", role=ROLE_DESK, attach_item="levain#5")
    with pytest.raises(sqlite3.DatabaseError):
        with store._write() as (conn, now):
            lease.claim_board_items(conn, now, ln.launch_id, ["levain#6"])


def test_a_store_made_before_the_claim_table_reads_board_items_none(desk):
    store, token = desk
    ln = store.launch_request(token, harness="claude", role=ROLE_DESK)
    conn = sqlite3.connect(store.path)
    try:
        conn.execute("DROP TABLE launch_board_items")
        assert lease.launch(conn, ln.launch_id).board_items is None
    finally:
        conn.close()


# --- 2 again: spawn --item rides the spawn's own transaction, now as the claim -----------------------------------------
from tests.test_agents_spawn import a_thread, registry, role  # noqa: E402,F401
from levain.agents import instantiate as inst  # noqa: E402


def test_spawn_item_writes_the_claim_with_the_launch(desk, registry, tmp_path):  # noqa: F811
    store, token = desk
    _d, t = a_thread(store)
    ln = inst.spawn_worker(store, token, thread_id=t.thread_id, cwd=str(tmp_path), inline=role(),
                           slots={"topic": "x"}, agent_store=registry, item="levain#7")
    assert ln.board_items == ("levain#7",)
    assert _count(store, "ready_items") == 0 and _count(store, "worker_attach") == 0


def test_a_spawn_with_a_malformed_item_leaves_no_launch(desk, registry, tmp_path):  # noqa: F811
    store, token = desk
    _d, t = a_thread(store)
    before = _count(store, "launches")
    with pytest.raises(inst.SpawnRefused, match="board item ids"):
        inst.spawn_worker(store, token, thread_id=t.thread_id, cwd=str(tmp_path), inline=role(),
                          slots={"topic": "x"}, agent_store=registry, item="levain:a")
    assert _count(store, "launches") == before and _count(store, "launch_board_items") == 0


# --- 1: the verbs refuse, naming `levain board add`, before any store is opened ----------------------------------------
from levain.cli import main  # noqa: E402
from levain.desk import ready, status_import, work_attach  # noqa: E402

RETIRED_CALLS = [
    ["ready", "add", "levain:x"], ["ready", "move", "levain:x"], ["ready", "correct", "levain:x"],
    ["ready", "add", "levain:x", "--repo", "levain", "--title", "t", "--who", "w", "--why", "y", "--token", "1"],
    ["ready", "move", "levain:x", "--to", "done", "--who", "w", "--why", "y", "--token", "1"],
    ["import-status", "STATUS.md"], ["import-status", "STATUS.md", "--token", "1"], ["report", "levain#1"],
    ["report", "levain#1", "--worker", "w", "--event", "attach"],
]


@pytest.mark.parametrize("argv", RETIRED_CALLS, ids=[" ".join(a)[:50] for a in RETIRED_CALLS])
def test_every_v1_write_verb_refuses_naming_board_add_and_opens_no_store(argv, tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("LEVAIN_HOME", str(tmp_path / "home"))
    code = main(["coord", *argv])
    cap = capsys.readouterr()
    assert code == 2 and "levain board add" in cap.err and "nothing was written" in cap.err
    assert not (tmp_path / "home").exists()


def test_the_retired_verbs_refuse_with_a_live_desk_and_a_token_too(desk, capsys):
    store, token = desk
    code = main(["coord", "ready", "add", "levain:x", "--repo", "levain", "--title", "t", "--who", "w", "--why", "y",
                 "--token", str(token), "--queue-dir", str(store.directory)])
    assert code == 2 and "levain board add" in capsys.readouterr().err
    assert _count(store, "ready_items") == 0 and _count(store, "ready_journal") == 0


# --- 3: record_landed is deleted --------------------------------------------------------------------------------------
def test_record_landed_is_gone_and_no_caller_names_it():
    import pathlib

    assert not hasattr(ready, "record_landed") and "record_landed" not in ready.__all__
    root = pathlib.Path(ready.__file__).resolve().parents[1]
    hits = [str(p) for p in root.rglob("*.py") if "record_landed" in p.read_text(encoding="utf-8")]
    assert hits == []


# --- 5: the ONE remaining v1 write function raises; the triggers stay -----------------------------------------------
def test_the_one_v1_write_function_raises_and_add_move_correct_go_through_it(desk, monkeypatch):
    store, token = desk
    for fn in (ready.add, ready.move, ready.correct, ready._transition):
        with pytest.raises(ready.V1Retired, match="levain board add"):
            fn(store, token, "levain:x", repo="levain", title="t", who="w", why="y", to="ready")
    seen = []
    monkeypatch.setattr(ready, "_transition", lambda *a, **k: seen.append(a) or {})
    ready.add(store, token, "levain:x")
    ready.move(store, token, "levain:x")
    ready.correct(store, token, "levain:x")
    assert len(seen) == 3
    assert _count(store, "ready_items") == 0 and _count(store, "ready_journal") == 0


def test_status_import_raises_before_it_reads_the_file(desk, tmp_path):
    store, token = desk
    with pytest.raises(ready.V1Retired):
        status_import.import_status(store, token, tmp_path / "absent.md")
    assert _count(store, "ready_items") == 0


def test_the_v1_tables_keep_their_append_triggers_and_what_they_hold_stays_readable(desk):
    from tests import v1_seed

    store, token = desk
    v1_seed.add(store, token, "levain:old", repo="levain", title="an old row", who="desk", why="seeded", to="ready")
    got = ready.list_ready(store, all=True)
    assert [i["id"] for i in got["items"]] == ["levain:old"]
    conn = sqlite3.connect(store.path)
    try:
        for sql in ("UPDATE ready_journal SET why = 'x'", "DELETE FROM ready_journal", "UPDATE ready_items SET title = 'x'",
                    "DELETE FROM ready_items"):
            with pytest.raises(sqlite3.DatabaseError):
                conn.execute(sql)
    finally:
        conn.close()


# --- 4: report / apply-reports stop writing v1 facts; a queued report is drained once, read-only --------------------------
def _plant(directory, **kw):
    """A report queued BEFORE the switch, planted around the (now refusing) door."""
    conn = sqlite3.connect(work_attach.reports_path(directory))
    try:
        for stmt in work_attach.REPORT_SCHEMA:
            conn.execute(stmt)
        row = {"report_id": "r-old", "item_id": "levain:a", "worker": "w", "worker_kind": "seat", "event": "progress",
               "at": "2026-11-16T12:00:00Z", **kw}
        conn.execute(f"INSERT INTO reports ({', '.join(row)}) VALUES ({', '.join('?' * len(row))})", list(row.values()))
        conn.commit()
    finally:
        conn.close()


def _v1_rows(store):
    conn = sqlite3.connect(store.path)
    try:
        return {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                for t in ("worker_attach", "report_refusals", "ready_items", "ready_journal", "sessions_seen")}
    finally:
        conn.close()


def test_enqueue_report_raises_and_makes_no_queue(tmp_path):
    with pytest.raises(ready.V1Retired):
        work_attach.enqueue_report(tmp_path, report_id="r", item_id="levain#1", worker="w", worker_kind="seat",
                                   event="progress", at="2026-11-17T00:00:00Z")
    assert not work_attach.reports_path(tmp_path).exists()


def test_a_report_queued_at_the_switch_is_drained_once_into_a_log_line_and_never_applied(desk):
    store, token = desk
    d = store.directory
    _plant(d, report_id="r-old-1")
    _plant(d, report_id="r-old-2", event="landed", sha="a" * 40)
    before = _v1_rows(store)
    first = work_attach.watch_apply(d)
    assert len(first) == 1 and "r-old-1" in first[0] and "r-old-2" in first[0] and "not applied" in first[0]
    assert work_attach.watch_apply(d) == [] and work_attach.desk_apply(store, token) == []     # once
    assert _v1_rows(store) == before == {"worker_attach": 0, "report_refusals": 0, "ready_items": 0,
                                         "ready_journal": 0, "sessions_seen": 0}
    conn = sqlite3.connect(work_attach.reports_path(d))
    assert conn.execute("SELECT COUNT(*) FROM reports").fetchone()[0] == 2      # the queue itself is untouched
    conn.close()


def test_the_watch_pass_takes_no_lease_and_opens_no_store_for_writing(desk, monkeypatch):
    store, token = desk
    _plant(store.directory)
    monkeypatch.setattr(CoordStore, "_write", lambda *a, **k: (_ for _ in ()).throw(AssertionError("a write")))
    assert "r-old" in work_attach.watch_apply(store.directory)[0]


def test_apply_reports_drains_and_applies_nothing(desk):
    store, token = desk
    _plant(store.directory)
    got = work_attach.apply_reports(store, token, live_sessions=frozenset({"s"}))
    assert got.drained == ["r-old"] and not hasattr(got, "applied")
    assert work_attach.apply_reports(store, token, live_sessions=None).drained == []
    assert _v1_rows(store)["worker_attach"] == 0 and _v1_rows(store)["sessions_seen"] == 0
    with pytest.raises(CoordError):
        work_attach.apply_reports(store, "not-a-token", live_sessions=None)


def test_desk_apply_still_refuses_a_token_that_is_not_the_live_lease(desk):
    store, token = desk
    _plant(store.directory)
    with pytest.raises(CoordError, match="lease"):
        work_attach.desk_apply(store, token + 99)
    assert not work_attach.drained_path(store.directory).exists()


def test_the_cli_apply_reports_prints_the_drain_line_once(desk, capsys):
    store, token = desk
    _plant(store.directory)
    argv = ["coord", "apply-reports", "--token", str(token), "--queue-dir", str(store.directory)]
    assert main(argv) == 0 and "r-old" in capsys.readouterr().out
    assert main(argv) == 0 and "no report is waiting" in capsys.readouterr().out
    assert _v1_rows(store)["worker_attach"] == 0


# --- 6: board changes leave the gseq stream; the open prints one board block; ready show prints the board ---------------
def test_a_v1_journal_row_is_not_news_in_the_work_delta(desk):
    from levain.desk import work_delta
    from tests import v1_seed

    store, token = desk
    v1_seed.add(store, token, "levain:old", repo="levain", title="t", who="desk", why="seeded", to="ready")
    conn = store.open_readonly()
    try:
        assert "ready_journal" not in {s.table for s in work_delta._SOURCES}
        assert not any("levain:old" in text for _g, text in work_delta.work_delta(conn, 0).lines)
    finally:
        conn.close()


DOC = {"schema": 3, "as_of": "2026-11-17T12:00:00Z", "reader": {},
       "items": [{"id": "levain#101", "state": "ready", "reason": "the claim reader landed"},
                 {"id": "flow#9", "state": "done", "reason": "merged"}],
       "audit": [{"kind": "unknown", "id": "flow#9", "why": "a trailer names it"}]}


def _fake_board(monkeypatch, doc=DOC, code=0, err=""):
    from levain.desk import board_cli

    calls = []
    monkeypatch.setattr(board_cli, "state_doc", lambda items=None, **kw: calls.append(items) or (code, doc, err))
    return calls


def test_the_desks_open_prints_one_board_block_from_the_boards_own_read(desk, monkeypatch):
    from datetime import datetime, timezone

    from levain.desk import seam

    store, token = desk
    calls = _fake_board(monkeypatch)
    text = seam.open_brief(store, store.directory, "operator", datetime.now(timezone.utc)).text.splitlines()
    block = [ln for ln in text if ln.startswith("Board:")]
    assert len(block) == 1 and "2 item(s), 1 audit entry" in block[0]
    assert "  levain#101 [ready] the claim reader landed" in text and "  audit unknown: flow#9: a trailer names it" in text
    assert calls == [None]


def test_an_unreadable_board_says_cannot_derive_never_an_empty_board(desk, monkeypatch):
    from datetime import datetime, timezone

    from levain.desk import seam

    store, token = desk
    _fake_board(monkeypatch, doc=None, code=2, err="no items store")
    text = seam.open_brief(store, store.directory, "operator", datetime.now(timezone.utc)).text
    assert "Board: cannot-derive (CoordError: no items store)" in text and "board items" not in text


def test_ready_show_prints_the_board_for_the_repo_and_says_the_queue_is_retired(desk, monkeypatch, capsys):
    _fake_board(monkeypatch)
    store, token = desk
    assert main(["coord", "ready", "show", "--repo", "levain", "--queue-dir", str(store.directory)]) == 0
    out = capsys.readouterr().out
    assert "v1 ready queue is retired" in out and "levain#101" in out and "flow#9" not in out
    assert "1 items" in out


def test_ready_show_json_carries_the_board_and_the_retired_flag(desk, monkeypatch, capsys):
    import json

    _fake_board(monkeypatch)
    store, token = desk
    assert main(["coord", "ready", "show", "--json", "--queue-dir", str(store.directory)]) == 0
    got = json.loads(capsys.readouterr().out)
    assert got["retired"] is True and [i["id"] for i in got["board"]["items"]] == ["levain#101", "flow#9"]


def test_ready_show_says_when_the_board_cannot_be_read(desk, monkeypatch, capsys):
    _fake_board(monkeypatch, doc=None, code=2, err="no items store")
    store, token = desk
    assert main(["coord", "ready", "show", "--queue-dir", str(store.directory)]) == 1
    out = capsys.readouterr().out
    assert "retired" in out and "could not be read: no items store" in out


def test_state_doc_is_the_boards_own_read_as_a_python_entry(tmp_path, monkeypatch):
    from levain.desk import board_cli

    monkeypatch.delenv("LEVAIN_BOARD_ITEMS", raising=False)
    monkeypatch.setenv("LEVAIN_HOME", str(tmp_path / "nohome"))
    code, doc, err = board_cli.read_board()
    assert code == 2 and doc is None and "no items store" in err

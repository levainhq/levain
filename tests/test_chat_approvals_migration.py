"""CH-D1c-C: a released store gains the approval tables and the view's approval branch through the ONE ensure path
(``ChatStore._connect`` -> ``chat._ensure``, head ruling Q-A), keeping every row and rowid.

The stores here are built from the chat schema TEXT two released shapes held (``tests/_chat_schema_snapshots.py``:
v0.11.25 and main at f75acbc7), filled with rows by raw SQL, and opened through the shipped ``levain chat show``."""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat
from datetime import datetime, timedelta, timezone

import pytest

from levain.coordinator import chat
from levain.coordinator.chat import ChatStore
from levain.coordinator.envelopes import EnvelopeStore
from levain.coordinator.store import CoordError, Part

from tests import _chat_schema_snapshots as snap

T0 = datetime(2026, 10, 27, 12, 0, tzinfo=timezone.utc)
CHAT_TABLES = ("chat_messages", "chat_outbox_events", "chat_events", "chat_cursors", "chat_launch_checks")
NEW_OBJECTS = ("chat_approvals", "chat_approval_events", "chat_approval_one_decision", "chat_approval_one_attempt",
               "chat_approval_one_redelivery", "chat_approval_one_written", "chat_approval_one_core_result",
               "chat_approval_events_next")


class Clock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        self.now += timedelta(seconds=1)
        return self.now


SHAPES = {"v0.11.25": snap.SCHEMA_V0_11_25, "main-f75acbc7": snap.SCHEMA_MAIN_F75ACBC7}


def build_released_store(tmp_path, shape):
    """A queue dir whose dispatch.db holds the coordinator tables of this checkout and the chat tables of ``shape``,
    with one row in each chat table, written by raw SQL (a released shape has no writer in this checkout)."""
    qdir = tmp_path / "consent"
    qdir.mkdir(mode=0o700)
    store = EnvelopeStore(qdir, clock=Clock())
    store.coord_set_agent("none")
    token = store.coord_take()
    d = store.accept_drop("fix the build", surface="cli")[0]
    thread = store.split(d.drop_id, [Part("work", "fix the build")])[0]
    ln = store.launch_request(token, harness="claude", role="worker", threads=[thread.thread_id])
    store.launch_activate(ln.launch_id, harness_session_id="sess-1")
    conn = store._connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        for stmt in SHAPES[shape]:
            conn.execute(stmt)
        at = "2026-10-27T12:00:00Z"

        def gseq() -> int:
            return conn.execute("INSERT INTO gseq (at) VALUES (?)", (at,)).lastrowid

        conn.execute("INSERT INTO chat_messages (message_id, gseq, thread_id, launch_id, recipient, author_kind, "
                     "author_surface, client_id, outbox_id, mode, expected_turn_id, owner_epoch, body, body_sha256, "
                     "body_norm_sha256, attachments, draft_ref, at) VALUES (?, ?, ?, ?, 'worker', 'operator', 'cli', "
                     "'c1', 'outbox-0001', 'queue', NULL, NULL, 'hello', 'h', 'n', '[]', NULL, ?)",
                     ("cm-" + "0" * 32, gseq(), thread.thread_id, ln.launch_id, at))
        conn.execute("INSERT INTO chat_outbox_events (message_id, seq, gseq, state, cause, by, at) VALUES (?, 1, ?, "
                     "'pending', 'queued', 'service', ?)", ("cm-" + "0" * 32, gseq(), at))
        conn.execute("INSERT INTO chat_events (launch_id, vendor_event_key, gseq, vendor_pos, kind, body, body_sha256, "
                     "body_norm_sha256, truncated, backfill, source, at) VALUES (?, 'agent_text:a1', ?, '1', "
                     "'agent_text', 'hi', 'h', 'n', 0, 0, 'vendor', ?)", (ln.launch_id, gseq(), at))
        conn.execute("INSERT INTO chat_cursors (launch_id, seq, gseq, vendor_cursor, complete_through, gap, at) "
                     "VALUES (?, 1, ?, 'c', '1', 0, ?)", (ln.launch_id, gseq(), at))
        conn.execute("INSERT INTO chat_launch_checks (launch_id, seq, gseq, kind, source, detail, by, at) VALUES (?, 1, "
                     "?, 'probing', 'activation', 'p', 'service', ?)", (ln.launch_id, gseq(), at))
        conn.execute("COMMIT")
    finally:
        conn.close()
    return qdir, thread.thread_id, ln.launch_id


def dump(path, tables=CHAT_TABLES):
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return {t: conn.execute(f"SELECT rowid, * FROM {t} ORDER BY rowid").fetchall() for t in tables}
    finally:
        conn.close()


def names(path):
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return {n for (n,) in conn.execute("SELECT name FROM sqlite_master")}
    finally:
        conn.close()


def run_show(qdir, thread, capsys):
    from levain.cli import main

    assert main(["chat", "show", thread, "--queue-dir", str(qdir), "--json"]) == 0
    return json.loads(capsys.readouterr().out)


@pytest.mark.parametrize("shape", list(SHAPES))
def test_released_store_opens_keeps_rows_and_gains_the_approval_branch(tmp_path, shape, capsys):
    qdir, thread, launch = build_released_store(tmp_path, shape)
    db = qdir / "dispatch.db"
    before = dump(db)
    assert not set(NEW_OBJECTS) & names(db)
    shown = run_show(qdir, thread, capsys)                                     # the shipped CLI path
    assert [r["kind"] for r in shown["timeline"]] == ["message", "vendor_agent_text"]
    assert set(NEW_OBJECTS) <= names(db)
    after = dump(db)
    for table, held in before.items():                  # every row, rowids included; an ALTERed table's rows gain NULLs
        assert [r[:len(h)] for r, h in zip(after[table], held)] == held and len(after[table]) == len(held)
        assert all(v is None for r, h in zip(after[table], held) for v in r[len(h):])
    conn = sqlite3.connect(db)
    try:
        assert conn.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
        view = conn.execute("SELECT sql FROM sqlite_master WHERE name = 'chat_timeline'").fetchone()[0]
        assert "chat_approval_events" in view and "chat_approvals" in view
        assert {r[1] for r in conn.execute("PRAGMA table_info(chat_launch_checks)")} >= {"canary_verdict_id",
                                                                                          "containment_digest"}
        assert conn.execute("SELECT COUNT(*) FROM chat_approvals").fetchone() == (0,)
    finally:
        conn.close()


@pytest.mark.parametrize("shape", list(SHAPES))
def test_migrated_store_takes_approval_rows_and_shows_them(tmp_path, shape, capsys):
    from levain.coordinator import chat_approvals as ca

    qdir, thread, launch = build_released_store(tmp_path, shape)
    run_show(qdir, thread, capsys)
    cs = ChatStore(EnvelopeStore(qdir, clock=Clock()))
    nonce = "0123456789abcdef0123456789abcdef"
    with cs._write() as (conn, now):
        got = ca.record_quarantine(conn, now, launch_id=launch, spawn_nonce=nonce, raw_line=b"x", transport_key="r")
        ca.append_event(conn, now, launch_id=launch, key=got.row.vendor_request_key, kind="core_started", by="service",
                        cause="t")
    kinds = [r["kind"] for r in run_show(qdir, thread, capsys)["timeline"]]
    assert kinds[-2:] == ["approval", "approval_core_started"] and kinds[:2] == ["message", "vendor_agent_text"]


@pytest.mark.parametrize("shape", list(SHAPES))
def test_second_open_changes_nothing(tmp_path, shape, capsys):
    qdir, thread, _launch = build_released_store(tmp_path, shape)
    run_show(qdir, thread, capsys)
    db = qdir / "dispatch.db"
    conn = sqlite3.connect(db)
    first = conn.execute("SELECT type, name, sql FROM sqlite_master ORDER BY type, name").fetchall()
    conn.close()
    run_show(qdir, thread, capsys)
    conn = sqlite3.connect(db)
    assert conn.execute("SELECT type, name, sql FROM sqlite_master ORDER BY type, name").fetchall() == first
    conn.close()


@pytest.mark.parametrize("shape", list(SHAPES))
def test_failed_integrity_check_rolls_the_whole_migration_back(tmp_path, shape, monkeypatch):
    qdir, _thread, _launch = build_released_store(tmp_path, shape)
    db = qdir / "dispatch.db"
    before_rows, before_names = dump(db), names(db)
    monkeypatch.setattr(chat, "_integrity", lambda conn: [("row 3 missing from index",)])
    with pytest.raises(CoordError, match="integrity"):
        ChatStore(EnvelopeStore(qdir, clock=Clock()))._connect()
    monkeypatch.undo()
    assert names(db) == before_names and dump(db) == before_rows


@pytest.mark.parametrize("shape", list(SHAPES))
def test_read_only_opener_migrates_nothing_and_still_reads_an_older_store(tmp_path, shape, capsys):
    qdir, thread, _launch = build_released_store(tmp_path, shape)
    db = qdir / "dispatch.db"
    for side in ("-wal", "-shm"):
        if os.path.exists(str(db) + side):
            pytest.skip("a live journal sidecar: a checkpointed copy is the read-only fixture")
    digest = hashlib.sha256(db.read_bytes()).hexdigest()
    os.chmod(db, stat.S_IRUSR)
    try:
        shown = run_show(qdir, thread, capsys)
        assert [r["kind"] for r in shown["timeline"]] == ["message", "vendor_agent_text"]
        assert hashlib.sha256(db.read_bytes()).hexdigest() == digest
        assert "chat_approvals" not in names(db)
        cs = ChatStore(EnvelopeStore(qdir, clock=Clock()))
        from levain.coordinator.chat_approvals import ambiguous_stops

        assert cs._read(lambda conn: ambiguous_stops(conn, _launch)) == []         # a missing table is "none", not a crash
    finally:
        os.chmod(db, stat.S_IRUSR | stat.S_IWUSR)


def test_snapshots_are_the_released_shapes():
    assert "chat_echoes" not in "".join(snap.SCHEMA_V0_11_25) and "chat_echoes" in "".join(snap.SCHEMA_MAIN_F75ACBC7)
    assert not any("chat_approval" in s for s in snap.SCHEMA_MAIN_F75ACBC7 + snap.SCHEMA_V0_11_25)
    assert snap.ADDED_V0_11_25 == () and snap.ADDED_MAIN_F75ACBC7 == chat.CHAT_ADDED_COLUMNS

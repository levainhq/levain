"""The read-only consent listing (Q6a): what the cockpit page shows of the queue, read without ConsentQueue's
mkdir / schema / sweep path."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from levain.consent.listing import UNSHOWN, audit_line, consent_targets, filed_input, read_listing
from levain.consent.queue import ConsentQueue, QueueStoreError, authority_source

T0 = datetime(2026, 10, 15, 12, 0, 0, tzinfo=timezone.utc)


def _queue(tmp_path: Path, clock) -> ConsentQueue:
    qdir = tmp_path / "consent"
    qdir.mkdir(mode=0o700)
    return ConsentQueue(qdir, authority=authority_source(qdir, anchor=None), clock=clock)


def _file(queue: ConsentQueue, event: str, hold_min: int = 5):
    return queue.file(agent="claude_code", agent_session_id="s", tool_name="Write",
                      tool_input={"file_path": "/w/a.txt", "content": "hi"}, cwd="/w",
                      hold=timedelta(minutes=hold_min), event_id=event)


def test_a_missing_queue_is_an_empty_listing_and_nothing_is_created(tmp_path):
    got = read_listing(tmp_path / "nope", now=T0)
    assert (got.pending, got.recent, got.skipped) == ((), (), 0)
    assert not (tmp_path / "nope").exists()
    (tmp_path / "empty").mkdir(mode=0o700)          # a directory with no queue.db: still nothing made in it
    assert read_listing(tmp_path / "empty", now=T0).pending == ()
    assert list((tmp_path / "empty").iterdir()) == []


def test_pending_and_terminal_requests_are_listed_and_the_row_is_not_swept(tmp_path):
    q = _queue(tmp_path, lambda: T0)
    waiting, gone = _file(q, "e1"), _file(q, "e2")
    assert q.withdraw(gone.id)
    got = read_listing(q.directory, now=T0 + timedelta(minutes=1))
    assert [e.request.id for e in got.pending] == [waiting.id]
    assert [(e.request.id, e.state) for e in got.recent] == [(gone.id, "withdrawn")]
    assert audit_line(got.recent[0]) == "withdrawn"
    # a terminal request older than 24 hours is not listed
    assert read_listing(q.directory, now=T0 + timedelta(hours=25)).recent == ()


def test_a_pending_past_its_expiry_reads_as_expired_without_a_write(tmp_path):
    q = _queue(tmp_path, lambda: T0)
    r = _file(q, "e1", hold_min=5)
    before = (q.directory / "queue.db").read_bytes()
    got = read_listing(q.directory, now=T0 + timedelta(minutes=6))
    assert got.pending == () and [(e.request.id, e.state) for e in got.recent] == [(r.id, "expired")]
    assert audit_line(got.recent[0]).startswith("expired: no answer")
    assert q.directory.joinpath("queue.db").read_bytes() == before        # nobody swept it: the row is untouched
    with sqlite3.connect(q.directory / "queue.db") as conn:
        assert conn.execute("SELECT state FROM requests").fetchone() == ("pending",)


def test_a_tampered_row_is_skipped_and_counted(tmp_path):
    q = _queue(tmp_path, lambda: T0)
    _file(q, "e1")
    keep = _file(q, "e2")
    conn = sqlite3.connect(q.directory / "queue.db", isolation_level=None)
    conn.execute("DROP TRIGGER requests_frozen")
    conn.execute("UPDATE requests SET display = 'Write {\"file_path\": \"/etc/passwd\"}' WHERE event_id = 'hook:e1'")
    conn.close()
    got = read_listing(q.directory, now=T0)
    assert [e.request.id for e in got.pending] == [keep.id] and got.skipped == 1


def test_a_directory_others_can_reach_is_refused(tmp_path):
    q = _queue(tmp_path, lambda: T0)
    q.directory.chmod(0o755)
    with pytest.raises(QueueStoreError):
        read_listing(q.directory, now=T0)


def test_the_shared_readers_show_the_targets_and_an_unreadable_display(tmp_path):
    q = _queue(tmp_path, lambda: T0)
    r = _file(q, "e1")
    assert filed_input(r) == {"file_path": "/w/a.txt", "content": "hi"}
    assert consent_targets(r) == "/w/a.txt"
    big = q.file(agent="claude_code", agent_session_id="s", tool_name="Write",
                 tool_input={"file_path": "/w/b", "content": "x" * 50000}, cwd="/w", hold=timedelta(minutes=5),
                 event_id="e9")
    assert filed_input(big) is UNSHOWN

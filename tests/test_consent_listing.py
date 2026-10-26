"""The read-only consent listing (Q6a): what the cockpit page shows of the queue, read without ConsentQueue's
mkdir / schema / sweep path."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from levain.consent.listing import (TOO_LONG, UNSHOWN, audit_line, consent_targets, entry_body, filed_input, find_entry,
                                    read_listing)
from levain.consent.queue import ConsentQueue, QueueStoreError, authority_source
from tests._h4 import h4_home

T0 = datetime(2026, 10, 15, 12, 0, 0, tzinfo=timezone.utc)


def _queue(tmp_path: Path, clock) -> ConsentQueue:
    qdir = tmp_path / "consent"
    qdir.mkdir(mode=0o700)
    return ConsentQueue(qdir, authority=authority_source(qdir, anchor=None), clock=clock, home=h4_home())


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
    assert audit_line(got.recent[0]) == ("withdrawn to the agent's own dialog; in the agent's dialog: no outcome "
                                         "seen yet, unsigned")
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


def test_a_request_whose_display_is_cut_offers_no_commands(tmp_path):
    q = _queue(tmp_path, lambda: T0)
    short = _file(q, "e1")
    big = q.file(agent="claude_code", agent_session_id="s", tool_name="Write",
                 tool_input={"file_path": "/w/b", "content": "x" * 50000}, cwd="/w", hold=timedelta(minutes=5),
                 event_id="e9")
    got = read_listing(q.directory, now=T0 + timedelta(minutes=1))
    cut = entry_body(find_entry(got, big.id))
    assert cut["answer_allow"] is None and cut["answer_deny"] is None and cut["note"] == TOO_LONG
    whole = entry_body(find_entry(got, short.id))
    assert whole["answer_deny"] == f"levain consent answer {short.id} deny --signing-key <key>" and whole["note"] == ""
    assert find_entry(got, "pend-unknown") is None


def test_pending_past_the_cap_is_counted_not_read(tmp_path, monkeypatch):
    import levain.consent.listing as listing

    monkeypatch.setattr(listing, "PENDING_CAP", 2)
    q = _queue(tmp_path, lambda: T0)
    for i in range(3):
        _file(q, f"e{i}")
    got = read_listing(q.directory, now=T0 + timedelta(minutes=1))
    assert len(got.pending) == 2 and got.overflow is True and got.skipped == 0


def test_past_the_cap_the_soonest_expiring_are_kept_and_overflow_is_flagged_without_a_count(tmp_path):
    q = _queue(tmp_path, lambda: T0)
    made = [_file(q, f"e{i}", hold_min=5 + i) for i in range(201)]        # the last one expires latest
    got = read_listing(q.directory, now=T0 + timedelta(minutes=1))
    ids = {e.request.id for e in got.pending}
    assert len(got.pending) == 200 and got.overflow is True
    assert made[0].id in ids and made[199].id in ids and made[200].id not in ids
    exact = read_listing(q.directory, now=T0 + timedelta(minutes=1))      # 200 would be no overflow: drop one
    q.withdraw(made[200].id)
    assert read_listing(q.directory, now=T0 + timedelta(minutes=1)).overflow is False and exact.overflow


def test_unswept_expired_pending_do_not_crowd_live_ones_out_of_the_cap(tmp_path, monkeypatch):
    import levain.consent.listing as listing

    monkeypatch.setattr(listing, "PENDING_CAP", 2)
    q = _queue(tmp_path, lambda: T0)
    stale = [_file(q, f"x{i}", hold_min=1) for i in range(3)]
    live = _file(q, "live", hold_min=30)
    got = read_listing(q.directory, now=T0 + timedelta(minutes=10))
    assert [e.request.id for e in got.pending] == [live.id] and got.overflow is False
    assert {e.request.id for e in got.recent} == {r.id for r in stale} and {e.state for e in got.recent} == {"expired"}


def _filed(tmp_path, tool, call):
    q = _queue(tmp_path, lambda: T0)
    return q.file(agent="claude_code", agent_session_id="s", tool_name=tool, tool_input=call, cwd="/w",
                  hold=timedelta(minutes=5), event_id="e1")


def test_a_publish_row_names_the_package_version_and_target(tmp_path):
    r = _filed(tmp_path, "levain_publish", {"index": "pypi", "target": "https://upload.pypi.org/legacy/",
                                             "package": "levain", "version": "1.0.0", "files": [{"name": "a.whl"}]})
    assert consent_targets(r) == "publish levain 1.0.0 to pypi (https://upload.pypi.org/legacy/)"


def test_a_limit_row_names_the_agent_session_kind_count_and_extension(tmp_path):
    r = _filed(tmp_path, "levain_limit", {"agent": "codex", "session": "sess-1", "limit": "turns", "now": "50",
                                          "extend_to": "100", "count": "51"})
    assert consent_targets(r) == "codex session sess-1 limit turns count 51 extend_to 100"
    (tmp_path / "x").mkdir()
    e = _filed(tmp_path / "x", "levain_limit",
               {"agent": "codex", "session": "s2", "limit": "turns", "now": "50", "extend_to": "100",
                "purpose": "extend"})
    assert consent_targets(e) == "codex session s2 limit turns count (absent) extend_to 100"


def test_a_publish_or_limit_row_with_missing_or_odd_fields_shows_absent(tmp_path):
    r = _filed(tmp_path, "levain_publish", {"index": "npm", "package": ["x"], "files": []})
    assert consent_targets(r) == "publish (absent) (absent) to npm ((absent))"
    (tmp_path / "y").mkdir()
    s = _filed(tmp_path / "y", "levain_limit", {"agent": "codex"})
    assert consent_targets(s) == "codex session (absent) limit (absent) count (absent) extend_to (absent)"

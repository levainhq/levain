"""The cockpit Inbox's read model (1101-6): one merged list of what Levain pinged the operator (pings and notices), a
per-row read mark held on the server so two devices agree, a floor so an upgrade does not turn history into unread."""
from __future__ import annotations

import sqlite3
import time

import pytest

from levain.webpush.store import PushStore


def T(days_ago):
    return time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(time.time() - days_ago * 86400))


def _notice(st, title="n", decision="recorded", priority="normal"):
    if decision == "recorded":
        return None, st.add_notice("Backup", title, "body", priority, "Backup", decision)
    return st.record_notice("notify", "notify:Backup", "Backup: " + title, "Backup", title, "body", priority, "Backup", decision,
                            hold=("/?view=notices", "sleep") if decision == "held" else None)


def test_the_list_merges_pings_and_notices_newest_first_without_duplicating_a_notices_ping(tmp_path):
    st = PushStore(tmp_path)
    p1 = st.add_ping("attention", "digest", "Daily Email Digest", "first line")
    _notice(st, "pushed one", "pushed")                    # a ping of its own, shown once, as the notice
    st.add_ping("summary", None, "levain", "2 pings held")   # a summary only links; it is not an item
    _notice(st, "recorded one")
    rows = st.inbox()
    assert [r["title"] for r in rows] == ["recorded one", "pushed one", "Daily Email Digest"]
    assert [r["kind"] for r in rows] == ["notice", "notice", "ping"]
    assert rows[2]["id"] == f"p{p1}" and rows[0]["id"].startswith("n")
    assert set(rows[0]) >= {"id", "kind", "title", "body", "at", "source", "priority", "read", "decision", "opened"}


def test_read_marks_are_per_row_and_the_count_follows(tmp_path):
    st = PushStore(tmp_path)
    a = st.add_ping("attention", "a", "A", "x")
    _, n = _notice(st, "N")
    assert st.inbox_counts() == {"unread": 2, "pings": 1, "notices": 1}
    assert st.mark_inbox_read(f"n{n}") is True
    assert [(r["id"], r["read"]) for r in st.inbox()] == [(f"n{n}", True), (f"p{a}", False)]
    assert st.inbox_counts()["unread"] == 1
    assert st.mark_inbox_read(f"n{n}") is True            # again: still there, nothing changes
    assert st.mark_inbox_read("n99999") is False and st.mark_inbox_read("zz") is False and st.mark_inbox_read("p0") is False


def test_up_to_marks_that_item_and_everything_older_and_all_marks_everything(tmp_path):
    st = PushStore(tmp_path)
    old = st.add_ping("attention", "a", "old", "x")
    _, n = _notice(st, "mid")
    new = st.add_ping("attention", "b", "new", "x")
    with st.write() as c:       # distinct seconds, so "older" is decided by the clock the way the list is
        c.execute("DROP TRIGGER pings_frozen")
        c.execute("UPDATE pings SET at = ? WHERE id = ?", (T(3), old))
        c.execute("UPDATE notices SET at = ? WHERE id = ?", (T(2), n))
        c.execute("UPDATE pings SET at = ? WHERE id = ?", (T(1), new))
    assert st.mark_inbox_read_up_to(f"n{n}") is True
    assert {r["id"]: r["read"] for r in st.inbox()} == {f"p{old}": True, f"n{n}": True, f"p{new}": False}
    assert st.mark_inbox_read_up_to("p99999") is False
    st.mark_inbox_read_all()
    assert st.inbox_counts()["unread"] == 0


def test_a_dismissed_notice_leaves_the_list_and_the_count(tmp_path):
    st = PushStore(tmp_path)
    _, n = _notice(st, "gone")
    st.dismiss_notice(n)
    assert st.inbox() == [] and st.inbox_counts()["unread"] == 0


def test_an_upgrade_from_the_schema_before_the_inbox_marks_history_read_and_new_rows_unread(tmp_path):
    st = PushStore(tmp_path)
    st.add_ping("attention", "a", "before", "x")
    _notice(st, "before notice")
    # take the store back to the schema as it was: no inbox_reads table, no floors
    with sqlite3.connect(st.path) as c:
        c.execute("DROP TABLE inbox_reads")
        c.execute("DELETE FROM meta WHERE key LIKE 'inbox_floor%'")
    up = PushStore(tmp_path)                 # a fresh opener runs _ensure_schema on the old file
    after = up.add_ping("attention", "b", "after", "x")
    rows = {r["title"]: r["read"] for r in up.inbox()}
    assert rows == {"after": False, "before": True, "before notice": True}
    assert up.inbox_counts()["unread"] == 1 and up.mark_inbox_read(f"p{after}")
    assert PushStore(tmp_path).inbox_counts()["unread"] == 0     # a second open changes no floor


def test_a_pruned_notice_takes_its_read_mark_with_it(tmp_path):
    st = PushStore(tmp_path)
    _, n = _notice(st, "old")
    st.mark_inbox_read(f"n{n}")
    with st.write() as c:
        c.execute("DELETE FROM notices WHERE id = ?", (n,))
    _notice(st, "new")                       # the next insert prunes the stale mark
    with st.read() as c:
        assert c.execute("SELECT COUNT(*) FROM inbox_reads WHERE item = ?", (f"n{n}",)).fetchone()[0] == 0

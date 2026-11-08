"""A coordinator store built before BOTH table rebuilds opens once and keeps every row of both tables.

C11 S4 rebuilds ``replies`` (``migrate_reply_kinds``: the ``coordinator_reply`` kind) and P8a rebuilds ``review_items``
(``migrate_items``: the ``agent`` feed). Each rebuild has its own test on a store old in one table only; this one opens a
store old in both at once and checks every row, event and sequence number of each table, the foreign keys, the triggers
and the integrity of the file, then that a second open changes nothing.
"""
from __future__ import annotations

import sqlite3

import pytest

from levain.coordinator import review
from levain.coordinator.store import REPLY_KINDS, CoordStore, Part
from tests.test_coordinator_drop_reply import _downgrade, _drop
from tests.test_coordinator_review import Clock, _old_items_store, _snapshot


@pytest.fixture
def qdir(tmp_path):
    d = tmp_path / "consent"
    d.mkdir(mode=0o700)
    return d


def _replies(conn):
    return conn.execute("SELECT rowid, reply_id, thread_id, kind, ref_store, ref_id, body, at, gseq FROM replies "
                        "ORDER BY rowid").fetchall()


def _store_old_in_both(qdir):
    """Replies and review items filed through the current code, then each table rewritten as main made it before its own
    rebuild: ``replies`` with the older kinds, ``review_items`` with the older feeds. Returns both tables' rows."""
    store = CoordStore(qdir, clock=Clock())
    drop = _drop(store)
    store.split(drop, [Part(kind="work", title="t")], by="operator")
    conn, keep, cleared = _old_items_store(store)
    items_before = _snapshot(conn)
    conn.close()
    _downgrade(qdir)
    raw = sqlite3.connect(qdir / "dispatch.db")
    try:
        replies_before = _replies(raw)
    finally:
        raw.close()
    assert len(replies_before) >= 2 and all(r[-1] is not None for r in replies_before)      # every reply holds its place in the store-wide order
    return keep, cleared, items_before, replies_before


def test_a_store_old_in_replies_and_in_review_items_opens_once_and_keeps_every_row_of_both(qdir):
    keep, cleared, items_before, replies_before = _store_old_in_both(qdir)
    raw = sqlite3.connect(qdir / "dispatch.db")
    try:
        assert "'agent'" not in raw.execute("SELECT sql FROM sqlite_master WHERE name = 'review_items'").fetchone()[0]
        assert "coordinator_reply" not in raw.execute("SELECT sql FROM sqlite_master WHERE name = 'replies'").fetchone()[0]
        with pytest.raises(sqlite3.IntegrityError):               # the old replies table refuses the new kind
            raw.execute("INSERT INTO replies (reply_id, kind, ref_store, ref_id, body, at) VALUES ('rp-x', "
                        "'coordinator_reply', 'drops', 'd', '{}', 'x')")
    finally:
        raw.close()
    fresh = CoordStore(qdir, clock=Clock())                       # the ordinary opener runs both rebuilds
    item, made = review.file_item(fresh, "agent", {"reason": "dream"}, dedupe_key="agent:both")
    assert made and item.feed == "agent"                          # review_items accepts the new feed
    with fresh._write() as (c, _now):
        assert _replies(c) == replies_before                      # every reply row, rowid and gseq kept
        rows, events, triggers = _snapshot(c)
        assert rows[:len(items_before[0])] == items_before[0] and len(rows) == len(items_before[0]) + 1
        assert events == items_before[1]                          # every review event kept
        assert c.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert c.execute("PRAGMA foreign_key_check").fetchall() == []
        assert c.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
        assert "coordinator_reply" in c.execute("SELECT sql FROM sqlite_master WHERE name = 'replies'").fetchone()[0]
        assert "coordinator_reply" in REPLY_KINDS
        assert {"replies_bound_drop", "replies_bound_once", "replies_bound_window", "replies_gseq_no_replace",
                "replies_frozen"} <= set(triggers)                # both tables' triggers came back with their rebuild
        assert set(items_before[2]) < set(triggers)
    again = CoordStore(qdir, clock=Clock())                       # a second open: nothing left to rebuild
    with again._write() as (c, _now):
        assert _replies(c) == replies_before and _snapshot(c) == (rows, events, triggers)
    assert {i.item_id: i.state for i in review.list_items(again, include_cleared=True)} == {
        keep.item_id: "open", cleared.item_id: "cleared", item.item_id: "open"}

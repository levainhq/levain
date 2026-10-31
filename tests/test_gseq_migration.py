"""C15 S0: the store-wide order reaches the nine tables that lacked it, and a hole in it stays visible.

``replies`` had its ``gseq`` since CH-D1 §2.8; S0 gives the same column, unique index and no-replace trigger to drops,
threads, thread_revisions, thread_events, review_items, review_events, first_reads, team_drafts and team_draft_events,
writes each table's ``gseq_migration_bounds`` row in the migration's own transaction, and makes every INSERT site take
``next_gseq`` in its own transaction. A row an older binary writes afterwards holds NULL past the bound: ``unsequenced``
counts it, and keeps counting it after any number of newer writes (a count that moved with the newest gseq would lose it).
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from levain.coordinator import review
from levain.coordinator import teamdrafts as T
from levain.coordinator.seam_classes import SeamClassError, unsequenced
from levain.coordinator.store import GSEQ_TABLES, CoordStore, Part
from tests.test_coordinator_first_reader import Band
from tests.test_coordinator_envelope import ctx, keys, repo, snapshot  # noqa: F401 - fixtures
from tests.test_review_clear_combined import _pre_r4_store, _strip_s0

NEW_TABLES = tuple(t for t in GSEQ_TABLES if t != "replies")


def _store(tmp_path: Path, name: str = "consent") -> CoordStore:
    d = tmp_path / name
    d.mkdir(mode=0o700)
    return CoordStore(d)


def _gseqs(path: Path) -> dict[str, list]:
    conn = sqlite3.connect(path)
    try:
        return {t: [r[0] for r in conn.execute(f"SELECT gseq FROM {t} ORDER BY rowid")] for t in GSEQ_TABLES}
    finally:
        conn.close()


def _bounds(path: Path) -> dict[str, int]:
    conn = sqlite3.connect(path)
    try:
        return dict(conn.execute("SELECT table_name, baseline_rowid FROM gseq_migration_bounds"))
    finally:
        conn.close()


def _unsequenced(path: Path) -> dict[str, int]:
    conn = sqlite3.connect(path)
    try:
        return {t: unsequenced(conn, t) for t in GSEQ_TABLES}
    finally:
        conn.close()


def _old_store(tmp_path: Path) -> CoordStore:
    """A store as it was before S0: rows in every one of the ten tables, none of the nine holding a gseq column, no
    bounds table. Built by the current code, then stripped (:func:`_strip_s0`), every row kept as it was. Returns the
    store object that wrote it (open the file again with a new ``CoordStore(s.directory)``)."""
    s = _store(tmp_path, "old")
    drop, _ = s.accept_drop("hello there, this is an old drop", surface="cli")
    other, _ = s.accept_drop("a second old drop", surface="cli")
    (thread,) = s.split(drop.drop_id, [Part("work", "an old thread")])
    s.set_state(thread.thread_id, "closed", by="operator")
    item, _ = review.file_item(s, "seat", {"text": "an old item"}, dedupe_key="old-1")
    review.clear_item(s, item.item_id)
    draft = T.add_draft(s, "finding", summary="an old draft", by="operator")
    T.withdraw_draft(s, draft["draft_id"], by="operator")
    assert other.drop_id != drop.drop_id
    _strip_s0(s.path)
    return s


def test_a_store_made_before_s0_opens_with_every_old_row_null_and_a_bound_recorded(tmp_path: Path) -> None:
    s = _old_store(tmp_path)
    conn = sqlite3.connect(s.path)
    try:
        assert "gseq_migration_bounds" not in {r[0] for r in conn.execute("SELECT name FROM sqlite_master")}
        for t in NEW_TABLES:
            assert "gseq" not in {r[1] for r in conn.execute(f"PRAGMA table_info({t})")}
        counts = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in GSEQ_TABLES}
        replies_before = [r[0] for r in conn.execute("SELECT gseq FROM replies ORDER BY rowid")]
    finally:
        conn.close()
    # the real run: every table holds old rows (drops 2, threads 1, thread_revisions 1, thread_events 4, review_items 1,
    # review_events 1, team_drafts 1, team_draft_events 2); first_reads is empty here (its own test below)
    assert counts == {"replies": 3, "drops": 2, "threads": 1, "thread_revisions": 1, "thread_events": 4,
                      "review_items": 1, "review_events": 1, "first_reads": 0, "team_drafts": 1,
                      "team_draft_events": 2}

    CoordStore(s.directory)._connect().close()               # the first open under the new code

    got = _gseqs(s.path)
    assert got["replies"] == replies_before                      # replies kept the numbers it already held
    for t in NEW_TABLES:
        assert got[t] == [None] * counts[t], t                   # every old row legacy: NULL
    assert _bounds(s.path) == counts                              # each table's largest rowid, replies' too
    assert _unsequenced(s.path) == {t: 0 for t in GSEQ_TABLES}   # the first open after migration reports no hole
    conn = sqlite3.connect(s.path)
    try:
        names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master")}
    finally:
        conn.close()
    for t in GSEQ_TABLES:
        assert {f"{t}_gseq", f"{t}_gseq_no_replace"} <= names


def test_opening_again_changes_nothing_and_a_bound_never_moves(tmp_path: Path) -> None:
    s = _old_store(tmp_path)
    CoordStore(s.directory)._connect().close()
    bounds = _bounds(s.path)
    CoordStore(s.directory).accept_drop("a new drop after the migration", surface="cli")
    CoordStore(s.directory)._connect().close()
    assert _bounds(s.path) == bounds                              # written once, in the migration's own transaction
    conn = sqlite3.connect(s.path)
    try:
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute("UPDATE gseq_migration_bounds SET baseline_rowid = 99")
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute("DELETE FROM gseq_migration_bounds")
        with pytest.raises(sqlite3.IntegrityError, match="never replaced"):
            conn.execute("INSERT OR REPLACE INTO gseq_migration_bounds VALUES ('drops', 99)")
    finally:
        conn.close()
    assert _bounds(s.path) == bounds


def test_a_new_write_through_each_public_method_takes_a_unique_increasing_gseq(tmp_path: Path) -> None:
    s = _store(tmp_path)
    s._connect().close()
    seen: list[tuple[str, dict[str, list[int]]]] = []
    before = _gseqs(s.path)

    def step(label, fn):
        nonlocal before
        out = fn()
        now = _gseqs(s.path)
        seen.append((label, {t: now[t][len(before[t]):] for t in GSEQ_TABLES if len(now[t]) > len(before[t])}))
        before = now
        return out

    drop = step("accept a drop", lambda: s.accept_drop("hello there", surface="cli")[0])
    (thread,) = step("split a thread", lambda: s.split(drop.drop_id, [Part("work", "w")]))
    step("set a thread state", lambda: s.set_state(thread.thread_id, "closed", by="operator"))
    item = step("file a review item", lambda: review.file_item(s, "seat", {"text": "a"}, dedupe_key="a")[0])
    step("clear a review item", lambda: review.clear_item(s, item.item_id))
    draft = step("a team draft", lambda: T.add_draft(s, "finding", summary="s", by="operator"))
    step("withdraw it", lambda: T.withdraw_draft(s, draft["draft_id"], by="operator"))

    # which tables each call wrote, from the real run (a reply takes its own gseq: the drop's and the split's receipts)
    assert [(label, sorted(got)) for label, got in seen] == [
        ("accept a drop", ["drops", "replies"]),
        ("split a thread", ["replies", "thread_events", "thread_revisions", "threads"]),
        ("set a thread state", ["thread_events"]),
        ("file a review item", ["review_items"]),
        ("clear a review item", ["review_events"]),
        ("a team draft", ["team_draft_events", "team_drafts"]),
        ("withdraw it", ["team_draft_events"]),
    ]
    assert [len(got["thread_events"]) for _l, got in seen if "thread_events" in got] == [3, 1]
    flat = [g for _label, got in seen for gs in got.values() for g in gs]
    assert all(isinstance(g, int) for g in flat) and len(flat) == len(set(flat))         # unique, across every table
    for (_a, earlier), (_b, later) in zip(seen, seen[1:]):                               # increasing in call order
        assert max(g for gs in earlier.values() for g in gs) < min(g for gs in later.values() for g in gs)
    assert _unsequenced(s.path) == {t: 0 for t in GSEQ_TABLES}


def test_a_first_read_takes_a_gseq_after_its_reply_and_item(ctx, keys, repo, snapshot):  # noqa: F811
    band = Band(ctx)
    q = band.ask()
    item, reply_id = band.answer(q)
    path = band.store.path
    conn = sqlite3.connect(path)
    try:
        (read_g,) = conn.execute("SELECT gseq FROM first_reads WHERE question_id = ?", (q.id,)).fetchone()
        (reply_g,) = conn.execute("SELECT gseq FROM replies WHERE reply_id = ?", (reply_id,)).fetchone()
        (item_g,) = conn.execute("SELECT gseq FROM review_items WHERE item_id = ?", (item.item_id,)).fetchone()
    finally:
        conn.close()
    assert reply_g < item_g < read_g                                # the answer's own three rows, in write order
    assert _unsequenced(path) == {t: 0 for t in GSEQ_TABLES}
    q2 = band.ask("another one?")
    from levain.coordinator import first_reader as fr

    fr.escalate(band.store, q2.id, "his to decide", token=band.token)
    conn = sqlite3.connect(path)
    try:
        g = [r[0] for r in conn.execute("SELECT gseq FROM first_reads ORDER BY rowid")]
    finally:
        conn.close()
    assert len(g) == 2 and g[0] == read_g and g[1] > g[0]


def test_an_old_binary_row_is_counted_and_stays_counted_after_new_writes(tmp_path: Path) -> None:
    s = _old_store(tmp_path)
    store = CoordStore(s.directory)
    store._connect().close()
    assert _unsequenced(s.path)["drops"] == 0

    def old_binary_drop(n: int) -> None:
        # what a binary that does not know the column writes: a plain INSERT without it (the column allows NULL)
        conn = sqlite3.connect(s.path, isolation_level=None)
        try:
            conn.execute("INSERT INTO drops (drop_id, surface, outbox_id, words, words_sha256, sent_at, received_at, "
                         "source_how) VALUES (?, 'cli', ?, 'w', 'h', 't', 't', 'none')",
                         (f"dr-{n:032x}", f"old-{n}"))
        finally:
            conn.close()

    old_binary_drop(1)
    assert _unsequenced(s.path)["drops"] == 1
    assert _gseqs(s.path)["drops"][-1] is None
    # new-code writes land AFTER it, with higher gseq numbers than anything the old row could have held
    store.accept_drop("a newer drop by the new code", surface="cli")
    assert _gseqs(s.path)["drops"][-1] is not None
    assert _unsequenced(s.path)["drops"] == 1                      # the anti-monotonic bug: it must NOT fall to zero
    review.file_item(store, "seat", {"text": "later"}, dedupe_key="later")
    store.accept_drop("and another", surface="cli")
    assert _unsequenced(s.path)["drops"] == 1
    old_binary_drop(2)
    assert _unsequenced(s.path)["drops"] == 2
    store.accept_drop("one more by the new code", surface="cli")
    assert _unsequenced(s.path)["drops"] == 2
    # the tables the old binary never touched stay clean
    assert {t: n for t, n in _unsequenced(s.path).items() if t != "drops"} == {t: 0 for t in GSEQ_TABLES
                                                                              if t != "drops"}
    # a row the migration FOUND stays legacy however the table grows
    assert _gseqs(s.path)["drops"][:2] == [None, None]
    assert _bounds(s.path)["drops"] == 2


def test_a_table_never_migrated_has_no_bound_and_is_refused(tmp_path: Path) -> None:
    s = _store(tmp_path)
    s._connect().close()
    conn = sqlite3.connect(s.path)
    try:
        with pytest.raises(SeamClassError, match="never migrated"):
            unsequenced(conn, "chat_messages")
        with pytest.raises(SeamClassError, match="not a table name"):
            unsequenced(conn, "drops; DROP TABLE drops")
    finally:
        conn.close()


def test_a_replacing_insert_that_reuses_a_gseq_is_refused_by_the_trigger(tmp_path: Path) -> None:
    s = _store(tmp_path)
    drop, _ = s.accept_drop("hello there", surface="cli")
    item, _ = review.file_item(s, "seat", {"text": "a"}, dedupe_key="a")
    conn = sqlite3.connect(s.path, isolation_level=None)
    try:
        (g,) = conn.execute("SELECT gseq FROM drops WHERE drop_id = ?", (drop.drop_id,)).fetchone()
        with pytest.raises(sqlite3.IntegrityError, match="a drop is never replaced"):
            conn.execute("INSERT OR REPLACE INTO drops (drop_id, surface, outbox_id, words, words_sha256, sent_at, "
                         "received_at, source_how, gseq) VALUES ('dr-" + "1" * 32 + "', 'cli', 'x', 'w', 'h', 't', 't', "
                         "'none', ?)", (g,))
        (ig,) = conn.execute("SELECT gseq FROM review_items WHERE item_id = ?", (item.item_id,)).fetchone()
        with pytest.raises(sqlite3.IntegrityError, match="a review item is never replaced"):
            conn.execute("INSERT OR REPLACE INTO review_items (item_id, thread_id, feed, dedupe_key, body, "
                         "authority_relevant, by, at, gseq) VALUES ('rv-z', NULL, 'seat', 'k', '{}', 0, 'service', 't', ?)",
                         (ig,))
        # NULL is allowed any number of times: that is what an older binary writes
        for n in (1, 2):
            conn.execute("INSERT INTO drops (drop_id, surface, outbox_id, words, words_sha256, sent_at, received_at, "
                         "source_how) VALUES (?, 'cli', ?, 'w', 'h', 't', 't', 'none')", (f"dr-{n:032x}", f"o{n}"))
    finally:
        conn.close()


def test_the_review_events_rebuild_keeps_the_gseq_column_index_and_trigger(tmp_path: Path, monkeypatch) -> None:
    d, items = _pre_r4_store(tmp_path, monkeypatch)                # a pre-R4 store (six columns), every S0 object stripped
    path = d / "dispatch.db"
    conn = sqlite3.connect(path)
    try:
        assert [r[1] for r in conn.execute("PRAGMA table_info(review_events)")][-1] == "at"
        old_events = conn.execute("SELECT COUNT(*) FROM review_events").fetchone()[0]
    finally:
        conn.close()
    assert old_events == 5

    CoordStore(d)._connect().close()           # migrate_events rebuilds review_events (via review_events_r4), then S0

    conn = sqlite3.connect(path)
    try:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(review_events)")]
        assert cols == ["item_id", "seq", "op", "mode", "by", "at", "sign_request_id", "gseq"]
        names = {r[0]: r[1] for r in conn.execute("SELECT name, tbl_name FROM sqlite_master")}
        assert names["review_events_gseq"] == "review_events" and names["review_events_gseq_no_replace"] == "review_events"
        assert "review_events_r4" not in names
        assert conn.execute("SELECT baseline_rowid FROM gseq_migration_bounds WHERE table_name = 'review_events'"
                            ).fetchone() == (5,)
        assert [r[0] for r in conn.execute("SELECT gseq FROM review_events ORDER BY rowid")] == [None] * 5
        assert conn.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
    finally:
        conn.close()

    review.reopen_item(CoordStore(d), items[0].item_id)             # a new event takes its gseq and the old five stay legacy
    conn = sqlite3.connect(path)
    try:
        got = [r[0] for r in conn.execute("SELECT gseq FROM review_events ORDER BY rowid")]
        assert got[:5] == [None] * 5 and isinstance(got[5], int)
        assert unsequenced(conn, "review_events") == 0
        with pytest.raises(sqlite3.IntegrityError, match="a review event is never replaced"):
            conn.execute("INSERT OR REPLACE INTO review_events (item_id, seq, op, mode, by, at, gseq) "
                         "VALUES (?, 9, 'cleared', 'manual', 'operator', 't', ?)", (items[1].item_id, got[5]))
    finally:
        conn.close()


def test_unsequenced_on_a_store_no_writer_has_migrated_is_a_seam_class_error(tmp_path: Path) -> None:
    """A display's read-only connection reads a store no writer has migrated (no ``gseq_migration_bounds`` table): the
    count says so in :class:`SeamClassError`, not in a raw ``sqlite3.OperationalError: no such table``."""
    s = _old_store(tmp_path)                                     # stripped: no bounds table, never opened by the new code
    conn = s.open_readonly()
    try:
        assert "gseq_migration_bounds" not in {r[0] for r in conn.execute("SELECT name FROM sqlite_master")}
        with pytest.raises(SeamClassError, match="drops: the store has not been migrated"):
            unsequenced(conn, "drops")
    finally:
        conn.close()

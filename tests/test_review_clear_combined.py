"""c4mut combined lane (1025-32/c4mut/RULINGS.md, "session 2 return" S2-1 and "fix r1 return" rulings 3-4, plus the
fix r1 verify LOWs): R4's migration runs in the store's own ensure-schema path; the review transitions write through
the store's one writer without making a store; a review verb raised above C1 is refused with the passkey path named;
the phone's queue rows share the panel's guard on a body that is not an object."""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from levain.coordinator import review
from levain.coordinator.store import CoordStore, StoreAbsent


def _dir(tmp_path: Path, name: str = "consent") -> Path:
    d = tmp_path / name
    d.mkdir(mode=0o700)
    return d


# review_events as session 1 (2338dd21, the last schema before R4) made it, verbatim
_S1_EVENTS = ("CREATE TABLE IF NOT EXISTS review_events (item_id TEXT NOT NULL REFERENCES review_items(item_id), "
              "seq INTEGER NOT NULL, op TEXT NOT NULL CHECK (op IN ('cleared', 'reopened')), "
              "mode TEXT NOT NULL CHECK (mode IN ('manual', 'fresh', 'coordinator')), "
              "by TEXT NOT NULL CHECK (by IN ('operator', 'coordinator')), "
              "at TEXT NOT NULL, PRIMARY KEY (item_id, seq))")


def _snapshot(path: Path) -> dict:
    conn = sqlite3.connect(path)
    try:
        return {
            "events": conn.execute("SELECT rowid, item_id, seq, op, mode, by, at FROM review_events "
                                   "ORDER BY rowid").fetchall(),
            # S0's gseq column is NULL on every row the migration found: the rest of the row is what is compared
            "items": conn.execute("SELECT rowid, item_id, thread_id, feed, dedupe_key, body, authority_relevant, by, at "
                                  "FROM review_items ORDER BY rowid").fetchall(),
            "triggers": conn.execute("SELECT name, tbl_name, sql FROM sqlite_master WHERE type = 'trigger' "
                                     "AND name NOT LIKE '%gseq%' ORDER BY name").fetchall(),
            "columns": [r[1] for r in conn.execute("PRAGMA table_info(review_events)")],
        }
    finally:
        conn.close()


def _strip_s0(path: Path) -> None:
    """Make the file what a store made before C15's S0 holds: no ``gseq`` column, index or trigger on any table the S0
    migration reaches except ``replies`` (which had its own, CH-D1 §2.8), and no ``gseq_migration_bounds``. The helper
    below builds its "old" store with the current code, whose ensure path adds them; every row is kept as it was."""
    from levain.coordinator.store import GSEQ_TABLES

    conn = sqlite3.connect(path, isolation_level=None)
    try:
        conn.execute("DROP TABLE gseq_migration_bounds")
        for table in GSEQ_TABLES:
            if table == "replies":
                continue
            conn.execute(f"DROP TRIGGER {table}_gseq_no_replace")
            conn.execute(f"DROP INDEX {table}_gseq")
            conn.execute(f"ALTER TABLE {table} DROP COLUMN gseq")
    finally:
        conn.close()


def _pre_r4_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, list[review.Item]]:
    """A dispatch.db made with session 1's review schema, holding items and events (clears, a reopen, the coordinator's
    own clear), then COPIED (SQLite's backup) to a fresh directory: what an operator upgrading from it holds."""
    from levain.coordinator.lease import LEASE_SCHEMA  # noqa: F401  (the lease the coordinator clear is fenced by)

    s1 = tuple(_S1_EVENTS if s.startswith("CREATE TABLE IF NOT EXISTS review_events ") else s
               for s in review.REVIEW_SCHEMA)
    assert s1 != review.REVIEW_SCHEMA
    with monkeypatch.context() as m:
        m.setattr(review, "REVIEW_SCHEMA", s1)
        m.setattr(review, "migrate_events", lambda conn: False, raising=False)
        store = CoordStore(_dir(tmp_path, "old"))
        a, _ = review.file_item(store, "seat", {"text": "a"}, dedupe_key="a")
        b, _ = review.file_item(store, "seat", {"text": "b"}, dedupe_key="b")
        c, _ = review.file_item(store, "doubt", {"text": "c"}, dedupe_key="c", authority_relevant=True)
        d, _ = review.file_item(store, "seat", {"text": "d"}, dedupe_key="d")
        review.clear_item(store, a.item_id)
        review.clear_item(store, b.item_id, mode="fresh")
        review.reopen_item(store, b.item_id)
        review.clear_item(store, c.item_id)
        tok = store.coord_take()
        review.clear_item(store, d.item_id, mode="coordinator", token=tok)
        _strip_s0(store.path)
        assert _snapshot(store.path)["columns"] == ["item_id", "seq", "op", "mode", "by", "at"]
    copy_dir = _dir(tmp_path, "upgraded")
    src, dst = sqlite3.connect(store.path), sqlite3.connect(copy_dir / "dispatch.db")
    try:
        src.backup(dst)
    finally:
        src.close()
        dst.close()
    (copy_dir / "dispatch.db").chmod(0o600)
    return copy_dir, [a, b, c, d]


# --- S2-1: R4's migration in the store's own ensure-schema path ----------------------------------------------------

def test_any_writer_opening_a_pre_r4_store_migrates_it_and_keeps_every_row(tmp_path, monkeypatch) -> None:
    d, items = _pre_r4_store(tmp_path, monkeypatch)
    before = _snapshot(d / "dispatch.db")
    assert len(before["events"]) == 5
    CoordStore(d)._connect().close()               # the opener every write takes; no review call at all
    after = _snapshot(d / "dispatch.db")
    # R4's column, then S0's: the rebuild runs first, and the gseq column is added to the rebuilt table
    assert after["columns"] == ["item_id", "seq", "op", "mode", "by", "at", "sign_request_id", "gseq"]
    assert after["events"] == before["events"]     # every row, its rowid (the cleared page's order) included
    assert after["items"] == before["items"]
    assert after["triggers"] == before["triggers"]
    conn = sqlite3.connect(d / "dispatch.db")
    try:
        assert conn.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert conn.execute("SELECT COUNT(*) FROM review_events WHERE sign_request_id IS NOT NULL").fetchone() == (0,)
        sql = conn.execute("SELECT sql FROM sqlite_master WHERE name = 'review_events'").fetchone()[0]
        assert "'signed'" in sql and "sign_request_id IS NOT NULL" in sql
        with pytest.raises(sqlite3.IntegrityError):              # the append-only triggers still hold
            conn.execute("UPDATE review_events SET mode = 'fresh'")
        with pytest.raises(sqlite3.IntegrityError):              # a signed event names its request
            conn.execute("INSERT INTO review_events (item_id, seq, op, mode, by, at) VALUES (?, 3, 'cleared', "
                         "'signed', 'operator', 'x')", (items[1].item_id,))
    finally:
        conn.close()
    # the queue reads as it did, and a write after the migration lands
    store = CoordStore(d)
    assert [(i.item_id, i.state) for i in review.list_items(store, include_cleared=True)] == \
        [(items[0].item_id, "cleared"), (items[1].item_id, "open"), (items[2].item_id, "cleared"),
         (items[3].item_id, "cleared")]
    assert review.clear_item(store, items[1].item_id).state == "cleared"


def test_the_migration_is_one_transaction_with_the_rest_of_the_schema(tmp_path, monkeypatch) -> None:
    """A failure after the rebuild, inside the same ensure-schema transaction, leaves the store as it was."""
    import levain.coordinator.store as store_mod

    d, _items = _pre_r4_store(tmp_path, monkeypatch)
    before = _snapshot(d / "dispatch.db")

    def boom(conn):
        raise RuntimeError("late in the ensure")
    monkeypatch.setattr(store_mod, "_replies_gseq", boom)
    with pytest.raises(RuntimeError):
        CoordStore(d)._connect()
    assert _snapshot(d / "dispatch.db") == before


def test_a_display_reads_a_pre_r4_store_before_any_writer_migrates_it(tmp_path, monkeypatch) -> None:
    d, items = _pre_r4_store(tmp_path, monkeypatch)
    store = CoordStore(d)
    with review.read_transaction(store.open_readonly) as conn:
        page = review.queue_page(conn, open_limit=10, cleared_limit=10)
    assert page.open_total == 1 and [i.item_id for i in page.open] == [items[1].item_id]
    assert _snapshot(d / "dispatch.db")["columns"][-1] == "at"   # a reader migrates nothing


# --- fix-r1 ruling 4: one writer, create=False ---------------------------------------------------------------------

def test_the_laptop_clear_brings_an_older_store_current_and_makes_none(tmp_path) -> None:
    """complement LOW (fix r1 verify): a store missing a table this levain adds (here review_marks, as a store from
    before it held) is brought current by the clear's own writer, not refused as absent."""
    from levain.coordinator.envelopes import EnvelopeStore

    d = _dir(tmp_path)
    item, _ = review.file_item(EnvelopeStore(d), "seat", {"text": "x"}, dedupe_key="a")
    conn = sqlite3.connect(d / "dispatch.db", isolation_level=None)
    try:
        conn.execute("DROP TABLE review_marks")
    finally:
        conn.close()
    assert review.clear_item(EnvelopeStore(d), item.item_id, mode="manual").state == "cleared"
    conn = sqlite3.connect(d / "dispatch.db")
    try:
        assert conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'review_marks'").fetchone() == (1,)
    finally:
        conn.close()


def test_the_migration_has_one_caller_the_stores_ensure_path() -> None:
    import inspect

    import levain.coordinator.store as store_mod

    assert inspect.getsource(store_mod).count("migrate_events(") == 1
    assert inspect.getsource(store_mod.CoordStore._ensure).count("migrate_events(conn)") == 1
    assert inspect.getsource(review).count("migrate_events(") == 1          # its definition


def test_review_has_no_writer_of_its_own() -> None:
    """One home: the lease fence and the awake reconcile live in CoordStore._write alone."""
    import inspect

    src = inspect.getsource(review)
    assert "_write_existing" not in src and "_connect_existing" not in src and "record_write(" not in src
    assert not hasattr(review, "_write_existing")


def test_a_signed_clear_or_its_request_makes_no_store(tmp_path) -> None:
    d = _dir(tmp_path)
    store = CoordStore(d)
    with pytest.raises(StoreAbsent):
        review.file_clear_request(store, "rv-" + "0" * 32, queue_dir=d)
    with pytest.raises((StoreAbsent, review.SignedClearRefused)):
        review.clear_signed(store, "rv-" + "0" * 32, queue_dir=d, request_id="lv-" + "0" * 32, keys=lambda: {})
    assert not (d / "dispatch.db").exists()


# --- fix-r1 ruling 3: a raised review verb is refused with the passkey path named -----------------------------------

from levain.cockpit.coordinator_panels import REVIEW_ID  # noqa: E402
from tests.cockpit_authority_rig import link  # noqa: E402
from tests.test_cockpit_k2a import _install  # noqa: E402
from tests.test_cockpit_k2b_broker import Key  # noqa: E402
from tests.test_cockpit_pairing_http import PROXY, _call, _pair, _serve  # noqa: E402


@pytest.mark.parametrize("verb", ["review_clear", "review_reopen"])
def test_a_review_verb_a_signed_policy_raises_is_refused_on_the_laptop_never_a_pending(tmp_path, monkeypatch,
                                                                                      verb) -> None:
    home = tmp_path / "home"
    (home / "consent").mkdir(parents=True, mode=0o700)
    monkeypatch.setenv("LEVAIN_HOME", str(home))
    _root, src, _ids = _install(tmp_path)
    store = CoordStore(home / "consent")
    item, _ = review.file_item(store, "seat", {"text": "seat lane-a died"}, dedupe_key="a")
    if verb == "review_reopen":
        review.clear_item(store, item.item_id)
    keys = tmp_path / "keys"
    keys.mkdir(mode=0o700)
    with _serve(src) as (port, httpd):
        _dev, tok = _pair(port, proxied=True)
        phone = Key(keys, "phone").enroll(httpd.broker, "display-phone")
        link(httpd.broker.db, {"op": "raise", "verb": verb, "tier": "T2"}, phone)
        st, panel, _ = _call(port, "GET", f"/cockpit/panel/{REVIEW_ID}.json?profile=full", token=tok, headers=PROXY)
        row = next(r for r in panel["rows"] if r["id"] == f"review:{item.item_id}")
        req = {"verb": verb, "panel_id": REVIEW_ID, "row_id": row["id"], "row_version": row["version"], "params": {}}
        # the preview, a fire, and a fire carrying the intent a T2 preview would have shown
        for extra in ({"dry_run": True}, {}, {"intent": "reported", "tier_seen": "T2", "gesture_seen": "direct-reported"}):
            st, out, _ = _call(port, "POST", "/cockpit/verb", {**req, "confirm": True, "idempotency_key": "k-1", **extra},
                               token=tok, headers=PROXY)
            assert (st, out.get("error")) == (403, "review_raised") and "pending_id" not in out, out
            assert "T2" in out["message"] and f"levain coord review {verb.split('_')[1]}" in out["message"], out
            if verb == "review_clear":
                assert "passkey" in out["message"], out
    assert review.get_item(store, item.item_id).state == ("open" if verb == "review_clear" else "cleared")


# --- the fix r1 verify LOW: the phone's queue rows share the panel's body guard --------------------------------------

def test_the_phone_queue_draws_an_item_whose_body_is_not_an_object(tmp_path) -> None:
    from datetime import datetime, timezone

    from levain.coordinator.phone import CoordPhone

    store = CoordStore(_dir(tmp_path))
    review.file_item(store, "seat", {"text": "fine"}, dedupe_key="a")
    with store._write() as (conn, now):
        conn.execute("INSERT INTO review_items (item_id, thread_id, feed, dedupe_key, body, authority_relevant, by, at) "
                     "VALUES (?, NULL, 'seat', 'b', '[1, 2]', 0, 'service', ?)", ("rv-" + "1" * 32, now))
    with review.read_transaction(store.open_readonly) as conn:
        page = review.queue_page(conn, open_limit=10, cleared_limit=10)
    phone = CoordPhone.__new__(CoordPhone)
    phone._clock = lambda: datetime.now(timezone.utc)
    view = phone.review_view(store, page)
    assert {i["item_id"]: i["text"] for i in view["open"]} == {page.open[0].item_id: "fine", "rv-" + "1" * 32: ""}


def test_the_panel_and_the_phone_take_an_items_fields_from_one_helper() -> None:
    import inspect

    import levain.cockpit.coordinator_panels as cp
    import levain.coordinator.phone as ph

    for mod in (cp, ph):
        src = inspect.getsource(mod)
        assert "i.body.get(" not in src and "isinstance(i.body, dict)" not in src, mod.__name__
        assert "review.body_fields(" in src, mod.__name__

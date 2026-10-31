"""C11 S4: the coordinator's reply to a drop (1030-2-c11/DESIGN.md sections 3.3 and 3.4). Built from the first-spend RUN
(a CLI drop to a scratch coordinator store, then ``coord reply``): REPLY-BOUND is the store's trigger, so each refusal
is shown with a direct SQL insert as well as through the CLI; the block is frozen at write; an upgraded store keeps
every reply row and its gseq."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from levain.cli import main
from levain.cockpit.consent import utc_stamp
from levain.cockpit.coordinator_status import status_reader
from levain.cockpit.engine import ReadContext
from levain.coordinator import drop_reply, work_delta
from levain.coordinator.chat import ChatStore, timeline
from levain.coordinator.envelopes import EnvelopeStore
from levain.coordinator.store import (REPLY_KINDS, REPLY_WINDOW_HOURS, UNREPLIED_AFTER_MINUTES, UNREPLIED_SHOWN_DAYS,
                                      CoordError, CoordStore, Part)

NOW = datetime(2026, 10, 31, 14, 0, tzinfo=timezone.utc)


@pytest.fixture
def qdir(tmp_path):
    d = tmp_path / "q"
    d.mkdir(mode=0o700)
    return d


@pytest.fixture
def store(qdir):
    return CoordStore(qdir)


@pytest.fixture
def token(store):
    return store.coord_take()


def _drop(store, words="tell me when it lands", surface="phone"):
    return store.accept_drop(words, surface=surface)[0].drop_id


def _cli(qdir, drop_id, text, token, capsys, *extra):
    code = main(["coord", "reply", drop_id, "--text", text, "--token", str(token), "--queue-dir", str(qdir), *extra])
    cap = capsys.readouterr()
    return code, cap.out, cap.err


def _insert(qdir, drop_id, at, kind="coordinator_reply", ref_store="drops"):
    c = sqlite3.connect(qdir / "dispatch.db")
    try:
        c.execute("INSERT INTO replies (reply_id, thread_id, kind, ref_store, ref_id, body, at, gseq) "
                  "VALUES (?, NULL, ?, ?, ?, '{}', ?, (SELECT COALESCE(MAX(gseq), 0) + 1 FROM replies))",
                  (f"rp-{abs(hash((drop_id, at, kind))):x}", kind, ref_store, drop_id, at))
        c.commit()
    finally:
        c.close()


def _stamp(dt):
    return utc_stamp(dt)


# ---------------------------------------------------------------------------------------------- REPLY-BOUND
def test_a_reply_to_a_drop_that_does_not_exist_is_refused_by_the_trigger(qdir, store, token, capsys) -> None:
    ghost = "dr-" + "0" * 32
    with pytest.raises(sqlite3.IntegrityError, match="names a drop that exists"):
        _insert(qdir, ghost, _stamp(NOW))
    code, _out, err = _cli(qdir, ghost, "x", token, capsys)
    assert code == 1 and "names a drop that exists" in err


def test_a_reply_that_names_something_other_than_a_drop_is_refused(qdir, store, token) -> None:
    d = _drop(store)
    with pytest.raises(sqlite3.IntegrityError, match="names a drop that exists"):
        _insert(qdir, d, _stamp(NOW), ref_store="questions")


def test_a_second_reply_to_one_drop_is_refused_by_the_trigger(qdir, store, token, capsys) -> None:
    d = _drop(store)
    code, _out, _err = _cli(qdir, d, "first", token, capsys)
    assert code == 0
    with pytest.raises(sqlite3.IntegrityError, match="one coordinator reply"):
        _insert(qdir, d, _stamp(datetime.now(timezone.utc)))
    code, _out, err = _cli(qdir, d, "second", token, capsys)
    assert code == 1 and "one coordinator reply" in err
    c = sqlite3.connect(qdir / "dispatch.db")
    try:
        assert c.execute("SELECT COUNT(*) FROM replies WHERE kind = 'coordinator_reply'").fetchone()[0] == 1
    finally:
        c.close()


def test_a_reply_after_the_window_is_refused_and_one_inside_it_is_not(qdir, token, capsys) -> None:
    old = CoordStore(qdir, clock=lambda: datetime.now(timezone.utc) - timedelta(hours=REPLY_WINDOW_HOURS + 2))
    expired = _drop(old, "an old word")
    received = datetime.fromisoformat(old.drop(expired).received_at.replace("Z", "+00:00"))
    with pytest.raises(sqlite3.IntegrityError, match="within the window"):
        _insert(qdir, expired, _stamp(received + timedelta(hours=REPLY_WINDOW_HOURS, minutes=1)))
    code, _out, err = _cli(qdir, expired, "late", token, capsys)
    assert code == 1 and "within the window" in err
    _insert(qdir, expired, _stamp(received + timedelta(hours=REPLY_WINDOW_HOURS - 1)))   # the same drop, inside it


def _raw_drop(qdir, drop_id, received_at) -> None:
    c = sqlite3.connect(qdir / "dispatch.db", isolation_level=None)
    try:
        c.execute("INSERT INTO drops (drop_id, surface, outbox_id, words, words_sha256, sent_at, received_at, "
                  "source_how) VALUES (?, 'cli', ?, 'w', 'h', ?, ?, 'none')",
                  (drop_id, "o-" + drop_id, received_at, received_at))
    finally:
        c.close()


@pytest.mark.parametrize("received", ["", "not a time"])
def test_a_drop_with_an_unreadable_received_at_takes_no_reply(qdir, store, received) -> None:
    store.accept_drop("x", surface="phone")                    # makes the file
    _raw_drop(qdir, "d-bad", received)
    with pytest.raises(sqlite3.IntegrityError, match="within the window"):
        _insert(qdir, "d-bad", "2026-10-31T14:00:00Z")


def test_a_reply_dated_before_its_drop_is_refused(qdir, store) -> None:
    store.accept_drop("x", surface="phone")
    _raw_drop(qdir, "d-late", "2026-10-31T14:00:00Z")
    with pytest.raises(sqlite3.IntegrityError, match="within the window"):
        _insert(qdir, "d-late", "2026-10-31T13:59:00Z")
    _insert(qdir, "d-late", "2026-10-31T14:00:00Z")            # the same instant is not before it


def test_a_lease_token_that_is_not_the_live_one_writes_nothing(qdir, store, token, capsys) -> None:
    d = _drop(store)
    code, _out, err = _cli(qdir, d, "x", token + 1, capsys)
    assert code == 1 and "not the live lease" in err
    assert drop_reply.replies(sqlite3.connect(qdir / "dispatch.db")) == []


# ---------------------------------------------------------------------------------------------- the frozen block
def test_the_block_is_frozen_at_write_and_unchanged_after_more_work_lands(qdir, store, token, capsys) -> None:
    d = _drop(store)
    code, out, _err = _cli(qdir, d, "heard", token, capsys, "--json")
    assert code == 0
    said = json.loads(out)
    assert said["covers"]["through"] < said["gseq"] and said["block"].startswith(work_delta.HEAD)
    for i in range(3):                                          # more records land after the reply
        _drop(store, f"later word {i}")
    conn = store.open_readonly()
    try:
        (stored,) = drop_reply.replies(conn)
        assert stored.block == said["block"] and (stored.after, stored.through) == (said["covers"]["after"], said["covers"]["through"])
        fresh = work_delta.render(work_delta.work_delta(conn, stored.after))
        assert fresh != stored.block                           # a re-render would have moved: the stored text did not
    finally:
        conn.close()


def test_writing_the_reply_moves_the_cursor_to_what_the_block_covered_and_the_next_block_names_the_reply(
        qdir, store, token, capsys) -> None:
    d = _drop(store)
    assert _cli(qdir, d, "heard", token, capsys)[0] == 0
    conn = store.open_readonly()
    try:
        mark = work_delta.watermark(conn, "coordinator")
        (stored,) = drop_reply.replies(conn)
        assert 0 < mark < stored.gseq                          # the reply's own row stays after the cursor
        nxt = work_delta.render(work_delta.work_delta(conn, mark))
    finally:
        conn.close()
    assert "1 reply to your phone message at" in nxt


# ---------------------------------------------------------------------------------------------- chat_timeline
def test_chat_timeline_shows_the_reply_on_the_drops_thread(qdir, store, token, capsys) -> None:
    d = _drop(store)
    (thread,) = store.split(d, [Part(kind="work", title="the work")], by="operator")
    assert _cli(qdir, d, "on it", token, capsys)[0] == 0
    cs = ChatStore(EnvelopeStore(qdir))
    conn = cs._read_connect()
    try:
        _before, merged = timeline(conn, thread.thread_id)
    finally:
        conn.close()
    rows = [r for r in merged if r.kind == "reply_coordinator"]
    assert len(rows) == 1 and json.loads(rows[0].body)["text"] == "on it"


# ---------------------------------------------------------------------------------------------- the migration
def _downgrade(qdir) -> list[tuple]:
    """Rewrite ``replies`` as a store from before this kind has it: the older CHECK, every row kept."""
    c = sqlite3.connect(qdir / "dispatch.db", isolation_level=None)
    try:
        c.execute("PRAGMA foreign_keys = OFF")
        c.execute("PRAGMA legacy_alter_table = ON")            # a view over replies must not stop the old shape being built
        sql = c.execute("SELECT sql FROM sqlite_master WHERE name = 'replies'").fetchone()[0]
        old = sql.replace(", 'coordinator_reply'", "").replace("TABLE replies", "TABLE replies_old", 1)
        assert "coordinator_reply" not in old
        c.execute("BEGIN")
        c.execute(old)
        c.execute("INSERT INTO replies_old (rowid, reply_id, thread_id, kind, ref_store, ref_id, body, at, gseq) "
                  "SELECT rowid, reply_id, thread_id, kind, ref_store, ref_id, body, at, gseq FROM replies")
        c.execute("DROP TABLE replies")
        c.execute("ALTER TABLE replies_old RENAME TO replies")
        c.execute("COMMIT")
        return c.execute("SELECT rowid, reply_id, kind, ref_id, body, at, gseq FROM replies ORDER BY rowid").fetchall()
    finally:
        c.close()


def test_an_upgraded_store_keeps_every_reply_row_and_its_gseq(qdir, store, token) -> None:
    d = _drop(store)
    store.split(d, [Part(kind="work", title="t")], by="operator")
    c = sqlite3.connect(qdir / "dispatch.db")
    reply_id = c.execute("SELECT reply_id FROM replies LIMIT 1").fetchone()[0]
    c.execute("INSERT INTO know_deliveries (reply_id, agent_session_id, at) VALUES (?, 's', 'x')", (reply_id,))
    c.commit()
    c.close()
    before = _downgrade(qdir)
    assert len(before) >= 2 and all(r[-1] is not None for r in before)
    c = sqlite3.connect(qdir / "dispatch.db")
    with pytest.raises(sqlite3.IntegrityError):               # the downgraded store really refuses the new kind
        c.execute("INSERT INTO replies (reply_id, kind, ref_store, ref_id, body, at) VALUES ('rp-x', "
                  "'coordinator_reply', 'drops', 'd', '{}', 'x')")
    c.close()
    upgraded = CoordStore(qdir)
    upgraded.coord_take()                                      # any writer's open runs the ensure path
    c = sqlite3.connect(qdir / "dispatch.db")
    try:
        after = c.execute("SELECT rowid, reply_id, kind, ref_id, body, at, gseq FROM replies ORDER BY rowid").fetchall()
        assert after == before
        assert c.execute("PRAGMA foreign_key_check").fetchall() == []
        assert c.execute("SELECT reply_id FROM know_deliveries").fetchone() == (reply_id,)
        assert "coordinator_reply" in REPLY_KINDS
        names = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type = 'trigger'")}
        assert {"replies_bound_drop", "replies_bound_once", "replies_bound_window", "replies_gseq_no_replace",
                "replies_frozen"} <= names
    finally:
        c.close()
    tok = upgraded.coord_take()
    assert drop_reply.write_reply(upgraded, d, "after the upgrade", token=tok).drop_id == d


def test_an_upgrade_of_a_store_holding_the_chat_timeline_view_keeps_everything(qdir, store, token) -> None:
    d = _drop(store)
    store.split(d, [Part(kind="work", title="t")], by="operator")
    ChatStore(EnvelopeStore(qdir))._connect().close()          # builds chat_timeline the way chat.py does
    c = sqlite3.connect(qdir / "dispatch.db", isolation_level=None)
    c.execute("INSERT INTO replies (rowid, reply_id, thread_id, kind, ref_store, ref_id, body, at, gseq) VALUES "
              "(4000, 'rp-high', NULL, 'know', 'questions', 'q-1', '{}', '2026-10-31T10:00:00Z', "
              "(SELECT COALESCE(MAX(gseq), 0) + 1 FROM replies))")
    c.execute("INSERT INTO first_reads (question_id, outcome, reply_id, at) VALUES ('q-1', 'know', 'rp-high', 'x')")
    assert c.execute("SELECT count(*) FROM sqlite_master WHERE name = 'chat_timeline'").fetchone() == (1,)
    c.close()
    before = _downgrade(qdir)
    assert any(r[0] == 4000 for r in before)
    CoordStore(qdir).coord_take()                              # the open runs the upgrade
    c = sqlite3.connect(qdir / "dispatch.db")
    try:
        after = c.execute("SELECT rowid, reply_id, kind, ref_id, body, at, gseq FROM replies ORDER BY rowid").fetchall()
        assert after == before
        assert c.execute("SELECT reply_id FROM first_reads").fetchone() == ("rp-high",)
        assert c.execute("PRAGMA foreign_key_check").fetchall() == []
        c.execute("SELECT * FROM chat_timeline").fetchall()           # the view still resolves
        assert {"replies_bound_drop", "replies_bound_once", "replies_bound_window", "replies_gseq_no_replace",
                "replies_frozen"} <= {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type = 'trigger'")}
        assert "replies_ref" in {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type = 'index'")}
        assert "coordinator_reply" in c.execute("SELECT sql FROM sqlite_master WHERE name = 'replies'").fetchone()[0]
    finally:
        c.close()


def _old_store(qdir, store) -> None:
    d = _drop(store)
    store.split(d, [Part(kind="work", title="t")], by="operator")
    _downgrade(qdir)


def _raw(qdir, keys: int):
    c = sqlite3.connect(qdir / "dispatch.db", isolation_level=None, timeout=0.2)
    c.execute(f"PRAGMA foreign_keys = {keys}")
    return c


@pytest.mark.parametrize("keys", [0, 1])
def test_the_upgrade_puts_foreign_keys_back_to_what_the_connection_had(qdir, store, keys) -> None:
    _old_store(qdir, store)
    c = _raw(qdir, keys)
    try:
        CoordStore(qdir)._ensure(c, create=False)
        assert c.execute("PRAGMA foreign_keys").fetchone() == (keys,)
        assert "coordinator_reply" in c.execute("SELECT sql FROM sqlite_master WHERE name = 'replies'").fetchone()[0]
    finally:
        c.close()


def test_a_busy_store_during_the_upgrade_does_not_leave_foreign_keys_off(qdir, store) -> None:
    _old_store(qdir, store)
    holder = sqlite3.connect(qdir / "dispatch.db", isolation_level=None)
    holder.execute("BEGIN IMMEDIATE")
    c = _raw(qdir, 1)
    try:
        with pytest.raises(sqlite3.OperationalError):
            CoordStore(qdir)._ensure(c, create=True)
        assert c.execute("PRAGMA foreign_keys").fetchone() == (1,)
    finally:
        c.close()
        holder.execute("ROLLBACK")
        holder.close()


def test_a_dangling_row_elsewhere_does_not_block_the_upgrade(qdir, store) -> None:
    _old_store(qdir, store)
    c = _raw(qdir, 0)
    c.execute("CREATE TABLE stray (x TEXT REFERENCES nowhere(id))")
    c.execute("INSERT INTO stray VALUES ('lost')")
    assert c.execute("PRAGMA foreign_key_check").fetchall()
    try:
        CoordStore(qdir)._ensure(c, create=False)
        assert "coordinator_reply" in c.execute("SELECT sql FROM sqlite_master WHERE name = 'replies'").fetchone()[0]
    finally:
        c.close()


def test_a_dangling_row_under_replies_blocks_the_upgrade_and_changes_nothing(qdir, store) -> None:
    _old_store(qdir, store)
    c = _raw(qdir, 0)
    c.execute("INSERT INTO first_reads (question_id, outcome, reply_id, at) VALUES ('q', 'know', 'rp-missing', 'x')")
    try:
        with pytest.raises(CoordError, match="replies rebuild.*dangling"):
            CoordStore(qdir)._ensure(c, create=False)
        assert "coordinator_reply" not in c.execute("SELECT sql FROM sqlite_master WHERE name = 'replies'").fetchone()[0]
    finally:
        c.close()


def test_an_integrity_failure_names_the_table_that_was_rebuilt(qdir, store) -> None:
    _old_store(qdir, store)
    c = _raw(qdir, 1)

    class Bad:
        def execute(self, sql, *a):
            if sql == "PRAGMA integrity_check":
                return sqlite3.connect(":memory:").execute("SELECT 'broken'")
            return c.execute(sql, *a)

    try:
        with pytest.raises(CoordError, match="the replies rebuild left .* integrity check"):
            CoordStore(qdir)._ensure(Bad(), create=False)
    finally:
        c.close()


# ---------------------------------------------------------------------------------------------- unreplied drops
def _clocked(qdir, at):
    box = [at]
    return CoordStore(qdir, clock=lambda: box[0]), box


def _delta(qdir, now, since=0):
    c = sqlite3.connect(qdir / "dispatch.db")
    try:
        return work_delta.work_delta(c, since, now=_stamp(now))
    finally:
        c.close()


def test_the_work_delta_prints_an_unreplied_drop_even_with_nothing_else_new(qdir) -> None:
    store, box = _clocked(qdir, NOW)
    d = _drop(store)
    late = NOW + timedelta(minutes=UNREPLIED_AFTER_MINUTES + 1)
    first = _delta(qdir, late)
    assert first.total >= 1                                    # the receipt is news the first time
    caught_up = _delta(qdir, late, since=first.through)
    assert caught_up.total == 0 and caught_up.through == first.through      # no gseq record, no cursor movement
    text = work_delta.render(caught_up)
    assert text.startswith(work_delta.HEAD) and f"drop {d} (14:00, phone): no reply yet" in text
    assert work_delta.render(_delta(qdir, NOW + timedelta(minutes=2), since=first.through)) == ""   # not yet quiet-minutes


def test_an_unreplied_drop_shows_closed_after_the_window_and_ages_out_after_seven_days(qdir) -> None:
    store, box = _clocked(qdir, NOW)
    d = _drop(store)
    closed = NOW + timedelta(hours=REPLY_WINDOW_HOURS + 1)
    text = work_delta.render(_delta(qdir, closed, since=10**6))
    assert f"drop {d} (2026-10-31 14:00, phone): no reply, window closed" in text
    still = NOW + timedelta(days=UNREPLIED_SHOWN_DAYS) - timedelta(minutes=1)
    assert "window closed" in work_delta.render(_delta(qdir, still, since=10**6))
    gone = NOW + timedelta(days=UNREPLIED_SHOWN_DAYS, minutes=1)
    assert work_delta.render(_delta(qdir, gone, since=10**6)) == ""
    c = sqlite3.connect(qdir / "dispatch.db")
    try:
        assert [u.closed for u in drop_reply.unreplied(c, _stamp(closed))] == [True]
        assert drop_reply.unreplied(c, _stamp(gone)) == []
    finally:
        c.close()
    rows = status_reader(lambda: qdir, lambda: closed)(ReadContext(closed)).rows
    assert any(r.id == f"noreply:{d}" and r.body.endswith("no reply, window closed") for r in rows)


def test_the_frozen_block_leaves_out_the_drop_it_answers_but_names_the_others(qdir) -> None:
    store, box = _clocked(qdir, NOW)
    answered, other = _drop(store, "one"), _drop(store, "two")
    box[0] = NOW + timedelta(minutes=UNREPLIED_AFTER_MINUTES + 1)
    got = drop_reply.write_reply(store, answered, "heard", token=store.coord_take())
    assert f"drop {answered} (" not in got.block              # its own "no reply yet" line
    assert f"drop {other} (14:00, phone): no reply yet" in got.block
    assert got.through < got.gseq                              # covers what the block covers, not the reply itself


def test_the_stored_block_carries_the_unreplied_lines_the_stop_check_verifies(qdir) -> None:
    from levain import band_view

    store, _box = _clocked(qdir, datetime.now(timezone.utc) - timedelta(minutes=30))
    d = _drop(store)
    block = work_delta.read_block(qdir, "v", 5.0)
    line = f"drop {d} ({_stamp(datetime.now(timezone.utc) - timedelta(minutes=30))[11:16]}, phone): no reply yet"
    assert line in block.text
    view = band_view.View(0, (), None)
    assert band_view._reprint_check(view, block.text, block.text) == ([], False)
    dropped = "\n".join(ln for ln in block.text.splitlines() if "no reply yet" not in ln)
    assert band_view._reprint_check(view, block.text, dropped)[1] is True


# ---------------------------------------------------------------------------------------------- the panel
def test_the_panel_shows_the_reply_with_its_frozen_block_and_the_unreplied_drop_past_the_window(qdir) -> None:
    clock_now = [NOW]
    store = CoordStore(qdir, clock=lambda: clock_now[0])
    replied, silent = _drop(store, "first"), _drop(store, "second")
    clock_now[0] = NOW + timedelta(minutes=UNREPLIED_AFTER_MINUTES + 1)
    drop_reply.write_reply(store, replied, "heard the first", token=store.coord_take())
    ctx = ReadContext(clock_now[0])
    rows = {r.id: r.body for r in status_reader(lambda: qdir, lambda: clock_now[0])(ctx).rows}
    reply_rows = [b for i, b in rows.items() if i.startswith("reply:") and ":block:" not in i]
    assert len(reply_rows) == 1 and "heard the first" in reply_rows[0] and replied in reply_rows[0]
    assert any(i.startswith("reply:") and ":block:" in i and work_delta.HEAD in b for i, b in rows.items())
    assert rows[f"noreply:{silent}"].endswith(f"drop {silent} (14:00, phone): no reply yet")
    assert f"noreply:{replied}" not in rows
    early = status_reader(lambda: qdir, lambda: NOW + timedelta(minutes=2))(ReadContext(NOW))
    assert not any(r.id.startswith("noreply:") for r in early.rows)       # inside the quiet minutes: no line yet

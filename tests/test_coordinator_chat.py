"""The chat records and the turn lease (CH-D1a: CH-D1 rev 2 §2.1-§2.4, §2.8, §5; B3, B9 and the static import test).

Every §2.2 edge and every §5 edge has a refused-transition test here, through the module (the refusal names why) AND
through raw SQL on a second connection (the trigger holds the edge for any writer)."""
from __future__ import annotations

import ast
import io
import json
import re
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from levain.coordinator import chat, chat_lease
from levain.coordinator.chat import ChatStore, MessageConflict, OutboxRefused, EventConflict
from levain.coordinator.chat_lease import TurnRefused
from levain.coordinator.envelopes import EnvelopeStore
from levain.coordinator.store import WORDS_MAX, CoordError, Part

T0 = datetime(2026, 10, 23, 12, 0, tzinfo=timezone.utc)
ME = "cockpit:tab-a"
OTHER_TAB = "cockpit:tab-b"
PHONE = "phone:iphone-1"


class Clock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kw: float) -> None:
        self.now += timedelta(**kw)


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def qdir(tmp_path):
    d = tmp_path / "consent"
    d.mkdir(mode=0o700)
    return d


@pytest.fixture
def store(qdir, clock):
    s = EnvelopeStore(qdir, clock=clock)
    s.coord_set_agent("none")
    return s


@pytest.fixture
def world(store):
    """A work thread and one running worker launch on it."""
    token = store.coord_take()
    d = store.accept_drop("fix the build", surface="cli")[0]
    thread = store.split(d.drop_id, [Part("work", "fix the build")])[0]
    ln = store.launch_request(token, harness="claude", role="worker", threads=[thread.thread_id])
    got, why = store.launch_activate(ln.launch_id, harness_session_id="sess-worker-1")
    assert why is None and got.state == "active"
    return {"thread": thread.thread_id, "launch": ln.launch_id, "token": token}


@pytest.fixture
def cs(store):
    s = ChatStore(store)
    s._connect().close()                 # the chat tables exist before a test's raw connection writes them
    return s


def raw(qdir):
    c = sqlite3.connect(qdir / "dispatch.db", isolation_level=None)
    c.execute("PRAGMA foreign_keys = ON")
    return c


def stamp(clock):
    from levain.cockpit.consent import utc_stamp
    return utc_stamp(clock.now)


def gseq(c, clock):
    return c.execute("INSERT INTO gseq (at) VALUES (?)", (stamp(clock),)).lastrowid


def msg(cs, world, *, outbox="ob-00000001", body="go", recipient="worker", **kw):
    with cs._write() as (conn, now):
        m, created = chat.record_message(
            conn, now, thread_id=world["thread"], recipient=recipient, author_kind=kw.pop("author_kind", "operator"),
            author_surface=kw.pop("author_surface", "cockpit"), client_id=kw.pop("client_id", "tab-a"),
            outbox_id=outbox, body=body, normalized=kw.pop("normalized", body),
            launch_id=kw.pop("launch_id", world["launch"] if recipient == "worker" else None), **kw)
    return m, created


def step(cs, message_id, state, **kw):
    kw.setdefault("cause", "test")
    if state == "attempted":
        kw.setdefault("op_id", "op-1")
        kw.setdefault("vendor_anchor", "0001.0000")
    with cs._write() as (conn, now):
        return chat.outbox_step(conn, now, message_id, state, **kw)


# --- the static import test (B4 (b)'s static half, complement F10) -------------------------------------------------
_MODULES = [Path(chat.__file__), Path(chat_lease.__file__)]
_FORBIDDEN_MODULES = ("levain.coordinator.lease", "levain.coordinator.envelopes", "levain.coordinator.envelope",
                      "levain.coordinator.launch", "levain.coordinator.codex_launch", "levain.coordinator.budget",
                      "levain.coordinator.review", "levain.coordinator.first_reader", "levain.coordinator.cli",
                      "levain.consent", "levain.broker", "levain.slot", "levain.dispatch")
_STORE_NAMES = {"WORDS_MAX", "CoordError", "_append_only", "_in", "next_gseq", "schema_present"}
_AUTHORITY_TABLES = ("launches", "launch_events", "launch_spawns", "launch_threads", "launch_sweeps", "lease_events",
                     "coord_settings", "envelopes", "envelope_binds", "envelope_attenuations", "drops", "threads",
                     "thread_revisions", "thread_events", "replies")
_WRITE = re.compile(r"\b(?:(?:INSERT|REPLACE)\s+(?:OR\s+\w+\s+)?INTO|UPDATE(?:\s+OR\s+\w+)?|DELETE\s+FROM)\s+("
                    + "|".join(_AUTHORITY_TABLES) + r")\b", re.IGNORECASE)


def _import_faults(source: str) -> list[str]:
    """What a chat module imports or writes that it must not: a forbidden module, a store name off the list, or SQL
    that writes an authority (or any non-chat) table."""
    faults = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            mods = [a.name for a in node.names]
            names: list[str] = []
        elif isinstance(node, ast.ImportFrom):
            mods = [node.module or ""]
            names = [a.name for a in node.names]
        else:
            continue
        for mod in mods:
            if any(mod == f or mod.startswith(f + ".") for f in _FORBIDDEN_MODULES):
                faults.append(f"imports {mod}")
            if mod == "levain.coordinator.store" and set(names) - _STORE_NAMES:
                faults.append(f"imports {sorted(set(names) - _STORE_NAMES)} from the store")
            if mod == "levain.coordinator" and set(names) - {"chat_lease"}:
                faults.append(f"imports {sorted(set(names) - {'chat_lease'})} from levain.coordinator")
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            for m in _WRITE.finditer(node.value):
                faults.append(f"writes {m.group(1)}: {node.value[:60]!r}")
    return faults


@pytest.mark.parametrize("path", _MODULES, ids=lambda p: p.name)
def test_chat_module_imports_no_authority_writer(path):
    assert _import_faults(path.read_text(encoding="utf-8")) == []


@pytest.mark.parametrize("bad", [
    "from levain.coordinator.lease import claim",
    "from levain.coordinator.store import CoordStore",
    "import levain.coordinator.envelopes",
    "from levain.coordinator import lease",
    "X = 'INSERT INTO launch_events (launch_id) VALUES (1)'",
    "X = 'INSERT INTO lease_events (seq) VALUES (1)'",
    "X = 'UPDATE envelopes SET body = 1'",
])
def test_the_import_check_catches_a_writer(bad):
    # the static test is evidence only if it fails on the thing it forbids
    assert _import_faults(bad) != []


# --- §2.1 chat_messages ---------------------------------------------------------------------------------------------
def test_message_is_idempotent_by_outbox_id(cs, world, qdir):
    m, created = msg(cs, world)
    again, created2 = msg(cs, world)
    assert created and not created2 and again.message_id == m.message_id
    assert raw(qdir).execute("SELECT COUNT(*) FROM chat_messages").fetchone()[0] == 1
    for kw in ({"body": "stop"}, {"client_id": "tab-b"}, {"author_surface": "phone"}, {"mode": "steer",
                                                                                         "expected_turn_id": "t1"}):
        with pytest.raises(MessageConflict, match="nothing was written"):
            msg(cs, world, **kw)
    assert raw(qdir).execute("SELECT COUNT(*) FROM chat_messages").fetchone()[0] == 1


def test_worker_cannot_write_messages(cs, world, qdir, clock):
    # B9: no writer takes a worker author, and the table refuses one from any writer
    with pytest.raises(CoordError, match="a worker writes none"):
        msg(cs, world, author_kind="worker")
    c = raw(qdir)
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        c.execute("INSERT INTO chat_messages (message_id, gseq, thread_id, launch_id, recipient, author_kind, "
                  "author_surface, client_id, outbox_id, mode, body, body_sha256, body_norm_sha256, attachments, at) "
                  "VALUES (?, ?, ?, ?, 'worker', 'worker', 'cockpit', 'w', 'ob-worker1', 'queue', 'x', 's', 's', "
                  "'[]', 'x')", ("cm-" + "1" * 32, gseq(c, clock), world["thread"], world["launch"]))


def test_message_shape_refusals(cs, world, store):
    with pytest.raises(CoordError, match="only you steer"):
        msg(cs, world, author_kind="coordinator", author_surface="coordinator", mode="steer", expected_turn_id="t")
    with pytest.raises(CoordError, match="own surface"):
        msg(cs, world, author_kind="coordinator", author_surface="cockpit")
    with pytest.raises(CoordError, match="coordinator never holds"):
        msg(cs, world, author_kind="coordinator", author_surface="coordinator", owner_epoch=3)
    with pytest.raises(CoordError, match="expected turn id"):
        msg(cs, world, mode="steer")
    with pytest.raises(CoordError, match="names no turn"):
        msg(cs, world, expected_turn_id="t1")
    with pytest.raises(CoordError, match=f"at most {WORDS_MAX} characters"):
        msg(cs, world, body="x" * (WORDS_MAX + 1))
    with pytest.raises(CoordError, match="words or an attachment"):
        msg(cs, world, body="  ")
    with pytest.raises(CoordError, match="names its launch"):
        msg(cs, world, recipient="coordinator", launch_id=world["launch"])
    with pytest.raises(CoordError, match="name, sha256, bytes"):
        msg(cs, world, attachments=[{"name": "a", "sha256": "0" * 64}])
    m, _ = msg(cs, world, attachments=[{"name": "log.txt", "sha256": "a" * 64, "bytes": 12}])
    assert m.attachments == ({"name": "log.txt", "sha256": "a" * 64, "bytes": 12},)
    assert m.body_sha256 == chat.body_sha256("go")
    # the coordinator queues to the thread, from its own surface, with no epoch
    c, created = msg(cs, world, outbox="ob-coord0001", recipient="thread", author_kind="coordinator",
                     author_surface="coordinator", client_id="s-0123456789abcdef")
    assert created and c.launch_id is None and c.mode == "queue"


def test_message_to_a_worker_names_a_worker_launch_of_its_thread(cs, world, store, qdir, clock):
    d = store.accept_drop("another", surface="cli")[0]
    other = store.split(d.drop_id, [Part("work", "other")])[0].thread_id
    c = raw(qdir)
    with pytest.raises(sqlite3.IntegrityError, match="worker launch of its thread"):
        c.execute("INSERT INTO chat_messages (message_id, gseq, thread_id, launch_id, recipient, author_kind, "
                  "author_surface, client_id, outbox_id, mode, body, body_sha256, body_norm_sha256, attachments, at) "
                  "VALUES (?, ?, ?, ?, 'worker', 'operator', 'cockpit', 'w', 'ob-other001', 'queue', 'x', 's', 's', "
                  "'[]', 'x')", ("cm-" + "2" * 32, gseq(c, clock), other, world["launch"]))
    for sql, match in (("INSERT INTO chat_messages (message_id, gseq, thread_id, launch_id, recipient, author_kind, "
                        "author_surface, client_id, outbox_id, mode, expected_turn_id, body, body_sha256, "
                        "body_norm_sha256, attachments, at) VALUES ('cm-{0}', {1}, '{2}', NULL, 'thread', "
                        "'coordinator', 'coordinator', 'c', 'ob-steer001', 'steer', 't', 'x', 's', 's', '[]', 'x')",
                        "CHECK"),
                       ("INSERT INTO chat_messages (message_id, gseq, thread_id, launch_id, recipient, author_kind, "
                        "author_surface, client_id, outbox_id, mode, body, body_sha256, body_norm_sha256, attachments, "
                        "at) VALUES ('cm-{0}', {1}, '{2}', NULL, 'thread', 'operator', 'cockpit', 'c', 'ob-long0001', "
                        "'queue', '" + "x" * (WORDS_MAX + 1) + "', 's', 's', '[]', 'x')", "CHECK")):
        with pytest.raises(sqlite3.IntegrityError, match=match):
            c.execute(sql.format("3" * 32, gseq(c, clock), world["thread"]))


# --- §2.2 the outbox edges ---------------------------------------------------------------------------------------
_PATH = {None: [], "refused": ["refused"], "pending": ["pending"], "attempted": ["pending", "attempted"],
         "ambiguous": ["pending", "attempted", "ambiguous"], "accepted": ["pending", "attempted", "accepted"],
         "not_delivered": ["pending", "not_delivered"], "cancelled": ["pending", "cancelled"]}
_ALLOWED = {None: {"refused", "pending"}, "pending": {"attempted", "cancelled", "not_delivered"},
            "attempted": {"accepted", "ambiguous", "not_delivered"}, "ambiguous": {"accepted", "not_delivered"}}


def _raw_step(c, clock, message_id, state, *, by="service", op_id="op-raw", anchor="0001.0000"):
    seq = c.execute("SELECT COALESCE(MAX(seq), 0) + 1 FROM chat_outbox_events WHERE message_id = ?",
                    (message_id,)).fetchone()[0]
    attempt = state == "attempted"
    c.execute("INSERT INTO chat_outbox_events (message_id, seq, gseq, state, op_id, vendor_anchor, cause, by, at) "
              "VALUES (?, ?, ?, ?, ?, ?, 'raw', ?, ?)", (message_id, seq, gseq(c, clock), state,
                                                         op_id if attempt else None, anchor if attempt else None,
                                                         by, stamp(clock)))


@pytest.mark.parametrize("frm", list(_PATH), ids=lambda s: s or "none")
@pytest.mark.parametrize("to", chat.OUTBOX_STATES)
def test_every_outbox_edge(cs, world, qdir, clock, frm, to):
    m, _ = msg(cs, world)
    for s in _PATH[frm]:
        step(cs, m.message_id, s)
    if to in _ALLOWED.get(frm, set()):
        step(cs, m.message_id, to)
        assert cs._read(lambda conn: chat.outbox_state(conn, m.message_id)) == to
        return
    with pytest.raises(OutboxRefused, match="cannot go from"):
        step(cs, m.message_id, to)
    with pytest.raises(sqlite3.IntegrityError, match="edge §2.2 does not allow"):
        _raw_step(raw(qdir), clock, m.message_id, to)
    assert cs._read(lambda conn: chat.outbox_state(conn, m.message_id)) == (frm if frm else None)


def test_only_an_ambiguous_send_is_resolved_by_you(cs, world, qdir, clock):
    m, _ = msg(cs, world)
    step(cs, m.message_id, "pending")
    with pytest.raises(OutboxRefused, match="only an ambiguous send"):
        step(cs, m.message_id, "not_delivered", by="operator")
    with pytest.raises(sqlite3.IntegrityError, match="edge"):
        _raw_step(raw(qdir), clock, m.message_id, "not_delivered", by="operator")
    step(cs, m.message_id, "attempted")
    step(cs, m.message_id, "ambiguous")
    step(cs, m.message_id, "accepted", by="operator", vendor_item_id="item-1")
    assert cs._read(lambda conn: chat.outbox_state(conn, m.message_id)) == "accepted"


def test_attempt_carries_op_id_and_the_partial_index_is_the_cas(cs, world, qdir, clock):
    m, _ = msg(cs, world)
    step(cs, m.message_id, "pending")
    with pytest.raises(CoordError, match="op_id"):
        step(cs, m.message_id, "attempted", op_id=None)
    c = raw(qdir)
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        _raw_step(c, clock, m.message_id, "attempted", op_id=None)
    # rev 3 §16 item 1: an attempt also names the vendor position read before it
    with pytest.raises(CoordError, match="vendor anchor"):
        step(cs, m.message_id, "attempted", vendor_anchor=None)
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        _raw_step(c, clock, m.message_id, "attempted", anchor=None)
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        _raw_step(c, clock, m.message_id, "attempted", op_id=None, anchor=None)
    assert cs._read(lambda conn: chat.outbox_state(conn, m.message_id)) == "pending"
    with pytest.raises(OutboxRefused, match="vendor outcome"):
        step(cs, m.message_id, "cancelled", vendor_turn_id="t1")
    # the index refuses a second attempt on its own, even with the edge trigger gone (a test copy only)
    c.execute("DROP TRIGGER chat_outbox_events_edges")
    _raw_step(c, clock, m.message_id, "attempted")
    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
        _raw_step(c, clock, m.message_id, "attempted")


# --- §2.3 / §2.4 events and cursors ---------------------------------------------------------------------------------
def ev(cs, world, key="item-1", body="hello", **kw):
    """One vendor event; ``key`` is the vendor id, qualified by the kind as rev 3 §16 item 2 mints it."""
    kind = kw.pop("kind", "agent_text")
    with cs._write() as (conn, now):
        return chat.record_event(conn, now, launch_id=world["launch"], key=kw.pop("full_key", f"{kind}:{key}"),
                                 pos=kw.pop("pos", "0001.0001"), kind=kind, body=body,
                                 normalized=kw.pop("normalized", body), **kw)


def test_one_vendor_item_holds_a_call_an_approval_and_a_result(cs, world, qdir):
    # rev 2's bare item id put a call's tool_call and approval_request rows on one primary key
    item = "exec-0b5e6c2a-1111-4c3d-9e2f-000000000001"
    for kind, pos in (("tool_call", "0001.0002"), ("approval_request", "0001.0003"), ("tool_result", "0001.0004")):
        e, created = ev(cs, world, key=item, kind=kind, pos=pos, body=f"{kind} body", call_id=item)
        assert created and e.vendor_event_key == f"{kind}:{item}"
    rows = raw(qdir).execute("SELECT kind FROM chat_events WHERE call_id = ? ORDER BY gseq", (item,)).fetchall()
    assert [r[0] for r in rows] == ["tool_call", "approval_request", "tool_result"]
    c = raw(qdir)
    for bad in (item, "agent_text:" + item, "tool_call:"):
        with pytest.raises(CoordError, match="kind-qualified"):
            ev(cs, world, kind="tool_call", full_key=bad, pos="0001.0009")
        # the table holds the same shape for any writer
        with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
            c.execute("INSERT INTO chat_events (launch_id, vendor_event_key, gseq, vendor_pos, kind, body, "
                      "body_sha256, body_norm_sha256, truncated, backfill, source, at) VALUES (?, ?, "
                      "(SELECT MAX(gseq) + 1000 FROM gseq), 'p', 'tool_call', 'x', 's', 's', 0, 0, 'vendor', 'x')",
                      (world["launch"], bad))


def test_event_replay_is_idempotent_and_a_changed_key_conflicts(cs, world, qdir):
    e, created = ev(cs, world)
    again, created2 = ev(cs, world)
    assert created and not created2 and again.gseq == e.gseq
    with pytest.raises(EventConflict, match="other content"):
        ev(cs, world, body="different")
    with pytest.raises(EventConflict):
        ev(cs, world, pos="0002.0001")
    assert raw(qdir).execute("SELECT COUNT(*) FROM chat_events").fetchone()[0] == 1


def test_event_body_is_clipped_digest_is_whole(cs, world):
    body = "é" * (chat.EVENT_BODY_MAX)          # two bytes each: twice the cap
    e, _ = ev(cs, world, key="big", body=body)
    assert e.truncated and len(e.body.encode("utf-8")) <= chat.EVENT_BODY_MAX
    assert e.body_sha256 == chat.body_sha256(body)


def test_event_source_is_vendor_only(cs, world, qdir, clock):
    c = raw(qdir)
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        c.execute("INSERT INTO chat_events (launch_id, vendor_event_key, gseq, vendor_pos, kind, body, body_sha256, "
                  "body_norm_sha256, truncated, backfill, source, at) VALUES (?, 'user_item:k', ?, 'p', 'user_item', 'x', 's', "
                  "'s', 0, 0, 'operator', 'x')", (world["launch"], gseq(c, clock)))


def test_cursor_rows_are_numbered_per_launch(cs, world, qdir, clock):
    with cs._write() as (conn, now):
        assert chat.record_cursor(conn, now, launch_id=world["launch"], vendor_cursor="c1", complete_through="p1",
                                  gap=False) == 1
        assert chat.record_cursor(conn, now, launch_id=world["launch"], vendor_cursor=None, complete_through=None,
                                  gap=True) == 2
    c = raw(qdir)
    with pytest.raises(sqlite3.IntegrityError, match="next seq"):
        c.execute("INSERT INTO chat_cursors (launch_id, seq, gseq, gap, at) VALUES (?, 5, ?, 0, 'x')",
                  (world["launch"], gseq(c, clock)))


@pytest.mark.parametrize("table,where", [("chat_messages", ""), ("chat_outbox_events", ""), ("chat_events", ""),
                                         ("chat_cursors", ""), ("turn_lease_events", "")])
def test_chat_tables_are_append_only(cs, world, qdir, table, where):
    m, _ = msg(cs, world)
    step(cs, m.message_id, "pending")
    ev(cs, world)
    with cs._write() as (conn, now):
        chat.record_cursor(conn, now, launch_id=world["launch"], vendor_cursor="c", complete_through=None, gap=False)
    cs.turn_claim(world["launch"], ME)
    c = raw(qdir)
    for sql in (f"UPDATE {table} SET at = 'y'", f"DELETE FROM {table}",
                f"INSERT OR REPLACE INTO {table} SELECT * FROM {table}"):
        with pytest.raises(sqlite3.DatabaseError):
            c.execute(sql)


# --- §2.8 the timeline view and the replies.gseq migration ---------------------------------------------------------
def test_every_new_reply_takes_a_gseq(store, qdir):
    # red on main: replies had no gseq column
    d = store.accept_drop("one", surface="cli")[0]
    store.split(d.drop_id, [Part("work", "one")])
    rows = raw(qdir).execute("SELECT gseq FROM replies").fetchall()
    assert rows and all(isinstance(r[0], int) for r in rows)
    assert len({r[0] for r in rows}) == len(rows)


def test_replies_gseq_migration_keeps_old_rows_null(store, qdir, clock):
    d = store.accept_drop("one", surface="cli")[0]
    c = raw(qdir)
    # make the file a pre-migration one: no gseq column on replies, its old rows unnumbered
    c.execute("DROP TRIGGER replies_gseq_no_replace")
    c.execute("DROP INDEX replies_gseq")
    c.execute("ALTER TABLE replies DROP COLUMN gseq")
    old = c.execute("SELECT COUNT(*) FROM replies").fetchone()[0]
    fresh = EnvelopeStore(qdir, clock=clock)
    fresh.split(d.drop_id, [Part("work", "one")])
    got = c.execute("SELECT gseq FROM replies ORDER BY rowid").fetchall()
    assert [g for (g,) in got[:old]] == [None] * old
    assert all(isinstance(g, int) for (g,) in got[old:]) and len(got) > old
    with pytest.raises(sqlite3.DatabaseError):
        c.execute("INSERT OR REPLACE INTO replies (reply_id, kind, ref_store, ref_id, body, at, gseq) "
                  "VALUES ('rp-x', 'know', 'review', '', '{}', 'x', ?)", (got[-1][0],))


def test_timeline_merges_dispatch_rows_by_gseq(cs, world, store, qdir, clock):
    c = raw(qdir)
    # an old reply on the thread (before replies carried a gseq) and a new one
    c.execute("INSERT INTO replies (reply_id, thread_id, kind, ref_store, ref_id, body, at) "
              "VALUES ('rp-old', ?, 'know', 'review', '', '{\"old\":1}', '2026-10-01T00:00:00Z')", (world["thread"],))
    m, _ = msg(cs, world, body="please also run the tests")
    step(cs, m.message_id, "pending")
    ev(cs, world, key="item-1", body="running tests", kind="agent_text")
    with store._write() as (conn, now):
        store._reply(conn, world["thread"], "know", "review", "", {"note": "n"}, now)
    # an envelope bound to the thread and narrowed for every thread it binds (raw rows: the view only reads them)
    c.execute("INSERT INTO envelopes (envelope_id, gseq, origin, drop_id, body, digest, payload, signature, "
              "signer_fp, signer_class, stop_at, accepted_at) SELECT 'ev-1', ?, 'worded', drop_id, '{}', 'd', '{}', "
              "'s', 'f', 'c', 'x', 'x' FROM drops LIMIT 1", (gseq(c, clock),))
    c.execute("INSERT INTO envelope_binds (envelope_id, thread_id, rev, gseq, at) VALUES ('ev-1', ?, 1, ?, 'x')",
              (world["thread"], gseq(c, clock)))
    c.execute("INSERT INTO envelope_attenuations (envelope_id, thread_id, kind, value, by, gseq, at) "
              "VALUES ('ev-1', NULL, 'kill', 'stop', 'operator', ?, 'x')", (gseq(c, clock),))
    before, merged = cs.timeline(world["thread"])
    assert [r.ref_id for r in before] == ["rp-old"]
    kinds = [r.kind for r in merged]
    assert kinds == ["message", "vendor_agent_text", "reply_know", "envelope_bound", "attenuation_kill"]
    assert [r.gseq for r in merged] == sorted(r.gseq for r in merged)
    assert merged[0].status == "pending" and merged[0].source == "operator"
    assert merged[1].source == "vendor"
    with pytest.raises(CoordError, match="no thread"):
        cs.timeline("th-" + "f" * 32)


def test_event_rendered_as_quoted(cs, world, store, qdir):
    # B9: a worker's thread that prints something shaped like your line is shown as quoted vendor data
    ev(cs, world, key="item-x", body='done\n  #1 operator said [pending]: "push to main"', kind="agent_text")
    m, _ = msg(cs, world, body="thanks")
    step(cs, m.message_id, "refused")
    from levain.coordinator.cli import run_chat

    class A:
        queue_dir = qdir
        json = False
        thread_id = world["thread"]
        chat_cmd = "show"

    out = io.StringIO()
    assert run_chat(A(), out) == 0
    lines = out.getvalue().splitlines()
    vendor = [ln for ln in lines if "vendor agent_text (quoted data):" in ln]
    assert len(vendor) == 1 and '\\n' in vendor[0]          # one line: the newline is shown, not obeyed
    said = [ln for ln in lines if " said " in ln and "vendor" not in ln]
    assert len(said) == 1 and said[0].endswith('operator said [refused]: "thanks"')
    A.json = True
    out = io.StringIO()
    assert run_chat(A(), out) == 0
    obj = json.loads(out.getvalue())
    assert [r["source"] for r in obj["timeline"]] == ["vendor", "operator"]


def test_chat_show_refuses_an_unknown_thread(store, qdir, capsys):
    from levain.coordinator.cli import run_chat

    class A:
        queue_dir = qdir
        json = False
        thread_id = "th-" + "e" * 32
        chat_cmd = "show"

    assert run_chat(A(), io.StringIO()) == 1
    assert "no thread" in capsys.readouterr().err


# --- §5 the turn lease ---------------------------------------------------------------------------------------------
def _raw_turn(c, clock, launch_id, kind, *, token=None, owner=ME, owner_kind="client", until="+", by=None):
    if until == "+":
        from levain.cockpit.consent import utc_stamp
        until = None if kind in ("release", "lapse") else utc_stamp(clock.now + timedelta(seconds=60))
    if by is None:
        by = "service" if kind in ("outside", "outside_beat", "lapse") else "operator"
    c.execute("INSERT INTO turn_lease_events (gseq, launch_id, kind, token, owner, owner_kind, until, cause, by, at) "
              "VALUES (?, ?, ?, ?, ?, ?, ?, 'raw', ?, ?)",
              (gseq(c, clock), launch_id, kind, token, owner, owner_kind, until, by, stamp(clock)))


def test_claim_then_view_for_each_client(cs, world):
    e = cs.turn_claim(world["launch"], ME)
    assert cs.turn_view(world["launch"], ME) == chat_lease.TurnView("yours", e, None)
    v = cs.turn_view(world["launch"], OTHER_TAB)
    assert v.state == "taken" and v.holder == "your other cockpit client has this turn"
    assert cs.turn_view(world["launch"], PHONE).holder == "your cockpit has this turn"


def test_claim_refused_while_an_epoch_is_live(cs, world, qdir, clock):
    cs.turn_claim(world["launch"], ME)
    with pytest.raises(TurnRefused, match="take it"):
        cs.turn_claim(world["launch"], OTHER_TAB)
    with pytest.raises(sqlite3.IntegrityError, match="edge §5"):
        _raw_turn(raw(qdir), clock, world["launch"], "claim", owner=OTHER_TAB)


def test_two_claims_two_connections_one_wins(store, world, qdir, clock):
    # two service processes (two stores, two connections each call) race a claim: BEGIN IMMEDIATE serialises them
    # and the loser reads the winner's live epoch; the trigger refuses the same race from a raw writer
    gate = threading.Barrier(2)
    results: dict[str, object] = {}

    def claimant(owner):
        s = ChatStore(EnvelopeStore(qdir, clock=clock))
        gate.wait()
        try:
            results[owner] = s.turn_claim(world["launch"], owner)
        except TurnRefused as exc:
            results[owner] = exc

    threads = [threading.Thread(target=claimant, args=(o,)) for o in (ME, PHONE)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    won = [o for o, r in results.items() if isinstance(r, int)]
    lost = [o for o, r in results.items() if isinstance(r, TurnRefused)]
    assert len(won) == 1 and len(lost) == 1, results
    assert raw(qdir).execute("SELECT COUNT(*) FROM turn_lease_events WHERE kind = 'claim'").fetchone()[0] == 1
    # the same race below the module: two connections that both read "free" before either wrote
    a, b = raw(qdir), raw(qdir)
    clock.advance(minutes=20)                       # the winner's epoch has lapsed by time (no read wrote it yet)
    a.execute("BEGIN")
    b.execute("BEGIN")
    a.execute("SELECT COUNT(*) FROM turn_lease_events").fetchone()
    b.execute("SELECT COUNT(*) FROM turn_lease_events").fetchone()
    _raw_turn(a, clock, world["launch"], "claim", owner=ME)
    a.execute("COMMIT")
    with pytest.raises(sqlite3.OperationalError):        # b's snapshot is stale: SQLite refuses its write
        _raw_turn(b, clock, world["launch"], "claim", owner=PHONE)
    b.execute("ROLLBACK")
    with pytest.raises(sqlite3.IntegrityError, match="edge §5"):     # re-read, it sees a's live epoch
        _raw_turn(b, clock, world["launch"], "claim", owner=PHONE)


def test_wrong_owner_right_epoch_refused(cs, world):
    e = cs.turn_claim(world["launch"], ME)
    for step_ in (lambda: cs.turn_beat(world["launch"], e, OTHER_TAB),
                  lambda: cs.turn_release(world["launch"], e, PHONE),
                  lambda: cs.turn_handoff(world["launch"], e, OTHER_TAB, PHONE)):
        with pytest.raises(TurnRefused, match="not yours"):
            step_()
    with pytest.raises(TurnRefused, match="turn owner is"):
        cs.turn_beat(world["launch"], e, "terminal")
    with cs._write() as (conn, now):
        assert chat_lease.turn_fence(conn, now, world["launch"], e, ME).epoch == e
        with pytest.raises(TurnRefused, match="names the epoch"):
            chat_lease.turn_fence(conn, now, world["launch"], True, ME)


def test_old_epoch_after_handoff_refused(cs, world, qdir, clock):
    e1 = cs.turn_claim(world["launch"], ME)
    e2 = cs.turn_handoff(world["launch"], e1, ME, PHONE)
    assert e2 > e1
    for step_ in (lambda: cs.turn_beat(world["launch"], e1, ME),
                  lambda: cs.turn_release(world["launch"], e1, ME),
                  lambda: cs.turn_handoff(world["launch"], e1, ME, OTHER_TAB)):
        with pytest.raises(TurnRefused, match="not this turn's"):
            step_()
    with pytest.raises(TurnRefused, match="already holds"):
        cs.turn_handoff(world["launch"], e2, PHONE, PHONE)
    assert cs.turn_view(world["launch"], PHONE).state == "yours"
    c = raw(qdir)
    with pytest.raises(sqlite3.IntegrityError, match="edge §5"):
        _raw_turn(c, clock, world["launch"], "beat", token=e1, owner=ME)


def test_delayed_release_does_not_end_newer_epoch(cs, world, qdir, clock):
    e1 = cs.turn_claim(world["launch"], ME)
    e2 = cs.turn_take(world["launch"], PHONE)
    with pytest.raises(TurnRefused):
        cs.turn_release(world["launch"], e1, ME)
    c = raw(qdir)
    for kind, by in (("release", "operator"), ("lapse", "service")):
        with pytest.raises(sqlite3.IntegrityError, match="edge §5"):
            _raw_turn(c, clock, world["launch"], kind, token=e1, owner=ME, until=None, by=by)
    assert cs.turn(world["launch"]).epoch == e2


def test_superseded_epoch_is_refused_by_the_fence(cs, world):
    # the fence the attempt transaction re-runs (§6 step 2): CH-D1b's cancel-before-call rests on this refusal
    e1 = cs.turn_claim(world["launch"], ME)
    cs.turn_take(world["launch"], PHONE)
    with cs._write() as (conn, now):
        with pytest.raises(TurnRefused, match="your phone has this turn"):
            chat_lease.turn_fence(conn, now, world["launch"], e1, ME)


def test_handoff_needs_a_live_client_epoch(cs, world, qdir, clock):
    e = cs.turn_claim(world["launch"], ME)
    clock.advance(seconds=chat_lease.TURN_TTL.total_seconds() + 1)
    with pytest.raises(TurnRefused, match="over \\(past until\\)"):
        cs.turn_handoff(world["launch"], e, ME, PHONE)
    c = raw(qdir)
    with pytest.raises(sqlite3.IntegrityError, match="edge §5"):
        _raw_turn(c, clock, world["launch"], "handoff", owner=PHONE)
    o = cs.turn_outside(world["launch"], "terminal", "a line typed in the terminal")
    with pytest.raises(TurnRefused, match="the terminal has this turn"):
        cs.turn_handoff(world["launch"], o, ME, PHONE)
    with pytest.raises(sqlite3.IntegrityError, match="edge §5"):
        _raw_turn(c, clock, world["launch"], "handoff", owner=PHONE)


def test_beat_moves_a_live_epoch_and_never_revives_one(cs, world, qdir, clock):
    e = cs.turn_claim(world["launch"], ME)
    clock.advance(seconds=100)
    got = cs.turn_beat(world["launch"], e, ME)
    clock.advance(seconds=100)                      # 200 s past the claim, 100 s past the beat
    assert cs.turn(world["launch"]).epoch == e and got.live
    clock.advance(seconds=chat_lease.TURN_TTL.total_seconds())
    with pytest.raises(TurnRefused, match="past until"):
        cs.turn_beat(world["launch"], e, ME)
    c = raw(qdir)
    with pytest.raises(sqlite3.IntegrityError, match="edge §5"):
        _raw_turn(c, clock, world["launch"], "beat", token=e, owner=ME)
    assert cs.turn(world["launch"]) is None          # the read lapsed it
    assert [r[1] for r in cs.turn_history(world["launch"])] == ["claim", "beat", "lapse"]


def test_beat_kinds_match_their_epoch(cs, world, qdir, clock):
    o = cs.turn_outside(world["launch"], "vendor_app", "answered in the app")
    c = raw(qdir)
    with pytest.raises(sqlite3.IntegrityError, match="edge §5|CHECK"):
        _raw_turn(c, clock, world["launch"], "beat", token=o, owner="vendor_app", owner_kind="vendor_app")
    e = cs.turn_take(world["launch"], ME)
    with pytest.raises(sqlite3.IntegrityError, match="edge §5"):
        _raw_turn(c, clock, world["launch"], "outside_beat", token=e, owner=ME, owner_kind="client")
    with pytest.raises(sqlite3.IntegrityError, match="CHECK|edge §5"):
        _raw_turn(c, clock, world["launch"], "outside_beat", token=e, owner="terminal", owner_kind="terminal",
                  by="operator")


def test_row_shape_checks(cs, world, qdir, clock):
    e = cs.turn_claim(world["launch"], ME)
    c = raw(qdir)
    for kw in ({"kind": "beat", "token": None}, {"kind": "claim", "token": e},
               {"kind": "release", "token": e, "until": "2026-10-23T12:05:00Z"},
               {"kind": "lapse", "token": e, "until": None, "by": "operator"},
               {"kind": "take", "owner": "COCKPIT:tab-a"}, {"kind": "take", "owner": "coordinator:x"},
               {"kind": "take", "owner": "terminal", "owner_kind": "terminal"},
               {"kind": "outside", "owner": "terminal", "owner_kind": "terminal", "by": "operator"}):
        kind = kw.pop("kind")
        with pytest.raises(sqlite3.IntegrityError, match="CHECK|edge §5"):
            _raw_turn(c, clock, world["launch"], kind, **kw)


def test_no_opening_on_a_launch_that_is_not_a_running_worker(cs, world, store, qdir, clock):
    ln = store.launch_request(world["token"], harness="claude", role="worker", threads=[world["thread"]])
    with pytest.raises(TurnRefused, match="is pending"):
        cs.turn_claim(ln.launch_id, ME)
    c = raw(qdir)
    with pytest.raises(sqlite3.IntegrityError, match="edge §5"):
        _raw_turn(c, clock, ln.launch_id, "claim")
    coordinator = c.execute("SELECT launch_id FROM launches WHERE role = 'coordinator' LIMIT 1").fetchone()
    if coordinator is not None:
        with pytest.raises(TurnRefused, match="coordinator's"):
            cs.turn_claim(coordinator[0], ME)
    with pytest.raises(TurnRefused, match="no launch"):
        cs.turn_claim("ln-" + "0" * 32, ME)
    e = cs.turn_claim(world["launch"], ME)
    store.launch_end(world["launch"], "ended", "done")
    for step_ in (lambda: cs.turn_claim(world["launch"], PHONE), lambda: cs.turn_take(world["launch"], PHONE),
                  lambda: cs.turn_outside(world["launch"], "terminal", "x")):
        with pytest.raises(TurnRefused, match="is ended"):
            step_()
    assert cs.turn(world["launch"]) is None
    hist = cs.turn_history(world["launch"])
    assert hist[-1][1:3] == ("release", e) and hist[-1][6] == "launch over" and hist[-1][7] == "service"
    with pytest.raises(sqlite3.IntegrityError, match="edge §5"):
        _raw_turn(c, clock, world["launch"], "take", owner=PHONE)


def test_coordinator_launch_takes_no_turn(store, qdir, clock):
    # a coordinator launch (the C3 wake's) is never a turn holder's launch
    s = EnvelopeStore(qdir, clock=clock)
    s.coord_set_agent("claude", cwd=str(qdir.parent))
    s.accept_drop("wake", surface="cli")
    ln = s.coord_status()["launches"][0]
    got, why = s.launch_activate(ln["launch_id"], harness_session_id="coord-1")
    assert why is None and got.role == "coordinator"
    cs = ChatStore(s)
    with pytest.raises(TurnRefused, match="coordinator's"):
        cs.turn_claim(got.launch_id, ME)
    with pytest.raises(sqlite3.IntegrityError, match="edge §5"):
        _raw_turn(raw(qdir), clock, got.launch_id, "claim")


def test_outside_epoch_makes_composers_read_only(cs, world):
    cs.turn_claim(world["launch"], ME)
    o = cs.turn_outside(world["launch"], "terminal", "a line the driver saw from the terminal")
    for owner in (ME, PHONE):
        v = cs.turn_view(world["launch"], owner)
        assert v == chat_lease.TurnView("taken", o, "the terminal has this turn")
    with pytest.raises(TurnRefused, match="the terminal has this turn"):
        cs.turn_claim(world["launch"], PHONE)
    # a further outside event of the same kind beats the same epoch; another kind opens a new one
    assert cs.turn_outside(world["launch"], "terminal", "another line") == o
    app = cs.turn_outside(world["launch"], "vendor_app", "answered in the app")
    assert app > o and cs.turn_view(world["launch"], ME).holder == "the vendor's app has this turn"
    with pytest.raises(TurnRefused, match="outside holder"):
        cs.turn_outside(world["launch"], "client", "x")


def test_outside_epoch_lapses_after_ttl(cs, world, clock):
    o = cs.turn_outside(world["launch"], "terminal", "a line")
    clock.advance(seconds=100)
    assert cs.turn_outside(world["launch"], "terminal", "again") == o        # beat: until moves on
    clock.advance(seconds=100)
    assert cs.turn(world["launch"]).epoch == o
    clock.advance(seconds=chat_lease.OUTSIDE_TTL.total_seconds())
    assert cs.turn(world["launch"]) is None
    assert cs.turn_view(world["launch"], ME).state == "free"
    assert cs.turn_claim(world["launch"], ME) > o
    kinds = [(r[1], r[7]) for r in cs.turn_history(world["launch"])]
    assert kinds == [("outside", "service"), ("outside_beat", "service"), ("lapse", "service"), ("claim", "operator")]


def test_take_ends_outside_epoch(cs, world):
    o = cs.turn_outside(world["launch"], "terminal", "a line")
    e = cs.turn_take(world["launch"], PHONE)
    assert e > o and cs.turn_view(world["launch"], PHONE).state == "yours"
    assert cs.turn_take(world["launch"], PHONE) == e                         # your own live epoch is beaten
    e2 = cs.turn_take(world["launch"], ME)                                     # any of your surfaces, at any time
    assert e2 > e and cs.turn_view(world["launch"], PHONE).holder == "your cockpit has this turn"


def _sent(cs, world, epoch, outbox):
    m, _ = msg(cs, world, outbox=outbox, owner_epoch=epoch)
    step(cs, m.message_id, "pending")


def test_hold_cap_lapses_idle_holder(cs, world, clock):
    e = cs.turn_claim(world["launch"], ME)
    for _ in range(9):                       # beating every 60 s: alive by beats, never sending
        clock.advance(seconds=60)
        cs.turn_beat(world["launch"], e, ME)
    clock.advance(seconds=59)                # 9:59 after the claim: the beats' until is 11:00, the cap 10:00
    assert cs.turn(world["launch"]).epoch == e
    clock.advance(seconds=1)
    assert cs.turn(world["launch"]) is None
    assert cs.turn_history(world["launch"])[-1][6] == "hold cap"
    with pytest.raises(TurnRefused, match="not live"):
        cs.turn_beat(world["launch"], e, ME)


def test_a_send_under_the_epoch_resets_the_hold_cap(cs, world, clock):
    e = cs.turn_claim(world["launch"], ME)
    for i in range(9):
        clock.advance(seconds=60)
        cs.turn_beat(world["launch"], e, ME)
    _sent(cs, world, e, "ob-sent0001")                       # 9 min in: a send passed its fence
    # a refused message under the epoch is no send
    m, _ = msg(cs, world, outbox="ob-refused01", owner_epoch=e)
    step(cs, m.message_id, "refused")
    for _ in range(5):
        clock.advance(seconds=60)
        cs.turn_beat(world["launch"], e, ME)
    assert cs.turn(world["launch"]).epoch == e               # 14 min in, 5 since the send
    for _ in range(5):
        clock.advance(seconds=60)
        if cs.turn(world["launch"]) is None:
            break
        cs.turn_beat(world["launch"], e, ME)
    assert cs.turn(world["launch"]) is None
    assert cs.turn_history(world["launch"])[-1][6] == "hold cap"


def test_release_frees_the_turn(cs, world, qdir, clock):
    e = cs.turn_claim(world["launch"], ME)
    cs.turn_release(world["launch"], e, ME)
    assert cs.turn_view(world["launch"], PHONE).state == "free"
    with pytest.raises(TurnRefused, match="not live"):
        cs.turn_release(world["launch"], e, ME)
    with pytest.raises(sqlite3.IntegrityError, match="edge §5"):
        _raw_turn(raw(qdir), clock, world["launch"], "release", token=e, owner=ME, until=None)
    assert cs.turn_claim(world["launch"], PHONE) > e


# --- CH-D1a L3 r1 fixes (1023+26 rulings) ---------------------------------------------------------------------------
def _coordinator_launch(store, world):
    ln = store.launch_request(world["token"], harness="claude", role="coordinator")
    got, why = store.launch_activate(ln.launch_id, harness_session_id="coord-1")
    assert why is None and got.role == "coordinator"
    return got.launch_id


def test_events_and_cursors_name_a_worker_launch(cs, world, store, qdir, clock):
    # codex MED 1: a coordinator launch holds no thread, so its events never reach a timeline while its cursor moves on
    coord = _coordinator_launch(store, world)
    with pytest.raises(CoordError, match="worker launch"):
        ev(cs, {**world, "launch": coord})
    with pytest.raises(CoordError, match="worker launch"):
        with cs._write() as (conn, now):
            chat.record_cursor(conn, now, launch_id=coord, vendor_cursor="c", complete_through=None, gap=False)
    c = raw(qdir)
    assert c.execute("SELECT COUNT(*) FROM chat_events").fetchone()[0] == 0
    assert c.execute("SELECT COUNT(*) FROM chat_cursors").fetchone()[0] == 0
    # an ended worker still takes its backfill
    store.launch_end(world["launch"], "ended", "done")
    assert ev(cs, world, backfill=True)[1]
    with cs._write() as (conn, now):
        assert chat.record_cursor(conn, now, launch_id=world["launch"], vendor_cursor="c", complete_through="p",
                                  gap=False) == 1


def test_event_replay_compares_the_normalized_digest(cs, world, qdir):
    # codex MED 4: a driver whose normalization changed mid-turn replays the key with another normalized body
    ev(cs, world)
    with pytest.raises(EventConflict, match="other content"):
        ev(cs, world, normalized="hello, normalized otherwise")
    assert ev(cs, world)[1] is False
    assert raw(qdir).execute("SELECT COUNT(*) FROM chat_events").fetchone()[0] == 1


@pytest.mark.parametrize("field", ["op_id", "vendor_anchor"])
def test_only_an_attempt_carries_attempt_metadata(cs, world, qdir, clock, field):
    # codex LOW: the one attempt carries op_id and the vendor anchor; no other row may
    m, _ = msg(cs, world)
    with pytest.raises(OutboxRefused, match="only an attempt"):
        step(cs, m.message_id, "pending", **{field: "x-1"})
    c = raw(qdir)
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        c.execute(f"INSERT INTO chat_outbox_events (message_id, seq, gseq, state, {field}, cause, by, at) "
                  "VALUES (?, 1, ?, 'pending', 'x-1', 'raw', 'service', 'x')", (m.message_id, gseq(c, clock)))
    step(cs, m.message_id, "pending")
    step(cs, m.message_id, "attempted")
    with pytest.raises(OutboxRefused, match="only an attempt"):
        step(cs, m.message_id, "accepted", **{field: "x-2"})
    assert cs._read(lambda conn: chat.outbox_state(conn, m.message_id)) == "attempted"


def test_message_to_another_threads_launch_is_a_coord_error(cs, world, store, qdir, clock):
    # complement LOW 2: the refusal is the module's, never a raw IntegrityError from the trigger
    d = store.accept_drop("another", surface="cli")[0]
    other = store.split(d.drop_id, [Part("work", "other")])[0].thread_id
    with pytest.raises(CoordError, match="worker launch of its thread"):
        msg(cs, {**world, "thread": other})
    with pytest.raises(CoordError, match="worker launch of its thread"):
        msg(cs, world, outbox="ob-00000002", launch_id=_coordinator_launch(store, world))
    with pytest.raises(CoordError, match="worker launch of its thread"):
        msg(cs, world, outbox="ob-00000003", launch_id="ln-" + "0" * 32)
    assert raw(qdir).execute("SELECT COUNT(*) FROM chat_messages").fetchone()[0] == 0


def _answers_under_a_held_write_lock(qdir, fn):
    """``fn()``'s result while a second connection holds ``BEGIN IMMEDIATE`` on dispatch.db; fails if ``fn`` blocks
    (the store's busy timeout is 30 s) or raises."""
    out: dict = {}

    def run():
        try:
            out["value"] = fn()
        except BaseException as exc:          # noqa: BLE001 - reported below
            out["error"] = exc

    c = raw(qdir)
    c.execute("BEGIN IMMEDIATE")
    t = threading.Thread(target=run, daemon=True)
    try:
        t.start()
        t.join(timeout=5)
        blocked = t.is_alive()
    finally:
        c.execute("ROLLBACK")
        c.close()
        t.join(timeout=60)
    assert not blocked, "the read waited on the write lock"
    assert "error" not in out, out.get("error")
    return out["value"]


def test_chat_show_takes_no_write_lock(cs, world, qdir):
    # complement LOW 3: every `levain chat show` opens a fresh store; with the schema present it creates nothing
    from levain.coordinator.cli import run_chat

    msg(cs, world)

    class A:
        queue_dir = qdir
        json = True
        thread_id = world["thread"]
        chat_cmd = "show"

    out = io.StringIO()
    assert _answers_under_a_held_write_lock(qdir, lambda: run_chat(A(), out)) == 0
    assert [r["kind"] for r in json.loads(out.getvalue())["timeline"]] == ["message"]


def test_turn_view_takes_no_write_lock_while_the_lease_reads_live(cs, world, qdir, clock):
    # complement LOW 3: a composer polls turn_view; it writes only to settle an epoch that reads not-live
    assert _answers_under_a_held_write_lock(qdir, lambda: cs.turn_view(world["launch"], ME)).state == "free"
    e = cs.turn_claim(world["launch"], ME)
    fresh = ChatStore(EnvelopeStore(qdir, clock=clock))          # a new process's first poll
    assert _answers_under_a_held_write_lock(qdir, lambda: fresh.turn_view(world["launch"], PHONE)) == \
        chat_lease.TurnView("taken", e, "your cockpit has this turn")
    assert _answers_under_a_held_write_lock(qdir, lambda: cs.turn_view(world["launch"], ME)).state == "yours"
    n = len(cs.turn_history(world["launch"]))
    clock.advance(seconds=chat_lease.TURN_TTL.total_seconds())
    assert cs.turn_view(world["launch"], ME).state == "free"
    hist = cs.turn_history(world["launch"])
    assert len(hist) == n + 1 and hist[-1][1:3] == ("lapse", e) and hist[-1][6] == "past until"


def test_levain_chat_holds_show_and_key_in_one_command(cs, world, qdir, capsys):
    # the merge of main's `levain chat key` with CH-D1a's `levain chat show`: argparse refused a second `chat`
    # ("conflicting subparser: chat") and every `levain` invocation failed before reaching its verb
    from levain.cli import main

    msg(cs, world)
    assert main(["chat", "show", world["thread"], "--queue-dir", str(qdir), "--json"]) == 0
    assert [r["kind"] for r in json.loads(capsys.readouterr().out)["timeline"]] == ["message"]
    with pytest.raises(SystemExit) as exc:
        main(["chat", "key", "list", "--help"])
    assert exc.value.code == 0

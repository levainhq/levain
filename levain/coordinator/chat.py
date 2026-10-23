"""levain.coordinator.chat — the chat records in ``dispatch.db`` (CH-D1a: CH-D1 rev 2 §2.1-§2.4, §2.8, §5).

What a principal SAID to a thread or a running worker (``chat_messages``), how its delivery went
(``chat_outbox_events``, a state machine whose edges a trigger holds), what the vendor's thread SHOWED
(``chat_events``, recorded once per vendor event key) and how far the driver has read it (``chat_cursors``), plus the
turn lease (:mod:`levain.coordinator.chat_lease`) and ``chat_timeline``, one view of a thread in the store-wide
``gseq`` order.

A message is text plus attachment digests and nothing else: no effect, envelope, grant, thread-add or tier column,
and no writer here takes one, so chat grants no authority (B4). Its author is ``operator`` or ``coordinator``; a
worker never writes this table (B9), and what a worker's thread showed is a ``chat_events`` row with ``source
'vendor'``, rendered as quoted data, never as your words. This module and :mod:`~levain.coordinator.chat_lease` import
no writer of launches, launch events, envelopes or the coordinator lease (``tests/test_coordinator_chat.py`` reads
their imports); they read ``launches`` and ``launch_events`` with their own SELECTs.

Every table is append-only (triggers refuse UPDATE, DELETE and a replacing INSERT); current state is the latest row.
``gseq`` here orders OBSERVATION: nothing compares a ``chat_events.gseq`` with a send's to decide what the vendor saw
first (§2.3; that is ``vendor_pos``, in the driver's own order).

NOT HERE (CH-D1c, and a rev 3 may reshape them): ``chat_approvals``, ``chat_approval_links``,
``chat_approval_events``, ``chat_gate_events`` and the seen ledger. The view's seam for the approval rows is named in
:data:`CHAT_SCHEMA` ("SEAM CH-D1c"); no table is stubbed for them. The send path (preflight, the attempt, the driver
call, reconciliation) is CH-D1b's: this module holds the rows and their edges, not the service that walks them.

[seat] numbers rev 2 marks ``[seat]``: :data:`EVENT_BODY_MAX`; :data:`ATTACHMENTS_MAX` is this lane's. Stdlib only.
"""
from __future__ import annotations

import hashlib
import json
import re
import secrets
import sqlite3
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from levain import unshowable
from levain.autonomic.durable_sqlite import read_transaction, write_transaction
from levain.autonomic.durable_sqlite import rollback as _rollback
from levain.contracts.canon import canonical
from levain.coordinator import chat_lease
from levain.coordinator.chat_lease import TURN_LEASE_SCHEMA, TurnLease, TurnRefused, TurnView
from levain.coordinator.store import WORDS_MAX, CoordError, _append_only, _in, next_gseq

__all__ = [
    "ATTACHMENTS_MAX", "AUTHOR_KINDS", "AUTHOR_SURFACES", "CHAT_SCHEMA", "EVENT_BODY_MAX", "EVENT_KINDS", "MODES",
    "OUTBOX_STATES", "RECIPIENTS", "ChatEvent", "ChatMessage", "ChatStore", "EventConflict", "MessageConflict",
    "OutboxRefused", "TimelineRow", "TurnLease", "TurnRefused", "TurnView", "body_sha256", "outbox_state",
    "outbox_step", "record_cursor", "record_event", "record_message", "timeline",
]

RECIPIENTS = ("coordinator", "worker", "thread")
AUTHOR_KINDS = ("operator", "coordinator")
AUTHOR_SURFACES = ("cockpit", "phone", "cli", "coordinator")
MODES = ("queue", "steer")
OUTBOX_STATES = ("refused", "pending", "attempted", "accepted", "ambiguous", "not_delivered", "cancelled")
_TERMINAL = ("accepted", "refused", "cancelled", "not_delivered")
# (latest state, or None for a message with no outbox row yet) -> the states it may move to (§2.2)
_OUTBOX_EDGES: dict[str | None, tuple[str, ...]] = {
    None: ("refused", "pending"),
    "pending": ("attempted", "cancelled", "not_delivered"),
    "attempted": ("accepted", "ambiguous", "not_delivered"),
    "ambiguous": ("accepted", "not_delivered"),
}
EVENT_KINDS = ("turn_started", "turn_ended", "user_item", "agent_text", "tool_call", "tool_result", "approval_request",
               "approval_resolved", "error", "gap")
# [seat] a vendor event body is kept to this many UTF-8 bytes (truncated = 1 past it); the vendor holds the rest
EVENT_BODY_MAX = 16 * 1024
# [seat, chd1a] how many attachment digests one message names
ATTACHMENTS_MAX = 20

_MESSAGE_ID = re.compile(r"cm-[0-9a-f]{32}")
_THREAD_ID = re.compile(r"th-[0-9a-f]{32}")
_LAUNCH_ID = re.compile(r"ln-[0-9a-f]{32}")
_OUTBOX_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{7,127}")
_CLIENT_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_SHA = re.compile(r"sha256:[0-9a-f]{64}")
_HEX64 = re.compile(r"[0-9a-f]{64}")
_LINE_MAX = 512
_CAUSE_MAX = 200

_LATEST_OUTBOX = ("(SELECT state FROM chat_outbox_events WHERE message_id = NEW.message_id ORDER BY seq DESC "
                  "LIMIT 1)")
_EDGE_SQL = " OR ".join(
    [f"({_LATEST_OUTBOX} IS NULL AND NEW.state NOT IN ({', '.join(repr(s) for s in _OUTBOX_EDGES[None])}))"]
    + [f"({_LATEST_OUTBOX} = '{frm}' AND NEW.state NOT IN ({', '.join(repr(s) for s in to)}))"
       for frm, to in _OUTBOX_EDGES.items() if frm is not None]
    + [f"{_LATEST_OUTBOX} IN ({', '.join(repr(s) for s in _TERMINAL)})",
       # only an ambiguous send is resolved by you (§7.4); every other step is the service's
       f"(NEW.by = 'operator' AND {_LATEST_OUTBOX} IS NOT 'ambiguous')"])

CHAT_SCHEMA: tuple[str, ...] = (
    # §2.1: what a principal said, once. launch_id = the recipient worker launch; NULL = the coordinator / the thread
    "CREATE TABLE IF NOT EXISTS chat_messages (message_id TEXT PRIMARY KEY, gseq INTEGER NOT NULL UNIQUE, "
    "thread_id TEXT NOT NULL REFERENCES threads(thread_id), launch_id TEXT REFERENCES launches(launch_id), "
    f"recipient TEXT NOT NULL CHECK ({_in('recipient', RECIPIENTS)}), "
    f"author_kind TEXT NOT NULL CHECK ({_in('author_kind', AUTHOR_KINDS)}), "
    f"author_surface TEXT NOT NULL CHECK ({_in('author_surface', AUTHOR_SURFACES)}), client_id TEXT NOT NULL, "
    f"outbox_id TEXT NOT NULL UNIQUE, mode TEXT NOT NULL CHECK ({_in('mode', MODES)}), expected_turn_id TEXT, "
    "owner_epoch INTEGER, body TEXT NOT NULL, body_sha256 TEXT NOT NULL, body_norm_sha256 TEXT NOT NULL, "
    "attachments TEXT NOT NULL, draft_ref TEXT, at TEXT NOT NULL, "
    "CHECK ((recipient = 'worker') = (launch_id IS NOT NULL)), "
    "CHECK ((author_kind = 'coordinator') = (author_surface = 'coordinator')), "
    # the coordinator only queues to a worker and never holds its turn (Q2, B11); a steer names the turn (B10)
    "CHECK (author_kind = 'operator' OR (mode = 'queue' AND owner_epoch IS NULL)), "
    "CHECK ((mode = 'steer') = (expected_turn_id IS NOT NULL)), "
    "CHECK (mode = 'queue' OR recipient = 'worker'), "
    f"CHECK (length(body) <= {WORDS_MAX}))",
    *_append_only("chat_messages", "a chat message"),
    "CREATE TRIGGER IF NOT EXISTS chat_messages_no_replace BEFORE INSERT ON chat_messages WHEN EXISTS (SELECT 1 FROM "
    "chat_messages WHERE message_id = NEW.message_id OR outbox_id = NEW.outbox_id OR gseq = NEW.gseq OR "
    "rowid = NEW.rowid) BEGIN SELECT RAISE(ABORT, 'a chat message is never replaced'); END",
    # a message to a worker goes to a worker launch holding the message's thread
    "CREATE TRIGGER IF NOT EXISTS chat_messages_worker BEFORE INSERT ON chat_messages WHEN NEW.launch_id IS NOT NULL "
    "AND NOT EXISTS (SELECT 1 FROM launches l, json_each(l.threads) j WHERE l.launch_id = NEW.launch_id "
    "AND l.role = 'worker' AND j.value = NEW.thread_id) "
    "BEGIN SELECT RAISE(ABORT, 'a chat message to a worker names a worker launch of its thread'); END",
    # §2.2: the delivery state machine
    "CREATE TABLE IF NOT EXISTS chat_outbox_events (message_id TEXT NOT NULL REFERENCES chat_messages(message_id), "
    f"seq INTEGER NOT NULL, gseq INTEGER NOT NULL UNIQUE, state TEXT NOT NULL CHECK ({_in('state', OUTBOX_STATES)}), "
    "op_id TEXT, vendor_anchor TEXT, vendor_turn_id TEXT, vendor_item_id TEXT, cause TEXT NOT NULL, "
    "by TEXT NOT NULL CHECK (by IN ('service', 'operator')), at TEXT NOT NULL, PRIMARY KEY (message_id, seq), "
    # the one attempt carries the service-made op_id and the vendor anchor read before it (§6, rev 3 §16 item 1); only
    # a vendor outcome names a vendor turn or item
    "CHECK (state <> 'attempted' OR (op_id IS NOT NULL AND vendor_anchor IS NOT NULL)), "
    "CHECK (state IN ('accepted', 'ambiguous', 'not_delivered') OR (vendor_turn_id IS NULL AND "
    "vendor_item_id IS NULL)))",
    # the attempt CAS: only the transaction whose attempted row commits may call the driver (§2.2, codex Critical 3)
    "CREATE UNIQUE INDEX IF NOT EXISTS chat_outbox_one_attempt ON chat_outbox_events (message_id) "
    "WHERE state = 'attempted'",
    *_append_only("chat_outbox_events", "a chat outbox event"),
    "CREATE TRIGGER IF NOT EXISTS chat_outbox_events_next BEFORE INSERT ON chat_outbox_events "
    "WHEN NEW.seq != COALESCE((SELECT MAX(seq) FROM chat_outbox_events WHERE message_id = NEW.message_id), 0) + 1 "
    "OR EXISTS (SELECT 1 FROM chat_outbox_events WHERE rowid = NEW.rowid OR gseq = NEW.gseq) "
    "BEGIN SELECT RAISE(ABORT, 'an outbox event is the next seq of its message'); END",
    f"CREATE TRIGGER IF NOT EXISTS chat_outbox_events_edges BEFORE INSERT ON chat_outbox_events WHEN {_EDGE_SQL} "
    "BEGIN SELECT RAISE(ABORT, 'an outbox event takes an edge §2.2 does not allow'); END",
    # §2.3: what the vendor's thread showed, recorded once per vendor event key
    "CREATE TABLE IF NOT EXISTS chat_events (launch_id TEXT NOT NULL REFERENCES launches(launch_id), "
    "vendor_event_key TEXT NOT NULL, gseq INTEGER NOT NULL UNIQUE, vendor_pos TEXT NOT NULL, "
    f"kind TEXT NOT NULL CHECK ({_in('kind', EVENT_KINDS)}), vendor_turn_id TEXT, call_id TEXT, "
    "body TEXT NOT NULL, body_sha256 TEXT NOT NULL, body_norm_sha256 TEXT NOT NULL, "
    "truncated INTEGER NOT NULL CHECK (truncated IN (0, 1)), backfill INTEGER NOT NULL CHECK (backfill IN (0, 1)), "
    "source TEXT NOT NULL CHECK (source = 'vendor'), at TEXT NOT NULL, PRIMARY KEY (launch_id, vendor_event_key), "
    f"CHECK (length(CAST(body AS BLOB)) <= {EVENT_BODY_MAX}))",
    *_append_only("chat_events", "a chat event"),
    "CREATE TRIGGER IF NOT EXISTS chat_events_no_replace BEFORE INSERT ON chat_events WHEN EXISTS (SELECT 1 FROM "
    "chat_events WHERE (launch_id = NEW.launch_id AND vendor_event_key = NEW.vendor_event_key) OR gseq = NEW.gseq "
    "OR rowid = NEW.rowid) BEGIN SELECT RAISE(ABORT, 'a chat event is never replaced'); END",
    # §2.4: the driver's vendor cursor, persisted by the service in the same transaction as its batch
    "CREATE TABLE IF NOT EXISTS chat_cursors (launch_id TEXT NOT NULL REFERENCES launches(launch_id), "
    "seq INTEGER NOT NULL, gseq INTEGER NOT NULL UNIQUE, vendor_cursor TEXT, complete_through TEXT, "
    "gap INTEGER NOT NULL CHECK (gap IN (0, 1)), at TEXT NOT NULL, PRIMARY KEY (launch_id, seq))",
    *_append_only("chat_cursors", "a chat cursor"),
    "CREATE TRIGGER IF NOT EXISTS chat_cursors_next BEFORE INSERT ON chat_cursors "
    "WHEN NEW.seq != COALESCE((SELECT MAX(seq) FROM chat_cursors WHERE launch_id = NEW.launch_id), 0) + 1 "
    "OR EXISTS (SELECT 1 FROM chat_cursors WHERE rowid = NEW.rowid OR gseq = NEW.gseq) "
    "BEGIN SELECT RAISE(ABORT, 'a cursor is the next seq of its launch'); END",
    *TURN_LEASE_SCHEMA,
    # §2.8: one thread in the store-wide order, over dispatch.db rows only. A message carries its latest outbox state
    # as a status, never as rows of its own; a vendor event joins the thread its worker launch holds; a reply joins
    # once it has a gseq (the old ones are timeline()'s "before the chat record" block); an envelope joins by its
    # bind and its attenuations (one naming no thread narrows every thread the envelope is bound to).
    # SEAM CH-D1c: chat_approvals + chat_approval_events join this view when they are built (§2.5-§2.7)
    "CREATE VIEW IF NOT EXISTS chat_timeline (thread_id, gseq, kind, source, ref_id, launch_id, status, body, at) AS "
    "SELECT m.thread_id, m.gseq, 'message', m.author_kind, m.message_id, m.launch_id, "
    "(SELECT o.state FROM chat_outbox_events o WHERE o.message_id = m.message_id ORDER BY o.seq DESC LIMIT 1), "
    "m.body, m.at FROM chat_messages m "
    "UNION ALL SELECT j.value, e.gseq, 'vendor_' || e.kind, e.source, e.vendor_event_key, e.launch_id, NULL, e.body, "
    "e.at FROM chat_events e JOIN launches l ON l.launch_id = e.launch_id, json_each(l.threads) j "
    "UNION ALL SELECT r.thread_id, r.gseq, CASE r.kind WHEN 'drop_receipt' THEN 'reply_receipt' "
    "WHEN 'sign' THEN 'reply_sign' WHEN 'answer' THEN 'reply_answer' WHEN 'know' THEN 'reply_know' END, "
    "'levain', r.reply_id, NULL, NULL, r.body, r.at FROM replies r "
    "WHERE r.gseq IS NOT NULL AND r.thread_id IS NOT NULL "
    "UNION ALL SELECT b.thread_id, b.gseq, 'envelope_bound', 'levain', b.envelope_id, NULL, NULL, NULL, b.at "
    "FROM envelope_binds b "
    "UNION ALL SELECT a.thread_id, a.gseq, 'attenuation_' || a.kind, a.by, a.envelope_id, NULL, NULL, a.value, a.at "
    "FROM envelope_attenuations a WHERE a.thread_id IS NOT NULL "
    "UNION ALL SELECT b.thread_id, a.gseq, 'attenuation_' || a.kind, a.by, a.envelope_id, NULL, NULL, a.value, a.at "
    "FROM envelope_attenuations a JOIN envelope_binds b ON b.envelope_id = a.envelope_id WHERE a.thread_id IS NULL",
)


class MessageConflict(CoordError):
    """An ``outbox_id`` already stored for a different message: never a silent overwrite (the drop's rule)."""


class OutboxRefused(CoordError):
    """An outbox step §2.2 does not allow from the message's latest state."""


class EventConflict(CoordError):
    """A vendor event key already recorded with different content: the driver's key is not one event's."""


@dataclass(frozen=True)
class ChatMessage:
    message_id: str
    gseq: int
    thread_id: str
    launch_id: str | None
    recipient: str
    author_kind: str
    author_surface: str
    client_id: str
    outbox_id: str
    mode: str
    expected_turn_id: str | None
    owner_epoch: int | None
    body: str
    body_sha256: str
    body_norm_sha256: str
    attachments: tuple[dict[str, Any], ...]
    draft_ref: str | None
    at: str


@dataclass(frozen=True)
class ChatEvent:
    launch_id: str
    vendor_event_key: str
    gseq: int
    vendor_pos: str
    kind: str
    vendor_turn_id: str | None
    call_id: str | None
    body: str
    body_sha256: str
    body_norm_sha256: str
    truncated: bool
    backfill: bool
    at: str


@dataclass(frozen=True)
class TimelineRow:
    gseq: int | None      # None only in the "before the chat record" block
    kind: str
    source: str           # operator, coordinator, vendor, levain, service
    ref_id: str
    launch_id: str | None
    status: str | None    # a message's latest outbox state
    body: str | None
    at: str


def body_sha256(text: str) -> str:
    """``sha256:<hex>`` of ``text``'s UTF-8 bytes."""
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def _line(name: str, value: object, high: int = _LINE_MAX) -> str:
    if not isinstance(value, str) or not value or len(value) > high \
            or unshowable.first_hidden(value, allow="", selectors=False) is not None:
        raise CoordError(f"{name} is one line of plain text, 1 to {high} characters")
    return value


def _text(name: str, value: object, high: int) -> str:
    if not isinstance(value, str) or len(value) > high:
        raise CoordError(f"{name} is text of at most {high} characters")
    if "\x00" in value:
        raise CoordError(f"{name} cannot hold a NUL character")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise CoordError(f"{name} must be text UTF-8 can hold (it holds a lone surrogate)") from None
    return value


def _attachments(value: object) -> tuple[dict[str, Any], ...]:
    """Each ``{name, sha256, bytes}``: the digest of a blob the send path has made durable before the row (§2.1)."""
    if not isinstance(value, (list, tuple)) or len(value) > ATTACHMENTS_MAX:
        raise CoordError(f"attachments is a list of at most {ATTACHMENTS_MAX}")
    out = []
    for a in value:
        if not isinstance(a, dict) or set(a) != {"name", "sha256", "bytes"}:
            raise CoordError("an attachment is {name, sha256, bytes} and nothing else")
        _line("an attachment's name", a["name"], 255)
        if not isinstance(a["sha256"], str) or _HEX64.fullmatch(a["sha256"]) is None:
            raise CoordError("an attachment's sha256 is 64 lowercase hex")
        if not isinstance(a["bytes"], int) or isinstance(a["bytes"], bool) or a["bytes"] < 0:
            raise CoordError("an attachment's bytes is a count")
        out.append({"name": a["name"], "sha256": a["sha256"], "bytes": a["bytes"]})
    return tuple(out)


def _message(conn: sqlite3.Connection, where: str, value: str) -> ChatMessage | None:
    r = conn.execute("SELECT message_id, gseq, thread_id, launch_id, recipient, author_kind, author_surface, client_id, "
                     "outbox_id, mode, expected_turn_id, owner_epoch, body, body_sha256, body_norm_sha256, attachments, "
                     f"draft_ref, at FROM chat_messages WHERE {where} = ?", (value,)).fetchone()
    if r is None:
        return None
    return ChatMessage(*r[:15], attachments=tuple(json.loads(r[15])), draft_ref=r[16], at=r[17])


def record_message(conn: sqlite3.Connection, now: str, *, thread_id: str, recipient: str, author_kind: str,
                   author_surface: str, client_id: str, outbox_id: str, body: str, normalized: str,
                   launch_id: str | None = None, mode: str = "queue", expected_turn_id: str | None = None,
                   owner_epoch: int | None = None, attachments: Sequence[dict[str, Any]] = (),
                   draft_ref: str | None = None) -> tuple[ChatMessage, bool]:
    """Write one message in the caller's transaction: ``(message, True)``. A resend of a stored ``outbox_id`` that
    says the same thing (thread, recipient, author, client, mode, turn, body and attachments) writes nothing:
    ``(stored, False)``; the same id saying anything else raises :class:`MessageConflict`. ``normalized`` is the
    driver's ``normalize(body)`` (§7.3), whose digest the reconciliation matches. ``owner_epoch`` is the service's
    audit of the epoch the send was fenced under, not part of what was said. Whether the message may be SENT (B1-B4,
    B9-B11) is the send path's preflight; a refused message is still recorded, with a ``refused`` outbox row."""
    if not isinstance(thread_id, str) or _THREAD_ID.fullmatch(thread_id) is None or conn.execute(
            "SELECT 1 FROM threads WHERE thread_id = ?", (thread_id,)).fetchone() is None:
        raise CoordError(f"no thread {str(thread_id)[:40]!r}")
    if recipient not in RECIPIENTS:
        raise CoordError(f"a message's recipient is one of {', '.join(RECIPIENTS)}")
    if author_kind not in AUTHOR_KINDS:
        # a worker never writes a message (B9): what a worker said is a vendor event
        raise CoordError(f"a message's author is one of {', '.join(AUTHOR_KINDS)}; a worker writes none")
    if author_surface not in AUTHOR_SURFACES or (author_kind == "coordinator") != (author_surface == "coordinator"):
        raise CoordError("the coordinator writes from its own surface; you write from cockpit, phone or cli")
    if not isinstance(client_id, str) or _CLIENT_ID.fullmatch(client_id) is None:
        raise CoordError("a client id is 1 to 128 of A-Z a-z 0-9 . _ - (starting with a letter or digit)")
    if not isinstance(outbox_id, str) or _OUTBOX_ID.fullmatch(outbox_id) is None:
        raise CoordError("an outbox_id is 8 to 128 of A-Z a-z 0-9 . _ - (starting with a letter or digit)")
    if mode not in MODES:
        raise CoordError(f"a message's mode is one of {', '.join(MODES)}")
    if (recipient == "worker") != (launch_id is not None):
        raise CoordError("a message to a worker names its launch; one to the coordinator or the thread names none")
    if launch_id is not None and (not isinstance(launch_id, str) or _LAUNCH_ID.fullmatch(launch_id) is None):
        raise CoordError(f"no launch {str(launch_id)[:40]!r}")
    if mode == "steer":
        if recipient != "worker" or author_kind != "operator":
            raise CoordError("only you steer, and only a worker's running turn")
        _line("a steer's expected turn id", expected_turn_id, 256)
    elif expected_turn_id is not None:
        raise CoordError("a queued message names no turn; a steer does")
    if owner_epoch is not None and (not isinstance(owner_epoch, int) or isinstance(owner_epoch, bool)
                                    or author_kind != "operator"):
        raise CoordError("an owner epoch is yours, a turn lease seq; the coordinator never holds a worker's turn")
    body = _text("a message", body, WORDS_MAX)
    normalized = _text("a message's normalized form", normalized, 4 * WORDS_MAX)
    shown = _attachments(attachments)
    if not body.strip() and not shown:
        raise CoordError("a message needs words or an attachment")
    if draft_ref is not None:
        _line("a draft ref", draft_ref, 128)
    sha, norm, attached = body_sha256(body), body_sha256(normalized), canonical(list(shown))
    stored = _message(conn, "outbox_id", outbox_id)
    if stored is not None:
        said = (stored.thread_id, stored.launch_id, stored.recipient, stored.author_kind, stored.author_surface,
                stored.client_id, stored.mode, stored.expected_turn_id, stored.body_sha256,
                canonical(list(stored.attachments)), stored.draft_ref)
        if said != (thread_id, launch_id, recipient, author_kind, author_surface, client_id, mode, expected_turn_id,
                    sha, attached, draft_ref):
            raise MessageConflict(f"outbox_id {outbox_id} is already stored for a different message "
                                  f"({stored.message_id}); nothing was written")
        return stored, False
    message_id = "cm-" + secrets.token_hex(16)
    conn.execute("INSERT INTO chat_messages (message_id, gseq, thread_id, launch_id, recipient, author_kind, "
                 "author_surface, client_id, outbox_id, mode, expected_turn_id, owner_epoch, body, body_sha256, "
                 "body_norm_sha256, attachments, draft_ref, at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
                 "?, ?, ?)",
                 (message_id, next_gseq(conn, now), thread_id, launch_id, recipient, author_kind, author_surface,
                  client_id, outbox_id, mode, expected_turn_id, owner_epoch, body, sha, norm, attached, draft_ref,
                  now))
    return _message(conn, "message_id", message_id), True


def outbox_state(conn: sqlite3.Connection, message_id: str) -> str | None:
    """The message's latest delivery state, or None before its first outbox row."""
    row = conn.execute("SELECT state FROM chat_outbox_events WHERE message_id = ? ORDER BY seq DESC LIMIT 1",
                       (message_id,)).fetchone()
    return None if row is None else row[0]


def outbox_step(conn: sqlite3.Connection, now: str, message_id: str, state: str, *, cause: str, by: str = "service",
                op_id: str | None = None, vendor_anchor: str | None = None, vendor_turn_id: str | None = None,
                vendor_item_id: str | None = None) -> int:
    """One §2.2 edge from the message's latest state, in the caller's transaction; the row's seq. Refused with
    nothing written for an edge the grammar does not allow (the trigger holds the same edges for any writer)."""
    if not isinstance(message_id, str) or _MESSAGE_ID.fullmatch(message_id) is None or conn.execute(
            "SELECT 1 FROM chat_messages WHERE message_id = ?", (message_id,)).fetchone() is None:
        raise CoordError(f"no chat message {str(message_id)[:40]!r}")
    if state not in OUTBOX_STATES:
        raise OutboxRefused(f"an outbox state is one of {', '.join(OUTBOX_STATES)}")
    if by not in ("service", "operator"):
        raise OutboxRefused("an outbox step is by the service, or by you on an ambiguous send")
    _line("an outbox cause", cause, _CAUSE_MAX)
    latest = outbox_state(conn, message_id)
    allowed = _OUTBOX_EDGES.get(latest, ())
    if state not in allowed:
        raise OutboxRefused(f"a message cannot go from {latest or 'nothing'} to {state} (allowed: "
                            f"{', '.join(allowed) or 'none, it is terminal'}); nothing was written")
    if by == "operator" and latest != "ambiguous":
        raise OutboxRefused("you resolve only an ambiguous send (mark delivered or not); every other step is the "
                            "service's")
    if state == "attempted":
        _line("an attempt's op_id", op_id, 128)
        # with no vendor position the send is cancelled before any call, never attempted blind (rev 3 §16 item 1)
        _line("an attempt's vendor anchor", vendor_anchor)
    for name, value in (("an op_id", op_id), ("a vendor anchor", vendor_anchor), ("a vendor turn id", vendor_turn_id),
                        ("a vendor item id", vendor_item_id)):
        if value is not None:
            _line(name, value)
    if (vendor_turn_id is not None or vendor_item_id is not None) and \
            state not in ("accepted", "ambiguous", "not_delivered"):
        raise OutboxRefused("only a vendor outcome names a vendor turn or item")
    seq = conn.execute("SELECT COALESCE(MAX(seq), 0) + 1 FROM chat_outbox_events WHERE message_id = ?",
                       (message_id,)).fetchone()[0]
    conn.execute("INSERT INTO chat_outbox_events (message_id, seq, gseq, state, op_id, vendor_anchor, vendor_turn_id, "
                 "vendor_item_id, cause, by, at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                 (message_id, seq, next_gseq(conn, now), state, op_id, vendor_anchor, vendor_turn_id, vendor_item_id,
                  cause, by, now))
    return seq


def _clip(body: str) -> tuple[str, bool]:
    raw = body.encode("utf-8")
    if len(raw) <= EVENT_BODY_MAX:
        return body, False
    return raw[:EVENT_BODY_MAX].decode("utf-8", "ignore"), True


def _event_row(conn: sqlite3.Connection, launch_id: str, key: str) -> ChatEvent | None:
    r = conn.execute("SELECT launch_id, vendor_event_key, gseq, vendor_pos, kind, vendor_turn_id, call_id, body, "
                     "body_sha256, body_norm_sha256, truncated, backfill, at FROM chat_events WHERE launch_id = ? AND "
                     "vendor_event_key = ?", (launch_id, key)).fetchone()
    return None if r is None else ChatEvent(*r[:10], truncated=bool(r[10]), backfill=bool(r[11]), at=r[12])


def record_event(conn: sqlite3.Connection, now: str, *, launch_id: str, key: str, pos: str, kind: str, body: str,
                 normalized: str, turn_id: str | None = None, call_id: str | None = None,
                 backfill: bool = False) -> tuple[ChatEvent, bool]:
    """Record one vendor event in the caller's transaction: ``(event, True)``; a replay of a recorded key with the same
    content writes nothing: ``(stored, False)``; the same key with other content raises :class:`EventConflict`. The
    digests are of the WHOLE body (the reconciliation matches them against a message's); the stored body is clipped
    to :data:`EVENT_BODY_MAX` bytes."""
    if not isinstance(launch_id, str) or _LAUNCH_ID.fullmatch(launch_id) is None or conn.execute(
            "SELECT 1 FROM launches WHERE launch_id = ?", (launch_id,)).fetchone() is None:
        raise CoordError(f"no launch {str(launch_id)[:40]!r}")
    _line("a vendor event key", key)
    _line("a vendor position", pos)
    if kind not in EVENT_KINDS:
        raise CoordError(f"a vendor event's kind is one of {', '.join(EVENT_KINDS)}")
    # rev 3 §16 item 2: a key is "<kind>:<vendor id>", so one vendor item's tool_call, approval_request and
    # tool_result are three rows, never a primary-key collision
    if not key.startswith(kind + ":") or len(key) == len(kind) + 1:
        raise CoordError(f"a vendor event key is {kind}:<vendor id> (kind-qualified), not {key[:60]!r}")
    for name, value in (("a vendor turn id", turn_id), ("a vendor call id", call_id)):
        if value is not None:
            _line(name, value)
    if not isinstance(body, str) or not isinstance(normalized, str):
        raise CoordError("a vendor event's body is text")
    whole = body.encode("utf-8", "replace").decode("utf-8")      # a lone surrogate from a vendor is not kept as one
    sha, norm = body_sha256(whole), body_sha256(normalized.encode("utf-8", "replace").decode("utf-8"))
    kept, truncated = _clip(whole.replace("\x00", "�"))
    stored = _event_row(conn, launch_id, key)
    if stored is not None:
        if (stored.vendor_pos, stored.kind, stored.vendor_turn_id, stored.call_id, stored.body_sha256) != \
                (pos, kind, turn_id, call_id, sha):
            raise EventConflict(f"vendor event {key[:60]!r} of launch {launch_id} is already recorded with other "
                                "content; nothing was written")
        return stored, False
    conn.execute("INSERT INTO chat_events (launch_id, vendor_event_key, gseq, vendor_pos, kind, vendor_turn_id, "
                 "call_id, body, body_sha256, body_norm_sha256, truncated, backfill, source, at) "
                 "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'vendor', ?)",
                 (launch_id, key, next_gseq(conn, now), pos, kind, turn_id, call_id, kept, sha, norm, int(truncated),
                  int(bool(backfill)), now))
    return _event_row(conn, launch_id, key), True


def record_cursor(conn: sqlite3.Connection, now: str, *, launch_id: str, vendor_cursor: str | None,
                  complete_through: str | None, gap: bool) -> int:
    """The driver's cursor after a batch, in the batch's own transaction (§2.4); the row's seq. ``complete_through``
    is the vendor position up to which the driver asserts it read everything: the only basis for an absence."""
    if not isinstance(launch_id, str) or _LAUNCH_ID.fullmatch(launch_id) is None or conn.execute(
            "SELECT 1 FROM launches WHERE launch_id = ?", (launch_id,)).fetchone() is None:
        raise CoordError(f"no launch {str(launch_id)[:40]!r}")
    for name, value in (("a vendor cursor", vendor_cursor), ("a complete-through position", complete_through)):
        if value is not None:
            _line(name, value, 4096)
    seq = conn.execute("SELECT COALESCE(MAX(seq), 0) + 1 FROM chat_cursors WHERE launch_id = ?",
                       (launch_id,)).fetchone()[0]
    conn.execute("INSERT INTO chat_cursors (launch_id, seq, gseq, vendor_cursor, complete_through, gap, at) "
                 "VALUES (?, ?, ?, ?, ?, ?, ?)",
                 (launch_id, seq, next_gseq(conn, now), vendor_cursor, complete_through, int(bool(gap)), now))
    return seq


def timeline(conn: sqlite3.Connection, thread_id: str) -> tuple[list[TimelineRow], list[TimelineRow]]:
    """``(before, merged)`` for a thread: ``merged`` is :data:`CHAT_SCHEMA`'s view in ``gseq`` order; ``before`` holds
    the thread's replies written before replies carried a gseq, ordered by ``at`` among themselves (one store, one
    clock) and never merged into that order (§2.8: no ordering by ``at`` across the merged stream)."""
    if not isinstance(thread_id, str) or _THREAD_ID.fullmatch(thread_id) is None or conn.execute(
            "SELECT 1 FROM threads WHERE thread_id = ?", (thread_id,)).fetchone() is None:
        raise CoordError(f"no thread {str(thread_id)[:40]!r}")
    before = [TimelineRow(None, k, "levain", rid, None, None, body, at) for rid, k, body, at in conn.execute(
        "SELECT reply_id, CASE kind WHEN 'drop_receipt' THEN 'reply_receipt' WHEN 'sign' THEN 'reply_sign' "
        "WHEN 'answer' THEN 'reply_answer' WHEN 'know' THEN 'reply_know' END, body, at FROM replies "
        "WHERE thread_id = ? AND gseq IS NULL ORDER BY at, rowid", (thread_id,))]
    merged = [TimelineRow(*r) for r in conn.execute(
        "SELECT gseq, kind, source, ref_id, launch_id, status, body, at FROM chat_timeline WHERE thread_id = ? "
        "ORDER BY gseq", (thread_id,))]
    return before, merged


class ChatStore:
    """The chat tables beside a coordinator store's, in its ``dispatch.db``. ``opener`` is that store (any object
    with its ``_connect()`` and ``_stamp()``, an :class:`~levain.coordinator.envelopes.EnvelopeStore` in use): this
    class adds its tables to the file the opener owns and never opens the file itself, so it holds no writer of the
    opener's own tables. Every write is one ``BEGIN IMMEDIATE`` with the clock read under it."""

    def __init__(self, opener: Any) -> None:
        self._opener = opener
        self._ready = False

    def _connect(self) -> sqlite3.Connection:
        conn = self._opener._connect()
        if not self._ready:
            try:
                conn.execute("BEGIN IMMEDIATE")
                try:
                    for stmt in CHAT_SCHEMA:
                        conn.execute(stmt)
                    conn.execute("COMMIT")
                except BaseException:
                    _rollback(conn)
                    raise
            except BaseException:
                conn.close()
                raise
            self._ready = True
        return conn

    @contextmanager
    def _write(self) -> Iterator[tuple[sqlite3.Connection, str]]:
        with write_transaction(self._connect) as conn:
            yield conn, self._opener._stamp()

    def _read(self, fn: Callable[[sqlite3.Connection], Any]) -> Any:
        with read_transaction(self._connect) as conn:
            return fn(conn)

    # --- the turn lease (chat_lease holds the rules) ------------------------------------------------------------
    def turn(self, launch_id: str) -> TurnLease | None:
        """The launch's live epoch, or None; an epoch found no longer live is ended here (the read commits it)."""
        with self._write() as (conn, now):
            return chat_lease.settle(conn, now, launch_id)

    def turn_view(self, launch_id: str, owner: str) -> TurnView:
        """What ``owner``'s composer shows: yours, free, or taken (read-only, naming the holder)."""
        with self._write() as (conn, now):
            chat_lease.settle(conn, now, launch_id)
            return chat_lease.view(conn, now, launch_id, owner)

    def turn_claim(self, launch_id: str, owner: str) -> int:
        with self._write() as (conn, now):
            return chat_lease.claim(conn, now, launch_id, owner)

    def turn_handoff(self, launch_id: str, epoch: int, owner: str, to_owner: str) -> int:
        with self._write() as (conn, now):
            return chat_lease.handoff(conn, now, launch_id, epoch, owner, to_owner)

    def turn_take(self, launch_id: str, owner: str) -> int:
        with self._write() as (conn, now):
            return chat_lease.take(conn, now, launch_id, owner)

    def turn_beat(self, launch_id: str, epoch: int, owner: str) -> TurnLease:
        with self._write() as (conn, now):
            return chat_lease.beat(conn, now, launch_id, epoch, owner)

    def turn_release(self, launch_id: str, epoch: int, owner: str) -> None:
        with self._write() as (conn, now):
            chat_lease.release(conn, now, launch_id, epoch, owner)

    def turn_outside(self, launch_id: str, owner_kind: str, cause: str) -> int:
        with self._write() as (conn, now):
            return chat_lease.outside(conn, now, launch_id, owner_kind, cause=cause)

    def turn_history(self, launch_id: str) -> Sequence[tuple]:
        return self._read(lambda conn: chat_lease.history(conn, launch_id))

    # --- reading ------------------------------------------------------------------------------------------------
    def timeline(self, thread_id: str) -> tuple[list[TimelineRow], list[TimelineRow]]:
        return self._read(lambda conn: timeline(conn, thread_id))

    def message(self, message_id: str) -> ChatMessage | None:
        return self._read(lambda conn: _message(conn, "message_id", message_id))

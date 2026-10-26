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

CH-D1b (rev 6 §2.7, §4.2, §6, §7.1-§7.4) adds ``chat_launch_checks`` and its latched fold (:func:`liveness`), and
:class:`ChatService`, which walks the send path (preflight + ``pending``, the one attempt, the driver call, its outcome),
the crash rule on restart, the vendor read (events and cursor in one transaction) and the ambiguous reconciliation
over :class:`~levain.coordinator.driver.DriverSession` s that decide nothing, plus ``chat_anomalies``: a proof that
arrived after its send's record was terminal, kept beside the record it contradicts. Its second slice adds
``chat_echoes`` (§2.9) and, in the transaction that records a user item, §7.6's echo search and §5.4's outside
epoch; the timeline's echo join; the cursor read-back (:meth:`ChatService.read_next`); the unfenced
:meth:`ChatService.interrupt` (§9, the session call only); and :func:`_ensure`, the one path that brings a store
made by an earlier release current (head ruling Q-A, 1025+40). Its third slice adds liveness: rev 7's two activation
columns (by :data:`CHAT_ADDED_COLUMNS`, their rule a trigger), the activation writer (:meth:`ChatService.activate`),
the verdict writer over an injected probe with retry-then-latch, ``probing`` at every §4.2 boundary (R9's Claude
send and result line included) and H9's first-send exception, the pump step's order (a pending probe before the next
line; the route seam after the verdict), the bounded vendor read and interrupt, and :func:`composer_state`.
CH-D1b-4 wires Claude's tripwires (§4.4; the rules are :mod:`levain.coordinator.chat_tripwires`) and the named replies
to inbound control requests (§8.1) into the pump, and files §4.3's ``seat`` review item when a ``not_live`` latches.

NOT HERE: CH-D1c's ``chat_approvals`` and ``chat_approval_events`` (the view's seam for them is named in
:data:`CHAT_SCHEMA`, "SEAM CH-D1c"); the vendor probes themselves (CH-D2 / CH-D3); a pump thread (the launcher's loop
over :meth:`ChatService.read_next`).

[seat] numbers rev 2 marks ``[seat]``: :data:`EVENT_BODY_MAX`, and rev 4's §7.6 :data:`ECHO_WINDOW`; §4.2's
:data:`PROBE_TRIES` and :data:`PROBE_GAP`; :data:`ATTACHMENTS_MAX`, :data:`READ_DEADLINE` and
:data:`INTERRUPT_DEADLINE` are lanes'. Stdlib only.
"""
from __future__ import annotations

import functools
import hashlib
import json
import os
import re
import secrets
import sqlite3
import threading
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import asdict, dataclass, is_dataclass
from datetime import timedelta
from typing import Any, NamedTuple

from levain import unshowable
from levain.autonomic.durable_sqlite import read_transaction, write_transaction
from levain.autonomic.durable_sqlite import rollback as _rollback
from levain.cockpit.consent import parse_stamp
from levain.contracts.canon import canonical
from levain.coordinator import chat_lease
from levain.coordinator.chat_lease import TURN_LEASE_SCHEMA, TurnLease, TurnRefused, TurnView
from levain.coordinator.chat_tripwires import (ControlReply, Trip, TripwireContext, TripwireWindow, control_line,
                                               mode_trip, reported_mode_trip)
from levain.coordinator.driver import (CommandResult, InterruptCommand, LivenessEvidence, NotSent, SendCommand,
                                       SendResult, SessionMeta)
from levain.coordinator.store import WORDS_MAX, CoordError, _append_only, _in, next_gseq, schema_present

__all__ = [
    "ATTACHMENTS_MAX", "AUTHOR_KINDS", "AUTHOR_SURFACES", "CHAT_SCHEMA", "COMPOSER_STATES", "EVENT_BODY_MAX",
    "EVENT_KINDS", "INTERRUPT_DEADLINE", "MODES", "OUTBOX_STATES", "PROBE_GAP", "PROBE_TRIES", "READ_DEADLINE",
    "RECIPIENTS", "CHECK_KINDS", "CHECK_SOURCES", "SEND_FAULT_POINTS", "ChatEvent", "ChatMessage", "ChatService",
    "ChatStore", "Composer", "EventConflict", "Liveness", "MessageConflict", "OutboxRefused", "SendReceipt",
    "TimelineRow", "TurnLease", "TurnRefused", "TurnView", "Verdict", "body_sha256", "canary_verdict_id",
    "composer_state", "liveness", "outbox_state", "outbox_step", "record_check", "record_cursor", "record_event",
    "record_message", "timeline",
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
# [seat] §7.6 condition 3: a user item recorded this long after a send's attempt committed is never its echo
ECHO_WINDOW = timedelta(seconds=60)

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

# what a chat_anomalies row records: a proof (the vendor echoing a send's op_id) after the send's record was terminal
ANOMALY_KINDS = ("proof_after_terminal",)
CHECK_KINDS = ("probing", "live", "not_live", "reenabled")
# where a check row came from (§2.7; "tripwire:<name>" too, on not_live rows only)
CHECK_SOURCES = ("activation", "turn_start", "send", "result", "operator")
_LAST_REENABLE = ("COALESCE((SELECT MAX(gseq) FROM chat_launch_checks WHERE launch_id = NEW.launch_id AND "
                  "kind = 'reenabled'), 0)")
# a reenabled row's precondition (§4.2): the launch's latest probing / live / not_live row is live, and a not_live
# after the last reenable latched it
_LATCHED_LIVE = ("(SELECT kind FROM chat_launch_checks WHERE launch_id = NEW.launch_id AND kind IN ('probing', "
                 "'live', 'not_live') ORDER BY seq DESC LIMIT 1) = 'live' AND EXISTS (SELECT 1 FROM "
                 "chat_launch_checks WHERE launch_id = NEW.launch_id AND kind = 'not_live' AND gseq > "
                 f"{_LAST_REENABLE})")

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
    # a vendor outcome names a vendor turn or item; no other row carries an op_id or an anchor
    "CHECK (state <> 'attempted' OR (op_id IS NOT NULL AND vendor_anchor IS NOT NULL)), "
    "CHECK (state = 'attempted' OR (op_id IS NULL AND vendor_anchor IS NULL)), "
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
    f"CHECK (length(CAST(body AS BLOB)) <= {EVENT_BODY_MAX}), "
    # rev 3 §16 item 2 (head ruling on Q1): the key is "<kind>:<vendor id>", its prefix the row's own kind
    "CHECK (substr(vendor_event_key, 1, length(kind) + 1) = kind || ':' "
    "AND length(vendor_event_key) > length(kind) + 1))",
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
    # CH-D1b: vendor evidence that contradicts a send's terminal record. The record is never rewritten; the
    # contradiction is kept beside it
    "CREATE TABLE IF NOT EXISTS chat_anomalies (launch_id TEXT NOT NULL REFERENCES launches(launch_id), "
    "seq INTEGER NOT NULL, gseq INTEGER NOT NULL UNIQUE, message_id TEXT REFERENCES chat_messages(message_id), "
    f"vendor_event_key TEXT NOT NULL, kind TEXT NOT NULL CHECK ({_in('kind', ANOMALY_KINDS)}), "
    "detail TEXT NOT NULL, at TEXT NOT NULL, PRIMARY KEY (launch_id, seq))",
    *_append_only("chat_anomalies", "a chat anomaly"),
    "CREATE TRIGGER IF NOT EXISTS chat_anomalies_next BEFORE INSERT ON chat_anomalies "
    "WHEN NEW.seq != COALESCE((SELECT MAX(seq) FROM chat_anomalies WHERE launch_id = NEW.launch_id), 0) + 1 "
    "OR EXISTS (SELECT 1 FROM chat_anomalies WHERE rowid = NEW.rowid OR gseq = NEW.gseq) "
    "BEGIN SELECT RAISE(ABORT, 'an anomaly is the next seq of its launch'); END",
    # §2.7 (CH-D1b): the liveness state machine's inputs. The service writes probing / live / not_live from its own
    # verdict over raw evidence; only you write reenabled, and only over a latch whose latest probe read live (§4.2)
    "CREATE TABLE IF NOT EXISTS chat_launch_checks (launch_id TEXT NOT NULL REFERENCES launches(launch_id), "
    f"seq INTEGER NOT NULL, gseq INTEGER NOT NULL UNIQUE, kind TEXT NOT NULL CHECK ({_in('kind', CHECK_KINDS)}), "
    f"source TEXT NOT NULL CHECK ({_in('source', CHECK_SOURCES)} OR "
    "(source GLOB 'tripwire:?*' AND kind = 'not_live')), "
    "evidence_sha256 TEXT CHECK (evidence_sha256 IS NULL OR (length(evidence_sha256) = 71 AND "
    "substr(evidence_sha256, 1, 7) = 'sha256:' AND substr(evidence_sha256, 8) NOT GLOB '*[^0-9a-f]*')), "
    "spawn_nonce TEXT, detail TEXT NOT NULL, by TEXT NOT NULL CHECK (by IN ('service', 'operator')), "
    "at TEXT NOT NULL, canary_verdict_id TEXT, containment_digest TEXT, PRIMARY KEY (launch_id, seq), "
    "CHECK ((kind = 'reenabled') = (by = 'operator')), CHECK ((kind = 'reenabled') = (source = 'operator')), "
    # rev 6 §2.7: a nonce only on an activation row, 32 lowercase hex
    "CHECK (spawn_nonce IS NULL OR (source = 'activation' AND length(spawn_nonce) = 32 "
    "AND spawn_nonce NOT GLOB '*[^0-9a-f]*')))",
    *_append_only("chat_launch_checks", "a launch check"),
    "CREATE TRIGGER IF NOT EXISTS chat_launch_checks_next BEFORE INSERT ON chat_launch_checks "
    "WHEN NEW.seq != COALESCE((SELECT MAX(seq) FROM chat_launch_checks WHERE launch_id = NEW.launch_id), 0) + 1 "
    "OR EXISTS (SELECT 1 FROM chat_launch_checks WHERE rowid = NEW.rowid OR gseq = NEW.gseq) "
    "BEGIN SELECT RAISE(ABORT, 'a launch check is the next seq of its launch'); END",
    f"CREATE TRIGGER IF NOT EXISTS chat_launch_checks_reenable BEFORE INSERT ON chat_launch_checks "
    f"WHEN NEW.kind = 'reenabled' AND NOT ({_LATCHED_LIVE}) "
    "BEGIN SELECT RAISE(ABORT, 'a re-enable clears a latch whose latest probe read live'); END",
    # rev 7 §2.7 (R7-1): the canary verdict id and the containment digest ride together, 64 lowercase hex each, on a
    # live activation row that names its child, and nowhere else. A trigger, not a CHECK: a released table gains the
    # columns by ALTER (CHAT_ADDED_COLUMNS), which cannot add a CHECK
    "CREATE TRIGGER IF NOT EXISTS chat_launch_checks_canary BEFORE INSERT ON chat_launch_checks "
    "WHEN (NEW.canary_verdict_id IS NULL) != (NEW.containment_digest IS NULL) OR (NEW.canary_verdict_id IS NOT NULL "
    "AND NOT (NEW.source = 'activation' AND NEW.kind = 'live' AND NEW.spawn_nonce IS NOT NULL "
    "AND typeof(NEW.canary_verdict_id) = 'text' AND length(NEW.canary_verdict_id) = 64 "
    "AND NEW.canary_verdict_id NOT GLOB '*[^0-9a-f]*' AND typeof(NEW.containment_digest) = 'text' "
    "AND length(NEW.containment_digest) = 64 AND NEW.containment_digest NOT GLOB '*[^0-9a-f]*')) "
    "BEGIN SELECT RAISE(ABORT, 'a canary verdict id and a containment digest ride together, 64 lowercase hex each, "
    "on a live activation row that names its child'); END",
    # §2.9 (CH-D1b): a user item that is probably our own send. NOT authorship and NOT delivery: nothing reads it to
    # set an outbox state (B12); it answers only whether the item opened an outside epoch (§7.6). One per message
    "CREATE TABLE IF NOT EXISTS chat_echoes (launch_id TEXT NOT NULL REFERENCES launches(launch_id), "
    "vendor_event_key TEXT NOT NULL, message_id TEXT NOT NULL UNIQUE REFERENCES chat_messages(message_id), "
    "gseq INTEGER NOT NULL UNIQUE, basis TEXT NOT NULL, at TEXT NOT NULL, PRIMARY KEY (launch_id, vendor_event_key), "
    "FOREIGN KEY (launch_id, vendor_event_key) REFERENCES chat_events(launch_id, vendor_event_key))",
    *_append_only("chat_echoes", "a chat echo"),
    "CREATE TRIGGER IF NOT EXISTS chat_echoes_no_replace BEFORE INSERT ON chat_echoes WHEN EXISTS (SELECT 1 FROM "
    "chat_echoes WHERE (launch_id = NEW.launch_id AND vendor_event_key = NEW.vendor_event_key) "
    "OR message_id = NEW.message_id OR gseq = NEW.gseq OR rowid = NEW.rowid) "
    "BEGIN SELECT RAISE(ABORT, 'a chat echo is never replaced'); END",
    *TURN_LEASE_SCHEMA,
    # §2.8: one thread in the store-wide order, over dispatch.db rows only. A message carries its latest outbox state
    # as a status, never as rows of its own; a vendor event joins the thread its worker launch holds, and a user item
    # with a chat_echoes row is "the vendor's copy of" the message echo_of names, never an outside line; a reply joins
    # once it has a gseq (the old ones are timeline()'s "before the chat record" block); an envelope joins by its
    # bind and its attenuations (one naming no thread narrows every thread the envelope is bound to). A store holding
    # an older text of this view is brought current by _ensure (ChatStore._connect runs it).
    # SEAM CH-D1c: chat_approvals + chat_approval_events join this view when they are built (§2.5-§2.7)
    "CREATE VIEW IF NOT EXISTS chat_timeline (thread_id, gseq, kind, source, ref_id, launch_id, status, body, at, "
    "echo_of) AS "
    "SELECT m.thread_id, m.gseq, 'message', m.author_kind, m.message_id, m.launch_id, "
    "(SELECT o.state FROM chat_outbox_events o WHERE o.message_id = m.message_id ORDER BY o.seq DESC LIMIT 1), "
    "m.body, m.at, NULL FROM chat_messages m "
    "UNION ALL SELECT j.value, e.gseq, CASE WHEN x.message_id IS NULL THEN 'vendor_' || e.kind ELSE 'vendor_echo' "
    "END, e.source, e.vendor_event_key, e.launch_id, NULL, e.body, e.at, x.message_id "
    "FROM chat_events e JOIN launches l ON l.launch_id = e.launch_id JOIN json_each(l.threads) j "
    "LEFT JOIN chat_echoes x ON x.launch_id = e.launch_id AND x.vendor_event_key = e.vendor_event_key "
    "UNION ALL SELECT r.thread_id, r.gseq, CASE r.kind WHEN 'drop_receipt' THEN 'reply_receipt' "
    "WHEN 'sign' THEN 'reply_sign' WHEN 'answer' THEN 'reply_answer' WHEN 'know' THEN 'reply_know' END, "
    "'levain', r.reply_id, NULL, NULL, r.body, r.at, NULL FROM replies r "
    "WHERE r.gseq IS NOT NULL AND r.thread_id IS NOT NULL "
    "UNION ALL SELECT b.thread_id, b.gseq, 'envelope_bound', 'levain', b.envelope_id, NULL, NULL, NULL, b.at, NULL "
    "FROM envelope_binds b "
    "UNION ALL SELECT a.thread_id, a.gseq, 'attenuation_' || a.kind, a.by, a.envelope_id, NULL, NULL, a.value, a.at, "
    "NULL FROM envelope_attenuations a WHERE a.thread_id IS NOT NULL "
    "UNION ALL SELECT b.thread_id, a.gseq, 'attenuation_' || a.kind, a.by, a.envelope_id, NULL, NULL, a.value, a.at, "
    "NULL FROM envelope_attenuations a JOIN envelope_binds b ON b.envelope_id = a.envelope_id "
    "WHERE a.thread_id IS NULL",
)
# (table, column, declaration): a column a released table gains, added by _ensure when it is missing. Only
# additive columns (nullable, or NOT NULL with a constant default; SQLite's ALTER TABLE refuses the rest). The table's
# CREATE statement above must carry the same column, so a new store has it from the start
CHAT_ADDED_COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("chat_launch_checks", "canary_verdict_id", "TEXT"),        # rev 7 §2.7 (CH-D1b-3); its rule is a trigger
    ("chat_launch_checks", "containment_digest", "TEXT"),
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
    echo_of: str | None = None    # a vendor_echo row's message: the vendor's copy of it (never authorship, §2.9)


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
    # the chat_messages_worker trigger's own query, so a caller catching CoordError never meets its IntegrityError
    if launch_id is not None and conn.execute(
            "SELECT 1 FROM launches l, json_each(l.threads) j WHERE l.launch_id = ? AND l.role = 'worker' "
            "AND j.value = ?", (launch_id, thread_id)).fetchone() is None:
        raise CoordError(f"launch {launch_id} is not a worker launch of its thread {thread_id}; a chat message to a "
                         "worker names a worker launch of its thread")
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
    elif op_id is not None or vendor_anchor is not None:
        raise OutboxRefused("only an attempt carries an op_id and a vendor anchor (the one attempt's); nothing was "
                            "written")
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


def _worker_launch(conn: sqlite3.Connection, launch_id: object) -> str:
    """``launch_id`` if it is a worker launch, running or ended (an ended one still takes its backfill). A coordinator
    launch holds no thread, so its events would reach no timeline while its cursor moved past them."""
    if not isinstance(launch_id, str) or _LAUNCH_ID.fullmatch(launch_id) is None:
        raise CoordError(f"no launch {str(launch_id)[:40]!r}")
    row = conn.execute("SELECT role FROM launches WHERE launch_id = ?", (launch_id,)).fetchone()
    if row is None:
        raise CoordError(f"no launch {launch_id}")
    if row[0] != "worker":
        raise CoordError(f"launch {launch_id} is the coordinator's; only a worker launch has a vendor thread here")
    return launch_id


def record_event(conn: sqlite3.Connection, now: str, *, launch_id: str, key: str, pos: str, kind: str, body: str,
                 normalized: str, turn_id: str | None = None, call_id: str | None = None,
                 backfill: bool = False) -> tuple[ChatEvent, bool]:
    """Record one vendor event of a worker launch in the caller's transaction: ``(event, True)``; a replay of a
    recorded key with the same content (both digests included) writes nothing: ``(stored, False)``; the same key with
    other content raises :class:`EventConflict`. The digests are of the WHOLE body (the reconciliation matches them
    against a message's); the stored body is clipped to :data:`EVENT_BODY_MAX` bytes."""
    _worker_launch(conn, launch_id)
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
        # the normalized digest too: a driver whose normalization changed mid-turn would otherwise get back a digest
        # its reconciliation does not compute
        if (stored.vendor_pos, stored.kind, stored.vendor_turn_id, stored.call_id, stored.body_sha256,
                stored.body_norm_sha256) != (pos, kind, turn_id, call_id, sha, norm):
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
    is the vendor position up to which the driver asserts it read everything: the only basis for an absence. A worker
    launch's only."""
    _worker_launch(conn, launch_id)
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
    clock) and never merged into that order (§2.8: no ordering by ``at`` across the merged stream). A store whose
    view is an older text (no ``echo_of`` column; a read-only reader brings nothing current) reads with ``echo_of``
    None."""
    if not isinstance(thread_id, str) or _THREAD_ID.fullmatch(thread_id) is None or conn.execute(
            "SELECT 1 FROM threads WHERE thread_id = ?", (thread_id,)).fetchone() is None:
        raise CoordError(f"no thread {str(thread_id)[:40]!r}")
    before = [TimelineRow(None, k, "levain", rid, None, None, body, at) for rid, k, body, at in conn.execute(
        "SELECT reply_id, CASE kind WHEN 'drop_receipt' THEN 'reply_receipt' WHEN 'sign' THEN 'reply_sign' "
        "WHEN 'answer' THEN 'reply_answer' WHEN 'know' THEN 'reply_know' END, body, at FROM replies "
        "WHERE thread_id = ? AND gseq IS NULL ORDER BY at, rowid", (thread_id,))]
    echo = "echo_of" if "echo_of" in {r[1] for r in conn.execute("PRAGMA table_info(chat_timeline)")} else "NULL"
    merged = [TimelineRow(*r) for r in conn.execute(
        f"SELECT gseq, kind, source, ref_id, launch_id, status, body, at, {echo} FROM chat_timeline "
        "WHERE thread_id = ? ORDER BY gseq", (thread_id,))]
    return before, merged


# the sqlite_master types _ensure drops and recreates when their stored text is not CHAT_SCHEMA's (a table is never
# rebuilt: it gains only CHAT_ADDED_COLUMNS)
_REPLACEABLE = ("view", "trigger", "index")


@functools.lru_cache(maxsize=4)
def _object_texts(statements: tuple[str, ...]) -> tuple[tuple[str, str, str], ...]:
    """``(type, name, sql)`` of every view, trigger and index ``statements`` create, as THIS SQLite stores it in
    ``sqlite_master``: read back from a scratch in-memory database that ran every statement (a trigger or an index needs
    its table there), never a hand normalisation of the text (SQLite drops ``IF NOT EXISTS``; a view's or a trigger
    body's other tables need not exist to create it). An automatic index has no text and is not listed."""
    scratch = sqlite3.connect(":memory:")
    try:
        for stmt in statements:
            scratch.execute(stmt)
        return tuple(scratch.execute(
            f"SELECT type, name, sql FROM sqlite_master WHERE type IN ({', '.join(repr(t) for t in _REPLACEABLE)}) "
            "AND sql IS NOT NULL ORDER BY type, name").fetchall())
    finally:
        scratch.close()


def _stale(conn: sqlite3.Connection) -> tuple[list[tuple[str, str]], list[tuple[str, str, str]]]:
    """What a store holds that is not current: ``([(type, name)] of every view, trigger and index whose stored text
    differs from CHAT_SCHEMA's, added columns a present table lacks)``. An object or table the file does not hold yet
    is not stale: its CREATE makes it."""
    stored = {(t, n): sql for t, n, sql in conn.execute("SELECT type, name, sql FROM sqlite_master")}
    objects = [(t, n) for t, n, sql in _object_texts(CHAT_SCHEMA) if (t, n) in stored and stored[(t, n)] != sql]
    columns = []
    for table, column, decl in CHAT_ADDED_COLUMNS:
        have = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        if have and column not in have:
            columns.append((table, column, decl))
    return objects, columns


def _unwritable(conn: sqlite3.Connection) -> bool:
    """Whether this process cannot write the file behind ``conn``'s main database (``os.access``: its mode, or a
    read-only mount)."""
    path = next((f for _seq, name, f in conn.execute("PRAGMA database_list") if name == "main"), "")
    return bool(path) and not os.access(path, os.W_OK)


def _integrity(conn: sqlite3.Connection) -> list[tuple]:
    return conn.execute("PRAGMA integrity_check").fetchall()


def _ensure(conn: sqlite3.Connection) -> None:
    """The ONE ensure-schema path of the chat tables (head ruling Q-A, 1025+40): in one ``BEGIN IMMEDIATE``, every
    missing additive column (:data:`CHAT_ADDED_COLUMNS`) by ``ALTER TABLE ... ADD COLUMN``, ``DROP`` of every view,
    trigger and index whose stored text is not :data:`CHAT_SCHEMA`'s (:func:`_stale`), then every statement of
    :data:`CHAT_SCHEMA` (``IF NOT EXISTS``: new tables and objects, and the dropped ones); when anything changed,
    ``PRAGMA integrity_check`` must answer ``ok`` before the COMMIT. Any raise rolls all of it back. Rows and rowids
    are never rewritten (no table rebuild, no ``writable_schema``)."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        before = conn.execute("SELECT type, name, sql FROM sqlite_master ORDER BY type, name").fetchall()
        objects, columns = _stale(conn)
        for table, column, decl in columns:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
        for kind, name in objects:
            conn.execute(f"DROP {kind.upper()} IF EXISTS {name}")
        for stmt in CHAT_SCHEMA:
            conn.execute(stmt)
        if conn.execute("SELECT type, name, sql FROM sqlite_master ORDER BY type, name").fetchall() != before:
            check = _integrity(conn)
            if check != [("ok",)]:
                raise CoordError(f"bringing the chat tables current left the store failing its integrity check "
                                 f"({str(check[:3])[:200]}); nothing was changed")
        conn.execute("COMMIT")
    except BaseException:
        _rollback(conn)
        raise


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
                _ensure(conn)
            except BaseException:
                conn.close()
                raise
            self._ready = True
        return conn

    def _read_connect(self) -> sqlite3.Connection:
        """A connection for a read that takes no write lock: the opener's (see its ``_read_connect``) once the chat
        tables are in the file and current too; a file missing them, or holding an older view, trigger or index or a
        table short of a column, is brought current by :meth:`_connect`, under the lock. A file this process cannot
        write (a read-only file or mount) is read as it is and never brought current: its reads see the shape it holds
        (:func:`timeline` reads an older view without ``echo_of``), and a table it lacks fails that read."""
        if self._ready:
            return self._connect()
        conn = self._opener._read_connect()
        try:
            current = (schema_present(conn, CHAT_SCHEMA) and _stale(conn) == ([], [])) or _unwritable(conn)
        except BaseException:
            conn.close()
            raise
        if current:
            return conn
        conn.close()
        return self._connect()

    @contextmanager
    def _write(self) -> Iterator[tuple[sqlite3.Connection, str]]:
        with write_transaction(self._connect) as conn:
            yield conn, self._opener._stamp()

    def _read(self, fn: Callable[[sqlite3.Connection], Any]) -> Any:
        with read_transaction(self._read_connect) as conn:
            return fn(conn)

    # --- the turn lease (chat_lease holds the rules) ------------------------------------------------------------
    def turn(self, launch_id: str) -> TurnLease | None:
        """The launch's live epoch, or None; an epoch found no longer live is ended here (the read commits it)."""
        with self._write() as (conn, now):
            return chat_lease.settle(conn, now, launch_id)

    def turn_view(self, launch_id: str, owner: str) -> TurnView:
        """What ``owner``'s composer shows: yours, free, or taken (read-only, naming the holder). Every composer polls
        this, so it reads without the write lock; only an epoch that reads not-live is settled, in a write that
        settles again under the lock (the read may be stale by then)."""
        def read(conn: sqlite3.Connection) -> TurnView | None:
            now = self._opener._stamp()
            held = chat_lease.turn_lease(conn, now, launch_id)
            return None if held is not None and not held.live else chat_lease.view(conn, now, launch_id, owner)

        shown = self._read(read)
        if shown is not None:
            return shown
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


# --- liveness: the §4.2 fold over chat_launch_checks (CH-D1b) ------------------------------------------------------
@dataclass(frozen=True)
class Liveness:
    """The fold of a launch's check rows (§4.2). ``enabled`` is the only thing B2 reads."""
    enabled: bool
    latest: str | None        # the last probing / live / not_live row's kind; None before the first check
    latched: bool             # a not_live after the last reenabled
    why_not: str | None       # None while enabled


def liveness(conn: sqlite3.Connection, launch_id: str) -> Liveness:
    """``latest`` = the last probing | live | not_live row; ``latched`` = a not_live after the last reenabled;
    ``enabled`` = latest is live and nothing latched. A later live never clears a latch: only your reenabled does."""
    rows = conn.execute("SELECT kind, detail FROM chat_launch_checks WHERE launch_id = ? ORDER BY seq",
                        (launch_id,)).fetchall()
    latest = None
    latched = False
    for kind, _detail in rows:
        if kind == "reenabled":
            latched = False
        else:
            latest = kind
            latched = latched or kind == "not_live"
    if latest is None:
        return Liveness(False, None, False, "no check has run")
    if latched:
        return Liveness(False, latest, True, "a check read not live; it stays off until you re-enable it")
    if latest != "live":
        return Liveness(False, latest, False, "a check is running" if latest == "probing" else "not live")
    return Liveness(True, latest, False, None)


def record_check(conn: sqlite3.Connection, now: str, launch_id: str, kind: str, source: str, detail: str, *,
                 by: str = "service", evidence_sha256: str | None = None, spawn_nonce: str | None = None,
                 canary_verdict_id: str | None = None, containment_digest: str | None = None) -> int:
    """One ``chat_launch_checks`` row in the caller's transaction; its seq. The table's CHECKs and triggers hold the
    same rules for any writer: reenabled is yours and needs a latch whose latest probe read live; a nonce rides only an
    activation row; a canary verdict id and a containment digest ride together, only on a live activation row that
    carries a nonce."""
    if (canary_verdict_id is None) != (containment_digest is None) or canary_verdict_id is not None and (
            source != "activation" or kind != "live" or spawn_nonce is None
            or any(not isinstance(v, str) or re.fullmatch(r"[0-9a-f]{64}", v) is None
                   for v in (canary_verdict_id, containment_digest))):
        raise CoordError("a canary verdict id and a containment digest ride together, 64 lowercase hex each, on a live "
                         "activation row that names its child")
    _worker_launch(conn, launch_id)
    if kind not in CHECK_KINDS:
        raise CoordError(f"a launch check is one of {', '.join(CHECK_KINDS)}")
    if not (source in CHECK_SOURCES or (source.startswith("tripwire:") and len(source) > 9 and kind == "not_live")):
        raise CoordError(f"a check's source is one of {', '.join(CHECK_SOURCES)}, or tripwire:<name> on not_live")
    if (kind == "reenabled") != (by == "operator") or (kind == "reenabled") != (source == "operator"):
        raise CoordError("only you re-enable a launch, and a re-enable is all you write here")
    if spawn_nonce is not None and (source != "activation" or re.fullmatch(r"[0-9a-f]{32}", spawn_nonce) is None):
        raise CoordError("a spawn nonce is 32 lowercase hex, on an activation row only")
    if evidence_sha256 is not None and _SHA.fullmatch(evidence_sha256) is None:
        raise CoordError("an evidence digest is sha256:<64 hex>")
    _line("a check's detail", detail, _CAUSE_MAX)
    if kind == "reenabled":
        fold = liveness(conn, launch_id)
        if not fold.latched or fold.latest != "live":
            raise CoordError("a re-enable needs a launch latched off whose latest check read live again; nothing was "
                             "written")
    seq = conn.execute("SELECT COALESCE(MAX(seq), 0) + 1 FROM chat_launch_checks WHERE launch_id = ?",
                       (launch_id,)).fetchone()[0]
    conn.execute("INSERT INTO chat_launch_checks (launch_id, seq, gseq, kind, source, evidence_sha256, spawn_nonce, "
                 "detail, by, at, canary_verdict_id, containment_digest) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                 (launch_id, seq, next_gseq(conn, now), kind, source, evidence_sha256, spawn_nonce, detail, by, now,
                  canary_verdict_id, containment_digest))
    return seq


def _owed_items(conn: sqlite3.Connection, launch_id: str) -> list[tuple[str, dict[str, Any]]]:
    """§4.3: ``(dedupe key, body)`` of each ``seat`` review item the launch's latches owe: a ``not_live`` that
    latched the launch (no other ``not_live`` since the last ``reenabled`` before it) when a ``live`` came before it,
    and a tripwire's with no prior live too (§4.4: it names the source of what it caught). One item per latch cycle
    either way: a ``not_live`` after another in the same cycle files none."""
    rows = conn.execute(
        "SELECT c.seq, c.source, c.detail FROM chat_launch_checks c WHERE c.launch_id = ? AND c.kind = 'not_live' "
        "AND NOT EXISTS (SELECT 1 FROM chat_launch_checks p WHERE p.launch_id = c.launch_id AND p.kind = 'not_live' "
        "AND p.seq < c.seq AND p.seq > COALESCE((SELECT MAX(r.seq) FROM chat_launch_checks r WHERE r.launch_id = "
        "c.launch_id AND r.kind = 'reenabled' AND r.seq < c.seq), 0)) AND (c.source GLOB 'tripwire:?*' OR EXISTS "
        "(SELECT 1 FROM chat_launch_checks l WHERE l.launch_id = c.launch_id AND l.kind = 'live' AND l.seq < c.seq)) "
        "ORDER BY c.seq", (launch_id,)).fetchall()
    return [(f"chat-not-live:{launch_id}:{seq}", {"what": "chat turned off for a worker until you re-enable it",
                                                  "launch": launch_id, "check": seq, "source": source,
                                                  "detail": detail}) for seq, source, detail in rows]


def _trip(conn: sqlite3.Connection, now: str, launch_id: str, trip: Trip) -> None:
    """A tripwire's ``not_live`` (source ``tripwire:<name>``), in the caller's transaction; once per tripwire while
    the launch stays latched (a second catch of the same kind adds nothing your re-enable would not clear)."""
    source = "tripwire:" + trip.name
    since = conn.execute("SELECT COALESCE(MAX(seq), 0) FROM chat_launch_checks WHERE launch_id = ? AND kind = "
                         "'reenabled'", (launch_id,)).fetchone()[0]
    if conn.execute("SELECT 1 FROM chat_launch_checks WHERE launch_id = ? AND kind = 'not_live' AND source = ? AND "
                    "seq > ? LIMIT 1", (launch_id, source, since)).fetchone() is None:
        record_check(conn, now, launch_id, "not_live", source, _cause_text(trip.detail))


# §4.2 [seat]: a probe that cannot run is retried in place this many times, this far apart (seconds), then latches
PROBE_TRIES = 3
PROBE_GAP = 2.0
# [lane d1b3, unmeasured against a real vendor]: how long (seconds) the service waits on a driver's read, evidence read
# or interrupt before it stops waiting (the D1b-2 known edges: a read under the launch mutex, an interrupt pinning
# detach)
READ_DEADLINE = 10.0
INTERRUPT_DEADLINE = 10.0
# the check sources whose probing row the service's own probe answers. An activation's probing is answered only by the
# activation writer (:meth:`ChatService.activate`: Claude's stage 2, H9)
_TURN_SOURCES = ("turn_start", "send", "result")


class Verdict(NamedTuple):
    """What the service's probe concludes from one :class:`~levain.coordinator.driver.LivenessEvidence`. Made by the
    probe the service was given, never by a session; read back by exact type (:meth:`ChatService._verdict`)."""
    live: bool
    detail: str


def canary_verdict_id(verdict: Any) -> str:
    """Rev 7 §2.7 (R7-1): ``sha256`` (hex) of ``json.dumps(dataclasses.asdict(verdict), sort_keys=True,
    separators=(",", ":"))``, over the canary verdict an activation accepted."""
    if not is_dataclass(verdict) or isinstance(verdict, type):
        raise CoordError("a canary verdict is a dataclass instance")
    try:
        text = json.dumps(asdict(verdict), sort_keys=True, separators=(",", ":"))
    except Exception as exc:  # noqa: BLE001 - a canary that cannot be serialised has no id: refused
        raise CoordError(f"a canary verdict that cannot be serialised has no id ({type(exc).__name__})") from None
    return hashlib.sha256(text.encode()).hexdigest()


def _evidence_sha256(evidence: LivenessEvidence) -> str | None:
    """``sha256:<hex>`` of the evidence's canonical JSON; None when it cannot be serialised (any exception): an
    activation refuses such evidence and a probe counts it a failed try."""
    try:
        text = json.dumps({"source": evidence.source, "raw": evidence.raw, "read_at": evidence.read_at},
                          sort_keys=True, separators=(",", ":"), allow_nan=False)
    except Exception:  # noqa: BLE001 - TypeError, ValueError, RecursionError, a raising __getattr__ ...
        return None
    return "sha256:" + hashlib.sha256(text.encode()).hexdigest()


def _check_rows(conn: sqlite3.Connection, launch_id: str) -> list[tuple]:
    """``(seq, gseq, kind, source, evidence_sha256)`` of the launch's probing / live / not_live rows, in order."""
    return conn.execute("SELECT seq, gseq, kind, source, evidence_sha256 FROM chat_launch_checks WHERE launch_id = ? "
                        "AND kind IN ('probing', 'live', 'not_live') ORDER BY seq", (launch_id,)).fetchall()


def _activated(conn: sqlite3.Connection, launch_id: str) -> bool:
    """The launch's latest activation row is live: its child passed activation (Claude: stage 2), so the service's own
    probe judges its boundaries from here on. Before that only the activation writer gives a verdict."""
    row = conn.execute("SELECT kind FROM chat_launch_checks WHERE launch_id = ? AND source = 'activation' ORDER BY "
                       "seq DESC LIMIT 1", (launch_id,)).fetchone()
    return row is not None and row[0] == "live"


def _boundary(conn: sqlite3.Connection, now: str, launch_id: str, source: str) -> None:
    """§4.2: ``probing`` at a boundary, in the caller's transaction (the one that records the boundary). A launch whose
    activation has not passed is probing already, and its boundaries write nothing."""
    if _activated(conn, launch_id):
        record_check(conn, now, launch_id, "probing", source, f"probing at the {source.replace('_', ' ')} boundary")


def _crossing(stored: set[str], seen: Sequence[Any], backfill: Sequence[bool], claude: bool) -> str | None:
    """The source of the first boundary of a batch that crosses one (two new boundaries, or one that is not the
    batch's last event); None when it does not. A boundary is a new (its key not in ``stored``, the launch's recorded
    keys among the batch's), non-backfill ``turn_started`` (any harness) or, on Claude, ``turn_ended`` (R9): the
    events :meth:`ChatService._ingest` writes a ``probing`` row for."""
    at = [(i, "turn_start" if e.kind == "turn_started" else "result") for i, (e, old) in enumerate(zip(seen, backfill))
          if not old and (e.kind == "turn_started" or (claude and e.kind == "turn_ended")) and e.key not in stored]
    if len(at) > 1 or at and at[0][0] != len(seen) - 1:
        return at[0][1]
    return None


def _pending_probe(conn: sqlite3.Connection, launch_id: str) -> tuple[int, str] | None:
    """``(seq, source)`` of the launch's latest check row when it is a boundary's ``probing`` the service's probe
    answers; else None."""
    rows = _check_rows(conn, launch_id)
    if not rows or rows[-1][2] != "probing" or rows[-1][3] not in _TURN_SOURCES or not _activated(conn, launch_id):
        return None
    return rows[-1][0], rows[-1][3]


def _first_send_open(conn: sqlite3.Connection, launch_id: str) -> bool:
    """H9 (Q-R6-7), the one exception to B2's "no send while probing": a Claude launch whose latest check is its
    activation's ``probing`` WITH evidence (stage 1 passed: the initialize response checked; the spawn's own probing
    row carries none), nothing latched, and no send of the launch attempted since that child's FIRST activation row
    (one first send per spawn nonce: a replayed stage 1 opens no second one). Every allow stays deferred: the launch
    is still not ``enabled``."""
    if _launch_state(conn, launch_id)[2] != "claude":
        return False
    rows = _check_rows(conn, launch_id)
    if not rows:
        return False
    seq, _gseq, kind, source, evidence = rows[-1]
    if kind != "probing" or source != "activation" or evidence is None or liveness(conn, launch_id).latched:
        return False
    nonce = conn.execute("SELECT spawn_nonce FROM chat_launch_checks WHERE launch_id = ? AND seq = ?",
                         (launch_id, seq)).fetchone()[0]
    if nonce is None:
        return False
    since = conn.execute("SELECT MIN(gseq) FROM chat_launch_checks WHERE launch_id = ? AND source = 'activation' AND "
                         "spawn_nonce = ?", (launch_id, nonce)).fetchone()[0]
    return conn.execute("SELECT 1 FROM chat_outbox_events o JOIN chat_messages m ON m.message_id = o.message_id "
                        "WHERE m.launch_id = ? AND o.state = 'attempted' AND o.gseq > ? LIMIT 1",
                        (launch_id, since)).fetchone() is None


# --- the composer (§4.3, §5.4, §7.4, H9) ---------------------------------------------------------------------------
# what a composer for one owner shows, in the order composer_state tests them: the launch is not running; no driver
# session is attached; not live (latched, or the latest check read not live: read-only, §4.3); a check is running
# (the composer shows "checking", §4.2 R9); another holder has the turn (read-only, naming it, §5.4); you may write
COMPOSER_STATES = ("ended", "no_session", "not_live", "checking", "taken", "writable")


@dataclass(frozen=True)
class Composer:
    state: str                        # one of COMPOSER_STATES
    why: str | None                   # None when writable
    holder: str | None                # taken: who holds the turn
    reenable: bool                    # not_live with a latch whose latest check read live: your re-enable is offered
    unresolved: tuple[str, ...]       # the launch's ambiguous sends (§7.4: open / mark delivered / not delivered)


def composer_state(conn: sqlite3.Connection, now: str, launch_id: str, owner: str, *, attached: bool) -> Composer:
    """What ``owner``'s composer for a launch shows, derived from rows alone (it writes nothing; ``attached`` is
    whether the service holds a driver session for the launch). A send it shows as writable may still be refused by
    the send's own preflight; this is the display, B2 is :func:`liveness`."""
    owner = chat_lease.check_owner(owner)
    unresolved = tuple(r[0] for r in conn.execute(
        "SELECT m.message_id FROM chat_messages m WHERE m.launch_id = ? AND (SELECT o.state FROM chat_outbox_events o "
        "WHERE o.message_id = m.message_id ORDER BY o.seq DESC LIMIT 1) = 'ambiguous' ORDER BY m.gseq", (launch_id,)))
    _role, state, _harness = _launch_state(conn, launch_id)
    if state != "active":
        return Composer("ended", f"the launch is {state}", None, False, unresolved)
    if not attached:
        return Composer("no_session", "no driver session is attached to this launch", None, False, unresolved)
    fold = liveness(conn, launch_id)
    if fold.latched or fold.latest == "not_live":
        return Composer("not_live", fold.why_not, None, fold.latched and fold.latest == "live", unresolved)
    if not fold.enabled and not _first_send_open(conn, launch_id):
        return Composer("checking", fold.why_not, None, False, unresolved)
    view = chat_lease.view(conn, now, launch_id, owner)
    if view.state == "taken":
        return Composer("taken", f"the turn is held ({view.holder})", view.holder, False, unresolved)
    return Composer("writable", None, None, False, unresolved)


# --- the send path (§6) and ambiguous reconciliation (§7.1-§7.4) (CH-D1b) -----------------------------------------
# where ChatService._fault is called, in order (the crash matrix cuts at each)
SEND_FAULT_POINTS = ("pending_committed", "attempt_committed", "call_returned", "outcome_committed")


@dataclass(frozen=True)
class SendReceipt:
    message_id: str
    outbox_id: str
    state: str                # the message's latest outbox state when send() returned
    cause: str
    created: bool             # False: a resend of a stored outbox_id; nothing was sent again


@dataclass(frozen=True)
class Anomaly:
    """Vendor evidence that contradicts a send's terminal record (a ``chat_anomalies`` row). The record stands as
    written; this is kept beside it."""
    launch_id: str
    seq: int
    message_id: str | None
    vendor_event_key: str
    kind: str
    detail: str
    at: str


def record_anomaly(conn: sqlite3.Connection, now: str, *, launch_id: str, message_id: str | None, key: str,
                   kind: str, detail: str) -> int:
    """One ``chat_anomalies`` row in the caller's transaction; its seq."""
    _worker_launch(conn, launch_id)
    if kind not in ANOMALY_KINDS:
        raise CoordError(f"an anomaly is one of {', '.join(ANOMALY_KINDS)}")
    _line("a vendor event key", key)
    seq = conn.execute("SELECT COALESCE(MAX(seq), 0) + 1 FROM chat_anomalies WHERE launch_id = ?",
                       (launch_id,)).fetchone()[0]
    conn.execute("INSERT INTO chat_anomalies (launch_id, seq, gseq, message_id, vendor_event_key, kind, detail, at) "
                 "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                 (launch_id, seq, next_gseq(conn, now), message_id, key, kind, _cause_text(detail), now))
    return seq


def _cause_text(text: object) -> str:
    """A cause row's text: one line of at most :data:`_CAUSE_MAX` characters, never refused for its shape."""
    flat = " ".join(str(text).split()) or "no reason given"
    flat = "".join(c if c.isprintable() else "?" for c in flat)
    return flat[:_CAUSE_MAX]


def _said(exc: BaseException) -> str:
    """An exception's text, or a stand-in when its ``__str__`` itself raises."""
    try:
        return str(exc)
    except Exception:  # noqa: BLE001 - a driver's exception is untrusted down to its text
        return "(its message could not be read)"


def _latest_row(conn: sqlite3.Connection, message_id: str) -> tuple[str, str] | None:
    return conn.execute("SELECT state, cause FROM chat_outbox_events WHERE message_id = ? ORDER BY seq DESC LIMIT 1",
                        (message_id,)).fetchone()


def _launch_state(conn: sqlite3.Connection, launch_id: str) -> tuple[str | None, str | None, str | None]:
    """``(role, latest launch event kind, harness)`` of a launch, read here."""
    row = conn.execute("SELECT l.role, (SELECT kind FROM launch_events e WHERE e.launch_id = l.launch_id ORDER BY "
                       "seq DESC LIMIT 1), l.harness FROM launches l WHERE l.launch_id = ?", (launch_id,)).fetchone()
    return (None, None, None) if row is None else row


def _anchor(conn: sqlite3.Connection, launch_id: str) -> str | None:
    """The launch's latest persisted ``complete_through``: every vendor event up to it is recorded, so a window that
    starts here misses nothing the vendor showed after the send. :meth:`ChatService.ingest` never persists one that
    moves back, so the latest is also the furthest."""
    row = conn.execute("SELECT complete_through FROM chat_cursors WHERE launch_id = ? AND complete_through IS NOT "
                       "NULL ORDER BY seq DESC LIMIT 1", (launch_id,)).fetchone()
    return None if row is None else row[0]


def _attempt(conn: sqlite3.Connection, message_id: str) -> tuple[str, str] | None:
    """``(op_id, vendor_anchor)`` of the message's one attempt, or None."""
    return conn.execute("SELECT op_id, vendor_anchor FROM chat_outbox_events WHERE message_id = ? AND "
                        "state = 'attempted'", (message_id,)).fetchone()


def _first_through(conn: sqlite3.Connection, launch_id: str) -> str | None:
    """The launch's first persisted ``complete_through``: an event at or before it is backfill."""
    row = conn.execute("SELECT complete_through FROM chat_cursors WHERE launch_id = ? AND complete_through IS NOT "
                       "NULL ORDER BY seq LIMIT 1", (launch_id,)).fetchone()
    return None if row is None else row[0]


def _fingerprint(conn: sqlite3.Connection, launch_id: str) -> tuple[int, int, int]:
    """The launch's latest cursor seq, its attempt count and its event count. Each only grows, so an equal
    fingerprint means no cursor, attempt or vendor event of the launch was written in between."""
    return tuple(conn.execute(
        "SELECT (SELECT COALESCE(MAX(seq), 0) FROM chat_cursors WHERE launch_id = ?), (SELECT COUNT(*) FROM "
        "chat_outbox_events o JOIN chat_messages m ON m.message_id = o.message_id WHERE m.launch_id = ? AND "
        "o.state = 'attempted'), (SELECT COUNT(*) FROM chat_events WHERE launch_id = ?)",
        (launch_id, launch_id, launch_id)).fetchone())


@dataclass(frozen=True)
class _Attempt:
    message_id: str
    op_id: str
    anchor: str
    norm: str                 # the message's body_norm_sha256
    state: str                # the message's latest outbox state
    item: str | None          # the vendor item its accepted row names, if any


@dataclass(frozen=True)
class _Item:
    key: str                  # a recorded, non-backfill user_item's vendor event key
    pos: str
    norm: str

    @property
    def vendor_id(self) -> str:
        return self.key[len("user_item:"):]


@dataclass(frozen=True)
class _LaunchRead:
    """What the reconciliation of one launch reads, in one read transaction; everything it then decides is computed
    from this outside any transaction, because the comparisons are the driver's ``pos_lt``."""
    attempts: tuple[_Attempt, ...]    # in the order the attempts committed (the service's own order, not vendor's)
    items: tuple[_Item, ...]
    through: str | None               # the complete frontier (:func:`_anchor`)
    gaps: tuple[str | None, ...]      # the complete_through of every cursor row that reported a gap
    fingerprint: tuple[int, int, int]


def _launch_read(conn: sqlite3.Connection, launch_id: str) -> _LaunchRead:
    _worker_launch(conn, launch_id)
    attempts = tuple(_Attempt(*r) for r in conn.execute(
        "SELECT o.message_id, o.op_id, o.vendor_anchor, m.body_norm_sha256, (SELECT l.state FROM chat_outbox_events l "
        "WHERE l.message_id = o.message_id ORDER BY l.seq DESC LIMIT 1), (SELECT l.vendor_item_id FROM "
        "chat_outbox_events l WHERE l.message_id = o.message_id AND l.state = 'accepted') FROM chat_outbox_events o "
        "JOIN chat_messages m ON m.message_id = o.message_id WHERE m.launch_id = ? AND o.state = 'attempted' "
        "ORDER BY o.gseq", (launch_id,)))
    items = tuple(_Item(*r) for r in conn.execute(
        "SELECT vendor_event_key, vendor_pos, body_norm_sha256 FROM chat_events WHERE launch_id = ? AND "
        "kind = 'user_item' AND backfill = 0", (launch_id,)))
    gaps = tuple(r[0] for r in conn.execute("SELECT complete_through FROM chat_cursors WHERE launch_id = ? AND "
                                            "gap = 1", (launch_id,)))
    return _LaunchRead(attempts, items, _anchor(conn, launch_id), gaps, _fingerprint(conn, launch_id))


def _next_anchor(read: _LaunchRead, att: _Attempt, lt: Callable[[str, str], bool]) -> str | None:
    """§7.2's window end: the earliest anchor of another attempt strictly after ``att``'s. An EQUAL anchor ends
    nothing: two sends attempted at one frontier cannot be told apart by position, so they share one window."""
    end = None
    for a in read.attempts:
        if a.message_id != att.message_id and lt(att.anchor, a.anchor) and (end is None or lt(a.anchor, end)):
            end = a.anchor
    return end


def _inside(pos: str, anchor: str, end: str | None, lt: Callable[[str, str], bool]) -> bool:
    """``anchor < pos <= end`` (no end: ``anchor < pos``)."""
    return lt(anchor, pos) and (end is None or not lt(end, pos))


def _untaken(read: _LaunchRead, att: _Attempt) -> list[_Item]:
    """The user items no OTHER message's accepted row names."""
    taken = {a.item for a in read.attempts if a.message_id != att.message_id and a.item is not None}
    return [i for i in read.items if i.vendor_id not in taken]


def _window(read: _LaunchRead, att: _Attempt, lt: Callable[[str, str], bool]) -> list[_Item]:
    """§7.2: the user items after ``att``'s anchor, up to and including the next attempt's anchor (an item there was
    read before that attempt), not backfill and not resolved to another message."""
    end = _next_anchor(read, att, lt)
    return [i for i in _untaken(read, att) if _inside(i.pos, att.anchor, end, lt)]


def _absent(read: _LaunchRead, lt: Callable[[str, str], bool], correlated: bool) -> list[str]:
    """§7.3 step 3: the ambiguous sends a complete read shows were NOT delivered. Never with a declared correlation
    field (the proof is awaited instead, step 1); never across a gap reported at or after the anchor; never while any
    item after the anchor carries the send's digest (step 2). Then only an unmatched user item inside the send's
    window AND inside the complete frontier counts, and only one no other send of ours could have caused (no other
    send anchored before it carries its digest). A turn start, or agent output, never counts."""
    if correlated or read.through is None:
        return []
    out = []
    for att in read.attempts:
        if att.state != "ambiguous" or not lt(att.anchor, read.through):
            continue
        if any(g is None or not lt(g, att.anchor) for g in read.gaps):
            continue
        after = [i for i in _untaken(read, att) if lt(att.anchor, i.pos)]
        if any(i.norm == att.norm for i in after):
            continue
        nxt = _next_anchor(read, att, lt)
        end = read.through if nxt is None or lt(read.through, nxt) else nxt
        others = [a for a in read.attempts if a.message_id != att.message_id]
        if any(_inside(i.pos, att.anchor, end, lt) and not any(o.norm == i.norm and lt(o.anchor, i.pos)
                                                               for o in others) for i in after):
            out.append(att.message_id)
    return out


def _last_cursor(conn: sqlite3.Connection, launch_id: str) -> str | None:
    """The vendor cursor of the launch's newest persisted batch, a None one included (None before the first): where a
    resumed ``read`` starts (§2.4). An older cursor is never carried past a batch that returned none."""
    _worker_launch(conn, launch_id)
    row = conn.execute("SELECT vendor_cursor FROM chat_cursors WHERE launch_id = ? ORDER BY seq DESC LIMIT 1",
                       (launch_id,)).fetchone()
    return None if row is None else row[0]


def _sent_turn(conn: sqlite3.Connection, message_id: str, attempt_gseq: int, launch_id: str) -> str | None:
    """§7.6 condition 2: the vendor turn a send went into. Its ``SendResult``'s turn when the vendor named one; else
    (a vendor that gives no turn id, or no reply yet) the turn the service had RECORDED as current when the attempt
    committed: the latest ``turn_started`` recorded before the attempt row with no ``turn_ended`` recorded before it.
    Those gseq comparisons order the service's own observations, never what the vendor saw first (§2.3). None: no
    turn is known, and the send is no candidate."""
    row = conn.execute("SELECT vendor_turn_id FROM chat_outbox_events WHERE message_id = ? AND vendor_turn_id IS NOT "
                       "NULL ORDER BY seq DESC LIMIT 1", (message_id,)).fetchone()
    if row is not None:
        return row[0]
    row = conn.execute("SELECT s.vendor_turn_id FROM chat_events s WHERE s.launch_id = ? AND s.kind = 'turn_started' "
                       "AND s.vendor_turn_id IS NOT NULL AND s.gseq < ? AND NOT EXISTS (SELECT 1 FROM chat_events f "
                       "WHERE f.launch_id = s.launch_id AND f.kind = 'turn_ended' AND f.vendor_turn_id = "
                       "s.vendor_turn_id AND f.gseq < ?) ORDER BY s.gseq DESC LIMIT 1",
                       (launch_id, attempt_gseq, attempt_gseq)).fetchone()
    return None if row is None else row[0]


def _echo_candidate(conn: sqlite3.Connection, now: str, message_id: str, item: ChatEvent) -> bool:
    """§7.6 conditions 1, 2, 4 and the time half of 3 for one send whose window holds ``item`` (the positional half
    is the caller's, from the driver's ``pos_lt``): latest state attempted / accepted / ambiguous and no echo row yet;
    the same vendor turn; the item recorded within :data:`ECHO_WINDOW` of the attempt's commit; equal normalized
    digests. An attempt whose stamp cannot be read is no candidate (never an exception out of the ingest)."""
    row = conn.execute("SELECT m.body_norm_sha256, o.gseq, o.at FROM chat_messages m JOIN chat_outbox_events o ON "
                       "o.message_id = m.message_id AND o.state = 'attempted' WHERE m.message_id = ?",
                       (message_id,)).fetchone()
    if row is None or outbox_state(conn, message_id) not in ("attempted", "accepted", "ambiguous"):
        return False
    norm, attempt_gseq, attempted_at = row
    if conn.execute("SELECT 1 FROM chat_echoes WHERE message_id = ?", (message_id,)).fetchone() is not None:
        return False
    if item.vendor_turn_id is None or _sent_turn(conn, message_id, attempt_gseq, item.launch_id) != item.vendor_turn_id:
        return False
    try:
        elapsed = parse_stamp(now) - parse_stamp(attempted_at)
    except ValueError:                  # parse_stamp's ConsentError is a ValueError
        return False
    if elapsed < timedelta(0) or elapsed > ECHO_WINDOW:      # a clock stepped back is outside the window
        return False
    return item.body_norm_sha256 == norm


def _echo_or_outside(conn: sqlite3.Connection, now: str, item: ChatEvent, window: Sequence[str]) -> None:
    """§7.6 then §5.4 for one just-recorded, non-backfill user item the vendor did not prove ours by op_id, in the
    transaction that recorded it. The send's own accepted ``vendor_item_id`` is ours: nothing. Exactly one candidate
    among the sends whose window holds the item (``window``): a ``chat_echoes`` row and no outside epoch. Zero, or two
    or more: the item opens an outside epoch on a running launch (an ended one has no turn to hold). The echo row is
    all this writes besides the lease: it never touches an outbox state (B12)."""
    launch_id = item.launch_id
    if conn.execute("SELECT 1 FROM chat_outbox_events o JOIN chat_messages m ON m.message_id = o.message_id WHERE "
                    "m.launch_id = ? AND o.state = 'accepted' AND o.vendor_item_id = ?",
                    (launch_id, item.vendor_event_key[len("user_item:"):])).fetchone() is not None:
        return
    found = [m for m in window if _echo_candidate(conn, now, m, item)]
    if len(found) == 1:
        conn.execute("INSERT INTO chat_echoes (launch_id, vendor_event_key, message_id, gseq, basis, at) VALUES "
                     "(?, ?, ?, ?, ?, ?)",
                     (launch_id, item.vendor_event_key, found[0], next_gseq(conn, now),
                      _cause_text(f"same turn {item.vendor_turn_id}; equal normalized digest; sole candidate"), now))
        return
    if _launch_state(conn, launch_id)[1] == "active":
        chat_lease.outside(conn, now, launch_id, "terminal",
                           cause=f"a user item from outside Levain ({len(found)} candidate sends)")


class _Handled(NamedTuple):
    """One control event in the handled-control record: its state and whether it was interrupted."""
    state: str
    interrupted: bool = False


class _Control(NamedTuple):
    """One control line of a batch: its identity (the event's position and vendor id), the trip that asks for the
    turn's interrupt, and the named reply."""
    ident: tuple[str, str]
    stop: Trip | None
    reply: ControlReply | None


@dataclass(frozen=True)
class _Seen:
    """One vendor event of a batch, every field read once, before any transaction (a driver's object is untrusted
    down to its attributes)."""
    key: str
    kind: str
    pos: str
    turn_id: str | None
    call_id: str | None
    body: str
    norm: str
    correlation: object
    vendor_id: str


def _outcome(got: object) -> tuple[str, str, str | None, str | None]:
    """A driver's send result mapped to ``(state, cause, vendor turn, vendor item)``. ``accepted`` needs an
    id-bearing acknowledgement; anything unreadable is ambiguous. Every field is read here, once."""
    if not isinstance(got, SendResult):
        return "ambiguous", "the driver returned no send result", None, None
    outcome, turn, item, detail = got.outcome, got.vendor_turn_id, got.vendor_item_id, got.detail
    if type(outcome) is not str or outcome not in ("accepted", "ambiguous", "not_delivered"):
        return "ambiguous", "the driver returned no send result", None, None
    for value in (turn, item):
        if value is not None and (type(value) is not str or _readable(value) is None):
            return "ambiguous", "the vendor's acknowledgement carried an unreadable id", None, None
    detail = detail if type(detail) is str else ""
    if outcome == "accepted":
        if turn is None and item is None:
            return "ambiguous", "accepted with no vendor id", None, None
        return "accepted", _cause_text(detail or "accepted"), turn, item
    return outcome, _cause_text(detail or outcome), turn, item


def _readable(value: str) -> str | None:
    try:
        return _line("a vendor id", value)
    except CoordError:
        return None


def _caps_snapshot(caps: Any) -> tuple[bool, bool]:
    """``(steer, queue)`` as plain bools: the driver's object is read here, outside every transaction."""
    return bool(caps.steer), bool(caps.queue)


def _plain_norm(value: Any) -> str:
    """A driver's normalized text as an exact ``str`` (a subclass's own methods are driver code, which must not run
    in a transaction); anything that is not a string is refused."""
    if not isinstance(value, str):
        raise CoordError("a driver's normalized form is a string")
    return str.__str__(value)


class _Overdue(Exception):
    """A bounded driver call that had not returned by its deadline (it may still be running)."""


def _bounded(fn: Callable[[], Any], seconds: float) -> Any:
    """``fn()`` on its own daemon thread, waited on for at most ``seconds``: its value, or its exception re-raised
    here, or :class:`_Overdue` when it has not returned (the thread is left to finish; nothing waits on it again)."""
    box: dict[str, Any] = {}
    done = threading.Event()

    def run() -> None:
        try:
            box["value"] = fn()
        except BaseException as exc:  # noqa: BLE001 - carried to the waiting caller
            box["error"] = exc
        finally:
            done.set()

    threading.Thread(target=run, name="levain-chat-driver-call", daemon=True).start()
    if not done.wait(seconds):
        raise _Overdue()
    if "error" in box:
        raise box["error"]
    return box["value"]


class ChatService:
    """Walks the send path over a :class:`ChatStore` and the :class:`~levain.coordinator.driver.DriverSession` s
    attached to it, one per worker launch. Every verdict is the service's, from rows and raw vendor evidence; a
    session decides nothing. ``fault(point)`` is the crash-matrix seam, called at each of
    :data:`SEND_FAULT_POINTS` with a launch's mutex held: a hook must not call back into the service for that launch
    (test-only; the mutex is not re-entrant).

    Each launch has one mutex, the service's: a send holds it from its preflight to its outcome (so two sends never
    meet inside one session), a read holds it from its first read of the store to its last write, and attaching or
    detaching a session takes it. No driver method runs inside a write transaction: the session is called first and
    its answers are carried into short transactions that hold only store work.

    :meth:`interrupt` takes no launch mutex; it PINS the session it calls instead (``_pins``), and :meth:`detach`
    refuses new pins (``_detaching``) and waits for the pinned interrupts to return before it closes the session, so
    an interrupt never reaches a session that was closed or replaced under it. Both waits are bounded: an interrupt
    that has not answered within :data:`INTERRUPT_DEADLINE` is ``ambiguous`` to its caller, and detach waits at most
    that long before it closes the session under it (closing is the stop that is always available, §4.3).

    A driver call that has not returned by its deadline (a read, an evidence read, an interrupt) WEDGES the session:
    the call is left in flight and nothing else is called on that session until it is detached (a new attach starts
    unwedged).

    Liveness (§4.1-§4.2): ``probe(launch_id, evidence)`` is the service's verdict function (the launcher's; CH-D2 /
    CH-D3 give the vendor ones) and returns a :class:`Verdict`. A session supplies only
    :class:`~levain.coordinator.driver.LivenessEvidence`; nothing it returns is read as a verdict. With no probe, every
    probe fails and latches. ``route(launch_id, keys)`` is the seam a later slice routes vendor requests through: the
    keys of the events one read recorded, called after that read's verdict with the launch's mutex held (it must not
    call back into the service for that launch: the mutex is not re-entrant). ``sleep`` is the retry wait.

    Tripwires (§4.4, Claude launches only): ``tripwires(launch_id)`` gives the launch's
    :class:`~levain.coordinator.chat_tripwires.TripwireContext` (the launcher's); None, an exception or anything else
    leaves the launch with none, and its first ``tool_result`` trips. Each attached session gets a fresh
    :class:`~levain.coordinator.chat_tripwires.TripwireWindow`; every pump step feeds it the events it will record new,
    BEFORE the transaction that records them (no context callback and no path resolve runs inside a write
    transaction), and that transaction writes each trip as ``not_live`` (source ``tripwire:<name>``) at its event.
    Each inbound non-approval control request gets its named error reply (§8.1), and a trip that asks for it sends the
    turn's interrupt, also before that transaction: a process that dies before the commit leaves the cursor where it
    was, so the next service reads the request again and answers it; one that dies after a reply and before the commit
    answers it again, and a second reply the session does not confirm latches ``tripwire:unanswerable_control``.

    §4.3's review item: ``seat_item(dedupe_key, body)`` is the launcher's filer of a ``seat`` review item, which must
    file at most once per dedupe key (this module never imports the review queue). After any write that can latch a
    launch, the service files each item its latches owe (:func:`_owed_items`) that this process has not filed yet.
    The filer runs after the latch commits, never inside it, so a filer that fails costs nothing but the item, and the
    next sweep files it again. It may run with the launch's mutex held (a pump step), so like ``route`` it must not
    call back into the service for that launch."""

    def __init__(self, store: ChatStore, *, fault: Callable[[str], None] | None = None,
                 probe: Callable[[str, LivenessEvidence], Verdict] | None = None,
                 route: Callable[[str, tuple[str, ...]], None] | None = None,
                 sleep: Callable[[float], None] = time.sleep,
                 tripwires: Callable[[str], TripwireContext | None] | None = None,
                 seat_item: Callable[[str, dict[str, Any]], Any] | None = None) -> None:
        self._chat = store
        self._sessions: dict[str, Any] = {}
        self._locks: dict[str, threading.Lock] = {}
        self._pins: dict[int, int] = {}               # id(session) -> interrupts in flight on it
        self._detaching: set[str] = set()             # launches whose session takes no new pin
        self._wedged: set[str] = set()                # launches whose session left a read unanswered: used no more
        self._guard = threading.Lock()                 # held only to read or change the dicts and the sets
        self._unpinned = threading.Condition(self._guard)
        self._fault_hook = fault
        self._probe = probe
        self._route = route
        self._sleep = sleep
        self._tripwires = tripwires
        self._windows: dict[str, TripwireWindow] = {}   # launch -> its attached session's tripwire window
        self._seat_item = seat_item
        self._filed: dict[str, set[str]] = {}            # launch -> the seat items' dedupe keys filed while attached
        # launch -> the control events this session was sent something for, by the event's identity (its stream
        # position and vendor id, which carries the request id) -> two facts. Its state: "done" (its reply was
        # confirmed, or its interrupt confirmed or answered no_such_request: nothing is sent for it again), "open"
        # (tried, not done: tried again) or "stale" (open when the launch was activated or re-enabled: its reply is
        # tried again, an interrupt naming no turn is not, since it would reach a turn started after that). And
        # whether it was interrupted (an interrupt sent for it was confirmed or answered no_such_request): that
        # interrupt is never sent again, only its reply is retried. A committed batch's events are dropped from it
        self._handled: dict[str, dict[tuple[str, str], _Handled]] = {}

    def _launch_lock(self, launch_id: str) -> threading.Lock:
        with self._guard:
            return self._locks.setdefault(launch_id, threading.Lock())

    def _session(self, launch_id: str) -> Any:
        with self._guard:
            return self._sessions.get(launch_id)

    def attach(self, launch_id: str, session: Any) -> None:
        """The launch's one session. Refused while another is attached: :meth:`detach` it first. The refusal does
        not wait for a send in flight (a session in use is an attached one)."""
        with self._guard:
            self._refuse_second(launch_id)
        with self._launch_lock(launch_id), self._guard:
            self._refuse_second(launch_id)
            self._sessions[launch_id] = session
            self._wedged.discard(launch_id)           # a wedge belongs to the session that left a call unanswered
            self._windows.pop(launch_id, None)        # so does a tripwire window: a new child is watched from zero
            self._handled.pop(launch_id, None)

    def _wedge(self, launch_id: str, session: Any) -> None:
        """``session`` left a driver call unanswered: the launch's session is used no more until it is detached.
        Only the session still attached is wedged (a call that went overdue after a detach marks nothing)."""
        with self._guard:
            if self._sessions.get(launch_id) is session:
                self._wedged.add(launch_id)

    def _refuse_wedged(self, launch_id: str, done: str) -> None:
        """Refuse (CoordError) once the launch's session is wedged; ``done`` says what was already written."""
        with self._guard:
            wedged = launch_id in self._wedged
        if wedged:
            raise CoordError(f"launch {str(launch_id)[:40]}'s driver session stopped answering; detach it; {done}")

    def _refuse_second(self, launch_id: str) -> None:
        if launch_id in self._sessions:
            raise CoordError(f"launch {str(launch_id)[:40]} already has a driver session; detach it first")

    def detach(self, launch_id: str) -> None:
        """Forget the launch's session and close it, once any send or read of the launch in flight has finished and
        every interrupt already pinned to it has returned; an interrupt arriving meanwhile is refused."""
        if self._session(launch_id) is None:
            return
        with self._launch_lock(launch_id):
            with self._guard:
                self._detaching.add(launch_id)
                try:
                    held = self._sessions.get(launch_id)
                    self._unpinned.wait_for(lambda: not self._pins.get(id(held)), timeout=INTERRUPT_DEADLINE)
                    session = self._sessions.pop(launch_id, None)
                    self._wedged.discard(launch_id)
                    self._windows.pop(launch_id, None)
                    self._handled.pop(launch_id, None)
                    # the filer files once per key (its contract): a later attach asks it again
                    self._filed.pop(launch_id, None)
                finally:
                    self._detaching.discard(launch_id)
            if session is not None:
                session.close()

    def close(self) -> None:
        """Detach every session. Each is closed even when an earlier close raised; the first error is raised last."""
        with self._guard:
            launches = list(self._sessions)
        first: Exception | None = None
        for launch_id in launches:
            try:
                self.detach(launch_id)
            except Exception as exc:  # noqa: BLE001 - every session is closed before any error propagates
                first = first or exc
        if first is not None:
            raise first

    def _fault(self, point: str) -> None:
        if self._fault_hook is not None:
            self._fault_hook(point)

    # --- the send: preflight + pending, attempt, call, outcome --------------------------------------------------
    def send(self, launch_id: str, *, author_kind: str, author_surface: str, client_id: str, outbox_id: str,
             body: str, mode: str = "queue", expected_turn_id: str | None = None,
             expected_epoch: int | None = None, attachments: Sequence[dict[str, Any]] = ()) -> SendReceipt:
        """One message to a running worker, sent at most once. ``author_surface:client_id`` is the AUTHENTICATED
        owner (the caller derives it from the client's session, never from a request body); an operator's send is
        fenced under ``expected_epoch``. A resend of a stored ``outbox_id`` sends nothing again."""
        if not isinstance(launch_id, str) or _LAUNCH_ID.fullmatch(launch_id) is None:
            raise CoordError(f"no worker launch {str(launch_id)[:40]!r}")
        owner = f"{author_surface}:{client_id}"
        # a launch that does not exist gets no lock: the registry holds attached and real launches only
        self._chat._read(lambda conn: self._need_worker(conn, launch_id))
        with self._launch_lock(launch_id):
            session = self._session(launch_id)
            caps = normalized = None
            # a resend of a stored outbox_id answers from the store alone: no driver method runs for it
            stored = self._chat._read(lambda conn: _message(conn, "outbox_id", outbox_id)) \
                if isinstance(outbox_id, str) else None
            if launch_id in self._wedged:
                session = None
            if session is not None and stored is None:
                caps = _caps_snapshot(session.capabilities())
                normalized = _plain_norm(session.normalize(body)) if isinstance(body, str) else body
            with self._chat._write() as (conn, now):
                self._need_worker(conn, launch_id)
                row = conn.execute("SELECT j.value FROM launches l, json_each(l.threads) j WHERE l.launch_id = ? "
                                   "LIMIT 1", (launch_id,)).fetchone()
                if row is None:
                    raise CoordError(f"worker launch {launch_id} holds no thread; nothing was written")
                m, created = record_message(
                    conn, now, thread_id=row[0], recipient="worker", author_kind=author_kind,
                    author_surface=author_surface, client_id=client_id, outbox_id=outbox_id, body=body,
                    normalized=body if normalized is None else normalized, launch_id=launch_id, mode=mode,
                    expected_turn_id=expected_turn_id,
                    owner_epoch=expected_epoch if author_kind == "operator" else None, attachments=attachments)
                if not created:
                    latest = _latest_row(conn, m.message_id)
                    return SendReceipt(m.message_id, outbox_id, latest[0] if latest else "pending",
                                       latest[1] if latest else "", False)
                why = self._preflight(conn, now, m, caps, owner)
                if why and launch_id in self._wedged:
                    why = "the launch's driver session stopped answering; it is detached and replaced before any send"
                outbox_step(conn, now, m.message_id, "refused" if why else "pending", cause=why or "preflight passed")
            if why:
                return SendReceipt(m.message_id, outbox_id, "refused", why, True)
            self._fault("pending_committed")
            return self._attempt_and_call(m, session, caps, owner)

    @staticmethod
    def _need_worker(conn: sqlite3.Connection, launch_id: str) -> None:
        role, _state, _h = _launch_state(conn, launch_id)
        if role != "worker":
            raise CoordError(f"no worker launch {launch_id!r}")

    def _preflight(self, conn: sqlite3.Connection, now: str, m: ChatMessage, caps: Any, owner: str) -> str | None:
        """B1 launch state, B2 the §4.2 fold, B3 the fence (yours only; the coordinator queues unfenced, B11), B9 a
        steer names the vendor's open turn. The first no, as a cause; None when all pass. ``caps`` is
        ``(steer, queue)`` as plain bools, copied from the attached session's capabilities before the transaction
        (None: no session)."""
        _role, state, _h = _launch_state(conn, m.launch_id)
        if state != "active":
            return _cause_text(f"launch {m.launch_id} is {state}; only a running worker takes a message")
        if caps is None:
            return "no driver session is attached to this launch"
        fold = liveness(conn, m.launch_id)
        if not fold.enabled and not _first_send_open(conn, m.launch_id):
            return _cause_text(f"the gate is not live: {fold.why_not}")
        if m.author_kind == "operator":
            try:
                chat_lease.turn_fence(conn, now, m.launch_id, m.owner_epoch, owner)
            except TurnRefused as exc:
                return _cause_text(exc)
        if m.mode == "steer":
            if not caps[0]:
                return "this vendor takes no steer; queue the message instead"
            if not self._turn_open(conn, m.launch_id, m.expected_turn_id):
                return _cause_text(f"turn {m.expected_turn_id} is not running; a steer lands only in the turn it "
                                   "names")
        elif not caps[1]:
            return "this vendor takes no queued message"
        return None

    @staticmethod
    def _turn_open(conn: sqlite3.Connection, launch_id: str, turn_id: str | None) -> bool:
        """The vendor showed ``turn_id`` start and has not shown it end."""
        kinds = {k for (k,) in conn.execute("SELECT kind FROM chat_events WHERE launch_id = ? AND vendor_turn_id = ? "
                                            "AND kind IN ('turn_started', 'turn_ended')", (launch_id, turn_id))}
        return kinds == {"turn_started"}

    def _attempt_and_call(self, m: ChatMessage, session: Any, caps: Any, owner: str) -> SendReceipt:
        op_id = "op-" + secrets.token_hex(16)
        with self._chat._write() as (conn, now):
            latest = outbox_state(conn, m.message_id)
            if latest != "pending":
                row = _latest_row(conn, m.message_id)
                return SendReceipt(m.message_id, m.outbox_id, row[0], row[1], True)
            why = self._preflight(conn, now, m, caps, owner)
            anchor = None if why else _anchor(conn, m.launch_id)
            if why is None and anchor is None:
                why = "no vendor position yet"
            if why:
                outbox_step(conn, now, m.message_id, "cancelled", cause=why)
                return SendReceipt(m.message_id, m.outbox_id, "cancelled", why, True)
            # R9: on Claude each send is a boundary, its probing in this transaction, before the bytes; the first send
            # under activation's probing (H9) writes none (stage 2 gives that launch its verdict)
            first = _first_send_open(conn, m.launch_id)
            outbox_step(conn, now, m.message_id, "attempted", cause="attempt", op_id=op_id, vendor_anchor=anchor)
            claude = _launch_state(conn, m.launch_id)[2] == "claude"
            if not first and claude:
                _boundary(conn, now, m.launch_id, "send")
        if claude:
            # R9: a send is a boundary for the tripwire counts too, once its attempt committed (the launch's mutex,
            # held here, is the one the pump updates the window under); they reset only with no call open
            with self._guard:
                window = self._windows.get(m.launch_id)
            if window is not None:
                window.boundary()
        # only the transaction whose attempted row committed reaches this call (the partial unique index)
        self._fault("attempt_committed")
        state, cause, turn, item, signal = self._call(session, _command(m, op_id))
        if signal is None:
            self._fault("call_returned")
        with self._chat._write() as (conn, now):
            if outbox_state(conn, m.message_id) == "attempted":
                outbox_step(conn, now, m.message_id, state, cause=cause, vendor_turn_id=turn, vendor_item_id=item)
            row = _latest_row(conn, m.message_id)
        if signal is not None:
            raise signal
        self._fault("outcome_committed")
        self._probe_pending(m.launch_id, session)
        return SendReceipt(m.message_id, m.outbox_id, row[0], row[1], True)

    @staticmethod
    def _call(session: Any, command: Any) -> tuple[str, str, str | None, str | None, BaseException | None]:
        """The one driver call, mapped to ``(state, cause, vendor turn, vendor item, signal)``. ``NotSent`` (proof
        no byte left) is not delivered; any other exception, and any result that cannot be read, is ambiguous. A
        ``BaseException`` that is not an ``Exception`` (an interrupt, an exit, a cancellation) comes back as
        ``signal``: the caller records the outcome first, then raises it."""
        try:
            got = session.send(command)
        except NotSent as exc:
            return "not_delivered", _cause_text(f"not sent: {_said(exc)}"), None, None, None
        except Exception as exc:  # noqa: BLE001 - any driver exception is an unknown outcome, never a lost send
            return "ambiguous", _cause_text(f"the driver raised {type(exc).__name__}: {_said(exc)}"), None, None, None
        except BaseException as exc:  # noqa: BLE001 - recorded, then re-raised by the caller
            return "ambiguous", _cause_text(f"the driver call was interrupted ({type(exc).__name__})"), None, None, exc
        try:
            return (*_outcome(got), None)
        except Exception as exc:  # noqa: BLE001 - a result object that raises when read is an unknown outcome
            return "ambiguous", _cause_text(f"the driver's result could not be read ({type(exc).__name__})"), None, \
                None, None
        except BaseException as exc:  # noqa: BLE001 - recorded, then re-raised by the caller
            return "ambiguous", _cause_text(f"reading the driver's result was interrupted ({type(exc).__name__})"), \
                None, None, exc

    # --- the crash rule (§6 step 4) ------------------------------------------------------------------------------
    def recover(self) -> dict[str, int]:
        """On service start: a message whose latest row is ``attempted`` becomes ``ambiguous`` (its bytes may have
        left); one still ``pending`` becomes ``not_delivered`` (never attempted, and never sent later). Run before
        this service sends anything; a second live service on the same file is not a supported shape."""
        done = {"ambiguous": 0, "not_delivered": 0}
        with self._chat._write() as (conn, now):
            rows = conn.execute("SELECT o.message_id, o.state FROM chat_outbox_events o WHERE o.seq = (SELECT "
                                "MAX(seq) FROM chat_outbox_events WHERE message_id = o.message_id) AND o.state IN "
                                "('attempted', 'pending') ORDER BY o.gseq").fetchall()
            for message_id, state in rows:
                if state == "attempted":
                    outbox_step(conn, now, message_id, "ambiguous", cause="service restarted after the attempt")
                    done["ambiguous"] += 1
                else:
                    outbox_step(conn, now, message_id, "not_delivered", cause="never attempted")
                    done["not_delivered"] += 1
        return done

    # --- reading the vendor: events + cursor in one transaction, then reconciliation ---------------------------
    def ingest(self, launch_id: str, batch: Any) -> int:
        """Record one :class:`~levain.coordinator.driver.ReadBatch` of a launch and its cursor in ONE transaction
        (§2.4), resolving any send the batch proves, then re-check every ambiguous send of the launch (§7.3). An
        event at or before the launch's first persisted ``complete_through`` is backfill; before any is persisted, one
        at or before this batch's own ``complete_through``; with neither, no event is. A ``complete_through`` before the
        one already persisted is not persisted: the frontier never moves back. The count of new events."""
        with self._attached(launch_id) as session:
            return self._ingest(launch_id, session, batch)

    def read_next(self, launch_id: str) -> int:
        """One pump step, the cursor read-back: a pending boundary probe first (§4.2: before the vendor's next line
        is handled), then ``session.read(<the launch's last persisted vendor cursor>)`` (None before the first),
        bounded by :data:`READ_DEADLINE`, then :meth:`ingest` of what it returned, all under the launch's mutex. A read
        that has not returned by the deadline records nothing and leaves the session unused until it is detached. A
        restarted service resumes where the last persisted batch ended. The count of new events."""
        with self._attached(launch_id) as session:
            self._probe_pending(launch_id, session)
            self._refuse_wedged(launch_id, "nothing was read")
            cursor = self._chat._read(lambda conn: _last_cursor(conn, launch_id))
            try:
                batch = _bounded(lambda: session.read(cursor), READ_DEADLINE)
            except _Overdue:
                self._wedge(launch_id, session)
                raise CoordError(f"launch {launch_id}'s vendor read did not return within {READ_DEADLINE:g} s; its "
                                 "session is used no more until it is detached; nothing was recorded") from None
            return self._ingest(launch_id, session, batch)

    @contextmanager
    def _attached(self, launch_id: str) -> Iterator[Any]:
        """The launch's mutex held, with its attached session; refused with nothing read when there is none."""
        refusal = f"no driver session is attached to launch {str(launch_id)[:40]}; nothing was read"
        if self._session(launch_id) is None:
            raise CoordError(refusal)
        with self._launch_lock(launch_id):
            session = self._session(launch_id)
            if session is None:
                raise CoordError(refusal)
            self._refuse_wedged(launch_id, "nothing was read")
            yield session

    def _ingest(self, launch_id: str, session: Any, batch: Any) -> int:
        """:meth:`ingest` with the launch's mutex held. Every driver call (``normalize``, ``pos_lt``) runs before the
        write transaction; the positions they place are immutable, and the fingerprint check refuses the write if a
        cursor, an attempt or an event of the launch was written since they were read."""
        self._probe_pending(launch_id, session)
        self._refuse_wedged(launch_id, "nothing was recorded")
        correlated = bool(session.capabilities().correlation_field)
        raws = tuple(batch.events)
        seen = []
        for e in raws:
            body = e.body
            seen.append(_Seen(e.key, e.kind, e.pos, e.turn_id, e.call_id, body,
                              _plain_norm(session.normalize(body)) if isinstance(body, str) else body,
                              e.correlation, e.vendor_id))
        through, cursor, gap = batch.complete_through, batch.next_cursor, bool(batch.gap)

        def frontier(conn: sqlite3.Connection) -> tuple[str | None, _LaunchRead, bool, set[str]]:
            return _first_through(conn, launch_id), _launch_read(conn, launch_id), \
                _launch_state(conn, launch_id)[2] == "claude", \
                {e.key for e in seen if _event_row(conn, launch_id, e.key) is not None}

        first, read, claude, stored = self._chat._read(frontier)
        prev, lt = read.through, session.pos_lt
        baseline = first if first is not None else through
        backfill = [baseline is not None and not lt(baseline, e.pos) for e in seen]
        # §7.6 condition 3's positional half: the attempts whose window holds each item (the rest is re-read below)
        windows = [tuple(a.message_id for a in read.attempts if _inside(e.pos, a.anchor, _next_anchor(read, a, lt), lt))
                   if e.kind == "user_item" and not old else () for e, old in zip(seen, backfill)]
        if through is not None and prev is not None and lt(through, prev):
            through, gap = prev, False      # an older batch: the persisted frontier already covers its gap
        # what the write below records new: a key not stored and not earlier in the batch (the fingerprint check
        # refuses the write if any event of the launch was written since this read, and each row is checked again)
        fresh, keys = [], set()
        for e in seen:
            fresh.append(e.key not in stored and e.key not in keys)
            keys.add(e.key)
        crossed = _crossing(stored, seen, backfill, claude)
        window, early, at, controls = self._watch(launch_id, session, claude, raws, seen, backfill, fresh, crossed,
                                                  gap)
        if controls:
            # answer-then-commit: every reply and the interrupt are written before the batch and its cursor commit
            failed = self._answer_controls(launch_id, session, controls)
            if failed:
                early.append(Trip("unanswerable_control", "a control reply could not be written: " + "; ".join(failed)))
            self._fault("controls_answered")
        new = 0
        recorded: list[str] = []
        with self._chat._write() as (conn, now):
            if _fingerprint(conn, launch_id) != read.fingerprint:
                raise CoordError(f"launch {launch_id}'s chat record changed while its batch was read; nothing "
                                 "was written")
            if crossed is not None:
                # the driver contract: a read ends at its one boundary. Fail closed: nothing of the batch is
                # recorded, the cursor stays, and the launch latches off
                record_check(conn, now, launch_id, "not_live", crossed, "batch crossed a boundary")
                seen = []
            for trip in early:                        # the batch's own trips, before any of its rows
                _trip(conn, now, launch_id, trip)
            for i, (e, old, window_keys) in enumerate(zip(seen, backfill, windows)):
                ev, created = record_event(conn, now, launch_id=launch_id, key=e.key, pos=e.pos, kind=e.kind,
                                           body=e.body, normalized=e.norm, turn_id=e.turn_id, call_id=e.call_id,
                                           backfill=old)
                if created != fresh[i]:
                    raise CoordError(f"launch {launch_id}'s chat record changed while its batch was read; nothing "
                                     "was written")
                new += created
                if created and not old:
                    recorded.append(e.key)
                # each trip is written at its event, so a boundary after it still gets its own probing and verdict
                for trip in at[i]:
                    _trip(conn, now, launch_id, trip)
                # §4.2: probing commits with the boundary that starts a turn (Codex turn/started; any harness's
                # turn_started) and, on Claude, with the turn_ended a result line maps to (R9). History is no boundary
                if created and not old and (e.kind == "turn_started" or (claude and e.kind == "turn_ended")):
                    _boundary(conn, now, launch_id, "turn_start" if e.kind == "turn_started" else "result")
                if not created or e.kind != "user_item" or old:
                    continue
                if correlated and isinstance(e.correlation, str) and self._prove(conn, now, launch_id, e):
                    continue
                _echo_or_outside(conn, now, ev, window_keys)
            if crossed is None:
                record_cursor(conn, now, launch_id=launch_id, vendor_cursor=cursor, complete_through=through,
                              gap=gap)
        # the window and the reply record first: nothing after the commit can lose them
        with self._guard:
            if window is not None and crossed is None and self._sessions.get(launch_id) is session:
                self._windows[launch_id] = window
            if crossed is None:                       # a crossing batch stays unrecorded: its re-read sends nothing new
                for ident, _stop, _reply in controls:
                    self._handled.get(launch_id, {}).pop(ident, None)
        self._file_latches(launch_id)
        if crossed is not None:
            raise CoordError(f"launch {launch_id}'s vendor read crossed a boundary (a boundary event that is not its "
                             "batch's last); nothing was recorded and the launch is off until you re-enable it")
        # the boundary (the batch's last event) is judged before anything handles what follows it (§4.2)
        self._probe_pending(launch_id, session)
        self._refuse_wedged(launch_id, "the batch was recorded; the launch is off")
        self._reconcile(launch_id, session, correlated)
        if recorded and self._route is not None:
            self._route(launch_id, tuple(recorded))
        return new

    @staticmethod
    def _prove(conn: sqlite3.Connection, now: str, launch_id: str, e: _Seen) -> bool:
        """§7.3 step 1: a user item carrying a send's op_id in the declared correlation field is that send's (True;
        False when no send of the launch has that op_id). A proof against a record already terminal (not delivered,
        or accepted naming another item) is kept as an anomaly; the record is never rewritten."""
        row = conn.execute("SELECT o.message_id FROM chat_outbox_events o JOIN chat_messages m ON m.message_id = "
                           "o.message_id WHERE m.launch_id = ? AND o.state = 'attempted' AND o.op_id = ?",
                           (launch_id, e.correlation)).fetchone()
        if row is None:
            return False
        state = outbox_state(conn, row[0])
        if state in ("attempted", "ambiguous"):
            outbox_step(conn, now, row[0], "accepted", cause="the vendor echoed our op_id",
                        vendor_turn_id=e.turn_id, vendor_item_id=e.vendor_id)
            return True
        named = conn.execute("SELECT vendor_item_id FROM chat_outbox_events WHERE message_id = ? AND state = "
                             "'accepted'", (row[0],)).fetchone()
        if state == "accepted" and named is not None and named[0] in (None, e.vendor_id):
            return True
        record_anomaly(conn, now, launch_id=launch_id, message_id=row[0], key=e.key, kind="proof_after_terminal",
                       detail=f"the vendor echoed this send's op_id on item {e.vendor_id} after it was recorded "
                              f"{state}")
        return True

    def _reconcile(self, launch_id: str, session: Any, correlated: bool) -> None:
        """§7.3 steps 2-4 for each ambiguous send of the launch (:func:`_absent` holds the rule). Read, decide outside
        any transaction, then step only a send still ambiguous (your mark may have resolved it since the read). The
        caller holds the launch's mutex."""
        read = self._chat._read(lambda conn: _launch_read(conn, launch_id))
        gone = _absent(read, session.pos_lt, correlated)
        if not gone:
            return
        with self._chat._write() as (conn, now):
            if _fingerprint(conn, launch_id) != read.fingerprint:
                raise CoordError(f"launch {launch_id}'s chat record changed while it was reconciled; nothing was "
                                 "written")
            for message_id in gone:
                if outbox_state(conn, message_id) == "ambiguous":
                    outbox_step(conn, now, message_id, "not_delivered",
                                cause="a complete read past its anchor shows another input and not this one")

    def candidates(self, message_id: str) -> list[str]:
        """§7.3 step 2's "probably this one": the vendor event keys in an ambiguous send's window whose normalized
        digest equals the send's. Shown beside it; never authorship, never delivery (B12). Empty with no session
        attached, or a wedged one (no ``pos_lt`` to place them with)."""
        m = self._chat.message(message_id)
        if m is None or m.launch_id is None or self._session(m.launch_id) is None:
            return []
        with self._launch_lock(m.launch_id):         # a detach or a send waits for the comparisons, and the reverse
            session = self._session(m.launch_id)
            with self._guard:
                wedged = m.launch_id in self._wedged
            if session is None or wedged:
                return []
            read = self._chat._read(lambda conn: _launch_read(conn, m.launch_id))
            att = next((a for a in read.attempts if a.message_id == m.message_id), None)
            if att is None or att.state != "ambiguous":
                return []
            return [i.key for i in _window(read, att, session.pos_lt) if i.norm == att.norm]

    def anomalies(self, launch_id: str) -> list[Anomaly]:
        """The launch's ``chat_anomalies`` rows, in order."""
        return self._chat._read(lambda conn: [Anomaly(*r) for r in conn.execute(
            "SELECT launch_id, seq, message_id, vendor_event_key, kind, detail, at FROM chat_anomalies WHERE "
            "launch_id = ? ORDER BY seq", (launch_id,))])

    def interrupt(self, launch_id: str, turn_id: str | None, owner_epoch: int | None = None) -> CommandResult:
        """§9: interrupt the launch's turn ``turn_id`` (None: whatever is running). Operator-authenticated by the
        caller and UNFENCED: it never reads the turn lease, so ``owner_epoch`` is accepted and checked against
        nothing. This half only calls ``session.interrupt`` with a fresh op_id and returns what the session said;
        it writes nothing (the in-flight approval rows a stop cancels are CH-D1c's). It does not wait on the launch's
        mutex: a stop never queues behind a send in flight. A driver that raises, or returns anything but a
        :class:`~levain.coordinator.driver.CommandResult`, is ``ambiguous``; so is one that has not answered within
        :data:`INTERRUPT_DEADLINE`, and that one also wedges the session (no further call reaches it, an interrupt
        included, until it is detached). The session is pinned until its call returns: :meth:`detach` refuses a new
        interrupt meanwhile and waits for the pin, at most :data:`INTERRUPT_DEADLINE`, before it closes the session
        (an interrupt reaches only the session it pinned; one stuck past the deadline is closed under it).
        ``turn_id`` is None or one line of plain text, as a recorded vendor turn id is (:func:`record_event`)."""
        if turn_id is not None:
            _line("an interrupt's turn id", turn_id)
        with self._guard:
            session = self._sessions.get(launch_id)
            if session is None:
                raise CoordError(f"no driver session is attached to launch {str(launch_id)[:40]}; nothing was sent")
            if launch_id in self._detaching:
                raise CoordError(f"launch {str(launch_id)[:40]}'s driver session is being detached; nothing was sent")
            if launch_id in self._wedged:
                raise CoordError(f"launch {str(launch_id)[:40]}'s driver session stopped answering; detach it; "
                                 "nothing was sent")
            self._pins[id(session)] = self._pins.get(id(session), 0) + 1
        command = InterruptCommand("op-" + secrets.token_hex(16), turn_id)
        ran = threading.Event()

        def unpin() -> None:
            with self._guard:
                self._pins[id(session)] -= 1
                if not self._pins[id(session)]:
                    del self._pins[id(session)]
                self._unpinned.notify_all()

        def call() -> Any:
            # the pin is released when the session's call returns, not when the caller stops waiting for it
            ran.set()
            try:
                return session.interrupt(command)
            finally:
                unpin()

        try:
            got = _bounded(call, INTERRUPT_DEADLINE)
        except _Overdue:
            self._wedge(launch_id, session)
            return CommandResult("ambiguous", f"the driver did not answer the interrupt within "
                                              f"{INTERRUPT_DEADLINE:g} s; its session is used no more until it is "
                                              "detached")
        except BaseException as exc:
            if not ran.is_set():
                unpin()                               # the call's thread never started: nothing else releases it
            if not isinstance(exc, Exception):
                raise
            return CommandResult("ambiguous", _cause_text(f"the driver raised {type(exc).__name__}: {_said(exc)}"))
        if not isinstance(got, CommandResult) or type(got.outcome) is not str or got.outcome not in (
                "confirmed", "ambiguous", "rejected", "no_such_request"):
            return CommandResult("ambiguous", "the driver returned no command result")
        return CommandResult(got.outcome, _cause_text(got.detail if type(got.detail) is str else got.outcome))

    # --- liveness: the activation writer, the probe, your re-enable, the composer (§4.1-§4.3, H9) -------------
    def activate(self, launch_id: str, kind: str, detail: str, *, evidence: LivenessEvidence | None = None,
                 spawn_nonce: str | None = None, canary: Any = None, containment_digest: str | None = None) -> int:
        """An activation row (source ``activation``) from a verdict the LAUNCHER computed (§3.3; CH-D2 / CH-D3 hold
        the vendor checks): ``probing`` at spawn, ``probing`` with ``evidence`` once Claude's stage 1 passed (the
        initialize response checked; it opens the first send, H9), and ``live`` / ``not_live``. A Claude row always
        names its child (``spawn_nonce``) and a Claude ``live`` carries the accepted canary verdict (its id is
        computed here, :func:`canary_verdict_id`) and the spawn's containment digest; a Codex row carries none of
        the three. The seq."""
        if kind not in ("probing", "live", "not_live"):
            raise CoordError("an activation row is probing, live or not_live")
        if evidence is not None and type(evidence) is not LivenessEvidence:
            raise CoordError("activation evidence is a LivenessEvidence")
        digest = None if evidence is None else _evidence_sha256(evidence)
        if evidence is not None and digest is None:
            raise CoordError("activation evidence that cannot be serialised is refused; nothing was written")
        verdict_id = None
        if canary is not None:
            if getattr(canary, "ok", None) is not True:
                raise CoordError("an activation accepts only a canary verdict that passed")
            verdict_id = canary_verdict_id(canary)
        # a launch that does not exist gets no lock: the registry holds attached and real launches only
        self._chat._read(lambda conn: _worker_launch(conn, launch_id))
        with self._launch_lock(launch_id):
            seq = self._activation_row(launch_id, kind, detail, digest, spawn_nonce, verdict_id, containment_digest)
            self._stale_handled(launch_id)
        self._file_latches(launch_id)
        return seq

    def _activation_row(self, launch_id: str, kind: str, detail: str, digest: str | None, spawn_nonce: str | None,
                        verdict_id: str | None, containment_digest: str | None) -> int:
        """:meth:`activate`'s row, in its own transaction (the caller holds the launch's mutex). The seq."""
        with self._chat._write() as (conn, now):
            harness = _launch_state(conn, launch_id)[2]
            if harness == "claude" and (spawn_nonce is None or kind == "live" and (
                    verdict_id is None or containment_digest is None)):
                raise CoordError("a Claude activation names its child, and its live carries the canary verdict and "
                                 "the containment digest; nothing was written")
            if harness != "claude" and (spawn_nonce, verdict_id, containment_digest) != (None, None, None):
                raise CoordError(f"a {harness} activation carries no spawn nonce, canary or containment digest; "
                                 "nothing was written")
            if kind == "probing" and digest is not None and spawn_nonce is not None and conn.execute(
                    "SELECT 1 FROM chat_launch_checks WHERE launch_id = ? AND source = 'activation' AND spawn_nonce "
                    "= ? AND evidence_sha256 IS NOT NULL LIMIT 1", (launch_id, spawn_nonce)).fetchone() is not None:
                raise CoordError("stage 1 of this child is recorded already; a replay opens no second first send; "
                                 "nothing was written")
            return record_check(conn, now, launch_id, kind, "activation", detail, evidence_sha256=digest,
                                spawn_nonce=spawn_nonce, canary_verdict_id=verdict_id,
                                containment_digest=containment_digest)

    def _stale_handled(self, launch_id: str) -> None:
        """After an activation or a re-enable, in the same hold of the launch's mutex as its commit (so no read
        holds an entry across it): every control event still open in the handled record turns stale, so an
        interrupt it asked for and that names no turn never reaches a turn started after it."""
        with self._guard:
            record = self._handled.get(launch_id, {})
            record.update([(ident, h._replace(state="stale")) for ident, h in record.items() if h.state == "open"])

    def reenable(self, launch_id: str, owner: str) -> int:
        """Your re-enable (§4.2): clears a latch only while the latest check read live again. ``owner`` is the
        authenticated client. The service never writes this row itself."""
        owner = chat_lease.check_owner(owner)
        self._chat._read(lambda conn: _worker_launch(conn, launch_id))
        with self._launch_lock(launch_id):
            with self._chat._write() as (conn, now):
                seq = record_check(conn, now, launch_id, "reenabled", "operator", f"re-enabled by {owner}",
                                   by="operator")
            self._stale_handled(launch_id)
        return seq

    def composer(self, launch_id: str, owner: str) -> Composer:
        """:func:`composer_state` for ``owner``, read without the write lock."""
        attached = self._session(launch_id) is not None and launch_id not in self._wedged
        return self._chat._read(lambda conn: composer_state(conn, self._chat._opener._stamp(), launch_id, owner,
                                                            attached=attached))

    def _watch(self, launch_id: str, session: Any, claude: bool, raws: Sequence[Any], seen: Sequence[Any],
               backfill: Sequence[bool], fresh: Sequence[bool], crossed: str | None,
               gap: bool) -> tuple[TripwireWindow | None, list[Trip], list[list[Trip]], list[_Control]]:
        """§4.4 on Claude, before the write transaction: ``(window copy, the batch's own trips, each event's trips,
        the control lines to answer or interrupt)``. The copy observes each event the write records new and that is
        not history; the mode is read from the session (a meta that cannot be read trips). A batch that crosses a
        boundary is not recorded, so nothing of it feeds the window, but a mode one of its lines reports is still kept
        and each of its control lines is still answered or interrupted. An effective gap drops the counts and, with a call open, trips."""
        at: list[list[Trip]] = [[] for _ in seen]
        if not claude:
            return None, [], at, []
        window, early, controls = self._window(launch_id), [], []
        mode = self._mode(launch_id, session)
        early += [mode] if mode is not None else []
        new = [(i, raw, e) for i, (raw, e, old, f) in enumerate(zip(raws, seen, backfill, fresh)) if f and not old]

        def control(e: _Seen, trip: Trip | None, reply: ControlReply | None) -> None:
            stop = trip if trip is not None and trip.interrupt else None
            if stop is not None or reply is not None:
                controls.append(_Control((str(e.pos), str(e.vendor_id)), stop, reply))

        if crossed is not None:
            for _i, raw, e in new:
                reported = reported_mode_trip(raw)
                trip, reply = control_line(raw) or (None, None)
                early += [t for t in (reported, trip) if t is not None]
                control(e, trip, reply)
            return window, early, at, controls
        was_open = bool(window.calls)
        for i, raw, e in new:
            trip, reply = window.observe(raw)
            at[i] += [trip] if trip is not None else []
            control(e, trip, reply)
            if e.kind in ("turn_started", "turn_ended"):
                trip = window.boundary(turn=True)
                at[i] += [trip] if trip is not None else []
        if gap:
            trip = window.gap(was_open)
            early += [trip] if trip is not None else []
        return window, early, at, controls

    def _window(self, launch_id: str) -> TripwireWindow:
        """A copy of the launch's tripwire window, or a fresh one (with the launcher's context) for a session not
        yet watched."""
        with self._guard:
            held = self._windows.get(launch_id)
        if held is not None:
            return held.copy()
        ctx = None
        if self._tripwires is not None:
            try:
                ctx = self._tripwires(launch_id)
            except Exception:  # noqa: BLE001 - no context: the launch's first tool_result trips
                ctx = None
        return TripwireWindow(ctx if isinstance(ctx, TripwireContext) else None)

    def _mode(self, launch_id: str, session: Any) -> Trip | None:
        """``mode_changed`` from the session's meta (its init, or a later line that reported a change), bounded by
        :data:`READ_DEADLINE`; a meta that cannot be read trips it too (an overdue one also wedges the session)."""
        try:
            meta = _bounded(session.meta, READ_DEADLINE)
        except _Overdue:
            self._wedge(launch_id, session)
            return Trip("mode_changed", "could not read the session's permission mode: the read is overdue")
        except Exception as exc:  # noqa: BLE001 - an unread mode is never read as default
            return Trip("mode_changed", f"could not read the session's permission mode: {type(exc).__name__}")
        if type(meta) is not SessionMeta:
            return Trip("mode_changed", "could not read the session's permission mode: the session returned no meta")
        return mode_trip(meta.permission_mode)

    def _answer_controls(self, launch_id: str, session: Any, controls: Sequence[_Control]) -> list[str]:
        """Before the batch commits (the caller holds the launch's mutex; no transaction is open): each inbound
        non-approval control request gets its named error reply through ``reject_control`` (§8.1), bounded by
        :data:`READ_DEADLINE`. Then, when a trip asked for it or a reply failed, ONE interrupt of the turn (§8.1, O6:
        the CLI's pending requests abort); an interrupt that is refused or ambiguous leaves the latch as the record.
        The handled-control record (``_handled``) decides what is sent: nothing for a control event already done, no
        second interrupt for one already interrupted, and no interrupt naming no turn for one left over from before the
        launch's last activation or re-enable. What failed, for the
        caller to latch ``tripwire:unanswerable_control`` in the batch's transaction."""
        with self._guard:
            record = self._handled.setdefault(launch_id, {})
            held = {c.ident: record.setdefault(c.ident, _Handled("open")) for c in controls}
        failed, stops, unanswered = [], [], []
        for c in controls:
            state, interrupted = held[c.ident]
            if state == "done":
                continue
            if c.stop is not None and not interrupted and (c.stop.turn_id is not None or state == "open"):
                stops.append(c)
            if c.reply is None:
                continue
            why = self._reject(launch_id, session, c.reply)
            if why is None:
                with self._guard:
                    record[c.ident] = _Handled("done", interrupted)
                continue
            failed.append(f"{c.reply.request_id[:40]}: {why}")
            if state == "open" and not interrupted:
                unanswered.append(c)
        if stops or unanswered:
            turn = next((c.stop.turn_id for c in stops if c.stop.turn_id is not None), None)
            try:
                got = self.interrupt(launch_id, turn)
            except CoordError:
                got = None                            # detached or wedged: the latch stands, and close is the stop
            if got is not None and got.outcome in ("confirmed", "no_such_request"):
                with self._guard:
                    # a stop carries no reply, so an interrupted stop is done; an unanswered reply is retried
                    record.update((c.ident, _Handled("done", True)) for c in stops)
                    record.update((c.ident, record[c.ident]._replace(interrupted=True)) for c in unanswered)
        return failed

    def _reject(self, launch_id: str, session: Any, reply: ControlReply) -> str | None:
        """Send one named error reply; None when the session confirmed it, else why it failed (a failed reply is tried
        again on a re-read). A reply answered ``no_such_request`` fails: the caller sends a control event only while
        the record holds it not done, so the session never confirmed this event and its request is unaccounted for
        (T1's ``unanswerable_control`` latch)."""
        with self._guard:
            wedged = launch_id in self._wedged
        if wedged:
            return "the session stopped answering"
        try:
            got = _bounded(functools.partial(session.reject_control, reply.request_id, reply.text), READ_DEADLINE)
        except _Overdue:
            self._wedge(launch_id, session)
            return f"the reply did not return within {READ_DEADLINE:g} s"
        except Exception as exc:  # noqa: BLE001 - an unwritten reply leaves the CLI waiting: latch and stop
            return f"{type(exc).__name__}: {_said(exc)}"
        if isinstance(got, CommandResult) and got.outcome == "no_such_request":
            return "the session holds no such request"
        if not isinstance(got, CommandResult) or got.outcome != "confirmed":
            return "the session did not confirm the reply"
        return None

    def _probe_pending(self, launch_id: str, session: Any) -> None:
        """Answer the launch's boundary ``probing`` row, if its latest check is one (the caller holds the launch's
        mutex): up to :data:`PROBE_TRIES` tries :data:`PROBE_GAP` apart, each reading fresh evidence from the
        session and judging it with the service's probe; a try that raises, returns anything but a
        :class:`Verdict`, or reads evidence that cannot be serialised fails. If every try fails, ``not_live``
        ("could not check: <why>"), which latches. An evidence read that has not returned by :data:`READ_DEADLINE`
        ends the probe at once: the session is wedged (no second call while that one is in flight) and the launch
        latches ``not_live`` ("could not check: evidence read overdue"). A wedged session is not called: its
        boundary's ``probing`` row stands."""
        pending = self._chat._read(lambda conn: _pending_probe(conn, launch_id))
        with self._guard:
            wedged = launch_id in self._wedged
        if pending is None or wedged:
            return                                    # a wedged session is called no more: the probing row stands
        why = "no probe ran"
        for attempt in range(PROBE_TRIES):
            if attempt:
                self._sleep(PROBE_GAP)
            try:
                evidence = _bounded(session.liveness_evidence, READ_DEADLINE)
            except _Overdue:
                # its call is still in flight: no second one on this session, no further try
                self._wedge(launch_id, session)
                self._write_verdict(launch_id, pending, "not_live", "could not check: evidence read overdue", None)
                return
            except Exception as exc:  # noqa: BLE001 - a failed try, retried; never read as live
                why = f"{type(exc).__name__}: {_said(exc)}"
                continue
            try:
                if type(evidence) is not LivenessEvidence:
                    raise CoordError("the session returned no liveness evidence")
                digest = _evidence_sha256(evidence)
                if digest is None:
                    raise CoordError("the session's evidence cannot be serialised")
                verdict = self._verdict(launch_id, evidence)
            except Exception as exc:  # noqa: BLE001 - a failed try, retried; never read as live
                why = f"{type(exc).__name__}: {_said(exc)}"
                continue
            self._write_verdict(launch_id, pending, "live" if verdict.live else "not_live",
                                _cause_text(verdict.detail or ("live" if verdict.live else "not live")), digest)
            return
        self._write_verdict(launch_id, pending, "not_live", _cause_text(f"could not check: {why}"), None)

    def _verdict(self, launch_id: str, evidence: LivenessEvidence) -> Verdict:
        """The probe's verdict on ``evidence``, read back by exact type: a :class:`Verdict` whose ``live`` is a bool
        and ``detail`` a str, or the try fails."""
        if self._probe is None:
            raise CoordError("no probe is configured for this service")
        got = self._probe(launch_id, evidence)
        if type(got) is not Verdict or type(got.live) is not bool or type(got.detail) is not str:
            raise CoordError("the probe returned no verdict")
        return got

    def _write_verdict(self, launch_id: str, pending: tuple[int, str], kind: str, detail: str,
                       evidence: str | None) -> None:
        """The verdict row for the boundary ``pending`` names (its source), written only while that boundary's
        probing is still the launch's latest check."""
        with self._chat._write() as (conn, now):
            if _pending_probe(conn, launch_id) != pending:
                return
            record_check(conn, now, launch_id, kind, pending[1], detail, evidence_sha256=evidence)
        self._file_latches(launch_id)

    def _file_latches(self, launch_id: str) -> None:
        """§4.3: file each ``seat`` item the launch's latches owe and this process has not filed while the launch's
        session is attached. A filer that raises, or a read that does, files nothing now; the next sweep asks again.
        Never raises an Exception."""
        if self._seat_item is None:
            return
        try:
            owed = self._chat._read(lambda conn: _owed_items(conn, launch_id))
        except Exception:  # noqa: BLE001 - after a latch committed: a failed read costs the item, never the latch
            return
        for key, body in owed:
            with self._guard:
                if key in self._filed.get(launch_id, ()):
                    continue
            try:
                self._seat_item(key, body)
            except Exception:  # noqa: BLE001 - the latch is the record; the item is its pointer, filed next sweep
                continue
            with self._guard:
                # cached only while attached (detach drops it): a launch with no session asks the filer each time
                if launch_id in self._sessions:
                    self._filed.setdefault(launch_id, set()).add(key)

    def mark(self, message_id: str, delivered: bool, owner: str) -> str:
        """§7.4: you resolve an ambiguous send, delivered or not. ``owner`` is the authenticated client."""
        owner = chat_lease.check_owner(owner)
        state = "accepted" if delivered else "not_delivered"
        with self._chat._write() as (conn, now):
            outbox_step(conn, now, message_id, state, by="operator", cause=f"marked by {owner}")
        return state


def _command(m: ChatMessage, op_id: str) -> SendCommand:
    """The driver command for a message's one attempt."""
    return SendCommand(op_id, m.body, tuple(a["sha256"] for a in m.attachments), m.mode, m.expected_turn_id)

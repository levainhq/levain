"""Frozen copies of the chat schema text two released shapes held, for the migration tests (D1c-C).

SCHEMA_V0_11_25 is ``CHAT_SCHEMA`` as tag v0.11.25 shipped it; SCHEMA_MAIN_F75ACBC7 is ``CHAT_SCHEMA`` and
``CHAT_ADDED_COLUMNS`` on main at f75acbc7, before the approval tables. Both were dumped by importing
that revision's ``levain.coordinator.chat`` in a scratch checkout; nothing here is hand-edited."""
from __future__ import annotations

SCHEMA_V0_11_25: tuple[str, ...] = ('CREATE TABLE IF NOT EXISTS chat_messages (message_id TEXT PRIMARY KEY, gseq INTEGER NOT NULL UNIQUE, thread_id '
 'TEXT NOT NULL REFERENCES threads(thread_id), launch_id TEXT REFERENCES launches(launch_id), recipient TEXT NOT '
 "NULL CHECK (recipient IN ('coordinator', 'worker', 'thread')), author_kind TEXT NOT NULL CHECK (author_kind IN "
 "('operator', 'coordinator')), author_surface TEXT NOT NULL CHECK (author_surface IN ('cockpit', 'phone', 'cli', "
 "'coordinator')), client_id TEXT NOT NULL, outbox_id TEXT NOT NULL UNIQUE, mode TEXT NOT NULL CHECK (mode IN "
 "('queue', 'steer')), expected_turn_id TEXT, owner_epoch INTEGER, body TEXT NOT NULL, body_sha256 TEXT NOT NULL, "
 'body_norm_sha256 TEXT NOT NULL, attachments TEXT NOT NULL, draft_ref TEXT, at TEXT NOT NULL, CHECK ((recipient = '
 "'worker') = (launch_id IS NOT NULL)), CHECK ((author_kind = 'coordinator') = (author_surface = 'coordinator')), "
 "CHECK (author_kind = 'operator' OR (mode = 'queue' AND owner_epoch IS NULL)), CHECK ((mode = 'steer') = "
 "(expected_turn_id IS NOT NULL)), CHECK (mode = 'queue' OR recipient = 'worker'), CHECK (length(body) <= 20000))",
 "CREATE TRIGGER IF NOT EXISTS chat_messages_frozen BEFORE UPDATE ON chat_messages BEGIN SELECT RAISE(ABORT, 'a chat "
 "message is append-only'); END",
 "CREATE TRIGGER IF NOT EXISTS chat_messages_kept BEFORE DELETE ON chat_messages BEGIN SELECT RAISE(ABORT, 'a chat "
 "message is append-only'); END",
 'CREATE TRIGGER IF NOT EXISTS chat_messages_no_replace BEFORE INSERT ON chat_messages WHEN EXISTS (SELECT 1 FROM '
 'chat_messages WHERE message_id = NEW.message_id OR outbox_id = NEW.outbox_id OR gseq = NEW.gseq OR rowid = '
 "NEW.rowid) BEGIN SELECT RAISE(ABORT, 'a chat message is never replaced'); END",
 'CREATE TRIGGER IF NOT EXISTS chat_messages_worker BEFORE INSERT ON chat_messages WHEN NEW.launch_id IS NOT NULL '
 'AND NOT EXISTS (SELECT 1 FROM launches l, json_each(l.threads) j WHERE l.launch_id = NEW.launch_id AND l.role = '
 "'worker' AND j.value = NEW.thread_id) BEGIN SELECT RAISE(ABORT, 'a chat message to a worker names a worker launch "
 "of its thread'); END",
 'CREATE TABLE IF NOT EXISTS chat_outbox_events (message_id TEXT NOT NULL REFERENCES chat_messages(message_id), seq '
 "INTEGER NOT NULL, gseq INTEGER NOT NULL UNIQUE, state TEXT NOT NULL CHECK (state IN ('refused', 'pending', "
 "'attempted', 'accepted', 'ambiguous', 'not_delivered', 'cancelled')), op_id TEXT, vendor_anchor TEXT, "
 "vendor_turn_id TEXT, vendor_item_id TEXT, cause TEXT NOT NULL, by TEXT NOT NULL CHECK (by IN ('service', "
 "'operator')), at TEXT NOT NULL, PRIMARY KEY (message_id, seq), CHECK (state <> 'attempted' OR (op_id IS NOT NULL "
 "AND vendor_anchor IS NOT NULL)), CHECK (state = 'attempted' OR (op_id IS NULL AND vendor_anchor IS NULL)), CHECK "
 "(state IN ('accepted', 'ambiguous', 'not_delivered') OR (vendor_turn_id IS NULL AND vendor_item_id IS NULL)))",
 'CREATE UNIQUE INDEX IF NOT EXISTS chat_outbox_one_attempt ON chat_outbox_events (message_id) WHERE state = '
 "'attempted'",
 'CREATE TRIGGER IF NOT EXISTS chat_outbox_events_frozen BEFORE UPDATE ON chat_outbox_events BEGIN SELECT '
 "RAISE(ABORT, 'a chat outbox event is append-only'); END",
 'CREATE TRIGGER IF NOT EXISTS chat_outbox_events_kept BEFORE DELETE ON chat_outbox_events BEGIN SELECT RAISE(ABORT, '
 "'a chat outbox event is append-only'); END",
 'CREATE TRIGGER IF NOT EXISTS chat_outbox_events_next BEFORE INSERT ON chat_outbox_events WHEN NEW.seq != '
 'COALESCE((SELECT MAX(seq) FROM chat_outbox_events WHERE message_id = NEW.message_id), 0) + 1 OR EXISTS (SELECT 1 '
 "FROM chat_outbox_events WHERE rowid = NEW.rowid OR gseq = NEW.gseq) BEGIN SELECT RAISE(ABORT, 'an outbox event is "
 "the next seq of its message'); END",
 'CREATE TRIGGER IF NOT EXISTS chat_outbox_events_edges BEFORE INSERT ON chat_outbox_events WHEN ((SELECT state FROM '
 'chat_outbox_events WHERE message_id = NEW.message_id ORDER BY seq DESC LIMIT 1) IS NULL AND NEW.state NOT IN '
 "('refused', 'pending')) OR ((SELECT state FROM chat_outbox_events WHERE message_id = NEW.message_id ORDER BY seq "
 "DESC LIMIT 1) = 'pending' AND NEW.state NOT IN ('attempted', 'cancelled', 'not_delivered')) OR ((SELECT state FROM "
 "chat_outbox_events WHERE message_id = NEW.message_id ORDER BY seq DESC LIMIT 1) = 'attempted' AND NEW.state NOT IN "
 "('accepted', 'ambiguous', 'not_delivered')) OR ((SELECT state FROM chat_outbox_events WHERE message_id = "
 "NEW.message_id ORDER BY seq DESC LIMIT 1) = 'ambiguous' AND NEW.state NOT IN ('accepted', 'not_delivered')) OR "
 "(SELECT state FROM chat_outbox_events WHERE message_id = NEW.message_id ORDER BY seq DESC LIMIT 1) IN ('accepted', "
 "'refused', 'cancelled', 'not_delivered') OR (NEW.by = 'operator' AND (SELECT state FROM chat_outbox_events WHERE "
 "message_id = NEW.message_id ORDER BY seq DESC LIMIT 1) IS NOT 'ambiguous') BEGIN SELECT RAISE(ABORT, 'an outbox "
 "event takes an edge §2.2 does not allow'); END",
 'CREATE TABLE IF NOT EXISTS chat_events (launch_id TEXT NOT NULL REFERENCES launches(launch_id), vendor_event_key '
 'TEXT NOT NULL, gseq INTEGER NOT NULL UNIQUE, vendor_pos TEXT NOT NULL, kind TEXT NOT NULL CHECK (kind IN '
 "('turn_started', 'turn_ended', 'user_item', 'agent_text', 'tool_call', 'tool_result', 'approval_request', "
 "'approval_resolved', 'error', 'gap')), vendor_turn_id TEXT, call_id TEXT, body TEXT NOT NULL, body_sha256 TEXT NOT "
 'NULL, body_norm_sha256 TEXT NOT NULL, truncated INTEGER NOT NULL CHECK (truncated IN (0, 1)), backfill INTEGER NOT '
 "NULL CHECK (backfill IN (0, 1)), source TEXT NOT NULL CHECK (source = 'vendor'), at TEXT NOT NULL, PRIMARY KEY "
 '(launch_id, vendor_event_key), CHECK (length(CAST(body AS BLOB)) <= 16384), CHECK (substr(vendor_event_key, 1, '
 "length(kind) + 1) = kind || ':' AND length(vendor_event_key) > length(kind) + 1))",
 "CREATE TRIGGER IF NOT EXISTS chat_events_frozen BEFORE UPDATE ON chat_events BEGIN SELECT RAISE(ABORT, 'a chat "
 "event is append-only'); END",
 "CREATE TRIGGER IF NOT EXISTS chat_events_kept BEFORE DELETE ON chat_events BEGIN SELECT RAISE(ABORT, 'a chat event "
 "is append-only'); END",
 'CREATE TRIGGER IF NOT EXISTS chat_events_no_replace BEFORE INSERT ON chat_events WHEN EXISTS (SELECT 1 FROM '
 'chat_events WHERE (launch_id = NEW.launch_id AND vendor_event_key = NEW.vendor_event_key) OR gseq = NEW.gseq OR '
 "rowid = NEW.rowid) BEGIN SELECT RAISE(ABORT, 'a chat event is never replaced'); END",
 'CREATE TABLE IF NOT EXISTS chat_cursors (launch_id TEXT NOT NULL REFERENCES launches(launch_id), seq INTEGER NOT '
 'NULL, gseq INTEGER NOT NULL UNIQUE, vendor_cursor TEXT, complete_through TEXT, gap INTEGER NOT NULL CHECK (gap IN '
 '(0, 1)), at TEXT NOT NULL, PRIMARY KEY (launch_id, seq))',
 "CREATE TRIGGER IF NOT EXISTS chat_cursors_frozen BEFORE UPDATE ON chat_cursors BEGIN SELECT RAISE(ABORT, 'a chat "
 "cursor is append-only'); END",
 "CREATE TRIGGER IF NOT EXISTS chat_cursors_kept BEFORE DELETE ON chat_cursors BEGIN SELECT RAISE(ABORT, 'a chat "
 "cursor is append-only'); END",
 'CREATE TRIGGER IF NOT EXISTS chat_cursors_next BEFORE INSERT ON chat_cursors WHEN NEW.seq != COALESCE((SELECT '
 'MAX(seq) FROM chat_cursors WHERE launch_id = NEW.launch_id), 0) + 1 OR EXISTS (SELECT 1 FROM chat_cursors WHERE '
 "rowid = NEW.rowid OR gseq = NEW.gseq) BEGIN SELECT RAISE(ABORT, 'a cursor is the next seq of its launch'); END",
 'CREATE TABLE IF NOT EXISTS chat_anomalies (launch_id TEXT NOT NULL REFERENCES launches(launch_id), seq INTEGER NOT '
 'NULL, gseq INTEGER NOT NULL UNIQUE, message_id TEXT REFERENCES chat_messages(message_id), vendor_event_key TEXT '
 "NOT NULL, kind TEXT NOT NULL CHECK (kind IN ('proof_after_terminal')), detail TEXT NOT NULL, at TEXT NOT NULL, "
 'PRIMARY KEY (launch_id, seq))',
 "CREATE TRIGGER IF NOT EXISTS chat_anomalies_frozen BEFORE UPDATE ON chat_anomalies BEGIN SELECT RAISE(ABORT, 'a "
 "chat anomaly is append-only'); END",
 "CREATE TRIGGER IF NOT EXISTS chat_anomalies_kept BEFORE DELETE ON chat_anomalies BEGIN SELECT RAISE(ABORT, 'a chat "
 "anomaly is append-only'); END",
 'CREATE TRIGGER IF NOT EXISTS chat_anomalies_next BEFORE INSERT ON chat_anomalies WHEN NEW.seq != COALESCE((SELECT '
 'MAX(seq) FROM chat_anomalies WHERE launch_id = NEW.launch_id), 0) + 1 OR EXISTS (SELECT 1 FROM chat_anomalies '
 "WHERE rowid = NEW.rowid OR gseq = NEW.gseq) BEGIN SELECT RAISE(ABORT, 'an anomaly is the next seq of its launch'); "
 'END',
 'CREATE TABLE IF NOT EXISTS chat_launch_checks (launch_id TEXT NOT NULL REFERENCES launches(launch_id), seq INTEGER '
 "NOT NULL, gseq INTEGER NOT NULL UNIQUE, kind TEXT NOT NULL CHECK (kind IN ('probing', 'live', 'not_live', "
 "'reenabled')), source TEXT NOT NULL CHECK (source IN ('activation', 'turn_start', 'send', 'result', 'operator') OR "
 "(source GLOB 'tripwire:?*' AND kind = 'not_live')), evidence_sha256 TEXT CHECK (evidence_sha256 IS NULL OR "
 "(length(evidence_sha256) = 71 AND substr(evidence_sha256, 1, 7) = 'sha256:' AND substr(evidence_sha256, 8) NOT "
 "GLOB '*[^0-9a-f]*')), spawn_nonce TEXT, detail TEXT NOT NULL, by TEXT NOT NULL CHECK (by IN ('service', "
 "'operator')), at TEXT NOT NULL, PRIMARY KEY (launch_id, seq), CHECK ((kind = 'reenabled') = (by = 'operator')), "
 "CHECK ((kind = 'reenabled') = (source = 'operator')), CHECK (spawn_nonce IS NULL OR (source = 'activation' AND "
 "length(spawn_nonce) = 32 AND spawn_nonce NOT GLOB '*[^0-9a-f]*')))",
 'CREATE TRIGGER IF NOT EXISTS chat_launch_checks_frozen BEFORE UPDATE ON chat_launch_checks BEGIN SELECT '
 "RAISE(ABORT, 'a launch check is append-only'); END",
 'CREATE TRIGGER IF NOT EXISTS chat_launch_checks_kept BEFORE DELETE ON chat_launch_checks BEGIN SELECT RAISE(ABORT, '
 "'a launch check is append-only'); END",
 'CREATE TRIGGER IF NOT EXISTS chat_launch_checks_next BEFORE INSERT ON chat_launch_checks WHEN NEW.seq != '
 'COALESCE((SELECT MAX(seq) FROM chat_launch_checks WHERE launch_id = NEW.launch_id), 0) + 1 OR EXISTS (SELECT 1 '
 "FROM chat_launch_checks WHERE rowid = NEW.rowid OR gseq = NEW.gseq) BEGIN SELECT RAISE(ABORT, 'a launch check is "
 "the next seq of its launch'); END",
 'CREATE TRIGGER IF NOT EXISTS chat_launch_checks_reenable BEFORE INSERT ON chat_launch_checks WHEN NEW.kind = '
 "'reenabled' AND NOT ((SELECT kind FROM chat_launch_checks WHERE launch_id = NEW.launch_id AND kind IN ('probing', "
 "'live', 'not_live') ORDER BY seq DESC LIMIT 1) = 'live' AND EXISTS (SELECT 1 FROM chat_launch_checks WHERE "
 "launch_id = NEW.launch_id AND kind = 'not_live' AND gseq > COALESCE((SELECT MAX(gseq) FROM chat_launch_checks "
 "WHERE launch_id = NEW.launch_id AND kind = 'reenabled'), 0))) BEGIN SELECT RAISE(ABORT, 'a re-enable clears a "
 "latch whose latest probe read live'); END",
 'CREATE TABLE IF NOT EXISTS turn_lease_events (seq INTEGER PRIMARY KEY AUTOINCREMENT, gseq INTEGER NOT NULL UNIQUE, '
 "launch_id TEXT NOT NULL REFERENCES launches(launch_id), kind TEXT NOT NULL CHECK (kind IN ('claim', 'handoff', "
 "'take', 'outside', 'beat', 'outside_beat', 'release', 'lapse')), token INTEGER, owner TEXT NOT NULL, owner_kind "
 "TEXT NOT NULL CHECK (owner_kind IN ('client', 'terminal', 'vendor_app')), until TEXT, cause TEXT NOT NULL, by TEXT "
 "NOT NULL CHECK (by IN ('operator', 'service')), at TEXT NOT NULL, CHECK ((kind IN ('claim', 'handoff', 'take', "
 "'outside')) = (token IS NULL)), CHECK ((kind IN ('release', 'lapse')) = (until IS NULL)), CHECK ((owner_kind = "
 "'client' AND (owner GLOB 'cockpit:?*' OR owner GLOB 'phone:?*' OR owner GLOB 'cli:?*')) OR (owner_kind != 'client' "
 "AND owner = owner_kind)), CHECK ((kind IN ('claim', 'handoff', 'take', 'beat') AND by = 'operator' AND owner_kind "
 "= 'client') OR (kind IN ('outside', 'outside_beat') AND by = 'service' AND owner_kind != 'client') OR (kind = "
 "'lapse' AND by = 'service') OR kind = 'release'))",
 'CREATE TRIGGER IF NOT EXISTS turn_lease_events_frozen BEFORE UPDATE ON turn_lease_events BEGIN SELECT RAISE(ABORT, '
 "'a turn lease event is append-only'); END",
 'CREATE TRIGGER IF NOT EXISTS turn_lease_events_kept BEFORE DELETE ON turn_lease_events BEGIN SELECT RAISE(ABORT, '
 "'a turn lease event is append-only'); END",
 'CREATE TRIGGER IF NOT EXISTS turn_lease_events_no_replace BEFORE INSERT ON turn_lease_events WHEN EXISTS (SELECT 1 '
 "FROM turn_lease_events WHERE seq = NEW.seq OR gseq = NEW.gseq) BEGIN SELECT RAISE(ABORT, 'a turn lease event is "
 "never replaced'); END",
 'CREATE TRIGGER IF NOT EXISTS turn_lease_events_edges BEFORE INSERT ON turn_lease_events WHEN (NEW.kind IN '
 "('claim', 'handoff', 'take', 'outside', 'beat', 'outside_beat') AND NOT EXISTS (SELECT 1 FROM launches l WHERE "
 "l.launch_id = NEW.launch_id AND l.role = 'worker' AND (SELECT kind FROM launch_events e WHERE e.launch_id = "
 "l.launch_id ORDER BY seq DESC LIMIT 1) = 'active')) OR (NEW.kind = 'claim' AND ((SELECT seq FROM turn_lease_events "
 "WHERE launch_id = NEW.launch_id AND kind IN ('claim', 'handoff', 'take', 'outside') ORDER BY seq DESC LIMIT 1) IS "
 'NOT NULL AND NOT EXISTS (SELECT 1 FROM turn_lease_events WHERE token = (SELECT seq FROM turn_lease_events WHERE '
 "launch_id = NEW.launch_id AND kind IN ('claim', 'handoff', 'take', 'outside') ORDER BY seq DESC LIMIT 1) AND kind "
 "IN ('release', 'lapse')) AND NEW.at < (SELECT MAX(until) FROM turn_lease_events WHERE seq = (SELECT seq FROM "
 "turn_lease_events WHERE launch_id = NEW.launch_id AND kind IN ('claim', 'handoff', 'take', 'outside') ORDER BY seq "
 "DESC LIMIT 1) OR (token = (SELECT seq FROM turn_lease_events WHERE launch_id = NEW.launch_id AND kind IN ('claim', "
 "'handoff', 'take', 'outside') ORDER BY seq DESC LIMIT 1) AND kind IN ('beat', 'outside_beat'))))) OR (NEW.kind = "
 "'handoff' AND (NOT ((SELECT seq FROM turn_lease_events WHERE launch_id = NEW.launch_id AND kind IN ('claim', "
 "'handoff', 'take', 'outside') ORDER BY seq DESC LIMIT 1) IS NOT NULL AND NOT EXISTS (SELECT 1 FROM "
 'turn_lease_events WHERE token = (SELECT seq FROM turn_lease_events WHERE launch_id = NEW.launch_id AND kind IN '
 "('claim', 'handoff', 'take', 'outside') ORDER BY seq DESC LIMIT 1) AND kind IN ('release', 'lapse')) AND NEW.at < "
 '(SELECT MAX(until) FROM turn_lease_events WHERE seq = (SELECT seq FROM turn_lease_events WHERE launch_id = '
 "NEW.launch_id AND kind IN ('claim', 'handoff', 'take', 'outside') ORDER BY seq DESC LIMIT 1) OR (token = (SELECT "
 "seq FROM turn_lease_events WHERE launch_id = NEW.launch_id AND kind IN ('claim', 'handoff', 'take', 'outside') "
 "ORDER BY seq DESC LIMIT 1) AND kind IN ('beat', 'outside_beat')))) OR (SELECT owner_kind FROM turn_lease_events "
 "WHERE seq = (SELECT seq FROM turn_lease_events WHERE launch_id = NEW.launch_id AND kind IN ('claim', 'handoff', "
 "'take', 'outside') ORDER BY seq DESC LIMIT 1)) != 'client')) OR (NEW.kind NOT IN ('claim', 'handoff', 'take', "
 "'outside') AND (NEW.token IS NOT (SELECT seq FROM turn_lease_events WHERE launch_id = NEW.launch_id AND kind IN "
 "('claim', 'handoff', 'take', 'outside') ORDER BY seq DESC LIMIT 1) OR EXISTS (SELECT 1 FROM turn_lease_events "
 "WHERE token = (SELECT seq FROM turn_lease_events WHERE launch_id = NEW.launch_id AND kind IN ('claim', 'handoff', "
 "'take', 'outside') ORDER BY seq DESC LIMIT 1) AND kind IN ('release', 'lapse')) OR (SELECT owner FROM "
 'turn_lease_events WHERE seq = NEW.token) IS NOT NEW.owner OR (SELECT owner_kind FROM turn_lease_events WHERE seq = '
 "NEW.token) IS NOT NEW.owner_kind)) OR (NEW.kind IN ('beat', 'outside_beat') AND NOT ((SELECT seq FROM "
 "turn_lease_events WHERE launch_id = NEW.launch_id AND kind IN ('claim', 'handoff', 'take', 'outside') ORDER BY seq "
 'DESC LIMIT 1) IS NOT NULL AND NOT EXISTS (SELECT 1 FROM turn_lease_events WHERE token = (SELECT seq FROM '
 "turn_lease_events WHERE launch_id = NEW.launch_id AND kind IN ('claim', 'handoff', 'take', 'outside') ORDER BY seq "
 "DESC LIMIT 1) AND kind IN ('release', 'lapse')) AND NEW.at < (SELECT MAX(until) FROM turn_lease_events WHERE seq = "
 "(SELECT seq FROM turn_lease_events WHERE launch_id = NEW.launch_id AND kind IN ('claim', 'handoff', 'take', "
 "'outside') ORDER BY seq DESC LIMIT 1) OR (token = (SELECT seq FROM turn_lease_events WHERE launch_id = "
 "NEW.launch_id AND kind IN ('claim', 'handoff', 'take', 'outside') ORDER BY seq DESC LIMIT 1) AND kind IN ('beat', "
 "'outside_beat'))))) OR (NEW.kind = 'beat' AND (SELECT kind FROM turn_lease_events WHERE seq = NEW.token) = "
 "'outside') OR (NEW.kind = 'outside_beat' AND (SELECT kind FROM turn_lease_events WHERE seq = NEW.token) != "
 "'outside') BEGIN SELECT RAISE(ABORT, 'a turn lease event takes an edge §5 does not allow'); END",
 'CREATE VIEW IF NOT EXISTS chat_timeline (thread_id, gseq, kind, source, ref_id, launch_id, status, body, at) AS '
 "SELECT m.thread_id, m.gseq, 'message', m.author_kind, m.message_id, m.launch_id, (SELECT o.state FROM "
 'chat_outbox_events o WHERE o.message_id = m.message_id ORDER BY o.seq DESC LIMIT 1), m.body, m.at FROM '
 "chat_messages m UNION ALL SELECT j.value, e.gseq, 'vendor_' || e.kind, e.source, e.vendor_event_key, e.launch_id, "
 'NULL, e.body, e.at FROM chat_events e JOIN launches l ON l.launch_id = e.launch_id, json_each(l.threads) j UNION '
 "ALL SELECT r.thread_id, r.gseq, CASE r.kind WHEN 'drop_receipt' THEN 'reply_receipt' WHEN 'sign' THEN 'reply_sign' "
 "WHEN 'answer' THEN 'reply_answer' WHEN 'know' THEN 'reply_know' END, 'levain', r.reply_id, NULL, NULL, r.body, "
 'r.at FROM replies r WHERE r.gseq IS NOT NULL AND r.thread_id IS NOT NULL UNION ALL SELECT b.thread_id, b.gseq, '
 "'envelope_bound', 'levain', b.envelope_id, NULL, NULL, NULL, b.at FROM envelope_binds b UNION ALL SELECT "
 "a.thread_id, a.gseq, 'attenuation_' || a.kind, a.by, a.envelope_id, NULL, NULL, a.value, a.at FROM "
 "envelope_attenuations a WHERE a.thread_id IS NOT NULL UNION ALL SELECT b.thread_id, a.gseq, 'attenuation_' || "
 'a.kind, a.by, a.envelope_id, NULL, NULL, a.value, a.at FROM envelope_attenuations a JOIN envelope_binds b ON '
 'b.envelope_id = a.envelope_id WHERE a.thread_id IS NULL')
ADDED_V0_11_25: tuple[tuple[str, str, str], ...] = ()

SCHEMA_MAIN_F75ACBC7: tuple[str, ...] = ('CREATE TABLE IF NOT EXISTS chat_messages (message_id TEXT PRIMARY KEY, gseq INTEGER NOT NULL UNIQUE, thread_id '
 'TEXT NOT NULL REFERENCES threads(thread_id), launch_id TEXT REFERENCES launches(launch_id), recipient TEXT NOT '
 "NULL CHECK (recipient IN ('coordinator', 'worker', 'thread')), author_kind TEXT NOT NULL CHECK (author_kind IN "
 "('operator', 'coordinator')), author_surface TEXT NOT NULL CHECK (author_surface IN ('cockpit', 'phone', 'cli', "
 "'coordinator')), client_id TEXT NOT NULL, outbox_id TEXT NOT NULL UNIQUE, mode TEXT NOT NULL CHECK (mode IN "
 "('queue', 'steer')), expected_turn_id TEXT, owner_epoch INTEGER, body TEXT NOT NULL, body_sha256 TEXT NOT NULL, "
 'body_norm_sha256 TEXT NOT NULL, attachments TEXT NOT NULL, draft_ref TEXT, at TEXT NOT NULL, CHECK ((recipient = '
 "'worker') = (launch_id IS NOT NULL)), CHECK ((author_kind = 'coordinator') = (author_surface = 'coordinator')), "
 "CHECK (author_kind = 'operator' OR (mode = 'queue' AND owner_epoch IS NULL)), CHECK ((mode = 'steer') = "
 "(expected_turn_id IS NOT NULL)), CHECK (mode = 'queue' OR recipient = 'worker'), CHECK (length(body) <= 20000))",
 "CREATE TRIGGER IF NOT EXISTS chat_messages_frozen BEFORE UPDATE ON chat_messages BEGIN SELECT RAISE(ABORT, 'a chat "
 "message is append-only'); END",
 "CREATE TRIGGER IF NOT EXISTS chat_messages_kept BEFORE DELETE ON chat_messages BEGIN SELECT RAISE(ABORT, 'a chat "
 "message is append-only'); END",
 'CREATE TRIGGER IF NOT EXISTS chat_messages_no_replace BEFORE INSERT ON chat_messages WHEN EXISTS (SELECT 1 FROM '
 'chat_messages WHERE message_id = NEW.message_id OR outbox_id = NEW.outbox_id OR gseq = NEW.gseq OR rowid = '
 "NEW.rowid) BEGIN SELECT RAISE(ABORT, 'a chat message is never replaced'); END",
 'CREATE TRIGGER IF NOT EXISTS chat_messages_worker BEFORE INSERT ON chat_messages WHEN NEW.launch_id IS NOT NULL '
 'AND NOT EXISTS (SELECT 1 FROM launches l, json_each(l.threads) j WHERE l.launch_id = NEW.launch_id AND l.role = '
 "'worker' AND j.value = NEW.thread_id) BEGIN SELECT RAISE(ABORT, 'a chat message to a worker names a worker launch "
 "of its thread'); END",
 'CREATE TABLE IF NOT EXISTS chat_outbox_events (message_id TEXT NOT NULL REFERENCES chat_messages(message_id), seq '
 "INTEGER NOT NULL, gseq INTEGER NOT NULL UNIQUE, state TEXT NOT NULL CHECK (state IN ('refused', 'pending', "
 "'attempted', 'accepted', 'ambiguous', 'not_delivered', 'cancelled')), op_id TEXT, vendor_anchor TEXT, "
 "vendor_turn_id TEXT, vendor_item_id TEXT, cause TEXT NOT NULL, by TEXT NOT NULL CHECK (by IN ('service', "
 "'operator')), at TEXT NOT NULL, PRIMARY KEY (message_id, seq), CHECK (state <> 'attempted' OR (op_id IS NOT NULL "
 "AND vendor_anchor IS NOT NULL)), CHECK (state = 'attempted' OR (op_id IS NULL AND vendor_anchor IS NULL)), CHECK "
 "(state IN ('accepted', 'ambiguous', 'not_delivered') OR (vendor_turn_id IS NULL AND vendor_item_id IS NULL)))",
 'CREATE UNIQUE INDEX IF NOT EXISTS chat_outbox_one_attempt ON chat_outbox_events (message_id) WHERE state = '
 "'attempted'",
 'CREATE TRIGGER IF NOT EXISTS chat_outbox_events_frozen BEFORE UPDATE ON chat_outbox_events BEGIN SELECT '
 "RAISE(ABORT, 'a chat outbox event is append-only'); END",
 'CREATE TRIGGER IF NOT EXISTS chat_outbox_events_kept BEFORE DELETE ON chat_outbox_events BEGIN SELECT RAISE(ABORT, '
 "'a chat outbox event is append-only'); END",
 'CREATE TRIGGER IF NOT EXISTS chat_outbox_events_next BEFORE INSERT ON chat_outbox_events WHEN NEW.seq != '
 'COALESCE((SELECT MAX(seq) FROM chat_outbox_events WHERE message_id = NEW.message_id), 0) + 1 OR EXISTS (SELECT 1 '
 "FROM chat_outbox_events WHERE rowid = NEW.rowid OR gseq = NEW.gseq) BEGIN SELECT RAISE(ABORT, 'an outbox event is "
 "the next seq of its message'); END",
 'CREATE TRIGGER IF NOT EXISTS chat_outbox_events_edges BEFORE INSERT ON chat_outbox_events WHEN ((SELECT state FROM '
 'chat_outbox_events WHERE message_id = NEW.message_id ORDER BY seq DESC LIMIT 1) IS NULL AND NEW.state NOT IN '
 "('refused', 'pending')) OR ((SELECT state FROM chat_outbox_events WHERE message_id = NEW.message_id ORDER BY seq "
 "DESC LIMIT 1) = 'pending' AND NEW.state NOT IN ('attempted', 'cancelled', 'not_delivered')) OR ((SELECT state FROM "
 "chat_outbox_events WHERE message_id = NEW.message_id ORDER BY seq DESC LIMIT 1) = 'attempted' AND NEW.state NOT IN "
 "('accepted', 'ambiguous', 'not_delivered')) OR ((SELECT state FROM chat_outbox_events WHERE message_id = "
 "NEW.message_id ORDER BY seq DESC LIMIT 1) = 'ambiguous' AND NEW.state NOT IN ('accepted', 'not_delivered')) OR "
 "(SELECT state FROM chat_outbox_events WHERE message_id = NEW.message_id ORDER BY seq DESC LIMIT 1) IN ('accepted', "
 "'refused', 'cancelled', 'not_delivered') OR (NEW.by = 'operator' AND (SELECT state FROM chat_outbox_events WHERE "
 "message_id = NEW.message_id ORDER BY seq DESC LIMIT 1) IS NOT 'ambiguous') BEGIN SELECT RAISE(ABORT, 'an outbox "
 "event takes an edge §2.2 does not allow'); END",
 'CREATE TABLE IF NOT EXISTS chat_events (launch_id TEXT NOT NULL REFERENCES launches(launch_id), vendor_event_key '
 'TEXT NOT NULL, gseq INTEGER NOT NULL UNIQUE, vendor_pos TEXT NOT NULL, kind TEXT NOT NULL CHECK (kind IN '
 "('turn_started', 'turn_ended', 'user_item', 'agent_text', 'tool_call', 'tool_result', 'approval_request', "
 "'approval_resolved', 'error', 'gap')), vendor_turn_id TEXT, call_id TEXT, body TEXT NOT NULL, body_sha256 TEXT NOT "
 'NULL, body_norm_sha256 TEXT NOT NULL, truncated INTEGER NOT NULL CHECK (truncated IN (0, 1)), backfill INTEGER NOT '
 "NULL CHECK (backfill IN (0, 1)), source TEXT NOT NULL CHECK (source = 'vendor'), at TEXT NOT NULL, PRIMARY KEY "
 '(launch_id, vendor_event_key), CHECK (length(CAST(body AS BLOB)) <= 16384), CHECK (substr(vendor_event_key, 1, '
 "length(kind) + 1) = kind || ':' AND length(vendor_event_key) > length(kind) + 1))",
 "CREATE TRIGGER IF NOT EXISTS chat_events_frozen BEFORE UPDATE ON chat_events BEGIN SELECT RAISE(ABORT, 'a chat "
 "event is append-only'); END",
 "CREATE TRIGGER IF NOT EXISTS chat_events_kept BEFORE DELETE ON chat_events BEGIN SELECT RAISE(ABORT, 'a chat event "
 "is append-only'); END",
 'CREATE TRIGGER IF NOT EXISTS chat_events_no_replace BEFORE INSERT ON chat_events WHEN EXISTS (SELECT 1 FROM '
 'chat_events WHERE (launch_id = NEW.launch_id AND vendor_event_key = NEW.vendor_event_key) OR gseq = NEW.gseq OR '
 "rowid = NEW.rowid) BEGIN SELECT RAISE(ABORT, 'a chat event is never replaced'); END",
 'CREATE TABLE IF NOT EXISTS chat_cursors (launch_id TEXT NOT NULL REFERENCES launches(launch_id), seq INTEGER NOT '
 'NULL, gseq INTEGER NOT NULL UNIQUE, vendor_cursor TEXT, complete_through TEXT, gap INTEGER NOT NULL CHECK (gap IN '
 '(0, 1)), at TEXT NOT NULL, PRIMARY KEY (launch_id, seq))',
 "CREATE TRIGGER IF NOT EXISTS chat_cursors_frozen BEFORE UPDATE ON chat_cursors BEGIN SELECT RAISE(ABORT, 'a chat "
 "cursor is append-only'); END",
 "CREATE TRIGGER IF NOT EXISTS chat_cursors_kept BEFORE DELETE ON chat_cursors BEGIN SELECT RAISE(ABORT, 'a chat "
 "cursor is append-only'); END",
 'CREATE TRIGGER IF NOT EXISTS chat_cursors_next BEFORE INSERT ON chat_cursors WHEN NEW.seq != COALESCE((SELECT '
 'MAX(seq) FROM chat_cursors WHERE launch_id = NEW.launch_id), 0) + 1 OR EXISTS (SELECT 1 FROM chat_cursors WHERE '
 "rowid = NEW.rowid OR gseq = NEW.gseq) BEGIN SELECT RAISE(ABORT, 'a cursor is the next seq of its launch'); END",
 'CREATE TABLE IF NOT EXISTS chat_anomalies (launch_id TEXT NOT NULL REFERENCES launches(launch_id), seq INTEGER NOT '
 'NULL, gseq INTEGER NOT NULL UNIQUE, message_id TEXT REFERENCES chat_messages(message_id), vendor_event_key TEXT '
 "NOT NULL, kind TEXT NOT NULL CHECK (kind IN ('proof_after_terminal')), detail TEXT NOT NULL, at TEXT NOT NULL, "
 'PRIMARY KEY (launch_id, seq))',
 "CREATE TRIGGER IF NOT EXISTS chat_anomalies_frozen BEFORE UPDATE ON chat_anomalies BEGIN SELECT RAISE(ABORT, 'a "
 "chat anomaly is append-only'); END",
 "CREATE TRIGGER IF NOT EXISTS chat_anomalies_kept BEFORE DELETE ON chat_anomalies BEGIN SELECT RAISE(ABORT, 'a chat "
 "anomaly is append-only'); END",
 'CREATE TRIGGER IF NOT EXISTS chat_anomalies_next BEFORE INSERT ON chat_anomalies WHEN NEW.seq != COALESCE((SELECT '
 'MAX(seq) FROM chat_anomalies WHERE launch_id = NEW.launch_id), 0) + 1 OR EXISTS (SELECT 1 FROM chat_anomalies '
 "WHERE rowid = NEW.rowid OR gseq = NEW.gseq) BEGIN SELECT RAISE(ABORT, 'an anomaly is the next seq of its launch'); "
 'END',
 'CREATE TABLE IF NOT EXISTS chat_launch_checks (launch_id TEXT NOT NULL REFERENCES launches(launch_id), seq INTEGER '
 "NOT NULL, gseq INTEGER NOT NULL UNIQUE, kind TEXT NOT NULL CHECK (kind IN ('probing', 'live', 'not_live', "
 "'reenabled')), source TEXT NOT NULL CHECK (source IN ('activation', 'turn_start', 'send', 'result', 'operator') OR "
 "(source GLOB 'tripwire:?*' AND kind = 'not_live')), evidence_sha256 TEXT CHECK (evidence_sha256 IS NULL OR "
 "(length(evidence_sha256) = 71 AND substr(evidence_sha256, 1, 7) = 'sha256:' AND substr(evidence_sha256, 8) NOT "
 "GLOB '*[^0-9a-f]*')), spawn_nonce TEXT, detail TEXT NOT NULL, by TEXT NOT NULL CHECK (by IN ('service', "
 "'operator')), at TEXT NOT NULL, canary_verdict_id TEXT, containment_digest TEXT, PRIMARY KEY (launch_id, seq), "
 "CHECK ((kind = 'reenabled') = (by = 'operator')), CHECK ((kind = 'reenabled') = (source = 'operator')), CHECK "
 "(spawn_nonce IS NULL OR (source = 'activation' AND length(spawn_nonce) = 32 AND spawn_nonce NOT GLOB "
 "'*[^0-9a-f]*')))",
 'CREATE TRIGGER IF NOT EXISTS chat_launch_checks_frozen BEFORE UPDATE ON chat_launch_checks BEGIN SELECT '
 "RAISE(ABORT, 'a launch check is append-only'); END",
 'CREATE TRIGGER IF NOT EXISTS chat_launch_checks_kept BEFORE DELETE ON chat_launch_checks BEGIN SELECT RAISE(ABORT, '
 "'a launch check is append-only'); END",
 'CREATE TRIGGER IF NOT EXISTS chat_launch_checks_next BEFORE INSERT ON chat_launch_checks WHEN NEW.seq != '
 'COALESCE((SELECT MAX(seq) FROM chat_launch_checks WHERE launch_id = NEW.launch_id), 0) + 1 OR EXISTS (SELECT 1 '
 "FROM chat_launch_checks WHERE rowid = NEW.rowid OR gseq = NEW.gseq) BEGIN SELECT RAISE(ABORT, 'a launch check is "
 "the next seq of its launch'); END",
 'CREATE TRIGGER IF NOT EXISTS chat_launch_checks_reenable BEFORE INSERT ON chat_launch_checks WHEN NEW.kind = '
 "'reenabled' AND NOT ((SELECT kind FROM chat_launch_checks WHERE launch_id = NEW.launch_id AND kind IN ('probing', "
 "'live', 'not_live') ORDER BY seq DESC LIMIT 1) = 'live' AND EXISTS (SELECT 1 FROM chat_launch_checks WHERE "
 "launch_id = NEW.launch_id AND kind = 'not_live' AND gseq > COALESCE((SELECT MAX(gseq) FROM chat_launch_checks "
 "WHERE launch_id = NEW.launch_id AND kind = 'reenabled'), 0))) BEGIN SELECT RAISE(ABORT, 'a re-enable clears a "
 "latch whose latest probe read live'); END",
 'CREATE TRIGGER IF NOT EXISTS chat_launch_checks_canary BEFORE INSERT ON chat_launch_checks WHEN '
 '(NEW.canary_verdict_id IS NULL) != (NEW.containment_digest IS NULL) OR (NEW.canary_verdict_id IS NOT NULL AND NOT '
 "(NEW.source = 'activation' AND NEW.kind = 'live' AND NEW.spawn_nonce IS NOT NULL AND typeof(NEW.canary_verdict_id) "
 "= 'text' AND length(NEW.canary_verdict_id) = 64 AND NEW.canary_verdict_id NOT GLOB '*[^0-9a-f]*' AND "
 "typeof(NEW.containment_digest) = 'text' AND length(NEW.containment_digest) = 64 AND NEW.containment_digest NOT "
 "GLOB '*[^0-9a-f]*')) BEGIN SELECT RAISE(ABORT, 'a canary verdict id and a containment digest ride together, 64 "
 "lowercase hex each, on a live activation row that names its child'); END",
 'CREATE TABLE IF NOT EXISTS chat_echoes (launch_id TEXT NOT NULL REFERENCES launches(launch_id), vendor_event_key '
 'TEXT NOT NULL, message_id TEXT NOT NULL UNIQUE REFERENCES chat_messages(message_id), gseq INTEGER NOT NULL UNIQUE, '
 'basis TEXT NOT NULL, at TEXT NOT NULL, PRIMARY KEY (launch_id, vendor_event_key), FOREIGN KEY (launch_id, '
 'vendor_event_key) REFERENCES chat_events(launch_id, vendor_event_key))',
 "CREATE TRIGGER IF NOT EXISTS chat_echoes_frozen BEFORE UPDATE ON chat_echoes BEGIN SELECT RAISE(ABORT, 'a chat "
 "echo is append-only'); END",
 "CREATE TRIGGER IF NOT EXISTS chat_echoes_kept BEFORE DELETE ON chat_echoes BEGIN SELECT RAISE(ABORT, 'a chat echo "
 "is append-only'); END",
 'CREATE TRIGGER IF NOT EXISTS chat_echoes_no_replace BEFORE INSERT ON chat_echoes WHEN EXISTS (SELECT 1 FROM '
 'chat_echoes WHERE (launch_id = NEW.launch_id AND vendor_event_key = NEW.vendor_event_key) OR message_id = '
 "NEW.message_id OR gseq = NEW.gseq OR rowid = NEW.rowid) BEGIN SELECT RAISE(ABORT, 'a chat echo is never "
 "replaced'); END",
 'CREATE TABLE IF NOT EXISTS turn_lease_events (seq INTEGER PRIMARY KEY AUTOINCREMENT, gseq INTEGER NOT NULL UNIQUE, '
 "launch_id TEXT NOT NULL REFERENCES launches(launch_id), kind TEXT NOT NULL CHECK (kind IN ('claim', 'handoff', "
 "'take', 'outside', 'beat', 'outside_beat', 'release', 'lapse')), token INTEGER, owner TEXT NOT NULL, owner_kind "
 "TEXT NOT NULL CHECK (owner_kind IN ('client', 'terminal', 'vendor_app')), until TEXT, cause TEXT NOT NULL, by TEXT "
 "NOT NULL CHECK (by IN ('operator', 'service')), at TEXT NOT NULL, CHECK ((kind IN ('claim', 'handoff', 'take', "
 "'outside')) = (token IS NULL)), CHECK ((kind IN ('release', 'lapse')) = (until IS NULL)), CHECK ((owner_kind = "
 "'client' AND (owner GLOB 'cockpit:?*' OR owner GLOB 'phone:?*' OR owner GLOB 'cli:?*')) OR (owner_kind != 'client' "
 "AND owner = owner_kind)), CHECK ((kind IN ('claim', 'handoff', 'take', 'beat') AND by = 'operator' AND owner_kind "
 "= 'client') OR (kind IN ('outside', 'outside_beat') AND by = 'service' AND owner_kind != 'client') OR (kind = "
 "'lapse' AND by = 'service') OR kind = 'release'))",
 'CREATE TRIGGER IF NOT EXISTS turn_lease_events_frozen BEFORE UPDATE ON turn_lease_events BEGIN SELECT RAISE(ABORT, '
 "'a turn lease event is append-only'); END",
 'CREATE TRIGGER IF NOT EXISTS turn_lease_events_kept BEFORE DELETE ON turn_lease_events BEGIN SELECT RAISE(ABORT, '
 "'a turn lease event is append-only'); END",
 'CREATE TRIGGER IF NOT EXISTS turn_lease_events_no_replace BEFORE INSERT ON turn_lease_events WHEN EXISTS (SELECT 1 '
 "FROM turn_lease_events WHERE seq = NEW.seq OR gseq = NEW.gseq) BEGIN SELECT RAISE(ABORT, 'a turn lease event is "
 "never replaced'); END",
 'CREATE TRIGGER IF NOT EXISTS turn_lease_events_edges BEFORE INSERT ON turn_lease_events WHEN (NEW.kind IN '
 "('claim', 'handoff', 'take', 'outside', 'beat', 'outside_beat') AND NOT EXISTS (SELECT 1 FROM launches l WHERE "
 "l.launch_id = NEW.launch_id AND l.role = 'worker' AND (SELECT kind FROM launch_events e WHERE e.launch_id = "
 "l.launch_id ORDER BY seq DESC LIMIT 1) = 'active')) OR (NEW.kind = 'claim' AND ((SELECT seq FROM turn_lease_events "
 "WHERE launch_id = NEW.launch_id AND kind IN ('claim', 'handoff', 'take', 'outside') ORDER BY seq DESC LIMIT 1) IS "
 'NOT NULL AND NOT EXISTS (SELECT 1 FROM turn_lease_events WHERE token = (SELECT seq FROM turn_lease_events WHERE '
 "launch_id = NEW.launch_id AND kind IN ('claim', 'handoff', 'take', 'outside') ORDER BY seq DESC LIMIT 1) AND kind "
 "IN ('release', 'lapse')) AND NEW.at < (SELECT MAX(until) FROM turn_lease_events WHERE seq = (SELECT seq FROM "
 "turn_lease_events WHERE launch_id = NEW.launch_id AND kind IN ('claim', 'handoff', 'take', 'outside') ORDER BY seq "
 "DESC LIMIT 1) OR (token = (SELECT seq FROM turn_lease_events WHERE launch_id = NEW.launch_id AND kind IN ('claim', "
 "'handoff', 'take', 'outside') ORDER BY seq DESC LIMIT 1) AND kind IN ('beat', 'outside_beat'))))) OR (NEW.kind = "
 "'handoff' AND (NOT ((SELECT seq FROM turn_lease_events WHERE launch_id = NEW.launch_id AND kind IN ('claim', "
 "'handoff', 'take', 'outside') ORDER BY seq DESC LIMIT 1) IS NOT NULL AND NOT EXISTS (SELECT 1 FROM "
 'turn_lease_events WHERE token = (SELECT seq FROM turn_lease_events WHERE launch_id = NEW.launch_id AND kind IN '
 "('claim', 'handoff', 'take', 'outside') ORDER BY seq DESC LIMIT 1) AND kind IN ('release', 'lapse')) AND NEW.at < "
 '(SELECT MAX(until) FROM turn_lease_events WHERE seq = (SELECT seq FROM turn_lease_events WHERE launch_id = '
 "NEW.launch_id AND kind IN ('claim', 'handoff', 'take', 'outside') ORDER BY seq DESC LIMIT 1) OR (token = (SELECT "
 "seq FROM turn_lease_events WHERE launch_id = NEW.launch_id AND kind IN ('claim', 'handoff', 'take', 'outside') "
 "ORDER BY seq DESC LIMIT 1) AND kind IN ('beat', 'outside_beat')))) OR (SELECT owner_kind FROM turn_lease_events "
 "WHERE seq = (SELECT seq FROM turn_lease_events WHERE launch_id = NEW.launch_id AND kind IN ('claim', 'handoff', "
 "'take', 'outside') ORDER BY seq DESC LIMIT 1)) != 'client')) OR (NEW.kind NOT IN ('claim', 'handoff', 'take', "
 "'outside') AND (NEW.token IS NOT (SELECT seq FROM turn_lease_events WHERE launch_id = NEW.launch_id AND kind IN "
 "('claim', 'handoff', 'take', 'outside') ORDER BY seq DESC LIMIT 1) OR EXISTS (SELECT 1 FROM turn_lease_events "
 "WHERE token = (SELECT seq FROM turn_lease_events WHERE launch_id = NEW.launch_id AND kind IN ('claim', 'handoff', "
 "'take', 'outside') ORDER BY seq DESC LIMIT 1) AND kind IN ('release', 'lapse')) OR (SELECT owner FROM "
 'turn_lease_events WHERE seq = NEW.token) IS NOT NEW.owner OR (SELECT owner_kind FROM turn_lease_events WHERE seq = '
 "NEW.token) IS NOT NEW.owner_kind)) OR (NEW.kind IN ('beat', 'outside_beat') AND NOT ((SELECT seq FROM "
 "turn_lease_events WHERE launch_id = NEW.launch_id AND kind IN ('claim', 'handoff', 'take', 'outside') ORDER BY seq "
 'DESC LIMIT 1) IS NOT NULL AND NOT EXISTS (SELECT 1 FROM turn_lease_events WHERE token = (SELECT seq FROM '
 "turn_lease_events WHERE launch_id = NEW.launch_id AND kind IN ('claim', 'handoff', 'take', 'outside') ORDER BY seq "
 "DESC LIMIT 1) AND kind IN ('release', 'lapse')) AND NEW.at < (SELECT MAX(until) FROM turn_lease_events WHERE seq = "
 "(SELECT seq FROM turn_lease_events WHERE launch_id = NEW.launch_id AND kind IN ('claim', 'handoff', 'take', "
 "'outside') ORDER BY seq DESC LIMIT 1) OR (token = (SELECT seq FROM turn_lease_events WHERE launch_id = "
 "NEW.launch_id AND kind IN ('claim', 'handoff', 'take', 'outside') ORDER BY seq DESC LIMIT 1) AND kind IN ('beat', "
 "'outside_beat'))))) OR (NEW.kind = 'beat' AND (SELECT kind FROM turn_lease_events WHERE seq = NEW.token) = "
 "'outside') OR (NEW.kind = 'outside_beat' AND (SELECT kind FROM turn_lease_events WHERE seq = NEW.token) != "
 "'outside') BEGIN SELECT RAISE(ABORT, 'a turn lease event takes an edge §5 does not allow'); END",
 'CREATE VIEW IF NOT EXISTS chat_timeline (thread_id, gseq, kind, source, ref_id, launch_id, status, body, at, '
 "echo_of) AS SELECT m.thread_id, m.gseq, 'message', m.author_kind, m.message_id, m.launch_id, (SELECT o.state FROM "
 'chat_outbox_events o WHERE o.message_id = m.message_id ORDER BY o.seq DESC LIMIT 1), m.body, m.at, NULL FROM '
 "chat_messages m UNION ALL SELECT j.value, e.gseq, CASE WHEN x.message_id IS NULL THEN 'vendor_' || e.kind ELSE "
 "'vendor_echo' END, e.source, e.vendor_event_key, e.launch_id, NULL, e.body, e.at, x.message_id FROM chat_events e "
 'JOIN launches l ON l.launch_id = e.launch_id JOIN json_each(l.threads) j LEFT JOIN chat_echoes x ON x.launch_id = '
 'e.launch_id AND x.vendor_event_key = e.vendor_event_key UNION ALL SELECT r.thread_id, r.gseq, CASE r.kind WHEN '
 "'drop_receipt' THEN 'reply_receipt' WHEN 'sign' THEN 'reply_sign' WHEN 'answer' THEN 'reply_answer' WHEN 'know' "
 "THEN 'reply_know' END, 'levain', r.reply_id, NULL, NULL, r.body, r.at, NULL FROM replies r WHERE r.gseq IS NOT "
 "NULL AND r.thread_id IS NOT NULL UNION ALL SELECT b.thread_id, b.gseq, 'envelope_bound', 'levain', b.envelope_id, "
 "NULL, NULL, NULL, b.at, NULL FROM envelope_binds b UNION ALL SELECT a.thread_id, a.gseq, 'attenuation_' || a.kind, "
 'a.by, a.envelope_id, NULL, NULL, a.value, a.at, NULL FROM envelope_attenuations a WHERE a.thread_id IS NOT NULL '
 "UNION ALL SELECT b.thread_id, a.gseq, 'attenuation_' || a.kind, a.by, a.envelope_id, NULL, NULL, a.value, a.at, "
 'NULL FROM envelope_attenuations a JOIN envelope_binds b ON b.envelope_id = a.envelope_id WHERE a.thread_id IS NULL')
ADDED_MAIN_F75ACBC7: tuple[tuple[str, str, str], ...] = (('chat_launch_checks', 'canary_verdict_id', 'TEXT'), ('chat_launch_checks', 'containment_digest', 'TEXT'))

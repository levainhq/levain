"""levain.autonomic.schema3 — the ``levain-autonomic/3`` store: its schema, its opener and its close path.

The ``/3`` format is the autonomic store whose every terminal row is written with its gate receipt in ONE statement
(``INSERT INTO close_subject``), whose rows are never replaced or removed, and whose held, decision and
environment-fault counts are kept by the database itself (autogrant DESIGN rev 7.3 / 7.4 / 7.4b, A1c1). Every trigger
here keeps the name it had in the probed prototypes (levain project_memory ``1026-16/autogrant14/probes/rev73_schema.py``,
``1026-20/rev74/probes/rev74_schema.py``, ``1026-20/rev74b/probes/rev74b_schema.py``) [judged by lane a1c1, 10-26:
assembled from their line ranges; the changes are the tunables (R14-4), the meta triggers (O-74b-4) and the
environment-fault writer (R14-2)]; the A1c1 L3 round-1 fixes then added ``<t>_legacy_frozen``,
``effects_in_legacy_run`` and the ``holds.decisions`` generation, and made ``pendings_born`` a born-open rule [lane
a1c1d, 10-26].

This module builds a ``/3`` store FRESH. :class:`AutonomicStore3` creates one in an empty directory and opens one it
made; a ``/1`` or ``/2`` store is refused by its format (:class:`StoreNeedsUpgrade`) and left as it was: the upgrade of
an existing V store (I15) is not in this module. The DDL a fresh store gets is :func:`ddl_v3` then
:func:`triggers_v3` over V's own ``/2`` schema (``levain.autonomic.db``), the same functions an upgrade step runs, so a
fresh store and an upgraded one carry one fingerprint (R14-4).

The tunables (:data:`TUNABLES`: the held-count bound and the environment-fault bound) live in ``meta`` rows the
triggers read, so changing one changes no DDL; the opener writes the code's values on open (R14-4).

Stdlib only.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from levain.autonomic import db as vdb
from levain.autonomic.db import StoreFormatError
from levain.autonomic.journal import UNSIGNED_DECIDER
from levain.autonomic.posture import Posture
from levain.autonomic.durable_sqlite import open_durable, read_transaction, write_transaction
from levain.autonomic.durable_sqlite import rollback as _rollback

_log = logging.getLogger("levain.autonomic.schema3")

STORE_FORMAT_V3 = "levain-autonomic/3"
_FORMAT_PREFIX = "levain-autonomic/"
_FORMAT_DIGITS = 9

# R14-4: the tunables, by meta key. The triggers read the meta row (``_tune``); the opener writes these values.
HELD_LIMIT = 5             # I5: the delivery held the HELD_LIMIT-th time closes its subject `refused`
ENV_N = 3                  # R13-7 (head-ruled): faults in one episode before the subject is surfaced
ENV_SPAN_S = 3600          # R13-7 (head-ruled): the episode spans at least this many seconds
TUNABLES = {"held_limit": HELD_LIMIT, "env_fault_n": ENV_N, "env_fault_span_s": ENV_SPAN_S}


def _tune(key: str) -> str:
    # a tunable as the triggers read it; a missing row reads NULL and every comparison with it is false
    return f"(SELECT CAST(value AS INTEGER) FROM meta WHERE key = '{key}')"


_HELD_LIMIT_SQL = _tune("held_limit")
# I11: the reason a held count carries: a subject-specific fault, ``subject:<why>``. One rule, a GLOB: the held_born /
# held_move triggers apply it, and count_held asks SQLite to apply the same pattern before it writes (O-a1c1-7)
SUBJECT_REASON = "subject:?*"


def format_n(marker: object) -> int | None:
    """The ``n`` of a ``levain-autonomic/<n>`` marker (ASCII digits, from 1, no leading zero, at most
    :data:`_FORMAT_DIGITS` of them), else None. The bound comes before ``int()``, which raises ``ValueError`` on a
    digit string past Python's integer-string limit."""
    if not isinstance(marker, str) or not marker.startswith(_FORMAT_PREFIX):
        return None
    tail = marker[len(_FORMAT_PREFIX):]
    if not (0 < len(tail) <= _FORMAT_DIGITS and tail.isascii() and tail.isdigit() and tail[0] != "0"):
        return None
    return int(tail)

# ---- rev 7.3 (rev73_schema.py): tables, columns, helpers, triggers, the close path, reaper selections ----
CONFIRM_RUNGS = "('COOLING_OFF', 'CONFIRM', 'CONFIRM_ELEVATED')"   # Posture.needs_confirm, by name

V3_TABLES = (
    "CREATE TABLE IF NOT EXISTS gate_receipts (receipt_id TEXT PRIMARY KEY, "
    "subject TEXT NOT NULL UNIQUE CHECK (subject GLOB 'run:?*' OR subject GLOB 'effect:?*' "
    "OR subject GLOB 'pending:?*' OR subject GLOB 'request:?*'), record TEXT NOT NULL, "
    "projected INTEGER NOT NULL DEFAULT 0 CHECK (projected IN (0, 1)), seq INTEGER NOT NULL UNIQUE)",
    "CREATE TABLE IF NOT EXISTS manual_pendings (pending_id TEXT PRIMARY KEY, seal TEXT NOT NULL, "
    "state TEXT NOT NULL CHECK (state IN ('open', 'firing', 'terminal')), receipt_id TEXT, decision TEXT, "
    "seq INTEGER NOT NULL UNIQUE, CHECK ((state = 'terminal') = (receipt_id IS NOT NULL)), "
    "CHECK ((state = 'firing') <= (decision IS NOT NULL)))",
    # D72: the terminal row of a request subject (a cancel=False refusal); request_id = '<run_id>:<effect_id>'
    "CREATE TABLE IF NOT EXISTS requests (request_id TEXT PRIMARY KEY, reason TEXT NOT NULL, "
    "receipt_id TEXT NOT NULL)",
    # I5: how many times a live subject was held by a fault; at HELD_LIMIT it closes `refused`. O-p2-1 R2-1: `attempt`
    # is the link's latest attempt whose not_started this count recorded (NULL: a count with no effect attempt)
    "CREATE TABLE IF NOT EXISTS held_counts (subject TEXT PRIMARY KEY, n INTEGER NOT NULL CHECK (n >= 1), "
    "last_reason TEXT NOT NULL, mark INTEGER NOT NULL, attempt INTEGER)",
    # I11: the subject a close_subject statement is closing (empty outside one statement; INVARIANT closing_left)
    "CREATE TABLE IF NOT EXISTS closing (subject TEXT PRIMARY KEY)",
    # I11: the one way a receipt row is written: receipt + its subject's terminal row, in one statement
    "CREATE VIEW IF NOT EXISTS close_subject AS SELECT NULL AS receipt_id, NULL AS subject, NULL AS record, "
    "NULL AS seq, NULL AS state, NULL AS reason, NULL AS result",
)
# I4 / D78: the terminal_state a receipt may carry for each terminal row (the record's action_face.terminal_state)
TERMINAL_MODES = {
    ("effects", "done"): ("fired",),
    ("effects", "unknown"): ("outcome_unknown",),
    # O-p2-1 section 3: an attempt that did not start; its receipt claims no effect fired (the frozen receipt
    # contract's terminal_state set is not changed)
    ("effects", "not_started"): ("refused",),
    ("cancels", None): ("refused", "killed", "timed_out"),
    ("manual_pendings", "terminal"): ("fired", "refused", "killed", "timed_out", "outcome_unknown"),
    ("requests", None): ("refused",),
}
ALL_MODES = ("fired", "refused", "killed", "timed_out", "outcome_unknown")

# (table, column, type): added in place, as db.py's _ADDED_COLUMNS does
V3_COLUMNS = (("runs", "seal", "TEXT"), ("runs", "started_at", "TEXT"), ("effects", "seal", "TEXT"),
              ("cancels", "receipt_id", "TEXT"), ("holds", "trigger_event", "TEXT"),
              # A1-R2: the hold's decision generation, the number of decisions it ever took (holds_move steps it
              # in the statement that decides; a hold_decisions row carries it)
              ("holds", "decisions", "INTEGER NOT NULL DEFAULT 0 CHECK (decisions >= 0)"))

# D70: the columns the whole-row triggers below were written against (a test pins PRAGMA table_info to these)
KNOWN_COLUMNS = {
    "runs": ("run_id", "binding_id", "generation", "seal", "started_at"),
    "effects": ("run_id", "effect_id", "digest", "pid", "state", "result", "receipt_id", "seal"),
    "cancels": ("run_id", "reason", "receipt_id"),
    "manual_pendings": ("pending_id", "seal", "state", "receipt_id", "decision", "seq"),
    "requests": ("request_id", "reason", "receipt_id"),
    "gate_receipts": ("receipt_id", "subject", "record", "projected", "seq"),
    "holds": ("hold_id", "run_id", "effect_id", "binding_id", "digest", "at", "pending", "pending_id", "chain",
              "chained", "decided", "decided_by", "decided_posture", "seq", "signer", "signature", "challenge", "fence",
              "trigger_event", "decisions"),
    "held_counts": ("subject", "n", "last_reason", "mark", "attempt"),
    "closing": ("subject",),
}
_HOLD_IDENT = ("hold_id", "run_id", "effect_id", "binding_id", "digest", "at", "pending", "pending_id", "chain",
               "chained", "seq", "trigger_event")
_HOLD_DECISION = ("decided_by", "decided_posture", "signer", "signature", "challenge", "fence")

def _json_eq(a: str, b: str) -> str:
    # structural equality of two JSON texts (key order and string escapes do not matter); both NULL is equal
    def sub(x, y):
        return (f"NOT EXISTS (SELECT fullkey, type, atom FROM json_tree({x}) "
                f"EXCEPT SELECT fullkey, type, atom FROM json_tree({y}))")
    return (f"((({a}) IS NULL AND ({b}) IS NULL) OR (({a}) IS NOT NULL AND ({b}) IS NOT NULL "
            f"AND {sub(a, b)} AND {sub(b, a)}))")


def _mode(rid: str, modes: tuple[str, ...]) -> str:
    # the receipt `rid` carries one of `modes` as its terminal_state
    listed = ", ".join(f"'{m}'" for m in modes)
    return (f"EXISTS (SELECT 1 FROM gate_receipts g WHERE g.receipt_id = {rid} AND "
            f"json_extract(g.record, '$.action_face.terminal_state') IN ({listed}))")


def _has(subject_sql: str, rid: str) -> str:
    # a receipt row with this id FOR this subject
    return f"EXISTS (SELECT 1 FROM gate_receipts g WHERE g.receipt_id = {rid} AND g.subject = {subject_sql})"


def _same(*cols: str) -> str:
    return " AND ".join(f"NEW.{c} IS OLD.{c}" for c in cols)


def _guard_complete(seal_sql: str) -> str:
    # D68: some guard in the sealed floor has rationale + kill_predicate + kill_drill, all non-null
    return (f"EXISTS (SELECT 1 FROM json_each({seal_sql}, '$.guard') g WHERE "
            "json_type(g.value, '$.rationale') = 'text' AND trim(json_extract(g.value, '$.rationale')) != '' "
            "AND json_type(g.value, '$.kill_predicate') = 'object' AND json_type(g.value, '$.kill_drill') = 'object')")


def _link_key(a: str) -> str:
    # a link's key, '<run_id>:<effect_id>', of the effects / holds row aliased `a` (run ids carry no ':')
    return f"{a}.run_id || ':' || {a}.effect_id"


def _attempt_key(a: str) -> str:
    # O-p2-1 section 4: an attempt's key: the link's key, then '#<n>' for attempt n >= 2 only (attempt 1 keeps the
    # subject every receipt and V row already has)
    return f"{_link_key(a)} || CASE WHEN {a}.attempt > 1 THEN '#' || {a}.attempt ELSE '' END"


def _effect_subj(a: str) -> str:
    # O-p2-1 section 4: THE spelling of an effects row's receipt subject in SQL (effect_subject is the Python twin)
    return "'effect:' || " + _attempt_key(a)


def _link_request(a: str) -> str:
    # the request subject of the link an effects / holds row aliased `a` belongs to (one per link, every attempt)
    return "'request:' || " + _link_key(a)


def _run_requests(run: str) -> str:
    # the prefix every request subject of the run `run` (an SQL expression) starts with
    return f"'request:' || {run} || ':'"


_RUN = "'run:' || NEW.run_id"
_EFF = _effect_subj("NEW")
_PEN = "'pending:' || NEW.pending_id"
_REQ = "'request:' || NEW.request_id"
# the run a receipt's subject belongs to (run ids carry no ':'); NULL for a pending
_RUN_OF = ("(CASE WHEN NEW.subject GLOB 'run:*' THEN substr(NEW.subject, 5) "
           "WHEN NEW.subject GLOB 'effect:*' THEN substr(NEW.subject, 8, instr(substr(NEW.subject, 8), ':') - 1) "
           "WHEN NEW.subject GLOB 'request:*' THEN substr(NEW.subject, 9, instr(substr(NEW.subject, 9), ':') - 1) "
           "END)")


def _dup_keys(doc: str) -> str:
    # I14: some object member key occurs twice under one parent (json_tree lists every occurrence)
    return (f"EXISTS (SELECT 1 FROM json_tree({doc}) t WHERE t.key IS NOT NULL AND typeof(t.key) = 'text' "
            "GROUP BY t.parent, t.key HAVING COUNT(*) > 1)")


def _run_of_request(subj: str) -> str:
    return f"substr({subj}, 9, instr(substr({subj}, 9), ':') - 1)"


# ---- rev 7.4 R13-6 (rev74_schema.py): monotone progress (replaces rev 7.3's) ----
def _progress(subj: str) -> str:
    # R13-6: the subject's DB-visible progress, NEVER decreasing: effects and holds are never removed once past intent
    # (effects_final_d, holds_final_d; and an effect row of any state is never removed: effects_d) and
    # hold_decisions is append-only. request:<run>:<eid> -> its run's effects + holds + decisions ever taken;
    # pending:<pid> -> its manual pending's state rank, or the number of decisions its hold ever took.
    # R14-1: a decision counts only when someone took it: the silence default's (UNSIGNED_DECIDER) is not progress,
    # or a coverage answer that flaps between the sweep's read and the admission's (approve, then reopen, every
    # sweep) restarts the held count every cycle and the subject never reaches HELD_LIMIT
    # O-p2-1 section 5: a link's attempts are ONE unit of progress (COUNT DISTINCT effect_id), or every not_started
    # attempt would read as progress and restart the count that bounds them
    r = _run_of_request(subj)
    taken = f"by IS NOT '{UNSIGNED_DECIDER}'"
    return (f"(CASE WHEN {subj} GLOB 'request:?*' THEN "
            f"(SELECT COUNT(DISTINCT effect_id) FROM effects WHERE run_id = {r}) "
            f"+ (SELECT COUNT(*) FROM holds WHERE run_id = {r}) "
            f"+ (SELECT COUNT(*) FROM hold_decisions WHERE run_id = {r} AND {taken}) "
            f"WHEN {subj} GLOB 'pending:?*' THEN IFNULL((SELECT CASE state WHEN 'open' THEN 0 WHEN 'firing' THEN 1 "
            f"ELSE 2 END FROM manual_pendings WHERE 'pending:' || pending_id = {subj}), "
            f"(SELECT COUNT(*) FROM hold_decisions WHERE 'pending:' || pending_id = {subj} AND {taken})) "
            "END)")



def _closed(subj: str) -> str:
    # I11: the subject is past (LOST), read from TERMINAL rows, never from a receipt's existence. O-p2-1 section 5: a
    # link whose only attempts are not_started is still a live request (an attempt that did not start ends nothing)
    r = _run_of_request(subj)
    return (f"(CASE WHEN {subj} GLOB 'request:?*' THEN EXISTS (SELECT 1 FROM requests WHERE 'request:' || request_id "
            f"= {subj}) OR EXISTS (SELECT 1 FROM cancels WHERE run_id = {r}) OR EXISTS (SELECT 1 FROM effects e "
            f"WHERE {_link_request('e')} = {subj} AND e.state != 'not_started') "
            f"WHEN {subj} GLOB 'pending:?*' THEN EXISTS (SELECT 1 FROM manual_pendings WHERE 'pending:' || pending_id "
            f"= {subj} AND state = 'terminal') OR EXISTS (SELECT 1 FROM holds h WHERE 'pending:' || h.pending_id = "
            f"{subj} AND (h.decided IS NOT NULL OR EXISTS (SELECT 1 FROM cancels c WHERE c.run_id = h.run_id))) "
            "ELSE 1 END)")


_KILLED_RUN = ("(NEW.subject GLOB 'run:?*' AND json_extract(NEW.record, '$.action_face.terminal_state') IS 'killed')")


def _grant_shape(doc: str, path: str) -> str:
    # D95 (the effects_grant_shape checks, I13, for a grant named inside a JSON document): an object, born an integer,
    # terms_text a graduation record of this grant's id, incarnation text prefixed by born
    return (f"(json_type({doc}, '{path}') = 'object' AND json_type({doc}, '{path}.born') = 'integer' "
            f"AND json_type({doc}, '{path}.terms_text') = 'text' AND json_valid(json_extract({doc}, '{path}.terms_text')) "
            f"AND json_extract(json_extract({doc}, '{path}.terms_text'), '$.kind') IS 'graduation' "
            f"AND json_extract(json_extract({doc}, '{path}.terms_text'), '$.id') IS json_extract({doc}, '{path}.id') "
            f"AND json_type({doc}, '{path}.incarnation') = 'text' "
            f"AND json_extract({doc}, '{path}.incarnation') GLOB (json_extract({doc}, '{path}.born') || ':?*') "
            ")")


def _abort(name: str, when: str, why: str, on: str) -> str:
    return f"CREATE TRIGGER IF NOT EXISTS {name} {on} WHEN {when} BEGIN SELECT RAISE(ABORT, '{why}'); END"


def _rank(seal_sql: str) -> str:
    # 10.Q2 / R2-3: a seal's posture as its Posture rank, emitted from the enum; no ELSE, so an unknown name is NULL
    whens = " ".join(f"WHEN '{p.name}' THEN {int(p)}" for p in Posture)
    return f"(CASE json_extract({seal_sql}, '$.posture') {whens} END)"


def _absent(doc: str, path: str) -> str:
    # R4-2: a missing key and a JSON null are one absence (the effects_grant_shape idiom)
    return f"IFNULL(json_type({doc}, '{path}'), 'null') = 'null'"


# O-p2-1: the link's previous attempt P of an insert NEW (attempt n - 1)
_PREV = ("FROM effects p WHERE p.run_id = NEW.run_id AND p.effect_id = NEW.effect_id "
         "AND p.attempt = NEW.attempt - 1")
_LINK_HELD = "EXISTS (SELECT 1 FROM holds h WHERE h.run_id = NEW.run_id AND h.effect_id = NEW.effect_id)"
# R3-2 as amended by R4-2: with a hold on the link, the seal's $.decision is the hold's CURRENT decision. Every field is
# type-checked first and compared with `=` to a column checked IS NOT NULL (R3-1); a NULL decided_posture is matched
# by an explicit absence, witnessed by the hold row
_HELD_DECISION = (
    "EXISTS (SELECT 1 FROM holds h WHERE h.run_id = NEW.run_id AND h.effect_id = NEW.effect_id AND h.decided IS 1 "
    "AND h.decisions IS NOT NULL AND h.decided_by IS NOT NULL "
    "AND json_type(NEW.seal, '$.decision.generation') = 'integer' "
    "AND json_extract(NEW.seal, '$.decision.generation') = h.decisions "
    "AND json_type(NEW.seal, '$.decision.by') = 'text' AND json_extract(NEW.seal, '$.decision.by') = h.decided_by "
    f"AND ((h.decided_posture IS NULL AND {_absent('NEW.seal', '$.decision.posture')}) "
    "OR (h.decided_posture IS NOT NULL AND json_type(NEW.seal, '$.decision.posture') = 'text' "
    "AND json_extract(NEW.seal, '$.decision.posture') = h.decided_posture)))")
# R3-1 as amended by R4-2: with no hold, the retry re-uses P's cover (both grant-less and cover-less, or the same cover
# text) at a posture rank no lower than P's (both ranks known)
_HOLDLESS_AUTHORITY = (
    f"EXISTS (SELECT 1 {_PREV} AND (({_absent('NEW.seal', '$.grant')} AND {_absent('p.seal', '$.grant')} "
    f"AND {_absent('NEW.seal', '$.cover')} AND {_absent('p.seal', '$.cover')}) "
    "OR (json_type(NEW.seal, '$.cover') = 'text' AND json_type(p.seal, '$.cover') = 'text' "
    "AND json_extract(NEW.seal, '$.cover') = json_extract(p.seal, '$.cover'))) "
    "AND json_type(NEW.seal, '$.posture') = 'text' AND json_type(p.seal, '$.posture') = 'text' "
    f"AND {_rank('NEW.seal')} IS NOT NULL AND {_rank('p.seal')} IS NOT NULL "
    f"AND {_rank('NEW.seal')} >= {_rank('p.seal')})")

RETRY_TRIGGERS = (
    # section 5: attempt 1 opens a link (no row yet); attempt n > 1 follows the link's LATEST row, attempt n - 1,
    # ended not_started, of the same digest (a retry after fired / outcome_unknown, or of another payload, is refused)
    _abort("effects_attempt", "NOT IFNULL(CASE WHEN NEW.attempt = 1 THEN NOT EXISTS (SELECT 1 FROM effects e "
           "WHERE e.run_id = NEW.run_id AND e.effect_id = NEW.effect_id) ELSE "
           f"EXISTS (SELECT 1 {_PREV} AND p.state = 'not_started' AND p.digest = NEW.digest) "
           "AND NOT EXISTS (SELECT 1 FROM effects q WHERE q.run_id = NEW.run_id AND q.effect_id = NEW.effect_id "
           "AND q.attempt >= NEW.attempt) END, 0)",
           "O-p2-1: attempt 1 opens a link; attempt n follows attempt n-1 not_started, same digest",
           "BEFORE INSERT ON effects"),
    # A1 + R2-1: the predecessor's not_started was COUNTED (the link's request count names attempt n - 1) and the
    # count is under the bound
    _abort("effects_attempt_counted", "NEW.attempt > 1 AND NOT IFNULL(EXISTS (SELECT 1 FROM held_counts h "
           f"WHERE h.subject = {_link_request('NEW')} AND h.attempt IS NOT NULL AND h.attempt = NEW.attempt - 1 "
           f"AND h.n < {_HELD_LIMIT_SQL}), 0)",
           "R2-1: a retry follows the count of its predecessor, under the held bound", "BEFORE INSERT ON effects"),
    # 10.Q2(a) + R2-2 + R3-1 + R3-2 + R4-2: who may retry
    _abort("effects_attempt_authority", f"NEW.attempt > 1 AND NOT IFNULL(({_LINK_HELD} AND {_HELD_DECISION}) "
           f"OR (NOT {_LINK_HELD} AND {_HOLDLESS_AUTHORITY}), 0)",
           "R3-1/R3-2: a retry carries the current decision of its link, or the cover of its predecessor at no lower posture",
           "BEFORE INSERT ON effects"),
)


V3_TRIGGERS = (
    # ---- D69: no row is ever replaced (every PK / UNIQUE column, checked before the conflict can resolve)
    _abort("runs_new", "EXISTS (SELECT 1 FROM runs WHERE run_id = NEW.run_id)",
           "D69: a row is never replaced", "BEFORE INSERT ON runs"),
    _abort("effects_new", "EXISTS (SELECT 1 FROM effects WHERE run_id = NEW.run_id AND effect_id = NEW.effect_id "
           "AND attempt = NEW.attempt)", "D69: a row is never replaced", "BEFORE INSERT ON effects"),
    # O-p2-1: the retry rules (legality, the counted witness, authority)
    *RETRY_TRIGGERS,
    _abort("cancels_new", "EXISTS (SELECT 1 FROM cancels WHERE run_id = NEW.run_id)",
           "D69: a row is never replaced", "BEFORE INSERT ON cancels"),
    _abort("pendings_new", "EXISTS (SELECT 1 FROM manual_pendings WHERE pending_id = NEW.pending_id "
           "OR seq = NEW.seq)", "D69: a row is never replaced", "BEFORE INSERT ON manual_pendings"),
    _abort("requests_new", "EXISTS (SELECT 1 FROM requests WHERE request_id = NEW.request_id)",
           "D69: a row is never replaced", "BEFORE INSERT ON requests"),
    _abort("receipts_new", "EXISTS (SELECT 1 FROM gate_receipts WHERE receipt_id = NEW.receipt_id "
           "OR subject = NEW.subject OR seq = NEW.seq)", "D69: a row is never replaced",
           "BEFORE INSERT ON gate_receipts"),
    # ---- runs: born sealed (D56) with its class and guard (D68); started once (D71); otherwise immutable (D70)
    _abort("runs_sealed", "NEW.seal IS NULL OR NOT json_valid(NEW.seal) "
           "OR IFNULL(json_type(NEW.seal, '$.confirm_class'), 'absent') NOT IN ('true', 'false') "
           "OR json_type(NEW.seal, '$.guard') IS NOT 'array' OR NEW.started_at IS NOT NULL "
           "OR json_type(NEW.seal, '$.links') IS NOT 'integer' OR json_extract(NEW.seal, '$.links') < 1 "
           "OR instr(NEW.run_id, ':') > 0 OR instr(NEW.run_id, '#') > 0",
           "D56/D68/D71/I2: a run is born sealed (class, guard, links) and unstarted", "BEFORE INSERT ON runs"),
    _abort("runs_guarded", f"json_extract(NEW.seal, '$.confirm_class') = 1 AND NOT {_guard_complete('NEW.seal')}",
           "D68: a confirm-class run is admitted only with its complete guard", "BEFORE INSERT ON runs"),
    _abort("runs_u", "NOT (" + _same("run_id", "binding_id", "generation", "seal")
           + " AND (NEW.started_at IS OLD.started_at OR (OLD.started_at IS NULL AND NEW.started_at IS NOT NULL)))",
           "D70/D71: a run is immutable but for its one start", "BEFORE UPDATE ON runs"),
    _abort("runs_d", "1", "D70: a run is never removed", "BEFORE DELETE ON runs"),
    # ---- cancels (the run's terminal): only with its own receipt, never changed or removed
    _abort("cancels_receipted", f"NEW.receipt_id IS NULL OR NOT {_has(_RUN, 'NEW.receipt_id')}",
           "D54: a run closes only with its receipt", "BEFORE INSERT ON cancels"),
    _abort("cancels_mode", f"NOT {_mode('NEW.receipt_id', TERMINAL_MODES[('cancels', None)])}",
           "I4: a run closes refused, killed or timed_out", "BEFORE INSERT ON cancels"),
    # I2: a run whose sealed links are all terminal is complete: nothing may close it afterward
    _abort("cancels_not_completed", "(SELECT COUNT(*) FROM effects e WHERE e.run_id = NEW.run_id "
           "AND e.state IN ('done', 'unknown')) >= (SELECT json_extract(r.seal, '$.links') FROM runs r "
           "WHERE r.run_id = NEW.run_id)", "I2: a completed run is never closed", "BEFORE INSERT ON cancels"),
    _abort("cancels_final_u", "1", "D54: a closed run stays closed", "BEFORE UPDATE ON cancels"),
    _abort("cancels_final_d", "1", "D54: a closed run stays closed", "BEFORE DELETE ON cancels"),
    # ---- effects: born intent, sealed, in a started run (D71), carrying the run's guard (D68); a confirm-class
    # effect no human decided needs that guard complete (D67/D68 backstop); identity and seal immutable (D70);
    # done/unknown only with its own receipt; an ended row is final (D60: no legacy carve-out)
    _abort("effects_born_intent", "NEW.state != 'intent' OR NEW.seal IS NULL OR NOT json_valid(NEW.seal) "
           "OR NEW.receipt_id IS NOT NULL OR instr(NEW.effect_id, ':') > 0",
           "D54/D56: an effect is born intent, sealed, unreceipted", "BEFORE INSERT ON effects"),
    _abort("effects_in_started_run", "(SELECT started_at FROM runs WHERE run_id = NEW.run_id) IS NULL",
           "D71: an effect begins only in a started run", "BEFORE INSERT ON effects"),
    _abort("effects_in_live_run", "EXISTS (SELECT 1 FROM cancels c WHERE c.run_id = NEW.run_id)",
           "I2: no effect begins in a closed run", "BEFORE INSERT ON effects"),
    # I1: a sealed grant is the signed terms its incarnation names (the hash of V graduation_incarnation)
    _abort("effects_grant_shape", "IFNULL(json_type(NEW.seal, '$.grant'), 'null') != 'null' AND NOT ("
           "json_type(NEW.seal, '$.grant') = 'object' AND json_type(NEW.seal, '$.grant.born') = 'integer' "
           "AND json_type(NEW.seal, '$.grant.terms_text') = 'text' "
           "AND json_valid(json_extract(NEW.seal, '$.grant.terms_text')) "
           "AND json_extract(json_extract(NEW.seal, '$.grant.terms_text'), '$.kind') IS 'graduation' "
           "AND json_extract(json_extract(NEW.seal, '$.grant.terms_text'), '$.id') IS json_extract(NEW.seal, '$.grant.id') "
           "AND json_type(NEW.seal, '$.grant.incarnation') = 'text' "
           "AND json_extract(NEW.seal, '$.grant.incarnation') GLOB (json_extract(NEW.seal, '$.grant.born') || ':?*') "
           "AND json_extract(NEW.seal, '$.cover') GLOB (json_extract(NEW.seal, '$.grant.incarnation') || '@?*'))",
           "I1/I13: a sealed grant has the shape of signed terms (the hash is checked by the fire check)",
           "BEFORE INSERT ON effects"),
    # I14: an effect id is a chain position: 'link-<n>', no leading zero, 0 <= n < the run's sealed links
    _abort("effects_canonical_id", "NOT IFNULL(NEW.effect_id GLOB 'link-[0-9]*' "
           "AND substr(NEW.effect_id, 6) NOT GLOB '*[^0-9]*' AND length(NEW.effect_id) BETWEEN 6 AND 14 "
           "AND (NEW.effect_id = 'link-0' OR substr(NEW.effect_id, 6, 1) != '0') "
           "AND CAST(substr(NEW.effect_id, 6) AS INTEGER) < (SELECT json_extract(seal, '$.links') FROM runs "
           "WHERE run_id = NEW.run_id), 0)",
           "I14: an effect id is link-<n> with 0 <= n < links", "BEFORE INSERT ON effects"),
    # D98: the effective guard is sealed as TWO fields, floor and grant_guard (the kill evaluates their union); the
    # seal carries no single concatenated `guard`
    _abort("effects_seal_guards", "json_type(NEW.seal, '$.floor') IS NOT 'array' "
           "OR json_type(NEW.seal, '$.grant_guard') IS NOT 'array' OR json_type(NEW.seal, '$.guard') IS NOT NULL",
           "D98: an effect seals floor and grant_guard, two arrays, and no other guard", "BEFORE INSERT ON effects"),
    # I6: the effect's floor is its run's sealed floor; compared structurally
    _abort("effects_carry_floor", "NOT " + _json_eq(
           "json_extract(NEW.seal, '$.floor')",
           "(SELECT json_extract(seal, '$.guard') FROM runs WHERE run_id = NEW.run_id)"),
           "I6: an effect seals the floor of its run", "BEFORE INSERT ON effects"),
    # I6/D98: the effect's grant_guard is the authorizing grant's signed guard ([] with no grant); compared
    # structurally, no concatenation in SQL
    _abort("effects_carry_guard", "NOT " + _json_eq(
           "json_extract(NEW.seal, '$.grant_guard')",
           "(CASE WHEN json_type(NEW.seal, '$.grant') = 'object' THEN "
           "IFNULL(json_extract(json_extract(NEW.seal, '$.grant.terms_text'), '$.guard'), '[]') ELSE '[]' END)"),
           "D68/I1/D98: an effect seals the grant guard that authorized it", "BEFORE INSERT ON effects"),
    # D67/D68/D75/I1: a confirm-class fire no human decided needs a sealed grant whose signed guard is complete
    _abort("effects_guarded", f"json_extract(NEW.seal, '$.posture') IN {CONFIRM_RUNGS} "
           "AND json_extract(NEW.seal, '$.by') IS NOT 'human' AND NOT (json_type(NEW.seal, '$.grant') IS 'object' AND "
           + _guard_complete("json_extract(NEW.seal, '$.grant.terms_text')") + ")",
           "D67/D75/I1: a confirm-class fire no human decided needs a signed grant guard", "BEFORE INSERT ON effects"),
    _abort("effects_ident", "NOT (" + _same("run_id", "effect_id", "attempt", "digest", "pid", "seal") + ")",
           "D70: an effect identity and seal are immutable", "BEFORE UPDATE ON effects"),
    _abort("effects_close", f"OLD.state = 'intent' AND ((NEW.state = 'intent' AND NEW.receipt_id IS NOT NULL) OR "
           f"(NEW.state != 'intent' AND (NEW.receipt_id IS NULL OR NOT {_has(_EFF, 'NEW.receipt_id')})))",
           "D54: an effect ends only with its receipt", "BEFORE UPDATE ON effects"),
    # O-p2-1 section 3 + A5: not_started is a refused receipt with no refuse_class that says nothing fired, and the
    # row's result carries the marker 'not_started:<why>' (a kill or prudential refusal never reads as not started)
    _abort("effects_close_mode", "OLD.state = 'intent' AND NEW.state IN ('done', 'unknown', 'not_started') AND NOT "
           f"((NEW.state = 'done' AND {_mode('NEW.receipt_id', TERMINAL_MODES[('effects', 'done')])}) OR "
           f"(NEW.state = 'unknown' AND {_mode('NEW.receipt_id', TERMINAL_MODES[('effects', 'unknown')])}) OR "
           f"(NEW.state = 'not_started' AND {_mode('NEW.receipt_id', TERMINAL_MODES[('effects', 'not_started')])} "
           "AND IFNULL(NEW.result GLOB 'not_started:?*', 0) AND EXISTS (SELECT 1 FROM gate_receipts g "
           "WHERE g.receipt_id = NEW.receipt_id AND json_type(g.record, '$.fired') = 'false' "
           "AND IFNULL(json_type(g.record, '$.action_face.refuse_class'), 'null') = 'null')))",
           "I4: done is fired, unknown is outcome_unknown, not_started is refused with its marker",
           "BEFORE UPDATE ON effects"),
    _abort("effects_final", "OLD.state != 'intent'", "D54: an ended effect is final", "BEFORE UPDATE ON effects"),
    _abort("effects_final_d", "OLD.state != 'intent'", "D54: an ended effect is final", "BEFORE DELETE ON effects"),
    # ---- manual pendings: born open; open->firing (with its decision), open|firing->terminal (with its receipt)
    _abort("pendings_born", "NEW.state IS NOT 'open' OR NEW.receipt_id IS NOT NULL OR NEW.decision IS NOT NULL",
           "D54: a pending is born open, unreceipted and undecided", "BEFORE INSERT ON manual_pendings"),
    _abort("pendings_move", "NOT (" + _same("pending_id", "seal", "seq") + " AND ("
           "(OLD.state = 'open' AND NEW.state = 'firing' AND NEW.receipt_id IS NULL AND NEW.decision IS NOT NULL) OR "
           f"(OLD.state IN ('open', 'firing') AND NEW.state = 'terminal' AND NEW.decision IS OLD.decision AND "
           f"{_has(_PEN, 'NEW.receipt_id')})))", "D54/D70: an illegal pending transition",
           "BEFORE UPDATE ON manual_pendings"),
    _abort("pendings_final_d", "1", "D55: a pending subject is never deleted", "BEFORE DELETE ON manual_pendings"),
    _abort("pendings_move_mode", "NEW.state = 'terminal' AND OLD.state != 'terminal' AND NOT "
           + _mode("NEW.receipt_id", TERMINAL_MODES[("manual_pendings", "terminal")]),
           "I4: a pending terminal_state its state permits", "BEFORE UPDATE ON manual_pendings"),
    # ---- requests (D72): born with this subject's receipt, never changed or removed
    _abort("requests_receipted", f"NOT {_has(_REQ, 'NEW.receipt_id')}",
           "D72: a request closes only with its receipt", "BEFORE INSERT ON requests"),
    _abort("requests_mode", f"NOT {_mode('NEW.receipt_id', TERMINAL_MODES[('requests', None)])}",
           "I4: a request closes refused", "BEFORE INSERT ON requests"),
    _abort("requests_final_u", "1", "D72: a closed request stays closed", "BEFORE UPDATE ON requests"),
    _abort("requests_final_d", "1", "D72: a closed request stays closed", "BEFORE DELETE ON requests"),
    # ---- receipts: never removed; only the projected mark moves, 0 -> 1 (D70); a receipt of an admitted run
    # carries that run's sealed guard (null when the floor is empty) (D68)
    _abort("receipts_final_d", "1", "D54: a receipt is never removed", "BEFORE DELETE ON gate_receipts"),
    _abort("receipts_final_u", "NOT (" + _same("receipt_id", "subject", "record", "seq")
           + " AND (NEW.projected IS OLD.projected OR (OLD.projected = 0 AND NEW.projected = 1)))",
           "D54/D70: a receipt is never rewritten", "BEFORE UPDATE ON gate_receipts"),
    # I4 / D78: the record names this row (id, envelope subject, kind) and a mode consistent with `fired`
    _abort("receipts_content", "NOT json_valid(NEW.record) OR json_extract(NEW.record, '$.id') IS NOT NEW.receipt_id "
           "OR json_extract(NEW.record, '$.subject') IS NOT NEW.subject OR json_extract(NEW.record, '$.kind') IS NOT 'gate' "
           "OR IFNULL(json_extract(NEW.record, '$.action_face.terminal_state'), '') NOT IN ("
           + ", ".join(f"'{m}'" for m in ALL_MODES) + ") "
           "OR IFNULL(json_type(NEW.record, '$.fired'), '') NOT IN ('true', 'false') "
           "OR (json_extract(NEW.record, '$.fired') = 1 AND "
           "json_extract(NEW.record, '$.action_face.terminal_state') IS NOT 'fired') "
           # I10 (13a): fired=false with terminal_state fired only for an effect still intent (it closes done)
           "OR (json_extract(NEW.record, '$.fired') = 0 AND "
           "json_extract(NEW.record, '$.action_face.terminal_state') = 'fired' AND NOT (NEW.subject GLOB 'effect:*' "
           f"AND EXISTS (SELECT 1 FROM effects e WHERE NEW.subject = {_effect_subj('e')} "
           "AND e.state = 'intent')))",
           "I4/I10: a receipt record names its own row", "BEFORE INSERT ON gate_receipts"),
    # D68/I1: an effect's receipt carries the effect's sealed guard; a run's or request's carries the run's floor
    # (null when empty); compared structurally (complement r11 8)
    # D98: an effect's receipt carries its seal's floor (action_face.guard) and grant_guard (action_face.grant_guard)
    _abort("receipts_carry_guard", f"EXISTS (SELECT 1 FROM effects e WHERE NEW.subject = {_effect_subj('e')} "
           "AND NOT (" + _json_eq(
               "(CASE WHEN json_array_length(e.seal, '$.floor') > 0 THEN json_extract(e.seal, '$.floor') END)",
               "json_extract(NEW.record, '$.action_face.guard')") + " AND " + _json_eq(
               "(CASE WHEN json_array_length(e.seal, '$.grant_guard') > 0 THEN json_extract(e.seal, '$.grant_guard') END)",
               "json_extract(NEW.record, '$.action_face.grant_guard')") + ")) "
           f"OR ((NEW.subject GLOB 'run:*' OR NEW.subject GLOB 'request:*') AND EXISTS (SELECT 1 FROM runs r "
           f"WHERE r.run_id = {_RUN_OF} AND NOT " + _json_eq(
               "(CASE WHEN json_array_length(r.seal, '$.guard') > 0 THEN json_extract(r.seal, '$.guard') END)",
               "json_extract(NEW.record, '$.action_face.guard')") + ")) "
           # D95: only a killed run receipt names a grant / grant guard (the guard that tripped)
           f"OR ((NEW.subject GLOB 'run:*' OR NEW.subject GLOB 'request:*') AND NOT {_KILLED_RUN} AND ("
           "IFNULL(json_type(NEW.record, '$.action_face.grant'), 'null') != 'null' "
           "OR IFNULL(json_type(NEW.record, '$.action_face.grant_guard'), 'null') != 'null'))",
           "D68/I1/D98: a receipt carries the guard its subject sealed", "BEFORE INSERT ON gate_receipts"),
    # D95: a policy kill stops the fire before any effect seal, so its run receipt carries the effective guard that
    # tripped: the floor (action_face.guard, above) and the authorizing grant's guard, with that grant, so the DB can
    # compare the guard to the grant's signed terms (the hash stays the fire check's, I13). No grant -> no grant_guard.
    _abort("receipts_kill_guard", f"{_KILLED_RUN} AND NOT IFNULL(("
           "IFNULL(json_type(NEW.record, '$.action_face.grant'), 'null') = 'null' "
           "AND json_extract(NEW.record, '$.action_face.grant_guard') IS NULL) OR ("
           + _grant_shape("NEW.record", "$.action_face.grant") + " AND " + _json_eq(
               "json_extract(NEW.record, '$.action_face.grant_guard')",
               "(CASE WHEN json_array_length(json_extract(NEW.record, '$.action_face.grant.terms_text'), '$.guard') > 0 "
               "THEN json_extract(json_extract(NEW.record, '$.action_face.grant.terms_text'), '$.guard') END)") + "), 0)",
           "D95: a killed run receipt carries the grant guard that tripped, the guard of the grant it names",
           "BEFORE INSERT ON gate_receipts"),
    # ---- holds (I3 / D77): born open; one decision, by a transition table; never replaced or removed
    _abort("holds_new", "EXISTS (SELECT 1 FROM holds WHERE hold_id = NEW.hold_id OR "
           "(NEW.pending_id IS NOT NULL AND pending_id = NEW.pending_id))",
           "D69: a row is never replaced", "BEFORE INSERT ON holds"),
    # I6: the sealed trigger event, when the hold carries one, is a JSON object (immutable: _HOLD_IDENT)
    _abort("holds_event_born", "NEW.trigger_event IS NOT NULL AND (NOT json_valid(NEW.trigger_event) "
           "OR json_type(NEW.trigger_event) IS NOT 'object')", "I6: a sealed trigger event is a JSON object",
           "BEFORE INSERT ON holds"),
    _abort("holds_born_open", "NEW.decided IS NOT NULL OR NEW.decisions IS NOT 0 OR "
           + " OR ".join(f"NEW.{c} IS NOT NULL" for c in _HOLD_DECISION),
           "I3: a hold is born undecided", "BEFORE INSERT ON holds"),
    # A1-R2: the decision generation steps by one in the statement that decides (approve, reject), and only there
    _abort("holds_move", "NOT (" + _same(*_HOLD_IDENT) + " AND ("
           # unchanged
           "(" + _same("decided", "decisions", *_HOLD_DECISION) + ") OR "
           # approve: open -> 1 (ends nothing)
           "(OLD.decided IS NULL AND NEW.decided IS 1 AND NEW.decided_by IS NOT NULL "
           "AND NEW.decisions IS OLD.decisions + 1) OR "
           # reopen (V _reopen_in): 1 -> open, every decision column cleared together, before its effect exists
           "(OLD.decided IS 1 AND NEW.decided IS NULL AND NEW.decisions IS OLD.decisions AND "
           + " AND ".join(f"NEW.{c} IS NULL" for c in _HOLD_DECISION)
           # O-p2-1 section 5: an attempt that did not start is not the link's effect (a retry may need the reopen)
           + " AND NOT EXISTS (SELECT 1 FROM effects e WHERE e.run_id = NEW.run_id AND e.effect_id = NEW.effect_id "
           "AND e.state != 'not_started')) OR "
           # reject: open -> 0, only once the run's cancel (with its receipt, cancels_receipted) is written
           "(OLD.decided IS NULL AND NEW.decided IS 0 AND NEW.decided_by IS NOT NULL "
           "AND NEW.decisions IS OLD.decisions + 1 AND NEW.signer IS NULL AND "
           "NEW.signature IS NULL AND NEW.challenge IS NULL AND NEW.fence IS NULL AND "
           "EXISTS (SELECT 1 FROM cancels c WHERE c.run_id = NEW.run_id AND c.receipt_id IS NOT NULL))))",
           "I3: an illegal hold transition", "BEFORE UPDATE ON holds"),
    _abort("holds_final_d", "1", "I3: a hold is never removed", "BEFORE DELETE ON holds"),
    # ---- I14: no JSON object in a receipt record or a seal holds a key twice (SQLite reads the first, Python
    # json.loads the last: codex r12)
    _abort("receipts_no_dup_keys", _dup_keys("NEW.record"), "I14: a JSON object never holds a key twice",
           "BEFORE INSERT ON gate_receipts"),
    _abort("seals_no_dup_keys_runs", _dup_keys("NEW.seal"), "I14: a JSON object never holds a key twice",
           "BEFORE INSERT ON runs"),
    _abort("seals_no_dup_keys_effects", _dup_keys("NEW.seal"), "I14: a JSON object never holds a key twice",
           "BEFORE INSERT ON effects"),
    # ---- held_counts (I5 -> I11): consecutive subject-specific holds of a LIVE subject; never replaced or removed
    _abort("held_new", "EXISTS (SELECT 1 FROM held_counts WHERE subject = NEW.subject)",
           "D69: a row is never replaced", "BEFORE INSERT ON held_counts"),
    # held_born / held_move: HELD_TRIGGERS
    _abort("held_final_d", "1", "I5: a held count is never removed", "BEFORE DELETE ON held_counts"),
    # I11: a held_limit close is backed by a count at the bound (a writer cannot invent one)
    # I11 / D87 probe: a close names a run that exists (run ids are deterministic, run_id_for: a close written before
    # its run is admitted would make that run dead on arrival)
    _abort("cancels_run_exists", "NOT EXISTS (SELECT 1 FROM runs WHERE run_id = NEW.run_id)",
           "I11: a close names an admitted run", "BEFORE INSERT ON cancels"),
    # (no requests twin: D72's run_not_admitted request closes a request whose run has no row, by design)
    _abort("cancels_held_limit", f"NEW.reason GLOB 'held_limit:*' AND NOT EXISTS (SELECT 1 FROM held_counts h "
           f"WHERE h.n >= {_HELD_LIMIT_SQL} AND (substr(h.subject, 1, length({_run_requests('NEW.run_id')})) = "
           f"{_run_requests('NEW.run_id')} OR h.subject IN (SELECT 'pending:' || pending_id FROM holds "
           "WHERE run_id = NEW.run_id AND pending_id IS NOT NULL)))",
           "I11: a held_limit close needs its count at the bound", "BEFORE INSERT ON cancels"),
    _abort("requests_held_limit", f"NEW.reason GLOB 'held_limit:*' AND NOT EXISTS (SELECT 1 FROM held_counts h "
           f"WHERE h.n >= {_HELD_LIMIT_SQL} AND h.subject = 'request:' || NEW.request_id)",
           "I11: a held_limit close needs its count at the bound", "BEFORE INSERT ON requests"),
)

# I11: the close path. A receipt row is written only by an INSERT INTO close_subject, whose INSTEAD OF trigger writes
# the receipt AND its subject's terminal row and refuses the statement unless exactly one terminal row now carries it.
_ONE_TERMINAL = ("(SELECT COUNT(*) FROM cancels WHERE receipt_id = NEW.receipt_id) "
                 "+ (SELECT COUNT(*) FROM requests WHERE receipt_id = NEW.receipt_id) "
                 "+ (SELECT COUNT(*) FROM effects WHERE receipt_id = NEW.receipt_id AND state IN ('done', 'unknown', 'not_started')) "
                 "+ (SELECT COUNT(*) FROM manual_pendings WHERE receipt_id = NEW.receipt_id AND state = 'terminal')")
CLOSE_PATH = (
    _abort("receipts_via_close", "NOT EXISTS (SELECT 1 FROM closing WHERE subject = NEW.subject)",
           "I11: a receipt is written only with its terminal row (close_subject)", "BEFORE INSERT ON gate_receipts"),
    "CREATE TRIGGER IF NOT EXISTS close_subject_do INSTEAD OF INSERT ON close_subject BEGIN "
    "INSERT INTO closing (subject) VALUES (NEW.subject); "
    "INSERT INTO gate_receipts (receipt_id, subject, record, seq) VALUES (NEW.receipt_id, NEW.subject, NEW.record, NEW.seq); "
    "INSERT INTO cancels (run_id, reason, receipt_id) SELECT substr(NEW.subject, 5), NEW.reason, NEW.receipt_id "
    "WHERE NEW.subject GLOB 'run:?*'; "
    "INSERT INTO requests (request_id, reason, receipt_id) SELECT substr(NEW.subject, 9), NEW.reason, NEW.receipt_id "
    "WHERE NEW.subject GLOB 'request:?*'; "
    "UPDATE effects SET state = NEW.state, result = NEW.result, receipt_id = NEW.receipt_id "
    f"WHERE NEW.subject GLOB 'effect:?*' AND NEW.subject = {_effect_subj('effects')} AND state = 'intent'; "
    "UPDATE manual_pendings SET state = 'terminal', receipt_id = NEW.receipt_id "
    "WHERE NEW.subject = 'pending:' || pending_id AND state IN ('open', 'firing'); "
    "DELETE FROM closing WHERE subject = NEW.subject; "
    f"SELECT RAISE(ABORT, 'I11: a receipt commits only with exactly one terminal row') WHERE ({_ONE_TERMINAL}) IS NOT 1; "
    "END",
)

# D57's selections (the lease test for 'intent' / 'firing' rows is applied by the caller, inside the write txn). A1-R1:
# none selects a legacy row (frozen: LEGACY_TRIGGERS would refuse its close); an unstamped intent effect in a legacy run
# is selected (rev74 DETAIL step 7: the reaper closes a poisoned intent after the upgrade)
REAP_EFFECTS = "SELECT run_id, effect_id, attempt, seal FROM effects WHERE state = 'intent' AND legacy = 0"
# D71: unstarted is POSITIVE evidence (started_at IS NULL), never the absence of an intent row
REAP_FENCED_RUNS = """
SELECT r.run_id, r.seal FROM runs r JOIN fences f ON f.binding_id = r.binding_id
 WHERE f.generation > r.generation
   AND r.legacy = 0
   AND r.started_at IS NULL
   AND NOT EXISTS (SELECT 1 FROM gate_receipts g WHERE g.subject = 'run:' || r.run_id)
"""
REAP_PENDINGS = "SELECT pending_id, seal FROM manual_pendings WHERE state = 'firing'"
# I2 / D76: fenced mid-chain = fenced AND started AND no effect attempt intent AND fewer done/unknown effects than
# the links sealed at admission (so a completed run, and a run with an effect still intent, is never an input). O-p2-1
# section 5: an attempt that ended not_started is not live, so a run whose open link only did not start is an input
REAP_MIDCHAIN_RUNS = """
SELECT r.run_id, r.seal FROM runs r JOIN fences f ON f.binding_id = r.binding_id
 WHERE f.generation > r.generation
   AND r.legacy = 0
   AND r.started_at IS NOT NULL
   AND NOT EXISTS (SELECT 1 FROM effects e WHERE e.run_id = r.run_id AND e.state = 'intent')
   AND (SELECT COUNT(*) FROM effects e WHERE e.run_id = r.run_id AND e.state IN ('done', 'unknown'))
       < json_extract(r.seal, '$.links')
   AND NOT EXISTS (SELECT 1 FROM cancels c WHERE c.run_id = r.run_id)
   AND NOT EXISTS (SELECT 1 FROM gate_receipts g WHERE g.subject = 'run:' || r.run_id)
"""


# I12 (D88 replaced): a started run with an unknown or failed link, fewer terminal effects than links, no intent, no
# close: it can never complete (a chain aborts at such a link, V chainpath ``_walk``; a redelivery is POISONED, V journal
# ``_barrier``), fenced or not. Closed with one run: receipt, refused, reason 'unknown-link'. "Failed" = a done effect
# whose receipt says fired=false (I10's pick, 13a2: terminal_state fired, envelope fired = execution.ok = false; D94).
REAP_UNKNOWN_LINK_RUNS = """
SELECT r.run_id, r.seal FROM runs r
 WHERE r.legacy = 0
   AND r.started_at IS NOT NULL
   AND EXISTS (SELECT 1 FROM effects e WHERE e.run_id = r.run_id AND (e.state = 'unknown' OR (e.state = 'done'
               AND EXISTS (SELECT 1 FROM gate_receipts g WHERE g.receipt_id = e.receipt_id
                           AND json_extract(g.record, '$.fired') = 0))))
   AND NOT EXISTS (SELECT 1 FROM effects e WHERE e.run_id = r.run_id AND e.state = 'intent')
   AND (SELECT COUNT(*) FROM effects e WHERE e.run_id = r.run_id AND e.state IN ('done', 'unknown'))
       < json_extract(r.seal, '$.links')
   AND NOT EXISTS (SELECT 1 FROM cancels c WHERE c.run_id = r.run_id)
   AND NOT EXISTS (SELECT 1 FROM gate_receipts g WHERE g.subject = 'run:' || r.run_id)
"""

# ---- rev 7.4 (rev74_schema.py): R13-6 decisions, R13-5 legacy mark, INVARIANT, R13-1 reaper, R13-9 floor ----
V74_TABLES = (
    "CREATE TABLE IF NOT EXISTS hold_decisions (hold_id TEXT NOT NULL, run_id TEXT NOT NULL, pending_id TEXT, "
    "by TEXT, n INTEGER NOT NULL CHECK (n >= 1), PRIMARY KEY (hold_id, n))",
)
LEGACY_TABLES = ("runs", "cancels", "effects", "holds")
V74_COLUMNS = tuple((t, "legacy", "INTEGER NOT NULL DEFAULT 0 CHECK (legacy IN (0, 1))") for t in LEGACY_TABLES)

KNOWN_COLUMNS = {k: tuple(v) for k, v in KNOWN_COLUMNS.items()}
for _t in LEGACY_TABLES:
    KNOWN_COLUMNS[_t] = KNOWN_COLUMNS[_t] + ("legacy",)
KNOWN_COLUMNS["hold_decisions"] = ("hold_id", "run_id", "pending_id", "by", "n")
# O-p2-1 (A6(b), R2-7): the attempt number, LAST, so no positional reader of a /2 row shifts
KNOWN_COLUMNS["effects"] = KNOWN_COLUMNS["effects"] + ("attempt",)

# O-p2-1 (A6(a)): the /3 effects table as authored; ddl_v3 creates it under this text (the stored DDL is this text on
# every path, never SQLite's rename rewrite). One row per ATTEMPT of a link: the key gains `attempt`, and a link's
# attempt ends `not_started` when nothing ran (section 3)
EFFECTS_V3 = ("CREATE TABLE effects (run_id TEXT NOT NULL, effect_id TEXT NOT NULL, digest TEXT NOT NULL, "
              "pid INTEGER, state TEXT NOT NULL CHECK (state IN ('intent', 'done', 'unknown', 'not_started')), "
              "result TEXT, receipt_id TEXT, seal TEXT, legacy INTEGER NOT NULL DEFAULT 0 CHECK (legacy IN (0, 1)), "
              "attempt INTEGER NOT NULL DEFAULT 1 CHECK (attempt >= 1), PRIMARY KEY (run_id, effect_id, attempt))")
# O-p2-1 (section 2, R2-8, R4-3): one end per link, one live attempt per link, one hold per link
ATTEMPT_INDEXES = (
    "CREATE UNIQUE INDEX IF NOT EXISTS effects_one_end ON effects (run_id, effect_id) "
    "WHERE state IN ('done', 'unknown')",
    "CREATE UNIQUE INDEX IF NOT EXISTS effects_one_live ON effects (run_id, effect_id) WHERE state = 'intent'",
    "CREATE UNIQUE INDEX IF NOT EXISTS holds_one_link ON holds (run_id, effect_id)",
)

# O-p2-1 R2-1: a count that names an attempt names the link's LATEST attempt, ended not_started (NULL: no attempt)
_HELD_ATTEMPT = ("(NEW.attempt IS NULL OR EXISTS (SELECT 1 FROM effects a WHERE " + _link_request("a")
                 + " = NEW.subject AND a.attempt = NEW.attempt AND a.state = 'not_started' AND NOT EXISTS ("
                 "SELECT 1 FROM effects b WHERE b.run_id = a.run_id AND b.effect_id = a.effect_id "
                 "AND b.attempt > a.attempt)))")
HELD_TRIGGERS = (
    _abort("held_born", f"NOT IFNULL(NEW.n = 1 AND NEW.mark IS {_progress('NEW.subject')} "
           f"AND NEW.last_reason GLOB '{SUBJECT_REASON}' AND NOT {_closed('NEW.subject')} AND {_HELD_ATTEMPT}, 0)",
           "I11: a held count is born 1, at the live subject mark, for a subject fault", "BEFORE INSERT ON held_counts"),
    # a same-mark rise records a new attempt (R3-3, R4-4: both NULL, or both named and the attempt rises) or carries
    # the named attempt forward for an attempt-less fault (P1); a restart on progress may name any witnessed attempt
    _abort("held_move", f"NOT IFNULL(NEW.subject IS OLD.subject AND NEW.last_reason GLOB '{SUBJECT_REASON}' "
           f"AND NOT {_closed('NEW.subject')} AND NEW.mark IS {_progress('NEW.subject')} AND {_HELD_ATTEMPT} AND ("
           "(NEW.n = OLD.n + 1 AND NEW.mark IS OLD.mark AND ((OLD.attempt IS NULL AND NEW.attempt IS NULL) "
           "OR (OLD.attempt IS NOT NULL AND NEW.attempt IS NOT NULL AND NEW.attempt > OLD.attempt) "
           "OR (OLD.attempt IS NOT NULL AND NEW.attempt IS OLD.attempt))) "
           "OR (NEW.n = 1 AND NEW.mark > OLD.mark)), 0)",
           "I11: a held count rises by one at the same mark, or restarts at 1 only on progress",
           "BEFORE UPDATE ON held_counts"),
)

DECISION_TRIGGERS = (
    "CREATE TRIGGER IF NOT EXISTS holds_decision_log AFTER UPDATE OF decided ON holds "
    "WHEN OLD.decided IS NULL AND NEW.decided IS NOT NULL BEGIN "
    "INSERT INTO hold_decisions (hold_id, run_id, pending_id, by, n) VALUES (NEW.hold_id, NEW.run_id, NEW.pending_id, "
    "NEW.decided_by, NEW.decisions); END",
    # A1-R2: n is the hold's decision generation, which only a deciding statement steps (holds_move); the primary key
    # refuses a second row for it. A1-R2b: the upgrade gives a decided legacy hold the column's default 0, so a legacy
    # hold takes no row and n starts at 1 (the table's CHECK too)
    _abort("hold_decisions_born", "NEW.n < 1 OR NOT EXISTS (SELECT 1 FROM holds h "
           "WHERE h.hold_id = NEW.hold_id AND h.legacy = 0 "
           "AND h.run_id IS NEW.run_id AND h.pending_id IS NEW.pending_id AND h.decided IS NOT NULL "
           "AND h.decided_by IS NEW.by AND h.decisions IS NEW.n)",
           "R13-6: a decision row is the current decision of a decided hold", "BEFORE INSERT ON hold_decisions"),
    _abort("hold_decisions_u", "1", "R13-6: a decision row is never changed", "BEFORE UPDATE ON hold_decisions"),
    _abort("hold_decisions_d", "1", "R13-6: a decision row is never removed", "BEFORE DELETE ON hold_decisions"),
)

# ---- R13-5: the legacy mark ---------------------------------------------------------------------------------------
LEGACY_TRIGGERS = tuple(
    t for tbl in LEGACY_TABLES for t in (
        _abort(f"{tbl}_legacy_born", "NEW.legacy IS NOT 0",
               "I15: a row is legacy only from the /2 -> /3 upgrade", f"BEFORE INSERT ON {tbl}"),
        _abort(f"{tbl}_legacy_fixed", "NEW.legacy IS NOT OLD.legacy",
               "I15: the legacy mark never moves", f"BEFORE UPDATE ON {tbl}"),
        # A1-R1: a legacy row is frozen whole [judged by lane a1c1d, 10-26, against levain project_memory
        # 1026-20/rev74/DETAIL.md I15 steps 3-7: no column of a stamped row changes after the upgrade]; an unstamped
        # row in a legacy run (a backfilled or poisoned intent effect) is not, and closes as any row does
        _abort(f"{tbl}_legacy_frozen", "OLD.legacy = 1",
               "I15: a legacy row is frozen (no change after the upgrade)", f"BEFORE UPDATE ON {tbl}"),
    )
) + (
    _abort("effects_in_legacy_run", "EXISTS (SELECT 1 FROM runs WHERE run_id = NEW.run_id AND legacy = 1)",
           "I15: a legacy run is frozen (no effect after the upgrade)", "BEFORE INSERT ON effects"),
    _abort("cancels_in_legacy_run", "EXISTS (SELECT 1 FROM runs WHERE run_id = NEW.run_id AND legacy = 1)",
           "I15: a legacy run is frozen (no close after the upgrade)", "BEFORE INSERT ON cancels"),
    _abort("holds_in_legacy_run", "EXISTS (SELECT 1 FROM runs WHERE run_id = NEW.run_id AND legacy = 1)",
           "I15: a legacy run is frozen (no hold after the upgrade)", "BEFORE INSERT ON holds"),
    _abort("requests_in_legacy_run", "EXISTS (SELECT 1 FROM runs WHERE run_id = "
           + _run_of_request("'request:' || NEW.request_id") + " AND legacy = 1)", "I15: a legacy run is frozen (no request close after the upgrade)", "BEFORE INSERT ON requests"),
)

V74_TRIGGERS = V3_TRIGGERS + HELD_TRIGGERS + DECISION_TRIGGERS + LEGACY_TRIGGERS

# O-p2-1 (A8): the states an effects row ENDS in, each carrying its receipt
_EFFECT_ENDS = "('done', 'unknown', 'not_started')"

# O-p2-1 R2-1 / glm F4 / R2-10 / R3-4: not_started_uncounted is a LIVE link (the held triggers' own _closed) whose
# latest attempt ended not_started with no count naming it (effects_attempt_counted refuses its retry); the remedy is
# the count, or the operator cancelling the run, which closes the link and clears the row
INVARIANT = f"""
SELECT 'terminal_without_receipt', 'run:' || c.run_id FROM cancels c
 WHERE c.legacy = 0 AND (c.receipt_id IS NULL OR NOT EXISTS (SELECT 1 FROM gate_receipts g
       WHERE g.receipt_id = c.receipt_id AND g.subject = 'run:' || c.run_id))
UNION ALL
SELECT 'terminal_without_receipt', {_effect_subj('e')} FROM effects e
 WHERE e.legacy = 0 AND e.state IN {_EFFECT_ENDS} AND (e.receipt_id IS NULL OR NOT EXISTS (SELECT 1 FROM gate_receipts g
       WHERE g.receipt_id = e.receipt_id AND g.subject = {_effect_subj('e')}))
UNION ALL
SELECT 'terminal_without_receipt', 'pending:' || p.pending_id FROM manual_pendings p
 WHERE p.state = 'terminal' AND (p.receipt_id IS NULL OR NOT EXISTS (SELECT 1 FROM gate_receipts g
       WHERE g.receipt_id = p.receipt_id AND g.subject = 'pending:' || p.pending_id))
UNION ALL
SELECT 'terminal_without_receipt', 'request:' || q.request_id FROM requests q
 WHERE NOT EXISTS (SELECT 1 FROM gate_receipts g
       WHERE g.receipt_id = q.receipt_id AND g.subject = 'request:' || q.request_id)
UNION ALL
SELECT 'receipts_per_subject_gt_1', subject FROM gate_receipts GROUP BY subject HAVING COUNT(*) > 1
UNION ALL
SELECT 'receipt_without_terminal', g.subject FROM gate_receipts g
 WHERE NOT EXISTS (SELECT 1 FROM cancels c WHERE c.receipt_id = g.receipt_id AND g.subject = 'run:' || c.run_id)
   AND NOT EXISTS (SELECT 1 FROM effects e WHERE e.receipt_id = g.receipt_id
                   AND g.subject = {_effect_subj('e')} AND e.state IN {_EFFECT_ENDS})
   AND NOT EXISTS (SELECT 1 FROM manual_pendings p WHERE p.receipt_id = g.receipt_id
                   AND g.subject = 'pending:' || p.pending_id AND p.state = 'terminal')
   AND NOT EXISTS (SELECT 1 FROM requests q WHERE q.receipt_id = g.receipt_id
                   AND g.subject = 'request:' || q.request_id)
UNION ALL
SELECT 'closing_left', subject FROM closing
UNION ALL
SELECT 'hold_rejected_without_run_close', 'run:' || h.run_id FROM holds h
 WHERE h.decided = 0 AND h.legacy = 0 AND NOT EXISTS (SELECT 1 FROM cancels c JOIN gate_receipts g ON g.receipt_id = c.receipt_id
       AND g.subject = 'run:' || c.run_id WHERE c.run_id = h.run_id)
UNION ALL
SELECT 'not_started_uncounted', {_effect_subj('ns')} FROM effects ns
 WHERE ns.state = 'not_started' AND ns.legacy = 0
   AND NOT EXISTS (SELECT 1 FROM effects x WHERE x.run_id = ns.run_id AND x.effect_id = ns.effect_id
                   AND x.attempt > ns.attempt)
   AND NOT EXISTS (SELECT 1 FROM held_counts hc WHERE hc.subject = {_link_request('ns')} AND hc.attempt IS ns.attempt)
   AND NOT {_closed(_link_request('ns'))}
"""

LEGACY_CENSUS = " UNION ALL ".join(
    f"SELECT '{t}', {k} FROM {t} WHERE legacy = 1" for t, k in (
        ("runs", "run_id"), ("cancels", "run_id"), ("effects", _attempt_key("effects")), ("holds", "hold_id"))
) + " ORDER BY 1, 2"


REAP_ORPHAN_RECEIPTS = f"""
SELECT g.receipt_id, g.subject, json_extract(g.record, '$.action_face.terminal_state') FROM gate_receipts g
 WHERE NOT EXISTS (SELECT 1 FROM cancels c WHERE c.receipt_id = g.receipt_id AND g.subject = 'run:' || c.run_id)
   AND NOT EXISTS (SELECT 1 FROM effects e WHERE e.receipt_id = g.receipt_id
                   AND g.subject = {_effect_subj('e')} AND e.state IN {_EFFECT_ENDS})
   AND NOT EXISTS (SELECT 1 FROM manual_pendings p WHERE p.receipt_id = g.receipt_id
                   AND g.subject = 'pending:' || p.pending_id AND p.state = 'terminal')
   AND NOT EXISTS (SELECT 1 FROM requests q WHERE q.receipt_id = g.receipt_id
                   AND g.subject = 'request:' || q.request_id)
 ORDER BY g.seq
"""
ORPHAN_REASON = "adopted:receipt_without_terminal"
# a committed `closing` row is always a leftover (close_subject_do deletes its own before the statement ends), and a
# leftover is a standing pass for receipts_via_close on that subject: the reaper removes it first
REAP_CLOSING_LEFT = "SELECT subject FROM closing ORDER BY subject"


def clear_closing_left(conn: sqlite3.Connection) -> list[str]:
    left = [s for (s,) in conn.execute(REAP_CLOSING_LEFT)]
    conn.execute("DELETE FROM closing")
    return left


ADOPTED = "adopted"
IN_FLIGHT = "in_flight"


def effect_subject(run_id: str, effect_id: str, attempt: int = 1) -> str:
    """The subject of an effect attempt's receipt: ``effect:<run_id>:<effect_id>`` for attempt 1 and
    ``effect:<run_id>:<effect_id>#<n>`` for attempt n >= 2 (O-p2-1 section 4; ``_effect_subj`` is the SQL twin). A run
    id holding ``:`` or ``#`` is refused: the subject is split at its first colon (here, in the journal, in the store's
    triggers), and only a colonless run id makes that split name one effect. ``effect_id`` may hold colons, never
    ``#`` (the attempt is split off at the last ``#``). ``attempt`` is an int >= 1, never a bool."""
    if ":" in run_id or "#" in run_id:
        raise ValueError(f"run id {run_id!r} holds a colon or '#': an effect subject would split ambiguously")
    if "#" in effect_id:
        raise ValueError(f"effect id {effect_id!r} holds '#': an effect subject would split ambiguously")
    if type(attempt) is not int or attempt < 1:
        raise ValueError(f"attempt {attempt!r} is not an int >= 1")
    return f"effect:{run_id}:{effect_id}" + (f"#{attempt}" if attempt > 1 else "")


def _effect_key(subject: str) -> tuple[str, str, int]:
    # ``effect:<run_id>:<effect_id>[#<n>]`` -> (run_id, effect_id, attempt): a subject that does not split, or one
    # attempt spelled another way than effect_subject spells it (``#1``, ``#02``), is refused as the store refuses a
    # bad row
    run_id, sep, rest = subject[7:].partition(":")
    effect_id, hsep, n = rest.rpartition("#") if "#" in rest else (rest, "", "")
    ok = bool(run_id and sep and effect_id) and "#" not in run_id and "#" not in effect_id
    if hsep:
        ok = ok and n.isascii() and n.isdigit() and n[0] != "0" and int(n) >= 2
    if not ok:
        raise sqlite3.IntegrityError(f"R13-1: malformed effect subject {subject!r}")
    return run_id, effect_id, int(n) if hsep else 1


def adopt_orphan(conn: sqlite3.Connection, receipt_id: str, subject: str, terminal_state: str, *,
                 lease_held: Callable[[str, str], bool], pending_lease_held: Callable[[str], bool]) -> str:
    """The D57 repair of one orphan receipt, in the caller's write transaction: write the subject's terminal row
    carrying the orphan's id (the terminal-row triggers check it against the receipt: ``cancels_receipted``,
    ``cancels_mode``, ``effects_close``, ``effects_close_mode``, ``requests_receipted``, ``pendings_move``). A receipt
    whose terminal_state its subject cannot end in is refused by those triggers and stays an INVARIANT row (doctor).
    R14-10: the D57 lease test comes first: an ``effect:`` orphan whose owner holds its lease (``lease_held(run_id,
    effect_id)``, V :meth:`RunJournal._lease_held`), or a ``firing`` pending whose owner holds its pending lease
    (``pending_lease_held(pending_id)``), is in flight, and is left as it is (:data:`IN_FLIGHT`)."""
    if subject.startswith("effect:"):
        run_id, effect_id, _attempt = _effect_key(subject)
        if lease_held(run_id, effect_id):   # the lease is the link's: its attempts are serial (effects_one_live)
            return IN_FLIGHT
    if subject.startswith("pending:"):
        firing = conn.execute("SELECT 1 FROM manual_pendings WHERE pending_id = ? AND state = 'firing'",
                              (subject[8:],)).fetchone()
        if firing is not None and pending_lease_held(subject[8:]):
            return IN_FLIGHT
    if subject.startswith("run:"):
        conn.execute("INSERT INTO cancels (run_id, reason, receipt_id) VALUES (?, ?, ?)",
                     (subject[4:], ORPHAN_REASON, receipt_id))
    elif subject.startswith("request:"):
        conn.execute("INSERT INTO requests (request_id, reason, receipt_id) VALUES (?, ?, ?)",
                     (subject[8:], ORPHAN_REASON, receipt_id))
    elif subject.startswith("effect:"):
        run_id, effect_id, attempt = _effect_key(subject)
        # O-p2-1 R2-9: a `refused` receipt is never adopted (a not_started close is one close_subject statement, so
        # it is never an orphan's half, and its row marker cannot be read from the receipt); it stays an INVARIANT row
        state = {"fired": "done", "outcome_unknown": "unknown"}.get(terminal_state)
        if state is None:
            raise sqlite3.IntegrityError(f"R13-1: an effect receipt in {terminal_state!r} ends no effect row")
        n = conn.execute("UPDATE effects SET state = ?, receipt_id = ? WHERE run_id = ? AND effect_id = ? "
                         "AND attempt = ? AND state = 'intent'", (state, receipt_id, run_id, effect_id, attempt)).rowcount
        if n != 1:
            raise sqlite3.IntegrityError("R13-1: no intent effect row adopts this receipt")
    elif subject.startswith("pending:"):
        n = conn.execute("UPDATE manual_pendings SET state = 'terminal', receipt_id = ? WHERE pending_id = ? "
                         "AND state IN ('open', 'firing')", (receipt_id, subject[8:])).rowcount
        if n != 1:
            raise sqlite3.IntegrityError("R13-1: no live pending row adopts this receipt")
    else:
        raise sqlite3.IntegrityError(f"R13-1: unknown subject {subject!r}")
    return ADOPTED


MIN_SQLITE = (3, 38, 0)   # JSON1 built in by default from 3.38.0 (json_each / json_tree / json_extract in every trigger)


class StoreSqliteTooOld(RuntimeError):
    """R13-9: the SQLite this opener runs on is below MIN_SQLITE or lacks JSON1. An environment fault (I11 b)."""


def check_sqlite(conn: sqlite3.Connection, minimum: tuple[int, int, int] = MIN_SQLITE) -> str:
    ver = conn.execute("SELECT sqlite_version()").fetchone()[0]
    have = tuple(int(x) for x in ver.split(".")[:3])
    if have < minimum:
        raise StoreSqliteTooOld(f"store-sqlite-too-old: SQLite {ver} is below {'.'.join(map(str, minimum))}")
    try:
        conn.execute("SELECT json_extract('{\"a\":1}', '$.a'), (SELECT COUNT(*) FROM json_tree('[1]'))").fetchone()
    except sqlite3.OperationalError as e:
        raise StoreSqliteTooOld(f"store-sqlite-too-old: SQLite {ver} has no JSON1 ({e})") from e
    return ver



# ---- O-74-4: no effect row is removed, an intent one included (effects_final_d leaves intent rows to this) --------
# O-a1c1-3: no levain writer deletes a fence, a graduation step (an audit row) or the health row [judged by lane
# a1c1b, 10-26, against git grep over levain/ for DELETE FROM, REPLACE INTO, executescript, DROP TABLE]; fences and
# the health row are written by upsert (ON CONFLICT DO UPDATE fires no delete: the delete-sweep test runs both).
# Left deletable: bindings (V BindingStore rewrites its registry by "DELETE FROM bindings") and closing
# (close_subject_do deletes its own mark).
DELETE_TRIGGERS = (
    _abort("effects_d", "1", "O-74-4: an effect row is never removed", "BEFORE DELETE ON effects"),
    _abort("fences_d", "1", "O-a1c1-3: a fence is never removed", "BEFORE DELETE ON fences"),
    _abort("graduation_steps_d", "1", "O-a1c1-3: a graduation step is never removed",
           "BEFORE DELETE ON graduation_steps"),
    _abort("env_health_d", "1", "O-a1c1-3: the health row is never removed", "BEFORE DELETE ON env_health"),
)

# ---- R13-7: the per-subject environment-fault bound ------------------------------------------------------------------
V74B_TABLES = (
    "CREATE TABLE IF NOT EXISTS env_faults (subject TEXT PRIMARY KEY, first_at TEXT, last_at TEXT NOT NULL, "
    "n INTEGER NOT NULL CHECK (n >= 0), last_reason TEXT NOT NULL, alerted_at TEXT)",
    "CREATE TABLE IF NOT EXISTS env_health (k INTEGER PRIMARY KEY CHECK (k = 1), at TEXT NOT NULL)",
)
# integer seconds (unixepoch), never a julianday difference: one hour as a float read below 1/24 (rev74b, run 1)
_AT_BOUND = (f"(NEW.n >= {_tune('env_fault_n')} AND unixepoch(NEW.last_at) - unixepoch(NEW.first_at) "
             f">= {_tune('env_fault_span_s')})")
ENV_TRIGGERS = (
    _abort("env_faults_born", "NOT IFNULL(NEW.n = 1 AND NEW.first_at IS NEW.last_at AND NEW.alerted_at IS NULL, 0)",
           "R13-7: an environment-fault row is born at its first fault, unalerted", "BEFORE INSERT ON env_faults"),
    _abort("env_faults_move", "NOT IFNULL(NEW.subject IS OLD.subject AND unixepoch(NEW.last_at) >= unixepoch(OLD.last_at) "
           "AND ((NEW.n = OLD.n + 1 AND NEW.first_at IS IFNULL(OLD.first_at, NEW.last_at) "
           "AND (NEW.alerted_at IS OLD.alerted_at OR OLD.alerted_at IS NULL)) "
           "OR (NEW.n = 0 AND NEW.first_at IS NULL AND NEW.alerted_at IS NULL) "
           "OR (NEW.n = OLD.n AND NEW.first_at IS OLD.first_at AND NEW.last_at IS OLD.last_at "
           "AND OLD.alerted_at IS NULL AND NEW.alerted_at IS NOT NULL)), 0)",
           "R13-7: a fault count rises by one in its episode, or resets to 0 on progress", "BEFORE UPDATE ON env_faults"),
    _abort("env_faults_alert_once", f"NEW.alerted_at IS NOT NULL AND (OLD.alerted_at IS NOT NULL OR NOT {_AT_BOUND})",
           "R13-7: one alert per episode, only at the bound", "BEFORE UPDATE OF alerted_at ON env_faults"),
    _abort("env_faults_d", "1", "R13-7: an environment-fault row is never removed", "BEFORE DELETE ON env_faults"),
)
ENV_FAULT_LISTING = ("SELECT subject, n, first_at, last_at, last_reason, alerted_at FROM env_faults "
                     f"WHERE n >= {_tune('env_fault_n')} AND unixepoch(last_at) - unixepoch(first_at) "
                     f">= {_tune('env_fault_span_s')} ORDER BY first_at, subject")

# ---- O-74b-4 + R14-4: the meta keys the store depends on --------------------------------------------------------------
PROTECTED_META = ("format", "store_id") + tuple(TUNABLES)
_PROT = "(" + ", ".join(f"'{k}'" for k in PROTECTED_META) + ")"
_TUNE_KEYS = "(" + ", ".join(f"'{k}'" for k in TUNABLES) + ")"


def _posint(v: str) -> str:
    return f"({v} GLOB '[1-9]*' AND {v} NOT GLOB '*[^0-9]*' AND length({v}) <= 12)"


_N = len(_FORMAT_PREFIX) + 1   # substr() is 1-based: the first character after the prefix
META_TRIGGERS = (
    # an insert over a protected key that exists is IGNORED, never applied (so a REPLACE never reaches its delete):
    # IGNORE rather than ABORT so an older levain's ``INSERT OR IGNORE`` of its store_id passes on to its own format
    # check, which refuses the store by name (V db.py ``migrate_to_v2``)
    f"CREATE TRIGGER IF NOT EXISTS meta_new BEFORE INSERT ON meta WHEN NEW.key IN {_PROT} AND EXISTS (SELECT 1 "
    "FROM meta WHERE key = NEW.key) BEGIN SELECT RAISE(IGNORE); END",
    _abort("meta_born", f"NEW.key IN {_TUNE_KEYS} AND NOT {_posint('NEW.value')}",
           "R14-4: a tunable is a positive integer", "BEFORE INSERT ON meta"),
    _abort("meta_d", f"OLD.key IN {_PROT}", "O-74b-4: a store meta key is never removed", "BEFORE DELETE ON meta"),
    _abort("meta_u", f"(OLD.key IN {_PROT} OR NEW.key IN {_PROT}) AND NOT IFNULL(NEW.key IS OLD.key AND ("
           f"(OLD.key = 'format' AND NEW.value GLOB '{_FORMAT_PREFIX}[1-9]*' "
           f"AND substr(NEW.value, {_N}) NOT GLOB '*[^0-9]*' AND OLD.value GLOB '{_FORMAT_PREFIX}[1-9]*' "
           f"AND CAST(substr(NEW.value, {_N}) AS INTEGER) > CAST(substr(OLD.value, {_N}) AS INTEGER)) "
           f"OR (OLD.key IN {_TUNE_KEYS} AND {_posint('NEW.value')})), 0)",
           "O-74b-4: the format marker only moves forward, a tunable only to a positive integer, the rest never",
           "BEFORE UPDATE ON meta"),
)


# ---- the schema, applied --------------------------------------------------------------------------------------------
ALL_TRIGGERS = V74_TRIGGERS + CLOSE_PATH + DELETE_TRIGGERS + ENV_TRIGGERS + META_TRIGGERS
KNOWN_COLUMNS["env_faults"] = ("subject", "first_at", "last_at", "n", "last_reason", "alerted_at")
KNOWN_COLUMNS["env_health"] = ("k", "at")


def ddl_v3(conn: sqlite3.Connection) -> None:
    """The ``/3`` tables and columns over a ``/2`` schema, in the caller's write transaction. An upgrade stamps its
    legacy rows between this and :func:`triggers_v3` (no trigger is installed yet).

    O-p2-1 (A6, R2-7, R2-8): SQLite cannot change a primary key in place, so ``effects`` is rebuilt to
    :data:`EFFECTS_V3` after the added columns: the old table is renamed away, the authored text created, every row
    copied with attempt 1, the old table dropped. The rebuild alone is skipped when ``attempt`` is already there; the
    indexes after it are made every time (``IF NOT EXISTS``). A unique index that a stored row violates (two holds on
    one link, P2) raises here, and the caller's transaction rolls the whole upgrade back."""
    for stmt in V3_TABLES + V74_TABLES + V74B_TABLES:
        conn.execute(stmt)
    for table, column, kind in V3_COLUMNS + V74_COLUMNS:
        if column not in {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {kind}")
    if "attempt" not in {r[1] for r in conn.execute("PRAGMA table_info(effects)")}:
        cols = ", ".join(KNOWN_COLUMNS["effects"][:-1])
        conn.execute("ALTER TABLE effects RENAME TO effects_v2")
        conn.execute(EFFECTS_V3)
        conn.execute(f"INSERT INTO effects ({cols}, attempt) SELECT {cols}, 1 FROM effects_v2")
        conn.execute("DROP TABLE effects_v2")
    for stmt in ATTEMPT_INDEXES:
        conn.execute(stmt)


def triggers_v3(conn: sqlite3.Connection) -> None:
    for stmt in ALL_TRIGGERS:
        conn.execute(stmt)


def write_tunables(conn: sqlite3.Connection) -> None:
    """The code's tunables into ``meta`` (inserted when missing, updated when they differ), in the caller's write
    transaction."""
    for key, value in TUNABLES.items():
        have = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        if have is None:
            conn.execute("INSERT INTO meta (key, value) VALUES (?, ?)", (key, str(value)))
        elif have[0] != str(value):
            conn.execute("UPDATE meta SET value = ? WHERE key = ?", (str(value), key))


def tunables_differ(conn: sqlite3.Connection) -> bool:
    """Some tunable row is missing or holds another value than the code's (:func:`write_tunables` would write)."""
    have = dict(conn.execute(f"SELECT key, value FROM meta WHERE key IN {_TUNE_KEYS}").fetchall())
    return any(have.get(key) != str(value) for key, value in TUNABLES.items())


def _v2_schema(conn: sqlite3.Connection) -> None:
    # V's own /2 tables, as V's opener makes them (levain.autonomic.db)
    for stmt in vdb._SCHEMA:
        conn.execute(stmt)
    for table, column, kind in vdb._ADDED_COLUMNS:
        if column not in {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {kind}")
    conn.execute(vdb._GRADUATION_STEPS)


def fingerprint(conn: sqlite3.Connection) -> str:
    """R13-9: sha256 over every ``sqlite_master`` row (tables, indexes, triggers, views) as SQLite holds its DDL;
    automatic indexes are left out by name (their sql is NULL; their UNIQUE constraint is in the table's text)."""
    rows = conn.execute("SELECT type, name, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_autoindex_%' "
                        "ORDER BY type, name").fetchall()
    return hashlib.sha256("\n".join(f"{t}|{n}|{q}" for t, n, q in rows).encode("utf-8")).hexdigest()


_EXPECTED: dict[str, str] = {}
_V2_COLUMNS: dict[str, frozenset[str]] = {}


def v2_columns(table: str) -> frozenset[str]:
    """The columns V's own ``/2`` opener gives ``table`` (:func:`_v2_schema`, built in memory); empty for a table it
    does not make."""
    if not _V2_COLUMNS:
        m = sqlite3.connect(":memory:", isolation_level=None)
        try:
            _v2_schema(m)
            for (name,) in m.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall():
                _V2_COLUMNS[name] = frozenset(r[1] for r in m.execute(f"PRAGMA table_info({name})"))
        finally:
            m.close()
    return _V2_COLUMNS.get(table, frozenset())


def expected_fingerprint() -> str:
    """The fingerprint this code gives a store: V's ``/2`` schema, then :func:`ddl_v3` and :func:`triggers_v3`, built
    in memory."""
    if "v3" not in _EXPECTED:
        m = sqlite3.connect(":memory:", isolation_level=None)
        try:
            m.execute("BEGIN")
            _v2_schema(m)
            ddl_v3(m)
            triggers_v3(m)
            _EXPECTED["v3"] = fingerprint(m)
            m.execute("ROLLBACK")
        finally:
            m.close()
    return _EXPECTED["v3"]


def invariant(conn: sqlite3.Connection) -> list[tuple[str, str]]:
    """D54's invariant read from SQLite alone: every row is a violation (kind, subject)."""
    return [tuple(r) for r in conn.execute(INVARIANT).fetchall()]


def census(conn: sqlite3.Connection) -> list[tuple[str, str]]:
    return [tuple(r) for r in conn.execute(LEGACY_CENSUS).fetchall()]


# ---- the opener -----------------------------------------------------------------------------------------------------
class StoreNeedsUpgrade(StoreFormatError):
    """A ``/1`` or ``/2`` store: this levain does not upgrade it (I15 is not built), and leaves it as it was."""


class StoreNewerThanLevain(StoreFormatError):
    """D93: the store's format is newer than this levain's (doctor: upgrade levain). An environment fault."""


class StoreSchemaMismatch(StoreFormatError):
    """D93 / R13-9: the store is ``/3`` but its DDL is not this code's. It is never rewritten. An environment fault."""


def _check_format(conn: sqlite3.Connection) -> None:
    marker = vdb.store_marker(conn)
    if marker != STORE_FORMAT_V3:
        raise StoreFormatError(f"store format {marker!r} is not {STORE_FORMAT_V3!r}")


def refuse_unless_this_v3(conn: sqlite3.Connection) -> None:
    """A database that holds tables is this code's ``/3`` store (format marker and fingerprint), else refused by why."""
    marker = vdb.store_marker(conn)
    n = format_n(marker)
    if marker is None:
        raise StoreFormatError("the database holds tables but no store format marker: it is not an "
                               "autonomic store, and is not taken over")
    if n is None:
        raise StoreFormatError(f"store format {marker!r} is not a levain-autonomic format")
    if n > 3:
        raise StoreNewerThanLevain(f"store-newer-than-levain: store format {marker!r} is newer than "
                                   f"this levain's {STORE_FORMAT_V3!r}; upgrade levain")
    if n < 3:
        raise StoreNeedsUpgrade(f"store-needs-upgrade: store format {marker!r} is older than "
                                f"{STORE_FORMAT_V3!r}, and this levain does not upgrade it: it is "
                                "left as it was")
    if fingerprint(conn) != expected_fingerprint():
        raise StoreSchemaMismatch(f"store-schema-mismatch: the store's DDL is not this levain's "
                                  f"({STORE_FORMAT_V3}); it is not rewritten")


def _holds_tables(conn: sqlite3.Connection) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchone() is not None


def preflight(path: Path, check: Callable[[sqlite3.Connection], None]) -> None:
    """A1-R3: ``check`` the database at ``path`` through a read-only connection (``mode=ro``, no PRAGMA set), before
    any durability PRAGMA can change it (:func:`open_durable` switches a database to WAL): a store ``check`` refuses
    is left as it was. The opener checks again on the durable connection, inside its own transactions."""
    conn = sqlite3.connect(Path(path).absolute().as_uri() + "?mode=ro", uri=True, isolation_level=None)
    try:
        check(conn)
    finally:
        conn.close()


def _preflight_v3(conn: sqlite3.Connection) -> None:
    # an empty database is the one AutonomicStore3 creates a store in
    if _holds_tables(conn):
        refuse_unless_this_v3(conn)


class AutonomicStore3:
    """A ``levain-autonomic/3`` store directory. The first connection creates the store in an empty database or opens
    one this code made (format and fingerprint checked, tunables written); every later transaction checks the format
    marker first, as :class:`levain.autonomic.db.AutonomicDB` does. Every connection runs with
    ``recursive_triggers = ON`` (D69: a REPLACE then fires the DELETE triggers too)."""

    def __init__(self, directory: Path | str) -> None:
        self.directory = Path(directory)
        self.path = self.directory / vdb.DB_NAME
        self._ready = False

    def _connect(self) -> sqlite3.Connection:
        if self._ready:
            if not self.path.exists():
                raise StoreFormatError(f"store {self.path} has disappeared")
        elif not self.directory.exists():
            self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not self._ready and self.path.exists():
            preflight(self.path, _preflight_v3)
        conn = open_durable(self.path)
        try:
            conn.execute("PRAGMA recursive_triggers = ON")
            if not self._ready:
                check_sqlite(conn)
                self._open(conn)
                self._ready = True
        except BaseException:
            conn.close()
            raise
        return conn

    @classmethod
    def _open(cls, conn: sqlite3.Connection) -> None:
        # O-a1c1-4: the open of a /3 store whose tunables equal the code's is a read transaction (deferred BEGIN): it
        # takes no write lock. Only a store to create or a tunable to write takes BEGIN IMMEDIATE, and every check
        # then runs again under that lock (another opener may have moved the store between the two).
        if cls._open_in(conn, write=False):
            cls._open_in(conn, write=True)

    @staticmethod
    def _open_in(conn: sqlite3.Connection, *, write: bool) -> bool:
        """The open's checks in one transaction; True when a read (``write=False``) found a write to do, after
        rolling its read back."""
        conn.execute("BEGIN IMMEDIATE" if write else "BEGIN")
        try:
            if not _holds_tables(conn):
                if not write:
                    _rollback(conn)
                    return True
                _v2_schema(conn)
                conn.execute("INSERT INTO meta (key, value) VALUES ('store_id', ?)", ("store-" + _random_hex(),))
                conn.execute("INSERT INTO meta (key, value) VALUES ('format', ?)", (STORE_FORMAT_V3,))
                write_tunables(conn)
                ddl_v3(conn)
                triggers_v3(conn)
            else:
                refuse_unless_this_v3(conn)
                if tunables_differ(conn):
                    if not write:
                        _rollback(conn)
                        return True
                    write_tunables(conn)
            if fingerprint(conn) != expected_fingerprint():
                raise StoreSchemaMismatch("store-schema-mismatch: the new store's DDL is not this levain's")
            conn.execute("COMMIT")
            return False
        except BaseException:
            _rollback(conn)
            raise

    @contextmanager
    def write(self) -> Iterator[sqlite3.Connection]:
        """One write transaction (``BEGIN IMMEDIATE``), format checked first."""
        with write_transaction(self._connect) as conn:
            _check_format(conn)
            yield conn

    @contextmanager
    def read(self) -> Iterator[sqlite3.Connection]:
        with read_transaction(self._connect) as conn:
            _check_format(conn)
            yield conn

    def meta(self, key: str) -> str | None:
        with self.read() as conn:
            row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None


def _random_hex() -> str:
    return os.urandom(16).hex()


# ---- the close path -------------------------------------------------------------------------------------------------
def _in_write(conn: sqlite3.Connection, what: str) -> None:
    if not conn.in_transaction:
        raise RuntimeError(f"{what} runs inside its caller's write transaction")


def close_subject(conn: sqlite3.Connection, *, subject: str, record: dict, state: str | None = None,
                  reason: str | None = None, result: str | None = None) -> str:
    """D92 / I11: write ``record`` as ``subject``'s receipt AND its terminal row, in one statement (the
    ``close_subject_do`` trigger refuses it unless exactly one terminal row carries the receipt). ``state`` is an
    effect's ``done`` | ``unknown`` | ``not_started`` (with result ``not_started:<why>``); ``reason`` a run's or
    request's close reason; ``result`` an effect's result text. The record is strict JSON (no NaN or Infinity: ``receipts_content`` refuses it, R13-8). Returns the
    receipt id (``record["id"]``); a record with no id is a ``ValueError`` before anything is written."""
    _in_write(conn, "close_subject")
    if not isinstance(record, dict) or record.get("id") is None:
        raise ValueError(f"close_subject: the receipt record for {subject} has no id: {record!r:.200}")
    text = json.dumps(record, ensure_ascii=False, allow_nan=False)
    seq = conn.execute("SELECT IFNULL(MAX(seq), 0) + 1 FROM gate_receipts").fetchone()[0]
    conn.execute("INSERT INTO close_subject (receipt_id, subject, record, seq, state, reason, result) "
                 "VALUES (?, ?, ?, ?, ?, ?, ?)", (record.get("id"), subject, text, seq, state, reason, result))
    return record["id"]


# the (terminal_state, effects.state) pairs a subject of each kind can end in (TERMINAL_MODES, by subject prefix)
CLOSE_STATES = {
    "run": tuple((t, None) for t in TERMINAL_MODES[("cancels", None)]),
    "request": tuple((t, None) for t in TERMINAL_MODES[("requests", None)]),
    "effect": (("fired", "done"), ("outcome_unknown", "unknown"), ("refused", "not_started")),
    "pending": tuple((t, None) for t in TERMINAL_MODES[("manual_pendings", "terminal")]),
}


class CloseUnbuildable(ValueError):
    """R13-8 / R14-3: an admission whose subject could not be closed in some terminal_state it can end in."""


def dry_run_close(conn: sqlite3.Connection, subject: str, record_for: Callable[[str], dict]) -> None:
    """R13-8 / R14-3, in the admitting write transaction, after the subject's row is written: for every terminal_state
    the subject can end in, build its receipt (``record_for(terminal_state)``, the builder the close itself calls)
    and run the real :func:`close_subject` inside a SAVEPOINT, then roll that back. Raises :class:`CloseUnbuildable`
    naming the state and the error when any close would fail; the caller's transaction then rolls back whole."""
    _in_write(conn, "dry_run_close")
    kind = subject.split(":", 1)[0]
    if kind not in CLOSE_STATES:
        raise ValueError(f"dry_run_close: unknown subject {subject!r}")
    if kind == "effect":
        _effect_key(subject)   # O-p2-1 A3: one attempt, one subject; a non-canonical spelling is refused, not closed
    for terminal_state, state in CLOSE_STATES[kind]:
        conn.execute("SAVEPOINT dry_run_close")
        try:
            close_subject(conn, subject=subject, record=record_for(terminal_state), state=state,
                          reason=None if kind in ("effect", "pending") else f"dry_run:{terminal_state}",
                          result="not_started:dry_run" if state == "not_started" else None)
        except BaseException as e:
            # A1-R4: a fault that rolled the transaction back (SQLITE_FULL, IOERR) took the savepoint with it; the
            # undo then fails, and that failure must not replace the fault that refuses the admission
            try:
                _undo_savepoint(conn)
            except sqlite3.Error as undo:
                _log.warning("autonomic store: dry_run_close savepoint not undone after %s: %s", type(e).__name__, undo)
            if isinstance(e, Exception):   # every builder or store fault refuses the admission
                raise CloseUnbuildable(f"close_unbuildable:{terminal_state}:{type(e).__name__}:{e}") from e
            raise
        _undo_savepoint(conn)   # a close that succeeded is undone, or its error is raised: its writes never stay


def _undo_savepoint(conn: sqlite3.Connection) -> None:
    conn.execute("ROLLBACK TO dry_run_close")
    conn.execute("RELEASE dry_run_close")


# ---- held counts (I5 / I11 / R13-6) ---------------------------------------------------------------------------------
def held_limit(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT value FROM meta WHERE key = 'held_limit'").fetchone()[0])


def count_held(conn: sqlite3.Connection, subject: str, reason: str, *, attempt: int | None = None) -> int:
    """Count one subject-specific hold of a live subject, in the caller's write transaction: born 1 at the subject's
    progress mark; +1 at the same mark; back to 1 when the mark moved. Returns the count (the caller closes the
    subject ``refused`` with reason ``held_limit:<reason>`` at :func:`held_limit`). ``reason`` is ``subject:<why>``.

    O-p2-1: ``attempt`` is the link attempt whose not_started close this count records (the witness a retry of the
    next attempt needs, R2-1); the held triggers refuse one that is not the link's latest, not_started attempt. A
    count of an attempt the row already names at the same mark returns the stored count and writes nothing (R3-3). A
    count with no attempt keeps the attempt the row names (P1)."""
    _in_write(conn, "count_held")
    if not isinstance(reason, str) or conn.execute("SELECT ? GLOB ?", (reason, SUBJECT_REASON)).fetchone()[0] != 1:
        raise ValueError(f"count_held: reason {reason!r} is not {SUBJECT_REASON!r} (a subject-specific fault)")
    if attempt is not None and (type(attempt) is not int or attempt < 1):
        raise ValueError(f"count_held: attempt {attempt!r} is not an int >= 1")
    mark = conn.execute(f"SELECT {_progress(':s')}", {"s": subject}).fetchone()[0]
    row = conn.execute("SELECT n, mark, attempt FROM held_counts WHERE subject = ?", (subject,)).fetchone()
    if row is None:
        conn.execute("INSERT INTO held_counts (subject, n, last_reason, mark, attempt) VALUES (?, 1, ?, ?, ?)",
                     (subject, reason, mark, attempt))
        return 1
    if attempt is not None and row[2] == attempt and row[1] == mark:
        return row[0]
    n = row[0] + 1 if mark == row[1] else 1
    conn.execute("UPDATE held_counts SET n = ?, mark = ?, last_reason = ?, attempt = ? WHERE subject = ?",
                 (n, mark, reason, row[2] if attempt is None else attempt, subject))
    return n


def hold_decision(conn: sqlite3.Connection, hold_id: str) -> tuple[int, str | None, str | None] | None:
    """R4-1: the decision a ``/3`` hold holds now, ``(generation, decided_by, decided_posture)``, the three values a
    retry's ``$.decision`` must carry (``effects_attempt_authority``); None for no such hold. ``/3`` only: a ``/2``
    ``holds`` table has no ``decisions`` column, so no shared ``/2`` read is changed for it."""
    row = conn.execute("SELECT decisions, decided_by, decided_posture FROM holds WHERE hold_id = ?",
                       (hold_id,)).fetchone()
    return None if row is None else (row[0], row[1], row[2])


def not_started_receipts(conn: sqlite3.Connection) -> frozenset[str]:
    """10.Q1: the receipt ids of every effects row ended ``not_started`` (each carries its receipt: D54). Their
    receipts say ``refused``: this set is what tells them apart for a by-mode reader (10.Q1 / R2-4 name
    ``gate_liveness``; no reader takes it yet, /3 is unwired)."""
    return frozenset(r for (r,) in conn.execute("SELECT receipt_id FROM effects WHERE state = 'not_started'"))


# ---- environment faults (R13-7, R14-2) ------------------------------------------------------------------------------
def utc_text(now: datetime | None = None) -> str:
    """A UTC instant with its offset (``2026-10-26T09:00:00+00:00``): ``unixepoch`` reads it whatever the local zone.
    A naive ``now`` is taken as UTC."""
    now = datetime.now(timezone.utc) if now is None else now
    now = now.replace(tzinfo=timezone.utc) if now.tzinfo is None else now.astimezone(timezone.utc)
    return now.isoformat(timespec="seconds")


def health_probe(conn: sqlite3.Connection, now: str) -> bool:
    """One write transaction on the fixed health row: True when the store takes writes (a fault just seen was the
    subject's), False when it does not (store-wide)."""
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("INSERT INTO env_health (k, at) VALUES (1, ?) ON CONFLICT (k) DO UPDATE SET at = excluded.at",
                     (now,))
        conn.execute("COMMIT")
        return True
    except sqlite3.DatabaseError:
        _rollback(conn)
        return False


def classify(conn: sqlite3.Connection, exc: BaseException, now: str) -> str:
    """After a failed transaction rolled back: ``integrity`` (an IntegrityError: the held-count path), ``subject``
    (another DatabaseError while the store takes writes) or ``store`` (it does not: the A2 pause). A non-database
    exception is raised again."""
    if isinstance(exc, sqlite3.IntegrityError):
        return "integrity"
    if not isinstance(exc, sqlite3.DatabaseError):
        raise exc
    return "subject" if health_probe(conn, now) else "store"


def record_fault(conn: sqlite3.Connection, subject: str, reason: str, now: datetime | None = None) -> bool:
    """Count one environment fault on ``subject`` in its OWN transaction (``conn`` has none open). Returns True iff
    this fault crossed the bound (``alerted_at`` is set in the same transaction: one alert per episode). The fault
    path never raises: a write that fails is logged and returns False. A clock that stepped back keeps the later
    ``last_at`` (R14-2)."""
    at = utc_text(now)
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            if conn.execute("SELECT 1 FROM env_faults WHERE subject = ?", (subject,)).fetchone() is None:
                conn.execute("INSERT INTO env_faults (subject, first_at, last_at, n, last_reason) VALUES (?, ?, ?, 1, ?)",
                             (subject, at, at, reason))
            else:
                # one statement (every right-hand side reads the old row): env_faults_move checks the whole move
                later = "CASE WHEN unixepoch(:at) > unixepoch(last_at) THEN :at ELSE last_at END"
                conn.execute(f"UPDATE env_faults SET n = n + 1, last_at = {later}, first_at = IFNULL(first_at, {later}), "
                             "last_reason = :reason WHERE subject = :subject",
                             {"at": at, "reason": reason, "subject": subject})
            crossed = conn.execute(f"SELECT 1 FROM ({ENV_FAULT_LISTING}) WHERE subject = ? AND alerted_at IS NULL",
                                   (subject,)).fetchone() is not None
            if crossed:
                conn.execute("UPDATE env_faults SET alerted_at = last_at WHERE subject = ?", (subject,))
            conn.execute("COMMIT")
            return crossed
        except BaseException:
            _rollback(conn)
            raise
    except Exception as e:  # noqa: BLE001 -- counting a fault must never become a second fault
        _log.error("autonomic store: environment fault on %s not counted (%s): %s", subject, type(e).__name__, e)
        return False


def record_success(conn: sqlite3.Connection, subject: str, now: datetime | None = None) -> None:
    """Progress on ``subject`` ends its episode (n = 0, by UPDATE; never a delete). No row, or n = 0: nothing. Never
    raises (logged)."""
    at = utc_text(now)
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute("UPDATE env_faults SET n = 0, first_at = NULL, alerted_at = NULL, last_at = CASE WHEN "
                         "unixepoch(?) > unixepoch(last_at) THEN ? ELSE last_at END WHERE subject = ? AND n > 0",
                         (at, at, subject))
            conn.execute("COMMIT")
        except BaseException:
            _rollback(conn)
            raise
    except Exception as e:  # noqa: BLE001
        _log.error("autonomic store: progress on %s not recorded (%s): %s", subject, type(e).__name__, e)


# ---- the orphan reaper (R13-1) --------------------------------------------------------------------------------------
def reap_orphans(conn: sqlite3.Connection, *, lease_held: Callable[[str, str], bool],
                 pending_lease_held: Callable[[str], bool]) -> tuple[list[str], dict[str, str]]:
    """The D57 repair, in the caller's write transaction: clear every leftover ``closing`` mark first (a standing pass
    for ``receipts_via_close``), then each orphan receipt in its own SAVEPOINT. Returns (marks cleared, {subject:
    :data:`ADOPTED` | :data:`IN_FLIGHT` | ``unadoptable: <the fault>``}): an unadoptable orphan stays an INVARIANT row,
    for doctor. A fault on one orphan (the store's refusal, a lease callback's ``OSError``, a malformed subject) is
    that orphan's outcome, and the repair goes on to the next."""
    _in_write(conn, "reap_orphans")
    cleared = clear_closing_left(conn)
    out: dict[str, str] = {}
    for rid, subject, ts in conn.execute(REAP_ORPHAN_RECEIPTS).fetchall():
        conn.execute("SAVEPOINT reap_orphan")
        try:
            out[subject] = adopt_orphan(conn, rid, subject, ts, lease_held=lease_held,
                                        pending_lease_held=pending_lease_held)
            conn.execute("RELEASE reap_orphan")
        except Exception as e:  # noqa: BLE001 -- one orphan's fault never stops the repair (D57)
            conn.execute("ROLLBACK TO reap_orphan")
            conn.execute("RELEASE reap_orphan")
            _log.warning("autonomic store: orphan receipt %s of %s not adopted (%s): %s", rid, subject,
                         type(e).__name__, e)
            out[subject] = f"unadoptable: {e}" if isinstance(e, sqlite3.Error) else \
                f"unadoptable: {type(e).__name__}: {e}"
    return cleared, out


# ---- the D57 reaper's pass (A1c1 part 2) ----------------------------------------------------------------------------
# Each selection, what it closes and how: (query, subject prefix, effect state, terminal_state, run close reason);
# _REAP_KEYS names the columns that key one row of a subject prefix's selection. The order is the pass's: an effect
# closed first can make its run an input of a later selection in the same pass, and a fenced run with an unknown or
# failed link is an input of both REAP_UNKNOWN_LINK_RUNS and REAP_MIDCHAIN_RUNS, so unknown-link (I12) runs first and
# closes it with its own reason.
REAP_PASS = (
    (REAP_EFFECTS, "effect", "unknown", "outcome_unknown", None),
    (REAP_PENDINGS, "pending", None, "outcome_unknown", None),
    (REAP_FENCED_RUNS, "run", None, "refused", "journal:fenced"),
    (REAP_UNKNOWN_LINK_RUNS, "run", None, "refused", "unknown-link"),
    (REAP_MIDCHAIN_RUNS, "run", None, "refused", "fenced-mid-chain"),
)
_REAP_KEYS = {"effect": ("run_id", "effect_id", "attempt"), "pending": ("pending_id",), "run": ("run_id",)}
CLOSED = "closed"


def _subject_of(kind: str, row: tuple) -> str:
    return effect_subject(row[0], row[1], row[2]) if kind == "effect" else f"{kind}:{row[0]}"


def reap(store: AutonomicStore3, *, lease_held: Callable[[str, str], bool],
         pending_lease_held: Callable[[str], bool],
         record_for: Callable[[str, str | None, str, str | None], dict]) -> dict[str, str]:
    """One D57 pass over a ``/3`` store: the orphan repair (:func:`reap_orphans`), then every live subject that can
    no longer progress (:data:`REAP_PASS`), each closed with one receipt in its OWN write transaction, so a fault on
    one never blocks the rest. Inside that transaction the subject is selected again and its lease tested (an
    ``intent`` effect by ``lease_held(run_id, effect_id)``, a ``firing`` pending by ``pending_lease_held(pending_id)``:
    an owner still inside it is :data:`IN_FLIGHT`, left as it is). ``record_for(subject, seal, terminal_state,
    reason)`` builds the receipt record from the subject's sealed row (the face builder, D56); a builder or store
    fault leaves that subject live for the next pass. Returns {subject: :data:`CLOSED` | :data:`IN_FLIGHT` |
    ``refused: <why>``} plus the orphan repair's outcomes (a subject the repair reported on is not selected again in
    this pass); a second pass over a quiet store returns {}."""
    out: dict[str, str] = {}
    with store.write() as conn:
        _cleared, orphans = reap_orphans(conn, lease_held=lease_held, pending_lease_held=pending_lease_held)
    out.update(orphans)
    for query, kind, state, terminal_state, reason in REAP_PASS:
        keyed = f"SELECT * FROM ({query}) WHERE " + " AND ".join(f"{k} = ?" for k in _REAP_KEYS[kind])
        with store.read() as conn:
            # a subject the orphan repair reported on keeps that outcome: it holds a receipt already, and its close
            # is the repair's (the store refuses a second receipt for it)
            candidates = []
            for r in conn.execute(query).fetchall():
                try:
                    subject = _subject_of(kind, r)
                except ValueError as e:   # a colon in a run id: left live, not selectable by subject
                    _log.error("autonomic store: reaper skips %s row %r: %s", kind, r[:len(_REAP_KEYS[kind])], e)
                    continue
                if subject not in orphans:
                    candidates.append((subject, tuple(r[:len(_REAP_KEYS[kind])])))
        for subject, key in candidates:
            try:
                with store.write() as conn:
                    row = conn.execute(keyed, key).fetchone()
                    if row is None:
                        continue   # closed or moved since the read
                    if kind == "effect" and lease_held(row[0], row[1]):
                        out[subject] = IN_FLIGHT
                        continue
                    if kind == "pending" and pending_lease_held(row[0]):
                        out[subject] = IN_FLIGHT
                        continue
                    seal = row[-1]
                    close_subject(conn, subject=subject, record=record_for(subject, seal, terminal_state, reason),
                                  state=state, reason=reason)
                    out[subject] = CLOSED
            except Exception as e:  # noqa: BLE001 -- one subject's fault never stops the pass (D57)
                _log.error("autonomic store: reaper left %s live (%s): %s", subject, type(e).__name__, e)
                out[subject] = f"refused: {type(e).__name__}: {e}"
    return out

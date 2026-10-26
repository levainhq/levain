"""levain.consent.queue — the consent queue: external agents' gates, their answers, their receipts.

A hook files an :class:`~levain.consent.record.ExternalGateRequest`; the request is ``pending`` until exactly
one of these ends it or moves it on:

  - ``allowed`` by a standing consent grant (:meth:`ConsentQueue.answer_by_grant`, Q4): a grant a fresh replay of
    the authority log holds, whose criteria (:func:`levain.consent.policy.grant_refusal`) hold for the request, the
    tool input it was filed for and the posture the agent reported. The row says it was answered by a grant
    (``via``) and stores which grant and the authority link that created or last renewed it. Never a chat
    launch's egress card (:data:`EGRESS_TOOL`, Q-R15-7): only a signed answer decides one;
  - ``allowed`` / ``denied``: an answer that VERIFIES (:meth:`ConsentQueue.answer`): a record binding this
    queue, the request's id, its digest, its expiry, the decision, when it was signed (before the expiry) and the
    head of the queue's receipt log when it was built (see the receipts paragraph below), signed in
    :data:`~levain.consent.record.ANSWER_NAMESPACE` by a key the replayed authority log holds unrevoked, of a
    class that covers the request's tier for an allow. A delivered "yes" that does not verify is no
    decision, never an allow (the 10-08 rule): the request stays pending;
  - ``expired``: its ``expires_at`` passed with no verified answer. Silence is NO decision: the hook outputs
    nothing and the agent's native dialog stands;
  - ``withdrawn``: the requester gave up (the human answered the native dialog, the hook stopped holding);
  - ``undecided`` (:meth:`ConsentQueue.mark_undecided`): the hook's hold ended with no verified answer and the
    action was NOT taken. The request is given a gap number, unique and never reused within its agent and
    session. It is the one state that is not terminal, with exactly one way out: a late RULING that verifies
    (:meth:`ConsentQueue.answer_late`), a record binding this queue, the request, its digest, its gap number,
    the decision, when it was signed and the receipt log's head, in :data:`~levain.consent.record.RULING_NAMESPACE` (never an answer's
    namespace, so neither verifies as the other), checked against the authority log exactly as an answer is,
    and signed no later than the queue's clock (read under the lock) plus :data:`RULING_SKEW`. Only the LATEST ruling on an identical call (agent, session, tool and
    input digest; the working directory is shown, never keyed, since a resumed session starts in its project
    directory while the call may have been made from below it) counts: a ruled allow pre-authorises that one call once
    (:meth:`ConsentQueue.take_ruled_allow`, the ONLY delivery of a ruled allow), for :data:`RULED_ALLOW_LAPSE`
    from when the queue applied it; a later ruling on the identical call voids every earlier one not yet taken, so
    a later deny takes the allow back and two allows give one run. :meth:`ConsentQueue.decision`,
    :meth:`ConsentQueue.mark_undecided` and :meth:`ConsentQueue.claim_rulings_for` return a ruling as a
    :class:`Ruling` (information: what was ruled, for the text the agent is told), never as a :class:`Decision`
    an adapter delivers.

A pending or undecided request can be snoozed (:meth:`ConsentQueue.snooze`): a time to ask the operator again,
recorded apart and unsigned, since it changes no authority, only when he is asked; a due re-ping is claimed by
one sweeper only (:meth:`ConsentQueue.claim_repings`). What a later resume of the agent's session needs (its
permission mode, how it was invoked, Codex's pid, the original working directory) is recorded beside the request
at filing (:meth:`ConsentQueue.resume_meta`), unsigned: it is never authority.

Away mode the operator sets with ``levain consent away on`` is an AWAY MARK here (:meth:`ConsentQueue.mark_away`,
unsigned: it only raises what the hook holds). Leaving it takes a LEAVE (:meth:`ConsentQueue.leave_away`): a record
binding this queue, the agent's latest mark and when it was signed, in :data:`~levain.consent.record.AWAY_NAMESPACE`,
by any enrolled, unrevoked key, verified as an answer is and again every time the hook reads it
(:meth:`ConsentQueue.away_mark`). An agent whose latest mark has no leave that verifies is away whatever its
settings file says: the hook reads away mode from here, not only from a file any process of the operator's can write.

What became of a request levain left to the agent's own dialog (withdrawn, expired, or answered in that dialog
while the hook still held it) is recorded apart, UNSIGNED, in ``dialog_outcomes`` (:meth:`ConsentQueue.record_ran`,
:meth:`ConsentQueue.close_unrun`, written by the agent's PostToolUse / Stop / UserPromptSubmit hooks): ``ran``
(the tool ran: a yes in the agent's dialog), ``ran_unasked`` (it ran within :data:`UNASKED_WITHIN` of levain
leaving it to the agent, too soon for a person: the agent's own standing approval) or ``did_not_run`` (a later
turn began or the turn ended without it running: a no, or abandoned; which is not known). An outcome never
touches the request's state, answer or signature, and is never authority: it is the record of a decision levain
did not make.

A refused answer or late ruling that is EVIDENCE is kept apart, in ``refused_answers``
(:meth:`ConsentQueue.refused_answers`): one that binds this queue, the request and its digest (a ruling also its
gap) and whose signature verifies under the key it names (an enrolled key's stored public key; for a key the
authority log does not hold, the key the SSHSIG embeds, whose fingerprint must be the named one), refused because
that key is not authority for it (``approver-not-authorized``) or, authorised, because it came after the hold ended
by the queue's clock or state (``approval-window-elapsed``; a ``signed_at`` after the hold is the signer's claim,
never lateness). Nothing else is kept: not a duplicate answer to a decided or withdrawn request, a stale receipts
head, an answer refused while the authority log cannot be replayed, a bad signature, a malformed record, or an
unenrolled passkey's assertion. A kept refusal never moves the request, never changes the refusal the signer is
given, is no receipt and no leaf of the receipts log, and is never authority; at most :data:`REFUSED_PER_REQUEST`
by enrolled keys and :data:`REFUSED_UNENROLLED_PER_REQUEST` by others are kept per request (counted before any
verify), each (request, signer, decision) once (a repeat costs no verify either), and keeping one waits at most
:data:`REFUSED_BUSY_MS` for the lock.

The row is an INDEX, never authority. A process that can write ``queue.db`` raw can set any state, so every
delivery or reading of a decision (:meth:`ConsentQueue.decision`, :meth:`ConsentQueue.await_decision`,
:meth:`ConsentQueue.take_ruled_allow`, :meth:`ConsentQueue.claim_rulings_for`) re-reads the stored answer or
ruling and its signature, checks it binds this queue, this request (and, for a ruling, the request's gap number) and the row's decision, and
re-verifies it against a FRESH replay of the authority log taken at delivery (the queue's ``authority``
source): a key revoked after it answered delivers nothing. A grant answer is re-verified the same way: the grant
must still be in a fresh replay, unexpired, its criteria still true for the tool input and posture the deliverer
holds. Anything that fails is ``None``, never an allow;
a raw write can only take a decision away (the native dialog then stands), never make one (one stated
exception, a deleted later deny, under known edges below).

A terminal request never changes again: SQL triggers refuse any update of a terminal row, any move but
``pending`` to a terminal state or to ``undecided`` and ``undecided`` to ``allowed``/``denied`` by a ruling, any
change of a request's fields, and any delete (a guard against a buggy writer; authority is the signature above).
Gap numbers, takes of a ruled allow, deliveries of a ruling's text, the resumes started to deliver one
(:mod:`levain.consent.delivery`), resume metadata, snoozes, away marks and their leaves, and session halts (:meth:`ConsentQueue.halt`, released in the transaction of the ruling on the halting request) are
append-only tables beside the request row, which they never change. Every transition writes its receipt
(built by :mod:`levain.autonomic.receipt`) in the same transaction, so a transition and its receipt can never disagree. Every expiry-conditioned transition reads the clock AFTER
its ``BEGIN IMMEDIATE`` holds the lock, and expires a due request before considering anything else.

The receipts are also a Merkle log, computed from the rows and never stored (:mod:`levain.consent.receipt_log`,
Q11): every answer and ruling record the queue hands a signer (:meth:`ConsentQueue.answer_payload`,
:meth:`ConsentQueue.ruling_payload`) carries ``receipts_head``, the log's head as a read of the receipts found it,
and the queue applies a signed one only when that head is still the head of its size over its receipts, checked
in the write transaction that applies it. So each signature attests every receipt before it under the signer's own
key, and a later rewrite of them (triggers dropped) refuses the next answer and fails
``levain consent receipts verify``. A record in the format from before the chaining (``.../1``) is refused when
applied (the request is answered again); one the queue APPLIED before the upgrade still delivers, its signature
re-verified exactly as before and only the head check skipped, and the verifier lists it as signed before
chaining. Delivery checks the head's shape, never the log (that is the apply's and the verifier's).

The store is ``<levain home>/consent/queue.db``, opened through :mod:`levain.autonomic.durable_sqlite` (WAL,
synchronous FULL); every write is one ``BEGIN IMMEDIATE``, serialized by SQLite. The directory is the
operator's alone (0700, refused otherwise). A store of the previous format (:data:`PRIOR_FORMAT`) is migrated
in place on open, in one transaction, its rows unchanged; any other format is refused, and a levain that knows
only the previous format refuses this one.

Known edges (each stated, none built against):
  - the take, the ruling-text claim and an answer read the authority log before taking the write lock: a key
    revoked between that read and the write still takes or applies; every later delivery re-verifies.
  - a process of an older levain that already opened this store (its ready flag cached) keeps writing it as
    the format it knew, after a newer one migrated it in place.
  - which ruling on a call is the latest is read from rows a raw writer can delete: deleting a later deny row
    (triggers dropped) makes an earlier untaken allow the latest again; ordering itself is checked against the
    signed ``signed_at``, so a reorder alone voids, never revives.

Stdlib only.
"""
from __future__ import annotations

import json
import os
import pwd
import re
import sqlite3
import time
import warnings
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from levain.authority import (
    _WEBAUTHN_SIG_TYPE, PASSKEY, AuthorityError, AuthoritySnapshot, ReplayCache, VerifierUnavailable,
    custody_mismatch, sshsig_public_key, verify_link_sig,
)
from levain.autonomic.authority import AuthorityScope
from levain.autonomic.durable_sqlite import open_durable, read_transaction, write_transaction
from levain.autonomic.durable_sqlite import rollback as _rollback
from levain.autonomic.receipt import build_action_face, build_gate_face, build_gate_verdict
from levain.cockpit.authority import RequestAuthority
from levain.cockpit.consent import ConsentError, parse_stamp, utc_stamp
from levain.cockpit.firejournal import _CLASS_RANK, _TIER_NEEDS
from levain.consent.policy import grant_refusal
from levain.consent.receipt_log import head as receipts_head
from levain.consent.receipt_log import LogUnreadable, head_problem, head_refusal
from levain.consent.record import (
    AGENTS, ANSWER_FORMAT, ANSWER_FORMAT_UNCHAINED, ANSWER_NAMESPACE, AWAY_LEAVE_FORMAT, AWAY_NAMESPACE, DECISIONS,
    GRANT_ANSWER_FORMAT, RULING_FORMAT, RULING_FORMAT_UNCHAINED, RULING_NAMESPACE, ExternalGateRequest,
    answer_message, answer_payload, away_leave_message, away_leave_payload, grant_answer_payload, redact_display,
    ruling_message, ruling_payload, tool_input_digest,
)

__all__ = ["DB_NAME", "INVOCATIONS", "MODEL_MAX", "MODEL_UNKNOWN", "PRIOR_FORMAT", "REFUSED_BUSY_MS", "REFUSED_KINDS", "REFUSED_PER_REQUEST",
           "REFUSED_REASONS", "REFUSED_UNENROLLED_PER_REQUEST", "REFUSED_SUBREASONS", "RULED_ALLOW_LAPSE", "RULING_SKEW", "SNOOZE_MAX_MINUTES",
           "STATES", "STORE_FORMAT", "TERMINAL", "AnswerRefused", "AuthoritySource", "AwayMark", "ConsentQueue", "Decision",
           "ANSWER_KEYS", "Gapped", "Marked", "QueueStoreError", "Reping", "Ruling", "StoreNotCurrent",
           "answer_refusal", "authority_source", "default_queue_dir", "gap_label", "session_handle", "signer_refusal"]

DB_NAME = "queue.db"
STORE_FORMAT = "levain-consent-queue/4"
# the format this one migrates from in place; the listing reads either
PRIOR_FORMAT = "levain-consent-queue/3"
STATES: tuple[str, ...] = ("pending", "allowed", "denied", "expired", "withdrawn", "undecided")
TERMINAL: frozenset[str] = frozenset({"allowed", "denied", "expired", "withdrawn"})
# the keys of a signed answer record (``ANSWER_FORMAT``); public so another store's answers are checked by the same set
ANSWER_KEYS = frozenset({"format", "queue_id", "request_id", "request_digest", "decision", "expires_at",
                         "signer_fp", "signed_at", "receipts_head"})
_ANSWER_KEYS = ANSWER_KEYS
_GRANT_ANSWER_KEYS = frozenset({"format", "queue_id", "request_id", "request_digest", "decision", "expires_at",
                                "grant_id", "grant_seq", "grant_hash", "posture"})
_RULING_KEYS = frozenset({"format", "queue_id", "request_id", "request_digest", "gap", "decision", "signer_fp",
                          "signed_at", "receipts_head"})
# an answer / ruling APPLIED in the format from before the receipts were chained (``.../1``, Q11) has no
# ``receipts_head``: it is never applied now, but one applied before the upgrade still delivers (its signature
# re-verified exactly as before; only the head check, which it cannot have, is skipped)
_ANSWER_KEYS_UNCHAINED = _ANSWER_KEYS - {"receipts_head"}
_RULING_KEYS_UNCHAINED = _RULING_KEYS - {"receipts_head"}
_LEAVE_KEYS = frozenset({"format", "queue_id", "mark_id", "agent", "decision", "signer_fp", "signed_at"})
# how a decided request was answered: a signed answer, a standing consent grant, or a late ruling on an undecided one
VIA: tuple[str, ...] = ("signature", "grant", "ruling")
# a ruled allow not taken by the call it rules on within this long of the queue applying it (its decided_at, the
# queue's own clock) pre-authorises nothing; nor does one applied this long or more after it was signed
RULED_ALLOW_LAPSE = timedelta(hours=24)
#: How long past ``expires_at`` (the end of the hook's hold) a pending request stays pending for the hook to
#: mark it undecided (:meth:`ConsentQueue.mark_undecided`) before a sweep or a read expires it. No answer is
#: applied once ``expires_at`` has passed; only the hook's undecided mark is.
UNDECIDED_GRACE = timedelta(seconds=60)
# how far ahead of the queue's clock a ruling's signed_at may be (two honest clocks disagree by seconds)
RULING_SKEW = timedelta(minutes=2)
# the longest a snooze may put off asking again: a week
SNOOZE_MAX_MINUTES = 7 * 24 * 60
# how the agent's session was invoked, recorded for a later resume (``claude -p``, a terminal, ``claude --bg``)
INVOCATIONS: tuple[str, ...] = ("-p", "interactive", "bg")
#: The longest model string a request records. A model id is agent-controlled text a reader shows, so only the
#: ASCII alphabet real ids use is recorded (see :func:`_model_of`); anything else is recorded as unknown.
MODEL_MAX = 200
_MODEL_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/@+\-]{0,%d}" % (MODEL_MAX - 1), re.ASCII)
#: What :meth:`ConsentQueue.model` reports for a model no source has given (DISPATCH F5: never inferred, never copied).
MODEL_UNKNOWN = "unknown"
_RESUME_KEYS = frozenset({"permission_mode", "invocation", "codex_pid", "cwd", "config_dir", "claude_pid",
                          "claude_started", "claude_flags", "codex_flags"})
_FIELDS = ("id", "agent", "agent_session_id", "event_id", "tool_name", "tool_input_digest", "display", "cwd",
           "tier", "received_at", "expires_at")
_ROW_COLUMNS = _FIELDS + ("state", "decided_at", "answer", "signature", "signer_fp", "note", "via")
_STATE_OF = {"allow": "allowed", "deny": "denied"}
_ACCESS_BY_OTHERS = 0o077
# what became of a request left to the agent's own dialog (unsigned; never a decision)
DIALOG_OUTCOMES: tuple[str, ...] = ("ran", "ran_unasked", "did_not_run")
# a tool that ran this soon after levain left its request to the agent was not answered by a person: Codex 0.162.1
# runs a call it holds a session approval for 0.1 s after levain withdraws the request, and a person's yes in its
# dialog came 12-13 s after [judged by levain seat lane H, 2026-10-17, against those two measured runs]
UNASKED_WITHIN = timedelta(seconds=0.5)
#: Why a refused answer or ruling was kept as evidence (:meth:`ConsentQueue.refused_answers`): its key is not one the
#: authority log holds active, of a class covering the tier for an allow; or it is authorised and verifies, but
#: reached the queue after the request's hold ended, by the queue's clock or state (never by its own ``signed_at``).
REFUSED_NOT_AUTHORIZED = "approver-not-authorized"
REFUSED_WINDOW_ELAPSED = "approval-window-elapsed"
REFUSED_REASONS: tuple[str, ...] = (REFUSED_NOT_AUTHORIZED, REFUSED_WINDOW_ELAPSED)
#: Which refusal of the key it was: ``not-enrolled``, ``revoked``, ``compromised`` (revoked with
#: ``compromised_since``) and ``class`` go with :data:`REFUSED_NOT_AUTHORIZED`, ``late`` with
#: :data:`REFUSED_WINDOW_ELAPSED`.
REFUSED_SUBREASONS: tuple[str, ...] = ("not-enrolled", "revoked", "compromised", "class", "late")
#: What was refused: an answer (:meth:`ConsentQueue.answer`) or a late ruling (:meth:`ConsentQueue.answer_late`).
REFUSED_KINDS: tuple[str, ...] = ("answer", "ruling")
#: The most refused answers kept per request by keys the authority log holds (``enrolled``): the first ones are
#: kept, later ones are refused as before and kept nowhere.
REFUSED_PER_REQUEST = 8
#: The most kept per request by keys the log does not hold, counted apart from :data:`REFUSED_PER_REQUEST`: anyone
#: can make a key, so throwaway keys never use up the room an enrolled key's evidence has.
REFUSED_UNENROLLED_PER_REQUEST = 4
#: How long keeping a refused answer waits for the queue's write lock, in ms: a refusal is never held up for the
#: 30 s every other write waits (:func:`levain.autonomic.durable_sqlite.open_durable`); past it, nothing is kept.
REFUSED_BUSY_MS = 2000
# a chat launch's egress card (chat_egress.cards.EGRESS_TOOL): filed with its card's own display, and decided only by
# a signed answer, never a standing grant (Q-R15-7)
EGRESS_TOOL = "levain_egress"

def _requests_table(name: str) -> str:
    return (f"CREATE TABLE IF NOT EXISTS {name} (id TEXT PRIMARY KEY, agent TEXT NOT NULL, "
            "agent_session_id TEXT NOT NULL, "
            "event_id TEXT NOT NULL, tool_name TEXT NOT NULL, tool_input_digest TEXT NOT NULL, display TEXT NOT NULL, "
            "cwd TEXT NOT NULL, tier TEXT NOT NULL, received_at TEXT NOT NULL, expires_at TEXT NOT NULL, "
            "state TEXT NOT NULL DEFAULT 'pending' "
            "CHECK (state IN ('pending','allowed','denied','expired','withdrawn','undecided')), "
            "decided_at TEXT, answer TEXT, signature TEXT, signer_fp TEXT, note TEXT, "
            "via TEXT CHECK (via IS NULL OR via IN ('signature','grant','ruling')))")


def _receipts_table(name: str) -> str:
    # one receipt per transition: an undecided request has its undecided receipt, then its ruling's
    return (f"CREATE TABLE IF NOT EXISTS {name} (seq INTEGER PRIMARY KEY AUTOINCREMENT, request_id TEXT NOT NULL "
            "REFERENCES requests(id), state TEXT NOT NULL, created_at TEXT NOT NULL, face TEXT NOT NULL, "
            "UNIQUE (request_id, state))")


_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
    _requests_table("requests"),
    "CREATE INDEX IF NOT EXISTS requests_pending ON requests (state, expires_at)",
    # the cockpit's read-only listing reads the last day's decided requests by this (levain.consent.listing)
    "CREATE INDEX IF NOT EXISTS requests_decided ON requests (decided_at)",
    # a hook's own event id names one permission event: a retry of it, at any time, is refused
    "CREATE UNIQUE INDEX IF NOT EXISTS requests_hook_event ON requests (agent, agent_session_id, event_id) "
    "WHERE event_id LIKE 'hook:%'",
    _receipts_table("receipts"),
    # terminal is terminal; the only moves are pending -> a terminal state (never by a ruling), pending -> undecided
    # (with no answer) and undecided -> allowed/denied by a ruling; a request's fields never change; nothing is
    # deleted; receipts are append-only
    "CREATE TRIGGER IF NOT EXISTS requests_frozen BEFORE UPDATE ON requests WHEN NOT ("
    "(OLD.state = 'pending' AND NEW.state IN ('allowed','denied','expired','withdrawn') AND NEW.via IS NOT 'ruling') "
    "OR (OLD.state = 'pending' AND NEW.state = 'undecided' AND NEW.via IS NULL AND NEW.answer IS NULL "
    "AND NEW.signature IS NULL AND NEW.signer_fp IS NULL) "
    "OR (OLD.state = 'undecided' AND NEW.state IN ('allowed','denied') AND NEW.via IS 'ruling')) "
    "OR NEW.id IS NOT OLD.id OR NEW.agent IS NOT OLD.agent "
    "OR NEW.agent_session_id IS NOT OLD.agent_session_id OR NEW.event_id IS NOT OLD.event_id "
    "OR NEW.tool_name IS NOT OLD.tool_name "
    "OR NEW.tool_input_digest IS NOT OLD.tool_input_digest OR NEW.display IS NOT OLD.display "
    "OR NEW.cwd IS NOT OLD.cwd OR NEW.tier IS NOT OLD.tier OR NEW.received_at IS NOT OLD.received_at "
    "OR NEW.expires_at IS NOT OLD.expires_at "
    "BEGIN SELECT RAISE(ABORT, 'a consent request moves only from pending to a terminal state or to undecided, "
    "and from undecided by a ruling'); END",
    "CREATE TRIGGER IF NOT EXISTS requests_kept BEFORE DELETE ON requests "
    "BEGIN SELECT RAISE(ABORT, 'consent requests are never deleted'); END",
    "CREATE TRIGGER IF NOT EXISTS receipts_frozen BEFORE UPDATE ON receipts "
    "BEGIN SELECT RAISE(ABORT, 'receipts are append-only'); END",
    "CREATE TRIGGER IF NOT EXISTS receipts_kept BEFORE DELETE ON receipts "
    "BEGIN SELECT RAISE(ABORT, 'receipts are append-only'); END",
    # one unsigned outcome per request left to the agent's dialog, written once (a table a store made before it
    # gains on its next open; the format is unchanged, so an older levain reading the store ignores it)
    "CREATE TABLE IF NOT EXISTS dialog_outcomes (request_id TEXT PRIMARY KEY REFERENCES requests(id), "
    "outcome TEXT NOT NULL CHECK (outcome IN ('ran','ran_unasked','did_not_run')), closed_at TEXT NOT NULL, "
    "event TEXT NOT NULL)",
    "CREATE TRIGGER IF NOT EXISTS dialog_outcomes_frozen BEFORE UPDATE ON dialog_outcomes "
    "BEGIN SELECT RAISE(ABORT, 'a dialog outcome is written once'); END",
    "CREATE TRIGGER IF NOT EXISTS dialog_outcomes_kept BEFORE DELETE ON dialog_outcomes "
    "BEGIN SELECT RAISE(ABORT, 'dialog outcomes are never deleted'); END",
    # the gap number an undecided request was given, once; per agent and session, never reused
    "CREATE TABLE IF NOT EXISTS gaps (request_id TEXT PRIMARY KEY REFERENCES requests(id), agent TEXT NOT NULL, "
    "agent_session_id TEXT NOT NULL, gap INTEGER NOT NULL CHECK (typeof(gap) = 'integer' AND gap >= 1), "
    "created_at TEXT NOT NULL)",
    "CREATE UNIQUE INDEX IF NOT EXISTS gaps_per_session ON gaps (agent, agent_session_id, gap)",
    "CREATE TRIGGER IF NOT EXISTS gaps_frozen BEFORE UPDATE ON gaps "
    "BEGIN SELECT RAISE(ABORT, 'a gap number is given once'); END",
    "CREATE TRIGGER IF NOT EXISTS gaps_kept BEFORE DELETE ON gaps "
    "BEGIN SELECT RAISE(ABORT, 'gap numbers are never deleted'); END",
    # the take's lookup: the ruled requests of one identical call (agent, session, tool, input digest; the trailing
    # cwd column is from when cwd was keyed)
    "CREATE INDEX IF NOT EXISTS requests_ruled ON requests (agent, agent_session_id, tool_name, tool_input_digest, "
    "cwd) WHERE via = 'ruling'",
    # a ruling's text delivered to its session (both allow and deny): once
    "CREATE TABLE IF NOT EXISTS ruling_deliveries (request_id TEXT PRIMARY KEY REFERENCES requests(id), "
    "agent TEXT NOT NULL, agent_session_id TEXT NOT NULL, delivered_at TEXT NOT NULL)",
    "CREATE TRIGGER IF NOT EXISTS ruling_deliveries_frozen BEFORE UPDATE ON ruling_deliveries "
    "BEGIN SELECT RAISE(ABORT, 'a ruling is delivered once'); END",
    "CREATE TRIGGER IF NOT EXISTS ruling_deliveries_kept BEFORE DELETE ON ruling_deliveries "
    "BEGIN SELECT RAISE(ABORT, 'ruling deliveries are never deleted'); END",
    # what a later resume of the request's session needs, recorded at filing (unsigned; never authority)
    "CREATE TABLE IF NOT EXISTS resume_meta (request_id TEXT PRIMARY KEY REFERENCES requests(id), "
    "meta TEXT NOT NULL, created_at TEXT NOT NULL)",
    "CREATE TRIGGER IF NOT EXISTS resume_meta_frozen BEFORE UPDATE ON resume_meta "
    "BEGIN SELECT RAISE(ABORT, 'resume metadata is written once'); END",
    "CREATE TRIGGER IF NOT EXISTS resume_meta_kept BEFORE DELETE ON resume_meta "
    "BEGIN SELECT RAISE(ABORT, 'resume metadata is never deleted'); END",
    # the model the agent's own hook payload named at filing (DISPATCH F5), unsigned and never authority: it is the
    # agent's claim, so it stays beside the sealed request and outside the receipts log. No row: unknown. A table a
    # store made before it gains on its next open; the format is unchanged, so an older levain ignores it
    "CREATE TABLE IF NOT EXISTS request_models (request_id TEXT PRIMARY KEY REFERENCES requests(id), "
    "model_requested TEXT NOT NULL, source TEXT NOT NULL CHECK (source IN ('hook')), created_at TEXT NOT NULL)",
    "CREATE TRIGGER IF NOT EXISTS request_models_frozen BEFORE UPDATE ON request_models "
    "BEGIN SELECT RAISE(ABORT, 'a request model is written once'); END",
    "CREATE TRIGGER IF NOT EXISTS request_models_kept BEFORE DELETE ON request_models "
    "BEGIN SELECT RAISE(ABORT, 'request models are never deleted'); END",
    # a ruled allow taken by the call it pre-authorised: at most once
    "CREATE TABLE IF NOT EXISTS ruled_takes (request_id TEXT PRIMARY KEY REFERENCES requests(id), "
    "taken_at TEXT NOT NULL, event TEXT)",
    "CREATE TRIGGER IF NOT EXISTS ruled_takes_frozen BEFORE UPDATE ON ruled_takes "
    "BEGIN SELECT RAISE(ABORT, 'a ruled allow is taken once'); END",
    "CREATE TRIGGER IF NOT EXISTS ruled_takes_kept BEFORE DELETE ON ruled_takes "
    "BEGIN SELECT RAISE(ABORT, 'takes of a ruled allow are never deleted'); END",
    # when to ask the operator again about a pending or undecided request (unsigned: it changes no authority)
    "CREATE TABLE IF NOT EXISTS snoozes (seq INTEGER PRIMARY KEY AUTOINCREMENT, request_id TEXT NOT NULL "
    "REFERENCES requests(id), reping_at TEXT NOT NULL, created_at TEXT NOT NULL)",
    "CREATE INDEX IF NOT EXISTS snoozes_request ON snoozes (request_id, seq)",
    "CREATE TABLE IF NOT EXISTS repings (snooze_seq INTEGER PRIMARY KEY REFERENCES snoozes(seq), "
    "repinged_at TEXT NOT NULL)",
    "CREATE TRIGGER IF NOT EXISTS snoozes_frozen BEFORE UPDATE ON snoozes "
    "BEGIN SELECT RAISE(ABORT, 'snoozes are append-only'); END",
    "CREATE TRIGGER IF NOT EXISTS snoozes_kept BEFORE DELETE ON snoozes "
    "BEGIN SELECT RAISE(ABORT, 'snoozes are append-only'); END",
    "CREATE TRIGGER IF NOT EXISTS repings_frozen BEFORE UPDATE ON repings "
    "BEGIN SELECT RAISE(ABORT, 'repings are append-only'); END",
    "CREATE TRIGGER IF NOT EXISTS repings_kept BEFORE DELETE ON repings "
    "BEGIN SELECT RAISE(ABORT, 'repings are append-only'); END",
    # a session halted on an undecided request (the irreversible / agent-config tier), and its release, written in
    # the transaction of the ruling on that request
    "CREATE TABLE IF NOT EXISTS halts (request_id TEXT PRIMARY KEY REFERENCES gaps(request_id), "
    "agent TEXT NOT NULL, agent_session_id TEXT NOT NULL, gap INTEGER NOT NULL, halted_at TEXT NOT NULL)",
    "CREATE INDEX IF NOT EXISTS halts_session ON halts (agent, agent_session_id, gap)",
    "CREATE TABLE IF NOT EXISTS halt_releases (request_id TEXT PRIMARY KEY REFERENCES halts(request_id), "
    "released_at TEXT NOT NULL, by_request_id TEXT NOT NULL REFERENCES requests(id))",
    "CREATE TRIGGER IF NOT EXISTS halts_frozen BEFORE UPDATE ON halts "
    "BEGIN SELECT RAISE(ABORT, 'halts are append-only'); END",
    "CREATE TRIGGER IF NOT EXISTS halts_kept BEFORE DELETE ON halts "
    "BEGIN SELECT RAISE(ABORT, 'halts are append-only'); END",
    "CREATE TRIGGER IF NOT EXISTS halt_releases_frozen BEFORE UPDATE ON halt_releases "
    "BEGIN SELECT RAISE(ABORT, 'halt releases are append-only'); END",
    "CREATE TRIGGER IF NOT EXISTS halt_releases_kept BEFORE DELETE ON halt_releases "
    "BEGIN SELECT RAISE(ABORT, 'halt releases are append-only'); END",
    # what became of a ruling's text after its delivery was claimed for a RESUME (unsigned: never authority), one
    # row per step, the latest one counting: ``launching`` (claimed for a resume about to start; left alone it is
    # UNCONFIRMED: it may have run), ``started`` (the resume was confirmed: delivered), ``failed`` (it did not start
    # or exited at once: deliverable again), ``hook`` (a failed one delivered by the session's next hook). A claim
    # with no row here was delivered by a hook.
    "CREATE TABLE IF NOT EXISTS ruling_resume_steps (seq INTEGER PRIMARY KEY AUTOINCREMENT, request_id TEXT NOT NULL "
    "REFERENCES ruling_deliveries(request_id), agent_session_id TEXT NOT NULL, "
    "outcome TEXT NOT NULL CHECK (outcome IN ('launching','started','failed','hook')), argv TEXT, cwd TEXT, "
    "detail TEXT, created_at TEXT NOT NULL)",
    "CREATE INDEX IF NOT EXISTS ruling_resume_steps_request ON ruling_resume_steps (request_id, seq)",
    "CREATE TRIGGER IF NOT EXISTS ruling_resume_steps_frozen BEFORE UPDATE ON ruling_resume_steps "
    "BEGIN SELECT RAISE(ABORT, 'ruling resume steps are append-only'); END",
    "CREATE TRIGGER IF NOT EXISTS ruling_resume_steps_kept BEFORE DELETE ON ruling_resume_steps "
    "BEGIN SELECT RAISE(ABORT, 'ruling resume steps are never deleted'); END",
    # away mode the operator set (`levain consent away on`: a mark, unsigned, since it only raises what the hook
    # holds) and its leave (signed in AWAY_NAMESPACE): an agent whose latest mark has no leave that verifies is away
    "CREATE TABLE IF NOT EXISTS away_marks (id TEXT PRIMARY KEY, agent TEXT NOT NULL, set_at TEXT NOT NULL)",
    "CREATE INDEX IF NOT EXISTS away_marks_agent ON away_marks (agent)",
    "CREATE TABLE IF NOT EXISTS away_leaves (mark_id TEXT PRIMARY KEY REFERENCES away_marks(id), "
    "left_at TEXT NOT NULL, payload TEXT NOT NULL, signature TEXT NOT NULL, signer_fp TEXT NOT NULL)",
    "CREATE TRIGGER IF NOT EXISTS away_marks_frozen BEFORE UPDATE ON away_marks "
    "BEGIN SELECT RAISE(ABORT, 'away marks are append-only'); END",
    "CREATE TRIGGER IF NOT EXISTS away_marks_kept BEFORE DELETE ON away_marks "
    "BEGIN SELECT RAISE(ABORT, 'away marks are append-only'); END",
    "CREATE TRIGGER IF NOT EXISTS away_leaves_frozen BEFORE UPDATE ON away_leaves "
    "BEGIN SELECT RAISE(ABORT, 'away leaves are append-only'); END",
    "CREATE TRIGGER IF NOT EXISTS away_leaves_kept BEFORE DELETE ON away_leaves "
    "BEGIN SELECT RAISE(ABORT, 'away leaves are append-only'); END",
    # INSERT OR REPLACE deletes the old row without firing a delete trigger, and the marks are ordered by rowid:
    # refuse a new row that reuses an id or an explicit rowid (one not given reads as -1 here, never a real row)
    "CREATE TRIGGER IF NOT EXISTS away_marks_no_replace BEFORE INSERT ON away_marks "
    "WHEN EXISTS (SELECT 1 FROM away_marks WHERE id = NEW.id OR rowid = NEW.rowid) "
    "BEGIN SELECT RAISE(ABORT, 'an away mark id is never reused'); END",
    "CREATE TRIGGER IF NOT EXISTS away_leaves_no_replace BEFORE INSERT ON away_leaves "
    "WHEN EXISTS (SELECT 1 FROM away_leaves WHERE mark_id = NEW.mark_id OR rowid = NEW.rowid) "
    "BEGIN SELECT RAISE(ABORT, 'an away leave is never replaced'); END",
    # an answer or late ruling refused as authority but kept as EVIDENCE (:meth:`ConsentQueue.refused_answers`):
    # never a receipt and never a leaf of the receipts log (a receipt is one per transition, and a refusal moves no
    # state), never authority; one row per (request, signer, decision), since a signature need not be deterministic
    "CREATE TABLE IF NOT EXISTS refused_answers (seq INTEGER PRIMARY KEY AUTOINCREMENT, request_id TEXT NOT NULL "
    "REFERENCES requests(id), kind TEXT NOT NULL CHECK (kind IN ('answer','ruling')), refused_at TEXT NOT NULL, "
    "reason_code TEXT NOT NULL CHECK (reason_code IN ('approver-not-authorized','approval-window-elapsed')), "
    "subreason TEXT NOT NULL CHECK (subreason IN ('not-enrolled','revoked','compromised','class','late')), "
    "enrolled INTEGER NOT NULL CHECK (enrolled IN (0, 1)), detail TEXT NOT NULL, "
    "decision TEXT NOT NULL, signer_fp TEXT NOT NULL, signed_at TEXT NOT NULL, record TEXT NOT NULL, "
    "signature TEXT NOT NULL, CHECK ((reason_code = 'approval-window-elapsed') = (subreason = 'late')), "
    "UNIQUE (request_id, signer_fp, decision))",
    "CREATE TRIGGER IF NOT EXISTS refused_answers_frozen BEFORE UPDATE ON refused_answers "
    "BEGIN SELECT RAISE(ABORT, 'refused answers are append-only'); END",
    "CREATE TRIGGER IF NOT EXISTS refused_answers_kept BEFORE DELETE ON refused_answers "
    "BEGIN SELECT RAISE(ABORT, 'refused answers are append-only'); END",
)
# what an away mark is read from (:meth:`ConsentQueue.away_mark`): a current-format store holding neither was made
# before them (no mark); one holding only one of them is damaged
_AWAY_TABLES = frozenset({"away_marks", "away_leaves"})
_AUTHORITY_NOW_UNREADABLE = "the authority log cannot be replayed now"
# the previous format's triggers on the two tables a migration rebuilds (the schema above recreates them)
_REBUILT_TRIGGERS = ("requests_frozen", "requests_kept", "receipts_frozen", "receipts_kept")


def _migrate_from_prior(conn: sqlite3.Connection) -> None:
    """Inside the caller's write transaction: rebuild ``requests`` (its state and via checks) and ``receipts``
    (its uniqueness, per request and state) under the new definitions, copying every row with its rowid and
    seq, and mark the store this format. SQLite cannot alter a CHECK or drop a column's UNIQUE in place, hence
    the copy. The caller then runs the schema, which recreates the indexes and triggers dropped with the old
    tables."""
    for trigger in _REBUILT_TRIGGERS:
        conn.execute(f"DROP TRIGGER IF EXISTS {trigger}")
    before = {t: conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in ("requests", "receipts")}
    cols = ", ".join(_ROW_COLUMNS)
    conn.execute(_requests_table("requests_next"))
    conn.execute(f"INSERT INTO requests_next (rowid, {cols}) SELECT rowid, {cols} FROM requests ORDER BY rowid")
    conn.execute("DROP TABLE requests")
    conn.execute("ALTER TABLE requests_next RENAME TO requests")
    rcols = "seq, request_id, state, created_at, face"
    conn.execute(_receipts_table("receipts_next"))
    conn.execute(f"INSERT INTO receipts_next ({rcols}) SELECT {rcols} FROM receipts ORDER BY seq")
    conn.execute("DROP TABLE receipts")
    conn.execute("ALTER TABLE receipts_next RENAME TO receipts")
    after = {t: conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in ("requests", "receipts")}
    if after != before:
        raise QueueStoreError(f"the migration from {PRIOR_FORMAT} copied {after} rows of {before}")
    conn.execute("UPDATE meta SET value = ? WHERE key = 'format'", (STORE_FORMAT,))


def _model_of(value: Any) -> str | None:
    """``value`` as a model string to record, or ``None``: a string that is wholly a model id in the ASCII alphabet
    of :data:`_MODEL_RE` (so at most :data:`MODEL_MAX` characters, and no control, bidirectional or unpaired-surrogate
    character can pass)."""
    return value if isinstance(value, str) and _MODEL_RE.fullmatch(value) else None


# how deep a signed record may nest when it is copied: an answer or ruling nests one level (``receipts_head``), so
# anything deeper is no record, refused before Python's recursion limit is near (L3 r2: a phone client posts it)
_DETACH_DEPTH = 8


def _detached(value: Any, depth: int = 0) -> Any:
    """A plain-data copy of a signed record as given, read once: every mapping a ``dict``, every list or tuple a
    ``list``, recursively; any other value kept as it is (the checks refuse what a record cannot hold). A caller's
    mapping that changes after this read changes nothing the queue checks or applies. Nesting past
    :data:`_DETACH_DEPTH` raises :class:`AnswerRefused`."""
    if depth > _DETACH_DEPTH:
        raise AnswerRefused("the signed record nests deeper than any answer or ruling record")
    if isinstance(value, Mapping):
        return {k: _detached(v, depth + 1) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_detached(v, depth + 1) for v in value]
    return value


class QueueStoreError(RuntimeError):
    """The store cannot be trusted: another format, a directory others can reach, one that vanished."""


class StoreNotCurrent(QueueStoreError):
    """A read that needs the current store's tables found none to read: an empty database, a store of
    :data:`PRIOR_FORMAT` not yet migrated, or one lacking a table (``_read_as_is`` with ``whole``)."""


class AnswerRefused(Exception):
    """An answer that is not authority. The request is unchanged (or, for a late answer, now expired)."""


@dataclass(frozen=True)
class Decision:
    """A verified answer: what the hook delivers. ``decision`` is ``allow`` or ``deny``. ``request_digest`` is
    the digest of the request the answer signed: an adapter delivers only when it is the digest of the request
    it filed for the input it is holding (``ExternalGateRequest.digest``). ``via`` says how it was answered:
    ``signature`` (``signer_fp`` is the key) or ``grant`` (``signer_fp`` is None; ``grant_id`` names the standing
    grant, ``grant_seq``/``grant_hash`` the authority link that created or last renewed it, ``posture`` the
    agent's reported posture it matched), or ``ruling`` (a late ruling on an undecided request: ``signer_fp`` is
    the key, ``gap`` the request's gap number)."""
    request_id: str
    decision: str
    signer_fp: str | None
    decided_at: str
    request_digest: str
    via: str = "signature"
    grant_id: str | None = None
    grant_seq: int | None = None
    grant_hash: str | None = None
    posture: str | None = None
    gap: int | None = None


@dataclass(frozen=True)
class Reping:
    """A snoozed request whose time to ask the operator again has come: ``seq`` names the snooze
    (claimed once by :meth:`ConsentQueue.claim_repings`), ``state`` is the request's state now (pending or
    undecided)."""
    seq: int
    request_id: str
    reping_at: str
    state: str


@dataclass(frozen=True)
class Ruling:
    """A late ruling on an undecided request, re-verified when read: INFORMATION (what was ruled, for the text the
    agent is told), never a deliverable decision. It is deliberately not a :class:`Decision`: an adapter delivers
    only a ``Decision``, and the one delivery of a ruled allow is :meth:`ConsentQueue.take_ruled_allow`. ``ruling``
    is ``allow`` or ``deny``; ``taken`` says whether a ruled allow has been taken by the call it ruled on;
    ``decided_at`` is when the queue applied it (its own clock), ``signed_at`` what the signer signed."""
    request_id: str
    ruling: str
    signer_fp: str
    decided_at: str
    signed_at: str
    request_digest: str
    gap: int
    taken: bool = False


@dataclass(frozen=True)
class Marked:
    """What :meth:`ConsentQueue.mark_undecided` did, so the hook can word its deny truthfully. ``state`` is the
    request's state after the call (``None`` when there is no sealed request of that id); ``gap`` its gap number
    when it has one; ``reason`` says why in a sentence; ``halted`` whether its session is halted on it.
    ``decision`` is what decided it before the deadline: a :class:`Decision` for a signed or grant answer that
    re-verifies (deliverable, as :meth:`ConsentQueue.decision` delivers it), a :class:`Ruling` for a late ruling
    (information only), ``None`` otherwise."""
    state: str | None
    gap: int | None
    reason: str
    halted: bool = False
    decision: "Decision | Ruling | None" = None


@dataclass(frozen=True)
class AwayMark:
    """An agent's latest away mark (:meth:`ConsentQueue.away_mark`): its id and when it was set; ``left`` whether a
    leave of it verifies now; ``leave_by`` the key a stored leave names (``None`` when there is none) and
    ``leave_refusal`` why that leave does not verify (``None`` when it does or there is none). The agent is away
    while ``left`` is false."""
    id: str
    agent: str
    set_at: str
    left: bool
    leave_by: str | None = None
    leave_refusal: str | None = None


@dataclass(frozen=True)
class Gapped:
    """A request that was given a gap number: the request, its gap, its state now (``undecided``, or
    ``allowed``/``denied`` once ruled) and when it went undecided."""
    request: ExternalGateRequest
    gap: int
    state: str
    undecided_at: str


def session_handle(agent_session_id: str) -> str:
    """A short handle for an agent session in what the operator reads and types: the LAST 6 characters of its id
    (hyphens dropped), and for a subagent's key (``<session>#agent:<id>``) ``+`` the last 6 of the subagent's id.
    The tail, not the head: a UUIDv7 (Codex's session ids) leads with a millisecond clock, so two sessions started in
    the same minute share their first characters, while the tail is random in v7 and v4 alike."""
    base, sep, sub = agent_session_id.partition("#agent:")
    return base.replace("-", "")[-6:] + (f"+{sub.replace('-', '')[-6:]}" if sep else "")


def gap_label(gap: int, agent_session_id: str) -> str:
    """A gap as the operator names it (``levain consent rule 3@a1b2c3``): ``#<gap>@<session handle>``. Gap numbers
    start at 1 in every session, so a bare number names one gap only while no other session has it."""
    return f"#{gap}@{session_handle(agent_session_id)}"


def class_covers(key_class: str, tier: str) -> bool:
    """True when a key of ``key_class`` may ALLOW a request of ``tier`` (a deny needs only an enrolled key)."""
    have = _CLASS_RANK.get(key_class)
    return have is not None and tier in _TIER_NEEDS and have >= _TIER_NEEDS[tier]


def queue_dir_for(environ: Mapping[str, str]) -> Path:
    """``<levain home>/consent`` as ``environ`` names it: ``$LEVAIN_HOME/consent`` if set (``~`` expanded under
    ``environ["HOME"]``), else ``$HOME/.levain/consent``, made absolute (as
    ``levain.autonomic.db.default_store_dir`` does for the autonomic store). Reads nothing of this process's own
    environment; with no ``HOME`` in ``environ``, the password database's home, as ``os.path.expanduser`` does."""
    home = environ.get("LEVAIN_HOME")
    if home and home.startswith("~") and not (home == "~" or home.startswith("~/")):
        base = Path(home).expanduser()      # ~user: the password database's, no $HOME read
    elif home and not home.startswith("~"):
        base = Path(home)
    else:
        user = environ["HOME"] if "HOME" in environ else pwd.getpwuid(os.getuid()).pw_dir
        user = user.rstrip("/") or "/"
        base = Path(user + home[1:]) if home else Path(user) / ".levain"
    return (base / "consent").resolve()


def default_queue_dir() -> Path:
    """:func:`queue_dir_for` this process's environment. Not for the core path: the hook entry points call
    :func:`queue_dir_for` with the environment they bound."""
    return queue_dir_for(os.environ)


def answer_refusal(request: ExternalGateRequest, queue_id: str, payload: Any, signature: Any, authority: Any, *,
                   ssh_keygen: Path | str | None = None, unchained_ok: bool = False) -> str | None:
    """Why ``payload``/``signature`` is not authority for ``request`` under ``authority``; ``None`` when it
    verifies. The one check an answer passes to be stored AND again every time it is delivered, in any store that
    holds levain consent answers; applying one also checks its ``receipts_head`` against that store's receipts
    (:func:`~levain.consent.receipt_log.head_refusal`, inside the applying write transaction, as
    :meth:`ConsentQueue.answer` does), which this does not. ``unchained_ok`` (delivery and the verifier,
    never the apply): an answer in the format from before the chaining is checked as that format, every
    check but the head's, since it has none. ``authority`` is an :class:`AuthoritySnapshot` or any object whose
    ``key(fingerprint)`` returns ``None`` or a key with ``public_key``, ``fingerprint``, ``active``, ``key_class`` and
    ``custody``; ``ssh_keygen`` is the verifier binary (default: levain's own resolution). Every check, and the
    bytes the signature is verified over, read one plain-dict copy of ``payload`` taken first; a caller stores what
    it parsed, never a mapping that can change after this returns."""
    if isinstance(payload, Mapping):
        payload = dict(payload)
    why = _answer_record_refusal(request, queue_id, payload, unchained_ok=unchained_ok)
    if why is not None:
        return why
    return signer_refusal(request, payload, signature, authority, noun="answer", encode_record=answer_message,
                          namespace=ANSWER_NAMESPACE, ssh_keygen=ssh_keygen)


def _answer_record_refusal(request: ExternalGateRequest, queue_id: str, payload: Any, *, unchained_ok: bool = False,
                           window: bool = True) -> str | None:
    """Every check of :func:`answer_refusal` but the signer's: why ``payload`` is not an answer record binding
    this queue and ``request``. ``window`` false (a refused answer read as evidence,
    :meth:`ConsentQueue._refused_reason`) skips only the check that it was signed before the request's hold ended."""
    unchained = isinstance(payload, Mapping) and payload.get("format") == ANSWER_FORMAT_UNCHAINED
    if unchained and not unchained_ok:
        return (f"the answer is in format {ANSWER_FORMAT_UNCHAINED!r}, signed before the queue's receipts were "
                "chained: it covers no receipts head; answer the request again")
    if not isinstance(payload, Mapping) \
            or set(payload) != (_ANSWER_KEYS_UNCHAINED if unchained else _ANSWER_KEYS):
        return "the answer is not an answer record"
    if not unchained:
        if payload["format"] != ANSWER_FORMAT:
            return f"answer format {payload['format']!r} is not {ANSWER_FORMAT!r}"
        problem = head_problem(payload["receipts_head"])
        if problem is not None:
            return f"the answer's {problem}"
    if payload["queue_id"] != queue_id:
        return "the answer names another queue"
    if payload["request_id"] != request.id:
        return "the answer is for another request"
    if payload["request_digest"] != request.digest:
        return "the answer signs another version of this request"
    if payload["expires_at"] != request.expires_at:
        return "the answer names another expiry"
    signed_at = payload["signed_at"]
    try:
        parse_stamp(signed_at)
    except ConsentError:
        return "the answer's signed_at is not a timestamp"
    if signed_at < request.received_at:
        return "the answer was signed before the request was filed"
    if window and signed_at >= request.expires_at:
        return "the answer was signed after the request expired"
    decision = payload["decision"]
    if decision not in DECISIONS:
        return f"decision {decision!r} is not one of {DECISIONS}"
    return None


def signer_refusal(request: ExternalGateRequest | None, payload: Mapping[str, Any], signature: Any, authority: Any, *,
                   noun: str, encode_record: Callable[[dict[str, Any]], bytes], namespace: str,
                   ssh_keygen: Path | str | None = None) -> str | None:
    """The key and signature checks an answer, a ruling and an away leave share: the named signer is a key
    ``authority`` holds active, of a class covering the request's tier for an allow (an allow with no
    ``request`` is refused), and ``signature`` verifies over the record's bytes in ``namespace``. Like
    :func:`answer_refusal`, it reads one plain-dict copy of ``payload`` taken first."""
    if not isinstance(payload, Mapping):
        return f"the {noun} is not a record"
    payload = dict(payload)
    if "decision" not in payload or "signer_fp" not in payload:
        return f"the {noun} names no decision or no signer"
    signer_fp = payload["signer_fp"]
    key = authority.key(signer_fp) if isinstance(signer_fp, str) else None
    why = _key_refusal(key, payload["decision"], request, noun=noun)
    if why is not None:
        return why[1]
    return _signature_refusal(key.public_key, key.fingerprint, key.custody, payload, signature, noun=noun,
                              encode_record=encode_record, namespace=namespace, ssh_keygen=ssh_keygen)


def _key_refusal(key: Any, decision: Any, request: ExternalGateRequest | None, *,
                 noun: str) -> tuple[str | None, str] | None:
    """Why ``key`` (``None``: the log does not hold it) is not authority for ``decision`` on ``request``, as
    ``(subreason, refusal)`` with the subreason one of :data:`REFUSED_SUBREASONS` (``None`` for an allow that
    names no request, never a kept refusal); ``None`` when it is authority. :func:`signer_refusal` gives the
    refusal, :meth:`ConsentQueue._evidence` keeps both: one text for both."""
    if key is None:
        return "not-enrolled", "the signer is not an enrolled key"
    if not key.active:
        # answer_refusal's contract keys need not carry compromised_since (only the kept subreason reads it)
        compromised = getattr(key, "compromised_since", None) is not None
        return ("compromised" if compromised else "revoked"), "the signer's key is revoked"
    if decision == "allow" and request is None:
        return None, f"an allow {noun} names no request"
    if decision == "allow" and not class_covers(key.key_class, request.tier):
        return "class", f"key class {key.key_class} does not cover {request.tier}"
    return None


def _signature_refusal(public_key: str, fingerprint: str, custody: str | None, payload: Mapping[str, Any],
                       signature: Any, *, noun: str, encode_record: Callable[[dict[str, Any]], bytes], namespace: str,
                       ssh_keygen: Path | str | None = None) -> str | None:
    """Why ``signature`` does not verify over the record's bytes in ``namespace`` by ``public_key`` (whose
    fingerprint is ``fingerprint``); ``None`` when it does. A passkey's (``custody``) own checks run first."""
    if not isinstance(signature, str) or not signature:
        return f"the {noun} carries no signature"
    # the custody that picks the checks below is the stored one, held equal to the one the key material shows
    problem = custody_mismatch(public_key, custody)
    if problem is not None:
        return problem
    if custody == PASSKEY:
        # a passkey's checks that ssh-keygen does not make (user verification, the page's origin), on every
        # verification, not only where the phone port converted the assertion
        from levain.consent.passkey import answer_signature_problem

        problem = answer_signature_problem(signature, public_key)
        if problem is not None:
            return problem
    try:
        message = encode_record(dict(payload))
    except ConsentError as exc:
        return f"the {noun} cannot be encoded: {exc}"
    kwargs: dict[str, Any] = {} if ssh_keygen is None else {"ssh_keygen": ssh_keygen}
    try:
        ok = verify_link_sig(message, signature, public_key=public_key, fingerprint=fingerprint,
                             namespace=namespace, **kwargs)
    except VerifierUnavailable as exc:
        return f"the {noun} could not be verified: {exc}"
    return None if ok else f"the {noun}'s signature does not verify"


class AuthoritySource:
    """The trusted authority source for a queue; make it with :func:`authority_source`. Each call opens the
    cockpit database read-only, begins a read transaction and returns the verified log head that
    :meth:`levain.cockpit.authority.RequestAuthority.capture` (``levain/cockpit/authority.py``, the one
    snapshot builder, DESIGN §3.4) takes in it: the anchor file read first, then the replay, then the replay
    checked against that anchor, so an append committing in between can only put the log ahead of the anchor.
    ``anchor=None`` checks no anchor, so the replay is only as trustworthy as the database file: production
    passes the real anchor. Raises on any failure: a verifier that cannot run during the replay as
    :class:`levain.authority.VerifierUnavailable` (a machine fault, never a verdict on the log), capture's refusal (a
    refused log or anchor, an empty or uninitialised log) as :class:`levain.authority.AuthorityError`, both with
    capture's reason, a journal read as ``sqlite3.Error``; the queue turns any of them into no decision.

    A queue takes only this class (not a subclass, not any callable): freshness is the queue's guarantee,
    never a promise a caller's ``lambda: snapshot`` could break."""

    __slots__ = ("_uri", "_anchor", "_kw")

    def __init__(self, cockpit_db: Path | str, *, anchor: Path | None, cache: ReplayCache | None = None,
                 ssh_keygen: Path | str | None = None) -> None:
        self._uri = Path(cockpit_db).resolve().as_uri() + "?mode=ro"
        self._anchor = None if anchor is None else Path(anchor)
        self._kw: dict[str, Any] = {"cache": cache}
        if ssh_keygen is not None:
            self._kw["ssh_keygen"] = ssh_keygen

    def __call__(self) -> AuthoritySnapshot:
        conn = sqlite3.connect(self._uri, uri=True, isolation_level=None, timeout=30.0)
        try:
            conn.execute("BEGIN")
            got = RequestAuthority.capture(conn, now=datetime.now(timezone.utc), anchor=self._anchor, **self._kw)
            conn.execute("COMMIT")
        finally:
            conn.close()
        if got.verifier_unavailable:
            raise VerifierUnavailable(got.authority_error or "the authority log could not be verified")
        if got.authority is None or got.authority_error is not None:
            raise AuthorityError(got.authority_error or "the authority log yields no head")
        return got.authority

    def head(self) -> tuple[int, str] | None:
        """The log's stored head ``(seq, hash)``, read without a replay (:func:`levain.authority.log_head`): cheap
        enough to compare inside another store's write lock against a snapshot this source returned before it."""
        from levain.authority import log_head

        # a short timeout: this read runs inside another store's write lock, and a head it cannot read is "moved"
        conn = sqlite3.connect(self._uri, uri=True, isolation_level=None, timeout=2.0)
        try:
            return log_head(conn)
        finally:
            conn.close()


def authority_source(cockpit_db: Path | str, *, anchor: Path | None, cache: ReplayCache | None = None,
                     ssh_keygen: Path | str | None = None) -> AuthoritySource:
    """The trusted authority source for a queue (see :class:`AuthoritySource`)."""
    return AuthoritySource(cockpit_db, anchor=anchor, cache=cache, ssh_keygen=ssh_keygen)


def _now_utc(clock: Callable[[], datetime]) -> datetime:
    now = clock()
    if now.tzinfo is None or now.utcoffset() is None:
        raise ConsentError("the queue's clock returned a naive datetime")
    return now


class ConsentQueue:
    """The queue in ``directory`` (default :func:`default_queue_dir`). Every operation opens its own
    connection, so one object is safe across threads and several processes may share the store.

    ``authority`` is the trusted source every answer and every delivered decision is verified against
    (:func:`authority_source`), called afresh each time: never a snapshot a caller captured earlier."""

    def __init__(self, directory: Path | str | None = None, *, authority: AuthoritySource, home: Path | str,
                 clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
                 ssh_keygen: Path | str | None = None) -> None:
        if type(authority) is not AuthoritySource:
            raise ConsentError("authority must be the AuthoritySource authority_source() returns, which replays "
                               "the authority log on every call; a snapshot or another callable is refused")
        self.directory = Path(directory) if directory is not None else default_queue_dir()
        self.path = self.directory / DB_NAME
        self._authority = authority
        self.home = Path(home)    # where a grant's ``~`` targets and home check are read (grant_refusal), never $HOME
        self._clock = clock
        self._ssh_keygen = ssh_keygen
        self._ready = False
        # a stored leave's verdict by its whole row (:meth:`away_mark`): one verify per queue, so per hook process
        self._leave_verdicts: dict[tuple[Any, ...], str | None] = {}

    # --- store ---------------------------------------------------------------------------------------
    def _check_directory(self) -> None:
        if not self.directory.exists():
            self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)   # the umask can only narrow it
        st = os.lstat(self.directory)
        if not os.path.isdir(self.directory) or os.path.islink(self.directory):
            raise QueueStoreError(f"consent queue directory {self.directory} is not a directory")
        if st.st_uid != os.getuid():
            raise QueueStoreError(f"consent queue directory {self.directory} is not owned by uid {os.getuid()}")
        if st.st_mode & _ACCESS_BY_OTHERS:
            raise QueueStoreError(f"consent queue directory {self.directory} is reachable by group or others "
                                  f"(mode {st.st_mode & 0o777:o}); it must be 0700")

    def _connect(self) -> sqlite3.Connection:
        if self._ready:
            if not self.path.exists():
                raise QueueStoreError(f"consent queue {self.path} has disappeared")
        else:
            self._check_directory()
        conn = open_durable(self.path)
        try:
            if not self._ready:
                os.chmod(self.path, 0o600)
                self._ensure_schema(conn)
                self._ready = True
        except BaseException:
            conn.close()
            raise
        return conn

    @staticmethod
    def _ensure_schema(conn: sqlite3.Connection) -> None:
        conn.execute("BEGIN IMMEDIATE")
        try:
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
            if tables and ("meta" not in tables or conn.execute(
                    "SELECT 1 FROM meta WHERE key = 'format'").fetchone() is None):
                raise QueueStoreError("the database holds tables but no consent-queue format marker")
            row = conn.execute("SELECT value FROM meta WHERE key = 'format'").fetchone() if tables else None
            if row is not None and row[0] == PRIOR_FORMAT:
                _migrate_from_prior(conn)
            elif row is not None and row[0] != STORE_FORMAT:
                raise QueueStoreError(f"store format {row[0]!r} is not {STORE_FORMAT!r}")
            elif row is not None and _AWAY_TABLES - tables and _AWAY_TABLES & tables:
                # never recreated: an empty table read as no mark would drop away mode
                raise QueueStoreError(f"the store is damaged: it lacks {', '.join(sorted(_AWAY_TABLES - tables))}")
            for stmt in _SCHEMA:
                conn.execute(stmt)
            if row is None:
                conn.execute("INSERT INTO meta (key, value) VALUES ('format', ?)", (STORE_FORMAT,))
            conn.execute("INSERT OR IGNORE INTO meta (key, value) VALUES ('queue_id', ?)",
                         ("cq-" + os.urandom(16).hex(),))
            conn.execute("COMMIT")
        except BaseException:
            _rollback(conn)
            raise

    @contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        with write_transaction(self._connect) as conn:
            yield conn

    @contextmanager
    def _read(self) -> Iterator[sqlite3.Connection]:
        with read_transaction(self._connect) as conn:
            yield conn

    @contextmanager
    def _read_as_is(self, tables: frozenset[str], *, whole: bool = False) -> Iterator[sqlite3.Connection | None]:
        """A read snapshot of the store as it stands, opened read-only: never created, migrated or given its schema
        (no write lock, no sync). ``None`` for an empty database (one the queue has not set up yet), a store of
        :data:`PRIOR_FORMAT`, which predates ``tables``, or a current-format store holding none of ``tables`` (made
        before them); raises :class:`QueueStoreError` for any other format, a database with no format marker, and a
        current-format store holding some of ``tables`` but not all (damaged, never read as holding nothing).
        With ``whole`` (a reader that needs ``tables`` themselves, never their absence) each of those ``None``
        cases and the damaged one raise :class:`StoreNotCurrent` instead, saying which it is."""
        self._check_directory()
        conn = sqlite3.connect(self.path.resolve().as_uri() + "?mode=ro", uri=True, timeout=30.0,
                               isolation_level=None)
        try:
            conn.execute("PRAGMA busy_timeout = 30000")
            conn.execute("BEGIN")
            have = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
            row = conn.execute("SELECT value FROM meta WHERE key = 'format'").fetchone() if "meta" in have else None
            if whole and not have:
                raise StoreNotCurrent(f"{self.path} holds no consent queue (an empty database)")
            if whole and row is not None and row[0] == PRIOR_FORMAT:
                raise StoreNotCurrent(
                    f"{self.path} is in format {PRIOR_FORMAT!r}, from before {STORE_FORMAT!r}, and is read only "
                    "once migrated: `levain consent pending` migrates it in place")
            if not have or (row is not None and row[0] == PRIOR_FORMAT):
                conn.execute("COMMIT")
                yield None
                return
            if row is None or row[0] != STORE_FORMAT:
                raise QueueStoreError(f"{self.path} is not a consent queue store: format "
                                      f"{None if row is None else row[0]!r} is not {STORE_FORMAT!r}")
            if whole and not tables <= have:
                raise StoreNotCurrent(f"{self.path} is damaged: it lacks {', '.join(sorted(tables - have))}")
            if not tables & have:
                conn.execute("COMMIT")
                yield None
                return
            if not tables <= have:
                raise QueueStoreError(f"{self.path} is damaged: it lacks {', '.join(sorted(tables - have))}")
            yield conn
            conn.execute("COMMIT")
        finally:
            conn.close()

    @property
    def queue_id(self) -> str:
        with self._read() as conn:
            return conn.execute("SELECT value FROM meta WHERE key = 'queue_id'").fetchone()[0]

    def _fresh_authority(self) -> AuthoritySnapshot | None:
        """A replay taken now, or ``None`` when the log cannot be replayed or checked."""
        try:
            snap = self._authority()
        except (AuthorityError, sqlite3.Error, OSError, ValueError):
            return None
        return snap if isinstance(snap, AuthoritySnapshot) else None

    # --- filing --------------------------------------------------------------------------------------
    @staticmethod
    def _resume_record(resume: Mapping[str, Any] | None) -> str | None:
        """``resume`` checked and encoded, or ``None`` for none; raises :class:`ConsentError` on a bad one."""
        if resume is None:
            return None
        if not isinstance(resume, Mapping) or not set(resume) <= _RESUME_KEYS or "invocation" not in resume:
            raise ConsentError(f"resume must be a mapping of {sorted(_RESUME_KEYS)} naming the invocation")
        if resume["invocation"] not in INVOCATIONS:
            raise ConsentError(f"resume invocation must be one of {INVOCATIONS}: {resume['invocation']!r}")
        for k in ("permission_mode", "cwd", "config_dir", "claude_started", "claude_flags", "codex_flags"):
            if k in resume and resume[k] is not None and not (isinstance(resume[k], str) and resume[k]):
                raise ConsentError(f"resume {k} must be a non-empty string or None")
        for k in ("codex_pid", "claude_pid"):
            pid = resume.get(k)
            if pid is not None and (type(pid) is not int or pid < 1):
                raise ConsentError(f"resume {k} must be a positive integer or None")
        return json.dumps(dict(resume), sort_keys=True, ensure_ascii=False)

    def file(self, *, agent: str, agent_session_id: str, tool_name: str, tool_input: Any, cwd: str,
             hold: timedelta, tier: str = "T2", event_id: str | None = None,
             resume: Mapping[str, Any] | None = None, model: Any = None) -> ExternalGateRequest:
        """File one gate an agent is waiting on; its hold (``expires_at``) ends ``hold`` after now, after which no
        answer applies and only the hook's :meth:`mark_undecided` does until it expires. ``tier`` must be one an
        allow can be signed at (``T2`` or ``T3``). ``event_id`` is the hook's own unique id for this
        permission event when its agent has one; without it the request gets an unpredictable one. Either
        way two filings are two requests, never one returned twice: a hook event id is unique per agent and
        session in the store, so filing the same event again raises, at any time. ``resume`` is what a later
        resume of the session needs (``permission_mode``, ``invocation`` one of :data:`INVOCATIONS`,
        ``codex_pid`` / ``claude_pid`` the agent's process, ``claude_started`` / ``claude_flags`` that process's
        start time and the option names on its command line (never their values) as the OS reported them,
        ``codex_flags`` the same option names for a Codex process, ``cwd`` the directory the session was started in,
        ``config_dir`` the agent's config directory, ``None`` for its default), written with the request in its transaction and
        read back by :meth:`resume_meta`; unsigned, never authority. ``model`` is the model the agent's own hook
        payload named, recorded beside the request in the same transaction and read back by :meth:`model`; one
        that is not a usable string (:func:`_model_of`) is recorded as nothing. Unsigned, never authority."""
        meta = self._resume_record(resume)
        named = _model_of(model)
        if tier not in _TIER_NEEDS:
            raise ConsentError(f"tier must be one of {tuple(_TIER_NEEDS)}: {tier!r}")
        if not isinstance(hold, timedelta) or hold <= timedelta(0):
            raise ConsentError("hold must be a positive timedelta")
        if event_id is None:
            event = "rand:" + os.urandom(16).hex()
        elif isinstance(event_id, str) and event_id:
            event = "hook:" + event_id
        else:
            raise ConsentError("event_id must be a non-empty string")
        if tool_name == EGRESS_TOOL:
            # a chat launch's egress card: its own display, in a fixed field order, never cut, after the card's whole
            # check and its filing's agent, tier, hold and event id (chat_egress.cards.filed_display)
            from levain.chat_egress.cards import CardError, filed_display
            try:
                display = filed_display(tool_input, agent=agent, tier=tier, hold=hold, event_id=event_id)
            except CardError as exc:
                raise ConsentError(f"a {EGRESS_TOOL} request must be a card: {exc}") from None
        else:
            display = redact_display(tool_name, tool_input)
        now = _now_utc(self._clock)
        request = ExternalGateRequest.create(
            agent=agent, agent_session_id=agent_session_id, event_id=event, tool_name=tool_name,
            tool_input_digest=tool_input_digest(tool_input), display=display,
            cwd=cwd, tier=tier, received_at=utc_stamp(now), expires_at=utc_stamp(now + hold))
        try:
            with self._write() as conn:
                conn.execute(f"INSERT INTO requests ({', '.join(_FIELDS)}) VALUES "
                             f"({', '.join('?' for _ in _FIELDS)})", tuple(request.fields()[f] for f in _FIELDS))
                if meta is not None:
                    conn.execute("INSERT INTO resume_meta (request_id, meta, created_at) VALUES (?, ?, ?)",
                                 (request.id, meta, request.received_at))
                if named is not None:
                    conn.execute("INSERT INTO request_models (request_id, model_requested, source, created_at) "
                                 "VALUES (?, ?, 'hook', ?)", (request.id, named, request.received_at))
        except sqlite3.IntegrityError:
            raise ConsentError(f"request {request.id} (event {event}) is already filed: one event is filed once"
                               ) from None
        return request

    def resume_meta(self, request_id: str) -> dict[str, Any] | None:
        """What was recorded at filing for a later resume of the request's session (:meth:`file`'s ``resume``),
        or ``None``. Unsigned: a hint for how to resume, never authority."""
        with self._read() as conn:
            row = conn.execute("SELECT meta FROM resume_meta WHERE request_id = ?", (request_id,)).fetchone()
        if row is None:
            return None
        try:
            got = json.loads(row[0])
        except ValueError:
            return None
        return got if isinstance(got, dict) else None

    def model(self, request_id: str) -> dict[str, str]:
        """The model on the request (DISPATCH F5): ``requested``, the model the agent's hook payload named at filing
        (``source`` ``"hook"``: the agent's own claim, unsigned), or :data:`MODEL_UNKNOWN` with ``source`` ``"none"``
        when it named none; ``answered`` is :data:`MODEL_UNKNOWN`, since only a gateway's response metadata can
        say which model answered and none is read here. Never authority, and outside the receipts log."""
        with self._read() as conn:
            row = conn.execute("SELECT model_requested, source FROM request_models WHERE request_id = ?",
                               (request_id,)).fetchone()
        if row is None:
            return {"requested": MODEL_UNKNOWN, "answered": MODEL_UNKNOWN, "source": "none"}
        return {"requested": row[0], "answered": MODEL_UNKNOWN, "source": row[1]}

    # --- reading -------------------------------------------------------------------------------------
    @staticmethod
    def _request_of(row: Mapping[str, Any]) -> ExternalGateRequest | None:
        try:
            request = ExternalGateRequest(**{f: row[f] for f in _FIELDS})
            return request if request.seal_matches() else None
        except (ConsentError, TypeError, ValueError):
            return None

    def _row(self, conn: sqlite3.Connection, request_id: str) -> dict[str, Any] | None:
        cur = conn.execute(f"SELECT {', '.join(_FIELDS)}, state, decided_at, answer, signature, signer_fp, via "
                           "FROM requests WHERE id = ?", (request_id,))
        row = cur.fetchone()
        if row is None:
            return None
        return dict(zip([d[0] for d in cur.description], row))

    def get(self, request_id: str) -> tuple[ExternalGateRequest, str] | None:
        """The request and its state, after expiring it if its hold ended :data:`UNDECIDED_GRACE` ago or more
        (a request past ``expires_at`` inside the grace reads ``pending``; no answer applies to it). ``None`` if unknown or its seal
        does not match (a row someone edited is never answered). Takes the write lock only when the row is
        actually due to expire."""
        with self._read() as conn:
            row = self._row(conn, request_id)
        if row is None:
            return None
        if row["state"] == "pending" and self._expiry_due(row["expires_at"], utc_stamp(_now_utc(self._clock))):
            self._expire_one(request_id)
            with self._read() as conn:
                row = self._row(conn, request_id)
            if row is None:
                return None
        request = self._request_of(row)
        return None if request is None else (request, row["state"])

    def pending(self) -> list[ExternalGateRequest]:
        """The open requests, oldest first, after a sweep (sealed rows only)."""
        self.sweep()
        with self._read() as conn:
            cur = conn.execute(f"SELECT {', '.join(_FIELDS)} FROM requests WHERE state = 'pending' "
                               "ORDER BY received_at, rowid")
            names = [d[0] for d in cur.description]
            rows = [dict(zip(names, r)) for r in cur.fetchall()]
        return [r for r in (self._request_of(row) for row in rows) if r is not None]

    def receipt(self, request_id: str) -> dict[str, Any] | None:
        """The request's latest receipt (an undecided request that was ruled on has two; see :meth:`receipts`)."""
        got = self.receipts(request_id)
        return got[-1] if got else None

    def receipts(self, request_id: str) -> list[dict[str, Any]]:
        """Every receipt of the request, oldest first: one per transition."""
        with self._read() as conn:
            rows = conn.execute("SELECT state, created_at, face FROM receipts WHERE request_id = ? ORDER BY seq",
                                (request_id,)).fetchall()
        return [{"state": r[0], "created_at": r[1], "face": json.loads(r[2])} for r in rows]

    _REFUSED_COLUMNS = ("seq", "request_id", "kind", "refused_at", "reason_code", "subreason", "enrolled", "detail",
                        "decision", "signer_fp", "signed_at", "record", "signature")

    def refused_answers(self, request_id: str) -> list[dict[str, Any]]:
        """Every refused answer or late ruling kept as evidence for the request, oldest first: ``kind`` one of
        :data:`REFUSED_KINDS`, ``reason_code`` one of :data:`REFUSED_REASONS`, ``subreason`` one of
        :data:`REFUSED_SUBREASONS`, ``enrolled`` whether the authority log held the key when it was refused,
        ``detail`` why it was refused (for ``approver-not-authorized`` the key's refusal, which the signer may not
        have been given: a late answer's signer is told the request expired), ``record`` the canonical signed bytes as text
        and ``signature`` the signature over them, which verified under the key ``signer_fp`` names. Evidence that
        an attempt was made and refused, never authority: no signed head covers these rows."""
        with self._read() as conn:
            rows = conn.execute(f"SELECT {', '.join(self._REFUSED_COLUMNS)} FROM refused_answers "
                                "WHERE request_id = ? ORDER BY seq", (request_id,)).fetchall()
        return [{**dict(zip(self._REFUSED_COLUMNS, r)), "enrolled": bool(r[6])} for r in rows]

    def gap(self, request_id: str) -> int | None:
        """The gap number the request was given when it went undecided, or ``None``."""
        with self._read() as conn:
            got = self._gap_of(conn, request_id)
        return None if got is None else got[0]

    @staticmethod
    def _gap_of(conn: sqlite3.Connection, request_id: str) -> tuple[int, str] | None:
        row = conn.execute("SELECT gap, created_at FROM gaps WHERE request_id = ?", (request_id,)).fetchone()
        return None if row is None or type(row[0]) is not int else (row[0], row[1])

    def _gapped(self, where: str, args: tuple[Any, ...]) -> list[Gapped]:
        with self._read() as conn:
            cur = conn.execute(f"SELECT {', '.join('r.' + f for f in _FIELDS)}, r.state, g.gap, g.created_at "
                               "FROM gaps g JOIN requests r ON r.id = g.request_id WHERE " + where +
                               " ORDER BY g.created_at, g.agent, g.agent_session_id, g.gap", args)
            names = [d[0] for d in cur.description]
            rows = [dict(zip(names, r)) for r in cur.fetchall()]
        out: list[Gapped] = []
        for row in rows:
            request = self._request_of(row)
            if request is not None and type(row["gap"]) is int:
                out.append(Gapped(request, row["gap"], row["state"], row["created_at"]))
        return out

    def undecided_for(self, agent: str | None = None, agent_session_id: str | None = None) -> list[Gapped]:
        """The undecided requests (sealed rows only), oldest gap first; of one agent and/or one session when
        named."""
        where, args = "r.state = 'undecided'", ()
        if agent is not None:
            where, args = where + " AND g.agent = ?", args + (agent,)
        if agent_session_id is not None:
            where, args = where + " AND g.agent_session_id = ?", args + (agent_session_id,)
        return self._gapped(where, args)

    def gapped(self, agent: str | None = None, *, since: datetime | None = None) -> list[Gapped]:
        """Every request that was given a gap number (undecided or ruled; sealed rows only), oldest gap first; of one
        agent when named, and with ``since`` only those still undecided or that went undecided at or after it."""
        where, args = "1 = 1", ()
        if agent is not None:
            where, args = where + " AND g.agent = ?", args + (agent,)
        if since is not None:
            where, args = where + " AND (r.state = 'undecided' OR g.created_at >= ?)", args + (utc_stamp(since),)
        return self._gapped(where, args)

    def request_by_gap(self, agent: str, agent_session_id: str, gap: int) -> Gapped | None:
        """The request this agent and session gave gap number ``gap``, in whatever state it is now (undecided, or
        ruled), or ``None``."""
        if type(gap) is not int:
            return None
        got = self._gapped("g.agent = ? AND g.agent_session_id = ? AND g.gap = ?", (agent, agent_session_id, gap))
        return got[0] if got else None

    # --- transitions ---------------------------------------------------------------------------------
    @staticmethod
    def _face(request: ExternalGateRequest, state: str, signer_fp: str | None,
              grant: Mapping[str, Any] | None = None, gap: int | None = None) -> dict[str, Any]:
        common: dict[str, Any] = dict(context_id=request.id, query_text=request.display, query_date=request.received_at[:10],
                      producers=[request.agent])
        if grant is not None:
            gid = grant["grant_id"]
            return build_gate_face(
                **common, gate=build_gate_verdict(verdict="approved", by="binding", binding_id=gid),
                authority=AuthorityScope(grantor="binding", binding_id=gid,
                                         grant=f"standing-grant:{gid}@{grant['grant_seq']}:{grant['grant_hash']} "
                                               f"posture={grant['posture']}"),
                terminal_state="fired", downstream_id=request.id)
        if state in ("allowed", "denied"):
            grant_text = f"signed-answer:{signer_fp}" if gap is None else f"signed-ruling:{signer_fp} gap=#{gap}"
            return build_gate_face(
                **common,
                gate=build_gate_verdict(verdict="approved" if state == "allowed" else "denied", by="human"),
                authority=AuthorityScope(grantor="human", grant=grant_text),
                terminal_state="fired" if state == "allowed" else "refused", downstream_id=request.id)
        face = build_action_face(**common)          # no decision: the gate slice stays null
        if state in ("expired", "undecided"):
            face["terminal_state"] = "timed_out"
        return face

    def _transition(self, conn: sqlite3.Connection, request: ExternalGateRequest, state: str, *, stamp: str,
                    answer: str | None = None, signature: str | None = None, signer_fp: str | None = None,
                    note: str | None = None, via: str | None = None,
                    grant: Mapping[str, Any] | None = None, from_state: str = "pending",
                    gap: int | None = None) -> bool:
        """One ``from_state -> state`` move plus its receipt, inside the caller's write transaction. False when
        the request is no longer in ``from_state``: nothing is written. ``grant`` is the grant answer record when
        a standing grant answered (its receipt names the grant and its link); ``gap`` the gap number a ruling
        rules on (its receipt names it)."""
        if conn.execute("UPDATE requests SET state = ?, decided_at = ?, answer = ?, signature = ?, signer_fp = ?, "
                        "note = ?, via = ? WHERE id = ? AND state = ?",
                        (state, stamp, answer, signature, signer_fp, note, via, request.id, from_state)
                        ).rowcount != 1:
            return False
        conn.execute("INSERT INTO receipts (request_id, state, created_at, face) VALUES (?, ?, ?, ?)",
                     (request.id, state, stamp, json.dumps(self._face(request, state, signer_fp, grant, gap),
                                                           ensure_ascii=False, sort_keys=True)))
        return True

    @staticmethod
    def _expiry_due(expires_at: str, stamp: str) -> bool:
        """A pending request is expired by a read or a sweep only :data:`UNDECIDED_GRACE` after its hold ended:
        the hook's undecided mark at the deadline takes precedence over the expire."""
        try:
            return parse_stamp(expires_at) + UNDECIDED_GRACE <= parse_stamp(stamp)
        except ConsentError:
            return True

    def _settle(self, conn: sqlite3.Connection, request_id: str, *, expire: bool = True
                ) -> tuple[ExternalGateRequest | None, str | None, str]:
        """Inside a held write lock: read the clock NOW, expire the request if it is due (:meth:`_expiry_due`;
        never when ``expire`` is false, the hook's undecided mark), and return the sealed request (or ``None``),
        its state after that, and the stamp every further transition in this transaction uses."""
        stamp = utc_stamp(_now_utc(self._clock))
        row = self._row(conn, request_id)
        request = None if row is None else self._request_of(row)
        if row is None or request is None:
            return None, None, stamp
        state = row["state"]
        if expire and state == "pending" and self._expiry_due(request.expires_at, stamp):
            self._transition(conn, request, "expired", stamp=stamp)
            state = "expired"
        return request, state, stamp

    def _expire_one(self, request_id: str) -> None:
        with self._write() as conn:
            self._settle(conn, request_id)

    def sweep(self) -> int:
        """Expire every pending request whose hold ended :data:`UNDECIDED_GRACE` ago or more; returns how many."""
        done = 0
        with self._write() as conn:
            now = _now_utc(self._clock)
            stamp = utc_stamp(now)
            cur = conn.execute(f"SELECT {', '.join(_FIELDS)} FROM requests WHERE state = 'pending' "
                               "AND expires_at <= ?", (utc_stamp(now - UNDECIDED_GRACE),))
            names = [d[0] for d in cur.description]
            for raw in cur.fetchall():
                request = self._request_of(dict(zip(names, raw)))
                if request is not None and self._transition(conn, request, "expired", stamp=stamp):
                    done += 1
        return done

    # --- what the agent's own dialog did (unsigned) -----------------------------------------------------------
    def record_ran(self, *, agent: str, agent_session_id: str, tool_name: str, tool_input: Any,
                   event: str, tool_use_id: str | None = None) -> tuple[str, str] | None:
        """The agent ran ``tool_name`` with ``tool_input`` in this session (its PostToolUse): the request of this
        agent and session for that tool and that exact input (its digest) that levain did not decide (pending,
        withdrawn or expired) and that has no outcome yet gets ``ran``, or ``ran_unasked`` when the tool ran
        within :data:`UNASKED_WITHIN` of levain leaving it; a pending one is also withdrawn. Which request: the
        one whose own tool-use id is ``tool_use_id`` when both carry one (a request carrying another id is a
        different call and is never taken); else the NEWEST, a still-pending one first: the call that ran is the
        latest retry, and an earlier identical request that was declined must not be recorded as run. Returns
        (request id, outcome), or ``None`` when no such request is open: a call levain never filed (a session
        approval Claude Code no longer asks about) is never given one."""
        digest = tool_input_digest({} if tool_input is None else tool_input)
        use_id = tool_use_id if isinstance(tool_use_id, str) and tool_use_id else None
        with self._write() as conn:
            stamp = utc_stamp(_now_utc(self._clock))
            where = ("FROM requests r WHERE agent = ? AND agent_session_id = ? "
                     "AND tool_name = ? AND tool_input_digest = ? AND state IN ('pending','withdrawn','expired') "
                     "AND NOT EXISTS (SELECT 1 FROM dialog_outcomes o WHERE o.request_id = r.id)")
            args: tuple[Any, ...] = (agent, agent_session_id, tool_name, digest)
            row = None
            if use_id is not None:
                row = conn.execute("SELECT id, state, decided_at " + where + " AND event_id = ? "
                                   "ORDER BY received_at DESC, rowid DESC LIMIT 1", args + ("hook:" + use_id,)).fetchone()
            if row is None:
                # a request that carries another call's id is not this call's; one with none (a PermissionRequest
                # Claude Code and Codex send without it) could be
                extra = " AND event_id NOT LIKE 'hook:%'" if use_id is not None else ""
                row = conn.execute("SELECT id, state, decided_at " + where + extra +
                                   " ORDER BY (state = 'pending') DESC, received_at DESC, rowid DESC LIMIT 1",
                                   args).fetchone()
            if row is None:
                return None
            outcome = "ran"
            if row[1] != "pending" and row[2] is not None:
                try:
                    if parse_stamp(stamp) - parse_stamp(row[2]) < UNASKED_WITHIN:
                        outcome = "ran_unasked"
                except ConsentError:
                    pass
            conn.execute("INSERT INTO dialog_outcomes (request_id, outcome, closed_at, event) VALUES (?, ?, ?, ?)",
                         (row[0], outcome, stamp, event))
            if row[1] == "pending":
                # answered in the agent's dialog while the hook still held it (Claude Code shows its dialog during
                # a hold): nothing is left to sign, so it stops being answerable
                request = self._request_of(self._row(conn, row[0]) or {})
                if request is not None:
                    self._transition(conn, request, "withdrawn", stamp=stamp,
                                     note="the agent ran it: answered in the agent's own dialog")
        return row[0], outcome

    def close_unrun(self, *, agent: str, agent_session_id: str, event: str) -> list[str]:
        """The session's turn ended or a later one began (its Stop / UserPromptSubmit): every request of this
        agent and session that levain left to the agent's dialog (withdrawn or expired, or pending past its
        expiry) and that has no outcome gets ``did_not_run``. A request still held is left open. Returns their
        ids."""
        with self._write() as conn:
            stamp = utc_stamp(_now_utc(self._clock))
            ids = [r[0] for r in conn.execute(
                "SELECT id FROM requests r WHERE agent = ? AND agent_session_id = ? "
                "AND (state IN ('withdrawn','expired') OR (state = 'pending' AND expires_at <= ?)) "
                "AND NOT EXISTS (SELECT 1 FROM dialog_outcomes o WHERE o.request_id = r.id) "
                "ORDER BY received_at, rowid", (agent, agent_session_id, stamp)).fetchall()]
            conn.executemany("INSERT INTO dialog_outcomes (request_id, outcome, closed_at, event) VALUES (?, ?, ?, ?)",
                             [(i, "did_not_run", stamp, event) for i in ids])
        return ids

    def dialog_outcome(self, request_id: str) -> tuple[str, str] | None:
        """(outcome, closed_at) recorded for ``request_id``, or ``None`` (none yet, or levain decided it)."""
        with self._read() as conn:
            row = conn.execute("SELECT outcome, closed_at FROM dialog_outcomes WHERE request_id = ?",
                               (request_id,)).fetchone()
        return None if row is None else (row[0], row[1])

    def withdraw(self, request_id: str, *, note: str = "") -> bool:
        """The requester stops waiting (the human answered the native dialog; the hook gave up). Needs no
        signature: it decides nothing, it leaves the agent's own dialog. False if the request is not pending. A
        request past its hold stays pending for :data:`UNDECIDED_GRACE`, so it can still be withdrawn then; after
        that a read or a sweep has expired it, and it is not withdrawn."""
        with self._write() as conn:
            request, state, stamp = self._settle(conn, request_id)
            if request is None or state != "pending":
                return False
            return self._transition(conn, request, "withdrawn", stamp=stamp, note=note or None)

    # --- the deadline: undecided, gaps, halts, snoozes -------------------------------------------------
    def mark_undecided(self, request_id: str, *, halt: bool = False, tool_input: Any = None,
                       posture: str | None = None, note: str | None = None) -> Marked:
        """The hook's hold ended with no verified decision: move the pending request to ``undecided`` and give it
        the next gap number of its agent and session, with its receipt, and, when ``halt`` (the irreversible /
        agent-config tier), the session's halt on it (:meth:`is_halted`), all in ONE write transaction (the clock
        read under the lock): a hook killed part way leaves either nothing or all of it. The library owns the
        deadline (the smaller of the two L3 r1 options, no separate answer-expiry in :meth:`file`): pending ->
        undecided is allowed at or after ``expires_at``, because the hook calls this only once its hold ended
        with the call not run, and it takes precedence over the expire, which a sweep or a read applies only
        :data:`UNDECIDED_GRACE` after ``expires_at`` and which this call never applies. No answer or grant is
        applied once ``expires_at`` has passed. Returns a :class:`Marked`: the request's state after the call, its gap, why, whether its session is
        halted on it, and, for a request decided before this call, what decided it (a re-verified
        :class:`Decision` for a signed or grant answer, ``tool_input`` / ``posture`` passed to :meth:`decision`;
        a :class:`Ruling`, information only, for a late ruling). A request already undecided keeps its gap (a
        retry of the hook) and gains the halt when ``halt``. ``note`` replaces the receipt's words for a request
        that was never held (``levain ask --would-push`` files one undecided at once)."""
        decided: str | None = None
        with self._write() as conn:
            request, state, stamp = self._settle(conn, request_id, expire=False)
            if request is None:
                return Marked(None, None, f"no sealed request {request_id!r}")
            if state == "undecided":
                got = self._gap_of(conn, request_id)
                if got is None:
                    if halt:                         # never skip a halt silently
                        raise QueueStoreError(f"request {request_id} is undecided but has no gap number: "
                                              "its session cannot be halted on it")
                    return Marked("undecided", None, "the request is undecided but has no gap number")
                if halt:
                    self._insert_halt(conn, request, got[0], stamp)
                return Marked("undecided", got[0], "the request was already undecided",
                              self._halted_on(conn, request_id))
            if state == "pending":
                last = conn.execute("SELECT MAX(gap) FROM gaps WHERE agent = ? AND agent_session_id = ?",
                                    (request.agent, request.agent_session_id)).fetchone()[0]
                gap = (last if type(last) is int else 0) + 1
                if not self._transition(conn, request, "undecided", stamp=stamp,
                                        note=note or "the hold ended with no verified decision"):
                    return Marked(state, None, "the request moved while it was being marked")
                conn.execute("INSERT INTO gaps (request_id, agent, agent_session_id, gap, created_at) "
                             "VALUES (?, ?, ?, ?, ?)", (request.id, request.agent, request.agent_session_id, gap,
                                                        stamp))
                if halt:
                    self._insert_halt(conn, request, gap, stamp)
                return Marked("undecided", gap, "the hold ended with no verified decision", halt)
            if state == "expired":
                return Marked(state, None, "the request expired before its hold ended: nothing to rule on")
            if state == "withdrawn":
                return Marked(state, None, "the request was withdrawn to the agent's own dialog")
            decided = state
            got_gap = self._gap_of(conn, request_id)
        got = self.decision(request_id, tool_input=tool_input, posture=posture)
        gap_no = None if got_gap is None else got_gap[0]
        if isinstance(got, Ruling):
            return Marked(decided, got.gap, f"the request was ruled {got.ruling} late, on gap #{got.gap}",
                          decision=got)
        if got is None:
            return Marked(decided, gap_no, f"the request is {decided} by an answer or ruling that no longer verifies")
        return Marked(decided, None, f"the request was answered {got.decision} before the deadline", decision=got)

    @staticmethod
    def _insert_halt(conn: sqlite3.Connection, request: ExternalGateRequest, gap: int, stamp: str) -> None:
        conn.execute("INSERT OR IGNORE INTO halts (request_id, agent, agent_session_id, gap, halted_at) "
                     "VALUES (?, ?, ?, ?, ?)", (request.id, request.agent, request.agent_session_id, gap, stamp))

    @staticmethod
    def _halted_on(conn: sqlite3.Connection, request_id: str) -> bool:
        return conn.execute("SELECT 1 FROM halts h WHERE h.request_id = ? AND NOT EXISTS "
                            "(SELECT 1 FROM halt_releases r WHERE r.request_id = h.request_id)",
                            (request_id,)).fetchone() is not None

    def halt(self, request_id: str) -> int | None:
        """Halt the request's session on it (the irreversible / agent-config tier, after :meth:`mark_undecided`):
        :meth:`is_halted` names its gap until a ruling on this request releases it. Returns the gap number, the
        same again when already halted; ``None`` when the request is not undecided (a ruled request is never
        halted again)."""
        with self._write() as conn:
            row = self._row(conn, request_id)
            got = self._gap_of(conn, request_id)
            if row is None or got is None or row["state"] != "undecided":
                return None
            request = self._request_of(row)
            if request is None:
                return None
            self._insert_halt(conn, request, got[0], utc_stamp(_now_utc(self._clock)))
            return got[0]

    def is_halted(self, agent: str, agent_session_id: str) -> int | None:
        """The lowest gap number this session is halted on and not yet released, or ``None``."""
        with self._read() as conn:
            row = conn.execute("SELECT MIN(h.gap) FROM halts h WHERE h.agent = ? AND h.agent_session_id = ? "
                               "AND NOT EXISTS (SELECT 1 FROM halt_releases r WHERE r.request_id = h.request_id)",
                               (agent, agent_session_id)).fetchone()
        return row[0] if row is not None and type(row[0]) is int else None

    def snooze(self, request_id: str, minutes: float = 30) -> str | None:
        """Ask the operator again about a pending or undecided request ``minutes`` from now (at most
        :data:`SNOOZE_MAX_MINUTES`; past it, or not a positive finite number, :class:`ConsentError`): records the
        time and returns it; ``None`` when the request is in neither state. Unsigned: it changes no authority, only
        when he is asked. A later snooze of the same request replaces the earlier one."""
        if isinstance(minutes, bool) or not isinstance(minutes, (int, float)) or not minutes > 0 \
                or minutes == float("inf"):
            raise ConsentError("minutes must be a positive finite number")
        if minutes > SNOOZE_MAX_MINUTES:
            raise ConsentError(f"minutes must be at most {SNOOZE_MAX_MINUTES} (a week)")
        with self._write() as conn:
            request, state, stamp = self._settle(conn, request_id)
            if request is None or state not in ("pending", "undecided"):
                return None
            reping_at = utc_stamp(parse_stamp(stamp) + timedelta(minutes=minutes))
            conn.execute("INSERT INTO snoozes (request_id, reping_at, created_at) VALUES (?, ?, ?)",
                         (request_id, reping_at, stamp))
        return reping_at

    def claim_repings(self, now: datetime | None = None) -> list[Reping]:
        """Claim the snoozed requests to ask about again at ``now`` (default the queue's clock): each request's
        latest snooze, due and not yet re-pinged, while the request is undecided or pending and unexpired, oldest
        due first. The read and the marking are ONE write transaction, so two sweepers never both claim one
        re-ping: the caller that gets it sends it."""
        with self._write() as conn:
            stamp = utc_stamp(_now_utc(self._clock) if now is None else _now_utc(lambda: now))
            rows = conn.execute(
                "SELECT s.seq, s.request_id, s.reping_at, r.state FROM snoozes s "
                "JOIN requests r ON r.id = s.request_id "
                "WHERE s.seq = (SELECT MAX(seq) FROM snoozes l WHERE l.request_id = s.request_id) "
                "AND s.reping_at <= ? AND NOT EXISTS (SELECT 1 FROM repings p WHERE p.snooze_seq = s.seq) "
                "AND (r.state = 'undecided' OR (r.state = 'pending' AND r.expires_at > ?)) "
                "ORDER BY s.reping_at, s.seq", (stamp, stamp)).fetchall()
            conn.executemany("INSERT INTO repings (snooze_seq, repinged_at) VALUES (?, ?)",
                             [(r[0], stamp) for r in rows])
        return [Reping(*r) for r in rows]

    # --- answers -------------------------------------------------------------------------------------
    def _refusal(self, request: ExternalGateRequest, queue_id: str, payload: Any, signature: Any,
                 authority: AuthoritySnapshot, *, unchained_ok: bool = False) -> str | None:
        """:func:`answer_refusal` with this queue's verifier."""
        return answer_refusal(request, queue_id, payload, signature, authority, ssh_keygen=self._ssh_keygen,
                              unchained_ok=unchained_ok)

    @staticmethod
    def _answer_record_refusal(request: ExternalGateRequest, queue_id: str, payload: Any, *,
                               unchained_ok: bool = False, window: bool = True) -> str | None:
        """The module's :func:`_answer_record_refusal`."""
        return _answer_record_refusal(request, queue_id, payload, unchained_ok=unchained_ok, window=window)

    def _signer_refusal(self, request: ExternalGateRequest | None, payload: Mapping[str, Any], signature: Any,
                        authority: AuthoritySnapshot, *, noun: str, encode_record: Callable[[dict[str, Any]], bytes],
                        namespace: str) -> str | None:
        """:func:`signer_refusal` with this queue's verifier."""
        return signer_refusal(request, payload, signature, authority, noun=noun, encode_record=encode_record,
                              namespace=namespace, ssh_keygen=self._ssh_keygen)

    @staticmethod
    def _key_refusal(key: Any, decision: Any, request: ExternalGateRequest | None, *,
                     noun: str) -> tuple[str | None, str] | None:
        """The module's :func:`_key_refusal`."""
        return _key_refusal(key, decision, request, noun=noun)

    def _signature_refusal(self, public_key: str, fingerprint: str, custody: str | None, payload: Mapping[str, Any],
                           signature: Any, *, noun: str, encode_record: Callable[[dict[str, Any]], bytes],
                           namespace: str) -> str | None:
        """The module's :func:`_signature_refusal` with this queue's verifier."""
        return _signature_refusal(public_key, fingerprint, custody, payload, signature, noun=noun,
                                  encode_record=encode_record, namespace=namespace, ssh_keygen=self._ssh_keygen)

    def _evidence(self, request: ExternalGateRequest, payload: Mapping[str, Any], signature: Any,
                  authority: AuthoritySnapshot, *, noun: str, encode_record: Callable[[dict[str, Any]], bytes],
                  namespace: str) -> tuple[tuple[str, str] | None, bool] | None:
        """For a record that binds this queue and ``request`` (already checked) whose signature verifies under the
        key it names: ``(why, enrolled)``, ``why`` the key's ``(subreason, refusal)`` when it is not authority for
        the record (:meth:`_key_refusal`: not enrolled, revoked, compromised, or of a class not covering the tier
        for an allow) and ``None`` when it is, ``enrolled`` whether ``authority`` holds the key. ``None`` when the
        record is no evidence. A key ``authority`` does not hold is the one the SSHSIG embeds, whose fingerprint
        must be the record's ``signer_fp``; an unenrolled passkey's assertion (a WebAuthn signature) is never
        evidence, since nothing enrolled says which key or page it is."""
        signer_fp = payload["signer_fp"]
        if not isinstance(signer_fp, str):
            return None
        key = authority.key(signer_fp)
        why = self._key_refusal(key, payload["decision"], request, noun=noun)
        if why is not None and why[0] is None:
            return None
        if key is None:
            try:
                public_key, fingerprint, sig_type = sshsig_public_key(signature)
            except ValueError:
                return None
            if fingerprint != signer_fp or sig_type == _WEBAUTHN_SIG_TYPE:
                return None
            custody = None
        else:
            public_key, fingerprint, custody = key.public_key, key.fingerprint, key.custody
        if self._signature_refusal(public_key, fingerprint, custody, payload, signature, noun=noun,
                                   encode_record=encode_record, namespace=namespace) is not None:
            return None
        return (None if why is None else (why[0], why[1])), key is not None

    def _keep_refused(self, detail: str, request: ExternalGateRequest, payload: Any, signature: Any, *,
                      late: bool, gap: tuple[int, str] | None = None, queue_id: str | None = None,
                      authority: AuthoritySnapshot | None = None) -> AnswerRefused:
        """The :class:`AnswerRefused` carrying ``detail``, after keeping the refused answer (``gap`` ``None``) or
        late ruling (``gap`` the request's) in ``refused_answers`` when it is evidence (:meth:`_refused_reason`).
        ``late``: the queue's clock or state says the request's hold had ended when it came. Keeping it is its own
        transaction; a store, disk, verifier or record failure there is swallowed, and any other exception (a bug
        in the keeping, a row the table's CHECKs refuse among them) is given as a :class:`RuntimeWarning`, never
        raised, even with warnings as errors: the caller raises the same refusal either way."""
        try:
            self._record_refused(detail, request, payload, signature, late=late, gap=gap, queue_id=queue_id,
                                 authority=authority)
        except sqlite3.IntegrityError as exc:   # the insert ignores only the dedupe: a CHECK failing is a bug
            bug: BaseException | None = exc
        except (sqlite3.Error, OSError, VerifierUnavailable, ConsentError):
            bug = None                      # the record is evidence, never a gate: the refusal stands as it is
        except Exception as exc:            # a bug: seen by the tests, never by the signer
            bug = exc
        else:
            bug = None
        if bug is not None:
            try:                            # warnings as errors (-W error) must not turn into the caller's error
                warnings.warn(f"levain: a refused answer to {request.id} was not kept: {type(bug).__name__}: {bug}",
                              RuntimeWarning, stacklevel=3)
            except Warning:
                pass
        return AnswerRefused(detail)

    def _refused_reason(self, request: ExternalGateRequest, queue_id: str, payload: Any, signature: Any, detail: str,
                        *, late: bool, gap: tuple[int, str] | None,
                        authority: AuthoritySnapshot) -> tuple[str, str, str, bool] | None:
        """``(reason, subreason, detail, enrolled)`` for a refused answer or ruling that is kept (``reason`` one of
        :data:`REFUSED_REASONS`, ``subreason`` of :data:`REFUSED_SUBREASONS`), or ``None`` when it is not: it must
        be a well-formed record for this queue, request and digest (a ruling also for the request's gap) whose
        signature verifies under the key it names (:meth:`_evidence`). An unauthorised one is kept with the KEY's
        refusal as its detail, whatever the caller was told; an authorised answer only when ``late`` (by the
        queue's clock or state: a ``signed_at`` after the hold, the signer's own claim, is never lateness), with
        ``detail``; an authorised ruling never is."""
        if gap is None:
            shape = self._answer_record_refusal(request, queue_id, payload, window=False)
            noun, encode_record, namespace = "answer", answer_message, ANSWER_NAMESPACE
        else:
            shape = self._ruling_record_refusal(request, queue_id, gap, payload)
            noun, encode_record, namespace = "ruling", ruling_message, RULING_NAMESPACE
        if shape is not None:
            return None
        verdict = self._evidence(request, payload, signature, authority, noun=noun, encode_record=encode_record,
                                 namespace=namespace)
        if verdict is None:
            return None
        why, enrolled = verdict
        if why is not None:
            return REFUSED_NOT_AUTHORIZED, why[0], why[1], enrolled
        if gap is None and late:
            return REFUSED_WINDOW_ELAPSED, "late", detail, enrolled
        return None

    def _connect_briefly(self) -> sqlite3.Connection:
        """A queue connection that waits at most :data:`REFUSED_BUSY_MS` for a lock (keeping a refused answer)."""
        conn = self._connect()
        try:
            conn.execute(f"PRAGMA busy_timeout = {int(REFUSED_BUSY_MS)}")
        except BaseException:
            conn.close()
            raise
        return conn

    def _record_refused(self, detail: str, request: ExternalGateRequest, payload: Any, signature: Any, *,
                        late: bool, gap: tuple[int, str] | None, queue_id: str | None,
                        authority: AuthoritySnapshot | None) -> None:
        def full(conn: sqlite3.Connection, enrolled: bool) -> bool:
            kept = conn.execute("SELECT count(*) FROM refused_answers WHERE request_id = ? AND enrolled = ?",
                                (request.id, int(enrolled))).fetchone()
            return kept[0] >= (REFUSED_PER_REQUEST if enrolled else REFUSED_UNENROLLED_PER_REQUEST)

        named = isinstance(payload, Mapping) and isinstance(payload.get("signer_fp"), str) \
            and isinstance(payload.get("decision"), str)
        head = payload.get("receipts_head") if isinstance(payload, Mapping) else None
        # the first read: a repeat, a stale head, or both classes full costs no replay and no verify; one class full
        # costs no verify, and no replay when the caller already holds one (only a replay says which class a key is
        # in). The receipts are append-only, so a head that is the log's head of its size here stays so.
        with read_transaction(self._connect_briefly) as conn:
            if named and conn.execute("SELECT 1 FROM refused_answers WHERE request_id = ? AND signer_fp = ? "
                                      "AND decision = ?", (request.id, payload["signer_fp"],
                                                           payload["decision"])).fetchone() is not None:
                return                      # this (request, signer, decision) is kept already
            fulls = {enrolled: full(conn, enrolled) for enrolled in (False, True)}
            if all(fulls.values()):
                return
            # after the cap: the head check hashes the log up to the head's size
            if head_problem(head) is None and head_refusal(conn, head, noun="ruling" if gap else "answer") is not None:
                return                      # not the log's head of its size: a stale head is never kept
            if queue_id is None:
                queue_id = conn.execute("SELECT value FROM meta WHERE key = 'queue_id'").fetchone()[0]
        if authority is not None and named and fulls[authority.key(payload["signer_fp"]) is not None]:
            return                          # its class is full, known without a replay
        if authority is None:
            authority = self._fresh_authority()
        if authority is None:
            return                          # no verdict on the key is possible
        if named and fulls[authority.key(payload["signer_fp"]) is not None]:
            return                          # its class is full: no verify
        kept = self._refused_reason(request, queue_id, payload, signature, detail, late=late, gap=gap,
                                    authority=authority)
        if kept is None:
            return
        reason, subreason, why, enrolled = kept
        record = (answer_message if gap is None else ruling_message)(dict(payload)).decode("utf-8")
        with write_transaction(self._connect_briefly) as conn:
            if full(conn, enrolled):        # the count again under the lock: the cap holds across processes
                return
            # only the dedupe is ignored: a CHECK or NOT NULL failure is a bug in the keeping and is raised
            conn.execute("INSERT INTO refused_answers (request_id, kind, refused_at, reason_code, subreason, "
                         "enrolled, detail, decision, signer_fp, signed_at, record, signature) "
                         "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                         "ON CONFLICT (request_id, signer_fp, decision) DO NOTHING",
                         (request.id, "answer" if gap is None else "ruling", utc_stamp(_now_utc(self._clock)), reason,
                          subreason, int(enrolled), why, payload["decision"], payload["signer_fp"],
                          payload["signed_at"], record, signature))

    def _ruling_refusal(self, request: ExternalGateRequest, queue_id: str, gap: tuple[int, str], payload: Any,
                        signature: Any, authority: AuthoritySnapshot, *, unchained_ok: bool = False) -> str | None:
        """Why ``payload``/``signature`` is not a ruling on ``request`` (gap number and the time it went undecided
        in ``gap``) under ``authority``; ``None`` when it verifies. Checked when it is applied and again every time
        it is delivered or taken; applying it also checks its ``receipts_head`` against the log (in
        :meth:`answer_late`'s write transaction), which this does not. ``unchained_ok`` as :meth:`_refusal`'s: a
        ruling applied before the chaining is still delivered and taken, every check but the head's."""
        why = self._ruling_record_refusal(request, queue_id, gap, payload, unchained_ok=unchained_ok)
        if why is not None:
            return why
        return self._signer_refusal(request, payload, signature, authority, noun="ruling",
                                    encode_record=ruling_message, namespace=RULING_NAMESPACE)

    @staticmethod
    def _ruling_record_refusal(request: ExternalGateRequest, queue_id: str, gap: tuple[int, str], payload: Any, *,
                               unchained_ok: bool = False) -> str | None:
        """Every check of :meth:`_ruling_refusal` but the signer's: why ``payload`` is not a ruling record binding
        this queue, ``request`` and its gap."""
        unchained = isinstance(payload, Mapping) and payload.get("format") == RULING_FORMAT_UNCHAINED
        if unchained and not unchained_ok:
            return (f"the ruling is in format {RULING_FORMAT_UNCHAINED!r}, signed before the queue's receipts were "
                    "chained: it covers no receipts head; rule on the request again")
        if not isinstance(payload, Mapping) \
                or set(payload) != (_RULING_KEYS_UNCHAINED if unchained else _RULING_KEYS):
            return "the ruling is not a ruling record"
        if not unchained:
            if payload["format"] != RULING_FORMAT:
                return f"ruling format {payload['format']!r} is not {RULING_FORMAT!r}"
            problem = head_problem(payload["receipts_head"])
            if problem is not None:
                return f"the ruling's {problem}"
        if payload["queue_id"] != queue_id:
            return "the ruling names another queue"
        if payload["request_id"] != request.id:
            return "the ruling is for another request"
        if payload["request_digest"] != request.digest:
            return "the ruling signs another version of this request"
        if type(payload["gap"]) is not int or payload["gap"] != gap[0]:
            return f"the ruling names gap {payload['gap']!r}, not this request's gap #{gap[0]}"
        signed_at = payload["signed_at"]
        try:
            early = parse_stamp(signed_at) < parse_stamp(gap[1])
        except ConsentError:
            return "the ruling's signed_at is not a timestamp"
        if early:
            return "the ruling was signed before the request went undecided"
        if payload["decision"] not in DECISIONS:
            return f"decision {payload['decision']!r} is not one of {DECISIONS}"
        return None

    def answer(self, request_id: str, payload: Mapping[str, Any], signature: str) -> Decision:
        """Apply a signed answer to ``request_id``, or raise :class:`AnswerRefused` (the request stays as it
        was: an answer after ``expires_at`` is refused, and the request stays pending until its grace ends). The answer is verified against a fresh
        replay from the queue's authority source, and its ``receipts_head`` must be the head of its size over the
        receipts as they stand inside the write transaction that applies it ("the receipts changed under this
        answer" otherwise). ``payload`` is read once, at entry: every check and the apply use that copy.

        A refused answer that is evidence (it binds this request and verifies under the key it names: an
        unauthorised key, or an authorised one too late) is kept in ``refused_answers`` (:meth:`refused_answers`)
        before the refusal is raised; the request is never moved by it and the refusal is the same."""
        payload = _detached(payload)
        got = self.get(request_id)                         # expires it first if it is due (UNDECIDED_GRACE)
        if got is None:
            raise AnswerRefused(f"no sealed request {request_id!r}")
        request, state = got
        if state in ("expired", "undecided"):              # the hold ended before this answer came
            raise self._keep_refused(f"request {request_id} is already {state}", request, payload, signature,
                                     late=True)
        if state != "pending":                             # decided or withdrawn: a duplicate, never kept
            raise AnswerRefused(f"request {request_id} is already {state}")
        if request.expires_at <= utc_stamp(_now_utc(self._clock)):
            raise self._keep_refused(f"request {request_id} is already expired: its hold ended {request.expires_at}",
                                     request, payload, signature, late=True)
        authority = self._fresh_authority()
        if authority is None:
            raise AnswerRefused("the authority log cannot be replayed now: no answer is authority")
        queue_id = self.queue_id
        why = self._refusal(request, queue_id, payload, signature, authority)
        if why is not None:
            raise self._keep_refused(why, request, payload, signature, late=False, queue_id=queue_id,
                                     authority=authority)
        decision = payload["decision"]
        message = answer_message(dict(payload))
        with self._write() as conn:
            current, now_state, stamp = self._settle(conn, request_id)
            if now_state == "pending" and request.expires_at <= stamp:
                now_state = "expired"                # the hold ended: only the hook's undecided mark applies
            stale = None
            if current is not None and now_state == "pending":
                stale = head_refusal(conn, payload["receipts_head"], noun="answer")
            moved = stale is None and current is not None and now_state == "pending" and self._transition(
                conn, request, _STATE_OF[decision], stamp=stamp, answer=message.decode("utf-8"),
                signature=signature, signer_fp=payload["signer_fp"], via="signature")
        if stale is not None:
            raise AnswerRefused(stale)                     # the receipts changed under it: never kept
        if not moved and now_state in ("expired", "undecided"):
            raise self._keep_refused(f"request {request_id} is already {now_state}", request, payload, signature,
                                     late=True, queue_id=queue_id, authority=authority)
        if not moved:
            raise AnswerRefused(f"request {request_id} is already {now_state or 'gone'}")
        return Decision(request_id=request.id, decision=decision, signer_fp=payload["signer_fp"], decided_at=stamp,
                        request_digest=request.digest)

    def _head_to_sign(self) -> dict[str, Any]:
        """The head of every receipt in one read of them, for a record to sign; :class:`AnswerRefused` when the
        receipts cannot be read as a log."""
        with self._read() as conn:
            try:
                return receipts_head(conn)
            except LogUnreadable as exc:
                raise AnswerRefused(f"the receipts cannot be read as a log ({exc})") from None

    def answer_payload(self, request_id: str, decision: str, signer_fp: str) -> dict[str, str]:
        """The record to sign to answer ``request_id``, ``signed_at`` now by the queue's clock, ``receipts_head``
        the head of every receipt in one read of them (what every signer signs: the CLI, the phone page). Raises
        :class:`AnswerRefused` when there is no sealed pending request."""
        got = self.get(request_id)
        if got is None:
            raise AnswerRefused(f"no sealed request {request_id!r}")
        request, state = got
        if state != "pending":
            raise AnswerRefused(f"request {request_id} is already {state}")
        if request.expires_at <= utc_stamp(_now_utc(self._clock)):
            raise AnswerRefused(f"request {request_id} is already expired: its hold ended {request.expires_at}")
        head = self._head_to_sign()
        return answer_payload(queue_id=self.queue_id, request=request, decision=decision, signer_fp=signer_fp,
                              signed_at=utc_stamp(_now_utc(self._clock)), receipts_head=head)

    # --- away marks ------------------------------------------------------------------------------------
    @staticmethod
    def _check_agent(agent: Any) -> None:
        if agent not in AGENTS:
            raise ConsentError(f"agent must be one of {AGENTS}: {agent!r}")

    @staticmethod
    def _latest_mark(conn: sqlite3.Connection, agent: str) -> tuple[Any, ...] | None:
        # latest by rowid: the triggers refuse an UPDATE, a DELETE and an INSERT that reuses an id or a rowid, so a
        # writer that leaves them in place can only add a mark (more away); one that drops them is the same-uid edge
        return conn.execute("SELECT m.id, m.set_at, l.left_at, l.payload, l.signature, l.signer_fp FROM away_marks m "
                            "LEFT JOIN away_leaves l ON l.mark_id = m.id WHERE m.agent = ? ORDER BY m.rowid DESC "
                            "LIMIT 1", (agent,)).fetchone()

    def mark_away(self, agent: str) -> str:
        """Record that the operator set ``agent``'s away mode (``levain consent away on``): a new away mark, whose
        id is returned. Unsigned, since a mark only raises what the hook holds. Until a leave of the agent's latest
        mark verifies (:meth:`leave_away`), the agent is away whatever its settings file says (:meth:`away_mark`)."""
        self._check_agent(agent)
        mark_id = "am-" + os.urandom(16).hex()
        with self._write() as conn:
            conn.execute("INSERT INTO away_marks (id, agent, set_at) VALUES (?, ?, ?)",
                         (mark_id, agent, utc_stamp(_now_utc(self._clock))))
        return mark_id

    def _leave_refusal(self, mark_id: str, agent: str, set_at: str, queue_id: str, payload: Any, signature: Any,
                       authority: AuthoritySnapshot) -> str | None:
        """Why ``payload``/``signature`` is not a leave of the away mark ``mark_id`` (``agent``'s, set at
        ``set_at``) under ``authority``; ``None`` when it verifies. Any enrolled, unrevoked key signs a leave, of
        any class: it lowers what the hook holds back to the operator's own presence."""
        if not isinstance(payload, Mapping) or set(payload) != _LEAVE_KEYS:
            return "the leave is not an away-leave record"
        if payload["format"] != AWAY_LEAVE_FORMAT:
            return f"leave format {payload['format']!r} is not {AWAY_LEAVE_FORMAT!r}"
        if payload["queue_id"] != queue_id:
            return "the leave names another queue"
        if payload["mark_id"] != mark_id:
            return "the leave is for another away mark"
        if payload["agent"] != agent:
            return "the leave is for another agent"
        if payload["decision"] != "leave":
            return f"decision {payload['decision']!r} is not 'leave'"
        try:
            early = parse_stamp(payload["signed_at"]) < parse_stamp(set_at)
        except ConsentError:
            return "the leave's signed_at is not a timestamp"
        if early:
            return "the leave was signed before away mode was set"
        return self._signer_refusal(None, payload, signature, authority, noun="leave",
                                    encode_record=away_leave_message, namespace=AWAY_NAMESPACE)

    def _stored_leave_refusal(self, agent: str, row: tuple[Any, ...], queue_id: str) -> str | None:
        """Why the leave stored for the mark in ``row`` (:meth:`_latest_mark`) does not count now; ``None`` when it
        re-verifies against a fresh replay and was signed no later than its row's ``left_at`` plus
        :data:`RULING_SKEW` (the bound :meth:`leave_away` applied)."""
        mark_id, set_at, left_at, stored, signature, signer_fp = row
        if not isinstance(stored, str) or not isinstance(left_at, str):
            return "the stored leave is not a record"
        try:
            payload = json.loads(stored)
            if not isinstance(payload, dict) or away_leave_message(payload) != stored.encode("utf-8"):
                return "the stored leave is not its own canonical encoding"
        except (ValueError, ConsentError, RecursionError):
            return "the stored leave does not parse"
        if payload.get("signer_fp") != signer_fp:
            return "the stored leave names another signer than its row"
        authority = self._fresh_authority()
        if authority is None:
            return _AUTHORITY_NOW_UNREADABLE
        why = self._leave_refusal(mark_id, agent, set_at, queue_id, payload, signature, authority)
        if why is not None:
            return why
        try:
            late = parse_stamp(payload["signed_at"]) > parse_stamp(left_at) + RULING_SKEW
        except ConsentError:
            return "the leave row's left_at is not a timestamp"
        return "the leave was signed after the queue recorded it" if late else None

    def away_mark(self, agent: str, *, as_is: bool = False) -> AwayMark | None:
        """``agent``'s latest away mark, or ``None`` when it never had one. A stored leave counts (``left``) only
        when it re-verifies (:meth:`_stored_leave_refusal`): a leave by a key revoked since, or a changed row,
        leaves the mark standing. The verdict is kept for this queue's life by the whole row, so a hook process
        verifies a leave once and its recheck ticks reuse it (a new mark or a changed row is read afresh; a log
        that could not be replayed is tried again). With ``as_is`` (a reader that holds no queue of its own) the
        store is read as it stands and never written (:meth:`_read_as_is`: a prior-format store has no mark).
        Raises when the store cannot be read (the hook takes that as away)."""
        self._check_agent(agent)
        reading = self._read_as_is(_AWAY_TABLES) if as_is and not self._ready else self._read()
        with reading as conn:
            if conn is None:
                return None
            row = self._latest_mark(conn, agent)
            queue_id = conn.execute("SELECT value FROM meta WHERE key = 'queue_id'").fetchone()[0]
        if row is None:
            return None
        if row[3] is None:
            return AwayMark(id=row[0], agent=agent, set_at=row[1], left=False)
        key = (queue_id, *row)
        if key in self._leave_verdicts:
            why = self._leave_verdicts[key]
        else:
            why = self._stored_leave_refusal(agent, row, queue_id)
            if why != _AUTHORITY_NOW_UNREADABLE:
                self._leave_verdicts[key] = why
        return AwayMark(id=row[0], agent=agent, set_at=row[1], left=why is None, leave_by=row[5], leave_refusal=why)

    def away_leave_payload(self, agent: str, signer_fp: str) -> dict[str, str]:
        """The record to sign to leave ``agent``'s away mode: its latest mark, ``signed_at`` now by the queue's
        clock. Raises :class:`AnswerRefused` when the agent has no mark or its latest mark already has a leave."""
        self._check_agent(agent)
        with self._read() as conn:
            row = self._latest_mark(conn, agent)
            queue_id = conn.execute("SELECT value FROM meta WHERE key = 'queue_id'").fetchone()[0]
        if row is None:
            raise AnswerRefused(f"{agent} has no away mark to leave")
        if row[3] is not None:
            raise AnswerRefused(f"{agent}'s away mark {row[0]} already has a leave; set away mode again to leave it "
                                "anew")
        return away_leave_payload(queue_id=queue_id, mark_id=row[0], agent=agent, signer_fp=signer_fp,
                                  signed_at=utc_stamp(_now_utc(self._clock)))

    def leave_away(self, payload: Mapping[str, Any], signature: str) -> AwayMark:
        """Apply a signed leave of an agent's away mode, or raise :class:`AnswerRefused` (the mark stands). The
        leave is verified against a fresh replay of the authority log (:meth:`_leave_refusal`, in
        :data:`~levain.consent.record.AWAY_NAMESPACE`), its mark must still be the agent's latest with no leave
        stored, and its ``signed_at`` no later than the queue's clock read under the write lock plus
        :data:`RULING_SKEW`."""
        payload = _detached(payload)        # read once, as answer() and answer_late() do: checked = applied
        agent = payload.get("agent") if isinstance(payload, Mapping) else None
        if agent not in AGENTS:
            raise AnswerRefused("the leave names no known agent")
        authority = self._fresh_authority()
        if authority is None:
            raise AnswerRefused("the authority log cannot be replayed now: no leave is authority")
        with self._read() as conn:
            row = self._latest_mark(conn, agent)
            queue_id = conn.execute("SELECT value FROM meta WHERE key = 'queue_id'").fetchone()[0]
        if row is None:
            raise AnswerRefused(f"{agent} has no away mark to leave")
        why = self._leave_refusal(row[0], agent, row[1], queue_id, payload, signature, authority)
        if why is not None:
            raise AnswerRefused(why)
        message = away_leave_message(dict(payload))
        with self._write() as conn:
            now = _now_utc(self._clock)
            stamp = utc_stamp(now)
            latest = self._latest_mark(conn, agent)
            if latest is None or latest[0] != row[0]:
                problem = "a newer away mark was set meanwhile; sign the leave again"
            elif latest[3] is not None:
                problem = f"away mark {row[0]} already has a leave"
            elif parse_stamp(payload["signed_at"]) > now + RULING_SKEW:
                problem = (f"the leave's signed_at {payload['signed_at']} is later than the queue's clock {stamp} "
                           f"plus {int(RULING_SKEW.total_seconds())} s")
            else:
                problem = None
                conn.execute("INSERT INTO away_leaves (mark_id, left_at, payload, signature, signer_fp) "
                             "VALUES (?, ?, ?, ?, ?)", (row[0], stamp, message.decode("utf-8"), signature,
                                                        payload["signer_fp"]))
        if problem is not None:
            raise AnswerRefused(problem)
        return AwayMark(id=row[0], agent=agent, set_at=row[1], left=True, leave_by=payload["signer_fp"])

    # --- late rulings ----------------------------------------------------------------------------------
    def _undecided(self, request_id: str) -> tuple[ExternalGateRequest, tuple[int, str], str]:
        with self._read() as conn:
            row = self._row(conn, request_id)
            gap = self._gap_of(conn, request_id)
            queue_id = conn.execute("SELECT value FROM meta WHERE key = 'queue_id'").fetchone()[0]
        request = None if row is None else self._request_of(row)
        if row is None or request is None:
            raise AnswerRefused(f"no sealed request {request_id!r}")
        if row["state"] != "undecided":
            raise AnswerRefused(f"request {request_id} is {row['state']}, not undecided: a ruling answers only an "
                                "undecided request")
        if gap is None:
            raise AnswerRefused(f"request {request_id} is undecided but has no gap number")
        return request, gap, queue_id

    def ruling_payload(self, request_id: str, decision: str, signer_fp: str) -> dict[str, Any]:
        """The record to sign to rule on the undecided ``request_id``, ``signed_at`` now by the queue's clock,
        ``receipts_head`` the head of every receipt in one read of them. Raises :class:`AnswerRefused` when there
        is no sealed undecided request."""
        request, gap, queue_id = self._undecided(request_id)
        head = self._head_to_sign()
        return ruling_payload(queue_id=queue_id, request=request, gap=gap[0], decision=decision,
                              signer_fp=signer_fp, signed_at=utc_stamp(_now_utc(self._clock)), receipts_head=head)

    def answer_late(self, request_id: str, payload: Mapping[str, Any], signature: str) -> Ruling:
        """Apply a signed late ruling to the undecided ``request_id``, or raise :class:`AnswerRefused` (the request
        stays undecided). The ruling is verified against a fresh replay from the queue's authority source exactly
        as :meth:`answer` verifies an answer (the signer enrolled and unrevoked, of a class covering the tier for
        an allow), in :data:`~levain.consent.record.RULING_NAMESPACE`, and its ``signed_at`` must be no later than
        the queue's clock read under the write lock plus :data:`RULING_SKEW`, and its ``receipts_head`` must be the
        head of its size over the receipts as they stand under that lock. The move to ``allowed``/``denied``,
        its receipt and the release of a halt on this request are one transaction. Returns the :class:`Ruling`
        (information: applying a ruling delivers nothing; a ruled allow reaches the agent only through
        :meth:`take_ruled_allow`). ``payload`` is read once, at entry, as :meth:`answer` reads it.

        A refused ruling by a key that is not authority for it, whose signature verifies under the key it names
        and which binds this request and gap, is kept in ``refused_answers`` as :meth:`answer` keeps a refused
        answer; the request stays undecided and the refusal is the same."""
        payload = _detached(payload)
        request, gap, queue_id = self._undecided(request_id)
        authority = self._fresh_authority()
        if authority is None:
            raise AnswerRefused("the authority log cannot be replayed now: no ruling is authority")
        why = self._ruling_refusal(request, queue_id, gap, payload, signature, authority)
        if why is not None:
            raise self._keep_refused(why, request, payload, signature, late=False, gap=gap, queue_id=queue_id,
                                     authority=authority)
        decision = payload["decision"]
        message = ruling_message(dict(payload))
        early = False
        stale = None
        with self._write() as conn:
            now = _now_utc(self._clock)
            stamp = utc_stamp(now)
            if parse_stamp(payload["signed_at"]) > now + RULING_SKEW:
                early, moved = True, False
            elif (stale := head_refusal(conn, payload["receipts_head"], noun="ruling")) is not None:
                moved = False
            else:
                moved = self._transition(conn, request, _STATE_OF[decision], stamp=stamp,
                                         answer=message.decode("utf-8"), signature=signature,
                                         signer_fp=payload["signer_fp"], via="ruling", from_state="undecided",
                                         gap=gap[0])
            if moved:
                conn.execute("INSERT INTO halt_releases (request_id, released_at, by_request_id) "
                             "SELECT request_id, ?, request_id FROM halts WHERE request_id = ?", (stamp, request.id))
        if stale is not None:
            raise AnswerRefused(stale)
        if early:
            raise AnswerRefused(f"the ruling's signed_at {payload['signed_at']} is later than the queue's clock "
                                f"{stamp} plus {int(RULING_SKEW.total_seconds())} s")
        if not moved:
            raise AnswerRefused(f"request {request_id} is no longer undecided")
        return Ruling(request_id=request.id, ruling=decision, signer_fp=payload["signer_fp"], decided_at=stamp,
                      signed_at=payload["signed_at"], request_digest=request.digest, gap=gap[0])

    def _verified_ruling(self, row: Mapping[str, Any], request: ExternalGateRequest, queue_id: str,
                         gap: tuple[int, str] | None, payload: Mapping[str, Any],
                         authority: AuthoritySnapshot | None, *, taken: bool = False) -> Ruling | None:
        """A ruled row's ruling, re-verified under ``authority``: the stored ruling names this row's decision and
        signer, verifies (:meth:`_ruling_refusal`) for the request's gap, and was signed no later than the row's
        ``decided_at`` plus :data:`RULING_SKEW` (the bound :meth:`answer_late` applied)."""
        named = payload.get("decision")
        if gap is None or authority is None or not isinstance(named, str) or _STATE_OF.get(named) != row["state"] \
                or payload.get("signer_fp") != row["signer_fp"] or not isinstance(row["decided_at"], str):
            return None
        if self._ruling_refusal(request, queue_id, gap, payload, row["signature"], authority,
                                unchained_ok=True) is not None:
            return None
        try:
            if parse_stamp(payload["signed_at"]) > parse_stamp(row["decided_at"]) + RULING_SKEW:
                return None
        except ConsentError:
            return None
        return Ruling(request_id=request.id, ruling=named, signer_fp=payload["signer_fp"],
                      decided_at=row["decided_at"], signed_at=payload["signed_at"], request_digest=request.digest,
                      gap=gap[0], taken=taken)

    @staticmethod
    def _stored_record(row: Mapping[str, Any]) -> dict[str, Any] | None:
        """The row's stored answer record when it is the canonical encoding of itself, else ``None``."""
        stored = row["answer"]
        if not isinstance(stored, str) or not isinstance(row["decided_at"], str):
            return None
        try:
            payload = json.loads(stored)
            if not isinstance(payload, dict) or answer_message(payload) != stored.encode("utf-8"):
                return None
        except (ValueError, ConsentError, RecursionError):
            return None
        return payload

    _RULED_COLS = ", ".join(_FIELDS) + ", state, decided_at, answer, signature, signer_fp, via"
    # the queue's own APPLICATION SEQUENCE of a ruling: the seq of its allowed/denied receipt (append-only,
    # AUTOINCREMENT), never a clock (decided_at / signed_at tie within a stamp and step backwards) or rowid.
    # A ruling is applied once, so it has exactly ONE terminal receipt and that receipt names the row's state;
    # anything else (a receipt a raw writer appended to lift an older ruling above a newer one) is NULL, and a
    # NULL on any ruled row of the call fails the take closed (B1 L3 r2, codex HIGH)
    _RULING_SEQ = ("(SELECT CASE WHEN COUNT(*) = 1 AND MIN(c.state) = r.state THEN MIN(c.seq) END "
                   "FROM receipts c WHERE c.request_id = r.id AND c.state IN ('allowed','denied')) AS ruling_seq")

    def take_ruled_allow(self, agent: str, agent_session_id: str, tool_name: str, tool_input: Any, *, cwd: str,
                         event: str | None = None) -> Decision | None:
        """The ONE delivery of a ruled allow: the :class:`Decision` that pre-authorises this call, taken once.
        The identical call is this agent, session, ``tool_name`` and exact input (its digest). ``cwd`` is not part
        of it: a resumed session starts in its project directory, and a call filed from below it would otherwise
        never take its ruling (the request's working directory is shown with the ruling, not matched). Of its ruled requests only the LATEST ruling counts (the highest application sequence,
        the seq of its ruling receipt; never a clock, which ties within a stamp and can step backwards): it
        must be an allow, not yet taken, re-verified against a fresh replay, applied less than
        :data:`RULED_ALLOW_LAPSE` ago and less than that after it was signed, and no other ruling on the call may
        carry a later signed ``signed_at`` (a tamper bound only: it can void, never revive). So a later
        deny voids every earlier allow not yet taken, and two allows give one run. Selection and signature
        verification run outside the write lock; a short write lock then re-checks that the chosen ruling's
        state and seq are unchanged and nothing newer was applied, and records the take (selection runs once more
        when it moved, else ``None``); a second take returns ``None``. A cheap read-only check runs first: a call with no
        untaken ruled allow costs no replay and no write lock."""
        del cwd                                      # shown with the ruling, never keyed (see above)
        digest = tool_input_digest(tool_input)
        key = (agent, agent_session_id, tool_name, digest)
        where = ("FROM requests r WHERE agent = ? AND agent_session_id = ? AND tool_name = ? "
                 "AND tool_input_digest = ? AND via = 'ruling'")
        with self._read() as conn:
            if conn.execute("SELECT 1 " + where + " AND state = 'allowed' AND NOT EXISTS "
                            "(SELECT 1 FROM ruled_takes t WHERE t.request_id = r.id) LIMIT 1", key).fetchone() is None:
                return None
        authority = self._fresh_authority()
        if authority is None:
            return None
        for _attempt in range(2):                    # selection once more when a newer ruling landed meanwhile
            picked = self._pick_ruled_allow(where, key, authority)
            if picked is None:
                return None
            got, seq = picked
            with self._write() as conn:              # short: no signature is verified under the write lock
                now = _now_utc(self._clock)
                row = conn.execute(f"SELECT r.state, {self._RULING_SEQ} FROM requests r WHERE r.id = ?",
                                   (got.request_id,)).fetchone()
                newest, unsequenced = conn.execute(
                    f"SELECT MAX(ruling_seq), SUM(ruling_seq IS NULL) FROM (SELECT {self._RULING_SEQ} " + where +
                    " AND state IN ('allowed','denied'))", key).fetchone()
                if unsequenced:
                    return None                      # a ruled row on the call lost its one ruling receipt: fail closed
                if row is None or row[0] != "allowed" or row[1] != seq or newest != seq:
                    continue                         # it moved between the read and the lock
                if conn.execute("SELECT 1 FROM ruled_takes WHERE request_id = ?", (got.request_id,)).fetchone():
                    return None
                signed, decided = parse_stamp(got.signed_at), parse_stamp(got.decided_at)
                if now >= decided + RULED_ALLOW_LAPSE or decided >= signed + RULED_ALLOW_LAPSE:
                    return None
                conn.execute("INSERT INTO ruled_takes (request_id, taken_at, event) VALUES (?, ?, ?)",
                             (got.request_id, utc_stamp(now), event))
            return Decision(request_id=got.request_id, decision="allow", signer_fp=got.signer_fp,
                            decided_at=got.decided_at, request_digest=got.request_digest, via="ruling", gap=got.gap)
        return None

    def take_gate_allow(self, agent: str, agent_session_id: str, tool_name: str, tool_input: Any, *, cwd: str,
                        within: timedelta, posture: str | None = None, event: str | None = None) -> Decision | None:
        """The allow a PreToolUse gate already delivered for this exact call, taken ONCE by the native dialog's
        own hook that follows it: Claude Code still shows its dialog after a hook's allow on a path it protects
        (measured 2.1.302: a gate-allowed Edit of ``.claude/settings.json`` in acceptEdits went on to
        ``PermissionRequest``, ``decision_reason_type: safetyCheck``), and that event carries no ``tool_use_id``, so
        it cannot name the gate's request by event id. The call is matched on agent, session, ``tool_name``, exact
        input digest and ``cwd``; the request must be the NEWEST gate filing
        (``hook:`` event id) on that call, allowed by a signed answer or a grant, decided less than ``within`` ago,
        never taken, and re-verify now (:meth:`decision`, with ``tool_input`` and ``posture``); the age is checked again
        inside the write transaction that records the take. The take is recorded
        in ``ruled_takes`` (an allow taken by the call it authorised, at most once), so one answer never approves a
        second dialog. ``None`` for anything else: the dialog's hook then files its own request as before."""
        digest = tool_input_digest({} if tool_input is None else tool_input)
        key = (agent, agent_session_id, tool_name, digest, cwd)
        where = ("FROM requests r WHERE agent = ? AND agent_session_id = ? AND tool_name = ? "
                 "AND tool_input_digest = ? AND cwd = ? AND event_id LIKE 'hook:%'")
        with self._read() as conn:
            row = conn.execute("SELECT id, state, via, decided_at " + where +
                               " ORDER BY received_at DESC, rowid DESC LIMIT 1", key).fetchone()
            if row is None or row[1] != "allowed" or row[2] not in ("signature", "grant") or row[3] is None:
                return None
            if conn.execute("SELECT 1 FROM ruled_takes WHERE request_id = ?", (row[0],)).fetchone():
                return None
        try:
            age = _now_utc(self._clock) - parse_stamp(row[3])
        except ConsentError:
            return None
        if not timedelta(0) <= age < within:
            return None
        got = self.decision(row[0], tool_input=tool_input, posture=posture)
        if not isinstance(got, Decision) or got.decision != "allow":
            return None
        with self._write() as conn:
            if conn.execute("SELECT 1 FROM ruled_takes WHERE request_id = ?", (row[0],)).fetchone():
                return None
            newest = conn.execute("SELECT id, state, decided_at " + where +
                                  " ORDER BY received_at DESC, rowid DESC LIMIT 1", key).fetchone()
            if newest is None or newest[0] != row[0] or newest[1] != "allowed" or newest[2] != row[3]:
                return None                          # it moved between the read and the lock
            # the age again, under the lock: the replay and the wait for the lock can outlast ``within``
            now = _now_utc(self._clock)
            if not timedelta(0) <= now - parse_stamp(newest[2]) < within:
                return None
            conn.execute("INSERT INTO ruled_takes (request_id, taken_at, event) VALUES (?, ?, ?)",
                         (row[0], utc_stamp(now), event))
        return got

    def _pick_ruled_allow(self, where: str, key: tuple[Any, ...], authority: AuthoritySnapshot
                          ) -> tuple[Ruling, int] | None:
        """Outside any write lock: the call's latest ruling (by application sequence) when it is an untaken allow
        that re-verifies under ``authority`` and no earlier-applied ruling carries a later signed ``signed_at``;
        with its ruling receipt's seq, which :meth:`take_ruled_allow` re-checks under its short write lock."""
        with self._read() as conn:
            queue_id = conn.execute("SELECT value FROM meta WHERE key = 'queue_id'").fetchone()[0]
            cur = conn.execute(f"SELECT {self._RULED_COLS}, {self._RULING_SEQ} " + where +
                               " AND state IN ('allowed','denied') ORDER BY ruling_seq DESC", key)
            names = [d[0] for d in cur.description]
            rows = [dict(zip(names, raw)) for raw in cur.fetchall()]
            if not rows or rows[0]["state"] != "allowed" or any(type(r["ruling_seq"]) is not int for r in rows):
                return None                          # a ruled row with no ruling receipt: fail closed
            latest = rows[0]
            if conn.execute("SELECT 1 FROM ruled_takes WHERE request_id = ?", (latest["id"],)).fetchone():
                return None
            gap = self._gap_of(conn, latest["id"])
        request = self._request_of(latest)
        payload = None if request is None else self._stored_record(latest)
        if request is None or payload is None:
            return None
        got = self._verified_ruling(latest, request, queue_id, gap, payload, authority)
        if got is None or got.ruling != "allow":
            return None
        signed = parse_stamp(got.signed_at)
        for other in rows[1:]:
            rec = self._stored_record(other)
            if rec is None:
                return None                          # another ruling on the call cannot be read: fail closed
            try:
                if parse_stamp(rec.get("signed_at")) > signed:
                    return None                      # applied earlier yet signed later: fail closed
            except (ConsentError, TypeError):
                return None
        return got, latest["ruling_seq"]

    # the latest resume step of a ruled request ``r`` (NULL when it is unclaimed or a hook claimed it)
    _LAST_STEP = "(SELECT e.outcome FROM ruling_resume_steps e WHERE e.request_id = r.id ORDER BY e.seq DESC LIMIT 1)"
    # a ruling whose text has not reached its session: never claimed, or claimed for a resume that failed
    _DELIVERABLE = ("(NOT EXISTS (SELECT 1 FROM ruling_deliveries d WHERE d.request_id = r.id) OR "
                    + _LAST_STEP + " = 'failed')")

    def claim_rulings_for(self, agent: str, agent_session_id: str) -> list[Ruling]:
        """This session's rulings (allow and deny) whose text has not reached it yet (never claimed, or claimed for
        a resume that failed: :meth:`settle_resume`), oldest applied first (by application sequence, the seq of the
        ruling's receipt), each re-verified against a fresh replay outside the write lock, then re-checked unchanged
        and marked delivered under one short write lock: a ruling's text reaches its session once. A ruling that no
        longer verifies is neither returned nor marked, and one a resume holds (:meth:`lease_rulings_for`) is not
        returned. What the agent is told is the caller's; a returned allow still runs only through
        :meth:`take_ruled_allow`."""
        return self._claim(agent, agent_session_id, step=None)

    def lease_rulings_for(self, agent: str, agent_session_id: str) -> list[Ruling]:
        """As :meth:`claim_rulings_for`, for a RESUME that carries the texts as its prompt: each is claimed with a
        ``launching`` step, so the resumed session's own hooks find nothing to deliver again, and is delivered only
        when :meth:`settle_resume` confirms the resume (``started``); a ``failed`` one is deliverable again, by the
        session's next hook or another resume. A ``launching`` step nobody settles (the deliverer died between the
        claim and the settle) stays UNCONFIRMED (:meth:`delivery_state`): never counted delivered, never retried,
        since the resume may have run."""
        return self._claim(agent, agent_session_id, step="launching")

    def _claim(self, agent: str, agent_session_id: str, *, step: str | None) -> list[Ruling]:
        key = (agent, agent_session_id)
        with self._read() as conn:
            if conn.execute("SELECT 1 FROM requests r WHERE agent = ? AND agent_session_id = ? AND via = 'ruling' "
                            "AND " + self._DELIVERABLE + " LIMIT 1", key).fetchone() is None:
                return []
        authority = self._fresh_authority()
        if authority is None:
            return []
        with self._read() as conn:                   # read and verify outside the write lock
            queue_id = conn.execute("SELECT value FROM meta WHERE key = 'queue_id'").fetchone()[0]
            cur = conn.execute(f"SELECT {self._RULED_COLS}, {self._RULING_SEQ} FROM requests r WHERE agent = ? "
                               "AND agent_session_id = ? AND via = 'ruling' AND state IN ('allowed','denied') "
                               "AND " + self._DELIVERABLE + " ORDER BY ruling_seq IS NULL, ruling_seq", key)
            names = [d[0] for d in cur.description]
            rows = [dict(zip(names, raw)) for raw in cur.fetchall()]
            gaps = {row["id"]: self._gap_of(conn, row["id"]) for row in rows}
        verified: list[tuple[Ruling, dict[str, Any]]] = []
        for row in rows:
            request = self._request_of(row)
            payload = None if request is None else self._stored_record(row)
            if request is None or payload is None or type(row["ruling_seq"]) is not int:
                continue
            got = self._verified_ruling(row, request, queue_id, gaps[row["id"]], payload, authority)
            if got is not None:
                verified.append((got, row))
        out: list[Ruling] = []
        if not verified:
            return out
        with self._write() as conn:                  # short: re-check each, claim it, nothing verified here
            stamp = utc_stamp(_now_utc(self._clock))
            for got, row in verified:
                now = conn.execute(f"SELECT r.state, {self._RULING_SEQ} FROM requests r WHERE r.id = ?",
                                   (got.request_id,)).fetchone()
                if now is None or now[0] != row["state"] or now[1] != row["ruling_seq"]:
                    continue
                if conn.execute("SELECT 1 FROM ruling_deliveries WHERE request_id = ?",
                                (got.request_id,)).fetchone() is None:
                    if conn.execute("INSERT OR IGNORE INTO ruling_deliveries (request_id, agent, agent_session_id, "
                                    "delivered_at) VALUES (?, ?, ?, ?)",
                                    (got.request_id, agent, agent_session_id, stamp)).rowcount != 1:
                        continue                     # another claimer delivered it first
                    if step is not None:
                        self._insert_step(conn, got.request_id, agent_session_id, step, stamp)
                else:
                    last = self._last_step(conn, got.request_id)
                    if last is None or last[0] != "failed":
                        continue                     # delivered meanwhile, or a resume holds it
                    self._insert_step(conn, got.request_id, agent_session_id, step or "hook", stamp)
                taken = conn.execute("SELECT 1 FROM ruled_takes WHERE request_id = ?",
                                     (got.request_id,)).fetchone() is not None
                out.append(got if got.taken == taken else replace(got, taken=taken))
        return out

    @staticmethod
    def _last_step(conn: sqlite3.Connection, request_id: str) -> tuple[str, str] | None:
        return conn.execute("SELECT outcome, created_at FROM ruling_resume_steps WHERE request_id = ? "
                            "ORDER BY seq DESC LIMIT 1", (request_id,)).fetchone()

    @staticmethod
    def _insert_step(conn: sqlite3.Connection, request_id: str, agent_session_id: str, outcome: str, stamp: str,
                     argv: list[str] | None = None, cwd: str | None = None, detail: str | None = None) -> None:
        conn.execute("INSERT INTO ruling_resume_steps (request_id, agent_session_id, outcome, argv, cwd, detail, "
                     "created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                     (request_id, agent_session_id, outcome,
                      None if argv is None else json.dumps(argv, ensure_ascii=False), cwd, detail, stamp))

    def settle_resume(self, request_ids: list[str], *, agent_session_id: str, outcome: str, argv: list[str],
                      cwd: str, detail: str | None = None) -> None:
        """Record what became of the resume that leased these rulings (:meth:`lease_rulings_for`): ``started``
        (confirmed: the texts are delivered) or ``failed`` (deliverable again), with what was run. Only a ruling
        whose latest step is still ``launching`` is settled. Unsigned."""
        if outcome not in ("started", "failed"):
            raise ConsentError(f"outcome must be started or failed: {outcome!r}")
        with self._write() as conn:
            stamp = utc_stamp(_now_utc(self._clock))
            for rid in request_ids:
                last = self._last_step(conn, rid)
                if last is not None and last[0] == "launching":
                    self._insert_step(conn, rid, agent_session_id, outcome, stamp, argv, cwd, detail)

    def delivery_state(self, request_id: str) -> tuple[str, str | None]:
        """Where the request's ruling text is, with when it got there: ``undelivered`` (never claimed),
        ``delivered`` (claimed by a hook, or by a resume that was confirmed), ``failed`` (its resume failed:
        deliverable again) or ``unconfirmed`` (claimed for a resume that was never settled: it may have run)."""
        with self._read() as conn:
            claimed = conn.execute("SELECT delivered_at FROM ruling_deliveries WHERE request_id = ?",
                                   (request_id,)).fetchone()
            last = None if claimed is None else self._last_step(conn, request_id)
        if claimed is None:
            return "undelivered", None
        if last is None:
            return "delivered", claimed[0]
        return {"launching": "unconfirmed", "started": "delivered", "hook": "delivered"}.get(last[0], "failed"), last[1]

    def ruling_delivered(self, request_id: str) -> str | None:
        """When the request's ruling text reached its session (:meth:`delivery_state` ``delivered``), or ``None``."""
        state, when = self.delivery_state(request_id)
        return when if state == "delivered" else None

    def undelivered_rulings(self) -> list[Gapped]:
        """The ruled requests whose text is not known to have reached their session (undelivered, failed or
        unconfirmed: :meth:`delivery_state`), oldest gap first (sealed rows only; not re-verified here: delivery
        re-verifies when it claims)."""
        return self._gapped("r.via = 'ruling' AND r.state IN ('allowed','denied') AND (" + self._DELIVERABLE +
                            " OR " + self._LAST_STEP + " = 'launching')", ())

    def resume_steps(self, request_id: str) -> list[dict[str, Any]]:
        """Every resume step recorded for this request's ruling (:meth:`lease_rulings_for`,
        :meth:`settle_resume`), oldest first."""
        with self._read() as conn:
            rows = conn.execute("SELECT outcome, argv, cwd, detail, created_at, agent_session_id FROM "
                                "ruling_resume_steps WHERE request_id = ? ORDER BY seq", (request_id,)).fetchall()
        return [{"outcome": r[0], "argv": None if r[1] is None else json.loads(r[1]), "cwd": r[2], "detail": r[3],
                 "created_at": r[4], "agent_session_id": r[5]} for r in rows]

    # --- standing grants (Q4) --------------------------------------------------------------------------
    def _matching_grant(self, authority: AuthoritySnapshot, request: ExternalGateRequest, *, tool_input: Any,
                        posture: Any, now: datetime) -> tuple[str, int, str] | None:
        """``(grant_id, link seq, link hash)`` of the first consent grant (by id) in ``authority`` that answers
        ``request``, or ``None``."""
        for gid in sorted(authority.policy.grants):
            link = authority.grant_link(gid)
            if link is not None and grant_refusal(authority.policy.grants[gid], request, tool_input=tool_input,
                                                  posture=posture, now=now, home=self.home) is None:
                return gid, link[0], link[1]
        return None

    def answer_by_grant(self, request_id: str, *, tool_input: Any, posture: str) -> Decision | None:
        """Answer ``request_id`` with a standing consent grant when one covers it: a grant a FRESH replay of
        the authority log holds whose criteria (:func:`levain.consent.policy.grant_refusal`) hold for the
        request, ``tool_input`` (the input it was filed for) and ``posture`` at the queue's clock read under
        the write lock. Records ``pending -> allowed`` with the grant, its link and the posture, and its
        receipt, in one transaction, then returns the decision as :meth:`decision` delivers it (re-verified
        in another fresh replay). ``None`` when no grant covers it, the request is not pending, or the log
        cannot be replayed: the request is left as it was."""
        authority = self._fresh_authority()
        if authority is None or not authority.policy.grants:
            return None
        moved = False
        with self._write() as conn:
            request, state, stamp = self._settle(conn, request_id)
            if request is not None and state == "pending" and request.expires_at > stamp \
                    and request.tool_name != EGRESS_TOOL:
                found = self._matching_grant(authority, request, tool_input=tool_input, posture=posture,
                                             now=parse_stamp(stamp))
                if found is not None:
                    queue_id = conn.execute("SELECT value FROM meta WHERE key = 'queue_id'").fetchone()[0]
                    record = grant_answer_payload(queue_id=queue_id, request=request, grant_id=found[0],
                                                  grant_seq=found[1], grant_hash=found[2], posture=posture)
                    moved = self._transition(conn, request, "allowed", stamp=stamp,
                                             answer=answer_message(record).decode("utf-8"), via="grant",
                                             grant=record)
        return self.decision(request_id, tool_input=tool_input, posture=posture) if moved else None

    def _grant_decision(self, row: Mapping[str, Any], request: ExternalGateRequest, queue_id: str,
                        payload: Mapping[str, Any], *, tool_input: Any, posture: Any) -> Decision | None:
        """A grant-answered row's decision, re-verified now: the stored record binds this queue, this request
        and its digest; the deliverer holds the tool input the request was filed for and the posture recorded;
        and the named grant is still in a fresh replay, at the link recorded, unexpired, its criteria true at the
        queue's clock."""
        if set(payload) != _GRANT_ANSWER_KEYS or payload["format"] != GRANT_ANSWER_FORMAT \
                or payload["queue_id"] != queue_id or payload["request_id"] != request.id \
                or payload["request_digest"] != request.digest or payload["expires_at"] != request.expires_at \
                or payload["decision"] != "allow" or row["state"] != "allowed" or row["signer_fp"] is not None \
                or request.tool_name == EGRESS_TOOL:
            return None
        if tool_input is None or not isinstance(posture, str) or payload["posture"] != posture:
            return None
        gid = payload["grant_id"]
        authority = self._fresh_authority()
        grant = None if authority is None or not isinstance(gid, str) else authority.policy.grants.get(gid)
        # the link recorded is the one that created or last changed the grant in this replay: a raw writer cannot
        # name another, and a renew, tighten or loosen after the answer takes back an undelivered answer
        if grant is None or authority.grant_link(gid) != (payload["grant_seq"], payload["grant_hash"]) \
                or grant_refusal(grant, request, tool_input=tool_input, posture=posture,
                                          now=_now_utc(self._clock), home=self.home) is not None:
            return None
        return Decision(request_id=request.id, decision="allow", signer_fp=None, decided_at=row["decided_at"],
                        request_digest=request.digest, via="grant", grant_id=gid, grant_seq=payload["grant_seq"],
                        grant_hash=payload["grant_hash"], posture=posture)

    def decision(self, request_id: str, *, tool_input: Any = None, posture: str | None = None
                 ) -> Decision | Ruling | None:
        """The decision of a terminal request, re-verified now. A signed answer: the stored answer must be the
        canonical record of this row's decision for this queue and request, decided before its expiry, and its
        signature must verify under a key a fresh replay holds active (and of a class covering the tier, for an
        allow). A grant answer (``via`` ``grant``): delivered only to a caller holding the request's tool input
        and the posture recorded, while the grant still answers it in a fresh replay (:meth:`_grant_decision`);
        without ``tool_input`` it is ``None``. A late ruling (``via`` ``ruling``) comes back as a :class:`Ruling`,
        INFORMATION and never a deliverable decision (a ruled allow is delivered only by :meth:`take_ruled_allow`,
        once; ``taken`` says whether it has been): the stored ruling must name this row's decision and signer and
        verify for the request's gap number under a fresh replay (:meth:`_verified_ruling`). ``None`` while
        pending or undecided, for expired/withdrawn, and for anything that fails."""
        with self._read() as conn:
            row = self._row(conn, request_id)
            queue_id = conn.execute("SELECT value FROM meta WHERE key = 'queue_id'").fetchone()
            gap = self._gap_of(conn, request_id)
        if row is None or queue_id is None or row["state"] not in ("allowed", "denied"):
            return None
        request = self._request_of(row)
        decided_at = row["decided_at"]
        # "in time" is the SIGNED signed_at (checked in _refusal), never decided_at, which a raw writer sets
        payload = None if request is None else self._stored_record(row)
        if request is None or payload is None:
            return None
        if row["via"] == "ruling":
            with self._read() as conn:
                taken = conn.execute("SELECT 1 FROM ruled_takes WHERE request_id = ?", (request_id,)).fetchone()
            return self._verified_ruling(row, request, queue_id[0], gap, payload, self._fresh_authority(),
                                         taken=taken is not None)
        if row["via"] == "grant":
            return self._grant_decision(row, request, queue_id[0], payload, tool_input=tool_input, posture=posture)
        if row["via"] != "signature":
            return None
        named = payload.get("decision")
        if not isinstance(named, str) or _STATE_OF.get(named) != row["state"] \
                or payload.get("signer_fp") != row["signer_fp"]:
            return None
        authority = self._fresh_authority()
        if authority is None or self._refusal(request, queue_id[0], payload, row["signature"], authority,
                                              unchained_ok=True) is not None:
            return None
        return Decision(request_id=request.id, decision=payload["decision"], signer_fp=payload["signer_fp"],
                        decided_at=decided_at, request_digest=request.digest)

    def await_decision(self, request_id: str, timeout: float, *, poll: float = 0.1,
                       sleep: Callable[[float], None] = time.sleep, tool_input: Any = None,
                       posture: str | None = None) -> Decision | Ruling | None:
        """Wait up to ``timeout`` seconds for a verified decision. ``None`` is NO decision: the request's hold
        ended (its ``expires_at``), it expired, was withdrawn, went undecided, is unknown, its stored answer no longer verifies, or it is still
        pending when the wait ends (the adapter then outputs nothing, so the native dialog stands, and withdraws or
        lets it expire). A request ruled late returns its :class:`Ruling`, information only, as :meth:`decision`
        does. ``poll`` below 1ms (or not a number) is 1ms. ``tool_input`` / ``posture`` are passed to
        :meth:`decision` (a grant answer is delivered only with them)."""
        poll = poll if poll >= 0.001 else 0.001             # NaN compares false: it becomes 1ms too
        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            got = self.get(request_id)
            if got is None:
                return None
            if got[1] != "pending":
                return self.decision(request_id, tool_input=tool_input, posture=posture)
            if got[0].expires_at <= utc_stamp(_now_utc(self._clock)):
                return None                          # the hold ended: the hook marks it undecided
            left = deadline - time.monotonic()
            if left <= 0:
                return None
            sleep(min(poll, left))

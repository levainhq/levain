"""levain.consent.agentboundary — the consent queue's receipts read as AgentBoundary v0.2-alpha Action Receipts (Q19).

AgentBoundary (jamjet-labs/agentboundary) is a receipt format for what an agent was allowed to do: closed JSON,
RFC 8785 (JCS) plus SHA-256 for ``arguments_hash`` and ``receipt_hash``, a singly linked ``prior_receipt`` chain.
This module is a MAPPING of ``queue.db`` (and, for what ran, the authority ledger beside it) onto it, computed on
every call from the rows, like :mod:`levain.consent.receipt_log`: no second store, no schema change of its own, no
hash kept anywhere. Both databases are opened read-only and never created, migrated or written.

  - one AgentBoundary receipt per TERMINAL receipt row (``allowed``, ``denied``, ``expired``, ``withdrawn``,
    ``undecided``), in ``seq`` order; ``pending`` rows stay in the Q11 log and are not exported. An undecided request
    that was ruled later has two rows and so two receipts. The map is one to one, and each receipt names its leaf in
    ``execution.result_ref`` (``levain-receipt:<queue_id>:<seq>``);
  - ``receipt_id`` is a UUIDv5 of ``<queue_id>:<seq>`` under a fixed namespace, and ``prior_receipt`` links each
    receipt to the one before it in the export, so a re-export reproduces the same ids and the same chain with
    nothing stored. The first receipt omits ``prior_receipt`` (genesis);
  - every receipt carries ``provenance`` (per path: ``observed`` read from one queue column as it is, ``inferred``
    composed or derived from what is stored, ``synthesized`` something the queue has no source for) and the
    ``completeness_score`` the spec's formula gives it; the score is part of ``receipt_hash``, so it is computed here
    by the same integer rule and never rounded;
  - ``receipt_hash`` is the sha256 of the JCS of the receipt without it, encoded by Levain's own canonical form
    (:func:`levain.cockpit.consent.encode`), the one ``tool_input_digest`` is already made with. That form is
    defined over integers and strings; the one number a receipt holds, ``completeness_score``, is a three-decimal
    fraction and is passed through it as its exact ES6 text (:class:`_Fraction`), never as a float.

Execution comes from evidence, never from permission. The queue proves a tool was PERMITTED; whether it ran is
the agent's own record, which the authority ledger keeps (``ledger.db`` beside ``queue.db``: the ``post`` and
``post_failure`` rows its hooks file when a tool call ends). For an ``allowed`` request the exporter finds the ledger
row of the same call (agent, session, tool, input digest, and the tool-use id when both sides carry one, each
ledger row used by one request, not before the decision and inside the settle window) and gives ``post`` ->
``success`` and ``post_failure`` -> ``failure`` with ``error_code`` ``tool-failed``, both ``inferred``. An allowed
request with no such row cannot be said to have run: while it is younger than ``SETTLE`` it is UNSETTLED (below),
and after that it is ``blocked`` with no refusal and ``error_code`` ``execution-unrecorded``, ``synthesized``. That is
the conservative falsehood: an audit record that under-claims, never one that claims an action ran. A store with no
ledger (an older install) has no evidence and follows the same rule. A request the agent's own dialog answered gets
its execution from ``dialog_outcomes`` instead: the row's ``event`` is the hook event that closed it
(``PostToolUse`` -> ``success``, ``PostToolUseFailure`` -> ``failure``).

What each queue state becomes (``policy.decision`` / ``execution.status`` / ``refusal``):
  allowed by a standing grant: allow / by evidence / none · allowed by a signed answer or ruling: require-approval /
  by evidence / none, with the ``approval`` block · denied: require-approval / blocked / deny ``operator-denied``, with
  NO approval block (a denial is not an approval; the signer stays findable through ``execution.result_ref``) ·
  expired: require-approval / blocked / expired ``approval-window-elapsed`` · undecided and withdrawn:
  require-approval / blocked / require-approval with the reason ``undecided`` or ``withdrawn``.
  An expired request with no counted dialog outcome that holds a kept refused ALLOW (``refused_answers``: an
  answer that verified under the key it names and was refused) carries it as the evidence of the refusal, the
  latest one kept less than ``SETTLE`` before the row and less than ``SETTLE`` minus the clock slack after it:
  ``approver-not-authorized`` -> refusal deny ``approver-not-authorized``; ``approval-window-elapsed`` -> the refusal
  stays expired ``approval-window-elapsed``. Both carry an ``approval`` block: approver ``ssh-key:<signer_fp>``
  (inferred), ``approved_at`` the answer's ``signed_at`` (inferred: the signer's claim), a context naming it a
  refused answer with its key's standing and levain's reason. The approver gets ``role: operator`` only for a key
  the authority log held, refused for its class or a plain revocation; an unenrolled key (anyone can make one) or a
  compromised one gets no role (the schema's ``role`` is optional) and the context says ``enrolled`` / ``compromised``.
  The action stays ``blocked``. A refused deny is never approval evidence. ``issued_at`` is moved to the refusal's
  time when it is later than the row's.
  A withdrawn or expired request left to the agent's own dialog gets its counted ``dialog_outcomes`` row mapped over
  the queue's state, because the queue's state then describes a request the agent answered, not levain, and
  ``blocked`` for a tool that ran would be a false record: ``did_not_run``: blocked with no refusal · ``ran`` (a yes
  in the agent's dialog, unsigned): require-approval, with an ``approval`` block whose approver is
  ``native-dialog:operator`` (provenance inferred), ``approved_at`` the request's ``received_at`` (no approval can
  precede the request, and the dialog's answer is not recorded: a lower bound, ``synthesized``) and a context of
  ``via: native-dialog, signed: false``, so it is never read as levain's signed answer · ``ran_unasked`` (the agent's
  own standing permission): policy ``<framework>.native-permissions`` version ``unknown`` (inferred / synthesized),
  allow, no approval. For both ``completed_at`` is the outcome's ``closed_at``, the moment the tool ended as levain
  learned it.
  ``approval.approved_at`` of a signed allow is the signed answer's own ``signed_at`` (``inferred``: the signer's
  claim), so the approval precedes ``completed_at``. When the answer holds no ``signed_at`` or it is not before the
  decision time, ``approved_at`` is the decision time, marked ``synthesized`` and counted in the report.
  ``policy.version`` is ``unknown`` (``synthesized``): the queue never recorded which code decided, and the version of
  the levain that happens to export is not it.
  ``issued_at`` is the later of the terminal row's stamp and the execution's time (a ``ran`` answered while the hook
  held the request is stamped in the same write as the withdrawn row, and ``close_unrun`` can precede an expiry row),
  so ``completed_at`` never follows ``issued_at``; it is ``inferred`` when it is the execution's time.

Why an exported receipt never changes (the settle rule). Evidence is written AFTER the terminal row it describes, so
reading it at export time could turn ``blocked`` into ``success`` under one ``receipt_id``. Rules over row values and
the export's clock (``now``, a parameter) make that impossible. Evidence counts only when its time is less than
``SETTLE`` after the terminal row's ``created_at`` (a negative difference is the normal order for a dialog outcome
and counts). A row whose execution is still open and which has no counted evidence, and that is younger than
``SETTLE``, is UNSETTLED: an ``allowed`` row with no ledger evidence, or a ``withdrawn`` / ``expired`` row with no
counted outcome. The export stops at the first unsettled row and reports how many terminal rows it held back
(``held_unsettled``). A row at least ``SETTLE`` old exports by what it holds, and evidence that arrives later is past
the window and changes nothing. So an exported prefix is the same at every later export, ids, hashes and links. That
holds only while ``now`` is not ahead of the writers' clocks: a ``now`` later than the real clock (by more than a few
seconds) is refused. A writer that holds an outcome transaction open across the window boundary is a known edge;
outcomes are written within the turn.

Fail closed: a value this module would emit that the spec rejects (a digest that is not 64 lowercase hex characters,
a timestamp that does not parse, an empty required string, a score that is not a number) raises
:class:`ExportError`, never an invalid receipt. A column is parsed only where it is used.

Known edges (each stated, none built against):
  - a kept refused answer is covered by no signed head (``refused_answers`` is outside the Q11 log): a raw writer of
    ``queue.db`` can add or drop one, so the approval block of a refusal is evidence that an attempt was made and
    refused, never authority. A malformed row is passed over, never an export error;
  - NO signature and NO COSE envelope. The spec's envelope is Ed25519 only, while an operator key may be a security
    key or a passkey (P-256), and a key Levain held itself would be the mediator's signature Q11 refused. Authenticity
    stays Levain's own: the operator's signed answer and the Q11 head, checked by ``levain consent receipts verify``.
    A receipt here proves its content is intact against its hash and its neighbours, never who produced it;
  - ``agent.framework_version``, ``agent.model`` and ``policy.version`` are ``"unknown"`` (the hook event carries
    none of them), and ``target.system`` and ``target.environment`` are fixed words; all of them are ``synthesized``,
    which lowers the score;
  - ``arguments_hash`` is the stored ``tool_input_digest`` without its ``jcs:`` prefix. Levain's canonical form
    NFC-normalises strings and the spec's does not, so a verifier handed arguments holding a non-NFC string computes
    another hash than the one stored. A digest under the ``json:`` prefix (a tool input holding a float, outside JCS's
    domain) is not a JCS hash at all: it is emitted as stored, marked ``synthesized`` and counted in the report;
  - the receipt's own strings are NFC-normalised for the same reason (a decomposed working directory would otherwise
    hash differently under the two forms); the count is in the report. ``resource_id`` can then differ from the
    stored ``cwd`` in its byte form, never in its characters;
  - ``actor.id`` carries the session id and ``resource_id`` the working directory because the closed schema has no
    field for either; a reader of an exported receipt learns both;
  - the ledger pairs a call to its request by the tool-use id when both carry one; with none on either side, two
    identical calls in one session are told apart only by order, so the earlier request takes the earlier ledger row;
  - ``completed_at`` is the execution's own time or, for a state with no decision, the receipt's own stamp
    (``inferred``): AgentBoundary timestamps are the emitter's own, and so are these;
  - the databases are opened ``mode=ro``, and a WAL store needs a writable directory or an existing ``-shm`` file for
    that: an export against a store whose directory the exporting user cannot write fails with ``ExportError``.

Stdlib only.
"""
from __future__ import annotations

import json
import math
import re
import sqlite3
import unicodedata
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, NamedTuple

from levain.cockpit.consent import ConsentError, parse_stamp, record_sha256
from levain.cockpit.consent import encode as jcs_encode

__all__ = ["AB_VERSION", "Export", "ExportError", "Report", "SETTLE", "export", "policy_identity", "receipt_hash"]

AB_VERSION = "agentboundary/v0.2-alpha"
# fixed, so a re-export reproduces every receipt_id; it is uuid5(NAMESPACE_URL, _NS_NAME), written out so an edit to
# the name cannot change the ids; a test pins the pair
LEVAIN_AB_NS = uuid.UUID("61dad17c-6f1f-5c59-988c-3035a88ad44e")
_NS_NAME = "https://levain.dev/consent/agentboundary-export/v0.2-alpha"

# evidence counts only inside this window after its terminal row, and a row whose execution is still open and younger
# than it is not exported yet (the settle rule in the module docstring)
SETTLE = timedelta(hours=24)
# how far ahead of the real clock an export's ``now`` may be (the clocks of two processes are never exactly equal)
_CLOCK_SLACK = timedelta(seconds=5)
_TERMINAL = frozenset({"allowed", "denied", "expired", "withdrawn", "undecided"})
_UNKNOWN = "unknown"
_FRAMEWORKS = {"claude_code": "claude-code", "codex": "codex"}
_WEIGHTS = {"observed": 2, "inferred": 1, "synthesized": 0}
_HEX64 = re.compile(r"[0-9a-f]{64}", re.ASCII)
# the hook events that close a tool call, and the execution status each says
_TOOL_STATUS = {"PostToolUse": "success", "PostToolUseFailure": "failure"}
_LEDGER_STATUS = {"post": "success", "post_failure": "failure"}

# the spec's provenance paths: always present, then present only when the leaf is
_REQUIRED_PATHS = ("receipt_id", "issued_at", "actor.type", "actor.id", "agent.framework", "agent.framework_version",
                   "agent.model", "tool.name", "tool.capability", "target.system", "target.environment",
                   "arguments_hash", "policy.name", "policy.version", "policy.decision", "execution.status",
                   "execution.completed_at")
_CONDITIONAL_PATHS = ("actor.display_name", "agent.model_version", "tool.version", "target.resource_id",
                      "approval.approver.id", "approval.approver.display_name", "approval.approver.role",
                      "approval.approved_at", "approval.context", "execution.error_code", "execution.result_ref",
                      "prior_receipt.receipt_id", "prior_receipt.receipt_hash", "refusal.verdict",
                      "refusal.reason_code")

_SQL = ("SELECT r.seq, r.request_id, r.state, r.created_at, q.id, q.agent, q.agent_session_id, q.tool_name, "
        "q.tool_input_digest, q.cwd, q.tier, q.state, q.decided_at, q.signer_fp, q.via, q.answer, q.received_at, "
        "q.event_id, {outcome}, {closed}, {event} "
        "FROM receipts r LEFT JOIN requests q ON q.id = r.request_id {join} ORDER BY r.seq")
_REQUEST_TEXT = (4, 5, 6, 7, 8, 9, 10, 11, 16, 17)     # the request columns every exported row must hold as text
_REQUIRED_TEXT = (5, 6, 7, 9, 10)                      # ... and the ones that must not be empty (they are emitted)
_POSTS_SQL = ("SELECT rowid, agent, agent_session_id, tool_use_id, tool_name, tool_input_digest, phase, received_at "
              "FROM calls WHERE phase IN ('post', 'post_failure') ORDER BY received_at, rowid")
_PRE_SQL = ("SELECT request_id, tool_use_id FROM calls WHERE phase = 'pre' AND request_id IS NOT NULL "
            "AND tool_use_id IS NOT NULL")
_REFUSED_SQL = ("SELECT seq, request_id, kind, refused_at, reason_code, subreason, enrolled, detail, decision, "
                "signer_fp, signed_at FROM refused_answers ORDER BY seq")
_REFUSED_REASONS = frozenset({"approver-not-authorized", "approval-window-elapsed"})
# the refusals of a key the operator holds (enrolled, refused only for its class or a plain revocation): the only ones
# whose signer is named an operator; an unenrolled key anyone can make, and a compromised one may be a thief's
_OPERATOR_SUBREASONS = frozenset({"class", "revoked"})
# a refused answer kept this long after an expired row counts as its evidence: SETTLE less the export clock's slack,
# so one written after an export that took the row (``now - created >= SETTLE``, ``now`` at most the slack ahead of
# the writer's clock) is past the window and changes nothing
_REFUSAL_AFTER = SETTLE - _CLOCK_SLACK


class ExportError(ValueError):
    """The queue cannot be exported: not a readable current-format store, or a row no receipt can be made of."""


class _Row(NamedTuple):
    seq: Any
    request_id: Any
    rstate: Any
    created: Any
    qid: Any
    agent: Any
    session: Any
    tool: Any
    digest: Any
    cwd: Any
    tier: Any
    qstate: Any
    decided: Any
    signer: Any
    via: Any
    answer: Any
    received: Any
    event_id: Any
    outcome: Any
    closed: Any
    oevent: Any


class _Refused(NamedTuple):
    seq: Any
    request_id: Any
    kind: Any
    refused_at: Any
    reason_code: Any
    subreason: Any
    enrolled: Any
    detail: Any
    decision: Any
    signer_fp: Any
    signed_at: Any


@dataclass(frozen=True)
class Report:
    """What an export did. ``by_state``: exported receipts per receipt-row state. ``pending_skipped``: ``pending``
    rows not exported. ``synthesized_arguments_hash``: receipts whose ``arguments_hash`` is a ``json:`` digest, not a
    JCS hash (the spec's verifier given the arguments would flag them). ``dialog_ran`` / ``dialog_ran_unasked``:
    receipts whose queue-state mapping a ``ran`` / ``ran_unasked`` dialog outcome overrode.
    ``dialog_did_not_run``: receipts mapped ``blocked`` with no refusal for that outcome. ``nfc_normalised``:
    receipts whose own strings were changed by NFC. ``approved_at_fallback``: signed allows whose answer gave no
    ``signed_at`` before the decision time, so ``approved_at`` is the decision time (``synthesized``).
    ``execution_failed``: receipts whose tool call ended in failure. ``execution_unrecorded``: allowed requests old
    enough to settle with no ledger evidence, exported ``blocked``. ``ledger_found``: whether the authority ledger
    was there to read. ``held_unsettled``: terminal rows not exported because the first unsettled row and all after
    it were held back. ``refused_not_authorized`` / ``refused_window_elapsed``: expired rows exported with a kept
    refused answer as their approval evidence, for each reason."""
    receipts: int
    by_state: Mapping[str, int]
    pending_skipped: int
    synthesized_arguments_hash: int
    dialog_ran: int
    dialog_ran_unasked: int
    dialog_did_not_run: int
    nfc_normalised: int
    approved_at_fallback: int
    execution_failed: int
    execution_unrecorded: int
    ledger_found: bool
    held_unsettled: int
    refused_not_authorized: int
    refused_window_elapsed: int


@dataclass(frozen=True)
class Export:
    receipts: list[dict[str, Any]]
    report: Report

    def referenced_policies(self) -> set[tuple[str, str]]:
        """The ``(policy.name, policy.version)`` pairs the receipts NAME. This is what the receipts claim, never a
        trusted policy store: handed to a verifier as its store it would bless any pair a receipt invents. A policy
        store comes from a source independent of the receipts being checked."""
        return {(r["policy"]["name"], r["policy"]["version"]) for r in self.receipts}


class _Fraction(int):
    """``completeness_score`` in thousandths, for Levain's canonical form, which has no float. The form writes an
    integer with ``str()``, so this prints the exact ES6 number text of ``milli / 1000`` instead (``1``, ``0.8``,
    ``0.005``): the three-decimal fractions the spec's integer formula yields all have a short exact form."""

    def __str__(self) -> str:
        milli = int(self)
        whole, frac = divmod(milli, 1000)
        return str(whole) if not frac else f"{whole}.{frac:03d}".rstrip("0")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _nfc(value: Any) -> Any:
    if isinstance(value, str):
        return unicodedata.normalize("NFC", value)
    if isinstance(value, list):
        return [_nfc(v) for v in value]
    if isinstance(value, dict):
        return {_nfc(k): _nfc(v) for k, v in value.items()}
    return value


def _present(receipt: Mapping[str, Any], dotted: str) -> bool:
    cursor: Any = receipt
    for part in dotted.split("."):
        if not isinstance(cursor, Mapping) or part not in cursor:
            return False
        cursor = cursor[part]
    return True


def _score_milli(receipt: Mapping[str, Any], provenance: Mapping[str, str]) -> int:
    """The spec's score, in thousandths: ``floor(weighted_sum * 1000 / (2 * total))`` over the applicable paths, a
    path with no provenance entry weighing as ``synthesized``."""
    paths = list(_REQUIRED_PATHS) + [p for p in _CONDITIONAL_PATHS if _present(receipt, p)]
    weighted = sum(_WEIGHTS[provenance.get(p, "synthesized")] for p in paths)
    return (weighted * 1000) // (2 * len(paths))


def receipt_hash(receipt: Mapping[str, Any]) -> str:
    """The sha256 (lowercase hex) of the JCS of ``receipt`` without its ``receipt_hash`` member, in Levain's own
    canonical form. ``completeness_score`` (a float in the exported dict) is written as its exact decimal text.
    Raises :class:`ExportError` for a score that is not a finite number, or a value that form cannot encode (a lone
    surrogate, an integer past 2**53 - 1)."""
    body: dict[str, Any] = {k: v for k, v in receipt.items() if k != "receipt_hash"}
    score = body.get("completeness_score")
    if score is not None:
        if isinstance(score, bool) or not isinstance(score, (int, float)) or not math.isfinite(score):
            raise ExportError(f"completeness_score is not a number: {score!r}")
        body["completeness_score"] = _Fraction(round(score * 1000))
    try:
        return record_sha256(jcs_encode(body))
    except ConsentError as exc:
        raise ExportError(f"the receipt cannot be canonically encoded: {exc}") from None


def policy_identity(tier: str) -> tuple[str, str]:
    """The ``(policy.name, policy.version)`` a receipt of a request in ``tier`` names: the queue never recorded which
    code decided, so the version is ``"unknown"``."""
    return f"levain.consent.{tier}", _UNKNOWN


def _open_ro(path: Path, what: str) -> sqlite3.Connection:
    try:
        return sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=30.0, isolation_level=None)
    except sqlite3.Error as exc:
        raise ExportError(f"{what} {path} cannot be opened read-only: {exc}") from None


def _open(queue_dir: Path) -> sqlite3.Connection:
    from levain.consent.queue import DB_NAME
    return _open_ro(Path(queue_dir) / DB_NAME, "the queue")


def _read(queue_dir: Path) -> tuple[str, list[_Row], dict[str, list[_Refused]]]:
    """``(queue_id, rows, refused)`` in one read snapshot, ``refused`` the kept refused answers by request id (empty
    for a store from before the table). Raises :class:`ExportError` when the store is not a readable current-format
    consent queue."""
    from levain.consent.queue import STORE_FORMAT
    conn = _open(queue_dir)
    try:
        conn.execute("PRAGMA busy_timeout = 30000")
        conn.execute("BEGIN")
        try:
            have = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
            if not {"meta", "requests", "receipts"} <= have:
                raise ExportError("the store holds no consent queue (it lacks meta, requests or receipts)")
            meta = dict(conn.execute("SELECT key, value FROM meta WHERE key IN ('format', 'queue_id')").fetchall())
            if meta.get("format") != STORE_FORMAT:
                raise ExportError(f"the store is in format {meta.get('format')!r}, not {STORE_FORMAT!r}")
            if not isinstance(meta.get("queue_id"), str) or not meta["queue_id"]:
                raise ExportError("the store has no queue_id")
            if "dialog_outcomes" in have:
                sql = _SQL.format(outcome="d.outcome", closed="d.closed_at", event="d.event",
                                  join="LEFT JOIN dialog_outcomes d ON d.request_id = q.id")
            else:
                sql = _SQL.format(outcome="NULL", closed="NULL", event="NULL", join="")
            rows = [_Row(*r) for r in conn.execute(sql).fetchall()]
            refused: dict[str, list[_Refused]] = {}
            if "refused_answers" in have:
                for r in conn.execute(_REFUSED_SQL).fetchall():
                    refused.setdefault(r[1], []).append(_Refused(*r))
        except sqlite3.Error as exc:
            raise ExportError(f"the store cannot be read: {exc}") from None
    finally:
        conn.close()                # ends the read transaction with it; nothing here can replace an ExportError
    return meta["queue_id"], rows, refused


class _Evidence:
    """The authority ledger's end-of-call rows, read once, and the pairing of an ``allowed`` request with the one that
    is its call. No ledger file means no evidence."""

    def __init__(self, queue_dir: Path) -> None:
        from levain.consent.ledger import DB_NAME, STORE_FORMAT
        self.found = False
        self._posts: dict[tuple[str, str, str, str], list[tuple[Any, ...]]] = {}
        self._pre: dict[str, str] = {}
        self._claimed: set[int] = set()
        path = Path(queue_dir) / DB_NAME
        if not path.exists():
            return
        conn = _open_ro(path, "the authority ledger")
        try:
            conn.execute("PRAGMA busy_timeout = 30000")
            conn.execute("BEGIN")
            try:
                have = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
                if not {"meta", "calls"} <= have:
                    raise ExportError("the authority ledger holds no calls table")
                row = conn.execute("SELECT value FROM meta WHERE key = 'format'").fetchone()
                if row is None or row[0] != STORE_FORMAT:
                    raise ExportError(f"the authority ledger is in format {row and row[0]!r}, not {STORE_FORMAT!r}")
                posts = conn.execute(_POSTS_SQL).fetchall()
                pre = conn.execute(_PRE_SQL).fetchall()
            except sqlite3.Error as exc:
                raise ExportError(f"the authority ledger cannot be read: {exc}") from None
        finally:
            conn.close()
        self.found = True
        for p in posts:
            if not all(type(p[i]) is str for i in (1, 2, 4, 5, 6, 7)) or type(p[0]) is not int:
                continue                                  # not a row any request could pair with
            self._posts.setdefault((p[1], p[2], p[4], p[5]), []).append(p)
        for request_id, tool_use_id in pre:
            if type(request_id) is str and type(tool_use_id) is str:
                self._pre[request_id] = tool_use_id

    def match(self, row: _Row, lower: datetime, upper: datetime) -> tuple[str, str] | None:
        """``(status, time)`` of the ledger row that is this request's call: the same agent, session, tool and
        input digest, a tool-use id equal when both carry one (an equal one is preferred), the time not before
        ``lower`` and before ``upper``, the earliest such row no earlier request took. ``None`` when there is none."""
        use_id = row.event_id[len("hook:"):] if row.event_id.startswith("hook:") else self._pre.get(row.qid)
        best: tuple[tuple[Any, ...], tuple[Any, ...], str] | None = None
        for p in self._posts.get((row.agent, row.session, row.tool, row.digest), ()):
            if p[0] in self._claimed or (use_id is not None and p[3] is not None and p[3] != use_id):
                continue
            at = _stamp(p[7], "ledger row time", row.seq)
            if not lower <= at < upper:
                continue
            key = (0 if use_id is not None and p[3] == use_id else 1, at, p[0])
            if best is None or key < best[0]:
                best = (key, p, p[6])
        if best is None:
            return None
        self._claimed.add(best[1][0])
        return _LEDGER_STATUS[best[2]], best[1][7]


def _row_problem(row: _Row) -> str | None:
    seq = row.seq
    if type(seq) is not int:
        return f"a receipt's seq is not an integer: {seq!r}"
    if type(row.request_id) is not str or type(row.rstate) is not str or type(row.created) is not str:
        return f"receipt {seq}: its request_id, state or created_at is not text"
    if row.qid is None:
        return f"receipt {seq}: it names a request the queue does not hold"
    if any(type(row[i]) is not str for i in _REQUEST_TEXT):
        return f"receipt {seq}: its request has a column that is not text"
    if any(not row[i] for i in _REQUIRED_TEXT):
        return f"receipt {seq}: its request has an empty agent, session, tool, working directory or tier"
    return None


def _stamp(text: Any, what: str, seq: int) -> datetime:
    try:
        return parse_stamp(text)
    except ConsentError:
        raise ExportError(f"receipt {seq}: its {what} is not a timestamp") from None


def _counted_outcome(row: _Row) -> tuple[str, str, str] | None:
    """``(outcome, closed_at, event)`` of the row's dialog outcome when it counts (closed less than ``SETTLE`` after
    the terminal row), else ``None``. Parsed only for a withdrawn or expired row: no other state uses one."""
    if row.rstate not in ("withdrawn", "expired") or row.outcome is None:
        return None
    if _stamp(row.closed, "dialog outcome time", row.seq) - _stamp(row.created, "stamp", row.seq) >= SETTLE:
        return None
    if type(row.outcome) is not str:
        return None
    if row.outcome != "did_not_run" and _TOOL_STATUS.get(row.oevent) is None:
        raise ExportError(f"receipt {row.seq}: its dialog outcome was closed by {row.oevent!r}, not a tool event")
    return row.outcome, row.closed, row.oevent


def _refused_evidence(row: _Row, kept: list[_Refused]) -> _Refused | None:
    """The kept refused answer an ``expired`` row exports as its approval evidence: the latest (by ``refused_at``,
    then ``seq``) refused ALLOW kept less than ``SETTLE`` before the row's ``created_at`` and less than
    :data:`_REFUSAL_AFTER` after it. A refused deny is never approval evidence and is passed over. So is a row that
    is not well-formed (text columns, a known reason, parseable times): the table is covered by no signed head, so a
    raw writer's malformed row neither fails the export nor reaches a receipt."""
    created = _stamp(row.created, "stamp", row.seq)
    best: tuple[tuple[datetime, int], _Refused] | None = None
    for r in kept:
        if type(r.seq) is not int or type(r.enrolled) is not int or r.enrolled not in (0, 1):
            continue
        if not all(type(v) is str and v for v in (r.kind, r.refused_at, r.reason_code, r.subreason, r.detail,
                                                  r.decision, r.signer_fp, r.signed_at)):
            continue
        if r.decision != "allow" or r.reason_code not in _REFUSED_REASONS:
            continue
        try:
            at = parse_stamp(r.refused_at)
            parse_stamp(r.signed_at)
        except ConsentError:
            continue
        if not -SETTLE < at - created < _REFUSAL_AFTER:
            continue
        if best is None or (at, r.seq) > best[0]:
            best = ((at, r.seq), r)
    return None if best is None else best[1]


def _approved_at(answer: Any, decided: str) -> tuple[str, bool]:
    """``(approved_at, from the answer)``: the signed answer's ``signed_at`` when it is before the decision time."""
    try:
        signed = json.loads(answer)["signed_at"]
        if parse_stamp(signed) < parse_stamp(decided):
            return signed, True
    except (TypeError, ValueError, KeyError, ConsentError):
        pass
    return decided, False


def _map(queue_id: str, row: _Row, outcome: tuple[str, str, str] | None, evidence: tuple[str, str] | None,
         counters: dict[str, int], refused: _Refused | None = None) -> tuple[dict[str, Any], dict[str, str]]:
    """The receipt (without ``prior_receipt``, ``provenance``, score and hash) and its provenance entries.
    ``outcome`` is the row's dialog outcome when it counts (:func:`_counted_outcome`), ``evidence`` the ledger's
    ``(status, time)`` for an allowed request, ``refused`` the kept refused answer of an expired row with no counted
    outcome (:func:`_refused_evidence`), else ``None``."""
    seq, rstate, created, qstate, via = row.seq, row.rstate, row.created, row.qstate, row.via
    _stamp(created, "stamp", seq)
    issued = created
    framework = _FRAMEWORKS.get(row.agent, row.agent.replace("_", "-"))
    # composed values are `inferred`: the actor id joins two columns, the framework is a renamed agent, the policy
    # name a prefixed tier; only a column copied as it is counts as `observed`
    prov: dict[str, str] = {
        "receipt_id": "inferred", "issued_at": "observed", "actor.type": "inferred", "actor.id": "inferred",
        "agent.framework": "inferred", "agent.framework_version": "synthesized", "agent.model": "synthesized",
        "tool.name": "observed", "tool.capability": "inferred", "target.system": "synthesized",
        "target.environment": "synthesized", "target.resource_id": "observed", "policy.name": "inferred",
        "policy.version": "synthesized", "policy.decision": "inferred", "execution.result_ref": "inferred",
        "execution.completed_at": "inferred", "execution.status": "inferred"}
    digest = row.digest
    if digest.startswith("jcs:"):
        arguments_hash = digest[4:]
        prov["arguments_hash"] = "observed"
    elif digest.startswith("json:"):
        arguments_hash = digest[5:]
        prov["arguments_hash"] = "synthesized"
        counters["synthesized_arguments_hash"] += 1
    else:
        raise ExportError(f"receipt {seq}: its tool input digest has no known scheme")
    if _HEX64.fullmatch(arguments_hash) is None:
        raise ExportError(f"receipt {seq}: its tool input digest is not 64 lowercase hex characters")
    name, version = policy_identity(row.tier)
    leaf = f"levain-receipt:{queue_id}:{seq}"
    # the request's own decision time belongs to the row that IS its final state; any other row is stamped by itself
    decision_row = row.decided is not None and rstate == qstate
    if decision_row:
        if type(row.decided) is not str:
            raise ExportError(f"receipt {seq}: its decision time is not text")
        _stamp(row.decided, "decision time", seq)
    completed = row.decided if decision_row else created
    error_code: str | None = None

    decision, status, refusal, approval = "require-approval", "blocked", None, None
    if rstate == "allowed":
        if via == "grant":
            decision = "allow"
        elif via not in ("signature", "ruling"):
            raise ExportError(f"receipt {seq}: an allowed request with via {via!r}")
        if evidence is not None:
            status, completed = evidence
            if status == "failure":
                error_code = "tool-failed"
                counters["execution_failed"] += 1
                prov["execution.error_code"] = "inferred"
        else:
            error_code = "execution-unrecorded"        # permitted is all the queue knows, never that it ran
            counters["execution_unrecorded"] += 1
            prov.update({"execution.status": "synthesized", "execution.error_code": "synthesized"})
    if rstate in ("allowed", "denied") and via in ("signature", "ruling"):
        if type(row.signer) is not str or not row.signer:
            raise ExportError(f"receipt {seq}: a signed {rstate} request with no signer")
    if rstate == "allowed" and via in ("signature", "ruling"):
        if not decision_row:
            raise ExportError(f"receipt {seq}: a signed allow with no decision time")
        approved, from_answer = _approved_at(row.answer, row.decided)
        if not from_answer:
            counters["approved_at_fallback"] += 1
        approval = {"approver": {"id": f"ssh-key:{row.signer}", "role": "operator"}, "approved_at": approved,
                    "context": json.dumps({"queue": queue_id, "request_id": row.request_id, "via": via,
                                           "verify": "levain consent receipts verify"},
                                          sort_keys=True, separators=(",", ":"))}
        prov.update({"approval.approver.id": "inferred", "approval.approver.role": "synthesized",
                     "approval.approved_at": "inferred" if from_answer else "synthesized",
                     "approval.context": "inferred"})
    if rstate == "denied":
        if via not in ("signature", "ruling"):
            raise ExportError(f"receipt {seq}: a denied request with via {via!r}")
        refusal = ("deny", "operator-denied")
    elif rstate == "expired":
        refusal = ("expired", "approval-window-elapsed")
        if refused is not None and outcome is None:
            # an answer that verified under the key it names and was refused: the evidence of the refusal (spec
            # v0.2-alpha section 2.3 rule 3), never an approval of the action, which stays blocked
            if refused.reason_code == "approver-not-authorized":
                refusal = ("deny", "approver-not-authorized")
                counters["refused_not_authorized"] += 1
            else:
                counters["refused_window_elapsed"] += 1
            operator = refused.enrolled == 1 and refused.subreason in _OPERATOR_SUBREASONS
            approver: dict[str, str] = {"id": f"ssh-key:{refused.signer_fp}"}
            if operator:
                approver["role"] = "operator"
            approval = {"approver": approver, "approved_at": refused.signed_at,
                        "context": json.dumps({"queue": queue_id, "request_id": row.request_id,
                                               "via": "refused-answer", "refused": True, "kind": refused.kind,
                                               "enrolled": refused.enrolled == 1,
                                               "compromised": refused.subreason == "compromised",
                                               "subreason": refused.subreason, "levain_reason": refused.detail},
                                              sort_keys=True, separators=(",", ":"))}
            prov.update({"approval.approver.id": "inferred", "approval.approved_at": "inferred",
                         "approval.context": "inferred"})
            if operator:
                prov["approval.approver.role"] = "synthesized"
    elif rstate in ("undecided", "withdrawn"):
        refusal = ("require-approval", rstate)
    if rstate in ("withdrawn", "expired") and outcome is not None and outcome[0] == "did_not_run":
        refusal = None
        counters["dialog_did_not_run"] += 1
    elif rstate in ("withdrawn", "expired") and outcome is not None:
        # the agent's own dialog or standing permission ran the tool: the queue's state describes a request levain
        # did not decide, so the outcome row overrides it (a receipt of `blocked` for a tool that ran is a false record)
        kind, closed, event = outcome
        refusal = None
        status = _TOOL_STATUS[event]
        completed = closed
        if status == "failure":
            error_code = "tool-failed"
            counters["execution_failed"] += 1
            prov["execution.error_code"] = "inferred"
        if kind == "ran":
            _stamp(row.received, "received time", seq)
            approval = {"approver": {"id": "native-dialog:operator", "role": "operator"}, "approved_at": row.received,
                        "context": json.dumps({"queue": queue_id, "request_id": row.request_id, "via": "native-dialog",
                                               "signed": False}, sort_keys=True, separators=(",", ":"))}
            prov.update({"approval.approver.id": "inferred", "approval.approver.role": "synthesized",
                         "approval.approved_at": "synthesized", "approval.context": "inferred"})
            decision = "require-approval"
            counters["dialog_ran"] += 1
        else:
            name, version = f"{framework}.native-permissions", _UNKNOWN
            decision = "allow"
            prov["policy.name"] = "inferred"
            counters["dialog_ran_unasked"] += 1
    # the later of the row's stamp and the execution's time, so nothing completes after its receipt was issued
    if _stamp(completed, "execution time", seq) > _stamp(created, "stamp", seq):
        issued = completed
        prov["issued_at"] = "inferred"
    # ... and of the refused answer it records, kept after the hold lapsed (a late one), so the evidence a receipt
    # carries is never later than the receipt
    if approval is not None and refused is not None and parse_stamp(refused.refused_at) > _stamp(issued, "stamp", seq):
        issued = refused.refused_at
        prov["issued_at"] = "inferred"
    receipt: dict[str, Any] = {
        "version": AB_VERSION,
        "receipt_id": str(uuid.uuid5(LEVAIN_AB_NS, f"{queue_id}:{seq}")),
        "issued_at": issued,
        "actor": {"type": "agent", "id": f"agent:{row.agent}:session/{row.session}"},
        "agent": {"framework": framework, "framework_version": _UNKNOWN, "model": _UNKNOWN},
        "tool": {"name": row.tool, "capability": f"{framework}.{row.tool.lower()}"},
        "target": {"system": "levain-workspace", "environment": "dev", "resource_id": row.cwd},
        "arguments_hash": arguments_hash,
        "policy": {"name": name, "version": version, "decision": decision},
        "execution": {"status": status, "completed_at": completed, "result_ref": leaf},
    }
    if error_code is not None:
        receipt["execution"]["error_code"] = error_code
    if approval is not None:
        receipt["approval"] = approval
    if refusal is not None:
        receipt["refusal"] = {"verdict": refusal[0], "reason_code": refusal[1]}
        prov.update({"refusal.verdict": "inferred", "refusal.reason_code": "inferred"})
    return receipt, prov


def export(queue_dir: Path | str, *, now: datetime | None = None) -> Export:
    """The AgentBoundary receipts of the consent queue in ``queue_dir``, one per terminal receipt row in ``seq``
    order, chained by ``prior_receipt``, up to the first unsettled row (the settle rule in the module docstring).
    ``now`` is the export's clock (an aware datetime; default the current UTC time) and must not be ahead of the real
    clock by more than a few seconds, or the export is refused; the same queue and ``now`` yield the same receipts,
    and any later ``now`` yields them as a prefix. Raises :class:`ExportError` when the store is not a readable
    current-format queue or a row cannot be made into a receipt."""
    real = _utcnow()
    if now is None:
        now = real
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise ExportError("now must be an aware datetime")
    if now > real + _CLOCK_SLACK:
        raise ExportError("now is ahead of the real clock: an export clock in the future would settle rows early "
                          "and let a later record change a receipt already exported")
    queue_id, rows, kept = _read(Path(queue_dir))
    ledger = _Evidence(Path(queue_dir))
    counters = {"synthesized_arguments_hash": 0, "dialog_ran": 0, "dialog_ran_unasked": 0,
                "dialog_did_not_run": 0, "approved_at_fallback": 0, "execution_failed": 0,
                "execution_unrecorded": 0, "refused_not_authorized": 0, "refused_window_elapsed": 0}
    by_state: dict[str, int] = {}
    receipts: list[dict[str, Any]] = []
    pending = normalised = held = 0
    prior: dict[str, str] | None = None
    stopped = False
    for row in rows:
        problem = _row_problem(row)
        if problem is not None:
            raise ExportError(problem)
        if row.rstate == "pending":
            pending += 1
            continue
        if row.rstate not in _TERMINAL:
            raise ExportError(f"receipt {row.seq}: its state {row.rstate!r} is not a queue state")
        outcome = evidence = None
        if not stopped:
            created = _stamp(row.created, "stamp", row.seq)
            if row.rstate == "allowed":
                if row.decided is not None and row.qstate == "allowed":
                    lower = _stamp(row.decided, "decision time", row.seq)
                else:
                    lower = created
                evidence = ledger.match(row, lower, created + SETTLE)
                stopped = evidence is None and now - created < SETTLE
            elif row.rstate in ("withdrawn", "expired"):
                outcome = _counted_outcome(row)
                stopped = outcome is None and now - created < SETTLE
        if stopped:
            held += 1
            continue
        # an expired row with no counted outcome is exported only once it is SETTLE old (the stop above), so every
        # refused answer inside its window is already kept when it is read here
        refused = (_refused_evidence(row, kept.get(row.request_id, []))
                   if row.rstate == "expired" and outcome is None else None)
        receipt, prov = _map(queue_id, row, outcome, evidence, counters, refused)
        clean = _nfc(receipt)
        if clean != receipt:
            normalised += 1
            receipt = clean
        if prior is not None:
            receipt["prior_receipt"] = dict(prior)
            prov.update({"prior_receipt.receipt_id": "inferred", "prior_receipt.receipt_hash": "inferred"})
        receipt["provenance"] = dict(sorted(prov.items()))
        receipt["completeness_score"] = _score_milli(receipt, prov) / 1000.0
        receipt["receipt_hash"] = receipt_hash(receipt)
        prior = {"receipt_id": receipt["receipt_id"], "receipt_hash": receipt["receipt_hash"]}
        by_state[row.rstate] = by_state.get(row.rstate, 0) + 1
        receipts.append(receipt)
    return Export(receipts=receipts,
                  report=Report(receipts=len(receipts), by_state=dict(sorted(by_state.items())),
                                pending_skipped=pending, nfc_normalised=normalised, held_unsettled=held,
                                ledger_found=ledger.found, **counters))

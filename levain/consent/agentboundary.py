"""levain.consent.agentboundary — the consent queue's receipts read as AgentBoundary v0.2-alpha Action Receipts (Q19).

AgentBoundary (jamjet-labs/agentboundary) is a receipt format for what an agent was allowed to do: closed JSON,
RFC 8785 (JCS) plus SHA-256 for ``arguments_hash`` and ``receipt_hash``, a singly linked ``prior_receipt`` chain.
This module is a pure MAPPING of ``queue.db`` onto it, computed on every call from the rows, like
:mod:`levain.consent.receipt_log`: no second store, no schema change, no hash kept anywhere. The database is opened
read-only and never created, migrated or written.

  - one AgentBoundary receipt per TERMINAL receipt row (``allowed``, ``denied``, ``expired``, ``withdrawn``,
    ``undecided``), in ``seq`` order; ``pending`` rows stay in the Q11 log and are not exported. An undecided request
    that was ruled later has two rows and so two receipts. The map is one to one, and each receipt names its leaf in
    ``execution.result_ref`` (``levain-receipt:<queue_id>:<seq>``);
  - ``receipt_id`` is a UUIDv5 of ``<queue_id>:<seq>`` under a fixed namespace, and ``prior_receipt`` links each
    receipt to the one before it in the export, so a re-export reproduces the same ids and the same chain with
    nothing stored. The first receipt omits ``prior_receipt`` (genesis);
  - every receipt carries ``provenance`` (per path: ``observed`` read from a queue column, ``inferred`` derived,
    ``synthesized`` something the queue has no source for) and the ``completeness_score`` the spec's formula gives
    it; the score is part of ``receipt_hash``, so it is computed here by the same integer rule and never rounded;
  - ``receipt_hash`` is the sha256 of the JCS of the receipt without it, encoded by Levain's own canonical form
    (:func:`levain.cockpit.consent.encode`), the one ``tool_input_digest`` is already made with. That form is
    defined over integers and strings; the one number a receipt holds, ``completeness_score``, is a three-decimal
    fraction and is passed through it as its exact ES6 text (:class:`_Fraction`), never as a float.

What each queue state becomes (``policy.decision`` / ``execution.status`` / ``refusal``):
  allowed by a standing grant: allow / success / none · allowed by a signed answer or ruling: require-approval /
  success / none, with the ``approval`` block · denied: require-approval / blocked / deny ``operator-denied``, the
  approval block present (the spec's refusal rule 3) · expired: require-approval / blocked / expired
  ``approval-window-elapsed`` · undecided and withdrawn: require-approval / blocked / require-approval with the reason
  ``undecided`` or ``withdrawn``. A request left to the agent's own dialog that never ran (``did_not_run``) is
  blocked with no refusal.

Known edges (each stated, none built against):
  - NO signature and NO COSE envelope. The spec's envelope is Ed25519 only, while an operator key may be a security
    key or a passkey (P-256), and a key Levain held itself would be the mediator's signature Q11 refused. Authenticity
    stays Levain's own: the operator's signed answer and the Q11 head, checked by ``levain consent receipts verify``.
    A receipt here proves its content is intact against its hash and its neighbours, never who produced it;
  - ``agent.framework_version`` and ``agent.model`` are ``"unknown"`` (the hook event carries neither), and
    ``target.system`` and ``target.environment`` are fixed words; all four are ``synthesized``, which lowers the score;
  - ``arguments_hash`` is the stored ``tool_input_digest`` without its ``jcs:`` prefix. Levain's canonical form
    NFC-normalises strings and the spec's does not, so a verifier handed arguments holding a non-NFC string computes
    another hash than the one stored. A digest under the ``json:`` prefix (a tool input holding a float, outside JCS's
    domain) is not a JCS hash at all: it is emitted as stored, marked ``synthesized`` and counted in the report;
  - the receipt's own strings are NFC-normalised for the same reason (a decomposed working directory would otherwise
    hash differently under the two forms); the count is in the report. ``resource_id`` can then differ from the
    stored ``cwd`` in its byte form, never in its characters;
  - ``approval.approved_at`` and ``execution.completed_at`` are the same instant (the queue stamps one moment for a
    decision), so ``approved_at`` equals, and does not precede, ``completed_at``;
  - an unsigned request outcome of ``ran`` or ``ran_unasked`` (the agent's own dialog ran the tool) is not mapped to
    ``execution.status = success``: the schema then requires an ``approval`` block, and levain holds no approval for
    a call its own dialog answered. Those receipts keep the queue's state mapping and are counted in the report
    (``dialog_ran``), so the gap is visible, not smoothed;
  - ``actor.id`` carries the session id and ``resource_id`` the working directory because the closed schema has no
    field for either; a reader of an exported receipt learns both;
  - ``completed_at`` is the decision time or, for a state with no decision, the receipt's own stamp: AgentBoundary
    timestamps are the emitter's own, and so are these;
  - a re-export after more receipts were written yields the same prefix (ids, hashes and links), then the new ones.

Stdlib only.
"""
from __future__ import annotations

import json
import sqlite3
import unicodedata
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from levain.cockpit.consent import ConsentError, record_sha256
from levain.cockpit.consent import encode as jcs_encode

__all__ = ["AB_VERSION", "Export", "ExportError", "Report", "export", "policy_identity", "receipt_hash"]

AB_VERSION = "agentboundary/v0.2-alpha"
# fixed, so a re-export reproduces every receipt_id; it is uuid5(NAMESPACE_URL, _NS_NAME), written out so an edit to
# the name cannot change the ids; a test pins the pair
LEVAIN_AB_NS = uuid.UUID("61dad17c-6f1f-5c59-988c-3035a88ad44e")
_NS_NAME = "https://levain.dev/consent/agentboundary-export/v0.2-alpha"

_TERMINAL = frozenset({"allowed", "denied", "expired", "withdrawn", "undecided"})
_UNKNOWN = "unknown"
_FRAMEWORKS = {"claude_code": "claude-code", "codex": "codex"}
_WEIGHTS = {"observed": 2, "inferred": 1, "synthesized": 0}

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
        "q.tool_input_digest, q.cwd, q.tier, q.state, q.decided_at, q.signer_fp, q.via, {outcome} "
        "FROM receipts r LEFT JOIN requests q ON q.id = r.request_id {join} ORDER BY r.seq")
_REQUEST_TEXT = (4, 5, 6, 7, 8, 9, 10, 11)     # the request columns every exported row must hold as text


class ExportError(ValueError):
    """The queue cannot be exported: not a readable current-format store, or a row no receipt can be made of."""


@dataclass(frozen=True)
class Report:
    """What an export did. ``by_state``: exported receipts per receipt-row state. ``pending_skipped``: ``pending``
    rows not exported. ``synthesized_arguments_hash``: receipts whose ``arguments_hash`` is a ``json:`` digest, not a
    JCS hash (the spec's verifier given the arguments would flag them). ``dialog_ran``: exported receipts of
    requests whose agent dialog ran the tool, kept on the queue's state mapping (see the module's known edges).
    ``dialog_did_not_run``: receipts mapped ``blocked`` with no refusal for that outcome. ``nfc_normalised``:
    receipts whose own strings were changed by NFC."""
    receipts: int
    by_state: Mapping[str, int]
    pending_skipped: int
    synthesized_arguments_hash: int
    dialog_ran: int
    dialog_did_not_run: int
    nfc_normalised: int


@dataclass(frozen=True)
class Export:
    receipts: list[dict[str, Any]]
    report: Report

    def policy_store(self) -> set[tuple[str, str]]:
        """The ``(policy.name, policy.version)`` pairs the receipts name: the verifier's policy store for them."""
        return {(r["policy"]["name"], r["policy"]["version"]) for r in self.receipts}


class _Fraction(int):
    """``completeness_score`` in thousandths, for Levain's canonical form, which has no float. The form writes an
    integer with ``str()``, so this prints the exact ES6 number text of ``milli / 1000`` instead (``1``, ``0.8``,
    ``0.005``): the three-decimal fractions the spec's integer formula yields all have a short exact form."""

    def __str__(self) -> str:
        milli = int(self)
        whole, frac = divmod(milli, 1000)
        return str(whole) if not frac else f"{whole}.{frac:03d}".rstrip("0")


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
    Raises :class:`ExportError` for a value that form cannot encode (a lone surrogate, an integer past 2**53 - 1)."""
    body: dict[str, Any] = {k: v for k, v in receipt.items() if k != "receipt_hash"}
    score = body.get("completeness_score")
    if score is not None:
        body["completeness_score"] = _Fraction(round(score * 1000))
    try:
        return record_sha256(jcs_encode(body))
    except ConsentError as exc:
        raise ExportError(f"the receipt cannot be canonically encoded: {exc}") from None


def policy_identity(tier: str, levain_version: str) -> tuple[str, str]:
    """The ``(policy.name, policy.version)`` a receipt of a request in ``tier`` names."""
    return f"levain.consent.{tier}", levain_version


def _levain_version() -> str:
    from levain import __version__
    return __version__


def _open(queue_dir: Path) -> sqlite3.Connection:
    from levain.consent.queue import DB_NAME
    path = Path(queue_dir) / DB_NAME
    try:
        return sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=30.0, isolation_level=None)
    except sqlite3.Error as exc:
        raise ExportError(f"{path} cannot be opened read-only: {exc}") from None


def _read(queue_dir: Path) -> tuple[str, list[tuple[Any, ...]]]:
    """``(queue_id, rows)`` in one read snapshot. Raises :class:`ExportError` when the store is not a readable
    current-format consent queue."""
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
                sql = _SQL.format(outcome="d.outcome", join="LEFT JOIN dialog_outcomes d ON d.request_id = q.id")
            else:
                sql = _SQL.format(outcome="NULL", join="")
            rows = conn.execute(sql).fetchall()
        except sqlite3.Error as exc:
            raise ExportError(f"the store cannot be read: {exc}") from None
        finally:
            conn.execute("COMMIT")
    finally:
        conn.close()
    return meta["queue_id"], rows


def _row_problem(row: tuple[Any, ...]) -> str | None:
    seq = row[0]
    if type(seq) is not int:
        return f"a receipt's seq is not an integer: {seq!r}"
    if type(row[1]) is not str or type(row[2]) is not str or type(row[3]) is not str:
        return f"receipt {seq}: its request_id, state or created_at is not text"
    if row[4] is None:
        return f"receipt {seq}: it names a request the queue does not hold"
    if any(type(row[i]) is not str for i in _REQUEST_TEXT):
        return f"receipt {seq}: its request has a column that is not text"
    return None


def _map(queue_id: str, row: tuple[Any, ...], levain_version: str, counters: dict[str, int]
         ) -> tuple[dict[str, Any], dict[str, str]]:
    """The receipt (without ``prior_receipt``, ``provenance``, score and hash) and its provenance entries."""
    (seq, _rid, rstate, created, _qid, agent, session, tool, digest, cwd, tier, qstate, decided, signer, via,
     outcome) = row
    framework = _FRAMEWORKS.get(agent, agent.replace("_", "-"))
    prov: dict[str, str] = {
        "receipt_id": "inferred", "issued_at": "observed", "actor.type": "inferred", "actor.id": "observed",
        "agent.framework": "observed", "agent.framework_version": "synthesized", "agent.model": "synthesized",
        "tool.name": "observed", "tool.capability": "inferred", "target.system": "synthesized",
        "target.environment": "synthesized", "target.resource_id": "observed", "policy.name": "observed",
        "policy.version": "inferred", "policy.decision": "inferred", "execution.result_ref": "inferred"}
    if digest.startswith("jcs:") and len(digest) == 68:
        arguments_hash = digest[4:]
        prov["arguments_hash"] = "observed"
    elif digest.startswith("json:") and len(digest) == 69:
        arguments_hash = digest[5:]
        prov["arguments_hash"] = "synthesized"
        counters["synthesized_arguments_hash"] += 1
    else:
        raise ExportError(f"receipt {seq}: its tool input digest has no known scheme")
    name, version = policy_identity(tier, levain_version)
    leaf = f"levain-receipt:{queue_id}:{seq}"
    # the request's own decision time belongs to the row that IS its final state; any other row is stamped by itself
    decision_row = decided is not None and rstate == qstate
    completed = decided if decision_row else created
    prov["execution.completed_at"] = "inferred" if decision_row else "observed"

    decision, status, refusal, approval = "require-approval", "blocked", None, None
    if rstate == "allowed":
        if via == "grant":
            decision, status = "allow", "success"
        elif via in ("signature", "ruling"):
            status = "success"
        else:
            raise ExportError(f"receipt {seq}: an allowed request with via {via!r}")
        prov["execution.status"] = "inferred"
    else:
        prov["execution.status"] = "inferred"
    if rstate in ("allowed", "denied") and via in ("signature", "ruling"):
        if type(signer) is not str or not signer:
            raise ExportError(f"receipt {seq}: a signed {rstate} request with no signer")
        approval = {"approver": {"id": f"ssh-key:{signer}", "role": "operator"}, "approved_at": completed,
                    "context": json.dumps({"queue": queue_id, "request_id": row[1], "via": via,
                                           "verify": "levain consent receipts verify"},
                                          sort_keys=True, separators=(",", ":"))}
        prov.update({"approval.approver.id": "observed", "approval.approver.role": "synthesized",
                     "approval.approved_at": "observed" if decision_row else "inferred",
                     "approval.context": "inferred"})
    if rstate == "denied":
        if via not in ("signature", "ruling"):
            raise ExportError(f"receipt {seq}: a denied request with via {via!r}")
        refusal = ("deny", "operator-denied")
    elif rstate == "expired":
        refusal = ("expired", "approval-window-elapsed")
    elif rstate in ("undecided", "withdrawn"):
        refusal = ("require-approval", rstate)
    if rstate in ("withdrawn", "expired") and outcome == "did_not_run":
        refusal = None
        prov["execution.status"] = "observed"
        counters["dialog_did_not_run"] += 1
    elif rstate in ("withdrawn", "expired") and outcome in ("ran", "ran_unasked"):
        counters["dialog_ran"] += 1
    receipt: dict[str, Any] = {
        "version": AB_VERSION,
        "receipt_id": str(uuid.uuid5(LEVAIN_AB_NS, f"{queue_id}:{seq}")),
        "issued_at": created,
        "actor": {"type": "agent", "id": f"agent:{agent}:session/{session}"},
        "agent": {"framework": framework, "framework_version": _UNKNOWN, "model": _UNKNOWN},
        "tool": {"name": tool, "capability": f"{framework}.{tool.lower()}"},
        "target": {"system": "levain-workspace", "environment": "dev", "resource_id": cwd},
        "arguments_hash": arguments_hash,
        "policy": {"name": name, "version": version, "decision": decision},
        "execution": {"status": status, "completed_at": completed, "result_ref": leaf},
    }
    if approval is not None:
        receipt["approval"] = approval
    if refusal is not None:
        receipt["refusal"] = {"verdict": refusal[0], "reason_code": refusal[1]}
        prov.update({"refusal.verdict": "inferred", "refusal.reason_code": "inferred"})
    return receipt, prov


def export(queue_dir: Path | str, *, levain_version: str | None = None) -> Export:
    """The AgentBoundary receipts of the consent queue in ``queue_dir``, one per terminal receipt row in ``seq``
    order, chained by ``prior_receipt``. ``levain_version`` is the ``policy.version`` the receipts name (default:
    the running levain's). The same queue yields the same receipts every time. Raises :class:`ExportError` when the
    store is not a readable current-format queue or a row cannot be made into a receipt."""
    version = levain_version or _levain_version()
    if not isinstance(version, str) or not version:
        raise ExportError("levain_version must be a non-empty string")
    queue_id, rows = _read(Path(queue_dir))
    counters = {"synthesized_arguments_hash": 0, "dialog_ran": 0, "dialog_did_not_run": 0}
    by_state: dict[str, int] = {}
    receipts: list[dict[str, Any]] = []
    pending = normalised = 0
    prior: dict[str, str] | None = None
    for row in rows:
        problem = _row_problem(row)
        if problem is not None:
            raise ExportError(problem)
        if row[2] == "pending":
            pending += 1
            continue
        if row[2] not in _TERMINAL:
            raise ExportError(f"receipt {row[0]}: its state {row[2]!r} is not a queue state")
        receipt, prov = _map(queue_id, row, version, counters)
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
        by_state[row[2]] = by_state.get(row[2], 0) + 1
        receipts.append(receipt)
    return Export(receipts=receipts,
                  report=Report(receipts=len(receipts), by_state=dict(sorted(by_state.items())),
                                pending_skipped=pending, nfc_normalised=normalised, **counters))

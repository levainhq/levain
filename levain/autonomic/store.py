"""levain.autonomic.store — the append-only gate-receipt trace (the efferent canonical buffer).

The efferent sibling of the afferent ``ProposalStore``: the gate WRITES one record per terminal
decision (a fire or a deny); surfaces (Bridge/FleetView) PROJECT it read-only (the receipt corpus
rendered as constraint-exposing topology — surface ``gate=denied`` + dissent, not a firehose). A
DEFERRED confirm writes NOTHING — a receipt records a DECISION, and "no decision yet" is not one
(the afferent proposal already carries its null-gate slice; re-emitting on defer would be noise).

Separate file from the afferent proposals (semantically distinct: afferent input buffer vs efferent
decision trace; linked by ``proposal_id``/``context_id``, not co-location). "One canonical object"
is the receipt SCHEMA (the shared shape), not one physical file. Append-only JSONL, single-writer,
stdlib-only — same epistemic shape + the same line-atomicity caveats as the afferent store (a record
can exceed ``PIPE_BUF``; concurrent appenders are NOT supported — add an ``flock`` if that ever
becomes real). A corrupt line is skipped LOUDLY on read; a missing file reads ``[]``.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from levain.autonomic.posture import Posture

__all__ = ["GateReceiptStore", "StoredGateReceipt"]

_log = logging.getLogger("levain.autonomic.store")


@dataclass(frozen=True)
class StoredGateReceipt:
    """A gate decision as persisted: the record id + the decision metadata + the FILLED receipt
    action-face slice."""

    id: str
    created_at: str
    action_name: str
    proposal_id: str | None
    posture: str          # the resolved Posture.name (string for forward-compat on read)
    fired: bool
    action_face: dict[str, Any]


def _receipt_id(
    *, created_at: str, context_id: str, action_name: str, fired: bool, action_face: dict[str, Any]
) -> str:
    """A stable, sortable, CONTENT-ADDRESSED id: ``<created_at>-<8hex>`` over the decision's identity
    (context + action + the gate verdict + ``fired``). Deterministic in inputs (testable); the
    ``created_at`` prefix keeps the log chronologically sortable.

    NOTE — ids are DETERMINISTIC, the store does NOT dedup (L1-MED-1 / L2-M3). The append store is
    append-only and never collapses records; two appends with the same id write two JSONL lines. A
    consumer should key by ``id`` + treat a repeat as the same decision (not assume global
    uniqueness). ``fired`` is in the basis so a retry that FAILED then SUCCEEDED at the same
    ``created_at`` gets a DISTINCT id rather than colliding (the production clock is wall-now, so
    real collisions are already near-impossible; this hardens the fixed-clock/retry edge)."""
    gate = action_face.get("gate") or {}
    basis = (
        f"{created_at}\x00{context_id}\x00{action_name}\x00"
        f"{gate.get('verdict', '')}\x00{gate.get('by', '')}\x00{int(fired)}"
    )
    h = hashlib.sha256(basis.encode()).hexdigest()[:8]
    return f"{created_at}-{h}"


class GateReceiptStore:
    """Append-only JSONL store of gate-receipt records. ``append`` writes one record + returns its
    id; ``read`` returns records NEWEST-FIRST (the surface wants the latest decision on top)."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def append(
        self,
        *,
        created_at: str,
        action_name: str,
        proposal_id: str | None,
        posture: Posture,
        fired: bool,
        action_face: dict[str, Any],
    ) -> str:
        """Append a gate-receipt record; returns its id. Creates the parent dir + file on first
        write. The write is serialized by an ``flock`` (L1-LOW-8): concurrent appenders — a CLI
        resolve racing the daemon sweep — can otherwise interleave/duplicate audit lines. The lock is
        held only across the single ``write`` (the JSON is built first); a crash can still truncate
        the LAST line (skipped on read)."""
        context_id = str(action_face.get("context_id", ""))
        rid = _receipt_id(
            created_at=created_at, context_id=context_id, action_name=action_name,
            fired=fired, action_face=action_face,
        )
        record = {
            "id": rid,
            "kind": "gate",
            "created_at": created_at,
            "action_name": action_name,
            "proposal_id": proposal_id,
            "posture": posture.name,
            "fired": fired,
            "action_face": action_face,
        }
        line = json.dumps(record, ensure_ascii=False) + "\n"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as fh:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            try:
                fh.write(line)
            finally:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        return rid

    def read(self, *, limit: int | None = None) -> list[StoredGateReceipt]:
        """Read gate records NEWEST-FIRST. A blank/malformed line is skipped with a WARNING (one bad
        historical line never breaks the surface — fail-soft); a non-``gate`` line is skipped
        silently (forward-compat if the file is ever co-mingled). A missing file returns ``[]`` via a
        ``FileNotFoundError`` CATCH (the read/exists race is not atomic). ``errors="replace"`` so a
        crash mid-multibyte char (append writes ``ensure_ascii=False``) can't make the WHOLE store
        unreadable before line-level JSON skipping (codex L3 MED) — the torn line's JSON parse then
        fails + is skipped. Any other ``OSError`` (permission / IsADirectory / transient IO) degrades
        to ``[]`` with a log, never an unhandled raise into the surface (complement L3 LOW-2)."""
        try:
            raw = self.path.read_text(encoding="utf-8", errors="replace")
        except FileNotFoundError:
            return []
        except OSError as e:
            _log.warning("efferent store: read failed (%s): %s — returning []", type(e).__name__, e)
            return []
        out: list[StoredGateReceipt] = []
        for lineno, line in enumerate(raw.splitlines(), start=1):
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
                if not isinstance(rec, dict):
                    _log.warning("efferent store: skipping non-object line %d (%s)", lineno, type(rec).__name__)
                    continue
                if rec.get("kind") != "gate":
                    continue
                out.append(
                    StoredGateReceipt(
                        id=rec["id"],
                        created_at=rec["created_at"],
                        action_name=rec["action_name"],
                        proposal_id=rec.get("proposal_id"),
                        posture=rec["posture"],
                        fired=rec["fired"],
                        action_face=rec["action_face"],
                    )
                )
            except (json.JSONDecodeError, KeyError, TypeError) as e:
                _log.warning("efferent store: skipping malformed line %d (%s)", lineno, type(e).__name__)
                continue
        out.reverse()  # newest-first
        return out[:limit] if limit is not None else out

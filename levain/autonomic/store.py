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
import os
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from levain.autonomic.journal import durable_fsync
from levain.autonomic.posture import Posture

__all__ = ["GateReceiptStore", "StoredGateReceipt", "gate_record"]

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
    *, created_at: str, context_id: str, action_name: str, fired: bool, action_face: dict[str, Any],
    subject: str | None = None,
) -> str:
    """A stable, sortable, CONTENT-ADDRESSED id: ``<created_at>-<8hex>`` over the decision's identity
    (context + action + the gate verdict + ``fired``). Deterministic in inputs (testable); the
    ``created_at`` prefix keeps the log chronologically sortable.

    NOTE — ids are DETERMINISTIC, and ``append`` does NOT dedup (L1-MED-1 / L2-M3): two appends with the
    same id write two JSONL lines. ``read`` returns the first line of an id; a consumer reading the file
    itself should key by ``id`` + treat a repeat as the same decision (not assume global uniqueness). ``fired`` is in the basis so a retry that FAILED then SUCCEEDED at the same
    ``created_at`` gets a DISTINCT id rather than colliding (the production clock is wall-now, so
    real collisions are already near-impossible; this hardens the fixed-clock/retry edge)."""
    gate = action_face.get("gate") or {}
    basis = (
        f"{created_at}\x00{context_id}\x00{action_name}\x00"
        f"{gate.get('verdict', '')}\x00{gate.get('by', '')}\x00{int(fired)}"
    )
    if subject is not None:
        # autogrant r7.0: a receipt of a subject (a /3 store) has the subject in its basis, so two subjects never
        # share an id; and its terminal_state (R15-5 S2): the ends of one attempt are built together at admission, at
        # one created_at with one verdict, and outcome_unknown and refused must not share one
        basis = f"{subject}\x00{action_face.get('terminal_state', '')}\x00{basis}"
    h = hashlib.sha256(basis.encode()).hexdigest()[:8]
    return f"{created_at}-{h}"


def gate_record(*, created_at: str, action_name: str, proposal_id: str | None, posture: Posture, fired: bool,
                action_face: dict[str, Any], subject: str | None = None) -> tuple[str, dict[str, Any]]:
    """The receipt id and record, built before any write: the one builder of the record shape. ``subject`` names
    the subject a ``levain-autonomic/3`` receipt closes (``run:`` / ``effect:`` / ``pending:`` / ``request:``): the
    record's envelope then carries it (the store's ``receipts_content`` trigger checks it against the row) and the
    id's basis includes it. Without it the record is exactly :meth:`GateReceiptStore.append`'s."""
    context_id = str(action_face.get("context_id", ""))
    rid = _receipt_id(created_at=created_at, context_id=context_id, action_name=action_name, fired=fired,
                      action_face=action_face, subject=subject)
    record: dict[str, Any] = {
        "id": rid,
        "kind": "gate",
        "created_at": created_at,
        "action_name": action_name,
        "proposal_id": proposal_id,
        "posture": posture.name,
        "fired": fired,
        "action_face": action_face,
    }
    if subject is not None:
        record["subject"] = subject
    return rid, record


_GATE_KEYS = ("id", "created_at", "action_name", "posture", "fired", "action_face")


def _records_in(raw: bytes) -> dict[str, list[dict[str, Any]]]:
    # every JSON object line in the file's bytes, by its id; a line that does not decode or parse (a torn tail), or
    # names no string id, is skipped, never raised
    out: dict[str, list[dict[str, Any]]] = {}
    for piece in raw.split(b"\n"):
        try:
            rec = json.loads(piece.decode("utf-8", errors="replace"))
        except ValueError:
            continue
        if isinstance(rec, dict) and isinstance(rec.get("id"), str):
            out.setdefault(rec["id"], []).append(rec)
    return out


def _canon(rec: dict[str, Any]) -> str:
    # the serialized form, so that 0 and false, 1 and 1.0 are different records (Python's == says they are equal)
    return json.dumps(rec, sort_keys=True, ensure_ascii=False)


def _is_gate(rec: dict[str, Any]) -> bool:
    # a line :meth:`GateReceiptStore.read` returns (kind gate, every field it reads)
    return rec.get("kind") == "gate" and all(k in rec for k in _GATE_KEYS)


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
        rid, record = gate_record(created_at=created_at, action_name=action_name, proposal_id=proposal_id,
                                  posture=posture, fired=fired, action_face=action_face)
        line = json.dumps(record, ensure_ascii=False) + "\n"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as fh:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            try:
                fh.write(line)
            finally:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        return rid

    def project_rows(self, rows: Sequence[tuple[str, str]]) -> list[str]:
        """Project committed receipt rows (``(receipt_id, record text)``) into the file: append each one the file
        does not hold, then make the file and its directory entry durable (autogrant r66.5). Returns the id of every
        row the file now holds, in row order; the caller marks a row projected only after this returns, so a stop or
        power loss before the flush leaves the mark unset and the next projection appends again what did not
        survive. A row counts as held only when a line equal to its record is in the file: a line that is no
        complete gate record (foreign, partial) is passed over and the row appended; a complete gate record under
        the row's id with other content keeps the row out of the return, so it is never marked. The file is read as
        bytes and each line decoded on its own, so a torn tail (a crash mid-line, mid-character) is skipped and ended
        with a newline, never raised."""
        made: list[Path] = []     # the directories this call creates, leaf first
        anc = self.path.parent
        while not anc.exists() and anc != anc.parent:
            made.append(anc)
            anc = anc.parent
        self.path.parent.mkdir(parents=True, exist_ok=True)
        done: list[str] = []
        with self.path.open("a+b") as fh:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            try:
                fh.seek(0)
                raw = fh.read()
                have = _records_in(raw)
                fh.seek(0, os.SEEK_END)
                if raw and not raw.endswith(b"\n"):
                    fh.write(b"\n")
                for rid, text in rows:
                    want = json.loads(text)
                    lines = [r for r in have.get(rid, ()) if _is_gate(r)]
                    if any(_canon(r) != _canon(want) for r in lines):
                        # a complete gate record under this id that is not this receipt: ``read`` could return it
                        # for this id, so the row is never marked projected (doctor reads it unprojected)
                        _log.error("efferent store: %s holds another record under receipt id %s: not projected",
                                   self.path, rid)
                        continue
                    if not lines:
                        fh.write(text.encode("utf-8") + b"\n")
                        have.setdefault(rid, []).append(want)
                    done.append(rid)
                fh.flush()
                durable_fsync(fh.fileno())
            finally:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        # the file's directory entry, on every projection: whoever created the file (an ``append``, a projection
        # stopped before this line) may not have flushed it, and the file existing says nothing about that
        # and the entry of each directory this call created, in ITS parent, up to the first that already existed
        for d in (self.path.parent, *made[1:], anc if made else None):
            if d is None:
                continue
            dfd = os.open(d, os.O_RDONLY)
            try:
                durable_fsync(dfd)
            finally:
                os.close(dfd)
        return done

    def read(self, *, limit: int | None = None) -> list[StoredGateReceipt]:
        """Read gate records NEWEST-FIRST, by ARRIVAL: file order, so a receipt projected after a stop reads as newer
        than receipts written after it. A record id repeated in the file (a projection repeated after a stop) is
        read once, at its first line. A blank/malformed line is skipped with a WARNING (one bad
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
        seen: set[str] = set()
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
                if rec.get("id") in seen:
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
                seen.add(rec["id"])
            except (json.JSONDecodeError, KeyError, TypeError) as e:
                _log.warning("efferent store: skipping malformed line %d (%s)", lineno, type(e).__name__)
                continue
        out.reverse()  # newest-first
        return out[:limit] if limit is not None else out

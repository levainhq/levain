"""levain.autonomic.pending — the persisted pending-action queue (the confirm-map's durable record).

A confirm-class posture (cooling-off / confirm / confirm-elevated) does NOT fire at the gate; it
PROPOSES (surfaces via the :class:`~levain.autonomic.transport.ConfirmTransport`) and persists a
:class:`PendingAction` here. The operator's later approve/deny — or a timeout sweep — re-enters the
gate via ``EfferentGate.resolve(pending_id, ...)``, which loads the pending, fires-or-drops, writes
the gate-receipt, and REMOVES it. So this store is the durable record that survives the propose→reply
gap (the operator may reply minutes/hours later, or never).

This is the inversion of the proven 2b directive-undo map, mirroring flow's ``pending_responses.json``
(the established deferred-action queue): a small, MUTABLE queue (items are removed on resolution), so
a JSON-LIST store with atomic tmp+replace writes + an ``flock`` is the right shape — NOT the
append-only JSONL the receipt/proposal stores use (those never remove). The flock serializes the
read-modify-write across the concurrent touch points (the gate's ``add``, the CLI's ``resolve``, the
daemon's timeout ``sweep``). A malformed historical record is skipped LOUDLY on read (fail-soft); a
missing file reads ``[]``.

Stdlib-only (``fcntl`` is POSIX — macOS/Linux, the stack's targets).
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import logging
import os
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from levain.autonomic.journal import durable_replace

__all__ = ["PendingAction", "PendingActionStore", "seal_pending_id"]

_log = logging.getLogger("levain.autonomic.pending")


def seal_pending_id(
    *, created_at: str, context_id: str, action_name: str, payload: str, proposal_id: str | None,
    posture: str, fail_open: bool, requires_typed: bool, expires_at: str | None,
    authority: dict[str, Any], query_text: str, query_date: str, producers: tuple[str, ...],
) -> str:
    """The content-FINGERPRINT pending id: ``pend-<created_at>-<16hex>`` over EVERY governance-relevant
    field of a pending record (L3 codex HIGH-1/2 + complement MED-1 — the cross-substrate consensus).
    The id therefore doubles as an integrity SEAL: recomputing it from a record and comparing
    (:meth:`PendingAction.seal_matches`) detects ANY post-propose alteration — of the payload, action,
    posture, the fail-open/typed knobs, the expiry, the authority, or the provenance. Catches accidental
    corruption, schema drift, and a buggy writer (a future binding / adopter); and — the basis includes
    payload + proposal_id — fixes the MED-6 collision (two distinct actions at the same clock get
    distinct ids; only a genuine re-propose of the identical record shares one).

    KEYLESS, so it does NOT defend against a MALICIOUS local process that can ALSO recompute it (full
    local compromise, out of scope). The §1.5 re-screen + manifest re-check in the gate's resolve guard
    are the independent second layer; the gate's auto-fire ALLOWLIST closes the autonomous (cooling-off
    auto-fire) half (a code-side gate-origin control a JSON writer can't mint); a KEYED HMAC is the
    boundary-crossing hardening for the confirm-class residual IF the store ever leaves the trusted laptop.

    The basis is STRUCTURALLY-ENCODED canonical JSON (L3 codex ship-gate HIGH-2), NOT a delimiter-join:
    a delimiter-join lets a field carrying the delimiter byte (``\\x00`` in a string, ``\\x01`` in a
    producer) reshuffle into an equal hash input for a DIFFERENT field tuple — a verified collision that
    would let ``seal_matches`` accept a mutated record. Canonical JSON encodes field boundaries + types
    unambiguously, closing that class."""
    body = {
        "created_at": created_at, "context_id": context_id, "action_name": action_name,
        "payload": payload, "proposal_id": proposal_id, "posture": posture,
        "fail_open": bool(fail_open), "requires_typed": bool(requires_typed),
        "expires_at": expires_at, "authority": authority,
        "query_text": query_text, "query_date": query_date, "producers": list(producers),
    }
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    h = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
    return f"pend-{created_at}-{h}"


def _strict_bool(d: dict[str, Any], key: str) -> bool:
    """A stored governance bool must be a REAL JSON bool — not a truthy string. ``bool("false")`` is
    truthy, so coercing an editable/drifted record's ``"false"`` would flip a fail-closed flag open
    (L1-HIGH-3). Reject anything but ``True``/``False`` (the read path skips the record)."""
    v = d[key]
    if not isinstance(v, bool):
        raise TypeError(f"{key} must be a JSON bool, got {type(v).__name__}: {v!r}")
    return v


def _authority_dict(v: Any) -> dict[str, Any]:
    """``authority`` must be a JSON object — reject anything else with ``TypeError`` (the read path
    skips the record). Guards the ``dict(...)`` ValueError on a non-mapping (L3 codex LOW)."""
    if not isinstance(v, dict):
        raise TypeError(f"authority must be a dict, got {type(v).__name__}")
    return dict(v)


def _producer_tuple(v: Any) -> tuple[str, ...]:
    """``producers`` must be a list of STRINGS (L3 codex ship-gate MED): a non-list, or a non-string
    element (``[123]``), is rejected with ``TypeError`` (the read path skips the record) rather than
    flowing into the record where the seal/render would later raise on a malformed-but-loaded value."""
    if not isinstance(v, (list, tuple)):
        raise TypeError(f"producers must be a list, got {type(v).__name__}")
    if not all(isinstance(x, str) for x in v):
        raise TypeError("producers must all be strings")
    return tuple(v)


@dataclass(frozen=True)
class PendingAction:
    """One proposed efferent action awaiting the operator's collapse. Carries EVERYTHING needed to
    fire the action + write its receipt at resolve time — the gate that resolves it may be a fresh
    process (the daemon sweep, a CLI ``resolve``), so nothing may rely on in-memory state.

    Action fields (to fire): ``action_name`` + ``payload`` + ``context_id`` (the narrow Executor
    seam). Receipt fields: ``context_id`` / ``query_text`` / ``query_date`` (the replayable bucket
    axis) / ``producers`` (the afferent lineage) / ``authority`` (the grant in force) / ``proposal_id``
    (links back to the afferent proposal). Governance fields: ``posture`` (the resolved rung name) +
    ``fail_open`` + ``requires_typed`` (the two knobs, frozen at propose time so the sweep resolves
    by the posture the human SAW). ``expires_at`` (None ⇒ wait indefinitely) is when the silence
    default fires."""

    pending_id: str
    created_at: str
    action_name: str
    payload: str
    context_id: str
    query_text: str
    query_date: str
    posture: str
    fail_open: bool
    requires_typed: bool
    authority: dict[str, Any]
    producers: tuple[str, ...] = ()
    proposal_id: str | None = None
    expires_at: str | None = None

    @classmethod
    def create(
        cls, *, created_at: str, action_name: str, payload: str, context_id: str, query_text: str,
        query_date: str, posture: str, fail_open: bool, requires_typed: bool, authority: dict[str, Any],
        producers: tuple[str, ...] = (), proposal_id: str | None = None, expires_at: str | None = None,
    ) -> "PendingAction":
        """Build a SEALED pending action — the ``pending_id`` is the content fingerprint over all the
        governance fields (:func:`seal_pending_id`), so any later alteration is detectable via
        :meth:`seal_matches`. The ONLY way to mint a valid pending: production + tests both go through
        here, so the sealed-invariant can't drift."""
        producers = tuple(producers)
        pid = seal_pending_id(
            created_at=created_at, context_id=context_id, action_name=action_name, payload=payload,
            proposal_id=proposal_id, posture=posture, fail_open=fail_open, requires_typed=requires_typed,
            expires_at=expires_at, authority=authority, query_text=query_text, query_date=query_date,
            producers=producers,
        )
        return cls(
            pending_id=pid, created_at=created_at, action_name=action_name, payload=payload,
            context_id=context_id, query_text=query_text, query_date=query_date, posture=posture,
            fail_open=fail_open, requires_typed=requires_typed, authority=authority,
            producers=producers, proposal_id=proposal_id, expires_at=expires_at,
        )

    def seal_matches(self) -> bool:
        """True iff this record is UNALTERED since :meth:`create` — recompute the content fingerprint
        from the current fields and compare to the stored ``pending_id``. A mismatch ⇒ tampered /
        corrupted / drifted ⇒ the gate refuses/drops (never fires)."""
        return self.pending_id == seal_pending_id(
            created_at=self.created_at, context_id=self.context_id, action_name=self.action_name,
            payload=self.payload, proposal_id=self.proposal_id, posture=self.posture,
            fail_open=self.fail_open, requires_typed=self.requires_typed, expires_at=self.expires_at,
            authority=self.authority, query_text=self.query_text, query_date=self.query_date,
            producers=self.producers,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "pending_id": self.pending_id,
            "created_at": self.created_at,
            "action_name": self.action_name,
            "payload": self.payload,
            "context_id": self.context_id,
            "query_text": self.query_text,
            "query_date": self.query_date,
            "posture": self.posture,
            "fail_open": self.fail_open,
            "requires_typed": self.requires_typed,
            "authority": dict(self.authority),
            "producers": list(self.producers),
            "proposal_id": self.proposal_id,
            "expires_at": self.expires_at,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "PendingAction":
        """Reconstruct from a stored record. Raises ``KeyError``/``TypeError``/``ValueError`` on a
        malformed record (the store's read CATCHES all three + skips it loudly — one bad line never
        breaks the queue; L3 codex LOW: a non-dict ``authority`` made ``dict(...)`` raise ``ValueError``,
        which the read sites didn't catch, blocking the whole sweep).

        ``fail_open`` / ``requires_typed`` are parsed STRICTLY (L1-HIGH-3): the pending file is
        editable, untrusted durable input, and ``bool("false")`` is truthy — a coerced bool would
        let a hand-edited/schema-drifted ``"false"`` flip a fail-CLOSED confirm into a fail-OPEN
        auto-fire. A non-bool raises here → the record is skipped on read (the safe direction: a
        record whose silence-default can't be trusted is dropped, never auto-fired)."""
        return cls(
            pending_id=d["pending_id"],
            created_at=d["created_at"],
            action_name=d["action_name"],
            payload=d["payload"],
            context_id=d["context_id"],
            query_text=d["query_text"],
            query_date=d["query_date"],
            posture=d["posture"],
            fail_open=_strict_bool(d, "fail_open"),
            requires_typed=_strict_bool(d, "requires_typed"),
            authority=_authority_dict(d["authority"]),
            producers=_producer_tuple(d.get("producers") or ()),
            proposal_id=d.get("proposal_id"),
            expires_at=d.get("expires_at"),
        )


class PendingActionStore:
    """A mutable JSON-list store of pending confirm-class actions, ``flock``-serialized + atomically
    written. ``add`` appends; ``get``/``list_open`` read; ``remove`` deletes one (on resolve). Every
    mutation is a locked read-modify-write so a concurrent ``add`` (gate) / ``remove`` (resolve) /
    ``sweep`` can't lose a record or tear the file."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._lock_path = self.path.with_suffix(self.path.suffix + ".lock")

    # --- locking -----------------------------------------------------------------------
    @contextmanager
    def _locked(self) -> Iterator[None]:
        """Hold an exclusive ``flock`` on a sidecar lockfile for the duration of a read-modify-write.
        A sidecar (not the data file) so the lock survives the ``os.replace`` that swaps the data
        inode. Created on first use; never removed (a stable lock target)."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # open the lockfile r/w, creating it; the FD is the flock target.
        fd = os.open(self._lock_path, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)

    # --- raw IO (call under the lock for mutations) ------------------------------------
    def _read_raw(self, *, for_mutation: bool = False) -> list[dict[str, Any]]:
        """Load the JSON list. A missing file → ``[]``. A fault (an unreadable or corrupt file, a
        non-list top level): a READ fails soft to ``[]`` with a WARNING; a MUTATION read RAISES, because
        writing back what an unreadable file "contained" would replace every other pending with nothing."""
        def fault(why: str) -> list[dict[str, Any]]:
            _log.warning("pending store: %s%s", why, " — RE-RAISING (mutation)" if for_mutation else " — returning []")
            if for_mutation:
                raise OSError(f"pending store {self.path}: {why}; refusing to write")
            return []
        try:
            text = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return []
        except (OSError, UnicodeDecodeError) as e:
            return fault(f"read failed ({type(e).__name__}: {e})")
        try:
            data = json.loads(text)
        except json.JSONDecodeError as e:
            return fault(f"corrupt JSON ({e})")
        if not isinstance(data, list):
            return fault(f"top level is {type(data).__name__}, not a list")
        return [r for r in data if isinstance(r, dict)]

    def _write_raw(self, records: list[dict[str, Any]]) -> None:
        """Atomically replace the file with ``records`` (tmp + ``os.replace`` — never a torn read).
        Call only under ``_locked``."""
        durable_replace(self.path, json.dumps(records, ensure_ascii=False, indent=2))

    # --- public API --------------------------------------------------------------------
    def add(self, pending: PendingAction) -> None:
        """Persist a pending action. Locked read-modify-write so a concurrent add/remove can't lose
        it. A duplicate ``pending_id`` is REPLACED (idempotent re-propose), not duplicated."""
        with self._locked():
            records = [r for r in self._read_raw(for_mutation=True) if r.get("pending_id") != pending.pending_id]
            records.append(pending.to_dict())
            self._write_raw(records)

    def get(self, pending_id: str) -> PendingAction | None:
        """Return the pending action by id, or ``None``. A read needs no lock (atomic-replace writes
        mean a read sees a whole old-or-new file); a malformed matching record → ``None`` (logged)."""
        for r in self._read_raw():
            if r.get("pending_id") == pending_id:
                try:
                    return PendingAction.from_dict(r)
                except (KeyError, TypeError) as e:
                    _log.warning("pending store: malformed record %r (%s) — treating as absent",
                                 pending_id, type(e).__name__)
                    return None
        return None

    def list_open(self) -> list[PendingAction]:
        """All open pending actions (oldest first by file order). Malformed records are skipped
        loudly (one bad record never hides the rest)."""
        out: list[PendingAction] = []
        for r in self._read_raw():
            try:
                out.append(PendingAction.from_dict(r))
            except (KeyError, TypeError) as e:
                _log.warning("pending store: skipping malformed record (%s)", type(e).__name__)
        return out

    def remove(self, pending_id: str) -> bool:
        """Delete one pending action. Returns True iff it was present. Locked read-modify-write.
        NOTE: for resolving an action use :meth:`claim` (the atomic test-and-take) — ``remove`` does
        not return the record, so a get→fire→remove flow has a TOCTOU double-fire window."""
        with self._locked():
            records = self._read_raw(for_mutation=True)
            kept = [r for r in records if r.get("pending_id") != pending_id]
            if len(kept) == len(records):
                return False
            self._write_raw(kept)
            return True

    def claim(self, pending_id: str) -> PendingAction | None:
        """ATOMICALLY remove-and-return the pending action — the at-most-once gate (L1-HIGH-1/2).

        Under the flock, in ONE locked read-modify-write: if the id is present, REMOVE it from the
        open set and RETURN it (the caller now exclusively OWNS it); if absent (already claimed /
        resolved / swept), return ``None``. Exactly one of N concurrent callers wins the record; the
        losers get ``None``. This replaces the get→fire→remove TOCTOU: the resolver claims FIRST,
        then fires — so a CLI ``resolve`` racing the daemon ``sweep`` (or two resolves) can never
        both fire the same irreversible action, and a crash mid-fire DROPS the action (it is already
        out of the open set) rather than leaving it re-fireable. At-most-once for an irreversible
        action is the safe direction; a durable inflight/recovery record is a later hardening.

        A malformed claimed record is removed (it is unresolvable) and ``None`` is returned — a
        record that can't be reconstructed is dropped, never fired."""
        with self._locked():
            records = self._read_raw()
            matches = [r for r in records if r.get("pending_id") == pending_id]
            if not matches:
                return None
            # every copy goes: a file holding two records for one id must not resolve (and fire) twice
            records = [r for r in records if r.get("pending_id") != pending_id]
            rec = matches[0]
            try:
                pending = PendingAction.from_dict(rec)
            except (KeyError, TypeError) as e:
                pending = None   # malformed — drop it (it can never be resolved), don't return it
                _log.warning("pending store: claimed a malformed record %r (%s) — dropped, NOT fired",
                             pending_id, type(e).__name__)
            try:
                self._write_raw(records)   # commit the removal (atomic tmp+replace)
            except OSError as e:
                # the write failed BEFORE os.replace → the file is unchanged → the record SURVIVES and
                # is retriable. Fail-soft to "not claimed" (None) rather than propagate a misleading
                # resolve_error: the caller sees unknown_pending; the pending stays open (L3 complement
                # LOW-2). Never returns a claimed record we couldn't durably remove (no double-fire).
                _log.error("pending store: claim write FAILED for %r (%s): %s — NOT claimed (record survives)",
                           pending_id, type(e).__name__, e)
                return None
            return pending

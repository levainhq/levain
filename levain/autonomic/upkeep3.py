"""levain.autonomic.upkeep3 — the callers of a ``levain-autonomic/3`` store's upkeep (R15-5 S3).

The store's projection, its not_started reader and its D57 repair had no caller (levain project_memory
1027-11/r155scout/SCOUT.md W8, W19). This module calls them for a :class:`~levain.autonomic.journal.RunJournal` over a ``/3`` store:

  - :func:`project`: the receipts the store committed (``gate_receipts``, written in the same statement as their
    terminal row) copied to the receipt file (:meth:`GateReceiptStore.project_rows`), then marked projected;
  - :func:`gate_liveness`: :func:`levain.autonomic.liveness.gate_liveness` with the store's ``not_started`` set, read
    after the trace (R2-4);
  - :func:`reap`: one D57 pass (:func:`levain.autonomic.schema3.reap`) with the journal's own lease test, the flock an
    effect's owner holds from its intent to its end, which the OS drops when the owner dies;
  - :func:`tick`: a reap, then a projection, which is one turn of the reaper loop. What runs it, and how often, is the
    binding driver's (S4); no ``levain`` command reaches a ``/3`` store.

The doctor rows read the store's ``invariant`` and ``census`` themselves (``levain.doctor._check_autonomic_store``),
through a read-only connection, so a doctor run never writes the store.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any, Callable

from levain.autonomic import schema3
from levain.autonomic.authority import AuthorityScope
from levain.autonomic.journal import RunJournal, mark_projected, unprojected_receipts
from levain.autonomic.liveness import gate_liveness as _gate_liveness
from levain.autonomic.posture import Posture
from levain.autonomic.receipt import build_gate_face, build_gate_verdict
from levain.autonomic.store import GateReceiptStore

__all__ = ["project", "gate_liveness", "reap", "tick", "reaper_record", "REAPER_ACTION"]

_log = logging.getLogger(__name__)

# the action name and grant of a receipt the reaper writes: D56's sealed face (context, query, action) is not in a
# seal yet, so a reaped receipt names the reaper as its action and the subject as its context
REAPER_ACTION = "reap"


def _v3(journal: RunJournal, what: str) -> None:
    if not journal.v3:
        raise ValueError(f"upkeep3.{what}: the journal is not over a levain-autonomic/3 store")


def project(journal: RunJournal, receipt_store: GateReceiptStore) -> list[str]:
    """Copy every receipt the store holds unprojected into ``receipt_store``'s file, oldest first, then mark projected
    exactly the ids the file now holds. The mark is written only after :meth:`GateReceiptStore.project_rows`
    returns, which is after its flush: a stop between the two leaves the mark unset, and the next projection finds
    the line already in the file and does not append it again. Returns the ids marked."""
    _v3(journal, "project")
    with journal._read() as conn:
        rows = unprojected_receipts(conn)
    if not rows:
        return []
    held = receipt_store.project_rows(rows)
    with journal._write() as conn:
        mark_projected(conn, held)
    return held


def gate_liveness(journal: RunJournal, receipt_store: GateReceiptStore, *, limit: int | None = None) -> dict[str, Any]:
    """:func:`levain.autonomic.liveness.gate_liveness` over a ``/3`` journal's trace. It projects first, so the file
    holds every receipt committed before this call. Then it passes the store's ``not_started`` set, which
    ``gate_liveness`` reads after the trace (R2-4), so every listed receipt in the trace is counted as
    ``not_started``. A projection that fails is logged and the tally goes on over the file as it is: the set may then
    name receipts the trace lacks, and those count nowhere. A read of the set that fails is not raised: the tally goes
    on without it, so each not_started receipt counts as its receipt says (``refused``), and the result gains
    ``not_started_unread`` (``"<error type>: <message>"``) to say that ``by_terminal_state`` is not split. The trace
    read itself (``GateReceiptStore.read``) raises as it does for the plain call."""
    _v3(journal, "gate_liveness")
    try:
        project(journal, receipt_store)
    except Exception as e:  # noqa: BLE001 -- the tally runs over the file as it is
        _log.warning("upkeep3: receipts not projected before the liveness read (%s): %s", type(e).__name__, e)
    unread: list[str] = []

    def listed() -> frozenset[str]:
        try:
            with journal._read() as conn:
                return schema3.not_started_receipts(conn)
        except Exception as e:  # noqa: BLE001 -- named in the result, never raised
            _log.warning("upkeep3: the not_started set was not read (%s): %s", type(e).__name__, e)
            unread.append(f"{type(e).__name__}: {e}")
            return frozenset()
    stats = _gate_liveness(receipt_store, limit=limit, not_started=listed)
    if unread:
        stats["not_started_unread"] = unread[0]
    return stats


def _binding_of(journal: RunJournal, run_id: str) -> str | None:
    with journal._read() as conn:
        row = conn.execute("SELECT binding_id FROM runs WHERE run_id = ?", (run_id,)).fetchone()
    return None if row is None else row[0]


def reaper_record(journal: RunJournal, created_at: str) -> Callable[[str, str | None, str, str | None], dict]:
    """The receipt builder :func:`levain.autonomic.schema3.reap` calls, ``record_for(subject, seal, terminal_state,
    reason)``, built from the subject's sealed row alone (D56), since nothing else of the owner survives it:
      - an effect attempt: its seal's rung, its actor ``by``, its verdict from the sealed hold decision's decider
        (``approved`` only when ``$.decision.by`` is ``human``, else ``auto``, as the gate's own fire of a journaled
        hold words it in ``EfferentGate``'s resolve path), and its ``floor`` / ``grant_guard``
        (``receipts_carry_guard``);
      - a run: a refusal by ``on-loop`` at ``REFUSE_ESCALATE`` (a run seal holds no rung), carrying the run's guard;
      - a pending: a refusal by ``on-loop`` at ``REFUSE_ESCALATE``, no guard.
    Each receipt is built with the gate's ``/3`` builder (``EfferentGate._v3_receipt``), so the face is the frozen
    contract's shape plus ``guard`` / ``grant_guard``."""
    from levain.autonomic.gate import EfferentGate

    def build(subject: str, seal: str | None, terminal_state: str, reason: str | None) -> dict:
        sealed = json.loads(seal) if seal else {}
        kind = subject.split(":", 1)[0]
        guard = grant_guard = None
        if kind == "effect":
            run_id = schema3._effect_key(subject)[0]
            posture = Posture[sealed["posture"]]
            decided_by = (sealed.get("decision") or {}).get("by")
            verdict, by = ("approved" if decided_by == "human" else "auto"), sealed["by"]
            guard, grant_guard = sealed.get("floor"), sealed.get("grant_guard")
        else:
            run_id = subject[4:] if kind == "run" else None
            posture, verdict, by = Posture.REFUSE_ESCALATE, "denied", "on-loop"
            if kind == "run":
                guard = sealed.get("guard")
        binding_id = _binding_of(journal, run_id) if run_id is not None else None
        face = build_gate_face(
            context_id=subject, query_text="", query_date=created_at, producers=[],
            gate=build_gate_verdict(verdict=verdict, by=by, binding_id=binding_id),
            authority=AuthorityScope(grantor="binding" if binding_id else "human", grant=f"reaper:{reason or terminal_state}",
                                     binding_id=binding_id),
            terminal_state=terminal_state,
        )
        return EfferentGate._v3_receipt(subject, created_at=created_at, action_name=REAPER_ACTION, proposal_id=None,
                                        posture=posture, fired=False, face=face, guard=guard,
                                        grant_guard=grant_guard).record
    return build


def _pending_owner_unknown(pending_id: str) -> bool:
    # no levain code writes a /3 manual pending or holds a pending lease, so no owner can be tested: a firing pending
    # is left as in flight. tests/test_autonomic_upkeep3.py::test_no_module_writes_a_manual_pending_without_its_lease
    # fails when a writer arrives
    return True


def reap(journal: RunJournal, *, now: datetime | None = None) -> dict[str, str]:
    """One D57 pass over the journal's ``/3`` store (:func:`levain.autonomic.schema3.reap`). An ``intent`` attempt
    whose owner still holds its lease (:meth:`RunJournal._lease_held`, the real flock) is ``in_flight`` and left as it
    is; one whose owner is gone closes ``unknown`` with an ``outcome_unknown`` receipt (:func:`reaper_record`). Every
    receipt of the pass has the same ``created_at`` (``now``, UTC). Returns schema3's ``{subject: outcome}``."""
    _v3(journal, "reap")
    return schema3.reap(journal.db, lease_held=journal._lease_held, pending_lease_held=_pending_owner_unknown,
                        record_for=reaper_record(journal, schema3.utc_text(now)))


def tick(journal: RunJournal, receipt_store: GateReceiptStore, *,
         now: datetime | None = None) -> tuple[dict[str, str], list[str]]:
    """One turn of the reaper loop: :func:`reap`, then :func:`project`, so a receipt the pass wrote reaches the file
    in the same turn. Returns both results."""
    reaped = reap(journal, now=now)
    return reaped, project(journal, receipt_store)

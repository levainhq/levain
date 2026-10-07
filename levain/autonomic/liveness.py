"""levain.autonomic.liveness — binding + kill/gate liveness telemetry (Slice 3a.5, piece 5).

A standing grant that fires autonomously, a kill that is supposed to stop it, and a monitor that is
supposed to catch the unpredicted are all INVISIBLE-INFRASTRUCTURE risks: they can silently rot (a
registry made inert by a record whose identity does not derive; a confirm-class grant with no sealed kill; a kill that never
trips; a monitor that never fires) while the registry "looks healthy" by record count. This module is
the structural read against that failure mode (``invisible_infrastructure_failure``): a pure,
side-effect-free tally over the binding registry + the gate-receipt trace, so the operator (and
Bridge/FleetView) can SEE the governed loop's live shape, not infer it.

Two reports, both pure dicts (stdlib-only, no I/O beyond the injected stores' own reads):
  - :func:`binding_liveness` over a :class:`~levain.autonomic.binding.BindingStore` — what STANDS
    (how many grants, at what lifecycle, how many BARRED-from-firing, how much kill coverage, how much
    tightening, how many carry a prediction-error monitor).
  - :func:`gate_liveness` over a :class:`~levain.autonomic.store.GateReceiptStore` — what HAPPENED (the
    terminal-state mix: fired / refused / killed / timed_out — the ``killed`` count is the
    prediction-error monitor's live firing rate, the 3a.5 signal that the unknown-danger backstop is
    actually catching things, not dead).

A liveness read is a PROJECTION (a replaceable surface over the canonical stores), never authority —
it counts, it never decides (``canonical_object_model_plus_replaceable_surfaces``).
"""
from __future__ import annotations

from typing import Any

from levain.autonomic.binding import BindingStatus, BindingStore, Guard
from levain.autonomic.monitor import trajectory_bound
from levain.autonomic.store import GateReceiptStore

__all__ = ["binding_liveness", "gate_liveness"]


def _guard_stats(guards: tuple[Guard, ...]) -> tuple[int, int, int, int]:
    """(count, kill_bearing, with_predicted_trajectory, with_monitor_bound) over a guard tuple."""
    count = len(guards)
    kill_bearing = sum(1 for g in guards if g.has_kill)
    with_traj = sum(1 for g in guards if g.predicted_trajectory is not None)
    with_bound = sum(1 for g in guards if trajectory_bound(g.predicted_trajectory) is not None)
    return count, kill_bearing, with_traj, with_bound


def binding_liveness(store: BindingStore) -> dict[str, Any]:
    """Tally the live shape of the binding registry. Reads the FULL inspection view (``list_all`` —
    includes inactive records, which ``list_active`` bars) once, plus the fire-view (``list_active``)
    for the canonical fireable count, so the BARRED gap (records that exist but cannot fire) is
    visible — the whole point of the read.

    Keys:
      - ``registry_corrupt`` — ``None``, or why the registry reads as NO bindings
        (:meth:`~levain.autonomic.binding.BindingStore.integrity`: a duplicate, a record whose core does
        not seal to its id, unreadable JSON). Without it a corrupt registry tallies exactly like an empty
        one; a non-``None`` value is a TAMPER/corruption signal and every other count below is zero.
      - ``total`` / ``by_status`` — record count + lifecycle mix.
      - ``fireable`` — the ``list_active`` count (active + sealed + a sealed kill at confirm-class).
      - ``barred_confirm_no_kill`` — ACTIVE confirm-class records with no SEALED-floor kill (barred by
        the ``RECEIPT_VERSION=4`` mandate; a nonzero count means a grant was minted/seeded around the
        compiler).
      - ``guards_floor`` / ``kill_bearing_floor`` — sealed-floor guard + kill coverage.
      - ``guards_additions`` / ``kill_bearing_additions`` — the unsealed 3a.5 tightening tier.
      - ``with_predicted_trajectory`` / ``with_monitor_bound`` — guards carrying a prediction-error
        trajectory, and the subset whose trajectory has a machine-checkable bound (a live monitor).
    """
    all_bindings = store.list_all()
    fireable = len(store.list_active())

    by_status = {s.value: 0 for s in BindingStatus}
    barred_confirm_no_kill = 0
    guards_floor = kill_floor = guards_add = kill_add = with_traj = with_bound = 0

    for b in all_bindings:
        by_status[b.status.value] = by_status.get(b.status.value, 0) + 1
        # confirm-class + active + NO sealed-floor kill ⇒ barred by the mandate. (Every record read here
        # proved its seal: one that does not makes the registry corrupt and the read empty.)
        if (b.status.is_active and b.posture.needs_confirm
                and not any(g.has_kill for g in b.guard)):
            barred_confirm_no_kill += 1
        f_count, f_kill, f_traj, f_bound = _guard_stats(b.guard)
        a_count, a_kill, a_traj, a_bound = _guard_stats(b.guard_additions)
        guards_floor += f_count
        kill_floor += f_kill
        guards_add += a_count
        kill_add += a_kill
        with_traj += f_traj + a_traj
        with_bound += f_bound + a_bound

    return {
        "registry_corrupt": store.integrity(),
        "total": len(all_bindings),
        "by_status": by_status,
        "fireable": fireable,
        "barred_confirm_no_kill": barred_confirm_no_kill,
        "guards_floor": guards_floor,
        "kill_bearing_floor": kill_floor,
        "guards_additions": guards_add,
        "kill_bearing_additions": kill_add,
        "with_predicted_trajectory": with_traj,
        "with_monitor_bound": with_bound,
    }


def gate_liveness(receipt_store: GateReceiptStore, *, limit: int | None = None) -> dict[str, Any]:
    """Tally the terminal-decision mix from the gate-receipt trace. ``limit`` bounds the read to the
    most recent N receipts (None = the whole trace).

    Keys:
      - ``total`` — receipts read.
      - ``fired`` — receipts whose action fired (``record.fired``).
      - ``killed`` — the prediction-error monitor's live firing count (``terminal_state=killed``); the
        3a.5 unknown-danger-backstop signal. A persistently-zero ``killed`` alongside a nonzero
        ``with_monitor_bound`` (from :func:`binding_liveness`) is the watch-item — a monitor wired but
        never catching, the dead-backstop shape.
      - ``by_terminal_state`` — the full MODE mix (fired / refused / killed / timed_out / ``null`` for
        any legacy pre-3a.5 receipt that predates the field).
      - ``by_verdict`` — the decision-CLASS mix (approved / denied / auto).
      - ``by_refuse_class`` — the refuse taxonomy where stamped (today: ``kill_triggered``).
    """
    receipts = receipt_store.read(limit=limit)
    by_terminal: dict[str, int] = {}
    by_verdict: dict[str, int] = {}
    by_refuse: dict[str, int] = {}
    fired = 0
    for r in receipts:
        if r.fired:
            fired += 1
        face = r.action_face if isinstance(r.action_face, dict) else {}
        ts = face.get("terminal_state")
        ts_key = ts if isinstance(ts, str) else "null"
        by_terminal[ts_key] = by_terminal.get(ts_key, 0) + 1
        gate = face.get("gate")
        if isinstance(gate, dict):
            v = gate.get("verdict")
            if isinstance(v, str):
                by_verdict[v] = by_verdict.get(v, 0) + 1
        rc = face.get("refuse_class")
        if isinstance(rc, str):
            by_refuse[rc] = by_refuse.get(rc, 0) + 1

    return {
        "total": len(receipts),
        "fired": fired,
        "killed": by_terminal.get("killed", 0),
        "by_terminal_state": by_terminal,
        "by_verdict": by_verdict,
        "by_refuse_class": by_refuse,
    }

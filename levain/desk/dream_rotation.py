"""The dream rotation (levain#82): a desk at a dream seam hands itself over onto the memory a dream saved.

:func:`rotate` is the ONE door. Inside ONE ``BEGIN IMMEDIATE`` (the desk's token fence first) it re-runs
:func:`levain.desk.dream_status.seam_check` on its own connection, writes the dream handoff through the one handoff
writer (:func:`levain.desk.seam_handoffs._append`), ends the desk's launch and releases its lease
(:func:`levain.desk.lease.end_for_dream`), and asks for the next desk (:func:`levain.desk.lease.wake`, the night
model: a dream saves at night). Every write follows the ``seam_check`` True; no argument skips it. When the check is
False, or the wake asks for nothing, the transaction raises and rolls back whole: no handoff, no ended desk, no beat.

The desk's own statement that it is at a turn boundary is the caller's (``at_turn_boundary=True`` is passed here
because the desk ran its open with a body file; the store records no desk turn). Stdlib only.
"""
from __future__ import annotations

from dataclasses import dataclass

from levain.autonomic.durable_sqlite import read_transaction
from levain.contracts.canon import canonical
from levain.desk import dream_status, lease as _leases, seam_handoffs
from levain.desk.legacy import ROLE_DESK
from levain.desk.seam_open import active_desk_fence
from levain.desk.store import CoordError

__all__ = ["KEY_PREFIX", "AlreadyStored", "NotDue", "Rotation", "rotate"]

#: the handoff's ``close_key`` is this and the dream's occurrence: one rotation per dream occurrence
KEY_PREFIX = "dream-rotation:"


@dataclass(frozen=True)
class Rotation:
    """A rotation that happened: the desk's launch is ended, its lease released, the next desk asked for."""
    handoff: seam_handoffs.SeamHandoff
    ended: _leases.Launch
    successor: _leases.Launch
    occurrence: str


@dataclass(frozen=True)
class AlreadyStored:
    """NOT a rotation: a handoff already sits under this occurrence's key (a manual close used the literal key), so
    nothing was written and the desk is still live, holding its lease."""
    handoff: seam_handoffs.SeamHandoff
    occurrence: str


@dataclass(frozen=True)
class NotDue:
    """Nothing was written: ``why`` says which conjunct of the seam invariant failed."""
    why: str


class _NotDue(Exception):
    """Raised inside the write to roll it back (a plain return would commit ``_write``'s own lease beat)."""


class _AlreadyDone(Exception):
    def __init__(self, handoff: seam_handoffs.SeamHandoff, occurrence: str) -> None:
        super().__init__(handoff.close_key)
        self.handoff = handoff
        self.occurrence = occurrence


def _advisory(store, token: int) -> tuple[bool, str]:
    """The read before the write (``rotation_due``: advisory). ``(True, "")`` when the launch cannot be found, so the
    fenced write says the refusal (a token that is not the live lease's)."""
    try:
        with read_transaction(store.open_readonly) as conn:
            held = _leases.lease(conn, store._stamp())
            ln = None if held is None or held.token != token else _leases._launch_by_key(conn, held.holder)
        if ln is None:
            return True, ""
        return dream_status.rotation_due(store, ln, at_turn_boundary=True)
    except Exception:  # noqa: BLE001 - best-effort: the in-transaction seam_check is the one authority
        return True, ""


def rotate(store, *, token: object, body: object) -> Rotation | NotDue | AlreadyStored:
    """Rotate the live desk (``token``) onto the newest dream save, writing ``body`` as the dream handoff.

    Refused before anything is opened: a token that is not an int, a body :func:`seam_handoffs._check_body` refuses.
    :class:`NotDue` (nothing written): the body attests the operator is present, or the seam invariant is False
    (read first, then again inside the write). :class:`AlreadyStored` (nothing written, the desk still live): the key
    is already stored. Raises (nothing written): a token the fence refuses, and a store that
    cannot ask for a next desk (no desk agent set, backoff, stopped): the rotation never ends a desk it could not
    replace."""
    if token is None or not isinstance(token, int) or isinstance(token, bool):
        raise CoordError("a dream rotation needs the desk's identity (the live lease token); nothing was written")
    body = seam_handoffs._check_body(body)
    if body["attested_operator_present"]:
        return NotDue("the handoff body attests the operator is present, so this is not a seam to hand over at")
    due, why = _advisory(store, token)
    if not due:
        return NotDue(why)
    canon_body = canonical(body)
    try:
        with store._write(ROLE_DESK, token) as (conn, now):
            held, launch = active_desk_fence(conn, now, token, "a dream rotation")
            due, why = dream_status.seam_check(conn, launch.launch_id, now, at_turn_boundary=True)
            if not due:
                raise _NotDue(why)
            # re-derived, not taken from seam_check: one transaction, so it names the save seam_check just judged
            saved, _ = dream_status._current(dream_status.records(conn, strict=True))
            occurrence = saved.occurrence    # a non-empty str: dream_status's parser builds it with str() and refuses ""
            key = KEY_PREFIX + occurrence
            seam_handoffs._check_request_id(key, "a dream rotation")
            stored = seam_handoffs._row(conn.execute(
                f"SELECT {seam_handoffs._COLUMNS} FROM seam_handoffs WHERE close_key = ?", (key,)).fetchone())
            if stored is not None:
                raise _AlreadyDone(stored, occurrence)
            head = seam_handoffs._row(conn.execute(
                f"SELECT {seam_handoffs._COLUMNS} FROM seam_handoffs ORDER BY handoff_id DESC LIMIT 1").fetchone())
            handoff = seam_handoffs._append(
                conn, now, writer=held.holder, kind="dream", body=body, canon_body=canon_body, request_id=key,
                prev_handoff_id=None if head is None else head.handoff_id)
            ended = _leases.end_for_dream(conn, now, launch.launch_id)
            successor = _leases.wake(conn, now, night=True)
            if successor is None:
                state, reason = _leases.wake_state(conn, now)
                raise CoordError(f"no next desk could be asked for ({state}: {reason}); nothing was rotated, the "
                                 "handoff was not written and this desk keeps its lease")
            return Rotation(handoff, ended, successor, occurrence)
    except _NotDue as stop:
        return NotDue(str(stop))
    except _AlreadyDone as done:
        return AlreadyStored(done.handoff, done.occurrence)

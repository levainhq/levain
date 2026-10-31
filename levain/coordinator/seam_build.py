"""Writing a build (C15 S2b): the two paths that turn :func:`levain.coordinator.seam.open_brief` into an ``open_builds``
row, and nothing else.

* :func:`starter_build`: a COLD START's path. No coordinator holds the lease yet, so there is no token to fence with;
  the anchor is the launch the starter was started for (the same one ``launch_claim_spawn`` keys on). ONE function, all
  of it readable top to bottom.
* :func:`build`: every other build, fenced by :func:`levain.coordinator.seam_open.active_coordinator_fence`. A
  ``launch`` signal is never its: only the starter builds for a launch, its own pending one.

Both look the signal up FIRST: a signal already built returns its stored build (under the anchor or the fence) before
anything is rendered, so a retry is the same build even when the stores have since grown or a read now fails. A new
signal renders (a read), then writes in one transaction that re-checks its anchor, re-checks the signal and inserts.
The rendered body is stored whole, so a build that died before delivery is re-sent faithfully. A degraded brief (a
read the successor must see failed) and a body over the caller's cap are refused before the write opens, so a retry
reads the stores again. Stdlib only.
"""
from __future__ import annotations

import hashlib
from contextlib import closing
from pathlib import Path
from typing import Any

from levain.coordinator import lease as _leases
from levain.coordinator.lease import LeaseRefused
from levain.coordinator.seam import Brief, open_brief
from levain.coordinator.seam_open import (BODY_CAP, LAUNCH_SIGNALS, SEAM_KINDS, SIGNAL_KINDS, OpenBuild,
                                         active_coordinator_fence, checked_text, insert_build, stored_build)
from levain.coordinator.store import CoordError, CoordStore

__all__ = ["build", "starter_build"]


def _check(kind: Any, signal_kind: Any, signal_key: Any) -> None:
    if kind not in SEAM_KINDS:
        raise CoordError(f"an open is of kind {', '.join(SEAM_KINDS)}, not {kind!r}")
    if signal_kind not in SIGNAL_KINDS:
        raise CoordError(f"an open's signal is one of {', '.join(SIGNAL_KINDS)}, not {signal_kind!r}")
    checked_text(signal_key, "an open's signal key")


def _storable(brief: Brief, cap: int) -> bytes:
    """The body's bytes, or :class:`CoordError` before the write transaction opens: a degraded brief (stored, it would
    be returned for its signal for good), and a body over the effective cap, ``min(cap, BODY_CAP)``."""
    if brief.degraded:
        raise CoordError(f"the brief's store read failed ({brief.degraded}); a degraded brief is never stored, so "
                         "nothing was written and a retry builds it again")
    return checked_text(brief.text, "the brief's body", max_bytes=min(cap, BODY_CAP), nonempty=False).encode("utf-8")


def _insert(conn: Any, now: str, brief: Brief, body: bytes, signal_kind: str, signal_key: str) -> OpenBuild:
    return insert_build(conn, now, seam_kind=brief.kind, signal_kind=signal_kind, signal_key=signal_key,
                        cursor_gseq=brief.cursor_gseq, shown_through_gseq=brief.shown_through_gseq,
                        handoff_id=brief.handoff_id, body=brief.text, body_sha256=hashlib.sha256(body).hexdigest(),
                        cuts=brief.unsequenced_cuts)


def _starter_anchor(conn: Any, now: str, launch_id: str, pid: int) -> None:
    """The cold start's anchor, inside a write transaction: refused unless every precondition of
    :func:`starter_build` holds. A refusal's writes (``refuse_several``'s ``failed`` row) roll back with it."""
    ln = _leases.launch(conn, launch_id)
    if ln is None:
        raise LeaseRefused(f"there is no launch {launch_id}; nothing was written")
    several = _leases.refuse_several(conn, now, ln)
    if several is not None:
        raise LeaseRefused(f"{several[1]}; nothing was written")
    if ln.role != "coordinator":
        raise LeaseRefused(f"launch {launch_id} is a {ln.role}'s, and an open is a coordinator's; nothing was written")
    if ln.state != "pending":
        raise LeaseRefused(f"launch {launch_id} is {ln.state}, not pending; nothing was written")
    spawn = conn.execute("SELECT pid FROM launch_spawns WHERE launch_id = ?", (launch_id,)).fetchone()
    if spawn is None or spawn[0] != pid:
        raise LeaseRefused(f"launch {launch_id}'s spawn is {'not claimed' if spawn is None else 'claimed by pid ' + str(spawn[0])}"
                           f", not by pid {pid}; nothing was written")
    if ln.until is None or now >= ln.until:
        raise LeaseRefused(f"launch {launch_id} was pending only until {ln.until}; nothing was written")
    held = _leases.lease(conn, now)
    if held is None or not held.live or held.token != ln.token:
        raise LeaseRefused(f"launch {launch_id} was asked for under lease token {ln.token}, which is not the live "
                           "lease's; nothing was written")


def starter_build(store: CoordStore, launch_id: Any, *, pid: Any, kind: str, queue_dir: Path | str | None) -> OpenBuild:
    """The cold start's build, for the launch ``launch_id`` its starter process (``pid``) claimed the spawn of. Signal
    ``launch``, key the launch id: a second call is the same build, returned before anything renders. Refused with
    nothing written unless ALL of these hold in the write transaction (checked before the lookup and again before the
    insert): the launch exists and is a coordinator's, its latest event is ``pending``, ``launch_spawns.pid`` is this
    starter's pid, ``now`` is before its ``until``, and the live lease's token is the one the launch was asked for
    under. It runs ``claim_spawn``'s several-threads check (:func:`lease.refuse_several`) and inherits none of its
    writes: no ``expired``, no ``failed``, no spawn row, no lapse, so a refusal leaves the store as it found it."""
    if not isinstance(launch_id, str) or not isinstance(pid, int) or isinstance(pid, bool):
        raise CoordError("a starter build names its launch id (text) and its own pid (a number)")
    _check(kind, "launch", launch_id)
    store._connect().close()          # the store a writer brings current: the read below must see the open tables
    with store._write() as (conn, now):
        _starter_anchor(conn, now, launch_id, pid)
        stored = stored_build(conn, kind, "launch", launch_id)
    if stored is not None:
        return stored
    brief = open_brief(store, queue_dir, kind, store._clock())
    body = _storable(brief, BODY_CAP)
    with store._write() as (conn, now):
        _starter_anchor(conn, now, launch_id, pid)
        return _insert(conn, now, brief, body, "launch", launch_id)


def build(store: CoordStore, *, kind: str, signal_kind: str, signal_key: str, queue_dir: Path | str | None,
          token: Any, body_cap: int = BODY_CAP) -> OpenBuild:
    """Every build but the cold start's: fenced by the lease token and :func:`active_coordinator_fence` (the operator's
    lease is refused). A signal already built returns its build, under the fence, before anything renders, and writes
    nothing but the fence's own record of the write. A ``launch`` signal is refused: it is the starter's
    (:func:`starter_build`). ``body_cap`` can only lower the cap: it is clamped to :data:`BODY_CAP`, the column's own."""
    _check(kind, signal_kind, signal_key)
    if signal_kind in LAUNCH_SIGNALS:
        raise CoordError(f"a {signal_kind} signal's build is the starter's (starter_build), for its own pending launch; "
                         "nothing was written")
    if token is None or not isinstance(token, int) or isinstance(token, bool):
        raise CoordError("a build needs the coordinator's identity (the live lease token); nothing was written")
    if not isinstance(body_cap, int) or isinstance(body_cap, bool):
        raise CoordError("a build's body cap is a number of bytes")
    cap = min(body_cap, BODY_CAP)
    with closing(store._connect()) as conn:       # the store a writer brings current, and the signal looked up first
        stored = stored_build(conn, kind, signal_kind, signal_key)
    if stored is None:
        brief = open_brief(store, queue_dir, kind, store._clock(), body_cap=cap)
        body = _storable(brief, cap)
    with store._write("coordinator", token) as (conn, now):
        active_coordinator_fence(conn, now, token, "an open build")
        if stored is not None:
            # append-only: the row looked up is the row; the fence is what this transaction adds
            return stored_build(conn, kind, signal_kind, signal_key)
        return _insert(conn, now, brief, body, signal_kind, signal_key)

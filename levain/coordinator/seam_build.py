"""Writing a build (C15 S2b): the two paths that turn :func:`levain.coordinator.seam.open_brief` into an ``open_builds``
row, and nothing else.

* :func:`starter_build`: a COLD START's path. No coordinator holds the lease yet, so there is no token to fence with;
  the anchor is the launch the starter was started for (the same one ``launch_claim_spawn`` keys on). ONE function, all
  of it readable top to bottom.
* :func:`build`: every other build, fenced by :func:`levain.coordinator.seam_open.active_coordinator_fence`.

Both render first (a read), then write in one transaction that re-checks its anchor, returns the stored build when the
signal was already built (a duplicate signal is the same build, never a second one), and inserts. The rendered body is
stored whole, so a build that died before delivery is re-sent faithfully. Stdlib only.
"""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from levain.coordinator import lease as _leases
from levain.coordinator.lease import LeaseRefused
from levain.coordinator.seam import Brief, open_brief
from levain.coordinator.seam_open import (BODY_CAP, SEAM_KINDS, SIGNAL_KINDS, OpenBuild, active_coordinator_fence, insert_build)
from levain.coordinator.store import CoordError, CoordStore

__all__ = ["build", "starter_build"]


def _check(kind: Any, signal_kind: Any, signal_key: Any) -> None:
    if kind not in SEAM_KINDS:
        raise CoordError(f"an open is of kind {', '.join(SEAM_KINDS)}, not {kind!r}")
    if signal_kind not in SIGNAL_KINDS:
        raise CoordError(f"an open's signal is one of {', '.join(SIGNAL_KINDS)}, not {signal_kind!r}")
    if not isinstance(signal_key, str) or not signal_key.strip() or "\x00" in signal_key:
        raise CoordError("an open's signal key is non-empty text")


def _insert(conn: Any, now: str, brief: Brief, signal_kind: str, signal_key: str) -> OpenBuild:
    return insert_build(conn, now, seam_kind=brief.kind, signal_kind=signal_kind, signal_key=signal_key,
                        cursor_gseq=brief.cursor_gseq, shown_through_gseq=brief.shown_through_gseq,
                        handoff_id=brief.handoff_id, body=brief.text,
                        body_sha256=hashlib.sha256(brief.text.encode("utf-8")).hexdigest(), cuts=brief.unsequenced_cuts)


def starter_build(store: CoordStore, launch_id: Any, *, pid: Any, kind: str, queue_dir: Path | str | None) -> OpenBuild:
    """The cold start's build, for the launch ``launch_id`` its starter process (``pid``) claimed the spawn of. Signal
    ``launch``, key the launch id: a second call is the same build. Refused with nothing written unless ALL of these
    hold in the write transaction: the launch exists and is a coordinator's, its latest event is ``pending``,
    ``launch_spawns.pid`` is this starter's pid, ``now`` is before its ``until``, and the live lease's token is the one
    the launch was asked for under. It inherits none of ``claim_spawn``'s writes: no ``expired``, no ``failed``, no
    spawn row, no lapse, so a refusal leaves the store as it found it."""
    if not isinstance(launch_id, str) or not isinstance(pid, int) or isinstance(pid, bool):
        raise CoordError("a starter build names its launch id (text) and its own pid (a number)")
    _check(kind, "launch", launch_id)
    store._connect().close()          # the store a writer brings current: the read below must see the open tables
    brief = open_brief(store, queue_dir, kind, store._clock())
    with store._write() as (conn, now):
        ln = _leases.launch(conn, launch_id)
        if ln is None:
            raise LeaseRefused(f"there is no launch {launch_id}; nothing was written")
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
        return _insert(conn, now, brief, "launch", launch_id)


def build(store: CoordStore, *, kind: str, signal_kind: str, signal_key: str, queue_dir: Path | str | None,
          token: Any, body_cap: int = BODY_CAP) -> OpenBuild:
    """Every build but the cold start's: fenced by the lease token and :func:`active_coordinator_fence` (the operator's
    lease is refused). A signal already built returns its build and writes nothing but the fence's own record of the
    write."""
    _check(kind, signal_kind, signal_key)
    if token is None or not isinstance(token, int) or isinstance(token, bool):
        raise CoordError("a build needs the coordinator's identity (the live lease token); nothing was written")
    store._connect().close()
    brief = open_brief(store, queue_dir, kind, store._clock(), body_cap=body_cap)
    with store._write("coordinator", token) as (conn, now):
        active_coordinator_fence(conn, now, token, "an open build")
        return _insert(conn, now, brief, signal_kind, signal_key)

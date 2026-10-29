"""levain.cockpit.coordinator_status: the cockpit's Coordinator panel (C10 S2), read-only.

It draws :func:`levain.coordinator.lease.status`, the data ``levain coord status`` prints: the ``wake_state`` word and
its ``why`` (quoted, never reworded), who holds the lease and until when, the coordinator setting and the latest
launches with the cause each one carries. Nothing here recomputes a state or decides anything.

The panel never writes. ``CoordStore.coord_status`` records a lapse and expires a pending launch before it answers, so
it is a write; this view opens the store's read-only connection (:meth:`CoordStore.open_readonly`, ``query_only``)
and calls ``lease.status`` on it directly. A lapse or an expiry that is due but not yet written is applied VIRTUALLY:
only when one is due, the snapshot is copied into memory, the coordinator's own ``_lapse_if_past`` and
``_expire_pending`` run on the copy, and the state word and its ``why`` come from there, so the state row is the word
``coord_status`` would report. The lease and launch rows keep what the store holds and say the write is not recorded.
Start, Stop and Hold are a later session's verbs; this panel names the CLI line that does each act.
"""
from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from levain.cockpit.consent import utc_stamp
from levain.cockpit.engine import ReadContext
from levain.cockpit.results import Read, Result, RowIn

__all__ = ["COORD_STATUS_ID", "status_reader"]

COORD_STATUS_ID = "coord-status"


def _read_status(store: Any, now: datetime) -> dict[str, Any]:
    import sqlite3

    from levain.autonomic.durable_sqlite import read_transaction
    from levain.coordinator import lease

    stamp = utc_stamp(now)
    with read_transaction(store.open_readonly) as conn:     # one snapshot for the lease, the state and the launches
        st = lease.status(conn, stamp)
        held = lease.lease(conn, stamp)
        if (held is not None and not held.live) or any(stamp >= ln.until for ln in lease._in_state(conn, "pending")):
            # a due lapse or expiry nothing has written: what a write would record, applied to a throwaway copy
            scratch = sqlite3.connect(":memory:", isolation_level=None)
            try:
                conn.backup(scratch)
                lease._lapse_if_past(scratch, stamp)
                lease._expire_pending(scratch, stamp)
                st["wake"], st["why"] = lease.wake_state(scratch, stamp)
            finally:
                scratch.close()
        return st


def _lease_body(held: dict[str, Any] | None) -> str:
    if held is None:
        return "none held"
    body = f"{held['holder']} until {held['until']} (token {held['token']}, {held['cause']})"
    if not held["live"]:
        body += "; past its until, the lapse is not yet recorded"
    return body


def _setting_body(chosen: dict[str, Any] | None) -> str:
    if chosen is None:
        return "none set (levain coord agent claude sets one)"
    parts = [str(chosen["agent"])]
    if chosen.get("model"):
        parts.append(f"model {chosen['model']}")
    if chosen.get("cwd"):
        parts.append(f"in {chosen['cwd']}")
    return ", ".join(parts)


def _launch_body(ln: dict[str, Any], now: str | None = None) -> str:
    body = f"{ln['harness']} {ln['role']} · {ln['cause']} · {ln['at']}"
    if ln["state"] == "pending" and ln.get("until"):
        if now is not None and now >= ln["until"]:
            body += f" · pending, past its until; not yet recorded as expired (until {ln['until']})"
        else:
            body += f" · pending until {ln['until']}"
    return body


def status_rows(st: dict[str, Any], now: datetime | None = None) -> tuple[RowIn, ...]:
    """The panel's rows from ``lease.status`` data: the state line first (the wake word, then its ``why`` as the
    lease module wrote it), then the lease, the setting and the launches newest first. No row carries an age that
    moves with the clock, so a row's version changes when the store or a stated deadline does (``now`` is the
    instant a pending launch is judged past its until against)."""
    stamp = None if now is None else utc_stamp(now)
    from levain.cockpit.coordinator_panels import _row

    rows = [_row("state", "Coordinator", f"{st['wake']}: {st['why']}"),
            _row("lease", "Lease", _lease_body(st["lease"])),
            _row("setting", "Setting", _setting_body(st["setting"]))]
    rows += [_row(f"launch:{ln['launch_id']}", f"Launch · {ln['state']}", _launch_body(ln, stamp)) for ln in st["launches"]]
    return tuple(rows)


def status_reader(directory: Callable[[], Path | str | None], clock: Callable[[], datetime] | None = None,
                  ) -> Callable[[ReadContext], Result]:
    """The panel's read callable: a store that cannot be read is its error row, never a raise."""
    from levain.cockpit.coordinator_panels import _store_at, _unreadable

    from levain.coordinator.store import StoreAbsent

    def read(ctx: ReadContext) -> Result:
        # the read cycle's one instant; an injected clock (a test seam) is the only thing that overrides it
        now = clock() if clock is not None else ctx.now
        try:
            store = _store_at(directory)
            if store is None:
                return Read(rows=())
            try:
                rows = status_rows(_read_status(store, now), now)
            except StoreAbsent:             # gone between the probe and the read: the same read as no store
                return Read(rows=())
        except Exception as e:
            return Read(rows=(_unreadable(e),))
        return Read(rows=rows, note=None)

    return read

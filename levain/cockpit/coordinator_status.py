"""levain.cockpit.coordinator_status: the cockpit's Coordinator panel (C10 S2), read-only.

It draws :func:`levain.coordinator.lease.status`, the data ``levain coord status`` prints: the ``wake_state`` word and
its ``why`` (quoted, never reworded), who holds the lease and until when, the coordinator setting and the latest
launches with the cause each one carries. Nothing here recomputes a state or decides anything.

The panel never writes. ``CoordStore.coord_status`` records a lapse and expires a pending launch before it answers, so
it is a write; this view opens the store's read-only connection (:meth:`CoordStore.open_readonly`, ``query_only``)
and calls ``lease.status`` on it directly. The cost is stated: a lapse or an expiry that nothing has written yet is
read as the store holds it, which is what :func:`wake_state` already says ("what a drop arriving now would do").
Start, Stop and Hold are a later session's verbs; this panel names the CLI line that does each act.
"""
from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from levain.cockpit.consent import utc_stamp
from levain.cockpit.engine import ReadContext
from levain.cockpit.results import Read, Result, RowIn

__all__ = ["COORD_STATUS_ID", "status_reader"]

COORD_STATUS_ID = "coord-status"


def _read_status(store: Any, now: datetime) -> dict[str, Any]:
    from levain.autonomic.durable_sqlite import read_transaction
    from levain.coordinator import lease

    with read_transaction(store.open_readonly) as conn:     # one snapshot for the lease, the state and the launches
        return lease.status(conn, utc_stamp(now))


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


def _launch_body(ln: dict[str, Any]) -> str:
    body = f"{ln['harness']} {ln['role']} · {ln['cause']} · {ln['at']}"
    if ln["state"] == "pending" and ln.get("until"):
        body += f" · pending until {ln['until']}"
    return body


def status_rows(st: dict[str, Any]) -> tuple[RowIn, ...]:
    """The panel's rows from ``lease.status`` data: the state line first (the wake word, then its ``why`` as the
    lease module wrote it), then the lease, the setting and the launches newest first. No row carries an age that
    moves with the clock, so a row's version changes only when the store does."""
    from levain.cockpit.coordinator_panels import _row

    rows = [_row("state", "Coordinator", f"{st['wake']}: {st['why']}"),
            _row("lease", "Lease", _lease_body(st["lease"])),
            _row("setting", "Setting", _setting_body(st["setting"]))]
    rows += [_row(f"launch:{ln['launch_id']}", f"Launch · {ln['state']}", _launch_body(ln)) for ln in st["launches"]]
    return tuple(rows)


def status_reader(directory: Callable[[], Path | str | None], clock: Callable[[], datetime] | None = None,
                  ) -> Callable[[ReadContext], Result]:
    """The panel's read callable: a store that cannot be read is its error row, never a raise."""
    from levain.cockpit.coordinator_panels import _store_at, _unreadable

    now_fn = clock or (lambda: datetime.now(timezone.utc))

    def read(ctx: ReadContext) -> Result:
        try:
            store = _store_at(directory)
            if store is None:
                return Read(rows=())
            rows = status_rows(_read_status(store, now_fn()))
        except Exception as e:
            return Read(rows=(_unreadable(e),))
        return Read(rows=rows, note=None)

    return read

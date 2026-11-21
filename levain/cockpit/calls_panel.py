"""levain.cockpit.calls_panel: the Calls panel (levain#221, agent-model ruling D4 (a), signed np-05034d15).

A READ-ONLY list over the desk's ``dispatch.db``: one row per SPAWNED WORKER, the summoned role ``levain coord spawn``
asks for (:func:`levain.agents.instantiate.spawn_worker`). A spawned worker is a ``launches`` row whose ``role`` is
``worker`` and whose ``role_ref`` holds its spawn record; a launch with no ``role_ref`` is a seat, not a call, and is not
here. In 1.0 this is all Calls is: raw model calls have no ledger a panel could read, and a panel over a store nobody has
named would read absence as health, so they are a later row that first names a source (D4).

The panel only draws what the launch and its events say; it decides nothing and offers no verb. The store is opened
``mode=ro`` and never created. A store that cannot be read is this panel's fault, said in words, never "no calls"; a
launch whose record cannot be read is a row that says so, never a hidden one. A desk store that lacks a table the panel
reads (``launches``, ``launch_events``) is a fault, never "no calls", except one made before ``role_ref`` existed, which
cannot hold a spawned worker. At most :data:`CALLS_SHOWN` calls are listed, the live ones first (a note says when there are
more).
"""
from __future__ import annotations

import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Any

from levain.cockpit.engine import ProviderSpec, ReadContext
from levain.cockpit.results import Absent, Fault, Read, Result, RowIn

__all__ = ["CALLS_ID", "CALLS_ORDER", "CALL_ROW_PREFIX", "CALLS_SHOWN", "calls_discoverer", "read_calls"]

CALLS_ID = "agent_calls"          # the id the Machine zone's styling and section head already name
CALL_ROW_PREFIX = "call:"
CALLS_ORDER = "calls.state"
CALL_FACETS = frozenset({"call_state"})
CALLS_SHOWN = 40
_LIVE_KINDS = ("pending", "active")                          # the desk's launch event kinds; the registry's enum adds "unknown"
_OVER_KINDS = ("failed", "expired", "ended")
_KNOWN_STATES = _LIVE_KINDS + _OVER_KINDS
_IN = lambda kinds: "(" + ", ".join(f"'{k}'" for k in kinds) + ")"   # noqa: E731 - constants of this module, never a caller's text

_SQL = ("SELECT launch_id, threads, at, role_ref, kind FROM (SELECT l.launch_id, l.threads, l.at, l.role_ref, l.gseq, "
        "(SELECT kind FROM launch_events e WHERE e.launch_id = l.launch_id ORDER BY e.seq DESC LIMIT 1) AS kind "
        "FROM launches l WHERE l.role = 'worker' AND l.role_ref IS NOT NULL) "
        f"ORDER BY CASE WHEN kind IN {_IN(_LIVE_KINDS)} THEN 0 WHEN kind IN {_IN(_OVER_KINDS)} THEN 2 ELSE 1 END, gseq DESC LIMIT ?")


def _one_line(text: object, limit: int = 300) -> str:
    line = " ".join(str(text or "").split())
    return line if len(line) <= limit else ("" if limit <= 0 else "…" if limit == 1 else line[:limit - 1].rstrip() + "…")


def _open(directory: Path) -> sqlite3.Connection | None:
    path = Path(directory) / "dispatch.db"
    if not path.is_file():
        return None
    return sqlite3.connect(path.absolute().as_uri() + "?mode=ro", uri=True, timeout=2.0, isolation_level=None)


def _role_name(role_ref: object) -> tuple[str, str]:
    """``(role name, "")`` of a launch's spawn record, or ``("", why)`` when the record cannot be read. The record is held to
    the check the worker's own start holds it to (:func:`levain.agents.instantiate.verified_record`: its role re-hashed), so
    a record that start would refuse is never drawn as a healthy name. Any failure is this ROW's, never the panel's."""
    from levain.agents.instantiate import verified_record

    try:
        return str(verified_record(role_ref)["role"]["name"]), ""
    except Exception as exc:  # noqa: BLE001 - a record that cannot be read (a RecursionError of a nested one too) is one row
        return "", _one_line(exc, 100) or type(exc).__name__


def _row(launch_id: str, threads: object, at: object, role_ref: object, kind: str | None) -> RowIn:
    state = kind if kind in _KNOWN_STATES else "unknown"       # a kind this panel does not know is never drawn as a state it is not
    name, why = _role_name(role_ref)
    shown = name or "unreadable spawn record"
    thread = _one_line(str(threads or "").strip("[]").replace('"', ""), 80)
    tail = f" · record unreadable: {why}" if why else ""
    body = _one_line(f"{shown} · {thread} · {state} · asked {at}", 300 - len(tail)) + tail
    return RowIn(id=CALL_ROW_PREFIX + launch_id, title=_one_line(shown, 120), body=body,
                 facets={"call_state": state},
                 stored={"launch_id": launch_id, "role": name, "threads": threads, "at": at, "state": state,
                         "unreadable": why})


def read_calls(directory: Path | None) -> tuple[tuple[RowIn, ...], str | None] | None:
    """The spawned-worker rows and a note, or None when there is no desk store to read. Writes nothing."""
    if directory is None:
        return None
    conn = _open(Path(directory))
    if conn is None:
        return None
    try:
        have = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        if "launches" not in have or "launch_events" not in have:
            raise sqlite3.DatabaseError("the desk store holds no launches or no launch events table")
        if "role_ref" not in {r[1] for r in conn.execute("PRAGMA table_info(launches)")}:
            return (), None            # made before spawned workers existed: it cannot hold one
        got = conn.execute(_SQL, (CALLS_SHOWN + 1,)).fetchall()
    finally:
        conn.close()
    older = len(got) > CALLS_SHOWN
    rows = tuple(_row(*r) for r in got[:CALLS_SHOWN])
    return rows, (f"Showing {CALLS_SHOWN} calls, the live ones first; the rest are in the desk's record." if older else None)


def calls_discoverer(dispatch_dir: Callable[[], Any] | None) -> Callable[[ReadContext], list[ProviderSpec]]:
    """The Calls panel for the install NOW (register with ``cockpit.discover(calls_discoverer(fn), retire=True)``):
    none while there is no desk directory or no ``dispatch.db`` in it, so an install with no desk draws no empty card."""
    def discover(ctx: ReadContext) -> list[ProviderSpec]:
        if dispatch_dir is None:
            return []
        try:
            directory = dispatch_dir()
            if directory is None or not (Path(directory) / "dispatch.db").is_file():
                return []
        except Exception:  # noqa: BLE001 - a directory that cannot be named is not "no desk": the panel stays and its read says so
            pass

        def calls_read(ctx: ReadContext) -> Result:
            try:
                where = dispatch_dir()
                got = read_calls(Path(where) if where is not None else None)
            except Exception as exc:  # noqa: BLE001 - an unreadable store is this panel's fault, never "no calls"
                return Fault(f"the desk store cannot be read: {type(exc).__name__}: {_one_line(exc)}")
            if got is None:
                return Absent("the desk store is gone")
            return Read(rows=got[0], note=got[1])

        return [ProviderSpec(CALLS_ID, "list", "Calls", "feed", calls_read, region="operate", rank=32,
                             order=CALLS_ORDER, version_fields=("*",), size="medium", span=1,
                             empty="No spawned worker yet.", facets=CALL_FACETS)]
    return discover

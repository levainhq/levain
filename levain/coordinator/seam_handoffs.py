"""Seam handoffs (C15 S1): the ``seam_handoffs`` table and :func:`close`, its ONE writer.

A handoff is what a coordinator leaves at a seam (a rotation, the operator leaving, a dream) so the next one starts
from a cut rather than a guess. The C13 pattern (``ready.py``): an append-only table, written by one lease-fenced
writer, with triggers that refuse the malformed shapes the writer refuses. The lease fence lives in :func:`close` only, as in C13: no trigger checks the lease, so a raw INSERT is not fenced. ``coord close`` (the CLI) is a later slice; this is
the Python API under it. The ``state_line`` trigger is a floor, not an equal: it refuses a line that is empty after
ASCII whitespace, and the writer refuses one that is empty after any Unicode whitespace.

* the cut: ``covers_gseq`` is the highest COUNTED ``gseq`` (``seam_classes``) read at the start of the close's own write
  transaction. A number read from the store, never a clock.
* the writer: derived from the live lease, never from request data. The lease holder must be an ACTIVE COORDINATOR
  LAUNCH's session key; the operator's lease (``coord take``) passes the fence but holds no session, so it is refused
  here: an operator has no handoff to write.
* idempotent: ``close_key`` is the caller's required request id. A retry with the same digest returns the stored
  handoff unchanged (it does not recompute the cut); the same id with another digest is refused.
* ordered: ``prev_handoff_id`` is the latest handoff the caller saw (None when it saw none). Anything else is a late
  close behind a newer handoff: refused "a newer handoff exists".

Stdlib only.
"""
from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass
from typing import Any

from levain.autonomic.durable_sqlite import read_transaction
from levain.contracts.canon import canonical
from levain.coordinator import seam_classes
from levain.coordinator.lease import LeaseRefused, lease as _lease, _launch_by_key
from levain.coordinator.store import CoordError, CoordStore, StoreAbsent, _append_only, next_gseq

__all__ = ["BODY_KEYS", "KINDS", "LIST_MAX", "MAX_BODY_BYTES", "SEAM_HANDOFF_SCHEMA", "SeamHandoff", "close",
           "latest"]

#: a handoff is written at these seams; ``light`` is an open with no handoff, never a row
KINDS = ("rotation", "operator", "dream")
#: the canonical body, in bytes, and the longest ``in_flight`` / ``contingencies`` list. A handoff is read whole at the
#: top of every seam: bound it rather than let it grow
MAX_BODY_BYTES = 16384
LIST_MAX = 50
#: every key is required; an empty list is ``[]``
BODY_KEYS = ("state_line", "in_flight", "next", "contingencies", "notes", "attested_operator_present",
             "attested_walked")
_LISTS = {"in_flight": ("item", "owner", "why"), "contingencies": ("if", "then")}
_REQUEST_ID_MAX = 200

_KINDS_SQL = ", ".join(repr(k) for k in KINDS)

SEAM_HANDOFF_SCHEMA: tuple[str, ...] = (
    "CREATE TABLE IF NOT EXISTS seam_handoffs (handoff_id INTEGER PRIMARY KEY AUTOINCREMENT, "
    "close_key TEXT NOT NULL UNIQUE, seam_kind TEXT NOT NULL, written_at TEXT NOT NULL, writer TEXT NOT NULL, "
    "gseq INTEGER NOT NULL UNIQUE, covers_gseq INTEGER NOT NULL CHECK (covers_gseq >= 0), prev_handoff_id INTEGER, "
    "request_digest TEXT NOT NULL, body TEXT NOT NULL)",
    *_append_only("seam_handoffs", "a seam handoff"),
    # INSERT OR REPLACE deletes the old row without firing a delete trigger: refuse a new row reusing any key
    "CREATE TRIGGER IF NOT EXISTS seam_handoffs_no_replace BEFORE INSERT ON seam_handoffs WHEN EXISTS (SELECT 1 FROM "
    "seam_handoffs WHERE close_key = NEW.close_key OR gseq = NEW.gseq OR handoff_id = NEW.handoff_id) "
    "BEGIN SELECT RAISE(ABORT, 'a seam handoff is never replaced'); END",
    f"CREATE TRIGGER IF NOT EXISTS seam_handoffs_kind BEFORE INSERT ON seam_handoffs WHEN NEW.seam_kind NOT IN "
    f"({_KINDS_SQL}) BEGIN SELECT RAISE(ABORT, 'a seam handoff is of kind rotation, operator or dream'); END",
    # CASE, not OR: SQLite does not promise to skip json_type on a body json_valid has refused. The trim set is ASCII
    # whitespace (space, tab, LF, VT, FF, CR): this trigger is the floor, and the writer's ``str.strip()`` is the full
    # check (it also strips NBSP, U+3000 and the rest of Unicode whitespace, which SQLite's trim cannot name)
    "CREATE TRIGGER IF NOT EXISTS seam_handoffs_state_line BEFORE INSERT ON seam_handoffs WHEN CASE WHEN "
    "json_valid(NEW.body) THEN COALESCE(json_type(NEW.body, '$.state_line'), '') != 'text' "
    "OR length(trim(json_extract(NEW.body, '$.state_line'), ' ' || char(9, 10, 11, 12, 13))) = 0 ELSE 1 END "
    "BEGIN SELECT RAISE(ABORT, 'a seam handoff needs a state_line'); END",
    f"CREATE TRIGGER IF NOT EXISTS seam_handoffs_size BEFORE INSERT ON seam_handoffs WHEN "
    f"length(CAST(NEW.body AS BLOB)) > {MAX_BODY_BYTES} OR CASE WHEN json_valid(NEW.body) THEN "
    f"COALESCE(json_array_length(NEW.body, '$.in_flight'), 0) > {LIST_MAX} "
    f"OR COALESCE(json_array_length(NEW.body, '$.contingencies'), 0) > {LIST_MAX} ELSE 0 END "
    "BEGIN SELECT RAISE(ABORT, 'a seam handoff body is over its size cap'); END",
    "CREATE TRIGGER IF NOT EXISTS seam_handoffs_prev BEFORE INSERT ON seam_handoffs WHEN NEW.prev_handoff_id IS NOT "
    "(SELECT MAX(handoff_id) FROM seam_handoffs) BEGIN SELECT RAISE(ABORT, 'a newer handoff exists'); END",
)

_COLUMNS = ("handoff_id, close_key, seam_kind, written_at, writer, gseq, covers_gseq, prev_handoff_id, "
            "request_digest, body")


@dataclass(frozen=True)
class SeamHandoff:
    handoff_id: int
    close_key: str
    seam_kind: str
    written_at: str
    writer: str
    gseq: int
    covers_gseq: int
    prev_handoff_id: int | None
    request_digest: str
    body: str            # the canonical JSON the digest and the caps are over


def _row(r: Any) -> SeamHandoff | None:
    return None if r is None else SeamHandoff(*r)


def latest(store: CoordStore) -> SeamHandoff | None:
    """The newest handoff, or None. A read on :meth:`CoordStore.open_readonly` (``mode=ro``, ``query_only``, no write
    lock, no lease): it makes and migrates nothing. None when there is no store (no directory or no file) and when the
    store predates S1 (no ``seam_handoffs`` table: a writer's open makes it). A store that is there but cannot be read
    (:class:`StoreAbsent` with something in the way) raises, never None."""
    from levain.coordinator.ready import _absent

    try:
        with read_transaction(store.open_readonly) as conn:
            if conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'seam_handoffs'").fetchone() \
                    is None:
                return None
            return _row(conn.execute(f"SELECT {_COLUMNS} FROM seam_handoffs ORDER BY handoff_id DESC LIMIT 1"
                                     ).fetchone())
    except StoreAbsent:
        if _absent(store):
            return None
        raise


def _text(name: str, value: Any, *, empty: bool) -> str:
    if not isinstance(value, str) or (not empty and not value.strip()):
        raise CoordError(f"{name} is {'text' if empty else 'non-empty text'}")
    return value


def _check_body(body: Any) -> dict:
    if not isinstance(body, dict):
        raise CoordError("a handoff body is a mapping")
    if not all(isinstance(k, str) for k in body):
        raise CoordError(f"a handoff body has exactly the keys {', '.join(BODY_KEYS)} (empty is []); every key is text")
    missing = [k for k in BODY_KEYS if k not in body]
    extra = sorted(set(body) - set(BODY_KEYS))
    if missing or extra:
        raise CoordError(f"a handoff body has exactly the keys {', '.join(BODY_KEYS)} (empty is []); "
                         f"missing: {', '.join(missing) or 'none'}; unknown: {', '.join(extra) or 'none'}")
    _text("state_line", body["state_line"], empty=False)
    _text("next", body["next"], empty=True)
    _text("notes", body["notes"], empty=True)
    for key in ("attested_operator_present", "attested_walked"):
        if not isinstance(body[key], bool):
            raise CoordError(f"{key} is true or false")
    for key, fields in _LISTS.items():
        items = body[key]
        if not isinstance(items, list):
            raise CoordError(f"{key} is a list (empty is [])")
        if len(items) > LIST_MAX:
            raise CoordError(f"{key} holds at most {LIST_MAX} entries, not {len(items)}")
        for entry in items:
            if not isinstance(entry, dict) or set(entry) != set(fields):
                raise CoordError(f"each {key} entry has exactly the keys {', '.join(fields)}")
            for f in fields:
                _text(f"{key}.{f}", entry[f], empty=False)
    try:
        size = len(canonical(body).encode("utf-8"))
    except UnicodeEncodeError:      # a lone surrogate (json.loads of "\ud800") has no UTF-8 form
        raise CoordError("a handoff body is text that encodes as UTF-8 (a lone surrogate does not)") from None
    if size > MAX_BODY_BYTES:
        raise CoordError(f"a handoff body is at most {MAX_BODY_BYTES} bytes, not {size}")
    return body


def _counted_head(conn: sqlite3.Connection) -> int:
    """The highest ``gseq`` over the COUNTED tables, and the COUNTED kinds of the two kind-classed tables; 0 when none."""
    present = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    top = 0
    for table in sorted(seam_classes.COUNTED & present):
        kinds = seam_classes.KIND_CLASSES.get(table, {}).get("COUNTED")
        if kinds is None:
            row = conn.execute(f"SELECT MAX(gseq) FROM {table}").fetchone()
        else:
            row = conn.execute(f"SELECT MAX(gseq) FROM {table} WHERE kind IN ({', '.join('?' * len(kinds))})",
                               kinds).fetchone()
        top = max(top, row[0] or 0)
    return top


def close(store: CoordStore, *, kind: Any, body: Any, request_id: Any, prev_handoff_id: int | None, token: Any,
          ) -> SeamHandoff:
    """Append one handoff, or return the stored one a retry names. Refused before anything is opened: an unknown kind,
    a body that is not every key with the right shapes and sizes, no request id, no token. Refused in the fenced
    transaction: a token that is not the live lease's, a lease that is not an active coordinator launch's (the
    operator's is not), the same request id with another digest, a ``prev_handoff_id`` that is not the latest."""
    if kind not in KINDS:
        raise CoordError(f"a seam handoff is of kind {', '.join(KINDS)}, not {kind!r} (a light open writes none)")
    body = _check_body(body)
    if not isinstance(request_id, str) or not request_id.strip() or len(request_id) > _REQUEST_ID_MAX \
            or not request_id.isprintable():
        raise CoordError("a close needs a request id (one printable line): a retry names the same one")
    if token is None or not isinstance(token, int) or isinstance(token, bool):
        raise CoordError("a close needs the coordinator's identity (the live lease token); nothing was written")
    if prev_handoff_id is not None and (not isinstance(prev_handoff_id, int) or isinstance(prev_handoff_id, bool)):
        raise CoordError("prev_handoff_id is the latest handoff's id, or None when there is none")
    canon_body = canonical(body)
    with store._write("coordinator", token) as (conn, now):
        held = _lease(conn, now)         # the fence has just passed it: the lease token is the live lease's
        writer = held.holder
        launch = None if writer == "operator" else _launch_by_key(conn, writer)
        if launch is None or launch.role != "coordinator" or launch.state != "active":
            raise LeaseRefused("a handoff is written by an active coordinator launch; the lease holder is "
                               f"{'the operator' if writer == 'operator' else writer}; nothing was written")
        digest = hashlib.sha256(canonical({"kind": kind, "body": body, "writer": writer}).encode("utf-8")).hexdigest()
        stored = _row(conn.execute(f"SELECT {_COLUMNS} FROM seam_handoffs WHERE close_key = ?",
                                   (request_id,)).fetchone())
        if stored is not None:
            if stored.request_digest != digest:
                raise CoordError(f"request id {request_id!r} already closed handoff {stored.handoff_id} by another "
                                 "writer or with other words; a changed close takes a new request id")
            return stored
        head = conn.execute("SELECT MAX(handoff_id) FROM seam_handoffs").fetchone()[0]
        if prev_handoff_id != head:
            raise CoordError(f"a newer handoff exists (latest is {head}, the close named {prev_handoff_id})")
        covers = _counted_head(conn)
        conn.execute(
            "INSERT INTO seam_handoffs (close_key, seam_kind, written_at, writer, gseq, covers_gseq, prev_handoff_id, "
            "request_digest, body) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (request_id, kind, now, writer, next_gseq(conn, now), covers, prev_handoff_id, digest, canon_body))
        return _row(conn.execute(f"SELECT {_COLUMNS} FROM seam_handoffs WHERE close_key = ?",
                                 (request_id,)).fetchone())

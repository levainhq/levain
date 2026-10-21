"""levain.qbroker.receipts — the broker's OWN receipts log, ``L/queue/receipts.db`` beside the rows (B3 ruling 10-19).

The table and its append-only triggers are taken from :mod:`levain.consent.queue` at import (never restated here),
so that :func:`levain.consent.receipt_log.head` and :func:`~levain.consent.receipt_log.head_refusal` read this log as
they read the consent queue's: leaves ``seq, request_id, state, created_at, face``, one receipt per transition the
broker makes, each face from ``ConsentQueue._face``. The transitions:

- ``pending`` for a NEW request row (:meth:`Receipts.filed`, which writes nothing for a request that already has a
  receipt, so it may be called again for a row whose first receipt write failed);
- ``allowed`` / ``denied`` on the first ANSWER? whose answer verifies, applied only when the answer's
  ``receipts_head`` is the head of its size over this log inside the write that records it (:meth:`Receipts.decided`).
  The first decision stands: a later answer row naming another decision is refused, never recorded;
- ``expired`` when an ANSWER? finds the hold ended with no decision recorded (:meth:`Receipts.expired`).

The database runs in rollback-journal mode, not WAL: the operator's cockpit reads the head from another uid with
read access only, and a WAL reader must write the ``-shm`` file. Stdlib only.
"""
from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from levain.consent import queue as consent_queue
from levain.consent.receipt_log import LogUnreadable, head, head_refusal
from levain.consent.record import ExternalGateRequest

__all__ = ["DB_NAME", "Receipts", "ReceiptsError"]

DB_NAME = "receipts.db"
_TABLE = consent_queue._receipts_table("receipts")
# the consent queue's triggers on its receipts table, taken from its schema by name: the broker's log must refuse
# an UPDATE or DELETE exactly as the consent queue's does
_TRIGGER_NAMES = ("receipts_frozen", "receipts_kept")
_TRIGGERS = tuple(stmt for stmt in consent_queue._SCHEMA
                  if any(stmt.startswith(f"CREATE TRIGGER IF NOT EXISTS {n} ") for n in _TRIGGER_NAMES))
if len(_TRIGGERS) != len(_TRIGGER_NAMES):
    raise ImportError("levain.consent.queue no longer holds the receipts triggers by these names: "
                      "the broker will not run a receipts log without them")
_DECIDED = tuple(consent_queue._STATE_OF.values())
_BUSY_MS = 30000


class ReceiptsError(Exception):
    """A receipt could not be written, or the log cannot be read."""


class Receipts:
    """The log at ``<queue root>/receipts.db``. ``create`` (the broker) makes the database and its schema;
    without it (the operator's reader) the file must exist and is opened read-only."""

    def __init__(self, root: Path | str, *, create: bool = True) -> None:
        self.path = Path(root) / DB_NAME
        self._create = create
        if create:
            with self._write() as conn:
                conn.execute(_TABLE)
                for stmt in _TRIGGERS:
                    conn.execute(stmt)
        elif not self.path.is_file() or self.path.is_symlink():
            raise FileNotFoundError(f"{self.path} is not the broker's receipts log")

    def _connect(self) -> sqlite3.Connection:
        if self._create:
            conn = sqlite3.connect(self.path, timeout=_BUSY_MS / 1000, isolation_level=None)
        else:
            conn = sqlite3.connect(f"{self.path.resolve().as_uri()}?mode=ro", uri=True, timeout=_BUSY_MS / 1000,
                                   isolation_level=None)
        try:
            conn.execute(f"PRAGMA busy_timeout = {_BUSY_MS}")
            if self._create:
                conn.execute("PRAGMA journal_mode = DELETE")
                conn.execute("PRAGMA synchronous = FULL")
                conn.execute("PRAGMA fullfsync = ON")
        except BaseException:
            conn.close()
            raise
        return conn

    @contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        if not self._create:
            raise ReceiptsError("the receipts log is open read-only")
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")
        finally:
            conn.close()

    @contextmanager
    def _read(self) -> Iterator[sqlite3.Connection]:
        conn = self._connect()
        try:
            conn.execute("BEGIN")
            try:
                yield conn
            finally:
                conn.execute("COMMIT")
        finally:
            conn.close()

    @staticmethod
    def _states(conn: sqlite3.Connection, request_id: str) -> list[str]:
        return [r[0] for r in conn.execute("SELECT state FROM receipts WHERE request_id = ? ORDER BY seq",
                                           (request_id,))]

    @staticmethod
    def _append(conn: sqlite3.Connection, request: ExternalGateRequest, state: str, stamp: str,
                signer_fp: str | None = None) -> None:
        face = consent_queue.ConsentQueue._face(request, state, signer_fp)
        conn.execute("INSERT INTO receipts (request_id, state, created_at, face) VALUES (?, ?, ?, ?)",
                     (request.id, state, stamp, json.dumps(face, ensure_ascii=False, sort_keys=True)))

    def head(self) -> dict[str, Any]:
        """The head of every receipt, in one read; :class:`ReceiptsError` when the log cannot be read as one."""
        try:
            with self._read() as conn:
                return head(conn)
        except (LogUnreadable, sqlite3.Error) as exc:
            raise ReceiptsError(f"the receipts cannot be read as a log ({exc})") from None

    def head_refusal(self, signed: Mapping[str, Any]) -> str | None:
        """Why the well-formed head ``signed`` is not a head of this log now, in one read; ``None`` when it is."""
        try:
            with self._read() as conn:
                return head_refusal(conn, signed, noun="answer")
        except sqlite3.Error as exc:
            return f"the receipts cannot be read ({exc})"

    def state(self, request_id: str) -> str | None:
        """The last state recorded for ``request_id``, ``None`` when it has no receipt."""
        with self._read() as conn:
            states = self._states(conn, request_id)
        return states[-1] if states else None

    def states(self) -> dict[str, str]:
        """Every request's last recorded state, in one read."""
        with self._read() as conn:
            return {rid: state for rid, state in conn.execute("SELECT request_id, state FROM receipts ORDER BY seq")}

    def filed(self, request: ExternalGateRequest, stamp: str) -> None:
        """The ``pending`` receipt of a linked row, unless the request already has a receipt."""
        with self._write() as conn:
            if not self._states(conn, request.id):
                self._append(conn, request, "pending", stamp)

    def decided(self, request: ExternalGateRequest, decision: str, signer_fp: str, signed: Mapping[str, Any],
                stamp: str) -> str | None:
        """Record ``decision`` for ``request`` (its answer already verified) when ``signed`` is the head of its size
        over this log inside this write; ``None`` when the decision is recorded (now or by an earlier answer naming
        the same decision), else why not. :class:`ReceiptsError` when the write itself fails."""
        state = consent_queue._STATE_OF.get(decision)
        if state is None:
            return f"decision {decision!r} is not one this log records"
        try:
            with self._write() as conn:
                states = self._states(conn, request.id)
                done = [s for s in states if s in _DECIDED]
                if done:
                    return None if done[0] == state else f"request {request.id} is already {done[0]}"
                if "expired" in states:
                    return f"request {request.id} is already expired"
                stale = head_refusal(conn, signed, noun="answer")
                if stale is not None:
                    return stale
                self._append(conn, request, state, stamp, signer_fp)
        except sqlite3.Error as exc:
            raise ReceiptsError(f"the decision could not be recorded ({exc})") from None
        return None

    def expired(self, request: ExternalGateRequest, stamp: str) -> None:
        """The ``expired`` receipt of a request whose hold ended with no decision recorded (once)."""
        with self._write() as conn:
            states = self._states(conn, request.id)
            if "expired" not in states and not any(s in _DECIDED for s in states):
                self._append(conn, request, "expired", stamp)

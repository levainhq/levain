"""levain.qbroker.receipts — the broker's OWN receipts log, ``L/queue/requests/receipts.db`` beside the rows (B3 ruling
10-19).

It lives in ``requests/`` because that is the one queue directory the broker owns (CONTRACT c3 §2.1:
``_levain_qbroker:levain-queue 0750``): ``L/queue`` itself is root's, and a rollback journal is a file sqlite creates
beside the database at every write. The operator reads it through the group. The database is made mode 0640, and
sqlite gives its journal the database's mode.

The table and its append-only triggers are taken from :mod:`levain.consent.queue` at import (never restated here),
so that :func:`levain.consent.receipt_log.head` and :func:`~levain.consent.receipt_log.head_refusal` read this log as
they read the consent queue's: leaves ``seq, request_id, state, created_at, face``, one receipt per transition the
broker makes, each face from ``ConsentQueue._face``. The transitions:

- ``pending`` for a NEW request row (:meth:`Receipts.filed`, which writes nothing for a request that already has a
  receipt, so it may be called again for a row whose first receipt write failed), and for every stored row with no
  receipt when the broker starts (:meth:`Receipts.reconcile`);
- ``allowed`` / ``denied`` on the first ANSWER? whose answer verifies, applied only when the answer's
  ``receipts_head`` is the head of its size over this log inside the write that records it, and that size reaches the
  request's ``pending`` receipt, so the answer attests the filing (:meth:`Receipts.decided`). The first decision
  stands: a later answer row naming another decision is refused, never recorded, and one naming the same decision is
  checked the same way;
- ``expired`` when an ANSWER? finds the hold ended with no decision recorded (:meth:`Receipts.expired`).

No transition is recorded for a request without its ``pending`` receipt: :meth:`Receipts.decided` and
:meth:`Receipts.expired` raise :class:`ReceiptsError` for one.

The database runs in rollback-journal mode, not WAL: the operator's cockpit reads the head from another uid with
read access only, and a WAL reader must write the ``-shm`` file. Stdlib only.
"""
from __future__ import annotations

import json
import os
import sqlite3
import stat
from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from levain.consent import queue as consent_queue
from levain.consent.receipt_log import LogUnreadable, head, head_refusal
from levain.consent.record import ExternalGateRequest

__all__ = ["DB_MODE", "DB_NAME", "Receipts", "ReceiptsError"]

DB_NAME = "receipts.db"
# the operator's cockpit reads it through levain-queue, the group of requests/
DB_MODE = 0o640
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
    """The log at ``<queue root>/requests/receipts.db``. ``create`` (the broker) makes the database and its schema;
    without it (the operator's reader) the file must exist and is opened read-only. Neither follows a symlink there."""

    def __init__(self, root: Path | str, *, create: bool = True) -> None:
        self.path = Path(root) / "requests" / DB_NAME
        self._create = create
        if create:
            self._make_file()
            with self._write() as conn:
                conn.execute(_TABLE)
                for stmt in _TRIGGERS:
                    conn.execute(stmt)
        elif not self.path.is_file() or self.path.is_symlink():
            raise FileNotFoundError(f"{self.path} is not the broker's receipts log")

    def _make_file(self) -> None:
        """The database file, a regular file of mode :data:`DB_MODE`, made here when absent: sqlite's own create
        would follow a symlink and take its mode from the umask."""
        try:
            fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, DB_MODE)
        except FileExistsError:
            pass
        else:
            try:
                os.fchmod(fd, DB_MODE)
                # Linux gives a new file its creator's primary group unless the directory is setgid; the operator
                # reads through the group of requests/, which the broker is a member of
                os.fchown(fd, -1, os.stat(self.path.parent).st_gid)
            finally:
                os.close(fd)
        try:
            st = os.lstat(self.path)
        except OSError as exc:
            raise ReceiptsError(f"{self.path} cannot be made ({exc})") from None
        if not stat.S_ISREG(st.st_mode):
            raise ReceiptsError(f"{self.path} is not a regular file (a symlink is refused)")

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
            yield conn
            # a read that raised ends with close(), which rolls it back: no COMMIT that could replace its error
            conn.execute("COMMIT")
        finally:
            conn.close()

    @staticmethod
    def _states(conn: sqlite3.Connection, request_id: str) -> list[str]:
        return [r[0] for r in conn.execute("SELECT state FROM receipts WHERE request_id = ? ORDER BY seq",
                                           (request_id,))]

    @staticmethod
    def _filing_seq(conn: sqlite3.Connection, request_id: str) -> int | None:
        row = conn.execute("SELECT seq FROM receipts WHERE request_id = ? AND state = 'pending'",
                           (request_id,)).fetchone()
        return None if row is None else row[0]

    @staticmethod
    def _attests(conn: sqlite3.Connection, seq: int, signed: Mapping[str, Any]) -> str | None:
        # a head is the log's first `size` receipts: one short of the filing's seq was signed before the filing
        if signed["size"] < seq:
            return (f"the answer's receipts head (size {signed['size']}) predates the request's filing receipt "
                    f"(receipt {seq})")
        return head_refusal(conn, signed, noun="answer")

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

    def head_refusal(self, signed: Mapping[str, Any], request_id: str | None = None) -> str | None:
        """Why the well-formed head ``signed`` is not a head of this log now, in one read; ``None`` when it is.
        With ``request_id``, the head must also reach that request's ``pending`` receipt, which must be there."""
        try:
            with self._read() as conn:
                if request_id is None:
                    return head_refusal(conn, signed, noun="answer")
                seq = self._filing_seq(conn, request_id)
                if seq is None:
                    return f"request {request_id} has no filing receipt"
                return self._attests(conn, seq, signed)
        except sqlite3.Error as exc:
            return f"the receipts cannot be read ({exc})"

    def state(self, request_id: str) -> str | None:
        """The last state recorded for ``request_id``, ``None`` when it has no receipt; :class:`ReceiptsError` when
        the log cannot be read."""
        try:
            with self._read() as conn:
                states = self._states(conn, request_id)
        except sqlite3.Error as exc:
            raise ReceiptsError(f"the receipts cannot be read ({exc})") from None
        return states[-1] if states else None

    def states(self) -> dict[str, str]:
        """Every request's last recorded state, in one read; :class:`ReceiptsError` when the log cannot be read."""
        try:
            with self._read() as conn:
                return {rid: state
                        for rid, state in conn.execute("SELECT request_id, state FROM receipts ORDER BY seq")}
        except sqlite3.Error as exc:
            raise ReceiptsError(f"the receipts cannot be read ({exc})") from None

    def filed(self, request: ExternalGateRequest, stamp: str) -> None:
        """The ``pending`` receipt of a linked row, unless the request already has a receipt."""
        with self._write() as conn:
            if not self._states(conn, request.id):
                self._append(conn, request, "pending", stamp)

    def reconcile(self, requests: Iterable[ExternalGateRequest], stamp: str) -> int:
        """The ``pending`` receipt of every request in ``requests`` (the stored rows) that has none, in one write;
        how many were written. The broker runs it before it listens: a row linked before a crash or an upgrade, whose
        filing receipt was never written, gets one before any answer for it is read."""
        written = 0
        with self._write() as conn:
            for request in sorted(requests, key=lambda r: (r.received_at, r.id)):
                if not self._states(conn, request.id):
                    self._append(conn, request, "pending", stamp)
                    written += 1
        return written

    def decided(self, request: ExternalGateRequest, decision: str, signer_fp: str, signed: Mapping[str, Any],
                stamp: str) -> str | None:
        """Record ``decision`` for ``request`` (its answer already verified) when ``signed`` is the head of its size
        over this log inside this write and reaches the request's ``pending`` receipt; ``None`` when the decision
        is recorded (now, or by an earlier answer naming the same decision and this head passes the same checks),
        else why not. :class:`ReceiptsError` when the request has no ``pending`` receipt or the write fails."""
        state = consent_queue._STATE_OF.get(decision)
        if state is None:
            return f"decision {decision!r} is not one this log records"
        try:
            with self._write() as conn:
                seq = self._filing_seq(conn, request.id)
                if seq is None:
                    raise ReceiptsError(f"request {request.id} has no filing receipt: no decision is recorded for it")
                states = self._states(conn, request.id)
                done = [s for s in states if s in _DECIDED]
                if done and done[0] != state:
                    return f"request {request.id} is already {done[0]}"
                if not done and "expired" in states:
                    return f"request {request.id} is already expired"
                stale = self._attests(conn, seq, signed)
                if stale is not None or done:
                    return stale
                self._append(conn, request, state, stamp, signer_fp)
        except sqlite3.Error as exc:
            raise ReceiptsError(f"the decision could not be recorded ({exc})") from None
        return None

    def expired(self, request: ExternalGateRequest, stamp: str) -> None:
        """The ``expired`` receipt of a request whose hold ended with no decision recorded (once);
        :class:`ReceiptsError` when the request has no ``pending`` receipt."""
        with self._write() as conn:
            states = self._states(conn, request.id)
            if "pending" not in states:
                raise ReceiptsError(f"request {request.id} has no filing receipt: its expiry is not recorded")
            if "expired" not in states and not any(s in _DECIDED for s in states):
                self._append(conn, request, "expired", stamp)

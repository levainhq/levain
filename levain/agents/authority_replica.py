"""The dispatch run server's authority REPLICA (levain 29, slice S10b-B1; design ``1104-28_S10B_INSTALL_DESIGN.md``
sections 2.2-2.5): a dispatch-owned copy of the operator's signed authority ledger, fed over the run socket and verified
by the server itself, so the server never reads the operator's home.

The replica is a CACHE of what the operator signed, never a second source of authority:

* ``<root>/cockpit.db`` holds only the ``authority_log`` table and ``<root>/anchor/authority.anchor`` is the same anchor
  file the cockpit keeps, so :class:`levain.agent_authority.AgentAuthority` replays it with no new code.
* :meth:`AuthorityReplica.append` takes rows that STRICTLY CONTINUE the replica's head, inserts them in one write
  transaction, runs the full verified replay (chain, hash links and every SSHSIG, through :func:`levain.authority.snapshot`)
  and the anchor check INSIDE that transaction, and commits only when all of it passed; only then does the anchor advance
  (``advance_anchor`` is called after the commit, so the anchor never leads the log). Any refusal rolls back: the replica
  is byte-for-byte what it was.
* It cannot go backward. Rows at or below the head are refused (never merged), a different row at a held seq is a fork,
  and the anchor, which never moves down, refuses a replica behind it (a restored older database).
* A forged ledger is a different store or genesis (the anchor names the first store this replica saw), so it is a fork.
  The first ledger a fresh replica is sent is accepted if it verifies (trust on first sync, design section 6 item 2).

One writer (the run server's SYNC) and one reader (the same process's replay), one uid. Appends are serialised by a
process lock and an ``flock`` on ``<root>/.append.lock``.
"""
from __future__ import annotations

import fcntl
import os
import sqlite3
import stat
import threading
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, Tuple

from levain import authority as _auth
from levain.agent_authority import AgentAuthority
from levain.cockpit.firejournal import DB_NAME

#: the most rows one SYNC may carry (the frame cap bounds the bytes; this bounds the work per call)
SYNC_MAX_ROWS = 1024
#: how long an append waits for the replica's own readers before it answers transient ``ledger-busy``
APPEND_BUSY_TIMEOUT_S = 5.0
LOCK_NAME = ".append.lock"

__all__ = ["APPEND_BUSY_TIMEOUT_S", "AuthorityReplica", "ReplicaRefused", "ReplicaRow", "SYNC_MAX_ROWS"]


class ReplicaRefused(Exception):
    """A SYNC the replica refuses; the replica is unchanged. ``reason_class`` is ``standing`` or ``transient``."""

    def __init__(self, name: str, detail: str = "", reason_class: str = "standing") -> None:
        super().__init__(f"{name}: {detail}" if detail else name)
        self.name, self.detail, self.reason_class = name, detail, reason_class


@dataclass(frozen=True)
class ReplicaRow:
    """One ``authority_log`` row as it travels: the six columns."""
    seq: int
    prev_hash: str
    record: str
    signer_fp: str
    signature: str
    hash: str


Head = Tuple[int, str]


class AuthorityReplica:
    """``AuthorityReplica(root)``: ``root`` is ``L/dispatch/authority``. Nothing is created until the first append."""

    def __init__(self, root: "Path | str", *, ssh_keygen: "Path | str | None" = None,
                 cache: Optional[_auth.ReplayCache] = None) -> None:
        self._root = Path(root)
        self._ssh_keygen = ssh_keygen
        self._cache = cache if cache is not None else _auth.ReplayCache()
        self._mu = threading.Lock()
        self._authority = AgentAuthority(self._root, ssh_keygen=ssh_keygen, cache=self._cache)

    @property
    def root(self) -> Path:
        return self._root

    @property
    def db_path(self) -> Path:
        return self._root / DB_NAME

    def authority(self) -> AgentAuthority:
        """The reader over this replica: ``AgentAuthority(root)``, the same code that replays the operator's ledger."""
        return self._authority

    def replay(self) -> _auth.AuthoritySnapshot:
        """The replica's verified, anchored replay. Raises on any failure (an empty or absent replica included)."""
        return self._authority.replay()

    def head(self) -> Head:
        """``(seq, hash)`` of the replica's last stored row, ``(0, "")`` when it holds none. A replica that exists but
        cannot be read raises (it is never read as empty)."""
        if not self.db_path.exists():
            return (0, "")
        conn = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True, isolation_level=None, timeout=2.0)
        try:
            got = _auth.log_head(conn)
        finally:
            conn.close()
        return (0, "") if got is None else (int(got[0]), str(got[1]))

    # -- append ----------------------------------------------------------------------------------------------------------

    def append(self, rows: Sequence[ReplicaRow]) -> Head:
        """Verify ``rows`` as a strict continuation of the head and commit them; return the new head. ``[]`` appends
        nothing and returns the head (and heals an anchor a crash left behind the log). Raises :class:`ReplicaRefused`
        with the replica unchanged."""
        rows = list(rows)
        if len(rows) > SYNC_MAX_ROWS:
            raise ReplicaRefused("sync-too-large", f"at most {SYNC_MAX_ROWS} rows per SYNC")
        for r in rows:
            if type(r) is not ReplicaRow or type(r.seq) is not int or not all(
                    type(v) is str for v in (r.prev_hash, r.record, r.signer_fp, r.signature, r.hash)):
                raise ReplicaRefused("bad-row", "a row is not the six authority_log columns")
        with self._mu:
            self._make_root()
            lock_fd = os.open(self._root / LOCK_NAME, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX)
                return self._append_locked(rows)
            finally:
                os.close(lock_fd)

    def _make_root(self) -> None:
        try:
            self._root.mkdir(mode=0o700, parents=True, exist_ok=True)
            st = os.lstat(self._root)
        except OSError as exc:
            raise ReplicaRefused("replica-unwritable", f"{type(exc).__name__}", "transient") from None
        if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode) or st.st_uid != os.geteuid():
            raise ReplicaRefused("replica-not-ours", str(self._root))

    def _append_locked(self, rows: "list[ReplicaRow]") -> Head:
        path = _auth.anchor_path(self._root)
        fresh = not self.db_path.exists()
        try:
            conn = sqlite3.connect(self.db_path, isolation_level=None, timeout=APPEND_BUSY_TIMEOUT_S)
        except sqlite3.Error as exc:
            raise ReplicaRefused("replica-unwritable", type(exc).__name__, "transient") from None
        try:
            if fresh:
                os.chmod(self.db_path, 0o600)
            return self._txn(conn, path, rows)
        finally:
            conn.close()

    def _txn(self, conn: sqlite3.Connection, path: Path, rows: "list[ReplicaRow]") -> Head:
        try:
            _auth.create_schema(conn)
            conn.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError:
            raise ReplicaRefused("ledger-busy", "the replica is busy", "transient") from None
        except sqlite3.Error as exc:
            raise ReplicaRefused("replica-unreadable", type(exc).__name__, "transient") from None
        done = False
        try:
            try:
                got = _auth.log_head(conn)
                head: Head = (0, "") if got is None else (int(got[0]), str(got[1]))
                self._continues(conn, head, rows)
                for r in rows:
                    conn.execute("INSERT INTO authority_log (seq, prev_hash, record, signer_fp, signature, hash) "
                                 "VALUES (?, ?, ?, ?, ?, ?)", (r.seq, r.prev_hash, r.record, r.signer_fp, r.signature,
                                                               r.hash))
                if head == (0, "") and not rows:
                    raise ReplicaRefused("ledger-empty", "the replica holds no ledger yet and none was sent",
                                         "transient")
                state = self._verify(conn, path)
                if (state.seq, state.head_hash) != ((rows[-1].seq, rows[-1].hash) if rows else head):
                    raise ReplicaRefused("ledger-unverifiable", "the replay's head is not the stored head")
                conn.execute("COMMIT")      # only after the verify and the anchor check passed
                done = True
            finally:
                if not done:
                    try:
                        conn.execute("ROLLBACK")
                    except sqlite3.Error:
                        pass
        except sqlite3.IntegrityError:
            raise ReplicaRefused("ledger-fork", "a row collides with one the replica holds") from None
        except sqlite3.Error as exc:
            raise ReplicaRefused("replica-unreadable", type(exc).__name__, "transient") from None
        try:
            _auth.advance_anchor(path, state)
        except _auth.AuthorityError as exc:
            raise ReplicaRefused("ledger-fork", f"the anchor refuses the replica ({type(exc).__name__})") from None
        except OSError as exc:
            raise ReplicaRefused("ledger-anchor-unwritten", type(exc).__name__, "transient") from None
        return (state.seq, state.head_hash)

    @staticmethod
    def _continues(conn: sqlite3.Connection, head: Head, rows: "list[ReplicaRow]") -> None:
        """``rows`` must be the rows right after ``head``, linked to each other. A row at or below the head is never
        merged: a different one is a fork, the same one a stale SYNC (the client re-reads the head)."""
        if not rows:
            return
        first = rows[0]
        if first.seq <= head[0]:
            held = conn.execute("SELECT prev_hash, record, signer_fp, signature, hash FROM authority_log WHERE seq = ?",
                                (first.seq,)).fetchone()
            if held is not None and tuple(held) == (first.prev_hash, first.record, first.signer_fp, first.signature,
                                                    first.hash):
                raise ReplicaRefused("ledger-stale", f"the replica already holds seq {first.seq} (head {head[0]})",
                                     "transient")
            raise ReplicaRefused("ledger-fork", f"seq {first.seq} differs from the row the replica holds")
        if first.seq != head[0] + 1:
            raise ReplicaRefused("ledger-gap", f"the replica's head is {head[0]}; the rows begin at {first.seq}",
                                 "transient")
        prev = head[1]
        for i, r in enumerate(rows):
            if r.seq != first.seq + i:
                raise ReplicaRefused("ledger-gap", "the rows are not consecutive", "transient")
            if r.prev_hash != prev:
                raise ReplicaRefused("ledger-fork", f"seq {r.seq} does not link to the replica's chain")
            prev = r.hash

    def _verify(self, conn: sqlite3.Connection, path: Path) -> _auth.AuthoritySnapshot:
        """The full verified replay of the (uncommitted) log in ``conn`` plus the anchor check."""
        kw: dict[str, Any] = {} if self._ssh_keygen is None else {"ssh_keygen": self._ssh_keygen}
        try:
            state = _auth.snapshot(conn, cache=self._cache, **kw)
        except _auth.HeadBelowCache as exc:
            raise ReplicaRefused("ledger-fork", f"{exc}"[:300]) from None
        except _auth.AuthorityError as exc:
            raise ReplicaRefused("ledger-unverifiable", f"{type(exc).__name__}: {exc}"[:300]) from None
        except OSError as exc:
            raise ReplicaRefused("ledger-verifier-unavailable", type(exc).__name__, "transient") from None
        try:
            _auth.check_anchor(state, _auth.read_anchor(path), path=path)
        except _auth.AuthorityError as exc:
            raise ReplicaRefused("ledger-fork", f"the anchor refuses this ledger ({type(exc).__name__})") from None
        return state

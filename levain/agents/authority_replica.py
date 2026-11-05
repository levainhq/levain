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

The root and its anchor directory are created private (0700), the database 0600 from inception, and every file the
replica keeps there (the database, SQLite's side files, the locks, the anchor) must be this uid's own private, regular,
single-link file, never a symlink, before anything is read or written (L3 r1, codex). A loose pre-existing object is
REFUSED (``replica-not-private``), never repaired: a chmod does not revoke a descriptor another uid already holds (L3 r2).
The checked descriptors were closed before SQLite and the anchor code reopened the same paths by name; that window was
left as a known edge (L3 r2), since a swap needs write access to the root's parent, which is root-owned in production.

An anchor a failed post-COMMIT write left behind the log is advanced by the next :meth:`replay` before it authorises
anything, and the replay refuses transient (``ledger-anchor-lagging``) while it cannot be (L3 r2): a lag never stands.

The verify inside :meth:`append` shares the replica's ``ReplayCache`` with :meth:`replay`. A chain that verified inside a
transaction that then rolled back stays cached, and that is safe: the cache serves a reader only the states of the rows
that reader's own snapshot holds (a digest-matching prefix), so a reader never sees an uncommitted row (L3 r1 #1, refuted
and pinned by ``test_a_replay_during_an_uncommitted_sync_reads_the_committed_head`` and the commit-failure test).
"""
from __future__ import annotations

import errno
import fcntl
import logging
import os
import sqlite3
import stat
import threading
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, Tuple
from urllib.parse import quote

from levain import authority as _auth
from levain.agent_authority import AgentAuthority
from levain.cockpit.firejournal import DB_NAME

#: the most rows one SYNC may carry (the frame cap bounds the bytes; this bounds the work per call)
SYNC_MAX_ROWS = 1024
#: how long an append waits for the replica's own readers before it answers transient ``ledger-busy``
APPEND_BUSY_TIMEOUT_S = 5.0
LOCK_NAME = ".append.lock"
#: what the replica keeps in its root, and in ``<root>/anchor``
_ROOT_FILES = (DB_NAME, DB_NAME + "-journal", DB_NAME + "-wal", DB_NAME + "-shm", LOCK_NAME)
_ANCHOR_FILES = (_auth.ANCHOR_NAME, _auth.ANCHOR_NAME + ".lock")

_log = logging.getLogger("levain.agents.authority_replica")

__all__ = ["APPEND_BUSY_TIMEOUT_S", "AuthorityReplica", "ReplicaRefused", "ReplicaRow", "SYNC_MAX_ROWS"]


class ReplicaRefused(Exception):
    """A SYNC the replica refuses; its log is unchanged (a ``ledger-stale`` resend may first advance an anchor that lags
    the held head, never past it), except ``ledger-anchor-conflict`` from an append, whose detail says the rows ARE
    committed. ``reason_class`` is ``standing`` or ``transient``."""

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
        self._private(create=False)
        state = self._authority.replay()
        path = _auth.anchor_path(self._root)
        try:
            anchor = _auth.read_anchor(path)
            if anchor is None or anchor.seq < state.seq:     # a lag a failed post-COMMIT anchor write left (L3 r2)
                _auth.advance_anchor(path, state)
        except OSError as exc:
            raise ReplicaRefused("ledger-anchor-lagging", f"the anchor is behind the log and cannot be advanced "
                                                          f"({type(exc).__name__})", "transient") from None
        except _auth.AuthorityError as exc:
            raise ReplicaRefused("ledger-anchor-conflict", f"the anchor conflicts with the log "
                                                           f"({type(exc).__name__})") from None
        return state

    def head(self) -> Head:
        """``(seq, hash)`` of the replica's last stored row, ``(0, "")`` when it holds none. A replica that exists but
        cannot be read raises (it is never read as empty)."""
        if not self._private(create=False) or not self.db_path.exists():
            return (0, "")
        conn = sqlite3.connect(f"file:{quote(str(self.db_path))}?mode=ro", uri=True, isolation_level=None,
                               timeout=2.0)
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
            self._private(create=True)
            try:
                lock_fd = os.open(self._root / LOCK_NAME, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
            except OSError as exc:
                raise ReplicaRefused("replica-unwritable", type(exc).__name__, "transient") from None
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX)
                return self._append_locked(rows)
            finally:
                os.close(lock_fd)

    def _private(self, *, create: bool) -> bool:
        """Check the root (and its anchor directory) are this uid's private directories and every file the replica
        keeps there is private; False when the root does not exist and ``create`` is off. Refuses ``replica-not-ours``
        and ``replica-not-private``."""
        if create:
            try:
                self._root.mkdir(mode=0o700, parents=True, exist_ok=True)
            except OSError as exc:
                raise ReplicaRefused("replica-unwritable", type(exc).__name__, "transient") from None
        root = self._open(None, str(self._root), directory=True)
        if root is None:
            if create:
                raise ReplicaRefused("replica-unwritable", "the root vanished", "transient")
            return False
        try:
            for name in _ROOT_FILES:
                self._close(self._open(root, name, directory=False))
            anchor = self._open(root, _auth.ANCHOR_DIR, directory=True)
            if anchor is not None:
                try:
                    for name in _ANCHOR_FILES:
                        self._close(self._open(anchor, name, directory=False))
                finally:
                    os.close(anchor)
        finally:
            os.close(root)
        return True

    @staticmethod
    def _close(fd: Optional[int]) -> None:
        if fd is not None:
            os.close(fd)

    def _open(self, parent: Optional[int], name: str, *, directory: bool) -> Optional[int]:
        """An fd on ``name`` (relative to ``parent``), never through a symlink, checked to be this uid's own directory
        or regular single-link file, with no group or other bits; None when it does not exist."""
        flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | (os.O_DIRECTORY if directory else os.O_NONBLOCK)
        try:
            fd = os.open(name, flags, dir_fd=parent)
        except FileNotFoundError:
            return None
        except OSError as exc:
            if exc.errno in (errno.ELOOP, errno.ENOTDIR, errno.EMLINK):
                raise ReplicaRefused("replica-not-ours", f"{name} is a symlink or not a {'directory' if directory else 'file'}") from None
            raise ReplicaRefused("replica-unreadable", type(exc).__name__, "transient") from None
        try:
            st = os.fstat(fd)
            kind_ok = stat.S_ISDIR(st.st_mode) if directory else (stat.S_ISREG(st.st_mode) and st.st_nlink == 1)
            if not kind_ok or st.st_uid != os.geteuid():
                raise ReplicaRefused("replica-not-ours", f"{name} is not this uid's own private "
                                                         f"{'directory' if directory else 'file'}")
            if st.st_mode & 0o077:      # refused, never repaired: a chmod revokes no descriptor already open (L3 r2)
                raise ReplicaRefused("replica-not-private", f"{name} is open to other users (mode "
                                                            f"{stat.S_IMODE(st.st_mode):o})")
        except ReplicaRefused:
            os.close(fd)
            raise
        except OSError as exc:
            os.close(fd)
            raise ReplicaRefused("replica-unreadable", type(exc).__name__, "transient") from None
        return fd

    def _append_locked(self, rows: "list[ReplicaRow]") -> Head:
        path = _auth.anchor_path(self._root)
        try:        # private from inception: SQLite gives its side files the database's mode
            os.close(os.open(self.db_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600))
        except FileExistsError:
            pass
        except OSError as exc:
            raise ReplicaRefused("replica-unwritable", type(exc).__name__, "transient") from None
        try:
            conn = sqlite3.connect(self.db_path, isolation_level=None, timeout=APPEND_BUSY_TIMEOUT_S)
        except sqlite3.Error as exc:
            raise ReplicaRefused("replica-unwritable", type(exc).__name__, "transient") from None
        try:
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
        stale: Optional[ReplicaRefused] = None
        try:
            try:
                got = _auth.log_head(conn)
                head: Head = (0, "") if got is None else (int(got[0]), str(got[1]))
                try:
                    self._continues(conn, head, rows)
                except ReplicaRefused as exc:
                    if exc.name != "ledger-stale":
                        raise
                    # a resend of rows the replica holds (a retry after a commit whose anchor advance failed): verify
                    # the held log and advance the anchor as an empty SYNC does, then answer stale (codex r1 #4)
                    stale, rows = exc, []
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
        except OSError as exc:
            # an unwritten anchor only: the rows are committed, so this is never "unchanged"; the head is answered, the
            # anchor lags (it never leads), and the next replay or SYNC advances it (codex r1 #4, L3 r2 #3)
            _log.warning("replica committed at seq %d; the anchor is behind (%s)", state.seq, type(exc).__name__)
        except _auth.AuthorityError as exc:
            # another writer's anchor (foreign, or conflicting) landed after the pre-COMMIT check: never a success, and
            # never "unchanged" either (L3 r2 #1)
            raise ReplicaRefused("ledger-anchor-conflict", f"the rows ARE committed (head seq {state.seq}) but the "
                                                           f"anchor conflicts with them ({type(exc).__name__})") from None
        if stale is not None:
            raise stale
        return (state.seq, state.head_hash)

    @staticmethod
    def _continues(conn: sqlite3.Connection, head: Head, rows: "list[ReplicaRow]") -> None:
        """``rows`` must be the rows right after ``head``, linked to each other. A row at or below the head is never
        merged: a different one is a fork, the same one a stale SYNC (the client re-reads the head). EVERY supplied row
        at a held seq is compared, so a held first row never masks a later one that differs (L3 r2 #2); a batch is
        stale only when its held rows are all identical and the whole batch links as one chain."""
        if not rows:
            return
        for r in rows:
            if r.seq > head[0]:
                continue
            held = conn.execute("SELECT prev_hash, record, signer_fp, signature, hash FROM authority_log WHERE seq = ?",
                                (r.seq,)).fetchone()
            if held is None or tuple(held) != (r.prev_hash, r.record, r.signer_fp, r.signature, r.hash):
                raise ReplicaRefused("ledger-fork", f"seq {r.seq} differs from the row the replica holds")
        first = rows[0]
        if first.seq > head[0] + 1:
            raise ReplicaRefused("ledger-gap", f"the replica's head is {head[0]}; the rows begin at {first.seq}",
                                 "transient")
        prev = head[1] if first.seq == head[0] + 1 else first.prev_hash     # a held first row was compared above
        for i, r in enumerate(rows):
            if r.seq != first.seq + i:
                raise ReplicaRefused("ledger-gap", "the rows are not consecutive", "transient")
            if r.prev_hash != prev:
                raise ReplicaRefused("ledger-fork", f"seq {r.seq} does not link to the replica's chain")
            prev = r.hash
        if first.seq <= head[0]:
            raise ReplicaRefused("ledger-stale", f"the replica already holds seq {first.seq} (head {head[0]})",
                                 "transient")

    def _verify(self, conn: sqlite3.Connection, path: Path) -> _auth.AuthoritySnapshot:
        """The full verified replay of the (uncommitted) log in ``conn`` plus the anchor check."""
        kw: dict[str, Any] = {} if self._ssh_keygen is None else {"ssh_keygen": self._ssh_keygen}
        try:
            state = _auth.snapshot(conn, cache=self._cache, **kw)
        except _auth.VerifierUnavailable as exc:      # an AuthorityError, but never a verdict on the log (codex r1 #5)
            raise ReplicaRefused("ledger-verifier-unavailable", f"{exc}"[:300], "transient") from None
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

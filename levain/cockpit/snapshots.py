"""The cockpit snapshot store: one sqlite file the derive worker writes and the serve reads.

The worker derives each panel and publishes the result here; the serve (and any other reader, in any language) answers
from the stored snapshot instead of deriving on the request path. The file is `snapshots.db` in the cockpit data root,
WAL mode, one writer and many read-only readers. The language-neutral contract (tables, columns, the gen rule, the body
encoding, the contract string, the freshness fields a reader derives) is docs/cockpit_snapshot_contract.md; this module
is the Python implementation of it. Stdlib only.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import sqlite3
import sys
import threading
import time
import urllib.parse
import weakref
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Callable

CONTRACT_NAME = "levain-cockpit-snapshots"
CONTRACT_MAJOR = 1
CONTRACT = f"{CONTRACT_NAME}/{CONTRACT_MAJOR}"
DB_FILENAME = "snapshots.db"
# A publish with one of these statuses never replaces the stored body (a first-ever one stores no body at all).
KEEP_BODY_STATUSES = ("error", "timeout")
# The statuses this code knows; a reader treats any other stored value as "partial".
KNOWN_STATUSES = ("ok", "empty", "partial", "error", "timeout")
# body_sha of a panel row that has no body (a first-ever error or timeout publish).
NO_BODY_SHA = ""
_OURS = ("meta", "panel", "stream")
LOCK_SUFFIX = ".lock"
DECODED_CACHE_MAX = 256
# A reader's thread connection is checked against the file on disk (device and inode) at most this often.
READER_REVALIDATE_S = 1.0
# The wal file is truncated back to this size after a checkpoint (sqlite's journal_size_limit, bytes).
JOURNAL_SIZE_LIMIT = 4 * 1024 * 1024

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS panel (
    id TEXT PRIMARY KEY,
    gen INTEGER NOT NULL,
    derived_at REAL NOT NULL,
    started_at REAL NOT NULL,
    duration_ms REAL NOT NULL,
    valid_until REAL,
    inputs_fp TEXT NOT NULL,
    write_gen INTEGER NOT NULL,
    status TEXT NOT NULL,
    error TEXT,
    body_sha TEXT NOT NULL,
    body BLOB
);
CREATE TABLE IF NOT EXISTS stream (
    id TEXT PRIMARY KEY,
    state TEXT NOT NULL,
    last_event_at REAL,
    detail TEXT NOT NULL DEFAULT ''
);
"""

_PANEL_COLS = (
    "id, gen, derived_at, started_at, duration_ms, valid_until, inputs_fp, write_gen, status, error, body_sha, body"
)


class ContractMismatch(Exception):
    """The store carries a contract major this code does not speak."""


class WriterLocked(Exception):
    """Another process holds the snapshot store's writer lock."""


class SnapshotReadError(Exception):
    """A read failed for a reason other than "the worker has not run" (busy, locked, I/O)."""


def encode_body(obj: Any) -> bytes:
    """Canonical JSON bytes: equal values give equal bytes, so equal `body_sha`.

    Keys sorted, no whitespace, every non-ASCII character escaped (so a lone surrogate encodes instead of raising and
    the bytes are plain ASCII), and NaN/Infinity refused with ValueError (they are not JSON).
    """
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode("ascii")


def _sha(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def _check_contract(value: str | None) -> None:
    """Raise ContractMismatch unless `value` names this code's contract and major. None (no contract row) passes."""
    if value is None:
        return
    name, _, major_text = value.rpartition("/")
    try:
        major = int(major_text)
    except ValueError:
        raise ContractMismatch(f"unreadable snapshot contract {value!r}") from None
    if name != CONTRACT_NAME:
        raise ContractMismatch(f"snapshot contract {value!r} is not {CONTRACT_NAME!r}")
    if major != CONTRACT_MAJOR:
        raise ContractMismatch(f"snapshot contract {value!r}: this code speaks major {CONTRACT_MAJOR}")


@dataclass(frozen=True)
class PanelSnap:
    id: str
    gen: int
    derived_at: float
    started_at: float
    duration_ms: float
    valid_until: float | None
    inputs_fp: str
    write_gen: int
    status: str
    error: str | None
    body_sha: str
    body: bytes | None  # None: no good body was ever stored (see the contract)


def _snap(row: tuple) -> PanelSnap:
    raw = row[11]
    body = None if raw is None else raw.encode("utf-8") if isinstance(raw, str) else bytes(raw)
    return PanelSnap(
        id=row[0], gen=row[1], derived_at=row[2], started_at=row[3], duration_ms=row[4], valid_until=row[5],
        inputs_fp=row[6], write_gen=row[7], status=row[8] if row[8] in KNOWN_STATUSES else "partial", error=row[9],
        body_sha=row[10], body=body,
    )


def _close_fd_box(box: list) -> None:
    """Close the descriptor in `box` once (the box outlives the writer object, so a finalizer can hold it)."""
    fd, box[0] = box[0], None
    if fd is not None:
        try:
            os.close(fd)  # closing the descriptor drops the flock
        except OSError:
            pass


def _preflight(path: str) -> tuple[str | None, set[str]]:
    """(stored contract value or None, table names) of the file at `path`, read without writing anything."""
    if not os.path.exists(path):
        return None, set()
    uri = _uri(path)
    if not os.path.exists(path + "-wal"):
        # No log to read, so the main file is the whole database; a plain read-only open of a WAL file would still
        # create the -wal/-shm.
        uri = uri.replace("?mode=ro", "?immutable=1")
    db = sqlite3.connect(uri, uri=True)
    try:
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        row = db.execute("SELECT value FROM meta WHERE key='contract'").fetchone() if "meta" in tables else None
        return (row[0] if row else None), tables
    finally:
        db.close()


class SnapshotWriter:
    """The one writer. Used from one thread at a time; the caller serialises its publishes.

    Holds an exclusive lock on `<path>.lock` for its lifetime (WriterLocked when another process has it).
    """

    def __init__(self, path: str | os.PathLike, *, pid: int | None = None, now: float | None = None) -> None:
        self.path = os.fspath(path)
        fd = os.open(self.path + LOCK_SUFFIX, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            raise WriterLocked(f"another process holds {self.path + LOCK_SUFFIX}") from None
        self._fd_box = [fd]
        # A writer dropped without close() still gives the lock back.
        self._fd_finalizer = weakref.finalize(self, _close_fd_box, self._fd_box)
        self._db: sqlite3.Connection | None = None
        try:
            # The contract is checked on a read-only connection before this one exists: a refusal leaves the file (and
            # its -wal/-shm) as it was found.
            stored, tables = _preflight(self.path)
            _check_contract(stored)
            if stored is None and tables - set(_OURS):
                raise ContractMismatch(f"{self.path} is a database with other tables and no snapshot contract")
            self._db = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
            mode = self._db.execute("PRAGMA journal_mode=WAL").fetchone()
            if mode is None or str(mode[0]).lower() != "wal":
                raise sqlite3.OperationalError(f"journal_mode stayed {mode[0] if mode else None!r}, not wal")
            self._db.execute("PRAGMA synchronous=NORMAL")
            self._db.execute(f"PRAGMA journal_size_limit={int(JOURNAL_SIZE_LIMIT)}")
            self._rebuild_if_drifted(tables)
            self._db.executescript(_SCHEMA)
            started = time.time() if now is None else now
            with self._tx():
                self._set_meta("contract", CONTRACT)
                self._set_meta("worker_pid", str(os.getpid() if pid is None else pid))
                self._set_meta("worker_started_at", repr(float(started)))
                self._db.execute("DELETE FROM meta WHERE key='heartbeat_at'")  # not live until its own first heartbeat
                self._db.execute("INSERT OR IGNORE INTO meta(key, value) VALUES ('publish_seq', '0')")
        except BaseException:
            self.close()
            raise

    def _tx(self):
        return _Tx(self._db)

    def _rebuild_if_drifted(self, tables: set[str]) -> None:
        """The file is a cache of derivable data: a table of ours that differs from the current schema is dropped.

        All three go in one transaction and are recreated by the schema that follows. Only a file already carrying
        this contract major gets here (another major was refused above).
        """
        want = sqlite3.connect(":memory:")
        try:
            want.executescript(_SCHEMA)
            drifted = [
                t for t in _OURS
                if t in tables
                and self._db.execute(f"PRAGMA table_info({t})").fetchall() != want.execute(f"PRAGMA table_info({t})").fetchall()
            ]
        finally:
            want.close()
        if not drifted:
            return
        print(f"cockpit snapshots: {self.path}: {', '.join(drifted)} differ from the current schema; rebuilding the cache",
              file=sys.stderr)
        with self._tx():
            for t in _OURS:
                self._db.execute(f"DROP TABLE IF EXISTS {t}")
            for stmt in _SCHEMA.split(";"):
                if stmt.strip():
                    self._db.execute(stmt)

    def _set_meta(self, key: str, value: str) -> None:
        self._db.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)", (key, value))

    def publish(
        self,
        panel_id: str,
        *,
        status: str,
        body: bytes | None,
        started_at: float,
        derived_at: float,
        duration_ms: float,
        valid_until: float | None = None,
        inputs_fp: str = "",
        write_gen: int = 0,
        error: str | None = None,
    ) -> int:
        """Store one panel's snapshot in one transaction and return its gen.

        An identical body and status keep gen (only the timing fields move, the body is not rewritten); a changed body
        or status bumps gen, and meta `publish_seq` goes up by one in the same transaction. `body=None` keeps the
        stored body and changes only the other columns (a panel with no stored row then gets the body `null`). A status
        in KEEP_BODY_STATUSES ignores `body` altogether and keeps the stored one; with no stored row it stores no body.
        """
        keep = status in KEEP_BODY_STATUSES
        if keep:
            body = None
        with self._tx():
            row = self._db.execute(
                "SELECT gen, body_sha, status, write_gen FROM panel WHERE id=?", (panel_id,)
            ).fetchone()
            if body is None:
                if row is not None:
                    sha = row[1]
                elif keep:
                    sha = NO_BODY_SHA
                else:
                    body = encode_body(None)
                    sha = _sha(body)
            else:
                sha = _sha(body)
            if row is None:
                gen, wg, changed = 1, write_gen, True
            else:
                changed = not (row[1] == sha and row[2] == status)
                gen = row[0] + 1 if changed else row[0]
                wg = max(row[3], write_gen)
            if row is not None and row[1] == sha:
                self._db.execute(
                    "UPDATE panel SET gen=?, derived_at=?, started_at=?, duration_ms=?, valid_until=?, inputs_fp=?,"
                    " write_gen=?, status=?, error=? WHERE id=?",
                    (gen, derived_at, started_at, duration_ms, valid_until, inputs_fp, wg, status, error, panel_id),
                )
            else:
                self._db.execute(
                    f"INSERT OR REPLACE INTO panel({_PANEL_COLS}) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (panel_id, gen, derived_at, started_at, duration_ms, valid_until, inputs_fp, wg, status, error,
                     sha, body),
                )
            if changed:
                self._db.execute(
                    "INSERT INTO meta(key, value) VALUES ('publish_seq', '1')"
                    " ON CONFLICT(key) DO UPDATE SET value=CAST(CAST(value AS INTEGER) + 1 AS TEXT)"
                )
        return gen

    def drop(self, panel_id: str) -> bool:
        """Delete one panel's row; True when there was one. The panel set changed, so ``publish_seq`` goes up in the same
        transaction: a reader's change counter moves for a removal as it does for a publish."""
        with self._tx():
            gone = self._db.execute("DELETE FROM panel WHERE id=?", (panel_id,)).rowcount > 0
            if gone:
                self._db.execute(
                    "INSERT INTO meta(key, value) VALUES ('publish_seq', '1')"
                    " ON CONFLICT(key) DO UPDATE SET value=CAST(CAST(value AS INTEGER) + 1 AS TEXT)"
                )
        return gone

    def heartbeat(self, now: float) -> None:
        with self._tx():
            self._set_meta("heartbeat_at", repr(float(now)))

    def set_stream(self, stream_id: str, *, state: str, last_event_at: float | None, detail: str = "") -> bool:
        """Write the stream row; True when it was written. An unchanged row is not rewritten.

        "Unchanged" compares state, detail and last_event_at, so a stream that only repeats itself commits nothing.
        """
        row = self._db.execute(
            "SELECT state, last_event_at, detail FROM stream WHERE id=?", (stream_id,)
        ).fetchone()
        if row == (state, last_event_at, detail):
            return False
        with self._tx():
            self._db.execute(
                "INSERT OR REPLACE INTO stream(id, state, last_event_at, detail) VALUES (?,?,?,?)",
                (stream_id, state, last_event_at, detail),
            )
        return True

    def close(self) -> None:
        if self._db is not None:
            try:
                self._db.close()
            finally:
                self._db = None
        _close_fd_box(self._fd_box)
        self._fd_finalizer.detach()


class _Tx:
    def __init__(self, db: sqlite3.Connection) -> None:
        self._db = db

    def __enter__(self) -> None:
        self._db.execute("BEGIN IMMEDIATE")

    def __exit__(self, exc_type, exc, tb) -> None:
        if exc_type:
            try:
                self._db.execute("ROLLBACK")  # sqlite may already have rolled back: never mask the original error
            except sqlite3.Error:
                pass
            return
        try:
            self._db.execute("COMMIT")
        except BaseException:
            try:
                self._db.execute("ROLLBACK")  # a failed COMMIT leaves the transaction open
            except sqlite3.Error:
                pass
            raise


def _absent(exc: sqlite3.Error) -> bool:
    return "no such table" in str(exc)


def _uri(path: str) -> str:
    abspath = os.path.abspath(path)
    if abspath.startswith("//"):
        abspath = "/" + abspath.lstrip("/")  # a leading // would parse as a URI authority
    return "file:" + urllib.parse.quote(abspath, safe="/") + "?mode=ro"


class SnapshotReader:
    """A read-only view, one connection per thread. A missing file answers None / {} (the worker has not run).

    The connection a thread holds is checked against the file on disk (see READER_REVALIDATE_S) and reopened when the
    file was replaced. Only an absent file or table reads as "not there"; any other sqlite failure raises
    SnapshotReadError.
    """

    def __init__(self, path: str | os.PathLike) -> None:
        self.path = os.fspath(path)
        self._local = threading.local()
        self._cache: OrderedDict[tuple[str, int, str], Any] = OrderedDict()
        self._cache_lock = threading.Lock()
        self._opens = threading.local()  # per thread: how many connections it has opened

    def _conn(self) -> sqlite3.Connection | None:
        now = time.monotonic()
        slot = getattr(self._local, "slot", None)  # (conn, st_dev, st_ino, checked_at)
        if slot is not None and now - slot[3] < READER_REVALIDATE_S:
            return slot[0]
        try:
            st = os.stat(self.path)
        except FileNotFoundError:
            self.close()
            return None
        except OSError as exc:
            raise SnapshotReadError(f"{type(exc).__name__}: {exc}") from exc
        if slot is not None:
            if (st.st_dev, st.st_ino) == (slot[1], slot[2]):
                self._local.slot = (slot[0], slot[1], slot[2], now)
                return slot[0]
            self.close()
        try:
            conn = sqlite3.connect(_uri(self.path), uri=True, isolation_level=None, timeout=5.0)
        except sqlite3.OperationalError as exc:
            if not os.path.exists(self.path):
                return None
            raise SnapshotReadError(f"{type(exc).__name__}: {exc}") from exc
        self._local.slot = (conn, st.st_dev, st.st_ino, now)
        self._opens.n = getattr(self._opens, "n", 0) + 1
        return conn

    def close(self) -> None:
        """Close this thread's connection (a later call on this thread opens a new one)."""
        slot = getattr(self._local, "slot", None)
        self._local.slot = None
        if slot is not None:
            try:
                slot[0].close()
            except sqlite3.Error:
                pass

    def _read(self, work: Callable[[sqlite3.Connection, dict[str, str]], Any], empty: Callable[[], Any]) -> Any:
        """Run `work(conn, meta)` in one read transaction; `empty()` when the file, a table or the contract row is absent."""
        conn = self._conn()
        if conn is None:
            return empty()
        try:
            conn.execute("BEGIN")
            try:
                try:
                    meta = {k: v for k, v in conn.execute("SELECT key, value FROM meta")}
                    _check_contract(meta.get("contract"))
                    result = empty() if "contract" not in meta else work(conn, meta)
                except sqlite3.OperationalError as exc:
                    if not _absent(exc):
                        raise
                    result = empty()
                conn.execute("COMMIT")  # a failed COMMIT is a failed read: it raises, nothing is returned
            except BaseException:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise
            return result
        except sqlite3.Error as exc:
            raise SnapshotReadError(f"{type(exc).__name__}: {exc}") from exc

    def data_version(self) -> int:
        """An opaque change token for this thread's connection (compare for equality only); 0 while the file is absent.

        It folds the count of connections this thread has opened with `PRAGMA data_version`, so a file replaced under
        the reader (a reopen) changes it even when the new connection's own data_version repeats the old one. It also
        moves when another connection commits anything, heartbeats included: it is a "the file changed" gate, not a
        "a panel changed" signal (that is meta `publish_seq`).
        """
        conn = self._conn()
        if conn is None:
            return 0
        try:
            return (getattr(self._opens, "n", 0) << 48) | int(conn.execute("PRAGMA data_version").fetchone()[0])
        except sqlite3.Error as exc:
            raise SnapshotReadError(f"{type(exc).__name__}: {exc}") from exc

    def read(self, panel_id: str) -> PanelSnap | None:
        def work(conn, meta):
            row = conn.execute(f"SELECT {_PANEL_COLS} FROM panel WHERE id=?", (panel_id,)).fetchone()
            return _snap(row) if row else None

        return self._read(work, lambda: None)

    def read_all(self) -> dict[str, PanelSnap]:
        """Every panel from one read transaction, so all rows come from one end-mark."""

        def work(conn, meta):
            return {r[0]: _snap(r) for r in conn.execute(f"SELECT {_PANEL_COLS} FROM panel").fetchall()}

        return self._read(work, dict)

    def read_manifest(self) -> tuple[dict[str, str], dict[str, PanelSnap]]:
        """(meta, every panel) from one read transaction: `publish_seq` and the panels' gens agree."""

        def work(conn, meta):
            return dict(meta), {r[0]: _snap(r) for r in conn.execute(f"SELECT {_PANEL_COLS} FROM panel").fetchall()}

        return self._read(work, lambda: ({}, {}))

    def meta(self) -> dict[str, str]:
        return self._read(lambda conn, meta: dict(meta), dict)

    def streams(self) -> dict[str, dict]:
        def work(conn, meta):
            rows = conn.execute("SELECT id, state, last_event_at, detail FROM stream").fetchall()
            return {r[0]: {"state": r[1], "last_event_at": r[2], "detail": r[3]} for r in rows}

        return self._read(work, dict)

    def decoded(self, snap: PanelSnap) -> Any:
        """`json.loads(snap.body)`, cached by (id, gen, body_sha) in a bounded LRU (DECODED_CACHE_MAX).

        The cached object is shared between callers: treat it as read-only.
        """
        key = (snap.id, snap.gen, snap.body_sha)
        with self._cache_lock:
            if key in self._cache:
                self._cache.move_to_end(key)
                return self._cache[key]
        value = None if snap.body is None else json.loads(snap.body)
        with self._cache_lock:
            self._cache[key] = value
            self._cache.move_to_end(key)
            while len(self._cache) > DECODED_CACHE_MAX:
                self._cache.popitem(last=False)
        return value

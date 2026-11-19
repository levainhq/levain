"""The cockpit snapshot store: one sqlite file the derive worker writes and the serve reads.

The worker derives each panel and publishes the result here; the serve (and any other reader, in any language) answers
from the stored snapshot instead of deriving on the request path. The file is `snapshots.db` in the cockpit data root,
WAL mode, one writer and many read-only readers. The language-neutral contract (tables, columns, the gen rule, the body
encoding, the contract string, the freshness fields a reader derives) is docs/cockpit_snapshot_contract.md; this module
is the Python implementation of it. Stdlib only.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any

CONTRACT = "levain-cockpit-snapshots/1"
CONTRACT_MAJOR = 1
DB_FILENAME = "snapshots.db"
DECODED_CACHE_MAX = 256

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
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
    body BLOB NOT NULL
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


def encode_body(obj: Any) -> bytes:
    """Canonical UTF-8 JSON: equal values give equal bytes, so equal `body_sha`."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _sha(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def _check_contract(value: str | None) -> None:
    """Raise ContractMismatch unless `value` names this code's contract major. None (no contract row yet) passes."""
    if value is None:
        return
    try:
        major = int(value.rsplit("/", 1)[1])
    except (IndexError, ValueError):
        raise ContractMismatch(f"unreadable snapshot contract {value!r}") from None
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
    body: bytes


def _snap(row: tuple) -> PanelSnap:
    return PanelSnap(
        id=row[0], gen=row[1], derived_at=row[2], started_at=row[3], duration_ms=row[4], valid_until=row[5],
        inputs_fp=row[6], write_gen=row[7], status=row[8], error=row[9], body_sha=row[10], body=bytes(row[11]),
    )


class SnapshotWriter:
    """The one writer. Used from one thread at a time; the caller serialises its publishes."""

    def __init__(self, path: str | os.PathLike, *, pid: int | None = None, now: float | None = None) -> None:
        self.path = os.fspath(path)
        self._db = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
        try:
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA synchronous=NORMAL")
            try:
                row = self._db.execute("SELECT value FROM meta WHERE key='contract'").fetchone()
            except sqlite3.OperationalError:
                row = None  # no meta table yet: a fresh file
            _check_contract(row[0] if row else None)
            self._db.executescript(_SCHEMA)
            started = time.time() if now is None else now
            with self._tx():
                self._set_meta("contract", CONTRACT)
                self._set_meta("worker_pid", str(os.getpid() if pid is None else pid))
                self._set_meta("worker_started_at", repr(float(started)))
        except BaseException:
            self._db.close()
            raise

    def _tx(self):
        return _Tx(self._db)

    def _set_meta(self, key: str, value: str) -> None:
        self._db.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)", (key, value))

    def publish(
        self,
        panel_id: str,
        *,
        status: str,
        body: bytes,
        started_at: float,
        derived_at: float,
        duration_ms: float,
        valid_until: float | None = None,
        inputs_fp: str = "",
        write_gen: int = 0,
        error: str | None = None,
    ) -> int:
        """Store one panel's snapshot in one transaction and return its gen.

        An identical body and status keep gen (only the timing fields move); a changed body or status bumps it.
        """
        sha = _sha(body)
        with self._tx():
            row = self._db.execute(
                "SELECT gen, body_sha, status, write_gen FROM panel WHERE id=?", (panel_id,)
            ).fetchone()
            if row is None:
                gen, wg = 1, write_gen
            else:
                gen = row[0] if (row[1] == sha and row[2] == status) else row[0] + 1
                wg = max(row[3], write_gen)
            self._db.execute(
                f"INSERT OR REPLACE INTO panel({_PANEL_COLS}) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (panel_id, gen, derived_at, started_at, duration_ms, valid_until, inputs_fp, wg, status, error, sha,
                 body),
            )
        return gen

    def heartbeat(self, now: float) -> None:
        with self._tx():
            self._set_meta("heartbeat_at", repr(float(now)))

    def set_stream(self, stream_id: str, *, state: str, last_event_at: float | None, detail: str = "") -> None:
        with self._tx():
            self._db.execute(
                "INSERT OR REPLACE INTO stream(id, state, last_event_at, detail) VALUES (?,?,?,?)",
                (stream_id, state, last_event_at, detail),
            )

    def close(self) -> None:
        self._db.close()


class _Tx:
    def __init__(self, db: sqlite3.Connection) -> None:
        self._db = db

    def __enter__(self) -> None:
        self._db.execute("BEGIN IMMEDIATE")

    def __exit__(self, exc_type, exc, tb) -> None:
        self._db.execute("ROLLBACK" if exc_type else "COMMIT")


class SnapshotReader:
    """A read-only view, one connection per thread. A missing file answers None / {} (the worker has not run)."""

    def __init__(self, path: str | os.PathLike) -> None:
        self.path = os.fspath(path)
        self._local = threading.local()
        self._cache: OrderedDict[tuple[str, int], Any] = OrderedDict()
        self._cache_lock = threading.Lock()

    def _conn(self) -> sqlite3.Connection | None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            return conn
        if not os.path.exists(self.path):
            return None
        uri = "file:" + self.path.replace("?", "%3f").replace("#", "%23") + "?mode=ro"
        conn = sqlite3.connect(uri, uri=True, isolation_level=None, timeout=5.0)
        self._local.conn = conn
        return conn

    def _meta_on(self, conn: sqlite3.Connection) -> dict[str, str]:
        try:
            meta = {k: v for k, v in conn.execute("SELECT key, value FROM meta")}
        except sqlite3.OperationalError:
            return {}  # a file with no schema yet
        _check_contract(meta.get("contract"))
        return meta

    def data_version(self) -> int:
        """`PRAGMA data_version` on this thread's connection; 0 while the file is absent."""
        conn = self._conn()
        if conn is None:
            return 0
        return int(conn.execute("PRAGMA data_version").fetchone()[0])

    def read(self, panel_id: str) -> PanelSnap | None:
        conn = self._conn()
        if conn is None:
            return None
        conn.execute("BEGIN")
        try:
            self._meta_on(conn)
            try:
                row = conn.execute(f"SELECT {_PANEL_COLS} FROM panel WHERE id=?", (panel_id,)).fetchone()
            except sqlite3.OperationalError:
                return None
        finally:
            conn.execute("COMMIT")
        return _snap(row) if row else None

    def read_all(self) -> dict[str, PanelSnap]:
        """Every panel from one read transaction, so all rows come from one end-mark."""
        conn = self._conn()
        if conn is None:
            return {}
        conn.execute("BEGIN")
        try:
            self._meta_on(conn)
            try:
                rows = conn.execute(f"SELECT {_PANEL_COLS} FROM panel").fetchall()
            except sqlite3.OperationalError:
                return {}
        finally:
            conn.execute("COMMIT")
        return {r[0]: _snap(r) for r in rows}

    def meta(self) -> dict[str, str]:
        conn = self._conn()
        if conn is None:
            return {}
        conn.execute("BEGIN")
        try:
            return self._meta_on(conn)
        finally:
            conn.execute("COMMIT")

    def streams(self) -> dict[str, dict]:
        conn = self._conn()
        if conn is None:
            return {}
        try:
            rows = conn.execute("SELECT id, state, last_event_at, detail FROM stream").fetchall()
        except sqlite3.OperationalError:
            return {}
        return {r[0]: {"state": r[1], "last_event_at": r[2], "detail": r[3]} for r in rows}

    def decoded(self, snap: PanelSnap) -> Any:
        """`json.loads(snap.body)`, cached by (id, gen) in a bounded LRU (DECODED_CACHE_MAX).

        The cached object is shared between callers: treat it as read-only.
        """
        key = (snap.id, snap.gen)
        with self._cache_lock:
            if key in self._cache:
                self._cache.move_to_end(key)
                return self._cache[key]
        value = json.loads(snap.body)
        with self._cache_lock:
            self._cache[key] = value
            self._cache.move_to_end(key)
            while len(self._cache) > DECODED_CACHE_MAX:
                self._cache.popitem(last=False)
        return value

"""The push store: ``<queue_dir>/webpush/webpush.db``, beside the consent queue and opened the way the queue opens its own.

The directory is 0700 and the file 0600; a symlinked directory or file is refused, and the file is created with
``O_NOFOLLOW`` so a link planted between the check and the open is not followed. Under a sandboxed agent the
queue directory is outside its write reach, which is where the VAPID private key belongs.

``pings``, ``deliveries`` and ``opens`` are records: a trigger refuses to change or delete a row. ``held`` is not: it is
the queue of pings the presence rule held back from the phone (:mod:`levain.webpush.held`). A flush claims a held ping
with a lease (``claimed_at``) and marks it released only after a push to the phone succeeded; a claim older than
:data:`HELD_LEASE_SECS` is taken again, so a flush that dies loses nothing and a duplicate is bounded by the lease.
"""
from __future__ import annotations

import os
import sqlite3
import stat
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from levain.autonomic.durable_sqlite import open_durable, read_transaction, rollback, write_transaction

DIR_NAME = "webpush"
DB_NAME = "webpush.db"
STORE_FORMAT = "levain-push-store/1"
MAX_SUBSCRIPTIONS_PER_DEVICE = 5
MAX_SUBSCRIPTIONS = 20
STAMP = "%Y-%m-%dT%H:%M:%SZ"   # the one stamp format of this store, whole seconds, UTC
HELD_LEASE_SECS = 300          # a held ping claimed for a push is another flush's to take again after this long


class PushStoreError(RuntimeError):
    """The push store cannot be opened safely."""


class DeviceNotLive(PushStoreError):
    """A subscribe for a device its own authority no longer holds as live (revoked after its credential was read)."""


# A subscription the cockpit stored is under its paired device's credential name, ``paired-<id>``
# (:data:`levain.cockpit.firejournal.PAIRED_PREFIX`; a test pins the two equal), and its ``journal`` column holds the
# cockpit's data root, whose pairing journal is that device's one authority. Every other subscription's device is a
# row of this store's own ``devices`` table.
PAIRED_PREFIX = "paired-"
_JOURNAL_DB = "cockpit.db"     # levain.cockpit.firejournal.DB_NAME (pinned equal by the same test)
_JOURNAL_WAIT_S = 0.25         # a gate reads liveness: a journal writer must not stall it


def paired_device_live(journal: "str | Path | None", device_id: str) -> bool:
    """Whether the paired device ``device_id`` (``paired-<id>``) is unrevoked in the cockpit pairing journal under
    ``journal``, read through a read-only connection that never creates the file. Anything else, an unreadable
    journal included, is False: a push to a device that may be revoked is never sent on a guess."""
    if not journal or not device_id.startswith(PAIRED_PREFIX):
        return False
    try:
        path = (Path(journal) / _JOURNAL_DB).resolve(strict=True)
        conn = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True, timeout=_JOURNAL_WAIT_S)
        try:
            row = conn.execute("SELECT revoked_at FROM paired_devices WHERE id = ?",
                               (device_id[len(PAIRED_PREFIX):],)).fetchone()
        finally:
            conn.close()
    except (OSError, ValueError, RuntimeError, sqlite3.Error):
        return False
    return row is not None and row[0] is None


def live_rows(conn: sqlite3.Connection, *, first: bool = False, device: str | None = None) -> list[dict[str, Any]]:
    """The live subscriptions, by the one rule every reader uses (the gate's :func:`levain.webpush.live` and the
    send's :meth:`PushStore.live_subscriptions`): not retired, and its device live in its own authority, a row of
    ``devices`` not revoked, or for a ``paired-`` device, unrevoked in the cockpit journal the row names, asked now.
    A device no authority holds is not live. ``first`` stops at the first live one; ``device`` keeps only that
    device's rows, so only they reach the per-device liveness check."""
    out: list[dict[str, Any]] = []
    asked: dict[tuple[Any, str], bool] = {}
    for r in conn.execute("SELECT s.*, d.id AS _dev, d.revoked_at AS _dev_revoked FROM subscriptions s "
                          "LEFT JOIN devices d ON d.id = s.device_id WHERE s.retired_at IS NULL "
                          + ("" if device is None else "AND s.device_id = ? ") + "ORDER BY s.id",
                          () if device is None else (device,)):
        row = dict(r)
        dev, dev_revoked = row.pop("_dev"), row.pop("_dev_revoked")
        owner = row.get("device_id") or ""
        if owner.startswith(PAIRED_PREFIX):
            key = (row.get("journal"), owner)
            if key not in asked:
                asked[key] = paired_device_live(row.get("journal"), owner)
            ok = asked[key]
        else:
            ok = dev is not None and dev_revoked is None
        if ok:
            out.append(row)
            if first:
                break
    return out


def now_stamp() -> str:
    return datetime.now(timezone.utc).strftime(STAMP)


def _records(table: str) -> tuple[str, str]:
    return (
        f"CREATE TRIGGER IF NOT EXISTS {table}_frozen BEFORE UPDATE ON {table} "
        f"BEGIN SELECT RAISE(ABORT, '{table} rows never change'); END",
        f"CREATE TRIGGER IF NOT EXISTS {table}_kept BEFORE DELETE ON {table} "
        f"BEGIN SELECT RAISE(ABORT, '{table} rows are never deleted'); END",
    )


_SCHEMA: tuple[str, ...] = (
    "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
    # one key pair; ``private_der`` is PKCS8 DER of the P-256 private key
    "CREATE TABLE IF NOT EXISTS vapid (id INTEGER PRIMARY KEY CHECK (id = 1), private_der BLOB NOT NULL, "
    "public_b64 TEXT NOT NULL, sub TEXT NOT NULL, created TEXT NOT NULL)",
    # a signed VAPID token per push-service origin, reused until it nears expiry (Apple asks for no more than
    # one new token an hour)
    "CREATE TABLE IF NOT EXISTS vapid_tokens (aud TEXT PRIMARY KEY, jwt TEXT NOT NULL, exp INTEGER NOT NULL)",
    # the pairing/device logic is levain.webpush.devices; this module only creates the tables
    "CREATE TABLE IF NOT EXISTS devices (id TEXT PRIMARY KEY, label TEXT, token_hash TEXT UNIQUE NOT NULL, "
    "created TEXT, last_seen TEXT, revoked_at TEXT)",
    "CREATE TABLE IF NOT EXISTS pairings (code_hash TEXT PRIMARY KEY, device_label TEXT, expires TEXT NOT NULL, "
    "used_at TEXT)",
    # the short code the operator types into the installed app, and the wrong guesses that lock it out; both are
    # additive tables, so an existing store gains them on its next open with no format change
    "CREATE TABLE IF NOT EXISTS short_pairings (code_hash TEXT PRIMARY KEY, device_label TEXT, expires TEXT NOT NULL, "
    "used_at TEXT)",
    "CREATE TABLE IF NOT EXISTS redeem_failures (at TEXT NOT NULL)",
    # ``subscribed`` is the subscribe order, moved up on every re-subscribe: the caps retire by it
    "CREATE TABLE IF NOT EXISTS subscriptions (id INTEGER PRIMARY KEY AUTOINCREMENT, device_id TEXT, "
    "endpoint TEXT NOT NULL UNIQUE, p256dh TEXT NOT NULL, auth TEXT NOT NULL, created TEXT NOT NULL, "
    "last_status INTEGER, last_at TEXT, retired_at TEXT, subscribed INTEGER NOT NULL DEFAULT 0)",
    "CREATE TABLE IF NOT EXISTS pings (id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL, kind TEXT NOT NULL, "
    "ref TEXT, title TEXT NOT NULL, body TEXT NOT NULL, command INTEGER)",
    "CREATE TABLE IF NOT EXISTS deliveries (id INTEGER PRIMARY KEY AUTOINCREMENT, ping_id INTEGER NOT NULL "
    "REFERENCES pings(id), subscription_id INTEGER NOT NULL REFERENCES subscriptions(id), at TEXT NOT NULL, "
    "status INTEGER NOT NULL)",
    "CREATE TABLE IF NOT EXISTS opens (id INTEGER PRIMARY KEY AUTOINCREMENT, ping_id INTEGER NOT NULL "
    "REFERENCES pings(id), device_id TEXT, at TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS held (ping_id INTEGER PRIMARY KEY REFERENCES pings(id), url TEXT NOT NULL, "
    "reason TEXT NOT NULL, held_at TEXT NOT NULL, released_at TEXT, claimed_at TEXT)",
    *_records("pings"), *_records("deliveries"), *_records("opens"),
)


def _check_dir(directory: Path) -> None:
    if not directory.exists():
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    st = os.lstat(directory)
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
        raise PushStoreError(f"push directory {directory} is not a plain directory")
    if st.st_uid != os.getuid():
        raise PushStoreError(f"push directory {directory} is not owned by uid {os.getuid()}")
    if st.st_mode & 0o077:
        os.chmod(directory, 0o700)


def _check_file(path: Path) -> None:
    if not os.path.lexists(path):
        # created here, not by sqlite, so O_NOFOLLOW applies to the file the database will then open
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
            os.close(fd)
            return
        except FileExistsError:
            pass                  # another opener created it first: checked below like any existing file
    st = os.lstat(path)
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
        raise PushStoreError(f"push database {path} is not a regular file")


class PushStore:
    def __init__(self, queue_dir: Path | str) -> None:
        self.directory = Path(queue_dir) / DIR_NAME
        self.path = self.directory / DB_NAME
        self._ready = False

    def _connect(self) -> sqlite3.Connection:
        if not self._ready:
            _check_dir(self.directory)
            _check_file(self.path)
        conn = open_durable(self.path)
        conn.row_factory = sqlite3.Row
        try:
            if not self._ready:
                os.chmod(self.path, 0o600)
                self._ensure_schema(conn)
                self._ready = True
        except BaseException:
            conn.close()
            raise
        return conn

    @staticmethod
    def _ensure_schema(conn: sqlite3.Connection) -> None:
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute("SELECT value FROM meta WHERE key = 'format'").fetchone() \
                if conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'meta'").fetchone() else None
            if row is not None and row[0] != STORE_FORMAT:
                raise PushStoreError(f"push store format {row[0]!r} is not {STORE_FORMAT!r}")
            for stmt in _SCHEMA:
                conn.execute(stmt)
            if "subscribed" not in {r[1] for r in conn.execute("PRAGMA table_info(subscriptions)")}:
                conn.execute("ALTER TABLE subscriptions ADD COLUMN subscribed INTEGER NOT NULL DEFAULT 0")
                conn.execute("UPDATE subscriptions SET subscribed = id")
            # ``journal``: the cockpit data root whose pairing journal holds a ``paired-`` device (live_rows)
            if "journal" not in {r[1] for r in conn.execute("PRAGMA table_info(subscriptions)")}:
                conn.execute("ALTER TABLE subscriptions ADD COLUMN journal TEXT")
            # ``command``: 1 the operator's notify command started for this ping, 0 it was set and could not start,
            # NULL none was set (and every ping recorded before the column existed)
            if "command" not in {r[1] for r in conn.execute("PRAGMA table_info(pings)")}:
                conn.execute("ALTER TABLE pings ADD COLUMN command INTEGER")
            if "claimed_at" not in {r[1] for r in conn.execute("PRAGMA table_info(held)")}:
                conn.execute("ALTER TABLE held ADD COLUMN claimed_at TEXT")
            # a subscription with no device is one no revoke can stop: none is stored any more, and any left is retired
            conn.execute("UPDATE subscriptions SET retired_at = ? WHERE device_id IS NULL AND retired_at IS NULL",
                         (now_stamp(),))
            conn.execute("INSERT OR IGNORE INTO meta (key, value) VALUES ('format', ?)", (STORE_FORMAT,))
            conn.execute("COMMIT")
        except BaseException:
            rollback(conn)
            raise

    def connection(self) -> sqlite3.Connection:
        """A plain connection for ``levain.webpush.devices``: default isolation (``with conn:`` commits), the
        store's durability PRAGMAs, schema ensured. The caller closes it."""
        self._connect().close()
        conn = open_durable(self.path)
        conn.isolation_level = ""
        return conn

    @contextmanager
    def write(self) -> Iterator[sqlite3.Connection]:
        with write_transaction(self._connect) as conn:
            yield conn

    @contextmanager
    def read(self) -> Iterator[sqlite3.Connection]:
        with read_transaction(self._connect) as conn:
            yield conn

    # --- subscriptions -------------------------------------------------------------------------------
    def add_subscription(self, endpoint: str, p256dh: str, auth: str, device_id: str, *,
                         journal: "str | Path | None" = None) -> int:
        """Store (or revive, with fresh keys) the subscription for ``endpoint`` under ``device_id``; returns its id.
        A revived row takes the device that subscribed it now, so it is never live under a device revoked before.
        Live subscriptions are capped per device and overall: the least recently subscribed retire first, never the
        one just stored. A ``paired-`` device needs ``journal``, its cockpit data root, and is asked again there
        inside this write: one revoked since its credential was read raises :class:`DeviceNotLive`, nothing
        stored (the send asks the journal again, :func:`live_rows`, so this bounds the window, it is not the
        guard)."""
        if not isinstance(device_id, str) or not device_id:
            raise ValueError("a subscription is always stored under a device")
        if device_id.startswith(PAIRED_PREFIX) and not journal:
            raise ValueError("a paired device's subscription names its pairing journal")
        with self.write() as c:
            if device_id.startswith(PAIRED_PREFIX) and not paired_device_live(journal, device_id):
                raise DeviceNotLive(f"{device_id} is not a live paired device")
            return _subscribe(c, endpoint, p256dh, auth, device_id,
                              journal=str(journal) if device_id.startswith(PAIRED_PREFIX) else None)

    def subscribe_unowned(self, endpoint: str, p256dh: str, auth: str, label: Any,
                          now: datetime) -> tuple[int, tuple[str, str] | None]:
        """A run-token subscribe, in one transaction: an endpoint a live device already owns stays that device's and
        nothing is minted; otherwise a new device is minted and owns it. Returns ``(subscription id, (device_id,
        device_token) or None)``."""
        from levain.webpush import devices

        with self.write() as c:
            row = c.execute("SELECT s.device_id FROM subscriptions s JOIN devices d ON d.id = s.device_id "
                            "WHERE s.endpoint = ? AND d.revoked_at IS NULL", (endpoint,)).fetchone()
            minted = None
            if row is None:
                minted = devices._insert_device(c, label, now)
            owner = row[0] if row is not None else minted[0]
            return _subscribe(c, endpoint, p256dh, auth, owner), minted

    def retire_subscription(self, endpoint: str) -> bool:
        with self.write() as c:
            return c.execute("UPDATE subscriptions SET retired_at = ? WHERE endpoint = ? AND retired_at IS NULL",
                             (now_stamp(), endpoint)).rowcount > 0

    def live_subscriptions(self, *, device: str | None = None) -> list[dict[str, Any]]:
        # a subscription whose device was revoked is not live even if a revoke left its row unretired (live_rows)
        with self.read() as c:
            return live_rows(c, device=device)

    # --- the record ----------------------------------------------------------------------------------
    def add_ping(self, kind: str, ref: str | None, title: str, body: str, command: bool | None = None, *,
                 hold: tuple[str, str] | None = None) -> int:
        """Record one ping; ``command`` is whether the notify command started (None: none was set). ``hold`` is
        ``(url, reason)``: the ping is also queued for the phone in the same transaction, ``reason`` being the word that
        muted it (``quiet``, ``sleep``) or ``active`` (the desk was in use)."""
        with self.write() as c:
            ping_id = int(c.execute("INSERT INTO pings (at, kind, ref, title, body, command) VALUES (?,?,?,?,?,?)",
                                    (now_stamp(), kind, ref, title, body,
                                     None if command is None else int(bool(command)))).lastrowid)
            if hold is not None:
                c.execute("INSERT INTO held (ping_id, url, reason, held_at) VALUES (?,?,?,?)",
                          (ping_id, hold[0], hold[1], now_stamp()))
            return ping_id

    # --- the held queue ------------------------------------------------------------------------------
    @staticmethod
    def _cutoff(now: float | None) -> str:
        at = time.time() if now is None else now
        return time.strftime(STAMP, time.gmtime(at - HELD_LEASE_SECS))

    def held_items(self, *, now: float | None = None) -> list[dict[str, Any]]:
        """The pings a flush may take now, oldest first, each with what it says: held, and not under a live claim."""
        with self.read() as c:
            return [dict(r) for r in c.execute(
                "SELECT h.ping_id, h.url, h.reason, h.held_at, p.kind, p.ref, p.title, p.body FROM held h "
                "JOIN pings p ON p.id = h.ping_id WHERE h.released_at IS NULL "
                "AND (h.claimed_at IS NULL OR h.claimed_at <= ?) ORDER BY h.ping_id", (self._cutoff(now),))]

    def claim_held(self, ping_ids: list[int], *, now: float | None = None) -> list[int]:
        """Lease these held pings to the caller and return the ids it got: a ping under a live claim, or already
        released, is not given again, so racing flushes take disjoint sets."""
        with self.write() as c:
            at = time.strftime(STAMP, time.gmtime(time.time() if now is None else now))
            cutoff = self._cutoff(now)
            return [i for i in ping_ids if c.execute(
                "UPDATE held SET claimed_at = ? WHERE ping_id = ? AND released_at IS NULL "
                "AND (claimed_at IS NULL OR claimed_at <= ?)", (at, i, cutoff)).rowcount == 1]

    def release_held(self, ping_id: int) -> None:
        """Mark a claimed ping released: its push to the phone succeeded."""
        with self.write() as c:
            c.execute("UPDATE held SET released_at = ? WHERE ping_id = ? AND released_at IS NULL",
                      (now_stamp(), ping_id))

    def add_delivery(self, ping_id: int, subscription_id: int, status: int, *, retire: bool = False) -> None:
        with self.write() as c:
            at = now_stamp()
            c.execute("INSERT INTO deliveries (ping_id, subscription_id, at, status) VALUES (?,?,?,?)",
                      (ping_id, subscription_id, at, status))
            c.execute("UPDATE subscriptions SET last_status = ?, last_at = ?, "
                      "retired_at = CASE WHEN ? THEN ? ELSE retired_at END WHERE id = ?",
                      (status, at, 1 if retire else 0, at, subscription_id))

    def add_open(self, ping_id: int, device_id: str | None) -> bool:
        with self.write() as c:
            if c.execute("SELECT 1 FROM pings WHERE id = ?", (ping_id,)).fetchone() is None:
                return False
            c.execute("INSERT INTO opens (ping_id, device_id, at) VALUES (?,?,?)", (ping_id, device_id, now_stamp()))
            return True

    def record(self, limit: int = 100) -> list[dict[str, Any]]:
        """The newest ``limit`` pings, each with its deliveries and opens."""
        with self.read() as c:
            pings = [dict(r) for r in c.execute("SELECT * FROM pings ORDER BY id DESC LIMIT ?", (limit,))]
            for p in pings:
                p["deliveries"] = [dict(r) for r in c.execute(
                    "SELECT subscription_id, at, status FROM deliveries WHERE ping_id = ? ORDER BY id", (p["id"],))]
                p["opens"] = [dict(r) for r in c.execute(
                    "SELECT device_id, at FROM opens WHERE ping_id = ? ORDER BY id", (p["id"],))]
            return pings


def _subscribe(c: sqlite3.Connection, endpoint: str, p256dh: str, auth: str, device_id: str, *,
               journal: str | None = None) -> int:
    """The body of :meth:`PushStore.add_subscription`, in the caller's write transaction."""
    nxt = "(SELECT COALESCE(MAX(subscribed), 0) + 1 FROM subscriptions)"
    c.execute(
        "INSERT INTO subscriptions (device_id, endpoint, p256dh, auth, created, subscribed, journal) "
        f"VALUES (?,?,?,?,?,{nxt},?) "
        "ON CONFLICT(endpoint) DO UPDATE SET p256dh = excluded.p256dh, auth = excluded.auth, "
        f"device_id = excluded.device_id, retired_at = NULL, subscribed = {nxt}, journal = excluded.journal",
        (device_id, endpoint, p256dh, auth, now_stamp(), journal))
    row = c.execute("SELECT id, device_id FROM subscriptions WHERE endpoint = ?", (endpoint,)).fetchone()
    sid, owner = int(row[0]), row[1]
    at = now_stamp()
    c.execute(
        "UPDATE subscriptions SET retired_at = ? WHERE retired_at IS NULL AND device_id IS ? AND id != ? "
        "AND id NOT IN (SELECT id FROM subscriptions WHERE retired_at IS NULL AND device_id IS ? "
        "AND id != ? ORDER BY subscribed DESC LIMIT ?)",
        (at, owner, sid, owner, sid, MAX_SUBSCRIPTIONS_PER_DEVICE - 1))
    c.execute(
        "UPDATE subscriptions SET retired_at = ? WHERE retired_at IS NULL AND id != ? "
        "AND id NOT IN (SELECT id FROM subscriptions WHERE retired_at IS NULL AND id != ? "
        "ORDER BY subscribed DESC LIMIT ?)", (at, sid, sid, MAX_SUBSCRIPTIONS - 1))
    return sid


def retire_device_subscriptions(conn: sqlite3.Connection, device_id: str, now: datetime) -> int:
    """Mark ``device_id``'s live subscriptions retired; the caller's transaction commits it."""
    return conn.execute("UPDATE subscriptions SET retired_at = ? WHERE device_id = ? AND retired_at IS NULL",
                        (now.astimezone(timezone.utc).strftime(STAMP), device_id)).rowcount

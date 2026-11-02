"""The push store: ``<queue_dir>/webpush/webpush.db``, beside the consent queue and opened the way the queue opens its own.

The directory is 0700 and the file 0600; a symlinked directory or file is refused, and the file is created with
``O_NOFOLLOW`` so a link planted between the check and the open is not followed. Under a sandboxed agent the
queue directory is outside its write reach, which is where the VAPID private key belongs.

``pings``, ``targets``, ``deliveries``, ``opens`` and ``receipts`` are records: a trigger refuses to change or delete a row.
A ``targets`` row is written for each subscription a push is about to go to, BEFORE the push services are asked (the
``deliveries`` row, with the answer, is written only after every push of the send finished, up to the send's wall
budget): a device's receipt can arrive in between, and it is accepted for a ping targeted at or delivered to its own
subscription, never any other. ``held`` is not a record: it is
the queue of pings the presence rule held back from the phone (:mod:`levain.webpush.held`). A flush claims a held ping
with a lease (``claimed_at``) and marks it released only after a push to the phone succeeded; a claim older than
:data:`HELD_LEASE_SECS` is taken again, so a flush that dies loses nothing and a duplicate is bounded by the lease.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import re
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
RECEIPT_KINDS = ("received", "shown")
MAX_RECEIPTS_PER_DEVICE_HOUR = 600   # what one device may report in an hour: a flood cannot grow the table past this
MAX_RECEIPTS_PER_PING_KIND = 8       # a ping delivered to one device more than once is a few rows, never unbounded
RECEIPT_MIN_MS = 1_577_836_800_000   # 2020-01-01: a device clock before it is not a clock this record keeps
RECEIPT_FUTURE_MS = 86_400_000       # and one a day ahead of this server's is not either
_SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")
NOTICES_KEEP = 500             # the newest notices kept, and no notice is kept past NOTICES_DAYS
NOTICES_DAYS = 30
HELD_LEASE_SECS = 300          # a held ping claimed for a push is another flush's to take again after this long


class PushStoreError(RuntimeError):
    """The push store cannot be opened safely."""


class DeviceNotLive(PushStoreError):
    """A subscribe for a device its own authority no longer holds as live (revoked after its credential was read)."""


# A subscription the cockpit stored is under its paired device's credential name, ``paired-<id>``
# (:data:`levain.cockpit.firejournal.PAIRED_PREFIX`; a test pins the two equal), and its ``journal`` column holds the
# cockpit's data root, whose pairing journal is that device's one authority. A subscription under any other device id
# is never live and never stored (W1b: the phone port's own device registry is gone).
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
    send's :meth:`PushStore.live_subscriptions`): not retired, and a ``paired-`` device unrevoked in the cockpit
    journal the row names, asked now. Any other device (a row a retired phone port wrote) is never live. ``first``
    stops at the first live one; ``device`` keeps only that device's rows, so only they reach the per-device
    liveness check."""
    out: list[dict[str, Any]] = []
    asked: dict[tuple[Any, str], bool] = {}
    for r in conn.execute("SELECT s.* FROM subscriptions s WHERE s.retired_at IS NULL "
                          + ("" if device is None else "AND s.device_id = ? ") + "ORDER BY s.id",
                          () if device is None else (device,)):
        row = dict(r)
        owner = row.get("device_id") or ""
        ok = False
        if owner.startswith(PAIRED_PREFIX):
            key = (row.get("journal"), owner)
            if key not in asked:
                asked[key] = paired_device_live(row.get("journal"), owner)
            ok = asked[key]
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
    # what a device reported about a push it got: ``at`` is the worker's own clock (whole stamp plus milliseconds),
    # ``reported_at`` this server's; the unique key makes a retried report a no-op
    "CREATE TABLE IF NOT EXISTS receipts (id INTEGER PRIMARY KEY AUTOINCREMENT, ping_id INTEGER NOT NULL "
    "REFERENCES pings(id), device_id TEXT NOT NULL, kind TEXT NOT NULL CHECK (kind IN ('received','shown')), "
    "at TEXT NOT NULL, reported_at TEXT NOT NULL, UNIQUE (ping_id, device_id, kind, at))",
    "CREATE TABLE IF NOT EXISTS held (ping_id INTEGER PRIMARY KEY REFERENCES pings(id), url TEXT NOT NULL, "
    "reason TEXT NOT NULL, held_at TEXT NOT NULL, released_at TEXT, claimed_at TEXT)",
    # a push about to go to a subscription (written before the push services are asked; see the module docstring)
    "CREATE TABLE IF NOT EXISTS targets (id INTEGER PRIMARY KEY AUTOINCREMENT, ping_id INTEGER NOT NULL "
    "REFERENCES pings(id), subscription_id INTEGER NOT NULL REFERENCES subscriptions(id), at TEXT NOT NULL)",
    # a posted notice (levain.webpush.notify): ``source`` and ``token`` are copied at write time, so a revoked or
    # renamed token never rewrites history; ``ping_id`` is the delivery record when one was made (a ``low`` notice has none)
    "CREATE TABLE IF NOT EXISTS notices (id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL, source TEXT NOT NULL, "
    "title TEXT NOT NULL, body TEXT NOT NULL, priority TEXT NOT NULL CHECK (priority IN ('low','normal','urgent')), "
    "token TEXT NOT NULL, decision TEXT NOT NULL CHECK (decision IN ('pushed','held','recorded')), "
    "ping_id INTEGER REFERENCES pings(id), dismissed_at TEXT)",
    "CREATE INDEX IF NOT EXISTS receipts_device_reported ON receipts (device_id, reported_at)",
    "CREATE INDEX IF NOT EXISTS targets_ping_sub ON targets (ping_id, subscription_id)",
    "CREATE INDEX IF NOT EXISTS deliveries_ping_sub ON deliveries (ping_id, subscription_id)",
    *_records("pings"), *_records("targets"), *_records("deliveries"), *_records("opens"), *_records("receipts"),
)
# every name the schema creates (tables, indexes, triggers), for the lock-free "nothing to do" check
_SCHEMA_NAMES = frozenset(re.findall(r"IF NOT EXISTS (\w+)", " ".join(_SCHEMA)))


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
    def _schema_current(conn: sqlite3.Connection) -> bool:
        """Whether every table, index, trigger and column :meth:`_ensure_schema` makes is already there, read without the
        write lock: a store opened per request (the token-less receipt route) never takes ``BEGIN IMMEDIATE`` just to
        find nothing to do."""
        names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master")}
        if not _SCHEMA_NAMES <= names:
            return False
        row = conn.execute("SELECT value FROM meta WHERE key = 'format'").fetchone()
        if row is None or row[0] != STORE_FORMAT:
            return False
        for table, column in (("subscriptions", "subscribed"), ("subscriptions", "journal"), ("pings", "command"),
                              ("held", "claimed_at")):
            if column not in {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}:
                return False
        return conn.execute("SELECT 1 FROM subscriptions WHERE device_id IS NULL AND retired_at IS NULL "
                            "LIMIT 1").fetchone() is None

    @staticmethod
    def _ensure_schema(conn: sqlite3.Connection) -> None:
        if PushStore._schema_current(conn):
            return
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
        """A plain connection: default isolation (``with conn:`` commits), the
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
        one just stored. The device must be a ``paired-`` one (anything else raises :class:`ValueError`: no other
        writer exists, so the store refuses what the send would never reach) and needs ``journal``, its cockpit data
        root, where it is asked again inside this write: one revoked since its credential was read raises
        :class:`DeviceNotLive`, nothing stored (the send asks the journal again, :func:`live_rows`, so this bounds
        the window, it is not the guard)."""
        if not isinstance(device_id, str) or not device_id.startswith(PAIRED_PREFIX):
            raise ValueError("a subscription is stored only under a paired device")
        if not journal:
            raise ValueError("a paired device's subscription names its pairing journal")
        with self.write() as c:
            if not paired_device_live(journal, device_id):
                raise DeviceNotLive(f"{device_id} is not a live paired device")
            return _subscribe(c, endpoint, p256dh, auth, device_id, journal=str(journal))

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
            return self._insert_ping(c, kind, ref, title, body, command, hold)

    @staticmethod
    def _insert_ping(c: sqlite3.Connection, kind: str, ref: str | None, title: str, body: str, command: bool | None,
                     hold: tuple[str, str] | None) -> int:
        ping_id = int(c.execute("INSERT INTO pings (at, kind, ref, title, body, command) VALUES (?,?,?,?,?,?)",
                                (now_stamp(), kind, ref, title, body,
                                 None if command is None else int(bool(command)))).lastrowid)
        if hold is not None:
            c.execute("INSERT INTO held (ping_id, url, reason, held_at) VALUES (?,?,?,?)",
                      (ping_id, hold[0], hold[1], now_stamp()))
        return ping_id

    # --- notices -------------------------------------------------------------------------------------
    def add_notice(self, source: str, title: str, body: str, priority: str, token: str, decision: str,
                   ping_id: int | None = None, *, now: float | None = None) -> int:
        """Record one posted notice and prune in the same transaction: past the newest :data:`NOTICES_KEEP`, or older than
        :data:`NOTICES_DAYS` days, goes."""
        with self.write() as c:
            return self._insert_notice(c, source, title, body, priority, token, decision, ping_id,
                                       time.time() if now is None else now)

    def record_notice(self, kind: str, ref: str, display: str, source: str, title: str, body: str, priority: str,
                      token: str, decision: str, *, hold: tuple[str, str] | None = None,
                      now: float | None = None) -> tuple[int | None, int]:
        """A posted notice and its delivery record in ONE transaction: the ping (``pushed`` and ``held``; a ``recorded``
        notice has none), its held row (``held`` only, ``hold`` being ``(url, reason)`` as :meth:`add_ping` takes it),
        the notice row and the retention prune. Any failure rolls back all of it, so there is never a ping without its
        notice or a notice pointing at no ping. Returns ``(ping_id, notice_id)``."""
        if decision not in ("pushed", "held", "recorded") or (hold is not None) != (decision == "held"):
            raise ValueError("a held notice, and only a held one, carries its hold")
        at = time.time() if now is None else now
        with self.write() as c:
            ping_id = None if decision == "recorded" else self._insert_ping(c, kind, ref, display, body, None, hold)
            return ping_id, self._insert_notice(c, source, title, body, priority, token, decision, ping_id, at)

    @staticmethod
    def _insert_notice(c: sqlite3.Connection, source: str, title: str, body: str, priority: str, token: str,
                       decision: str, ping_id: int | None, at: float) -> int:
        nid = int(c.execute("INSERT INTO notices (at, source, title, body, priority, token, decision, ping_id) "
                            "VALUES (?,?,?,?,?,?,?,?)",
                            (time.strftime(STAMP, time.gmtime(at)), source, title, body, priority, token, decision,
                             ping_id)).lastrowid)
        c.execute("DELETE FROM notices WHERE at < ?", (time.strftime(STAMP, time.gmtime(at - NOTICES_DAYS * 86400)),))
        c.execute("DELETE FROM notices WHERE id NOT IN (SELECT id FROM notices ORDER BY id DESC LIMIT ?)",
                  (NOTICES_KEEP,))
        return nid

    def notices(self, limit: int = 100, *, include_dismissed: bool = False) -> list[dict[str, Any]]:
        """The newest ``limit`` notices (at most :data:`NOTICES_KEEP`), dismissed ones left out unless asked for."""
        n = max(0, min(int(limit), NOTICES_KEEP))
        with self.read() as c:
            return [dict(r) for r in c.execute(
                "SELECT * FROM notices" + ("" if include_dismissed else " WHERE dismissed_at IS NULL")
                + " ORDER BY id DESC LIMIT ?", (n,))]

    def opened_ping_ids(self, ping_ids: list[int]) -> set[int]:
        """Which of these pings some device opened (a row in ``opens``)."""
        ids = sorted({int(i) for i in ping_ids})
        if not ids:
            return set()
        with self.read() as c:
            return {int(r[0]) for r in c.execute(
                "SELECT DISTINCT ping_id FROM opens WHERE ping_id IN (%s)" % ",".join("?" * len(ids)), ids)}

    def dismiss_notice(self, notice_id: int, *, now: float | None = None) -> bool:
        """Mark a notice dismissed; whether one was (an unknown or already dismissed id is not)."""
        at = time.time() if now is None else now
        with self.write() as c:
            return c.execute("UPDATE notices SET dismissed_at = ? WHERE id = ? AND dismissed_at IS NULL",
                             (time.strftime(STAMP, time.gmtime(at)), notice_id)).rowcount == 1

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

    def add_targets(self, ping_id: int, subscription_ids: list[int]) -> None:
        """Record that ``ping_id`` is about to be pushed to each of ``subscription_ids`` (one transaction), before any
        push service is asked: a receipt from one of those devices is accepted from this moment."""
        if not subscription_ids:
            return
        with self.write() as c:
            at = now_stamp()
            c.executemany("INSERT INTO targets (ping_id, subscription_id, at) VALUES (?,?,?)",
                          [(ping_id, sid, at) for sid in subscription_ids])

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
            # idempotent per (ping, device): two cockpit tabs may drain the same tap, and the second answers like the first
            if c.execute("SELECT 1 FROM opens WHERE ping_id = ? AND device_id IS ?", (ping_id, device_id)).fetchone() is None:
                c.execute("INSERT INTO opens (ping_id, device_id, at) VALUES (?,?,?)", (ping_id, device_id, now_stamp()))
            return True

    def add_receipt(self, endpoint_sha256: str, ping_id: int, kind: str, at_ms: int) -> str:
        """Append what a device reported about a push it got. The device is the one whose live subscription's endpoint
        hashes to ``endpoint_sha256`` (the endpoint is the secret only that device and this server hold). Answers
        ``recorded``, ``duplicate`` (the same report again, nothing written), or the refusal: ``unknown_device`` (no live
        subscription hashes to it), ``unknown_ping``, ``bad_report`` (a kind or time this record does not keep) or
        ``limited`` (the device reported more than its caps: nothing written).

        ``unknown_ping`` is also the answer for a ping that exists but was never targeted at or delivered to THIS
        subscription (the one the hash resolved to): one device's hash writes only about its own pushes, and the answer
        is the same as for an absent ping, so it says nothing about which pings exist.

        The request carries no token, so nothing it sends takes the write lock until it has earned it: the hash is
        resolved, and the ping, duplicate and cap checks made, in a READ transaction; every non-retired subscription's
        digest is compared with :func:`hmac.compare_digest`, all of them, with no early exit; the pairing journal is
        asked only about the one matched device. Only a matched, live, targeted, new, within-cap receipt opens the write
        transaction, which checks the subscription, duplicate and caps again before it appends."""
        if kind not in RECEIPT_KINDS or isinstance(at_ms, bool) or not isinstance(at_ms, int):
            return "bad_report"
        if not RECEIPT_MIN_MS <= at_ms <= int(time.time() * 1000) + RECEIPT_FUTURE_MS:
            return "bad_report"
        if not isinstance(endpoint_sha256, str) or _SHA256_HEX.match(endpoint_sha256) is None:
            return "unknown_device"
        if isinstance(ping_id, bool) or not isinstance(ping_id, int) or not 1 <= ping_id <= 2**63 - 1:
            return "unknown_ping"
        at = time.strftime(STAMP[:-1], time.gmtime(at_ms // 1000)) + f".{at_ms % 1000:03d}Z"
        with self.read() as c:
            match = None
            for row in c.execute("SELECT id, endpoint, device_id, journal FROM subscriptions WHERE retired_at IS NULL"):
                digest = hashlib.sha256(str(row["endpoint"]).encode("utf-8")).hexdigest()
                if hmac.compare_digest(digest, endpoint_sha256) and match is None:
                    match = dict(row)
        if match is None or not paired_device_live(match["journal"], match["device_id"] or ""):
            return "unknown_device"
        sid, device = match["id"], match["device_id"]
        with self.read() as c:
            got = _receipt_checks(c, sid, device, ping_id, kind, at)
        if got is not None:
            return got
        with self.write() as c:
            if c.execute("SELECT 1 FROM subscriptions WHERE id = ? AND device_id = ? AND retired_at IS NULL",
                         (sid, device)).fetchone() is None:
                return "unknown_device"
            got = _receipt_checks(c, sid, device, ping_id, kind, at)
            if got is not None:
                return got
            c.execute("INSERT INTO receipts (ping_id, device_id, kind, at, reported_at) VALUES (?,?,?,?,?)",
                      (ping_id, device, kind, at, now_stamp()))
            return "recorded"

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


def _receipt_checks(c: sqlite3.Connection, sid: int, device: str, ping_id: int, kind: str, at: str) -> str | None:
    """The answer a receipt gets before it is written (``unknown_ping``, ``duplicate``, ``limited``), or None when it may
    be appended: the ping was targeted at or delivered to subscription ``sid``, the report is new, and the caps hold."""
    if (c.execute("SELECT 1 FROM targets WHERE ping_id = ? AND subscription_id = ?", (ping_id, sid)).fetchone() is None
            and c.execute("SELECT 1 FROM deliveries WHERE ping_id = ? AND subscription_id = ?",
                          (ping_id, sid)).fetchone() is None):
        return "unknown_ping"
    if c.execute("SELECT 1 FROM receipts WHERE ping_id = ? AND device_id = ? AND kind = ? AND at = ?",
                 (ping_id, device, kind, at)).fetchone() is not None:
        return "duplicate"
    hour_ago = time.strftime(STAMP, time.gmtime(time.time() - 3600))
    if (c.execute("SELECT COUNT(*) FROM receipts WHERE device_id = ? AND reported_at >= ?",
                  (device, hour_ago)).fetchone()[0] >= MAX_RECEIPTS_PER_DEVICE_HOUR
            or c.execute("SELECT COUNT(*) FROM receipts WHERE ping_id = ? AND device_id = ? AND kind = ?",
                         (ping_id, device, kind)).fetchone()[0] >= MAX_RECEIPTS_PER_PING_KIND):
        return "limited"
    return None


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

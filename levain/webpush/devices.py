"""levain.webpush.devices: the installed phone app's own credential (PUSH_DESIGN, "The installed-app credential").

The phone link's token is minted per run of ``levain consent phone`` and travels in the URL fragment. A page added to an
iPhone's Home Screen opens its manifest's ``start_url`` with no address bar, so that token cannot reach it, and it would
die at the next restart anyway. Pairing bridges the two: a page holding a live token mints a ONE-TIME code, the code rides
the manifest's ``start_url`` into the installed app, and the app's first open trades it for a DEVICE token it keeps. A
page on the run token that turns pings on is given a device the same way, so every push subscription belongs to a
device a revoke can stop.

What a device token opens is what the run token opens (the phone server decides that, not this module): read the queue,
start a passkey challenge, answer a question with words. It never signs; every answer that authorises still needs the
passkey. It is revocable, and only its hash is stored, so a copy of ``webpush.db`` holds no usable token.

Every function takes an open ``sqlite3.Connection`` on the push store (``levain.webpush.store``), whose schema holds
``devices(id, label, token_hash, created, last_seen, revoked_at)`` and
``pairings(code_hash, device_label, expires, used_at)``.
"""
from __future__ import annotations

import hashlib
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

__all__ = ["PAIRING_TTL", "OPEN_PAIRINGS_KEPT", "LABEL_MAX", "mint_pairing", "redeem", "verify", "revoke",
           "list_devices"]

# Long enough to mint on the laptop's page, walk to the phone and add it to the Home Screen later in the day; short
# enough that a code left in an old manifest is dead by tomorrow.
PAIRING_TTL = timedelta(hours=24)
# Each "Install on this phone" tap mints one; only the newest few stay redeemable, so repeated taps never pile up codes.
OPEN_PAIRINGS_KEPT = 5
LABEL_MAX = 64
# last_seen is a display ("used 3h ago"); writing it on every request would turn each read into a write.
_SEEN_EVERY = timedelta(minutes=5)


def _hash(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def _stamp(t: datetime) -> str:
    return t.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse(s: Any) -> datetime | None:
    if not isinstance(s, str) or not s.endswith("Z"):
        return None
    try:
        return datetime.fromisoformat(s[:-1] + "+00:00")
    except ValueError:
        return None


def _label(label: Any) -> str:
    text = "".join(c for c in str(label or "") if c.isprintable()).strip()
    return text[:LABEL_MAX] or "phone"


def mint_pairing(conn: sqlite3.Connection, label: str | None, now: datetime) -> str:
    """A new one-time pairing code (128 bits, url-safe), stored only as its hash. Unused codes past the newest
    :data:`OPEN_PAIRINGS_KEPT` are expired in the same step."""
    code = secrets.token_urlsafe(16)
    with conn:
        conn.execute("INSERT INTO pairings (code_hash, device_label, expires, used_at) VALUES (?, ?, ?, NULL)",
                     (_hash(code), _label(label), _stamp(now + PAIRING_TTL)))
        conn.execute("UPDATE pairings SET expires = ? WHERE used_at IS NULL AND expires > ? AND code_hash NOT IN "
                     # newest by mint order: codes minted in one second share their expiry, so it cannot rank them
                     "(SELECT code_hash FROM pairings WHERE used_at IS NULL ORDER BY rowid DESC LIMIT ?)",
                     (_stamp(now), _stamp(now), OPEN_PAIRINGS_KEPT))
    return code


def _insert_device(conn: sqlite3.Connection, label: Any, now: datetime) -> tuple[str, str]:
    """A new device row in the caller's transaction; returns ``(device_id, device_token)``."""
    token = secrets.token_urlsafe(32)
    device_id = "d-" + secrets.token_hex(6)
    conn.execute("INSERT INTO devices (id, label, token_hash, created, last_seen, revoked_at) VALUES (?, ?, ?, ?, ?, NULL)",
                 (device_id, _label(label), _hash(token), _stamp(now), _stamp(now)))
    return device_id, token


def redeem(conn: sqlite3.Connection, code: Any, now: datetime) -> tuple[str, str] | None:
    """Trade a live pairing code for ``(device_id, device_token)``, once. The code is spent and the device created in
    one transaction, so two racing opens of the same code cannot both get a token. None for an unknown, spent or
    expired code (the three are not told apart)."""
    if not isinstance(code, str) or not 8 <= len(code) <= 64:
        return None
    conn.isolation_level = None   # explicit BEGIN IMMEDIATE below; restored before return
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute("SELECT device_label FROM pairings WHERE code_hash = ? AND used_at IS NULL AND expires > ?",
                               (_hash(code), _stamp(now))).fetchone()
            if row is None:
                conn.execute("ROLLBACK")
                return None
            conn.execute("UPDATE pairings SET used_at = ? WHERE code_hash = ?", (_stamp(now), _hash(code)))
            device_id, token = _insert_device(conn, row[0], now)
            conn.execute("COMMIT")
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
    finally:
        conn.isolation_level = ""
    return device_id, token


def verify(conn: sqlite3.Connection, token: Any, now: datetime) -> str | None:
    """The device id a live (unrevoked) device token belongs to, or None. The lookup is by the token's hash, so no
    comparison runs over the secret itself."""
    if not isinstance(token, str) or not 16 <= len(token) <= 128:
        return None
    row = conn.execute("SELECT id, last_seen FROM devices WHERE token_hash = ? AND revoked_at IS NULL",
                       (_hash(token),)).fetchone()
    if row is None:
        return None
    seen = _parse(row[1])
    if seen is None or now - seen >= _SEEN_EVERY:
        try:
            with conn:
                conn.execute("UPDATE devices SET last_seen = ? WHERE id = ?", (_stamp(now), row[0]))
        except sqlite3.OperationalError:
            pass                  # a busy store skips the display stamp; the device is still the device
    return row[0]


def revoke(conn: sqlite3.Connection, device_id: str, now: datetime) -> bool:
    """Revoke one device: its token stops opening anything and its push subscriptions are retired with it (a revoked
    phone gets no more pings). True when a live device was revoked."""
    with conn:
        cur = conn.execute("UPDATE devices SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL", (_stamp(now), device_id))
        if cur.rowcount != 1:
            return False
        from levain.webpush import store

        store.retire_device_subscriptions(conn, device_id, now)
    return True


def list_devices(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Every paired device, newest first, revoked ones included (said, never hidden)."""
    rows = conn.execute("SELECT id, label, created, last_seen, revoked_at FROM devices ORDER BY created DESC").fetchall()
    return [{"id": r[0], "label": r[1], "created": r[2], "last_seen": r[3], "revoked_at": r[4]} for r in rows]

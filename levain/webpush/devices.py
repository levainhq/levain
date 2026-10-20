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

An iPhone's Home Screen app keeps its own storage, apart from Safari's, and the manifest hand-off can fail to arrive.
The SHORT code is the path that always works: eight characters the operator types once into the installed app. It is
short enough to type, so it lives ten minutes, only the newest one is redeemable, and wrong guesses lock every short
redeem out for a while; with those three, a guesser gets a handful of tries at a 40-bit secret per code. Minting a code
clears the lock: a mint takes the run token or the laptop, which a guesser holding no credential lacks, so wrong
codes alone cannot keep a fresh short code from redeeming. They can still keep the phone server's own per-minute cap
on failed redeems full (it counts the lock's refusals and a mint does not reset it), which refuses short and long
redeems alike for as long as the peer keeps sending: an availability edge only, nothing is guessed through it.

Every function takes an open ``sqlite3.Connection`` on the push store (``levain.webpush.store``), whose schema holds
``devices(id, label, token_hash, created, last_seen, revoked_at)``,
``pairings(code_hash, device_label, expires, used_at)``,
``short_pairings(code_hash, device_label, expires, used_at)`` and ``redeem_failures(at)``.
"""
from __future__ import annotations

import hashlib
import math
import secrets
import sqlite3
import unicodedata
from datetime import datetime, timedelta, timezone
from typing import Any

__all__ = ["PAIRING_TTL", "OPEN_PAIRINGS_KEPT", "LABEL_MAX", "SHORT_PAIRING_TTL", "SHORT_MAX_FAILURES",
           "SHORT_LOCK_WINDOW", "SHORT_ALPHABET", "LOCKED", "mint_pairing", "redeem", "mint_short_pairing",
           "normalize_short", "display_short", "redeem_short", "verify", "revoke", "list_devices"]

# Long enough to mint on the laptop's page, walk to the phone and add it to the Home Screen later in the day; short
# enough that a code left in an old manifest is dead by tomorrow.
PAIRING_TTL = timedelta(hours=24)
# Each "Install on this phone" tap mints one; only the newest few stay redeemable, so repeated taps never pile up codes.
OPEN_PAIRINGS_KEPT = 5
LABEL_MAX = 64
# last_seen is a display ("used 3h ago"); writing it on every request would turn each read into a write.
_SEEN_EVERY = timedelta(minutes=5)

# The short code: long enough to walk from the laptop to the phone and type it, short enough that a code read over a
# shoulder is soon dead.
SHORT_PAIRING_TTL = timedelta(minutes=10)
# Wrong short codes allowed inside the sliding window before every short redeem is refused, a right code included:
# the lock is what makes a typeable code safe to accept from a caller holding no credential.
SHORT_MAX_FAILURES = 5
SHORT_LOCK_WINDOW = timedelta(minutes=10)
# Crockford base32: no I, L, O or U, so what is read off a screen is what gets typed
SHORT_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
SHORT_LENGTH = 8
LOCKED = "locked"
_LOOK_ALIKES = {"O": "0", "I": "1", "L": "1"}
# a hyphen is display only; a phone keyboard may hand over any of these for the one the operator typed
_DASHES = frozenset("-‐‑‒–—−")
_SHORT_INPUT_MAX = 64


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


def display_short(code: str) -> str:
    """A canonical short code as the operator reads it: ``ABCD-EFGH``."""
    return code[:4] + "-" + code[4:]


def mint_short_pairing(conn: sqlite3.Connection, label: str | None, now: datetime) -> str:
    """A new short pairing code (eight Crockford base32 characters, 40 bits, from :mod:`secrets`), stored only as its
    hash and returned for display as ``ABCD-EFGH``. Every short code still open dies in the same step, so only the
    newest one redeems; dead rows are dropped with them, since nothing can redeem them again.

    The wrong-code failures are cleared in the same step, so the new code redeems at once even while the lock is shut.
    Only a caller holding the run token or the laptop reaches this function; the redeem a caller with no credential
    makes never clears them.

    One ``BEGIN IMMEDIATE`` transaction holds the delete, the insert and the clear, so two racing mints leave one
    open code, never two."""
    for _ in range(8):
        code = "".join(secrets.choice(SHORT_ALPHABET) for _ in range(SHORT_LENGTH))
        conn.isolation_level = None   # explicit BEGIN IMMEDIATE below; restored before return
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute("DELETE FROM short_pairings WHERE used_at IS NULL OR expires <= ?", (_stamp(now),))
                conn.execute("INSERT INTO short_pairings (code_hash, device_label, expires, used_at) VALUES (?, ?, ?, NULL)",
                             (_hash(code), _label(label), _stamp(now + SHORT_PAIRING_TTL)))
                conn.execute("DELETE FROM redeem_failures")
                conn.execute("COMMIT")
            except sqlite3.IntegrityError:
                conn.execute("ROLLBACK")
                continue          # the same 40 bits as a spent, unexpired code: draw again
            except BaseException:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise
        finally:
            conn.isolation_level = ""
        return display_short(code)
    raise RuntimeError("no unused short pairing code could be drawn")


def normalize_short(text: Any) -> str | None:
    """The canonical eight-character form of a typed short code, or None when it cannot be one. Spaces and hyphens
    are dropped, and so are invisible format and control characters (a zero-width space, a direction mark, a soft
    hyphen: what a copy from Messages or Notes can carry along); case is folded, and the Crockford look-alikes are read
    as what they look like (O as 0, I and L as 1). Anything but ASCII letters and digits after that is not a code. The
    length cap is on the input as it arrived."""
    if not isinstance(text, str) or len(text) > _SHORT_INPUT_MAX:
        return None
    kept = "".join(c for c in text
                   if not c.isspace() and c not in _DASHES and unicodedata.category(c) not in ("Cf", "Cc"))
    if not kept.isascii():
        return None
    kept = "".join(_LOOK_ALIKES.get(c, c) for c in kept.upper())
    if len(kept) != SHORT_LENGTH or any(c not in SHORT_ALPHABET for c in kept):
        return None
    return kept


def redeem_short(conn: sqlite3.Connection, text: Any, now: datetime) -> "tuple[str, str] | tuple[str, int] | None":
    """Trade a typed short code for ``(device_id, device_token)``, once, or refuse it.

    ``(LOCKED, seconds_left)`` while :data:`SHORT_MAX_FAILURES` wrong codes fall inside the sliding
    :data:`SHORT_LOCK_WINDOW`: the code is not looked at, so a right code is refused too, and a refused try is not
    another failure. ``seconds_left`` runs until enough of those failures age out to open the lock (at least 1). None
    for an unknown, spent or expired code (not told apart), and that try is recorded as a failure; input that cannot
    be a code at all is refused without a lookup and is not one. A success leaves the failures where they are: only a
    mint (:func:`mint_short_pairing`) clears them.

    One ``BEGIN IMMEDIATE`` transaction holds the lock check, the lookup, the spend and the new device, so two racing
    redeems of one code make one device, and racing wrong guesses cannot both slip under the lock."""
    code = normalize_short(text)
    if code is None:
        return None
    cutoff = now - SHORT_LOCK_WINDOW
    conn.isolation_level = None   # explicit BEGIN IMMEDIATE below; restored before return
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            # the stamp is whole seconds, so the SQL bound is loose by under one and the exact test is made here
            stamps = (_parse(r[0]) for r in conn.execute("SELECT at FROM redeem_failures WHERE at >= ?", (_stamp(cutoff),)))
            recent = sorted(t for t in stamps if t is not None and t > cutoff)
            if len(recent) >= SHORT_MAX_FAILURES:
                conn.execute("ROLLBACK")
                opens = recent[len(recent) - SHORT_MAX_FAILURES] + SHORT_LOCK_WINDOW
                return LOCKED, max(1, math.ceil((opens - now).total_seconds()))
            row = conn.execute("SELECT device_label FROM short_pairings WHERE code_hash = ? AND used_at IS NULL "
                               "AND expires > ?", (_hash(code), _stamp(now))).fetchone()
            if row is None:
                conn.execute("DELETE FROM redeem_failures WHERE at < ?", (_stamp(cutoff),))
                conn.execute("INSERT INTO redeem_failures (at) VALUES (?)", (_stamp(now),))
                conn.execute("COMMIT")
                return None
            conn.execute("UPDATE short_pairings SET used_at = ? WHERE code_hash = ?", (_stamp(now), _hash(code)))
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

"""levain.cockpit.keyfacts: what a page needs to know about an enrolled passkey, which is no authority at all.

A passkey's credential id lets a page ask the browser for exactly that passkey (``allowCredentials``), so a browser that
has never used it (a fresh one, a re-paired one) offers it at once. Its AAGUID names the provider that holds it
(iCloud Keychain, Google Password Manager, ...), which only the Devices / keys view shows.

Neither is authority. Who may sign, and what, is the signed authority log's replay (``levain.authority``) and the class
it gives each key; nothing in that replay, the consent hook or a verifier reads this store. A fact is held in
``key_facts.db`` beside ``cockpit.db``, keyed by the key's fingerprint, so the log and the signers file the broker
writes keep their exact format.

A fact is written only by the operator's enrol command or beside a verified signature by that key's holder:
  - at enrolment (:func:`record_enrolment`), from the creation's authenticatorData (:func:`attested`), whose attested
    public key must be the enrolled key and whose rpIdHash must be its application, in the call that signed the
    enrol link; or by a rerun of that enrol for the key already enrolled (:func:`repair_enrolment`), which fills a
    missing row and never overwrites a different id;
  - on a passkey signature (:func:`learn`), the credential id the browser reported with an assertion that verified
    under that key, and never over a different id already held.
A wrong fact can only make a page ask for the wrong credential (the browser then offers none) or name the wrong
provider: it never widens what a key may sign.
"""
from __future__ import annotations

import base64
import hashlib
import sqlite3
import struct
from dataclasses import dataclass
from pathlib import Path

__all__ = ["DB_NAME", "PROVIDERS", "Attested", "KeyFacts", "KeyFactsError", "attested", "learn", "provider_name",
           "provider_text", "raw_id_bytes", "read", "record_enrolment", "repair_enrolment"]

DB_NAME = "key_facts.db"
_ZERO = "00000000-0000-0000-0000-000000000000"
_AT = 0x40                          # authenticatorData flag: attested credential data present
_ED = 0x80                          # authenticatorData flag: an extensions map follows the credential
_HEAD = 32 + 1 + 4                  # rpIdHash || flags || signCount
_CRED_MAX = 1023                    # WebAuthn's longest credential id
_CRED_MIN = 16                      # WebAuthn asks an authenticator for an id of at least 16 bytes
_CRED_TEXT_MAX = -(-_CRED_MAX // 3) * 4     # the longest id's base64url, padded: no longer text decodes to an id
_FIELD_MAX = 16 * 1024              # a real creation's authenticatorData is a few hundred bytes

# AAGUID -> provider, from github.com/passkeydeveloper/passkey-authenticator-aaguids (aaguid.json, read 2026-10-25).
# Display only. An all-zero AAGUID names nothing: a browser or provider may send zeros.
PROVIDERS: dict[str, str] = {
    "fbfc3007-154e-4ecc-8c0b-6e020557d7bd": "iCloud Keychain (Apple Passwords)",
    "dd4ec289-e01d-41c9-bb89-70fa845d4bf2": "iCloud Keychain (Managed)",
    "ea9b8d66-4d01-1d21-3ce4-b6b48cb575d4": "Google Password Manager",
    "adce0002-35bc-c60a-648b-0b25f1f05503": "Chrome on Mac",
    "08987058-cadc-4b81-b6e1-30de50dcbe96": "Windows Hello",
    "9ddd1817-af5a-4672-a2b9-3e3dd95000a9": "Windows Hello",
    "6028b017-b1d4-4c02-b4b3-afcdafc96bb2": "Windows Hello",
    "d3452668-01fd-4c12-926c-83a4204853aa": "Microsoft Password Manager",
    "bada5566-a7aa-401f-bd96-45619a55120d": "1Password",
    "d548826e-79b4-db40-a3d8-11116f7e8349": "Bitwarden",
    "531126d6-e717-415c-9320-3d9aa6981239": "Dashlane",
    "50726f74-6f6e-5061-7373-50726f746f6e": "Proton Pass",
    "53414d53-554e-4700-0000-000000000000": "Samsung Pass",
    "fdb141b2-5d84-443e-8a35-4698c205a502": "KeePassXC",
    "f3809540-7f14-49c1-a8b3-8f813b225541": "Enpass",
    "b84e4048-15dc-4dd0-8640-f4f60813c8af": "NordPass",
    "0ea242b4-43c4-4a1b-8b17-dd6d0b6baec6": "Keeper",
}


class KeyFactsError(Exception):
    """Refused attested data, or a store that cannot be read; the message is the operator-facing reason."""


@dataclass(frozen=True)
class Attested:
    """What a creation's authenticatorData says about the credential it made."""
    credential_id: bytes
    aaguid: str


@dataclass(frozen=True)
class KeyFacts:
    fingerprint: str
    credential_id: str | None       # base64url, no padding: what a page puts in allowCredentials
    aaguid: str | None
    source: str                     # "enrol" or "signature"
    recorded_at: str


def _b64u(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def raw_id_bytes(text: object) -> bytes:
    """A browser's ``rawId`` (base64url) as bytes, refused unless it is a credential id's length."""
    from levain.consent.passkey import PasskeyError, b64url_decode

    if isinstance(text, str) and len(text) > _CRED_TEXT_MAX:
        raise KeyFactsError(f"raw_id is {len(text)} characters; a credential id's base64url is at most "
                            f"{_CRED_TEXT_MAX}")
    try:
        raw = b64url_decode(text, "raw_id")
    except PasskeyError as exc:
        raise KeyFactsError(str(exc)) from None
    if not _CRED_MIN <= len(raw) <= _CRED_MAX:
        raise KeyFactsError(f"raw_id is {len(raw)} bytes; a credential id is {_CRED_MIN} to {_CRED_MAX}")
    return raw


def _aaguid_text(raw: bytes) -> str:
    h = raw.hex()
    return f"{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:]}"


def attested(authenticator_data: bytes, public_key: str) -> Attested:
    """The credential id and AAGUID in a passkey creation's ``authenticatorData``, refused unless it attests exactly
    ``public_key`` (an ``sk-ecdsa`` passkey line, :func:`levain.consent.passkey.public_key_line`): its rpIdHash is the
    key's application, its attested-credential flag is set, and the credential public key (decoded as one COSE map
    at its offset, :func:`_cose_point`) is an ES256 P-256 key whose x and y are the enrolled point. Nothing after that
    map is read: with the extensions flag clear, any byte there refuses the creation."""
    from levain.consent.passkey import PasskeyError, _key_parts

    if not isinstance(authenticator_data, bytes) or len(authenticator_data) > _FIELD_MAX:
        raise KeyFactsError("the attested authenticatorData is not bytes of a creation's size")
    try:
        blob, application = _key_parts(public_key)
    except PasskeyError as exc:
        raise KeyFactsError(f"only a passkey's creation is attested here: {exc}") from None
    (n,) = struct.unpack(">I", blob[:4])
    at = 4 + n
    (n,) = struct.unpack(">I", blob[at:at + 4])
    at += 4 + n
    (n,) = struct.unpack(">I", blob[at:at + 4])
    point = blob[at + 4:at + 4 + n]
    if len(point) != 65 or point[0] != 0x04:
        raise KeyFactsError("the enrolled key's point is not an uncompressed P-256 point")
    x, y = point[1:33], point[33:]
    d = authenticator_data
    if len(d) < _HEAD + 16 + 2:
        raise KeyFactsError("the attested authenticatorData is too short to carry a credential")
    if d[:32] != hashlib.sha256(application.encode("ascii")).digest():
        raise KeyFactsError(f"the attested authenticatorData was made for another name than {application}")
    if not d[32] & _AT:
        raise KeyFactsError("the attested authenticatorData carries no attested credential (it is an assertion's)")
    aaguid = d[_HEAD:_HEAD + 16]
    (size,) = struct.unpack(">H", d[_HEAD + 16:_HEAD + 18])
    start = _HEAD + 18
    if not _CRED_MIN <= size <= _CRED_MAX or start + size > len(d):
        raise KeyFactsError("the attested credential id is not a credential id's length")
    cx, cy, end = _cose_point(d, start + size)
    if (cx, cy) != (x, y):
        raise KeyFactsError("the attested credential's public key is not the key being enrolled")
    if not d[32] & _ED and end != len(d):
        raise KeyFactsError("the attested authenticatorData has trailing bytes after its credential public key")
    return Attested(credential_id=d[start:start + size], aaguid=_aaguid_text(aaguid))


def _cbor_head(d: bytes, i: int) -> tuple[int, int, int]:
    """One CBOR item head at ``d[i]``: (major type, argument, offset after the head). Only the 0-, 1- and 2-byte
    argument forms a COSE EC2 key uses are read."""
    if i >= len(d):
        raise KeyFactsError("the attested credential public key is cut short")
    major, ai = d[i] >> 5, d[i] & 0x1F
    if ai < 24:
        return major, ai, i + 1
    width = {24: 1, 25: 2}.get(ai)
    if width is None or i + 1 + width > len(d):
        raise KeyFactsError("the attested credential public key is not a COSE key this cockpit reads")
    return major, int.from_bytes(d[i + 1:i + 1 + width], "big"), i + 1 + width


def _cbor_value(d: bytes, i: int) -> tuple[int | bytes, int]:
    """An integer (major 0 or 1) or a byte string (major 2) at ``d[i]``, and the offset after it."""
    major, arg, i = _cbor_head(d, i)
    if major == 0:
        return arg, i
    if major == 1:
        return -1 - arg, i
    if major == 2 and i + arg <= len(d):
        return d[i:i + arg], i + arg
    raise KeyFactsError("the attested credential public key is not a COSE key this cockpit reads")


def _cose_point(d: bytes, i: int) -> tuple[bytes, bytes, int]:
    """The x and y of the COSE_Key map at ``d[i]``, refused unless it is EC2 (kty 2), ES256 (alg -7) on P-256
    (crv 1) with 32-byte coordinates; and the offset after the map. A label held twice is refused."""
    major, count, i = _cbor_head(d, i)
    if major != 5 or not 5 <= count <= 16:
        raise KeyFactsError("the attested credential public key is not a COSE key map")
    fields: dict[int, int | bytes] = {}
    for _ in range(count):
        label, i = _cbor_value(d, i)
        if not isinstance(label, int) or label in fields:
            raise KeyFactsError("the attested credential public key is not a COSE key this cockpit reads")
        fields[label], i = _cbor_value(d, i)
    x, y = fields.get(-2), fields.get(-3)
    if (fields.get(1), fields.get(3), fields.get(-1)) != (2, -7, 1) or not (
            isinstance(x, bytes) and isinstance(y, bytes) and len(x) == len(y) == 32):
        raise KeyFactsError("the attested credential public key is not an ES256 P-256 key")
    return x, y, i


def provider_name(aaguid: str | None) -> str | None:
    """The provider :data:`PROVIDERS` lists for an AAGUID, else None: absent, all zeros, or unlisted. Never a guess."""
    if aaguid is None or aaguid == _ZERO:
        return None
    return PROVIDERS.get(aaguid)


def provider_text(facts: KeyFacts | None) -> str:
    """The provider line for a key view: always a sentence, never a blank that reads as fine."""
    if facts is None or facts.source != "enrol":
        return "provider not recorded (only an enrolment that sends its creation names it)"
    if facts.aaguid is None:
        return "provider not named (no AAGUID recorded)"
    if facts.aaguid == _ZERO:
        return "provider not named (the passkey's AAGUID is all zeros)"
    return provider_name(facts.aaguid) or f"unknown provider (AAGUID {facts.aaguid})"


def _connect(root: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(Path(root) / DB_NAME), timeout=5)
    conn.execute("CREATE TABLE IF NOT EXISTS key_facts (fingerprint TEXT PRIMARY KEY, credential_id TEXT, "
                 "aaguid TEXT, source TEXT NOT NULL, recorded_at TEXT NOT NULL)")
    return conn


def record_enrolment(root: Path, fingerprint: str, facts: Attested, *, now: str) -> None:
    """Hold ``facts`` for the key just enrolled under ``fingerprint`` (by a link this caller signed)."""
    conn = _connect(root)
    try:
        with conn:
            conn.execute("INSERT OR REPLACE INTO key_facts VALUES (?, ?, ?, 'enrol', ?)",
                         (fingerprint, _b64u(facts.credential_id), facts.aaguid, now))
    finally:
        conn.close()


def repair_enrolment(root: Path, fingerprint: str, facts: Attested, *, now: str) -> bool:
    """Hold ``facts`` for an already-enrolled key when its row is missing, or holds the same credential id without
    the enrolment's AAGUID (so rerunning an enrol whose facts were never written recovers them). True when it wrote.
    KeyFactsError when another credential id is held: a held id is never overwritten."""
    want = _b64u(facts.credential_id)
    conn = _connect(root)
    try:
        with conn:
            # the write lock before the read: an id learned meanwhile waits on it (or times out), never lands between
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT credential_id, aaguid, source FROM key_facts WHERE fingerprint = ?",
                               (fingerprint,)).fetchone()
            if row is not None and row[0] is not None and row[0] != want:
                raise KeyFactsError(f"{fingerprint} already holds another credential id ({row[0]}); a held id is "
                                    "never overwritten")
            if row == (want, facts.aaguid, "enrol"):
                return False
            conn.execute("INSERT OR REPLACE INTO key_facts VALUES (?, ?, ?, 'enrol', ?)",
                         (fingerprint, want, facts.aaguid, now))
            return True
    finally:
        conn.close()


def learn(root: Path, fingerprint: str, raw_id: bytes, *, now: str) -> bool:
    """Hold ``raw_id`` as ``fingerprint``'s credential id, reported with an assertion that verified under that key.
    Written only when the key has no id held; True when it wrote. An id that differs from the one held is kept out."""
    if not _CRED_MIN <= len(raw_id) <= _CRED_MAX:
        return False
    conn = _connect(root)
    try:
        with conn:
            cur = conn.execute("INSERT OR IGNORE INTO key_facts VALUES (?, ?, NULL, 'signature', ?)",
                               (fingerprint, _b64u(raw_id), now))
            return cur.rowcount == 1
    finally:
        conn.close()


def read(root: Path) -> dict[str, KeyFacts]:
    """Every held fact by fingerprint; empty when nothing was ever recorded. KeyFactsError when the store is there
    and cannot be read."""
    path = Path(root).absolute() / DB_NAME      # as_uri() refuses a relative path
    if not path.exists():
        return {}
    try:
        conn = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True, timeout=5)
        try:
            rows = conn.execute("SELECT fingerprint, credential_id, aaguid, source, recorded_at FROM key_facts").fetchall()
        finally:
            conn.close()
    except (sqlite3.Error, ValueError) as exc:
        raise KeyFactsError(f"the key facts in {path} cannot be read: {exc}") from None
    return {r[0]: KeyFacts(*r) for r in rows}

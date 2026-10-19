"""Web Push message encryption (RFC 8291, the RFC 8188 ``aes128gcm`` record) and VAPID (RFC 8292).

The only ``cryptography`` use in the push package. One record per message: a payload that does not fit one
record is refused rather than split, because the push services cap a message well below a second record.
"""
from __future__ import annotations

import base64
import json
import os
import struct
import time
from urllib.parse import urlsplit

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

RECORD_SIZE = 4096
_TAG = 16
_HEADER = 16 + 4 + 1 + 65            # salt, record size, key-id length, the sender's 65-byte public point = 86
# RFC 8030 section 7.2: a push service accepts a request body of 4096 bytes. The body is the header plus one
# ciphertext record (plaintext + one delimiter byte + the 16-byte tag), so the plaintext ceiling follows.
MAX_REQUEST_BODY = 4096
MAX_PLAINTEXT = MAX_REQUEST_BODY - _HEADER - _TAG - 1   # 3993
VAPID_LIFETIME = 12 * 3600           # RFC 8292 caps a JWT at 24 h; half that leaves room for clock skew


def b64e(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def b64d(text: str) -> bytes:
    if not isinstance(text, str) or not text.isascii():
        raise ValueError("not base64url")
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def public_bytes(key: ec.EllipticCurvePrivateKey) -> bytes:
    """The uncompressed X9.62 point (65 bytes) of ``key``'s public half."""
    return key.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)


def _hkdf(salt: bytes, ikm: bytes, info: bytes, length: int) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=length, salt=salt, info=info).derive(ikm)


def encrypt(plaintext: bytes, ua_public: bytes, auth_secret: bytes, *, _salt: bytes | None = None,
            _sender: ec.EllipticCurvePrivateKey | None = None) -> bytes:
    """The request body for one push message to the user agent holding ``ua_public`` (65-byte point) and
    ``auth_secret`` (16 bytes). ``_salt`` and ``_sender`` are a seam for the RFC's worked example only: a real
    send draws a fresh salt and a fresh ephemeral key every time."""
    if len(plaintext) > MAX_PLAINTEXT:
        raise ValueError(f"payload of {len(plaintext)} bytes does not fit one push request ({MAX_PLAINTEXT})")
    if len(auth_secret) != 16:
        raise ValueError("the auth secret must be 16 bytes")
    peer = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), ua_public)   # refuses a point off the curve
    sender = _sender if _sender is not None else ec.generate_private_key(ec.SECP256R1())
    salt = _salt if _salt is not None else os.urandom(16)
    if len(salt) != 16:
        raise ValueError("the salt must be 16 bytes")
    as_public = public_bytes(sender)
    shared = sender.exchange(ec.ECDH(), peer)
    ikm = _hkdf(auth_secret, shared, b"WebPush: info\x00" + ua_public + as_public, 32)
    cek = _hkdf(salt, ikm, b"Content-Encoding: aes128gcm\x00", 16)
    nonce = _hkdf(salt, ikm, b"Content-Encoding: nonce\x00", 12)
    record = AESGCM(cek).encrypt(nonce, plaintext + b"\x02", None)   # 0x02: the last record's delimiter
    return salt + struct.pack(">IB", RECORD_SIZE, len(as_public)) + as_public + record


def vapid_aud(endpoint: str) -> str:
    """The VAPID ``aud`` of ``endpoint``: ``https://`` + the lowercase host, and the port only when it is not 443.
    The one place it is built; the JWT claim and the token cache key both come from here."""
    parts = urlsplit(endpoint)
    if parts.scheme.lower() != "https" or not parts.hostname:
        raise ValueError("a push endpoint is an https URL")
    port = parts.port
    host = parts.hostname.lower()
    return f"https://{host}" if port in (None, 443) else f"https://{host}:{port}"


def vapid_headers(key: ec.EllipticCurvePrivateKey, endpoint: str, sub: str, *, _now: float | None = None) -> dict[str, str]:
    """The ``Authorization`` header for a push to ``endpoint`` (RFC 8292 scheme ``vapid``). ``aud`` is the
    endpoint's origin; ``sub`` is the contact claim."""
    aud = vapid_aud(endpoint)
    now = time.time() if _now is None else _now
    head = b64e(json.dumps({"typ": "JWT", "alg": "ES256"}, separators=(",", ":")).encode())
    claims = b64e(json.dumps({"aud": aud, "exp": int(now) + VAPID_LIFETIME, "sub": sub},
                             separators=(",", ":")).encode())
    signing_input = f"{head}.{claims}".encode("ascii")
    r, s = decode_dss_signature(key.sign(signing_input, ec.ECDSA(hashes.SHA256())))
    jwt = f"{head}.{claims}.{b64e(r.to_bytes(32, 'big') + s.to_bytes(32, 'big'))}"
    return {"Authorization": f"vapid t={jwt}, k={b64e(public_bytes(key))}"}

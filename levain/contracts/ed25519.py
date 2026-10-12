"""The one Ed25519 verdict in levain (F2), and this package's only ``cryptography`` import.

levain's Ed25519 profile is "the Go/OpenSSL row": exactly the 12 verdicts the published speccheck table
(https://eprint.iacr.org/2020/1244, https://github.com/novifinancial/ed25519-speccheck) gives Go
``crypto/ed25519``, OpenSSL 3.0, BoringSSL and PyCA. No pre-check and no ZIP-215 sit in front of the
library: ``tests/test_contracts_ed25519.py`` asserts that this module reproduces the row, so a wheel that
changes it turns CI red. A ``cryptography`` built against another backend is untested (the row test is the
detector). F2 note signatures, F4 cosignatures, F12 SSHSIG and F13 all verify through ``verify``.
"""
from __future__ import annotations

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

PUBLIC_KEY_LEN = 32
SIGNATURE_LEN = 64
SEED_LEN = 32

# The pinned row, one verdict per speccheck vector 0..11 (True = valid).
PROFILE_ROW = (True, True, True, True, False, False, False, False, False, False, False, True)


def verify(pub: bytes, msg: bytes, sig: bytes) -> bool:
    """True when ``sig`` is a valid Ed25519 signature of ``msg`` under ``pub`` per the pinned profile."""
    if not isinstance(pub, bytes) or len(pub) != PUBLIC_KEY_LEN:
        return False
    if not isinstance(sig, bytes) or len(sig) != SIGNATURE_LEN:
        return False
    try:
        key = Ed25519PublicKey.from_public_bytes(pub)
        key.verify(sig, msg)
    except (InvalidSignature, ValueError):
        return False
    return True


def public_from_seed(seed: bytes) -> bytes:
    key = Ed25519PrivateKey.from_private_bytes(seed)
    return key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)


def sign(seed: bytes, msg: bytes) -> bytes:
    """Sign with a 32-byte seed. For logs whose key is held in-process and for tests; ssh-agent keys use keys.py."""
    return Ed25519PrivateKey.from_private_bytes(seed).sign(msg)

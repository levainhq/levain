"""F12: SSHSIG verification for ``device_sig``, in-process (OpenSSH ``PROTOCOL.sshsig``), never by ``ssh-keygen``.

A ``device_sig`` is the raw SSHSIG blob (§2), no armor::

    byte[6] "SSHSIG" · uint32 1 · string publickey · string namespace · string reserved · string hash_algorithm ·
    string signature

levain's profile on top of the spec: namespace ``levain-team-entry``, reserved empty, hash algorithm ``sha512``,
public key ``ssh-ed25519`` whose F7 fingerprint equals ``device_fp``, and a signature blob
``string("ssh-ed25519") || string(sig)`` with a 64-byte ``sig``. ``sha256``, a non-empty reserved field and any
other key type are refused although the spec allows them. The outer blob, the public-key blob and the signature
blob are each read to their last byte: a trailing byte would give one signed entry two leaf hashes.

The signed data (§3) is ``"SSHSIG" || string(namespace) || string("") || string("sha512") || string(SHA-512(msg))``
and its Ed25519 verdict comes from ``ed25519.verify``, the one pinned profile (F2). Only Ed25519 device lines
freeze in S1; P-256 device lines arrive under P11 as a new ``(line, v)``.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass

from . import ContractError
from . import ed25519
from .keys import ED25519_KEY_TYPE, ed25519_pub_from_blob, fp_of_blob, read_string, ssh_string

MAGIC = b"SSHSIG"
SIG_VERSION = 1
HASH_ALGORITHM = "sha512"


@dataclass(frozen=True)
class SshSig:
    public_key: bytes      # the embedded public-key wire blob
    namespace: str
    reserved: bytes
    hash_algorithm: str
    signature: bytes       # the embedded signature wire blob


def parse(blob: bytes) -> SshSig:
    """Split a raw SSHSIG blob into its §2 fields, every byte consumed. Applies no profile rule."""
    if not isinstance(blob, bytes):
        raise ContractError("an SSHSIG blob is bytes")
    if blob[:len(MAGIC)] != MAGIC:
        raise ContractError("an SSHSIG blob starts with the magic 'SSHSIG'")
    off = len(MAGIC)
    if off + 4 > len(blob):
        raise ContractError("an SSHSIG blob is truncated before its version")
    version = int.from_bytes(blob[off:off + 4], "big")
    off += 4
    if version != SIG_VERSION:
        raise ContractError(f"SSHSIG version {version}, not {SIG_VERSION}")
    public_key, off = read_string(blob, off)
    namespace, off = read_string(blob, off)
    reserved, off = read_string(blob, off)
    hash_algorithm, off = read_string(blob, off)
    signature, off = read_string(blob, off)
    if off != len(blob):
        raise ContractError("trailing bytes after the SSHSIG blob")
    try:
        ns, alg = namespace.decode("ascii"), hash_algorithm.decode("ascii")
    except UnicodeDecodeError:
        raise ContractError("an SSHSIG namespace and hash algorithm are ASCII") from None
    return SshSig(public_key, ns, reserved, alg, signature)


def signed_data(namespace: str, message: bytes, hash_algorithm: str = HASH_ALGORITHM) -> bytes:
    """PROTOCOL.sshsig §3: the bytes the key signs."""
    if hash_algorithm != HASH_ALGORITHM:
        raise ContractError(f"levain signs SSHSIG with {HASH_ALGORITHM} only")
    return (MAGIC + ssh_string(namespace.encode("ascii")) + ssh_string(b"") + ssh_string(hash_algorithm.encode("ascii"))
            + ssh_string(hashlib.sha512(message).digest()))


def _signature_bytes(sig_blob: bytes) -> bytes:
    """The 64 bytes inside ``string("ssh-ed25519") || string(sig)``, every byte consumed (the F13 unwrap)."""
    alg, off = read_string(sig_blob, 0)
    if alg != ED25519_KEY_TYPE.encode():
        raise ContractError(f"the SSHSIG signature is {alg!r}, not {ED25519_KEY_TYPE}")
    sig, off = read_string(sig_blob, off)
    if len(sig) != ed25519.SIGNATURE_LEN:
        raise ContractError("an Ed25519 signature is 64 bytes")
    if off != len(sig_blob):
        raise ContractError("trailing bytes after the SSHSIG signature blob")
    return sig


def check_profile(sig: SshSig, *, namespace: str, device_fp: str) -> tuple[bytes, bytes]:
    """levain's F12 profile over a parsed blob. Returns (Ed25519 public key, 64-byte signature)."""
    if sig.namespace != namespace:
        raise ContractError(f"SSHSIG namespace {sig.namespace!r}, not {namespace!r}")
    if sig.reserved != b"":
        raise ContractError("the SSHSIG reserved field is not empty")
    if sig.hash_algorithm != HASH_ALGORITHM:
        raise ContractError(f"SSHSIG hash algorithm {sig.hash_algorithm!r}, not {HASH_ALGORITHM}")
    pub = ed25519_pub_from_blob(sig.public_key)    # ssh-ed25519 only, 32 bytes, no trailing byte
    if fp_of_blob(sig.public_key) != device_fp:
        raise ContractError("the SSHSIG public key is not the leaf's device_fp")
    return pub, _signature_bytes(sig.signature)


def verify(blob: bytes, *, namespace: str, device_fp: str, message: bytes) -> None:
    """Verify a raw SSHSIG blob over ``message`` under levain's profile. Raises ``ContractError``; returns None."""
    if not isinstance(message, bytes):
        raise ContractError("an SSHSIG message is bytes")
    pub, sig = check_profile(parse(blob), namespace=namespace, device_fp=device_fp)
    if not ed25519.verify(pub, signed_data(namespace, message), sig):
        raise ContractError("the SSHSIG signature does not verify")

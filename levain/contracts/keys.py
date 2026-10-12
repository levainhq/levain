"""F7 key fingerprints and F13 the ssh-agent signature unwrap.

``fp_of_blob(blob) = "SHA256:" + base64(SHA-256(blob))`` without ``=`` padding: what ``ssh-keygen -lf`` prints
for a plain OpenSSH public key of any type. Which key types a consumer accepts is the consumer's rule (T's
``team/signing.py`` keeps its allowlist and calls ``fp_of_blob`` after it); certificates are outside F7.

F13: an ssh-agent's sign response for an Ed25519 key is ``string("ssh-ed25519") || string(sig)``; the
note signer requires that algorithm string and a 64-byte ``sig`` and keeps only those 64 bytes.
"""
from __future__ import annotations

import base64
import hashlib
import struct

from . import ContractError
from .grammar import b64decode

ED25519_KEY_TYPE = "ssh-ed25519"
# Equal to ed25519.PUBLIC_KEY_LEN / SIGNATURE_LEN (a test asserts it); not imported, so importing this module
# (as team/signing.py does) never loads `cryptography`.
PUBLIC_KEY_LEN = 32
SIGNATURE_LEN = 64
_CERT_SUFFIX = "-cert-v01@openssh.com"


def read_string(blob: bytes, off: int) -> tuple[bytes, int]:
    """One SSH wire ``string``: uint32 big-endian byte length, then that many bytes."""
    if off + 4 > len(blob):
        raise ContractError("truncated before an SSH string length")
    (n,) = struct.unpack(">I", blob[off:off + 4])
    off += 4
    if off + n > len(blob):
        raise ContractError("truncated inside an SSH string")
    return blob[off:off + n], off + n


def ssh_string(b: bytes) -> bytes:
    return struct.pack(">I", len(b)) + b


def fp_of_blob(blob: bytes) -> str:
    if not isinstance(blob, bytes):
        raise ContractError("a key blob is bytes")
    return "SHA256:" + base64.b64encode(hashlib.sha256(blob).digest()).decode("ascii").rstrip("=")


def parse_pubkey_line(line: str) -> tuple[str, bytes]:
    """``<type> <base64 blob> [comment]`` -> (type, blob); the blob's embedded type must equal the declared one."""
    if not isinstance(line, str):
        raise ContractError("a public key line is text")
    fields = line.strip().split()
    if len(fields) < 2:
        raise ContractError("a public key line is '<type> <base64> [comment]'")
    declared, b64 = fields[0], fields[1]
    if declared.endswith(_CERT_SUFFIX):
        raise ContractError("certificates are outside F7")
    blob = b64decode(b64)   # canonical only: non-zero pad bits are refused, as ssh-keygen refuses them
    embedded, _ = read_string(blob, 0)
    if embedded != declared.encode("utf-8"):
        raise ContractError(f"the line declares {declared} but the blob says {embedded!r}")
    return declared, blob


def fingerprint_line(line: str) -> str:
    return fp_of_blob(parse_pubkey_line(line)[1])


def ed25519_blob(pub: bytes) -> bytes:
    if len(pub) != PUBLIC_KEY_LEN:
        raise ContractError("an Ed25519 public key is 32 bytes")
    return ssh_string(ED25519_KEY_TYPE.encode()) + ssh_string(pub)


def ed25519_pub_from_blob(blob: bytes) -> bytes:
    """The 32-byte key inside ``string("ssh-ed25519") || string(pub32)``, every byte consumed (F13)."""
    kind, off = read_string(blob, 0)
    if kind != ED25519_KEY_TYPE.encode():
        raise ContractError(f"not an {ED25519_KEY_TYPE} key blob")
    pub, off = read_string(blob, off)
    if len(pub) != PUBLIC_KEY_LEN:
        raise ContractError("an Ed25519 public key is 32 bytes")
    if off != len(blob):
        raise ContractError("trailing bytes after the key blob")
    return pub


def unwrap_agent_signature(blob: bytes) -> bytes:
    """The 64 signature bytes inside an agent's ``string("ssh-ed25519") || string(sig)``, every byte consumed."""
    alg, off = read_string(blob, 0)
    if alg != ED25519_KEY_TYPE.encode():
        raise ContractError(f"the agent signed with {alg!r}, not {ED25519_KEY_TYPE}")
    sig, off = read_string(blob, off)
    if len(sig) != SIGNATURE_LEN:
        raise ContractError("an Ed25519 signature is 64 bytes")
    if off != len(blob):
        raise ContractError("trailing bytes after the signature blob")
    return sig

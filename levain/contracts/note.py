"""F2: signed notes, C2SP signed-note@v1.1.0, Ed25519 (signature type 0x01).

A note is text ending in ``\\n``, a blank line, then one or more lines ``— <name> <base64(keyID || sig)>\\n``
(U+2014, space). The text is separated from the signatures by the LAST empty line. Signatures from unknown
keys are ignored "even if they share a name or ID (but not both) with a known key"; a known key whose
signature fails rejects the whole note (the spec's SHOULD, taken); no verifying known key rejects the note.

levain's bound: at most 16 signature lines (the spec's floor), each line at most 8 KiB; no aggregate cap.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Iterable

from . import ContractError
from . import ed25519
from .grammar import b64decode, b64encode

SIG_TYPE_ED25519 = 0x01
SIG_TYPE_COSIG_V1 = 0x04
MAX_SIGNATURE_LINES = 16
MAX_SIGNATURE_LINE_BYTES = 8 * 1024   # UTF-8 bytes of one signature line, its "\n" excluded
KEY_ID_LEN = 4
DASH = "— "
# Unicode White_Space (the set Go's unicode.IsSpace tests, which the spec's "Unicode spaces" means upstream).
_WHITE_SPACE = frozenset(
    [*range(0x09, 0x0E), 0x20, 0x85, 0xA0, 0x1680, *range(0x2000, 0x200B), 0x2028, 0x2029, 0x202F, 0x205F, 0x3000]
)


def valid_key_name(name: str) -> bool:
    """signed-note: "Key names MUST be non-empty and MUST NOT contain any Unicode spaces or plus (U+002B)"."""
    return (isinstance(name, str) and name != "" and "+" not in name
            and not any(ord(c) in _WHITE_SPACE or ord(c) < 0x20 for c in name))


def key_id(name: str, sig_type: int, pub: bytes) -> bytes:
    """SHA-256(key name || 0x0A || signature type || public key)[:4]."""
    return hashlib.sha256(name.encode("utf-8") + b"\n" + bytes([sig_type]) + pub).digest()[:KEY_ID_LEN]


@dataclass(frozen=True)
class VerifierKey:
    name: str
    key_id: bytes
    sig_type: int
    pub: bytes

    def vkey(self) -> str:
        return f"{self.name}+{self.key_id.hex()}+{b64encode(bytes([self.sig_type]) + self.pub)}"


def make_key(name: str, pub: bytes, sig_type: int = SIG_TYPE_ED25519) -> VerifierKey:
    if not valid_key_name(name):
        raise ContractError(f"invalid key name {name!r}")
    if type(sig_type) is not int or sig_type not in (SIG_TYPE_ED25519, SIG_TYPE_COSIG_V1):
        raise ContractError(f"signature type {sig_type!r} is not Ed25519 (0x01) or cosignature/v1 (0x04)")
    if not isinstance(pub, bytes) or len(pub) != ed25519.PUBLIC_KEY_LEN:
        raise ContractError("an Ed25519 public key is 32 bytes")
    return VerifierKey(name, key_id(name, sig_type, pub), sig_type, pub)


def parse_vkey(vkey: str) -> VerifierKey:
    """``<name>+<hex keyID>+base64(type || pub)``; Ed25519 note (0x01) and cosignature/v1 (0x04) types only."""
    if not isinstance(vkey, str):
        raise ContractError("a vkey is a string")
    parts = vkey.split("+", 2)   # the base64 key may itself contain "+"; a name may not
    if len(parts) != 3:
        raise ContractError("a vkey is <name>+<hex key ID>+<base64 key>")
    name, hex_id, b64 = parts
    if not valid_key_name(name):
        raise ContractError(f"invalid key name {name!r}")
    if len(hex_id) != 2 * KEY_ID_LEN or any(c not in "0123456789abcdef" for c in hex_id):
        raise ContractError("a vkey's key ID is 8 lowercase hex digits")
    raw = b64decode(b64)
    if len(raw) != 1 + ed25519.PUBLIC_KEY_LEN or raw[0] not in (SIG_TYPE_ED25519, SIG_TYPE_COSIG_V1):
        raise ContractError("only Ed25519 (0x01) and cosignature/v1 (0x04) vkeys are accepted")
    key = make_key(name, raw[1:], raw[0])
    if key.key_id.hex() != hex_id:
        raise ContractError("the vkey's key ID does not match its name, type and key")
    return key


@dataclass(frozen=True)
class Signature:
    name: str
    key_id: bytes
    sig: bytes        # the bytes after the key ID
    line: str         # the signature line as it appeared, without its "\n"


@dataclass(frozen=True)
class Note:
    text: str
    signatures: tuple[Signature, ...]


def _check_chars(s: str) -> None:
    for c in s:
        if ord(c) < 0x20 and c != "\n":
            raise ContractError(f"control character U+{ord(c):04X} in a signed note")


def parse(note: bytes | str) -> Note:
    """Split a note into text and signature lines, refusing every malformed form. Verifies nothing."""
    if isinstance(note, bytes):
        try:
            note = note.decode("utf-8")
        except UnicodeDecodeError:
            raise ContractError("a signed note is not valid UTF-8") from None
    if not isinstance(note, str):
        raise ContractError("a signed note is text")
    _check_chars(note)
    if not note.endswith("\n"):
        raise ContractError("a signed note ends in a newline")
    cut = note.rfind("\n\n")
    if cut < 0:
        raise ContractError("a signed note has no blank line before its signatures")
    text, block = note[:cut + 1], note[cut + 2:]
    lines = block.split("\n")[:-1]   # block ends in "\n"
    if not lines:
        raise ContractError("a signed note has no signature lines")
    if len(lines) > MAX_SIGNATURE_LINES:
        raise ContractError(f"a signed note carries more than {MAX_SIGNATURE_LINES} signature lines")
    sigs = []
    for line in lines:
        if len(line.encode("utf-8")) > MAX_SIGNATURE_LINE_BYTES:
            raise ContractError(f"a signature line is over {MAX_SIGNATURE_LINE_BYTES} bytes")
        if not line.startswith(DASH):
            raise ContractError("a signature line starts with an em dash and a space")
        name, sep, b64 = line[len(DASH):].partition(" ")
        if not sep or not valid_key_name(name):
            raise ContractError("a signature line is '— <key name> <base64>'")
        raw = b64decode(b64)
        if len(raw) <= KEY_ID_LEN:
            raise ContractError("a signature is a key ID and at least one byte")
        sigs.append(Signature(name, raw[:KEY_ID_LEN], raw[KEY_ID_LEN:], line))
    return Note(text, tuple(sigs))


def check_text(text: str) -> None:
    """A note text levain will sign: valid characters, ends in exactly one newline-terminated line."""
    if not isinstance(text, str) or not text.endswith("\n"):
        raise ContractError("a note text ends in a newline")
    _check_chars(text)


def verify(note: bytes | str, known: Iterable[VerifierKey]) -> tuple[Note, list[VerifierKey]]:
    """Verify a note's Ed25519 (0x01) signatures; return the note and the known keys that verified.

    Raises ``ContractError`` when a known key's signature fails or when no known key verifies.
    """
    parsed = parse(note)
    by_id = {(k.name, k.key_id): k for k in known if k.sig_type == SIG_TYPE_ED25519}
    msg = parsed.text.encode("utf-8")
    good: list[VerifierKey] = []
    for s in parsed.signatures:
        k = by_id.get((s.name, s.key_id))
        if k is None:
            continue
        if not ed25519.verify(k.pub, msg, s.sig):
            raise ContractError(f"the signature from known key {k.name} does not verify")
        if k not in good:
            good.append(k)
    if not good:
        raise ContractError("no signature from a known key")
    return parsed, good


def signature_line(name: str, kid: bytes, sig: bytes) -> str:
    if not valid_key_name(name) or len(kid) != KEY_ID_LEN:
        raise ContractError("invalid key name or key ID")
    return f"{DASH}{name} {b64encode(kid + sig)}\n"


def sign(text: str, name: str, seed: bytes) -> str:
    """A note signed by one in-process Ed25519 key (log signature, type 0x01)."""
    check_text(text)
    key = make_key(name, ed25519.public_from_seed(seed))
    return text + "\n" + signature_line(name, key.key_id, ed25519.sign(seed, text.encode("utf-8")))

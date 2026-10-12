"""F3 checkpoints (tlog-checkpoint@v1.1.0), F4 cosignature verification (tlog-cosignature@v1.1.0,
Ed25519 ``cosignature/v1``, type 0x04) and F5 the witness ``add-checkpoint`` body (tlog-witness@v1.1.0).

levain's checkpoint profile on top of the spec: zero extension lines (the spec: "NOT RECOMMENDED"), the log's
key name equals the origin (the spec: "SHOULD"), the origin carries a frozen levain prefix (F6) and a team
verifier requires the exact team production. The spec's MUST: no two signature lines with both the same key
name and the same key ID.

F4 freezes verification to a record. Counting records into a quorum is P4's, not this module's.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import Iterable

from . import ContractError
from . import ed25519
from . import merkle
from . import note as N
from . import origin as O
from .grammar import NOTE_INT_MAX, b64decode, b64encode, check_int, parse_decimal

COSIG_FUTURE_SLACK_S = 600       # a cosignature timestamp above now + this is refused (levain profile)
COSIG_TIMESTAMP_LEN = 8
MAX_PROOF_LINES = 63             # tlog-witness: "The client MUST NOT send more than 63 consistency proof lines"
_MAX_NAME_BYTES = 255


@dataclass(frozen=True)
class Checkpoint:
    origin: str
    size: int
    root: bytes
    extensions: tuple[str, ...]

    def text(self) -> str:
        return "".join(f"{x}\n" for x in (self.origin, str(self.size), b64encode(self.root), *self.extensions))


def _check_origin_name(origin: str) -> None:
    # tlog-cosignature's name rule, which an origin follows: non-empty, <= 255 bytes, no spaces, '+' or controls
    if not N.valid_key_name(origin) or len(origin.encode("utf-8")) > _MAX_NAME_BYTES:
        raise ContractError(f"origin {origin!r} is not a legal log origin")


def check_tree_head(size: int, root: bytes) -> None:
    """The one size/root check: ``sign`` calls it, and ``parse_text`` (so ``verify``, ``cosign``,
    ``verify_cosignatures`` and ``add_checkpoint_body``) calls it. A size-0 root is SHA-256 of the empty string."""
    check_int(size, NOTE_INT_MAX)
    if not isinstance(root, bytes) or len(root) != merkle.HASH_LEN:
        raise ContractError("a checkpoint root is 32 bytes")
    merkle.check_root_of_size(size, root, "the checkpoint")


def parse_text(text: str) -> Checkpoint:
    """A checkpoint note text: origin, size, base64 root, then optional non-empty extension lines."""
    if not isinstance(text, str) or not text.endswith("\n"):
        raise ContractError("a checkpoint text ends in a newline")
    lines = text[:-1].split("\n")
    if len(lines) < 3:
        raise ContractError("a checkpoint text has at least three lines")
    origin, size_s, root_s, *ext = lines
    _check_origin_name(origin)
    size = parse_decimal(size_s, NOTE_INT_MAX)
    root = b64decode(root_s)
    check_tree_head(size, root)
    for e in ext:
        if not e:
            raise ContractError("a checkpoint extension line is non-empty")
    return Checkpoint(origin, size, root, tuple(ext))


def _no_duplicate_lines(parsed: N.Note) -> None:
    seen = set()
    for s in parsed.signatures:
        if (s.name, s.key_id) in seen:
            raise ContractError("two signature lines share both key name and key ID")
        seen.add((s.name, s.key_id))


def verify(note: bytes | str, log_key: N.VerifierKey, *, team: bool = False) -> tuple[Checkpoint, N.Note]:
    """Verify a levain log's checkpoint under the log's own Ed25519 key (key name = origin).

    ``team=True`` is a team-log verifier: the origin must be the exact ``levain-team/<32 hex>`` production.
    """
    parsed = N.parse(note)
    _no_duplicate_lines(parsed)
    cp = parse_text(parsed.text)
    if cp.extensions:
        raise ContractError("a levain checkpoint carries no extension lines")
    if team:
        O.check_team_origin(cp.origin)
    else:
        O.log_kind(cp.origin)
    if log_key.sig_type != N.SIG_TYPE_ED25519 or log_key.name != cp.origin:
        raise ContractError("the log's key name must equal the checkpoint origin")
    N.verify(note, [log_key])
    return cp, parsed


def sign(cp: Checkpoint, seed: bytes) -> str:
    """A levain log signs its checkpoint with key name = origin."""
    if cp.extensions:
        raise ContractError("a levain checkpoint carries no extension lines")
    O.log_kind(cp.origin)
    check_tree_head(cp.size, cp.root)
    return N.sign(cp.text(), cp.origin, seed)


# ---- F4: cosignatures -------------------------------------------------------------------------------------


@dataclass(frozen=True)
class VerifiedCosignature:
    cosigner_name: str
    key_id: bytes
    timestamp: int


def cosigned_message(timestamp: int, checkpoint_text: str) -> bytes:
    """``cosignature/v1`` · ``time <decimal>`` · the checkpoint text (origin, size, root, extension lines)."""
    check_int(timestamp, NOTE_INT_MAX)
    return f"cosignature/v1\ntime {timestamp}\n{checkpoint_text}".encode("utf-8")


def cosign(checkpoint_text: str, name: str, seed: bytes, timestamp: int) -> str:
    """One cosignature line (Ed25519 cosignature/v1) for ``checkpoint_text``."""
    check_int(timestamp, NOTE_INT_MAX)   # before struct.pack, which raises struct.error or packs a bool
    parse_text(checkpoint_text)
    key = N.make_key(name, ed25519.public_from_seed(seed), N.SIG_TYPE_COSIG_V1)
    sig = struct.pack(">Q", timestamp) + ed25519.sign(seed, cosigned_message(timestamp, checkpoint_text))
    return N.signature_line(name, key.key_id, sig)


def verify_cosignatures(note: bytes | str, trusted: Iterable[N.VerifierKey], now: int) -> list[VerifiedCosignature]:
    """Every signature line from a trusted cosigner (type 0x04), verified to a record.

    Unknown keys are ignored. A trusted cosigner's line that fails, or whose timestamp is above
    ``now + 600``, refuses the whole note. ``now`` is the caller's clock; nothing here reads one.
    """
    check_int(now, NOTE_INT_MAX)
    trusted = list(trusted)
    for k in trusted:
        if k.sig_type != N.SIG_TYPE_COSIG_V1:
            raise ContractError(f"cosigner {k.name} is configured with a type {k.sig_type:#04x} key, not 0x04")
    parsed = N.parse(note)
    _no_duplicate_lines(parsed)
    parse_text(parsed.text)
    by_id = {(k.name, k.key_id): k for k in trusted}
    out: list[VerifiedCosignature] = []
    for s in parsed.signatures:
        k = by_id.get((s.name, s.key_id))
        if k is None:
            continue
        if len(s.sig) != COSIG_TIMESTAMP_LEN + ed25519.SIGNATURE_LEN:
            raise ContractError(f"cosignature from {k.name} is not an 8-byte timestamp and a 64-byte signature")
        (ts,) = struct.unpack(">Q", s.sig[:COSIG_TIMESTAMP_LEN])
        if ts > NOTE_INT_MAX:
            raise ContractError(f"cosignature from {k.name} has a timestamp above {NOTE_INT_MAX}")
        if ts > now + COSIG_FUTURE_SLACK_S:
            raise ContractError(f"cosignature from {k.name} is timestamped more than {COSIG_FUTURE_SLACK_S}s ahead")
        if not ed25519.verify(k.pub, cosigned_message(ts, parsed.text), s.sig[COSIG_TIMESTAMP_LEN:]):
            raise ContractError(f"cosignature from {k.name} does not verify")
        out.append(VerifiedCosignature(k.name, k.key_id, ts))
    return out


def check_advance(held: Checkpoint, new: Checkpoint, proof: list[bytes]) -> None:
    """A verifier holding a cosigned ``held`` refuses a smaller, inconsistent, or same-size-different-root ``new``."""
    if held.origin != new.origin:
        raise ContractError("the checkpoints are for different origins")
    merkle.verify_consistency(held.size, new.size, proof, held.root, new.root)


# ---- F5: the add-checkpoint body --------------------------------------------------------------------------


def add_checkpoint_body(old_size: int, proof: list[bytes], checkpoint_note: bytes | str) -> bytes:
    """``old <size>\\n``, one base64 line per proof hash, ``\\n``, then the signed checkpoint (tlog-witness)."""
    check_int(old_size, NOTE_INT_MAX)
    if isinstance(checkpoint_note, bytes):
        try:
            checkpoint_note = checkpoint_note.decode("utf-8")
        except UnicodeDecodeError:
            raise ContractError("a signed note is not valid UTF-8") from None
    if not isinstance(checkpoint_note, str):
        raise ContractError("a signed checkpoint note is text or UTF-8 bytes")
    parsed = N.parse(checkpoint_note)
    cp = parse_text(parsed.text)
    if old_size > cp.size:
        raise ContractError("the old size is above the checkpoint size")
    if (old_size == 0 or old_size == cp.size) and proof:
        raise ContractError("a proof from size 0, or between equal sizes, is empty")
    if len(proof) > MAX_PROOF_LINES:
        raise ContractError(f"more than {MAX_PROOF_LINES} consistency proof lines")
    for p in proof:
        if not isinstance(p, bytes) or len(p) != merkle.HASH_LEN:
            raise ContractError("a proof line encodes one 32-byte hash")
    lines = [f"old {old_size}\n"] + [b64encode(p) + "\n" for p in proof] + ["\n", checkpoint_note]
    return "".join(lines).encode("utf-8")

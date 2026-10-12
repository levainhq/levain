"""F10: the ``receipt_checkpoint`` leaf (Shadow Receipt §7.1's ask), a team-log leaf type (TS l.952).

Leaf ``(receipt_checkpoint, 1)``, team log only::

    {"leaf":"receipt_checkpoint","v":1,"checkpoint":<a signed checkpoint note, verbatim text>,
     "old_size":<int>,"consistency":[<canonical base64 of 32-byte hashes>]}

Ingest and verify both check: the note verifies (F2/F3) under the key the team's directory binds to the writing
device (the caller passes it: that binding is P1/P12); ``old_size`` equals the size of the same origin's previous
ANCHOR in team-log order (0 for the first); the proof verifies old -> new (F1, m == 0 and m == n included); an equal
size with a different root (a fork) and a size below the previous anchor are refused; a checkpoint size above
2^53-1 is refused, since the next anchor's ``old_size`` could not be encoded. The receipt origin's production, which
key signs a receipt log, and the receipt log's own payloads are not frozen here (P12, P1, P6).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from .. import ContractError
from .. import canon
from .. import checkpoint as CP
from .. import merkle
from .. import note as N
from ..grammar import JSON_INT_MAX, b64decode, check_int

LEAF, VERSION = "receipt_checkpoint", 1
LOG_KIND = "team"
LEAF_KEYS = frozenset({"leaf", "v", "checkpoint", "old_size", "consistency"})


@dataclass(frozen=True)
class Anchor:
    origin: str
    size: int
    root: bytes


@dataclass(frozen=True)
class ReceiptCheckpointLeaf:
    checkpoint: str
    old_size: int
    consistency: tuple[bytes, ...]
    anchor: Anchor          # the checkpoint's (origin, size, root), parsed but not yet signature-verified


def check_fields(obj: dict) -> ReceiptCheckpointLeaf:
    """The context-free half: exact keys, the note's and checkpoint's grammar, integer and proof shapes."""
    if not isinstance(obj, dict) or set(obj) != LEAF_KEYS:
        raise ContractError(f"a receipt_checkpoint leaf has exactly the keys {sorted(LEAF_KEYS)}")
    if obj["leaf"] != LEAF or type(obj["v"]) is not int or obj["v"] != VERSION:
        raise ContractError("not a (receipt_checkpoint, 1) leaf")
    text, old_size, proof = obj["checkpoint"], obj["old_size"], obj["consistency"]
    if not isinstance(text, str):
        raise ContractError("checkpoint is the signed note's text")
    cp = CP.parse_text(N.parse(text).text)
    if cp.extensions:
        raise ContractError("a levain checkpoint carries no extension lines")
    if cp.size > JSON_INT_MAX:
        raise ContractError(f"an anchored checkpoint's size is at most {JSON_INT_MAX}: the next old_size must encode")
    check_int(old_size, JSON_INT_MAX)
    if not isinstance(proof, list):
        raise ContractError("consistency is a list")
    hashes = []
    for p in proof:
        h = b64decode(p) if isinstance(p, str) else None
        if h is None or len(h) != merkle.HASH_LEN:
            raise ContractError("a consistency proof line is canonical base64 of 32 bytes")
        hashes.append(h)
    return ReceiptCheckpointLeaf(text, old_size, tuple(hashes), Anchor(cp.origin, cp.size, cp.root))


def check_receipt_checkpoint(d: bytes, *, log_key: N.VerifierKey, previous: Anchor | None) -> Anchor:
    """The full check of one leaf. ``previous`` is the same origin's previous ACCEPTED anchor, or None.

    Returns the new anchor. Raises ``canon.UnknownLeafError`` or ``ContractError``.
    """
    obj = canon.check_leaf(d, LOG_KIND)
    if obj["leaf"] != LEAF:
        raise ContractError(f"a {obj['leaf']!r} leaf is not a receipt_checkpoint")
    leaf = check_fields(obj)
    cp, _ = CP.verify(leaf.checkpoint, log_key)
    new = Anchor(cp.origin, cp.size, cp.root)
    if previous is not None and previous.origin != new.origin:
        raise ContractError("the previous anchor is for another origin")
    old = previous or Anchor(new.origin, 0, merkle.EMPTY_ROOT)
    if leaf.old_size != old.size:
        raise ContractError(f"old_size {leaf.old_size} is not the previous anchor's size {old.size}")
    merkle.verify_consistency(old.size, new.size, list(leaf.consistency), old.root, new.root)
    return new


def judge_anchors(leaves: Iterable[tuple[bytes, N.VerifierKey]], *, at: str) -> list[tuple[str, str]]:
    """Walk ``receipt_checkpoint`` leaves in team-log order; each is judged against its origin's previous ACCEPTED
    anchor (a refused leaf moves nothing). Returns ``(verdict, reason)`` per leaf. Ingest and verify apply the same
    checks; ``at`` decides only how an unknown ``(leaf, v)`` reads (REFUSE at ingest, UNVERIFIABLE at verify).

    At verify the walk is a replay (rev 11): once a leaf is UNVERIFIABLE, every later leaf of its origin is
    UNVERIFIABLE too, naming the first. An unknown ``(leaf, v)`` has no origin levain can read (reading a field of an
    unknown version would be a guess), so it poisons every origin. Ingest is unchanged."""
    if at not in ("ingest", "verify"):
        raise ValueError("at is 'ingest' or 'verify'")
    anchors: dict[str, Anchor] = {}
    poisoned: dict[str | None, tuple[int, str]] = {}    # origin (None = every origin) -> the first UNVERIFIABLE leaf
    out = []
    for i, (d, key) in enumerate(leaves):
        try:
            origin = check_fields(canon.parse(d)).anchor.origin
        except Exception:   # noqa: BLE001 - the full check below gives the refusal its reason
            origin = None
        first = poisoned.get(None) or (poisoned.get(origin) if origin is not None else None)
        if first is not None:
            out.append((canon.UNVERIFIABLE, f"after leaf {first[0]}, which is UNVERIFIABLE: {first[1]}"))
            continue
        try:
            new = check_receipt_checkpoint(d, log_key=key, previous=anchors.get(origin))
        except canon.UnknownLeafError as e:
            if at == "ingest":
                out.append((canon.REFUSE, str(e)))
            else:
                poisoned.setdefault(origin, (i, str(e)))
                out.append((canon.UNVERIFIABLE, str(e)))
            continue
        except ContractError as e:
            out.append((canon.REFUSE, str(e)))
            continue
        except Exception as e:   # noqa: BLE001 - F8: any exception in leaf validation is a refusal
            out.append((canon.REFUSE, f"leaf validation failed: {type(e).__name__}"))
            continue
        anchors[new.origin] = new
        out.append((canon.ACCEPT, ""))
    return out

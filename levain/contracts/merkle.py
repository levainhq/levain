"""F1: RFC 6962 / RFC 9162 §2.1 Merkle tree hashing and proofs, SHA-256.

Verification follows RFC 9162 §2.1.3.2 (inclusion) and §2.1.4.2 (consistency, 0 < m < n). The two cases
§2.1.4.2 does not cover come from tlog-witness@v1.1.0: m == 0 ("the consistency proof MUST be empty", and a
size-0 tree head "MUST have the root hash of the empty tree") and m == n (proof empty, "the root hashes are
also identical"). Proofs are lists of 32-byte hashes, leaf-sibling first. Every verifier returns None on
success and raises ``ContractError`` on any refusal; there is no boolean to forget to check.
"""
from __future__ import annotations

import hashlib

from . import ContractError
from .grammar import NOTE_INT_MAX, check_int

HASH_LEN = 32
EMPTY_ROOT = hashlib.sha256(b"").digest()


def leaf_hash(d: bytes) -> bytes:
    return hashlib.sha256(b"\x00" + d).digest()


def node_hash(left: bytes, right: bytes) -> bytes:
    return hashlib.sha256(b"\x01" + left + right).digest()


def _split(n: int) -> int:
    """The largest power of two smaller than ``n`` (n > 1)."""
    return 1 << ((n - 1).bit_length() - 1)


def _mth_hashes(leaves: list[bytes]) -> bytes:
    n = len(leaves)
    if n == 0:
        return EMPTY_ROOT
    if n == 1:
        return leaves[0]
    k = _split(n)
    return node_hash(_mth_hashes(leaves[:k]), _mth_hashes(leaves[k:]))


def root(data: list[bytes]) -> bytes:
    """MTH(D_n) over the leaf inputs ``data``."""
    return _mth_hashes([leaf_hash(d) for d in data])


def _path(m: int, leaves: list[bytes]) -> list[bytes]:
    n = len(leaves)
    if n <= 1:
        return []
    k = _split(n)
    if m < k:
        return _path(m, leaves[:k]) + [_mth_hashes(leaves[k:])]
    return _path(m - k, leaves[k:]) + [_mth_hashes(leaves[:k])]


def _subproof(m: int, leaves: list[bytes], whole: bool) -> list[bytes]:
    n = len(leaves)
    if m == n:
        return [] if whole else [_mth_hashes(leaves)]
    k = _split(n)
    if m <= k:
        return _subproof(m, leaves[:k], whole) + [_mth_hashes(leaves[k:])]
    return _subproof(m - k, leaves[k:], False) + [_mth_hashes(leaves[:k])]


def inclusion_proof(data: list[bytes], index: int) -> list[bytes]:
    """RFC 9162 §2.1.3.1 PATH(index, D_n)."""
    if not 0 <= index < len(data):
        raise ContractError("index is not below the tree size")
    return _path(index, [leaf_hash(d) for d in data])


def consistency_proof(data: list[bytes], m: int) -> list[bytes]:
    """RFC 9162 §2.1.4.1 PROOF(m, D_n); empty for m == 0 and m == n (tlog-witness)."""
    n = len(data)
    if not 0 <= m <= n:
        raise ContractError("old size is not within 0..size")
    if m == 0 or m == n:
        return []
    return _subproof(m, [leaf_hash(d) for d in data], True)


def _check_hash(h: object, what: str) -> bytes:
    if not isinstance(h, bytes) or len(h) != HASH_LEN:
        raise ContractError(f"{what} is not a {HASH_LEN}-byte hash")
    return h


def _check_proof(proof: object) -> list[bytes]:
    if not isinstance(proof, list):
        raise ContractError("a proof is a list of hashes")
    return [_check_hash(p, "a proof element") for p in proof]


def check_root_of_size(size: int, root_hash: bytes, what: str) -> None:
    if size == 0 and root_hash != EMPTY_ROOT:
        raise ContractError(f"{what} has size 0 and its root is not SHA-256 of the empty string")


def verify_inclusion(index: int, size: int, leaf: bytes, proof: list[bytes], root_hash: bytes) -> None:
    """RFC 9162 §2.1.3.2, with ``leaf`` the leaf HASH (not the leaf input)."""
    check_int(index, NOTE_INT_MAX)
    check_int(size, NOTE_INT_MAX)
    r = _check_hash(leaf, "the leaf hash")
    _check_hash(root_hash, "the root")
    proof = _check_proof(proof)
    if index >= size:
        raise ContractError("index is not below the tree size")
    fn, sn = index, size - 1
    for p in proof:
        if sn == 0:
            raise ContractError("the inclusion proof is longer than the tree allows")
        if fn & 1 or fn == sn:
            r = node_hash(p, r)
            if not fn & 1:
                while fn and not fn & 1:
                    fn >>= 1
                    sn >>= 1
        else:
            r = node_hash(r, p)
        fn >>= 1
        sn >>= 1
    if sn != 0:
        raise ContractError("the inclusion proof is shorter than the tree requires")
    if r != root_hash:
        raise ContractError("the inclusion proof does not lead to the root")


def verify_consistency(m: int, n: int, proof: list[bytes], old_root: bytes, new_root: bytes) -> None:
    """RFC 9162 §2.1.4.2 for 0 < m < n; tlog-witness@v1.1.0 for m == 0 and m == n."""
    check_int(m, NOTE_INT_MAX)
    check_int(n, NOTE_INT_MAX)
    _check_hash(old_root, "the old root")
    _check_hash(new_root, "the new root")
    proof = _check_proof(proof)
    if m > n:
        raise ContractError("the old size is larger than the new size")
    check_root_of_size(m, old_root, "the old tree")
    check_root_of_size(n, new_root, "the new tree")
    if m == 0:
        if proof:
            raise ContractError("the old size is zero and the consistency proof is not empty")
        return
    if m == n:
        if proof:
            raise ContractError("the sizes are equal and the consistency proof is not empty")
        if old_root != new_root:
            raise ContractError("the sizes are equal and the roots differ")
        return
    if not proof:
        raise ContractError("the consistency proof is empty")
    if m & (m - 1) == 0:
        proof = [old_root] + proof
    fn, sn = m - 1, n - 1
    while fn & 1:
        fn >>= 1
        sn >>= 1
    fr = sr = proof[0]
    for c in proof[1:]:
        if sn == 0:
            raise ContractError("the consistency proof is longer than the trees allow")
        if fn & 1 or fn == sn:
            fr = node_hash(c, fr)
            sr = node_hash(c, sr)
            if not fn & 1:
                while fn and not fn & 1:
                    fn >>= 1
                    sn >>= 1
        else:
            sr = node_hash(sr, c)
        fn >>= 1
        sn >>= 1
    if sn != 0:
        raise ContractError("the consistency proof is shorter than the trees require")
    if fr != old_root or sr != new_root:
        raise ContractError("the consistency proof does not lead to both roots")

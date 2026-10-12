"""Shared helpers for the frozen-vector generators. Standard library and ``cryptography`` only, never ``levain``
(``tests/test_contracts_generators.py`` checks every ``.py`` under ``tests/golden/contracts/``).

The arithmetic here is a second, independent implementation of the spec rules (RFC 9162 MTH/PATH/PROOF, the
signed-note line format, cosignature/v1), so a vector's expected bytes never come from the code under test.
"""
from __future__ import annotations

import base64
import hashlib
import json
import struct
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

FROZEN = Path(__file__).resolve().parent / "frozen"

GO = "go1.27.2"
UPSTREAM = {
    "merkle": ("github.com/transparency-dev/merkle/proof", "v0.0.2"),
    "note": ("golang.org/x/mod/sumdb/note", "v0.42.0"),
    "checkpoint": ("github.com/transparency-dev/formats/log.ParseCheckpoint", "v0.1.1"),
    "cosig": ("github.com/transparency-dev/formats/note.NewVerifierForCosignatureV1", "v0.1.1"),
    "witness": ("github.com/transparency-dev/witness/witness.NewAddCheckpointHandler",
                "v0.0.0-20260925114237-b4c9458d9b14"),
    "ed25519": ("crypto/ed25519", GO),
    "decimal": ("strconv.ParseUint", GO),
    "b64": ("encoding/base64.StdEncoding", GO),
}

ACCEPT, REFUSE = "accept", "refuse"


def spec(source: str, clause: str) -> dict:
    return {"kind": "spec", "source": source, "clause": clause}


def derivation(source: str, clause: str) -> dict:
    return {"kind": "byte_derivation", "source": source, "clause": clause}


def profile(source: str, clause: str, *, quote: str | None = None, rationale: str | None = None) -> dict:
    basis = ({"kind": "spec_permission", "quote": quote} if quote is not None
             else {"kind": "local_bound", "rationale": rationale})
    return {"kind": "profile_rule", "source": source, "clause": clause, "basis": basis}


def vector(vid: str, kind: str, op: str, inp: dict, levain: dict, authority: dict, upstream: str | None) -> dict:
    """One vector. ``upstream`` names a key of UPSTREAM, or None for ``no_upstream``.

    The upstream verdict follows from the classification: ``spec`` = levain's verdict; ``levain_profile`` and
    ``upstream_lax`` = upstream accepts what levain refuses.
    """
    assert kind in ("spec", "levain_profile", "upstream_lax", "no_upstream"), kind
    assert levain["verdict"] in (ACCEPT, REFUSE)
    if kind == "no_upstream":
        assert upstream is None
        up: object = "n/a"
    else:
        impl, ver = UPSTREAM[upstream]
        verdict = levain["verdict"] if kind == "spec" else ACCEPT
        if kind != "spec":
            assert levain["verdict"] == REFUSE, vid
        up = {"implementation": impl, "version": ver, "verdict": verdict}
    return {"id": vid, "kind": kind, "op": op, "input": inp, "expect": {"levain": levain, "upstream": up},
            "authority": authority}


def write(item: str, directory: str, vectors: list[dict], **extra) -> None:
    ids = [v["id"] for v in vectors]
    assert len(ids) == len(set(ids)), "duplicate vector id"
    doc = {"item": item, "generator": "gen.py", **extra, "vectors": vectors}
    out = FROZEN / directory / "vectors.json"
    out.write_text(json.dumps(doc, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"{out}: {len(vectors)} vectors")


# ---- keys -----------------------------------------------------------------------------------------------


def seed(label: str) -> bytes:
    return hashlib.sha256(b"levain-contracts/golden/" + label.encode()).digest()


def pub(sd: bytes) -> bytes:
    return Ed25519PrivateKey.from_private_bytes(sd).public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw)


def ed_sign(sd: bytes, msg: bytes) -> bytes:
    return Ed25519PrivateKey.from_private_bytes(sd).sign(msg)


def b64(b: bytes) -> str:
    return base64.b64encode(b).decode()


def kid(name: str, typ: int, pk: bytes) -> bytes:
    return hashlib.sha256(name.encode() + b"\n" + bytes([typ]) + pk).digest()[:4]


def vkey(name: str, typ: int, pk: bytes) -> str:
    return f"{name}+{kid(name, typ, pk).hex()}+{b64(bytes([typ]) + pk)}"


def sig_line(name: str, key_id: bytes, sig: bytes) -> str:
    return f"— {name} {b64(key_id + sig)}\n"


def note_sig(text: str, name: str, sd: bytes) -> str:
    """One Ed25519 (0x01) signature line over ``text``."""
    return sig_line(name, kid(name, 1, pub(sd)), ed_sign(sd, text.encode()))


def cosig_line(cp_text: str, name: str, sd: bytes, ts: int) -> str:
    msg = f"cosignature/v1\ntime {ts}\n{cp_text}".encode()
    return sig_line(name, kid(name, 4, pub(sd)), struct.pack(">Q", ts) + ed_sign(sd, msg))


# ---- RFC 9162 §2.1, independently ----------------------------------------------------------------------


def lh(d: bytes) -> bytes:
    return hashlib.sha256(b"\x00" + d).digest()


def nh(a: bytes, b: bytes) -> bytes:
    return hashlib.sha256(b"\x01" + a + b).digest()


def k_of(n: int) -> int:
    k = 1
    while k * 2 < n:
        k *= 2
    return k


def mth(d: list[bytes]) -> bytes:
    if not d:
        return hashlib.sha256(b"").digest()
    if len(d) == 1:
        return lh(d[0])
    k = k_of(len(d))
    return nh(mth(d[:k]), mth(d[k:]))


def path(m: int, d: list[bytes]) -> list[bytes]:
    if len(d) <= 1:
        return []
    k = k_of(len(d))
    return path(m, d[:k]) + [mth(d[k:])] if m < k else path(m - k, d[k:]) + [mth(d[:k])]


def subproof(m: int, d: list[bytes], b: bool) -> list[bytes]:
    n = len(d)
    if m == n:
        return [] if b else [mth(d)]
    k = k_of(n)
    if m <= k:
        return subproof(m, d[:k], b) + [mth(d[k:])]
    return subproof(m - k, d[k:], False) + [mth(d[:k])]


def leaves(n: int) -> list[bytes]:
    """F1's golden leaf rule: ``d_i = bytes([i % 256]) * (i+1)``."""
    return [bytes([i % 256]) * (i + 1) for i in range(n)]

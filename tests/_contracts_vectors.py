"""Loading the frozen contract vectors (``tests/golden/contracts/frozen/<dir>/vectors.json``) for the tests.

Every vector carries ``kind``, ``authority`` and ``expect.levain`` / ``expect.upstream`` (S1 §1.0); ``check_shape``
asserts that, so a vector cannot lose its authority without a test going red.
"""
from __future__ import annotations

import json
from pathlib import Path

GOLDEN = Path(__file__).resolve().parent / "golden" / "contracts"
FROZEN = GOLDEN / "frozen"
KINDS = ("spec", "levain_profile", "upstream_lax", "no_upstream")
AUTHORITY_KINDS = ("spec", "published_table", "byte_derivation", "profile_rule")


def load(directory: str) -> list[dict]:
    doc = json.loads((FROZEN / directory / "vectors.json").read_text(encoding="utf-8"))
    return doc["vectors"]


def check_shape(v: dict) -> None:
    assert v["kind"] in KINDS, v["id"]
    a = v["authority"]
    assert a["kind"] in AUTHORITY_KINDS and a["source"] and a["clause"], v["id"]
    if a["kind"] == "profile_rule":
        b = a["basis"]
        assert (b["kind"], bool(b.get("quote") or b.get("rationale"))) in (
            ("spec_permission", True), ("local_bound", True)), v["id"]
    if v["kind"] == "levain_profile":
        assert a["kind"] == "profile_rule", v["id"]
    up = v["expect"]["upstream"]
    if v["kind"] == "no_upstream":
        assert up == "n/a", v["id"]
    else:
        assert set(up) == {"implementation", "version", "verdict"}, v["id"]
        if v["kind"] in ("levain_profile", "upstream_lax"):
            assert (v["expect"]["levain"]["verdict"], up["verdict"]) == ("refuse", "accept"), v["id"]
        else:
            assert up["verdict"] == v["expect"]["levain"]["verdict"], v["id"]


def ids(vectors: list[dict]) -> list[str]:
    return [v["id"] for v in vectors]


def h(x: str) -> bytes:
    return bytes.fromhex(x)

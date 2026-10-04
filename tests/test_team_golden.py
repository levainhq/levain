"""The ledger's hashing contract, pinned as bytes.

anneal-memory's team import recomputes these chains. Both packages assert the same vector; if this test has
to change, the anneal side changes in the same release pair or imported ledgers stop verifying.
"""
import hashlib
import json

from levain.team import entry as E

ENTRY_1 = {
    "v": 1, "id": "ana-20261004120000-0a1b2c3d", "ts": "2026-10-04T12:00:00Z", "author": "ana",
    "agent": "claude-code", "type": "decision", "kind": "ruling", "paths": ["src/billing.py"],
    "owner": "client:Dana",
    "words": "Per-line rounding stays — the bank’s validator rejects mismatched totals.",
    "supersedes": [],
}
CANONICAL_1 = (
    '{"agent":"claude-code","author":"ana","id":"ana-20261004120000-0a1b2c3d","kind":"ruling",'
    '"owner":"client:Dana","paths":["src/billing.py"],"prev":"","supersedes":[],"ts":"2026-10-04T12:00:00Z",'
    '"type":"decision","v":1,"words":"Per-line rounding stays — the bank’s validator rejects mismatched totals."}'
)
HASH_1 = "64e621bb36fc63176a4b17966cb58a51921980f27559d0fc4308c2fff80b7e94"
ENTRY_2 = {
    "v": 1, "id": "ana-20261004120100-0a1b2c3e", "ts": "2026-10-04T12:01:00Z", "author": "ana", "type": "ack",
    "refs": ["ana-20261004120000-0a1b2c3d"], "session": "s1", "paths": [], "supersedes": [],
}
HASH_2 = "1370dd29f84c9723e0536fc39dfac071fb4356b05db3717f13a2f1eef853417e"


def test_canonical_bytes_and_hash_are_pinned():
    sealed = E.seal(ENTRY_1, "")
    assert E.canonical(sealed) == CANONICAL_1
    # computed here without levain, so the vector is not levain checking itself
    assert hashlib.sha256(("" + CANONICAL_1).encode("utf-8")).hexdigest() == HASH_1
    assert sealed["hash"] == HASH_1


def test_chain_links_prev_to_the_previous_hash():
    s1 = E.seal(ENTRY_1, "")
    s2 = E.seal(ENTRY_2, s1["hash"])
    assert s2["prev"] == HASH_1 and s2["hash"] == HASH_2
    lines = [json.dumps(s1, ensure_ascii=False), json.dumps(s2)]
    entries, problems, last = E.verify_lines(lines)
    assert problems == [] and [e["id"] for e in entries] == [ENTRY_1["id"], ENTRY_2["id"]] and last == HASH_2


def test_key_order_and_ascii_escaping_do_not_change_the_hash():
    s1 = E.seal(ENTRY_1, "")
    reordered = json.dumps(dict(reversed(list(s1.items()))), ensure_ascii=True)
    entries, problems, _ = E.verify_lines([reordered])
    assert problems == [] and entries[0]["hash"] == HASH_1

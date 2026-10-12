"""F1: RFC 9162 Merkle hashing and proofs against the frozen vectors."""
import pytest

from levain.contracts import ContractError, merkle as M
from tests._contracts_vectors import check_shape, h, ids, load

VECTORS = load("merkle")


def _leaves(n):
    return [bytes([i % 256]) * (i + 1) for i in range(n)]


def _run(v):
    i = v["input"]
    if v["op"] == "root":
        return M.root(_leaves(i["size"])).hex()
    if v["op"] == "root_explicit":
        return M.root([h(x) for x in i["leaves"]]).hex()
    if v["op"] == "inclusion":
        M.verify_inclusion(i["index"], i["size"], h(i["leaf_hash"]), [h(p) for p in i["proof"]], h(i["root"]))
        return None
    if v["op"] == "consistency":
        M.verify_consistency(i["m"], i["n"], [h(p) for p in i["proof"]], h(i["old_root"]), h(i["new_root"]))
        return None
    raise AssertionError(v["op"])


@pytest.mark.parametrize("v", VECTORS, ids=ids(VECTORS))
def test_vector(v):
    check_shape(v)
    want = v["expect"]["levain"]
    if want["verdict"] == "refuse":
        with pytest.raises(ContractError):
            _run(v)
    else:
        out = _run(v)
        if "root" in want:
            assert out == want["root"]


def test_producers_match_the_vectors():
    """The proof builders (used by logs) emit exactly the proofs the vectors carry."""
    by_id = {v["id"]: v for v in VECTORS}
    for n in (7, 8):
        for i in range(n):
            v = by_id[f"inclusion-{n}-{i}"]["input"]
            assert [p.hex() for p in M.inclusion_proof(_leaves(n), i)] == v["proof"]
    d = _leaves(1000)
    for m in (1, 511, 512, 513, 999):
        assert [p.hex() for p in M.consistency_proof(d, m)] == by_id[f"consistency-{m}-1000"]["input"]["proof"]
    assert M.consistency_proof(d, 0) == [] and M.consistency_proof(d, 1000) == []


def test_every_consistency_pair_up_to_64_round_trips():
    d = _leaves(64)
    for n in range(1, 65):
        rn = M.root(d[:n])
        for m in range(1, n + 1):
            M.verify_consistency(m, n, M.consistency_proof(d[:n], m), M.root(d[:m]), rn)
        for i in range(n):
            M.verify_inclusion(i, n, M.leaf_hash(d[i]), M.inclusion_proof(d[:n], i), rn)


def test_golden_counts():
    """§F1's list: roots at 0-8, 2^k±1 (k=4,8,10) and 1000; every proof at 7 and 8; the 1000 samples."""
    got = set(ids(VECTORS))
    want = {f"root-{n}" for n in [*range(9), 15, 16, 17, 255, 256, 257, 1023, 1024, 1025, 1000]}
    want |= {f"inclusion-7-{i}" for i in range(7)} | {f"inclusion-8-{i}" for i in range(8)}
    want |= {f"consistency-{m}-{n}" for n in range(1, 9) for m in range(1, n + 1)}
    want |= {f"consistency-{m}-1000" for m in (1, 511, 512, 513, 999)}
    assert want <= got

"""F8: canonical JSON, the parse refusals, leaf framing, the registry, ``chain_hash``, against the frozen vectors."""
import json

import pytest

from levain.contracts import ContractError, canon as C
from levain.team import entry as E
from tests import test_team_golden as TG
from tests._contracts_vectors import check_shape, h, ids, load

VECTORS = load("canon")


@pytest.mark.parametrize("v", VECTORS, ids=ids(VECTORS))
def test_vector(v):
    check_shape(v)
    i, want = v["input"], v["expect"]["levain"]
    if v["op"] == "canonical":
        assert C.canonical_bytes(i["value"]).hex() == want["bytes_hex"]
        # the team writer emits the same bytes for an object without "hash"
        assert E.canonical(i["value"]).encode("utf-8").hex() == want["bytes_hex"]
    elif v["op"] == "leaf":
        assert C.classify_leaf(h(i["data_hex"]), i["log_kind"], at=i["at"]) == want["verdict"]
    elif v["op"] == "chain_hash":
        assert C.chain_hash(i["prev"], i["entry"]) == want["hash"]
        assert C.canonical({k: x for k, x in i["entry"].items() if k != "hash"}) == want["canonical"]
    else:
        raise AssertionError(v["op"])


def test_chain_hash_equals_team_entry_on_every_shipped_golden_entry():
    """V12: contracts' copy of the formula against the entries tests/test_team_golden.py ships, through T's sealer."""
    s1 = E.seal(TG.ENTRY_1, "")
    s2 = E.seal(TG.ENTRY_2, s1["hash"])
    assert C.chain_hash("", s1) == E.chain_hash("", s1) == TG.HASH_1
    assert C.chain_hash(s1["hash"], s2) == E.chain_hash(s1["hash"], s2) == TG.HASH_2
    assert C.canonical({k: x for k, x in s1.items() if k != "hash"}) == TG.CANONICAL_1


def test_depth_100000_is_refused_without_a_crash():
    d = b'{"leaf":"line","v":1,"x":' + b"[" * 100000 + b"]" * 100000 + b"}"
    assert C.classify_leaf(d, "team", at="ingest") == C.REFUSE
    with pytest.raises(RecursionError):     # the reason depth is measured on the text: json.loads alone crashes here
        json.loads(d)


def test_any_exception_in_validation_is_a_refusal(monkeypatch):
    def boom(*a, **k):
        raise MemoryError("simulated")
    monkeypatch.setattr(C.json, "loads", boom)
    assert C.classify_leaf(b'{"leaf":"line","v":1}', "team", at="verify") == C.REFUSE


def test_unknown_log_kind_and_bad_mode():
    assert C.classify_leaf(b'{"leaf":"line","v":1}', "galaxy", at="ingest") == C.REFUSE
    with pytest.raises(ValueError):
        C.classify_leaf(b'{"leaf":"line","v":1}', "team", at="later")


def test_registry_is_exact_pairs_with_known_log_kinds():
    for (leaf, v), kinds in C.REGISTRY.items():
        assert isinstance(leaf, str) and type(v) is int and kinds <= set(C.LOG_KINDS)


def test_an_oversized_leaf_integer_is_a_contract_error_at_its_own_site():
    with pytest.raises(ContractError):
        C._int("9" * 5000)
    with pytest.raises(ContractError):
        C._int("-" + "9" * 5000)

"""F8: canonical JSON, the parse refusals, leaf framing, the registry, ``chain_hash``, against the frozen vectors."""
import json

import pytest

from levain.contracts import ContractError, canon as C
from levain.team import entry as E
from tests import test_team_golden as TG
from tests._contracts_vectors import check_shape, h, ids, load

VECTORS = load("canon")


def _framing_only(obj):
    return obj


@pytest.mark.parametrize("v", VECTORS, ids=ids(VECTORS))
def test_vector(v):
    check_shape(v)
    i, want = v["input"], v["expect"]["levain"]
    if v["op"] == "canonical":
        assert C.canonical_bytes(i["value"]).hex() == want["bytes_hex"]
        # the team writer emits the same bytes for an object without "hash"
        assert E.canonical(i["value"]).encode("utf-8").hex() == want["bytes_hex"]
    elif v["op"] == "leaf":
        # the injected TEST registry checks framing only; the shipped one adds each type's field validator
        reg = {(e["leaf"], e["v"]): C.LeafType(frozenset(e["log_kinds"]), _framing_only) for e in i["registry"]}
        assert C.classify_leaf(h(i["data_hex"]), i["log_kind"], at=i["at"], registry=reg) == want["verdict"]
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


def test_the_shipped_registry_holds_each_frozen_type_with_its_validator():
    """HEAD_RULINGS #7: each type is registered with its field validator, so framing alone never accepts a leaf.

    S1-a asserted ``C.REGISTRY == {}``; S1-b registers (line, 1) with F9's validator, team log only.
    """
    from levain.contracts.leaves import team
    assert set(C.REGISTRY) == {("line", 1)}
    assert C.REGISTRY[("line", 1)] == C.LeafType(frozenset({"team"}), team.check_line_fields)
    for framed_only in (b'{"leaf":"line","v":1}',):
        assert C.classify_leaf(framed_only, "team", at="ingest") == C.REFUSE
        assert C.classify_leaf(framed_only, "team", at="verify") == C.REFUSE
        with pytest.raises(ContractError):
            C.check_leaf(framed_only, "team")
    assert C.classify_leaf(b'{"leaf":"line","v":2}', "team", at="verify") == C.UNVERIFIABLE


def test_registry_is_exact_pairs_with_known_log_kinds():
    for (leaf, v), entry in C.REGISTRY.items():
        assert isinstance(leaf, str) and type(v) is int and entry.log_kinds <= set(C.LOG_KINDS)
        assert callable(entry.check_fields)


def test_an_oversized_leaf_integer_is_a_contract_error_at_its_own_site():
    with pytest.raises(ContractError):
        C._int("9" * 5000)
    with pytest.raises(ContractError):
        C._int("-" + "9" * 5000)

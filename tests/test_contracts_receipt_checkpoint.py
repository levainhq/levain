"""F10: the receipt_checkpoint leaf, walked in team-log order, against frozen/receipt_checkpoint/."""
import json

import pytest

from levain.contracts import ContractError, canon as C, note as N
from levain.contracts.leaves import receipt as R
from tests._contracts_vectors import GOLDEN, check_shape, h, ids, load

VECTORS = load("receipt_checkpoint")


@pytest.mark.parametrize("v", VECTORS, ids=ids(VECTORS))
def test_vector(v):
    check_shape(v)
    assert v["op"] == "anchors"
    i, want = v["input"], v["expect"]["levain"]
    got = R.judge_anchors([(h(x["data_hex"]), N.parse_vkey(x["vkey"])) for x in i["leaves"]], at=i["at"])
    assert [g for g, _ in got] == want["verdicts"], got


def test_the_design_lists_are_all_present():
    want = {"first-anchor", "second-anchor-with-proof", "equal-size-same-root-re-anchor-accepted",
            "equal-size-different-root-refused", "wrong-old-size-refused", "forked-second-anchor-refused",
            "truncated-anchor-refused", "size-2-53-refused", "size-2-53-minus-1-accepted"}
    assert want <= set(ids(VECTORS))


def test_fixture_origins_are_provisional_and_carry_the_receipt_prefix():
    status = (GOLDEN / "provisional" / "receipt_origin" / "STATUS").read_text()
    assert status.startswith("PROVISIONAL") and "P12" in status
    origins = json.loads((GOLDEN / "provisional" / "receipt_origin" / "origins.json").read_text())["origins"]
    assert all(o.startswith("levain-receipt/") for o in origins.values())


def test_no_package_code_builds_the_provisional_receipt_production():
    import levain.contracts as pkg
    from pathlib import Path
    for p in Path(pkg.__file__).parent.rglob("*.py"):
        assert "provisional" not in p.parts and "receipt_origin" not in p.read_text(encoding="utf-8"), p


def test_a_previous_anchor_for_another_origin_is_refused():
    v = next(v for v in VECTORS if v["id"] == "two-origins-anchor-independently")
    (a, ka), (b, kb) = [(h(x["data_hex"]), N.parse_vkey(x["vkey"])) for x in v["input"]["leaves"][:2]]
    prev = R.check_receipt_checkpoint(a, log_key=ka, previous=None)
    with pytest.raises(ContractError):
        R.check_receipt_checkpoint(b, log_key=kb, previous=prev)


def test_a_line_leaf_is_not_a_receipt_checkpoint():
    line = next(v for v in load("line") if v["id"] == "leaf-good-at-verify")
    key = N.parse_vkey(VECTORS[0]["input"]["leaves"][0]["vkey"])
    with pytest.raises(ContractError):
        R.check_receipt_checkpoint(h(line["input"]["data_hex"]), log_key=key, previous=None)


def test_on_a_fleet_or_receipt_log_the_leaf_is_refused():
    d = h(VECTORS[0]["input"]["leaves"][0]["data_hex"])
    assert C.classify_leaf(d, "team", at="verify") == C.ACCEPT
    assert C.classify_leaf(d, "fleet", at="verify") == C.REFUSE
    assert C.classify_leaf(d, "receipt", at="verify") == C.REFUSE


def test_at_is_closed():
    with pytest.raises(ValueError):
        R.judge_anchors([], at="later")

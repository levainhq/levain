"""F4 cosignature verification (to records) and the client-side advance rule, against the frozen vectors."""
import hashlib

import pytest

from levain.contracts import ContractError, checkpoint as C, note as N
from tests._contracts_vectors import check_shape, h, ids, load

VECTORS = load("cosig")


def _run(v):
    i = v["input"]
    if v["op"] == "verify_cosignatures":
        return C.verify_cosignatures(i["note"], [N.parse_vkey(k) for k in i["trusted"]], i["now"])
    if v["op"] == "check_advance":
        return C.check_advance(C.parse_text(i["held"]), C.parse_text(i["new"]), [h(p) for p in i["proof"]])
    raise AssertionError(v["op"])


@pytest.mark.parametrize("v", VECTORS, ids=ids(VECTORS))
def test_vector(v):
    check_shape(v)
    want = v["expect"]["levain"]
    if want["verdict"] == "refuse":
        with pytest.raises(ContractError):
            _run(v)
        return
    out = _run(v)
    if v["op"] == "verify_cosignatures":
        assert [{"cosigner_name": r.cosigner_name, "key_id": r.key_id.hex(), "timestamp": r.timestamp}
                for r in out] == want["records"]


def test_signed_message_is_the_spec_example():
    v = {x["id"]: x for x in VECTORS}["spec-example-input"]["input"]
    text = v["note"].split("\n\n")[0] + "\n"
    assert C.cosigned_message(1679315147, text) == v["signed_message"].encode()


def test_cosign_writes_the_vector_line():
    v = {x["id"]: x for x in VECTORS}["three-cosignatures"]["input"]
    text = v["note"].split("\n\n")[0] + "\n"
    seed = hashlib.sha256(b"levain-contracts/golden/cosig-1").digest()
    assert C.cosign(text, "witness.example/w1", seed, v["now"]) in v["note"]


def test_now_is_validated_and_never_read():
    v = {x["id"]: x for x in VECTORS}["three-cosignatures"]["input"]
    keys = [N.parse_vkey(k) for k in v["trusted"]]
    with pytest.raises(ContractError):
        C.verify_cosignatures(v["note"], keys, True)
    # the same note far in the past is refused: the verifier uses the caller's clock, not its own
    with pytest.raises(ContractError):
        C.verify_cosignatures(v["note"], keys, 1_000_000_000)


@pytest.mark.parametrize("ts", [-1, 2**63, 2**64, True, 1.0])
def test_cosign_refuses_a_bad_timestamp_before_packing(ts):
    v = {x["id"]: x for x in VECTORS}["spec-example-input"]["input"]
    text = v["note"].split("\n\n")[0] + "\n"
    with pytest.raises(ContractError):
        C.cosign(text, "w.example/w", bytes(32), ts)

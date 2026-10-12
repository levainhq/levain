"""F5: the tlog-witness add-checkpoint body encoder against hand-written expected bodies."""
import pytest

from levain.contracts import ContractError, checkpoint as C
from tests._contracts_vectors import check_shape, h, ids, load

VECTORS = load("witness")


@pytest.mark.parametrize("v", VECTORS, ids=ids(VECTORS))
def test_vector(v):
    check_shape(v)
    i = v["input"]
    want = v["expect"]["levain"]
    if want["verdict"] == "refuse":
        with pytest.raises(ContractError):
            C.add_checkpoint_body(i["old_size"], [h(p) for p in i["proof"]], i["checkpoint"])
    else:
        assert C.add_checkpoint_body(i["old_size"], [h(p) for p in i["proof"]], i["checkpoint"]) == want["body"].encode()


def test_sixty_three_lines_is_the_limit():
    v = {x["id"]: x for x in VECTORS}["old-5-new-9"]["input"]
    one = h(v["proof"][0])
    body = C.add_checkpoint_body(5, [one] * 63, v["checkpoint"])
    assert len(body.split(b"\n\n")[0].split(b"\n")) == 64   # the old line + 63 proof lines


def test_a_bytes_checkpoint_note_is_decoded_or_refused():
    v = {x["id"]: x for x in VECTORS}["old-5-new-9"]
    i = v["input"]
    proof = [h(p) for p in i["proof"]]
    assert C.add_checkpoint_body(5, proof, i["checkpoint"].encode()) == v["expect"]["levain"]["body"].encode()
    for bad in (i["checkpoint"].encode() + b"\xff", None, 7):
        with pytest.raises(ContractError):
            C.add_checkpoint_body(5, proof, bad)

"""§1.1 grammar against the frozen vectors, plus the measured reasons the rules exist."""
import pytest

from levain.contracts import ContractError, grammar as G
from tests._contracts_vectors import check_shape, ids, load

VECTORS = load("grammar")
_MAX = {"note": G.NOTE_INT_MAX, "json": G.JSON_INT_MAX}


def _run(v):
    i = v["input"]
    if v["op"] == "decimal":
        return G.parse_decimal(i["text"])
    if v["op"] == "b64":
        return G.b64decode(i["text"]).hex()
    if v["op"] == "int":
        return G.check_int(i["value"], _MAX[i["domain"]])
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
    if v["op"] == "decimal":
        assert out == want["value"]
    elif v["op"] == "b64":
        assert out == want["bytes"]


def test_the_bounds_are_the_stated_powers_of_two():
    assert (G.NOTE_INT_MAX, G.JSON_INT_MAX) == (2**63 - 1, 2**53 - 1)


@pytest.mark.parametrize("text", ["+1", " 1", "1_0", "１２"])
def test_int_would_have_accepted_what_the_grammar_refuses(text):
    """Measured: Python's int() parses each of these, which is why the grammar never calls it on unchecked text."""
    int(text)
    with pytest.raises(ContractError):
        G.parse_decimal(text)


def test_round_trip_encode():
    for raw in (b"", b"\x00", b"\xff" * 31, bytes(range(32))):
        assert G.b64decode(G.b64encode(raw)) == raw

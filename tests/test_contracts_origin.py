"""F6 origins: the grammar, the frozen prefixes and the team production, against the frozen vectors."""
import pytest

from levain.contracts import ContractError, origin as O
from tests._contracts_vectors import check_shape, ids, load

VECTORS = load("origin")
_CHECK = {"grammar": O.check_grammar, "log_kind": O.log_kind, "team": O.check_team_origin}


@pytest.mark.parametrize("v", VECTORS, ids=ids(VECTORS))
def test_vector(v):
    check_shape(v)
    i = v["input"]
    want = v["expect"]["levain"]
    if want["verdict"] == "refuse":
        with pytest.raises(ContractError):
            _CHECK[i["check"]](i["origin"])
    else:
        out = _CHECK[i["check"]](i["origin"])
        if "log_kind" in want:
            assert out == want["log_kind"]


def test_every_levain_origin_is_a_legal_note_key_name():
    from levain.contracts import note as N
    for v in VECTORS:
        if v["expect"]["levain"]["verdict"] == "accept":
            assert N.valid_key_name(v["input"]["origin"])


def test_team_origin_builder():
    assert O.team_origin("0" * 32) == "levain-team/" + "0" * 32
    with pytest.raises(ContractError):
        O.team_origin("0" * 31)

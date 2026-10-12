"""F3 checkpoints against the frozen vectors."""
import pytest

from levain.contracts import ContractError, checkpoint as C, ed25519 as E, note as N
from tests._contracts_vectors import check_shape, ids, load

VECTORS = load("checkpoint")


@pytest.mark.parametrize("v", VECTORS, ids=ids(VECTORS))
def test_vector(v):
    check_shape(v)
    i = v["input"]
    key = N.parse_vkey(i["log_vkey"])
    want = v["expect"]["levain"]
    if want["verdict"] == "refuse":
        with pytest.raises(ContractError):
            C.verify(i["note"], key, team=i["team"])
    else:
        cp, _ = C.verify(i["note"], key, team=i["team"])
        assert (cp.origin, cp.size, cp.root.hex()) == (want["origin"], want["size"], want["root"])


def test_sign_produces_the_vector_bytes():
    """The log signer writes exactly the golden note (Ed25519 is deterministic)."""
    import hashlib
    v = {x["id"]: x for x in VECTORS}["team-size-1"]
    seed = hashlib.sha256(b"levain-contracts/golden/log-team").digest()
    cp = C.parse_text(v["input"]["note"].split("\n\n")[0] + "\n")
    assert C.sign(cp, seed) == v["input"]["note"]
    assert N.make_key(cp.origin, E.public_from_seed(seed)).vkey() == v["input"]["log_vkey"]


def test_sign_refuses_extension_lines_and_foreign_origins():
    seed = bytes(32)
    with pytest.raises(ContractError):
        C.sign(C.Checkpoint("levain-team/" + "a" * 32, 1, bytes(32), ("x",)), seed)
    with pytest.raises(ContractError):
        C.sign(C.Checkpoint("example.com/log", 1, bytes(32), ()), seed)


@pytest.mark.parametrize("text", ["o\n1\n", "o\n1\n" + "A" * 43 + "=\n\n", "\n1\nAA==\n", "o\n1\nAA==\n"])
def test_parse_text_refusals(text):
    with pytest.raises(ContractError):
        C.parse_text(text)


def test_size_zero_needs_the_empty_root_at_sign_parse_and_cosign():
    """tlog-witness: a size-0 checkpoint's root is SHA-256(""); one check serves sign, verify, cosign and F5."""
    import hashlib
    seed = bytes(32)
    origin = "levain-team/" + "a" * 32
    with pytest.raises(ContractError):
        C.sign(C.Checkpoint(origin, 0, bytes(32), ()), seed)
    good = C.sign(C.Checkpoint(origin, 0, hashlib.sha256(b"").digest(), ()), seed)
    assert C.verify(good, N.make_key(origin, E.public_from_seed(seed)), team=True)[0].size == 0
    bad_text = f"{origin}\n0\n{'A' * 43}=\n"
    with pytest.raises(ContractError):
        C.parse_text(bad_text)
    with pytest.raises(ContractError):
        C.cosign(bad_text, "w.example/w", seed, 1)

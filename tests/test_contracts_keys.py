"""F7 ``fp_of_blob`` / F6 ``fp_hex`` against keys whose fingerprints ``ssh-keygen -lf`` printed, and T's delegation."""
import shutil
import subprocess

import pytest

from levain.contracts import ContractError, keys as K, origin as O
from levain.team import signing as S
from tests._contracts_vectors import FROZEN, check_shape, ids, load

VECTORS = load("fingerprint")


@pytest.mark.parametrize("v", VECTORS, ids=ids(VECTORS))
def test_vector(v):
    check_shape(v)
    line = v["input"]["line"]
    want = v["expect"]["levain"]
    if want["verdict"] == "refuse":
        with pytest.raises(ContractError):
            K.parse_pubkey_line(line)
        return
    _, blob = K.parse_pubkey_line(line)
    assert K.fp_of_blob(blob) == want["fp"] and K.fingerprint_line(line) == want["fp"]
    assert O.fp_hex(blob) == want["fp_hex"]
    assert (FROZEN / "fingerprint" / v["input"]["file"]).read_text().strip() == line


@pytest.mark.skipif(not shutil.which("ssh-keygen"), reason="no ssh-keygen")
@pytest.mark.parametrize("v", [v for v in VECTORS if "file" in v["input"]], ids=lambda v: v["id"])
def test_ssh_keygen_still_agrees(v):
    out = subprocess.run(["ssh-keygen", "-lf", str(FROZEN / "fingerprint" / v["input"]["file"])],
                         capture_output=True, text=True, check=True).stdout
    assert v["expect"]["levain"]["fp"] in out.split()


@pytest.mark.parametrize("v", [v for v in VECTORS if v["id"] in (
    "fingerprint-ssh-ed25519", "fingerprint-ecdsa-sha2-nistp256", "fingerprint-ecdsa-sha2-nistp384",
    "fingerprint-ecdsa-sha2-nistp521")], ids=lambda v: v["id"])
def test_team_fingerprint_delegates_to_fp_of_blob(v):
    """T's ``fingerprint()`` keeps its allowlist and returns ``fp_of_blob``'s value for the types it accepts."""
    assert S.fingerprint(v["input"]["line"]) == v["expect"]["levain"]["fp"]


@pytest.mark.parametrize("vid", ["fingerprint-sk-ssh-ed25519", "fingerprint-sk-ecdsa-sha2-nistp256"])
def test_team_allowlist_still_refuses_what_f7_fingerprints(vid):
    v = {x["id"]: x for x in VECTORS}[vid]
    assert K.fingerprint_line(v["input"]["line"]) == v["expect"]["levain"]["fp"]
    with pytest.raises(S.SigningError):
        S.fingerprint(v["input"]["line"])


def test_certificates_are_outside_f7():
    with pytest.raises(ContractError):
        K.parse_pubkey_line("ssh-ed25519-cert-v01@openssh.com AAAA")


def test_ed25519_blob_round_trip_and_exact_consumption():
    pub = bytes(range(32))
    blob = K.ed25519_blob(pub)
    assert K.ed25519_pub_from_blob(blob) == pub
    for bad in (blob + b"\x00", blob[:-1], K.ssh_string(b"ssh-ed25519") + K.ssh_string(bytes(31))):
        with pytest.raises(ContractError):
            K.ed25519_pub_from_blob(bad)


def test_a_non_canonical_base64_blob_is_refused():
    """Non-zero pad bits decode to the same blob under a lax decoder; F7 uses the canonical one (verify r2, V3)."""
    good = (FROZEN / "fingerprint" / "ecdsa-sha2-nistp256.pub").read_text().strip()
    typ, b64, comment = good.split()
    assert b64.endswith("g=")   # 2 pad bits, both zero
    bad = f"{typ} {b64[:-2]}h= {comment}"
    K.parse_pubkey_line(good)
    with pytest.raises(ContractError):
        K.parse_pubkey_line(bad)

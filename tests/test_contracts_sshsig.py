"""F12: SSHSIG verification for ``device_sig``, in-process, against frozen/sshsig/ and against ssh-keygen at test time.

ssh-keygen is a test-time cross-check only (S1_DESIGN F12): every valid control, armored per PROTOCOL.sshsig §1, must
pass ``ssh-keygen -Y check-novalidate`` on any OpenSSH; the recorded upstream verdict of every other vector is
compared only under the OpenSSH version the vectors recorded (its Ed25519 verdicts move between versions, F12);
and every blob ``ssh-keygen -Y sign`` makes with a throwaway key, de-armored, verifies in levain.
"""
import base64
import json
import shutil
import subprocess
import sys

import pytest

from levain.contracts import ContractError, sshsig as S
from levain.contracts.keys import fp_of_blob, parse_pubkey_line
from levain.contracts.namespaces import TEAM_ENTRY_NAMESPACE
from tests._contracts_vectors import FROZEN, GOLDEN, check_shape, h, ids, load

VECTORS = load("sshsig")
DOC = json.loads((FROZEN / "sshsig" / "vectors.json").read_text(encoding="utf-8"))
HAVE_KEYGEN = shutil.which("ssh-keygen") is not None
SSH_V = (subprocess.run(["ssh", "-V"], capture_output=True, text=True).stderr.strip().split(",")[0]
         if shutil.which("ssh") else None)

sys.path.insert(0, str(GOLDEN))
try:
    from _genlib import armor, dearmor
finally:
    sys.path.remove(str(GOLDEN))


def _verdict(v) -> str:
    i = v["input"]
    try:
        S.verify(base64.b64decode(i["blob_b64"]), namespace=i["namespace"], device_fp=i["device_fp"],
                 message=h(i["message_hex"]))
    except ContractError:
        return "refuse"
    return "accept"


@pytest.mark.parametrize("v", VECTORS, ids=ids(VECTORS))
def test_vector(v):
    check_shape(v)
    i, want = v["input"], v["expect"]["levain"]
    if v["op"] == "signed_data":
        assert S.signed_data(i["namespace"], h(i["message_hex"])).hex() == want["bytes_hex"]
    elif v["op"] == "verify":
        assert i["namespace"] == TEAM_ENTRY_NAMESPACE
        assert _verdict(v) == want["verdict"]
    else:
        raise AssertionError(v["op"])


def test_the_vector_set_holds_every_case_the_design_lists():
    want = {"signed-data-fixed-message", "good", "wrong-namespace-refused", "hash-sha256-refused",
            "reserved-non-empty-refused", "embedded-key-not-device-fp-refused", "ecdsa-p256-key-refused",
            "one-byte-tampered-refused", "wrong-key-refused", "trailing-byte-outer-blob-refused",
            "trailing-byte-key-blob-refused", "trailing-byte-signature-blob-refused", "s-plus-l-mutation-refused"}
    assert want <= set(ids(VECTORS))
    assert DOC["message"] == {"origin": "levain-team/" + "a" * 32, "file": "ledger/dana/findings.jsonl",
                              "chain_hash": "00" * 31 + "01"}


def _check_novalidate(blob: bytes, message: bytes, tmp_path) -> int:
    sig = tmp_path / "sig"
    sig.write_text(armor(blob))
    return subprocess.run(["ssh-keygen", "-Y", "check-novalidate", "-n", TEAM_ENTRY_NAMESPACE, "-s", str(sig)],
                          input=message, capture_output=True).returncode


@pytest.mark.skipif(not HAVE_KEYGEN, reason="no ssh-keygen")
@pytest.mark.parametrize("v", [v for v in VECTORS if v["op"] == "verify" and v["expect"]["levain"]["verdict"] == "accept"],
                         ids=lambda v: v["id"])
def test_ssh_keygen_accepts_every_armored_valid_control(v, tmp_path):
    i = v["input"]
    assert _check_novalidate(base64.b64decode(i["blob_b64"]), h(i["message_hex"]), tmp_path) == 0


@pytest.mark.skipif(not HAVE_KEYGEN, reason="no ssh-keygen")
@pytest.mark.parametrize("v", [v for v in VECTORS if v["expect"]["upstream"] != "n/a"], ids=lambda v: v["id"])
def test_ssh_keygen_gives_the_recorded_upstream_verdict(v, tmp_path):
    up = v["expect"]["upstream"]
    if SSH_V != up["version"]:
        pytest.skip(f"vectors recorded {up['version']}; this machine runs {SSH_V}")
    i = v["input"]
    rc = _check_novalidate(base64.b64decode(i["blob_b64"]), h(i["message_hex"]), tmp_path)
    assert ("accept" if rc == 0 else "refuse") == up["verdict"]


@pytest.mark.skipif(not HAVE_KEYGEN, reason="no ssh-keygen")
@pytest.mark.parametrize("message", [b"", b"x", "unicode é \U0001F35E".encode(), bytes(range(256)) * 9])
def test_every_ssh_keygen_sign_blob_verifies_in_levain(message, tmp_path):
    key, msg = tmp_path / "k", tmp_path / "m"
    msg.write_bytes(message)
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "", "-f", str(key)], check=True)
    subprocess.run(["ssh-keygen", "-Y", "sign", "-n", TEAM_ENTRY_NAMESPACE, "-f", str(key), str(msg)], check=True,
                   capture_output=True)
    blob = dearmor((tmp_path / "m.sig").read_text())
    device_fp = fp_of_blob(parse_pubkey_line((tmp_path / "k.pub").read_text())[1])
    S.verify(blob, namespace=TEAM_ENTRY_NAMESPACE, device_fp=device_fp, message=message)
    with pytest.raises(ContractError):
        S.verify(blob, namespace=TEAM_ENTRY_NAMESPACE, device_fp=device_fp, message=message + b"!")


def test_the_ecdsa_golden_is_a_real_ssh_keygen_blob():
    blob = dearmor((FROZEN / "sshsig" / "ecdsa-p256.sig").read_text())
    sig = S.parse(blob)
    assert sig.namespace == TEAM_ENTRY_NAMESPACE and sig.hash_algorithm == "sha512"
    assert sig.public_key[4:4 + 19] == b"ecdsa-sha2-nistp256"


@pytest.mark.parametrize("blob", [b"", b"SSHSI", b"SSHSIG", b"SSHSIG\x00\x00\x00\x02", b"XSHSIG\x00\x00\x00\x01",
                                  b"SSHSIG\x00\x00\x00\x01\x00\x00\x00\x05ab", "SSHSIG"])
def test_malformed_blobs_are_refused_not_crashed(blob):
    with pytest.raises(ContractError):
        S.parse(blob)


def test_a_non_ascii_namespace_or_algorithm_is_refused():
    good = base64.b64decode(next(v for v in VECTORS if v["id"] == "good")["input"]["blob_b64"])
    bad = good.replace(b"\x00\x00\x00\x06sha512", b"\x00\x00\x00\x06sha51\xff")
    with pytest.raises(ContractError):
        S.parse(bad)


def test_signed_data_refuses_any_hash_but_sha512():
    with pytest.raises(ContractError):
        S.signed_data(TEAM_ENTRY_NAMESPACE, b"m", "sha256")


def test_a_non_bytes_message_is_refused():
    v = next(v for v in VECTORS if v["id"] == "good")
    with pytest.raises(ContractError):
        S.verify(base64.b64decode(v["input"]["blob_b64"]), namespace=TEAM_ENTRY_NAMESPACE,
                 device_fp=v["input"]["device_fp"], message=h(v["input"]["message_hex"]).decode("latin-1"))

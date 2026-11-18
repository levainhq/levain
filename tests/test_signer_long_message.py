"""1118-12 (desk 25): Phill's long terminal sign (~1,000 characters) was refused 11-18: ssh-keygen ran 0.9 s (he touched) and
returned a signature that "does not verify" as his Touch ID key. The key is a Secure Enclave key and cannot be driven here
(tests never reach a real key), so these hold what a throwaway key CAN: that the signing path itself carries a long, multi-line,
non-ASCII message byte for byte (a message's length changes only the hash that ssh-keygen signs, never the signed blob), and that
the words an error now carries tell the two causes apart: a signature made over other bytes than the message, or a key the
verifier did not accept. Not discharged: the Secure Enclave provider itself, which only his next sign can show."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from levain.authority_sig import verify_failure_detail
from levain.cockpit.keys import KeyError_, signing_public_key, ssh_signer


def _key(tmp_path: Path) -> Path:
    d = tmp_path / "k"
    d.mkdir(mode=0o700)
    path = d / "k"
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "t", "-f", str(path)], check=True)
    return path


@pytest.mark.parametrize("size", [800, 1_000, 4_096, 20_000])
def test_a_long_multiline_message_signs_and_verifies(tmp_path, size):
    key = _key(tmp_path)
    fp, sign = ssh_signer(key)
    text = ("sign: 1267: 100% agree on your analysis here, \"quoted\", a back\\slash, and an é and a ✓ on a line\n" * 400)
    message = text.encode("utf-8")[:size]
    assert len(message) == size
    assert sign(message).startswith("-----BEGIN SSH SIGNATURE-----")


def test_the_failure_detail_tells_a_signature_over_other_bytes_from_a_refused_key(tmp_path):
    key = _key(tmp_path)
    fp, sign = ssh_signer(key)
    message = b"sign: 1267: " + b"x" * 1_000
    signature = sign(message)
    pub = signing_public_key(key)
    ok = verify_failure_detail(message, signature, public_key=pub)
    assert "verify: Good" in ok and "check-novalidate: Good" in ok, ok
    assert f"message {len(message)} bytes" in ok
    other = verify_failure_detail(message + b"!", signature, public_key=pub)
    assert "verify: Good" not in other, other
    assert "check-novalidate: Good" not in other, other


def test_the_failure_detail_never_raises(tmp_path):
    out = verify_failure_detail(b"m", "not a signature", public_key="not a key")
    assert isinstance(out, str) and out


def test_a_refused_signature_error_carries_the_detail(tmp_path, monkeypatch):
    key = _key(tmp_path)
    fp, sign = ssh_signer(key)
    import levain.cockpit.keys as keys
    monkeypatch.setattr(keys._authority, "verify_link_sig", lambda *a, **k: False)
    with pytest.raises(KeyError_) as err:
        sign(b"sign: 1267: " + b"y" * 1_000)
    text = str(err.value)
    assert "does not verify as" in text and "check-novalidate: Good" in text and "message 1012 bytes" in text, text

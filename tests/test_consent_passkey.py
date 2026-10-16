"""Q6b: a WebAuthn passkey assertion carried as an OpenSSH signature, checked by the verifier the consent queue uses."""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from levain.authority import PASSKEY, custody_problem, verify_link_sig
from levain.cockpit.keys import fingerprint_of
from levain.consent.passkey import (
    KEY_TYPE, PasskeyError, challenge, key_application, public_key_line, to_sshsig,
)

_FIX = json.loads((Path(__file__).parent / "fixtures" / "passkey_assertion_virtual.json").read_text(encoding="utf-8"))
_SPKI_PREFIX = bytes.fromhex("3059301306072a8648ce3d020106082a8648ce3d030107034200")
needs_ssh_keygen = pytest.mark.skipif(shutil.which("ssh-keygen") is None, reason="needs ssh-keygen")


def _parts():
    key = public_key_line(_SPKI_PREFIX + bytes.fromhex(_FIX["pub_point_hex"]), _FIX["rp_id"])
    return (key, bytes.fromhex(_FIX["authData_hex"]), _FIX["clientDataJSON"].encode(), bytes.fromhex(_FIX["sig_der_hex"]))


@needs_ssh_keygen
def test_a_recorded_passkey_assertion_verifies_as_an_sshsig_and_a_changed_message_does_not():
    key, auth, cdj, der = _parts()
    message = _FIX["message"].encode()
    assert key.startswith(KEY_TYPE + " ") and key_application(key) == "localhost"
    # the recorded challenge is exactly this module's challenge for the recorded message
    assert json.loads(cdj)["challenge"].rstrip("=") == __import__("base64").urlsafe_b64encode(
        challenge(message, _FIX["namespace"])).decode().rstrip("=")
    sig = to_sshsig(key, auth, cdj, der, namespace=_FIX["namespace"])
    fp = fingerprint_of(key)
    assert verify_link_sig(message, sig, public_key=key, fingerprint=fp, namespace=_FIX["namespace"]) is True
    tampered = message.replace(b"allow", b"allpw")
    assert verify_link_sig(tampered, sig, public_key=key, fingerprint=fp, namespace=_FIX["namespace"]) is False


def test_shape_refusals_name_the_site_and_the_presence_flag():
    key, auth, cdj, der = _parts()
    other = public_key_line(_SPKI_PREFIX + bytes.fromhex(_FIX["pub_point_hex"]), "example.ts.net")
    with pytest.raises(PasskeyError, match="another site"):
        to_sshsig(other, auth, cdj, der)
    no_presence = auth[:32] + bytes([auth[32] & ~0x01]) + auth[33:]
    with pytest.raises(PasskeyError, match="user presence"):
        to_sshsig(key, no_presence, cdj, der)
    with pytest.raises(PasskeyError, match="not https"):
        to_sshsig(key, auth, cdj.replace(b"http://localhost:8765", b"http://evil.example"), der)


def test_a_passkey_answers_for_a_display_phone_key_only():
    assert custody_problem("display-phone", PASSKEY, KEY_TYPE) is None
    assert "display-phone key only" in custody_problem("laptop-presence", PASSKEY, KEY_TYPE)
    assert custody_problem("display-phone", PASSKEY, "ssh-ed25519") is not None


def test_the_presence_rule_reads_a_webauthn_signature():
    import base64

    from levain.authority import presence_missing

    key, auth, cdj, der = _parts()
    sig = to_sshsig(key, auth, cdj, der, namespace=_FIX["namespace"])
    assert presence_missing(sig, key) is False
    blob = bytearray(base64.b64decode("".join(sig.strip().splitlines()[1:-1])))
    flags_at = blob.rfind(bytes([auth[32]]) + auth[33:37])     # flags byte, then the counter
    assert flags_at > 0
    blob[flags_at] &= ~0x01
    text = base64.b64encode(bytes(blob)).decode()
    no_touch = "-----BEGIN SSH SIGNATURE-----\n" + text + "\n-----END SSH SIGNATURE-----\n"
    assert presence_missing(no_touch, key) is True

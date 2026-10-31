"""A passkey's authority and its answer's signature, pinned without any server (moved from the retired
``tests/test_phone_server.py`` when the phone port's token routes went, W1b slice C): a display-phone key signs no
authority link but a revoke, and an answer must be a verified same-origin WebAuthn signature."""
from __future__ import annotations

import base64
import shutil
import sqlite3
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

import levain.authority as auth
from levain.authority import ReplayCache, anchor_path
from levain.consent.passkey import public_key_line
from tests._phone_fixtures import _FIX, _SPKI, world  # noqa: F401  (world is a fixture)

pytestmark = pytest.mark.skipif(shutil.which("ssh-keygen") is None, reason="needs ssh-keygen")


def test_a_passkey_key_signs_no_authority_link_but_a_revoke(world) -> None:
    # L3 r2 HIGH: a display-phone key's rank covers a renew or a tighten; its passkey custody refuses all but a revoke
    conn = sqlite3.connect(world["root"] / "cockpit.db", isolation_level=None)
    try:
        with pytest.raises(auth.AuthorityError, match="consent answers and grant revokes only, never another"):
            auth.sign_and_append(conn, {"op": "raise", "verb": "spore_touch", "tier": "T3"},
                                 signer_fp=world["passkey_fp"], sign=lambda m: "not a signature",
                                 clock=lambda: datetime.now(timezone.utc), cache=ReplayCache(),
                                 anchor=anchor_path(world["root"]))
    finally:
        conn.close()


def test_a_passkey_answer_must_be_a_verified_same_origin_webauthn_signature() -> None:
    from levain.consent.passkey import answer_signature_problem, to_sshsig

    key = public_key_line(_SPKI, _FIX["rp_id"])
    auth_data = bytes.fromhex(_FIX["authData_hex"])
    good = to_sshsig(key, auth_data, _FIX["clientDataJSON"].encode(), bytes.fromhex(_FIX["sig_der_hex"]),
                     namespace=_FIX["namespace"])
    assert answer_signature_problem(good, key) is None
    # the same signature with the user-verification bit cleared (a wrapper outside the cockpit could send it)
    blob = bytearray(base64.b64decode("".join(good.strip().splitlines()[1:-1])))
    at = blob.rfind(bytes([auth_data[32]]) + auth_data[33:37])
    blob[at] &= ~0x04
    no_uv = "-----BEGIN SSH SIGNATURE-----\n" + base64.b64encode(bytes(blob)).decode() + "\n-----END SSH SIGNATURE-----\n"
    assert "did not verify the user" in answer_signature_problem(no_uv, key)
    assert answer_signature_problem("garbage", key) is not None


def test_a_passkey_answer_that_is_a_plain_ssh_signature_is_refused(tmp_path: Path) -> None:
    from levain.consent.passkey import answer_signature_problem

    plain = tmp_path / "plain"
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(plain)], check=True)
    sig = subprocess.run(["ssh-keygen", "-Y", "sign", "-q", "-f", str(plain), "-n", "levain-consent"],
                         input=b"x", capture_output=True, check=True).stdout.decode()
    assert "WebAuthn" in answer_signature_problem(sig, public_key_line(_SPKI, "localhost"))







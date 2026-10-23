"""D2 of the cockpit signing design (project_memory/1023-21/sign/DESIGN_SIGN.md): a passkey verifier given an explicit
https page origin holds the page to exactly it, port included, and one given none keeps the portless rule every other
levain verifier relies on. A malformed pin is a ValueError when it is set up, never a silent accept."""
from __future__ import annotations

import base64
import hashlib
import json
import os
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

from levain.authority import AuthorityKey
from levain.cockpit.keys import fingerprint_of
from levain.consent.passkey import (PasskeyError, _client_origin, answer_signature_problem, public_key_line,
                                    to_sshsig)
from levain.consent.sign import Signer, SignRefused

RP, NS = "box.example", "flow-needs-phill"
PINNED = f"https://{RP}:8443"


def _b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def _cdj(origin: str, *, cross: bool = False, challenge: str = "x") -> bytes:
    return json.dumps({"type": "webauthn.get", "challenge": challenge, "origin": origin, "crossOrigin": cross},
                      separators=(",", ":")).encode()


class _Passkey:
    """A software P-256 passkey on ``RP``: what a phone's authenticator returns, built the way the virtual
    authenticator's assertions are (authenticatorData flags UP|UV, a DER ECDSA signature over authData||sha256(cdj))."""

    def __init__(self, rp: str = RP) -> None:
        self.rp = rp
        self.priv = ec.generate_private_key(ec.SECP256R1())
        spki = self.priv.public_key().public_bytes(serialization.Encoding.DER,
                                                   serialization.PublicFormat.SubjectPublicKeyInfo)
        self.line = public_key_line(spki, rp)
        self.key = AuthorityKey(fingerprint=fingerprint_of(self.line), public_key=self.line, key_class="display-phone",
                                enrolled_seq=1)

    def assertion(self, challenge: str, origin: str, *, flags: int = 0x05, cross: bool = False) -> dict:
        auth = hashlib.sha256(self.rp.encode()).digest() + bytes([flags]) + (1).to_bytes(4, "big")
        cdj = _cdj(origin, cross=cross, challenge=challenge)
        der = self.priv.sign(auth + hashlib.sha256(cdj).digest(), ec.ECDSA(hashes.SHA256()))
        return {"authenticator_data": _b64u(auth), "client_data_json": _b64u(cdj), "signature": _b64u(der)}

    def raw(self, origin: str, *, cross: bool = False) -> "tuple[bytes, bytes, bytes]":
        a = self.assertion("x", origin, cross=cross)
        return tuple(base64.urlsafe_b64decode(a[k] + "=" * (-len(a[k]) % 4))      # type: ignore[return-value]
                     for k in ("authenticator_data", "client_data_json", "signature"))


def _file(qdir: Path, rid: str, message: str = "I attest this.\nnonce: 41f0") -> None:
    sign = qdir / "sign"
    sign.mkdir(parents=True, exist_ok=True)
    body = {"format": "levain-sign-choice/1", "id": rid, "namespace": NS, "message": message, "title": "t",
            "requester": "test", "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
            "options": [{"key": "a", "text": "keep it"}, {"key": "b", "text": "drop it"}]}
    (sign / f".{rid}.tmp").write_text(json.dumps(body))
    os.replace(sign / f".{rid}.tmp", sign / f"{rid}.json")


def test_an_unpinned_verifier_refuses_a_port_and_a_pinned_one_takes_exactly_its_page() -> None:
    """Red on main for the pinned :8443 accept: main ignored ``page_origin`` for any name but localhost, so the https
    name tailscale serves on :8443 could never sign. Every other half held on main and must keep holding."""
    with pytest.raises(PasskeyError, match="exactly: no port"):
        _client_origin(_cdj(PINNED), RP)                           # no pin: the portless rule stands
    assert _client_origin(_cdj(f"https://{RP}"), RP) == f"https://{RP}"
    assert _client_origin(_cdj(PINNED), RP, PINNED) == PINNED        # pinned: exactly the page
    for wrong in (f"https://{RP}:9999", f"http://{RP}:8443", f"https://{RP}", "https://other.example:8443",
                  f"https://{RP}:8443/", f"https://u@{RP}:8443"):
        with pytest.raises(PasskeyError, match="exactly: scheme, name and port"):
            _client_origin(_cdj(wrong), RP, PINNED)
    with pytest.raises(PasskeyError, match="another site"):
        _client_origin(_cdj(PINNED, cross=True), RP, PINNED)


def test_a_malformed_pin_is_refused_never_compared() -> None:
    """A pin that is not ``https://<rp>[:port]`` (for localhost also http) is a PasskeyError (a ValueError) at the
    verifier, whatever origin the assertion names: an exact compare against a bad pin must never become the rule."""
    from levain.consent.passkey import page_origin_problem

    assert page_origin_problem(RP, PINNED) is None and page_origin_problem(RP, f"https://{RP}") is None
    assert page_origin_problem("localhost", "http://localhost:7999") is None
    for bad in (f"http://{RP}:8443", "https://other.example:8443", f"https://{RP}:8443/", f"https://u@{RP}:8443",
                f"https://{RP}:0", f"https://{RP}:443", f"https://{RP}:08443", f"https://{RP.upper()}:8443",
                f"https://{RP}:8443?x", f"https://{RP}:8443#x", f"https://{RP}:x", f"https://{RP}:99999", "",
                f"https://[{RP}]:8443", "http://localhost:7999/", "ftp://localhost:7999", 8443):
        assert page_origin_problem(RP if "localhost" not in str(bad) else "localhost", bad), bad
        with pytest.raises(ValueError):
            _client_origin(_cdj(str(bad)), RP if "localhost" not in str(bad) else "localhost", bad)


def test_a_real_assertion_converts_and_verifies_only_on_its_pinned_page() -> None:
    pk = _Passkey()
    auth, cdj, der = pk.raw(PINNED)
    with pytest.raises(PasskeyError, match="exactly: no port"):
        to_sshsig(pk.line, auth, cdj, der, namespace=NS)
    sig = to_sshsig(pk.line, auth, cdj, der, namespace=NS, page_origin=PINNED)
    assert answer_signature_problem(sig, pk.line, PINNED) is None
    # the same signature re-verified with no pin (any levain verifier that passes none) is refused
    assert "exactly: no port" in answer_signature_problem(sig, pk.line)
    with pytest.raises(PasskeyError, match="exactly: scheme, name and port"):
        to_sshsig(pk.line, auth, cdj, der, namespace=NS, page_origin=f"https://{RP}:9999")


def test_a_signer_pinned_to_8443_signs_there_and_nowhere_else(tmp_path: Path) -> None:
    """The Signer path (main's code path for D6 2): pinned to :8443, an assertion from :9999, http or a cross-origin
    frame is refused with no file, and the :8443 one is recorded and verifies with stock ssh-keygen."""
    pk = _Passkey()
    signer = Signer(tmp_path, (NS,), RP, lambda: {pk.key.fingerprint: pk.key})
    signer.page_origin = PINNED
    rid = "pinned-8443-0001"
    _file(tmp_path, rid)

    def challenge() -> str:
        [row] = signer.listing([pk.key.fingerprint])
        return signer.challenge(rid, {"signer_fp": pk.key.fingerprint, "request_sha256": row["sha256"],
                                      "choice": "b"})["challenge"]

    for origin, cross in ((f"https://{RP}:9999", False), (f"http://{RP}:8443", False), (f"https://{RP}", False),
                          (PINNED, True)):
        with pytest.raises(SignRefused):
            signer.answer(rid, pk.assertion(challenge(), origin, cross=cross))
        assert not (tmp_path / "sign" / f"{rid}.signed.json").exists() and not (tmp_path / "sign" / f"{rid}.sig").exists()
    done = signer.answer(rid, pk.assertion(challenge(), PINNED))
    assert done["choice"] == "b"
    rec = json.loads((tmp_path / "sign" / f"{rid}.signed.json").read_text())
    (tmp_path / "allowed").write_text(f'x namespaces="{NS}" {pk.line}\n')
    assert subprocess.run(["ssh-keygen", "-Y", "verify", "-f", str(tmp_path / "allowed"), "-I", "x", "-n", NS, "-s",
                           str(tmp_path / "sign" / f"{rid}.sig")], input=rec["signed_message"].encode(),
                          capture_output=True).returncode == 0

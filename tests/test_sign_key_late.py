"""A plain sign request may name its signer late: the challenge carries no ``signer_fp`` and the answer is tried against the
active passkeys as they are at answer time. Nothing about who may sign changes: a signature verifies under exactly one key,
a bound challenge is still bound, and the cap on tried keys is a bound."""
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
from levain.consent import sign as signmod
from levain.consent.passkey import public_key_line
from levain.consent.sign import Signer, SignRefused

RP, ORIGIN, NS = "localhost", "http://localhost:7999", "example-attest"
RID = "late-0001"


def _b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


class _Passkey:
    """One software passkey: its authority key and the assertion it gives over a challenge."""

    def __init__(self, seq: int) -> None:
        self.priv = ec.generate_private_key(ec.SECP256R1())
        spki = self.priv.public_key().public_bytes(serialization.Encoding.DER,
                                                    serialization.PublicFormat.SubjectPublicKeyInfo)
        line = public_key_line(spki, RP)
        self.key = AuthorityKey(fingerprint=fingerprint_of(line), public_key=line, key_class="display-phone",
                                enrolled_seq=seq, enrolled_at=f"2026-11-0{seq}T10:00:00Z")

    def assertion(self, challenge: str) -> dict:
        auth = hashlib.sha256(RP.encode()).digest() + bytes([0x05]) + (1).to_bytes(4, "big")
        cdj = json.dumps({"type": "webauthn.get", "challenge": challenge, "origin": ORIGIN, "crossOrigin": False},
                         separators=(",", ":")).encode()
        der = self.priv.sign(auth + hashlib.sha256(cdj).digest(), ec.ECDSA(hashes.SHA256()))
        return {"authenticator_data": _b64u(auth), "client_data_json": _b64u(cdj), "signature": _b64u(der)}


def _file(qdir: Path, rid: str = RID) -> None:
    sign = qdir / "sign"
    sign.mkdir(exist_ok=True)
    body = {"format": "levain-sign-request/1", "id": rid, "namespace": NS, "message": "I attest this.",
            "title": "report", "requester": "test",
            "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()}
    (sign / f".{rid}.tmp").write_text(json.dumps(body))
    os.replace(sign / f".{rid}.tmp", sign / f"{rid}.json")


@pytest.fixture
def rig(tmp_path: Path):
    a, b, c = _Passkey(1), _Passkey(2), _Passkey(3)
    active = {a.key.fingerprint: a.key, b.key.fingerprint: b.key}
    signer = Signer(tmp_path, (NS,), RP, lambda: dict(active))
    signer.page_origin = ORIGIN
    _file(tmp_path)

    def sha() -> str:
        return next(r["sha256"] for r in signer.listing(list(active)) if r["id"] == RID)

    class Rig:
        pass

    r = Rig()
    r.signer, r.a, r.b, r.c, r.active, r.sha, r.dir = signer, a, b, c, active, sha, tmp_path
    return r


def _verify(tmp_path: Path, key: AuthorityKey) -> int:
    (tmp_path / "allowed").write_text(f'x namespaces="{NS}" {key.public_key}\n')
    return subprocess.run(["ssh-keygen", "-Y", "verify", "-f", str(tmp_path / "allowed"), "-I", "x", "-n", NS,
                           "-s", str(tmp_path / "sign" / f"{RID}.sig")], input=b"I attest this.",
                          capture_output=True).returncode


def test_s1_a_late_challenge_is_answered_by_whichever_active_key_signs(rig) -> None:
    ch = rig.signer.challenge(RID, {"request_sha256": rig.sha()})
    done = rig.signer.answer(RID, rig.b.assertion(ch["challenge"]))
    assert done["signer_fp"] == rig.b.key.fingerprint
    rec = json.loads((rig.dir / "sign" / f"{RID}.signed.json").read_text())
    assert rec["signer_fp"] == rig.b.key.fingerprint and rec["public_key"] == rig.b.key.public_key
    assert _verify(rig.dir, rig.b.key) == 0
    assert _verify(rig.dir, rig.a.key) != 0           # and under no other key


def test_s2_a_key_that_is_not_enrolled_is_refused_and_nothing_is_signed(rig) -> None:
    ch = rig.signer.challenge(RID, {"request_sha256": rig.sha()})
    with pytest.raises(SignRefused, match="does not verify under any passkey"):
        rig.signer.answer(RID, rig.c.assertion(ch["challenge"]))
    assert not (rig.dir / "sign" / f"{RID}.signed.json").exists()
    assert not (rig.dir / "sign" / f"{RID}.sig").exists()


def test_s3_a_key_revoked_between_challenge_and_answer_is_not_a_candidate(rig) -> None:
    ch = rig.signer.challenge(RID, {"request_sha256": rig.sha()})
    del rig.active[rig.b.key.fingerprint]
    with pytest.raises(SignRefused, match="does not verify under any passkey"):
        rig.signer.answer(RID, rig.b.assertion(ch["challenge"]))
    assert not (rig.dir / "sign" / f"{RID}.signed.json").exists()


def test_s4_a_bound_challenge_answered_by_another_key_is_still_refused(rig) -> None:
    ch = rig.signer.challenge(RID, {"signer_fp": rig.a.key.fingerprint, "request_sha256": rig.sha()})
    # today's words for a bad signature, byte for byte
    with pytest.raises(SignRefused) as refused:
        rig.signer.answer(RID, rig.b.assertion(ch["challenge"]))
    assert str(refused.value) == "the passkey's signature does not verify over this message"
    assert not (rig.dir / "sign" / f"{RID}.signed.json").exists()
    assert rig.signer.answer(RID, rig.a.assertion(ch["challenge"]))["signer_fp"] == rig.a.key.fingerprint


def test_s5_the_challenge_bytes_name_no_key(rig) -> None:
    """The invariant a late signer rests on: a payload that began to carry the fp would turn this red."""
    late = rig.signer.challenge(RID, {"request_sha256": rig.sha()})
    for key in (rig.a.key, rig.b.key):
        bound = rig.signer.challenge(RID, {"signer_fp": key.fingerprint, "request_sha256": rig.sha()})
        assert bound == late


def test_s6_more_active_keys_than_the_cap_is_refused_in_words(rig, monkeypatch) -> None:
    ch = rig.signer.challenge(RID, {"request_sha256": rig.sha()})
    monkeypatch.setattr(signmod, "_LATE_KEYS_MAX", 1)
    with pytest.raises(SignRefused, match="the page must name the key"):
        rig.signer.answer(RID, rig.b.assertion(ch["challenge"]))
    assert not (rig.dir / "sign" / f"{RID}.signed.json").exists()
    # a bound answer is not capped
    rig.signer.challenge(RID, {"signer_fp": rig.b.key.fingerprint, "request_sha256": rig.sha()})
    assert rig.signer.answer(RID, rig.b.assertion(ch["challenge"]))["signer_fp"] == rig.b.key.fingerprint


def test_a_late_challenge_with_no_active_key_is_refused(rig) -> None:
    sha = rig.sha()
    rig.active.clear()
    with pytest.raises(SignRefused, match="no passkey is enrolled"):
        rig.signer.challenge(RID, {"request_sha256": sha})
    assert rig.signer._challenged == {}


def test_the_listing_facts_carry_the_enrol_date_and_number(rig) -> None:
    facts = rig.signer.key_facts([rig.a.key.fingerprint, rig.b.key.fingerprint])
    assert facts[rig.b.key.fingerprint]["seq"] == 2
    assert facts[rig.b.key.fingerprint]["enrolled_at"] == "2026-11-02T10:00:00Z"
    assert "credential_id" not in facts[rig.b.key.fingerprint]

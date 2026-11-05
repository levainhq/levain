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


# --- L3 picker r1 fixes (project_memory/1105-7/L3_picker_r1.txt) ---------------------------------------------------------


@pytest.mark.parametrize("fp", [None, 7, ["x"], {"a": 1}])
def test_s7_a_signer_fp_that_is_present_must_be_a_fingerprint(rig, fp) -> None:
    """An explicit null is not "left out": a page bug that sends null never turns a bound challenge into a late one."""
    with pytest.raises(SignRefused) as refused:
        rig.signer.challenge(RID, {"request_sha256": rig.sha(), "signer_fp": fp})
    assert refused.value.status == 400 and "signer_fp must be a passkey's fingerprint" in str(refused.value)
    assert rig.signer._challenged == {}


def test_s8_a_body_without_its_listed_version_is_never_challenged(rig) -> None:
    """request_sha256 is required: an empty body is malformed (400); a body naming only a key is a page from before the
    version was sent and keeps its 409 "reload" (test_phone_sign pins that; the L3 r1 ask to make it 400 is routed)."""
    for body, status in (({}, 400), ({"signer_fp": rig.a.key.fingerprint}, 409), ({"signer_fp": None}, 400)):
        with pytest.raises(SignRefused) as refused:
            rig.signer.challenge(RID, body)
        assert refused.value.status == status, (body, refused.value)
    assert rig.signer._challenged == {}


def test_s9_more_active_keys_than_the_cap_is_refused_at_the_challenge_before_any_tap(rig, monkeypatch) -> None:
    monkeypatch.setattr(signmod, "_LATE_KEYS_MAX", 1)
    with pytest.raises(SignRefused) as refused:
        rig.signer.challenge(RID, {"request_sha256": rig.sha()})
    assert "2 passkeys are enrolled" in str(refused.value) and "names the key" in str(refused.value), refused.value
    assert rig.signer._challenged == {}
    # a bound challenge is not capped
    ch = rig.signer.challenge(RID, {"signer_fp": rig.b.key.fingerprint, "request_sha256": rig.sha()})
    assert rig.signer.answer(RID, rig.b.assertion(ch["challenge"]))["signer_fp"] == rig.b.key.fingerprint


def _bad_key(seq: int, public_key: str) -> AuthorityKey:
    return AuthorityKey(fingerprint=f"SHA256:bad{seq}", public_key=public_key, key_class="display-phone",
                        enrolled_seq=seq, enrolled_at=f"2026-11-0{seq}T10:00:00Z")


def _other_site_key(seq: int) -> AuthorityKey:
    priv = ec.generate_private_key(ec.SECP256R1())
    spki = priv.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    line = public_key_line(spki, "example.com")
    return AuthorityKey(fingerprint=fingerprint_of(line), public_key=line, key_class="display-phone", enrolled_seq=seq,
                        enrolled_at=f"2026-11-0{seq}T10:00:00Z")


def test_s10_a_late_answer_moves_past_a_key_whose_own_line_refuses_it(rig) -> None:
    """complement MED 1: the newest active keys are a malformed line and a key made on another site; tried first, each
    refuses for a reason of its own, and the older well-formed key that signed still verifies."""
    bad = _bad_key(9, "sk-ecdsa-sha2-nistp256@openssh.com AAAA")
    other = _other_site_key(8)
    rig.active[bad.fingerprint] = bad
    rig.active[other.fingerprint] = other
    ch = rig.signer.challenge(RID, {"request_sha256": rig.sha()})
    assert rig.signer.answer(RID, rig.a.assertion(ch["challenge"]))["signer_fp"] == rig.a.key.fingerprint
    assert _verify(rig.dir, rig.a.key) == 0


def test_s11_a_late_answer_no_key_verifies_says_the_last_keys_own_problem(rig) -> None:
    bad = _bad_key(9, "ssh-ed25519 AAAA")
    rig.active.clear()
    rig.active[bad.fingerprint] = bad
    ch = rig.signer.challenge(RID, {"request_sha256": rig.sha()})
    with pytest.raises(SignRefused, match="the enrolled key is not a sk-ecdsa"):
        rig.signer.answer(RID, rig.a.assertion(ch["challenge"]))
    assert not (rig.dir / "sign" / f"{RID}.signed.json").exists()


def test_s12_a_problem_of_the_answer_itself_refuses_on_the_first_well_formed_key(rig, monkeypatch) -> None:
    """No user verification is the answer's, not a key's: it refuses at once in its own words, and no candidate is spawned
    for. The malformed key is the OLDEST, tried last, so an answer problem passed over as a key's would end in its words."""
    import levain.authority as authority

    spawned: list[str] = []
    real = authority.verify_link_sig
    monkeypatch.setattr(authority, "verify_link_sig", lambda *a, **k: spawned.append(k["fingerprint"]) or real(*a, **k))
    bad = _bad_key(0, "sk-ecdsa-sha2-nistp256@openssh.com AAAA")
    rig.active[bad.fingerprint] = bad
    ch = rig.signer.challenge(RID, {"request_sha256": rig.sha()})
    answer = rig.a.assertion(ch["challenge"])
    auth = bytearray(base64.urlsafe_b64decode(answer["authenticator_data"] + "=="))
    auth[32] = 0x01                                           # user present, not verified
    answer["authenticator_data"] = _b64u(bytes(auth))
    with pytest.raises(SignRefused, match="did not verify the user"):
        rig.signer.answer(RID, answer)
    assert spawned == []
    with pytest.raises(SignRefused, match="not base64url"):
        rig.signer.answer(RID, {**rig.a.assertion(ch["challenge"]), "signature": "!!"})


def test_s13_a_bound_answer_under_a_malformed_key_refuses_in_the_same_words(rig) -> None:
    """The late loop's skip is late only: a challenge bound to a key whose line is malformed refuses on that key, as
    before."""
    bad = _bad_key(9, "sk-ecdsa-sha2-nistp256@openssh.com AAAA")
    rig.active[bad.fingerprint] = bad
    ch = rig.signer.challenge(RID, {"signer_fp": bad.fingerprint, "request_sha256": rig.sha()})
    with pytest.raises(SignRefused) as refused:
        rig.signer.answer(RID, rig.a.assertion(ch["challenge"]))
    assert str(refused.value) == "the enrolled key is truncated"

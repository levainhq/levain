"""levain.cockpit.keyfacts (ONESURFACE OS-K, L-5): a passkey's credential id and provider are recorded beside its key,
only from a verified signature by its holder, and never widen what the key may sign."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

from levain.cockpit import keyfacts
from levain.cockpit.firejournal import CockpitDB
from levain.cockpit.keys import KeyError_, enroll, keys
from levain.consent.passkey import public_key_line
from tests.cockpit_authority_rig import RealKey, enrol as genesis_enrol

NOW = datetime(2026, 10, 25, 12, 0, tzinfo=timezone.utc)
RP = "cockpit.example.net"
GPM = "ea9b8d66-4d01-1d21-3ce4-b6b48cb575d4"
NS = "example-attest"
ORIGIN = f"https://{RP}"


def _b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


class Passkey:
    """A P-256 credential as a browser makes one: its OpenSSH line, its id, and the creation's authenticatorData."""

    def __init__(self, rp: str = RP, aaguid: str = GPM) -> None:
        self.rp = rp
        self.priv = ec.generate_private_key(ec.SECP256R1())
        pub = self.priv.public_key()
        spki = pub.public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
        self.spki = spki
        self.line = public_key_line(spki, rp)
        n = pub.public_numbers()
        x, y = n.x.to_bytes(32, "big"), n.y.to_bytes(32, "big")
        self.cred = os.urandom(32)
        cose = bytes.fromhex("a501020326200121" + "5820") + x + b"\x22\x58\x20" + y
        self.creation = (hashlib.sha256(rp.encode()).digest() + bytes([0x45]) + (0).to_bytes(4, "big")
                         + bytes.fromhex(aaguid.replace("-", "")) + len(self.cred).to_bytes(2, "big") + self.cred + cose)

    def assertion(self, challenge: str, origin: str = ORIGIN) -> dict:
        auth = hashlib.sha256(self.rp.encode()).digest() + bytes([0x05]) + (0).to_bytes(4, "big")
        cdj = json.dumps({"type": "webauthn.get", "challenge": challenge, "origin": origin, "crossOrigin": False},
                         separators=(",", ":")).encode()
        der = self.priv.sign(auth + hashlib.sha256(cdj).digest(), ec.ECDSA(hashes.SHA256()))
        return {"authenticator_data": _b64u(auth), "client_data_json": _b64u(cdj), "signature": _b64u(der)}


def _cockpit(tmp: Path) -> tuple[Path, RealKey]:
    root = tmp / "cockpit"
    root.mkdir()
    CockpitDB(root).set_install_class("reported")
    laptop = RealKey(tmp, "laptop")
    genesis_enrol(CockpitDB(root), laptop, "laptop-presence", now=NOW)
    return root, laptop


def _by(k: RealKey, tmp: Path) -> dict:
    return {"signer_fp": k.fingerprint, "sign": k.sign_link, "clock": lambda: NOW, "marker_directory": tmp / "m"}


# --- the attested creation ---------------------------------------------------------------------------------------

def test_a_creation_names_its_credential_id_and_provider() -> None:
    pk = Passkey()
    got = keyfacts.attested(pk.creation, pk.line)
    assert got.credential_id == pk.cred and got.aaguid == GPM
    assert keyfacts.provider_name(got.aaguid) == "Google Password Manager"
    assert keyfacts.provider_name("00000000-0000-0000-0000-000000000000") is None
    assert "unlisted" in keyfacts.provider_name("12345678-1234-1234-1234-123456789abc")


def test_a_creation_that_does_not_attest_this_key_is_refused() -> None:
    pk, other = Passkey(), Passkey()
    with pytest.raises(keyfacts.KeyFactsError, match="not the key being enrolled"):
        keyfacts.attested(other.creation, pk.line)
    with pytest.raises(keyfacts.KeyFactsError, match="another name"):
        keyfacts.attested(Passkey(rp="other.example.net").creation, pk.line)
    no_at = bytearray(pk.creation)
    no_at[32] = 0x05
    with pytest.raises(keyfacts.KeyFactsError, match="no attested credential"):
        keyfacts.attested(bytes(no_at), pk.line)
    with pytest.raises(keyfacts.KeyFactsError, match="too short"):
        keyfacts.attested(pk.creation[:40], pk.line)
    with pytest.raises(keyfacts.KeyFactsError, match="credential id"):
        keyfacts.attested(pk.creation[:60], pk.line)
    with pytest.raises(keyfacts.KeyFactsError, match="only a passkey"):
        keyfacts.attested(pk.creation, "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIH")


# --- enrolment ----------------------------------------------------------------------------------------------------

def test_enrolment_records_the_attested_facts_beside_the_key(tmp_path: Path) -> None:
    root, laptop = _cockpit(tmp_path)
    pk = Passkey()
    row, created, _ = enroll(root, pk.line, "display-phone", custody="passkey", attested=pk.creation,
                             **_by(laptop, tmp_path))
    assert created
    facts = keyfacts.read(root)[row.fingerprint]
    assert facts.credential_id == _b64u(pk.cred) and facts.aaguid == GPM and facts.source == "enrol"
    assert keyfacts.provider_text(facts) == "Google Password Manager"


def test_a_mismatched_creation_is_refused_before_anything_is_signed(tmp_path: Path) -> None:
    root, laptop = _cockpit(tmp_path)
    pk = Passkey()
    before = keys(root, now=NOW, marker_directory=tmp_path / "m")[0]
    with pytest.raises(KeyError_, match="not the key being enrolled"):
        enroll(root, pk.line, "display-phone", custody="passkey", attested=Passkey().creation, **_by(laptop, tmp_path))
    assert keys(root, now=NOW, marker_directory=tmp_path / "m")[0] == before
    assert keyfacts.read(root) == {}
    laptop_line = (tmp_path / "laptop.pub").read_text()
    with pytest.raises(KeyError_, match="only a passkey"):
        enroll(root, laptop_line, "laptop-presence", custody="passphrase", attested=pk.creation,
               **_by(laptop, tmp_path))


def test_an_already_enrolled_key_gets_no_facts_from_an_enrol_that_signed_nothing(tmp_path: Path) -> None:
    root, laptop = _cockpit(tmp_path)
    pk = Passkey()
    enroll(root, pk.line, "display-phone", custody="passkey", **_by(laptop, tmp_path))
    _, created, _ = enroll(root, pk.line, "display-phone", custody="passkey", attested=pk.creation,
                           **_by(laptop, tmp_path))
    assert not created and keyfacts.read(root) == {}


def test_the_cli_enrols_with_attested_facts_and_keys_shows_provider(tmp_path: Path, capsys, monkeypatch) -> None:
    from levain import cli
    from levain.cockpit import install

    monkeypatch.setattr(install, "marker_dir", lambda: tmp_path / "m")
    root, laptop = _cockpit(tmp_path)
    pk, old = Passkey(), Passkey()
    (tmp_path / "pk.pub").write_text(pk.line + "\n")
    (tmp_path / "old.pub").write_text(old.line + "\n")
    run = lambda *a: cli.main(["cockpit", *a, "--data-root", str(root)])   # noqa: E731
    assert run("enroll", str(tmp_path / "pk.pub"), "--class", "display-phone", "--custody", "passkey",
               "--signing-key", str(laptop.path), "--attested", "!!") == 1
    assert "authenticator_data is not base64url" in capsys.readouterr().err
    assert run("enroll", str(tmp_path / "pk.pub"), "--class", "display-phone", "--custody", "passkey",
               "--signing-key", str(laptop.path), "--attested", _b64u(pk.creation)) == 0
    assert run("enroll", str(tmp_path / "old.pub"), "--class", "display-phone", "--custody", "passkey",
               "--signing-key", str(laptop.path)) == 0
    capsys.readouterr()
    assert run("keys") == 0
    out = capsys.readouterr().out.splitlines()
    [new_line] = [l for l in out if "Google Password Manager" in l]
    assert "credential id recorded" in new_line and "display-phone" in new_line
    [old_line] = [l for l in out if l.startswith(keys(root, now=NOW)[0][2].fingerprint)]
    assert "provider not recorded" in old_line and "no credential id" in old_line
    laptop_row = [l for l in out if "laptop-presence" in l][0]
    assert "provider" not in laptop_row         # not a passkey: nothing to say about a provider


# --- learning from a signature ------------------------------------------------------------------------------------

def test_learn_writes_only_when_no_id_is_held(tmp_path: Path) -> None:
    a, b = os.urandom(32), os.urandom(32)
    assert keyfacts.learn(tmp_path, "SHA256:k", a, now="t1")
    assert not keyfacts.learn(tmp_path, "SHA256:k", b, now="t2")
    assert keyfacts.read(tmp_path)["SHA256:k"].credential_id == _b64u(a)
    assert not keyfacts.learn(tmp_path, "SHA256:j", b"short", now="t3")
    assert "SHA256:j" not in keyfacts.read(tmp_path)


def _file(qdir: Path, rid: str) -> None:
    sign = qdir / "sign"
    sign.mkdir(exist_ok=True)
    body = {"format": "levain-sign-request/1", "id": rid, "namespace": NS, "message": "I attest this.",
            "title": "report", "requester": "test",
            "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()}
    (sign / f"{rid}.json").write_text(json.dumps(body))


def _signer(tmp: Path, pk: Passkey, facts_root: Path):
    from levain.authority import AuthorityKey
    from levain.cockpit.keys import fingerprint_of
    from levain.consent.sign import Signer

    key = AuthorityKey(fingerprint=fingerprint_of(pk.line), public_key=pk.line, key_class="display-phone",
                       enrolled_seq=2)
    signer = Signer(tmp / "q", (NS,), RP, lambda: {key.fingerprint: key}, facts_root=lambda: facts_root)
    signer.page_origin = ORIGIN

    def answer(rid: str, extra: dict, *, assertion=None) -> dict:
        _file(tmp / "q", rid)
        row = next(r for r in signer.listing([key.fingerprint]) if r["id"] == rid)
        c = signer.challenge(rid, {"signer_fp": key.fingerprint, "request_sha256": row["sha256"]})["challenge"]
        return signer.answer(rid, {**(assertion or pk.assertion)(c), **extra})

    return signer, key, answer


def test_a_verified_signature_records_its_credential_id_once(tmp_path: Path) -> None:
    from levain.consent.sign import SignRefused

    (tmp_path / "q").mkdir()
    facts = tmp_path / "facts"
    facts.mkdir()
    pk = Passkey()
    signer, key, answer = _signer(tmp_path, pk, facts)
    # an assertion that does not verify (another key's signature) records nothing, with or without an id
    with pytest.raises(SignRefused):
        answer("bad-0001", {"raw_id": _b64u(pk.cred)}, assertion=Passkey().assertion)
    assert keyfacts.read(facts) == {}
    # an answer without raw_id still signs, as before
    assert answer("old-0001", {})["signer_fp"] == key.fingerprint
    assert keyfacts.read(facts) == {}
    # a malformed id never costs the signature: it is signed, and nothing is recorded
    assert answer("odd-0001", {"raw_id": "!!"})["signer_fp"] == key.fingerprint
    assert keyfacts.read(facts) == {}
    assert answer("new-0001", {"raw_id": _b64u(pk.cred)})["signer_fp"] == key.fingerprint
    held = keyfacts.read(facts)[key.fingerprint]
    assert held.credential_id == _b64u(pk.cred) and held.source == "signature"
    # a later different id from the same key is kept out
    answer("new-0002", {"raw_id": _b64u(os.urandom(32))})
    assert keyfacts.read(facts)[key.fingerprint].credential_id == _b64u(pk.cred)
    assert signer.key_facts([key.fingerprint]) == {
        key.fingerprint: {"credential_id": _b64u(pk.cred),
                          "provider": "provider not recorded (only an enrolment that sends its creation names it)"}}
    # an unknown request key is still refused
    with pytest.raises(SignRefused, match="request keys"):
        answer("new-0003", {"raw_id": _b64u(pk.cred), "credential": "x"})


def test_a_facts_store_that_cannot_be_written_never_fails_a_signature(tmp_path: Path) -> None:
    (tmp_path / "q").mkdir()
    pk = Passkey()
    _, key, answer = _signer(tmp_path, pk, tmp_path / "missing" / "dir")
    assert answer("fine-0001", {"raw_id": _b64u(pk.cred)})["signer_fp"] == key.fingerprint


# --- the authority boundary ---------------------------------------------------------------------------------------

def _authority(root: Path):
    from levain.authority import anchor_path
    from levain.consent.queue import authority_source

    return authority_source(root / "cockpit.db", anchor=anchor_path(root))()


def test_a_recorded_or_forged_credential_id_never_widens_what_a_key_signs(tmp_path: Path) -> None:
    """The HARD RULE: what a key may sign is its class's rank in the replayed log. A display-phone passkey with its
    own id recorded, or with another key's id forged into the store, signs T2 and never T3, exactly as before."""
    from levain.cockpit.firejournal import _key_problem
    from levain.consent.answering import passkey_keys

    root, laptop = _cockpit(tmp_path)
    phone, other = Passkey(), Passkey()
    row, _, _ = enroll(root, phone.line, "display-phone", custody="passkey", **_by(laptop, tmp_path))

    def judged() -> tuple:
        snap = _authority(root)
        conn = sqlite3.connect(root / "cockpit.db")
        try:
            return (tuple(sorted(snap.keys)), snap.key(row.fingerprint).key_class,
                    [k.fingerprint for k in passkey_keys(snap, RP)],
                    _key_problem(conn, snap, row.fingerprint, "T2")[0], _key_problem(conn, snap, row.fingerprint, "T3")[0])
        finally:
            conn.close()

    before = judged()
    assert before[3] is None and before[4] == "key class display-phone does not cover T3"
    keyfacts.learn(root, row.fingerprint, phone.cred, now="t")                   # its own id, recorded
    assert judged() == before
    # forged: the laptop key's fingerprint given the phone's credential id, and an enrol-sourced row naming a provider
    conn = sqlite3.connect(root / keyfacts.DB_NAME)
    with conn:
        conn.execute("INSERT INTO key_facts VALUES (?, ?, ?, 'enrol', 't')", (laptop.fingerprint, _b64u(phone.cred), GPM))
        conn.execute("UPDATE key_facts SET credential_id = ? WHERE fingerprint = ?", (_b64u(other.cred), row.fingerprint))
    conn.close()
    assert judged() == before
    snap = _authority(root)
    assert snap.key(laptop.fingerprint).key_class == "laptop-presence"
    assert [k.fingerprint for k in passkey_keys(snap, RP)] == [row.fingerprint]    # the laptop key never becomes one


def test_nothing_that_decides_authority_reads_the_facts_store() -> None:
    """Structural: the replay, the rank check, the consent hook and the sign verifier import without keyfacts."""
    probe = ("import sys, levain.authority, levain.cockpit.firejournal, levain.consent.answering, "
             "levain.consent.queue, levain.cockpit.keys, levain.consent.passkey; "
             "print('levain.cockpit.keyfacts' in sys.modules)")
    out = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, check=True,
                         env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1])})
    assert out.stdout.strip() == "False", out.stderr


# --- the phone page's key-line --------------------------------------------------------------------------------------

def test_the_key_line_puts_a_checked_creation_on_the_enrol_command_and_records_nothing(tmp_path: Path, capsys,
                                                                                    monkeypatch) -> None:
    import shlex

    from levain import cli
    from levain.cockpit import install
    from levain.phone_server import PhoneError, make_phone_server

    monkeypatch.setattr(install, "marker_dir", lambda: tmp_path / "m")
    root, laptop = _cockpit(tmp_path)
    q = tmp_path / "q"
    q.mkdir()
    (q / "claude_code.json").write_text(json.dumps({"cockpit_data_root": str(root)}))
    pk = Passkey()
    srv = make_phone_server(q, RP, port=0)
    try:
        plain = srv.key_line({"spki": _b64u(pk.spki)})
        assert all("--attested" not in c for c in plain["commands"])
        with pytest.raises(PhoneError) as bad:
            srv.key_line({"spki": _b64u(pk.spki), "authenticator_data": _b64u(Passkey().creation)})
        assert bad.value.status == 422 and "not the key being enrolled" in str(bad.value)
        with pytest.raises(PhoneError) as extra:
            srv.key_line({"spki": _b64u(pk.spki), "raw_id": _b64u(pk.cred)})
        assert extra.value.status == 400
        got = srv.key_line({"spki": _b64u(pk.spki), "authenticator_data": _b64u(pk.creation) + "=="})
    finally:
        srv.server_close()
    assert keyfacts.read(root) == {}                                     # the page's call records nothing
    enrol = [c for c in got["commands"] if c.startswith("levain cockpit enroll")]
    assert enrol, got
    argv = shlex.split(enrol[0])
    assert argv[argv.index("--attested") + 1] == _b64u(pk.creation)
    (tmp_path / got["file"]).write_text(got["public_key"] + "\n")
    argv[argv.index(got["file"])] = str(tmp_path / got["file"])
    argv[argv.index("YOUR_LAPTOP_PRESENCE_KEY")] = str(laptop.path)
    assert cli.main(argv[1:]) == 0, capsys.readouterr().err
    assert keyfacts.read(root)[got["fingerprint"]].credential_id == _b64u(pk.cred)


# --- the cockpit's sign listing ---------------------------------------------------------------------------------------

def test_a_fresh_browser_gets_the_credential_id_from_the_cockpit_after_one_signature(tmp_path: Path) -> None:
    """Over http: the listing names no credential before any signature; an answer carrying ``raw_id`` that verifies
    records it; the next listing (what a fresh browser reads) carries it for that signer."""
    from levain.authority import AuthorityKey
    from levain.cockpit.keys import fingerprint_of
    from levain.consent.sign import Signer
    from tests.test_cockpit_sign_routes import _call, _serving

    rp = "box.example"
    origin = f"https://{rp}:8443"
    pk = Passkey(rp=rp)
    key = AuthorityKey(fingerprint=fingerprint_of(pk.line), public_key=pk.line, key_class="display-phone",
                       enrolled_seq=2)
    facts = tmp_path / "facts"
    facts.mkdir()
    q = tmp_path / "q"
    q.mkdir()
    signer = Signer(q, (NS,), rp, lambda: {key.fingerprint: key}, facts_root=lambda: facts)
    signer.page_origin = origin
    _file(q, "http-0001")
    with _serving(tmp_path / "inst", signer=signer, extra_hosts=[rp]) as (url, _httpd):
        status, body = _call(url, "/cockpit/sign.json")
        assert status == 200 and body["credentials"] == {}
        sha = next(r["sha256"] for r in body["requests"] if r["id"] == "http-0001")
        status, c = _call(url, "/cockpit/sign/http-0001/challenge", {"signer_fp": key.fingerprint, "request_sha256": sha})
        assert status == 200, c
        status, a = _call(url, "/cockpit/sign/http-0001/answer",
                          {**pk.assertion(c["challenge"], origin), "raw_id": _b64u(pk.cred)})
        assert status == 200, a
        status, body = _call(url, "/cockpit/sign.json")
        assert body["credentials"] == {key.fingerprint: {
            "credential_id": _b64u(pk.cred),
            "provider": "provider not recorded (only an enrolment that sends its creation names it)"}}
        _file(q, "http-0002")
        status, one = _call(url, "/cockpit/sign/http-0002.json")
        assert status == 200 and one["credentials"][key.fingerprint]["credential_id"] == _b64u(pk.cred)

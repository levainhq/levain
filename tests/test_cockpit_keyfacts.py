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
    assert keyfacts.provider_name("12345678-1234-1234-1234-123456789abc") is None      # never a guess


@pytest.mark.parametrize("aaguid, shown", [
    ("fbfc3007-154e-4ecc-8c0b-6e020557d7bd", "iCloud Keychain (Apple Passwords)"),   # his, named "Passwords" on the Mac
    ("dd4ec289-e01d-41c9-bb89-70fa845d4bf2", "iCloud Keychain (Managed)"),
    ("00000000-0000-0000-0000-000000000000", "provider not named (the passkey's AAGUID is all zeros)"),
    ("12345678-1234-1234-1234-123456789abc", "unknown provider (AAGUID 12345678-1234-1234-1234-123456789abc)"),
    (None, "provider not named (no AAGUID recorded)"),
])
def test_the_provider_line_names_only_what_the_aaguid_names(aaguid, shown) -> None:
    """Head fact 10-25: his passkey is one synced credential in iCloud Keychain (his Mac named it "Passwords"). The
    view names a listed AAGUID's provider, says so plainly for an all-zero one, and never guesses at an unlisted one."""
    facts = keyfacts.KeyFacts("SHA256:k", "aWQ", aaguid, "enrol", "t")
    assert keyfacts.provider_text(facts) == shown


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


def _cose(x: bytes, y: bytes, *, kty: int = 2, alg: int = -7, crv: int = 1) -> bytes:
    def item(v: int) -> bytes:
        return bytes([v]) if v >= 0 else bytes([0x20 | (-1 - v)])
    return (b"\xa5" + item(1) + item(kty) + item(3) + item(alg) + item(-1) + item(crv)
            + item(-2) + b"\x58\x20" + x + item(-3) + b"\x58\x20" + y)


def _creation(pk: "Passkey", cose: bytes, *, flags: int = 0x45, tail: bytes = b"") -> bytes:
    return (pk.creation[:32] + bytes([flags]) + pk.creation[33:55 + len(pk.cred)] + cose + tail)


def _xy(pk: "Passkey") -> tuple[bytes, bytes]:
    n = pk.priv.public_key().public_numbers()
    return n.x.to_bytes(32, "big"), n.y.to_bytes(32, "big")


def test_the_attested_key_is_parsed_never_searched_for() -> None:
    """K1 (L3 r1 codex): the credential public key is decoded as one COSE EC2 map at its offset and compared to the
    enrolled point; the bytes after it (extensions, or anything when ED is clear) are never searched."""
    a, b = Passkey(), Passkey()
    ax, ay = _xy(a)
    bx, by = _xy(b)
    a_items = b"\x21\x58\x20" + ax + b"\x22\x58\x20" + ay
    # codex's construction: B's COSE key, then trailing bytes carrying A's x and y, offered as A's creation
    forged = _creation(a, _cose(bx, by), tail=a_items)
    with pytest.raises(keyfacts.KeyFactsError):
        keyfacts.attested(forged, a.line)
    # the same with ED set, A's x and y riding in the extensions
    with pytest.raises(keyfacts.KeyFactsError, match="not the key being enrolled"):
        keyfacts.attested(_creation(a, _cose(bx, by), flags=0xC5, tail=b"\xa1\x41\x00" + a_items), a.line)
    # A's own key with stray bytes after it and ED clear: refused (nothing may follow an un-extended key)
    with pytest.raises(keyfacts.KeyFactsError, match="trailing"):
        keyfacts.attested(_creation(a, _cose(ax, ay), tail=b"\x00"), a.line)
    # A's own key with ED set and an extensions map after it: accepted, the extensions unread
    got = keyfacts.attested(_creation(a, _cose(ax, ay), flags=0xC5, tail=b"\xa1\x41\x00\x41\x00"), a.line)
    assert got.credential_id == a.cred
    # A's x and y under a COSE key that is not ES256 on P-256: refused
    for bad in ({"kty": 3}, {"alg": -8}, {"crv": 2}):
        with pytest.raises(keyfacts.KeyFactsError, match="not an ES256 P-256 key"):
            keyfacts.attested(_creation(a, _cose(ax, ay, **bad)), a.line)
    # a map that is not CBOR this decoder reads (a 2-byte head where a map is due; a truncated key): refused
    with pytest.raises(keyfacts.KeyFactsError):
        keyfacts.attested(_creation(a, b"\x82\x01\x02"), a.line)
    with pytest.raises(keyfacts.KeyFactsError):
        keyfacts.attested(_creation(a, _cose(ax, ay)[:-5]), a.line)
    # a duplicated label is refused, never last-wins
    dup = b"\xa6" + _cose(bx, by)[1:] + b"\x21\x58\x20" + ax
    with pytest.raises(keyfacts.KeyFactsError):
        keyfacts.attested(_creation(a, dup), a.line)
    # the labels in another order still decode (a parse, not a fixed header)
    reordered = (b"\xa5" + b"\x22\x58\x20" + ay + b"\x21\x58\x20" + ax + b"\x20\x01\x03\x26\x01\x02")
    assert keyfacts.attested(_creation(a, reordered), a.line).credential_id == a.cred


def test_a_relative_data_root_reads_its_facts(tmp_path: Path, monkeypatch) -> None:
    """K2 (L3 r1 deepseek + codex): ``read`` builds its read-only URI from the absolute path."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "rel").mkdir()
    cred = os.urandom(32)
    assert keyfacts.learn(Path("rel"), "SHA256:k", cred, now="t")
    assert keyfacts.read(Path("rel"))["SHA256:k"].credential_id == _b64u(cred)


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


def test_a_rerun_of_an_enrolled_keys_enrol_repairs_missing_facts_and_never_overwrites(tmp_path: Path) -> None:
    """K5 (L3 r1 codex): the facts are written after the enrol link commits, so a crash between the two is
    recovered by rerunning the same command: it signs nothing, fills a missing row, and keeps a different id held."""
    root, laptop = _cockpit(tmp_path)
    pk = Passkey()
    row, _, _ = enroll(root, pk.line, "display-phone", custody="passkey", **_by(laptop, tmp_path))
    seq = keys(root, now=NOW, marker_directory=tmp_path / "m")
    _, created, warning = enroll(root, pk.line, "display-phone", custody="passkey", attested=pk.creation,
                                 **_by(laptop, tmp_path))
    assert not created and warning is None
    assert keys(root, now=NOW, marker_directory=tmp_path / "m") == seq                 # nothing was signed
    held = keyfacts.read(root)[row.fingerprint]
    assert held.credential_id == _b64u(pk.cred) and held.aaguid == GPM and held.source == "enrol"
    # the same rerun again is a no-op
    assert enroll(root, pk.line, "display-phone", custody="passkey", attested=pk.creation,
                  **_by(laptop, tmp_path))[1:] == (False, None)
    assert keyfacts.read(root)[row.fingerprint] == held


def test_a_rerun_fills_a_signature_learned_ids_provider_and_keeps_out_a_different_id(tmp_path: Path) -> None:
    root, laptop = _cockpit(tmp_path)
    pk = Passkey()
    row, _, _ = enroll(root, pk.line, "display-phone", custody="passkey", **_by(laptop, tmp_path))
    assert keyfacts.learn(root, row.fingerprint, pk.cred, now="t")
    enroll(root, pk.line, "display-phone", custody="passkey", attested=pk.creation, **_by(laptop, tmp_path))
    held = keyfacts.read(root)[row.fingerprint]
    assert (held.credential_id, held.aaguid, held.source) == (_b64u(pk.cred), GPM, "enrol")
    # a creation naming another credential id for this key: the held id stays, and the operator is told
    other = bytearray(pk.creation)
    other[55:87] = os.urandom(32)
    _, created, warning = enroll(root, pk.line, "display-phone", custody="passkey", attested=bytes(other),
                                 **_by(laptop, tmp_path))
    assert not created and "another credential id" in warning
    assert keyfacts.read(root)[row.fingerprint] == held


def test_an_id_learned_while_a_repair_runs_is_never_overwritten(tmp_path: Path, monkeypatch) -> None:
    """verify r1 (codex LOW): a signature that learns another id for the key while ``repair_enrolment`` is between
    its read and its write either waits out the repair or is held, and is never replaced by the repair's id."""
    pk, racer = Passkey(), os.urandom(32)
    facts = keyfacts.attested(pk.creation, pk.line)
    real_connect = keyfacts._connect
    raced: list = []

    def connect(root: Path) -> sqlite3.Connection:
        conn = real_connect(root)

        def trace(sql: str) -> None:
            # the moment between the repair's read and its write: another process learns a different id
            if sql.startswith("INSERT OR REPLACE") and not raced:
                other = sqlite3.connect(str(Path(root) / keyfacts.DB_NAME), timeout=0)
                try:
                    other.execute("INSERT OR IGNORE INTO key_facts VALUES (?, ?, NULL, 'signature', 't0')",
                                  ("SHA256:k", _b64u(racer)))
                    other.commit()
                    raced.append("learned")
                except sqlite3.OperationalError as exc:
                    raced.append(str(exc))
                finally:
                    other.close()

        conn.set_trace_callback(trace)
        return conn

    real_connect(tmp_path).close()                      # the table exists, empty
    monkeypatch.setattr(keyfacts, "_connect", connect)
    try:
        wrote = keyfacts.repair_enrolment(tmp_path, "SHA256:k", facts, now="t1")
    except keyfacts.KeyFactsError:
        wrote = False
    held = keyfacts.read(tmp_path)["SHA256:k"].credential_id
    assert raced, "the race was never staged"
    if raced == ["learned"]:
        assert held == _b64u(racer) and not wrote, "a held id was overwritten by the repair"
    else:
        assert "locked" in raced[0] and held == _b64u(pk.cred) and wrote


def test_an_enrol_whose_facts_cannot_be_written_still_enrols_with_a_warning(tmp_path: Path, monkeypatch) -> None:
    """K4 (L3 r1 deepseek): the sqlite3.Error the enrol catches is a warning, never an enrol failure."""
    root, laptop = _cockpit(tmp_path)
    pk = Passkey()

    def broken(*_a, **_k):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(keyfacts, "record_enrolment", broken)
    row, created, warning = enroll(root, pk.line, "display-phone", custody="passkey", attested=pk.creation,
                                   **_by(laptop, tmp_path))
    assert created and "were not recorded: database is locked" in warning
    assert row.fingerprint in {r.fingerprint for r in keys(root, now=NOW, marker_directory=tmp_path / "m")[0]}
    monkeypatch.undo()
    # and the rerun repairs it (K5)
    enroll(root, pk.line, "display-phone", custody="passkey", attested=pk.creation, **_by(laptop, tmp_path))
    assert keyfacts.read(root)[row.fingerprint].credential_id == _b64u(pk.cred)


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


def test_a_raw_id_too_long_to_be_a_credential_id_is_refused_before_it_is_decoded(monkeypatch) -> None:
    """verify r1 (deepseek LOW 3): the text is bounded by the longest credential id's base64url before decoding."""
    from levain.consent import passkey

    longest = _b64u(b"\x01" * keyfacts._CRED_MAX)
    assert keyfacts.raw_id_bytes(longest) == b"\x01" * keyfacts._CRED_MAX
    decoded: list = []
    real = passkey.b64url_decode
    monkeypatch.setattr(passkey, "b64url_decode", lambda t, w: decoded.append(t) or real(t, w))
    for text in (longest + "AAAA", "A" * 100_000):
        with pytest.raises(keyfacts.KeyFactsError, match="credential id"):
            keyfacts.raw_id_bytes(text)
    assert decoded == []


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


def test_nothing_learning_raises_ever_fails_a_verified_answer(tmp_path: Path, monkeypatch, caplog) -> None:
    """K3 (L3 r1 deepseek): an answer whose signature verified stands whatever learning raises; it is logged."""
    import logging

    (tmp_path / "q").mkdir()
    pk = Passkey()
    signer, key, answer = _signer(tmp_path, pk, tmp_path / "facts")

    def root_fails() -> Path:
        raise RuntimeError("no data root here")

    signer._facts_root = root_fails
    with caplog.at_level(logging.WARNING, logger="levain.consent.sign"):
        assert answer("odd-0001", {"raw_id": _b64u(pk.cred)})["signer_fp"] == key.fingerprint
    assert "no data root here" in caplog.text
    assert (tmp_path / "q" / "sign" / "odd-0001.signed.json").exists()

    def learn_fails(*_a, **_k):
        raise ValueError("unexpected")

    signer._facts_root = lambda: tmp_path
    monkeypatch.setattr(keyfacts, "learn", learn_fails)
    assert answer("odd-0002", {"raw_id": _b64u(pk.cred)})["signer_fp"] == key.fingerprint

    # verify r1 (LOW 2): only keyfacts' own refusal is silent, recognised by class, never by a class's name
    class KeyFactsError(Exception):
        pass

    def learn_foreign(*_a, **_k):
        raise KeyFactsError("from elsewhere")

    monkeypatch.setattr(keyfacts, "learn", learn_foreign)
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="levain.consent.sign"):
        assert answer("odd-0003", {"raw_id": _b64u(pk.cred)})["signer_fp"] == key.fingerprint
    assert "from elsewhere" in caplog.text
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="levain.consent.sign"):
        assert answer("odd-0004", {"raw_id": "!!"})["signer_fp"] == key.fingerprint
    assert caplog.text == ""                            # keyfacts' own refusal of the id: dropped silently


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

# dash72.example: its rpIdHash starts 0xFB, so the creation's base64url starts with "-" (verify r1 N1)


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


def test_facts_that_cannot_be_read_for_any_reason_never_fail_the_sign_listing(tmp_path: Path, caplog) -> None:
    """verify r1 (deepseek MED 1): the listing's facts are display only, so whatever reading them raises, the listing
    is served with its rows and no credentials, and the failure is logged."""
    import logging

    from levain.authority import AuthorityKey
    from levain.cockpit.keys import fingerprint_of
    from levain.consent.sign import Signer
    from tests.test_cockpit_sign_routes import _call, _serving

    rp = "box.example"
    pk = Passkey(rp=rp)
    key = AuthorityKey(fingerprint=fingerprint_of(pk.line), public_key=pk.line, key_class="display-phone",
                       enrolled_seq=2)

    def root_fails() -> Path:
        raise RuntimeError("no data root here")

    q = tmp_path / "q"
    q.mkdir()
    signer = Signer(q, (NS,), rp, lambda: {key.fingerprint: key}, facts_root=root_fails)
    signer.page_origin = f"https://{rp}:8443"
    _file(q, "list-0001")
    with caplog.at_level(logging.WARNING, logger="levain.consent.sign"):
        with _serving(tmp_path / "inst", signer=signer, extra_hosts=[rp]) as (url, _httpd):
            status, body = _call(url, "/cockpit/sign.json")
            assert status == 200, body
            assert [r["id"] for r in body["requests"]] == ["list-0001"] and body["credentials"] == {}
            status, one = _call(url, "/cockpit/sign/list-0001.json")
            assert status == 200 and one["credentials"] == {}
    assert "no data root here" in caplog.text

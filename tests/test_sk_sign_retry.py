"""levain#125 (1118-22): the Touch ID terminal sign twice returned a signature that does not verify (flags 0x21, a byte
the authenticator never signed; check-novalidate failed too, so the bytes were not even self-consistent). The Secure
Enclave cannot be driven here, so these build the failure from a SOFTWARE sk-ecdsa key: a real SSHSIG (OpenSSH
PROTOCOL.sshsig over PROTOCOL.u2f) made in Python, whose flags byte is then flipped the way the real failure was. ssh-keygen
itself judges it, so the "error in libcrypto" is the real one. Nothing here touches a real key, the Secure Enclave or
~/.levain: the key and its signatures live in tmp_path.

Held: a corrupted first answer is retried once (a new sign over the same message) and the second, good one is returned; every
failed attempt leaves one 0600 evidence file with no message bytes; two failures raise with both flags bytes; the evidence
directory never holds more than the bound; a non-sk key is not retried."""
from __future__ import annotations

import base64
import hashlib
import json
import stat
import struct
import subprocess
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

import levain.cockpit.keys as keys
from levain.authority_sig import _sshsig_type_and_flags
from levain.cockpit.keys import KeyError_, ssh_signer

SK = "sk-ecdsa-sha2-nistp256@openssh.com"
NS = "levain-authority@levain"


def _s(b: bytes) -> bytes:
    return struct.pack(">I", len(b)) + b


def _mpint(n: int) -> bytes:
    raw = n.to_bytes((n.bit_length() + 8) // 8, "big")
    return _s(raw)


class SoftSk:
    """A software sk-ecdsa key: signs like a FIDO authenticator would (sha256(app) | flags | counter | sha256(data))."""

    def __init__(self, dir_: Path) -> None:
        from cryptography.hazmat.primitives import serialization
        self.priv = ec.generate_private_key(ec.SECP256R1())
        point = self.priv.public_key().public_bytes(serialization.Encoding.X962,
                                                     serialization.PublicFormat.UncompressedPoint)
        self.blob = _s(SK.encode()) + _s(b"nistp256") + _s(point) + _s(b"ssh:")
        dir_.mkdir(mode=0o700, exist_ok=True)
        self.path = dir_ / "id_sk"
        self.path.write_text("not a real private key; the fake ssh-keygen sign never reads it\n")
        self.path.chmod(0o600)
        Path(str(self.path) + ".pub").write_text(f"{SK} {base64.b64encode(self.blob).decode()} test\n")

    def sign(self, message: bytes, namespace: str, *, flags: int = 0x01, counter: int = 7, sig_flags: int | None = None) -> bytes:
        """The armored SSHSIG over ``message``; ``sig_flags`` is the byte WRITTEN into the blob when it differs from the one
        the authenticator signed (the real failure's shape)."""
        data = b"SSHSIG" + _s(namespace.encode()) + _s(b"") + _s(b"sha512") + _s(hashlib.sha512(message).digest())
        signed = hashlib.sha256(b"ssh:").digest() + bytes([flags]) + struct.pack(">I", counter) + hashlib.sha256(data).digest()
        r, s = decode_dss_signature(self.priv.sign(signed, ec.ECDSA(hashes.SHA256())))
        inner = _s(SK.encode()) + _s(_mpint(r) + _mpint(s)) + bytes([flags if sig_flags is None else sig_flags]) \
            + struct.pack(">I", counter)
        blob = b"SSHSIG" + struct.pack(">I", 1) + _s(self.blob) + _s(namespace.encode()) + _s(b"") + _s(b"sha512") + _s(inner)
        b64 = base64.b64encode(blob).decode()
        lines = [b64[i:i + 70] for i in range(0, len(b64), 70)]
        return ("-----BEGIN SSH SIGNATURE-----\n" + "\n".join(lines) + "\n-----END SSH SIGNATURE-----\n").encode()


class FakeKeygenSign:
    """Stands in for ONLY the ``ssh-keygen -Y sign`` call; every verify still runs the real ssh-keygen. ``plan`` is one
    entry per sign: the flags byte WRITTEN (None = honest)."""

    def __init__(self, key: SoftSk, plan: list[int | None], real_run) -> None:
        self.key, self.plan, self.real_run, self.signs = key, list(plan), real_run, 0

    def __call__(self, argv, **kw):
        if "-Y" in argv and argv[argv.index("-Y") + 1] == "sign":
            self.signs += 1
            written = self.plan.pop(0) if self.plan else None
            ns = argv[argv.index("-n") + 1]
            out = self.key.sign(kw["input"], ns, counter=self.signs, sig_flags=written)
            return subprocess.CompletedProcess(argv, 0, stdout=out, stderr=b"")
        return self.real_run(argv, **kw)


@pytest.fixture
def rig(tmp_path, monkeypatch):
    key = SoftSk(tmp_path / "keys")
    fake = FakeKeygenSign(key, [], subprocess.run)
    monkeypatch.setattr(keys.subprocess, "run", fake)
    fp, sign = ssh_signer(key.path, namespace=NS)

    class R:
        pass

    r = R()
    r.key, r.fake, r.fp, r.sign, r.dir = key, fake, fp, sign, key.path.parent / "sign-failures"
    return r


MESSAGE = b"sign: 1267: 100% agree, a very private consent answer text that must never reach a diagnostics file\n"


def test_reproduction_a_flipped_flags_byte_does_not_verify_in_ssh_keygen(rig):
    """The failure as measured 11-18: flags 0x21 where the authenticator signed 0x01; verify and check-novalidate both fail
    with libcrypto's error. This is what the two real failures looked like, built in software."""
    sig = rig.key.sign(MESSAGE, NS, flags=0x01, sig_flags=0x21).decode()
    assert _sshsig_type_and_flags(sig) == (SK, 0x21)
    detail = keys._authority_sig.verify_failure_detail(MESSAGE, sig, public_key=SK + " " + base64.b64encode(rig.key.blob).decode(),
                                                       namespace=NS)
    assert "verify: Signature verification failed" in detail and "check-novalidate: Signature verification failed" in detail, detail


def test_an_honest_sign_returns_at_once_and_leaves_no_evidence(rig):
    sig = rig.sign(MESSAGE)
    assert sig.startswith("-----BEGIN SSH SIGNATURE-----")
    assert rig.fake.signs == 1 and not rig.dir.exists()


def test_a_corrupted_first_answer_is_retried_once_and_the_good_second_is_returned(rig, capsys):
    rig.fake.plan = [0x21, None]
    sig = rig.sign(MESSAGE)
    assert rig.fake.signs == 2
    assert _sshsig_type_and_flags(sig) == (SK, 0x01)
    assert "the key's answer did not check out; touch again" in capsys.readouterr().err
    files = list(rig.dir.iterdir())
    assert len(files) == 1
    f = files[0]
    assert stat.S_IMODE(f.stat().st_mode) == 0o600 and stat.S_IMODE(rig.dir.stat().st_mode) == 0o700
    ev = json.loads(f.read_text())
    assert ev["flags"] == "0x21" and ev["counter"] == 1 and ev["fingerprint"] == rig.fp and ev["attempt"] == 1
    assert ev["message_sha256"] == hashlib.sha256(MESSAGE).hexdigest()
    assert ev["signature"].startswith("-----BEGIN SSH SIGNATURE-----") and ev["time"]
    assert "verify: Signature verification failed" in ev["ssh_keygen"] and "check-novalidate:" in ev["ssh_keygen"]


def test_the_evidence_file_holds_no_message_bytes(rig):
    rig.fake.plan = [0x21, None]
    rig.sign(MESSAGE)
    raw = next(rig.dir.iterdir()).read_bytes()
    for piece in (MESSAGE, b"private consent answer", b"sign: 1267", base64.b64encode(MESSAGE)):
        assert piece not in raw


def test_two_failures_raise_with_both_flags_and_both_evidence_paths(rig):
    rig.fake.plan = [0x21, 0x23]
    with pytest.raises(KeyError_) as err:
        rig.sign(MESSAGE)
    text = str(err.value)
    assert rig.fake.signs == 2                      # one retry, never a third
    assert "both attempts" in text and "0x21" in text and "0x23" in text
    assert "the key's answer did not check out" in text
    files = sorted(rig.dir.iterdir())
    assert len(files) == 2 and all(str(f) in text for f in files)
    assert text.startswith(f"ssh-keygen returned a signature that does not verify as {rig.fp}")


def test_the_evidence_directory_is_bounded(rig):
    for _ in range(keys._SIGN_FAILURE_KEEP):        # each failing sign writes two files
        rig.fake.plan = [0x21, 0x21]
        with pytest.raises(KeyError_):
            rig.sign(MESSAGE)
    assert len(list(rig.dir.iterdir())) == keys._SIGN_FAILURE_KEEP
    # the oldest go, the newest stay
    newest = max(p.name for p in rig.dir.iterdir())
    rig.fake.plan = [0x21, 0x21]
    with pytest.raises(KeyError_):
        rig.sign(MESSAGE)
    names = sorted(p.name for p in rig.dir.iterdir())
    assert len(names) == keys._SIGN_FAILURE_KEEP and newest in names


def test_a_file_that_is_not_ours_is_left_alone_by_the_prune(rig):
    rig.dir.mkdir(mode=0o700)
    other = rig.dir / "notes.txt"
    other.write_text("mine")
    for _ in range(keys._SIGN_FAILURE_KEEP + 2):
        rig.fake.plan = [0x21, 0x21]
        with pytest.raises(KeyError_):
            rig.sign(MESSAGE)
    assert other.read_text() == "mine"


def test_a_no_touch_signature_is_still_refused_without_a_retry(rig):
    rig.fake.plan = [0x00, None]
    with pytest.raises(KeyError_, match="without the user-presence flag"):
        rig.sign(MESSAGE)
    assert rig.fake.signs == 1


def test_an_unwritable_evidence_dir_does_not_change_the_refusal(rig, tmp_path):
    (rig.key.path.parent / "sign-failures").write_text("a file where the directory should be")
    rig.fake.plan = [0x21, None]
    assert _sshsig_type_and_flags(rig.sign(MESSAGE))[1] == 0x01        # the retry still works
    rig.fake.plan = [0x21, 0x21]
    with pytest.raises(KeyError_, match="evidence not written"):
        rig.sign(MESSAGE)


def test_a_non_sk_key_that_fails_to_verify_is_not_retried(tmp_path, monkeypatch):
    d = tmp_path / "k"
    d.mkdir(mode=0o700)
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "t", "-f", str(d / "k")], check=True)
    fp, sign = ssh_signer(d / "k")
    monkeypatch.setattr(keys._authority, "verify_link_sig", lambda *a, **k: False)
    calls: list[int] = []
    real = subprocess.run

    def counting(argv, **kw):
        if "-Y" in argv and argv[argv.index("-Y") + 1] == "sign":
            calls.append(1)
        return real(argv, **kw)

    monkeypatch.setattr(keys.subprocess, "run", counting)
    with pytest.raises(KeyError_, match="does not verify"):
        sign(b"x")
    assert len(calls) == 1 and not (d / "sign-failures").exists()

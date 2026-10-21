"""`levain team record` needs a signature with user presence (Phill 10-21 "12a"): a file or agent key is refused unless
the team's owner allowed file keys, and a FIDO key whose signature carries no presence flag is refused and undone.

The FIDO key here is a software authenticator: a P-256 key whose signatures are laid out as an sk-ecdsa key's (OpenSSH
PROTOCOL.u2f), with the flags byte read from a file, used by git through ``gpg.ssh.program``. Stock ssh-keygen verifies
them; no hardware is touched, so what a real touch adds (the person at the key) is outside this test.
"""
import base64
import os
import shutil
import struct
import subprocess
import sys
from pathlib import Path

import pytest

from levain.cli import main as levain_main
import levain.team.transport as transport

pytestmark = pytest.mark.skipif(shutil.which("git") is None or shutil.which("ssh-keygen") is None
                                or os.name != "posix", reason="needs git + ssh-keygen + POSIX")

SIGNER = r'''#!{py}
import base64, hashlib, os, struct, sys
from pathlib import Path
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
a = sys.argv[1:]
if a[:2] != ["-Y", "sign"]:
    os.execv("{real}", ["{real}"] + a)
ns, pub, msgf = a[a.index("-n") + 1], Path(a[a.index("-f") + 1]), a[-1]
def s(b): return struct.pack(">I", len(b)) + b
def mp(n):
    b = n.to_bytes((n.bit_length() + 7) // 8 or 1, "big")
    return s(b"\x00" + b if b[0] & 0x80 else b)
priv = serialization.load_pem_private_key(Path(str(pub) + ".emu").read_bytes(), None)
flags = int(Path(str(pub) + ".flags").read_text())
signed = b"SSHSIG" + s(ns.encode()) + s(b"") + s(b"sha512") + s(hashlib.sha512(Path(msgf).read_bytes()).digest())
auth = hashlib.sha256(b"ssh:").digest() + bytes([flags]) + struct.pack(">I", 1) + hashlib.sha256(signed).digest()
r, v = decode_dss_signature(priv.sign(auth, ec.ECDSA(hashes.SHA256())))
t = b"sk-ecdsa-sha2-nistp256@openssh.com"
sig = s(t) + s(mp(r) + mp(v)) + bytes([flags]) + struct.pack(">I", 1)
blob = b"SSHSIG" + struct.pack(">I", 1) + s(base64.b64decode(pub.read_text().split()[1])) + s(ns.encode()) + s(b"") \
    + s(b"sha512") + s(sig)
b = base64.b64encode(blob).decode()
Path(msgf + ".sig").write_text("-----BEGIN SSH SIGNATURE-----\n" + "\n".join(b[i:i + 70] for i in range(0, len(b), 70))
                               + "\n-----END SSH SIGNATURE-----\n")
'''


def git(*args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout


def team(*args, repo: Path) -> int:
    return levain_main(["team", *args, "--repo", str(repo)])


def project(tmp: Path) -> Path:
    git("init", "-q", "--bare", "--initial-branch=main", "origin.git", cwd=tmp)
    git("clone", "-q", str(tmp / "origin.git"), "ana", cwd=tmp)
    ana = tmp / "ana"
    git("config", "user.email", "ana@ex.com", cwd=ana)
    git("config", "user.name", "ana", cwd=ana)
    (ana / "a.py").write_text("x = 1\n")
    git("add", ".", cwd=ana)
    git("commit", "-qm", "init", cwd=ana)
    git("push", "-q", "origin", "HEAD:main", cwd=ana)
    return ana


def ledger_tip(tmp: Path) -> str:
    return git("rev-parse", "levain-team-ledger", cwd=tmp / "origin.git").strip()


def record(repo: Path, words: str) -> int:
    return team("record", "decision", "--kind", "ruling", "--owner", "ana", "--paths", "a.py", "--words", words,
                repo=repo)


def test_a_file_key_records_nothing_until_the_owner_allows_file_keys(tmp_path, monkeypatch, capsys) -> None:
    # reproduced on 05741fbd (coord/c8_probe_run.txt): a passphrase-less file key recorded, from any process of the user
    monkeypatch.setattr(transport, "_REQUIRE_PRESENCE", True)
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "ana@test", "-f", str(tmp_path / "k")],
                   check=True, capture_output=True)
    ana = project(tmp_path)
    assert team("init", "--project", "p", "--owner", "ana", "--member", "ana=ana@ex.com",
                "--signing-key", str(tmp_path / "k.pub"), repo=ana) == 0
    tip = ledger_tip(tmp_path)
    capsys.readouterr()
    assert record(ana, "a file key wrote this") != 0
    err = capsys.readouterr()
    assert "ssh-ed25519 key" in err.err + err.out and "levain team file-keys allow" in err.err + err.out
    assert ledger_tip(tmp_path) == tip
    assert "a file key wrote this" not in git("log", "-p", "levain-team-ledger", cwd=ana)

    assert team("file-keys", "allow", repo=ana) == 0
    assert "allow_file_keys = true" in git("show", "levain-team-ledger:team.toml", cwd=tmp_path / "origin.git")
    assert record(ana, "allowed file key") == 0
    capsys.readouterr()
    assert team("status", repo=ana) == 0
    out = capsys.readouterr().out
    assert "recorded by ana (human) (file key, no presence)" in out and "file keys: ALLOWED" in out


def test_a_fido_key_records_with_a_touch_and_is_refused_and_undone_without_one(tmp_path, monkeypatch, capsys) -> None:
    # reproduced on 05741fbd: team signing refused every sk-* key ("security-key types ... are not accepted"); and stock
    # `ssh-keygen -Y check-novalidate` calls a flags=0x00 sk signature good, so only levain's flag check refuses it
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    monkeypatch.setattr(transport, "_REQUIRE_PRESENCE", True)
    priv = ec.generate_private_key(ec.SECP256R1())
    point = priv.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)

    def s(b: bytes) -> bytes:
        return struct.pack(">I", len(b)) + b
    blob = s(b"sk-ecdsa-sha2-nistp256@openssh.com") + s(b"nistp256") + s(point) + s(b"ssh:")
    pub = tmp_path / "sk.pub"
    pub.write_text(f"sk-ecdsa-sha2-nistp256@openssh.com {base64.b64encode(blob).decode()} ana@test\n")
    Path(str(pub) + ".emu").write_bytes(priv.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                                           serialization.NoEncryption()))
    flags = Path(str(pub) + ".flags")
    flags.write_text("1")
    bindir = tmp_path / "bin"
    bindir.mkdir()
    signer = bindir / "ssh-keygen"
    signer.write_text(SIGNER.replace("{py}", sys.executable).replace("{real}", shutil.which("ssh-keygen")))
    signer.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    ana = project(tmp_path)
    assert team("init", "--project", "p", "--owner", "ana", "--member", "ana=ana@ex.com",
                "--signing-key", str(pub), repo=ana) == 0
    assert record(ana, "touched") == 0
    capsys.readouterr()
    assert team("status", repo=ana) == 0
    out = capsys.readouterr().out
    assert '"touched"' in out and "no presence" not in out

    flags.write_text("0")          # the same key, signing as if made with no-touch-required
    tip, local = ledger_tip(tmp_path), git("rev-parse", "levain-team-ledger", cwd=ana).strip()
    assert record(ana, "untouched") != 0
    err = capsys.readouterr()
    assert "carries no user-presence flag" in err.err + err.out
    assert ledger_tip(tmp_path) == tip
    assert git("rev-parse", "levain-team-ledger", cwd=ana).strip() == local
    assert "untouched" not in git("log", "-p", "levain-team-ledger", cwd=ana)

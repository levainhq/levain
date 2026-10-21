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
import levain.team.tenure as tenure

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
if not Path(str(pub) + ".emu").exists():
    os.execv("{real}", ["{real}"] + a)      # any other key signs as ssh-keygen signs it
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


def fido_key(tmp: Path, monkeypatch) -> tuple[Path, Path]:
    """A software sk-ecdsa key (the module docstring's authenticator) on PATH's ssh-keygen; (its .pub, its flags file,
    "1" = user present)."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    priv = ec.generate_private_key(ec.SECP256R1())
    point = priv.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)

    def s(b: bytes) -> bytes:
        return struct.pack(">I", len(b)) + b
    blob = s(b"sk-ecdsa-sha2-nistp256@openssh.com") + s(b"nistp256") + s(point) + s(b"ssh:")
    pub = tmp / "sk.pub"
    pub.write_text(f"sk-ecdsa-sha2-nistp256@openssh.com {base64.b64encode(blob).decode()} ana@test\n")
    Path(str(pub) + ".emu").write_bytes(priv.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                                           serialization.NoEncryption()))
    flags = Path(str(pub) + ".flags")
    flags.write_text("1")
    bindir = tmp / "bin"
    bindir.mkdir()
    signer = bindir / "ssh-keygen"
    signer.write_text(SIGNER.replace("{py}", sys.executable).replace("{real}", shutil.which("ssh-keygen")))
    signer.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    return pub, flags


def test_a_file_key_records_nothing_unless_the_team_allows_file_keys(tmp_path, monkeypatch, capsys) -> None:
    # reproduced on 05741fbd (coord/c8_probe_run.txt): a passphrase-less file key recorded, from any process of the user
    monkeypatch.setattr(tenure, "_REQUIRE_PRESENCE", True)
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

    # the switch is judged by the rule it switches: a file-key owner cannot loosen a team that refuses file keys
    assert team("file-keys", "allow", repo=ana) != 0
    err = capsys.readouterr()
    assert "levain team init --allow-file-keys" in err.err + err.out
    assert "allow_file_keys = false" in git("show", "levain-team-ledger:team.toml", cwd=tmp_path / "origin.git")

    # the bootstrap: a team that allows file keys from its genesis
    (tmp_path / "two").mkdir()
    gus = project(tmp_path / "two")
    assert team("init", "--project", "p", "--owner", "ana", "--member", "ana=ana@ex.com", "--allow-file-keys",
                "--signing-key", str(tmp_path / "k.pub"), repo=gus) == 0
    assert record(gus, "allowed file key") == 0
    capsys.readouterr()
    assert team("status", repo=gus) == 0
    out = capsys.readouterr().out
    assert "recorded by ana (human) (file key, no presence)" in out and "file keys: ALLOWED" in out


def test_a_fido_key_records_with_a_touch_and_is_refused_and_undone_without_one(tmp_path, monkeypatch, capsys) -> None:
    # reproduced on 05741fbd: team signing refused every sk-* key ("security-key types ... are not accepted"); and stock
    # `ssh-keygen -Y check-novalidate` calls a flags=0x00 sk signature good, so only levain's flag check refuses it
    monkeypatch.setattr(tenure, "_REQUIRE_PRESENCE", True)
    pub, flags = fido_key(tmp_path, monkeypatch)
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


def test_an_entry_without_presence_is_not_in_force_and_not_published_where_the_team_refuses(tmp_path, monkeypatch,
                                                                                             capsys) -> None:
    # reproduced on 6ab12370 (teampres/fix1_repro_before.txt (a)): ledger_api's append(presence=False) of a ruling, a
    # file key, the team refusing file keys: in force, and the next sync published it. The writer's own check is
    # switched off here, as any code running as the user can; the READ decides.
    from levain.team import entry as E
    from levain.team.ledger_api import open_ledger
    from levain.team.transport import Repo
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "ana@test", "-f", str(tmp_path / "k")],
                   check=True, capture_output=True)
    ana = project(tmp_path)
    assert team("init", "--project", "p", "--owner", "ana", "--member", "ana=ana@ex.com",
                "--signing-key", str(tmp_path / "k.pub"), repo=ana) == 0
    assert "allow_file_keys = false" in git("show", "levain-team-ledger:team.toml", cwd=tmp_path / "origin.git")
    open_ledger(Repo.discover(ana)).append(E.build("ana", "decision", kind="ruling", owner="ana", paths=["a.py"],
                                                   words="bypassed", agent="human"), push=False)
    monkeypatch.setattr(tenure, "_REQUIRE_PRESENCE", True)
    tip = ledger_tip(tmp_path)
    assert not open_ledger(Repo.discover(ana)).ledger().in_force
    capsys.readouterr()
    assert team("verify", repo=ana) != 0
    assert "signed with no user presence while the team refuses file keys; not in force" in capsys.readouterr().out
    assert team("sync", repo=ana) == 0
    err = capsys.readouterr()
    assert "kept back under refs/levain/held/" in err.err + err.out and "reset" not in err.err + err.out
    assert ledger_tip(tmp_path) == tip
    assert "bypassed" in git("log", "-p", "--all", "--not", "levain-team-ledger", cwd=ana)


def test_a_file_key_entry_made_while_allowed_is_held_not_published_after_the_owner_refuses(tmp_path, monkeypatch,
                                                                                           capsys) -> None:
    # reproduced on 6ab12370 (teampres/fix1_repro_before.txt (c)): Ben recorded offline with his file key while the team
    # allowed file keys; Ana (a FIDO key) refused them; Ben's sync re-signed his entry onto the refusing parent,
    # published it, and it was in force
    monkeypatch.setattr(tenure, "_REQUIRE_PRESENCE", True)
    pub, _flags = fido_key(tmp_path, monkeypatch)
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "ben@test", "-f", str(tmp_path / "kb")],
                   check=True, capture_output=True)
    ana = project(tmp_path)
    assert team("init", "--project", "p", "--owner", "ana", "--member", "ana=ana@ex.com", "--member",
                f"ben=ben@ex.com={tmp_path / 'kb.pub'}", "--signing-key", str(pub), repo=ana) == 0
    assert team("file-keys", "allow", repo=ana) == 0
    git("clone", "-q", str(tmp_path / "origin.git"), "ben", cwd=tmp_path)
    ben = tmp_path / "ben"
    git("config", "user.email", "ben@ex.com", cwd=ben)
    git("config", "user.name", "ben", cwd=ben)
    assert team("join", "--signing-key", str(tmp_path / "kb.pub"), "--no-install", repo=ben) == 0
    assert team("record", "decision", "--kind", "practice", "--paths", "a.py", "--words", "ben offline",
                "--no-push", repo=ben) == 0
    assert team("file-keys", "refuse", repo=ana) == 0
    tip = ledger_tip(tmp_path)
    capsys.readouterr()
    assert team("sync", repo=ben) == 0
    err = capsys.readouterr()
    assert "kept back under refs/levain/held/ (" in err.err + err.out
    assert "the team refuses file keys and they are signed with no user presence" in err.err + err.out
    assert ledger_tip(tmp_path) == tip
    assert "ben offline" not in git("log", "-p", "levain-team-ledger", cwd=tmp_path / "origin.git")


def test_sync_holds_from_the_first_entry_without_presence_and_publishes_the_rest(tmp_path, monkeypatch,
                                                                                 capsys) -> None:
    # reproduced on c2d6fbeb (teampres/fix2/repro_before.txt (P)): an ack, a ruling with no presence, an ack, all
    # unpublished; sync refused all three and named a hard reset that dropped them. Now the ruling is held, the first
    # ack publishes, and the second is re-sealed onto the published chain and publishes too
    from levain.team import entry as E
    from levain.team.ledger_api import open_ledger
    from levain.team.transport import Repo
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "ana@test", "-f", str(tmp_path / "k")],
                   check=True, capture_output=True)
    ana = project(tmp_path)
    assert team("init", "--project", "p", "--owner", "ana", "--member", "ana=ana@ex.com", "--allow-file-keys",
                "--signing-key", str(tmp_path / "k.pub"), repo=ana) == 0
    assert record(ana, "first") == 0
    assert team("file-keys", "refuse", repo=ana) == 0
    gl = open_ledger(Repo.discover(ana))
    rid = gl.ledger().in_force[0]["id"]
    for e in (E.build("ana", "ack", refs=[rid], session="s", agent="claude-code", summary="ack one"),
              E.build("ana", "decision", kind="ruling", owner="ana", paths=["a.py"], words="bypassed", agent="human"),
              E.build("ana", "ack", refs=[rid], session="s", agent="claude-code", summary="ack two")):
        open_ledger(Repo.discover(ana)).append(e, push=False)       # the writer's check off, as the suite runs
    monkeypatch.setattr(tenure, "_REQUIRE_PRESENCE", True)
    capsys.readouterr()
    assert team("sync", repo=ana) == 0
    err = capsys.readouterr()
    assert "kept back under refs/levain/held/ (" in err.err + err.out and "reset" not in err.err + err.out
    published = git("log", "-p", "levain-team-ledger", cwd=tmp_path / "origin.git")
    assert "ack one" in published and "ack two" in published and "bypassed" not in published
    assert "bypassed" in git("log", "-p", "--all", "--not", "levain-team-ledger", cwd=ana)
    assert team("verify", repo=ana) == 0


def test_a_regenesis_keeps_the_teams_presence_rule(tmp_path) -> None:
    # reproduced on c2d6fbeb (teampres/fix2/repro_before.txt (G)): a refusing team's re-genesis had no allow_file_keys
    # line, so the new ledger read as a team that has not chosen, where an entry with no presence counts
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "ana@test", "-f", str(tmp_path / "k")],
                   check=True, capture_output=True)
    ana = project(tmp_path)
    assert team("init", "--project", "p", "--owner", "ana", "--member", "ana=ana@ex.com",
                "--signing-key", str(tmp_path / "k.pub"), repo=ana) == 0
    tip = ledger_tip(tmp_path)
    assert team("regenesis", "--from", tip, "--owner", "ana", "--member", "ana=ana@ex.com", repo=ana) == 0
    branches = [b for b in git("for-each-ref", "--format=%(refname:short)", "refs/heads/",
                               cwd=tmp_path / "origin.git").split() if b.startswith("levain-team-ledger-")]
    assert len(branches) == 1
    assert "allow_file_keys = false" in git("show", f"{branches[0]}:team.toml", cwd=tmp_path / "origin.git")


def _held_ruling_behind_an_ack(tmp_path, monkeypatch, *, mixed: bool) -> Path:
    """ana's file-key team refuses file keys; offline: a ruling with no presence, then an ack (in ONE commit with
    `member remove ben` when ``mixed``), then a second ack; synced once with the rule on."""
    from levain.team import entry as E
    from levain.team import transport as TR
    from levain.team.ledger_api import open_ledger
    for k in ("k", "kb"):
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", f"{k}@test", "-f", str(tmp_path / k)],
                       check=True, capture_output=True)
    ana = project(tmp_path)
    assert team("init", "--project", "p", "--owner", "ana", "--member", "ana=ana@ex.com", "--member",
                f"ben=ben@ex.com={tmp_path / 'kb.pub'}", "--allow-file-keys", "--signing-key", str(tmp_path / "k.pub"),
                repo=ana) == 0
    assert record(ana, "first") == 0
    assert team("file-keys", "refuse", repo=ana) == 0
    rid = open_ledger(TR.Repo.discover(ana)).ledger().in_force[0]["id"]
    for e in (E.build("ana", "decision", kind="ruling", owner="ana", paths=["a.py"], words="bypassed", agent="human"),
              E.build("ana", "ack", refs=[rid], session="s", agent="claude-code", summary="ack one")):
        gl = open_ledger(TR.Repo.discover(ana))
        gl.append(e, push=False)       # the writer's check off, as the suite runs
    if mixed:
        # the ack's line left staged and committed WITH the team change: one own signed commit holding both
        git("reset", "-q", "--soft", "HEAD^", cwd=gl.wt)
        with monkeypatch.context() as m:
            m.setattr(TR.GitLedger, "_recover_dirty", lambda self: None)
            assert team("member", "remove", "ben", "--no-push", repo=ana) == 0
        assert "team.toml" in git("show", "--name-only", "--format=", "HEAD", cwd=gl.wt)
    open_ledger(TR.Repo.discover(ana)).append(
        E.build("ana", "ack", refs=[rid], session="s", agent="claude-code", summary="ack two"), push=False)
    monkeypatch.setattr(tenure, "_REQUIRE_PRESENCE", True)
    assert team("sync", repo=ana) == 0
    return ana


def test_a_commit_with_a_team_change_and_an_entry_behind_a_held_entry_loses_neither(tmp_path, monkeypatch) -> None:
    # reproduced on db17b607 (teampres/fix3/repro_before.txt (M)): the mixed commit was stripped to its team change
    # when the branch moved back past the held ruling, and its ack was on no branch and under no ref
    ana = _held_ruling_behind_an_ack(tmp_path, monkeypatch, mixed=True)
    published = git("log", "-p", "levain-team-ledger", cwd=tmp_path / "origin.git")
    assert "ack one" in published and "ack two" in published and "bypassed" not in published
    assert "ben" not in git("show", "levain-team-ledger:team.toml", cwd=tmp_path / "origin.git").split("[members]")[-1]
    assert "bypassed" in git("log", "-p", "--all", "--not", "levain-team-ledger", cwd=ana)
    assert team("verify", repo=ana) == 0


def test_a_held_entry_publishes_once_it_counts_after_a_later_entry_went_out_past_it(tmp_path, monkeypatch,
                                                                                    capsys) -> None:
    # reproduced on db17b607 (teampres/fix3/repro_before.txt (A)): ack two was re-sealed and published past the held
    # ruling, so once the ruling counted its plain pick conflicted, and every sync kept it held with "could not be
    # replayed"
    ana = _held_ruling_behind_an_ack(tmp_path, monkeypatch, mixed=False)
    monkeypatch.setattr(tenure, "_REQUIRE_PRESENCE", False)      # the policy now lets it count
    capsys.readouterr()
    assert team("sync", repo=ana) == 0
    assert "could not be" not in capsys.readouterr().err
    assert "bypassed" in git("log", "-p", "levain-team-ledger", cwd=tmp_path / "origin.git")
    assert not git("for-each-ref", "refs/levain/held/", cwd=ana).strip()
    assert team("verify", repo=ana) == 0


def test_an_interrupted_write_on_a_fido_clone_is_signed_only_after_it_is_named_on_a_terminal(tmp_path, monkeypatch,
                                                                                            capsys) -> None:
    # reproduced on 9537e31a (teampres/fix4/repro_before.txt (R)): killed after the flush, before the commit, the next
    # non-interactive sync committed the entry under an unnamed signature and published it
    from levain.team import entry as E
    from levain.team import transport as TR
    from levain.team.ledger_api import open_ledger

    class Killed(BaseException):
        pass

    def killed(self, message):
        raise Killed

    monkeypatch.setattr(tenure, "_REQUIRE_PRESENCE", True)
    pub, _flags = fido_key(tmp_path, monkeypatch)
    ana = project(tmp_path)
    assert team("init", "--project", "p", "--owner", "ana", "--member", "ana=ana@ex.com",
                "--signing-key", str(pub), repo=ana) == 0
    gl = open_ledger(TR.Repo.discover(ana))
    with monkeypatch.context() as m:
        m.setattr(TR.GitLedger, "_commit", killed)
        with pytest.raises(Killed):
            gl.append(E.build("ana", "decision", kind="ruling", owner="ana", paths=["a.py"], words="crashed",
                              agent="human"), push=False)
    wt, tip = gl.wt, ledger_tip(tmp_path)
    capsys.readouterr()
    assert team("sync", repo=ana) != 0
    err = capsys.readouterr().err
    assert "decision ana-" in err and ": crashed" in err and "`levain team sync` in a terminal" in err
    assert "crashed" not in git("log", "-p", "HEAD", cwd=wt) and git("status", "--porcelain", cwd=wt).strip()
    assert ledger_tip(tmp_path) == tip

    monkeypatch.setattr(TR, "_interactive", lambda: True)
    assert team("sync", repo=ana) == 0
    assert "to sign decision ana-" in capsys.readouterr().err
    assert "crashed" in git("log", "-p", "levain-team-ledger", cwd=tmp_path / "origin.git")
    assert [e["words"] for e in open_ledger(TR.Repo.discover(ana)).ledger().in_force] == ["crashed"]


def test_a_team_change_committed_with_a_line_outside_an_entry_file_refuses_by_name(tmp_path, monkeypatch,
                                                                                  capsys) -> None:
    # reproduced on 9537e31a (teampres/fix4/repro_before.txt (J)): a sealed line at ledger/junk in one commit with a
    # team change passed as appended ledger lines, and sync published it
    from levain.team import transport as TR
    from levain.team.ledger_api import open_ledger
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "k@test", "-f", str(tmp_path / "k")],
                   check=True, capture_output=True)
    ana = project(tmp_path)
    assert team("init", "--project", "p", "--owner", "ana", "--member", "ana=ana@ex.com", "--member", "ben=ben@ex.com",
                "--signing-key", str(tmp_path / "k.pub"), "--allow-file-keys", repo=ana) == 0
    assert record(ana, "first") == 0
    wt = open_ledger(TR.Repo.discover(ana)).wt
    line = next((wt / "ledger").rglob("*.jsonl")).read_text().splitlines()[0]
    (wt / "ledger" / "junk").write_text(line + "\n")
    git("add", "ledger/junk", cwd=wt)
    with monkeypatch.context() as m:
        m.setattr(TR.GitLedger, "_recover_dirty", lambda self: None)
        assert team("member", "remove", "ben", "--no-push", repo=ana) == 0
    head, tip = git("rev-parse", "HEAD", cwd=wt), ledger_tip(tmp_path)
    capsys.readouterr()
    assert team("sync", repo=ana) != 0
    assert "AND ledger/junk" in capsys.readouterr().err
    assert git("rev-parse", "HEAD", cwd=wt) == head and not git("status", "--porcelain", cwd=wt).strip()
    assert ledger_tip(tmp_path) == tip

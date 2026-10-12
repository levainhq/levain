"""Custody (trust-tiering DESIGN §3.2, rev 6 presence floor items 1-5): ``levain cockpit key create``, the
sidecar, the encrypted-file check, the CTK identity re-check, ``ssh_signer``'s provider and agent handling,
and the CLI's custody refusals.

The touch-id path runs against FAKE ``sc_auth`` and ``ssh-keygen -K`` (scripts in a temp dir) so no real
Secure Enclave identity is made: a ``-t bio`` key would need Phill's finger to sign. The fakes speak the
formats measured on macOS 26.6.2 (``sc_auth list-ctk-identities`` columns; ``ssh-keygen -K`` writing
``id_ecdsa_sk_rk`` and asking ``Overwrite (y/n)?`` on stdin before a second key). The fake identity hash is
the SHA-1 of the point, as measured. One real create + list + fetch + delete is recorded in the build's
RETURN.md, not run here.
"""
from __future__ import annotations

import base64
import hashlib
import json
import stat
import struct
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

import levain.cockpit.custody as custody
from levain.cockpit.keys import KeyError_
from tests.cockpit_authority_rig import RealKey
from tests.cockpit_authority_rig import enrol as genesis_enrol

NOW = datetime(2026, 10, 12, 9, 0, tzinfo=timezone.utc)
PY = sys.executable


def _s(b: bytes) -> bytes:
    return struct.pack(">I", len(b)) + b


def _sk_from_ecdsa(ecdsa_pub: str) -> str:
    raw = base64.b64decode(ecdsa_pub.split()[1])
    fields, i = [], 0
    while i < len(raw):
        (n,) = struct.unpack(">I", raw[i:i + 4])
        fields.append(raw[i + 4:i + 4 + n])
        i += 4 + n
    blob = _s(b"sk-ecdsa-sha2-nistp256@openssh.com") + _s(b"nistp256") + _s(fields[2]) + _s(b"ssh:")
    return "sk-ecdsa-sha2-nistp256@openssh.com " + base64.b64encode(blob).decode()


def _point_sha1(sk_pub: str) -> str:
    raw = base64.b64decode(sk_pub.split()[1])
    i, fields = 0, []
    while i < len(raw):
        (n,) = struct.unpack(">I", raw[i:i + 4])
        fields.append(raw[i + 4:i + 4 + n])
        i += 4 + n
    return hashlib.sha1(fields[2]).hexdigest().upper()


def _ecdsa(d: Path, name: str) -> str:
    subprocess.run(["ssh-keygen", "-q", "-t", "ecdsa", "-b", "256", "-N", "", "-f", str(d / name)], check=True,
                   capture_output=True, timeout=30)
    return " ".join((d / f"{name}.pub").read_text().split()[:2])


# --- the fakes ---------------------------------------------------------------------------------------

# Each fake records the environment it was started with (STATE + ".env", one JSON line per run), and finds
# its state through the STATE line written above its body, never the environment: a helper hands its tools
# the allowlist only (levain.launch.child_env), which carries no FAKE_STATE.
_RECORD_ENV = r'''
import json, os, sys
with open(STATE + ".env", "a") as _fh:
    _fh.write(json.dumps({"argv": sys.argv[1:], "env": dict(os.environ)}) + "\n")
'''

_FAKE_SC_AUTH = r'''
import json, sys, os
state = STATE
db = json.load(open(state))
cmd = sys.argv[1:]
log = open(state + ".log", "a")
log.write(" ".join(cmd) + "\n")
if cmd[0] == "list-ctk-identities":
    rows = db["identities"]
    lw = max([len("Label")] + [len(r["label"]) for r in rows])
    print("Key Type Public Key Hash" + " " * 26 + "Prot " + "Label".ljust(lw + 1) + "Common Name    Email Address Valid To        Valid ")
    for r in rows:
        print("p-256-ne " + r["hash"] + " " + r["prot"].ljust(4) + " " + r["label"].ljust(lw + 1) + r["label"] + "   10/12/27, 15:28 YES   ")
elif cmd[0] == "create-ctk-identity":
    label = cmd[cmd.index("-l") + 1]
    if db.get("concurrent"):               # another run's identity, same label, lands first
        other = db["next"].pop(0)
        db["identities"].append({"hash": other["hash"], "prot": "bio", "label": label, "pub": other["pub"]})
    nxt = db["next"].pop(0)
    db["identities"].append({"hash": nxt["hash"], "prot": db.get("prot", cmd[cmd.index("-t") + 1]), "label": label,
                             "pub": nxt["pub"]})
    if db.get("create_exit"):
        json.dump(db, open(state, "w")); sys.exit(db["create_exit"])
elif cmd[0] == "delete-ctk-identity":
    h = cmd[cmd.index("-h") + 1]
    db["identities"] = [r for r in db["identities"] if r["hash"] != h]
json.dump(db, open(state, "w"))
'''

_FAKE_SSH_KEYGEN = r'''
import json, os, sys
state = STATE
args = sys.argv[1:]
if "-K" not in args:
    os.execv("/usr/bin/ssh-keygen", ["ssh-keygen"] + args)
db = json.load(open(state))
open(state + ".log", "a").write("ssh-keygen " + " ".join(args) + "\n")
sys.stderr.write("Enter PIN for authenticator: You may need to touch your authenticator to authorize key download.\n")
sys.stdin.readline()                       # the PIN line
rows = [r for r in db["identities"] if not db.get("hide_new") or r["label"] != db["hide_new"]]
if db.get("ed25519"):                      # a FIDO ed25519 resident key: its own file name, no prompt
    open("id_ed25519_sk_rk", "w").write("reference ed25519\n")
    open("id_ed25519_sk_rk.pub", "w").write(db["ed25519"] + " ssh:\n")
    sys.stderr.write("Saved ED25519-SK key to id_ed25519_sk_rk\n")
for n, r in enumerate(rows):
    if n:
        print("id_ecdsa_sk_rk already exists.")
        print("Overwrite (y/n)? ", end="", flush=True)
        if sys.stdin.readline().strip() != "y":
            sys.exit(255)
    open("id_ecdsa_sk_rk", "w").write("reference " + r["hash"] + "\n")
    open("id_ecdsa_sk_rk.pub", "w").write(r["pub"] + " ssh:\n")
    sys.stderr.write("Saved ECDSA-SK key to id_ecdsa_sk_rk\n")
'''


class Mac:
    """A fake Mac: an sc_auth and an ssh-keygen sharing one state file."""

    def __init__(self, tmp: Path, monkeypatch, *, existing: int = 1) -> None:
        self.dir = tmp / "fakebin"
        self.dir.mkdir()
        self.state = tmp / "fake_state.json"
        self.sc_auth, self.ssh_keygen = self.dir / "sc_auth", self.dir / "ssh-keygen"
        for path, body in ((self.sc_auth, _FAKE_SC_AUTH), (self.ssh_keygen, _FAKE_SSH_KEYGEN)):
            path.write_text(f"#!{PY} -I\nSTATE = {str(self.state)!r}\n" + _RECORD_ENV + body)
            path.chmod(0o755)
        keys = tmp / "points"
        keys.mkdir()
        pubs = [_sk_from_ecdsa(_ecdsa(keys, f"p{n}")) for n in range(existing + 2)]
        ident = [{"hash": _point_sha1(p), "prot": "bio", "label": f"other-{n}", "pub": p}
                 for n, p in enumerate(pubs[:existing])]
        self.new = [{"hash": _point_sha1(p), "pub": p} for p in pubs[existing:]]
        self.db = {"identities": ident, "next": list(self.new)}
        self.save()
        monkeypatch.setattr(custody, "APPLE_PROVIDER", "/usr/lib/ssh-keychain.dylib")

    def save(self) -> None:
        self.state.write_text(json.dumps(self.db))

    def load(self) -> dict:
        return json.loads(self.state.read_text())

    def log(self) -> str:
        p = Path(str(self.state) + ".log")
        return p.read_text() if p.exists() else ""

    def envs(self) -> list[dict]:
        p = Path(str(self.state) + ".env")
        return [json.loads(ln) for ln in p.read_text().splitlines()] if p.exists() else []

    def create(self, kdir: Path, label: str = "levain-test", **kw):
        return custody.create_touch_id(kdir, label, sc_auth=self.sc_auth, ssh_keygen=self.ssh_keygen,
                                       platform="darwin", **kw)


@pytest.fixture
def provider_present(monkeypatch):
    """The fakes stand in for the provider; the existence check reads this path."""
    real_exists = Path.exists
    monkeypatch.setattr(Path, "exists", lambda self: True if str(self) == custody.APPLE_PROVIDER else
                        real_exists(self))


# --- key create --custody touch-id ---------------------------------------------------------------------


def test_touch_id_create_keeps_the_new_identity_s_reference_and_writes_the_sidecar(tmp_path, monkeypatch,
                                                                                   provider_present):
    mac = Mac(tmp_path, monkeypatch, existing=2)
    kdir = tmp_path / "keys"
    made = mac.create(kdir)
    assert "create-ctk-identity -l levain-test -k p-256-ne -t bio" in mac.log()
    assert made.public_key == mac.new[0]["pub"] and made.ctk_hash == mac.new[0]["hash"]
    assert made.path == kdir / "levain-test" and made.public_path == kdir / "levain-test.pub"
    assert stat.S_IMODE(kdir.stat().st_mode) == 0o700 and stat.S_IMODE(made.path.stat().st_mode) == 0o600
    assert made.public_path.read_text().split()[:2] == made.public_key.split()
    side = json.loads((kdir / "levain-test.levain-key.json").read_text())
    assert side == {"format": "levain-key/1", "custody": "touch-id", "provider": "/usr/lib/ssh-keychain.dylib",
                    "ctk_hash": made.ctk_hash, "label": "levain-test"}
    assert "delete-ctk-identity" not in mac.log()
    assert sorted(p.name for p in kdir.iterdir()) == ["levain-test", "levain-test.levain-key.json", "levain-test.pub"]
    # the -K walk answered 0, 1 and 2 overwrites: three identities, the new one last
    assert mac.log().count("ssh-keygen -K -w /usr/lib/ssh-keychain.dylib -N ") == 3


def test_touch_id_create_refuses_an_identity_not_protected_bio_and_deletes_it(tmp_path, monkeypatch,
                                                                              provider_present):
    mac = Mac(tmp_path, monkeypatch)
    mac.db["prot"] = "none"                     # what a -t none identity lists as
    mac.save()
    with pytest.raises(KeyError_, match="protection 'none', not bio; nothing is enrolled; the identity was deleted"):
        mac.create(tmp_path / "keys")
    assert [i["label"] for i in mac.load()["identities"]] == ["other-0"]
    assert "ssh-keygen -K" not in mac.log()               # refused before anything is fetched
    assert list((tmp_path / "keys").iterdir()) == []


def test_touch_id_create_refuses_when_no_fetched_reference_is_the_new_identity(tmp_path, monkeypatch,
                                                                               provider_present):
    mac = Mac(tmp_path, monkeypatch)
    mac.db["hide_new"] = "levain-test"
    mac.save()
    with pytest.raises(KeyError_, match="1 reference key.*0 of them the new identity.*the identity was deleted"):
        mac.create(tmp_path / "keys")
    assert [i["label"] for i in mac.load()["identities"]] == ["other-0"]
    assert list((tmp_path / "keys").iterdir()) == []


def test_touch_id_create_deletes_the_identity_when_creation_reports_failure(tmp_path, monkeypatch,
                                                                            provider_present):
    mac = Mac(tmp_path, monkeypatch)
    mac.db["create_exit"] = 3
    mac.save()
    with pytest.raises(KeyError_, match="exit 3.*the identity was deleted"):
        mac.create(tmp_path / "keys")
    assert [i["label"] for i in mac.load()["identities"]] == ["other-0"]


def test_a_concurrent_same_label_identity_survives_our_refusal(tmp_path, monkeypatch, provider_present):
    """codex MED (L3 r1): two runs with one label each saw the other's identity as theirs and deleted it.
    The new identity is the one in after-minus-before carrying the label; two of them is a refusal that
    deletes neither (by label nothing is ever deleted)."""
    mac = Mac(tmp_path, monkeypatch)
    mac.db["concurrent"] = True
    mac.save()
    with pytest.raises(KeyError_, match="2 new identities labelled 'levain-test'.*cannot tell which.*deleted none"):
        mac.create(tmp_path / "keys")
    assert "delete-ctk-identity" not in mac.log()
    assert sorted(i["hash"] for i in mac.load()["identities"]) == sorted(
        [mac.db["identities"][0]["hash"], mac.new[0]["hash"], mac.new[1]["hash"]])
    assert list((tmp_path / "keys").iterdir()) == []


def test_our_failure_deletes_only_our_identity_by_hash(tmp_path, monkeypatch, provider_present):
    mac = Mac(tmp_path, monkeypatch)
    mac.db["hide_new"] = "levain-test"
    mac.save()
    with pytest.raises(KeyError_, match="the identity was deleted"):
        mac.create(tmp_path / "keys")
    deletes = [ln for ln in mac.log().splitlines() if ln.startswith("delete-ctk-identity")]
    assert deletes == [f"delete-ctk-identity -h {mac.new[0]['hash']}"]


def _interrupting_create(monkeypatch, *, after_it_ran: bool) -> None:
    real = subprocess.run

    def run(argv, *a, **kw):
        if "create-ctk-identity" in argv:
            if after_it_ran:
                real(argv, *a, **kw)
            raise KeyboardInterrupt
        return real(argv, *a, **kw)
    monkeypatch.setattr(custody.subprocess, "run", run)


def test_an_interrupt_during_create_leaves_no_identity_of_ours(tmp_path, monkeypatch, provider_present):
    """codex LOW + complement LOW-2 (L3 r1): Ctrl-C during ``sc_auth create-ctk-identity`` left the
    identity behind; the cleanup region now opens before the create."""
    mac = Mac(tmp_path, monkeypatch)
    _interrupting_create(monkeypatch, after_it_ran=True)
    with pytest.raises(KeyboardInterrupt):
        mac.create(tmp_path / "keys")
    assert [i["label"] for i in mac.load()["identities"]] == ["other-0"]
    assert list((tmp_path / "keys").iterdir()) == []


def test_an_interrupt_before_the_identity_exists_deletes_nothing(tmp_path, monkeypatch, provider_present):
    mac = Mac(tmp_path, monkeypatch)
    _interrupting_create(monkeypatch, after_it_ran=False)
    with pytest.raises(KeyboardInterrupt):
        mac.create(tmp_path / "keys")
    assert [i["label"] for i in mac.load()["identities"]] == ["other-0"]
    assert "delete-ctk-identity" not in mac.log()


def test_mkdtemp_failing_after_create_deletes_ours(tmp_path, monkeypatch, provider_present):
    """codex LOW (L3 r1): the temporary directory was made outside the cleanup region."""
    mac = Mac(tmp_path, monkeypatch)

    def no_tmp(*a, **kw):
        raise PermissionError(13, "Permission denied")
    monkeypatch.setattr(custody.tempfile, "mkdtemp", no_tmp)
    with pytest.raises(KeyError_, match="PermissionError.*the identity was deleted"):
        mac.create(tmp_path / "keys")
    assert [i["label"] for i in mac.load()["identities"]] == ["other-0"]


def test_the_fetch_keeps_every_reference_and_the_matcher_picks(tmp_path, monkeypatch, provider_present):
    """glm MED (L3 r1): a -K run leaving an ecdsa and an ed25519 reference refused outright. Every
    ``*_sk_rk.pub`` is collected; the public-key matcher picks the new identity's."""
    mac = Mac(tmp_path, monkeypatch)
    blob = _s(b"sk-ssh-ed25519@openssh.com") + _s(b"\x07" * 32) + _s(b"ssh:")
    mac.db["ed25519"] = "sk-ssh-ed25519@openssh.com " + base64.b64encode(blob).decode()
    mac.save()
    made = mac.create(tmp_path / "keys")
    assert made.public_key == mac.new[0]["pub"] and made.ctk_hash == mac.new[0]["hash"]
    assert "delete-ctk-identity" not in mac.log()
    refs = custody._fetch_references(_fresh(tmp_path / "walk"), ssh_keygen=mac.ssh_keygen,
                                     provider=custody.APPLE_PROVIDER)
    assert sorted(n.split()[0] for _, n in refs) == ["sk-ecdsa-sha2-nistp256@openssh.com"] * 2 + [
        "sk-ssh-ed25519@openssh.com"]                 # other-0, ours, and the ed25519 key, once each


def _fresh(d: Path) -> Path:
    d.mkdir(mode=0o700)
    return d


def test_touch_id_create_refuses_a_label_already_in_use_before_creating(tmp_path, monkeypatch, provider_present):
    mac = Mac(tmp_path, monkeypatch)
    with pytest.raises(KeyError_, match="labelled 'other-0' already exists"):
        mac.create(tmp_path / "keys", "other-0")
    assert "create-ctk-identity" not in mac.log()


def test_touch_id_create_refuses_off_macos_and_an_open_key_dir(tmp_path, monkeypatch, provider_present):
    mac = Mac(tmp_path, monkeypatch)
    with pytest.raises(KeyError_, match="macOS only.*--custody passphrase"):
        custody.create_touch_id(tmp_path / "k", "x", sc_auth=mac.sc_auth, ssh_keygen=mac.ssh_keygen, platform="linux")
    open_dir = tmp_path / "open"
    open_dir.mkdir(mode=0o755)
    open_dir.chmod(0o755)
    with pytest.raises(KeyError_, match="open to other users"):
        mac.create(open_dir)
    with pytest.raises(KeyError_, match="label"):
        mac.create(tmp_path / "k", "bad label;rm")
    assert "create-ctk-identity" not in mac.log()


def test_the_shipped_helper_never_offers_a_no_touch_identity():
    """DESIGN §3.2 rev 6 item 4: no flag, no environment, no test hook makes -t none."""
    import inspect
    src = inspect.getsource(custody)
    assert '"-t", "bio"' in src and '"-t", "none"' not in src and "-t none" not in src


def test_point_sha1_is_what_sc_auth_lists():
    """Measured on macOS 26.6.2 (RETURN.md): the hash column of the identity whose reference key is this
    line is the SHA-1 of its uncompressed point."""
    line = ("sk-ecdsa-sha2-nistp256@openssh.com AAAAInNrLWVjZHNhLXNoYTItbmlzdHAyNTZAb3BlbnNzaC5jb20AAAAIbmlzdHAyNTYAAABB"
            "BOzb70UQZqwNgYD05Q8SZzl5SAQAc+SS0XJhZSutH5Kw3G/+Sc0zbjhT5W3rjaNsQ0UHKuobTNismdkZs02nVaoAAAAEc3NoOg==")
    assert custody.point_sha1(line) == "6996CA0EAAA409791400B4785C224D2837A18EB3"


# --- what every tool the helpers start is given (L3 r1 P1) ------------------------------------------------

_PLANTED = "planted-secret-0c8e"


def _outside_the_allowlist(record: dict) -> list[str]:
    """The names in one spawn's environment that neither the allowlist admits nor the call itself sets
    (``_VERIFY_ENV`` for the ssh-keygen checks), and any name holding the planted secret."""
    from levain.launch import allowed
    env = record["env"]
    return sorted({k for k in env if not allowed(k) and k not in custody._VERIFY_ENV}
                  | {k for k, v in env.items() if _PLANTED in v})


def test_every_tool_the_helpers_start_gets_the_allowlist_and_nothing_else(tmp_path, monkeypatch,
                                                                          provider_present):
    """codex HIGH (L3 r1): sc_auth and ssh-keygen inherited the whole restored environment, which sits in
    each child's exec-time block (``ps -E``) while it runs; ssh-keygen may wait ten minutes on a person."""
    for name in ("GH_TOKEN", "OPENAI_API_KEY", "LEVAIN_TEAM_TOKEN", "SOME_PLAIN_NAME"):
        monkeypatch.setenv(name, _PLANTED)
    mac = Mac(tmp_path, monkeypatch)
    mac.create(tmp_path / "keys")                            # list, create, list, ssh-keygen -K
    mac.db = mac.load()
    mac.db["prot"] = "none"
    mac.save()
    with pytest.raises(KeyError_, match="not bio"):
        mac.create(tmp_path / "keys", "levain-two")          # and delete
    custody.create_passphrase(tmp_path / "pkeys", "pp", ssh_keygen=_fake_keygen_making(tmp_path, "pw"),
                              interactive=True)              # ssh-keygen -t ed25519, then -y -P ""
    records = mac.envs() + [json.loads(ln) for ln in (tmp_path / "keygen.env").read_text().splitlines()]
    ran = " | ".join(" ".join(r["argv"]) for r in records)
    for what in ("list-ctk-identities", "create-ctk-identity", "delete-ctk-identity", "-K -w", "-t ed25519",
                 "-y -P"):
        assert what in ran, f"{what!r} never ran: {ran}"
    bad = {" ".join(r["argv"][:2]): _outside_the_allowlist(r) for r in records if _outside_the_allowlist(r)}
    assert bad == {}


# --- file writes (L3 r1 P4) ------------------------------------------------------------------------------


def test_a_short_write_is_continued_until_every_byte_is_written(tmp_path, monkeypatch):
    """codex LOW (L3 r1): one os.write was taken as the whole file."""
    import os
    real = os.write
    monkeypatch.setattr(custody.os, "write", lambda fd, data: real(fd, bytes(data)[:3]))
    body = b"sk-ecdsa-sha2-nistp256@openssh.com AAAA levain-test\n"
    custody._write_new(tmp_path / "f.pub", body, 0o644)
    monkeypatch.undo()
    assert (tmp_path / "f.pub").read_bytes() == body


@pytest.mark.parametrize("fails", ["write", "fsync"])
def test_a_failed_write_or_fsync_removes_the_new_file(tmp_path, monkeypatch, fails):
    def boom(*a):
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(custody.os, fails, boom)
    with pytest.raises(OSError, match="No space"):
        custody._write_new(tmp_path / "f.pub", b"abc\n", 0o644)
    monkeypatch.undo()
    assert not (tmp_path / "f.pub").exists()


# --- key create --custody passphrase --------------------------------------------------------------------


def _fake_keygen_making(tmp: Path, passphrase: str) -> Path:
    """An ssh-keygen whose key generation uses ``passphrase`` (the real one asks it on the terminal)."""
    path = tmp / "keygen-make"
    path.write_text(f"#!{PY} -I\nSTATE = {str(tmp / 'keygen')!r}\n" + _RECORD_ENV + "import os, sys\nargs = sys.argv[1:]\n"
                    f"if '-t' in args:\n    args = args + ['-q', '-N', {passphrase!r}]\n"
                    "os.execv('/usr/bin/ssh-keygen', ['ssh-keygen'] + args)\n")
    path.chmod(0o755)
    return path


def test_passphrase_create_keeps_an_encrypted_key_and_its_sidecar(tmp_path):
    kdir = tmp_path / "keys"
    made = custody.create_passphrase(kdir, "pp", ssh_keygen=_fake_keygen_making(tmp_path, "correct horse"),
                                     interactive=True)
    assert made.custody == "passphrase" and made.public_key.startswith("ssh-ed25519 ")
    assert custody.is_encrypted_file(made.path)
    assert json.loads((kdir / "pp.levain-key.json").read_text()) == {
        "format": "levain-key/1", "custody": "passphrase", "label": "pp"}
    assert custody.passphrase_problem(made.public_path, made.public_key) is None


def test_passphrase_create_refuses_an_empty_passphrase_and_keeps_nothing(tmp_path):
    kdir = tmp_path / "keys"
    with pytest.raises(KeyError_, match="not encrypted.*an empty one is refused.*the key was not kept"):
        custody.create_passphrase(kdir, "pp", ssh_keygen=_fake_keygen_making(tmp_path, ""), interactive=True)
    assert list(kdir.iterdir()) == []


def test_passphrase_create_refuses_off_a_terminal(tmp_path):
    with pytest.raises(KeyError_, match="on the terminal"):
        custody.create_passphrase(tmp_path / "keys", "pp", interactive=False)


def test_passphrase_problem_reads_the_file(tmp_path):
    plain = RealKey(tmp_path, "plain")
    assert "is not encrypted" in custody.passphrase_problem(plain.path, plain.public)
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "s3cret", "-f", str(tmp_path / "enc")], check=True,
                   capture_output=True, timeout=30)
    enc = " ".join((tmp_path / "enc.pub").read_text().split()[:2])
    assert custody.passphrase_problem(tmp_path / "enc.pub", enc) is None
    assert "not the private half" in custody.passphrase_problem(tmp_path / "enc", plain.public)
    (tmp_path / "lonely.pub").write_text(enc)
    assert "no private key file" in custody.passphrase_problem(tmp_path / "lonely.pub", enc)


# --- the custody a CLI act declares ---------------------------------------------------------------------


def test_declared_custody_refusals(tmp_path):
    plain = RealKey(tmp_path, "plain")
    with pytest.raises(KeyError_, match="custody touch-id needs a key of type sk-"):
        custody.declared_custody(plain.path, "touch-id", "laptop-presence", plain.public, platform="linux")
    with pytest.raises(KeyError_, match="is not encrypted"):
        custody.declared_custody(plain.path, "passphrase", "laptop-presence", plain.public)
    with pytest.raises(KeyError_, match="fido-attested is refused until attestation at enrol.*governed install"):
        custody.declared_custody(plain.path, "fido-attested", "laptop-presence", plain.public)
    with pytest.raises(KeyError_, match="a laptop-presence key must state its custody"):
        custody.declared_custody(plain.path, "unstated", "laptop-presence", plain.public)
    with pytest.raises(KeyError_, match="a laptop-presence key states its custody: pass --custody"):
        custody.declared_custody(plain.path, None, "laptop-presence", plain.public)
    assert custody.declared_custody(plain.path, None, "display-phone", plain.public) == "unstated"


def test_declared_custody_defaults_from_the_sidecar_and_refuses_a_disagreement(tmp_path):
    kdir = tmp_path / "keys"
    made = custody.create_passphrase(kdir, "pp", ssh_keygen=_fake_keygen_making(tmp_path, "pw"), interactive=True)
    assert custody.declared_custody(made.path, None, "laptop-presence", made.public_key) == "passphrase"
    assert custody.declared_custody(made.public_path, None, "laptop-presence", made.public_key) == "passphrase"
    with pytest.raises(KeyError_, match="--custody tpm disagrees with the key's sidecar"):
        custody.declared_custody(made.path, "tpm", "laptop-presence", made.public_key)


def test_a_sidecar_naming_an_unknown_provider_is_refused(tmp_path):
    k = RealKey(tmp_path, "k")
    (tmp_path / "k.levain-key.json").write_text(json.dumps({
        "format": "levain-key/1", "custody": "touch-id", "provider": "/tmp/evil.dylib", "ctk_hash": "A" * 40}))
    with pytest.raises(KeyError_, match="which levain does not load"):
        custody.read_sidecar(k.path)


def test_declared_touch_id_rechecks_the_ctk_identity_on_this_mac(tmp_path, monkeypatch, provider_present):
    """complement LOW-3 (L3 r1): ``platform`` reaches ctk_status, so the re-check runs (against the fakes)
    on any host; the host here is made to say linux, as CI's does."""
    mac = Mac(tmp_path, monkeypatch)
    made = mac.create(tmp_path / "keys")
    monkeypatch.setattr(custody.sys, "platform", "linux")
    assert custody.declared_custody(made.path, None, "laptop-presence", made.public_key, sc_auth=mac.sc_auth,
                                    platform="darwin") == "touch-id"
    db = mac.load()
    db["identities"][-1]["prot"] = "none"
    mac.db = db
    mac.save()
    with pytest.raises(KeyError_, match="protection none, not bio"):
        custody.declared_custody(made.path, None, "laptop-presence", made.public_key, sc_auth=mac.sc_auth,
                                 platform="darwin")
    db["identities"].pop()
    mac.db = db
    mac.save()
    with pytest.raises(KeyError_, match="no CTK identity for it on this Mac"):
        custody.declared_custody(made.path, None, "laptop-presence", made.public_key, sc_auth=mac.sc_auth,
                                 platform="darwin")


@pytest.mark.skipif(sys.platform != "darwin", reason="the CTK re-check runs on a Mac only")
def test_keys_listing_rechecks_touch_id_rows(tmp_path, monkeypatch, provider_present):
    from levain.cockpit.keys import KeyRow
    mac = Mac(tmp_path, monkeypatch)
    made = mac.create(tmp_path / "keys")
    rows = [KeyRow("SHA256:t", "kt", "laptop-presence", 1, None, None, None, custody="touch-id",
                   public_key=made.public_key),
            KeyRow("SHA256:p", "kp", "laptop-presence", 2, None, None, None, custody="passphrase",
                   public_key="ssh-ed25519 AAAA")]
    assert custody.custody_note(rows, sc_auth=mac.sc_auth) == {"SHA256:t": "CTK identity on this Mac, protection bio"}
    db = mac.load()
    db["identities"][-1]["prot"] = "none"
    mac.db = db
    mac.save()
    assert custody.custody_note(rows, sc_auth=mac.sc_auth)["SHA256:t"].startswith("WARNING: ")
    assert custody.custody_note(rows, sc_auth=mac.sc_auth, platform="linux") == {}


# --- ssh_signer: the provider and the agent --------------------------------------------------------------


def _capture_sign(monkeypatch) -> list[dict]:
    import levain.cockpit.keys as keys_mod
    seen: list[dict] = []

    def run(argv, **kw):
        seen.append(kw.get("env") or {})
        return subprocess.CompletedProcess(argv, 0, b"-----BEGIN SSH SIGNATURE-----\n", b"")
    monkeypatch.setattr(keys_mod.subprocess, "run", run)
    # these tests read the env sign hands ssh-keygen; the fake's output is no signature, so the
    # signing-time self-check (tested on its own below) is stubbed here
    monkeypatch.setattr(keys_mod._authority, "verify_link_sig", lambda *a, **kw: True)
    return seen


def test_ssh_signer_sets_the_sidecar_s_provider_and_an_explicit_one_wins(tmp_path, monkeypatch):
    import levain.cockpit.keys as keys_mod
    k = RealKey(tmp_path, "k")
    (tmp_path / "k.levain-key.json").write_text(json.dumps({
        "format": "levain-key/1", "custody": "touch-id", "provider": "/usr/lib/ssh-keychain.dylib",
        "ctk_hash": "A" * 40}))
    fp, sign = keys_mod.ssh_signer(k.path)
    seen = _capture_sign(monkeypatch)
    monkeypatch.delenv("SSH_SK_PROVIDER", raising=False)
    monkeypatch.setenv("SSH_AUTH_SOCK", "/tmp/agent.sock")
    sign(b"m")
    assert seen[-1]["SSH_SK_PROVIDER"] == "/usr/lib/ssh-keychain.dylib"
    assert seen[-1]["SSH_AUTH_SOCK"] == "/tmp/agent.sock"
    monkeypatch.setenv("SSH_SK_PROVIDER", "/opt/mine.dylib")
    sign(b"m")
    assert seen[-1]["SSH_SK_PROVIDER"] == "/opt/mine.dylib"


def test_ssh_signer_signs_a_passphrase_key_from_the_file_with_no_agent(tmp_path, monkeypatch):
    import levain.cockpit.keys as keys_mod
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "s3cret", "-f", str(tmp_path / "enc")], check=True,
                   capture_output=True, timeout=30)
    plain = RealKey(tmp_path, "plain")
    monkeypatch.setenv("SSH_AUTH_SOCK", "/tmp/agent.sock")
    _, enc_sign = keys_mod.ssh_signer(tmp_path / "enc")
    _, plain_sign = keys_mod.ssh_signer(plain.path)
    seen = _capture_sign(monkeypatch)
    enc_sign(b"m")
    plain_sign(b"m")
    assert "SSH_AUTH_SOCK" not in seen[0] and "SSH_SK_PROVIDER" not in seen[0]
    assert seen[1]["SSH_AUTH_SOCK"] == "/tmp/agent.sock"            # an unencrypted key may be agent-held


# --- ssh_signer: a signature the key did not make (a cancelled Touch ID prompt) --------------------------
# Measured 2026-10-12 on Phill's Mac (desk-driven run, rev 6 presence): cancelling the Touch ID prompt let
# `ssh-keygen -Y sign` exit 0 with output that is not a signature by the key, and the refusal surfaced only
# at the log's replay as "the signature does not verify". Phill ruled the wording a fix. The fake below
# makes a REAL SSHSIG with a different key, so the verifier is the real one.


def _signs_with(monkeypatch, other: RealKey) -> None:
    import levain.cockpit.keys as keys_mod
    real_run = subprocess.run

    def run(argv, **kw):
        argv = list(argv)
        if "-Y" in argv and argv[argv.index("-Y") + 1] == "sign":
            argv[argv.index("-f") + 1] = str(other.path)
            kw["env"] = {k: v for k, v in (kw.get("env") or {}).items() if k != "SSH_SK_PROVIDER"}
        return real_run(argv, **kw)
    monkeypatch.setattr(keys_mod.subprocess, "run", run)


def test_a_touch_id_signature_the_key_did_not_make_reads_as_no_presence_not_a_bad_signature(tmp_path, monkeypatch):
    import levain.cockpit.keys as keys_mod
    k, other = RealKey(tmp_path, "k"), RealKey(tmp_path, "other")
    (tmp_path / "k.levain-key.json").write_text(json.dumps({
        "format": "levain-key/1", "custody": "touch-id", "provider": "/usr/lib/ssh-keychain.dylib",
        "ctk_hash": "A" * 40}))
    _, sign = keys_mod.ssh_signer(k.path)
    _signs_with(monkeypatch, other)
    with pytest.raises(KeyError_) as err:
        sign(b"m")
    msg = str(err.value)
    assert "Touch ID" in msg and "cancelled" in msg and k.fingerprint in msg
    assert "does not verify" not in msg


def test_a_hand_made_touch_id_key_with_no_sidecar_is_named_by_its_provider(tmp_path, monkeypatch):
    # L3 r1 (codex MED): a hand-made Secure Enclave key signs through SSH_SK_PROVIDER with no sidecar
    import levain.cockpit.keys as keys_mod
    from levain.cockpit.custody import APPLE_PROVIDER
    k, other = RealKey(tmp_path, "k"), RealKey(tmp_path, "other")
    monkeypatch.setenv("SSH_SK_PROVIDER", APPLE_PROVIDER)
    _, sign = keys_mod.ssh_signer(k.path)
    _signs_with(monkeypatch, other)
    with pytest.raises(KeyError_) as err:
        sign(b"m")
    assert "Touch ID" in str(err.value) and "cancelled" in str(err.value)


def test_any_key_whose_signature_is_not_its_own_is_refused_at_signing(tmp_path, monkeypatch):
    import levain.cockpit.keys as keys_mod
    k, other = RealKey(tmp_path, "k"), RealKey(tmp_path, "other")
    _, sign = keys_mod.ssh_signer(k.path)
    _signs_with(monkeypatch, other)
    with pytest.raises(KeyError_) as err:
        sign(b"m")
    assert "no valid signature" in str(err.value) and "Touch ID" not in str(err.value)


def test_a_good_signature_still_passes_the_signing_time_check(tmp_path):
    import levain.cockpit.keys as keys_mod
    from levain.authority import verify_link_sig
    k = RealKey(tmp_path, "k")
    fp, sign = keys_mod.ssh_signer(k.path)
    sig = sign(b"m")
    assert verify_link_sig(b"m", sig, public_key=k.public, fingerprint=fp)


# --- the CLI ---------------------------------------------------------------------------------------------


def _initialised(tmp: Path) -> tuple[Path, RealKey]:
    from levain.cockpit.firejournal import CockpitDB
    root = tmp / "cockpit"
    root.mkdir()
    CockpitDB(root).set_install_class("reported")
    laptop = RealKey(tmp, "laptop")
    genesis_enrol(CockpitDB(root), laptop, "laptop-presence", now=NOW)
    return root, laptop


@pytest.mark.parametrize("extra, err", [
    (("--custody", "touch-id"), "custody touch-id needs a key of type sk-"),
    (("--custody", "passphrase"), "is not encrypted"),
    (("--custody", "fido-attested"), "fido-attested is refused until attestation at enrol"),
    (("--custody", "unstated"), "a laptop-presence key must state its custody"),
    ((), "a laptop-presence key states its custody: pass --custody"),
])
def test_the_cli_refuses_each_bad_custody_before_signing(tmp_path, monkeypatch, capsys, extra, err):
    from levain import cli
    from levain.cockpit import install
    import levain.cockpit.keys as keys_mod

    monkeypatch.setattr(install, "marker_dir", lambda: tmp_path / "m")
    root, laptop = _initialised(tmp_path)
    new = RealKey(tmp_path, "new")
    signed = []
    real = keys_mod.ssh_signer
    monkeypatch.setattr(keys_mod, "ssh_signer", lambda *a, **kw: signed.append(1) or real(*a, **kw))
    rc = cli.main(["cockpit", "enroll", str(new.path) + ".pub", "--signing-key", str(laptop.path), *extra,
                   "--data-root", str(root)])
    assert rc == 1 and err in capsys.readouterr().err
    assert signed == []


def test_the_cli_lists_custody_beside_the_class(tmp_path, monkeypatch, capsys):
    from levain import cli
    from levain.cockpit import install

    monkeypatch.setattr(install, "marker_dir", lambda: tmp_path / "m")
    root, laptop = _initialised(tmp_path)
    phone = RealKey(tmp_path, "phone")
    assert cli.main(["cockpit", "enroll", str(phone.path) + ".pub", "--class", "display-phone", "--signing-key",
                     str(laptop.path), "--data-root", str(root)]) == 0
    assert "as display-phone, custody unstated, at seq 2" in capsys.readouterr().out
    assert cli.main(["cockpit", "keys", "--data-root", str(root)]) == 0
    out = capsys.readouterr().out.splitlines()
    assert "laptop-presence  custody passphrase" in out[0] and "display-phone    custody unstated" in out[1]


def test_key_create_has_no_option_for_a_no_touch_key(capsys):
    from levain import cli
    with pytest.raises(SystemExit):
        cli.main(["cockpit", "key", "create", "--help"])
    help_text = capsys.readouterr().out
    assert "{touch-id,passphrase}" in help_text and "none" not in help_text.replace("nothing", "")
    with pytest.raises(SystemExit):
        cli.main(["cockpit", "key", "create", "--custody", "fido"])


# --- key create's default custody (DESIGN §3.2 rev 6, L3 r1 P6) ---------------------------------------

# `bioutil -r` on the build Mac (macOS 26.6.2, Touch ID enrolled), verbatim
_BIOUTIL_ON = """User Touch ID configuration:
\tBiometrics for unlock: 1
\tBiometrics for ApplePay: 1
\tEffective biometrics for unlock: 1
\tEffective biometrics for ApplePay: 1
Operation performed successfully.
"""


def _bioutil(tmp: Path, text: str, code: int = 0) -> Path:
    path = tmp / "bioutil"
    path.write_text(f"#!{PY} -I\nimport sys\nopen({str(tmp / 'bioutil.ran')!r}, 'a').write(' '.join(sys.argv[1:]) + '\\n')\n"
                    f"sys.stdout.write({text!r})\nsys.exit({code})\n")
    path.chmod(0o755)
    return path


def _default(tmp: Path, text: str = _BIOUTIL_ON, code: int = 0, platform: str = "darwin"):
    present = tmp / "present"
    present.write_text("")
    return custody.default_custody(platform=platform, bioutil=_bioutil(tmp, text, code), sc_auth=present,
                                   provider=present)


def test_key_create_defaults_to_touch_id_on_a_mac_with_touch_id_configured(tmp_path):
    custody_, why = _default(tmp_path)
    assert custody_ == "touch-id" and "Touch ID is configured" in why and "bioutil -r" in why
    assert (tmp_path / "bioutil.ran").read_text() == "-r\n"


@pytest.mark.parametrize("text,code,platform,says", [
    (_BIOUTIL_ON.replace("for unlock: 1", "for unlock: 0", 1), 0, "darwin", "not configured for unlock"),
    (_BIOUTIL_ON.replace("Effective biometrics for unlock: 1", "Effective biometrics for unlock: 0"), 0, "darwin",
     "not in effect"),
    ("", 1, "darwin", "could not read"),
    ("something else entirely\n", 0, "darwin", "could not read"),
])
def test_key_create_defaults_to_passphrase_when_touch_id_is_not_measured(tmp_path, text, code, platform, says):
    custody_, why = _default(tmp_path, text, code, platform)
    assert custody_ == "passphrase" and says in why


def test_off_macos_the_default_is_passphrase_and_nothing_is_run(tmp_path):
    custody_, why = _default(tmp_path, platform="linux")
    assert custody_ == "passphrase" and "macOS only" in why
    assert not (tmp_path / "bioutil.ran").exists()


def test_key_create_with_no_custody_says_what_it_chose_and_why(tmp_path, monkeypatch, capsys):
    from levain import cli
    called = []
    monkeypatch.setattr(custody, "default_custody", lambda: ("touch-id", "WHY-MEASURED"))

    def fake_create(d, label):
        called.append((d, label))
        raise KeyError_("stopped here by the test")
    monkeypatch.setattr(custody, "create_touch_id", fake_create)
    assert cli.main(["cockpit", "key", "create", "--dir", str(tmp_path / "k"), "--label", "x"]) == 1
    out = capsys.readouterr()
    assert "custody: touch-id (WHY-MEASURED; pass --custody to choose)" in out.out
    assert called == [(tmp_path / "k", "x")] and "stopped here by the test" in out.err


def test_the_custody_guarantee_lines_match_the_changelog():
    from levain import cli
    changelog = (Path(__file__).resolve().parents[1] / "CHANGELOG.md").read_text()
    assert "a fingerprint per signature" in cli._GUARANTEE["touch-id"]
    assert "a fingerprint per signature on a key that cannot leave the Secure Enclave" in changelog
    assert "a typed passphrase per signature" in changelog and "no defence against same-uid malware" in changelog

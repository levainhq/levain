"""The real terminal key is reached only by a process a levain launcher marked live (board levain#237).

Before: ``keys.pwd_key_dir()`` reads the password database's home, so a bare child python (no pytest, so neither of the
in-process walls) calling ``terminal_sign.sign(msg)`` with no ``key_dir`` found the operator's key, spawned a real
``ssh-keygen -Y sign`` and wrote evidence beside it. Now ``refuse_real_key_unless_live`` stops it first.

NOTHING here touches the operator's key or ``~/.levain``. Each child runs with a scratch HOME and LEVAIN_HOME and an
explicit environment, and before it imports levain it points the password database's home at a directory under
``tmp_path`` (``pwd.getpwuid``), so "the real key directory" it resolves is a stand-in holding a scratch ed25519 key.
A stub ssh-keygen under ``tmp_path`` is first on the child's PATH, and the child records every spawn in a marker file
(``subprocess.run`` wrapped in the child), so a spawn is visible whichever way the binary is named. Whatever
runs, it runs on the stand-in's scratch key, so even the old code, which these tests fail on, never reaches the
operator's key.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

import levain
from levain import home_guard
from levain.cockpit import keys as K
from levain.cockpit.custody import APPLE_PROVIDER, SIDECAR_FORMAT, SIDECAR_SUFFIX
from levain.consent import terminal_sign as ts

LEVAIN_ROOT = Path(levain.__file__).resolve().parent.parent      # the tree the test run imported, which the child imports

CHILD = '''
import pwd
import sys
from pathlib import Path

account, marker, mode = sys.argv[1], sys.argv[2], sys.argv[3]
_real = pwd.getpwuid


def _getpwuid(uid):
    e = _real(uid)
    return pwd.struct_passwd((e.pw_name, e.pw_passwd, e.pw_uid, e.pw_gid, e.pw_gecos, account, e.pw_shell))


pwd.getpwuid = _getpwuid        # the account's home is the test's stand-in, so nothing here can be the operator's
from levain.cockpit import keys as K
from levain.consent import terminal_sign as ts

import subprocess
_run = subprocess.run


def _recording_run(argv, *a, **kw):
    """Every spawn is written to the marker first, then runs the system ssh-keygen on the stand-in's scratch key. A stub
    first on PATH sees only a spawn by bare name; this sees a spawn by any path."""
    with open(marker, "a") as fh:
        fh.write(" ".join(map(str, argv)) + "\\n")
    return _run(argv, *a, **kw)


subprocess.run = _recording_run
if mode == "signer":
    K.ssh_signer(Path(account) / ".levain" / "keys" / ts.KEY_LABEL)
    sys.exit(0)
if mode == "factory":
    ts.default_signer_factory(Path(account) / ".levain" / "keys" / ts.KEY_LABEL, 5.0)
    sys.exit(0)
if mode == "sign-failure":
    K._write_sign_failure(Path(account) / ".levain" / "keys" / ts.KEY_LABEL, fp="SHA256:x", message=b"x", signature="",
                          sig_type="x", flags=0, counter=1, detail="d", namespace="n", attempt=1, elapsed=0.0)
    sys.exit(0)
out = ts.sign(b"x", key_dir=Path(sys.argv[4])) if mode == "sign-keydir" else ts.sign(b"x")
print(out.state, out.reason)
if out.state != "signed":
    sys.stderr.write(out.detail + "\\n")
    sys.exit(3)
'''


def _scratch_key(d: Path) -> Path:
    d.mkdir(parents=True, mode=0o700, exist_ok=True)
    d.chmod(0o700)
    path = d / ts.KEY_LABEL
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "scratch", "-f", str(path)], check=True)
    (d / (ts.KEY_LABEL + SIDECAR_SUFFIX)).write_text(json.dumps(
        {"format": SIDECAR_FORMAT, "custody": "touch-id", "provider": APPLE_PROVIDER, "ctk_hash": "A" * 40,
         "label": ts.KEY_LABEL}))
    return path


class Rig:
    """A stand-in account under ``tmp_path``: its ``.levain/keys`` holds a scratch key, ``marker`` records every spawn the
    child makes (and every call of the stub on PATH), and ``run`` starts a bare child python with an explicit environment."""

    def __init__(self, tmp_path: Path) -> None:
        self.tmp = tmp_path
        self.account = tmp_path / "acct"
        self.keys = self.account / ".levain" / "keys"
        self.key = _scratch_key(self.keys)
        self.marker = tmp_path / "stub-called"
        self.stub = tmp_path / "bin" / "ssh-keygen"
        self.stub.parent.mkdir()
        self.stub.write_text(f'#!/bin/sh\necho "$@" >> {self.marker}\nexec /usr/bin/ssh-keygen "$@"\n')
        self.stub.chmod(0o755)
        self.script = tmp_path / "bare_child.py"
        self.script.write_text(CHILD, encoding="utf-8")
        self.home = tmp_path / "scratch-home"
        self.home.mkdir()

    def run(self, mode: str, *, live: bool = False, extra: str = ""):
        env = {"PATH": f"{self.stub.parent}:/usr/bin:/bin", "HOME": str(self.home),
               "LEVAIN_HOME": str(self.home / "lh"), "PYTHONPATH": str(LEVAIN_ROOT)}
        assert "PYTEST_CURRENT_TEST" not in env
        if live:
            env[home_guard.LIVE_ENV] = "1"
        argv = [sys.executable, str(self.script), str(self.account), str(self.marker), mode] + ([extra] if extra else [])
        return subprocess.run(argv, env=env, capture_output=True, text=True, timeout=120, cwd=str(self.tmp))

    def failures(self) -> Path:
        return self.keys / "sign-failures"


@pytest.fixture
def rig(tmp_path):
    return Rig(tmp_path)


# --- (a) the named test: an unmarked bare child is refused before anything is spawned or written ------------------------

def test_a_bare_child_not_marked_live_cannot_sign_with_the_real_terminal_key(rig):
    done = rig.run("sign")
    assert done.returncode != 0, done.stdout
    assert "RealKeyUnmarked" not in done.stdout
    assert "operator's real key" in done.stderr and "mark_live" in done.stderr, done.stderr   # the refusal names its fix
    assert "the real key directory" in done.stderr       # refused where the default key directory is picked, before any look at it
    assert str(rig.keys) in done.stderr
    assert not rig.marker.exists(), "an ssh-keygen was spawned"
    assert not rig.failures().exists()


def test_the_refusal_is_a_traceback_that_names_the_cause_from_ssh_signer(rig):
    done = rig.run("signer")
    assert done.returncode != 0
    assert "RealKeyUnmarked" in done.stderr and "mark_live" in done.stderr, done.stderr
    assert not rig.marker.exists() and not rig.failures().exists()


def test_the_signer_factory_door_refuses_too(rig):
    done = rig.run("factory")
    assert done.returncode != 0 and "RealKeyUnmarked" in done.stderr, done.stderr
    assert not rig.marker.exists() and not rig.failures().exists()


def test_an_unmarked_child_cannot_write_into_the_real_sign_failures_directory(rig):
    done = rig.run("sign-failure")
    assert done.returncode != 0 and "RealKeyUnmarked" in done.stderr, done.stderr
    assert not rig.failures().exists()


# --- (b) marked live: the opt-in works --------------------------------------------------------------------------------

def test_a_child_marked_live_signs_with_a_key_dir_under_tmp_through_the_stub(rig, tmp_path):
    other = _scratch_key(tmp_path / "other-keys")
    done = rig.run("sign-keydir", live=True, extra=str(other.parent))
    assert done.returncode == 0, done.stderr
    assert done.stdout.split() == ["signed", "None"]
    assert rig.marker.exists() and "-Y sign" in rig.marker.read_text()


def test_a_child_marked_live_signs_with_the_real_key_directory_it_finds_by_default(rig):
    done = rig.run("sign", live=True)
    assert done.returncode == 0, done.stderr
    assert done.stdout.split() == ["signed", "None"]
    assert rig.marker.exists() and "-Y sign" in rig.marker.read_text()


def test_an_unmarked_child_with_a_key_dir_outside_the_real_one_still_signs(rig, tmp_path):
    other = _scratch_key(tmp_path / "other-keys")
    done = rig.run("sign-keydir", extra=str(other.parent))
    assert done.returncode == 0, done.stderr
    assert rig.marker.exists()


# --- (c) the evidence writer, in process ------------------------------------------------------------------------------

def _write(key_path: Path) -> str:
    return K._write_sign_failure(key_path, fp="SHA256:x", message=b"x", signature="", sig_type="x", flags=0, counter=1,
                                 detail="d", namespace="n", attempt=1, elapsed=0.0)


def test_write_sign_failure_refuses_the_real_key_dir_when_unmarked_and_writes_elsewhere(tmp_path, monkeypatch):
    real = tmp_path / "acct" / ".levain" / "keys"
    real.mkdir(parents=True, mode=0o700)
    monkeypatch.setattr(K, "pwd_key_dir", lambda: real)
    monkeypatch.delenv(home_guard.LIVE_ENV, raising=False)
    with pytest.raises(K.RealKeyUnmarked, match="mark_live"):
        _write(real / ts.KEY_LABEL)
    assert not (real / "sign-failures").exists()
    other = tmp_path / "other-keys"
    other.mkdir(mode=0o700)
    written = _write(other / ts.KEY_LABEL)
    assert Path(written).parent == other / "sign-failures" and Path(written).is_file()


def test_write_sign_failure_in_the_real_key_dir_is_allowed_when_marked_live(tmp_path, monkeypatch):
    real = tmp_path / "acct" / ".levain" / "keys"
    real.mkdir(parents=True, mode=0o700)
    monkeypatch.setattr(K, "pwd_key_dir", lambda: real)
    monkeypatch.setenv(home_guard.LIVE_ENV, "1")
    assert Path(_write(real / ts.KEY_LABEL)).is_file()


def test_refuse_real_key_unless_live_judges_the_resolved_path(tmp_path, monkeypatch):
    real = tmp_path / "acct" / ".levain" / "keys"
    real.mkdir(parents=True, mode=0o700)
    link = tmp_path / "link"
    link.symlink_to(real)
    monkeypatch.setattr(K, "pwd_key_dir", lambda: real)
    monkeypatch.delenv(home_guard.LIVE_ENV, raising=False)
    for path in (real, real / ts.KEY_LABEL, link / ts.KEY_LABEL, real / ".." / "keys" / ts.KEY_LABEL):
        with pytest.raises(K.RealKeyUnmarked):
            K.refuse_real_key_unless_live(path)
    K.refuse_real_key_unless_live(tmp_path / "elsewhere" / ts.KEY_LABEL)
    K.refuse_real_key_unless_live(real.parent / "keys-sibling" / ts.KEY_LABEL)
    monkeypatch.setenv(home_guard.LIVE_ENV, "1")
    K.refuse_real_key_unless_live(real / ts.KEY_LABEL)


def test_the_refusal_is_a_real_key_reached_and_a_key_error():
    assert issubclass(K.RealKeyUnmarked, K.RealKeyReached) and issubclass(K.RealKeyUnmarked, K.KeyError_)

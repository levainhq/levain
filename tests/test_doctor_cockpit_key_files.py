"""`levain doctor` finds an active laptop-presence key enrolled as ``passphrase`` whose local key file is an
unencrypted OpenSSH key (an install enrolled before custody checks existed). Keys here are generated in a temp dir."""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from levain.cockpit.firejournal import CockpitDB
from levain.doctor import _check_cockpit_key_files
from tests.cockpit_authority_rig import RealKey, enrol

pytestmark = pytest.mark.skipif(shutil.which("ssh-keygen") is None, reason="needs ssh-keygen")


def _install(tmp: Path, key: RealKey) -> Path:
    root = tmp / "inst" / ".levain" / "cockpit"
    root.mkdir(parents=True)
    db = CockpitDB(root)
    db.set_install_class("reported")
    enrol(db, key, "laptop-presence")             # declared passphrase
    return tmp / "inst"


def _encrypt(key: RealKey) -> None:
    subprocess.run(["ssh-keygen", "-q", "-p", "-N", "a-test-passphrase", "-f", str(key.path)],
                   check=True, capture_output=True, timeout=30)


def test_unencrypted_file_declared_passphrase_fails_with_fingerprint_and_remedy(tmp_path):
    keys = tmp_path / "keys"
    keys.mkdir()
    k = RealKey(keys, "laptop")
    [r] = _check_cockpit_key_files(_install(tmp_path, k), key_dirs=[keys])
    assert not r.ok and k.fingerprint in r.detail and str(k.path) in r.detail
    assert "levain cockpit key create" in r.hint and "levain cockpit enroll" in r.hint
    assert f"levain cockpit revoke {k.fingerprint} --signing-key" in r.hint
    assert "--compromised-since would void" in r.hint


def test_encrypted_file_passes(tmp_path):
    keys = tmp_path / "keys"
    keys.mkdir()
    k = RealKey(keys, "laptop")
    install = _install(tmp_path, k)               # enrolled while the file was plain: the harness signs with it
    _encrypt(k)
    [r] = _check_cockpit_key_files(install, key_dirs=[keys])
    assert r.ok and not r.warn


def test_no_local_file_is_no_failure(tmp_path):
    keys = tmp_path / "keys"
    keys.mkdir()
    k = RealKey(tmp_path, "elsewhere")
    empty = tmp_path / "empty"
    empty.mkdir()
    assert _check_cockpit_key_files(_install(tmp_path, k), key_dirs=[empty]) == []
    assert _check_cockpit_key_files(tmp_path / "no-install", key_dirs=[keys]) == []

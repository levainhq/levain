"""`levain doctor` finds an active laptop-presence key enrolled as ``passphrase`` whose local key file is an
unencrypted OpenSSH key (an install enrolled before custody checks existed). Keys here are generated in a temp dir."""
from __future__ import annotations

import os
import shlex
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
    subprocess.run(["ssh-keygen", "-q", "-p", "-P", "", "-N", "a-test-passphrase", "-f", str(key.path)],
                   check=True, capture_output=True, timeout=30, stdin=subprocess.DEVNULL)


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


def _copy_key(src: RealKey, dst_dir: Path, name: str) -> Path:
    dst_dir.mkdir(exist_ok=True)
    shutil.copy(src.path, dst_dir / name)
    shutil.copy(str(src.path) + ".pub", dst_dir / f"{name}.pub")
    return dst_dir / name


def test_encrypted_copy_first_does_not_shadow_an_unencrypted_copy(tmp_path):
    keys = tmp_path / "keys"
    keys.mkdir()
    k = RealKey(keys, "laptop")
    install = _install(tmp_path, k)
    plain = _copy_key(k, tmp_path / "zz", "z")           # a plain copy, sorted AFTER the encrypted original
    _encrypt(k)
    [r] = _check_cockpit_key_files(install, key_dirs=[keys, tmp_path / "zz"])
    assert not r.ok and str(plain) in r.detail and str(k.path) not in r.detail


def test_orphan_pub_first_does_not_hide_a_later_plain_file(tmp_path):
    keys = tmp_path / "keys"
    keys.mkdir()
    k = RealKey(keys, "laptop")
    install = _install(tmp_path, k)
    orphans = tmp_path / "a-orphans"
    orphans.mkdir()
    shutil.copy(str(k.path) + ".pub", orphans / "laptop.pub")      # a .pub with no private file beside it
    [r] = _check_cockpit_key_files(install, key_dirs=[orphans, keys])
    assert not r.ok and str(k.path) in r.detail


def test_a_different_key_beside_the_pub_is_not_this_keys_file(tmp_path):
    keys = tmp_path / "keys"
    keys.mkdir()
    k = RealKey(keys, "laptop")
    install = _install(tmp_path, k)
    other = RealKey(tmp_path, "other")                     # plain, but NOT the enrolled key
    shutil.copy(other.path, keys / "laptop")               # replaces the private file beside laptop.pub
    assert _check_cockpit_key_files(install, key_dirs=[keys]) == []
    _encrypt(other)
    shutil.copy(other.path, keys / "laptop")               # an encrypted stranger must not make a false OK either
    assert _check_cockpit_key_files(install, key_dirs=[keys]) == []


def test_remedy_targets_this_install_and_quotes_paths(tmp_path):
    keys = tmp_path / "key dir; $x"
    keys.mkdir()
    k = RealKey(keys, "laptop")
    install = _install(tmp_path, k)
    [r] = _check_cockpit_key_files(install, key_dirs=[keys])
    q = shlex.quote(str(install))
    assert r.hint.count(f"--path {q}") == 2
    assert f"--signing-key {shlex.quote(str(k.path))}" in r.hint
    assert shlex.quote(str(k.path)) != str(k.path)         # the unsafe name really needed quoting


def test_fifo_and_symlinked_pub_are_never_opened(tmp_path):
    keys = tmp_path / "keys"
    keys.mkdir()
    k = RealKey(keys, "laptop")
    install = _install(tmp_path, k)
    os.mkfifo(keys / "block.pub")                          # a read of this would hang forever
    (keys / "dev.pub").symlink_to("/dev/zero")
    (keys / "big.pub").write_bytes(b"x" * (64 * 1024))     # over the 16 KiB public-key cap
    os.mkfifo(keys / "laptop-fifo")                        # a FIFO where a private file should be
    shutil.copy(str(k.path) + ".pub", keys / "laptop-fifo.pub")
    [r] = _check_cockpit_key_files(install, key_dirs=[keys])
    assert not r.ok and str(k.path) in r.detail            # the real plain file is still found
    (keys / "laptop").unlink()
    (keys / "laptop.pub").unlink()
    assert _check_cockpit_key_files(install, key_dirs=[keys]) == []   # only the junk left: no row, no hang


def test_ok_row_counts_only_keys_it_checked(tmp_path):
    keys = tmp_path / "keys"
    keys.mkdir()
    k = RealKey(keys, "laptop")
    install = _install(tmp_path, k)
    _encrypt(k)
    [r] = _check_cockpit_key_files(install, key_dirs=[keys])
    assert r.ok and r.detail.startswith("1 local")

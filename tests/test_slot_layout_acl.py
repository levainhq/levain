"""1026+8 adoptfix L3 r1 (codex HIGH): setup and repair re-apply each layout row's owner and mode, and on macOS that
left an extended ACL in place. A numeric ``chmod`` does not touch the ACL, so a class dir an earlier install gave to
``_levain_export`` could keep an ``add_file`` / ``delete_child`` grant after it became ``root 0750``, and the
helper's trust check (owner and mode bits from ``fstat``) would accept it.

Reproduced first on a real directory with a real ACL (``chmod +a``), read back with ``ls -led``: on 625732ea the
row's steps left ``0: group:everyone allow add_file,delete_child`` on the dir (``write`` on a file).

Linux: the row steps clear no POSIX ACL. No install code reads or writes POSIX ACLs, and ``setfacl`` is not a tool
the install helper names (``layout._tools``), so a Linux row gets no ACL step.
"""
from __future__ import annotations

import grp
import os
import pwd
import subprocess
import sys

import pytest

from levain.slot import layout as LY
from levain.slot.ids import SlotId

ACL = "everyone allow add_file,delete_child"
darwin_only = pytest.mark.skipif(sys.platform != "darwin", reason="a macOS extended ACL needs macOS chmod +a")


def _ls(path) -> str:
    return subprocess.run(["/bin/ls", "-led", str(path)], check=True, capture_output=True, text=True).stdout


def _run(steps) -> None:
    """The plan's argv steps as this uid runs them (a ``write`` step's file is made by the caller first, so its
    ``skip_if`` skips it)."""
    for s in steps:
        if s.skip_if and subprocess.run(list(s.skip_if), capture_output=True).returncode == 0:
            continue
        assert s.argv, s
        subprocess.run(list(s.argv), check=True, capture_output=True)


def _me():
    return pwd.getpwuid(os.getuid()).pw_name, grp.getgrgid(os.getgid()).gr_name


@darwin_only
@pytest.mark.parametrize("kind", ["dir", "file"])
def test_the_row_steps_clear_an_extended_acl_on_macos(tmp_path, kind):
    p = tmp_path / ("export-operator" if kind == "dir" else "pool.lock")
    p.mkdir() if kind == "dir" else p.write_bytes(b"")
    subprocess.run(["/bin/chmod", "+a", ACL, str(p)], check=True)
    assert " 0: " in _ls(p), _ls(p)                       # the ACL is really there before the steps
    owner, group = _me()
    row = LY.Row(p, owner, group, 0o750 if kind == "dir" else 0o600, kind)
    _run(LY.row_steps("darwin", [row]))
    out = _ls(p)
    assert " 0: " not in out and "allow" not in out, out
    assert (os.stat(p).st_mode & 0o7777) == row.mode


@darwin_only
def test_a_row_with_no_acl_still_applies(tmp_path):
    # chmod -N on a dir with no ACL exits 0, so a re-run of setup over a clean store does not stop
    p = tmp_path / "d"
    owner, group = _me()
    _run(LY.row_steps("darwin", [LY.Row(p, owner, group, 0o2750)]))
    assert (os.stat(p).st_mode & 0o7777) == 0o2750 and " 0: " not in _ls(p)


def _all_rows(host):
    slots = (SlotId("egress", 0), SlotId("none", 0))
    return (LY.base_store_rows(host, "op") + LY.slot_rows(host, slots) + LY.base_run_rows(host)
            + [LY.reserve_row(host)])


def test_every_darwin_row_clears_its_acl_after_the_chown_and_before_the_mode():
    """After the chown: before it, the old owner may still add an ACL entry (an owner always can), and that entry
    would outlive the chown. The mode stays last, so no step after it can clear a setgid bit."""
    for r in _all_rows("darwin"):
        argvs = [s.argv for s in LY.row_steps("darwin", [r]) if s.argv]
        p = str(r.path)
        chown = argvs.index(("/usr/sbin/chown", f"{r.owner}:{r.group}", p))
        clear = argvs.index(("/bin/chmod", "-N", p))
        mode = argvs.index(("/bin/chmod", f"{r.mode:04o}", p))
        assert chown < clear < mode == len(argvs) - 1, (p, argvs)


def test_no_linux_row_names_an_acl_step(monkeypatch):
    from levain.firing import hands
    monkeypatch.setattr(hands.shutil, "which", lambda n, path=None: f"/usr/bin/{n}")
    for r in _all_rows("linux"):
        argvs = [s.argv for s in LY.row_steps("linux", [r]) if s.argv]
        assert not [a for a in argvs if "-N" in a or "setfacl" in a[0]], argvs

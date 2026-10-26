"""3c-9 session B, rulings 1 and 2 of 1025-23/3c9a: ``R/control`` is setgid so CONTROL's socket takes ``levain-control``
(a host test of the mode and the gid), and keys exits EX_CONFIG on a missing account, which systemd does not restart."""
from __future__ import annotations

import os
import socket
import stat
from pathlib import Path

import pytest

from levain.broker import keys as K
from levain.slot import layout, service_boot, units
from levain.slot.services import keys as SK


def _control_row(host):
    return next(r for r in layout.base_run_rows(host) if r.path == layout.RUN[host] / "control")


@pytest.mark.parametrize("host", ["darwin", "linux"])
def test_r_control_is_setgid_levain_control(host):
    row = _control_row(host)
    assert (row.owner, row.group, row.mode) == ("_levain_control", "levain-control", 0o2750)
    if host == "linux":    # the boot-time copy keeps the setgid bit
        assert f"d {row.path} 2750 _levain_control levain-control -" in layout.tmpfiles_text(host, ())
    steps = layout.row_steps(host, [row])
    assert steps[-1].argv[-2:] == ("2750", str(row.path))   # the mode last, so no chown clears it


def test_a_socket_bound_in_a_setgid_dir_takes_the_dirs_group(tmp_path):
    """The host half of ruling 1: a socket CONTROL binds in a 2750 dir takes the dir's group, not its creator's primary
    group (Linux follows the setgid bit; BSD/macOS gives the directory's group to every new entry)."""
    others = [g for g in os.getgroups() if g != os.getgid()]
    if not others:
        pytest.skip("this uid is in no supplementary group to give the dir")
    import tempfile
    d = Path(tempfile.mkdtemp(prefix="c9b", dir="/tmp")) / "control"    # AF_UNIX paths are short
    d.mkdir()
    os.chown(d, -1, others[0])
    os.chmod(d, 0o2750)
    st = os.stat(d)
    assert stat.S_IMODE(st.st_mode) == 0o2750 and st.st_gid == others[0]
    path = str(d / "c.sock")
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        s.bind(path)
        assert os.stat(path).st_gid == others[0] != os.getgid()
    finally:
        s.close()
        os.unlink(path)
        os.rmdir(d)
        os.rmdir(d.parent)


def test_service_boot_accepts_the_setgid_control_dir():
    host = "linux"
    row = _control_row(host)

    class St:
        def __init__(self, uid, gid, mode):
            self.st_uid, self.st_gid, self.st_mode = uid, gid, stat.S_IFDIR | mode

    def lstat(p):
        return St(0, 0, 0o755) if p == str(layout.RUN[host]) else St(500, 501, row.mode)
    ok, why = service_boot.check_run_dir(host, "control", lstat=lstat, uid_of=lambda n: 500, gid_of=lambda n: 501)
    assert ok, why
    ok, why = service_boot.check_run_dir(host, "control", lstat=lambda p: St(0, 0, 0o755) if p == str(
        layout.RUN[host]) else St(500, 501, 0o750), uid_of=lambda n: 500, gid_of=lambda n: 501)
    assert not ok and "0750" in why     # a dir left at the old mode is named until setup or repair re-applies it


def test_keys_exits_ex_config_with_no_gw_account(monkeypatch, capsys):
    called = []
    monkeypatch.setattr(K, "main", lambda argv: called.append(argv) or 0)
    monkeypatch.setattr(K, "_gw_uids", lambda: frozenset())
    monkeypatch.setattr(K, "_account_uid", lambda name: 602)
    assert SK.main(["keys"], host="linux") == SK.EX_CONFIG == units.EX_CONFIG == 78
    assert called == [] and "no _levain_gw<n> account" in capsys.readouterr().err
    monkeypatch.setattr(K, "_gw_uids", lambda: frozenset({601}))
    monkeypatch.setattr(K, "_account_uid", lambda name: None)
    assert SK.main(["keys"], host="linux") == 78 and called == []
    monkeypatch.setattr(K, "_account_uid", lambda name: 602)
    assert SK.main(["keys"], host="linux") == 0 and len(called) == 1
    # the single-uid test form names no account, so keys' own refusals stand there
    monkeypatch.setattr(K, "_gw_uids", lambda: frozenset())
    assert SK.main(["keys", "--test-single-uid"], host="linux") == 0 and len(called) == 2


def test_systemd_does_not_restart_an_ex_config_exit():
    for svc in units.services_for("linux"):
        text = units.systemd_unit_text(svc)
        # every policy (always; dispatch's on-failure, §1.3) keeps EX_CONFIG down
        assert f"Restart={svc.restart}\n" in text and "RestartPreventExitStatus=78\n" in text, svc.name

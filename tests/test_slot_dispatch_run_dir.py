"""R/dispatch/ (S10b slice R): the dispatch run server's socket dir. Owner ``_levain_dispatch``, group
``levain-control`` (the operator is in it, no slot uid is), 0750 (setgid on Linux); every place that makes the R dirs
makes it the same way."""
from __future__ import annotations

import os
import stat

import pytest

from levain.slot import layout, reaper, service_boot, units
from levain.slot.ids import SlotId
from tests.test_slot_s2f2_runtime_helper import World

HOSTS = ["linux", "darwin"]


def _row(host):
    return next(r for r in layout.base_run_rows(host, "phill") if r.path == layout.RUN[host] / "dispatch")


@pytest.mark.parametrize("host", HOSTS)
def test_the_row_is_the_dispatch_account_in_group_control(host):
    row = _row(host)
    assert (row.owner, row.group, row.kind) == ("_levain_dispatch", "levain-control", "dir")
    assert row.mode == (0o2750 if host == "linux" else 0o750)
    paths = [r.path for r in layout.base_run_rows(host, "phill")]
    assert paths.index(layout.RUN[host]) < paths.index(row.path)          # parents first
    assert [r for r in layout.base_run_rows(host, None) if r.path == row.path] == [row]   # not operator-dependent


@pytest.mark.parametrize("host", HOSTS)
def test_the_service_checks_that_row(host):
    assert service_boot.RUN_DIR_OF["dispatch_run"] == "dispatch"
    assert service_boot.run_dir_row(host, "dispatch_run") == _row(host)


@pytest.mark.parametrize("host", HOSTS)
def test_install_steps_chown_then_mode(host):
    row = _row(host)
    argvs = [s.argv for s in layout.row_steps(host, [row]) if s.argv]
    p = str(row.path)
    assert any(a[0].endswith("mkdir") and a[-1] == p for a in argvs)
    chown = next(i for i, a in enumerate(argvs) if a[0].endswith("chown") and a[-2:] == ("_levain_dispatch:levain-control", p))
    assert argvs[-1][0].endswith("chmod") and argvs[-1][-2:] == (f"{row.mode:04o}", p) and chown < len(argvs) - 1


def test_tmpfiles_recreates_it_at_boot():
    text = layout.tmpfiles_text("linux", [SlotId("egress", 0)], "phill").splitlines()
    line = f"d {layout.RUN['linux']}/dispatch 2750 _levain_dispatch levain-control -"
    assert line in text
    assert text.index(f"d {layout.RUN['linux']} 0755 root root -") < text.index(line)


@pytest.mark.parametrize("host", HOSTS)
def test_the_unit_is_installed_and_started_by_name(host):
    name = "levain-dispatch_run.service" if host == "linux" else "com.levain.dispatch_run"
    assert name in units.start_order(host)
    order = units.start_order(host)
    other = "levain-dispatch.service" if host == "linux" else "com.levain.dispatch"
    assert order.index(other) < order.index(name)                         # start order: after dispatch
    steps = units.install_steps(host, units.services_for(host), run=lambda *a, **k: None, read=lambda p: None)
    assert any(name in repr(s) for s in steps)


@pytest.mark.parametrize("host", HOSTS)
def test_the_reaper_boot_pass_makes_it(tmp_path, host):
    w = World(tmp_path, host=host)
    w.seams.account_uid = lambda name: 800
    w.seams.group_gid = lambda g: 700
    rows = {r.path: r for r in reaper.run_rows(w.seams)}
    row = rows[w.R / "dispatch"]
    assert (row.owner, row.group) == ("_levain_dispatch", "levain-control")
    assert reaper.boot_dirs(w.seams, log=lambda m: None) == []
    assert (w.R / "dispatch").is_dir() and not (w.R / "dispatch").is_symlink()
    mode = stat.S_IMODE(os.lstat(w.R / "dispatch").st_mode)
    assert mode & 0o777 == row.mode & 0o777
    if host == "linux":
        assert mode & 0o2000 == 0o2000      # setgid kept: run.sock takes the dir's levain-control group


# --- a fresh setup: the run server starts before the operator record exists (L3 r1 codex H1) ---------------------------


def _fresh_linux_plan():
    from levain.firing import hands
    from levain.slot.grammar import parse_install_argv
    from levain.slot.install_helper import HostFacts, plan_setup
    real = hands.shutil.which
    hands.shutil.which = lambda n, path=None: real(n, path=path) or f"/usr/sbin/{n}"
    try:
        cmd = parse_install_argv(["setup", "--slots", "2", "--verifier-slots", "1", "--operator", "alice"])
        facts = HostFacts("linux", caller_uid=501, caller_name="alice", operator_uid=501,
                          tool_present=lambda p: True, xcode_ready=lambda: True)     # no operator record: a fresh host
        return plan_setup(cmd, facts, read_sysctl=lambda p: "1", program_exists=lambda p: True)
    finally:
        hands.shutil.which = real


def test_a_fresh_linux_setup_starts_the_run_server_before_it_writes_the_operator_record():
    steps = _fresh_linux_plan().steps
    start = next(i for i, s in enumerate(steps) if s.argv and s.argv[-2:] == ("--now", "levain-dispatch_run.service"))
    record = next(i for i, s in enumerate(steps) if s.write and s.write[0] == layout.operator_record_path("linux"))
    assert start < record          # the record is setup's LAST act (§23), so the server's first start finds none


def test_run_sock_is_group_writable_under_a_default_umask():
    """connect() needs write on the socket inode: a levain-control member reaches it through the dir's group (L3 r1 M1)."""
    import shutil
    import tempfile
    from pathlib import Path
    from levain.slot.services import dispatch_run as D
    short = Path(tempfile.mkdtemp(dir="/tmp"))      # AF_UNIX paths are short; pytest's macOS tmp_path is too long
    old = os.umask(0o022)
    try:
        sock = D._bind(short / "run.sock")
    finally:
        os.umask(old)
    try:
        assert stat.S_IMODE(os.lstat(short / "run.sock").st_mode) & 0o060 == 0o060
    finally:
        sock.close()
        shutil.rmtree(short)


def _unit_lines(svc):
    return dict(line.split("=", 1) for line in units.systemd_unit_text(svc).splitlines() if "=" in line)


def test_an_absent_operator_record_is_a_temporary_exit_the_unit_restarts(monkeypatch, tmp_path, capsys):
    import pwd
    from levain.slot.services import dispatch_run as D
    monkeypatch.setattr(D.pwd, "getpwnam", lambda n: pwd.struct_passwd((n, "x", os.geteuid(), 0, "", "/", "/bin/sh")))
    monkeypatch.setattr(D, "build_server", lambda *a, **k: pytest.fail("served without an operator uid"))
    rc = D.main(["--operator-record", str(tmp_path / "absent"), "--socket", str(tmp_path / "run.sock")])
    assert rc == 75                                                         # EX_TEMPFAIL
    assert "no-operator" in capsys.readouterr().err
    lines = _unit_lines(units.by_name("dispatch_run"))
    assert lines["Restart"] == "always" and str(rc) not in lines["RestartPreventExitStatus"].split()
    assert not (tmp_path / "run.sock").exists()


def test_a_malformed_operator_record_still_stops_the_unit(monkeypatch, tmp_path, capsys):
    import pwd
    from levain.slot.services import dispatch_run as D
    monkeypatch.setattr(D.pwd, "getpwnam", lambda n: pwd.struct_passwd((n, "x", os.geteuid(), 0, "", "/", "/bin/sh")))
    monkeypatch.setattr(D, "build_server", lambda *a, **k: pytest.fail("served without an operator uid"))
    (tmp_path / "bad").write_text("{}")
    assert D.main(["--operator-record", str(tmp_path / "bad"), "--socket", str(tmp_path / "run.sock")]) == D.EX_CONFIG
    assert "operator-record-malformed" in capsys.readouterr().err

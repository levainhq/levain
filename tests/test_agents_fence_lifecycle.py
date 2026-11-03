"""The agent fence's lifecycle (P7-2 wiring r1, codex HIGH): `levain agents fence start|status` and `levain update`
install the operator's per-user fence unit through the provider every `levain daemon install*` verb uses. A fake
provider records what is asked of it; one test drives the real launchd provider with launchctl recorded, not run."""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

import pytest

from levain import daemon
from levain.agents import cli as A
from levain.qbroker import fence_server as FS
from levain.slot import layout


class FakeProvider:
    def __init__(self) -> None:
        self.installed: list[daemon.DaemonSpec] = []
        self.on_disk: list[str] | None = None     # the argv of the unit already installed (None: no unit)

    def install(self, spec: daemon.DaemonSpec) -> str:
        self.installed.append(spec)
        self.on_disk = list(spec.argv)
        return f"installed {spec.label}\n  status: running"

    def installed_argv(self, label: str) -> list[str] | None:
        return self.on_disk

    def status(self, label: str) -> daemon.DaemonStatus:
        return daemon.DaemonStatus(installed=bool(self.installed), running=bool(self.installed), detail="fake")


@pytest.fixture
def host(tmp_path, monkeypatch):
    """An isolation install's L (with policy/) and R under tmp, as this host's layout names them."""
    L, R = tmp_path / "L", tmp_path / "R"
    (L / "policy").mkdir(parents=True)
    R.mkdir()
    h = "darwin" if sys.platform == "darwin" else "linux"
    monkeypatch.setattr(layout, "STORE", {h: L})
    monkeypatch.setattr(layout, "RUN", {h: R})
    monkeypatch.setenv("LEVAIN_HOME", str(tmp_path / "home"))
    install = tmp_path / "entity"
    install.mkdir()
    provider = FakeProvider()
    monkeypatch.setattr(daemon, "select_provider", lambda system=None: provider)
    return argparse.Namespace(L=L, R=R, install=install, provider=provider, tmp=tmp_path)


def _start(host, capsys):
    rc = A.run_fence_start(argparse.Namespace(path=host.install, store_dir=None))
    return rc, capsys.readouterr()


def test_start_installs_the_resident_unit_with_this_hosts_R(host, capsys):
    rc, out = _start(host, capsys)
    assert rc == 0, out.err
    [spec] = host.provider.installed
    assert spec.label == daemon.DEFAULT_FENCE_LABEL
    # resident: the provider starts it now (launchd kickstart, systemd enable --now) and at login, and keeps it alive
    assert spec.start_interval is None and spec.keep_alive and spec.run_at_login
    args = FS.parse_args(spec.argv[3:])
    assert Path(args.run) == host.R
    assert Path(args.agent_store) == (host.tmp / "home" / "autonomic").resolve()
    assert Path(args.cockpit_root) == (host.install / ".levain" / "cockpit").resolve()
    assert "installed " + daemon.DEFAULT_FENCE_LABEL in out.out


def test_a_second_start_installs_the_same_unit_again(host, capsys):
    assert _start(host, capsys)[0] == 0
    assert _start(host, capsys)[0] == 0
    first, second = host.provider.installed
    assert first == second            # the provider's install is idempotent for an unchanged spec


def test_start_refuses_a_host_without_the_isolation_install(host, capsys):
    (host.L / "policy").rmdir()
    rc, out = _start(host, capsys)
    assert rc == 1 and "no isolation install" in out.err and host.provider.installed == []


def test_start_takes_the_store_dir_given_to_agents(host, capsys):
    assert A.run_fence_start(argparse.Namespace(path=host.install, store_dir=host.tmp / "other")) == 0
    args = FS.parse_args(host.provider.installed[0].argv[3:])
    assert Path(args.agent_store) == (host.tmp / "other").resolve()


def test_the_cli_parses_fence_start_and_status(host):
    from levain.cli import build_parser

    for verb, fn in (("start", A.run_fence_start), ("status", A.run_fence_status)):
        args = build_parser().parse_args(["agents", "fence", verb, "--path", str(host.install)])
        assert args.func is fn and args.path == host.install


def test_status_reports_the_unit_and_the_doctor_row(host, capsys, monkeypatch):
    from levain import doctor

    seen = {}

    def row(**kw):
        seen.update(kw)
        return [doctor.CheckResult("agent fence", False, "not running.", "Start it")]
    monkeypatch.setattr(doctor, "_check_agent_fence", row)
    rc = A.run_fence_status(argparse.Namespace(path=host.install, store_dir=None))
    out = capsys.readouterr().out
    assert rc == 1 and "installed=False" in out and "agent fence: not running." in out
    assert seen["expected"] == A.fence_roots(host.install)


# --- levain update ------------------------------------------------------------------------------------------------

def test_update_installs_the_fence_unit_and_a_second_update_is_idempotent(host, monkeypatch):
    from levain import band_hook, update

    (host.install / ".levain").mkdir()
    monkeypatch.setattr(update, "_run_update", lambda path, **kw: 0)
    monkeypatch.setattr(band_hook, "refresh_after_update", lambda: None)
    lines: list[str] = []
    assert update.run_update(host.install, emit=lines.append) == 0
    assert update.run_update(host.install, emit=lines.append) == 0
    first, second = host.provider.installed
    assert first == second and first.label == daemon.DEFAULT_FENCE_LABEL
    assert Path(FS.parse_args(first.argv[3:]).run) == host.R
    assert lines == [f"agent fence: installed {daemon.DEFAULT_FENCE_LABEL}"] * 2


def test_update_on_a_host_without_the_isolation_install_says_nothing(host, monkeypatch):
    from levain import band_hook, update

    (host.L / "policy").rmdir()
    (host.install / ".levain").mkdir()
    monkeypatch.setattr(update, "_run_update", lambda path, **kw: 0)
    monkeypatch.setattr(band_hook, "refresh_after_update", lambda: None)
    lines: list[str] = []
    assert update.run_update(host.install, emit=lines.append) == 0
    assert lines == [] and host.provider.installed == []


def test_a_failed_fence_install_is_said_and_never_fails_the_update(host, monkeypatch):
    from levain import band_hook, update

    def boom(spec):
        raise daemon.DaemonError("bootstrap failed: 5")
    host.provider.install = boom
    (host.install / ".levain").mkdir()
    monkeypatch.setattr(update, "_run_update", lambda path, **kw: 0)
    monkeypatch.setattr(band_hook, "refresh_after_update", lambda: None)
    lines: list[str] = []
    assert update.run_update(host.install, emit=lines.append) == 0
    assert len(lines) == 1 and "could not be installed" in lines[0] and "levain agents fence start" in lines[0]


def test_a_fence_spec_oserror_is_said_and_never_escapes_the_update(host, monkeypatch):
    def boom(install, **kw):
        raise OSError("resolve failed")
    monkeypatch.setattr(A, "fence_spec", boom)
    line = A.refresh_fence_after_update(host.install)
    assert "could not be installed (OSError: resolve failed)" in line and "levain agents fence start" in line
    [via_update] = _update(host, monkeypatch)
    assert "OSError" in via_update


# --- two installs on one host (P7-2 wiring r2, codex HIGH): one fence unit, one install's roots ------------------

def _other_install(host) -> Path:
    other = host.tmp / "other-entity"
    (other / ".levain").mkdir(parents=True)
    return other


def _update(host, monkeypatch) -> list[str]:
    from levain import band_hook, update

    (host.install / ".levain").mkdir(exist_ok=True)
    monkeypatch.setattr(update, "_run_update", lambda path, **kw: 0)
    monkeypatch.setattr(band_hook, "refresh_after_update", lambda: None)
    lines: list[str] = []
    assert update.run_update(host.install, emit=lines.append) == 0
    return lines


def test_update_leaves_a_unit_that_fences_another_install_and_says_so(host, monkeypatch):
    other = _other_install(host)
    host.provider.on_disk = A.fence_spec(other).argv          # install B's unit is the one on this host
    lines = _update(host, monkeypatch)
    assert host.provider.installed == []                       # A's update never replaced it
    [line] = lines
    _, mine = A.fence_roots(host.install)
    _, theirs = A.fence_roots(other)
    assert str(mine) in line and str(theirs) in line
    assert f"levain agents fence start --path {host.install.resolve()}" in line
    assert host.provider.on_disk == A.fence_spec(other).argv


def test_update_leaves_a_unit_whose_agent_store_differs(host, monkeypatch):
    host.provider.on_disk = A.fence_spec(host.install, agent_store=host.tmp / "elsewhere").argv
    [line] = _update(host, monkeypatch)
    assert host.provider.installed == [] and str((host.tmp / "elsewhere").resolve()) in line


def test_update_with_the_same_roots_reinstalls_idempotently(host, monkeypatch):
    host.provider.on_disk = A.fence_spec(host.install).argv
    lines = _update(host, monkeypatch) + _update(host, monkeypatch)
    first, second = host.provider.installed
    assert first == second and lines == [f"agent fence: installed {daemon.DEFAULT_FENCE_LABEL}"] * 2


def test_update_leaves_a_unit_it_cannot_read_and_says_so(host, monkeypatch):
    def unreadable(label):
        raise daemon.DaemonError("the plist is not a plist")
    host.provider.installed_argv = unreadable
    [line] = _update(host, monkeypatch)
    assert host.provider.installed == []
    assert "could not be read" in line and "the plist is not a plist" in line
    assert "levain agents fence start" in line


def test_update_leaves_a_unit_that_is_not_a_fence_units_shape(host, monkeypatch):
    host.provider.on_disk = ["/usr/bin/python3", "-m", "something.else"]
    [line] = _update(host, monkeypatch)
    assert host.provider.installed == [] and "could not be read" in line


def test_explicit_start_replaces_another_installs_unit_and_names_both_roots(host, capsys):
    other = _other_install(host)
    host.provider.on_disk = A.fence_spec(other).argv
    rc, out = _start(host, capsys)
    assert rc == 0, out.err
    [spec] = host.provider.installed
    assert spec == A.fence_spec(host.install)
    _, mine = A.fence_roots(host.install)
    _, theirs = A.fence_roots(other)
    assert f"replacing the fence unit for {theirs}" in out.out and str(mine) in out.out


def test_explicit_start_replaces_a_unit_it_cannot_read(host, capsys):
    def unreadable(label):
        raise daemon.DaemonError("garbled")
    host.provider.installed_argv = unreadable
    rc, out = _start(host, capsys)
    assert rc == 0 and len(host.provider.installed) == 1 and "could not be read" in out.out


def test_explicit_start_on_the_same_roots_says_nothing_extra(host, capsys):
    assert _start(host, capsys)[0] == 0
    rc, out = _start(host, capsys)
    assert rc == 0 and "replacing" not in out.out and "could not be read" not in out.out


# --- reading an installed unit back, through the real providers ----------------------------------------------

_ODD = ["/opt/py thon/bin/python3", "-m", daemon.FENCE_MODULE, "--agent-store", "/a/100%/$HOME/s'q\\x",
        "--cockpit-root", "/c/root", "--run", "/r"]


def _odd_spec(tmp: Path) -> daemon.DaemonSpec:
    return daemon.DaemonSpec(label=daemon.DEFAULT_FENCE_LABEL, argv=list(_ODD), working_dir=tmp, env={"PATH": "/bin"},
                             stdout_log=tmp / "o.log", stderr_log=tmp / "e.log")


def test_launchd_reads_back_the_argv_it_rendered(tmp_path, monkeypatch):
    monkeypatch.setattr(daemon.LaunchdProvider, "UNIT_DIR", tmp_path)
    p = daemon.LaunchdProvider()
    assert p.installed_argv(daemon.DEFAULT_FENCE_LABEL) is None
    (tmp_path / f"{daemon.DEFAULT_FENCE_LABEL}.plist").write_text(p.render_unit(_odd_spec(tmp_path)))
    assert p.installed_argv(daemon.DEFAULT_FENCE_LABEL) == _ODD
    (tmp_path / f"{daemon.DEFAULT_FENCE_LABEL}.plist").write_text("not a plist")
    with pytest.raises(daemon.DaemonError):
        p.installed_argv(daemon.DEFAULT_FENCE_LABEL)


def test_systemd_reads_back_the_argv_it_rendered(tmp_path, monkeypatch):
    monkeypatch.setattr(daemon.SystemdUserProvider, "UNIT_DIR", tmp_path)
    p = daemon.SystemdUserProvider()
    assert p.installed_argv(daemon.DEFAULT_FENCE_LABEL) is None
    unit = tmp_path / f"{daemon.DEFAULT_FENCE_LABEL}.service"
    unit.write_text(p.render_unit(_odd_spec(tmp_path)))
    assert p.installed_argv(daemon.DEFAULT_FENCE_LABEL) == _ODD
    unit.write_text("[Service]\nType=simple\n")
    with pytest.raises(daemon.DaemonError):
        p.installed_argv(daemon.DEFAULT_FENCE_LABEL)


# --- the real launchd provider, launchctl recorded ------------------------------------------------------------

def test_start_through_the_launchd_provider_bootstraps_and_kickstarts(host, capsys, monkeypatch):
    calls: list[list[str]] = []

    def run(cmd, *, check):
        calls.append(list(cmd))
        return subprocess.CompletedProcess(cmd, 0, stdout="state = running\n\tpid = 4242\n", stderr="")
    monkeypatch.setattr(daemon, "_run", run)
    monkeypatch.setattr(daemon, "_refuse_root", lambda: None)
    monkeypatch.setattr(daemon, "_default_log_dir", lambda: host.tmp / "logs")
    monkeypatch.setattr(daemon.LaunchdProvider, "UNIT_DIR", host.tmp / "LaunchAgents")
    provider = daemon.LaunchdProvider()
    monkeypatch.setattr(daemon, "select_provider", lambda system=None: provider)
    for _ in range(2):
        rc, out = _start(host, capsys)
        assert rc == 0, out.err
    plist = host.tmp / "LaunchAgents" / f"{daemon.DEFAULT_FENCE_LABEL}.plist"
    assert plist.is_file() and str(host.R) in plist.read_text()
    # a second install's update reads the plist the first wrote and leaves it, with no launchctl call at all
    before = list(calls)
    lines = _update(argparse.Namespace(**{**vars(host), "install": _other_install(host)}), monkeypatch)
    assert calls == before and len(lines) == 1 and "left alone" in lines[0] and str(host.R) in plist.read_text()
    assert str(A.fence_roots(host.install)[1]) in plist.read_text()
    verbs = [c[1] for c in calls if c[0] == "launchctl"]
    # each start: bootout, bootstrap, kickstart -k (the start), then the status read
    kick = [c for c in calls if c[:3] == ["launchctl", "kickstart", "-k"]]
    assert len(kick) == 2 and all(c[3].endswith("/" + daemon.DEFAULT_FENCE_LABEL) for c in kick)
    assert verbs.count("bootstrap") == 2


def test_start_creates_the_absent_agent_store_before_installing(host, capsys):
    store = host.tmp / "home" / "autonomic"
    assert not store.exists()
    rc, out = _start(host, capsys)
    assert rc == 0, out.err
    assert store.is_dir()                       # launchd exits 78 (EX_CONFIG) on a missing WorkingDirectory
    assert host.provider.installed[0].working_dir == store.resolve()


def test_update_creates_the_absent_agent_store_before_installing(host):
    store = host.tmp / "update-home" / "autonomic"
    line = A.refresh_fence_after_update(host.install, provider=host.provider, agent_store=store)
    assert store.is_dir() and line.startswith("agent fence:") and "not installed" not in line
    assert host.provider.installed[0].working_dir == store.resolve()


def test_update_says_and_installs_nothing_when_the_agent_store_cannot_be_created(host):
    blocker = host.tmp / "update-blocker"
    blocker.write_text("a regular file")
    line = A.refresh_fence_after_update(host.install, provider=host.provider, agent_store=blocker / "autonomic")
    assert line.startswith("agent fence: not installed:") and "could not be created" in line
    assert host.provider.installed == []


def test_a_store_this_user_cannot_enter_installs_nothing(host, capsys):
    import os
    store = host.tmp / "locked-store"
    store.mkdir()
    store.chmod(0o000)
    try:
        if os.access(store, os.X_OK):
            pytest.skip("this user can enter a mode-000 directory (root)")
        rc = A.run_fence_start(argparse.Namespace(path=host.install, store_dir=store))
        err = capsys.readouterr().err
        assert rc == 1 and "not a directory this user can enter" in err and host.provider.installed == []
    finally:
        store.chmod(0o700)


def test_start_refuses_and_installs_nothing_when_the_agent_store_cannot_be_created(host, capsys):
    blocker = host.tmp / "blocker"
    blocker.write_text("a regular file")
    rc = A.run_fence_start(argparse.Namespace(path=host.install, store_dir=blocker / "autonomic"))
    err = capsys.readouterr().err
    assert rc == 1 and "could not be created" in err and host.provider.installed == []

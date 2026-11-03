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

    def install(self, spec: daemon.DaemonSpec) -> str:
        self.installed.append(spec)
        return f"installed {spec.label}\n  status: running"

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
    verbs = [c[1] for c in calls if c[0] == "launchctl"]
    # each start: bootout, bootstrap, kickstart -k (the start), then the status read
    kick = [c for c in calls if c[:3] == ["launchctl", "kickstart", "-k"]]
    assert len(kick) == 2 and all(c[3].endswith("/" + daemon.DEFAULT_FENCE_LABEL) for c in kick)
    assert verbs.count("bootstrap") == 2

"""Tests for levain.daemon — the cross-platform always-on `serve` recipe.

`build_spec` + `render_unit` are PURE (no I/O) and tested directly. The launchd
install/uninstall/status/restart shell out to `launchctl`; those tests fake
subprocess.run and redirect UNIT_DIR + logs to tmp_path so nothing touches the
real ~/Library/LaunchAgents.
"""

from __future__ import annotations

import plistlib
import subprocess
from pathlib import Path

import pytest

from levain import daemon
from levain.daemon import (
    DaemonError,
    LaunchdProvider,
    build_spec,
    select_provider,
)


# --- build_spec (pure resolution) ------------------------------------------------------------

def test_build_spec_argv_is_serve_write_no_open() -> None:
    spec = build_spec(install_path=Path("/tmp/some/install"), port=7421, label="com.x.y")
    assert "serve" in spec.argv
    assert "--write" in spec.argv          # the daily-driver cockpit, not read-only
    assert "--no-open" in spec.argv        # a login-launched proc must not pop a tab
    assert "--port" in spec.argv and "7421" in spec.argv
    assert "--path" in spec.argv
    assert spec.label == "com.x.y"


def test_build_spec_resolves_install_path_absolute() -> None:
    spec = build_spec(install_path=Path("."), port=7420)
    idx = spec.argv.index("--path")
    assert Path(spec.argv[idx + 1]).is_absolute()   # a login unit has no stable cwd
    assert spec.working_dir.is_absolute()


def test_build_spec_env_has_login_path_gotcha_keys() -> None:
    spec = build_spec(install_path=Path("/tmp/x"))
    assert spec.env["PYTHONUNBUFFERED"] == "1"      # banner + crash reach the log live
    assert "HOME" in spec.env
    bin_dir = str(Path(spec.argv[0]).resolve().parent)
    assert bin_dir in spec.env["PATH"].split(":")   # minimal-login-PATH gotcha
    # PYTHONPATH points at the dir that CONTAINS the levain package (import-from-any-cwd)
    assert (Path(spec.env["PYTHONPATH"]) / "levain").is_dir()


def test_build_spec_log_paths_carry_label(tmp_path) -> None:
    spec = build_spec(install_path=Path("/tmp/x"), label="com.levainhq.zz", log_dir=tmp_path)
    # log_dir is resolved (a login unit needs absolute resolved paths) -> compare resolved
    assert spec.stdout_log == tmp_path.resolve() / "com.levainhq.zz.log"
    assert spec.stderr_log == tmp_path.resolve() / "com.levainhq.zz.err"


# --- render_unit (pure, macOS plist) ---------------------------------------------------------

def test_render_unit_is_valid_plist_with_core_keys() -> None:
    spec = build_spec(install_path=Path("/tmp/inst"), port=7420, label="com.levainhq.t")
    d = plistlib.loads(LaunchdProvider().render_unit(spec).encode())
    assert d["Label"] == "com.levainhq.t"
    assert d["ProgramArguments"] == spec.argv
    assert d["RunAtLoad"] is True          # login-start
    assert d["KeepAlive"] is True          # crash-survive
    assert d["WorkingDirectory"] == str(spec.working_dir)
    assert d["EnvironmentVariables"]["PYTHONUNBUFFERED"] == "1"
    assert d["StandardOutPath"].endswith(".log")
    assert d["StandardErrorPath"].endswith(".err")


def test_render_unit_is_per_user_never_system_scope() -> None:
    # the LOAD-BEARING invariant: a launchd USER agent, never a system LaunchDaemon / root.
    xml = LaunchdProvider().render_unit(build_spec(install_path=Path("/tmp/inst")))
    assert "LaunchDaemon" not in xml
    assert "RU SYSTEM" not in xml
    assert LaunchdProvider()._plist_path("com.x") == Path.home() / "Library" / "LaunchAgents" / "com.x.plist"


# --- select_provider -------------------------------------------------------------------------

def test_select_provider_darwin() -> None:
    assert isinstance(select_provider("Darwin"), LaunchdProvider)


@pytest.mark.parametrize("os_name", ["Linux", "Windows", "Plan9"])
def test_select_provider_unsupported_raises(os_name: str) -> None:
    with pytest.raises(NotImplementedError):
        select_provider(os_name)


# --- launchd lifecycle (faked launchctl) -----------------------------------------------------

class _FakeRun:
    """Records launchctl invocations; returns a CompletedProcess with a per-subcommand rc/stdout."""

    def __init__(self, rc_for: dict[str, int] | None = None,
                 stdout_for: dict[str, str] | None = None) -> None:
        self.calls: list[list[str]] = []
        self._rc_for = rc_for or {}
        self._stdout_for = stdout_for or {}

    def __call__(self, cmd, capture_output=True, text=True):  # noqa: ANN001
        self.calls.append(cmd)
        sub = cmd[1] if len(cmd) > 1 else ""
        return subprocess.CompletedProcess(
            cmd, self._rc_for.get(sub, 0), stdout=self._stdout_for.get(sub, ""), stderr="")

    @property
    def subs(self) -> list[str]:
        return [c[1] for c in self.calls]


@pytest.fixture
def launchd(tmp_path, monkeypatch):
    monkeypatch.setattr(LaunchdProvider, "UNIT_DIR", tmp_path / "LaunchAgents")
    fake = _FakeRun()
    monkeypatch.setattr(daemon.subprocess, "run", fake)
    return LaunchdProvider(), fake


def test_install_writes_plist_then_bootout_bootstrap_kickstart(launchd, tmp_path) -> None:
    prov, fake = launchd
    spec = build_spec(install_path=tmp_path / "inst", port=7420, label="com.levainhq.t",
                      log_dir=tmp_path / "logs")
    msg = prov.install(spec)
    plist = prov._plist_path("com.levainhq.t")
    assert plist.exists()
    assert plistlib.loads(plist.read_bytes())["Label"] == "com.levainhq.t"
    # idempotent ordering: bootout (drop any stale) BEFORE bootstrap, then kickstart-now
    assert fake.subs[:3] == ["bootout", "bootstrap", "kickstart"]
    assert "print" in fake.subs            # install VERIFIES the run state (no false green)
    assert "installed com.levainhq.t" in msg
    assert (tmp_path / "logs").exists()    # log dir created


def test_install_raises_when_bootstrap_fails(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(LaunchdProvider, "UNIT_DIR", tmp_path / "LaunchAgents")
    fake = _FakeRun(rc_for={"bootstrap": 5},
                    stdout_for={"bootstrap": "Bootstrap failed: 5: Input/output error"})
    monkeypatch.setattr(daemon.subprocess, "run", fake)
    spec = build_spec(install_path=tmp_path / "inst", label="com.levainhq.t",
                      log_dir=tmp_path / "logs")
    with pytest.raises(DaemonError):
        LaunchdProvider().install(spec)


def test_uninstall_removes_plist(launchd, tmp_path) -> None:
    prov, fake = launchd
    spec = build_spec(install_path=tmp_path / "inst", label="com.levainhq.t",
                      log_dir=tmp_path / "logs")
    prov.install(spec)
    assert prov._plist_path("com.levainhq.t").exists()
    fake.calls.clear()
    msg = prov.uninstall("com.levainhq.t")
    assert not prov._plist_path("com.levainhq.t").exists()
    assert fake.subs == ["bootout"]
    assert "uninstalled" in msg


def test_uninstall_absent_is_noop(launchd) -> None:
    prov, _ = launchd
    assert "was not installed" in prov.uninstall("com.levainhq.never")


def test_status_reports_installed_and_running(launchd, tmp_path) -> None:
    prov, fake = launchd
    spec = build_spec(install_path=tmp_path / "inst", label="com.levainhq.t",
                      log_dir=tmp_path / "logs")
    prov.install(spec)
    fake._stdout_for["print"] = "com.levainhq.t = {\n\tstate = running\n\tpid = 4242\n}"
    st = prov.status("com.levainhq.t")
    assert st.installed is True and st.running is True   # live PID present
    assert "pid = 4242" in st.detail


def test_status_active_state_with_pid_is_running(launchd) -> None:
    # L4-live: a live launchd job printed `state = active` (NOT "running") WITH a pid — keying on
    # the state string was a false NEGATIVE. A live PID is the cross-version running signal.
    prov, fake = launchd
    fake._stdout_for["print"] = "com.levainhq.t = {\n\tstate = active\n\tpid = 95810\n}"
    st = prov.status("com.levainhq.t")
    assert st.running is True
    assert "pid = 95810" in st.detail


def test_status_crash_loop_surfaces_last_exit(launchd) -> None:
    # a flapping job: a transient pid + a nonzero last exit code -> running True (a process exists
    # this instant) but detail surfaces the nonzero exit so the operator sees it's dying.
    prov, fake = launchd
    fake._stdout_for["print"] = (
        "com.levainhq.t = {\n\tstate = active\n\tpid = 700\n\tlast exit code = 1\n}")
    st = prov.status("com.levainhq.t")
    assert st.running is True
    assert "last exit = 1" in st.detail


def test_status_not_loaded(launchd) -> None:
    prov, fake = launchd
    fake._rc_for["print"] = 113   # launchctl: could not find service
    st = prov.status("com.levainhq.gone")
    assert st.installed is False and st.running is False
    assert st.detail == "not loaded"


def test_status_loaded_but_waiting_is_not_running(launchd) -> None:
    # codex L3 MED: rc==0 means LOADED, not running. A KeepAlive-throttled / `state = waiting`
    # job is loaded but has no live PID -> must NOT report running=True (the false-green codex
    # caught). running is true ONLY when launchd reports `state = running`.
    prov, fake = launchd
    fake._stdout_for["print"] = "com.levainhq.t = {\n\tstate = waiting\n}"
    st = prov.status("com.levainhq.t")
    assert st.running is False
    assert "waiting" in st.detail


def test_daemon_ops_refuse_root_and_sudo(launchd, tmp_path, monkeypatch) -> None:
    # the per-user/NO-root invariant, structural (codex L3 MED): a daemon op as root or via sudo
    # would write root-owned files + target gui/0. Both signals (euid==0 AND $SUDO_UID) refuse.
    prov, _ = launchd
    spec = build_spec(install_path=tmp_path / "inst", label="com.levainhq.t",
                      log_dir=tmp_path / "logs")
    monkeypatch.setattr(daemon.os, "geteuid", lambda: 0, raising=False)
    with pytest.raises(DaemonError, match="root"):
        prov.install(spec)
    # the sudo signal (normal euid, but SUDO_UID set) is refused too
    monkeypatch.setattr(daemon.os, "geteuid", lambda: 501, raising=False)
    monkeypatch.setenv("SUDO_UID", "501")
    with pytest.raises(DaemonError, match="sudo"):
        prov.uninstall("com.levainhq.t")


def test_restart_kickstarts(launchd) -> None:
    prov, fake = launchd
    assert "restarted" in prov.restart("com.levainhq.t")
    assert fake.subs == ["kickstart"]


def test_restart_raises_on_failure(monkeypatch) -> None:
    monkeypatch.setattr(daemon.subprocess, "run", _FakeRun(rc_for={"kickstart": 3}))
    with pytest.raises(DaemonError):
        LaunchdProvider().restart("com.levainhq.t")

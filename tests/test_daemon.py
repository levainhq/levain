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
    """Records launchctl invocations; returns a CompletedProcess with a per-subcommand rc/stdout.

    `print` of a SERVICE (target `gui/UID/label`, ≥2 slashes) keys on `"print"`; `print` of a DOMAIN
    (target `gui/UID`, the honesty-floor probe) keys on `"print_domain"` and DEFAULTS to rc 0 (domain
    readable) unless set — so a not-loaded service can still sit in a readable domain."""

    def __init__(self, rc_for: dict[str, int] | None = None,
                 stdout_for: dict[str, str] | None = None) -> None:
        self.calls: list[list[str]] = []
        self._rc_for = rc_for or {}
        self._stdout_for = stdout_for or {}

    def __call__(self, cmd, capture_output=True, text=True):  # noqa: ANN001
        self.calls.append(cmd)
        sub = cmd[1] if len(cmd) > 1 else ""
        key = sub
        if sub == "print":
            key = "print" if str(cmd[-1]).count("/") >= 2 else "print_domain"
        rc = self._rc_for.get(key, 0)
        out = self._stdout_for.get(key, "")
        return subprocess.CompletedProcess(cmd, rc, stdout=out, stderr="")

    @property
    def subs(self) -> list[str]:
        return [c[1] for c in self.calls]


@pytest.fixture
def launchd(tmp_path, monkeypatch):
    monkeypatch.setattr(LaunchdProvider, "UNIT_DIR", tmp_path / "LaunchAgents")
    monkeypatch.setattr(daemon.time, "sleep", lambda *_: None)  # don't sleep through bootstrap retries
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


def test_install_first_install_failure_keeps_valid_unit_for_runatload(tmp_path, monkeypatch) -> None:
    # the apparatus pivot (codex+L2 HIGH): a FIRST install whose bootstrap fails must NOT delete the
    # unit — the failure is usually TRANSIENT (the bootout-teardown race) or environmental (no Aqua
    # domain), not a bad def. The plist is valid (render_unit produced it); KEEP it so macOS RunAtLoad
    # self-heals it at next login. Deleting it would regress autostart.
    monkeypatch.setattr(LaunchdProvider, "UNIT_DIR", tmp_path / "LaunchAgents")
    monkeypatch.setattr(daemon.time, "sleep", lambda *_: None)
    # bootstrap fails AND the service never reads as loaded (so the retry exhausts, no false success)
    fake = _FakeRun(rc_for={"bootstrap": 5, "print": 113},
                    stdout_for={"bootstrap": "Bootstrap failed: 5: Input/output error"})
    monkeypatch.setattr(daemon.subprocess, "run", fake)
    prov = LaunchdProvider()
    spec = build_spec(install_path=tmp_path / "inst", label="com.levainhq.t",
                      log_dir=tmp_path / "logs")
    plist = prov._plist_path("com.levainhq.t")
    with pytest.raises(DaemonError, match="KEPT"):
        prov.install(spec)
    assert plist.exists()                                    # valid unit kept for RunAtLoad
    assert fake.subs.count("bootstrap") == 3                 # retried the transient before giving up


def test_install_rolls_back_to_prior_unit_on_bootstrap_failure(launchd, tmp_path) -> None:
    # a CHANGED unit whose bootstrap fails must roll back to the prior GOOD unit (degraded-but-running
    # beats down) — never destroy the working def AND never leave the rejected one.
    prov, fake = launchd
    prov.install(build_spec(install_path=tmp_path / "inst", port=7420, label="com.levainhq.t",
                            log_dir=tmp_path / "logs"))
    plist = prov._plist_path("com.levainhq.t")
    prior_bytes = plist.read_bytes()
    fake._rc_for["bootstrap"] = 5            # the next bootstrap (of the changed unit) fails
    fake._rc_for["print"] = 113              # ...and the service never reads loaded (retry exhausts)
    fake._stdout_for["bootstrap"] = "Bootstrap failed: 5"
    spec2 = build_spec(install_path=tmp_path / "inst2", port=7421, label="com.levainhq.t",
                       log_dir=tmp_path / "logs")
    with pytest.raises(DaemonError, match="rolled back"):
        prov.install(spec2)
    assert plist.exists()
    assert plist.read_bytes() == prior_bytes  # the prior good unit, NOT the rejected spec2 unit


# --- would_install (dry-run, mutation-free; the honesty floor) --------------------------------

def _no_mutation(fake) -> bool:
    # a dry-run must call only the read-only `print` probe — never a state-changing verb.
    return all(s == "print" for s in fake.subs) and fake.subs.count("print") >= 1


def test_would_install_fresh_when_nothing_on_disk(launchd, tmp_path) -> None:
    prov, fake = launchd
    fake._rc_for["print"] = 113              # service not loaded (domain readable by default)
    spec = build_spec(install_path=tmp_path / "inst", label="com.levainhq.t",
                      log_dir=tmp_path / "logs")
    plan = prov.would_install(spec)
    assert plan.on_disk is False and plan.would_change is True
    assert "FRESH INSTALL" in plan.action
    assert plan.current.installed is False and plan.current.running is False
    assert not plan.unit_path.exists()       # DRY-RUN: nothing written
    assert _no_mutation(fake)                # only read-only print probes, no bootout/bootstrap/kickstart


def test_would_install_noop_when_unchanged_and_running(launchd, tmp_path) -> None:
    prov, fake = launchd
    spec = build_spec(install_path=tmp_path / "inst", label="com.levainhq.t",
                      log_dir=tmp_path / "logs")
    prov.install(spec)
    fake.calls.clear()
    fake._stdout_for["print"] = "com.levainhq.t = {\n\tstate = running\n\tpid = 5\n}"
    plan = prov.would_install(spec)
    assert plan.on_disk is True and plan.would_change is False
    assert plan.current.load_state == "running"
    assert "no-op" in plan.action
    assert _no_mutation(fake)                # still mutation-free even with a unit on disk


def test_would_install_rebootstrap_when_on_disk_but_not_loaded(launchd, tmp_path) -> None:
    # the honesty floor: a unit FILE on disk that is NOT actually loaded still needs a (re-)bootstrap
    # — "on disk" must never read as "installed + loaded".
    prov, fake = launchd
    spec = build_spec(install_path=tmp_path / "inst", label="com.levainhq.t",
                      log_dir=tmp_path / "logs")
    prov.install(spec)
    fake._rc_for["print"] = 113              # service NOT loaded, BUT domain readable (default rc 0)
    plan = prov.would_install(spec)
    assert plan.on_disk is True and plan.would_change is False
    assert plan.current.load_state == "not-loaded"
    assert "RE-BOOTSTRAP" in plan.action


def test_would_install_unknown_when_domain_unreadable(launchd, tmp_path) -> None:
    # the no-data≠not-loaded honesty floor: when the GUI/Aqua domain itself is unreadable (ssh), the
    # dry-run must say UNKNOWN — never assert a load state it cannot see.
    prov, fake = launchd
    spec = build_spec(install_path=tmp_path / "inst", label="com.levainhq.t",
                      log_dir=tmp_path / "logs")
    prov.install(spec)
    fake._rc_for["print"] = 113              # service print fails
    fake._rc_for["print_domain"] = 113       # ...AND the domain probe fails → genuinely UNKNOWN
    plan = prov.would_install(spec)
    assert plan.current.load_state == "unknown"
    assert "UNKNOWN" in plan.action


def test_would_install_reinstall_when_unit_changed(launchd, tmp_path) -> None:
    prov, _ = launchd
    prov.install(build_spec(install_path=tmp_path / "inst", port=7420, label="com.levainhq.t",
                            log_dir=tmp_path / "logs"))
    spec2 = build_spec(install_path=tmp_path / "inst2", port=7421, label="com.levainhq.t",
                       log_dir=tmp_path / "logs")
    plan = prov.would_install(spec2)
    assert plan.on_disk is True and plan.would_change is True
    assert "REINSTALL" in plan.action


def test_would_install_unreadable_on_disk_unit_treated_as_change(launchd, tmp_path, monkeypatch) -> None:
    # the OSError branch: an on-disk unit we can't READ is treated as a change (a real reinstall), not
    # a silent no-op.
    prov, _ = launchd
    spec = build_spec(install_path=tmp_path / "inst", label="com.levainhq.t",
                      log_dir=tmp_path / "logs")
    prov.install(spec)
    def _boom(*a, **k):
        raise OSError("unreadable")
    monkeypatch.setattr(Path, "read_text", _boom)
    plan = prov.would_install(spec)
    assert plan.on_disk is True and plan.would_change is True
    assert "REINSTALL" in plan.action


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
    fake._rc_for["print"] = 113   # service print fails; domain probe (print_domain) defaults rc 0 = readable
    st = prov.status("com.levainhq.gone")
    assert st.installed is False and st.running is False
    assert st.detail == "not loaded"
    assert st.load_state == "not-loaded"   # domain readable + service absent = genuinely not-loaded


def test_status_unknown_when_domain_unreadable(launchd) -> None:
    # the no-data≠not-loaded honesty floor (codex+L2 HIGH): a failed service print PLUS a failed
    # domain probe (ssh / no Aqua session) must read as UNKNOWN, never a false "not loaded".
    prov, fake = launchd
    fake._rc_for["print"] = 113          # service print fails
    fake._rc_for["print_domain"] = 113   # ...AND the domain itself is unreadable
    st = prov.status("com.levainhq.t")
    assert st.running is False and st.load_state == "unknown"
    assert "unknown" in st.detail


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

"""Tests for levain.daemon — the cross-platform always-on `serve` recipe.

`build_spec` + `render_unit` are PURE (no I/O) and tested directly. The launchd
install/uninstall/status/restart shell out to `launchctl`; those tests fake
subprocess.run and redirect UNIT_DIR + logs to tmp_path so nothing touches the
real ~/Library/LaunchAgents.
"""

from __future__ import annotations

import platform
import plistlib
import shutil
import subprocess
from pathlib import Path

import pytest
from dataclasses import replace

from levain import daemon
from levain.daemon import (
    DaemonError,
    DaemonSpec,
    LaunchdProvider,
    SystemdUserProvider,
    build_seat_spec,
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


def test_select_provider_linux_is_systemd_user() -> None:
    """K4c. Linux was previously in the "unsupported" parametrize list below; it shipping is why it
    moved out. Without this provider there is no SCHEDULED Linux seat at all — only confinement with
    nothing supervising it."""
    assert isinstance(select_provider("Linux"), SystemdUserProvider)


@pytest.mark.parametrize("os_name", ["Windows", "Plan9"])
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


# --- K4a: the scheduled governed seat (periodic, not resident) --------------------------------
#
# A seat and the cockpit are DIFFERENT SHAPES of supervised process: the cockpit is resident
# (keep_alive, no interval), a seat runs one bounded turn on a cadence and exits. These tests pin
# the difference, and pin the three argv choices that each look like an obvious flag to flip.

def test_seat_spec_is_periodic_not_resident(tmp_path) -> None:
    spec = build_seat_spec(entity_path=tmp_path / "ent", task="t", interval=1800,
                           log_dir=tmp_path / "logs")
    assert spec.start_interval == 1800
    assert spec.keep_alive is False        # a KeepAlive seat would ignore its own interval
    assert spec.run_at_login is False      # installing a schedule ≠ spending a turn right now


def test_seat_spec_passes_entity_path_positionally_not_as_a_flag(tmp_path) -> None:
    # `levain run` takes the entity dir POSITIONALLY; a `--path` would be a usage error that
    # fails identically every interval, forever, into a log nobody is watching.
    spec = build_seat_spec(entity_path=tmp_path / "ent", task="review", log_dir=tmp_path / "l")
    assert "--path" not in spec.argv
    run_i = spec.argv.index("run")
    assert spec.argv[run_i + 1] == str((tmp_path / "ent").resolve())
    assert spec.argv[spec.argv.index("--task") + 1] == "review"


def test_seat_spec_resolves_entity_path_absolute(tmp_path) -> None:
    spec = build_seat_spec(entity_path=Path("."), task="t", log_dir=tmp_path / "l")
    assert Path(spec.argv[spec.argv.index("run") + 1]).is_absolute()


def test_seat_spec_declares_itself_unattended(tmp_path) -> None:
    """EMITTED, not inferred: the seat's governance posture must be auditable in the unit file
    itself. Anyone reading the plist argv sees that this drive declares no human in the loop, and
    therefore that the standard credential stores are denied by default. Inferring it from "does
    this spec have a StartInterval" would hide a security-relevant fact from the argv."""
    spec = build_seat_spec(entity_path=tmp_path / "ent", task="t", log_dir=tmp_path / "l")
    assert "--unattended" in spec.argv


def test_seat_spec_does_not_quiet_the_activity_stream(tmp_path) -> None:
    # --quiet suppresses tool activity and prints only the final reply. For an UNATTENDED seat
    # that stream IS the operator's fan-in surface — the record of what it did, including a K3
    # gated halt. Quieting it leaves a log that cannot tell "did nothing" from "was stopped".
    spec = build_seat_spec(entity_path=tmp_path / "ent", task="t", log_dir=tmp_path / "l")
    assert "--quiet" not in spec.argv


def test_seat_spec_bounds_iterations_by_default(tmp_path) -> None:
    # "time-bounded" is part of K4a's definition: an unattended turn with no step bound can
    # spend indefinitely with nobody watching.
    spec = build_seat_spec(entity_path=tmp_path / "ent", task="t", log_dir=tmp_path / "l")
    i = spec.argv.index("--max-iterations")
    assert spec.argv[i + 1] == str(daemon.DEFAULT_SEAT_MAX_ITERATIONS)


def test_seat_spec_iteration_bound_is_opt_outable(tmp_path) -> None:
    spec = build_seat_spec(entity_path=tmp_path / "ent", task="t", max_iterations=None,
                           log_dir=tmp_path / "l")
    assert "--max-iterations" not in spec.argv


def test_seat_spec_bounds_WALL_CLOCK_by_default(tmp_path) -> None:
    """K4a ⑥, `spore-434`. The step bound above cannot bound a turn hung inside ONE step, and
    because launchd COALESCES per label the seat then never runs again — behind a unit still
    reporting installed + loaded. So this bound is not redundant with the step bound; it is the only
    one of the two that makes K4a's "restartable" true.

    EMITTED into the argv rather than left to a default inside the binary, so "is this seat
    restartable" is answerable by reading the plist — which is where an auditor looks."""
    spec = build_seat_spec(entity_path=tmp_path / "ent", task="t", log_dir=tmp_path / "l")
    i = spec.argv.index("--max-seconds")
    assert spec.argv[i + 1] == f"{daemon.DEFAULT_SEAT_MAX_SECONDS:g}"


def test_seat_spec_wall_clock_bound_renders_without_a_float_tail(tmp_path) -> None:
    """`str(1800.0)` is `"1800.0"`, and this value goes into a UNIT FILE a human reads. Cosmetic on
    its own, but the plist is the audit surface, so noise in it costs real legibility."""
    spec = build_seat_spec(entity_path=tmp_path / "ent", task="t", log_dir=tmp_path / "l")
    i = spec.argv.index("--max-seconds")
    assert spec.argv[i + 1] == "1800"
    assert "." not in spec.argv[i + 1]


def test_seat_spec_wall_clock_bound_is_opt_outable(tmp_path) -> None:
    spec = build_seat_spec(entity_path=tmp_path / "ent", task="t", max_seconds=None,
                           log_dir=tmp_path / "l")
    assert "--max-seconds" not in spec.argv


def test_the_default_wall_clock_bound_fits_INSIDE_the_default_cadence() -> None:
    """THE RELATIONSHIP BETWEEN THE TWO DEFAULTS IS ITSELF LOAD-BEARING, so it is pinned.

    launchd coalesces per label, so a turn allowed to outlive its own interval necessarily SKIPS
    intervals — the cadence silently becomes the turn's duration. That is legitimate when an operator
    chooses it (the install banner says so), but it must never be what the DEFAULTS do: a seat that
    quietly runs at half its stated cadence out of the box is the same class of lie as a unit that
    reports itself loaded while dead.

    Pinned in the other direction too — a bound must leave room for a real turn. 40 agent steps on a
    cloud open model runs to minutes, so a bound of, say, 30s would terminate healthy work every
    interval and present exactly as a broken seat."""
    assert daemon.DEFAULT_SEAT_MAX_SECONDS < daemon.DEFAULT_SEAT_INTERVAL
    assert daemon.DEFAULT_SEAT_MAX_SECONDS >= 300


def test_seat_and_cockpit_do_not_share_a_label() -> None:
    # colliding labels would make `daemon install` silently REPLACE one unit with the other.
    assert daemon.DEFAULT_SEAT_LABEL != daemon.DEFAULT_LABEL


# --- the invariant: a periodic spec that keeps alive is UNREPRESENTABLE ------------------------

def test_periodic_spec_refuses_keep_alive() -> None:
    # launchd relaunches a KeepAlive job the instant it exits, so periodic+keepalive silently
    # collapses the cadence into a hot loop — a unit that renders cleanly and LIES about itself.
    with pytest.raises(ValueError, match="keep_alive=False"):
        DaemonSpec(label="x", argv=["a"], working_dir=Path("/"), env={},
                   stdout_log=Path("/o"), stderr_log=Path("/e"),
                   keep_alive=True, start_interval=60)


@pytest.mark.parametrize("bad", [0, -1])
def test_periodic_spec_refuses_nonpositive_interval(bad: int) -> None:
    with pytest.raises(ValueError, match="positive number of seconds"):
        DaemonSpec(label="x", argv=["a"], working_dir=Path("/"), env={},
                   stdout_log=Path("/o"), stderr_log=Path("/e"),
                   keep_alive=False, start_interval=bad)


def test_resident_spec_is_unaffected_by_the_invariant() -> None:
    spec = DaemonSpec(label="x", argv=["a"], working_dir=Path("/"), env={},
                      stdout_log=Path("/o"), stderr_log=Path("/e"), keep_alive=True)
    assert spec.start_interval is None and spec.keep_alive is True


# --- rendering --------------------------------------------------------------------------------

def test_render_unit_emits_start_interval_for_a_seat(tmp_path) -> None:
    spec = build_seat_spec(entity_path=tmp_path / "ent", task="t", interval=900,
                           log_dir=tmp_path / "l")
    doc = plistlib.loads(LaunchdProvider().render_unit(spec).encode("utf-8"))
    assert doc["StartInterval"] == 900
    assert doc["KeepAlive"] is False


def test_render_unit_omits_start_interval_for_the_resident_cockpit(tmp_path) -> None:
    # non-regression: the cockpit unit must be untouched by the seat addition.
    spec = build_spec(install_path=tmp_path / "inst", port=7420)
    doc = plistlib.loads(LaunchdProvider().render_unit(spec).encode("utf-8"))
    assert "StartInterval" not in doc
    assert doc["KeepAlive"] is True


# --- install: a seat is NOT kickstarted, and idle is NOT an alarm -----------------------------

def test_install_does_not_kickstart_a_periodic_seat(launchd, tmp_path) -> None:
    prov, fake = launchd
    spec = build_seat_spec(entity_path=tmp_path / "ent", task="t", label="com.levainhq.seat.t",
                           log_dir=tmp_path / "logs")
    prov.install(spec)
    assert fake.subs[:2] == ["bootout", "bootstrap"]
    assert "kickstart" not in fake.subs   # installing a schedule must not spend a model turn now


def test_install_of_a_seat_reports_idle_as_normal_not_as_a_fault(launchd, tmp_path) -> None:
    # The resident path says "NOT yet running — check the log" when there's no pid. For a periodic
    # seat between turns that is the HEALTHY state, and that message is a false alarm.
    prov, fake = launchd
    spec = build_seat_spec(entity_path=tmp_path / "ent", task="t", interval=1800,
                           label="com.levainhq.seat.t", log_dir=tmp_path / "logs")
    msg = prov.install(spec)
    assert "NOT yet running" not in msg
    assert "1800s" in msg and "NORMAL" in msg
    # BOTH streams named here too — the stdout-only pointer that codex's HIGH was about existed
    # in TWO places, and fixing only the CLI copy would leave the same lie in the install banner.
    assert str(spec.stdout_log) in msg and str(spec.stderr_log) in msg


def test_install_of_the_cockpit_still_kickstarts_and_still_warns(launchd, tmp_path) -> None:
    # non-regression on the resident path: both behaviours preserved.
    prov, fake = launchd
    spec = build_spec(install_path=tmp_path / "inst", port=7420, label="com.levainhq.c",
                      log_dir=tmp_path / "logs")
    msg = prov.install(spec)
    assert "kickstart" in fake.subs
    assert "NOT yet running" in msg   # no pid from the fake → the resident alarm still fires


# --- L3 fixes: the unit must not lie about itself ---------------------------------------------

def test_render_unit_lowers_throttle_for_a_sub_10s_interval(tmp_path) -> None:
    """launchd will not spawn a job more than once every 10s by default (ThrottleInterval). A
    sub-10s StartInterval otherwise installs happily and REPORTS its requested cadence while
    launchd silently enforces ~10s — the unit lying about itself. (codex L3 MEDIUM.)"""
    spec = build_seat_spec(entity_path=tmp_path / "e", task="t", interval=5,
                           log_dir=tmp_path / "l")
    doc = plistlib.loads(LaunchdProvider().render_unit(spec).encode("utf-8"))
    assert doc["StartInterval"] == 5
    assert doc["ThrottleInterval"] == 5


def test_render_unit_leaves_throttle_default_at_or_above_10s(tmp_path) -> None:
    spec = build_seat_spec(entity_path=tmp_path / "e", task="t", interval=3600,
                           log_dir=tmp_path / "l")
    doc = plistlib.loads(LaunchdProvider().render_unit(spec).encode("utf-8"))
    assert "ThrottleInterval" not in doc      # don't override launchd's default needlessly


def test_bootstrap_failure_message_does_not_promise_login_retry_to_a_seat(tmp_path,
                                                                          monkeypatch) -> None:
    """A seat has RunAtLoad=False, so "RunAtLoad will retry it at next login" is FALSE for it —
    the operator reboots and waits for a run that never comes. (glm L3 MEDIUM.)"""
    monkeypatch.setattr(LaunchdProvider, "UNIT_DIR", tmp_path / "LaunchAgents")
    monkeypatch.setattr(daemon.time, "sleep", lambda *_: None)
    monkeypatch.setattr(daemon.subprocess, "run",
                        _FakeRun(rc_for={"bootstrap": 1}))
    spec = build_seat_spec(entity_path=tmp_path / "e", task="t", interval=1800,
                           label="com.levainhq.seat.z", log_dir=tmp_path / "logs")
    with pytest.raises(DaemonError) as exc:
        LaunchdProvider().install(spec)
    msg = str(exc.value)
    assert "RunAtLoad will retry it at next login" not in msg
    assert "RunAtLoad=False" in msg and "1800s" in msg


def test_bootstrap_failure_message_still_promises_login_retry_to_the_cockpit(tmp_path,
                                                                             monkeypatch) -> None:
    # non-regression: the resident unit genuinely DOES self-heal at login.
    monkeypatch.setattr(LaunchdProvider, "UNIT_DIR", tmp_path / "LaunchAgents")
    monkeypatch.setattr(daemon.time, "sleep", lambda *_: None)
    monkeypatch.setattr(daemon.subprocess, "run", _FakeRun(rc_for={"bootstrap": 1}))
    spec = build_spec(install_path=tmp_path / "i", port=7420, label="com.levainhq.c2",
                      log_dir=tmp_path / "logs")
    with pytest.raises(DaemonError) as exc:
        LaunchdProvider().install(spec)
    assert "RunAtLoad will retry it at next login" in str(exc.value)


def test_would_install_surfaces_a_chronically_failing_seat_instead_of_idle(launchd,
                                                                           tmp_path) -> None:
    """"idle" is the healthy state for a seat between turns AND what a seat that has failed every
    interval for three days looks like. The one-line verdict must not read as a clean bill of
    health. (glm L3 LOW.)"""
    prov, fake = launchd
    spec = build_seat_spec(entity_path=tmp_path / "e", task="t", label="com.levainhq.seat.f",
                           log_dir=tmp_path / "logs")
    prov.UNIT_DIR.mkdir(parents=True, exist_ok=True)
    prov._atomic_write(prov._plist_path(spec.label), prov.render_unit(spec).encode("utf-8"))
    fake._stdout_for["print"] = "\tstate = waiting\n\tlast exit code = 3\n"
    plan = prov.would_install(spec)
    assert "LAST RUN FAILED" in plan.action
    assert "last exit = 3" in plan.action


def test_would_install_still_says_idle_for_a_healthy_loaded_seat(launchd, tmp_path) -> None:
    prov, fake = launchd
    spec = build_seat_spec(entity_path=tmp_path / "e", task="t", label="com.levainhq.seat.h",
                           log_dir=tmp_path / "logs")
    prov.UNIT_DIR.mkdir(parents=True, exist_ok=True)
    prov._atomic_write(prov._plist_path(spec.label), prov.render_unit(spec).encode("utf-8"))
    fake._stdout_for["print"] = "\tstate = waiting\n\tlast exit code = 0\n"
    plan = prov.would_install(spec)
    assert plan.action == "no-op — unit unchanged and loaded (idle)"


# =============================================================================================
# SystemdUserProvider (K4c) — the Linux lifecycle half.
#
# ⚠ THE CENTRAL TRAP THESE TESTS GUARD: on launchd the cadence is one more key in the SAME plist,
# so a launchd-derived reading of DaemonSpec says "one spec, one unit". systemd has no such key —
# a periodic job is a .service PLUS a .timer, different unit types with different lifecycles. A
# unit that renders the interval into the service installs cleanly and NEVER FIRES.
# =============================================================================================


class _FakeSystemctl:
    """Records systemctl/loginctl invocations; returns per-verb rc and `show` output."""

    def __init__(self, rc_for=None, show=None) -> None:  # noqa: ANN001
        self.calls: list[list[str]] = []
        self._rc_for = rc_for or {}
        self._show = show or {}

    def __call__(self, cmd, *, check=False):  # noqa: ANN001
        # NOTE the signature: this fake replaces `daemon._run` (which takes `check`), not
        # `subprocess.run` (which the launchd fake above replaces). Two fakes, two seams.
        self.calls.append(cmd)
        verb = cmd[2] if cmd[0] == "systemctl" and len(cmd) > 2 else cmd[0]
        rc = self._rc_for.get(verb, 0)
        if isinstance(rc, list):   # per-call results for this verb, in order; the last one repeats
            rc = rc.pop(0) if len(rc) > 1 else rc[0]
        out = ""
        if verb == "show":
            unit = cmd[3]
            props = self._show.get(unit, self._show.get("*", {}))
            out = "\n".join(f"{k}={v}" for k, v in props.items())
            if not props:
                rc = self._rc_for.get("show", 1)
        return subprocess.CompletedProcess(cmd, rc, stdout=out, stderr="")

    @property
    def verbs(self) -> list[str]:
        return [c[2] if c[0] == "systemctl" and len(c) > 2 else c[0] for c in self.calls]


@pytest.fixture
def systemd(tmp_path, monkeypatch):
    monkeypatch.setattr(SystemdUserProvider, "UNIT_DIR", tmp_path / "systemd-user")
    return SystemdUserProvider()


def _resident(tmp_path) -> DaemonSpec:
    return DaemonSpec(
        label="levain-cockpit", argv=["/usr/bin/levain", "serve", "--write"],
        working_dir=tmp_path, env={"PATH": "/usr/bin", "HOME": str(tmp_path)},
        stdout_log=tmp_path / "out.log", stderr_log=tmp_path / "err.log",
    )


def _seat(tmp_path, interval: int = 900) -> DaemonSpec:
    return DaemonSpec(
        label="levain-seat", argv=["/usr/bin/levain", "run", "--task", "x"],
        working_dir=tmp_path, env={"PATH": "/usr/bin"},
        stdout_log=tmp_path / "seat.log", stderr_log=tmp_path / "seat.err",
        run_at_login=False, keep_alive=False, start_interval=interval,
    )


def test_systemd_resident_unit_restarts_and_has_no_schedule(systemd, tmp_path) -> None:
    unit = systemd.render_unit(_resident(tmp_path))
    assert "Type=simple" in unit
    assert "Restart=always" in unit           # the KeepAlive analogue
    assert "WantedBy=default.target" in unit
    assert systemd.render_timer(_resident(tmp_path)) is None


def test_systemd_seat_puts_the_cadence_in_a_TIMER_not_the_service(systemd, tmp_path) -> None:
    """⛔ THE RE-DERIVATION TRAP. A seat's cadence must live in a separate .timer unit. If it were
    rendered into the .service (the shape launchd's StartInterval suggests) the unit would install
    cleanly, report success, and never fire — the unit lying about its own cadence."""
    spec = _seat(tmp_path, 900)
    service = systemd.render_unit(spec)
    timer = systemd.render_timer(spec)
    assert timer is not None
    assert "OnUnitActiveSec=900" in timer
    assert "Unit=levain-seat.service" in timer
    assert "WantedBy=timers.target" in timer
    # and the SERVICE must not pretend to schedule itself, nor restart-loop
    assert "900" not in service
    assert "Restart=" not in service
    assert "Type=oneshot" in service


def test_systemd_timer_pins_accuracy_or_the_cadence_is_a_lie(systemd, tmp_path) -> None:
    """systemd's DEFAULT AccuracySec is ONE MINUTE. Without pinning it, a sub-minute cadence
    installs happily and silently runs about once a minute — the same class as an unthrottled
    sub-10s launchd job, which the launchd provider already guards with ThrottleInterval."""
    assert "AccuracySec=1s" in systemd.render_timer(_seat(tmp_path, 30))


def test_systemd_timer_is_not_persistent(systemd, tmp_path) -> None:
    """`Persistent=true` would make a box that was off for a week fire every missed turn at once —
    a token stampede on boot. Absence here is a decision, so it is asserted."""
    assert "Persistent" not in systemd.render_timer(_seat(tmp_path))


def test_systemd_env_and_argv_are_quoted(systemd, tmp_path) -> None:
    spec = DaemonSpec(
        label="q", argv=["/usr/bin/levain", "run", "--task", "a b c"],
        working_dir=tmp_path, env={"MSG": 'he said "hi"', "P": "/a b"},
        stdout_log=tmp_path / "o", stderr_log=tmp_path / "e",
    )
    unit = systemd.render_unit(spec)
    assert "'a b c'" in unit                       # argv shell-quoted
    assert r'Environment="MSG=he said \"hi\""' in unit  # embedded quote escaped, not terminating


def test_systemd_refuses_a_control_char_in_a_unit_value(systemd, tmp_path) -> None:
    """A newline in a value would END the directive and inject a new one — in the file that defines
    what runs unattended. FAIL CLOSED, the same call the seatbelt provider makes for SBPL."""
    spec = DaemonSpec(
        label="x", argv=["/usr/bin/levain"], working_dir=tmp_path,
        env={"EVIL": "a\nExecStartPost=/bin/rm -rf /"},
        stdout_log=tmp_path / "o", stderr_log=tmp_path / "e",
    )
    with pytest.raises(DaemonError):
        systemd.render_unit(spec)


def test_systemd_install_writes_both_units_and_enables_the_timer(systemd, tmp_path, monkeypatch) -> None:
    fake = _FakeSystemctl(show={"*": {"LoadState": "loaded", "ActiveState": "active",
                                      "SubState": "waiting", "MainPID": "0",
                                      "ExecMainStatus": "0"}})
    monkeypatch.setattr(daemon, "_run", fake)
    out = systemd.install(_seat(tmp_path))
    assert (systemd.UNIT_DIR / "levain-seat.service").exists()
    assert (systemd.UNIT_DIR / "levain-seat.timer").exists()
    assert "daemon-reload" in fake.verbs
    # the TIMER is what gets enabled — enabling the service would run it once and never again
    assert ["systemctl", "--user", "enable", "levain-seat.timer"] in fake.calls
    # ...and RESTARTED, not `enable --now`: --now leaves an already-active unit on its old
    # definition, so a reinstall with a new cadence or task never took effect (L3 2026-09-30).
    assert ["systemctl", "--user", "restart", "levain-seat.timer"] in fake.calls
    assert "idle between turns is NORMAL" in out


def test_systemd_install_requests_lingering_and_reports_a_refusal(systemd, tmp_path, monkeypatch) -> None:
    """⚠ WITHOUT LINGERING A USER UNIT DIES AT LOGOUT — spore-418's "always-on agent that dies when
    the laptop lid closes", reappearing as a session lifetime. A refusal must be SURFACED, never
    swallowed into a green install line."""
    fake = _FakeSystemctl(rc_for={"loginctl": 1},
                          show={"*": {"LoadState": "loaded", "ActiveState": "active",
                                      "SubState": "running", "MainPID": "42",
                                      "ExecMainStatus": "0"}})
    monkeypatch.setattr(daemon, "_run", fake)
    out = systemd.install(_resident(tmp_path))
    assert ["loginctl", "enable-linger"] in fake.calls
    assert "could NOT enable lingering" in out
    assert "will NOT run while you are logged out" in out


def test_systemd_status_reports_the_TIMER_for_a_seat(systemd, tmp_path, monkeypatch) -> None:
    """A seat's SERVICE is inactive between turns — that is health, not failure. Reporting on the
    service would call a perfectly working seat "inactive" in the command an operator uses to check
    on it. The timer is the thing that must be active."""
    systemd.UNIT_DIR.mkdir(parents=True)
    (systemd.UNIT_DIR / "levain-seat.service").write_text("x")
    (systemd.UNIT_DIR / "levain-seat.timer").write_text("x")
    fake = _FakeSystemctl(show={"levain-seat.timer": {"LoadState": "loaded",
                                                      "ActiveState": "active",
                                                      "SubState": "waiting", "MainPID": "0",
                                                      "ExecMainStatus": "0"}})
    monkeypatch.setattr(daemon, "_run", fake)
    st = systemd.status("levain-seat")
    assert any(c[3] == "levain-seat.timer" for c in fake.calls if c[0] == "systemctl")
    assert st.installed is True
    # a timer is `active (waiting)` with MainPID=0 FOREVER — that is healthy, and it is not "running"
    assert st.running is False
    assert st.load_state == "loaded"


def test_systemd_status_unknown_when_the_user_manager_is_unreachable(systemd, tmp_path, monkeypatch) -> None:
    """⛔ NO-DATA IS NOT NO-EVENT. `systemctl --user` fails outright over ssh without lingering, or
    in a container with no systemd. Calling that "not loaded" is the same violation the launchd
    provider guards against for an unreadable Aqua domain."""
    systemd.UNIT_DIR.mkdir(parents=True)
    (systemd.UNIT_DIR / "levain-cockpit.service").write_text("x")
    monkeypatch.setattr(daemon, "_run", _FakeSystemctl(rc_for={"show": 1}))
    st = systemd.status("levain-cockpit")
    assert st.load_state == "unknown"
    assert st.running is False
    assert "cannot reach" in st.detail


def test_systemd_uninstall_removes_the_timer_too(systemd, tmp_path, monkeypatch) -> None:
    """An orphan timer left behind stays armed to start a unit that no longer exists — systemd then
    reports a failing timer forever, and a later resident install gets two schedulers on one
    service."""
    systemd.UNIT_DIR.mkdir(parents=True)
    (systemd.UNIT_DIR / "levain-seat.service").write_text("x")
    (systemd.UNIT_DIR / "levain-seat.timer").write_text("x")
    fake = _FakeSystemctl()
    monkeypatch.setattr(daemon, "_run", fake)
    out = systemd.uninstall("levain-seat")
    assert not (systemd.UNIT_DIR / "levain-seat.service").exists()
    assert not (systemd.UNIT_DIR / "levain-seat.timer").exists()
    # the timer is disabled BEFORE the service
    disabled = [c[4] for c in fake.calls if len(c) > 4 and c[2] == "disable"]
    assert disabled.index("levain-seat.timer") < disabled.index("levain-seat.service")
    assert "levain-seat.timer" in out


def test_systemd_would_install_diffs_the_timer_too(systemd, tmp_path, monkeypatch) -> None:
    """A pure CADENCE edit changes ONLY the timer. Diffing just the service would report "no change"
    for the single field a seat's operator is most likely to be changing."""
    systemd.UNIT_DIR.mkdir(parents=True)
    spec = _seat(tmp_path, 900)
    (systemd.UNIT_DIR / "levain-seat.service").write_text(systemd.render_unit(spec))
    (systemd.UNIT_DIR / "levain-seat.timer").write_text(systemd.render_timer(spec))
    monkeypatch.setattr(daemon, "_run", _FakeSystemctl(
        show={"*": {"LoadState": "loaded", "ActiveState": "active", "SubState": "waiting",
                    "MainPID": "0", "ExecMainStatus": "0"}}))
    assert systemd.would_install(spec).would_change is False
    assert systemd.would_install(_seat(tmp_path, 60)).would_change is True


def test_systemd_install_drops_an_orphan_timer_on_a_shape_change(systemd, tmp_path, monkeypatch) -> None:
    """periodic -> resident. A left-behind timer would keep firing the service on the OLD cadence
    alongside the new resident unit — two schedulers driving one service, which reads to an operator
    as "it randomly restarts"."""
    systemd.UNIT_DIR.mkdir(parents=True)
    (systemd.UNIT_DIR / "levain-cockpit.timer").write_text("stale")
    monkeypatch.setattr(daemon, "_run", _FakeSystemctl(
        show={"*": {"LoadState": "loaded", "ActiveState": "active", "SubState": "running",
                    "MainPID": "7", "ExecMainStatus": "0"}}))
    systemd.install(_resident(tmp_path))
    assert not (systemd.UNIT_DIR / "levain-cockpit.timer").exists()


def test_systemd_install_rolls_back_to_the_prior_unit_on_failure(systemd, tmp_path, monkeypatch) -> None:
    """Same transactional floor as launchd: a failed enable must not destroy a prior good unit."""
    systemd.UNIT_DIR.mkdir(parents=True)
    prior = "[Unit]\nDescription=PRIOR\n"
    (systemd.UNIT_DIR / "levain-cockpit.service").write_text(prior)
    monkeypatch.setattr(daemon, "_run", _FakeSystemctl(rc_for={"enable": 1}))
    with pytest.raises(DaemonError, match="rolled back"):
        systemd.install(_resident(tmp_path))
    assert (systemd.UNIT_DIR / "levain-cockpit.service").read_text() == prior


def test_systemd_rollback_disables_the_service_a_failed_shape_change_enabled(
        systemd, tmp_path, monkeypatch) -> None:
    """periodic -> resident, enable fails: the restored periodic service file has no [Install], so the
    default.target.wants link the failed enable made would start the service at login beside its
    restored timer unless the rollback disables it (Diogenes LOW 2026-10-02; untested per 10-03)."""
    systemd.UNIT_DIR.mkdir(parents=True)
    (systemd.UNIT_DIR / "levain-cockpit.service").write_text("[Unit]\nDescription=PRIOR\n")
    (systemd.UNIT_DIR / "levain-cockpit.timer").write_text("[Timer]\nOnUnitActiveSec=900\n")
    fake = _FakeSystemctl(rc_for={"enable": 1})
    monkeypatch.setattr(daemon, "_run", fake)
    with pytest.raises(DaemonError, match="rolled back"):
        systemd.install(_resident(tmp_path))
    assert ["systemctl", "--user", "disable", "levain-cockpit.service"] in fake.calls


@pytest.mark.parametrize("rollback_rc,said", [(0, "re-enabled levain-cockpit.timer"),
                                              (1, "re-enabling levain-cockpit.timer FAILED")])
def test_systemd_rollback_reports_whether_the_prior_unit_was_re_enabled(
        systemd, tmp_path, monkeypatch, rollback_rc, said) -> None:
    """The rollback's own enable can fail too; the error must not claim a re-enable it did not get
    (L3 codex 2026-10-03, run: every enable failed and the message still said re-enabled)."""
    systemd.UNIT_DIR.mkdir(parents=True)
    (systemd.UNIT_DIR / "levain-cockpit.service").write_text("[Unit]\nDescription=PRIOR\n")
    (systemd.UNIT_DIR / "levain-cockpit.timer").write_text("[Timer]\nOnUnitActiveSec=900\n")
    monkeypatch.setattr(daemon, "_run", _FakeSystemctl(rc_for={"enable": [1, rollback_rc]}))
    with pytest.raises(DaemonError) as exc:
        systemd.install(_resident(tmp_path))
    assert said in str(exc.value)


# --- systemd's OWN parser is the oracle, not our string assertions ----------------------------

_HAS_SYSTEMD_ANALYZE = shutil.which("systemd-analyze") is not None
_systemd_live = pytest.mark.skipif(
    not (platform.system() == "Linux" and _HAS_SYSTEMD_ANALYZE),
    # ⛔ THE REMEDY IS NAMED IN THE SKIP REASON ON PURPOSE — this is the only line the person who
    # needs it is guaranteed to read. On 2026-09-04 the overnight reviewer skipped these two tests
    # and reported "I HAVE NO LINUX HOST", which was TRUE and became a false constraint: an image
    # that runs them had been on the build machine for a day. Nobody goes looking for a container
    # they do not know exists. `tests/linux/Dockerfile` is committed for exactly this moment.
    reason=("needs a Linux host with systemd-analyze — you can get one: the build and run commands "
            "are in the header of tests/linux/Dockerfile"),
)


@_systemd_live
@pytest.mark.parametrize("periodic", [False, True])
def test_systemd_rendered_units_pass_systemd_analyze_verify(tmp_path, monkeypatch, periodic) -> None:
    """⚠ EVERY OTHER TEST IN THIS SECTION CHECKS THAT OUR STRINGS SAY WHAT WE INTENDED. None of them
    can tell whether SYSTEMD accepts the file — a unit can satisfy all of them and still be rejected
    at load, which is the mechanism-correct/meaning-wrong class. This hands the rendered units to
    systemd's own parser.

    ``systemd-analyze verify`` fails on an ExecStart binary it cannot find, so a stub is placed on
    PATH first — otherwise this asserts "levain is installed", not "the unit is valid"."""
    monkeypatch.setattr(SystemdUserProvider, "UNIT_DIR", tmp_path / "units")
    p = SystemdUserProvider()
    stub_dir = tmp_path / "bin"
    stub_dir.mkdir()
    stub = stub_dir / "levain-stub"
    stub.write_text("#!/bin/sh\nexit 0\n")
    stub.chmod(0o755)

    spec = DaemonSpec(
        label="levain-verify", argv=[str(stub), "run", "--task", "a task with spaces"],
        working_dir=tmp_path, env={"PATH": "/usr/bin:/bin", "MSG": 'quoted "value"'},
        stdout_log=tmp_path / "o.log", stderr_log=tmp_path / "e.log",
        run_at_login=not periodic, keep_alive=not periodic,
        start_interval=900 if periodic else None,
    )
    units = {"levain-verify.service": p.render_unit(spec)}
    timer = p.render_timer(spec)
    if timer is not None:
        units["levain-verify.timer"] = timer
    d = tmp_path / "units"
    d.mkdir(parents=True, exist_ok=True)
    for name, text in units.items():
        (d / name).write_text(text)
    for name in units:
        proc = subprocess.run(
            ["systemd-analyze", "verify", "--user", str(d / name)],
            capture_output=True, text=True,
        )
        # A container has no system bus; that is environmental, not a unit defect. And `--user`
        # verify loads every user unit on the host, so a real host prints warnings about OTHER
        # units, each prefixed with that unit's own path (argushub, 2026-10-01: "argus-*.service:3:
        # Invalid URL"). Only lines about the units written here count.
        def _about_another_unit(ln: str) -> bool:
            head = ln.split(":", 1)[0]
            return head.startswith("/") and not head.startswith(str(d))
        errs = [ln for ln in (proc.stderr or "").splitlines()
                if ln.strip() and "Failed to connect to system bus" not in ln
                and not _about_another_unit(ln)]
        assert not errs, f"{name} rejected by systemd: {errs}"


# --- the unit-injection guard covers every directive, not just Environment= --------------------


def _injection_seat(tmp_path, task="review the diff", interval=3600):
    return build_seat_spec(entity_path=tmp_path, task=task, interval=interval)


def test_a_multiline_task_is_REFUSED_not_rendered_into_a_broken_unit(tmp_path) -> None:
    """⛔ THE FIELD THE GUARD WAS WRITTEN FOR WAS THE ONE FIELD IT DID NOT COVER.

    `_systemd_escape`'s control-character refusal was applied to `Environment=`, whose values levain
    GENERATES (PATH/HOME/PYTHONUNBUFFERED — the least attacker-reachable input in the function), and
    omitted from `ExecStart`, which carries `--task`: free text the OPERATOR types, `required=True`
    at the CLI.

    `shlex.quote` does not save it — that is SHELL quoting, not systemd's, and it closes its quote at
    the END of the argument, so an embedded newline leaves every earlier line inside an open quote.
    Before this fix the render produced an `ExecStart` truncated mid-task with `--unattended`,
    `--max-seconds` and `--consolidate` on an orphan line outside every directive."""
    with pytest.raises(DaemonError) as exc:
        SystemdUserProvider().render_unit(_injection_seat(tmp_path, task="review the diff\nand report"))
    assert "ExecStart" in str(exc.value)
    assert "control character" in str(exc.value)


def test_launchd_is_unaffected_which_is_why_the_gap_survived(tmp_path) -> None:
    """The IDENTICAL spec renders fine on macOS: a plist `<string>` carries a newline inertly. That
    asymmetry is why a shared-looking `DaemonSpec` hid a Linux-only injection — and it is also why
    the refusal lives in the systemd renderer rather than in `build_seat_spec`, which would have
    taken a working macOS capability away to satisfy a systemd constraint."""
    unit = LaunchdProvider().render_unit(_injection_seat(tmp_path, task="review the diff\nand report"))
    assert "review the diff" in unit


@pytest.mark.parametrize("field,kw", [
    ("an ExecStart argument", {"task": "a\nb"}),
])
def test_every_directive_bearing_value_is_refused(tmp_path, field, kw) -> None:
    with pytest.raises(DaemonError) as exc:
        SystemdUserProvider().render_unit(_injection_seat(tmp_path, **kw))
    assert field in str(exc.value)


def test_the_timer_label_is_guarded_too_because_Unit_names_the_service(tmp_path) -> None:
    """`render_timer` puts the label in TWO directives, and `Unit=` is the one naming the service the
    timer fires. A guard on the service alone would leave the timer injectable."""
    spec = _injection_seat(tmp_path)
    # Refused at construction now (the label is a file name too)...
    with pytest.raises(DaemonError):
        replace(spec, label="seat\nExecStart=/bin/sh")
    # ...and the renderers' own guard still holds for a spec that got past it some other way.
    bad = replace(spec)
    object.__setattr__(bad, "label", "seat\nExecStart=/bin/sh")
    with pytest.raises(DaemonError):
        SystemdUserProvider().render_timer(bad)
    with pytest.raises(DaemonError):
        SystemdUserProvider().render_unit(bad)


def test_an_ordinary_single_line_task_still_renders(tmp_path) -> None:
    """The CONTROL. A refusal that also refused the normal case would be caught by nothing else
    here — every other test in this block asserts a raise."""
    unit = SystemdUserProvider().render_unit(_injection_seat(tmp_path, task="review the diff"))
    assert "--task 'review the diff'" in unit
    assert "--unattended" in unit
    # and the governance flags are INSIDE the ExecStart line, which is the property the whole
    # finding was about
    exec_line = [ln for ln in unit.splitlines() if ln.startswith("ExecStart=")][0]
    for flag in ("--unattended", "--max-seconds", "--consolidate"):
        assert flag in exec_line, f"{flag} left the ExecStart directive"


# ---------- L3 2026-09-30 (the K4c port): labels, escaping, reinstall, seat status ----------


@pytest.mark.parametrize("label", ["../escape", "a/b", "seat@1", "100%", ".hidden", "x..y", "", "a" * 129])
def test_a_label_that_is_not_a_plain_name_is_refused_on_both_platforms(tmp_path, label) -> None:
    """The label is a FILE NAME on both platforms; ``../x`` would write, or for uninstall delete,
    outside the unit directory. Refused at the spec AND at every provider's path helper."""
    with pytest.raises(DaemonError):
        replace(_seat(tmp_path), label=label)
    with pytest.raises(DaemonError):
        SystemdUserProvider()._service_path(label)
    with pytest.raises(DaemonError):
        daemon.LaunchdProvider()._plist_path(label)


def test_the_shipped_labels_are_still_accepted(tmp_path) -> None:
    for label in (daemon.DEFAULT_LABEL, daemon.DEFAULT_SEAT_LABEL, "levain-seat", "a_b-c.d"):
        assert replace(_seat(tmp_path), label=label).label == label


def test_execstart_escapes_what_systemd_would_otherwise_rewrite(tmp_path) -> None:
    """MEASURED on systemd 255: with shlex.quote alone, ``%n`` became the unit name, ``${PATH}`` was
    expanded and ``\\n`` became a newline. Doubled, every argument arrived byte-exact."""
    spec = replace(_seat(tmp_path), argv=["/usr/bin/levain", "run", "--task", "90% of ${PATH} a\\nb %n"])
    unit = SystemdUserProvider().render_unit(spec)
    line = next(ln for ln in unit.splitlines() if ln.startswith("ExecStart="))
    assert "90%% of $${PATH} a\\\\nb %%n" in line


def test_a_seat_service_has_no_install_section(tmp_path) -> None:
    """Only the timer starts a seat; an [Install] section let a stale enablement start it at login."""
    assert "[Install]" not in SystemdUserProvider().render_unit(_seat(tmp_path))
    assert "[Install]" in SystemdUserProvider().render_unit(_resident(tmp_path))


def test_resident_to_periodic_stops_the_resident_service(systemd, tmp_path, monkeypatch) -> None:
    fake = _FakeSystemctl(show={"*": {"LoadState": "loaded", "ActiveState": "active",
                                      "SubState": "waiting", "MainPID": "0", "ExecMainStatus": "0"}})
    monkeypatch.setattr(daemon, "_run", fake)
    seat = _seat(tmp_path)
    systemd.UNIT_DIR.mkdir(parents=True)
    (systemd.UNIT_DIR / f"{seat.label}.service").write_text("old resident unit\n")
    systemd.install(seat)
    assert ["systemctl", "--user", "disable", "--now", f"{seat.label}.service"] in fake.calls


def test_seat_status_reads_the_turn_from_the_service_not_the_timer(systemd, tmp_path, monkeypatch) -> None:
    """A timer has no MainPID or exit status; a seat whose every turn failed read as healthy."""
    systemd.UNIT_DIR.mkdir(parents=True)
    (systemd.UNIT_DIR / "levain-seat.timer").write_text("t\n")
    (systemd.UNIT_DIR / "levain-seat.service").write_text("s\n")
    fake = _FakeSystemctl(show={
        "levain-seat.timer": {"LoadState": "loaded", "ActiveState": "active", "SubState": "waiting"},
        "levain-seat.service": {"MainPID": "0", "ExecMainStatus": "3", "Result": "exit-code"},
    })
    monkeypatch.setattr(daemon, "_run", fake)
    st = systemd.status("levain-seat")
    assert "last exit = 3" in st.detail and st.running is False


def test_an_old_systemd_is_refused_before_anything_is_written(systemd, tmp_path, monkeypatch) -> None:
    def old(cmd, *, check=False):  # noqa: ANN001
        return subprocess.CompletedProcess(cmd, 0, stdout="systemd 239 (239-58.el8)\n+PAM", stderr="")
    monkeypatch.setattr(daemon, "_run", old)
    with pytest.raises(DaemonError, match="240"):
        systemd.install(_seat(tmp_path))
    assert not (systemd.UNIT_DIR / "levain-seat.service").exists()


@pytest.mark.parametrize("provider", [daemon.SystemdUserProvider, daemon.LaunchdProvider])
def test_restart_refuses_an_invalid_label_before_the_service_manager_sees_it(provider, monkeypatch):
    """Reproduced 2026-10-01 with the manager stubbed: `restart('*')` sent
    `systemctl --user restart '*.service'` (a glob over every loaded user unit) and
    `launchctl kickstart -k gui/<uid>/*`. restart builds no unit path, so the label check
    the other verbs get through their path helpers never ran."""
    calls: list[list[str]] = []
    monkeypatch.setattr(daemon, "_run", lambda cmd, check: calls.append(cmd))
    monkeypatch.setattr(daemon, "_refuse_root", lambda: None)
    for bad in ("*", "../x", "a@b"):
        with pytest.raises(daemon.DaemonError):
            provider().restart(bad)
    assert calls == []


def test_systemd_reinstall_during_a_turn_says_that_turn_runs_the_old_definition(
        systemd, tmp_path, monkeypatch) -> None:
    """Reproduced 2026-10-01 on argushub (systemd 255, a real seat): a reinstall with a new task
    while a turn ran left that turn finishing the OLD task, and the summary printed its pid as the
    idle timer's. Restarting the timer never touches a running oneshot service."""
    running = {"*": {"LoadState": "loaded", "ActiveState": "activating", "SubState": "start",
                     "MainPID": "4242", "ExecMainStatus": "0"}}
    monkeypatch.setattr(daemon, "_run", _FakeSystemctl(show=running))
    proc = tmp_path / "proc"
    monkeypatch.setattr(SystemdUserProvider, "PROC", proc)
    (proc / "4242").mkdir(parents=True)
    (proc / "4242" / "cmdline").write_bytes(b"\0".join(a.encode() for a in _seat(tmp_path).argv) + b"\0")
    same = systemd.install(_seat(tmp_path))
    assert "PREVIOUS definition" not in same           # the turn runs exactly this argv
    changed = replace(_seat(tmp_path), argv=["/usr/bin/levain", "run", "--task", "y"])
    out = systemd.install(changed)
    assert "PREVIOUS definition" in out and "4242" in out
    # L3 (complement): a SECOND install of the same new definition, the old turn still running
    again = systemd.install(changed)
    assert "PREVIOUS definition" in again
    # L3 (codex): a turn that started after the reload already runs the new argv
    (proc / "4242" / "cmdline").write_bytes(b"\0".join(a.encode() for a in changed.argv) + b"\0")
    assert "PREVIOUS definition" not in systemd.install(changed)


def test_systemd_reinstall_during_a_turn_reads_a_shebang_cmdline(systemd, tmp_path, monkeypatch):
    """Reproduced 2026-10-02 (Diogenes, Linux container): the default argv is the `levain` console
    script, a shebang script, so the turn's /proc cmdline reads `<python> <script> <args...>`. The
    exact comparison called every identical reinstall "another argv" and told the operator to stop
    the turn."""
    running = {"*": {"LoadState": "loaded", "ActiveState": "activating", "SubState": "start",
                     "MainPID": "4242", "ExecMainStatus": "0"}}
    monkeypatch.setattr(daemon, "_run", _FakeSystemctl(show=running))
    proc = tmp_path / "proc"
    monkeypatch.setattr(SystemdUserProvider, "PROC", proc)
    (proc / "4242").mkdir(parents=True)
    seat = _seat(tmp_path)
    kernel_view = ["/home/entity/venv/bin/python3", *seat.argv]
    (proc / "4242" / "cmdline").write_bytes(b"\0".join(a.encode() for a in kernel_view) + b"\0")
    assert "PREVIOUS definition" not in systemd.install(seat)
    changed = replace(seat, argv=[*seat.argv[:-1], "a different task"])
    assert "PREVIOUS definition" in systemd.install(changed)


# --- head ruling 2026-10-07 (L2 MED): a daemon's logs are private, and hold no token ----------------------------


def test_every_unit_runs_with_a_private_umask():
    spec = build_spec(install_path=Path("/tmp/inst"), port=7420, label="com.levainhq.t")
    assert plistlib.loads(LaunchdProvider().render_unit(spec).encode())["Umask"] == 0o077
    assert "UMask=0077" in SystemdUserProvider().render_unit(spec).splitlines()


def test_the_log_directory_and_files_are_created_private_under_a_linux_style_path(tmp_path, monkeypatch):
    import os
    import stat

    import levain.daemon as d

    monkeypatch.setattr(d.platform, "system", lambda: "Linux")
    monkeypatch.setenv("HOME", str(tmp_path))
    old = os.umask(0o022)   # the wide default a login shell hands a user unit
    try:
        spec = build_spec(install_path=Path("/tmp/inst"), label="com.levainhq.t")
        assert spec.stdout_log.parent == tmp_path / ".local" / "state" / "levain"
        d._prepare_private_logs(spec)
        assert stat.S_IMODE(spec.stdout_log.parent.stat().st_mode) == 0o700
        for log in (spec.stdout_log, spec.stderr_log):
            assert stat.S_IMODE(log.stat().st_mode) == 0o600
        # an existing, wider Levain-owned directory and log (an older unit's) are narrowed back
        os.chmod(spec.stdout_log.parent, 0o755)
        os.chmod(spec.stdout_log, 0o644)
        d._prepare_private_logs(spec)
        assert stat.S_IMODE(spec.stdout_log.parent.stat().st_mode) == 0o700
        assert stat.S_IMODE(spec.stdout_log.stat().st_mode) == 0o600
    finally:
        os.umask(old)


def test_the_threat_model_note_no_longer_says_there_is_no_token():
    from levain.daemon import THREAT_MODEL_NOTE

    assert "no token" not in THREAT_MODEL_NOTE and "--open-running" in THREAT_MODEL_NOTE


def test_a_symlinked_log_path_is_never_followed(tmp_path, monkeypatch):
    """complement L3: a dangling symlink at a log path made the O_CREAT follow it and create its target."""
    import levain.daemon as d

    monkeypatch.setenv("HOME", str(tmp_path))
    spec = build_spec(install_path=Path("/tmp/inst"), label="com.levainhq.t", log_dir=tmp_path / "logs")
    (tmp_path / "logs").mkdir(mode=0o700)
    target = tmp_path / "elsewhere.txt"
    spec.stdout_log.symlink_to(target)
    with pytest.raises(d.DaemonError, match="symlink"):   # codex L3: the service would follow it later
        d._prepare_private_logs(spec)
    assert not target.exists()

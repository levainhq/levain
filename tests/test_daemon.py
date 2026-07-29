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
    DaemonSpec,
    LaunchdProvider,
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

"""The S1 services (CONTRACT §1.3, §23): LaunchDaemon plists (macOS) and systemd units (Linux), and the
Steps that install, start, stop and remove them.

Each runs the root interpreter isolated (:data:`levain.slot.root_tree.PY_FLAGS`) on the shared
service entry script (:func:`levain.slot.layout.service_entry_path`), the same bootstrap the helpers
use (§23): the root-owned import tree is the one path added, then
:mod:`levain.slot.service_boot` checks the service's run dir and runs its program,
``levain/slot/services/<name>.py``, as its own uid. Setup installs a unit only when that program
exists in the package it copies (§23); the plan names every skipped one in ``pending``.

Start order: systemd orders by ``After=`` (with ``Wants=``, so a dependency is pulled in but its
restart does not restart the dependant). launchd has no ordering key, so on macOS the order is the
order setup bootstraps the jobs in; at boot launchd starts them together, and each non-root service
exits until its run dir is there (§22), restarting under ``KeepAlive``.

The reaper is ONE long-running root service; its pass interval and its wake socket are inside its
program, so no unit here carries a timer or a start interval (§23).
"""
from __future__ import annotations

import plistlib
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Literal

from levain.firing.hands import HostOS, Step, _abs
from levain.slot import Refused
from levain.slot.layout import ROOT_GROUP, service_entry_path
from levain.slot.root_tree import PY_FLAGS, ROOT_PYTHON

LAUNCHD_DIR = Path("/Library/LaunchDaemons")
SYSTEMD_DIR = Path("/etc/systemd/system")


@dataclass(frozen=True)
class Service:
    name: str                              # the program's name under levain/slot/services/
    title: str                             # §1.3 "Service" column
    user: str                              # §1.3 "Uid" column
    after: tuple[str, ...]                 # §1.3 "Starts after" (names); () = boot
    restart: Literal["always", "one-shot"]
    hosts: tuple[HostOS, ...] = ("darwin", "linux")


#: CONTRACT §1.3, in start order.
SERVICES: tuple[Service, ...] = (
    Service("control", "CONTROL", "_levain_control", (), "always"),
    Service("keys", "gw-keys", "_levain_keys", ("control",), "always"),
    Service("qbroker", "QUEUE BROKER", "_levain_qbroker", ("control",), "always"),
    Service("dispatch", "dispatch", "_levain_dispatch", ("control", "keys"), "always"),
    Service("reaper", "reaper", "root", ("control",), "always"),
    Service("pf", "pf anchor", "root", (), "one-shot", hosts=("darwin",)),
)


def services_for(host: HostOS) -> tuple[Service, ...]:
    if host not in ("darwin", "linux"):
        raise Refused("unsupported-host", repr(host))
    return tuple(s for s in SERVICES if host in s.hosts)


def by_name(name: str) -> Service:
    for s in SERVICES:
        if s.name == name:
            return s
    raise Refused("bad-token", f"service: {name!r}")


def program_relpath(svc: Service) -> str:
    return f"levain/slot/services/{svc.name}.py"


def program_source(package: Path, svc: Service) -> Path:
    """The program inside the ``levain`` package directory ``package`` (the one setup copies)."""
    return package / "slot" / "services" / f"{svc.name}.py"


def built_services(host: HostOS, package: Path, *,
                   exists: Callable[[Path], bool] = Path.is_file) -> tuple[tuple[Service, ...], tuple[str, ...]]:
    """(the services whose program exists in ``package``, a ``pending`` line for each that does not)."""
    have: list[Service] = []
    skipped: list[str] = []
    for s in services_for(host):
        if exists(program_source(package, s)):
            have.append(s)
        else:
            unit = launchd_label(s) if host == "darwin" else systemd_name(s)
            skipped.append(f"{program_relpath(s)} (the {s.title} program) does not exist yet: {unit} not installed")
    return tuple(have), tuple(skipped)


def argv(host: HostOS, svc: Service) -> tuple[str, ...]:
    return (ROOT_PYTHON[host], PY_FLAGS, str(service_entry_path(host)), svc.name)


def _group(host: HostOS, svc: Service) -> str:
    return ROOT_GROUP[host] if svc.user == "root" else svc.user   # every account's own primary group (§1.1)


# --- macOS -------------------------------------------------------------------------------------------


def launchd_label(svc: Service) -> str:
    return f"com.levain.{svc.name}"


def launchd_path(svc: Service) -> Path:
    return LAUNCHD_DIR / f"{launchd_label(svc)}.plist"


def launchd_plist_text(svc: Service) -> str:
    job = {
        "Label": launchd_label(svc),
        "ProgramArguments": list(argv("darwin", svc)),
        "UserName": svc.user,
        "GroupName": _group("darwin", svc),
        "InitGroups": True,     # the account's §1.2 supplementary groups
        "RunAtLoad": True,
        # "always": restarted whenever it exits. "one-shot": restarted only while it exits non-zero, so a
        # boot load that failed once is retried until it succeeds instead of leaving its work undone.
        "KeepAlive": True if svc.restart == "always" else {"SuccessfulExit": False},
    }
    return plistlib.dumps(job, sort_keys=True).decode("utf-8")


# --- Linux -------------------------------------------------------------------------------------------


def systemd_name(svc: Service) -> str:
    return f"levain-{svc.name}.service"


def systemd_path(name: str) -> Path:
    return SYSTEMD_DIR / name


def systemd_unit_text(svc: Service) -> str:
    if "linux" not in svc.hosts:
        raise Refused("unsupported-host", f"{svc.name} is macOS only")
    deps = " ".join(systemd_name(by_name(a)) for a in svc.after)
    unit = [f"Description=Levain {svc.title} (CONTRACT §1.3)"]
    if deps:
        unit += [f"After={deps}", f"Wants={deps}"]
    unit.append("StartLimitIntervalSec=0")   # "always" means always: no start-rate limit gives up on it
    lines = [
        "# Written by the Levain install helper; removed by its undo verb.",
        "[Unit]", *unit, "",
        "[Service]", "Type=simple", f"User={svc.user}", f"Group={_group('linux', svc)}",
        f"ExecStart={' '.join(argv('linux', svc))}",
        # A service that keeps failing backs off step by step instead of restarting at a fixed short interval.
        "Restart=always", "RestartSec=1", "RestartSteps=5", "RestartMaxDelaySec=30", "",
        "[Install]", "WantedBy=multi-user.target", "",
    ]
    return "\n".join(lines)


# --- steps -------------------------------------------------------------------------------------------


def start_order(host: HostOS, services: Iterable[Service] | None = None) -> tuple[str, ...]:
    """The unit names setup starts, in order (macOS labels / Linux unit names)."""
    chosen = services_for(host) if services is None else tuple(services)
    names = {s.name for s in chosen}
    ordered = [s for s in services_for(host) if s.name in names]
    return tuple(launchd_label(s) if host == "darwin" else systemd_name(s) for s in ordered)


def _launchd_job_absent(label: str, run: Callable[..., object] | None = None) -> tuple[bool, str]:
    import subprocess

    r = (run or subprocess.run)(["/bin/launchctl", "print", f"system/{label}"], capture_output=True)
    loaded = getattr(r, "returncode", 1) == 0
    return (not loaded, f"{label} is still loaded" if loaded else "")


def _read_or_none(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None


def _differs(read: Callable[[Path], bytes | None], path: Path, text: str) -> bool:
    return read(path) != text.encode("utf-8")


def _unload_launchd_if_changed(label: str, path: Path, text: str, run: Callable[..., object] | None,
                               read: Callable[[Path], bytes | None]) -> tuple[bool, str]:
    """Boot out a loaded job whose plist on disk differs from the one about to be written. launchd keeps
    running the definition it loaded, so the job must be out before the new file lands; the bootstrap
    step that follows the write loads the new one. Decided from disk when the step runs, with nothing
    carried in memory: wherever a run dies, the re-run finds either a loaded job whose plist still
    differs, which it boots out, or a job that is not loaded, which its bootstrap step loads from the
    new file."""
    import subprocess

    absent, _ = _launchd_job_absent(label, run)
    if absent or not _differs(read, path, text):
        return True, ""
    (run or subprocess.run)(["/bin/launchctl", "bootout", f"system/{label}"], capture_output=True)
    return _launchd_job_absent(label, run)


def _stop_unit_if_changed(systemctl: str, name: str, path: Path, text: str, run: Callable[..., object] | None,
                          read: Callable[[Path], bytes | None]) -> tuple[bool, str]:
    """Linux twin of :func:`_unload_launchd_if_changed`: stop a unit whose file on disk differs from the
    one about to be written. ``enable --now`` after ``daemon-reload`` starts a stopped unit on the new
    definition but leaves a running one on the old, so stopping it first is what makes the new definition
    take, and a run that dies after the stop leaves it stopped for the re-run to start.

    The stop is unconditional, never gated on ``is-active``: a ``Restart=always`` unit waiting out its
    restart delay reads as not active, and would come back on the old cached definition. An explicit
    stop cancels that pending restart (the stop-before-upgrade order of Debian's maintainer scripts).
    Success is read from ``ActiveState`` afterwards: ``inactive`` or ``failed``."""
    import subprocess

    # Only an identical file skips the stop. An absent file is no proof nothing runs: systemd keeps a
    # loaded unit whose file was deleted, and stopping a unit it never loaded is harmless.
    if not _differs(read, path, text):
        return True, ""
    run = run or subprocess.run
    run([systemctl, "stop", name], capture_output=True)   # a unit systemd never loaded: non-zero, harmless
    r = run([systemctl, "show", "-p", "ActiveState", "--value", name], capture_output=True, text=True)
    state = (getattr(r, "stdout", "") or "").strip()
    if state not in ("inactive", "failed"):
        return False, f"{name} is {state or 'in an unknown state'} after systemctl stop"
    return True, ""


def install_steps(host: HostOS, services: Iterable[Service], *, run: Callable[..., object] | None = None,
                  read: Callable[[Path], bytes | None] = _read_or_none) -> list[Step]:
    """Write each of ``services``' units, then start each in :func:`start_order` (``run``/``read``:
    launchctl or systemctl, and the unit file read). A running job whose file is about to change is
    unloaded (macOS) or stopped (Linux) just BEFORE its write, and the start steps bring it up on the new
    file; an unchanged running job is left running. Crash window: a run that dies between the unload and
    the start leaves that service down until setup or repair is run again."""
    names = {x.name for x in services}
    chosen = [s for s in services_for(host) if s.name in names]
    steps: list[Step] = []
    if host == "darwin":
        for s in chosen:
            label, path, text = launchd_label(s), launchd_path(s), launchd_plist_text(s)
            steps.append(Step(f"boot out {label} if it is loaded and {path} is about to change",
                              call=lambda lb=label, p=path, t=text: _unload_launchd_if_changed(lb, p, t, run, read)))
            steps.append(Step(f"write the LaunchDaemon {label} ({s.title}, as {s.user})",
                              write=(path, text, 0o644), validate=("/usr/bin/plutil", "-lint")))
        for s in chosen:
            label = launchd_label(s)
            steps.append(Step(f"start {label}", ("/bin/launchctl", "bootstrap", "system", str(launchd_path(s))),
                              skip_if=("/bin/launchctl", "print", f"system/{label}")))
        return steps
    if not chosen:
        return steps
    systemctl = _abs("systemctl")
    for s in chosen:
        name, text = systemd_name(s), systemd_unit_text(s)
        path = systemd_path(name)
        steps.append(Step(f"stop {name} if it is active and {path} is about to change",
                          call=lambda n=name, p=path, t=text: _stop_unit_if_changed(systemctl, n, p, t, run, read)))
        steps.append(Step(f"write the systemd unit {name} ({s.title}, as {s.user})", write=(path, text, 0o644)))
    steps.append(Step("tell systemd about the units", (systemctl, "daemon-reload")))
    for name in start_order(host, chosen):
        steps.append(Step(f"enable and start {name}", (systemctl, "enable", "--now", name)))
    return steps


def uninstall_steps(host: HostOS) -> list[Step]:
    """Stop every service in reverse start order, then remove the unit files. Every service, installed
    or not: each step is a no-op for one that is absent, so a failed undo re-runs."""
    steps: list[Step] = []
    if host == "darwin":
        for s in reversed(services_for(host)):
            label = launchd_label(s)
            steps += [
                Step(f"stop {label}", ("/bin/launchctl", "bootout", f"system/{label}"), allow_fail=True),
                Step(f"check {label} is stopped", call=lambda lb=label: _launchd_job_absent(lb)),
            ]
        steps += [Step(f"remove {launchd_path(s)}", ("/bin/rm", "-f", str(launchd_path(s)))) for s in services_for(host)]
        return steps
    systemctl, test, rm = _abs("systemctl"), _abs("test"), _abs("rm")
    for name in reversed(start_order(host)):
        steps.append(Step(f"stop and disable {name}", (systemctl, "disable", "--now", name),
                          skip_if=(test, "!", "-e", str(systemd_path(name)))))
    steps += [Step(f"remove {systemd_path(n)}", (rm, "-f", str(systemd_path(n)))) for n in start_order(host)]
    steps.append(Step("tell systemd", (systemctl, "daemon-reload")))
    return steps


__all__ = ["SERVICES", "Service", "argv", "built_services", "by_name", "install_steps", "launchd_label",
           "launchd_plist_text", "program_relpath", "program_source", "services_for", "start_order",
           "systemd_name", "systemd_unit_text", "uninstall_steps"]

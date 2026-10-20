"""The S1 services (CONTRACT §1.3): LaunchDaemon plists (macOS) and systemd units (Linux), and the
Steps that install, start, stop and remove them.

Each runs the root interpreter isolated (:data:`levain.slot.root_tree.PY_FLAGS`) on its program in
the root-owned import tree, ``<import tree>/levain/slot/services/<name>.py``, as its own uid. The
program bodies are not built yet (:data:`PENDING` names them in the plan output).

Start order: systemd orders by ``After=`` (with ``Wants=``, so a dependency is pulled in but its
restart does not restart the dependant). launchd has no ordering key, so on macOS the order is the
order setup bootstraps the jobs in; at boot launchd starts them together and each restarts until its
dependency answers (``KeepAlive``).
"""
from __future__ import annotations

import plistlib
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from levain.firing.hands import HostOS, Step, _abs
from levain.slot import Refused
from levain.slot.layout import IMPORT_TREE, ROOT_GROUP
from levain.slot.root_tree import PY_FLAGS, ROOT_PYTHON

LAUNCHD_DIR = Path("/Library/LaunchDaemons")
SYSTEMD_DIR = Path("/etc/systemd/system")
REAPER_TIMER_SECONDS = 60   # §1.3


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

PENDING = tuple(f"levain/slot/services/{s.name}.py (the {s.title} program) does not exist yet" for s in SERVICES)


def services_for(host: HostOS) -> tuple[Service, ...]:
    if host not in ("darwin", "linux"):
        raise Refused("unsupported-host", repr(host))
    return tuple(s for s in SERVICES if host in s.hosts)


def _by_name(name: str) -> Service:
    return next(s for s in SERVICES if s.name == name)


def program_path(host: HostOS, svc: Service) -> Path:
    return IMPORT_TREE[host] / "levain" / "slot" / "services" / f"{svc.name}.py"


def argv(host: HostOS, svc: Service) -> tuple[str, ...]:
    return (ROOT_PYTHON[host], PY_FLAGS, str(program_path(host, svc)))


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
        "KeepAlive": svc.restart == "always",
    }
    return plistlib.dumps(job, sort_keys=True).decode("utf-8")


# --- Linux -------------------------------------------------------------------------------------------


def systemd_name(svc: Service) -> str:
    return f"levain-{svc.name}.service"


def systemd_path(name: str) -> Path:
    return SYSTEMD_DIR / name


REAPER_TIMER = "levain-reaper.timer"


def systemd_unit_text(svc: Service) -> str:
    if "linux" not in svc.hosts:
        raise Refused("unsupported-host", f"{svc.name} is macOS only")
    deps = " ".join(systemd_name(_by_name(a)) for a in svc.after)
    unit = [f"Description=Levain {svc.title} (CONTRACT §1.3)"]
    if deps:
        unit += [f"After={deps}", f"Wants={deps}"]
    unit.append("StartLimitIntervalSec=0")   # "always" means always: no start-rate limit gives up on it
    lines = [
        "# Written by the Levain install helper; removed by its undo verb.",
        "[Unit]", *unit, "",
        "[Service]", "Type=simple", f"User={svc.user}", f"Group={_group('linux', svc)}",
        f"ExecStart={' '.join(argv('linux', svc))}", "Restart=always", "RestartSec=1", "",
        "[Install]", "WantedBy=multi-user.target", "",
    ]
    return "\n".join(lines)


def reaper_timer_text() -> str:
    return "\n".join([
        "# Written by the Levain install helper; removed by its undo verb.",
        "[Unit]", "Description=Levain reaper wake (CONTRACT §1.3)", "",
        "[Timer]", f"OnBootSec={REAPER_TIMER_SECONDS}", f"OnUnitActiveSec={REAPER_TIMER_SECONDS}",
        f"Unit={systemd_name(_by_name('reaper'))}", "",
        "[Install]", "WantedBy=timers.target", "",
    ])


# --- steps -------------------------------------------------------------------------------------------


def start_order(host: HostOS) -> tuple[str, ...]:
    """The unit names setup starts, in order (macOS labels / Linux unit names)."""
    out: list[str] = []
    for s in services_for(host):
        if host == "darwin":
            out.append(launchd_label(s))
        else:
            out.append(systemd_name(s))
            if s.name == "reaper":
                out.append(REAPER_TIMER)
    return tuple(out)


def _launchd_job_absent(label: str) -> tuple[bool, str]:
    import subprocess

    r = subprocess.run(["/bin/launchctl", "print", f"system/{label}"], capture_output=True)
    return (r.returncode != 0, f"{label} is still loaded" if r.returncode == 0 else "")


def install_steps(host: HostOS) -> list[Step]:
    """Write every unit, then start each in :func:`start_order`."""
    steps: list[Step] = []
    if host == "darwin":
        for s in services_for(host):
            steps.append(Step(f"write the LaunchDaemon {launchd_label(s)} ({s.title}, as {s.user})",
                              write=(launchd_path(s), launchd_plist_text(s), 0o644),
                              validate=("/usr/bin/plutil", "-lint")))
        for s in services_for(host):
            steps.append(Step(f"start {launchd_label(s)}", ("/bin/launchctl", "bootstrap", "system", str(launchd_path(s))),
                              skip_if=("/bin/launchctl", "print", f"system/{launchd_label(s)}")))
        return steps
    systemctl = _abs("systemctl")
    for s in services_for(host):
        steps.append(Step(f"write the systemd unit {systemd_name(s)} ({s.title}, as {s.user})",
                          write=(systemd_path(systemd_name(s)), systemd_unit_text(s), 0o644)))
    steps.append(Step(f"write the systemd timer {REAPER_TIMER}",
                      write=(systemd_path(REAPER_TIMER), reaper_timer_text(), 0o644)))
    steps.append(Step("tell systemd about the units", (systemctl, "daemon-reload")))
    for name in start_order(host):
        steps.append(Step(f"enable and start {name}", (systemctl, "enable", "--now", name)))
    return steps


def uninstall_steps(host: HostOS) -> list[Step]:
    """Stop every service in reverse start order, then remove the unit files."""
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


__all__ = ["PENDING", "REAPER_TIMER", "SERVICES", "Service", "argv", "install_steps", "launchd_label",
           "launchd_plist_text", "program_path", "reaper_timer_text", "services_for", "start_order",
           "systemd_name", "systemd_unit_text", "uninstall_steps"]

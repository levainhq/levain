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

import os
import plistlib
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Iterable, Literal

from levain.firing.hands import HostOS, Step, _abs
from levain.launch import child_env
from levain.slot import Refused
from levain.slot.layout import ROOT_GROUP, service_entry_path
from levain.slot.root_tree import PY_FLAGS, ROOT_PYTHON

if TYPE_CHECKING:
    from levain.slot.removal import Live

LAUNCHD_DIR = Path("/Library/LaunchDaemons")
SYSTEMD_DIR = Path("/etc/systemd/system")
#: The first line of every systemd unit :func:`systemd_unit_text` writes (every version since S1: ``git log -S`` on it
#: in this file). :func:`unit_state` reads a unit file as Levain's only when its first line is this.
SYSTEMD_HEADER = "# Written by the Levain install helper; removed by its undo verb."
#: sysexits.h EX_CONFIG: a service program's exit for an install that lacks what it needs; systemd does not restart it.
EX_CONFIG = 78


@dataclass(frozen=True)
class Service:
    name: str                              # the program's name under levain/slot/services/
    title: str                             # §1.3 "Service" column
    user: str                              # §1.3 "Uid" column
    after: tuple[str, ...]                 # §1.3 "Starts after" (names); () = boot
    restart: Literal["always", "on-failure", "one-shot"]
    hosts: tuple[HostOS, ...] = ("darwin", "linux")
    logs: bool = False                     # macOS: stdout and stderr kept in launchd_log_path, never thrown away


#: CONTRACT §1.3, in start order.
SERVICES: tuple[Service, ...] = (
    Service("control", "CONTROL", "_levain_control", (), "always"),
    Service("keys", "gw-keys", "_levain_keys", ("control",), "always"),
    Service("qbroker", "QUEUE BROKER", "_levain_qbroker", ("control",), "always"),
    Service("dispatch", "dispatch", "_levain_dispatch", ("control", "keys"), "on-failure"),
    Service("reaper", "reaper", "root", ("control",), "always"),
    Service("pf", "pf anchor", "root", (), "one-shot", hosts=("darwin",), logs=True),
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


def launchd_log_path(svc: Service) -> Path:
    """Where launchd appends a logging service's stdout and stderr (under root's ``/var/log``), so the pf one-shot's
    "restored pf" / "not retried" line from a boot run can be read afterwards. The program keeps the file's size
    bounded (:func:`levain.slot.services.pf.bound_log`)."""
    return Path("/var/log") / f"{launchd_label(svc)}.log"


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
        # "on-failure" (§1.3 dispatch): the same mapping; a non-zero exit or a signal death is relaunched, an exit 0
        # (the supervisor-lock loser's) is not.
        "KeepAlive": True if svc.restart == "always" else {"SuccessfulExit": False},
    }
    if svc.logs:
        job["StandardOutPath"] = job["StandardErrorPath"] = str(launchd_log_path(svc))
    return plistlib.dumps(job, sort_keys=True).decode("utf-8")


# --- Linux -------------------------------------------------------------------------------------------


def systemd_name(svc: Service) -> str:
    return f"levain-{svc.name}.service"


def systemd_path(name: str) -> Path:
    return SYSTEMD_DIR / name


#: Each Linux restart policy's ``[Service]`` lines. ``always``: a service that keeps failing backs off step by step
#: instead of restarting at a fixed short interval. ``on-failure`` (§1.3 dispatch): a non-zero exit or a signal other
#: than SIGHUP/SIGINT/SIGTERM/SIGPIPE is restarted 5 s later; an exit 0 and a SIGTERM death are not (systemd's own
#: definition of on-failure). Under either, an EX_CONFIG exit is not (``RestartPreventExitStatus``, below). No Linux
#: service is one-shot.
_SYSTEMD_RESTART: dict[str, tuple[str, ...]] = {
    "always": ("Restart=always", "RestartSec=1", "RestartSteps=5", "RestartMaxDelaySec=30"),
    "on-failure": ("Restart=on-failure", "RestartSec=5"),
}


def systemd_unit_text(svc: Service) -> str:
    if "linux" not in svc.hosts:
        raise Refused("unsupported-host", f"{svc.name} is macOS only")
    if svc.restart not in _SYSTEMD_RESTART:
        raise Refused("unsupported-host", f"{svc.name}: no Linux form of restart {svc.restart!r}")
    deps = " ".join(systemd_name(by_name(a)) for a in svc.after)
    unit = [f"Description=Levain {svc.title} (CONTRACT §1.3)"]
    if deps:
        unit += [f"After={deps}", f"Wants={deps}"]
    unit.append("StartLimitIntervalSec=0")   # no start-rate limit gives up on a service that is restarted
    lines = [
        SYSTEMD_HEADER,
        "[Unit]", *unit, "",
        "[Service]", "Type=simple", f"User={svc.user}", f"Group={_group('linux', svc)}",
        f"ExecStart={' '.join(argv('linux', svc))}",
        *_SYSTEMD_RESTART[svc.restart],
        # EX_CONFIG: the install lacks what the program needs (keys with no _levain_gw<n> account). EX_NOPERM 77: the
        # program runs as a user its row does not name. A restart fixes neither, so the unit stays down until setup or
        # repair changes the install.
        f"RestartPreventExitStatus={EX_CONFIG} 77", "",
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


#: launchd finishes unloading a job after `launchctl bootout` returns; a check made right after a bootout waits this long
#: for the job to go before it calls it still loaded (a fresh setup then an immediate undo hit it on a macOS VM, 10-22).
LAUNCHD_UNLOAD_WAIT_S = 10.0
_LAUNCHD_POLL_S = 0.2
#: one `launchctl print` may take this long at least, so a check with no wait is still bounded
_LAUNCHD_PRINT_MIN_S = 5.0
#: `launchctl print` exits with this when launchd has no such service: the ONE answer that means absent
_LAUNCHCTL_NO_SUCH_SERVICE = 113


def _launchd_job_absent(label: str, run: Callable[..., object] | None = None, *, wait_s: float = 0.0,
                        sleep: Callable[[float], None] | None = None,
                        clock: Callable[[], float] | None = None) -> tuple[bool, str]:
    """True only when launchd says it has no such job. Loaded, an unexpected exit or a timed-out query are
    all "not absent" (fail closed); with ``wait_s`` the check re-asks until absent or the deadline."""
    import subprocess
    import time

    sleep, clock = sleep or time.sleep, clock or time.monotonic
    deadline = clock() + wait_s
    while True:
        try:
            r = (run or subprocess.run)(["/bin/launchctl", "print", f"system/{label}"], capture_output=True,
                                        env=child_env(),
                                        timeout=max(deadline - clock(), _LAUNCHD_PRINT_MIN_S))
            rc = getattr(r, "returncode", None)
            why = (f"{label} is still loaded" if rc == 0
                   else "" if rc == _LAUNCHCTL_NO_SUCH_SERVICE
                   else f"{label} could not be checked (launchctl print exited {rc})")
        except subprocess.TimeoutExpired:
            why = f"{label} could not be checked (launchctl print timed out)"
        if not why or clock() >= deadline:
            return (not why, why)
        sleep(min(_LAUNCHD_POLL_S, max(deadline - clock(), 0.0)))


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
    (run or subprocess.run)(["/bin/launchctl", "bootout", f"system/{label}"], capture_output=True, env=child_env())
    return _launchd_job_absent(label, run, wait_s=LAUNCHD_UNLOAD_WAIT_S)


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
    run([systemctl, "stop", name], capture_output=True, env=child_env())   # a unit systemd never loaded: non-zero, harmless
    r = run([systemctl, "show", "-p", "ActiveState", "--value", name], capture_output=True, text=True, env=child_env())
    state = (getattr(r, "stdout", "") or "").strip()
    if state not in ("inactive", "failed"):
        return False, f"{name} is {state or 'in an unknown state'} after systemctl stop"
    return True, ""


def _enable_now_step(systemctl: str, name: str) -> Step:
    """The ONE home of the Linux start step's wording and argv (setup, repair, replenish)."""
    return Step(f"enable and start {name}", (systemctl, "enable", "--now", name))


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
        steps.append(Step(f"stop {name} unless {path} already matches",
                          call=lambda n=name, p=path, t=text: _stop_unit_if_changed(systemctl, n, p, t, run, read)))
        steps.append(Step(f"write the systemd unit {name} ({s.title}, as {s.user})", write=(path, text, 0o644)))
    steps.append(Step("tell systemd about the units", (systemctl, "daemon-reload")))
    for name in start_order(host, chosen):
        steps.append(_enable_now_step(systemctl, name))
    return steps


def keys_start_steps(host: HostOS, *, restart: bool = False) -> list[Step]:
    """Linux: the steps that start the keys service, built from the same step :func:`install_steps` ends with. ``restart``: a stop first, the order repair uses, for a keys that already runs and read its gateway
    accounts once at startup. macOS: none, its keys job re-checks its accounts itself and never exits for a missing one."""
    if host != "linux":
        return []
    keys = by_name("keys")
    name = systemd_name(keys)
    start = _enable_now_step(_abs("systemctl"), name)
    if not restart:
        return [start]

    def stop() -> tuple[bool, str]:
        from levain.slot.removal import Live, ReadFailed
        try:
            return stop_unit_act(host, keys.name, Live(), disable=False)
        except ReadFailed as e:
            return False, str(e)
    return [Step(f"stop {name}", call=stop), start]


__all__ = ["SERVICES", "Service", "argv", "built_services", "by_name", "install_steps", "keys_start_steps", "launchd_label",
           "launchd_log_path", "launchd_plist_text", "program_relpath", "program_source", "services_for", "start_order",
           "systemd_name", "systemd_unit_text"]


# --- row 33 R1b: a unit removed by its RECORDED name (§1.1 table row 1) -------------------------------------------


def _launchd_state(label: str, live: "Live") -> str:
    """``loaded`` / ``absent`` from ``launchctl print``: 113 (no such service) is the ONE absent answer; any other
    non-zero exit is a failed read (DESIGN X4-M1), never absent."""
    from levain.slot.removal import ReadFailed

    rc, out, err = live.out(("/bin/launchctl", "print", f"system/{label}"))
    if rc == 0:
        return "loaded"
    if rc == _LAUNCHCTL_NO_SUCH_SERVICE:
        return "absent"
    raise ReadFailed(f"launchctl print system/{label} exited {rc}: {(err or out).strip()[:200]}")


def _systemd_state(prop: str, name: str, live: "Live") -> str:
    """``systemctl show -p <prop> --value``; a failing ``systemctl show`` raises (never read as stopped or absent)."""
    return live.read((_abs("systemctl"), "show", "-p", prop, "--value", name)).strip()


def _plist_is(label: str) -> Callable[[bytes], bool]:
    def check(data: bytes) -> bool:
        try:
            return plistlib.loads(data).get("Label") == label
        except Exception:   # noqa: BLE001 — a file that is not our plist is not ours
            return False
    return check


def _unit_is_ours(data: bytes) -> bool:
    return data.split(b"\n", 1)[0] == SYSTEMD_HEADER.encode()


def unit_state(host: HostOS, n: str, live: "Live") -> str:
    """OURS: the unit file is a regular root file Levain wrote (macOS: its ``Label`` is ``com.levain.<n>``; Linux: its
    first line is :data:`SYSTEMD_HEADER`), OR the file is gone and
    the job is still loaded (the bound reserves the name). ABSENT: no file and not loaded. NOT_OURS: a file at the
    name that is not ours."""
    from levain.slot.removal import ABSENT, OURS, root_file

    if host == "darwin":
        label = f"com.levain.{n}"
        state = root_file(live, LAUNCHD_DIR / f"{label}.plist", check=_plist_is(label))
        if state != ABSENT:
            return state
        return OURS if _launchd_state(label, live) == "loaded" else ABSENT
    unit = f"levain-{n}.service"
    state = root_file(live, SYSTEMD_DIR / unit, check=_unit_is_ours)
    if state != ABSENT:
        return state
    if _systemd_state("LoadState", unit, live) != "not-found":
        return OURS
    return ABSENT if _systemd_state("ActiveState", unit, live) in ("inactive", "failed") else OURS


def stop_unit_act(host: HostOS, n: str, live: "Live", *, disable: bool) -> tuple[bool, str]:
    """The ONE stop body (repair's R5 stops and a removed unit's revoke): the verdict is the END STATE, never the
    stop's exit status. macOS boots out a loaded job and waits for launchd to unload it; Linux stops it (``disable
    --now`` for a unit leaving the install) and reads ``ActiveState`` again. A leaving unit whose FILE is gone while
    systemd still runs it gets ``systemctl stop``, then a best-effort ``disable``: ``disable`` needs the file and may
    fail before its ``--now`` stops anything (unmeasured: the owed Linux round, U12), and that state would read OURS on
    every re-run."""
    if host == "darwin":
        label = f"com.levain.{n}"
        if _launchd_state(label, live) == "absent":
            return True, ""
        live.out(("/bin/launchctl", "bootout", f"system/{label}"))      # its status is not the verdict: checked next
        return _launchd_job_absent(label, live.run, wait_s=LAUNCHD_UNLOAD_WAIT_S)
    unit, systemctl = f"levain-{n}.service", _abs("systemctl")
    if not disable and _systemd_state("ActiveState", unit, live) in ("inactive", "failed"):
        return True, ""
    if disable and os.path.lexists(live.at(SYSTEMD_DIR / unit)):
        live.out((systemctl, "disable", "--now", unit))                 # its status is not the verdict: checked next
        how = "disable --now"
    else:
        live.out((systemctl, "stop", unit))
        if disable:
            live.out((systemctl, "disable", unit))                      # best effort: with no file it may fail
        how = "stop"
    state = _systemd_state("ActiveState", unit, live)
    if state not in ("inactive", "failed"):
        return False, f"{unit} is {state or 'in an unknown state'} after systemctl {how}"
    return True, ""


def stop_unit_by_name(host: HostOS, n: str, *, live: "Live") -> list[Step]:
    """``unit/1`` revoke (R5): the recorded unit stopped by NAME, never through :data:`SERVICES`."""
    from levain.slot.removal import chain, point, remove_step

    why = f"stop com.levain.{n}" if host == "darwin" else f"stop and disable levain-{n}.service"
    tools = ("/bin/launchctl",) if host == "darwin" else (_abs("systemctl"),)
    return [remove_step(why, ("unit/1", n), live, lambda: unit_state(host, n, live),
                        chain(lambda: stop_unit_act(host, n, live, disable=True), point(live, "unit/1:after-stop")),
                        tools=tools)]


def remove_unit_file_by_name(host: HostOS, n: str, *, live: "Live") -> list[Step]:
    """``unit/1`` destroy (R12): ``rm -f`` the recorded unit file; Linux then ``daemon-reload`` and ``reset-failed``
    (a file gone while systemd still holds the unit loaded reads OURS, so a re-run reloads)."""
    from levain.slot.removal import at, chain, point, remove_step

    if host == "darwin":
        path = LAUNCHD_DIR / f"com.levain.{n}.plist"
        return [remove_step(f"remove {path}", ("unit/1", n), live, lambda: unit_state(host, n, live),
                            chain(lambda: live.act(("/bin/rm", "-f", at(live, path))), point(live, "unit/1:after-rm")),
                            tools=("/bin/launchctl", "/bin/rm"))]
    unit, systemctl, rm = f"levain-{n}.service", _abs("systemctl"), _abs("rm")
    path = SYSTEMD_DIR / unit

    def reset() -> tuple[bool, str]:
        live.out((systemctl, "reset-failed", unit))     # best effort: a unit systemd holds no record of answers non-zero
        return True, ""

    return [remove_step(f"remove {path} (then daemon-reload, reset-failed)", ("unit/1", n), live,
                        lambda: unit_state(host, n, live),
                        chain(lambda: live.act((rm, "-f", at(live, path))), point(live, "unit/1:after-rm"),
                              lambda: live.act((systemctl, "daemon-reload")), reset),
                        tools=(systemctl, rm))]

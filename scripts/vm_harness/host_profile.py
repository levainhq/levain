"""The guest host's profile: every value the VM harness used to hard-code for its macOS clone, as data.

Harness code, stdlib only. The two guest-side scripts (``guest_ops.py``, ``guest_dispatch.py``) are copied into the clone
next to this file and import it from their own directory, so it imports nothing from the repo.

``DARWIN`` is the value set the harness ran with before the profile existed (byte for byte; the parity test in
``tests/test_vm_harness_profile.py`` pins it). ``LINUX`` takes the store and run paths from ``levain.slot.layout``
(``STORE`` / ``RUN``), the interpreter and tools from the Ubuntu 24.04 aarch64 image measured in
``project_memory/1107-26-linuxvm/L0_RUN.md``. No macOS-bound literal belongs in ``driver.py``, ``vm_run.py``,
``guest_ops.py`` or ``guest_dispatch.py``: a test over their string constants fails if one comes back.
"""
from __future__ import annotations

from dataclasses import dataclass


class ProfileMismatch(RuntimeError):
    """The guest is not the host OS the chosen profile describes."""


@dataclass(frozen=True)
class HostProfile:
    name: str                      # "darwin" / "linux": also levain's own host key (layout.STORE[host])
    host_os: str                   # the value the product's runtime takes as ``host=``
    uname: str                     # what ``uname -s`` prints in such a guest
    store: str                     # levain.slot.layout.STORE[host_os]
    run_dir: str                   # levain.slot.layout.RUN[host_os]
    ops_control_sock: str          # the control socket path guest_ops dials (the path the harness has always used)
    exchange_dir: str              # the host/guest rendezvous dir, owned by the dispatch user
    tools_dir: str                 # where the two guest scripts and this module are copied
    python: str                    # the guest interpreter that runs them
    git: str                       # the git the dispatch workspace seam bundles with
    ps_bin: str                    # ps(1)
    ps_columns: str                # the ``-o`` list the uid scan reads (positional: pid, etime, pcpu, command, uid)
    dispatch_user: str             # the unprivileged dispatch account
    ssh_user: str                  # the clone image's admin login
    transport: str                 # "ssh" (askpass password) or "tart_exec" (guest agent)
    ssh_fallback: bool             # a tart_exec profile falls back to ssh when the guest agent does not answer
    service_manager: str           # "launchd" / "systemd"
    account_tool: str              # how the product creates slot accounts there
    ssh_password: str = "admin"    # the Tart image's public default password (askpass hands it over; NOT the login name)
    levain_roots: tuple = ("/tmp/lvclean", "/tmp/lv")   # where the rig stages the levain source; the first with ``levain/`` wins

    def ops_argv(self) -> tuple[str, ...]:
        """The root-side guest_ops invocation prefix (``sudo -n`` is passwordless for the image's admin)."""
        return ("sudo", "-n", self.python, "-I", f"{self.tools_dir}/guest_ops.py")

    def dispatch_argv(self) -> tuple[str, ...]:
        """The dispatch-user guest_dispatch invocation prefix."""
        return ("sudo", "-n", "-u", self.dispatch_user, "/usr/bin/env", "PATH=/usr/bin:/bin",
                self.python, "-I", f"{self.tools_dir}/guest_dispatch.py")

    def ps_argv(self) -> list[str]:
        return [self.ps_bin, "-Ao", self.ps_columns]


DARWIN = HostProfile(
    name="darwin", host_os="darwin", uname="Darwin",
    store="/private/var/levain", run_dir="/private/var/run/levain",
    ops_control_sock="/var/run/levain/control/control.sock",
    exchange_dir="/private/tmp/fh", tools_dir="/tmp/fire",
    python="/usr/local/bin/python3.13",
    git="/Library/Developer/CommandLineTools/usr/bin/git",
    ps_bin="/bin/ps", ps_columns="pid,etime,pcpu,ucomm,uid",
    dispatch_user="_levain_dispatch", ssh_user="admin",
    transport="ssh", ssh_fallback=False,
    service_manager="launchd", account_tool="dscl",
)

LINUX = HostProfile(
    name="linux", host_os="linux", uname="Linux",
    store="/var/levain", run_dir="/run/levain",
    ops_control_sock="/run/levain/control/control.sock",
    exchange_dir="/tmp/fh", tools_dir="/tmp/fire",
    python="/usr/bin/python3.12",
    git="/usr/bin/git",
    ps_bin="/bin/ps", ps_columns="pid,etime,pcpu,comm,uid",
    dispatch_user="_levain_dispatch", ssh_user="admin",
    transport="tart_exec", ssh_fallback=True,
    service_manager="systemd", account_tool="useradd --system",
)

PROFILES = {p.name: p for p in (DARWIN, LINUX)}


def profile_named(name: str) -> HostProfile:
    try:
        return PROFILES[name]
    except KeyError:
        raise ValueError(f"unknown host profile {name!r}: one of {sorted(PROFILES)}") from None


def profile_for_platform(platform: str) -> HostProfile:
    """The profile of the machine a guest script runs on (``sys.platform``), so a guest script cannot disagree with its host."""
    if platform == "darwin":
        return DARWIN
    if platform.startswith("linux"):
        return LINUX
    raise ProfileMismatch(f"no host profile for platform {platform!r}")


def check_guest_os(profile: HostProfile, uname_output: str) -> None:
    """Refuse when the guest's ``uname -s`` is not the profile's OS (a Linux profile pointed at a macOS clone, or the reverse)."""
    got = uname_output.strip()
    if got != profile.uname:
        raise ProfileMismatch(f"profile {profile.name!r} expects a {profile.uname} guest, the guest says {got!r}")

"""Linux: the slots' own copy of bubblewrap (CONTRACT §1.2 ``levain-slots`` row; §9.2 lists it in the
root view), ``/usr/libexec/levain/bwrap``, root : ``levain-slots`` 0750, copied from the system's.

Where the kernel restricts unprivileged user namespaces through AppArmor
(``kernel.apparmor_restrict_unprivileged_userns`` = 1), the copy cannot map its uid without a profile
granting it ``userns`` (R-L2). Setup DETECTS that by reading the sysctl (:func:`userns_restricted`,
the read injected for tests) and renders and loads the profile only then.

The profile is ``levain-slot-bwrap``: hands' floor already installs a profile named ``levain-bwrap``
(for the system bwrap), and ours must never replace it. Undo unloads and removes only ours.
"""
from __future__ import annotations

from pathlib import Path
from typing import Callable

from levain.firing.hands import Step, _abs

LEVAIN_BWRAP = Path("/usr/libexec/levain/bwrap")
BWRAP_GROUP = "levain-slots"
BWRAP_MODE = 0o750
APPARMOR_PROFILE_NAME = "levain-slot-bwrap"
APPARMOR_PROFILE = Path("/etc/apparmor.d") / APPARMOR_PROFILE_NAME
USERNS_SYSCTL = "/proc/sys/kernel/apparmor_restrict_unprivileged_userns"


def _read(path: str) -> str:
    return Path(path).read_text(encoding="ascii")


def userns_restricted(read: Callable[[str], str] = _read) -> bool:
    """True when the sysctl reads 1. Absent (no AppArmor userns restriction on this kernel) = False."""
    try:
        return read(USERNS_SYSCTL).strip() == "1"
    except FileNotFoundError:
        return False


def system_bwrap() -> str:
    return _abs("bwrap")


def apparmor_profile_text() -> str:
    """Grants the Levain copy of bwrap user namespaces and nothing else (the shape R-L2 ran)."""
    return (
        "# Levain session slots' bubblewrap (CONTRACT §1.2). Written by the install helper; removed by its undo verb.\n"
        "abi <abi/4.0>,\n"
        "include <tunables/global>\n"
        "\n"
        f"profile {APPARMOR_PROFILE_NAME} {LEVAIN_BWRAP} flags=(unconfined) {{\n"
        "  userns,\n"
        "\n"
        f"  include if exists <local/{APPARMOR_PROFILE_NAME}>\n"
        "}\n"
    )


def bwrap_steps(*, apparmor_userns_restricted: bool, source: str | None = None) -> list[Step]:
    """Copy the system bwrap to the slots' path with its owner, group and mode in one ``install``
    (no window where the copy is another group's or wider), then, when the host restricts user
    namespaces (the caller passes what :func:`userns_restricted` read), its profile."""
    src = source or system_bwrap()
    steps = [
        Step(f"create {LEVAIN_BWRAP.parent}", (_abs("mkdir"), "-p", "-m", "755", str(LEVAIN_BWRAP.parent))),
        Step(f"copy {src} to {LEVAIN_BWRAP}, root:{BWRAP_GROUP} {BWRAP_MODE:04o} (the slots' bwrap)",
             (_abs("install"), "-o", "root", "-g", BWRAP_GROUP, "-m", f"{BWRAP_MODE:04o}", src, str(LEVAIN_BWRAP))),
    ]
    if apparmor_userns_restricted:
        steps += [
            Step(f"write the AppArmor profile {APPARMOR_PROFILE_NAME} (user namespaces for the slots' bwrap)",
                 write=(APPARMOR_PROFILE, apparmor_profile_text(), 0o644)),
            Step(f"load {APPARMOR_PROFILE_NAME}", (_abs("apparmor_parser"), "-r", str(APPARMOR_PROFILE))),
        ]
    return steps


def bwrap_undo_steps() -> list[Step]:
    """Unload and remove OUR profile only (never hands' ``levain-bwrap``), then the copy."""
    test = _abs("test")
    return [
        Step(f"unload the AppArmor profile {APPARMOR_PROFILE_NAME}",
             (_abs("apparmor_parser"), "-R", str(APPARMOR_PROFILE)), skip_if=(test, "!", "-e", str(APPARMOR_PROFILE))),
        Step(f"remove {APPARMOR_PROFILE}", (_abs("rm"), "-f", str(APPARMOR_PROFILE))),
        Step(f"remove {LEVAIN_BWRAP}", (_abs("rm"), "-f", str(LEVAIN_BWRAP))),
    ]


__all__ = ["APPARMOR_PROFILE", "APPARMOR_PROFILE_NAME", "BWRAP_GROUP", "BWRAP_MODE", "LEVAIN_BWRAP", "USERNS_SYSCTL",
           "apparmor_profile_text", "bwrap_steps", "bwrap_undo_steps", "system_bwrap", "userns_restricted"]

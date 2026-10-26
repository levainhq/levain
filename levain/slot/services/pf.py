"""The ``com.levain.pf`` boot one-shot (CONTRACT §1.3, §23, §24): what a reboot undoes, redone once per boot.

A reboot leaves pf disabled, the ``com.apple/levain`` anchor empty and lo0 without the slots' aliases, and every session
start then refuses ``firewall-open`` or ``loopback-alias-down`` [measured in r3c10-vm 10-26, adoptvm RETURN]. This
program runs :func:`levain.slot.pf.boot_steps` for the egress slots in service: each alias, the rules file setup,
replenish or repair last wrote, loaded as it is, and pf's enable reference taken again.

It runs as root under the install lock (:func:`levain.slot.intent.install_lock`), so it never interleaves with a
setup, replenish, repair or undo, which write the same rules file and token. The unit's ``KeepAlive`` is
``SuccessfulExit: false`` (:func:`levain.slot.units.launchd_plist_text`): launchd starts it again after a non-zero
exit, and leaves it after 0. So an install verb holding the lock (:data:`EX_BUSY`) and a step that fails (1) are tried
again; a machine with nothing installed exits 0. The install verbs run the same alias and load steps themselves, so a
run that finds the lock held loses nothing by waiting for the next start.
"""
from __future__ import annotations

import os
import sys
from types import SimpleNamespace
from typing import Callable, ContextManager, Optional, Sequence, Tuple

from levain.firing.hands import run_plan
from levain.slot import Refused, pf
from levain.slot import install_helper as IH
from levain.slot import runtime_helper as RH
from levain.slot.ids import SlotId
from levain.slot.intent import install_lock

#: sysexits.h EX_TEMPFAIL: another install-helper run holds the install lock; launchd starts this again.
EX_BUSY = 75
#: sysexits.h EX_NOPERM: not root.
EX_NOPERM = 77
#: sysexits.h EX_CONFIG: not macOS (the unit exists on macOS alone: ``units.SERVICES``).
EX_CONFIG = 78


def _log(msg: str) -> None:
    try:
        sys.stderr.write(f"levain pf[{os.getpid()}]: {msg}\n")
        sys.stderr.flush()
    except Exception:   # noqa: BLE001
        pass


def installed_egress_slots() -> Optional[Tuple[SlotId, ...]]:
    """The egress slots in service, read as the install helper reads them (one dirfd, every component O_NOFOLLOW,
    root's dirs only), or None when this machine has no finished install (no ``L/policy/operator``)."""
    seams = RH.Seams(host="darwin")
    if not os.path.lexists(seams.L):
        return None
    store = RH.Store(seams)
    if IH._operator_of(store) is None:
        return None
    return tuple(SlotId("egress", i) for i in IH.slots_in_service(store).get("egress", ()))


def _lock() -> ContextManager[int]:
    return install_lock("darwin")


def _run(steps) -> int:
    return run_plan(SimpleNamespace(steps=tuple(steps)), dry_run=False, emit=_log)   # type: ignore[arg-type]


def main(argv: Optional[Sequence[str]] = None, *, platform: str = sys.platform,
         euid: Callable[[], int] = os.geteuid,
         lock: Callable[[], ContextManager[int]] = _lock,
         slots: Callable[[], Optional[Tuple[SlotId, ...]]] = installed_egress_slots,
         run: Callable[[list], int] = _run) -> int:
    """0: every step ran or was already done, or nothing is installed. :data:`EX_BUSY`: an install verb holds the lock.
    1: a step failed (its line is on stderr). :data:`EX_NOPERM` / :data:`EX_CONFIG`: not root / not macOS."""
    if platform != "darwin":
        _log("the pf one-shot is macOS only")
        return EX_CONFIG
    if euid() != 0:
        _log("must run as root")
        return EX_NOPERM
    try:
        with lock():
            found = slots()
            if found is None:
                _log("no finished install here: nothing to restore")
                return 0
            rc = run(pf.boot_steps(found))
    except Refused as e:
        if e.name == "install-busy":
            _log("an install-helper run holds the install lock; launchd starts this again")
            return EX_BUSY
        _log(f"refused {e.name}: {e.detail}")
        return 1
    if rc != 0:
        return 1
    _log(f"restored pf for {len(found)} egress slot(s): aliases, the {pf.ANCHOR} rules, pf's enable reference")
    return 0


__all__ = ["EX_BUSY", "EX_CONFIG", "EX_NOPERM", "installed_egress_slots", "main"]


if __name__ == "__main__":
    sys.exit(main())

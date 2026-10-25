"""The gw-keys service program (CONTRACT §1.3, §23; 3c design §2.4): its argv completed from the install, the
accounts keys' start needs looked up (EX_CONFIG, 78, when one is missing: :func:`missing_account`; on macOS it idles
instead, see :func:`main`), then :func:`levain.broker.keys.main`, and nothing else.

The unit runs it through ``service-entry keys`` with no other argument, so keys' three required paths come from
:mod:`levain.slot.layout`: ``--runtime-root`` R, ``--lib-root`` L, ``--control`` ``R/control/control.sock``. Any flag
given here follows them, so it replaces its default; every other flag is passed to keys as written. keys is metered whatever the argv: its
``main`` opens the spend ledger ``L/keys/ledger.jsonl`` before it binds anything, and has no flag that skips it.
"""
from __future__ import annotations

import sys
import time
from typing import Callable, List, Optional, Sequence

from levain.broker import keys as K
from levain.slot.layout import RUN, STORE

NAME = "keys"


def _host() -> str:
    return "darwin" if sys.platform == "darwin" else "linux"


def keys_argv(argv: Sequence[str], *, host: Optional[str] = None) -> List[str]:
    """``argv`` (the program's own name first is allowed: ``service-entry keys`` leaves it) after keys' three required
    paths, the install's. keys parses with argparse, where the last of a repeated flag wins, so a path ``argv`` names
    replaces the install's."""
    host = host or _host()
    args: List[str] = list(argv)
    if args[:1] == [NAME]:
        args = args[1:]
    return ["--runtime-root", str(RUN[host]), "--lib-root", str(STORE[host]),
            "--control", str(RUN[host] / "control" / "control.sock"), *args]


#: sysexits.h EX_CONFIG (``units.EX_CONFIG``): the install has no account keys serves or answers to
EX_CONFIG = 78


def missing_account() -> Optional[str]:
    """What keys' own start refuses on (``keys.main``: no ``_levain_gw<n>``, no CONTROL account), named before it runs,
    so the unit sees EX_CONFIG instead of keys' generic exit 2 and does not restart it (ruling 2 of 1025-23/3c9a)."""
    if not K._gw_uids():
        return "no _levain_gw<n> account exists; nothing may connect (an install with no egress slot)"
    if K._account_uid(K.CONTROL_ACCOUNT) is None:
        return f"no {K.CONTROL_ACCOUNT} account exists; nothing may administer keys"
    return None


#: macOS: how often an idling keys looks for its accounts again [judged by lane-3c9-fix, 10-25, against a replenish
#: or repair the operator waits on: a minute is soon enough, and one lookup a minute costs nothing]
IDLE_RECHECK_S = 60.0


def main(argv: Optional[Sequence[str]] = None, *, host: Optional[str] = None,
         sleep: Callable[[float], None] = time.sleep) -> int:
    """Linux: a missing account exits EX_CONFIG, which the unit's ``RestartPreventExitStatus`` leaves stopped until
    setup or repair starts it. macOS: launchd's ``KeepAlive`` restarts every exit, whatever its status (ruling 1 of
    1025-23/3c9b RULINGS_OBS), so keys idles instead, looking again every :data:`IDLE_RECHECK_S`, and runs once the
    accounts exist (a later replenish or repair made them)."""
    host = host or _host()
    args = keys_argv(sys.argv[1:] if argv is None else argv, host=host)
    why = None if "--test-single-uid" in args else missing_account()
    if why is not None:
        print(f"levain gw-keys: {why}", file=sys.stderr)
        if host != "darwin":
            return EX_CONFIG
        print("levain gw-keys: idle until the accounts exist", file=sys.stderr)
        while missing_account() is not None:
            sleep(IDLE_RECHECK_S)
    return K.main(args)


__all__ = ["IDLE_RECHECK_S", "keys_argv", "main"]


if __name__ == "__main__":
    sys.exit(main())

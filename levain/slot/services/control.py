"""The CONTROL service program (CONTRACT §1.3, §23; 3c design §2.4): its argv parsed into a
:class:`levain.slot.control.ServeConfig`, then :func:`levain.slot.control.serve`, and nothing else.

The unit runs it through ``service-entry control`` with no other argument, so every path and uid comes from the install:
the paths from :mod:`levain.slot.layout`, the operator's uid from ``L/policy/operator``, each peer's uid from its account.
A flag replaces one of them (a test store, run by an unprivileged uid). It never runs as root: the unit runs it as
``_levain_control``.
"""
from __future__ import annotations

import argparse
import os
import pwd
import sys
from pathlib import Path
from typing import List, Optional, Sequence

from levain.slot import Refused
from levain.slot import control as C
from levain.slot.install_helper import parse_operator_record
from levain.slot.layout import RUN, STORE, operator_record_path

NAME = "control"
#: the peer accounts CONTROL serves by uid (CONTRACT §1.1, §3.3)
KEYS_ACCOUNT = "_levain_keys"
QBROKER_ACCOUNT = "_levain_qbroker"
DISPATCH_ACCOUNT = "_levain_dispatch"
EX_USAGE = 64
EX_CONFIG = 78       # EX_CONFIG: the install this program reads is missing a piece it needs
EX_REFUSED = 75      # EX_TEMPFAIL: a refused start; the unit manager starts it again


def _host() -> str:
    return "darwin" if sys.platform == "darwin" else "linux"


def _parser(host: str) -> argparse.ArgumentParser:
    L, R = STORE[host], RUN[host]
    ap = argparse.ArgumentParser(prog="levain-control", description="CONTROL (CONTRACT §1.3); the unit passes no flag.")
    ap.add_argument("--store-dir", default=str(L / "sessions"), help="the session state store (L/sessions)")
    ap.add_argument("--socket", default=str(R / "control" / "control.sock"), help="the socket CONTROL serves")
    ap.add_argument("--keys-admin", default=str(R / "keys" / "admin.sock"), help="keys' admin socket")
    ap.add_argument("--routes-dir", default=str(L / "routes"), help="L/routes (trust, base map, dispatch maps)")
    ap.add_argument("--lineage-dir", default=str(L / "lineage"), help="L/lineage (the lineage sink's dir)")
    ap.add_argument("--ceiling", default=None, help="the dispatch ceiling file (default: L/policy/dispatch_ceiling)")
    ap.add_argument("--ceiling-uid", type=int, default=0, help="the ceiling file's required owner (root)")
    ap.add_argument("--operator-record", default=str(operator_record_path(host)),
                    help="the operator record the operator's uid is read from")
    ap.add_argument("--operator-uid", type=int, default=None, help="the operator's uid (default: the record's)")
    ap.add_argument("--keys-uid", type=int, default=None, help=f"keys' uid (default: {KEYS_ACCOUNT}'s)")
    ap.add_argument("--qbroker-uid", type=int, default=None, help=f"the queue broker's uid (default: {QBROKER_ACCOUNT}'s)")
    ap.add_argument("--dispatch-uid", type=int, default=None, help=f"dispatch's uid (default: {DISPATCH_ACCOUNT}'s)")
    return ap


def _account_uid(name: str) -> int:
    try:
        return pwd.getpwnam(name).pw_uid
    except KeyError:
        raise Refused("no-account", name) from None


def _operator_uid(record: Path) -> int:
    try:
        data = record.read_bytes()
    except FileNotFoundError:
        raise Refused("no-operator", str(record)) from None
    except OSError as e:
        raise Refused("operator-record-unreadable", f"{record}: {e.strerror or type(e).__name__}") from None
    try:
        return int(parse_operator_record(data)["uid"])   # type: ignore[call-overload]
    except (ValueError, KeyError, TypeError) as e:
        raise Refused("operator-record-malformed", f"{type(e).__name__}: {e}") from None


def config_from_argv(argv: Sequence[str], *, host: Optional[str] = None) -> C.ServeConfig:
    """``argv`` (the program's own name first is allowed: ``service-entry control`` leaves it) -> the config.
    Raises :class:`SystemExit` on a usage error and :class:`Refused` on a missing account or operator record."""
    host = host or _host()
    args: List[str] = list(argv)
    if args[:1] == [NAME]:
        args = args[1:]
    ap = _parser(host)
    a = ap.parse_args(args)
    for flag, v in (("--store-dir", a.store_dir), ("--socket", a.socket), ("--keys-admin", a.keys_admin),
                    ("--routes-dir", a.routes_dir), ("--lineage-dir", a.lineage_dir), ("--ceiling", a.ceiling),
                    ("--operator-record", a.operator_record)):
        if v is not None and not os.path.isabs(v):
            ap.error(f"{flag} must be an absolute path")
    return C.ServeConfig(
        store_dir=Path(a.store_dir), socket_path=Path(a.socket),
        operator_uid=a.operator_uid if a.operator_uid is not None else _operator_uid(Path(a.operator_record)),
        keys_admin_path=Path(a.keys_admin),
        keys_uid=a.keys_uid if a.keys_uid is not None else _account_uid(KEYS_ACCOUNT),
        qbroker_uid=a.qbroker_uid if a.qbroker_uid is not None else _account_uid(QBROKER_ACCOUNT),
        routes_dir=Path(a.routes_dir),
        dispatch_uid=a.dispatch_uid if a.dispatch_uid is not None else _account_uid(DISPATCH_ACCOUNT),
        ceiling_path=Path(a.ceiling) if a.ceiling is not None else None, ceiling_uid=a.ceiling_uid,
        lineage_dir=Path(a.lineage_dir), host=host)


def main(argv: Optional[Sequence[str]] = None) -> int:
    if os.geteuid() == 0:
        print("levain CONTROL: refusing to run as root (it runs as _levain_control)", file=sys.stderr)
        return EX_USAGE
    try:
        config = config_from_argv(sys.argv[1:] if argv is None else argv)
    except Refused as e:
        print(f"levain CONTROL: refused: {e}", file=sys.stderr, flush=True)
        return EX_CONFIG
    try:
        return C.serve(config)
    except Refused:
        return EX_REFUSED     # serve printed the refusal's name


__all__ = ["EX_CONFIG", "EX_REFUSED", "EX_USAGE", "config_from_argv", "main"]


if __name__ == "__main__":
    sys.exit(main())

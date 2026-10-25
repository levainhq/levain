"""``python -I -m levain.qbroker``: run the queue broker as ``_levain_qbroker`` (CONTRACT c3 §1.3).

Serves ``R/qb/slot<n>/q.sock`` for every ``--slot``, each accepting only the uid of ``_levain_s<n>`` and served only
when the socket is in group ``_levain_s<n>``; resolves caps through ``R/control/control.sock``; reads rows under
``L/queue/`` and the answer-signer snapshot under ``L/policy/`` (verified by ``L/routes/trust.pub``), and the signed
contracts dispatch publishes under ``L/contracts/`` (CONTRACT §26 S1-b; files owned by ``_levain_dispatch``: with no
such user, no contract grant applies and every call goes to the queue), and the slot limits under ``L/policy/limits``
(:class:`levain.qbroker.limits.PolicyLimits`, owned by root). Runs until SIGTERM or SIGINT.
"""
from __future__ import annotations

import argparse
import grp
import logging
import pwd
import signal
import sys
import threading
from pathlib import Path

from levain.qbroker.broker import Broker
from levain.qbroker.control import ControlResolver
from levain.qbroker.limits import PolicyLimits
from levain.qbroker.server import SlotListener, socket_path
from levain.qbroker.signers import SignersSource
from levain.qbroker.store import QueueStore

_DEFAULT_STORE = "/var/levain"
# CONTRACT §26 S1-b: the directory's row in §2.1 is S1's (levain/slot/layout.py); the name is this one constant
CONTRACTS_DIR = "contracts"
DISPATCH_USER = "_levain_dispatch"
_DEFAULT_RUN = "/private/var/run/levain" if sys.platform == "darwin" else "/run/levain"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(prog="python -I -m levain.qbroker")
    ap.add_argument("--store", default=_DEFAULT_STORE, help="L, the store root")
    ap.add_argument("--run", default=_DEFAULT_RUN, help="R, the run directory")
    ap.add_argument("--slot", type=int, action="append", required=True, help="a configured slot (repeatable)")
    return ap.parse_args(argv)


def build_broker(args: argparse.Namespace, *, limits_owner: int = 0) -> Broker:
    """The broker :func:`main` serves (``limits_owner``: the uid ``L/policy`` and its limits file must belong to)."""
    store_root, run_dir = Path(args.store), Path(args.run)
    try:
        dispatch_uid: int | None = pwd.getpwnam(DISPATCH_USER).pw_uid
    except KeyError:
        logging.getLogger("levain.qbroker").warning("no %s user: no contract grant applies", DISPATCH_USER)
        dispatch_uid = None
    return Broker(QueueStore(store_root / "queue"), ControlResolver(run_dir / "control" / "control.sock"),
                  SignersSource(store_root / "policy", store_root / "routes" / "trust.pub"),
                  contracts=store_root / CONTRACTS_DIR, contracts_owner=dispatch_uid,
                  limits=PolicyLimits(store_root / "policy", owner=limits_owner))


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    run_dir = Path(args.run)
    broker = build_broker(args)
    listeners = []
    for slot in sorted(set(args.slot)):
        listener = SlotListener(broker, slot, socket_path(run_dir, slot), pwd.getpwnam(f"_levain_s{slot}").pw_uid,
                                expected_gid=grp.getgrnam(f"_levain_s{slot}").gr_gid)
        listener.start()
        listeners.append(listener)
    done = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: done.set())
    done.wait()
    for listener in listeners:
        listener.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())

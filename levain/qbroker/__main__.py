"""``python -I -m levain.qbroker``: run the queue broker as ``_levain_qbroker`` (CONTRACT c3 §1.3).

Serves ``R/qb/slot<n>/q.sock`` for every ``--slot``, each accepting only the uid of ``_levain_s<n>`` and served only
when the socket is in group ``_levain_s<n>``; resolves caps through ``R/control/control.sock``; reads rows under
``L/queue/`` and the answer-signer snapshot under ``L/policy/`` (verified by ``L/routes/trust.pub``). Runs until SIGTERM or SIGINT.
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
from levain.qbroker.server import SlotListener, socket_path
from levain.qbroker.signers import SignersSource
from levain.qbroker.store import QueueStore

_DEFAULT_STORE = "/var/levain"
_DEFAULT_RUN = "/private/var/run/levain" if sys.platform == "darwin" else "/run/levain"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -I -m levain.qbroker")
    ap.add_argument("--store", default=_DEFAULT_STORE, help="L, the store root")
    ap.add_argument("--run", default=_DEFAULT_RUN, help="R, the run directory")
    ap.add_argument("--slot", type=int, action="append", required=True, help="a configured slot (repeatable)")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    store_root, run_dir = Path(args.store), Path(args.run)
    broker = Broker(QueueStore(store_root / "queue"), ControlResolver(run_dir / "control" / "control.sock"),
                    SignersSource(store_root / "policy", store_root / "routes" / "trust.pub"))
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

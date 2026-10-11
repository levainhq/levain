"""Subprocess entry point of the K2b-2 kill-point crash harness (tests/test_cockpit_k2b_crash.py).

Builds a broker over a data root with ``kill_at`` passed as an argument (never an environment variable),
starts it, and drives ONE reported write. A kill point that fires ends this process with ``os._exit(137)``;
a run that reaches the end prints the result as JSON and exits 0.

argv[1]: JSON ``{data_root, spores, kill_at, verb, params, target, idempotency_key}``.
"""
from __future__ import annotations

import json
import sys

from levain.cockpit.broker import Broker
from levain.cockpit.effects import SporeEffectStore


def main() -> int:
    cfg = json.loads(sys.argv[1])
    broker = Broker(cfg["data_root"], stores={"spores": SporeEffectStore(cfg["spores"])},
                    store_paths=[cfg["spores"]], install_class="reported", kill_at=cfg["kill_at"])
    broker.start()
    out = broker.issue(verb=cfg["verb"], params=cfg["params"], target=cfg["target"], tier="T2",
                       credential={"class": "token", "name": "browser"}, intent="reported",
                       idempotency_key=cfg["idempotency_key"])
    print(json.dumps(out))
    broker.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())

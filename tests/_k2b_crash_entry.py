"""Subprocess entry point of the K2b-2 kill-point crash harness (tests/test_cockpit_k2b_crash.py).

Builds a broker over a data root with ``kill_at`` passed as an argument (never an environment variable),
starts it, and drives ONE reported write. A kill point that fires ends this process with ``os._exit(137)``;
a run that reaches the end prints the result as JSON and exits 0.

argv[1]: JSON ``{data_root, spores, kill_at, verb, params, target, idempotency_key}``; the anneal kinds
add ``db`` (the anneal store), which registers the episode and section stores beside the spore store.
"""
from __future__ import annotations

import json
import sys

from levain.cockpit.broker import Broker
from levain.cockpit.effects import SporeEffectStore
from levain.cockpit.anneal_effects import EPISODES, SECTIONS, EpisodeEffectStore, SectionEffectStore


def main() -> int:
    cfg = json.loads(sys.argv[1])
    stores: dict = {"spores": SporeEffectStore(cfg["spores"])}
    paths = [cfg["spores"]]
    if cfg.get("db"):
        stores.update({EPISODES: EpisodeEffectStore(cfg["db"]), SECTIONS: SectionEffectStore(cfg["db"])})
        paths.append(cfg["db"])
    broker = Broker(cfg["data_root"], stores=stores, store_paths=paths, install_class="reported",
                    kill_at=cfg["kill_at"])
    broker.start()
    out = broker.issue(verb=cfg["verb"], params=cfg["params"], target=cfg["target"], tier="T2",
                       credential={"class": "token", "name": "browser"}, intent="reported",
                       idempotency_key=cfg["idempotency_key"])
    print(json.dumps(out))
    broker.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())

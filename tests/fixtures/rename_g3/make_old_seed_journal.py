# rename-intention: never-touch
"""Builds the OLD-release journal of two ops signed on the retired disposition ``seed`` (tests/test_rename_g3_seed_replay.py).

Not run by the suite. It drives the release that still said "spore" (a checkout of base a8617e55c) through its own broker,
which still accepted ``seed`` as a write value, and kills it at the ``decided`` point so each op is left DECIDED: a create
(``spore_seed`` with disposition ``seed``) and a re-disposition (``spore_set_disposition`` to ``seed``). The output is what
that release left on disk; no byte comes from the code under test.

    python3 make_old_seed_journal.py <old-tree> <site-packages-with-anneal-memory> <out-dir>
"""
from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path


def _child(cfg: dict) -> int:
    sys.path[:0] = [cfg["old_tree"], cfg["anneal"]]
    from levain.cockpit.broker import Broker
    from levain.cockpit.effects import SporeEffectStore

    class Unsettled(Broker):
        def _recover(self, now, db=None):
            return 0

        def _redrive(self):
            return None

    broker = Unsettled(cfg["data_root"], stores={"spores": SporeEffectStore(cfg["store"])}, store_paths=[cfg["store"]],
                       install_class="reported", kill_at="decided")
    broker.start()
    out = broker.issue(verb=cfg["verb"], params=cfg["params"], target=cfg["target"], tier="T2",
                       credential={"class": "token", "name": "browser"}, intent="reported",
                       idempotency_key=cfg["key"])
    print(json.dumps(out))
    broker.stop()
    return 0


def main(old_tree: str, anneal: str, out: str) -> int:
    sys.path[:0] = [old_tree, anneal]
    from anneal_memory.spores import SporeStore
    from levain.cockpit.effects import SporeEffectStore
    from tests.autonomic_confirm_keys import anchor_scratch

    home = anchor_scratch("g3-old-journal-")
    data, store = home / "data", home / "spores.json"
    SporeStore(store).add(origin_key="k.i", type="thought", text="iota", disposition="agenda", next="2030-01-01")

    def target(key: str | None) -> dict:
        if key is None:
            return {"kind": "create", "panel_id": "tray"}
        row = SporeStore(store).get_by_origin_key(key)
        version = SporeEffectStore(store).snapshot(f"spore:{key}").version
        return {"kind": "row", "panel_id": "tray", "row_id": f"spore:{row['id']}", "row_version": version}

    def run(name: str, verb: str, params: dict, key: str | None) -> None:
        cfg = {"old_tree": old_tree, "anneal": anneal, "data_root": str(data), "store": str(store), "verb": verb,
               "params": params, "target": target(key), "key": f"idem-{name}"}
        proc = subprocess.run([sys.executable, __file__, "--child", json.dumps(cfg)], capture_output=True, text=True,
                              timeout=120, cwd=old_tree)
        assert proc.returncode == 137, f"{name}: rc={proc.returncode}\n{proc.stdout}\n{proc.stderr}"

    run("h", "spore_seed", {"text": "captured as a seed", "type": "thought", "disposition": "seed"}, None)
    run("i", "spore_set_disposition", {"disposition": "seed"}, "k.i")

    conn = sqlite3.connect(data / "cockpit.db")
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    ops = {r[0]: r[1:] for r in conn.execute("SELECT idempotency_key, pending_id, verb, state FROM pendings")}
    dump = "\n".join(conn.iterdump()) + "\n"
    conn.close()
    outp = Path(out)
    outp.mkdir(parents=True, exist_ok=True)
    (outp / "old_journal.sql").write_text(dump, encoding="utf-8")
    shutil.copyfile(store, outp / "old_store.json")
    (outp / "old_journal.json").write_text(json.dumps(
        {k.removeprefix("idem-"): {"pending_id": v[0], "verb": v[1], "state": v[2]} for k, v in sorted(ops.items())},
        indent=2) + "\n", encoding="utf-8")
    shutil.rmtree(home, ignore_errors=True)
    return 0


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--child":
        sys.exit(_child(json.loads(sys.argv[2])))
    sys.exit(main(*sys.argv[1:4]))

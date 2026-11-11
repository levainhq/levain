# rename-intention: never-touch
"""Builds the OLD-release cockpit journal the rename tests replay (tests/test_rename_h2_journal_replay.py).

Not run by the suite. It drives the release that still said "spore" (the tree at base a8617e55c, a checkout of
it) through its own broker: one terminal op with a label, one op that ends COMMITTED_INCOMPLETE, four ops left
DECIDED at a kill point (an update, a create whose store write is already durable, a resolve and a promote) and one
reported op still WAITING. The output is what that release left on disk, so no byte of it is written by the code under test.

    python3 make_old_journal.py <old-tree> <anneal-memory-checkout> <out-dir>

``<out-dir>`` receives ``old_journal.sql`` (``sqlite3.Connection.iterdump`` of ``cockpit.db``, a checkpointed
WAL), ``old_store.json`` (the intention store beside it) and ``old_journal.json`` (which op is which).
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
        """A broker that settles only the op it is asked to issue: starting one recovers every DECIDED op, which
        would finish the ops this builder leaves DECIDED on purpose."""

        def _recover(self, now, db=None):
            return 0

        def _redrive(self):
            return None

    broker = Unsettled(cfg["data_root"], stores={"spores": SporeEffectStore(cfg["store"])}, store_paths=[cfg["store"]],
                       install_class="reported", kill_at=cfg["kill_at"])
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
    from levain.cockpit.broker import Broker
    from levain.cockpit.effects import SporeEffectStore
    from tests.autonomic_confirm_keys import anchor_scratch

    home = anchor_scratch("h2-old-journal-")
    data, store = home / "data", home / "spores.json"
    st = SporeStore(store)
    for key, kw in (("k.a", dict(type="task", text="alpha", tier="warm")),
                    ("k.b", dict(type="thought", text="beta", disposition="seed")),
                    ("k.c", dict(type="task", text="gamma", tier="warm")),
                    ("k.e", dict(type="task", text="epsilon", tier="warm")),
                    ("k.f", dict(type="task", text="zeta", tier="warm")),
                    ("k.g", dict(type="task", text="eta", tier="warm"))):
        st.add(origin_key=key, **kw)

    def target(key: str | None) -> dict:
        if key is None:
            return {"kind": "create", "panel_id": "tray"}
        row = SporeStore(store).get_by_origin_key(key)
        version = SporeEffectStore(store).snapshot(f"spore:{key}").version
        return {"kind": "row", "panel_id": "tray", "row_id": f"spore:{row['id']}", "row_version": version}

    def run(name: str, verb: str, params: dict, key: str | None, kill_at: str | None) -> int:
        cfg = {"old_tree": old_tree, "anneal": anneal, "data_root": str(data), "store": str(store),
               "kill_at": kill_at, "verb": verb, "params": params, "target": target(key), "key": f"idem-{name}"}
        proc = subprocess.run([sys.executable, __file__, "--child", json.dumps(cfg)], capture_output=True, text=True,
                              timeout=120, cwd=old_tree)
        want = 137 if kill_at else 0
        assert proc.returncode == want, f"{name}: rc={proc.returncode}\n{proc.stdout}\n{proc.stderr}"
        return proc.returncode

    run("a", "spore_update", {"text": "alpha edited"}, "k.a", None)                          # COMMITTED, one label
    run("b", "spore_set_disposition", {"disposition": "agenda"}, "k.b", "decided")           # DECIDED ...
    row_b = SporeStore(store).get_by_origin_key("k.b")
    SporeStore(store).update(row_b["id"], text="beta moved under it")                         # ... the row moves
    broker = Broker(data, stores={"spores": SporeEffectStore(store)}, store_paths=[store], install_class="reported")
    broker.start()
    broker.recover()                                                                           # -> COMMITTED_INCOMPLETE
    broker.stop()
    run("c", "spore_update", {"text": "gamma edited"}, "k.c", "decided")
    run("d", "spore_seed", {"text": "captured", "type": "task"}, None, "applied:0")
    run("e", "spore_descend", {"spore_kind": "done"}, "k.e", "decided")
    run("f", "spore_ascend", {"spore_kind": "project", "ref": "commit abc"}, "k.f", "decided")
    run("g", "spore_update", {"text": "eta edited"}, "k.g", "verified")                      # still WAITING

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

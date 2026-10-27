"""O-p2-1 slice B1a: the ``levain-autonomic/3`` ``effects`` table holds attempts (levain project_memory
``1026-27/O-p2-1_DESIGN.md``, sections 2-5 as amended by 9-14).

The first test is R2-8's: fingerprint parity on the A6 rebuild plan (rename the OLD table away, create the authored
text, copy with attempt 1, drop), run over a POPULATED ``/2`` store that V's own opener made."""
from __future__ import annotations

import json
import sqlite3

from levain.autonomic import db as vdb
from levain.autonomic import schema3 as s
from levain.autonomic.db import AutonomicDB


def canon(o) -> str:
    return json.dumps(o, sort_keys=True, separators=(",", ":"))


def _populated_v2(d):
    """A /2 store made by V's opener (db.py), with rows in the table the rebuild copies."""
    AutonomicDB(d).meta("format")
    c = sqlite3.connect(d / vdb.DB_NAME, isolation_level=None)
    c.execute("INSERT INTO runs (run_id, binding_id, generation) VALUES ('r', 'b', 0)")
    c.execute("INSERT INTO effects (run_id, effect_id, digest, state, receipt_id, result) "
              "VALUES ('r', 'link-0', 'd0', 'done', 'v-r0', 'ok')")
    c.execute("INSERT INTO effects (run_id, effect_id, digest, pid, state) VALUES ('r', 'link-1', 'd1', 7, 'intent')")
    c.execute("INSERT INTO holds (hold_id, run_id, effect_id, binding_id, digest, seq) "
              "VALUES ('h', 'r', 'link-2', 'b', 'd2', 1)")
    c.close()


def _master(c) -> list:
    return sorted(c.execute("SELECT type, name, tbl_name, sql FROM sqlite_master").fetchall(), key=repr)


# ---- R2-8: the build lane's FIRST test ------------------------------------------------------------------------------
def test_a_fresh_store_and_ddl_v3_over_a_populated_v2_store_share_one_sqlite_master(tmp_path):
    fresh = s.AutonomicStore3(tmp_path / "fresh")
    fresh.meta("format")
    with fresh.read() as c:
        want = _master(c)
    d = tmp_path / "v2"
    _populated_v2(d)
    c = sqlite3.connect(d / vdb.DB_NAME, isolation_level=None)
    try:
        c.execute("BEGIN IMMEDIATE")
        s.ddl_v3(c)
        s.triggers_v3(c)
        assert _master(c) == want
        assert s.fingerprint(c) == s.expected_fingerprint()
        # A6(a): the stored DDL is the authored text, not SQLite's rename rewrite
        assert c.execute("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'effects'").fetchone() == (
            s.EFFECTS_V3,)
        assert c.execute("SELECT run_id, effect_id, digest, pid, state, result, receipt_id, seal, legacy, attempt "
                         "FROM effects ORDER BY effect_id").fetchall() == [
            ("r", "link-0", "d0", None, "done", "ok", "v-r0", None, 0, 1),
            ("r", "link-1", "d1", 7, "intent", None, None, None, 0, 1)]
        assert "effects_v2" not in {n for (n,) in c.execute("SELECT name FROM sqlite_master")}
        c.execute("ROLLBACK")
    finally:
        c.close()

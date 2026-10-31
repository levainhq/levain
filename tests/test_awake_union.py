"""P6s: ONE sleep reconciler over both sources (the coordinator's working threads and the agents' live occurrences).

Built from a two-process run (A0 §2.5: an occurrence is live while its state is ``admitted`` or ``launched``): process
1 holds a live occurrence, process 2's last work thread ends and its sync runs; the holder STAYS. On the unmodified
code (one-source ``sync``) the same run released the holder, so the first test fails there. The holder is a harmless
``sleep`` in every process; no real assertion is ever taken, and every store is a scratch directory."""
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time

import pytest

from levain.autonomic.db import DB_NAME, AutonomicDB
from levain.coordinator import awake

pytestmark = pytest.mark.skipif(os.name != "posix" or shutil.which("sleep") is None or shutil.which("ps") is None,
                                reason="needs POSIX ps + sleep")

NOW = "2026-10-31T10:00:00Z"
PRELUDE = f"""
import shutil, sys
from levain.coordinator import awake
awake._IN_TESTS = True
awake.holder_command = lambda *a, **k: [shutil.which("sleep"), "90"]
"""
AGENT = PRELUDE + """
import sqlite3
from pathlib import Path
q, a, oid, action = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3], sys.argv[4]
c = sqlite3.connect(a / "autonomic.db", isolation_level=None)
if action == "start":
    c.execute("INSERT INTO agent_occurrences (occurrence_id, agent_id, kind, key, state, window_key, spec_hash,"
              " binding_id, run_id, admitted_at) VALUES (?, 'ag-1', 'scheduled', ?, 'admitted', 'w', 's', 'b', ?, ?)",
              (oid, oid, "run-" + oid, sys.argv[5]))
else:
    c.execute("UPDATE agent_occurrences SET state='skipped-budget', reason='x', ended_at=? WHERE occurrence_id=?",
              (sys.argv[5], oid))
print(awake.sync(directory=q, agent_dir=a))
"""
COORD = PRELUDE + """
from pathlib import Path
from levain.coordinator.store import CoordStore, Part
q, action = Path(sys.argv[1]), sys.argv[2]
s = CoordStore(q)
if action == "work":
    s.coord_set_agent("claude", cwd=sys.argv[3])
    d, _ = s.accept_drop("w", surface="cli")
    ln = s.coord_status()["launches"][0]
    s.launch_activate(ln["launch_id"], harness_session_id="u1")
    tok = s.coord_claim(ln["launch_id"], s.launch(ln["launch_id"]).session_key)
    t, _ = s.split(d.drop_id, [Part(kind="work", title="w"), Part(kind="note", title="n")], by="coordinator", token=tok)
    s.set_state(t.thread_id, "working", by="coordinator", token=tok)
    print(d.drop_id, t.thread_id)
else:
    s.detach(sys.argv[3], sys.argv[4], by="operator")      # the last member out: the work thread closes; sync runs
"""


@pytest.fixture
def world(tmp_path, monkeypatch):
    q = tmp_path / "consent"
    q.mkdir(mode=0o700)
    home = tmp_path / "home"
    adir = home / "autonomic"
    with AutonomicDB(adir).write():       # a scratch autonomic store with the agent tables
        pass
    monkeypatch.setenv("LEVAIN_HOME", str(home))
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join(sys.path))
    yield q, adir
    if (q / "awake.json").exists():
        awake._stop(json.loads((q / "awake.json").read_text()))


def run(script, *args):
    cp = subprocess.run([sys.executable, "-c", script, *map(str, args)], capture_output=True, text=True, timeout=120,
                        env={**os.environ, "LEVAIN_NO_IDLE_ASSERTION": ""})
    assert cp.returncode == 0, cp.stderr[-800:]
    return cp.stdout.strip()


def rec(q):
    return json.loads((q / "awake.json").read_text())


def test_the_last_work_thread_ending_leaves_a_live_occurrence_holding_the_host(world, tmp_path):
    q, adir = world
    run(AGENT, q, adir, "occ-1", "start", NOW)                              # process 1: a live occurrence
    first = rec(q)
    assert first["source"] == "agent" and first["occurrences"] == ["occ-1"] and awake._ours(first)
    drop, thread = run(COORD, q, "work", tmp_path).split()                  # process 2: a work thread works
    both = rec(q)
    assert both["source"] == "coordinator+agent" and both["threads"] == [thread] and awake._ours(both)
    run(COORD, q, "end", thread, drop)                                      # its last work thread ends, sync runs
    kept = rec(q)
    assert awake._ours(kept), "the holder was released under a live occurrence"
    assert kept["pid"] == both["pid"] and kept["threads"] == [] and kept["occurrences"] == ["occ-1"]
    assert kept["source"] == "agent"
    out = run(AGENT, q, adir, "occ-1", "end", NOW)                          # the occurrence ends: both are empty
    assert "'held': False" in out and not (q / "awake.json").exists() and not awake._ours(kept)


def test_a_work_thread_alone_still_holds_and_nothing_at_all_releases(world, tmp_path):
    q, adir = world
    drop, thread = run(COORD, q, "work", tmp_path).split()
    held = rec(q)
    assert held["source"] == "coordinator" and held["occurrences"] == [] and awake._ours(held)
    run(COORD, q, "end", thread, drop)
    assert not (q / "awake.json").exists() and not awake._ours(held)


def test_sync_never_trusts_the_caller_the_agent_side_sees_the_coordinators_thread(world, tmp_path):
    q, adir = world
    drop, thread = run(COORD, q, "work", tmp_path).split()
    out = run(AGENT, q, adir, "occ-2", "start", NOW)           # the agent side re-reads the coordinator half itself
    assert f"'threads': ['{thread}']" in out and "'occurrences': ['occ-2']" in out
    run(AGENT, q, adir, "occ-2", "end", NOW)                   # an occurrence ending leaves the thread's hold
    assert awake._ours(rec(q)) and rec(q)["source"] == "coordinator"


def test_an_unreadable_agent_store_never_releases_the_holder(world, tmp_path):
    q, adir = world
    run(AGENT, q, adir, "occ-3", "start", NOW)
    held = rec(q)
    (adir / DB_NAME).write_bytes(b"not a database" * 100)
    out = run(PRELUDE + "from pathlib import Path\nprint(awake.sync(directory=Path(sys.argv[1]), agent_dir=Path(sys.argv[2])))",
              q, adir)
    assert "could not sync" in out and awake._ours(rec(q)) and rec(q)["pid"] == held["pid"]


def test_no_agent_store_or_table_reads_as_no_occurrence(tmp_path):
    assert awake.live_occurrences(tmp_path) == []
    c = sqlite3.connect(tmp_path / DB_NAME)
    c.execute("CREATE TABLE other (x)")
    c.close()
    assert awake.live_occurrences(tmp_path) == []
    assert sorted(os.listdir(tmp_path)) == [DB_NAME]      # read-only: no journal or other file was created


def test_doctor_line_names_the_source_and_the_occurrence(tmp_path, monkeypatch):
    monkeypatch.setattr(awake, "_ours", lambda r: True)
    (tmp_path / awake.FILE).write_text(json.dumps({"pid": 77, "until": time.time() + 99, "threads": [],
                                                   "occurrences": ["occ-9"], "source": "agent"}))
    ok, line = awake.describe(tmp_path, working_count=0, occurrence_count=1)
    assert ok and "held by agent" in line and "occ-9" in line, line
    (tmp_path / awake.FILE).unlink()
    ok, line = awake.describe(tmp_path, working_count=0, occurrence_count=2)
    assert not ok and "2 agent occurrence(s) are live" in line, line

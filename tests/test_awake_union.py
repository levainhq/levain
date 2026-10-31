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
    assert sorted(os.listdir(tmp_path)) == [DB_NAME]      # a rollback-journal store: a read leaves no other file


def test_doctor_line_names_the_source_and_the_occurrence(tmp_path, monkeypatch):
    monkeypatch.setattr(awake, "_ours", lambda r: True)
    (tmp_path / awake.FILE).write_text(json.dumps({"pid": 77, "until": time.time() + 99, "threads": [],
                                                   "occurrences": ["occ-9"], "source": "agent"}))
    ok, line = awake.describe(tmp_path, working_count=0, occurrence_count=1)
    assert ok and "held by agent" in line and "occ-9" in line, line
    (tmp_path / awake.FILE).unlink()
    ok, line = awake.describe(tmp_path, working_count=0, occurrence_count=2)
    assert not ok and "2 agent occurrence(s) are live" in line, line


def _insert(adir, oid, launched=None):
    """A claim (admitted), then the launch transition when ``launched`` is given: the triggers allow no other path."""
    c = sqlite3.connect(adir / DB_NAME, isolation_level=None)
    try:
        c.execute("INSERT INTO agent_occurrences (occurrence_id, agent_id, kind, key, state, window_key, spec_hash,"
                  " binding_id, run_id, admitted_at) VALUES (?, 'ag-1', 'scheduled', ?, 'admitted', 'w', 's', 'b', ?, ?)",
                  (oid, oid, "run-" + oid, NOW))
        if launched:
            c.execute("UPDATE agent_occurrences SET state='launched', launch_epoch=0, link_id='l', link_incarnation='i',"
                      " launched_at=? WHERE occurrence_id=?", (launched, oid))
    finally:
        c.close()


def test_a_read_of_the_real_wal_store_writes_no_row_and_adds_only_sidecars(tmp_path):
    adir = tmp_path / "autonomic"
    with AutonomicDB(adir).write():          # the store the way levain/agents makes it
        pass
    c = sqlite3.connect(adir / DB_NAME)
    assert c.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    c.close()
    _insert(adir, "occ-1")
    _insert(adir, "occ-2", launched="2026-10-31T10:05:00Z")
    before_files, before_bytes = set(os.listdir(adir)), (adir / DB_NAME).read_bytes()
    got = awake.live_occurrence_details(adir)
    assert [(o["id"], o["state"], o["since"]) for o in got] == [
        ("occ-1", "admitted", NOW), ("occ-2", "launched", "2026-10-31T10:05:00Z")]
    assert awake.live_occurrences(adir) == ["occ-1", "occ-2"]
    assert (adir / DB_NAME).read_bytes() == before_bytes                      # no content change in the main file
    assert set(os.listdir(adir)) - before_files <= {DB_NAME + "-wal", DB_NAME + "-shm"}   # sidecars, nothing else
    c = sqlite3.connect(f"file:{adir / DB_NAME}?mode=ro", uri=True)
    assert c.execute("SELECT occurrence_id, state FROM agent_occurrences ORDER BY 1").fetchall() == [
        ("occ-1", "admitted"), ("occ-2", "launched")]                         # the same two rows, none added
    c.close()


def test_doctor_line_names_each_occurrences_state_and_age_and_the_oldest(tmp_path, monkeypatch):
    monkeypatch.setattr(awake, "_ours", lambda r: True)
    now = awake._epoch("2026-10-31T13:12:00Z")
    live = [{"id": "occ-a", "state": "launched", "since": "2026-10-31T10:00:00Z"},
            {"id": "occ-b", "state": "admitted", "since": "2026-10-31T13:11:30Z"},
            {"id": "occ-c", "state": "admitted", "since": "not a time"}]
    (tmp_path / awake.FILE).write_text(json.dumps({"pid": 77, "until": now + 99, "threads": [], "occurrences": ["occ-a"],
                                                   "source": "agent"}))
    ok, line = awake.describe(tmp_path, working_count=0, occurrence_count=3, occurrences=live, now=now)
    assert ok and "occ-a (launched, 3h12m)" in line and "occ-b (admitted, 30s)" in line, line
    assert "occ-c (admitted, age unknown)" in line and "oldest live: 3h12m" in line, line
    (tmp_path / awake.FILE).unlink()                                       # not held: the same state and age still show
    ok, line = awake.describe(tmp_path, working_count=0, occurrence_count=3, occurrences=live, now=now)
    assert not ok and "3 agent occurrence(s) are live" in line and "occ-a (launched, 3h12m)" in line, line


def test_the_occurrence_only_branch_makes_the_same_two_checks_as_the_work_branch(tmp_path, monkeypatch):
    monkeypatch.setattr(awake, "holder_command", lambda *a, **k: ["caffeinate"])
    monkeypatch.setattr(awake, "fcntl", None)
    for kw in ({"working_count": 0, "occurrence_count": 2}, {"working_count": 1, "occurrence_count": 0}):
        ok, line = awake.describe(tmp_path, **kw)
        assert not ok and "no POSIX file locks" in line and "may sleep under them" in line, line
    monkeypatch.setattr(awake, "fcntl", object())
    monkeypatch.setattr(awake, "holder_command", lambda *a, **k: None)
    for kw in ({"working_count": 0, "occurrence_count": 2}, {"working_count": 1, "occurrence_count": 0}):
        ok, line = awake.describe(tmp_path, **kw)
        assert not ok and "no idle-sleep tool" in line and "may sleep under them" in line, line
    monkeypatch.setattr(awake, "holder_command", lambda *a, **k: ["caffeinate"])
    ok, line = awake.describe(tmp_path, working_count=0, occurrence_count=2)
    assert not ok and "set on the next sync" in line, line


def test_a_dead_or_expired_holder_with_a_live_occurrence_is_respawned(world):
    q, adir = world
    run(AGENT, q, adir, "occ-1", "start", NOW)
    first = rec(q)
    os.kill(first["pid"], 15)                                                # the holder dies; awake.json is stale
    for _ in range(100):
        if not awake._ours(first):
            break
        time.sleep(0.05)
    assert not awake._ours(first)
    run(AGENT, q, adir, "occ-2", "start", NOW)
    second = rec(q)
    assert second["pid"] != first["pid"] and awake._ours(second) and second["occurrences"] == ["occ-1", "occ-2"]
    (q / "awake.json").write_text(json.dumps({**second, "until": time.time() - 5}))    # alive but past its record
    run(AGENT, q, adir, "occ-3", "start", NOW)
    third = rec(q)
    assert third["pid"] != second["pid"] and awake._ours(third) and third["occurrences"] == ["occ-1", "occ-2", "occ-3"]
    for _ in range(100):                                                     # the replaced holder is stopped
        if not awake._ours(second):
            break
        time.sleep(0.05)
    assert not awake._ours(second)


def test_the_same_live_holder_is_rewritten_when_the_lists_change(world):
    q, adir = world
    run(AGENT, q, adir, "occ-1", "start", NOW)
    first = rec(q)
    run(AGENT, q, adir, "occ-2", "start", NOW)                               # same holder, record now names both
    second = rec(q)
    assert second["pid"] == first["pid"] and second["started"] == first["started"] and awake._ours(second)
    assert second["occurrences"] == ["occ-1", "occ-2"] and second["until"] == first["until"]
    run(AGENT, q, adir, "occ-1", "end", NOW)
    third = rec(q)
    assert third["pid"] == first["pid"] and third["occurrences"] == ["occ-2"] and third["source"] == "agent"


def test_the_doctor_row_names_the_source_the_occurrence_its_state_and_age(world, monkeypatch):
    from levain import install
    from levain.doctor import _check_host_awake

    q, adir = world
    monkeypatch.setattr(install, "_consent_queue_dir", lambda: q)
    run(AGENT, q, adir, "occ-1", "start", NOW)
    [row] = _check_host_awake()
    assert row.ok and not row.warn, row
    assert "held by agent" in row.detail and "occ-1 (admitted, " in row.detail and "oldest live: " in row.detail, row.detail
    run(AGENT, q, adir, "occ-1", "end", NOW)
    assert _check_host_awake() == []                                        # nothing live, no coordinator store: no row


def test_an_unreadable_agents_store_still_takes_the_coordinators_hold(world, tmp_path):
    q, adir = world
    (adir / DB_NAME).write_bytes(b"not a database" * 100)
    drop, thread = run(COORD, q, "work", tmp_path).split()                  # the coordinator side has work, agents unknown
    held = rec(q)
    assert awake._ours(held) and held["source"] == "coordinator" and held["threads"] == [thread]
    assert held["unknown"] == ["agent"]
    out = run(COORD, q, "end", thread, drop)                                 # its work ends; the agents store is still unknown
    assert awake._ours(rec(q)) and rec(q)["pid"] == held["pid"], "the holder was released with a source unknown"


def test_an_unreadable_coordinator_store_still_takes_the_agents_hold_and_both_unknown_holds_nothing_new(world):
    q, adir = world
    (q / "dispatch.db").write_bytes(b"not a database" * 100)
    out = run(AGENT, q, adir, "occ-1", "start", NOW)
    assert "'held': True" in out and "'unknown': ['coordinator']" in out, out
    held = rec(q)
    assert awake._ours(held) and held["source"] == "agent" and held["unknown"] == ["coordinator"]
    run(AGENT, q, adir, "occ-1", "end", NOW)                                 # the occurrence ends: coordinator still unknown
    assert awake._ours(rec(q)) and rec(q)["pid"] == held["pid"]
    (adir / DB_NAME).write_bytes(b"not a database" * 100)                    # now both are unreadable
    out = run(PRELUDE + "from pathlib import Path\nprint(awake.sync(directory=Path(sys.argv[1]), agent_dir=Path(sys.argv[2])))",
              q, adir)
    assert "could not sync" in out and "coordinator and agent" in out and awake._ours(rec(q))
    assert rec(q)["pid"] == held["pid"]


# ---- fix round 1 (L3 r1): absence is only a confirmed FileNotFoundError; directory= is required without a store ----

def _in_process(monkeypatch):
    monkeypatch.setattr(awake, "_IN_TESTS", True)
    monkeypatch.setattr(awake, "holder_command", lambda *a, **k: [shutil.which("sleep"), "90"])
    monkeypatch.delenv("LEVAIN_NO_IDLE_ASSERTION", raising=False)


def _holder_survives(q, adir, held):
    out = awake.sync(directory=q, agent_dir=adir)
    assert "could not sync" in out["why"] and out["unknown"] == ["agent"], out
    assert awake._ours(rec(q)) and rec(q)["pid"] == held["pid"], "the holder was released with the agents store unreadable"


def test_an_unsearchable_agent_store_parent_is_unknown_and_the_holder_stays(world, monkeypatch):
    q, adir = world
    run(AGENT, q, adir, "occ-1", "start", NOW)
    held = rec(q)
    _in_process(monkeypatch)
    adir.chmod(0)
    try:
        if os.access(adir / DB_NAME, os.F_OK):
            pytest.skip("this uid ignores directory permissions (root)")
        with pytest.raises(PermissionError):
            awake.live_occurrence_details(adir)
        _holder_survives(q, adir, held)
    finally:
        adir.chmod(0o700)


def test_a_broken_symlink_for_the_agent_store_is_unknown_and_the_holder_stays(world, monkeypatch, tmp_path):
    q, adir = world
    run(AGENT, q, adir, "occ-1", "start", NOW)
    held = rec(q)
    _in_process(monkeypatch)
    (adir / DB_NAME).rename(adir / "aside.db")
    (adir / DB_NAME).symlink_to(tmp_path / "nowhere.db")
    with pytest.raises(OSError):
        awake.live_occurrence_details(adir)
    _holder_survives(q, adir, held)


def test_a_directory_in_place_of_the_agent_store_is_unknown_and_the_holder_stays(world, monkeypatch):
    q, adir = world
    run(AGENT, q, adir, "occ-1", "start", NOW)
    held = rec(q)
    _in_process(monkeypatch)
    (adir / DB_NAME).rename(adir / "aside.db")
    (adir / DB_NAME).mkdir()
    with pytest.raises(OSError, match="not a regular file"):
        awake.live_occurrence_details(adir)
    _holder_survives(q, adir, held)


def test_only_a_confirmed_absence_is_empty(tmp_path):
    assert awake.live_occurrence_details(tmp_path) == []                 # the directory exists, the file does not
    assert awake.live_occurrence_details(tmp_path / "no" / "such") == []   # nor does the directory: still FileNotFoundError


def test_sync_without_a_store_or_a_directory_names_the_missing_directory_and_touches_nothing(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("LEVAIN_HOME", str(home))
    _in_process(monkeypatch)
    from levain.consent.queue import default_queue_dir

    dq = default_queue_dir()
    dq.mkdir(parents=True, mode=0o700)          # it exists, so a silent fall back would take a lock here
    out = awake.sync(agent_dir=tmp_path)
    assert out["held"] is False and "no coordinator queue directory was given" in out["why"], out
    assert list(dq.iterdir()) == [], "the default queue directory was used"
    assert not list(tmp_path.glob("awake.*"))


def test_the_doctor_row_describes_a_live_holder_with_nothing_left_and_no_coordinator_store(world, monkeypatch):
    from levain import install
    from levain.doctor import _check_host_awake

    q, adir = world
    monkeypatch.setattr(install, "_consent_queue_dir", lambda: q)
    run(AGENT, q, adir, "occ-1", "start", NOW)
    c = sqlite3.connect(adir / DB_NAME, isolation_level=None)             # terminal, and the process died before its sync
    c.execute("UPDATE agent_occurrences SET state='skipped-budget', reason='x', ended_at=? WHERE occurrence_id='occ-1'", (NOW,))
    c.close()
    assert (q / "awake.json").exists() and awake._ours(rec(q))
    [row] = _check_host_awake()
    assert row.ok and not row.warn and "holder alive with no current work seen" in row.detail, row.detail


def test_the_doctor_row_keeps_the_coordinator_side_when_the_agent_store_is_unreadable(world, monkeypatch, tmp_path):
    from levain import install
    from levain.doctor import _check_host_awake

    q, adir = world
    monkeypatch.setattr(install, "_consent_queue_dir", lambda: q)
    drop, thread = run(COORD, q, "work", tmp_path).split()
    (adir / DB_NAME).write_bytes(b"not a database" * 100)
    [row] = _check_host_awake()
    assert "agent store unreadable" in row.detail and row.warn and "1 work thread(s) working" in row.detail, row.detail
    assert "not read" not in row.detail


def test_a_held_line_never_renders_an_empty_who(tmp_path, monkeypatch):
    monkeypatch.setattr(awake, "_ours", lambda r: True)
    (tmp_path / awake.FILE).write_text(json.dumps({"pid": 77, "until": time.time() + 99, "threads": [],
                                                   "occurrences": [], "source": "coordinator"}))
    ok, line = awake.describe(tmp_path, working_count=0, occurrence_count=0, occurrences=[])
    assert ok and "no live work seen now" in line and ": at its" not in line and ":  " not in line, line

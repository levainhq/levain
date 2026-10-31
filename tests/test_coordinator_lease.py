"""The coordinator lease and launch rows (C3a; C0 §6, §3.4). Built from the run in
project_memory 1020-coordinator/c3/run_c3a.txt: each test below replays one of its phases."""
from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from levain.cockpit.consent import utc_stamp
from levain.coordinator import lease
from levain.coordinator.store import CoordError, CoordStore, Part

T0 = datetime(2026, 10, 20, 20, 0, tzinfo=timezone.utc)


class Clock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kw: float) -> None:
        self.now += timedelta(**kw)


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def qdir(tmp_path):
    d = tmp_path / "consent"
    d.mkdir(mode=0o700)
    return d


@pytest.fixture
def store(qdir, clock):
    # a wake launches only the coordinator you set (C3b): every test here sets one first, as you would
    s = CoordStore(qdir, clock=clock)
    s.coord_set_agent("claude", cwd=str(qdir.parent))
    return s


def drop(store, words="fix the build"):
    return store.accept_drop(words, surface="cli")[0]


def claimed(store):
    """The pending coordinator launch's spawn claimed by a starter that then died (C3b fix 2 G6: a launch nobody tried
    to start is no death when it expires)."""
    ln = store.pending_coordinator()
    assert store.launch_claim_spawn(ln.launch_id, 1)[1] is None
    return ln


def boot(store):
    """A drop with no lease: its pending coordinator launch, activated."""
    drop(store)
    ln = store.coord_status()["launches"][0]
    got, why = store.launch_activate(ln["launch_id"], harness_session_id="probe-A")
    assert why is None and got.state == "active"
    return got


def rows(qdir, sql):
    c = sqlite3.connect(qdir / "dispatch.db")
    try:
        return c.execute(sql).fetchall()
    finally:
        c.close()


_RACER = """
import json, sys, time
sys.path.insert(0, sys.argv[1])
from levain.coordinator.store import CoordStore
store = CoordStore(sys.argv[2])
time.sleep(max(0.0, float(sys.argv[3]) - time.time()))
d, _ = store.accept_drop("race drop", surface="cli")
print(json.dumps(d.drop_id))
"""


def test_two_processes_dropping_at_once_ask_for_one_coordinator(qdir, tmp_path):
    # run phase 1: two real processes, one instant, no lease -> one pending launch and one bootstrap claim
    # the store exists already, as on any install past its first use, with a coordinator set (C3b)
    CoordStore(qdir).coord_set_agent("claude", cwd=str(tmp_path))
    script = tmp_path / "racer.py"
    script.write_text(_RACER)
    tree = str(Path(lease.__file__).resolve().parents[2])
    start = str(time.time() + 1.0)
    procs = [subprocess.Popen([sys.executable, str(script), tree, str(qdir), start], stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, text=True) for _ in range(2)]
    outs = [p.communicate(timeout=60) for p in procs]
    assert [p.returncode for p in procs] == [0, 0], outs
    assert len({json.loads(o) for o, _ in outs}) == 2
    assert rows(qdir, "SELECT role, token FROM launches") == [("coordinator", 1)]
    assert rows(qdir, "SELECT seq, kind, cause, by FROM lease_events") == [(1, "claim", "bootstrap", "service")]


def test_a_pending_bootstrap_holds_the_lease_but_its_writes_wait_for_active(store, clock):
    d = drop(store)
    st = store.coord_status()
    assert st["wake"] == "launching" and st["lease"]["cause"] == "bootstrap"
    with pytest.raises(CoordError, match="not an active launch"):
        store.split(d.drop_id, [Part("work", "x")], by="coordinator", token=st["lease"]["token"])
    drop(store, "a second drop only queues")
    assert len(store.coord_status()["launches"]) == 1


def test_activation_gives_the_holder_a_full_ttl_and_its_writes_count(store, clock):
    ln = boot(store)
    held = store.coord_status()["lease"]
    assert held["holder"] == ln.session_key
    assert held["until"] == utc_stamp(T0 + lease.LEASE_TTL)
    d = drop(store, "split me")
    t = store.split(d.drop_id, [Part("work", "x")], by="coordinator", token=held["token"])[0]
    assert store.set_state(t.thread_id, "working", by="coordinator", token=held["token"]).state == "working"


def test_the_stale_token_is_refused_after_a_rotation_claim(store, clock):
    # run phases 4 and 5
    first = boot(store)
    old = store.coord_status()["lease"]["token"]
    rot = store.launch_request(old, harness="claude", role="coordinator")
    assert store.launch_activate(rot.launch_id, harness_session_id="probe-B")[1] is None
    with pytest.raises(CoordError, match="held by"):
        store.coord_claim(rot.launch_id, rot.session_key)          # the old holder still holds it
    store.coord_release(old, by="coordinator")
    new = store.coord_claim(rot.launch_id, rot.session_key)
    assert new != old
    d = drop(store, "after the rotation")
    with pytest.raises(CoordError, match=f"lease token {old} is not the live lease's"):
        store.split(d.drop_id, [Part("work", "x")], by="coordinator", token=old)
    with pytest.raises(CoordError, match="held by"):
        store.coord_claim(first.launch_id, first.session_key)
    assert store.split(d.drop_id, [Part("work", "x")], by="coordinator", token=new)


def test_a_holder_past_its_until_is_refused_and_the_lapse_is_written(store, clock, qdir):
    # run phase 6
    boot(store)
    token = store.coord_status()["lease"]["token"]
    d = drop(store, "late")
    clock.advance(minutes=31)
    with pytest.raises(CoordError, match="past its until"):
        store.split(d.drop_id, [Part("work", "x")], by="coordinator", token=token)
    assert store.coord_status()["lease"] is None          # the partial write rolled back with the wake
    assert rows(qdir, "SELECT kind, token FROM lease_events ORDER BY seq DESC LIMIT 1") == [("lapse", token)]
    drop(store, "the coordinator is still running: no second one beside it")
    st = store.coord_status()
    assert st["wake"] == "idle" and len(st["launches"]) == 1
    ln = st["launches"][0]
    assert store.coord_claim(ln["launch_id"], ln["session_key"]) > token


def test_a_beat_keeps_the_lease_and_never_changes_the_token(store, clock):
    boot(store)
    token = store.coord_status()["lease"]["token"]
    for _ in range(3):
        clock.advance(minutes=20)
        held = store.coord_beat(token)
        assert held.token == token and held.live
    with pytest.raises(CoordError, match="not the live lease's"):
        store.coord_beat(token + 100)


def test_a_take_voids_the_pending_launch_and_it_never_activates(store, clock):
    # run phase 7
    drop(store)
    pending = store.coord_status()["launches"][0]
    tok = store.coord_take()
    assert store.coord_status()["lease"]["holder"] == "operator"
    got, why = store.launch_activate(pending["launch_id"], harness_session_id="probe-C")
    assert got.state == "failed" and "not pending" in why
    d = drop(store, "you hold it: a drop only queues")
    assert len(store.coord_status()["launches"]) == 1
    assert store.split(d.drop_id, [Part("work", "x")], by="coordinator", token=tok)


def test_a_launch_asked_for_under_a_lapsed_lease_fails_at_activation(store, clock):
    boot(store)                                    # the holder's lease runs to T0 + LEASE_TTL
    token = store.coord_status()["lease"]["token"]
    d = drop(store, "work")
    t = store.split(d.drop_id, [Part("work", "x")], by="coordinator", token=token)[0]
    clock.advance(seconds=(lease.LEASE_TTL - timedelta(minutes=2)).total_seconds())
    worker = store.launch_request(token, harness="claude", threads=[t.thread_id])
    clock.advance(minutes=3)                       # the lease is past its until; the worker's pending window is not
    got, why = store.launch_activate(worker.launch_id, harness_session_id="probe-W")
    assert got.state == "failed" and got.cause == "lease lost before active"
    assert f"token {token}, which is no longer live" in why


def test_a_pending_launch_past_its_until_expires_at_activation(store, clock):
    drop(store)
    pending = store.coord_status()["launches"][0]
    clock.advance(seconds=lease.LAUNCH_TTL.total_seconds())
    got, why = store.launch_activate(pending["launch_id"], harness_session_id="late")
    assert got.state == "expired" and "pending past" in why


def test_your_release_voids_a_pending_worker(store, clock):
    boot(store)
    token = store.coord_status()["lease"]["token"]
    d = drop(store, "work")
    t = store.split(d.drop_id, [Part("work", "x")], by="coordinator", token=token)[0]
    worker = store.launch_request(token, harness="codex", threads=[t.thread_id])
    store.coord_release()
    got, why = store.launch_activate(worker.launch_id, harness_session_id="thread-1")
    assert got.state == "failed" and got.cause == "operator release" and "not pending" in why


def test_three_coordinator_deaths_stop_the_wakes_until_you_look(store, clock):
    for n, wait in enumerate((None, lease.BACKOFF[0], lease.BACKOFF[1])):
        if wait is not None:
            drop(store, f"too soon {n}")
            assert store.coord_status()["wake"] == "backoff"
            clock.advance(seconds=wait.total_seconds())
        drop(store, f"drop {n}")
        assert store.coord_status()["wake"] == "launching"
        claimed(store)
        clock.advance(seconds=lease.LAUNCH_TTL.total_seconds())   # its starter died: it expires
    drop(store, "after three deaths")
    st = store.coord_status()
    assert st["wake"] == "stopped" and "until you look" in st["why"]
    assert all(ln["state"] == "expired" for ln in st["launches"])
    store.coord_release()                                          # you looked
    drop(store, "the wakes run again")
    assert store.coord_status()["wake"] == "launching"


def test_a_coordinator_dead_at_zero_turns_is_a_death_and_releases_its_lease(store, clock):
    ln = boot(store)
    store.launch_end(ln.launch_id, "ended", "0 turns")
    assert store.coord_status()["lease"] is None
    drop(store, "the next drop waits out the backoff")
    assert store.coord_status()["wake"] == "backoff"


def test_only_an_active_coordinator_with_its_own_session_key_claims(store, clock):
    ln = boot(store)
    token = store.coord_status()["lease"]["token"]
    d = drop(store)
    t = store.split(d.drop_id, [Part("work", "x")], by="coordinator", token=token)[0]
    worker = store.launch_request(token, harness="claude", threads=[t.thread_id])
    store.launch_activate(worker.launch_id, harness_session_id="w")
    store.coord_release(token, by="coordinator")                   # the holder hands off; its launch keeps running
    with pytest.raises(CoordError, match="only an active coordinator"):
        store.coord_claim(worker.launch_id, worker.session_key)
    with pytest.raises(CoordError, match="is not session"):
        store.coord_claim(ln.launch_id, worker.session_key)
    assert store.coord_claim(ln.launch_id, ln.session_key)


def test_your_own_writes_carry_no_token_and_a_coordinator_write_must(store, clock):
    d = drop(store)
    tok = store.coord_take()
    with pytest.raises(CoordError, match="your own writes carry none"):
        store.split(d.drop_id, [Part("work", "x")], by="operator", token=tok)
    with pytest.raises(CoordError, match="carries the lease token"):
        store.split(d.drop_id, [Part("work", "x")], by="coordinator")
    with pytest.raises(CoordError, match="carries the lease token"):
        store.split(d.drop_id, [Part("work", "x")], by="coordinator", token=True)
    assert store.split(d.drop_id, [Part("work", "x")])          # you, by default


def test_the_coordinators_own_launch_holds_no_threads_and_a_worker_holds_some(store, clock):
    boot(store)
    token = store.coord_status()["lease"]["token"]
    with pytest.raises(CoordError, match="holds no threads"):
        store.launch_request(token, harness="claude", role="coordinator", threads=["th-" + "0" * 32])
    with pytest.raises(CoordError, match="at least one thread"):
        store.launch_request(token, harness="claude", role="worker")
    with pytest.raises(CoordError, match="no thread"):
        store.launch_request(token, harness="claude", threads=["th-" + "0" * 32])


def test_lease_and_launch_rows_are_append_only(store, clock, qdir):
    boot(store)
    c = sqlite3.connect(qdir / "dispatch.db")
    try:
        c.execute("INSERT INTO gseq (at) VALUES ('x')")
        active = c.execute("SELECT launch_id FROM launches").fetchone()[0]
        with pytest.raises(sqlite3.DatabaseError, match="pending -> active"):
            c.execute("INSERT INTO launch_events (launch_id, seq, gseq, kind, cause, at) "
                      "VALUES (?, 3, (SELECT MAX(gseq) FROM gseq), 'failed', 'x', 'x')", (active,))
        for sql in ("UPDATE lease_events SET until = 'x'", "DELETE FROM lease_events",
                    "UPDATE launches SET token = 99", "DELETE FROM launch_events",
                    "INSERT OR REPLACE INTO launches SELECT * FROM launches"):
            with pytest.raises(sqlite3.DatabaseError):
                c.execute(sql)
        last = c.execute("SELECT launch_id FROM launches").fetchone()[0]
        with pytest.raises(sqlite3.DatabaseError, match="pending -> active"):
            c.execute("INSERT INTO launch_events (launch_id, seq, gseq, kind, cause, at) "
                      "VALUES (?, 3, 9999, 'active', 'again', 'x')", (last,))
    finally:
        c.close()


def test_levain_coord_cli(qdir, capsys):
    from levain.cli import main

    def run(*argv, rc=0):
        got = main([*argv, "--queue-dir", str(qdir)])
        out = capsys.readouterr()
        assert got == rc, (argv, out.out, out.err)
        return out

    assert "no coordinator store here" in run("coord", "status").out and list(qdir.iterdir()) == []
    did = json.loads(run("drop", "hello", "--json").out)["drop_id"]
    assert "nobody holds" in run("coord", "status").out
    # no coordinator agent set (C3b): the drop only queued, nothing was asked for (test_coordinator_launch drives a
    # drop that launches)
    assert "next drop: unset" in run("coord", "status").out
    assert "carries the lease token" in run("drops", "split", did, "--thread", "work:x", "--by", "coordinator",
                                            rc=1).err
    tok = json.loads(run("coord", "take", "--for", "1h", "--json").out)["token"]
    run("drops", "split", did, "--thread", "work:x", "--by", "coordinator", "--token", str(tok))
    assert f"released token {tok}" in run("coord", "release").out
    assert "--for is a duration" in run("coord", "take", "--for", "soon", rc=1).err


def test_your_take_ends_the_running_coordinator_so_it_never_claims_back(store, clock):
    ln = boot(store)
    store.coord_take(timedelta(minutes=5))
    clock.advance(minutes=6)                                       # your hold runs out
    with pytest.raises(CoordError, match="is a ended coordinator"):
        store.coord_claim(ln.launch_id, ln.session_key)
    assert store.launch(ln.launch_id).cause == "takeover"
    drop(store, "your hold ran out: the next drop asks for a fresh coordinator")
    assert store.coord_status()["wake"] == "launching"


def test_a_holder_whose_launch_ended_does_not_hold_up_the_wakes(store, clock, qdir):
    ln = boot(store)
    token = store.coord_status()["lease"]["token"]
    # an end recorded without its release (a writer that died between the two): the claim still stands
    c = sqlite3.connect(qdir / "dispatch.db")
    c.execute("INSERT INTO gseq (at) VALUES ('x')")
    c.execute("INSERT INTO launch_events (launch_id, seq, gseq, kind, cause, at) VALUES (?, 3, "
              "(SELECT MAX(gseq) FROM gseq), 'ended', 'gone', 'x')", (ln.launch_id,))
    c.commit()
    c.close()
    assert store.coord_status()["lease"]["token"] == token
    assert store.coord_status()["wake"] == "wake"
    d = drop(store, "wakes a new coordinator")
    assert store.coord_status()["lease"]["cause"] == "bootstrap"
    with pytest.raises(CoordError, match="not the live lease's"):
        store.split(d.drop_id, [Part("work", "x")], by="coordinator", token=token)


def test_a_wake_that_fails_never_loses_the_drop(store, clock, monkeypatch):
    def broken(conn, now):
        lease._event(conn, now, "claim", "s-" + "0" * 16, token=None, until=now, cause="half", by="service")
        raise RuntimeError("a wake bug that is not a store error")       # after a partial write
    monkeypatch.setattr(lease, "wake", broken)
    d = drop(store, "keep these words")
    assert store.drop(d.drop_id).words == "keep these words"
    assert store.coord_status()["lease"] is None


def test_a_coordinator_running_without_the_lease_is_waited_for_only_so_long(store, clock):
    ln = boot(store)
    clock.advance(seconds=(lease.LEASE_TTL + timedelta(minutes=1)).total_seconds())
    drop(store, "it may still claim again")
    assert store.coord_status()["wake"] == "idle"
    clock.advance(seconds=lease.IDLE_GRACE.total_seconds())
    drop(store, "it never did: a fresh coordinator")
    st = store.coord_status()
    assert st["wake"] == "launching"
    assert store.launch(ln.launch_id).state == "ended" and store.launch(ln.launch_id).cause == "lost the lease"
    with pytest.raises(CoordError, match="only an active coordinator"):
        store.coord_claim(ln.launch_id, ln.session_key)


def test_your_take_and_release_reset_the_deaths(store, clock):
    drop(store)
    claimed(store)
    clock.advance(seconds=lease.LAUNCH_TTL.total_seconds())
    drop(store, "one death: backoff")
    assert store.coord_status()["wake"] == "backoff"
    store.coord_take()
    store.coord_release()
    drop(store, "you looked: no backoff")
    assert store.coord_status()["wake"] == "launching"


def test_the_backoff_runs_from_the_death_not_from_when_it_was_noticed(store, clock):
    drop(store)
    claimed(store)
    clock.advance(seconds=(lease.LAUNCH_TTL + lease.BACKOFF[0] + timedelta(seconds=1)).total_seconds())
    drop(store, "the expiry is noticed only now, past its backoff already")
    assert store.coord_status()["wake"] == "launching"


def test_no_caller_writes_a_cause_the_store_keeps_for_itself(store, clock):
    drop(store)
    ln = store.coord_status()["launches"][0]
    with pytest.raises(CoordError, match="the store writes itself"):
        store.launch_end(ln["launch_id"], "failed", "takeover")
    with pytest.raises(CoordError, match="takes no token"):
        store.coord_release(5)


def test_a_late_look_never_restarts_the_idle_wait(store, clock):
    ln = boot(store)
    clock.advance(hours=10)                                        # crashed; nobody looked for ten hours
    drop(store, "the first look is long past the grace")
    assert store.coord_status()["wake"] == "launching"
    assert store.launch(ln.launch_id).cause == "lost the lease"


def test_a_beat_under_a_clock_stepped_back_never_shortens_the_lease(store, clock):
    boot(store)
    token = store.coord_status()["lease"]["token"]
    clock.advance(minutes=20)
    far = store.coord_beat(token).until
    clock.advance(minutes=-15)                                     # the clock is corrected backward
    store.coord_beat(token)
    assert store.coord_status()["lease"]["until"] == far


def test_deaths_count_in_the_order_launches_ended(store, clock, qdir):
    ln = boot(store)                                               # A: active, never writes as coordinator
    clock.advance(seconds=(lease.LEASE_TTL + lease.IDLE_GRACE + lease.BACKOFF[0]).total_seconds())
    drop(store, "A died idle (one death, its backoff past); B is asked for")
    assert store.launch(ln.launch_id).cause == "lost the lease"
    st = store.coord_status()
    assert st["wake"] == "launching"
    claimed(store)
    clock.advance(seconds=lease.LAUNCH_TTL.total_seconds())
    drop(store, "B expired: two deaths in a row")
    assert store.coord_status()["wake"] == "backoff" and "2 coordinator" in store.coord_status()["why"]


def test_a_vacant_lease_whose_holder_died_is_claimable(store, clock, qdir):
    first = boot(store)
    token = store.coord_status()["lease"]["token"]
    rot = store.launch_request(token, harness="claude", role="coordinator")
    store.launch_activate(rot.launch_id, harness_session_id="probe-B")
    c = sqlite3.connect(qdir / "dispatch.db")
    c.execute("INSERT INTO gseq (at) VALUES ('x')")
    c.execute("INSERT INTO launch_events (launch_id, seq, gseq, kind, cause, at) VALUES (?, 3, "
              "(SELECT MAX(gseq) FROM gseq), 'ended', 'gone', 'x')", (first.launch_id,))
    c.commit()
    c.close()
    assert store.coord_claim(rot.launch_id, rot.session_key) != token


def test_a_take_of_zero_is_refused(store, clock):
    with pytest.raises(CoordError, match="1 minute to 7 days"):
        store.coord_take(timedelta(0))

"""levain.cockpit.derive: dueness rules, fingerprints, the threaded loop (fake writer, fake clock, tmp files)."""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import threading
import time

import pytest

from levain.cockpit import derive, snapshots
from levain.cockpit.derive import (
    DeriveLoop,
    DeriveResult,
    Dir,
    File,
    GitRefs,
    InputsView,
    PanelPlan,
    PanelState,
    Poll,
    Sqlite,
    due_panels,
    fingerprint,
)

WAIT = 5.0


class FakeClock:
    def __init__(self, t: float = 1000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


class FakeWriter:
    """Records what the loop writes; implements the S1 writer methods the loop calls."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.publishes: list[dict] = []
        self.heartbeats: list[float] = []
        self.streams: list[dict] = []
        self.published = threading.Condition(self.lock)

    def publish(self, panel_id, *, status, body, started_at, derived_at, duration_ms, valid_until=None,
                inputs_fp="", write_gen=0, error=None):
        with self.published:
            self.publishes.append(dict(id=panel_id, status=status, body=body, started_at=started_at,
                                       derived_at=derived_at, duration_ms=duration_ms, valid_until=valid_until,
                                       inputs_fp=inputs_fp, write_gen=write_gen, error=error))
            self.published.notify_all()
            return len(self.publishes)

    def heartbeat(self, now):
        with self.lock:
            self.heartbeats.append(now)

    def set_stream(self, stream_id, *, state, last_event_at, detail=""):
        with self.lock:
            self.streams.append(dict(id=stream_id, state=state, last_event_at=last_event_at, detail=detail))

    def of(self, pid):
        with self.lock:
            return [p for p in self.publishes if p["id"] == pid]

    def wait_for(self, pred, timeout=WAIT):
        with self.published:
            return self.published.wait_for(lambda: pred(self.publishes), timeout)


def ok(body="x", **kw):
    return DeriveResult(status="ok", body=body, **kw)


def make(plans, fn=None, clock=None, **kw):
    writer = FakeWriter()
    clock = clock or FakeClock()
    calls: list[str] = []

    def default(pid):
        calls.append(pid)
        return ok({"id": pid, "n": len(calls)})

    loop = DeriveLoop(writer, {p.id: p for p in plans}, fn or default, clock=clock, **kw)
    return loop, writer, clock, calls


def settle(loop, clock):
    """First derive of everything, so later steps start from a quiet loop."""
    loop.run_once(clock.t)


# ------------------------------------------------------------------------------------------ fingerprints


def test_file_fingerprint_absent_then_moves(tmp_path):
    p = tmp_path / "f.txt"
    assert fingerprint(File(str(p))) == "absent"
    p.write_text("a")
    a = fingerprint(File(str(p)))
    assert a not in (None, "absent")
    assert fingerprint(File(str(p))) == a
    p.write_text("longer")
    assert fingerprint(File(str(p))) != a


def test_poll_fingerprint_is_none():
    assert fingerprint(Poll(5.0)) is None


def test_dir_fingerprint_moves_on_add_and_touch(tmp_path):
    d = tmp_path / "d"
    d.mkdir()
    (d / "a").write_text("1")
    a = fingerprint(Dir(str(d)))
    assert fingerprint(Dir(str(d))) == a
    (d / "b").write_text("2")
    b = fingerprint(Dir(str(d)))
    assert b != a
    os.utime(d / "a", ns=(1, 1))
    assert fingerprint(Dir(str(d))) != b
    assert fingerprint(Dir(str(tmp_path / "nope"))) == "absent"


def test_dir_past_the_bound_answers_none(tmp_path, monkeypatch):
    monkeypatch.setattr(derive, "DIR_ENTRIES_MAX", 2)
    d = tmp_path / "d"
    d.mkdir()
    for i in range(3):
        (d / f"f{i}").write_text("x")
    assert fingerprint(Dir(str(d))) is None


def test_sqlite_fingerprint_follows_data_version(tmp_path):
    path = str(tmp_path / "s.db")
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE t(x)")
    conn.commit()
    assert fingerprint(Sqlite(str(tmp_path / "missing.db"))) == "absent"
    fp = derive._Fingerprinter()
    try:
        a = fp.fingerprint(Sqlite(path))
        assert a not in (None, "absent")
        assert fp.fingerprint(Sqlite(path)) == a
        conn.execute("INSERT INTO t VALUES (1)")
        conn.commit()
        assert fp.fingerprint(Sqlite(path)) != a
    finally:
        fp.close()
        conn.close()


def git(repo, *args):
    env = dict(os.environ, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_SYSTEM=os.devnull)
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", "-c",
                    "commit.gpgsign=false", *args], check=True, capture_output=True, env=env)


@pytest.fixture
def repo(tmp_path):
    if shutil.which("git") is None:
        pytest.skip("git not installed")
    r = tmp_path / "repo"
    r.mkdir()
    git(r, "init", "-q")
    git(r, "commit", "-q", "--allow-empty", "-m", "one")
    return r


def test_gitrefs_fingerprint_moves_on_commit_and_branch(repo):
    a = fingerprint(GitRefs(str(repo)))
    assert a not in (None, "absent", "locked")
    assert fingerprint(GitRefs(str(repo))) == a
    git(repo, "commit", "-q", "--allow-empty", "-m", "two")
    b = fingerprint(GitRefs(str(repo)))
    assert b != a
    git(repo, "branch", "side")
    assert fingerprint(GitRefs(str(repo))) != b


def test_gitrefs_locked_and_not_a_repo(repo, tmp_path):
    lock = repo / ".git" / "index.lock"
    lock.write_text("")
    assert fingerprint(GitRefs(str(repo))) == "locked"
    lock.unlink()
    assert fingerprint(GitRefs(str(repo))) != "locked"
    assert fingerprint(GitRefs(str(tmp_path / "plain"))) == "absent"


def test_gitrefs_resolves_a_linked_worktree_gitfile(repo, tmp_path):
    wt = tmp_path / "wt"
    git(repo, "worktree", "add", "-q", str(wt), "-b", "wtbranch")
    assert (wt / ".git").is_file()
    a = fingerprint(GitRefs(str(wt)))
    assert a not in (None, "absent")
    git(wt, "commit", "-q", "--allow-empty", "-m", "on wt")
    assert fingerprint(GitRefs(str(wt))) != a


# ------------------------------------------------------------------------------------------ the pure rules


def view(fp="", unknown=False, locked=False):
    return InputsView(fp, unknown, locked, ("p: x",) if unknown else ())


def test_due_panels_orders_dirty_first_then_oldest_derived():
    plans = {n: PanelPlan(n) for n in "abcd"}
    states = {
        "a": PanelState(derived_at=50.0, started_at=50.0),
        "b": PanelState(derived_at=10.0, started_at=10.0),
        "c": PanelState(derived_at=90.0, started_at=90.0, dirty_gen=1),
        "d": PanelState(),
    }
    due = due_panels(plans, states, {}, None, now=100.0, backstop_s=30.0)
    assert [p for p, _ in due] == ["c", "d", "b", "a"]
    assert dict(due) == {"a": "backstop", "b": "backstop", "c": "dirty", "d": "first"}


def test_due_reason_busy_panel_is_not_due():
    plan = PanelPlan("a")
    assert derive.due_reason(plan, PanelState(busy=True), view(), None, 1.0, 30.0) is None


def test_poll_input_is_due_at_its_period_and_not_degraded():
    plan = PanelPlan("a", inputs=(Poll(10.0),))
    st = PanelState(derived_at=100.0, started_at=100.0, inputs_fp="")
    assert derive.due_reason(plan, st, view(), None, 105.0, 999.0) is None
    assert derive.due_reason(plan, st, view(), None, 110.0, 999.0) == "every_s"


# ------------------------------------------------------------------------------------------ each due rule


def test_first_derive_publishes_and_nothing_moves_means_nothing_derives(tmp_path):
    f = tmp_path / "f"
    f.write_text("1")
    loop, writer, clock, calls = make([PanelPlan("a", inputs=(File(str(f)),))])
    assert loop.run_once(clock.t) == ["a"]
    pub = writer.of("a")[0]
    assert pub["status"] == "ok" and json.loads(pub["body"]) == {"id": "a", "n": 1}
    assert pub["inputs_fp"] != "" and pub["write_gen"] == 0
    clock.t += 5
    assert loop.run_once(clock.t) == []
    assert calls == ["a"]
    loop.stop()


def test_body_is_canonical_json_bytes():
    loop, writer, clock, _ = make([PanelPlan("a")], fn=lambda pid: ok({"b": 1, "a": "é"}))
    loop.run_once(clock.t)
    assert writer.of("a")[0]["body"] == snapshots.encode_body({"b": 1, "a": "é"}) == b'{"a":"\\u00e9","b":1}'


def test_file_input_move_derives(tmp_path):
    f = tmp_path / "f"
    f.write_text("1")
    loop, writer, clock, calls = make([PanelPlan("a", inputs=(File(str(f)),))])
    settle(loop, clock)
    f.write_text("22")
    clock.t += 1
    assert loop.run_once(clock.t) == ["a"]
    assert loop.run_once(clock.t) == []
    loop.stop()


def test_dir_input_move_derives(tmp_path):
    d = tmp_path / "d"
    d.mkdir()
    loop, writer, clock, calls = make([PanelPlan("a", inputs=(Dir(str(d)),))])
    settle(loop, clock)
    (d / "new").write_text("x")
    clock.t += 1
    assert loop.run_once(clock.t) == ["a"]
    loop.stop()


def test_sqlite_input_move_derives(tmp_path):
    path = str(tmp_path / "s.db")
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE t(x)")
    conn.commit()
    loop, writer, clock, calls = make([PanelPlan("a", inputs=(Sqlite(path),))])
    try:
        settle(loop, clock)
        clock.t += 1
        assert loop.run_once(clock.t) == []
        conn.execute("INSERT INTO t VALUES (1)")
        conn.commit()
        clock.t += 1
        assert loop.run_once(clock.t) == ["a"]
    finally:
        loop.stop()
        conn.close()


def test_gitrefs_input_move_derives(repo):
    loop, writer, clock, calls = make([PanelPlan("a", inputs=(GitRefs(str(repo)),))])
    settle(loop, clock)
    clock.t += 1
    assert loop.run_once(clock.t) == []
    git(repo, "commit", "-q", "--allow-empty", "-m", "two")
    clock.t += 1
    assert loop.run_once(clock.t) == ["a"]
    loop.stop()


def test_index_lock_holds_a_gitrefs_panel_until_it_goes(repo):
    lock = repo / ".git" / "index.lock"
    lock.write_text("")
    loop, writer, clock, calls = make([PanelPlan("a", inputs=(GitRefs(str(repo)),)), PanelPlan("b")])
    assert loop.run_once(clock.t) == ["b"]
    clock.t += 100  # even the backstop does not derive a locked repo (b has no lock, so b does)
    assert loop.run_once(clock.t) == ["b"]
    assert writer.of("a") == []
    lock.unlink()
    assert loop.run_once(clock.t) == ["a"]
    loop.stop()


def test_every_s_timer():
    loop, writer, clock, calls = make([PanelPlan("a", every_s=10.0)], backstop_s=1e9)
    settle(loop, clock)
    clock.t += 9
    assert loop.run_once(clock.t) == []
    clock.t += 1
    assert loop.run_once(clock.t) == ["a"]
    loop.stop()


def test_poll_input_derives_at_its_period():
    loop, writer, clock, calls = make([PanelPlan("a", inputs=(Poll(7.0),))], backstop_s=1e9)
    settle(loop, clock)
    clock.t += 6
    assert loop.run_once(clock.t) == []
    clock.t += 1
    assert loop.run_once(clock.t) == ["a"]
    loop.stop()


def test_valid_until_passing_derives():
    results = iter([ok(valid_until=1050.0), ok(valid_until=None)])
    loop, writer, clock, calls = make([PanelPlan("a")], fn=lambda pid: next(results), backstop_s=1e9)
    settle(loop, clock)
    assert writer.of("a")[0]["valid_until"] == 1050.0
    clock.t = 1049.0
    assert loop.run_once(clock.t) == []
    clock.t = 1050.0
    assert loop.run_once(clock.t) == ["a"]
    clock.t = 1e6
    assert loop.run_once(clock.t) == []
    loop.stop()


def test_valid_until_already_past_does_not_hot_loop():
    loop, writer, clock, calls = make([PanelPlan("a")], fn=lambda pid: ok(valid_until=clock.t - 5), backstop_s=1e9)
    settle(loop, clock)
    clock.t += 1
    assert loop.run_once(clock.t) == []
    loop.stop()


def test_on_token_moves_derive_and_plain_panels_ignore_it():
    token = {"v": 1}
    loop, writer, clock, calls = make(
        [PanelPlan("a", on_token=True), PanelPlan("b")], token_fn=lambda: token["v"], backstop_s=1e9
    )
    settle(loop, clock)
    clock.t += 1
    assert loop.run_once(clock.t) == []
    token["v"] = 2
    assert loop.run_once(clock.t) == ["a"]
    assert loop.run_once(clock.t) == []
    loop.stop()


def test_dirty_derives_first_and_stamps_write_gen():
    loop, writer, clock, calls = make([PanelPlan("a"), PanelPlan("b")], backstop_s=1e9)
    settle(loop, clock)
    loop.dirty(["b"], 4)
    assert loop.run_once(clock.t) == ["b"]
    assert writer.of("b")[-1]["write_gen"] == 4
    assert loop.run_once(clock.t) == []
    loop.dirty(["nope"], 9)  # an unknown panel is ignored
    assert loop.run_once(clock.t) == []
    loop.stop()


def test_dirty_write_gen_is_the_max_seen_before_start_and_a_late_dirty_derives_again():
    seen = []
    armed = []

    def fn(pid):
        if armed and not seen:
            seen.append(1)
            loop.dirty(["a"], 9)  # arrives while this derive runs: belongs to the next one
        return ok()

    loop, writer, clock, calls = make([PanelPlan("a")], fn=fn, backstop_s=1e9)
    settle(loop, clock)
    armed.append(1)
    loop.dirty(["a"], 3)
    loop.dirty(["a"], 7)
    loop.dirty(["a"], 5)
    assert loop.run_once(clock.t) == ["a"]
    assert writer.of("a")[-1]["write_gen"] == 7
    assert loop.run_once(clock.t) == ["a"]
    assert writer.of("a")[-1]["write_gen"] == 9
    assert loop.run_once(clock.t) == []
    loop.stop()


def test_backstop_derives_every_panel_when_nothing_else_is_due():
    loop, writer, clock, calls = make([PanelPlan("a"), PanelPlan("b")], backstop_s=30.0)
    settle(loop, clock)
    clock.t += 29
    assert loop.run_once(clock.t) == []
    clock.t += 1
    assert sorted(loop.run_once(clock.t)) == ["a", "b"]
    loop.stop()


# ------------------------------------------------------------------------------------------ errors, degraded


def test_exception_publishes_error_keeps_last_body_and_the_loop_continues():
    state = {"boom": False}

    def fn(pid):
        if pid == "a" and state["boom"]:
            raise ValueError("bad thing")
        return ok({"pid": pid})

    loop, writer, clock, calls = make([PanelPlan("a"), PanelPlan("b")], fn=fn, backstop_s=1e9)
    settle(loop, clock)
    state["boom"] = True
    loop.dirty(["a", "b"], 1)
    assert sorted(loop.run_once(clock.t)) == ["a", "b"]
    err = writer.of("a")[-1]
    assert err["status"] == "error" and err["error"] == "ValueError: bad thing" and err["body"] is None
    assert writer.of("b")[-1]["status"] == "ok"
    state["boom"] = False
    loop.dirty(["a"], 2)
    assert loop.run_once(clock.t) == ["a"]
    assert writer.of("a")[-1]["status"] == "ok"
    loop.stop()


def test_error_before_any_good_body_asks_the_writer_to_keep_the_stored_body():
    def fn(pid):
        raise RuntimeError("nope")

    loop, writer, clock, _ = make([PanelPlan("a")], fn=fn)
    loop.run_once(clock.t)
    pub = writer.of("a")[0]
    assert pub["status"] == "error" and pub["body"] is None
    loop.stop()


def test_nan_body_is_an_error_publish_naming_the_value_problem():
    loop, writer, clock, _ = make([PanelPlan("a")], fn=lambda pid: ok({"x": float("nan")}))
    loop.run_once(clock.t)
    pub = writer.of("a")[0]
    assert pub["status"] == "error" and pub["error"].startswith("ValueError") and "JSON" in pub["error"]
    loop.stop()


def test_lone_surrogate_body_publishes_ok():
    loop, writer, clock, _ = make([PanelPlan("a")], fn=lambda pid: ok({"n": "x\ud83dy"}))
    loop.run_once(clock.t)
    assert writer.of("a")[0]["status"] == "ok"
    loop.stop()


def test_unencodable_body_is_an_error_publish():
    loop, writer, clock, _ = make([PanelPlan("a")], fn=lambda pid: ok({"x": object()}))
    loop.run_once(clock.t)
    pub = writer.of("a")[0]
    assert pub["status"] == "error" and pub["error"].startswith("TypeError")
    loop.stop()


def test_a_failing_writer_does_not_kill_the_loop():
    loop, writer, clock, _ = make([PanelPlan("a")])

    def boom(*a, **k):
        raise OSError("disk")

    writer.publish = boom
    assert loop.run_once(clock.t) == ["a"]
    assert isinstance(loop.last_publish_error, OSError)
    loop.stop()


def test_dir_past_the_bound_degrades_the_stream_naming_panel_and_input(tmp_path, monkeypatch):
    monkeypatch.setattr(derive, "DIR_ENTRIES_MAX", 2)
    d = tmp_path / "big"
    d.mkdir()
    for i in range(3):
        (d / f"f{i}").write_text("x")
    loop, writer, clock, calls = make([PanelPlan("a", inputs=(Dir(str(d)),)), PanelPlan("b")], backstop_s=1e9)
    assert sorted(loop.run_once(clock.t)) == ["a", "b"]
    stream = writer.streams[-1]
    assert stream["id"] == "inputs" and stream["state"] == "degraded"
    assert "a" in stream["detail"] and "Dir" in stream["detail"] and str(d) in stream["detail"]
    # None means cannot tell, but never "changed": no back-to-back derives
    assert loop.run_once(clock.t) == []
    for i in range(3):
        (d / f"f{i}").unlink()
    assert loop.run_once(clock.t) == ["a"]  # the fingerprint is real again: one derive from the changed state
    assert loop.run_once(clock.t) == []
    assert writer.streams[-1]["state"] == "ok"
    loop.stop()


def test_none_fingerprint_derives_at_most_once_per_backstop(tmp_path, monkeypatch):
    monkeypatch.setattr(derive, "DIR_ENTRIES_MAX", 2)
    d = tmp_path / "big"
    d.mkdir()
    for i in range(3):
        (d / f"f{i}").write_text("x")
    loop, writer, clock, calls = make([PanelPlan("a", inputs=(Dir(str(d)),)), PanelPlan("b", inputs=(Dir(str(d)),), backstop_s=50.0)], backstop_s=30.0)
    for _ in range(100):
        loop.run_once(clock.t)
        clock.t += 1
    # first derive, then one per backstop: 30 s for a (t=30, 60, 90), 50 s for b (t=50)
    assert len(writer.of("a")) == 1 + 3 and len(writer.of("b")) == 1 + 1
    loop.stop()


def test_none_fingerprint_does_not_rewake_the_loop_into_back_to_back_derives(tmp_path, monkeypatch):
    monkeypatch.setattr(derive, "DIR_ENTRIES_MAX", 2)
    d = tmp_path / "big"
    d.mkdir()
    for i in range(3):
        (d / f"f{i}").write_text("x")
    loop, writer, clock, calls = make([PanelPlan("a", inputs=(Dir(str(d)),))], poll_s=0.01, backstop_s=1e9)
    loop.start()
    try:
        assert writer.wait_for(lambda pubs: len(pubs) == 1)
        deadline = time.monotonic() + WAIT
        while len(writer.heartbeats) < 8 and time.monotonic() < deadline:
            time.sleep(0.005)
        assert len(writer.heartbeats) >= 8
    finally:
        loop.stop()
    assert len(writer.of("a")) == 1


def test_heartbeat_and_ok_stream_each_poll(tmp_path):
    f = tmp_path / "f"
    f.write_text("1")
    loop, writer, clock, _ = make([PanelPlan("a", inputs=(File(str(f)),))], backstop_s=1e9)
    for _ in range(3):
        loop.run_once(clock.t)
        clock.t += 1
    assert writer.heartbeats == [1000.0, 1001.0, 1002.0]
    assert [s["state"] for s in writer.streams] == ["ok"] * 3
    assert all(s["id"] == "inputs" for s in writer.streams)
    loop.stop()


def test_stream_last_event_moves_with_an_input(tmp_path):
    f = tmp_path / "f"
    f.write_text("1")
    loop, writer, clock, _ = make([PanelPlan("a", inputs=(File(str(f)),))], backstop_s=1e9)
    loop.run_once(clock.t)
    clock.t += 10
    loop.run_once(clock.t)
    assert writer.streams[-1]["last_event_at"] is None  # nothing has moved yet
    f.write_text("changed")
    clock.t += 10
    loop.run_once(clock.t)
    assert writer.streams[-1]["last_event_at"] == 1020.0
    loop.stop()


# ------------------------------------------------------------------------------------------ the threads


def derive_threads():
    return [t for t in threading.enumerate() if t.name.startswith("derive") and t.is_alive()]


def test_start_stop_leaves_no_thread_alive_and_is_single_use():
    loop, writer, clock, _ = make([PanelPlan("a")], poll_s=0.01, threads=2)
    before = set(derive_threads())
    loop.start()
    with pytest.raises(RuntimeError):
        loop.start()  # a loop starts once
    assert writer.wait_for(lambda pubs: len(pubs) >= 1)
    assert len(set(derive_threads()) - before) == 3
    loop.stop()
    assert set(derive_threads()) - before == set()
    with pytest.raises(RuntimeError):
        loop.start()  # nor after stop(): a restart is a new DeriveLoop
    assert set(derive_threads()) - before == set()


def test_a_slow_panel_does_not_block_the_others():
    gate, entered = threading.Event(), threading.Event()

    def fn(pid):
        if pid == "slow":
            entered.set()
            assert gate.wait(WAIT)
        return ok(pid)

    loop, writer, clock, _ = make(
        [PanelPlan("slow"), PanelPlan("fast1"), PanelPlan("fast2")], fn=fn, poll_s=0.01, threads=2
    )
    loop.start()
    try:
        assert entered.wait(WAIT)
        assert writer.wait_for(lambda pubs: {"fast1", "fast2"} <= {p["id"] for p in pubs})
        assert writer.of("slow") == []
    finally:
        gate.set()
        loop.stop()
    assert len(writer.of("slow")) == 1


def test_one_derive_per_panel_at_a_time_under_concurrent_dirties():
    lock = threading.Lock()
    active = {"a": 0, "b": 0}
    peak = {"a": 0, "b": 0}
    counts = {"a": 0, "b": 0}
    first_in, release = threading.Event(), threading.Event()

    def fn(pid):
        with lock:
            active[pid] += 1
            counts[pid] += 1
            peak[pid] = max(peak[pid], active[pid])
            n = counts[pid]
        if pid == "a" and n == 1:
            first_in.set()
            assert release.wait(WAIT)
        with lock:
            active[pid] -= 1
        return ok(pid)

    loop, writer, clock, _ = make([PanelPlan("a"), PanelPlan("b")], fn=fn, poll_s=0.01, threads=3, backstop_s=1e9)
    loop.start()
    try:
        assert first_in.wait(WAIT)
        hammers = [threading.Thread(target=lambda g=g: [loop.dirty(["a", "b"], g * 10 + k) for k in range(20)])
                   for g in range(4)]
        for t in hammers:
            t.start()
        for t in hammers:
            t.join()
        assert writer.wait_for(lambda pubs: len([p for p in pubs if p["id"] == "b"]) >= 2)
        release.set()
        assert writer.wait_for(lambda pubs: len([p for p in pubs if p["id"] == "a"]) >= 2)
    finally:
        release.set()
        loop.stop()
    assert peak == {"a": 1, "b": 1}
    # the dirties that landed during a's first derive were served by exactly one more derive, with the max gen
    assert [p["write_gen"] for p in writer.of("a")][:2] == [0, 3 * 10 + 19]


def test_loop_threads_publish_and_react_to_dirty():
    loop, writer, clock, calls = make([PanelPlan("a")], poll_s=30.0, backstop_s=1e9)
    loop.start()
    try:
        assert writer.wait_for(lambda pubs: len(pubs) == 1)
        loop.dirty(["a"], 5)  # wakes the poll long before poll_s
        assert writer.wait_for(lambda pubs: len(pubs) == 2)
    finally:
        loop.stop()
    assert writer.of("a")[-1]["write_gen"] == 5
    assert writer.heartbeats


# ------------------------------------------------------------------------------------------ L1/L2 round


def test_per_plan_backstop():
    loop, writer, clock, calls = make([PanelPlan("fast"), PanelPlan("heavy", heavy=True, backstop_s=300.0)], backstop_s=30.0)
    settle(loop, clock)
    clock.t += 30
    assert loop.run_once(clock.t) == ["fast"]
    clock.t = 1000.0 + 299
    assert "heavy" not in loop.run_once(clock.t)
    clock.t = 1000.0 + 300
    assert "heavy" in loop.run_once(clock.t)
    loop.stop()


def test_input_moving_during_its_own_derive_derives_once_more(tmp_path):
    f = tmp_path / "f"
    f.write_text("1")
    runs = []

    def fn(pid):
        runs.append(1)
        if len(runs) == 1:
            f.write_text("moved while deriving")
        return ok(len(runs))

    loop, writer, clock, _ = make([PanelPlan("a", inputs=(File(str(f)),))], fn=fn, backstop_s=1e9)
    assert loop.run_once(clock.t) == ["a"]
    assert loop.run_once(clock.t) == ["a"]
    assert loop.run_once(clock.t) == []
    assert len(runs) == 2
    loop.stop()


def test_index_lock_in_a_linked_worktree_holds_the_panel(repo, tmp_path):
    wt = tmp_path / "wt"
    git(repo, "worktree", "add", "-q", str(wt), "-b", "wtbranch")
    gitdir = (wt / ".git").read_text().split("gitdir:", 1)[1].strip()
    lock = os.path.join(gitdir, "index.lock")
    open(lock, "w").close()
    assert fingerprint(GitRefs(str(wt))) == "locked"
    loop, writer, clock, calls = make([PanelPlan("a", inputs=(GitRefs(str(wt)),)), PanelPlan("b")])
    assert loop.run_once(clock.t) == ["b"]
    os.unlink(lock)
    assert loop.run_once(clock.t) == ["a"]
    loop.stop()


def test_a_stale_index_lock_stops_holding_the_panel_and_degrades_the_stream(repo):
    lock = repo / ".git" / "index.lock"
    lock.write_text("")
    old = time.time() - derive.LOCK_HOLD_MAX_S - 10
    os.utime(lock, (old, old))
    assert fingerprint(GitRefs(str(repo))).startswith(derive.STALE_LOCK + ":")
    loop, writer, clock, calls = make([PanelPlan("a", inputs=(GitRefs(str(repo)),))], backstop_s=1e9)
    assert loop.run_once(clock.t) == ["a"]
    stream = writer.streams[-1]
    assert stream["state"] == "degraded" and "stale index.lock" in stream["detail"] and str(lock) in stream["detail"]
    assert loop.run_once(clock.t) == []  # the lock is still there but the panel is not re-derived for it
    git(repo, "update-ref", "refs/heads/side", "HEAD")  # a ref move: git commit itself would refuse under the lock
    assert loop.run_once(clock.t) == ["a"]
    loop.stop()


def test_a_young_lock_past_the_hold_limit_in_the_fake_future_is_stale(repo, monkeypatch):
    lock = repo / ".git" / "index.lock"
    lock.write_text("")
    assert fingerprint(GitRefs(str(repo))) == "locked"
    monkeypatch.setattr(derive, "LOCK_HOLD_MAX_S", 0.0)
    assert fingerprint(GitRefs(str(repo))).startswith(derive.STALE_LOCK)


def test_a_hung_derive_publishes_timeout_once_keeps_busy_and_is_never_started_twice():
    gate, entered = threading.Event(), threading.Event()
    runs = []

    def fn(pid):
        runs.append(1)
        entered.set()
        assert gate.wait(WAIT * 4)
        return ok("late")

    loop, writer, clock, _ = make([PanelPlan("a", timeout_s=50.0)], fn=fn, poll_s=0.01, backstop_s=1.0)
    loop.start()
    try:
        assert entered.wait(WAIT)
        clock.t += 49
        time.sleep(0.05)
        assert writer.of("a") == []
        clock.t += 2  # now past the deadline (also past the backstop, which must not start a second derive)
        assert writer.wait_for(lambda pubs: any(p["status"] == "timeout" for p in pubs))
        pub = writer.of("a")[0]
        assert pub["status"] == "timeout" and pub["body"] is None and "still running" in pub["error"]
        deadline = time.monotonic() + WAIT
        while time.monotonic() < deadline and not (writer.streams and writer.streams[-1]["state"] == "degraded"):
            time.sleep(0.005)
        assert writer.streams[-1]["state"] == "degraded" and "a: derive timeout" in writer.streams[-1]["detail"]
        time.sleep(0.1)
        assert len(runs) == 1 and len(writer.of("a")) == 1  # one timeout publish, no second derive
        gate.set()
        assert writer.wait_for(lambda pubs: any(p["status"] == "ok" for p in pubs))  # the late result still lands
    finally:
        gate.set()
        loop.stop()
    assert loop._states["a"].busy is False


def test_a_base_exception_in_derive_does_not_leave_the_panel_busy():
    def fn(pid):
        raise SystemExit("bye")

    loop, writer, clock, _ = make([PanelPlan("a")], fn=fn)
    with pytest.raises(SystemExit):
        loop.run_once(clock.t)
    assert loop._states["a"].busy is False
    pub = writer.of("a")[0]
    assert pub["status"] == "error" and pub["error"].startswith("SystemExit")
    # in a worker thread the thread survives and the panel is derived again
    loop.stop()


def test_a_worker_thread_survives_a_system_exit_in_derive():
    calls = []

    def fn(pid):
        calls.append(1)
        if len(calls) == 1:
            raise SystemExit("bye")
        return ok("fine")

    loop, writer, clock, _ = make([PanelPlan("a")], fn=fn, poll_s=0.01, threads=1, backstop_s=1e9)
    loop.start()
    try:
        assert writer.wait_for(lambda pubs: len(pubs) >= 1)
        loop.dirty(["a"], 1)
        assert writer.wait_for(lambda pubs: any(p["status"] == "ok" for p in pubs))
    finally:
        loop.stop()


def test_past_valid_until_is_published_as_no_expiry():
    loop, writer, clock, _ = make([PanelPlan("a")], fn=lambda pid: ok(valid_until=clock.t - 5), backstop_s=1e9)
    settle(loop, clock)
    assert writer.of("a")[0]["valid_until"] is None
    loop.stop()


def test_a_failed_publish_keeps_the_dirty_write_owed_and_degrades_the_stream():
    loop, writer, clock, _ = make([PanelPlan("a")], backstop_s=1e9)
    settle(loop, clock)
    real = writer.publish

    def boom(*a, **k):
        raise OSError("disk")

    writer.publish = boom
    loop.dirty(["a"], 6)
    assert loop.run_once(clock.t) == ["a"]
    loop.run_once(clock.t)
    assert "OSError" in writer.streams[-1]["detail"] and writer.streams[-1]["state"] == "degraded"
    writer.publish = real
    assert loop.run_once(clock.t) == ["a"]  # the owed write derives again, still stamped
    assert writer.of("a")[-1]["write_gen"] == 6
    loop.run_once(clock.t)
    assert writer.streams[-1]["state"] == "ok"
    loop.stop()


def test_an_exception_in_a_poll_does_not_end_the_poll_thread(monkeypatch):
    loop, writer, clock, _ = make([PanelPlan("a")], poll_s=0.01)
    real = loop._views
    n = []

    def flaky():
        n.append(1)
        if len(n) == 1:
            raise RuntimeError("bad input")
        return real()

    monkeypatch.setattr(loop, "_views", flaky)
    loop.start()
    try:
        assert writer.wait_for(lambda pubs: len(pubs) >= 1)
    finally:
        loop.stop()


def test_token_unset_at_first_derive_then_a_value_is_a_move():
    box = {"v": None}

    def token_fn():
        if box["v"] is None:
            raise RuntimeError("no token yet")
        return box["v"]

    loop, writer, clock, calls = make([PanelPlan("a", on_token=True)], token_fn=token_fn, backstop_s=1e9)
    settle(loop, clock)
    box["v"] = 1
    assert loop.run_once(clock.t) == ["a"]
    assert loop.run_once(clock.t) == []
    loop.stop()


def test_a_publish_racing_stop_lands_before_stop_returns(tmp_path):
    w = snapshots.SnapshotWriter(tmp_path / snapshots.DB_FILENAME)
    entered, gate = threading.Event(), threading.Event()

    def fn(pid):
        entered.set()
        assert gate.wait(WAIT * 4)
        return ok({"late": True})

    loop = DeriveLoop(w, {"a": PanelPlan("a")}, fn, poll_s=0.01, threads=1)
    loop.start()
    assert entered.wait(WAIT)
    stopper = threading.Thread(target=loop.stop)
    stopper.start()
    time.sleep(0.05)
    assert stopper.is_alive()  # stop() waits for the running derive
    gate.set()
    stopper.join(WAIT * 2)
    assert not stopper.is_alive()
    w.close()  # the owner closes after stop(): nothing is publishing any more
    snap = snapshots.SnapshotReader(tmp_path / snapshots.DB_FILENAME).read("a")
    assert snap is not None and snap.status == "ok" and loop.last_publish_error is None


def test_unknown_input_detail_carries_the_failure_type(tmp_path, monkeypatch):
    d = tmp_path / "d"
    d.mkdir()
    real = os.scandir

    def deny(path):
        raise PermissionError("no")

    monkeypatch.setattr(derive.os, "scandir", deny)
    loop, writer, clock, _ = make([PanelPlan("a", inputs=(Dir(str(d)),))])
    loop.run_once(clock.t)
    assert "PermissionError" in writer.streams[-1]["detail"]
    monkeypatch.setattr(derive.os, "scandir", real)
    loop.stop()


def test_sqlite_fingerprint_reopens_when_the_db_file_is_replaced(tmp_path):
    path = tmp_path / "s.db"

    def make_db(v):
        c = sqlite3.connect(path)
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("CREATE TABLE t(x)")
        c.execute("INSERT INTO t VALUES (?)", (v,))
        c.commit()
        c.close()

    make_db(1)
    fp = derive._Fingerprinter()
    try:
        a = fp.fingerprint(Sqlite(str(path)))
        held = fp._conns[str(path)][0]
        for suffix in ("", "-wal", "-shm"):
            if os.path.exists(str(path) + suffix):
                os.unlink(str(path) + suffix)
        make_db(2)
        b = fp.fingerprint(Sqlite(str(path)))
        assert b != a and fp._conns[str(path)][0] is not held
    finally:
        fp.close()


# ------------------------------------------------------------------------------------------ L3 round 1


def _fail_publish_once(writer):
    real, left = writer.publish, [1]

    def flaky(*a, **k):
        if left[0]:
            left[0] -= 1
            raise OSError("disk full")
        return real(*a, **k)

    writer.publish = flaky


def test_a_failed_publish_leaves_the_panel_due_and_the_stream_degraded(tmp_path):
    f = tmp_path / "f.txt"
    f.write_text("1")
    loop, writer, clock, calls = make([PanelPlan("a", inputs=(File(str(f)),))], backstop_s=1e9)
    _fail_publish_once(writer)
    assert loop.run_once(clock.t) == ["a"]  # first derive: the publish fails
    assert isinstance(loop.last_publish_error, OSError) and writer.of("a") == []
    jobs = loop._evaluate(clock.t)
    assert [j.pid for j in jobs] == ["a"]  # still due, not waiting for the backstop
    assert writer.streams[-1]["state"] == "degraded" and "write error" in writer.streams[-1]["detail"]
    assert loop.run_once(clock.t) == ["a"] and len(writer.of("a")) == 1
    assert loop.last_publish_error is None and loop.run_once(clock.t) == []
    loop.stop()


def test_a_failed_publish_after_an_input_move_retries_the_move(tmp_path):
    f = tmp_path / "f.txt"
    f.write_text("1")
    loop, writer, clock, calls = make([PanelPlan("a", inputs=(File(str(f)),))], backstop_s=1e9)
    settle(loop, clock)
    f.write_text("22")
    _fail_publish_once(writer)
    assert loop.run_once(clock.t) == ["a"]
    assert len(writer.of("a")) == 1
    assert loop.run_once(clock.t) == ["a"]  # the move was not consumed by the failed publish
    assert len(writer.of("a")) == 2
    loop.stop()


def test_a_failed_publish_keeps_the_write_owed():
    loop, writer, clock, _ = make([PanelPlan("a")], backstop_s=1e9)
    settle(loop, clock)
    _fail_publish_once(writer)
    loop.dirty(["a"], 5)
    loop.run_once(clock.t)
    assert loop.run_once(clock.t) == ["a"] and writer.of("a")[-1]["write_gen"] == 5


@pytest.mark.parametrize("status", ["error", "timeout"])
def test_an_error_or_timeout_result_is_published_without_a_body(status):
    loop, writer, clock, _ = make(
        [PanelPlan("a")], fn=lambda pid: DeriveResult(status=status, body={"junk": 1}, error="e")
    )
    loop.run_once(clock.t)
    pub = writer.of("a")[0]
    assert pub["status"] == status and pub["body"] is None
    loop.stop()


def test_valid_until_that_passed_during_the_derive_does_not_hot_loop():
    loop, writer, clock, _ = make([PanelPlan("a")], backstop_s=1e9)

    def slow(pid):
        clock.t += 10
        return ok(valid_until=clock.t - 5)  # after the start, before the finish

    loop._derive_fn = slow
    settle(loop, clock)
    assert writer.of("a")[0]["valid_until"] is None
    assert loop.run_once(clock.t) == []
    loop.stop()


def test_timeout_s_zero_is_not_read_as_unset():
    loop, writer, clock, _ = make([PanelPlan("a", timeout_s=0.0)])
    with loop._lock:
        st = loop._states["a"]
        st.running_since = clock.t
    loop._publish_timeouts(clock.t)
    assert writer.of("a")[-1]["status"] == "timeout"


def test_a_failed_timeout_publish_is_retried_and_timed_out_is_set_only_after_it_lands():
    loop, writer, clock, _ = make([PanelPlan("a", timeout_s=5.0)])
    with loop._lock:
        loop._states["a"].running_since = clock.t
    _fail_publish_once(writer)
    clock.t += 6
    loop._publish_timeouts(clock.t)
    assert not loop._states["a"].timed_out and writer.of("a") == []
    loop._publish_timeouts(clock.t)
    assert loop._states["a"].timed_out and writer.of("a")[-1]["status"] == "timeout"


def test_stop_drops_queued_jobs_and_frees_their_panels():
    gate, entered = threading.Event(), threading.Event()

    def fn(pid):
        entered.set()
        assert gate.wait(WAIT * 4)
        return ok("x")

    loop, writer, clock, _ = make([PanelPlan("a"), PanelPlan("b")], fn=fn, poll_s=0.01, threads=1, backstop_s=1e9)
    loop.start()
    try:
        assert entered.wait(WAIT)
        deadline = time.monotonic() + WAIT
        while time.monotonic() < deadline and not loop._queue:
            time.sleep(0.005)
        assert loop._queue  # one is running, one is waiting
        loop.stop(timeout=0.05)
        assert not loop._queue and sum(1 for s in loop._states.values() if s.busy) == 1  # only the running one
    finally:
        gate.set()
        loop.stop()


def test_the_fingerprinter_is_closed_only_after_every_thread_has_ended():
    gate, entered = threading.Event(), threading.Event()

    def fn(pid):
        entered.set()
        assert gate.wait(WAIT * 4)
        return ok("x")

    loop, writer, clock, _ = make([PanelPlan("a")], fn=fn, poll_s=0.01, threads=1, backstop_s=1e9)
    closed = []
    real_close = loop._fingerprinter.close
    loop._fingerprinter.close = lambda: (closed.append(1), real_close())
    loop.start()
    assert entered.wait(WAIT)
    alive = loop.stop(timeout=0.05)
    assert [t.name for t in alive] == ["derive-0"] and closed == []  # the running derive is reported, the fingerprinter kept
    gate.set()
    deadline = time.monotonic() + WAIT
    while time.monotonic() < deadline and not closed:
        time.sleep(0.01)
    assert closed == [1]
    assert loop.stop() == [] and closed == [1]  # a second stop() waits for nothing and closes nothing again


def test_a_job_taken_before_stop_but_not_yet_begun_is_dropped():
    loop, writer, clock, calls = make([PanelPlan("a")], backstop_s=1e9)
    job = loop._evaluate(clock.t)[0]
    loop._states["a"].busy = True
    loop._stopping = True  # stop() ran between the worker taking the job and beginning it
    assert loop._run_job(job, clock.t) is None
    assert calls == [] and writer.of("a") == [] and not loop._states["a"].busy
    loop.stop()


def test_start_after_stop_and_a_second_start_raise():
    loop, writer, clock, _ = make([PanelPlan("a")], poll_s=0.01)
    loop.stop()  # stop before any start is terminal too
    with pytest.raises(RuntimeError):
        loop.start()


def test_an_error_publish_carries_the_write_gen_it_was_derived_for():
    def fn(pid):
        raise ValueError("x")

    loop, writer, clock, _ = make([PanelPlan("a")], fn=fn, backstop_s=1e9)
    loop.dirty(["a"], 7)
    loop.run_once(clock.t)
    pub = writer.of("a")[-1]
    assert pub["status"] == "error" and pub["write_gen"] == 7
    assert loop.run_once(clock.t) == []  # answered, not owed again
    loop.stop()


def test_a_timeout_publish_carries_the_write_gen_of_the_running_derive():
    loop, writer, clock, _ = make([PanelPlan("a", timeout_s=5)], backstop_s=1e9)
    with loop._lock:
        st = loop._states["a"]
        st.running_since, st.run_write_gen = clock.t, 4
    clock.t += 6
    loop._publish_timeouts(clock.t)
    assert writer.of("a")[-1]["status"] == "timeout" and writer.of("a")[-1]["write_gen"] == 4
    loop.stop()


def test_a_reader_waiting_for_a_write_is_answered_by_the_error_row(tmp_path):
    w = snapshots.SnapshotWriter(tmp_path / snapshots.DB_FILENAME)

    def fn(pid):
        raise ValueError("x")

    loop = DeriveLoop(w, {"a": PanelPlan("a")}, fn, backstop_s=1e9)
    loop.dirty(["a"], 7)
    loop.run_once(1000.0)
    snap = snapshots.SnapshotReader(tmp_path / snapshots.DB_FILENAME).read("a")
    assert snap.status == "error" and snap.write_gen >= 7
    loop.stop()
    w.close()


def test_a_pending_publish_error_clears_when_the_panel_no_longer_owes_one(tmp_path):
    f = tmp_path / "f.txt"
    f.write_text("1")
    loop, writer, clock, _ = make([PanelPlan("a", inputs=(File(str(f)),))], backstop_s=1e9)
    settle(loop, clock)
    f.write_text("22")
    _fail_publish_once(writer)
    loop.run_once(clock.t)  # the publish for the moved input fails
    assert loop._pending_pub and loop.last_publish_error is not None
    f.write_text("1")  # the input goes back to what the store already reflects
    st = loop._states["a"]
    st.inputs_fp = loop._views()[0]["a"].fp
    loop._evaluate(clock.t)
    assert not loop._pending_pub and loop.last_publish_error is None
    loop._evaluate(clock.t)
    assert writer.streams[-1]["state"] == "ok"
    loop.stop()


def test_sqlite_reopen_failure_leaves_no_closed_connection_cached(tmp_path, monkeypatch):
    path = tmp_path / "s.db"
    c = sqlite3.connect(path)
    c.execute("CREATE TABLE t(x)")
    c.commit()
    c.close()
    fp = derive._Fingerprinter()
    try:
        fp.fingerprint(Sqlite(str(path)))
        held = fp._conns[str(path)][0]
        os.replace(str(path), str(tmp_path / "old.db"))
        c = sqlite3.connect(path)
        c.execute("CREATE TABLE t(x)")
        c.commit()
        c.close()

        def refuse(*a, **k):
            raise sqlite3.OperationalError("unable to open")

        monkeypatch.setattr(derive.sqlite3, "connect", refuse)
        assert fp.fingerprint(Sqlite(str(path))) is None
        assert str(path) not in fp._conns
        monkeypatch.undo()
        assert fp.fingerprint(Sqlite(str(path))) not in (None, "absent")
        assert fp._conns[str(path)][0] is not held
    finally:
        fp.close()


def test_a_vanished_sqlite_file_answers_absent_and_closes_the_held_connection(tmp_path):
    path = tmp_path / "s.db"
    c = sqlite3.connect(path)
    c.execute("CREATE TABLE t(x)")
    c.commit()
    c.close()
    fp = derive._Fingerprinter()
    try:
        fp.fingerprint(Sqlite(str(path)))
        os.unlink(path)
        assert fp.fingerprint(Sqlite(str(path))) == "absent" and fp._conns == {}
    finally:
        fp.close()


def test_the_module_fingerprint_helper_holds_no_connection(tmp_path):
    path = tmp_path / "s.db"
    c = sqlite3.connect(path)
    c.execute("CREATE TABLE t(x)")
    c.commit()
    c.close()
    assert fingerprint(Sqlite(str(path))) not in (None, "absent")
    assert not hasattr(derive, "_DEFAULT_FINGERPRINTER")


def test_a_same_size_atomic_replace_moves_a_dir_fingerprint(tmp_path):
    d = tmp_path / "d"
    d.mkdir()
    a = d / "ref"
    a.write_text("A" * 41)
    ts = os.stat(a).st_mtime_ns
    before = fingerprint(Dir(str(d)))
    tmp = d / "tmp"
    tmp.write_text("B" * 41)
    os.utime(tmp, ns=(ts, ts))
    os.replace(tmp, a)  # same name, size and mtime: only the inode differs
    assert fingerprint(Dir(str(d))) != before


def test_a_same_size_atomic_replace_moves_the_refs_walk(tmp_path):
    refs = tmp_path / "refs"
    (refs / "heads").mkdir(parents=True)
    a = refs / "heads" / "main"
    a.write_text("A" * 41)
    ts = os.stat(a).st_mtime_ns
    before = derive._refs_walk(str(refs))
    tmp = refs / "heads" / "tmp"
    tmp.write_text("B" * 41)
    os.utime(tmp, ns=(ts, ts))
    os.replace(tmp, a)
    assert derive._refs_walk(str(refs)) != before

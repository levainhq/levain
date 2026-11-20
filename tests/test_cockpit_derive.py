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
        self.drops: list[str] = []
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

    def drop(self, panel_id):
        with self.lock:
            self.drops.append(panel_id)
            return True

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


def _loop_thread_names(loop):
    return sorted(t.name for t in loop._threads)


def _started_names(**kw):
    loop, writer, clock, _ = make([PanelPlan("a")], poll_s=0.01, threads=3, **kw)
    loop.start()
    try:
        names = _loop_thread_names(loop)
        assert writer.wait_for(lambda pubs: len(pubs) >= 1)
    finally:
        loop.stop()
    return names


def test_thread_names_default_to_derive_poll_and_derive_n():
    assert _started_names() == ["derive-0", "derive-1", "derive-2", "derive-poll"]


def test_thread_name_prefix_applies_to_the_poll_thread_and_every_derive_thread():
    assert _started_names(thread_name_prefix="pfx-") == ["pfx-derive-0", "pfx-derive-1", "pfx-derive-2", "pfx-derive-poll"]


def test_an_empty_thread_name_prefix_is_the_default():
    assert _started_names(thread_name_prefix="") == _started_names()


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


def test_past_valid_until_is_published_as_declared_and_not_kept_for_scheduling():
    loop, writer, clock, _ = make([PanelPlan("a")], fn=lambda pid: ok(valid_until=clock.t - 5), backstop_s=1e9)
    settle(loop, clock)
    assert writer.of("a")[0]["valid_until"] == clock.t - 5  # the store keeps it: a reader marks the row stale
    assert loop._states["a"].valid_until is None
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

    def flaky(*args):
        n.append(1)
        if len(n) == 1:
            raise RuntimeError("bad input")
        return real(*args)

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
    assert writer.of("a")[0]["valid_until"] is not None  # published as declared
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
    assert "publish:a" in loop._write_errors and loop.last_publish_error is not None
    f.write_text("1")  # the input goes back to what the store already reflects
    st = loop._states["a"]
    st.inputs_fp = loop._views(loop._plans)[0]["a"].fp
    loop._evaluate(clock.t)
    assert not loop._write_errors and loop.last_publish_error is None
    assert writer.streams[-1]["state"] == "ok"  # the same poll that pruned it wrote the stream clean
    loop.stop()


def test_stop_racing_start_never_raises():
    errors = []
    for _ in range(40):
        loop, _w, _c, _ = make([PanelPlan("a")], poll_s=0.005, threads=2)
        go = threading.Barrier(2)

        def starter():
            go.wait()
            try:
                loop.start()
            except RuntimeError:
                pass  # stop() won: a stopped loop does not start
            except Exception as exc:
                errors.append(exc)

        t = threading.Thread(target=starter)
        t.start()
        go.wait()
        try:
            loop.stop(WAIT)
        except Exception as exc:  # RuntimeError: cannot join thread before it is started
            errors.append(exc)
        t.join(WAIT)
        loop.stop(WAIT)
    assert errors == []


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


# ------------------------------------------------------------------------------------------ L3 round 4: errors per source


def _fail_n(obj, name, n, exc=OSError):
    real, left = getattr(obj, name), [n]

    def flaky(*a, **k):
        if left[0]:
            left[0] -= 1
            raise exc("boom")
        return real(*a, **k)

    setattr(obj, name, flaky)


def test_a_heartbeat_success_does_not_clear_a_failing_timeout_publish():
    loop, writer, clock, _ = make([PanelPlan("a", timeout_s=5.0)])
    with loop._lock:
        st = loop._states["a"]
        st.running_since, st.busy = clock.t, True
    _fail_n(writer, "publish", 10**6)
    _fail_n(writer, "heartbeat", 1)
    clock.t += 6
    loop._evaluate(clock.t)  # the heartbeat fails, and so does the timeout publish
    loop._evaluate(clock.t)  # the heartbeat lands; the timeout publish still fails
    assert writer.streams[-1]["state"] == "degraded" and "timeout:a" in writer.streams[-1]["detail"]
    loop.stop()


def test_a_publish_failure_is_cleared_by_that_panels_publish_not_by_a_heartbeat(tmp_path):
    fa, fb = tmp_path / "a.txt", tmp_path / "b.txt"
    fa.write_text("1")
    fb.write_text("1")
    loop, writer, clock, _ = make(
        [PanelPlan("a", inputs=(File(str(fa)),)), PanelPlan("b", inputs=(File(str(fb)),))], backstop_s=1e9
    )
    settle(loop, clock)
    fa.write_text("22")
    fb.write_text("22")
    real = writer.publish

    def only_a_fails(pid, **k):
        if pid == "a" and fail_a[0]:
            raise OSError("disk full")
        return real(pid, **k)

    fail_a = [True]
    writer.publish = only_a_fails
    loop.run_once(clock.t)  # a's publish fails, b's lands
    loop._evaluate(clock.t)  # a heartbeat success in between
    assert writer.streams[-1]["state"] == "degraded" and "publish:a" in writer.streams[-1]["detail"]
    assert "publish:b" not in writer.streams[-1]["detail"]
    fail_a[0] = False
    loop.run_once(clock.t)  # a's publish lands
    loop._evaluate(clock.t)
    assert writer.streams[-1]["state"] == "ok"
    loop.stop()


def test_a_set_stream_failure_is_cleared_only_by_a_set_stream_success():
    loop, writer, clock, _ = make([PanelPlan("a")], backstop_s=1e9)
    settle(loop, clock)
    _fail_n(writer, "set_stream", 1)
    loop._evaluate(clock.t)  # the stream write fails
    assert loop.last_publish_error is not None
    loop._evaluate(clock.t)  # the heartbeat lands; the stream still reads as failed
    assert writer.streams[-1]["state"] == "degraded" and "stream" in writer.streams[-1]["detail"]
    loop._evaluate(clock.t)  # the stream write landed on the previous poll
    assert writer.streams[-1]["state"] == "ok" and loop.last_publish_error is None
    loop.stop()


def test_a_panel_publish_success_does_not_clear_a_failing_stream_write(tmp_path):
    f = tmp_path / "a.txt"
    f.write_text("1")
    loop, writer, clock, _ = make([PanelPlan("a", inputs=(File(str(f)),))], backstop_s=1e9)
    settle(loop, clock)
    f.write_text("22")
    _fail_n(writer, "set_stream", 1)
    assert loop.run_once(clock.t) == ["a"]  # the stream write fails in the evaluation, then a's publish lands
    assert "stream" in loop._write_errors and loop.last_publish_error is not None
    loop._evaluate(clock.t)  # the stream write lands now
    loop._evaluate(clock.t)
    assert not loop._write_errors and writer.streams[-1]["state"] == "ok"
    loop.stop()


# ------------------------------------------------------------------------------------------ a dynamic plan set


def _wait_until(pred, timeout=WAIT):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.005)
    return pred()


def test_set_plans_adds_a_panel_that_derives_on_the_next_run():
    loop, writer, clock, calls = make([PanelPlan("a")], backstop_s=1e9)
    settle(loop, clock)
    loop.set_plans([PanelPlan("a"), PanelPlan("b")])
    assert loop.run_once(clock.t) == ["b"]
    assert calls == ["a", "b"]
    loop.stop()


def test_set_plans_keeps_the_history_of_a_changed_plan_and_derives_again_only_when_its_inputs_changed(tmp_path):
    f, g = tmp_path / "f", tmp_path / "g"
    f.write_text("1")
    g.write_text("1")
    loop, writer, clock, calls = make([PanelPlan("a", inputs=(File(str(f)),))], backstop_s=1e9)
    settle(loop, clock)
    # cadence, flags and the timeout change; the inputs are equal: the fingerprint it derived from still holds
    loop.set_plans([PanelPlan("a", inputs=(File(str(f)),), every_s=500.0, heavy=True, on_token=True, timeout_s=7.0)])
    assert loop.run_once(clock.t) == []
    assert loop._plans["a"].every_s == 500.0
    clock.t += 500
    assert loop.run_once(clock.t) == ["a"]  # the new cadence is the one in force
    # other inputs: a different fingerprint, so it derives again
    loop.set_plans([PanelPlan("a", inputs=(File(str(g)),), every_s=500.0)])
    assert loop.run_once(clock.t) == ["a"]
    assert loop.run_once(clock.t) == []
    loop.stop()


def test_set_plans_keeps_a_dirty_mark_across_a_plan_change():
    loop, writer, clock, calls = make([PanelPlan("a")], backstop_s=1e9)
    settle(loop, clock)
    loop.dirty(["a"], 6)
    loop.set_plans([PanelPlan("a", every_s=1e9)])
    assert loop.run_once(clock.t) == ["a"]
    assert writer.of("a")[-1]["write_gen"] == 6
    loop.stop()


def test_set_plans_retires_the_ids_it_leaves_out():
    loop, writer, clock, calls = make([PanelPlan("a"), PanelPlan("b")], backstop_s=30.0)
    settle(loop, clock)
    loop.set_plans([PanelPlan("a")])
    assert "b" not in loop._states and "b" not in loop._plans
    loop.dirty(["b"], 3)  # no longer a panel of the loop: ignored
    clock.t += 31
    assert loop.run_once(clock.t) == ["a"]
    assert len(writer.of("b")) == 1
    assert not loop.pending("b") and loop.deriving() == []
    loop.stop()


def test_retire_of_an_unknown_id_is_a_no_op_and_a_duplicate_id_in_set_plans_raises():
    loop, writer, clock, calls = make([PanelPlan("a")], backstop_s=1e9)
    settle(loop, clock)
    loop.retire("nope")
    with pytest.raises(ValueError):
        loop.set_plans([PanelPlan("a"), PanelPlan("a", every_s=5.0)])
    assert loop._plans["a"].every_s is None  # the failed call changed nothing
    assert loop.run_once(clock.t) == []
    loop.stop()


def test_set_plans_before_start_then_the_loop_derives_exactly_that_set():
    loop, writer, clock, calls = make([])
    loop.set_plans([PanelPlan("a"), PanelPlan("b")])
    loop.set_plans([PanelPlan("b"), PanelPlan("c")])
    loop.start()
    try:
        assert writer.wait_for(lambda pubs: {"b", "c"} <= {p["id"] for p in pubs})
    finally:
        loop.stop()
    assert {p["id"] for p in writer.publishes} == {"b", "c"}


def test_a_derive_running_when_its_panel_is_retired_finishes_but_is_never_published(tmp_path):
    w = snapshots.SnapshotWriter(tmp_path / snapshots.DB_FILENAME)
    reader = snapshots.SnapshotReader(tmp_path / snapshots.DB_FILENAME)
    gate, entered, returned = threading.Event(), threading.Event(), threading.Event()
    n = []

    def fn(pid):
        n.append(1)
        if len(n) == 2:
            entered.set()
            assert gate.wait(WAIT * 4)
            returned.set()
        return ok({"n": len(n)})

    loop = DeriveLoop(w, {"a": PanelPlan("a")}, fn, poll_s=0.01, threads=2, backstop_s=1e9)
    loop.start()
    try:
        assert _wait_until(lambda: reader.read("a") is not None)
        assert reader.read("a").gen == 1
        loop.dirty(["a"], 5)
        assert entered.wait(WAIT)
        assert loop.pending("a")
        loop.retire("a")  # does not wait for the running derive
        assert not loop.pending("a") and loop.deriving() == []
        gate.set()
        assert returned.wait(WAIT)
    finally:
        gate.set()
        loop.stop()
    assert reader.read("a") is None  # the row went with the panel at retire, and the finished derive did not bring it back
    assert loop.last_publish_error is None
    w.close()


def test_a_retired_derive_never_publishes_a_timeout_either():
    gate, entered = threading.Event(), threading.Event()

    def fn(pid):
        entered.set()
        assert gate.wait(WAIT * 4)
        return ok("late")

    loop, writer, clock, _ = make([PanelPlan("a", timeout_s=50.0)], fn=fn, poll_s=0.01, backstop_s=1e9)
    loop.start()
    try:
        assert entered.wait(WAIT)
        loop.retire("a")
        clock.t += 100  # past the deadline of the derive that is still running
        time.sleep(0.1)
        gate.set()
    finally:
        gate.set()
        loop.stop()
    assert writer.of("a") == []
    assert loop.last_publish_error is None


def test_a_job_queued_for_a_panel_that_is_retired_is_dropped():
    gate = threading.Event()
    calls = []

    def fn(pid):
        calls.append(pid)
        if pid == "a":
            assert gate.wait(WAIT * 4)
        return ok(pid)

    # one worker: "a" (first by id) holds it while "z" waits in the queue
    loop, writer, clock, _ = make([PanelPlan("a"), PanelPlan("z")], fn=fn, poll_s=0.01, threads=1, backstop_s=1e9)
    loop.start()
    try:
        assert _wait_until(lambda: loop.deriving() == ["a", "z"])
        loop.retire("z")
        assert loop.deriving() == ["a"]
        gate.set()
        assert writer.wait_for(lambda pubs: any(p["id"] == "a" for p in pubs))
        time.sleep(0.1)
    finally:
        gate.set()
        loop.stop()
    assert calls == ["a"] and writer.of("z") == []


def test_a_retired_panel_added_back_is_not_derived_while_its_old_derive_still_runs():
    gate, entered = threading.Event(), threading.Event()
    lock = threading.Lock()
    active, peak, n = [0], [0], [0]

    def fn(pid):
        with lock:
            n[0] += 1
            mine = n[0]
            active[0] += 1
            peak[0] = max(peak[0], active[0])
        try:
            if mine == 1:
                entered.set()
                assert gate.wait(WAIT * 4)
            return ok({"call": mine})
        finally:
            with lock:
                active[0] -= 1

    loop, writer, clock, _ = make([PanelPlan("a")], fn=fn, poll_s=0.01, threads=3, backstop_s=1e9)
    loop.start()
    try:
        assert entered.wait(WAIT)
        loop.retire("a")
        loop.set_plans([PanelPlan("a")])
        time.sleep(0.15)  # polls pass: the added-back panel is held while the old derive runs
        assert n[0] == 1 and writer.of("a") == []
        gate.set()
        assert writer.wait_for(lambda pubs: len([p for p in pubs if p["id"] == "a"]) >= 1)
    finally:
        gate.set()
        loop.stop()
    assert peak[0] == 1
    assert [json.loads(p["body"]) for p in writer.of("a")] == [{"call": 2}]  # the old result was discarded


def test_deriving_and_pending_name_the_queued_and_running_panels_and_hand_out_copies():
    gate = threading.Event()

    def fn(pid):
        if pid == "a":
            assert gate.wait(WAIT * 4)
        return ok(pid)

    loop, writer, clock, _ = make([PanelPlan("a"), PanelPlan("m"), PanelPlan("z")], fn=fn, poll_s=0.01, threads=1,
                                  backstop_s=1e9)
    assert loop.deriving() == [] and not loop.pending("a") and not loop.pending("nope")
    loop.start()
    try:
        assert _wait_until(lambda: loop.deriving() == ["a", "m", "z"])  # one running, two queued
        assert loop.pending("a") and loop.pending("m") and loop.pending("z") and not loop.pending("nope")
        got = loop.deriving()
        got.append("junk")
        got.clear()
        assert loop.deriving() == ["a", "m", "z"]
        gate.set()
        assert _wait_until(lambda: loop.deriving() == [])
        assert not loop.pending("a")
    finally:
        gate.set()
        loop.stop()


def test_set_plans_and_retire_from_a_thread_storm_leave_a_consistent_loop():
    ids = [f"p{i}" for i in range(8)]
    errors = []

    def fn(pid):
        time.sleep(0.001)
        return ok(pid)

    loop, writer, clock, _ = make([PanelPlan(i) for i in ids[:4]], fn=fn, poll_s=0.005, threads=3, backstop_s=0.02)
    stop = threading.Event()

    def churn(seed):
        import random
        rng = random.Random(seed)
        try:
            for _ in range(150):
                if rng.random() < 0.3:
                    loop.retire(rng.choice(ids))
                else:
                    loop.set_plans([PanelPlan(i) for i in rng.sample(ids, rng.randint(0, len(ids)))])
                loop.deriving()
                loop.pending(rng.choice(ids))
                loop.dirty([rng.choice(ids)], rng.randint(1, 9))
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    def watch():
        try:
            while not stop.is_set():
                assert set(loop.deriving()) <= set(ids)
                loop.last_publish_error
                time.sleep(0.001)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    watcher = threading.Thread(target=watch)
    loop.start()
    watcher.start()
    try:
        storm = [threading.Thread(target=churn, args=(s,)) for s in range(6)]
        for t in storm:
            t.start()
        for t in storm:
            t.join()
        assert errors == []
        loop.set_plans([PanelPlan(i) for i in ids[:5]])
        assert writer.wait_for(lambda pubs: {"p0", "p1", "p2", "p3", "p4"} <= {p["id"] for p in pubs})
        assert set(loop._states) == set(loop._plans) == set(ids[:5])
        loop.retire("p0")
        n_after_retire = len(writer.of("p0"))
        time.sleep(0.15)  # many polls and backstop periods pass
    finally:
        stop.set()
        watcher.join()
        loop.stop()
    assert errors == []
    assert len(writer.of("p0")) == n_after_retire  # nothing published for a retired id once retire() returned
    assert loop.deriving() == [] and not derive_threads()


# ------------------------------------------------------------------------------------------ the on_publish hook


def test_on_publish_fires_once_per_publish_that_moved_the_gen_or_the_write_gen_and_after_the_row_is_readable(tmp_path):
    w = snapshots.SnapshotWriter(tmp_path / snapshots.DB_FILENAME)
    reader = snapshots.SnapshotReader(tmp_path / snapshots.DB_FILENAME)
    clock = FakeClock()
    body = {"v": 1}
    seen = []

    def hook(pid, gen, write_gen):
        row = reader.read(pid)
        locks_free = []
        for lk in (loop._lock, loop._write_lock):  # called with neither loop lock held
            locks_free.append(lk.acquire(timeout=1.0))
            if locks_free[-1]:
                lk.release()
        seen.append((pid, gen, write_gen, row.gen, row.write_gen, all(locks_free)))

    loop = DeriveLoop(w, {"a": PanelPlan("a", every_s=10.0)}, lambda pid: ok(dict(body)), clock=clock, backstop_s=1e9,
                      on_publish=hook)
    loop.run_once(clock.t)
    assert seen == [("a", 1, 0, 1, 0, True)]
    clock.t += 10
    assert loop.run_once(clock.t) == ["a"]  # derived again, same body, nothing moved: no call
    assert len(seen) == 1
    loop.dirty(["a"], 5)
    loop.run_once(clock.t)  # same body, but the write_gen moved
    assert seen[-1] == ("a", 1, 5, 1, 5, True) and len(seen) == 2
    body["v"] = 2
    clock.t += 10
    loop.run_once(clock.t)  # the body changed: the gen moved
    assert seen[-1] == ("a", 2, 5, 2, 5, True) and len(seen) == 3
    assert loop.last_hook_error is None and loop.hook_errors == 0
    loop.stop()
    w.close()


def test_on_publish_fires_on_the_derive_thread_and_a_timeout_row_that_carries_a_write_gen_also_fires():
    gate, entered = threading.Event(), threading.Event()
    calls = []
    fired = []

    def fn(pid):
        calls.append(pid)
        if len(calls) == 2:
            entered.set()
            assert gate.wait(WAIT * 4)
        return ok("x")

    def hook(pid, gen, write_gen):
        fired.append((pid, write_gen, threading.current_thread().name))

    loop, writer, clock, _ = make([PanelPlan("a", timeout_s=50.0)], fn=fn, poll_s=0.01, backstop_s=1e9, on_publish=hook)
    loop.start()
    try:
        assert writer.wait_for(lambda pubs: len(pubs) == 1)
        loop.dirty(["a"], 8)
        assert entered.wait(WAIT)
        clock.t += 60
        assert writer.wait_for(lambda pubs: any(p["status"] == "timeout" for p in pubs))
        assert _wait_until(lambda: any(f[1] == 8 for f in fired))
    finally:
        gate.set()
        loop.stop()
    assert fired[0][0] == "a" and fired[0][2].startswith("derive-")
    assert ("a", 8) in [(f[0], f[1]) for f in fired]


def test_a_raising_hook_is_swallowed_counted_and_the_loop_goes_on():
    seen = []

    def hook(pid, gen, write_gen):
        seen.append(pid)
        raise RuntimeError("hook broke")

    loop, writer, clock, _ = make([PanelPlan("a"), PanelPlan("b")], poll_s=0.01, backstop_s=1e9, on_publish=hook)
    loop.start()
    try:
        assert writer.wait_for(lambda pubs: len(pubs) >= 2)
        assert _wait_until(lambda: loop.hook_errors == 2)
        assert isinstance(loop.last_hook_error, RuntimeError)
        assert loop.last_publish_error is None  # a hook failure is not a write failure
        loop.dirty(["a", "b"], 2)
        assert writer.wait_for(lambda pubs: len(pubs) >= 4)
        assert _wait_until(lambda: loop.hook_errors == 4)
    finally:
        loop.stop()
    assert len(seen) == 4 and len(writer.publishes) == 4 and not derive_threads()


def test_a_hook_error_clears_when_a_later_call_succeeds_and_the_count_stays():
    seen = []

    def hook(pid, gen, write_gen):
        seen.append(write_gen)
        if len(seen) == 1:
            raise RuntimeError("once")

    loop, writer, clock, _ = make([PanelPlan("a")], poll_s=0.01, backstop_s=1e9, on_publish=hook)
    loop.start()
    try:
        assert _wait_until(lambda: loop.hook_errors == 1 and isinstance(loop.last_hook_error, RuntimeError))
        loop.dirty(["a"], 4)
        assert _wait_until(lambda: len(seen) == 2 and loop.last_hook_error is None)
    finally:
        loop.stop()
    assert loop.hook_errors == 1 and seen == [0, 4]


def test_a_hook_that_exits_does_not_take_a_worker_thread_down():
    seen = []

    def hook(pid, gen, write_gen):
        seen.append(pid)
        if len(seen) == 1:
            raise SystemExit(3)

    loop, writer, clock, _ = make([PanelPlan("a")], poll_s=0.01, threads=1, backstop_s=1e9, on_publish=hook)
    loop.start()
    try:
        assert _wait_until(lambda: loop.hook_errors == 1)
        loop.dirty(["a"], 1)
        assert writer.wait_for(lambda pubs: len(pubs) >= 2)
        assert _wait_until(lambda: len(seen) == 2)
        assert len(derive_threads()) >= 2  # the poll thread and the one worker are alive
    finally:
        loop.stop()
    assert loop.hook_errors == 1 and loop.last_hook_error is None


def test_run_once_raises_a_hook_that_exits_as_it_does_for_a_derive():
    def hook(pid, gen, write_gen):
        raise SystemExit(4)

    loop, writer, clock, _ = make([PanelPlan("a")], on_publish=hook)
    with pytest.raises(SystemExit):
        loop.run_once(clock.t)
    assert loop.hook_errors == 1 and len(writer.of("a")) == 1
    loop.stop()


# ------------------------------------------------------------------------------------------ write-first ordering


def test_dirty_panels_derive_before_the_input_and_timer_ones_and_carry_the_write_gen(tmp_path):
    f = tmp_path / "f"
    f.write_text("1")
    loop, writer, clock, calls = make(
        [PanelPlan("a", inputs=(File(str(f)),)), PanelPlan("b", every_s=10.0), PanelPlan("c"), PanelPlan("z")],
        backstop_s=1e9,
    )
    settle(loop, clock)
    f.write_text("22")  # a is due by its input
    clock.t += 10  # b is due by its timer
    loop.dirty(["z"], 7)  # z sorts last by id and has the newest derive
    ran = loop.run_once(clock.t)
    assert ran == ["z", "a", "b"]
    assert writer.of("z")[-1]["write_gen"] == 7 and writer.of("a")[-1]["write_gen"] == 0
    loop.stop()


def test_a_panel_dirtied_while_it_waits_in_the_queue_jumps_the_queue():
    gate, entered = threading.Event(), threading.Event()

    def fn(pid):
        if pid == "a":
            entered.set()
            assert gate.wait(WAIT * 4)
        return ok(pid)

    loop, writer, clock, _ = make(
        [PanelPlan(i) for i in ("a", "b", "c", "z")], fn=fn, poll_s=0.01, threads=1, backstop_s=1e9
    )
    loop.start()
    try:
        assert entered.wait(WAIT)  # "a" holds the one worker; the rest wait in id order
        assert _wait_until(lambda: loop.deriving() == ["a", "b", "c", "z"])
        loop.dirty(["z"], 7)
        gate.set()
        assert writer.wait_for(lambda pubs: len(pubs) == 4)
    finally:
        gate.set()
        loop.stop()
    assert [p["id"] for p in writer.publishes] == ["a", "z", "b", "c"]
    assert writer.of("z")[0]["write_gen"] == 7


# ------------------------------------------------------------------------------------------ an unknown status


def test_an_unknown_status_is_published_as_partial_with_its_error_text(tmp_path):
    w = snapshots.SnapshotWriter(tmp_path / snapshots.DB_FILENAME)
    loop = DeriveLoop(
        w,
        {"a": PanelPlan("a"), "b": PanelPlan("b")},
        lambda pid: DeriveResult(status="stale", body={"rows": [1]}, error="reason kept" if pid == "a" else None),
        clock=FakeClock(),
    )
    loop.run_once(1000.0)
    reader = snapshots.SnapshotReader(tmp_path / snapshots.DB_FILENAME)
    a, b = reader.read("a"), reader.read("b")
    assert a.status == "partial" and a.error == "reason kept" and reader.decoded(a) == {"rows": [1]}
    assert b.status == "partial" and "stale" in b.error  # no text given: the status it was is named
    loop.stop()
    w.close()


def test_an_unknown_status_reaches_the_writer_as_partial():
    loop, writer, clock, _ = make([PanelPlan("a")], fn=lambda pid: DeriveResult(status="bogus", body=1, error="e"))
    loop.run_once(clock.t)
    pub = writer.of("a")[0]
    assert pub["status"] == "partial" and pub["error"] == "e" and pub["body"] == b"1"
    loop.stop()


def test_a_panel_added_to_the_plan_set_is_not_an_input_event_and_a_removed_sqlite_input_closes_its_connection(tmp_path):
    f = tmp_path / "f"
    f.write_text("1")
    db = tmp_path / "x.db"
    sqlite3.connect(db).close()
    loop, writer, clock, _ = make(
        [PanelPlan("a", inputs=(File(str(f)),)), PanelPlan("s", inputs=(Sqlite(str(db)),))], backstop_s=1e9
    )
    settle(loop, clock)
    assert str(db) in loop._fingerprinter._conns
    assert writer.streams[-1]["last_event_at"] is None
    loop.set_plans([PanelPlan("a", inputs=(File(str(f)),)), PanelPlan("n", inputs=(File(str(tmp_path / "other")),))])
    assert loop.run_once(clock.t) == ["n"]
    assert writer.streams[-1]["last_event_at"] is None  # a new input appearing is not an input moving
    assert loop._fingerprinter._conns == {}  # the vanished panel's sqlite connection was closed
    f.write_text("22")
    loop.run_once(clock.t)
    assert writer.streams[-1]["last_event_at"] == clock.t
    loop.stop()


def test_an_unknown_token_re_derives_a_no_input_panel_on_the_slow_tick_not_the_backstop():
    loop, writer, clock, calls = make([PanelPlan("a", on_token=True)], token_fn=lambda: None, backstop_s=1e9)
    settle(loop, clock)
    assert calls == ["a"]
    assert loop.run_once(clock.t) == []                 # within one slow tick: nothing
    clock.t += 60                                       # well past it and far short of the backstop
    assert loop.run_once(clock.t) == ["a"]
    assert loop.run_once(clock.t) == []
    loop.stop()


def test_retire_and_set_plans_drop_the_rows_of_the_panels_that_leave_and_a_failing_drop_does_not_break_the_plan_change():
    loop, writer, clock, calls = make([PanelPlan("a"), PanelPlan("b"), PanelPlan("c")])
    loop.retire("a")
    assert writer.drops == ["a"]
    loop.set_plans([PanelPlan("c")])
    assert writer.drops == ["a", "b"]
    loop.retire("nope")
    assert writer.drops == ["a", "b"]           # an id that was never planned has no row to drop

    def boom(pid):
        raise OSError("disk")

    writer.drop = boom
    loop.set_plans([])                           # the plan change lands though the row could not be dropped
    assert loop.deriving() == [] and not loop._plans
    loop.stop()


# ------------------------------------------------------------------------------------------ S3a fix 2


def test_a_publish_that_fails_while_the_panel_is_retired_leaves_no_error_for_the_retired_panel():
    loop, writer, clock, calls = make([PanelPlan("a"), PanelPlan("b")])

    def boom(*a, **k):
        raise OSError("disk full")

    writer.publish = boom
    real_fail, retirers = loop._fail, []

    def fail_with_a_retire_trying_to_run(key, exc):
        # a retire that arrives as the failed publish is recorded: it must wait for the record, not slip in before it
        if key == "publish:a":
            t = threading.Thread(target=loop.retire, args=("a",), name="retirer")
            t.start()
            t.join(0.5)
            retirers.append(t)
        real_fail(key, exc)

    loop._fail = fail_with_a_retire_trying_to_run
    job = derive._Job("a", "first", "", derive._UNSET, loop._states["a"])
    loop._states["a"].busy = True
    loop._run_job(job, clock.t)
    retirers[0].join(WAIT)
    assert not retirers[0].is_alive() and "a" not in loop._states
    assert "publish:a" not in loop._write_errors
    loop.stop()


def _bad_inputs_plan_and_a_good_one(tmp_path):
    f = tmp_path / "f.txt"
    f.write_text("1")
    return [PanelPlan("bad", inputs=([],)), PanelPlan("good", inputs=(File(str(f)),))]


def test_a_plan_whose_inputs_cannot_be_looked_at_is_degraded_and_the_other_panels_still_derive(tmp_path):
    loop, writer, clock, calls = make(_bad_inputs_plan_and_a_good_one(tmp_path), backstop_s=1e9)
    assert sorted(loop.run_once(clock.t)) == ["bad", "good"]       # the bad one derives on first, by the timers' rules
    assert writer.of("good") and writer.of("bad")
    assert writer.streams[-1]["state"] == "degraded" and "bad: inputs unreadable [TypeError]" in writer.streams[-1]["detail"]
    assert loop.run_once(clock.t) == []
    loop.stop()


def test_a_fingerprint_that_raises_for_one_plan_does_not_stop_the_poll(tmp_path, monkeypatch):
    f, g = tmp_path / "f.txt", tmp_path / "g.txt"
    f.write_text("1")
    g.write_text("1")
    loop, writer, clock, calls = make(
        [PanelPlan("bad", inputs=(File(str(f)),)), PanelPlan("good", inputs=(File(str(g)),))], backstop_s=1e9)
    real = loop._fingerprinter.fingerprint

    def fingerprint(inp):
        if inp == File(str(f)):
            raise RuntimeError("cannot look")
        return real(inp)

    monkeypatch.setattr(loop._fingerprinter, "fingerprint", fingerprint)
    assert sorted(loop.run_once(clock.t)) == ["bad", "good"]
    assert writer.heartbeats and "bad: inputs unreadable [RuntimeError]" in writer.streams[-1]["detail"]
    g.write_text("22")
    assert loop.run_once(clock.t) == ["good"]                       # the good panel still follows its own input
    loop.stop()


def test_a_plan_whose_sqlite_path_is_unhashable_degrades_alone_and_the_other_panels_still_derive(tmp_path):
    f = tmp_path / "f.txt"
    f.write_text("1")
    good = PanelPlan("good", inputs=(File(str(f)),))
    loop, writer, clock, calls = make([good], backstop_s=1e9)
    assert loop.run_once(clock.t) == ["good"]
    # a plan set that changes is the moment the held Sqlite connections are trimmed; the plan is built by hand around the
    # registration check
    loop.set_plans([good, PanelPlan("bad", inputs=(Sqlite([]),))])
    assert loop.run_once(clock.t) == ["bad"]
    assert writer.of("good") and writer.heartbeats
    assert "bad: inputs unreadable [TypeError]" in writer.streams[-1]["detail"]
    loop.stop()

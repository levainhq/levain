"""Starting and watching a Codex coordinator launch (C3c). A fake app-server daemon replays what C3c's runs measured
on codex-cli 0.162.1 (1021+18 lane c3c): WebSocket over the unix control socket after an HTTP Upgrade, JSON-RPC with
no "jsonrpc" field, ``initialize`` naming ``codexHome``, and ``-32600 "thread not loaded: <id>"`` for a thread the
daemon does not have. No test here reaches a real Codex."""
from __future__ import annotations

import json
import shutil
import sqlite3
import tempfile
from pathlib import Path

import pytest

from levain.coordinator import codex_launch as cx
from levain.coordinator import launch as cl
from levain.coordinator.store import CoordStore
from tests._codex_fake_daemon import FakeDaemon


@pytest.fixture
def home(monkeypatch):
    # a short path: a unix socket path is capped near 104 bytes on macOS, which pytest's tmp_path can pass
    d = Path(tempfile.mkdtemp(prefix="cx", dir="/tmp"))
    monkeypatch.setenv("CODEX_HOME", str(d))
    yield d
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def store(tmp_path, monkeypatch):
    q = tmp_path / "consent"
    q.mkdir(mode=0o700)
    monkeypatch.setattr(cx, "global_hook_live", lambda d: (True, "wired to this install"))
    monkeypatch.setattr(cx, "UNLISTED_RECHECK", 0.0)
    # the coordinator's workspace, a directory Levain makes (head ruling 13): never the operator's own install
    work, ready, detail = cx.setup(tmp_path / "coordinator-codex")
    assert ready, detail
    s = CoordStore(q)
    # main refused this outright ("only claude is built"): C3c's reproduced failure (repro_main.txt)
    s.coord_set_agent("codex", cwd=str(work))
    return s


def _woken(store: CoordStore) -> str:
    d, _ = store.accept_drop("fix the build", surface="cli")
    return store.woken_launch(d.drop_id)


def test_a_codex_coordinator_starts_on_the_daemon_and_ends_when_its_thread_is_gone(home, store):
    daemon = FakeDaemon(home)
    try:
        launch_id = _woken(store)
        got = cl.launch_session(store, launch_id)
        assert got.started, got.refusal
        ln = store.launch(launch_id)
        tid = ln.harness_session_id
        assert ln.state == "active" and tid in daemon.threads
        th = daemon.threads[tid]
        assert th["name"] == cl.session_name(ln) and th["cwd"] == str(Path(ln.cwd).resolve())
        assert got.attach == f"codex resume --remote unix://{cx.control_socket(home)} {tid}"
        start = next(p for m, p in daemon.calls if m == "thread/start")
        assert set(start) == {"cwd"}               # no approval or sandbox override: the user's config decides
        # the run's failure (run_done_pass1.txt): the daemon's answer for a thread it lacks is "thread not loaded",
        # which the first build read as "cannot read" and so never ended the launch
        del daemon.threads[tid]
        lines = cl.watch_once(store)
        assert any("ended: 0 turns" in line for line in lines), lines
        assert store.launch(launch_id).state == "ended"
    finally:
        daemon.close()


def test_no_daemon_refuses_before_the_spawn_claim_with_the_remedy(home, store, tmp_path):
    launch_id = _woken(store)
    got = cl.launch_session(store, launch_id)
    assert not got.started and got.remedy == "codex app-server daemon start"
    assert "not answering" in got.refusal and "left pending" in got.refusal
    assert store.launch(launch_id).state == "pending"           # no spawn claimed: it expires, which is no death
    c = sqlite3.connect(store.path)
    try:
        assert c.execute("SELECT COUNT(*) FROM launch_spawns").fetchone()[0] == 0
    finally:
        c.close()


def test_setup_writes_the_codex_rule_the_briefs_own_commands_match_and_nothing_else(home, store):
    # the reproduced failure (c3c run_done.txt): a real Codex coordinator's first `coord claim` waited on an approval
    # request nobody answers. The rule setup writes must admit the brief's own spelling, word for word (Codex matches
    # argv as an exact prefix), and no other levain verb; a file of that name holding anything else is left alone.
    import re
    import shlex

    from levain import install as inst

    daemon = FakeDaemon(home)
    try:
        launch_id = _woken(store)
        assert cl.launch_session(store, launch_id).started
        ln = store.launch(launch_id)
        brief = (cl._brief_dir(ln) / f"{launch_id}.md").read_text(encoding="utf-8")
    finally:
        daemon.close()
    work = Path(ln.cwd)
    target, live, detail = cx.setup(work)
    rules = work.joinpath(*inst.CODEX_COORDINATOR_RULES_REL)
    assert live and rules.is_file() and inst.persisted_role(work) == "coordinator", detail
    pattern = json.loads(re.search(r"^    pattern = (.*),$", rules.read_text(encoding="utf-8"), re.M).group(1))
    *prefix, verbs = pattern
    commands = [shlex.split(c) for c in re.findall(r"`([^`]*-m levain [^`]*)`", brief)]
    assert commands and any(c[len(prefix)] == "coord" and c[len(prefix) + 1] == "claim" for c in commands)
    for argv in commands:                        # every levain command the brief gives runs unasked
        assert argv[:len(prefix)] == prefix and argv[len(prefix)] in verbs, argv
    assert sorted(verbs) == sorted(inst.COORDINATOR_VERBS) and "drop" not in verbs
    rules.write_text("# the operator's own narrower rule\n", encoding="utf-8")
    assert not cx.setup(work)[1] and rules.read_text(encoding="utf-8") == "# the operator's own narrower rule\n"


def test_a_starter_killed_between_turn_start_and_activation_leaves_a_turn_the_watcher_interrupts(home, store,
                                                                                                monkeypatch):
    # L3 r1 codex HIGH 2, reproduced: the starter SIGKILLed after turn/start and before activation left a running
    # turn whose thread no row named, so nothing ever stopped it. A real SIGKILL, in a forked child: no finally runs.
    import os
    import signal
    from datetime import datetime, timedelta, timezone

    from levain.coordinator import lease

    clock = [datetime.now(timezone.utc)]
    monkeypatch.setattr(store, "_clock", lambda: clock[0])
    daemon = FakeDaemon(home)
    try:
        launch_id = _woken(store)
        pid = os.fork()
        if pid == 0:                                     # the starter: killed outright as it would activate
            try:
                store.launch_activate = lambda *a, **k: os.kill(os.getpid(), signal.SIGKILL)
                cl.launch_session(store, launch_id)
            finally:
                os._exit(3)
        _, status = os.waitpid(pid, 0)
        assert os.WIFSIGNALED(status) and os.WTERMSIG(status) == signal.SIGKILL, status
        (tid, th), = daemon.threads.items()
        assert th["turns"][0]["status"] == "inProgress" and store.launch(launch_id).state == "pending"
        assert cx.watch_once(store) == [] or th["turns"][0]["status"] == "inProgress"   # in flight: never stopped
        clock[0] += lease.LAUNCH_TTL + timedelta(seconds=1)
        store.coord_status()                             # the watcher's pass writes the expiry first
        lines = cx.watch_once(store)
        assert store.launch(launch_id).state == "expired"
        assert th["turns"][0]["status"] == "interrupted", (lines, th)
        assert any(launch_id in line and "interrupted" in line for line in lines), lines
    finally:
        daemon.close()


def test_a_turn_start_without_a_turn_id_fails_the_launch_and_interrupts_its_turn(home, store):
    # coordland L3 r1 codex MED, reproduced (coordint/fix2/repro_before.txt, FIX 3): turn/start answered with a turn
    # in progress but no id, and the launch still activated, recording a coordinator nothing could name the turn of
    daemon = FakeDaemon(home)
    orig = daemon._answer
    daemon._answer = lambda m, p: ({"result": {"turn": {"status": "inProgress"}}} if m == "turn/start" and
                                   orig(m, p) else orig(m, p))
    try:
        launch_id = _woken(store)
        got = cl.launch_session(store, launch_id)
        assert not got.started and "without a turn id" in got.refusal, got.refusal
        assert store.launch(launch_id).state == "failed"
        (th,) = daemon.threads.values()
        assert [t["status"] for t in th["turns"]] == ["interrupted"]
    finally:
        daemon.close()


def test_the_twenty_first_oldest_over_launch_is_still_inspected(home, store):
    # L3 r1 codex HIGH 3, reproduced: the watcher read the newest 20 over launches every pass, so with 21 the oldest,
    # its turn still running, was never inspected; a launch whose thread is seen finished leaves that list for good
    daemon = FakeDaemon(home)
    try:
        ids = []
        for _ in range(21):
            launch_id = _woken(store)
            assert cl.launch_session(store, launch_id).started
            store.launch_end(launch_id, "ended", "done")
            ids.append(launch_id)
        for launch_id in ids[1:]:
            daemon.threads[store.launch(launch_id).harness_session_id]["status"] = {"type": "idle"}
        oldest = daemon.threads[store.launch(ids[0]).harness_session_id]
        assert oldest["status"]["type"] == "active"
        lines = cx.watch_once(store) + cx.watch_once(store)
        assert oldest["turns"][0]["status"] == "interrupted", lines
    finally:
        daemon.close()

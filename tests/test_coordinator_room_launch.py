"""RM-E: the room's launcher (``levain.coordinator.room_launch``) against a fake ``claude`` that speaks stream-json over
real pipes: argv and config files, the open check (init + canary), activation and attach, the room's tripwire
context, resume, lapse recycling, restart, and a close that does not wait behind a hung send. NOTHING here runs a real
Claude child; the frames are the shapes RM0 / RM0B recorded (see ``tests/test_coordinator_claude_session.py``)."""
from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from levain.coordinator import chat, lease, room, room_launch
from levain.coordinator.chat import ChatService, ChatStore, liveness
from levain.coordinator.chat_tripwires import TripwireContext
from levain.coordinator.claude_session import ClaudeSession, PipeTransport, ROOM_MCP_TOOL, ROOM_TOOLS
from levain.coordinator.driver import LivenessEvidence, NotSent, SendCommand
from levain.coordinator.envelopes import EnvelopeStore
from levain.coordinator.room_launch import (RoomLauncher, RoomLaunchError, RoomSetting, make_probe, make_tripwires,
                                            room_tripwire_context)
from levain.coordinator.store import CoordError, Part

T0 = datetime(2026, 11, 3, 12, 0, tzinfo=timezone.utc)

FAKE = r'''#!{python}
import json, os, sys, time, uuid

argv = sys.argv[1:]
def flag(name):
    return argv[argv.index(name) + 1] if name in argv else None

base = os.environ["FAKE_DIR"]
n = len([f for f in os.listdir(base) if f.startswith("spawn-")]) + 1
open(os.path.join(base, "spawn-%d.argv" % n), "w").write(json.dumps(argv))
mode = open(os.path.join(base, "mode")).read().split() if os.path.exists(os.path.join(base, "mode")) else ["ok"]
mode, model = mode[0], (mode[1] if len(mode) > 1 else "fake-model")
sid = flag("--session-id") or flag("--resume")
if mode == "stale_sid":
    sid = open(os.path.join(base, "stale_sid")).read().strip()
if mode == "exit_early" or (mode == "resume_rejected" and "--resume" in argv):
    sys.stderr.write("boom: the fake exits at once\n"); sys.exit(3)
if mode == "deaf":
    time.sleep(600)
RULE = {{"decision": "accept", "source": "user_permanent", "reason_type": "rule"}}
CFG = {{"decision": "accept", "source": "config", "reason_type": "rule"}}
DENY = {{"decision": "reject", "source": "config", "reason_type": "rule"}}
def out(f):
    f.setdefault("session_id", sid); f.setdefault("uuid", str(uuid.uuid4()))
    sys.stdout.write(json.dumps(f) + "\n"); sys.stdout.flush()
def init():
    tools = ["Glob", "Grep", "Read", "WebSearch", "mcp__levain_room__propose_drop"]
    if mode == "bad_tools": tools.append("Bash")
    cwd = os.getcwd()
    if mode == "wrong_cwd" or (mode == "cwd_flip" and inits > 1): cwd = os.path.dirname(cwd)
    out({{"type": "system", "subtype": "init", "cwd": cwd, "tools": tools,
         "mcp_servers": [{{"name": "levain_room", "status": "connected"}}], "model": model,
         "permissionMode": "default" if mode == "bad_mode" else "dontAsk", "claude_code_version": "2.1.317"}})
def tool(tid, name, path, body, decision, error=False):
    out({{"type": "assistant", "message": {{"role": "assistant", "content": [
        {{"type": "tool_use", "id": tid, "name": name, "input": {{"file_path": path}}}}]}}}})
    block = {{"type": "tool_result", "tool_use_id": tid, "content": body}}
    if error: block["is_error"] = True
    out({{"type": "user", "message": {{"role": "user", "content": [block]}},
         "tool_result_meta": [{{"id": tid, "permission_decision": decision}}]}})
def result(denials=()):
    out({{"type": "result", "subtype": "success", "is_error": False, "terminal_reason": "completed",
         "permission_denials": list(denials), "num_turns": 1}})
def tid():
    return "toolu_" + uuid.uuid4().hex[:10]
def say(t):
    out({{"type": "assistant", "message": {{"role": "assistant", "content": [{{"type": "text", "text": t}}]}}}})

inits = 0
for line in sys.stdin:
    try: msg = json.loads(line)
    except ValueError: continue
    if msg.get("type") != "user": continue
    text = msg["message"]["content"]
    if mode == "silent": continue
    inits += 1
    if mode == "mode_flip" and inits > 1: mode = "bad_mode"
    if mode in ("mute_after_open",) and inits > 1: continue
    init()
    if text.startswith("Open check"):
        path = text.split(": ", 1)[1].strip()
        if mode == "no_deny":
            tool("toolu_c", "Read", path, "DECOY", RULE); say("done"); result()
        else:
            tool("toolu_c", "Read", path, "denied", DENY, error=True); say("done")
            result([{{"tool_name": "Read", "tool_use_id": "toolu_c"}}])
        if mode == "deaf_after_open":
            time.sleep(600)
        if mode == "exit_after_open":
            sys.exit(0)
        continue
    if text.startswith("read2 "):
        tool(tid(), "Read", text.split(" ", 1)[1], "file body", CFG)
    elif text.startswith("read "):
        tool(tid(), "Read", text.split(" ", 1)[1], "file body", RULE)
    elif text.startswith("bash"):
        tool(tid(), "Bash", "x", "ran", RULE)
    say("echo: " + text[:40]); result()
'''


class Clock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        self.now += timedelta(seconds=1)
        return self.now


def build(tmp_path, open_room=True):
    consent = tmp_path / "consent"
    consent.mkdir(mode=0o700)
    store = EnvelopeStore(consent, clock=Clock())
    store.coord_set_agent("none")
    cs = ChatStore(store)
    cs._connect().close()
    d = store.accept_drop("the room", surface="cli")[0]
    thread = store.split(d.drop_id, [Part("note", "the room")])[0].thread_id
    if open_room:
        with store._write() as (conn, _now):
            room.open_room_thread(conn, thread)
    fake_dir = tmp_path / "fake"
    fake_dir.mkdir()
    script = tmp_path / "fake_claude"
    script.write_text(FAKE.format(python=sys.executable))
    script.chmod(0o700)
    cwd, home = tmp_path / "cwd", tmp_path / "home"
    cwd.mkdir()
    home.mkdir()
    (cwd / "note.txt").write_text("hi\n")
    svc = ChatService(cs, probe=make_probe(cs), tripwires=make_tripwires(cs), sleep=lambda _s: None)
    w = {"store": store, "cs": cs, "thread": thread, "svc": svc, "fake": fake_dir, "cwd": cwd, "home": home,
         "run": tmp_path / "run", "script": script, "clock": [T0], "launchers": []}
    return w


def teardown(w) -> None:
    for lr in w["launchers"]:
        lr.close()
    w["svc"].close()


@pytest.fixture
def world(tmp_path):
    w = build(tmp_path)
    yield w
    teardown(w)


def mode(w, text="ok") -> None:
    (w["fake"] / "mode").write_text(text)


def launcher(w, *, service=None, **kw) -> RoomLauncher:
    kw.setdefault("now", lambda: w["clock"][0])
    lr = RoomLauncher(w["cs"], service or w["svc"], RoomSetting(str(w["cwd"]), model="sonnet",
                                                                claude_bin=str(w["script"])),
                      run_dir=str(w["run"]), home=str(w["home"]),
                      environ={**os.environ, "FAKE_DIR": str(w["fake"])}, **kw)
    w["launchers"].append(lr)
    return lr


def spawns(w) -> list:
    return [json.loads(p.read_text()) for p in sorted(w["fake"].glob("spawn-*.argv"), key=lambda p: int(p.stem[6:]))]


def say(w, body, *, outbox):
    return w["svc"].send_room(w["thread"], author_surface="cockpit", client_id="tab-a", outbox_id=outbox, body=body)


def checks(w, launch):
    return w["cs"]._read(lambda conn: conn.execute(
        "SELECT kind, source FROM chat_launch_checks WHERE launch_id = ? ORDER BY seq", (launch,)).fetchall())


def state(w, launch):
    return w["cs"]._read(lambda conn: chat._launch_state(conn, launch)[1])


def pump(w, launch, n=40):
    """Read until the fake has nothing more to say (bounded)."""
    total = 0
    for _ in range(n):
        got = w["svc"].read_next(launch)
        total += got
        if not got and total:
            break
        if not got:
            time.sleep(0.05)
    return total


def kinds(w, launch):
    return w["cs"]._read(lambda conn: [r[0] for r in conn.execute(
        "SELECT kind FROM chat_events WHERE launch_id = ? ORDER BY gseq", (launch,)).fetchall()])




# --- the open ----------------------------------------------------------------------------------------------------
def test_ensure_opens_a_live_attached_launch_with_the_rooms_argv_and_files(world):
    lr = launcher(world)
    h = lr.ensure()
    assert (h.room_thread, h.resumed) == (world["thread"], False)
    assert state(world, h.launch_id) == "active"
    assert world["cs"]._read(lambda conn: liveness(conn, h.launch_id)).enabled
    meta = world["svc"].session_meta(h.launch_id)
    assert (meta.permission_mode, set(meta.tools), meta.mcp_servers) == \
        ("dontAsk", {*ROOM_TOOLS, ROOM_MCP_TOOL}, ("levain_room",))
    [argv] = spawns(world)
    assert argv[argv.index("--tools") + 1] == "Read,Grep,Glob,WebSearch"
    assert argv[argv.index("--permission-mode") + 1] == "dontAsk" and "--strict-mcp-config" in argv
    assert argv[argv.index("--setting-sources") + 1] == "user" and argv[argv.index("--model") + 1] == "sonnet"
    for banned in ("--dangerously-skip-permissions", "--bare", "--restricted", "--bg"):
        assert banned not in argv
    assert "--session-id" in argv and "--resume" not in argv
    workdir = Path(world["run"]) / h.launch_id
    mcp = json.loads((workdir / "mcp.json").read_text())["mcpServers"]["levain_room"]
    assert mcp["env"]["LEVAIN_ROOM_LAUNCH"] == h.launch_id and mcp["args"][-1] == h.launch_id
    deny = json.loads((workdir / "settings.json").read_text())["permissions"]["deny"]
    assert any(str(world["home"]) + "/.ssh" in r for r in deny)
    assert stat.S_IMODE(workdir.stat().st_mode) == 0o700
    assert all(stat.S_IMODE(p.stat().st_mode) == 0o600 for p in workdir.iterdir() if p.is_file())
    row = world["cs"]._read(lambda conn: conn.execute("SELECT role, cwd FROM launches WHERE launch_id = ?",
                                                      (h.launch_id,)).fetchone())
    assert row == ("room", str(world["cwd"]))


def test_ensure_with_a_healthy_launch_spawns_nothing_more(world):
    lr = launcher(world)
    a, b = lr.ensure(), lr.ensure()
    assert a == b and len(spawns(world)) == 1


@pytest.mark.parametrize("fake_mode,why", [
    ("bad_tools", "read-only tools"), ("bad_mode", "read-only tools"), ("no_deny", "deny rules not holding"),
    ("exit_early", r"exited \(code 3\)")])
def test_an_open_check_that_fails_ends_the_launch_and_attaches_nothing(world, fake_mode, why):
    mode(world, fake_mode)
    lr = launcher(world)
    with pytest.raises(RoomLaunchError, match=why):
        lr.ensure()
    [launch] = world["cs"]._read(lambda conn: [r[0] for r in conn.execute(
        "SELECT launch_id FROM launches WHERE role = 'room'").fetchall()])
    assert state(world, launch) == "failed"
    assert world["svc"].session_meta(launch) is None
    assert not world["cs"]._read(lambda conn: liveness(conn, launch)).enabled
    assert ("not_live", "activation") in checks(world, launch)
    assert not (Path(world["run"]) / launch).exists()


def test_a_silent_child_fails_at_the_open_deadline_and_is_killed(world):
    mode(world, "silent")
    lr = launcher(world, open_deadline=0.6)
    t0 = time.monotonic()
    with pytest.raises(RoomLaunchError, match="did not end within"):
        lr.ensure()
    assert time.monotonic() - t0 < 10


def test_a_codex_setting_a_relative_cwd_and_a_cwd_inside_a_deny_path_are_refused(world):
    cs, svc = world["cs"], world["svc"]
    with pytest.raises(RoomLaunchError, match="RM-D3"):
        RoomLauncher(cs, svc, RoomSetting(str(world["cwd"]), harness="codex"), run_dir=str(world["run"]))
    with pytest.raises(RoomLaunchError, match="absolute"):
        RoomLauncher(cs, svc, RoomSetting("cwd"), run_dir=str(world["run"]))
    ssh = world["home"] / ".ssh"
    ssh.mkdir()
    with pytest.raises(RoomLaunchError, match="settings could not be built"):
        RoomLauncher(cs, svc, RoomSetting(str(ssh)), run_dir=str(world["run"]), home=str(world["home"]))
    assert not cs._read(lambda conn: conn.execute("SELECT 1 FROM launches").fetchone())


def test_ensure_with_no_open_room_writes_no_launch(tmp_path):
    w = build(tmp_path, open_room=False)
    try:
        with pytest.raises(RoomLaunchError, match="no open room"):
            launcher(w).ensure()
        assert not w["cs"]._read(lambda conn: conn.execute("SELECT 1 FROM launches").fetchone())
        assert spawns(w) == []
    finally:
        teardown(w)


# --- item 1: the room's TripwireContext -------------------------------------------------------------------------------
def settle(w, launch, turns, seconds=8.0):
    """Pump until the launch has recorded ``turns`` ended turns (the open check is the first)."""
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        try:
            w["svc"].read_next(launch)
        except CoordError:
            pass
        if kinds(w, launch).count("turn_ended") >= turns:
            return
        time.sleep(0.05)
    raise AssertionError(f"only {kinds(w, launch).count('turn_ended')} turns ended: {kinds(w, launch)}")


def trips(w, launch):
    return [src for kind, src in checks(w, launch) if src.startswith("tripwire:")]


def test_item1_accepted_rule_reads_and_the_open_checks_own_turn_do_not_latch_the_room(world):
    lr = launcher(world)
    h = lr.ensure()
    path = str(world["cwd"] / "note.txt")
    say(world, f"read {path}", outbox="ob-00000001")
    settle(world, h.launch_id, 2)
    say(world, f"read2 {path}", outbox="ob-00000002")
    settle(world, h.launch_id, 3)
    assert trips(world, h.launch_id) == []
    assert world["cs"]._read(lambda conn: liveness(conn, h.launch_id)).enabled
    assert kinds(world, h.launch_id).count("tool_result") == 3          # the canary's, and both accepted reads


def test_item1_the_workers_context_latches_the_same_room_stream(world):
    """FAILS on the base: a context without rule_allow (every context before this slice) latches the room on the
    open check's own turn and on an accepted rule read."""
    cwd = str(world["cwd"])
    worker = lambda _l: TripwireContext(cwd, lambda _t: 1, lambda _c, _i: False)     # noqa: E731
    svc = ChatService(world["cs"], probe=make_probe(world["cs"]), tripwires=worker, sleep=lambda _s: None)
    try:
        lr = launcher(world, service=svc)
        h = lr.ensure()
        world["svc"] = svc
        say(world, f"read {cwd}/note.txt", outbox="ob-00000001")
        settle_ignoring = time.monotonic() + 5
        while time.monotonic() < settle_ignoring and not trips(world, h.launch_id):
            try:
                svc.read_next(h.launch_id)
            except CoordError:
                pass
            time.sleep(0.05)
        assert trips(world, h.launch_id) == ["tripwire:deficit"]
        assert not world["cs"]._read(lambda conn: liveness(conn, h.launch_id)).enabled
    finally:
        svc.close()


def test_item1_a_tool_outside_the_rooms_set_latches(world):
    lr = launcher(world)
    h = lr.ensure()
    say(world, "bash now", outbox="ob-00000001")
    settle(world, h.launch_id, 2)
    assert trips(world, h.launch_id) == ["tripwire:deficit"]


def test_the_rooms_tripwire_context_names_exactly_its_five_tools_and_two_sources():
    ctx = room_tripwire_context("/x")
    assert ctx.cwd == "/x" and ctx.gate_allowed("c", {}) is False
    assert ctx.rule_allow == frozenset((t, s) for t in (*ROOM_TOOLS, ROOM_MCP_TOOL)
                                       for s in ("user_permanent", "config"))
    with pytest.raises(ValueError):
        ctx.expected_pre_hooks("Bash")


def test_make_probe_and_make_tripwires_route_a_room_launch_and_leave_the_rest(world):
    lr = launcher(world)
    h = lr.ensure()
    cs = world["cs"]
    ev = LivenessEvidence("claude init", {"tools": ["Bash"]}, "now")
    unknown = "ln-" + "0" * 32
    assert make_tripwires(cs, lambda _l: "other")(h.launch_id).cwd == str(world["cwd"])
    assert make_tripwires(cs, lambda _l: "other")(unknown) == "other" and make_tripwires(cs)(unknown) is None
    assert not make_probe(cs, lambda _l, _e: chat.Verdict(True, "other"))(h.launch_id, ev).live   # the room's judges
    assert make_probe(cs, lambda _l, _e: chat.Verdict(True, "other"))(unknown, ev).detail == "other"
    assert not make_probe(cs)(unknown, ev).live


# --- item 2: the cached snapshot ---------------------------------------------------------------------------------------
def test_item2_a_reopen_is_a_new_session_with_a_new_snapshot_and_the_old_one_is_gone(world):
    lr = launcher(world)
    mode(world, "ok first-model")
    h1 = lr.ensure()
    say(world, "hello", outbox="ob-00000001")
    settle(world, h1.launch_id, 2)
    assert world["svc"].session_meta(h1.launch_id).model == "first-model"
    lr.end(h1.launch_id, "test recycle")
    assert world["svc"].session_meta(h1.launch_id) is None and state(world, h1.launch_id) == "ended"
    mode(world, "ok second-model")
    h2 = lr.ensure()
    assert h2.launch_id != h1.launch_id and h2.resumed and h2.harness_session_id == h1.harness_session_id
    assert world["svc"].session_meta(h2.launch_id).model == "second-model"
    assert spawns(world)[1][spawns(world)[1].index("--resume") + 1] == h1.harness_session_id
    assert "--session-id" not in spawns(world)[1]
    assert world["cs"]._read(lambda conn: lease.by_harness_session(conn, h1.harness_session_id, room=True)
                             ).launch_id == h2.launch_id


def test_item2_a_mode_that_changes_in_a_live_session_latches_and_ensure_replaces_the_launch(world):
    mode(world, "mode_flip")
    lr = launcher(world)
    h1 = lr.ensure()
    assert world["svc"].session_meta(h1.launch_id).permission_mode == "dontAsk"
    say(world, "hello", outbox="ob-00000001")
    end = time.monotonic() + 8
    while time.monotonic() < end and not trips(world, h1.launch_id):
        try:
            world["svc"].read_next(h1.launch_id)
        except CoordError:
            pass
        time.sleep(0.05)
    assert trips(world, h1.launch_id) == ["tripwire:mode_changed"]
    assert world["svc"].session_meta(h1.launch_id).permission_mode == "dontAsk"      # the snapshot IS stale ...
    mode(world, "ok")
    h2 = lr.ensure()                                                                   # ... and the latch retires it
    assert h2.launch_id != h1.launch_id and state(world, h1.launch_id) == "ended"
    assert world["svc"].session_meta(h2.launch_id).permission_mode == "dontAsk"


# --- restart, a dead child, a fresh start -------------------------------------------------------------------------------
def cause(w, launch):
    return w["cs"]._read(lambda conn: conn.execute(
        "SELECT cause FROM launch_events WHERE launch_id = ? ORDER BY seq DESC LIMIT 1", (launch,)).fetchone()[0])


def test_a_dead_child_is_replaced_by_a_resume(world):
    lr = launcher(world)
    h1 = lr.ensure()
    lr._live[h1.launch_id].proc.kill()
    lr._live[h1.launch_id].proc.wait(5)
    h2 = lr.ensure()
    assert h2.launch_id != h1.launch_id and h2.resumed and cause(world, h1.launch_id) == "room child exited"


def test_a_restarted_service_ends_the_launch_it_holds_no_session_for_and_resumes(world):
    h1 = launcher(world).ensure()
    svc2 = ChatService(world["cs"], probe=make_probe(world["cs"]), tripwires=make_tripwires(world["cs"]),
                       sleep=lambda _s: None)
    try:
        h2 = launcher(world, service=svc2).ensure()
        assert state(world, h1.launch_id) == "ended" and cause(world, h1.launch_id) == \
            "room launch not held by this process"
        assert h2.resumed and state(world, h2.launch_id) == "active"
        assert svc2.session_meta(h2.launch_id) is not None
    finally:
        svc2.close()


def test_a_fresh_ensure_does_not_resume(world):
    lr = launcher(world)
    h1 = lr.ensure()
    lr.end(h1.launch_id, "test")
    h2 = lr.ensure(fresh=True)
    assert not h2.resumed and h2.harness_session_id != h1.harness_session_id
    assert "--session-id" in spawns(world)[1] and "--resume" not in spawns(world)[1]


def test_close_ends_every_launch_and_stops_the_child(world):
    lr = launcher(world)
    h = lr.ensure()
    proc = lr._live[h.launch_id].proc
    lr.close()
    assert state(world, h.launch_id) == "ended" and proc.poll() is not None
    assert not (Path(world["run"]) / h.launch_id).exists()


# --- lapse ----------------------------------------------------------------------------------------------------------------
def test_an_idle_child_is_recycled_and_a_busy_or_recent_one_is_not(world):
    lr = launcher(world, idle_seconds=1200)
    h = lr.ensure()
    world["clock"][0] = T0 + timedelta(minutes=10)
    assert lr.recycle_idle() == [] and state(world, h.launch_id) == "active"
    world["clock"][0] = T0 + timedelta(minutes=30)
    assert lr.recycle_idle() == [h.launch_id]
    assert state(world, h.launch_id) == "ended" and cause(world, h.launch_id) == "room idle"
    h2 = lr.ensure()
    assert h2.resumed and h2.launch_id != h.launch_id


def test_a_turn_still_open_keeps_the_child_through_the_idle_time(world):
    mode(world, "mute_after_open")
    lr = launcher(world, idle_seconds=1200)
    h = lr.ensure()
    say(world, "hello", outbox="ob-00000001")                       # written, never answered: its turn stays open
    world["clock"][0] = T0 + timedelta(hours=3)
    assert lr.recycle_idle() == [] and state(world, h.launch_id) == "active"


# --- item 3: a close that does not wait behind a hung send --------------------------------------------------------------
BIG = "A" * 4_000_000


def deaf_child(w):
    """A fake that never reads stdin, so a 4 MB line fills the pipe and the writer blocks."""
    mode(w, "deaf")
    return subprocess.Popen([str(w["script"])], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                            text=True, env={**os.environ, "FAKE_DIR": str(w["fake"])})


def blocked_send(session):
    box = {}

    def run():
        try:
            session.send(SendCommand("op", BIG, (), "queue", None))
            box["end"] = "returned"
        except Exception as exc:  # noqa: BLE001
            box["end"] = type(exc).__name__
    t = threading.Thread(target=run, daemon=True)
    t.start()
    time.sleep(0.5)
    assert t.is_alive(), "the send was expected to block on the full pipe"
    return t, box


def test_item3_the_plain_sessions_close_blocks_behind_a_hung_send(world):
    """The defect the room works around (claude_session.py is untouched: RM-G follow-up). If this starts failing,
    ClaudeSession.close no longer waits behind the send and RoomSession.close's kill-first step can go."""
    proc = deaf_child(world)
    session = ClaudeSession(PipeTransport(proc))
    send, _box = blocked_send(session)
    closer = threading.Thread(target=session.close, daemon=True)
    closer.start()
    closer.join(2.0)
    assert closer.is_alive()
    proc.kill()                                   # release both so the test leaves no thread behind
    send.join(10)
    closer.join(10)
    assert not send.is_alive() and not closer.is_alive()


def test_item3_the_rooms_session_closes_at_once_and_frees_the_hung_send(world):
    proc = deaf_child(world)
    session = room_launch.RoomSession(PipeTransport(proc), child=proc)
    send, box = blocked_send(session)
    t0 = time.monotonic()
    closer = threading.Thread(target=session.close, daemon=True)
    closer.start()
    closer.join(15)
    assert not closer.is_alive() and time.monotonic() - t0 < 15
    send.join(10)
    assert not send.is_alive() and box["end"] == "NotSent" and proc.poll() is not None


def test_item3_ending_a_launch_whose_send_is_hung_completes_and_ends_the_launch(world):
    mode(world, "deaf_after_open")
    lr = launcher(world)
    h = lr.ensure()
    send, box = blocked_send(lr._live[h.launch_id].session)
    done = threading.Thread(target=lambda: lr.end(h.launch_id, "test"), daemon=True)
    done.start()
    done.join(20)
    assert not done.is_alive()
    send.join(10)
    assert box["end"] == "NotSent" and state(world, h.launch_id) == "ended"
    assert world["svc"].session_meta(h.launch_id) is None


# --- rme-fix1: L3_rme_r1 findings, each reproduced on the RM-E tip --------------------------------------------------------
def new_thread(w, title="the next room"):
    d = w["store"].accept_drop(title, surface="cli")[0]
    return w["store"].split(d.drop_id, [Part("note", title)])[0].thread_id


def rotate_to(w, thread):
    with w["store"]._write() as (conn, _now):
        room.rotate(conn, thread)


def capture_spawns(w):
    """A spawn seam that keeps every child it started."""
    procs = []

    def spawn(argv, *, cwd, env, stderr):
        p = room_launch._spawn(argv, cwd=cwd, env=env, stderr=stderr)
        procs.append(p)
        return p
    return procs, spawn


@pytest.mark.parametrize("point", ["attach", "live", "read_next"])
def test_c1_a_send_in_the_activation_window_is_never_permanently_refused(world, point):
    """FAILS on the RM-E tip (codex HIGH 1): the launch was resolvable before it was attached, live and given a vendor
    position, so a send in that window was stored refused or cancelled for good under its outbox_id."""
    svc, seen = world["svc"], []

    def race():
        if seen:
            return
        try:
            seen.append(say(world, "hello", outbox="ob-race0001").state)
        except CoordError as exc:
            seen.append(f"nothing written: {exc}")

    if point == "attach":
        orig_attach = svc.attach
        svc.attach = lambda lid, s: (race(), orig_attach(lid, s))[1]
    elif point == "live":
        orig_activate = svc.activate

        def activate(lid, kind, *a, **kw):
            if kind == "live":
                race()
            return orig_activate(lid, kind, *a, **kw)
        svc.activate = activate
    else:
        orig_read = svc.read_next
        svc.read_next = lambda lid: (race(), orig_read(lid))[1]
    lr = launcher(world)
    h = lr.ensure()
    for name in ("attach", "activate", "read_next"):
        svc.__dict__.pop(name, None)
    assert len(seen) == 1
    again = say(world, "hello", outbox="ob-race0001")
    assert again.state not in ("refused", "cancelled"), (seen, again)
    settle(world, h.launch_id, 2, seconds=30)


def test_c2_an_open_whose_child_reports_another_cwd_fails(world):
    """FAILS on the RM-E tip (codex HIGH 2): the reported cwd was never compared with the launch's."""
    mode(world, "wrong_cwd")
    with pytest.raises(RoomLaunchError, match="cwd"):
        launcher(world).ensure()


def test_c2_the_rooms_probe_latches_a_missing_malformed_or_changed_cwd(world):
    """FAILS on the RM-E tip: the room's probe (every later boundary) ignored cwd."""
    lr = launcher(world)
    h = lr.ensure()
    ev = lr._live[h.launch_id].session.liveness_evidence()
    probe = make_probe(world["cs"])
    assert probe(h.launch_id, ev).live
    for cwd in (str(world["cwd"].parent), None, 7, "relative/dir", ""):
        raw = dict(ev.raw)
        if cwd is None:
            raw.pop("cwd", None)
        else:
            raw["cwd"] = cwd
        assert not probe(h.launch_id, ev._replace(raw=raw)).live, cwd


def test_c2_a_cwd_that_changes_in_a_live_session_latches_the_room(world):
    """FAILS on the RM-E tip: the send's boundary probe accepted the second init's other cwd."""
    mode(world, "cwd_flip")
    lr = launcher(world)
    h = lr.ensure()
    say(world, "hello", outbox="ob-00000001")
    end = time.monotonic() + 30
    while time.monotonic() < end and world["cs"]._read(lambda conn: liveness(conn, h.launch_id)).enabled:
        try:
            world["svc"].read_next(h.launch_id)
        except CoordError:
            pass
        time.sleep(0.05)
    assert not world["cs"]._read(lambda conn: liveness(conn, h.launch_id)).enabled


def test_c3_a_room_rotated_during_the_open_fails_the_launch_and_stops_its_child(world):
    """FAILS on the RM-E tip (codex HIGH 3): the launcher activated the room it read before the open check."""
    other = new_thread(world)
    svc = world["svc"]
    orig_activate = svc.activate

    def activate(lid, kind, *a, **kw):
        if kind == "probing":
            rotate_to(world, other)
        return orig_activate(lid, kind, *a, **kw)
    svc.activate = activate
    procs, spawn = capture_spawns(world)
    lr = launcher(world, spawn=spawn)
    try:
        with pytest.raises(RoomLaunchError, match="rotated"):
            lr.ensure()
    finally:
        svc.__dict__.pop("activate", None)
    [launch] = world["cs"]._read(lambda conn: [r[0] for r in conn.execute(
        "SELECT launch_id FROM launches WHERE role = 'room'").fetchall()])
    assert state(world, launch) == "failed" and procs and all(p.poll() is not None for p in procs)


def test_c4_a_child_reporting_another_session_id_than_it_was_given_fails(world):
    """FAILS on the RM-E tip (codex MED 4): a fresh start whose child reports an old ended id was accepted."""
    lr = launcher(world)
    h1 = lr.ensure()
    lr.end(h1.launch_id, "test")
    (world["fake"] / "stale_sid").write_text(h1.harness_session_id)
    mode(world, "stale_sid")
    with pytest.raises(RoomLaunchError, match="session id"):
        lr.ensure(fresh=True)


def test_c5_ensure_after_a_rotation_ends_the_old_rooms_launch(world):
    """FAILS on the RM-E tip (codex MED 5): the old room's launch stayed active with its child running."""
    lr = launcher(world)
    h1 = lr.ensure()
    proc = lr._live[h1.launch_id].proc
    nxt = new_thread(world)
    rotate_to(world, nxt)
    h2 = lr.ensure()
    assert h2.room_thread == nxt and state(world, h1.launch_id) == "ended" and proc.poll() is not None
    assert list(lr._live) == [h2.launch_id]


def test_c5_recycle_ends_a_launch_whose_room_rotated(world):
    """FAILS on the RM-E tip: recycle_idle kept a busy-or-recent launch of a room that is no longer open."""
    lr = launcher(world)
    h1 = lr.ensure()
    rotate_to(world, new_thread(world))
    assert lr.recycle_idle() == [h1.launch_id] and state(world, h1.launch_id) == "ended"


def test_c7_a_rejected_resume_retries_once_fresh_and_says_so(world):
    """FAILS on the RM-E tip (complement MED 1): a resume the vendor rejects failed every reopen."""
    lr = launcher(world)
    h1 = lr.ensure()
    lr.end(h1.launch_id, "test")
    mode(world, "resume_rejected")
    h2 = lr.ensure()
    assert not h2.resumed and h2.resume_failed == h1.harness_session_id
    assert h2.harness_session_id != h1.harness_session_id
    argvs = spawns(world)
    assert len(argvs) == 3 and "--resume" in argvs[1] and "--session-id" in argvs[2]
    rooms = world["cs"]._read(lambda conn: [r[0] for r in conn.execute(
        "SELECT launch_id FROM launches WHERE role = 'room' ORDER BY gseq").fetchall()])
    assert state(world, rooms[1]) == "failed" and "exited" in cause(world, rooms[1])
    probing = world["cs"]._read(lambda conn: conn.execute(
        "SELECT detail FROM chat_launch_checks WHERE launch_id = ? AND kind = 'probing' ORDER BY seq LIMIT 1",
        (h2.launch_id,)).fetchone()[0])
    assert h1.harness_session_id in probing and "fresh" in probing
    assert lr.ensure().resume_failed == h1.harness_session_id         # the record stays with the launch


def test_c7_the_fresh_retry_runs_once_never_a_loop(world):
    lr = launcher(world)
    h1 = lr.ensure()
    lr.end(h1.launch_id, "test")
    mode(world, "exit_early")
    with pytest.raises(RoomLaunchError, match="exited"):
        lr.ensure()
    assert len(spawns(world)) == 3


def exited_before_the_first_write(w, *, only_resume=True):
    """A spawn seam that returns each (or each ``--resume``) child only after it has exited, so the open check's first
    write meets a dead transport."""
    procs = []

    def spawn(argv, *, cwd, env, stderr):
        p = room_launch._spawn(argv, cwd=cwd, env=env, stderr=stderr)
        procs.append(p)
        if not only_resume or "--resume" in argv:
            p.wait(20)
        return p
    return procs, spawn


def test_rme_fix2_r1_a_resumed_child_dead_at_the_first_write_retries_once_fresh(world):
    """FAILS on the rme-fix1 tip (L3_rme_r2 codex OPEN 1): the dead transport at the open check's first write was a
    plain RoomLaunchError, so no fresh retry ran and every ensure() picked the same resume id again."""
    procs, spawn = exited_before_the_first_write(world)
    lr = launcher(world, spawn=spawn)
    h1 = lr.ensure()
    lr.end(h1.launch_id, "test")
    mode(world, "resume_rejected")
    h2 = lr.ensure()
    assert not h2.resumed and h2.resume_failed == h1.harness_session_id
    assert h2.harness_session_id != h1.harness_session_id and state(world, h2.launch_id) == "active"
    argvs = spawns(world)
    assert len(argvs) == 3 and "--resume" in argvs[1] and "--session-id" in argvs[2]
    probing = world["cs"]._read(lambda conn: conn.execute(
        "SELECT detail FROM chat_launch_checks WHERE launch_id = ? AND kind = 'probing' ORDER BY seq LIMIT 1",
        (h2.launch_id,)).fetchone()[0])
    assert h1.harness_session_id in probing and "fresh" in probing


def test_rme_fix3_a_live_resumed_child_with_a_dead_first_write_is_a_plain_failure_never_retried_fresh(world, monkeypatch):
    """FAILS on the ff49d7ea tip (L3_rme_r3 NotSent): any first-write NotSent was read as a dead child, so a healthy
    resumed child with a transient dead pipe was replaced by a fresh one and its history abandoned."""
    monkeypatch.setattr(room_launch, "_NOTSENT_GRACE", 0.05, raising=False)
    lr = launcher(world)
    h1 = lr.ensure()
    lr.end(h1.launch_id, "test")
    real = room_launch.RoomSession.send
    last = []

    def spawn(argv, *, cwd, env, stderr):
        last.append("--resume" in argv)
        return room_launch._spawn(argv, cwd=cwd, env=env, stderr=stderr)

    def send(self, cmd):
        if last and last[-1]:                                # the resume child's open check: a live child, a dead pipe
            raise NotSent("transient dead pipe")
        return real(self, cmd)

    lr._spawn = spawn
    monkeypatch.setattr(room_launch.RoomSession, "send", send)
    with pytest.raises(RoomLaunchError, match="still running") as ei:
        lr.ensure()
    assert not isinstance(ei.value, room_launch._ChildExited)
    argvs = spawns(world)
    assert len(argvs) == 2 and "--resume" in argvs[1]       # one resume spawn, no fresh one


def test_rme_fix2_r1_a_fresh_child_dead_at_the_first_write_is_a_plain_failure_never_retried(world):
    procs, spawn = exited_before_the_first_write(world, only_resume=False)
    mode(world, "exit_early")
    with pytest.raises(RoomLaunchError, match="exited"):
        launcher(world, spawn=spawn).ensure(fresh=True)
    assert len(spawns(world)) == 1


def test_rme_fix2_r1_a_resume_and_its_fresh_retry_both_dead_at_the_first_write_stop_after_one_retry(world):
    lr = launcher(world)
    h1 = lr.ensure()
    lr.end(h1.launch_id, "test")
    procs, spawn = exited_before_the_first_write(world, only_resume=False)
    lr._spawn = spawn
    mode(world, "exit_early")
    with pytest.raises(RoomLaunchError, match="exited"):
        lr.ensure()
    assert len(spawns(world)) == 3


def test_rme_fix2_r2_the_launchers_resume_pick_ignores_a_pending_rows_ask(world):
    """L3_rme_r2 complement UNSETTLED: ``_resume_id`` reads only ``active`` rows, so a pending launch's ask (here an id
    nothing ever recorded) is never picked as the room's newest conversation. A pin: passed on the rme-fix1 tip."""
    lr = launcher(world)
    h1 = lr.ensure()
    lr.end(h1.launch_id, "test")
    with world["store"]._write() as (conn, now):
        lease.open_room_launch(conn, now, harness="claude", model=None, cwd=str(world["cwd"]),
                               room_thread=world["thread"], pid=os.getpid(), resumed_from="sid-asked-only")
    assert lr._resume_id(world["thread"]) == h1.harness_session_id


def test_c8_an_already_active_resumed_launch_still_says_resumed(world):
    """FAILS on the RM-E tip (complement LOW 4)."""
    lr = launcher(world)
    lr.end(lr.ensure().launch_id, "test")
    h2 = lr.ensure()
    assert h2.resumed and lr.ensure() == h2


def test_c8_the_containment_digest_does_not_depend_on_the_deny_list_order(monkeypatch):
    """FAILS on the RM-E tip (deepseek LOW 5)."""
    settings = json.dumps({"permissions": {"deny": []}})
    a = room_launch.containment_digest(settings)
    monkeypatch.setattr(room_launch, "ROOM_DENIED_TOOLS", tuple(reversed(room_launch.ROOM_DENIED_TOOLS)))
    assert room_launch.containment_digest(settings) == a


def test_c8_a_child_that_exits_after_its_open_check_is_not_activated(world):
    """FAILS on the RM-E tip (deepseek LOW 1): the open check declared live a child that had already exited."""
    mode(world, "exit_after_open")
    procs, spawn = capture_spawns(world)
    svc = world["svc"]
    orig_read = svc.read_next

    def read_next(lid):
        procs[-1].wait(20)                       # deterministic: the child has exited by the time it is read
        return orig_read(lid)
    svc.read_next = read_next
    try:
        with pytest.raises(RoomLaunchError, match="exited"):
            launcher(world, spawn=spawn).ensure()
    finally:
        svc.__dict__.pop("read_next", None)


def test_c8_ending_a_launch_closes_its_session_even_when_it_was_never_attached(world, monkeypatch):
    """FAILS on the RM-E tip (deepseek LOW 3): a failed open never closed its session (its reader thread and pipes
    waited for GC)."""
    made = []

    class Recording(room_launch.RoomSession):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            made.append(self)
    monkeypatch.setattr(room_launch, "RoomSession", Recording)
    mode(world, "bad_tools")
    with pytest.raises(RoomLaunchError):
        launcher(world).ensure()
    assert made and all(s.closed for s in made)


def test_c8_a_spawn_that_fails_still_records_not_live(world):
    """Pins deepseek LOW 6 as not a defect: with no probing row before it (the spawn raised), the not_live row is
    still written (this passes on the RM-E tip too; nothing was changed for it)."""
    def spawn(*_a, **_kw):
        raise OSError("no such claude")
    with pytest.raises(RoomLaunchError, match="no such claude"):
        launcher(world, spawn=spawn).ensure()
    [launch] = world["cs"]._read(lambda conn: [r[0] for r in conn.execute(
        "SELECT launch_id FROM launches WHERE role = 'room'").fetchall()])
    assert ("not_live", "activation") in checks(world, launch) and state(world, launch) == "failed"

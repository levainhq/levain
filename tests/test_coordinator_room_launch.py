"""RM-E: the room's launcher (``levain.coordinator.room_launch``) against a fake ``claude`` that speaks stream-json over
real pipes: argv and config files, the open check (init + canary), activation and attach, the room's tripwire
context, resume, lapse recycling, restart, and a close that does not wait behind a hung send. NOTHING here runs a real
Claude child; the frames are the shapes RM0 / RM0B recorded (see ``tests/test_coordinator_claude_session.py``)."""
from __future__ import annotations

import io
import json
import os
import shlex
import stat
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from levain.consent.adapters import claude_code
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
n = len([f for f in os.listdir(base) if f.startswith("spawn-") and f.endswith(".argv")]) + 1
open(os.path.join(base, "spawn-%d.argv" % n), "w").write(json.dumps(argv))
open(os.path.join(base, "spawn-%d.env" % n), "w").write(json.dumps(sorted(os.environ)))
mode = open(os.path.join(base, "mode")).read().split() if os.path.exists(os.path.join(base, "mode")) else ["ok"]
mode, model = mode[0], (mode[1] if len(mode) > 1 else "fake-model")
sid = flag("--session-id") or flag("--resume")
if mode == "stale_sid":
    sid = open(os.path.join(base, "stale_sid")).read().strip()
if mode == "exit_early" or (mode == "resume_rejected" and "--resume" in argv):
    sys.stderr.write("boom: the fake exits at once\n"); sys.exit(3)
if mode == "deaf":
    time.sleep(600)
if mode == "grandchild":
    import subprocess
    g = subprocess.Popen([sys.executable, "-I", "-c", "import time; time.sleep(120)"])
    open(os.path.join(base, "grand.pid"), "w").write(str(g.pid))
    mode = "ok"
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
        paths = [p.strip() for p in text.split("done:", 1)[1].split("\n") if p.strip()]
        open(os.path.join(base, "canary-%d.paths" % n), "w").write(json.dumps(paths))
        denied = []
        for i, path in enumerate(paths):
            scope = path.endswith("scope-canary.txt")
            if mode == "no_deny" or (mode == "scope_open" and scope):
                tool("toolu_c%d" % i, "Read", path, "DECOY", RULE)
            else:
                tool("toolu_c%d" % i, "Read", path, "denied", DENY, error=True)
                denied.append({{"tool_name": "Read", "tool_use_id": "toolu_c%d" % i}})
        say("done"); result(denied)
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
                      environ={**os.environ, "FAKE_DIR": str(w["fake"])}, env_keep=("FAKE_DIR",), **kw)
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
    assert kinds(world, h.launch_id).count("tool_result") == 4          # the two canaries', and both accepted reads


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


def test_item3_the_plain_sessions_close_frees_a_hung_send(world):
    """The defect the room once worked around is gone from ClaudeSession.close: it takes no I/O lock, so a send blocked
    in a full pipe no longer holds it. The send's bytes may have partly left, so it ends ambiguous (BrokenPipeError)."""
    proc = deaf_child(world)
    session = ClaudeSession(PipeTransport(proc))
    send, box = blocked_send(session)
    closer = threading.Thread(target=session.close, daemon=True)
    closer.start()
    closer.join(15)
    assert not closer.is_alive()
    send.join(10)
    assert not send.is_alive() and box["end"] == "BrokenPipeError" and proc.poll() is not None


def test_item3_the_room_session_is_the_plain_close(world):
    """RoomSession adds no close of its own: the plain close does the job."""
    assert "close" not in vars(room_launch.RoomSession)


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
    assert box["end"] == "BrokenPipeError" and state(world, h.launch_id) == "ended"
    assert world["svc"].session_meta(h.launch_id) is None


GRANDCHILD = (
    "import os, subprocess, sys, time\n"
    "g = subprocess.Popen([sys.executable, '-I', '-c', 'import time; time.sleep(120)'])\n"
    "print(g.pid, flush=True)\n"
    "time.sleep(120)\n"
)


def test_item3_the_room_spawns_its_child_in_its_own_group_and_a_close_reaches_a_grandchild(world):
    """The spawn leads its own process group (start_new_session), so the session close signals the group: a
    descendant that inherited the pipe and sleeps is stopped with the child, and a hung send is freed."""
    proc = room_launch._spawn([sys.executable, "-I", "-c", GRANDCHILD], cwd=str(world["fake"]), env=dict(os.environ),
                              stderr=subprocess.DEVNULL)
    grand = int(proc.stdout.readline())
    try:
        assert os.getpgid(proc.pid) == proc.pid and os.getpgid(grand) == proc.pid
        session = room_launch.RoomSession(PipeTransport(proc))
        send, box = blocked_send(session)
        closer = threading.Thread(target=session.close, daemon=True)
        closer.start()
        closer.join(15)
        assert not closer.is_alive()
        send.join(10)
        assert not send.is_alive() and box["end"] == "BrokenPipeError" and proc.poll() is not None
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                os.kill(grand, 0)
            except ProcessLookupError:
                break
            time.sleep(0.05)
        else:
            raise AssertionError("the grandchild outlived the session close")
    finally:
        for kill in (lambda: os.killpg(proc.pid, 9), lambda: os.kill(grand, 9), proc.kill):
            try:
                kill()
            except (ProcessLookupError, PermissionError):
                pass


def _gone(pid, secs=5):
    deadline = time.monotonic() + secs
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.05)
    return False


def test_r1_ending_an_idle_launch_reaches_a_grandchild_and_lets_the_child_exit_on_eof(world):
    """FAILS on the tip (3 lineages): _end stopped only the group leader before the session close, which then saw a
    reaped leader and never signalled the group, so a tool process survived rotation, idle recycle and shutdown. Idle,
    the child now also gets the graceful EOF exit (returncode 0), not a SIGTERM."""
    mode(world, "grandchild")
    lr = launcher(world)
    h = lr.ensure()
    grand = int((world["fake"] / "grand.pid").read_text())
    proc = lr._live[h.launch_id].proc
    try:
        assert os.getpgid(grand) == proc.pid
        lr.end(h.launch_id, "test")
        assert state(world, h.launch_id) == "ended"
        assert proc.returncode == 0, "an idle child should leave on its own at EOF, not be signalled"
        assert _gone(grand), "the grandchild outlived the launch end"
    finally:
        try:
            os.kill(grand, 9)
        except ProcessLookupError:
            pass


def test_r1_a_failed_open_before_attach_stops_the_whole_group(world):
    """The group-aware fallback for a child no session holds: _stop_child signals the group, not just the leader."""
    proc = room_launch._spawn([sys.executable, "-I", "-c", GRANDCHILD], cwd=str(world["fake"]), env=dict(os.environ),
                              stderr=subprocess.DEVNULL)
    grand = int(proc.stdout.readline())
    try:
        room_launch._stop_child(proc)
        assert _gone(grand), "_stop_child left a member of the child's group alive"
    finally:
        try:
            os.kill(grand, 9)
        except ProcessLookupError:
            pass


def test_r1_roomsession_still_accepts_the_old_child_keyword(world):
    room_launch.RoomSession(PipeTransport(subprocess.Popen([sys.executable, "-I", "-c", "pass"],
                                                           stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)),
                            child=None)


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


# --- roomfix1 C2: the effective read scope; C3: the child's environment ------------------------------------------------
def _paths(w, n=1):
    return json.loads((w["fake"] / f"canary-{n}.paths").read_text())


def test_c2_the_open_check_reads_a_rule_decoy_and_a_scope_decoy_outside_every_rule_and_removes_it(world):
    lr = launcher(world)
    lr.ensure()
    rule, scope = _paths(world)
    assert Path(rule).name == room_launch._CANARY_NAME
    assert Path(scope).name == room_launch._SCOPE_NAME and not Path(scope).name.startswith(".env")
    real = os.path.realpath(os.path.dirname(scope))
    for root in (world["cwd"], world["home"], world["run"]):
        r = os.path.realpath(root)
        assert not (real == r or real.startswith(r + "/")), root
    assert not os.path.exists(scope) and not os.path.exists(os.path.dirname(scope))     # removed after the open


def test_c2_a_child_whose_read_scope_is_wider_than_its_cwd_does_not_open(world):
    """Reproduced: the one canary matched the dotenv deny rule, so a child that could read outside its cwd (an
    additionalDirectories, or an allow rule that outranks the outside-cwd block) still passed. The scope decoy matches
    no rule; its Read returning content fails the open."""
    mode(world, "scope_open")
    with pytest.raises(RoomLaunchError, match="read scope wider than its cwd"):
        launcher(world).ensure()
    [launch] = world["cs"]._read(lambda conn: [r[0] for r in conn.execute(
        "SELECT launch_id FROM launches WHERE role = 'room'").fetchall()])
    assert state(world, launch) == "failed"
    assert not os.path.exists(os.path.dirname(_paths(world)[1]))


@pytest.mark.parametrize("where", ["user", "config_dir", "managed"])
def test_c2_settings_naming_additional_directories_refuse_the_open_before_any_spawn(world, tmp_path, monkeypatch, where):
    doc = json.dumps({"permissions": {"additionalDirectories": [str(tmp_path / "elsewhere")]}})
    extra = {}
    if where == "user":
        (world["home"] / ".claude").mkdir()
        (world["home"] / ".claude" / "settings.json").write_text(doc)
    elif where == "config_dir":
        cfg = tmp_path / "cfg"
        cfg.mkdir()
        (cfg / "settings.json").write_text(doc)
        extra = {"CLAUDE_CONFIG_DIR": str(cfg)}
    if where == "managed":
        sysdir = tmp_path / "sys"
        sysdir.mkdir()
        (sysdir / "managed-settings.json").write_text(doc)
        _point_owner_at(monkeypatch, sysdir)
    lr = RoomLauncher(world["cs"], world["svc"], RoomSetting(str(world["cwd"]), claude_bin=str(world["script"])),
                      run_dir=str(world["run"]), home=str(world["home"]), env_keep=("FAKE_DIR",),
                      environ={**os.environ, "FAKE_DIR": str(world["fake"]), **extra})
    world["launchers"].append(lr)
    with pytest.raises(RoomLaunchError, match="additionalDirectories"):
        lr.ensure()
    assert spawns(world) == []


@pytest.fixture(autouse=True)
def _no_machine_managed_settings(monkeypatch):
    """Nothing of this machine's managed settings reaches a test: the owner's seam points nowhere, and so does the room's
    own profile line; a test that wants one points them at its own."""
    monkeypatch.setattr(claude_code, "MANAGED_BASES", {})
    monkeypatch.setattr(room_launch, "MANAGED_PROFILES", ())


def _point_owner_at(monkeypatch, sysdir):
    """The owner's seam (``claude_code.MANAGED_BASES``) for both platforms, so the test holds on any host."""
    monkeypatch.setattr(claude_code, "MANAGED_BASES", {"darwin": sysdir, "linux": sysdir})


def test_c2_a_settings_file_that_cannot_be_read_refuses_and_a_missing_one_adds_nothing(tmp_path):
    home = tmp_path / "h"
    (home / ".claude").mkdir(parents=True)
    assert room_launch.settings_scope_refusal({}, str(home)) is None
    (home / ".claude" / "settings.json").write_text("{not json")
    assert "cannot be read" in room_launch.settings_scope_refusal({}, str(home))
    (home / ".claude" / "settings.json").write_text('{"permissions": {"allow": ["Read"], "additionalDirectories": []}}')
    assert room_launch.settings_scope_refusal({}, str(home)) is None


# --- roomfix2 K2: a relative CLAUDE_CONFIG_DIR; K3: path-specific allow rules ------------------------------------------
def _scope_launcher(world, extra_env):
    lr = RoomLauncher(world["cs"], world["svc"], RoomSetting(str(world["cwd"]), claude_bin=str(world["script"])),
                      run_dir=str(world["run"]), home=str(world["home"]), env_keep=("FAKE_DIR",),
                      environ={**os.environ, "FAKE_DIR": str(world["fake"]), **extra_env})
    world["launchers"].append(lr)
    return lr


def test_k2_a_relative_config_dir_is_refused_before_any_spawn(world, monkeypatch):
    """FINDINGS_room_r2 K2: a relative CLAUDE_CONFIG_DIR was joined against the SERVER's cwd, while the child (cwd = the
    room's DIR) loads DIR/<rel>/settings.json. Reproduced here: the file the child would load widens the scope and the
    server's cwd holds no such file, so the check passed."""
    (world["cwd"] / "cfg").mkdir()
    (world["cwd"] / "cfg" / "settings.json").write_text(json.dumps({"permissions": {"additionalDirectories": ["/"]}}))
    assert not os.path.exists(os.path.join(os.getcwd(), "cfg", "settings.json"))
    lr = _scope_launcher(world, {"CLAUDE_CONFIG_DIR": "cfg"})
    with pytest.raises(RoomLaunchError, match="CLAUDE_CONFIG_DIR 'cfg' is not an absolute path"):
        lr.ensure()
    assert spawns(world) == []
    for rel in ("~/cfg", "./cfg"):
        assert "not an absolute path" in room_launch.settings_scope_refusal({"CLAUDE_CONFIG_DIR": rel}, str(world["home"]),
                                                                            cwd=str(world["cwd"]))


def test_k2_an_empty_config_dir_is_unset_for_the_child_as_it_is_for_the_check():
    env = room_launch.room_env({"PATH": "/bin", "CLAUDE_CONFIG_DIR": ""})
    assert "CLAUDE_CONFIG_DIR" not in env
    assert room_launch.room_env({"CLAUDE_CONFIG_DIR": "/abs/cfg"})["CLAUDE_CONFIG_DIR"] == "/abs/cfg"


def test_k5_relative_and_empty_search_path_entries_do_not_reach_the_child():
    """FINDINGS_room_r2 K5: the child runs in the room's DIR, so '.', '' or 'bin' in PATH (or PYTHONPATH) would let a
    file in DIR stand in for claude (or levain's room door)."""
    env = room_launch.room_env({"PATH": "/bin:.:bin::/usr/bin", "PYTHONPATH": ":src:/abs/site"})
    assert env["PATH"] == "/bin:/usr/bin" and env["PYTHONPATH"] == "/abs/site"
    assert "PYTHONPATH" not in room_launch.room_env({"PATH": "/bin"})


_OUTSIDE = ["Read(//{other}/**)", "Read(~/private/**)", "Read(~)", "Grep(/notes/**)", "Glob(../sibling/**)",
            "Read(//**)", "Edit(//{other}/x)", "Read( //{other}/a*b )", "Read(//{cwd}-twin/**)"]
_INSIDE = ["Read", "Grep", "Read(**)", "Read(*)", "Read(./src/**)", "Read(src/*.py)", "Read(//{cwd}/**)",
           "Read(//{cwd})", "Bash(ls:*)", "WebFetch(domain:example.com)", "mcp__levain_room__drop"]


@pytest.fixture
def short_tmp():
    """A short base directory for a test whose assertion reads a refusal that names the path TWICE (the settings file and
    the rule). ``settings_scope_refusal`` caps its message at 400 characters (``_one_line``), so under pytest's default
    macOS base (``/private/var/folders/.../pytest-of-<user>/pytest-N/<test>``, ~116 characters before the test's own
    names) the tail that holds "outside its directory" is cut and the assertion reads a message that real use (an
    operator path of ~90 characters in all) never produces. The product still refuses: only the explanation is clipped."""
    base = "/tmp" if os.path.isdir("/tmp") else None
    d = Path(os.path.realpath(tempfile.mkdtemp(prefix="rl-", dir=base)))
    yield d
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def short_world(short_tmp):
    w = build(short_tmp)
    yield w
    teardown(w)


def _rule_world(tmp_path):
    home, cwd, other = tmp_path / "h", tmp_path / "proj", tmp_path / "other"
    for d in (home / ".claude", cwd / "src", other):
        d.mkdir(parents=True)
    return home, cwd, other


@pytest.mark.parametrize("rule", _OUTSIDE)
def test_k3_a_path_specific_file_allow_rule_outside_the_room_refuses(short_tmp, rule):
    """FINDINGS_room_r2 K3: the check saw only additionalDirectories; a path-specific Read allow outside the cwd widens
    the read scope while the one scope decoy (a fresh temp dir) can still be denied."""
    home, cwd, other = _rule_world(short_tmp)
    r = rule.format(other=str(other).lstrip("/"), cwd=str(cwd).lstrip("/"))
    (home / ".claude" / "settings.json").write_text(json.dumps({"permissions": {"allow": ["Read", r]}}))
    why = room_launch.settings_scope_refusal({}, str(home), cwd=str(cwd))
    assert why is not None and "allows" in why and "outside its directory" in why, (r, why)


@pytest.mark.parametrize("rule", _INSIDE)
def test_k3_a_bare_rule_another_tool_or_a_path_inside_the_room_adds_nothing(tmp_path, rule):
    home, cwd, _other = _rule_world(tmp_path)
    r = rule.format(cwd=str(cwd).lstrip("/"))
    (home / ".claude" / "settings.json").write_text(json.dumps({"permissions": {"allow": [r], "ask": ["Read(//**)"]}}))
    assert room_launch.settings_scope_refusal({}, str(home), cwd=str(cwd)) is None, r


def test_k3_a_managed_file_or_profile_with_an_outside_rule_refuses_and_allow_must_be_a_list(tmp_path, monkeypatch):
    import plistlib
    home, cwd, other = _rule_world(tmp_path)
    sysdir = tmp_path / "sys"
    sysdir.mkdir()
    (sysdir / "managed-settings.json").write_text(json.dumps({"permissions": {"allow": [f"Read(/{other}/**)"]}}))
    _point_owner_at(monkeypatch, sysdir)
    assert "allows" in room_launch.settings_scope_refusal({}, str(home), cwd=str(cwd))
    _point_owner_at(monkeypatch, tmp_path / "none")
    prof = tmp_path / "com.anthropic.claudecode.plist"
    monkeypatch.setattr(room_launch, "MANAGED_PROFILES", (str(prof),))
    prof.write_bytes(plistlib.dumps({"permissions": {"allow": [f"Read(/{other}/**)"]}}))
    assert "allows" in room_launch.settings_scope_refusal({}, str(home), cwd=str(cwd))
    prof.write_bytes(b"not a plist")
    assert "cannot be read" in room_launch.settings_scope_refusal({}, str(home), cwd=str(cwd))
    (home / ".claude" / "settings.json").write_text(json.dumps({"permissions": {"allow": "Read"}}))
    assert "not a list" in room_launch.settings_scope_refusal({}, str(home), cwd=str(cwd))


def test_k3_the_launcher_refuses_an_outside_rule_before_any_spawn(short_world, short_tmp, monkeypatch):
    world = short_world
    (world["home"] / ".claude").mkdir()
    (world["home"] / ".claude" / "settings.json").write_text(
        json.dumps({"permissions": {"allow": [f"Read(/{short_tmp}/elsewhere/**)"]}}))
    with pytest.raises(RoomLaunchError, match="outside its directory"):
        _scope_launcher(world, {}).ensure()
    assert spawns(world) == []


# --- roomfix3 R3-1: the managed-settings.d drop-ins; R3-2: an all-relative search path; the LOWs ------------------------
def _managed_dir(tmp_path):
    """A managed system directory holding NO managed-settings.json, only a drop-in directory (Claude Code loads the
    drop-ins alone: code.claude.com/docs/en/managed-settings.md, source label `(drop-ins)`)."""
    sysdir = tmp_path / "ClaudeCode"
    (sysdir / "managed-settings.d").mkdir(parents=True)
    return sysdir, sysdir


def test_r3_1_a_drop_in_alone_with_an_outside_rule_or_directory_refuses(tmp_path, monkeypatch):
    """FINDINGS_room_r3 R3-1: the room's hard-coded MANAGED_SETTINGS named three files; the drop-ins Claude Code merges after
    managed-settings.json (every *.json in managed-settings.d, even with no base file) were never read."""
    home, cwd, other = _rule_world(tmp_path)
    sysdir, managed = _managed_dir(tmp_path)
    _point_owner_at(monkeypatch, managed)
    drop = sysdir / "managed-settings.d" / "x.json"
    drop.write_text(json.dumps({"permissions": {"allow": [f"Read(/{other}/**)"]}}))
    why = room_launch.settings_scope_refusal({}, str(home), cwd=str(cwd))
    assert why is not None and "allows" in why and str(drop) in why, why
    drop.write_text(json.dumps({"permissions": {"additionalDirectories": [str(other)]}}))
    assert "additionalDirectories" in room_launch.settings_scope_refusal({}, str(home), cwd=str(cwd))


def test_r3_1_hidden_and_non_json_drop_ins_are_not_read_and_a_bad_drop_in_refuses(tmp_path, monkeypatch):
    """Claude Code ignores hidden files and files not ending in .json (the same page); a drop-in it would refuse to
    start on (not a JSON object) is refused here too."""
    home, cwd, other = _rule_world(tmp_path)
    sysdir, managed = _managed_dir(tmp_path)
    _point_owner_at(monkeypatch, managed)
    outside = json.dumps({"permissions": {"allow": [f"Read(/{other}/**)"]}})
    (sysdir / "managed-settings.d" / ".hidden.json").write_text(outside)
    (sysdir / "managed-settings.d" / "notes.txt").write_text(outside)
    (sysdir / "managed-settings.d" / "10-ok.json").write_text(json.dumps({"permissions": {"allow": ["Read"]}}))
    assert room_launch.settings_scope_refusal({}, str(home), cwd=str(cwd)) is None
    (sysdir / "managed-settings.d" / "20-bad.json").write_text("[1, 2]")
    assert "not a JSON object" in room_launch.settings_scope_refusal({}, str(home), cwd=str(cwd))


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads a 000 directory")
def test_r3_1_a_drop_in_directory_that_cannot_be_listed_refuses(tmp_path, monkeypatch):
    home, cwd, _other = _rule_world(tmp_path)
    sysdir, managed = _managed_dir(tmp_path)
    _point_owner_at(monkeypatch, managed)
    d = sysdir / "managed-settings.d"
    d.chmod(0)
    try:
        why = room_launch.settings_scope_refusal({}, str(home), cwd=str(cwd))
    finally:
        d.chmod(0o700)
    assert why is not None and "cannot be read" in why and str(d) in why, why


def test_r3_1_the_launcher_refuses_a_drop_in_rule_before_any_spawn(short_world, short_tmp, monkeypatch):
    world = short_world
    sysdir, managed = _managed_dir(short_tmp)
    _point_owner_at(monkeypatch, managed)
    (sysdir / "managed-settings.d" / "x.json").write_text(
        json.dumps({"permissions": {"allow": [f"Read(/{short_tmp}/elsewhere/**)"]}}))
    with pytest.raises(RoomLaunchError, match="outside its directory"):
        _scope_launcher(world, {}).ensure()
    assert spawns(world) == []


def test_c3_the_room_and_host_guard_read_the_managed_files_through_the_one_owner(tmp_path, monkeypatch):
    """The lane C3 class: two lists of the managed files (the room's own tuple and the owner's) drift. The room keeps no
    list of its own: patching ONLY the owner's seam changes what the room reads, and so does host_guard's."""
    from levain import host_guard
    home, cwd, other = _rule_world(tmp_path)
    sysdir, managed = _managed_dir(tmp_path)
    (sysdir / "managed-settings.d" / "x.json").write_text(
        json.dumps({"permissions": {"additionalDirectories": [str(other)]}}))
    assert not hasattr(room_launch, "MANAGED_SETTINGS") and not hasattr(room_launch, "_DROP_INS")
    assert room_launch.settings_scope_refusal({}, str(home), cwd=str(cwd)) is None     # the owner points nowhere
    _point_owner_at(monkeypatch, managed)
    why = room_launch.settings_scope_refusal({}, str(home), cwd=str(cwd))
    assert why is not None and "additionalDirectories" in why and str(sysdir / "managed-settings.d" / "x.json") in why
    assert [p for p, _doc in host_guard.load_managed_settings("linux")][-1] == str(sysdir / "managed-settings.d" / "x.json")


def test_c3_an_owner_listing_that_raises_refuses_the_room_by_name(tmp_path, monkeypatch):
    home, cwd, _other = _rule_world(tmp_path)

    def boom(*a, **k):
        raise RuntimeError("owner broke")

    monkeypatch.setattr(claude_code, "managed_settings_files", boom)
    why = room_launch.settings_scope_refusal({}, str(home), cwd=str(cwd))
    assert why is not None and "cannot be listed" in why and "RuntimeError" in why, why


def test_c3_the_launcher_refuses_before_any_spawn_when_the_owner_listing_raises(world, monkeypatch):
    def boom(*a, **k):
        raise OSError("owner broke")

    monkeypatch.setattr(claude_code, "managed_settings_files", boom)
    with pytest.raises(RoomLaunchError, match="cannot be listed"):
        _scope_launcher(world, {}).ensure()
    assert spawns(world) == []


def test_r3_2_an_all_relative_search_path_is_not_passed_as_the_empty_path():
    """FINDINGS_room_r3 R3-2: every entry relative left PATH="" (and PYTHONPATH=""), and an empty entry is the child's
    cwd, the room's DIR. The name is dropped instead, so the child gets its platform's default search path."""
    env = room_launch.room_env({"PATH": "bin:.:", "PYTHONPATH": "src::"})
    assert "PATH" not in env and "PYTHONPATH" not in env, env
    assert "" not in os.get_exec_path(env)


def test_r3_low_a_parent_segment_after_a_glob_counts_as_outside(short_tmp):
    """FINDINGS_room_r3 LOW: the literal-prefix cut stopped at the first glob, so `..` after it climbed out unseen."""
    home, cwd, _other = _rule_world(short_tmp)
    for r in (f"Read(/{cwd}/*/../../other/**)", "Read(src/**/../../../other/x)"):
        (home / ".claude" / "settings.json").write_text(json.dumps({"permissions": {"allow": [r]}}))
        assert "outside its directory" in (room_launch.settings_scope_refusal({}, str(home), cwd=str(cwd))
                                           or ""), r


def test_r3_low_a_malformed_xml_profile_refuses_rather_than_raising(tmp_path, monkeypatch):
    home, cwd, _other = _rule_world(tmp_path)
    prof = tmp_path / "com.anthropic.claudecode.plist"
    monkeypatch.setattr(room_launch, "MANAGED_PROFILES", (str(prof),))
    prof.write_bytes(b'<?xml version="1.0" encoding="UTF-8"?>\n<plist version="1.0"><dict><key>permissions</broken>')
    why = room_launch.settings_scope_refusal({}, str(home), cwd=str(cwd))
    assert why is not None and "cannot be read" in why, why


def test_r3_low_xdg_config_home_does_not_reach_the_child():
    """FINDINGS_room_r3 LOW: XDG_CONFIG_HOME was passed on but no file under it is checked; it is not passed, so the
    child's config is the one the check reads."""
    assert "XDG_CONFIG_HOME" not in room_launch.room_env({"PATH": "/bin", "XDG_CONFIG_HOME": "/elsewhere"})


def test_c3_the_child_gets_an_allowlisted_environment(world):
    secret = {"LEVAIN_TEST_SECRET": "s3cret", "AWS_SECRET_ACCESS_KEY": "x", "GITHUB_TOKEN": "y", "LC_ALL": "C"}
    lr = RoomLauncher(world["cs"], world["svc"], RoomSetting(str(world["cwd"]), claude_bin=str(world["script"])),
                      run_dir=str(world["run"]), home=str(world["home"]), env_keep=("FAKE_DIR",),
                      environ={**os.environ, "FAKE_DIR": str(world["fake"]), **secret})
    world["launchers"].append(lr)
    lr.ensure()
    got = set(json.loads((world["fake"] / "spawn-1.env").read_text()))
    assert not got & {"LEVAIN_TEST_SECRET", "AWS_SECRET_ACCESS_KEY", "GITHUB_TOKEN"}
    assert {"PATH", "LEVAIN_ROOM", "FAKE_DIR", "LC_ALL"} <= got
    env = room_launch.room_env({"PATH": "/bin", "HOME": "/h", "ANTHROPIC_API_KEY": "k", "OPENAI_API_KEY": "o",
                                "LEVAIN_HOME": "/lv", "LEVAIN_WRITE_TOKEN": "t"})
    assert env == {"PATH": "/bin", "HOME": "/h", "ANTHROPIC_API_KEY": "k", "LEVAIN_HOME": "/lv", "LEVAIN_ROOM": "1"}


# --- 1105-49: the room's own read-guard hook ---------------------------------------------------------------------
def _env_spy():
    """A spawn seam that keeps the environment each child was given."""
    envs = []

    def spawn(argv, *, cwd, env, stderr):
        envs.append(dict(env))
        return room_launch._spawn(argv, cwd=cwd, env=env, stderr=stderr)
    return envs, spawn


def test_1105_49_the_launch_writes_the_hook_script_and_a_settings_document_that_names_it(world):
    from levain.coordinator import room_readguard
    h = launcher(world).ensure()
    guard = Path(world["run"]) / "room_readguard.py"
    assert guard.read_bytes() == Path(room_readguard.__file__).read_bytes()
    assert stat.S_IMODE(guard.stat().st_mode) == 0o600 and not list(Path(world["run"]).glob(".room_readguard.*"))
    doc = json.loads((Path(world["run"]) / h.launch_id / "settings.json").read_text())
    assert doc["permissions"]["blockReadsOutsideWorkingDirectories"] is True       # the vendor's block stays beside it
    [entry] = doc["hooks"]["PreToolUse"]
    assert entry["matcher"] == "Read|Grep|Glob"
    [hook] = entry["hooks"]
    assert hook["command"] == f"{shlex.quote(sys.executable)} -I {shlex.quote(str(guard))}"


def test_1105_49_the_child_is_given_both_decoys_so_only_the_vendor_answers_the_canary(world):
    """Canary-masking (the design's first point): a hook deny is an ``is_error`` result AND a ``permission_denials``
    entry, so a hook that answered a decoy would pass the open check without the deny rules or the block holding. The
    child's environment names both decoys; the real hook, run with exactly that environment, prints nothing for them
    and still refuses an outside path."""
    from levain.coordinator import room_readguard
    envs, spawn = _env_spy()
    launcher(world, spawn=spawn).ensure()
    rule, scope = _paths(world)
    [env] = envs
    assert json.loads(env[room_readguard.EXEMPT_ENV]) == [rule, scope]
    for decoy in (rule, scope):
        ev = json.dumps({"tool_name": "Read", "tool_input": {"file_path": decoy}, "cwd": str(world["cwd"])})
        out = io.StringIO()
        assert room_readguard.main(io.StringIO(ev), out, env) == 0 and out.getvalue() == "", decoy
    ev = json.dumps({"tool_name": "Read", "tool_input": {"file_path": "/etc/hosts"}, "cwd": str(world["cwd"])})
    out = io.StringIO()
    room_readguard.main(io.StringIO(ev), out, env)
    assert room_readguard.ROOM_READ_REFUSAL in out.getvalue()


def test_1105_49_the_containment_digest_covers_the_hook_entry_and_its_script_and_is_stable_across_launches(world, monkeypatch):
    lr = launcher(world)
    lr.end(lr.ensure().launch_id, "test")
    lr.ensure()
    settings = lr._settings_json
    a = room_launch.containment_digest(settings)
    recorded = world["cs"]._read(lambda conn: [r[0] for r in conn.execute(
        "SELECT containment_digest FROM chat_launch_checks WHERE containment_digest IS NOT NULL ORDER BY seq").fetchall()])
    assert recorded == [a, a]                                                    # both launches ran under one policy
    doc = json.loads(settings)
    del doc["hooks"]
    assert room_launch.containment_digest(json.dumps(doc)) != a                  # no hook entry: another policy
    monkeypatch.setattr(room_launch, "_guard_source", lambda: b"print('another script')\n")
    assert room_launch.containment_digest(settings) != a                         # another script behind the same entry

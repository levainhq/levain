"""The launcher's host profile (levain 23, key 1112-40, lane ROOM-P1C): ``RoomSetting.profile`` opens the room the way a
host that accepts the operator's full reach wants it (``levain.desk.room_profile``), and ``profile=None`` is the
confined room, unchanged (the existing launcher, serve and tripwire suites are that proof; the pins here repeat the
few places the two diverge).

NOTHING here runs a real Claude child: the frames are a fake ``claude`` over real pipes, shaped by the ROOM-D0
measurements of the vendor under ``--dangerously-skip-permissions`` (design page, section 0: every accepted call is
``{accept, config, mode}``, ``init`` reports ``permissionMode: bypassPermissions`` and the vendor's default tool set).
The first real bypass child meets this code in lane V."""
from __future__ import annotations

import hashlib
import json
import os
import shlex
import stat
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from levain.desk import chat, room, room_launch, room_readguard
from levain.desk.chat import ChatService, ChatStore, liveness
from levain.desk.claude_session import ROOM_MCP_TOOL, room_host_argv
from levain.desk.driver import LivenessEvidence
from levain.desk.envelopes import EnvelopeStore
from levain.desk.room_launch import (ROOM_HOST_OPEN_PROMPT, RoomLauncher, RoomLaunchError, RoomSetting, containment_digest,
                                     make_probe, make_tripwires, room_probe, room_tripwire_context)
from levain.desk.room_profile import HookSpec, RoomProfile, host_policy
from levain.desk.room_serve import RoomServe
from levain.desk.store import Part

# What a caller that names NO profile gets today (the confined room), in ONE place so a change of what the unnamed case
# means is a change here: the profile value, the permission mode the serve expects, and what ``status()["profile"]`` says.
# Every test below that pins "no profile named" reads these; the tests that open the confined room on purpose pass
# ``EXPLICIT_CONFINED`` (the profile=None spelling of "the confined room").
CONFINED_DEFAULT = None
CONFINED_DEFAULT_MODE = "dontAsk"
CONFINED_DEFAULT_STATUS_PROFILE = None
CONFINED_DEFAULT_OPEN_TOOLS = False
EXPLICIT_CONFINED = None
T0 = datetime(2026, 11, 12, 21, 0, tzinfo=timezone.utc)
BYPASS = "bypassPermissions"
HOOK_MATCHER = "Bash|Agent|Task"

FAKE_HOST = r'''#!{python}
import json, os, sys, time, uuid

argv = sys.argv[1:]
def flag(name):
    return argv[argv.index(name) + 1] if name in argv else None

base = os.environ["FAKE_DIR"]
n = len([f for f in os.listdir(base) if f.startswith("spawn-") and f.endswith(".argv")]) + 1
open(os.path.join(base, "spawn-%d.argv" % n), "w").write(json.dumps(argv))
open(os.path.join(base, "spawn-%d.env" % n), "w").write(json.dumps(sorted(os.environ)))
mode = open(os.path.join(base, "mode")).read().split() if os.path.exists(os.path.join(base, "mode")) else ["ok"]
mode = mode[0]
sid = flag("--session-id") or flag("--resume")
if mode == "stale_sid": sid = "not-the-sid-it-was-given"
CFG_MODE = {{"decision": "accept", "source": "config", "reason_type": "mode"}}
def out(f):
    f.setdefault("session_id", sid); f.setdefault("uuid", str(uuid.uuid4()))
    sys.stdout.write(json.dumps(f) + "\n"); sys.stdout.flush()
def init():
    tools = ["Task", "Bash", "Edit", "Read", "Skill", "WebSearch", "Write", "mcp__levain_room__propose_drop"]
    if mode == "no_bash": tools.remove("Bash")
    servers = [{{"name": "levain_room", "status": "connected"}}]
    if mode == "two_servers": servers.append({{"name": "iterm-mcp", "status": "connected"}})
    cwd = os.getcwd()
    if mode == "wrong_cwd": cwd = os.path.dirname(cwd)
    pm = {{"dont_ask": "dontAsk", "bad_mode": "default"}}.get(mode, "bypassPermissions")
    out({{"type": "system", "subtype": "init", "cwd": cwd, "tools": tools, "mcp_servers": servers, "model": "fake-model",
         "permissionMode": pm, "claude_code_version": "2.1.323"}})
def tool(tid, name, inp, body, decision):
    out({{"type": "assistant", "message": {{"role": "assistant", "content": [
        {{"type": "tool_use", "id": tid, "name": name, "input": inp}}]}}}})
    out({{"type": "user", "message": {{"role": "user", "content": [
        {{"type": "tool_result", "tool_use_id": tid, "content": body}}]}},
         "tool_result_meta": [{{"id": tid, "permission_decision": decision}}]}})
def result():
    out({{"type": "result", "subtype": "success", "is_error": False, "terminal_reason": "completed",
         "permission_denials": [], "num_turns": 1}})
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
    open(os.path.join(base, "prompt-%d-%d.txt" % (n, inits)), "w").write(text)
    init()
    if text.startswith("Open check"):
        say("ok"); result()
        continue
    if text.startswith("bash"):
        tool("toolu_b%d" % inits, "Bash", {{"command": "echo hi"}}, "hi", CFG_MODE)
        tool("toolu_w%d" % inits, "Write", {{"file_path": "x"}}, "wrote", CFG_MODE)
    say("echo: " + text[:40]); result()
'''

HOOK_BODY = "import sys\nsys.exit(0)\n"


class Clock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        self.now += timedelta(seconds=1)
        return self.now


def make_profile(tmp_path, **kw) -> RoomProfile:
    script = tmp_path / "host_guard.py"
    if not script.exists():
        script.write_text(HOOK_BODY)
    kw.setdefault("hooks", (HookSpec(HOOK_MATCHER, str(script)),))
    kw.setdefault("system_prompt", "You are the room.")
    kw.setdefault("setting_sources", ("user", "project", "local"))
    kw.setdefault("effort", "medium")
    kw.setdefault("max_turns", 40)
    kw.setdefault("turn_budget_usd", 5.0)
    kw.setdefault("recycle_at_usd", 1.0)
    return RoomProfile(**kw)


def build(tmp_path, profile="default"):
    consent = tmp_path / "consent"
    consent.mkdir(mode=0o700)
    store = EnvelopeStore(consent, clock=Clock())
    store.coord_set_agent("none")
    cs = ChatStore(store)
    cs._connect().close()
    d = store.accept_drop("the room", surface="cli")[0]
    thread = store.split(d.drop_id, [Part("note", "the room")])[0].thread_id
    with store._write() as (conn, _now):
        room.open_room_thread(conn, thread)
    fake = tmp_path / "fake"
    fake.mkdir()
    script = tmp_path / "fake_claude"
    script.write_text(FAKE_HOST.format(python=sys.executable))
    script.chmod(0o700)
    cwd, home = tmp_path / "cwd", tmp_path / "home"
    cwd.mkdir()
    home.mkdir()
    prof = make_profile(tmp_path) if profile == "default" else profile
    svc = ChatService(cs, probe=make_probe(cs, profile=prof), tripwires=make_tripwires(cs, profile=prof),
                      sleep=lambda _s: None,
                      **({"room_permission_mode": prof.permission_mode} if prof is not None else {}))
    return {"store": store, "cs": cs, "thread": thread, "svc": svc, "fake": fake, "cwd": cwd, "home": home,
            "run": tmp_path / "run", "script": script, "profile": prof, "launchers": [], "tmp": tmp_path}


@pytest.fixture
def world(tmp_path):
    w = build(tmp_path)
    yield w
    for lr in w["launchers"]:
        lr.close()
    w["svc"].close()


def mode(w, text="ok") -> None:
    (w["fake"] / "mode").write_text(text)


def launcher(w, *, profile="world", spawn=None, **kw) -> RoomLauncher:
    prof = w["profile"] if profile == "world" else profile
    extra = {} if spawn is None else {"spawn": spawn}
    lr = RoomLauncher(w["cs"], w["svc"], RoomSetting(str(w["cwd"]), model="sonnet", claude_bin=str(w["script"]),
                                                     profile=prof),
                      run_dir=str(w["run"]), home=str(w["home"]), now=lambda: T0,
                      environ={**os.environ, "FAKE_DIR": str(w["fake"])}, env_keep=("FAKE_DIR",), **extra, **kw)
    w["launchers"].append(lr)
    return lr


def argv_spy():
    """A spawn seam keeping each child's argv and env, and (``spawn.settings``) the settings text it was given: a
    launch's workdir is removed when the launch ends, so a later read of that file would find nothing."""
    seen = []

    def spawn(argv, *, cwd, env, stderr):
        seen.append((list(argv), dict(env)))
        spawn.settings.append(Path(argv[argv.index("--settings") + 1]).read_text())
        return room_launch._spawn(argv, cwd=cwd, env=env, stderr=stderr)
    spawn.settings = []
    return seen, spawn


def spawns(w) -> list:
    return [json.loads(p.read_text()) for p in sorted(w["fake"].glob("spawn-*.argv"), key=lambda p: int(p.stem[6:]))]


def state(w, launch):
    return w["cs"]._read(lambda conn: chat._launch_state(conn, launch)[1])


def digests(w) -> list:
    return w["cs"]._read(lambda conn: [r[0] for r in conn.execute(
        "SELECT containment_digest FROM chat_launch_checks WHERE containment_digest IS NOT NULL ORDER BY seq").fetchall()])


def hook_copy(settings_text) -> Path:
    doc = json.loads(settings_text)
    return Path(shlex.split(doc["hooks"]["PreToolUse"][0]["hooks"][0]["command"])[-1])


def pump(w, launch, n=40):
    total = 0
    for _ in range(n):
        got = w["svc"].read_next(launch)
        total += got
        if not got and total:
            break
        if not got:
            time.sleep(0.05)
    return total


def say(w, body, *, outbox):
    return w["svc"].send_room(w["thread"], author_surface="cockpit", client_id="tab-a", outbox_id=outbox, body=body)


# --- the setting ------------------------------------------------------------------------------------------------------
def test_the_setting_defaults_to_no_profile_and_takes_only_a_room_profile(tmp_path):
    assert RoomSetting(str(tmp_path)).profile is CONFINED_DEFAULT
    for bad in ("host", {"name": "host"}, 1):
        with pytest.raises(ValueError, match="RoomProfile"):
            RoomSetting(str(tmp_path), profile=bad)


# --- the open ---------------------------------------------------------------------------------------------------------
def test_a_host_profile_opens_a_live_launch_with_the_host_argv_and_files(world):
    seen, spawn = argv_spy()
    lr = launcher(world, spawn=spawn)
    h = lr.ensure()
    assert (h.room_thread, h.resumed) == (world["thread"], False)
    assert state(world, h.launch_id) == "active"
    assert world["cs"]._read(lambda conn: liveness(conn, h.launch_id)).enabled
    [(argv, env)] = seen
    workdir = Path(world["run"]) / h.launch_id
    want = room_host_argv(world["profile"], mcp_config=str(workdir / "mcp.json"), settings=str(workdir / "settings.json"),
                          model="sonnet", session_id=h.harness_session_id, claude_bin=str(world["script"]))
    assert argv == want
    for narrowing in ("--tools", "--allowedTools", "--disallowedTools", "--permission-mode", "--add-dir"):
        assert narrowing not in argv
    assert "--dangerously-skip-permissions" in argv and "--strict-mcp-config" in argv
    assert argv[argv.index("--setting-sources") + 1] == "user,project,local"
    meta = world["svc"].session_meta(h.launch_id)
    assert meta.permission_mode == BYPASS
    settings = json.loads((workdir / "settings.json").read_text())
    assert list(settings) == ["hooks"]                                  # no permissions block, no deny rule, no read block
    assert settings["hooks"]["PreToolUse"][0]["matcher"] == HOOK_MATCHER
    assert not (workdir / "decoy").exists()                              # no decoy files
    assert room_readguard.EXEMPT_ENV not in env                          # the read guard's exemption is a confined-room thing
    assert env.get("LEVAIN_ROOM") == "1"                                 # the allowlisted environment still applies
    assert stat.S_IMODE(workdir.stat().st_mode) == 0o700


def test_the_launch_records_the_profiles_model_over_the_settings(tmp_path):
    w = build(tmp_path, profile=make_profile(tmp_path, model="claude-opus-5-5"))
    try:
        seen, spawn = argv_spy()
        lr = launcher(w, spawn=spawn)
        h = lr.ensure()
        argv = seen[0][0]
        assert argv[argv.index("--model") + 1] == "claude-opus-5-5"
        row = w["cs"]._read(lambda conn: conn.execute("SELECT model FROM launches WHERE launch_id = ?",
                                                      (h.launch_id,)).fetchone())
        assert row == ("claude-opus-5-5",)
    finally:
        for lr in w["launchers"]:
            lr.close()
        w["svc"].close()


def test_the_hook_runs_a_copy_at_a_stable_path_in_run_dir_named_by_its_sha(world):
    seen, spawn = argv_spy()
    lr = launcher(world, spawn=spawn)
    lr.end(lr.ensure().launch_id, "test")
    lr.ensure()
    first, second = (hook_copy(t) for t in spawn.settings)
    data = (world["tmp"] / "host_guard.py").read_bytes()
    sha = hashlib.sha256(data).hexdigest()
    assert first == second == Path(world["run"]) / f"room_hook-{sha[:16]}.py"      # not under a per-launch directory
    assert first.read_bytes() == data and stat.S_IMODE(first.stat().st_mode) == 0o600


def test_the_hook_command_is_isolated_python_on_the_copy(world):
    seen, spawn = argv_spy()
    launcher(world, spawn=spawn).ensure()
    doc = json.loads(spawn.settings[0])
    parts = shlex.split(doc["hooks"]["PreToolUse"][0]["hooks"][0]["command"])
    assert parts[1] == "-I" and parts[0] == sys.executable and doc["hooks"]["PreToolUse"][0]["hooks"][0]["timeout"] == 10


def test_a_host_open_sends_the_host_open_turn_not_the_canary(world):
    launcher(world).ensure()
    [prompt] = [p.read_text() for p in world["fake"].glob("prompt-1-*.txt")]
    assert prompt == ROOM_HOST_OPEN_PROMPT and prompt != room_launch.CANARY_PROMPT.split("{")[0]
    assert ".env.levain-room-canary" not in prompt and room_launch._SCOPE_NAME not in prompt


def test_the_host_open_prompt_asks_for_one_word_and_no_tool():
    assert ROOM_HOST_OPEN_PROMPT.startswith("Open check")
    assert "single word ok" in ROOM_HOST_OPEN_PROMPT and "no tool" in ROOM_HOST_OPEN_PROMPT
    assert "{" not in ROOM_HOST_OPEN_PROMPT                                  # no placeholder: nothing is formatted into it


def test_a_host_open_skips_the_scope_refusal_and_the_decoys(world):
    (world["home"] / ".claude").mkdir()
    (world["home"] / ".claude" / "settings.json").write_text(json.dumps({"permissions": {"additionalDirectories": ["/tmp"]}}))
    lr = launcher(world)                                                    # the host profile has no read scope to widen
    assert state(world, lr.ensure().launch_id) == "active"
    with pytest.raises(RoomLaunchError, match="additionalDirectories"):      # the confined room still refuses it
        launcher(world, profile=EXPLICIT_CONFINED).ensure(fresh=True)


def test_the_host_open_check_judges_the_init_by_the_profile(world):
    for text, why in (("bad_mode", "permissionMode"), ("dont_ask", "permissionMode"), ("no_bash", "Bash"),
                      ("two_servers", "exactly one MCP server"), ("wrong_cwd", "cwd")):
        mode(world, text)
        with pytest.raises(RoomLaunchError, match=why):
            launcher(world).ensure(fresh=True)


def test_the_host_open_check_holds_the_child_to_the_session_id_it_was_given(world):
    mode(world, "stale_sid")
    with pytest.raises(RoomLaunchError, match="session id"):
        launcher(world).ensure()
    mode(world, "ok")
    assert launcher(world).ensure(fresh=True).harness_session_id


def test_a_failed_host_open_leaves_the_launch_failed_and_nothing_attached(world):
    mode(world, "bad_mode")
    lr = launcher(world)
    with pytest.raises(RoomLaunchError):
        lr.ensure()
    [row] = world["cs"]._read(lambda conn: conn.execute("SELECT launch_id FROM launches WHERE role = 'room'").fetchall())
    assert state(world, row[0]) != "active" and not lr._live


def test_a_host_open_does_not_spawn_when_the_hook_script_vanished_after_the_profile_was_built(world):
    lr = launcher(world)                                                    # built while the script still existed
    (world["tmp"] / "host_guard.py").unlink()
    with pytest.raises(RoomLaunchError, match="not a readable file"):       # HookSpec.check's own words, at the open
        lr.ensure()
    assert spawns(world) == []


def test_a_host_open_refuses_when_the_copied_script_changes_before_the_spawn(world, monkeypatch):
    real = RoomLauncher._write_host_files

    def tampering(self, *a, **kw):
        got = real(self, *a, **kw)
        for p in Path(world["run"]).glob("room_hook-*.py"):
            p.write_text("import sys\nsys.exit(2)\n")
        return got
    monkeypatch.setattr(RoomLauncher, "_write_host_files", tampering)
    with pytest.raises(RoomLaunchError, match="no longer holds the bytes"):
        launcher(world).ensure()
    assert spawns(world) == []


def test_a_launcher_refuses_a_profile_whose_script_is_gone_at_construction(world):
    (world["tmp"] / "host_guard.py").unlink()
    with pytest.raises(RoomLaunchError, match="not a readable file"):
        launcher(world)


def test_a_profile_with_no_hooks_opens_with_empty_settings_and_a_digest(tmp_path):
    w = build(tmp_path, profile=make_profile(tmp_path, hooks=()))
    try:
        seen, spawn = argv_spy()
        lr = launcher(w, spawn=spawn)
        h = lr.ensure()
        argv = seen[0][0]
        assert json.loads(Path(argv[argv.index("--settings") + 1]).read_text()) == {}
        assert state(w, h.launch_id) == "active" and len(digests(w)) == 1
    finally:
        for lr in w["launchers"]:
            lr.close()
        w["svc"].close()


# --- the digest -------------------------------------------------------------------------------------------------------
def test_the_digest_is_the_host_policy_and_equal_across_two_launches(world):
    seen, spawn = argv_spy()
    lr = launcher(world, spawn=spawn)
    lr.end(lr.ensure().launch_id, "test")
    lr.ensure()
    settings = spawn.settings[1]
    sha = hashlib.sha256((world["tmp"] / "host_guard.py").read_bytes()).hexdigest()
    a = containment_digest(settings, "", None, profile=world["profile"], hook_sha256=[sha])
    assert digests(world) == [a, a]                                         # one policy, both launches
    policy = host_policy(world["profile"], settings_json=settings, hook_sha256=[sha], door=["propose_drop"])
    assert a == hashlib.sha256(json.dumps(policy, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def test_the_digest_moves_with_the_profile_the_hook_bytes_and_the_prompt(tmp_path):
    base = []
    for label, edit in (("same", None), ("prompt", "prompt"), ("bytes", "bytes"), ("name", "name"), ("sources", "sources")):
        sub = tmp_path / label
        sub.mkdir()
        kw = {}
        if edit == "prompt":
            kw["system_prompt"] = "You are another room."
        if edit == "name":
            kw["name"] = "other"
        if edit == "sources":
            kw["setting_sources"] = ("user",)
        prof = make_profile(sub, **kw)
        if edit == "bytes":
            (sub / "host_guard.py").write_text(HOOK_BODY + "# changed\n")
            prof = make_profile(sub, **kw)
        w = build(sub, profile=prof)
        try:
            launcher(w).ensure()
            base.append((label, digests(w)[0]))
        finally:
            for lr in w["launchers"]:
                lr.close()
            w["svc"].close()
    got = dict(base)
    assert len(set(got.values())) == len(got)                                # five policies, five digests


def test_the_confined_digest_and_a_host_digest_never_collide_and_the_confined_form_is_unchanged():
    settings = json.dumps({"permissions": {"deny": [], "blockReadsOutsideWorkingDirectories": True}})
    a = containment_digest(settings, "0" * 64)
    assert a == containment_digest(settings, "0" * 64, None)
    assert a == containment_digest(settings, "0" * 64, None, profile=EXPLICIT_CONFINED, hook_sha256=())


def test_the_host_digest_binds_the_hosts_command_table_and_its_door(world):
    from levain.desk.room_host import HostCommands
    sha = hashlib.sha256((world["tmp"] / "host_guard.py").read_bytes()).hexdigest()
    plain = containment_digest("{}", "", None, profile=world["profile"], hook_sha256=[sha])
    relayed = containment_digest("{}", "", HostCommands(relay=("/a",), relay_without_tap=True),
                                 profile=world["profile"], hook_sha256=[sha])
    other = containment_digest("{}", "", HostCommands(relay=("/b",), relay_without_tap=True),
                               profile=world["profile"], hook_sha256=[sha])
    assert len({plain, relayed, other}) == 3


def test_a_host_digest_needs_one_hook_sha_per_hook(world):
    settings = "{}"
    with pytest.raises(ValueError, match="one hook sha256"):
        containment_digest(settings, "", None, profile=world["profile"], hook_sha256=())


# --- the probe, the tripwire context, the mode ------------------------------------------------------------------------
BYPASS_INIT = {"permissionMode": BYPASS, "cwd": "/w", "tools": ["Bash", "Read", "Edit", "Write", "Glob", "TodoWrite",
                                                               "mcp__levain_room__propose_drop"],
               "mcp_servers": [{"name": "levain_room", "status": "connected"}]}


def test_room_probe_takes_the_profile_and_judges_the_bypass_init(tmp_path):
    prof = make_profile(tmp_path)
    ev = LivenessEvidence("claude init", dict(BYPASS_INIT), "t")
    assert room_probe("L", ev, "/w", None, prof).live is True
    assert not room_probe("L", ev, "/w", None, None).live                    # the confined probe wants dontAsk and its exact set
    assert not room_probe("L", ev, "/elsewhere", None, prof).live            # the cwd is still held


def test_the_rooms_tripwire_context_is_open_only_for_the_host_profile(tmp_path):
    prof = make_profile(tmp_path)
    assert room_tripwire_context("/w").open_tools is CONFINED_DEFAULT_OPEN_TOOLS
    assert room_tripwire_context("/w", None, EXPLICIT_CONFINED).open_tools is False
    ctx = room_tripwire_context("/w", None, prof)
    assert ctx.open_tools is True and ctx.rule_allow == frozenset()
    with pytest.raises(ValueError):
        ctx.expected_pre_hooks("Bash")                                       # no count is ever owed on an open pass


def test_a_host_room_runs_bash_and_write_under_bypass_without_latching(world):
    lr = launcher(world)
    h = lr.ensure()
    out = say(world, "bash please", outbox="ob-00000001")
    assert out is not None
    pump(world, h.launch_id)
    fold = world["cs"]._read(lambda conn: liveness(conn, h.launch_id))
    assert fold.enabled, fold.why_not
    kinds = world["cs"]._read(lambda conn: [r[0] for r in conn.execute(
        "SELECT kind FROM chat_events WHERE launch_id = ? ORDER BY gseq", (h.launch_id,)).fetchall()])
    assert "tool_call" in kinds


def test_the_same_run_under_the_confined_context_latches(tmp_path):
    w = build(tmp_path)
    try:
        # a service built with NO profile (the confined room's) judges the same bypass accepts
        w["svc"].close()
        w["svc"] = ChatService(w["cs"], probe=lambda _l, _e: chat.Verdict(True, "test probe"),
                               tripwires=make_tripwires(w["cs"]), sleep=lambda _s: None, room_permission_mode=BYPASS)
        lr = launcher(w)
        h = lr.ensure()
        say(w, "bash please", outbox="ob-00000002")
        pump(w, h.launch_id)
        assert not w["cs"]._read(lambda conn: liveness(conn, h.launch_id)).enabled
        sources = w["cs"]._read(lambda conn: [r[0] for r in conn.execute(
            "SELECT source FROM chat_launch_checks WHERE launch_id = ? AND kind = 'not_live'", (h.launch_id,)).fetchall()])
        assert "tripwire:deficit" in sources                                  # the confined context reads bypass accepts as a deficit
    finally:
        for lr in w["launchers"]:
            lr.close()
        w["svc"].close()


def test_a_service_expecting_dontask_latches_a_host_room(tmp_path):
    w = build(tmp_path)
    try:
        w["svc"].close()
        w["svc"] = ChatService(w["cs"], probe=make_probe(w["cs"], profile=w["profile"]),
                               tripwires=make_tripwires(w["cs"], profile=w["profile"]), sleep=lambda _s: None)
        with pytest.raises(RoomLaunchError, match="turned off by its own open check"):
            launcher(w).ensure()                                              # the mode line latches the open itself
    finally:
        for lr in w["launchers"]:
            lr.close()
        w["svc"].close()


# --- resume -----------------------------------------------------------------------------------------------------------
def test_a_host_launch_resumes_the_newest_conversation_with_the_same_argv_shape(world):
    seen, spawn = argv_spy()
    lr = launcher(world, spawn=spawn)
    a = lr.ensure()
    lr.end(a.launch_id, "test")
    b = lr.ensure()
    argv = seen[1][0]
    assert b.resumed and argv[argv.index("--resume") + 1] == a.harness_session_id and "--session-id" not in argv
    assert "--dangerously-skip-permissions" in argv and "--append-system-prompt" in argv


# --- the serve --------------------------------------------------------------------------------------------------------
def test_the_serve_expects_the_profiles_mode_and_names_it_in_status(tmp_path):
    store = EnvelopeStore(_consent(tmp_path), clock=Clock())
    store.coord_set_agent("none")
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    prof = make_profile(tmp_path)
    rs = RoomServe(store, RoomSetting(str(cwd), profile=prof), run_dir=str(tmp_path / "run"))
    try:
        assert rs.service._room_mode == BYPASS
        assert rs.status()["profile"] == "host"
        assert rs.launcher._setting.profile is prof
    finally:
        rs.stop()


def test_the_serve_without_a_profile_is_the_confined_room(tmp_path):
    store = EnvelopeStore(_consent(tmp_path), clock=Clock())
    store.coord_set_agent("none")
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    rs = RoomServe(store, RoomSetting(str(cwd)), run_dir=str(tmp_path / "run"))
    try:
        assert rs.service._room_mode == CONFINED_DEFAULT_MODE
        assert rs.status()["profile"] == CONFINED_DEFAULT_STATUS_PROFILE
        assert {"last_error", "running", "drops_via"} <= set(rs.status())
    finally:
        rs.stop()


def test_a_serve_probe_and_tripwires_are_the_profiles(tmp_path):
    store = EnvelopeStore(_consent(tmp_path), clock=Clock())
    store.coord_set_agent("none")
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    rs = RoomServe(store, RoomSetting(str(cwd), profile=make_profile(tmp_path)), run_dir=str(tmp_path / "run"))
    try:
        rs.chat_store._connect().close()
        d = store.accept_drop("the room", surface="cli")[0]
        thread = store.split(d.drop_id, [Part("note", "the room")])[0].thread_id
        with store._write() as (conn, _now):
            room.open_room_thread(conn, thread)
        from levain.desk import lease
        with store._write() as (conn, now):
            ln = lease.open_room_launch(conn, now, harness="claude", model=None, cwd=str(cwd), room_thread=thread,
                                        pid=os.getpid())
        assert rs.service._tripwires(ln.launch_id).open_tools is True
        ev = LivenessEvidence("claude init", {**BYPASS_INIT, "cwd": str(cwd)}, "t")
        assert rs.service._probe(ln.launch_id, ev).live is True
    finally:
        rs.stop()


def _consent(tmp_path):
    d = tmp_path / "consent"
    d.mkdir(mode=0o700)
    return d


def test_build_room_serve_passes_the_profile_into_the_setting(tmp_path, monkeypatch):
    from levain import web_server
    import inspect
    assert "profile" in inspect.signature(web_server._build_room_serve).parameters
    monkeypatch.setenv("HOME", str(tmp_path / "h"))
    (tmp_path / "h").mkdir()
    monkeypatch.setenv("LEVAIN_HOME", str(tmp_path / "lh"))
    proj = tmp_path / "proj"
    proj.mkdir()
    prof = make_profile(tmp_path)
    rs = web_server._build_room_serve(proj, profile=prof)
    try:
        assert rs.status()["profile"] == "host" and rs.service._room_mode == BYPASS
    finally:
        rs.stop()
    # np-97fa3dd6 (a): the PRODUCT default is the host profile; the confined room is the explicit opt-in (ROOMFLIP)
    rs = web_server._build_room_serve(proj)
    try:
        assert rs.status()["profile"] == "host" and rs.service._room_mode == BYPASS
    finally:
        rs.stop()
    from levain.desk.room_default import CONFINED
    rs = web_server._build_room_serve(proj, profile=CONFINED)
    try:
        assert rs.status()["profile"] == CONFINED_DEFAULT_STATUS_PROFILE and rs.service._room_mode == CONFINED_DEFAULT_MODE
    finally:
        rs.stop()

"""The L3 deploy round 1 fixes on the final Room shape (levain 23, key 1112-40, lane ROOMFIX4).

1. The hook-disable class: (a) the open refuses while a settings file the child loads turns hooks off
   (``disableAllHooks`` anywhere it loads, ``allowManagedHooksOnly`` in managed settings); (b) every host hook entry
   is rendered with ``"onFailure": "block"`` so a hook that cannot start or times out blocks the call; (c) the turn
   guard DENIES a Bash call whose decision raises (a traceback was a non-blocking error: the call ran unguarded).
2. A host room does not resume a conversation whose first system prompt is not the profile's.
3. A host room's tripwire names an unexpected accept by its own branch, not as a "count unknown" deficit.
4. ``required_tools`` is in the host policy; ``_abandon`` signals only a watchdog that is still running; the
   ``--room-confined`` refusal exits 1 like every other ``--room`` refusal; an ImportError building the room refuses.

NOTHING here runs a real Claude child (the P1C fake over real pipes); lane V owns that."""
from __future__ import annotations

import hashlib
import json
import os
import plistlib
import subprocess
import sys

import pytest

from levain.consent.adapters import claude_code
from levain.desk import room_host, room_launch, room_turn_guard
from levain.desk.chat_tripwires import TripwireWindow
from levain.desk.room_default import GUARD_PATH, ROOM_TURN_GUARD_SHA256
from levain.desk.room_launch import RoomLaunchError, containment_digest, hooks_off_refusal
from levain.desk.room_profile import HookSpec, RoomProfile, host_policy, host_settings_json

from tests.test_room_host_launch import (argv_spy, build, digests, launcher, make_profile, spawns,  # noqa: F401
                                         world)
from tests.test_room_open_tools_tripwire import _call_and_result, _ctx

ALL = ("user", "project", "local")


@pytest.fixture(autouse=True)
def no_machine_policy(tmp_path, monkeypatch):
    """The machine's own managed settings are not this suite's: every test reads a managed base under tmp_path."""
    base = tmp_path / "managed"
    base.mkdir()
    monkeypatch.setattr(claude_code, "MANAGED_BASES", {"darwin": base, "linux": base})
    monkeypatch.setattr(room_launch, "MANAGED_PROFILES", (str(tmp_path / "managed" / "profile.plist"),))
    return base


def _put(path, doc):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc))
    return path


# --- 1(a) the open refuses while hooks are turned off -------------------------------------------------------------------
def test_no_settings_at_all_is_no_refusal(tmp_path):
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    assert hooks_off_refusal({}, str(tmp_path / "home"), str(cwd), ALL) is None


@pytest.mark.parametrize("where", ["user", "config_dir", "project", "local", "project_ancestor", "local_ancestor"])
def test_disable_all_hooks_in_a_loaded_file_refuses_naming_the_file_and_the_key(tmp_path, where):
    home, cwd = tmp_path / "home", tmp_path / "repo" / "sub"
    cwd.mkdir(parents=True)
    env = {}
    path = {"user": home / ".claude" / "settings.json",
            "config_dir": tmp_path / "cfg" / "settings.json",
            "project": cwd / ".claude" / "settings.json",
            "local": cwd / ".claude" / "settings.local.json",
            "project_ancestor": tmp_path / "repo" / ".claude" / "settings.json",
            "local_ancestor": tmp_path / "repo" / ".claude" / "settings.local.json"}[where]
    if where == "config_dir":
        env["CLAUDE_CONFIG_DIR"] = str(tmp_path / "cfg")
    _put(path, {"disableAllHooks": True})
    why = hooks_off_refusal(env, str(home), str(cwd), ALL)
    assert why is not None and str(path) in why and "disableAllHooks" in why and "turn guard" in why


@pytest.mark.parametrize("source, name", [("user", None), ("project", "settings.json"),
                                          ("local", "settings.local.json")])
def test_a_source_the_profile_does_not_load_is_not_read(tmp_path, source, name):
    home, cwd = tmp_path / "home", tmp_path / "cwd"
    cwd.mkdir()
    path = home / ".claude" / "settings.json" if name is None else cwd / ".claude" / name
    _put(path, {"disableAllHooks": True})
    others = tuple(s for s in ALL if s != source)
    assert hooks_off_refusal({}, str(home), str(cwd), others) is None
    assert hooks_off_refusal({}, str(home), str(cwd), ALL) is not None


@pytest.mark.parametrize("key", ["disableAllHooks", "allowManagedHooksOnly"])
@pytest.mark.parametrize("where", ["file", "dropin", "plist"])
def test_managed_settings_refuse_on_either_key(tmp_path, no_machine_policy, key, where):
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    if where == "file":
        path = _put(no_machine_policy / "managed-settings.json", {key: True})
    elif where == "dropin":
        path = _put(no_machine_policy / "managed-settings.d" / "20-x.json", {key: True})
    else:
        path = no_machine_policy / "profile.plist"
        path.write_bytes(plistlib.dumps({key: True}))
    why = hooks_off_refusal({}, str(tmp_path / "home"), str(cwd), ("user",))
    assert why is not None and str(path) in why and key in why


def test_allow_managed_hooks_only_outside_managed_settings_is_not_read(tmp_path):
    """The key is managed-only (settings reference, scope Managed): in a user file it does nothing, so no refusal."""
    home, cwd = tmp_path / "home", tmp_path / "cwd"
    cwd.mkdir()
    _put(home / ".claude" / "settings.json", {"allowManagedHooksOnly": True})
    assert hooks_off_refusal({}, str(home), str(cwd), ALL) is None


@pytest.mark.parametrize("value", [False, "true", 1, None])
def test_only_a_json_true_refuses(tmp_path, value):
    home, cwd = tmp_path / "home", tmp_path / "cwd"
    cwd.mkdir()
    _put(home / ".claude" / "settings.json", {"disableAllHooks": value})
    assert hooks_off_refusal({}, str(home), str(cwd), ALL) is None


@pytest.mark.parametrize("text", ["{not json", "[1, 2]", ""])
def test_an_unreadable_or_odd_file_is_passed_over(tmp_path, text):
    home, cwd = tmp_path / "home", tmp_path / "cwd"
    cwd.mkdir()
    p = home / ".claude" / "settings.json"
    p.parent.mkdir(parents=True)
    p.write_text(text)
    assert hooks_off_refusal({}, str(home), str(cwd), ALL) is None


def test_a_host_open_refuses_and_spawns_nothing_while_hooks_are_off(world):
    path = _put(world["home"] / ".claude" / "settings.json", {"disableAllHooks": True})
    lr = launcher(world)
    with pytest.raises(RoomLaunchError) as err:
        lr.ensure()
    assert str(path) in str(err.value) and "disableAllHooks" in str(err.value)
    assert spawns(world) == []


def test_a_host_profile_with_no_hooks_has_nothing_to_turn_off(tmp_path):
    w = build(tmp_path, profile=make_profile(tmp_path, hooks=()))
    try:
        _put(w["home"] / ".claude" / "settings.json", {"disableAllHooks": True})
        launcher(w).ensure()
        assert len(spawns(w)) == 1
    finally:
        for lr in w["launchers"]:
            lr.close()
        w["svc"].close()


# --- 1(b) onFailure block on every host hook entry ------------------------------------------------------------------
def test_every_host_hook_entry_blocks_on_failure(tmp_path):
    a, b = tmp_path / "a.py", tmp_path / "b.py"
    a.write_text("pass\n")
    b.write_text("pass\n")
    prof = RoomProfile(hooks=(HookSpec("Bash", str(a)), HookSpec("Agent", str(b), 30)))
    doc = json.loads(host_settings_json(prof, ["cmd a", "cmd b"]))
    hooks = [h for e in doc["hooks"]["PreToolUse"] for h in e["hooks"]]
    assert [h["onFailure"] for h in hooks] == ["block", "block"]
    assert [h["timeout"] for h in hooks] == [10, 30]


def test_the_launchs_settings_file_carries_onfailure_block(world):
    _seen, spawn = argv_spy()
    launcher(world, spawn=spawn).ensure()
    entry = json.loads(spawn.settings[0])["hooks"]["PreToolUse"][0]["hooks"][0]
    assert entry["onFailure"] == "block" and entry["type"] == "command"


# --- 1(c) the guard denies a Bash call it could not decide -----------------------------------------------------------
def _run_guard(payload: str):
    return subprocess.run([sys.executable, "-I", str(GUARD_PATH)], input=payload, capture_output=True, text=True,
                          timeout=30)


def test_the_shipped_guard_is_the_new_pin():
    assert hashlib.sha256(GUARD_PATH.read_bytes()).hexdigest() == ROOM_TURN_GUARD_SHA256 == (
        "58aac7f3845409af089a7047fef9f2af6692c9a2ad0a5a819f2f6abdb84c1f03")


@pytest.mark.parametrize("tool_input", [[1], "echo hi", 7, ["nohup", "x"]])
def test_a_bash_call_the_guard_cannot_decide_is_denied(tool_input):
    got = _run_guard(json.dumps({"tool_name": "Bash", "tool_input": tool_input}))
    assert got.returncode == 2, got.stderr
    out = json.loads(got.stdout)["hookSpecificOutput"]
    assert out["permissionDecision"] == "deny" and "could not check this Bash call" in out["permissionDecisionReason"]
    assert "Traceback" not in got.stderr


@pytest.mark.parametrize("payload", [
    {"tool_name": "Agent", "tool_input": [1]},          # args.get raises for a non-Bash tool: it still runs
    {"tool_name": ["Bash"], "tool_input": {}},          # an unhashable tool name is not Bash
    {"tool_name": "Task", "tool_input": "x"},
])
def test_a_crash_on_any_other_tool_still_lets_it_run(payload):
    got = _run_guard(json.dumps(payload))
    assert got.returncode == 0 and got.stdout == ""


def test_a_recursion_error_deciding_bash_denies(monkeypatch, capsys):
    def boom(_event):
        raise RecursionError("deep")
    monkeypatch.setattr(room_turn_guard, "refusal_for", boom)
    monkeypatch.setattr(sys, "stdin", __import__("io").StringIO(json.dumps({"tool_name": "Bash",
                                                                            "tool_input": {"command": "x"}})))
    assert room_turn_guard.main() == 2
    assert "RecursionError" in capsys.readouterr().out


def test_the_guard_still_allows_a_plain_bash_call_and_refuses_a_background_one():
    assert _run_guard(json.dumps({"tool_name": "Bash", "tool_input": {"command": "ls"}})).returncode == 0
    assert _run_guard(json.dumps({"tool_name": "Bash", "tool_input": {"command": "sleep 9 &"}})).returncode == 2
    assert _run_guard("not json").returncode == 0


# --- 2. a changed system prompt never resumes -------------------------------------------------------------------------
def test_a_changed_prompt_starts_a_fresh_conversation_and_records_the_new_prompts_digest(world):
    seen, spawn = argv_spy()
    lr = launcher(world, spawn=spawn)
    a = lr.ensure()
    lr.end(a.launch_id, "test")
    prof_b = make_profile(world["tmp"], system_prompt="Never deploy.")
    b = launcher(world, profile=prof_b, spawn=spawn).ensure()
    argv = seen[1][0]
    assert not b.resumed and b.harness_session_id != a.harness_session_id and "--resume" not in argv
    assert argv[argv.index("--session-id") + 1] == b.harness_session_id
    assert b.prompt_changed == a.harness_session_id
    sha = hashlib.sha256((world["tmp"] / "host_guard.py").read_bytes()).hexdigest()
    want = containment_digest(spawn.settings[1], "", None, profile=prof_b, hook_sha256=[sha])
    assert digests(world)[-1] == want
    detail = world["cs"]._read(lambda conn: conn.execute(
        "SELECT detail FROM chat_launch_checks WHERE launch_id = ? AND kind = 'probing'", (b.launch_id,)).fetchone()[0])
    assert a.harness_session_id in detail and "system prompt" in detail


def test_the_same_prompt_resumes(world):
    lr = launcher(world)
    a = lr.ensure()
    lr.end(a.launch_id, "test")
    b = launcher(world, profile=make_profile(world["tmp"])).ensure()     # an equal profile, built again
    assert b.resumed and b.harness_session_id == a.harness_session_id and b.prompt_changed is None


def test_a_prompt_that_goes_away_is_a_change_too(world):
    lr = launcher(world)
    a = lr.ensure()
    lr.end(a.launch_id, "test")
    prof = make_profile(world["tmp"], system_prompt=None)
    b = launcher(world, profile=prof).ensure()
    assert not b.resumed and b.prompt_changed == a.harness_session_id
    c_lr = launcher(world, profile=prof)
    c_lr.end(b.launch_id, "test")
    c = c_lr.ensure()
    assert c.resumed and c.harness_session_id == b.harness_session_id     # no prompt, then no prompt: same


def test_an_unknown_first_prompt_counts_as_changed(world):
    lr = launcher(world)
    a = lr.ensure()
    lr.end(a.launch_id, "test")
    (world["run"] / "room-prompts" / a.harness_session_id).unlink()
    b = lr.ensure()
    assert not b.resumed and b.prompt_changed == a.harness_session_id


def test_the_held_launch_reports_its_prompt_change_on_a_later_ensure(world):
    lr = launcher(world)
    a = lr.ensure()
    lr.end(a.launch_id, "test")
    lr2 = launcher(world, profile=make_profile(world["tmp"], system_prompt="Other."))
    b = lr2.ensure()
    assert lr2.ensure() == b and b.prompt_changed == a.harness_session_id


# --- 3. the open tripwire names the unexpected accept --------------------------------------------------------------
@pytest.mark.parametrize("tool", ["Read", "Bash", "Write", "Foo"])
def test_a_session_accept_in_a_host_room_latches_accepted_without_host_and_does_not_raise(tool):
    trip = _call_and_result(TripwireWindow(_ctx(open_tools=True)), tool,
                            {"decision": "accept", "source": "session", "reason_type": "mode"})
    assert trip is not None and trip.name == "accepted_without_host"


def test_a_session_rule_accept_in_a_host_room_latches_unexpected_rule():
    trip = _call_and_result(TripwireWindow(_ctx(open_tools=True)), "Read",
                            {"decision": "accept", "source": "session", "reason_type": "rule"})
    assert trip is not None and trip.name == "unexpected_rule"


def test_the_real_room_context_latches_a_session_accept_without_raising(tmp_path):
    ctx = room_launch.room_tripwire_context(str(tmp_path), profile=make_profile(tmp_path))
    trip = _call_and_result(TripwireWindow(ctx), "Read",
                            {"decision": "accept", "source": "session", "reason_type": "mode"})
    assert trip is not None and trip.name == "accepted_without_host"


# --- 4. the small ones ------------------------------------------------------------------------------------------------
def test_required_tools_moves_the_host_policy(tmp_path):
    a = make_profile(tmp_path, required_tools=("Read",))
    b = make_profile(tmp_path, required_tools=("Bash", "Read", "Edit", "Write"))
    sha = ["0" * 64]
    pa = host_policy(a, settings_json="{}", hook_sha256=sha, door=["propose_drop"])
    pb = host_policy(b, settings_json="{}", hook_sha256=sha, door=["propose_drop"])
    assert pa != pb and pa["required_tools"] == ["Read"]


class _Dog:
    def __init__(self, code):
        self.pid, self.code = 424242, code

    def poll(self):
        return self.code

    def wait(self, timeout=None):
        return 0

    def kill(self):
        pass


@pytest.mark.parametrize("code, killed", [(None, [424242]), (0, []), (-9, [])])
def test_abandon_signals_only_a_watchdog_still_running(monkeypatch, code, killed):
    got = []
    monkeypatch.setattr(room_host, "_kill_group", got.append)
    _r, w = os.pipe()
    try:
        room_host._abandon(_Dog(code), w)
    finally:
        os.close(_r)
    assert got == killed


def test_room_confined_without_a_room_exits_1_like_every_other_room_refusal(tmp_path, monkeypatch, capsys):
    from tests.test_room_default_profile import _run_serve
    rc, asked = _run_serve(tmp_path, monkeypatch, room_confined=True)
    assert rc == 1 and asked == []
    assert "refusing --room-confined" in capsys.readouterr().err


def test_an_import_error_building_the_room_is_a_refusal_line(tmp_path, monkeypatch, capsys):
    """A pin, not a RED: before this lane the broad ``except Exception`` after the tuple already caught it."""
    import levain.web_server as ws
    from levain.dashboard import AnnealPaths, SubstrateSource
    (tmp_path / "memory.db").write_bytes(b"")
    monkeypatch.setattr(ws, "_resolve_source", lambda p: SubstrateSource(anneal=AnnealPaths.from_db(tmp_path / "memory.db")))

    def broken(*_a, **_k):
        raise ImportError("no module named levain.desk.room_default")
    monkeypatch.setattr(ws, "_build_room_serve", broken)
    monkeypatch.setattr(ws, "make_server", lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("reached")))
    rc = ws.run_web_server(tmp_path, open_browser=False, write=True, room=tmp_path)
    assert rc == 1 and "refusing --room: no module named" in capsys.readouterr().err


# --- 5. the words ----------------------------------------------------------------------------------------------------
def test_the_readme_says_the_room_trusts_dir_like_claude_at_a_terminal():
    readme = (GUARD_PATH.parents[2] / "README.md").read_text()
    assert "CLAUDE.md" in readme and "directory you would not run Claude in" in readme


def test_the_profile_docs_say_the_spend_recycle_is_not_built():
    from levain.desk import room_profile
    doc = " ".join((room_profile.__doc__ or "").split())
    assert "NOT BUILT" in doc and "P2" in doc and "recycle_at_usd" in doc

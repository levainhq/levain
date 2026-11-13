"""The host room profile (levain 22, key 1112-36, lane P1A): the value, its validation, the settings JSON, the policy
dict the containment digest hashes, the argv and the open probe. Everything here is pure; the launcher that uses it is
another lane's. The confined room's own pins live in the existing suites and are not touched by this file."""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import pytest

from levain.desk import claude_session as cs
from levain.desk import room_profile as rp

SID = "11111111-1111-4111-8111-111111111111"
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "room_host_init_bypass_2_1_323.json"
MATCHER = "Bash|Agent|Task|CronCreate|Monitor|ScheduleWakeup|Workflow"
SHA = "ab" * 32


@pytest.fixture
def script(tmp_path):
    p = tmp_path / "guard.py"
    p.write_text("print('guard')\n", encoding="utf-8")
    return str(p)


def profile(script, **over):
    kw = dict(hooks=(rp.HookSpec(MATCHER, script),), system_prompt="You are the room.", model="claude-opus-5-5",
              effort="medium", max_turns=40, turn_budget_usd=5.0, recycle_at_usd=1.0,
              setting_sources=("user", "project", "local"))
    kw.update(over)
    return rp.RoomProfile(**kw)


def val(argv, flag):
    return argv[argv.index(flag) + 1]


def recorded_init():
    """The recorded bypass ``init`` plus what a launched room adds: its one connected door and the door's tool."""
    init = json.loads(FIXTURE.read_text(encoding="utf-8"))
    init.pop("_note", None)
    init["tools"] = [*init["tools"], *cs.room_door_tools(("propose_drop",))]
    init["mcp_servers"] = [{"name": "levain_room", "status": "connected"}]
    return init


# ---------------------------------------------------------------- the value and its validation

def test_a_valid_profile_is_built_and_frozen(script):
    p = profile(script)
    assert p.permission_mode == "bypassPermissions" and p.required_tools == ("Bash", "Read", "Edit", "Write")
    assert p.max_budget_usd == 6.0
    with pytest.raises(Exception):
        p.model = "x"                      # frozen


@pytest.mark.parametrize("why, make", [
    ("relative script", lambda s: rp.HookSpec(MATCHER, "scripts/guard.py")),
    ("missing script", lambda s: rp.HookSpec(MATCHER, s + ".gone")),
    ("a directory is not a script", lambda s: rp.HookSpec(MATCHER, str(Path(s).parent))),
    ("control char in the matcher", lambda s: rp.HookSpec("Bash\n|Write", s)),
    ("empty matcher", lambda s: rp.HookSpec("", s)),
    ("zero timeout", lambda s: rp.HookSpec(MATCHER, s, timeout=0)),
    ("mode other than bypass: dontAsk", lambda s: profile(s, permission_mode="dontAsk")),
    ("mode other than bypass: default", lambda s: profile(s, permission_mode="default")),
    ("recycle equal to the budget", lambda s: profile(s, turn_budget_usd=5.0, recycle_at_usd=5.0)),
    ("recycle above the budget", lambda s: profile(s, turn_budget_usd=5.0, recycle_at_usd=6.0)),
    ("recycle without a budget", lambda s: profile(s, turn_budget_usd=None, recycle_at_usd=1.0)),
    ("negative budget", lambda s: profile(s, turn_budget_usd=-1.0, recycle_at_usd=None)),
    ("sub-cent budget", lambda s: profile(s, turn_budget_usd=5.005, recycle_at_usd=None)),
    ("bool budget", lambda s: profile(s, turn_budget_usd=True, recycle_at_usd=None)),
    ("unknown effort", lambda s: profile(s, effort="turbo")),
    ("turns above the bound", lambda s: profile(s, max_turns=201)),
    ("zero turns", lambda s: profile(s, max_turns=0)),
    ("unknown setting source", lambda s: profile(s, setting_sources=("user", "managed"))),
    ("repeated setting source", lambda s: profile(s, setting_sources=("user", "user"))),
    ("no setting source", lambda s: profile(s, setting_sources=())),
    ("a hook that is not a HookSpec", lambda s: profile(s, hooks=({"matcher": MATCHER, "script": s},))),
    ("empty prompt", lambda s: profile(s, system_prompt="  ")),
    ("NUL in the prompt", lambda s: profile(s, system_prompt="a\x00b")),
    ("model with a space", lambda s: profile(s, model="claude opus")),
    ("no required tools", lambda s: profile(s, required_tools=())),
    ("repeated required tool", lambda s: profile(s, required_tools=("Bash", "Bash"))),
])
def test_the_profile_refuses(script, why, make):
    with pytest.raises(ValueError):
        make(script)


def test_a_relative_script_is_refused_even_when_it_exists_from_the_cwd(script, monkeypatch):
    monkeypatch.chdir(Path(script).parent)
    assert Path("guard.py").is_file()
    with pytest.raises(ValueError, match="absolute"):
        rp.HookSpec(MATCHER, "guard.py")


def test_a_script_that_vanishes_after_construction_is_refused_on_check(script):
    spec = rp.HookSpec(MATCHER, script)
    spec.check()
    Path(script).unlink()
    with pytest.raises(ValueError):
        spec.check()
    with pytest.raises(ValueError):
        rp.RoomProfile(hooks=(spec,))


# ---------------------------------------------------------------- the settings JSON

def test_host_settings_json_has_the_hook_entry_and_no_permissions_block(script):
    p = profile(script)
    doc = json.loads(rp.host_settings_json(p, ["/py -I /run/room_hook-abc.py"]))
    assert set(doc) == {"hooks"}
    assert "permissions" not in doc
    assert doc["hooks"] == {"PreToolUse": [{"matcher": MATCHER, "hooks": [
        {"type": "command", "command": "/py -I /run/room_hook-abc.py", "timeout": 10}]}]}
    assert "blockReadsOutsideWorkingDirectories" not in json.dumps(doc) and "deny" not in json.dumps(doc)


def test_host_settings_json_keeps_hooks_in_order_and_no_hooks_is_empty(script):
    two = profile(script, hooks=(rp.HookSpec("Bash", script, timeout=3), rp.HookSpec("Write", script)))
    doc = json.loads(rp.host_settings_json(two, ["a", "b"]))
    assert [e["matcher"] for e in doc["hooks"]["PreToolUse"]] == ["Bash", "Write"]
    assert doc["hooks"]["PreToolUse"][0]["hooks"][0]["timeout"] == 3
    assert json.loads(rp.host_settings_json(profile(script, hooks=()), [])) == {}


@pytest.mark.parametrize("commands", [[], ["a", "b"], [""], ["a\nb"], [None]])
def test_host_settings_json_refuses_commands_that_do_not_match_the_hooks(script, commands):
    with pytest.raises(ValueError):
        rp.host_settings_json(profile(script), commands)


# ---------------------------------------------------------------- the policy the digest hashes

def test_host_policy_records_what_the_spawn_ran_under(script):
    p = profile(script)
    settings = rp.host_settings_json(p, ["cmd"])
    pol = rp.host_policy(p, settings_json=settings, hook_sha256=[SHA], door=["mcp__levain_room__propose_drop"])
    assert pol["profile"] == "host" and pol["mode"] == "bypassPermissions"
    assert pol["setting_sources"] == ["user", "project", "local"]
    assert pol["hooks"] == [{"matcher": MATCHER, "sha256": SHA, "timeout": 10}]
    assert pol["settings"] == json.loads(settings)
    assert pol["system_prompt_sha256"] == hashlib.sha256(b"You are the room.").hexdigest()
    assert (pol["model"], pol["effort"], pol["max_turns"], pol["max_budget_usd"], pol["recycle_at_usd"]) == (
        "claude-opus-5-5", "medium", 40, 6.0, 1.0)
    assert pol["strict_mcp"] is True and pol["door"] == ["mcp__levain_room__propose_drop"]
    assert "host_commands" not in pol
    json.dumps(pol, sort_keys=True)            # plain data: the digest can hash it


def test_host_policy_moves_with_each_input_the_digest_must_bind(script):
    p = profile(script)
    settings = rp.host_settings_json(p, ["cmd"])

    def pol(prof=p, sha=SHA, st=settings, hc=None):
        return rp.host_policy(prof, settings_json=st, hook_sha256=[sha], door=["d"], host_commands=hc)
    base = json.dumps(pol(), sort_keys=True)
    assert json.dumps(pol(), sort_keys=True) == base               # stable
    assert json.dumps(pol(sha="cd" * 32), sort_keys=True) != base  # the hook's bytes
    assert json.dumps(pol(profile(script, system_prompt="other")), sort_keys=True) != base
    assert json.dumps(pol(profile(script, model="claude-haiku-5-5")), sort_keys=True) != base
    assert json.dumps(pol(hc={"relay": ["x"]}), sort_keys=True) != base
    assert pol(hc={"relay": ["x"]})["host_commands"] == {"relay": ["x"]}


@pytest.mark.parametrize("sha", ["", "AB" * 32, "ab" * 31, "zz" * 32, None])
def test_host_policy_refuses_a_malformed_hook_sha(script, sha):
    p = profile(script)
    with pytest.raises(ValueError):
        rp.host_policy(p, settings_json="{}", hook_sha256=[sha], door=["d"])
    with pytest.raises(ValueError):
        rp.host_policy(p, settings_json="{}", hook_sha256=[], door=["d"])


# ---------------------------------------------------------------- the argv

def test_the_host_argv_is_the_flowconnect_shape_and_narrows_nothing(script):
    p = profile(script)
    a = cs.room_host_argv(p, mcp_config="/r/mcp.json", settings="/r/settings.json", session_id=SID)
    assert a[:7] == ["claude", "-p", "--input-format", "stream-json", "--output-format", "stream-json", "--verbose"]
    assert "--include-partial-messages" in a and "--dangerously-skip-permissions" in a
    assert val(a, "--model") == "claude-opus-5-5" and val(a, "--effort") == "medium"
    assert val(a, "--max-turns") == "40" and val(a, "--max-budget-usd") == "6.00"
    assert "--strict-mcp-config" in a and val(a, "--mcp-config") == "/r/mcp.json"
    assert val(a, "--setting-sources") == "user,project,local" and val(a, "--settings") == "/r/settings.json"
    assert val(a, "--append-system-prompt") == "You are the room." and val(a, "--session-id") == SID
    for flag in ("--tools", "--allowedTools", "--disallowedTools", "--permission-mode", "--add-dir", "--bare",
                 "--resume"):
        assert flag not in a, flag


def test_the_host_argv_on_resume_and_with_the_dials_unset(script):
    bare = rp.RoomProfile(hooks=(rp.HookSpec(MATCHER, script),))
    a = cs.room_host_argv(bare, mcp_config="/r/m.json", settings="/r/s.json", resume=SID)
    assert val(a, "--resume") == SID and "--session-id" not in a
    for flag in ("--model", "--effort", "--max-turns", "--max-budget-usd", "--append-system-prompt"):
        assert flag not in a, flag
    assert val(a, "--setting-sources") == "user"
    # the setting's model is the fallback; the profile's own model wins when set
    assert val(cs.room_host_argv(bare, mcp_config="/r/m.json", settings="/r/s.json", resume=SID, model="sonnet"),
               "--model") == "sonnet"
    assert val(cs.room_host_argv(profile(script), mcp_config="/r/m.json", settings="/r/s.json", resume=SID,
                                 model="sonnet"), "--model") == "claude-opus-5-5"
    # a budget with no recycle is carried as it is
    one = profile(script, recycle_at_usd=None, turn_budget_usd=3.5)
    assert val(cs.room_host_argv(one, mcp_config="/r/m.json", settings="/r/s.json", resume=SID),
               "--max-budget-usd") == "3.50"


def test_the_host_argv_refuses_what_it_cannot_make_safe(script):
    ok = dict(mcp_config="/r/m.json", settings="/r/s.json")
    p = profile(script)
    with pytest.raises(ValueError):
        cs.room_host_argv(p, **ok)
    with pytest.raises(ValueError):
        cs.room_host_argv(p, **ok, session_id=SID, resume=SID)
    with pytest.raises(ValueError):
        cs.room_host_argv(p, mcp_config="m.json", settings="/r/s.json", session_id=SID)
    with pytest.raises(ValueError):
        cs.room_host_argv(p, mcp_config="/r/m.json", settings="s.json", session_id=SID)


def test_the_confined_argv_is_unchanged_by_the_profile_module(script):
    a = cs.room_argv(mcp_config="/r/mcp.json", settings="/r/settings.json", session_id=SID)
    assert val(a, "--tools") == "Read,Grep,Glob,WebSearch" and val(a, "--permission-mode") == "dontAsk"
    assert "--dangerously-skip-permissions" not in a


# ---------------------------------------------------------------- the open probe

def test_host_probe_accepts_the_recorded_bypass_init(script):
    p = profile(script)
    init = recorded_init()
    assert "Glob" not in init["tools"] and "Grep" not in init["tools"]      # the recorded default set has neither
    got = cs.host_probe(init, p)
    assert got.live and "bypassPermissions" in got.reason


def test_host_probe_accepts_extra_tools_and_the_hosts_door_set(script):
    p = profile(script)
    init = recorded_init()
    init["tools"] = [*init["tools"], "SomeNewVendorTool"]
    assert cs.host_probe(init, p).live
    both = ("propose_drop", "relay_to_desk")
    init["tools"] = [*init["tools"], *cs.room_door_tools(both)[1:]]
    assert cs.host_probe(init, p, both).live
    assert not cs.host_probe(recorded_init(), p, both).live                  # a configured door tool that is absent


def _without(key):
    def make():
        i = recorded_init()
        del i[key]
        return i
    return make


def _edit(fn):
    def make():
        i = copy.deepcopy(recorded_init())
        fn(i)
        return i
    return make


@pytest.mark.parametrize("make_init, why", [
    (lambda: _edit(lambda i: i.update(permissionMode="dontAsk"))(), "bypassPermissions"),
    (lambda: _edit(lambda i: i.update(permissionMode="default"))(), "bypassPermissions"),
    (_without("permissionMode"), "bypassPermissions"),
    (lambda: _edit(lambda i: i.update(tools=[t for t in i["tools"] if t != "Bash"]))(), "Bash"),
    (lambda: _edit(lambda i: i.update(tools=[t for t in i["tools"] if t != "Write"]))(), "Write"),
    (lambda: _edit(lambda i: i.update(tools=[t for t in i["tools"] if "propose_drop" not in t]))(), "propose_drop"),
    (lambda: _edit(lambda i: i["tools"].append("Bash"))(), "repeats"),
    (_without("tools"), "tool list"),
    (lambda: _edit(lambda i: i.update(mcp_servers=[]))(), "exactly one"),
    (lambda: _edit(lambda i: i["mcp_servers"].append({"name": "iterm-mcp", "status": "connected"}))(), "exactly one"),
    (lambda: _edit(lambda i: i.update(mcp_servers=[{"name": "other", "status": "connected"}]))(), "levain_room"),
    (lambda: _edit(lambda i: i.update(mcp_servers=[{"name": "levain_room", "status": "pending"}]))(), "connected"),
    (_without("mcp_servers"), "exactly one"),
])
def test_host_probe_refuses_with_the_reason(script, make_init, why):
    got = cs.host_probe(make_init(), profile(script))
    assert not got.live and why in got.reason


def test_host_probe_with_no_init_and_a_wider_floor(script):
    assert cs.host_probe(None, profile(script)) == cs.Probe(False, "no init has been read")
    wide = profile(script, required_tools=("Bash", "Read", "Edit", "Write", "Glob"))
    got = cs.host_probe(recorded_init(), wide)
    assert not got.live and "Glob" in got.reason


def test_the_confined_probe_still_refuses_the_hosts_surface(script):
    assert not cs.open_probe(recorded_init()).live

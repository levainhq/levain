"""Room D2 (1106-16): the host's commands as room tools.

The host (flow's bridge) hands the room a relay and a replies command; levain names no script. With none, the room is
what it was (``propose_drop`` and his tap). With a relay the host marked ``relay_without_tap``, ``relay_to_desk``
replaces ``propose_drop``: his quoted words go to the command's STDIN, never argv, once per room."""
from __future__ import annotations

import json
import sqlite3
import sys

import pytest

from levain.coordinator import claude_session as cs
from levain.coordinator import room, room_host, room_launch, room_mcp
from levain.coordinator.room_host import HostCommandError, HostCommands
from tests.test_coordinator_room_mcp import (NOLAUNCH, Clock, call, make_launch, make_thread, qdir, raw, refused, say,  # noqa: F401
                                             store, world)

THREAD = "th-0abcdef-a1b2c3"
FAKE = """\
import json, sys
mode = sys.argv[1]
rec = sys.argv[2]
if mode == "relay":
    text = sys.stdin.read()
    open(rec, "a").write(json.dumps({"argv": sys.argv[1:], "stdin": text}) + "\\n")
    if "FAIL" in text:
        print("Not sent: the inbox did not take it (OSError).", file=sys.stderr)
        sys.exit(1)
    print("Sent to the desk; it reads it on its next poll. (thread %s)" % "th-0abcdef-a1b2c3")
elif mode == "replies":
    open(rec, "a").write(json.dumps({"argv": sys.argv[1:], "stdin": sys.stdin.read()}) + "\\n")
    print("10:42 on it")
"""


@pytest.fixture
def host(tmp_path):
    script = tmp_path / "fake_desk.py"
    script.write_text(FAKE)
    rec = tmp_path / "rec.jsonl"
    py = sys.executable
    h = HostCommands(relay=(py, str(script), "relay", str(rec)), replies=(py, str(script), "replies", str(rec), "--thread"),
                     relay_without_tap=True)
    object.__setattr__(h, "rec", rec)      # the frozen dataclass has no slots: a plain attribute, for the test only
    return h


def hcall(lid, host, arguments, name, rid=1):
    params = {"name": name, "arguments": arguments}
    return room_mcp.handle(lid, {"jsonrpc": "2.0", "id": rid, "method": "tools/call", "params": params}, host)


def names(host, lid=NOLAUNCH):
    reply = room_mcp.handle(lid, {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}, host)
    return [t["name"] for t in reply["result"]["tools"]]


def recs(host):
    return [json.loads(x) for x in host.rec.read_text().splitlines()] if host.rec.exists() else []


def relays(qdir):
    c = raw(qdir)
    try:
        return c.execute("SELECT outcome, desk_thread, quoted, detail FROM room_relays ORDER BY rowid").fetchall()
    finally:
        c.close()


# --- which tools exist ------------------------------------------------------------------------------------------------
def test_no_host_is_exactly_the_old_door():
    assert names(None) == ["propose_drop"]
    assert room_host.door_tools(None) == ("propose_drop",)


def test_relay_without_tap_retires_the_card_tool_and_adds_replies(host):
    assert names(host) == ["relay_to_desk", "desk_replies"]


def test_a_relay_the_host_did_not_free_of_the_tap_keeps_the_card(host):
    h = HostCommands(relay=host.relay, replies=host.replies, relay_without_tap=False)
    assert names(h) == ["propose_drop", "desk_replies"]
    assert names(HostCommands(relay=host.relay)) == ["propose_drop"]


def test_replies_alone_adds_a_read_tool_and_retires_nothing(host):
    assert names(HostCommands(replies=host.replies)) == ["propose_drop", "desk_replies"]


def test_a_retired_tool_is_unknown(host, world):
    r = hcall(world["launch"], host, {"quote_ids": [world["m1"]], "summary": "x"}, "propose_drop")
    assert r["error"]["code"] == -32602 and "unknown tool" in r["error"]["message"]
    r = call(world["launch"], {"quote_ids": [world["m1"]]}, name="relay_to_desk")        # no host: not a tool at all
    assert "unknown tool" in r["error"]["message"]


def test_the_old_path_is_untouched_when_the_host_has_no_relay(store, qdir, world):
    r = hcall(world["launch"], None, {"quote_ids": [world["m1"]], "summary": "why"}, "propose_drop")
    assert r["result"]["isError"] is False and "A card is waiting for your tap" in r["result"]["content"][0]["text"]


# --- the relay --------------------------------------------------------------------------------------------------------
def test_relay_sends_his_words_on_stdin_never_argv(host, qdir, world):
    r = hcall(world["launch"], host, {"quote_ids": [world["m1"], world["m2"]]}, "relay_to_desk")
    assert r["result"]["isError"] is False, r
    text = r["result"]["content"][0]["text"]
    assert "relayed to the desk" in text and THREAD in text and "desk_replies" in text
    (rec,) = recs(host)
    assert rec["stdin"] == "drop this: fix the thing\n\nand check the other"
    assert "fix the thing" not in " ".join(rec["argv"])
    ((outcome, desk, quoted, _detail),) = relays(qdir)
    assert (outcome, desk) == ("sent", THREAD) and json.loads(quoted) == [world["m1"], world["m2"]]


def test_the_same_words_are_relayed_once_per_room(host, qdir, world):
    ok = hcall(world["launch"], host, {"quote_ids": [world["m1"]]}, "relay_to_desk")
    again = hcall(world["launch"], host, {"quote_ids": [world["m1"]]}, "relay_to_desk")
    refused(again, text="already relayed")
    assert len(recs(host)) == 1 and ok["result"]["isError"] is False
    # a different set is a different relay
    assert hcall(world["launch"], host, {"quote_ids": [world["m2"]]}, "relay_to_desk")["result"]["isError"] is False
    assert len(recs(host)) == 2


def test_a_failed_command_is_reported_recorded_failed_and_retryable(host, store, qdir, world):
    bad = say(store, world["thread"], "FAIL please", n=9)
    r = hcall(world["launch"], host, {"quote_ids": [bad]}, "relay_to_desk")
    assert r["result"]["isError"] is True and "not relayed" in r["result"]["content"][0]["text"]
    assert "inbox did not take it" in r["result"]["content"][0]["text"]
    assert [x[0] for x in relays(qdir)] == ["failed"]
    # a failed relay does not use up the words
    r2 = hcall(world["launch"], host, {"quote_ids": [bad]}, "relay_to_desk")
    assert r2["result"]["isError"] is True and len(recs(host)) == 2


def test_a_missing_program_is_a_refusal_not_a_crash(qdir, world, tmp_path):
    h = HostCommands(relay=("/nonexistent/prog",), relay_without_tap=True)
    r = hcall(world["launch"], h, {"quote_ids": [world["m1"]]}, "relay_to_desk")
    assert r["result"]["isError"] is True and "could not start" in r["result"]["content"][0]["text"]
    assert [x[0] for x in relays(qdir)] == ["failed"]


def test_relay_takes_ids_only(host, world):
    for extra in ({"words": "mine"}, {"text": "mine"}, {"summary": "s"}, {"launch_id": "ln-" + "1" * 32}, {"thread": "x"}):
        r = hcall(world["launch"], host, {"quote_ids": [world["m1"]], **extra}, "relay_to_desk")
        assert r["error"]["code"] == -32602, extra
    assert not host.rec.exists()


def test_relay_quotes_only_his_own_messages_in_the_open_room(host, store, qdir, world):
    coord = say(store, world["thread"], "the coordinator's words", n=7, author="coordinator")
    refused(hcall(world["launch"], host, {"quote_ids": [coord]}, "relay_to_desk"), text="not the operator's own")
    refused(hcall(world["launch"], host, {"quote_ids": ["cm-" + "0" * 32]}, "relay_to_desk"), text="no message")
    assert not host.rec.exists() and relays(qdir) == []


def test_relay_refuses_a_non_room_or_missing_launch(host, store, qdir, world):
    refused(hcall(NOLAUNCH, host, {"quote_ids": [world["m1"]]}, "relay_to_desk"), text="does not exist")
    worker, _ = make_launch(store, qdir, world["thread"], role="worker", n=2)
    refused(hcall(worker, host, {"quote_ids": [world["m1"]]}, "relay_to_desk"), text="not a room launch")
    assert not host.rec.exists()


# --- replies ----------------------------------------------------------------------------------------------------------
def test_replies_appends_only_a_validated_thread_id(host, world):
    r = hcall(world["launch"], host, {"thread": THREAD}, "desk_replies")
    assert r["result"]["isError"] is False and r["result"]["content"][0]["text"] == "10:42 on it"
    (rec,) = recs(host)
    assert rec["argv"][-2:] == ["--thread", THREAD] and rec["stdin"] == ""


@pytest.mark.parametrize("bad", ["", "x", "th-", "th-abc; rm -rf /", "th-0abcdef-a1b2c3 --text", "../th-1", "th-" + "a" * 40,
                                 7, None, ["th-0abcdef-a1b2c3"]])
def test_replies_refuses_anything_but_a_thread_id(host, world, bad):
    r = hcall(world["launch"], host, {"thread": bad}, "desk_replies")
    assert r["error"]["code"] == -32602
    assert not host.rec.exists()


def test_replies_takes_no_other_argument(host, world):
    r = hcall(world["launch"], host, {"thread": THREAD, "text": "hi"}, "desk_replies")
    assert r["error"]["code"] == -32602


# --- the host's configuration -----------------------------------------------------------------------------------------
@pytest.mark.parametrize("bad", [("relative/prog",), (), "prog", ("/a", ""), ("/a", "b\x00"), ["/a"] * 40, ("/a", 3)])
def test_a_bad_argv_is_refused_at_construction(bad):
    with pytest.raises(HostCommandError):
        HostCommands(relay=bad)


def test_the_environment_round_trip_and_a_malformed_value_is_refused(host):
    assert room_host.from_env(None) is None and room_host.from_env("") is None
    back = room_host.from_env(host.to_json())
    assert (back.relay, back.replies, back.relay_without_tap) == (host.relay, host.replies, True)
    for bad in ("nope", "[]", '{"relay": ["/a"], "extra": 1}', '{"relay": ["rel"]}', '{"relay_without_tap": "yes"}'):
        with pytest.raises(HostCommandError):
            room_host.from_env(bad)


def test_the_door_refuses_to_start_on_a_malformed_host_value(monkeypatch, capsys):
    monkeypatch.setenv(room_host.HOSTCMD_ENV, "not json")
    assert room_mcp.main(["--launch", "ln-" + "a" * 32]) == 2
    assert "not started" in capsys.readouterr().err


def test_run_times_out_and_caps_output(tmp_path):
    s = tmp_path / "slow.py"
    s.write_text("import time\ntime.sleep(5)\n")
    code, out = room_host.run((sys.executable, str(s)), timeout=0.3)
    assert code == 124 and "did not finish" in out
    b = tmp_path / "big.py"
    b.write_text("print('x' * 100000)\n")
    code, out = room_host.run((sys.executable, str(b)))
    assert code == 0 and len(out) == room_host.OUTPUT_MAX


# --- containment: the argv, the probe, the digest ---------------------------------------------------------------------
def test_argv_allows_exactly_the_hosts_door_tools(host):
    base = cs.room_argv(mcp_config="/r/m.json", settings="/r/s.json", session_id="s" * 8 + "-0000-4000-8000-" + "0" * 12)
    allowed = base[base.index("--allowedTools") + 1].split(",")
    assert allowed == [*cs.ROOM_TOOLS, cs.ROOM_MCP_TOOL]                      # no host: unchanged
    h = cs.room_argv(mcp_config="/r/m.json", settings="/r/s.json", session_id="s" * 8 + "-0000-4000-8000-" + "0" * 12,
                     door_tools=room_host.door_tools(host))
    allowed = h[h.index("--allowedTools") + 1].split(",")
    assert allowed == [*cs.ROOM_TOOLS, "mcp__levain_room__relay_to_desk", "mcp__levain_room__desk_replies"]
    with pytest.raises(ValueError):
        cs.room_door_tools(("propose_drop", "Bash"))
    with pytest.raises(ValueError):
        cs.room_door_tools(("propose_drop", "propose_drop"))
    with pytest.raises(ValueError):
        cs.room_door_tools(())


def _init(extra):
    return {"type": "system", "subtype": "init", "tools": [*cs.ROOM_TOOLS, *extra], "permissionMode": "dontAsk",
            "mcp_servers": [{"name": "levain_room", "status": "connected"}]}


def test_the_probe_wants_exactly_the_configured_tools(host):
    names_ = room_host.door_tools(host)
    relay_init = _init(["mcp__levain_room__relay_to_desk", "mcp__levain_room__desk_replies"])
    assert cs.open_probe(relay_init, names_).live
    assert not cs.open_probe(relay_init).live                                   # a door configured no host is not this one
    assert not cs.open_probe(_init([cs.ROOM_MCP_TOOL]), names_).live              # the retired card tool is EXTRA... and missing
    assert not cs.open_probe(_init(["mcp__levain_room__relay_to_desk"]), names_).live
    assert cs.open_probe(_init([cs.ROOM_MCP_TOOL])).live


def test_the_tripwire_context_names_the_hosts_tools(host):
    ctx = room_launch.room_tripwire_context("/w", room_host.door_tools(host))
    tools = {t for t, _src in ctx.rule_allow}
    assert tools == {*cs.ROOM_TOOLS, "mcp__levain_room__relay_to_desk", "mcp__levain_room__desk_replies"}
    assert {t for t, _src in room_launch.room_tripwire_context("/w").rule_allow} == {*cs.ROOM_TOOLS, cs.ROOM_MCP_TOOL}


def test_mcp_config_carries_the_host_commands_only_when_there_are_some(host):
    plain = json.loads(room_launch.mcp_config_json("ln-" + "a" * 32, "/py"))
    assert room_host.HOSTCMD_ENV not in plain["mcpServers"]["levain_room"]["env"]
    with_host = json.loads(room_launch.mcp_config_json("ln-" + "a" * 32, "/py", host))
    assert json.loads(with_host["mcpServers"]["levain_room"]["env"][room_host.HOSTCMD_ENV])["relay_without_tap"] is True


def test_the_digest_binds_the_host_commands_and_is_unchanged_without_them(host):
    s = json.dumps({"a": 1})
    sha = "0" * 64
    base = room_launch.containment_digest(s, sha)
    assert base == room_launch.containment_digest(s, sha, None)
    with_host = room_launch.containment_digest(s, sha, host)
    other = HostCommands(relay=("/bin/other",), replies=host.replies, relay_without_tap=True)
    assert len({base, with_host, room_launch.containment_digest(s, sha, other)}) == 3


def test_the_setting_without_a_host_opens_the_old_door():
    assert room_launch.RoomSetting("/w").door_names == ("propose_drop",)
    assert room_launch.RoomSetting("/w", host=HostCommands(relay=("/a",), relay_without_tap=True)).door_names == \
        ("relay_to_desk",)


# --- the page's word --------------------------------------------------------------------------------------------------
def test_room_json_says_how_a_drop_leaves_the_room(tmp_path):
    from levain.cockpit.room import serve_room

    d = tmp_path / "d"
    d.mkdir()
    for view, want in ((None, None), (lambda: {"drops_via": "desk"}, "desk"), (lambda: {"drops_via": "card"}, "card"),
                       (lambda: {"drops_via": "weird"}, "card"), (lambda: {}, "card")):
        status, body = serve_room("/cockpit/room.json", directory=d, view=view)
        assert status == 200
        assert body.get("drops_via") == want, (view, body)


def test_the_schema_is_additive(qdir, store):
    c = sqlite3.connect(qdir / "dispatch.db")
    room.ensure(c)
    assert c.execute("SELECT 1 FROM sqlite_master WHERE name = 'room_relays'").fetchone()
    c.close()


def test_the_real_stdio_door_reads_the_hosts_commands_from_its_environment(host, tmp_path):
    import os
    import subprocess

    env = {**os.environ, room_host.HOSTCMD_ENV: host.to_json(), "PYTHONPATH": os.getcwd()}
    msgs = "\n".join(json.dumps(m) for m in (
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "desk_replies", "arguments": {"thread": THREAD}}},
    )) + "\n"
    out = subprocess.run([sys.executable, "-P", "-m", "levain.coordinator.room_mcp", "--launch", "ln-" + "a" * 32],
                         input=msgs, capture_output=True, text=True, env=env, timeout=60)
    replies = [json.loads(x) for x in out.stdout.splitlines()]
    assert [t["name"] for t in replies[0]["result"]["tools"]] == ["relay_to_desk", "desk_replies"]
    assert replies[1]["result"]["content"][0]["text"] == "10:42 on it"

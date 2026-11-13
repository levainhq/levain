"""Room D2 (1106-16): the host's commands as room tools.

The host (flow's bridge) hands the room a relay and a replies command; levain names no script. With none, the room is
what it was (``propose_drop`` and his tap). With a relay the host marked ``relay_without_tap``, ``relay_to_desk``
replaces ``propose_drop``: his quoted words go to the command's STDIN, never argv, once per room."""
from __future__ import annotations

import json
import sqlite3
import sys

import pytest

from levain.desk import claude_session as cs
from levain.desk import room, room_host, room_launch, room_mcp
from levain.desk.room_host import HostCommandError, HostCommands
from tests.test_desk_room_mcp import (NOLAUNCH, Clock, call, make_launch, make_thread, qdir, raw, refused, say,  # noqa: F401
                                             store, world)
from levain.desk.legacy import ROLE_DESK

THREAD = "th-0abcdef-a1b2c3"
FAKE = """\
import json, sys, time
mode = sys.argv[1]
rec = sys.argv[2]
if mode == "relay":
    text = sys.stdin.read()
    open(rec, "a").write(json.dumps({"argv": sys.argv[1:], "stdin": text}) + "\\n")
    if "SLOW" in text:
        time.sleep(1.0)
    if "HANG" in text:
        time.sleep(30)
    if "FAIL" in text:
        print("Not sent: the inbox did not take it (OSError).", file=sys.stderr)
        sys.exit(1)
    print("Sent to the desk; it reads it on its next poll. (thread %s)" % "th-0abcdef-a1b2c3")
elif mode == "replies":
    open(rec, "a").write(json.dumps({"argv": sys.argv[1:], "stdin": sys.stdin.read()}) + "\\n")
    print("10:42 on it")
elif mode == "note":
    text = sys.stdin.read()
    open(rec, "a").write(json.dumps({"argv": sys.argv[1:], "stdin": text}) + "\\n")
    if "NOTEFAIL" in text:
        print("Not sent: the inbox did not take it (OSError).", file=sys.stderr)
        sys.exit(1)
    print("Sent to the desk; it reads it on its next poll.")
"""


@pytest.fixture
def host(tmp_path):
    script = tmp_path / "fake_desk.py"
    script.write_text(FAKE)
    rec = tmp_path / "rec.jsonl"
    py = sys.executable
    h = HostCommands(relay=(py, str(script), "relay", str(rec)), replies=(py, str(script), "replies", str(rec), "--thread"),
                     note=(py, str(script), "note", str(rec)), relay_without_tap=True)
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
        return c.execute("SELECT COALESCE(o.outcome, 'claimed'), o.desk_thread, c.quoted, o.detail FROM room_relay_claims c "
                         "LEFT JOIN room_relay_outcomes o ON o.relay_id = c.relay_id ORDER BY c.rowid").fetchall()
    finally:
        c.close()


# --- which tools exist ------------------------------------------------------------------------------------------------
def test_no_host_is_exactly_the_old_door():
    assert names(None) == ["propose_drop"]
    assert room_host.door_tools(None) == ("propose_drop",)


def test_relay_without_tap_retires_the_card_tool_and_adds_replies(host):
    assert names(host) == ["relay_to_desk", "desk_replies", "note_to_desk"]
    assert names(HostCommands(relay=host.relay, replies=host.replies, relay_without_tap=True)) == \
        ["relay_to_desk", "desk_replies"]


def test_a_relay_the_host_did_not_free_of_the_tap_keeps_the_card(host):
    h = HostCommands(relay=host.relay, replies=host.replies, relay_without_tap=False)
    assert names(h) == ["propose_drop", "desk_replies"]
    assert names(HostCommands(relay=host.relay, replies=host.replies, note=host.note)) == \
        ["propose_drop", "desk_replies", "note_to_desk"]
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
    # a different message is a different relay
    assert hcall(world["launch"], host, {"quote_ids": [world["m2"]]}, "relay_to_desk")["result"]["isError"] is False
    assert len(recs(host)) == 2


def test_each_message_is_relayed_once_whatever_set_it_rides_in(host, store, qdir, world):
    """L3 r1 (complement 4): the key was the id LIST, so [m1] then [m1, m3] (or a reordered list) filed m1 twice."""
    m3 = say(store, world["thread"], "a third thing", n=11)
    assert hcall(world["launch"], host, {"quote_ids": [world["m1"]]}, "relay_to_desk")["result"]["isError"] is False
    refused(hcall(world["launch"], host, {"quote_ids": [world["m1"], m3]}, "relay_to_desk"), text="already relayed")
    refused(hcall(world["launch"], host, {"quote_ids": [m3, world["m1"]]}, "relay_to_desk"), text="already relayed")
    assert len(recs(host)) == 1
    assert hcall(world["launch"], host, {"quote_ids": [m3]}, "relay_to_desk")["result"]["isError"] is False


def test_a_command_that_ran_and_failed_is_unknown_and_never_rerun(host, store, qdir, world):
    """It started, so it may have delivered before failing: the words stay held, and the model is told so."""
    bad = say(store, world["thread"], "FAIL please", n=9)
    r = hcall(world["launch"], host, {"quote_ids": [bad]}, "relay_to_desk")
    text = r["result"]["content"][0]["text"]
    assert r["result"]["isError"] is True and "delivery unknown" in text and "inbox did not take it" in text, text
    assert "do not retry" in text
    assert [x[0] for x in relays(qdir)] == ["unknown"]
    refused(hcall(world["launch"], host, {"quote_ids": [bad]}, "relay_to_desk"), text="delivery is unknown")
    assert len(recs(host)) == 1


def test_a_missing_program_failed_certainly_and_releases_the_words(qdir, world, tmp_path):
    h = HostCommands(relay=("/nonexistent/prog",), relay_without_tap=True)
    r = hcall(world["launch"], h, {"quote_ids": [world["m1"]]}, "relay_to_desk")
    assert r["result"]["isError"] is True and "could not start" in r["result"]["content"][0]["text"]
    assert "nothing was delivered" in r["result"]["content"][0]["text"]
    assert [x[0] for x in relays(qdir)] == ["failed"]
    # nothing was delivered, so the words are free: a second attempt runs (and fails the same way)
    hcall(world["launch"], h, {"quote_ids": [world["m1"]]}, "relay_to_desk")
    assert [x[0] for x in relays(qdir)] == ["failed", "failed"]


def test_two_concurrent_relays_of_one_message_run_the_command_once(host, store, qdir, world):
    """L3 r1 (all four seats): the check and the record were two transactions with the command between them, so two
    doors (or a retry racing the first) both ran it. REPRODUCED on ec0e6f4a: two executions, and the loser told
    'nothing was relayed'."""
    import threading

    slow = say(store, world["thread"], "SLOW: file this", n=12)
    gate, out = threading.Barrier(2), [None, None]

    def go(i):
        gate.wait()
        out[i] = hcall(world["launch"], host, {"quote_ids": [slow]}, "relay_to_desk", rid=i)

    ts = [threading.Thread(target=go, args=(i,)) for i in (0, 1)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(30)
    assert len(recs(host)) == 1, recs(host)
    errs = sorted(o["result"]["isError"] for o in out)
    assert errs == [False, True], out
    loser = next(o for o in out if o["result"]["isError"])["result"]["content"][0]["text"]
    assert "in flight" in loser or "already relayed" in loser, loser
    assert [x[0] for x in relays(qdir)] == ["sent"]


def test_a_crash_between_delivery_and_the_record_never_duplicates(host, store, qdir, world, monkeypatch):
    """L3 r1 (codex 1, gemini 1): delivery succeeded, the record failed; a retry found no record and re-ran. Now the claim
    committed before the command holds the words."""
    def boom(*_a, **_k):
        raise sqlite3.OperationalError("disk I/O error")

    with monkeypatch.context() as m:
        m.setattr(room, "record_relay_outcome", boom)
        r = hcall(world["launch"], host, {"quote_ids": [world["m1"]]}, "relay_to_desk")
    text = r["result"]["content"][0]["text"]
    assert "relayed to the desk" in text and "could not record" in text and "do not relay them again" in text, text
    assert [x[0] for x in relays(qdir)] == ["claimed"]
    refused(hcall(world["launch"], host, {"quote_ids": [world["m1"]]}, "relay_to_desk"), text="in flight")
    assert len(recs(host)) == 1


def test_a_timed_out_relay_is_unknown_and_never_retried(host, store, qdir, world, monkeypatch):
    real = room_host.run
    monkeypatch.setattr(room_host, "run", lambda *a, **k: real(*a, **{**k, "timeout": 0.5}))
    hang = say(store, world["thread"], "HANG then file", n=13)
    r = hcall(world["launch"], host, {"quote_ids": [hang]}, "relay_to_desk")
    text = r["result"]["content"][0]["text"]
    assert r["result"]["isError"] is True and "delivery unknown" in text and "did not finish" in text, text
    assert [x[0] for x in relays(qdir)] == ["unknown"]
    refused(hcall(world["launch"], host, {"quote_ids": [hang]}, "relay_to_desk"), text="delivery is unknown")
    assert len(recs(host)) == 1


def test_the_claim_trigger_holds_for_any_writer(store, qdir, world):
    """The store itself refuses a second live claim on a message, whatever Python did before it."""
    c = raw(qdir)
    try:
        c.execute("BEGIN IMMEDIATE")
        room.claim_relay(c, world["launch"], world["thread"], [world["m1"]], "w")
        with pytest.raises(sqlite3.IntegrityError, match="in flight"):
            c.execute("INSERT INTO room_relay_claims (relay_id, room_thread, launch_id, quoted, words_sha256, at) VALUES "
                      "('rl-x', ?, ?, ?, 'h', 'now')", (world["thread"], world["launch"], json.dumps([world["m1"]])))
        c.execute("ROLLBACK")
    finally:
        c.close()


def test_relay_takes_ids_only(host, world):
    for extra in ({"words": "mine"}, {"text": "mine"}, {"summary": "s"}, {"launch_id": "ln-" + "1" * 32}, {"thread": "x"}):
        r = hcall(world["launch"], host, {"quote_ids": [world["m1"]], **extra}, "relay_to_desk")
        assert r["error"]["code"] == -32602, extra
    assert not host.rec.exists()


def test_relay_quotes_only_his_own_messages_in_the_open_room(host, store, qdir, world):
    coord = say(store, world["thread"], "the desk's words", n=7, author=ROLE_DESK)
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
    assert hcall(world["launch"], host, {"quote_ids": [world["m1"]]}, "relay_to_desk")["result"]["isError"] is False
    r = hcall(world["launch"], host, {"thread": THREAD}, "desk_replies")
    assert r["result"]["isError"] is False and r["result"]["content"][0]["text"] == "10:42 on it"
    (_relay, rec) = recs(host)
    assert rec["argv"][-2:] == ["--thread", THREAD] and rec["stdin"] == ""


def test_replies_reads_only_a_thread_this_room_relayed_to(host, world):
    """L3 r1 (codex 2, complement 3): any well-shaped desk thread id was read, so a room could read another conversation."""
    foreign = "th-0abcdef-ffff00"
    refused(hcall(world["launch"], host, {"thread": foreign}, "desk_replies"), text="not a desk thread this room relayed to")
    refused(hcall(world["launch"], host, {"thread": THREAD}, "desk_replies"), text="not a desk thread this room relayed to")
    assert not host.rec.exists()
    hcall(world["launch"], host, {"quote_ids": [world["m1"]]}, "relay_to_desk")
    refused(hcall(world["launch"], host, {"thread": foreign}, "desk_replies"), text="not a desk thread this room relayed to")
    assert len(recs(host)) == 1


def test_replies_refuses_a_launch_that_is_not_live(host, store, world):
    from levain.desk import lease

    refused(hcall(NOLAUNCH, host, {"thread": THREAD}, "desk_replies"), text="does not exist")
    hcall(world["launch"], host, {"quote_ids": [world["m1"]]}, "relay_to_desk")
    with store._write() as (conn, now):
        lease.end_launch(conn, now, world["launch"], "ended", "done")
    refused(hcall(world["launch"], host, {"thread": THREAD}, "desk_replies"), text="ended, not active")
    assert len(recs(host)) == 1


@pytest.mark.parametrize("bad", ["", "x", "th-", "th-abc; rm -rf /", "th-0abcdef-a1b2c3 --text", "../th-1", "th-" + "a" * 40,
                                 7, None, ["th-0abcdef-a1b2c3"]])
def test_replies_refuses_anything_but_a_thread_id(host, world, bad):
    r = hcall(world["launch"], host, {"thread": bad}, "desk_replies")
    assert r["error"]["code"] == -32602
    assert not host.rec.exists()


def test_replies_takes_no_other_argument(host, world):
    r = hcall(world["launch"], host, {"thread": THREAD, "text": "hi"}, "desk_replies")
    assert r["error"]["code"] == -32602


# --- the seat-note (Room gap 3) ---------------------------------------------------------------------------------------
PREFIX = "[from the Room] "


def note_call(lid, host, arguments, rid=1):
    return hcall(lid, host, arguments, "note_to_desk", rid)


def test_the_note_tool_is_offered_only_when_the_host_gave_a_note(host, world):
    plain = HostCommands(relay=host.relay, replies=host.replies, relay_without_tap=True)
    assert "note_to_desk" not in names(plain)
    r = note_call(world["launch"], plain, {"note": "hello"})
    assert r["error"]["code"] == -32602 and "unknown tool" in r["error"]["message"]
    assert names(HostCommands(note=host.note)) == ["propose_drop", "note_to_desk"]       # a note retires nothing
    assert names(host)[-1] == "note_to_desk"


def test_the_note_tool_has_one_argument_named_note_and_the_cap_leaves_room_for_the_prefix(host):
    reply = room_mcp.handle(NOLAUNCH, {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}, host)
    (tool,) = [t for t in reply["result"]["tools"] if t["name"] == "note_to_desk"]
    schema = tool["inputSchema"]
    assert set(schema["properties"]) == {"note"} and schema["required"] == ["note"]
    assert schema["additionalProperties"] is False
    assert schema["properties"]["note"]["maxLength"] == room_host.NOTE_MAX - len(room_host.NOTE_PREFIX)
    assert room_host.NOTE_MAX == 600 and room_host.NOTE_PREFIX == PREFIX


def test_the_note_text_goes_on_stdin_with_the_room_prefix_and_never_in_argv(host, qdir, world):
    r = note_call(world["launch"], host, {"note": "the build is green but two tests skip"})
    assert r["result"]["isError"] is False, r
    assert "desk" in r["result"]["content"][0]["text"]
    (rec,) = recs(host)
    assert rec["stdin"] == PREFIX + "the build is green but two tests skip"
    assert rec["argv"] == ["note", str(host.rec)]            # the host's fixed argv, nothing the model named
    assert "green" not in " ".join(rec["argv"])


def test_the_prefixed_note_fits_the_scripts_cap_exactly(host, world):
    """chat_to_desk.py counts the WHOLE stdin text against its 600, so prefix + note is what must fit."""
    cap = room_host.NOTE_MAX - len(room_host.NOTE_PREFIX)
    assert note_call(world["launch"], host, {"note": "n" * cap})["result"]["isError"] is False
    (rec,) = recs(host)
    assert len(rec["stdin"]) == room_host.NOTE_MAX


def test_a_note_over_the_cap_is_refused_before_the_command_runs(host, world):
    cap = room_host.NOTE_MAX - len(room_host.NOTE_PREFIX)
    r = note_call(world["launch"], host, {"note": "n" * (cap + 1)})
    assert r["error"]["code"] == -32602 and "over" in r["error"]["message"], r
    assert not host.rec.exists()


@pytest.mark.parametrize("key", ["text", "body", "message", "words", "content"])
def test_the_note_tool_refuses_the_words_keys_even_beside_a_note(host, world, key):
    for args in ({key: "mine"}, {"note": "ok", key: "mine"}):
        r = note_call(world["launch"], host, args)
        assert r["error"]["code"] == -32602, args
    assert not host.rec.exists()


@pytest.mark.parametrize("extra", [{"summary": "s"}, {"quote_ids": ["cm-x"]}, {"destination": "him"}, {"launch_id": "ln-" + "1" * 32}])
def test_the_note_tool_takes_a_note_and_nothing_else(host, world, extra):
    assert note_call(world["launch"], host, {"note": "ok", **extra})["error"]["code"] == -32602
    assert not host.rec.exists()


@pytest.mark.parametrize("bad", [None, 7, ["a"], {"a": 1}, "", "   ", " \t "])
def test_a_note_must_be_non_empty_text(host, world, bad):
    r = note_call(world["launch"], host, {"note": bad})
    assert r["error"]["code"] == -32602
    assert note_call(world["launch"], host, {})["error"]["code"] == -32602
    assert not host.rec.exists()


@pytest.mark.parametrize("ch", ["\x00", "\x07", "\x1b", "\n", "\r", "\t", "\x7f", "\x85", " ", " "])
def test_a_note_with_a_control_character_is_refused(host, world, ch):
    r = note_call(world["launch"], host, {"note": f"first{ch}second"})
    assert r["error"]["code"] == -32602 and "control" in r["error"]["message"], r
    assert not host.rec.exists()


def test_a_note_needs_a_live_room_launch(host, store, qdir, world):
    from levain.desk import lease

    refused_note = lambda lid: note_call(lid, host, {"note": "x"})        # noqa: E731
    r = refused_note(NOLAUNCH)
    assert r["result"]["isError"] is True and "does not exist" in r["result"]["content"][0]["text"]
    worker, _ = make_launch(store, qdir, world["thread"], role="worker", n=2)
    r = refused_note(worker)
    assert r["result"]["isError"] is True and "not a room launch" in r["result"]["content"][0]["text"]
    with store._write() as (conn, now):
        lease.end_launch(conn, now, world["launch"], "ended", "done")
    r = refused_note(world["launch"])
    assert r["result"]["isError"] is True and "ended, not active" in r["result"]["content"][0]["text"]
    assert not host.rec.exists()


def test_a_note_claims_nothing_and_repeats_are_harmless(host, qdir, world):
    for _ in range(2):
        assert note_call(world["launch"], host, {"note": "same note"})["result"]["isError"] is False
    assert len(recs(host)) == 2 and relays(qdir) == []
    # and it does not touch his messages: the relay of one still goes
    assert hcall(world["launch"], host, {"quote_ids": [world["m1"]]}, "relay_to_desk")["result"]["isError"] is False
    assert [x[0] for x in relays(qdir)] == ["sent"]


def test_a_note_the_command_could_not_file_says_so(host, world):
    r = note_call(world["launch"], host, {"note": "NOTEFAIL now"})
    text = r["result"]["content"][0]["text"]
    assert r["result"]["isError"] is True and "not filed" in text and "inbox did not take it" in text, text
    r = note_call(world["launch"], HostCommands(note=("/nonexistent/prog",)), {"note": "x"})
    assert r["result"]["isError"] is True and "not filed" in r["result"]["content"][0]["text"]


def test_the_note_argv_is_validated_like_the_others(tmp_path):
    for bad in (("relative/prog",), (), "prog", ("/a", ""), ("/a", "b\x00")):
        with pytest.raises(HostCommandError):
            HostCommands(note=bad)
    assert HostCommands(note=["/a", "b"]).note == ("/a", "b")


def test_the_note_survives_the_environment_round_trip(host):
    doc = json.loads(host.to_json())
    assert doc["note"] == list(host.note)
    assert json.loads(HostCommands(relay=host.relay).to_json())["note"] is None
    assert room_host.from_env(json.dumps({"note": ["/a", "note"]})).note == ("/a", "note")


def test_the_door_name_is_in_the_closed_list_and_in_the_argv():
    assert "note_to_desk" in cs.ROOM_DOOR_NAMES
    assert cs.room_door_tools(("propose_drop", "note_to_desk")) == ("mcp__levain_room__propose_drop",
                                                                   "mcp__levain_room__note_to_desk")


def test_the_digest_changes_by_itself_when_only_the_note_door_changes(host):
    s, sha = json.dumps({"a": 1}), "0" * 64
    without = HostCommands(relay=host.relay, replies=host.replies, relay_without_tap=True)
    other = HostCommands(relay=host.relay, replies=host.replies, note=("/bin/other", "note"), relay_without_tap=True)
    digests = {room_launch.containment_digest(s, sha, h) for h in (without, host, other)}
    assert len(digests) == 3


# --- the host's configuration -----------------------------------------------------------------------------------------
@pytest.mark.parametrize("bad", [("relative/prog",), (), "prog", ("/a", ""), ("/a", "b\x00"), ["/a"] * 40, ("/a", 3)])
def test_a_bad_argv_is_refused_at_construction(bad):
    with pytest.raises(HostCommandError):
        HostCommands(relay=bad)


def test_the_environment_round_trip_and_a_malformed_value_is_refused(host):
    assert room_host.from_env(None) is None and room_host.from_env("") is None
    back = room_host.from_env(host.to_json())
    assert (back.relay, back.replies, back.note, back.relay_without_tap) == (host.relay, host.replies, host.note, True)
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
    code, out, started = room_host.run((sys.executable, str(s)), timeout=0.3)
    assert code == 124 and "did not finish" in out and started is True
    b = tmp_path / "big.py"
    b.write_text("print('x' * 100000)\n")
    code, out, _started = room_host.run((sys.executable, str(b)))
    assert code == 0 and len(out) == room_host.OUTPUT_MAX


def test_a_timeout_kills_the_commands_whole_process_group(tmp_path):
    """L3 r1 (codex 3): only the immediate child was killed, so a helper it spawned delivered after the door gave up.
    REPRODUCED on ec0e6f4a: the grandchild outlived the timeout and wrote its mark."""
    import os
    import time

    pidf, mark = tmp_path / "gc.pid", tmp_path / "late.mark"
    gc = tmp_path / "gc.py"
    gc.write_text(f"import os, time\nopen({str(pidf)!r}, 'w').write(str(os.getpid()))\ntime.sleep(1.5)\n"
                  f"open({str(mark)!r}, 'w').write('delivered late')\n")
    parent = tmp_path / "parent.py"
    parent.write_text(f"import subprocess, sys, time\nsubprocess.Popen([sys.executable, {str(gc)!r}])\ntime.sleep(30)\n")
    t0 = time.monotonic()
    code, _out, started = room_host.run((sys.executable, str(parent)), timeout=0.8)
    assert code == 124 and started and time.monotonic() - t0 < 6
    pid = int(pidf.read_text())
    time.sleep(2.0)
    assert not mark.exists(), "the grandchild survived the timeout and acted"
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_a_noisy_command_is_read_with_bounded_memory(tmp_path):
    """L3 r1 (codex 4): capture_output held the whole output before the cap. 64 MB of output now peaks far below that."""
    import tracemalloc

    noisy = tmp_path / "noisy.py"
    noisy.write_text("import sys\nb = b'y' * (1 << 20)\nfor _ in range(64):\n    sys.stdout.buffer.write(b)\n"
                     "sys.stderr.write('the real error')\nsys.exit(3)\n")
    tracemalloc.start()
    try:
        code, out, _started = room_host.run((sys.executable, str(noisy)))
        _now, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert code == 3 and len(out) <= room_host.OUTPUT_MAX
    assert peak < 4 * (1 << 20), peak
    assert out.endswith("the real error")          # a failure's stderr survives a long stdout (gemini 5)


def test_a_program_popen_will_not_pass_never_started():
    code, out, started = room_host.run(("/bin/echo", "a\x00b"))
    assert (code, started) == (room_host.NOT_STARTED, False) and "could not start" in out


# --- containment: the argv, the probe, the digest ---------------------------------------------------------------------
def test_argv_allows_exactly_the_hosts_door_tools(host):
    base = cs.room_argv(mcp_config="/r/m.json", settings="/r/s.json", session_id="s" * 8 + "-0000-4000-8000-" + "0" * 12)
    allowed = base[base.index("--allowedTools") + 1].split(",")
    assert allowed == [*cs.ROOM_TOOLS, cs.ROOM_MCP_TOOL]                      # no host: unchanged
    h = cs.room_argv(mcp_config="/r/m.json", settings="/r/s.json", session_id="s" * 8 + "-0000-4000-8000-" + "0" * 12,
                     door_tools=room_host.door_tools(host))
    allowed = h[h.index("--allowedTools") + 1].split(",")
    assert allowed == [*cs.ROOM_TOOLS, "mcp__levain_room__relay_to_desk", "mcp__levain_room__desk_replies",
                       "mcp__levain_room__note_to_desk"]
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
    relay_init = _init(["mcp__levain_room__relay_to_desk", "mcp__levain_room__desk_replies",
                        "mcp__levain_room__note_to_desk"])
    assert cs.open_probe(relay_init, names_).live
    assert not cs.open_probe(relay_init).live                                   # a door configured no host is not this one
    assert not cs.open_probe(_init([cs.ROOM_MCP_TOOL]), names_).live              # the retired card tool is EXTRA... and missing
    assert not cs.open_probe(_init(["mcp__levain_room__relay_to_desk"]), names_).live
    assert not cs.open_probe(_init(["mcp__levain_room__relay_to_desk", "mcp__levain_room__desk_replies"]), names_).live   # the note is configured, so it is wanted
    assert cs.open_probe(_init([cs.ROOM_MCP_TOOL])).live


def test_the_tripwire_context_names_the_hosts_tools(host):
    ctx = room_launch.room_tripwire_context("/w", room_host.door_tools(host))
    tools = {t for t, _src in ctx.rule_allow}
    assert tools == {*cs.ROOM_TOOLS, "mcp__levain_room__relay_to_desk", "mcp__levain_room__desk_replies",
                     "mcp__levain_room__note_to_desk"}
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
    for name in ("room_relay_claims", "room_relay_outcomes"):
        assert c.execute("SELECT 1 FROM sqlite_master WHERE name = ?", (name,)).fetchone()
    c.close()


def test_the_real_stdio_door_reads_the_hosts_commands_from_its_environment(host, qdir, world, tmp_path):
    """The door as Claude starts it (a fresh process, -P, its launch and host commands from the environment), against
    the test store (LEVAIN_HOME is the fixture's): it starts (L3 r1 deepseek 1, the 'missing import os', is refuted
    here), relays, reads the relayed thread, and refuses a foreign one."""
    import os
    import subprocess

    env = {**os.environ, room_host.HOSTCMD_ENV: host.to_json(), room_mcp.LAUNCH_ENV: world["launch"],
           "PYTHONPATH": os.getcwd()}
    assert env["LEVAIN_HOME"] == str(qdir.parent)
    tool = lambda i, name, args: {"jsonrpc": "2.0", "id": i, "method": "tools/call",      # noqa: E731
                                  "params": {"name": name, "arguments": args}}
    msgs = "\n".join(json.dumps(m) for m in (
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
        tool(2, "relay_to_desk", {"quote_ids": [world["m1"]]}),
        tool(3, "desk_replies", {"thread": THREAD}),
        tool(4, "desk_replies", {"thread": "th-0abcdef-ffff00"}),
        tool(5, "note_to_desk", {"note": "seen through the real door"}),
        tool(6, "note_to_desk", {"note": "x", "text": "spoof"}),
    )) + "\n"
    out = subprocess.run([sys.executable, "-P", "-m", "levain.desk.room_mcp"],
                         input=msgs, capture_output=True, text=True, env=env, timeout=60)
    assert out.returncode == 0, out.stderr
    replies = [json.loads(x) for x in out.stdout.splitlines()]
    assert [t["name"] for t in replies[0]["result"]["tools"]] == ["relay_to_desk", "desk_replies", "note_to_desk"]
    assert "relayed to the desk" in replies[1]["result"]["content"][0]["text"], replies[1]
    assert replies[2]["result"]["content"][0]["text"] == "10:42 on it"
    assert replies[3]["result"]["isError"] is True and "not a desk thread this room relayed to" in \
        replies[3]["result"]["content"][0]["text"]
    assert replies[4]["result"]["isError"] is False, replies[4]
    assert replies[5]["error"]["code"] == -32602
    stdins = [json.loads(x)["stdin"] for x in host.rec.read_text().splitlines() if json.loads(x)["argv"][0] == "note"]
    assert stdins == [PREFIX + "seen through the real door"]

"""H1-4 shape A, step 4: the room's ``propose_envelope`` door. It takes his quoted message ids, an open work thread, a spec
OBJECT and a summary, dry-runs the spec through ``parse_spec`` (the reader a preview and a tap use), and writes a proposal
and its spec row only: never a drop, never a draft. Refused by name, each with nothing written: a launch grant, an
identity or words key, a spec given as text, keys outside the draft's, a non-open or non-work thread, unshowable
characters (literal or escaped), a repeated key, over the cap, no quotes or foreign ones. Each test names the failure."""
from __future__ import annotations

import io
import json
import sqlite3

import pytest

from levain.desk.legacy import ROLE_DESK
from levain.desk import room, room_host, room_mcp, spec_card
from levain.desk.store import Part, words_sha256
from tests.test_desk_envelope import BUDGET, repo  # noqa: F401  (fixture)
from tests.test_desk_room_mcp import call, make_thread, proposals, qdir, raw, say, store, world  # noqa: F401


@pytest.fixture
def w(world, store, qdir, repo):  # noqa: F811
    d = store.accept_drop("fix the readme", surface="cli")[0]
    (t,) = store.split(d.drop_id, [Part("work", "fix the readme")])
    world.update(work=t.thread_id, path=repo["path"], qdir=qdir, store=store)
    return world


def spec(w, **over):
    s = {"where": [{"repo": w["path"], "paths": ["README"]}], "lands": [{"repo": w["path"], "onto": "main"}],
         "budget": dict(BUDGET), "stop": "2h"}
    s.update(over)
    return s


def args(w, **over):
    a = {"quote_ids": [w["m1"]], "thread": w["work"], "spec": spec(w), "summary": "he said two hours"}
    a.update(over)
    return a


def envelope(w, **over):
    return call(w["launch"], args(w, **over), name="propose_envelope")


def counts(w):
    c = raw(w["qdir"])
    try:
        return {t: c.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
                for t in ("drops", "envelope_drafts", "drop_proposals", "spec_proposals", "proposal_events", "drop_notes")}
    finally:
        c.close()


def refused(reply):
    """True for either shape of refusal: a protocol error, or the tool's own isError."""
    return "error" in reply or reply["result"]["isError"] is True


def test_the_door_writes_a_proposal_and_a_spec_row_and_no_drop_no_draft(w):
    """Failure: a door that commits (a drop or a draft from the model's call)."""
    before = counts(w)
    reply = envelope(w)
    assert "error" not in reply, reply
    assert reply["result"]["isError"] is False and "waiting for your tap" in reply["result"]["content"][0]["text"]
    after = counts(w)
    assert {k: after[k] - before[k] for k in after} == {"drops": 0, "envelope_drafts": 0, "drop_proposals": 1,
                                                         "spec_proposals": 1, "proposal_events": 0, "drop_notes": 0}
    c = raw(w["qdir"])
    try:
        (thread_id, text) = c.execute("SELECT thread_id, spec_text FROM spec_proposals").fetchone()
    finally:
        c.close()
    assert thread_id == w["work"] and json.loads(text) == spec(w)


def why(reply):
    """The refusal's sentence, from either shape."""
    return reply["error"]["message"] if "error" in reply else reply["result"]["content"][0]["text"]


@pytest.mark.parametrize("over,said", [
    ({"launches": []}, "launch grant"),
    ({"spec": {"launches": [], "stop": "2h"}}, "launch grant"),
    ({"thread_id": "th-x"}, "never names its own"),
    ({"launch_id": "ln-x"}, "never names its own"),
    ({"words": "do it"}, "no words"),
    ({"text": "do it"}, "no words"),
    ({"quote": "x"}, "no words"),
    ({"spec": '{"stop": "2h"}'}, "object, not text"),
    ({"spec": {}}, "non-empty object"),
    ({"extra": 1}, "extra"),
    ({"spec": {"stop": "2h", "text": "run rm"}}, "keys are among"),
    ({"quote_ids": []}, "quote_ids"),
    ({"quote_ids": ["cm-" + "0" * 32]}, "no message"),
    ({"thread": "nope"}, "work thread id"),
    ({"thread": "th-" + "0" * 32}, "names no thread"),
    ({"summary": "x" * 100_000}, "summary is over"),
    ({"spec": {"stop": "2h", "where": [{"repo": "/r", "paths": ["a" * 30_000]}]}}, "over"),
])
def test_refused_by_name_and_nothing_written(w, over, said):
    before = counts(w)
    reply = envelope(w, **over)
    assert refused(reply) and said in why(reply), why(reply)
    assert counts(w) == before


@pytest.mark.parametrize("which", ["missing thread", "missing spec", "missing summary", "missing quotes"])
def test_a_missing_argument_is_refused(w, which):
    a = args(w)
    del a[{"missing thread": "thread", "missing spec": "spec", "missing summary": "summary", "missing quotes": "quote_ids"}[which]]
    before = counts(w)
    assert refused(call(w["launch"], a, name="propose_envelope")) and counts(w) == before


def test_a_desk_message_is_not_his_to_quote(w):
    """Failure: a card quoting a line the desk wrote as if he had said it."""
    desk = say(w["store"], w["thread"], "the desk's line", n=9, author=ROLE_DESK)
    before = counts(w)
    assert refused(envelope(w, quote_ids=[desk])) and counts(w) == before


def test_a_closed_or_non_work_thread_is_refused(w):
    before = counts(w)
    assert refused(envelope(w, thread=w["thread"]))                      # the room's own note thread
    w["store"].set_state(w["work"], "closed", by="operator")
    assert refused(envelope(w))
    assert counts(w) == before


@pytest.mark.parametrize("bad", ["‮", "​", "\x07", " "])
def test_unshowable_characters_are_refused_in_a_value_or_a_key(w, bad):
    """Failure (M5): an override a person cannot see reaching a card, a draft and a signature sheet."""
    before = counts(w)
    reply = envelope(w, spec=spec(w, where=[{"repo": w["path"] + bad, "paths": ["README"]}]))
    assert refused(reply) and "unshowable" in why(reply), why(reply)
    reply = envelope(w, spec={"stop" + bad: "2h"})                   # a key: refused, and never echoed raw
    assert refused(reply) and bad not in why(reply), why(reply)
    assert counts(w) == before


def test_the_escaped_form_of_an_override_is_refused_through_the_frame(w):
    """Failure (M5): the escaped U+202E arrives as the character after the frame is parsed; it must not pass."""
    frame = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
        "name": "propose_envelope", "arguments": args(w, spec=spec(w, where=[{"repo": w["path"] + "‮", "paths": ["a"]}]))}})
    assert "\\u202e" in frame and "‮" not in frame
    out = io.StringIO()
    room_mcp.serve(w["launch"], io.StringIO(frame + "\n"), out)
    reply = json.loads(out.getvalue())
    assert refused(reply) and "unshowable" in why(reply), why(reply)
    assert counts(w)["spec_proposals"] == 0


def test_a_repeated_key_in_a_frame_is_refused(w):
    """Failure: a duplicate key silently keeping the last value, so the card shows one budget and the model sent two."""
    good = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
        "name": "propose_envelope", "arguments": args(w)}})
    dup = good.replace('"stop": "2h"', '"stop": "2h", "stop": "20h"')
    assert dup != good
    out = io.StringIO()
    room_mcp.serve(w["launch"], io.StringIO(dup + "\n"), out)
    got = json.loads(out.getvalue())["error"]
    assert got["code"] == room_mcp.PARSE_ERROR and "repeated" in got["message"]
    assert counts(w)["spec_proposals"] == 0


@pytest.mark.parametrize("bad", [
    {"budget": "lots", "stop": "2h"},
    {"stop": "soon"},
    {"stop": 5},
    {"lands": [{"from": "source"}], "stop": "2h"},                  # a land from source: the room must name repo and branch
])
def test_a_spec_the_grammar_refuses_never_becomes_a_card(w, bad):
    """Failure: a card whose tap can only fail (the dry run is parse_spec, the tap's own reader)."""
    before = counts(w)
    reply = envelope(w, spec=bad)
    assert refused(reply) and "this spec" in why(reply) + "this spec", why(reply)
    assert counts(w) == before


def test_a_card_the_door_made_taps_into_an_unsigned_draft(w):
    """The door's card is the one the cockpit's tap reads: the bytes he posts become a draft, never an envelope."""
    first = envelope(w)
    assert "error" not in first, first
    assert first["result"]["isError"] is False
    c = raw(w["qdir"])
    try:
        pid, text = c.execute("SELECT proposal_id, spec_text FROM spec_proposals").fetchone()
    finally:
        c.close()
    got = spec_card.tap(w["store"], pid, text, "ob-door0001", "cockpit", thread_echo=w["work"], input_sha256=words_sha256(text))
    assert got.draft_id and got.revision >= 1
    c = raw(w["qdir"])
    try:
        assert c.execute("SELECT count(*) FROM envelopes").fetchone()[0] == 0
    finally:
        c.close()


def test_the_launch_must_be_an_active_room_launch(w):
    from tests.test_desk_room_mcp import NOLAUNCH
    before = counts(w)
    assert refused(call(NOLAUNCH, args(w), name="propose_envelope")) and counts(w) == before


# --- the door's tool lists -------------------------------------------------------------------------------------------
def _tools(host):
    reply = room_mcp.handle("ln-" + "0" * 32, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, host)
    return [t["name"] for t in reply["result"]["tools"]]


def test_the_envelope_tool_is_in_every_door_list_and_only_propose_drop_is_retired():
    """Failure: a relay host retiring the envelope tool along with propose_drop, or no host losing it."""
    assert room_host.door_tools(None) == ("propose_drop", "propose_envelope")
    for relay in (False, True):
        host = room_host.HostCommands(relay=("/bin/relay",) if relay else None, relay_without_tap=relay,
                                      replies=("/bin/replies",), note=("/bin/note",))
        names = room_host.door_tools(host)
        assert "propose_envelope" in names and ("propose_drop" in names) is (not relay)
        assert _tools(host) == list(names)
    assert _tools(None) == ["propose_drop", "propose_envelope"]


def test_a_tool_the_host_did_not_list_is_unknown(w):
    reply = call(w["launch"], args(w), name="propose_nothing")
    assert "error" in reply


# --- the containment digest hashes the door the spawn really had ------------------------------------------------------
def _confined_policy(door):
    import json as _json
    from levain.desk.claude_session import ROOM_DENIED_TOOLS, ROOM_TOOLS
    from levain.desk.room_launch import ROOM_PERMISSION_MODE
    return {"tools": list(ROOM_TOOLS), "denied": sorted(ROOM_DENIED_TOOLS), "mode": ROOM_PERMISSION_MODE,
            "setting_sources": "user", "settings": {}, "door": door, "readguard_sha256": "g"}


def _sha(policy):
    import hashlib
    import json as _json
    return hashlib.sha256(_json.dumps(policy, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def test_the_confined_digest_hashes_the_real_door_list_so_adding_a_tool_moves_it():
    """Failure: ``"door": ROOM_MCP_TOOL`` hard-coded, a digest that describes a sandbox with one tool when it has two."""
    from levain.desk.room_launch import containment_digest
    got = containment_digest("{}", "g", None)
    assert got == _sha(_confined_policy(["propose_drop", "propose_envelope"]))
    assert got != _sha(_confined_policy("mcp__levain_room__propose_drop"))           # what it hashed before the tool
    relay = room_host.HostCommands(relay=("/bin/relay",), relay_without_tap=True)
    assert containment_digest("{}", "g", relay) != got
    with_relay = _confined_policy(["relay_to_desk", "propose_envelope"])
    with_relay["host_commands"] = json.loads(relay.to_json())
    assert containment_digest("{}", "g", relay) == _sha(with_relay)


# --- L3 round 1 (h14c) -------------------------------------------------------------------------------------------------
def test_a_refused_call_with_no_store_makes_no_store(tmp_path, monkeypatch):
    """codex L3 r1 MED: the dry run opened the store to read its id, and opening one that is not there MADE it: a refused
    call left a database behind."""
    from tests.test_desk_room_mcp import NOLAUNCH
    monkeypatch.setenv("LEVAIN_HOME", str(tmp_path / "empty"))
    reply = call(NOLAUNCH, {"quote_ids": ["cm-" + "0" * 32], "thread": "th-" + "0" * 32,
                            "spec": {"budget": dict(BUDGET), "stop": "2h"}, "summary": "s"}, name="propose_envelope")
    assert refused(reply), reply
    assert not (tmp_path / "empty").exists()


@pytest.mark.parametrize("bad", [
    lambda w: {"pushes": [1], "stop": "2h"},
    lambda w: {"budget": {"hours": True}, "stop": "2h"},
    lambda w: {"where": [{"repo": w["path"], "paths": ["../escape"]}], "stop": "2h"},
])
def test_a_value_the_draft_writer_refuses_never_becomes_a_card(w, bad):
    """codex L3 r1 MED: a card advertised as dry-run valid whose tap could only roll back, and whose preview could crash."""
    before = counts(w)
    reply = envelope(w, spec=bad(w))
    assert refused(reply) and "this spec" in why(reply), why(reply)
    assert counts(w) == before


def test_a_spec_nested_too_deeply_is_a_named_refusal(w):
    """complement L3 r1 LOW: the indented dump recursed outside the try, so deep nesting was an internal error."""
    deep: list = []
    for _ in range(3000):
        deep = [deep]
    before = counts(w)
    reply = envelope(w, spec={"where": deep, "stop": "2h"})
    assert refused(reply) and "error" not in reply, reply
    assert counts(w) == before

"""The Desk tab's port (levain#2 lane A): the contract tests for :mod:`levain.desk.desk_port`.

The host-command port is driven with a fake argv script (as ``tests/test_room_host_d2.py`` does); the double is held to the
same contract, since lanes B and C build against it. No real flow script runs here."""
from __future__ import annotations

import dataclasses
import inspect
import json
import os
import sqlite3
import sys

import pytest

from levain.desk import desk_port, room_host
from levain.desk.desk_port import (Armed, Delivery, DeskCaps, DeskCommandError, DeskCommands, DeskEvent, DeskPort,
                                   DeskPortError, EventBatch, HostCommandDeskPort, LeaseView, OutboxConflict, PostResult,
                                   ScriptedDeskPort, WakeAdapter)
from levain.desk.room_host import HostCommands

THREAD = "th-0abcdef-a1b2c3"
ORIGIN = "cockpit:phill"
FAKE = """\
import json, os, signal, sys, time
mode, rec = sys.argv[1], sys.argv[2]
if mode == "post":
    text = sys.stdin.read()
    open(rec, "a").write(json.dumps({"argv": sys.argv[1:], "stdin": text}) + "\\n")
    if "HANG" in text:
        time.sleep(30)
    if "KILL" in text:
        os.kill(os.getpid(), signal.SIGKILL)
    if "FAIL" in text:
        print("Not sent: the inbox did not take it (OSError).", file=sys.stderr)
        sys.exit(1)
    if "REFUSE" in text:
        print("The desk takes 2000 characters at most.")
        sys.exit(2)
    if "NOTHREAD" in text:
        print("Filed.")
        sys.exit(0)
    print("Filed in the inbox. (thread %s)" % "th-0abcdef-a1b2c3")
else:
    open(rec, "a").write(json.dumps({"argv": sys.argv[1:], "stdin": sys.stdin.read()}) + "\\n")
    src = sys.argv[3]
    if src.endswith(".fail"):
        sys.exit(1)
    sys.stdout.write(open(src).read())
"""


class Crash(BaseException):
    """A process crash mid-command: not an Exception, so nothing in the port may swallow it."""


@pytest.fixture
def rig(tmp_path):
    script = tmp_path / "fake_host.py"
    script.write_text(FAKE)
    rec = tmp_path / "rec.jsonl"
    lease_src = tmp_path / "lease.json"
    events_src = tmp_path / "events.json"
    lease_src.write_text(json.dumps({"state": "held", "holder": "desk 25", "age_s": 180}))
    events_src.write_text(json.dumps({"events": [{"by": "script", "kind": "took", "text": "took it", "at": "10:42:01"}],
                                      "cursor": "c1"}))
    py = sys.executable
    cmds = DeskCommands(post=(py, str(script), "post", str(rec)), lease=(py, str(script), "lease", str(rec), str(lease_src)),
                        events=(py, str(script), "events", str(rec), str(events_src)))
    claims = tmp_path / "claims.sqlite"

    class Rig:
        pass
    r = Rig()
    r.cmds, r.claims, r.rec, r.lease_src, r.events_src, r.script, r.py, r.tmp = (cmds, claims, rec, lease_src, events_src,
                                                                                 script, py, tmp_path)
    r.port = lambda **kw: HostCommandDeskPort(cmds, claims, **kw)
    r.recs = lambda: [json.loads(x) for x in rec.read_text().splitlines()] if rec.exists() else []
    return r


def oid(n: int = 1) -> str:
    return f"outbox-test-{n:04d}"


# --- the pinned shapes: lanes B and C build against these names ------------------------------------------------------------
def test_the_result_dataclasses_have_their_pinned_field_names():
    names = lambda c: [f.name for f in dataclasses.fields(c)]      # noqa: E731
    assert names(DeskCaps) == ["adapter", "wake", "receipts", "lease", "replies", "words_max"]
    assert names(LeaseView) == ["state", "holder", "age_s"]
    assert names(PostResult) == ["outcome", "ref", "thread_id", "reason", "replayed"]
    assert names(DeskEvent) == ["by", "kind", "text", "at"]
    assert names(EventBatch) == ["events", "cursor", "readable"]
    assert names(Delivery) == ["outcome", "detail"]
    assert names(Armed) == ["state", "until"]
    assert names(DeskCommands) == ["post", "lease", "events", "words_max"]
    assert all(c.__dataclass_params__.frozen for c in (DeskCaps, LeaseView, PostResult, DeskEvent, EventBatch, Delivery,
                                                      Armed, DeskCommands))


def test_the_outbox_id_shape_is_the_rooms_own():
    from levain.desk import chat
    assert desk_port._OUTBOX_ID.pattern == chat._OUTBOX_ID.pattern


def test_the_origin_name_limit_is_the_turn_leases_own():
    """A credential name the lease accepts as an owner is one the port accepts as an origin (a valid 65-128 character
    name once passed ``check_owner`` and 400'd every send); both refuse one past the lease's limit."""
    from levain.desk import chat_lease
    for surface in ("cockpit", "phone"):
        for n in (1, 64, 65, 128):
            owner = f"{surface}:" + "a" * n
            assert chat_lease.check_owner(owner) == owner
            desk_port._check_ids(oid(), owner)
        too_long = f"{surface}:" + "a" * 129
        with pytest.raises(chat_lease.TurnRefused):
            chat_lease.check_owner(too_long)
        with pytest.raises(DeskPortError):
            desk_port._check_ids(oid(), too_long)
    assert desk_port._ORIGIN.pattern.endswith(chat_lease._CLIENT_ID)


def test_a_port_with_no_post_command_says_it_cannot_post(rig):
    assert HostCommandDeskPort(DeskCommands(lease=rig.cmds.lease), rig.claims).can_post is False
    assert rig.port().can_post is True


def test_the_enum_fields_refuse_a_value_outside_their_set():
    for build in (lambda: LeaseView("maybe"), lambda: PostResult("sent"), lambda: DeskEvent("flow", "took", "x", "t"),
                  lambda: DeskEvent("script", "read", "x", "t"), lambda: Delivery("ok"), lambda: Armed("yes"),
                  lambda: DeskCaps("a", "push", True, True, True, 10)):
        with pytest.raises(ValueError):
            build()
    with pytest.raises(ValueError):
        LeaseView("none", holder="desk 1")         # only a held lease carries a holder
    with pytest.raises(ValueError):
        EventBatch((), "c1", readable=False)       # an unreadable batch is empty


def test_the_port_signatures_are_the_designs():
    assert list(inspect.signature(DeskPort.post).parameters) == ["self", "words", "outbox_id", "origin"]
    assert list(inspect.signature(DeskPort.events).parameters) == ["self", "thread_id", "after"]
    assert list(inspect.signature(DeskPort.lease).parameters) == ["self"]
    assert list(inspect.signature(DeskPort.capabilities).parameters) == ["self"]
    assert list(inspect.signature(WakeAdapter.armed).parameters) == ["self"]


def test_a_wake_adapter_rings_an_entry_id_and_has_no_words_parameter():
    params = inspect.signature(WakeAdapter.ring).parameters
    assert list(params) == ["self", "entry_id"] and "words" not in params

    class Bell:
        harness = "claude-code"
        mode = "pull"

        def ring(self, entry_id):
            return Delivery("delivered")

        def armed(self):
            return Armed("unknown")
    assert isinstance(Bell(), WakeAdapter) and not isinstance(object(), WakeAdapter)
    assert Bell().ring("e-1").outcome == "delivered" and Bell().armed().state == "unknown"


def test_both_ports_are_desk_ports(rig):
    assert isinstance(rig.port(), DeskPort) and isinstance(ScriptedDeskPort(), DeskPort)


# --- the commands are the desk's own: never the Room's -----------------------------------------------------------------------
def test_host_commands_key_set_holds_none_of_the_desk_keys():
    room_keys = {f.name for f in dataclasses.fields(HostCommands)}
    desk_keys = {f.name for f in dataclasses.fields(DeskCommands)}
    assert room_keys == {"relay", "replies", "relay_without_tap", "note"}   # "note": main's note_to_desk (80f48f69b) landed after this pin
    assert not room_keys & desk_keys, "a key in both is a desk argv the Room's model environment could carry"
    assert not issubclass(DeskCommands, HostCommands) and not issubclass(HostCommands, DeskCommands)
    assert desk_port.DESKCMD_ENV != room_host.HOSTCMD_ENV
    # neither door accepts the other's keys, or silently ignores them
    py = sys.executable
    with pytest.raises(room_host.HostCommandError):
        room_host.from_env(json.dumps({"post": [py, "/x"]}))
    with pytest.raises(room_host.HostCommandError):
        room_host.from_env(json.dumps({"relay": [py, "/x"], "events": [py, "/x"]}))
    with pytest.raises(DeskCommandError):
        DeskCommands.from_env(json.dumps({"relay": [py, "/x"]}))
    with pytest.raises(DeskCommandError):
        DeskCommands.from_env(json.dumps({"post": [py, "/x"], "relay_without_tap": True}))


def test_desk_commands_construction_is_strict():
    py = sys.executable
    assert DeskCommands.from_env(None) is None and DeskCommands.from_env("") is None
    ok = DeskCommands.from_env(json.dumps({"post": [py, "/x/post"], "words_max": 500}))
    assert ok.post == (py, "/x/post") and ok.lease is None and ok.words_max == 500
    assert DeskCommands.from_env(ok.to_json()) == ok
    for bad in ('{"post": "/bin/true"}', '{"post": ["relative"]}', '{"post": []}', '{"post": [""]}', '{"words_max": 0}',
                '{"words_max": true}', "[]", "not json", '{"post": ["/a\\u0000b"]}'):
        with pytest.raises(DeskCommandError):
            DeskCommands.from_env(bad)


def test_the_port_will_not_take_a_non_desk_commands_object():
    with pytest.raises(DeskPortError):
        HostCommandDeskPort(HostCommands(relay=(sys.executable, "/x")), "/tmp/never")      # type: ignore[arg-type]


# --- post: argv shape and the stdin rule ----------------------------------------------------------------------------------
def test_words_ride_stdin_and_argv_carries_none_of_them(rig):
    words = "line one with SECRETWORD\nline two: ‮ bidi, quote \" and $(x)\n"
    r = rig.port().post(words, outbox_id=oid(), origin=ORIGIN)
    assert r.outcome == "accepted"
    (rec,) = rig.recs()
    assert rec["stdin"] == words
    assert rec["argv"] == ["post", str(rig.rec)], "post appends nothing to the host's argv"
    joined = " ".join(rec["argv"])
    assert "SECRETWORD" not in joined and ORIGIN not in joined and oid() not in joined


def test_post_runs_the_argv_without_a_shell(rig, monkeypatch):
    seen = []
    real = room_host.run

    def spy(argv, stdin_text="", extra=(), *, timeout=room_host.TIMEOUT):
        seen.append((argv, stdin_text, extra))
        return real(argv, stdin_text, extra, timeout=timeout)
    monkeypatch.setattr(room_host, "run", spy)
    rig.port().post("hello", outbox_id=oid(), origin=ORIGIN)
    ((argv, stdin_text, extra),) = seen
    assert argv == rig.cmds.post and stdin_text == "hello" and extra == ()
    assert os.path.isabs(argv[0])


# --- post: outcome mapping ------------------------------------------------------------------------------------------------
def test_exit_zero_is_accepted_with_the_ref_and_the_thread(rig):
    r = rig.port().post("hi", outbox_id=oid(), origin=ORIGIN)
    assert (r.outcome, r.thread_id, r.replayed) == ("accepted", THREAD, False)
    assert r.ref == f"Filed in the inbox. (thread {THREAD})"


def test_exit_zero_without_a_thread_line_is_accepted_with_no_thread(rig):
    r = rig.port().post("NOTHREAD", outbox_id=oid(), origin=ORIGIN)
    assert (r.outcome, r.ref, r.thread_id) == ("accepted", "Filed.", None)


def test_exit_two_is_the_hosts_refusal_and_not_delivered(rig):
    r = rig.port().post("REFUSE this", outbox_id=oid(), origin=ORIGIN)
    assert (r.outcome, r.reason, r.ref) == ("not_delivered", "The desk takes 2000 characters at most.", None)


def test_any_other_exit_after_it_started_is_ambiguous_and_carries_the_hosts_line(rig):
    r = rig.port().post("FAIL", outbox_id=oid(), origin=ORIGIN)
    assert r.outcome == "ambiguous" and "exit 1" in r.reason and "inbox did not take it" in r.reason
    assert "not sent again" in r.reason and len(r.reason) <= 300


def test_a_timeout_is_ambiguous(rig):
    r = rig.port(timeout=1.0).post("HANG", outbox_id=oid(), origin=ORIGIN)
    assert r.outcome == "ambiguous" and "did not finish" in r.reason


def test_a_killed_command_is_ambiguous(rig):
    r = rig.port().post("KILL", outbox_id=oid(), origin=ORIGIN)
    assert r.outcome == "ambiguous" and "killed" in r.reason


def test_a_program_that_never_started_delivered_nothing(rig):
    cmds = DeskCommands(post=("/nonexistent/levain-desk-post",))
    r = HostCommandDeskPort(cmds, rig.claims).post("hi", outbox_id=oid(), origin=ORIGIN)
    assert r.outcome == "not_delivered" and "nothing was sent" in r.reason


def test_a_door_that_raises_mid_command_is_ambiguous_and_stays_claimed(rig, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("the runner broke")
    monkeypatch.setattr(room_host, "run", boom)
    assert rig.port().post("hi", outbox_id=oid(), origin=ORIGIN).outcome == "ambiguous"
    monkeypatch.undo()
    again = rig.port().post("hi", outbox_id=oid(), origin=ORIGIN)
    assert (again.outcome, again.replayed) == ("ambiguous", True) and rig.recs() == []


# --- post: idempotency on outbox_id ---------------------------------------------------------------------------------------
def test_the_same_outbox_id_runs_the_command_once_and_replays_the_answer(rig):
    port = rig.port()
    first = port.post("hello", outbox_id=oid(), origin=ORIGIN)
    again = port.post("hello", outbox_id=oid(), origin=ORIGIN)
    assert len(rig.recs()) == 1
    assert (again.outcome, again.ref, again.thread_id, again.replayed) == (first.outcome, first.ref, first.thread_id, True)
    assert rig.port().post("hello", outbox_id=oid(2), origin=ORIGIN).replayed is False      # a new id is a new letter
    assert len(rig.recs()) == 2


def test_idempotency_survives_a_restart(rig):
    first = rig.port().post("hello", outbox_id=oid(), origin=ORIGIN)
    restarted = rig.port()                       # a new process: a new port on the same claims file
    again = restarted.post("hello", outbox_id=oid(), origin=ORIGIN)
    assert len(rig.recs()) == 1 and again.replayed and again.thread_id == first.thread_id


def test_a_failed_post_is_not_retried_under_the_same_id(rig):
    port = rig.port()
    assert port.post("FAIL", outbox_id=oid(), origin=ORIGIN).outcome == "ambiguous"
    again = port.post("FAIL", outbox_id=oid(), origin=ORIGIN)
    assert (again.outcome, again.replayed) == ("ambiguous", True) and len(rig.recs()) == 1
    refused = port.post("REFUSE", outbox_id=oid(2), origin=ORIGIN)
    assert port.post("REFUSE", outbox_id=oid(2), origin=ORIGIN) == dataclasses.replace(refused, replayed=True)


def test_the_claim_is_written_before_the_command_runs(rig, monkeypatch):
    seen = {}

    def peek(argv, stdin_text="", extra=(), *, timeout=room_host.TIMEOUT):
        conn = sqlite3.connect(str(rig.claims))
        try:
            seen["row"] = conn.execute("SELECT state, words_sha256 FROM desk_post_claims WHERE outbox_id = ?",
                                       (oid(),)).fetchone()
        finally:
            conn.close()
        raise Crash()
    monkeypatch.setattr(room_host, "run", peek)
    with pytest.raises(Crash):
        rig.port().post("hello", outbox_id=oid(), origin=ORIGIN)
    assert seen["row"] is not None and seen["row"][0] == "claimed", "the claim must be committed before the command runs"


def test_a_crash_mid_command_is_ambiguous_after_a_restart_and_never_a_double(rig, monkeypatch):
    def crash(*a, **k):
        raise Crash()
    monkeypatch.setattr(room_host, "run", crash)
    with pytest.raises(Crash):
        rig.port().post("hello", outbox_id=oid(), origin=ORIGIN)
    monkeypatch.undo()
    again = rig.port().post("hello", outbox_id=oid(), origin=ORIGIN)      # the restarted process: the same letter again
    assert (again.outcome, again.replayed) == ("ambiguous", True)
    assert rig.recs() == [], "the command must not run a second time for one outbox_id"


def test_the_same_id_with_other_words_is_a_conflict_and_runs_nothing(rig):
    port = rig.port()
    port.post("hello", outbox_id=oid(), origin=ORIGIN)
    with pytest.raises(OutboxConflict):
        port.post("different words", outbox_id=oid(), origin=ORIGIN)
    assert len(rig.recs()) == 1


def test_a_bad_outbox_id_or_origin_is_a_caller_fault(rig):
    port = rig.port()
    for kw in ({"outbox_id": "short", "origin": ORIGIN}, {"outbox_id": oid(), "origin": "someone"},
               {"outbox_id": oid(), "origin": "cockpit:"}, {"outbox_id": "x" * 129, "origin": ORIGIN}):
        with pytest.raises(DeskPortError):
            port.post("hi", **kw)
    assert rig.recs() == []


def test_the_claims_file_is_private(rig):
    rig.port()
    assert oct(os.stat(rig.claims).st_mode & 0o777) == "0o600"


# --- post: the cap refuses, never cuts ------------------------------------------------------------------------------------
def test_words_over_the_cap_are_refused_not_cut_and_nothing_is_claimed(rig):
    port = rig.port()
    r = port.post("x" * (rig.cmds.words_max + 1), outbox_id=oid(), origin=ORIGIN)
    assert r.outcome == "not_delivered" and str(rig.cmds.words_max + 1) in r.reason and "not sent or cut" in r.reason
    assert rig.recs() == []
    conn = sqlite3.connect(str(rig.claims))
    try:
        assert conn.execute("SELECT COUNT(*) FROM desk_post_claims").fetchone() == (0,)
    finally:
        conn.close()
    # the id is free: the same id with words that fit goes
    assert port.post("fits", outbox_id=oid(), origin=ORIGIN).outcome == "accepted"


def test_words_exactly_at_the_cap_go_whole(rig):
    words = "y" * rig.cmds.words_max
    assert rig.port().post(words, outbox_id=oid(), origin=ORIGIN).outcome == "accepted"
    assert rig.recs()[0]["stdin"] == words


def test_the_hosts_own_lower_cap_binds(rig):
    cmds = DeskCommands(post=rig.cmds.post, words_max=10)
    port = HostCommandDeskPort(cmds, rig.claims)
    assert port.capabilities().words_max == 10
    assert port.post("12345678901", outbox_id=oid(), origin=ORIGIN).outcome == "not_delivered"
    assert rig.recs() == []


@pytest.mark.parametrize("words", ["", "   \n\t", "a\x00b", "lone surrogate \ud800"])
def test_empty_nul_and_unencodable_words_are_refused(rig, words):
    assert rig.port().post(words, outbox_id=oid(), origin=ORIGIN).outcome == "not_delivered"
    assert rig.recs() == []


def test_a_port_with_no_post_command_files_nothing(rig):
    port = HostCommandDeskPort(DeskCommands(lease=rig.cmds.lease), rig.claims)
    r = port.post("hi", outbox_id=oid(), origin=ORIGIN)
    assert r.outcome == "not_delivered" and "no desk wired" in r.reason and rig.recs() == []


# --- lease and events: strict parsing ------------------------------------------------------------------------------------
def test_lease_reads_the_allowlisted_fields_only(rig):
    rig.lease_src.write_text(json.dumps({"state": "held", "holder": "desk 25", "age_s": 180, "pid": 9, "secret": "x"}))
    v = rig.port().lease()
    assert v == LeaseView("held", "desk 25", 180)
    assert {f.name for f in dataclasses.fields(v)} == {"state", "holder", "age_s"}
    (rec,) = rig.recs()
    assert rec["argv"][0] == "lease" and rec["stdin"] == ""


@pytest.mark.parametrize("doc,expect", [
    ({"state": "none"}, LeaseView("none")),
    ({"state": "none", "holder": "desk 3", "age_s": 5}, LeaseView("none")),
    ({"state": "unknown"}, LeaseView("unknown")),
    ({"state": "held"}, LeaseView("held")),
    ({"state": "HELD", "holder": "x"}, LeaseView("unknown")),
    ({"state": "held", "holder": 25, "age_s": 3}, LeaseView("unknown")),
    ({"state": "held", "holder": "d", "age_s": -1}, LeaseView("unknown")),
    ({"state": "held", "holder": "d", "age_s": True}, LeaseView("unknown")),
    ({"state": "held", "holder": "d", "age_s": "3"}, LeaseView("unknown")),
    ({"state": "held", "holder": "h" * 81}, LeaseView("unknown")),
    ({"nothing": 1}, LeaseView("unknown")),
    ([1, 2], LeaseView("unknown")),
    ("held", LeaseView("unknown")),
])
def test_an_unknown_lease_shape_degrades_to_unknown(rig, doc, expect):
    rig.lease_src.write_text(json.dumps(doc))
    assert rig.port().lease() == expect


def test_lease_that_is_not_json_or_fails_or_is_absent_is_unknown(rig):
    rig.lease_src.write_text("held by desk 25")
    assert rig.port().lease() == LeaseView("unknown")
    failing = rig.tmp / "lease.fail"
    failing.write_text("")
    cmds = DeskCommands(lease=(rig.py, str(rig.script), "lease", str(rig.rec), str(failing)))
    assert HostCommandDeskPort(cmds, rig.claims).lease() == LeaseView("unknown")
    assert HostCommandDeskPort(DeskCommands(post=rig.cmds.post), rig.claims).lease() == LeaseView("unknown")


def test_events_reads_a_batch_and_passes_the_thread_then_the_cursor_in_argv(rig):
    port = rig.port()
    batch = port.events(THREAD)
    assert batch == EventBatch((DeskEvent("script", "took", "took it", "10:42:01"),), "c1", True)
    port.events(THREAD, after="c1")
    first, second = rig.recs()
    assert first["argv"][3:] == [THREAD] and second["argv"][3:] == [THREAD, "--after", "c1"]


@pytest.mark.parametrize("doc", [
    {"events": [{"by": "flow", "kind": "took", "text": "x", "at": "t"}]},
    {"events": [{"by": "script", "kind": "read", "text": "x", "at": "t"}]},
    {"events": [{"by": "script", "kind": "took", "text": 5, "at": "t"}]},
    {"events": [{"by": "script", "kind": "took", "at": "t"}]},
    {"events": [{"by": "script", "kind": "took", "text": "x", "at": ""}]},
    {"events": [{"by": "script", "kind": "took", "text": "x", "at": "t"}, "junk"]},
    {"events": [{"by": "script", "kind": "took", "text": "x", "at": "t"}], "cursor": "has space"},
    {"events": [{"by": "script", "kind": "took", "text": "x", "at": "t"}], "cursor": 7},
    {"events": [{"by": "script", "kind": "took", "text": "x", "at": "t"}], "cursor": "-x"},
    {"events": "none"},
    {"cursor": "c1"},
    [],
])
def test_an_unknown_events_shape_is_an_empty_unreadable_batch(rig, doc):
    rig.events_src.write_text(json.dumps(doc))
    assert rig.port().events(THREAD) == EventBatch(readable=False)


def test_events_ignores_fields_outside_the_allowlist(rig):
    rig.events_src.write_text(json.dumps({"events": [{"by": "desk", "kind": "reply", "text": "on it", "at": "t",
                                                      "pid": 4, "html": "<b>"}], "cursor": None, "debug": True}))
    (e,) = rig.port().events(THREAD).events
    assert e == DeskEvent("desk", "reply", "on it", "t")


def test_events_for_a_bad_thread_or_cursor_never_runs_the_command(rig):
    port = rig.port()
    for thread, after in (("not-a-thread", None), ("th-x; rm -rf /", None), (THREAD, "has space"), (THREAD, "--evil")):
        assert port.events(thread, after) == EventBatch(readable=False)
    assert rig.recs() == []


def test_events_that_fail_or_have_no_command_are_unreadable_never_empty_ok(rig):
    failing = rig.tmp / "events.fail"
    failing.write_text("")
    cmds = DeskCommands(events=(rig.py, str(rig.script), "events", str(rig.rec), str(failing)))
    assert HostCommandDeskPort(cmds, rig.claims).events(THREAD).readable is False
    assert HostCommandDeskPort(DeskCommands(post=rig.cmds.post), rig.claims).events(THREAD).readable is False


def test_capabilities_are_asked_not_assumed(rig):
    full = rig.port().capabilities()
    assert full == DeskCaps("host-command", "poll", True, True, True, desk_port.WORDS_MAX)
    bare = HostCommandDeskPort(DeskCommands(post=rig.cmds.post), rig.claims, adapter="flow", wake="event").capabilities()
    assert (bare.adapter, bare.wake, bare.receipts, bare.lease, bare.replies) == ("flow", "event", False, False, False)
    with pytest.raises(ValueError):
        rig.port(wake="sometimes")


# --- the double ---------------------------------------------------------------------------------------------------------
def test_the_double_posts_accepted_by_default_and_runs_a_replayed_id_once():
    d = ScriptedDeskPort()
    first = d.post("hi", outbox_id=oid(), origin=ORIGIN)
    assert first.outcome == "accepted" and first.thread_id is not None and not first.replayed
    again = d.post("hi", outbox_id=oid(), origin=ORIGIN)
    assert again.replayed and again.thread_id == first.thread_id and len(d.posts) == 1
    assert (d.posts[0].words, d.posts[0].outbox_id, d.posts[0].origin) == ("hi", oid(), ORIGIN)
    with pytest.raises(OutboxConflict):
        d.post("other", outbox_id=oid(), origin=ORIGIN)
    assert d.post("hi", outbox_id=oid(2), origin=ORIGIN).thread_id != first.thread_id


def test_the_double_scripts_each_post_outcome_in_order():
    d = ScriptedDeskPort()
    d.script_post("not_delivered", reason="The desk would not take that message.")
    d.script_post("ambiguous", reason="It may not have arrived.")
    d.script_post("accepted", ref="Filed.", thread_id=THREAD)
    got = [d.post("m", outbox_id=oid(i), origin=ORIGIN) for i in (1, 2, 3)]
    assert [g.outcome for g in got] == ["not_delivered", "ambiguous", "accepted"]
    assert got[2].thread_id == THREAD and got[2].ref == "Filed."
    assert d.post("m", outbox_id=oid(4), origin=ORIGIN).outcome == "accepted"      # an empty script is accepted


def test_the_double_refuses_over_the_cap_and_claims_nothing():
    d = ScriptedDeskPort(caps=DeskCaps("scripted", "poll", True, True, True, 5))
    r = d.post("123456", outbox_id=oid(), origin=ORIGIN)
    assert r.outcome == "not_delivered" and d.posts == []
    assert d.post("12345", outbox_id=oid(), origin=ORIGIN).outcome == "accepted"


def test_the_double_scripts_the_lease_states():
    d = ScriptedDeskPort()
    assert d.lease() == LeaseView("unknown")
    d.script_lease("held", "desk 25", 180)
    assert d.lease() == LeaseView("held", "desk 25", 180)
    d.script_lease("none")
    assert d.lease() == LeaseView("none")
    d.caps = DeskCaps("scripted", "poll", True, False, True, 10)           # capabilities are asked: no lease, no holder line
    d.script_lease("held", "desk 25", 1)
    assert d.lease() == LeaseView("unknown")


def test_the_double_scripts_events_with_a_cursor():
    d = ScriptedDeskPort()
    assert d.events(THREAD) == EventBatch((), "0", True)
    d.script_event(THREAD, "script", "took", "Desk took it", "10:42:01")
    d.script_event(THREAD, "script", "held", "No desk holds the lease", "10:42:02")
    first = d.events(THREAD)
    assert [e.kind for e in first.events] == ["took", "held"] and first.cursor == "2"
    d.script_event(THREAD, "desk", "reply", "on it", "10:43:00")
    later = d.events(THREAD, after=first.cursor)
    assert [(e.by, e.kind) for e in later.events] == [("desk", "reply")] and later.cursor == "3"
    assert d.events("th-other").events == ()
    assert d.events(THREAD, after="not-a-cursor") == EventBatch(readable=False)
    d.script_unreadable()
    assert d.events(THREAD) == EventBatch(readable=False)
    d.script_unreadable(False)
    d.caps = DeskCaps("scripted", "poll", False, True, False, 10)           # no receipts: no slot
    assert d.events(THREAD).readable is False


def test_the_double_holds_the_same_caller_fault_checks():
    d = ScriptedDeskPort()
    with pytest.raises(DeskPortError):
        d.post("hi", outbox_id="short", origin=ORIGIN)
    with pytest.raises(DeskPortError):
        d.post("hi", outbox_id=oid(), origin="nobody")

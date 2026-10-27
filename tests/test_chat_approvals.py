"""CH-D1c-C: the approval records. The two tables' CHECKs and indexes, the deterministic durable key, the quarantine row,
the per-harness agent-config classifier, the arrival transaction without the core call, and the durable cancellation
writers (``latch_not_live``, ``record_abstain``, ``record_cancelled``, ``cancel_launch_requests``, ``ambiguous_stops``),
through the real store on real sqlite files."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import MappingProxyType

import pytest

from levain.coordinator import chat
from levain.coordinator import chat_approvals as ca
from levain.coordinator.chat import ChatStore, record_check, record_event
from levain.coordinator.chat_approvals import (CANCEL_CAUSES, CancelRegistry, ChatLaunchContext, agent_config_tagged,
                                               ambiguous_stops, append_event, approval, arrive, cancel_launch_requests,
                                               claude_raw_key, codex_command_call, codex_file_change_call,
                                               codex_raw_key, durable_key, latch_not_live, quarantine_raw_key,
                                               record_abstain, record_cancelled, record_quarantine)
from levain.coordinator.envelopes import EnvelopeStore
from levain.coordinator.store import CoordError, Part

T0 = datetime(2026, 10, 27, 12, 0, tzinfo=timezone.utc)
NONCE = "0123456789abcdef0123456789abcdef"
NONCE2 = "fedcba9876543210fedcba9876543210"
VERDICT = "ab" * 32
DIGEST = "cd" * 32


class Clock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        self.now += timedelta(seconds=1)
        return self.now


@pytest.fixture
def store(tmp_path):
    d = tmp_path / "consent"
    d.mkdir(mode=0o700)
    return EnvelopeStore(d, clock=Clock())


def _world(store, harness):
    store.coord_set_agent("none")
    token = store.coord_take()
    d = store.accept_drop("fix the build", surface="cli")[0]
    thread = store.split(d.drop_id, [Part("work", "fix the build")])[0]
    ln = store.launch_request(token, harness=harness, role="worker", threads=[thread.thread_id])
    got, why = store.launch_activate(ln.launch_id, harness_session_id=f"sess-{harness}-1")
    assert why is None and got.state == "active"
    return {"thread": thread.thread_id, "launch": ln.launch_id, "harness": harness}


@pytest.fixture
def cs(store):
    s = ChatStore(store)
    s._connect().close()
    return s


@pytest.fixture
def claude(store, cs):
    w = _world(store, "claude")
    _activate(cs, w["launch"])
    return w


@pytest.fixture
def codex(store, cs):
    w = _world(store, "codex")
    _live(cs, w["launch"])
    return w


def _live(cs, launch, nonce=None):
    with cs._write() as (conn, now):
        if nonce is None:
            record_check(conn, now, launch, "probing", "activation", "p")
            record_check(conn, now, launch, "live", "activation", "ok")
        else:
            record_check(conn, now, launch, "probing", "activation", "p", spawn_nonce=nonce)
            record_check(conn, now, launch, "live", "activation", "ok", spawn_nonce=nonce, canary_verdict_id=VERDICT,
                         containment_digest=DIGEST)


def _activate(cs, launch, nonce=NONCE):
    _live(cs, launch, nonce)


def ctx(tmp_path, **env):
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    cwd = tmp_path / "work"
    cwd.mkdir(exist_ok=True)
    return ChatLaunchContext(queue_dir=tmp_path / "q", cwd=str(cwd), home=home, environ=MappingProxyType(dict(env)),
                             spawn_nonce=NONCE, claude_pid=4242)


def host(cs, launch, c, *, tool_use_id="toolu_1", tool="Bash", tool_input=None, turn="t-1", nonce=NONCE):
    tool_input = {"command": "ls"} if tool_input is None else tool_input
    with cs._write() as (conn, now):
        return arrive(conn, now, launch_id=launch, raw_vendor_key=claude_raw_key(nonce, tool_use_id),
                      transport_key=f"req-{tool_use_id}", vendor_turn_id=turn, vendor_call_id=tool_use_id, approval_id=None, kind="host",
                      available_decisions=("allow", "deny"), tool_name=tool, tool_input=tool_input, ctx=c,
                      spawn_nonce=nonce)


def rows(cs, sql, *args):
    return cs._read(lambda conn: conn.execute(sql, args).fetchall())


def events(cs, launch, key):
    return [r[0] for r in rows(cs, "SELECT kind FROM chat_approval_events WHERE launch_id = ? AND vendor_request_key = ? "
                                   "ORDER BY seq", launch, key)]


# --- the durable key ---------------------------------------------------------------------------------------------
def test_durable_key_is_deterministic_on_replay(cs, claude, tmp_path):
    c = ctx(tmp_path)
    first = host(cs, claude["launch"], c)
    again = host(cs, claude["launch"], c)
    assert first.disposition == "routed" and again.disposition == "replay"
    assert first.row.vendor_request_key == again.row.vendor_request_key
    assert rows(cs, "SELECT COUNT(*) FROM chat_approvals")[0][0] == 1


def test_durable_key_has_no_store_local_suffix():
    key = durable_key("can_use_tool:" + NONCE + ":toolu_1", "Bash", "jcs:abc")
    raw = "can_use_tool:" + NONCE + ":toolu_1"
    assert key == raw + ":call:" + hashlib.sha256(b"Bash\0jcs:abc").hexdigest()
    assert len(key) == len(raw) + 70 and "#" not in key


@pytest.mark.parametrize("suffix", ["g" * 64, "0" * 63 + "A"])
def test_durable_key_suffix_must_be_lower_hex(cs, claude, tmp_path, suffix):
    raw = claude_raw_key(NONCE, "toolu_1")
    with pytest.raises(sqlite3.IntegrityError):
        with cs._write() as (conn, now):
            conn.execute("INSERT INTO chat_approvals (launch_id, vendor_request_key, gseq, raw_vendor_key, "
                         "transport_key, vendor_turn_id, kind, route, available_decisions, tool_name, tool_input_digest, "
                         "spawn_nonce, at) VALUES (?, ?, ?, ?, 'r', 't', 'host', 'core', '[]', 'Bash', 'jcs:x', ?, ?)",
                         (claude["launch"], raw + ":call:" + suffix, chat.next_gseq(conn, now), raw, NONCE, now))


def test_raw_key_stored_and_session_addressed_by_it(cs, claude, tmp_path):
    got = host(cs, claude["launch"], ctx(tmp_path))
    raw = claude_raw_key(NONCE, "toolu_1")
    assert raw == f"can_use_tool:{NONCE}:toolu_1"
    assert got.row.raw_vendor_key == raw and got.row.vendor_request_key.startswith(raw + ":call:")
    assert codex_raw_key("item/commandExecution/requestApproval", "it-1", None).endswith(":it-1:-")


def test_resumed_child_reused_request_id_is_a_new_key(cs, claude, tmp_path):
    """A resumed launch is a new child, so its nonce differs; the transport request_id is not in the key at all."""
    c = ctx(tmp_path)
    one = host(cs, claude["launch"], c)
    _activate(cs, claude["launch"], NONCE2)
    two = host(cs, claude["launch"], c, nonce=NONCE2)
    assert one.row.vendor_request_key != two.row.vendor_request_key
    assert one.row.vendor_request_key.startswith(f"can_use_tool:{NONCE}:") and \
        two.row.vendor_request_key.startswith(f"can_use_tool:{NONCE2}:")


def test_same_key_different_input_is_new_request(cs, claude, tmp_path):
    c = ctx(tmp_path)
    a = host(cs, claude["launch"], c, tool_input={"command": "ls"})
    b = host(cs, claude["launch"], c, tool_input={"command": "rm -rf /"})
    assert a.row.raw_vendor_key == b.row.raw_vendor_key
    assert a.row.vendor_request_key != b.row.vendor_request_key and b.disposition == "routed"


# --- the quarantine row (R7-7) ----------------------------------------------------------------------------------
def test_claude_request_without_tool_use_id_denied(cs, claude, tmp_path):
    """No tool_use_id: no key from the request_id (codex defect 5). The keyable path refuses, and the line is a
    quarantine row whose only offered answer is a deny and whose route is never the core."""
    with pytest.raises(CoordError, match="tool_use_id"):
        claude_raw_key(NONCE, None)
    with pytest.raises(CoordError, match="tool_use_id"):
        claude_raw_key(NONCE, 17)
    line = b'{"type":"control_request","request_id":"r1","request":{"subtype":"can_use_tool","tool_name":"Bash"}}'
    with cs._write() as (conn, now):
        got = record_quarantine(conn, now, launch_id=claude["launch"], spawn_nonce=NONCE, raw_line=line, transport_key="req-q")
    assert got.disposition == "quarantined"
    assert (got.row.kind, got.row.route, got.row.tool_name, got.row.available_decisions) == \
        ("quarantine", "quarantine", "", ("deny",))
    assert got.row.targets_agent_config is True


def test_quarantine_key_is_sha_of_raw_bytes(cs, claude):
    a, b = b'{"a":1}', b'{"a": 1}'
    with cs._write() as (conn, now):
        r1 = record_quarantine(conn, now, launch_id=claude["launch"], spawn_nonce=NONCE, raw_line=a, transport_key="req-q")
        r2 = record_quarantine(conn, now, launch_id=claude["launch"], spawn_nonce=NONCE, raw_line=b, transport_key="req-q")
        again = record_quarantine(conn, now, launch_id=claude["launch"], spawn_nonce=NONCE, raw_line=a, transport_key="req-q")
    assert r1.row.vendor_request_key != r2.row.vendor_request_key
    assert again.disposition == "replay" and again.row.vendor_request_key == r1.row.vendor_request_key
    raw, digest = quarantine_raw_key(NONCE, a)
    assert digest == hashlib.sha256(a).hexdigest() and raw == f"quarantine:{NONCE}:{digest}"
    assert r1.row.raw_vendor_key == raw and r1.row.vendor_request_key == durable_key(raw, "", digest)


def test_quarantine_never_calls_core_or_cards(cs, claude):
    with cs._write() as (conn, now):
        q = record_quarantine(conn, now, launch_id=claude["launch"], spawn_nonce=NONCE, raw_line=b"junk", transport_key="req-q")
    assert q.row.route != "core"
    assert record_abstain(cs, launch_id=claude["launch"], vendor_request_key=q.row.vendor_request_key,
                          precursor={"cause": "abstained:hold_expired"}) == "no_card"
    assert events(cs, claude["launch"], q.row.vendor_request_key) == []


@pytest.mark.parametrize("change", [
    ("kind", "'host'"), ("route", "'core'"), ("tool_name", "'Bash'"), ("targets_agent_config", "0"),
    ("raw_vendor_key", "'quarantine:x'")])
def test_quarantine_row_checks(cs, claude, change):
    """Each CHECK of rev 7: a quarantine row is kind = route = quarantine, nameless, tagged, and keyed by its bytes."""
    raw, digest = quarantine_raw_key(NONCE, b"x")
    values = {"kind": "'quarantine'", "route": "'quarantine'", "tool_name": "''", "targets_agent_config": "1",
              "raw_vendor_key": f"'{raw}'"}
    values[change[0]] = change[1]
    key = durable_key(values["raw_vendor_key"].strip("'"), "", digest)
    with pytest.raises(sqlite3.IntegrityError):
        with cs._write() as (conn, now):
            conn.execute(
                "INSERT INTO chat_approvals (launch_id, vendor_request_key, gseq, raw_vendor_key, transport_key, "
                "vendor_turn_id, kind, route, available_decisions, tool_name, tool_input_digest, targets_agent_config, "
                "spawn_nonce, at) "
                f"VALUES (?, ?, ?, {values['raw_vendor_key']}, 'r', '-', {values['kind']}, {values['route']}, '[]', "
                f"{values['tool_name']}, ?, {values['targets_agent_config']}, ?, ?)",
                (claude["launch"], key, chat.next_gseq(conn, now), digest, NONCE, now))


def test_host_row_needs_a_nonce(cs, claude):
    raw = claude_raw_key(NONCE, "toolu_9")
    with pytest.raises(sqlite3.IntegrityError):
        with cs._write() as (conn, now):
            conn.execute("INSERT INTO chat_approvals (launch_id, vendor_request_key, gseq, raw_vendor_key, "
                         "transport_key, vendor_turn_id, kind, route, available_decisions, tool_name, tool_input_digest, "
                         "at) VALUES (?, ?, ?, ?, 'r', 't', 'host', 'core', '[]', 'Bash', 'jcs:x', ?)",
                         (claude["launch"], durable_key(raw, "Bash", "jcs:x"), chat.next_gseq(conn, now), raw, now))


# --- the five classifier tests (R5) -----------------------------------------------------------------------------
def test_agent_config_classifier_claude_settings_edit(tmp_path):
    c = ctx(tmp_path)
    assert agent_config_tagged("claude", "Edit", {"file_path": f"{c.cwd}/.claude/settings.json"}, c) is True
    assert agent_config_tagged("claude", "Edit", {"file_path": f"{c.cwd}/notes.txt"}, c) is False


def test_agent_config_classifier_codex_apply_patch_codex_config_toml(tmp_path):
    c = ctx(tmp_path)
    tool, inp = codex_file_change_call([["update", ".codex/config.toml"]])
    assert agent_config_tagged("codex", tool, inp, c) is True
    tool, inp = codex_file_change_call([["update", "src/app.py"]])
    assert agent_config_tagged("codex", tool, inp, c) is False


def test_agent_config_classifier_codex_patch_under_codex_home(tmp_path):
    elsewhere = tmp_path / "chome"
    c = ctx(tmp_path, CODEX_HOME=str(elsewhere))
    tool, inp = codex_file_change_call([["add", str(elsewhere / "auth.json")]])
    assert agent_config_tagged("codex", tool, inp, c) is True
    # without CODEX_HOME in the LAUNCH's environ the dir is <home>/.codex, whatever the process environment says
    plain = ctx(tmp_path)
    tool, inp = codex_file_change_call([["add", str(plain.home / ".codex" / "x.json")]])
    assert agent_config_tagged("codex", tool, inp, plain) is True


def test_agent_config_classifier_codex_file_change_no_item_is_tagged(tmp_path):
    tool, inp = codex_file_change_call(None)
    assert inp is None
    assert agent_config_tagged("codex", tool, inp, ctx(tmp_path)) is True


def test_agent_config_classifier_codex_command_never_tagged(tmp_path):
    tool, inp = codex_command_call({"command": "cat .claude/settings.json > /dev/null", "cwd": "/w"})
    assert (tool, inp) == ("commandExecution", {"command": "cat .claude/settings.json > /dev/null", "cwd": "/w"})
    assert agent_config_tagged("codex", tool, inp, ctx(tmp_path)) is False


def test_agent_config_classifier_unknown_harness_tagged(tmp_path):
    assert agent_config_tagged("someone-else", "Read", {"file_path": "x"}, ctx(tmp_path)) is True


def test_arrival_stores_the_harness_classification(cs, claude, codex, tmp_path):
    c = ctx(tmp_path)
    tagged = host(cs, claude["launch"], c, tool="Edit", tool_input={"file_path": f"{c.cwd}/.claude/settings.json"})
    assert tagged.row.targets_agent_config is True
    tool, inp = codex_file_change_call([["update", "src/app.py"]])
    with cs._write() as (conn, now):
        got = arrive(conn, now, launch_id=codex["launch"], raw_vendor_key=codex_raw_key("item/fileChange/requestApproval",
                                                                                         "it-1", "ap-1"),
                     transport_key="17", vendor_turn_id="t-1", vendor_call_id="it-1", approval_id="ap-1",
                     kind="file_change",
                     available_decisions=("accept", "decline"), tool_name=tool, tool_input=inp, ctx=c)
    assert got.disposition == "routed" and got.row.route == "native" and got.row.targets_agent_config is False


# --- the arrival transaction ------------------------------------------------------------------------------------
def test_arrival_denies_when_gate_not_live_and_only_once(cs, claude, tmp_path):
    with cs._write() as (conn, now):
        record_check(conn, now, claude["launch"], "not_live", "tripwire:deficit", "x")
    got = host(cs, claude["launch"], ctx(tmp_path))
    assert got.disposition == "denied" and got.attempt is not None
    assert got.attempt.reply["behavior"] == "deny" and "gate is not live" in got.attempt.reply["message"]
    assert events(cs, claude["launch"], got.row.vendor_request_key) == ["decision_observed", "answer_attempted"]
    assert host(cs, claude["launch"], ctx(tmp_path)).disposition == "replay"


def test_arrival_probing_waits_and_live_routes(store, cs, tmp_path):
    w = _world(store, "claude")
    with cs._write() as (conn, now):
        record_check(conn, now, w["launch"], "probing", "activation", "p", spawn_nonce=NONCE)
    got = host(cs, w["launch"], ctx(tmp_path))
    assert got.disposition == "probing" and got.attempt is None
    assert events(cs, w["launch"], got.row.vendor_request_key) == []


def test_codex_not_live_deny_is_the_rows_refusing_decision(cs, codex, tmp_path):
    with cs._write() as (conn, now):
        record_check(conn, now, codex["launch"], "not_live", "turn_start", "x")
        got = arrive(conn, now, launch_id=codex["launch"], raw_vendor_key=codex_raw_key("m", "i", None),
                     transport_key="18", vendor_turn_id="t", vendor_call_id="i", approval_id=None, kind="command",
                     available_decisions=("accept", "decline", "cancel"), tool_name="commandExecution",
                     tool_input={"command": "ls", "cwd": "/w"}, ctx=ctx(tmp_path))
    assert got.disposition == "denied" and dict(got.attempt.reply) == {"decision": "decline"}


# --- the indexes: append-only and CAS ---------------------------------------------------------------------------
def _request(cs, claude, tmp_path):
    return host(cs, claude["launch"], ctx(tmp_path)).row


def _ev(cs, row, kind, **kw):
    with cs._write() as (conn, now):
        return append_event(conn, now, launch_id=row.launch_id, key=row.vendor_request_key, kind=kind, by="service",
                            cause="t", **kw)


def test_approval_tables_are_append_only(cs, claude, tmp_path):
    row = _request(cs, claude, tmp_path)
    _ev(cs, row, "core_started")
    for sql in ("UPDATE chat_approvals SET route = 'native'", "DELETE FROM chat_approvals",
                "UPDATE chat_approval_events SET cause = 'x'", "DELETE FROM chat_approval_events"):
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            with cs._write() as (conn, _now):
                conn.execute(sql)


def test_one_decision_one_attempt_one_written_per_request(cs, claude, tmp_path):
    row = _request(cs, claude, tmp_path)
    _ev(cs, row, "decision_observed", decision="allow")
    _ev(cs, row, "answer_attempted", decision="allow", reply='{"behavior":"allow"}', op_id="op-1")
    _ev(cs, row, "answer_written", op_id="op-1")
    for kind, kw in (("decision_observed", {"decision": "deny"}),
                     ("answer_attempted", {"reply": "{}", "op_id": "op-2"}), ("answer_written", {"op_id": "op-2"})):
        with pytest.raises(sqlite3.IntegrityError):
            _ev(cs, row, kind, **kw)


def test_one_redelivery_per_transport_gen(cs, claude, tmp_path):
    row = _request(cs, claude, tmp_path)
    _ev(cs, row, "answer_redelivered", transport_gen=1)
    _ev(cs, row, "answer_redelivered", transport_gen=2)
    with pytest.raises(sqlite3.IntegrityError):
        _ev(cs, row, "answer_redelivered", transport_gen=2)
    with pytest.raises(sqlite3.IntegrityError):
        _ev(cs, row, "core_started", transport_gen=3)


def test_two_racing_attempts_one_commits(cs, claude, tmp_path):
    row = _request(cs, claude, tmp_path)
    won, lost = [], []

    def go(n):
        try:
            _ev(cs, row, "answer_attempted", reply="{}", op_id=f"op-{n}")
            won.append(n)
        except (sqlite3.IntegrityError, CoordError):
            lost.append(n)

    threads = [threading.Thread(target=go, args=(n,)) for n in range(4)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert len(won) == 1 and len(lost) == 3


def test_event_kinds_are_the_fourteen():
    assert len(ca.APPROVAL_EVENT_KINDS) == 14 and "answer_written" in ca.APPROVAL_EVENT_KINDS
    assert len(ca.APPROVAL_SCHEMA) and sum("CREATE UNIQUE INDEX" in s for s in ca.APPROVAL_SCHEMA) == 5


# --- record_abstain: five durable checks ------------------------------------------------------------------------
def _abstain(cs, row, cause="abstained:hold_expired"):
    return record_abstain(cs, launch_id=row.launch_id, vendor_request_key=row.vendor_request_key,
                          precursor={"cause": cause})


def test_record_abstain_writes_the_card_when_all_five_hold(cs, claude, tmp_path):
    row = _request(cs, claude, tmp_path)
    assert _abstain(cs, row) == "card"
    assert events(cs, row.launch_id, row.vendor_request_key) == ["core_result"]


def test_no_decider_card_says_no_rules_loaded(cs, claude, tmp_path):
    row = _request(cs, claude, tmp_path)
    assert _abstain(cs, row, "abstained:no_decider:claude_code.json is unreadable") == "card"
    cause = rows(cs, "SELECT cause FROM chat_approval_events WHERE vendor_request_key = ?", row.vendor_request_key)[0][0]
    assert ca.card_headline(cause) == "Levain has no rules loaded for this launch: claude_code.json is unreadable"
    assert "no rules loaded" not in ca.card_headline("abstained:hold_expired")
    assert "left this to you" in ca.card_headline("abstained:hold_expired")


def test_cancel_between_check_and_card_is_never_card(cs, claude, tmp_path):
    """A durable cancel lands after the core returned and before the card: the card is refused."""
    row = _request(cs, claude, tmp_path)
    with cs._write() as (conn, now):
        record_event(conn, now, launch_id=row.launch_id, key="turn_ended:t-1", pos="9", kind="turn_ended", body="end",
                     normalized="end", turn_id="t-1")
    assert _abstain(cs, row) == "decided_after_turn"
    assert events(cs, row.launch_id, row.vendor_request_key) == ["decided_after_turn"]


def test_record_abstain_refuses_when_not_live_committed_and_event_unset(cs, claude, tmp_path):
    """codex's interleaving: not_live committed, the Event never set, the core returned Abstained."""
    row = _request(cs, claude, tmp_path)
    with cs._write() as (conn, now):
        record_check(conn, now, row.launch_id, "not_live", "tripwire:deficit", "x")
    assert _abstain(cs, row) == "no_card"
    assert events(cs, row.launch_id, row.vendor_request_key) == []              # nothing terminal blocks the deny


def test_record_abstain_refuses_after_driver_lost(cs, claude, tmp_path):
    row = _request(cs, claude, tmp_path)
    with cs._write() as (conn, now):
        ca.mark_driver_lost(conn, now, row.launch_id, "stop")
    assert _abstain(cs, row) == "decided_after_turn"
    assert events(cs, row.launch_id, row.vendor_request_key) == ["driver_lost"]


def test_record_abstain_refuses_after_launch_stopped(store, cs, claude, tmp_path):
    row = _request(cs, claude, tmp_path)
    store.launch_end(row.launch_id, "ended", "stopped")
    assert _abstain(cs, row) == "decided_after_turn"
    assert events(cs, row.launch_id, row.vendor_request_key) == ["decided_after_turn"]


def test_record_abstain_refuses_after_activation_replaced(cs, claude, tmp_path):
    row = _request(cs, claude, tmp_path)
    _activate(cs, row.launch_id, NONCE2)
    assert _abstain(cs, row) == "decided_after_turn"


def test_record_abstain_no_card_when_attempt_exists(cs, claude, tmp_path):
    row = _request(cs, claude, tmp_path)
    _ev(cs, row, "decision_observed", decision="deny")
    _ev(cs, row, "answer_attempted", reply="{}", op_id="op-1")
    assert _abstain(cs, row) == "no_card"
    assert events(cs, row.launch_id, row.vendor_request_key) == ["decision_observed", "answer_attempted"]


# --- latch_not_live (R9-3) --------------------------------------------------------------------------------------
def test_latch_not_live_attempts_every_pending_request_in_one_transaction(cs, claude, tmp_path):
    c = ctx(tmp_path)
    a, b, done = (host(cs, claude["launch"], c, tool_use_id=f"toolu_{i}").row for i in (1, 2, 3))
    _ev(cs, done, "turn_ended")                                    # terminal: not pending
    attempts = latch_not_live(cs, launch_id=claude["launch"], source="tripwire:deficit", cause="two calls, one answer")
    assert {x.vendor_request_key for x in attempts} == {a.vendor_request_key, b.vendor_request_key}
    for x in attempts:
        assert x.reply["behavior"] == "deny" and "two calls" in x.reply["message"]
        assert events(cs, x.launch_id, x.vendor_request_key) == ["decision_observed", "answer_attempted"]
    assert events(cs, claude["launch"], done.vendor_request_key) == ["turn_ended"]
    assert chat.liveness(cs._connect(), claude["launch"]).latched is True


def test_latch_commit_failure_writes_no_bytes(cs, claude, tmp_path, monkeypatch):
    row = _request(cs, claude, tmp_path)
    real = ca._deny_attempt

    def broken(*a, **k):
        real(*a, **k)
        raise RuntimeError("commit failed")

    monkeypatch.setattr(ca, "_deny_attempt", broken)
    with pytest.raises(RuntimeError):
        latch_not_live(cs, launch_id=claude["launch"], source="tripwire:deficit", cause="x")
    assert events(cs, row.launch_id, row.vendor_request_key) == []
    assert chat.liveness(cs._connect(), claude["launch"]).latched is False


def test_not_live_then_abstain_exactly_one_deny_written(cs, claude, tmp_path):
    """codex's exact interleaving: the latch commits the deny; the late Abstained finds no card to write and no
    terminal row that would block the deny."""
    row = _request(cs, claude, tmp_path)
    attempts = latch_not_live(cs, launch_id=claude["launch"], source="tripwire:deficit", cause="x")
    assert len(attempts) == 1
    assert _abstain(cs, row) == "no_card"
    assert events(cs, row.launch_id, row.vendor_request_key) == ["decision_observed", "answer_attempted"]


def test_request_after_latch_denied_at_arrival(cs, claude, tmp_path):
    latch_not_live(cs, launch_id=claude["launch"], source="tripwire:deficit", cause="x")
    late = host(cs, claude["launch"], ctx(tmp_path), tool_use_id="toolu_late")
    assert late.disposition == "denied" and late.attempt is not None


def test_allow_attempted_before_latch_is_written_once_and_latch_skips_it(cs, claude, tmp_path):
    row = _request(cs, claude, tmp_path)
    _ev(cs, row, "decision_observed", decision="allow")
    _ev(cs, row, "answer_attempted", decision="allow", reply='{"behavior":"allow"}', op_id="op-1")
    assert latch_not_live(cs, launch_id=claude["launch"], source="tripwire:deficit", cause="x") == []
    assert events(cs, row.launch_id, row.vendor_request_key) == ["decision_observed", "answer_attempted"]


# --- record_cancelled / cancel_launch_requests ------------------------------------------------------------------
@pytest.mark.parametrize("cause,terminal", [("stop", "driver_lost"), ("kill", "driver_lost"),
                                            ("stdin closed", "driver_lost"), ("service shutdown", "driver_lost"),
                                            ("turn_ended", "decided_after_turn"), ("not_live", "decided_after_turn"),
                                            ("control_cancel_request", "decided_after_turn")])
def test_cancelled_hold_is_never_card(cs, claude, tmp_path, cause, terminal):
    row = _request(cs, claude, tmp_path)
    record_cancelled(cs, launch_id=row.launch_id, vendor_request_key=row.vendor_request_key, cause=cause,
                     tag="abstained")
    assert events(cs, row.launch_id, row.vendor_request_key) == ["core_result", terminal]
    got = rows(cs, "SELECT decision, cause FROM chat_approval_events WHERE kind = 'core_result'")[0]
    assert got == ("abstained", f"cancelled: {cause}")                    # never `none`: no card


def test_cancelled_driver_lost_already_committed_is_gets_no_second_row(cs, claude, tmp_path):
    row = _request(cs, claude, tmp_path)
    with cs._write() as (conn, now):
        ca.mark_driver_lost(conn, now, row.launch_id, "kill")
    record_cancelled(cs, launch_id=row.launch_id, vendor_request_key=row.vendor_request_key, cause="kill",
                     tag="decided")
    assert events(cs, row.launch_id, row.vendor_request_key) == ["driver_lost"]


def test_each_cancel_producer_commits_before_set(cs, claude, tmp_path):
    """For all seven causes the durable row is visible to a SECOND connection at the moment the Event is set."""
    for cause in CANCEL_CAUSES:
        c = ctx(tmp_path)
        row = host(cs, claude["launch"], c, tool_use_id=f"toolu_{cause.replace(' ', '_')}").row
        registry = CancelRegistry()
        call = registry.register(row.launch_id, row.vendor_request_key)
        seen = []
        original = call.set

        def set_and_look(why, _row=row, _original=original):
            conn = sqlite3.connect(cs._opener._connect().execute("PRAGMA database_list").fetchone()[2])
            seen.append(conn.execute("SELECT COUNT(*) FROM chat_approval_events WHERE vendor_request_key = ?",
                                     (_row.vendor_request_key,)).fetchone()[0])
            conn.close()
            _original(why)

        call.set = set_and_look
        cancel_launch_requests(cs, launch_id=row.launch_id, cause=cause, registry=registry,
                               commit=lambda conn, now, _row=row, _cause=cause: append_event(
                                   conn, now, launch_id=_row.launch_id, key=_row.vendor_request_key,
                                   kind="driver_lost" if _cause in ("stop", "kill", "stdin closed", "service shutdown")
                                   else "resolved_outside", by="outside", cause=_cause))
        assert seen == [1] and call.is_set() and call.cause == cause


def test_cancel_commit_failure_sets_nothing(cs, claude, tmp_path):
    row = _request(cs, claude, tmp_path)
    registry = CancelRegistry()
    call = registry.register(row.launch_id, row.vendor_request_key)

    def boom(conn, now):
        raise RuntimeError("busy")

    with pytest.raises(RuntimeError):
        cancel_launch_requests(cs, launch_id=row.launch_id, cause="stop", commit=boom, registry=registry)
    assert not call.is_set()
    with pytest.raises(CoordError):
        cancel_launch_requests(cs, launch_id=row.launch_id, cause="because", commit=lambda c, n: None,
                               registry=registry)


# --- ambiguous_stops (R11-7) ------------------------------------------------------------------------------------
def test_ambiguous_on_live_child_surfaces_stop_card(cs, claude, tmp_path):
    row = _request(cs, claude, tmp_path)
    _ev(cs, row, "answer_ambiguous")
    got = cs._read(lambda conn: ambiguous_stops(conn, row.launch_id))
    assert [(g.launch_id, g.request_id) for g in got] == [(row.launch_id, row.vendor_request_key)]
    assert got[0].since


def test_ambiguous_stop_card_gone_after_settle(cs, claude, tmp_path):
    row = _request(cs, claude, tmp_path)
    _ev(cs, row, "answer_ambiguous")
    _ev(cs, row, "answer_confirmed")
    assert cs._read(lambda conn: ambiguous_stops(conn, row.launch_id)) == []
    other = host(cs, claude["launch"], ctx(tmp_path), tool_use_id="toolu_2").row
    _ev(cs, other, "answer_ambiguous")
    with cs._write() as (conn, now):
        record_event(conn, now, launch_id=row.launch_id, key="turn_ended:t-1", pos="9", kind="turn_ended", body="e",
                     normalized="e", turn_id="t-1")
    assert cs._read(lambda conn: ambiguous_stops(conn, row.launch_id)) == []


def test_ambiguous_stop_card_none_for_dead_child(store, cs, claude, tmp_path):
    row = _request(cs, claude, tmp_path)
    _ev(cs, row, "answer_ambiguous")
    store.launch_end(row.launch_id, "ended", "gone")
    assert cs._read(lambda conn: ambiguous_stops(conn, row.launch_id)) == []


def test_ambiguous_stop_card_writes_no_answer(cs, claude, tmp_path):
    row = _request(cs, claude, tmp_path)
    _ev(cs, row, "answer_ambiguous")
    before = rows(cs, "SELECT COUNT(*) FROM chat_approval_events")[0][0]
    for _ in range(3):
        cs._read(lambda conn: ambiguous_stops(conn, row.launch_id))
    assert rows(cs, "SELECT COUNT(*) FROM chat_approval_events")[0][0] == before


# --- the view's approval branch ---------------------------------------------------------------------------------
def test_timeline_shows_the_request_and_its_events_in_gseq_order(cs, claude, tmp_path):
    row = _request(cs, claude, tmp_path)
    _abstain(cs, row)
    _, merged = cs.timeline(claude["thread"])
    kinds = [(r.kind, r.status) for r in merged if r.kind.startswith("approval")]
    assert kinds == [("approval", "core_result"), ("approval_core_result", "none")]
    assert [r.gseq for r in merged] == sorted(r.gseq for r in merged)
    shown = [r for r in merged if r.kind == "approval"][0]
    assert shown.body == "Bash" and "ls" not in json.dumps([r.body for r in merged if r.kind.startswith("approval")])


def test_context_is_a_frozen_named_tuple(tmp_path):
    c = ctx(tmp_path)
    assert isinstance(c, tuple) and c._fields == ("queue_dir", "cwd", "home", "environ", "spawn_nonce", "claude_pid")
    with pytest.raises(AttributeError):
        c.cwd = "/elsewhere"                                                  # type: ignore[misc]
    assert isinstance(c.environ, type(MappingProxyType({}))) and not isinstance(c.queue_dir, str)
    assert isinstance(Path(c.cwd), Path)


# --- fix round 1 ------------------------------------------------------------------------------------------------
def _codex(cs, launch, c, *, kind="command", decisions=None, item="it-1", tool="commandExecution", inp=None):
    with cs._write() as (conn, now):
        return arrive(conn, now, launch_id=launch, raw_vendor_key=codex_raw_key(f"m/{kind}", item, None),
                      transport_key=f"rpc-{item}", vendor_turn_id="t-1", vendor_call_id=item, approval_id=None,
                      kind=kind, available_decisions=decisions, tool_name=tool,
                      tool_input={"command": "ls", "cwd": "/w"} if inp is None else inp, ctx=c)


def _not_live(cs, launch):
    with cs._write() as (conn, now):
        record_check(conn, now, launch, "not_live", "tripwire:deficit", "x")


def _scripted_with(request_id, tool="Bash", tool_input=None):
    from levain.coordinator.driver import ScriptedSession
    s = ScriptedSession()
    s.inbound(request_id, "can_use_tool", tool_name=tool, tool_input=tool_input or {"command": "ls"})
    return s


def _answer_for(attempt, key=None):
    from levain.coordinator.driver import ApprovalAnswer
    return ApprovalAnswer(attempt.op_id, attempt.vendor_request_key, attempt.raw_vendor_key if key is None else key,
                          attempt.reply)


# R1: the reply is addressed by the transport key, not the identity key
def test_claude_attempt_answer_reaches_the_session_by_the_transport_key(cs, claude, tmp_path):
    _not_live(cs, claude["launch"])
    got = host(cs, claude["launch"], ctx(tmp_path), tool_use_id="toolu_7")        # transport key "req-toolu_7"
    assert got.disposition == "denied" and got.row.raw_vendor_key != got.row.transport_key
    session = _scripted_with("req-toolu_7")
    assert got.attempt.raw_vendor_key == "req-toolu_7"
    assert session.answer(_answer_for(got.attempt)).outcome == "confirmed"
    # the identity key is not an id the session holds (this is what addressing by it did before the fix)
    other = _scripted_with("req-toolu_7")
    assert other.answer(_answer_for(got.attempt, key=got.row.raw_vendor_key)).outcome == "no_such_request"


def test_latch_attempt_for_a_claude_row_reaches_the_session(cs, claude, tmp_path):
    row = host(cs, claude["launch"], ctx(tmp_path), tool_use_id="toolu_8").row
    (attempt,) = latch_not_live(cs, launch_id=claude["launch"], source="tripwire:deficit", cause="x")
    assert attempt.vendor_request_key == row.vendor_request_key
    assert _scripted_with("req-toolu_8").answer(_answer_for(attempt)).outcome == "confirmed"


def test_quarantine_row_keeps_the_transport_key_and_its_deny_reaches_the_session(cs, claude):
    line = b'{"type":"control_request","request_id":"req-q9","request":{"subtype":"can_use_tool"}}'
    with cs._write() as (conn, now):
        q = record_quarantine(conn, now, launch_id=claude["launch"], spawn_nonce=NONCE, raw_line=line,
                              transport_key="req-q9")
    assert q.row.transport_key == "req-q9"
    (attempt,) = latch_not_live(cs, launch_id=claude["launch"], source="tripwire:deficit", cause="x")
    assert attempt.raw_vendor_key == "req-q9" and attempt.reply["behavior"] == "deny"
    assert _scripted_with("req-q9").answer(_answer_for(attempt)).outcome == "confirmed"


def test_transport_key_is_required_and_nonempty(cs, claude):
    with pytest.raises(CoordError, match="transport key"):
        with cs._write() as (conn, now):
            arrive(conn, now, launch_id=claude["launch"], raw_vendor_key=claude_raw_key(NONCE, "t"), transport_key="",
                   vendor_turn_id="t", vendor_call_id="t", approval_id=None, kind="host", available_decisions=None,
                   tool_name="Bash", tool_input={}, ctx=ctx(Path("/tmp")), spawn_nonce=NONCE)


# R2: a Codex denial is always buildable
def test_codex_permission_request_is_denied_with_empty_permissions(cs, codex, tmp_path):
    _not_live(cs, codex["launch"])
    got = _codex(cs, codex["launch"], ctx(tmp_path), kind="permission", decisions=None, tool="permissions",
                 inp={"permissions": {}})
    assert got.disposition == "denied" and dict(got.attempt.reply) == {"permissions": {}}
    assert got.row.available_decisions == () and got.attempt.raw_vendor_key == "rpc-it-1"


def test_codex_command_without_decisions_is_denied_with_the_pinned_decline(cs, codex, tmp_path):
    _not_live(cs, codex["launch"])
    got = _codex(cs, codex["launch"], ctx(tmp_path), decisions=None)
    assert got.disposition == "denied" and dict(got.attempt.reply) == {"decision": "decline"}
    other = _codex(cs, codex["launch"], ctx(tmp_path), item="it-2", decisions=("accept", "acceptForSession"))
    assert dict(other.attempt.reply) == {"decision": "decline"}


def test_codex_deny_replies_match_the_wire_registry():
    from levain.coordinator.codex_wire import SERVER_REQUESTS
    assert dict(SERVER_REQUESTS["item/permissions/requestApproval"].reply) == {"permissions": {}}
    for m in ("item/commandExecution/requestApproval", "item/fileChange/requestApproval"):
        assert dict(SERVER_REQUESTS[m].reply) == {"decision": "decline"}


def test_latch_commits_when_a_codex_request_offers_no_decision(cs, codex, tmp_path):
    c = ctx(tmp_path)
    perm = _codex(cs, codex["launch"], c, kind="permission", decisions=None, tool="permissions", inp={"permissions": {}})
    cmd = _codex(cs, codex["launch"], c, item="it-2", decisions=())
    attempts = latch_not_live(cs, launch_id=codex["launch"], source="turn_start", cause="x")
    assert {a.vendor_request_key for a in attempts} == {perm.row.vendor_request_key, cmd.row.vendor_request_key}
    assert chat.liveness(cs._connect(), codex["launch"]).latched is True


def test_latch_one_undeniable_request_does_not_undo_the_verdict_or_the_others(cs, claude, tmp_path, monkeypatch):
    c = ctx(tmp_path)
    bad, good = (host(cs, claude["launch"], c, tool_use_id=f"toolu_{i}").row for i in (1, 2))
    real = ca._deny_attempt

    def flaky(conn, now, row, *, cause):
        out = real(conn, now, row, cause=cause)
        if row.vendor_request_key == bad.vendor_request_key:
            raise CoordError("cannot build")                    # after its two rows: its savepoint must roll them back
        return out

    monkeypatch.setattr(ca, "_deny_attempt", flaky)
    attempts = latch_not_live(cs, launch_id=claude["launch"], source="tripwire:deficit", cause="x")
    assert [a.vendor_request_key for a in attempts] == [good.vendor_request_key]
    assert events(cs, claude["launch"], bad.vendor_request_key) == ["answer_refused"]
    assert events(cs, claude["launch"], good.vendor_request_key) == ["decision_observed", "answer_attempted"]
    assert chat.liveness(cs._connect(), claude["launch"]).latched is True


# R3: a decided request is the door's, not the latch's
def test_latch_skips_a_request_that_already_has_a_decision(cs, claude, tmp_path):
    row = _request(cs, claude, tmp_path)
    _ev(cs, row, "decision_observed", decision="allow")
    assert latch_not_live(cs, launch_id=claude["launch"], source="tripwire:deficit", cause="x") == []
    assert events(cs, row.launch_id, row.vendor_request_key) == ["decision_observed"]
    assert chat.liveness(cs._connect(), claude["launch"]).latched is True


# R4: the two core-result writers are idempotent
def test_record_abstain_retry_writes_one_card(cs, claude, tmp_path):
    row = _request(cs, claude, tmp_path)
    assert _abstain(cs, row) == "card"
    assert _abstain(cs, row) == "no_card"
    assert events(cs, row.launch_id, row.vendor_request_key) == ["core_result"]


def test_record_cancelled_retry_writes_one_result_and_one_terminal(cs, claude, tmp_path):
    row = _request(cs, claude, tmp_path)
    for _ in range(2):
        record_cancelled(cs, launch_id=row.launch_id, vendor_request_key=row.vendor_request_key, cause="stop", tag="x")
    assert events(cs, row.launch_id, row.vendor_request_key) == ["core_result", "driver_lost"]


def test_core_result_unique_index_holds_for_any_writer(cs, claude, tmp_path):
    row = _request(cs, claude, tmp_path)
    _ev(cs, row, "core_result", decision="none")
    with pytest.raises(sqlite3.IntegrityError):
        _ev(cs, row, "core_result", decision="none")


@pytest.mark.parametrize("terminal", ["turn_ended", "resolved_outside", "driver_lost", "decided_after_turn",
                                      "answer_confirmed", "answer_ambiguous"])
def test_cancel_and_abstain_after_a_terminal_write_nothing(cs, claude, tmp_path, terminal):
    row = _request(cs, claude, tmp_path)
    _ev(cs, row, terminal)
    record_cancelled(cs, launch_id=row.launch_id, vendor_request_key=row.vendor_request_key, cause="not_live", tag="x")
    assert _abstain(cs, row) == "decided_after_turn"
    assert events(cs, row.launch_id, row.vendor_request_key) == [terminal]


def test_cancel_and_abstain_after_an_attempt_write_nothing(cs, claude, tmp_path):
    row = _request(cs, claude, tmp_path)
    _ev(cs, row, "decision_observed", decision="deny")
    _ev(cs, row, "answer_attempted", reply="{}", op_id="op-1")
    record_cancelled(cs, launch_id=row.launch_id, vendor_request_key=row.vendor_request_key, cause="not_live", tag="x")
    assert _abstain(cs, row) == "no_card"
    assert events(cs, row.launch_id, row.vendor_request_key) == ["decision_observed", "answer_attempted"]


def test_abstain_after_a_cancel_result_writes_no_card(cs, claude, tmp_path):
    row = _request(cs, claude, tmp_path)
    record_cancelled(cs, launch_id=row.launch_id, vendor_request_key=row.vendor_request_key, cause="not_live", tag="x")
    assert _abstain(cs, row) == "decided_after_turn"
    assert events(cs, row.launch_id, row.vendor_request_key) == ["core_result", "decided_after_turn"]


# R5: only the current activation's ambiguity is a stop card
def test_ambiguity_of_a_replaced_child_is_no_stop_card(cs, claude, tmp_path):
    row = _request(cs, claude, tmp_path)
    _ev(cs, row, "answer_ambiguous")
    assert len(cs._read(lambda conn: ambiguous_stops(conn, row.launch_id))) == 1
    _activate(cs, row.launch_id, NONCE2)
    assert cs._read(lambda conn: ambiguous_stops(conn, row.launch_id)) == []


# R7: a carded request is denied by the latch, by intent
def test_latch_denies_a_carded_request(cs, claude, tmp_path):
    row = _request(cs, claude, tmp_path)
    assert _abstain(cs, row) == "card"
    (attempt,) = latch_not_live(cs, launch_id=claude["launch"], source="tripwire:deficit", cause="x")
    assert attempt.vendor_request_key == row.vendor_request_key
    assert events(cs, row.launch_id, row.vendor_request_key) == ["core_result", "decision_observed", "answer_attempted"]


# --- fix round 2 -------------------------------------------------------------------------------------------------
def _arrive_with_transport(cs, launch, c, transport):
    with cs._write() as (conn, now):
        return arrive(conn, now, launch_id=launch, raw_vendor_key=claude_raw_key(NONCE, "toolu_1"),
                      transport_key=transport, vendor_turn_id="t-1", vendor_call_id="toolu_1", approval_id=None,
                      kind="host", available_decisions=("allow", "deny"), tool_name="Bash",
                      tool_input={"command": "ls"}, ctx=c, spawn_nonce=NONCE)


def test_replay_with_a_new_transport_key_is_addressed_by_the_current_one(cs, claude, tmp_path):
    c = ctx(tmp_path)
    first = _arrive_with_transport(cs, claude["launch"], c, "req-old")
    again = _arrive_with_transport(cs, claude["launch"], c, "req-new")
    assert again.disposition == "replay" and again.row.vendor_request_key == first.row.vendor_request_key
    assert again.row.transport_key == "req-new"
    attempts = latch_not_live(cs, launch_id=claude["launch"], source="tripwire:deficit", cause="x")
    assert [a.raw_vendor_key for a in attempts] == ["req-new"]


def test_quarantine_replay_with_a_new_transport_key_is_addressed_by_the_current_one(cs, claude):
    def q(transport):
        with cs._write() as (conn, now):
            return record_quarantine(conn, now, launch_id=claude["launch"], spawn_nonce=NONCE, raw_line=b"junk",
                                     transport_key=transport)
    q("req-old")
    assert q("req-new").row.transport_key == "req-new"
    attempts = latch_not_live(cs, launch_id=claude["launch"], source="tripwire:deficit", cause="x")
    assert [a.raw_vendor_key for a in attempts] == ["req-new"]


def test_only_transport_key_may_change_on_an_approval_row(cs, claude, tmp_path):
    _request(cs, claude, tmp_path)
    with cs._write() as (conn, _now):
        conn.execute("UPDATE chat_approvals SET transport_key = 'req-x'")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        with cs._write() as (conn, _now):
            conn.execute("UPDATE chat_approvals SET tool_name = 'Other'")


def test_cancel_after_a_card_still_writes_the_terminal(cs, claude, tmp_path):
    row = _request(cs, claude, tmp_path)
    assert _abstain(cs, row) == "card"
    assert row.vendor_request_key in cs._read(lambda conn: ca.pending_requests(conn, row.launch_id))
    record_cancelled(cs, launch_id=row.launch_id, vendor_request_key=row.vendor_request_key,
                     cause="control_cancel_request", tag="x")
    assert events(cs, row.launch_id, row.vendor_request_key) == ["core_result", "decided_after_turn"]
    assert row.vendor_request_key not in cs._read(lambda conn: ca.pending_requests(conn, row.launch_id))


def test_cancel_after_decision_observed_writes_nothing(cs, claude, tmp_path):
    row = _request(cs, claude, tmp_path)
    _ev(cs, row, "decision_observed", decision="deny")
    record_cancelled(cs, launch_id=row.launch_id, vendor_request_key=row.vendor_request_key, cause="not_live", tag="x")
    assert events(cs, row.launch_id, row.vendor_request_key) == ["decision_observed"]

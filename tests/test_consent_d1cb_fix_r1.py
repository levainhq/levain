"""CH-D1c-B fix round 1 (1027-9/d1cb/FIX_r1.md): the contained-shell proof bound to its launch and to chat calls
(D1), an SH1 grant counted by the no-decider gate (D2), one pending chat row per call (D3), the digest's whole
failure set declined (D4), one vendor-name classifier for the chat declines (D5), and the take path's lost read
(D6)."""
from __future__ import annotations

import os
import sqlite3
import typing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

import levain.authority as auth
from levain.cockpit.consent import ConsentError, utc_stamp
from levain.consent import contained
from levain.consent.adapters import claude_code as cc
from levain.consent.agentboundary import _tool_use_id_of
from levain.consent.contained import ContainedLaunchProof, CoreCall, core_call_of
from levain.consent.policy import contained_shell_refusal
from levain.consent.queue import Decision
from levain.consent.record import ExternalGateRequest, tool_input_digest
from tests._decide_scenarios import _event
from tests.test_consent_core_union import AWAY, CONFIG, NONCE, Q, _take, env, ex
from tests.test_consent_queue import authority, clock, keys, queue, signed  # noqa: F401 - the shared fixtures

INPUT = {"command": "pytest -q"}


def _proof(cwd, **kw):
    base = {"launch_id": "L1", "spawn_nonce": NONCE, "profile_digest": "d" * 64, "canary_verdict_id": "c" * 64,
            "cwd": os.path.realpath(cwd)}
    return ContainedLaunchProof(**{**base, **kw})


# --- D1 (a): a proof is bound to the call's launch ----------------------------------------------------------

def test_a_proof_from_another_launch_is_declined_before_anything_is_filed(tmp_path):
    # codex r1 HIGH: a valid proof from launch A passed with launch B's nonce
    q = Q("allow")
    e = _event("Bash", INPUT, tool_use_id="toolu_1")
    out = ex(e, q, tmp_path, nonce="b" * 32, proof=_proof(tmp_path))
    assert isinstance(out, cc.Declined) and out.reason == "contained_launch_mismatch" and q.filed == []
    assert out.call == core_call_of(e, launch_nonce="b" * 32)


def test_a_proof_on_a_hook_call_is_declined(tmp_path):
    q = Q("allow")
    out = ex(_event("Bash", INPUT, tool_use_id="toolu_1"), q, tmp_path, nonce="", proof=_proof(tmp_path))
    assert isinstance(out, cc.Declined) and out.reason == "contained_launch_mismatch" and q.filed == []


def test_a_misshapen_proof_is_declined(tmp_path):
    q = Q("allow")
    out = ex(_event("Bash", INPUT, tool_use_id="toolu_1"), q, tmp_path, nonce=NONCE,
             proof=_proof(tmp_path, profile_digest="x"))
    assert isinstance(out, cc.Declined) and out.reason == "contained_launch_mismatch" and q.filed == []


def test_a_proof_of_this_launch_is_filed_as_a_chat_call(tmp_path):
    q = Q("allow")
    out = ex(_event("Bash", INPUT, tool_use_id="toolu_1"), q, tmp_path, nonce=NONCE, proof=_proof(tmp_path))
    assert isinstance(out, cc.Decided) and q.filed[0]["event_source"] == "chat"


def test_the_mismatch_reason_is_in_the_union_with_a_message():
    hints = typing.get_type_hints(cc.Declined)
    assert "contained_launch_mismatch" in typing.get_args(hints["reason"])
    assert cc.DECLINE_MESSAGES["contained_launch_mismatch"]


# --- D1 (b): an SH1 grant answers only a chat: request ------------------------------------------------------

def _sh1(ws):
    return {"id": "sh1-a", "kind": auth.CONSENT_CONTAINED_SHELL_KIND, "agent": "claude_code", "tools": ["Bash"],
            "workspace": ws, "postures": ["default"], "containment_digest": "d" * 64,
            "expires_at": utc_stamp(datetime.now(timezone.utc) + timedelta(days=7))}


@pytest.fixture
def ws(tmp_path):
    d = tmp_path / "home" / "proj"
    d.mkdir(parents=True)
    return os.path.realpath(d)


def _request(ws, event_id):
    return ExternalGateRequest.create(
        agent="claude_code", agent_session_id="s1", event_id=event_id, tool_name="Bash",
        tool_input_digest=tool_input_digest(INPUT), display="d", cwd=ws, tier="T2",
        received_at="2026-10-27T12:00:00.000000Z", expires_at="2026-10-27T12:10:00.000000Z")


def _refusal(ws, event_id):
    return contained_shell_refusal(_sh1(ws), _request(ws, event_id), tool_input=INPUT, posture="default",
                                   now=datetime.now(timezone.utc), home=os.path.dirname(ws),
                                   contained_launch=_proof(ws))


@pytest.mark.parametrize("event_id", ["hook:toolu_1", "rand:" + "0" * 32, "toolu_1", "chat:"])
def test_contained_shell_refusal_refuses_a_request_that_is_not_a_chat_call(ws, event_id):
    # codex r1 HIGH: a matching request whose event_id is hook:toolu_1 was answered
    assert _refusal(ws, event_id) == "not a chat call"


def test_contained_shell_refusal_answers_a_chat_call(ws):
    assert _refusal(ws, "chat:toolu_1") is None


# --- D2: the no-decider gate counts a live SH1 grant for a chat Bash call with a proof ----------------------

def _only_sh1(monkeypatch, tmp_path, grant):
    root = tmp_path / "cockpit"
    root.mkdir()
    (root / "cockpit.db").write_bytes(b"")
    snap = SimpleNamespace(active_keys=lambda: [], policy=SimpleNamespace(grants={grant["id"]: grant}))
    monkeypatch.setattr(cc, "authority_source", lambda *a, **kw: (lambda: snap))
    return cc.AdapterSettings(cockpit_data_root=str(root))


def test_an_sh1_grant_alone_lets_a_chat_bash_call_with_a_proof_be_filed(tmp_path, monkeypatch):
    settings = _only_sh1(monkeypatch, tmp_path, _sh1(str(tmp_path)))
    q = Q(None)
    out = ex(_event("Bash", INPUT, tool_use_id="toolu_1"), q, tmp_path, settings=settings, nonce=NONCE,
             proof=_proof(tmp_path))
    assert len(q.filed) == 1 and not (isinstance(out, cc.Abstained) and out.reason == "no_decider")


@pytest.mark.parametrize("case", ["hook", "no_proof", "not_bash", "expired"])
def test_an_sh1_grant_decides_nothing_else(tmp_path, monkeypatch, case):
    grant = _sh1(str(tmp_path))
    if case == "expired":
        grant["expires_at"] = utc_stamp(datetime.now(timezone.utc) - timedelta(minutes=1))
    settings = _only_sh1(monkeypatch, tmp_path, grant)
    tool = ("Read", {"file_path": "/x"}) if case == "not_bash" else ("Bash", INPUT)
    nonce = "" if case == "hook" else NONCE
    proof = None if case in ("hook", "no_proof") else _proof(tmp_path)
    q = Q(None)
    out = ex(_event(*tool, tool_use_id="toolu_1"), q, tmp_path, settings=settings, nonce=nonce, proof=proof)
    assert isinstance(out, cc.Abstained) and out.reason == "no_decider" and q.filed == []


# --- D3: one pending chat row per call; record_ran pairs the newest row of either prefix --------------------

def _file(q, event_id, source):
    return q.file(agent="claude_code", agent_session_id="sess-1", tool_name="Bash", tool_input=INPUT,
                  cwd="/work/proj", hold=timedelta(minutes=5), event_id=event_id, event_source=source)


def test_a_chat_call_filed_twice_is_refused(queue, clock):
    _file(queue, "toolu_1", "chat")
    clock.now += timedelta(seconds=3)
    with pytest.raises(ConsentError):
        _file(queue, "toolu_1", "chat")


def test_a_chat_and_a_hook_filing_of_one_id_coexist(queue):
    _file(queue, "toolu_1", "chat")
    _file(queue, "toolu_1", "hook")


def test_the_chat_index_is_in_the_store(queue):
    _file(queue, "toolu_0", "chat")
    conn = sqlite3.connect(queue.directory / "queue.db")
    try:
        sql = conn.execute("SELECT sql FROM sqlite_master WHERE name = 'requests_chat_event'").fetchone()
    finally:
        conn.close()
    assert sql is not None and "chat:%" in sql[0]


def test_record_ran_pairs_the_newest_row_across_both_prefixes(queue, clock):
    _file(queue, "toolu_1", "hook")
    clock.now += timedelta(seconds=2)
    newer = _file(queue, "toolu_1", "chat")
    got = queue.record_ran(agent="claude_code", agent_session_id="sess-1", tool_name="Bash", tool_input=INPUT,
                           event="PostToolUse", tool_use_id="toolu_1")
    assert got is not None and got[0] == newer.id


def test_record_ran_without_an_id_never_pairs_a_chat_row(queue):
    # deepseek r1 MED 2: the no-id fallback took a chat-core row
    _file(queue, "toolu_1", "chat")
    assert queue.record_ran(agent="claude_code", agent_session_id="sess-1", tool_name="Bash", tool_input=INPUT,
                            event="PostToolUse", tool_use_id=None) is None


# --- D4: every failure of the digest is invalid_event -------------------------------------------------------

def _recursive():
    d: dict = {}
    d["self"] = d
    return d


def _deep(n=100_000):
    d: dict = {}
    for _ in range(n):
        d = {"a": d}
    return d


@pytest.mark.parametrize("make", [lambda: {1: "a"}, lambda: {"x": {2: "b"}}, lambda: {"x": [{None: 1.5}]},
                                  lambda: {1: "a", "x": "b"}, lambda: {"x": object()}, lambda: {"x": {1, 2}},
                                  _recursive, _deep])
@pytest.mark.parametrize("nonce", ["", NONCE])
def test_an_undigestable_input_is_declined_invalid_event(tmp_path, make, nonce):
    # L3 r2 codex D4: the exact {1: "a"} (a json fallback coerced the key to "1"), and an object json cannot encode
    q = Q("allow")
    out = ex(_event("Bash", make(), tool_use_id="toolu_1"), q, tmp_path, nonce=nonce)
    assert isinstance(out, cc.Declined) and out.reason == "invalid_event" and q.filed == []


@pytest.mark.parametrize("value", [{1: "a"}, {"x": {2: "b"}}, {"x": [{True: 1.5}]}, {"x": object()},
                                   {"x": {1, 2}}, {"x": 1.5, "y": Path("/p")}])
def test_the_json_fallback_refuses_a_non_string_key_and_an_unencodable_object(value):
    with pytest.raises(TypeError):
        tool_input_digest(value)


def test_the_json_fallback_still_digests_a_float_and_a_string_keyed_tree():
    assert tool_input_digest({"a": 1.5, "b": [{"c": 2.5}]}).startswith("json:")
    assert tool_input_digest({"a": 1}).startswith("jcs:")


# --- L3 r2 complement LOW 3: an SH1 grant never counts for an agent-config call -----------------------------

def test_an_sh1_grant_does_not_count_for_an_agent_config_call(tmp_path, monkeypatch):
    # Bash is never agent config today (targets_agent_config does not parse Bash), so this pins the order should
    # that change: with only an SH1 grant, an agent-config call is no_decider, never a T3 row no key can sign
    settings = _only_sh1(monkeypatch, tmp_path, _sh1(str(tmp_path)))
    monkeypatch.setattr(cc, "targets_agent_config", lambda *a, **kw: True)
    q = Q(None)
    out = ex(_event("Bash", INPUT, tool_use_id="toolu_1"), q, tmp_path, settings=settings, nonce=NONCE,
             proof=_proof(tmp_path))
    assert isinstance(out, cc.Abstained) and out.reason == "no_decider" and q.filed == []


def test_core_call_digest_survives_a_lone_surrogate():
    # complement r1 LOW 3
    call = CoreCall(session="s\ud800", call_id="c", tool_name="Bash", input_digest="jcs:x", cwd="/w", launch_nonce="")
    assert len(call.digest()) == 64


# --- D5: one classifier for the escape key and the network tool ----------------------------------------------

@pytest.mark.parametrize("name", ["SandboxNetworkAccess", "sandboxnetworkaccess", "SANDBOXNETWORKACCESS",
                                  "mcp__srv__SandboxNetworkAccess", "ToolSearch__sandboxNetworkAccess"])
def test_a_sandbox_net_name_variant_is_declined_in_chat(tmp_path, name):
    q = Q("allow")
    out = ex(_event(name, {"host": "pypi.org"}, tool_use_id="toolu_1"), q, tmp_path, nonce=NONCE)
    assert isinstance(out, cc.Declined) and out.reason == "egress_not_levain" and q.filed == []


@pytest.mark.parametrize("name,key", [("bash", "dangerouslyDisableSandbox"), ("Bash", "DANGEROUSLYDISABLESANDBOX"),
                                      ("mcp__srv__Bash", "dangerouslyDisableSandbox"),
                                      ("BASH", "x__dangerouslydisablesandbox")])
def test_a_sandbox_escape_variant_is_declined_in_chat(tmp_path, name, key):
    q = Q("allow")
    out = ex(_event(name, {"command": "ls", key: True}, tool_use_id="toolu_1"), q, tmp_path, nonce=NONCE)
    assert isinstance(out, cc.Declined) and out.reason == "sandbox_escape_requested" and q.filed == []


@pytest.mark.parametrize("name", ["NetworkAccess", "SandboxNetworkAccessV2", "xSandboxNetworkAccess", 7, None])
def test_the_classifier_matches_only_the_documented_name(name):
    assert not contained.vendor_name_is(name, contained.SANDBOX_NET_TOOL)


def test_contained_shell_refusal_uses_the_classifier_for_the_key(ws):
    inp = {"command": "ls", "DangerouslyDisableSandbox": False}
    req = ExternalGateRequest.create(
        agent="claude_code", agent_session_id="s1", event_id="chat:toolu_1", tool_name="Bash",
        tool_input_digest=tool_input_digest(inp), display="d", cwd=ws, tier="T2",
        received_at="2026-10-27T12:00:00.000000Z", expires_at="2026-10-27T12:10:00.000000Z")
    assert contained_shell_refusal(_sh1(ws), req, tool_input=inp, posture="default", now=datetime.now(timezone.utc),
                                   home=os.path.dirname(ws), contained_launch=_proof(ws)) \
        == "the call asks to leave the sandbox"


def test_the_hook_path_is_not_declined_by_the_classifier(tmp_path):
    q = Q("allow")
    out = ex(_event("sandboxnetworkaccess", {"host": "x"}), q, tmp_path)
    assert isinstance(out, cc.Decided) and len(q.filed) == 1


# --- D6: a lost read after the take is re-read once, then abstained, never a second filing ------------------

class _Flaky(Q):
    def __init__(self, misses, **kw):
        super().__init__(None, **kw)
        self.misses, self.gets = misses, 0

    def get(self, request_id):
        self.gets += 1
        if self.gets <= self.misses:
            return None
        return super().get(request_id)


def test_one_transient_miss_after_the_take_is_re_read_and_delivered(tmp_path):
    q = _Flaky(1)
    q.take = _take(q)
    out = ex(_event(*CONFIG), q, tmp_path)
    assert isinstance(out, cc.Decided) and out.request.id == q.take.request_id and q.filed == [] and q.gets == 2


def test_a_lost_take_is_abstained_and_never_filed_again(tmp_path):
    # deepseek r1 MED 1: the take spent the gate's allow, then a second request was filed
    q = _Flaky(2)
    q.take = _take(q)
    out = ex(_event(*CONFIG), q, tmp_path)
    assert isinstance(out, cc.Abstained) and out.reason == "take_unread" and out.request is None and out.why
    assert q.filed == [] and q.gets == 2
    q2 = _Flaky(2)
    q2.take = _take(q2)
    assert cc.decide(_event(*CONFIG), q2, AWAY, environ=env(tmp_path), home=tmp_path, idle=lambda: None) is None
    assert q2.filed == []


# --- the one-line LOWs ---------------------------------------------------------------------------------------

@pytest.mark.parametrize("bad", [None, 7, b"hook:x"])
def test_tool_use_id_of_a_non_string_is_none(bad):
    # deepseek r1 LOW 4
    assert _tool_use_id_of(bad) is None

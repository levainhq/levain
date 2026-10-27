"""CH-D1c-B: the chat core's result union (``Decided | Abstained | Declined``), ``decide_ex`` and the values in
``levain.consent.contained``. ``decide()`` is ``decide_ex`` plus ``hook_output``, and says byte for byte what it said
at be0af189 on every scenario of ``tests/_decide_scenarios.py``."""
from __future__ import annotations

import json
import os
import typing
from dataclasses import fields, replace
from pathlib import Path

import pytest

from levain.consent.adapters import claude_code as cc
from levain.consent.contained import ContainedLaunchProof, CoreCall, core_call_of, plain_dns_host
from levain.consent.queue import Decision
from levain.consent.record import ExternalGateRequest
from tests._decide_scenarios import _event, run_all

GOLDEN = Path(__file__).parent / "fixtures" / "decide_golden_be0af189.json"
NONCE = "a" * 32
AWAY = cc.AdapterSettings(away_mode=True, away_hold_seconds=20)


class Q:
    """A deterministic core: ``verdict`` None / 'allow' / 'deny' at the wait; ``take`` a Decision the take returns."""

    directory = Path("/nonexistent/levain-consent")

    def __init__(self, verdict=None, *, at="await", take=None, get=True):
        self.verdict, self.at, self.take, self.get_ok = verdict, at, take, get
        self.requests: list[ExternalGateRequest] = []
        self.filed: list[dict] = []
        self.withdrawn: list[str] = []

    def file(self, **kw):
        self.filed.append(kw)
        r = ExternalGateRequest.create(
            agent=kw["agent"], agent_session_id=kw["agent_session_id"], event_id=kw["event_id"] or "rand:1",
            tool_name=kw["tool_name"], tool_input_digest="jcs:x", display="d", cwd=kw["cwd"], tier=kw["tier"],
            received_at="2026-10-13T20:00:00.000000Z", expires_at="2026-10-13T20:10:00.000000Z")
        self.requests.append(r)
        return r

    def _d(self, request_id):
        if self.verdict is None:
            return None
        digest = next(r.digest for r in self.requests if r.id == request_id)
        return Decision(request_id=request_id, decision=self.verdict, signer_fp="SHA256:k", decided_at="t",
                        request_digest=digest)

    def await_decision(self, request_id, timeout, **kw):
        return self._d(request_id) if self.at == "await" else None

    def withdraw(self, request_id, note=""):
        self.withdrawn.append(request_id)
        return True

    def decision(self, request_id, **kw):
        return None

    def answer_by_grant(self, request_id, **kw):
        return None

    def take_gate_allow(self, *a, **kw):
        return self.take

    def get(self, request_id):
        if not self.get_ok:
            return None
        for r in self.requests:
            if r.id == request_id:
                return r, "allowed"
        return None


def env(home):
    return {"HOME": str(home), "PATH": "/usr/bin:/bin"}


def ex(event, queue, tmp_path, *, settings=AWAY, nonce="", proof=None, **kw):
    return cc.decide_ex(event, queue, settings, environ=kw.pop("environ", env(tmp_path)), home=tmp_path,
                        launch_nonce=nonce, contained_launch=proof, idle=lambda: None, **kw)


# --- decide() is byte-identical ----------------------------------------------------------------------------

def test_decide_is_byte_identical_to_be0af189_on_every_scenario(tmp_path):
    assert run_all(cc, Decision, ExternalGateRequest, tmp_path) == json.loads(GOLDEN.read_text())


def test_decide_is_hook_output_of_a_decided_result_else_none(tmp_path):
    for verdict in ("allow", "deny", None):
        out = ex(_event(), Q(verdict), tmp_path)
        got = cc.decide(_event(), Q(verdict), AWAY, environ=env(tmp_path), home=tmp_path, idle=lambda: None)
        assert got == (cc.hook_output(out.decision) if isinstance(out, cc.Decided) else None)


# --- the signature -----------------------------------------------------------------------------------------

@pytest.mark.parametrize("missing", ["launch_nonce", "contained_launch"])
def test_launch_nonce_and_contained_launch_are_required(tmp_path, missing):
    kw = {"launch_nonce": "", "contained_launch": None}
    del kw[missing]
    with pytest.raises(TypeError):
        cc.decide_ex(_event(), Q("allow"), AWAY, environ=env(tmp_path), home=tmp_path, **kw)


def test_declined_reasons_are_the_literal_and_each_has_a_message():
    hints = typing.get_type_hints(cc.Declined)
    assert set(typing.get_args(hints["reason"])) == cc.DECLINED_REASONS
    assert set(cc.DECLINE_MESSAGES) == cc.DECLINED_REASONS
    assert all(isinstance(m, str) and m for m in cc.DECLINE_MESSAGES.values())
    assert set(typing.get_args(typing.get_type_hints(cc.Abstained)["reason"])) == {"no_decider", "hold_expired"}


# --- each exit of decide maps to one tag -------------------------------------------------------------------

@pytest.mark.parametrize("event", [["x"], _event(hook_event_name="PreToolUse"), _event(cwd=""),
                                   _event(session_id=None), _event(tool_name=7)])
def test_an_unreadable_event_is_declined_invalid_event_with_no_call(tmp_path, event):
    q = Q("allow")
    out = ex(event, q, tmp_path)
    assert isinstance(out, cc.Declined) and out.reason == "invalid_event" and out.call is None
    assert q.filed == []


def test_a_chat_call_without_a_tool_use_id_is_declined_invalid_event(tmp_path):
    q = Q("allow")
    out = ex(_event(), q, tmp_path, nonce=NONCE)
    assert isinstance(out, cc.Declined) and out.reason == "invalid_event" and q.filed == []


def test_nothing_can_decide_is_abstained_no_decider_with_nothing_filed(tmp_path):
    q = Q("allow")
    out = ex(_event(), q, tmp_path, settings=cc.AdapterSettings())
    assert isinstance(out, cc.Abstained) and out.reason == "no_decider" and out.request is None and out.why
    assert q.filed == []


def test_unconfined_agent_config_is_declined_unconfined(tmp_path):
    e = _event("Write", {"file_path": "/work/proj/.claude/settings.json", "content": "{}"})
    out = ex(e, Q("allow"), tmp_path, environ={**env(tmp_path), "LEVAIN_UNCONFINED": "1"})
    assert isinstance(out, cc.Declined) and out.reason == "unconfined"


def test_a_verified_answer_is_decided_with_its_request(tmp_path):
    q = Q("deny")
    out = ex(_event(), q, tmp_path)
    assert isinstance(out, cc.Decided) and out.decision.decision == "deny" and out.request is q.requests[0]


def test_no_answer_is_abstained_hold_expired_with_the_request(tmp_path):
    q = Q(None)
    out = ex(_event(), q, tmp_path)
    assert isinstance(out, cc.Abstained) and out.reason == "hold_expired" and out.request is q.requests[0]
    assert out.why == "" and q.withdrawn == [q.requests[0].id]


def test_every_result_carries_the_call_core_call_of_computes(tmp_path):
    e = _event(tool_use_id="toolu_9", agent_id="a1")
    for q in (Q("allow"), Q(None)):
        out = ex(e, q, tmp_path, nonce=NONCE)
        assert out.call == core_call_of(e, launch_nonce=NONCE)
        assert out.call.session.endswith("#agent:a1") and out.call.launch_nonce == NONCE


# --- the take path (R7-9) ----------------------------------------------------------------------------------

def _take(q):
    r = q.file(agent="claude_code", agent_session_id="s", tool_name="Write", tool_input={}, cwd="/work/proj",
               tier="T3", event_id="hook:t")
    q.filed.clear()
    return Decision(request_id=r.id, decision="allow", signer_fp="SHA256:k", decided_at="t", request_digest=r.digest)


CONFIG = ("Write", {"file_path": "/work/proj/.claude/settings.json", "content": "{}"})


def test_take_path_uses_request_not_tuple(tmp_path):
    q = Q(None)
    q.take = _take(q)
    out = ex(_event(*CONFIG), q, tmp_path)
    assert isinstance(out, cc.Decided) and isinstance(out.request, ExternalGateRequest)
    assert out.request.id == q.take.request_id and q.filed == []


@pytest.mark.parametrize("how", ["get_none", "other_digest"])
def test_take_path_unverified_take_falls_through_to_file(tmp_path, how):
    q = Q(None, get=how != "get_none")
    took = _take(q)
    q.take = replace(took, request_digest="sha256:other") if how == "other_digest" else took
    out = ex(_event(*CONFIG), q, tmp_path)
    assert len(q.filed) == 1 and isinstance(out, cc.Abstained) and out.reason == "hold_expired"
    assert cc.decide(_event(*CONFIG), q, AWAY, environ=env(tmp_path), home=tmp_path, idle=lambda: None) is None


# --- R8-5: the sandbox-escape key is declined in chat, before anything is filed ----------------------------

@pytest.mark.parametrize("value", [True, False, None, "yes"])
@pytest.mark.parametrize("with_proof", [True, False])
def test_dangerously_disable_sandbox_is_declined_deny_never_card(tmp_path, value, with_proof):
    q = Q("allow")
    e = _event(tool_input={"command": "ls", "dangerouslyDisableSandbox": value}, tool_use_id="toolu_1")
    out = ex(e, q, tmp_path, nonce=NONCE, proof=_proof(tmp_path) if with_proof else None)
    assert isinstance(out, cc.Declined) and out.reason == "sandbox_escape_requested" and q.filed == []
    assert out.call == core_call_of(e, launch_nonce=NONCE)


def test_decline_message_says_no_sandbox_escape():
    assert "dangerouslyDisableSandbox" in cc.DECLINE_MESSAGES["sandbox_escape_requested"]


def test_hook_path_unchanged_by_escape_decline(tmp_path):
    q = Q("allow")
    out = ex(_event(tool_input={"command": "ls", "dangerouslyDisableSandbox": True}), q, tmp_path)
    assert isinstance(out, cc.Decided) and len(q.filed) == 1


# --- a SandboxNetworkAccess ask in chat is declined (Q-R15-4 REC: loopback_host, egress_not_levain) --------

@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "foo.localhost.", "[::1]", "2130706433", "a.com:443"])
def test_sandbox_net_non_dns_host_is_declined_loopback_host(tmp_path, host):
    q = Q("allow")
    out = ex(_event("SandboxNetworkAccess", {"host": host}, tool_use_id="toolu_1"), q, tmp_path, nonce=NONCE)
    assert isinstance(out, cc.Declined) and out.reason == "loopback_host" and q.filed == []


@pytest.mark.parametrize("inp", [{"host": "pypi.org"}, {"host": "pypi.org", "port": 443}, {}, "pypi.org"])
def test_any_other_sandbox_net_ask_in_chat_is_declined_egress_not_levain(tmp_path, inp):
    q = Q("allow")
    out = ex(_event("SandboxNetworkAccess", inp, tool_use_id="toolu_1"), q, tmp_path, nonce=NONCE,
             proof=_proof(tmp_path))
    assert isinstance(out, cc.Declined) and out.reason == "egress_not_levain" and q.filed == []


# --- levain.consent.contained -------------------------------------------------------------------------------

def _proof(tmp_path, **kw):
    base = {"launch_id": "L1", "spawn_nonce": NONCE, "profile_digest": "b" * 64, "canary_verdict_id": "c" * 64,
            "cwd": os.path.realpath(tmp_path)}
    return ContainedLaunchProof(**{**base, **kw})


def test_a_well_formed_proof_has_no_problem(tmp_path):
    assert _proof(tmp_path).problem() is None


@pytest.mark.parametrize("field,value", [("launch_id", ""), ("spawn_nonce", "A" * 32), ("spawn_nonce", "a" * 31),
                                         ("profile_digest", "b" * 63), ("canary_verdict_id", "g" * 64),
                                         ("cwd", "rel/dir"), ("launch_id", None)])
def test_a_proof_of_the_wrong_shape_has_a_problem(tmp_path, field, value):
    assert _proof(tmp_path, **{field: value}).problem()


def test_a_proof_cwd_that_is_not_its_realpath_has_a_problem(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    (tmp_path / "link").symlink_to(real)
    assert _proof(tmp_path, cwd=str(tmp_path / "link")).problem()


def test_core_call_digest_changes_with_each_field():
    call = CoreCall(session="s", call_id="c", tool_name="Bash", input_digest="jcs:x", cwd="/w", launch_nonce=NONCE)
    seen = {call.digest()}
    for f in fields(CoreCall):
        seen.add(replace(call, **{f.name: getattr(call, f.name) + "z"}).digest())
    assert len(seen) == 1 + len(fields(CoreCall)) and len(call.digest()) == 64


def test_core_call_of_derives_as_decide_does():
    e = _event(tool_use_id="toolu_1", agent_id="a1", tool_input=None)
    call = core_call_of(e, launch_nonce="")
    assert call.session == "ceedbaa9-2e6d-433f-b374-0ad96dd5f62d#agent:a1" and call.call_id == "toolu_1"
    assert call.input_digest == core_call_of({**e, "tool_input": {}}, launch_nonce="").input_digest
    assert core_call_of(_event(), launch_nonce="").call_id == ""


@pytest.mark.parametrize("event", [_event(session_id=""), _event(cwd=None), _event(tool_use_id=3),
                                   _event(agent_id=5), "x"])
def test_core_call_of_refuses_an_event_that_names_no_call(event):
    with pytest.raises(ValueError):
        core_call_of(event, launch_nonce="")


@pytest.mark.parametrize("host", ["127.1", "2130706433", "0x7f.1", "0x7f000001", "0177.0.0.1", "0.0.0.0",
                                  "localhost.", "LOCALHOST", "foo.localhost.", "[::ffff:7f00:1]", "::1", "1.2.3.4",
                                  "a..b", "-a.com", "a.com:443", "user@a.com", "single", "", "a b.com", None])
def test_plain_dns_host_declines(host):
    assert plain_dns_host(host) is None


@pytest.mark.parametrize("host,name", [("pypi.org", "pypi.org"), ("files.pythonhosted.org", "files.pythonhosted.org"),
                                       ("registry.npmjs.org.", "registry.npmjs.org"),
                                       ("xn--bcher-kva.example", "xn--bcher-kva.example"), ("PyPI.org", "pypi.org")])
def test_plain_dns_host_accepts(host, name):
    assert plain_dns_host(host) == name

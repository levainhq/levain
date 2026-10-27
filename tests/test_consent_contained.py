"""CH-D1c-B, SH1: the contained-shell grant kind (``levain.authority``) and the one policy branch that reads it
(``levain.consent.policy.contained_shell_refusal``). The queue answer, the ``decision()`` re-delivery branch and the
``via`` migration are the next slice's (RETURN.md of lane d1cb)."""
from __future__ import annotations

import os
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

import levain.authority as auth
from levain.cockpit.consent import parse_stamp, utc_stamp
from levain.consent.contained import ContainedLaunchProof
from levain.consent.policy import contained_shell_refusal, grant_refusal
from levain.consent.record import ExternalGateRequest, tool_input_digest
from tests.chat_egress.keys_rig import Cockpit

NONCE = "a" * 32
DIGEST = "d" * 64
INPUT = {"command": "pytest -q", "description": "run the tests"}
NOW = datetime(2026, 10, 27, 12, 0, tzinfo=timezone.utc)


def _grant(workspace, /, **kw):
    g = {"id": "sh1-a", "kind": auth.CONSENT_CONTAINED_SHELL_KIND, "agent": "claude_code", "tools": ["Bash"],
         "workspace": str(workspace), "postures": ["default"], "containment_digest": DIGEST,
         "expires_at": utc_stamp(datetime.now(timezone.utc) + timedelta(days=7))}
    g.update(kw)
    return g


@pytest.fixture
def ws(tmp_path):
    d = tmp_path / "home" / "proj"
    d.mkdir(parents=True)
    return os.path.realpath(d)


def _home(ws):
    return os.path.dirname(ws)


def _proof(ws, **kw):
    base = {"launch_id": "L1", "spawn_nonce": NONCE, "profile_digest": DIGEST, "canary_verdict_id": "c" * 64,
            "cwd": ws}
    return ContainedLaunchProof(**{**base, **kw})


def _request(ws, *, tool_name="Bash", tool_input=INPUT, agent="claude_code", tier="T2", cwd=None):
    return ExternalGateRequest.create(
        agent=agent, agent_session_id="s1", event_id="chat:toolu_1", tool_name=tool_name,
        tool_input_digest=tool_input_digest(tool_input), display="d", cwd=cwd or ws, tier=tier,
        received_at="2026-10-27T12:00:00.000000Z", expires_at="2026-10-27T12:10:00.000000Z")


def refusal(ws, grant=None, request=None, *, tool_input=INPUT, posture="default", proof="default", now=None):
    return contained_shell_refusal(grant if grant is not None else _grant(ws),
                                   request if request is not None else _request(ws, tool_input=tool_input),
                                   tool_input=tool_input, posture=posture, now=now or datetime.now(timezone.utc),
                                   home=_home(ws), contained_launch=_proof(ws) if proof == "default" else proof)


# --- the policy branch -------------------------------------------------------------------------------------

def test_contained_shell_grant_answers_chat_bash(ws):
    assert refusal(ws) is None


@pytest.mark.parametrize("proof", [None, "bad-shape"])
def test_no_or_misshapen_proof_is_refused_first(ws, proof):
    p = None if proof is None else _proof(ws, spawn_nonce="X")
    assert refusal(ws, proof=p) == "no contained launch"


@pytest.mark.parametrize("name", ["mcp__srv__Bash", "bash", "BASH", "Bash ", "PowerShell"])
def test_contained_shell_grant_refuses_mcp_tool_named_bash(ws, name):
    req = _request(ws, tool_name=name)
    assert refusal(ws, request=req) == "not the Bash tool"


@pytest.mark.parametrize("value", [True, False, None])
def test_contained_shell_grant_refuses_dangerouslyDisableSandbox(ws, value):
    inp = {**INPUT, "dangerouslyDisableSandbox": value}
    assert refusal(ws, tool_input=inp) == "the call asks to leave the sandbox"


def test_dangerously_disable_sandbox_nested_is_not_looked_at(ws):
    inp = {**INPUT, "env": {"dangerouslyDisableSandbox": True}}
    assert refusal(ws, tool_input=inp) is None


def test_sandbox_network_access_never_answered_by_sh1(ws):
    inp = {"host": "pypi.org"}
    assert refusal(ws, request=_request(ws, tool_name="SandboxNetworkAccess", tool_input=inp),
                   tool_input=inp) == "not the Bash tool"


def test_input_other_than_filed_is_refused(ws):
    req = _request(ws)
    assert refusal(ws, request=req, tool_input={**INPUT, "command": "rm -rf ."}) == \
        "not the input the request was filed for"


def test_contained_shell_refused_for_other_containment_digest(ws):
    assert refusal(ws, proof=_proof(ws, profile_digest="e" * 64)) == "another containment"


def test_contained_shell_refused_outside_workspace(ws, tmp_path):
    other = os.path.realpath(tmp_path / "home")
    req = _request(other, cwd=other)
    assert refusal(ws, request=req, proof=_proof(other)) == "outside the grant's workspace"


def test_request_dir_other_than_the_launch_is_refused(ws):
    sub = os.path.join(ws, "sub")
    os.mkdir(sub)
    assert refusal(ws, request=_request(ws, cwd=sub)) == "not this launch's directory"


@pytest.mark.parametrize("posture", ["acceptEdits", "bypassPermissions", None, "Default"])
def test_posture_other_than_default_is_refused(ws, posture):
    assert refusal(ws, posture=posture) == "posture"


def test_expired_grant_is_refused(ws):
    assert refusal(ws, now=datetime.now(timezone.utc) + timedelta(days=8)) == "expired"


def test_agent_config_and_other_agent_are_refused(ws):
    assert refusal(ws, request=_request(ws, tier="T3")) == "agent config is never answered by a grant"
    assert refusal(ws, request=_request(ws, agent="codex")) == "the grant is for another agent"


def test_workspace_at_home_is_refused(ws):
    home = _home(ws)
    grant = _grant(home)
    req = _request(ws)
    assert contained_shell_refusal(grant, req, tool_input=INPUT, posture="default", now=datetime.now(timezone.utc),
                                   home=home, contained_launch=_proof(ws)) is not None


def test_a_consent_grant_is_not_a_contained_shell_grant(ws):
    g = {**_grant(ws), "kind": auth.CONSENT_GRANT_KIND}
    del g["containment_digest"]
    assert refusal(ws, grant=g) == "not a contained-shell grant"


def test_generic_grant_path_never_matches_contained_shell_kind(ws):
    g = _grant(ws)
    assert not auth.consent_grant(g) and auth.contained_shell_grant(g)
    assert grant_refusal(g, _request(ws), tool_input=INPUT, posture="default", now=datetime.now(timezone.utc),
                         home=_home(ws)) == "not a consent grant"


# --- the grant kind ----------------------------------------------------------------------------------------

def test_a_contained_shell_grant_is_appended_and_replayed(tmp_path, ws, monkeypatch):
    ck = Cockpit(tmp_path)
    ck.link({"op": "create_grant", "grant": _grant(ws)})
    held = ck.state().policy.grants["sh1-a"]
    assert auth.contained_shell_grant(held) and not auth.consent_grant(held) and not auth.inert_grant(held)


@pytest.mark.parametrize("change", [
    {"tools": ["Bash", "Read"]}, {"tools": ["Read"]}, {"postures": ["acceptEdits"]},
    {"postures": ["default", "plan"]}, {"agent": "codex"}, {"containment_digest": "D" * 64},
    {"containment_digest": "d" * 63}, {"workspace": "rel/proj"}, {"extra": 1}])
def test_contained_shell_grant_append_refuses_other_tools_postures_and_shapes(tmp_path, ws, change):
    ck = Cockpit(tmp_path)
    with pytest.raises(auth.AuthorityError):
        ck.link({"op": "create_grant", "grant": _grant(ws, **change)})


def test_contained_shell_grant_append_refuses_a_lifetime_past_30_days(tmp_path, ws):
    ck = Cockpit(tmp_path)
    far = utc_stamp(datetime.now(timezone.utc) + timedelta(days=31))
    with pytest.raises(auth.AuthorityError):
        ck.link({"op": "create_grant", "grant": _grant(ws, expires_at=far)})


def test_contained_shell_grant_append_refuses_home_and_symlinked_workspace(tmp_path, ws, monkeypatch):
    ck = Cockpit(tmp_path)
    monkeypatch.setenv("HOME", _home(ws))
    with pytest.raises(auth.AuthorityError):
        ck.link({"op": "create_grant", "grant": _grant(_home(ws), id="sh1-home")})
    link = tmp_path / "link"
    link.symlink_to(ws)
    with pytest.raises(auth.AuthorityError):
        ck.link({"op": "create_grant", "grant": _grant(link, id="sh1-link")})


def test_contained_shell_grant_revocable(tmp_path, ws):
    ck = Cockpit(tmp_path)
    ck.link({"op": "create_grant", "grant": _grant(ws)})
    assert auth.passkey_revocable(ck.state().policy.grants["sh1-a"])
    ck.link({"op": "revoke_grant", "grant_id": "sh1-a"})
    assert "sh1-a" not in ck.state().policy.grants


def test_contained_shell_grant_change_classes():
    o = _grant("/w/p")
    exp = lambda g: parse_stamp(g["expires_at"])  # noqa: E731
    later = {**o, "expires_at": utc_stamp(datetime(2099, 1, 1, tzinfo=timezone.utc))}
    assert auth._contained_shell_grant_change(o, later, exp(o), exp(later)) == "renew"
    under = {**o, "workspace": "/w/p/sub"}
    assert auth._contained_shell_grant_change(o, under, exp(o), exp(under)) == "tight"
    other = {**o, "containment_digest": "e" * 64}
    assert auth._contained_shell_grant_change(o, other, exp(o), exp(other)) == "loose"
    wider = {**o, "workspace": "/w"}
    assert auth._contained_shell_grant_change(o, wider, exp(o), exp(wider)) == "loose"


def test_proof_fields_are_compared_by_value(ws):
    # the proof is a frozen value: a changed field is another proof, never the same one
    assert _proof(ws) == _proof(ws) and replace(_proof(ws), launch_id="L2") != _proof(ws)


def test_the_replay_tier_order_is_the_cockpits():
    """``levain.authority`` holds its own tier order so root's replay never imports ``levain.cockpit.verbs`` (which
    reaches ``anneal_memory``); this holds the copy equal to the cockpit's, where that import resolves."""
    pytest.importorskip("anneal_memory")
    from levain.cockpit.verbs import TIERS
    assert auth._tiers() == TIERS

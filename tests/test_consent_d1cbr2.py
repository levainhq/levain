"""CH-D1c-B fix round 2 (1027-11/d1cbr2): away mode never decides an agent-config call SH1 was cleared for (R1);
every reader of ``via`` names a contained-shell answer as the standing grant it is, never an operator's answer (R2);
an SH1 grant is listed, revocable and its answers counted like any standing grant (R3)."""
from __future__ import annotations

import sqlite3
from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace

from levain.consent.adapters import claude_code as cc
from levain.consent.adapters import codex as cx
from levain.consent.grants import grant_listing, revoke_grant
from levain.consent.ledger import Ledger, gate_row
from levain.consent.listing import read_grant_evidence
from levain.consent.queue import Decision
from levain.dispatch.calibration import read_labels
from tests._decide_scenarios import _event
from tests.test_consent_core_union import NONCE, Q, ex
from tests.test_consent_d1cb_fix_r1 import INPUT, _only_sh1, _proof, _sh1
from tests.test_consent_d1cbq import _chat, _proof as _rig_proof, rig  # noqa: F401 - the shared fixture


# --- R1 (codex Q2): away mode is no decider for a T3 call ----------------------------------------------------

def test_away_mode_does_not_decide_an_agent_config_call_sh1_was_cleared_for(tmp_path, monkeypatch):
    # codex's probe: away on, only an SH1 grant, no active key, the Bash call tagged agent config: before the fix
    # away mode counted as a decider (tier None) and a T3 row no key can sign was filed
    settings = replace(_only_sh1(monkeypatch, tmp_path, _sh1(str(tmp_path))), away_mode=True, away_hold_seconds=20)
    monkeypatch.setattr(cc, "targets_agent_config", lambda *a, **kw: True)
    q = Q(None)
    out = ex(_event("Bash", INPUT, tool_use_id="toolu_1"), q, tmp_path, settings=settings, nonce=NONCE,
             proof=_proof(tmp_path))
    assert isinstance(out, cc.Abstained) and out.reason == "no_decider" and q.filed == []
    assert out.why == "no enrolled key can sign an agent-config allow"


# --- R2: the readers of `via` ---------------------------------------------------------------------------------

def _sh1_decision(**kw):
    base = dict(request_id="r1", decision="allow", signer_fp=None, decided_at="2026-10-27T12:00:00.000000Z",
                request_digest="x" * 64, via="contained_shell", grant_id="sh1-a", grant_seq=3, grant_hash="h" * 64,
                posture="default")
    return Decision(**{**base, **kw})


def test_the_gate_row_of_a_contained_shell_allow_is_stamped_grant_not_answer(tmp_path):
    qdir = tmp_path / "consent"
    qdir.mkdir(mode=0o700)
    event = {"session_id": "s1", "tool_use_id": "toolu_1", "permission_mode": "default", "tool_name": "Bash",
             "tool_input": INPUT, "cwd": "/w", "hook_event_name": "PreToolUse"}
    allow = SimpleNamespace(decision="allow", via="contained_shell", request_id="r1")
    assert gate_row("claude_code", qdir, event, "s1", "Bash", INPUT, "/w", decision=allow)
    raw = sqlite3.connect(Ledger(qdir).path).execute("SELECT phase, stamp FROM calls").fetchall()
    assert raw == [("pre", "grant")]


def test_calibration_counts_a_contained_shell_allow_as_an_excluded_grant(rig):  # noqa: F811
    out = _chat(rig, proof=_rig_proof(rig["ws"]))
    assert isinstance(out, cc.Decided) and out.decision.via == "contained_shell"
    got = read_labels(rig["q"].directory, now=datetime.now(timezone.utc))
    assert got.labels == () and got.excluded_grants == 1


def test_claude_gate_text_names_the_contained_shell_grant():
    reason = str(cc.gate_output(_sh1_decision()))
    assert "contained-shell grant sh1-a" in reason and "key None" not in reason and "signed levain answer" not in reason


def test_claude_hook_output_names_the_contained_shell_grant():
    out = cc.hook_output(_sh1_decision())
    assert "contained-shell grant sh1-a" in out["systemMessage"]
    assert "levain consent grant revoke sh1-a" in out["systemMessage"]


def test_codex_gate_text_names_the_contained_shell_grant():
    reason = str(cx.gate_output(_sh1_decision()))
    assert "contained-shell grant sh1-a" in reason and "key None" not in reason and "signed levain answer" not in reason


# --- R3: an SH1 grant is listed, counted and revocable --------------------------------------------------------

def test_contained_shell_grant_revocable_and_listed(rig):  # noqa: F811
    ck, now = rig["ck"], datetime.now(timezone.utc)
    grants, inert = grant_listing(ck.root, now=now)
    (g,) = [g for g in grants if g.id == "sh1-a"]
    assert inert == [] and g.tools == ("Bash",) and g.tool_problem("Bash") is None
    assert g.containment_digest == "d" * 64
    out = _chat(rig, proof=_rig_proof(rig["ws"]))
    assert isinstance(out, cc.Decided) and out.decision.via == "contained_shell"
    seq, link_hash = ck.state().grant_link("sh1-a")
    evidence = read_grant_evidence(rig["q"].directory, [("sh1-a", seq, link_hash)])
    assert evidence["sh1-a"]["count"] == 1 and evidence["sh1-a"]["recent"][0]["request_id"] == out.request.id
    # the call is then refused after the allow: the grant's refused count holds it, as a consent grant's does
    ledger = Ledger(rig["q"].directory)
    common = dict(agent="claude_code", agent_session_id="sess-1", tool_name="Bash", tool_input=INPUT, cwd=rig["ws"],
                  tool_use_id="toolu_1")
    ledger.record_call(**common, phase="pre", stamp="grant", request_id=out.request.id)
    ledger.record_call(**common, phase="post_failure", stamp="unknown", outcome="refused")
    evidence = read_grant_evidence(rig["q"].directory, [("sh1-a", seq, link_hash)])
    assert evidence["sh1-a"]["refused_count"] == 1 and evidence["sh1-a"]["recent"][0]["outcome"] == "refused"
    revoke_grant(ck.root, "sh1-a", **ck.kw())
    assert [g.id for g in grant_listing(ck.root, now=now)[0]] == []
    # an answer it already gave is no longer delivered, even to the launch it was given under
    assert rig["q"].decision(out.request.id, tool_input=INPUT, posture="default",
                             contained_launch=_rig_proof(rig["ws"])) is None

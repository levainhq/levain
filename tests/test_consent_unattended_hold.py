"""11-07 (Phill 08:38, the stuck-seat approvals): a ``claude -p`` seat (entrypoint ``sdk-*``) has no dialog anyone can
answer, so the hook holds its request for a signed answer whatever the presence record says, and does not hand it back
to a dialog when presence reads present. Reproduced first by ``consent_trial.py fire`` with Phill present: the request
was filed, no hold ran, no row reached the band, the seat exited with "The permission prompt was not granted"."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from levain.consent.adapters import claude_code as cc
from levain.consent.queue import ConsentQueue
from tests._decide_scenarios import _event
from tests.chat_egress.keys_rig import Cockpit


@pytest.fixture
def rig(tmp_path):
    home = tmp_path / "home"
    ws = home / "proj"
    ws.mkdir(parents=True)
    ck = Cockpit(tmp_path)          # one enrolled key: a signed answer can verify
    q = ConsentQueue(tmp_path / "levain-home" / "consent", authority=ck.source(), home=os.path.realpath(home))
    return {"ws": os.path.realpath(ws), "home": Path(os.path.realpath(home)), "ck": ck, "q": q}


def _run(rig, monkeypatch, *, entrypoint, settings, present=True, away_hold=0.0):
    seen: dict = {}

    def fake_hold(queue, request, wait, **kw):
        seen["wait"], seen["still_away"] = wait, kw.get("still_away")
        return None, None

    monkeypatch.setattr(cc, "hold_for_answer", fake_hold)
    monkeypatch.setattr(cc, "hold_ping", lambda *a, **k: True)
    # the presence record reads present: no away hold (the record lives outside this rig, so it is stood in for)
    monkeypatch.setattr(cc, "away_hold", lambda *a, **k: away_hold)
    env = {"HOME": str(rig["home"]), "PATH": "/usr/bin:/bin"}
    if entrypoint:
        env["CLAUDE_CODE_ENTRYPOINT"] = entrypoint
    ev = _event("Bash", {"command": "echo consent-trial-ok"}, cwd=rig["ws"])
    # present: input idle 0 s, so the idle/presence reading says the operator is at the desk
    out = cc.decide_ex(ev, rig["q"], settings, environ=env, home=rig["home"], launch_nonce="", contained_launch=None,
                       idle=lambda: 0.0, max_hold=cc.CLEANUP_RESERVE + 30,
                       still_away=lambda: (False, "intent", "present"))
    return out, seen


def _settings(rig, **kw):
    return cc.AdapterSettings(cockpit_data_root=str(rig["ck"].root), away_hold_seconds=20, **kw)


def test_a_headless_seat_is_held_while_he_is_present_and_the_hold_is_not_cut_short(rig, monkeypatch):
    out, seen = _run(rig, monkeypatch, entrypoint="sdk-cli", settings=_settings(rig))
    assert isinstance(out, cc.Abstained) and out.reason == "hold_expired"
    assert seen["wait"] > 0 and seen["still_away"] is None, seen


def test_a_headless_seat_already_held_for_away_is_not_cut_short_when_he_returns(rig, monkeypatch):
    """L3 r1 (complement, deepseek, codex converged): the away hold was already positive, so the forced path was
    skipped and the presence recheck ended the wait the moment he came back."""
    out, seen = _run(rig, monkeypatch, entrypoint="sdk-cli", settings=_settings(rig), away_hold=30.0)
    assert seen["wait"] > 0 and seen["still_away"] is None, seen


def test_an_interactive_seat_present_still_gets_its_own_dialog_not_a_hold(rig, monkeypatch):
    out, seen = _run(rig, monkeypatch, entrypoint="cli", settings=_settings(rig))
    assert seen == {}, "an attended session's dialog is his: no hold when present"


def test_his_explicit_off_word_still_stops_the_hold_for_a_headless_seat(rig, monkeypatch):
    out, seen = _run(rig, monkeypatch, entrypoint="sdk-cli", settings=_settings(rig, explicit_off=True))
    assert seen == {}

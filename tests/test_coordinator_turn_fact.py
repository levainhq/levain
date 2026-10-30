"""C11 S1+S2 L3 r1: the turn's classification is ONE fact per hook call, carried from UserPromptSubmit to Stop.

Each test is a failure reproduced on 49418381 + adf03277 (the r1 findings, 1030-24_L3_c11s12_r1.txt): the launch's
real spawn -> first prompt -> activate ordering (codex HIGH 1), a second launch-row read that fails where the first
did not (codex HIGH 2, deepseek HIGH 1), a Stop that re-reads the viewer (complement 2), a wake record that outlived
its turn (complement 1, deepseek 2), one staged view shared by two coordinator sessions (codex 2), turn records that
pile up per launch (complement 3), store reads outside the hook's deadline (codex MED 4) and the kind consumers
(codex MED 3)."""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess

import pytest

from levain import band_view
from levain.coordinator import door
from levain.coordinator import launch as cl
from levain.coordinator import lease
from levain.coordinator.store import CoordStore
from tests.test_coordinator_viewer_role import SID_B, band_of, second_launch
from tests.test_coordinator_wake_silent import SID, Rig

SID_N = "sess-coord-unbound"


@pytest.fixture(params=["claude_code", "codex"])
def rig(request, tmp_path):
    return Rig(tmp_path, request.param)


def payload(rig: Rig, sid: str = SID, **kw) -> dict:
    return {"session_id": sid, "cwd": str(rig.install), **kw}


class Flaky:
    """``lease.by_harness_session`` that answers while ``ok`` and raises as a held store does otherwise."""

    def __init__(self, monkeypatch, ok=lambda n: True):
        self.real, self.n, self.ok = lease.by_harness_session, 0, ok
        monkeypatch.setattr(lease, "by_harness_session", self)

    def __call__(self, conn, sid):
        self.n += 1
        if not self.ok(self.n):
            raise sqlite3.OperationalError("database is locked")
        return self.real(conn, sid)


def test_the_first_prompt_before_activation_is_a_wake(rig, monkeypatch):
    """codex HIGH 1: ``claude --bg`` submits the first prompt before ``launch_activate`` records the session, so the
    UserPromptSubmit hook finds no launch row for it. The marker's launch id is the provenance: a coordinator launch
    row that is unbound (or bound to this session) makes it a wake. The REAL ordering: spawn (the hook fires inside
    it), read back, then activate."""
    rig.ask("Pick a DB")
    first = rig.ups("hello coordinator")
    assert rig.stop(band_of(first)) == ""                         # launch A's view, committed
    rig.ask("Ship it?")
    store = CoordStore(rig.qdir)
    store.coord_release()
    d, _ = store.accept_drop("second wake", surface="cli")
    launch_id = store.woken_launch(d.drop_id)
    seen: dict = {}

    def run(argv, *, cwd, env, timeout):
        if "--bg" in argv:                                        # the harness's first turn runs while --bg starts it
            seen["unbound"] = store.launch(launch_id).harness_session_id
            seen["ctx"] = rig.ups(argv[-1], sid=SID_N)
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(cl, "hook_live", lambda d: (True, "wired to this install"))
    monkeypatch.setattr(door, "launch_refusal", lambda d: None)
    monkeypatch.setattr(cl, "_harness", lambda: ("/fake/claude", {}))
    monkeypatch.setattr(cl, "_handover_blocked", lambda s, e, v: "")
    monkeypatch.setattr(cl, "_run", run)
    monkeypatch.setattr(cl, "_read_back", lambda exe, name, *, cwd, env: [
        {"id": "hid-n", "sessionId": SID_N, "cwd": os.path.realpath(str(rig.install))}])
    got = cl.launch_session(store, launch_id)
    assert got.started, got.refusal
    assert seen["unbound"] is None                                # the hook really ran before the activation
    assert band_view.WAKE_LINE in seen["ctx"] and "NEEDS YOU" not in seen["ctx"], seen["ctx"]
    assert rig.stop("wake: nothing needs you", sid=SID_N) == ""   # no band demand of the wake reply
    assert "since your last message: new 2" in rig.ups("what changed?", sid=SID_N)   # the delta is still his
    kinds = [k for k, t in rig.transcript() if t.startswith("[levain wake") or t.startswith("wake:")]
    assert kinds == ["wake", "wake_reply"]


def test_a_marker_naming_a_launch_bound_elsewhere_is_no_wake(rig):
    """The unbound-row rule admits only an unbound launch or this session's own: another session's launch id is the
    operator's text and gets today's band."""
    rig.ask("Pick a DB")
    assert "NEEDS YOU" in rig.ups(rig.wake, sid="some-other-seat")        # launch A is bound to SID
    assert "NEEDS YOU" in rig.ups("hello\n" + rig.wake)                   # the marker not as the first line


def test_one_launch_row_read_classifies_the_whole_prompt(rig, monkeypatch):
    """codex HIGH 2 / deepseek HIGH 1: the first read said coordinator, a later read in the same hook failed, and the
    wake rendered the band. One read decides it now."""
    rig.ask("Pick a DB")
    flaky = Flaky(monkeypatch, ok=lambda n: n == 1)
    text, _commit = band_view.hook_prompt(payload(rig, prompt=rig.wake), rig.env, rig.agent)
    assert text == band_view.WAKE_LINE, text
    assert flaky.n == 1


def test_stop_uses_the_viewer_its_prompt_chose(rig, monkeypatch):
    """complement 2: the prompt's launch-row read failed (a per-session viewer, committed at prompt time); Stop's own
    read succeeded, picked the coordinator viewer, found nothing staged and let a band-dropping reply through."""
    rig.ask("Pick a DB")
    busy = {"on": True}
    Flaky(monkeypatch, ok=lambda n: not busy["on"])
    text, commit = band_view.hook_prompt(payload(rig, prompt="hello"), rig.env, rig.agent)
    assert "NEEDS YOU" in text
    commit()                                                       # delivered
    busy["on"] = False
    reason = band_view.hook_stop(payload(rig, last_assistant_message="done", stop_hook_active=False), rig.env,
                                 rig.agent)
    assert reason and "Reprint" in reason, reason


def test_an_operator_turn_after_a_wake_is_logged_as_his(rig, monkeypatch):
    """complement 1 / deepseek 2: the operator prompt after a wake returned early (an error) before it overwrote the
    wake's record, and the transcript logged his words as a wake."""
    rig.ask("Pick a DB")
    rig.ups(rig.wake)

    def boom(*a, **kw):
        raise OSError("store gone")

    with monkeypatch.context() as m:
        m.setattr(band_view, "QuestionStore", boom)
        band_view.hook_prompt(payload(rig, prompt="what changed?"), rig.env, rig.agent)
    assert band_view.turn_log_kind(payload(rig, prompt="what changed?"), "prompt", rig.env, rig.agent) == "prompt"
    assert band_view.turn_log_kind(payload(rig, last_assistant_message="x"), "reply", rig.env, rig.agent) == "reply"


def test_a_second_coordinator_session_keeps_the_firsts_staged_view(rig):
    """codex 2: one staged file per global viewer; launch B's prompt replaced launch A's stage, and A's Stop failed
    open (a foreign session's record). Staged views are keyed by session."""
    rig.ask("Pick a DB")
    rig.ups("hello coordinator")                                   # A: staged
    second_launch(rig)
    rig.ups("hi", sid=SID_B)                                       # B: staged
    assert json.loads(rig.stop("done") or "{}").get("decision") == "block"            # A's reply dropped its band
    assert json.loads(rig.stop("done", sid=SID_B) or "{}").get("decision") == "block"  # and B's own stage survived A's Stop


def test_a_new_coordinator_session_keeps_a_live_launchs_turn_record_and_age_collects_a_dead_one(rig):
    """L3 r2 codex HIGH: launch B's prompt deleted launch A's still-live record, so A's Stop (with a transient launch-row
    fault) classed A as attended and let a band-dropping reply pass. A record leaves with its own Stop, or by age."""
    import os
    rig.ups("hello coordinator")                                    # A
    second_launch(rig)
    rig.ups("hi", sid=SID_B)                                        # B prompts while A is live
    recs = sorted((rig.qdir / "band_stop").glob("s-*.turn.rec"))
    assert len(recs) == 2                                           # A's record survived B's prompt
    old = recs[0]
    os.utime(old, (1, 1))                                           # a record nobody removed, a day old
    second_launch(rig, sid="sess-coord-launch-c")
    rig.ups("hey", sid="sess-coord-launch-c")
    left = sorted((rig.qdir / "band_stop").glob("s-*.turn.rec"))
    assert old not in left and len(left) == 2


def test_the_transcript_kind_reads_no_store(rig, monkeypatch):
    """codex MED 4: ``turn_log_kind`` reopened the coordinator and question stores outside the hook's deadline. It
    reads the turn record only; Stop, with a record, reads no launch row."""
    rig.ask("Pick a DB")
    rig.ups(rig.wake)
    opened = []
    real_open, real_peek = CoordStore.open_readonly, band_view.QuestionStore._peek
    monkeypatch.setattr(CoordStore, "open_readonly", lambda self, *a, **k: opened.append("coord") or real_open(self, *a, **k))
    monkeypatch.setattr(band_view.QuestionStore, "_peek", lambda self, *a, **k: opened.append("q") or real_peek(self, *a, **k))
    assert band_view.turn_log_kind(payload(rig, prompt=rig.wake), "prompt", rig.env, rig.agent) == "wake"
    assert band_view.turn_log_kind(payload(rig, last_assistant_message="x"), "reply", rig.env, rig.agent) == "wake_reply"
    assert opened == []
    assert band_view.hook_stop(payload(rig, last_assistant_message="x", stop_hook_active=False), rig.env,
                               rig.agent) is None
    assert "coord" not in opened


def test_topology_reads_a_wake_as_an_open_turn_and_its_reply_as_the_end():
    """codex MED 3: the topology knew only prompt/tool (open) and reply (done)."""
    from levain.cockpit import topology

    assert topology._node_state(False, 10, "wake_reply") == "idle"
    assert topology._node_state(False, topology.STALL_S + 1, "wake") == "stalled"
    assert topology._node_state(False, 10, "wake") == "running"

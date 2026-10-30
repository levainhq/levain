"""C11 S1: a coordinator wake is silent at Levain's boundary. Built from the reproduced failures (1030-2-c11/DESIGN.md
section 1): a real coordinator launch row, and the installed hook ENTRY scripts run as subprocesses with real hook JSON.
F1 (a wake spent the operator's delta), F2 (the Stop hook blocked a wake reply), F4 (the cockpit transcript logged the
wake as his words), R (a ``levain:`` relay on another seat is unchanged), and the fail-closed record."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

import levain
from levain import band_view
from levain.consent.questions import QuestionStore
from levain.coordinator import launch as cl
from levain.coordinator.store import CoordStore

TEMPLATES = Path(levain.__file__).parent / "templates"
SRC = Path(levain.__file__).resolve().parent.parent
SID = "sess-coord"
ENTRY = {"claude_code": TEMPLATES / "activation" / "hooks",
         "codex": TEMPLATES / "adapters" / "codex" / "activation" / "hooks"}


class Rig:
    def __init__(self, tmp: Path, agent: str):
        self.agent = agent
        self.home = tmp / "home"
        self.lv = self.home / "lv"
        self.qdir = self.lv / "consent"
        self.qdir.mkdir(parents=True, mode=0o700)
        self.qdir.chmod(0o700)
        self.install = tmp / "install"
        self.hooks = self.install / "activation" / "hooks"
        shutil.copytree(ENTRY[agent], self.hooks)
        (self.install / ".levain").mkdir()
        (self.install / ".levain" / "config.json").write_text('{"role": "coordinator"}\n')
        self.env = {"HOME": str(self.home), "LEVAIN_HOME": str(self.lv), "PATH": os.environ["PATH"],
                    "PYTHONPATH": str(SRC), "LEVAIN_SCOPE": "global"}
        store = CoordStore(self.qdir)
        store.coord_set_agent("claude", model="haiku", cwd=str(self.install.resolve()))
        d, _ = store.accept_drop("fix the build", surface="cli")
        launch_id = store.woken_launch(d.drop_id)
        store.launch_activate(launch_id, harness_session_id=SID)
        self.launch = store.launch_by_harness_session(SID)
        self.wake = cl._prompt(self.launch, cl.brief_path(self.launch))
        self.qs = QuestionStore(self.qdir)

    def ask(self, what):
        return self.qs.file(agent="claude_code", agent_session_id="other-seat", what=what,
                            options=[("a", "yes"), ("b", "no")], rec="a", blocks="x", cwd=str(self.home))

    def run(self, script, payload, sid=SID):
        p = subprocess.run([sys.executable, str(self.hooks / script)], input=json.dumps({"session_id": sid, **payload}),
                           capture_output=True, text=True, env=self.env, cwd=str(self.install), timeout=60)
        return p.stdout.strip()

    def ups(self, prompt, sid=SID):
        out = self.run("user_prompt_submit.py", {"prompt": prompt, "cwd": str(self.install)}, sid)
        return json.loads(out)["hookSpecificOutput"]["additionalContext"] if out else ""

    def stop(self, message, sid=SID):
        return self.run("stop.py", {"last_assistant_message": message, "cwd": str(self.install),
                                    "stop_hook_active": False}, sid)

    def transcript(self):
        out = []
        for f in sorted((self.install / ".levain" / "sessions").glob("*.log.jsonl")):
            out += [(e["kind"], e["text"]) for e in map(json.loads, f.read_text().splitlines())]
        return out


@pytest.fixture(params=["claude_code", "codex"])
def rig(request, tmp_path):
    return Rig(tmp_path, request.param)


def test_marker_is_the_prompts_first_line(rig):
    assert rig.wake.splitlines()[0] == f"[levain wake {rig.launch.launch_id}]"
    assert band_view.wake_marker(rig.launch.launch_id) == rig.wake.splitlines()[0]


def test_f1_a_wake_spends_no_delta(rig):
    q1 = rig.ask("Pick a DB")
    first = rig.ups("hello coordinator")
    assert "first band on this seat" in first
    assert rig.stop(first[first.find("NEEDS YOU"):]) == ""      # S2: a coordinator's view commits at its verified Stop
    rig.qs.answer(q1.id, option="a", channel="cli")
    rig.ask("Ship it?")
    ctx = rig.ups(rig.wake)
    assert "NEEDS YOU" not in ctx and band_view.WAKE_LINE in ctx
    nxt = rig.ups("what changed?")
    assert "since your last message: new 2 · closed 1 (answered via cli)" in nxt     # F1: the wake left the delta alone


def test_f2_the_stop_hook_does_not_demand_the_band_of_a_wake_reply(rig):
    rig.ask("Pick a DB")
    rig.ups("hello coordinator")
    rig.ups(rig.wake)
    assert rig.stop("split drop d1 into 2 threads") == ""
    rig.ups("what now?")                                  # his next turn: the verifier is back on
    assert json.loads(rig.stop("done"))["decision"] == "block"


def test_f4_the_transcript_marks_a_wake_never_as_his_words(rig):
    rig.ask("Pick a DB")
    rig.ups("hello coordinator")
    rig.ups(rig.wake)
    rig.stop("split drop d1 into 2 threads")
    rig.ups("what changed?")
    rig.stop("nothing")
    kinds = [k for k, _ in rig.transcript()]
    assert kinds == ["prompt", "wake", "wake_reply", "prompt", "reply"]
    assert rig.transcript()[1][1].startswith("[levain wake ")


def test_missing_stale_or_foreign_turn_record_verifies(rig):
    rig.ask("Pick a DB")
    rig.ups("hello coordinator")
    rig.ups(rig.wake)
    rec = next((rig.qdir / "band_stop").glob("*.turn.*"))
    saved = rec.read_text()
    rec.write_text("{ not json")                          # unreadable
    assert json.loads(rig.stop("x"))["decision"] == "block"
    # (the one block per view is spent; a fresh view and wake follow for each further case)
    for bad in ({"kind": "wake", "at": 1, "session": SID},                  # older than the view
                {"kind": "wake", "at": 2 ** 62, "session": "someone-else"},  # names another session
                {"kind": "operator", "at": 2 ** 62, "session": SID}):       # not a wake
        rig.ask("again")
        rig.ups("anything new?")
        rec.write_text(json.dumps(bad))
        assert json.loads(rig.stop("x"))["decision"] == "block", bad
    rec.write_text(saved)
    rec.unlink()                                          # missing
    rig.ask("once more")
    rig.ups("anything new?")
    assert json.loads(rig.stop("x"))["decision"] == "block"


def test_the_marker_is_the_coordinators_alone(rig):
    """Provenance plus role: the marker typed on another session, or another launch's marker on this one, is an
    operator turn and gets today's band."""
    rig.ask("Pick a DB")
    assert "NEEDS YOU" in rig.ups(rig.wake, sid="some-other-seat")
    forged = rig.wake.replace(rig.launch.launch_id, "ln-" + "0" * 32, 1)
    assert "NEEDS YOU" in rig.ups(forged)


def test_r_a_levain_relay_line_on_another_seat_is_unchanged(rig):
    """Q17's rule, pinned: a ``levain:`` relay still renders on an attended seat and is verified."""
    rig.ask("Pick a DB")
    ctx = rig.ups("levain: an answer was recorded for question #9", sid="plain-seat")
    assert "NEEDS YOU" in ctx
    assert json.loads(rig.stop("done", sid="plain-seat"))["decision"] == "block"
    assert band_view.turn_kind("levain: x", band_view.SeatFact(False), {}) == "relay"
    assert band_view.turn_kind("hi", band_view.SeatFact(False), {}) == "operator"

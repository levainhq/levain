"""C11 S2: the coordinator's band viewer is its role, and its view commits at Stop. Built from F3 (1030-2-c11/DESIGN.md
section 1): the delta reset on every launch because the viewer was ``agent:session`` and every launch is a new session.
Two REAL coordinator launch rows (the first ended by ``coord release``, a second woken), the installed hook entry
scripts as subprocesses with real hook JSON."""
from __future__ import annotations

import json

import pytest

from levain import band_view
from levain.coordinator import launch as cl
from levain.coordinator.store import CoordStore
from tests.test_coordinator_wake_silent import SID, Rig

SID_B = "sess-coord-launch-b"


def band_of(ctx: str) -> str:
    return ctx[ctx.find("NEEDS YOU"):]


def second_launch(rig: Rig, sid: str = SID_B) -> None:
    """End launch A by ``coord release``, then wake a second coordinator launch (a new drop) with its own session."""
    store = CoordStore(rig.qdir)
    store.coord_release()
    d, _ = store.accept_drop("second wake", surface="cli")
    store.launch_activate(store.woken_launch(d.drop_id), harness_session_id=sid)
    ln = store.launch_by_harness_session(sid)
    assert ln.role == "coordinator" and ln.launch_id != rig.launch.launch_id


@pytest.fixture(params=["claude_code", "codex"])
def rig(request, tmp_path):
    return Rig(tmp_path, request.param)


def test_f3_the_next_launch_continues_the_delta(rig):
    rig.ask("Pick a DB")
    first = rig.ups("hello coordinator")
    assert "first band on this seat" in first
    assert rig.stop(band_of(first)) == ""                      # a verified reply: the view commits at Stop
    rig.ask("Ship it?")
    second_launch(rig)
    ctx = rig.ups("anything new?", sid=SID_B)
    assert "first band on this seat" not in ctx, ctx            # F3: failed on 7956c9db / 49418381
    assert "since your last message: new 2" in ctx


def test_an_interrupted_turn_leaves_the_delta_for_the_next_reply(rig):
    rig.ask("Pick a DB")
    first = rig.ups("hello coordinator")
    assert rig.stop(band_of(first)) == ""
    rig.ask("Ship it?")
    ctx = rig.ups("what changed?")                             # delivered ...
    assert "since your last message: new 2" in ctx
    # ... and no Stop (interrupted, crashed): the cursor stayed where the last verified reply left it
    again = rig.ups("hello? (resent)")
    assert "since your last message: new 2" in again, again
    assert rig.stop(band_of(again)) == ""
    assert "since your last message" not in rig.ups("and now?")        # the verified reply spent it


def test_shorthand_in_launch_b_binds_to_launch_as_committed_view(rig):
    q1 = rig.ask("Pick a DB")
    first = rig.ups("hello coordinator")
    assert rig.stop(band_of(first)) == ""
    second_launch(rig)
    assert rig.qs.get(q1.id).answer is None
    rig.ups("1a", sid=SID_B)                                   # typed in launch B, about launch A's committed view
    assert rig.qs.get(q1.id).answer is not None                # bound to A's view: it would be "no band seen" otherwise


def test_a_reply_that_drops_the_band_does_not_commit(rig):
    rig.ask("Pick a DB")
    first = rig.ups("hello coordinator")
    assert json.loads(rig.stop("done"))["decision"] == "block"       # the one block
    again = rig.ups("hello again")
    assert "first band on this seat" in again                        # nothing was committed by the unverified reply


def test_the_reprint_after_the_block_commits(rig):
    rig.ask("Pick a DB")
    first = rig.ups("hello coordinator")
    block = json.loads(rig.stop("done"))
    assert block["decision"] == "block"
    out = rig.run("stop.py", {"last_assistant_message": band_of(first), "cwd": str(rig.install),
                              "stop_hook_active": True})
    assert out == ""
    assert "first band on this seat" not in rig.ups("next")


def test_a_wake_does_not_commit_a_staged_view(rig):
    rig.ask("Pick a DB")
    rig.ups("hello coordinator")                               # staged, never stopped
    rig.ups(rig.wake)
    assert rig.stop("split drop d1") == ""
    assert "first band on this seat" in rig.ups("hi")          # the wake's Stop committed nothing


def test_an_empty_band_with_a_delta_is_still_verified(rig):
    q1 = rig.ask("Pick a DB")
    first = rig.ups("hello coordinator")
    assert rig.stop(band_of(first)) == ""
    rig.qs.answer(q1.id, option="a", channel="cli")
    ctx = rig.ups("what now?")
    assert "NEEDS YOU · 0 open" in ctx
    assert json.loads(rig.stop("nothing to report"))["decision"] == "block"   # the early return on an empty list is gone


def test_other_seats_still_commit_at_prompt_time(rig):
    rig.ask("Pick a DB")
    assert "first band on this seat" in rig.ups("hi", sid="plain-seat")
    assert "first band on this seat" not in rig.ups("hi again", sid="plain-seat")   # no Stop between: unchanged

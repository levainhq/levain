"""P10a, item (5) of levain#121 (L3): a claimed dream row and its card, duplicated, through a REAL ``dream start`` fire
against the stub ``claude`` (``tests/_p10a_stub.py``, ``Install(tmp, stub=True)``).

(5a) a second fire process for the occurrence whose first fire holds the claim; (5b) a day already dreamed, started
again, and its notices ticked twice. The in-tree precedent is ``tests/test_agents_dq_fix.py::
test_two_fires_spawned_for_one_row_run_one_body`` (two fires, an injected body); these run the product's own body."""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import time

import pytest

pytest.importorskip("cryptography")
from tests._p10a_rig import Install, utc  # noqa: E402

pytestmark = pytest.mark.skipif(shutil.which("ssh-keygen") is None, reason="needs ssh-keygen")

_VALID = """\
## State
First consolidation of the test store.

## Active Threads
- Getting my bearings: pointer: this session.

## Patterns
Nothing graduated yet.

## Decisions
Nothing committed yet.

## Context
The first dream metabolized the opening episodes.

## Understanding
Early days.
"""


def _wait(pred, what: str, seconds: float = 90.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        got = pred()
        if got:
            return got
        time.sleep(0.25)
    raise AssertionError(f"timed out waiting for {what}")


def _started(inst: Install):
    """A signed install and one real ``dream start`` (a detached fire). Returns the Ran."""
    assert inst.enable("--time", "00:00").rc == 0
    assert inst.sign(inst.link_doc()).rc == 0
    ran = inst.cli("dream", "start", spawn_gate=False)
    assert ran.rc == 0, ran.text
    return ran


def _cards(inst: Install, *extra: str) -> list[dict]:
    got = inst.cli("desk", "review", "list", "--queue-dir", str(inst.desk), "--all", "--json", *extra, spawn_gate=False)
    assert got.rc == 0, got.text
    return [i for i in json.loads(got.out)["items"] if i["feed"] == "agent"]




def _saving(inst: Install, sleep: float = 0.0) -> None:
    """The stub's reply is a valid dream package, so the fire saves and the dream files its card."""
    inst.stub.reply({"type": "result", "subtype": "success", "is_error": False, "result": _VALID,
                     "usage": {"input_tokens": 1, "output_tokens": 1, "cache_read_input_tokens": 0,
                               "cache_creation_input_tokens": 0},
                     "modelUsage": {"claude-opus-5-5": {"costUSD": 0.01}}, "total_cost_usd": 0.01})
    if sleep:
        inst.stub.sleep(sleep)


def _settled(inst: Install, n: int = 1) -> list[dict]:
    return _wait(lambda: (lambda rows: rows if len(rows) >= n and all(r["state"] != "launched" for r in rows)
                          else None)(inst.rows()), "the fire to settle", 90)


def test_a_second_fire_for_a_claimed_row_does_nothing_and_there_is_one_card(tmp_path):
    """Reproduced on the real fire (levain#121 5a): while the first fire's stub sleeps, the product's own fire argv
    (the one a tick respawn uses) run as a second process exits 0 with no output, leaves the identity record and the
    row's pid as they were, and the stub is called once; the row settles done/completed with ONE agent card."""
    from levain.agents import controller as ctl

    inst = Install(tmp_path, stub=True)
    _saving(inst, sleep=6)
    _started(inst)
    _wait(lambda: inst.stub.calls() >= 1, "the first fire's stub call")
    (row,) = inst.rows()
    assert row["state"] == "launched" and row["pid"] is not None
    record = ctl.fire_record_path(inst.store_dir, row["occurrence_id"])
    before = record.read_bytes()

    second = subprocess.run(ctl.fire_argv(inst.store_dir, row["occurrence_id"]), env=inst.env(spawn_gate=False),
                            capture_output=True, text=True, timeout=60, cwd=inst.tmp, stdin=subprocess.DEVNULL)
    assert (second.returncode, second.stdout, second.stderr) == (0, "", ""), second
    assert record.read_bytes() == before                                  # a live fire's identity is never replaced
    (mid,) = inst.rows()
    assert (mid["state"], mid["pid"], mid["epoch"]) == ("launched", row["pid"], row["epoch"])
    assert inst.stub.calls() == 1

    (done,) = _settled(inst)
    assert (done["state"], done["reason"], done["pid"]) == ("done", "completed", row["pid"])
    _wait(lambda: len(_cards(inst)) >= 1, "the card")
    time.sleep(1)
    assert len(_cards(inst)) == 1 and inst.stub.calls() == 1
    assert len(inst.rows()) == 1


def test_a_card_is_not_delivered_twice_by_a_second_start_or_two_ticks(tmp_path):
    """Reproduced (levain#121 5b): after a saved run, a second ``dream start`` the same day ends skipped-no-input with
    a not-news class (no stub call, no new card), and two ticks over the day's notices change nothing: the desk
    review queue shows exactly one agent card."""
    from levain.dream.agent import NOT_NEWS

    inst = Install(tmp_path, stub=True)
    _saving(inst)
    _started(inst)
    (first,) = _settled(inst)
    assert (first["state"], first["reason"]) == ("done", "completed")
    _wait(lambda: len(_cards(inst)) >= 1, "the card")
    (card,) = _cards(inst)

    again = inst.cli("dream", "start", spawn_gate=False)
    assert again.rc == 1 and again.out == "", again
    assert "ended skipped-no-input: nothing was started." in again.err, again.err
    assert any(f"quiet/{kind}" in again.err for kind in NOT_NEWS), again.err
    rows = _settled(inst, 2)
    assert [(r["state"], r["reason"]) for r in rows] == [("done", "completed"), ("skipped-no-input", "no_input")]
    assert all(r["pid"] is None for r in rows[1:])                        # no fire was ever claimed for it
    assert inst.stub.calls() == 1

    for _ in range(2):
        tick = inst.cli("agents", "tick", spawn_gate=True)
        assert tick.rc == 0 and tick.out == "", tick
        assert all(ln.startswith("attention resolved [") for ln in tick.err.splitlines()), tick.err
    assert inst.spawned() == []
    assert [(r["state"], r["reason"]) for r in inst.rows()] == [("done", "completed"), ("skipped-no-input", "no_input")]
    assert inst.stub.calls() == 1
    assert _cards(inst) == [card]                                          # the same one card, same item
    assert [c["state"] for c in _cards(inst)] == ["open"]

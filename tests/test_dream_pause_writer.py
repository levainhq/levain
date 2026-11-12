"""1112-1 D1-P: the dream's pause is written by its failed runs, and the tick's pause notice follows from it.

The rig is ``test_dream_notices.Night`` (a real agent store with the dream registered and installed, a real desk review
store); the rows are launched occurrences ended through ``controller.settle_terminal``, the same transaction a real
fire's end uses. Nothing here sets ``operation`` or ``failure_streak`` by hand.
"""
from __future__ import annotations

import shutil
from datetime import timedelta

import pytest

from levain.agents import controller as ctl
from levain.agents import tick_driver as T
from levain.agents.accounting import Usage
from tests.test_dream_notices import AGENT, Night

pytestmark = pytest.mark.skipif(shutil.which("ssh-keygen") is None, reason="needs ssh-keygen")


@pytest.fixture
def night(tmp_path) -> Night:
    return Night(tmp_path)


def _fail(night: Night, hour: int, state="failed", reason="run_failed") -> str:
    oid = night.row(night.last_closed, "launched", hour=hour)
    with night.rig.store.write() as c:
        ended = ctl.settle_terminal(c, oid, 0, state, reason, Usage(None, None, None, "t"), night.now)
    assert ended.ended, ended
    return oid


def _agent(night: Night) -> tuple[str, int]:
    with night.rig.store.read() as c:
        return c.execute("SELECT operation, failure_streak FROM agents WHERE agent_id = ?",
                         (night.reg.agent_id,)).fetchone()


def _notes(night: Night) -> list:
    return [i for i in night.items() if i.body.get("kind") == "attention" and "paused" in i.body.get("reason", "")]


def test_one_failed_night_pauses_nothing_and_files_no_pause_notice(night):
    _fail(night, 1)
    assert _agent(night) == ("active", 1)
    assert night.tick() == T.EXIT_OK, night.err
    assert _notes(night) == []


def test_two_failed_runs_pause_the_dream_and_the_tick_files_one_pause_notice_naming_resume(night):
    first, second = _fail(night, 1), _fail(night, 3, "stopped", "crash_recovered")
    assert _agent(night) == ("paused", 2)
    for minute in range(3):                         # a second and third tick upsert the same row
        assert night.tick(later=timedelta(minutes=minute)) == T.EXIT_OK, night.err
    notes = _notes(night)
    assert len(notes) == 1, [i.body for i in night.items()]
    text = notes[0].body["reason"]
    assert "levain agents resume dream" in text and first in text and second in text, text
    assert "attention [dream-paused:" in night.err


def test_resume_lifts_the_pause_and_a_third_failure_starts_the_count_again(night):
    from levain.agents import spec as spec_mod

    _fail(night, 1)
    _fail(night, 3)
    assert _agent(night) == ("paused", 2)
    spec_mod.set_operation(night.rig.store, "dream", "active")
    assert _agent(night) == ("active", 0)
    _fail(night, 4)
    assert _agent(night) == ("active", 1)


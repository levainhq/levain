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



# ---------------------------------------------------------------------- D1-P review round 1 (board row 1688)

def _breach(night: Night, hour: int) -> str:
    """A launched row that holds an open reservation, settled past it: ``accounting.settle`` pauses the agent and the
    end it forces is ``failed`` / ``budget_breach`` (the one pause that is not a streak)."""
    oid = night.row(night.last_closed, "launched", hour=hour)
    with night.rig.store.write() as c:
        c.execute("INSERT INTO agent_reservations (occurrence_id, agent_id, window_key, reserved_usd_micros, state)"
                  " VALUES (?, ?, 'w', 1000, 'open')", (oid, night.reg.agent_id))
        ended = ctl.settle_terminal(c, oid, 0, "done", "completed", Usage(10**12, 1, 1, "t"), night.now)
    assert (ended.state, ended.reason) == ("failed", "budget_breach"), ended
    return oid


def test_a_budget_breach_pause_files_a_pause_notice_at_a_streak_of_one(night):
    oid = _breach(night, 1)
    assert _agent(night) == ("paused", 1)
    assert night.tick() == T.EXIT_OK, night.err
    notes = _notes(night)
    assert len(notes) == 1, [i.body for i in night.items()]
    text = notes[0].body["reason"]
    assert "budget breach" in text and oid in text and "levain agents resume dream" in text, text
    assert "failed runs in a row" not in text, text           # it was one run: no streak is claimed


def test_an_operator_pause_at_a_streak_of_one_files_no_pause_notice(night):
    _fail(night, 1)
    night.pause(streak=1)                                     # his own `levain agents pause dream`, after one failure
    assert night.tick() == T.EXIT_OK, night.err
    assert _notes(night) == []


def test_the_pause_notice_names_the_runs_the_writer_counted_and_not_the_rows_it_ignored(night):
    stamp = lambda minutes: (night.now + timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")  # noqa: E731
    first = _fail(night, 1)
    second = _fail(night, 4, "stopped", "crash_recovered")
    # rows that end AFTER the two failures and that the writer ignores: an authority revocation and a launch that never was
    night.row(night.last_closed, "stopped", "barred", at=stamp(1))
    night.row(night.last_closed, "stopped", "never_launched", at=stamp(2))
    assert _agent(night) == ("paused", 2)
    assert night.tick() == T.EXIT_OK, night.err
    text = _notes(night)[0].body["reason"]
    assert first in text and second in text and "barred" not in text and "never_launched" not in text, text


def test_a_spec_signing_three_pauses_at_the_third_failure_and_the_notice_says_three(tmp_path, monkeypatch):
    """The dream's own registration signs ``PAUSE_AFTER`` into ``on_failure.pause_after`` (``dream_spec`` takes no
    other value; ``register_dream`` is the real path), so the test registers the dream with the constant at 3."""
    from levain.dream import agent as da

    monkeypatch.setattr(da, "PAUSE_AFTER", 3)
    night = Night(tmp_path)
    assert '"pause_after":3' in _signed_spec(night)
    ids = [_fail(night, 1), _fail(night, 2)]
    assert _agent(night) == ("active", 2)
    assert night.tick() == T.EXIT_OK, night.err
    assert _notes(night) == []                                # two failures: still active, nothing filed
    ids.append(_fail(night, 3))
    assert _agent(night) == ("paused", 3)
    assert night.tick() == T.EXIT_OK, night.err
    text = _notes(night)[0].body["reason"]
    assert "paused after 3 failed runs in a row" in text and all(i in text for i in ids), text


def test_a_spec_signing_one_pauses_at_the_first_failure_and_the_notice_says_one(tmp_path, monkeypatch):
    from levain.dream import agent as da

    monkeypatch.setattr(da, "PAUSE_AFTER", 1)
    night = Night(tmp_path)
    oid = _fail(night, 1)
    assert _agent(night) == ("paused", 1)
    assert night.tick() == T.EXIT_OK, night.err
    text = _notes(night)[0].body["reason"]
    assert "paused after 1 failed run in a row" in text and oid in text, text


def _signed_spec(night: Night) -> str:
    with night.rig.store.read() as c:
        return c.execute("SELECT s.spec FROM agent_specs s JOIN agents a ON a.agent_id = s.agent_id"
                         " AND s.version = a.current_version WHERE a.agent_id = ?", (night.reg.agent_id,)).fetchone()[0]

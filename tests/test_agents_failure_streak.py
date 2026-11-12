"""1112-1 D1-P (board row 1688): the dream agent's ``failure_streak`` had no writer.

Every test here was run RED against the tree before the writer existed (the failing assertion is quoted in the lane's
return, ``project_memory/1112-1_dp_return.md``). The rig is the launch tests' ``Rig`` (a real agent store, a real signed
link, the real launch transaction), with ``settle_terminal`` and ``takeover`` as the ends.

The rule under test (``controller._record_run_end``): ONE writer, inside ``_end_launched`` (the one place a launched
occurrence ends), so the streak is written in the same transaction as the settle.

* ``done`` resets the streak to 0;
* ``failed`` (any reason) and ``stopped`` with ``crash_recovered`` or ``stop_at`` add 1, and the agent is paused when
  the streak reaches its spec's ``on_failure.pause_after`` (default 2);
* every other end (``stopped`` by a bar, a fence or a dead link, and every row that never launched: ``skipped-*``,
  ``missed``, a hand start ended ``never_launched``) changes nothing.
"""
from __future__ import annotations

import shutil
import sqlite3

import pytest

from levain.agents import controller as ctl
from levain.agents import slots
from levain.agents import spec as spec_mod
from levain.agents import supervise
from levain.agents.accounting import Usage
from tests.test_agents_launch import Rig

pytestmark = pytest.mark.skipif(shutil.which("ssh-keygen") is None, reason="needs ssh-keygen")

USAGE = Usage(40_000, 1, 1, "t")


@pytest.fixture
def rig(tmp_path) -> Rig:
    r = Rig(tmp_path)
    r.link()
    return r


def _agent(rig: Rig) -> tuple[str, int]:
    with rig.store.read() as c:
        return c.execute("SELECT operation, failure_streak FROM agents WHERE agent_id = ?", (rig.agent,)).fetchone()


def _launch(rig: Rig, key: str) -> tuple[str, int]:
    oid = rig.claim(key)
    got = rig.launch(oid)
    assert got.outcome == "launched", got
    return oid, got.launch_epoch


def _end(rig: Rig, key: str, state: str, reason: str) -> str:
    oid, epoch = _launch(rig, key)
    with rig.store.write() as c:
        ended = ctl.settle_terminal(c, oid, epoch, state, reason, USAGE, rig.now())
    assert (ended.ended, ended.state) == (True, state), ended
    return oid


def _lapse(rig: Rig, key: str) -> str:
    oid, _ = _launch(rig, key)
    with rig.store.read() as c:
        lease = c.execute("SELECT lease_until FROM agent_occurrences WHERE occurrence_id = ?", (oid,)).fetchone()[0]
    from datetime import datetime, timedelta, timezone
    late = datetime.strptime(lease, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc) + timedelta(seconds=1)
    with rig.store.write() as c:
        assert ctl.takeover(c, oid, late) is True
    return oid


# (a) ------------------------------------------------------------------------------------ a failed run adds one

def test_a_failed_run_adds_one_to_the_streak(rig):
    assert _agent(rig) == ("active", 0)
    _end(rig, "k1", "failed", "run_failed")
    assert _agent(rig) == ("active", 1)


def test_a_taken_over_run_counts_as_a_failed_one(rig):
    _lapse(rig, "k1")
    assert _agent(rig) == ("active", 1)


def test_a_run_stopped_at_its_signed_ceiling_counts_as_a_failed_one(rig):
    assert ctl.FAILING_STOPS == ("crash_recovered", supervise.STOP_AT_REASON)
    _end(rig, "k1", "stopped", supervise.STOP_AT_REASON)
    assert _agent(rig) == ("active", 1)


# (b) ------------------------------------------------------------------------- the second one pauses, and a paused agent claims nothing

def test_the_second_failure_in_a_row_pauses_the_agent_and_a_paused_agent_launches_nothing(rig):
    _end(rig, "k1", "failed", "run_failed")
    _end(rig, "k2", "failed", "verify_error")
    assert _agent(rig) == ("paused", 2)
    oid = rig.claim("k3")                       # a row claimed before the pause, or by a hand start, finds it paused
    got = rig.launch(oid)
    assert (got.outcome, got.reason) == ("stopped", "paused"), got


def test_a_paused_agent_walks_no_slot(rig, tmp_path):
    """The tick does not fire an agent whose operation is not ``active``: the claim path never sees it."""
    _end(rig, "k1", "failed", "run_failed")
    _end(rig, "k2", "failed", "run_failed")
    out = ctl.tick(rig.store, rig.now, binding_of=lambda a, h: "b1")
    assert rig.agent in out.paused and out.claimed == []


def test_the_pause_threshold_is_the_specs_own_and_defaults_to_two():
    """``on_failure.pause_after`` is a signed spec field (default 2); the writer reads the CURRENT version's."""
    import json

    assert ctl.pause_after_of(json.dumps({"on_failure": {"pause_after": 3}})) == 3
    assert ctl.pause_after_of(json.dumps({"on_failure": {"pause_after": 1}})) == 1
    for unreadable in (None, "", "{", json.dumps({}), json.dumps({"on_failure": {"pause_after": 0}}),
                       json.dumps({"on_failure": {"pause_after": True}}), json.dumps({"on_failure": []})):
        assert ctl.pause_after_of(unreadable) == spec_mod.DEFAULT_PAUSE_AFTER, unreadable


# (c) ------------------------------------------------------------------------------------------ done resets it

def test_a_done_run_resets_the_streak(rig):
    _end(rig, "k1", "failed", "run_failed")
    assert _agent(rig) == ("active", 1)
    _end(rig, "k2", "done", "completed")
    assert _agent(rig) == ("active", 0)
    _end(rig, "k3", "failed", "run_failed")               # not consecutive any more: one, not two
    assert _agent(rig) == ("active", 1)


# (d) ------------------------------------------------------------------- what is not a failure changes nothing

@pytest.mark.parametrize("reason", ["barred", "fenced", "link_not_live"])
def test_a_run_stopped_by_a_bar_a_fence_or_a_dead_link_changes_nothing(rig, reason):
    _end(rig, "k1", "failed", "run_failed")
    _end(rig, "k2", "stopped", reason)
    assert _agent(rig) == ("active", 1)                   # neither reset nor counted


def test_rows_that_never_launched_change_nothing(rig):
    _end(rig, "k1", "failed", "run_failed")
    with rig.store.write() as c:
        for key, state, reason in (("a", "skipped-no-input", "no_input"), ("b", "skipped-budget", "ceiling"),
                                   ("d", "missed", "no_slot"), ("e", "stopped", ctl.NEVER_LAUNCHED)):
            oid, _ = ctl.claim(c, agent_id=rig.agent, kind="manual", key=key, spec_hash=rig.spec_hash,
                               binding_id="b", window_key="m:2026-10-25", now=rig.now())
            ctl._end_admitted(c, oid, state, reason, "2026-11-02T12:00:00Z")
    assert _agent(rig) == ("active", 1)


def test_a_second_settle_of_the_same_row_writes_no_second_streak(rig):
    oid, epoch = _launch(rig, "k1")
    with rig.store.write() as c:
        assert ctl.settle_terminal(c, oid, epoch, "failed", "run_failed", USAGE, rig.now()).ended
    with rig.store.write() as c:
        assert ctl.settle_terminal(c, oid, epoch, "failed", "run_failed", USAGE, rig.now()) == ctl.Ended(False)
    assert _agent(rig) == ("active", 1)


# (e) -------------------------------------------------------- the writer and the settle are one transaction

def test_a_settle_that_raises_leaves_the_streak_and_the_row_untouched(rig, monkeypatch):
    oid, epoch = _launch(rig, "k1")

    def boom(conn, occurrence_id):
        raise RuntimeError("the slot release failed")

    monkeypatch.setattr(slots, "release", boom)
    with pytest.raises(RuntimeError, match="slot release"):
        with rig.store.write() as c:
            ctl.settle_terminal(c, oid, epoch, "failed", "run_failed", USAGE, rig.now())
    assert _agent(rig) == ("active", 0)
    assert rig.row(oid)["state"] == "launched"


def test_a_streak_write_that_raises_leaves_the_row_launched(rig, monkeypatch):
    """The other direction: the writer fails after the state UPDATE, and the whole transaction goes back."""
    oid, epoch = _launch(rig, "k1")
    monkeypatch.setattr(ctl, "_record_run_end", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("streak write")))
    with pytest.raises(RuntimeError, match="streak write"):
        with rig.store.write() as c:
            ctl.settle_terminal(c, oid, epoch, "failed", "run_failed", USAGE, rig.now())
    assert rig.row(oid)["state"] == "launched" and rig.taken(oid)[0] != []
    assert _agent(rig) == ("active", 0)


# (f) ------------------------------------------------------------------------------------- resume clears it

def test_resume_clears_the_streak_and_the_agent_launches_again(rig):
    _end(rig, "k1", "failed", "run_failed")
    _end(rig, "k2", "failed", "run_failed")
    assert _agent(rig) == ("paused", 2)
    members = spec_mod.set_operation(rig.store, rig.agent, "active")
    assert [m[1] for m in members] == [True]
    assert _agent(rig) == ("active", 0)
    oid = rig.claim("k3")
    assert rig.launch(oid).outcome == "launched"


def test_a_budget_breach_still_pauses_and_still_counts_as_a_failed_run(rig):
    """accounting.settle pauses on a breach in the same transaction; the end it forces is ``failed``/``budget_breach``."""
    oid, epoch = _launch(rig, "k1")
    with rig.store.write() as c:
        ended = ctl.settle_terminal(c, oid, epoch, "done", "completed", Usage(10**12, 1, 1, "t"), rig.now())
    assert (ended.state, ended.reason) == ("failed", "budget_breach")
    assert _agent(rig) == ("paused", 1)


# (g) ------------------------------------------------------------------ D1-P review round 1 (board row 1688)

def test_an_agent_row_that_is_missing_does_not_make_the_end_raise(rig):
    """``accounting.settle`` already tolerates an occurrence whose agent row is gone (no FK; its UPDATE finds nothing);
    the streak writer is the first writer in the terminal path that could otherwise refuse to end a row."""
    oid, epoch = _launch(rig, "k1")
    with rig.store.write() as c:
        c.execute("DELETE FROM agents WHERE agent_id = ?", (rig.agent,))
        ended = ctl.settle_terminal(c, oid, epoch, "failed", "run_failed", USAGE, rig.now())
    assert ended.ended and rig.row(oid)["state"] == "failed"


def test_the_rows_behind_the_streak_are_the_rows_the_writer_counts():
    """One predicate, two readers: the dream's pause notice names ``failing_run_rows``, not a list of its own."""
    rows = [
        {"occurrence_id": "a", "state": "failed", "reason": "run_failed", "ended_at": "2026-11-01T02:00:00Z"},
        {"occurrence_id": "b", "state": "done", "reason": "completed", "ended_at": "2026-11-02T02:00:00Z"},
        {"occurrence_id": "c", "state": "failed", "reason": "run_failed", "ended_at": "2026-11-03T02:00:00Z"},
        {"occurrence_id": "d", "state": "stopped", "reason": "barred", "ended_at": "2026-11-04T02:00:00Z"},
        {"occurrence_id": "e", "state": "stopped", "reason": "never_launched", "ended_at": "2026-11-04T03:00:00Z"},
        {"occurrence_id": "f", "state": "stopped", "reason": "crash_recovered", "ended_at": "2026-11-05T02:00:00Z"},
        {"occurrence_id": "g", "state": "launched", "reason": None, "ended_at": None, "admitted_at": "2026-11-06T02:00:00Z"},
    ]
    assert [r["occurrence_id"] for r in ctl.failing_run_rows(rows)] == ["c", "f"]
    assert ctl.failing_run_rows([]) == []

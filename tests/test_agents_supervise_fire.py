"""P6-4a: the run supervisor, fire side (A0 r8 §2.3; BUILD_PLAN P6-4a).

Every store is a real ``/2`` agent store with a real launched occurrence and a real authority log; the executors are
stubs that return a ``StopResult`` and signal nothing.
"""
from __future__ import annotations

import ast
import copy
import shutil
from pathlib import Path

import pytest

from levain.agent_authority import AgentCoverMoved
from levain.agents import supervise
from levain.agents.accounting import StopResult, Usage
from levain.autonomic.journal import EffectStatus, RunJournal

from levain.agents import controller as ctl
from levain.agents.role import role_hash
from levain.agents.spec import parse_spec, register_spec
from tests.test_agents_launch import BINDING, ROLES, SPECS, WINDOW, Exec, Rig  # noqa: E402
from tests.test_authority_agent_link import link as link_record  # noqa: E402
from tests.test_authority_agent_link import revoke_agent  # noqa: E402

pytestmark = pytest.mark.skipif(shutil.which("ssh-keygen") is None, reason="needs ssh-keygen")

LEVAIN = Path(__file__).resolve().parent.parent / "levain"


class Flaky:
    """The authority with the first ``fail`` peeks refused (a cover that cannot be read), counting its refreshes."""

    def __init__(self, inner, fail=0):
        self.inner, self.fail, self.refreshes = inner, fail, 0

    def refresh(self):
        self.refreshes += 1
        return self.inner.refresh()

    def peek(self, *args, **kw):
        if self.fail > 0:
            self.fail -= 1
            raise AgentCoverMoved("injected")
        return self.inner.peek(*args, **kw)


class Ex:
    max_exposure_usd_micros = 100_000
    action_exposure = None

    def __init__(self, terminated=True):
        self.terminated, self.stops = terminated, 0

    def usage(self):
        return Usage(5_000, None, None, "test")

    def stop(self, deadline_s):
        self.stops += 1
        return StopResult(self.terminated, "test")


def _fire(rig: Rig, key: str, journal: RunJournal) -> str:
    """A launched occurrence with its journal run started."""
    oid = rig.claim(key)
    assert rig.launch(oid).outcome == "launched"
    journal.start(rig.row(oid)["run_id"], binding_id=BINDING)
    return oid


def _second_fire(rig: Rig, journal: RunJournal) -> str:
    """A launched occurrence of a second agent (one agent has one live occurrence at a time), in the same store."""
    d = copy.deepcopy(SPECS["job_sync_check"])
    d["role"]["role_hash"] = role_hash(ROLES[d["role"]["name"]])
    agent, spec_hash, *_ = register_spec(rig.store, parse_spec(d), "America/New_York")
    rig.n += 1
    rig.world.link({"op": "create_grant", "grant": link_record(rig.world, rig.n, agent=agent, store=rig.sid,
                                                              spec_hash=spec_hash, binding_id=BINDING)}, "laptop")
    with rig.store.write() as c:
        oid, outcome = ctl.claim(c, agent_id=agent, kind="manual", key="b", spec_hash=spec_hash, binding_id=BINDING,
                                 window_key="m:2026-10-25", now=rig.now())
    assert outcome == ctl.CLAIMED
    assert ctl.launch(rig.store, oid, authority=rig.authority, executor=Exec(), window=WINDOW,
                      now=rig.now()).outcome == "launched"
    journal.start(rig.row(oid)["run_id"], binding_id=BINDING)
    return oid


@pytest.fixture
def rig(tmp_path) -> Rig:
    r = Rig(tmp_path)
    r.link()
    return r


def _supervisor(rig, authority=None, **kw):
    journal = RunJournal(rig.store.directory)
    return supervise.Supervisor(rig.store, authority=authority or rig.authority, clock=rig.now, journal=journal,
                                **kw), journal


# --- the trust boundary ---------------------------------------------------------------------------------------------

def test_only_the_supervisor_calls_the_agent_journal_effect_path():
    # behaviour: the agent fence_now reaches RunJournal.effect from supervise.py alone; a module that builds an agent
    # fence calls no .effect(, and a module outside supervise.py calls .effect( with no agent_fence in its source
    def calls_effect(tree):
        return [n for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                and n.func.attr == "effect"]

    def builds_fence(tree):
        return any((isinstance(n, ast.Name) and n.id == "agent_fence")
                   or (isinstance(n, ast.Attribute) and n.attr == "agent_fence")
                   or (isinstance(n, ast.alias) and n.name == "agent_fence") for n in ast.walk(tree))

    builders, effectors = set(), set()
    for path in LEVAIN.rglob("*.py"):
        tree = ast.parse(path.read_text(), filename=str(path))
        if builds_fence(tree):
            builders.add(path.name)
        if calls_effect(tree):
            effectors.add(path.name)
    assert builders - {"agent_authority.py"} == {"supervise.py"}   # nobody else even names the fence builder
    assert "supervise.py" in effectors
    calls = calls_effect(ast.parse((LEVAIN / "agents" / "supervise.py").read_text()))
    assert len(calls) == 1
    keywords = {k.arg: k.value for k in calls[0].keywords}
    assert isinstance(keywords["needs_decision"], ast.Constant) and keywords["needs_decision"].value is False
    assert {"fence", "fence_now"} <= keywords.keys()


# --- the UNCLASSIFIED retry -----------------------------------------------------------------------------------------

def test_an_unreadable_cover_is_tried_three_times_with_one_refresh_per_tick_for_every_occurrence(rig):
    # behaviour: a cover that fails twice then reads runs the effect on the third try after one refresh; a cover that
    # never reads stops at three tries and leaves the row launched; two occurrences in one tick share one refresh, and
    # the next tick may refresh again; a moved head alone stops nothing
    flaky = Flaky(rig.authority)
    sup, journal = _supervisor(rig, flaky)
    first = _fire(rig, "a", journal)
    second = _second_fire(rig, journal)
    ran = []

    sup.begin_tick()
    flaky.fail = 2
    done = sup.effect(first, "e1", digest="d1", fn=lambda: ran.append("e1") or "ok", executor=Ex())
    assert (done.outcome.status, done.tries, done.waits) == (EffectStatus.DONE, 3, False)
    assert flaky.refreshes == 1 and ran == ["e1"]

    flaky.fail = 10      # the same tick, another occurrence: the refresh is spent, so only the tries are
    waits = sup.effect(second, "e1", digest="d1", fn=lambda: ran.append("x") or "ok", executor=Ex())
    assert (waits.outcome.status, waits.tries, waits.waits) == (EffectStatus.UNCLASSIFIED, 3, True)
    assert flaky.refreshes == 1 and ran == ["e1"]
    assert rig.row(second)["state"] == "launched" and rig.taken(second) == ([(2,)], [("open",)])

    sup.begin_tick()
    flaky.fail = 1
    again = sup.effect(second, "e1", digest="d1", fn=lambda: ran.append("e1b") or "ok", executor=Ex())
    assert again.outcome.status is EffectStatus.DONE and flaky.refreshes == 2 and ran == ["e1", "e1b"]

    rig.n += 1       # the head moves with a link for another agent; nothing of this one's is revoked
    rig.world.link({"op": "create_grant", "grant": link_record(rig.world, rig.n, agent="ag-" + "0" * 32, store=rig.sid,
                                                              spec_hash=rig.spec_hash, binding_id=BINDING)}, "laptop")
    sup.begin_tick()
    moved = sup.effect(first, "e2", digest="d2", fn=lambda: ran.append("e2") or "ok", executor=Ex())
    assert moved.outcome.status is EffectStatus.DONE and rig.row(first)["state"] == "launched"


# --- the STALE end, by the stop rule --------------------------------------------------------------------------------

def test_a_stale_ends_the_occurrence_stopped_only_after_a_confirmed_stop_and_never_adopts_a_new_fence(rig):
    # behaviour: a revoke makes the next effect STALE (its fn never runs); an unconfirmed stop, a truthy non-True
    # answer or a raising stop() leaves the row launched with its slot and reservation; terminated=True writes
    # stopped, frees the slot and settles in one transaction; every later effect is STALE again, the fence is never
    # re-read
    sup, journal = _supervisor(rig)
    oid = _fire(rig, "a", journal)
    ran = []
    sup.begin_tick()
    ok = sup.effect(oid, "e1", digest="d1", fn=lambda: ran.append("e1") or "ok", executor=Ex())
    assert ok.outcome.status is EffectStatus.DONE

    rig.world.link(revoke_agent(rig.agent, rig.sid), "laptop")
    class Truthy(Ex):
        def stop(self, deadline_s):
            self.stops += 1
            return StopResult(1, "not a bool")    # type: ignore[arg-type]

    class Raises(Ex):
        def stop(self, deadline_s):
            raise RuntimeError("no")

    for executor in (Ex(False), Truthy(), Raises()):
        sup.begin_tick()
        held = sup.effect(oid, "e2", digest="d2", fn=lambda: ran.append("e2") or "ok", executor=executor)
        assert held.stale and held.ended is None and held.stop.terminated is not True
        assert rig.row(oid)["state"] == "launched" and rig.taken(oid) == ([(1,)], [("open",)])

    confirmed = Ex(True)
    ended = sup.effect(oid, "e3", digest="d3", fn=lambda: ran.append("e3") or "ok", executor=confirmed)
    row = rig.row(oid)
    assert ended.stale and confirmed.stops == 1 and ended.ended.ended
    assert (row["state"], row["reason"]) == ("stopped", "link_not_live") and row["ended_at"]
    assert rig.taken(oid) == ([], [("settled",)])
    assert ran == ["e1"]

    after = Ex(True)
    gone = sup.effect(oid, "e4", digest="d4", fn=lambda: ran.append("e4") or "ok", executor=after)
    assert gone.stale and after.stops == 0 and gone.ended is None and ran == ["e1"]   # already ended: no second stop


# --- the hook points ------------------------------------------------------------------------------------------------

def test_verify_may_fail_a_done_and_on_end_runs_after_the_commit_whatever_it_raises(rig):
    # behaviour: verify runs before the end is written and a reason turns done into failed; on_end sees the committed
    # end once, and an on_end that raises undoes nothing; a second finish writes nothing and calls no on_end
    seen = []
    hooks = supervise.Hooks()
    sup, journal = _supervisor(rig, hooks=hooks)
    oid = _fire(rig, "a", journal)

    def verify(end):
        with rig.store.read() as c:      # nothing is written yet
            assert c.execute("SELECT state FROM agent_occurrences WHERE occurrence_id = ?",
                             (end.occurrence_id,)).fetchone()[0] == "launched"
        return "no_win"

    def on_end(end):
        with rig.store.read() as c:      # the end is committed by now
            seen.append((end.state, end.reason, c.execute("SELECT state FROM agent_occurrences WHERE occurrence_id = ?",
                                                          (end.occurrence_id,)).fetchone()[0]))
        raise RuntimeError("a gauge failed")

    hooks.verify, hooks.on_end = verify, on_end
    end = sup.finish(oid, Ex())
    row = rig.row(oid)
    assert (end.state, end.reason) == ("failed", "no_win") and (row["state"], row["reason"]) == ("failed", "no_win")
    assert seen == [("failed", "no_win", "failed")] and rig.taken(oid) == ([], [("settled",)])
    sup.finish(oid, Ex())
    assert len(seen) == 1

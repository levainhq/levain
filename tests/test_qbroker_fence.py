"""P7-2: the stale-session fence at the queue broker's GATE (A0 section 2.3; P7-0 section 2). Every authority here is
real (ssh-keygen links through the replay, the real agent tables); the fence step runs before the run's count."""
from __future__ import annotations

import shutil
import sqlite3
from datetime import timedelta

import pytest

import levain.authority as auth
from levain.agent_authority import AgentAuthority
from levain.autonomic.agent_tables import create_agent_objects
from levain.consent import broker_client
from levain.consent.broker_client import BrokerError
from levain.qbroker import broker as qbroker, operator
from levain.qbroker.broker import Resolved
from levain.qbroker.fence import AgentFences
import tests.test_qbroker as base
from tests.test_qbroker import RUN, make_broker
from tests.test_qbroker_gate import gate
from tests.test_qbroker_limits import BIG
from tests.test_consent_broker_client import Scripted

pytest.importorskip("cryptography")
from tests.test_authority_agent_link import AGENT, STORE, World, create, revoke_agent  # noqa: E402

pytestmark = pytest.mark.skipif(shutil.which("ssh-keygen") is None, reason="needs ssh-keygen")

L, clock, keys, resolver = base.L, base.clock, base.keys, base.resolver
LINK = "al-000000000001"


@pytest.fixture
def world(tmp_path) -> World:
    return World(tmp_path)


class Rig:
    def __init__(self, world, tmp_path, L, resolver, clock):
        self.world, self.L = world, L
        create(world, 1)
        self.db = tmp_path / "autonomic.db"
        self.conn = sqlite3.connect(self.db, isolation_level=None)
        create_agent_objects(self.conn)
        inc = auth.agent_link_incarnation(world.state(), LINK)
        self.conn.execute(
            "INSERT INTO agent_occurrences (occurrence_id, agent_id, kind, key, state, epoch, window_key, spec_hash,"
            " binding_id, run_id, admitted_at) VALUES ('occ-1', ?, 'manual', 'k', 'admitted', 0, 'w', ?, ?, ?, 't')",
            (AGENT, "b" * 64, "bind-0123456789abcdef", RUN["run_id"]))
        self.conn.execute("UPDATE agent_occurrences SET state = 'launched', launch_epoch = 0, link_id = ?,"
                          " link_incarnation = ?, launched_at = 't' WHERE occurrence_id = 'occ-1'", (LINK, inc))
        self.authority = AgentAuthority(world.root)
        self.authority.refresh()
        self.now = {"t": world.clock.now}
        self.fences = AgentFences(self.db, self.authority, STORE, clock=lambda: self.now["t"],
                                  refresh_interval=0.0)
        resolver.add(2, "cap-two", Resolved(session="sess-2", kind="dispatch",
                                            run=dict(RUN) | {"max_calls": 50}))
        self.resolver, self.clock = resolver, clock

    def broker(self, fence="own", **kw):
        return make_broker(self.L, self.resolver, self.clock, limits=lambda a, s: dict(BIG),
                           fence=self.fences if fence == "own" else fence, **kw)

    def calls(self):
        return operator.run_state(self.L / "queue", RUN["run_id"])


@pytest.fixture
def rig(world, tmp_path, L, resolver, clock):
    return Rig(world, tmp_path, L, resolver, clock)


def body(got):
    return {k: v for k, v in got.items() if k not in ("id", "ok")}


def go2(b, n=1):
    return [gate(b, slot=2, cap="cap-two", input_digest=f"sha256:{i:064x}") for i in range(n)]


def test_the_gap_a_broker_with_no_fence_answers_go_and_counts_a_revoked_session(rig):
    b = rig.broker(fence=None)
    assert go2(b)[0]["gate"] == "go"
    rig.world.link(revoke_agent())
    rig.authority.refresh()
    got = go2(b)[0]
    print("NO FENCE, REVOKED:", got, rig.calls())
    assert got["gate"] == "go" and rig.calls()["calls"] == 2


def test_a_current_session_is_go_and_counted(rig):
    got = go2(rig.broker(), 2)
    assert [g["gate"] for g in got] == ["go", "go"] and rig.calls()["calls"] == 2


def test_a_revoked_session_is_fenced_and_counts_nothing(rig):
    b = rig.broker()
    assert go2(b)[0]["gate"] == "go"
    rig.world.link(revoke_agent())          # the replay is NOT refreshed here: the broker's own cover-moved refresh
    got = go2(b, 3)
    print("FENCED:", got, rig.calls())
    assert [g["gate"] for g in got] == ["fenced"] * 3
    assert "no longer live" in got[0]["reason"]
    assert rig.calls()["calls"] == 1                              # only the call before the revoke


def test_expiry_epoch_state_and_bar_each_fence_with_their_own_reason(rig):
    b = rig.broker()
    assert go2(b)[0]["gate"] == "go"
    rig.now["t"] = rig.world.clock.now + timedelta(days=40)
    assert "no longer live" in go2(b)[0]["reason"]
    rig.now["t"] = rig.world.clock.now
    assert go2(b)[0]["gate"] == "go"
    rig.conn.execute("INSERT INTO agent_bars (bar_id, agent_id, barred_at, head_seq_at_bar) VALUES ('b1', ?, 't', 1)",
                     (AGENT,))
    assert body(go2(b)[0]) == {"gate": "fenced", "reason": "the agent is barred"}
    rig.conn.execute("UPDATE agent_bars SET closed_at = 't', revoke_seq = 1 WHERE bar_id = 'b1'")
    rig.conn.execute("UPDATE agent_occurrences SET epoch = 1 WHERE occurrence_id = 'occ-1'")
    assert body(go2(b)[0]) == {"gate": "fenced", "reason": "the occurrence's epoch moved"}
    rig.conn.execute("UPDATE agent_occurrences SET state = 'stopped', ended_at = 't', reason = 'x'"
                     " WHERE occurrence_id = 'occ-1'")
    assert body(go2(b)[0]) == {"gate": "fenced", "reason": "the occurrence left launched (now stopped)"}
    assert rig.calls()["calls"] == 2


def test_an_unreadable_fence_is_never_go(rig, tmp_path):
    b = rig.broker(fence=AgentFences(tmp_path / "missing.db", rig.authority, STORE))
    got = go2(b)[0]
    assert got["gate"] == "limits-failed" and "could not be read" in got["reason"]
    assert rig.calls()["known"] is False                        # nothing was counted


@pytest.mark.parametrize("answer", ["", 3, ["x"]])
def test_a_fence_that_answers_garbage_is_limits_failed(rig, answer):
    got = go2(rig.broker(fence=lambda who: answer))[0]
    assert (got["gate"], got["reason"]) == ("limits-failed", qbroker.FENCE_ANSWER_MALFORMED)


def test_a_fence_that_raises_is_limits_failed(rig):
    def boom(who):
        raise RuntimeError("x")
    got = go2(rig.broker(fence=boom))[0]
    assert got["gate"] == "limits-failed" and "RuntimeError" in got["reason"] and rig.calls()["known"] is False


def test_a_session_with_no_run_or_no_occurrence_is_not_fenced(rig):
    b = rig.broker()
    assert gate(b, slot=1, cap="cap-a")["gate"] == "go"          # interactive: run is None
    seen = []
    assert rig.broker(fence=lambda who: seen.append(who)) and gate(
        rig.broker(fence=lambda who: seen.append(who)), slot=1, cap="cap-a")["gate"] == "go" and seen == []
    rig.resolver.add(2, "cap-two", Resolved(session="sess-2", kind="dispatch", run=dict(RUN) | {"run_id": "run-other"}))
    assert go2(rig.broker())[0]["gate"] == "go"                              # no agent occurrence carries this run id


def test_the_fence_runs_before_the_run_cap_and_a_fenced_call_does_not_latch(rig):
    resolver_run = dict(RUN) | {"max_calls": 1}
    rig.resolver.add(2, "cap-two", Resolved(session="sess-2", kind="dispatch", run=resolver_run))
    b = rig.broker()
    rig.world.link(revoke_agent())
    assert [g["gate"] for g in go2(b, 3)] == ["fenced"] * 3      # over the cap of 1 only if it counted
    assert rig.calls()["known"] is False


# --- the client's words -------------------------------------------------------------------------------------------

KW = {"agent": "claude_code", "tool_name": "Bash", "tool_input": {}, "tool_use_id": None}


def test_the_client_names_fenced_and_the_run_kinds():
    assert Scripted([{"gate": "fenced", "reason": "the agent is barred"}]).gate(**KW) == ("fenced", "the agent is barred")
    text = broker_client.gate_check(Scripted([{"gate": "fenced", "reason": "the agent is barred"}]), **KW)
    assert text == broker_client.SLOT_FENCED_TEXT.format(why="the agent is barred") and "barred" in text
    calls = broker_client.gate_check(Scripted([{"gate": "over-limit", "kind": "run-calls"}]), **KW)
    deadline = broker_client.gate_check(Scripted([{"gate": "over-limit", "kind": "run-deadline"}]), **KW)
    assert calls == broker_client.SLOT_RUN_CALLS_TEXT and deadline == broker_client.SLOT_RUN_DEADLINE_TEXT
    assert "a configured limit" not in calls + deadline and calls != deadline


@pytest.mark.parametrize("answer", [{"gate": "fenced"}, {"gate": "fenced", "reason": 3},
                                    {"gate": "fenced", "reason": "r", "x": 1}, {"gate": "fenced", "request_id": "r"}])
def test_a_fenced_answer_out_of_shape_raises(answer):
    with pytest.raises(BrokerError):
        Scripted([answer]).gate(**KW)

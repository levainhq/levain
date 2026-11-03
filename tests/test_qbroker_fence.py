"""P7-2: the stale-session fence at the queue broker's GATE (A0 section 2.3; P7-0 section 2). Every authority here is
real (ssh-keygen links through the replay, the real agent tables, the occurrence claimed by the real controller); the
fence step runs before the run's count. The session is joined to its occurrence by ``session_key`` (P7-0 sections 2.1,
3.1, 5): the ticket's run id is CONTROL's ``r-`` id, the occurrence's is ``run-``, and the two never join."""
from __future__ import annotations

import argparse
import os
import shutil
import sqlite3
import tempfile
import threading
import time
from datetime import timedelta
from pathlib import Path

import pytest

import levain.authority as auth
from levain.agent_authority import AgentAuthority, AgentCoverMoved
from levain.agents import controller
from levain.autonomic.agent_tables import create_agent_objects
from levain.consent import broker_client
from levain.consent.broker_client import BrokerError
from levain.qbroker import __main__ as qmain, broker as qbroker, fence as qfence, fence_server, operator
from levain.qbroker.broker import Resolved
from levain.qbroker.fence import AgentFences, RemoteFences, Unfenceable
from levain.slot import tickets
import tests.test_qbroker as base
from tests.test_qbroker import RUN, make_broker
from tests.test_qbroker_gate import gate
from tests.test_qbroker_limits import BIG, ME
from tests.test_consent_broker_client import Scripted

pytest.importorskip("cryptography")
from tests.test_authority_agent_link import AGENT, OTHER_AGENT, STORE, World, create, revoke_agent  # noqa: E402

pytestmark = pytest.mark.skipif(shutil.which("ssh-keygen") is None, reason="needs ssh-keygen")

L, clock, keys, resolver = base.L, base.clock, base.keys, base.resolver
LINK = "al-000000000001"
SESSION = "s-0123456789abcdef"          # an S1 session id (levain.slot.grammar.SESSION_RE)


@pytest.fixture
def world(tmp_path) -> World:
    return World(tmp_path)


def agent_store(directory, world):
    """An autonomic store's agent tables and meta row, with one occurrence claimed by the real controller and
    launched under link ``LINK`` with ``session_key`` = :data:`SESSION`, as the fire records it."""
    directory.mkdir()
    conn = sqlite3.connect(directory / "autonomic.db", isolation_level=None)
    conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    conn.execute("INSERT INTO meta (key, value) VALUES ('store_id', ?)", (STORE,))
    create_agent_objects(conn)
    conn.execute("BEGIN IMMEDIATE")
    oid, outcome = controller.claim(conn, agent_id=AGENT, kind="manual", key="op-1", spec_hash="b" * 64,
                                    binding_id="bind-0123456789abcdef", window_key="m:2026-01-01",
                                    now=world.clock.now)
    assert outcome == controller.CLAIMED
    inc = auth.agent_link_incarnation(world.state(), LINK)
    conn.execute("UPDATE agent_occurrences SET state = 'launched', launch_epoch = 0, link_id = ?, link_incarnation = ?,"
                 " launched_at = 't', session_key = ? WHERE occurrence_id = ?", (LINK, inc, SESSION, oid))
    conn.execute("COMMIT")
    return conn, oid


class Rig:
    def __init__(self, world, tmp_path, L, resolver, clock):
        self.world, self.L = world, L
        create(world, 1)
        self.conn, self.oid = agent_store(tmp_path / "autonomic", world)
        self.db = tmp_path / "autonomic" / "autonomic.db"
        self.authority = AgentAuthority(world.root)
        self.now = {"t": world.clock.now}
        self.fences = AgentFences(self.db, self.authority, clock=lambda: self.now["t"], refresh_interval=0.0)
        # the ticket's run id is CONTROL's, minted with the ticket: never the occurrence's
        self.run = dict(RUN) | {"run_id": tickets.new_run_id(), "max_calls": 50}
        resolver.add(2, "cap-two", Resolved(session=SESSION, kind="dispatch", run=self.run))
        self.resolver, self.clock = resolver, clock

    def broker(self, fence="own", **kw):
        return make_broker(self.L, self.resolver, self.clock, limits=lambda a, s: dict(BIG),
                           fence=self.fences if fence == "own" else fence, **kw)

    def calls(self):
        return operator.run_state(self.L / "queue", self.run["run_id"])


@pytest.fixture
def rig(world, tmp_path, L, resolver, clock):
    return Rig(world, tmp_path, L, resolver, clock)


def body(got):
    return {k: v for k, v in got.items() if k not in ("id", "ok")}


def go2(b, n=1):
    return [gate(b, slot=2, cap="cap-two", input_digest=f"sha256:{i:064x}") for i in range(n)]


def test_the_two_run_id_namespaces_never_join(rig):
    occurrence_run = rig.conn.execute("SELECT run_id FROM agent_occurrences WHERE occurrence_id = ?",
                                      (rig.oid,)).fetchone()[0]
    assert occurrence_run == controller.run_id(AGENT, "manual", "op-1") and occurrence_run.startswith("run-")
    assert tickets.RUN_ID_RE.fullmatch(rig.run["run_id"]) and rig.run["run_id"] != occurrence_run


def test_a_revoked_real_occurrence_under_a_control_run_id_is_fenced(rig):
    # L3 r1 codex HIGH, reproduced on 3183f466: the run-id join found no row and a revoked agent's session got go
    b = rig.broker()
    assert go2(b)[0]["gate"] == "go"
    rig.world.link(revoke_agent())          # the replay is NOT refreshed here: the broker's own cover-moved refresh
    got = go2(b, 3)
    print("FENCED:", got, rig.calls())
    assert [g["gate"] for g in got] == ["fenced"] * 3
    assert "no longer live" in got[0]["reason"]
    assert rig.calls()["calls"] == 1                              # only the call before the revoke


def test_the_gap_a_broker_with_no_fence_answers_go_and_counts_a_revoked_session(rig):
    b = rig.broker(fence=None)
    assert go2(b)[0]["gate"] == "go"
    rig.world.link(revoke_agent())
    got = go2(b)[0]
    assert got["gate"] == "go" and rig.calls()["calls"] == 2


def test_a_current_session_is_go_and_counted(rig):
    got = go2(rig.broker(), 2)
    assert [g["gate"] for g in got] == ["go", "go"] and rig.calls()["calls"] == 2


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
    rig.conn.execute("UPDATE agent_occurrences SET epoch = 1 WHERE occurrence_id = ?", (rig.oid,))
    assert body(go2(b)[0]) == {"gate": "fenced", "reason": "the occurrence's epoch moved"}
    rig.conn.execute("UPDATE agent_occurrences SET state = 'stopped', ended_at = 't', reason = 'x'"
                     " WHERE occurrence_id = ?", (rig.oid,))
    assert body(go2(b)[0]) == {"gate": "fenced", "reason": "the occurrence left launched (now stopped)"}
    assert rig.calls()["calls"] == 2


def test_two_occurrences_naming_one_session_are_never_go(rig):
    # session_key is not UNIQUE in agent_tables.py: a second row naming the session refuses, whichever row is current
    rig.conn.execute("BEGIN IMMEDIATE")
    other, _ = controller.claim(rig.conn, agent_id=OTHER_AGENT, kind="manual", key="op-2", spec_hash="b" * 64,
                                binding_id="bind-0123456789abcdef", window_key="m:2026-01-01", now=rig.world.clock.now)
    rig.conn.execute("UPDATE agent_occurrences SET session_key = ? WHERE occurrence_id = ?", (SESSION, other))
    rig.conn.execute("COMMIT")
    got = go2(rig.broker())[0]
    assert got["gate"] == "limits-failed" and "more than one agent occurrence" in got["reason"]
    assert rig.calls()["known"] is False


def test_an_unreadable_fence_is_never_go(rig, tmp_path):
    b = rig.broker(fence=AgentFences(tmp_path / "missing.db", rig.authority, STORE))
    got = go2(b)[0]
    assert got["gate"] == "limits-failed" and "could not be read" in got["reason"]
    assert rig.calls()["known"] is False                        # nothing was counted


def test_a_store_with_no_store_id_is_never_go(rig):
    rig.conn.execute("DELETE FROM meta")
    got = go2(rig.broker())[0]
    assert got["gate"] == "limits-failed" and "no well-formed store id" in got["reason"]


@pytest.mark.parametrize("answer", ["", 3, ["x"]])
def test_a_fence_that_answers_garbage_is_limits_failed(rig, answer):
    got = go2(rig.broker(fence=lambda who: answer))[0]
    assert (got["gate"], got["reason"]) == ("limits-failed", qbroker.FENCE_ANSWER_MALFORMED)


def test_a_fence_that_raises_is_limits_failed(rig):
    def boom(who):
        raise RuntimeError("x")
    got = go2(rig.broker(fence=boom))[0]
    assert got["gate"] == "limits-failed" and "RuntimeError" in got["reason"] and rig.calls()["known"] is False


def test_a_session_no_occurrence_names_is_current(rig):
    # the open contract: a dispatch session whose session_key no occurrence carries is no agent's (P7-1 records it)
    rig.resolver.add(2, "cap-two", Resolved(session="s-fedcba9876543210", kind="dispatch", run=rig.run))
    assert go2(rig.broker())[0]["gate"] == "go"
    b = rig.broker()
    assert gate(b, slot=1, cap="cap-a")["gate"] == "go"          # interactive: run is None, never fenced


def test_the_reason_of_a_row_that_is_gone_names_it(rig):
    assert AgentFences._reason(rig.conn, {"occurrence_id": "oc-gone", "launch_epoch": 0, "agent_id": AGENT}) \
        == "the occurrence is no longer in the agent store"


def test_the_fence_runs_before_the_run_cap_and_a_fenced_call_does_not_latch(rig):
    rig.resolver.add(2, "cap-two", Resolved(session=SESSION, kind="dispatch", run=rig.run | {"max_calls": 1}))
    b = rig.broker()
    rig.world.link(revoke_agent())
    assert [g["gate"] for g in go2(b, 3)] == ["fenced"] * 3      # over the cap of 1 only if it counted
    assert rig.calls()["known"] is False


# --- the refresh: its timestamp and its lock ----------------------------------------------------------------------

class SlowAuthority:
    """A stand-in replay whose refresh clears its snapshot while it replays, as AgentAuthority.refresh does."""

    def __init__(self, tick=None, pause=0.0):
        self.state, self.refreshes, self.tick, self.pause, self.fail = None, 0, tick, pause, False

    def refresh(self):
        self.state = None
        self.refreshes += 1
        if self.tick is not None:
            self.tick()
        time.sleep(self.pause)
        if self.fail:
            raise OSError("the cockpit cannot be read")
        self.state = "held"

    def peek(self):
        if self.state is None:
            raise AgentCoverMoved("no verified replay is held")


def stub_fence(monkeypatch, authority):
    def agent_fence(occurrence, *, authority=authority, store_id, now):
        def fence_now(conn):
            time.sleep(0.002)                     # a peek takes time: a refresh elsewhere can start meanwhile
            authority.peek()
            return "f"
        return "f", fence_now
    monkeypatch.setattr(qfence, "agent_fence", agent_fence)


def test_a_cover_that_moves_right_after_a_refresh_is_read_again(rig, monkeypatch):
    # L3 r1 complement #2: a time throttle on success answered limits-failed for a revoke inside its window
    t = {"now": 0.0}
    authority = SlowAuthority()
    stub_fence(monkeypatch, authority)
    fences = AgentFences(rig.db, authority, STORE, refresh_interval=1.0, monotonic=lambda: t["now"])
    who = Resolved(session=SESSION, kind="dispatch", run=rig.run)
    assert fences(who) is None and authority.refreshes == 1
    t["now"] += 0.1
    authority.state = None                                        # the cover moves again 0.1 s after the refresh
    assert fences(who) is None and authority.refreshes == 2


def test_after_a_failed_refresh_the_interval_runs_from_its_end(rig, monkeypatch):
    t = {"now": 0.0}
    authority = SlowAuthority(tick=lambda: t.__setitem__("now", t["now"] + 5.0))   # a refresh that takes 5 s
    authority.fail = True
    stub_fence(monkeypatch, authority)
    fences = AgentFences(rig.db, authority, STORE, refresh_interval=1.0, monotonic=lambda: t["now"])
    who = Resolved(session=SESSION, kind="dispatch", run=rig.run)
    with pytest.raises(OSError):
        fences(who)
    t["now"] += 0.5                                               # 0.5 s after the failed refresh ended
    with pytest.raises(AgentCoverMoved):
        fences(who)
    assert authority.refreshes == 1                               # inside the interval: no second replay
    t["now"] += 1.0
    authority.fail = False
    assert fences(who) is None and authority.refreshes == 2


def test_agent_fence_raising_cover_moved_takes_the_refresh_path(rig, monkeypatch):
    # L3 r2 deepseek: agent_fence itself raised AgentCoverMoved (a refresh elsewhere cleared the replay) outside the
    # guarded region, so no refresh ran and GATE answered limits-failed
    authority = SlowAuthority()                                   # nothing held: a refresh elsewhere cleared it

    def agent_fence(occurrence, *, authority=authority, store_id, now):
        authority.peek()
        return "f", lambda conn: "f"
    monkeypatch.setattr(qfence, "agent_fence", agent_fence)
    fences = AgentFences(rig.db, authority, STORE, refresh_interval=1.0)
    assert fences(Resolved(session=SESSION, kind="dispatch", run=rig.run)) is None
    assert authority.refreshes == 1


def test_a_cover_that_moves_again_after_another_callers_refresh_is_refreshed(rig, monkeypatch):
    # L3 r2 codex: the caller's peek failed, another caller refreshed, then the cover moved again before this caller's
    # retry; _refresh(seen) skipped on the generation and the retry answered limits-failed
    authority = SlowAuthority()
    authority.state = "held"
    fences = AgentFences(rig.db, authority, STORE, refresh_interval=0.0)
    calls = {"n": 0}

    def fence_now(conn):
        calls["n"] += 1
        if calls["n"] == 1:
            authority.state = None                                # the cover moves under this caller's peek
            with fences._lock:                                    # another caller refreshes meanwhile
                fences._refresh(fences._generation)
            authority.state = None                                # and the cover moves again before the retry
        authority.peek()
        return "f"
    monkeypatch.setattr(qfence, "agent_fence", lambda occurrence, *, authority, store_id, now: ("f", fence_now))
    assert fences(Resolved(session=SESSION, kind="dispatch", run=rig.run)) is None
    assert authority.refreshes == 2


def test_a_cover_that_never_settles_fails_closed_after_a_bounded_retry(rig, monkeypatch):
    authority = SlowAuthority()

    def fence_now(conn):
        authority.state = None                                    # every peek finds the cover moved
        authority.peek()
    monkeypatch.setattr(qfence, "agent_fence", lambda occurrence, *, authority, store_id, now: ("f", fence_now))
    fences = AgentFences(rig.db, authority, STORE, refresh_interval=0.0)
    with pytest.raises(AgentCoverMoved):
        fences(Resolved(session=SESSION, kind="dispatch", run=rig.run))
    assert 1 <= authority.refreshes <= qfence._COVER_RETRIES


def test_a_check_whose_transaction_already_ended_raises_its_own_error(rig):
    # L3 r2 complement LOW: an unconditional ROLLBACK in the finally raised "no transaction is active" over the
    # check's own error when SQLite had already rolled the transaction back (or BEGIN never opened one)
    conn = sqlite3.connect(rig.db, isolation_level=None)

    def fence_now(c):
        c.execute("ROLLBACK")                                     # as SQLite's own rollback on an I/O or full error
        raise AgentCoverMoved("the cover moved")
    with pytest.raises(AgentCoverMoved, match="the cover moved"):
        rig.fences._check(conn, {}, "f", fence_now)
    conn.close()


def test_a_retry_never_runs_beside_a_refresh(rig, monkeypatch):
    authority = SlowAuthority(pause=0.005)
    stub_fence(monkeypatch, authority)
    fences = AgentFences(rig.db, authority, STORE, refresh_interval=0.0)
    who = Resolved(session=SESSION, kind="dispatch", run=rig.run)
    errors: list[BaseException] = []

    def run():
        for _ in range(15):
            try:
                fences(who)
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)
    threads = [threading.Thread(target=run) for _ in range(8)]
    for th in threads:
        th.start()
    for th in threads:
        th.join(timeout=60)
    print("refreshes", authority.refreshes, "errors", len(errors))
    assert errors == []


# --- the production wiring ---------------------------------------------------------------------------------------

def production(L, tmp_path, *extra):
    args = qmain.parse_args(["--store", str(L), "--run", str(tmp_path / "run"), "--slot", "2", *extra])
    return qmain.build_broker(args, limits_owner=ME)


@pytest.fixture
def fence_run():
    """A short R (a unix socket path is bounded) holding ``fence/`` as the fence server demands it: mine, 0750."""
    top = Path(tempfile.mkdtemp(prefix="fen-", dir="/tmp"))
    (top / "fence").mkdir()
    os.chmod(top / "fence", 0o750)
    yield top
    shutil.rmtree(top)


def serve_fence(rig, run_dir, *, broker_uid=None, operator_uid=None, fences=None, **kw):
    """The real fence server over ``rig``'s real AgentFences, on ``run_dir/fence/fence.sock``. Both ends run as this
    uid here, so the server takes this uid as the broker's and a uid no process has as the operator's (the one seam
    a laptop cannot cross: two real accounts, P72_WIRING_DESIGN section 6)."""
    me = os.geteuid()
    srv = fence_server.FenceServer(rig.fences if fences is None else fences, qfence.fence_socket_path(run_dir),
                                   broker_uid=me if broker_uid is None else broker_uid,
                                   operator_uid=me + 100_000 if operator_uid is None else operator_uid,
                                   expected_gid=os.stat(run_dir / "fence").st_gid,
                                   store=rig.db.parent, cockpit_root=rig.world.root, **kw)
    srv.start()
    return srv


def test_the_production_broker_fences_a_revoked_agent_session_through_the_fence_server(rig, L, tmp_path, monkeypatch,
                                                                                       fence_run):
    srv = serve_fence(rig, fence_run)
    try:
        b = production(L, tmp_path, "--fence-socket", str(qfence.fence_socket_path(fence_run)),
                       "--fence-uid", str(os.geteuid()))
        assert isinstance(b._fence, RemoteFences)
        who = Resolved(session=SESSION, kind="dispatch", run=rig.run)
        monkeypatch.setattr(b, "_resolve", lambda slot, cap, **kw: who)
        assert go2(b)[0]["gate"] == "go"
        rig.world.link(revoke_agent())
        got = go2(b)[0]
        assert got["gate"] == "fenced" and "no longer live" in got["reason"]
    finally:
        srv.stop()


@pytest.mark.parametrize("extra", [(), ("--fence-socket", "/nonexistent"), ("--fence-uid", "501"),
                                   ("--fence-socket", "/x", "--fence-uid", "0")])
def test_the_production_broker_without_a_fence_refuses_every_dispatch_session(rig, L, tmp_path, monkeypatch, extra):
    b = production(L, tmp_path, *extra)
    assert isinstance(b._fence, Unfenceable)
    monkeypatch.setattr(b, "_resolve", lambda slot, cap, **kw: Resolved(session=SESSION, kind="dispatch",
                                                                        run=rig.run))
    got = go2(b)[0]
    assert got["gate"] == "limits-failed" and qmain.NO_FENCE in got["reason"]
    monkeypatch.setattr(b, "_resolve", lambda slot, cap, **kw: base.SESSION_A)
    assert gate(b, slot=1, cap="cap-a")["gate"] == "go"          # an interactive session is not a run


def test_the_production_broker_with_its_fence_down_refuses_every_dispatch_session(rig, L, tmp_path, monkeypatch,
                                                                                   fence_run):
    b = production(L, tmp_path, "--fence-socket", str(qfence.fence_socket_path(fence_run)),
                   "--fence-uid", str(os.geteuid()))
    monkeypatch.setattr(b, "_resolve", lambda slot, cap, **kw: Resolved(session=SESSION, kind="dispatch",
                                                                        run=rig.run))
    got = go2(b)[0]
    assert got["gate"] == "limits-failed" and "the agent fence is not running" in got["reason"]
    assert rig.calls()["known"] is False


def test_the_old_store_flags_are_gone_from_the_broker():
    # moved to the fence server (the operator's stores are out of _levain_qbroker's reach, P7-0 U4)
    for flag in ("--agent-store", "--cockpit-root"):
        with pytest.raises(SystemExit):
            qmain.parse_args(["--slot", "1", flag, "/x"])


def test_the_service_namespace_without_a_fence_builds_a_refusing_fence():
    assert isinstance(qmain.build_fence(argparse.Namespace(store="/x", run="/y")), Unfenceable)


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


def test_the_client_constants_are_the_brokers():
    assert (broker_client.RUN_CALLS_KIND, broker_client.RUN_DEADLINE_KIND) == (qbroker.RUN_CALLS_KIND,
                                                                               qbroker.RUN_DEADLINE_KIND)
    assert set(qbroker.GATE_VERDICTS) == {broker_client.GATE_GO, broker_client.GATE_HALTED, broker_client.GATE_FENCED,
                                          broker_client.GATE_OVER, broker_client.GATE_FAILED}


@pytest.mark.parametrize("answer", [{"gate": "fenced"}, {"gate": "fenced", "reason": 3}, {"gate": "fenced", "reason": ""},
                                    {"gate": "fenced", "reason": "r", "x": 1}, {"gate": "fenced", "request_id": "r"}])
def test_a_fenced_answer_out_of_shape_raises(answer):
    with pytest.raises(BrokerError):
        Scripted([answer]).gate(**KW)

"""CH-D1b-3, L3 r1 fixes (the head's rulings B1-B6): a read batch never crosses a liveness boundary, first-send is
consumed per child, an overdue driver call wedges its session at once, evidence that cannot be serialised is refused,
backfill is never routed, and the canary / pin / wedge edges. Each refusal fails closed and was reproduced red on the
pre-fix service first."""
from __future__ import annotations

import dataclasses
import threading

import pytest

from levain.coordinator import chat
from levain.coordinator.chat import ChatService, Verdict, _evidence_sha256, _last_cursor
from levain.coordinator.driver import LivenessEvidence, ReadBatch, ScriptedSession, event
from levain.coordinator.store import CoordError

from tests.test_coordinator_chat_liveness import (CANARY, DIGEST, NONCE, Probe, _world, activate, cs, fold,  # noqa: F401
                                                  kinds, ready, rows, send, service, stage1, store)


@pytest.fixture(params=["claude", "codex"])
def world(store, request):
    return _world(store, request.param)


@pytest.fixture
def claude(store):
    return _world(store, "claude")


@pytest.fixture
def codex(store):
    return _world(store, "codex")


class Route:
    def __init__(self, cs) -> None:
        self.cs, self.calls = cs, []

    def __call__(self, launch_id, keys):
        self.calls.append((keys, kinds(self.cs, launch_id)[-1]))


def _events(cs, launch):
    with cs._write() as (conn, _now):
        return [k for (k,) in conn.execute("SELECT vendor_event_key FROM chat_events WHERE launch_id = ? ORDER BY "
                                           "gseq", (launch,))]


def _cursor(cs, launch):
    return cs._read(lambda conn: _last_cursor(conn, launch))


# --- B1: a read batch may not cross a liveness boundary --------------------------------------------------------------
def _crossing(after: int) -> ReadBatch:
    """[turn_started, approval_request, turn_ended] past position ``after``: two boundaries on Claude, and a
    turn_started that is not the batch's last event on any harness."""
    p = [f"{after + i:08d}" for i in (1, 2, 3)]
    evs = (event("turn_started", "t-1", p[0], turn_id="t-1"),
           event("approval_request", "req-1", p[1], turn_id="t-1", request_key="req-1", host=True,
                 tool_name="Bash", tool_input={"command": "ls"}, control_subtype="can_use_tool"),
           event("turn_ended", "t-1", p[2], turn_id="t-1"))
    return ReadBatch(evs, p[2], p[2], False)


def test_batch_crossing_a_boundary_is_refused_and_latches(cs, world):
    route = Route(cs)
    svc, _s = ready(cs, world, route=route)
    route.calls.clear()
    before_events, before_cursor = _events(cs, world["launch"]), _cursor(cs, world["launch"])
    with pytest.raises(CoordError, match="crossed a boundary"):
        svc.ingest(world["launch"], _crossing(0))
    assert _events(cs, world["launch"]) == before_events               # nothing recorded
    assert _cursor(cs, world["launch"]) == before_cursor               # the cursor did not advance
    kind, _src, detail, *_ = rows(cs, world["launch"])[-1]
    assert (kind, detail) == ("not_live", "batch crossed a boundary")
    assert fold(cs, world["launch"]).latched and route.calls == []


def test_boundary_last_in_its_batch_is_accepted(cs, world):
    svc, _s = ready(cs, world)
    b = _crossing(0)
    svc.ingest(world["launch"], ReadBatch(b.events[1:2] + b.events[:1], b.events[0].pos, b.events[0].pos, False))
    # the request before the turn start, then the turn start last: one boundary, its own probing and verdict
    assert kinds(cs, world["launch"])[-2:] == [("probing", "turn_start"), ("live", "turn_start")]


def test_scripted_session_ends_each_batch_at_a_boundary(cs, world):
    # the same three events shown at once: each read stops at a boundary, so every boundary gets its own probing row
    # and verdict before the request behind it is handled
    route = Route(cs)
    svc, s = ready(cs, world, route=route)
    route.calls.clear()
    s.show("turn_started", "t-1", turn_id="t-1")
    s.inbound("req-1", "can_use_tool", tool_name="Bash", tool_input={}, turn_id="t-1")
    s.show("turn_ended", "t-1", turn_id="t-1")
    while svc.read_next(world["launch"]):
        pass
    want = [("live", "activation"), ("probing", "turn_start"), ("live", "turn_start")]
    if world["harness"] == "claude":
        want += [("probing", "result"), ("live", "result")]
    assert kinds(cs, world["launch"]) == want
    # each batch is routed after its own boundary's verdict; the request was recorded after the turn start's verdict
    assert route.calls[0] == (("turn_started:t-1",), ("live", "turn_start"))
    assert route.calls[1] == (("approval_request:req-1", "turn_ended:t-1"),
                              ("live", "result") if world["harness"] == "claude" else ("live", "turn_start"))
    with cs._write() as (conn, _now):
        verdict = conn.execute("SELECT gseq FROM chat_launch_checks WHERE launch_id = ? AND kind = 'live' AND "
                               "source = 'turn_start'", (world["launch"],)).fetchone()[0]
        req = conn.execute("SELECT gseq FROM chat_events WHERE vendor_event_key = 'approval_request:req-1'"
                           ).fetchone()[0]
    assert verdict < req


def test_history_read_may_hold_many_boundaries(cs, world):
    s = ScriptedSession()
    s.show("turn_started", "t-0", turn_id="t-0")
    s.show("turn_ended", "t-0", turn_id="t-0")
    s.show("turn_started", "t-1", turn_id="t-1")
    svc, _ = ready(cs, world, session=s)                     # the first read: history, recorded as backfill
    assert len(_events(cs, world["launch"])) == 3 and fold(cs, world["launch"]).enabled


# --- B2: first send consumed per spawn nonce ---------------------------------------------------------------------
def test_replayed_stage1_is_refused(cs, claude):
    svc = service(cs)
    s = ScriptedSession()
    svc.attach(claude["launch"], s)
    svc.read_next(claude["launch"])
    stage1(svc, claude)
    assert send(svc, cs, claude).state == "accepted"
    n = len(rows(cs, claude["launch"]))
    with pytest.raises(CoordError, match="stage 1"):
        svc.activate(claude["launch"], "probing", "stage 1 again", spawn_nonce=NONCE,
                     evidence=LivenessEvidence("claude init", {"pid": 4242}, ""))
    assert len(rows(cs, claude["launch"])) == n
    r = send(svc, cs, claude, outbox="ob-00000002")
    assert r.state == "refused" and len(s.written) == 1


def test_first_send_counts_from_the_childs_first_activation_row(cs, claude):
    # a stage-1 row written past the writer (a replay straight into the table) opens no second first send
    svc = service(cs)
    s = ScriptedSession()
    svc.attach(claude["launch"], s)
    svc.read_next(claude["launch"])
    stage1(svc, claude)
    assert send(svc, cs, claude).state == "accepted"
    with cs._write() as (conn, now):
        chat.record_check(conn, now, claude["launch"], "probing", "activation", "replayed", spawn_nonce=NONCE,
                          evidence_sha256="sha256:" + "1" * 64)
    assert send(svc, cs, claude, outbox="ob-00000002").state == "refused" and len(s.written) == 1


def test_a_new_child_gets_its_own_first_send(cs, claude):
    svc = service(cs)
    s = ScriptedSession()
    svc.attach(claude["launch"], s)
    svc.read_next(claude["launch"])
    stage1(svc, claude)
    assert send(svc, cs, claude).state == "accepted"
    other = "fedcba9876543210fedcba9876543210"
    svc.activate(claude["launch"], "probing", "respawned", spawn_nonce=other)
    svc.activate(claude["launch"], "probing", "stage 1", spawn_nonce=other,
                 evidence=LivenessEvidence("claude init", {"pid": 4343}, ""))
    assert send(svc, cs, claude, outbox="ob-00000002").state == "accepted"


# --- B3: an overdue driver call wedges the session at once ---------------------------------------------------------
class Hangs(ScriptedSession):
    def __init__(self, **kw) -> None:
        super().__init__(**kw)
        self.release = threading.Event()
        self.hang_evidence = False
        self.hang_interrupt = False
        self.evidence_calls = 0

    def liveness_evidence(self):
        self.evidence_calls += 1
        if self.hang_evidence:
            self.release.wait(10)
        return super().liveness_evidence()

    def interrupt(self, command):
        if self.hang_interrupt:
            self.release.wait(10)
        return super().interrupt(command)

    def close(self):
        self.release.set()
        super().close()


def _driver_threads() -> int:
    return sum(t.name == "levain-chat-driver-call" and t.is_alive() for t in threading.enumerate())


def test_overdue_evidence_read_wedges_and_latches_at_once(cs, world, monkeypatch):
    monkeypatch.setattr(chat, "READ_DEADLINE", 0.2)
    s = Hangs()
    svc, _ = ready(cs, world, session=s)
    base = _driver_threads()
    s.hang_evidence = True
    s.show("turn_started", "t-1", turn_id="t-1")
    with pytest.raises(CoordError, match="stopped answering"):
        svc.read_next(world["launch"])
    assert s.evidence_calls == 1 and _driver_threads() == base + 1          # one try, one stuck call
    kind, _src, detail, *_ = rows(cs, world["launch"])[-1]
    assert (kind, detail) == ("not_live", "could not check: evidence read overdue")
    # nothing else reaches the session while its call is stuck
    with pytest.raises(CoordError, match="stopped answering"):
        svc.read_next(world["launch"])
    assert send(svc, cs, world).state == "refused"
    with pytest.raises(CoordError, match="stopped answering"):
        svc.interrupt(world["launch"], None)
    assert s.evidence_calls == 1 and _driver_threads() == base + 1
    svc.detach(world["launch"])
    assert s.closed


def test_overdue_interrupt_wedges_the_session(cs, world, monkeypatch):
    monkeypatch.setattr(chat, "INTERRUPT_DEADLINE", 0.2)
    s = Hangs()
    svc, _ = ready(cs, world, session=s)
    s.hang_interrupt = True
    assert svc.interrupt(world["launch"], None).outcome == "ambiguous"
    with pytest.raises(CoordError, match="stopped answering"):
        svc.interrupt(world["launch"], None)
    with pytest.raises(CoordError, match="stopped answering"):
        svc.read_next(world["launch"])
    assert send(svc, cs, world).state == "refused"
    svc.detach(world["launch"])
    assert s.closed


# --- B4: evidence that cannot be serialised -------------------------------------------------------------------------
def test_unserialisable_stage1_evidence_is_refused(cs, claude):
    svc = service(cs)
    svc.activate(claude["launch"], "probing", "spawned", spawn_nonce=NONCE)
    n = len(rows(cs, claude["launch"]))
    with pytest.raises(CoordError, match="serialis"):
        svc.activate(claude["launch"], "probing", "stage 1", spawn_nonce=NONCE,
                     evidence=LivenessEvidence("claude init", b"\x00raw", ""))
    assert len(rows(cs, claude["launch"])) == n


def test_evidence_digest_catches_every_exception():
    deep: list = []
    for _ in range(100_000):
        deep = [deep]
    assert _evidence_sha256(LivenessEvidence("x", deep, "")) is None


class Bytes(ScriptedSession):
    def liveness_evidence(self):
        return LivenessEvidence("scripted", b"\x00raw", "")


def test_unhashable_probe_evidence_is_a_failed_try(cs, world):
    probe = Probe(Verdict(True, "ok"))
    svc, s = ready(cs, world, session=Bytes(), probe=probe)
    s.show("turn_started", "t-1", turn_id="t-1")
    svc.read_next(world["launch"])
    kind, _src, detail, *_ = rows(cs, world["launch"])[-1]
    assert kind == "not_live" and detail.startswith("could not check: ") and probe.seen == []


# --- B5: backfill is never routed --------------------------------------------------------------------------------
def test_backfill_request_is_not_routed(cs, world):
    route = Route(cs)
    s = ScriptedSession()
    s.inbound("req-0", "can_use_tool", tool_name="Bash", tool_input={}, turn_id="t-0")   # before the first read
    ready(cs, world, session=s, route=route)
    assert "approval_request:req-0" in _events(cs, world["launch"])
    assert all("approval_request:req-0" not in keys for keys, _ in route.calls)


# --- B6: canary serialisation, the unstarted pin, the wedge cleared on attach ----------------------------------------
@dataclasses.dataclass(frozen=True)
class OddCanary:
    ok: bool
    blob: bytes


def test_canary_that_cannot_serialise_is_a_coord_error(cs, claude):
    svc = service(cs)
    with pytest.raises(CoordError):
        svc.activate(claude["launch"], "live", "x", spawn_nonce=NONCE, canary=OddCanary(True, b"\x00"),
                     containment_digest=DIGEST)
    assert rows(cs, claude["launch"]) == []


def test_pin_released_when_the_call_thread_cannot_start(cs, world, monkeypatch):
    svc, _s = ready(cs, world)

    def no_thread(self):
        raise RuntimeError("can't start new thread")

    monkeypatch.setattr(threading.Thread, "start", no_thread)
    got = svc.interrupt(world["launch"], None)
    monkeypatch.undo()
    assert got.outcome == "ambiguous" and svc._pins == {}


def test_attach_clears_a_stale_wedge(cs, world):
    svc = service(cs)
    activate(svc, world)
    with svc._guard:
        svc._wedged.add(world["launch"])        # left by a call that went overdue after its session was detached
    svc.attach(world["launch"], ScriptedSession())
    svc.read_next(world["launch"])
    assert svc.composer(world["launch"], "cockpit:tab-a").state != "no_session"


def test_late_overdue_does_not_wedge_a_replacement_session(cs, world):
    svc, s = ready(cs, world)
    fresh = ScriptedSession()
    svc.detach(world["launch"])
    svc.attach(world["launch"], fresh)
    svc._wedge(world["launch"], s)              # the old session's call goes overdue after the swap
    svc.read_next(world["launch"])

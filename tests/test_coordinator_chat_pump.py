"""CH-D1b-3: the pump. One step (:meth:`ChatService.read_next`) runs a pending probe before it handles the vendor's
next line, records the batch (with any boundary's ``probing`` row in the same transaction), runs the new probe, and
only then hands the new events to the route seam, so a request queued behind a boundary is routed after its verdict
(§4.2). The vendor read and the interrupt are bounded (the D1b-2 known edges), and a thread race on a launch is run
in-process."""
from __future__ import annotations

import threading

import pytest

from levain.coordinator import chat
from levain.coordinator.chat import ChatService, Verdict, liveness
from levain.coordinator.driver import CommandResult, ScriptedSession
from levain.coordinator.store import CoordError

from tests.test_coordinator_chat_liveness import (ME, Probe, _world, activate, cs, kinds, ready,  # noqa: F401
                                                  send, store)


@pytest.fixture(params=["claude", "codex"])
def world(store, request):
    return _world(store, request.param)


@pytest.fixture
def claude(store):
    return _world(store, "claude")


class Route:
    """The route seam: records, for each call, the keys it was handed and the fold at that moment."""

    def __init__(self, cs, launch) -> None:
        self.cs, self.launch, self.calls = cs, launch, []

    def __call__(self, launch_id, keys):
        f = self.cs._read(lambda conn: liveness(conn, launch_id))
        self.calls.append((keys, f.latest, f.enabled))


def test_request_during_probing_routes_after_verdict(cs, world):
    route = Route(cs, world["launch"])
    svc, s = ready(cs, world, route=route)
    route.calls.clear()
    s.show("turn_started", "t-1", turn_id="t-1")
    s.inbound("req-1", "can_use_tool", tool_name="Bash", tool_input={"command": "ls"}, turn_id="t-1")
    svc.read_next(world["launch"])
    (keys, latest, enabled), = route.calls
    assert keys == ("turn_started:t-1", "approval_request:req-1")
    assert (latest, enabled) == ("live", True)               # routed after the turn's verdict, never under probing


def test_request_during_not_live_verdict_routes_under_not_live(cs, world):
    route = Route(cs, world["launch"])
    svc, s = ready(cs, world, probe=Probe(Verdict(False, "hook gone")), route=route)
    route.calls.clear()
    s.show("turn_started", "t-1", turn_id="t-1")
    s.inbound("req-1", "can_use_tool", tool_name="Bash", tool_input={}, turn_id="t-1")
    svc.read_next(world["launch"])
    assert route.calls[0][1:] == ("not_live", False)


def test_claude_request_during_send_probe_routes_after_verdict(cs, claude):
    route = Route(cs, claude["launch"])
    probe = Probe(Verdict(True, "ok"))
    svc, s = ready(cs, claude, probe=probe, route=route)

    def stuck(launch_id, evidence):
        raise KeyboardInterrupt                               # the send's probe is cut off: probing stays pending

    svc._probe = stuck
    with pytest.raises(KeyboardInterrupt):
        send(svc, cs, claude)
    assert kinds(cs, claude["launch"])[-1] == ("probing", "send")
    svc._probe = probe
    route.calls.clear()
    s.inbound("req-1", "can_use_tool", tool_name="Bash", tool_input={}, turn_id="t-1")
    svc.read_next(claude["launch"])
    # the pending send probe ran BEFORE the vendor's next line was handled, and the request was routed after it
    assert kinds(cs, claude["launch"])[-2:] == [("probing", "send"), ("live", "send")]
    with cs._write() as (conn, _now):
        verdict_gseq = conn.execute("SELECT MAX(gseq) FROM chat_launch_checks WHERE launch_id = ?",
                                    (claude["launch"],)).fetchone()[0]
        req_gseq = conn.execute("SELECT gseq FROM chat_events WHERE vendor_event_key = 'approval_request:req-1'"
                                ).fetchone()[0]
    assert verdict_gseq < req_gseq
    assert route.calls[-1][1:] == ("live", True)


def test_route_sees_only_new_events(cs, world):
    route = Route(cs, world["launch"])
    svc, s = ready(cs, world, route=route)
    route.calls.clear()
    s.show("agent_text", "a-1", body="hi")
    svc.read_next(world["launch"])
    svc.read_next(world["launch"])                            # nothing new: the seam is not called
    assert [c[0] for c in route.calls] == [("agent_text:a-1",)]


# --- the bounded read and interrupt ----------------------------------------------------------------------------------
class Hangs(ScriptedSession):
    def __init__(self, **kw) -> None:
        super().__init__(**kw)
        self.release = threading.Event()
        self.hang_read = False
        self.hang_interrupt = False

    def read(self, cursor):
        if self.hang_read:
            self.release.wait(10)
        return super().read(cursor)

    def interrupt(self, command):
        if self.hang_interrupt:
            self.release.wait(10)
        return super().interrupt(command)

    def close(self):
        self.release.set()
        super().close()


def test_read_that_never_returns_is_bounded(cs, world, monkeypatch):
    monkeypatch.setattr(chat, "READ_DEADLINE", 0.2)
    s = Hangs()
    svc, _ = ready(cs, world, session=s)
    s.hang_read = True
    with pytest.raises(CoordError, match="did not return"):
        svc.read_next(world["launch"])
    # the launch's mutex is free again: a send is answered (refused: its session stopped answering), never blocked
    r = send(svc, cs, world)
    assert r.state == "refused" and "stopped answering" in r.cause
    with pytest.raises(CoordError, match="stopped answering"):
        svc.read_next(world["launch"])
    svc.detach(world["launch"])                               # close unblocks the stuck read
    assert s.closed
    svc.attach(world["launch"], ScriptedSession())            # a fresh session is not wedged
    svc.read_next(world["launch"])


def test_interrupt_that_never_returns_is_ambiguous_and_detach_closes(cs, world, monkeypatch):
    monkeypatch.setattr(chat, "INTERRUPT_DEADLINE", 0.2)
    s = Hangs()
    svc, _ = ready(cs, world, session=s)
    s.hang_interrupt = True
    got = svc.interrupt(world["launch"], "t-1")
    assert got.outcome == "ambiguous" and "did not answer" in got.detail
    svc.detach(world["launch"])                               # waits at most the deadline, then closes
    assert s.closed


def test_interrupt_in_time_still_pins_detach(cs, world):
    s = Hangs()
    svc, _ = ready(cs, world, session=s)
    s.hang_interrupt = True
    out = []
    t = threading.Thread(target=lambda: out.append(svc.interrupt(world["launch"], None)))
    t.start()
    d = threading.Thread(target=svc.detach, args=(world["launch"],))
    for _ in range(200):
        with svc._guard:
            if svc._pins:
                break
        threading.Event().wait(0.005)
    d.start()
    d.join(0.1)
    assert d.is_alive() and not s.closed                      # detach waits on the pinned interrupt
    s.release.set()
    t.join(5)
    d.join(5)
    assert out == [CommandResult("confirmed", "interrupted")] and s.closed


# --- a thread race on the pump, in-process -------------------------------------------------------------------------
def test_send_racing_a_turn_probe_never_passes_under_probing(cs, world):
    entered, release = threading.Event(), threading.Event()

    def slow(launch_id, evidence):
        entered.set()
        release.wait(5)
        return Verdict(True, "ok")

    svc, s = ready(cs, world)
    svc._probe = slow
    s.show("turn_started", "t-1", turn_id="t-1")
    pump = threading.Thread(target=svc.read_next, args=(world["launch"],))
    pump.start()
    assert entered.wait(5)
    out = []
    sender = threading.Thread(target=lambda: out.append(send(svc, cs, world)))
    sender.start()
    sender.join(0.1)
    assert sender.is_alive() and s.written == []              # the send waits on the launch's mutex
    release.set()
    pump.join(5)
    sender.join(5)
    assert out[0].state == "accepted"
    with cs._write() as (conn, _now):
        attempt = conn.execute("SELECT MIN(gseq) FROM chat_outbox_events WHERE state = 'attempted'").fetchone()[0]
        live = conn.execute("SELECT MAX(gseq) FROM chat_launch_checks WHERE kind = 'live' AND source = 'turn_start'"
                            ).fetchone()[0]
    assert live < attempt


# --- the crash matrix at the probe point ---------------------------------------------------------------------------
def test_restart_between_probing_and_verdict_judges_before_the_next_line(cs, claude):
    # the service dies after the send's probing row committed and before its verdict: the latest check stays probing,
    # every send refuses meanwhile, and a restarted service's first read judges it before recording the next line
    svc, s = ready(cs, claude)

    def dies(launch_id, evidence):
        raise SystemExit("killed mid-probe")

    svc._probe = dies
    with pytest.raises(SystemExit):
        send(svc, cs, claude)
    assert kinds(cs, claude["launch"])[-1] == ("probing", "send")
    restarted = ChatService(cs, probe=Probe(Verdict(True, "ok")), sleep=lambda _s: None)
    assert restarted.recover() == {"ambiguous": 0, "not_delivered": 0}
    s2 = ScriptedSession()
    restarted.attach(claude["launch"], s2)
    r = send(restarted, cs, claude, outbox="ob-00000002")
    assert r.state == "refused" and "check is running" in r.cause and s2.written == []
    restarted.read_next(claude["launch"])
    assert kinds(cs, claude["launch"])[-2:] == [("probing", "send"), ("live", "send")]


# --- broken fake (§12): a service that routes a request during probing -------------------------------------------
class _RoutesDuringProbing(ChatService):
    """BROKEN FAKE: routes a step's new events without running the boundary's probe first (it is left for a later
    step)."""

    late = False

    def _probe_pending(self, launch_id, session):
        if not self.late:
            super()._probe_pending(launch_id, session)


def _routes_after_verdict(svc_cls, cs, world) -> bool:
    route = Route(cs, world["launch"])
    svc = svc_cls(cs, probe=Probe(Verdict(True, "ok")), route=route, sleep=lambda _s: None)
    activate(svc, world)
    s = ScriptedSession()
    svc.attach(world["launch"], s)
    svc.read_next(world["launch"])
    route.calls.clear()
    s.show("turn_started", "t-1", turn_id="t-1")
    s.inbound("req-1", "can_use_tool", tool_name="Bash", tool_input={}, turn_id="t-1")
    svc.late = True
    svc.read_next(world["launch"])
    return [c[1] for c in route.calls] == ["live"]


def test_broken_fake_routes_during_probing(store, cs):
    assert _routes_after_verdict(ChatService, cs, _world(store, "codex"))
    assert not _routes_after_verdict(_RoutesDuringProbing, cs, _world(store, "codex"))

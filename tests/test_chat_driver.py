"""S1 of the chat-to-primary build: the HarnessDriver contract (``levain.chat_driver``) and the chat host
driving it. A fake driver stands in for a second harness; no SDK and no model is contacted.

The existing ``tests/test_chat.py`` is the proof that moving OpenHands behind the contract changed no
behaviour (it runs unchanged). This file pins what the contract adds: the host holds a driver per
session by entity name, refuses a driver whose outcomes break the consent guarantees, and refuses one
whose consent needs a state machine the host does not have.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import pytest

from levain.chat import ChatError, ChatHost
from levain.chat_driver import (
    DriverCaps,
    DriverContractError,
    DriverEvent,
    DriverState,
    DriverUnsupported,
    HarnessDriver,
    OpenHandsDriver,
    PendingApproval,
    TurnSnapshot,
    read_outcome,
)
from levain.firing.gate import PendingEfferent


@dataclass
class _Out:
    reply: str | None = "ok"
    tool_activity: list[str] = field(default_factory=list)
    error: str | None = None
    nudged: bool = False
    gated: bool = False
    timed_out: bool = False
    pending: tuple = ()
    held_digest: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None and not self.gated and not self.timed_out and bool(self.reply)

    @property
    def exit_code(self) -> int:
        return 0 if self.ok else 3


_HELD = PendingEfferent("terminal", "rm x", "bash fans in", full='{"command": "rm x"}')


class _Fake(HarnessDriver):
    """A second harness: scripted outcomes, its own state tracking, nothing OpenHands about it."""

    harness = "fake"

    def __init__(self, script: list[_Out], *, timing: str = "after_turn"):
        self.caps = DriverCaps(approval_timing=timing)   # type: ignore[arg-type]
        self.script = script
        self._state: DriverState = "closed"
        self.calls: list[tuple[str, Any]] = []
        self.sink: Callable[[DriverEvent], None] | None = None
        self.opened = self.closed = False
        self.stops = 0
        self.probe: Callable[[], Any] | None = None
        self.probed: Any = None

    def open(self, on_event, *, resume=None):
        self.sink, self.opened, self._state = on_event, True, "idle"

    def close(self):
        self.closed, self._state = True, "closed"

    @property
    def state(self) -> DriverState:
        return self._state

    def describe(self):
        return {"label": "fake", "model_label": "fake-model"}

    def _next(self, name, arg=None) -> _Out:
        self.calls.append((name, arg))
        if self.sink:
            self.sink(DriverEvent("activity", f"fake: {name}"))
            self.sink(DriverEvent("a-kind-nobody-knows", "ignored"))
        if self.probe:
            self.probed = self.probe()
        snap = read_outcome(self.script.pop(0))
        self._state = "awaiting_approval" if snap.gated else "idle"
        return snap

    def send_turn(self, message):
        return self._next("send_turn", message)

    def approve(self):
        return self._next("approve")

    def reject(self, reason):
        return self._next("reject", reason)

    def held_digest(self):
        return "d1" if self._state == "awaiting_approval" else None

    def interrupt(self):
        self.stops += 1


def _wait(host, job_id, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        st = host.job_status(job_id)
        if st["status"] != "running":
            return st
        time.sleep(0.01)
    raise AssertionError(host.job_status(job_id))


def _host(tmp_path, made: dict[str, _Fake], **kw) -> ChatHost:
    def factory(name: str, entity_dir: Path) -> HarnessDriver:
        return made[name]

    return ChatHost({n: tmp_path / n for n in made}, driver_factory=factory, **kw)


def _open(host, name):
    out = host.open(name)
    st = _wait(host, out["job_id"])
    return out["session_id"], st


def _halt() -> _Out:
    return _Out(reply=None, gated=True, pending=(_HELD,), held_digest="d1")


# -- the host drives any driver ------------------------------------------------------------------


def test_the_host_holds_a_driver_per_entity_and_a_second_harness_runs_the_whole_consent_loop(tmp_path):
    a = _Fake([_halt(), _Out(reply="ran it")])
    b = _Fake([_Out(reply="beta here")])
    host = _host(tmp_path, {"alpha": a, "beta": b})
    sa, st = _open(host, "alpha")
    sb, _ = _open(host, "beta")
    assert st["result"]["session"]["label"] == "fake" and a.opened and b.opened

    a.probe = lambda: host.job_status(host._sessions[sa].job_id)["activity"]
    res = _wait(host, host.turn(sa, "go")["job_id"])
    assert res["status"] == "done"
    assert a.probed == ["fake: send_turn"]      # streamed to the running job; the unknown kind is dropped
    out = res["result"]
    assert out["gated"] and out["pending"][0]["full"] == _HELD.full
    assert host.session_status(sa)["state"] == "gated"
    done = _wait(host, host.approve(sa, out["decision_id"])["job_id"])
    assert done["result"]["reply"] == "ran it" and [c[0] for c in a.calls] == ["send_turn", "approve"]
    assert _wait(host, host.turn(sb, "hi")["job_id"])["result"]["reply"] == "beta here"
    assert [c[0] for c in b.calls] == ["send_turn"]   # beta's driver never saw alpha's calls
    host.shutdown()
    assert a.closed and b.closed


def test_reject_goes_through_the_driver_and_nothing_runs(tmp_path):
    a = _Fake([_halt(), _Out(reply="declined")])
    host = _host(tmp_path, {"alpha": a})
    sid, _ = _open(host, "alpha")
    out = _wait(host, host.turn(sid, "go")["job_id"])["result"]
    _wait(host, host.reject(sid, "no", out["decision_id"])["job_id"])
    assert [c[0] for c in a.calls] == ["send_turn", "reject"] and a.calls[1][1] == "no"


def test_the_deadline_watcher_interrupts_through_the_driver(tmp_path):
    class Slow(_Fake):
        def send_turn(self, message):
            deadline = time.monotonic() + 5
            while self.stops == 0 and time.monotonic() < deadline:
                time.sleep(0.01)
            return read_outcome(_Out(reply=None, timed_out=True))

    d = Slow([])
    host = _host(tmp_path, {"alpha": d}, turn_seconds=0.05)
    sid, _ = _open(host, "alpha")
    st = _wait(host, host.turn(sid, "x")["job_id"])
    assert d.stops >= 1 and st["deadline_hit"] and st["result"]["timed_out"]


# -- the contract's guarantees, enforced by the host -----------------------------------------------


@pytest.mark.parametrize(
    "outcome, needle",
    [
        (_Out(reply=None, gated=True, pending=(), held_digest="d"), "no held action"),
        (_Out(reply="x", gated=False, pending=(_HELD,)), "did not report as halted"),
    ],
)
def test_a_driver_that_hides_or_skips_the_consent_row_is_refused(tmp_path, outcome, needle):
    """Mutation: the driver halts with an empty consent row (nothing to show the operator), or returns held
    calls while reporting an ordinary turn (nothing stops them being run). Either breaks the session; the
    host never records a hold it cannot show."""
    d = _Fake([outcome])
    host = _host(tmp_path, {"alpha": d})
    sid, _ = _open(host, "alpha")
    st = _wait(host, host.turn(sid, "go")["job_id"])
    assert st["status"] == "failed" and needle in st["error"]
    assert host.session_status(sid)["state"] == "broken" and d.closed


def test_a_halt_whose_digest_is_missing_can_only_be_rejected(tmp_path):
    """Fail-closed, not a violation: no digest means nothing for an approval to bind to."""
    d = _Fake([_Out(reply=None, gated=True, pending=(_HELD,), held_digest=None)])
    d.held_digest = lambda: None   # type: ignore[method-assign]
    host = _host(tmp_path, {"alpha": d})
    sid, _ = _open(host, "alpha")
    out = _wait(host, host.turn(sid, "go")["job_id"])["result"]
    with pytest.raises(ChatError) as e:
        host.approve(sid, out["decision_id"])
    assert e.value.code == "stale_decision"
    assert not [c for c in d.calls if c[0] == "approve"]


def test_a_driver_that_needs_in_turn_consent_is_refused_at_open_not_driven(tmp_path):
    """S10 is not built: a driver whose consent request arrives while the turn is running would be
    driven through a path that cannot carry it. Refused before it is opened."""
    d = _Fake([], timing="in_turn")
    host = _host(tmp_path, {"alpha": d})
    sid, st = _open(host, "alpha")
    assert st["status"] == "failed" and "approval state machine" in st["error"]
    assert not d.opened and host.session_status(sid)["state"] == "failed"


def test_a_failed_outcome_is_not_second_guessed(tmp_path):
    d = _Fake([_Out(reply=None, error="boom", gated=True)])
    host = _host(tmp_path, {"alpha": d})
    sid, _ = _open(host, "alpha")
    st = _wait(host, host.turn(sid, "go")["job_id"])
    assert st["result"]["error"] == "boom" and host.session_status(sid)["state"] == "broken"


def test_a_session_factory_and_a_driver_factory_together_are_refused(tmp_path):
    with pytest.raises(ValueError):
        ChatHost({"a": tmp_path}, session_factory=lambda d, **k: None,
                 driver_factory=lambda n, p: _Fake([]))


def test_a_snapshot_that_breaks_the_hold_guarantees_cannot_be_built():
    """The guarantees live where the snapshot is built: one that breaks them does not exist to be recorded."""
    assert read_outcome(_halt()).gated and not read_outcome(_Out()).gated
    with pytest.raises(DriverContractError, match="no held action"):
        read_outcome(_Out(gated=True))
    with pytest.raises(DriverContractError, match="did not report as halted"):
        read_outcome(_Out(reply="x", pending=(_HELD,)))


# -- the base class and the OpenHands driver -----------------------------------------------------------


def test_optional_capabilities_refuse_by_default_and_openhands_offers_none_of_them(tmp_path):
    d = _Fake([])
    for call in (lambda: d.set_model("m"), lambda: d.set_effort("low"), lambda: d.list_models()):
        with pytest.raises(DriverUnsupported):
            call()
    oh = OpenHandsDriver(tmp_path, lambda p, on_event: None)
    assert oh.caps == DriverCaps() and oh.caps.approval_timing == "after_turn"
    with pytest.raises(DriverUnsupported):
        oh.open(lambda e: None, resume="abc")      # nothing is persisted, so nothing resumes


def test_the_openhands_driver_tracks_state_and_forwards_text_events_as_activity(tmp_path):
    class Sess:
        label, model_label, gate_mode, bash_ok = "e", "m", "gated", True
        workspace = tmp_path

        def __init__(self, on_event):
            self.on_event, self.closed = on_event, False

        def run_turn(self, message):
            self.on_event("⚙ terminal: ls")
            return _halt()

        def resume_turn(self):
            return _Out()

        def reject_turn(self, reason):
            return _Out()

        def held_digest(self):
            return "d1"

        def request_stop(self):
            self.stopped = True

        def close(self):
            self.closed = True

    seen: list[DriverEvent] = []
    d = OpenHandsDriver(tmp_path, lambda p, on_event: Sess(on_event))
    assert d.state == "closed" and d.native is None
    d.open(seen.append)
    assert d.state == "idle" and d.describe()["label"] == "e" and d.describe()["workspace"] == str(tmp_path)
    assert d.send_turn("x").gated and d.state == "awaiting_approval"
    assert seen == [DriverEvent("activity", "⚙ terminal: ls")]
    assert d.held_digest() == "d1"
    d.approve()
    assert d.state == "idle"
    d.interrupt()
    sess = d.native
    d.close()
    d.close()                      # idempotent
    assert sess.closed and d.state == "closed" and d.native is None and d.held_digest() is None


def test_a_real_turn_result_satisfies_the_outcome_protocol_the_host_reads():
    """chat_driver's TurnOutcome says TurnResult satisfies it as it stands; this is what makes that true."""
    from levain.chat_driver import TurnOutcome
    from levain.session import TurnResult

    assert isinstance(TurnResult(reply="hi"), TurnOutcome)
    assert not isinstance(object(), TurnOutcome)


def test_an_outcome_missing_the_fields_the_host_reads_is_refused(tmp_path):
    """L2: a driver that omits `gated` or `pending` would read as 'nothing held', which is fail-open on
    exactly the property the snapshot's checks exist for."""
    class Bare:
        reply, tool_activity, error, nudged, timed_out, ok, exit_code = "x", [], None, False, False, True, 0

    class Raw(_Fake):
        def send_turn(self, message, *, options=None):
            return read_outcome(Bare())

    d = Raw([])
    host = _host(tmp_path, {"alpha": d})
    sid, _ = _open(host, "alpha")
    st = _wait(host, host.turn(sid, "go")["job_id"])
    assert st["status"] == "failed" and "fields the host reads" in st["error"]


def test_a_driver_that_returns_anything_but_a_snapshot_is_refused(tmp_path):
    class Raw(_Fake):
        def send_turn(self, message, *, options=None):
            return _Out(reply="looks fine")   # type: ignore[return-value]

    d = Raw([])
    host = _host(tmp_path, {"alpha": d})
    sid, _ = _open(host, "alpha")
    st = _wait(host, host.turn(sid, "go")["job_id"])
    assert st["status"] == "failed" and "not a TurnSnapshot" in st["error"] and d.closed


def test_per_turn_options_and_after_turn_answers_refuse_by_default(tmp_path):
    from levain.chat_driver import TurnOptions

    oh = OpenHandsDriver(tmp_path, lambda p, on_event: None)
    with pytest.raises(DriverUnsupported):
        oh.send_turn("x", options=TurnOptions(model="m"))
    d = _Fake([])
    with pytest.raises(DriverUnsupported):
        HarnessDriver.approve(d)      # the base default, not the fake's override
    with pytest.raises(DriverUnsupported):
        HarnessDriver.reject(d, "no")


def test_a_failed_open_closes_the_driver_it_built(tmp_path):
    """L1: a driver's open() may allocate before it raises; the host closes it and keeps only the text."""
    class Leaky(_Fake):
        def open(self, on_event, *, resume=None):
            self.opened = True
            raise RuntimeError("half built")

    d = Leaky([])
    host = _host(tmp_path, {"alpha": d})
    sid, st = _open(host, "alpha")
    assert st["status"] == "failed" and st["error"] == "half built" and d.closed


def test_the_openhands_driver_is_not_driveable_when_closed_and_keeps_an_errored_halt_held(tmp_path):
    class Sess:
        def run_turn(self, m):
            return _Out(reply=None, gated=True, error="refusal did not take")

        def close(self):
            pass

    d = OpenHandsDriver(tmp_path, lambda p, on_event: Sess())
    with pytest.raises(RuntimeError):
        d.send_turn("x")                     # never opened
    d.open(lambda e: None)
    assert d.send_turn("x").error and d.state == "awaiting_approval"   # the harness still holds actions
    d.close()
    with pytest.raises(RuntimeError):
        d.send_turn("x")
    assert d.state == "closed"
    d.interrupt()                            # tolerated after close


# -- 1007+19: the driver life cycle is a forward-only state machine; the outcome is read once ------------


class _Hands:
    """A session whose turn blocks until a stop request arrives, recording the order of events."""

    def __init__(self):
        self.log: list[str] = []
        self.entered = threading.Event()
        self.stop = threading.Event()

    def run_turn(self, message):
        self.log.append("turn-start")
        self.entered.set()
        assert self.stop.wait(5)
        self.log.append("turn-end")
        return _Out(reply="stopped")

    def request_stop(self):
        self.log.append("stop")
        self.stop.set()

    def close(self):
        self.log.append("close")


def test_close_during_a_running_turn_ends_the_turn_and_only_then_releases_the_session(tmp_path):
    hands = _Hands()
    d = OpenHandsDriver(tmp_path, lambda p, on_event: hands)
    d.open(lambda e: None)
    t = threading.Thread(target=lambda: d.send_turn("x"))
    t.start()
    assert hands.entered.wait(5)
    d.close()                      # returns only after the turn's own return and the release
    t.join(5)
    assert hands.log.index("turn-end") < hands.log.index("close") and hands.log.count("close") == 1
    assert d.state == "closed" and d.native is None


def test_a_turn_cannot_start_while_closing_or_overlap_another(tmp_path):
    hands = _Hands()
    d = OpenHandsDriver(tmp_path, lambda p, on_event: hands)
    d.open(lambda e: None)
    t = threading.Thread(target=lambda: d.send_turn("x"))
    t.start()
    assert hands.entered.wait(5)
    with pytest.raises(RuntimeError, match="already running"):
        d.send_turn("y")
    d.close()
    t.join(5)
    with pytest.raises(RuntimeError, match="not open"):
        d.send_turn("z")


def test_close_racing_open_leaves_no_published_session(tmp_path):
    gate, built = threading.Event(), threading.Event()
    sess = _Hands()

    def opener(path, on_event):
        built.set()
        assert gate.wait(5)
        return sess

    d = OpenHandsDriver(tmp_path, opener)
    errors: list[BaseException] = []

    def _open():
        try:
            d.open(lambda e: None)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    o = threading.Thread(target=_open)
    o.start()
    assert built.wait(5)
    c = threading.Thread(target=d.close)
    c.start()
    time.sleep(0.05)
    assert c.is_alive() and "close" not in sess.log      # close waits for the opener; nothing released yet
    gate.set()
    o.join(5), c.join(5)
    assert not c.is_alive() and sess.log == ["close"]    # released exactly once, by the opener that lost
    assert errors and "closed while it opened" in str(errors[0])
    assert d.native is None and d.state == "closed"


def test_a_driver_opens_once_and_a_failed_opener_leaves_it_closed(tmp_path):
    d = OpenHandsDriver(tmp_path, lambda p, on_event: _Hands())
    d.open(lambda e: None)
    with pytest.raises(RuntimeError, match="opens once"):
        d.open(lambda e: None)
    d.close()
    with pytest.raises(RuntimeError, match="opens once"):
        d.open(lambda e: None)

    def boom(p, on_event):
        raise ValueError("no hands")

    f = OpenHandsDriver(tmp_path, boom)
    with pytest.raises(ValueError):
        f.open(lambda e: None)
    f.close()
    assert f.state == "closed"


def test_the_outcome_is_read_once_and_the_snapshot_is_what_gets_recorded():
    reads: list[str] = []

    class Flip:
        reply, tool_activity, error, nudged, timed_out, ok, exit_code = "r", [], None, False, False, False, 0
        held_digest = "d1"
        pending = (_HELD,)

        @property
        def gated(self):
            reads.append("gated")
            return len(reads) == 1          # True on the first read, False on any later one

    snap = read_outcome(Flip())
    assert snap.gated is True and reads == ["gated"]       # one read; the check and the record agree
    assert snap.pending[0].tool_name == "terminal" and snap.held_digest == "d1"
    with pytest.raises(Exception):
        snap.gated = False                                  # type: ignore[misc]  # frozen


@pytest.mark.parametrize("bad, needle", [
    (dict(tool_activity="ls"), "tool_activity"),                       # a str is not split into characters
    (dict(tool_activity=[1]), "tool_activity"),
    (dict(tool_activity={"ls"}), "tool_activity"),
    (dict(gated=1), "`gated` is not a bool"),
    (dict(nudged="no"), "`nudged` is not a bool"),
    (dict(reply=b"bytes"), "`reply` is neither text"),
    (dict(held_digest=7), "`held_digest` is neither text"),
    (dict(pending="terminal"), "`pending` is not a list"),
])
def test_outcome_value_shapes_are_checked_never_coerced(bad, needle):
    """complement r2 LOW: the snapshot coerced (`tuple("ls")`, `bool(1)`); a shape the contract does not name
    is refused, so what is recorded is what the harness said."""
    with pytest.raises(DriverContractError, match=needle):
        read_outcome(_Out(**bad))

    class Meth(_Out):
        def ok(self):                                      # a method where a value was meant
            return True

    with pytest.raises(DriverContractError, match="`ok` is not a bool"):
        read_outcome(Meth())

    class BoolCode(_Out):
        exit_code = True                                   # type: ignore[assignment]

    with pytest.raises(DriverContractError, match="exit_code"):
        read_outcome(BoolCode())


def test_a_held_row_must_name_its_tool_even_on_a_failed_outcome():
    """complement r2 LOW: an errored halt skipped the tool-name check. The pairing is not asked of a failed
    outcome, but every row it carries is built as a PendingApproval, which refuses a nameless one."""
    nameless = PendingEfferent("", "x", "r", full="{}")
    for out in (_Out(reply=None, gated=True, pending=(nameless,), held_digest="d"),
                _Out(reply=None, gated=True, pending=(nameless,), error="the refusal did not take")):
        with pytest.raises(DriverContractError, match="names no tool"):
            read_outcome(out)
    with pytest.raises(DriverContractError, match="names no tool"):
        PendingApproval(tool_name="", detail="", full="", reason="", recognized=False)


def test_the_host_ignores_a_driver_event_that_is_not_a_driver_event(tmp_path):
    d = _Fake([_Out(reply="ok")])
    host = _host(tmp_path, {"alpha": d})
    sid, _ = _open(host, "alpha")
    d.sink("a plain string, not a DriverEvent")            # type: ignore[misc]
    st = _wait(host, host.turn(sid, "go")["job_id"])
    assert st["status"] == "done"


def test_a_pending_row_attribute_is_read_once_and_validated_from_that_read():
    class Shifty:
        detail, reason, recognized = "d", "r", True

        def __init__(self):
            self.names = self.fulls = 0

        @property
        def tool_name(self):
            self.names += 1
            return "terminal" if self.names == 1 else 12345

        @property
        def full(self):
            self.fulls += 1
            return "{}" if self.fulls == 1 else ["not", "a", "str"]

    snap = read_outcome(_Out(reply=None, gated=True, pending=(Shifty(),), held_digest="d"))
    assert snap.pending[0].tool_name == "terminal" and snap.pending[0].full == "{}"


def test_a_result_that_cannot_report_gated_still_releases_the_turn_and_close_returns(tmp_path):
    class Odd:
        @property
        def gated(self):
            raise ValueError("boom")

    class Sess:
        def run_turn(self, m):
            return Odd()

        def request_stop(self):
            pass

        def close(self):
            self.closed = True

    d = OpenHandsDriver(tmp_path, lambda p, on_event: Sess())
    d.open(lambda e: None)
    with pytest.raises(ValueError):
        d.send_turn("x")                        # the host's job catches this as a broken turn
    assert d.state == "awaiting_approval"        # unreadable reads as held: fail closed
    t = threading.Thread(target=d.close)
    t.start()
    t.join(5)
    assert not t.is_alive() and d.state == "closed"


def test_a_turn_is_refused_and_state_reads_active_while_close_waits_for_it(tmp_path):
    hands = _Hands()
    d = OpenHandsDriver(tmp_path, lambda p, on_event: hands)
    d.open(lambda e: None)
    t = threading.Thread(target=lambda: d.send_turn("x"))
    t.start()
    assert hands.entered.wait(5)
    hands.stop.clear()
    orig = hands.request_stop
    hands.request_stop = lambda: hands.log.append("stop-held")   # the turn ignores stop until released
    c = threading.Thread(target=d.close)
    c.start()
    deadline = time.monotonic() + 5
    while "stop-held" not in hands.log and time.monotonic() < deadline:
        time.sleep(0.01)
    assert d.state == "active"
    with pytest.raises(RuntimeError, match="not open"):
        d.send_turn("y")
    orig()
    c.join(5), t.join(5)
    assert d.state == "closed"


# -- 1007+5 lane B: the snapshot is built inside the turn; close always ends closed and is bounded -------------


def test_a_close_landing_after_the_turn_returned_does_not_void_its_outcome(tmp_path):
    """L3 r2 (complement, codex, gemini): the host read `driver.state` AFTER the turn returned, so a close
    landing in that window turned a valid outcome into a contract error. The snapshot is the record now."""
    class Sess:
        def run_turn(self, m):
            return _Out(reply="a real answer")

        def request_stop(self):
            pass

        def close(self):
            pass

    class ClosedRightAfter(OpenHandsDriver):
        def send_turn(self, message, *, options=None):
            snap = super().send_turn(message, options=options)
            self.close()                    # lands between the turn's return and the host's record
            return snap

    host = ChatHost({"alpha": tmp_path}, driver_factory=lambda n, p: ClosedRightAfter(p, lambda d, on_event: Sess()))
    sid, _ = _open(host, "alpha")
    st = _wait(host, host.turn(sid, "go")["job_id"])
    assert st["status"] == "done" and st["result"]["reply"] == "a real answer"


def test_gated_is_read_once_from_the_harness_result_end_to_end(tmp_path):
    """L3 r2 (codex MED): `_run` read `result.gated` and the host's snapshot read it again."""
    reads: list[int] = []

    class Counted:
        reply, tool_activity, error, nudged, timed_out, pending, held_digest = "hi", [], None, False, False, (), None
        ok, exit_code = True, 0

        @property
        def gated(self):
            reads.append(1)
            return False

    class Sess:
        def __init__(self, on_event):
            pass

        def run_turn(self, m):
            return Counted()

        def request_stop(self):
            pass

        def close(self):
            pass

    host = ChatHost({"alpha": tmp_path}, session_factory=lambda d, on_event: Sess(on_event))
    sid, _ = _open(host, "alpha")
    assert _wait(host, host.turn(sid, "go")["job_id"])["result"]["reply"] == "hi"
    assert len(reads) == 1


def test_a_base_exception_reading_the_result_still_releases_the_turn(tmp_path):
    """L3 r2 (codex HIGH): a BaseException (the deadline's TurnTimeout) out of reading `gated` skipped the guard
    release, so the driver read `active` forever and close() never returned."""
    from levain.firing.deadline import TurnTimeout

    class Odd:
        reply, tool_activity, error, nudged, timed_out, pending, held_digest = "r", [], None, False, False, (), None
        ok, exit_code = True, 0

        @property
        def gated(self):
            raise TurnTimeout(0.05)       # the bound fired while the result was read

    class Sess:
        closed = False

        def run_turn(self, m):
            return Odd()

        def request_stop(self):
            pass

        def close(self):
            self.closed = True

    sess = Sess()
    d = OpenHandsDriver(tmp_path, lambda p, on_event: sess, close_wait=5)
    d.open(lambda e: None)
    with pytest.raises(TurnTimeout):
        d.send_turn("x")
    assert d.state == "awaiting_approval"           # released, and unknown reads as held
    t = threading.Thread(target=d.close, daemon=True)
    t.start()
    t.join(2)
    assert not t.is_alive() and sess.closed and d.state == "closed"


def test_a_failing_stop_request_never_releases_under_the_turn_and_close_still_ends_closed(tmp_path):
    """L3 r2 (codex HIGH): a raise out of `request_stop` in close() left the phase `closing` forever and the
    session never released. The stop requests now run on their own thread; a failing one is logged and
    retried, close() returns at its bound, and the running turn releases the session on its own return."""
    hands = _Hands()

    def stop():
        hands.log.append("stop-raised")
        raise OSError("the stop request failed")

    hands.request_stop = stop                          # type: ignore[method-assign]
    d = OpenHandsDriver(tmp_path, lambda p, on_event: hands, close_wait=0.3)
    d.open(lambda e: None)
    t = threading.Thread(target=lambda: d.send_turn("x"), daemon=True)
    t.start()
    assert hands.entered.wait(5)
    d.close()
    assert "stop-raised" in hands.log and d.state == "active" and "close" not in hands.log
    with pytest.raises(RuntimeError, match="not open"):
        d.send_turn("y")
    hands.stop.set()                                   # the turn returns on its own
    t.join(5)
    assert hands.log.count("close") == 1 and hands.log.index("turn-end") < hands.log.index("close")
    assert d.state == "closed" and d.native is None
    d.close()                                          # idempotent after the hand-off
    assert hands.log.count("close") == 1


def test_a_blocking_stop_request_does_not_hold_close_past_its_bound(tmp_path):
    """L1 (80d2fc4): close() called `request_stop` inline, which may block (the SDK's pause waits for a step's
    state lock), so close_wait bounded nothing while it did. RAN: a 5 s stop request held close() 5 s."""
    hands = _Hands()
    release = threading.Event()

    def stop():
        hands.log.append("stop-blocking")
        release.wait(5)
        hands.stop.set()

    hands.request_stop = stop                          # type: ignore[method-assign]
    d = OpenHandsDriver(tmp_path, lambda p, on_event: hands, close_wait=0.2)
    d.open(lambda e: None)
    t = threading.Thread(target=lambda: d.send_turn("x"), daemon=True)
    t.start()
    assert hands.entered.wait(5)
    started = time.monotonic()
    d.close()
    assert time.monotonic() - started < 1.5 and "close" not in hands.log
    release.set()
    t.join(5)
    assert hands.log.count("close") == 1 and d.state == "closed"


def test_a_release_that_fails_after_the_turn_does_not_replace_its_outcome(tmp_path):
    """L1 + L2 (80d2fc4): when close() handed the release to the turn and the session's close raised, that
    error replaced the turn's snapshot: a valid outcome turned into an exception, r2's class on the release side."""
    hands = _Hands()
    hands.request_stop = lambda: None                  # type: ignore[method-assign]

    def bad_close():
        hands.log.append("close")
        raise OSError("close failed")

    hands.close = bad_close                            # type: ignore[method-assign]
    d = OpenHandsDriver(tmp_path, lambda p, on_event: hands, close_wait=0.2)
    d.open(lambda e: None)
    got: list[Any] = []
    t = threading.Thread(target=lambda: got.append(d.send_turn("x")), daemon=True)
    t.start()
    assert hands.entered.wait(5)
    d.close()                                          # gives up waiting; the turn owns the release
    hands.stop.set()
    t.join(5)
    assert got and isinstance(got[0], TurnSnapshot) and got[0].reply == "stopped"
    assert hands.log.count("close") == 1 and d.state == "closed"


def test_a_held_row_missing_its_explanation_is_refused():
    """L1 (80d2fc4): a missing `reason` or `detail` read as "", a consent row with a blank explanation."""
    class Row:
        tool_name, full = "terminal", "{}"

    for present in ({"reason": "r"}, {"detail": "d"}):
        row = Row()
        for k, v in present.items():
            setattr(row, k, v)
        with pytest.raises(DriverContractError, match="does not carry"):
            read_outcome(_Out(reply=None, gated=True, pending=(row,), held_digest="d"))
    row = Row()
    row.detail, row.reason = "d", "r"                  # type: ignore[attr-defined]
    snap = read_outcome(_Out(reply=None, gated=True, pending=(row,), held_digest="d"))
    assert snap.pending[0].recognized is False         # a missing `recognized` fails closed


def test_close_is_bounded_and_a_turn_outliving_it_releases_on_its_return(tmp_path):
    """complement r2 LOW: close() waited without bound on a turn that ignores stop requests."""
    hands = _Hands()
    hands.request_stop = lambda: hands.log.append("stop-ignored")   # type: ignore[method-assign]
    d = OpenHandsDriver(tmp_path, lambda p, on_event: hands, close_wait=0.3)
    d.open(lambda e: None)
    t = threading.Thread(target=lambda: d.send_turn("x"), daemon=True)
    t.start()
    assert hands.entered.wait(5)
    started = time.monotonic()
    c = threading.Thread(target=d.close, daemon=True)
    c.start()
    c.join(3)
    assert not c.is_alive() and time.monotonic() - started < 3
    assert "stop-ignored" in hands.log and "close" not in hands.log and d.state == "active"
    second = threading.Thread(target=d.close, daemon=True)  # a second closer is bounded too
    second.start()
    second.join(3)
    assert not second.is_alive()
    hands.stop.set()
    t.join(5)
    assert hands.log.count("close") == 1 and d.state == "closed"


@pytest.mark.parametrize("wait", [0, -1, float("inf"), float("nan"), "30", True])
def test_close_wait_must_be_a_finite_bound(tmp_path, wait):
    with pytest.raises(ValueError, match="bounded"):
        OpenHandsDriver(tmp_path, lambda p, on_event: None, close_wait=wait)


# -- the life cycle holds whatever raises wherever: an asynchronous BaseException at every line --------------


class _Injected(BaseException):
    pass


def _body_lines(func) -> list[int]:
    """The line numbers of ``func``'s body before its last ``finally:`` (an exception landing at the first
    instruction of a finally cannot be closed by any structure, so the property is asked of the body)."""
    import inspect

    src, start = inspect.getsourcelines(func)
    cut = max((i for i, line in enumerate(src) if line.strip() == "finally:"), default=len(src))
    return [start + i for i in range(1, cut)]


def _raise_at(func, lineno: int, fired: list[int]):
    """A trace function raising _Injected once, when ``func``'s frame is about to run ``lineno``."""
    def local(frame, event, arg):
        if event == "line" and frame.f_lineno == lineno and not fired:
            fired.append(lineno)
            raise _Injected()
        return local

    def tracer(frame, event, arg):
        return local if frame.f_code is func.__code__ else None

    return tracer


def _traced(func, lineno: int, call) -> bool:
    import sys

    fired: list[int] = []
    sys.settrace(_raise_at(func, lineno, fired))
    try:
        call()
    except _Injected:
        pass
    finally:
        sys.settrace(None)
    return bool(fired)


class _Counted:
    def __init__(self):
        self.closes = 0
        self.stop = threading.Event()
        self.entered = threading.Event()

    def run_turn(self, m):
        self.entered.set()
        assert self.stop.wait(5)
        return _Out(reply="r")

    def request_stop(self):
        self.stop.set()

    def close(self):
        self.closes += 1


def _closes_promptly(d) -> bool:
    t = threading.Thread(target=d.close, daemon=True)
    t.start()
    t.join(3)
    return not t.is_alive()


@pytest.mark.parametrize("lineno", _body_lines(OpenHandsDriver._run))
def test_a_raise_anywhere_in_a_turn_never_leaves_the_guard_set(tmp_path, lineno):
    """L2 (80d2fc4, repro F): an asynchronous BaseException between `_running = True` and `try:` left the
    driver `active` forever and close() handed the release to a turn that did not exist."""
    s = _Counted()
    s.stop.set()
    d = OpenHandsDriver(tmp_path, lambda p, on_event: s, close_wait=0.5)
    d.open(lambda e: None)
    if not _traced(OpenHandsDriver._run, lineno, lambda: d.send_turn("x")):
        pytest.skip("line not executed on this path")
    assert d.state != "active"
    assert _closes_promptly(d) and s.closes == 1 and d.state == "closed"


@pytest.mark.parametrize("busy", [False, True])
@pytest.mark.parametrize("lineno", _body_lines(OpenHandsDriver.close))
def test_a_raise_anywhere_in_close_still_releases_exactly_once(tmp_path, lineno, busy):
    """L2 (80d2fc4): close() set `closing` before its try, so a raise in between left the phase there with no
    one to release; a later close() then waited on a release that never came."""
    s = _Counted()
    d = OpenHandsDriver(tmp_path, lambda p, on_event: s, close_wait=0.5)
    d.open(lambda e: None)
    t = None
    if busy:
        t = threading.Thread(target=lambda: d.send_turn("x"), daemon=True)
        t.start()
        assert s.entered.wait(5)
    else:
        s.stop.set()
    fired = _traced(OpenHandsDriver.close, lineno, d.close)
    s.stop.set()
    if t is not None:
        t.join(5)
    if not fired:
        pytest.skip("line not executed on this path")
    assert _closes_promptly(d) and s.closes == 1 and d.state == "closed"


@pytest.mark.parametrize("lineno", _body_lines(OpenHandsDriver.open))
def test_a_raise_anywhere_in_open_leaves_the_driver_closed_and_nothing_leaked(tmp_path, lineno):
    """L2 (80d2fc4): a raise between the opener's return and the publish leaked the built session and left the
    phase at `opening`; a close() then waited on an opener that had already gone."""
    built: list[_Counted] = []

    def opener(p, on_event):
        built.append(_Counted())
        return built[-1]

    d = OpenHandsDriver(tmp_path, opener, close_wait=0.5)
    if not _traced(OpenHandsDriver.open, lineno, lambda: d.open(lambda e: None)):
        pytest.skip("line not executed on this path")
    assert _closes_promptly(d) and d.state == "closed"
    assert all(b.closes == 1 for b in built)

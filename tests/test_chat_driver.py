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
        self.on_released: Callable[[Any], None] | None = None
        self.reported = False

    def open(self, on_event, *, on_released, resume=None):
        self.sink, self.opened, self._state = on_event, True, "idle"
        self.on_released = on_released

    def close(self):
        self.closed, self._state = True, "closed"
        self.report()

    def report(self, error=None):
        """Tell the host the release is done (or failed): the push half of the contract."""
        if self.on_released is not None and not self.reported:
            self.reported = True
            self.on_released(error)

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


def _unheard(error):
    """An on_released for a driver driven directly (no host listening)."""


def _until(pred, timeout: float = 5.0, what: str = "") -> None:
    end = time.monotonic() + timeout
    while not pred():
        if time.monotonic() > end:
            raise AssertionError(f"timed out waiting for {what}")
        time.sleep(0.01)


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
            return read_outcome(_Out(reply=None, timed_out=True, error="stopped at its bound"))

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
    _until(lambda: host.session_status(sid)["state"] == "broken" and d.closed, what="the release")


def test_a_halt_whose_digest_is_missing_can_only_be_rejected(tmp_path):
    """Fail-closed, not a violation: no digest means nothing for an approval to bind to."""
    d = _Fake([_Out(reply=None, gated=True, pending=(_HELD,), held_digest=None)])
    d.held_digest = lambda: None   # type: ignore[method-assign]
    host = _host(tmp_path, {"alpha": d})
    sid, _ = _open(host, "alpha")
    out = _wait(host, host.turn(sid, "go")["job_id"])["result"]
    assert out["approvable"] is False          # offered as reject-only from the start
    with pytest.raises(ChatError) as e:
        host.approve(sid, out["decision_id"])
    assert e.value.code == "undecidable"
    assert not [c for c in d.calls if c[0] == "approve"]


def test_a_driver_that_needs_in_turn_consent_is_refused_at_open_not_driven(tmp_path):
    """S10 is not built: a driver whose consent request arrives while the turn is running would be
    driven through a path that cannot carry it. Refused before it is opened."""
    d = _Fake([], timing="in_turn")
    host = _host(tmp_path, {"alpha": d})
    sid, st = _open(host, "alpha")
    assert st["status"] == "failed" and "approval state machine" in st["error"]
    _until(lambda: not d.opened and host.session_status(sid)["state"] == "failed", what="the release")


def test_a_failed_outcome_is_not_second_guessed(tmp_path):
    d = _Fake([_Out(reply=None, error="boom", gated=True)])
    host = _host(tmp_path, {"alpha": d})
    sid, _ = _open(host, "alpha")
    st = _wait(host, host.turn(sid, "go")["job_id"])
    _until(lambda: st["result"]["error"] == "boom" and host.session_status(sid)["state"] == "broken", what="the release")


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
        oh.open(lambda e: None, on_released=_unheard, resume="abc")      # nothing is persisted, so nothing resumes


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
    d.open(seen.append, on_released=_unheard)
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
    assert st["status"] == "failed" and "not TurnSnapshot" in st["error"] and d.closed


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
        def open(self, on_event, *, on_released, resume=None):
            self.opened, self.on_released = True, on_released
            raise RuntimeError("half built")

    d = Leaky([])
    host = _host(tmp_path, {"alpha": d})
    sid, st = _open(host, "alpha")
    assert st["status"] == "failed" and st["error"] == "half built"
    _until(lambda: d.closed and host.session_status(sid)["state"] == "failed", what="the release")


def test_the_openhands_driver_is_not_driveable_when_closed_and_keeps_an_errored_halt_held(tmp_path):
    class Sess:
        def run_turn(self, m):
            return _Out(reply=None, gated=True, error="refusal did not take")

        def close(self):
            pass

    d = OpenHandsDriver(tmp_path, lambda p, on_event: Sess())
    with pytest.raises(RuntimeError):
        d.send_turn("x")                     # never opened
    d.open(lambda e: None, on_released=_unheard)
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
    d.open(lambda e: None, on_released=_unheard)
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
    d.open(lambda e: None, on_released=_unheard)
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
            d.open(lambda e: None, on_released=_unheard)
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
    d.open(lambda e: None, on_released=_unheard)
    with pytest.raises(RuntimeError, match="opens once"):
        d.open(lambda e: None, on_released=_unheard)
    d.close()
    with pytest.raises(RuntimeError, match="opens once"):
        d.open(lambda e: None, on_released=_unheard)

    def boom(p, on_event):
        raise ValueError("no hands")

    f = OpenHandsDriver(tmp_path, boom)
    with pytest.raises(ValueError):
        f.open(lambda e: None, on_released=_unheard)
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
    (dict(pending="terminal"), "`pending` is not a sequence"),
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
    d.open(lambda e: None, on_released=_unheard)
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
    d.open(lambda e: None, on_released=_unheard)
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
    d.open(lambda e: None, on_released=_unheard)
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
    d.open(lambda e: None, on_released=_unheard)
    t = threading.Thread(target=lambda: d.send_turn("x"), daemon=True)
    t.start()
    assert hands.entered.wait(5)
    d.close()
    assert "stop-raised" in hands.log and d.state == "active" and "close" not in hands.log
    with pytest.raises(RuntimeError, match="not open"):
        d.send_turn("y")
    hands.stop.set()                                   # the turn returns on its own
    t.join(5)
    assert d.wait_closed(5)
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
    d.open(lambda e: None, on_released=_unheard)
    t = threading.Thread(target=lambda: d.send_turn("x"), daemon=True)
    t.start()
    assert hands.entered.wait(5)
    started = time.monotonic()
    d.close()
    assert time.monotonic() - started < 1.5 and "close" not in hands.log
    release.set()
    t.join(5)
    assert d.wait_closed(5)
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
    d.open(lambda e: None, on_released=_unheard)
    got: list[Any] = []
    t = threading.Thread(target=lambda: got.append(d.send_turn("x")), daemon=True)
    t.start()
    assert hands.entered.wait(5)
    d.close()                                          # gives up waiting; the turn owns the release
    hands.stop.set()
    t.join(5)
    assert got and isinstance(got[0], TurnSnapshot) and got[0].reply == "stopped"
    end = time.monotonic() + 5
    while d.release_error() is None and time.monotonic() < end:
        time.sleep(0.01)
    # the failed release is reported where it belongs, and is not a release (2026-10-07 ruling)
    assert "close failed" in d.release_error() and not d.wait_closed(0)
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
    d.open(lambda e: None, on_released=_unheard)
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
    assert d.wait_closed(5)
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
    d.open(lambda e: None, on_released=_unheard)
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
    d.open(lambda e: None, on_released=_unheard)
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
    if not _traced(OpenHandsDriver.open, lineno, lambda: d.open(lambda e: None, on_released=_unheard)):
        pytest.skip("line not executed on this path")
    assert _closes_promptly(d) and d.state == "closed"
    assert all(b.closes == 1 for b in built)


# -- L3 r1 (input 6c3d158e56b2f130) --------------------------------------------------------------------------


def test_a_blank_digest_cannot_bind_an_approval(tmp_path):
    """codex HIGH: a driver reporting held_digest "" for hold A and "" again from held_digest() after the hold
    became B passed the binding check, so the approve ran B unseen. A blank digest names nothing: refused."""
    class Blank(_Fake):
        def held_digest(self):
            return ""

    d = Blank([_Out(reply=None, gated=True, pending=(_HELD,), held_digest=""), _Out(reply="ran B")])
    host = _host(tmp_path, {"alpha": d})
    sid, _ = _open(host, "alpha")
    st = _wait(host, host.turn(sid, "go")["job_id"])
    assert st["status"] == "failed" and "blank" in st["error"]
    _until(lambda: not [c for c in d.calls if c[0] == "approve"] and host.session_status(sid)["state"] == "broken", what="the release")


def test_a_timed_out_outcome_without_an_error_breaks_the_session(tmp_path):
    """codex MED: timed_out=True with error=None was recorded as an ordinary turn and the session went back to
    idle, though a stopped turn did not complete and the module says it breaks the session."""
    d = _Fake([_Out(reply=None, timed_out=True)])
    host = _host(tmp_path, {"alpha": d})
    sid, _ = _open(host, "alpha")
    st = _wait(host, host.turn(sid, "go")["job_id"])
    assert st["status"] == "failed" and "timed out but carries no error" in st["error"]
    _until(lambda: host.session_status(sid)["state"] == "broken" and d.closed, what="the release")


def test_a_whitespace_tool_name_is_no_tool_name():
    """gemini LOW: "   " passed the tool-name check and showed an invisible tool on the consent row."""
    with pytest.raises(DriverContractError, match="names no tool"):
        read_outcome(_Out(reply=None, gated=True, held_digest="d",
                          pending=(PendingEfferent("   ", "x", "r", full="{}"),)))


def test_a_blocking_native_close_does_not_hold_close_and_the_slot_stays_counted(tmp_path):
    """codex HIGH + complement LOW: close_wait bounded only the wait for a turn; a native close that blocks held
    close() (and the host's caller) forever. And a release still in flight must keep its cap slot."""
    gate = threading.Event()

    class Sess:
        def __init__(self, on_event=None):
            self.closes = 0

        def run_turn(self, m):
            return _Out(reply="hi")

        def request_stop(self):
            pass

        def close(self):
            self.closes += 1
            assert gate.wait(10)

    s = Sess()
    d = OpenHandsDriver(tmp_path, lambda p, on_event: s, close_wait=0.3)
    d.open(lambda e: None, on_released=_unheard)
    started = time.monotonic()
    d.close()
    assert time.monotonic() - started < 2 and not d.wait_closed(0)
    gate.set()
    assert d.wait_closed(5) and s.closes == 1 and d.state == "closed"

    gate.clear()
    made: list[Sess] = []

    def opener(p, on_event):
        made.append(Sess())
        return made[-1]

    host = ChatHost({"alpha": tmp_path / "a", "beta": tmp_path / "b"}, max_sessions=1,
                    driver_factory=lambda n, p: OpenHandsDriver(p, opener, close_wait=0.3))
    sid, _ = _open(host, "alpha")
    started = time.monotonic()
    assert host.close(sid)["state"] == "closing"          # returned at the bound, still releasing
    assert time.monotonic() - started < 2
    with pytest.raises(ChatError) as e:
        host.open("beta")                                  # the slot is still counted
    assert e.value.code == "too_many_sessions"
    gate.set()
    end = time.monotonic() + 5
    while host.session_status(sid)["state"] != "closed" and time.monotonic() < end:
        time.sleep(0.02)
    _until(lambda: host.session_status(sid)["state"] == "closed", what="the release")
    _open(host, "beta")                                    # and comes back once released
    host.shutdown()


def test_a_broken_turn_whose_teardown_raises_still_publishes_its_result(tmp_path):
    """complement LOW: `dead.close()` raising skipped the publish, so the turn's own result (its error) was
    replaced by the teardown's exception text."""
    class Raising(_Fake):
        def close(self):
            self.closed = True
            raise RuntimeError("teardown failed")

    d = Raising([_Out(reply=None, error="boom")])
    host = _host(tmp_path, {"alpha": d})
    sid, _ = _open(host, "alpha")
    st = _wait(host, host.turn(sid, "go")["job_id"])
    assert st["status"] == "done" and st["result"]["error"] == "boom"
    # a close that raised is not a release (ruled 2026-10-07): counted, and it says why
    view = host.session_status(sid)
    assert view["state"] == "release_failed" and "teardown failed" in view["error"]


# -- L1 on 3331e91..3328b56 -----------------------------------------------------------------------------------


class _Trigger:
    """`done.set()` for a fake whose release ends later: it REPORTS the release (the push contract)."""

    def __init__(self, report):
        self._report = report

    def set(self):
        self._report(None)


class _LateRelease(_Fake):
    """A driver whose close() returns before everything is released; it reports when `done` is set."""

    def __init__(self, script=None):
        super().__init__(script or [])
        self.done = _Trigger(self.report)

    def close(self):
        self.closed, self._state = True, "closed"        # no report yet


def test_a_driver_that_never_reports_is_release_failed_at_the_deadline(tmp_path, monkeypatch):
    """Ruling 2026-10-07 (B): the host never asks a driver whether it released; a driver that has not
    REPORTED by the deadline reads release_failed, still counted, and nothing is polled meanwhile."""
    import levain.chat as chat

    monkeypatch.setattr(chat, "_REPORT_SECONDS", 0.3)

    class Asked(_LateRelease):
        asks = 0

        def wait_closed(self, timeout):
            Asked.asks += 1
            return False

        def release_error(self):
            Asked.asks += 1
            return None

    d = Asked()
    host = _host(tmp_path, {"alpha": d}, max_sessions=1)
    sid, _ = _open(host, "alpha")
    assert host.close(sid)["state"] == "closing"
    _until(lambda: host.session_status(sid)["state"] == "release_failed", what="the deadline")
    assert "did not report" in host.session_status(sid)["error"] and Asked.asks == 0
    with pytest.raises(ChatError) as e:
        host.open("alpha")
    assert e.value.code == "too_many_sessions"


def test_shutdown_closes_sessions_side_by_side(tmp_path):
    """L1 (RAN): shutdown closed one session after another, so N slow releases cost N close bounds."""
    gate = threading.Event()

    class Slow(_Fake):
        def close(self):
            self.closed = True
            gate.wait(0.5)          # a close that uses its whole bound

    drivers = {n: Slow([]) for n in ("a", "b", "c", "d")}
    host = _host(tmp_path, drivers)
    for n in drivers:
        _open(host, n)
    started = time.monotonic()
    host.shutdown()
    assert time.monotonic() - started < 1.5 and all(d.closed for d in drivers.values())


def test_a_settle_that_fails_after_a_late_release_still_ends_the_record(tmp_path):
    """L1: a settle run on the reaper thread is outside every worker's guard; one that raised left the job
    running and the record busy for good."""
    d = _LateRelease()
    host = _host(tmp_path, {"alpha": d})
    sid, _ = _open(host, "alpha")
    rec = host._sessions[sid]
    with host._lock:
        job = host._new_job(rec, "turn")
        rec.state = "busy"

    def settle():
        raise RuntimeError("publish failed")

    host._close_then(rec, rec.driver, settle)
    d.done.set()
    end = time.monotonic() + 5
    while host.session_status(sid)["state"] == "busy" and time.monotonic() < end:
        time.sleep(0.02)
    _until(lambda: host.session_status(sid)["state"] == "broken" and host.job_status(job.job_id)["status"] == "failed", what="the release")


# -- L3 r2 (input 74542a05e53135bf) and the 2026-10-07 ruling on a failed release ------------------------------


class _Forcing(_Fake):
    """A driver whose release fails, and that has a force_release a host could be tempted to call."""

    def __init__(self):
        super().__init__([])
        self.forced = 0

    def close(self):
        self.closed = True
        self.report("OSError: the shell would not stop")

    def force_release(self, native=None):
        self.forced += 1


def test_a_failed_release_stays_counted_and_the_host_never_forces_it(tmp_path):
    """Ruling 2026-10-07 (codex r2 HIGH; r3 ruling (b), complement r3 MED): a release that failed is not a
    release, and the host never calls force_release (a blocking one would hold the reaper every session
    shares); an escalation is the driver's own, on its release worker."""
    d = _Forcing()
    host = _host(tmp_path, {"alpha": d}, max_sessions=1)
    sid, _ = _open(host, "alpha")
    view = host.close(sid)
    assert d.forced == 0
    assert view["state"] == "release_failed" and "would not stop" in view["error"]
    assert "release_failed_since" in view
    with pytest.raises(ChatError) as e:
        host.open("alpha")
    assert e.value.code == "too_many_sessions"


def test_the_host_never_polls_a_driver_for_its_release(tmp_path):
    """Ruling 2026-10-07 (B): the polling is deleted, not bounded. A driver whose probes would block forever
    is never asked, and its report still settles the record."""
    class Never(_LateRelease):
        def wait_closed(self, timeout):
            raise AssertionError("the host polled wait_closed")

        def release_error(self):
            raise AssertionError("the host polled release_error")

    d = Never()
    host = _host(tmp_path, {"alpha": d})
    sid, _ = _open(host, "alpha")
    host.close(sid)
    time.sleep(0.3)
    assert host.session_status(sid)["state"] == "closing"
    d.done.set()
    _until(lambda: host.session_status(sid)["state"] == "closed", what="the report")


def test_a_drivers_banner_cannot_overwrite_the_hosts_fields(tmp_path):
    """codex r2 MED: `**rec.info` came last, so a describe() key `state` or `session_id` replaced the host's."""
    class Loud(_Fake):
        def describe(self):
            return {"label": "fake", "state": "closed", "session_id": "someone-else", "error": "none"}

    host = _host(tmp_path, {"alpha": Loud([])})
    sid, _ = _open(host, "alpha")
    view = host.session_status(sid)
    assert view["state"] == "idle" and view["session_id"] == sid and "error" not in view
    assert view["label"] == "fake"


def test_any_sequence_the_protocol_declares_is_read():
    """codex r2 MED: TurnOutcome declares Sequence, but only list/tuple were read."""
    from collections import UserList

    snap = read_outcome(_Out(tool_activity=UserList(["⚙ terminal: ls"])))
    assert snap.tool_activity == ("⚙ terminal: ls",)


def test_no_teardown_needs_a_new_thread(tmp_path, monkeypatch):
    """codex r2 MED + complement MED: a release thread created AT teardown could fail to start, and the
    native close then ran inline, unbounded. The release worker is acquired at open."""
    gate = threading.Event()

    class Sess:
        def run_turn(self, m):
            return _Out()

        def request_stop(self):
            pass

        def close(self):
            gate.wait(10)

    d = OpenHandsDriver(tmp_path, lambda p, on_event: Sess(), close_wait=0.3)
    d.open(lambda e: None, on_released=_unheard)

    def no_threads(self):
        raise RuntimeError("can't start new thread")

    monkeypatch.setattr(threading.Thread, "start", no_threads)
    started = time.monotonic()
    d.close()
    assert time.monotonic() - started < 2
    monkeypatch.undo()
    gate.set()
    assert d.wait_closed(5)


def test_a_broken_turns_outcome_is_published_before_its_release_ends(tmp_path):
    """glm r2: the job stayed running until the teardown finished; a client polling it waited on the
    release. The outcome is published at once; the record stays counted until the release is confirmed."""
    class Slow(_LateRelease):
        pass

    d = Slow([_Out(reply=None, error="boom")])
    host = _host(tmp_path, {"alpha": d}, max_sessions=1)
    sid, _ = _open(host, "alpha")
    st = _wait(host, host.turn(sid, "go")["job_id"])
    assert st["status"] == "done" and st["result"]["error"] == "boom"
    assert host.session_status(sid)["state"] == "busy"           # still counted while it releases
    d.done.set()
    end = time.monotonic() + 5
    while host.session_status(sid)["state"] != "broken" and time.monotonic() < end:
        time.sleep(0.02)
    _until(lambda: host.session_status(sid)["state"] == "broken", what="the release")


def test_a_host_with_its_own_drivers_builds_no_openhands_opener(tmp_path, monkeypatch):
    """complement r2 LOW: the default opener was built even when a driver_factory replaced it."""
    import levain.chat as chat

    def boom(**kw):
        raise AssertionError("the OpenHands opener was built")

    monkeypatch.setattr(chat, "_default_factory", boom)
    ChatHost({"alpha": tmp_path}, driver_factory=lambda n, p: _Fake([]))


def test_shutdown_is_bounded_even_by_a_driver_that_breaks_its_close_bound(tmp_path, monkeypatch):
    """complement r2 LOW: shutdown joined each close without a bound, trusting every driver's."""
    import levain.chat as chat

    monkeypatch.setattr(chat, "_SHUTDOWN_JOIN_SECONDS", 0.3)
    gate = threading.Event()

    class Stuck(_Fake):
        def close(self):
            gate.wait(10)

    host = _host(tmp_path, {"alpha": Stuck([])})
    sid, _ = _open(host, "alpha")
    started = time.monotonic()
    host.shutdown()
    assert time.monotonic() - started < 2 and host.session_status(sid)["state"] == "closing"
    gate.set()


# -- L3 r3 (input e37c5536d052c9b5) and the 2026-10-07 ruling: ONE boundary into driver code ----------------


class _Escape(BaseException):
    """Not an Exception: what a TurnTimeout, a KeyboardInterrupt or a SystemExit looks like to a catcher."""


@pytest.fixture
def escaped(monkeypatch):
    """Every exception that leaves a thread, of any class (the boundary's whole claim is that none does)."""
    seen: list[str] = []
    monkeypatch.setattr(threading, "excepthook", lambda args: seen.append(
        f"{args.thread.name if args.thread else '?'}: {args.exc_type.__name__}"))
    return seen


def _raising(method: str, base: type[_Fake] = _Fake, **kw):
    class Raises(base):  # type: ignore[valid-type,misc]
        pass

    def boom(self, *a, **k):
        raise _Escape(f"{method} escaped")

    setattr(Raises, method, boom)
    return Raises(**kw) if kw else Raises([])


def test_a_close_that_raises_anything_is_a_failed_release_and_nothing_escapes(tmp_path, escaped):
    """codex r3 HIGH (RAN), the close half: a BaseException from close() escaped and stranded the record.
    Through the boundary it ends release_failed, with the error's class, counted."""
    d = _raising("close")
    host = _host(tmp_path, {"alpha": d, "beta": _Fake([])}, max_sessions=2)
    sid, _ = _open(host, "alpha")
    host.close(sid)
    _until(lambda: host.session_status(sid)["state"] == "release_failed", what="the failed release")
    assert "_Escape" in host.session_status(sid)["error"]
    _open(host, "beta")                    # the host still works, and the failed slot is still counted
    with pytest.raises(ChatError) as e:
        host.open("beta")
    assert e.value.code == "too_many_sessions"
    assert escaped == []


def test_one_driver_that_never_reports_does_not_hold_up_the_others(tmp_path, monkeypatch, escaped):
    """codex r3 HIGH, the reaper half: one bad driver stranded every pending release. Now nothing is shared
    but the deadline clock: the silent one goes release_failed, the other settles on its report."""
    import levain.chat as chat

    monkeypatch.setattr(chat, "_REPORT_SECONDS", 0.3)
    bad, good = _LateRelease(), _LateRelease()
    host = _host(tmp_path, {"bad": bad, "good": good})
    s_bad, _ = _open(host, "bad")
    s_good, _ = _open(host, "good")
    host.close(s_bad)
    host.close(s_good)
    good.done.set()
    _until(lambda: host.session_status(s_good)["state"] == "closed", what="the good release")
    _until(lambda: host.session_status(s_bad)["state"] == "release_failed", what="the bad release")
    assert escaped == []


def test_nothing_describe_or_a_turn_raises_escapes(tmp_path, escaped):
    """The same boundary for the other calls: describe() at open, send_turn, held_digest at approve."""
    host = _host(tmp_path, {"a": _raising("describe"), "b": _raising("send_turn"), "c": _raising("held_digest")})
    out = host.open("a")
    st = _wait(host, out["job_id"])
    assert st["status"] == "failed" and "describe escaped" in st["error"]
    sid, _ = _open(host, "b")
    st = _wait(host, host.turn(sid, "go")["job_id"])
    assert st["status"] == "failed" and "_Escape" in st["error"]
    c = host._sessions[_open(host, "c")[0]]
    c.driver._driver.script.append(_halt())
    st = _wait(host, host.turn(c.session_id, "go")["job_id"])
    with pytest.raises(ChatError) as e:
        host.approve(c.session_id, expect=st["result"]["decision_id"])
    assert e.value.code == "stale_decision" and host.session_status(c.session_id)["state"] == "gated"
    assert escaped == []


def test_a_stop_request_that_raises_does_not_end_the_deadline_watcher(tmp_path, escaped):
    """request_stop through the boundary: a watcher whose interrupt raised a BaseException died, and the turn
    lost its bound. It keeps asking until the job ends."""
    gate = threading.Event()

    class Stuck(_Fake):
        stops = 0

        def send_turn(self, message):
            assert gate.wait(10)
            return read_outcome(_Out())

        def interrupt(self):
            Stuck.stops += 1
            if Stuck.stops >= 2:
                gate.set()
            raise _Escape("interrupt escaped")

    host = _host(tmp_path, {"alpha": Stuck([])}, turn_seconds=0.1)
    sid, _ = _open(host, "alpha")
    st = _wait(host, host.turn(sid, "go")["job_id"], timeout=10)
    assert st["deadline_hit"] and Stuck.stops >= 2
    assert escaped == []


def test_the_host_holds_no_driver_method():
    """The proof that no host call site bypasses the boundary: the proxy the host holds has none of the
    contract's methods, so a direct call is an AttributeError, not a raise that escapes."""
    from levain.chat import _DriverProxy

    contract = {n for n in dir(HarnessDriver) if not n.startswith("_")}
    exposed = {n for n in dir(_DriverProxy) if not n.startswith("_")}
    assert exposed == {"call", "read", "harness", "caps", "make", "submit_close", "retire", "release",
                       "early_report", "stop_idle"}
    assert exposed & contract == {"harness", "caps"}       # values, read once through the boundary
    proxy = _DriverProxy("t")
    assert proxy.make(lambda: _Fake([]), timeout=2).ok
    with pytest.raises(AttributeError):
        proxy.close  # noqa: B018
    proxy.retire()


def test_a_driver_that_does_not_answer_fails_closed_and_can_still_be_closed(tmp_path, monkeypatch):
    """L2 r4 + complement r4 (ruling: no host call waits unbounded on driver code). A held_digest that never
    returns: the approve is refused ("the driver did not answer"), nothing runs, and the session reads
    unresponsive, which close acts on (it is never treated as busy)."""
    import levain.chat as chat

    monkeypatch.setattr(chat, "_CALL_SECONDS", 0.2)
    gate = threading.Event()

    class Hung(_Fake):
        def held_digest(self):
            gate.wait(10)
            return "d1"

    d = Hung([_halt()])
    host = _host(tmp_path, {"alpha": d}, max_sessions=1)
    sid, _ = _open(host, "alpha")
    did = _wait(host, host.turn(sid, "go")["job_id"])["result"]["decision_id"]
    started = time.monotonic()
    with pytest.raises(ChatError) as e:
        host.approve(sid, expect=did)
    assert e.value.code == "unresponsive" and time.monotonic() - started < 2
    assert host.session_status(sid)["state"] == "unresponsive"
    assert not [c for c in d.calls if c[0] == "approve"]
    host.close(sid)                                   # the release lane is not the stuck one
    _until(lambda: host.session_status(sid)["state"] == "closed", what="the close")
    gate.set()


def test_the_reaper_starts_with_the_first_open_and_exits_when_nothing_is_live(tmp_path):
    """codex r3/r4 MED + complement LOW (ruling: lazy). A host holds no reaper until an open, and the reaper
    exits once no session can start a release and no report is awaited, so a host nobody shut down holds no
    thread. A release in flight keeps it."""
    late = _LateRelease()
    host = _host(tmp_path, {"alpha": late, "beta": _Fake([])})
    assert host._reaper_thread is None
    sid, _ = _open(host, "alpha")
    reaper = host._reaper_thread
    assert reaper is not None and reaper.is_alive()
    host.close(sid)
    time.sleep(1.5)
    assert reaper.is_alive() and host.session_status(sid)["state"] == "closing"
    late.done.set()
    reaper.join(3)
    assert not reaper.is_alive() and host._reaper_thread is None
    _until(lambda: host.session_status(sid)["state"] == "closed", what="the release")
    _open(host, "beta")                              # the next open starts it again
    assert host._reaper_thread is not None and host._reaper_thread.is_alive()


def test_shutdown_starts_no_thread(tmp_path, monkeypatch):
    """codex r3 MED (ruling (c)): shutdown started a closer thread per session and, when one could not
    start, ran the close inline. Every close now goes on its driver's own release lane, started at open."""
    d = _Fake([])
    host = _host(tmp_path, {"alpha": d})
    sid, _ = _open(host, "alpha")

    def no_threads(self):
        raise RuntimeError("can't start new thread")

    monkeypatch.setattr(threading.Thread, "start", no_threads)
    host.shutdown()
    monkeypatch.undo()
    assert d.closed
    _until(lambda: host.session_status(sid)["state"] == "closed", what="the release")


class _NativeRaises:
    def __init__(self, exc: BaseException):
        self.exc = exc

    def run_turn(self, message):
        return _Out()

    def request_stop(self):
        pass

    def close(self):
        raise self.exc


def test_a_native_close_that_raises_any_baseexception_is_not_a_release(tmp_path):
    """codex r3 HIGH: a TurnTimeout (a BaseException) from the native close skipped recording the failure,
    and the phase still went to closed, so wait_closed() read True for a shell that may be live."""
    d = OpenHandsDriver(tmp_path, lambda p, on_event: _NativeRaises(_Escape("mid-teardown")), close_wait=2)
    d.open(lambda e: None, on_released=_unheard)
    with pytest.raises(RuntimeError, match="release failed"):
        d.close()
    assert not d.wait_closed(0) and "_Escape" in d.release_error()


@pytest.mark.parametrize("force_ok", [True, False])
def test_the_driver_escalates_on_its_own_release_worker(tmp_path, force_ok):
    """Ruling (b): a force release is bounded like close and runs on the driver's own release worker, once;
    only a confirmed one is a release."""
    ran: list[str] = []

    class Forcing(OpenHandsDriver):
        def _escalate_release(self, session):
            ran.append(threading.current_thread().name)
            if not force_ok:
                raise OSError("kill failed")
            return True

    d = Forcing(tmp_path, lambda p, on_event: _NativeRaises(OSError("would not stop")), close_wait=2)
    d.open(lambda e: None, on_released=_unheard)
    if force_ok:
        d.close()
        assert d.wait_closed(0) and d.release_error() is None
    else:
        with pytest.raises(RuntimeError, match="kill failed"):
            d.close()
        assert not d.wait_closed(0)
        assert "would not stop" in d.release_error() and "kill failed" in d.release_error()
    assert ran == ["levain-driver-release"]


def test_the_release_worker_exits_when_the_phase_reaches_closed(tmp_path):
    """Ruling (d) + complement r3 LOW: the worker exits after its release, and when the driver closes with
    nothing handed to it."""
    class Sess:
        def close(self):
            pass

    d = OpenHandsDriver(tmp_path, lambda p, on_event: Sess(), close_wait=2)
    d.open(lambda e: None, on_released=_unheard)
    d.close()
    d._worker.join(2)
    assert not d._worker.is_alive()

    def opener(p, on_event):
        raise OSError("no entity")

    d = OpenHandsDriver(tmp_path, opener, close_wait=2)
    with pytest.raises(OSError):
        d.open(lambda e: None, on_released=_unheard)
    d._worker.join(2)
    assert not d._worker.is_alive() and d.wait_closed(0)


def test_a_worker_whose_driver_closed_with_nothing_handed_exits(tmp_path):
    """Ruling (d), the path only an asynchronous raise reaches (between the worker's start and open()
    recording it): the driver is marked closed inline, with nothing handed, and the worker must not wait
    forever for a hand-off that will never come."""
    d = OpenHandsDriver(tmp_path, lambda p, on_event: None, close_wait=2)
    worker = threading.Thread(target=d._release_worker, daemon=True)
    worker.start()
    d._release(None)            # what _hand_release does when no worker was recorded
    worker.join(2)
    assert not worker.is_alive() and d.wait_closed(0)


# -- L1 + L2 on df9d4b2: the boundary's own edges -------------------------------------------------------------


class _Unprintable(BaseException):
    def __str__(self):
        raise _Unprintable()        # and so does what it raises: every handler that prints it fails


def test_the_boundary_survives_an_exception_it_cannot_print():
    """L1 r4 HIGH (RAN): the boundary's own except built str(exc), so an exception whose __str__ raised
    escaped _call_driver itself."""
    from levain.chat_driver import _call_driver

    def boom():
        raise _Unprintable()

    got = _call_driver(boom)
    assert not got.ok and got.error == "_Unprintable: <unprintable _Unprintable>"


def test_a_session_whose_close_lookup_raises_is_not_released(tmp_path, escaped):
    """L1 r4 HIGH (RAN): `_call_driver(session.close)` looked the method up OUTSIDE the boundary; a raising
    lookup escaped _release, whose finally then marked the driver closed with no error: released."""
    class Sess:
        def run_turn(self, m):
            return _Out()

        def request_stop(self):
            pass

        def __getattr__(self, name):
            if name == "close":
                raise RuntimeError("close lookup broke")
            raise AttributeError(name)

    d = OpenHandsDriver(tmp_path, lambda p, on_event: Sess(), close_wait=2)
    d.open(lambda e: None, on_released=_unheard)
    with pytest.raises(RuntimeError, match="close lookup broke"):
        d.close()
    assert not d.wait_closed(0) and "close lookup broke" in d.release_error()
    assert escaped == []


@pytest.mark.parametrize("report", [5, "str-subclass"])
def test_a_release_report_that_breaks_the_contract_is_a_failed_release(tmp_path, escaped, report):
    """L2 r4 (RAN): a probe answering None left the record closing forever; L1 r4 (RAN): str() of a reported
    value ran driver code. A report that is neither None nor exactly str is a contract violation:
    release_failed, never "still releasing", and no driver code runs reading it."""
    class BadStr(str):
        def __str__(self):
            raise RuntimeError("str broke")

    class Bad(_LateRelease):
        def close(self):
            self.closed = True
            self.on_released(BadStr("x") if report == "str-subclass" else report)

    host = _host(tmp_path, {"alpha": Bad()})
    sid, _ = _open(host, "alpha")
    host.close(sid)
    _until(lambda: host.session_status(sid)["state"] == "release_failed", what="the contract")
    assert "driver contract" in host.session_status(sid)["error"]
    assert escaped == []


def test_a_late_confirmation_still_frees_the_slot(tmp_path, monkeypatch):
    """Ruling 2026-10-07: release_failed means "not confirmed YET". A release reported after the deadline,
    or after a close that raised, frees the slot, and the session says it was released late."""
    import levain.chat as chat

    monkeypatch.setattr(chat, "_REPORT_SECONDS", 0.2)
    d = _LateRelease()
    host = _host(tmp_path, {"alpha": d, "beta": _Fake([])}, max_sessions=1)
    sid, _ = _open(host, "alpha")
    host.close(sid)
    _until(lambda: host.session_status(sid)["state"] == "release_failed", what="the deadline")
    d.done.set()
    view = host.session_status(sid)
    assert view["state"] == "closed" and view["released_late"] and "release_failed_since" not in view
    _open(host, "beta")                              # the slot is free


def test_a_fault_in_reaping_ends_the_record_rather_than_retrying_forever(tmp_path, monkeypatch):
    """L1 r4: the reaper's catch-all left a faulting item queued and retried it forever. A deadline is
    handled once: a fault in handling it still ends the record release_failed, and is not retried."""
    import levain.chat as chat

    monkeypatch.setattr(chat, "_REPORT_SECONDS", 0.2)
    late = _LateRelease()
    host = _host(tmp_path, {"alpha": late})
    sid, _ = _open(host, "alpha")
    calls = []

    def broken(item, failure):
        calls.append(1)
        raise RuntimeError("host bug")

    monkeypatch.setattr(host, "_release_failed", broken)
    host.close(sid)
    _until(lambda: host.session_status(sid)["state"] == "release_failed", what="the reaper")
    time.sleep(0.5)
    assert len(calls) == 1 and host._reports == []


def test_a_failed_turns_outcome_is_published_before_its_close_returns(tmp_path):
    """L1 r4 (RAN, a regression of glm L3 r2): the job was published only after close() returned, 3 s here."""
    gate = threading.Event()

    class SlowClose(_Fake):
        def close(self):
            assert gate.wait(10)
            self.closed = True
            self.report()

    d = SlowClose([_Out(reply=None, error="boom")])
    host = _host(tmp_path, {"alpha": d})
    sid, _ = _open(host, "alpha")
    st = _wait(host, host.turn(sid, "go")["job_id"], timeout=2)
    assert st["status"] == "done" and host.session_status(sid)["state"] == "busy"
    gate.set()
    _until(lambda: host.session_status(sid)["state"] == "broken", what="the release")


def test_held_digest_is_read_outside_the_host_lock(tmp_path):
    """L2 r4 (RAN): approve read held_digest (driver code) under the host lock, so one slow digest stalled
    every route. It is read outside it; meanwhile the session reads busy."""
    gate, inside = threading.Event(), threading.Event()

    class SlowDigest(_Fake):
        def held_digest(self):
            inside.set()
            assert gate.wait(10)
            return "d1"

    d = SlowDigest([_halt(), _Out()])
    host = _host(tmp_path, {"alpha": d, "beta": _Fake([])})
    sid, _ = _open(host, "alpha")
    did = _wait(host, host.turn(sid, "go")["job_id"])["result"]["decision_id"]
    out: dict = {}
    t = threading.Thread(target=lambda: out.update(host.approve(sid, expect=did)))
    t.start()
    assert inside.wait(5)
    started = time.monotonic()
    host.listing()                                    # not stalled behind the digest
    assert host.session_status(sid)["state"] == "busy"
    with pytest.raises(ChatError) as e:
        host.approve(sid, expect=did)                 # the id is held aside: a second approve is stale
    assert e.value.code in ("wrong_state", "stale_decision")
    assert time.monotonic() - started < 1
    gate.set()
    t.join(5)
    assert _wait(host, out["job_id"])["status"] == "done"


def test_a_banner_is_copied_into_plain_data(tmp_path):
    """L1 r4 LOW: describe()'s dict was kept as returned, so a dict subclass ran driver code under the host
    lock on every view. It is copied into plain data inside the boundary, dropping what is not plain."""
    class Sneaky(dict):
        def items(self):
            raise RuntimeError("driver code under the host lock")

    class Banner(_Fake):
        def describe(self):
            return Sneaky(label="fake", nested={"x": 1}, n=3)

    host = _host(tmp_path, {"alpha": Banner([])})
    sid, _ = _open(host, "alpha")
    rec = host._sessions[sid]
    assert type(rec.info) is dict and rec.info == {"label": "fake", "n": 3}
    assert host.session_status(sid)["label"] == "fake"


# -- L3 r4 (input cd208f2244398812) -----------------------------------------------------------------------------


class _Liar(str):
    """Shows "" (or what it was built with) while its iteration and equality say otherwise."""

    def __iter__(self):
        return iter("rm -rf target")

    def __eq__(self, other):
        return True

    def __ne__(self, other):
        return False

    __hash__ = str.__hash__


def test_a_held_call_that_only_looks_blank_cannot_be_approved(tmp_path):
    """codex r4 HIGH (RAN): a str subclass passed the contract's isinstance check; the client saw full == ""
    while shown_in_full iterated it as text, and approve ran a call never shown. Only exact str is text."""
    held = PendingEfferent("terminal", "rm", "bash fans in", full=_Liar(""))
    d = _Fake([_Out(reply=None, gated=True, pending=(held,), held_digest="d1"), _Out()])
    host = _host(tmp_path, {"alpha": d})
    sid, _ = _open(host, "alpha")
    st = _wait(host, host.turn(sid, "go")["job_id"])
    assert st["result"]["pending"][0]["full"] == ""
    with pytest.raises(ChatError) as e:
        host.approve(sid, expect=st["result"]["decision_id"])
    assert e.value.code == "undecidable" and ("approve", None) not in d.calls


@pytest.mark.parametrize("field", ["reply", "tool_activity", "tool_name"])
def test_text_that_is_a_str_subclass_breaks_the_contract(field):
    with pytest.raises(DriverContractError):
        if field == "tool_name":
            PendingApproval(_Liar("terminal"), "d", "f", "r", True)
        elif field == "reply":
            read_outcome(_Out(reply=_Liar("hi")))
        else:
            read_outcome(_Out(tool_activity=[_Liar("x")]))


def test_a_live_digest_that_is_not_exactly_text_binds_nothing(tmp_path):
    """complement r4 LOW: a digest subclass overriding == matched any recorded digest."""
    class Lying(_Fake):
        def held_digest(self):
            return _Liar("anything")

    d = Lying([_halt(), _Out()])
    host = _host(tmp_path, {"alpha": d})
    sid, _ = _open(host, "alpha")
    st = _wait(host, host.turn(sid, "go")["job_id"])
    with pytest.raises(ChatError) as e:
        host.approve(sid, expect=st["result"]["decision_id"])
    assert e.value.code == "stale_decision"


def test_a_factory_raising_an_unprintable_exception_is_an_ordinary_failed_open(tmp_path, escaped):
    """codex r4 MED (RAN): the factory ran outside the boundary and the catch-all rendered str(exc); an
    unprintable BaseException killed the worker and left the job running and the record opening."""
    def factory(name, path):
        raise _Unprintable()

    host = ChatHost({"alpha": tmp_path}, driver_factory=factory)
    out = host.open("alpha")
    st = _wait(host, out["job_id"])
    assert st["status"] == "failed" and "unprintable" in st["error"]
    _until(lambda: host.session_status(out["session_id"])["state"] == "failed", what="the release")
    assert escaped == []


def test_a_banner_float_that_json_cannot_carry_is_dropped(tmp_path):
    """complement r4 LOW: nan or inf in describe() made every view of that session invalid JSON."""
    import json

    class Banner(_Fake):
        def describe(self):
            return {"label": "fake", "nan": float("nan"), "inf": float("inf"), "ok": 1.5}

    host = _host(tmp_path, {"alpha": Banner([])})
    sid, _ = _open(host, "alpha")
    json.dumps(host.listing(), allow_nan=False)
    assert host._sessions[sid].info == {"label": "fake", "ok": 1.5}


def test_a_failed_release_keeps_the_error_that_ended_the_session(tmp_path):
    """complement r4 LOW: release_failed replaced a broken turn's error with the release failure."""
    class Raising(_Fake):
        def close(self):
            raise RuntimeError("teardown failed")

    host = _host(tmp_path, {"alpha": Raising([_Out(reply=None, error="boom")])})
    sid, _ = _open(host, "alpha")
    _wait(host, host.turn(sid, "go")["job_id"])
    _until(lambda: host.session_status(sid)["state"] == "release_failed", what="the release")
    err = host.session_status(sid)["error"]
    assert "boom" in err and "teardown failed" in err


# -- the release-range review's chat.py findings (queued after S1) ---------------------------------------------


def test_a_view_cannot_change_what_the_next_viewer_is_shown(tmp_path):
    """codex (RAN): job and session views shallow-copied the result; editing a view's pending row changed
    what every later viewer was shown under the same decision id."""
    d = _Fake([_halt()])
    host = _host(tmp_path, {"alpha": d})
    sid, _ = _open(host, "alpha")
    jid = host.turn(sid, "go")["job_id"]
    st = _wait(host, jid)
    st["result"]["pending"][0]["full"] = "echo safe"
    view = host.session_status(sid)
    view["pending"][0]["full"] = "echo safe"
    view["last_job"]["result"]["pending"][0]["full"] = "echo safe"
    assert host.job_status(jid)["result"]["pending"][0]["full"] == _HELD.full
    again = host.session_status(sid)
    assert again["pending"][0]["full"] == _HELD.full
    assert again["last_job"]["result"]["pending"][0]["full"] == _HELD.full


def test_a_reject_binds_to_the_halt_its_caller_saw(tmp_path):
    """codex (RAN): A kept D1; B rejected D1 and the continuation halted at D2; A's reject without an id then
    rejected D2. A reject needs the current id; A's D1 is stale."""
    d = _Fake([_halt(), _halt(), _Out()])
    host = _host(tmp_path, {"alpha": d})
    sid, _ = _open(host, "alpha")
    d1 = _wait(host, host.turn(sid, "go")["job_id"])["result"]["decision_id"]
    _wait(host, host.reject(sid, "no", expect=d1)["job_id"])          # B
    for kw in ({}, {"expect": d1}):                                      # A, with nothing or with D1
        with pytest.raises(ChatError) as e:
            host.reject(sid, "no", **kw)
        assert e.value.code in ("decision_id_required", "stale_decision")
    assert [c[0] for c in d.calls].count("reject") == 1


# -- the 2026-10-07 rulings after r4: every host->driver call on the driver's lanes, with a deadline ----------


def test_a_turn_the_driver_never_returns_fails_and_the_session_is_released(tmp_path, monkeypatch):
    """No host call waits unbounded on driver code: a turn call past turn_seconds plus the grace fails ("the
    driver did not answer"), and the session is closed on its release lane, which the stuck turn lane does
    not block."""
    import levain.chat as chat

    monkeypatch.setattr(chat, "_TURN_GRACE_SECONDS", 0.2)
    gate = threading.Event()

    class Hung(_Fake):
        def send_turn(self, message):
            gate.wait(10)
            return read_outcome(_Out())

    host = _host(tmp_path, {"alpha": Hung([])}, turn_seconds=0.2)
    sid, _ = _open(host, "alpha")
    st = _wait(host, host.turn(sid, "go")["job_id"], timeout=5)
    assert st["status"] == "failed" and "did not answer" in st["error"]
    _until(lambda: host.session_status(sid)["state"] == "broken", what="the release")
    gate.set()


@pytest.mark.parametrize("where", ["make", "open", "describe"])
def test_an_open_the_driver_never_finishes_fails_closed(tmp_path, monkeypatch, where):
    """The factory, open() and describe() run on the driver's lanes with deadlines too."""
    import levain.chat as chat

    monkeypatch.setattr(chat, "_OPEN_SECONDS", 0.3)
    monkeypatch.setattr(chat, "_CALL_SECONDS", 0.3)
    gate = threading.Event()

    class Slow(_Fake):
        def open(self, on_event, *, on_released, resume=None):
            super().open(on_event, on_released=on_released)
            if where == "open":
                gate.wait(10)

        def describe(self):
            if where == "describe":
                gate.wait(10)
            return super().describe()

    def factory(name, path):
        if where == "make":
            gate.wait(10)
        return Slow([])

    host = ChatHost({"alpha": tmp_path}, driver_factory=factory, max_sessions=1)
    out = host.open("alpha")
    st = _wait(host, out["job_id"], timeout=5)
    assert st["status"] == "failed" and "did not answer" in st["error"]
    _until(lambda: host.session_status(out["session_id"])["state"] == "failed", what="the release")
    gate.set()


def test_a_stop_request_the_driver_never_answers_breaks_the_session(tmp_path, monkeypatch):
    """L1 r5 (RAN): a stop request past its deadline kept running and landed in the NEXT turn. It runs on its
    own lane (so it never takes held_digest's), the watcher is not held by it, and a job that ends with one
    still outstanding breaks the session instead of handing it back for another turn."""
    import levain.chat as chat

    monkeypatch.setattr(chat, "_CALL_SECONDS", 0.1)
    gate, stop = threading.Event(), threading.Event()

    class Stuck(_Fake):
        def send_turn(self, message):
            assert stop.wait(10)
            return read_outcome(_Out())

        def interrupt(self):
            gate.wait(10)       # never answers within the deadline

    host = _host(tmp_path, {"alpha": Stuck([])}, turn_seconds=0.1)
    sid, _ = _open(host, "alpha")
    job = host.turn(sid, "go")["job_id"]
    _until(lambda: host.job_status(job)["deadline_hit"], what="the deadline")
    time.sleep(0.5)
    stop.set()
    st = _wait(host, job)
    assert st["status"] == "done"            # the job keeps its own result (complement r5) ...
    _until(lambda: host.session_status(sid)["state"] == "broken", what="the release")
    assert "stop request" in host.session_status(sid)["error"]   # ... and the session takes no further turn
    gate.set()


def test_a_release_reported_before_close_is_still_heard(tmp_path):
    """A driver may report before close is called (its failed open released what it built)."""
    class Early(_Fake):
        def open(self, on_event, *, on_released, resume=None):
            super().open(on_event, on_released=on_released)
            self.report()
            raise OSError("no entity")

        def close(self):
            self.closed = True       # already reported

    host = _host(tmp_path, {"alpha": Early([])}, max_sessions=1)
    out = host.open("alpha")
    assert _wait(host, out["job_id"])["status"] == "failed"
    _until(lambda: host.session_status(out["session_id"])["state"] == "failed", what="the release")


def test_a_closed_drivers_lanes_exit(tmp_path):
    d = _Fake([])
    host = _host(tmp_path, {"alpha": d})
    sid, _ = _open(host, "alpha")
    lanes = list(host._sessions[sid].driver._lanes.values())
    host.close(sid)
    for lane in lanes:
        lane._thread.join(2)
        assert not lane._thread.is_alive()


# -- L1 + L2 r5 on ff3268c ---------------------------------------------------------------------------------------


def test_a_report_racing_a_failure_is_not_overwritten(tmp_path, monkeypatch):
    """L1 + L2 r5 (RAN, widened window): the failure path checked `settled`, let go of the lock, and later
    wrote release_failed over a None report that had settled the record in between: counted forever."""
    class D(_Fake):
        def close(self):
            threading.Timer(0.05, self.report).start()
            raise RuntimeError("close raised")

    host = _host(tmp_path, {"e": D([]), "f": _Fake([])}, max_sessions=1)
    real = host._mark_release_failed

    def slow(*a, **k):
        time.sleep(0.3)             # widen the gap between the caller's check and the write
        return real(*a, **k)

    monkeypatch.setattr(host, "_mark_release_failed", slow)
    sid, _ = _open(host, "e")
    host.close(sid)
    time.sleep(1.0)
    assert host.session_status(sid)["state"] == "closed"
    _open(host, "f")                                  # the slot is free


def test_an_events_text_is_never_read_under_the_host_lock(tmp_path):
    """L1 r5 (RAN): a str subclass in a DriverEvent ran its __str__ with the host lock held. Only exact
    types are read, outside the lock."""
    held: list[bool] = []

    class Sly(str):
        def __str__(self):
            held.append(host._lock.locked())
            return "x"

    class D(_Fake):
        def send_turn(self, message):
            self.sink(DriverEvent("activity", Sly("hi")))
            self.sink(DriverEvent("activity", "plain"))
            return self._next("send_turn", message)

    host = _host(tmp_path, {"e": D([_Out()])})
    sid, _ = _open(host, "e")
    jid = host.turn(sid, "go")["job_id"]
    _wait(host, jid)
    assert held == []


def test_a_close_that_never_returns_leaves_only_its_release_lane(tmp_path):
    """L1 + L2 r5 (RAN): the other lanes were retired only when close RETURNED, so a driver whose close
    reported and then hung kept all of them, one set per session, unbounded."""
    gate = threading.Event()

    class Hangs(_Fake):
        def close(self):
            self.report()
            gate.wait(10)

    host = _host(tmp_path, {"e": Hangs([])})
    sid, _ = _open(host, "e")
    lanes = host._sessions[sid].driver._lanes
    host.close(sid)
    _until(lambda: host.session_status(sid)["state"] == "closed", what="the report")
    for name in ("turn", "control", "stop"):
        lanes[name]._thread.join(2)
        assert not lanes[name]._thread.is_alive(), name
    assert lanes["release"]._thread.is_alive()
    gate.set()


def test_a_driver_closed_before_it_opened_reports_its_release(tmp_path):
    """L1 r5: close() from "new" reported nothing, so an open that registered a sink and then refused (a
    resume) left the host waiting for a report until its deadline."""
    heard: list = []
    d = OpenHandsDriver(tmp_path, lambda p, on_event: None, close_wait=2)
    with pytest.raises(DriverUnsupported):
        d.open(lambda e: None, on_released=heard.append, resume="abc")
    d.close()
    assert heard == [None]


def test_an_abandoned_call_never_runs_and_does_not_hold_the_lane(tmp_path):
    """L2 r5: a call queued behind a stuck one ran after its caller was told it failed, and a lane cleared
    by name could be cleared by the wrong call."""
    from levain.chat import _Lane
    from levain.chat_driver import _call_driver

    lane = _Lane("t")
    lane.start()
    gate, ran = threading.Event(), []
    slow = threading.Thread(target=lambda: lane.call("a", lambda: _call_driver(lambda: gate.wait(5)), 10))
    slow.start()                                        # running, within its own (long) deadline
    _until(lambda: not lane.idle(), what="the first call")
    queued = lane.call("b", lambda: _call_driver(lambda: ran.append(1)), 0.1)
    assert queued.unanswered                            # abandoned while queued behind it
    gate.set()
    slow.join(5)
    _until(lambda: lane.idle(), what="the lane")
    assert ran == []                                    # never run once its caller was told it failed
    first = lane.call("c", lambda: _call_driver(lambda: gate.clear() or threading.Event().wait(0.5)), 0.1)
    assert first.unanswered                             # stuck: later calls fail at once ...
    assert "has not returned" in lane.call("d", lambda: _call_driver(lambda: 1), 1).error
    _until(lambda: lane.idle(), what="the stuck call")  # ... until it returns
    assert lane.call("e", lambda: _call_driver(lambda: 7), 1).value == 7
    lane.retire()


# -- L3 r5 (input 977c005f89ca632a) -------------------------------------------------------------------------------


def test_caps_are_exact_types():
    """codex r5 MED (RAN): a str subclass naming "in_turn" whose `!=` lies passed as after-turn."""
    class Lying(str):
        def __ne__(self, other):
            return False

    with pytest.raises(DriverContractError):
        DriverCaps(approval_timing=Lying("in_turn"))   # type: ignore[arg-type]
    with pytest.raises(DriverContractError):
        DriverCaps(can_resume=1)                        # type: ignore[arg-type]


def test_a_driver_whose_name_cannot_be_read_is_not_opened(tmp_path):
    """codex r5 LOW: a harness read that failed was ignored when caps read fine."""
    class Nameless(_Fake):
        @property
        def harness(self):
            raise RuntimeError("no name")

    host = _host(tmp_path, {"alpha": Nameless([])})
    sid, st = _open(host, "alpha")
    assert st["status"] == "failed" and "no name" in st["error"]


def test_a_failed_open_whose_teardown_fails_keeps_both_errors(tmp_path):
    """codex r5 LOW: the open's own error was lost from the session when its teardown failed too."""
    class Bad(_Fake):
        def open(self, on_event, *, on_released, resume=None):
            super().open(on_event, on_released=on_released)
            raise RuntimeError("bad model")

        def close(self):
            raise RuntimeError("teardown failed")

    host = _host(tmp_path, {"alpha": Bad([])})
    sid, st = _open(host, "alpha")
    _until(lambda: host.session_status(sid)["state"] == "release_failed", what="the release")
    err = host.session_status(sid)["error"]
    assert "bad model" in err and "teardown failed" in err

"""Routing heavy panels to the warm derive child (levain#157 S4b): which panels go, what a failure of the child becomes, when a
panel is promoted and demoted by its measured duration, and the wiring (a factory, the child closed with the host).

Most tests use ``FakeChild``, an object with the client's surface that spawns nothing and records what it was asked.
The tests that need a process use the S4a stub factory (``test_derive_child_stub``) and nothing else; each cleans its
processes up in a ``finally``."""

from __future__ import annotations

import dataclasses
import json
import os
import signal
import sys
import time
from pathlib import Path
from typing import Any, Callable

import pytest

from levain.cockpit import derive_child_client as client_mod
from levain.cockpit import snapshot_host as sh
from levain.cockpit.derive import DERIVE_TIMEOUT_S, DeriveResult
from levain.cockpit.derive_child_client import ChildAnswer, DeriveChild
from levain.cockpit.engine import Cockpit
from levain.cockpit.snapshot_host import (
    DEMOTE_MS, DISCOVER_ID, ENTITY_ID, HEAVY_PANELS, PROMOTE_MS, PROMOTE_WINDOW, SnapshotHost,
)
from levain.web_server import COCKPIT_DERIVE_FACTORY_ENV, COCKPIT_DERIVE_SYSPATH_ENV, COCKPIT_SNAPSHOTS_ENV, make_server

from tests import test_derive_child_stub as stub
from tests.test_cockpit_k1 import _install, _simple

TESTS_DIR = str(Path(__file__).resolve().parent)
FACTORY = "test_derive_child_stub:factory"
MARK = {"v": 1, "marker": "child"}


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def ok_answer(pid: str, timeout_s: float) -> ChildAnswer:
    return ChildAnswer("ok", dict(MARK, panel=pid), None, 5)


class FakeChild:
    """The client's surface (``derive``, ``knows``, ``warm``, ``close``) with no process behind it. ``answer`` makes each
    reply; ``known`` is the set of panels its (imagined) factory built, None meaning every panel."""

    def __init__(self, answer: "Callable[[str, float], ChildAnswer] | None" = None, known: "set[str] | None" = None) -> None:
        self.answer = answer or ok_answer
        self.known = known
        self.calls: list[tuple[str, float]] = []
        self.required: "list[frozenset[str]]" = []      # the ``require_attached`` each ask came with
        self.closed = 0
        self.warmed = 0

    def derive(self, panel_id: str, timeout_s: float, require_attached: "frozenset[str]" = frozenset()) -> ChildAnswer:
        self.calls.append((panel_id, timeout_s))
        self.required.append(require_attached)
        return self.answer(panel_id, timeout_s)

    def knows(self, panel_id: str) -> bool:
        return self.known is None or panel_id in self.known

    def warm(self) -> None:
        self.warmed += 1

    def close(self) -> None:
        self.closed += 1

    def asked(self) -> list[str]:
        return [p for p, _ in self.calls]


def lost(kind: str, hung: bool = False) -> ChildAnswer:
    return ChildAnswer(None, None, None, None, kind, f"the child {kind}", hung)


def timed(ms: "list[int]") -> "Callable[[str, float], ChildAnswer]":
    """Answers that each say the derive took the next number of milliseconds (the last one repeats)."""
    left = list(ms)
    return lambda pid, t: ChildAnswer("ok", dict(MARK, panel=pid), None, left.pop(0) if len(left) > 1 else left[0])


class Timer:
    """A ``timer`` for the host that reads each derive as taking the next number of milliseconds in ``ms``."""

    def __init__(self, ms: "list[float]") -> None:
        self.ms = list(ms)
        self.now = 1000.0
        self._open = False

    def __call__(self) -> float:
        if not self._open:
            self._open = True
            return self.now
        self._open = False
        self.now += self.ms.pop(0) / 1000.0
        return self.now


@pytest.fixture
def hosts():
    made: list[SnapshotHost] = []
    yield made
    for h in made:
        h.stop(timeout=10)


@pytest.fixture
def make_host(tmp_path: Path, hosts: "list[SnapshotHost]", monkeypatch):
    monkeypatch.setenv("DERIVE_CHILD_STUB_DIR", str(tmp_path))     # the stub's in-process ``hang`` reads its flag from here

    def make(ck: "Cockpit | None" = None, **kw: Any) -> SnapshotHost:
        root = tmp_path / f"host{len(hosts)}"
        root.mkdir()
        host = SnapshotHost(ck if ck is not None else stub.factory(), root, **kw)
        hosts.append(host)
        return host

    return make


# ------------------------------------------------------------------------------------------------ the constants


def test_the_promotion_rule_is_named_and_its_demotion_bound_is_half_of_it():
    """A rule written as bare numbers in the code could drift apart; these are the three the rule is made of."""
    assert (PROMOTE_MS, PROMOTE_WINDOW, DEMOTE_MS) == (250, 3, 125)
    assert HEAVY_PANELS == {"board", "topology", "sky"}


# ------------------------------------------------------------------------------------------------ which panels go


def test_a_heavy_panel_goes_to_the_child_with_the_plans_timeout_and_a_light_one_never_does(make_host):
    """A light panel sent to the child, or a heavy one derived here, shows as the wrong host in the answer; the deadline the
    child is killed at must be the one the loop publishes its timeout row at (the plan's), not a default."""
    child = FakeChild()
    ck = stub.factory()
    real_spec = ck.spec
    ck.spec = lambda pid, **kw: (dataclasses.replace(real_spec(pid), timeout_s=7.0) if pid == "fast" else real_spec(pid, **kw))  # type: ignore[method-assign]
    host = make_host(ck, heavy={"fast"}, derive_child=child)
    plan = {p.id: p for p in host._plans()}
    assert plan["fast"].timeout_s == 7.0 and plan["fast"].heavy is True and plan["partial"].heavy is False
    res = host._derive("fast")
    assert res.body["marker"] == "child" and child.calls == [("fast", 7.0)]
    light = host._derive("partial")
    assert light.status == "partial" and "marker" not in light.body
    assert child.asked() == ["fast"]


def test_a_panel_with_no_deadline_of_its_own_gets_the_loops_default(make_host):
    child = FakeChild()
    ck = stub.factory()
    real_spec = ck.spec
    ck.spec = lambda pid, **kw: (dataclasses.replace(real_spec(pid), timeout_s=None) if pid == "fast" else real_spec(pid, **kw))  # type: ignore[method-assign]
    host = make_host(ck, heavy={"fast"}, derive_child=child)
    host._derive("fast")
    assert child.calls == [("fast", DERIVE_TIMEOUT_S)]


def test_underscore_serve_composed_discovered_and_pseudo_panels_are_never_routed(make_host):
    """The child refuses a pseudo id, and a serve-composed or discovered panel is the serve's own: each must stay here
    even when it is in the heavy set, and even when it is as slow as can be."""
    child = FakeChild()
    ck = Cockpit()
    for pid in ("_sneaky", "mine", "found", "plain"):
        ck.register(_simple(pid))
    ck.mark_serve_composed(["mine"])
    ck._discovered = {"found": 0}       # the engine's own record of a panel a discoverer registered
    host = make_host(ck, heavy={"_sneaky", "mine", "found", "plain", DISCOVER_ID, ENTITY_ID}, derive_child=child)
    assert [host._routed(p) for p in ("_sneaky", "mine", "found", "plain")] == [False, False, False, True]
    host._derive("_sneaky")
    host._derive("mine")
    host._derive("found")
    host._derive(DISCOVER_ID)
    host._derive(ENTITY_ID)
    assert child.calls == []
    # slow derives promote nothing of these
    host2 = make_host(ck, derive_child=child, timer=Timer([900.0] * 9))
    for _ in range(3):
        for pid in ("_sneaky", "mine", "found"):
            host2._derive(pid)
    assert [host2._routed(p) for p in ("_sneaky", "mine", "found")] == [False, False, False]
    assert child.calls == []


def test_a_panel_the_cockpit_no_longer_has_is_derived_here_so_the_loop_retires_it(make_host):
    child = FakeChild()
    host = make_host(heavy={"fast", "gone"}, derive_child=child)
    res = host._derive("gone")
    assert res.status == "error" and "no longer registered" in (res.error or "")
    assert child.calls == []


# ------------------------------------------------------------------------------------------------ what an answer becomes


def test_an_error_answer_becomes_the_error_row_the_in_process_derive_publishes(make_host):
    """The in-process derive that raises is published by the loop as an error row with ``Type: text``; the child's error
    answer (no body) must land as the very same row, read from the store."""
    child = FakeChild(lambda p, t: ChildAnswer("error", None, None, 3, None, "ValueError: the stub's derive_panel blew up"))
    routed = make_host(heavy={"explode"}, derive_child=child)
    here = make_host()
    routed.run_once()
    here.run_once()
    a, b = routed.reader.read("explode"), here.reader.read("explode")
    assert child.asked() == ["explode"]
    assert a is not None and b is not None
    assert (a.status, a.error, a.body) == (b.status, b.error, b.body) == ("error", "ValueError: the stub's derive_panel blew up", None)


def test_ok_and_partial_answers_pass_through_with_their_bound(make_host):
    answers = {"fast": ChildAnswer("ok", dict(MARK), 4070908800.0, 5), "partial": ChildAnswer("partial", dict(MARK), None, 5)}
    host = make_host(heavy={"fast", "partial"}, derive_child=FakeChild(lambda p, t: answers[p]))
    assert host._derive("fast") == DeriveResult("ok", MARK, error=None, valid_until=4070908800.0)
    assert host._derive("partial") == DeriveResult("partial", MARK, error=None, valid_until=None)


# ------------------------------------------------------------------------------------------------ what a failure becomes


def test_a_timeout_is_the_loops_timeout_row_and_runs_nothing_here(make_host):
    """The child is killed already; running the derive again in this process would hold a thread for the very hang the kill
    ended."""
    child = FakeChild(lambda p, t: lost("timeout"))
    host = make_host(heavy={"count"}, derive_child=child)
    ran: list[str] = []
    real = host._cockpit.derive_panel
    host._cockpit.derive_panel = lambda pid: (ran.append(pid), real(pid))[1]       # type: ignore[method-assign]
    res = host._derive("count")
    assert res.status == "timeout" and res.body is None and "still running after" in (res.error or "")
    assert ran == [] and host.child_fallbacks == 0


@pytest.mark.parametrize("kind", ["died", "refused", "bad_reply"])
def test_a_child_that_died_refused_or_sent_nonsense_leaves_this_derive_to_run_here_and_is_counted(make_host, kind):
    child = FakeChild(lambda p, t: lost(kind))
    host = make_host(heavy={"fast"}, derive_child=child)
    res = host._derive("fast")
    assert res.status == "ok" and "marker" not in res.body and [r["title"] for r in res.body["rows"]] == ["row 1", "row 2", "row 3"]
    assert host.child_fallbacks == 1
    host._derive("fast")
    assert host.child_fallbacks == 2


@pytest.mark.parametrize("answer", [
    ChildAnswer("ok", None, None, 5),                   # a status that carries a body, with none
    ChildAnswer("nonsense", dict(MARK), None, 5),       # a status the store does not know
    ChildAnswer("ok", "not a body", None, 5),
], ids=["ok-without-body", "unknown-status", "body-not-an-object"])
def test_an_answer_that_cannot_be_a_result_is_a_fallback_not_a_row(make_host, answer):
    host = make_host(heavy={"fast"}, derive_child=FakeChild(lambda p, t: answer))
    res = host._derive("fast")
    assert res.status == "ok" and "marker" not in res.body
    assert host.child_fallbacks == 1


def test_a_client_that_raises_is_a_fallback_and_nothing_raises_into_the_loop(make_host):
    def boom(p: str, t: float) -> ChildAnswer:
        raise RuntimeError("a client bug")

    host = make_host(heavy={"fast"}, derive_child=FakeChild(boom))
    assert host._derive("fast").status == "ok"
    assert host.child_fallbacks == 1


def test_a_failure_after_stop_is_an_error_row_not_a_derive_here(make_host):
    """A host that is stopping has closed its child on purpose: the derive it cut short must not start over in this process."""
    child = FakeChild(lambda p, t: lost("died"))
    host = make_host(heavy={"fast"}, derive_child=child)
    host.stop(timeout=5)
    res = host._derive("fast")
    assert res.status == "error" and "stopping" in (res.error or "")
    assert host.child_fallbacks == 0


# ------------------------------------------------------------------------------------------------ the promotion rule


def _light(make_host, ms: "list[float]", child_ms: "list[int] | None" = None) -> "tuple[SnapshotHost, FakeChild]":
    """A host whose in-process derives take ``ms`` (its timer) and whose child says its derives take ``child_ms``."""
    child = FakeChild(timed(child_ms) if child_ms else None)
    return make_host(derive_child=child, timer=Timer(ms)), child


def test_three_slow_derives_promote_a_panel_from_its_next_derive(make_host):
    host, child = _light(make_host, [300, 300, 300, 10])
    for _ in range(2):
        host._derive("count")
        assert not host._routed("count")
    host._derive("count")
    assert "count" in host.routed_panels() and child.calls == [], "the third slow derive ran here: routing starts at the next"
    res = host._derive("count")
    assert res.body["marker"] == "child" and child.asked() == ["count"]


def test_a_promotion_needs_every_one_of_the_last_three_over_the_bound(make_host):
    """One fast derive in the window, or a derive exactly at the bound, keeps the panel here: a rule on the average or on
    'at least' would promote."""
    host, _ = _light(make_host, [300, 300, 100, 300, 300, 300, 300, 300, PROMOTE_MS, 300, 300])
    for _ in range(5):
        host._derive("count")
    assert not host._routed("count"), "[300,300,100] then [300,100,300] then [100,300,300] each hold a fast derive"
    host._derive("count")       # window is now [300, 300, 300]
    assert host._routed("count")
    host2, _ = _light(make_host, [300, 300, PROMOTE_MS])
    for _ in range(3):
        host2._derive("count")
    assert not host2._routed("count"), "a derive of exactly the bound is not over it"


def test_a_promoted_panel_returns_after_three_fast_derives_and_not_before(make_host):
    host, _ = _light(make_host, [300, 300, 300], child_ms=[100])
    for _ in range(3):
        host._derive("count")
    assert host._routed("count")
    host._derive("count")
    host._derive("count")
    assert host._routed("count"), "the window [300, 100, 100] still holds a slow derive: a panel returns only when all three are fast"
    host._derive("count")
    assert not host._routed("count")


def test_a_promoted_panel_that_now_answers_fast_and_empty_is_demoted(make_host):
    """A healthy ``empty`` answer is a measured derive like an ``ok`` one: ``ms`` came back None for it, so a promoted panel that
    began answering fast-and-empty (nothing to show) stayed in the child for good."""
    empty = lambda pid, t: ChildAnswer("empty", dict(MARK, panel=pid), None, 5)     # noqa: E731
    host, child = _light(make_host, [300, 300, 300])
    for _ in range(3):
        host._derive("count")
    assert host._routed("count")
    child.answer = empty
    for _ in range(3):
        res = host._derive("count")
        assert res.status == "empty"
    assert not host._routed("count")


def test_between_the_two_bounds_a_panel_stays_where_it_is(make_host):
    """200 ms is under the promotion bound and over the demotion bound: it neither promotes a panel here nor sends a routed
    one back, which is what keeps the list from flapping."""
    here, _ = _light(make_host, [200] * 6)
    for _ in range(6):
        here._derive("count")
    assert not here._routed("count")
    routed, _ = _light(make_host, [300, 300, 300], child_ms=[200])
    for _ in range(9):
        routed._derive("count")
    assert routed._routed("count")


def test_the_named_heavy_panels_stay_routed_however_fast_they_are(make_host):
    child = FakeChild()
    host = make_host(heavy={"fast"}, derive_child=FakeChild(timed([5])), timer=Timer([5.0] * 6))
    for _ in range(6):
        host._derive("fast")
    assert host._routed("fast") and host._windows == {}, "a named heavy panel is routed always and is not even measured"
    host2 = make_host(heavy={"fast"}, derive_child=child)
    for _ in range(6):
        host2._derive("fast")
    assert child.asked() == ["fast"] * 6


def test_a_host_with_no_child_keeps_no_durations_and_promotes_nothing(make_host):
    host = make_host(timer=Timer([900.0] * 3))
    for _ in range(3):
        host._derive("count")
    assert host._windows == {} and host.routed_panels() == HEAVY_PANELS and host.child_fallbacks == 0


# ------------------------------------------------------------------------------------------------ the wiring, no process


def test_without_a_factory_no_child_client_is_ever_imported_and_the_rows_are_the_same(make_host, monkeypatch):
    """The import of the child's client is poisoned: a host that touches it fails at once. The rows of a host with no child
    equal those of a host whose (never asked) child covers no panel."""
    monkeypatch.setitem(sys.modules, "levain.cockpit.derive_child_client", None)
    bare = make_host()
    assert bare._child is None
    bare.run_once()
    untouched = FakeChild()
    twin = make_host(heavy=(), derive_child=untouched)
    twin.run_once()
    for pid in ("fast", "partial", "explode", "pid"):     # "count" is left out: it counts this process's reads, so it differs by design
        a, b = bare.reader.read(pid), twin.reader.read(pid)
        assert (a.status, a.body_sha, a.body, a.error, a.valid_until) == (b.status, b.body_sha, b.body, b.error, b.valid_until), pid
    assert untouched.calls == []
    # and the runtime of snapshot_host holds no import of the client outside type checking
    src = Path(sh.__file__).read_text()
    assert "derive_child_client import" not in src.replace("    from levain.cockpit.derive_child_client import ChildAnswer, DeriveChild\n", "")


def test_the_heavy_flag_the_scheduler_reads_is_unchanged_for_a_routed_panel(make_host):
    """Routing is the derive function's business: a panel's plan says heavy the same with a child as without."""
    with_child = {p.id: p.heavy for p in make_host(heavy={"fast"}, derive_child=FakeChild())._plans()}
    without = {p.id: p.heavy for p in make_host(heavy={"fast"})._plans()}
    assert with_child == without and with_child["fast"] is True and with_child["partial"] is False


def test_stop_closes_the_child_and_twice_is_safe(make_host):
    child = FakeChild()
    host = make_host(derive_child=child)
    host.stop(timeout=5)
    assert child.closed == 1
    host.stop(timeout=5)
    assert child.closed >= 1


# ------------------------------------------------------------------------------------------------ the stub child, a real process


class Procs:
    """Every child client made by a test, so a finally can end whatever is left."""

    def __init__(self, tmp: Path) -> None:
        self.tmp = tmp
        self.clients: list[DeriveChild] = []
        self.pids: set[int] = set()
        self.offset = [0.0]
        self.err = open(tmp / "child.stderr", "w+")

    def client(self, factory: str = FACTORY, **kw: Any) -> DeriveChild:
        kw.setdefault("backoff_base_s", 5.0)
        kw.setdefault("term_grace_s", 2.0)
        c = DeriveChild(factory, syspath=[TESTS_DIR], env=dict(os.environ, DERIVE_CHILD_STUB_DIR=str(self.tmp)), stderr=self.err,
                        clock=lambda: time.monotonic() + self.offset[0], **kw)
        self.clients.append(c)
        return c

    def note(self, c: DeriveChild) -> "int | None":
        if c.pid is not None:
            self.pids.add(c.pid)
        return c.pid

    def skip_backoff(self) -> None:
        self.offset[0] += 1000.0

    def cleanup(self) -> None:
        (self.tmp / "hang.flag").unlink(missing_ok=True)
        for c in self.clients:
            try:
                c.close()
            except Exception:  # noqa: BLE001
                pass
            if c._proc is not None:
                try:
                    os.killpg(c._proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        for pid in self.pids:
            if _alive(pid):
                try:
                    os.killpg(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        self.err.close()


@pytest.fixture
def procs(tmp_path: Path):
    p = Procs(tmp_path)
    try:
        yield p
    finally:
        p.cleanup()


def _row_of(host: SnapshotHost, pid: str) -> tuple:
    r = host.reader.read(pid)
    assert r is not None, pid
    return (r.status, r.body_sha, r.body, r.error, r.valid_until)


def test_a_row_made_by_the_child_is_the_row_the_in_process_host_makes(make_host, procs):
    """Two hosts, one store each, the same panels: the heavy ones derived in the child must publish rows equal to the
    in-process host's, byte for byte, over an ok, a partial and an error status."""
    heavy = {"fast", "partial", "explode"}
    c = procs.client()
    assert c.start(30.0)
    routed = make_host(heavy=heavy, derive_child=c)
    here = make_host()
    routed.run_once()
    procs.note(c)
    here.run_once()
    statuses = set()
    for pid in sorted(heavy):
        assert _row_of(routed, pid) == _row_of(here, pid), pid
        statuses.add(routed.reader.read(pid).status)
    assert statuses == {"ok", "partial", "error"} and c.pid is not None and routed.child_fallbacks == 0


def test_a_routed_panel_really_runs_in_another_process_and_a_light_one_in_this(make_host, procs):
    c = procs.client()
    assert c.start(30.0)
    host = make_host(heavy={"pid"}, derive_child=c)
    host.run_once()
    procs.note(c)
    row = host.reader.decoded(host.reader.read("pid"))["rows"][0]["title"]
    assert int(row.split()[0]) == c.pid != os.getpid()
    here = host.reader.decoded(host.reader.read("count"))["rows"][0]["title"]
    assert here.startswith("call ")


def test_a_hung_heavy_derive_is_killed_at_its_deadline_the_loop_publishes_its_timeout_row_and_the_next_derive_gets_a_fresh_child(make_host, procs):
    c = procs.client()
    assert c.start(30.0)
    ck = stub.factory()
    real_spec = ck.spec
    ck.spec = lambda pid, **kw: (dataclasses.replace(real_spec(pid), timeout_s=1.0) if pid == "hang" else real_spec(pid, **kw))  # type: ignore[method-assign]
    host = make_host(ck, heavy={"hang", "count"}, derive_child=c)
    flag = procs.tmp / "hang.flag"
    try:
        assert host._derive("count").body["rows"][0]["title"] == "call 1"
        old = procs.note(c)
        assert old is not None
        flag.write_text("x")
        t0 = time.monotonic()
        host.run_once()                    # derives every due panel; "hang" is the one that does not come back
        took = time.monotonic() - t0
        row = host.reader.read("hang")
        assert row is not None and row.status == "timeout" and row.body is None
        assert "still running after 1 s" in (row.error or "")
        assert 0.9 <= took < 20.0, f"killed at the deadline, not long after: {took:.2f}s"
        assert not _alive(old), "the child that held the hung derive is gone"
        assert host.child_fallbacks == 0, "a timeout is not run again in this process"
        ran: list[str] = []
        real = host._cockpit.derive_panel
        host._cockpit.derive_panel = lambda pid: (ran.append(pid), real(pid))[1]       # type: ignore[method-assign]
        again = host._derive("hang")            # inside the client's backoff: refused, and still not run here
        assert again.status == "timeout" and "still running after 1 s" in (again.error or "")
        assert ran == [] and host.child_fallbacks == 0, "the hung panel's provider never ran in this process"
        host._cockpit.derive_panel = real       # type: ignore[method-assign]
        procs.skip_backoff()               # the client's backoff, on its injected clock
        flag.unlink()
        assert host._derive("count").body["rows"][0]["title"] == "call 1", "a fresh child counts from one"
        fresh = procs.note(c)
        assert fresh is not None and fresh != old
        released = host._derive("hang")
        assert released.status == "ok" and released.body["rows"][0]["title"] == "released"
    finally:
        flag.unlink(missing_ok=True)


def test_a_child_that_cannot_start_leaves_every_derive_to_run_here(make_host, procs):
    """A child that is dead (its factory cannot build), and then refused inside its backoff, never makes the serve worse
    than the thread host: the panel still derives, here. The child never held the panel, so it is never routed (no fallback
    is counted: nothing was asked)."""
    c = procs.client(factory="test_derive_child_stub:failing_factory")
    assert c.start(30.0) is False
    host = make_host(heavy={"fast"}, derive_child=c)
    first = host._derive("fast")
    assert first.status == "ok" and host.child_fallbacks == 0
    second = host._derive("fast")                           # inside the backoff: nothing is started
    assert second == first and c.pid is None


def test_a_child_killed_between_derives_is_replaced_without_a_fallback(make_host, procs):
    c = procs.client()
    assert c.start(30.0)
    host = make_host(heavy={"count"}, derive_child=c)
    host._derive("count")
    old = procs.note(c)
    assert old is not None
    proc = c._proc
    os.kill(old, signal.SIGKILL)
    assert proc is not None and proc.wait(timeout=10) == -signal.SIGKILL
    res = host._derive("count")
    assert res.body["rows"][0]["title"] == "call 1" and host.child_fallbacks == 0 and procs.note(c) != old


def test_stopping_the_host_leaves_no_child_process(make_host, procs):
    c = procs.client()
    assert c.start(30.0)
    host = make_host(heavy={"count"}, derive_child=c)
    host._derive("count")
    pid = procs.note(c)
    assert pid is not None and _alive(pid)
    assert host.stop(timeout=10) == []
    assert not _alive(pid) and c.pid is None


# ------------------------------------------------------------------------------------------------ make_server


def _serve(tmp_path: Path, monkeypatch, *, env: "dict[str, str] | None" = None, **kw: Any):
    monkeypatch.setenv(COCKPIT_SNAPSHOTS_ENV, "1")
    monkeypatch.setenv("DERIVE_CHILD_STUB_DIR", str(tmp_path))
    for key in (COCKPIT_DERIVE_FACTORY_ENV, COCKPIT_DERIVE_SYSPATH_ENV):
        monkeypatch.delenv(key, raising=False)
    for key, value in (env or {}).items():
        monkeypatch.setenv(key, value)
    _root, src = _install(tmp_path)
    return make_server(src, host="127.0.0.1", port=0, **kw)


def test_make_server_takes_the_factory_from_its_argument_else_the_environment_else_none(tmp_path: Path, monkeypatch):
    servers = []
    try:
        for name, env, kw, want in (
            ("a", {}, {}, (None, ())),
            ("b", {COCKPIT_DERIVE_FACTORY_ENV: "m:envf", COCKPIT_DERIVE_SYSPATH_ENV: os.pathsep.join(["/x", "/y"])}, {}, ("m:envf", ("/x", "/y"))),
            ("c", {COCKPIT_DERIVE_FACTORY_ENV: "m:envf"}, {"derive_factory": "m:arg", "derive_syspath": ("/z",)}, ("m:arg", ("/z",))),
        ):
            (tmp_path / name).mkdir()
            servers.append(_serve(tmp_path / name, monkeypatch, env=env, **kw))
            assert (servers[-1].derive_factory, servers[-1].derive_syspath) == want, name
    finally:
        for srv in servers:
            srv.server_close()


def test_a_server_with_a_factory_opens_a_host_that_owns_a_child_and_closing_it_leaves_no_process(tmp_path: Path, monkeypatch):
    httpd = _serve(tmp_path, monkeypatch, derive_factory=FACTORY, derive_syspath=(TESTS_DIR,))
    pid = None
    try:
        httpd._open_snapshot_host(stub.factory())
        host = httpd._snapshot_host
        assert host is not None and isinstance(host._child, DeriveChild)
        assert host._child.start(30.0), "the host warmed its child when it started: the panels it holds are known"
        assert host._child.knows("pid")
        ans = host._child.derive("pid", 30.0)
        assert ans.failure is None
        pid = host._child.pid
        assert pid is not None and _alive(pid)
    finally:
        httpd.server_close()
        if pid is not None and _alive(pid):
            os.killpg(pid, signal.SIGKILL)
    assert pid is not None and not _alive(pid)


def test_a_server_without_a_factory_opens_a_host_with_no_child_and_never_loads_the_client(tmp_path: Path, monkeypatch):
    monkeypatch.setitem(sys.modules, "levain.cockpit.derive_child_client", None)
    httpd = _serve(tmp_path, monkeypatch)
    try:
        httpd._open_snapshot_host(stub.factory())
        assert httpd._snapshot_host is not None and httpd._snapshot_host._child is None
    finally:
        httpd.server_close()


class _Spy(FakeChild):
    made: "list[_Spy]" = []

    def __init__(self, factory: str, syspath: Any = (), **kw: Any) -> None:
        super().__init__()
        self.factory, self.syspath = factory, tuple(syspath)
        _Spy.made.append(self)


def test_the_child_is_built_from_the_factory_and_syspath_and_closed_with_the_server(tmp_path: Path, monkeypatch):
    _Spy.made = []
    monkeypatch.setattr(client_mod, "DeriveChild", _Spy)
    httpd = _serve(tmp_path, monkeypatch, derive_factory="m:f", derive_syspath=("/q",))
    try:
        httpd._open_snapshot_host(stub.factory())
        host = httpd._snapshot_host
        assert host is not None and len(_Spy.made) == 1
        assert (_Spy.made[0].factory, _Spy.made[0].syspath) == ("m:f", ("/q",)) and host._child is _Spy.made[0]
        assert not _Spy.made[0].closed
    finally:
        httpd.server_close()
    assert _Spy.made[0].closed >= 1


def test_a_store_another_writer_holds_closes_the_child_made_for_the_host_that_did_not_open(tmp_path: Path, monkeypatch):
    from levain.cockpit.snapshots import SnapshotWriter

    _Spy.made = []
    monkeypatch.setattr(client_mod, "DeriveChild", _Spy)
    httpd = _serve(tmp_path, monkeypatch, derive_factory="m:f")
    holder = SnapshotWriter(httpd.broker.data_root / "snapshots.db")
    try:
        httpd._open_snapshot_host(stub.factory())
        assert httpd._snapshot_host is None and len(_Spy.made) == 1 and _Spy.made[0].closed >= 1
    finally:
        holder.close()
        httpd.server_close()


# ------------------------------------------------------------------------------------------------ L3 round 1: the fixes


def test_a_panel_the_child_does_not_hold_is_never_routed_or_promoted(make_host):
    """The child serves only what its factory built. A heavy panel, or a slow one, that the child's ready frame does not list
    derives here every time and is never promoted (before: it was routed, the child answered "no longer registered" and that was
    published as the panel's row)."""
    child = FakeChild(known={"fast"})
    host = make_host(heavy={"fast", "partial"}, derive_child=child, timer=Timer([900.0] * 9))
    assert [host._routed(p) for p in ("fast", "partial")] == [True, False]
    assert host._derive("partial").status == "partial"
    for _ in range(3):
        host._derive("count")                       # slow, and not held by the child
    assert not host._routed("count") and "count" not in host.routed_panels() and host._windows == {}
    assert child.asked() == [] and child.warmed >= 1, "an unknown panel starts the child's warm-up and runs here meanwhile"


def test_a_child_that_says_it_lacks_a_panel_is_no_verdict_on_the_panel(make_host):
    """If the child answers "the panel is no longer registered" for a panel the parent has, that is a failed ask: this derive
    runs here (counted), the panel is not routed again, and the loop is never told to retire it."""
    child = FakeChild(lambda p, t: ChildAnswer("error", None, None, 1, None, sh.UNREGISTERED_TEXT))
    host = make_host(heavy={"fast"}, derive_child=child)
    res = host._derive("fast")
    assert res.status == "ok" and [r["title"] for r in res.body["rows"]] == ["row 1", "row 2", "row 3"]
    assert host.child_fallbacks == 1 and not host._routed("fast") and host._cockpit.spec("fast") is not None
    assert "fast" in {p.id for p in host._plans()} and "fast" in host.loop._plans, "the loop still derives it"
    host._derive("fast")
    assert child.asked() == ["fast"], "asked once, then never again"


def test_a_hung_panel_is_not_run_here_while_the_child_backs_off(make_host):
    """After a timeout that killed the child, the panel's next derives are refused by the client; they answer the timeout row
    and never run the provider in this process. A refusal for a panel that did not hang, or any other failure, still falls back."""
    seq = iter([lost("timeout", hung=True), lost("refused"), lost("refused"), lost("died"), lost("refused")])
    child = FakeChild(lambda p, t: next(seq))
    host = make_host(heavy={"fast"}, derive_child=child)
    ran: list[str] = []
    real = host._cockpit.derive_panel
    host._cockpit.derive_panel = lambda pid: (ran.append(pid), real(pid))[1]       # type: ignore[method-assign]
    for _ in range(3):
        res = host._derive("fast")
        assert res.status == "timeout" and "still running after" in (res.error or "")
    assert ran == [] and host.child_fallbacks == 0
    assert host._derive("fast").status == "ok" and ran == ["fast"] and host.child_fallbacks == 1      # died: a crash falls back
    assert host._derive("fast").status == "ok" and host.child_fallbacks == 2, "the hang is forgotten once the child answered otherwise"


def test_a_timeout_that_was_only_a_wait_is_not_a_hang(make_host):
    """A turn wait or a cold start that ran out (``hung`` False) kills nothing, so a later refusal falls back as usual."""
    seq = iter([lost("timeout"), lost("refused")])
    host = make_host(heavy={"fast"}, derive_child=FakeChild(lambda p, t: next(seq)))
    assert host._derive("fast").status == "timeout"
    assert host._derive("fast").status == "ok" and host.child_fallbacks == 1


def test_stop_racing_a_routed_derive_leaves_the_last_good_row(make_host):
    """``stop`` closes the child under a derive that is waiting on it; the synthetic "host is stopping" result is not stored
    over the panel's last good row (before: the loop published it as the panel's error)."""
    import threading

    from levain.cockpit.snapshots import SnapshotReader

    entered, release = threading.Event(), threading.Event()
    mode = {"hold": False}

    def answer(pid: str, t: float) -> ChildAnswer:
        if mode["hold"] and pid == "fast":
            entered.set()
            release.wait(20)
            return lost("died")
        return ChildAnswer("ok", {"v": 1, "marker": "child"}, None, 5)

    child = FakeChild(answer)
    child.close = lambda: (child.__setattr__("closed", child.closed + 1), release.set())[1]       # type: ignore[method-assign]
    host = make_host(heavy={"fast"}, derive_child=child)
    host.run_once()
    good = host.reader.read("fast")
    assert good is not None and good.status == "ok"
    mode["hold"] = True
    host.loop.dirty(["fast"], 1)
    host.start()
    assert entered.wait(20), "the routed derive is waiting on the child"
    assert host.stop(timeout=20) == []
    after = SnapshotReader(host._writer.path).read("fast")
    assert after is not None and after.status == "ok" and after.error is None and after.body_sha == good.body_sha
    assert "stopping" not in (after.error or ""), after


def test_only_servable_ids_are_remembered_and_they_are_dropped_when_they_leave_the_plan_set(make_host):
    """2,000 ids that a retiring discoverer comes and goes with, each slow, are neither measured nor promoted; and a panel
    that was promoted is forgotten when it leaves the plan set (so an id reused later starts clean)."""
    ck = Cockpit()
    ck.register(_simple("plain"))
    ids = [0]

    def discoverer(ctx):
        ids[0] += 1
        return [_simple(f"d{ids[0] * 1000 + n}") for n in range(50)]

    ck.discover(discoverer, retire=True)
    child = FakeChild()
    host = make_host(ck, derive_child=child, timer=Timer([900.0] * 3000))
    seen = set()
    for _ in range(50):
        host._derive_discovery()
        for pid in list(ck.panel_ids):
            if pid.startswith("d"):
                seen.add(pid)
                host._derive(pid)
    assert len(seen) >= 2000
    assert host._windows == {} and host._promoted == set() and host._child_lacks == set() and host._hung == set()
    # a servable panel IS measured and promoted, then forgotten when it leaves the plan set
    for _ in range(3):
        host._derive("plain")
    assert "plain" in host._promoted and "plain" in host._windows
    ck.unregister("plain")
    host._derive_discovery()
    assert "plain" not in host._promoted and "plain" not in host._windows


def test_the_childs_ready_list_gates_a_real_child_too(make_host, procs):
    """With a real child: a panel its factory built is routed; one the parent registered on its own (the factory never saw
    it) is not, however slow, and the heavy name does not change that."""
    c = procs.client()
    assert c.start(30.0)
    ck = stub.factory()
    ck.register(_simple("only_here"))
    host = make_host(ck, heavy={"fast", "only_here"}, derive_child=c)
    assert host._routed("fast") and not host._routed("only_here")
    res = host._derive("only_here")
    assert res.status in ("ok", "empty") and "marker" not in (res.body or {}) and host.child_fallbacks == 0


# ------------------------------------------------------------------------------------------------ make_server: the environment never overrides an argument


def test_an_explicit_empty_argument_is_not_overridden_by_the_environment(tmp_path: Path, monkeypatch):
    from levain.web_server import _resolve_derive_child

    env = {COCKPIT_DERIVE_FACTORY_ENV: "m:envf", COCKPIT_DERIVE_SYSPATH_ENV: "/x"}
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    assert _resolve_derive_child(None, None) == ("m:envf", ("/x",))
    assert _resolve_derive_child("", None) == (None, ()), "an empty factory is an explicit none"
    assert _resolve_derive_child("m:arg", None) == ("m:arg", ()), "the environment's directories belong to the environment's factory"
    assert _resolve_derive_child("m:arg", ()) == ("m:arg", ())
    assert _resolve_derive_child("m:arg", ["/q", ""]) == ("m:arg", ("/q",))
    assert _resolve_derive_child(None, ("/own",)) == ("m:envf", ("/own",))
    monkeypatch.setenv(COCKPIT_DERIVE_FACTORY_ENV, "   ")
    assert _resolve_derive_child(None, None) == (None, ()), "a blank factory in the environment is no factory"

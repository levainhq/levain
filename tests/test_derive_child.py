"""The warm derive child and its parent-side client, over the real child process and a stub factory (tests/test_derive_child_stub.py).

Each test names the bug it is built to catch. The only process a test starts is the stub child; the client's own fixture
ends every child it made, and the tests assert a killed child is gone (``os.kill(pid, 0)`` raises: a zombie would pass it).
"""

from __future__ import annotations

import json
import os
import signal
import threading
import time
from dataclasses import FrozenInstanceError
from pathlib import Path
from typing import Any

import pytest

from levain.cockpit import engine
from levain.cockpit.derive_child_client import ChildAnswer, DeriveChild, _Lost, backoff_s
from levain.cockpit.snapshot_host import SnapshotHost

TESTS_DIR = str(Path(__file__).resolve().parent)
FACTORY = "test_derive_child_stub:factory"


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


class Rig:
    """The clients a test made, the pids they ran, and the stub's flag directory."""

    def __init__(self, tmp: Path) -> None:
        self.tmp = tmp
        self.clients: list[DeriveChild] = []
        self.pids: set[int] = set()
        self.err = open(tmp / "child.stderr", "w+")

    def client(self, factory: str = FACTORY, **kw: Any) -> DeriveChild:
        kw.setdefault("backoff_base_s", 0.3)
        kw.setdefault("term_grace_s", 2.0)
        c = DeriveChild(factory, syspath=[TESTS_DIR], stderr=self.err, **kw, env=dict(
            os.environ, DERIVE_CHILD_STUB_DIR=str(self.tmp), DERIVE_CHILD_STUB_HELPERS="1", DERIVE_CHILD_STUB_SLOW_S="5"))
        self.clients.append(c)
        return c

    def ask(self, c: DeriveChild, panel: str, timeout_s: float = 20.0) -> ChildAnswer:
        got = c.derive(panel, timeout_s)
        if c.pid is not None:
            self.pids.add(c.pid)
        return got

    def hang_flag(self) -> Path:
        flag = self.tmp / "hang.flag"
        flag.write_text("x")
        return flag

    def stderr_text(self) -> str:
        self.err.flush()
        return (self.tmp / "child.stderr").read_text()


@pytest.fixture
def rig(tmp_path: Path):
    r = Rig(tmp_path)
    try:
        yield r
    finally:
        (tmp_path / "hang.flag").unlink(missing_ok=True)
        for c in r.clients:
            try:
                c.close()
            except Exception:  # noqa: BLE001
                pass
            if c._proc is not None:             # a child close() could not end: never leave one running
                try:
                    os.killpg(c._proc.pid, signal.SIGKILL)
                except ProcessLookupError:      # it died uncollected: the teardown must not mask the test's own failure
                    pass
        for pid in r.pids:
            if _alive(pid):
                try:
                    os.killpg(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        gpid = (tmp_path / "grandchild.pid")
        if gpid.exists():                       # a SIGTERM-proof helper a test started: never leave one running
            try:
                os.kill(int(gpid.read_text()), signal.SIGKILL)
            except (ProcessLookupError, ValueError):
                pass
        r.err.close()


def _rows(ans: ChildAnswer) -> list[str]:
    return [r["title"] for r in ans.body["rows"]]


def _text(ans: ChildAnswer) -> str:
    return json.dumps(ans.body)


# ------------------------------------------------------------------------------------------------ the round trip


def test_round_trip(rig: Rig):
    """A derive comes back whole: status, a body of rows, the panel's declared bound as epoch seconds, a measured duration."""
    c = rig.client()
    assert c.pid is None, "the child starts on the first ask, not at construction"
    ans = rig.ask(c, "fast")
    assert ans.failure is None and ans.status == "ok" and ans.error is None
    assert _rows(ans) == ["row 1", "row 2", "row 3"]
    assert ans.valid_until == 4070908800.0                  # 2099-01-01T00:00:00Z
    assert isinstance(ans.duration_ms, int) and ans.duration_ms >= 0
    with pytest.raises(FrozenInstanceError):
        ans.status = "x"                                    # type: ignore[misc]


def test_child_derivation_equals_the_in_process_one(rig: Rig, tmp_path: Path):
    """One derivation, two hosts: a second copy of "how a panel is derived" in the child would show here as a difference in
    status, body bytes, error or valid_until against what the in-process host makes for the same panel."""
    from tests import test_derive_child_stub as stub

    store = tmp_path / "store"
    store.mkdir()
    host = SnapshotHost(stub.factory(), store)
    try:
        c = rig.client()
        statuses = set()
        # an unregistered panel answers the same error in both hosts; "boom" is left out: its error text carries a clock reading
        for panel in ("fast", "partial", "nothing_registered"):
            local = host._derive(panel)
            statuses.add(local.status)
            got = rig.ask(c, panel)
            assert got.failure is None
            assert got.status == local.status, panel
            assert json.dumps(got.body, sort_keys=True) == json.dumps(local.body, sort_keys=True), panel
            assert got.valid_until == local.valid_until, panel
            assert got.error == local.error, panel
        assert statuses == {"ok", "partial", "error"}, "the panels must cover more than one status, or the guard guards little"
    finally:
        host.stop(timeout=5)


def test_the_child_runs_the_parents_install(rig: Rig):
    """The child imports levain from the directory the parent imported it from, not from whatever the interpreter finds first."""
    ans = rig.ask(rig.client(), "pid")
    assert ans.status == "ok"
    pid_text = ans.body["rows"][0]["title"]
    assert pid_text.endswith(engine.__file__)
    assert int(pid_text.split()[0]) != os.getpid()


def test_the_child_is_warm(rig: Rig):
    """A provider's state lives on between derives: the second derive is the same process and counts on from the first."""
    c = rig.client()
    first, pid1 = rig.ask(c, "count"), c.pid
    second, pid2 = rig.ask(c, "count"), c.pid
    assert pid1 == pid2 and pid1 is not None
    assert [_rows(first), _rows(second)] == [["call 1"], ["call 2"]]


# ------------------------------------------------------------------------------------------------ a derive that fails


def test_a_failing_derive_is_an_answer_and_the_child_lives_on(rig: Rig):
    """A provider that raises, a derive that raises, an unregistered panel and a pseudo-plan id are each an error ANSWER
    (failure None, status error): none costs the child, and none sets a backoff, so the next ask is at once."""
    c = rig.client()
    pid = None
    for panel, text in (("boom", "RuntimeError"), ("explode", "ValueError: the stub's derive_panel blew up"),
                        ("nothing_registered", "no longer registered"), ("_entity", "not a panel this child derives")):
        ans = rig.ask(c, panel)
        assert ans.failure is None and ans.status == "error", (panel, ans)
        assert text in (ans.error or ""), (panel, ans)
        assert pid in (None, c.pid), "the child must be the same process after every failed derive"
        pid = c.pid
    assert rig.ask(c, "fast").status == "ok" and c.pid == pid


# ------------------------------------------------------------------------------------------------ supervision


def test_a_hung_derive_is_killed_at_its_deadline_and_the_next_child_is_fresh(rig: Rig):
    """A derive past ``timeout_s`` is a typed timeout and its child is gone (not a zombie, not orphaned); a call inside the
    backoff is refused without starting anything; after the backoff the next call runs on a new process."""
    c = rig.client(backoff_base_s=0.6)
    rig.hang_flag()
    assert rig.ask(c, "count").failure is None
    old = c.pid
    assert old is not None
    t0 = time.monotonic()
    ans = rig.ask(c, "hang", timeout_s=1.0)
    took = time.monotonic() - t0
    assert ans.failure == "timeout" and ans.status is None and ans.body is None
    assert 0.9 <= took < 6.0, f"killed at the deadline, not before and not long after: {took:.2f}s"
    assert not _alive(old), "the timed-out child must be gone and reaped"
    assert c.pid is None
    refused = rig.ask(c, "fast")
    assert refused.failure == "refused" and c.pid is None, "inside the backoff nothing is started"
    time.sleep(0.7)
    (rig.tmp / "hang.flag").unlink()
    fresh = rig.ask(c, "count")
    assert fresh.failure is None and _rows(fresh) == ["call 1"], "a fresh child counts from one"
    assert c.pid is not None and c.pid != old


def test_a_child_killed_from_outside_is_replaced_on_the_next_call(rig: Rig):
    """A SIGKILL between asks: the next ask finds it dead, reaps it and starts another at once (no failure was a derive's)."""
    c = rig.client()
    assert rig.ask(c, "count").failure is None
    old = c.pid
    assert old is not None
    proc = c._proc
    os.kill(old, signal.SIGKILL)
    assert proc is not None and proc.wait(timeout=10) == -signal.SIGKILL
    ans = rig.ask(c, "count")
    assert ans.failure is None and _rows(ans) == ["call 1"]
    assert c.pid is not None and c.pid != old and not _alive(old)


def test_a_child_killed_during_a_derive_answers_died(rig: Rig):
    """The child dying under a running derive is a typed ``died`` at once (not a wait for the deadline), the child is reaped,
    and after the backoff the next call is answered by a new one."""
    c = rig.client()
    rig.hang_flag()
    assert rig.ask(c, "count").failure is None
    old = c.pid
    assert old is not None
    threading.Timer(0.5, lambda: os.kill(old, signal.SIGKILL)).start()
    t0 = time.monotonic()
    ans = rig.ask(c, "hang", timeout_s=30.0)
    assert ans.failure == "died" and time.monotonic() - t0 < 10.0
    assert "exited" in (ans.error or "")
    assert not _alive(old)
    time.sleep(0.4)                                     # past the 0.3 s backoff
    (rig.tmp / "hang.flag").unlink()
    assert rig.ask(c, "fast").failure is None


def test_a_factory_that_cannot_build_is_died_then_backed_off(rig: Rig):
    """A child that cannot build its cockpit says why, is not retried at once, and never raises into the caller."""
    c = rig.client("test_derive_child_stub:failing_factory", backoff_base_s=0.5)
    first = rig.ask(c, "fast")
    assert first.failure == "died" and "the stub factory cannot build" in (first.error or "")
    second = rig.ask(c, "fast")
    assert second.failure == "refused"
    time.sleep(0.6)
    assert rig.ask(c, "fast").failure == "died", "after the backoff the client tries again"


def test_a_factory_that_does_not_exist_is_died(rig: Rig):
    c = rig.client("no_such_stub_module:factory")
    got = rig.ask(c, "fast")
    assert got.failure == "died" and "no_such_stub_module" in (got.error or "")


def test_the_backoff_doubles_and_stops_at_the_cap():
    """1 s, 2 s, 4 s ... and never past 30 s, however many failures there have been."""
    got = [backoff_s(n, 1.0, 30.0) for n in (1, 2, 3, 4, 5, 6, 7, 100, 10_000)]
    assert got == [1.0, 2.0, 4.0, 8.0, 16.0, 30.0, 30.0, 30.0, 30.0]


def test_a_malformed_answer_is_bad_reply_not_an_exception(rig: Rig):
    """What the child sends is checked before the loop sees it. A line that is not JSON, an answer to another request, a
    status that is not a string, a bound that is not a number: each is ``bad_reply`` (the caller then kills the child)."""
    c = rig.client()
    good = {"id": 7, "status": "ok", "body": {}, "valid_until": None, "error": None, "duration_ms": 3}
    assert c._answer(dict(good), 7).status == "ok"
    for broken in ({**good, "id": 8}, {**good, "status": 5}, {**good, "valid_until": "soon"}, {**good, "valid_until": True},
                   {**good, "duration_ms": 1.5}, {**good, "error": 3}, ["not", "an", "object"], None):
        with pytest.raises(_Lost) as lost:
            c._answer(broken, 7)
        assert lost.value.kind == "bad_reply", broken
    r, w = os.pipe()

    class _Pipe:
        stdout = os.fdopen(r, "rb")

    c._proc = _Pipe()                                   # type: ignore[assignment]  # a stand-in for the child's pipe: no process
    try:
        os.write(w, b"this is not json\n")
        with pytest.raises(_Lost) as lost:
            c._frame(time.monotonic() + 2, "late")
        assert lost.value.kind == "bad_reply"
        with pytest.raises(_Lost) as lost:
            c._frame(time.monotonic() + 0.2, "nothing came")
        assert lost.value.kind == "timeout"
    finally:
        c._proc = None
        os.close(w)
        _Pipe.stdout.close()


# ------------------------------------------------------------------------------------------------ the protocol's own


def test_a_providers_stdout_cannot_corrupt_the_protocol(rig: Rig):
    """A ``print``, a raw write to fd 1 and a ``sys.stdout.write`` inside a provider: the frames stay whole (before the
    redirect, the first stray byte was read as the answer and made a ``bad_reply``), and the text lands on stderr."""
    c = rig.client()
    ans = rig.ask(c, "chatty")
    assert ans.failure is None and ans.status == "ok" and _rows(ans) == ["chatty"]
    assert rig.ask(c, "fast").failure is None
    err = rig.stderr_text()
    for stray in ("a stray print from a provider", "a raw write to fd 1", "a stray write to sys.stdout"):
        assert stray in err


def test_stdin_eof_ends_the_child(rig: Rig):
    """The parent's pipe closing (however the parent went) is the child's signal to exit, with status 0."""
    c = rig.client()
    assert rig.ask(c, "fast").failure is None
    proc = c._proc
    assert proc is not None
    proc.stdin.close()                                  # type: ignore[union-attr]
    assert proc.wait(timeout=10) == 0


def test_a_stop_request_ends_the_child(rig: Rig):
    c = rig.client()
    assert rig.ask(c, "fast").failure is None
    proc = c._proc
    assert proc is not None
    os.write(proc.stdin.fileno(), b'{"op":"stop"}\n')   # type: ignore[union-attr]
    assert proc.wait(timeout=10) == 0


def test_close_is_idempotent_and_ends_the_child(rig: Rig):
    """``close`` twice (and once on a client that never started a child) is fine; the child is gone; later asks are refused."""
    never = rig.client()
    never.close()
    never.close()
    c = rig.client()
    assert rig.ask(c, "fast").failure is None
    pid = c.pid
    assert pid is not None
    c.close()
    c.close()
    assert not _alive(pid) and c.pid is None
    assert c.derive("fast", 5.0).failure == "refused"


# ------------------------------------------------------------------------------------------------ one at a time


def test_concurrent_asks_take_turns(rig: Rig):
    """Eight threads ask at once: each gets one whole answer and the provider counted 1..8 with no repeat (two derives
    interleaved on the one pipe would repeat a count or mix a frame)."""
    c = rig.client()
    assert rig.ask(c, "fast").failure is None           # started: the threads race the asks, not the spawn
    out: list[ChildAnswer] = []
    lock = threading.Lock()

    def one() -> None:
        got = c.derive("count", 30.0)
        with lock:
            out.append(got)

    threads = [threading.Thread(target=one) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert [a.failure for a in out] == [None] * 8
    assert sorted(_rows(a)[0] for a in out) == sorted(f"call {n}" for n in range(1, 9))


def test_a_caller_waiting_its_turn_is_bounded(rig: Rig):
    """While one derive holds the child, a second caller waits at most its own ``timeout_s`` and then gets a typed timeout,
    and the first derive, which the wait did not touch, still completes when its provider does."""
    c = rig.client()
    rig.hang_flag()
    assert rig.ask(c, "fast").failure is None
    first: list[ChildAnswer] = []
    t = threading.Thread(target=lambda: first.append(c.derive("hang", 30.0)))
    t.start()
    time.sleep(0.5)                                     # the first ask holds the turn
    t0 = time.monotonic()
    second = c.derive("fast", 0.5)
    waited = time.monotonic() - t0
    assert second.failure == "timeout" and "turn" in (second.error or "")
    assert 0.4 <= waited < 3.0
    (rig.tmp / "hang.flag").unlink()
    t.join(timeout=30)
    assert first and first[0].failure is None and _rows(first[0]) == ["released"], "the wait must not kill the child"


# ------------------------------------------------------------------------------------------------ L3 round 1: the fixes


def _gone(pid: int, within_s: float = 8.0) -> bool:
    end = time.monotonic() + within_s
    while time.monotonic() < end:
        if not _alive(pid):
            return True
        time.sleep(0.05)
    return not _alive(pid)


def _grandchild_pid(tmp: Path) -> int:
    return int((tmp / "grandchild.pid").read_text())


def test_a_helper_that_ignores_sigterm_is_gone_after_close(rig: Rig):
    """A provider's subprocess that ignores SIGTERM, left behind when ``close`` reaped the leader that had exited on its own
    (before: the group was never signalled once the leader was gone, so the helper outlived the child)."""
    c = rig.client()
    assert rig.ask(c, "grandchild").failure is None
    helper = _grandchild_pid(rig.tmp)
    assert _alive(helper)
    c.close()
    assert _gone(helper), "the whole process group must end with the child"


def test_a_helper_that_ignores_sigterm_is_gone_after_a_timeout_kill(rig: Rig):
    """The leader dies on SIGTERM at once, so the escalation to SIGKILL used to be skipped and the helper survived."""
    c = rig.client()
    rig.hang_flag()
    assert rig.ask(c, "grandchild").failure is None
    helper = _grandchild_pid(rig.tmp)
    assert rig.ask(c, "hang", timeout_s=1.0).failure == "timeout"
    assert _gone(helper)


def test_a_helper_is_gone_when_the_leader_exited_by_itself(rig: Rig):
    """The leader leaves on a stop request (no signal from the client); the next ask finds it gone and must end the group too."""
    c = rig.client()
    assert rig.ask(c, "grandchild").failure is None
    helper = _grandchild_pid(rig.tmp)
    proc = c._proc
    assert proc is not None
    os.write(proc.stdin.fileno(), b'{"op":"stop"}\n')        # type: ignore[union-attr]
    time.sleep(1.0)                                         # the leader is a zombie: nobody has reaped it
    assert rig.ask(c, "fast").failure is None
    assert _gone(helper)


def _scratch_parent(rig: Rig, factory_sleep: str) -> "tuple[Any, int]":
    """A parent process that starts a child whose factory is slow, and prints the child's pid; ``(parent, child_pid)``."""
    import subprocess
    import sys

    root = str(Path(engine.__file__).resolve().parents[2])
    script = rig.tmp / "scratch_parent.py"
    script.write_text(
        "import os, sys, threading, time\n"
        "from levain.cockpit.derive_child_client import DeriveChild\n"
        "from levain.launch import child_env\n"
        f"c = DeriveChild({FACTORY.replace('factory', 'slow_factory')!r}, syspath=[{TESTS_DIR!r}], ready_timeout_s=600, "
        "env=child_env(prefixes=('DERIVE_CHILD_STUB_',)))\n"
        "threading.Thread(target=lambda: c.derive('fast', 600), daemon=True).start()\n"
        "while c._proc is None:\n"
        "    time.sleep(0.05)\n"
        "print(c._proc.pid, flush=True)\n"
        "time.sleep(600)\n")
    env = dict(os.environ, PYTHONPATH=os.pathsep.join([root, TESTS_DIR]), DERIVE_CHILD_STUB_DIR=str(rig.tmp),
               DERIVE_CHILD_STUB_FACTORY_SLEEP=factory_sleep)
    parent = subprocess.Popen([sys.executable, "-P", str(script)], stdout=subprocess.PIPE, env=env)
    assert parent.stdout is not None
    child_pid = int(parent.stdout.readline())
    rig.pids.add(child_pid)
    return parent, child_pid


def test_a_child_whose_parent_dies_during_a_slow_factory_leaves_no_process(rig: Rig):
    """The parent is killed while the factory is still building. The watchdog used to start only after the factory returned,
    so a factory that took long (or never returned) kept an orphan, and its helper, for as long as it ran."""
    parent, child_pid = _scratch_parent(rig, "600")
    try:
        end = time.monotonic() + 20
        while not (rig.tmp / "grandchild.pid").exists() and time.monotonic() < end:
            time.sleep(0.05)                                # the factory is running now, and hung
        helper = _grandchild_pid(rig.tmp)
        assert _alive(child_pid) and _alive(helper)
        parent.kill()
        parent.wait(timeout=10)
        assert _gone(child_pid), "the orphan must exit while its factory is still running"
        assert _gone(helper), "and take its process group with it"
    finally:
        parent.kill()
        parent.wait(timeout=10)


def test_a_ready_frame_of_another_protocol_is_died(rig: Rig, monkeypatch):
    """A child of another protocol version (a package updated on disk under a running parent) is refused at the ready frame,
    not talked to."""
    from levain.cockpit import derive_child_client as cc

    monkeypatch.setattr(cc, "PROTOCOL", "levain-derive-child/0")
    got = rig.ask(rig.client(), "fast")
    assert got.failure == "died" and "protocol" in (got.error or "")


def test_the_ready_frame_lists_the_panels_the_child_holds_and_knows_says_so(rig: Rig):
    c = rig.client()
    assert c.knows("fast") is False, "nothing is known of a child that has not started"
    assert rig.ask(c, "fast").failure is None
    assert c.knows("fast") and c.knows("count") and not c.knows("nothing_registered") and not c.knows("_entity")
    assert c.knows(5) is False                              # type: ignore[arg-type]


def test_the_ready_frames_panel_list_is_bounded(rig: Rig):
    from levain.cockpit import derive_child_client as cc

    c = rig.client("test_derive_child_stub:many_factory")
    c._env["DERIVE_CHILD_STUB_MANY"] = str(cc.READY_PANELS_MAX + 7)
    assert c.start(30.0)
    assert len(c._panels) == cc.READY_PANELS_MAX and c.knows("m00000") and not c.knows(f"m{cc.READY_PANELS_MAX + 6:05d}")


def test_a_request_that_is_json_but_not_an_object_is_answered_not_fatal(rig: Rig):
    """``null``, a list, a number and a non-finite id each get an error answer and the child lives on (before: an
    AttributeError, or a ValueError in the frame writer, ended it)."""
    c = rig.client()
    assert rig.ask(c, "fast").failure is None
    proc = c._proc
    assert proc is not None and proc.stdin is not None
    os.write(proc.stdin.fileno(), b'null\n[1,2]\n42\n{"id": NaN, "op": "nope"}\n{"id": Infinity, "op": "derive", "panel": "fast"}\n')
    seen = [c._frame(time.monotonic() + 10, "an answer") for _ in range(5)]
    assert all(f["status"] == "error" for f in seen[:4]) and seen[4]["status"] == "ok", seen
    assert all(f["id"] is None for f in seen), "an id that is not a plain integer or string is not echoed"
    assert proc.poll() is None and rig.ask(c, "fast").failure is None


@pytest.mark.parametrize("bad", [float("inf"), float("nan"), -1.0, 0.0, "5", None, True, 10**1000, -(10**1000)],
                         ids=lambda v: repr(v)[:24])
def test_a_timeout_that_is_not_a_positive_finite_number_is_refused_and_touches_nothing(rig: Rig, bad):
    """``derive(p, inf)`` raised out of the lock; ``derive(p, 0)`` killed a healthy child and set a backoff. Neither may
    happen: a typed ``refused``, the same child, and the very next valid ask is answered at once."""
    c = rig.client(backoff_base_s=30.0)
    assert rig.ask(c, "count").failure is None
    pid = c.pid
    got = c.derive("count", bad)                            # type: ignore[arg-type]
    assert got.failure == "refused" and "timeout_s" in (got.error or "")
    assert c.pid == pid
    assert _rows(rig.ask(c, "count")) == ["call 2"], "the child is the warm one, with no backoff set"


def test_one_deadline_covers_the_spawn_and_a_cold_start_is_not_killed_by_a_short_call(rig: Rig):
    """A one-second ask of a child whose factory takes three seconds comes back near one second (before: the ready wait had its
    own thirty and the answer its own one), and the child, which has done nothing wrong, is left starting for the next ask."""
    c = rig.client(backoff_base_s=30.0)
    c._env["DERIVE_CHILD_STUB_FACTORY_SLEEP"] = "3"
    c._argv[c._argv.index("--factory") + 1] = "test_derive_child_stub:slow_factory"
    t0 = time.monotonic()
    got = c.derive("fast", 1.0)
    took = time.monotonic() - t0
    assert got.failure == "timeout" and took < 2.0, (got, took)
    assert not got.hung, "a derive that never started is not a hung panel"
    assert c._proc is not None and c._proc.poll() is None, "the cold start goes on"
    ok = c.derive("fast", 20.0)
    assert ok.failure is None and ok.status == "ok", ok


def test_the_wait_for_the_turn_is_part_of_the_deadline(rig: Rig):
    """B asks for a two-second deadline, waits about a second for A's turn, and its derive then runs five: it ends near two
    seconds from its call (before: the answer wait began again at two, so near three)."""
    c = rig.client()
    rig.hang_flag()
    assert rig.ask(c, "fast").failure is None
    a = threading.Thread(target=lambda: c.derive("hang", 30.0))
    a.start()
    time.sleep(0.3)                                         # A holds the turn
    threading.Timer(1.0, lambda: (rig.tmp / "hang.flag").unlink(missing_ok=True)).start()
    t0 = time.monotonic()
    got = c.derive("slow5", 2.0)
    took = time.monotonic() - t0
    a.join(timeout=30)
    assert got.failure == "timeout" and got.hung, got
    assert 1.8 <= took < 2.7, f"one deadline for the wait and the answer together: {took:.2f}s"


def test_close_while_a_derive_runs_cuts_it_short_and_leaves_no_child(rig: Rig):
    """``close`` from another thread while a derive holds the turn: the derive answers ``died`` (its pipe is not closed under
    it), the child is gone, and the holder's own reap leaves no zombie."""
    c = rig.client(term_grace_s=0.5)
    rig.hang_flag()
    assert rig.ask(c, "fast").failure is None
    pid = c.pid
    assert pid is not None
    out: list[ChildAnswer] = []
    t = threading.Thread(target=lambda: out.append(c.derive("hang", 30.0)))
    t.start()
    time.sleep(0.5)
    c.close()
    t.join(timeout=20)
    assert out and out[0].failure in ("died", "refused") and _gone(pid)
    assert c._proc is None


def test_close_during_a_cold_start_leaves_no_child(rig: Rig):
    """``close`` while a derive is still waiting for the factory: nothing is left running when both have returned."""
    c = rig.client(term_grace_s=0.5)
    c._env["DERIVE_CHILD_STUB_FACTORY_SLEEP"] = "30"
    c._argv[c._argv.index("--factory") + 1] = "test_derive_child_stub:slow_factory"
    out: list[ChildAnswer] = []
    t = threading.Thread(target=lambda: out.append(c.derive("fast", 60.0)))
    t.start()
    time.sleep(1.0)
    proc = c._proc
    assert proc is not None
    pid = proc.pid
    rig.pids.add(pid)
    c.close()
    t.join(timeout=20)
    assert out and out[0].failure in ("died", "refused") and _gone(pid)
    assert c._proc is None


def test_a_child_is_not_asked_through_a_descriptor_past_the_select_limit(rig: Rig):
    """The parent has more than 1024 descriptors open (a busy serve does): ``select.select`` raised ValueError on the child's
    pipe and every ask was ``died``. A poll-based wait has no such limit."""
    import resource

    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    want = 1400
    if hard != resource.RLIM_INFINITY and hard < want:
        pytest.skip("the hard descriptor limit is too low to open 1100 descriptors")
    if soft != resource.RLIM_INFINITY and soft < want:
        resource.setrlimit(resource.RLIMIT_NOFILE, (want, hard))
    held = [os.open(os.devnull, os.O_RDONLY) for _ in range(1100)]
    try:
        c = rig.client()
        got = rig.ask(c, "fast")
        assert got.failure is None and got.status == "ok", got
        assert c._proc is not None and c._proc.stdout.fileno() > 1024      # type: ignore[union-attr]
    finally:
        for fd in held:
            os.close(fd)
        if soft != resource.RLIM_INFINITY and soft < want:
            resource.setrlimit(resource.RLIMIT_NOFILE, (soft, hard))


def test_close_tolerates_a_stdin_the_caller_closed(rig: Rig):
    """The caller closed the child's stdin (a test, a bug): ``close`` raised ValueError out of ``with`` and left the child."""
    c = rig.client()
    assert rig.ask(c, "fast").failure is None
    pid = c.pid
    assert pid is not None and c._proc is not None
    c._proc.stdin.close()                                   # type: ignore[union-attr]
    c.close()
    assert _gone(pid)


def test_a_request_is_written_whole_whatever_the_os_returns_for_one_write(rig: Rig, monkeypatch):
    """A short ``os.write`` once left half a request on the pipe, and the child waited for the newline until the deadline."""
    from levain.cockpit import derive_child_client as cc

    real = os.write
    calls = []

    def one_byte(fd: int, data: Any) -> int:
        calls.append(len(data))
        return real(fd, bytes(data)[:1])

    c = rig.client()
    assert rig.ask(c, "fast").failure is None
    monkeypatch.setattr(cc.os, "write", one_byte)
    got = c.derive("fast", 10.0)
    monkeypatch.undo()
    assert got.failure is None and got.status == "ok" and len(calls) > 3


# ------------------------------------------------------------------------------------------------ L3 round 2: the fixes


def test_a_holder_that_ends_normally_after_a_contended_close_reaps_the_child(rig: Rig):
    """``close`` cannot take the turn from a holder, kills the group and leaves the reap to it. A holder that goes on to end
    normally (it had read its answer, or timed out without cost) never killed anything, so the child stayed an unreaped zombie
    with pipes nobody owned. The holder now ends a child a closed client still holds before it lets go of the turn."""
    c = rig.client(term_grace_s=0.3)
    assert rig.ask(c, "fast").failure is None
    proc = c._proc
    assert proc is not None
    pid = proc.pid
    entered, go = threading.Event(), threading.Event()

    def held(panel_id: str, deadline: float, timeout_s: float) -> ChildAnswer:
        entered.set()
        go.wait(30)
        return ChildAnswer("ok", {"v": 1}, None, 1)         # the holder had its answer: it ends normally, never through _kill

    c._ask = held                                           # type: ignore[method-assign]
    out: list[ChildAnswer] = []
    t = threading.Thread(target=lambda: out.append(c.derive("fast", 30.0)))
    t.start()
    assert entered.wait(10)
    c.close()                                               # contended: the group is killed, the rest is the holder's
    go.set()
    t.join(timeout=20)
    assert out and out[0].failure is None
    assert c._proc is None, "the holder let go of a child nobody owns any more"
    assert proc.returncode is not None, "and the child was reaped, not left a zombie"
    assert not _alive(pid)


class _StuckProc:
    """A ``Popen`` whose leader cannot be reaped until ``release`` is set (a SIGKILLed process in an uninterruptible wait)."""

    def __init__(self) -> None:
        self.pid = 2 ** 22 + 12345                          # never signalled: the test replaces the signalling
        self.returncode: "int | None" = None
        self.stdin = self.stdout = None
        self.release = threading.Event()
        self.reaped = threading.Event()

    def poll(self) -> "int | None":
        if self.returncode is None and self.release.is_set():
            self.returncode = -9
            self.reaped.set()
        return self.returncode

    def wait(self, timeout: "float | None" = None) -> int:
        end = None if timeout is None else time.monotonic() + timeout
        while self.poll() is None:
            if end is not None and time.monotonic() >= end:
                import subprocess
                raise subprocess.TimeoutExpired("stuck", timeout)
            time.sleep(0.01)
        return self.returncode                              # type: ignore[return-value]


def test_a_leader_the_reap_gave_up_on_is_still_waited_for(monkeypatch):
    """``_reap`` gives up after its cap and ``_kill`` dropped the Popen, so a SIGKILLed leader that was slow to leave was
    never waited for and became a zombie. The Popen now goes to a reaper that waits for it however long that takes."""
    from levain.cockpit import derive_child_client as cc

    monkeypatch.setattr(cc, "_REAP_BOUND_S", 0.05)
    monkeypatch.setattr(cc, "_signal_group", lambda proc, sig: None)
    monkeypatch.setattr(cc, "_wait_exit_unreaped", lambda proc, grace: None)
    stuck = _StuckProc()
    c = DeriveChild("x:y", term_grace_s=0.0)
    c._proc = stuck                                         # type: ignore[assignment]
    t0 = time.monotonic()
    c._kill()
    assert time.monotonic() - t0 < 5 and c._proc is None, "the call itself still returns at its cap"
    assert not stuck.reaped.is_set()
    stuck.release.set()                                     # the leader finally leaves the kernel's wait
    assert stuck.reaped.wait(10), "somebody must still wait for it"


def test_a_start_that_hangs_in_the_factory_is_killed_at_the_ready_bound_and_backed_off(rig: Rig):
    """``start(ready_timeout_s)`` (what ``warm`` runs) took its deadline before the spawn and measured the ready bound from after
    it, so it gave up a moment BEFORE the bound and called that a cost-free wait: the hung child lived on and no backoff was set."""
    c = rig.client("test_derive_child_stub:slow_factory", ready_timeout_s=1.0, term_grace_s=0.5, backoff_base_s=30.0)
    c._env["DERIVE_CHILD_STUB_FACTORY_SLEEP"] = "600"
    assert c.start(1.0) is False
    assert c._proc is None, "the child that never said ready is killed"
    assert c._failures == 1 and c._not_before > time.monotonic(), "and the next one waits out a backoff"
    assert c.derive("fast", 5.0).failure == "refused"


def test_warm_kills_a_child_that_hangs_in_the_factory_and_sets_the_backoff(rig: Rig):
    c = rig.client("test_derive_child_stub:slow_factory", ready_timeout_s=1.0, term_grace_s=0.5, backoff_base_s=30.0)
    c._env["DERIVE_CHILD_STUB_FACTORY_SLEEP"] = "600"
    c.warm()
    end = time.monotonic() + 20
    while (c._warming or c._failures == 0) and time.monotonic() < end:
        time.sleep(0.05)
    assert c._proc is None and c._failures == 1 and c._not_before > time.monotonic()
    assert not c.knows("fast")


def test_a_short_start_that_is_not_the_ready_bound_is_still_a_wait_and_not_a_kill(rig: Rig):
    """The short deadline of a derive (or of a ``start`` that asks for less than the ready bound) leaves a still-building child to build."""
    c = rig.client("test_derive_child_stub:slow_factory", ready_timeout_s=60.0, backoff_base_s=30.0)
    c._env["DERIVE_CHILD_STUB_FACTORY_SLEEP"] = "3"
    assert c.start(0.5) is False
    assert c._proc is not None and c._proc.poll() is None and c._failures == 0
    assert c.start(30.0) is True and c.knows("fast")


# ---- the child's own exit when its parent is gone and the pipe says so


def _child_main(monkeypatch, *, factory, parent_gone: bool, break_out_after_ready: bool):
    """Run ``derive_child.main`` in this process over pipes (no watchdog, no stdio claim), with the parent said to be gone or not;
    returns ``(exit code, how often the orphan exit ran)``."""
    from levain.cockpit import derive_child as dc

    in_r, in_w = os.pipe()
    out_r, out_w = os.pipe()
    orphans: list[int] = []
    monkeypatch.setattr(dc, "_claim_stdio", lambda: (out_w, in_r))
    monkeypatch.setattr(dc, "_watch_parent", lambda parent, tick: None)
    monkeypatch.setattr(dc, "_load_factory", lambda spec: factory)
    monkeypatch.setattr(dc, "_parent_gone", lambda parent, settle: parent_gone)
    monkeypatch.setattr(dc, "_exit_orphan", lambda: orphans.append(1))
    if break_out_after_ready:
        def parent() -> None:
            with os.fdopen(out_r, "rb") as r:
                r.readline()                                # the ready frame
            os.write(in_w, b'{"id":1,"op":"derive","panel":"fast"}\n')     # the child's answer now has no reader
        threading.Thread(target=parent, daemon=True).start()
    else:
        os.close(out_r)                                     # nobody reads the child's first frame at all
    try:
        code = dc.main(["--factory", "x:y", "--parent-pid", str(os.getppid())])
    finally:
        for fd in (in_r, in_w):
            try:
                os.close(fd)
            except OSError:
                pass
    return code, len(orphans)


def test_a_child_answering_a_dead_parent_runs_the_orphan_exit(monkeypatch):
    """The broken pipe on an answer ended ``main`` normally and skipped the group cleanup, so a SIGTERM-proof helper outlived a
    child that left before its watchdog's next tick."""
    from tests import test_derive_child_stub as stub

    code, orphans = _child_main(monkeypatch, factory=stub.factory, parent_gone=True, break_out_after_ready=True)
    assert code == 0 and orphans == 1


def test_a_child_whose_factory_failed_to_a_dead_parent_runs_the_orphan_exit(monkeypatch):
    def failing():
        raise RuntimeError("cannot build")

    code, orphans = _child_main(monkeypatch, factory=failing, parent_gone=True, break_out_after_ready=False)
    assert orphans == 1


def test_a_broken_pipe_with_the_parent_still_there_is_no_orphan_exit(monkeypatch):
    from tests import test_derive_child_stub as stub

    code, orphans = _child_main(monkeypatch, factory=stub.factory, parent_gone=False, break_out_after_ready=True)
    assert code == 0 and orphans == 0, "the orphan exit kills the process group: it is for a parent that is gone"


def test_a_parent_killed_while_the_child_answers_leaves_no_process(rig: Rig):
    """The whole path in real processes: the parent dies while the child's derive runs, and neither the child nor its helper
    survives. (The watchdog covers this one as well; the unit tests above pin the pipe path alone.)"""
    import subprocess
    import sys

    root = str(Path(engine.__file__).resolve().parents[2])
    script = rig.tmp / "scratch_parent2.py"
    script.write_text(
        "import os, sys, threading, time\n"
        "from levain.cockpit.derive_child_client import DeriveChild\n"
        "from levain.launch import child_env\n"
        f"c = DeriveChild({FACTORY!r}, syspath=[{TESTS_DIR!r}], ready_timeout_s=60, "
        "env=child_env(prefixes=('DERIVE_CHILD_STUB_',)))\n"
        "assert c.derive('grandchild', 30).failure is None\n"
        "threading.Thread(target=lambda: c.derive('slow5', 60), daemon=True).start()\n"
        "print(c._proc.pid, flush=True)\n"
        "time.sleep(600)\n")
    env = dict(os.environ, PYTHONPATH=os.pathsep.join([root, TESTS_DIR]), DERIVE_CHILD_STUB_DIR=str(rig.tmp),
               DERIVE_CHILD_STUB_HELPERS="1", DERIVE_CHILD_STUB_SLOW_S="1.5")
    parent = subprocess.Popen([sys.executable, "-P", str(script)], stdout=subprocess.PIPE, env=env)
    try:
        assert parent.stdout is not None
        child_pid = int(parent.stdout.readline())
        rig.pids.add(child_pid)
        helper = _grandchild_pid(rig.tmp)
        time.sleep(0.5)                                     # the child is inside the slow derive now
        parent.kill()
        parent.wait(timeout=10)
        assert _gone(child_pid) and _gone(helper)
    finally:
        parent.kill()
        parent.wait(timeout=10)

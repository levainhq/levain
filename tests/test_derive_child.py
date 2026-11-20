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
        c = DeriveChild(factory, syspath=[TESTS_DIR], env=dict(os.environ, DERIVE_CHILD_STUB_DIR=str(self.tmp)),
                        stderr=self.err, **kw)
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
                os.killpg(c._proc.pid, signal.SIGKILL)
        for pid in r.pids:
            if _alive(pid):
                try:
                    os.killpg(pid, signal.SIGKILL)
                except ProcessLookupError:
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

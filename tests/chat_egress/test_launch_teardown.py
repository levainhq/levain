"""CH-D2 S8 r1 (RULINGS_R1 items 3 + 4), through the real launch (real glue + srt 0.0.81):

- item 3: a launch refused at any step after its first allocation leaves no descriptor, thread or glue behind (the
  listener, both socket pairs, both pipes, the SOCKS listener);
- item 4: teardown attempts every close whatever one of them raises (a glue wait that times out, a decider close
  that raises), reports the failures, and never replaces the exception that ended the launch.
"""

from __future__ import annotations

import errno
import os
import subprocess
import threading

import pytest

from levain.chat_egress import decider as dm
from levain.chat_egress import launch as lm
from levain.coordinator.envelopes import EnvelopeStore

from .test_launch import Origin, _node_children, _start, real_launch


@pytest.fixture
def origin():
    o = Origin()
    yield o
    o.close()


def _fds() -> set[int]:
    return {int(x) for x in os.listdir("/dev/fd")} - {max(int(x) for x in os.listdir("/dev/fd"))}


def _egress_threads() -> list[str]:
    return [t.name for t in threading.enumerate() if t.name.startswith("levain-egress")]


def _reap(before: set[int]) -> None:
    for pid in {p.pid for p in _node_children()} - before:
        try:
            os.kill(pid, 9)
            os.waitpid(pid, 0)
        except OSError:
            pass


class Fault(Exception):
    pass


def _nth(real, n: int, exc: BaseException):
    calls = {"n": 0}

    def f(*a, **k):
        calls["n"] += 1
        if calls["n"] == n:
            raise exc
        return real(*a, **k)
    return f


def _fault(name: str, monkeypatch) -> None:
    emfile = OSError(errno.EMFILE, "Too many open files")
    if name == "socketpair-2":
        monkeypatch.setattr(lm.socket, "socketpair", _nth(lm.socket.socketpair, 2, emfile))
    elif name == "pipe-2":
        monkeypatch.setattr(lm.os, "pipe", _nth(lm.os.pipe, 2, emfile))
    elif name == "socks":
        def boom(*a, **k):
            raise emfile
        monkeypatch.setattr(lm, "SocksRefuser", boom)
    elif name == "popen":
        monkeypatch.setattr(lm.subprocess, "Popen", _nth(lm.subprocess.Popen, 1, emfile))
    elif name == "decider-init":
        def bad(*a, **k):
            raise Fault("decider-init")
        monkeypatch.setattr(dm, "Decider", bad)
    elif name != "events-root":
        raise AssertionError(name)


@pytest.mark.chat_egress_srt
@pytest.mark.parametrize("step", ["socketpair-2", "pipe-2", "socks", "popen", "events-root", "decider-init"])
def test_a_refusal_at_any_step_leaves_nothing_open(tmp_path, origin, monkeypatch, step) -> None:
    store = EnvelopeStore(tmp_path / "dispatch")
    lid = real_launch(store)
    store.launch(lid)
    monkeypatch.setattr(lm, "check_node", lambda node: "v26.8.1")       # no node run inside the measured window
    kids, fds = {p.pid for p in _node_children()}, _fds()
    _fault(step, monkeypatch)
    events = __import__("pathlib").Path("/") if step == "events-root" else None
    with pytest.raises(BaseException) as ei:
        _start(tmp_path, origin, monkeypatch, store=store, launch_id=lid, events=events)
    left = _fds() - fds
    print(f"RUN S8C refusal at {step}: raised {type(ei.value).__name__}: {ei.value}; fds left {sorted(left)}; "
          f"threads {_egress_threads()}; node children {sorted({p.pid for p in _node_children()} - kids)}")
    _reap(kids)
    assert left == set(), step
    assert _egress_threads() == [] and dm.service_threads() == []
    expected = Fault if step == "decider-init" else lm.EgressRefused
    assert isinstance(ei.value, expected), (step, ei.value)


def _wait_times_out(monkeypatch):
    real = subprocess.Popen.wait

    def wait(self, timeout=None):
        if timeout is not None:
            raise subprocess.TimeoutExpired(self.args, timeout)
        return real(self, timeout)
    monkeypatch.setattr(subprocess.Popen, "wait", wait)


@pytest.mark.chat_egress_srt
def test_a_start_failure_whose_glue_wait_times_out_still_closes_everything_and_keeps_its_exception(
        tmp_path, origin, monkeypatch) -> None:
    store = EnvelopeStore(tmp_path / "dispatch")
    lid = real_launch(store)
    store.launch(lid)
    monkeypatch.setattr(lm, "check_node", lambda node: "v26.8.1")
    kids, fds = {p.pid for p in _node_children()}, _fds()
    _fault("decider-init", monkeypatch)
    _wait_times_out(monkeypatch)
    with pytest.raises(BaseException) as ei:
        _start(tmp_path, origin, monkeypatch, store=store, launch_id=lid)
    left = _fds() - fds
    print(f"RUN S8C start failure + wait timeout: raised {type(ei.value).__name__}: {ei.value}; fds left "
          f"{sorted(left)}; threads {_egress_threads()}")
    monkeypatch.undo()
    _reap(kids)
    assert isinstance(ei.value, Fault)
    assert left == set() and _egress_threads() == []


@pytest.mark.chat_egress_srt
def test_stop_whose_glue_wait_times_out_still_closes_the_decider_and_socks(tmp_path, origin, monkeypatch) -> None:
    kids = {p.pid for p in _node_children()}
    e = _start(tmp_path, origin, monkeypatch)
    _wait_times_out(monkeypatch)
    try:
        out = lm.stop_egress(e)
    finally:
        monkeypatch.undo()
        _reap(kids)
    print(f"RUN S8C stop + wait timeout: {out}")
    assert out.glue_exit is None and out.final_flush is None
    assert out.socks_clean and _egress_threads() == [] and dm.service_threads() == []
    assert out.order == ("socks", "lifeline", "glue", "decider")
    assert [f.split(":")[0] for f in out.failures] == ["glue"]


@pytest.mark.chat_egress_srt
def test_stop_whose_decider_close_raises_still_closes_socks(tmp_path, origin, monkeypatch) -> None:
    e = _start(tmp_path, origin, monkeypatch)
    real_close = e.decider.close

    def close(*a, **k):
        real_close(*a, **k)
        raise Fault("decider close")
    monkeypatch.setattr(e.decider, "close", close)
    out = lm.stop_egress(e)
    print(f"RUN S8C stop + decider close raises: {out}")
    assert out.glue_exit == 0 and out.socks_clean and not out.decider_clean
    assert _egress_threads() == [] and dm.service_threads() == []
    assert [f.split(":")[0] for f in out.failures] == ["decider"]


def _read_exact(c, n: int, within_s: float = 5.0) -> bytes:
    """Exactly `n` bytes (a TCP read may return fewer), bounded by one deadline; fewer at EOF."""
    import time
    deadline = time.monotonic() + within_s
    buf = b""
    while len(buf) < n:
        left = deadline - time.monotonic()
        assert left > 0, f"read {len(buf)} of {n} bytes inside {within_s}s"
        c.settimeout(left)
        chunk = c.recv(n - len(buf))
        if not chunk:
            break
        buf += chunk
    return buf


@pytest.mark.chat_egress_srt
def test_a_socks_refusal_landing_during_stop_is_counted_in_the_final_flush(tmp_path, origin, monkeypatch) -> None:
    import socket
    import time
    e = _start(tmp_path, origin, monkeypatch)
    real_close = e.socks.close

    def close(*a, **k):
        c = socket.create_connection(("127.0.0.1", e.socks_port), timeout=5)
        try:
            c.sendall(b"\x05\x01\x00")
            assert _read_exact(c, 2) == b"\x05\x00"    # the refusal was reported before this answer
        finally:
            c.close()
        return real_close(*a, **k)
    monkeypatch.setattr(e.socks, "close", close)
    out = lm.stop_egress(e)
    print(f"RUN S1 refusal during stop: {out.final_flush}")
    assert out.glue_exit == 0 and out.final_flush is not None
    assert out.final_flush["total"] == 1 and out.final_flush["by_reason"] == {"socks": 1}


# --- S8D r1 DS item 4: an OSError closing the glue's stderr file never leaves the glue unowned ---------------------

class _StderrCloseFails:
    """The glue's stderr file, whose close (the `with` block's exit) raises OSError after really closing."""

    def __init__(self, f) -> None:
        self._f = f

    def fileno(self) -> int:
        return self._f.fileno()

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        self._f.close()
        raise OSError(errno.EIO, "close failed")


@pytest.mark.chat_egress_srt
def test_an_oserror_closing_the_glue_stderr_still_ends_the_glue(tmp_path, origin, monkeypatch) -> None:
    import pathlib
    real_open = pathlib.Path.open

    def opener(self, mode="r", *a, **kw):
        f = real_open(self, mode, *a, **kw)
        return _StderrCloseFails(f) if self.name.endswith(".glue-stderr") and mode == "w" else f
    monkeypatch.setattr(pathlib.Path, "open", opener)
    before = {p.pid for p in _node_children()}
    with pytest.raises(lm.EgressRefused, match="did not start"):
        lm.stop_egress(_start(tmp_path, origin, monkeypatch))
    left = {p.pid for p in _node_children()} - before
    print(f"RUN S8D DS-4: node children left after the refused launch: {sorted(left)}")
    for pid in left:                                   # the red case: the orphaned glue, killed so the run ends clean
        os.kill(pid, 9)
        os.waitpid(pid, 0)
    assert left == set()


# --- socks3 r1 item 1: a launch torn down after a partial start has no SOCKS refuser ------------------------------

class _Proc:
    def wait(self, timeout=None):
        return 0

    def kill(self) -> None:
        pass


class _Decider:
    def close(self, timeout_s=None) -> bool:
        return True


def test_stop_of_a_launch_whose_socks_refuser_never_started_does_not_raise(tmp_path) -> None:
    from types import SimpleNamespace
    e = SimpleNamespace(proc=_Proc(), decider=_Decider(), socks=None, life_w=None, events=tmp_path / "ev.jsonl")
    out = lm.stop_egress(e)
    assert out.socks_wall_failures == 0 and out.socks_count_exact
    assert out.failures == () and out.order == ("socks", "lifeline", "glue", "decider")

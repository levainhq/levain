"""Fixtures for the composed suite: the real glue and decider (`test_decider_glue.Launch`) over a recording origin."""

from __future__ import annotations

import logging
import os
import subprocess

import pytest

from levain.chat_egress import decider as dm

from ..decider_rig import Lookup
from ..test_decider_glue import Launch
from .rig import RecordingOrigin


class _Capture(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(record.getMessage() + (f" {record.exc_text}" if record.exc_text else ""))
        if record.exc_info:
            self.lines.append(logging.Formatter().formatException(record.exc_info))


@pytest.fixture
def logs():
    """Every record any logger emitted during the test (the root logger at DEBUG), as text."""
    h = _Capture()
    root = logging.getLogger()
    old = root.level
    root.addHandler(h)
    root.setLevel(logging.DEBUG)
    yield h
    root.removeHandler(h)
    root.setLevel(old)


@pytest.fixture
def make_origin():
    made: list[RecordingOrigin] = []

    def make(**kw) -> RecordingOrigin:
        o = RecordingOrigin(**kw)
        made.append(o)
        return o

    yield make
    for o in made:
        o.close()


def _shutdown(L: Launch) -> None:
    """Always closes the launch's decider, whether the glue is still running, exited by itself or was stopped by the
    test already; a second call is harmless. A decider that does not close fails it, after the glue is reaped."""
    try:
        L.close_life()                         # a no-op when the test's own stop() closed it
        try:
            L.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            L.proc.kill()
            L.proc.wait(timeout=5)
    finally:
        closed = L.d.close(timeout_s=10)
    assert closed, "a decider thread outlived the launch's close"


@pytest.fixture
def make_launch_(tmp_path):
    made: list[Launch] = []

    def make(origin: RecordingOrigin, **kw) -> Launch:
        kw.setdefault("limits", dm.Limits(card_poll_s=0.1, stop_drain_s=2.0))
        kw.setdefault("lookup", Lookup())
        L = Launch(tmp_path / f"L{len(made)}", origin.port, **kw)
        made.append(L)
        return L

    yield make
    failed: list[BaseException] = []
    for L in made:                             # one launch's failed teardown must not skip the rest
        try:
            _shutdown(L)
        except BaseException as exc:           # noqa: BLE001 - re-raised below
            failed.append(exc)
    if failed:
        raise failed[0]


@pytest.fixture
def make_tls_launch(tmp_path, monkeypatch):
    """`make(**kw)` starts a production-spawned launch over a TLS recording origin (`tls_rig.TlsLaunch`); all are
    stopped at teardown, the origin closed, a failed stop re-raised after the rest ran."""
    from .tls_rig import TlsLaunch
    made: list[TlsLaunch] = []

    def make(**kw) -> TlsLaunch:
        L = TlsLaunch(tmp_path / f"T{len(made)}", monkeypatch, **kw)
        made.append(L)
        return L

    yield make
    failed: list[BaseException] = []
    for L in made:
        try:
            L.close()
        except BaseException as exc:           # noqa: BLE001 - re-raised below
            failed.append(exc)
    if failed:
        raise failed[0]


@pytest.fixture
def make_leaky_tls(tmp_path):
    """`make()` builds the TEST-ONLY leaky stand-in (`tls_rig.LeakyTls`): an open relay to a TLS recording origin, the
    negative control for every "reaches nothing" oracle. Closed at teardown."""
    from .tls_rig import LeakyTls
    made: list[LeakyTls] = []

    def make() -> LeakyTls:
        L = LeakyTls(tmp_path / f"X{len(made)}")
        made.append(L)
        return L

    yield make
    for L in made:
        L.close()


class LeakyPlain:
    """TEST-ONLY negative control for the plain path: the launch's `raw` / `origin_port` / `stop` surface over an open
    relay straight to a plain recording origin."""

    def __init__(self, origin: RecordingOrigin) -> None:
        from .rig import LeakyRelay
        self.relay = LeakyRelay(origin.port, "raw")
        self.origin_port = origin.port
        self.port = self.relay.port

    def raw(self, text: bytes, timeout: float = 10.0) -> bytes:
        import socket
        s = socket.create_connection(("127.0.0.1", self.relay.port), timeout=timeout)
        try:
            s.sendall(text)
            out = b""
            while b"\r\n\r\n" not in out:
                chunk = s.recv(65536)
                if not chunk:
                    break
                out += chunk
            return out
        finally:
            s.close()

    def stop(self) -> int:
        return 0


@pytest.fixture
def make_leaky_plain():
    made: list[LeakyPlain] = []

    def make(origin: RecordingOrigin) -> LeakyPlain:
        L = LeakyPlain(origin)
        made.append(L)
        return L

    yield make
    for L in made:
        L.relay.close()

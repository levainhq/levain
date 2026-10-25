"""Fixtures for the composed suite: the real glue and decider (`test_decider_glue.Launch`) over a recording origin."""

from __future__ import annotations

import logging

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
    for L in made:
        if L.proc.poll() is None:
            L.stop()

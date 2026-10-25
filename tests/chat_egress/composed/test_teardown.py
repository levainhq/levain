"""The composed launch teardown: it never closes a descriptor number the test no longer owns, and a failed decider
close fails the teardown."""

from __future__ import annotations

import os

import pytest

from .conftest import _shutdown


def test_shutdown_after_stop_leaves_an_unrelated_descriptor_that_reused_the_number_open(make_origin,
                                                                                         make_launch_) -> None:
    L = make_launch_(make_origin())
    stale = L.life_w                          # the number stop() releases; claim it for an unrelated descriptor
    assert L.stop() == 0
    held: list[int] = []
    try:
        for _ in range(4096):
            fd = os.dup(0)
            held.append(fd)
            if fd == stale:
                break
        else:
            pytest.skip("could not reuse the released descriptor number")
        _shutdown(L)
        os.fstat(stale)                       # EBADF here means teardown closed a number it no longer owned
    finally:
        for fd in held:
            try:
                os.close(fd)
            except OSError:
                pass


def test_a_decider_close_that_returns_false_fails_the_teardown(make_origin, make_launch_, monkeypatch) -> None:
    L = make_launch_(make_origin())
    assert L.stop() == 0
    real = L.d.close
    monkeypatch.setattr(L.d, "close", lambda *a, **k: (real(*a, **k), False)[1])
    try:
        with pytest.raises(AssertionError, match="decider"):
            _shutdown(L)
    finally:
        monkeypatch.undo()                    # the fixture's own teardown must see the real close

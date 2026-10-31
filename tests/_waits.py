"""One environment-scaled clock for the tests' waits.

``LEVAIN_TEST_WAIT_SCALE`` (a float, default 1) multiplies every deadline a test sets for something that happens on another
thread, process or page; a loaded CI runner sets it above 1. A value below 1 counts as 1: a fast machine does not shorten what
a test asked for. The browser driver (``tests/_page_drive.mjs``) reads the same variable with the same floor, so the two
sides of a test stretch together.
"""
from __future__ import annotations

import os
import time
from collections.abc import Callable


def scale() -> float:
    """The scale factor: the env var as a float, 1.0 when unset or unreadable, never below 1.0."""
    try:
        v = float(os.environ.get("LEVAIN_TEST_WAIT_SCALE", ""))
    except ValueError:
        return 1.0
    return v if v >= 1.0 else 1.0


def scaled(seconds: float) -> float:
    """``seconds`` stretched by :func:`scale`."""
    return seconds * scale()


def wait_until(predicate: Callable[[], object], timeout: float, interval: float = 0.05, what: str = "") -> None:
    """Poll ``predicate`` every ``interval`` s until it is truthy; the deadline is ``scaled(timeout)``.

    The predicate is tried once more after the deadline, so a slow last poll does not lose a condition that came true.
    Raises AssertionError naming ``what`` when it never does."""
    end = time.monotonic() + scaled(timeout)
    while True:
        if predicate():
            return
        if time.monotonic() >= end:
            break
        time.sleep(interval)
    if predicate():
        return
    raise AssertionError(f"never came true within {scaled(timeout):g}s (scale {scale():g}): {what or getattr(predicate, '__name__', 'condition')}")

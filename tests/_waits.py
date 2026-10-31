"""One environment-scaled clock for the tests' waits.

``LEVAIN_TEST_WAIT_SCALE`` multiplies every deadline a test sets for something that happens on another thread, process or
page; a loaded CI runner sets it above 1. The whole string must be a finite decimal number (``2``, ``1.5``, ``1e1``); anything
else (``2junk``, ``inf``, ``nan``, ``1e9999``, blank) is 1. The number is clamped to [1, 20]: a fast machine does not shorten
what a test asked for, and no setting can make a deadline unbounded. The browser driver (``tests/_page_drive.mjs``) parses the
variable by the same rule (the same pattern), so the two sides of a test stretch together.
"""
from __future__ import annotations

import os
import re
import time
from collections.abc import Callable


MAX_SCALE = 20.0
# the one grammar, shared with the driver: a plain decimal with an optional exponent, nothing else (no inf / nan / hex / underscores / blanks)
_DECIMAL = re.compile(r"[+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?", re.ASCII)


def scale() -> float:
    """The scale factor: the env var when the whole string is a finite decimal, clamped to [1, 20]; 1.0 otherwise."""
    raw = os.environ.get("LEVAIN_TEST_WAIT_SCALE", "")
    if not _DECIMAL.fullmatch(raw):
        return 1.0
    v = float(raw)
    if v != v or v in (float("inf"), float("-inf")):                  # 1e9999 matches the grammar and overflows
        return 1.0
    return min(max(v, 1.0), MAX_SCALE)


def scaled(seconds: float) -> float:
    """``seconds`` stretched by :func:`scale`."""
    return seconds * scale()


def wait_until(predicate: Callable[[], object], timeout: float, interval: float = 0.05, what: str = "") -> None:
    """Poll ``predicate`` every ``interval`` s until it is truthy; the deadline is ``scaled(timeout)``.

    A predicate that raises counts as "not yet" and is polled again, as the page driver's ``until`` does; at the deadline the
    AssertionError names ``what`` and chains the last exception. The predicate is tried once more after the deadline, so a slow
    last poll does not lose a condition that came true: it must therefore be free of side effects (it is called once more
    after the deadline). Raises AssertionError naming ``what`` when it never comes true."""
    end = time.monotonic() + scaled(timeout)
    last: Exception | None = None

    def poll() -> bool:
        nonlocal last
        try:
            return bool(predicate())
        except Exception as e:                                          # noqa: BLE001 - a not-yet-true state may raise
            last = e
            return False

    while True:
        if poll():
            return
        if time.monotonic() >= end:
            break
        time.sleep(interval)
    if poll():
        return
    raise AssertionError(f"never came true within {scaled(timeout):g}s (scale {scale():g}): {what or getattr(predicate, '__name__', 'condition')}"
                         + (f" (last error: {last!r})" if last else "")) from last

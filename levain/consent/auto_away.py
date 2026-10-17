"""levain.consent.auto_away — whether the operator is away, as the consent hooks and ``levain consent away`` read it.

Away is decided in this order, strongest first: the operator's explicit word (``levain consent away on`` or
``off``, kept in the agent's settings file until ``levain consent away auto`` clears it), then input idle (no
keyboard, mouse or trackpad input for ``away_idle_seconds``, default :data:`IDLE_THRESHOLD`), then off. Input idle
is read only on macOS, from ``ioreg``'s ``HIDIdleTime`` (HID-level, so a mouse move counts as being there, and input
typed into a pty by another process does not); every other platform has no idle source here, so only the explicit
word turns away mode on there. A read that fails or times out is "no idle source", never "away".

Away mode answers nothing: it decides only whether the hook holds a request for a signed answer or leaves the
agent's own dialog to ask at once.
"""
from __future__ import annotations

import re
import subprocess
import sys
from collections.abc import Callable
from typing import Any

__all__ = ["DEFAULT_HOLDS", "IDLE_THRESHOLD", "WORDS", "away_state", "hold_seconds", "input_idle_seconds"]

# the settings file's ``away_word``: the operator's explicit words, or ``auto`` for none (input idle decides)
WORDS = ("on", "off", "auto")
# input silence after which the operator reads as away: 10 minutes
IDLE_THRESHOLD = 600.0
# Claude Code shows its own dialog at once while the hook holds (an allow from the hook dismisses it), so a long
# hold costs the person at the laptop nothing; Codex shows no dialog until the hook returns, so a hold is a stall
DEFAULT_HOLDS = {"claude_code": 300.0, "codex": 120.0}
_IOREG = ("/usr/sbin/ioreg", "-r", "-d", "1", "-k", "HIDIdleTime", "-c", "IOHIDSystem")
_IDLE_LINE = re.compile(rb'"HIDIdleTime" = (\d+)')
IDLE_READ_TIMEOUT = 2.0


def input_idle_seconds() -> float | None:
    """Seconds since the last keyboard, mouse or trackpad input on this Mac (``ioreg``'s ``HIDIdleTime``), or
    ``None`` when there is no such source: not macOS, or the read failed, timed out (:data:`IDLE_READ_TIMEOUT`) or
    did not parse."""
    if sys.platform != "darwin":
        return None
    try:
        out = subprocess.run(_IOREG, capture_output=True, timeout=IDLE_READ_TIMEOUT, check=False, env={},
                             stdin=subprocess.DEVNULL).stdout
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    m = _IDLE_LINE.search(out or b"")
    return int(m.group(1)) / 1e9 if m else None


def away_state(settings: Any, idle: Callable[[], float | None] | None = None) -> tuple[bool, str, str]:
    """``(away, source, why)`` for an agent's adapter settings: ``source`` is ``word`` (the explicit word decided),
    ``idle`` (input idle decided) or ``none`` (no idle source; off). ``idle`` (default :func:`input_idle_seconds`)
    is read only when no explicit word stands."""
    if settings.away_mode:
        return True, "word", "explicit word: on"
    if settings.explicit_off:
        if getattr(settings, "legacy", False):
            return False, "word", ("explicit word: off (set before auto-away existed); `levain consent away auto` "
                                   "lets input idle decide")
        return False, "word", "explicit word: off"
    seconds = (idle or input_idle_seconds)()
    if seconds is None:
        return False, "none", "no input-idle source on this platform: only `levain consent away on` turns it on"
    threshold = settings.away_idle_seconds
    if seconds >= threshold:
        return True, "idle", f"input idle {seconds:.0f}s >= {threshold:g}s"
    return False, "idle", f"input idle {seconds:.0f}s < {threshold:g}s"


def hold_seconds(settings: Any, agent: str, source: str) -> float:
    """How long a request is held when :func:`away_state` says away from ``source``: the file's hold for the explicit
    word (as before input idle existed); for input idle, the file's hold when above zero, else the agent's
    :data:`DEFAULT_HOLDS` (a ``levain init`` file holds 0 until ``levain consent away`` writes one)."""
    if source == "word" or settings.away_hold_seconds > 0:
        return float(settings.away_hold_seconds)
    return DEFAULT_HOLDS[agent]

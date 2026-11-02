"""levain.consent.auto_away — whether the operator is away, as the consent hooks and ``levain consent away`` read it.

Given a queue directory (every hook call site), away is decided in this order, strongest first: an unreadable
store (away), a signed away mark (``levain consent away on``, which only a signed ``off`` or ``auto`` leaves; away),
then the presence record: an approval is held for a signed answer exactly when it would reach the operator's devices
(:func:`levain.webpush.intent.approvals_to_devices`, the same call as ``should_push(queue_dir, "consent")``, so the
push and the hold cannot diverge). The settings file's ``away_word``, ``away_mode`` and ``away_idle_seconds`` are not
read then; the one threshold is the record's ``follow_after``. With no queue directory (an old caller, kept for one
release) the legacy order stands: the explicit word, then input idle (no keyboard, mouse or trackpad input for
``away_idle_seconds``, default :data:`IDLE_THRESHOLD`), then off. Input idle is read only on macOS, from ``ioreg``'s
``HIDIdleTime`` (HID-level, so a mouse move counts as being there, and input typed into a pty by another process
does not); every other platform has no idle source here. A read that fails or times out is "no idle source", never
"away".

Away mode answers nothing: it decides only whether the hook holds a request for a signed answer or leaves the
agent's own dialog to ask at once.
"""
from __future__ import annotations

import re
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

__all__ = ["DEFAULT_HOLDS", "IDLE_THRESHOLD", "WORDS", "approvals_note", "away_state", "hold_seconds",
           "input_idle_seconds"]

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


def away_state(settings: Any, idle: Callable[[], float | None] | None = None, *,
               queue_dir: Path | str | None = None) -> tuple[bool, str, str]:
    """``(away, source, why)`` for an agent's adapter settings: ``source`` is ``word`` (the explicit word, or a signed
    mark, decided), ``intent`` (the presence record decided; only with ``queue_dir``), ``idle`` (input idle decided)
    or ``none`` (no idle source; off). In order: an unreadable store is away; a signed away mark is away ("a signed
    away stands"); with ``queue_dir``, away exactly when an approval reaches the operator's devices
    (:func:`levain.webpush.intent.approvals_to_devices`); with none (an old caller, one release), the legacy word and
    input idle. ``idle`` (default :func:`input_idle_seconds`) is read only when nothing stronger stands."""
    unreadable = getattr(settings, "away_unreadable", "")
    if unreadable:
        return True, "word", unreadable
    if getattr(settings, "away_marked", False):
        return True, "word", "a signed away stands (leaving it needs a signed `levain consent away off`)"
    if queue_dir is not None:
        from levain.webpush import intent

        push, why = intent.approvals_to_devices(queue_dir, idle=idle)
        return push, "intent", why
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
    word or a signed mark (as before input idle existed); for the presence record (``intent``) or input idle, the
    file's hold when above zero, else the agent's :data:`DEFAULT_HOLDS` (a ``levain init`` file holds 0 until
    ``levain consent away`` writes one). The hold length is per agent; it is not presence."""
    if source == "word" or settings.away_hold_seconds > 0:
        return float(settings.away_hold_seconds)
    return DEFAULT_HOLDS[agent]


def approvals_note(queue_dir: Path | str | None, *, to_devices: bool | None = None) -> str | None:
    """The ``Approvals:`` clause of the describe line (:func:`levain.webpush.intent.describe_line`), without its
    prefix or final period, or ``None`` when approvals go where ``Pings now`` already says. It reads consent's own side
    only (a signed away mark, an enrolled key that could sign), never the presence record: the line is one sample of the
    record and the clause must not take a second (DESIGN §7). ``to_devices``, when the caller has it from that same
    sample (whether the record sends approvals to the devices: devices on now and approvals let through), makes the
    clause appear only where consent differs from the record; without it a standing mark or an unsignable install is
    always named (true, and never wrong). A signed away mark stands over the record (approvals reach the devices for a
    signed answer whatever it says); a record that would send them there still holds only while an enrolled key could
    sign. Read for every agent that has settings or a store in ``queue_dir``; any fault says nothing."""
    if queue_dir is None:
        return None
    try:
        from levain.consent import away as away_mod
        from levain.consent.adapters import claude_code

        qdir = Path(queue_dir)
        marked = unsignable = False
        for agent in DEFAULT_HOLDS:
            if not (qdir / away_mod.AGENTS[agent]).exists() and not (qdir / claude_code.DB_NAME).exists():
                continue
            st = away_mod.settings(qdir, agent)
            if getattr(st, "away_unreadable", ""):
                continue
            if st.away_marked:
                marked = True
            elif not claude_code._can_sign(st):
                unsignable = True
        if marked and to_devices is not True:
            return ("your devices, for a signed answer (a signed away stands; leave: levain consent away off "
                    "--signing-key <key>)")
        if unsignable and to_devices is not False:
            return "the agent's own dialog (no enrolled key can sign from a device)"
    except Exception:  # noqa: BLE001 - the clause is an aid; a fault says nothing rather than something wrong
        return None
    return None

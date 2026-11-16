"""levain.desk.night_lease — the night lease rule and the keepalive, as pure functions (ND-5b).

Nothing here reads a store, a clock or a process: every input is passed in, so a fake clock drives it.

THE NIGHT LEASE RULE. By day the watcher beats a lease only for a holder that wrote as desk within
``SERVICE_BEAT_WINDOW``. While a night window is open the desk is resident and its lease is held by RESIDENCY, not by
writes: a holder whose session is alive and not done is beaten however long it has been silent. A dead or done session
is never held.

THE KEEPALIVE. A quiet resident desk is woken at most every 50 minutes so the one-hour prompt cache stays warm
(np-8af88e02). A keepalive turn is a turn but NOT a write: it never counts toward the day rule's "wrote recently".

CLOCK AND INPUT RULES. Every datetime is timezone-aware (the desk's stamps are aware UTC); a naive one raises
TypeError naming the argument. A stamp more than ``CLOCK_JITTER`` ahead of ``now`` means the clock stepped backwards:
the elapsed time is UNKNOWN, never "recent". Turn lists must be a list or tuple, never an iterator.

No caller yet except the tests; wiring into the watch pass is levain#15.
"""
from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta

from levain.desk.lease import SERVICE_BEAT_WINDOW

__all__ = ["CLOCK_JITTER", "KEEPALIVE", "KEEPALIVE_PERIOD", "WRITE", "beat_due", "keepalive_due", "last_turn_at",
           "last_write_at"]

KEEPALIVE_PERIOD = timedelta(minutes=50)
# A stamp this far ahead of ``now`` is jitter between two reads of one clock; further ahead, the clock stepped backwards
# (or the stamp came from another host) and the elapsed time is unknown.
CLOCK_JITTER = timedelta(seconds=5)
WRITE = "write"
KEEPALIVE = "keepalive"


def _aware(name: str, value: datetime) -> None:
    if not isinstance(value, datetime):
        raise TypeError(f"{name} must be a datetime, not {type(value).__name__}")
    if value.tzinfo is None or value.utcoffset() is None:
        raise TypeError(f"{name} must be timezone-aware (a naive datetime has no zone)")


def beat_due(*, night_open: bool, session_alive: bool, session_done: bool, last_write_at: datetime | None,
             now: datetime, window: timedelta = SERVICE_BEAT_WINDOW) -> bool:
    """Whether the watcher should beat the holder's lease now.

    By day a write dated more than ``CLOCK_JITTER`` AHEAD of ``now`` (the clock stepped back) is unknown, not recent:
    the holder is not beaten. A lapsed lease is re-claimed by a live desk's next write; a silent holder kept beaten is
    held for as long as the step lasts.
    """
    _aware("now", now)
    if last_write_at is not None:
        _aware("last_write_at", last_write_at)
    if window <= timedelta(0):
        raise ValueError(f"window must be positive, not {window!r}")
    if not session_alive or session_done:
        return False
    if night_open:
        return True
    return last_write_at is not None and -CLOCK_JITTER <= now - last_write_at <= window


def keepalive_due(last_turn_at: datetime | None, now: datetime) -> bool:
    """True once ``KEEPALIVE_PERIOD`` has passed since the desk's last turn of any kind (none yet: due).

    A last turn dated more than ``CLOCK_JITTER`` ahead of ``now`` (the clock stepped back) is unknown: due, since a
    keepalive is cheap and a cold one-hour cache is not.
    """
    _aware("now", now)
    if last_turn_at is None:
        return True
    _aware("last_turn_at", last_turn_at)
    elapsed = now - last_turn_at
    return elapsed < -CLOCK_JITTER or elapsed >= KEEPALIVE_PERIOD


def _checked(turns: Sequence[tuple[datetime, str]]) -> Sequence[tuple[datetime, str]]:
    # An iterator is drained by the first function and read as "no turns" by the second: refuse it loudly.
    if not isinstance(turns, (list, tuple)):
        raise TypeError(f"turns must be a list or tuple, not {type(turns).__name__} (an iterator is read once)")
    for at, kind in turns:
        if kind not in (WRITE, KEEPALIVE):
            raise ValueError(f"unknown turn kind {kind!r}")
        _aware("a turn's datetime", at)
    return turns


def last_write_at(turns: Sequence[tuple[datetime, str]]) -> datetime | None:
    """The latest WRITE turn; a keepalive turn is not one."""
    return max((at for at, kind in _checked(turns) if kind == WRITE), default=None)


def last_turn_at(turns: Sequence[tuple[datetime, str]]) -> datetime | None:
    """The latest turn of any kind, a keepalive included."""
    return max((at for at, _ in _checked(turns)), default=None)

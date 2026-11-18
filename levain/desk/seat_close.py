"""The close predicate of a seat launched for board items (T10, levain#102; design 6, "The close, chosen by the LAUNCH protocol").

A pure function over facts the caller reads; it opens no store and reads no file. What a seat's close is judged by is the
protocol its LAUNCH recorded (:attr:`levain.desk.lease.Launch.protocol`), never the repository's mode now:

* ``tracked``: for each item the seat touched, at least one episode written at or after the launch's whole second whose
  ``source`` is the seat's durable key and that is tagged ``item:<id>``, AND a closing trailer or an ``amend --done`` for that item.
  A ``tracked`` launch with no readable item claim is NOT closed: it has nothing the predicate can judge;
* ``legacy`` and ``ceremony``: the old close, ``old_close_ok`` (the caller's reading of the pickup file's CURRENT STATE
  check). When the repository has become tracked while the seat ran (``repo_tracked_now``) its old pickup file is a stub, so
  the tracked predicate is accepted too: either one closes it (design 6's dual-accept). A stub is never read as a failed
  CURRENT STATE close, so such a seat with no item claimed closes by the tracked predicate, which then has no item to want.

Times are compared as instants: each is parsed (an offset is honoured; a time with no offset is unreadable and never
counts). An episode counts when it is at or after the launch's whole second: episodes are stored at whole seconds and
launches at microseconds, so an episode in the launch's own second is counted. A launch time that cannot be read is
reported only when there is an item to judge against it.

Known edges, judged and left:

* the same-second tolerance would count an episode written in the launch's second but before its instant; that was judged
  not reachable as a false accept, since it needs a close episode for the item written in the very second the seat launched.

Nothing here decides which episodes or trailers exist: the caller (flow's seat close) reads them and passes them in.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping

__all__ = ["CloseVerdict", "check_close", "tracked_missing"]


@dataclass(frozen=True)
class CloseVerdict:
    ok: bool
    via: str                        # "tracked" or "old": the predicate that held, or the one reported when none did
    missing: tuple[str, ...]        # one plain line per thing absent, for the seat to read
    reason: str = ""                # the rule that decided, when it is not plain from ``via``


def _tags(episode: Mapping[str, Any]) -> frozenset[str]:
    got = episode.get("tags")
    if not got and isinstance(episode.get("metadata"), Mapping):       # an empty top-level list is no tags: read metadata
        got = episode["metadata"].get("tags")
    return frozenset(t for t in got if isinstance(t, str)) if isinstance(got, (list, tuple, set, frozenset)) else frozenset()


def _instant(text: Any) -> datetime | None:
    """The aware instant an ISO-8601 time names (``Z`` or an offset; any fraction), else None: a time with no offset has
    no known zone and is never compared."""
    if not isinstance(text, str):
        return None
    try:
        when = datetime.fromisoformat(text.strip())
    except ValueError:
        return None
    if when.tzinfo is None or when.utcoffset() is None:
        return None
    return when.astimezone(timezone.utc)


def _after(at: Any, launched: datetime | None) -> bool:
    """Whether the episode time ``at`` is at or after the launch's whole second, at the episode's own precision: a
    whole-second time is the second it names, so it counts when that second is not before the launch's; a time with a
    fraction counts when it is after the launch's instant."""
    when = _instant(at)
    if when is None or launched is None:
        return False
    if when.microsecond == 0 and "." not in at:
        return when >= launched.replace(microsecond=0)
    return when > launched


def tracked_missing(items: Iterable[str], *, key: str, launched_at: str, episodes: Iterable[Mapping[str, Any]],
                    closed_items: Iterable[str]) -> list[str]:
    """What the tracked close lacks, one line each (empty: it holds). ``episodes`` are mappings with ``source``, ``at`` and
    ``tags`` (or ``metadata.tags``); an episode counts at or after the launch's whole second (see the module).
    ``closed_items`` are the item ids a closing trailer or an ``amend --done`` covers. An unreadable ``launched_at`` is
    a missing line only when there are items to judge: with none, nothing is compared against it."""
    items = list(items)
    launched = _instant(launched_at)
    eps = [e for e in episodes if isinstance(e, Mapping) and e.get("source") == key and _after(e.get("at"), launched)]
    closed = set(closed_items)
    out: list[str] = []
    if items and launched is None:
        out.append(f"the launch time {str(launched_at)[:40]!r} is unreadable, so no episode can be judged after it")
    for item in items:
        if launched is not None and not any(f"item:{item}" in _tags(e) for e in eps):
            out.append(f"{item}: no episode with source {key} tagged item:{item} at or after the launch's whole second")
        if item not in closed:
            out.append(f"{item}: no closing trailer (Board-Closes) and no `levain board amend --done` for it")
    return out


def check_close(protocol: str, items: Iterable[str], *, key: str, launched_at: str, episodes: Iterable[Mapping[str, Any]],
                closed_items: Iterable[str], old_close_ok: bool, repo_tracked_now: bool) -> CloseVerdict:
    """Judge a close by the launch's ``protocol``. An unknown protocol reads ``legacy``, as a record with no field does.
    ``items`` is the launch's readable claim (none: no claim, or a damaged claim cell)."""
    items = list(items)
    missing = tuple(tracked_missing(items, key=key, launched_at=launched_at, episodes=episodes, closed_items=closed_items))
    if protocol == "tracked":
        if not items:
            return CloseVerdict(False, "tracked", ("this tracked launch has no readable board item claim "
                                                   "(launch_board_items): there is no item to close",),
                                "a tracked close is judged per claimed item")
        return CloseVerdict(not missing, "tracked", missing)
    if old_close_ok:
        return CloseVerdict(True, "old", ())
    if repo_tracked_now:
        rule = ("the repository became tracked while this seat ran: its pickup file is a stub, so the tracked close is "
                "accepted in place of the CURRENT STATE close (dual-accept)")
        return CloseVerdict(not missing, "tracked", missing, rule)
    return CloseVerdict(False, "old", ("the pickup file's CURRENT STATE close does not hold",))

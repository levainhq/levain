"""The close predicate of a seat launched for board items (T10, levain#102; design 6, "The close, chosen by the LAUNCH protocol").

A pure function over facts the caller reads; it opens no store and reads no file. What a seat's close is judged by is the
protocol its LAUNCH recorded (:attr:`levain.desk.lease.Launch.protocol`), never the repository's mode now:

* ``tracked``: for each item the seat touched, at least one episode written after its launch whose ``source`` is the seat's
  durable key and that is tagged ``item:<id>``, AND a closing trailer or an ``amend --done`` for that item;
* ``legacy`` and ``ceremony``: the old close, ``old_close_ok`` (the caller's reading of the pickup file's CURRENT STATE
  check). When the repository has become tracked while the seat ran (``repo_tracked_now``) its old pickup file is a stub, so
  the tracked predicate is accepted too: either one closes it.

Nothing here decides which episodes or trailers exist: the caller (flow's seat close) reads them and passes them in.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping

__all__ = ["CloseVerdict", "check_close", "tracked_missing"]


@dataclass(frozen=True)
class CloseVerdict:
    ok: bool
    via: str                        # "tracked" or "old": the predicate that held, or the one reported when none did
    missing: tuple[str, ...]        # one plain line per thing absent, for the seat to read


def _tags(episode: Mapping[str, Any]) -> frozenset[str]:
    got = episode.get("tags")
    if got is None:
        got = (episode.get("metadata") or {}).get("tags") if isinstance(episode.get("metadata"), Mapping) else None
    return frozenset(t for t in got if isinstance(t, str)) if isinstance(got, (list, tuple, set, frozenset)) else frozenset()


def tracked_missing(items: Iterable[str], *, key: str, launched_at: str, episodes: Iterable[Mapping[str, Any]],
                    closed_items: Iterable[str]) -> list[str]:
    """What the tracked close lacks, one line each (empty: it holds). ``episodes`` are mappings with ``source``, ``at`` and
    ``tags`` (or ``metadata.tags``); ``at`` is compared as the same-format UTC text the stores write, so a lexical comparison
    is a time comparison. ``closed_items`` are the item ids a closing trailer or an ``amend --done`` covers."""
    eps = [e for e in episodes if isinstance(e, Mapping) and e.get("source") == key
           and isinstance(e.get("at"), str) and e["at"] > launched_at]
    closed = set(closed_items)
    out: list[str] = []
    for item in items:
        if not any(f"item:{item}" in _tags(e) for e in eps):
            out.append(f"{item}: no episode with source {key} tagged item:{item} after launch")
        if item not in closed:
            out.append(f"{item}: no closing trailer (Board-Closes) and no `levain board amend --done` for it")
    return out


def check_close(protocol: str, items: Iterable[str], *, key: str, launched_at: str, episodes: Iterable[Mapping[str, Any]],
                closed_items: Iterable[str], old_close_ok: bool, repo_tracked_now: bool) -> CloseVerdict:
    """Judge a close by the launch's ``protocol``. An unknown protocol reads ``legacy``, as a record with no field does."""
    items = list(items)
    missing = tuple(tracked_missing(items, key=key, launched_at=launched_at, episodes=episodes, closed_items=closed_items))
    if protocol == "tracked":
        return CloseVerdict(not missing, "tracked", missing)
    if old_close_ok:
        return CloseVerdict(True, "old", ())
    if repo_tracked_now and items and not missing:
        return CloseVerdict(True, "tracked", ())
    if repo_tracked_now and items:
        return CloseVerdict(False, "tracked", missing)
    return CloseVerdict(False, "old", ("the pickup file's CURRENT STATE close does not hold",))

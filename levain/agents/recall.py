"""levain.agents.recall — one agent's own episodes, read back out of a shared episodic store.

Recall is three steps, in this order, and the order is the point:

1. read the last ``window_days`` days by TIME (every agent's episodes are in the window);
2. keep the episodes whose ``agent`` metadata equals the asked agent exactly;
3. cap the result at ``limit``, newest first.

Capping before filtering is the failure this shape exists to refuse: a quiet agent's few episodes would be
pushed out of a fixed-size page by a busy neighbour's, and recall would report an empty memory that is not
empty. The time read is ONE call to the store, bounded by ``max_scan`` rows and by both ends of the window, so
it is a single read snapshot: paging by offset over a store other agents are writing to duplicates or skips
rows. If the bound is hit the result says so (``truncated``) instead of passing a partial read off as complete.

The window is a string-order bound on the stored timestamps, not an exact instant. Superseded episodes are not
returned: anneal hides what a newer episode replaced, and "what the agent currently knows" is the question
recall answers.

No anneal schema change: the identity rides in episode metadata (see ``levain.firing.anneal``).
"""
from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from levain.firing.anneal import IDENTITY_AGENT_KEY, episode_identity

DEFAULT_WINDOW_DAYS = 30
DEFAULT_LIMIT = 20
DEFAULT_MAX_SCAN = 50_000
_STAMP = "%Y-%m-%dT%H:%M:%S.%fZ"


@dataclass(frozen=True)
class AgentRecall:
    """What recall found. ``scanned`` is the rows the time read covered. ``truncated`` is True when
    ``max_scan`` stopped the read before the window was exhausted AND fewer than ``limit`` matches were found,
    so an absence (or a short answer) is not proof. ``unreadable`` counts the rows whose metadata could not be
    read as a mapping: they are counted, never silently dropped."""

    episodes: tuple[Any, ...]
    scanned: int
    truncated: bool
    unreadable: int = 0


def _metadata_of(ep: Any) -> Mapping | None:
    """The episode's metadata as a mapping, ``None`` if it has none, ``ValueError`` if it cannot be read."""
    meta = ep.metadata
    if isinstance(meta, str):
        try:
            meta = json.loads(meta)
        except (ValueError, RecursionError):
            raise ValueError("metadata is not JSON") from None
    if meta is None:
        return None
    if not isinstance(meta, Mapping):
        raise ValueError("metadata is not a mapping")
    return meta


MAX_WINDOW_DAYS = 3650   # far past any memory window; keeps the date arithmetic inside datetime's range


def _checked(agent: str, window_days: Any, limit: Any, max_scan: Any, now: Any) -> datetime:
    """Every argument check, in one place for both entry points; returns the window's end in UTC."""
    episode_identity(agent, "x")  # validates the agent id's shape; the run is a placeholder
    for v in (window_days, limit, max_scan):
        if not isinstance(v, int) or isinstance(v, bool) or v < 1:
            raise ValueError("window_days, limit and max_scan must each be an integer of at least 1")
    if window_days > MAX_WINDOW_DAYS:
        raise ValueError(f"window_days is at most {MAX_WINDOW_DAYS}")
    end = now if now is not None else datetime.now(timezone.utc)
    if not isinstance(end, datetime) or end.tzinfo is None:
        raise ValueError("now must be a timezone-aware datetime")
    return end.astimezone(timezone.utc)


def recall_agent_episodes(
    store: Any,
    agent: str,
    *,
    window_days: int = DEFAULT_WINDOW_DAYS,
    limit: int = DEFAULT_LIMIT,
    max_scan: int = DEFAULT_MAX_SCAN,
    now: datetime | None = None,
) -> AgentRecall:
    """Recall ``agent``'s episodes from an open anneal ``Store`` (read-only is enough). ``now`` must be
    timezone-aware (any zone; it is converted to UTC): a naive one is refused, not guessed at."""
    end = _checked(agent, window_days, limit, max_scan, now)
    since = (end - timedelta(days=window_days)).strftime(_STAMP)
    until = end.strftime(_STAMP)
    res = store.recall(since=since, until=until, limit=max_scan)
    rows = res.episodes
    kept: list[Any] = []
    unreadable = 0
    for ep in rows:  # newest first, as the store returns them
        try:
            meta = _metadata_of(ep)
        except ValueError:
            unreadable += 1
            continue
        if meta is not None and meta.get(IDENTITY_AGENT_KEY) == agent:
            kept.append(ep)
    truncated = res.total_matching > len(rows) and len(kept) < limit
    return AgentRecall(episodes=tuple(kept[:limit]), scanned=len(rows), truncated=truncated,
                       unreadable=unreadable)


def recall_agent_episodes_at(episodic_path: Path, agent: str, **kwargs: Any) -> AgentRecall:
    """Open the store at ``episodic_path`` read-only, recall, close. A missing file is an empty memory."""
    from anneal_memory import Store

    path = Path(episodic_path)
    _checked(agent, kwargs.get("window_days", DEFAULT_WINDOW_DAYS), kwargs.get("limit", DEFAULT_LIMIT),
             kwargs.get("max_scan", DEFAULT_MAX_SCAN), kwargs.get("now"))
    if not path.exists():
        return AgentRecall(episodes=(), scanned=0, truncated=False)
    with Store(path, read_only=True) as store:
        return recall_agent_episodes(store, agent, **kwargs)


__all__ = ["AgentRecall", "recall_agent_episodes", "recall_agent_episodes_at"]

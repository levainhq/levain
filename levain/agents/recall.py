"""levain.agents.recall — one agent's own episodes, read back out of a shared episodic store.

Recall is three steps, in this order, and the order is the point:

1. read the last ``window_days`` days by TIME (every agent's episodes are in the window);
2. keep the episodes whose ``agent`` metadata equals the asked agent exactly;
3. cap the result at ``limit``, newest first.

Capping before filtering is the failure this shape exists to refuse: a quiet agent's few episodes would be
pushed out of a fixed-size page by a busy neighbour's, and recall would report an empty memory that is not
empty. The time read therefore pages through the whole window, bounded by ``max_scan`` rows; if the bound is
hit the result says so (``truncated``) instead of passing a partial read off as complete.

No anneal schema change: the identity rides in episode metadata (see ``levain.firing.anneal``).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from levain.firing.anneal import IDENTITY_AGENT_KEY, episode_identity

DEFAULT_WINDOW_DAYS = 30
DEFAULT_LIMIT = 20
DEFAULT_MAX_SCAN = 50_000
_PAGE = 500


@dataclass(frozen=True)
class AgentRecall:
    """What recall found. ``scanned`` is the rows the time read covered; ``truncated`` is True when
    ``max_scan`` stopped the read before the window was exhausted, so an absence is not proof of absence."""

    episodes: tuple[Any, ...]
    scanned: int
    truncated: bool


def recall_agent_episodes(
    store: Any,
    agent: str,
    *,
    window_days: int = DEFAULT_WINDOW_DAYS,
    limit: int = DEFAULT_LIMIT,
    max_scan: int = DEFAULT_MAX_SCAN,
    now: datetime | None = None,
) -> AgentRecall:
    """Recall ``agent``'s episodes from an open anneal ``Store`` (read-only is enough)."""
    episode_identity(agent, "x")  # validates the agent id's shape; the run is a placeholder
    if window_days < 1 or limit < 1 or max_scan < 1:
        raise ValueError("window_days, limit and max_scan must each be at least 1")
    end = now or datetime.now(timezone.utc)
    since = (end - timedelta(days=window_days)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    kept: list[Any] = []
    scanned = 0
    truncated = False
    offset = 0
    while True:
        page_size = min(_PAGE, max_scan - scanned)
        res = store.recall(since=since, limit=page_size, offset=offset)
        rows = res.episodes
        scanned += len(rows)
        offset += len(rows)
        for ep in rows:
            meta = ep.metadata
            if isinstance(meta, dict) and meta.get(IDENTITY_AGENT_KEY) == agent:
                kept.append(ep)
        if offset >= res.total_matching or not rows:
            break
        if scanned >= max_scan:
            truncated = True
            break
    # store.recall is newest-first, so the head of ``kept`` is the newest.
    return AgentRecall(episodes=tuple(kept[:limit]), scanned=scanned, truncated=truncated)


def recall_agent_episodes_at(episodic_path: Path, agent: str, **kwargs: Any) -> AgentRecall:
    """Open the store at ``episodic_path`` read-only, recall, close. A missing file is an empty memory."""
    from anneal_memory import Store

    path = Path(episodic_path)
    if not path.exists():
        return AgentRecall(episodes=(), scanned=0, truncated=False)
    with Store(path, read_only=True) as store:
        return recall_agent_episodes(store, agent, **kwargs)


__all__ = ["AgentRecall", "recall_agent_episodes", "recall_agent_episodes_at"]

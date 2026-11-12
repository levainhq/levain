"""The agent graph's REPLAY keeps the question rows it can place at T. The live graph draws none (tests/test_cockpit_graph_not_band.py),
so the old live pin (the graph counts what NEEDS YOU counts: a row the desk alone owes is not a waiting task) is gone with the
count it pinned."""
from __future__ import annotations

import types
from datetime import datetime, timezone

from levain.cockpit import topology as T


def _q(n, section, carried):
    return types.SimpleNamespace(n=n, agent="desk", agent_session_id="x", section=section, carried=carried, link=None,
                                 snoozed_until=None, dated=None, filed_at="2026-11-10T12:00:00Z", answer=None)


class _Store:
    def __init__(self, qs):
        self.qs = qs

    def open_questions(self, agent=None):
        return self.qs


class _PastStore(_Store):
    def recent(self, limit):
        return self.qs


def test_the_past_read_never_hides_a_row_by_its_current_desk_flag() -> None:
    """The section/carried flag carries no stamp of when it was set, so a replay at T fails open: a row the desk owes NOW
    was filed and unanswered at T and still shows waiting."""
    store = _PastStore([_q(1, "you", False), _q(2, "desk", False)])
    at = datetime(2026, 11, 10, 13, 0, tzinfo=timezone.utc)
    assert [q.n for q in T._questions_at(store, at)] == [1, 2]

"""The agent graph's WAITING ON YOU counts exactly what NEEDS YOU counts: a row the desk alone owes (section desk, not carried
over to him) is not a waiting question task. Fixture of the 1110-26 perf finding: 3 operator rows, 6 desk-owed, 1 carried
desk row (his); the expected number comes from the app badge's own function (webpush.needs.open_needs_count)."""
from __future__ import annotations

import types
from datetime import datetime, timezone

from levain.cockpit import topology as T
from levain.webpush.needs import open_needs_count


def _q(n, section, carried):
    return types.SimpleNamespace(n=n, agent="desk", agent_session_id="x", section=section, carried=carried, link=None,
                                 snoozed_until=None, dated=None, filed_at="2026-11-10T12:00:00Z", answer=None)


class _Store:
    def __init__(self, qs):
        self.qs = qs

    def open_questions(self, agent=None):
        return self.qs


def test_graph_waiting_count_equals_needs_count(tmp_path) -> None:
    qs = [_q(1, "you", False), _q(2, "you", False), _q(3, "you", False)]
    qs += [_q(10 + i, "desk", False) for i in range(6)] + [_q(20, "desk", True)]
    store = _Store(qs)
    now = datetime(2026, 11, 10, 21, 0, tzinfo=timezone.utc)
    tasks = T._question_tasks(T._questions_at(store, None, now), lambda a, s: None)
    needs = open_needs_count(tmp_path / "q", question_source=store)
    assert needs == 4
    assert len(tasks) == needs
    assert sorted(t["card"] for t in tasks) == ["question:1", "question:2", "question:20", "question:3"]


class _PastStore(_Store):
    def recent(self, limit):
        return self.qs


def test_the_past_read_never_hides_a_row_by_its_current_desk_flag() -> None:
    """The section/carried flag carries no stamp of when it was set, so a replay at T fails open: a row the desk owes NOW
    was filed and unanswered at T and still shows waiting."""
    store = _PastStore([_q(1, "you", False), _q(2, "desk", False)])
    at = datetime(2026, 11, 10, 13, 0, tzinfo=timezone.utc)
    assert [q.n for q in T._questions_at(store, at)] == [1, 2]

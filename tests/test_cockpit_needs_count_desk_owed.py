"""The app badge counts what the Needs strip counts: a question the desk alone owes (section desk, not carried over to him) is not
his, so it is not counted; a carried desk row, his own row and a row with no section are. The same four rows as the board
column's test (test_cockpit_board: the needs-you column leaves out a desk-owed row)."""
from __future__ import annotations

import dataclasses
from pathlib import Path

from levain.consent import questions as qmod
from levain.consent.questions import QuestionStore
from levain.webpush.needs import open_needs_count


def test_needs_count_desk_owed(tmp_path: Path, monkeypatch) -> None:
    qs = QuestionStore(tmp_path / "q")
    for sid in ("fresh-desk", "old-desk", "phill", "plain"):
        qs.file(agent="claude_code", agent_session_id=sid, what=sid, options=[("a", "A"), ("b", "B")], rec="a", blocks="b", cwd="/w")
    assert open_needs_count(tmp_path / "q") == 4              # a native store sets no section: all four are his
    orig = qmod.QuestionStore._question_of
    mark = {"fresh-desk": ("desk", False), "old-desk": ("desk", True), "phill": ("you", False), "plain": ("", False)}

    def with_section(row):
        q = orig(row)
        sec, car = mark[q.agent_session_id]
        return dataclasses.replace(q, section=sec, carried=car)

    monkeypatch.setattr(qmod.QuestionStore, "_question_of", staticmethod(with_section))
    assert open_needs_count(tmp_path / "q") == 3              # the fresh desk row is out

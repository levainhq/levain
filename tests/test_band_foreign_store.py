"""A foreign question store (flow's ``NeedsQuestions``: only open_questions / recent / get / answer, no sqlite, no
``_peek``) is read and answered through its PUBLIC methods; the band numbers by the store's own ``q.n`` and persists
nothing into it. A levain ``QuestionStore`` is unchanged (the other band suites)."""
from __future__ import annotations

import pytest

from levain import band_view
from levain.cockpit import board as cockpit_board
from levain.consent.questions import Answer, Question


def _q(n: int, qid: str, answer: Answer | None = None) -> Question:
    return Question(n=n, id=qid, agent="desk", agent_session_id="-", what=f"repo: question {n}",
                    options=(("a", "yes"), ("b", "no")), rec="a", blocks="the lane", cwd="/tmp/x",
                    filed_at="2026-10-30T10:00:00Z", answer=answer)


class ForeignStore:
    """Only the public methods flow's NeedsQuestions has."""

    def __init__(self) -> None:
        self.rows = [_q(11, "np-aaaa1111"), _q(7, "np-bbbb2222")]
        self.answered: list[tuple[str, str | None, str | None, str]] = []

    def open_questions(self, agent=None):
        return [q for q in self.rows if q.answer is None and (agent is None or q.agent == agent)]

    def recent(self, limit=50):
        return list(self.rows)[:limit]

    def get(self, ref):
        text = str(ref).lstrip("#")
        return next((q for q in self.rows if text in (str(q.n), q.id)), None)

    def answer(self, ref, *, option=None, text=None, channel):
        q = self.get(ref)
        self.answered.append((q.id, option, text, channel))
        return Answer(q.id, "option" if option else "text", option or text, channel, "2026-10-30T10:05:00Z")


def test_board_cards_read_a_foreign_store():
    cards, bad = cockpit_board._band_cards(ForeignStore())
    assert bad is None
    assert sorted(c["number"] for c in cards) == [7, 11]


def test_band_text_and_payload_number_by_the_stores_own_n():
    store = ForeignStore()
    r = band_view.show(store, "cockpit:web")
    assert "NEEDS YOU · 2 open" in r.text and "7." in r.text and "11." in r.text
    p = band_view.band_payload(store, "cockpit:web")
    assert [i["n"] for i in p["items"]] == [7, 11]
    assert p["store_id"] and p["view_id"] is not None and not p["note"]
    assert [i["question"] for i in p["items"]] == [7, 11]


def test_a_foreign_store_band_is_read_only_and_answers_record_nothing():
    store = ForeignStore()
    p = band_view.band_payload(store, "cockpit:web")
    with pytest.raises(ValueError, match="nothing was recorded"):
        band_view.answer_text(store, "7b", viewer="cockpit:web", channel="cockpit",
                              store_id=p["store_id"], view_id=p["view_id"])
    assert store.answered == []

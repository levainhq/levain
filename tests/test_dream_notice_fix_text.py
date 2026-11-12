"""D1-N fix round, finding 3: the words the ``failed`` card prints say what is true today (D1-P: the pause is real)."""
from __future__ import annotations

from levain.dream import agent as da


def test_the_failed_card_says_what_two_failed_runs_in_a_row_do():
    """D1-P (1112-1) gave ``failure_streak`` its writer: two failed runs in a row pause the dream, so the card's words
    are true at one failure (it tries again) and at two (it is paused until resumed)."""
    text = da.window_remedy("failed", "/tmp/some/store/memory.db")
    assert "Two failed runs in a row pause the dream" in text and "levain agents resume dream" in text
    assert "levain agents show dream" in text and "next window" in text


def test_the_pause_notice_still_names_the_verb_for_an_agent_that_is_paused():
    got: list[str] = []
    da.pause_notice({"operation": "paused", "failure_streak": 2, "agent_id": "ag-1", "name": "dream"}, [],
                    attention=lambda key, text: got.append(text))
    assert len(got) == 1 and "levain agents resume dream" in got[0]

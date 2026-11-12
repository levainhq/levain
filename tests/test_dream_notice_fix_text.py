"""D1-N fix round, finding 3: the words the ``failed`` card prints say what is true today (D1-P: the pause is real)."""
from __future__ import annotations

import re

from levain.dream import agent as da


def test_the_failed_card_says_what_pauses_the_dream_and_what_it_does_then():
    """D1-P (1112-1) gave ``failure_streak`` its writer, and a budget breach already paused at once. The card is filed
    for a night that failed, when the dream may or may not be paused, so each clause says which: the next window runs
    UNLESS it is paused, the streak count is the constant's, and the breach pause is named beside it."""
    text = da.window_remedy("failed", "/tmp/some/store/memory.db")
    assert f"{da.PAUSE_AFTER} failed runs in a row" in text and "budget breach" in text, text
    assert "Two failed runs" not in text and "levain agents resume dream" in text
    assert "levain agents show dream" in text and "next window" in text
    assert re.search(r"unless it is paused", text), text            # "tries again" is not said of a paused dream


def test_the_failed_card_builds_its_number_from_the_constant(monkeypatch):
    monkeypatch.setattr(da, "PAUSE_AFTER", 3)
    assert "3 failed runs in a row" in da.window_remedy("failed", "/tmp/some/store/memory.db")


def test_the_pause_notice_still_names_the_verb_for_an_agent_that_is_paused():
    got: list[str] = []
    da.pause_notice({"operation": "paused", "failure_streak": 2, "agent_id": "ag-1", "name": "dream"}, [],
                    attention=lambda key, text: got.append(text))
    assert len(got) == 1 and "levain agents resume dream" in got[0]

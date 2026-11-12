"""D1-N fix round, finding 3: the words the ``failed`` card prints say what is true today."""
from __future__ import annotations

from levain.dream import agent as da


def test_the_failed_card_does_not_promise_a_pause_nothing_writes():
    """Nothing under levain/ increments ``failure_streak`` (the writer is a board row), so no agent is paused after
    failed runs; the card must not say one is."""
    text = da.window_remedy("failed", "/tmp/some/store/memory.db")
    assert "pauses" not in text and "in a row" not in text and "resume" not in text, text
    assert "levain agents show dream" in text and "next window" in text


def test_the_pause_notice_still_names_the_verb_for_an_agent_that_is_paused():
    got: list[str] = []
    da.pause_notice({"operation": "paused", "failure_streak": 2, "agent_id": "ag-1", "name": "dream"}, [],
                    attention=lambda key, text: got.append(text))
    assert len(got) == 1 and "levain agents resume dream" in got[0]

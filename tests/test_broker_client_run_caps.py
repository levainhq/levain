"""The stop texts of the two P7-A3 run-cap GATE verdicts (``run-calls``, ``run-deadline``)."""
from __future__ import annotations

import pytest

from levain.consent import broker_client
from levain.qbroker import broker


class _Client:
    def __init__(self, answer):
        self.answer = answer

    def gate(self, **_kw):
        return self.answer


def _text(kind):
    return broker_client.gate_check(_Client((broker_client.GATE_OVER, kind)), agent="claude", tool_name="Read",
                                    tool_input={}, tool_use_id="t1")


def test_the_kind_strings_are_the_brokers():
    assert (broker.RUN_CALLS_KIND, broker.RUN_DEADLINE_KIND) == ("run-calls", "run-deadline")


@pytest.mark.parametrize("kind,text,words", [
    (broker.RUN_CALLS_KIND, broker_client.SLOT_RUN_CALLS_TEXT, "this run reached its tool-call cap"),
    (broker.RUN_DEADLINE_KIND, broker_client.SLOT_RUN_DEADLINE_TEXT, "this run reached its time limit"),
])
def test_a_run_cap_verdict_renders_its_own_stop_text(kind, text, words):
    out = _text(kind)
    assert out == text and words in out
    assert "a configured limit" not in out and "budget the operator set" not in out
    assert "another run" in out


def test_an_unknown_kind_still_renders_the_generic_text():
    assert "a configured limit" in _text("no-such-kind")

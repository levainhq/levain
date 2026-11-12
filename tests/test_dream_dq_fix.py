"""1111-38 D1-Q fix round, dream side (board row 1689): a night whose slots were claimed but never launched is not a
slept laptop (L2 MED).

Since the tick launches the slots it claims, a slot that is still not launched at its grace is ended ``missed`` with
the controller's reason (``never_launched``, ``input_unreadable``, ``no_slot``). A window whose every row ended that way
was classified ``laptop-slept`` and its card said nothing needs doing; it now has its own class, ``not-launched``,
whose remedy names the commands that show why. Written red first.
"""
from __future__ import annotations

import re
import shlex
from datetime import date

import pytest

from levain.agents import controller as ctl
from levain.dream import agent as da
from tests.test_dream_agent import AID, STORE_PATH, close, filed, row, rs  # noqa: F401 - rs is a fixture

REASONS = (ctl.NEVER_LAUNCHED, ctl.INPUT_UNREADABLE, "no_slot")


@pytest.mark.parametrize("reason", REASONS)
def test_a_night_whose_slots_were_claimed_and_never_launched_is_not_a_slept_laptop(reason):
    rows = [row("missed", reason=reason), row("missed", reason=reason, key="2026-10-31T02:20:00Z")]
    cls, detail = da.classify_window(rows)
    assert cls == "not-launched", (cls, detail)
    assert reason in detail


def test_missed_rows_with_no_launch_reason_are_still_a_slept_laptop():
    assert da.classify_window([row("missed"), row("missed", key="k2")])[0] == "laptop-slept"


def test_one_never_launched_slot_among_slept_ones_names_the_launch(rs):
    r = close(rs, [row("missed"), row("missed", reason=ctl.NEVER_LAUNCHED, key="2026-10-31T02:20:00Z")])
    assert r.action == "filed" and r.cls == "not-launched", r
    (card,) = filed(rs)
    assert card.body["reason"].startswith("not-launched")
    assert "nothing needs doing" not in card.body["remedy"]


def test_the_not_launched_remedy_names_ready_show_and_start_and_never_says_nothing_needs_doing(tmp_path):
    text = da.window_remedy("not-launched", STORE_PATH, agent_id=AID)
    assert "nothing needs doing" not in text
    cmds = re.findall(r"`([^`]*)`", text)
    ready = next(c for c in cmds if c.startswith("levain dream ready"))
    words = shlex.split(ready)
    for flag in ("--store-dir", "--agent-dir", "--agent-id"):
        assert flag in words, ready
    assert any(c.startswith("levain agents") and c.endswith("show dream") for c in cmds), cmds
    assert any(c.startswith("levain dream start") for c in cmds), cmds
    assert "levain agents tick" in text                      # where the launch failures were named

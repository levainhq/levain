"""The morning card marks an agent's presence word whose quote of the operator was not found in what he wrote."""
from __future__ import annotations

import json
import time

import pytest

from levain.coordinator import deliver, morning
from levain.webpush import presence
from levain.webpush.presence import PresenceError, set_word
from levain.webpush.quote import Match

SEAT = {"agent": "claude_code", "session": "s-1", "role": "seat"}
T = time.time() - 600.0


def _line(pres) -> str:
    c = {"at": "t", "presence": pres, "away": {}, "band": {"text": "x", "error": None}, "undecided": [], "budgets": [],
         "waiting": [], "review": [], "review_total": 0, "errors": [], "delivery_ran": None}
    mp = pytest.MonkeyPatch()
    try:
        mp.setattr(deliver, "waiting_lines", lambda w: [])
        mp.setattr(deliver, "pass_line", lambda p: "pass")
        return next(l for l in morning.render(c).splitlines() if l.startswith("presence"))
    finally:
        mp.undo()


def test_a_verified_word_carries_true_and_no_mark(tmp_path):
    set_word(tmp_path, "away", by=SEAT, said="heading out", now=T, match=Match(True, T - 5, "ok"))
    pres = morning._presence(tmp_path)
    assert pres["verified"] is True
    assert "(unverified)" not in _line(pres) and "from your words: heading out;" in _line(pres)


def test_an_unverified_word_carries_false_and_the_mark(tmp_path):
    set_word(tmp_path, "away", by=SEAT, said="heading out", now=T, match=Match(False, None, "not in a turn"))
    pres = morning._presence(tmp_path)
    assert pres["verified"] is False
    assert "from your words: heading out (unverified);" in _line(pres)


def test_a_legacy_record_with_no_verified_key_reads_as_unverified(tmp_path):
    set_word(tmp_path, "away", by=SEAT, said="heading out", now=T, match=Match(True, T - 5, "ok"))
    p = tmp_path / "webpush" / presence.FILE_NAME
    rec = json.loads(p.read_text())
    del rec["verified"]
    p.write_text(json.dumps(rec))
    pres = morning._presence(tmp_path)
    assert pres["verified"] is False
    assert "(unverified)" in _line(pres)


def test_no_setter_leaves_the_block_unchanged(tmp_path):
    set_word(tmp_path, "away", now=T)                    # the operator's own word: no setter
    with pytest.raises(PresenceError):
        set_word(tmp_path, "quiet", by=SEAT, said="be quiet", now=T + 60)   # a refusal is a row, not a setter
    pres = morning._presence(tmp_path)
    assert pres == {"word": None, "by": None, "said": None, "at": None, "agent_rows": 1, "refused": 1}
    assert "(unverified)" not in _line(pres)

"""The presence log: every word set or refused leaves a row, so the record outlives the next word."""
from __future__ import annotations

import json

import pytest

from levain.coordinator import morning
from levain.webpush import presence
from levain.webpush.presence import PresenceError, read_log, read_word, set_word

SEAT = {"agent": "claude_code", "session": "s-1", "role": "seat"}
COORD = {"agent": "claude_code", "session": "c-1", "role": "coordinator"}
T = 1_000_000.0


def test_each_word_a_setter_replaces_stays_on_record_and_the_card_names_the_setter(tmp_path):
    set_word(tmp_path, "quiet", duration="2h", now=T)
    set_word(tmp_path, "away", by=SEAT, said="anything at all", now=T + 60)
    set_word(tmp_path, "sleep", now=T + 120)
    set_word(tmp_path, "remote", by=COORD, said="I am on the train", now=T + 180)
    rows = read_log(tmp_path)
    assert [(r["word"], r["outcome"], r["prior"].get("word")) for r in rows] == [
        ("quiet", "set", None), ("away", "set", "quiet"), ("sleep", "set", "away"), ("remote", "set", "sleep")]
    assert rows[1]["by"] == SEAT and rows[1]["said"] == "anything at all" and "by" not in rows[0]
    assert rows[3]["by"] == COORD and rows[3]["prior"] == {"word": "sleep"}
    card = morning._presence(tmp_path)   # the standing word is the coordinator's; the rows are old, so no refusals
    assert card["word"] == "remote" and card["by"] == COORD and card["said"] == "I am on the train"


def test_a_refusal_leaves_a_row_and_the_word_unchanged(tmp_path):
    set_word(tmp_path, "away", now=T)
    on_file = (tmp_path / "webpush" / presence.FILE_NAME).read_text()
    with pytest.raises(PresenceError):
        set_word(tmp_path, "quiet", by=SEAT, said="be quiet", now=T + 60)
    with pytest.raises(PresenceError):
        set_word(tmp_path, "away", by=SEAT, said=None, now=T + 120)
    assert (tmp_path / "webpush" / presence.FILE_NAME).read_text() == on_file
    refused = [r for r in read_log(tmp_path) if r["outcome"] == "refused"]
    assert [(r["word"], r["by"]["role"], r["prior"]) for r in refused] == [("quiet", "seat", {"word": "away"}),
                                                                           ("away", "seat", {"word": "away"})]
    assert "operator's own word" in refused[0]["why"] and refused[0]["said"] == "be quiet"


def test_an_unwritable_log_refuses_a_setter_and_not_the_operator(tmp_path, capsys):
    (tmp_path / "webpush").mkdir()
    (tmp_path / "webpush" / presence.LOG_NAME).mkdir()   # a directory where the log file goes: appending fails
    set_word(tmp_path, "quiet", duration="10m", now=T)
    before = (tmp_path / "webpush" / presence.FILE_NAME).read_text()
    with pytest.raises(PresenceError, match="no record is not made"):
        set_word(tmp_path, "away", by=SEAT, said="heading out", now=T + 60)
    assert (tmp_path / "webpush" / presence.FILE_NAME).read_text() == before
    assert read_word(tmp_path)["word"] == "quiet"
    assert set_word(tmp_path, "sleep", now=T + 120)["word"] == "sleep"   # his own word goes through
    assert "presence log not written" in capsys.readouterr().err


def test_past_the_size_bound_the_log_keeps_only_its_newest_rows(tmp_path, monkeypatch):
    monkeypatch.setattr(presence, "LOG_MAX_BYTES", 1500)
    monkeypatch.setattr(presence, "LOG_KEEP_ROWS", 4)
    for i in range(40):
        set_word(tmp_path, "remote", duration=f"{i + 1}m", by=SEAT, said=f"word {i}", now=T + i)
    rows = read_log(tmp_path)
    assert [r["said"] for r in rows][-1] == "word 39" and 4 <= len(rows) < 40
    assert [r["said"] for r in rows] == [f"word {i}" for i in range(40 - len(rows), 40)]
    assert all(json.loads(ln) for ln in (tmp_path / "webpush" / presence.LOG_NAME).read_text().splitlines())

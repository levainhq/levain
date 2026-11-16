"""The presence log: every word set or refused leaves a row, so the record outlives the next word."""
from __future__ import annotations

import json
import time

import pytest

from levain.desk import morning
from levain.webpush import intent, presence
from levain.webpush.presence import PresenceError, read_log, read_word, set_word
from levain.webpush.quote import Match
from levain.desk.legacy import ROLE_DESK

SEAT = {"agent": "claude_code", "session": "s-1", "role": "seat"}
COORD = {"agent": "claude_code", "session": "c-1", "role": ROLE_DESK}
T = time.time() - 1800.0   # inside the card's 24 h window


def said(at: float) -> Match:
    return Match(True, at, "test: the quote is in an operator turn")


def test_each_word_a_setter_replaces_stays_on_record_and_the_card_names_the_setter(tmp_path):
    # Every clock is injected: the rows are stamped from T, the card is read at NOW (a pure function of its inputs).
    # The card used to read the wall clock while T came from collection time or from the test's start, so a run that
    # stood still for the last word's default quiet (3600 s) read the standing word as ended and the card as None
    # (the dep-drift reds of 11-13 and 11-14, a 61-minute unsharded suite; see the test below that pins the state).
    T = 1_800_000_000.0 - 1800.0
    NOW = T + 1800.0
    set_word(tmp_path, "quiet", duration="2h", now=T)
    set_word(tmp_path, "away", by=SEAT, said="anything at all", now=T + 60, match=said(T + 30))
    set_word(tmp_path, "sleep", now=T + 120)
    set_word(tmp_path, "quiet", by=COORD, said="I am on the train", now=T + 180, match=said(T + 150))
    rows = read_log(tmp_path)
    assert [(r["word"], r["outcome"], r["prior"].get("word")) for r in rows] == [
        ("quiet", "set", None), ("away", "set", "quiet"), ("sleep", "set", "away"), ("quiet", "set", "sleep")]
    assert rows[1]["by"] == SEAT and rows[1]["said"] == "anything at all" and "by" not in rows[0]
    assert rows[3]["by"] == COORD and rows[3]["prior"] == {"word": "sleep"}
    assert read_word(tmp_path, NOW, idle=lambda: None) is not None   # a swallowed fault would read None here, not as a TypeError below
    card = morning._presence(tmp_path, NOW)   # the standing word is the desk's; the rows are old, so no refusals
    assert card["word"] == "quiet" and card["by"] == COORD and card["said"] == "I am on the train"
    set_word(tmp_path, "quiet", duration="1m", by=COORD, said="back soon", now=T)   # ran out 29 minutes ago
    assert morning._presence(tmp_path, NOW) is None


def test_the_card_reads_none_once_the_default_quiet_has_run_out_and_names_the_setter_before(tmp_path):
    """The state that reddened the nightly dep-drift run: a `quiet` with no duration stands for 3600 s from its
    stamp; the card read after that is None (read_word reads an ended word as None, correctly). Pinned by injection."""
    t = 1_800_000_000.0
    set_word(tmp_path, "quiet", by=COORD, said="I am on the train", now=t, match=said(t - 30))
    for now in (t + 1, t + 3599.0):
        assert morning._presence(tmp_path, now)["by"] == COORD
    for now in (t + 3600.0, t + 4000.0):   # the 61-minute suite
        assert read_word(tmp_path, now, idle=lambda: None) is None
        assert morning._presence(tmp_path, now) is None


def test_a_refusal_leaves_a_row_and_the_word_unchanged(tmp_path):
    set_word(tmp_path, "away", now=T)
    on_file = (tmp_path / "webpush" / intent.FILE_NAME).read_text()
    with pytest.raises(PresenceError):
        set_word(tmp_path, "quiet", by=SEAT, said="be quiet", now=T + 60)
    with pytest.raises(PresenceError):
        set_word(tmp_path, "away", by=SEAT, said=None, now=T + 120)
    assert (tmp_path / "webpush" / intent.FILE_NAME).read_text() == on_file
    refused = [r for r in read_log(tmp_path) if r["outcome"] == "refused"]
    assert [(r["word"], r["by"]["role"], r["prior"]) for r in refused] == [("quiet", "seat", {"word": "away"}),
                                                                           ("away", "seat", {"word": "away"})]
    assert "operator's own word" in refused[0]["why"] and refused[0]["said"] == "be quiet"


def test_an_unwritable_log_refuses_a_setter_and_not_the_operator(tmp_path, capsys):
    (tmp_path / "webpush").mkdir()
    (tmp_path / "webpush" / presence.LOG_NAME).mkdir()   # a directory where the log file goes: appending fails
    set_word(tmp_path, "quiet", duration="10m", now=T)
    before = (tmp_path / "webpush" / intent.FILE_NAME).read_text()
    with pytest.raises(PresenceError, match="no record is not made"):
        set_word(tmp_path, "away", by=SEAT, said="heading out", now=T + 60, match=said(T + 30))
    assert (tmp_path / "webpush" / intent.FILE_NAME).read_text() == before
    assert read_word(tmp_path, now=T + 61, idle=lambda: 5.0)["word"] == "quiet"
    assert set_word(tmp_path, "sleep", now=T + 120)["word"] == "sleep"   # his own word goes through
    assert "presence log not written" in capsys.readouterr().err


def test_past_the_size_bound_the_log_rotates_and_reads_back_newest_first_kept(tmp_path, monkeypatch):
    monkeypatch.setattr(presence, "LOG_MAX_BYTES", 1500)
    for i in range(40):
        set_word(tmp_path, "away", by=SEAT, said=f"word {i}", now=T + i)
    d = tmp_path / "webpush"
    assert (d / (presence.LOG_NAME + ".1")).stat().st_size < 3000
    assert not (d / presence.LOG_NAME).exists() or (d / presence.LOG_NAME).stat().st_size < 3000
    rows = read_log(tmp_path)
    assert 2 <= len(rows) < 40 and rows[-1]["said"] == "word 39"
    assert [r["said"] for r in rows] == [f"word {i}" for i in range(40 - len(rows), 40)]
    assert [r["said"] for r in read_log(tmp_path, limit=2)] == ["word 38", "word 39"]


def test_a_setter_blocked_in_the_log_cannot_overwrite_a_loud_he_sets_meanwhile(tmp_path, monkeypatch):
    import threading

    inside, release = threading.Event(), threading.Event()
    real = presence._log

    def paused(queue_dir, row):
        if row.get("by", {}).get("role") == ROLE_DESK:
            inside.set()
            release.wait(5)
        real(queue_dir, row)

    monkeypatch.setattr(presence, "_log", paused)
    now = time.time()
    a = threading.Thread(target=lambda: set_word(tmp_path, "sleep", by=COORD, said="going to bed", now=now))
    b = threading.Thread(target=lambda: set_word(tmp_path, "loud", duration="30m", now=now + 1))
    a.start()
    assert inside.wait(5)
    b.start()
    time.sleep(0.3)   # his loud is now racing the desk's paused write
    release.set()
    a.join(5)
    b.join(5)
    assert read_word(tmp_path)["word"] == "loud"   # his loud was the later word: it stands
    assert [r["word"] for r in read_log(tmp_path)] == ["sleep", "loud"]

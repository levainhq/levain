"""PRESENCE-SEAT: a seat or the coordinator sets a presence word only on the operator's quoted words, only the words
its role allows, and the record shows who set it."""
from __future__ import annotations

import argparse
import json

import pytest

from levain.consent import band
from levain.webpush import cli, presence
from levain.webpush.presence import PresenceError, describe, read_word, set_word

SEAT = {"agent": "claude_code", "session": "abc12345-seat", "role": "seat"}
COORD = {"agent": "claude_code", "session": "def67890-coord", "role": "coordinator"}
NOW = 1_000_000.0


def _file(tmp_path):
    return tmp_path / "webpush" / presence.FILE_NAME


def _idle():
    return 10.0


@pytest.mark.parametrize("word,duration", [("away", None), ("remote", "30m")])
def test_a_seat_sets_away_and_remote_on_his_words_and_the_record_names_it(tmp_path, word, duration):
    rec = set_word(tmp_path, word, duration=duration, now=NOW, by=SEAT, said="heading out")
    assert rec["word"] == word and rec["by"] == SEAT and rec["said"] == "heading out"
    on_file = json.loads(_file(tmp_path).read_text())
    assert on_file["by"] == SEAT and on_file["said"] == "heading out"
    assert read_word(tmp_path)["by"] == SEAT


def test_status_says_who_set_it_and_from_what_words(tmp_path):
    set_word(tmp_path, "away", by=SEAT, said="heading out")
    line = describe(tmp_path, idle=_idle)
    assert line.endswith("; set by claude_code abc12345-seat from your words: heading out (unverified)")


@pytest.mark.parametrize("word", ["quiet", "sleep", "loud", "back"])
def test_a_seat_is_refused_the_words_that_mute_or_clear_and_nothing_is_written(tmp_path, word):
    with pytest.raises(PresenceError, match="operator's own word"):
        set_word(tmp_path, word, by=SEAT, said="he said so")
    assert not _file(tmp_path).exists()


def test_a_seat_cannot_clear_a_loud_either(tmp_path):
    set_word(tmp_path, "loud", duration="30m", now=NOW)
    before = _file(tmp_path).read_text()
    with pytest.raises(PresenceError, match="operator's own word"):
        set_word(tmp_path, "loud", duration="off", by=SEAT, said="he said so")
    assert _file(tmp_path).read_text() == before


@pytest.mark.parametrize("said", [None, "", "   ", "\n\t"])
def test_a_seat_without_his_words_is_refused_and_nothing_is_written(tmp_path, said):
    with pytest.raises(PresenceError, match="--said"):
        set_word(tmp_path, "away", by=SEAT, said=said)
    assert not _file(tmp_path).exists()


def test_a_refusal_leaves_the_word_already_on_file_untouched(tmp_path):
    set_word(tmp_path, "quiet", duration="10m", now=NOW)
    before = _file(tmp_path).read_text()
    with pytest.raises(PresenceError):
        set_word(tmp_path, "away", by=SEAT, said=None)
    with pytest.raises(PresenceError):
        set_word(tmp_path, "back", by=SEAT, said="he said so")
    assert _file(tmp_path).read_text() == before


def test_the_coordinator_may_also_set_quiet_and_sleep_but_not_back_or_loud(tmp_path):
    q = set_word(tmp_path, "quiet", duration="2h", now=NOW, by=COORD, said="quiet till noon please")
    assert q["by"]["role"] == "coordinator" and q["until"] == NOW + 7200
    assert "set by the coordinator claude_code def67890-coord from your words: quiet till noon please" in describe(
        tmp_path, now=NOW + 1, idle=_idle)
    assert set_word(tmp_path, "sleep", now=NOW, by=COORD, said="going to bed")["word"] == "sleep"
    for word in ("back", "loud"):
        with pytest.raises(PresenceError, match="operator's own word"):
            set_word(tmp_path, word, by=COORD, said="he said so")
    with pytest.raises(PresenceError, match="--said"):
        set_word(tmp_path, "quiet", by=COORD)


def test_an_overlong_quote_is_refused_and_control_characters_are_flattened(tmp_path):
    with pytest.raises(PresenceError, match="too long"):
        set_word(tmp_path, "away", by=SEAT, said="x" * 301)
    rec = set_word(tmp_path, "away", by=SEAT, said="heading\x1b[2J out\nnow")
    assert rec["said"] == "heading [2J out now"


def test_the_operators_own_words_are_unchanged_and_clear_the_setter(tmp_path):
    set_word(tmp_path, "away", by=SEAT, said="heading out")
    rec = set_word(tmp_path, "quiet", duration="10m", now=NOW)
    assert "by" not in rec and "said" not in rec
    assert "by" not in json.loads(_file(tmp_path).read_text())
    assert "set by" not in describe(tmp_path, now=NOW + 1, idle=_idle)
    assert set_word(tmp_path, "back") is None


def test_a_record_with_a_malformed_setter_reads_as_the_plain_word(tmp_path):
    d = tmp_path / "webpush"
    d.mkdir()
    (d / presence.FILE_NAME).write_text(json.dumps({"word": "away", "ts": NOW, "by": "claude", "said": "x"}))
    assert read_word(tmp_path) == {"word": "away", "ts": NOW}
    (d / presence.FILE_NAME).write_text(json.dumps({"word": "away", "ts": NOW, "by": SEAT}))
    assert read_word(tmp_path) == {"word": "away", "ts": NOW}


# the CLI: who is setting it is read from the same identity `levain ask` names

def _args(tmp_path, word, said=None, duration=None):
    return argparse.Namespace(queue_dir=tmp_path, word=word, duration=duration, said=said)


def _as_agent(monkeypatch, session="abc12345-seat", coordinator=False):
    from levain.coordinator import first_reader

    monkeypatch.setattr(band, "agent_process", lambda: "claude_code")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", session)
    monkeypatch.setattr(first_reader, "is_coordinator_session", lambda directory, sess: coordinator and sess == session)


def _as_operator(monkeypatch):
    monkeypatch.setattr(band, "agent_process", lambda: None)


def test_cli_a_seat_sets_away_with_said_and_status_shows_it(tmp_path, monkeypatch, capsys):
    _as_agent(monkeypatch)
    assert cli._word(_args(tmp_path, "away", said="heading out")) == 0
    assert read_word(tmp_path)["by"] == SEAT and read_word(tmp_path)["said"] == "heading out"
    assert cli._status(argparse.Namespace(queue_dir=tmp_path)) == 0
    assert "set by claude_code abc12345-seat from your words: heading out" in capsys.readouterr().out


def test_cli_a_seat_without_said_is_refused_in_words_and_writes_nothing(tmp_path, monkeypatch, capsys):
    _as_agent(monkeypatch)
    assert cli._word(_args(tmp_path, "away")) == 2
    out = capsys.readouterr().out
    assert "--said" in out and "no words, no change" in out
    assert not _file(tmp_path).exists()


def test_cli_a_seat_quiet_is_refused_with_the_reason(tmp_path, monkeypatch, capsys):
    _as_agent(monkeypatch)
    assert cli._word(_args(tmp_path, "quiet", said="he said so", duration="30m")) == 2
    assert "operator's own word" in capsys.readouterr().out
    assert not _file(tmp_path).exists()


def test_cli_the_coordinator_session_may_set_quiet(tmp_path, monkeypatch):
    _as_agent(monkeypatch, session="def67890-coord", coordinator=True)
    assert cli._word(_args(tmp_path, "quiet", said="quiet till noon", duration="1h")) == 0
    assert read_word(tmp_path)["by"] == COORD


def test_cli_the_operators_shell_needs_no_said_and_records_no_setter(tmp_path, monkeypatch):
    _as_operator(monkeypatch)
    assert cli._word(_args(tmp_path, "quiet", duration="30m")) == 0
    assert "by" not in read_word(tmp_path)
    assert cli._word(_args(tmp_path, "back")) == 0
    assert read_word(tmp_path) is None


def test_cli_the_parser_takes_said_on_every_word():
    top = argparse.ArgumentParser()
    cli.register(top.add_subparsers(dest="cmd"))
    got = top.parse_args(["ping", "away", "--said", "heading out"])
    assert got.said == "heading out" and got.word == "away"
    assert top.parse_args(["ping", "remote", "2h", "--said", "x"]).duration == "2h"
    assert top.parse_args(["ping", "back"]).said is None


# L3 round 1: identity fails closed, the role comes from the selected session, loud is the operator's

def test_cli_a_second_failed_detection_does_not_turn_a_seat_into_the_operator(tmp_path, monkeypatch, capsys):
    answers = iter(["claude_code", None, None])   # the first scan sees the seat; any later scan sees nothing
    monkeypatch.setattr(band, "agent_process", lambda: next(answers, None))
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    assert cli._word(_args(tmp_path, "quiet", duration="30m")) == 2
    assert "operator's own word" in capsys.readouterr().out
    assert not _file(tmp_path).exists()


def test_cli_a_detected_agent_whose_identity_cannot_be_named_is_refused_not_the_operator(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(band, "agent_process", lambda: "not-an-agent")
    assert cli._word(_args(tmp_path, "back")) == 2
    assert "levain ping back" in capsys.readouterr().out
    assert not _file(tmp_path).exists()


def test_cli_a_codex_seat_under_a_coordinator_is_a_seat_not_the_coordinator(tmp_path, monkeypatch, capsys):
    from levain.coordinator import first_reader

    monkeypatch.setattr(band, "agent_process", lambda: "codex")
    monkeypatch.setenv("CODEX_THREAD_ID", "codex-seat-1")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "inherited-coord")   # the coordinator's, inherited, not this seat's
    monkeypatch.setattr(first_reader, "is_coordinator_session", lambda directory, sess: sess == "inherited-coord")
    assert cli._word(_args(tmp_path, "quiet", said="quiet till noon", duration="1h")) == 2
    assert "operator's own word" in capsys.readouterr().out
    assert not _file(tmp_path).exists()


def test_cli_the_role_check_failing_reads_as_a_seat(tmp_path, monkeypatch):
    from levain.coordinator import first_reader

    def boom(directory, sess):
        raise OSError("store unreadable")

    _as_agent(monkeypatch)
    monkeypatch.setattr(first_reader, "is_coordinator_session", boom)
    assert cli._word(_args(tmp_path, "quiet", said="x", duration="1h")) == 2
    assert cli._word(_args(tmp_path, "away", said="heading out")) == 0
    assert read_word(tmp_path)["by"]["role"] == "seat"


@pytest.mark.parametrize("by,word", [(SEAT, "away"), (COORD, "quiet"), (COORD, "sleep")])
def test_a_setter_cannot_replace_a_standing_loud(tmp_path, by, word):
    set_word(tmp_path, "loud", duration="2h", now=NOW)
    before = _file(tmp_path).read_text()
    with pytest.raises(PresenceError, match="operator's standing word"):
        set_word(tmp_path, word, now=NOW + 60, by=by, said="he said so")
    assert _file(tmp_path).read_text() == before


def test_a_setter_may_write_over_a_loud_that_has_ended_and_the_operator_over_a_live_one(tmp_path):
    set_word(tmp_path, "loud", duration="1m", now=NOW)
    assert set_word(tmp_path, "away", now=NOW + 3600, by=SEAT, said="heading out")["by"] == SEAT
    set_word(tmp_path, "loud", duration="2h", now=NOW)
    assert set_word(tmp_path, "quiet", duration="10m", now=NOW + 60)["word"] == "quiet"


@pytest.mark.parametrize("by", [{"role": "coordinator"}, {"agent": " ", "session": "s", "role": "seat"},
                                {"agent": 7, "role": "seat"}, "coordinator", ["seat"]])
def test_a_setter_that_names_no_agent_is_refused_and_nothing_is_written(tmp_path, by):
    with pytest.raises(PresenceError, match="names no agent"):
        set_word(tmp_path, "away", by=by, said="heading out")
    assert not _file(tmp_path).exists()


def test_a_said_that_is_not_text_is_a_missing_quote_not_a_crash(tmp_path):
    for said in (5, ["x"], b"x"):
        with pytest.raises(PresenceError, match="--said"):
            set_word(tmp_path, "away", by=SEAT, said=said)
    assert not _file(tmp_path).exists()


def test_cli_said_help_names_the_dash_form(capsys):
    top = argparse.ArgumentParser()
    cli.register(top.add_subparsers(dest="cmd"))
    with pytest.raises(SystemExit):
        top.parse_args(["ping", "away", "-h"])
    assert '--said="-text"' in " ".join(capsys.readouterr().out.split())

"""P2: the quote a setter gives for a presence word is checked against what the operator typed, and decides what the
setter may replace. Transcript fixtures carry the shapes read from real Claude Code and Codex transcripts (10-30)."""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import time
from datetime import datetime, timezone

import pytest

from levain.consent import band
from levain.webpush import cli, presence, quote
from levain.webpush.presence import PresenceError, describe, read_log, read_word, set_word

NOW = time.time()
SEAT = {"agent": "claude_code", "session": "abc12345-seat", "role": "seat"}
SID = "abc12345-seat"


def _iso(t: float) -> str:
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _claude(at: float, content, kind: str | None = "human", **extra) -> dict:
    d = {"type": "user", "timestamp": _iso(at), "message": {"role": "user", "content": content}, **extra}
    if kind is not None:
        d["origin"] = {"kind": kind}
    return d


def _write(path, rows) -> str:
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    return str(path)


def _codex_new(at: float, text: str) -> dict:
    return {"timestamp": _iso(at), "type": "event_msg", "payload": {
        "type": "item_completed", "item": {"type": "UserMessage", "id": "i", "content": [
            {"type": "text", "text": text, "text_elements": []}]}}}


def test_a_turn_is_his_only_when_he_typed_it_a_desks_relay_of_his_words_is_not(tmp_path):
    tx = _write(tmp_path / "t.jsonl", [
        _claude(NOW - 900, "I am heading out for the evening, back after dinner"),
        _claude(NOW - 800, "PRIORITY CHANGE from the desk. Phill said he is on the train tonight", kind="peer", isMeta=True),
        _claude(NOW - 700, "<task-notification> he is stepping out for lunch </task-notification>", kind="task-notification"),
        _claude(NOW - 600, "<teammate-message> he is leaving for the airport now </teammate-message>", kind=None),
        _claude(NOW - 500, [{"type": "tool_result", "tool_use_id": "t", "content": "he is going to bed early tonight"}]),
    ])
    m = quote.match_transcript(tx, "claude_code", "heading out for the EVENING", now=NOW)
    assert m.verified and abs(m.said_at - (NOW - 900)) < 1
    for relayed in ("he is on the train tonight", "he is stepping out for lunch", "he is leaving for the airport",
                    "he is going to bed early"):
        assert not quote.match_transcript(tx, "claude_code", relayed, now=NOW).verified


def test_the_quote_must_be_long_enough_recent_and_among_the_last_twenty_turns(tmp_path):
    rows = [_claude(NOW - 8 * 3600, "going out for the whole night, see you tomorrow")]
    rows += [_claude(NOW - 3000 + i, f"filler turn number {i} about something else") for i in range(20)]
    rows.append(_claude(NOW - 60, "heading out"))
    tx = _write(tmp_path / "t.jsonl", rows)
    assert not quote.match_transcript(tx, "claude_code", "going out for the whole night", now=NOW).verified   # 8 h old, and pushed out
    assert not quote.match_transcript(tx, "claude_code", "out", now=NOW).verified   # four characters
    assert not quote.match_transcript(tx, "claude_code", "heading out", now=NOW).verified   # 11 characters
    assert quote.match_transcript(tx, "claude_code", "filler turn number 19", now=NOW).verified
    assert not quote.match_transcript(tmp_path / "gone.jsonl", "claude_code", "heading out now", now=NOW).verified


def test_codex_turns_are_read_in_both_rollout_formats_and_an_injected_user_item_is_not_one(tmp_path):
    old = {"timestamp": _iso(NOW - 600), "type": "event_msg", "payload": {"type": "user_message", "message": "leaving for the airport"}}
    injected = {"timestamp": _iso(NOW - 300), "type": "response_item", "payload": {
        "type": "message", "role": "user", "content": [{"type": "input_text", "text": "AGENTS.md says he is out until Monday"}]}}
    tx = _write(tmp_path / "rollout.jsonl", [old, _codex_new(NOW - 900, "we are heading to dinner now"), injected])
    assert quote.match_transcript(tx, "codex", "heading to dinner", now=NOW).verified
    assert quote.match_transcript(tx, "codex", "leaving for the airport", now=NOW).verified
    assert not quote.match_transcript(tx, "codex", "out until Monday", now=NOW).verified
    assert not quote.match_transcript(tx, "gemini", "heading to dinner", now=NOW).verified


@pytest.mark.parametrize("his_word,setter_word,role", [("quiet", "away", "seat"), ("away", "sleep", "coordinator"),
                                                       ("sleep", "remote", "seat")])
def test_an_invented_quote_never_replaces_a_word_he_set_himself(tmp_path, his_word, setter_word, role):
    set_word(tmp_path, his_word, now=NOW - 3000)
    on_file = (tmp_path / "webpush" / presence.FILE_NAME).read_text()
    by = {**SEAT, "role": role}
    with pytest.raises(PresenceError, match="he ends it himself"):
        set_word(tmp_path, setter_word, by=by, said="anything at all", now=NOW,
                 match=quote.Match(False, None, "the quote is in none of the last 3 operator turns"))
    assert (tmp_path / "webpush" / presence.FILE_NAME).read_text() == on_file
    row = read_log(tmp_path)[-1]
    assert row["outcome"] == "refused" and row["verified"] is False and row["prior"]["word"] == his_word


def test_a_real_quote_replaces_his_word_only_if_he_typed_it_after_that_word(tmp_path, monkeypatch, capsys):
    tx = _write(tmp_path / "t.jsonl", [_claude(NOW - 1200, "I am on the train, push everything to me")])
    monkeypatch.setattr(band, "agent_process", lambda: "claude_code")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", SID)
    monkeypatch.setattr(quote, "locate_transcript", lambda agent, session, roots: tx)
    from levain.coordinator import first_reader
    monkeypatch.setattr(first_reader, "is_coordinator_session", lambda directory, sess: False)

    def run(word="remote", said="I am on the train"):
        return cli._word(argparse.Namespace(queue_dir=tmp_path, word=word, duration=None, said=said, drop=None))

    set_word(tmp_path, "sleep", now=NOW - 600)   # his word, newer than the sentence he typed to the seat
    assert run() == 2 and "after the sentence you quote" in capsys.readouterr().out
    assert read_word(tmp_path)["word"] == "sleep"
    set_word(tmp_path, "sleep", now=NOW - 1800)   # his word, older than the sentence
    assert run() == 0
    rec = read_word(tmp_path)
    assert rec["word"] == "remote" and rec["verified"] is True
    set_word(tmp_path, "sleep", now=NOW - 1800)
    row = [r for r in read_log(tmp_path) if r["outcome"] == "set" and r.get("by")][-1]
    assert row["verified"] is True and abs(row["said_at"] - (NOW - 1200)) < 1
    assert "(unverified)" not in describe(tmp_path, idle=lambda: 10.0)


def test_with_no_transcript_a_setter_is_unverified_but_sets_over_no_word_or_a_relayed_word(tmp_path):
    no = quote.Match(False, None, "no transcript found for the session")
    rec = set_word(tmp_path, "away", by=SEAT, said="heading out now", now=NOW - 100, match=no)
    assert rec["verified"] is False and describe(tmp_path, idle=lambda: 10.0).endswith("heading out now (unverified)")
    assert read_log(tmp_path)[-1]["why"].startswith("unverified: no transcript")
    assert set_word(tmp_path, "remote", duration="30m", by=SEAT, said="on the train", now=NOW, match=no)["word"] == "remote"
    assert set_word(tmp_path, "away", by={**SEAT, "session": "other-seat-1"}, said="heading out", now=NOW + 1)["word"] == "away"


def test_a_coordinator_proves_its_quote_with_the_drop_it_names_and_a_seat_may_not(tmp_path, monkeypatch, capsys):
    db = sqlite3.connect(tmp_path / "dispatch.db")
    db.execute("CREATE TABLE drops (drop_id TEXT PRIMARY KEY, words TEXT, received_at TEXT)")
    db.execute("INSERT INTO drops VALUES ('d-1', 'closing my eyes now, hold everything until morning', ?)", (_iso(NOW - 600),))
    db.execute("INSERT INTO drops VALUES ('d-2', 'closing my eyes now, hold everything until morning', ?)", (_iso(NOW - 9 * 3600),))
    db.commit()
    db.close()
    m = quote.match_drop(tmp_path, "d-1", "hold everything until MORNING", now=NOW)
    assert m.verified and abs(m.said_at - (NOW - 600)) < 1
    assert not quote.match_drop(tmp_path, "d-2", "hold everything until morning", now=NOW).verified
    assert not quote.match_drop(tmp_path, "d-1", "not in the drop at all", now=NOW).verified
    assert not quote.match_drop(tmp_path, "d-9", "hold everything until morning", now=NOW).verified
    monkeypatch.setattr(band, "agent_process", lambda: "claude_code")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "def67890-coord")
    monkeypatch.setattr(quote, "locate_transcript", lambda agent, session, roots: None)
    from levain.coordinator import first_reader
    monkeypatch.setattr(first_reader, "is_coordinator_session", lambda directory, sess: True)
    set_word(tmp_path, "sleep", now=NOW - 3000)   # his own
    ns = argparse.Namespace(queue_dir=tmp_path, word="quiet", duration=None, said="hold everything until morning", drop="d-1")
    assert cli._word(ns) == 0 and read_word(tmp_path)["verified"] is True
    monkeypatch.setattr(first_reader, "is_coordinator_session", lambda directory, sess: False)
    assert cli._word(ns) == 2 and "--drop is the coordinator's" in capsys.readouterr().out


def test_the_transcript_is_found_by_its_session_record_else_by_the_session_id(tmp_path, monkeypatch):
    proj = tmp_path / "claude" / "projects" / "-some-other-cwd"
    proj.mkdir(parents=True)
    tx = proj / f"{SID}.jsonl"
    tx.write_text("{}\n")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    assert quote.locate_transcript("claude_code", SID, [tmp_path / "work"]) == tx   # the id names the file
    install = tmp_path / "install"
    (install / ".levain" / "sessions").mkdir(parents=True)
    far = tmp_path / "elsewhere.jsonl"
    far.write_text("{}\n")
    key = hashlib.sha1(f"claude_code:{SID}".encode()).hexdigest()[:24]
    (install / ".levain" / "sessions" / f"{key}.json").write_text(json.dumps(
        {"v": 1, "agent": "claude_code", "session_id": SID, "cwd": str(install), "transcript": str(far)}))
    sub = install / "a" / "b"
    sub.mkdir(parents=True)
    assert quote.locate_transcript("claude_code", SID, [sub]) == far   # the record, found from a subdirectory
    assert quote.locate_transcript("claude_code", "../../etc/passwd", [sub]) is None


def test_a_turn_or_a_drop_dated_in_the_future_is_not_evidence(tmp_path):
    tx = _write(tmp_path / "t.jsonl", [_claude(NOW + 3600, "I am heading out for the evening"),
                                        _claude(NOW + 60, "back on the train in a minute")])
    assert not quote.match_transcript(tx, "claude_code", "heading out for the evening", now=NOW).verified   # an hour ahead
    assert quote.match_transcript(tx, "claude_code", "back on the train", now=NOW).verified   # inside the clock skew
    db = sqlite3.connect(tmp_path / "dispatch.db")
    db.execute("CREATE TABLE drops (drop_id TEXT PRIMARY KEY, words TEXT, received_at TEXT)")
    db.execute("INSERT INTO drops VALUES ('d-f', 'closing my eyes now, hold everything', ?)", (_iso(NOW + 3600),))
    db.commit()
    db.close()
    m = quote.match_drop(tmp_path, "d-f", "hold everything", now=NOW)
    assert not m.verified and "future" in m.why


def test_a_lookup_that_fails_is_an_unverified_quote_never_a_traceback(tmp_path, monkeypatch):
    monkeypatch.setattr(band, "agent_process", lambda: "claude_code")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", SID)
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    from levain.coordinator import first_reader
    monkeypatch.setattr(first_reader, "is_coordinator_session", lambda directory, sess: False)
    q = tmp_path / "q"
    q.mkdir()
    gone = tmp_path / "cwd-gone"
    gone.mkdir()
    monkeypatch.chdir(gone)
    gone.rmdir()   # the seat's working directory was removed under it
    ns = argparse.Namespace(queue_dir=q, word="away", duration=None, said="heading out for the evening", drop=None)
    assert cli._word(ns) == 0
    assert read_word(q)["word"] == "away" and read_word(q)["verified"] is False
    monkeypatch.chdir(tmp_path)

    proj = tmp_path / "claude" / "projects" / "-x"
    proj.mkdir(parents=True)
    real_exists = quote.Path.exists
    vanished = proj / f"{SID}.jsonl"   # globbed, then removed before its stat
    monkeypatch.setattr(quote.Path, "glob", lambda self, pat: iter([vanished]))
    monkeypatch.setattr(quote.Path, "exists", lambda self: True if self == vanished else real_exists(self))
    m = quote.match("claude_code", SID, "heading out for the evening", now=NOW, roots=[tmp_path / "work"])
    assert not m.verified

    def no_home():
        raise RuntimeError("Could not determine home directory")
    monkeypatch.delenv("CLAUDE_CONFIG_DIR")
    monkeypatch.setattr(quote.Path, "home", staticmethod(no_home))
    m = quote.match("claude_code", SID, "heading out for the evening", now=NOW, roots=[tmp_path / "work"])
    assert not m.verified and "RuntimeError" in m.why


def test_a_seat_set_word_from_before_the_check_reads_as_unverified(tmp_path):
    set_word(tmp_path, "away", by=SEAT, said="heading out now", now=NOW - 100,
             match=quote.Match(True, NOW - 200, "the quote is in an operator turn of the transcript"))
    path = tmp_path / "webpush" / presence.FILE_NAME
    rec = json.loads(path.read_text())
    del rec["verified"]   # the record a seat wrote before P2
    path.write_text(json.dumps(rec))
    assert describe(tmp_path, idle=lambda: 10.0).endswith("heading out now (unverified)")
    assert read_word(tmp_path)["verified"] is False


def test_an_over_long_line_is_counted_in_the_reason_and_a_cut_first_line_is_dropped_whole(tmp_path, monkeypatch):
    monkeypatch.setattr(quote, "_LINE_CAP", 400)
    big = _claude(NOW - 300, "heading out for the evening " + "x" * 600)
    tx = _write(tmp_path / "t.jsonl", [big, _claude(NOW - 200, "some other turn entirely")])
    m = quote.match_transcript(tx, "claude_code", "heading out for the evening", now=NOW)
    assert not m.verified and "1 over-long line" in m.why
    turn = json.dumps(_claude(NOW - 100, "closing my eyes now, hold everything"))
    cut = "y" * 401 + turn   # one line; the window starts inside it, and its tail past one _LINE_CAP is valid JSON
    path = tmp_path / "cut.jsonl"
    path.write_text("{}\n" + cut + "\n" + json.dumps(_claude(NOW - 50, "a later turn of his")) + "\n")
    monkeypatch.setattr(quote, "MAX_BYTES", path.stat().st_size - 4)   # the tail starts one byte into the cut line
    assert not quote.match_transcript(path, "claude_code", "hold everything", now=NOW).verified
    assert quote.match_transcript(path, "claude_code", "a later turn of his", now=NOW).verified

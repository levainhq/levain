"""The presence that follows him: a desk message, the desk's liveness record, the Here / Away reading, and the decision record
(levain/webpush/desk.py). Each test is tied to one stated behaviour."""
from __future__ import annotations

import json
import time

from levain.webpush import desk, intent, presence

NOW = 1_800_000_000.0     # a fixed clock: nothing here depends on the real one
HID_IDLE = lambda: 3600.0     # the laptop's keyboard and mouse have been still for an hour


def _log(q):
    return presence.read_log(q)


def test_a_desk_message_ends_away_and_sleep_never_quiet_or_loud_and_counts_as_input_for_the_window(tmp_path):
    q = tmp_path
    for word in ("away", "sleep"):
        presence.set_word(q, word, now=NOW - 60)
        raw = (q / "webpush" / "intent.json").read_bytes()
        got = desk.note_desk_message(q, "test-hook", desk="desk 2", now=NOW)
        assert got["cleared"] == word and got["ended"] == word
        assert presence.read_word(q, now=NOW + 1, idle=HID_IDLE) is None             # ended by reading ...
        assert (q / "webpush" / "intent.json").read_bytes() == raw                    # ... the record is never written
    for word, dur in (("quiet", "1h"), ("loud", "1h")):
        presence.set_word(q, word, duration=dur, now=NOW - 60)
        assert desk.note_desk_message(q, "test-hook", now=NOW)["cleared"] is None
        assert presence.read_word(q, now=NOW + 1, idle=HID_IDLE)["word"] == word     # his own standing choice, kept
        presence.set_word(q, "back" if word == "quiet" else "loud", duration=None if word == "quiet" else "off", now=NOW)
    # the laptop has been still for an hour, yet a message sent 2 minutes ago holds an ordinary ping (a long message being composed)
    desk.note_desk_message(q, "test-hook", now=NOW - 120)
    held = presence.should_push(q, "ping", now=NOW, idle=HID_IDLE, suggest=False)
    assert (held.push, held.state) == (False, "holding") and "desk message 2 min ago" in held.why
    # ten minutes (the follow time) after it, with no further input, he reads away and the ping pushes
    later = presence.should_push(q, "ping", now=NOW + 700, idle=HID_IDLE, suggest=False)
    assert later.push is True
    rows = [r for r in _log(q) if r.get("word") == "desk-message"]
    assert rows and rows[-1]["via"] == "test-hook" and rows[-1]["by"]["role"] == "desk"      # a desk can wake on this row
    assert [r["why"] for r in rows if r.get("why")] == ["ended away", "ended sleep"]


def test_the_chip_reading_changes_once_per_change_and_writes_one_event_each(tmp_path):
    q = tmp_path
    assert desk.observe(q, NOW, 10.0) == "here"                              # the first reading only seeds the state
    assert desk.observe(q, NOW + 1, 20.0) == "here"
    assert desk.observe(q, NOW + 400, 400.0) == "here"                       # inside the 10 min follow time: still here
    assert desk.observe(q, NOW + 700, 700.0) == "away"                       # no input for the follow time: Away, once
    assert desk.observe(q, NOW + 710, 710.0) == "away"
    assert desk.observe(q, NOW + 800, 2.0) == "here"                         # input clears it
    words = [r["word"] for r in _log(q) if str(r.get("word", "")).startswith("auto-")]
    assert words == ["auto-away", "auto-here"]
    assert "10 min follow threshold" in [r for r in _log(q) if r["word"] == "auto-away"][0]["why"]
    assert desk.you_view(NOW, None)["state"] == "unknown"                    # no input source is not "here"
    intent.apply_settings(q, devices="follow", follow_after="30m", now=NOW, idle=lambda: 5.0)   # a custom threshold moves it
    assert desk.observe(q, NOW + 900, 900.0) == "here"


def test_the_desk_record_is_live_only_while_refreshed_and_a_closing_old_desk_leaves_its_successor(tmp_path):
    q = tmp_path
    assert desk.read_desk(q, NOW)["text"] == "No desk running" and desk.read_desk(q, NOW)["live"] is False
    assert desk.publish_desk(q, "desk 2", since=NOW - 3600, now=NOW) is True
    v = desk.read_desk(q, NOW + 60)
    assert v["live"] is True and v["name"] == "desk 2" and v["text"].startswith("Desk: desk 2 live since")
    stale = desk.read_desk(q, NOW + desk.DESK_LIVE_TTL_SECS + 5)              # the publisher stopped refreshing
    assert stale["live"] is False and stale["stale"] is True and "not heard since" in stale["text"]
    desk.publish_desk(q, "desk 3", now=NOW + 100)                             # a successor claims
    assert desk.end_desk(q, "desk 2", now=NOW + 110) is False                 # the old desk closing late does not end it
    assert desk.read_desk(q, NOW + 120)["live"] is True
    assert desk.end_desk(q, "desk 3", now=NOW + 200) is True
    gone = desk.read_desk(q, NOW + 210)
    assert gone["live"] is False and gone["text"].startswith("No desk running (last ended ")


def test_the_one_rule_never_calls_laptop_idle_the_desk_and_keeps_what_each_delivery_saw(tmp_path):
    q = tmp_path
    d = presence.should_push(q, "ping", now=NOW, idle=lambda: 900.0, suggest=True)      # a delivery decision
    assert d.push is True and "laptop" not in d.why.lower() and d.why == "follow: no desk message"      # 1a: raw laptop idle is not the desk, and not a message
    rows = [json.loads(x) for x in (q / "webpush" / desk.DECISIONS_NAME).read_text().splitlines()]
    assert rows[-1]["push"] is True and rows[-1]["idle"] == 900.0 and rows[-1]["kind"] == "ping" and rows[-1]["word"] is None
    eff = presence.effective(q, NOW, idle=lambda: 900.0)
    assert eff["you"]["state"] == "away" and eff["desk"]["live"] is False
    assert presence.should_push(q, "ping", now=NOW, idle=lambda: 900.0, suggest=False).why == eff["reason"]    # the chip and a push are one answer


def test_a_ping_held_for_an_active_desk_is_not_released_by_the_flush_while_a_desk_message_counts_as_input(tmp_path):
    from levain.webpush import held
    from levain.webpush.store import PushStore

    q = tmp_path
    PushStore(q).add_ping("ping", "r1", "t", "b", None, hold=("/", "active"))
    def pending(now):
        active, muted = held._plan_now(q, now, lambda: 3600.0)           # the laptop itself has been still for an hour
        return len(active)
    assert pending(time.time()) == 1                                     # no desk message: raw idle releases it
    desk.note_desk_message(q, "test-hook", now=time.time() - 30)
    assert pending(time.time()) == 0                                     # the same reading the delivery rule used

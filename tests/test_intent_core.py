"""Presence 1b, lane A: the one intent record (levain/webpush/intent.py). Each test is a stated behaviour of DESIGN §12."""
from __future__ import annotations

import json

from levain.webpush import desk, intent, presence

NOW = 1_800_000_000.0     # a fixed clock: nothing here depends on the real one
CLASSES_OF_KIND = ("consent", "attention", "dated", "notify_urgent", "notify", "question")


def _idle(secs):
    return lambda: secs


def _file(q):
    return (q / "webpush" / intent.FILE_NAME).read_bytes()


def _doc(q):
    return json.loads(_file(q))


def _push(q, kind, idle, now=NOW):
    return presence.should_push(q, kind, now=now, idle=_idle(idle), suggest=False)


def test_presets_write_exactly_their_table_and_back_clears_the_overlay_but_not_a_standing_loud(tmp_path):
    q = tmp_path
    want = {"away": ("everything", {"kind": "input"}),
            "quiet": ("needs", {"kind": "timer", "at": NOW + 3600}),
            "sleep": ("needs", {"kind": "input"}),
            "loud": ("everything", {"kind": "timer", "at": NOW + 3600})}
    for preset, (lets, until) in want.items():
        intent.apply_preset(q, preset, now=NOW, idle=_idle(5.0))
        doc = _doc(q)
        assert doc["overlay"] == {"preset": preset, "devices": "on", "follow_after": 600, "lets_through": lets,
                                  "ts": NOW, "until": until}, preset
        assert doc["base"]["devices"] == "follow" and doc["base"]["follow_after"] == 600 and doc["base"]["lets_through"] == "everything"
    base_before = _doc(q)["base"]
    intent.apply_preset(q, "back", now=NOW + 1, idle=_idle(5.0))        # a standing loud survives a stray back
    assert _doc(q)["overlay"]["preset"] == "loud"
    intent.apply_preset(q, "loud", duration="off", now=NOW + 2, idle=_idle(5.0))
    intent.apply_preset(q, "sleep", now=NOW + 3, idle=_idle(5.0))
    intent.apply_preset(q, "back", now=NOW + 4, idle=_idle(5.0))        # back clears any other overlay, base untouched
    assert _doc(q)["overlay"] is None and _doc(q)["base"] == base_before
    # a seat's away is the same record plus who set it and on whose words
    seat = {"agent": "claude", "session": "ab12cd", "role": "seat"}
    intent.apply_preset(q, "away", by=seat, said="heading out", now=NOW + 5, idle=_idle(5.0))
    ov = _doc(q)["overlay"]
    assert ov["preset"] == "away" and ov["by"] == seat and ov["said"] == "heading out" and ov["verified"] is False


def test_follow_pushes_at_the_threshold_and_not_before_and_a_custom_time_applies(tmp_path):
    q = tmp_path
    assert _push(q, "consent", 599.0).push is False      # the default base: follow, 600 s
    assert _push(q, "consent", 600.0).push is True
    assert _push(q, "consent", 5.0).push is False        # input again: devices off
    intent.apply_settings(q, devices="follow", follow_after="15m", now=NOW, idle=_idle(5.0))   # his standing default
    assert _doc(q)["base"]["follow_after"] == 900 and _doc(q)["overlay"] is None
    assert _push(q, "consent", 899.0).push is False
    assert _push(q, "consent", 900.0).push is True
    assert "after 15 min" in intent.describe_line(q, now=NOW, idle=_idle(5.0))


def test_an_until_input_overlay_ends_by_reading_never_by_a_write(tmp_path):
    q = tmp_path
    intent.apply_preset(q, "away", now=NOW, idle=_idle(5.0))
    before = _file(q)
    # input 100 s after the word is the parting instruction (inside the 180 s grace): still away, though he is at the laptop
    assert _push(q, "ping", 200.0, now=NOW + 300).push is True
    # input 200 s after the word is past the grace: the overlay is over and follow reads the desk
    got = _push(q, "ping", 100.0, now=NOW + 300)
    assert (got.push, got.state) == (False, "holding")
    # the same through the sensing writer: events only, the record byte-identical
    for _ in range(2):
        desk.observe(q, NOW + 300, 100.0)
    assert _file(q) == before
    assert [r["word"] for r in presence.read_log(q) if str(r.get("word", "")).startswith("ended")] == ["ended away"]
    # a desk message one second after the word ends it at once (no grace), the record untouched
    intent.apply_preset(q, "sleep", now=NOW + 1000, idle=_idle(5.0))
    before = _file(q)
    assert _push(q, "consent", 3600.0, now=NOW + 1001).push is True         # nothing has happened since: the sleep stands
    assert desk.note_desk_message(q, "test-hook", now=NOW + 1001)["ended"] == "sleep"
    got = _push(q, "consent", 3600.0, now=NOW + 1002)
    assert (got.push, got.state) == (False, "holding")                      # the message is laptop input: follow, at the desk
    assert _file(q) == before


def test_remote_input_is_not_the_desk_and_feeds_follow_only(tmp_path):
    q = tmp_path
    (q / "webpush").mkdir(mode=0o700)
    (q / "webpush" / presence.REMOTE_NAME).write_text(json.dumps({"at": NOW - 60, "source": "remote-terminal"}))
    got = _push(q, "ping", 0.0)                                  # follow, the laptop keyboard in use, a fresh remote record
    assert got.push is True and "remote terminal input 1 min ago" in got.why
    intent.apply_settings(q, devices="off", now=NOW, idle=_idle(0.0))
    got = _push(q, "ping", 0.0)                                  # what he set decides: remote feeds follow only
    assert (got.push, got.why) == (False, "devices off")
    intent.apply_settings(q, devices="follow", now=NOW, idle=_idle(0.0))
    intent.apply_preset(q, "away", now=NOW - 400, idle=_idle(5.0))
    (q / "webpush" / presence.REMOTE_NAME).write_text(json.dumps({"at": NOW - 10, "source": "remote-control"}))
    standing = intent.view(q, now=NOW, idle=_idle(3600.0))                       # the laptop still, remote input fresh
    assert standing["overlay"] is not None and standing["word"] == "away"       # remote input never ends an until-input overlay


def test_sleep_lets_approvals_and_high_priority_through_and_mutes_the_rest(tmp_path):
    q = tmp_path
    intent.apply_preset(q, "sleep", now=NOW, idle=_idle(5.0))
    for kind in ("consent", "attention", "dated", "notify_urgent"):
        assert _push(q, kind, 5.0).push is True, kind
    for kind in ("notify", "question"):
        got = _push(q, kind, 5.0)
        assert (got.push, got.state, got.by) == (False, "muted", "sleep"), kind
    assert _push(q, "notify", 5.0).why == "sleep: lets through needs only"


def test_migration_maps_a_word_and_a_route_to_one_record_is_idempotent_and_undoes_byte_identical(tmp_path):
    q = tmp_path
    d = q / "webpush"
    d.mkdir(mode=0o700)
    (d / "presence.json").write_text(json.dumps({"word": "away", "ts": NOW - 400}))
    (d / "route.json").write_text(json.dumps({"route": "devices", "ts": NOW - 300, "until": NOW + 3600, "source": "operator"}))
    (q / "claude_code.json").write_text(json.dumps({"cockpit_data_root": "/x"}))
    snap = {p.name: p.read_bytes() for p in d.glob("*.json")}
    # the projection behaves the same before the migration runs, and says so
    assert intent.describe_line(q, now=NOW, idle=_idle(5.0)).endswith("Not migrated yet: levain ping migrate.")
    dry = intent.migrate(q, dry_run=True, now=NOW)
    assert dry["status"] == "dry run" and not (d / intent.FILE_NAME).exists() and {p.name: p.read_bytes() for p in d.glob("*.json")} == snap
    got = intent.migrate(q, now=NOW)
    assert got["status"] == "migrated"
    ov = _doc(q)["overlay"]
    assert ov == {"preset": None, "devices": "on", "follow_after": 600, "lets_through": "everything", "ts": NOW - 300,
                  "until": {"kind": "timer", "at": NOW + 3600}, "via": "migrate"}
    assert _doc(q)["base"]["devices"] == "follow"
    assert any("consent away was off for claude_code" in ln for ln in got["lines"])
    assert not (d / "presence.json").exists() and not (d / "route.json").exists()
    assert intent.migrate(q, now=NOW + 1)["status"] == "already migrated"
    assert intent.describe_line(q, now=NOW, idle=_idle(5.0)).startswith("Pings now: your devices (set for a while).")
    assert intent.unmigrate(q, now=NOW + 2)["status"] == "undone"
    assert {p.name: p.read_bytes() for p in d.glob("*.json")} == snap and not (d / intent.FILE_NAME).exists()


def test_a_record_that_cannot_be_read_refuses_every_write_and_stays_byte_identical(tmp_path):
    """L3 r1 codex HIGH, reproduced (R2): a newer-version (or corrupt) intent.json was replaced by the default base on the
    next write (`levain ping away` destroyed it). Every write path refuses it now, with a repair pointer, as migrate does."""
    import pytest

    q = tmp_path
    (q / "webpush").mkdir(mode=0o700)
    for raw in (json.dumps({"version": 2, "base": {"devices": "off"}, "future": True}).encode(), b"{not json"):
        (q / "webpush" / intent.FILE_NAME).write_bytes(raw)
        for write in (lambda: intent.apply_preset(q, "away", now=NOW, idle=_idle(5.0)),
                      lambda: intent.apply_preset(q, "back", now=NOW, idle=_idle(5.0)),
                      lambda: intent.apply_settings(q, devices="on", now=NOW, idle=_idle(5.0)),
                      lambda: intent.apply_settings(q, devices="off", until="30m", now=NOW, idle=_idle(5.0)),
                      lambda: presence.set_word(q, "desk", now=NOW)):
            with pytest.raises(presence.PresenceError, match="cannot be read.*repair it, or move it aside"):
                write()
            assert _file(q) == raw


def test_a_refused_write_on_an_unmigrated_install_migrates_nothing(tmp_path):
    """L3 r1 codex MED, reproduced (R6): an invalid preset or until on an install with only the old files refused, but had
    already migrated them (intent.json written, the old files moved). The call is validated and planned against the
    projection first; only a write that will happen migrates."""
    import pytest

    q = tmp_path
    d = q / "webpush"
    d.mkdir(mode=0o700)
    old = json.dumps({"word": "away", "ts": NOW - 400})
    (d / "presence.json").write_text(old)
    for refused in (lambda: intent.apply_preset(q, "napping", now=NOW, idle=_idle(5.0)),
                    lambda: intent.apply_preset(q, "away", duration="30m", now=NOW, idle=_idle(5.0)),
                    lambda: intent.apply_settings(q, devices="on", until=5, now=NOW, idle=_idle(5.0))):
        with pytest.raises(presence.PresenceError):
            refused()
        assert not (d / intent.FILE_NAME).exists() and (d / "presence.json").read_text() == old
        assert not (d / intent.BACKUP_DIR).exists()
    intent.apply_preset(q, "quiet", duration="30m", now=NOW, idle=_idle(5.0))   # a valid write still migrates first
    assert _doc(q)["overlay"]["preset"] == "quiet" and not (d / "presence.json").exists()
    assert len(list((d / intent.BACKUP_DIR).iterdir())) == 1


def test_a_tap_alias_never_writes_the_base_nor_replaces_a_standing_preset(tmp_path):
    """L3 r1 complement MED #2 = deepseek MED (two lineages), reproduced (R4): from a tap token `desk` rewrote the base
    (the operator's standing devices off became follow, for good) and `devices` replaced a standing sleep. A tap alias is
    a bounded overlay now (desk: a day, the retired route's default), refused over a standing preset; the operator's own
    path keeps the base write."""
    import pytest

    q = tmp_path
    intent.apply_settings(q, devices="off", now=NOW, idle=_idle(5.0))
    presence.set_word(q, "desk", now=NOW, tap=True)
    doc = _doc(q)
    assert doc["base"]["devices"] == "off"
    assert doc["overlay"]["devices"] == "follow" and doc["overlay"]["until"] == {"kind": "timer", "at": NOW + 86400}
    with pytest.raises(presence.PresenceError, match="bounded while only"):
        presence.set_word(q, "desk", duration="switch", now=NOW, tap=True)
    intent.apply_preset(q, "sleep", now=NOW, idle=_idle(5.0))
    before = _file(q)
    for word in ("devices", "desk"):
        with pytest.raises(presence.PresenceError, match="`sleep` stands"):
            presence.set_word(q, word, now=NOW + 1, tap=True)
        assert _file(q) == before
    presence.set_word(q, "desk", now=NOW + 2)    # the operator's own path: unchanged, the base
    assert _doc(q)["base"]["devices"] == "follow" and _doc(q)["overlay"] is None

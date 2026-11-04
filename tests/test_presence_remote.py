"""P2: measured remote activity (ssh/mosh input, a remote-control turn) is not desk presence: it makes an ordinary ping
push to his devices and never changes the laptop reading."""
from __future__ import annotations

import json
import os
import time

import pytest

from levain.webpush import intent, presence
from levain.webpush.presence import ACTIVE_WINDOW_SECS, PresenceError

NOW = 1_000_000.0
FRESH = {"at": NOW - 120.0, "source": "remote-terminal", "age": 120.0}


def _rec(overlay=None):
    base = {"devices": "follow", "follow_after": 600, "lets_through": "everything", "ts": 0.0}
    return {"status": "file", "reason": "", "base": base, "overlay": overlay, "projection": None}


def _ov(preset, lets, until, ts=NOW - 10):
    return {"preset": preset, "devices": "on", "follow_after": 600, "lets_through": lets, "ts": ts, "until": until}


def decide(rec_or_none, idle, *, class_name="other", remote=None):
    # 1a (Phill 11-03): follow reads the newest typed desk message, not laptop input; `idle` here is that message's age
    return intent.decide(NOW, rec_or_none or _rec(), idle, class_name=class_name, remote=remote,
                         desk_msg_at=None if idle is None else NOW - idle)


def _q(tmp_path):
    q = tmp_path / "q"
    (q / "webpush").mkdir(parents=True, mode=0o700)
    return q


def _file(q):
    return q / "webpush" / presence.REMOTE_NAME


def test_a_bad_source_raises_and_writes_nothing(tmp_path):
    q = _q(tmp_path)
    for bad in ("remote", "", None, "ssh"):
        with pytest.raises(PresenceError):
            presence.note_remote_activity(q, bad)
    assert not _file(q).exists()
    assert presence.note_remote_activity(q, "remote-terminal", float("nan")) is False   # a bad time is False, never a raise
    assert not _file(q).exists()


def test_the_mark_is_monotone_and_an_older_at_is_ignored(tmp_path):
    q = _q(tmp_path)
    now = time.time()
    assert presence.note_remote_activity(q, "remote-terminal", now - 100) is True
    assert presence.note_remote_activity(q, "remote-control", now - 200) is False       # older: ignored whole
    assert json.loads(_file(q).read_text()) == {"at": now - 100, "source": "remote-terminal"}
    assert presence.note_remote_activity(q, "remote-control", now - 100) is True        # equal replaces
    assert json.loads(_file(q).read_text())["source"] == "remote-control"
    assert presence.note_remote_activity(q, "remote-terminal", now - 50) is True        # newer replaces
    assert json.loads(_file(q).read_text()) == {"at": now - 50, "source": "remote-terminal"}
    assert (os.stat(_file(q)).st_mode & 0o777) == 0o600
    assert [p.name for p in (q / "webpush").glob("*.tmp")] == []


def test_a_future_at_beyond_a_minute_is_clamped_to_now(tmp_path):
    q = _q(tmp_path)
    before = time.time()
    assert presence.note_remote_activity(q, "remote-control", before + 3600) is True
    at = json.loads(_file(q).read_text())["at"]
    assert before <= at <= time.time()
    q2 = _q(tmp_path / "x")
    t0 = time.time()
    assert presence.note_remote_activity(q2, "remote-control", t0 + 30) is True           # any future at is stamped now
    assert t0 <= json.loads(_file(q2).read_text())["at"] <= time.time()


def test_one_validator_for_at_on_write_and_read(tmp_path):
    q = _q(tmp_path)
    for bad in (10**500, float("nan"), float("inf"), float("-inf"), -1, -100.0, 0, 0.0, True, "5", 1e11):
        assert presence.note_remote_activity(q, "remote-terminal", bad) is False, bad
        assert not _file(q).exists(), bad
    for bad in ('{"at": 1' + "0" * 499 + ', "source": "remote-terminal"}', '{"at": NaN, "source": "remote-terminal"}',
                '{"at": -5, "source": "remote-terminal"}', '{"at": 0, "source": "remote-terminal"}',
                '{"at": true, "source": "remote-terminal"}'):
        _file(q).write_text(bad)
        assert presence.read_remote_activity(q, NOW) is None, bad


def test_a_future_dated_record_is_absent_to_the_writer_and_the_reader(tmp_path):
    q = _q(tmp_path)
    now = time.time()
    _file(q).write_text(json.dumps({"at": now + 3600, "source": "remote-terminal"}))   # a clock stepped back
    assert presence.read_remote_activity(q, now) is None
    assert presence.note_remote_activity(q, "remote-control", now - 5) is True          # a current write overwrites it
    assert json.loads(_file(q).read_text())["source"] == "remote-control"
    assert presence.read_remote_activity(q, now)["source"] == "remote-control"
    _file(q).write_text(json.dumps({"at": now + 30, "source": "remote-terminal"}))      # within a minute ahead: age 0
    assert presence.read_remote_activity(q, now)["age"] == 0.0


def test_read_shape_stale_absent_unreadable_and_symlink(tmp_path):
    q = _q(tmp_path)
    assert presence.read_remote_activity(q, NOW) is None                                  # absent
    _file(q).write_text(json.dumps({"at": NOW - 120, "source": "remote-terminal"}))
    got = presence.read_remote_activity(q, NOW)
    assert got == {"at": NOW - 120, "source": "remote-terminal", "age": 120.0}
    _file(q).write_text(json.dumps({"at": NOW - ACTIVE_WINDOW_SECS, "source": "remote-terminal"}))
    assert presence.read_remote_activity(q, NOW) is None                                  # stale at the window
    for bad in ("not json", json.dumps({"at": NOW, "source": "ssh"}), json.dumps({"at": "x", "source": "remote-control"}),
                json.dumps({"at": True, "source": "remote-control"}), json.dumps([1]), "x" * 2000,
                json.dumps({"at": NOW + 3600, "source": "remote-control"})):
        _file(q).write_text(bad)
        assert presence.read_remote_activity(q, NOW) is None, bad
    _file(q).unlink()
    target = tmp_path / "elsewhere.json"
    target.write_text(json.dumps({"at": NOW - 5, "source": "remote-terminal"}))
    os.symlink(target, _file(q))
    assert presence.read_remote_activity(q, NOW) is None                                  # a symlink is no record
    _file(q).unlink()
    _file(q).mkdir()
    assert presence.read_remote_activity(q, NOW) is None                                  # not a regular file
    assert presence.read_remote_activity(None, NOW) is None


def test_fresh_remote_makes_an_active_laptop_ordinary_ping_push():
    assert decide(None, 0.0).push is False                                                # control: held without it
    d = decide(None, 0.0, remote=FRESH)
    assert (d.push, d.state) == (True, "pushing")
    assert d.why == "follow: remote terminal input 2 min ago"
    _, source = intent._decide(NOW, _rec(), 0.0, "other", FRESH, None)
    assert source == "measured"
    stale = {"at": NOW - ACTIVE_WINDOW_SECS - 1, "source": "remote-control", "age": ACTIVE_WINDOW_SECS + 1}
    assert decide(None, 0.0, remote=stale).push is False                                  # a stale reading decides nothing
    assert decide(None, 0.0, remote=None).push is False


def test_typing_at_the_laptop_does_not_clear_a_fresh_remote_record():
    for idle in (0.0, 1.0, 30.0):                                                         # he is at the laptop; the record is fresh
        assert decide(None, idle, remote=FRESH).push is True


def test_remote_never_counts_as_the_desk_and_feeds_follow_only():
    """DESIGN §3 and FOR PHILL 2 (a): under explicit `off` or a mute, what he set decides; remote never ends an until-input overlay."""
    off = _rec()
    off["base"] = {**off["base"], "devices": "off"}
    assert decide(off, 900.0, remote=FRESH).push is False                                 # off: remote feeds follow only
    sleep = _rec(_ov("sleep", "needs", {"kind": "input"}, ts=NOW + 5))                    # input never lands after it: a standing mute
    d = decide(sleep, 900.0, remote=FRESH)
    assert (d.push, d.state, d.by) == (False, "muted", "sleep")                           # a mute still holds an ordinary ping
    for rec in (sleep, _rec(_ov("loud", "everything", {"kind": "timer", "at": NOW + 600})),
                _rec(_ov("quiet", "needs", {"kind": "timer", "at": NOW + 600})), _rec(_ov("away", "everything", {"kind": "input"}))):
        for cls in ("approval", "attention", "other"):
            for idle in (0.0, 900.0, None):   # an overlay has devices on already, so a remote record changes nothing it decides
                assert intent._decide(NOW, rec, idle, cls, None, None) == intent._decide(NOW, rec, idle, cls, FRESH, None), (rec["overlay"]["preset"], cls, idle)
    for idle in (0.0, 30.0, 599.0, 900.0, None):   # follow: the remote record turns devices on, and only follow
        assert decide(None, idle, remote=FRESH).push is True
    assert decide(_rec(_ov("away", "everything", {"kind": "input"})), 900.0, remote=FRESH).why == "away"   # away decides before remote


def test_effective_shape_and_the_one_function_rule(tmp_path):
    q = _q(tmp_path)
    assert presence.effective(q, NOW, idle=lambda: 0.0)["remote"] is None
    _file(q).write_text(json.dumps({"at": NOW - 120, "source": "remote-control"}))
    eff = presence.effective(q, NOW, idle=lambda: 0.0)
    assert eff["remote"] == {"source": "remote-control", "at": NOW - 120}
    assert eff["state"] == "pushing" and eff["source"] == "measured" and "remote control turn 2 min ago" in eff["reason"]
    want = presence.should_push(q, "ping", now=NOW, idle=lambda: 0.0, suggest=False)
    assert eff["pushing"] is want.push and eff["reason"] == want.why
    # a deciding overlay still shows the fresh record, so flow can tag the row
    presence.set_word(q, "quiet", duration="1h", now=NOW)
    eff = presence.effective(q, NOW, idle=lambda: 0.0)
    assert eff["state"] == "muted" and eff["remote"]["source"] == "remote-control"
    assert "remote terminal input" in presence.describe(tmp_path / "other", now=NOW, idle=lambda: 0.0, remote=FRESH)


def test_a_status_read_reads_the_remote_record_once(tmp_path, monkeypatch):
    q = _q(tmp_path)
    now = time.time()
    _file(q).write_text(json.dumps({"at": now - 60, "source": "remote-terminal"}))
    reads = []
    real = presence._read_remote_raw

    def counting(queue):
        reads.append(1)
        return real(queue)

    monkeypatch.setattr(presence, "_read_remote_raw", counting)
    v = intent.view(q, now=now, idle=lambda: 5.0)
    assert len(reads) == 1
    assert v["remote"]["source"] == "remote-terminal" and v["pushing"] is True
    assert "remote terminal input" in v["line"]


def test_a_faulted_remote_read_fails_open_in_the_status_and_in_delivery(tmp_path, monkeypatch):
    q = _q(tmp_path)

    def boom(*a, **k):
        raise RuntimeError("read blew up")

    monkeypatch.setattr(presence, "read_remote_activity", boom)
    v = intent.view(q, now=time.time(), idle=lambda: 0.0)          # active laptop: would hold
    want = presence.should_push(q, "ping", now=time.time(), idle=lambda: 0.0, suggest=False)
    assert want.push is True and want.why == presence._FAULT
    assert v["pushing"] is True and v["state"] == "pushing" and v["reason"] == presence._FAULT
    assert v["line"].startswith("Pings now: your devices (the presence record could not be read")


def test_a_lock_timeout_returns_false_and_never_raises(tmp_path, monkeypatch):
    q = _q(tmp_path)

    def busy(*a, **k):
        raise presence.PresenceBusy("held")

    monkeypatch.setattr(presence, "_locked", busy)
    assert presence.note_remote_activity(q, "remote-terminal") is False
    assert not _file(q).exists()
    monkeypatch.undo()
    # a real held lock, past the short wait
    import fcntl

    monkeypatch.setattr(presence, "_REMOTE_LOCK_WAIT_SECS", 0.1)
    fd = os.open(q / "webpush" / presence.LOCK_NAME, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        assert presence.note_remote_activity(q, "remote-control") is False
    finally:
        os.close(fd)
    assert not _file(q).exists()


def test_an_unexpected_fault_inside_the_raw_remote_read_fails_open_not_to_no_activity(tmp_path, monkeypatch):
    """The reader used to swallow any fault as 'no record', so an active desk held the ping; now the fault reaches the callers
    that read it as push."""
    q = _q(tmp_path)

    def boom(*a, **k):
        raise RuntimeError("raw read blew up")

    monkeypatch.setattr(presence, "_read_remote_raw", boom)
    with pytest.raises(RuntimeError):
        presence.read_remote_activity(q)
    d = presence.should_push(q, "ping", now=time.time(), idle=lambda: 0.0, suggest=False)
    assert d.push is True and d.why == presence._FAULT
    e = presence.effective(q, time.time(), idle=lambda: 0.0)
    assert e["pushing"] is True


def test_the_readme_says_levain_ships_no_remote_activity_reporter_until_one_exists() -> None:
    """README and CHANGELOG say nothing in Levain reports remote activity (Diogenes 11-02). If any code REFERENCE (a call, an import, a callback) appears in the package,
    this fails: update both documents to say what the new caller measures, then change this test. Docs and comments that
    only name the function are not calls."""
    import ast
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    defining = root / "levain" / "webpush" / "presence.py"
    callers = []
    for p in (root / "levain").rglob("*.py"):
        if p == defining:
            continue
        for node in ast.walk(ast.parse(p.read_text(encoding="utf-8"))):
            named = (node.id if isinstance(node, ast.Name) else node.attr if isinstance(node, ast.Attribute)
                     else [a.name for a in node.names] if isinstance(node, ast.ImportFrom) else None)
            if named == "note_remote_activity" or (isinstance(named, list) and "note_remote_activity" in named):
                callers.append(str(p.relative_to(root)))
    assert callers == [], f"a caller now exists ({callers}): the 'Levain ships no measurer' wording is stale"

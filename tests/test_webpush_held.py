"""Held phone pings: queued when the presence rule holds them, released once when it next says push."""
from __future__ import annotations

import time
from datetime import datetime, timezone

import pytest

from levain.consent.adapters import claude_code
from levain.webpush import held, intent, presence, sender
from levain.webpush.presence import set_word
from levain.webpush.store import PushStore
from tests._h4 import h4_env, h4_home
from tests._push_fixtures import phone

NOW = 1_000_000.0
ACTIVE, INACTIVE = 10.0, intent.FOLLOW_DEFAULT + 1.0


@pytest.fixture(autouse=True)
def _as_operator(monkeypatch):
    """These tests drive the operator's own CLI: run them as a shell outside any agent session, wherever the suite runs."""
    from levain.consent import band

    monkeypatch.setattr(band, "agent_process", lambda: None)


def _typed(qdir, ago, now=NOW):
    """1a (Phill 11-03): follow returns to the desk only on a typed desk message, so "at the laptop" is a message sent ``ago`` s before
    ``now``; the laptop's own input counts for nothing."""
    from levain.webpush import desk

    desk._write(qdir, desk.DESK_MESSAGE_NAME, {"at": now - ago, "source": "test", "desk": ""})


def _phone(qdir):
    phone(qdir, "https://fcm.googleapis.com/fcm/send/held-test")


def _hold(qdir, reason, kind="ruling", ref="question:1", body="b"):
    sender.send(qdir, kind=kind, ref=ref, title="levain", body=body, url="/?view=needs", push=False, hold=reason)


def _fake(monkeypatch):
    """_prepare returns a real Request-typed stand-in so deliver counts one push per subscription."""
    import urllib.request

    sent = []

    def prepare(qdir, sub, data, topic):
        sent.append(data)
        return urllib.request.Request("https://fcm.googleapis.com/x", data=b"")

    monkeypatch.setattr(sender, "_prepare", prepare)
    monkeypatch.setattr(sender, "_post", lambda req: 201)
    return sent


def test_a_held_ping_is_recorded_and_queued_and_pushes_nothing(tmp_path, monkeypatch):
    _phone(tmp_path)
    sent = _fake(monkeypatch)
    _hold(tmp_path, "active")
    assert sent == []
    assert held.count(tmp_path) == 1
    assert len(PushStore(tmp_path).record()) == 1


def test_active_held_pings_wait_for_the_window_then_push_once(tmp_path, monkeypatch):
    _phone(tmp_path)
    sent = _fake(monkeypatch)
    _hold(tmp_path, "active")
    _typed(tmp_path, ACTIVE)
    later = NOW + INACTIVE
    assert held.flush(tmp_path, now=NOW, idle=lambda: 0.0) == 0
    assert held.due(tmp_path, now=NOW, idle=lambda: 0.0) is False
    assert held.due(tmp_path, now=later, idle=lambda: 0.0) is True
    assert held.flush(tmp_path, now=later, idle=lambda: 0.0) == 1
    assert len(sent) == 1
    assert held.flush(tmp_path, now=later, idle=lambda: 0.0) == 0   # at most once
    assert held.count(tmp_path) == 0


def test_a_quiet_that_ends_sends_one_summary_then_the_held_pings_once_his_devices_are_on(tmp_path, monkeypatch):
    _phone(tmp_path)
    sent = _fake(monkeypatch)
    set_word(tmp_path, "quiet", duration="10", now=NOW)
    _hold(tmp_path, "quiet", ref="question:1", body="first")
    _hold(tmp_path, "quiet", ref="question:2", body="second")
    inside = NOW + 300
    assert held.flush(tmp_path, now=inside, idle=lambda: INACTIVE) == 0   # still quiet
    after = NOW + 700
    _typed(tmp_path, ACTIVE, now=after)
    assert held.flush(tmp_path, now=after, idle=lambda: 0.0) == 0   # the quiet is over, but he just sent a desk message: follow keeps them for the desk
    assert held.count(tmp_path) == 2 and sent == []
    after += INACTIVE
    assert held.flush(tmp_path, now=after, idle=lambda: 0.0) == 2   # no message for the follow time: devices on, one summary first
    assert [b"held while you were quiet" in d for d in sent] == [True, False, False]
    assert b"2 pings" in sent[0]
    assert [b"first" in d for d in sent[1:]] == [True, False] and b"second" in sent[2]
    assert held.flush(tmp_path, now=after, idle=lambda: 0.0) == 0
    assert len(sent) == 3


def test_sleep_lets_a_held_approval_through_at_once_and_the_rest_wait_until_his_devices_are_on(tmp_path, monkeypatch):
    _phone(tmp_path)
    sent = _fake(monkeypatch)
    set_word(tmp_path, "sleep", now=NOW)
    _hold(tmp_path, "sleep", kind="consent", ref="request:r1", body="approval held before the ruling")
    _hold(tmp_path, "sleep", kind="ruling", ref="question:1", body="ordinary held by sleep")
    # Phill 10-25: in sleep an approval still reaches him, and an ordinary ping waits
    assert held.flush(tmp_path, now=NOW + 600, idle=lambda: 600.0 + 50) == 1
    assert b"approval held before the ruling" in sent[-1] and held.count(tmp_path) == 1
    # sensed input at the laptop does not end a set sleep (np-a93deaf7 "2a"): the ordinary ping still waits
    assert held.flush(tmp_path, now=NOW + 700, idle=lambda: 20.0) == 0
    # he types a message into a session: the sleep is over, and at the laptop follow keeps the ordinary ping for the desk
    from levain.webpush import desk

    desk.note_desk_message(tmp_path, "test-hook", now=NOW + 701)
    assert held.flush(tmp_path, now=NOW + 702, idle=lambda: 5.0) == 0
    # ten minutes without input: devices on, the ordinary ping goes after its summary
    assert held.flush(tmp_path, now=NOW + 1400, idle=lambda: 700.0) == 1
    assert b"asleep" in sent[-2] and b"ordinary held by sleep" in sent[-1]


def test_loud_releases_everything_at_once_and_back_ends_a_quiet_release(tmp_path, monkeypatch):
    _phone(tmp_path)
    sent = _fake(monkeypatch)
    _hold(tmp_path, "active")
    set_word(tmp_path, "loud", duration="60", now=NOW)
    assert held.flush(tmp_path, now=NOW + 1, idle=lambda: 0.0) == 1      # loud: devices on whatever the laptop does
    set_word(tmp_path, "quiet", duration="60", now=NOW + 2)
    _hold(tmp_path, "quiet")
    set_word(tmp_path, "back", now=NOW + 5)
    _typed(tmp_path, 1.0, now=NOW + 6)
    assert held.flush(tmp_path, now=NOW + 6, idle=lambda: 0.0) == 0      # back: follow again, and he just sent a desk message
    assert held.flush(tmp_path, now=NOW + 7 + INACTIVE, idle=lambda: 0.0) == 1
    assert len(sent) == 3


def test_nothing_is_released_while_no_phone_is_live_and_it_is_kept(tmp_path, monkeypatch):
    sent = _fake(monkeypatch)
    _phone(tmp_path)
    _hold(tmp_path, "active")
    store = PushStore(tmp_path)
    store.retire_subscription("https://fcm.googleapis.com/fcm/send/held-test")
    assert held.flush(tmp_path, now=NOW, idle=lambda: INACTIVE) == 0
    assert held.count(tmp_path) == 1 and sent == []


def test_two_claims_of_one_held_ping_release_it_once(tmp_path):
    _phone(tmp_path)
    _hold(tmp_path, "active")
    store = PushStore(tmp_path)
    [item] = store.held_items()
    assert store.claim_held([item["ping_id"]]) == [item["ping_id"]]
    assert store.claim_held([item["ping_id"]]) == []


def test_count_is_zero_without_a_store_and_does_not_create_one(tmp_path):
    assert held.count(tmp_path) == 0
    assert not (tmp_path / "webpush").exists()
    assert held.due(tmp_path) is False


def test_flush_if_due_starts_the_child_only_when_something_is_due(tmp_path, monkeypatch):
    _phone(tmp_path)
    _hold(tmp_path, "active")
    started = []
    monkeypatch.setattr(held.subprocess, "Popen", lambda argv, **kw: started.append(argv))
    monkeypatch.setattr("levain.consent.auto_away.input_idle_seconds", lambda: 0.0)   # the laptop's own input decides nothing (1a)
    _typed(tmp_path, ACTIVE, now=time.time())
    assert held.flush_if_due(tmp_path) is False and started == []
    _typed(tmp_path, INACTIVE, now=time.time())
    assert held.flush_if_due(tmp_path) is True
    assert started[0][-3:-1] == ["flush", "--queue-dir"] or "flush" in started[0]


def test_the_gate_hook_helper_flushes_and_never_raises(tmp_path, monkeypatch):
    class Q:
        directory = tmp_path

    monkeypatch.setattr(held, "flush_if_due", lambda d: (_ for _ in ()).throw(RuntimeError("x")))
    assert claude_code.flush_held_pings(Q()) is False


def test_wiring_notify_queues_a_held_ping_under_its_reason(tmp_path, monkeypatch):
    _phone(tmp_path)
    calls = []
    monkeypatch.setattr(sender, "spawn", lambda qdir, **kw: calls.append(kw) or True)
    monkeypatch.setattr("levain.consent.auto_away.input_idle_seconds", lambda: 0.0)
    _typed(tmp_path, ACTIVE, now=time.time())

    def ping(kind):
        return claude_code.notify(claude_code.AdapterSettings(), "levain: x", cwd=tmp_path, kind=kind, ref="request:r1")

    ping("ruling")
    set_word(tmp_path, "quiet", duration="30")
    ping("ruling")
    ping("consent")
    set_word(tmp_path, "sleep")
    ping("consent")
    assert [(c["push"], c["hold"]) for c in calls] == [(False, "active"), (False, "quiet"), (True, None),
                                                      (True, None)]   # Phill 10-25: a consent still pings him in sleep


def test_status_says_how_many_are_held(tmp_path, monkeypatch):
    _phone(tmp_path)
    _fake(monkeypatch)
    _hold(tmp_path, "active")
    assert "Held for your devices: 1." in presence.describe(tmp_path, now=NOW, idle=lambda: ACTIVE)


def test_tick_flushes_until_stopped(tmp_path, monkeypatch):
    import threading

    stop = threading.Event()
    seen = []
    monkeypatch.setattr(held, "flush", lambda d: seen.append(d) or (len(seen) >= 2 and stop.set()))
    held.tick(tmp_path, stop, every=0.01)
    assert len(seen) == 2


def test_the_send_child_flushes_held_pings_after_a_push_but_not_after_a_held_one(tmp_path, monkeypatch):
    import io
    import json
    import sys

    from levain.webpush import __main__ as child

    flushed = []
    monkeypatch.setattr(held, "flush", lambda d: flushed.append(d))
    monkeypatch.setattr(sender, "send", lambda *a, **kw: [])

    def run(fields):
        monkeypatch.setattr(sys, "stdin", type("I", (), {"buffer": io.BytesIO(json.dumps(fields).encode())})())
        return child.main(["send", "--queue-dir", str(tmp_path), "--kind", "ruling"])

    base = {"title": "t", "body": "b", "ref": None, "url": "/"}
    assert run({**base, "push": True, "hold": None}) == 0 and flushed == [tmp_path]
    assert run({**base, "push": False, "hold": "active"}) == 0 and flushed == [tmp_path]
    assert run({**base, "push": False, "hold": "lets"}) == 0   # a mute from his base lets-through setting is a valid reason: refusing it dropped the ping (11-02)
    assert run({**base, "push": False, "hold": 3}) == 2   # a hold that is not a word is refused
    assert child.main(["flush", "--queue-dir", str(tmp_path)]) == 0 and flushed == [tmp_path, tmp_path]


# --- fix round r1: H1 (release only on success, leased claim), H2, H3, H4, H5, H6 --------------------------------------

def _status(monkeypatch, status):
    monkeypatch.setattr(sender, "_post", lambda req: status)


def test_h1_a_push_that_failed_keeps_the_ping_held_until_the_lease_ends_then_retries_it(tmp_path, monkeypatch):
    from levain.webpush.store import HELD_LEASE_SECS

    _phone(tmp_path)
    _fake(monkeypatch)
    _hold(tmp_path, "active")
    at = NOW
    for bad in (0, sender.UNKNOWN, 500):
        _status(monkeypatch, bad)
        assert held.flush(tmp_path, now=at, idle=lambda: INACTIVE) == 0
        assert held.count(tmp_path) == 1
        at += HELD_LEASE_SECS + 1   # past the lease: the next flush may retry it
    _status(monkeypatch, 201)
    assert held.flush(tmp_path, now=at, idle=lambda: INACTIVE) == 1
    assert held.count(tmp_path) == 0


def test_h1_a_claim_inside_its_lease_is_not_taken_by_a_second_flush(tmp_path, monkeypatch):
    from levain.webpush.store import HELD_LEASE_SECS

    _phone(tmp_path)
    _fake(monkeypatch)
    _hold(tmp_path, "active")
    store = PushStore(tmp_path)
    [item] = store.held_items(now=NOW)
    assert store.claim_held([item["ping_id"]], now=NOW) == [item["ping_id"]]
    assert store.claim_held([item["ping_id"]], now=NOW + 5) == []
    assert store.held_items(now=NOW + 5) == []
    assert held.count(tmp_path) == 1   # still held: status says so
    assert store.claim_held([item["ping_id"]], now=NOW + HELD_LEASE_SECS + 1) == [item["ping_id"]]


def test_h1_a_delivery_that_raises_keeps_that_ping_and_the_rest_still_go(tmp_path, monkeypatch):
    _phone(tmp_path)
    _fake(monkeypatch)
    _hold(tmp_path, "active", ref="question:1", body="one")
    _hold(tmp_path, "active", ref="question:2", body="two")
    real = sender.deliver

    def deliver(qdir, store, ping_id, **kw):
        if kw["body"] == "one":
            raise RuntimeError("boom")
        return real(qdir, store, ping_id, **kw)

    monkeypatch.setattr(sender, "deliver", deliver)
    assert held.flush(tmp_path, now=NOW, idle=lambda: INACTIVE) == 1
    assert held.count(tmp_path) == 1   # "one" stays held; "two" went


def test_h1_a_summary_that_raises_does_not_lose_the_held_pings(tmp_path, monkeypatch):
    _phone(tmp_path)
    sent = _fake(monkeypatch)
    set_word(tmp_path, "quiet", duration="10", now=NOW)
    _hold(tmp_path, "quiet", body="kept")
    real = sender.send

    def send(qdir, **kw):
        if kw["kind"] == "summary":
            raise RuntimeError("summary down")
        return real(qdir, **kw)

    monkeypatch.setattr(sender, "send", send)
    assert held.flush(tmp_path, now=NOW + 700, idle=lambda: INACTIVE) == 1
    assert held.count(tmp_path) == 0 and b"kept" in sent[0]


def test_h2_the_ping_row_and_the_held_row_are_one_transaction(tmp_path):
    import sqlite3

    _hold(tmp_path, "active")   # creates the store
    db = tmp_path / "webpush" / "webpush.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TRIGGER no_more_held BEFORE INSERT ON held BEGIN SELECT RAISE(ABORT, 'full'); END")
    conn.commit()
    conn.close()
    before = len(PushStore(tmp_path).record())
    try:
        _hold(tmp_path, "active", ref="question:2")
    except Exception:
        pass
    else:
        raise AssertionError("the held insert failed and send did not")
    assert len(PushStore(tmp_path).record()) == before   # no ping row without its held row


from tests.test_consent_queue import authority, clock, keys, queue  # noqa: E402,F401


def _ct(monkeypatch, mod, name="flush_held_pings"):
    calls = []
    monkeypatch.setattr(mod, name, lambda q: calls.append(q.directory))
    return calls


def test_h3_the_claude_code_gate_flushes_on_a_limit_return_and_when_repings_raise(queue, monkeypatch):
    from pathlib import Path

    from levain.consent.adapters import claude_code as cc

    calls = _ct(monkeypatch, cc)
    monkeypatch.setattr(cc, "_limits", lambda *a, **kw: {"limited": True})
    ev = {"hook_event_name": cc.GATE_EVENT, "tool_name": "Write", "tool_input": {"file_path": "/w/x"}, "cwd": "/w",
          "session_id": "s1"}
    assert cc.gate(ev, queue, cc.AdapterSettings(), home=Path("/home/u"), environ=h4_env()) == {"limited": True}
    assert calls == [queue.directory]
    malformed = {"hook_event_name": cc.GATE_EVENT, "tool_name": "Write", "tool_input": {}, "cwd": "/w"}
    cc.gate(malformed, queue, cc.AdapterSettings(), home=Path("/home/u"), environ=h4_env())
    assert len(calls) == 2
    monkeypatch.setattr(cc, "_limits", lambda *a, **kw: None)
    monkeypatch.setattr(cc, "send_repings", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("x")))
    try:
        cc.gate(ev, queue, cc.AdapterSettings(), home=Path("/home/u"), environ=h4_env())
    except Exception:
        pass
    assert len(calls) == 3


def test_h3_the_codex_gate_flushes_for_a_halted_session_and_a_limit_return(tmp_path, monkeypatch):
    from pathlib import Path

    from levain.consent.adapters import codex as cx
    from levain.consent import ConsentQueue, authority_source

    calls = _ct(monkeypatch, cx)
    queue = ConsentQueue(tmp_path / "q", authority=authority_source(tmp_path / "q", anchor=None), home=h4_home())
    ev = {"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": {"command": "ls"}, "cwd": "/work/p",
          "session_id": "s1", "turn_id": "t1", "tool_use_id": "c1"}
    monkeypatch.setattr(queue, "is_halted", lambda agent, session: 1)
    cx.gate(ev, queue, cx.AdapterSettings(), home=Path("/home/u"), codex_dir=Path("/home/u/.codex"), environ=h4_env())
    assert calls == [queue.directory]
    monkeypatch.setattr(queue, "is_halted", lambda agent, session: None)
    monkeypatch.setattr(cx, "_limits", lambda *a, **kw: {"limited": True})
    cx.gate(ev, queue, cx.AdapterSettings(), home=Path("/home/u"), codex_dir=Path("/home/u/.codex"), environ=h4_env())
    assert len(calls) == 2


def _word_cli(tmp_path, word, duration=None):
    import argparse

    from levain.webpush import cli

    return cli._word(argparse.Namespace(queue_dir=tmp_path, word=word, duration=duration))


def test_h4_a_word_that_changes_the_rule_starts_a_flush_of_what_is_now_due(tmp_path, monkeypatch):
    _phone(tmp_path)
    _fake(monkeypatch)
    _hold(tmp_path, "active")
    started = []
    monkeypatch.setattr(held.subprocess, "Popen", lambda argv, **kw: started.append(argv))
    monkeypatch.setattr("levain.consent.auto_away.input_idle_seconds", lambda: ACTIVE)
    assert _word_cli(tmp_path, "quiet", "30m") == 0 and started == []   # a mute releases nothing
    assert _word_cli(tmp_path, "loud", "30m") == 0
    assert len(started) == 1 and "flush" in started[0]


def test_h5_a_burst_of_due_checks_starts_one_flush_child(tmp_path, monkeypatch):
    _phone(tmp_path)
    _hold(tmp_path, "active")
    started = []
    monkeypatch.setattr(held.subprocess, "Popen", lambda argv, **kw: started.append(argv))
    monkeypatch.setattr("levain.consent.auto_away.input_idle_seconds", lambda: INACTIVE)
    results = [held.flush_if_due(tmp_path) for _ in range(5)]
    assert results == [True, False, False, False, False] and len(started) == 1


def test_h5_a_flush_already_running_makes_a_second_one_stand_down(tmp_path, monkeypatch):
    import fcntl
    import os

    _phone(tmp_path)
    sent = _fake(monkeypatch)
    _hold(tmp_path, "active")
    fd = os.open(tmp_path / "webpush" / "flush.lock", os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        assert held.flush(tmp_path, now=NOW, idle=lambda: INACTIVE) == 0
        assert held.count(tmp_path) == 1 and sent == []
    finally:
        os.close(fd)
    assert held.flush(tmp_path, now=NOW, idle=lambda: INACTIVE) == 1


def test_h6_sleep_without_an_idle_reading_stands_until_a_word_or_a_typed_turn(tmp_path):
    set_word(tmp_path, "sleep", now=NOW)
    rec = intent.read(tmp_path)
    assert intent.decide(NOW + 60, rec, None, class_name="other", desk_msg_at=None).push is False                     # no reading: it cannot see his input
    assert intent.decide(NOW + 10 * 3600, rec, None, class_name="other", desk_msg_at=None).push is False             # ... and no bound ends it (np-a93deaf7 "2a")
    for idle in (lambda: None, lambda: 5000.0, lambda: 5.0):
        text = presence.describe(tmp_path, now=NOW + 60, idle=idle)
        assert "no input reading" not in text and "a message you type into a session" in text



# --- fix round r2: E1/H1 (every device answered or was retired), H5 (one spawn under a burst) --------------------------

def _deliver_returns(monkeypatch, results):
    monkeypatch.setattr(sender, "deliver", lambda *a, **kw: list(results))


def test_e1_a_ping_is_released_only_when_every_device_answered_or_was_retired(tmp_path, monkeypatch):
    _phone(tmp_path)
    _hold(tmp_path, "active")
    cases = [
        ([(1, 201), (2, 500)], False),                        # one device failed: held for the retry
        ([(1, 201), (2, sender.UNKNOWN)], False),
        ([(1, 201), (2, 0)], False),
        ([(1, 201), (2, 429)], False),                        # a 4xx that does not retire is a failure
        ([(1, 500), (2, 410)], False),                        # no device got it
        ([], False),
        ([(1, 201), (2, 410)], True),                         # the other device is gone for good
        ([(1, 201), (2, 404)], True),
        ([(1, 201), (2, sender._Dead(400))], True),
        ([(1, 201), (2, 202)], True),
    ]
    from levain.webpush.store import HELD_LEASE_SECS

    at = NOW
    for results, released in cases:
        _deliver_returns(monkeypatch, results)
        n = held.flush(tmp_path, now=at, idle=lambda: INACTIVE)
        assert (n == 1 and held.count(tmp_path) == 0) == released, results
        if released:
            _hold(tmp_path, "active")
        at += HELD_LEASE_SECS + 1


def test_e1_a_device_with_unusable_keys_is_a_retirement_not_a_failure(tmp_path, monkeypatch):
    import urllib.request

    for n in (1, 2):
        phone(tmp_path, f"https://fcm.googleapis.com/fcm/send/e1-{n}", f"phone{n}")
    _hold(tmp_path, "active")
    monkeypatch.setattr(sender, "_prepare", lambda qdir, sub, data, topic:
                        None if sub["endpoint"].endswith("e1-2") else urllib.request.Request("https://x/", data=b""))
    monkeypatch.setattr(sender, "_post", lambda req: 201)
    assert held.flush(tmp_path, now=NOW, idle=lambda: INACTIVE) == 1
    assert held.count(tmp_path) == 0


def test_h5_concurrent_due_checks_start_exactly_one_flush_child(tmp_path, monkeypatch):
    import os
    import threading
    import time as _t

    _phone(tmp_path)
    _hold(tmp_path, "active")
    stamp = tmp_path / "webpush" / held.STAMP_NAME
    stamp.touch()
    os.utime(stamp, (NOW, NOW))   # a stale stamp: every thread sees "long ago" until one of them re-stamps
    started = []

    def popen(argv, **kw):
        _t.sleep(0.05)   # a slow spawn: every other thread arrives while this one is mid-spawn
        started.append(argv)

    real_utime = held.os.utime

    def slow_utime(*a, **kw):
        _t.sleep(0.05)   # widen the check-then-stamp window so an unguarded one is hit every run
        return real_utime(*a, **kw)

    monkeypatch.setattr(held.subprocess, "Popen", popen)
    monkeypatch.setattr(held.os, "utime", slow_utime)
    monkeypatch.setattr("levain.consent.auto_away.input_idle_seconds", lambda: INACTIVE)
    go = threading.Barrier(8)
    out = []

    def run():
        go.wait()
        out.append(held.flush_if_due(tmp_path))

    threads = [threading.Thread(target=run) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(started) == 1 and out.count(True) == 1, (out, len(started))


def test_h5_a_spawn_that_failed_leaves_no_stamp_so_the_next_call_retries(tmp_path, monkeypatch):
    _phone(tmp_path)
    _hold(tmp_path, "active")
    monkeypatch.setattr("levain.consent.auto_away.input_idle_seconds", lambda: INACTIVE)

    def boom(argv, **kw):
        raise OSError("no fork")

    monkeypatch.setattr(held.subprocess, "Popen", boom)
    assert held.flush_if_due(tmp_path) is False
    started = []
    monkeypatch.setattr(held.subprocess, "Popen", lambda argv, **kw: started.append(argv))
    assert held.flush_if_due(tmp_path) is True and len(started) == 1


def test_d1_a_site_module_without_getsitepackages_still_spawns(tmp_path, monkeypatch):
    _phone(tmp_path)
    _hold(tmp_path, "active")
    started = []
    monkeypatch.setattr(held.subprocess, "Popen", lambda argv, **kw: started.append(argv))
    monkeypatch.setattr("levain.consent.auto_away.input_idle_seconds", lambda: INACTIVE)
    monkeypatch.delattr(held.site, "getsitepackages")
    assert held.flush_if_due(tmp_path) is True and len(started) == 1


def _idle_reads(monkeypatch, value):
    reads = []

    def idle():
        reads.append(1)
        return value

    monkeypatch.setattr("levain.consent.auto_away.input_idle_seconds", idle)
    return reads


def test_d2_a_throttled_gate_inside_the_spawn_gap_reads_no_idle(tmp_path, monkeypatch):
    _phone(tmp_path)
    _hold(tmp_path, "active")
    (tmp_path / "webpush" / held.STAMP_NAME).touch()   # a child started just now
    reads = _idle_reads(monkeypatch, INACTIVE)
    started = []
    monkeypatch.setattr(held.subprocess, "Popen", lambda argv, **kw: started.append(argv))
    assert held.flush_if_due(tmp_path) is False
    assert reads == [] and started == []


def test_d2_the_word_path_without_the_throttle_still_reads_idle(tmp_path, monkeypatch):
    _phone(tmp_path)
    _hold(tmp_path, "active")
    (tmp_path / "webpush" / held.STAMP_NAME).touch()
    reads = _idle_reads(monkeypatch, INACTIVE)
    started = []
    monkeypatch.setattr(held.subprocess, "Popen", lambda argv, **kw: started.append(argv))
    assert held.flush_if_due(tmp_path, throttle=False) is True
    assert reads and len(started) == 1


def test_d3_no_live_subscription_means_nothing_is_due_and_no_child(tmp_path, monkeypatch):
    _phone(tmp_path)
    _hold(tmp_path, "active")
    PushStore(tmp_path).retire_subscription("https://fcm.googleapis.com/fcm/send/held-test")
    reads = _idle_reads(monkeypatch, INACTIVE)
    started = []
    monkeypatch.setattr(held.subprocess, "Popen", lambda argv, **kw: started.append(argv))
    assert held.due(tmp_path) is False
    assert held.flush_if_due(tmp_path) is False and started == [] and reads == []

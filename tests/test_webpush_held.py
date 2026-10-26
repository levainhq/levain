"""Held phone pings: queued when the presence rule holds them, released once when it next says push."""
from __future__ import annotations

from datetime import datetime, timezone

from levain.consent.adapters import claude_code
from levain.webpush import held, presence, sender
from levain.webpush.presence import ACTIVE_WINDOW_SECS, set_word
from levain.webpush.store import PushStore

NOW = 1_000_000.0
ACTIVE, INACTIVE = 10.0, ACTIVE_WINDOW_SECS + 1.0


def _phone(qdir):
    PushStore(qdir).subscribe_unowned("https://fcm.googleapis.com/fcm/send/held-test", "p", "a", "phone",
                                      datetime.now(timezone.utc))


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
    assert held.flush(tmp_path, now=NOW, idle=lambda: ACTIVE) == 0
    assert held.due(tmp_path, now=NOW, idle=lambda: ACTIVE) is False
    assert held.due(tmp_path, now=NOW, idle=lambda: INACTIVE) is True
    assert held.flush(tmp_path, now=NOW, idle=lambda: INACTIVE) == 1
    assert len(sent) == 1
    assert held.flush(tmp_path, now=NOW, idle=lambda: INACTIVE) == 0   # at most once
    assert held.count(tmp_path) == 0


def test_a_quiet_that_ends_sends_one_summary_then_the_held_pings_even_at_an_active_desk(tmp_path, monkeypatch):
    _phone(tmp_path)
    sent = _fake(monkeypatch)
    set_word(tmp_path, "quiet", duration="10", now=NOW)
    _hold(tmp_path, "quiet", ref="question:1", body="first")
    _hold(tmp_path, "quiet", ref="question:2", body="second")
    inside = NOW + 300
    assert held.flush(tmp_path, now=inside, idle=lambda: INACTIVE) == 0   # still quiet
    after = NOW + 700
    assert held.flush(tmp_path, now=after, idle=lambda: ACTIVE) == 2
    assert [b"held while you were quiet" in d for d in sent] == [True, False, False]
    assert b"2 pings" in sent[0]
    assert [b"first" in d for d in sent[1:]] == [True, False] and b"second" in sent[2]
    assert held.flush(tmp_path, now=after, idle=lambda: ACTIVE) == 0
    assert len(sent) == 3


def test_sleep_ends_at_his_next_input_and_releases_the_same_way(tmp_path, monkeypatch):
    _phone(tmp_path)
    sent = _fake(monkeypatch)
    set_word(tmp_path, "sleep", now=NOW)
    _hold(tmp_path, "sleep", kind="consent", ref="request:r1", body="urgent held by sleep")
    assert held.flush(tmp_path, now=NOW + 600, idle=lambda: 600.0 + 50) == 0   # no input since the word
    assert held.flush(tmp_path, now=NOW + 600, idle=lambda: 20.0) == 1   # typed 20 s ago
    assert b"asleep" in sent[0] and b"urgent held by sleep" in sent[1]


def test_loud_releases_everything_and_back_ends_a_quiet_release(tmp_path, monkeypatch):
    _phone(tmp_path)
    sent = _fake(monkeypatch)
    set_word(tmp_path, "quiet", duration="60", now=NOW)
    _hold(tmp_path, "quiet")
    set_word(tmp_path, "back", now=NOW + 5)
    assert held.flush(tmp_path, now=NOW + 6, idle=lambda: ACTIVE) == 1
    assert len(sent) == 2


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
    monkeypatch.setattr("levain.consent.auto_away.input_idle_seconds", lambda: ACTIVE)
    assert held.flush_if_due(tmp_path) is False and started == []
    monkeypatch.setattr("levain.consent.auto_away.input_idle_seconds", lambda: INACTIVE)
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
    monkeypatch.setattr("levain.consent.auto_away.input_idle_seconds", lambda: ACTIVE)

    def ping(kind):
        return claude_code.notify(claude_code.AdapterSettings(), "levain: x", cwd=tmp_path, kind=kind, ref="request:r1")

    ping("ruling")
    set_word(tmp_path, "quiet", duration="30")
    ping("ruling")
    ping("consent")
    set_word(tmp_path, "sleep")
    ping("consent")
    assert [(c["push"], c["hold"]) for c in calls] == [(False, "active"), (False, "quiet"), (True, None),
                                                      (False, "sleep")]


def test_status_says_how_many_are_held(tmp_path, monkeypatch):
    _phone(tmp_path)
    _fake(monkeypatch)
    _hold(tmp_path, "active")
    assert "1 held for the phone" in presence.describe(tmp_path, now=NOW, idle=lambda: ACTIVE)


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
    assert run({**base, "push": False, "hold": 3}) == 2   # a hold that is not a word is refused
    assert child.main(["flush", "--queue-dir", str(tmp_path)]) == 0 and flushed == [tmp_path, tmp_path]

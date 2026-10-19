"""The consent pings also go out as web push when a paired phone has pings on (P1 wiring)."""
from __future__ import annotations

from datetime import datetime, timezone

from levain import webpush
from levain.consent import delivery
from levain.consent.adapters import claude_code
from levain.webpush import sender
from levain.webpush.store import DIR_NAME, PushStore

FCM = "https://fcm.googleapis.com/fcm/send/wire-test"


def _phone(qdir):
    store = PushStore(qdir)
    store.subscribe_unowned(FCM, "p", "a", "phone", datetime.now(timezone.utc))
    return store


def _spy(monkeypatch):
    calls = []
    monkeypatch.setattr(sender, "spawn", lambda qdir, **kw: calls.append(kw) or True)
    return calls


def test_no_phone_reads_nothing_and_creates_no_store(tmp_path, monkeypatch):
    calls = _spy(monkeypatch)
    assert webpush.live(tmp_path) is False
    assert claude_code.notify(claude_code.AdapterSettings(), "levain: x waits", cwd=tmp_path) is False
    assert calls == [] and not (tmp_path / DIR_NAME).exists()
    assert claude_code.ping_configured(claude_code.AdapterSettings(), tmp_path) is False


def test_a_phone_with_pings_on_gets_the_gate_ping_with_its_ref(tmp_path, monkeypatch):
    _phone(tmp_path)
    calls = _spy(monkeypatch)
    assert webpush.live(tmp_path) is True
    assert claude_code.ping_configured(claude_code.AdapterSettings(), tmp_path) is True
    assert claude_code.notify(claude_code.AdapterSettings(), "levain: claude waits on you: Bash (request r1).",
                              cwd=tmp_path, ref="request:r1") is True
    assert calls == [{"kind": "consent", "ref": "request:r1", "title": "levain",
                      "body": "claude waits on you: Bash (request r1)."}]


def test_a_ruling_ping_reaches_the_phone_without_a_notify_command(tmp_path, monkeypatch):
    _phone(tmp_path)
    calls = _spy(monkeypatch)
    monkeypatch.setattr(claude_code, "load_settings", lambda qdir: claude_code.AdapterSettings())
    send = delivery.agent_notify(tmp_path, "claude_code", "r9")
    assert send is not None
    send("levain: ruling on gap 1 (ALLOW) waits: x.")
    assert calls and calls[0]["kind"] == "ruling" and calls[0]["ref"] == "request:r9"

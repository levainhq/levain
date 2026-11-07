"""Declarative Web Push, per device (Phill 11-07, np-029b717e (a)): OFF for every device until ``levain ping declarative on``;
a band row's ping opens its Needs card; the worker stands down for a declarative payload so its showNotification cannot replace
the OS's notification (reproduced as the cause: an iPhone tap never ran notificationclick, so no worker routing could help)."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from levain.webpush import center, declarative, sender
from levain.webpush.store import PushStore

HERE = Path(__file__).resolve().parent
SW = HERE.parent / "levain" / "webpush" / "static" / "sw.js"
ORIGIN = "https://cockpit.example.test"


def _legacy(url: str = "/?view=needs&item=np-0123abcd", **kw) -> bytes:
    return sender.build_payload("T", "a body", "np-0123abcd", url, 7, 3)


def test_the_switch_is_off_until_a_device_is_added_and_a_bad_file_reads_as_off(tmp_path):
    assert declarative.enabled_devices(tmp_path) == frozenset()
    assert declarative.set_device(tmp_path, "paired-d-750e832de5b7", True) == {"paired-d-750e832de5b7"}
    assert declarative.enabled_devices(tmp_path) == {"paired-d-750e832de5b7"}
    declarative.set_device(tmp_path, "paired-d-750e832de5b7", False)
    assert declarative.enabled_devices(tmp_path) == frozenset()
    f = tmp_path / "webpush" / declarative.FILE_NAME
    for junk in (b"not json", b'{"devices": "x"}', b'[1]', b'{"devices": ["has space", 3]}'):
        f.write_bytes(junk)
        assert declarative.enabled_devices(tmp_path) == frozenset()
    with pytest.raises(ValueError):
        declarative.set_device(tmp_path, "bad id!", True)


def test_a_band_rows_ping_opens_its_needs_card_and_every_other_ping_keeps_its_link(tmp_path):
    q = tmp_path / "q"
    q.mkdir(mode=0o700)
    s = PushStore(q)
    pid = s.add_ping("attention", "np-0123abcd", "T", "b")
    assert center.push_url(s, pid, "attention", "/sign/x", ref="np-0123abcd") == "/?view=needs&item=np-0123abcd"
    for ref in ("request:pend-1", "np-0123ABCD", "np-0123abc", None, "np-0123abcd/../x"):
        assert center.push_url(s, pid, "attention", "/sign/x", ref=ref) == f"/?view=notifications&n=p{pid}"
    assert center.push_url(s, pid, "consent", "/sign/x", ref="np-0123abcd") == f"/?view=notifications&n=p{pid}"
    assert center.push_url(s, pid, "summary", "/?view=needs", ref="np-0123abcd") == "/?view=needs"


def test_the_declarative_navigate_is_the_absolute_url_and_the_legacy_members_stay_relative():
    out = json.loads(sender.declarative_payload(_legacy(), ORIGIN))
    assert out["web_push"] == 8030 and out["notification"]["navigate"] == ORIGIN + "/?view=needs&item=np-0123abcd"
    assert out["url"] == "/?view=needs&item=np-0123abcd" and out["ping"] == 7 and "mutable" not in out
    assert sender.declarative_payload(_legacy()) == _legacy()                  # no origin, a relative url: the legacy bytes
    assert sender.declarative_payload(_legacy("//evil.example/x"), ORIGIN) == _legacy("//evil.example/x")


def test_only_a_switched_on_apple_device_gets_the_declarative_payload(tmp_path, monkeypatch):
    q = tmp_path / "q"
    q.mkdir(mode=0o700)
    s = PushStore(q)
    (q / "cockpit-origin.json").write_text(json.dumps({"cockpit_origin": ORIGIN}))
    fake = [{"id": i, "device_id": d, "endpoint": ep, "p256dh": "", "auth": ""} for i, (d, ep) in enumerate((
        ("paired-d-aaaa", "https://web.push.apple.com/A1"), ("paired-d-bbbb", "https://web.push.apple.com/B2"),
        ("paired-d-cccc", "https://fcm.googleapis.com/fcm/send/C3")), 1)]
    monkeypatch.setattr(s, "live_subscriptions", lambda device=None: fake)
    declarative.set_device(q, "paired-d-aaaa", True)
    declarative.set_device(q, "paired-d-cccc", True)       # on, but not an Apple endpoint
    seen: dict[str, object] = {}
    monkeypatch.setattr(sender, "_prepare", lambda qd, sub, data, topic, origin=None: seen.setdefault(sub["device_id"], origin))  # a device that is off is called with 4 arguments
    monkeypatch.setattr(s, "add_targets", lambda *a, **k: None)
    monkeypatch.setattr(s, "add_delivery", lambda *a, **k: None)
    pid = s.add_ping("attention", "np-0123abcd", "T", "b")
    sender.deliver(q, s, pid, kind="attention", ref="np-0123abcd", title="T", body="b", url="/x")
    assert seen == {"paired-d-aaaa": ORIGIN, "paired-d-bbbb": None, "paired-d-cccc": ORIGIN}


def _node(mode: str) -> dict:
    cp = subprocess.run(["node", str(HERE / "_sw_worker_rig.js"), str(SW), "push", mode], capture_output=True, text=True, timeout=60)
    assert cp.returncode == 0, cp.stdout + cp.stderr
    return json.loads(cp.stdout)


def test_the_worker_shows_no_notification_for_a_declarative_push_but_still_reports_received():
    got = _node("ok+declarative")
    assert got["shownCalls"] == [], "a showNotification would replace the OS's notification and lose its navigate"
    assert [p["body"]["kind"] for p in got["posts"]] == ["received"] and got["posts"][0]["body"]["ping"] == 42
    assert any(e["step"] == "push-declarative" for e in got["trace"])
    assert not any(e["step"] == "shown-pending" for e in got["trace"])
    plain = _node("ok")
    assert plain["shownCalls"] == [{"title": "T", "tag": "x"}]                 # the legacy path is untouched

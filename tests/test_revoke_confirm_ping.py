"""A test ping from the Devices panel's pings row, any time (levain 30 R8): the button rides POST /webpush/test and the
pairing card's own words; a gone subscription and an unsubscribed browser each say what to do."""
from __future__ import annotations

import os
from pathlib import Path

from levain.webpush import sender
from tests.test_cockpit_webpush import (  # noqa: F401  (fixtures and helpers shared with the pings tests)
    _ROW, _fake_push, _panel, _signed, _sub, full_chrome, pushes, rig)
from tests.test_cockpit_pairing_http import _pair
from tests.test_cockpit_flip_parity import _drive, needs_browser

NOTE = "document.querySelector('.dev-note').textContent"
TEST_BTN = f"{_ROW}.querySelector('.dev-test')"
ON = f"{_ROW}.querySelector('button').textContent === 'Turn off'"


def _shots(tmp_path: Path) -> Path:
    d = Path(os.environ.get("REVOKE_SHOTS") or tmp_path)
    d.mkdir(parents=True, exist_ok=True)
    return d


@needs_browser
def test_the_test_ping_button_sends_through_the_existing_endpoint_and_says_so(full_chrome, rig, pushes, tmp_path) -> None:
    src, qdir = rig
    with _signed(src, qdir) as (port, _h):
        _dev, tok = _pair(port, label="iPhone")
        out = _drive(tmp_path, f"http://localhost:{port}/#write_token={tok}", _panel(tok, _sub(7), [
            {"emulate": {"w": 390, "h": 844, "dpr": 2, "mobile": True}},
            {"until": ON, "ms": 20000},
            {"eval": f"[!!{TEST_BTN}, {TEST_BTN}.textContent, {TEST_BTN}.hidden]"},
            {"eval": f"{TEST_BTN}.scrollIntoView({{block: 'center'}}); 1"},
            {"shot": str(_shots(tmp_path) / "pings_row_test_button_390.png"), "full": False},
            {"eval": f"{TEST_BTN}.click() || 1"},
            {"until": f"{NOTE} && {NOTE}.includes('test ping') && !{TEST_BTN}.disabled && {NOTE}", "ms": 20000},
        ]))
    posts = out["posts"]
    assert any(p.startswith("/webpush/test ") for p in posts), posts
    v = [x for x in out["values"] if not (isinstance(x, dict) and "shot" in x)]
    assert ["Send a test ping", False] == v[4][1:], v
    assert "A test ping was sent" in v[-1], v
    assert pushes[-1] == _sub(7)["endpoint"]


@needs_browser
def test_a_gone_subscription_reads_turn_pings_on_again_and_the_button_comes_back(full_chrome, rig, pushes, monkeypatch,
                                                                              tmp_path) -> None:
    src, qdir = rig
    with _signed(src, qdir) as (port, _h):
        _dev, tok = _pair(port, label="iPhone")
        monkeypatch.setattr(sender, "_post", lambda req: 410)
        out = _drive(tmp_path, f"http://localhost:{port}/#write_token={tok}", _panel(tok, _sub(7), [
            {"until": ON, "ms": 20000},
            {"eval": f"{TEST_BTN}.click() || 1"},
            {"until": f"{NOTE}.includes('turn pings on again') && !{TEST_BTN}.disabled && {NOTE}", "ms": 20000},
        ]))
    assert "subscription is gone" in out["values"][-1], out["values"]


@needs_browser
def test_a_gone_older_subscription_of_the_same_device_is_not_called_this_browsers(full_chrome, rig, pushes, monkeypatch,
                                                                                  tmp_path) -> None:
    """L3 r1 (codex MED): one credential holds up to MAX_SUBSCRIPTIONS_PER_DEVICE live subscriptions (an earlier
    subscribe, another browser holding the same key), and /webpush/test pings them all; a 410 from another one was read
    as "this browser's subscription is gone" while this browser's own ping was accepted."""
    from tests.test_cockpit_pairing_http import _call

    src, qdir = rig
    with _signed(src, qdir) as (port, _h):
        _dev, tok = _pair(port, label="iPhone")
        assert _call(port, "POST", "/webpush/subscribe", _sub(8), token=tok)[0] == 200       # the older subscription
        monkeypatch.setattr(sender, "_post", lambda req: 410 if req.full_url.endswith("/dev8") else 201)
        out = _drive(tmp_path, f"http://localhost:{port}/#write_token={tok}", _panel(tok, _sub(7), [
            {"until": ON, "ms": 20000},
            {"eval": f"{TEST_BTN}.click() || 1"},
            {"until": f"{NOTE} && {NOTE}.includes('test ping') && !{TEST_BTN}.disabled && {NOTE}", "ms": 20000},
        ]))
    note = out["values"][-1]
    assert "This browser's subscription is gone" not in note, note
    assert "1 of this device's 2 subscriptions" in note and "dropped 1" in note, note


def test_the_test_ping_request_is_never_left_to_reject_unhandled() -> None:
    """L3 r1 (complement 5): when the 20 s wait wins the race, the api() promise can still reject later; it carries its
    own catch before the race."""
    import re

    src = (Path(__file__).resolve().parents[1] / "levain/templates/web/devices_panel.js").read_text()
    body = src[src.index("async function sendTestPing"):src.index("async function turnOff")]
    call = re.search(r"const (\w+) = api\(\"POST\", \"/webpush/test\", \{\}\);", body)
    assert call, body
    name = call.group(1)
    assert re.search(rf"{name}\.catch\(\(\) => \{{\s*[^}}]*\}}\);", body), body
    assert body.index(f"{name}.catch(") < body.index("Promise.race("), body
    assert f"Promise.race([{name}, wait])" in body, body


@needs_browser
def test_without_a_subscription_the_button_says_why_and_posts_nothing(full_chrome, rig, pushes, tmp_path) -> None:
    src, qdir = rig
    with _signed(src, qdir) as (port, _h):
        _dev, tok = _pair(port, label="iPhone")
        out = _drive(tmp_path, f"http://localhost:{port}/#write_token={tok}", [
            {"until": f"{_ROW} && !{_ROW}.hidden", "ms": 20000},
            {"eval": f"{TEST_BTN}.click() || 1"},
            {"until": f"{NOTE} && !{NOTE.replace('.textContent', '')}.hidden && {NOTE}", "ms": 20000},
            {"eval": f"{TEST_BTN}.disabled"},
        ])
    v = out["values"]
    assert "turn pings on first" in v[-2].lower() or "blocks notifications" in v[-2], v
    assert v[-1] is False
    assert not any(p.startswith("/webpush/test ") for p in out["posts"]), out["posts"]

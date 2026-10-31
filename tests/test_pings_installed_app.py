"""Pings where the browser has no push API. WebKit (every iOS and iPadOS browser) offers Notification and PushManager only to a
web app opened from the Home Screen, so a tab there has neither: the Devices panel must say how to get pings, never leave a live
"Turn on" that throws. The page runs in the headless rig; a fake user agent and a deleted push API are installed before the
page's own scripts run (the driver's ``init`` step)."""
from __future__ import annotations

import json
import os
from typing import Any

from tests.test_cockpit_k2a import _install
from tests.test_cockpit_pairing_http import T, _serve
from tests.test_cockpit_webpush import _ROW, _panel, _signed, _sub, rig, pushes  # noqa: F401 (fixtures)
from tests.test_cockpit_flip_parity import _drive, needs_browser
from tests import test_pair_card_w1b as w1b

IPAD = "Mozilla/5.0 (iPad; CPU OS 17_4 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) CriOS/124.0 Mobile/15E148 Safari/604.1"
DESKTOP = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
NO_PUSH = "delete window.Notification; delete window.PushManager;"
STANDALONE = "Object.defineProperty(navigator, 'standalone', { value: true, configurable: true });"


def _ua(ua: str) -> str:
    return f"Object.defineProperty(navigator, 'userAgent', {{ get: () => {json.dumps(ua)}, configurable: true }});"


HOME_WORDS = ("On iPhone and iPad, pings work in the Levain app on your Home Screen. Tap the Share button, then Add to Home Screen, "
              "open Levain from the Home Screen, pair it once with a code, then tap Turn on there.")
SHOTS = os.environ.get("LEVAIN_PINGS_SHOTS", "")
# errors the page raises on its own (an uncaught throw, a rejected promise) are collected here
WATCH = ("(() => { window.__errs = []; window.addEventListener('error', (e) => window.__errs.push(String(e.message)));"
         " window.addEventListener('unhandledrejection', (e) => window.__errs.push(String(e.reason))); return 1; })()")
STATE = f"{_ROW}.querySelector('.dev-meta').textContent"
BTN_LIVE = f"(() => {{ const b = {_ROW}.querySelector('button'); return !b.hidden && !b.disabled && getComputedStyle(b).display !== 'none'; }})()"


def _shot(name: str) -> list[dict[str, Any]]:
    return [{"shot": os.path.join(SHOTS, name), "full": True}] if SHOTS else []


@needs_browser
def test_an_ipad_tab_with_no_push_api_gets_the_home_screen_words_and_no_live_turn_on(rig, tmp_path) -> None:
    src, qdir = rig
    with _signed(src, qdir) as (port, _):
        out = _drive(tmp_path, f"http://localhost:{port}/#write_token={T}", [
            {"init": _ua(IPAD) + NO_PUSH},
            {"until": f"{_ROW} && !{_ROW}.hidden && {STATE}", "ms": 20000},
            {"eval": WATCH},
            {"eval": STATE},
            {"eval": BTN_LIVE},
            {"eval": f"{_ROW}.querySelector('button').click() || 1"},          # even a forced click must not throw
            {"eval": "new Promise((ok) => setTimeout(() => ok(JSON.stringify([window.__errs, document.querySelector('.dev-note').textContent])), 500))"},
            *_shot("ipad-no-push.png"),
        ])
    v = out["values"]
    assert v[2] == HOME_WORDS, v[2]
    assert v[3] is False, v[3]
    errs, note = json.loads(v[5])
    assert errs == [] and note == HOME_WORDS, (errs, note)


@needs_browser
def test_a_home_screen_app_with_push_turns_pings_on_as_before(rig, tmp_path) -> None:
    src, qdir = rig
    with _signed(src, qdir) as (port, _):
        out = _drive(tmp_path, f"http://localhost:{port}/#write_token={T}", [
            {"init": _ua(IPAD) + STANDALONE},
            *_panel(T, _sub(21), [
                {"until": f"{_ROW}.querySelector('button').textContent === 'Turn off'", "ms": 20000},
                {"eval": STATE},
            ]),
        ])
    assert out["values"][-1].startswith("on:"), out["values"]
    assert any(p.startswith("/webpush/subscribe ") for p in out["posts"]), out["posts"]


@needs_browser
def test_a_non_apple_browser_with_no_push_api_says_so_plainly(rig, tmp_path) -> None:
    src, qdir = rig
    with _signed(src, qdir) as (port, _):
        out = _drive(tmp_path, f"http://localhost:{port}/#write_token={T}", [
            {"init": _ua(DESKTOP) + NO_PUSH},
            {"until": f"{_ROW} && !{_ROW}.hidden && {STATE}", "ms": 20000},
            {"eval": WATCH},
            {"eval": STATE},
            {"eval": BTN_LIVE},
            {"eval": f"{_ROW}.querySelector('button').click() || 1"},
            {"eval": "new Promise((ok) => setTimeout(() => ok(JSON.stringify(window.__errs)), 300))"},
        ])
    v = out["values"]
    assert v[2] == "This browser does not offer pings." and v[3] is False and json.loads(v[5]) == [], v


@needs_browser
def test_the_pair_card_of_a_home_screen_app_says_it_needs_its_own_code(tmp_path, monkeypatch) -> None:
    _root, src, _ids = _install(tmp_path)
    card = "document.querySelector('#ck-unpaired .pair-installed')"
    with _serve(src, extra_hosts=[w1b.NAME]) as (port, _h):
        url = f"http://{w1b.NAME}:{port}/"
        flags = w1b._offbox_flags(port)
        installed = w1b._drive(tmp_path, monkeypatch, url, [
            {"init": STANDALONE},
            {"until": w1b.CARD, "ms": 15000},
            {"eval": f"{card} && {card}.textContent"},
        ], flags)
        browser_tab = w1b._drive(tmp_path, monkeypatch, url, [
            {"until": w1b.CARD, "ms": 15000},
            {"eval": f"{card} === null"},
        ], flags)
    assert installed["values"][-1] == ("This Home Screen app keeps its own sign-in, so it needs one pairing code: get one from the "
                                       "Devices panel in the cockpit on another device."), installed["values"]
    assert browser_tab["values"][-1] is True, browser_tab["values"]

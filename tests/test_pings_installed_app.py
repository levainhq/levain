"""Pings where the browser has no push API. WebKit (every iOS and iPadOS browser) offers Notification and PushManager only to a
web app opened from the Home Screen, so a tab there has neither: the Devices panel must say how to get pings, never leave a live
"Turn on" that throws. The page runs in the headless rig; a fake user agent and a deleted push API are installed before the
page's own scripts run (the driver's ``init`` step)."""
from __future__ import annotations

import json
import re
import os
from typing import Any

from tests.test_cockpit_k2a import _install
from tests.test_cockpit_pairing_http import T, _serve
from tests.test_cockpit_webpush import _ROW, _panel, _signed, _sub, full_chrome, rig, pushes  # noqa: F401 (fixtures)
from tests.test_cockpit_flip_parity import _drive, needs_browser
from tests import test_pair_card_w1b as w1b

IPAD = "Mozilla/5.0 (iPad; CPU OS 17_4 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) CriOS/124.0 Mobile/15E148 Safari/604.1"
DESKTOP = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
NO_PUSH = "delete window.Notification; delete window.PushManager;"
STANDALONE = "Object.defineProperty(navigator, 'standalone', { value: true, configurable: true });"


def _ua(ua: str) -> str:
    return f"Object.defineProperty(navigator, 'userAgent', {{ get: () => {json.dumps(ua)}, configurable: true }});"


HOME_WORDS = ("On iPhone and iPad, pings work in the Levain app on your Home Screen: open the Share menu (in Safari, the square with an arrow), "
              "choose Add to Home Screen, open Levain from your Home Screen, pair it once with a code, then tap Turn on there. "
              "If your browser has no Add to Home Screen, open this address in Safari.")
OLD_APPLE_WORDS = "Pings need iOS or iPadOS 16.4 or later, with Lockdown Mode off."
IPADOS_DESKTOP_MODE = ("Object.defineProperty(navigator, 'platform', { get: () => 'MacIntel', configurable: true });"
                       " Object.defineProperty(navigator, 'maxTouchPoints', { get: () => 5, configurable: true });")
# only the display-mode query says standalone: what Android Chrome and desktop installs report (navigator.standalone is iOS-only)
DISPLAY_MODE_ONLY = ("const __mm = window.matchMedia.bind(window); window.matchMedia = (q) => /display-mode: standalone/.test(q)"
                     " ? { matches: true, media: q, addEventListener() {}, removeEventListener() {} } : __mm(q);")
IN_PANEL = "document.querySelector('.dev-zone').textContent"
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
            {"eval": f"new Promise((ok) => setTimeout(() => ok(JSON.stringify([window.__errs, document.querySelector('.dev-note').textContent, {IN_PANEL}.split({json.dumps(HOME_WORDS)}).length - 1])), 500))"},
            *_shot("ipad-no-push.png"),
        ])
    v = out["values"]
    assert v[2] == HOME_WORDS, v[2]
    assert v[3] is False, v[3]
    errs, note, times = json.loads(v[5])
    assert errs == [] and note == "" and times == 1, (errs, note, times)   # a forced click adds no second copy of the sentence


@needs_browser
def test_a_home_screen_app_with_push_turns_pings_on_as_before(rig, tmp_path) -> None:
    src, qdir = rig
    with _signed(src, qdir) as (port, _):
        out = _drive(tmp_path, f"http://localhost:{port}/#write_token={T}", [
            {"init": _ua(IPAD) + STANDALONE},
            *_panel(T, _sub(21), [
                {"until": f"{_ROW}.querySelector('button').textContent === 'Turn off'", "ms": 20000},
                {"eval": STATE},
                {"eval": "JSON.stringify(window.levainPairCard.env())"},     # the detection the words hang on: false here would send an installed app to the Home Screen words
            ]),
        ])
    assert out["values"][-2].startswith("on:"), out["values"]
    assert json.loads(out["values"][-1]) == {"installed": True, "apple": True}, out["values"]
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
def test_the_pair_card_of_an_ios_home_screen_app_says_it_needs_its_own_code_and_where_it_comes_from(tmp_path, monkeypatch) -> None:
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
                                       "Devices panel on any device that is already paired."), installed["values"]
    assert browser_tab["values"][-1] is True, browser_tab["values"]


def _words_for(tmp_path, rig, init: str) -> str:
    src, qdir = rig
    with _signed(src, qdir) as (port, _):
        out = _drive(tmp_path, f"http://localhost:{port}/#write_token={T}", [
            {"init": init},
            {"until": f"{_ROW} && !{_ROW}.hidden && {STATE}", "ms": 20000},
            {"eval": STATE},
            {"eval": BTN_LIVE},
        ])
    assert out["values"][-1] is False, out["values"]
    return out["values"][-2]


@needs_browser
def test_ipados_desktop_mode_with_no_push_api_gets_the_home_screen_words(rig, tmp_path) -> None:
    # no iPad in the user agent: a Mac platform with a touch screen is how iPadOS desktop mode reports itself
    assert _words_for(tmp_path, rig, _ua(DESKTOP) + IPADOS_DESKTOP_MODE + NO_PUSH) == HOME_WORDS


@needs_browser
def test_an_installed_apple_app_with_no_push_api_names_the_os_version(rig, tmp_path) -> None:
    assert _words_for(tmp_path, rig, _ua(IPAD) + STANDALONE + NO_PUSH) == OLD_APPLE_WORDS


@needs_browser
def test_an_installed_non_apple_app_with_no_push_api_says_device_not_browser(rig, tmp_path) -> None:
    assert _words_for(tmp_path, rig, _ua(DESKTOP) + DISPLAY_MODE_ONLY + NO_PUSH) == "This device does not offer pings."


@needs_browser
def test_the_pair_card_note_is_for_the_ios_home_screen_app_only(tmp_path, monkeypatch) -> None:
    _root, src, _ids = _install(tmp_path)
    card = "document.querySelector('#ck-unpaired .pair-installed')"
    with _serve(src, extra_hosts=[w1b.NAME]) as (port, _h):
        url = f"http://{w1b.NAME}:{port}/"
        flags = w1b._offbox_flags(port)
        other = w1b._drive(tmp_path, monkeypatch, url, [
            {"init": DISPLAY_MODE_ONLY},
            {"until": w1b.CARD, "ms": 15000},
            {"eval": f"{card} === null"},
        ], flags)
    assert other["values"][-1] is True, other["values"]      # an installed app that is not iOS shares the browser's sign-in: no note



def test_the_shared_push_helper_loads_before_either_script_that_reads_it() -> None:
    # plain scripts run in the order written; a 403 that answers at once must find the helper already defined
    from levain.web_server import load_web_asset

    html = load_web_asset("dashboard.html")
    at = {n: html.index(f'<script src="/{n}.js"></script>') for n in ("pair_card", "dashboard_boot", "devices_panel")}
    assert at["pair_card"] < at["dashboard_boot"] and at["pair_card"] < at["devices_panel"], at
    assert not re.search(r"<script[^>]*src=\"/(pair_card|dashboard_boot|devices_panel)\.js\"[^>]*(async|defer)", html)


def test_the_stale_sub_note_style_outranks_the_empty_state_block() -> None:
    from levain.web_server import load_web_asset

    css = load_web_asset("dashboard.css")
    assert css.index(".dev-none.dev-stale-note") > css.index("\n.dev-none {")


FAKE_SUBSCRIBE = ("(() => { window.__unsubs = 0; Notification.requestPermission = async () => 'granted';"
                  " const fake = { endpoint: 'https://push.invalid/x', toJSON: () => ({ endpoint: 'https://push.invalid/x', keys: {} }),"
                  " unsubscribe: async () => { window.__unsubs++; return true; } };"
                  " PushManager.prototype.subscribe = async () => fake; PushManager.prototype.getSubscription = async () => null;"
                  " const f = window.fetch.bind(window);"
                  " window.fetch = (u, o) => String(u).includes('/webpush/subscribe') ? %s : f(u, o); return 1; })()")
LOST = "Promise.reject(new TypeError('network'))"
REFUSED = "Promise.resolve(new Response(JSON.stringify({message: 'not now'}), {status: 400}))"
NOTE = "document.querySelector('.dev-note').textContent"


def _turn_on_with(tmp_path, rig, answer: str):
    src, qdir = rig
    with _signed(src, qdir) as (port, _):
        out = _drive(tmp_path, f"http://localhost:{port}/#write_token={T}", [
            {"until": f"{_ROW} && !{_ROW}.hidden", "ms": 20000},
            {"eval": FAKE_SUBSCRIBE % answer},
            {"eval": f"{_ROW}.querySelector('button').click() || 1"},
            {"until": f"{NOTE}.length > 0", "ms": 20000},
            {"eval": NOTE},
            {"eval": "window.__unsubs"},
        ])
    return out["values"][-2], out["values"][-1]


@needs_browser
def test_a_lost_subscribe_answer_keeps_the_subscription_and_does_not_claim_nothing_changed(full_chrome, rig, tmp_path) -> None:
    note, unsubs = _turn_on_with(tmp_path, rig, LOST)
    assert unsubs == 0, unsubs
    assert "Nothing was changed" not in note and "Devices panel" in note, note


@needs_browser
def test_a_clear_subscribe_refusal_still_undoes_the_subscription(full_chrome, rig, tmp_path) -> None:
    note, unsubs = _turn_on_with(tmp_path, rig, REFUSED)
    assert unsubs == 1 and note == "Could not turn pings on (not now).", (note, unsubs)


def _devices_json(last_seen: object, created: str) -> str:
    dev = {"id": "d-abc123", "label": "Tablet", "created": created, "last_seen": last_seen, "revoked_at": None, "subscribed": False, "paired_by": None}
    return ("(() => { const f = window.fetch.bind(window); window.fetch = (u, o) => String(u).endsWith('/cockpit/pair/devices.json')"
            f" ? Promise.resolve(new Response({json.dumps(json.dumps({'devices': [dev]}))}, {{status: 200}})) : f(u, o); return 1; }})()")


@needs_browser
def test_an_epoch_last_seen_reads_the_same_as_iso_text(rig, tmp_path) -> None:
    import time

    src, qdir = rig
    three_days = int(time.time()) - 3 * 86400
    chips = []
    for seen in (three_days, three_days * 1000):   # seconds, then milliseconds
        with _signed(src, qdir) as (port, _):
            out = _drive(tmp_path, f"http://localhost:{port}/#write_token={T}", [
                {"init": _devices_json(seen, "2020-01-01T00:00:00+00:00")},
                {"until": "document.querySelector('.dev-list .dev-row')", "ms": 20000},
                {"eval": "(document.querySelector('.dev-list .dev-stale') || {}).textContent || ''"},
            ])
        chips.append(out["values"][-1])
    assert chips == ["not seen for 3 days"] * 2, chips

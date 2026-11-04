"""The install coach (levain 27, PWA item 4): shown in a phone tab, never in the installed app; pairing and pings are the installed app's."""
from __future__ import annotations

import json

import pytest

from levain.web_server import load_web_asset
from tests.test_cockpit_k2a import _install
from tests.test_cockpit_pairing_http import T
from tests.test_cockpit_webpush import _signed
from tests.test_pair_card_w1b import _drive, needs_browser

IOS = "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1"
ANDROID = "Mozilla/5.0 (Linux; Android 10; Pixel 3) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Mobile Safari/537.36"
DESKTOP = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"


@pytest.fixture(autouse=True)
def _as_operator(monkeypatch):
    from levain.consent import band

    monkeypatch.setattr(band, "agent_process", lambda: None)
    monkeypatch.setattr("levain.consent.auto_away.input_idle_seconds", lambda: 5.0)


def _init(user_agent: str, standalone: bool) -> str:
    # before the page's scripts: the phone's user agent, the display mode, and (Android) the browser arming its install prompt
    return ("Object.defineProperty(navigator,'userAgent',{get:()=>%s});"
            "const mm=window.matchMedia.bind(window);"
            "window.matchMedia=(q)=>/display-mode:\\s*standalone/.test(q)?{matches:%s,media:q,addEventListener(){},removeEventListener(){},addListener(){},removeListener(){}}:mm(q);"
            "window.addEventListener('DOMContentLoaded',()=>{const e=new Event('beforeinstallprompt');e.prompt=async()=>{};e.userChoice=Promise.resolve({outcome:'accepted'});window.dispatchEvent(e);});"
            % (json.dumps(user_agent), "true" if standalone else "false"))


def test_the_coach_is_served_and_loaded_before_the_pair_card():
    html = load_web_asset("dashboard.html")
    html = html[0].decode() if isinstance(html, tuple) else html
    assert html.index('/pwa_install.js') < html.index('/pair_card.js')
    assert load_web_asset("pwa_install.js") is not None


@needs_browser
@pytest.mark.parametrize("ua,standalone,coach,gated", [
    (IOS, False, True, True), (ANDROID, False, True, True),
    (IOS, True, False, False), (ANDROID, True, False, False),
    (DESKTOP, False, False, False),
])
def test_the_coach_shows_in_a_phone_tab_and_pairing_and_pings_are_the_installed_apps(tmp_path, monkeypatch, ua, standalone, coach, gated):
    src = _install(tmp_path)[1]
    q = tmp_path / "signq"
    q.mkdir(mode=0o700)
    with _signed(src, q) as (port, _):
        out = _drive(tmp_path, monkeypatch, f"http://127.0.0.1:{port}/#write_token={T}", [
            {"init": _init(ua, standalone)},
            {"until": "document.querySelector('.dev-pings') && !document.querySelector('.dev-pings').hidden", "ms": 20000},
            {"until": "document.querySelector('.dev-pings button') && (document.querySelector('.dev-pings button').hidden || document.querySelector('.dev-pings button').textContent)", "ms": 8000, "may_timeout": True},
            {"eval": "[!!document.getElementById('pwa-coach'), document.querySelector('.dev-pings button').hidden, window.levainPwa.gated(), window.levainPwa.standalone()]"},
            {"eval": "(async () => { const ui = {message: document.createElement('p'), card: document.createElement('div'), form: null};"
                     "const r = await window.levainPairCard.claim('ABCD-EFGH', ui); return [r, ui.message.textContent]; })()"},
        ])
    shown, btn_hidden, is_gated, is_standalone = out["values"][-2]
    assert shown is coach and btn_hidden is gated and is_gated is gated and is_standalone is standalone
    claimed, said = out["values"][-1]
    if gated:                                  # the claim is refused in the tab before any request is sent
        assert claimed is False and ("Add to Home Screen" in said or "Install" in said)
        assert not any("/cockpit/pair/claim" in p for p in out["posts"])
    else:                                      # not gated: the claim goes on to the server (a bad code is refused there)
        assert any("/cockpit/pair/claim" in p for p in out["posts"])

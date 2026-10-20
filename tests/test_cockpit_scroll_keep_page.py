"""The reader's place on the page across a reload, and the hero weather's refresh button. Driven in headless
Chrome against a served cockpit: a reload rebuilds the board (emptying it clamps the page to the top), so
`LevainDashboard.render` puts the scroll back; focus restored to a filter box must not scroll; the hero
weather carries a button that re-reads its panel."""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

from levain.cockpit.engine import ProviderSpec
from levain.cockpit.providers import build_default_cockpit
from levain.cockpit.results import Read
from levain.web_server import make_server
from tests.test_cockpit_flip_parity import _drive, needs_browser
from tests.test_cockpit_k2a import _install

# the page's height comes from rules that match what a repaint RECREATES (inline styles on the existing children would
# be thrown away with them, and the page would shrink for the wrong reason). The page's CSP (style-src 'self') refuses a
# <style> element, but a rule inserted through the CSSOM into an existing stylesheet applies. No padding on #board itself:
# an emptied board must make the page SHORT, or the clamp this guards never happens.
TALL = ("(() => { const ss = document.styleSheets[0]; ss.insertRule('#board > * { min-height: 1600px !important; }', ss.cssRules.length);"
        " return document.documentElement.scrollHeight; })()")
# count the renders (the boot code calls LevainDashboard.render on every load) without changing what they do
COUNT = ("(() => { const L = window.LevainDashboard, r = L.render; window.__renders = 0;"
         " L.render = function () { try { return r.apply(this, arguments); } finally { window.__renders++; } }; return 1; })()")
WEATHER = {"as_of": "2026-10-16T12:00:00+00:00", "value": 61, "unit": "F", "condition": "clear", "symbol": "clear",
           "sub": [{"label": "AQI", "value": 42, "band": "good"}, {"label": "feels", "value": "59°"}], "hi": 70, "lo": 52}


def _weather_cockpit(src: Any) -> Any:
    ck = build_default_cockpit(src)
    ck.register(ProviderSpec("weather", "reading", "Weather", "feed", lambda c: Read(value=WEATHER), region="hero"))
    return ck


class _Served:
    def __init__(self, src: Any, **kw: Any) -> None:
        self.httpd = make_server(src, host="127.0.0.1", port=0, **kw)
        self.t = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def __enter__(self) -> str:
        self.t.start()
        return f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def __exit__(self, *a: Any) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.t.join(timeout=5)


def _reload_keeps_the_scroll(tmp_path: Path, trigger: str) -> dict[str, Any]:
    _root, src, _ids = _install(tmp_path)
    with _serve_plain(src) as url:
        return _drive(tmp_path, f"{url}/", [
            {"until": "document.querySelector('#board') && document.querySelector('#board').children.length > 0 && 1"},
            {"eval": TALL},
            {"eval": COUNT},
            {"eval": "window.scrollTo(0, 1500); window.scrollY"},
            {"eval": trigger},
            {"until": "window.__renders >= 1 && window.__renders"},
            {"eval": "new Promise(r => setTimeout(() => r([window.scrollY, document.documentElement.scrollHeight]), 400))"},
        ])


def _serve_plain(src: Any) -> _Served:
    return _Served(src, surface_tokens={"browser": "T"}, install_class="reported")


@needs_browser
def test_the_refresh_button_keeps_the_readers_place(tmp_path: Path) -> None:
    out = _reload_keeps_the_scroll(tmp_path, "document.getElementById('refresh').click(); 1")
    v = out["values"]
    assert v[1] > 2600, f"the page must be tall enough to scroll: {v}"
    assert abs(v[3] - 1500) <= 2, f"the scroll was not set: {v}"
    assert abs(v[6][0] - 1500) <= 2, f"a reload moved the page: {v}"


@needs_browser
def test_a_tab_coming_back_keeps_the_readers_place(tmp_path: Path) -> None:
    trig = ("Object.defineProperty(document, 'visibilityState', {configurable: true, get: () => 'visible'});"
            " document.dispatchEvent(new Event('visibilitychange')); 1")
    out = _reload_keeps_the_scroll(tmp_path, trig)
    v = out["values"]
    assert v[1] > 2600 and abs(v[3] - 1500) <= 2, v
    assert abs(v[6][0] - 1500) <= 2, f"a reload moved the page: {v}"


@needs_browser
def test_the_hero_weather_has_a_button_that_rereads_its_panel(tmp_path: Path) -> None:
    _root, src, _ids = _install(tmp_path)
    with _Served(src, cockpit=_weather_cockpit(src), surface_tokens={"browser": "T"}, install_class="reported") as url:
        btn = "document.querySelector('[data-ck-hero=weather] button.wx-refresh')"
        out = _drive(tmp_path, f"{url}/", [
            {"until": f"{btn} && 1"},
            # the button sits in flow: it overlaps none of the figure, the bounds or the sub-readings
            {"eval": f"(() => {{ const b = {btn}.getBoundingClientRect(); const hit = (e) => {{ const r = e.getBoundingClientRect();"
                     " return r.width && r.height && b.left < r.right && r.left < b.right && b.top < r.bottom && r.top < b.bottom; };"
                     " return [...document.querySelectorAll('[data-ck-hero=weather] .wx-main > *, [data-ck-hero=weather] .aqi,"
                     " [data-ck-hero=weather] .wx-src')].filter(hit).map(e => e.className); })()"},
            # a fetch counter, installed before the click: every GET that names the weather panel
            {"eval": "(() => { const f = window.fetch; window.__wx = []; window.fetch = function (u, o) {"
                     " const url = typeof u === 'string' ? u : u.url; const m = ((o && o.method) || 'GET').toUpperCase();"
                     " if (m === 'GET' && /weather/.test(url)) window.__wx.push(url); return f.apply(this, arguments); }; return 1; })()"},
            {"eval": f"{btn}.click(); 1"},
            {"until": "window.__wx.length >= 1 && window.__wx"},
            {"until": f"{btn} && !{btn}.hasAttribute('aria-busy') && !{btn}.disabled && 1"},
        ])
    v = out["values"]
    assert v[0] is True or v[0] == 1, v
    assert v[1] == [], f"the refresh button overlaps the weather: {v[1]}"
    assert isinstance(v[4], list) and v[4], f"no GET for the weather panel was sent: {v}"
    assert v[5] in (True, 1), f"the button is gone or stuck busy after the refetch: {v}"


@needs_browser
def test_refocusing_the_filter_box_after_a_reload_does_not_scroll_the_page(tmp_path: Path) -> None:
    """The reader focused a panel's filter box, then scrolled away from it. A reload redraws the box and puts
    focus back; focusing must not drag the page to the box (`focus({preventScroll: true})`)."""
    _root, src, _ids = _install(tmp_path)
    box = "document.querySelector('#board .xfilter-input')"
    with _serve_plain(src) as url:
        out = _drive(tmp_path, f"{url}/", [
            {"until": f"{box} && 1"},
            {"eval": TALL},
            {"eval": COUNT},
            {"eval": f"(() => {{ {box}.focus(); window.scrollTo(0, 1500); const r = {box}.getBoundingClientRect();"
                     " return [window.scrollY, r.bottom < 0 || r.top > innerHeight]; })()"},
            {"eval": "document.getElementById('refresh').click(); 1"},
            {"until": "window.__renders >= 1 && window.__renders"},
            # long enough for the 0ms focus restore to have run and any scroll it caused to land
            {"eval": f"new Promise(r => setTimeout(() => r([window.scrollY, document.activeElement === {box}]), 600))"},
        ])
    v = out["values"]
    assert v[3][1] is True and abs(v[3][0] - 1500) <= 2, f"the filter box must start off-screen: {v}"
    assert v[6][1] is True, f"the filter box was not refocused after the reload (the test would prove nothing): {v}"
    assert abs(v[6][0] - 1500) <= 2, f"refocusing the filter box scrolled the page: {v}"


@needs_browser
def test_slash_focuses_the_hovered_filter_and_brings_an_off_screen_one_into_view(tmp_path: Path) -> None:
    """`/` focuses the hovered panel's filter box: where it is when it is on screen, scrolled to when the keyboard
    has scrolled it away (typing into a box the reader cannot see)."""
    _root, src, _ids = _install(tmp_path)
    box = "document.querySelector('#board .xfilter-input')"
    on_screen = f"(() => {{ const r = {box}.getBoundingClientRect(); return r.bottom > 0 && r.top < innerHeight; }})()"
    slash = "document.dispatchEvent(new KeyboardEvent('keydown', {key: '/', bubbles: true, cancelable: true})); 1"
    with _serve_plain(src) as url:
        out = _drive(tmp_path, f"{url}/", [
            {"until": f"{box} && 1"},
            {"eval": TALL},
            {"eval": f"{box}.closest('.panel').dispatchEvent(new MouseEvent('mouseover', {{bubbles: true}})); 1"},
            # on screen: the page stays where it is
            {"eval": f"(() => {{ {box}.scrollIntoView({{block: 'center'}}); return window.scrollY; }})()"},
            {"eval": slash},
            {"eval": f"[window.scrollY, document.activeElement === {box}]"},
            # scrolled away by the keyboard: `/` brings it back into view
            {"eval": f"(() => {{ {box}.blur(); window.scrollTo(0, 2500); return {on_screen}; }})()"},
            {"eval": slash},
            {"eval": f"[{on_screen}, document.activeElement === {box}]"},
        ])
    v = out["values"]
    assert v[5][1] is True and abs(v[5][0] - v[3]) <= 2, f"focusing an on-screen box moved the page: {v}"
    assert v[6] is False, f"the box must start off screen: {v}"
    assert v[8] == [True, True], f"an off-screen box was focused where the reader cannot see it: {v}"

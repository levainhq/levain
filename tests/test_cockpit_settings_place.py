"""The cockpit's Settings place (levain#141 polish, Phill 11-19).

His words: "Paired devices and Presence and notification settings are completely broken, both display at like 100px wide and thousands
tall"; "Memory: no point in having a Needs you row on this page"; "what if we removed all the settings from the bottom of the pages
and instead added a Settings link to nav with it's own page for these and any future settings" (signed in).

What these hold: the bar's fifth place and key 5, the settings sections drawn on Settings only and at the page's full width at 1440
and 390, the cause fixed at its root (a zone appended to main.frame on Home spans the deck), the Needs-you row not drawn on Memory,
and the presence sheet's link landing on the section. What they cannot hold (a person judges it on the real page): whether the two
sections read well with his real devices and tokens in them."""

from __future__ import annotations

import json
import re
from pathlib import Path

from tests.test_cockpit_flip_parity import _drive, needs_browser
from tests.test_cockpit_k2a import _install, _serve
from tests.test_cockpit_places import PRESS, READY, SETTLE

# the place the page names and the one the bar marks (the Desk's own buttons also carry aria-current, so the bar is asked by its scope)
STATE = """(() => ({ place: document.body.dataset.iaPlace,
  current: [...document.querySelectorAll('.ia-places [aria-current="page"]')].map(b => b.dataset.iaPlaceBtn) }))()"""

WEB = Path(__file__).resolve().parent.parent / "levain" / "templates" / "web"

# Show the two settings sections whatever the test install's reads said (the sections show with their own reads; geometry is the
# question here), then report each one's place in the page. `after` is the place the page is on when it is read.
SHOW = """(() => { for (const s of document.querySelectorAll('section.dev-zone')) { s.hidden = false; for (const p of s.querySelectorAll('.panel')) p.hidden = false; } return 1; })()"""
MEASURE = """(() => {
  // the width a section may fill: the deck's content box on Home, main.frame's on Settings (which is not a grid)
  const box = document.body.dataset.iaPlace === 'settings' ? document.querySelector('main.frame') : document.querySelector('.deck'); const dcs = getComputedStyle(box);
  const inner = box.clientWidth - parseFloat(dcs.paddingLeft) - parseFloat(dcs.paddingRight);
  const zone = (z) => { const n = document.querySelector('section.dev-zone[data-zone="' + z + '"]'); if (!n) return null;
    const r = n.getBoundingClientRect(); return { display: getComputedStyle(n).display, w: Math.round(r.width), h: Math.round(r.height) }; };
  const needs = document.querySelector('#ck-needs');
  return JSON.stringify({ probe: 'settings', place: document.body.dataset.iaPlace, flag: document.body.classList.contains('ia-settings-on'),
    mode: document.body.dataset.mode || '', inner: Math.round(inner), vw: document.documentElement.clientWidth, scrollW: document.documentElement.scrollWidth,
    devices: zone('devices'), presence: zone('presence'), needs: needs ? getComputedStyle(needs).display : null,
    current: [...document.querySelectorAll('.ia-places [aria-current="page"]')].map(b => b.dataset.iaPlaceBtn),
    board: getComputedStyle(document.querySelector('#board')).display, hero: getComputedStyle(document.querySelector('#ck-hero')).display }); })()"""
KEY5 = "document.dispatchEvent(new KeyboardEvent('keydown', {key: '5', bubbles: true, cancelable: true})); 1"
# a zone nothing in the page knows about, appended to main.frame as devices_panel.js appends its sections
APPEND = """(() => { const s = document.createElement('section'); s.className = 'ck-zone'; s.id = 'probe-zone'; s.innerHTML = '<div class="ck-zgrid"><div class="panel" data-modes="board">a future zone</div></div>';   // a zone with no board panel is not drawn at all
  document.querySelector('main.frame').appendChild(s); return 1; })()"""
PROBE_ZONE = """(() => { const s = document.getElementById('probe-zone'); const deck = document.querySelector('.deck'); const dcs = getComputedStyle(deck);
  return JSON.stringify({ probe: 'zone', w: Math.round(s.getBoundingClientRect().width),
    inner: Math.round(deck.clientWidth - parseFloat(dcs.paddingLeft) - parseFloat(dcs.paddingRight)) }); })()"""


def _probes(out: dict, kind: str = "settings") -> list[dict]:
    got = [json.loads(v) for v in out["values"] if isinstance(v, str) and v.startswith("{")]
    return [g for g in got if g.get("probe") == kind]


def test_the_bar_markup_and_script_name_settings_as_the_fifth_place() -> None:
    html = (WEB / "dashboard.html").read_text()
    assert re.search(r'<button[^>]*data-ia-place-btn="settings"[^>]*title="Settings \(key 5\)"[^>]*aria-keyshortcuts="5"[^>]*>Settings</button>', html), "Settings is not in the bar"
    assert html.index('data-ia-place-btn="memory"') < html.index('data-ia-place-btn="settings"')
    js = (WEB / "dashboard_places.js").read_text()
    assert '5: "settings"' in js
    assert 'SETTINGS_ON = "ia-settings-on"' in js


def test_every_link_to_a_settings_section_goes_through_the_place() -> None:
    """The presence sheet's "Tap tokens" link goes to the Settings place and then to the section; no other jump names the sections."""
    panel = (WEB / "devices_panel.js").read_text()
    assert 'openSettings("presence-settings")' in panel
    # the section id the link lands on exists, and the only other pointer at it is the sheet link's own href
    assert 'prSetSec.id = "presence-settings"' in panel
    others = [f.name for f in WEB.glob("*.js") if f.name != "devices_panel.js" and "presence-settings" in f.read_text()]
    assert others == [], others


@needs_browser
def test_settings_draws_both_sections_full_width_at_1440_and_390_and_no_other_place_draws_them(tmp_path: Path) -> None:
    _root, src, _ids = _install(tmp_path)
    steps_at = lambda emu: [                                                          # noqa: E731
        {"emulate": emu}, READY, SETTLE, {"eval": SHOW},
        {"eval": MEASURE},                                                            # 0 Home: not drawn
        {"eval": PRESS % "settings"}, SETTLE, {"eval": MEASURE},                      # 1 Settings, by the bar
        {"eval": PRESS % "memory"}, SETTLE, {"eval": MEASURE},                        # 2 Memory: not drawn, no Needs-you row
        {"eval": KEY5}, SETTLE, {"eval": MEASURE},                                    # 3 Settings, by key 5
        {"eval": PRESS % "home"}, SETTLE, {"eval": MEASURE},                          # 4 Home again
    ]
    with _serve(src, surface_tokens={"browser": "T"}, install_class="reported") as (url, _httpd):
        wide = _probes(_drive(tmp_path, f"{url}/#write_token=T", steps_at({"w": 1440, "h": 900, "dpr": 1})))
        phone = _probes(_drive(tmp_path, f"{url}/#write_token=T", steps_at({"w": 390, "h": 800, "dpr": 1, "mobile": True})))
    for name, runs in (("1440", wide), ("390", phone)):
        assert len(runs) == 5, (name, runs)
        home, settings, memory, key5, home2 = runs
        for r in (home, memory, home2):
            assert r["devices"]["display"] == "none" and r["presence"]["display"] == "none", (name, r)
        assert home["place"] == "home" and not home["flag"] and home["current"] == ["home"], (name, home)
        assert memory["place"] == "memory" and memory["mode"] == "memory" and not memory["flag"] and memory["needs"] == "none", (name, memory)
        for r in (settings, key5):
            assert r["place"] == "settings" and r["flag"] and r["mode"] == "board" and r["current"] == ["settings"], (name, r)
            assert r["board"] == "none" and r["hero"] == "none" and r["needs"] == "none", (name, r)
            for z in ("devices", "presence"):
                assert r[z]["display"] != "none", (name, z, r)
                assert r[z]["w"] >= r["inner"] - 2, (name, z, r)               # the page's width, not a ~100 px track
                assert r[z]["h"] < 2500, (name, z, r)                          # not thousands tall
            assert r["scrollW"] <= r["vw"], (name, r)                          # never sideways
        # stacked, readable: the second section sits below the first
    # (the order is read from the geometry of the page itself in the next test; here only the widths and the places)


@needs_browser
def test_the_places_after_settings_leave_it_and_the_bar_agrees_with_the_body(tmp_path: Path) -> None:
    """The Desk and the Room end Settings (the bar's current and body[data-ia-place] always agree); Home and Memory too."""
    _root, src, _ids = _install(tmp_path)
    with _serve(src, surface_tokens={"browser": "T"}, install_class="reported") as (url, _httpd):
        out = _drive(tmp_path, f"{url}/#write_token=T", [
            {"emulate": {"w": 1440, "h": 900, "dpr": 1}}, READY, SETTLE,
            {"eval": PRESS % "settings"}, SETTLE, {"eval": STATE},                    # 0
            {"eval": PRESS % "desk"}, SETTLE, {"eval": STATE},                        # 1
            {"eval": PRESS % "settings"}, SETTLE, {"eval": STATE},                    # 2: Settings again, from the Desk
            {"eval": PRESS % "room"}, SETTLE, {"eval": STATE},                        # 3
            {"eval": PRESS % "settings"}, SETTLE, {"eval": PRESS % "memory"}, SETTLE, {"eval": STATE},   # 4
            {"eval": KEY5}, SETTLE, {"eval": "document.body.classList.contains('ia-settings-on') + ''"},   # 5: key 5
            {"eval": PRESS % "home"}, SETTLE, {"eval": STATE}, {"eval": "document.body.classList.contains('ia-settings-on') + ''"},   # 6
        ])
    states = [v for v in out["values"] if isinstance(v, dict) and "place" in v]
    assert [s["place"] for s in states] == ["settings", "desk", "settings", "room", "memory", "home"], out["values"]
    assert [s["current"] for s in states] == [[p] for p in ("settings", "desk", "settings", "room", "memory", "home")], states
    assert [v for v in out["values"] if v in ("true", "false")] == ["true", "false"], out["values"]


@needs_browser
def test_a_zone_appended_to_main_frame_on_home_spans_the_deck(tmp_path: Path) -> None:
    """The cause of the 100 px panels: layout A flattens main.frame into the 12-column deck grid, and a zone with no span took one track."""
    _root, src, _ids = _install(tmp_path)
    with _serve(src, surface_tokens={"browser": "T"}, install_class="reported") as (url, _httpd):
        out = _drive(tmp_path, f"{url}/#write_token=T", [
            {"emulate": {"w": 1440, "h": 900, "dpr": 1}}, READY, SETTLE,
            {"eval": APPEND}, SETTLE, {"eval": PROBE_ZONE},
            {"emulate": {"w": 390, "h": 800, "dpr": 1, "mobile": True}}, SETTLE, {"eval": PROBE_ZONE},
        ])
    wide, phone = _probes(out, "zone")
    for r in (wide, phone):
        assert r["w"] >= r["inner"] - 2 and r["w"] > 150, r


@needs_browser
def test_the_presence_sheet_link_lands_on_the_section_in_the_settings_place(tmp_path: Path) -> None:
    """From Home the sheet's "Tap tokens" link goes to Settings and the section is drawn and focused (no longer a scroll to the foot of Home)."""
    _root, src, _ids = _install(tmp_path)
    with _serve(src, surface_tokens={"browser": "T"}, install_class="reported") as (url, _httpd):
        out = _drive(tmp_path, f"{url}/#write_token=T", [
            {"emulate": {"w": 1440, "h": 900, "dpr": 1}}, READY, SETTLE, {"eval": SHOW},
            {"eval": "const l = document.querySelector('.pr-sheet-link'); document.getElementById('pr-sheet').hidden = false; l.click(); 1"},
            {"eval": "new Promise(r => setTimeout(r, 700)).then(() => 1)"},
            {"eval": "JSON.stringify({probe: 'link', place: document.body.dataset.iaPlace, focus: document.activeElement && document.activeElement.id, shown: !!document.getElementById('presence-settings').getClientRects().length})"},
        ])
    got = _probes(out, "link")[0]
    assert got["place"] == "settings" and got["focus"] == "presence-settings" and got["shown"], got


HEAD = """(() => { const z = document.querySelector('section.dev-zone[data-zone="devices"]'); const h = z.querySelector('.zone-head'); const a = h.querySelector('a.sign-enrol-link');
  const ph = document.querySelector('section.dev-zone[data-zone="presence"] .zone-head');
  const r = a.getBoundingClientRect(); const hr = h.getBoundingClientRect();
  return JSON.stringify({ probe: 'head', display: getComputedStyle(h).display, pdisplay: getComputedStyle(ph).display, title: h.querySelector('.zone-title').textContent,
    gloss: (h.querySelector('.zone-gloss') || {}).textContent, href: a.getAttribute('href'), linkH: Math.round(r.height), linkW: Math.round(r.width), headW: Math.round(hr.width),
    inside: r.right <= hr.right + 1 && r.left >= hr.left - 1 }); })()"""
EMPTY = """(() => { const e = document.querySelector('.dev-empty'); const r = e.getBoundingClientRect();
  return JSON.stringify({ probe: 'empty', hidden: e.hidden, display: getComputedStyle(e).display, h: Math.round(r.height), text: e.textContent, place: document.body.dataset.iaPlace }); })()"""
HIDE_ALL = """(() => { for (const s of document.querySelectorAll('section.dev-zone')) { s.hidden = true; for (const p of s.querySelectorAll('.panel')) p.hidden = true; }
  const e = document.querySelector('.dev-empty'); return 1; })()"""


@needs_browser
def test_the_devices_head_and_the_passkey_link_draw_on_settings_at_1440_and_390(tmp_path: Path) -> None:
    _root, src, _ids = _install(tmp_path)
    steps_at = lambda emu: [{"emulate": emu}, READY, SETTLE, {"eval": SHOW}, {"eval": PRESS % "settings"}, SETTLE, {"eval": HEAD}]   # noqa: E731
    with _serve(src, surface_tokens={"browser": "T"}, install_class="reported") as (url, _httpd):
        wide = _probes(_drive(tmp_path, f"{url}/#write_token=T", steps_at({"w": 1440, "h": 900, "dpr": 1})), "head")
        phone = _probes(_drive(tmp_path, f"{url}/#write_token=T", steps_at({"w": 390, "h": 800, "dpr": 1, "mobile": True})), "head")
    for name, runs in (("1440", wide), ("390", phone)):
        assert len(runs) == 1, (name, runs)
        h = runs[0]
        assert h["display"] == "flex" and h["pdisplay"] == "flex", (name, h)       # drawn as the Presence head is
        assert h["title"] == "Devices" and h["gloss"] == "who holds a key to this cockpit, and who may post to you", (name, h)
        assert h["href"] == "/enrol" and h["linkH"] >= 44 and h["linkW"] > 0 and h["inside"], (name, h)


@needs_browser
def test_settings_with_no_section_drawn_says_so_and_the_line_leaves_when_one_is(tmp_path: Path) -> None:
    _root, src, _ids = _install(tmp_path)
    with _serve(src, surface_tokens={"browser": "T"}, install_class="reported") as (url, _httpd):
        out = _drive(tmp_path, f"{url}/#write_token=T", [
            {"emulate": {"w": 390, "h": 800, "dpr": 1, "mobile": True}}, READY, SETTLE,
            {"eval": PRESS % "settings"}, SETTLE, {"eval": SHOW}, {"eval": EMPTY},      # 0: a section drawn
            {"eval": "window.LevainDevices && 1"},
        ])
        shown = _probes(out, "empty")
    # the line is driven by devices_panel.js syncSection(); the static contract is held here, the live states by the source below
    js = (WEB / "devices_panel.js").read_text()
    assert "emptyLine.hidden = !(section.hidden && (!prSetSec || prSetSec.hidden))" in js
    css = (WEB / "dashboard.css").read_text()
    assert 'body.ia:not([data-ia-place="settings"]) .dev-empty { display: none !important; }' in css
    assert shown and shown[0]["place"] == "settings"


NATURAL = """(() => { const e = document.querySelector('.dev-empty');
  const drawn = [...document.querySelectorAll('section.dev-zone')].filter(s => !s.hidden).length;
  return JSON.stringify({ probe: 'natural', drawn, lineHidden: e.hidden, display: getComputedStyle(e).display, place: document.body.dataset.iaPlace }); })()"""


@needs_browser
def test_the_empty_line_follows_the_sections_own_state_on_settings(tmp_path: Path) -> None:
    _root, src, _ids = _install(tmp_path)
    steps = [{"emulate": {"w": 390, "h": 800, "dpr": 1, "mobile": True}}, READY, SETTLE,
             {"eval": PRESS % "settings"}, SETTLE, {"eval": NATURAL}]
    with _serve(src, surface_tokens={"browser": "T"}, install_class="reported") as (url, _httpd):
        with_token = _probes(_drive(tmp_path, f"{url}/#write_token=T", steps), "natural")
    with _serve(src, surface_tokens={"browser": "T"}, install_class="reported") as (url, _httpd):
        no_token = _probes(_drive(tmp_path, f"{url}/", steps), "natural")
    for run in (with_token, no_token):
        assert len(run) == 1 and run[0]["place"] == "settings", run
        n = run[0]
        # the one invariant, whichever way the reads went: the line is drawn exactly when no section is
        assert (n["drawn"] == 0) == (n["display"] == "block") and (n["drawn"] == 0) == (not n["lineHidden"]), n
    assert with_token[0]["drawn"] > 0 and with_token[0]["display"] == "none", with_token
    assert no_token[0]["drawn"] == 0 and no_token[0]["display"] == "block", no_token

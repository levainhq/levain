"""levain#141 polish, the review fix round (fix4, 1119-10): what the two reviewers found, held where a test can hold it.

Held: the rename control reachable on Settings when the masthead hides the entity name, focus and jumps landing below whatever is stuck
to the top (scroll-padding, at 1280 and at 390), no pinning on a short screen (one source for the two thresholds, JS and CSS), the
status-bar inset kept where the masthead is gone, 44 px masthead targets at every width on touch, the graphs toggle's semantics, no
seam between the two pinned bars, the Settings empty line never shown before the settings reads answer, the comments that stated more
than the page does, and the dead masthead jar rules.

NOT held (a person judges them): whether the "This cockpit" section reads well beside the others, how the status-bar inset looks in the
standalone window on a phone with the keyboard up (a device check), and the Machine controls' Tab position (a known edge)."""

from __future__ import annotations

import json
import re
from pathlib import Path

from tests._ke_dom import WEB
from tests.test_cockpit_flip_parity import _drive, needs_browser
from tests.test_cockpit_k2a import _install, _serve
from tests.test_cockpit_narrow_layout_rig import drive_touch
from tests.test_cockpit_places import PANELS, PRESS, READY, SETTLE

CSS = (WEB / "dashboard.css").read_text(encoding="utf-8")
HTML = (WEB / "dashboard.html").read_text(encoding="utf-8")
PLACES_JS = (WEB / "dashboard_places.js").read_text(encoding="utf-8")
CORE = (WEB / "dashboard_core.js").read_text(encoding="utf-8")


def _vals(out: dict, probe: str) -> list[dict]:
    got = [json.loads(v) for v in out["values"] if isinstance(v, str) and v.startswith("{")]
    return [g for g in got if g.get("probe") == probe]


def _open(tmp_path: Path, steps: list, url_tail: str = "/#write_token=T", touch: bool = False) -> dict:
    _root, src, _ids = _install(tmp_path)
    with _serve(src, surface_tokens={"browser": "T"}, install_class="reported") as (url, _httpd):
        return (drive_touch if touch else _drive)(tmp_path, url + url_tail, steps)


def _wait(ms: int) -> dict:
    return {"eval": f"new Promise(r => setTimeout(r, {ms})).then(() => 1)"}


# ------------------------------------------------------------------------------------------------------------ 1: rename on Settings

RENAME = r"""(() => { const sec = document.getElementById('ck-thiscockpit'); const unit = document.querySelector('.masthead .unit');
  const btn = sec && sec.querySelector('.name-edit'); const box = (n) => { if (!n) return null; const r = n.getBoundingClientRect(); return { shown: n.getClientRects().length > 0, w: Math.round(r.width), h: Math.round(r.height) }; };
  const first = document.querySelector('main.frame > :not([hidden])');
  return JSON.stringify({ probe: 'rename', place: document.body.dataset.iaPlace, sec: box(sec), btn: box(btn), unitDisplay: getComputedStyle(unit).display, dupe: document.querySelector('.masthead').hasAttribute('data-entity-dupe'),
    firstId: first && first.id, title: sec && sec.querySelector('.zone-title').textContent, name: sec && sec.querySelector('#ck-entity').textContent,
    mastBtn: !!document.querySelector('.masthead .name-edit') }); })()"""
EDITOR = r"""(() => { document.querySelector('#ck-thiscockpit .name-edit').click(); const sec = document.getElementById('ck-thiscockpit');
  const ed = sec.querySelector('.name-editor'); const inp = ed && ed.querySelector('.name-input'); const r = inp && inp.getBoundingClientRect();
  return JSON.stringify({ probe: 'editor', editor: !!ed, input: !!inp, shown: !!(r && r.width > 0), focus: document.activeElement === inp, value: inp && inp.value, entityHidden: sec.querySelector('#ck-entity').style.display }); })()"""


# Today's kernel offers entity_name to no panel (cockpit/verbs.py: unbuilt, a laptop-presence signature), so the real render draws no rename
# anywhere. The page's own render runs again here with the verb offered and a write transport, so wireEntityName, the entity paint and the
# section's own sync are the page's, not a copy.
OFFER_RENAME = r"""(() => { const orig = window.LevainDashboard.render;
  window.LevainDashboard.render = (v, o) => orig(Object.assign({}, v, { offered_verbs: Object.assign({}, v && v.offered_verbs, { entity_name: true }) }),
    Object.assign({}, o, { commit: (o && o.commit) || (async () => ({ ok: true })) }));
  document.getElementById('refresh').click(); return 1; })()"""


@needs_browser
def test_the_rename_control_lives_on_settings_when_the_masthead_hides_the_name(tmp_path: Path) -> None:
    """The entity name equals the wordmark: the masthead's .unit (and the rename inside it) is not drawn. Settings has a first section,
    "This cockpit", with the name and the same rename control; clicking it opens the same editor (the commit gate is wireEntityName's)."""
    out = _open(tmp_path, [{"emulate": {"w": 1440, "h": 900, "dpr": 1}}, READY, SETTLE, {"eval": OFFER_RENAME}, _wait(1500),
                           {"eval": "document.querySelector('.masthead').setAttribute('data-entity-dupe', ''); 1"},
                           {"eval": PRESS % "settings"}, SETTLE, {"eval": RENAME}, {"eval": EDITOR}])
    (got,) = _vals(out, "rename")
    (ed,) = _vals(out, "editor")
    assert got["place"] == "settings" and got["dupe"] is True and got["unitDisplay"] == "none", got      # the masthead keeps its rule
    assert got["sec"]["shown"] and got["btn"] and got["btn"]["shown"] and got["btn"]["h"] > 0 and got["btn"]["w"] > 0, got
    assert got["firstId"] == "ck-thiscockpit" and got["title"] == "This cockpit" and got["name"].strip() not in ("", "—"), got   # first on the page, with the name
    assert ed["editor"] and ed["shown"] and ed["focus"] and ed["entityHidden"] == "none", ed


def test_the_section_is_in_the_page_first_and_only_drawn_on_settings() -> None:
    assert re.search(r'<main class="frame">\s*(<!--.*?-->\s*)?<section class="ck-thiscockpit" id="ck-thiscockpit"', HTML, re.S)
    assert 'id="ck-entity"' in HTML and "This cockpit" in HTML
    assert re.search(r'body\.ia:not\(\[data-ia-place="settings"\]\) :is\(\.dev-zone, \.ck-thiscockpit\) \{ display: none !important; \}', CSS)   # every settings section, this one included
    assert "ck-thiscockpit" in CSS.split("main.frame > :not(.dev-zone)", 1)[1].split("{", 1)[0]                                       # and Settings keeps it when it hides the rest
    assert re.search(r"body\.ia \.masthead\[data-entity-dupe\] \.unit \{ display: none; \}", CSS)                     # the masthead rule is unchanged


# ------------------------------------------------------------------------------------------------- 2, 3, 7: pinned bars, focus, jumps

FOCUS_UNDER = r"""(() => { const bars = document.querySelector('.ia-places').getBoundingClientRect().bottom;
  const b = [...document.querySelectorAll('#board .panel button, #board .panel a[href], #board .panel input')].find(x => { const r = x.getBoundingClientRect(); return x.getClientRects().length && r.top + scrollY > 900; });
  if (!b) return JSON.stringify({ probe: 'focus', none: true });
  window.scrollTo(0, b.getBoundingClientRect().top + scrollY - 40);   /* the control sits at top 40, under the bars */
  return new Promise(r => setTimeout(r, 300)).then(() => { const before = Math.round(b.getBoundingClientRect().top); b.focus();
    return new Promise(r => setTimeout(r, 500)).then(() => JSON.stringify({ probe: 'focus', before, top: Math.round(b.getBoundingClientRect().top), bars: Math.round(bars), pos: getComputedStyle(document.querySelector('.masthead')).position })); }); })()"""
SETTINGS_JUMP = r"""(() => { for (const s of document.querySelectorAll('section.dev-zone')) { s.hidden = false; for (const p of s.querySelectorAll('.panel')) p.hidden = false; }
  const sp = document.createElement('div'); sp.className = 'dev-zone'; sp.style.height = '2600px'; document.querySelector('main.frame').appendChild(sp);
  window.scrollTo(0, 0); window.LevainPlaces.openSettings('presence-settings');
  return new Promise(r => setTimeout(r, 900)).then(() => { const n = document.getElementById('presence-settings').getBoundingClientRect(); const bar = document.querySelector('.ia-places').getBoundingClientRect();
    return JSON.stringify({ probe: 'jump', top: Math.round(n.top), barBottom: Math.round(bar.bottom), scrolled: Math.round(scrollY) }); }); })()"""


@needs_browser
def test_focus_lands_below_the_pinned_bars_at_1280(tmp_path: Path) -> None:
    out = _open(tmp_path, [{"emulate": {"w": 1280, "h": 800, "dpr": 1}}, READY, PANELS, SETTLE, {"eval": FOCUS_UNDER}])
    (got,) = _vals(out, "focus")
    assert not got.get("none"), got
    assert got["pos"] in ("fixed", "sticky"), got
    assert got["top"] >= got["bars"] - 1, got                     # the focused control is below the bars' bottom, not under them


@needs_browser
def test_a_settings_jump_lands_below_the_places_strip_at_390(tmp_path: Path) -> None:
    out = _open(tmp_path, [{"emulate": {"w": 390, "h": 800, "dpr": 1, "mobile": True}}, READY, SETTLE, {"eval": PRESS % "settings"}, SETTLE, {"eval": SETTINGS_JUMP}])
    (got,) = _vals(out, "jump")
    assert got["scrolled"] > 100, got                              # the page did scroll, so the section was brought to the top
    assert got["top"] >= got["barBottom"] - 1, got


def test_the_scroll_padding_is_the_bars_height_and_the_margin_is_only_breathing_room() -> None:
    assert re.search(r"html \{ scroll-padding-top: var\(--ia-bars-h, 0px\); \}", CSS)
    m = re.search(r"body\.ia :is\(([^)]*)\) \{ scroll-margin-top: ([^;]*); \}", CSS)
    assert m and ".dev-zone" in m.group(1) and ".ck-thiscockpit" in m.group(1) and "--ia-bars-h" not in m.group(2), m and m.groups()   # not counted twice


def test_nothing_says_that_a_phone_pins_nothing() -> None:
    assert "nothing is pinned" not in CSS and "nothing is pinned" not in PLACES_JS


PIN_PROBE = r"""(() => { window.scrollTo(0, 700); return new Promise(r => setTimeout(r, 400)).then(() => { const m = document.querySelector('.masthead'); const r = m.getBoundingClientRect();
  return JSON.stringify({ probe: 'pin', w: innerWidth, h: innerHeight, place: document.body.dataset.iaPlace, pos: getComputedStyle(m).position, top: Math.round(r.top), mastH: getComputedStyle(document.documentElement).getPropertyValue('--ia-mast-h').trim() }); }); })()"""


@needs_browser
def test_a_short_screen_pins_nothing_and_a_tall_one_pins(tmp_path: Path) -> None:
    """932 x 430 (a landscape phone) is wider than 720 and shorter than 600: the masthead scrolls away on Home and on Settings; at 1280 x 800 it is pinned."""
    steps: list = []
    for emu in ({"w": 932, "h": 430, "dpr": 1, "mobile": True}, {"w": 1280, "h": 800, "dpr": 1}):
        steps += [{"emulate": emu}, SETTLE, {"eval": PRESS % "home"}, SETTLE, {"eval": PIN_PROBE}, {"eval": PRESS % "settings"}, SETTLE, {"eval": PIN_PROBE}]
    out = _open(tmp_path, [{"emulate": {"w": 1280, "h": 800, "dpr": 1}}, READY, PANELS, SETTLE] + steps)
    rows = _vals(out, "pin")
    assert len(rows) == 4, out["values"]
    for r in rows[:2]:
        assert r["w"] > 720 and r["h"] < 600, r
        assert r["pos"] == "static" and r["top"] < 0 and r["mastH"] == "0px", r
    for r in rows[2:]:
        assert r["pos"] in ("fixed", "sticky") and r["top"] == 0 and r["mastH"] != "0px", r


def test_the_pin_thresholds_have_one_source_and_the_css_mirrors_it() -> None:
    w = int(re.search(r"var PIN_MIN_W = (\d+), PIN_MIN_H = (\d+);", PLACES_JS).group(1))
    h = int(re.search(r"var PIN_MIN_W = (\d+), PIN_MIN_H = (\d+);", PLACES_JS).group(2))
    assert (w, h) == (721, 600)
    pinned = re.findall(r"@media \(min-width: (\d+)px\) and \(min-height: (\d+)px\) \{\s*body\.ia \.deck > \.masthead \{ position: sticky;", CSS)
    assert pinned == [(str(w), str(h))], pinned                                       # the CSS pin block names the same two numbers
    assert not re.search(r"@media \(min-width: 721px\) \{\s*body\.ia \.deck > \.masthead \{ position: sticky;", CSS)   # and no pin left without the height


SEAM = r"""(() => { const m = document.querySelector('.masthead'); m.style.setProperty('padding-bottom', '3.4px', 'important'); m.style.setProperty('box-sizing', 'content-box', 'important');   /* a fractional height */
  return new Promise(r => setTimeout(r, 500)).then(() => { const mb = m.getBoundingClientRect(); const b = document.querySelector('.ia-places').getBoundingClientRect();
    return JSON.stringify({ probe: 'seam', mastBottom: mb.bottom, barTop: b.top, frac: mb.height % 1, h: mb.height, pb: getComputedStyle(m).paddingBottom }); }); })()"""


@needs_browser
def test_the_places_bar_starts_exactly_where_the_masthead_ends(tmp_path: Path) -> None:
    out = _open(tmp_path, [{"emulate": {"w": 1280, "h": 900, "dpr": 1}}, READY, PANELS, SETTLE, {"eval": PRESS % "home"}, SETTLE, {"eval": SEAM}])
    (got,) = _vals(out, "seam")
    assert got["frac"] > 0.1, got                                    # the probe made the height fractional (else it proves nothing)
    assert abs(got["barTop"] - got["mastBottom"]) < 0.05, got       # no ceil gap where the page shows through


# ------------------------------------------------------------------------------------------------------------------ 4: the inset

def test_the_deck_keeps_the_status_bar_inset_where_the_masthead_is_gone() -> None:
    """A phone with the keyboard up in the Room or the Desk hides the masthead (which carries the inset); the deck takes it over there."""
    assert re.search(r"@media \(max-width: 720px\) \{\s*body\.rv-on\.rv-kb \.deck, body\.dv-on\.dv-kb \.deck \{ padding-top: env\(safe-area-inset-top, 0px\); \}", CSS)
    assert re.search(r"body\.rv-on\.rv-kb \.deck > \.masthead, body\.rv-on\.rv-kb \.deck > \.ck-phonetabs \{ display: none; \}", CSS)   # the rule it answers
    assert re.search(r"body\.ia \.deck > \.masthead \{[^}]*padding-top: calc\(var\(--sp-5\) \+ env\(safe-area-inset-top", CSS)           # and the masthead still carries it


# ------------------------------------------------------------------------------------------------------------------ 5: touch targets

COARSE = r"""(() => { const hit = [...document.querySelectorAll('.masthead .indicators button, .masthead .themesw button, .masthead .ia-act, .ck-mctl button')]
  .filter((b) => b.getClientRects().length > 0).map((b) => { const r = b.getBoundingClientRect(); return [b.id || b.getAttribute('aria-label') || b.className, Math.round(r.width), Math.round(r.height)]; });
  return JSON.stringify({ probe: 'coarse', coarse: matchMedia('(pointer: coarse)').matches, w: innerWidth, small: hit.filter((h) => h[1] < 44 || h[2] < 44), n: hit.length, machine: hit.filter((h) => /graph|calm|sky|Ambient/i.test(h[0])).length }); })()"""


@needs_browser
def test_every_masthead_control_and_the_machine_row_are_44_px_on_touch_at_every_width(tmp_path: Path) -> None:
    steps: list = []
    for emu in ({"w": 390, "h": 844, "dpr": 2, "mobile": True}, {"w": 768, "h": 1024, "dpr": 2, "mobile": True}, {"w": 932, "h": 430, "dpr": 2, "mobile": True}):
        steps += [{"emulate": emu}, SETTLE, {"eval": COARSE}]
    out = _open(tmp_path, [{"emulate": {"w": 390, "h": 844, "dpr": 2, "mobile": True}}, {"touch": True}, READY, SETTLE] + steps, touch=True)
    rows = _vals(out, "coarse")
    assert len(rows) == 3, out["values"]
    for g in rows:
        assert g["coarse"] is True and g["n"] >= 10 and g["machine"] >= 2, g
        assert g["small"] == [], g


# --------------------------------------------------------------------------------------------------------- 6: the graphs toggle

def test_the_graphs_toggle_names_its_state_in_its_text_and_controls_the_graph() -> None:
    """The visible text flips ("Show the graphs" / "Hide the graphs"); aria-expanded is gone (the name already says the state) and
    aria-controls names the card it opens (the lens bar and the graph, L3 r1 codex)."""
    assert 'b.setAttribute("aria-controls", "ck-skycard")' in PLACES_JS
    assert "aria-expanded" not in PLACES_JS
    assert 'g.textContent = open ? "Hide the graphs" : "Show the graphs"' in PLACES_JS
    assert 'id="ck-skycard"' in HTML


# --------------------------------------------------------------------------------------------------------- 8: the empty line

LINE = r"""(() => { const e = document.querySelector('.dev-empty'); const drawn = [...document.querySelectorAll('section.dev-zone')].filter(s => !s.hidden && s.id !== '').length;
  return JSON.stringify({ probe: 'line', place: document.body.dataset.iaPlace, hidden: e.hidden, display: getComputedStyle(e).display, drawn }); })()"""
DELAY = ("const f = window.fetch.bind(window); window.fetch = (u, o) => (/voice\\.json|pair\\/devices|webpush\\/presence/.test(String(u))"
         " ? new Promise((r) => setTimeout(r, 2200)).then(() => f(u, o)) : f(u, o));")


@needs_browser
def test_the_empty_line_is_not_shown_before_the_settings_reads_answer(tmp_path: Path) -> None:
    """With no token no section will ever be drawn, so the line is right to show; but not while the reads are still out (it flashed)."""
    steps = [{"emulate": {"w": 390, "h": 800, "dpr": 1, "mobile": True}}, {"touch": True}, {"init": DELAY}, READY, {"eval": PRESS % "settings"}, _wait(500), {"eval": LINE},
             _wait(3500), {"eval": LINE}]
    out = _open(tmp_path, steps, url_tail="/", touch=True)
    early, late = _vals(out, "line")
    assert early["place"] == "settings" and early["display"] == "none", early      # the reads have not answered
    assert late["display"] == "block" and late["hidden"] is False, late            # they have, and nothing is drawn


def test_every_settings_read_marks_itself_settled_whatever_it_answered() -> None:
    js = (WEB / "devices_panel.js").read_text(encoding="utf-8")
    for inner, which in (("readOnceInner", "dev"), ("readPresenceInner", "pr")):
        assert f'try {{ return await {inner}(); }} finally {{ settle("{which}"); }}' in js or f'try {{ await {inner}(); }} finally {{ settle("{which}"); }}' in js, inner
    # the voice probe settles only when it ran its read (L3 r1: a probe skipped because another is out has learned nothing)
    assert 'try { ran = await voiceProbeInner(); } finally { if (ran || vcDone || vcTries >= VC_MAX_TRIES) settle("vc"); }' in js
    assert "if (vcDone || vcBusy || vcTries >= VC_MAX_TRIES) return false;" in js


# --------------------------------------------------------------------------------------------------------- 9, 10: comments and dead CSS

def test_the_comments_say_what_the_page_does() -> None:
    order = CSS.split("/* HOME ORDER:", 1)[1].split("*/", 1)[0]
    assert "so Tab walks the drawn order through them" in order and "Three exceptions" in order and "#ck-skycol" in order
    zone = CSS.split("The cause of the 100 px panels", 1)[1].split("*/", 1)[0]
    assert "LATER" in zone and "today none is" in zone


def test_the_masthead_jar_and_sky_toggle_rules_that_cannot_match_are_gone() -> None:
    """The jar is drawn only into #ck-jarbox (renderJar returns when the box is absent) and the Ambient toggle only into #ck-mctl, so no
    .jar, .jar-label or .sky-toggle is ever a child of the masthead."""
    assert not re.search(r"body\.ke \.masthead \.jar|body\.ia \.masthead :is\(\.unit, \.jar\)|masthead \.indicators button\.sky-toggle", CSS)
    header = HTML.split("</header>", 1)[0]
    assert "jar" not in header and "sky-toggle" not in header
    assert 'const j = document.getElementById("ck-jarbox");\n    if (!j) return;' in CORE
    assert 'id="ck-mctl"' in HTML and 'toggleHost: document.getElementById("ck-mctl")' in (WEB / "cockpit_view.js").read_text(encoding="utf-8")

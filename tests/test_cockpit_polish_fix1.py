"""Cockpit polish fix 1 (levain#141): a jump from Settings leaves Settings, and the dead Calm rules are gone.

What these hold: a Jump-palette choice or a data-phone-jump button pressed while the Settings place is up ends Settings and lands on
the place that draws the target, with the target drawn; and no stylesheet rule or script names #calmphone (removed from the markup by
the machine lane) or the masthead's #calmbtn (it now lives in the Machine card row). What they cannot hold: how the landing feels."""

from __future__ import annotations

import json
import re
from pathlib import Path

from tests.test_cockpit_flip_parity import _drive, needs_browser
from tests.test_cockpit_k2a import _install, _serve
from tests.test_cockpit_places import PRESS, READY, SETTLE

WEB = Path(__file__).resolve().parent.parent / "levain" / "templates" / "web"

OPEN_JUMP = "document.getElementById('jumpbtn').click(); 1"
# the first row of kind panel that the board draws (the list now opens with the places, whose first row is Home, then the current place's
# own panels: on Settings those are Settings panels, which are not a jump away from Settings, so the first board panel is the one chosen)
CHOOSE_FIRST = """(() => { const board = [...document.querySelectorAll('#board .panel > .phead h2')].map((h) => h.textContent);
  const li = [...document.querySelectorAll('.palette-list li')].find((x) => x.querySelector('.pk').textContent === 'panel' && board.includes(x.querySelector('.pl').textContent));
  if (!li) return 'none'; window.__lbl = li.querySelector('.pl').textContent; li.click(); return 1; })()"""
# the panel the palette's first row named, drawn or not, and the place the page is on
PROBE_PANEL = """(() => { const want = window.__lbl; let drawn = null;
  for (const h of document.querySelectorAll('#board .panel > .phead h2')) if (h.textContent === want) drawn = h.closest('.panel').getClientRects().length > 0;
  return JSON.stringify({ probe: 'jump', want: want, drawn: drawn, place: document.body.dataset.iaPlace,
    flag: document.body.classList.contains('ia-settings-on') }); })()"""
ADD_BTN = """(() => { const b = document.createElement('button'); b.id = 'probe-jump'; b.type = 'button'; b.dataset.phoneJump = 'board';
  document.body.appendChild(b); b.click(); return 1; })()"""
PROBE_TRAY = """(() => { const p = document.querySelector('.panel[data-ck-panel="tray"]');
  return JSON.stringify({ probe: 'tray', drawn: !!p && p.getClientRects().length > 0, place: document.body.dataset.iaPlace,
    flag: document.body.classList.contains('ia-settings-on') }); })()"""


def _probe(out: dict, kind: str) -> dict:
    got = [json.loads(v) for v in out["values"] if isinstance(v, str) and v.startswith("{")]
    return [g for g in got if g.get("probe") == kind][0]


@needs_browser
def test_a_jump_palette_choice_on_settings_leaves_settings_with_the_panel_drawn(tmp_path: Path) -> None:
    _root, src, _ids = _install(tmp_path)
    with _serve(src, surface_tokens={"browser": "T"}, install_class="reported") as (url, _httpd):
        out = _drive(tmp_path, f"{url}/#write_token=T", [
            {"emulate": {"w": 1440, "h": 900, "dpr": 1}}, READY, SETTLE,
            {"eval": PRESS % "settings"}, SETTLE,
            {"eval": OPEN_JUMP}, SETTLE, {"eval": CHOOSE_FIRST}, SETTLE,
            {"eval": PROBE_PANEL},
        ])
    r = _probe(out, "jump")
    assert r["flag"] is False and r["place"] in ("home", "memory"), r
    assert r["drawn"] is True, r


@needs_browser
def test_a_phone_jump_on_settings_lands_on_home_with_the_panel_drawn(tmp_path: Path) -> None:
    _root, src, _ids = _install(tmp_path)
    with _serve(src, surface_tokens={"browser": "T"}, install_class="reported") as (url, _httpd):
        out = _drive(tmp_path, f"{url}/#write_token=T", [
            {"emulate": {"w": 1440, "h": 900, "dpr": 1}}, READY, SETTLE,
            {"eval": PRESS % "settings"}, SETTLE,
            {"eval": ADD_BTN}, SETTLE, {"eval": PROBE_TRAY},
        ])
    r = _probe(out, "tray")
    assert r["flag"] is False and r["place"] == "home", r
    assert r["drawn"] is True, r


def test_no_rule_or_script_names_the_removed_masthead_calm_buttons() -> None:
    css = (WEB / "dashboard.css").read_text()
    assert "calmphone" not in css
    assert not re.search(r"\.masthead[^{}]*#calmbtn", css), "a masthead-scoped #calmbtn rule cannot match: the button is in .ck-mctl"
    for f in WEB.glob("*.js"):
        assert "calmphone" not in f.read_text(), f.name
    assert "calmphone" not in (WEB / "dashboard.html").read_text()

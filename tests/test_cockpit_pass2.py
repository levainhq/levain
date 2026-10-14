"""Cockpit pass 2 (Phill 10-14 ~19:2x): the phone's header leads with the nav and the sky shows only rows with events; an empty card
is a slim bar at the end of its zone instead of a void beside tall neighbours."""

from __future__ import annotations

import datetime as d
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.test_cockpit_flip_parity import _drive, needs_browser
from tests.test_cockpit_k2a import _install, _serve

ROOT = Path(__file__).resolve().parents[1]
SKY = ROOT / "levain" / "templates" / "web" / "sky.js"
NODE = shutil.which("node")


def _compact_rows(events: list[dict]) -> list[str]:
    js = ("const {buildModel}=require(process.argv[1]);const i=JSON.parse(require('fs').readFileSync(0,'utf8'));"
          "const ev=i.events.map(e=>({t:Date.parse(e.at),kind:e.kind,repo:e.repo,agent:null,ok:true}));"
          "const m=buildModel(ev,{windowHours:14,topRows:5,pinnedRows:[],single:'',compact:true},i.now);"
          "console.log(JSON.stringify(m.rows.map(r=>r.label)));")
    out = subprocess.run([NODE, "-e", js, str(SKY)], input=json.dumps({"events": events, "now": 1_000_000_000_000}),
                         capture_output=True, text=True, check=True).stdout
    return json.loads(out)


def _at(kind: str, repo: str | None) -> dict:
    at = d.datetime.fromtimestamp(1_000_000_000 - 300, d.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {"kind": kind, "at": at, "repo": repo}


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_a_phone_sky_drops_the_rows_nothing_landed_on_and_keeps_all_rows_when_nothing_did() -> None:
    assert _compact_rows([_at("commit", "a"), _at("commit", "b")]) == ["a", "b"]           # no memory or decisions row
    assert _compact_rows([_at("commit", "a"), _at("commit", "b"), _at("decision", None), _at("episode", None)]) == ["a", "b", "mem", "dec"]
    assert _compact_rows([]) == ["mem", "dec"]                                              # a bare frame would read as broken


def _page(tmp_path: Path, steps: list[dict]) -> list:
    _root, src, _ids = _install(tmp_path)
    with _serve(src, surface_tokens={"browser": "T"}, install_class="reported") as (url, _httpd):
        return _drive(tmp_path, f"{url}/#write_token=T", [{"until": "document.querySelector('.ck-widgets .ck-card.w')"}, *steps])["values"]


@needs_browser
def test_the_phone_header_leads_with_the_nav_and_centres_its_buttons(tmp_path: Path) -> None:
    vals = _page(tmp_path, [{"emulate": {"w": 390, "h": 844, "dpr": 2, "mobile": True}}, {"eval": """(async () => { await new Promise(r => setTimeout(r, 500));
      const top = (s) => document.querySelector(s).getBoundingClientRect().top, box = (e) => e.getBoundingClientRect();
      const seg = document.querySelector('.hero .seg'), btns = [...document.querySelectorAll('.hero .indicators button')];
      const l = Math.min(...btns.map(b => box(b).left)), r = Math.max(...btns.map(b => box(b).right));
      return { segTop: top('.hero .seg'), clockTop: top('.hero .clock'), segShare: box(seg).width / innerWidth, mid: (l + r) / 2, vw: innerWidth, sw: document.documentElement.scrollWidth }; })()"""}])
    got = vals[1]
    assert got["segTop"] < got["clockTop"] and got["segShare"] > 0.85 and abs(got["mid"] - got["vw"] / 2) < 8 and got["sw"] <= got["vw"], got


@needs_browser
def test_an_empty_card_with_no_compose_box_is_a_full_width_bar_after_its_zones_cards(tmp_path: Path) -> None:
    vals = _page(tmp_path, [{"eval": """(async () => { await new Promise(r => setTimeout(r, 500));
      const bars = [...document.querySelectorAll('.ck-zgrid > .panel.ck-fold')].filter(p => getComputedStyle(p).display !== 'none'); if (!bars.length) return null;
      return bars.map(b => { const g = b.parentElement, gw = g.getBoundingClientRect().width, bt = b.getBoundingClientRect().top;
        const cards = [...g.children].filter(p => p !== b && !p.classList.contains('ck-fold') && getComputedStyle(p).display !== 'none');
        return [b.getBoundingClientRect().width / gw, cards.every(c => c.getBoundingClientRect().bottom <= bt + 1), !!b.querySelector('.tray-dump')]; }); })()"""}])
    assert vals[1], vals
    for share, after_cards, has_box in vals[1]:
        assert share > 0.95 and after_cards and not has_box, vals

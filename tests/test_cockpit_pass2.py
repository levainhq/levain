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
def test_the_phone_bar_is_two_rows_with_the_tabs_across_the_width(tmp_path: Path) -> None:
    """K2 r2 (L2-9): brand, status dot, Sync and the theme switch on one row, the four tabs across the width on the next; no horizontal scroll."""
    vals = _page(tmp_path, [{"emulate": {"w": 390, "h": 844, "dpr": 2, "mobile": True}}, {"until": "document.querySelector('.masthead .seg') && document.querySelector('.band .clock') && document.querySelector('.masthead .indicators button')"}, {"eval": """(() => {
      const box = (e) => e.getBoundingClientRect(), q = (s) => document.querySelector(s);
      const bar = q('.masthead'), seg = q('.masthead .seg'), theme = q('.masthead .themesw'), brand = q('.masthead .brand');
      return { segTop: box(seg).top, clockTop: box(q('.band .clock')).top, segShare: box(seg).width / innerWidth, barH: box(bar).height,
        oneRow: Math.abs(box(theme).top - box(brand).top) < 24 && box(theme).bottom < box(seg).top, sw: document.documentElement.scrollWidth, vw: innerWidth }; })()"""}])
    got = vals[2]
    assert got["segTop"] < got["clockTop"] and got["segShare"] > 0.85 and got["oneRow"] and got["barH"] < 150 and got["sw"] <= got["vw"], got


@needs_browser
def test_an_empty_card_with_no_compose_box_is_a_full_width_bar_after_its_zones_cards(tmp_path: Path) -> None:
    vals = _page(tmp_path, [{"eval": "document.querySelector('[data-mode-tab=build]').click(); 1"}, {"until": "document.querySelector('.ck-zgrid .panel.ck-fold')"}, {"eval": """(() => {
      const bars = [...document.querySelectorAll('.ck-zgrid .panel.ck-fold')].filter(p => getComputedStyle(p).display !== 'none'); if (!bars.length) return null;
      return bars.map(b => { const g = b.parentElement, gw = g.getBoundingClientRect().width, bt = b.getBoundingClientRect().top;
        const cards = [...g.querySelectorAll('.panel')].filter(p => p !== b && !p.classList.contains('ck-fold') && getComputedStyle(p).display !== 'none');
        return [b.getBoundingClientRect().width / gw, cards.every(c => c.getBoundingClientRect().bottom <= bt + 1), !!b.querySelector('.tray-dump')]; }); })()"""}])
    assert vals[3], vals
    for share, after_cards, has_box in vals[3]:
        assert share > 0.95 and after_cards and not has_box, vals


def test_a_fire_reads_as_a_sentence_and_an_unknown_shape_is_named_as_the_store_names_it() -> None:
    from levain.cockpit.providers import _edit_target, plain_edit_title

    assert plain_edit_title("spore_descend", _edit_target("spore:spore-017"), "token:browser:1") == "resolved item spore-017, from the browser"
    assert plain_edit_title("section_edit", _edit_target("section:State"), "device:ab12") == "edited the State section, from a signed device"
    assert plain_edit_title("token_rotate", _edit_target("token:browser"), "token:browser:2") == "rotated the surface token browser, from the browser"
    assert plain_edit_title("pending_cancel", _edit_target("pend:xyz"), "") == "cancelled a pending change"
    assert plain_edit_title("new_verb_x", _edit_target("thing:1"), None) == "new verb x thing:1"
    assert plain_edit_title("", "", None) == ""
    assert plain_edit_title(7, "", "token:a:b") == "from the a:b"      # a non-string verb, a colon in a surface name with no generation


@needs_browser
def test_a_text_editor_keeps_its_own_size_when_typed_in(tmp_path: Path) -> None:
    vals = _page(tmp_path, [{"eval": "document.querySelector('[data-mode-tab=memory]').click(); 1"}, {"until": "[...document.querySelectorAll('.panel')].find(p => /Operator · Identity/.test((p.querySelector('h2') || {}).textContent || ''))"}, {"eval": """(() => {
      const card = [...document.querySelectorAll('.panel')].find(p => /Operator · Identity/.test((p.querySelector('h2') || {}).textContent || ''));
      card.querySelector('.edit-btn').click(); const ta = card.querySelector('.text-editor textarea');
      const before = ta.offsetHeight; ta.value = Array.from({length: 60}, (_, i) => 'line ' + i).join('\\n'); ta.dispatchEvent(new Event('input', {bubbles: true}));
      return { before, after: ta.offsetHeight, inline: ta.style.height }; })()"""}])
    got = vals[3]
    assert got["before"] > 0 and got["after"] == got["before"] and got["inline"] == "", got


@needs_browser
def test_the_clock_is_one_text_node_and_an_am_pm_at_every_hour_with_no_time_of_day_tint(tmp_path: Path) -> None:
    vals = _page(tmp_path, [{"until": "document.querySelector('.hero .clock')"}, {"eval": """(() => {
      const c = document.querySelector('.hero .clock'); const small = c.querySelector('small');
      return { kids: [...c.childNodes].map(n => n.nodeType + ':' + (n.nodeName)), colon: !!c.querySelector('.clock-colon'), tod: c.dataset.tod || '',
        colonText: /^\\d{1,2}:\\d{2}$/.test(c.firstChild.textContent), ampm: small ? small.textContent : '' }; })()"""}])
    got = vals[2]
    assert got["kids"] == ["3:#text", "1:SMALL"] and got["colonText"] and not got["colon"] and got["tod"] == "" and got["ampm"] in ("am", "pm"), got


@needs_browser
def test_project_colour_is_the_product_with_no_variant_switch_and_no_repo_of_its_own(tmp_path: Path) -> None:
    # this install's data names no repo, so the page tags nothing; a hue is a slot picked from the name alone
    vals = _page(tmp_path, [{"eval": "new Promise(r => setTimeout(r, 1800))"}, {"eval": """(() => {
      const root = getComputedStyle(document.documentElement);
      const stripe = document.querySelector('.ck-card.w[data-ck-panel="decisions"], .ck-card.w[data-ck-panel="memory"]');
      const k = window.LevainSky ? window.LevainSky.hueKey : null;
      return { variantAttr: document.documentElement.dataset.variant || '', slots: [1, 2, 3, 4, 5].map(i => !!root.getPropertyValue('--hue-' + i).trim()),
        stripe: stripe ? getComputedStyle(stripe, '::before').content : '', tags: document.querySelectorAll('.rp-tag').length,
        hues: k ? ['alpha', 'Alpha', 'my-app', 'x'.repeat(300), '', null].map(k) : [] }; })()"""}])
    got = vals[2]
    assert got["variantAttr"] == "" and all(got["slots"]) and got["stripe"] in ("none", "normal") and got["tags"] == 0, got
    assert got["hues"][0] == got["hues"][1] and all(h in "12345" and h for h in got["hues"][:4]) and got["hues"][4:] == ["", ""], got


# Phill 10-15: "there is no reason a public package should have my personal dealings in it". The names are held as hash
# prefixes so this file does not publish them either; any word of the page's own files that hashes into the set fails.
_AUTHOR_NAMES = {"13bf0c28c373b20e", "209fe9ce83ce987a", "30ac85182428479d", "5990f302c4e48d0c", "601ded6e7a9d9993",
                 "60f80eae09b53d12", "687f2181e010409a", "7b8d92a4afb4265a", "a5967a750d416704", "ac5135dc0e52a42c",
                 "f275dcde8d5ee4bb", "f8cf3c92d1f8041c"}


def test_the_page_carries_no_author_repo_or_client_name() -> None:
    import hashlib
    import re
    web = ROOT / "levain" / "templates" / "web"
    files = sorted(p for p in web.rglob("*") if p.is_file() and p.suffix in (".js", ".css", ".html"))
    assert files
    hits = sorted({(p.name, w) for p in files for w in re.findall(r"[a-z0-9]+", p.read_text(encoding="utf-8").lower())
                   if hashlib.sha256(w.encode()).hexdigest()[:16] in _AUTHOR_NAMES})
    assert not hits, hits


@needs_browser
def test_a_row_with_a_repo_facet_is_tagged_from_it_and_a_row_without_one_from_its_words(tmp_path: Path) -> None:
    # a repo named like a common word ("review") must not tag a row that carries another repo's facet and merely uses the word;
    # a row with no facet keeps the word match, so a provider that does not set the facet regresses nowhere
    from levain.cockpit.engine import ProviderSpec
    from levain.cockpit.providers import build_default_cockpit
    from levain.cockpit.results import Read, RowIn

    _root, src, _ids = _install(tmp_path)
    now = d.datetime.now(d.timezone.utc).isoformat()
    rows = [RowIn(id="p:1", title="fix the flaky test", facets={"repo": "review", "at": now}),
            RowIn(id="p:2", title="Review the cash flow report", facets={"repo": "website", "at": now}),
            RowIn(id="p:3", title="Review the invoice", facets={"at": now})]
    ck = build_default_cockpit(src)
    ck.register(ProviderSpec("projects", "list", "Projects", "feed", lambda ctx: Read(rows=rows), region="operate", rank=30,
                             order="time.desc", facets=frozenset({"repo", "at"}), version_fields=("*",), empty="none"))
    with _serve(src, surface_tokens={"browser": "T"}, cockpit=ck) as (url, _httpd):
        vals = _drive(tmp_path, f"{url}/#write_token=T", [{"until": "document.querySelector('.ck-widgets .ck-card.w')"},
                      {"until": "[...document.querySelectorAll('.ck-zgrid .row')].filter(r => /flaky|cash flow|invoice/.test(r.textContent) && r.querySelector('.rp-tag')).length === 3"}, {"eval": """(() => [...document.querySelectorAll('.ck-zgrid .row')]
        .filter(r => /flaky|cash flow|invoice/.test(r.textContent)).map(r => [r.querySelector('.clause').textContent.replace(/^(review|website)/, ''),
          (r.querySelector('.rp-tag') || {}).textContent || ''])
        )()"""}])["values"]
    got = {k.strip(): t for k, t in vals[2]}
    assert got == {"fix the flaky test": "review", "Review the cash flow report": "website", "Review the invoice": "review"}, vals[2]

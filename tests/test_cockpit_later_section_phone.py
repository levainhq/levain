"""Phill 11-09 (phone): the Needs-you view's LATER section (a header, then ``number day headline`` rows) with the live headlines'
lengths, in a real headless Chrome at a phone's width: the header is one line, each row's number and day sit on one line together
and level with the headline's first line, and a long headline wraps under its own column. The List's LATER group carries its
"back <day>" tag as one unbreakable run. ``LEVAIN_LATER_SECTION_SHOTS=<dir>`` keeps the PNGs."""
from __future__ import annotations

import json
import os
from pathlib import Path

from tests.test_cockpit_desk_tabs import READY_ANY, _later_home
from tests.test_cockpit_flip_parity import _drive, needs_browser
from tests.test_cockpit_k2a import _install, _serve

LIVE = [
    ("levain: TUE 11-10: read Levain 1.0's release essay draft 2 and fill its 17 [[PHILL]] slots; it ships with 1.0.", 1),
    ("DECISION: flow: Friction day Tue 11-10: start the friction seat (default permissions, count real prompts)?", 1),
]
PROBE = """(() => { const sec = document.querySelector('#ck-needs .needs-later'), head = sec.querySelector('.needs-later-head');
  const box = (e) => { const r = e.getBoundingClientRect(); return {t: Math.round(r.top), b: Math.round(r.bottom), l: Math.round(r.left), h: Math.round(r.height)}; };
  const lh = (e) => parseFloat(getComputedStyle(e).lineHeight) || parseFloat(getComputedStyle(e).fontSize) * 1.4;
  const rows = Array.from(sec.querySelectorAll('.needs-later-line')).map(r => { const b = r.querySelector('.needs-later-band'),
    d = r.querySelector('.needs-later-text'), t = r.querySelector('.needs-later-title');
    return { band: box(b), day: box(d), title: box(t), bandLines: Math.round(box(b).h / lh(b)), dayLines: Math.round(box(d).h / lh(d)),
             titleLines: Math.round(box(t).h / lh(t)), text: t.textContent }; });
  return JSON.stringify({ head: box(head), headLines: Math.round(box(head).h / lh(head)), headText: head.textContent, rows: rows,
    docW: document.documentElement.scrollWidth, winW: window.innerWidth }); })()"""


def _shots() -> Path | None:
    d = os.environ.get("LEVAIN_LATER_SECTION_SHOTS")
    if d:
        Path(d).mkdir(parents=True, exist_ok=True)
    return Path(d) if d else None


def _measure(tmp_path: Path, widths: list[int], prefix: str, query: str = "") -> list[dict]:
    src = _install(tmp_path)[1]
    shots = _shots()
    ready = ("document.querySelectorAll('#ck-needs .needs-later-line').length === 2 && Array.from(document.querySelectorAll("
             "'#ck-needs .needs-later-title')).every(e => e.textContent)")
    steps: list = []
    for w in widths:
        steps += [{"emulate": {"w": w, "h": 844, "dpr": 2, "mobile": True}}, {"until": READY_ANY, "ms": 20000}, {"until": ready, "ms": 20000},
                  {"eval": "document.querySelector('#ck-needs .needs-later').scrollIntoView({block: 'center'}); 1"},
                  {"eval": "new Promise(r => setTimeout(r, 300)).then(() => 1)"}, {"eval": PROBE}]
        if shots:
            steps.append({"shot": str(shots / f"{prefix}_{w}.png"), "full": False})
    with _serve(src, surface_tokens={"browser": "T"}, install_class="reported") as (url, _httpd):
        vals = _drive(tmp_path, f"{url}/{query}#write_token=T", steps)["values"]
    return [json.loads(v) for v in vals if isinstance(v, str) and v.startswith('{"head"')]


def _check(g: dict) -> None:
    assert g["headLines"] == 1 and g["headText"].lower().startswith("later"), g
    assert g["docW"] <= g["winW"], g
    for r in g["rows"]:
        assert r["bandLines"] == 1 and r["dayLines"] == 1, r                      # the number and the day never wrap
        assert abs(r["band"]["b"] - r["day"]["b"]) <= 2, r                        # on one line together
        assert abs(r["title"]["t"] - r["day"]["t"]) <= 4, r                       # level with the headline's first line
        assert r["title"]["l"] > r["day"]["l"], r                                 # the headline is a column of its own


@needs_browser
def test_later_section_rows_hold_together_on_a_phone(tmp_path: Path, monkeypatch) -> None:
    _later_home(tmp_path, monkeypatch, LIVE)
    got = _measure(tmp_path, [430, 390], os.environ.get("LEVAIN_LATER_SECTION_PREFIX", "section"))
    assert len(got) == 2, got
    for g in got:
        _check(g)


@needs_browser
def test_later_section_tiles_layout_holds_together_on_a_phone(tmp_path: Path, monkeypatch) -> None:
    _later_home(tmp_path, monkeypatch, LIVE)
    got = _measure(tmp_path, [430, 390], os.environ.get("LEVAIN_LATER_SECTION_PREFIX", "section") + "_tiles", "?needs=tiles")
    assert len(got) == 2, got
    for g in got:
        _check(g)


# ---- the List's own LATER group (the focus view): a band row's meta line carries "back <day>" ----

LIST_PROBE = """(() => { const mb = document.querySelector('#levain-modal .modal-body');
  const h = Array.from(mb.querySelectorAll('.grp-head')).find(x => /^later/i.test(x.textContent.trim()));
  const lh = (e) => parseFloat(getComputedStyle(e).lineHeight) || parseFloat(getComputedStyle(e).fontSize) * 1.4;
  const rows = Array.from((h ? h.parentElement : mb).querySelectorAll('.row')).map(r => {
    const tag = Array.from(r.querySelectorAll('[data-ck-tag]')).find(e => /^(wakes|back) /.test(e.textContent));
    const b = tag && tag.getBoundingClientRect();
    return { text: (r.querySelector('.clause') || r).textContent.slice(0, 30), tag: tag ? tag.textContent : null,
             tagLines: tag ? Math.round(b.height / lh(tag)) : null, ws: tag ? getComputedStyle(tag).whiteSpace : null }; });
  const hb = h && h.getBoundingClientRect();
  return JSON.stringify({ head: h ? h.textContent.trim() : null, headLines: h ? Math.round(hb.height / lh(h)) : null, rows: rows }); })()"""


# The reload below waits until the page's change stream (cockpit_stream.js) has left "connecting". The driver's whole-board wait
# returns the moment the board is drawn, which is the moment the page starts its stream and its activity read; a reload sent in
# that window hung the renderer for good (the reload's document never committed, every evaluate unanswered: CI runs 43059685049,
# 43133698406, 43188571836, 43197388187, 43247185880, 43292927761, 43304306382, and 43332959158 with the worker settled), and a
# local rig hung 10 of 72 such reloads, 0 of 48 with a 3 s pause before the reload or before the emulate (laneHPRED_return.md).
# Measured limit: the rig also passed 36 of 36 when this wait accepted ANY chip state (one evaluate, no real wait), so it shows
# the window is narrow, not that "connecting" is the state that matters; leaving "connecting" is the page's own signal that the
# start is over, chosen so the reload is past the window by construction rather than by a few milliseconds.
STREAM_PAST_CONNECTING = ("(() => { const c = document.querySelector('.livechip'); "
                          "return !!(c && c.dataset.state && c.dataset.state !== 'connecting'); })()")


@needs_browser
def test_the_lists_later_group_tag_is_one_line_on_a_phone(tmp_path: Path, monkeypatch) -> None:
    from dataclasses import replace
    from datetime import date, timedelta

    from levain import band_view
    from levain.consent.queue import default_queue_dir
    from levain.consent.questions import QuestionStore
    from tests.test_cockpit_k2a import TOKEN
    from tests.test_list_page_browser import _seed_more
    root, src, _ids = _install(tmp_path)
    _seed_more(root)
    qs = QuestionStore(default_queue_dir())
    for i, (what, _d) in enumerate(LIVE):
        qs.file(agent="claude_code", agent_session_id=f"sl{i}", what=what, options=[("a", "Yes"), ("b", "No")], rec="a", blocks="b", cwd="/w")
    later = (date.today() + timedelta(days=2)).isoformat()
    real = band_view._read
    whats = {w for w, _ in LIVE}

    def read(store):
        items, numbers, closed = real(store)
        return [(ref, replace(q, section="you", snoozed_until=later) if q is not None and q.what in whats else q) for ref, q in items], numbers, closed
    monkeypatch.setattr(band_view, "_read", read)
    shots = _shots()
    prefix = os.environ.get("LEVAIN_LATER_SECTION_PREFIX", "section")
    widths = (430, 390, 200)
    steps: list = []
    for w in widths:
        steps += [{"emulate": {"w": w, "h": 844, "dpr": 2, "mobile": True}}, {"until": STREAM_PAST_CONNECTING, "ms": 20000},
                  {"eval": "location.reload(); 1"},
                  {"until": "!!document.querySelector('[data-ck-panel=tray] [data-list-more]')", "ms": 30000},
                  {"eval": "document.querySelector('[data-ck-panel=tray] [data-list-more]').click(); 1"},
                  {"until": "!!document.querySelector('#levain-modal .modal-body .sp-results .row')", "ms": 20000},
                  {"eval": "new Promise(r => setTimeout(r, 700)).then(() => 1)"},
                  {"eval": "(() => { const h = Array.from(document.querySelectorAll('#levain-modal .grp-head')).find(x => /^later/i.test(x.textContent.trim())); if (h) h.scrollIntoView({block: 'start'}); return 1; })()"},
                  {"eval": LIST_PROBE}]
        if shots:
            steps.append({"shot": str(shots / f"{prefix}_list_{w}.png"), "full": False})
    with _serve(src, write_token=TOKEN, surface_tokens={"browser": TOKEN}, install_class="reported") as (url, _httpd):
        vals = _drive(tmp_path, f"{url}/", steps)["values"]
    got = [json.loads(v) for v in vals if isinstance(v, str) and v.startswith('{"head"')]
    assert len(got) == len(widths) and all(g["head"] for g in got), got
    for g in got:
        assert g["headLines"] == 1, g
        tags = [r for r in g["rows"] if r["tag"]]
        assert tags, g
        assert all(r["tagLines"] == 1 for r in tags), g                            # "back Tue 11-10" is one line
        assert all(r["ws"] == "nowrap" for r in tags), g                           # and cannot be split by a tighter line

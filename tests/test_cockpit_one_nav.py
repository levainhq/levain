"""W1c, the one nav: six destinations (Now, Board, Inbox, Grants, Build, Memory) are reachable at a phone's, a tablet's and a laptop's
width, each as a thumb-sized control that is on screen and not covered, and the nav takes the shape of the width alone (bar under 600,
rail from 600, sidebar from 840). Keys 1-6, the old `#morning` address and the kept `levain_cockpit_mode` value land on the same
destinations. Browser tests over the narrow-layout rig; widths are set through CDP device metrics (the window flag clamps below 500)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.test_cockpit_narrow_layout_rig import FullRig, NEEDS, SETTLE, TOK, _drive, drive_touch, needs_browser, needs_ssh

DESTS = ["now", "board", "inbox", "grants", "build", "memory"]
MODE_OF = {"now": "morning", "board": "board", "inbox": "board", "grants": "board", "build": "build", "memory": "memory"}
WIDTHS = [(390, 844, True, "bar"), (700, 1024, True, "rail"), (1280, 800, False, "sidebar")]

SHAPE = """(() => { const n = document.getElementById('ck-nav'), r = n.getBoundingClientRect(), vw = document.documentElement.clientWidth, vh = window.innerHeight;
  return JSON.stringify({ k: 'shape', pos: getComputedStyle(n).position, left: Math.round(r.left), top: Math.round(r.top), w: Math.round(r.width), h: Math.round(r.height),
    bottom: Math.round(r.bottom), vw, vh, scrollW: document.documentElement.scrollWidth, n: n.querySelectorAll('.ck-dest').length }); })()"""

# a modal closes before the next destination is tried: Grants opens the Decisions view
CLOSE = "(() => { const m = document.getElementById('levain-modal'); if (m) m.remove(); document.body.classList.remove('modal-open'); return 1; })()"


def _reach(dest: str) -> list:
    js = f"""(() => {{ const b = document.querySelector('.ck-dest[data-dest="{dest}"]'), r = b.getBoundingClientRect(), x = r.left + r.width / 2, y = r.top + r.height / 2;
      const top = document.elementFromPoint(x, y);
      const out = {{ k: 'reach', dest: '{dest}', w: Math.round(r.width), h: Math.round(r.height), onScreen: r.left >= -0.5 && r.top >= -0.5 && r.right <= document.documentElement.clientWidth + 0.5 && r.bottom <= window.innerHeight + 0.5, box: [r.left, r.top, r.right, r.bottom, window.innerHeight],
        uncovered: !!top && b.contains(top) }};
      b.click(); return JSON.stringify(out); }})()"""
    state = """(() => JSON.stringify({ k: 'after', dest: document.body.dataset.dest, mode: document.body.dataset.mode,
      sel: [...document.querySelectorAll('.ck-dest[aria-selected="true"]')].map(e => e.dataset.dest) }))()"""
    return [{"eval": CLOSE}, {"eval": js}, {"eval": "new Promise(r => setTimeout(r, 500)).then(() => 1)"}, {"eval": state}]


def _run(tmp_path: Path, monkeypatch, w: int, h: int, mobile: bool, steps: list) -> list:
    rig = FullRig(tmp_path, monkeypatch)
    url = f"{rig.origin}/#write_token={TOK}"
    head = [{"emulate": {"w": w, "h": h, "dpr": 2 if mobile else 1, "mobile": mobile}}]
    if mobile:
        head.append({"touch": True})
    with rig.serving():
        res = (drive_touch if mobile else _drive)(tmp_path, url, [*head, {"until": NEEDS, "ms": 30000}, SETTLE, *steps])
    return [json.loads(v) for v in res["values"] if isinstance(v, str) and v.startswith("{")]


@needs_browser
@needs_ssh
@pytest.mark.parametrize("w,h,mobile,shape", WIDTHS)
def test_every_destination_is_reachable_at_each_width(tmp_path: Path, monkeypatch, w: int, h: int, mobile: bool, shape: str) -> None:
    steps = [{"eval": SHAPE}]
    for d in DESTS:
        steps += _reach(d)
    got = _run(tmp_path, monkeypatch, w, h, mobile, steps)
    sh = got[0]
    assert sh["n"] == 6 and sh["pos"] == "fixed", sh
    assert sh["scrollW"] <= sh["vw"], sh   # no sideways scroll with the nav drawn
    if shape == "bar":
        assert sh["w"] == sh["vw"] and sh["bottom"] == sh["vh"] and sh["h"] >= 56, sh
    else:
        assert sh["left"] == 0 and sh["top"] == 0 and sh["h"] == sh["vh"], sh
        assert (sh["w"] < 100) if shape == "rail" else (sh["w"] >= 180), sh
    reach = {v["dest"]: v for v in got if v["k"] == "reach"}
    after = {v["dest"]: v for v in got if v["k"] == "after"}
    assert list(reach) == DESTS
    for d in DESTS:
        r = reach[d]
        assert r["h"] >= 44 and r["w"] >= 44, r   # a thumb's size
        assert r["onScreen"] and r["uncovered"], r
    # each tap lands on its own destination, shows its page view, and that destination alone reads as selected
    seq = [a for a in got if a["k"] == "after"]
    assert [a["dest"] for a in seq] == [d for d in DESTS], seq
    for a in seq:
        assert a["mode"] == MODE_OF[a["dest"]] and a["sel"] == [a["dest"]], a
    assert len(after) == 6


@needs_browser
@needs_ssh
def test_keys_one_to_six_choose_the_destinations_and_the_address_follows(tmp_path: Path, monkeypatch) -> None:
    steps = []
    for i, d in enumerate(DESTS, 1):
        steps += [{"eval": CLOSE},
                  {"eval": f"document.dispatchEvent(new KeyboardEvent('keydown', {{ key: '{i}', bubbles: true }})); 1"},
                  {"eval": "new Promise(r => setTimeout(r, 400)).then(() => 1)"},
                  {"eval": "JSON.stringify({ k: 'key', dest: document.body.dataset.dest, hash: location.hash })"}]
    got = [v for v in _run(tmp_path, monkeypatch, 1280, 800, False, steps) if v["k"] == "key"]
    assert [g["dest"] for g in got] == DESTS, got
    assert all(g["hash"].endswith("#" + g["dest"]) or g["hash"].endswith("&" + g["dest"]) for g in got), got


@needs_browser
@needs_ssh
@pytest.mark.parametrize("hash_,dest", [("#morning", "now"), ("#now", "now"), ("#build", "build"), ("#memory", "memory"), ("#grants", "grants")])
def test_an_old_or_new_address_opens_its_destination(tmp_path: Path, monkeypatch, hash_: str, dest: str) -> None:
    rig = FullRig(tmp_path, monkeypatch)
    url = f"{rig.origin}/#write_token={TOK}&{hash_[1:]}"
    with rig.serving():
        res = _drive(tmp_path, url, [{"emulate": {"w": 1280, "h": 800, "dpr": 1}}, {"until": "document.body.dataset.dest", "ms": 30000}, SETTLE,
                                     {"eval": "JSON.stringify({ k: 'at', dest: document.body.dataset.dest, mode: document.body.dataset.mode })"}])
    at = [json.loads(v) for v in res["values"] if isinstance(v, str) and v.startswith("{")][0]
    assert at["dest"] == dest and at["mode"] == MODE_OF[dest], at


@needs_browser
@needs_ssh
def test_a_kept_mode_from_before_the_nav_maps_to_its_destination(tmp_path: Path, monkeypatch) -> None:
    rig = FullRig(tmp_path, monkeypatch)
    url = f"{rig.origin}/#write_token={TOK}"
    with rig.serving():
        res = _drive(tmp_path, url, [{"emulate": {"w": 1280, "h": 800, "dpr": 1}}, {"until": NEEDS, "ms": 30000},
                                     {"eval": "sessionStorage.setItem('levain_cockpit_mode', 'morning'); history.replaceState(null, '', '/'); 1"},
                                     {"eval": "location.reload(); 1"}, {"until": "document.body.dataset.dest", "ms": 30000}, SETTLE,
                                     {"eval": "JSON.stringify({ k: 'at', dest: document.body.dataset.dest })"}])
    at = [json.loads(v) for v in res["values"] if isinstance(v, str) and v.startswith("{")][-1]
    assert at["dest"] == "now", at


@needs_browser
@needs_ssh
def test_an_address_naming_inbox_scrolls_to_its_block_not_just_lights_the_destination(tmp_path: Path, monkeypatch) -> None:
    """A reload at `#inbox` (a phone: the Inbox block is far down the page) lands on the block, where it once only lit the destination."""
    rig = FullRig(tmp_path, monkeypatch)
    url = f"{rig.origin}/#write_token={TOK}&inbox"
    block = "(document.querySelector('.panel[data-ck-panel=\"inbox\"]') || document.querySelector('.panel[data-ck-panel=\"notifications\"]'))"
    probe = ("JSON.stringify({ k: 'at', dest: document.body.dataset.dest, y: Math.round(window.scrollY),"
             f" top: Math.round({block}.getBoundingClientRect().top) }})")
    with rig.serving():
        res = drive_touch(tmp_path, url, [{"emulate": {"w": 390, "h": 844, "dpr": 2, "mobile": True}}, {"touch": True}, {"until": NEEDS, "ms": 30000},
                                          {"until": f"{block} && window.scrollY > 0", "ms": 30000}, {"eval": "new Promise(r => setTimeout(r, 600)).then(() => 1)"},
                                          {"eval": probe}])
    at = [json.loads(v) for v in res["values"] if isinstance(v, str) and v.startswith("{")][-1]
    assert at["dest"] == "inbox" and at["y"] > 0 and 0 <= at["top"] < 140, at

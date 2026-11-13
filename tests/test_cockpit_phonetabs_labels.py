"""The phone's jump strip at 390: each tab shows its whole label (none ends in an ellipsis or is cut at its edge), the strip scrolls
sideways when the labels outrun its width, and it never wraps to a second row. `LEVAIN_TABSTRIP_SHOT=<png path>` keeps the strip's screen."""
from __future__ import annotations

import json
import os
from pathlib import Path

from tests.test_cockpit_narrow_layout_rig import (NEEDS, PHONE, PHONE_TOUCH, SETTLE, TOK, FullRig, drive_touch, needs_browser, needs_ssh)

STRIP = """(() => { const s = document.querySelector('#ck-phonetabs'); const bs = [...s.querySelectorAll('button')];
  return JSON.stringify({ wrap: getComputedStyle(s).flexWrap, overflowX: getComputedStyle(s).overflowX,
    stripScrolls: s.scrollWidth > s.clientWidth, tops: [...new Set(bs.map(b => Math.round(b.getBoundingClientRect().top)))],
    pageScrollW: document.documentElement.scrollWidth, vw: document.documentElement.clientWidth,
    tabs: bs.map(b => ({ jump: b.dataset.phoneJump, text: b.textContent.trim(), cut: b.scrollWidth > b.clientWidth, ellipsis: getComputedStyle(b).textOverflow === 'ellipsis' && b.scrollWidth > b.clientWidth })) }); })()"""


@needs_browser
@needs_ssh
def test_every_phone_tab_shows_its_whole_label_and_the_strip_scrolls_not_wraps(tmp_path: Path, monkeypatch) -> None:
    rig = FullRig(tmp_path, monkeypatch)
    url = f"{rig.origin}/#write_token={TOK}"
    steps = [*PHONE_TOUCH, {"until": NEEDS, "ms": 30000}, SETTLE]
    shot = os.environ.get("LEVAIN_TABSTRIP_SHOT")
    if shot:
        steps.append({"shot": shot, "full": False})
    steps.append({"eval": STRIP})
    with rig.serving():
        values = [json.loads(v) for v in drive_touch(tmp_path, url, steps)["values"] if isinstance(v, str) and v.startswith("{")]
    got = values[-1]
    assert [t["jump"] for t in got["tabs"]] == ["needs", "board", "notifications", "grants", "drop", "room", "desk"], got
    assert all(t["text"] for t in got["tabs"]), got
    assert not [t for t in got["tabs"] if t["cut"] or t["ellipsis"]], got   # every label whole
    assert got["wrap"] == "nowrap" and len(got["tops"]) == 1, got           # one row
    assert got["stripScrolls"] and got["overflowX"] in ("auto", "scroll"), got   # the overflow goes sideways inside the strip
    assert got["pageScrollW"] <= got["vw"], got                             # never the page


CUE = """(() => { const s = document.querySelector('#ck-phonetabs');
  return JSON.stringify({ cue: true, right: s.classList.contains('ck-more-right'), left: s.classList.contains('ck-more-left'),
    scrolls: s.scrollWidth > s.clientWidth + 1, x: Math.round(s.scrollLeft) }); })()"""
TO_END = """(() => { const s = document.querySelector('#ck-phonetabs'); s.scrollLeft = s.scrollWidth;
  return new Promise(r => setTimeout(r, 400)).then(() => 1); })()"""


def _cue_values(tmp_path: Path, monkeypatch, view: dict, *, scroll_end: bool) -> list[dict]:
    rig = FullRig(tmp_path, monkeypatch)
    url = f"{rig.origin}/#write_token={TOK}"
    steps = [view, {"touch": True}, {"until": NEEDS, "ms": 30000}, SETTLE, {"eval": CUE}]
    if scroll_end:
        steps += [{"eval": TO_END}, {"eval": CUE}]
    with rig.serving():
        vals = drive_touch(tmp_path, url, steps)["values"]
    return [json.loads(v) for v in vals if isinstance(v, str) and v.startswith("{") and '"cue"' in v]


@needs_browser
@needs_ssh
def test_strip_cues_the_tabs_off_screen_at_390(tmp_path: Path, monkeypatch) -> None:
    start, end = _cue_values(tmp_path, monkeypatch, PHONE, scroll_end=True)
    assert start["scrolls"] and start["right"] and not start["left"], start   # Room is off the right edge: the right edge fades
    assert end["x"] > 0 and end["left"] and not end["right"], end             # at the far end the fade moves to the left edge


@needs_browser
@needs_ssh
def test_strip_has_no_cue_when_every_tab_fits(tmp_path: Path, monkeypatch) -> None:
    (got,) = _cue_values(tmp_path, monkeypatch, {"emulate": {"w": 1000, "h": 800, "dpr": 1}}, scroll_end=False)
    assert not got["scrolls"] and not got["right"] and not got["left"], got


SHRINK = """(() => { document.querySelectorAll('#ck-phonetabs button').forEach(b => { b.style.setProperty('font-size', '1px', 'important');
  b.style.setProperty('padding', '0', 'important'); b.style.setProperty('width', '8px', 'important'); b.style.setProperty('min-width', '0', 'important');
  b.style.setProperty('overflow', 'hidden', 'important'); });
  return new Promise(r => setTimeout(r, 600)).then(() => 1); })()"""
# Inline styles on the buttons (CSSOM, so a style-src policy allows them): no window resize and no change to the strip's child list,
# only the labels' width moves. The attribute write is on the button, not a childList/characterData change under the strip.
UNSHRINK = """(() => { document.querySelectorAll('#ck-phonetabs button').forEach(b => b.style.cssText = '');
  return new Promise(r => setTimeout(r, 600)).then(() => 1); })()"""


@needs_browser
@needs_ssh
def test_strip_cue_returns_when_labels_widen_without_a_watched_event(tmp_path: Path, monkeypatch) -> None:
    rig = FullRig(tmp_path, monkeypatch)
    url = f"{rig.origin}/#write_token={TOK}"
    steps = [PHONE, {"touch": True}, {"until": NEEDS, "ms": 30000}, SETTLE, {"eval": SHRINK}, {"eval": CUE}, {"eval": UNSHRINK}, {"eval": CUE}]
    with rig.serving():
        vals = drive_touch(tmp_path, url, steps)["values"]
    fit, widened = [json.loads(v) for v in vals if isinstance(v, str) and v.startswith("{") and '"cue"' in v]
    assert not fit["scrolls"] and not fit["right"], (fit, vals)            # the shrunk labels fit: the cue cleared
    assert widened["scrolls"] and widened["right"], widened        # widened again with no resize or strip mutation: the cue is back

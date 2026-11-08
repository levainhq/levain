"""The phone's jump strip at 390: each tab shows its whole label (none ends in an ellipsis or is cut at its edge), the strip scrolls
sideways when the labels outrun its width, and it never wraps to a second row. `LEVAIN_TABSTRIP_SHOT=<png path>` keeps the strip's screen."""
from __future__ import annotations

import json
import os
from pathlib import Path

from tests.test_cockpit_narrow_layout_rig import (NEEDS, PHONE_TOUCH, SETTLE, TOK, FullRig, drive_touch, needs_browser, needs_ssh)

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
    assert [t["jump"] for t in got["tabs"]] == ["needs", "board", "notifications", "grants", "drop", "room"], got
    assert all(t["text"] for t in got["tabs"]), got
    assert not [t for t in got["tabs"] if t["cut"] or t["ellipsis"]], got   # every label whole
    assert got["wrap"] == "nowrap" and len(got["tops"]) == 1, got           # one row
    assert got["stripScrolls"] and got["overflowX"] in ("auto", "scroll"), got   # the overflow goes sideways inside the strip
    assert got["pageScrollW"] <= got["vw"], got                             # never the page

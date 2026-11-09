"""Phill 11-09 (phone): the LATER word and its day split onto two lines. In a real headless Chrome at a phone's width, the
LATER tag of a board card (a snoozed question in Needs you) and of the Decisions panel's collapsed LATER line is ONE line:
the tag never wraps inside itself, the line's own words wrap around it. ``LEVAIN_LATER_TAG_SHOTS=<dir>`` keeps the PNGs."""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta
from pathlib import Path

from levain.consent.questions import QuestionStore, local_today
from tests.test_band_pages import HISTORY
from tests.test_cockpit_flip_parity import _drive, needs_browser
from tests.test_cockpit_k2a import TOKEN, _install, _serve
from tests.test_cockpit_questions import _file
from tests.test_cockpit_questions import lh  # noqa: F401  (fixture)
from tests.test_one_board_browser import _seed

WORK_BOARD = "[data-ck-panel=work_board]"
# The LATER tag is the first words of its line up to the day's label (the board's "LATER: <label>", the band's "LATER <label>").
# Measured as the box of that run of text, so the same probe reads the page before and after the tag is its own element.
PROBE = """(() => {
  const run = (root, until) => {
    if (!root) return null;
    const texts = []; const w = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
    for (let n = w.nextNode(); n; n = w.nextNode()) texts.push(n);
    const at = root.textContent.indexOf(until) + until.length;
    let seen = 0, end = null;
    for (const n of texts) { if (seen + n.textContent.length >= at) { end = [n, at - seen]; break; } seen += n.textContent.length; }
    const r = document.createRange(); r.setStart(texts[0], 0); r.setEnd(end[0], end[1]);
    const tops = new Set(Array.from(r.getClientRects()).map(x => Math.round(x.top)));
    return { lines: tops.size, height: Math.round(r.getBoundingClientRect().height),
             lineH: Math.round(parseFloat(getComputedStyle(root).lineHeight) || 16), text: root.textContent.slice(0, at) };
  };
  const band = Array.from(document.querySelectorAll('.band-later .ext-meta')).find(e => /^LATER/.test(e.textContent));
  return JSON.stringify({ card: run(document.querySelector('%(panel)s .bd-later .bd-title'), %(until)s),
                          band: run(band, %(until)s), docW: document.documentElement.scrollWidth, winW: window.innerWidth });
})()"""


def _snoozed(lh: Path) -> str:
    """A band question snoozed two days ahead, filed with a long headline so its LATER line is full."""
    qs = QuestionStore(lh)
    q = _file(qs, what="TALK: trans-allies-ohio: send Jeanne the upgrade notice on Signal, then take the Linode snapshot before the window opens")
    day = (datetime.fromisoformat(local_today(datetime.now().astimezone())) + timedelta(days=2)).date().isoformat()
    qs.snooze(q.id, until=day, channel="cli", via="cli")
    return day


def _run(tmp_path: Path, lh: Path, widths: list[int], shots: Path | None, prefix: str) -> list[dict]:
    lh.mkdir(parents=True, exist_ok=True, mode=0o700)
    _seed(lh)
    day = _snoozed(lh)
    probe = PROBE % {"panel": WORK_BOARD, "until": json.dumps(day[5:])}
    steps: list = []
    for w in widths:
        shot_name = f"{prefix}_{w}.png"
        steps += [{"emulate": {"w": w, "h": 900, "dpr": 2, "mobile": True}},
                  {"until": f"!!document.querySelector('{WORK_BOARD} .bd-later .bd-title')", "ms": 30000},
                  {"eval": f"document.querySelector('{WORK_BOARD} .bd-later').scrollIntoView({{block: 'center'}}); 1"},
                  {"eval": "new Promise(r => setTimeout(() => r(1), 400))"}]
        if shots:
            steps.append({"shot": str(shots / f"board_{shot_name}")})
        steps += [{"eval": HISTORY},
                  {"until": "!!document.querySelector('.band-later .ext-meta')", "ms": 30000},
                  {"eval": "document.querySelector('.band-later').scrollIntoView({block: 'center'}); 1"},
                  {"eval": "new Promise(r => setTimeout(() => r(1), 400))"}]
        if shots:
            steps.append({"shot": str(shots / f"band_{shot_name}")})
        steps.append({"eval": probe})
    _root, src, _ids = _install(tmp_path)
    with _serve(src, write_token=TOKEN) as (base, _h):
        vals = _drive(tmp_path, f"{base}/#write_token={TOKEN}", steps)["values"]
    return [json.loads(v) for v in vals if isinstance(v, str) and v.startswith('{"card"')]


@needs_browser
def test_the_later_tag_is_one_line_on_a_phone(tmp_path: Path, lh) -> None:  # noqa: F811
    d = os.environ.get("LEVAIN_LATER_TAG_SHOTS")
    shots = Path(d) if d else None
    if shots:
        shots.mkdir(parents=True, exist_ok=True)
    got = _run(tmp_path, lh, [430, 390, 200], shots, os.environ.get("LEVAIN_LATER_TAG_PREFIX", "shot"))
    assert got, got
    for g in got:
        for k in ("card", "band"):
            assert g[k] and g[k]["lines"] == 1 and g[k]["height"] <= g[k]["lineH"] * 1.5, (k, g)
        assert g["docW"] <= g["winW"], g

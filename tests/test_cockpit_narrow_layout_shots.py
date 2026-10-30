"""OS-3 screenshots: the cockpit over the narrow-layout rig (every phone tab's content present), as a phone shows it (390 wide,
touch) and as a desktop does (1440 wide). `LEVAIN_OS3_SHOTS=<dir>` says where the PNGs go and `LEVAIN_OS3_PREFIX` names them
(before or after). Without the directory they land in the test's tmp dir: the test then only proves the page draws at both
widths and that the phone views each hold their tab's content."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from tests.test_cockpit_consent_rows import OPEN_DECISIONS
from tests.test_cockpit_narrow_layout_rig import (DESKTOP, NEEDS, PHONE_TOUCH, SETTLE, TOK, FullRig, _drive, drive_touch,
                                                  needs_browser, needs_ssh)

HIT_MIN = 44   # the stylesheet's --hit-min
GRANTS_HEAD = "[...document.querySelectorAll('#levain-modal .ext-grouphead')].find(h => /standing grants/.test(h.textContent))"
NOTES = "[...document.querySelectorAll('.card, .panel, section, div')].find(e => /^Notification record/.test(e.textContent.trim()) && e.textContent.length < 400)"


def _look(out_dir: Path, name: str, find: str) -> list:
    """Bring the element `find` (a JS expression) to the top of the screen and keep the screen as one PNG."""
    return [{"eval": f"(() => {{ const e = {find}; if (!e) return 'missing'; e.scrollIntoView({{block: 'start'}}); return 'ok'; }})()"},
            {"eval": "new Promise(r => setTimeout(r, 400)).then(() => 1)"},
            {"shot": str(out_dir / f"{name}.png"), "full": False}]


# what a phone must be able to use: no sideways scroll, and every control at a thumb's size
METRICS = """(() => { const root = %s; const vw = document.documentElement.clientWidth;
  const small = [...root.querySelectorAll('button, input, select, textarea, summary, a[href]')]
    .filter(b => b.getClientRects().length && b.getBoundingClientRect().height < 40 && b.getBoundingClientRect().width > 0)
    .map(b => (b.className || b.tagName) + ':' + Math.round(b.getBoundingClientRect().height) + ':' + (b.textContent || '').trim().slice(0, 14));
  const wide = [...document.querySelectorAll('body *')].filter(e => e.getClientRects().length && e.getBoundingClientRect().right > vw + 1 && !e.closest('.sky, .tv-wrap, svg, .ck-phonetabs'))
    .map(e => (e.className || e.tagName) + ':' + Math.round(e.getBoundingClientRect().right)).slice(0, 12);
  const hit = [...root.querySelectorAll('.needs-history, .tray-dump-btn, .state-edit, .tv-seg-b, .tv-live, .ck-lay-btn')].filter(b => b.getClientRects().length)
    .map(b => { const r = b.getBoundingClientRect(), a = getComputedStyle(b, '::after');
      const w = Math.max(r.width, parseFloat(a.width) || 0), h = Math.max(r.height, parseFloat(a.height) || 0), cx = r.left + r.width / 2, cy = r.top + r.height / 2;
      return { c: String(b.className), w, h, l: cx - w / 2, r: cx + w / 2, t: cy - h / 2, b: cy + h / 2 }; });
  const overlap = []; for (let i = 0; i < hit.length; i++) for (let j = i + 1; j < hit.length; j++) { const p = hit[i], q = hit[j];
    if (Math.min(p.r, q.r) - Math.max(p.l, q.l) > 1 && Math.min(p.b, q.b) - Math.max(p.t, q.t) > 1) overlap.push(p.c + ' x ' + q.c); }
  return JSON.stringify({ vw, scrollW: document.documentElement.scrollWidth, wide, small: small.slice(0, 40), hit, overlap: overlap.slice(0, 12) }); })()"""


def shoot(tmp_path: Path, monkeypatch: Any, out_dir: Path, prefix: str) -> dict:
    rig = FullRig(tmp_path, monkeypatch)
    url = f"{rig.origin}/#write_token={TOK}"
    out: dict = {}
    with rig.serving():
        phone = [*PHONE_TOUCH, {"until": NEEDS, "ms": 30000}, SETTLE,
                 {"shot": str(out_dir / f"{prefix}_390.png"), "full": True},
                 {"eval": METRICS % "document.body"}]
        phone += _look(out_dir, f"{prefix}_390_needs", "document.getElementById('ck-needs')")
        phone += _look(out_dir, f"{prefix}_390_state", "document.getElementById('ck-state-slot') || document.getElementById('state')")
        phone += _look(out_dir, f"{prefix}_390_board", "document.querySelector('.panel[data-ck-panel=\"tray\"]')")
        phone += _look(out_dir, f"{prefix}_390_notes", NOTES)
        phone += [{"eval": OPEN_DECISIONS}, {"until": "document.querySelector('#levain-modal [data-consent-grant]')", "ms": 30000},
                  {"until": "document.querySelector('#levain-modal [data-coord-drop]')", "ms": 30000}, SETTLE,
                  {"eval": METRICS % "document.getElementById('levain-modal')"}]
        phone += _look(out_dir, f"{prefix}_390_decisions", "document.querySelector('#levain-modal .modal-body')")
        phone += _look(out_dir, f"{prefix}_390_grants", GRANTS_HEAD)
        phone += _look(out_dir, f"{prefix}_390_drop", "document.querySelector('#levain-modal .coord-drop-slot')")
        phone += _look(out_dir, f"{prefix}_390_drop_card", "document.querySelector('#levain-modal .coord-drop-row')")
        out["phone"] = drive_touch(tmp_path, url, phone)["values"]
        desktop = [DESKTOP, {"until": NEEDS, "ms": 30000}, SETTLE, {"shot": str(out_dir / f"{prefix}_1440.png"), "full": True},
                   {"eval": OPEN_DECISIONS}, {"until": "document.querySelector('#levain-modal [data-coord-drop]')", "ms": 30000}, SETTLE,
                   {"shot": str(out_dir / f"{prefix}_1440_decisions.png"), "full": False}]
        out["desktop"] = _drive(tmp_path, url, desktop)["values"]
    return out


@needs_browser
@needs_ssh
def test_the_cockpit_draws_every_phone_tab_at_a_phone_and_a_desktop_width(tmp_path: Path, monkeypatch) -> None:
    out_dir = Path(os.environ.get("LEVAIN_OS3_SHOTS") or tmp_path)
    prefix = os.environ.get("LEVAIN_OS3_PREFIX") or "after"
    out_dir.mkdir(parents=True, exist_ok=True)
    out = shoot(tmp_path, monkeypatch, out_dir, prefix)
    assert "missing" not in out["phone"], out["phone"]   # every view the shots look at was on the page
    metrics = [json.loads(v) for v in out["phone"] if isinstance(v, str) and v.startswith("{") and '"scrollW"' in v]
    assert len(metrics) == 2, metrics   # the page, then the Decisions view
    for m in metrics:
        print("METRICS", json.dumps(m))
        assert m["scrollW"] <= m["vw"], m   # no sideways scroll
        assert m["wide"] == [], m   # nothing drawn past the right edge but the parts that scroll on their own
        short = [h for h in m["hit"] if h["w"] < HIT_MIN or h["h"] < HIT_MIN]
        assert not short, short   # a named control's effective target (its box or its hit-area pseudo-element) is a thumb's in both directions
        assert m["overlap"] == [], m["overlap"]   # and no two of them share ground: a tap near one's edge is never taken by its neighbour
    assert metrics[0]["hit"], metrics[0]   # the page draws at least one of them, so the check above is not empty
    for name in ("390", "1440", "1440_decisions", "390_needs", "390_state", "390_board", "390_notes", "390_decisions", "390_grants",
                 "390_drop", "390_drop_card"):
        assert (out_dir / f"{prefix}_{name}.png").stat().st_size > 1000, name

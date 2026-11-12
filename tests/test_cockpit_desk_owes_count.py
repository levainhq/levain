"""Every Desk owes number the page draws is ONE number: the desk rows of the band, a dated or snoozed one included (flow's footer
lists a desk row that comes back on a later day under DESK OWES, and the registry keeps it in the List's desk group). The band
here is the shape that produced the mismatch: undated desk rows, a carried one, snoozed ones and dated ones. The old strip tab and
the Decisions card's tab each left the dated and snoozed desk rows out while the List card's control counted them, so one band
read as three numbers (11 on the footer and the List, 7 on both tabs)."""
from __future__ import annotations

import dataclasses
import json
import shutil
import subprocess
from datetime import date, timedelta
from pathlib import Path

import pytest

from levain import band_view
from levain.consent.questions import QuestionStore
from tests.test_cockpit_desk_tabs import READY_ANY
from tests.test_cockpit_flip_parity import _drive, needs_browser
from tests.test_cockpit_k2a import _install, _serve

HERE = Path(__file__).parent

# (what, spec): the host's own facts for each question, as flow's desk_cards sends them (section, carried, snoozed_until, dated)
_FUTURE = (date.today() + timedelta(days=2)).isoformat()
BAND = (
    [(f"desk plain {i}", dict(section="desk", carried=False)) for i in range(5)]
    + [("desk carried", dict(section="desk", carried=True))]
    + [(f"desk snoozed {i}", dict(section="desk", carried=False, snoozed_until=_FUTURE)) for i in range(2)]
    + [(f"desk dated {i}", dict(section="desk", carried=False, dated=_FUTURE)) for i in range(2)]
    + [("you due", dict(section="you", carried=False)), ("you snoozed", dict(section="you", carried=False, snoozed_until=_FUTURE))]
)
DESK_ROWS = sum(1 for _w, s in BAND if s["section"] == "desk")        # the footer's DESK OWES: every desk row, whatever its day
DESK_WAITING_TODAY = sum(1 for _w, s in BAND if s["section"] == "desk" and not (s.get("snoozed_until") or s.get("dated")))


def _band(tmp_path: Path, monkeypatch) -> None:
    home = tmp_path / "levain_home"
    monkeypatch.setenv("LEVAIN_HOME", str(home))
    (home / "consent").mkdir(parents=True, mode=0o700)
    qs = QuestionStore(home / "consent")
    spec: dict[str, dict] = {}
    for i, (what, facts) in enumerate(BAND):
        q = qs.file(agent="claude_code", agent_session_id=f"s{i}", what=what, options=[("a", "Yes"), ("b", "No")], rec="a", blocks="b", cwd="/w")
        spec[q.id] = facts
    orig = QuestionStore.open_questions

    def open_questions(self, *a, **kw):
        return [dataclasses.replace(q, **spec[q.id]) if q.id in spec else q for q in orig(self, *a, **kw)]
    monkeypatch.setattr(QuestionStore, "open_questions", open_questions)
    real = band_view._read          # the List reads the band through this seam

    def read(store):
        items, numbers, closed = real(store)
        return [(ref, dataclasses.replace(q, **spec[q.id]) if q is not None and q.id in spec else q) for ref, q in items], numbers, closed
    monkeypatch.setattr(band_view, "_read", read)


PROBE = """(() => {
  const tab = (s) => { const e = document.querySelector(s); return e ? e.textContent.trim() : null; };
  const tray = document.querySelector('[data-ck-panel=tray]');
  const apart = tray && tray.dataset.listApart ? JSON.parse(tray.dataset.listApart) : null;
  const more = tray && tray.querySelector('[data-list-more]');
  const dtab = Array.from(document.querySelectorAll('#levain-modal .ck-desk-tabs .ck-tab')).map(e => e.textContent.trim());
  return JSON.stringify({strip: tab('#ck-needs [data-needs-tab=desk]'), you: tab('#ck-needs [data-needs-tab=you]'),
    listDesk: apart && apart.desk, more: more ? more.textContent.trim() : null, decisions: dtab, w: window.innerWidth}); })()"""

CLOSE = ("(() => { const m = document.querySelector('#levain-modal'); const x = m && m.querySelector('.modal-close, [data-modal-close], "
         "button[aria-label*=lose]'); if (x) x.click(); else document.dispatchEvent(new KeyboardEvent('keydown', {key: 'Escape'})); return 1; })()")
SETTLE = {"eval": "new Promise(r => setTimeout(r, 900)).then(() => 1)"}


def _read_surfaces(tmp_path: Path, monkeypatch, shots: Path | None = None, prefix: str = "") -> list[dict]:
    _band(tmp_path, monkeypatch)
    _root, src, _ids = _install(tmp_path)
    steps: list = []
    for w, h, mobile in ((1280, 900, False), (390, 844, True)):
        steps += [{"emulate": {"w": w, "h": h, "dpr": 2 if mobile else 1, "mobile": mobile}}, {"until": READY_ANY, "ms": 20000},
                  {"until": "!!document.querySelector('#ck-needs [data-needs-tab=desk]') && !!document.querySelector('[data-ck-panel=tray] [data-list-more]')",
                   "ms": 30000},
                  SETTLE,
                  # the Decisions focus view, on its Desk owes tab
                  {"eval": "(() => { const b = document.querySelector('#ck-needs .needs-history'); if (b) b.click(); return 1; })()"},
                  {"until": "!!document.querySelector('#levain-modal .ck-desk-tabs .ck-tab')", "ms": 20000},
                  {"eval": "(() => { const b = Array.from(document.querySelectorAll('#levain-modal .ck-desk-tabs .ck-tab')).find(x => /^Desk owes/.test(x.textContent)); if (b) b.click(); return 1; })()"},
                  SETTLE, {"eval": PROBE}]
        if shots:
            steps.append({"shot": str(shots / f"{prefix}_decisions_{w}.png"), "full": False})
        steps += [{"eval": CLOSE}, SETTLE,
                  {"eval": "(() => { const b = document.querySelector('#ck-needs [data-needs-tab=desk]'); if (b) { b.click(); b.scrollIntoView({block: 'start'}); } return 1; })()"},
                  SETTLE]
        if shots:
            steps.append({"shot": str(shots / f"{prefix}_strip_{w}.png"), "full": False})
    with _serve(src, surface_tokens={"browser": "T"}, install_class="reported") as (url, _httpd):
        vals = _drive(tmp_path, f"{url}/#write_token=T", steps)["values"]
    return [json.loads(v) for v in vals if isinstance(v, str) and v.startswith('{"strip"')]


def _count(text: str | None) -> int | None:
    """The n of a `Desk owes (n)` tab label."""
    if text is None or not text.startswith("Desk owes (") or not text.endswith(")"):
        return None
    return int(text[len("Desk owes ("):-1])


@needs_browser
def test_every_desk_owes_number_is_the_bands_desk_rows(tmp_path: Path, monkeypatch) -> None:
    import os
    d = os.environ.get("LEVAIN_DESK_OWES_SHOTS")
    if d:
        Path(d).mkdir(parents=True, exist_ok=True)
    got = _read_surfaces(tmp_path, monkeypatch, Path(d) if d else None, os.environ.get("LEVAIN_DESK_OWES_PREFIX", "shot"))
    assert [g["w"] for g in got] == [1280, 390], got
    for g in got:
        assert _count(g["strip"]) == DESK_ROWS, g                                   # the Needs strip's tab
        assert g["listDesk"] == DESK_ROWS, g                                        # the List card's control (it already counted every desk row)
        assert f"desk owes {DESK_ROWS}" in g["more"], g
        assert [_count(t) for t in g["decisions"] if t.startswith("Desk owes")] == [DESK_ROWS], g   # the Decisions card's tab
        assert g["you"] == "Needs you (2)", g                                       # the other tab is untouched: the due row and the carried desk row
    assert DESK_WAITING_TODAY < DESK_ROWS         # the fixture holds the rows the old tabs dropped


def test_deskTabs_counts_every_open_desk_row_and_keeps_you_to_what_waits() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("needs node")
    out = subprocess.run([node, str(HERE / "desk_tabs_check.js")], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0 and "ALL PASS" in out.stdout, out.stdout + out.stderr
    assert "PASS a dated desk row is owed by the desk today: 2/4" in out.stdout, out.stdout

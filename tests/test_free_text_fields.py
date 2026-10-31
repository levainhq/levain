"""Phill 10-30: the Drop sheet's words box rendered at the browser default (~20 columns, 4 rows), impossible to type a
message in on a phone. One rule per stylesheet now gives every free-text field the full width of its sheet or card,
~8 rows of height, 16px type (no iOS zoom) and growth with its content. Measured in the headless rig at 390 and 1280:
the Drop sheet's words box, a cockpit write-in and the phone page's write-in. ``FIELDS_SHOTS=<dir>`` keeps the shots."""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from tests.test_cockpit_flip_parity import _drive, needs_browser
from tests.test_cockpit_sign_card import OPEN_Q, _Rig, needs_ssh
from tests.test_phone_card_format import ROWS, _file, _page, port  # noqa: F401  (port: fixture)
from tests.test_phone_sign_preselect import _Port

LONG = ROWS["np-ab37059b"]
VIEWS = {"390": {"emulate": {"w": 390, "h": 844, "dpr": 1, "mobile": True}}, "1280": {"emulate": {"w": 1280, "h": 900, "dpr": 1}}}


def _measure(sel: str) -> str:
    return (f"(() => {{ const e = document.querySelector({json.dumps(sel)}); if (!e) return null; const r = e.getBoundingClientRect(); "
            f"const p = e.parentElement.getBoundingClientRect(); return {{w: r.width, pw: p.width, h: r.height, "
            f"font: parseFloat(getComputedStyle(e).fontSize), over: document.documentElement.scrollWidth > window.innerWidth}}; }})()")


def _shot(tmp_path: Path, name: str) -> dict:
    folder = Path(os.environ.get("FIELDS_SHOTS") or tmp_path)
    folder.mkdir(parents=True, exist_ok=True)
    return {"shot": str(folder / f"{os.environ.get('FIELDS_STEM', 'fields_after')}_{name}.png"), "full": False}


def _wide(m: dict, rows: bool) -> None:
    assert m and not m["over"], m
    assert m["font"] >= 16, m                                  # iOS Safari zooms a focused field under 16px
    assert m["w"] >= 0.9 * m["pw"] - 2, m                      # the full width of its sheet or card
    assert m["h"] >= (150 if rows else 40), m                  # ~8 rows for a message box; a tap target for a one-line answer


@needs_browser
@needs_ssh
@pytest.mark.parametrize("view", ["390", "1280"])
def test_the_drop_box_and_the_cockpit_write_in_are_full_width_and_tall_enough(view: str, tmp_path: Path, monkeypatch) -> None:
    rig = _Rig(tmp_path, monkeypatch)
    with rig.serving() as (url, _h):
        keep = "window.localStorage.setItem('levain_write_token', 'T'); "
        out = _drive(tmp_path, f"{rig.origin}/nope", [
            VIEWS[view], rig.authenticator(),
            {"eval": f"(() => {{ {keep} location.href = '{rig.origin}/sign/drop'; return 1; }})()"},
            {"until": "document.getElementById('drop-words')", "ms": 30000},
            {"eval": _measure("#drop-words")},
            _shot(tmp_path, f"drop_empty_{view}"),
            {"eval": "(() => { const t = document.getElementById('drop-words'); const h0 = t.getBoundingClientRect().height; "
                     "t.value = Array.from({length: 40}, (_, i) => 'line ' + i).join('\\n'); t.dispatchEvent(new Event('input', {bubbles: true})); "
                     "return t.getBoundingClientRect().height > h0; })()"},
            _shot(tmp_path, f"drop_{view}"),
            {"eval": f"(() => {{ {keep} location.href = '{rig.origin}/'; return 1; }})()"},
            {"until": f"{OPEN_Q} && document.querySelector('#levain-modal .needs-question .sign-write-in')", "ms": 60000},
            {"eval": _measure("#levain-modal .needs-question .sign-write-in")},
            _shot(tmp_path, f"write_in_{view}"),
        ])
    vals = [x for x in out["values"] if not (isinstance(x, dict) and "shot" in x)]
    measured = [x for x in vals if isinstance(x, dict) and "fontSize" not in x and "font" in x]
    drop, write_in = measured[0], measured[1]
    grows = [x for x in vals if isinstance(x, bool)][0]
    _wide(drop, rows=True)
    assert grows is True, "the box grows as lines are typed"
    _wide(write_in, rows=False)


@needs_browser
@needs_ssh
@pytest.mark.parametrize("view", ["390", "1280"])
def test_the_phone_write_in_is_full_width_at_16px(view: str, tmp_path: Path, port: _Port) -> None:  # noqa: F811
    rid = _file(port, LONG)
    p = port.qdir / "sign" / f"{rid}.json"
    body = json.loads(p.read_text())
    body["write_in"] = True
    p.write_text(json.dumps(body))
    out = _drive(tmp_path, _page(port), [VIEWS[view], port.authenticator(), {"until": "document.querySelector('.req.sign input.write-in')"},
                                          {"eval": _measure(".req.sign input.write-in")}, _shot(tmp_path, f"phone_write_in_{view}")])
    _wide([x for x in out["values"] if not (isinstance(x, dict) and "shot" in x)][-1], rows=False)

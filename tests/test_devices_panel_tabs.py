"""The Devices panel keeps revoked devices out of the main list: a Revoked tab (with its count) holds them, newest-revoked first,
the chosen tab survives the panel's re-reads, and the page does not scroll sideways on a phone."""
from __future__ import annotations

import json
import time

from tests.test_cockpit_k2a import _install
from tests.test_cockpit_pairing_http import T, _call, _pair, _serve
from tests.test_pair_card_w1b import _drive, needs_browser

READY = "document.querySelector('.dev-zone') && !document.querySelector('.dev-zone').hidden && document.querySelectorAll('.dev-row').length"
ROWS = "JSON.stringify([...document.querySelectorAll('.dev-list .dev-name')].map((n) => n.textContent))"
TABS = "JSON.stringify([...document.querySelectorAll('.dev-tabs .ck-tab')].map((b) => [b.textContent, b.getAttribute('aria-selected')]))"
PICK = "[...document.querySelectorAll('.dev-tabs .ck-tab')].find((b) => b.textContent.startsWith('%s')).click() || 1"
OVERFLOW = "document.documentElement.scrollWidth - document.documentElement.clientWidth"


def _seed(port: int, active: list[str], revoked: list[str]) -> None:
    ids = {}
    for label in [*revoked, *active]:
        ids[label] = _pair(port, label=label)[0]
    for label in revoked:
        st, out, _ = _call(port, "POST", "/cockpit/pair/revoke", {"id": ids[label]}, token=T)
        assert st == 200, out
        time.sleep(1.1)       # revoked_at has one-second resolution: the order below needs distinct stamps


@needs_browser
def test_revoked_devices_have_their_own_tab_and_the_choice_survives_a_refresh(tmp_path, monkeypatch) -> None:
    _root, src, _ids = _install(tmp_path)
    with _serve(src) as (port, _h):
        _seed(port, ["Laptop", "Phone"], ["Old A", "Old B", "Old C"])
        out = _drive(tmp_path, monkeypatch, f"http://127.0.0.1:{port}/#write_token={T}", [
            {"until": READY, "ms": 20000},
            {"eval": ROWS},
            {"eval": TABS},
            {"eval": PICK % "Revoked"},
            {"eval": ROWS},
            {"eval": "document.querySelector('.dev-list .dev-pill').textContent.startsWith('revoked ')"},
            {"eval": "document.querySelectorAll('.dev-list .dev-btn').length"},
            # the panel's own re-read (the offer form calls it): the tab stays on Revoked
            {"eval": "(() => { window.__reads = 0; const f = window.fetch.bind(window);"
                     " window.fetch = (u, o) => { if (String(u).endsWith('/devices.json')) window.__reads++; return f(u, o); }; return 1; })()"},
            {"eval": "(() => { document.querySelector('.dev-label').value = 'x'; document.querySelector('.dev-offer').requestSubmit(); return 1; })()"},
            {"until": "window.__reads >= 1", "ms": 10000},
            {"eval": TABS},
            {"eval": ROWS},
            {"eval": PICK % "Active"},
            {"eval": ROWS},
        ])
    print("VALUES", json.dumps(out["values"]))
    v = out["values"]
    assert sorted(json.loads(v[1])) == ["Laptop", "Phone"], v[1]
    assert json.loads(v[2]) == [["Active (2)", "true"], ["Revoked (3)", "false"]], v[2]
    assert json.loads(v[4]) == ["Old C", "Old B", "Old A"], v[4]          # newest-revoked first
    assert v[5] is True and v[6] == 0, v[5:7]                            # a revoked row keeps its pill and has no Revoke button
    assert json.loads(v[10]) == [["Active (2)", "false"], ["Revoked (3)", "true"]], v[10]
    assert json.loads(v[11]) == ["Old C", "Old B", "Old A"], v[11]
    assert sorted(json.loads(v[13])) == ["Laptop", "Phone"], v[13]


@needs_browser
def test_no_revoked_tab_until_something_is_revoked(tmp_path, monkeypatch) -> None:
    _root, src, _ids = _install(tmp_path)
    with _serve(src) as (port, _h):
        _seed(port, ["Laptop"], [])
        out = _drive(tmp_path, monkeypatch, f"http://127.0.0.1:{port}/#write_token={T}", [
            {"until": READY, "ms": 20000},
            {"eval": "document.querySelector('.dev-tabs').hidden"},
        ])
    assert out["values"][1] is True


@needs_browser
def test_the_devices_panel_does_not_scroll_sideways_at_phone_width(tmp_path, monkeypatch) -> None:
    _root, src, _ids = _install(tmp_path)
    with _serve(src) as (port, _h):
        _seed(port, ["iPhone with a rather long name to wrap onto lines", "iPad"], ["MacBook"])
        out = _drive(tmp_path, monkeypatch, f"http://127.0.0.1:{port}/#write_token={T}", [
            {"emulate": {"w": 390, "h": 844, "dpr": 3, "mobile": True}},
            {"until": READY, "ms": 20000},
            {"eval": OVERFLOW},
            {"eval": PICK % "Revoked"},
            {"eval": OVERFLOW},
            {"eval": "JSON.stringify([...document.querySelectorAll('.dev-tabs .ck-tab')].map((b) => Math.round(b.getBoundingClientRect().height)))"},
        ])
    print("VALUES", json.dumps(out["values"]))
    v = out["values"]
    assert v[1] <= 0 and v[3] <= 0, v
    assert all(h >= 44 for h in json.loads(v[4])), v[4]

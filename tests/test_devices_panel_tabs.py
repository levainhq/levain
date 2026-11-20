"""The Devices panel keeps revoked devices out of the main list: a Revoked tab (with its count) holds them, newest-revoked first,
the chosen tab survives the panel's re-reads, and the page does not scroll sideways on a phone."""
from __future__ import annotations

import json
import os
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from levain.cockpit.firejournal import revoke_digest
from tests.test_cockpit_k2a import _install
from tests.test_cockpit_pairing_http import T, _call, _pair, _serve
from tests.test_pair_card_w1b import _drive, needs_browser

# the Paired devices section is drawn on the Settings place only (levain#141 polish): the page is taken there while it is awaited
READY = "(document.querySelector('.ia-places [data-ia-place-btn=settings]').click(), document.querySelector('.dev-zone') && !document.querySelector('.dev-zone').hidden && document.querySelectorAll('.dev-row').length)"
ROWS = "JSON.stringify([...document.querySelectorAll('.dev-list .dev-name')].map((n) => n.textContent))"
TABS = "JSON.stringify([...document.querySelectorAll('.dev-tabs .ck-tab')].map((b) => [b.textContent, b.getAttribute('aria-selected')]))"
PICK = "[...document.querySelectorAll('.dev-tabs .ck-tab')].find((b) => b.textContent.startsWith('%s')).click() || 1"
OVERFLOW = "document.documentElement.scrollWidth - document.documentElement.clientWidth"


def _seed(port: int, httpd, active: list[str], revoked: list[str]) -> None:
    ids = {}
    for label in [*revoked, *active]:
        ids[label] = _pair(port, label=label)[0]
    for label in revoked:
        st, out, _ = _call(port, "POST", "/cockpit/pair/revoke", {"id": ids[label], "expect_digest": revoke_digest([ids[label]], "family"), "mode": "family"}, token=T)
        assert st == 200, out
    # the server stamps with one-second resolution: set distinct stamps directly (later in the list = newer)
    db = sqlite3.connect(str(Path(httpd.broker.data_root) / "cockpit.db"))
    base = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(days=1)
    for k, label in enumerate(revoked):
        db.execute("UPDATE paired_devices SET revoked_at = ? WHERE id = ?", ((base + timedelta(hours=k)).isoformat(), ids[label]))
    db.commit()
    db.close()


@needs_browser
def test_revoked_devices_have_their_own_tab_and_the_choice_survives_a_refresh(tmp_path, monkeypatch) -> None:
    _root, src, _ids = _install(tmp_path)
    with _serve(src) as (port, httpd):
        _seed(port, httpd, ["Laptop", "Phone"], ["Old A", "Old B", "Old C"])
        out = _drive(tmp_path, monkeypatch, f"http://127.0.0.1:{port}/#write_token={T}", [
            {"until": READY, "ms": 20000},
            {"eval": ROWS},
            {"eval": TABS},
            {"eval": PICK % "Revoked"},
            {"eval": ROWS},
            {"eval": "document.querySelector('.dev-list .dev-pill').textContent.startsWith('revoked ')"},
            {"eval": "document.querySelectorAll('.dev-list .dev-btn').length"},
            {"eval": "(() => { const f = window.fetch.bind(window); window.__reads = 0;"
                     " window.fetch = (u, o) => { if (String(u).endsWith('/devices.json')) window.__reads++; return f(u, o); }; return 1; })()"},
            # a tab arrow key: roving tabindex, selection moves, panel labelled by the selected tab
            {"eval": "(() => { const t = document.querySelector('#dev-tab-revoked'); t.focus();"
                     " t.dispatchEvent(new KeyboardEvent('keydown', {key: 'ArrowLeft', bubbles: true}));"
                     " const b = [...document.querySelectorAll('.dev-tabs .ck-tab')];"
                     " return JSON.stringify([b.map((x) => x.tabIndex), b.map((x) => x.getAttribute('aria-selected')),"
                     " document.querySelector('.dev-list').getAttribute('aria-labelledby'), document.activeElement.id,"
                     " document.querySelector('.dev-list').getAttribute('role')]); })()"},
            {"eval": PICK % "Revoked"},
            {"eval": "(() => { window.__reads = 0; window.__p1 = Number(document.querySelector('.dev-list').dataset.paints); return 1; })()"},
            # pair a device while Revoked is open: the refresh must COMPLETE (a paint after the request), and the tab lands on Active
            {"eval": "(() => { document.querySelector('.dev-label').value = 'Fresh'; document.querySelector('.dev-offer').requestSubmit(); return 1; })()"},
            {"until": "window.__reads >= 1 && Number(document.querySelector('.dev-list').dataset.paints) >= window.__p1 + 2", "ms": 10000},
            {"eval": TABS},
            {"eval": ROWS},
            {"eval": PICK % "Revoked"},
            {"eval": ROWS},
        ])
    print("VALUES", json.dumps(out["values"]))
    v = out["values"]
    assert sorted(json.loads(v[1])) == ["Laptop", "Phone"], v[1]
    assert json.loads(v[2]) == [["Paired (2)", "true"], ["Revoked (3)", "false"]], v[2]
    assert json.loads(v[4]) == ["Old C", "Old B", "Old A"], v[4]          # newest-revoked first
    assert v[5] is True and v[6] == 0, v[5:7]                            # a revoked row keeps its pill and has no Revoke button
    kb = json.loads(v[8])
    assert kb == [[0, -1], ["true", "false"], "dev-tab-active", "dev-tab-active", "tabpanel"], kb
    assert json.loads(v[13]) == [["Paired (2)", "true"], ["Revoked (3)", "false"]], v[13]   # pairing lands on Active
    assert sorted(json.loads(v[14])) == ["Laptop", "Phone"], v[14]
    assert json.loads(v[16]) == ["Old C", "Old B", "Old A"], v[16]


@needs_browser
def test_no_revoked_tab_until_something_is_revoked(tmp_path, monkeypatch) -> None:
    _root, src, _ids = _install(tmp_path)
    with _serve(src) as (port, httpd):
        _seed(port, httpd, ["Laptop"], [])
        out = _drive(tmp_path, monkeypatch, f"http://127.0.0.1:{port}/#write_token={T}", [
            {"until": READY, "ms": 20000},
            {"eval": "document.querySelector('.dev-tabs').hidden"},
            {"eval": "getComputedStyle(document.querySelector('.dev-tabs')).display"},
            {"eval": "document.querySelector('.dev-tabs').offsetParent === null"},
        ])
    assert out["values"][1:] == [True, "none", True], out["values"]


@needs_browser
def test_the_devices_panel_does_not_scroll_sideways_at_phone_width(tmp_path, monkeypatch) -> None:
    _root, src, _ids = _install(tmp_path)
    with _serve(src) as (port, httpd):
        _seed(port, httpd, ["iPhone with a rather long name to wrap onto lines", "iPad"], ["MacBook"])
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


def _stamp(db: sqlite3.Connection, ids: dict[str, str], label: str, *, seen: "timedelta | None", made: timedelta) -> None:
    now = datetime.now(timezone.utc).replace(microsecond=0)
    db.execute("UPDATE paired_devices SET created = ?, last_seen = ? WHERE id = ?",
               ((now - made).isoformat(), None if seen is None else (now - seen).isoformat(), ids[label]))


@needs_browser
def test_a_paired_row_not_seen_for_days_says_so_and_how_to_fix_it(tmp_path, monkeypatch) -> None:
    shots = os.environ.get("LEVAIN_PINGS_SHOTS", "")
    _root, src, _ids = _install(tmp_path)
    with _serve(src) as (port, httpd):
        ids = {label: _pair(port, label=label)[0] for label in ("Stale iPad", "Fresh phone", "Never used")}
        db = sqlite3.connect(str(Path(httpd.broker.data_root) / "cockpit.db"))
        _stamp(db, ids, "Stale iPad", seen=timedelta(days=3, hours=1), made=timedelta(days=9))
        _stamp(db, ids, "Fresh phone", seen=timedelta(minutes=5), made=timedelta(days=9))
        _stamp(db, ids, "Never used", seen=None, made=timedelta(days=5))
        db.commit()
        db.close()
        rows = ("JSON.stringify(Object.fromEntries([...document.querySelectorAll('.dev-list .dev-row')].map((r) => [r.querySelector('.dev-name').textContent,"
                " [(r.querySelector('.dev-stale') || {}).textContent || '', r.querySelector('.dev-meta').textContent]])))")
        fixes = "document.querySelectorAll('.dev-zone .dev-stale-note').length"
        fix_line = "(document.querySelector('.dev-list').firstChild.className + '|' + document.querySelector('.dev-list').firstChild.textContent)"
        out = _drive(tmp_path, monkeypatch, f"http://127.0.0.1:{port}/#write_token={T}", [
            {"until": READY, "ms": 20000},
            {"eval": TABS},
            {"eval": rows},
            {"eval": fixes},
            {"eval": fix_line},
            *([{"shot": os.path.join(shots, "paired-stale-row.png"), "full": True}] if shots else []),
        ])
    v = out["values"]
    assert json.loads(v[1])[0] == ["Paired (3)", "true"], v[1]
    fix = "If this device asks for a pairing code, revoke it here and pair it again."
    got = json.loads(v[2])
    assert got["Stale iPad"][0] == "not seen for 3 days" and got["Never used"][0] == "never seen" and got["Fresh phone"][0] == "", got
    # the fix is ONE line at the top of the tab for two stale rows, and no row carries it
    assert v[3] == 1, v[3]
    assert v[4] == "dev-none dev-stale-note|" + fix, v[4]
    assert all(fix not in meta for _chip, meta in got.values()), got
    # when(): a known row reads in dates and ages, never a raw stamp
    month_day = r"[A-Z][a-z]{2} \d{1,2}"
    assert re.fullmatch(rf"paired {month_day} · last seen {month_day} · by \w+ · \S+ · pings off", got["Stale iPad"][1]), got["Stale iPad"][1]
    assert re.fullmatch(rf"paired {month_day} · last seen (just now|\d+ min ago) · by \w+ · \S+ · pings off", got["Fresh phone"][1]), got["Fresh phone"][1]
    assert not any(re.search(r"\d{10,}", meta) for _chip, meta in got.values()), got


@needs_browser
def test_a_row_with_a_not_seen_chip_keeps_its_name_readable_at_phone_width(tmp_path, monkeypatch) -> None:
    """levain#161: at 390 px the 'not seen for 3 days' chip sat beside the name in one flex line and squeezed it to ~69 px while a row
    without the chip got ~203 px. The chip wraps under the name instead, so every row's name keeps a readable width."""
    shots = os.environ.get("LEVAIN_PINGS_SHOTS", "")
    _root, src, _ids = _install(tmp_path)
    with _serve(src) as (port, httpd):
        labels = ("A long device label for the iPad", "A long device label for the phone")
        ids = {label: _pair(port, label=label)[0] for label in labels}
        db = sqlite3.connect(str(Path(httpd.broker.data_root) / "cockpit.db"))
        _stamp(db, ids, labels[0], seen=timedelta(days=3, hours=1), made=timedelta(days=9))
        _stamp(db, ids, labels[1], seen=timedelta(minutes=5), made=timedelta(days=9))
        db.commit()
        db.close()
        widths = ("JSON.stringify(Object.fromEntries([...document.querySelectorAll('.dev-list .dev-row')].map((r) => [r.querySelector('.dev-name').textContent,"
                  " [Math.round(r.querySelector('.dev-name').getBoundingClientRect().width), !!r.querySelector('.dev-stale')]])))")
        out = _drive(tmp_path, monkeypatch, f"http://127.0.0.1:{port}/#write_token={T}", [
            {"emulate": {"w": 390, "h": 844, "dpr": 3, "mobile": True}},
            {"until": READY, "ms": 20000},
            {"eval": widths},
            {"eval": OVERFLOW},
            *([{"shot": os.path.join(shots, "paired-stale-row-390.png"), "full": True}] if shots else []),
        ])
    v = out["values"]
    got = json.loads(v[1])
    stale, fresh = got[labels[0]], got[labels[1]]
    assert stale[1] and not fresh[1], got
    assert stale[0] >= 150 and stale[0] >= 0.8 * fresh[0], got
    assert v[2] <= 0, v[2]


@needs_browser
def test_no_fix_line_when_every_paired_row_is_fresh(tmp_path, monkeypatch) -> None:
    _root, src, _ids = _install(tmp_path)
    with _serve(src) as (port, httpd):
        ids = {label: _pair(port, label=label)[0] for label in ("One", "Two")}
        db = sqlite3.connect(str(Path(httpd.broker.data_root) / "cockpit.db"))
        _stamp(db, ids, "One", seen=timedelta(minutes=5), made=timedelta(days=9))
        _stamp(db, ids, "Two", seen=timedelta(hours=2), made=timedelta(hours=3))
        db.commit()
        db.close()
        out = _drive(tmp_path, monkeypatch, f"http://127.0.0.1:{port}/#write_token={T}", [
            {"until": READY, "ms": 20000},
            {"eval": "document.querySelectorAll('.dev-zone .dev-stale-note, .dev-zone .dev-stale').length"},
        ])
    assert out["values"][-1] == 0, out["values"]

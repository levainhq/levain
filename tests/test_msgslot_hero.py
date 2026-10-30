"""Phill 10-30 (17:0x): the coordinator drop is the HERO of the cockpit's home page, "front and center", not a card at the bottom of the
Decisions view. These drive the real page in headless Chrome over the narrow-layout rig (a cockpit with a Signer, hence the real
CoordSheet and a real coordinator store): where the hero sits at 390 and 1280, that Send writes a drop through the one route the
/sign/drop sheet posts to, that "Add an envelope" hands its words to that sheet, the recent drops, that no drop row is left in the
consent strip, and the one line the hero draws when the drop read does not work. ``MSGSLOT_SHOTS=<dir>`` keeps the screens."""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from tests.test_cockpit_consent_rows import OPEN_DECISIONS
from tests.test_cockpit_k2a import TOKEN as K2A_TOKEN, _install, _serve
from tests.test_cockpit_narrow_layout_rig import (NEEDS, PHONE_TOUCH, SETTLE, TOK, FullRig, _drive, drive_touch, needs_browser,
                                                  needs_ssh)

VIEWS = {"390": list(PHONE_TOUCH), "1280": [{"emulate": {"w": 1280, "h": 900, "dpr": 1}}]}
BOX = "document.getElementById('ck-drop-words')"
READY = f"{BOX} && !{BOX}.disabled"


def _drive_view(view: str, tmp_path: Path, url: str, steps: list) -> dict:
    return (drive_touch if view == "390" else _drive)(tmp_path, url, VIEWS[view] + steps)


def _shot(tmp_path: Path, name: str) -> dict:
    folder = Path(os.environ.get("MSGSLOT_SHOTS") or tmp_path)
    folder.mkdir(parents=True, exist_ok=True)
    return {"shot": str(folder / f"{os.environ.get('MSGSLOT_STEM', 'drop_after')}_{name}.png"), "full": False}


def _tagged(out: dict) -> dict:
    got = {}
    for v in out["values"]:
        if isinstance(v, str) and v.startswith("{"):
            d = json.loads(v)
            if "t" in d:
                got[d["t"]] = d
    return got


def _type(words: str) -> dict:
    return {"eval": f"(() => {{ {BOX}.value = {json.dumps(words)}; {BOX}.dispatchEvent(new Event('input', {{bubbles: true}})); return 1; }})()"}


CLICK_SEND = {"eval": "document.querySelector('#ck-drop [data-coord-drop=send]').click(); 1"}
RECEIPT_DONE = "(document.querySelector('#ck-drop .drop-receipt') || {}).textContent && /^(Sent|Already|The drop|Not sure)/.test(document.querySelector('#ck-drop .drop-receipt').textContent)"


@needs_browser
@needs_ssh
@pytest.mark.parametrize("view", ["390", "1280"])
def test_the_drop_is_the_hero_under_the_now_band_above_everything_else(view: str, tmp_path: Path, monkeypatch) -> None:
    rig = FullRig(tmp_path, monkeypatch)
    probe = ("(() => { const top = (s) => { const e = document.querySelector(s); return e && e.getClientRects().length ? Math.round(e.getBoundingClientRect().top + window.scrollY) : null; };"
             " const bot = (s) => { const e = document.querySelector(s); return e ? Math.round(e.getBoundingClientRect().bottom + window.scrollY) : null; };"
             " const box = document.getElementById('ck-drop-words').getBoundingClientRect(); const host = document.getElementById('ck-drop').getBoundingClientRect();"
             " return JSON.stringify({t: 'pos', bandBottom: bot('#ck-band'), drop: top('#ck-drop'), needs: top('#ck-needs'), brief: top('#ck-brief'), tray: top('.panel[data-ck-panel=\"tray\"]'),"
             " sky: top('#ck-skycard'), boxW: box.width, hostW: host.width, boxH: box.height, over: document.documentElement.scrollWidth > window.innerWidth,"
             " buttons: [...document.querySelectorAll('#ck-drop button')].map(b => b.textContent), insideHero: !!document.querySelector('#ck-hero > #ck-drop') }); })()")
    with rig.serving():
        out = _drive_view(view, tmp_path, f"{rig.origin}/#write_token={TOK}",
                          [{"until": NEEDS, "ms": 30000}, {"until": READY, "ms": 30000}, SETTLE, {"eval": probe}, _shot(tmp_path, f"{view}_home")])
    p = _tagged(out)["pos"]
    assert p["insideHero"] and not p["over"], p
    assert p["drop"] is not None and p["bandBottom"] <= p["drop"], p                        # directly under the now band
    assert p["drop"] < p["needs"] < p["tray"], p                                            # above the strip, the work regions...
    assert p["brief"] is None or p["drop"] < p["brief"], p                                  # ...and the brief
    assert p["sky"] is None or p["drop"] < p["sky"] or view == "390", p                     # (the narrow page draws the sky after the strip)
    assert p["boxW"] >= 0.9 * p["hostW"] - 2 and p["boxH"] >= 150, p                      # the one free-text rule: full width, ~8 rows
    assert p["buttons"][:2] == ["Send", "Add an envelope"], p


@needs_browser
@needs_ssh
def test_send_writes_the_drop_through_the_one_route_clears_the_box_and_says_what_went(tmp_path: Path, monkeypatch) -> None:
    rig = FullRig(tmp_path, monkeypatch)
    after = ("JSON.stringify({t: 'after', box: " + BOX + ".value, focused: document.activeElement === " + BOX + ", receipt: document.querySelector('#ck-drop .drop-receipt').textContent,"
             " rows: [...document.querySelectorAll('#ck-drop .drop-recent-row')].map(r => r.textContent), consent: document.querySelectorAll('#ck-needs .coord-drop-slot').length})")
    with rig.serving():
        out = _drive(tmp_path, f"{rig.origin}/#write_token={TOK}",
                     [{"emulate": {"w": 1280, "h": 900, "dpr": 1}}, {"until": NEEDS, "ms": 30000}, {"until": READY, "ms": 30000},
                      _type("tell the coordinator: land c-12"), CLICK_SEND, {"posts": 1, "ms": 15000},
                      {"until": RECEIPT_DONE + " && document.querySelectorAll('#ck-drop .drop-recent-row').length === 1", "ms": 20000},
                      {"eval": after}, _shot(tmp_path, "1280_sent")])
    a = _tagged(out)["after"]
    assert a["box"] == "" and a["focused"] is True, a
    assert a["receipt"].startswith("Sent to the coordinator: “tell the coordinator: land c-12”"), a
    assert len(a["rows"]) == 1 and "tell the coordinator: land c-12" in a["rows"][0] and "unsigned" in a["rows"][0], a   # read back from the store
    assert len(out["posts"]) == 1 and out["posts"][0].startswith("/cockpit/coord/drop "), out["posts"]                   # the sheet's route, once
    sent = json.loads(out["posts"][0].split(" ", 1)[1])
    assert set(sent) == {"words", "outbox_id", "sent_at", "store_id"} and sent["words"] == "tell the coordinator: land c-12", sent
    assert sent["outbox_id"].startswith("ph-") and sent["store_id"] is None, sent       # a fresh install has no store yet: the first drop makes it


@needs_browser
@needs_ssh
def test_add_an_envelope_opens_the_drop_sheet_with_the_typed_words(tmp_path: Path, monkeypatch) -> None:
    rig = FullRig(tmp_path, monkeypatch)
    frame = "document.querySelector('#levain-sign-sheet-frame, iframe[title=\"coordinator drop\"]')"
    inner = f"(() => {{ const f = {frame}; const d = f && f.contentDocument; return d && d.getElementById('drop-words') ? d.getElementById('drop-words').value : null; }})()"
    with rig.serving():
        out = _drive(tmp_path, f"{rig.origin}/#write_token={TOK}",
                     [{"emulate": {"w": 1280, "h": 900, "dpr": 1}}, {"until": NEEDS, "ms": 30000}, {"until": READY, "ms": 30000},
                      _type("with an envelope\nsecond line"), {"eval": "document.querySelector('#ck-drop [data-coord-drop=envelope]').click(); 1"},
                      {"until": f"{inner} === 'with an envelope\\nsecond line'", "ms": 30000},
                      {"eval": f"JSON.stringify({{t: 'sheet', src: ({frame} || {{}}).src, words: {inner}, stored: sessionStorage.getItem('levain-drop-prefill')}})"},
                      _shot(tmp_path, "1280_sheet")])
    s = _tagged(out)["sheet"]
    assert s["src"].endswith("/sign/drop") and s["words"] == "with an envelope\nsecond line", s
    assert s["stored"] is None, s                           # read once by the sheet, then gone
    assert out["posts"] == [], out["posts"]                 # the cockpit page sent nothing: the passkey signs on the sheet only


@needs_browser
@needs_ssh
def test_recent_drops_list_collapses_past_three_and_no_drop_row_is_left_in_the_consent_strip(tmp_path: Path, monkeypatch) -> None:
    rig = FullRig(tmp_path, monkeypatch)
    steps = [{"emulate": {"w": 1280, "h": 900, "dpr": 1}}, {"until": NEEDS, "ms": 30000}, {"until": READY, "ms": 30000}]
    for i in range(4):
        steps += [_type(f"drop number {i}"), CLICK_SEND, {"until": RECEIPT_DONE + f" && {BOX}.value === '' && !{BOX}.disabled", "ms": 20000}]
    steps += [{"until": "document.querySelectorAll('#ck-drop .drop-recent-row').length >= 3 && document.querySelector('#ck-drop .drop-more')", "ms": 20000},
              {"eval": "JSON.stringify({t: 'collapsed', rows: [...document.querySelectorAll('#ck-drop .drop-recent-row')].map(r => r.textContent), more: document.querySelector('#ck-drop .drop-more').textContent})"},
              {"eval": "document.querySelector('#ck-drop .drop-more').click(); 1"},
              {"eval": "JSON.stringify({t: 'opened', n: document.querySelectorAll('#ck-drop .drop-recent-row').length})"},
              {"eval": OPEN_DECISIONS}, {"until": "document.querySelector('#levain-modal [data-consent-grant]')", "ms": 30000}, SETTLE,
              {"eval": "JSON.stringify({t: 'strip', slot: document.querySelectorAll('.coord-drop-slot, .coord-drop-row, [data-coord-drop=open]').length, hero: document.querySelectorAll('#ck-drop').length})"}]
    with rig.serving():
        got = _tagged(_drive(tmp_path, f"{rig.origin}/#write_token={TOK}", steps))
    assert len(got["collapsed"]["rows"]) == 3 and got["collapsed"]["more"] == "show 1 more", got["collapsed"]
    assert "drop number 3" in got["collapsed"]["rows"][0] and "drop number 1" in got["collapsed"]["rows"][2], got["collapsed"]   # newest first
    assert got["opened"]["n"] == 4, got["opened"]
    assert got["strip"] == {"t": "strip", "slot": 0, "hero": 1}, got["strip"]      # the Decisions view holds no drop row; the hero is the one copy


@needs_browser
@needs_ssh
def test_without_a_token_the_hero_still_draws_and_says_to_sign_in(tmp_path: Path, monkeypatch) -> None:
    rig = FullRig(tmp_path, monkeypatch)
    line = ("JSON.stringify({t: 'line', note: document.querySelector('#ck-drop .drop-note').textContent, box: !!" + BOX + ", disabled: [...document.querySelectorAll('#ck-drop button, #ck-drop textarea')].every(e => e.disabled),"
            " shown: document.getElementById('ck-drop').getClientRects().length > 0})")
    with rig.serving():
        out = _drive(tmp_path, f"{rig.origin}/", [{"emulate": {"w": 1280, "h": 900, "dpr": 1}},
                     {"until": "document.querySelector('#ck-drop .drop-note') && document.querySelector('#ck-drop .drop-note').textContent", "ms": 30000},
                     SETTLE, {"eval": line}, _shot(tmp_path, "1280_no_token")])
    got = _tagged(out)["line"]
    assert got["note"].startswith("Sign in to send") and got["box"] and got["disabled"] and got["shown"], got
    assert out["posts"] == [], out["posts"]


@needs_browser
def test_a_cockpit_with_no_coordinator_store_says_so_in_one_line(tmp_path: Path) -> None:
    _root, src, _ids = _install(tmp_path)
    with _serve(src, write_token=K2A_TOKEN) as (url, _httpd):         # no Signer: /cockpit/coord/drops.json is a 404
        out = _drive(tmp_path, f"{url}/#write_token={K2A_TOKEN}",
                     [{"emulate": {"w": 1280, "h": 900, "dpr": 1}},
                      {"until": "document.querySelector('#ck-drop .drop-note') && document.querySelector('#ck-drop .drop-note').textContent", "ms": 30000},
                      {"eval": "JSON.stringify({t: 'line', note: document.querySelector('#ck-drop .drop-note').textContent, shown: document.getElementById('ck-drop').getClientRects().length > 0,"
                               " sendOff: document.querySelector('#ck-drop [data-coord-drop=send]').disabled})"}])
    got = _tagged(out)["line"]
    assert got["note"].startswith("This cockpit has no coordinator store") and got["shown"] and got["sendOff"], got

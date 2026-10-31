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


SEND = "document.querySelector('#ck-drop [data-coord-drop=send]')"
# press Send only while the words are still in the box and Send is live: the page clears the box (and forgets the request) once a
# drop is answered, so a press after that would be a different, empty send and not a repeat of this one
CLICK_SEND_JS = f"({BOX}.value && !{SEND}.disabled) ? ({SEND}.click(), 1) : 1"


def _send(*, posts: int | None = None, until: str | None = None, ms: int = 15000, every: int = 1500) -> dict:
    """Press Send, retried together with the wait for its effect. A press whose handler returned early (a repaint replaced the
    button or the box between the readiness check and the click) is done again after `every` ms. A repeat click is safe because
    it only clicks while the words are still in the box: unsent, or sent and unanswered, when the page reuses the same outbox
    id. Once the reply lands the box is empty and the repeat does nothing. The words are typed once, before, by `_sent`."""
    if posts is None and until is None:
        raise ValueError("_send needs the effect to wait for: posts= or until=")
    return {"do": CLICK_SEND_JS, "ms": ms, "every": every, **({"posts": posts} if posts is not None else {"until": until})}


def _sent(words: str, **kw) -> list:
    """Type the words once, then the retried Send step that waits for the effect named in `kw`."""
    return [_type(words), _send(**kw)]


RECEIPT_DONE = "(document.querySelector('#ck-drop .drop-receipt') || {}).textContent && /^(Sent|Already|The drop|Not sure)/.test(document.querySelector('#ck-drop .drop-receipt').textContent)"


@needs_browser
@needs_ssh
@pytest.mark.parametrize("view", ["390", "1280"])
def test_the_drop_sits_directly_under_the_needs_you_row_above_the_work_regions(view: str, tmp_path: Path, monkeypatch) -> None:
    rig = FullRig(tmp_path, monkeypatch)
    probe = ("(() => { const top = (s) => { const e = document.querySelector(s); return e && e.getClientRects().length ? Math.round(e.getBoundingClientRect().top + window.scrollY) : null; };"
             " const bot = (s) => { const e = document.querySelector(s); return e ? Math.round(e.getBoundingClientRect().bottom + window.scrollY) : null; };"
             " const box = document.getElementById('ck-drop-words').getBoundingClientRect(); const host = document.getElementById('ck-drop').getBoundingClientRect();"
             " return JSON.stringify({t: 'pos', bandBottom: bot('#ck-band'), drop: top('#ck-drop'), needs: top('#ck-needs'), needsBottom: bot('#ck-needs'), brief: top('#ck-brief'), tray: top('.panel[data-ck-panel=\"tray\"]'),"
             " sky: top('#ck-skycard'), boxW: box.width, hostW: host.width, boxH: box.height, over: document.documentElement.scrollWidth > window.innerWidth,"
             " buttons: [...document.querySelectorAll('#ck-drop button')].map(b => b.textContent), afterNeeds: !!document.querySelector('#ck-needs + #ck-drop'), insideHero: !!document.querySelector('#ck-hero #ck-drop') }); })()")
    with rig.serving():
        out = _drive_view(view, tmp_path, f"{rig.origin}/#write_token={TOK}",
                          [{"until": NEEDS, "ms": 30000}, {"until": READY, "ms": 30000}, SETTLE, {"eval": probe}, _shot(tmp_path, f"{view}_home")])
    p = _tagged(out)["pos"]
    assert p["needs"] is not None and p["bandBottom"] is not None and p["drop"] is not None, p   # a hidden Needs-you row fails here, not as a TypeError below
    assert p["afterNeeds"] and not p["insideHero"] and not p["over"], p                    # the next element after the strip in the document, outside the hero
    assert p["bandBottom"] <= p["needs"] and p["needsBottom"] <= p["drop"], p               # under the band, and under the strip (the document order is the narrow order too)
    assert p["drop"] < p["tray"], p                                                         # above the work regions...
    assert p["brief"] is None or p["drop"] < p["brief"], p                                  # ...and the brief
    assert p["sky"] is None or (p["sky"] < p["needs"] if view == "1280" else p["needs"] < p["sky"]), p   # the desktop sky card sits above the strip; the narrow page draws it after
    assert p["boxW"] >= (0.55 if view == "1280" else 0.85) * p["hostW"] and p["boxH"] >= 150, p   # the free-text rule: the box fills its card (a side column of drops beside it from 1100px), ~8 rows
    assert p["buttons"][:2] == ["Send", "Add an envelope"], p


@needs_browser
@needs_ssh
def test_a_hidden_drop_paints_nothing_at_desktop_width(tmp_path: Path, monkeypatch) -> None:
    rig = FullRig(tmp_path, monkeypatch)
    probe = ("(() => { const d = document.getElementById('ck-drop'); const was = d.hidden; d.hidden = true; const cs = getComputedStyle(d);"
             " const r = JSON.stringify({t: 'hid', was, display: cs.display, h: d.getBoundingClientRect().height}); d.hidden = was; return r; })()")
    with rig.serving():
        out = _drive(tmp_path, f"{rig.origin}/#write_token={TOK}",
                     [{"emulate": {"w": 1280, "h": 900, "dpr": 1}}, {"until": NEEDS, "ms": 30000}, {"until": READY, "ms": 30000}, SETTLE, {"eval": probe}])
    h = _tagged(out)["hid"]
    assert h["was"] is False and h["display"] == "none" and h["h"] == 0, h   # a [hidden] card does not paint as an empty frame at >=1100px


@needs_browser
@needs_ssh
@pytest.mark.parametrize("bad_view", ["null", "'not a view'"])
def test_the_drop_leaves_with_the_ke_page_and_returns_with_the_next_ke_render(bad_view: str, tmp_path: Path, monkeypatch) -> None:
    rig = FullRig(tmp_path, monkeypatch)
    state = ("(() => { const d = document.getElementById('ck-drop'); const b = document.getElementById('ck-drop-words');"
             " return JSON.stringify({t: 'TAG', hidden: d.hidden, display: getComputedStyle(d).display, shown: d.getClientRects().length > 0,"
             " send: !!document.querySelector('#ck-drop [data-coord-drop=send]'), box: !!b}); })()")
    away = f"window.LevainDashboard.render({bad_view}); 1"
    with rig.serving():
        out = _drive(tmp_path, f"{rig.origin}/#write_token={TOK}",
                     [{"emulate": {"w": 1280, "h": 900, "dpr": 1}}, {"until": NEEDS, "ms": 30000}, {"until": READY, "ms": 30000}, SETTLE,
                      {"eval": state.replace("TAG", "on")}, {"eval": away}, {"eval": state.replace("TAG", "away")},
                      {"eval": "document.dispatchEvent(new Event('visibilitychange')); 1"},
                      {"until": "!document.getElementById('ck-drop').hidden", "ms": 20000}, SETTLE, {"eval": state.replace("TAG", "back")}])
    t = _tagged(out)
    assert t["on"]["hidden"] is False and t["on"]["shown"] is True, t["on"]
    assert t["away"]["hidden"] is True and t["away"]["display"] == "none" and t["away"]["shown"] is False, t["away"]   # not drawn, so Send cannot be clicked on stale state
    assert t["back"]["hidden"] is False and t["back"]["shown"] is True and t["back"]["send"] is True, t["back"]


@needs_browser
@needs_ssh
def test_send_writes_the_drop_through_the_one_route_clears_the_box_and_says_what_went(tmp_path: Path, monkeypatch) -> None:
    rig = FullRig(tmp_path, monkeypatch)
    after = ("JSON.stringify({t: 'after', box: " + BOX + ".value, focused: document.activeElement === " + BOX + ", receipt: document.querySelector('#ck-drop .drop-receipt').textContent,"
             " rows: [...document.querySelectorAll('#ck-drop .drop-recent-row')].map(r => r.textContent), consent: document.querySelectorAll('#ck-needs .coord-drop-slot').length})")
    with rig.serving():
        out = _drive(tmp_path, f"{rig.origin}/#write_token={TOK}",
                     [{"emulate": {"w": 1280, "h": 900, "dpr": 1}}, {"until": NEEDS, "ms": 30000}, {"until": READY, "ms": 30000},
                      *_sent("tell the coordinator: land c-12", posts=1),
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
        steps += [*_sent(f"drop number {i}", until=RECEIPT_DONE + f" && {BOX}.value === '' && !{BOX}.disabled", ms=20000)]
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


GAPS = ("(() => { const h = document.getElementById('ck-drop'); const s = ['.needs-headline', '.drop-words', '.drop-actions', '.drop-receipt', '.drop-earlier'];"
        " const b = s.map(x => h.querySelector(x).getBoundingClientRect()); const g = []; for (let i = 1; i < b.length; i++) g.push(Math.round(b[i].top - b[i - 1].bottom));"
        " return JSON.stringify({t: 'TAG', gaps: g, kids: h.children.length, recent: Math.round(h.querySelector('.drop-recent').getBoundingClientRect().height),"
        " left: Math.round(h.querySelector('.drop-main').getBoundingClientRect().height)}); })()")


@needs_browser
@needs_ssh
def test_an_expanded_recent_list_taller_than_the_composer_does_not_move_the_composers_rows(tmp_path: Path, monkeypatch) -> None:
    """The recent column is one of the card's two children, so however tall it grows the left column's gaps are what they were."""
    rig = FullRig(tmp_path, monkeypatch)
    steps = [{"emulate": {"w": 1280, "h": 900, "dpr": 1}}, {"until": NEEDS, "ms": 30000}, {"until": READY, "ms": 30000}]
    for i in range(4):
        steps += [*_sent(f"drop number {i}", until=RECEIPT_DONE + f" && {BOX}.value === '' && !{BOX}.disabled", ms=20000)]
    fill = ("(() => { const h = document.getElementById('ck-drop'); h.querySelector('.drop-earlier').textContent = '1 earlier drop not confirmed.'; return 1; })()")
    # the clones go in and the gaps are read in ONE evaluation, so a redraw of the list cannot land between them
    grow = ("(() => { const r = document.querySelector('#ck-drop .drop-recent'); const row = r.querySelector('.drop-recent-row');"
            " for (let i = 0; i < 12; i++) r.insertBefore(row.cloneNode(true), r.querySelector('.drop-more')); return " + GAPS.replace("TAG", "after") + "; })()")
    steps += [{"until": "document.querySelector('#ck-drop .drop-more')", "ms": 20000}, {"eval": fill}, {"eval": GAPS.replace("TAG", "before")},
              {"eval": "document.querySelector('#ck-drop .drop-more').click(); 1"}, {"eval": grow}]
    with rig.serving():
        got = _tagged(_drive(tmp_path, f"{rig.origin}/#write_token={TOK}", steps))
    before, after = got["before"], got["after"]
    assert after["recent"] > after["left"] + 200, got                  # the list really is taller than the composer
    assert after["gaps"] == before["gaps"] and after["left"] == before["left"], got   # ...and the composer's rows did not move
    assert all(g <= 24 for g in after["gaps"]), got                    # natural gaps, no spread
    assert after["kids"] == 2, got                                     # the composer and the recent column are the card's only children


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


# ---- L3 r1 (1030-42): the retained retry, the read cache, storage that throws, a bad row, the prefill, the key --------------------------

LOSE_FIRST_REPLY = ("(() => { const f = window.fetch; let n = 0; window.fetch = function (u, o) { const url = typeof u === 'string' ? u : (u && u.url) || '';"
                    " if (o && o.method === 'POST' && url.indexOf('/cockpit/coord/drop') >= 0 && n++ === 0) return f.apply(this, arguments).then(() => { throw new TypeError('reply lost'); });"
                    " return f.apply(this, arguments); }; })();")
PEND = "'levain-drop-pending:' + location.hostname"


def _posted(out: dict) -> list:
    return [json.loads(p.split(" ", 1)[1]) for p in out["posts"] if p.startswith("/cockpit/coord/drop ")]


@needs_browser
@needs_ssh
def test_a_send_whose_reply_was_lost_goes_again_as_the_same_request_and_files_once(tmp_path: Path, monkeypatch) -> None:
    rig = FullRig(tmp_path, monkeypatch)
    steps = [{"emulate": {"w": 1280, "h": 900, "dpr": 1}}, {"init": LOSE_FIRST_REPLY}, {"until": NEEDS, "ms": 30000}, {"until": READY, "ms": 30000},
             *_sent("land c-14", posts=1), {"until": RECEIPT_DONE + " && /^Not sure/.test(document.querySelector('#ck-drop .drop-receipt').textContent)", "ms": 20000},
             {"eval": "new Promise(r => setTimeout(r, 1100)).then(() => 1)"}, _send(posts=2),
             {"until": "/^Already delivered/.test(document.querySelector('#ck-drop .drop-receipt').textContent) && document.querySelectorAll('#ck-drop .drop-recent-row').length === 1", "ms": 20000}]
    with rig.serving():
        out = drive_touch(tmp_path, f"{rig.origin}/#write_token={TOK}", steps)
    first, again = _posted(out)
    assert first == again, (first, again)            # same words, outbox_id, sent_at and store_id: the identical request


# the first drop's reply comes 1600 ms late (past two retry ticks of 700 ms), and a flag is raised 1500 ms after it lands: the wait
# below stays open over the stretch in which a repeated action would see the box already cleared
SLOW_FIRST_REPLY = ("(() => { const f = window.fetch; let n = 0; window.fetch = function (u, o) { const url = typeof u === 'string' ? u : (u && u.url) || '';"
                    " if (o && o.method === 'POST' && url.indexOf('/cockpit/coord/drop') >= 0 && n++ === 0)"
                    " return f.apply(this, arguments).then((r) => new Promise((ok) => setTimeout(() => { ok(r); setTimeout(() => { window.__settled = 1; }, 1500); }, 1600)));"
                    " return f.apply(this, arguments); }; })();")


def _slow_reply_steps(send_steps: list) -> list:
    return [{"emulate": {"w": 1280, "h": 900, "dpr": 1}}, {"init": SLOW_FIRST_REPLY}, {"until": NEEDS, "ms": 30000}, {"until": READY, "ms": 30000},
            *send_steps]


@needs_browser
@needs_ssh
def test_a_repeated_send_press_over_a_slow_reply_files_one_drop_and_leaves_the_box_empty(tmp_path: Path, monkeypatch) -> None:
    rig = FullRig(tmp_path, monkeypatch)
    done = f"window.__settled && /^Sent/.test(document.querySelector('#ck-drop .drop-receipt').textContent) && {BOX}.value === ''"
    steps = _slow_reply_steps([*_sent("one drop only", until=done, ms=20000, every=700),
                               {"eval": f"JSON.stringify({{t: 'end', box: {BOX}.value, receipt: document.querySelector('#ck-drop .drop-receipt').textContent}})"}])
    with rig.serving():
        out = drive_touch(tmp_path, f"{rig.origin}/#write_token={TOK}", steps)
    posted = _posted(out)
    end = _tagged(out)["end"]
    assert len(posted) == 1 and posted[0]["words"] == "one drop only", out["posts"]      # the press repeated past the reply filed nothing more
    assert end["box"] == "" and end["receipt"].startswith("Sent to the coordinator: “one drop only”"), end


@needs_browser
@needs_ssh
def test_a_kept_request_from_an_earlier_visit_is_resent_whole_envelope_and_store_included(tmp_path: Path, monkeypatch) -> None:
    rig = FullRig(tmp_path, monkeypatch)
    seed = {"words": "signed earlier", "outbox_id": "ph-" + "ab" * 16, "sent_at": "2026-10-30T12:00:00.123000Z", "store_id": "store-from-before", "envelope": {"kind": "kept"}}
    init = f"localStorage.setItem({PEND}, {json.dumps(json.dumps(seed))});"
    steps = [{"emulate": {"w": 1280, "h": 900, "dpr": 1}}, {"init": init}, {"until": NEEDS, "ms": 30000}, {"until": READY, "ms": 30000}, _send(posts=1)]
    with rig.serving():
        out = drive_touch(tmp_path, f"{rig.origin}/#write_token={TOK}", steps)
    assert _posted(out) == [seed], _posted(out)


@needs_browser
@needs_ssh
def test_words_edited_after_a_lost_reply_are_a_new_drop_not_the_old_id(tmp_path: Path, monkeypatch) -> None:
    rig = FullRig(tmp_path, monkeypatch)
    steps = [{"emulate": {"w": 1280, "h": 900, "dpr": 1}}, {"init": LOSE_FIRST_REPLY}, {"until": NEEDS, "ms": 30000}, {"until": READY, "ms": 30000},
             *_sent("words A", posts=1), {"until": RECEIPT_DONE + " && /^Not sure/.test(document.querySelector('#ck-drop .drop-receipt').textContent)", "ms": 20000},
             *_sent("words B", posts=2), {"until": "/^Sent/.test(document.querySelector('#ck-drop .drop-receipt').textContent)", "ms": 20000}]
    with rig.serving():
        out = drive_touch(tmp_path, f"{rig.origin}/#write_token={TOK}", steps)
    a, b = _posted(out)
    assert a["outbox_id"] != b["outbox_id"] and b["words"] == "words B", (a, b)


@needs_browser
@needs_ssh
def test_a_signed_drop_from_the_sheet_shows_in_the_hero_at_once_not_after_the_read_cache(tmp_path: Path, monkeypatch) -> None:
    rig = FullRig(tmp_path, monkeypatch)
    frame = "document.querySelector('#levain-sign-sheet-frame, iframe[title=\"coordinator drop\"]')"
    click_in = f"(() => {{ const f = {frame}; const d = f && f.contentDocument; const b = d && d.getElementById('drop-send'); if (!b || b.disabled) return 0; d.getElementById('drop-words').value = 'from the sheet'; b.click(); return 1; }})()"
    steps = [{"emulate": {"w": 1280, "h": 900, "dpr": 1}}, {"until": NEEDS, "ms": 30000}, {"until": READY, "ms": 30000},
             {"eval": "document.querySelector('#ck-drop [data-coord-drop=envelope]').click(); 1"}, {"until": click_in, "ms": 30000}, {"posts": 1, "ms": 15000},
             {"until": "document.querySelectorAll('#ck-drop .drop-recent-row').length === 1 && /from the sheet/.test(document.querySelector('#ck-drop .drop-recent-row').textContent)", "ms": 15000}]
    with rig.serving():
        out = _drive(tmp_path, f"{rig.origin}/#write_token={TOK}", steps)
    assert len(_posted(out)) == 1, out["posts"]


GET_STALE_ONCE = ("(() => { const f = window.fetch; window.__slowGet = false; window.fetch = function (u, o) { const url = typeof u === 'string' ? u : (u && u.url) || '';"
                  " if (window.__slowGet && url.indexOf('/cockpit/coord/drops.json') >= 0) { window.__slowGet = false;"
                  " return f.apply(this, arguments).then((r) => new Promise((ok) => setTimeout(() => ok(r), 5000))); }"
                  " return f.apply(this, arguments); }; })();")


@needs_browser
@needs_ssh
def test_a_late_older_read_never_overwrites_the_list_a_send_just_drew(tmp_path: Path, monkeypatch) -> None:
    rig = FullRig(tmp_path, monkeypatch)
    wait = int(os.environ.get("DROPGEN_WAIT_MS", "3600"))        # the read cache holds 3 s once failed, 30 s once read: a render past it reads again
    steps = [{"emulate": {"w": 1280, "h": 900, "dpr": 1}}, {"init": GET_STALE_ONCE}, {"until": NEEDS, "ms": 30000}, {"until": READY, "ms": 30000},
             {"eval": f"new Promise(r => setTimeout(r, {wait})).then(() => 1)"},
             {"eval": "window.__slowGet = true; document.dispatchEvent(new Event('visibilitychange')); 1"}, {"eval": "new Promise(r => setTimeout(r, 800)).then(() => 1)"},
             *_sent("drawn once", posts=1),
             {"until": "document.querySelectorAll('#ck-drop .drop-recent-row').length === 1", "ms": 15000},
             {"eval": "new Promise(r => setTimeout(r, 6500)).then(() => 1)"},
             {"eval": "JSON.stringify({t: 'late', rows: document.querySelectorAll('#ck-drop .drop-recent-row').length})"}]
    with rig.serving():
        out = drive_touch(tmp_path, f"{rig.origin}/#write_token={TOK}", steps)
    assert _tagged(out)["late"]["rows"] == 1, out["values"][-1]


@needs_browser
@needs_ssh
def test_storage_that_throws_does_not_block_the_send_and_the_receipt_says_a_reload_may_repeat_it(tmp_path: Path, monkeypatch) -> None:
    rig = FullRig(tmp_path, monkeypatch)
    init = "Storage.prototype.setItem = function () { throw new DOMException('quota', 'QuotaExceededError'); };" + LOSE_FIRST_REPLY
    steps = [{"emulate": {"w": 1280, "h": 900, "dpr": 1}}, {"init": init}, {"until": NEEDS, "ms": 30000}, {"until": READY, "ms": 30000},
             *_sent("no storage", posts=1), {"until": RECEIPT_DONE + " && /^Not sure/.test(document.querySelector('#ck-drop .drop-receipt').textContent)", "ms": 20000},
             {"eval": "JSON.stringify({t: 'r', receipt: document.querySelector('#ck-drop .drop-receipt').textContent})"}]
    with rig.serving():
        out = drive_touch(tmp_path, f"{rig.origin}/#write_token={TOK}", steps)
    assert len(_posted(out)) == 1, out["posts"]
    assert "reload" in _tagged(out)["r"]["receipt"], _tagged(out)["r"]


@needs_browser
@needs_ssh
def test_one_malformed_entry_in_the_recent_list_is_skipped_not_the_whole_list(tmp_path: Path, monkeypatch) -> None:
    rig = FullRig(tmp_path, monkeypatch)
    body = {"coord_store_read": True, "coord_store_id": "s1", "coord_drops": [None, 7, {"drop_id": "d1", "words": "the good one", "sent_at": "2026-10-30T12:00:00.000000Z"}]}
    init = ("(() => { const f = window.fetch; window.fetch = function (u, o) { const url = typeof u === 'string' ? u : (u && u.url) || '';"
            " if (url.indexOf('/cockpit/coord/drops.json') >= 0) return Promise.resolve(new Response(" + json.dumps(json.dumps(body)) + ", {status: 200, headers: {'Content-Type': 'application/json'}}));"
            " return f.apply(this, arguments); }; })();")
    steps = [{"emulate": {"w": 1280, "h": 900, "dpr": 1}}, {"init": init}, {"until": NEEDS, "ms": 30000}, {"until": READY, "ms": 30000},
             {"eval": "JSON.stringify({t: 'rows', rows: [...document.querySelectorAll('#ck-drop .drop-recent-row')].map(r => r.textContent)})"}]
    with rig.serving():
        out = drive_touch(tmp_path, f"{rig.origin}/#write_token={TOK}", steps)
    rows = _tagged(out)["rows"]["rows"]
    assert len(rows) == 1 and "the good one" in rows[0], rows


@needs_browser
@needs_ssh
def test_a_closed_sheet_leaves_no_prefill_behind(tmp_path: Path, monkeypatch) -> None:
    rig = FullRig(tmp_path, monkeypatch)
    steps = [{"emulate": {"w": 1280, "h": 900, "dpr": 1}}, {"until": NEEDS, "ms": 30000}, {"until": READY, "ms": 30000},
             {"eval": "window.LevainSignSheet.openDrop('typed in the cockpit'); window.LevainSignSheet.close(); 1"},
             {"eval": "JSON.stringify({t: 'pre', stored: sessionStorage.getItem('levain-drop-prefill')})"}]
    with rig.serving():
        out = _drive(tmp_path, f"{rig.origin}/#write_token={TOK}", steps)
    assert _tagged(out)["pre"]["stored"] is None, _tagged(out)["pre"]


@needs_browser
@needs_ssh
def test_the_hero_box_and_its_typed_words_survive_the_cockpit_re_render(tmp_path: Path, monkeypatch) -> None:
    rig = FullRig(tmp_path, monkeypatch)
    mark = f"(() => {{ {BOX}.__mark = 1; const w = document.getElementById('ck-when'); w.__gen = 1; if (w.firstChild) w.firstChild.__gen = 1; return 1; }})()"
    redrawn = "(() => { const w = document.getElementById('ck-when'); return w && w.firstChild && !w.firstChild.__gen; })()"
    steps = [{"emulate": {"w": 1280, "h": 900, "dpr": 1}}, {"until": NEEDS, "ms": 30000}, {"until": READY, "ms": 30000}, _type("typed, not sent"), {"eval": mark},
             {"eval": "document.dispatchEvent(new Event('visibilitychange')); 1"}, {"until": redrawn, "ms": 20000}, SETTLE,
             {"eval": f"JSON.stringify({{t: 'kept', same: {BOX}.__mark === 1, value: {BOX}.value, afterNeeds: !!document.querySelector('#ck-needs + #ck-drop')}})"}]
    with rig.serving():
        out = _drive(tmp_path, f"{rig.origin}/#write_token={TOK}", steps)
    assert _tagged(out)["kept"] == {"t": "kept", "same": True, "value": "typed, not sent", "afterNeeds": True}, _tagged(out)["kept"]


def test_the_sheet_and_the_hero_derive_the_one_kept_key_the_same_way() -> None:
    root = Path(__file__).resolve().parent.parent / "levain" / "templates" / "web"
    core, sign = (root / "dashboard_core.js").read_text(), (root / "sign.js").read_text()
    want = '"levain-drop-pending:" + window.location.hostname'
    assert want in core and want in sign, "the hero and the sheet must build the kept-drop key the same way"
    assert 'dropPendKey() + ":" + id' in core and 'PEND_KEY + ":" + id' in sign, "and the per-outbox-id key the same way"


# ---- L3 r2 (1030-42): pending requests are kept per outbox id, the sheet's "reload", the grow of a programmatic value, the failure colour ----

SIGNED_ID = "ph-" + "ab" * 16
SIGNED = {"words": "signed earlier", "outbox_id": SIGNED_ID, "sent_at": "2026-10-30T12:00:00.123000Z", "store_id": "store-from-before", "envelope": {"kind": "kept"}}
EARLIER = "document.querySelector('#ck-drop .drop-earlier').textContent"


def _seed(rec: dict) -> str:
    return f"localStorage.setItem({PEND} + ':' + {json.dumps(rec['outbox_id'])}, {json.dumps(json.dumps(rec))});"


KEYS = "JSON.stringify({t: 'keys', keys: Object.keys(localStorage).filter(k => k.indexOf('levain-drop-pending:') === 0).sort(), box: document.getElementById('ck-drop-words').value, earlier: " + EARLIER + "})"


@needs_browser
@needs_ssh
def test_a_new_drop_never_overwrites_a_restored_signed_pending_one_and_the_signed_one_comes_back_whole(tmp_path: Path, monkeypatch) -> None:
    rig = FullRig(tmp_path, monkeypatch)
    steps = [{"emulate": {"w": 1280, "h": 900, "dpr": 1}}, {"init": _seed(SIGNED)}, {"until": NEEDS, "ms": 30000}, {"until": READY, "ms": 30000},
             *_sent("something else entirely", posts=1), {"until": "/^Sent/.test(document.querySelector('#ck-drop .drop-receipt').textContent)", "ms": 20000},
             {"eval": KEYS}, _send(posts=2)]
    with rig.serving():
        out = drive_touch(tmp_path, f"{rig.origin}/#write_token={TOK}", steps)
    new, again = _posted(out)
    assert new["words"] == "something else entirely" and "envelope" not in new and new["outbox_id"] != SIGNED_ID, new
    k = _tagged(out)["keys"]
    assert len(k["keys"]) == 1 and k["keys"][0].endswith(":" + SIGNED_ID), k      # the new one answered and left; the signed one is still kept
    assert k["box"] == "signed earlier" and "not confirmed earlier" in k["earlier"], k       # and it is back in the box, to send unchanged
    assert again == SIGNED, again                                                            # whole: envelope and store included


@needs_browser
@needs_ssh
def test_an_unanswered_new_drop_leaves_the_older_pending_one_kept_and_counted(tmp_path: Path, monkeypatch) -> None:
    rig = FullRig(tmp_path, monkeypatch)
    steps = [{"emulate": {"w": 1280, "h": 900, "dpr": 1}}, {"init": _seed(SIGNED) + LOSE_FIRST_REPLY}, {"until": NEEDS, "ms": 30000}, {"until": READY, "ms": 30000},
             *_sent("lost reply", posts=1), {"until": RECEIPT_DONE + " && /^Not sure/.test(document.querySelector('#ck-drop .drop-receipt').textContent)", "ms": 20000},
             {"eval": KEYS}]
    with rig.serving():
        out = drive_touch(tmp_path, f"{rig.origin}/#write_token={TOK}", steps)
    k = _tagged(out)["keys"]
    assert len(k["keys"]) == 2 and any(x.endswith(SIGNED_ID) for x in k["keys"]), k
    assert k["earlier"] == "1 earlier drop not confirmed.", k


def _reply(envelope: dict) -> str:
    body = {"created": True, "drop": {"drop_id": "d-1"}, "store_id": "s1", "envelope": envelope}
    return ("(() => { const f = window.fetch; window.fetch = function (u, o) { const url = typeof u === 'string' ? u : (u && u.url) || '';"
            " if (o && o.method === 'POST' && url.indexOf('/cockpit/coord/drop') >= 0) return Promise.resolve(new Response(" + json.dumps(json.dumps(body)) + ", {status: 200, headers: {'Content-Type': 'application/json'}}));"
            " return f.apply(this, arguments); }; })();")


@needs_browser
@needs_ssh
@pytest.mark.parametrize("envelope,says,bad", [({"taken": True, "envelope_id": "env-1"}, "envelope env-1 taken", False),
                                               ({"taken": False, "refused": "stale signature"}, "envelope NOT taken: stale signature", True)])
def test_a_resent_signed_request_says_what_became_of_its_envelope_not_words_only(envelope: dict, says: str, bad: bool, tmp_path: Path, monkeypatch) -> None:
    rig = FullRig(tmp_path, monkeypatch)
    steps = [{"emulate": {"w": 1280, "h": 900, "dpr": 1}}, {"init": _seed(SIGNED) + _reply(envelope)}, {"until": NEEDS, "ms": 30000}, {"until": READY, "ms": 30000},
             _send(until="/^Sent/.test(document.querySelector('#ck-drop .drop-receipt').textContent)", ms=20000),
             {"eval": "JSON.stringify({t: 'r', text: document.querySelector('#ck-drop .drop-receipt').textContent, bad: document.querySelector('#ck-drop .drop-receipt').classList.contains('bad')})"}]
    with rig.serving():
        out = drive_touch(tmp_path, f"{rig.origin}/#write_token={TOK}", steps)
    r = _tagged(out)["r"]
    assert says in r["text"] and "words only" not in r["text"] and r["bad"] is bad, r


@needs_browser
@needs_ssh
def test_the_sheets_reload_message_reads_the_drops_again_not_from_the_cache(tmp_path: Path, monkeypatch) -> None:
    rig = FullRig(tmp_path, monkeypatch)
    count = ("(() => { const f = window.fetch; window.__gets = 0; window.fetch = function (u, o) { const url = typeof u === 'string' ? u : (u && u.url) || '';"
             " if (url.indexOf('/cockpit/coord/drops.json') >= 0) window.__gets++; return f.apply(this, arguments); }; })();")
    msg = ("(() => { const fr = document.querySelector('#levain-sign-sheet-frame, iframe[title=\"coordinator drop\"]'); window.__before = window.__gets;"
           " window.dispatchEvent(new MessageEvent('message', {data: {levainSheet: 'reload'}, origin: location.origin, source: fr.contentWindow})); return 1; })()")
    steps = [{"emulate": {"w": 1280, "h": 900, "dpr": 1}}, {"init": count}, {"until": NEEDS, "ms": 30000}, {"until": READY, "ms": 30000},
             {"eval": "window.LevainSignSheet.openDrop(''); 1"}, {"eval": msg}, {"eval": "new Promise(r => setTimeout(r, 700)).then(() => 1)"},
             {"eval": "JSON.stringify({t: 'g', before: window.__before, after: window.__gets})"}]
    with rig.serving():
        out = drive_touch(tmp_path, f"{rig.origin}/#write_token={TOK}", steps)
    g = _tagged(out)["g"]
    assert g.get("after", 0) > g.get("before", 99), (g, out["values"][-4:])


NO_FIELD_SIZING = ("document.addEventListener('DOMContentLoaded', () => { const s = document.createElement('style');"
                   " s.textContent = 'textarea { field-sizing: fixed !important; }'; document.head.appendChild(s); });")
LONG = "\n".join(f"line {i} of the words kept from an earlier visit" for i in range(40))


@needs_browser
@needs_ssh
def test_restored_words_grow_the_box_where_the_browser_has_no_field_sizing(tmp_path: Path, monkeypatch) -> None:
    rig = FullRig(tmp_path, monkeypatch)
    rec = dict(SIGNED, words=LONG)
    steps = [{"emulate": {"w": 1280, "h": 900, "dpr": 1}}, {"init": _seed(rec) + NO_FIELD_SIZING}, {"until": NEEDS, "ms": 30000}, {"until": READY, "ms": 30000},
             {"eval": f"JSON.stringify({{t: 'h', style: {BOX}.style.height, fits: {BOX}.offsetHeight >= {BOX}.scrollHeight}})"}]
    with rig.serving():
        out = drive_touch(tmp_path, f"{rig.origin}/#write_token={TOK}", steps)
    h = _tagged(out)["h"]
    assert h["style"].endswith("px") and h["fits"], h


@needs_browser
@needs_ssh
def test_words_handed_to_the_sheet_grow_its_box_where_the_browser_has_no_field_sizing(tmp_path: Path, monkeypatch) -> None:
    rig = FullRig(tmp_path, monkeypatch)
    frame = "document.querySelector('#levain-sign-sheet-frame, iframe[title=\"coordinator drop\"]')"
    box = f"({frame} && {frame}.contentDocument && {frame}.contentDocument.getElementById('drop-words'))"
    steps = [{"emulate": {"w": 1280, "h": 900, "dpr": 1}}, {"init": NO_FIELD_SIZING}, {"until": NEEDS, "ms": 30000}, {"until": READY, "ms": 30000},
             {"eval": f"window.LevainSignSheet.openDrop({json.dumps(LONG)}); 1"}, {"until": f"{box} && {box}.value.length > 100", "ms": 30000},
             {"eval": f"JSON.stringify({{t: 'h', style: {box}.style.height, fits: {box}.offsetHeight >= {box}.scrollHeight}})"}]
    with rig.serving():
        out = drive_touch(tmp_path, f"{rig.origin}/#write_token={TOK}", steps)
    h = _tagged(out)["h"]
    assert h["style"].endswith("px") and h["fits"], h


@needs_browser
@needs_ssh
def test_the_drop_sheet_asks_about_a_pending_request_kept_under_its_own_outbox_id(tmp_path: Path, monkeypatch) -> None:
    rig = FullRig(tmp_path, monkeypatch)
    plain = {"words": "kept by the hero", "outbox_id": "ph-" + "cd" * 16, "sent_at": "2026-10-30T12:00:00.123000Z", "store_id": None}
    steps = [{"emulate": {"w": 1280, "h": 900, "dpr": 1}}, {"until": NEEDS, "ms": 30000}, {"until": READY, "ms": 30000},
             {"eval": _seed(plain).replace("localStorage", "window.localStorage") + " window.LevainSignSheet.openDrop(''); 1"}, {"posts": 1, "ms": 30000}]
    with rig.serving():
        out = _drive(tmp_path, f"{rig.origin}/#write_token={TOK}", steps)
    assert [p["outbox_id"] for p in _posted(out)] == [plain["outbox_id"]], out["posts"]


def test_the_failure_lines_of_the_hero_take_the_theme_danger_colour_not_the_text_colour() -> None:
    css = (Path(__file__).resolve().parent.parent / "levain" / "templates" / "web" / "dashboard.css").read_text()
    for sel in ("body.ke .ck-drop .drop-note.bad", "body.ke .ck-drop .drop-receipt.bad"):
        rule = css[css.index(sel):].split("}", 1)[0]
        assert "color: var(--bad)" in rule, rule


def test_the_hero_commits_its_state_before_it_registers_the_keydown_listener() -> None:
    core = (Path(__file__).resolve().parent.parent / "levain" / "templates" / "web" / "dashboard_core.js").read_text()
    build = core[core.index("function dropHeroBuild(host)"):core.index("function dropHeroDraw(host, r)")]
    assert build.index("host._drop = ui;") < build.index('host.addEventListener("keydown"'), "a failed build must leave no listener behind"


# ---- L3 r3 (1030-42): one malformed kept entry, a bare+:id pair, a read that fails after a lost reply ------------------------------------

OLDER = dict(SIGNED, words="older kept", outbox_id="ph-" + "cd" * 16, sent_at="2026-10-29T12:00:00.123000Z", envelope=None)
BAD_FIRST = f"localStorage.setItem({PEND} + ':ph-bad', '{{not json');"
BARE_TWIN = f"localStorage.setItem({PEND}, {json.dumps(json.dumps(OLDER))});"      # the legacy bare key holding the same request as its :id key


@needs_browser
@needs_ssh
def test_one_malformed_kept_entry_hides_neither_valid_pending_drop_and_a_bare_twin_counts_once(tmp_path: Path, monkeypatch) -> None:
    rig = FullRig(tmp_path, monkeypatch)
    init = BAD_FIRST + _seed(OLDER) + _seed(SIGNED) + BARE_TWIN
    steps = [{"emulate": {"w": 1280, "h": 900, "dpr": 1}}, {"init": init}, {"until": NEEDS, "ms": 30000}, {"until": READY, "ms": 30000}, {"eval": KEYS}]
    with rig.serving():
        out = drive_touch(tmp_path, f"{rig.origin}/#write_token={TOK}", steps)
    k = _tagged(out)["keys"]
    assert k["box"] == "signed earlier", k                           # the newest valid one is back in the box
    assert "1 earlier drop not confirmed." in k["earlier"], k        # the other valid one, counted once: the bare twin is the same request
    assert any(x.endswith(":ph-bad") for x in k["keys"]), k          # the malformed value is skipped, not deleted


@needs_browser
@needs_ssh
def test_the_sheet_restores_a_valid_kept_drop_past_a_malformed_entry(tmp_path: Path, monkeypatch) -> None:
    rig = FullRig(tmp_path, monkeypatch)
    frame = "document.querySelector('#levain-sign-sheet-frame, iframe[title=\"coordinator drop\"]')"
    box = f"({frame} && {frame}.contentDocument && {frame}.contentDocument.getElementById('drop-words'))"
    steps = [{"emulate": {"w": 1280, "h": 900, "dpr": 1}}, {"init": BAD_FIRST + _seed(OLDER)}, {"until": NEEDS, "ms": 30000}, {"until": READY, "ms": 30000},
             {"eval": "window.LevainSignSheet.openDrop(''); 1"}, {"until": f"{box} && {box}.value === 'older kept'", "ms": 30000},
             {"eval": "JSON.stringify({t: 'ok', v: 1})"}]
    with rig.serving():
        out = drive_touch(tmp_path, f"{rig.origin}/#write_token={TOK}", steps)
    assert _tagged(out)["ok"]["v"] == 1


@needs_browser
@needs_ssh
def test_a_read_that_fails_after_a_lost_reply_does_not_leave_send_dead(tmp_path: Path, monkeypatch) -> None:
    rig = FullRig(tmp_path, monkeypatch)
    # the cockpit transport throws on its next headers() call once the lost reply has been seen: the read the failed send starts fails
    arm = ("(() => { const t = window.LevainCockpitTransport; const h = t.headers.bind(t); let armed = 0; const f = window.fetch;"
           " window.fetch = function (u, o) { const url = typeof u === 'string' ? u : (u && u.url) || '';"
           " if (o && o.method === 'POST' && url.indexOf('/cockpit/coord/drop') >= 0 && !armed) { armed = 1; return f.apply(this, arguments).then(() => { throw new TypeError('reply lost'); }); }"
           " return f.apply(this, arguments); };"
           " t.headers = function () { if (armed === 1) { armed = 2; throw new Error('read failed'); } return h(); }; })();")
    steps = [{"emulate": {"w": 1280, "h": 900, "dpr": 1}}, {"until": NEEDS, "ms": 30000}, {"until": READY, "ms": 30000}, {"eval": arm + " 1"},
             *_sent("land c-15", posts=1),
             {"until": RECEIPT_DONE + " && /^Not sure/.test(document.querySelector('#ck-drop .drop-receipt').textContent)", "ms": 20000},
             {"eval": "new Promise(r => setTimeout(r, 600)).then(() => 1)"}, _send(posts=2)]
    with rig.serving():
        out = drive_touch(tmp_path, f"{rig.origin}/#write_token={TOK}", steps)
    assert len(_posted(out)) == 2, out["posts"]

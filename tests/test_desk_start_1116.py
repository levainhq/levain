"""levain#78 and levain#107: the desk button lives on the Desk view, not on the Room page. With no desk holding the lease it reads
"New Desk" (levain#78, which started life as "Start desk"); with one live it reads "Rotate Desk" (levain#107; its asks and its Take over are tests/test_desk_rotate_107b.py); when the lease cannot
be told it is not drawn.

The Desk view (desk_tab_view.js, the lease bar under its title) is the reference; the Room's header keeps only New. The words New Desk
hands the cockpit's own signing sheet are unchanged ("/fanin broad", the relay reads the first word and an optional shape):
the page starts nothing, the passkey tap in the sheet is the signature. Driven on the real cockpit page behind the desk stub of
tests/test_desk_pane_page.py. LEVAIN_DESK_SHOTS=<dir> saves the screenshots the row asks for.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from tests.test_cockpit_flip_parity import _drive, needs_browser
from tests.test_cockpit_k2a import TOKEN, _serve
from tests.test_cockpit_room_join import _rig
from tests.test_cockpit_threads_mount import qdir  # noqa: F401  (fixture)
from tests.test_room_fulltab_1106 import _ENTER
from tests.test_desk_pane_page import _OPEN, _TAP_DESK, _TOKENED, WEB, desk_page, put, until, wired

PHONE = {"emulate": {"w": 390, "h": 844, "dpr": 3, "mobile": True}}
DESKTOP = {"emulate": {"w": 1280, "h": 900, "dpr": 1}}
NONE = {"state": "none"}


def _shot(name: str) -> list[dict]:
    d = os.environ.get("LEVAIN_DESK_SHOTS")
    if not d:
        return []
    Path(d).mkdir(parents=True, exist_ok=True)
    return [{"shot": str(Path(d) / name), "full": False}]


# what the page draws in the lease bar and what the Start control did; the sheet's opener is stubbed only where a test names it
_LOOK = """(() => { const z = document.querySelector('.dv-zone'), q = s => z.querySelector(s), r = n => { const b = n.getBoundingClientRect(); return {l: b.left, t: b.top, w: b.width, h: b.height, r: b.right}; };
  const s = q('.dv-start'), bar = q('.dv-state'), sheet = document.getElementById('levain-sign-sheet');
  return JSON.stringify({ start: !!s, startText: s && s.textContent, startBox: s && r(s), bar: bar ? bar.innerText : null, barCls: bar ? bar.className : null,
    startCount: z.querySelectorAll('.dv-start').length, roomDesk: document.querySelectorAll('.rv-desk').length, drops: window.__drops || null,
    note: q('.dv-startnote') ? q('.dv-startnote').textContent : null, sheet: !!sheet, frame: sheet && sheet.querySelector('iframe') ? sheet.querySelector('iframe').getAttribute('src') : null,
    pageX: document.documentElement.scrollWidth > innerWidth + 1, vw: innerWidth }); })()"""
LOOK = {"eval": _LOOK}
_STUB = "(() => { window.__drops = []; const S = window.LevainSignSheet; S.openDrop = (w) => { window.__drops.push(w); return %s; }; return 1; })()"


def looks(out) -> list[dict]:
    return [json.loads(v) for v in out["values"] if isinstance(v, str) and v.startswith("{") and '"startCount"' in v]


def _drive_desk(tmp_path, emulate, steps):
    with desk_page(tmp_path) as (stub, base):
        out = _drive(tmp_path, base + "/", [emulate, *_TOKENED, _TAP_DESK, _OPEN, *steps])
        out["sends"] = list(stub.sends)
    return out


@needs_browser
def test_the_desk_button_reads_new_desk_with_none_live_and_rotate_desk_with_one_live(tmp_path):
    out = _drive_desk(tmp_path, PHONE, [
        put(get={"status": 200, "json": wired(lease=NONE)}), until("document.querySelector('.dv-lease-none')"), LOOK, *_shot("desk_none_390.png"),
        put(get={"status": 200, "json": wired()}), until("document.querySelector('.dv-lease-held')"), LOOK, *_shot("desk_held_390.png"),
        put(get={"status": 200, "json": wired(lease={"state": "unknown"})}), until("document.querySelector('.dv-lease-unknown')"), LOOK])
    none, held, unknown = looks(out)
    assert none["start"] and none["startText"] == "New Desk" and none["startCount"] == 1, none
    assert none["startBox"]["w"] >= 44 and none["startBox"]["h"] >= 44 and none["startBox"]["r"] <= none["vw"] + 0.5 and not none["pageX"], none
    assert held["start"] and held["startText"] == "Rotate Desk" and held["startCount"] == 1, held
    assert held["startBox"]["w"] >= 44 and held["startBox"]["h"] >= 44 and held["startBox"]["r"] <= held["vw"] + 0.5 and not held["pageX"], held
    assert not unknown["start"], unknown                                       # the lease cannot be told: neither a new desk nor a rotation


@needs_browser
def test_new_desk_hands_the_signing_sheet_the_fanin_words_and_starts_nothing_itself(tmp_path):
    out = _drive_desk(tmp_path, DESKTOP, [
        put(get={"status": 200, "json": wired(lease=NONE)}), until("document.querySelector('.dv-start')"),
        {"eval": _STUB % "{}"}, {"tap": "document.querySelector('.dv-zone .dv-start')"},
        {"eval": "new Promise(r => setTimeout(r, 300)).then(() => 1)"}, LOOK, *_shot("desk_none_1280.png")])
    g = looks(out)[-1]
    assert g["drops"] == ["/fanin broad"], g
    # the sheet also carries "Send words only", which wakes the desk with no limits: the line says both, never "only a signature starts it"
    assert "Sign it there" in g["note"] and "Send words only" in g["note"] and "nothing is sent until you press one of them" in g["note"] and "ask the host to start a desk" in g["note"], g
    assert g["startText"] == "New Desk", g
    assert out["sends"] == []         # the tap filed nothing through the desk port


@needs_browser
def test_rotate_desk_hands_the_signing_sheet_the_same_words_and_rotates_nothing_itself(tmp_path):
    out = _drive_desk(tmp_path, DESKTOP, [
        put(get={"status": 200, "json": wired()}), until("document.querySelector('.dv-lease-held .dv-start')"),
        {"eval": _STUB % "{}"}, {"tap": "document.querySelector('.dv-zone .dv-start')"},
        {"eval": "new Promise(r => setTimeout(r, 300)).then(() => 1)"}, LOOK, *_shot("desk_held_1280.png")])
    g = looks(out)[-1]
    assert g["startText"] == "Rotate Desk" and len(g["drops"]) == 1 and g["drops"][0].startswith("/rotate desk 25 "), g      # the rotate request naming the desk, not /fanin
    assert "Only the signature asks" in g["note"] and "Send words only" in g["note"] and "not counted as asked here" in g["note"], g      # Send words only is not an ask
    assert "nothing is asked until you press one of them" not in g["note"], g
    assert "rotates nothing" in g["note"], g                                   # the page says what it did not do
    assert out["sends"] == []                                                  # the tap filed nothing through the desk port


@needs_browser
def test_a_refused_open_on_rotate_says_nothing_was_rotated(tmp_path):
    out = _drive_desk(tmp_path, DESKTOP, [
        put(get={"status": 200, "json": wired()}), until("document.querySelector('.dv-lease-held .dv-start')"),
        {"eval": _STUB % "null"}, {"tap": "document.querySelector('.dv-zone .dv-start')"}, {"eval": "new Promise(r => setTimeout(r, 300)).then(() => 1)"}, LOOK])
    g = looks(out)[-1]
    assert len(g["drops"]) == 1 and g["drops"][0].startswith("/rotate ") and g["note"] == "The signing sheet did not open; no rotate request was sent from this button.", g


@needs_browser
def test_a_note_said_for_one_state_goes_when_the_lease_moves_to_the_other(tmp_path):
    out = _drive_desk(tmp_path, DESKTOP, [
        put(get={"status": 200, "json": wired()}), until("document.querySelector('.dv-lease-held .dv-start')"),
        {"eval": _STUB % "{}"}, {"tap": "document.querySelector('.dv-zone .dv-start')"}, {"eval": "new Promise(r => setTimeout(r, 200)).then(() => 1)"}, LOOK,
        put(get={"status": 200, "json": wired(lease=NONE)}), until("document.querySelector('.dv-lease-none .dv-start') && !document.querySelector('.dv-startnote')"), LOOK])
    said, gone = looks(out)
    assert said["note"] and said["startText"] == "Rotate Desk", said
    assert gone["note"] is None and gone["startText"] == "New Desk", gone


@needs_browser
def test_the_desk_button_opens_the_real_drop_sheet(tmp_path):
    out = _drive_desk(tmp_path, DESKTOP, [
        put(get={"status": 200, "json": wired(lease=NONE)}), until("document.querySelector('.dv-start')"),
        {"tap": "document.querySelector('.dv-zone .dv-start')"}, {"eval": "new Promise(r => setTimeout(r, 400)).then(() => 1)"}, LOOK])
    g = looks(out)[-1]
    assert g["sheet"] and g["frame"] == "/sign/drop", g                      # the cockpit's own signed-drop path, not a second one


@needs_browser
def test_a_refused_open_says_no_desk_was_started(tmp_path):
    out = _drive_desk(tmp_path, DESKTOP, [
        put(get={"status": 200, "json": wired(lease=NONE)}), until("document.querySelector('.dv-start')"),
        {"eval": _STUB % "null"}, {"tap": "document.querySelector('.dv-zone .dv-start')"}, {"eval": "new Promise(r => setTimeout(r, 300)).then(() => 1)"}, LOOK])
    g = looks(out)[-1]
    assert g["drops"] == ["/fanin broad"] and g["note"] == "The signing sheet did not open; no start request was sent from this button.", g


@needs_browser
def test_the_new_desk_note_goes_when_a_desk_takes_the_lease(tmp_path):
    out = _drive_desk(tmp_path, DESKTOP, [
        put(get={"status": 200, "json": wired(lease=NONE)}), until("document.querySelector('.dv-start')"),
        {"eval": _STUB % "{}"}, {"tap": "document.querySelector('.dv-zone .dv-start')"}, {"eval": "new Promise(r => setTimeout(r, 200)).then(() => 1)"}, LOOK,
        put(get={"status": 200, "json": wired()}), until("document.querySelector('.dv-lease-held') && !document.querySelector('.dv-startnote')"), LOOK])
    said, live = looks(out)
    assert said["note"], said
    assert live["note"] is None and live["startText"] == "Rotate Desk", live


# --- the Room page no longer has the control --------------------------------------------------------------------------------------

def test_the_room_page_draws_no_desk_control_and_holds_no_copy_of_its_words():
    room = (WEB / "room_view.js").read_text()
    css = (WEB / "dashboard.css").read_text()
    assert "rv-desk" not in room and "Start desk" not in room and "New Desk" not in room and "Rotate Desk" not in room
    assert "/fanin broad" not in room and "openDrop" not in room
    assert ".rv-desk" not in css
    desk = (WEB / "desk_tab_view.js").read_text()
    assert desk.count("/fanin broad") == 1 and "openDrop" in desk


@needs_browser
def test_the_room_page_has_no_desk_button_in_a_real_page(tmp_path, qdir):
    rs, _launcher, src = _rig(tmp_path, qdir)
    with _serve(src, write_token=TOKEN, room_serve=rs) as (base, _h):
        out = _drive(tmp_path, base + "/", [DESKTOP] + list(_ENTER) + [
            {"until": "document.querySelector('.rv-top .rv-rotate')"}, {"eval": "JSON.stringify({desk: document.querySelectorAll('.rv-desk').length, "
             "top: [...document.querySelectorAll('.rv-top button')].map(b => b.textContent)})"}, *_shot("room_after_1280.png")])
    g = json.loads(next(v for v in out["values"] if isinstance(v, str) and v.startswith("{") and '"top"' in v))
    assert g == {"desk": 0, "top": ["New"]}, g

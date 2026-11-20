"""levain#78 / levain#154: the one Open a Desk button lives on the Desk view, drawn only where the host's lease read says open_desk, never on the Room page.

The Desk view (desk_tab_view.js, the lease bar under its title) is the reference; the Room's header keeps only New. The words the
control hands the cockpit's own signing sheet are unchanged ("/fanin broad", the relay reads the first word and an optional shape):
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
NONE = {"state": "none", "open_desk": True}
STALLED = {"state": "held", "holder": "desk 25", "age_s": 1200, "open_desk": True}


def _shot(name: str) -> list[dict]:
    d = os.environ.get("LEVAIN_DESK_SHOTS")
    if not d:
        return []
    Path(d).mkdir(parents=True, exist_ok=True)
    return [{"shot": str(Path(d) / name), "full": False}]


# what the page draws in the lease bar and what the Start control did; the sheet's opener is stubbed only where a test names it
_LOOK = """(() => { const z = document.querySelector('.dv-zone'), q = s => z.querySelector(s), r = n => { const b = n.getBoundingClientRect(); return {l: b.left, t: b.top, w: b.width, h: b.height, r: b.right}; };
  const s = q('.dv-open'), bar = q('.dv-state'), sheet = document.getElementById('levain-sign-sheet');
  return JSON.stringify({ start: !!s, openText: s && s.textContent, openBox: s && r(s), bar: bar ? bar.innerText : null, barCls: bar ? bar.className : null,
    openCount: z.querySelectorAll('.dv-open').length, buttons: [...z.querySelectorAll('.dv-state button')].map(b => b.textContent), roomDesk: document.querySelectorAll('.rv-desk').length, drops: window.__drops || null,
    note: q('.dv-startnote') ? q('.dv-startnote').textContent : null, sheet: !!sheet, frame: sheet && sheet.querySelector('iframe') ? sheet.querySelector('iframe').getAttribute('src') : null,
    pageX: document.documentElement.scrollWidth > innerWidth + 1, vw: innerWidth }); })()"""
LOOK = {"eval": _LOOK}
_STUB = "(() => { window.__drops = []; const S = window.LevainSignSheet; S.openDrop = (w) => { window.__drops.push(w); return %s; }; return 1; })()"


def looks(out) -> list[dict]:
    return [json.loads(v) for v in out["values"] if isinstance(v, str) and v.startswith("{") and '"openCount"' in v]


def _drive_desk(tmp_path, emulate, steps):
    with desk_page(tmp_path) as (stub, base):
        out = _drive(tmp_path, base + "/", [emulate, *_TOKENED, _TAP_DESK, _OPEN, *steps])
        out["sends"] = list(stub.sends)
    return out


@needs_browser
def test_the_desk_view_offers_open_a_desk_only_where_the_lease_read_says_open_desk(tmp_path):
    out = _drive_desk(tmp_path, PHONE, [
        put(get={"status": 200, "json": wired(lease=NONE)}), until("document.querySelector('.dv-lease-none')"), LOOK, *_shot("desk_none_390.png"),
        put(get={"status": 200, "json": wired()}), until("document.querySelector('.dv-lease-held')"), LOOK,
        put(get={"status": 200, "json": wired(lease={"state": "unknown"})}), until("document.querySelector('.dv-lease-unknown')"), LOOK])
    none, held, unknown = looks(out)
    assert none["start"] and none["openText"] == "Open a Desk" and none["openCount"] == 1, none
    assert none["openBox"]["w"] >= 44 and none["openBox"]["h"] >= 44 and none["openBox"]["r"] <= none["vw"] + 0.5 and not none["pageX"], none
    assert not held["start"] and not unknown["start"], (held, unknown)       # a desk is live, or the lease cannot be told: nothing to open


@needs_browser
def test_open_a_desk_hands_the_signing_sheet_the_fanin_words_and_starts_nothing_itself(tmp_path):
    out = _drive_desk(tmp_path, DESKTOP, [
        put(get={"status": 200, "json": wired(lease=NONE)}), until("document.querySelector('.dv-open')"),
        {"eval": _STUB % "{}"}, {"tap": "document.querySelector('.dv-zone .dv-open')"},
        {"eval": "new Promise(r => setTimeout(r, 300)).then(() => 1)"}, LOOK, *_shot("desk_none_1280.png")])
    g = looks(out)[-1]
    assert g["drops"] == ["/fanin broad"], g
    # the sheet also carries "Send words only", which wakes the desk with no limits: the line says both, never "only a signature starts it"
    assert "Sign it there" in g["note"] and "Send words only" in g["note"] and "nothing starts until you press one of them" in g["note"], g
    assert out["sends"] == []         # the tap filed nothing through the desk port


@needs_browser
def test_open_a_desk_opens_the_real_drop_sheet(tmp_path):
    out = _drive_desk(tmp_path, DESKTOP, [
        put(get={"status": 200, "json": wired(lease=NONE)}), until("document.querySelector('.dv-open')"),
        {"tap": "document.querySelector('.dv-zone .dv-open')"}, {"eval": "new Promise(r => setTimeout(r, 400)).then(() => 1)"}, LOOK])
    g = looks(out)[-1]
    assert g["sheet"] and g["frame"] == "/sign/drop", g                      # the cockpit's own signed-drop path, not a second one


@needs_browser
def test_a_refused_open_says_no_desk_was_started(tmp_path):
    out = _drive_desk(tmp_path, DESKTOP, [
        put(get={"status": 200, "json": wired(lease=NONE)}), until("document.querySelector('.dv-open')"),
        {"eval": _STUB % "null"}, {"tap": "document.querySelector('.dv-zone .dv-open')"}, {"eval": "new Promise(r => setTimeout(r, 300)).then(() => 1)"}, LOOK])
    g = looks(out)[-1]
    assert g["drops"] == ["/fanin broad"] and g["note"] == "The signing sheet did not open, so no desk was started.", g


@needs_browser
def test_the_open_note_goes_when_a_desk_takes_the_lease(tmp_path):
    out = _drive_desk(tmp_path, DESKTOP, [
        put(get={"status": 200, "json": wired(lease=NONE)}), until("document.querySelector('.dv-open')"),
        {"eval": _STUB % "{}"}, {"tap": "document.querySelector('.dv-zone .dv-open')"}, {"eval": "new Promise(r => setTimeout(r, 200)).then(() => 1)"}, LOOK,
        put(get={"status": 200, "json": wired()}), until("document.querySelector('.dv-lease-held') && !document.querySelector('.dv-startnote')"), LOOK])
    said, live = looks(out)
    assert said["note"], said
    assert live["note"] is None and not live["start"], live


# --- the Room page no longer has the control --------------------------------------------------------------------------------------

def test_the_room_page_draws_no_start_desk_control_and_holds_no_copy_of_its_words():
    room = (WEB / "room_view.js").read_text()
    css = (WEB / "dashboard.css").read_text()
    assert "rv-desk" not in room and "Open a Desk" not in room and "/fanin broad" not in room and "openDrop" not in room
    assert ".rv-desk" not in css
    desk = (WEB / "desk_tab_view.js").read_text()
    assert desk.count("/fanin broad") == 1 and "openDrop" in desk


@needs_browser
def test_the_room_page_has_no_start_desk_button_in_a_real_page(tmp_path, qdir):
    rs, _launcher, src = _rig(tmp_path, qdir)
    with _serve(src, write_token=TOKEN, room_serve=rs) as (base, _h):
        out = _drive(tmp_path, base + "/", [DESKTOP] + list(_ENTER) + [
            {"until": "document.querySelector('.rv-top .rv-rotate')"}, {"eval": "JSON.stringify({desk: document.querySelectorAll('.rv-desk').length, "
             "top: [...document.querySelectorAll('.rv-top button')].map(b => b.textContent)})"}, *_shot("room_after_1280.png")])
    g = json.loads(next(v for v in out["values"] if isinstance(v, str) and v.startswith("{") and '"top"' in v))
    assert g == {"desk": 0, "top": ["New"]}, g


# --- the show/hide rule: the page holds no copy of it; each answer of the host's relay is drawn as given ------------------------------

def _open_looks(tmp_path, leases):
    steps = []
    for lease in leases:
        steps += [put(get={"status": 200, "json": wired(lease=lease)}), until("document.querySelector('.dv-state')"),
                  {"eval": "new Promise(r => setTimeout(r, 250)).then(() => 1)"}, LOOK]
    return looks(_drive_desk(tmp_path, DESKTOP, steps))


@needs_browser
def test_each_open_desk_basis_is_drawn_as_the_host_said_it(tmp_path):
    # no_lease and night_after_0400 read as a none or held lease with open_desk true; stalled is a held lease with it true; a held
    # lease the relay does not open (basis None), a none lease it cannot compute, and an unknown lease never draw the button
    shown = _open_looks(tmp_path, [NONE, STALLED, {"state": "held", "holder": "desk 40", "age_s": 60, "open_desk": False},
                                   {"state": "none", "open_desk": False}, {"state": "unknown", "open_desk": True},
                                   {"state": "held", "holder": "desk 40", "age_s": 60, "open_desk": "true"}])
    assert [g["openCount"] for g in shown] == [1, 1, 0, 0, 0, 0], shown
    assert ", renewed" in shown[1]["bar"] and ". The host says a new desk can open now." in shown[1]["bar"] and "The host says" not in shown[2]["bar"], shown   # a held lease beside the button says why


@needs_browser
def test_no_rotate_or_second_desk_button_is_drawn_in_any_lease_state(tmp_path):
    shown = _open_looks(tmp_path, [NONE, STALLED, {"state": "held", "holder": "desk 40", "age_s": 60}])
    for g in shown:
        assert g["buttons"].count("Open a Desk") == g["openCount"], g
        assert not any("otate" in t or "ake over" in t or "tart desk" in t for t in g["buttons"]), g


def test_the_desk_page_source_holds_no_rotate_path_and_no_clock_rule():
    desk = (WEB / "desk_tab_view.js").read_text()
    for gone in ("Rotate Desk", "ROTATE_", "STALL_AGE_S", "ROTATE_WAIT_MS", "/rotate", "Take over", "Start desk", "\"dv-start\""):
        assert gone not in desk, gone
    assert desk.count("lease.open_desk") == 3          # the reads of the host's field, all in leaseModel (held, its hint, none); draw() reads only the model

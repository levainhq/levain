"""Row 1704 fix round 4 (codex HIGH + deepseek HIGH on fix 3, L3 r4): the cockpit shows the sign sheet as a same-origin
IFRAME inside its own veil. A node of the COCKPIT page painted over that frame (a fixed toast, a bar) is invisible to the
frame's long-input gate: its IntersectionObserver ignores paint and its hit test sees only the frame's own document, so
the chunks under the cover counted as shown and Allow could open over text never on screen.

The reviewers' fix, a hit test in the parent through ``frameElement``, was measured dead in Chrome first: the parent makes
every node added to its body while the sheet is open ``inert``, and an inert node hit-tests as ``pointer-events: none``,
so the parent's own ``document.elementFromPoint`` at the frame's centre returned the IFRAME with an opaque, inert cover
painted over it (and the cover itself once it was not inert). The frame here deletes the class instead: the veil is a
``<dialog>`` opened with ``showModal()``, in the browser's top layer, so nothing of the page can paint above the frame
whatever its z-index; and the frame's gate counts nothing unless its frame sits in the page's one ``:modal`` dialog, so a
browser without ``showModal`` (the old div veil) keeps a long input's Allow shut while Deny stays open.

Reproduced red on fix 3 (352188251): with a ``position: fixed; inset: 0; z-index: 2147483647`` cover appended to the
cockpit's body (not inert), the cockpit's ``elementFromPoint`` at the frame's centre was the COVER, and the screenshot's
pixel there was the cover's red."""
from __future__ import annotations

import json
import re
import struct
import zlib
from pathlib import Path

from tests.test_cockpit_consent_sheet_page import FRAME, IN, _ConsentRig
from tests.test_cockpit_flip_parity import needs_browser
from tests.test_cockpit_sign_card import DESKTOP, TOK, needs_ssh
from tests.test_consent_long_input_coverage_page import FRAMES, _attack, _shots, probed_drive, stepped

WEB = Path(__file__).resolve().parent.parent / "levain" / "templates" / "web"
VEIL = "document.getElementById('levain-sign-sheet')"
COVER_RGB = (204, 51, 51)
# the frame's own long-input block, its Allow and its state, read through the cockpit
F_INPUT = f"{IN}.getElementById('sheet-input')"
F_FULL = f"{F_INPUT}.querySelector('.long-whole > pre.long-full')"
F_ALLOW = f"{IN}.querySelector('#sheet-actions [data-choice=\"allow\"]')"
F_DENY = f"{IN}.querySelector('#sheet-actions [data-choice=\"deny\"]')"
F_STATE = (f"JSON.stringify({{allow: {F_ALLOW} ? {F_ALLOW}.disabled : 'gone', deny: {F_DENY} ? {F_DENY}.disabled : 'gone', "
           f"seen: {F_INPUT}.dataset.seen, "
           f"unseen: Number({F_INPUT}.dataset.unseen), "
           f"veil: {VEIL}.tagName, modal: {VEIL}.matches(':modal')}})")


def _open(rid: str) -> list[dict]:
    """The cockpit, then its own sheet over consent request `rid`, as a Needs card's button opens it."""
    return [
        {"until": "window.LevainSignSheet && typeof window.LevainSignSheet.openConsent === 'function' && 1", "ms": 30000},
        {"eval": f"(window.LevainSignSheet.openConsent({json.dumps(rid)}) ? 1 : 0)"},
        {"until": f"{IN} && {IN}.getElementById('sheet') && {IN}.getElementById('sheet').dataset.state !== 'reading' && "
                  f"{IN}.getElementById('sheet').dataset.state", "ms": 30000},
    ]


# a cover over the whole cockpit page, appended to its body while the sheet is open; the sheet's observer makes it inert
# as it arrives, so `inert` is set after that as the step says. Answers what the cockpit's own hit test finds at the
# frame's centre (the frame, the veil or a node in it; or the cover), and the frame's centre in screen pixels.
def _cover(inert: bool) -> str:
    return (f"(async () => {{ let c = document.getElementById('test-cover'); if (!c) {{ c = document.createElement('div'); "
            f"c.id = 'test-cover'; c.style.cssText = 'position:fixed;inset:0;z-index:2147483647;background:rgb(204,51,51)'; "
            f"document.body.appendChild(c); }} {FRAMES}; c.inert = {'true' if inert else 'false'}; {FRAMES}; "
            f"const fr = {FRAME}, v = {VEIL}, r = fr.getBoundingClientRect(); const x = r.left + r.width / 2, y = r.top + r.height / 2; "
            f"const h = document.elementFromPoint(x, y); "
            f"return JSON.stringify({{hit: h === c ? 'cover' : h === fr ? 'frame' : (v && v.contains(h)) ? 'veil' : (h ? h.tagName : null), "
            f"inert: c.inert, x: x, y: y, iw: innerWidth, veil: v.tagName, modal: v.matches(':modal')}}); }})()")


def _pixel(png: Path, x: float, y: float, css_width: float) -> tuple[int, int, int]:
    """The RGB of a PNG screenshot at CSS point (x, y): 8-bit RGB or RGBA, not interlaced (what Chrome writes)."""
    data = png.read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    pos, idat, w = 8, b"", 0
    while pos < len(data):
        n, kind = struct.unpack(">I4s", data[pos:pos + 8])
        body = data[pos + 8:pos + 8 + n]
        if kind == b"IHDR":
            w, h, depth, ctype, _c, _f, inter = struct.unpack(">IIBBBBB", body)
            assert depth == 8 and ctype in (2, 6) and inter == 0, (depth, ctype, inter)
            bpp = 3 if ctype == 2 else 4
        elif kind == b"IDAT":
            idat += body
        pos += 12 + n
    raw = zlib.decompress(idat)
    stride = w * bpp
    rows: list[bytearray] = []
    prev = bytearray(stride)
    for r in range(h):
        f = raw[r * (stride + 1)]
        line = bytearray(raw[r * (stride + 1) + 1:(r + 1) * (stride + 1)])
        for i in range(stride):
            a = line[i - bpp] if i >= bpp else 0
            b = prev[i]
            c = prev[i - bpp] if i >= bpp else 0
            if f == 1:
                line[i] = (line[i] + a) & 255
            elif f == 2:
                line[i] = (line[i] + b) & 255
            elif f == 3:
                line[i] = (line[i] + (a + b) // 2) & 255
            elif f == 4:
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                line[i] = (line[i] + (a if pa <= pb and pa <= pc else b if pb <= pc else c)) & 255
        rows.append(line)
        prev = line
    s = w / css_width
    px, py = int(x * s), int(y * s)
    o = px * bpp
    return rows[py][o], rows[py][o + 1], rows[py][o + 2]


@needs_browser
@needs_ssh
def test_a_cover_on_the_cockpit_page_never_paints_over_the_sheets_frame(tmp_path, monkeypatch) -> None:
    """The sheet open in the cockpit; a full-page fixed cover at the highest z-index appended to the cockpit's body, once
    not inert and once inert: the cockpit's hit test at the frame's centre finds the frame (or the veil), never the
    cover, and the screenshot's pixel there is not the cover's red. The veil is a modal dialog."""
    rig = _ConsentRig(tmp_path, monkeypatch)
    shots = _shots(tmp_path)
    one, two = shots / "fix4_1_cockpit_cover_not_inert_frame_on_top.png", shots / "fix4_2_cockpit_cover_inert_frame_on_top.png"
    with rig.serving():
        out = probed_drive(tmp_path, f"{rig.origin}/#write_token={TOK}", [
            DESKTOP, rig.authenticator(), *_open(rig.rid),                                              # 0 1 2
            {"eval": _cover(inert=False)},                                                              # 3
            {"shot": str(one)},                                                                         # 4
            {"eval": _cover(inert=True)},                                                               # 5
            {"shot": str(two)},                                                                         # 6
        ])
    v = out["values"]
    assert v[1] == 1, v
    for i, shot in ((3, one), (5, two)):
        c = json.loads(v[i])
        print("COVER", c)
        assert c["veil"] == "DIALOG" and c["modal"] is True, c
        assert c["hit"] in ("frame", "veil"), c                     # never the cover
        px = _pixel(shot, c["x"], c["y"], c["iw"])
        print("PIXEL", shot.name, px)
        assert max(abs(a - b) for a, b in zip(px, COVER_RGB)) > 24, px    # the frame is painted there, not the cover
    assert json.loads(v[3])["inert"] is False and json.loads(v[5])["inert"] is True


@needs_browser
@needs_ssh
def test_in_the_cockpits_sheet_a_long_input_read_through_opens_allow(tmp_path, monkeypatch) -> None:
    """The gate inside the cockpit's top-layer frame still works: opened and stepped through, Allow opens."""
    rig = _ConsentRig(tmp_path, monkeypatch)
    rid = _attack(rig)
    with rig.serving():
        out = probed_drive(tmp_path, f"{rig.origin}/#write_token={TOK}", [
            DESKTOP, rig.authenticator(), *_open(rid),                                                  # 0 1 2
            {"tap": f"{F_INPUT}.querySelector('.long-whole > summary')"},                               # 3
            {"until": f"{F_INPUT}.classList.contains('open') && 1", "ms": 10000},                 # 4
            {"eval": f"(async () => {{ {FRAMES}; {FRAMES}; return {F_STATE}; }})()"},                   # 5
            {"eval": stepped(F_FULL, 0.8, down=True)},                                                  # 6
            {"eval": f"(async () => {{ {FRAMES}; {FRAMES}; return {F_STATE}; }})()"},                   # 7
        ])
    v = out["values"]
    before, after = json.loads(v[5]), json.loads(v[7])
    print("TOP LAYER", before, after)
    # the step's wait read the FRAME's own observers (it is not vacuous here): deliveries came and the gate ran its callbacks
    waited = json.loads(v[6])
    assert waited["io"] > 0 and waited["ran"] > 0 and waited["n"] > 1, waited
    assert before["veil"] == "DIALOG" and before["modal"] is True, before
    assert before["allow"] is True and before["seen"] == "0", before
    assert after["allow"] is False and after["seen"] == "1" and after["unseen"] == 0, after


@needs_browser
@needs_ssh
def test_without_show_modal_the_sheet_opens_and_a_long_input_stays_deny_only(tmp_path, monkeypatch) -> None:
    """A browser with no HTMLDialogElement.showModal: the cockpit draws the old div veil (the sheet still opens), and
    the frame's gate, outside any modal dialog, never counts a chunk shown: read through, Allow stays shut, Deny open."""
    rig = _ConsentRig(tmp_path, monkeypatch)
    rid = _attack(rig)
    with rig.serving():
        out = probed_drive(tmp_path, f"{rig.origin}/#write_token={TOK}", [
            DESKTOP, rig.authenticator(),
            {"until": "window.LevainSignSheet && 1", "ms": 30000},                                     # 0
            {"eval": "(delete HTMLDialogElement.prototype.showModal, typeof HTMLDialogElement.prototype.showModal)"},  # 1
            *_open(rid)[1:],                                                                            # 2 3
            {"tap": f"{F_INPUT}.querySelector('.long-whole > summary')"},                               # 4
            {"until": f"{F_INPUT}.classList.contains('open') && 1", "ms": 10000},                 # 5
            {"eval": stepped(F_FULL, 0.8, down=True, counts=False)},                                                  # 6
            {"eval": f"(async () => {{ {FRAMES}; {FRAMES}; return {F_STATE}; }})()"},                   # 7
        ])
    v = out["values"]
    assert v[1] == "undefined", v
    s = json.loads(v[7])
    print("NO SHOWMODAL", s)
    assert s["veil"] == "DIV" and s["modal"] is False, s
    assert s["allow"] is True and s["deny"] is False, s                       # Allow shut, Deny open
    assert s["seen"] == "0" and s["unseen"] > 16_000, s                       # nothing counted shown


@needs_browser
@needs_ssh
def test_a_show_modal_that_throws_leaves_the_dialog_in_place_and_a_long_input_deny_only(tmp_path, monkeypatch) -> None:
    """showModal exists but throws (a dialog that cannot be made modal): the veil is the dialog shown in place, NOT in the
    top layer, and the frame's gate, whose frame then sits in an open dialog that is not :modal, counts nothing shown."""
    rig = _ConsentRig(tmp_path, monkeypatch)
    rid = _attack(rig)
    with rig.serving():
        out = probed_drive(tmp_path, f"{rig.origin}/#write_token={TOK}", [
            DESKTOP, rig.authenticator(),
            {"until": "window.LevainSignSheet && 1", "ms": 30000},                                     # 0
            {"eval": "(HTMLDialogElement.prototype.showModal = function () { throw new DOMException('no', 'InvalidStateError'); }, 1)"},  # 1
            *_open(rid)[1:],                                                                            # 2 3
            {"tap": f"{F_INPUT}.querySelector('.long-whole > summary')"},                               # 4
            {"until": f"{F_INPUT}.classList.contains('open') && 1", "ms": 10000},                       # 5
            {"eval": stepped(F_FULL, 0.8, down=True, counts=False)},                                                  # 6
            {"eval": f"(async () => {{ {FRAMES}; {FRAMES}; return JSON.stringify({{...JSON.parse({F_STATE}), open: {VEIL}.open}}); }})()"},  # 7
        ])
    s = json.loads(out["values"][7])
    print("THROWS", s)
    assert s["veil"] == "DIALOG" and s["open"] is True and s["modal"] is False, s
    assert s["allow"] is True and s["deny"] is False, s                       # Allow shut, Deny open
    assert s["seen"] == "0" and s["unseen"] > 16_000, s                       # nothing counted shown


@needs_browser
@needs_ssh
def test_another_modal_dialog_on_the_page_does_not_stand_in_for_the_sheets(tmp_path, monkeypatch) -> None:
    """The veil could not be made modal (its showModal throws) while ANOTHER dialog of the page is the one :modal: the
    page has exactly one modal, but not the one the frame sits in, so the frame's gate still counts nothing shown. (The
    other dialog makes the frame inert, so the summary is clicked and the block scrolled by script, not by input.)"""
    rig = _ConsentRig(tmp_path, monkeypatch)
    rid = _attack(rig)
    with rig.serving():
        out = probed_drive(tmp_path, f"{rig.origin}/#write_token={TOK}", [
            DESKTOP, rig.authenticator(),
            {"until": "window.LevainSignSheet && 1", "ms": 30000},                                     # 0
            {"eval": "(() => { const sm = HTMLDialogElement.prototype.showModal; HTMLDialogElement.prototype.showModal = "
                     "function () { if (this.id === 'levain-sign-sheet') throw new DOMException('no', 'InvalidStateError'); "
                     "return sm.call(this); }; return 1; })()"},                                       # 1
            *_open(rid)[1:],                                                                            # 2 3
            {"eval": "(() => { const d = document.createElement('dialog'); d.id = 'test-other'; d.textContent = 'other'; "
                     "document.body.appendChild(d); d.showModal(); return document.querySelectorAll(':modal').length; })()"},  # 4
            {"eval": f"({F_INPUT}.querySelector('.long-whole > summary').click(), 1)"},                 # 5
            {"until": f"{F_INPUT}.classList.contains('open') && 1", "ms": 10000},                       # 6
            {"eval": stepped(F_FULL, 0.8, down=True, counts=False)},                                                  # 7
            {"eval": f"(async () => {{ {FRAMES}; {FRAMES}; return JSON.stringify({{...JSON.parse({F_STATE}), "
                     f"other: document.getElementById('test-other').matches(':modal')}}); }})()"},       # 8
        ])
    v = out["values"]
    s = json.loads(v[8])
    print("OTHER MODAL", v[4], s)
    assert v[4] == 1 and s["other"] is True and s["veil"] == "DIALOG" and s["modal"] is False, (v[4], s)
    assert s["allow"] is True and s["seen"] == "0" and s["unseen"] > 16_000, s   # nothing counted shown


# counts the veil's `cancel` and `close` events and stops the frame from answering a close ask (so the sheet stays for
# the page's 1 s ack timer and what happened to the dialog can be read); focus on the veil's X, in this page
_TRACE = (f"(() => {{ const v = {VEIL}; window.__ev = {{cancel: 0, close: 0}}; "
          f"v.addEventListener('cancel', () => window.__ev.cancel++); v.addEventListener('close', () => window.__ev.close++); "
          f"{FRAME}.contentWindow.postMessage = () => {{}}; v.querySelector('button').focus(); "
          f"return document.activeElement && v.contains(document.activeElement) ? 1 : 0; }})()")
_READ = (f"(() => {{ const v = {VEIL}; return JSON.stringify({{...window.__ev, here: !!v && v.isConnected, "
         f"open: !!v && v.open, modal: !!v && v.matches(':modal')}}); }})()")


@needs_browser
@needs_ssh
def test_escape_a_close_request_and_a_close_never_take_the_sheet_out_of_the_top_layer(tmp_path, monkeypatch) -> None:
    """Escape on a modal dialog is a close request, and Chrome makes its `cancel` uncancelable without a fresh user
    activation (measured in fix 4): the dialog would close and leave the top layer under a ceremony in flight. So (a) an
    Escape at the cockpit fires no cancel and no close: its keydown is prevented; (b) a close request (requestClose, where
    the browser has it) fires a cancel that is prevented, and no close; (c) a close the page did not make (close())
    is undone at once: the dialog is open and modal again. Each asks the sheet to close (the frame here never answers)."""
    rig = _ConsentRig(tmp_path, monkeypatch)
    with rig.serving():
        out = probed_drive(tmp_path, f"{rig.origin}/#write_token={TOK}", [
            DESKTOP, rig.authenticator(), *_open(rig.rid),                                              # 0 1 2
            {"eval": _TRACE},                                                                           # 3
            {"key": "Escape", "vk": 27},
            {"eval": _READ},                                                                            # 4
            {"eval": f"(() => {{ const v = {VEIL}; if (typeof v.requestClose !== 'function') return 'none'; "
                     f"v.requestClose(); return 1; }})()"},                                             # 5
            {"eval": _READ},                                                                            # 6
            {"eval": f"(async () => {{ {VEIL}.close(); {FRAMES}; return 1; }})()"},                     # 7
            {"eval": _READ},                                                                            # 8
        ])
    v = out["values"]
    assert v[3] == 1, v
    esc, req, closed = json.loads(v[4]), json.loads(v[6]), json.loads(v[8])
    print("CLOSE REQUESTS", v[5], esc, req, closed)
    assert esc == {"cancel": 0, "close": 0, "here": True, "open": True, "modal": True}, esc
    if v[5] == 1:
        assert req == {"cancel": 1, "close": 0, "here": True, "open": True, "modal": True}, req
    assert closed["close"] == req["close"] + 1 and closed["here"] and closed["open"] and closed["modal"], closed


HIDDEN = "\u200b\u202e\u2066\u200d\u00ad\ufeff"
# what the frame's long block holds: the most child nodes of any chunk, how many chunks hold an element, how many of the
# hidden characters arrived as themselves (raw), and how many visible marks (the server's "⟨U+200B⟩") the text holds
_KIDS = (f"(() => {{ const sp = [...{F_FULL}.querySelectorAll('.long-group > span')]; const t = {F_FULL}.textContent; "
         f"return JSON.stringify({{chunks: sp.length, most: Math.max(...sp.map(c => c.childNodes.length)), "
         f"elems: sp.filter(c => c.children.length).length, "
         f"raw: [...{json.dumps(HIDDEN)}].filter(c => t.includes(c)).length, "
         f"marked: (t.match(/\\u27e8U\\+[0-9A-F]{{4,6}}\\u27e9/g) || []).length}}); }})()")


@needs_browser
@needs_ssh
def test_a_long_input_with_hidden_characters_reads_through_to_allow(tmp_path, monkeypatch) -> None:
    """(deepseek MED on fix 5, run) A long input holding zero-width, bidi, joiner, soft-hyphen and BOM characters (several
    times, so they fall inside chunks): fix 5's hit test reads a chunk's ONE text node, and a chunk holding a child element
    (a mark drawn as a span) would never latch. It did not reproduce (run 11-13): the server marks each hidden character
    as visible TEXT ("⟨U+200B⟩", 24 marks here, none raw), and longText builds every chunk from text alone (textContent),
    so no chunk holds an element. Pinned: the marks arrive as text, every chunk is one text node, and a read-through in
    the cockpit's top-layer frame opens Allow."""
    from datetime import timedelta

    from tests.test_consent_long_input import _long_command

    rig = _ConsentRig(tmp_path, monkeypatch)
    command = _long_command(16_489)
    for at in (3_000, 3_007, 8_000, 12_345):
        command = command[:at] + HIDDEN + command[at + len(HIDDEN):]
    rid = rig.queue.file(agent="claude_code", agent_session_id="s1", tool_name="Bash", tool_input={"command": command},
                         cwd="/w", hold=timedelta(minutes=30), tier="T2").id
    with rig.serving():
        out = probed_drive(tmp_path, f"{rig.origin}/#write_token={TOK}", [
            DESKTOP, rig.authenticator(), *_open(rid),                                                  # 0 1 2
            {"tap": f"{F_INPUT}.querySelector('.long-whole > summary')"},                               # 3
            {"until": f"{F_INPUT}.classList.contains('open') && 1", "ms": 10000},                       # 4
            {"eval": _KIDS},                                                                            # 5
            {"eval": stepped(F_FULL, 0.8, down=True)},                                                  # 6
            {"eval": f"(async () => {{ {FRAMES}; {FRAMES}; return {F_STATE}; }})()"},                   # 7
        ])
    v = out["values"]
    kids, after = json.loads(v[5]), json.loads(v[7])
    print("HIDDEN", kids, after)
    assert kids["raw"] == 0 and kids["marked"] == 4 * len(HIDDEN), kids    # every hidden character is a visible mark, in text
    assert kids["most"] == 1 and kids["elems"] == 0, kids          # every chunk is one text node
    assert after["allow"] is False and after["seen"] == "1" and after["unseen"] == 0, after


def test_nothing_else_in_the_web_templates_enters_the_top_layer() -> None:
    """The top layer's only residual is another top-layer entry painted above the sheet (a second modal dialog, a popover,
    a fullscreen element). The web templates make none: the only showModal calls are the sign sheet's own two (open, and
    reopen after a close it did not make), both in buildSignSheet."""
    other = re.compile(r"\bshowPopover\b|\btogglePopover\b|\bpopover\s*=|[\"']popover[\"']|equestFullscreen|webkitEnterFullscreen")
    modal = re.compile(r"\.showModal\(")
    found_other, found_modal = [], []
    for p in sorted(WEB.iterdir()):
        if p.suffix not in (".js", ".html", ".mjs"):
            continue
        for n, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
            if other.search(line):
                found_other.append(f"{p.name}:{n}: {line.strip()}")
            if modal.search(line):
                found_modal.append((p.name, n))
    assert found_other == [], found_other
    assert [f for f, _ in found_modal] == ["dashboard_core.js", "dashboard_core.js"], found_modal
    core = (WEB / "dashboard_core.js").read_text(encoding="utf-8").splitlines()
    start = next(i for i, l in enumerate(core, 1) if l.startswith("  function buildSignSheet("))
    end = next(i for i, l in enumerate(core, 1) if i > start and l.startswith("  function askCloseSignSheet("))
    assert all(start < n < end for _, n in found_modal), (start, end, found_modal)

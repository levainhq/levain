"""Row 1704 fix round 3 (codex HIGH on fix 2, L3 r3): fix 2 gated Allow on the UNION of the block's scroll views, kept
per layout key ``scrollHeight x clientWidth``. A typography reflow (a font arriving, letter-spacing, word-spacing) that
moves characters but keeps both integers left the old union in force over different text: characters that were below
the covered part moved up into it, were never on screen in either layout, and still counted as seen.

The gate is now the BROWSER's own report: the long text is laid out in chunks (inline spans, the text unchanged) and an
IntersectionObserver on the viewport says which chunk was ever FULLY inside every clip (the block's scrollport, the
page's viewport, any clipping ancestor or outer frame), with a hit test that nothing in the page painted over it. Seen
means every chunk was so reported at some moment; a reflow cannot move unseen text into a seen state because the set is
of characters, not pixels.

These drives never scroll on their own before an Allow assertion; every scroll is the test's own step."""
from __future__ import annotations

import json
from datetime import timedelta

import pytest

from tests.test_cockpit_consent_sheet_page import _ConsentRig
from tests.test_cockpit_flip_parity import needs_browser
from tests.test_cockpit_sign_card import DESKTOP, TOK, needs_ssh
from tests.test_consent_long_input_coverage_page import (
    C_FULL, CARD, C_STATE, EVIL, FRAMES, FULL, INPUT, LATE_IO_MS, PHONE, SHEET, SUMMARY, _attack, _shots, first_view_counted,
    io_probe, jump, probed_drive, settled,
    stepped)


# The truth, read by the test from the page itself, about which chunks are wholly on screen now: inside the block's
# padding box, inside the viewport, and clear of an overlay's rect when one is given. Answers the chunks the gate holds as
# shown that are not wholly on screen now (`wrong`; a drive that only ever showed this view must have none) and the
# shown count. `box` is the long-text box, `full` its block.
def truth(box: str, full: str, overlay: str = "null") -> str:
    return (f"(() => {{ const b0 = {box}, f = {full}, o = {overlay}; const pr = f.getBoundingClientRect(); "
            f"const top = Math.max(0, pr.top + f.clientTop), bot = Math.min(innerHeight, pr.top + f.clientTop + f.clientHeight); "
            f"const orr = o ? o.getBoundingClientRect() : null; const sp = [...f.querySelectorAll('.long-group > span')]; "
            f"const u = new Set(b0.longUnshown()); let onscreen = 0; const wrong = []; "
            f"sp.forEach((c, i) => {{ const r = c.getBoundingClientRect(); "
            f"const under = !!orr && r.bottom > orr.top && r.top < orr.bottom; "
            f"const whole = r.top >= top - 0.5 && r.bottom <= bot + 0.5 && !under; if (whole) onscreen += 1; "
            f"if (!whole && !u.has(i)) wrong.push(i); }}); "
            f"return JSON.stringify({{chunks: sp.length, shown: sp.length - u.size, onscreen: onscreen, wrong: wrong.slice(0, 20), "
            f"nwrong: wrong.length, block: [pr.top, pr.bottom], vh: innerHeight, "
            f"overlay: orr ? [orr.top, orr.bottom] : null, state: JSON.parse({SHEET})}}); }})()")

# a paragraph that wraps (letter-spacing changes how many lines it takes) above many short lines that never wrap (their
# lines stay put): tightening the paragraph moves every short line UP, and padding added at the bottom keeps the
# block's scrollHeight and clientWidth exactly as they were (fix 2's layout key)
WORDS = ("alpha bravo charlie delta echo foxtrot golf hotel india juliet kilo lima mike november oscar papa quebec "
         "romeo sierra tango uniform victor whiskey xray yankee zulu ")


def _typography_command() -> str:
    para = (WORDS * 60)[:5_000].rstrip()
    rows = "".join(f"row {i:04d} short line\n" for i in range(600))
    return "cat <<'EOF' > /w/notes.txt\n" + para + "\n" + rows + "EOF\nsh /w/notes.txt # the decisive end"


# a typography reflow that keeps fix 2's key: letter-spacing -0.4px on the block (the text rewraps shorter), then an
# empty block (no text: the block's text is unchanged) at its end grown until scrollHeight is what it was. Answers the
# keys before and after, the view's height, and how far the first short line moved up, measured from its painted box.
def _reflow(full: str) -> str:
    return (f"(async () => {{ const f = {full}; const s = f.textContent; "
            f"const r = document.createRange(); "
            f"const y = () => {{ let rest = s.indexOf('row 0000'); const w = document.createTreeWalker(f, NodeFilter.SHOW_TEXT); "
            f"for (let n = w.nextNode(); n; n = w.nextNode()) {{ if (rest < n.data.length) {{ "
            f"r.setStart(n, rest); r.setEnd(n, rest + 1); "
            f"return r.getBoundingClientRect().top - f.getBoundingClientRect().top + f.scrollTop; }} "
            f"rest -= n.data.length; }} return -1; }}; "
            f"const sh0 = f.scrollHeight, cw0 = f.clientWidth, ch0 = f.clientHeight, y0 = y(); "
            f"f.style.letterSpacing = '-0.4px'; "
            f"const pad = document.createElement('span'); pad.style.display = 'block'; pad.style.height = '0px'; "
            f"f.appendChild(pad); let h = 0; "
            f"for (let i = 0; i < 12 && f.scrollHeight !== sh0; i++) {{ h = Math.max(0, h + sh0 - f.scrollHeight); "
            f"pad.style.height = h + 'px'; }} "
            f"{FRAMES}; {FRAMES}; "
            f"return JSON.stringify({{sh0: sh0, cw0: cw0, ch0: ch0, sh1: f.scrollHeight, cw1: f.clientWidth, ch1: f.clientHeight, spacer: h, "
            f"moved: y0 - y(), same_text: f.textContent === s}}); }})()")


@needs_browser
@needs_ssh
def test_a_reflow_that_keeps_scroll_height_and_width_does_not_make_unshown_text_seen(tmp_path, monkeypatch) -> None:
    """codex HIGH (fix 2): cover the upper half, scroll back to the top, reflow so the short lines move up by many lines
    while scrollHeight and clientWidth stay EXACTLY the same (printed), jump to where the old cover ended, scroll down
    to the end in steps. The lines that moved up into the old cover were shown in neither layout: Allow stays shut."""
    rig = _ConsentRig(tmp_path, monkeypatch)
    command = _typography_command()
    rid = rig.queue.file(agent="claude_code", agent_session_id="s1", tool_name="Bash", tool_input={"command": command},
                         cwd="/w", hold=timedelta(minutes=30), tier="T2").id
    shots = _shots(tmp_path)
    with rig.serving():
        out = probed_drive(tmp_path, f"{rig.origin}/nope", [
            PHONE, rig.authenticator(), *rig.open_top(rid),                                               # 0 1
            {"tap": SUMMARY},                                                                             # 2
            {"until": f"{INPUT}.classList.contains('open') && 1", "ms": 10000},                           # 3
            {"eval": stepped(FULL, 0.8, down=True, until=0.5)},                                           # 4
            {"eval": f"(() => {{ const f = {FULL}; window.__coverEnd = f.scrollTop + f.clientHeight; "
                     f"return JSON.stringify({{end: window.__coverEnd, at: f.textContent.indexOf('row 0000'), "
                     f"state: JSON.parse({SHEET})}}); }})()"},                                               # 5
            {"eval": stepped(FULL, 0.8, down=False)},                                                     # 6
            {"eval": _reflow(FULL)},                                                                  # 7
            {"eval": f"(async () => {{ const f = {FULL}; f.scrollTop = window.__coverEnd; {FRAMES}; "
                     f"return f.scrollTop; }})()"},                                                       # 8
            {"eval": stepped(FULL, 0.8, down=True)},                                                      # 9
            {"eval": f"(async () => {{ {FRAMES}; return {SHEET}; }})()"},                                 # 10
            {"eval": f"(() => {{ const a = document.getElementById('sheet-actions'); "
                     f"if (a) a.scrollIntoView({{block: 'end'}}); return 1; }})()"},                     # 11
            {"shot": str(shots / "fix3_1_sheet_typography_reflow_allow_shut_390.png")},                    # 12
        ])
    v = out["values"]
    cover = json.loads(v[5])
    assert cover["state"]["seen"] == "0" and cover["state"]["allow"] is True, cover
    rf = json.loads(v[7])
    print("REFLOW KEYS", rf)                                                     # the measurement the brief asks for
    assert rf["same_text"] is True, rf
    assert rf["sh0"] == rf["sh1"] and rf["cw0"] == rf["cw1"], rf                 # fix 2's key is unchanged
    assert abs(rf["ch0"] - rf["ch1"]) <= 1, rf                                  # and the view is as tall as before
    assert rf["moved"] > 60, rf                                                  # the short lines moved up, many lines
    assert abs(v[8] - cover["end"]) <= 1, (v[8], cover)
    for i in (6, 9):
        s = json.loads(v[i])
        assert s["max"] <= s["ch"], s
    end = json.loads(v[10])
    print("AFTER", end)
    assert end["top"] + end["ch"] >= end["sh"] - 2, end                         # it did reach the end
    assert end["allow"] is True and end["seen"] == "0", end                      # Allow DISABLED
    assert end["unseen"] > 0 and "not yet shown" in end["line"], end
    assert rig.state(rid) == "pending"


@needs_browser
@needs_ssh
def test_a_jump_names_the_skipped_chunks_and_the_payload_is_among_them(tmp_path, monkeypatch) -> None:
    """(a) Open and jump to the end in one step: the chunks not shown are one run between the first view and the last,
    their code points are exactly the count the line gives, and the chunk holding the payload is one of them."""
    rig = _ConsentRig(tmp_path, monkeypatch)
    rid = _attack(rig)
    with rig.serving():
        out = probed_drive(tmp_path, f"{rig.origin}/nope", [
            PHONE, rig.authenticator(), *rig.open_top(rid),                                               # 0 1
            {"tap": SUMMARY},                                                                             # 2
            {"until": f"{INPUT}.classList.contains('open') && 1", "ms": 10000},                           # 3
            {"eval": f"(async () => {{ await {first_view_counted(INPUT)}; return {jump(FULL)}; }})()"},   # 4
            {"eval": f"(async () => {{ {FRAMES}; const f = {FULL}; const sp = [...f.querySelectorAll('.long-group > span')]; "
                     f"const u = {INPUT}.longUnshown(); const at = f.textContent.indexOf({json.dumps(EVIL)}); "
                     f"let k = 0, hit = -1; sp.forEach((c, i) => {{ if (hit < 0 && at < k + c.textContent.length) hit = i; "
                     f"k += c.textContent.length; }}); "
                     f"return JSON.stringify({{u0: u[0], u1: u[u.length - 1], n: u.length, chunks: sp.length, hit: hit, "
                     f"cps: u.reduce((a, i) => a + Array.from(sp[i].textContent).length, 0), "
                     f"state: JSON.parse({SHEET})}}); }})()"},                                              # 5
        ])
    j = json.loads(out["values"][5])
    s = j["state"]
    assert s["allow"] is True and s["seen"] == "0", j                            # Allow DISABLED
    assert j["u1"] - j["u0"] + 1 == j["n"], j                                    # one run: the skipped middle
    assert 0 < j["u0"] and j["u1"] < j["chunks"] - 1, j                          # the first and the last view are shown
    assert j["cps"] == s["unseen"] and f"{s['unseen']:,} of" in s["line"], j     # the count is those chunks, exactly
    assert j["u0"] <= j["hit"] <= j["u1"], j                                     # the payload is not shown


@needs_browser
@needs_ssh
def test_at_the_desktop_cockpits_width_a_read_through_opens_allow(tmp_path, monkeypatch) -> None:
    """Fix 5: at the size the desktop cockpit gives the sheet's frame (898 x 680), the block's lines wrap at spaces inside
    chunks. Chrome hit-tests the white space a line wraps at, and the right part of the glyph before it, to the PRE, so
    fixes 3 and 4 never counted such a chunk uncovered: a read-through in the cockpit's frame left 99 of its 1,106
    chunks unshown (measured on fix 4) and Allow shut. Stepped through, every chunk is shown, Allow opens and the count line says so."""
    rig = _ConsentRig(tmp_path, monkeypatch)
    rid = _attack(rig)
    with rig.serving():
        out = probed_drive(tmp_path, f"{rig.origin}/nope", [
            {"emulate": {"w": 898, "h": 680, "dpr": 1}}, rig.authenticator(), *rig.open_top(rid),        # 0 1
            {"tap": SUMMARY},                                                                             # 2
            {"until": f"{INPUT}.classList.contains('open') && 1", "ms": 10000},                           # 3
            {"eval": f"(async () => {{ {FULL}.scrollIntoView({{block: 'center'}}); {FRAMES}; {FRAMES}; "
                     f"const f = {FULL}, r = f.getBoundingClientRect(); return JSON.stringify({{block: [r.top, r.bottom], "
                     f"vh: innerHeight, w: f.clientWidth, ws: getComputedStyle(f).whiteSpace}}); }})()"},           # 4
            {"eval": stepped(FULL, 0.8, down=True)},                                                      # 5
            {"eval": f"(async () => {{ {FRAMES}; {FRAMES}; return JSON.stringify({{n: {INPUT}.longUnshown().length, "
                     f"state: JSON.parse({SHEET})}}); }})()"},                                              # 6
        ])
    v = out["values"]
    at = json.loads(v[4])
    print("DESKTOP WIDTH", at, v[5], v[6])
    assert 0 <= at["block"][0] and at["block"][1] <= at["vh"] and at["ws"] == "pre-wrap", at    # the whole view on screen
    s = json.loads(v[5])
    assert s["max"] <= s["ch"], s                                                # no step skipped a view
    end = json.loads(v[6])
    assert end["n"] == 0, end                                                    # every chunk shown
    assert end["state"]["seen"] == "1" and end["state"]["allow"] is False, end   # Allow OPEN
    assert end["state"]["unseen"] == 0 and "Allow is open" in end["state"]["line"], end


@needs_browser
@needs_ssh
def test_a_block_partly_below_the_fold_shows_only_its_visible_part(tmp_path, monkeypatch) -> None:
    """(e, complement LOW) The page pushed down so the open block runs past the bottom of the screen: the part of the
    block's own view that is below the fold is NOT shown, nor a chunk the fold cuts; scrolling the page up to it shows
    it."""
    rig = _ConsentRig(tmp_path, monkeypatch)
    rid = _attack(rig)
    shots = _shots(tmp_path)
    with rig.serving():
        out = probed_drive(tmp_path, f"{rig.origin}/nope", [
            PHONE, rig.authenticator(), *rig.open_top(rid),                                               # 0 1
            {"eval": "(document.body.style.paddingTop = '620px', 1)"},                                   # 2
            {"eval": f"({SUMMARY}.click(), 1)"},                                                          # 3
            {"until": f"{INPUT}.classList.contains('open') && 1", "ms": 10000},                           # 4
            {"eval": first_view_counted(INPUT)},                                                          # 5
            {"eval": truth(INPUT, FULL)},                                                                 # 6
            {"shot": str(shots / "fix3_2_sheet_block_below_the_fold_390.png")},                           # 7
            {"eval": f"(async () => {{ const p = {FULL}.getBoundingClientRect(); "
                     f"window.scrollBy(0, p.bottom - innerHeight + 4); {FRAMES}; {FRAMES}; return 1; }})()"},  # 8
            {"eval": truth(INPUT, FULL)},                                                                 # 9
        ])
    v = out["values"]
    below = json.loads(v[6])
    print("FOLD", below)
    assert below["block"][1] > below["vh"] + 100 and below["block"][0] < below["vh"], below    # cut by the fold
    assert below["nwrong"] == 0, below                     # nothing below the fold, nor cut by it, counts as shown
    assert 0 < below["shown"] == below["onscreen"], below  # what is wholly on screen does
    assert below["state"]["allow"] is True and below["state"]["seen"] == "0", below
    up = json.loads(v[9])
    print("FOLD UP", up)
    assert up["shown"] > below["shown"] + 20 and up["state"]["seen"] == "0", (below, up)


@needs_browser
@needs_ssh
def test_a_block_under_an_overlay_shows_only_what_is_not_covered(tmp_path, monkeypatch) -> None:
    """(e, overlay) A fixed bar (a sticky header or action bar) painted over the lower half of the open block: the
    chunks under it are inside the block's view and the viewport, and are NOT shown; once the bar is gone and the block
    is scrolled by a pixel, they are."""
    rig = _ConsentRig(tmp_path, monkeypatch)
    rid = _attack(rig)
    shots = _shots(tmp_path)
    bar = "document.getElementById('test-bar')"
    with rig.serving():
        out = probed_drive(tmp_path, f"{rig.origin}/nope", [
            PHONE, rig.authenticator(), *rig.open_top(rid),                                               # 0 1
            # the bar goes up BEFORE the block is opened (screen y 400 to 700: the open block's lower half), so the
            # browser never painted that half uncovered
            {"eval": "(() => { const d = document.createElement('div'); d.id = 'test-bar'; "
                     "d.style.cssText = 'position:fixed;left:0;right:0;z-index:50;background:#c33;top:400px;height:300px'; "
                     "document.body.appendChild(d); return 1; })()"},                                     # 2
            {"eval": f"({SUMMARY}.click(), 1)"},                                                          # 3
            {"until": f"{INPUT}.classList.contains('open') && 1", "ms": 10000},                           # 4
            {"eval": first_view_counted(INPUT)},                                                          # 5
            {"eval": truth(INPUT, FULL, bar)},                                                            # 6
            {"shot": str(shots / "fix3_3_sheet_block_under_an_overlay_390.png")},                         # 7
            {"eval": settled(FULL, f"{bar}.remove(); f.scrollTop = 1")},                                  # 8
            {"eval": truth(INPUT, FULL)},                                                                 # 9
        ])
    v = out["values"]
    under = json.loads(v[6])
    print("OVERLAY", under)
    assert under["block"][0] < under["overlay"][0] < under["block"][1] < under["vh"], under   # the bar is over the block
    assert under["nwrong"] == 0, under                                           # nothing under it counts as shown
    assert 0 < under["shown"] == under["onscreen"], under
    clear = json.loads(v[9])
    print("OVERLAY GONE", clear)
    assert clear["shown"] > under["shown"] + 10, (under, clear)


@needs_browser
@needs_ssh
@pytest.mark.parametrize("late_ms", [0, LATE_IO_MS])
def test_on_the_needs_card_a_jump_leaves_one_run_of_chunks_not_shown(tmp_path, monkeypatch, late_ms) -> None:
    """(f) The Needs-you card in the cockpit's modal: after a jump, the chunks not shown are one run (the skipped
    middle), Allow is shut, and the line gives the count. The jump is made only once the gate has counted the view the
    block opened on (`first_view_counted`): a jump made before it leaves that view uncounted for good, which is the
    product failing closed and not a defect (CI red 11-13, `u0 == 0`). `late_ms` holds every observer callback that long
    (the loaded box, made deterministic): at 250 the unwaited version of this drive fails every run."""
    rig = _ConsentRig(tmp_path, monkeypatch)
    rid = _attack(rig)
    with rig.serving():
        out = probed_drive(tmp_path, f"{rig.origin}/#write_token={TOK}", [
            {"init": io_probe(late_ms)},
            DESKTOP, rig.authenticator(),
            {"until": "[...document.querySelectorAll('#ck-needs .needs-consent')].length >= 1", "ms": 30000},     # 0
            {"eval": "[...document.querySelectorAll('#ck-needs .needs-consent')].find(x => /Bash/.test(x.textContent))"
                     ".click() || 1"},                                                                    # 1
            {"until": f"{CARD}.querySelector('.long-text') && 1", "ms": 30000},                           # 2
            {"tap": f"{CARD}.querySelector('.long-whole > summary')"},                                    # 3
            {"until": f"{CARD}.querySelector('.long-text.open') && 1", "ms": 10000},                      # 4
            {"eval": f"(async () => {{ await {first_view_counted(CARD)}; return await {jump(C_FULL)}; }})()"},  # 5
            {"eval": C_STATE},                                                                            # 6
            {"eval": f"(() => {{ const u = {CARD}.querySelector('.long-text').longUnshown(); "
                     f"return JSON.stringify({{n: u.length, run: u[u.length - 1] - u[0] + 1, u0: u[0], "
                     f"chunks: {C_FULL}.querySelectorAll('.long-group > span').length}}); }})()"},                                     # 7
        ])
    v = out["values"]
    s = json.loads(v[6])
    assert s["allow"] is True and s["seen"] == "0" and "not yet shown" in s["line"], s
    u = json.loads(v[7])
    assert u["n"] == u["run"] and 0 < u["u0"] and u["n"] < u["chunks"], u
    assert rig.state(rid) == "pending"


@needs_browser
@needs_ssh
def test_the_chunked_text_copies_and_finds_as_the_original(tmp_path, monkeypatch) -> None:
    """(g) The chunks change nothing of the text: the block's text and a selection of all of it are the input, the head
    and the tail are its first and last 400 code points, and find-in-page finds the payload and a string across a chunk
    edge."""
    rig = _ConsentRig(tmp_path, monkeypatch)
    rid = _attack(rig)
    with rig.serving():
        out = probed_drive(tmp_path, f"{rig.origin}/nope", [
            PHONE, rig.authenticator(), *rig.open_top(rid),                                               # 0 1
            {"tap": SUMMARY},                                                                             # 2
            {"until": f"{INPUT}.classList.contains('open') && 1", "ms": 10000},                           # 3
            {"eval": f"(() => {{ const f = {FULL}; const t = f.textContent; const r = document.createRange(); "
                     f"r.selectNodeContents(f); const s = getSelection(); s.removeAllRanges(); s.addRange(r); "
                     f"const sel = s.toString(); s.removeAllRanges(); const cps = Array.from(t); "
                     f"const head = {INPUT}.querySelector('.long-head').textContent; "
                     f"const tail = {INPUT}.querySelector('.long-tail').textContent; "
                     f"const e = [...f.querySelectorAll('.long-group > span')].findIndex((c, i) => i > 3 && c.textContent.length > 4 && /^\\w{{3}}$/.test(c.textContent.slice(-3))); "
                     f"const across = f.querySelectorAll('.long-group > span')[e].textContent.slice(-3) + f.querySelectorAll('.long-group > span')[e + 1].textContent.slice(0, 3); "
                     f"const fe = window.find({json.dumps(EVIL.strip())}); s.removeAllRanges(); "
                     f"const fa = window.find(across, false, false, true); "
                     f"return JSON.stringify({{same_sel: sel === t, n: cps.length, chars: Number({INPUT}.dataset.chars), "
                     f"head: head === cps.slice(0, 400).join(''), tail: tail === cps.slice(-400).join(''), "
                     f"evil: t.indexOf({json.dumps(EVIL)}) >= 0, spans: f.querySelectorAll('.long-group > span').length, "
                     f"find_evil: fe, across: across, find_across: fa}}); }})()"},                         # 4
        ])
    g = json.loads(out["values"][4])
    print("COPY", g)
    assert g["same_sel"] and g["n"] == g["chars"] and g["head"] and g["tail"] and g["evil"], g
    assert g["spans"] > 1 and g["find_evil"] is True and g["find_across"] is True, g
    assert rig.state(rid) == "pending"


# Fix round 4. The fine observer's targets, tracked by wrapping its prototype before the block opens: answers how many
# chunks it still observes that the gate holds as shown, how many it observes in all, and the shown count.
TRACK_IO = ("(() => { const P = IntersectionObserver.prototype, o = P.observe, u = P.unobserve, dc = P.disconnect; "
            "window.__obs = new Map(); "
            "P.observe = function (t) { if (!window.__obs.has(this)) window.__obs.set(this, new Set()); window.__obs.get(this).add(t); return o.call(this, t); }; "
            "P.unobserve = function (t) { const s = window.__obs.get(this); if (s) s.delete(t); return u.call(this, t); }; "
            "P.disconnect = function () { window.__obs.delete(this); return dc.call(this); }; return 1; })()")
HELD = (f"(() => {{ const sp = [...{FULL}.querySelectorAll('.long-group > span')]; const at = new Map(sp.map((c, i) => [c, i])); "
        f"const u = new Set({INPUT}.longUnshown()); let held = 0, observed = 0; "
        f"for (const s of window.__obs.values()) for (const t of s) {{ if (!at.has(t)) continue; observed += 1; "
        f"if (!u.has(at.get(t))) held += 1; }} "
        f"return JSON.stringify({{held: held, observed: observed, shown: sp.length - u.size, chunks: sp.length, "
        f"observers: window.__obs.size, state: JSON.parse({SHEET})}}); }})()")


@needs_browser
@needs_ssh
def test_a_shown_chunk_is_no_longer_observed(tmp_path, monkeypatch) -> None:
    """(fix 4, complement + codex MED, perf) A chunk the gate has shown is let go by the fine observer: part way through
    a read the observer holds none of the hundreds shown so far, and the read still latches at its end."""
    rig = _ConsentRig(tmp_path, monkeypatch)
    rid = _attack(rig)
    with rig.serving():
        out = probed_drive(tmp_path, f"{rig.origin}/nope", [
            PHONE, rig.authenticator(), *rig.open_top(rid),                                               # 0 1
            {"eval": TRACK_IO},                                                                           # 2
            {"tap": SUMMARY},                                                                             # 3
            {"until": f"{INPUT}.classList.contains('open') && 1", "ms": 10000},                           # 4
            {"eval": stepped(FULL, 0.8, down=True, until=0.6)},                                           # 5
            {"eval": f"(async () => {{ {FRAMES}; return {HELD}; }})()"},                                  # 6
            {"eval": stepped(FULL, 0.8, down=True)},                                                      # 7
            {"eval": f"(async () => {{ {FRAMES}; return {HELD}; }})()"},                                  # 8
        ])
    v = out["values"]
    mid, end = json.loads(v[6]), json.loads(v[8])
    print("HELD", mid, end)
    assert mid["shown"] > 300 and mid["state"]["seen"] == "0", mid
    assert mid["held"] == 0, mid                                    # no shown chunk is still observed
    assert mid["observed"] < 6 * 64 < mid["shown"] + 64, mid        # only the unshown chunks of runs near the view are
    assert end["state"]["seen"] == "1" and end["state"]["allow"] is False, end    # the read latches; Allow open
    assert end["observed"] == 0, end                                # and everything is let go


# a visual viewport of the test's own, put in place before the block opens: `scale`, and its height as a share of the
# window's (an iOS keyboard shrinks it; pinch-zoom scales it)
def _stub_vv(scale: float, share: float) -> str:
    return ("(() => { const vv = {scale: %s, offsetLeft: 0, offsetTop: 0, width: innerWidth, height: innerHeight * %s, "
            "addEventListener() {}, removeEventListener() {}}; "
            "Object.defineProperty(window, 'visualViewport', {configurable: true, value: vv}); window.__vv = vv; return 1; })()"
            % (scale, share))


VV_TRUTH = (f"(() => {{ const f = {FULL}, pr = f.getBoundingClientRect(), vv = window.__vv; "
            f"const top = Math.max(0, pr.top + f.clientTop), bot = Math.min(innerHeight, pr.top + f.clientTop + f.clientHeight); "
            f"const sp = [...f.querySelectorAll('.long-group > span')]; const u = new Set({INPUT}.longUnshown()); "
            f"let outside = 0, wrong = 0; sp.forEach((c, i) => {{ const r = c.getBoundingClientRect(); "
            f"const inView = r.top >= top - 0.5 && r.bottom <= bot + 0.5; const inVv = r.bottom <= vv.offsetTop + vv.height + 0.5; "
            f"if (inView && !inVv) outside += 1; if (!u.has(i) && !inVv) wrong += 1; }}); "
            f"return JSON.stringify({{shown: sp.length - u.size, outside: outside, wrong: wrong, vvh: vv.height, block: [pr.top, pr.bottom], "
            f"state: JSON.parse({SHEET})}}); }})()")


@needs_browser
@needs_ssh
def test_under_pinch_zoom_nothing_is_shown_and_only_the_visual_viewport_counts(tmp_path, monkeypatch) -> None:
    """(fix 4, complement + fable LOW) IntersectionObserver and the hit test work in the LAYOUT viewport. Under a visual
    viewport scaled 2x nothing is shown; at scale 1 with the visual viewport cut to the upper part of the screen (a
    keyboard up), no chunk below it is shown though it is inside the block's view and the layout viewport."""
    rig = _ConsentRig(tmp_path, monkeypatch)
    rid = _attack(rig)
    nudge = f"(async () => {{ {FULL}.scrollTop += 2; {FRAMES}; {FRAMES}; return 1; }})()"
    with rig.serving():
        out = probed_drive(tmp_path, f"{rig.origin}/nope", [
            PHONE, rig.authenticator(), *rig.open_top(rid),                                               # 0 1
            {"eval": _stub_vv(2, 1)},                                                                     # 2
            {"eval": f"({SUMMARY}.click(), 1)"},                                                          # 3
            {"until": f"{INPUT}.classList.contains('open') && 1", "ms": 10000},                           # 4
            {"eval": nudge},                                                                              # 5
            {"eval": VV_TRUTH},                                                                           # 6
            {"eval": f"(() => {{ const pr = {FULL}.getBoundingClientRect(); window.__vv.scale = 1; "
                     f"window.__vv.height = Math.round((pr.top + pr.bottom) / 2); return window.__vv.height; }})()"},  # 7
            {"eval": nudge},                                                                              # 8
            {"eval": VV_TRUTH},                                                                           # 9
        ])
    v = out["values"]
    zoomed, keyboard = json.loads(v[6]), json.loads(v[9])
    print("ZOOM", zoomed, "KEYBOARD", keyboard)
    assert zoomed["shown"] == 0 and zoomed["state"]["seen"] == "0", zoomed
    assert keyboard["outside"] > 5, keyboard                       # chunks in the block's view, below the visual viewport
    assert keyboard["shown"] > 0 and keyboard["wrong"] == 0, keyboard    # what is above it shows; nothing below does


@needs_browser
@needs_ssh
def test_without_a_visual_viewport_nothing_is_shown(tmp_path, monkeypatch) -> None:
    """(fix 6, codex on fix 5) A browser with no visualViewport cannot say whether the page is pinch-zoomed nor where the
    visual viewport lies, so the gate counts nothing shown: read through to the end, Allow stays shut and Deny open."""
    rig = _ConsentRig(tmp_path, monkeypatch)
    rid = _attack(rig)
    with rig.serving():
        out = probed_drive(tmp_path, f"{rig.origin}/nope", [
            PHONE, rig.authenticator(), *rig.open_top(rid),                                               # 0 1
            {"eval": "(Object.defineProperty(window, 'visualViewport', {configurable: true, value: undefined}), "
                     "String(window.visualViewport))"},                                                  # 2
            {"eval": f"({SUMMARY}.click(), 1)"},                                                          # 3
            {"until": f"{INPUT}.classList.contains('open') && 1", "ms": 10000},                           # 4
            {"eval": stepped(FULL, 0.8, down=True, counts=False)},                                        # 5
            {"eval": f"(async () => {{ {FRAMES}; {FRAMES}; return JSON.stringify({{n: {INPUT}.longUnshown().length, "
                     f"state: JSON.parse({SHEET})}}); }})()"},                                              # 6
        ])
    v = out["values"]
    assert v[2] == "undefined", v
    end = json.loads(v[6])
    print("NO VISUAL VIEWPORT", end)
    assert end["state"]["allow"] is True and end["state"]["seen"] == "0", end    # Allow DISABLED
    assert end["state"]["unseen"] == end["state"]["total"] > 0, end               # nothing counted shown: every character unseen


# window scroll listeners named `later` (the gate's), wrapped to count their calls and how many are still added
TRACK_LATER = ("(() => { const add = window.addEventListener, rem = window.removeEventListener, m = new Map(); "
               "window.__later = 0; window.__live = 0; "
               "window.addEventListener = function (t, fn, o) { if (t === 'scroll' && fn && fn.name === 'later') { "
               "const w = function (e) { window.__later += 1; return fn.call(this, e); }; m.set(fn, w); window.__live += 1; "
               "return add.call(this, t, w, o); } return add.call(this, t, fn, o); }; "
               "window.removeEventListener = function (t, fn, o) { if (t === 'scroll' && m.has(fn)) { window.__live -= 1; "
               "const w = m.get(fn); m.delete(fn); return rem.call(this, t, w, o); } return rem.call(this, t, fn, o); }; "
               "return 1; })()")


@needs_browser
@needs_ssh
def test_a_box_removed_before_it_is_seen_lets_its_listeners_go(tmp_path, monkeypatch) -> None:
    """(fix 4, complement LOW, leak) The box drawn again or removed before it was seen: at the next scroll its window
    listener lets go of everything, and a scroll after that runs no handler of it."""
    rig = _ConsentRig(tmp_path, monkeypatch)
    rid = _attack(rig)
    box = f"({INPUT}.classList.contains('long-text') ? {INPUT} : {INPUT}.querySelector('.long-text'))"
    scroll = f"(async () => {{ window.dispatchEvent(new Event('scroll')); {FRAMES}; return JSON.stringify([window.__later, window.__live]); }})()"
    with rig.serving():
        out = probed_drive(tmp_path, f"{rig.origin}/nope", [
            PHONE, rig.authenticator(), *rig.open_top(rid),                                               # 0 1
            {"eval": TRACK_LATER},                                                                        # 2
            {"eval": f"({SUMMARY}.click(), 1)"},                                                          # 3
            {"until": f"{INPUT}.classList.contains('open') && 1", "ms": 10000},                           # 4
            {"eval": scroll},                                                                             # 5
            {"eval": f"(() => {{ const b = {box}; window.__box = b; b.remove(); return b.dataset.seen; }})()"},   # 6
            {"eval": scroll},                                                                             # 7
            {"eval": scroll},                                                                             # 8
            {"eval": scroll},                                                                             # 9
        ])
    v = out["values"]
    print("LATER", v[5:])
    on = json.loads(v[5])
    assert on[1] == 1 and on[0] >= 1, on                            # watching while in the page
    assert v[6] == "0", v
    first, second, third = json.loads(v[7]), json.loads(v[8]), json.loads(v[9])
    assert first[1] == 0, first                                     # the first scroll after the removal let it go
    assert second[0] == first[0] and third[0] == first[0], (first, second, third)   # no handler of it ran again


@needs_browser
@needs_ssh
def test_in_a_frame_the_parent_pages_fold_clips_what_is_shown(tmp_path, monkeypatch) -> None:
    """(e, frames) The sheet in a frame (the cockpit shows it in one) placed so the open block runs past the bottom of
    the PARENT's screen. Inside the frame everything is in its own viewport, and a hit test there passes; only the
    browser's intersection with the top-level viewport knows the lower part is off screen. Nothing below the parent's
    fold, nor cut by it, is shown."""
    rig = _ConsentRig(tmp_path, monkeypatch)
    rid = _attack(rig)
    fd = "document.querySelector('iframe').contentDocument"
    f_input = INPUT.replace("document.", fd + ".")
    f_full = FULL.replace("document.", fd + ".")
    f_summary = SUMMARY.replace("document.", fd + ".")
    with rig.serving():
        out = probed_drive(tmp_path, f"{rig.origin}/nope", [
            PHONE, rig.authenticator(), *rig.open_top(rid),                                               # 0 1
            # the cockpit's own page frames the sheet (its modal does): a frame of ours, placed past the fold, in a modal
            # dialog as the cockpit's is (fix 4: a frame outside the top layer counts nothing)
            {"eval": "(() => { window.__sheet = location.pathname; location.href = '/'; return 1; })()"},       # 2
            {"until": "location.pathname === '/' && document.readyState === 'complete' && 1", "ms": 30000},
            {"eval": f"(() => {{ const d = document.createElement('dialog'); "
                     "d.style.cssText = 'position:fixed;inset:0;margin:0;padding:0;border:0;max-width:none;max-height:none;"
                     "width:auto;height:auto;overflow:visible;background:transparent'; "
                     f"const fr = document.createElement('iframe'); fr.src = '/sign/consent/{rig.enc(rid)}'; "
                     "fr.style.cssText = 'position:fixed;left:0;top:520px;width:390px;height:1400px;border:0;z-index:99999'; "
                     "d.appendChild(fr); document.body.appendChild(d); d.showModal(); return 1; })()"},   # 4
            {"until": f"{fd} && {f_input} && {f_input}.querySelector('.long-whole') && 1", "ms": 30000},  # 3
            {"eval": f"({f_summary}.click(), 1)"},                                                        # 4
            {"until": f"{f_input}.classList.contains('open') && 1", "ms": 10000},                         # 5
            {"eval": f"(async () => {{ {FRAMES}; {FRAMES}; {FRAMES}; return 1; }})()"},                  # 6
            {"eval": f"(() => {{ const fr = document.querySelector('iframe'); const off = fr.getBoundingClientRect().top; "
                     f"const f = {f_full}; const w = fr.contentWindow; const pr = f.getBoundingClientRect(); "
                     f"const sp = [...f.querySelectorAll('.long-group > span')]; const u = new Set({f_input}.longUnshown()); "
                     f"let below = 0, wrong = 0, onscreen = 0; sp.forEach((c, i) => {{ const r = c.getBoundingClientRect(); "
                     f"const inBlock = r.top >= pr.top && r.bottom <= pr.top + f.clientTop + f.clientHeight; "
                     f"const whole = inBlock && off + r.bottom <= innerHeight + 0.5; if (whole) onscreen += 1; "
                     f"if (inBlock && !whole) below += 1; if (!whole && !u.has(i)) wrong += 1; }}); "
                     f"return JSON.stringify({{off: off, vh: innerHeight, frame_vh: w.innerHeight, "
                     f"block: [off + pr.top, off + pr.bottom], below: below, onscreen: onscreen, wrong: wrong, "
                     f"shown: sp.length - u.size, seen: {f_input}.dataset.seen}}); }})()"},                 # 7
        ])
    t = json.loads(out["values"][9])
    print("FRAME", t)
    assert t["frame_vh"] > t["block"][1] - t["off"], t                           # the block is inside the frame's viewport
    assert t["block"][0] < t["vh"] < t["block"][1], t                            # and cut by the parent's fold
    assert t["below"] > 5, t                                                     # chunks of the block's view off screen
    assert t["wrong"] == 0, t                                                    # none of them, nor a cut one, shown
    assert 0 < t["shown"] == t["onscreen"] and t["seen"] == "0", t

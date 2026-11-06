"""Phill 11-06: the cockpit hero Drop box is a contenteditable div (role=textbox), not a textarea, so iOS Safari's AutoFill has no form
control to classify as a card or a contact. These drive the real page in headless Chrome with trusted key events and check that the
div behaves as the textarea did: multi-line typing read back, Shift+Enter, plain-text paste, the placeholder, send clears it, a
cleared box shows the placeholder again, and Cmd+Enter does not send mid-composition."""
from __future__ import annotations

import json
from pathlib import Path

from tests.test_msgslot_hero import BOX, OFF, READY, TXT, _send, _tagged, _type
from tests.test_cockpit_narrow_layout_rig import NEEDS, SETTLE, TOK, FullRig, drive_touch, needs_browser, needs_ssh

PH = f"getComputedStyle({BOX}, '::before').content"
ENTER = {"key": "Enter", "vk": 13, "text": "\r"}
SHIFT_ENTER = {"key": "Enter", "vk": 13, "text": "\r", "modifiers": 8}


def _tag(t: str, expr: str) -> dict:
    return {"eval": f"JSON.stringify(Object.assign({{t: {json.dumps(t)}}}, {expr}))"}


def _keys(text: str) -> list:
    return [{"key": c} for c in text]


def _open(extra: list) -> list:
    return [{"emulate": {"w": 1280, "h": 900, "dpr": 1}}, {"until": NEEDS, "ms": 30000}, {"until": READY, "ms": 30000}, SETTLE,
            {"eval": f"{BOX}.focus(); 1"}, *extra]


def _run(tmp_path: Path, monkeypatch, steps: list) -> dict:
    rig = FullRig(tmp_path, monkeypatch)
    with rig.serving():
        return _tagged(drive_touch(tmp_path, f"{rig.origin}/#write_token={TOK}", steps))


STATE = f"({{txt: {TXT}, html: {BOX}.innerHTML, ph: {PH}, kids: {BOX}.children.length, role: {BOX}.getAttribute('role'), tag: {BOX}.tagName}})"


@needs_browser
@needs_ssh
def test_multi_line_typing_reads_back_as_the_words_with_newlines_and_shift_enter_adds_one(tmp_path: Path, monkeypatch) -> None:
    steps = _open([*_keys("hi"), ENTER, *_keys("there"), SHIFT_ENTER, *_keys("you"), _tag("typed", STATE)])
    got = _run(tmp_path, monkeypatch, steps)["typed"]
    assert got["txt"] == "hi\nthere\nyou", got
    assert got["tag"] == "DIV" and got["role"] == "textbox", got
    assert got["ph"] in ("none", "normal"), got      # words in the box: no placeholder


@needs_browser
@needs_ssh
def test_the_placeholder_shows_when_empty_hides_when_typed_and_returns_when_cleared_by_backspace(tmp_path: Path, monkeypatch) -> None:
    steps = _open([_tag("empty", STATE), *_keys("a"), _tag("one", STATE), {"key": "Backspace", "vk": 8}, _tag("gone", STATE)])
    got = _run(tmp_path, monkeypatch, steps)
    assert got["empty"]["ph"] == '"words for the coordinator"' and got["empty"]["txt"] == "", got["empty"]
    assert got["one"]["ph"] in ("none", "normal") and got["one"]["txt"] == "a", got["one"]
    assert got["gone"]["txt"] == "" and got["gone"]["html"] == "" and got["gone"]["ph"] == '"words for the coordinator"', got["gone"]


PASTE = ("(() => {{ const dt = new DataTransfer(); dt.setData('text/html', '<b style=\"color:red\">bold</b> <a href=\"x\">link</a><img src=x>');"
         " dt.setData('text/plain', 'bold link\\nsecond line'); const ev = new ClipboardEvent('paste', {{clipboardData: dt, bubbles: true, cancelable: true}});"
         " {box}.dispatchEvent(ev); return 1; }})()").format(box=BOX)


@needs_browser
@needs_ssh
def test_pasting_html_inserts_plain_text_only(tmp_path: Path, monkeypatch) -> None:
    steps = _open([*_keys("x "), {"eval": PASTE}, _tag("pasted", STATE)])
    got = _run(tmp_path, monkeypatch, steps)["pasted"]
    assert got["txt"] == "x bold link\nsecond line", got
    assert "<b" not in got["html"] and "<a " not in got["html"] and "<img" not in got["html"] and "style=" not in got["html"], got


@needs_browser
@needs_ssh
def test_send_clears_the_box_and_the_placeholder_returns(tmp_path: Path, monkeypatch) -> None:
    done = "window.__sent = /^Sent/.test(document.querySelector('#ck-drop .drop-receipt').textContent) && " + TXT + " === ''"
    steps = _open([_type("two\nlines"), _send(until=done, ms=20000), _tag("after", STATE)])
    got = _run(tmp_path, monkeypatch, steps)["after"]
    assert got["txt"] == "" and got["html"] == "" and got["ph"] == '"words for the coordinator"', got


@needs_browser
@needs_ssh
def test_command_enter_sends_but_not_while_composing(tmp_path: Path, monkeypatch) -> None:
    arm = f"window.__n = 0; document.querySelector('#ck-drop [data-coord-drop=send]').addEventListener('click', (e) => {{ window.__n++; e.stopImmediatePropagation(); }}, true); 1"
    fire = lambda composing: {"eval": f"{BOX}.dispatchEvent(new KeyboardEvent('keydown', {{key: 'Enter', metaKey: true, isComposing: {str(composing).lower()}, bubbles: true, cancelable: true}})); window.__n"}
    steps = _open([{"eval": arm}, fire(True), fire(False)])
    out = _run_values(tmp_path, monkeypatch, steps)
    assert out[-2:] == [0, 1], out


def _run_values(tmp_path: Path, monkeypatch, steps: list) -> list:
    rig = FullRig(tmp_path, monkeypatch)
    with rig.serving():
        return drive_touch(tmp_path, f"{rig.origin}/#write_token={TOK}", steps)["values"]


@needs_browser
@needs_ssh
def test_a_box_that_is_off_takes_no_typing_and_says_so(tmp_path: Path, monkeypatch) -> None:
    off = f"(() => {{ const h = document.getElementById('ck-drop'); h._drop.working = true; h._drop.lock(true); return 1; }})()"
    steps = _open([{"eval": off}, *_keys("zzz"), _tag("off", f"({{txt: {TXT}, ce: {BOX}.getAttribute('contenteditable'), aria: {BOX}.getAttribute('aria-disabled'), off: {OFF}}})")])
    got = _run(tmp_path, monkeypatch, steps)["off"]
    assert got == {"t": "off", "txt": "", "ce": "false", "aria": "true", "off": True}, got


# ---- L3 r1 (1106-16 composer): the fixes, each from the reproduced failure ----
from tests.test_msgslot_hero import EARLIER, _posted, _seed

OFF_ON = "(() => { const h = document.getElementById('ck-drop'); h._drop.working = true; h._drop.lock(true); return 1; })()"
PASTE_OF = lambda txt: ("(() => { const dt = new DataTransfer(); dt.setData('text/plain', %s); const ev = new ClipboardEvent('paste', {clipboardData: dt, bubbles: true, cancelable: true});"
                        " %s.dispatchEvent(ev); return 1; })()") % (json.dumps(txt), BOX)
DROP_OF = lambda txt, x, y: ("(() => { const dt = new DataTransfer(); dt.setData('text/plain', %s); const ev = new DragEvent('drop', {dataTransfer: dt, clientX: %d, clientY: %d, bubbles: true, cancelable: true});"
                             " %s.dispatchEvent(ev); return 1; })()") % (json.dumps(txt), x, y, BOX)


@needs_browser
@needs_ssh
def test_a_paste_and_a_drop_onto_an_off_box_insert_nothing(tmp_path: Path, monkeypatch) -> None:
    steps = _open([{"eval": OFF_ON}, {"eval": PASTE_OF("pasted while sending")}, {"eval": DROP_OF("dropped while sending", -5, -5)}, _tag("off", f"({{txt: {TXT}, html: {BOX}.innerHTML}})")])
    got = _run(tmp_path, monkeypatch, steps)["off"]
    assert got["txt"] == "" and got["html"] == "", got


FALLBACK = ("(() => { document.execCommand = () => false; const out = document.createElement('p'); out.id = 'outside-card'; out.textContent = 'adjacent card text'; document.body.appendChild(out);"
            " const t = %s.firstChild; const s = getSelection(); s.setBaseAndExtent(t, 1, out.firstChild, 5); return 1; })()") % BOX


@needs_browser
@needs_ssh
def test_the_paste_fallback_never_deletes_dom_outside_the_composer_when_the_selection_runs_out_of_it(tmp_path: Path, monkeypatch) -> None:
    steps = _open([*_keys("hello"), {"eval": FALLBACK}, {"eval": PASTE_OF("NEW")},
                   _tag("after", f"({{txt: {TXT}, outside: document.getElementById('outside-card').textContent}})")])
    got = _run(tmp_path, monkeypatch, steps)["after"]
    assert got["outside"] == "adjacent card text", got      # the page beside the box is untouched
    assert "NEW" in got["txt"] and got["txt"].startswith("hello"), got


@needs_browser
@needs_ssh
def test_the_words_read_back_with_their_line_breaks_while_the_host_is_hidden(tmp_path: Path, monkeypatch) -> None:
    hide = f"(() => {{ {BOX}.innerHTML = 'hi<div>there</div><div><br></div><div>you</div>'; document.getElementById('ck-drop').hidden = true; return 1; }})()"
    steps = _open([{"eval": hide}, _tag("hid", f"({{txt: {TXT}, shown: !!{BOX}.getClientRects().length}})")])
    got = _run(tmp_path, monkeypatch, steps)["hid"]
    assert got["shown"] is False and got["txt"] == "hi\nthere\n\nyou", got


@needs_browser
@needs_ssh
def test_a_kept_request_whose_words_end_in_newlines_or_nbsp_is_retried_under_its_own_id(tmp_path: Path, monkeypatch) -> None:
    rec = {"words": "tell it now\n\n", "outbox_id": "ph-" + "ef" * 16, "sent_at": "2026-10-30T12:00:00.123000Z", "store_id": None}
    rig = FullRig(tmp_path, monkeypatch)
    from tests.test_msgslot_hero import _send as send_step
    steps = [{"emulate": {"w": 1280, "h": 900, "dpr": 1}}, {"init": _seed(rec)}, {"until": NEEDS, "ms": 30000}, {"until": READY, "ms": 30000}, SETTLE,
             _tag("before", f"({{box: {TXT}, earlier: {EARLIER}}})"), send_step(posts=1)]
    with rig.serving():
        out = drive_touch(tmp_path, f"{rig.origin}/#write_token={TOK}", steps)
    assert _posted(out) == [rec], _posted(out)      # the SAME request: id and the words as kept, not a new id
    before = _tagged(out)["before"]
    assert before["box"] == "tell it now" and "not confirmed earlier" in before["earlier"], before


@needs_browser
@needs_ssh
def test_enter_in_an_empty_box_keeps_the_newline_it_made(tmp_path: Path, monkeypatch) -> None:
    got = _run(tmp_path, monkeypatch, _open([ENTER, *_keys("a"), _tag("e", STATE)]))["e"]
    assert got["txt"] == "\na", got


FORMATS = ("(() => { const o = {}; for (const t of ['formatBold', 'insertOrderedList', 'insertUnorderedList', 'insertText']) { const ev = new InputEvent('beforeinput', {inputType: t, cancelable: true, bubbles: true});"
           " %s.dispatchEvent(ev); o[t] = ev.defaultPrevented; } return JSON.stringify(Object.assign({t: 'fmt'}, o)); })()") % BOX


@needs_browser
@needs_ssh
def test_list_and_format_edits_are_refused_but_typing_is_not(tmp_path: Path, monkeypatch) -> None:
    got = _run(tmp_path, monkeypatch, _open([{"eval": FORMATS}]))["fmt"]
    assert (got["formatBold"], got["insertOrderedList"], got["insertUnorderedList"], got["insertText"]) == (True, True, True, False), got


OTHER = ("(() => { const i = document.createElement('input'); i.id = 'other-field'; i.value = 'abcdef'; document.body.appendChild(i); i.focus(); i.setSelectionRange(1, 3); return 1; })()")


@needs_browser
@needs_ssh
def test_a_drop_with_no_drop_point_does_not_write_into_another_focused_field(tmp_path: Path, monkeypatch) -> None:
    steps = _open([{"eval": OTHER}, {"eval": DROP_OF("DROPPED", -5, -5)}, _tag("d", f"({{txt: {TXT}, other: document.getElementById('other-field').value}})")])
    got = _run(tmp_path, monkeypatch, steps)["d"]
    assert got["other"] == "abcdef" and got["txt"] == "DROPPED", got

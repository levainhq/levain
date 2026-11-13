"""Row 185, the Dictate half: a Dictate that fails says why, in words, on the real cockpit card at desktop and phone width.

The unit-level table of reasons lives in tests/test_card_mic_lifecycle.py (a bare ``LevainMic.attach`` box); this file runs
the same failure through the cockpit's own cards in headless Chrome and reads what the card SHOWS. Two cards carry a
Dictate box and draw its note differently: the sign card's write-in (a ``.sign-note`` line) and the question card's reply
box (an ``.edit-msg`` span under the buttons). For each, at desktop and phone width, the words are there, drawn (not hidden
or empty), inside the viewport, inside the card's own box, and not cut by any ancestor that clips. A line that wraps can
never be wider than its box, so width alone proves nothing: the vertical edges and the clipping ancestors are the checks
that can fail."""
from __future__ import annotations

import contextlib
import json
from pathlib import Path

from levain.consent.questions import QuestionStore
from tests.test_card_mic_lifecycle import OPEN_Q, RECOG, SIGN_BOX
from tests.test_cockpit_flip_parity import _drive, needs_browser
from tests.test_cockpit_k2a import _install, _serve
from tests.test_cockpit_sign_card import TOK, _Rig, needs_ssh
from tests.test_mic_page import _fake

DESK_1280 = {"emulate": {"w": 1280, "h": 800, "dpr": 1}}
PHONE_390 = {"emulate": {"w": 390, "h": 844, "dpr": 2, "mobile": True}}

REASON = "Speech recognition could not run for this language: its on-device pack may be missing, or this browser blocks it."
CARD = "#levain-modal .needs-question"
CARD_DICT = f"document.querySelector('{CARD} .card-mic-dictate')"

# the two cards: where each draws a Dictate note
SIGN_NOTE_SEL = ".sign-note"
REPLY_NOTE_SEL = ".edit-msg"


def shown(note_sel: str) -> str:
    """What the card shows for its note: the words, whether it is drawn, and whether its edges are visible: inside the screen
    sideways, inside the card on every side, not cut by any ancestor whose overflow clips (the modal's scroller included) and not
    cut by its own overflow. The note is scrolled into view first, so a card that scrolls is judged on what the person reaches,
    not on what is below the fold; an ancestor that clips without letting the person scroll (hidden, clip) is put back to its
    origin first, because a script's scrollIntoView moves such a box and would hide the very cut the walk exists to find. The
    bottom edge is deliberately not compared with the window: under headless emulation the phone sheet is sized by 100dvh of the
    real window while innerHeight is the emulated one, so the sheet's own bottom can sit past innerHeight whatever the note does;
    the clipping walk is the vertical check that means something here (a real phone is Phill's look)."""
    return ("(() => { const card = document.querySelector(%s), n = card && card.querySelector(%s);"
            " if (!n) return JSON.stringify(null);"
            " n.scrollIntoView({ block: 'nearest' });"
            " const up = []; for (let p = n.parentElement; p && p !== document.documentElement; p = p.parentElement) up.push(p);"
            " for (const p of up) { const o = getComputedStyle(p); if (/hidden|clip/.test(o.overflowX + o.overflowY)) { p.scrollTop = 0; p.scrollLeft = 0; } }"
            " const r = n.getBoundingClientRect(), c = card.getBoundingClientRect(), s = getComputedStyle(n), cut = [];"
            " for (const p of up) {"
            "   const o = getComputedStyle(p); if (o.overflowX === 'visible' && o.overflowY === 'visible') continue;"
            "   const b = p.getBoundingClientRect();"
            "   if (r.left < b.left - 0.5 || r.right > b.right + 0.5 || r.top < b.top - 0.5 || r.bottom > b.bottom + 0.5) cut.push(p.className || p.tagName); }"
            " return JSON.stringify({ text: n.textContent, shown: s.display !== 'none' && s.visibility !== 'hidden' && r.width > 0 && r.height > 0,"
            " onScreen: r.left >= 0 && r.right <= window.innerWidth + 0.5,"
            " inCard: r.left >= c.left - 0.5 && r.right <= c.right + 0.5 && r.top >= c.top - 0.5 && r.bottom <= c.bottom + 0.5,"
            " cut, selfCut: n.scrollHeight > n.clientHeight + 1 || n.scrollWidth > n.clientWidth + 1, vw: window.innerWidth, edge: [Math.round(r.top), Math.round(r.bottom), window.innerHeight] }); })()") % (json.dumps(CARD), json.dumps(note_sel))


def tap_and_fail(error: str) -> dict:
    """Tap Dictate on the open card, then let the LAST recognizer made fail with `error`."""
    return {"eval": f"{CARD_DICT}.click(); window.__srs[window.__srs.length - 1].onerror({{ error: {json.dumps(error)} }}); 1"}


def _read(out: dict) -> dict:
    return json.loads(out["values"][-1])


def sign_card(tmp_path: Path, monkeypatch, device: dict, error: str, shot: str | None = None) -> dict:
    """The sign card's write-in: open it, fail Dictate, read the note."""
    rig = _Rig(tmp_path, monkeypatch)
    st = [{"init": _fake() + RECOG},
          {"until": f"{OPEN_Q} && {SIGN_BOX} && !!{CARD_DICT} && !{CARD_DICT}.hidden && 1", "ms": 30000},
          tap_and_fail(error), *([{"shot": shot}] if shot else []), {"eval": shown(SIGN_NOTE_SEL)}]
    with rig.serving():
        return _read(_drive(tmp_path, f"{rig.origin}/#write_token={TOK}", [device, rig.authenticator(), *st]))


@contextlib.contextmanager
def reply_card_served(tmp_path: Path, monkeypatch):
    """A cockpit holding one open question with options, so its card draws the reply box (words, Mic, Dictate, send words)."""
    home = tmp_path / "levain_home"
    monkeypatch.setenv("LEVAIN_HOME", str(home))
    (home / "consent").mkdir(parents=True, mode=0o700)
    QuestionStore(home / "consent").file(agent="codex", agent_session_id="s3", what="Which way for the sweep?", blocks="the sweep waits", cwd="/w",
                                         options=[{"key": "a", "label": "left"}, {"key": "b", "label": "right"}], rec="a")
    _root, src, _ids = _install(tmp_path)
    with _serve(src, surface_tokens={"browser": "T"}, install_class="reported") as (url, _httpd):
        yield url


def reply_steps(device: dict, error: str, shot: str | None = None) -> list[dict]:
    return [device, {"init": _fake() + RECOG},
            {"until": "[...document.querySelectorAll('#ck-needs .needs-question')].length === 1", "ms": 30000},
            {"eval": "[...document.querySelectorAll('#ck-needs .needs-question')][0].click(); 1"},
            {"until": f"{CARD_DICT} && !{CARD_DICT}.hidden && 1", "ms": 30000},
            tap_and_fail(error), *([{"shot": shot}] if shot else []), {"eval": shown(REPLY_NOTE_SEL)}]


def reply_card(tmp_path: Path, monkeypatch, device: dict, error: str, shot: str | None = None) -> dict:
    with reply_card_served(tmp_path, monkeypatch) as url:
        return _read(_drive(tmp_path, f"{url}/#write_token=T", reply_steps(device, error, shot)))


def whole(got: dict, width: int) -> None:
    assert got["text"] == REASON and got["shown"] and got["onScreen"] and got["inCard"] and got["cut"] == [] and not got["selfCut"] and got["vw"] == width, json.dumps(got)


@needs_browser
@needs_ssh
def test_a_dictate_failure_says_its_reason_on_the_sign_card_at_desktop_width(tmp_path: Path, monkeypatch) -> None:
    """Red with: onerror folding every error but not-allowed into "Dictation stopped." (the card gives no reason); the note
    kept out of the card's box or clipped by an ancestor."""
    whole(sign_card(tmp_path, monkeypatch, DESK_1280, "language-not-supported"), 1280)


@needs_browser
@needs_ssh
def test_a_dictate_failure_says_its_reason_on_the_sign_card_at_phone_width(tmp_path: Path, monkeypatch) -> None:
    """Red with: the same fold; and the longest reason running off the bottom of the phone card or under a clipping ancestor."""
    whole(sign_card(tmp_path, monkeypatch, PHONE_390, "language-not-supported"), 390)


@needs_browser
def test_a_dictate_failure_says_its_reason_on_the_question_card_at_desktop_width(tmp_path: Path, monkeypatch) -> None:
    """Red with: the question card's reply box passing no note to LevainMic.attach (the failure is silent there)."""
    whole(reply_card(tmp_path, monkeypatch, DESK_1280, "language-not-supported"), 1280)


@needs_browser
def test_a_dictate_failure_says_its_reason_on_the_question_card_at_phone_width(tmp_path: Path, monkeypatch) -> None:
    """Red with: the same; the reply box's note is a span under the buttons, so this is the narrow place it could be cut."""
    whole(reply_card(tmp_path, monkeypatch, PHONE_390, "language-not-supported"), 390)


@needs_browser
def test_a_second_dictate_after_a_failure_clears_the_note_on_the_question_card(tmp_path: Path, monkeypatch) -> None:
    """Red with: the click path never clearing the previous reason (the card says "No speech was heard" while dictation works)."""
    with reply_card_served(tmp_path, monkeypatch) as url:
        steps = reply_steps(DESK_1280, "no-speech")
        steps += [{"eval": f"{CARD_DICT}.click(); window.__srs.length + '|' + document.querySelector('{CARD} {REPLY_NOTE_SEL}').textContent"}]
        out = _drive(tmp_path, f"{url}/#write_token=T", steps)
    assert out["values"][-1] == "2|", out["values"]

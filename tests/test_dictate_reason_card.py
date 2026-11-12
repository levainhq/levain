"""Row 185, the Dictate half: a Dictate that fails says why, in words, on the real cockpit card at desktop and phone width.

The unit-level table of reasons lives in tests/test_card_mic_lifecycle.py (a bare ``LevainMic.attach`` box); this file runs
the same failure through the cockpit's own question card in headless Chrome and reads what the card SHOWS: the words are
there, drawn (not hidden or empty) and inside the viewport (not clipped or wider than the screen)."""
from __future__ import annotations

import json
from pathlib import Path

from tests.test_card_mic_lifecycle import OPEN_Q, RECOG, SIGN_BOX
from tests.test_cockpit_flip_parity import _drive, needs_browser
from tests.test_cockpit_sign_card import TOK, _Rig, needs_ssh
from tests.test_mic_page import _fake

DESK_1280 = {"emulate": {"w": 1280, "h": 800, "dpr": 1}}
PHONE_390 = {"emulate": {"w": 390, "h": 844, "dpr": 2, "mobile": True}}

REASON = "This browser has no on-device dictation pack for this language yet, so Dictate cannot run; the keyboard dictation key works."
CARD_DICT = "document.querySelector('#levain-modal .needs-question .card-mic-dictate')"
CARD_NOTE = "document.querySelector('#levain-modal .needs-question .sign-note')"

# what the card shows for the note: its words, whether it is drawn, and whether it fits the screen
SHOWN = (f"(() => {{ const n = {CARD_NOTE}; if (!n) return JSON.stringify(null); const r = n.getBoundingClientRect(), s = getComputedStyle(n);"
         " return JSON.stringify({ text: n.textContent, shown: s.display !== 'none' && s.visibility !== 'hidden' && r.width > 0 && r.height > 0,"
         " inside: r.left >= 0 && r.right <= window.innerWidth + 0.5, fits: n.scrollWidth <= n.clientWidth + 1, vw: window.innerWidth }); })()")


def steps(device: dict, error: str, shot: str | None = None) -> list[dict]:
    """The drive: open the question card, tap Dictate, let the recognizer fail with `error`, read what the card shows."""
    s = [
        device, {"init": _fake() + RECOG},
        {"until": f"{OPEN_Q} && {SIGN_BOX} && !!{CARD_DICT} && !{CARD_DICT}.hidden && 1", "ms": 30000},
        {"eval": f"{CARD_DICT}.click(); window.__srs[0].onerror({{ error: {json.dumps(error)} }}); 1"},
    ]
    if shot:
        s.append({"shot": shot})
    return [*s, {"eval": SHOWN}]


def _shown(tmp_path: Path, monkeypatch, device: dict, error: str, shot: str | None = None) -> dict:
    rig = _Rig(tmp_path, monkeypatch)
    st = steps(device, error, shot)
    with rig.serving():
        out = _drive(tmp_path, f"{rig.origin}/#write_token={TOK}", [st[0], rig.authenticator(), *st[1:]])
    return json.loads(out["values"][-1])


@needs_browser
@needs_ssh
def test_a_dictate_failure_says_its_reason_on_the_card_at_desktop_width(tmp_path: Path, monkeypatch) -> None:
    """Red with: onerror folding every error but not-allowed into "Dictation stopped." (the card gives no reason)."""
    got = _shown(tmp_path, monkeypatch, DESK_1280, "language-not-supported")
    assert got["text"] == REASON and got["shown"] and got["inside"] and got["fits"], got


@needs_browser
@needs_ssh
def test_a_dictate_failure_says_its_reason_on_the_card_at_phone_width(tmp_path: Path, monkeypatch) -> None:
    """Red with: the same fold; and a reason too wide for the phone card would show as not `fits` / not `inside`."""
    got = _shown(tmp_path, monkeypatch, PHONE_390, "language-not-supported")
    assert got["text"] == REASON and got["shown"] and got["inside"] and got["fits"] and got["vw"] == 390, got

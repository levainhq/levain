"""SIGNCOV (1104-8): the sign sheet's behaviours whose only coverage lived in the retired phone port's drives (deleted in
ONESURFACE OS-6), ported to the sheet's own DOM and the real cockpit ceremony in headless Chrome (the same harness as
test_sign_sheet_page.py). Each test fails against the matching mutation of ``levain/templates/web/sign.js`` (see
project_memory/1104-8/RETURN_signcov.md)."""
from __future__ import annotations

from pathlib import Path

from tests.test_cockpit_flip_parity import _drive, needs_browser
from tests.test_cockpit_sign_card import DESKTOP, QRID, TOK, _Rig, needs_ssh
from tests.test_sign_sheet_page import (
    CLOSE_IN_FRAME, FRAME, HANG, IN, LOST_AFTER_SEND, LOST_BEFORE_SEND, NOTE, OPEN_FRAMED, SPY, STATE, WAIT_RELOAD,
    _button, _open, _stub,
)

# a stub reply that counts each call it takes (in the window the stub was installed from) and then hangs
COUNT_HANG = f"(real, u, o) => {{ window.__chn = (window.__chn || 0) + 1; return ({HANG})(real, u, o); }}"


@needs_browser
@needs_ssh
def test_a_double_tap_on_sign_is_one_ceremony_and_one_answer(tmp_path: Path, monkeypatch) -> None:
    rig = _Rig(tmp_path, monkeypatch)
    with rig.serving():
        out = _drive(tmp_path, f"{rig.origin}/nope", [
            DESKTOP, rig.authenticator(), *_open(rig), {"eval": SPY},
            # two taps in one task: the second lands while the first ceremony is still in flight
            {"eval": f"(() => {{ const b = {_button('b')}; b.click(); b.click(); return 1; }})()"},
            {"until": f"/^signed/.test({NOTE}) && {NOTE}", "ms": 30000},
            {"eval": "window.__gets"},
            {"eval": STATE},
        ])
    v = out["values"]
    assert v[5] == 1 and v[6] == "signed", v                          # one passkey ceremony, one outcome
    posts = [p.split(" ", 1)[0] for p in out["posts"]]
    assert posts == [f"/cockpit/sign/{QRID}/challenge", f"/cockpit/sign/{QRID}/answer"], posts
    assert rig.verifies(QRID)


@needs_browser
@needs_ssh
def test_a_fault_after_the_answer_was_sent_says_unconfirmed_never_not_signed(tmp_path: Path, monkeypatch) -> None:
    """The answer reached the cockpit, its reply was lost, and the status read fails too: the sheet cannot know. It says
    so, keeps every control frozen and offers Check again; it never says the sign did not happen."""
    rig = _Rig(tmp_path, monkeypatch)
    with rig.serving():
        out = _drive(tmp_path, f"{rig.origin}/nope", [
            DESKTOP, rig.authenticator(), *_open(rig), _stub(),
            {"eval": f"window.__stub['/answer'] = {LOST_AFTER_SEND}; window.__stub['/attempt'] = {LOST_BEFORE_SEND}; "
                     f"{_button('b')}.click() || 1"},
            {"until": "!!document.getElementById('sheet-recheck') && 1", "ms": 30000},
            {"eval": NOTE},
            {"eval": "(document.getElementById('sheet-signing') || {}).textContent"},
            {"eval": "[...document.querySelectorAll('#sheet-actions button')]"
                     ".filter((b) => b.id !== 'sheet-recheck' && !b.disabled).length"},
            {"eval": STATE},
        ])
    v = out["values"]
    assert v[5].startswith("Not confirmed"), v
    for text in (v[5], v[6]):
        assert not any(w in str(text).lower() for w in ("not applied", "nothing was signed", "not signed:")), v
    assert str(v[6]).startswith("Not confirmed:"), v                  # the signing line, relabelled, not "Not signed"
    assert v[7] == 0 and v[8] != "signed", v                          # nothing re-armed
    assert rig.verifies(QRID)                                         # and the answer had in fact landed


@needs_browser
@needs_ssh
def test_dismissing_the_sheet_mid_ceremony_keeps_the_lock_and_starts_no_second_ceremony(tmp_path: Path, monkeypatch) -> None:
    """The review-dismissal hold: a close while a ceremony is in flight neither releases the lock nor loses the pending
    state (the line reads Not confirmed and the controls stay frozen); a tap forced through starts nothing; a second
    close leaves."""
    rig = _Rig(tmp_path, monkeypatch)
    with rig.serving():
        out = _drive(tmp_path, f"{rig.origin}/#write_token={TOK}", [
            DESKTOP, rig.authenticator(), *OPEN_FRAMED,
            _stub(f"{FRAME}.contentWindow"),
            {"eval": f"{FRAME}.contentWindow.__stub['/challenge'] = {COUNT_HANG}; "
                     f"{IN}.querySelector('#sheet-actions [data-choice=\"b\"]').click() || 1"},
            {"until": "window.__chn >= 1 && 1", "ms": 30000},
            {"eval": CLOSE_IN_FRAME},
            WAIT_RELOAD,
            {"eval": f"!!{FRAME}"},                                   # the overlay stayed
            {"eval": f"[...{IN}.querySelectorAll('#sheet-actions button')].filter((b) => !b.disabled).length"},
            {"eval": f"{IN}.getElementById('sheet-signing').textContent"},
            # force a tap past the disabled attribute: the in-flight lock must still refuse it
            {"eval": f"(() => {{ const b = {IN}.querySelector('#sheet-actions [data-choice=\"b\"]'); "
                     "b.disabled = false; b.click(); return 1; })()"},
            {"eval": "new Promise((r) => setTimeout(() => r(window.__chn), 800))"},
            {"eval": CLOSE_IN_FRAME},                                  # a second close leaves
            {"until": f"!{FRAME} && 1", "ms": 5000},
        ])
    v = out["values"]
    assert v[-7] is True, v
    assert v[-6] == 0, v                                              # still frozen
    assert v[-5].startswith("Not confirmed:"), v
    assert v[-3] == 1, v                                              # no second challenge

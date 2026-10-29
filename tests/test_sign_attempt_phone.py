"""The attempt-status construct on the phone's sign card (phone.js signAnswer / signCard over the phone port's
/sign/<id>/answer, which is levain.consent.sign Signer.answer), in headless Chrome against a real phone port with a
virtual authenticator (the rig of test_phone_sign_preselect). An answer whose reply was lost is settled by the port's
word on its attempt: signed shows signed, refused gives the card back, and one still verifying keeps the card frozen
until a later listing's card asks again and finds it settled."""
from __future__ import annotations

import json
import threading
from pathlib import Path

from tests.test_cockpit_flip_parity import _drive, needs_browser
from tests.test_phone_sign_preselect import PHONE, RID, TOK, _file, _Port, _signed, needs_ssh, port  # noqa: F401

OPTION_B = "[...document.querySelectorAll('.req.sign .actions .option')].find((b) => /^\\(b\\)/.test(b.textContent))"
OPTIONS = "[...document.querySelectorAll('.req.sign .actions .option')].map((b) => b.disabled)"
CARD_NOTE = "(document.querySelector('.req.sign > .note') || {}).textContent"
LISTED = {"until": "document.querySelector('.req.sign .actions .option') && 1", "ms": 30000}
# the first POST ending in `tail` reaches the port and its reply is lost on the way back
LOSE_REPLY = ("(() => {{ const real = window.fetch; let armed = true; window.fetch = function (u, o) {{ "
              "const p = real.apply(this, arguments); if (armed && String(u).endsWith({tail})) {{ armed = false; "
              "return p.then(() => {{ throw new TypeError('the reply was lost'); }}); }} return p; }}; return 1; }})()")
# the first POST ending in `tail` never leaves the page
NEVER_SENT = ("(() => {{ const real = window.fetch; let armed = true; window.fetch = function (u, o) {{ "
              "if (armed && String(u).endsWith({tail})) {{ armed = false; return Promise.reject(new TypeError('lost')); }} "
              "return real.apply(this, arguments); }}; return 1; }})()")
# the first POST ending in `tail` reaches the port, and half a second later the page is told its reply was lost
SENT_THEN_LOST = ("(() => {{ const real = window.fetch; let armed = true; window.fetch = function (u, o) {{ "
                  "if (armed && String(u).endsWith({tail})) {{ armed = false; real.apply(this, arguments).catch(() => 0); "
                  "return new Promise((res, rej) => setTimeout(() => rej(new TypeError('the reply was lost')), 500)); }} "
                  "return real.apply(this, arguments); }}; return 1; }})()")
AFTER = "new Promise((r) => setTimeout(() => r(1), {ms}))"
ANSWER = json.dumps("/answer")


@needs_browser
@needs_ssh
def test_the_phone_card_confirms_an_answer_whose_reply_was_lost_by_its_attempt(tmp_path: Path, port: _Port) -> None:
    _file(port.qdir, RID)
    out = _drive(tmp_path, port.url + f"#phone_token={TOK}", [
        PHONE, port.authenticator(), LISTED,
        {"eval": LOSE_REPLY.format(tail=ANSWER)},
        {"eval": f"{OPTION_B}.click() || 1"},
        {"until": f"/the port confirmed it/.test({CARD_NOTE}) && {CARD_NOTE}", "ms": 30000},
    ])
    note = out["values"][-1]
    assert note.startswith("signed (b) · by " + port.pk.key.fingerprint), note
    assert (_signed(port.qdir, RID) or {}).get("choice") == "b"


@needs_browser
@needs_ssh
def test_the_phone_card_comes_back_when_the_port_refuses_an_answer_that_never_reached_it(tmp_path: Path, port: _Port) -> None:
    _file(port.qdir, RID)
    out = _drive(tmp_path, port.url + f"#phone_token={TOK}", [
        PHONE, port.authenticator(), LISTED,
        {"eval": NEVER_SENT.format(tail=ANSWER)},
        {"eval": f"{OPTION_B}.click() || 1"},
        {"until": f"/never reached/.test({CARD_NOTE}) && {CARD_NOTE}", "ms": 30000},
        {"until": f"{OPTION_B} && !{OPTION_B}.disabled && 1", "ms": 15000},
        {"eval": f"{OPTION_B}.click() || 1"},
        {"until": f"/^signed \\(b\\)/.test({CARD_NOTE}) && {CARD_NOTE}", "ms": 30000},
    ])
    v = out["values"]
    assert "sign it again" in v[-4], v
    assert (_signed(port.qdir, RID) or {}).get("choice") == "b"           # and the next tap signed it


@needs_browser
@needs_ssh
def test_the_phone_card_stays_frozen_while_the_port_verifies_and_a_later_listing_finds_it_signed(tmp_path: Path, port: _Port,
                                                                                                 monkeypatch) -> None:
    import levain.authority as authority
    from levain.consent import sign as signmod

    _file(port.qdir, RID)
    go = threading.Event()
    real = authority.verify_link_sig

    def held(*a, **k):
        go.wait(60)
        return real(*a, **k)

    monkeypatch.setattr(authority, "verify_link_sig", held)
    monkeypatch.setattr(signmod, "ATTEMPT_WAIT_S", 0.5)
    timer = threading.Timer(9.0, go.set)
    timer.daemon = True
    timer.start()
    try:
        out = _drive(tmp_path, port.url + f"#phone_token={TOK}", [
            PHONE, port.authenticator(), LISTED,
            {"eval": SENT_THEN_LOST.format(tail=ANSWER)},
            {"eval": f"{OPTION_B}.click() || 1"},
            {"until": f"/still verifying/.test({CARD_NOTE}) && {CARD_NOTE}", "ms": 30000},
            {"eval": AFTER.format(ms=2500)},                       # the redraw after the ceremony has come
            {"eval": OPTIONS},
            # a later listing: the request has left the list (it signed), and the status line says what the port confirmed
            {"until": "/the port confirmed it/.test(document.getElementById('status').textContent) && "
                      "document.getElementById('status').textContent", "ms": 40000},
        ])
    finally:
        go.set()
    v = out["values"]
    assert "not confirmed" in v[-4], v
    assert v[-2] and all(v[-2]), v                                  # the redrawn card offers nothing that signs
    assert (_signed(port.qdir, RID) or {}).get("choice") == "b"


@needs_browser
@needs_ssh
def test_the_deep_link_sign_button_is_frozen_too_while_the_port_verifies(tmp_path: Path, port: _Port, monkeypatch) -> None:
    """L3 r1 (codex MED, complement 1): the freeze reached only the card's `.actions`, so the deep link's picked
    Sign button stayed live over an unsettled answer."""
    import levain.authority as authority
    from levain.consent import sign as signmod
    from tests.test_phone_sign_preselect import _legacy_link, _v

    _file(port.qdir, RID)
    go = threading.Event()
    real = authority.verify_link_sig

    def held(*a, **k):
        go.wait(60)
        return real(*a, **k)

    monkeypatch.setattr(authority, "verify_link_sig", held)
    monkeypatch.setattr(signmod, "ATTEMPT_WAIT_S", 0.5)
    timer = threading.Timer(9.0, go.set)
    timer.daemon = True
    timer.start()
    picked = "document.querySelector('.req.sign.linked .picked button')"
    try:
        out = _drive(tmp_path, _legacy_link(port.url, RID, TOK) +f"&choice=b&v={_v(port.qdir, RID)}", [
            PHONE, port.authenticator(), {"until": picked + " && 1", "ms": 30000},
            {"eval": SENT_THEN_LOST.format(tail=ANSWER)},
            {"eval": f"{picked}.click() || 1"},
            {"until": f"/still verifying/.test({CARD_NOTE}) && {CARD_NOTE}", "ms": 30000},
            {"eval": AFTER.format(ms=2500)},
            {"eval": f"[{picked}].map((b) => !b || b.disabled)"},
        ])
    finally:
        go.set()
    v = out["values"]
    assert v[-1] == [True], v

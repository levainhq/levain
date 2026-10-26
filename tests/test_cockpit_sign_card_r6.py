"""The cockpit card's sign path against the classes the card's L3 r1 named (project_memory/1025-43/signcard/RULINGS_L3_R1.md
S1-S7), over the rig and helpers of test_cockpit_sign_card_r5.

S1: no read of the request after an answer whose reply was lost is proof nothing was signed (the server may still be
verifying): the card asks the cockpit about the answer's attempt, and an attempt the cockpit never saw is fenced, so an
answer arriving after that read is refused and the card that came back stays right. S2: a cancelled prompt keeps the remembered passkey. S3: a remembered entry that cannot be an
id is purged before the card decides who signs. S5: an attempt read that rejects is said, and the card stays frozen. S6: a re-armed card is not redrawn."""
from __future__ import annotations

import json

from tests.test_cockpit_flip_parity import _drive, needs_browser
from tests.test_cockpit_sign_card import FACET_NOTE, OPEN, OPTION_B, QRID, STRIP_FACET, TOK, _Rig, needs_ssh
from tests.test_cockpit_sign_card_r5 import (AFTER, BUTTONS, NEVER_SENT, OTHER, RECORD, _card, _remember)

# the first POST whose path ends with `tail` is answered to the page as lost at once, and reaches the server `ms` later
LATE_COMMIT = ("(() => {{ const real = window.fetch; let armed = true; window.fetch = function (u, o) {{ "
               "if (armed && String(u).endsWith({tail})) {{ armed = false; const a = arguments; const self = this; "
               "setTimeout(() => real.apply(self, a), {ms}); return Promise.reject(new TypeError('the reply was lost')); }} "
               "return real.apply(this, arguments); }}; return 1; }})()")
# every passkey prompt is cancelled
CANCEL_ALL = ("(() => { navigator.credentials.get = (o) => { window.__gets.push(((o.publicKey || {}).allowCredentials || []).length); "
              "return Promise.reject(new DOMException('cancelled', 'NotAllowedError')); }; return 1; })()")
# the read of the request by its own id throws before it leaves the page
ONE_THROWS = ("(() => {{ const real = window.fetch; window.fetch = function (u, o) {{ "
              "if (String(u).endsWith({tail})) throw new TypeError('the read failed'); return real.apply(this, arguments); }}; "
              "return 1; }})()")


@needs_browser
@needs_ssh
def test_s1_an_answer_still_on_its_way_is_fenced_by_its_attempt_read_and_never_lands(tmp_path, monkeypatch) -> None:
    rig = _Rig(tmp_path, monkeypatch)
    with rig.serving():
        out = _drive(tmp_path, f"{rig.origin}/#write_token={TOK}", [
            *_card(rig),
            {"eval": LATE_COMMIT.format(tail=json.dumps("/answer"), ms=900)},
            {"eval": f"{OPTION_B}.click() || 1"},
            {"until": f"!{OPTION_B}.disabled && {FACET_NOTE}", "ms": 30000},
            {"eval": AFTER.format(ms=1500)},                  # past the late answer's arrival
            {"eval": BUTTONS},
            {"eval": FACET_NOTE},
        ])
    v = out["values"]
    assert "never reached the cockpit" in v[5], v[5]      # the read fenced the attempt the cockpit had not seen
    assert v[7] == [False, False, False], v[7]
    assert "never reached the cockpit" in v[8], v[8]
    assert rig.nothing_signed(QRID)                       # and the answer that arrived after it was refused


@needs_browser
@needs_ssh
def test_s1_s6_an_answer_that_never_left_comes_back_on_its_attempts_refusal_and_is_not_redrawn(tmp_path, monkeypatch) -> None:
    rig = _Rig(tmp_path, monkeypatch)
    with rig.serving():
        out = _drive(tmp_path, f"{rig.origin}/#write_token={TOK}", [
            *_card(rig),
            {"eval": NEVER_SENT.format(tail=json.dumps("/answer"))},
            {"eval": f"{OPTION_B}.click() || 1"},
            {"until": f"!{OPTION_B}.disabled && {FACET_NOTE}", "ms": 30000},
            {"eval": AFTER.format(ms=2500)},
            {"eval": "window.__reloads"},
        ])
    v = out["values"]
    assert "never reached the cockpit" in v[5], v[5]
    assert v[7] == 0, v[7]                                # a card that came back is not drawn again under the person
    assert rig.nothing_signed(QRID)


@needs_browser
@needs_ssh
def test_s2_a_cancelled_prompt_keeps_the_remembered_passkey(tmp_path, monkeypatch) -> None:
    rig = _Rig(tmp_path, monkeypatch)
    with rig.serving():
        out = _drive(tmp_path, f"{rig.origin}/#write_token={TOK}", [
            *_card(rig, _remember(rig, OTHER)),
            {"eval": CANCEL_ALL},
            {"eval": f"{OPTION_B}.click() || 1"},
            {"until": f"/nothing was signed/.test({FACET_NOTE}) && {FACET_NOTE}", "ms": 30000},
            {"eval": "window.__gets"},
            {"eval": RECORD},
        ])
    v = out["values"]
    assert v[7] == [1, 0], v[7]                           # the retry still happens, once
    assert json.loads(v[8]) == {rig.pk.key.fingerprint: OTHER}, v[8]   # and the mapping survives both cancels
    assert rig.nothing_signed(QRID)


@needs_browser
@needs_ssh
def test_s3_a_remembered_entry_that_cannot_be_an_id_is_purged(tmp_path, monkeypatch) -> None:
    rig = _Rig(tmp_path, monkeypatch)
    fp = rig.pk.key.fingerprint
    with rig.serving():
        out = _drive(tmp_path, f"{rig.origin}/#write_token={TOK}", [
            *_card(rig),
            {"eval": f"window.localStorage.setItem('levain-passkeys:localhost', {json.dumps(json.dumps({fp: '', 'x': None, 'y': 5}))}) || 1"},
            {"eval": CANCEL_ALL},
            {"eval": f"{OPTION_B}.click() || 1"},
            {"until": f"/nothing was signed/.test({FACET_NOTE}) && {FACET_NOTE}", "ms": 30000},
            {"eval": RECORD},
        ])
    assert json.loads(out["values"][7]) == {}, out["values"][7]


@needs_browser
@needs_ssh
def test_s4_a_corrupt_record_is_no_record_and_the_card_signs(tmp_path, monkeypatch) -> None:
    rig = _Rig(tmp_path, monkeypatch)
    with rig.serving():
        out = _drive(tmp_path, f"{rig.origin}/#write_token={TOK}", [
            *_card(rig),
            {"eval": "window.localStorage.setItem('levain-passkeys:localhost', '{not json') || 1"},
            {"eval": f"{OPTION_B}.click() || 1"},
            {"until": f"/^signed|nothing was signed|not sure/.test({FACET_NOTE}) && {FACET_NOTE}", "ms": 30000},
            {"eval": RECORD},
        ])
    v = out["values"]
    assert str(v[5]).startswith("signed (b)"), v[5]
    assert json.loads(v[6]) == {rig.pk.key.fingerprint: "bGV2YWluLWNhcmQtY3JlZA"}, v[6]
    assert rig.verifies(QRID)


@needs_browser
@needs_ssh
def test_s5_an_attempt_read_that_rejects_is_said_and_the_card_stays_frozen(tmp_path, monkeypatch) -> None:
    rig = _Rig(tmp_path, monkeypatch)
    with rig.serving():
        out = _drive(tmp_path, f"{rig.origin}/#write_token={TOK}", [
            *_card(rig),
            {"eval": NEVER_SENT.format(tail=json.dumps("/answer"))},
            {"eval": ONE_THROWS.format(tail=json.dumps("/attempt"))},
            {"eval": f"{OPTION_B}.click() || 1"},
            {"until": f"/could not be read/.test({FACET_NOTE}) && {FACET_NOTE}", "ms": 15000},
            {"eval": BUTTONS},
        ])
    v = out["values"]
    assert "not sure this was signed" in v[6] and "could not be read" in v[6] and "not confirmed" in v[6], v[6]
    assert v[7] == [True, True, True], v[7]               # nothing is known, so the card stays frozen
    assert rig.nothing_signed(QRID)
"""levain#107: what the sign sheet tells the cockpit page about a drop it delivered (the `signed` flag of sign.js `done()`), driven for real.

The Desk view counts a Rotate Desk or Take over ask only when the cockpit raises `levain:drop-delivered` with `signed: true` (the sheet's
own flag, relayed by dashboard_core.js). The sheet sets it from the server's own reply: an envelope was TAKEN (`taken` is true, not merely
truthy), the drop was not sent as words only, and the reply NAMES the store the sheet was opened for (a reply with no store_id, or one that is
not a string, is not signed). Here the real sheet delivers a kept drop (the sheet sends one an
earlier visit left unconfirmed, as test_envelope_sheet's kept-drop test does, so no passkey ceremony is needed to reach `done()`), the
server's answer is faked inside the page, and the event the cockpit page raised is read back. The Desk view's own handling of each flag
is tests/test_desk_rotate_107b.py. The event is display only (the server verifies an envelope on its own): this pins the flag's rule,
not an authentication.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.test_cockpit_narrow_layout_rig import NEEDS, TOK, FullRig, drive_touch, drop_tap, needs_browser, needs_ssh
from tests.test_envelope_sheet import make_init
from tests.test_msgslot_hero import READY, _type

WORDS = "/rotate desk 25 Write your handoff"
KEPT_PLAIN = {"words": WORDS, "outbox_id": "ob-107", "sent_at": "2026-11-18T10:00:00.000000Z", "store_id": "st-a"}
KEPT_SIGNED = {**KEPT_PLAIN, "envelope": {"kept": "a signed envelope an earlier visit sent"}}
TAKEN = {"taken": True, "envelope_id": "ev-107"}
TAKEN_YES = {"taken": "yes", "envelope_id": "ev-107"}
NOT_TAKEN = {"taken": False, "refused": "this drop was delivered before, without this envelope"}

# the page's own fetch is wrapped: the drop POST is answered here (and nothing else), the rest goes to the rig's fake server
_SERVER = """(() => {
  const top = window.top;
  if (window === top) {
    try { document.addEventListener('levain:drop-delivered', (e) => { (top.__dd = top.__dd || []).push(e.detail); }); } catch (x) { /* none */ }
    try { localStorage.setItem('levain-drop-pending:' + location.hostname + ':ob-107', JSON.stringify(__KEPT__)); } catch (x) { /* none */ }
  }
  const prior = window.fetch.bind(window);
  window.fetch = (u, o) => {
    const path = String(u).split('?')[0];
    if (/\\/cockpit\\/coord\\/drop$/.test(path) && o && o.method === 'POST') {
      const b = JSON.parse(o.body);
      return Promise.resolve(new Response(JSON.stringify({created: true, drop: {drop_id: 'dr-107', words: b.words, sent_at: b.sent_at, receipts: []},
        envelope: __ENV__, woken: null, store_id: __STORE__}), {status: 200, headers: {'Content-Type': 'application/json'}}));
    }
    return prior(u, o);
  };
})()"""


def _delivered(tmp_path: Path, monkeypatch, kept: dict, envelope, store: str) -> list:
    """`store` is a JS literal for the reply's store_id: a quoted string, a number, or `undefined` (the key is absent from the reply)."""
    init = (make_init(store="st-a") + ";" + _SERVER.replace("__KEPT__", json.dumps(kept)).replace("__ENV__", json.dumps(envelope))
            .replace("__STORE__", store))
    steps = [{"emulate": {"w": 1280, "h": 900, "dpr": 1}}, {"init": init}, {"until": NEEDS, "ms": 30000}, {"until": READY, "ms": 30000},
             _type("tidy the changelog"), drop_tap("envelope"),
             {"until": "(window.__dd || []).length >= 1", "ms": 30000},
             {"eval": "new Promise((r) => setTimeout(() => r(1), 500))"},
             {"eval": "JSON.stringify({t: 'dd', dd: window.__dd})"}]
    rig = FullRig(tmp_path, monkeypatch)
    with rig.serving():
        out = drive_touch(tmp_path, f"{rig.origin}/#write_token={TOK}", steps)
    got = [json.loads(v) for v in out["values"] if isinstance(v, str) and v.startswith("{") and '"t":"dd"' in v]
    assert len(got) == 1, out["values"]
    return got[0]["dd"]


@needs_browser
@needs_ssh
@pytest.mark.parametrize("name, kept, envelope, store, signed", [
    ("words only", KEPT_PLAIN, None, '"st-a"', False),
    ("signed, envelope taken", KEPT_SIGNED, TAKEN, '"st-a"', True),
    ("signed, envelope not taken", KEPT_SIGNED, NOT_TAKEN, '"st-a"', False),
    ("signed, answered by another store", KEPT_SIGNED, TAKEN, '"st-b"', False),
    ("words only, answered by another store", KEPT_PLAIN, None, '"st-b"', False),
    ("signed, the reply names no store", KEPT_SIGNED, TAKEN, "undefined", False),
    ("signed, the reply's store_id is not a string", KEPT_SIGNED, TAKEN, "42", False),
    ("signed, taken is a truthy string", KEPT_SIGNED, TAKEN_YES, '"st-a"', False),
])
def test_the_sheet_says_signed_only_for_a_taken_envelope_from_the_store_it_was_opened_for(tmp_path, monkeypatch, name, kept, envelope, store, signed):
    dd = _delivered(tmp_path, monkeypatch, kept, envelope, store)
    assert dd == [{"words": WORDS, "signed": signed}], (name, dd)

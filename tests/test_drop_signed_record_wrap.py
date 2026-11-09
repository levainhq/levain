"""The record a signed drop leaves on the sheet (the signing line and the delivery note) wraps inside a phone's width.

Phill, iPhone 15 Pro Max (430 CSS px), the cockpit as a Home Screen app: after signing an envelope drop "the record of the signing
flows way off the right side of screen". The sheet draws the challenge's own line ("Signed: <line>") and a note naming the drop and
envelope ids; real values carry long unbroken tokens (a 64-hex digest, an ``SHA256:`` fingerprint, a 36-character drop id). The
page runs in headless Chrome over the cockpit rig with a passkey that answers and a server that accepts the drop."""
from __future__ import annotations

import json
from pathlib import Path

from tests.test_cockpit_narrow_layout_rig import NEEDS, TOK, FullRig, drive_touch, drop_tap, needs_browser, needs_ssh
from tests.test_envelope_sheet import FD, SIGN_ON, _in, _tag, make_init
from tests.test_msgslot_hero import READY, _tagged, _type

LONG_LINE = ("signing ev-fa26a07a" + "0123456789abcdef" * 3 + "01234567 for SHA256:k7W5K5UxuJkJIHKlROxooI4LS5ZnR8BYUbl/MU9HyAA"
             " digest sha256:" + "fedcba9876543210" * 4)
DROP_ID = "dr-9298c8d209b6afd097b8daf21609456e"
ENV_ID = "ev-fa26a07a" + "0123456789abcdef" * 3 + "01234567"

# a passkey that answers and a server that accepts the drop, laid over the envelope-sheet fakes (this runs after them in every frame)
EXTRA = """(() => {
  const prior = window.fetch.bind(window);
  const reply = (s, o) => new Response(JSON.stringify(o), {status: s, headers: {'Content-Type': 'application/json'}});
  const buf = (n) => new Uint8Array(n).fill(7).buffer;
  try { navigator.credentials.get = () => Promise.resolve({rawId: buf(16), response: {authenticatorData: buf(37), clientDataJSON: buf(60), signature: buf(70)}}); } catch (e) { /* none */ }
  window.fetch = (u, o) => {
    const url = String(u);
    if (url.indexOf('/cockpit/coord/envelope-challenge') >= 0) {
      return Promise.resolve(reply(200, {challenge: 'AAAA', payload: {}, rp_id: location.hostname, line: __LINE__, store_id: 'st-1', store_created: false}));
    }
    if (url.indexOf('/cockpit/coord/drop') >= 0 && o && o.method === 'POST') {
      const b = JSON.parse(o.body);
      return Promise.resolve(reply(200, {created: true, drop: {drop_id: __DROP__, words: b.words, sent_at: b.sent_at, receipts: []},
        envelope: {taken: true, envelope_id: __ENV__}, woken: null, store_id: 'st-1'}));
    }
    return prior(u, o);
  };
})()""".replace("__LINE__", json.dumps(LONG_LINE)).replace("__DROP__", json.dumps(DROP_ID)).replace("__ENV__", json.dumps(ENV_ID))

# every element whose right edge passes the document's width, and the root's own scroll width, in the doc it runs in
OVER = ("const w = d.documentElement.clientWidth; const out = []; for (const e of d.body.querySelectorAll('*')) { const r = e.getBoundingClientRect();"
        " if (r.width > 0 && (r.right > w + 0.5 || e.scrollWidth > e.clientWidth + 0.5)) out.push(e.tagName + '.' + (e.className || e.id) + ' ' + Math.round(r.right) + '/' + e.scrollWidth + '>' + e.clientWidth + ':' + (e.textContent || '').slice(0, 40)); }"
        " return {w: w, sw: d.documentElement.scrollWidth, win: d.defaultView.innerWidth, over: out.slice(0, 12)};")


def _run(tmp_path: Path, monkeypatch) -> dict:
    emu = {"emulate": {"w": 430, "h": 932, "dpr": 3, "mobile": True}}
    init = make_init() + ";" + EXTRA
    steps = [emu, {"touch": True}, {"init": init}, {"until": NEEDS, "ms": 30000}, {"until": READY, "ms": 30000}, _type("tidy the changelog"),
             drop_tap("envelope"),
             {"until": _in(FD, "return !!d.getElementById('drop-review') && /ev-test/.test(d.getElementById('drop-review').textContent);"), "ms": 30000},
             SIGN_ON, {"tap": f"{FD}.querySelector('#drop-sign')"},
             {"until": _in(FD, "return /delivered/.test(d.getElementById('sheet-note').textContent);"), "ms": 20000},
             {"eval": "new Promise((r) => setTimeout(() => r(1), 600))"},
             _tag("frame", FD, OVER),
             _tag("note", FD, "return {note: d.getElementById('sheet-note').textContent, signing: (d.getElementById('sheet-signing') || {}).textContent};")]
    rig = FullRig(tmp_path, monkeypatch)
    with rig.serving():
        return _tagged(drive_touch(tmp_path, f"{rig.origin}/#write_token={TOK}", steps))


@needs_browser
@needs_ssh
def test_the_record_a_signed_drop_leaves_wraps_inside_a_phones_width(tmp_path: Path, monkeypatch) -> None:
    t = _run(tmp_path, monkeypatch)
    note = t["note"]
    assert DROP_ID in note["note"] and ENV_ID in note["note"] and LONG_LINE in (note["signing"] or ""), note   # the full record stays in the page
    f = t["frame"]
    assert f["win"] <= 430, f
    assert f["sw"] <= f["w"] and not f["over"], f

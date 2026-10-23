"""The phone page's Drop tab, in headless Chrome at a phone's width (390), over the real phone server.

One run through the page: a drop sent while its reply is lost (the page's fetch throws AFTER the server accepted), so it
stays in the outbox and is sent again under the same outbox id, and the store files it once; then an envelope built by
the server, shown on the sheet, signed with Chrome's virtual authenticator holding the SAME software passkey the cockpit
enrolled, carried by its drop and verified by the store. Screenshots go to ``LEVAIN_SHOT_DIR`` when it is set (the
lane's RUN), else the test's tmp dir."""
from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from levain.coordinator.envelopes import EnvelopeStore
from tests.test_cockpit_flip_parity import _drive, needs_browser
from tests.test_dispatch_phone import repo, world  # noqa: F401  (fixtures)
from tests.test_phone_dispatch import _serve
from tests.test_phone_page_dispatch import _authenticator
from tests.test_phone_server import _TOK

pytestmark = pytest.mark.skipif(shutil.which("ssh-keygen") is None, reason="needs ssh-keygen")

NOTE = "document.getElementById('drop-note').textContent"
# the reply to the FIRST /coord/drop is lost: the request reaches the server, the page sees a network failure
LOSE_FIRST_REPLY = """(() => { const f = window.fetch; let lost = false; window.fetch = (u, o) => {
  const p = f(u, o);
  if (!lost && String(u).endsWith('/coord/drop')) { lost = true; return p.then(() => { throw new TypeError('lost'); }); }
  return p; }; return 1; })()"""


def _set(id_: str, value: str) -> str:
    import json

    return (f"(() => {{ const e = document.getElementById({json.dumps(id_)}); e.value = {json.dumps(value)};"
            " e.dispatchEvent(new Event('input', {bubbles: true})); return 1; })()")


@needs_browser
def test_drop_lost_reply_then_envelope_signed_through_the_page(world, tmp_path: Path) -> None:
    shots = Path(os.environ.get("LEVAIN_SHOT_DIR") or tmp_path)
    repo_path = str(world["repo"][0])
    with _serve(world["qdir"], world["qdir"]) as (base, _):
        url = base.replace("127.0.0.1", "localhost")
        out = _drive(tmp_path, f"{url}/#phone_token={_TOK}", [
            {"emulate": {"w": 390, "h": 844, "dpr": 2, "mobile": True}},
            _authenticator(world["phone"]),
            {"until": "document.getElementById('status').textContent !== 'reading the queue…' && 1"},
            {"eval": "document.getElementById('tab-drop').click() || 1"},
            {"shot": str(shots / "drop_before.png")},
            {"eval": LOSE_FIRST_REPLY},
            {"eval": _set("drop-words", "seeing suite gate errors from the couch, please get them fixed")},
            {"eval": "document.getElementById('drop-send').click() || 1"},
            # the lost reply: the drop stays in the outbox, said as not delivered yet
            {"until": "(() => { const n = document.querySelector('#drop-outbox .row .note'); return n && n.textContent; })()"},
            {"shot": str(shots / "drop_outbox_waiting.png")},
            {"eval": "window.dispatchEvent(new Event('online')) || 1"},
            {"until": f"/^delivered/.test({NOTE}) && {NOTE}"},
            {"until": "document.querySelectorAll('#drop-recent .row.drop').length === 1 && "
                      "document.querySelector('#drop-outbox').children.length === 0 && "
                      "document.querySelector('#drop-recent .row.drop .receipt').textContent"},
            # the envelope: the server builds it, the sheet shows its words, the sheet's Sign asks for the passkey
            {"eval": _set("drop-words", "build the fix and land it")},
            {"eval": "document.getElementById('drop-env').open = true; 1"},
            {"eval": _set("drop-repo", repo_path)},
            {"eval": _set("drop-onto", "main")},
            {"eval": _set("drop-calls", "40")},
            {"eval": "document.getElementById('drop-sign').click() || 1"},
            {"until": "(() => { const p = document.querySelector('#drop-review pre.display'); return p && p.textContent; })()"},
            {"eval": "document.getElementById('drop-send').disabled && document.getElementById('drop-sign').disabled"
                     " && document.querySelector('#drop-review .dg-val').title"},
            {"shot": str(shots / "drop_envelope_sheet.png"), "full": True},
            {"eval": "[...document.querySelectorAll('#drop-review button')].find(b => /^Sign/.test(b.textContent)).click() || 1"},
            {"until": f"/envelope ev-[0-9a-f]{{32}} taken/.test({NOTE}) && {NOTE}"},
            {"until": "document.querySelectorAll('#drop-recent .row.drop').length === 2 && 1"},
            {"eval": "document.documentElement.scrollWidth > innerWidth"},
            {"shot": str(shots / "drop_after.png"), "full": True},
        ])
    vals = out["values"]
    # every eval, until and shot collects one value, in step order (emulate and authenticator collect none)
    delivered, receipt, view, digest, taken, sideways = vals[9], vals[10], vals[17], vals[18], vals[21], vals[23]
    assert "(it had arrived before: filed once)" in delivered, delivered
    assert receipt.startswith("receipt: delivered"), receipt
    assert "inferred, not where you dropped from" in view and "NOT IN FORCE YET" in view, view
    routes = [p.split(" ", 1)[0] for p in out["posts"]]
    assert routes.count("/coord/drop") == 3 and routes.count("/coord/envelope-challenge") == 1, routes
    # the lost-reply drop and its resend carry ONE outbox id
    first_two = [p for p in out["posts"] if p.startswith("/coord/drop ")][:2]
    assert first_two[0].split(" ", 1)[1] == first_two[1].split(" ", 1)[1]
    store = EnvelopeStore(world["qdir"])
    drops = store.drops()
    assert len(drops) == 2 and {d.surface for d in drops} == {"phone"}       # filed once each
    signed = next(d for d in drops if d.envelope_id)
    e = store.envelope(signed.envelope_id)
    assert e.digest == digest and e.signer_fp == world["phone"].fp and e.signer_class == "display-phone"
    assert e.body["lands"][0]["onto_from"] == "inferred" and e.body["budget"]["tool_calls"] == 40
    assert signed.envelope_id in taken
    assert sideways is False

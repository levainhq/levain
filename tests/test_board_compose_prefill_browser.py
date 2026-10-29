"""C12 composer URL prefill, in a real browser: `/#compose=re%20<card id>%3A%20` opens the Board's "tell the coordinator" box with
the words started; the operator edits them; nothing is posted until the operator's own submit, and that goes to the one drop
route. The URL and the prefill carry no token (the page's own token sits in its storage, set by the operator beforehand).
The token checks cover the page markup and the POST body only (the token lives in localStorage, which the markup cannot show)."""
from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

from levain.cockpit import board
from levain.coordinator.store import CoordStore
from tests.test_cockpit_flip_parity import _drive, needs_browser
from tests.test_cockpit_k2a import TOKEN, _install, _serve
from tests.test_cockpit_questions import lh  # noqa: F401  (fixture)

BOX = "document.querySelector('.bd-tell textarea')"
PREFILL = "re%20c-12%3A%20"          # the flow-facing shape: no `board`, no token


class _Sheet:
    """The drop sheet as the page sees it: an identity listing and the one drop route, recording each drop it is handed."""
    rp_id = "localhost"
    STORE_ID = "S1"      # the identity this fake lists; the real route refuses (409) a drop bound to any other
    signer = SimpleNamespace(key_facts=lambda fps: {})

    def __init__(self, queue_dir):
        self.queue_dir, self.dropped = queue_dir, []

    def close(self):
        pass

    def listing(self):
        return {"coord_store_read": True, "coord_store_id": self.STORE_ID, "coord_drops": [], "coord_signers": []}

    def step(self, action, req):
        """What the real drop route (levain.coordinator.phone.CoordPhone._drop) needs: exactly its four keys, text words /
        outbox_id / sent_at (a timestamp), and the store identity this listing reported (a wrong or omitted one is 409/400)."""
        assert action == "drop", action
        assert isinstance(req, dict) and set(req) == {"words", "outbox_id", "sent_at", "store_id"}, req
        assert isinstance(req["words"], str) and isinstance(req["outbox_id"], str) and isinstance(req["sent_at"], str), req
        datetime.fromisoformat(req["sent_at"].replace("Z", "+00:00"))
        assert req["store_id"] == self.STORE_ID, req
        self.dropped.append((action, req))
        return {"created": True, "drop": {"drop_id": "d-1"}}


def _reload_on(link: str) -> list:
    """Storage as an operator's browser already holds it, then the link followed fresh (no token in the link)."""
    return [{"eval": f"localStorage.setItem('levain_write_token', {json.dumps(TOKEN)}); "
                     "sessionStorage.setItem('t0', String(performance.timeOrigin)); 1"},
            {"eval": f"history.replaceState(null, '', '/{link}'); location.reload(); 1"},
            # a fresh document, not the old one still answering the next step
            {"until": "String(performance.timeOrigin) !== sessionStorage.getItem('t0')"}]


RELOAD = 3        # steps _reload_on contributes (and so values)
POLL_MS = 5000    # the Board's default poll interval (board_view.js pollMs)


@needs_browser
def test_the_compose_link_fills_the_box_and_posts_nothing_until_the_operator_sends(tmp_path: Path, lh) -> None:  # noqa: F811
    lh.mkdir(parents=True, exist_ok=True, mode=0o700)
    CoordStore(lh)
    _root, src, _ids = _install(tmp_path)
    assert "token" not in PREFILL.lower() and TOKEN not in PREFILL
    with _serve(src, write_token=TOKEN) as (url, httpd):
        httpd.coord_sheet = sheet = _Sheet(lh)        # the board reads the queue the drop writes
        assert board.build(lh, None, drop_dir=lh)["tell"]["offered"] is True
        out = _drive(tmp_path, f"{url}/", [
            *_reload_on("#compose=" + PREFILL),
            # offered: the box is shown, focused, and the address has dropped the compose part
            {"until": f"{BOX} && {BOX}.value === 're c-12: ' && !{BOX}.hidden && document.activeElement === {BOX} && "
                      f"{BOX}.selectionStart === {BOX}.value.length && {BOX}.selectionEnd === {BOX}.value.length && "
                      f"!location.hash.includes('compose') && "
                      f"JSON.stringify([{BOX}.value, document.activeElement === {BOX}, {BOX}.selectionStart, {BOX}.selectionEnd, location.hash])"},
            {"eval": f"new Promise(r => setTimeout(r, {POLL_MS + 500}))"},          # past a full poll: still nothing sent, words kept
            {"eval": f"JSON.stringify([{BOX}.value, document.body.dataset.mode, location.hash, "
                     "document.documentElement.outerHTML.includes(" + json.dumps(TOKEN) + ")])"},
            # the operator edits the words
            {"eval": f"{BOX}.value = {BOX}.value + 'do it'; {BOX}.dispatchEvent(new Event('input', {{bubbles: true}})); {BOX}.value"},
            {"eval": "new Promise(r => setTimeout(r, 500))"},
        ])
        before_send = list(out["posts"])
        out2 = _drive(tmp_path, f"{url}/", [
            *_reload_on("#compose=" + PREFILL),
            {"until": f"{BOX} && {BOX}.value !== '' && !{BOX}.hidden"},          # the box is offered (the board has answered)
            {"eval": f"{BOX}.value = {BOX}.value + 'do it'; {BOX}.dispatchEvent(new Event('input', {{bubbles: true}})); 1"},
            # Send, explicitly: the box holds exactly one button, enabled, and it says Send
            {"eval": "(() => { const bs = [...document.querySelectorAll('.bd-tell button')]; "
                     "if (bs.length !== 1 || bs[0].disabled || bs[0].textContent !== 'Send') "
                     "return JSON.stringify(bs.map(b => [b.textContent, b.disabled])); bs[0].click(); return 'clicked'; })()"},
            {"posts": 1, "ms": 15000},
            # keep Chrome (and so the server) up until the answer has come back to the page
            {"until": "(document.querySelector('.bd-say') || {}).textContent && document.querySelector('.bd-say').textContent.startsWith('delivered')",
             "ms": 15000},
        ])
        deadline = time.monotonic() + 10         # the page saw the answer, so the sheet has it; bounded all the same
        while not sheet.dropped and time.monotonic() < deadline:
            time.sleep(0.1)
    assert [a for a, _r in sheet.dropped] == ["drop"] and sheet.dropped[0][1]["words"] == "re c-12: do it"   # and only the second page's send
    assert out2["values"][RELOAD + 2] == "clicked", out2["values"]
    vals = out["values"]
    filled = json.loads(vals[RELOAD])
    assert filled[0] == "re c-12: " and filled[1] is True and filled[2] == filled[3] == 9, filled   # plain text, focused, cursor at the end
    kept = json.loads(vals[RELOAD + 2])
    assert kept[0] == "re c-12: " and kept[1] == "board" and kept[3] is False, kept               # the Board tab, the token nowhere on the page
    assert "compose" not in kept[2], kept                                                        # the address forgets it once offered
    assert vals[RELOAD + 3] == "re c-12: do it"                                                           # editable
    assert before_send == [], before_send                                                        # the link, the polls and the edit post nothing
    drops = [p for p in out2["posts"] if p.startswith("/cockpit/coord/drop ")]                   # the operator's own submit, the one route
    assert len(out2["posts"]) == 1 and len(drops) == 1, out2["posts"]
    sent = json.loads(drops[0].split(" ", 1)[1])
    assert sent["words"] == "re c-12: do it" and sent["outbox_id"].startswith("ph-")
    assert sheet.dropped[0][1] == sent and sent["store_id"] == _Sheet.STORE_ID and sent["sent_at"], sent   # what the sheet accepted is what the page sent
    assert TOKEN not in drops[0]

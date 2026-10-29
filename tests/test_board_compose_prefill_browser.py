"""C12 composer URL prefill, in a real browser: `/#compose=re%20<card id>%3A%20` opens the Board's "tell the coordinator" box with
the words started; the operator edits them; nothing is posted until the operator's own submit, and that goes to the one drop
route. The URL and the prefill carry no token (the page's own token sits in its storage, set by the operator beforehand)."""
from __future__ import annotations

import json
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
    signer = SimpleNamespace(key_facts=lambda fps: {})

    def __init__(self, queue_dir):
        self.queue_dir, self.dropped = queue_dir, []

    def close(self):
        pass

    def listing(self):
        return {"coord_store_read": True, "coord_store_id": "S1", "coord_drops": [], "coord_signers": []}

    def step(self, action, req):
        self.dropped.append((action, req))
        return {"created": True, "drop": {"drop_id": "d-1"}}


def _reload_on(link: str) -> list:
    """Storage as an operator's browser already holds it, then the link followed fresh (no token in the link)."""
    return [{"eval": f"localStorage.setItem('levain_write_token', {json.dumps(TOKEN)}); 1"},
            {"eval": f"history.replaceState(null, '', '/{link}'); location.reload(); 1"}]


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
            {"until": f"{BOX} && {BOX}.value !== '' && !{BOX}.hidden && document.activeElement === {BOX} && !location.hash.includes('compose') && "
                      f"JSON.stringify([{BOX}.value, document.activeElement === {BOX}, {BOX}.selectionStart, {BOX}.selectionEnd, location.hash])"},
            {"eval": "new Promise(r => setTimeout(r, 1500))"},                       # a poll or two: still nothing sent
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
            {"eval": "[...document.querySelectorAll('.bd-tell button')].find(b => !b.disabled).click(); 1"},
            {"posts": 1, "ms": 15000},
        ])
    assert [a for a, _r in sheet.dropped] == ["drop"] and sheet.dropped[0][1]["words"] == "re c-12: do it"   # and only the second page's send
    vals = out["values"]
    filled = json.loads(vals[2])
    assert filled[0] == "re c-12: " and filled[1] is True and filled[2] == filled[3] == 9, filled   # plain text, focused, cursor at the end
    kept = json.loads(vals[4])
    assert kept[0] == "re c-12: " and kept[1] == "board" and kept[3] is False, kept               # the Board tab, the token nowhere on the page
    assert "compose" not in kept[2], kept                                                        # the address forgets it once offered
    assert vals[5] == "re c-12: do it"                                                           # editable
    assert before_send == [], before_send                                                        # the link, the polls and the edit post nothing
    drops = [p for p in out2["posts"] if p.startswith("/cockpit/coord/drop ")]                   # the operator's own submit, the one route
    assert len(out2["posts"]) == 1 and len(drops) == 1, out2["posts"]
    sent = json.loads(drops[0].split(" ", 1)[1])
    assert sent["words"] == "re c-12: do it" and sent["outbox_id"].startswith("ph-")
    assert TOKEN not in drops[0]

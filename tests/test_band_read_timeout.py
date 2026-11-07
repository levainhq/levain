"""The notification-tap card lookup asks the band read (`bandRead`) to pair an item's own id with its card number. That read had no client
timeout: on a hung connection the lookup waited forever and the reader saw nothing (receipt_bundle5 known edge). A hung read now times out
on the page, counts as a refusal (one retry, then the plain Needs view WITHOUT the "no longer waiting" notice)."""
from __future__ import annotations

import json
from pathlib import Path

from tests.test_cockpit_band_title import _two_questions
from tests.test_cockpit_flip_parity import _drive, needs_browser
from tests.test_cockpit_k2a import _install, _serve
from tests.test_cockpit_k4a_needs import DESKTOP
from tests.test_cockpit_needs_item_link import READ, ROWS

# every band read of the document never answers (the connection hangs; the request ends only when the page aborts it)
HANG_BAND = """(() => { const f = window.fetch; window.fetch = (...a) => {
  if (String(a[0]).indexOf('/cockpit/band.json') === 0) {
    const sig = a[1] && a[1].signal;
    return new Promise((_res, rej) => { if (sig) sig.addEventListener('abort', () => rej(new DOMException('aborted', 'AbortError'))); });
  }
  return f.apply(window, a); }; })();"""
WAIT = {"eval": "new Promise(r => setTimeout(() => r(1), 16000))"}


@needs_browser
def test_a_hung_band_read_falls_through_to_the_plain_needs_view_without_the_notice(tmp_path: Path, monkeypatch) -> None:
    _home, store = _two_questions(tmp_path, monkeypatch)
    qid = store.get(2).id               # the item's own id: no card number carries it, so only the band read can name the card
    _root, src, _ids = _install(tmp_path)
    go = {"eval": f"location.assign('/?view=needs&item={qid}#write_token=T'); 1"}
    with _serve(src, surface_tokens={"browser": "T"}, install_class="reported") as (url, _httpd):
        out = _drive(tmp_path, f"{url}/#write_token=T", [{"init": HANG_BAND}, DESKTOP, go, {"until": ROWS, "ms": 40000}, WAIT, {"eval": READ}])["values"]
    got = json.loads(out[-1])
    assert got["modal"] is False and got["search"] == "" and got["note"] == "" and got["y"] > 100, got

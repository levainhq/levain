"""1110-23: the "New version available. Tap to reload." bar (pwa_update.js) fires only when the browser sees /sw.js change by
BYTES. An install that changes a shell asset (the page's ?v= digests move) but leaves sw.js's bytes alone never produces a
waiting worker, so an already-open page never shows the bar. This asserts the seam: when the shell the server serves changes,
the /sw.js bytes it serves change with it."""
from __future__ import annotations

import re
import threading
import urllib.request
from contextlib import contextmanager
from pathlib import Path

from levain import web_server
from levain.web_server import make_server
from tests.test_web_server import _store_with_data


@contextmanager
def _up(tmp_path: Path):
    httpd = make_server(_store_with_data(tmp_path), host="127.0.0.1", port=0)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}"
    finally:
        httpd.shutdown()
        httpd.server_close()
        t.join(timeout=5)


def _get(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=5) as r:  # noqa: S310 - loopback only
        return r.read()


def test_a_changed_shell_asset_changes_the_served_worker_bytes(tmp_path: Path, monkeypatch) -> None:
    with _up(tmp_path / "a") as base:
        page_a, sw_a = _get(base + "/").decode(), _get(base + "/sw.js")
    real = web_server.load_web_asset
    monkeypatch.setattr(web_server, "load_web_asset",
                        lambda fn: real(fn) + ("\n// a new version\n" if fn == "dashboard_core.js" else ""))
    with _up(tmp_path / "b") as base:
        page_b, sw_b = _get(base + "/").decode(), _get(base + "/sw.js")
    v = lambda p: re.search(r"dashboard_core\.js\?v=([0-9a-f]{12})", p).group(1)  # noqa: E731
    assert v(page_a) != v(page_b), "precondition: the shell digest moved"
    assert sw_a != sw_b, "the shell moved but /sw.js is byte-identical: no waiting worker, so no reload bar on an open page"

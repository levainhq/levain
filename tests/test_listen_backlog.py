"""A page load's burst of concurrent connections must not be reset at the listen queue (iOS cockpit, 2026-10-25).

The stdlib servers default to ``request_queue_size = 5``. On macOS a burst past that queue is answered with a TCP
reset, so a dashboard load (the stylesheet and eight scripts at once, each its own connection through a proxy such as
``tailscale serve``) lost a random script; losing cockpit_view.js left ``window.LevainCockpitView`` undefined and the
page said "read failed: undefined is not an object (evaluating 'window.LevainCockpitView.fromManifest')".
"""
from __future__ import annotations

import collections
import concurrent.futures
import threading
import urllib.request
from pathlib import Path

import pytest

from levain.dashboard import SubstrateSource
from levain.docs_server import _DocsServer
from levain.http_guards import LISTEN_BACKLOG
from levain.init_server import _InitServer
from levain.phone_server import _RedirectServer
from levain.team.view import _ViewServer
from levain.web_server import _LevainHTTPServer, make_server
from tests.test_dashboard import _make_levain_install

BURST = 48


@pytest.mark.parametrize("cls", [_LevainHTTPServer, _RedirectServer, _InitServer, _DocsServer, _ViewServer])
def test_every_levain_http_server_takes_the_shared_backlog(cls: type) -> None:
    assert cls.request_queue_size == LISTEN_BACKLOG
    assert LISTEN_BACKLOG >= BURST


def test_a_burst_of_concurrent_asset_reads_is_all_served(tmp_path: Path) -> None:
    srv = make_server(SubstrateSource.local(_make_levain_install(tmp_path)), host="127.0.0.1", port=0)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    url = f"http://127.0.0.1:{srv.server_address[1]}/cockpit_view.js"
    gate = threading.Barrier(BURST, timeout=30)   # a thread that cannot start breaks the barrier, never hangs CI
    direct = urllib.request.build_opener(urllib.request.ProxyHandler({}))   # a CI proxy must not carry 127.0.0.1

    def one(_: int) -> str:
        gate.wait()   # every connection opens at the same moment, like a page's script tags
        try:
            with direct.open(url, timeout=30) as r:
                r.read()
                return str(r.status)
        except Exception as exc:  # noqa: BLE001 - the tally is the assertion
            return type(exc).__name__ + ": " + str(exc)[:60]

    try:
        with concurrent.futures.ThreadPoolExecutor(BURST) as ex:
            tally = collections.Counter(ex.map(one, range(BURST)))
    finally:
        srv.shutdown()
        srv.server_close()
        t.join(5)
    assert tally == {"200": BURST}, tally

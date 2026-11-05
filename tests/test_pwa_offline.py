"""1105-7 PWA defect B: a cold open with the cockpit unreachable showed a blank white page (iPhone, iPad) or only the app
icon (Pixel) instead of the cockpit's cached shell or its offline page. Reproduced in full Chrome with the cockpit's real
worker: the worker's network-first fetches had NO deadline, so a host that does not answer (Tailscale off: the packets to
the tailnet address go nowhere, nothing refuses them) held the navigation, and then every render-blocking script of the
cached shell, for the browser's own minute-long timeout. The rig installs the shell online, then makes the origin ANSWER
NOTHING (a hung request, never a refused connection), and cold-opens the URL in a new tab."""
from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from levain.web_server import make_server
from tests.test_cockpit_flip_parity import CHROME, NODE, needs_browser
from tests.test_cockpit_k2a import _install

RIG = Path(__file__).with_name("_sw_offline_rig.mjs")
OFFLINE_TEXT = "Can't reach your cockpit. Is Tailscale on?"
# the longest a person may stare at nothing: the worker's own deadline (sw.js NET_TIMEOUT_MS) plus the browser's work after it
PAINT_BUDGET_MS = 9000


def _cold_open(tmp_path: Path, *, drop_shell: bool, hang: bool = True) -> "tuple[dict[str, Any], dict[str, Any]]":
    """Install online, make the origin unreachable, cold-open in a new tab. Returns (the online state, the cold tab's report)."""
    src = _install(tmp_path)[1]
    httpd = make_server(src, host="127.0.0.1", port=0, surface_tokens={"browser": "t"}, install_class="reported")
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    port = httpd.server_address[1]
    gone = threading.Event()
    real = httpd.RequestHandlerClass.handle_one_request

    def handle_one_request(self: Any) -> None:
        # an unreachable host answers nothing: every request, on a kept-alive connection or a new one, simply waits
        if gone.is_set():
            time.sleep(600)
            return
        real(self)

    # one connection per request: no kept-alive socket of the install survives into the silent phase to answer the cold open
    http_version = httpd.RequestHandlerClass.protocol_version
    httpd.RequestHandlerClass.handle_one_request = handle_one_request       # type: ignore[method-assign]
    httpd.RequestHandlerClass.protocol_version = "HTTP/1.0"
    env = {**os.environ, "SW_OFFLINE_DROP_SHELL": "1" if drop_shell else "0", "SW_OFFLINE_WAIT_MS": "20000"}
    proc = subprocess.Popen([NODE, str(RIG), CHROME, f"http://localhost:{port}/"], stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True, env=env)
    try:
        online = json.loads(proc.stdout.readline())["online"]
        gone.set()
        if not hang:
            httpd.shutdown()
            httpd.server_close()
        proc.stdin.write("go\n")
        proc.stdin.flush()
        cold = json.loads(proc.stdout.readline())
        return online, cold
    finally:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            pass
        proc.wait(timeout=10)
        httpd.RequestHandlerClass.handle_one_request = real                  # type: ignore[method-assign]
        httpd.RequestHandlerClass.protocol_version = http_version
        for pipe in (proc.stdin, proc.stdout, proc.stderr):
            try:
                pipe.close()
            except OSError:
                pass


@needs_browser
def test_a_cold_open_with_a_silent_origin_paints_the_cached_shell(tmp_path: Path) -> None:
    online, cold = _cold_open(tmp_path, drop_shell=False)
    assert "/" in online["keys"] and online["controlled"], online     # the shell really was installed before the origin went quiet
    tab = cold["offline"]
    assert cold["ms"] is not None and cold["ms"] < PAINT_BUDGET_MS, (cold["ms"], tab.get("readyState"), tab.get("html", "")[-200:])
    assert "Morning" in tab["text"], tab["text"]       # the cockpit's own page, drawn from the cached shell


@needs_browser
def test_a_cold_open_with_a_silent_origin_and_no_shell_shows_the_offline_page(tmp_path: Path) -> None:
    online, cold = _cold_open(tmp_path, drop_shell=True)
    assert online["controlled"], online
    tab = cold["offline"]
    assert cold["ms"] is not None and cold["ms"] < PAINT_BUDGET_MS, (cold["ms"], tab.get("readyState"))
    assert OFFLINE_TEXT in tab["text"], tab["text"]


@needs_browser
def test_a_cold_open_with_a_refusing_origin_still_paints_the_shell_and_the_page(tmp_path: Path) -> None:
    """The case that always worked (the connection is refused at once): kept, so the deadline never replaces it."""
    _, cold = _cold_open(tmp_path, drop_shell=False, hang=False)
    assert cold["ms"] is not None and "Morning" in cold["offline"]["text"], cold
    _, cold = _cold_open(tmp_path / "b", drop_shell=True, hang=False)
    assert cold["ms"] is not None and OFFLINE_TEXT in cold["offline"]["text"], cold

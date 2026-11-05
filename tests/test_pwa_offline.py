"""1105-7 PWA defect B: a cold open with the cockpit unreachable showed a blank white page (iPhone, iPad) or only the app
icon (Pixel) instead of the cockpit's cached shell or its offline page. Reproduced in full Chrome with the cockpit's real
worker: the worker's network-first fetches had NO deadline, so a host that does not answer (Tailscale off: the packets to
the tailnet address go nowhere, nothing refuses them) held the navigation, and then every render-blocking script of the
cached shell, for the browser's own minute-long timeout. The rig installs the shell online, then makes the origin ANSWER
NOTHING (a hung request, never a refused connection), and cold-opens the URL in a new tab."""
from __future__ import annotations

import json
import os
import signal
import subprocess
import threading
from pathlib import Path
from typing import Any

from levain.web_server import make_server
from tests.test_cockpit_flip_parity import CHROME, NODE, needs_browser
from tests.test_cockpit_k2a import _install

RIG = Path(__file__).with_name("_sw_offline_rig.mjs")
OFFLINE_TEXT = "Can't reach your cockpit. Is Tailscale on?"
# the longest a person may stare at nothing: ONE of the worker's deadlines (sw.js NET_TIMEOUT_MS) plus the browser's work after
# it. Two stacked deadlines (the navigation's, then a render-blocking asset's: L3 pwa-ab r1) do not fit.
_SW = (Path(__file__).resolve().parent.parent / "levain" / "webpush" / "static" / "sw.js").read_text()
NET_TIMEOUT_MS = int(_SW.split("const NET_TIMEOUT_MS = ", 1)[1].split(";", 1)[0])
PAINT_BUDGET_MS = NET_TIMEOUT_MS + 2500


def _cold_open(tmp_path: Path, *, drop_shell: bool, hang: bool = True, retry: bool = False) -> "tuple[dict[str, Any], dict[str, Any]]":
    """Install online, make the origin unreachable, cold-open in a new tab. Returns (the online state, the cold tab's report).
    `retry`: then the origin answers again (the silent requests are released) and the offline page's Retry is pressed; the
    report gains `retry`, that tab's state once it shows the cockpit (its `ms` null when it never did)."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    src = _install(tmp_path)[1]
    httpd = make_server(src, host="127.0.0.1", port=0, surface_tokens={"browser": "t"}, install_class="reported")
    gone, release = threading.Event(), threading.Event()

    class Silent(httpd.RequestHandlerClass):  # type: ignore[name-defined,misc]
        """This server's own handler (the shared class is never touched). One connection per request (HTTP/1.0): no
        kept-alive socket of the install survives into the silent phase to answer the cold open."""
        protocol_version = "HTTP/1.0"

        def handle_one_request(self) -> None:
            # an unreachable host answers nothing: every request simply waits, until the test is over
            if gone.is_set():
                release.wait(600)
                return
            super().handle_one_request()

    httpd.RequestHandlerClass = Silent
    server = threading.Thread(target=httpd.serve_forever, daemon=True)
    server.start()
    port = httpd.server_address[1]
    profiles = tmp_path / "chrome-profiles"       # the rig's Chrome profile lands here, so a rig killed mid-cleanup leaks nothing outside
    profiles.mkdir(exist_ok=True)
    env = {**os.environ, "SW_OFFLINE_DROP_SHELL": "1" if drop_shell else "0", "SW_OFFLINE_WAIT_MS": "20000",
           "SW_OFFLINE_TMP": str(profiles), "SW_OFFLINE_RETRY": "1" if retry else "0", "SW_OFFLINE_RETRY_TEXT": "Morning"}
    proc = subprocess.Popen([NODE, str(RIG), CHROME, f"http://localhost:{port}/"], stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True, env=env)
    try:
        online = json.loads(proc.stdout.readline())["online"]
        if hang:
            gone.set()
        else:
            httpd.shutdown()
            httpd.server_close()
        proc.stdin.write("go\n")
        proc.stdin.flush()
        cold = json.loads(proc.stdout.readline())
        print(f"cold open ({'silent' if hang else 'refused'}, {'no shell' if drop_shell else 'shell'}): {cold['ms']} ms")
        if retry:
            gone.clear()
            release.set()
            proc.stdin.write("go\n")
            proc.stdin.flush()
            cold["retry"] = json.loads(proc.stdout.readline())
            print(f"retry after the origin returned: {cold['retry']['ms']} ms")
        return online, cold
    finally:
        # the rig stops Chrome and removes its profile itself: let it, then SIGTERM, then SIGKILL on a deadline
        for sig, wait in ((None, 10), (signal.SIGTERM, 5), (signal.SIGKILL, 10)):
            if sig is not None:
                try:
                    os.killpg(proc.pid, sig)
                except OSError:
                    pass
            try:
                proc.wait(timeout=wait)
                break
            except subprocess.TimeoutExpired:
                continue
        try:
            os.killpg(proc.pid, signal.SIGKILL)      # Chrome's helpers share the rig's process group: none outlives the test
        except OSError:
            pass
        release.set()
        httpd.shutdown()
        httpd.server_close()
        server.join(timeout=5)
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
    online, cold = _cold_open(tmp_path, drop_shell=True, retry=True)
    assert online["controlled"], online
    tab = cold["offline"]
    assert cold["ms"] is not None and cold["ms"] < PAINT_BUDGET_MS, (cold["ms"], tab.get("readyState"))
    assert OFFLINE_TEXT in tab["text"], tab["text"]
    # the origin is back: Retry is a fresh navigation (the timed-out probes were aborted, nothing queued ahead of it) and the
    # cockpit itself paints, inside one deadline's budget
    again = cold["retry"]
    assert again["ms"] is not None and again["ms"] < PAINT_BUDGET_MS and "Morning" in again["retry"]["text"], again


@needs_browser
def test_a_cold_open_with_a_refusing_origin_still_paints_the_shell_and_the_page(tmp_path: Path) -> None:
    """The case that always worked (the connection is refused at once): kept, so the deadline never replaces it."""
    _, cold = _cold_open(tmp_path, drop_shell=False, hang=False)
    assert cold["ms"] is not None and cold["ms"] < PAINT_BUDGET_MS and "Morning" in cold["offline"]["text"], cold
    _, cold = _cold_open(tmp_path / "b", drop_shell=True, hang=False)
    assert cold["ms"] is not None and cold["ms"] < PAINT_BUDGET_MS and OFFLINE_TEXT in cold["offline"]["text"], cold

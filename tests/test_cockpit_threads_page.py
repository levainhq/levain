"""CH-U1a: thread_view.js drawn in headless Chrome over the real reader (threads.serve_threads) on a seeded dispatch.db."""
from __future__ import annotations

import http.server
import json
import threading
import urllib.parse
from pathlib import Path

from levain.cockpit import threads
from tests.test_cockpit_flip_parity import _drive, needs_browser
from tests.test_cockpit_threads import clock, qdir, seeded  # noqa: F401  (fixtures)

WEB = Path(threads.__file__).resolve().parents[1] / "templates" / "web"
PAGE = Path(__file__).with_name("fixtures") / "thread_view_page.html"
SHOT = Path("/Users/phillipclapham/.claude/jobs/095d5b71/tmp/lanes/chu1a/thread_view.png")


def _serve(qdir: Path):
    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            path = urllib.parse.urlsplit(self.path).path
            if path == "/":
                body, ctype, status = PAGE.read_bytes(), "text/html; charset=utf-8", 200
            elif path == "/thread_view.js":
                body, ctype, status = (WEB / "thread_view.js").read_bytes(), "text/javascript; charset=utf-8", 200
            elif threads.is_threads_path(path):
                status, obj = threads.serve_threads(path, "", directory=qdir)
                body, ctype = threads.encode(obj), "application/json"
            else:
                body, ctype, status = b"", "text/plain", 404
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


@needs_browser
def test_the_list_opens_a_thread_and_draws_it_as_text(tmp_path, seeded):
    qdir, world = seeded
    httpd = _serve(qdir)
    try:
        out = _drive(tmp_path, f"http://127.0.0.1:{httpd.server_address[1]}/", [
            {"until": "document.querySelector('.tv-row')"},
            {"eval": "document.querySelector('.tv-row').click(); 1"},
            {"until": "document.querySelectorAll('.tv-line').length >= 5"},
            {"eval": "JSON.stringify({kinds: [...document.querySelectorAll('.tv-kind')].map(k => k.textContent),"
                     " unverified: document.querySelectorAll('.tv-unverified').length,"
                     " open: document.querySelectorAll('.tv-tool-open').length, done: document.querySelectorAll('.tv-tool-done').length,"
                     " card: document.querySelector('.tv-card').getAttribute('href'),"
                     " bold: document.querySelectorAll('.tv-body b').length,"
                     " text: document.querySelector('.tv-message .tv-body').textContent,"
                     " composer: document.querySelector('.tv-composer').textContent})"},
            {"shot": str(SHOT)},
        ])
    finally:
        httpd.shutdown()
    got = json.loads(next(v for v in out["values"] if isinstance(v, str)))
    assert got["kinds"][0] == "message" and "approval" in got["kinds"], got
    assert (got["unverified"], got["open"], got["done"]) == (1, 1, 1), got
    assert got["card"] == "/cockpit/consent/req-77.json", got
    assert got["bold"] == 0 and "<b>build</b>" in got["text"], got     # the thread's words never become markup
    assert "read-only" in got["composer"], got
    assert SHOT.is_file()

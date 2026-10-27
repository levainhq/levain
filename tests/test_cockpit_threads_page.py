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
    shot = tmp_path / "thread_view.png"
    httpd = _serve(qdir)
    try:
        out = _drive(tmp_path, f"http://127.0.0.1:{httpd.server_address[1]}/", [
            {"until": "document.querySelector('.tv-row')"},
            {"eval": "document.querySelector('.tv-row').click(); 1"},
            {"until": "document.querySelectorAll('.tv-line').length >= 5"},
            {"eval": "JSON.stringify({kinds: [...document.querySelectorAll('.tv-kind')].map(k => k.textContent),"
                     " unverified: document.querySelectorAll('.tv-unverified').length,"
                     " open: document.querySelectorAll('.tv-tool-open').length, done: document.querySelectorAll('.tv-tool-done').length,"
                     " links: document.querySelectorAll('.tv-line a').length,"
                     " approval: document.querySelector('.tv-vendor_approval_request .tv-body').textContent,"
                     " bold: document.querySelectorAll('.tv-body b').length,"
                     " text: document.querySelector('.tv-message .tv-body').textContent,"
                     " composer: document.querySelector('.tv-composer').textContent})"},
            {"shot": str(shot)},
        ])
    finally:
        httpd.shutdown()
        httpd.server_close()
    got = json.loads(next(v for v in out["values"] if isinstance(v, str)))
    assert got["kinds"][0] == "message" and "approval" in got["kinds"], got
    assert (got["unverified"], got["open"], got["done"]) == (1, 1, 1), got
    assert got["links"] == 0 and "answer it in Needs you" in got["approval"], got
    assert got["bold"] == 0 and "<b>build</b>" in got["text"], got     # the thread's words never become markup
    assert "read-only" in got["composer"], got
    assert shot.is_file()


_RACE = """
(() => {
  const host = document.createElement("div"); document.body.appendChild(host);
  const pending = {};
  const f = (url) => new Promise((ok, no) => { pending[url] = {ok, no}; });
  const body = (name) => ({ok: true, status: 200, text: () => Promise.resolve(JSON.stringify(
    {thread_id: name, gap: false, composer: null, before: [], stream: []}))});
  const tv = LevainThreadView.mount(host, {fetch: f, pollMs: 0});
  const A = "/cockpit/thread/A.json", B = "/cockpit/thread/B.json";
  tv.open("A"); tv.open("B");
  pending[B].ok(body("B"));
  const first = () => host.querySelector(".tv-title") && host.querySelector(".tv-title").textContent;
  setTimeout(() => {
    const afterB = first();
    pending[A].ok(body("A"));                        // A answers late: it must not repaint B
    setTimeout(() => {
      const afterA = first();
      tv.open("C");                                  // C is then abandoned for D; C fails late: it must not overwrite D
      tv.open("D");
      pending["/cockpit/thread/D.json"].ok(body("D"));
      setTimeout(() => {
        pending["/cockpit/thread/C.json"].no(new Error("late"));
        setTimeout(() => { window.__race = [afterB, afterA, first(), host.querySelector(".tv-note") && host.querySelector(".tv-note").textContent]; }, 30);
      }, 30);
    }, 30);
  }, 30);
})()
"""


@needs_browser
def test_a_late_reply_for_an_abandoned_navigation_never_repaints(tmp_path, seeded):
    qdir, _world = seeded
    httpd = _serve(qdir)
    try:
        out = _drive(tmp_path, f"http://127.0.0.1:{httpd.server_address[1]}/", [
            {"until": "document.querySelector('.tv-row')"},
            {"eval": _RACE},
            {"until": "window.__race ? JSON.stringify(window.__race) : null"},
        ])
    finally:
        httpd.shutdown()
        httpd.server_close()
    race = json.loads(next(v for v in reversed(out["values"]) if isinstance(v, str)))
    assert race[:3] == ["B", "B", "D"], race
    assert race[3] in (None, "Nothing has been said in this thread yet."), race     # no error text over D


@needs_browser
def test_a_launch_with_an_unreadable_composer_is_not_called_launchless(tmp_path, seeded):
    qdir, _world = seeded
    httpd = _serve(qdir)
    try:
        out = _drive(tmp_path, f"http://127.0.0.1:{httpd.server_address[1]}/", [
            {"until": "document.querySelector('.tv-row')"},
            {"eval": "JSON.stringify({unread: LevainThreadView.threadModel({thread_id: 't', launch_id: 'L1', composer: null}).composer,"
                     " none: LevainThreadView.threadModel({thread_id: 't', launch_id: null, composer: null}).composer,"
                     " answered: LevainThreadView.threadModel({thread_id: 't', stream: [{kind: 'vendor_approval_resolved', body: 'leak'}]})"
                     ".stream[0].text})"},
        ])
    finally:
        httpd.shutdown()
        httpd.server_close()
    got = json.loads(next(v for v in reversed(out["values"]) if isinstance(v, str)))
    assert got["unread"] == "The launch's state could not be read.", got
    assert got["none"] == "This thread has no worker launch.", got
    assert got["answered"] == "An approval was answered.", got

"""The page driver's `posts` list is what an operator's click sent. The cockpit page's own telemetry (the SW-open landing report
`/webpush/landed` and the ring-trace report `/webpush/trace`, both from `sw_open.js`) is never an operator POST, and a test that
asserts `posts == []` on a paired page must not depend on whether the worker's ring had entries 2.5 s after load.

The page here posts all of them itself, through the real driver in a real Chrome: the telemetry paths are absent from `posts`,
every other path (an operator verb, a near-miss path that only starts like a telemetry one) is present."""
from __future__ import annotations

import contextlib
import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from tests import _page_drive_run
from tests.test_cockpit_flip_parity import CHROME, DRIVE, NODE, needs_browser

# a text/plain body is a CORS-simple request: the browser sends it without a preflight, and the driver sees it either way
_PAGE = """<!doctype html><title>telemetry</title><script>
const send = (path, body) => fetch(path, { method: "POST", headers: { "content-type": "text/plain" }, body }).catch(() => {});
window.__sent = (async () => {
  await send("/webpush/trace", "trace-body");
  await send("/webpush/landed", "landed-body");
  await send("/webpush/trace?page=%2F", "trace-query-body");
  await send("/webpush/landed?k=1", "landed-query-body");
  await send("/cockpit/verb", "verb-body");
  await send("/webpush/traced", "near-miss-trace");
  await send("/webpush/landed/extra", "near-miss-landed");
  return true;
})();
</script>"""


@contextlib.contextmanager
def _serve() -> Iterator[str]:
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a) -> None:    # noqa: D401 - silence
            pass

        def do_GET(self) -> None:
            body = _PAGE.encode()
            self.send_response(200)
            self.send_header("content-type", "text/html; charset=utf-8")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self) -> None:
            self.rfile.read(int(self.headers.get("content-length") or 0))
            self.send_response(204)
            self.end_headers()

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        yield f"http://127.0.0.1:{srv.server_address[1]}/"
    finally:
        srv.shutdown()
        srv.server_close()
        t.join(timeout=5)


@needs_browser
def test_the_pages_own_telemetry_is_never_counted_as_an_operator_post(tmp_path: Path) -> None:
    steps = tmp_path / "steps.json"
    steps.write_text(json.dumps([{"eval": "window.__sent"}, {"posts": 3, "ms": 15000}]))
    with _serve() as url:
        r = _page_drive_run.run([NODE, str(DRIVE), CHROME, url, str(steps)], timeout=90)
    assert r.returncode == 0, r.stderr
    posts = json.loads(r.stdout)["posts"]
    paths = [p.split(" ", 1)[0] for p in posts]
    # telemetry (landed and trace, bare or with a query) is out; an operator verb and the two near-misses are counted, in order
    assert paths == ["/cockpit/verb", "/webpush/traced", "/webpush/landed/extra"], posts
    assert posts[0] == "/cockpit/verb verb-body", posts

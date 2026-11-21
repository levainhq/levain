"""The page driver answers alert / confirm / prompt in the page itself (the DIALOG_SHIM in tests/_page_drive.mjs), so a page can never sit
behind a native dialog: each returns what a declined dialog returns, and the driver lists each one in "dialogs", across reloads."""
from __future__ import annotations

import contextlib
import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from tests import _page_drive_run
from tests.test_cockpit_flip_parity import CHROME, DRIVE, NODE, needs_browser


@contextlib.contextmanager
def _site(html: str) -> Iterator[str]:
    """A local origin that serves `html` at every path; yields its url."""
    raw = html.encode()

    class H(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def log_message(self, *a: object) -> None:
            pass

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}/"
    finally:
        httpd.shutdown()
        httpd.server_close()
        t.join(timeout=5)


def _drive(tmp_path: Path, html: str, steps: list):
    f = tmp_path / "steps.json"
    f.write_text(json.dumps(steps))
    with _site(html) as url:
        return _page_drive_run.run([NODE, str(DRIVE), CHROME, url, str(f)], timeout=90)


@needs_browser
def test_a_prompt_returns_null_to_the_page_and_is_listed(tmp_path: Path) -> None:
    """Red with the shim answering anything but null to a prompt (the title is not "null"), or with its marker line left in the console log
    instead of "dialogs" (the list comes back empty)."""
    html = "<title>t</title><script>document.title = String(window.prompt('write token?'));</script>"
    r = _drive(tmp_path, html, [{"until": "document.title === 'null'", "ms": 15000}, {"eval": "document.title"}])
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    assert out["values"][-1] == "null", out
    assert out["dialogs"] == ["prompt: write token?"], out


@needs_browser
def test_confirm_returns_false_and_alert_returns_undefined_and_each_is_listed(tmp_path: Path) -> None:
    """Red with confirm left returning true or alert returning something else (the title differs), and with either not recorded (a name
    is missing from "dialogs")."""
    html = ("<title>t</title><script>document.title = String(window.confirm('sure?')) + ',' + String(window.alert('heads up'));</script>")
    r = _drive(tmp_path, html, [{"until": "document.title === 'false,undefined'", "ms": 15000}, {"eval": "document.title"}])
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    assert out["values"][-1] == "false,undefined", out
    assert out["dialogs"] == ["confirm: sure?", "alert: heads up"], out


@needs_browser
def test_a_dialog_in_the_new_document_after_a_reload_is_answered_and_listed_too(tmp_path: Path) -> None:
    """Red with the shim registered only for the first document (the reloaded document's window.prompt is Chrome's own: "[native code]"),
    or with the record reset by a reload (only the second dialog is listed). The page prompts on every load."""
    html = ("<title>t</title><script>const n = Number(sessionStorage.getItem('n') || 0) + 1; sessionStorage.setItem('n', String(n));"
            "window.prompt('prompt-' + n); document.title = 'loaded-' + n;</script>")
    r = _drive(tmp_path, html, [{"until": "document.title === 'loaded-1'", "ms": 15000}, {"eval": "location.reload(); 1"},
                                {"until": "document.title === 'loaded-2'", "ms": 20000}, {"eval": "String(window.prompt)"}])
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    assert out["dialogs"] == ["prompt: prompt-1", "prompt: prompt-2"], out
    assert "[native code]" not in out["values"][-1], out


@needs_browser
def test_the_shim_is_what_answers_the_dialog_not_the_browser(tmp_path: Path) -> None:
    """Red with the shim script not registered: window.prompt is then Chrome's own function and its source reads "[native code]"."""
    r = _drive(tmp_path, "<title>t</title>", [{"eval": "String(window.prompt)"}, {"eval": "String(window.confirm)"}, {"eval": "String(window.alert)"}])
    assert r.returncode == 0, r.stderr
    values = json.loads(r.stdout)["values"]
    assert len(values) == 3 and all("[native code]" not in v for v in values), values

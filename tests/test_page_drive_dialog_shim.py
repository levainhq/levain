"""The page driver answers alert / confirm / prompt in the page itself (the DIALOG_SHIM in tests/_page_drive.mjs): each returns what a declined
dialog returns, and the driver lists each one in "dialogs". These browser cases cover a shim that is installed and working in the main document,
across one reload. They do not cover a failed shim registration, a failed Runtime.enable, the CDP decline of a dialog the shim cannot catch
(beforeunload), or the wait at the end of the drive for a decline still in flight."""
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
    """Red with the shim answering a prompt with anything but null (the title is not "null"), or with the shim's marker line not turned into a
    "dialogs" entry (the list comes back empty or differs). Not red with the shim removed: a prompt Chrome's own handler declines also
    returns null and is listed, so only the shim-specific outcomes above fail this case."""
    html = "<title>t</title><script>document.title = String(window.prompt('write token?'));</script>"
    r = _drive(tmp_path, html, [{"until": "document.title === 'null'", "ms": 15000}, {"eval": "document.title"}])
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    assert out["values"][-1] == "null", out
    assert out["dialogs"] == ["prompt: write token?"], out


@needs_browser
def test_confirm_returns_false_and_alert_returns_undefined_and_each_is_listed(tmp_path: Path) -> None:
    """Red with the shim's confirm returning anything but false or its alert returning anything but undefined (the title differs), and with
    either not turned into a "dialogs" entry (one is missing). Not red with the shim removed: a declined confirm is false, a declined alert
    undefined, and both are listed, so only the shim-specific outcomes above fail this case."""
    html = ("<title>t</title><script>document.title = String(window.confirm('sure?')) + ',' + String(window.alert('heads up'));</script>")
    r = _drive(tmp_path, html, [{"until": "document.title === 'false,undefined'", "ms": 15000}, {"eval": "document.title"}])
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    assert out["values"][-1] == "false,undefined", out
    assert out["dialogs"] == ["confirm: sure?", "alert: heads up"], out


@needs_browser
def test_a_dialog_in_the_new_document_after_a_reload_is_answered_and_listed_too(tmp_path: Path) -> None:
    """The page prompts on every load. Red with the shim not in force in the reloaded document: its window.prompt is then Chrome's own and
    reads "[native code]" (the last assertion). The "dialogs" assertion is not red with the shim removed (Chrome's declined prompt is
    listed too, unless it refuses the decline mid-reload, which is the flake the shim exists to remove and which no run of this case can
    be relied on to produce), so only the "[native code]" assertion is shim-specific here."""
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
    """Red with the shim script not registered at all: window.prompt, confirm and alert are then Chrome's own and read "[native code]". It
    checks that the shim is installed, not what it answers or records."""
    r = _drive(tmp_path, "<title>t</title>", [{"eval": "String(window.prompt)"}, {"eval": "String(window.confirm)"}, {"eval": "String(window.alert)"}])
    assert r.returncode == 0, r.stderr
    values = json.loads(r.stdout)["values"]
    assert len(values) == 3 and all("[native code]" not in v for v in values), values

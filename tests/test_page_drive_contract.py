"""The page driver's contract for failure and retry: a thrown eval fails the drive unless the step may error (a navigating eval
never fails it), an act-until step repeats its action with its assertion and only while the condition is false, and one
environment scale stretches every deadline."""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from tests import _page_drive_run
from tests.test__waits import SCALE_TABLE
from tests.test_cockpit_flip_parity import CHROME, DRIVE, NODE, needs_browser


def _drive(tmp_path: Path, steps: list, timeout: float = 60):
    f = tmp_path / "steps.json"
    f.write_text(json.dumps(steps))
    return _page_drive_run.run([NODE, str(DRIVE), CHROME, "about:blank", str(f)], timeout=timeout)


@needs_browser
def test_an_eval_that_throws_fails_the_drive_and_names_the_step_and_the_error(tmp_path: Path) -> None:
    r = _drive(tmp_path, [{"eval": "1"}, {"eval": "(() => { throw new Error('boom-in-the-page'); })()"}])
    assert r.returncode == 1 and "step 1" in r.stderr and "boom-in-the-page" in r.stderr, (r.returncode, r.stderr)
    assert (tmp_path / "timeout-step-1.png").stat().st_size > 0 and (tmp_path / "timeout-step-1.txt").exists(), r.stderr
    r = _drive(tmp_path, [{"eval": "document.querySelector('#nope').click(); 1"}])      # a missing node: the common lost-event shape
    assert r.returncode == 1 and "step 0" in r.stderr and "eval failed" in r.stderr, (r.returncode, r.stderr)


@needs_browser
def test_may_error_collects_the_error_in_place_of_a_value(tmp_path: Path) -> None:
    r = _drive(tmp_path, [{"eval": "(() => { throw new Error('expected-throw'); })()", "may_error": True}, {"eval": "2"}])
    assert r.returncode == 0, r.stderr
    values = json.loads(r.stdout)["values"]
    assert "expected-throw" in values[0]["error"] and values[1] == 2, values


@needs_browser
def test_act_until_runs_its_action_again_while_the_condition_is_false(tmp_path: Path) -> None:
    # the action only takes effect on its second run, as a click does that a repaint ate the first time
    do = "window.__n = (window.__n || 0) + 1; if (window.__n >= 2) window.__ok = 1; 1"
    r = _drive(tmp_path, [{"do": do, "until": "window.__ok", "ms": 10000, "every": 300}, {"eval": "window.__n"}])
    assert r.returncode == 0, r.stderr
    values = json.loads(r.stdout)["values"]
    assert values[0] is True or values[0] == 1, values
    assert values[1] >= 2, values


def _drive_at(tmp_path: Path, url: str, steps: list, timeout: float = 60):
    f = tmp_path / "steps.json"
    f.write_text(json.dumps(steps))
    return _page_drive_run.run([NODE, str(DRIVE), CHROME, url, str(f)], timeout=timeout)


# the cockpit's pages forbid in-page eval (script-src 'self', no 'unsafe-eval'); a re-run must still act there
_STRICT_CSP_PAGE = "data:text/html,<meta http-equiv='Content-Security-Policy' content=\"script-src 'self'\"><p>strict</p>"


@needs_browser
@pytest.mark.parametrize("do", [
    "window.__n = (window.__n || 0) + 1; if (window.__n >= 2) window.__ok = 1; 1",       # a statement list
    "(window.__n = (window.__n || 0) + 1) >= 2 ? (window.__ok = 1) : 0",                 # one expression
    "let c = (window.__n = (window.__n || 0) + 1); if (c >= 2) window.__ok = 1; 1",      # declares a name of its own
])
@pytest.mark.parametrize("until", ["window.__ok", "new Promise(r => setTimeout(() => r(window.__ok), 20))"])
def test_act_until_re_runs_its_action_under_a_page_csp_that_forbids_eval(tmp_path: Path, do: str, until: str) -> None:
    # in-page eval is allowed only until an evaluation awaits something; a promise condition makes the re-run await first
    r = _drive_at(tmp_path, _STRICT_CSP_PAGE, [{"do": do, "until": until, "ms": 8000, "every": 300}, {"eval": "window.__n"}])
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout)["values"][1] >= 2, r.stdout


@needs_browser
def test_act_until_awaits_a_promise_condition_on_a_re_run(tmp_path: Path) -> None:
    # the condition resolves false: a re-run must not read the pending promise as true and skip the action
    until = "new Promise(r => setTimeout(() => r(window.__n >= 3 ? 1 : false), 50))"
    r = _drive(tmp_path, [{"do": "window.__n = (window.__n || 0) + 1; 1", "until": until, "ms": 8000, "every": 200},
                          {"eval": "window.__n"}])
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout)["values"][1] >= 3, r.stdout


@needs_browser
def test_an_action_declaring_var_runs_the_same_on_every_run(tmp_path: Path) -> None:
    # a re-run scopes the action exactly as the first run did: a declaration never collides with the driver's own names
    r = _drive(tmp_path, [{"do": "var c = (window.__n || 0) + 1; window.__n = c; 1", "until": "window.__n >= 3",
                           "ms": 8000, "every": 200}, {"eval": "window.__n"}])
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout)["values"][1] >= 3, r.stdout


@needs_browser
def test_an_action_returning_done_true_does_not_end_the_step(tmp_path: Path) -> None:
    # an action's own value is never read as the driver's re-run result
    r = _drive(tmp_path, [{"do": "window.__n = (window.__n || 0) + 1; ({done: true, v: 1})", "until": "window.__n >= 2",
                           "ms": 8000, "every": 200}, {"eval": "window.__n"}])
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout)["values"][1] >= 2, r.stdout


@needs_browser
def test_act_until_posts_form_and_a_failure_names_the_last_error_of_the_action(tmp_path: Path) -> None:
    r = _drive(tmp_path, [{"do": "document.querySelector('#nope').click()", "posts": 1, "ms": 1200, "every": 300}])
    assert r.returncode == 1 and "step 0" in r.stderr and "posts never reached 1" in r.stderr, r.stderr
    assert "last error from the action" in r.stderr, r.stderr
    assert (tmp_path / "timeout-step-0.png").exists(), r.stderr
    r = _drive(tmp_path, [{"do": "window.__x = 1", "until": "false", "ms": 600, "every": 200, "may_timeout": True}])
    assert r.returncode == 0, r.stderr                                  # the opt-out is the same as `until`'s
    assert json.loads(r.stdout)["values"] == [{"timeout": "false"}]


@needs_browser
def test_the_wait_scale_multiplies_a_deadline_and_never_shortens_one(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    steps = [{"eval": "setTimeout(() => { window.__late = 1; }, 3000); 1"}, {"until": "window.__late", "ms": 2500}]
    monkeypatch.delenv("LEVAIN_TEST_WAIT_SCALE", raising=False)
    r = _drive(tmp_path, steps)
    assert r.returncode == 1 and "until never came true within 2500ms" in r.stderr, (r.returncode, r.stderr)
    monkeypatch.setenv("LEVAIN_TEST_WAIT_SCALE", "2")
    r = _drive(tmp_path, steps, timeout=90)                              # the scaled page-load gate alone can take 30 s
    assert r.returncode == 0, r.stderr
    monkeypatch.setenv("LEVAIN_TEST_WAIT_SCALE", "0.1")                  # below 1 counts as 1
    r = _drive(tmp_path, steps, timeout=90)
    assert r.returncode == 1 and "within 2500ms" in r.stderr, (r.returncode, r.stderr)


@needs_browser
@pytest.mark.parametrize("raw,want", SCALE_TABLE)
def test_the_driver_parses_the_wait_scale_as_the_python_side_does(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, raw: str, want: float) -> None:
    monkeypatch.setenv("LEVAIN_TEST_WAIT_SCALE", raw)
    r = _drive(tmp_path, [{"until": "false", "ms": 100}], timeout=90)   # fails at once; the message carries the scaled deadline
    assert r.returncode == 1 and f"within {round(100 * want)}ms" in r.stderr, (raw, r.returncode, r.stderr)


@needs_browser
def test_an_eval_that_navigates_passes_and_any_other_eval_error_still_fails(tmp_path: Path) -> None:
    r = _drive(tmp_path, [{"eval": "location.href = 'data:text/html,<p>x</p>'; 1"}, {"eval": "2"}])
    assert r.returncode == 0, r.stderr
    values = json.loads(r.stdout)["values"]
    assert values[0] in (1, {"navigated": True}) and values[-1] == 2, values
    # an eval left waiting on a promise while the page reloads is the shape Chrome answers with a destroyed-context error
    r = _drive(tmp_path, [{"eval": "setTimeout(() => location.reload(), 50); new Promise(() => {})"}, {"eval": "3"}])
    assert r.returncode == 0, r.stderr
    values = json.loads(r.stdout)["values"]
    assert values[0] == {"navigated": True} and values[-1] == 3, values
    r = _drive(tmp_path, [{"eval": "null.x; 1"}])
    assert r.returncode == 1 and "eval failed" in r.stderr and "TypeError" in r.stderr, (r.returncode, r.stderr)
    # an error that only SAYS navigation, with no navigation started, is not a navigation: it fails (no false green)
    r = _drive(tmp_path, [{"eval": "(() => { throw new Error('Execution context was destroyed (not really)'); })()"}])
    assert r.returncode == 1 and "eval failed" in r.stderr, (r.returncode, r.stderr)


@needs_browser
def test_act_until_does_not_run_its_action_again_once_the_condition_came_true_between_polls(tmp_path: Path) -> None:
    # no page timer, so nothing here can run late. The condition counts its own reads and is true from the 2nd: the driver's loop is
    # first run -> read 1 (false) -> 100 ms poll sleep, and the retry tick (every 50, below that sleep) is due before the 2nd poll, so the
    # retry's own check-first read is read 2 (true) and the action never runs again. Without that guard the retry acts (__n == 2)
    do = "window.__n = (window.__n || 0) + 1; 1"
    until = "(window.__r = (window.__r || 0) + 1) >= 2"
    r = _drive(tmp_path, [{"do": do, "until": until, "ms": 10000, "every": 50}, {"eval": "window.__n"}])
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout)["values"][-1] == 1, r.stdout


@needs_browser
def test_a_timeout_prints_the_page_state_after_its_named_line_and_keeps_the_exit_code(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LEVAIN_TEST_WAIT_SCALE", "0.1")                  # below 1 counts as 1: CI's own scale would double the 1500 ms the line names
    page = ("data:text/html,<div id='levain-modal'><p data-question-row>a</p><p data-question-row>b</p></div>"
            "<script>console.log('hello-' + 'from-the-page'); setTimeout(() => { throw new Error('late-' + 'page-error'); }, 100)</script>")
    r = _drive_at(tmp_path, page, [{"until": "document.querySelectorAll('#levain-modal [data-question-row]').length === 7", "ms": 1500}])
    assert r.returncode == 1 and "until never came true within 1500ms" in r.stderr, (r.returncode, r.stderr)
    head, _, block = r.stderr.partition("--- page state at the timeout ---")
    assert block, r.stderr                                           # the block follows the unchanged line, never replaces it
    for label in ("PAGE URL:", "READY STATE: complete", "MODAL: #levain-modal exists=true [data-question-row] count=2",
                  "CONSOLE (last 20", "console.log: hello-from-the-page", "page error: Error: late-page-error", "BODY TEXT (first 600):", "SCREENSHOT: ",
                  "--- end page state ---"):
        assert label in block, (label, r.stderr)
    assert len(block) < 4100, len(block)


_SLOW_WORKER = "self.addEventListener('install', () => self.skipWaiting());\nself.addEventListener('activate', (e) => e.waitUntil(new Promise((r) => setTimeout(r, 2000)).then(() => self.clients.claim())));\n"
_WORKER_PAGE = ("<!doctype html><title>w</title><p>w</p><script>navigator.serviceWorker.register('/sw.js', {scope: '/'});\n"
                "addEventListener('beforeunload', () => sessionStorage.setItem('ctl', String(!!navigator.serviceWorker.controller)));</script>")


@needs_browser
def test_an_eval_that_reloads_waits_for_the_pages_worker_to_take_control(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # CI, the List LATER phone test, 1 run in 4: the page reloaded while its worker was still installing, the navigation never committed and
    # the page stopped answering (reproduced at a 10x CPU throttle: 11 of 27 runs; 0 of 14 with the wait). The `init` step has always waited
    # for the worker before its reload; an `eval` that reloads waits the same way. The worker here takes 2000 ms to activate (the driver's wait is bounded at 5 s, so the waited reload keeps slack under the bound, and the unwaited one goes out as soon as the page has loaded), so a reload
    # sent at once reads no controller at the moment the page unloads; a reload that waited reads one.
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class H(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            body, ctype = (_SLOW_WORKER, "text/javascript") if self.path == "/sw.js" else (_WORKER_PAGE, "text/html")
            raw = body.encode()
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def log_message(self, *a: object) -> None:
            pass

    monkeypatch.setenv("LEVAIN_DRIVE_FULL_CHROME", "1")                  # headless-shell has no service worker; this drive declares it needs one
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        r = _drive_at(tmp_path, f"http://127.0.0.1:{httpd.server_address[1]}/",
                      [{"eval": "location.reload(); 1"}, {"until": "document.readyState === 'complete' && sessionStorage.getItem('ctl') !== null", "ms": 20000},
                       {"eval": "sessionStorage.getItem('ctl')"}], timeout=90)
    finally:
        httpd.shutdown()
        httpd.server_close()
        t.join(timeout=5)
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout)["values"][-1] == "true", r.stdout


@needs_browser
def test_a_timeout_on_a_page_that_stopped_answering_says_what_the_browser_and_the_page_last_said(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # CI, three page-drive hangs in one morning (board row 1698 and after): a page that stops answering the debugging channel came back as
    # "PAGE URL: ?  READY STATE: ?", which cannot tell a renderer that hung, one that crashed and a target that is gone. The block now asks
    # the BROWSER whether the page is listed, and prints the last page event and the navigation trail.
    monkeypatch.setenv("LEVAIN_TEST_WAIT_SCALE", "1")
    # the hang is started by the drive itself, AFTER setup and the emulate round trip (a timer that fires 50 ms after an eval that has already
    # returned), so no setup poll can race it; the failing until then runs against a page that no longer answers
    page = "data:text/html,<p>hang</p>"
    r = _drive_at(tmp_path, page, [{"emulate": {"w": 430, "h": 844, "dpr": 2, "mobile": True}},
                                   {"eval": "setTimeout(() => { for (;;) {} }, 50); 1"}, {"until": "false", "ms": 1500}], timeout=90)
    assert r.returncode == 1 and "until never came true within 1500ms" in r.stderr, (r.returncode, r.stderr)
    _, _, block = r.stderr.partition("--- page state at the timeout ---")
    assert "PAGE URL: ?" in block, r.stderr                              # the page did not answer: the premise of the test
    assert "TARGET: listed" in block, r.stderr                           # the browser still lists it
    assert "LAST PAGE EVENT: " in block and "loadEventFired" in block, r.stderr   # and the page's trail names its load
    quiet = re.search(r"LAST PAGE EVENT: (\d+)ms ago", block)
    assert quiet and int(quiet.group(1)) >= 1000, r.stderr             # the page was silent for the whole wait: a reply to the driver's own probe is not the page speaking
    assert len(block) < 4100, len(block)

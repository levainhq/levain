"""The cockpit's fragment write-token intake (`takeFragmentWriteToken`, dashboard_boot.js) must not replace a stored token
with a value the server has not accepted. A seat reply can carry an allowed external link that 302-redirects to the cockpit
origin with `#write_token=<bad>`; the intake used to store it at once and clear the paired mark, which forces the owner to
pair again. Driven in headless Chrome: the page is loaded once (so a token is stored), then navigated to a fresh URL that
carries the fragment, and what the browser then holds is read back. Not driven here: a real redirect from another origin
(the landing page is the same one a typed link reaches)."""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Any

import pytest

from tests._page_drive_run import run as run_driver
from tests.test_cockpit_k2a import _install, _serve

CHROME = os.environ.get("CHROME") or "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
NODE = shutil.which("node")
DRIVE = Path(__file__).with_name("_page_drive.mjs")
needs_browser = pytest.mark.skipif(NODE is None or not Path(CHROME).exists(), reason="node and Chrome are needed")

TOKEN_KEY = "levain_write_token"
MARK_KEY = "levain_paired_device"
WHOLE = "document.documentElement.hasAttribute('data-board-whole')"
READ = (f"JSON.stringify({{token: localStorage.getItem('{TOKEN_KEY}'), mark: localStorage.getItem('{MARK_KEY}'), "
        "hash: location.hash, search: location.search})")


# Page-side stubs, installed by the driver's `init` step (before the page's own scripts on every later load): a prompt that counts
# and declines, and a console.warn that keeps what it was told (the intake's refusal line is the sign that a check has ended).
INIT_BASE = (
    "window.__warns = []; window.__prompted = 0;"
    "const __cw = console.warn.bind(console);"
    "console.warn = function () { try { window.__warns.push(Array.prototype.join.call(arguments, ' ')); } catch (e) {} return __cw.apply(console, arguments); };"
    "window.prompt = function () { window.__prompted++; return ''; };"
)


def _manifest_stub(token: str, body: str) -> str:
    """A fetch wrapper: a manifest read that carries `token` goes to `body` (JS, with u, o, of in scope); every other read passes."""
    return (
        "(function () { const of = window.fetch;"
        "window.fetch = function (u, o) { try {"
        f"if (String(u).indexOf('/cockpit/manifest.json') >= 0 && o && o.headers && o.headers['X-Levain-Write-Token'] === '{token}') {{ {body} }}"
        "} catch (e) {} return of.apply(window, arguments); }; })();"
    )


def _hold_manifest_for(token: str) -> str:
    """The FIRST manifest read for `token` (the intake's check; a later board read with that token must pass) is held until
    `window.__release()`, then goes to the real server."""
    return "window.__held = 0;" + _manifest_stub(
        token, "if (window.__held === 0) { window.__held++; return new Promise((res) => { window.__release = () => res(of.call(window, u, o)); }); }")


def _silent_manifest_for(token: str) -> str:
    """The manifest read for `token` never answers and ignores any abort signal."""
    return "window.__silent = 0;" + _manifest_stub(token, "window.__silent++; return new Promise(() => {});")


def _drive(tmp_path: Path, url: str, steps: list[dict[str, Any]]) -> dict[str, Any]:
    f = tmp_path / "steps.json"
    f.write_text(json.dumps(steps))
    res = run_driver([NODE, str(DRIVE), CHROME, url, str(f)], timeout=300)
    assert res.returncode == 0, res.stderr or res.stdout
    return json.loads(res.stdout)


def _arrive(tmp_path: Path, url: str, first: str, second: str) -> dict[str, Any]:
    """Load the page with `first` as its fragment token (a first pairing: nothing stored yet), mark the browser as paired,
    then arrive at a fresh URL carrying `second` and read back what the browser holds once that board is drawn."""
    steps: list[dict[str, Any]] = [
        {"until": WHOLE},
        {"eval": f"localStorage.setItem('{MARK_KEY}', '1'); 1"},
        {"eval": f"location.assign('/?arrived=1#write_token={second}'); 1"},
        {"until": f"location.search === '?arrived=1' && {WHOLE} && 1", "ms": 40000},
        {"eval": READ},
    ]
    out = _drive(tmp_path, f"{url}/#write_token={first}", steps)
    return json.loads(out["values"][-1])


@needs_browser
def test_a_refused_fragment_token_leaves_the_stored_token_and_the_paired_mark(tmp_path: Path) -> None:
    _root, src, _ids = _install(tmp_path)
    with _serve(src, surface_tokens={"browser": "T"}) as (url, _httpd):
        got = _arrive(tmp_path, url, "T", "BAD")
    assert got == {"token": "T", "mark": "1", "hash": "", "search": "?arrived=1"}, got


@needs_browser
def test_an_accepted_fragment_token_replaces_the_stored_one_and_clears_the_paired_mark(tmp_path: Path) -> None:
    _root, src, _ids = _install(tmp_path)
    with _serve(src, surface_tokens={"browser": "T", "spare": "U"}) as (url, _httpd):
        got = _arrive(tmp_path, url, "U", "T")
    assert got == {"token": "T", "mark": None, "hash": "", "search": "?arrived=1"}, got


@needs_browser
def test_an_accepted_fragment_token_replaces_a_stored_one_the_server_no_longer_knows(tmp_path: Path) -> None:
    """The common case: a server restarted with a fresh per-launch token, and its link arrives in a browser holding the last one.
    The page is loaded with a valid token, then the stored value is set to OLD (which the server does not know) in the same step
    that navigates to the link, so the intake meets OLD as the held token, and no prompt may appear on the way."""
    _root, src, _ids = _install(tmp_path)
    with _serve(src, surface_tokens={"browser": "T"}) as (url, _httpd):
        out = _drive(tmp_path, f"{url}/#write_token=T", [
            {"until": WHOLE},
            {"init": INIT_BASE},
            {"eval": f"localStorage.setItem('{MARK_KEY}', '1'); localStorage.setItem('{TOKEN_KEY}', 'OLD'); "
                     "location.assign('/?arrived=1#write_token=T'); 1"},
            {"until": f"location.search === '?arrived=1' && {WHOLE} && 1", "ms": 40000},
            {"eval": READ},
            {"eval": "window.__prompted"},
        ])
    got = json.loads(out["values"][-2])
    assert got == {"token": "T", "mark": None, "hash": "", "search": "?arrived=1"}, got
    assert out["values"][-1] == 0, f"the page prompted for a token {out['values'][-1]} time(s)"


@needs_browser
def test_a_token_stored_by_another_tab_during_the_check_is_not_overwritten(tmp_path: Path) -> None:
    """Token B is checked (the manifest answer is held back), another tab pairs and stores C with the paired mark, then B is
    accepted: B must not replace C, and C's mark stays."""
    _root, src, _ids = _install(tmp_path)
    with _serve(src, surface_tokens={"browser": "A", "second": "B", "third": "C"}) as (url, _httpd):
        out = _drive(tmp_path, f"{url}/#write_token=A", [
            {"until": WHOLE},
            {"init": INIT_BASE + _hold_manifest_for("B")},
            {"eval": f"localStorage.setItem('{MARK_KEY}', '1'); location.assign('/?arrived=1#write_token=B'); 1"},
            {"until": "location.search === '?arrived=1' && window.__held > 0 && 1", "ms": 40000, "partial": True},
            {"eval": f"localStorage.setItem('{TOKEN_KEY}', 'C'); localStorage.setItem('{MARK_KEY}', '1'); 1", "partial": True},
            {"eval": "window.__release(); 1", "partial": True},
            {"until": f"{WHOLE} && 1", "ms": 40000},
            {"eval": READ},
        ])
    got = json.loads(out["values"][-1])
    assert got == {"token": "C", "mark": "1", "hash": "", "search": "?arrived=1"}, got


@needs_browser
def test_a_retained_paired_mark_with_no_token_still_validates_the_link_token_first(tmp_path: Path) -> None:
    """A paired browser that dropped its token keeps `levain_paired_device`; a bad link token must not be stored at once
    (which would clear the mark), it is checked like any other."""
    _root, src, _ids = _install(tmp_path)
    with _serve(src, surface_tokens={"browser": "T"}) as (url, _httpd):
        out = _drive(tmp_path, f"{url}/#write_token=T", [
            {"until": WHOLE},
            {"init": INIT_BASE},
            {"eval": f"localStorage.setItem('{MARK_KEY}', '1'); localStorage.removeItem('{TOKEN_KEY}'); "
                     "location.assign('/?arrived=1#write_token=BAD'); 1"},
            {"until": "location.search === '?arrived=1' && window.__warns.some((w) => /not accepted/.test(w)) && 1",
             "ms": 40000, "partial": True},
            {"eval": READ, "partial": True},
        ])
    got = json.loads(out["values"][-1])
    assert got["token"] is None and got["mark"] == "1" and got["search"] == "?arrived=1", got


@needs_browser
def test_the_check_is_bounded_without_abortcontroller_and_the_old_token_is_kept(tmp_path: Path) -> None:
    """No AbortController and a manifest that never answers: the link's token is given up on after the 8 s bound, the held
    token and mark stay, and the first board still draws."""
    _root, src, _ids = _install(tmp_path)
    with _serve(src, surface_tokens={"browser": "A"}) as (url, _httpd):
        out = _drive(tmp_path, f"{url}/#write_token=A", [
            {"until": WHOLE},
            {"init": INIT_BASE + "window.AbortController = undefined;" + _silent_manifest_for("B")},
            {"eval": f"localStorage.setItem('{MARK_KEY}', '1'); location.assign('/?arrived=1#write_token=B'); 1"},
            {"until": "location.search === '?arrived=1' && window.__silent > 0 && 1", "ms": 40000, "partial": True},
            {"until": "window.__warns.some((w) => /not accepted/.test(w)) && 1", "ms": 40000, "partial": True},
            {"until": f"{WHOLE} && typeof AbortController === 'undefined' && 1", "ms": 40000},
            {"eval": READ},
        ])
    got = json.loads(out["values"][-1])
    assert got == {"token": "A", "mark": "1", "hash": "", "search": "?arrived=1"}, got


@needs_browser
def test_a_first_pairing_from_a_fragment_stores_the_token_with_nothing_held(tmp_path: Path) -> None:
    _root, src, _ids = _install(tmp_path)
    with _serve(src, surface_tokens={"browser": "T"}) as (url, _httpd):
        out = _drive(tmp_path, f"{url}/#write_token=T&board", [{"until": WHOLE}, {"eval": READ}])
    got = json.loads(out["values"][-1])
    assert got["token"] == "T" and got["mark"] is None, got
    assert "write_token" not in got["hash"], got        # stripped as before; the tab part beside it stays
    assert got["hash"] == "#board=", got                # URLSearchParams writes a bare part back with its "=", as it always did

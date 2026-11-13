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
    """The common case: a server restarted with a fresh per-launch token, and its link arrives in a browser holding the last one."""
    _root, src, _ids = _install(tmp_path)
    with _serve(src, surface_tokens={"browser": "T"}) as (url, _httpd):
        got = _arrive(tmp_path, url, "OLD", "T")
    assert got == {"token": "T", "mark": None, "hash": "", "search": "?arrived=1"}, got


@needs_browser
def test_a_first_pairing_from_a_fragment_stores_the_token_with_nothing_held(tmp_path: Path) -> None:
    _root, src, _ids = _install(tmp_path)
    with _serve(src, surface_tokens={"browser": "T"}) as (url, _httpd):
        out = _drive(tmp_path, f"{url}/#write_token=T&board", [{"until": WHOLE}, {"eval": READ}])
    got = json.loads(out["values"][-1])
    assert got["token"] == "T" and got["mark"] is None, got
    assert "write_token" not in got["hash"], got        # stripped as before; the tab part beside it stays
    assert got["hash"] == "#board=", got                # URLSearchParams writes a bare part back with its "=", as it always did

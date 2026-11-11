"""1110-23: the "New version available. Tap to reload." bar (pwa_update.js) fires only when the browser sees /sw.js change by
BYTES. An install that changes a shell asset (the page's ?v= digests move) but leaves sw.js's bytes alone never produces a
waiting worker, so an already-open page never shows the bar. This asserts the seam: when the shell the server serves changes,
the /sw.js bytes it serves change with it."""
from __future__ import annotations

import re
import threading
import urllib.request
from contextlib import contextmanager
from pathlib import Path

from levain import web_server
from levain.web_server import make_server
from tests.test_web_server import _store_with_data


@contextmanager
def _up(tmp_path: Path):
    httpd = make_server(_store_with_data(tmp_path), host="127.0.0.1", port=0)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}"
    finally:
        httpd.shutdown()
        httpd.server_close()
        t.join(timeout=5)


def _get(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=5) as r:  # noqa: S310 - loopback only
        return r.read()


def test_a_changed_shell_asset_changes_the_served_worker_bytes(tmp_path: Path, monkeypatch) -> None:
    with _up(tmp_path / "a") as base:
        page_a, sw_a = _get(base + "/").decode(), _get(base + "/sw.js")
    real = web_server.load_web_asset
    monkeypatch.setattr(web_server, "load_web_asset",
                        lambda fn: real(fn) + ("\n// a new version\n" if fn == "dashboard_core.js" else ""))
    with _up(tmp_path / "b") as base:
        page_b, sw_b = _get(base + "/").decode(), _get(base + "/sw.js")
    v = lambda p: re.search(r"dashboard_core\.js\?v=([0-9a-f]{12})", p).group(1)  # noqa: E731
    assert v(page_a) != v(page_b), "precondition: the shell digest moved"
    assert sw_a != sw_b, "the shell moved but /sw.js is byte-identical: no waiting worker, so no reload bar on an open page"


# --- 1111-20: the worker's version is DERIVED from the shell, never hand-edited -------------------------------------------

def _version_of(sw: bytes) -> str:
    m = re.search(rb"const SW_VERSION = '([^']+)';", sw)
    assert m, "the served worker names its version"
    return m.group(1).decode()


def _sw_bytes(tmp_path: Path, monkeypatch, edit=None) -> bytes:
    if edit is not None:
        real = web_server.load_web_asset
        monkeypatch.setattr(web_server, "load_web_asset", lambda fn: edit(fn, real(fn)))
    with _up(tmp_path) as base:
        return _get(base + "/sw.js")


def test_two_servers_over_identical_assets_serve_identical_worker_bytes(tmp_path: Path) -> None:
    with _up(tmp_path / "a") as base:
        a = _get(base + "/sw.js")
    with _up(tmp_path / "b") as base:
        b = _get(base + "/sw.js")
    assert a == b and re.fullmatch(r"[0-9a-f]{12}", _version_of(a)), _version_of(a)
    assert b"__LEVAIN_SHELL_VERSION__" not in a


def test_a_one_byte_change_to_a_shell_asset_changes_the_worker_bytes_and_version(tmp_path: Path, monkeypatch) -> None:
    base_sw = _sw_bytes(tmp_path / "a", monkeypatch)
    one_byte = _sw_bytes(tmp_path / "b", monkeypatch, lambda fn, t: t + " " if fn == "dashboard_core.js" else t)
    assert one_byte != base_sw and _version_of(one_byte) != _version_of(base_sw)
    # only the version line differs: the worker's code is the same bytes either way
    strip = lambda b: re.sub(rb"const SW_VERSION = '[^']+';", b"", b)  # noqa: E731
    assert strip(one_byte) == strip(base_sw)


def test_the_shell_page_changing_changes_the_worker_bytes_too(tmp_path: Path, monkeypatch) -> None:
    base_sw = _sw_bytes(tmp_path / "a", monkeypatch)
    page_change = _sw_bytes(tmp_path / "b", monkeypatch, lambda fn, t: t + "\n<!-- a page edit -->\n" if fn == web_server._ASSETS["/"][0] else t)
    assert _version_of(page_change) != _version_of(base_sw)


def test_a_changed_mic_worklet_changes_the_worker_version_though_the_page_does_not_name_it(tmp_path: Path, monkeypatch) -> None:
    """1111-25: /mic_worklet.js is loaded by audioWorklet.addModule, so no ?v= in the page names it; its bytes must still move
    the served worker, or a worklet-only change ships an unchanged /sw.js and no reload bar."""
    base_sw = _sw_bytes(tmp_path / "a", monkeypatch)
    worklet = _sw_bytes(tmp_path / "b", monkeypatch, lambda fn, t: t + "\n// a worklet edit\n" if fn == "mic_worklet.js" else t)
    assert _version_of(worklet) != _version_of(base_sw)


def test_the_worker_is_served_no_cache_and_the_hand_edited_constant_is_gone(tmp_path: Path) -> None:
    src = (Path(web_server.__file__).parent / "webpush" / "static" / "sw.js").read_text(encoding="utf-8")
    assert len(re.findall(r"const SW_VERSION\s*=", src)) == 1 and "'1107-56-decl-1'" not in src
    with _up(tmp_path) as base:
        with urllib.request.urlopen(base + "/sw.js", timeout=5) as r:  # noqa: S310 - loopback only
            assert r.headers["Cache-Control"] == "no-cache"
            assert r.headers["Content-Type"].startswith("text/javascript")


def test_a_worker_without_its_version_placeholder_refuses_to_start(tmp_path: Path, monkeypatch) -> None:
    from levain.webpush import routes

    real = routes.worker_template
    for broken in (lambda: real().replace(routes.WORKER_VERSION_LINE, b"const SW_VERSION = '1107-56-decl-1';"),
                   lambda: real() + b"\n" + routes.WORKER_VERSION_LINE):
        monkeypatch.setattr(routes, "worker_template", broken)
        import pytest
        with pytest.raises(ValueError, match="placeholder"):
            make_server(_store_with_data(tmp_path / "x"), host="127.0.0.1", port=0)


def test_a_shell_page_naming_no_versioned_asset_refuses_to_start() -> None:
    import pytest
    with pytest.raises(ValueError, match="no \\?v="):
        web_server._shell_version(b"<html></html>", b"worker", {})

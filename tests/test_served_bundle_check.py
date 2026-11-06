"""scripts/served_bundle_check.py: "merged is not running" as one command. Built from a reproduced run (1105-36 b4): a real
``web_server`` on an ephemeral loopback port, checked against this checkout (all PASS) and against a copy with one asset
byte changed (FAIL naming that asset). The origin is always a test server on 127.0.0.1; nothing else is touched."""
from __future__ import annotations

import importlib.util
import socket
import sys
import threading
from contextlib import contextmanager
from pathlib import Path

import pytest

from levain import web_server
from levain.web_server import make_server
from tests.test_web_server import _store_with_data

REPO = Path(web_server.__file__).resolve().parents[1]
_PATH = REPO / "scripts" / "served_bundle_check.py"
_spec = importlib.util.spec_from_file_location("served_bundle_check_under_test", _PATH)
sbc = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = sbc          # a dataclass looks its own module up while it is defined
_spec.loader.exec_module(sbc)


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


def _failed(results) -> list[str]:
    return [r.name for r in results if not r.ok]


def test_a_server_running_this_tree_passes_every_check_and_every_static_path_is_covered(tmp_path: Path) -> None:
    with _up(tmp_path) as base:
        results = sbc.run(base, REPO)
    assert _failed(results) == [], [r.line() for r in results if not r.ok]
    names = {r.name for r in results}
    # every path the tree's own table serves is checked (derived from the table, not a remembered count) ...
    assert {f"tree {p}" for p in web_server._ASSETS} <= names
    # ... and the worker's shape is read from the real sw.js, so a rename in the worker turns this red
    assert any(r.name == "worker shape" and r.ok for r in results)


def test_one_changed_asset_byte_on_the_server_fails_naming_that_asset_and_no_other(tmp_path: Path, monkeypatch) -> None:
    """The reproduced failure: the origin runs bytes the tree does not have (merged is not running)."""
    real = web_server.load_web_asset
    monkeypatch.setattr(web_server, "load_web_asset",
                        lambda fn: real(fn) + "\n// one byte-level change\n" if fn == "sky.js" else real(fn))
    with _up(tmp_path) as base:
        results = sbc.run(base, REPO)
    assert _failed(results) == ["tree /sky.js"], [r.line() for r in results if not r.ok]
    # the origin is self-consistent (its page names the digest of what it serves): only the compare against the tree sees it
    assert all(r.ok for r in results if r.name.startswith("served "))


def test_a_changed_non_shell_page_and_a_changed_cockpit_template_are_each_caught(tmp_path: Path, monkeypatch) -> None:
    real = web_server.load_web_asset

    def changed(fn: str) -> str:
        return real(fn) + "\n<!-- x -->\n" if fn in ("pair.html", "dashboard.html") else real(fn)

    monkeypatch.setattr(web_server, "load_web_asset", changed)
    with _up(tmp_path) as base:
        results = sbc.run(base, REPO)
    assert sorted(_failed(results)) == ["tree /", "tree /pair"], [r.line() for r in results if not r.ok]


def _with_sw(sw_edit):
    """A ``get`` over the real fetch that rewrites the served worker."""
    def get(url: str):
        status, body = sbc.fetch(url)
        return (status, sw_edit(body)) if url.endswith("/sw.js") else (status, body)
    return get


def test_a_worker_that_is_not_the_row_35_shape_fails_the_shape_and_the_tree_compare(tmp_path: Path) -> None:
    with _up(tmp_path) as base:
        old = sbc.run(base, REPO, _with_sw(lambda b: b + b"\nlet navOffline = false;\n"))
        no_cache = sbc.run(base, REPO, _with_sw(lambda b: b.replace(b"async function cachedAsset(", b"async function other(")))
    assert sorted(_failed(old)) == ["tree /sw.js", "worker shape"]
    assert sorted(_failed(no_cache)) == ["tree /sw.js", "worker shape"]


def test_an_origin_that_predates_the_digests_and_a_page_naming_a_wrong_digest_both_fail(tmp_path: Path) -> None:
    with _up(tmp_path) as base:
        def strip(url: str):
            status, body = sbc.fetch(url)
            return (status, sbc._VERSION_QUERY.sub("", body.decode()).encode()) if url == base + "/" else (status, body)

        def wrong(url: str):
            status, body = sbc.fetch(url)
            if url == base + "/":
                text = body.decode()
                digest = sbc.shell_refs(text)[0]["/sky.js"]
                body = text.replace(f"/sky.js?v={digest}", f"/sky.js?v={'0' * 12}").encode()
            return status, body

        def other_bytes(url: str):          # a 200 whose bytes are not the ones the page's digest names (a proxy, a half-deployed cache)
            status, body = sbc.fetch(url)
            return (status, body + b" ") if "/sky.js?v=" in url else (status, body)

        no_digest = sbc.run(base, None, strip)
        bad_digest = sbc.run(base, None, wrong)
        mismatch = sbc.run(base, None, other_bytes)
    assert _failed(mismatch) == ["served /sky.js"] and "hash to" in next(r.detail for r in mismatch if not r.ok)
    assert "page" in _failed(no_digest) and any(n.startswith("page /") for n in _failed(no_digest))   # the page, and each bare ref
    assert _failed(bad_digest) == ["served /sky.js"]                      # the real server answers the wrong ?v= 404


def test_the_tree_may_be_a_checkout_or_the_package_dir_and_a_non_tree_is_refused(tmp_path: Path) -> None:
    assert sbc.package_dir(REPO) == REPO / "levain"
    assert sbc.package_dir(REPO / "levain") == REPO / "levain"
    with pytest.raises(OSError):
        sbc.package_dir(tmp_path)


def test_main_exit_codes_pass_fail_and_unreadable(tmp_path: Path, capsys, monkeypatch) -> None:
    real = web_server.load_web_asset
    with _up(tmp_path / "a") as base:
        assert sbc.main(["--url", base, "--tree", str(REPO)]) == 0
        assert "0 FAIL" in capsys.readouterr().out
    monkeypatch.setattr(web_server, "load_web_asset", lambda fn: real(fn) + " " if fn == "sky.js" else real(fn))
    with _up(tmp_path / "b") as base:
        assert sbc.main(["--url", base, "--tree", str(REPO)]) == 1
        assert "FAIL  tree /sky.js" in capsys.readouterr().out
        assert sbc.main(["--url", base, "--tree", str(tmp_path)]) == 2          # not a tree
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    assert sbc.main(["--url", f"http://127.0.0.1:{port}", "--timeout", "2"]) == 2   # nothing listening

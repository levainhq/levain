"""scripts/served_bundle_check.py: "merged is not running" as one command. Built from a reproduced run (1105-36 b4): a real
``web_server`` on an ephemeral loopback port, checked against this checkout (all PASS) and against a copy with one asset
byte changed (FAIL naming that asset). The origin is always a test server on 127.0.0.1; nothing else is touched."""
from __future__ import annotations

import http.server
import importlib.util
import re
import shutil
import socket
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path

import pytest

from levain import web_server
from levain.consent.sign import Signer
from levain.web_server import make_server
from tests.test_cockpit_sign_origin import NS, PINNED, RP, _file, _Passkey
from tests.test_cockpit_sign_routes import RID, _serving
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


def test_a_served_worker_version_must_be_a_derived_hex_label(tmp_path: Path) -> None:
    """1111-25: the tree compare blanks the version line on both sides, so without the hex requirement a served
    placeholder or a hand-set label would pass. A derived 12-hex label passes (the first test of this file)."""
    def with_version(v: bytes):
        return _with_sw(lambda b: re.sub(rb"const SW_VERSION = '[^']*';", b"const SW_VERSION = '" + v + b"';", b))

    with _up(tmp_path) as base:
        derived = sbc.run(base, REPO, with_version(b"0123456789ab"))
        placeholder = sbc.run(base, REPO, with_version(b"__LEVAIN_SHELL_VERSION__"))
        hand_set = sbc.run(base, REPO, with_version(b"1107-56-decl-1"))
        short_hex = sbc.run(base, REPO, with_version(b"0123456789a"))
    assert _failed(derived) == [], [r.line() for r in derived if not r.ok]
    assert _failed(placeholder) == ["tree /sw.js"]
    assert _failed(hand_set) == ["tree /sw.js"]
    assert _failed(short_hex) == ["tree /sw.js"]


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
    with socket.socket() as s:                  # bound and never listening, held open so no other process can take the port
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        assert sbc.main(["--url", f"http://127.0.0.1:{port}", "--timeout", "2"]) == 2   # nothing listening


# ---- L3 r1 fix round (1105-36 b4fix): each test below was first run RED against a6fc89e1 -------------------------------


def _tree(tmp: Path) -> Path:
    """A copy of this checkout's package pieces the checker reads (templates/web, web_server.py, the worker), safe to edit."""
    pkg = tmp / "levain"
    shutil.copytree(REPO / "levain" / "templates" / "web", pkg / "templates" / "web")
    shutil.copy(REPO / "levain" / "web_server.py", pkg / "web_server.py")
    (pkg / "webpush" / "static").mkdir(parents=True)
    shutil.copy(REPO / "levain" / "webpush" / "static" / "sw.js", pkg / "webpush" / "static" / "sw.js")
    return tmp


@contextmanager
def _origin(respond):
    """A tiny loopback origin (ephemeral port) whose every GET is answered by ``respond(handler)``."""
    class H(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self):
            try:
                respond(self)
            except OSError:                    # the client hung up on us: the point of some tests
                pass

        def log_message(self, *a):
            pass

    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    httpd.daemon_threads = True
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}"
    finally:
        httpd.shutdown()
        httpd.server_close()
        t.join(timeout=5)


def test_a_declared_asset_missing_from_the_tree_is_unreadable_not_a_silent_skip(tmp_path: Path, capsys) -> None:
    tree = _tree(tmp_path / "t")
    (tree / "levain" / "templates" / "web" / "pair.html").unlink()       # _ASSETS declares /pair -> pair.html
    with _up(tmp_path / "a") as base:
        assert sbc.main(["--url", base, "--tree", str(tree)]) == 2
    assert "pair.html" in capsys.readouterr().err


def test_the_sign_sheet_is_compared_when_served_and_named_unverified_when_not(tmp_path: Path, capsys, monkeypatch) -> None:
    pk = _Passkey()
    qdir = tmp_path / "q"
    signer = Signer(qdir, (NS,), RP, lambda: {pk.key.fingerprint: pk.key})
    signer.page_origin = PINNED
    _file(qdir, RID)
    with _serving(tmp_path / "s1", signer=signer, extra_hosts=[RP]) as (base, _httpd):
        same = sbc.run(base, REPO)
    assert _failed(same) == [] and any(r.name == "tree sign sheet" and r.ok and not r.unverified for r in same)
    real = web_server.load_web_asset
    monkeypatch.setattr(web_server, "load_web_asset", lambda fn: real(fn) + "<!-- x -->" if fn == "sign.html" else real(fn))
    with _serving(tmp_path / "s2", signer=signer, extra_hosts=[RP]) as (base, _httpd):
        changed = sbc.run(base, REPO)
    assert _failed(changed) == ["tree sign sheet"]
    monkeypatch.undo()
    with _up(tmp_path / "s3") as base:                       # no signer: the route is 404, the sheet cannot be read
        assert sbc.main(["--url", base, "--tree", str(REPO)]) == 0
    out = capsys.readouterr().out
    assert "UNVERIFIED  sign sheet (served at /sign/" in out
    assert out.rstrip().splitlines()[-1].endswith("UNVERIFIED: sign sheet") and "0 FAIL" in out


def test_tree_bytes_are_read_as_the_server_reads_them_so_crlf_files_do_not_fail_a_server_on_the_same_checkout(tmp_path: Path, monkeypatch) -> None:
    tree = _tree(tmp_path / "t")
    web = tree / "levain" / "templates" / "web"
    for name in ("sky.js", "pair.html"):
        (web / name).write_bytes((web / name).read_bytes().replace(b"\n", b"\r\n"))     # a Windows / autocrlf checkout
    monkeypatch.setattr(web_server, "load_web_asset", lambda fn: (web / fn).read_text(encoding="utf-8"))   # the real function's read
    with _up(tmp_path / "a") as base:
        results = sbc.run(base, tree)
    assert _failed(results) == [], [r.line() for r in results if not r.ok]


def test_a_cut_body_and_a_malformed_asset_table_are_exit_two_not_a_traceback(tmp_path: Path, capsys) -> None:
    def cut(h):                                 # Content-Length 5000, ten bytes, then the connection closes
        h.send_response(200)
        h.send_header("Content-Length", "5000")
        h.end_headers()
        h.wfile.write(b"0123456789")
        h.wfile.flush()
        h.close_connection = True

    def chunk_cut(h):                           # a chunked body that announces 0x64 bytes, sends ten, closes: IncompleteRead
        h.send_response(200)
        h.send_header("Transfer-Encoding", "chunked")
        h.end_headers()
        h.wfile.write(b"64\r\n0123456789")
        h.wfile.flush()
        h.close_connection = True

    def garbage(h):                             # not an HTTP status line at all: BadStatusLine
        h.wfile.write(b"NOT-HTTP garbage\r\n\r\n")
        h.wfile.flush()
        h.close_connection = True

    for respond in (cut, chunk_cut, garbage):
        with _origin(respond) as base:
            assert sbc.main(["--url", base, "--timeout", "3"]) == 2, respond.__name__
        assert capsys.readouterr().err.startswith("ERROR"), respond.__name__
    tree = _tree(tmp_path / "t")
    (tree / "levain" / "web_server.py").write_text("_ASSETS = {'/': 5}\n", encoding="utf-8")
    with _up(tmp_path / "a") as base:
        assert sbc.main(["--url", base, "--tree", str(tree)]) == 2
    assert capsys.readouterr().err.startswith("ERROR")


def test_a_page_that_omits_or_bares_its_shell_assets_yields_one_result_per_name(tmp_path: Path) -> None:
    with _up(tmp_path) as base:
        def strip(url: str):
            status, body = sbc.fetch(url)
            return (status, sbc._VERSION_QUERY.sub("", body.decode()).encode()) if url == base + "/" else (status, body)

        def omit(url: str):                     # the page names no sky.js at all
            status, body = sbc.fetch(url)
            if url == base + "/":
                body = re.sub(rb"<script[^>]*sky\.js[^>]*></script>", b"", body)
            return status, body

        bare = sbc.run(base, REPO, strip)
        omitted = sbc.run(base, REPO, omit)
    for results in (bare, omitted):
        names = [r.name for r in results]
        assert len(names) == len(set(names)), [r.line() for r in results]
    assert next(r for r in bare if r.name == "tree /sky.js").ok          # served bare, bytes equal the tree: judged on its bytes
    assert not next(r for r in omitted if r.name == "tree /sky.js").ok   # the tree's page names it, the served page does not


def test_one_path_named_twice_with_different_digests_fails(tmp_path: Path) -> None:
    with _up(tmp_path) as base:
        def twice(url: str):
            status, body = sbc.fetch(url)
            if url == base + "/":               # a wrong digest FIRST, the right one later (a dict keeps only the last)
                body = body.replace(b"<head>", b'<head><script src="/sky.js?v=' + b"0" * 12 + b'"></script>', 1)
            return status, body

        results = sbc.run(base, None, twice)
    page = [r for r in results if r.name == "page /sky.js"]          # the page's own contradiction is named (the first digest
    assert len(page) == 1 and not page[0].ok and "different" in page[0].detail    # is then also the one fetched, and fails alone)


def test_a_response_is_bounded_in_size_and_in_total_time(monkeypatch) -> None:
    def big(h):
        h.send_response(200)
        h.send_header("Content-Length", "5000")
        h.end_headers()
        h.wfile.write(b"x" * 5000)

    def drip(h):                                # one byte per 0.15 s: every socket read is fast, the whole is not
        h.send_response(200)
        h.send_header("Content-Length", "100")
        h.end_headers()
        for _ in range(100):
            h.wfile.write(b"x")
            h.wfile.flush()
            time.sleep(0.15)

    monkeypatch.setattr(sbc, "MAX_BODY", 1000)
    with _origin(big) as base:
        with pytest.raises(OSError, match="larger than"):
            sbc.fetch(base + "/", timeout=3.0)
    with _origin(drip) as base:
        started = time.monotonic()
        with pytest.raises(OSError, match="deadline"):
            sbc.fetch(base + "/", timeout=3.0, total_timeout=0.6)
        assert time.monotonic() - started < 3.0


# ---- L3 r2 fix round (1105-36 b4fix2): each test below was first run RED against 04a8ab6f -------------------------------


@pytest.mark.parametrize("status", [200, 404])
def test_a_body_that_stalls_past_the_deadline_is_exit_two_at_the_deadline_not_a_pass_after_it(status: int) -> None:
    def stall(h):                               # a chunked body: five bytes, a stall LONGER than the deadline but shorter than the
        h.send_response(status)                 # socket timeout, then the terminating chunk. The read that ends it is one blocking call
        h.send_header("Transfer-Encoding", "chunked")
        h.end_headers()
        h.wfile.write(b"5\r\nhello\r\n")
        h.wfile.flush()
        time.sleep(1.5)
        h.wfile.write(b"0\r\n\r\n")
        h.wfile.flush()

    with _origin(stall) as base:
        started = time.monotonic()
        with pytest.raises(OSError, match="deadline"):
            sbc.fetch(base + "/", timeout=3.0, total_timeout=0.5)
        assert time.monotonic() - started < 1.2      # given up AT the deadline: the blocking read was capped, not waited out


def test_a_read_that_finishes_after_the_deadline_is_over_deadline_even_when_it_returns_nothing() -> None:
    class Slow:                                 # no socket to cap: the one read takes 0.3 s and then says "end of body"
        headers = {}

        def read1(self, n):
            time.sleep(0.3)
            return b""

    with pytest.raises(OSError, match="deadline"):
        sbc._read_bounded("http://x/", Slow(), time.monotonic() + 0.1)


def test_a_tree_file_that_is_not_utf8_is_exit_two_naming_the_file(tmp_path: Path, capsys) -> None:
    tree = _tree(tmp_path / "t")
    (tree / "levain" / "templates" / "web" / "sky.js").write_bytes(b"\xff\xfe not utf-8")
    with pytest.raises(OSError, match="not valid UTF-8"):
        sbc.tree_bytes(tree / "levain" / "templates" / "web" / "sky.js")
    with _up(tmp_path / "a") as base:
        assert sbc.main(["--url", base, "--tree", str(tree)]) == 2
    err = capsys.readouterr().err
    assert err.startswith("ERROR") and "sky.js" in err and "UTF-8" in err


def test_a_path_named_bare_and_with_two_digests_is_one_result_carrying_every_reason(tmp_path: Path) -> None:
    with _up(tmp_path) as base:
        def messy(url: str):
            status, body = sbc.fetch(url)
            if url == base + "/":               # the real versioned sky.js stays; add a bare one and a second, wrong digest
                body = body.replace(b"<head>", b'<head><script src="/sky.js"></script><script src="/sky.js?v=' + b"0" * 12 + b'"></script>', 1)
            return status, body

        results = sbc.run(base, None, messy)
    page = [r for r in results if r.name == "page /sky.js"]
    assert len(page) == 1 and not page[0].ok, [r.line() for r in results]
    assert "without its ?v=" in page[0].detail and "different ?v=" in page[0].detail


# ---- L3 land11 r1 fix round (1105-49 fix11): each test below was first run RED against bb583d1e ---------------------------


def test_the_worker_compare_always_yields_one_tree_sw_result_and_names_why_it_failed(tmp_path: Path) -> None:
    """The docstring promises ONE ``tree <path>`` result per path: an empty served worker used to emit none."""
    tree = _tree(tmp_path / "t")
    with _up(tmp_path / "a") as base:
        empty = sbc.run(base, tree, _with_sw(lambda b: b""))                    # 200 and no body
        gone = sbc.run(base, tree, lambda u: (404, b"") if u.endswith("/sw.js") else sbc.fetch(u))   # no worker served at all
        (tree / "levain" / "webpush" / "static" / "sw.js").unlink()
        no_tree_sw = sbc.run(base, tree)
    for results in (empty, gone, no_tree_sw):
        mine = [r for r in results if r.name == "tree /sw.js"]
        assert len(mine) == 1 and not mine[0].ok, [r.line() for r in results]
    assert "empty" in next(r.detail for r in empty if r.name == "tree /sw.js")
    assert "origin" in next(r.detail for r in gone if r.name == "tree /sw.js")
    assert "no webpush/static/sw.js" in next(r.detail for r in no_tree_sw if r.name == "tree /sw.js")
    # check_origin's own lines are unchanged: the shape / worker failure is still reported, once
    assert "worker /sw.js" in _failed(gone) and "worker shape" in _failed(empty)


def test_a_crlf_copy_of_the_worker_in_the_tree_passes_against_the_lf_bytes_the_server_serves(tmp_path: Path) -> None:
    tree = _tree(tmp_path / "t")
    sw = tree / "levain" / "webpush" / "static" / "sw.js"
    sw.write_bytes(sw.read_bytes().replace(b"\r\n", b"\n").replace(b"\n", b"\r\n"))    # a Windows / autocrlf checkout
    assert b"\r\n" in sw.read_bytes()
    with _up(tmp_path / "a") as base:
        results = sbc.run(base, tree)
    assert _failed(results) == [], [r.line() for r in results if not r.ok]


def test_a_name_assigned_twice_at_module_level_is_refused_naming_the_file_and_the_name(tmp_path: Path, capsys) -> None:
    tree = _tree(tmp_path / "t")
    ws = tree / "levain" / "web_server.py"
    ws.write_text(ws.read_text(encoding="utf-8") + "\n_ASSETS = {}\n", encoding="utf-8")   # the later rebind is what Python uses
    with pytest.raises(OSError, match=r"web_server\.py.*_ASSETS.*more than once"):
        sbc._tree_literal(tree / "levain", "_ASSETS")
    with _up(tmp_path / "a") as base:
        assert sbc.main(["--url", base, "--tree", str(tree)]) == 2
    err = capsys.readouterr().err
    assert err.startswith("ERROR") and "_ASSETS" in err and "web_server.py" in err

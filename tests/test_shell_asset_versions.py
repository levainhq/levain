"""1105-17 (frame E): the URL is the version. The cockpit page the server serves names each of its scripts and stylesheets as
``<path>?v=<digest>``, the digest taken from the bytes the server serves for that path; the query is ignored when the path
is matched. The worker half (cache-first by exact URL) is in ``test_sw_worker.py``."""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import threading
import urllib.error
import urllib.request
from contextlib import contextmanager
from pathlib import Path

import pytest

from levain import web_server
from levain.web_server import make_server
from tests.test_web_server import _store_with_data

_REF = re.compile(r"""<(?:script|link)\b[^>]*?\b(?:src|href)\s*=\s*["']([^"']+)["'][^>]*>""", re.IGNORECASE)


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
    with urllib.request.urlopen(url, timeout=5) as r:  # noqa: S310 — loopback only
        return r.read()


def _shell_refs(page: str) -> dict[str, str]:
    """path -> the full reference, for the page's scripts and stylesheets that name a path this server serves."""
    out = {}
    for m in re.finditer(r"<(?:script|link)\b[^>]*>", page, re.IGNORECASE):
        tag = m.group(0)
        if tag.lower().startswith("<link") and not re.search(r"""rel\s*=\s*["']?stylesheet""", tag, re.IGNORECASE):
            continue
        ref = _REF.search(tag)
        if ref and ref.group(1).split("?", 1)[0] in web_server._ASSETS:
            out[ref.group(1).split("?", 1)[0]] = ref.group(1)
    return out


def test_every_shell_script_and_stylesheet_carries_the_digest_of_the_bytes_served_for_its_path(tmp_path: Path) -> None:
    with _up(tmp_path) as base:
        page = _get(base + "/").decode("utf-8")
        refs = _shell_refs(page)
        # every script and stylesheet the template names (by commit-time source, not a remembered count) comes back versioned
        assert set(refs) == set(_shell_refs(web_server.load_web_asset("dashboard.html"))) and refs, refs
        for path, ref in refs.items():
            want = hashlib.sha256(_get(base + path)).hexdigest()[:12]
            assert ref == f"{path}?v={want}", (path, ref, want)
        # the unversioned tags are gone: no script or stylesheet of the shell is named bare
        assert not re.search(r"""<script\b[^>]*\bsrc\s*=\s*["']/[^"'?]+["']""", page), page
        assert 'href="/dashboard.css"' not in page


def _status(url: str) -> tuple[int, bytes]:
    try:
        with urllib.request.urlopen(url, timeout=5) as r:  # noqa: S310 — loopback only
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def test_a_versioned_url_is_served_only_for_its_current_digest_a_wrong_one_is_404_and_no_v_is_served(tmp_path: Path) -> None:
    """1105-17 fix1 (codex MED, complement MED-LOW): the server used to ignore ``v``, so an old page's ``?v=OLD`` got the new
    bytes (which the worker then cached under the old name). Now a ``v`` that is not exactly the current digest fails closed."""
    with _up(tmp_path) as base:
        bare = _get(base + "/dashboard_core.js")
        digest = hashlib.sha256(bare).hexdigest()[:12]
        assert _status(base + f"/dashboard_core.js?v={digest}") == (200, bare)
        assert _status(base + "/dashboard_core.js") == (200, bare)                      # no v: served as before
        for wrong in {"x", "000000000000", digest[:-1], digest + "0", digest.upper(), ""} - {digest}:
            code, body = _status(base + f"/dashboard_core.js?v={wrong}")
            assert code == 404 and body == b"not found\n", (wrong, code)               # fixed body, the query is not echoed
        assert _status(base + f"/dashboard_core.js?v={digest}&v=x")[0] == 404           # two v's are not "exactly the digest"
        assert _status(base + "/dashboard_core.js?other=1") == (200, bare)              # a query without v is not a version claim
        assert _status(base + "/dashboard.css?v=anything")[0] == 404


def test_other_pages_stay_unversioned(tmp_path: Path) -> None:
    with _up(tmp_path) as base:
        pair = _get(base + "/pair").decode("utf-8")
        assert "?v=" not in pair, pair


def test_changing_one_assets_bytes_changes_the_page_and_no_other_assets_url(tmp_path: Path, monkeypatch) -> None:
    with _up(tmp_path / "a") as base:
        before = _shell_refs(_get(base + "/").decode("utf-8"))
        page_before = _get(base + "/")
    real = web_server.load_web_asset

    def changed(filename: str) -> str:
        text = real(filename)
        return text + "\n// one byte-level change\n" if filename == "sky.js" else text

    monkeypatch.setattr(web_server, "load_web_asset", changed)
    with _up(tmp_path / "b") as base:
        after = _shell_refs(_get(base + "/").decode("utf-8"))
        page_after = _get(base + "/")
    assert page_before != page_after
    moved = {p for p in before if before[p] != after[p]}
    assert moved == {"/sky.js"}, moved


def test_a_data_src_attribute_is_not_the_script_src_and_the_real_src_is_versioned() -> None:
    """1105-17 fix1 (complement LOW, deepseek MED, gemini MED): ``\\b`` matched ``data-src`` (the hyphen is a word boundary), so
    the first match on a tag could be the data attribute and leave the real ``src`` bare."""
    digests = {"/sky.js": "aaaaaaaaaaaa"}
    page = '<script data-src="/sky.js" src="/sky.js"></script><script src="/sky.js" data-href="/sky.js"></script>'
    out = web_server._version_shell_page(page, digests)
    assert out == ('<script data-src="/sky.js" src="/sky.js?v=aaaaaaaaaaaa"></script>'
                   '<script src="/sky.js?v=aaaaaaaaaaaa" data-href="/sky.js"></script>'), out


def test_the_startup_check_refuses_a_page_with_a_bare_shell_asset_reference_the_rewrite_missed(tmp_path: Path, monkeypatch) -> None:
    """The bound under the regexes (1105-17 fix1): after the rewrite no served script/stylesheet path may remain a bare
    src/href value anywhere in the cockpit page. Proven on make_server with a template that hides one."""
    digests = {"/sky.js": "aaaaaaaaaaaa", "/dashboard.css": "bbbbbbbbbbbb", "/pair": "cccccccccccc"}
    clean = web_server._version_shell_page('<script src="/sky.js"></script><a href="/pair">pair</a>'
                                           '<link rel="icon" href="/dashboard.css">', digests)
    web_server._assert_shell_versioned(clean, digests)      # the clean page, a non-asset anchor and a non-stylesheet link pass
    for hidden in ('<script data-x=">" src="/sky.js"></script>',                    # a '>' in an attribute ends the tag match early
                   "<script src=/sky.js></script>",                                  # unquoted: the rewrite does not reach it
                   "<script>document.write('<script src=\\\"/sky.js\\\"><\\/script>')</script>"):   # inside a script string
        try:
            web_server._assert_shell_versioned(web_server._version_shell_page(hidden, digests), digests)
        except ValueError as e:
            assert "/sky.js" in str(e), e
        else:
            raise AssertionError(f"the bound did not fire for {hidden!r}")
    real = web_server.load_web_asset

    def hiding(filename: str) -> str:
        text = real(filename)
        return text.replace("</body>", '<script data-x=">" src="/sky.js"></script></body>') if filename == "dashboard.html" else text

    monkeypatch.setattr(web_server, "load_web_asset", hiding)
    try:
        make_server(_store_with_data(tmp_path), host="127.0.0.1", port=0)
    except ValueError as e:
        assert "/sky.js" in str(e) and "refusing to start" in str(e), e
    else:
        raise AssertionError("make_server started with a bare shell asset reference in the cockpit page")


_SW = Path(__file__).resolve().parent.parent / "levain" / "webpush" / "static" / "sw.js"
_NODE = shutil.which("node")
needs_node = pytest.mark.skipif(_NODE is None, reason="node is not installed")


def test_the_workers_attribute_boundary_is_the_servers() -> None:
    """1105-17 fix2/fix3: the same fact in two languages must not drift. ``levain/webpush/static/sw.js`` holds the boundary as
    ``ATTR_BOUNDARY = String.raw`...``` (no build step to share one source); the server's ``_ATTR_BOUNDARY`` is that text and
    ``_SHELL_ATTR`` opens with it."""
    m = re.search(r"const ATTR_BOUNDARY = String\.raw`([^`]+)`;", _SW.read_text(encoding="utf-8"))
    assert m, "sw.js no longer declares ATTR_BOUNDARY as a String.raw literal"
    assert m.group(1) == web_server._ATTR_BOUNDARY, (m.group(1), web_server._ATTR_BOUNDARY)
    assert web_server._SHELL_ATTR.pattern.startswith(m.group(1)), (m.group(1), web_server._SHELL_ATTR.pattern)


def test_no_regex_lookbehind_in_the_worker_or_the_servers_shell_attribute_pattern() -> None:
    """1105-17 fix3 (L3 r3 MED): ``new RegExp('(?<!...')`` THROWS in Safari before 16.4, so a lookbehind in the worker's shell
    parse caches no shell on those devices. Old Safari cannot be run here: the guard is that no ``(?<`` (lookbehind or named
    group, neither needed) is anywhere in sw.js, and none is in the server's half of the shared boundary."""
    sw = _SW.read_text(encoding="utf-8")
    assert "(?<" not in sw, [ln for ln in sw.splitlines() if "(?<" in ln]
    assert "(?<" not in web_server._ATTR_BOUNDARY and "(?<" not in web_server._SHELL_ATTR.pattern, web_server._SHELL_ATTR.pattern


_D = {"/a.js": "aaaaaaaaaaaa", "/b.js": "bbbbbbbbbbbb", "/c.css": "cccccccccccc"}
# (tag html, the server's rewrite of it, the keys the worker's shell parse finds)
_BOUNDARY_CASES = [
    ('<script src="/a.js"></script>', '<script src="/a.js?v=aaaaaaaaaaaa"></script>', ["/a.js"]),
    ("<script  src='/a.js'></script>", "<script  src='/a.js?v=aaaaaaaaaaaa'></script>", ["/a.js"]),            # two spaces, single quotes
    ('<script src = "/a.js"></script>', '<script src = "/a.js?v=aaaaaaaaaaaa"></script>', ["/a.js"]),
    ('<script\tsrc="/a.js"></script>', '<script\tsrc="/a.js?v=aaaaaaaaaaaa"></script>', ["/a.js"]),            # tab before the attribute
    ('<script\nsrc="/a.js"></script>', '<script\nsrc="/a.js?v=aaaaaaaaaaaa"></script>', ["/a.js"]),            # newline before it
    ('<script data-src="/a.js"></script>', '<script data-src="/a.js"></script>', []),                          # preceded by '-'
    ('<script xsrc="/a.js"></script>', '<script xsrc="/a.js"></script>', []),                                  # preceded by a word char
    ('<script src="/a.js" src="/b.js"></script>', '<script src="/a.js?v=aaaaaaaaaaaa" src="/b.js"></script>', ["/a.js"]),  # the first only
    ('<script data-src="/a.js" src="/b.js"></script>', '<script data-src="/a.js" src="/b.js?v=bbbbbbbbbbbb"></script>', ["/b.js"]),
    ('<link rel="stylesheet" href="/c.css">', '<link rel="stylesheet" href="/c.css?v=cccccccccccc">', ["/c.css"]),
    ('<link data-href="/c.css" rel="stylesheet">', '<link data-href="/c.css" rel="stylesheet">', []),
]


@pytest.mark.parametrize("html,rewritten,_keys", _BOUNDARY_CASES)
def test_the_servers_rewrite_follows_the_attribute_boundary_and_re_emits_the_boundary_character(html, rewritten, _keys) -> None:
    assert web_server._version_shell_page(html, _D) == rewritten


def test_the_servers_attribute_pattern_matches_at_the_string_start_and_re_emits_nothing_extra() -> None:
    sub = lambda t: web_server._SHELL_ATTR.sub(lambda m: f"{m.group(1)}{m.group(2)}{m.group(3)}'X'", t, count=1)  # noqa: E731
    assert sub('src="/a.js"') == "src='X'"                      # group 1 is empty: the start of the string is the boundary
    assert sub(' src="/a.js"') == " src='X'"                    # a space is consumed and put back
    assert sub('data-src="/a.js"') == 'data-src="/a.js"' and sub('xsrc="/a.js"') == 'xsrc="/a.js"'


def test_the_servers_bound_under_the_rewrite_sees_the_same_attribute_boundary() -> None:
    """``_ANY_REF`` keeps its own (Python-only) lookbehind; it must agree with the shared boundary on every case."""
    for html, _out, keys in _BOUNDARY_CASES:
        seen = [m.group(1) for m in web_server._ANY_REF.finditer(html)]
        assert bool(seen) == bool(keys) and (not keys or keys[0] in seen), (html, seen, keys)


_WORKER_SCRIPT = r"""
const fs = require('fs'), vm = require('vm');
const [swPath, casesPath] = process.argv.slice(2);
const sandbox = { self: { addEventListener() {}, registration: {} }, URL, console, Promise, JSON, String, Array, Math, Date,
  setTimeout, clearTimeout, TextEncoder, Response, Headers, caches: {}, indexedDB: {} };
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync(swPath, 'utf8'), sandbox);
const out = JSON.parse(fs.readFileSync(casesPath, 'utf8')).map((h) => {
  sandbox.__html = h;
  return vm.runInContext('shellAssets(__html).map((a) => a.key)', sandbox);
});
// the boundary alone, at the string start: group 1 is empty there
const start = vm.runInContext("new RegExp(ATTR_BOUNDARY + 'src').exec('src=1')", sandbox);
console.log(JSON.stringify({ out, start: start && [start.index, start[1]] }));
"""


@needs_node
def test_the_workers_shell_parse_follows_the_same_attribute_boundary_cases(tmp_path: Path) -> None:
    """The worker half of the table above, run in node against the real sw.js (``shellAssets``)."""
    script, cases = tmp_path / "shell_assets.js", tmp_path / "cases.json"
    script.write_text(_WORKER_SCRIPT, encoding="utf-8")
    cases.write_text(json.dumps([c[0] for c in _BOUNDARY_CASES]), encoding="utf-8")
    cp = subprocess.run([_NODE, str(script), str(_SW), str(cases)], capture_output=True, text=True, timeout=60)
    assert cp.returncode == 0, cp.stdout + cp.stderr
    got = json.loads(cp.stdout)
    for (html, _out, keys), found in zip(_BOUNDARY_CASES, got["out"]):
        assert found == keys, (html, found, keys)
    assert got["start"] == [0, ""], got

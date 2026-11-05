"""1105-17 (frame E): the URL is the version. The cockpit page the server serves names each of its scripts and stylesheets as
``<path>?v=<digest>``, the digest taken from the bytes the server serves for that path; the query is ignored when the path
is matched. The worker half (cache-first by exact URL) is in ``test_sw_worker.py``."""
from __future__ import annotations

import hashlib
import re
import threading
import urllib.error
import urllib.request
from contextlib import contextmanager
from pathlib import Path

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

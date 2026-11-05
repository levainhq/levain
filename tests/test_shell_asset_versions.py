"""1105-17 (frame E): the URL is the version. The cockpit page the server serves names each of its scripts and stylesheets as
``<path>?v=<digest>``, the digest taken from the bytes the server serves for that path; the query is ignored when the path
is matched. The worker half (cache-first by exact URL) is in ``test_sw_worker.py``."""
from __future__ import annotations

import hashlib
import re
import threading
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


def test_a_versioned_url_is_served_by_its_path_and_the_query_does_not_change_the_bytes(tmp_path: Path) -> None:
    with _up(tmp_path) as base:
        assert _get(base + "/dashboard_core.js?v=x") == _get(base + "/dashboard_core.js")
        assert _get(base + "/dashboard.css?v=anything") == _get(base + "/dashboard.css")


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

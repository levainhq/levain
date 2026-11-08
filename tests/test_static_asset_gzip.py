"""Static .css/.js assets: gzip when accepted, strong ETag + no-cache, 304 on a match; every other response keeps no-store.

Measured cause (consent cards slow on a phone): ~538 KB per card, uncompressed, ``no-store``, re-fetched every card."""
from __future__ import annotations

import gzip
import http.client
import threading
from contextlib import contextmanager
from typing import Iterator

from levain.dashboard import AnnealPaths, SubstrateSource
from levain.web_server import make_server


@contextmanager
def _serving(tmp_path) -> Iterator[int]:
    source = SubstrateSource(anneal=AnnealPaths.from_db(tmp_path / "memory.db"))
    httpd = make_server(source, host="127.0.0.1", port=0)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        yield httpd.server_address[1]
    finally:
        httpd.shutdown()
        httpd.server_close()
        t.join(timeout=5)


def _get(port: int, path: str, headers: dict | None = None):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    c.request("GET", path, headers=headers or {})
    r = c.getresponse()
    body = r.read()
    out = (r.status, {k.lower(): v for k, v in r.getheaders()}, body)
    c.close()
    return out


def test_gzip_when_accepted_identity_otherwise(tmp_path):
    with _serving(tmp_path) as port:
        st, h, plain = _get(port, "/dashboard.css")
        assert st == 200 and "content-encoding" not in h and h["vary"] == "Accept-Encoding"
        st, h, gz = _get(port, "/dashboard.css", {"Accept-Encoding": "gzip, deflate"})
        assert st == 200 and h["content-encoding"] == "gzip"
        assert int(h["content-length"]) == len(gz) < len(plain) // 2
        assert gzip.decompress(gz) == plain
        _, h0, refused = _get(port, "/sign.js", {"Accept-Encoding": "gzip;q=0"})
        assert "content-encoding" not in h0 and refused


def test_etag_revalidates_to_an_empty_304(tmp_path):
    with _serving(tmp_path) as port:
        _, h, _ = _get(port, "/sign.js", {"Accept-Encoding": "gzip"})
        assert h["cache-control"] == "no-cache" and h["etag"].startswith('"')
        st, h2, body = _get(port, "/sign.js", {"Accept-Encoding": "gzip", "If-None-Match": h["etag"]})
        assert st == 304 and body == b"" and h2["etag"] == h["etag"]
        st, _, body = _get(port, "/sign.js", {"If-None-Match": '"other"'})
        assert st == 200 and body


def test_api_and_html_responses_keep_no_store(tmp_path):
    with _serving(tmp_path) as port:
        for path in ("/", "/substrate.json", "/cockpit/sign.json"):
            st, h, _ = _get(port, path, {"Accept-Encoding": "gzip"})
            assert h["cache-control"] != "no-cache" and "etag" not in h, (path, st, h)
            assert "content-encoding" not in h, path
            assert "no-store" in h["cache-control"], (path, st, h)


def test_gzip_and_identity_carry_distinct_strong_etags(tmp_path):
    with _serving(tmp_path) as port:
        _, hp, _ = _get(port, "/sign.js")
        _, hg, _ = _get(port, "/sign.js", {"Accept-Encoding": "gzip"})
        assert hp["etag"] != hg["etag"] and hg["etag"].endswith('-gz"') and not hg["etag"].startswith("W/")
        st, h2, body = _get(port, "/sign.js", {"Accept-Encoding": "gzip", "If-None-Match": hg["etag"]})
        assert st == 304 and body == b"" and h2["etag"] == hg["etag"] and h2["vary"] == "Accept-Encoding"
        st, h3, _ = _get(port, "/sign.js", {"If-None-Match": hp["etag"]})
        assert st == 304 and h3["etag"] == hp["etag"]


def test_every_js_css_content_type_is_compressible():
    from levain.web_server import _ASSETS, _compressible

    emitted = {ct for path, (_, ct) in _ASSETS.items() if path.endswith((".js", ".css"))}
    assert emitted and all(_compressible(ct) for ct in emitted), emitted
    for ct in ("application/javascript", "Application/JavaScript; charset=utf-8", "TEXT/CSS", "text/javascript"):
        assert _compressible(ct), ct
    for ct in ("text/html; charset=utf-8", "application/json", "text/plain"):
        assert not _compressible(ct), ct


def test_accept_encoding_q_case_and_wildcard():
    from levain.web_server import _accepts_gzip as a

    assert not a("gzip;Q=0, identity") and not a("GZIP; Q=0.0")
    assert a("gzip;Q=0.5") and a("gzip") and a("x-gzip")
    assert a("*") and a("*;q=1, identity;q=0") and not a("*;q=0") and not a("identity")
    assert not a("gzip;q=0, *;q=1") and a("gzip;q=1, *;q=0")
    assert not a(None) and not a("gzip;q=abc")


def test_concurrent_first_requests_share_one_cached_entry(monkeypatch):
    import levain.web_server as ws

    calls = []
    real = ws.gzip.compress

    def slow(data, **kw):
        calls.append(1)
        threading.Event().wait(0.05)
        return real(data, **kw)

    monkeypatch.setattr(ws.gzip, "compress", slow)
    body = b"x" * 5000 + b"unique-cardgz-body"
    results = []
    ts = [threading.Thread(target=lambda: results.append(ws._static_entry(body))) for _ in range(12)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert len(results) == 12 and len(set(results)) == 1
    assert 1 <= len(calls) <= 12
    before = len(calls)
    assert ws._static_entry(body) == results[0] and len(calls) == before


def test_conditional_matches_only_the_served_representation(tmp_path):
    with _serving(tmp_path) as port:
        _, hp, plain = _get(port, "/sign.js")
        _, hg, _ = _get(port, "/sign.js", {"Accept-Encoding": "gzip"})
        st, h, body = _get(port, "/sign.js", {"Accept-Encoding": "gzip", "If-None-Match": hp["etag"]})
        assert st == 200 and h["etag"] == hg["etag"] and gzip.decompress(body) == plain
        st, h, body = _get(port, "/sign.js", {"If-None-Match": hg["etag"]})
        assert st == 200 and h["etag"] == hp["etag"] and body == plain
        st, _, body = _get(port, "/sign.js", {"Accept-Encoding": "gzip", "If-None-Match": hg["etag"]})
        assert st == 304 and body == b""


def test_slow_compression_does_not_block_a_cache_hit(monkeypatch):
    import levain.web_server as ws

    a = b"a" * 4000 + b"cardgz-a-body"
    b = b"b" * 4000 + b"cardgz-b-body"
    ws._static_entry(a)
    started, release = threading.Event(), threading.Event()
    real = ws.gzip.compress

    def gated(data, **kw):
        if data is b:
            started.set()
            assert release.wait(10)
        return real(data, **kw)

    monkeypatch.setattr(ws.gzip, "compress", gated)
    tb = threading.Thread(target=ws._static_entry, args=(b,))
    tb.start()
    try:
        assert started.wait(10)
        out = []
        ta = threading.Thread(target=lambda: out.append(ws._static_entry(a)))
        ta.start()
        ta.join(timeout=5)
        assert not ta.is_alive() and out
    finally:
        release.set()
        tb.join(timeout=10)
    assert ws._static_entry(b)[2] == real(b, compresslevel=9, mtime=0)

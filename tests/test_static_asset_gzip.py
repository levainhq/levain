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

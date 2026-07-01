"""Tests for levain.docs_server — `levain docs`, the read-only manual server.

Exercised as a REAL bound server over loopback (urllib), like ``test_init_server``.
Load-bearing guards: the loopback-only bind refusal, the DNS-rebinding Host
allowlist + cross-site read refusal, the CSP/security headers on EVERY response
(including framework-generated errors), and the composed /docs.json projection.
Read-only: there is no write route.
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from contextlib import contextmanager
from pathlib import Path

import pytest

from levain.docs_server import make_docs_server


@contextmanager
def _serving(install: Path):
    httpd = make_docs_server(install, port=0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    host, port = httpd.server_address[0], httpd.server_address[1]
    try:
        yield f"http://{host}:{port}", port
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def _req(url: str, *, method: str = "GET", headers: dict | None = None):
    req = urllib.request.Request(url, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:  # noqa: S310 — loopback only
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


# --------------------------------------------------------------------------
# bind
# --------------------------------------------------------------------------

class TestBind:
    def test_refuses_wildcard(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="loopback-only"):
            make_docs_server(tmp_path, host="0.0.0.0", port=0)

    def test_refuses_lan_address(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="loopback-only"):
            make_docs_server(tmp_path, host="192.168.1.9", port=0)

    def test_refuses_public_address(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="loopback-only"):
            make_docs_server(tmp_path, host="8.8.8.8", port=0)

    def test_loopback_binds(self, tmp_path: Path) -> None:
        for host in ("127.0.0.1", "localhost"):
            httpd = make_docs_server(tmp_path, host=host, port=0)
            try:
                assert httpd.server_address[0].startswith("127.")
            finally:
                httpd.server_close()


# --------------------------------------------------------------------------
# reads
# --------------------------------------------------------------------------

class TestReads:
    def test_shell_served(self, tmp_path: Path) -> None:
        with _serving(tmp_path) as (base, _port):
            status, headers, body = _req(base + "/")
            assert status == 200
            assert "text/html" in headers["Content-Type"]
            assert b"Operator Manual" in body

    def test_assets_served(self, tmp_path: Path) -> None:
        with _serving(tmp_path) as (base, _port):
            for path, ctype in (
                ("/docs.css", "text/css"),
                ("/docs.js", "text/javascript"),
                ("/markdown.js", "text/javascript"),
            ):
                status, headers, body = _req(base + path)
                assert status == 200
                assert ctype in headers["Content-Type"]
                assert len(body) > 0

    def test_markdown_asset_exposes_renderer(self, tmp_path: Path) -> None:
        with _serving(tmp_path) as (base, _port):
            _s, _h, body = _req(base + "/markdown.js")
            assert b"window.LevainMD" in body
            assert b"renderMarkdown" in body

    def test_docs_json_shape(self, tmp_path: Path) -> None:
        with _serving(tmp_path) as (base, _port):
            _s, headers, raw = _req(base + "/docs.json")
            assert "application/json" in headers["Content-Type"]
            data = json.loads(raw)
            assert data["chapters"], "base chapters always present"
            first = data["chapters"][0]
            assert first["source"] == "base"
            assert "Driving Your Partner" in first["title"]
            assert isinstance(first["markdown"], str) and first["markdown"]

    def test_docs_json_composes_pack(self, tmp_path: Path) -> None:
        layer = tmp_path / ".levain" / "docs" / "001-acme"
        layer.mkdir(parents=True)
        (layer / "c.md").write_text("# Acme Work Day\nbody\n", encoding="utf-8")
        with _serving(tmp_path) as (base, _port):
            _s, _h, raw = _req(base + "/docs.json")
            data = json.loads(raw)
            assert data["chapters"][-1]["source"] == "acme"
            assert data["chapters"][-1]["title"] == "Acme Work Day"

    def test_security_headers_on_every_response(self, tmp_path: Path) -> None:
        with _serving(tmp_path) as (base, _port):
            for path in ("/", "/docs.json", "/nope"):
                _s, headers, _b = _req(base + path)
                assert "default-src 'none'" in headers["Content-Security-Policy"]
                assert headers["X-Content-Type-Options"] == "nosniff"
                assert headers["X-Frame-Options"] == "DENY"
                assert headers["Cache-Control"] == "no-store"

    def test_unknown_route_404(self, tmp_path: Path) -> None:
        with _serving(tmp_path) as (base, _port):
            status, _hd, _b = _req(base + "/nope")
            assert status == 404

    def test_head_ok_no_body(self, tmp_path: Path) -> None:
        with _serving(tmp_path) as (base, _port):
            status, _hd, body = _req(base + "/", method="HEAD")
            assert status == 200
            assert body == b""


# --------------------------------------------------------------------------
# security
# --------------------------------------------------------------------------

class TestSecurity:
    def test_bad_host_rejected(self, tmp_path: Path) -> None:
        with _serving(tmp_path) as (base, _port):
            status, _hd, _b = _req(base + "/docs.json", headers={"Host": "evil.com"})
            assert status == 403

    def test_cross_site_get_rejected(self, tmp_path: Path) -> None:
        with _serving(tmp_path) as (base, _port):
            status, _hd, _b = _req(
                base + "/docs.json", headers={"Sec-Fetch-Site": "cross-site"}
            )
            assert status == 403

    def test_post_not_supported_but_secured(self, tmp_path: Path) -> None:
        # Read-only: there is no write route, so a POST falls through to the stdlib
        # 501 — which still carries the security headers (via end_headers).
        with _serving(tmp_path) as (base, _port):
            status, headers, _b = _req(base + "/docs.json", method="POST")
            assert status in (400, 501)
            assert "default-src 'none'" in headers["Content-Security-Policy"]

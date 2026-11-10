"""The cockpit's JSON reads (manifest, panels) are gzipped when the client accepts it and the body is big enough;
the change stream is not; a 304 stays bodiless; the page's panel read is conditional."""

from __future__ import annotations

import gzip
import http.client
import json
from pathlib import Path

from levain.cockpit import RowIn
from levain.cockpit.providers import build_default_cockpit
from levain.web_server import COCKPIT_GZIP_MIN_BYTES
from tests.test_cockpit_k1 import _http, _install, _serve, _simple

WEB = Path(__file__).resolve().parent.parent / "levain" / "templates" / "web"


def _cockpit(src, n_rows: int):
    ck = build_default_cockpit(src)
    rows = [RowIn(f"r{i}", "t" * 40, {"at": "1"}, stored={"id": f"r{i}"}) for i in range(n_rows)]
    ck.register(_simple("bulk", rows=rows))
    ck.read_cache_ttl_s = 0
    return ck


def test_a_big_panel_and_the_manifest_are_gzipped_for_a_client_that_accepts_it(tmp_path: Path) -> None:
    _root, src = _install(tmp_path)
    with _serve(src, cockpit=_cockpit(src, 60)) as (base, _h):
        for path in ("/cockpit/panel/bulk.json", "/cockpit/manifest.json"):
            st, hdr, wire = _http(base + path, {"Accept-Encoding": "gzip"})
            plain = _http(base + path)[2]
            assert st == 200 and hdr["Content-Encoding"] == "gzip"
            assert int(hdr["Content-Length"]) == len(wire) < len(plain)
            assert json.loads(gzip.decompress(wire))["etag"] == json.loads(plain)["etag"]
            vary = [v.strip() for v in hdr["Vary"].split(",")]
            assert "Accept-Encoding" in vary and "X-Levain-Write-Token" in vary
            assert "Content-Encoding" not in dict(_http(base + path)[1])


def test_identity_is_served_when_gzip_is_refused_or_not_offered(tmp_path: Path) -> None:
    _root, src = _install(tmp_path)
    with _serve(src, cockpit=_cockpit(src, 60)) as (base, _h):
        for ae in ({}, {"Accept-Encoding": "gzip;q=0"}, {"Accept-Encoding": "identity"}):
            st, hdr, wire = _http(base + "/cockpit/panel/bulk.json", ae)
            assert st == 200 and "Content-Encoding" not in hdr
            assert json.loads(wire)["id"] == "bulk"
            assert "Accept-Encoding" in hdr["Vary"]


def test_a_small_body_and_an_error_are_not_compressed(tmp_path: Path) -> None:
    _root, src = _install(tmp_path)
    with _serve(src, cockpit=_cockpit(src, 60)) as (base, _h):
        st, hdr, wire = _http(base + "/cockpit/freshness.json", {"Accept-Encoding": "gzip"})
        assert st == 200 and len(wire) < COCKPIT_GZIP_MIN_BYTES and "Content-Encoding" not in hdr
        json.loads(wire)
        st, hdr, wire = _http(base + "/cockpit/panel/nope.json", {"Accept-Encoding": "gzip"})
        assert st == 404 and "Content-Encoding" not in hdr and json.loads(wire)["error"] == "not_found"


def test_a_304_stays_bodiless_and_keeps_its_validator_and_vary(tmp_path: Path) -> None:
    _root, src = _install(tmp_path)
    with _serve(src, cockpit=_cockpit(src, 60)) as (base, _h):
        ae = {"Accept-Encoding": "gzip"}
        _st, hdr, _wire = _http(base + "/cockpit/panel/bulk.json", ae)
        st, h2, wire = _http(base + "/cockpit/panel/bulk.json", {**ae, "If-None-Match": hdr["ETag"]})
        assert st == 304 and wire == b"" and "Content-Encoding" not in h2
        assert h2["ETag"] == hdr["ETag"] and "Accept-Encoding" in h2["Vary"]


def test_the_change_stream_is_not_compressed(tmp_path: Path) -> None:
    _root, src = _install(tmp_path)
    with _serve(src, cockpit=_cockpit(src, 3)) as (base, _h):
        conn = http.client.HTTPConnection(base.removeprefix("http://"), timeout=5)
        try:
            conn.request("GET", "/cockpit/events", headers={"Accept-Encoding": "gzip"})
            resp = conn.getresponse()
            assert resp.status == 200 and resp.getheader("Content-Encoding") is None
        finally:
            conn.close()


def test_the_pages_panel_read_sends_if_none_match_with_the_etag_it_holds() -> None:
    js = (WEB / "dashboard_boot.js").read_text(encoding="utf-8")
    fetch_panel = js[js.index("const fetchPanel = async"): js.index("const queue = ids.slice()")]
    assert 'headers["If-None-Match"] = hit.etag' in fetch_panel
    assert "res.status === 304 && hit" in fetch_panel
    assert "panelBodies.set(url" in fetch_panel

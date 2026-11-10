"""The cockpit's JSON reads (manifest, panels) are gzipped when the client accepts it and the body is big enough;
the change stream is not; a 304 stays bodiless; the page's panel read is conditional."""

from __future__ import annotations

import gzip
import http.client
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from levain.cockpit import RowIn
from levain.cockpit.providers import build_default_cockpit
import levain.web_server as web_server
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


def test_compression_runs_while_the_read_slot_is_held(tmp_path: Path, monkeypatch) -> None:
    _root, src = _install(tmp_path)
    seen: list[int] = []
    real = web_server.gzip.compress
    with _serve(src, cockpit=_cockpit(src, 60)) as (base, httpd):
        def spy(*a, **kw):
            # a free slot is acquirable without blocking; a held one is not (the pool of 6 is otherwise idle here)
            held = 0
            got = []
            while httpd.read_gate.acquire(blocking=False):
                got.append(1)
            held = 6 - len(got) if len(got) <= 6 else 0
            for _ in got:
                httpd.read_gate.release()
            seen.append(held)
            return real(*a, **kw)
        monkeypatch.setattr(web_server.gzip, "compress", spy)
        st, hdr, _wire = _http(base + "/cockpit/panel/bulk.json", {"Accept-Encoding": "gzip"})
        assert st == 200 and hdr["Content-Encoding"] == "gzip"
        assert seen and seen[-1] >= 1


def test_a_compression_fault_answers_the_identity_body_with_vary_kept(tmp_path: Path, monkeypatch) -> None:
    _root, src = _install(tmp_path)
    def boom(*a, **kw):
        raise OSError("zlib")
    with _serve(src, cockpit=_cockpit(src, 60)) as (base, _h):
        monkeypatch.setattr(web_server.gzip, "compress", boom)
        st, hdr, wire = _http(base + "/cockpit/panel/bulk.json", {"Accept-Encoding": "gzip"})
        assert st == 200 and "Content-Encoding" not in hdr
        assert json.loads(wire)["id"] == "bulk" and int(hdr["Content-Length"]) == len(wire)
        assert "Accept-Encoding" in hdr["Vary"] and "X-Levain-Write-Token" in hdr["Vary"]


_NODE_DRIVER = r"""
const fs = require("fs");
const src = fs.readFileSync(process.argv[2], "utf8");
const a = src.indexOf("// BEGIN conditional panel read"), b = src.indexOf("// END conditional panel read");
const calls = [];
let answer;
const fetch = async (url, init) => { calls.push({ url, headers: init.headers }); return answer(url, init); };
const readHeaders = () => ({});
const body = new Function("fetch", "readHeaders", src.slice(a, b) +
  "; return { fetchPanelConditional, pruneHeldPanels, panelUrl, panelBodies };")(fetch, readHeaders);
(async () => {
  const out = {};
  const u = body.panelUrl("loops");
  const panel = JSON.stringify({ id: "loops", as_of: "2026-11-10T10:00:00Z", rows: [1, 2] });
  answer = () => new Response(panel, { status: 200, headers: { "Content-Type": "application/json; charset=utf-8", "ETag": 'W/"abc"' } });
  const r1 = await body.fetchPanelConditional(u, { as_of: "2026-11-10T10:00:00Z" });
  out.first = { status: r1.status, etag: r1.headers.get("ETag"), type: r1.headers.get("Content-Type"), sent: calls[0].headers["If-None-Match"] || null };
  answer = () => new Response(null, { status: 304, headers: { "ETag": 'W/"abc"' } });
  const r2 = await body.fetchPanelConditional(u, { as_of: "2026-11-10T10:05:00Z" });
  const j2 = await r2.json();
  out.second = { status: r2.status, etag: r2.headers.get("ETag"), type: r2.headers.get("Content-Type"), sent: calls[1].headers["If-None-Match"], as_of: j2.as_of, rows: j2.rows };
  const r3 = await body.fetchPanelConditional(u, undefined);
  out.nohead = (await r3.json()).as_of;
  body.panelBodies.set(body.panelUrl("retired"), { etag: "x", text: "{}", type: "t" });
  body.pruneHeldPanels(["loops"]);
  out.kept = Array.from(body.panelBodies.keys());
  answer = () => new Response("nope", { status: 403 });
  const r4 = await body.fetchPanelConditional(u, undefined);
  out.refused = { status: r4.status, held: body.panelBodies.has(u) };
  console.log(JSON.stringify(out));
})();
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_the_conditional_panel_read_keeps_as_of_advancing_the_etag_readable_and_the_cache_pruned(tmp_path: Path) -> None:
    driver = tmp_path / "drive.js"
    driver.write_text(_NODE_DRIVER, encoding="utf-8")
    run = subprocess.run([shutil.which("node"), str(driver), str(WEB / "dashboard_boot.js")],
                         capture_output=True, text=True, timeout=30, check=True)
    out = json.loads(run.stdout)
    assert out["first"] == {"status": 200, "etag": 'W/"abc"', "type": "application/json; charset=utf-8", "sent": None}
    # the 304 sends the held validator, rebuilds the held body, and the freshness comes from the manifest head
    assert out["second"] == {"status": 200, "etag": 'W/"abc"', "type": "application/json; charset=utf-8",
                             "sent": 'W/"abc"', "as_of": "2026-11-10T10:05:00Z", "rows": [1, 2]}
    assert out["nohead"] == "2026-11-10T10:00:00Z"        # no manifest head: the held body is served as it was
    assert out["kept"] == ["/cockpit/panel/loops.json?profile=full"]
    assert out["refused"] == {"status": 403, "held": False}

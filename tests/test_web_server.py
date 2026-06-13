"""Tests for levain.web_server — the Slice-1 local sovereign web-app.

The server is exercised as a REAL bound ``ThreadingHTTPServer`` serving over a
loopback socket against a real (temp) anneal store — routes are hit with
``urllib``, response bodies + headers + the read-only invariant are asserted, not
mocked. The one thing NOT machine-checkable here is the in-browser render (needs
a live browser); that is the documented L4 manual canary.

Load-bearing guards: ``/substrate.json`` carries the full SubstrateView shape; the
route set is a closed allowlist (no path-traversal surface); requests never mutate
the store; the server binds loopback.
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from contextlib import contextmanager
from pathlib import Path

from levain.dashboard import AnnealPaths, SubstrateSource
from levain.web_server import (
    build_substrate_json,
    load_web_asset,
    make_server,
    run_web_server,
)


def _store_with_data(tmp_path: Path) -> SubstrateSource:
    """A populated substrate: a graduated association (live write-path), one open
    spore, and a two-section continuity. Mirrors the app_server fixture so the two
    surfaces are tested against the same shape. Returned as a ``SubstrateSource``
    (no install root — these temp stores aren't a full ``.levain`` install, so the
    seed/config tier is simply absent)."""
    from anneal_memory import Store
    from anneal_memory.spores import SporeStore

    db = tmp_path / "memory.db"
    with Store(db) as store:
        a = store.record("decided X because Y", "decision")
        b = store.record("noticed Z", "observation")
        store.record_associations({(a.id, b.id)})
    SporeStore(tmp_path / "memory.spores.json").add(
        type="task", text="an open loop", tier="hot", salience=2
    )
    (tmp_path / "memory.continuity.md").write_text(
        "## State\ncurrent focus line\n\n## Active Threads\n- thread one\n",
        encoding="utf-8",
    )
    return SubstrateSource(anneal=AnnealPaths.from_db(db))


@contextmanager
def _serving(source: SubstrateSource):
    """Bring up a real server on an ephemeral loopback port, yield its base URL,
    and tear it down cleanly — the live integration harness for the route tests."""
    httpd = make_server(source, host="127.0.0.1", port=0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    host, port = httpd.server_address[0], httpd.server_address[1]
    try:
        yield f"http://{host}:{port}", httpd
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def _get(url: str):
    with urllib.request.urlopen(url, timeout=5) as r:  # noqa: S310 — loopback only
        return r.status, dict(r.headers), r.read()


def _request(url: str, *, method: str = "GET", headers: dict | None = None):
    req = urllib.request.Request(url, method=method, headers=headers or {})
    with urllib.request.urlopen(req, timeout=5) as r:  # noqa: S310 — loopback only
        return r.status, dict(r.headers), r.read()


# --- the data endpoint: shape parity with the SubstrateView ----------------

class TestSubstrateJson:
    def test_build_substrate_json_shape(self, tmp_path: Path) -> None:
        data = json.loads(build_substrate_json(_store_with_data(tmp_path)))
        for key in ("paths", "health", "graph", "crystal_index", "open_spores",
                    "sections", "errors"):
            assert key in data
        assert data["health"]["total_episodes"] == 2
        assert data["health"]["write_path_live"] is True
        assert len(data["open_spores"]) == 1
        assert data["sections"][0]["heading"] == "State"

    def test_endpoint_serves_the_view(self, tmp_path: Path) -> None:
        with _serving(_store_with_data(tmp_path)) as (base, _):
            status, headers, body = _get(base + "/substrate.json")
        assert status == 200
        assert headers["Content-Type"] == "application/json; charset=utf-8"
        data = json.loads(body)
        assert data["health"]["total_episodes"] == 2
        assert len(data["open_spores"]) == 1

    def test_endpoint_ignores_query_string(self, tmp_path: Path) -> None:
        with _serving(_store_with_data(tmp_path)) as (base, _):
            status, _h, body = _get(base + "/substrate.json?cachebust=1")
        assert status == 200
        assert json.loads(body)["health"]["total_episodes"] == 2

    def test_missing_store_degrades_not_crashes(self, tmp_path: Path) -> None:
        """A bare source with no db yields a renderable, degraded view — the
        endpoint must 200 with an errors entry, never 500 / dead-connection."""
        source = SubstrateSource(anneal=AnnealPaths.from_db(tmp_path / "nope" / "memory.db"))
        with _serving(source) as (base, _):
            status, _h, body = _get(base + "/substrate.json")
        assert status == 200
        assert "store" in json.loads(body)["errors"]


# --- the static assets ------------------------------------------------------

class TestAssets:
    def test_load_web_asset_reads_each(self) -> None:
        html = load_web_asset("dashboard.html")
        core = load_web_asset("dashboard_core.js")
        boot = load_web_asset("dashboard_boot.js")
        css = load_web_asset("dashboard.css")
        assert "<!DOCTYPE html>" in html
        # the page wires both served scripts + the served stylesheet, NO external/CDN
        # import and NO inline <style> (Slice 2a moved CSS out → style-src 'self').
        assert '/dashboard_core.js' in html
        assert '/dashboard_boot.js' in html
        assert '/dashboard.css' in html
        assert "<style>" not in html
        assert ".panel" in css and ".edit-btn" in css  # the moved sheet + the 2a affordances
        assert "unpkg.com" not in html and "modelcontextprotocol" not in html
        # the core exposes the one render entry point the boot shim calls
        assert "window.LevainDashboard" in core
        # the boot shim is THIS surface's transport: fetch from the localhost JSON
        # endpoint, NOT the MCP-Apps `new App(...)` / ontoolresult lifecycle
        assert "/substrate.json" in boot
        assert "fetch(" in boot
        assert "new App(" not in boot and "app.connect(" not in boot

    def test_index_route(self, tmp_path: Path) -> None:
        with _serving(_store_with_data(tmp_path)) as (base, _):
            status, headers, body = _get(base + "/")
        assert status == 200
        assert headers["Content-Type"] == "text/html; charset=utf-8"
        # the page identifies as Levain AND serves the Identity·Operate·Mind tab bar
        assert b"Levain" in body
        assert b'class="tabs"' in body and b'data-zone="mind"' in body

    def test_script_routes(self, tmp_path: Path) -> None:
        with _serving(_store_with_data(tmp_path)) as (base, _):
            for route in ("/dashboard_core.js", "/dashboard_boot.js"):
                status, headers, body = _get(base + route)
                assert status == 200, route
                assert headers["Content-Type"] == "text/javascript; charset=utf-8"
                assert body, route

    def test_stylesheet_route(self, tmp_path: Path) -> None:
        with _serving(_store_with_data(tmp_path)) as (base, _):
            status, headers, body = _get(base + "/dashboard.css")
        assert status == 200
        assert headers["Content-Type"] == "text/css; charset=utf-8"
        assert b".panel" in body


# --- security boundary: closed allowlist, headers, loopback ----------------

class TestSecurityBoundary:
    def test_unknown_route_404(self, tmp_path: Path) -> None:
        with _serving(_store_with_data(tmp_path)) as (base, _):
            try:
                _get(base + "/nope")
                raised = None
            except urllib.error.HTTPError as e:
                raised = e
        assert raised is not None and raised.code == 404

    def test_no_path_traversal(self, tmp_path: Path) -> None:
        """No path → filesystem mapping exists, so a traversal attempt is just an
        unknown route → 404 (never reads a file off disk)."""
        with _serving(_store_with_data(tmp_path)) as (base, _):
            for evil in ("/../../etc/passwd", "/../web_server.py", "/levain/cli.py"):
                try:
                    _get(base + evil)
                    code = 200
                except urllib.error.HTTPError as e:
                    code = e.code
                assert code == 404, evil

    def test_security_headers_present(self, tmp_path: Path) -> None:
        # the data-bearing route must carry the headers too, not just `/`
        with _serving(_store_with_data(tmp_path)) as (base, _):
            for route in ("/", "/substrate.json"):
                _s, headers, _b = _get(base + route)
                csp = headers["Content-Security-Policy"]
                assert "default-src 'none'" in csp, route
                assert "script-src 'self'" in csp, route
                # Slice 2a: CSS is a served sheet now → style-src 'self', no inline.
                assert "style-src 'self'" in csp, route
                assert "unsafe-inline" not in csp, route
                assert "frame-ancestors 'none'" in csp, route
                assert headers["X-Content-Type-Options"] == "nosniff", route
                assert headers["X-Frame-Options"] == "DENY", route
                assert headers["Cache-Control"] == "no-store", route

    def test_binds_loopback(self, tmp_path: Path) -> None:
        with _serving(_store_with_data(tmp_path)) as (_base, httpd):
            assert httpd.server_address[0] == "127.0.0.1"

    def test_rejects_non_loopback_host_header(self, tmp_path: Path) -> None:
        """DNS-rebinding guard: a request whose Host isn't a loopback name (the
        shape a rebinding attacker's page sends) is refused 403 — the substrate is
        never disclosed cross-origin."""
        with _serving(_store_with_data(tmp_path)) as (base, _):
            try:
                _request(base + "/substrate.json", headers={"Host": "evil.com"})
                code = 200
            except urllib.error.HTTPError as e:
                code = e.code
        assert code == 403

    def test_localhost_host_header_allowed(self, tmp_path: Path) -> None:
        with _serving(_store_with_data(tmp_path)) as (base, httpd):
            port = httpd.server_address[1]
            status, _h, body = _request(
                base + "/substrate.json", headers={"Host": f"localhost:{port}"}
            )
        assert status == 200
        assert json.loads(body)["health"]["total_episodes"] == 2

    def test_host_check_is_case_and_dot_insensitive(self, tmp_path: Path) -> None:
        """RFC 7230: host comparison is case-insensitive, and a trailing FQDN dot
        is equivalent — neither should false-reject a legitimate loopback Host."""
        with _serving(_store_with_data(tmp_path)) as (base, httpd):
            port = httpd.server_address[1]
            for h in (f"LOCALHOST:{port}", f"localhost.:{port}", f"  localhost:{port}"):
                status, _hd, _b = _request(base + "/", headers={"Host": h})
                assert status == 200, h

    def test_malformed_bracket_host_rejected(self, tmp_path: Path) -> None:
        with _serving(_store_with_data(tmp_path)) as (base, _):
            try:
                _request(base + "/", headers={"Host": "[::1]evil"})
                code = 200
            except urllib.error.HTTPError as e:
                code = e.code
        assert code == 403

    def test_cross_site_request_rejected(self, tmp_path: Path) -> None:
        """A hostile cross-site page's fetch (Sec-Fetch-Site: cross-site) is refused
        before it can even trigger the per-request store read."""
        with _serving(_store_with_data(tmp_path)) as (base, _):
            try:
                _request(
                    base + "/substrate.json",
                    headers={"Sec-Fetch-Site": "cross-site"},
                )
                code = 200
            except urllib.error.HTTPError as e:
                code = e.code
        assert code == 403

    def test_same_origin_fetch_allowed(self, tmp_path: Path) -> None:
        with _serving(_store_with_data(tmp_path)) as (base, _):
            status, _h, _b = _request(
                base + "/substrate.json", headers={"Sec-Fetch-Site": "same-origin"}
            )
        assert status == 200

    def test_server_header_hides_python_version(self, tmp_path: Path) -> None:
        with _serving(_store_with_data(tmp_path)) as (base, _):
            _s, headers, _b = _get(base + "/")
        assert headers["Server"] == "levain-serve"
        assert "Python/" not in headers["Server"]


# --- HTTP wire contract: framing + HEAD ------------------------------------

class TestHttpContract:
    def test_content_length_matches_body(self, tmp_path: Path) -> None:
        with _serving(_store_with_data(tmp_path)) as (base, _):
            for route in ("/", "/dashboard_core.js", "/substrate.json"):
                _s, headers, body = _get(base + route)
                assert int(headers["Content-Length"]) == len(body), route

    def test_head_sends_headers_and_framing_no_body(self, tmp_path: Path) -> None:
        """HEAD must report the Content-Length the GET WOULD return, with no body —
        or an HTTP/1.1 keep-alive connection desyncs."""
        with _serving(_store_with_data(tmp_path)) as (base, _):
            _gs, ghead, gbody = _get(base + "/substrate.json")
            hs, hhead, hbody = _request(base + "/substrate.json", method="HEAD")
        assert hs == 200
        assert hbody == b""  # no body on HEAD
        # but the framing matches what the GET body would have been
        assert hhead["Content-Length"] == ghead["Content-Length"]
        assert int(hhead["Content-Length"]) == len(gbody)
        assert hhead["Content-Type"] == "application/json; charset=utf-8"

    def test_head_on_unknown_route_404(self, tmp_path: Path) -> None:
        with _serving(_store_with_data(tmp_path)) as (base, _):
            try:
                _request(base + "/nope", method="HEAD")
                code = 200
            except urllib.error.HTTPError as e:
                code = e.code
        assert code == 404

    def test_head_on_asset_matches_get_framing(self, tmp_path: Path) -> None:
        with _serving(_store_with_data(tmp_path)) as (base, _):
            _gs, gh, gbody = _get(base + "/dashboard_core.js")
            hs, hh, hbody = _request(base + "/dashboard_core.js", method="HEAD")
        assert hs == 200 and hbody == b""
        assert hh["Content-Length"] == gh["Content-Length"]
        assert int(hh["Content-Length"]) == len(gbody)


# --- the loopback-bind boundary (unauthenticated read surface) -------------

class TestLoopbackBindBoundary:
    def test_make_server_refuses_non_loopback(self, tmp_path: Path) -> None:
        import pytest

        from levain.web_server import make_server

        for bad in ("0.0.0.0", "192.168.1.5", "::1"):  # ::1 = IPv6, Slice-2+ nicety
            with pytest.raises(ValueError, match="loopback"):
                make_server(_store_with_data(tmp_path), host=bad)

    def test_make_server_allows_loopback(self, tmp_path: Path) -> None:
        from levain.web_server import make_server

        for ok in ("127.0.0.1", "localhost"):  # both reliably bind everywhere
            httpd = make_server(_store_with_data(tmp_path), host=ok, port=0)
            try:
                assert httpd.server_address[0].startswith("127.")
            finally:
                httpd.server_close()

    def test_is_loopback_host_predicate(self) -> None:
        from levain.web_server import _is_loopback_host

        assert _is_loopback_host("127.0.0.1")
        assert _is_loopback_host("127.0.0.2")  # all of 127.0.0.0/8 is loopback
        assert _is_loopback_host("localhost")
        assert not _is_loopback_host("0.0.0.0")
        assert not _is_loopback_host("192.168.1.5")
        assert not _is_loopback_host("::1")  # IPv6 bind deferred to Slice 2
        assert not _is_loopback_host("evil.com")

    def test_run_web_server_refuses_non_loopback(self, tmp_path: Path, capsys) -> None:
        from anneal_memory import Store

        levain_dir = tmp_path / ".levain"
        levain_dir.mkdir()
        with Store(levain_dir / "memory.db"):
            pass
        rc = run_web_server(tmp_path, host="0.0.0.0", open_browser=False)
        assert rc == 1
        assert "loopback-only" in capsys.readouterr().err


# --- the handler's own fault path (untested defensive code) ----------------

class TestHandlerFaultPath:
    def test_unexpected_fault_degrades_to_error_json(self, tmp_path: Path, monkeypatch) -> None:
        """If building the snapshot raises an UNEXPECTED fault (past the data-layer
        degradation), the handler must still 200 with an errors.server body — never
        a 500 / dead connection."""
        import levain.web_server as ws

        def boom(_paths):
            raise RuntimeError("kaboom")

        monkeypatch.setattr(ws, "build_substrate_json", boom)
        with _serving(_store_with_data(tmp_path)) as (base, _):
            status, headers, body = _get(base + "/substrate.json")
        assert status == 200
        assert headers["Content-Type"] == "application/json; charset=utf-8"
        data = json.loads(body)
        assert "kaboom" in data["errors"]["server"]


# --- the read-only invariant ------------------------------------------------

class TestReadOnly:
    def test_requests_do_not_mutate_store(self, tmp_path: Path) -> None:
        source = _store_with_data(tmp_path)
        db = source.anneal.episodic_db
        before = db.stat().st_mtime_ns
        with _serving(source) as (base, _):
            _get(base + "/substrate.json")
            _get(base + "/substrate.json")
            _get(base + "/")
        assert db.stat().st_mtime_ns == before


# --- the startup contract (run_web_server / CLI) ---------------------------

class TestStartupContract:
    def test_missing_store_returns_1(self, tmp_path: Path, capsys) -> None:
        # no .levain/memory.db under tmp_path → returns before binding
        rc = run_web_server(tmp_path, open_browser=False)
        assert rc == 1
        assert "No anneal store" in capsys.readouterr().err

    def test_via_cli_main_missing_store(self, tmp_path: Path, capsys) -> None:
        from levain.cli import main

        rc = main(["serve", "--path", str(tmp_path), "--no-open"])
        assert rc == 1
        assert "No anneal store" in capsys.readouterr().err

    def test_port_in_use_returns_1(self, tmp_path: Path, capsys) -> None:
        """Store present but the chosen port is taken → rc=1 with a clear hint,
        not a traceback."""
        from anneal_memory import Store

        levain_dir = tmp_path / ".levain"
        levain_dir.mkdir()
        with Store(levain_dir / "memory.db"):
            pass

        # occupy a port, then ask the server to bind the same one
        blocker = make_server(
            SubstrateSource(anneal=AnnealPaths.from_db(levain_dir / "memory.db")), port=0
        )
        busy_port = blocker.server_address[1]
        try:
            rc = run_web_server(
                tmp_path, host="127.0.0.1", port=busy_port, open_browser=False
            )
        finally:
            blocker.server_close()
        assert rc == 1
        err = capsys.readouterr().err
        assert "Could not bind" in err and "--port" in err


# --- Slice 2a: the governed write boundary (POST /edit) ---------------------

_W_WORLD = (
    "# Who Your Operator Is\n\n> Seed material — operator template.\n\n"
    "## Identity\n\nPhill. 46. Columbus, OH.\n\n"
    "## Communication\n\nDirect, profanity welcome.\n"
)
_W_ORIGIN = "# Who You Are — Aria\n\nA new entity.\n"
_W_CONST = "# Constitution\n\nUniversal core.\n"


def _make_full_install(tmp_path: Path) -> SubstrateSource:
    """A full Levain install (seed + activation + .levain) so the write route has a
    real install_root + Class-A files to edit. No anneal store is needed for
    Class-A FILE edits (make_server doesn't require the db to exist)."""
    root = tmp_path / "install"
    (root / "seed").mkdir(parents=True)
    (root / "activation").mkdir(parents=True)
    (root / ".levain").mkdir()
    (root / "seed" / "world.md").write_text(_W_WORLD, encoding="utf-8")
    (root / "seed" / "origin.md").write_text(_W_ORIGIN, encoding="utf-8")
    (root / "seed" / "partnership.md").write_text(_W_CONST, encoding="utf-8")
    (root / "seed" / "memory.md").write_text(_W_CONST, encoding="utf-8")
    (root / "seed" / "spore_instructions.md").write_text(_W_CONST, encoding="utf-8")
    (root / "activation" / "posture.md").write_text("Slow is fast.\n", encoding="utf-8")
    (root / "activation" / "recency_directives.md").write_text("No gatekeeping.\n", encoding="utf-8")
    return SubstrateSource.local(root)


def _post(url: str, payload, *, headers: dict | None = None, content_type: str = "application/json"):
    data = json.dumps(payload).encode("utf-8")
    h = {} if content_type is None else {"Content-Type": content_type}
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, data=data, method="POST", headers=h)
    try:
        with urllib.request.urlopen(req, timeout=5) as r:  # noqa: S310 — loopback only
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


class TestWriteBoundary:
    def test_world_section_edit_happy_path(self, tmp_path: Path) -> None:
        src = _make_full_install(tmp_path)
        with _serving(src) as (base, _httpd):
            status, body = _post(base + "/edit", {
                "kind": "config", "source": "seed/world.md", "heading": "Identity",
                "expected_body": "Phill. 46. Columbus, OH.", "new_body": "Topological mind.",
            })
        assert status == 200 and body["ok"] is True
        out = (src.install_root / "seed" / "world.md").read_text(encoding="utf-8")
        assert "Topological mind." in out
        assert "Direct, profanity welcome." in out  # sibling preserved

    def test_entity_name_edit(self, tmp_path: Path) -> None:
        src = _make_full_install(tmp_path)
        with _serving(src) as (base, _httpd):
            status, body = _post(base + "/edit", {"kind": "entity_name", "value": "Sol"})
        assert status == 200
        cfg = json.loads((src.install_root / ".levain" / "config.json").read_text("utf-8"))
        assert cfg["entity_name"] == "Sol"

    def test_cross_site_refused(self, tmp_path: Path) -> None:
        src = _make_full_install(tmp_path)
        with _serving(src) as (base, _httpd):
            status, body = _post(base + "/edit", {
                "kind": "config", "source": "seed/world.md", "heading": "Identity",
                "expected_body": "Phill. 46. Columbus, OH.", "new_body": "x",
            }, headers={"Sec-Fetch-Site": "cross-site"})
        assert status == 403 and body["error"] == "forbidden"
        # the file is untouched
        assert "Phill. 46." in (src.install_root / "seed" / "world.md").read_text("utf-8")

    def test_same_site_refused(self, tmp_path: Path) -> None:
        src = _make_full_install(tmp_path)
        with _serving(src) as (base, _httpd):
            status, _ = _post(base + "/edit", {"kind": "entity_name", "value": "x"},
                              headers={"Sec-Fetch-Site": "same-site"})
        assert status == 403

    def test_same_origin_allowed(self, tmp_path: Path) -> None:
        src = _make_full_install(tmp_path)
        with _serving(src) as (base, _httpd):
            status, _ = _post(base + "/edit", {"kind": "entity_name", "value": "Ok"},
                              headers={"Sec-Fetch-Site": "same-origin"})
        assert status == 200

    def test_wrong_content_type_415(self, tmp_path: Path) -> None:
        src = _make_full_install(tmp_path)
        with _serving(src) as (base, _httpd):
            status, body = _post(base + "/edit", {"kind": "entity_name", "value": "x"},
                                 content_type="text/plain")
        assert status == 415 and body["error"] == "unsupported_media_type"

    def test_class_c_origin_refused(self, tmp_path: Path) -> None:
        src = _make_full_install(tmp_path)
        with _serving(src) as (base, _httpd):
            status, body = _post(base + "/edit", {
                "kind": "config", "source": "seed/origin.md", "heading": None,
                "expected_body": _W_ORIGIN, "new_body": "hacked",
            })
        assert status == 403 and body["error"] == "not_editable"
        assert (src.install_root / "seed" / "origin.md").read_text("utf-8") == _W_ORIGIN

    def test_stale_409(self, tmp_path: Path) -> None:
        src = _make_full_install(tmp_path)
        with _serving(src) as (base, _httpd):
            status, body = _post(base + "/edit", {
                "kind": "config", "source": "seed/world.md", "heading": "Identity",
                "expected_body": "WRONG", "new_body": "x",
            })
        assert status == 409 and body["error"] == "stale"

    def test_bad_host_refused(self, tmp_path: Path) -> None:
        src = _make_full_install(tmp_path)
        with _serving(src) as (base, _httpd):
            status, _ = _post(base + "/edit", {"kind": "entity_name", "value": "x"},
                              headers={"Host": "evil.com"})
        assert status == 403

    def test_unknown_route_404(self, tmp_path: Path) -> None:
        src = _make_full_install(tmp_path)
        with _serving(src) as (base, _httpd):
            status, _ = _post(base + "/wat", {"kind": "entity_name", "value": "x"})
        assert status == 404

    def test_bad_json_400(self, tmp_path: Path) -> None:
        src = _make_full_install(tmp_path)
        with _serving(src) as (base, _httpd):
            data = b"{not json"
            req = urllib.request.Request(
                base + "/edit", data=data, method="POST",
                headers={"Content-Type": "application/json"},
            )
            try:
                with urllib.request.urlopen(req, timeout=5) as r:  # noqa: S310
                    status = r.status
            except urllib.error.HTTPError as e:
                status = e.code
        assert status == 400

    def test_oversize_413(self, tmp_path: Path) -> None:
        from levain.web_server import _MAX_POST_BYTES

        src = _make_full_install(tmp_path)
        big = "z" * (_MAX_POST_BYTES + 1024)
        with _serving(src) as (base, _httpd):
            status, body = _post(base + "/edit", {
                "kind": "config", "source": "seed/world.md", "heading": "Identity",
                "expected_body": "Phill. 46. Columbus, OH.", "new_body": big,
            })
        assert status == 413

    def test_get_to_edit_is_404(self, tmp_path: Path) -> None:
        # /edit is POST-only; a GET falls through the read allowlist to 404.
        src = _make_full_install(tmp_path)
        with _serving(src) as (base, _httpd):
            try:
                status, _, _ = _request(base + "/edit", method="GET")
            except urllib.error.HTTPError as e:
                status = e.code
        assert status == 404

    def test_rate_gate_503(self, tmp_path: Path) -> None:
        # Saturate the bounded gate, then a store read returns a clean 503.
        from levain.web_server import _MAX_INFLIGHT

        src = _make_full_install(tmp_path)
        with _serving(src) as (base, httpd):
            held = [httpd.request_gate.acquire(blocking=False) for _ in range(_MAX_INFLIGHT)]
            assert all(held)
            try:
                status, _, _ = _request(base + "/substrate.json", method="GET")
            except urllib.error.HTTPError as e:
                status = e.code
            finally:
                for _ in range(_MAX_INFLIGHT):
                    httpd.request_gate.release()
        assert status == 503

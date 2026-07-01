"""levain.docs_server — ``levain docs``: the composed operator manual in the browser.

Serves the base manual (shipped in the wheel) COMPOSED with any installed pack's
chapters (`levain.docs.discover_chapters`) as a single, read-only local web page.
Same multi-root layering as the seed roster, applied to docs.

READ-ONLY BY CONSTRUCTION. There is no write route, no store, no install — just
GET/HEAD of four static assets and one JSON projection of the composed chapters.
So this is a strict SUBSET of the init/dashboard servers: no POST, no lock, no
input validation. It still rides the SAME security envelope those surfaces share,
re-used rather than re-implemented so a contract can't diverge: loopback-only bind
(refused before AND re-verified after binding — a tampered hosts file can't map a
loopback name off-box), the DNS-rebinding Host allowlist (`host_header_allowed`),
the Sec-Fetch-Site cross-site read refusal, the CSP + security headers stamped on
EVERY response (`end_headers`, so framework-generated error responses carry them
too), and `no-store`.

The composed payload is built ONCE at server construction (the manual is static
for a serve session) and served from cache — so a corrupt wheel (missing base
docs) fails at startup with a clear message, never as a silent empty page.
"""

from __future__ import annotations

import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from levain.docs import DocsError, chapters_payload
from levain.web_server import (
    _CSP,
    _LOOPBACK_HOSTS,
    _is_loopback_host,
    host_header_allowed,
    load_web_asset,
)

__all__ = ["DEFAULT_DOCS_HOST", "DEFAULT_DOCS_PORT", "make_docs_server", "run_docs_web"]

DEFAULT_DOCS_HOST = "127.0.0.1"
# A distinct default port from the dashboard's 7420 and init's 7430, so an operator
# can run a dashboard, an onboarding, and the docs at once without a collision.
DEFAULT_DOCS_PORT = 7440

# The docs page's static assets, by request path → (package-data filename,
# content-type). An explicit allowlist; no path → filesystem mapping anywhere, so a
# crafted path cannot escape this set. `markdown.js` is the SAME reviewed renderer
# the dashboard uses (drift-locked by a parity test).
_ASSETS: dict[str, tuple[str, str]] = {
    "/": ("docs.html", "text/html; charset=utf-8"),
    "/docs.css": ("docs.css", "text/css; charset=utf-8"),
    "/docs.js": ("docs.js", "text/javascript; charset=utf-8"),
    "/markdown.js": ("markdown.js", "text/javascript; charset=utf-8"),
}


class _DocsServer(ThreadingHTTPServer):
    """A ``ThreadingHTTPServer`` carrying the composed docs JSON (built once), the
    cached static assets, and the Host allowlist. Typed so the handler's access is
    checked and can't silently break."""

    docs_json: bytes
    assets: dict[str, bytes]
    allowed_hosts: frozenset[str]

    def handle_error(self, request: Any, client_address: Any) -> None:
        """Swallow the benign client-disconnect family (idle keep-alive resets)
        instead of dumping a traceback; defer genuine errors to the base so real
        bugs still surface (mirrors ``_LevainHTTPServer`` / ``_InitServer``)."""
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionError, TimeoutError)):
            return
        super().handle_error(request, client_address)


class _DocsHandler(BaseHTTPRequestHandler):
    """Serves the docs page (GET/HEAD only) behind the same DNS-rebinding Host
    allowlist + cross-site read refusal the dashboard/init servers use. No write
    route exists — an unsupported method falls through to the stdlib's 501, which
    still carries the security headers via ``end_headers``."""

    protocol_version = "HTTP/1.1"
    server_version = "levain-docs"
    timeout = 30
    server: _DocsServer  # narrow the type for typed attribute access

    def end_headers(self) -> None:
        """Stamp the security headers on EVERY response — structurally, so the
        invariant can't be skipped. ``_send`` is the normal path, but the stdlib's
        ``send_error`` (an unsupported method, a malformed request) builds its own
        response that never passes through ``_send``; putting the headers here
        covers those framework-generated responses too."""
        self.send_header("Content-Security-Policy", _CSP)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def _send(
        self, body: bytes, content_type: str, status: int = 200, *, head: bool = False
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        if self.close_connection:
            self.send_header("Connection", "close")
        self.end_headers()
        if not head:
            self.wfile.write(body)

    def version_string(self) -> str:
        return self.server_version

    def _host_ok(self) -> bool:
        return host_header_allowed(self.headers.get("Host"), self.server.allowed_hosts)

    def _route(self, *, head: bool) -> None:
        if not self._host_ok():
            self._send(b"forbidden\n", "text/plain; charset=utf-8", status=403, head=head)
            return
        # Defense-in-depth: refuse a cross-site browser read (a same-origin fetch
        # sends same-origin; a top-level nav sends none; non-browser clients omit
        # it). The same cheap layer the dashboard/init read paths use.
        if self.headers.get("Sec-Fetch-Site") == "cross-site":
            self._send(b"forbidden\n", "text/plain; charset=utf-8", status=403, head=head)
            return

        path = self.path.split("?", 1)[0]

        if path == "/docs.json":
            self._send(
                self.server.docs_json, "application/json; charset=utf-8", head=head
            )
            return

        asset = _ASSETS.get(path)
        if asset is not None:
            filename, content_type = asset
            self._send(self.server.assets[filename], content_type, head=head)
            return

        self._send(b"not found\n", "text/plain; charset=utf-8", status=404, head=head)

    def do_GET(self) -> None:  # noqa: N802 — BaseHTTPRequestHandler contract
        self._route(head=False)

    def do_HEAD(self) -> None:  # noqa: N802 — same routing, headers only
        self._route(head=True)

    def log_message(self, fmt: str, *args: object) -> None:
        """Quiet by default; set ``LEVAIN_SERVE_VERBOSE`` to restore the access log."""
        import os

        if os.environ.get("LEVAIN_SERVE_VERBOSE"):
            super().log_message(fmt, *args)


def make_docs_server(
    install: Path,
    *,
    host: str = DEFAULT_DOCS_HOST,
    port: int = DEFAULT_DOCS_PORT,
) -> _DocsServer:
    """Build a configured, bound (not-yet-serving) docs server.

    LOOPBACK-ONLY: a non-loopback ``host`` is refused before binding (the docs are
    a local read surface; there is no reason to expose them off-box). The composed
    chapters payload + the static assets are built BEFORE the bind, so a corrupt
    wheel (missing base docs → :class:`DocsError`, or a missing asset →
    ``FileNotFoundError``) fails cleanly at startup, not as a port error. Raises
    ``ValueError`` on a non-loopback host; ``DocsError`` on missing base docs;
    ``FileNotFoundError`` on a missing asset; ``OSError`` if the bind fails.
    Separated from ``run_docs_web`` so tests can drive a real bound server without
    the print/browser/serve_forever wrapper."""
    if not _is_loopback_host(host):
        raise ValueError(
            f"refusing to bind {host!r}: `levain docs` is loopback-only "
            "(127.0.0.1 / localhost). The operator manual is a local read surface; "
            "there is no off-box docs server."
        )
    # Build the payload + assets BEFORE binding — a corrupt wheel should fail with a
    # clear message, not a bind error (mirrors init's asset-load-before-bind).
    docs_json = json.dumps(chapters_payload(install)).encode("utf-8")
    assets = {fn: load_web_asset(fn).encode("utf-8") for fn, _ in _ASSETS.values()}

    httpd = _DocsServer((host, port), _DocsHandler)
    httpd.docs_json = docs_json
    httpd.assets = assets
    # Re-verify the ACTUAL bound address is loopback (verify-the-bound-address, not
    # the resolver — a tampered hosts file could map 'localhost' off-box).
    bound = str(httpd.server_address[0])
    if not _is_loopback_host(bound):
        httpd.server_close()
        raise ValueError(
            f"refusing to serve: requested host {host!r} bound a non-loopback address "
            f"({bound}). `levain docs` is loopback-only."
        )
    # Allow the canonical loopback names + the exact bound address (covers a
    # 127.0.0.x bind), lowercased to match the Host check; any other Host → 403.
    httpd.allowed_hosts = _LOOPBACK_HOSTS | {bound.lower()}
    return httpd


def run_docs_web(
    path: Path,
    *,
    host: str = DEFAULT_DOCS_HOST,
    port: int = DEFAULT_DOCS_PORT,
    open_browser: bool = True,
) -> int:
    """``levain docs`` entry point — serve the composed operator manual on localhost.

    Returns nonzero only if the bind fails, the host is refused, or the wheel is
    corrupt (missing base docs / assets). Blocks in ``serve_forever`` until
    interrupted (Ctrl+C → clean exit 0)."""
    install = Path(str(path)).expanduser().resolve()

    try:
        httpd = make_docs_server(install, host=host, port=port)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except DocsError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except FileNotFoundError as exc:
        # The web assets load (from package data) BEFORE the bind — a missing asset
        # is a packaging failure, NOT a port-in-use bind error.
        print(
            f"Levain web assets not found in the installed package ({exc}). The wheel "
            f"may be corrupt; reinstall with `pip install --force-reinstall levain`.",
            file=sys.stderr,
        )
        return 1
    except OSError as exc:
        print(
            f"Could not bind {host}:{port} — {exc}.\n"
            "The port may be in use; try `levain docs --port <N>`.",
            file=sys.stderr,
        )
        return 1

    bound_host, bound_port = str(httpd.server_address[0]), httpd.server_address[1]
    host_for_url = f"[{bound_host}]" if ":" in bound_host else bound_host
    url = f"http://{host_for_url}:{bound_port}/"
    n_chapters = len(json.loads(httpd.docs_json)["chapters"])
    print(f"Levain docs → {url}")
    print(f"  {n_chapters} chapter(s) · install: {install}")
    print("  loopback-only · read-only · Ctrl+C to stop")

    if open_browser:
        import webbrowser

        try:
            webbrowser.open(url)
        except Exception:  # noqa: BLE001 — a headless box without a browser is fine
            pass

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped.")
    finally:
        httpd.server_close()
    return 0

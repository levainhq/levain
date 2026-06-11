"""levain.web_server — Levain v2: the local SOVEREIGN web-app (``levain serve``).

This is the v2 control surface's primary face: the substrate dashboard rendered
in YOUR browser, served by a localhost-bound process YOU run. Where
``levain dashboard`` (``dashboard.py``) prints the substrate to a terminal and
the parked ``levain serve-app`` (``app_server.py``) renders it inside a vendor's
chat host, this serves it on a sovereign surface — your machine, your browser,
no rented vendor glass, no CDN, no account.

It is a thin port ON PURPOSE — the same discipline that made the pivot off the
in-host MCP-App cheap. The compounding value is the governed cargo underneath
(anneal's developmental memory). The rails are commodity and disposable, so this
wrapper stays minimal: the already-built ``SubstrateView`` data layer
(``build_substrate_view``) does all the work, and the browser renders it with the
shared, transport-agnostic ``dashboard_core.js``. Swapping the MCP-Apps
``app.ontoolresult`` lifecycle for ``fetch('/substrate.json')`` is the whole
delta between the two surfaces.

Slice-1 properties carried straight from the data layer:
- **read-only** — every request builds a fresh read-only ``SubstrateView``; the
  server has no write route and the store is opened ``read_only=True``. "Human is
  the fan-in" holds trivially: this surface acts on nothing. Slice 2 adds steering
  verbs ONLY behind an explicit approval boundary, at which point the
  bind-localhost line below becomes a genuine auth boundary.
- **migration-free** — reads the operator's EXISTING store; zero re-seed.
- **billing-immune** — a human runs it and reads it; never headless.

Sovereignty boundary (load-bearing, not incidental):
- **binds 127.0.0.1 by default** — reachable only from this machine. A sovereign
  substrate is not exposed on a network it doesn't have to be on, and a read-only
  surface that will grow write verbs must not be remotely reachable before the
  write/auth boundary lands.
- **Host-header allowlist** — bind-localhost stops *network* peers but NOT a
  *browser-mediated* DNS-rebinding attacker (a hostile page rebinds its name to
  127.0.0.1, becoming same-origin in the browser, and could read the substrate
  cross-origin). So every request's ``Host`` is checked against a loopback
  allowlist and anything else is a 403. That closes the rebinding disclosure now
  and is the structural seed of the Slice-2 write/auth boundary.
- **no dependencies, no CDN** — stdlib ``http.server`` + vanilla-JS assets served
  from package data. The dashboard renders with the network cable unplugged. A
  sovereign surface does not rent its client library either.
- **explicit route allowlist** — exactly four routes (``/`` + two scripts +
  ``/substrate.json``), GET and HEAD only; no filesystem mapping, so there is no
  path-traversal surface. Anything else is a flat 404.
"""

from __future__ import annotations

import ipaddress
import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from levain.dashboard import AnnealPaths, _resolve_store, build_substrate_view

__all__ = [
    "DEFAULT_HOST",
    "DEFAULT_PORT",
    "build_substrate_json",
    "load_web_asset",
    "make_server",
    "run_web_server",
]

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 7420

# The page only ever loads its own same-origin scripts and fetches its own JSON.
# Lock everything else off; allow inline styles only (the one inline thing left
# in the page). `frame-ancestors 'none'` denies clickjacking / hostile-iframe
# embedding (paired with X-Frame-Options below). Tightening style to a
# hashed/served sheet is a Slice-2 item.
_CSP = (
    "default-src 'none'; script-src 'self'; style-src 'unsafe-inline'; "
    "connect-src 'self'; img-src 'self'; base-uri 'none'; form-action 'none'; "
    "frame-ancestors 'none'"
)

# Loopback names a request's Host header may legitimately carry. A DNS-rebinding
# page rebinds its OWN name to 127.0.0.1, so its requests still arrive with
# ``Host: evil.com`` — rejecting any non-loopback Host closes that disclosure.
# The actual bound address is added to this set in ``make_server``. (Lowercase —
# the Host check normalizes case before comparing, per RFC 7230 §2.7.3.)
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


def _is_loopback_host(host: str) -> bool:
    """True iff ``host`` is a loopback bind address Slice 1 will serve on:
    ``localhost`` or an IPv4 loopback literal (127.0.0.0/8).

    Slice-1 ``serve`` is read-only AND unauthenticated, so it MUST refuse to bind
    anywhere reachable off this machine — binding a LAN/`0.0.0.0` address would
    make the substrate remotely readable. IPv6 (``::1``) bind needs an AF_INET6
    server and is a Slice-2+ nicety; rejected here for now (use 127.0.0.1)."""
    if host.lower() == "localhost":
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return ip.is_loopback and ip.version == 4

# The static assets, by request path → (package-data filename, content-type).
# An explicit allowlist: there is no path → filesystem mapping anywhere, so a
# crafted path cannot escape this set.
_ASSETS: dict[str, tuple[str, str]] = {
    "/": ("dashboard.html", "text/html; charset=utf-8"),
    "/dashboard_core.js": ("dashboard_core.js", "text/javascript; charset=utf-8"),
    "/dashboard_boot.js": ("dashboard_boot.js", "text/javascript; charset=utf-8"),
}


def load_web_asset(filename: str) -> str:
    """Read a web UI asset from package data (``templates/web/``).

    Shipped as ``package_data`` (``templates/**/*``); read via ``importlib`` so it
    resolves from an installed wheel, an editable install, or a zip alike — the
    same access path the MCP-App server uses for its bundle."""
    from importlib.resources import files

    return (files("levain") / "templates" / "web" / filename).read_text(
        encoding="utf-8"
    )


def build_substrate_json(paths: AnnealPaths) -> bytes:
    """The ``/substrate.json`` body: a fresh read-only ``SubstrateView`` snapshot.

    Pure read — ``build_substrate_view`` opens the episodic Store ``read_only=True``
    and degrades any unavailable tier into ``view.errors`` rather than failing, so
    this always yields a renderable view (the dashboard shows which tiers are dark
    rather than blanking). Built per request so the browser's refresh reflects the
    live store, including a wrap that landed since the page opened."""
    view = build_substrate_view(paths)
    return json.dumps(view.to_dict()).encode("utf-8")


class _LevainHTTPServer(ThreadingHTTPServer):
    """A ``ThreadingHTTPServer`` carrying the substrate paths, the cached static
    assets, and the Host-header allowlist. A typed subclass (rather than ad-hoc
    attributes on a plain server) so the handler's access is type-checked and
    can't silently break into an unhandled ``AttributeError`` if construction
    ever moves."""

    levain_paths: AnnealPaths
    levain_assets: dict[str, bytes]
    allowed_hosts: frozenset[str]


class _Handler(BaseHTTPRequestHandler):
    """Serves the four read-only routes (GET + HEAD). Store paths, cached assets,
    and the Host allowlist live on the typed server instance (``self.server``)."""

    # A tidy, modern protocol version (enables keep-alive + proper 1.1 behavior).
    protocol_version = "HTTP/1.1"
    server_version = "levain-serve"
    server: _LevainHTTPServer  # narrow the type for typed attribute access

    def _send(
        self, body: bytes, content_type: str, status: int = 200, *, head: bool = False
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        # Always the length the GET body WOULD be — so a HEAD reports correct
        # framing without a body, and GET/HEAD can never disagree on the wire.
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Content-Security-Policy", _CSP)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        # Snapshots are per-request; never let a browser cache a stale substrate.
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if not head:
            self.wfile.write(body)

    def version_string(self) -> str:
        # Don't leak the Python version: BaseHTTPRequestHandler's default Server
        # header appends "Python/X.Y". A localhost tool advertises nothing.
        return self.server_version

    def _host_ok(self) -> bool:
        """True iff the request's ``Host`` names a loopback the server answers for.

        Closes DNS-rebinding read-disclosure: bind-localhost stops network peers,
        but a hostile page that rebinds its own name to 127.0.0.1 still sends its
        own ``Host``, so a loopback-only allowlist refuses it (403). Parsing is
        strict (RFC 7230): the Host is normalized (trim, case-fold, drop the
        trailing FQDN dot) before comparison, and a bracketed-IPv6 form must be
        well-formed (``[host]`` optionally ``:port``) or it's refused — so neither
        ``LOCALHOST`` false-rejects nor ``[::1]evil`` sneaks through."""
        host = self.headers.get("Host")
        if host is None:
            return False  # HTTP/1.1 requires a Host; absent = refuse (fail-closed)
        host = host.strip()
        if host.startswith("["):  # bracketed IPv6 literal: [::1] or [::1]:port
            end = host.find("]")
            if end == -1:
                return False  # unterminated bracket
            hostname = host[1:end]
            rest = host[end + 1 :]
            if rest and not (rest.startswith(":") and rest[1:].isdigit()):
                return False  # junk after the bracket (e.g. "[::1]evil")
        else:
            head_part, sep, port = host.rpartition(":")
            if sep:
                if not port.isdigit():
                    return False  # a ":" that isn't a clean numeric port → malformed
                hostname = head_part
            else:
                hostname = host  # bare host, no port
        hostname = hostname.rstrip(".").lower()
        return hostname in self.server.allowed_hosts

    def _route(self, *, head: bool) -> None:
        if not self._host_ok():
            self._send(
                b"forbidden\n", "text/plain; charset=utf-8", status=403, head=head
            )
            return

        # Reject cross-site browser requests. A same-origin page fetch sends
        # `Sec-Fetch-Site: same-origin`; a top-level navigation sends `none`; only
        # a hostile cross-site page sends `cross-site`. Non-browser clients (curl,
        # urllib) omit the header entirely → allowed. This stops a cross-origin
        # page from even TRIGGERING the per-request store read (it can't read the
        # response anyway, post-Host-check) — a cheap defense-in-depth layer.
        if self.headers.get("Sec-Fetch-Site") == "cross-site":
            self._send(
                b"forbidden\n", "text/plain; charset=utf-8", status=403, head=head
            )
            return

        # Strip any query string; route on the bare path against the allowlist.
        path = self.path.split("?", 1)[0]

        if path == "/substrate.json":
            try:
                body = build_substrate_json(self.server.levain_paths)
            except Exception as exc:  # noqa: BLE001 — never 500 on a runtime fault
                # build_substrate_view already degrades data/IO faults into the
                # view; reaching here is an unexpected fault. Surface it as JSON the
                # UI can show, not a dead connection.
                body = json.dumps(
                    {"paths": {}, "errors": {"server": f"{type(exc).__name__}: {exc}"}}
                ).encode("utf-8")
            self._send(body, "application/json; charset=utf-8", head=head)
            return

        asset = _ASSETS.get(path)
        if asset is not None:
            filename, content_type = asset
            self._send(self.server.levain_assets[filename], content_type, head=head)
            return

        self._send(b"not found\n", "text/plain; charset=utf-8", status=404, head=head)

    def do_GET(self) -> None:  # noqa: N802 — BaseHTTPRequestHandler contract
        self._route(head=False)

    def do_HEAD(self) -> None:  # noqa: N802 — same routing, headers only (no body)
        self._route(head=True)

    def log_message(self, fmt: str, *args: object) -> None:
        """Quiet by default — the server prints its own startup line; per-request
        access logging would just be noise in an interactive terminal. Set
        ``LEVAIN_SERVE_VERBOSE`` to restore the stdlib access log on stderr."""
        import os

        if os.environ.get("LEVAIN_SERVE_VERBOSE"):
            super().log_message(fmt, *args)


def make_server(
    paths: AnnealPaths, *, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT
) -> _LevainHTTPServer:
    """Build a configured, bound (but not-yet-serving) web server over a substrate.

    The static assets are cached on the instance once here (they never change at
    runtime); a missing asset is a packaging bug and surfaces loud. Raises
    ``OSError`` if the bind fails (e.g. the port is in use) — the caller decides
    how to report it. Separated from ``run_web_server`` so tests can drive a real
    bound server without the resolve/existence/print/browser/serve_forever wrapper.

    Raises ``ValueError`` (before binding) if ``host`` is not a loopback address —
    Slice-1 ``serve`` is unauthenticated + read-only and must not be remotely
    reachable."""
    if not _is_loopback_host(host):
        raise ValueError(
            f"refusing to bind {host!r}: Slice-1 `levain serve` is loopback-only "
            "(127.0.0.1 / localhost). Off-loopback exposure waits for the Slice-2 "
            "write/auth boundary — an unauthenticated read surface must not be "
            "remotely reachable."
        )
    assets = {fn: load_web_asset(fn).encode("utf-8") for fn, _ in _ASSETS.values()}
    httpd = _LevainHTTPServer((host, port), _Handler)
    httpd.levain_paths = paths
    httpd.levain_assets = assets
    # Allow the loopback names + the exact bound address (covers a 127.0.0.x bind),
    # normalized lowercase to match the Host check; any other Host is refused.
    httpd.allowed_hosts = _LOOPBACK_HOSTS | {str(httpd.server_address[0]).lower()}
    return httpd


def run_web_server(
    path: Path,
    *,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    open_browser: bool = True,
) -> int:
    """``levain serve`` entry point — serve the substrate dashboard on localhost.

    Returns nonzero only if the store is unreachable before the server starts, or
    the bind fails (e.g. the port is in use) — mirroring ``levain dashboard`` /
    ``serve-app``. A degraded sub-tier renders visibly and is not a startup
    failure. Blocks in ``serve_forever`` until interrupted (Ctrl+C → clean exit 0).
    """
    paths = _resolve_store(path)
    if not paths.episodic_db.exists():
        print(
            f"No anneal store at {paths.episodic_db}.\n"
            "Run `levain init` in this directory, or pass --path to an install.",
            file=sys.stderr,
        )
        return 1

    try:
        httpd = make_server(paths, host=host, port=port)
    except ValueError as exc:  # non-loopback bind refused (the Slice-1 boundary)
        print(str(exc), file=sys.stderr)
        return 1
    except OSError as exc:
        print(
            f"Could not bind {host}:{port} — {exc}.\n"
            "The port may be in use; try `levain serve --port <N>`.",
            file=sys.stderr,
        )
        return 1

    bound_host, bound_port = httpd.server_address[0], httpd.server_address[1]
    # Bracket an IPv6 literal so the URL is RFC-3986 valid (defensive — Slice-1
    # binds IPv4 loopback, but keep the printed/opened URL correct regardless).
    host_for_url = f"[{bound_host}]" if ":" in str(bound_host) else bound_host
    url = f"http://{host_for_url}:{bound_port}/"
    print(f"Levain substrate → {url}")
    print(f"  store: {paths.episodic_db}")
    print("  read-only · localhost-only · Ctrl+C to stop")

    if open_browser:
        # The listening socket is already bound (ThreadingHTTPServer binds in
        # __init__), so the browser's connection queues until serve_forever
        # accepts it — opening before the blocking call is correct.
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

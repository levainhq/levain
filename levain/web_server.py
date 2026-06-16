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

Properties carried straight from the data layer:
- **reads stay read-only** — every GET builds a fresh ``SubstrateView`` with the
  store opened ``read_only=True``; the read path acts on nothing.
- **writes are governed (Slice 2a)** — exactly ONE write route, ``POST /edit``,
  behind the write/auth boundary below. It edits ONLY Class-A operator inputs
  (``world.md`` sections, ``posture``/``recency`` thinking-style, the entity name)
  via ``levain.writes`` — never the consolidated cognition (scope §1). Every write
  is backed up + audited + reversible; the consolidate stays the felt layer's
  single writer.
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
  allowlist and anything else is a 403. The same check now also fronts the write
  route — it IS the Slice-2a write/auth boundary it was the seed of.
- **no-token localhost-sovereign write/auth (Slice 2a)** — there is no password/
  token (a startup token is SaaS thinking; principle #6 rejects it at the seat
  layer). The auth for a write is the loopback bind + the Host allowlist + two
  CSRF layers: (1) ``Sec-Fetch-Site`` must be absent (a non-browser client like the
  operator's own curl) or ``same-origin`` (our own dashboard page) — a hostile
  cross-site page's request carries ``cross-site`` and is refused; (2) the body
  must be ``application/json``, which a cross-origin page cannot send without a
  CORS preflight we never satisfy. A malicious LOCAL process could omit the header
  AND set the type — but a process with local code execution can already edit the
  files directly, so that is not a new exposure. Reversibility (backup + audit) is
  the safety net per the doctrine; destructive Class-B writes (Slice 2b) add a
  per-write confirm, Class-A does not.
- **no dependencies, no CDN** — stdlib ``http.server`` + vanilla-JS assets served
  from package data. The dashboard renders with the network cable unplugged. A
  sovereign surface does not rent its client library either.
- **explicit route allowlist** — GET/HEAD serve four read routes (``/`` + two
  scripts + ``/substrate.json``); POST serves exactly one (``/edit``). No
  filesystem mapping, so there is no path-traversal surface. Anything else is a
  flat 404.
- **bounded concurrency** — the per-request store read and every write run under a
  bounded gate; past the cap a request gets a clean 503 rather than letting an
  unbounded thread pile-up exhaust the box.
"""

from __future__ import annotations

import ipaddress
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from levain.dashboard import SubstrateSource, _resolve_source
from levain.writes import MAX_BODY_BYTES, EditError, apply_edit

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

# The POST body is JSON wrapping an edit (its largest field, ``new_body``, is itself
# capped at MAX_BODY_BYTES by the write layer). Cap the raw request a little above
# that for the JSON envelope; anything larger is refused at the transport before we
# read it into memory (413). The write layer re-checks ``new_body`` independently.
_MAX_POST_BYTES = MAX_BODY_BYTES + 64 * 1024

# When refusing an over-cap body, drain up to this much so the client can finish its
# send and read the 413 cleanly on a kept-alive connection; an absurdly larger
# declared body isn't drained (we just close — abuse doesn't earn a tidy goodbye).
_DRAIN_CAP = _MAX_POST_BYTES * 4

# Bounded concurrency for the expensive routes (the per-request store read + writes).
# ThreadingHTTPServer spawns a thread per connection; without a gate a burst could
# pile up unbounded store reads / file writes. Past the cap a request gets a clean
# 503. Static assets (cached bytes) bypass the gate.
_MAX_INFLIGHT = 8

# A write only ever legitimately originates from our own dashboard page (which sends
# ``Sec-Fetch-Site: same-origin``) or a non-browser client that sends NO Sec-Fetch-
# Site header at all (the operator's own curl/script — sovereign; absent reads as
# Python ``None``). Any present value other than ``same-origin`` — ``cross-site``,
# ``same-site``, or even ``none`` (a top-level navigation, which can't carry a JSON
# POST anyway) — is an unexpected/hostile origin and is refused.
_WRITE_SEC_FETCH_ALLOWED = "same-origin"

# The page only ever loads its own same-origin scripts + stylesheet and fetches its
# own JSON. Lock everything else off. Slice 2a moved the one inline <style> block out
# to a served `/dashboard.css`, so `style-src` is now `'self'` — no `'unsafe-inline'`
# (the Slice-2 tightening the prior comment flagged). `frame-ancestors 'none'` denies
# clickjacking / hostile-iframe embedding (paired with X-Frame-Options below).
_CSP = (
    "default-src 'none'; script-src 'self'; style-src 'self'; "
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
    "/dashboard.css": ("dashboard.css", "text/css; charset=utf-8"),
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


def build_substrate_json(source: SubstrateSource) -> bytes:
    """The ``/substrate.json`` body: a fresh read-only ``SubstrateView`` snapshot.

    Pure read — ``source.build()`` opens the episodic Store ``read_only=True`` and
    degrades any unavailable tier into ``view.errors`` rather than failing, so this
    always yields a renderable view (the dashboard shows which tiers are dark
    rather than blanking). Built per request so the browser's refresh reflects the
    live store, including a wrap that landed since the page opened. The
    ``SubstrateSource`` carries the install root, so the served view includes the
    seed/config surface (1.5), not just the anneal store.

    ``writable`` is the transport's write capability — true only when the source has
    an install root. With no install root (a read-only inspection source, e.g. the
    flow self-ops cockpit over a store with no ``.levain`` install) the write route
    ``POST /edit`` 422s server-side; this flag carries that SAME signal to the
    frontend so it wires no ``commit`` and renders no edit affordances at all (NO
    THEATER — one signal drives both the server refusal and the UI suppression)."""
    view = source.build()
    payload = view.to_dict()
    # `writable` is a transport capability, deliberately NOT a SubstrateView field;
    # guard the namespace so a future to_dict() field of the same name can't be
    # silently clobbered (caught in tests, never in prod — complement L3).
    assert "writable" not in payload, "SubstrateView.to_dict() collided with transport `writable`"
    payload["writable"] = source.install_root is not None
    return json.dumps(payload).encode("utf-8")


class _LevainHTTPServer(ThreadingHTTPServer):
    """A ``ThreadingHTTPServer`` carrying the substrate source, the cached static
    assets, and the Host-header allowlist. A typed subclass (rather than ad-hoc
    attributes on a plain server) so the handler's access is type-checked and
    can't silently break into an unhandled ``AttributeError`` if construction
    ever moves."""

    levain_source: SubstrateSource
    levain_assets: dict[str, bytes]
    allowed_hosts: frozenset[str]
    request_gate: threading.BoundedSemaphore


class _Handler(BaseHTTPRequestHandler):
    """Serves the four read-only routes (GET + HEAD). Store paths, cached assets,
    and the Host allowlist live on the typed server instance (``self.server``)."""

    # A tidy, modern protocol version (enables keep-alive + proper 1.1 behavior).
    protocol_version = "HTTP/1.1"
    server_version = "levain-serve"
    # Socket timeout (L1 MED): without it BaseHTTPRequestHandler.timeout is None, so a
    # client that declares a Content-Length then stalls (slowloris) holds a server
    # thread forever — the body is read before the rate-gate, so the gate can't help.
    # 30s lets a real localhost request finish while killing a stalled one.
    timeout = 30
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
        # If we're closing the connection (the pre-read 411/413 rejections that don't
        # drain the body), tell the client so it reads the response cleanly instead of
        # logging a reset on the dropped keep-alive socket. [L1 LOW]
        if self.close_connection:
            self.send_header("Connection", "close")
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
            # Bound concurrent store reads (the expensive route); past the cap → 503.
            if not self.server.request_gate.acquire(blocking=False):
                self._send(
                    b"busy\n", "text/plain; charset=utf-8", status=503, head=head
                )
                return
            try:
                body = build_substrate_json(self.server.levain_source)
            except Exception as exc:  # noqa: BLE001 — never 500 on a runtime fault
                # build_substrate_view already degrades data/IO faults into the
                # view; reaching here is an unexpected fault. Surface it as JSON the
                # UI can show, not a dead connection.
                # The capability bit belongs on EVERY renderable payload, not just
                # the happy path: a degraded read that omits `writable` makes the
                # frontend default to writable and render edit affordances a read-only
                # source can't honor (codex L3). Same predicate as build_substrate_json.
                body = json.dumps({
                    "paths": {},
                    "writable": self.server.levain_source.install_root is not None,
                    "errors": {"server": f"{type(exc).__name__}: {exc}"},
                }).encode("utf-8")
            finally:
                self.server.request_gate.release()
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

    def _send_json(self, payload: dict[str, object], status: int = 200) -> None:
        self._send(
            json.dumps(payload).encode("utf-8"),
            "application/json; charset=utf-8",
            status=status,
        )

    def _drain(self, n: int) -> None:
        """Read and discard up to ``n`` bytes of the request body in bounded chunks,
        so a rejected request's body doesn't dangle on a kept-alive connection — the
        client can finish its send and read the error response cleanly."""
        remaining = n
        while remaining > 0:
            chunk = self.rfile.read(min(remaining, 65536))
            if not chunk:
                break
            remaining -= len(chunk)

    def _reject(self, status: int, error: str, message: str) -> None:
        """Refuse a write BEFORE its body is read: close the connection (so the unread
        body can't desync a kept-alive socket) and send the error JSON."""
        self.close_connection = True
        self._send_json({"error": error, "message": message}, status)

    def do_POST(self) -> None:  # noqa: N802 — BaseHTTPRequestHandler contract
        """The Slice-2a write route — ``POST /edit``, behind the write/auth boundary.

        The cheap fail-closed checks (Host → CSRF → Content-Type → route →
        Content-Length) run BEFORE any body read and each closes the connection on
        rejection — so a wrong-Host / wrong-route client never reads a body and can't
        stall a thread there [L3 MED], and no unread body dangles on a kept-alive
        socket. The rate-gate is acquired BEFORE the body read, so the bounded
        read+drain+write phase is itself concurrency-bounded; a slowloris is capped by
        the gate + the 30s socket timeout. The write layer does the actual edit."""
        # Same Host allowlist as reads — closes DNS-rebinding for the write route too.
        if not self._host_ok():
            return self._reject(403, "forbidden", "bad Host header")
        # CSRF layer 1: a write must come from our own page (same-origin) or a
        # non-browser client (no Sec-Fetch-Site at all). Anything else is refused.
        sfs = self.headers.get("Sec-Fetch-Site")
        if sfs is not None and sfs != _WRITE_SEC_FETCH_ALLOWED:
            return self._reject(403, "forbidden", "cross-origin write refused")
        # CSRF layer 2: require application/json (a cross-origin page cannot send it
        # without a CORS preflight this server never answers).
        ctype = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        if ctype != "application/json":
            return self._reject(
                415, "unsupported_media_type", "Content-Type must be application/json"
            )
        # Exactly one write route.
        if self.path.split("?", 1)[0] != "/edit":
            return self._reject(404, "not_found", "no such route")
        # Content-Length: required + numeric (checked before reading a byte).
        clen_raw = self.headers.get("Content-Length")
        if clen_raw is None or not clen_raw.isdigit():
            return self._reject(411, "length_required", "Content-Length required")
        clen = int(clen_raw)

        # Acquire the rate-gate BEFORE any body read/drain so the read+write phase is
        # concurrency-bounded — a stalled body can't pile up unbounded threads. [L3 MED]
        if not self.server.request_gate.acquire(blocking=False):
            return self._reject(503, "busy", "server busy")
        result: dict[str, object] | None = None
        try:
            if clen > _MAX_POST_BYTES:
                # Drain a bounded oversize body so the client reads the 413 cleanly;
                # an absurdly large declared body just closes without draining.
                if clen <= _DRAIN_CAP:
                    self._drain(clen)
                else:
                    self.close_connection = True
                self._send_json(
                    {"error": "too_large",
                     "message": f"body exceeds {_MAX_POST_BYTES} bytes"}, 413
                )
                return
            try:
                raw = self.rfile.read(clen)
            except OSError:  # slow/stalled client hit the socket timeout → drop it
                self.close_connection = True
                return
            try:
                req = json.loads(raw.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                self._send_json(
                    {"error": "bad_json", "message": "body is not valid JSON"}, 400
                )
                return
            install_root = self.server.levain_source.install_root
            if install_root is None:
                self._send_json(
                    {"error": "no_install",
                     "message": "no install root; nothing editable"}, 422
                )
                return
            try:
                result = apply_edit(install_root, req)
            except EditError as exc:
                self._send_json({"error": exc.code, "message": str(exc)}, exc.http_status)
            except Exception as exc:  # noqa: BLE001 — never leak a traceback to the client
                self._send_json({"error": "internal", "message": type(exc).__name__}, 500)
        finally:
            self.server.request_gate.release()
        if result is not None:
            self._send_json(result, 200)

    def log_message(self, fmt: str, *args: object) -> None:
        """Quiet by default — the server prints its own startup line; per-request
        access logging would just be noise in an interactive terminal. Set
        ``LEVAIN_SERVE_VERBOSE`` to restore the stdlib access log on stderr."""
        import os

        if os.environ.get("LEVAIN_SERVE_VERBOSE"):
            super().log_message(fmt, *args)


def make_server(
    source: SubstrateSource, *, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT
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
    httpd.levain_source = source
    httpd.levain_assets = assets
    # Allow the loopback names + the exact bound address (covers a 127.0.0.x bind),
    # normalized lowercase to match the Host check; any other Host is refused.
    httpd.allowed_hosts = _LOOPBACK_HOSTS | {str(httpd.server_address[0]).lower()}
    # Bound the expensive routes (store read + writes); past the cap → 503.
    httpd.request_gate = threading.BoundedSemaphore(_MAX_INFLIGHT)
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
    source = _resolve_source(path)
    if not source.anneal.episodic_db.exists():
        print(
            f"No anneal store at {source.anneal.episodic_db}.\n"
            "Run `levain init` in this directory, or pass --path to an install.",
            file=sys.stderr,
        )
        return 1

    try:
        httpd = make_server(source, host=host, port=port)
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

    # str-coerce the bound address (socket address types are loosely str|bytes;
    # AF_INET yields a str, but coerce so the f-strings below can't ever render a
    # b'...' repr — also clears the pre-existing mypy str-bytes-safe finding here).
    bound_host, bound_port = str(httpd.server_address[0]), httpd.server_address[1]
    # Bracket an IPv6 literal so the URL is RFC-3986 valid (defensive — Slice-1
    # binds IPv4 loopback, but keep the printed/opened URL correct regardless).
    host_for_url = f"[{bound_host}]" if ":" in bound_host else bound_host
    url = f"http://{host_for_url}:{bound_port}/"
    print(f"Levain substrate → {url}")
    print(f"  store: {source.anneal.episodic_db}")
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

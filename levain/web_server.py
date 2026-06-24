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
- **explicit route allowlist** — GET/HEAD serve the built-in read routes (``/`` +
  the css + two scripts + ``/substrate.json`` + the read-only ``/recall.json`` episode
  keyword search) PLUS any read-only routes a downstream registers via
  ``make_server(extra_assets=..., extra_json=...)`` (the FleetView extension point — a
  collision with a reserved built-in route is refused at registration); POST serves
  exactly one (``/edit``), and no extra route can reach it. No filesystem mapping, so
  there is no path-traversal surface. Anything else is a flat 404.
- **bounded concurrency** — the per-request store read and every write run under a
  bounded gate; past the cap a request gets a clean 503 rather than letting an
  unbounded thread pile-up exhaust the box.
"""

from __future__ import annotations

import ipaddress
import json
import sys
import threading
from collections.abc import Callable, Mapping
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from levain.dashboard import SubstrateSource, _resolve_source, recall_episode_rows
from levain.writes import MAX_BODY_BYTES, EditError, apply_edit

__all__ = [
    "DEFAULT_HOST",
    "DEFAULT_PORT",
    "build_recall_json",
    "build_substrate_json",
    "load_web_asset",
    "make_server",
    "run_web_server",
]

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 7420

# Cap on episode keyword-search results (spore-107). The recall route is a bounded
# read like /substrate.json; a fixed ceiling keeps a broad keyword from returning an
# unbounded payload. 100 matches is plenty for a triage glance — narrow for more.
_RECALL_LIMIT = 100

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


def _canonical_bind_ip(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """The canonical IP for a bind host, or None if it isn't one we accept.

    SECURITY-CRITICAL canonicalization: the socket layer (libc getaddrinfo) accepts
    legacy numeric IPv4 forms that Python's strict ``ipaddress`` REJECTS — ``0``,
    ``134744072``, ``0x08080808``, ``010.010.010.010`` all bind as 0.0.0.0 / 8.8.8.8
    / 10.10.10.10. Validating with ``ipaddress`` but BINDING the raw string would let
    those slip past the wildcard/public guard (codex L3). So we accept ONLY
    ``localhost`` or a canonical IP literal; a hostname or legacy-numeric form is
    refused. IPv4-mapped IPv6 (``::ffff:x.x.x.x``) is unwrapped so the EMBEDDED
    address is classified, not the mapping wrapper (complement L3)."""
    h = host.strip().rstrip(".").lower()
    if h == "localhost":
        return ipaddress.ip_address("127.0.0.1")
    try:
        ip = ipaddress.ip_address(h)
    except ValueError:
        return None  # a hostname or legacy-numeric form, not a canonical literal
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        return ip.ipv4_mapped
    return ip


def _rejected_bind_host(host: str) -> str | None:
    """Reason a host must NEVER be bound (for ANY source), or None if acceptable.

    A non-loopback bind is meant for ONE specific, non-public interface — a LAN IP
    or a Tailscale CGNAT address (RFC 6598, 100.64/10). Refused for ANY source:
    - a non-canonical / hostname bind host (see ``_canonical_bind_ip`` — closes the
      libc-vs-``ipaddress`` parser-differential bypass);
    - a WILDCARD (``0.0.0.0`` / ``::``) — binds EVERY interface, not just the mesh;
    - a GLOBAL/public IP — would expose the substrate to the internet."""
    ip = _canonical_bind_ip(host)
    if ip is None:
        return ("a hostname or non-canonical numeric address is not allowed; pass a "
                "canonical IP literal (e.g. your Tailscale IP) or 'localhost'")
    if ip.is_unspecified:
        return "a wildcard address (0.0.0.0 / ::) exposes EVERY interface, not just the private mesh"
    if ip.is_global:
        return "a public/global IP would expose the substrate to the internet"
    return None

# The static assets, by request path → (package-data filename, content-type).
# An explicit allowlist: there is no path → filesystem mapping anywhere, so a
# crafted path cannot escape this set.
_ASSETS: dict[str, tuple[str, str]] = {
    "/": ("dashboard.html", "text/html; charset=utf-8"),
    "/dashboard.css": ("dashboard.css", "text/css; charset=utf-8"),
    "/dashboard_core.js": ("dashboard_core.js", "text/javascript; charset=utf-8"),
    "/dashboard_boot.js": ("dashboard_boot.js", "text/javascript; charset=utf-8"),
}


# Paths a downstream-registered extra route may NEVER shadow: the built-in static
# assets + the two dynamic read routes + the one write route. A control plane
# (the flow Bridge's FleetView) registers ADDITIONAL read-only views; it must not
# be able to override the substrate dashboard, the JSON reads, or the governed write
# route. make_server refuses a collision loudly (a packaging-class bug, not runtime).
_RESERVED_PATHS: frozenset[str] = frozenset(_ASSETS) | {
    "/substrate.json",
    "/recall.json",
    "/edit",
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


def build_substrate_json(
    source: SubstrateSource,
    extra_panels: "Callable[[], list[dict]] | None" = None,
) -> bytes:
    """The ``/substrate.json`` body: a fresh read-only ``SubstrateView`` snapshot.

    Pure read — ``source.build()`` opens the episodic Store ``read_only=True`` and
    degrades any unavailable tier into ``view.errors`` rather than failing, so this
    always yields a renderable view (the dashboard shows which tiers are dark
    rather than blanking). Built per request so the browser's refresh reflects the
    live store, including a wrap that landed since the page opened. The
    ``SubstrateSource`` carries the install root, so the served view includes the
    seed/config surface (1.5), not just the anneal store.

    ``writable`` is the transport's write capability — true only when the source carries
    a ``write_scope`` (the governed write surface). A read-only inspection source (no
    write_scope — e.g. the flow self-ops cockpit served read-only, or any source built
    without one) makes the write route ``POST /edit`` 422 server-side; this flag carries
    that SAME signal to the frontend so it wires no ``commit`` and renders no edit
    affordances at all (NO THEATER — one signal drives both the server refusal and the
    UI suppression)."""
    view = source.build()
    payload = view.to_dict()
    # `writable` is a transport capability, deliberately NOT a SubstrateView field;
    # guard the namespace so a future to_dict() field of the same name can't be
    # silently clobbered (caught in tests, never in prod — complement L3).
    assert "writable" not in payload, "SubstrateView.to_dict() collided with transport `writable`"
    payload["writable"] = source.write_scope is not None
    # Inline external panels (the read-only extra-PANEL seam): a per-request provider returns
    # full panel descriptors+data; split each into a layout entry (kind:"external") + its data
    # under payload["extra_panels"][id], so the frontend renders them inline among the substrate
    # panels. The provider is a DOWNSTREAM read (e.g. the Bridge's inbox) — gated like this whole
    # route. FAIL-SOFT: a provider fault degrades to no extra panels, never breaks the cockpit.
    if extra_panels is not None:
        assert "extra_panels" not in payload, "to_dict() collided with transport `extra_panels`"
        try:
            panels = extra_panels()
            # the provider CONTRACT is a list/tuple of dicts; coerce anything else (None, an
            # int, a generator) to empty so a malformed return degrades to no panels rather
            # than raising out of the otherwise-unguarded iteration below [codex L3 MED].
            if not isinstance(panels, (list, tuple)):
                panels = []
        except Exception:  # noqa: BLE001 — a downstream provider fault must not break the dashboard
            panels = []
        data: dict[str, dict] = {}
        layout = payload.get("layout")
        seen_ids: set[str] = set()
        for p in panels:
            if not isinstance(p, dict):
                continue
            pid = str(p.get("id") or "")  # explicit None/"" → missing (not the literal "None") [codex L3 LOW]
            if not pid or pid in seen_ids:
                continue  # id required + UNIQUE — a dup renders the last's data under both [codex L3 MED]
            seen_ids.add(pid)
            zone = str(p.get("zone", "operate"))
            if zone not in ("identity", "operate", "mind"):
                zone = "operate"  # unknown zones have no tab + would orphan a divider [codex L3 LOW]
            entry = {"kind": "external", "id": pid, "zone": zone,
                     "title": str(p.get("title", pid))}
            if isinstance(layout, list):
                # Insert AFTER the last panel of the SAME zone, not at the end: the base layout
                # is zone-contiguous (Identity→Operate→Mind) and the frontend drops a zone
                # divider on each zone change, so appending an operate panel past the Mind block
                # would re-emit an orphaned 2nd "Operate" header in the all-view (L1 LOW-1).
                # Grouping it into its zone's run preserves the contiguity the divider relies on.
                insert_at = len(layout)
                for i in range(len(layout) - 1, -1, -1):
                    if isinstance(layout[i], dict) and layout[i].get("zone") == zone:
                        insert_at = i + 1
                        break
                layout.insert(insert_at, entry)
                # record the data ONLY when the layout entry actually landed — so a missing /
                # non-list layout (a future to_dict() regression) can't leave orphan panel data
                # that exists in the payload but never renders (complement L3 MED).
                data[pid] = {
                    "note": p.get("note", ""),
                    "lines": p.get("lines", []),
                    "empty": p.get("empty", ""),
                    "error": p.get("error"),
                }
        payload["extra_panels"] = data
    return json.dumps(payload).encode("utf-8")


def build_recall_json(source: SubstrateSource, keyword: str) -> bytes:
    """The ``/recall.json`` body: a read-only keyword search over the episodic store.

    Same read discipline as ``build_substrate_json`` — a fresh ``read_only=True`` open
    per request, here via ``recall_episode_rows`` (the data layer) — and the matched
    episodes carry the SAME row shape as the recent-episodes panel, so the surface
    renders search hits with identical markup. The result count is pinned to
    ``_RECALL_LIMIT`` HERE (not a caller parameter — the payload bound is the route's, not
    a caller's to widen, L3 complement). The route is read-only BY CONSTRUCTION:
    ``recall_episode_rows`` opens the store read-only and there is no write path regardless
    of the source's ``write_scope`` — so unlike ``/substrate.json`` this body carries no
    ``writable`` capability bit (nothing to write).

    On a store/data fault the body carries the error and DROPS any partial rows: a partial
    search result reads as 'all the matches', which is misleading — so error WINS (the box
    shows 'search unavailable'), never partial-rows-plus-error (L3 complement MED-1). It
    never 500s."""
    rows, error = recall_episode_rows(
        source.anneal.episodic_db, keyword=keyword, limit=_RECALL_LIMIT
    )
    if error is not None:
        return json.dumps(
            {"keyword": keyword, "episodes": [], "count": 0, "errors": {"episodes": error}}
        ).encode("utf-8")
    return json.dumps(
        {"keyword": keyword, "episodes": [r.to_dict() for r in rows], "count": len(rows)}
    ).encode("utf-8")


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
    # Downstream-registered READ-ONLY routes (the FleetView extension point). Both
    # default to empty so the base product carries nothing extra. extra_assets are
    # cached static bytes (ungated, like levain_assets); extra_json are per-request
    # builders served under the concurrency gate (like /substrate.json).
    extra_assets: dict[str, tuple[str, bytes]]
    extra_json: dict[str, Callable[[], bytes]]
    # Downstream-injected READ-ONLY inline PANELS (the panel peer of the extra-route seam): a
    # per-request provider returning panel descriptors+data that render INLINE in the dashboard
    # alongside the substrate panels (vs extra_json, which serves a SEPARATE page). None → the
    # base product injects nothing. Generic by design (a `kind:"external"` panel of titled
    # lines) so the kernel stays domain-agnostic — the flow Bridge injects its inbox here.
    extra_panels: "Callable[[], list[dict]] | None"


class _Handler(BaseHTTPRequestHandler):
    """Serves the five read-only routes (GET + HEAD). Store paths, cached assets,
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
                body = build_substrate_json(
                    self.server.levain_source, self.server.extra_panels)
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
                    "writable": self.server.levain_source.write_scope is not None,
                    "errors": {"server": f"{type(exc).__name__}: {exc}"},
                }).encode("utf-8")
            finally:
                self.server.request_gate.release()
            self._send(body, "application/json; charset=utf-8", head=head)
            return

        if path == "/recall.json":
            # Read-only keyword search over episodes (spore-107). Bounded like
            # /substrate.json — the store read is the expensive part; past the cap → 503.
            if not self.server.request_gate.acquire(blocking=False):
                self._send(
                    b"busy\n", "text/plain; charset=utf-8", status=503, head=head
                )
                return
            try:
                from urllib.parse import parse_qs, urlsplit

                keyword = parse_qs(urlsplit(self.path).query).get("keyword", [""])[0].strip()
                body = build_recall_json(self.server.levain_source, keyword)
            except Exception as exc:  # noqa: BLE001 — never 500 on a runtime fault
                body = json.dumps({
                    "keyword": "",
                    "episodes": [],
                    "count": 0,
                    "errors": {"server": f"{type(exc).__name__}: {exc}"},
                }).encode("utf-8")
            finally:
                self.server.request_gate.release()
            self._send(body, "application/json; charset=utf-8", head=head)
            return

        # Downstream-registered dynamic JSON routes (the FleetView extension point):
        # read-only, gated EXACTLY like /substrate.json so a control plane's extra views
        # ride this same security envelope (Host allowlist, CSP, concurrency gate, the
        # bind-refusal contract) instead of standing up a second server that would
        # duplicate — and could drift from — that contract. GET/HEAD only; there is no
        # POST path to them, so an extra route can never become a write surface.
        json_builder = self.server.extra_json.get(path)
        if json_builder is not None:
            if not self.server.request_gate.acquire(blocking=False):
                self._send(b"busy\n", "text/plain; charset=utf-8", status=503, head=head)
                return
            try:
                body = json_builder()
                if not isinstance(body, bytes):
                    # The route contract is bytes. A non-bytes return would let _send emit
                    # a Content-Length header and then TypeError on wfile.write AFTER the
                    # headers are already on the wire (broken framing) — catch it HERE so it
                    # degrades to a JSON error like any other builder fault (codex L3).
                    raise TypeError(
                        f"extra_json builder for {path} must return bytes, "
                        f"got {type(body).__name__}"
                    )
            except Exception as exc:  # noqa: BLE001 — never 500 on a runtime fault
                # Same discipline as /substrate.json: surface the fault as JSON the
                # registering surface can render, never a dead connection.
                body = json.dumps(
                    {"errors": {"server": f"{type(exc).__name__}: {exc}"}}
                ).encode("utf-8")
            finally:
                self.server.request_gate.release()
            self._send(body, "application/json; charset=utf-8", head=head)
            return

        asset = _ASSETS.get(path)
        if asset is not None:
            filename, content_type = asset
            self._send(self.server.levain_assets[filename], content_type, head=head)
            return

        # Downstream-registered static assets (cached bytes, ungated — same class as the
        # built-in assets above). Checked after the built-ins so it can never shadow them
        # (make_server also refuses a reserved-path collision at registration).
        extra = self.server.extra_assets.get(path)
        if extra is not None:
            content_type, body = extra
            self._send(body, content_type, head=head)
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
            scope = self.server.levain_source.write_scope
            if scope is None:
                # Distinct from writes.py's `no_install` (a writable source whose seed/
                # config kinds need an install): this is the WHOLE source being read-only.
                self._send_json(
                    {"error": "read_only",
                     "message": "this source is read-only; nothing editable"}, 422
                )
                return
            try:
                result = apply_edit(scope, req)
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
    source: SubstrateSource,
    *,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    extra_assets: Mapping[str, tuple[str, bytes]] | None = None,
    extra_json: Mapping[str, Callable[[], bytes]] | None = None,
    extra_panels: "Callable[[], list[dict]] | None" = None,
) -> _LevainHTTPServer:
    """Build a configured, bound (but not-yet-serving) web server over a substrate.

    The static assets are cached on the instance once here (they never change at
    runtime); a missing asset is a packaging bug and surfaces loud. Raises
    ``OSError`` if the bind fails (e.g. the port is in use) — the caller decides
    how to report it. Separated from ``run_web_server`` so tests can drive a real
    bound server without the resolve/existence/print/browser/serve_forever wrapper.

    ``extra_assets`` / ``extra_json`` are the READ-ONLY extension point a downstream
    control plane uses to register additional GET views that ride this server's
    security envelope (the Host allowlist, CSP, the concurrency gate, the bind-refusal
    contract) — the flow Bridge's FleetView registers its ``/fleet`` landing (static)
    + ``/fleet.json`` (live) here rather than standing up a second server that would
    re-implement (and could drift from) the bind-refusal contract
    (``structural_invariants_beat_discipline``). ``extra_assets`` maps a path →
    ``(content_type, body_bytes)`` (cached, ungated — like the built-in assets);
    ``extra_json`` maps a path → a zero-arg builder called per request under the gate
    (like ``/substrate.json``), its body served as ``application/json``. Both are
    GET/HEAD-only: Levain dispatches them ONLY from the read path and has no POST handler
    for an extra route, so the kernel cannot be made to add a WRITE ROUTE through this
    seam. (The registered builders themselves must be side-effect-free — that is the
    REGISTRANT's contract, which the kernel cannot enforce on an arbitrary ``Callable``;
    the Bridge's fleet builders are pure reads.) A path that collides with a
    reserved built-in route (the dashboard assets / the JSON reads / ``/edit``) or that
    appears in BOTH mappings raises ``ValueError`` (a registration-time packaging bug,
    surfaced loud — never a silent shadow of the dashboard or the write route).

    Non-loopback binding (a LAN / Tailscale IP) is allowed ONLY for a READ-ONLY,
    NO-INSTALL source (``write_scope is None`` AND ``install_root is None`` — e.g. flow's
    self-ops cockpit over a bare anneal store): the write route 422s (no unauthenticated
    WRITE surface) and there is no seed/config (operator-profile) data to expose, so the
    private mesh (e.g. Tailscale's WireGuard) is the access boundary. A WRITABLE source
    (its no-token localhost-sovereign write auth assumes loopback) OR an install-bearing
    source (its seed/config is operator-private — kept local, matching the pre-WriteScope
    boundary that gated on install presence) stays loopback-only. Raises ``ValueError``
    (before binding) on a disallowed non-loopback bind. [codex L3 MED]"""
    # Wildcard / public binds are refused for ANY source — a non-loopback bind is for
    # ONE specific private/mesh interface, never every interface or the internet.
    reason = _rejected_bind_host(host)
    if reason:
        raise ValueError(
            f"refusing to bind {host!r}: {reason}. Pass a SPECIFIC private interface "
            "IP (e.g. your Tailscale IP), not a wildcard or a public address."
        )
    if not _is_loopback_host(host) and (
        source.write_scope is not None or source.install_root is not None
    ):
        raise ValueError(
            f"refusing to bind {host!r}: a WRITABLE or install-bearing substrate is "
            "loopback-only (127.0.0.1 / localhost). A writable source's no-token "
            "localhost-sovereign write boundary (POST /edit) assumes loopback; an "
            "install-bearing source's seed/config is operator-private. Only a READ-ONLY, "
            "NO-INSTALL source (no write_scope, no install_root) MAY bind a non-loopback "
            "address — e.g. a Tailscale IP — for private-mesh access."
        )
    # Validate downstream routes BEFORE binding a socket — a collision is a packaging
    # bug, caught at registration, never a silent shadow at request time.
    extra_assets = dict(extra_assets or {})
    extra_json = dict(extra_json or {})
    _reserved_lower = {p.lower() for p in _RESERVED_PATHS}
    for path in (*extra_assets, *extra_json):
        if not isinstance(path, str) or not path.startswith("/"):
            raise ValueError(
                f"refusing extra route {path!r}: must be an absolute path (start with '/')."
            )
        # CANONICAL-FORM safe, not merely exact-string safe (codex L3): the dispatcher
        # routes on the raw exact path, so a percent-encoded / dot-segment / double-slash /
        # backslash path is harmless TODAY — but this is now a public kernel seam, so reject
        # any non-canonical form up front rather than let a future normalization layer turn
        # a harmless near-collision into route ambiguity.
        if any(c in path for c in "?#%\\;"):
            raise ValueError(
                f"refusing extra route {path!r}: no query/fragment, percent-encoding, "
                "backslash, or ';' params — pass a bare canonical path."
            )
        # Reject any empty path segment after the leading slash — i.e. a double slash OR a
        # trailing slash (`/edit/` is the same future-normalization-ambiguity class the guard
        # closes; codex L3). `path.split('/')[1:]` drops the always-empty leading element.
        if any(seg == "" for seg in path.split("/")[1:]):
            raise ValueError(
                f"refusing extra route {path!r}: no empty path segment "
                "(double slash or trailing slash)."
            )
        if any(seg in (".", "..") for seg in path.split("/")):
            raise ValueError(
                f"refusing extra route {path!r}: no '.'/'..' path segments."
            )
        # Case-INSENSITIVE reserved check: /EDIT must be refused as a near-collision even
        # though exact-string routing would treat it as a distinct path — defense against a
        # future case-folding normalization.
        if path.lower() in _reserved_lower:
            raise ValueError(
                f"refusing extra route {path!r}: it collides with a built-in route "
                "(a dashboard asset, the JSON reads, or /edit) and may not be overridden."
            )
    dup = set(extra_assets) & set(extra_json)
    if dup:
        raise ValueError(
            f"refusing extra routes {sorted(dup)!r}: registered as BOTH a static asset "
            "and a dynamic JSON route — a path resolves to exactly one handler."
        )
    # Validate the static-asset VALUE shape at registration (a (content_type, body) pair
    # of the right types) so a malformed asset surfaces here, not as a 500 / broken framing
    # at request time. The dynamic extra_json builders are validated to return bytes at the
    # call site (inside the request handler's guarded block).
    for path, value in extra_assets.items():
        if (not isinstance(value, tuple) or len(value) != 2
                or not isinstance(value[0], str) or not isinstance(value[1], bytes)):
            raise ValueError(
                f"refusing extra asset {path!r}: value must be "
                "(content_type: str, body: bytes)."
            )
        # The content_type flows into send_header("Content-Type", ...) — reject CR/LF so a
        # bad registrant can't inject response headers through the seam (codex L3 defense-in-
        # depth; FleetView's values are safe constants, but this is a public kernel seam).
        if "\r" in value[0] or "\n" in value[0]:
            raise ValueError(
                f"refusing extra asset {path!r}: content_type must not contain CR/LF."
            )

    # Inline external-panel provider (read-only, per-request, fail-soft); None for the base
    # product. A non-callable is a registrant bug — reject it BEFORE binding a socket, with the
    # rest of the pre-bind registration validation (codex L3: reject registration bugs first).
    if extra_panels is not None and not callable(extra_panels):
        raise ValueError("extra_panels must be a zero-arg callable returning a list of panels.")

    assets = {fn: load_web_asset(fn).encode("utf-8") for fn, _ in _ASSETS.values()}
    httpd = _LevainHTTPServer((host, port), _Handler)
    httpd.levain_source = source
    httpd.levain_assets = assets
    httpd.extra_assets = extra_assets
    httpd.extra_json = extra_json
    httpd.extra_panels = extra_panels
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

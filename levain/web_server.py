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
- **writes are governed** — TWO write routes, ``POST /edit`` + the governed
  ``POST /action`` ops-verb seam, both behind the write/auth boundary below.
  ``/edit`` mutates Class-A operator inputs (``world.md`` sections, ``posture``/
  ``recency``, the entity name), the ``## State`` section, the spore lifecycle
  (touch/descend/ascend/seed/disposition/surface_at), and episode tombstones via
  ``levain.writes`` — never the consolidated FELT layer (the five felt sections;
  the consolidate stays their single writer). Every write is backed up + audited +
  reversible. The write surface is served ONLY when the source carries a
  ``write_scope`` — ``levain serve --write`` (the default ``serve`` strips it →
  read-only); a writable source needs loopback or, off-box, a write token.
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
  exactly two GOVERNED write routes (``/edit`` + ``/action``, the ops-verb seam), and
  no downstream extra route can add, shadow, or reach a POST handler. No filesystem
  mapping, so there is no path-traversal surface. Anything else is a flat 404.
- **bounded concurrency** — the per-request store read and every write run under a
  bounded gate; past the cap a request gets a clean 503 rather than letting an
  unbounded thread pile-up exhaust the box.
"""

from __future__ import annotations

import dataclasses
import hmac
import ipaddress
import json
import sys
import threading
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from levain.dashboard import SubstrateSource, _resolve_source, recall_episode_rows
from levain.jobs import JobRuntime, JobStore, JobStoreCorruptError
from levain.writes import (
    MAX_BODY_BYTES,
    ActionVerb,
    EditError,
    apply_action,
    apply_edit,
)

__all__ = [
    "DEFAULT_HOST",
    "DEFAULT_PORT",
    "build_job_json",
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

# The OFF-BOX write governance factor (spore-129). The no-token rule above is for the
# LOCALHOST-SOVEREIGN seat: when the surface is bound to loopback, the bind IS the auth and
# a token there is theater (principle #6). But the MOMENT a writable surface binds an
# off-loopback (private-mesh / Tailscale) address, loopback-is-auth no longer holds — so a
# write MUST then carry a shared token the device holds, in this header, constant-time
# compared against the token ``make_server`` was given. This is NOT a seat-layer password;
# it is the factor that replaces loopback when the write surface leaves the machine.
# Loopback binds stay token-free (the check below is skipped for a loopback-bound server).
_WRITE_TOKEN_HEADER = "X-Levain-Write-Token"

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


def host_header_allowed(
    raw_host: str | None, allowed_hosts: "frozenset[str] | set[str]"
) -> bool:
    """True iff a request's raw ``Host`` header names one of ``allowed_hosts``.

    The SECURITY-CRITICAL DNS-rebinding parse, factored out so the dashboard
    server (``_Handler._host_ok``) and the standalone init server
    (``levain.init_server``) share ONE implementation and can never DIVERGE on it
    — a divergence here is a rebinding read-disclosure hole, exactly the class a
    shared structural invariant beats per-surface discipline at. Strict RFC-7230:
    absent Host → refuse (fail-closed); a bracketed IPv6 literal must be
    well-formed (``[host]`` optionally ``:port``); a non-bracket Host with a ``:``
    must carry a clean numeric port; the hostname is normalized (trailing FQDN dot
    dropped, case-folded) before the allowlist compare. So neither ``LOCALHOST``
    false-rejects nor ``[::1]evil`` / a junk ``:`` port sneaks through."""
    if raw_host is None:
        return False  # HTTP/1.1 requires a Host; absent = refuse (fail-closed)
    host = raw_host.strip()
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
    return hostname in allowed_hosts


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


def _is_install_bearing(source: SubstrateSource) -> bool:
    """True if the source carries operator-private seed/config that must stay loopback-only.

    Checks BOTH install_root fields, because they can DIVERGE and the write path keys off the
    WRITE_SCOPE's, not the source's: ``writes._require_install_root`` gates a config/seed/
    entity_name edit on ``scope.install_root`` (= ``source.write_scope.install_root``), while
    the old bind guard only consulted ``source.install_root`` [L1 + codex L3]. So a source with
    ``install_root=None`` but a ``write_scope`` whose ``install_root`` is set would slip past a
    source-only guard and serve operator-private seed WRITES off-box. A write_scope whose
    install_root can't be proven None is treated as install-bearing (fail-closed)."""
    if source.install_root is not None:
        return True
    ws = source.write_scope
    if ws is None:
        return False
    # getattr default True (a non-None sentinel) → a write_scope MISSING the attr is treated as
    # install-bearing (the safe direction), never silently as no-install.
    return getattr(ws, "install_root", True) is not None

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
    "/job.json",
    "/edit",
    "/action",
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


_ACTION_FIELD_KINDS = ("text", "textarea", "csv", "multiselect")
_UNSAFE_FIELD_NAMES = ("__proto__", "prototype", "constructor")


def _valid_multiselect_options(options: object) -> bool:
    """A multiselect's ``options`` must be a non-empty list whose entries are each a non-empty
    (post-trim) string OR a {value, label} dict with a non-empty string value + (if present) a
    string label, with NO duplicate values. Validated per-FIELD (not just per-entry) so a malformed
    chooser — whitespace-only value, non-string label, dup values rendering dup checkboxes — is
    rejected at the seam, not left for the downstream handler to clean (codex L3)."""
    if not isinstance(options, list) or not options:
        return False
    seen: set[str] = set()
    for opt in options:
        value: object
        label: object
        if isinstance(opt, str):
            value, label = opt, None
        elif isinstance(opt, dict):
            value, label = opt.get("value"), opt.get("label")
        else:
            return False
        if not isinstance(value, str) or not value.strip():
            return False
        if label is not None and not isinstance(label, str):
            return False
        if value in seen:
            return False
        seen.add(value)
    return True


def _validated_panel_action(action: object) -> "dict | None":
    """Validate an external panel's optional compose `action` before it reaches the frontend
    (the panel author's contract — defense-in-depth for any adopter). Well-formed = a dict with a
    non-empty string ``verb`` + a ``fields`` list of dicts, each with a UNIQUE, non-empty,
    NON-dunder string ``name`` and a ``kind`` in {text,textarea,csv}. Malformed → ``None`` (the
    compose box just doesn't render; the panel still shows read-only). Rejecting
    ``__proto__``/``prototype``/``constructor`` field names blocks the JS prototype-pollution +
    silent-overwrite class at the source (codex L3); the frontend's null-proto params is the
    matching belt-and-suspenders."""
    if not isinstance(action, dict):
        return None
    verb = action.get("verb")
    if not isinstance(verb, str) or not verb:
        return None
    fields = action.get("fields")
    if not isinstance(fields, list):
        return None
    seen: set[str] = set()
    for f in fields:
        if not isinstance(f, dict):
            return None
        name = f.get("name")
        if (not isinstance(name, str) or not name
                or name in _UNSAFE_FIELD_NAMES or name in seen):
            return None
        kind = f.get("kind")
        if kind not in _ACTION_FIELD_KINDS:
            return None
        if kind == "multiselect" and not _valid_multiselect_options(f.get("options")):
            return None
        seen.add(name)
    return {"verb": verb, "fields": fields}


def build_substrate_json(
    source: SubstrateSource,
    extra_panels: "Callable[[], list[dict]] | None" = None,
    *,
    write_token_required: bool = False,
    extra_verbs: "Mapping[str, ActionVerb] | None" = None,
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
    # OFF-BOX write signal (spore-129): true iff this surface is writable AND bound off-loopback,
    # i.e. the server's POST /edit will REQUIRE the X-Levain-Write-Token header (see do_POST).
    # It tells the frontend to attach the device-held token; on a loopback-bound (or read-only)
    # surface it stays false and the localhost-sovereign token-free path is unchanged. The
    # handler computes the predicate (it owns the bound-address fact); default False here.
    assert "write_token_required" not in payload, "to_dict() collided with transport `write_token_required`"
    payload["write_token_required"] = write_token_required
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
                    # OPTIONAL governed compose/action affordance (the external-panel-ACTION
                    # seam, the write-peer of how spore panels carry verbs): a {verb, fields}
                    # spec the frontend renders as a confirm-gated compose box → POST /action.
                    # Passed through like lines/note (the panel author's contract); the frontend
                    # renders it ONLY when the surface is writable AND the verb is in the
                    # action_verbs registry below — double-gated, NO THEATER. Validated here (verb
                    # + unique non-dunder field names + known kinds) so a malformed adopter action
                    # is dropped to None rather than reaching the frontend (codex L3).
                    "action": _validated_panel_action(p.get("action")),
                }
        payload["extra_panels"] = data
    # Expose the registered action verbs' governance metadata (confirm requirement + label) so a
    # compose affordance sources its confirm step from the verb REGISTRY (the single source of
    # truth) — never a panel-declared flag that could drift from the ActionVerb. Absent when no
    # verbs are registered (a read-only serve passes none), so the frontend renders no compose
    # affordance at all even if a panel still declares an `action` (NO THEATER).
    if extra_verbs:
        assert "action_verbs" not in payload, "to_dict() collided with transport `action_verbs`"
        payload["action_verbs"] = {
            # `idempotent` tells the frontend to mint + send a client `idempotency_key` for this
            # verb (the at-most-once retry token); `job` tells it the POST returns a job HANDLE to
            # POLL (GET /job.json) rather than a final result — both sourced from the registry,
            # never drift-prone.
            name: {"confirm_required": bool(spec.confirm_required),
                   "idempotent": bool(spec.idempotent), "job": bool(spec.job),
                   "label": spec.label}
            for name, spec in extra_verbs.items()
        }
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


def build_job_json(job_runtime: "JobRuntime | None", job_id: str) -> bytes:
    """The ``GET /job.json?id=<job_id>`` body: an async job's current status (the POLL read).

    Returns ``{job_id, status, result?|error?}`` — ``pending``/``running`` (keep polling),
    ``done`` (with the handler ``result``), ``failed`` (with an ``error``), or ``unknown`` (a
    missing / pruned id; the operator re-proposes). A repeatable read — polling N times is the
    SAME result; the at-most-once guarantee is on the job's EXECUTION (the single-winner
    ``pending``→``running`` claim) + on the propose (idempotency), never on this read.

    A server with no job runtime → ``unknown`` (it has no jobs). FAIL-CLOSED on a corrupt store:
    :meth:`JobStore.read_status` raises :class:`JobStoreCorruptError`, which the route maps to a
    500 — NEVER a false ``unknown`` (that would make the operator re-propose a duplicate expensive
    job) nor a false ``done``. The lease backstop (a non-terminal record past its window reads as
    ``failed``) is applied inside ``read_status``."""
    if job_runtime is None:
        return json.dumps({"job_id": job_id, "status": "unknown"}).encode("utf-8")
    now = datetime.now(timezone.utc).isoformat()
    return json.dumps(job_runtime.store.read_status(job_id, now)).encode("utf-8")


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
    # OFF-BOX write auth (spore-129). ``is_loopback_bind`` is computed from the ACTUAL bound
    # address (un-foolable) and gates whether POST /edit requires a token. ``write_token`` is
    # the shared secret an off-loopback writable surface demands; None on a loopback-bound or
    # read-only server (then the token check is skipped — the localhost path stays token-free).
    is_loopback_bind: bool
    write_token: str | None
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
    # Downstream-registered governed ACTION verbs (the write-peer of extra_panels): a
    # name → ActionVerb registry the POST /action route dispatches to. Empty on the base
    # product. Present ONLY on a WRITABLE source (make_server enforces it), so the off-box
    # write-token gate — keyed on write_scope — already governs /action with no second path.
    extra_verbs: dict[str, ActionVerb]
    # The async-job runtime (levain.jobs) for I/O-BOUND (``job=True``) action verbs. None unless a
    # job verb is registered — then make_server auto-creates it (store under the write_scope's
    # ledger_root + a bounded executor) and sweeps orphaned jobs at startup. apply_action routes a
    # job verb's propose through it; GET /job.json polls it.
    job_runtime: "JobRuntime | None"

    def handle_error(self, request: Any, client_address: Any) -> None:
        """Swallow the benign client-disconnect family instead of dumping a traceback.

        ``_Handler.protocol_version = "HTTP/1.1"`` keeps connections alive, so a
        browser pools several and RESETs the idle ones as normal lifecycle; on a mesh
        (``--host``) bind, several devices each bring a pool. Each reset raises a
        ``ConnectionError`` (or a socket ``TimeoutError``) up through ``socketserver``'s
        per-connection thread, whose default ``handle_error`` prints a full traceback —
        flooding the console with noise for connections that were never a server fault
        (the server keeps serving thread-per-connection; only the log is wrong). Filter
        that family to silence; defer any genuine error to the base so real bugs still
        surface (spore-178)."""
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionError, TimeoutError)):
            return
        super().handle_error(request, client_address)


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
        own ``Host``, so a loopback-only allowlist refuses it (403). The strict
        RFC-7230 parse lives in the shared ``host_header_allowed`` so this surface
        and the standalone init server can't diverge on it."""
        return host_header_allowed(self.headers.get("Host"), self.server.allowed_hosts)

    def _write_token_required(self) -> bool:
        """True iff a write to this surface must carry ``X-Levain-Write-Token`` — i.e. the
        surface is writable AND bound off-loopback (spore-129). Mirrors do_POST's gate
        EXACTLY (off-loopback ⟹ token required) so the frontend signal can never disagree
        with the server's actual enforcement. Loopback-bound or read-only → False."""
        return (
            not self.server.is_loopback_bind
            and self.server.levain_source.write_scope is not None
        )

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
                    self.server.levain_source, self.server.extra_panels,
                    write_token_required=self._write_token_required(),
                    extra_verbs=self.server.extra_verbs)
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
                    "write_token_required": self._write_token_required(),
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

        if path == "/job.json":
            # The async-job POLL (the propose→job→POLL seam). A read like /substrate.json
            # (gated, GET/HEAD only — no write path), so it rides this same envelope. Returns the
            # job's status/result; FAILS CLOSED on a corrupt job store (500) so a poll never reports
            # a false `unknown` that would make the operator re-propose a duplicate expensive job.
            if not self.server.request_gate.acquire(blocking=False):
                self._send(b"busy\n", "text/plain; charset=utf-8", status=503, head=head)
                return
            try:
                from urllib.parse import parse_qs, urlsplit

                job_id = parse_qs(urlsplit(self.path).query).get("id", [""])[0].strip()
                if not job_id:
                    body = json.dumps({"error": "bad_request", "message": "id is required"}).encode("utf-8")
                    status_code = 400
                else:
                    body = build_job_json(self.server.job_runtime, job_id)
                    status_code = 200
            except JobStoreCorruptError as exc:
                # fail-closed: the store is untrustworthy → 500, NEVER a false unknown/done.
                body = json.dumps(
                    {"error": "job_store_corrupt", "message": f"job store unreadable: {exc}"}
                ).encode("utf-8")
                status_code = 500
            except Exception as exc:  # noqa: BLE001 — any other fault → 500, fail-closed (never false unknown)
                body = json.dumps(
                    {"error": "internal", "message": f"{type(exc).__name__}: {exc}"}
                ).encode("utf-8")
                status_code = 500
            finally:
                self.server.request_gate.release()
            self._send(body, "application/json; charset=utf-8", status=status_code, head=head)
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
        """The governed write routes — ``POST /edit`` + ``POST /action`` — behind the
        write/auth boundary. Both fork only at dispatch, AFTER the shared auth checks;
        both refuse with 422 ``read_only`` when the source carries no ``write_scope``.

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
        # OFF-BOX write governance factor (spore-129): when this surface is bound off-loopback,
        # loopback-is-auth no longer holds, so the write MUST carry the shared device-held token.
        # Constant-time compare against the token make_server was given. Fail CLOSED if the
        # server is off-loopback yet somehow has no token (make_server refuses to bind a writable
        # off-loopback source WITHOUT one, so reaching here token-less is defense-in-depth). A
        # loopback bind skips this entirely — the localhost-sovereign token-free path is unchanged.
        # The gate also requires write_scope (writability): a READ-ONLY off-box surface has no
        # write path, so it falls through to the 422 'read_only' refusal below (NOT a token-403)
        # — which keeps this gate MIRRORING `_write_token_required` exactly [codex L3 LOW].
        if not self.server.is_loopback_bind and self.server.levain_source.write_scope is not None:
            expected = self.server.write_token or ""
            supplied = self.headers.get(_WRITE_TOKEN_HEADER, "")
            if not expected or not hmac.compare_digest(
                supplied.encode("utf-8"), expected.encode("utf-8")
            ):
                return self._reject(403, "forbidden", "missing or invalid write token")
        # CSRF layer 2: require application/json (a cross-origin page cannot send it
        # without a CORS preflight this server never answers).
        ctype = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        if ctype != "application/json":
            return self._reject(
                415, "unsupported_media_type", "Content-Type must be application/json"
            )
        # The write routes: substrate edits (/edit) + governed channel actions (/action).
        # Both ride this ONE auth gate above (Host → CSRF → off-box token → Content-Type) —
        # /action carries no second, weaker auth path. The off-box token check keys on
        # write_scope, and action verbs REQUIRE a write_scope (make_server enforces it), so
        # the spore-129 governance already covers /action with no change here.
        route = self.path.split("?", 1)[0]
        if route not in ("/edit", "/action"):
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
                if route == "/edit":
                    result = apply_edit(scope, req)
                else:  # /action — governed channel-verb dispatch (the write-peer of extra_panels)
                    result = apply_action(scope, self.server.extra_verbs, req,
                                          job_runtime=self.server.job_runtime)
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
    extra_verbs: "Mapping[str, ActionVerb] | None" = None,
    write_token: str | None = None,
    job_runtime: "JobRuntime | None" = None,
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

    Non-loopback binding (a LAN / Tailscale IP) is governed by the source's nature:
    - a READ-ONLY, NO-INSTALL source (``write_scope is None`` AND ``install_root is None`` —
      e.g. flow's self-ops cockpit over a bare anneal store) MAY bind off-loopback: the write
      route 422s and there is no seed/config to expose, so the private mesh (Tailscale's
      WireGuard) is the access boundary;
    - a WRITABLE source MAY bind off-loopback ONLY with ``write_token`` (spore-129) — the
      off-box governance factor that replaces loopback-is-auth. Then POST /edit requires the
      ``X-Levain-Write-Token`` header (constant-time compared); a loopback bind needs no token
      (the localhost-sovereign path is unchanged). Without a token a writable source stays
      loopback-only;
    - an INSTALL-bearing source stays loopback-only UNCONDITIONALLY (its seed/config is
      operator-private; a token does not relax this — a different concern than spore-129).
    Raises ``ValueError`` (before binding) on a disallowed non-loopback bind. [codex L3 MED]"""
    # Wildcard / public binds are refused for ANY source — a non-loopback bind is for
    # ONE specific private/mesh interface, never every interface or the internet.
    reason = _rejected_bind_host(host)
    if reason:
        raise ValueError(
            f"refusing to bind {host!r}: {reason}. Pass a SPECIFIC private interface "
            "IP (e.g. your Tailscale IP), not a wildcard or a public address."
        )
    if not _is_loopback_host(host):
        # An INSTALL-bearing source is loopback-only UNCONDITIONALLY — its seed/config is
        # operator-private (matching the pre-WriteScope boundary that gated on install
        # presence). A token does NOT relax this; it is a different concern than spore-129.
        # _is_install_bearing checks BOTH install_root fields (source + write_scope) — they can
        # diverge and the write path keys off the write_scope's [L1 + codex L3].
        if _is_install_bearing(source):
            raise ValueError(
                f"refusing to bind {host!r}: an install-bearing substrate is loopback-only "
                "(127.0.0.1 / localhost) — its seed/config is operator-private. Only a "
                "NO-INSTALL source MAY bind a non-loopback address for private-mesh access."
            )
        # A WRITABLE source MAY bind off-loopback (spore-129) — but ONLY with a write_token,
        # the off-box governance factor that replaces loopback-is-auth. Without one it stays
        # loopback-only (its no-token localhost-sovereign POST /edit boundary assumes loopback).
        # `not write_token` (not `is None`): an EMPTY-string token must also refuse the bind, not
        # bind-then-brick. The per-request gate already treats "" as no-token (`not expected` →
        # 403), so without this an off-box writable bind with write_token="" would pass here and
        # then 403 every write — a silently bricked surface instead of a clean refusal [L2 LOW].
        if source.write_scope is not None and not write_token:
            raise ValueError(
                f"refusing to bind {host!r}: a WRITABLE substrate may bind a non-loopback "
                "(private-mesh / Tailscale) address ONLY with a write_token — the off-box "
                "governance factor that replaces the loopback bind as the write auth. Pass "
                "write_token=<shared secret the device holds>, or bind loopback "
                "(127.0.0.1 / localhost) for the token-free localhost-sovereign write path."
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

    # Governed action verbs (the POST /action seam — the write-peer of extra_panels): a
    # name → ActionVerb registry. REQUIRE a writable source: actions are mutations, and tying
    # them to write_scope means the off-box write-token governance (the bind-refusal above + the
    # per-request gate, both keyed on write_scope) already covers /action with NO second auth
    # path (structural_invariants_beat_discipline). Validate the registry shape at registration
    # so a bug surfaces here, not as a 500 at request time.
    extra_verbs = dict(extra_verbs or {})
    if extra_verbs:
        if source.write_scope is None:
            raise ValueError(
                "refusing extra_verbs: action verbs are mutations and require a WRITABLE source "
                "(write_scope) — a read-only source has no governed write/audit path for them."
            )
        for name, spec in extra_verbs.items():
            if not isinstance(name, str) or not name:
                raise ValueError(f"refusing action verb {name!r}: name must be a non-empty string.")
            if not isinstance(spec, ActionVerb):
                raise ValueError(
                    f"refusing action verb {name!r}: value must be an ActionVerb "
                    "(handler, confirm_required, label)."
                )
            # Validate the runtime FIELD types too (codex L3): a non-callable handler, non-bool
            # confirm_required, or non-str label passes the isinstance check above but later breaks
            # dispatch or /substrate.json serialization (label is JSON-emitted in action_verbs).
            # Fail at registration (before bind), matching the published API.
            if not callable(spec.handler):
                raise ValueError(f"refusing action verb {name!r}: handler must be callable.")
            if not isinstance(spec.confirm_required, bool):
                raise ValueError(f"refusing action verb {name!r}: confirm_required must be a bool.")
            if not isinstance(spec.job, bool):
                raise ValueError(f"refusing action verb {name!r}: job must be a bool.")
            if not isinstance(spec.label, str):
                raise ValueError(f"refusing action verb {name!r}: label must be a string.")

    # The async-job runtime for I/O-BOUND (``job=True``) verbs. AUTO-CREATE one when a job verb is
    # registered and the caller didn't inject its own (tests inject; production auto-creates): the
    # store lives under the write_scope's ledger_root (the same governed location as the idempotency
    # store + audit trail), with a bounded executor. extra_verbs already required a write_scope, so
    # ledger_root is present. SWEEP at startup — a fresh process has no live workers, so any
    # non-terminal job on disk is a crash orphan, marked ``failed`` so a poll tells the truth.
    if job_runtime is None and any(getattr(s, "job", False) for s in extra_verbs.values()):
        assert source.write_scope is not None  # guaranteed by the extra_verbs write-scope check above
        job_runtime = JobRuntime(JobStore(source.write_scope.ledger_root / "jobs.json"))
    if job_runtime is not None:
        job_runtime.store.sweep(datetime.now(timezone.utc).isoformat())

    assets = {fn: load_web_asset(fn).encode("utf-8") for fn, _ in _ASSETS.values()}
    httpd = _LevainHTTPServer((host, port), _Handler)
    httpd.levain_source = source
    httpd.levain_assets = assets
    httpd.extra_assets = extra_assets
    httpd.extra_json = extra_json
    httpd.extra_panels = extra_panels
    httpd.extra_verbs = extra_verbs
    httpd.job_runtime = job_runtime
    # OFF-BOX write auth (spore-129): key the token requirement on the ACTUAL bound address
    # (un-foolable — the real socket, not the requested ``host`` string). A loopback-bound
    # server skips the POST /edit token check (the token-free localhost-sovereign path is
    # unchanged); an off-loopback writable bind enforces ``write_token`` (the bind-refusal
    # above already guaranteed a writable off-loopback source was given one).
    httpd.is_loopback_bind = _is_loopback_host(str(httpd.server_address[0]))
    httpd.write_token = write_token
    # GENERAL post-bind reality check (codex L3): the pre-bind refusal rejects a wildcard/PUBLIC
    # *requested* host, but the ACTUAL bound address can still be wildcard/public via a resolver
    # surprise (a hosts-file/DNS mapping of a loopback-CLASSIFIED name to such an address) — for
    # ANY source class, including a read-only or token'd one the specific checks below don't cover.
    # Serving flow's substrate on 0.0.0.0 / a public IP is the catastrophic case the whole bind
    # refusal exists to prevent, so verify the BOUND address and refuse it universally. (Private /
    # CGNAT-mesh addresses — Tailscale 100.64/10 — are is_global False, so a legit mesh bind passes.)
    try:
        _bound_ip = ipaddress.ip_address(str(httpd.server_address[0]))
        _disallowed_bound = _bound_ip.is_unspecified or _bound_ip.is_global
    except ValueError:
        _disallowed_bound = False  # a non-IP bound address (unix socket etc.) — not this concern
    if _disallowed_bound:
        bound = str(httpd.server_address[0])
        httpd.server_close()
        raise ValueError(
            f"refusing to serve: requested host {host!r} bound a wildcard/public address "
            f"({bound}). Verify the ACTUAL bound address, never trust the resolver — bind an "
            "explicit loopback or private-mesh interface."
        )
    # POST-BIND reality check (codex L3 MED): the bind-refusal above validates the REQUESTED
    # host string, but a loopback-CLASSIFIED host that RESOLVES off-box (e.g. a tampered/odd
    # hosts file mapping 'localhost' to a non-loopback IP the box holds) would bind off-loopback
    # with is_loopback_bind False. Re-verify against the ACTUAL bound address and refuse —
    # verify-the-bound-address, don't trust the resolver (the same structural discipline the flow
    # Bridge applies at its own exposure point). Two cases the actual-address check closes:
    #   (a) an INSTALL-bearing source → its operator-private seed/config is READABLE off-box via GET;
    #   (b) a WRITABLE (non-install) source given NO token → it was meant to be the token-free
    #       loopback-sovereign write path, but resolved off-box, so its substrate reads (and the
    #       /action verbs) would be served off-box without the token the off-box factor requires
    #       (writes still token-gate / 422, but the GET read path does not) [L2 LOW]. A writable
    #       source WITH a token is a legitimate off-box mesh bind — not refused here.
    if not httpd.is_loopback_bind and (
        _is_install_bearing(source)
        or (source.write_scope is not None and not write_token)
    ):
        bound = str(httpd.server_address[0])
        httpd.server_close()
        why = ("an install-bearing source's seed/config is operator-private (loopback-only)"
               if _is_install_bearing(source)
               else "a writable source without a write_token is loopback-only (its reads + "
                    "action verbs must not be served off-box without the off-box token factor)")
        raise ValueError(
            f"refusing to serve: requested host {host!r} bound a non-loopback address "
            f"({bound}), but {why}. Bind an explicit loopback address"
            + ("" if _is_install_bearing(source) else " or pass a write_token for an off-box bind")
            + "."
        )
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
    write: bool = False,
) -> int:
    """``levain serve`` entry point — serve the substrate dashboard on localhost.

    READ-ONLY by default (``write=False``): the served source carries no write scope,
    so every edit affordance is dark and ``POST /edit`` 422s — a pure inspection
    cockpit. ``write=True`` (``levain serve --write``) serves the GOVERNED WRITABLE
    cockpit — State / spore / Tray-Keep / episode edits through the same ``apply_edit``
    seam ``levain tui`` uses, under the localhost-sovereign auth (the loopback bind +
    the Host/CSRF guards ARE the auth; no token on loopback). An install-bearing
    substrate is loopback-only either way (its seed/config is operator-private), so
    ``serve`` never binds off-box — a posture that fits a network surface: read-only
    is the safe default, writes are an explicit opt-in (mirrors flow's bridge cockpit).

    Returns nonzero only if the store is unreachable before the server starts, or
    the bind fails (e.g. the port is in use) — mirroring ``levain dashboard`` /
    ``serve-app``. A degraded sub-tier renders visibly and is not a startup
    failure. Blocks in ``serve_forever`` until interrupted (Ctrl+C → clean exit 0).
    """
    # `serve` ALWAYS resolves an install-bearing source (SubstrateSource.local sets
    # install_root), so make_server keeps it loopback-only in BOTH read-only and --write
    # modes — the `--host` "there is no off-box serve" help relies on this invariant. If a
    # future refactor ever lets `serve` resolve a no-install source, the read-only strip
    # below would leave write_scope=None AND install_root=None, which make_server permits
    # off-loopback — re-pin loopback-only here if that ever changes.
    source = _resolve_source(path)
    if not write:
        # READ-ONLY by default — drop the governed write surface the install source
        # carries (`SubstrateSource.local` sets a write_scope for `levain tui`; `serve`
        # opts OUT unless --write). install_root is KEPT so seed/config still RENDER —
        # only the WRITE path is suppressed (writable:false + POST /edit 422). A network
        # surface defaults read-only; --write is the deliberate, governed opt-in.
        source = dataclasses.replace(source, write_scope=None)
    if not source.anneal.episodic_db.exists():
        print(
            f"No anneal store at {source.anneal.episodic_db}.\n"
            "Run `levain init` in this directory, or pass --path to an install.",
            file=sys.stderr,
        )
        return 1

    try:
        httpd = make_server(source, host=host, port=port)
    except ValueError as exc:  # bind refused — wildcard/public, or an install-bearing/writable source off-loopback
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
    mode = "GOVERNED WRITABLE" if source.write_scope is not None else "read-only"
    print(f"  {mode} · localhost-only · Ctrl+C to stop")

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

"""levain.cockpit.routes: the two cockpit GET routes (design §3.1, §3.2), served inside the
dashboard server's existing read gate. This module owns the cockpit-specific HTTP semantics:
the credential class, ETag / 304, and the cache headers that keep one caller's manifest from
being served to another through a shared cache."""

from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

from levain.cockpit.engine import Cockpit, QueryError, RowNotFound

MANIFEST_PATH = "/cockpit/manifest.json"
FRESHNESS_PATH = "/cockpit/freshness.json"
EVENTS_PATH = "/cockpit/events"      # the change stream (which panels moved; no panel data)
PANEL_PREFIX = "/cockpit/panel/"
PANEL_SUFFIX = ".json"
TOKEN_HEADER = "X-Levain-Write-Token"
# The manifest and the panels differ per credential, so a shared cache must key on the credential
# header and may not serve one caller's copy to another: ``private`` plus ``Vary``.
CACHE_CONTROL = "private, no-cache"
VARY = TOKEN_HEADER
PROFILES = ("full", "compact")
# Hashed into every validator: bumped when a wire projection changes (rev 2: compact `now` rows are
# refs, panel heads carry edit_class; rev 3: kind contract v2, panel heads carry searchable / window), so a client holding an older body revalidates to a 200.
REPRESENTATION = 3


def is_cockpit_path(path: str) -> bool:
    return path in (MANIFEST_PATH, FRESHNESS_PATH, EVENTS_PATH) or (path.startswith(PANEL_PREFIX) and path.endswith(PANEL_SUFFIX))


def credential_for(supplied: str, expected: str | None) -> dict[str, Any]:
    """The credential class for one expected token (tests and single-token callers). The server
    verifies against every surface's token itself and passes the result to ``handle_get``."""
    ok = bool(expected) and hmac.compare_digest(supplied.encode("utf-8"), (expected or "").encode("utf-8"))
    return {"class": "token" if ok else "none", "device_class": None}




def _etag_header(etag: str, variant: list[Any]) -> str:
    """A WEAK validator: the body also carries ``as_of`` / ``generated_at``, which the etag leaves
    out on purpose, so two byte-distinct bodies are semantically equivalent, not identical. The
    variant (profile, query, row, credential) is hashed as canonical JSON, never joined with a
    delimiter a query string could contain."""
    h = hashlib.sha256(json.dumps([REPRESENTATION, etag, variant], separators=(",", ":")).encode()).hexdigest()[:32]
    return f'W/"{h}"'


def _matches(if_none_match: str | None, etag: str) -> bool:
    if not if_none_match:
        return False
    if if_none_match.strip() == "*":
        return True
    return any(t.strip().removeprefix("W/") == etag.removeprefix("W/") for t in if_none_match.split(","))


def handle_get(
    cockpit: Cockpit, raw_path: str, *, if_none_match: str | None, credential: dict[str, Any] | None = None,
    token: str = "", expected_token: str | None = None, transport: dict[str, Any] | None = None,
) -> tuple[int, bytes, list[tuple[str, str]]]:
    """Return ``(status, body, extra headers)`` for a cockpit GET. Never raises on a provider
    fault: a panel's fault is carried in that panel's own ``status``/``error``. ``transport`` is the
    server's write capability, published in the manifest."""
    parts = urlsplit(raw_path)
    path = parts.path
    qs = parse_qs(parts.query)
    cred = credential if credential is not None else credential_for(token, expected_token)
    base_headers = [("Vary", VARY)]

    def jerr(status: int, error: str, message: str) -> tuple[int, bytes, list[tuple[str, str]]]:
        return status, json.dumps({"error": error, "message": message}).encode(), base_headers

    def ok(payload: dict[str, Any], variant: list[Any]) -> tuple[int, bytes, list[tuple[str, str]]]:
        etag = _etag_header(payload["etag"], variant)
        hdrs = base_headers + [("ETag", etag)]
        if _matches(if_none_match, etag):
            return 304, b"", hdrs
        return 200, json.dumps(payload, separators=(",", ":")).encode("utf-8"), hdrs

    if path == FRESHNESS_PATH:
        # small and NEVER answered 304: no validator is offered, and If-None-Match is ignored, so a
        # client that revalidated a panel always reads that panel's current age here.
        return 200, json.dumps(cockpit.freshness(), separators=(",", ":")).encode("utf-8"), base_headers
    if path == MANIFEST_PATH:
        return ok(cockpit.manifest(cred, transport=transport), ["manifest", cred["class"]])
    panel_id = unquote(path[len(PANEL_PREFIX): -len(PANEL_SUFFIX)])
    profile = (qs.get("profile") or ["full"])[0]
    if profile not in PROFILES:
        return jerr(400, "bad_request", f"profile must be one of {list(PROFILES)}")
    q = (qs.get("q") or [""])[0] or None      # the decoded value as sent: the kernel validates it, nothing trims it first
    row = (qs.get("row") or [""])[0] or None
    # Search is a READ on the panel's own auth: nothing here authenticates differently, and ``q`` goes
    # nowhere but the cockpit call (it is hashed into the validator, never logged or journalled).
    limit_raw = (qs.get("limit") or [""])[0]
    try:
        limit = int(limit_raw) if limit_raw else None
    except ValueError:
        return jerr(400, "bad_request", "limit must be an integer")
    try:
        panel = cockpit.panel(panel_id, profile=profile, q=q, row=row, credential_class=cred["class"], limit=limit)
    except QueryError as exc:
        return jerr(400, "bad_request", str(exc))
    except RowNotFound:
        return jerr(404, "not_found", f"panel {panel_id!r} has no row {row!r}")
    if panel is None:
        return jerr(404, "not_found", f"no panel {panel_id!r}")
    variant = ["panel", profile, q, row, limit, cred["class"]]
    return ok(panel, variant)

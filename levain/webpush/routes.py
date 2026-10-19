"""The HTTP surface of web push, as one function the phone server dispatches to.

``handle`` returns ``(status, body, content_type)`` for a push path and ``None`` for any other, so the caller falls
through to its own routes. It never authenticates: the caller says whether the request is ``authorized`` (and by
which paired device, when it is one). The static files (service worker, manifest, icons) are public by nature: the
browser fetches the worker and manifest without the page's credential.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from urllib.parse import quote, urlsplit

from cryptography.hazmat.primitives.asymmetric import ec

from datetime import datetime, timezone

from levain.webpush import devices, ece, vapid
from levain.webpush.store import PushStore

STATIC = Path(__file__).resolve().parent / "static"
_STATIC_FILES = {
    "/sw.js": ("sw.js", "text/javascript; charset=utf-8"),
    "/app.webmanifest": ("app.webmanifest", "application/manifest+json"),
    "/icon-192.png": ("icon-192.png", "image/png"),
    "/icon-512.png": ("icon-512.png", "image/png"),
    "/apple-touch-icon.png": ("apple-touch-icon.png", "image/png"),
}
MAX_BODY = 8192
MAX_ENDPOINT = 2048
_PAIR_CODE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
_LABEL = re.compile(r"^(?!-)[a-z0-9-]{1,63}(?<!-)$")
JSON = "application/json"


def _json(status: int, obj: object) -> tuple[int, bytes, str]:
    return status, json.dumps(obj, separators=(",", ":")).encode("utf-8"), JSON


def _err(status: int, code: str, message: str) -> tuple[int, bytes, str]:
    return _json(status, {"error": code, "message": message})


def public_dns_host(host: str) -> bool:
    """A DNS name with at least two labels: no IP literal (v4, v6 or numeric), no ``localhost`` or single-label
    name. The stored endpoint is later POSTed to, so a loopback or private address is refused here."""
    host = host.lower().rstrip(".")
    if not host or len(host) > 253 or ":" in host:
        return False
    labels = host.split(".")
    if len(labels) < 2 or not all(_LABEL.match(x) for x in labels):
        return False
    if labels[-1].isdigit() or host.endswith((".localhost", ".local", ".internal", ".lan", ".home.arpa")):
        return False
    return True


def valid_endpoint(endpoint: object) -> str | None:
    """The endpoint when it is an https URL on a public DNS host with no credentials, else None."""
    if not isinstance(endpoint, str) or not endpoint or len(endpoint) > MAX_ENDPOINT or not endpoint.isascii():
        return None
    try:
        parts = urlsplit(endpoint)
        port = parts.port
    except ValueError:
        return None
    if parts.scheme != "https" or parts.username or parts.password or parts.fragment:
        return None
    if port not in (None, 443) or not parts.hostname or not public_dns_host(parts.hostname):
        return None
    return endpoint


def _body_json(body: bytes) -> dict | None:
    if len(body) > MAX_BODY:
        return None
    try:
        obj = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    return obj if isinstance(obj, dict) else None


def handle(method: str, path: str, query: dict, body: bytes, *, queue_dir: Path | str, authorized: bool,
           device_id: str | None) -> tuple[int, bytes, str] | None:
    if method == "GET" and path in _STATIC_FILES:
        name, ctype = _STATIC_FILES[path]
        data = (STATIC / name).read_bytes()
        if path == "/app.webmanifest":
            pair = query.get("pair")
            pair = pair[0] if isinstance(pair, list) and pair else pair
            if isinstance(pair, str) and _PAIR_CODE.match(pair):
                manifest = json.loads(data)
                manifest["start_url"] = "/?pair=" + quote(pair, safe="")
                data = json.dumps(manifest, indent=2).encode("utf-8")
        return 200, data, ctype
    if not path.startswith("/webpush/"):
        return None
    if method == "POST" and path == "/webpush/pair/redeem":
        # the installed app has no credential yet: the one-time code is the credential
        obj = _body_json(body)
        conn = PushStore(queue_dir).connection()
        try:
            got = devices.redeem(conn, obj.get("code") if obj else None, datetime.now(timezone.utc))
        finally:
            conn.close()
        if got is None:
            return _err(403, "bad_code", "the pairing code is unknown, spent or expired")
        return _json(200, {"device_id": got[0], "token": got[1]})
    if not authorized:
        return _err(403, "credential_required", "this page's credential is needed")
    store = PushStore(queue_dir)
    if method == "GET" and path == "/webpush/key":
        _, public, _sub = vapid.ensure_keys(queue_dir)
        return _json(200, {"key": public})
    if method == "GET" and path == "/webpush/record":
        return _json(200, {"pings": store.record(100)})
    if method == "POST" and path == "/webpush/pair":
        obj = _body_json(body) or {}
        conn = store.connection()
        try:
            code = devices.mint_pairing(conn, obj.get("label"), datetime.now(timezone.utc))
        finally:
            conn.close()
        return _json(200, {"code": code})
    if method == "POST" and path == "/webpush/subscribe":
        obj = _body_json(body)
        keys = obj.get("keys") if obj else None
        endpoint = valid_endpoint(obj.get("endpoint")) if obj else None
        if endpoint is None:
            return _err(400, "bad_endpoint", "the endpoint must be an https URL on a public DNS host")
        try:
            p256dh, auth = keys["p256dh"], keys["auth"]
            ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), ece.b64d(p256dh))
            if len(ece.b64d(auth)) != 16:
                raise ValueError("auth")
        except (TypeError, KeyError, ValueError):
            return _err(400, "bad_keys", "keys.p256dh must be a P-256 point and keys.auth 16 bytes, base64url")
        return _json(200, {"id": store.add_subscription(endpoint, p256dh, auth, device_id)})
    if method == "POST" and path == "/webpush/unsubscribe":
        obj = _body_json(body)
        endpoint = valid_endpoint(obj.get("endpoint")) if obj else None
        if endpoint is None:
            return _err(400, "bad_endpoint", "the endpoint must be an https URL on a public DNS host")
        return _json(200, {"retired": store.retire_subscription(endpoint)})
    if method == "POST" and path == "/webpush/opened":
        obj = _body_json(body)
        ping = obj.get("ping") if obj else None
        if not isinstance(ping, int) or isinstance(ping, bool):
            return _err(400, "bad_ping", "ping must be an integer id")
        return _json(200, {"recorded": store.add_open(ping, device_id)})
    return _err(404, "not_found", "no such push path")

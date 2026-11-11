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
from urllib.parse import urlsplit, urlunsplit

from cryptography.hazmat.primitives.asymmetric import ec

from levain.webpush import ece, vapid
from levain.webpush.store import PAIRED_PREFIX, DeviceNotLive, PushStore

STATIC = Path(__file__).resolve().parent / "static"
_STATIC_FILES = {
    "/sw.js": ("sw.js", "text/javascript; charset=utf-8"),
    "/app.webmanifest": ("app.webmanifest", "application/manifest+json"),
    "/icon-192.png": ("icon-192.png", "image/png"),
    "/icon-512.png": ("icon-512.png", "image/png"),
    "/icon-maskable-192.png": ("icon-maskable-192.png", "image/png"),
    "/icon-maskable-512.png": ("icon-maskable-512.png", "image/png"),
    "/shot-narrow.png": ("shot-narrow.png", "image/png"),
    "/shot-wide.png": ("shot-wide.png", "image/png"),
    "/badge-96.png": ("badge-96.png", "image/png"),
    "/apple-touch-icon.png": ("apple-touch-icon.png", "image/png"),
}
STATIC_PATHS = frozenset(_STATIC_FILES)
WORKER_PATH = "/sw.js"
# The one line of the worker whose value the server derives (see :func:`render_worker`); the worker's source keeps this
# placeholder, and a source that does not carry it exactly once is refused, never served.
WORKER_VERSION_LINE = b"const SW_VERSION = '__LEVAIN_SHELL_VERSION__';"


def worker_template() -> bytes:
    """The worker's source as shipped (its version still the placeholder). Raises OSError when the file is missing."""
    return (STATIC / _STATIC_FILES[WORKER_PATH][0]).read_bytes()


def render_worker(template: bytes, version: str) -> bytes:
    """``template`` with its version placeholder replaced by ``version`` (a short lowercase-hex label). Raises ValueError
    unless the placeholder line appears exactly once, so a worker whose version could not be written is never served."""
    if not re.fullmatch(r"[0-9a-f]{1,64}", version):
        raise ValueError(f"refusing to start: the worker version {version!r} is not a hex label")
    if template.count(WORKER_VERSION_LINE) != 1:
        raise ValueError("refusing to start: the service worker source does not carry its version placeholder line "
                         f"{WORKER_VERSION_LINE.decode()!r} exactly once")
    return template.replace(WORKER_VERSION_LINE, WORKER_VERSION_LINE.replace(b"__LEVAIN_SHELL_VERSION__", version.encode("ascii")))
MAX_BODY = 8192
MAX_RECEIPT_BODY = 512
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
MAX_ENDPOINT = 2048
_HOST = re.compile(r"^(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
JSON = "application/json"


def _json(status: int, obj: object) -> tuple[int, bytes, str]:
    return status, json.dumps(obj, separators=(",", ":")).encode("utf-8"), JSON


def _err(status: int, code: str, message: str) -> tuple[int, bytes, str]:
    return _json(status, {"error": code, "message": message})


# The push services a browser subscribes through today: FCM (Chrome, Edge-on-Android, Brave, Samsung, Opera),
# Mozilla autopush (Firefox), Apple (Safari, installed web apps) and WNS (Edge on Windows). A host is accepted by
# NAME, never by shape: a lexical "looks public" test passes ``127.0.0.1.nip.io`` and ``0x7f.0x1``, which resolve to
# loopback, and a stored endpoint is later POSTed to from the operator's machine.
PUSH_HOST_SUFFIXES = ("fcm.googleapis.com", "push.apple.com", "push.services.mozilla.com", "notify.windows.com")
MAX_INT = 2**63


def push_service_host(host: str) -> bool:
    """True when ``host`` is one of :data:`PUSH_HOST_SUFFIXES` or a subdomain of one (``web.push.apple.com``,
    ``wns2-par02p.notify.windows.com``). The caller has lowercased it."""
    return bool(_HOST.match(host)) and any(host == s or host.endswith("." + s) for s in PUSH_HOST_SUFFIXES)


def valid_endpoint(endpoint: object) -> str | None:
    """The normalised endpoint (lowercase scheme and host) when it is an https URL on a known push service with no
    credentials, port, whitespace or control character, else None. Checked again at send time."""
    if not isinstance(endpoint, str) or not endpoint or len(endpoint) > MAX_ENDPOINT or not endpoint.isascii():
        return None
    if any(c.isspace() or ord(c) < 33 or ord(c) == 127 or c == "\\" for c in endpoint):
        return None
    try:
        parts = urlsplit(endpoint)
        port = parts.port
        host = parts.hostname
    except ValueError:
        return None
    if parts.scheme != "https" or parts.username or parts.password or parts.fragment or "@" in parts.netloc:
        return None
    if port not in (None, 443) or not host or not push_service_host(host.lower()):
        return None
    return urlunsplit(("https", host.lower(), parts.path, parts.query, ""))


def _int(value: object) -> int | None:
    """``value`` when it is an integer in 1 .. 2**63 - 1 (not a bool), else None."""
    if isinstance(value, bool) or not isinstance(value, int) or not 0 < value < MAX_INT:
        return None
    return value


def _body_json(body: bytes) -> dict | None:
    if len(body) > MAX_BODY:
        return None
    try:
        obj = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        return None
    return obj if isinstance(obj, dict) else None


def _subscription(body: bytes) -> "tuple[dict, None] | tuple[None, tuple[int, bytes, str]]":
    """A subscribe body as ``(obj, None)``, its endpoint normalised, or ``(None, the 400 it earns)``."""
    obj = _body_json(body)
    keys = obj.get("keys") if obj else None
    endpoint = valid_endpoint(obj.get("endpoint")) if obj else None
    if endpoint is None:
        return None, _err(400, "bad_endpoint", "the endpoint must be an https URL on a known push service")
    try:
        p256dh, auth = keys["p256dh"], keys["auth"]
        ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), ece.b64d(p256dh))
        if len(ece.b64d(auth)) != 16:
            raise ValueError("auth")
    except (TypeError, KeyError, ValueError):
        return None, _err(400, "bad_keys", "keys.p256dh must be a P-256 point and keys.auth 16 bytes, base64url")
    return {**obj, "endpoint": endpoint}, None


def subscription_refusal(body: bytes) -> tuple[int, bytes, str] | None:
    """The 400 ``handle`` would answer this subscribe body with, or None when it is one ``handle`` stores (the cockpit
    asks before it pairs a browser, so a bad body mints nothing)."""
    return _subscription(body)[1]


_RECEIPT_REFUSALS = {"unknown_device": (403, "unknown_device"), "unknown_ping": (404, "unknown_ping"),
                     "bad_report": (400, "bad_report"), "limited": (429, "limited")}


def receipt(body: bytes, *, queue_dir: Path | str) -> tuple[int, bytes, str]:
    """``POST /webpush/receipt {ping, kind, at, endpoint_sha256}``: a device's own report that a push arrived or was shown.
    It is the one webpush write that carries no token: the service worker holds no credential, so the report is
    authenticated by the SHA-256 of the device's own push-subscription endpoint, which only that device and this server
    know. It appends ids and times only (never ping text), and the caller must not hand it to :func:`handle`, whose
    every other route is token-gated. An unknown hash, an absent ping or one never pushed to that device's subscription
    (the same 404 as an absent one), a malformed or oversize body: refused, nothing written, and no refusal takes the
    store's write lock (:meth:`PushStore.add_receipt`)."""
    if len(body) > MAX_RECEIPT_BODY:
        return _err(413, "too_large", "a receipt is a few hundred bytes")
    obj = _body_json(body)
    ping = _int(obj.get("ping")) if obj else None
    digest = obj.get("endpoint_sha256") if obj else None
    if (ping is None or not isinstance(digest, str) or _SHA256.match(digest) is None
            or not {"ping", "kind", "at", "endpoint_sha256"} <= set(obj)
            or not set(obj) <= {"ping", "kind", "at", "endpoint_sha256", "target", "how"}):
        return _err(400, "bad_report", "send ping, kind, at and endpoint_sha256 (a tap adds target and how), and nothing else")
    got = PushStore(queue_dir).add_receipt(digest, ping, obj["kind"], obj["at"], obj.get("target"), obj.get("how"))
    if got in _RECEIPT_REFUSALS:
        status, code = _RECEIPT_REFUSALS[got]
        return _err(status, code, "this receipt was not recorded")
    return _json(200, {"recorded": got == "recorded"})


def handle(method: str, path: str, query: dict, body: bytes, *, queue_dir: Path | str, authorized: bool,
           device_id: str | None, journal: Path | str | None = None) -> tuple[int, bytes, str] | None:
    """``journal``: the cockpit data root whose pairing journal holds ``device_id`` when it is a ``paired-`` device
    (the cockpit's caller), stored with its subscription and asked again inside the subscribe's write."""
    if method == "GET" and path == WORKER_PATH:
        return None   # the worker is the server's to render (its version is derived, :func:`render_worker`): never the raw source
    if method == "GET" and path in _STATIC_FILES:
        name, ctype = _STATIC_FILES[path]
        return 200, (STATIC / name).read_bytes(), ctype
    if not path.startswith("/webpush/"):
        return None
    if not authorized:
        return _err(403, "credential_required", "this page's credential is needed")
    store = PushStore(queue_dir)
    if method == "GET" and path == "/webpush/key":
        _, public, _sub = vapid.ensure_keys(queue_dir)
        return _json(200, {"key": public})
    if method == "GET" and path == "/webpush/record":
        from levain.webpush import record as rec

        return _json(200, {"pings": rec.record(Path(queue_dir), store.record(limit=rec.SHOWN))})
    if method == "POST" and path == "/webpush/subscribe":
        obj, refusal = _subscription(body)
        if refusal is not None:
            return refusal
        endpoint, p256dh, auth = obj["endpoint"], obj["keys"]["p256dh"], obj["keys"]["auth"]
        if not isinstance(device_id, str) or not device_id.startswith(PAIRED_PREFIX):
            # only a paired device subscribes: no credential without a device (the owner key included) becomes one
            return _err(403, "credential_required", "only a paired device turns pings on")
        if not journal:
            # the device is paired; this server names no pairing journal for it (a cockpit run without a broker), so
            # pings cannot be turned on here: unavailable, not the 403 a phone with no pairing gets and not a 500 fault
            return _err(503, "no_pairing_journal", "this cockpit names no pairing journal, so pings cannot be turned on here")
        try:
            return _json(200, {"id": store.add_subscription(endpoint, p256dh, auth, device_id, journal=journal)})
        except DeviceNotLive:
            return _err(403, "credential_required", "this device was revoked")
    if method == "POST" and path == "/webpush/unsubscribe":
        obj = _body_json(body)
        endpoint = valid_endpoint(obj.get("endpoint")) if obj else None
        if endpoint is None:
            return _err(400, "bad_endpoint", "the endpoint must be an https URL on a known push service")
        return _json(200, {"retired": store.retire_subscription(endpoint)})
    if method == "POST" and path == "/webpush/opened":
        obj = _body_json(body)
        ping = _int(obj.get("ping")) if obj else None
        if ping is None:
            return _err(400, "bad_ping", "ping must be an integer id")
        return _json(200, {"recorded": store.add_open(ping, device_id)})
    if method == "POST" and path == "/webpush/landed":
        # a cockpit page says where it landed and what it did about a tap (the TAP record, store.TAP_KINDS); the device is
        # the credential's own, the body only a bounded target and how
        obj = _body_json(body)
        if not obj or set(obj) != {"target", "how"}:
            return _err(400, "bad_report", "send target and how, and nothing else")
        got = store.add_landing(device_id, obj["target"], obj["how"])
        if got == "recorded":
            return _json(200, {"recorded": True})
        return _err(429 if got == "limited" else 400, got, "this landing was not recorded")
    if method == "POST" and path == "/webpush/trace":
        from levain.webpush import tap_report

        obj = _body_json(body)
        entries = tap_report.clean(obj.get("entries")) if obj else None
        if entries is None:
            return _err(400, "bad_trace", "entries must be a list of objects")
        try:
            return _json(200, {"kept": tap_report.record(queue_dir, device_id, entries, page=obj.get("page"))})
        except tap_report.TraceBusy:
            return _err(503, "trace_busy", "the trace report file is busy; the page posts again later")
        except OSError:
            return _err(503, "trace_unavailable", "the trace report could not be written")
    if method == "POST" and path == "/webpush/devtest":   # devtest (band 299): the notification-action device test's report
        from levain.webpush import devtest

        report = devtest.clean(_body_json(body))
        if report is None:
            return _err(400, "bad_devtest", "send event shown|click and the allowlisted fields only")
        try:
            return _json(200, {"kept": devtest.record(queue_dir, device_id, report)})
        except devtest.RateLimited:
            return _err(429, "limited", "too many device-test reports; wait a minute")
        except devtest.Busy:
            return _err(503, "devtest_busy", "the report file is busy; the page posts again")
        except OSError:
            return _err(503, "devtest_unavailable", "the report could not be written")
    if method == "POST" and path == "/webpush/devtest/send":   # devtest: ask the server to push the test notification to THIS device
        from levain.webpush import devtest

        delay = devtest.clean_send(_body_json(body))
        if delay is None:
            return _err(400, "bad_devtest", "send {delay_s: 10 or 60} and nothing else")
        try:
            return _json(200, devtest.request_send(queue_dir, device_id, delay))
        except devtest.NoDevice:
            return _err(403, "no_device", "only a paired device with pings turned on can run the test")
        except devtest.Pending:
            return _err(409, "pending", "a test push is already on its way to this device")
        except devtest.SendLimited:
            return _err(429, "limited", "too many test pushes this hour")
    return _err(404, "not_found", "no such push path")

"""levain.http_guards — the request guards every local Levain HTTP surface shares.

``levain serve`` (``web_server``), ``levain init --web`` (``init_server``) and ``levain docs``
(``docs_server``) are three stdlib ``BaseHTTPRequestHandler`` subclasses. Each used to carry its own
copy of the same guard code: the security headers, the DNS-rebinding Host check, the cross-site read
refusal, and (for the two with a write route) the cross-origin, JSON-only and Content-Length checks
on a write. The copies drifted: a fix that moved the security headers into ``end_headers`` (so the
stdlib's own error responses carry them too) reached two of the three, and ``levain serve`` answered
an OPTIONS or PUT with a 501 that had no CSP, no ``nosniff`` and no ``X-Frame-Options`` (spore-1013,
reproduced 2026-10-03). Every guard now lives here once, in :class:`GuardedHandler`, and each server
subclasses it. The guards that every request meets first (Host, cross-site or write origin, and the launch token)
run in ``do_GET`` / ``do_HEAD`` / ``do_POST`` here, before a server's own ``_route`` / ``_post`` is reached. A
server keeps only what is its own: its routes, its body limit, and the ORDER in which it applies the remaining
write checks. (Server-level setup, the allowed-hosts set and ``handle_error``, is still
per server; see the routed list in Levain's project notes.)

Stdlib only; nothing here imports another Levain module, so every server can import it.
"""
from __future__ import annotations

import hmac
import json
import os
from http.server import BaseHTTPRequestHandler
from typing import Any

__all__ = [
    "LAUNCH_TOKEN_HEADER",
    "GuardedHandler",
    "host_header_allowed",
]

# The LAUNCH TOKEN (np-ebb8a399, codex HIGH f5d5e483a0c7e3a8). Loopback reachability is not file access: another OS
# user, a container reaching host loopback (host.docker.internal) and a sandboxed app with a network entitlement can all
# connect to 127.0.0.1 without being able to read the operator's files. So a server that sets ``launch_token`` on its
# server instance refuses every request that does not carry it, except the paths it names in ``token_free_paths`` (the
# page shell, which carries no operator data). The token is generated per launch, held only in the process, and printed
# once to the terminal that started it. ``X-Levain-Chat-Token`` is the header the chat routes took before the token
# covered every route; it is still accepted so a script written against it keeps working.
LAUNCH_TOKEN_HEADER = "X-Levain-Token"
_LEGACY_TOKEN_HEADERS = ("X-Levain-Chat-Token",)

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


def host_header_allowed(
    raw_host: str | None, allowed_hosts: "frozenset[str] | set[str]"
) -> bool:
    """True iff a request's raw ``Host`` header names one of ``allowed_hosts``.

    The SECURITY-CRITICAL DNS-rebinding parse, in one place so every server
    (through :meth:`GuardedHandler._host_ok`) uses ONE implementation and none can
    DIVERGE on it: a divergence here is a rebinding read-disclosure hole. Strict RFC-7230:
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


class GuardedHandler(BaseHTTPRequestHandler):
    """The shared guard surface. A subclass sets ``server_version``, and its server instance must
    carry ``allowed_hosts`` (the loopback names plus the bound address)."""

    # A tidy, modern protocol version (enables keep-alive + proper 1.1 behavior).
    protocol_version = "HTTP/1.1"
    server_version = "levain"
    # Without a socket timeout a client that declares a Content-Length and then stalls holds a
    # server thread forever (the body is read before any rate gate can help). 30s lets a real
    # localhost request finish and kills a stalled one.
    timeout = 30
    server: Any

    def end_headers(self) -> None:
        """Stamp the security headers on EVERY response, structurally. ``_send`` is the normal path,
        but the stdlib's ``send_error`` (an unsupported method, an OPTIONS preflight, a malformed
        request) builds its own response that never passes through ``_send``; stamping here covers
        those too. Nothing else sets these headers, so each appears exactly once per response."""
        self.send_header("Content-Security-Policy", _CSP)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        # Snapshots are per-request; never let a browser cache a stale one.
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def _send(
        self, body: bytes, content_type: str, status: int = 200, *, head: bool = False
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        # Always the length the GET body WOULD be, so a HEAD reports correct framing without a body.
        self.send_header("Content-Length", str(len(body)))
        # When the connection is being closed (a refused write whose body was not read), say so,
        # so the client reads the response instead of seeing a reset keep-alive socket.
        if self.close_connection:
            self.send_header("Connection", "close")
        self.end_headers()
        if not head:
            self.wfile.write(body)

    def _send_json(self, payload: dict[str, object], status: int = 200) -> None:
        self._send(
            json.dumps(payload).encode("utf-8"),
            "application/json; charset=utf-8",
            status=status,
        )

    def version_string(self) -> str:
        # The stdlib's default Server header appends "Python/X.Y"; a localhost tool advertises nothing.
        return self.server_version

    def _host_ok(self) -> bool:
        """True iff the request's ``Host`` names a loopback the server answers for (DNS rebinding:
        a hostile page that rebinds its own name to 127.0.0.1 still sends its own Host)."""
        return host_header_allowed(self.headers.get("Host"), self.server.allowed_hosts)

    def _launch_token_required(self) -> bool:
        """True iff this server was started with a launch token. A server that never set one is ungated (a
        downstream ``make_server`` caller that keeps its own auth posture)."""
        return getattr(self.server, "launch_token", None) is not None

    def _launch_token_valid(self) -> bool:
        """True iff the request carries this launch's token, in the current header or the legacy chat one.
        Constant-time compare on bytes; an empty expected or supplied token fails closed."""
        expected = (getattr(self.server, "launch_token", None) or "").encode("utf-8")
        if not expected:
            return False
        for name in (LAUNCH_TOKEN_HEADER, *_LEGACY_TOKEN_HEADERS):
            supplied = self.headers.get(name)
            if supplied and hmac.compare_digest(supplied.encode("utf-8"), expected):
                return True
        return False

    def _token_refusal_body(self) -> bytes:
        return json.dumps({
            "error": "launch_token",
            "message": (f"this server needs the token printed when it started, sent as {LAUNCH_TOKEN_HEADER}"),
        }).encode("utf-8")

    def _refuse_untokened_read(self, *, head: bool) -> bool:
        """The launch-token gate for a read: every path except the server's ``token_free_paths`` (the page shell)
        needs the token, including a path that does not exist, so an unauthenticated caller cannot probe the route
        set. Sends a JSON 403 (``error: launch_token``) and returns True when refused."""
        if not self._launch_token_required():
            return False
        path = self.path.split("?", 1)[0]
        if path in getattr(self.server, "token_free_paths", frozenset()):
            return False
        if self._launch_token_valid():
            return False
        self._send(self._token_refusal_body(), "application/json; charset=utf-8", status=403, head=head)
        return True

    def _refuse_untokened_write(self) -> bool:
        """The launch-token gate for a POST: no POST path is token-free. Refused before any body read (the
        connection closes, see :meth:`_reject`). Returns True when refused."""
        if not self._launch_token_required() or self._launch_token_valid():
            return False
        self._reject(403, "launch_token",
                     f"this server needs the token printed when it started, sent as {LAUNCH_TOKEN_HEADER}")
        return True

    def _cross_site_read(self) -> bool:
        """True for a cross-site browser read. A same-origin fetch sends ``same-origin``, a top-level
        navigation ``none``, and a non-browser client nothing; only a hostile page sends
        ``cross-site``. Refusing it stops such a page from even triggering a read."""
        return self.headers.get("Sec-Fetch-Site") == "cross-site"

    def _refuse_read(self, *, head: bool) -> bool:
        """The read preamble every server runs first: the Host allowlist, then the cross-site
        refusal, each answered with a plain 403. Sends it and returns True when refused. A server
        adds its own read gates after this (the dashboard's off-box token)."""
        if not self._host_ok() or self._cross_site_read():
            self._send(b"forbidden\n", "text/plain; charset=utf-8", status=403, head=head)
            return True
        return False

    def _reject(self, status: int, error: str, message: str) -> None:
        """Refuse a write BEFORE its body is read: close the connection (so the unread body cannot
        desync a kept-alive socket) and send the error JSON."""
        self.close_connection = True
        self._send_json({"error": error, "message": message}, status)

    def _drain(self, n: int) -> None:
        """Read and discard up to ``n`` bytes of the request body in bounded chunks, so a refused
        request's body does not dangle on a kept-alive connection."""
        remaining = n
        while remaining > 0:
            chunk = self.rfile.read(min(remaining, 65536))
            if not chunk:
                break
            remaining -= len(chunk)

    def _refuse_write_origin(self) -> bool:
        """The first two write checks: the Host allowlist, then CSRF layer 1 (``Sec-Fetch-Site`` must
        be absent, a non-browser client, or ``same-origin``, our own page). Sends the refusal and
        returns True when the request is refused."""
        if not self._host_ok():
            self._reject(403, "forbidden", "bad Host header")
            return True
        sfs = self.headers.get("Sec-Fetch-Site")
        if sfs is not None and sfs != _WRITE_SEC_FETCH_ALLOWED:
            self._reject(403, "forbidden", "cross-origin write refused")
            return True
        return False

    def _refuse_non_json(self) -> bool:
        """CSRF layer 2: the body must be ``application/json``, which a cross-origin page cannot send
        without a CORS preflight these servers never answer. Sends the refusal and returns True when
        the request is refused."""
        ctype = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        if ctype != "application/json":
            self._reject(415, "unsupported_media_type", "Content-Type must be application/json")
            return True
        return False

    def _refuse_oversize(self, clen: int, limit: int, drain_cap: int) -> bool:
        """Refuse a body declared larger than ``limit`` with a 413. Sends it and returns True when
        refused.

        ⛔ ALWAYS CLOSE, AND GUARD THE DRAIN. Both halves were once missing from ``levain serve``
        while ``levain init --web`` had them (a hardening fix that reached one of two copies; run
        against both real servers by Diogenes 2026-08-19). The connection is closed unconditionally,
        because a truncated or lying body would otherwise desync the next request on a kept-alive
        socket. The drain is bounded by ``drain_cap`` and its socket timeout is caught: a stalled
        body raised ``TimeoutError``, which the servers' ``handle_error`` swallows as a benign
        keep-alive reset, so the client got no status at all instead of the 413."""
        if clen <= limit:
            return False
        self.close_connection = True
        if clen <= drain_cap:
            try:
                self._drain(clen)
            except OSError:
                pass  # stalled/short body: send the 413 anyway (TimeoutError is an OSError)
        self._send_json({"error": "too_large", "message": f"body exceeds {limit} bytes"}, 413)
        return True

    def _declared_length(self) -> int | None:
        """The request's Content-Length, or ``None`` after sending a 411 when it is missing or not
        ASCII digits.

        ⛔ ``isascii()`` IS LOAD-BEARING. ``str.isdigit()`` is True for characters ``int()`` refuses
        (``"²".isdigit()`` is True and ``int("²")`` raises), so a ``Content-Length: ²`` header used to
        pass the guard and blow up on the next line. RFC 7230 makes Content-Length ASCII digits."""
        raw = self.headers.get("Content-Length")
        if raw is None or not (raw.isascii() and raw.isdigit()):
            self._reject(411, "length_required", "Content-Length required")
            return None
        try:
            return int(raw)
        except ValueError:
            # More digits than Python converts (sys.int_info.str_digits_check_threshold): not a
            # length any body could have (codex L3 2026-10-03).
            self._reject(411, "length_required", "Content-Length required")
            return None

    # -- routing and logging, the same on every server ------------------------

    # DENY BY DEFAULT. The guards run HERE, before a server's own routing is reached: the Host allowlist, the
    # cross-site refusal and the launch token for a read; the Host allowlist, the write-origin check and the launch
    # token for a POST. A server defines ``_route`` and ``_post`` and never the ``do_*`` methods, so a route it adds
    # later is behind every guard without writing one.

    def _route(self, *, head: bool) -> None:  # pragma: no cover — every server defines its own
        raise NotImplementedError

    def _post(self) -> None:
        """A server with no POST route refuses every POST (after the guards, so the answer does not reveal that)."""
        self._reject(404, "not_found", "no such route")

    def do_GET(self) -> None:  # noqa: N802 — BaseHTTPRequestHandler contract
        if self._refuse_read(head=False) or self._refuse_untokened_read(head=False):
            return
        self._route(head=False)

    def do_HEAD(self) -> None:  # noqa: N802 — same routing, headers only (no body)
        if self._refuse_read(head=True) or self._refuse_untokened_read(head=True):
            return
        self._route(head=True)

    def do_POST(self) -> None:  # noqa: N802 — BaseHTTPRequestHandler contract
        if self._refuse_write_origin() or self._refuse_untokened_write():
            return
        self._post()

    def log_message(self, fmt: str, *args: object) -> None:
        """Quiet by default: each server prints its own startup line, and a per-request access log
        is noise in an interactive terminal. ``LEVAIN_SERVE_VERBOSE`` restores it on stderr."""
        if os.environ.get("LEVAIN_SERVE_VERBOSE"):
            super().log_message(fmt, *args)

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

import hashlib
import hmac
import json
import os
import secrets
import stat
import sys
import threading
import time
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import Any, Callable, TextIO

__all__ = [
    "LAUNCH_TOKEN_HEADER",
    "LINK_CODE_HEADER",
    "LINK_PATH",
    "RUNTIME_DIR_NAME",
    "UNLOCK_PATH",
    "GuardedHandler",
    "PublishedToken",
    "arm_launch_token",
    "check_launch_token",
    "host_header_allowed",
    "mint_link_code",
    "new_launch_token",
    "open_unlocked",
    "publish_launch_token",
    "read_running",
    "request_link_code",
    "runtime_dir",
    "SigtermStop",
    "stop_on_sigterm",
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

# The link a browser opens never carries the token. It carries a single-use LINK CODE (``#code=...``) that the page
# trades once, by ``POST /unlock``, for the token. Measured 2026-10-07: Chrome's History database kept every
# ``#token=`` link this lane opened, fragment included, after the page had stripped it from the address bar; a spent
# code is all such a record can hold now. A code expires unused after LINK_CODE_SECONDS.
LINK_CODE_HEADER = "X-Levain-Link-Code"
LINK_CODE_SECONDS = 600
UNLOCK_PATH = "/unlock"   # token-free: trades a link code for the token
LINK_PATH = "/link"       # mints a fresh link code (`levain serve --open-running`), for a caller that proves the token
# /link is authenticated by proof, both ways, so the token itself never crosses the socket: the caller sends a nonce
# and HMAC(token, request || nonce); the server answers with the code and HMAC(token, reply || nonce || code), which
# the caller checks before it opens anything. A listener that is not the server (another user's process that took the
# port, a proxy) learns nothing it can use and cannot hand back a code the caller will open (L2 2026-10-07).
LINK_NONCE_HEADER = "X-Levain-Link-Nonce"
LINK_PROOF_HEADER = "X-Levain-Link-Proof"
LINK_TIME_HEADER = "X-Levain-Link-Time"   # the request's wall-clock second, inside the proof
LINK_SKEW_SECONDS = 120                   # a proof older (or newer) than this is refused; nonces are kept longer


def _link_proof(token: str, *parts: str) -> str:
    return hmac.new(token.encode("utf-8"), "\x00".join(parts).encode("utf-8"), hashlib.sha256).hexdigest()

# Where a running server leaves its token for the operator, one file per port: a 0700 directory of 0600
# files in the home directory. It is a crown jewel (levain.firing.confinement denies it to an entity's hands), so a
# server's token reaches the operator's terminal or this directory and nothing else: never a log, never a pipe.
RUNTIME_DIR_NAME = ".levain-runtime"


def new_launch_token() -> str:
    """A fresh launch token: 256 bits, URL-safe base64 (43 characters)."""
    return secrets.token_urlsafe(32)


def _token_shaped(value: str) -> bool:
    return bool(value) and all(c.isascii() and (c.isalnum() or c in "-_") for c in value)


# A token must fit comfortably in one request header line (codex L3: an over-long one would be refused by the
# stdlib's line limit before any handler ran, bricking the server). The generated ones are 43 characters.
_TOKEN_LEN = (8, 256)


def check_launch_token(token: "str | None") -> None:
    """Refuse a launch token the page could not carry. ``None`` passes: its meaning is the caller's (a server builder
    reads it as "make one up", :func:`arm_launch_token` as "ungated", an explicit choice). Anything else must be
    non-empty URL-safe base64 (``secrets.token_urlsafe``'s alphabet), the only shape token.js takes from a URL
    fragment and the form keeps intact: an empty token would let every request through, and a token with a space
    or a symbol would print an unlocked link the page refuses (codex L3 r1)."""
    if token is None:
        return
    if not _token_shaped(token) or not _TOKEN_LEN[0] <= len(token) <= _TOKEN_LEN[1]:
        raise ValueError(f"a launch token must be {_TOKEN_LEN[0]} to {_TOKEN_LEN[1]} characters of A-Z, a-z, 0-9, "
                         "'-' and '_'; omit it to have one generated.")

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


_warned_legacy: set[str] = set()


def _warn_legacy_header(name: str) -> None:
    """Once per process, on stderr: a client sent the token in a deprecated header."""
    if name in _warned_legacy:
        return
    _warned_legacy.add(name)
    print(f"levain: a client sent the token as {name}, which is deprecated; send {LAUNCH_TOKEN_HEADER}.",
          file=sys.stderr, flush=True)


# Handlers allowed to define their own do_* methods: none. The set can only shrink, and it is empty.
_DO_METHOD_EXEMPT: frozenset[tuple[str, str]] = frozenset()


class GuardedHandler(BaseHTTPRequestHandler):
    """The shared guard surface. A subclass sets ``server_version``, and its server instance must
    carry ``allowed_hosts`` (the loopback names plus the bound address) and ``launch_token`` (see
    :func:`arm_launch_token`). A subclass defines ``_route`` (and ``_post`` if it has a write route), never a
    ``do_*`` method: defining one raises ``TypeError`` when the class is created, because it would run before, and
    instead of, the guards in :meth:`do_GET` / :meth:`do_HEAD` / :meth:`do_POST`."""

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        # Every do_* the class would dispatch to, wherever in its MRO it comes from (a mixin ahead of GuardedHandler
        # counts as much as the class's own body), must be GuardedHandler's.
        own = sorted(n for n in dir(cls) if n.startswith("do_")
                     and next(k for k in cls.__mro__ if n in vars(k)) is not GuardedHandler)
        if own and (cls.__module__, cls.__qualname__) not in _DO_METHOD_EXEMPT:
            raise TypeError(f"{cls.__qualname__} defines {own}: a GuardedHandler routes through _route / _post, "
                            "so the shared guards (Host, origin, launch token) always run first.")

    # The methods this server answers, as its 405s' ``Allow`` says them. A read-only server declares ``"GET, HEAD"``
    # once, and a POST then gets the guards and a 405 like any other method (head ruling 2026-10-07).
    allow = "GET, HEAD, POST"

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
        but the stdlib's ``send_error`` (a malformed request line, an oversized header) builds its own
        response that never passes through ``_send``; stamping here covers those too. Nothing else sets
        these headers, so each appears exactly once per response. A 405 also carries ``Allow``."""
        if getattr(self, "_allow", None):
            self.send_header("Allow", self._allow)
        self.send_header("Content-Security-Policy", _CSP)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        # No page here has a reason to tell another site where it was opened from (a docs link to an outside
        # site would otherwise carry this loopback URL in its Referer).
        self.send_header("Referrer-Policy", "no-referrer")
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
        """True iff this server was started with a launch token. Read without a default: a server must say
        ``launch_token = None`` to be ungated (``make_server`` does, for a downstream that keeps its own auth), and one
        that never set the attribute fails the request instead of serving it ungated (complement L3 r2)."""
        return self.server.launch_token is not None

    def _launch_token_valid(self) -> bool:
        """True iff the request carries this launch's token, in the current header or the legacy chat one.
        Constant-time compare on bytes; an empty expected or supplied token fails closed."""
        expected = (getattr(self.server, "launch_token", None) or "").encode("utf-8")
        if not expected:
            return False
        for name in (LAUNCH_TOKEN_HEADER, *_LEGACY_TOKEN_HEADERS):
            supplied = (self.headers.get(name) or "").strip()
            if supplied and hmac.compare_digest(supplied.encode("utf-8"), expected):
                if name != LAUNCH_TOKEN_HEADER:
                    _warn_legacy_header(name)
                return True
        return False

    def _token_refusal_body(self) -> bytes:
        return json.dumps({
            "error": "launch_token",
            "message": (f"this server needs its token, sent as {LAUNCH_TOKEN_HEADER}: open the page with "
                        f"`levain serve --open-running --port {self.server.server_address[1]}`"),
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
                     f"this server needs its token, sent as {LAUNCH_TOKEN_HEADER}: open the page with "
                     f"`levain serve --open-running --port {self.server.server_address[1]}`")
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
    # token for a POST. A server defines ``_route`` and ``_post``, not the ``do_*`` methods, so a route it adds later
    # is behind every guard without writing one.

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
        if self._refuse_write_origin():
            return
        path = self.path.split("?", 1)[0]
        if path == UNLOCK_PATH and self._launch_token_required():
            return self._unlock()
        if path == LINK_PATH and self._launch_token_required():
            return self._link()
        if self._refuse_untokened_write():
            return
        self._post()

    def _other_method(self) -> None:
        """A method not in ``allow`` passes the same guards as a write, then gets 405 with ``Allow``: without this
        BaseHTTPRequestHandler answered 501 before any guard ran (codex L3). No body is read."""
        self.close_connection = True
        if self._refuse_write_origin() or self._refuse_untokened_write():
            return
        self._allow = self.allow
        self._reject(405, "method_not_allowed", f"this server answers {self.allow}")

    def handle_one_request(self) -> None:
        """The stdlib's request loop with ONE change, the dispatch: GET, HEAD and POST go to this class's guarded
        ``do_*``, and every other method, whatever its name (PROPFIND, a lowercase "get", any token), goes through
        the same guards to a 405 (:meth:`_other_method`). The stdlib looked up ``do_<METHOD>`` and answered 501
        before any guard ran (codex L3 + L1). The one exempt handler (``_DO_METHOD_EXEMPT``) keeps the stdlib loop
        until its gate lands."""
        if (type(self).__module__, type(self).__qualname__) in _DO_METHOD_EXEMPT:
            return super().handle_one_request()
        try:
            self.raw_requestline = self.rfile.readline(65537)
            if len(self.raw_requestline) > 65536:
                self.requestline = ""
                self.request_version = ""
                self.command = ""
                self.send_error(HTTPStatus.REQUEST_URI_TOO_LONG)
                return
            if not self.raw_requestline:
                self.close_connection = True
                return
            if not self.parse_request():
                return   # an error was sent
            served = {m.strip() for m in self.allow.split(",")}
            route = {"GET": self.do_GET, "HEAD": self.do_HEAD, "POST": self.do_POST}.get(self.command)
            # The guard's own routes (trade a link code, mint one) belong to the guard layer, not to the handler's
            # `allow`: a read-only server must still unlock (lane E2, measured on a live team view).
            if (self.command == "POST" and self.path.split("?", 1)[0] in (UNLOCK_PATH, LINK_PATH)
                    and self._launch_token_required()):
                route = self.do_POST
            elif self.command not in served:
                route = None
            (route if route is not None else self._other_method)()
            self.wfile.flush()
        except TimeoutError as e:
            self.log_error("Request timed out: %r", e)
            self.close_connection = True

    def _link(self) -> None:
        """``POST /link``: mint a link code for a caller that proves it holds the token without sending it (see
        ``LINK_PROOF_HEADER``). No body is read."""
        self.close_connection = True
        nonce = (self.headers.get(LINK_NONCE_HEADER) or "").strip()
        proof = (self.headers.get(LINK_PROOF_HEADER) or "").strip()
        stamp = (self.headers.get(LINK_TIME_HEADER) or "").strip()
        token = self.server.launch_token
        # The proof binds a time, so a request seen once cannot be replayed after its nonce is forgotten (gemini L3).
        fresh = (stamp.isascii() and stamp.isdigit() and len(stamp) <= 12   # a bounded int() (codex + glm L3)
                 and abs(time.time() - int(stamp)) <= LINK_SKEW_SECONDS)
        if (len(nonce) < 16 or not _token_shaped(nonce) or not proof or not fresh
                or not hmac.compare_digest(proof.encode("utf-8"),
                                           _link_proof(token, "levain-link-request", nonce, stamp).encode("utf-8"))):
            return self._send_json({"error": "link_proof", "message": "a link needs proof of this server's token"}, 403)
        if not _first_use_of_nonce(self.server, nonce):
            return self._send_json({"error": "link_proof", "message": "that request was already answered"}, 403)
        code = mint_link_code(self.server)
        self._send_json({"code": code, "expires_in": LINK_CODE_SECONDS,
                         "proof": _link_proof(token, "levain-link-reply", nonce, code)})

    def _unlock(self) -> None:
        """``POST /unlock``: trade a single-use link code (``X-Levain-Link-Code``) for the launch token. The one POST
        that needs no token, so it is refused unless the code is one this server minted, unused and unexpired; the
        code is spent by the attempt that matches it. Reached only after the Host and write-origin checks, so a
        cross-site page cannot call it, and a caller that never saw the link has nothing to send. No body is read."""
        self.close_connection = True
        code = (self.headers.get(LINK_CODE_HEADER) or "").strip()
        if code and _spend_link_code(self.server, code):
            return self._send_json({"token": self.server.launch_token})
        self._send_json({"error": "link_code", "message": "that link was already used or has expired"}, 403)

    def log_message(self, fmt: str, *args: object) -> None:
        """Quiet by default: each server prints its own startup line, and a per-request access log
        is noise in an interactive terminal. ``LEVAIN_SERVE_VERBOSE`` restores it on stderr."""
        if os.environ.get("LEVAIN_SERVE_VERBOSE"):
            super().log_message(fmt, *args)


# -- the launch token's lifecycle, for every server ----------------------------------------------------------------


def arm_launch_token(server: Any, token: "str | None", token_free_paths: "frozenset[str] | set[str]") -> None:
    """Set a server's gate: ``token`` (None: ungated, an explicit choice) and the paths that skip it (the page shell,
    which must carry no operator data). The one place a server's gate is configured."""
    check_launch_token(token)
    server.launch_token = token
    server.token_free_paths = frozenset(token_free_paths)
    server.link_codes = {}
    server.link_nonces = {}
    server.link_codes_lock = threading.Lock()


def _link_state(server: Any) -> "tuple[dict[str, tuple[float, float]], threading.Lock]":
    codes = getattr(server, "link_codes", None)
    lock = getattr(server, "link_codes_lock", None)
    if codes is None or lock is None:
        codes, lock = {}, threading.Lock()
        server.link_codes, server.link_codes_lock = codes, lock
    return codes, lock


def _now() -> "tuple[float, float]":
    """(monotonic, wall) seconds. A deadline holds while BOTH are short of it: the monotonic clock is immune to a wall
    clock set back, and the wall clock keeps running through sleep, which macOS's monotonic clock does not (L2 + codex
    L3 2026-10-07: a code printed before the lid closed outlived its 10 minutes)."""
    return time.monotonic(), time.time()


def _deadline(seconds: float) -> "tuple[float, float]":
    mono, wall = _now()
    return mono + seconds, wall + seconds


def _expired(deadline: "tuple[float, float]", now: "tuple[float, float]") -> bool:
    return now[0] >= deadline[0] or now[1] >= deadline[1]


def _first_use_of_nonce(server: Any, nonce: str) -> bool:
    """True the first time ``nonce`` proves a /link request on this server (within LINK_CODE_SECONDS), so a request
    seen once cannot be replayed to mint more codes (complement L3)."""
    _codes, lock = _link_state(server)
    now = _now()
    with lock:   # created under the lock (L3 consensus: two first requests could each make their own dict)
        seen = getattr(server, "link_nonces", None)
        if seen is None:
            seen = server.link_nonces = {}
        for n in [n for n, exp in seen.items() if _expired(exp, now)]:
            del seen[n]
        if nonce in seen:
            return False
        seen[nonce] = _deadline(LINK_CODE_SECONDS)
        return True


def mint_link_code(server: Any) -> str:
    """A fresh single-use link code for ``server`` (see ``LINK_CODE_HEADER``)."""
    codes, lock = _link_state(server)
    code = new_launch_token()
    now = _now()
    with lock:
        for c in [c for c, exp in codes.items() if _expired(exp, now)]:
            del codes[c]
        codes[code] = _deadline(LINK_CODE_SECONDS)
    return code


def _spend_link_code(server: Any, code: str) -> bool:
    """True, once, for a code this server minted and has not seen expire. Compared in constant time against every
    live code, so the answer's timing does not depend on how much of a code matches."""
    codes, lock = _link_state(server)
    now = _now()
    supplied = code.encode("utf-8")
    with lock:
        hit = None
        for c, exp in list(codes.items()):
            if hmac.compare_digest(c.encode("utf-8"), supplied) and not _expired(exp, now):
                hit = c
        if hit is None:
            return False
        del codes[hit]
        return True


def runtime_dir() -> Path:
    return Path.home() / RUNTIME_DIR_NAME


def _private_runtime_dir() -> Path:
    """The runtime directory, created 0700 and refused if it is a symlink or not this user's."""
    d = runtime_dir()
    d.mkdir(mode=0o700, exist_ok=True)
    st = os.lstat(d)
    if not stat.S_ISDIR(st.st_mode) or (hasattr(os, "getuid") and st.st_uid != os.getuid()):
        raise OSError(f"{d} is not a directory this user owns; refusing to write a launch token there")
    os.chmod(d, 0o700)
    return d


class _PortLock:
    """An exclusive flock on ``~/.levain-runtime/.<port>.lock`` around a record's write and its conditional removal,
    so one server's close cannot remove another's just-published record between the read and the unlink (codex L3).
    A no-op where fcntl is missing."""

    def __init__(self, d: Path, port: int) -> None:
        self.path = d / f".{int(port)}.lock"
        self.fd: "int | None" = None

    def __enter__(self) -> "_PortLock":
        try:
            import fcntl

            self.fd = os.open(self.path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
            try:
                fcntl.flock(self.fd, fcntl.LOCK_EX)
            except OSError:
                os.close(self.fd)   # __exit__ does not run when __enter__ raises
                self.fd = None
                raise
        except ImportError:
            self.fd = None
        return self

    def __exit__(self, *exc: object) -> None:
        if self.fd is not None:
            os.close(self.fd)   # closing releases the flock


@dataclass
class PublishedToken:
    """What :func:`publish_launch_token` did: the unlocked URL, and the runtime file to remove at shutdown."""

    unlocked: str
    path: "Path | None"
    token: str = ""
    pub_id: str = ""

    def close(self) -> None:
        """Remove this server's runtime file, if it is still this publication's: a later server on the port (even one
        in the same process, even with the same token) may have replaced it, so the record's ``pub_id`` must match."""
        if self.path is None:
            return
        try:
            with _PortLock(self.path.parent, int(self.path.stem)):
                rec = json.loads(self.path.read_text(encoding="utf-8"))
                # The publication's own id (codex L3: a same-process successor reusing an explicit token on the
                # same port would match on pid and token alone).
                if isinstance(rec, dict) and rec.get("pub_id") == self.pub_id:
                    self.path.unlink()
        except Exception:  # noqa: BLE001 — shutdown must go on: a bad record is not a reason to skip server_close
            pass


def publish_launch_token(server: Any, url: str, *, port: int, kind: str,
                         stream: "TextIO | None" = None) -> PublishedToken:
    """Hand the operator the server's token, and nobody else.

    The token goes to ``~/.levain-runtime/<port>.json`` (0600, in a 0700 directory an entity's hands are denied),
    where ``levain serve --open-running --port <port>`` reads it to open the page. It is printed only when ``stream``
    (stdout by default) is a terminal; when it is not (launchd, systemd, a pipe, a log file) only the file's path is
    printed, because a log is a file an entity, a backup or another reader may see. The returned ``unlocked`` link
    carries a single-use link code, never the token (see ``LINK_CODE_HEADER``). If the file cannot be written and
    the stream is not a terminal, this raises ``OSError``: the operator would have no way to get the token."""
    token = getattr(server, "launch_token", None)
    if not token:
        return PublishedToken(url, None)
    out = stream if stream is not None else sys.stdout
    unlocked = f"{url}#code={mint_link_code(server)}"
    pub_id = secrets.token_hex(8)
    path: "Path | None" = None
    err: "OSError | None" = None
    try:
        d = _private_runtime_dir()
        path = d / f"{int(port)}.json"
        tmp = d / f".{int(port)}.{os.getpid()}.{secrets.token_hex(4)}.tmp"
        with _PortLock(d, port):
            # A tmp file a crashed writer left (head ruling: unlink it if stale): its name carries the writer's pid.
            for old in d.glob(f".{int(port)}.*.tmp"):
                if sys.platform == "win32":
                    break   # os.kill(pid, 0) is CTRL_C there (gemini L3); a stale tmp only takes a little space
                try:
                    old_pid = int(old.name.split(".")[2])
                    os.kill(old_pid, 0)
                except (ValueError, IndexError, ProcessLookupError, PermissionError):
                    old.unlink(missing_ok=True)
                except OSError:
                    pass
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    json.dump({"kind": kind, "url": url, "token": token, "pid": os.getpid(), "pub_id": pub_id}, fh)
                os.replace(tmp, path)
            except OSError:
                tmp.unlink(missing_ok=True)
                raise
    except OSError as exc:
        err, path = exc, None
    tty = bool(getattr(out, "isatty", lambda: False)())
    if tty:
        print(f"  token (send as {LAUNCH_TOKEN_HEADER}; valid until this server stops): {token}", file=out, flush=True)
        print(f"  open it unlocked (the link works once): {unlocked}", file=out, flush=True)
        if err is not None:
            print(f"  (could not write {runtime_dir()}: {err}; `levain serve --open-running` will not find this "
                  "server)", file=out, flush=True)
    elif err is not None:
        raise err
    else:
        print(f"  token: not printed here (this output is not a terminal); it is in {path} (0600). "
              f"Open the page with: levain serve --open-running --port {int(port)}", file=out, flush=True)
    return PublishedToken(unlocked, path, token, pub_id)


def read_running(port: int) -> dict[str, Any]:
    """The runtime record a live server on ``port`` left, or ``OSError`` / ``ValueError`` saying why there is none
    (no file, unreadable, or the process that wrote it is gone)."""
    path = runtime_dir() / f"{int(port)}.json"
    rec = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(rec, dict):
        raise ValueError(f"{path} is not a Levain runtime record")
    pid = rec.get("pid")
    if not isinstance(pid, int) or not isinstance(rec.get("token"), str) or not isinstance(rec.get("url"), str):
        raise ValueError(f"{path} is not a Levain runtime record")
    if sys.platform == "win32":
        return rec   # os.kill(pid, 0) sends CTRL_C on Windows; request_link_code's proof check is what refuses a stranger
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        raise ValueError(f"the server that wrote {path} (pid {pid}) is no longer running") from None
    except PermissionError:
        # This user wrote the record, so its pid now belonging to someone else means the writer is gone.
        raise ValueError(f"the server that wrote {path} (pid {pid}) is no longer running") from None
    return rec


_LINK_REPLY_MAX = 4096


def request_link_code(url: str, token: str, *, timeout: float = 5.0) -> str:
    """Ask the server at ``url`` (a loopback origin from a runtime record) for a fresh link code, proving the token
    without sending it, and check the server's proof before returning the code. No proxy is used (an
    ``HTTP_PROXY`` in the environment would otherwise receive the request). Raises ``ValueError`` on a non-loopback
    URL, an answer that does not prove the token, is not HTTP, is too long or takes too long, and ``OSError`` on a
    connection failure."""
    import http.client
    import ipaddress
    import urllib.parse
    import urllib.request

    parts = urllib.parse.urlsplit(url)
    host = parts.hostname or ""
    try:
        loopback = host == "localhost" or ipaddress.ip_address(host).is_loopback
    except ValueError:
        loopback = False
    if parts.scheme != "http" or not loopback or parts.port is None:
        raise ValueError(f"refusing to ask {url!r} for a link: not an http loopback origin")
    nonce = secrets.token_urlsafe(24)
    stamp = str(int(time.time()))
    req = urllib.request.Request(
        f"http://{parts.netloc}{LINK_PATH}", data=b"", method="POST",
        headers={LINK_NONCE_HEADER: nonce, LINK_TIME_HEADER: stamp,
                 LINK_PROOF_HEADER: _link_proof(token, "levain-link-request", nonce, stamp)})
    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *_a: Any, **_k: Any) -> None:   # a 3xx is an answer, never a second request
            return None

    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    got: list[bytes | BaseException] = []

    def exchange() -> None:
        try:
            with opener.open(req, timeout=timeout) as r:  # noqa: S310 — a loopback origin, checked above
                got.append(r.read(_LINK_REPLY_MAX + 1))
        except BaseException as exc:  # noqa: BLE001 — handed to the caller's thread below
            got.append(exc)

    # Bounded in size AND time: whoever holds the port cannot hold this call without end, by volume or by dripping
    # the status line, headers or body a byte at a time (codex + L1). A socket timeout is per receive, so the whole
    # exchange runs on a daemon thread with one deadline; a thread still waiting at it is abandoned.
    worker = threading.Thread(target=exchange, name="levain-link-request", daemon=True)
    worker.start()
    worker.join(2 * timeout)
    if worker.is_alive() or not got:
        raise ValueError("the answer on that port took too long to be a link; not opening it")
    if isinstance(got[0], http.client.HTTPException):   # whatever holds the port did not answer in HTTP (complement)
        raise ValueError("the answer on that port was not a link; not opening it") from got[0]
    if isinstance(got[0], BaseException):
        raise got[0]
    raw = got[0]
    if len(raw) > _LINK_REPLY_MAX:
        raise ValueError("the answer on that port was too long to be a link; not opening it")
    reply = json.loads(raw)
    if not isinstance(reply, dict):
        raise ValueError("the answer on that port was not a link; not opening it")
    code, proof = reply.get("code"), reply.get("proof")
    if not (isinstance(code, str) and _token_shaped(code) and isinstance(proof, str)
            and hmac.compare_digest(proof.encode("utf-8"),
                                    _link_proof(token, "levain-link-reply", nonce, code).encode("utf-8"))):
        raise ValueError("the answer on that port did not prove this server's token; not opening it")
    return code


def open_unlocked(url: str, unlocked: str) -> "str | None":
    """Open the page. ``unlocked`` (the URL with a single-use link code in its fragment) goes ONLY to macOS's osascript
    controller, which hands the URL over on osascript's stdin and then as an Apple Event, never on a command line.
    Every other controller (``open``, xdg-open, a browser binary, ``$BROWSER``) puts the URL in argv, which other OS
    users can read from the process table: the very callers the token exists to keep out. So the osascript
    controller is called directly, never through ``webbrowser.open``, which on a failure would hand the same URL to
    the next registered controller; if it is not the default or fails, the plain URL opens through the usual chain
    and the page's unlock form asks. Returns ``"unlocked"`` when the unlocked link was handed to a browser,
    ``"locked"`` when only the plain URL was (the page will ask for the token), and None when no browser took it."""
    import webbrowser

    try:
        ctl = webbrowser.get()
        if isinstance(ctl, webbrowser.MacOSXOSAScript) and ctl.open(unlocked):
            return "unlocked"
    except Exception:  # noqa: BLE001 — no usable controller, or no MacOSXOSAScript on this platform
        pass
    try:
        if webbrowser.open(url):
            return "locked"
    except Exception:  # noqa: BLE001 — a headless box without a browser is fine
        pass
    return None


class SigtermStop:
    """What :func:`stop_on_sigterm` armed. ``hold()`` makes SIGTERM a no-op for the cleanup (call it first in the
    ``finally``: a SIGTERM that lands during cleanup, say after a Ctrl+C, must not cut it short; complement L3);
    calling the object puts the previous handler back. Unarmed (``SigtermStop()``), both do nothing."""

    def __init__(self, previous: Any = None, *, armed: bool = False) -> None:
        self._previous, self._armed = previous, armed

    def hold(self) -> None:
        import signal

        if self._armed:
            # A Python no-op, not SIG_IGN, which a child started during shutdown would inherit across exec.
            signal.signal(signal.SIGTERM, lambda *_a: None)

    def __call__(self) -> None:
        import signal

        if self._armed:
            # A handler installed from C reads back as None, which signal.signal will not take: restore the default.
            signal.signal(signal.SIGTERM, self._previous if self._previous is not None else signal.SIG_DFL)
            self._armed = False


def stop_on_sigterm() -> SigtermStop:
    """Make SIGTERM (what launchd and systemd send to stop a unit) stop ``serve_forever`` the way Ctrl+C does, so a
    server's ``finally`` runs and its runtime file is removed. Only from the main thread, where Python allows it.
    Returns the :class:`SigtermStop` whose ``hold()`` the cleanup calls first and whose call ends it."""
    import signal

    if threading.current_thread() is not threading.main_thread():
        return SigtermStop()

    def _stop(signum: int, frame: Any) -> None:
        # One stop: a second SIGTERM must not cut the cleanup short. A Python no-op, not SIG_IGN, which a child
        # started during shutdown would inherit across exec (complement L3).
        signal.signal(signal.SIGTERM, lambda *_a: None)
        raise KeyboardInterrupt

    return SigtermStop(signal.signal(signal.SIGTERM, _stop), armed=True)

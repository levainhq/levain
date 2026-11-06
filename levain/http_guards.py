"""levain.http_guards — the request guards every local Levain HTTP surface shares.

``levain serve`` (``web_server``), ``levain init --web`` (``init_server``) and ``levain docs``
(``docs_server``) are three stdlib ``BaseHTTPRequestHandler`` subclasses. Each used to carry its own
copy of the same guard code: the security headers, the DNS-rebinding Host check, the cross-site read
refusal, and (for the two with a write route) the cross-origin, JSON-only and Content-Length checks
on a write. The copies drifted: a fix that moved the security headers into ``end_headers`` (so the
stdlib's own error responses carry them too) reached two of the three, and ``levain serve`` answered
an OPTIONS or PUT with a 501 that had no CSP, no ``nosniff`` and no ``X-Frame-Options`` (spore-1013,
reproduced 2026-10-03). Every guard now lives here once, in :class:`GuardedHandler`, and each server
subclasses it. A server keeps only what is its own: its routes, its body limit, and the ORDER in which
it applies the write checks. (Server-level setup, the allowed-hosts set and ``handle_error``, is still
per server; see the routed list in Levain's project notes.)

Stdlib only; nothing here imports another Levain module, so every server can import it.
"""
from __future__ import annotations

import json
import os
import re
import time
from http.server import BaseHTTPRequestHandler
from typing import Any
from urllib.parse import unquote_plus

__all__ = [
    "BODY_DEADLINE_S",
    "BODY_TIMEOUT_WORDS",
    "LISTEN_BACKLOG",
    "GuardedHandler",
    "host_header_allowed",
]

# The listen queue every Levain HTTP server binds with (its ``request_queue_size``). The stdlib default is 5, and on
# macOS a connection past a full queue is RESET: a dashboard load opens the stylesheet and its scripts at once, each its
# own connection through a proxy such as ``tailscale serve``, and lost a random script (2026-10-25, the iOS cockpit's
# "window.LevainCockpitView.fromManifest" error). The kernel caps it at kern.ipc.somaxconn / net.core.somaxconn.
LISTEN_BACKLOG = 128

# The most time a request body (or the drain of a refused one) may take to arrive, counted ONCE from the first byte
# asked for. The socket timeout below is per recv and restarts with every byte, so a client sending one byte just inside
# it held the handler thread, and on a route that reads under a gate the gate, for as long as it liked. The largest body
# any route takes (and the largest drain) is a few hundred KiB to about a MiB, so this still fits a slow honest link.
BODY_DEADLINE_S = 20.0
BODY_TIMEOUT_WORDS = "The request did not arrive in time; try again."

# A write only ever legitimately originates from our own dashboard page (which sends
# ``Sec-Fetch-Site: same-origin``) or a non-browser client that sends NO Sec-Fetch-
# Site header at all (the operator's own curl/script — sovereign; absent reads as
# Python ``None``). Any present value other than ``same-origin`` — ``cross-site``,
# ``same-site``, or even ``none`` (a top-level navigation, which can't carry a JSON
# POST anyway) — is an unexpected/hostile origin and is refused.
_WRITE_SEC_FETCH_ALLOWED = "same-origin"
_QUERY_STRING = re.compile(r"\?\S*")
# RFC 9110 5.1: a field name is a token. Anything else (whitespace, parentheses, a colon-less fragment) is a name a
# strict peer and a lenient one can read differently.
_FIELD_NAME = re.compile(r"[!#$%&'*+\-.^_`|~0-9A-Za-z]+")
# The methods no Levain route reads a body for. A body on one of them is never read, so its bytes would be parsed as
# the next request on a kept-alive connection; a Content-Length other than 0 on them is refused at the boundary.
_BODILESS_METHODS = frozenset({"GET", "HEAD"})


def _plain_length(raw: str) -> bool:
    """True iff ``raw`` is a Content-Length value made only of ASCII digits.

    ⛔ ``isascii()`` IS LOAD-BEARING. ``str.isdigit()`` is True for characters ``int()`` refuses (``"²".isdigit()`` is
    True and ``int("²")`` raises), so a ``Content-Length: ²`` header once passed the guard and blew up on the next
    line. RFC 9110 8.6 makes Content-Length ASCII digits; a comma list (``2, 2``) is not one value and fails here too."""
    return raw.isascii() and raw.isdigit()


def _redact_query(text: str) -> str:
    """A cockpit search query (parameter ``q``) never reaches a log line. The query is parsed, not
    pattern-matched on its encoded spelling: ``%71`` is ``q`` too."""
    def one(m: "re.Match[str]") -> str:
        pairs = m.group(0)[1:].split("&")
        kept = [f"{pair.split('=', 1)[0]}=-" if unquote_plus(pair.split("=", 1)[0]) == "q" and "=" in pair else pair
                for pair in pairs]
        return "?" + "&".join(kept)
    return _QUERY_STRING.sub(one, text)

# The page only ever loads its own same-origin scripts + stylesheet and fetches its
# own JSON. Lock everything else off. Slice 2a moved the one inline <style> block out
# to a served `/dashboard.css`, so `style-src` is now `'self'` — no `'unsafe-inline'`
# (the Slice-2 tightening the prior comment flagged). `frame-ancestors 'none'` denies
# clickjacking / hostile-iframe embedding (paired with X-Frame-Options below).
_CSP = (
    "default-src 'none'; script-src 'self'; style-src 'self'; "
    "connect-src 'self'; img-src 'self'; manifest-src 'self'; base-uri 'none'; form-action 'none'; "
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
        # Snapshots are per-request; never let a browser cache a stale one. A route that serves a
        # revalidatable, per-credential body (the cockpit's ETag'd reads) sets ``_cache_control``
        # for that one response through ``_send(cache_control=...)``.
        self.send_header("Cache-Control", getattr(self, "_cache_control", None) or "no-store")
        super().end_headers()

    def _send(
        self, body: bytes, content_type: str, status: int = 200, *, head: bool = False,
        headers: "list[tuple[str, str]] | None" = None, cache_control: str | None = None,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        for name, value in headers or ():
            self.send_header(name, value)
        self._cache_control = cache_control
        # Always the length the GET body WOULD be, so a HEAD reports correct framing without a body.
        self.send_header("Content-Length", str(len(body)))
        # When the connection is being closed (a refused write whose body was not read), say so,
        # so the client reads the response instead of seeing a reset keep-alive socket.
        if self.close_connection:
            self.send_header("Connection", "close")
        self.end_headers()
        self._cache_control = None
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
            self._send(self._refusal_text(), "text/plain; charset=utf-8", status=403, head=head)
            return True
        return False

    def _refusal_text(self) -> bytes:
        """The plain 403 body: the word the page always said, then WHY, from the requester's own headers (11-06: a Pixel's browser
        was refused with the bare word and nothing measurable; a photo of this page names the rule that fired). Header values are
        cut and reduced to letters, digits and a few separators so the body cannot carry a request's bytes back as markup."""
        def seen(name: str) -> str:
            v = self.headers.get(name)
            return "(absent)" if v is None else "".join(ch for ch in v[:80] if ch.isalnum() and ch.isascii() or ch in " .-_:;,=/?*") or "(empty)"
        if not self._host_ok():
            why = f"the Host header {seen('Host')} is not a name this server answers for"
        else:
            why = (f"a cross-site page load of {self.path.split('?', 1)[0][:60]!r} (Sec-Fetch-Site {seen('Sec-Fetch-Site')}, "
                   f"Mode {seen('Sec-Fetch-Mode')}, Dest {seen('Sec-Fetch-Dest')}) that is not a pairing or sign page")
        return f"forbidden\n\nrefused: {why}\n".encode("utf-8")

    def _reject(self, status: int, error: str, message: str) -> None:
        """Refuse a write BEFORE its body is read: close the connection (so the unread body cannot
        desync a kept-alive socket) and send the error JSON."""
        self.close_connection = True
        self._send_json({"error": error, "message": message}, status)

    # -- request framing: the one parse boundary ---------------------------------------------------------------------

    def parse_request(self) -> bool:
        """The stdlib's request-line and header parse, then the framing check, before ANY method's handler runs.

        This is the one place every request passes once its headers are parsed, whatever its method or route; a
        framing check inside a route helper (``_declared_length``) was reached only by the POST routes that read a
        body, so a GET or HEAD carrying the same ambiguous framing was answered and its "body" parsed as the next
        request (L3 2026-11-05, four lineages)."""
        if not super().parse_request():
            return False
        return not self._refuse_framing()

    def handle_expect_100(self) -> bool:
        """``Expect: 100-continue`` is answered INSIDE the stdlib's ``parse_request``, before it returns; checking
        the framing only afterwards would first tell the client to send the body of a request about to be refused
        (codex L3 2026-11-05). So the check runs here first too; on a request without ``Expect`` it runs once, in
        ``parse_request``."""
        if self._refuse_framing():
            return False
        return super().handle_expect_100()

    def _refuse_framing(self) -> bool:
        """Refuse a request whose body framing a strict peer and a lenient one could read differently: answer in
        fixed words (400; a single malformed Content-Length keeps its older 411 ``length_required``), close the
        connection, read no body. Returns True when refused.

        Levain sits behind ``tailscale serve`` (a proxy) and may sit behind others. When the proxy and this server
        disagree on where a request's body ends, the leftover bytes become a second request the proxy never saw
        (request smuggling). Every rule below closes one such disagreement, for any method (RFC 9112 6.1, 6.3):

        - any ``Transfer-Encoding``: no Levain route reads a transfer-coded body, so every coding is one this
          server does not speak; refusing all of them (not only beside a Content-Length) leaves one answer for one
          shape, and a refused client is told to send a Content-Length body;
        - ``Content-Length`` more than once or as a comma list (400), or a single value that is not a plain digit
          string (411);
        - a Content-Length other than 0 on a method no route reads a body for (GET, HEAD);
        - a header block the parser could not read cleanly: a recorded defect (whitespace before a colon ends the
          header block early and is recorded as one), a folded continuation line (obs-fold, kept inside the
          previous value), a field name that is not a token, or a leading ``From `` line the parser swallows."""
        headers = self.headers
        if not hasattr(headers, "get_all"):
            return False  # an HTTP/0.9 request: no header block, nothing framed
        refusal: tuple[int, str, str] | None = None
        if headers.defects or headers.get_unixfrom() is not None or any(
            not _FIELD_NAME.fullmatch(name) or "\r" in value or "\n" in value for name, value in headers.items()
        ):
            refusal = (400, "malformed_header_refused", "the request's header block is malformed")
        elif headers.get("Transfer-Encoding") is not None:
            refusal = (400, "transfer_encoding_refused", "Transfer-Encoding is not accepted; send a Content-Length body")
        else:
            lengths = headers.get_all("Content-Length") or []
            if len(lengths) > 1 or (lengths and "," in lengths[0]):  # repeated, or one field carrying a list
                refusal = (400, "content_length_refused", "send one Content-Length")
            elif lengths and not _plain_length(lengths[0]):
                # ``_declared_length``'s long-standing word for a malformed length (411 ``length_required``), kept
                # so its clients and tests see no change; now given for every method, before any route runs.
                refusal = (411, "length_required", "Content-Length required")
            elif lengths and self.command in _BODILESS_METHODS and lengths[0].strip("0"):  # not int(): no digit cap
                refusal = (400, "content_length_refused", f"a {self.command} request carries no body")
        if refusal is None:
            return False
        self.close_connection = True
        status, error, message = refusal
        self._send(json.dumps({"error": error, "message": message}).encode("utf-8"),
                   "application/json; charset=utf-8", status=status, head=self.command == "HEAD")
        return True

    def _read_within(self, n: int, total_deadline_s: float, *, keep: bool) -> bytes:
        """Read up to ``n`` request-body bytes under ONE monotonic deadline across every recv: each recv waits at most
        what is left of the budget (and never more than the per-recv ``timeout``), and the socket's own timeout is
        restored after. Returns the bytes (empty when ``keep`` is false), SHORT when the client closes first. Raises
        ``TimeoutError`` once the budget is spent and ``OSError`` on a reset."""
        deadline = time.monotonic() + total_deadline_s
        pieces: list[bytes] = []
        got = 0
        try:
            while got < n:
                left = deadline - time.monotonic()
                if left <= 0:
                    raise TimeoutError("request body deadline")
                self.connection.settimeout(min(left, self.timeout))
                piece = self.rfile.read1(min(65536, n - got))
                if not piece:        # the client closed before its declared length arrived
                    break
                got += len(piece)
                if keep:
                    pieces.append(piece)
        finally:
            try:
                self.connection.settimeout(self.timeout)
            except OSError:
                pass
        return b"".join(pieces)

    def _read_body(self, clen: int, total_deadline_s: "float | None" = None) -> "bytes | None":
        """The ``clen`` body bytes of a write, read under ONE total deadline (default :data:`BODY_DEADLINE_S`; read at
        call time so a test can move it). A spent deadline is answered 408 in fixed words and the connection closes
        (so the unread rest cannot desync it); a client that reset gets no answer. ``None`` in both cases: nothing more
        to send, the caller just returns. A client that closed early gives the SHORT bytes, as ``rfile.read`` did, and
        the route's own JSON check answers.

        ⛔ EVERY ROUTE READS ITS BODY HERE, never with a bare ``rfile.read``: that read waits per recv, and the
        gate a route holds while it reads (the request gate, a pairing or receipt slot, the install lock) is then
        held for as long as a client trickles one byte at a time. The header block is not covered (the stdlib reads it
        per recv before any route runs), but no gate is held yet while it arrives."""
        budget = BODY_DEADLINE_S if total_deadline_s is None else total_deadline_s
        try:
            return self._read_within(clen, budget, keep=True)
        except TimeoutError:
            try:
                self._reject(408, "request_timeout", BODY_TIMEOUT_WORDS)
            except OSError:      # the client went away while it was being told
                self.close_connection = True
            return None
        except OSError:          # a reset
            self.close_connection = True
            return None

    def _drain(self, n: int) -> None:
        """Read and discard up to ``n`` bytes of the request body, under the same total deadline as a body read, so a
        refused request's body does not dangle on a kept-alive connection and a trickle cannot hold the thread (or the
        gate the caller holds). Raises ``TimeoutError`` when the budget is spent; the only caller,
        :meth:`_refuse_oversize`, still sends its 413."""
        self._read_within(n, BODY_DEADLINE_S, keep=False)

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

        Request FRAMING (a Transfer-Encoding, a repeated or malformed Content-Length, a malformed header) is
        refused earlier, for every method, at the parse boundary (:meth:`_refuse_framing`); by the time a route
        calls this, a Content-Length that is present is one plain digit string. The digit check stays here
        because this is where ``int()`` runs, and a caller that reaches it another way must not crash it."""
        raw = self.headers.get("Content-Length")
        if raw is None or not _plain_length(raw):
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

    def _route(self, *, head: bool) -> None:  # pragma: no cover — every server defines its own
        raise NotImplementedError

    def do_GET(self) -> None:  # noqa: N802 — BaseHTTPRequestHandler contract
        self._route(head=False)

    def do_HEAD(self) -> None:  # noqa: N802 — same routing, headers only (no body)
        self._route(head=True)

    def log_message(self, fmt: str, *args: object) -> None:
        """Quiet by default: each server prints its own startup line, and a per-request access log
        is noise in an interactive terminal. ``LEVAIN_SERVE_VERBOSE`` restores it on stderr."""
        if os.environ.get("LEVAIN_SERVE_VERBOSE"):
            # a cockpit search query (``q=``) is never written to a log, verbose or not
            super().log_message(fmt, *(_redact_query(a) if isinstance(a, str) else a for a in args))

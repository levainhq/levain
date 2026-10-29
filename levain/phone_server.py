"""levain.phone_server — the phone port: answer consent requests with a passkey from a phone (Q6b).

``levain consent phone`` runs this. It is its OWN loopback listener, separate from the cockpit (``levain serve``), and
it serves only these:

  - a static page (``/``, ``/phone.js``, ``/phone.css``) that carries no data;
  - ``GET /consent.json``: the pending consent requests and, for each, the passkey keys enrolled to answer it on
    this server's https name;
  - ``POST /consent/<request_id>/challenge`` and ``.../answer``: the answer record to sign and its WebAuthn
    challenge, then the passkey's assertion, which the consent queue verifies with the same ssh-keygen check as
    ``levain consent answer``;
  - ``POST /consent/<request_id>/rule-challenge`` and ``.../rule``: the same for a late RULING on an undecided
    request (``levain consent rule``), signed in the ruling namespace, never an answer's; once it verifies, the
    ruling is delivered to its session (:mod:`levain.consent.delivery`), on its own thread;
  - ``POST /passkey/key-line``: a new passkey's OpenSSH public key and the laptop commands that enrol it (a passkey
    never enrols itself: enrolment is signed by a laptop-presence key);
  - ``POST /sign/<id>/challenge``, ``.../answer`` and ``.../deny``: a statement a caller filed for the operator's
    passkey to sign, in a namespace the command was started with (``--sign-namespace``), listed in ``consent.json``
    as ``sign_pending``, with ``sign_overflow`` the request files past the listing's cap (:mod:`levain.consent.sign`);
    ``GET /sign/<id>/state`` says where one a link named stands (open, signed, denied, expired, gone), token-gated;
  - ``POST /dispatch/<job>/challenge``, ``.../answer``, ``.../cancel-challenge``, ``.../cancel``,
    ``.../authorise-challenge`` and ``.../authorise``: a filed dispatch job's contract signed, a live one
    cancelled, or a recommended one's run authorised and landed, with a passkey, verified by the job store against the same one
    cockpit as the sign requests; ``consent.json`` lists the live jobs as ``dispatch_pending``, with ``dispatch_note``
    (why the store could not be read) and ``dispatch_overflow`` (:mod:`levain.dispatch.phone`).
  - ``POST /coord/drop``, ``/coord/envelope-review`` and ``/coord/envelope-challenge``: the drop sheet (coordinator
    C1/C2), for a PAIRED DEVICE's token only (this run's phone token is refused 403 ``device_required``): a drop
    delivered to the store ``levain drop`` writes, resent from the phone's outbox under its one ``outbox_id`` until it
    is delivered, bound to the store identity it was made against, optionally carrying a structured envelope signed
    with a passkey and verified by that store, and an accepted drop starting the coordinator launch it asked for;
    ``consent.json`` lists the newest phone drops and their drop receipts as ``coord_drops`` and the store identity as
    ``coord_store_id`` (:mod:`levain.coordinator.phone`). ``GET /coord.json`` (the same token gate) lists the
    coordinator's review queue as ``coord_review`` and its morning card as ``coord_morning`` (each with a ``*_note``
    when it cannot be read); the page asks for it only while its Review tab is shown. ``POST
    /coord/review/<item>/clear`` and ``/reopen`` (a paired device's token only) clear or reopen a plain item as you; a
    "yours to clear" item is cleared only on a passkey signature: ``POST /coord/review/<item>/sign-request`` files
    levain's own sign request over it, the page signs it on ``/sign/<id>/challenge`` and ``answer``, then sends
    ``/coord/review/<item>/clear`` with ``{sign_request}`` (:func:`levain.coordinator.review.clear_signed`).
  - ``POST /grant/<id>/revoke-challenge`` and ``.../revoke``: a consent grant revoked with a passkey made on the
    https name (the one authority link a passkey may sign), the challenge naming the grant's ``instance`` as listed,
    the link built and held here and verified by the authority log itself;
    ``consent.json`` lists the grants as ``grants``, with ``grants_note`` and ``inert_note``
    (:mod:`levain.consent.grant_phone`).
  - ``GET /consent.json`` also lists ``questions`` (the open ones an agent filed, :mod:`levain.consent.questions`) and
    ``POST /question/<n>/answer`` answers one by an option key or free text, once. An answer is UNSIGNED (no
    passkey) and authorises NOTHING: it is words the agent is told; it is then delivered to the agent's session
    on its own thread (:func:`levain.consent.band.deliver_answer`), as a ruling is.
  - ``GET /consent.json`` also lists ``notes`` (the open ones, :mod:`levain.consent.notes`: an agent telling the
    operator something that needs no answer) and ``POST /note/<n>/dismiss`` marks one read, once. A dismissal is
    unsigned, authorises nothing and is delivered to nobody.
  - K8, the phone as a mobile cockpit: ``POST /cockpit/verb`` (the laptop's verb body plus the signing passkey's
    ``fingerprint``), ``GET /cockpit/pending/<id>`` (a phone pending's stored record and its challenge),
    ``POST /cockpit/pending/<id>/signature`` (the passkey's assertion) and ``.../cancel``: proxied in-process into the
    SAME ``verbs.dispatch`` and broker as the laptop's (:mod:`levain.phone_app`), with a ``device`` credential. The
    token here is still only the transport: every such write is a broker pending that applies once the passkey's
    signature verifies. No tier logic lives on this port.

A proxy the operator runs (``tailscale serve``) puts it on an https name. Every data route needs this server's own
token (``X-Levain-Phone-Token``), printed in the phone link's fragment (new each run, unless ``--token-file`` keeps
it; delete that file to make a new one); it is not the cockpit's write token and
opens nothing on the cockpit. The https name is configured, never read from a request: it is the WebAuthn rpId, and a
passkey enrolled on it signs that name's hash, which ssh-keygen checks against the key's application.

``levain consent phone --redirect-to <origin>`` (ONESURFACE OS-4) runs this port as a redirect ONLY
(:func:`run_phone_redirect`): ``/`` and ``/phone.html`` serve one page whose one script sends an old phone link
(``#phone_token=…&sign=<id>&v=…&choice=…``) to the cockpit's sheet, ``<origin>/sign/<id>#v=…&choice=…``, dropping the
token; every other path is 404, and no token is minted or read.

Why a separate listener: a proxy's clients and the laptop's own processes share 127.0.0.1, so no header the cockpit
reads can tell them apart (measured through ``tailscale serve`` 2026-10-16: the Host is the client's, X-Forwarded-For
is the proxy's). A listener that serves nothing else needs no such test.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import os
import json
import re
import secrets
import shlex
import sqlite3
import stat
import sys
import threading
from datetime import datetime, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

from levain.http_guards import LISTEN_BACKLOG, GuardedHandler
from levain.webpush.held import tick as held_tick

__all__ = ["DEFAULT_PHONE_PORT", "PHONE_TOKEN_HEADER", "PhoneError", "make_phone_server", "make_redirect_server",
           "redirect_origin", "run_phone_redirect", "run_phone_server", "tailscale_name"]

DEFAULT_PHONE_PORT = 7428
PHONE_TOKEN_HEADER = "X-Levain-Phone-Token"
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
_STATIC = {"/": ("phone.html", "text/html; charset=utf-8"),
           "/phone.js": ("phone.js", "text/javascript; charset=utf-8"),
           "/phone.css": ("phone.css", "text/css; charset=utf-8"),
           "/phone_app.js": ("phone_app.js", "text/javascript; charset=utf-8"),
           "/phone_app.css": ("phone_app.css", "text/css; charset=utf-8")}
_CONSENT_PREFIX = "/consent/"
_CONSENT_ID = re.compile(r"[A-Za-z0-9._:-]{1,160}")      # the cockpit's consent-body id shape
_ACTIONS = ("challenge", "answer", "rule-challenge", "rule")
_QUESTION_ROUTE = re.compile(r"/question/([0-9]{1,12})/(answer|rule)")
_NOTE_ROUTE = re.compile(r"/note/([0-9]{1,12})/dismiss")    # an agent's note, marked read: unsigned, delivers nothing
_KEY_LINE = "/passkey/key-line"
_SIGN_PREFIX = "/sign/"
_SIGN_ACTIONS = ("challenge", "answer", "deny")
_SIGN_STATE = re.compile(r"/sign/([A-Za-z0-9_-]{8,64})/state")    # GET: where a linked request stands (display only)
_DISPATCH_ROUTE = re.compile(
    r"/dispatch/(dj-[0-9a-f]{32})/(challenge|answer|cancel-challenge|cancel|authorise-challenge|authorise)")
_COORD_ROUTE = re.compile(r"/coord/(drop|envelope-review|envelope-challenge)")
# a coordinator review item cleared or reopened as you, mode manual: a paired device only (head ruling 1025+25), never a
# "yours to clear" item (the store refuses it on this path: its clear is ruled signed-only). The cockpit's verb kernel
# decides a plain clear or reopen, through the phone app (_PhoneServer.coord_review is only its transport); a "yours to
# clear" item's signed clear is decided at the store's door on the passkey signature
_COORD_REVIEW_ROUTE = re.compile(r"/coord/review/(rv-[0-9a-f]{32})/(clear|reopen|sign-request)")
_GRANT_PREFIX = "/grant/"
_MAX_BODY = 16 * 1024          # an assertion is well under 2 KB, an answer record under 1 KB
_MAX_INFLIGHT = 4              # each answer replays the authority log; past this a caller gets 503, never a queue
# a cockpit write (a verb, a signature, a cancel) runs one at a time on its own gate, never a read slot: a signature's
# check is two ssh-keygen runs of 10 s each at most. A queued writer waits this long at most, then answers busy (503);
# past _MAX_INFLIGHT writers (the running one included) one is busy at once, so waiting threads stay bounded
_WRITE_WAIT = 5.0
# how long a ruling's response waits for its delivery, which runs on its own thread holding no request slot (a
# listing and a resume's settle each take seconds to tens of seconds); past this the page says it is still running
_DELIVERY_WAIT = 45.0
# the key a ruling's result carries its delivery's waiter under (taken off before the result is sent)
_AWAIT = "_await_delivery"


class PhoneError(Exception):
    """A refusal a route answers with: an HTTP status, an error word and the words the page shows."""

    def __init__(self, status: int, error: str, message: str, extra: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.status, self.error, self.message, self.extra = status, error, message, extra or {}


def _consent_route(route: str) -> "tuple[str, str] | None":
    """``(request_id, action)`` for ``/consent/<id>/<action>``, else None."""
    if not route.startswith(_CONSENT_PREFIX):
        return None
    from urllib.parse import unquote

    quoted, sep, action = route[len(_CONSENT_PREFIX):].partition("/")
    rid = unquote(quoted)          # the page sends encodeURIComponent(id); an id carries ':'
    if not sep or _CONSENT_ID.fullmatch(rid) is None or action not in _ACTIONS:
        return None
    return rid, action


def _sign_route(route: str) -> "tuple[str, str] | None":
    """``(request_id, action)`` for ``/sign/<id>/<action>``, else None."""
    if not route.startswith(_SIGN_PREFIX):
        return None
    rid, sep, action = route[len(_SIGN_PREFIX):].partition("/")
    if not sep or re.fullmatch(r"[A-Za-z0-9_-]{8,64}", rid) is None or action not in _SIGN_ACTIONS:
        return None
    return rid, action


def _grant_route(route: str) -> "tuple[str, str] | None":
    """``(grant_id, action)`` for ``/grant/<id>/<action>``, else None (:func:`levain.consent.grant_phone.grant_route`,
    the cockpit's parse too)."""
    from levain.consent.grant_phone import grant_route

    return grant_route(route, _GRANT_PREFIX)


# the phone app's own routes (levain.phone_app), each with the one method it answers
_APP_ROUTES = {"/app/state.json": "GET", "/app/board.json": "GET", "/app/verb": "POST"}
# K8: the cockpit write routes the phone proxies into the laptop's kernel and broker
_COCKPIT_VERB = "/cockpit/verb"
_COCKPIT_PENDING = re.compile(r"/cockpit/pending/(cp[0-9a-f]{32})(?:/(signature|cancel))?")


def _cockpit_route(method: str, route: str) -> "tuple[str, str | None] | None":
    """``(action, pending id)`` for a K8 cockpit route asked with its one method (``verb``; ``read`` a pending by GET;
    ``signature`` or ``cancel`` it by POST), else None."""
    if route == _COCKPIT_VERB:
        return ("verb", None) if method == "POST" else None
    m = _COCKPIT_PENDING.fullmatch(route)
    if m is None:
        return None
    action = m.group(2) or "read"
    return (action, m.group(1)) if (action == "read") == (method in ("GET", "HEAD")) else None


# failed unauthenticated pairing redeems a single process takes per minute. A long code is 128 bits and a short one 40,
# so beside the short code's own lock this is a second bound on guessing and keeps a caller from holding the push
# store's write lock with a stream of guesses
_REDEEMS_PER_MINUTE = 20


def _json_or_none(body: bytes) -> Any:
    try:
        return json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None


def _b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


class _PhoneHandler(GuardedHandler):
    server_version = "levain-phone"

    # -- the gates ---------------------------------------------------------------------------------------

    def _credential(self) -> "tuple[str, str | None] | None":
        """``("run", None)`` for this run's phone token, ``("device", id)`` for a live paired device token
        (:mod:`levain.webpush.devices`), else None. Either is only the transport gate the page needs to load the
        queue and carry a passkey ceremony: the authority for an answer is the passkey's signature, which the queue
        verifies whoever carried it. A device store that cannot be read raises :class:`PhoneError` (503)."""
        supplied = self.headers.get(PHONE_TOKEN_HEADER)
        if supplied is None:
            return None
        if hmac.compare_digest(supplied.encode("utf-8", "replace"), self.server.phone_token.encode("utf-8")):
            return ("run", None)
        device = self.server.device_for(supplied)
        return ("device", device) if device is not None else None

    def _token_ok(self) -> bool:
        return self._credential() is not None

    def _cross_site_read(self) -> bool:
        """The cross-site refusal, less a top-level navigation to a page: the phone link opened from another app
        arrives cross-site. A page carries no data (the token gates every data route), and framing is still refused."""
        if not super()._cross_site_read():
            return False
        static = _STATIC.get(self.path.split("?", 1)[0])
        return not (self.command == "GET" and static is not None and static[1].startswith("text/html")
                    and self.headers.get("Sec-Fetch-Mode") == "navigate"
                    and self.headers.get("Sec-Fetch-Dest") == "document")

    def _json(self, payload: dict[str, Any], status: int = 200, *, head: bool = False) -> None:
        self._send(json.dumps(payload).encode("utf-8"), "application/json; charset=utf-8", status=status, head=head,
                   headers=[("Vary", PHONE_TOKEN_HEADER)], cache_control="private, no-store")

    # -- reads -------------------------------------------------------------------------------------------

    def _route(self, *, head: bool) -> None:
        from levain.webpush.routes import STATIC_PATHS

        if self._refuse_read(head=head):
            return
        route = self.path.split("?", 1)[0]
        static = _STATIC.get(route)
        if static is not None:
            from levain.web_server import load_web_asset

            self._send(load_web_asset(static[0]).encode("utf-8"), static[1], head=head)
            return
        if route in STATIC_PATHS:
            self._app_route("GET", route, b"", head=head)
            return
        if not self.server.request_gate.acquire(blocking=False):
            self._json({"error": "busy", "message": "the phone port is busy; try again"}, 503, head=head)
            return
        try:
            state = _SIGN_STATE.fullmatch(route)
            if route not in ("/consent.json", "/coord.json") and state is None:
                if not self._app_route("GET", route, b"", head=head):
                    self._send(b"not found\n", "text/plain; charset=utf-8", status=404, head=head)
                return
            try:
                ok = self._token_ok()
            except PhoneError as exc:
                self._json({"error": exc.error, "message": exc.message}, exc.status, head=head)
                return
            if not ok:
                self._json({"error": "forbidden", "message": f"this page's token is needed, sent as {PHONE_TOKEN_HEADER}"},
                           403, head=head)
                return
            self._json(self.server.signer.state(state.group(1)) if state is not None
                       else self.server.coord.coord_listing() if route == "/coord.json" else self.server.listing(),
                       head=head)
        except Exception as exc:  # noqa: BLE001 - a queue that cannot be read is an answer, never a dead connection
            self._json({"error": "internal", "message": f"{type(exc).__name__}: {exc}"}, 500, head=head)
        finally:
            self.server.request_gate.release()

    # -- the phone app: web push, the state line and the board (P1-P4) ----------------------------------------

    def _app_route(self, method: str, route: str, body: bytes, *, head: bool = False) -> bool:
        """Serve one phone-app route (:mod:`levain.webpush.routes`, :mod:`levain.phone_app`); False when the path is
        not one of them. Every fault is an answer, never a dead connection."""
        from levain.phone_app import PhoneAppError, transport_of
        from levain.webpush import routes as push_routes

        try:
            cred = None if route in push_routes.STATIC_PATHS else self._credential()
            cockpit = _cockpit_route(method, route)
            if cockpit is not None:
                if cred is None:
                    self._json({"error": "forbidden", "message": f"this page's token is needed, sent as {PHONE_TOKEN_HEADER}"},
                               403, head=head)
                    return True
                action, pid = cockpit
                app = self.server.phone_app()
                transport = transport_of(cred[0], cred[1], run_token=self.server.phone_token)
                if action == "verb":
                    self._json(app.device_verb(_json_or_none(body), transport=transport))
                elif action == "read":
                    assert pid is not None
                    self._json({**app.pending(pid), "rp_id": self.server.rp_id}, head=head)
                elif action == "signature":
                    assert pid is not None
                    self._json(app.pending_signature(pid, _json_or_none(body), page_origin=self.server.page_origin))
                else:
                    assert pid is not None
                    self._json(app.pending_cancel(pid, _json_or_none(body), transport=transport))
                return True
            if route.startswith("/app/"):
                if route not in _APP_ROUTES or _APP_ROUTES[route] != method:
                    return False
                if cred is None:
                    self._json({"error": "forbidden", "message": f"this page's token is needed, sent as {PHONE_TOKEN_HEADER}"},
                               403, head=head)
                    return True
                if route == "/app/state.json":
                    self._json(self.server.phone_app().state(), head=head)
                elif route == "/app/board.json":
                    self._json(self.server.phone_app().board(), head=head)
                else:
                    self._json(self.server.phone_app().fire(_json_or_none(body)))
                return True
            if route == "/webpush/pair" and (cred is None or cred[0] != "run"):
                # a device never mints another device: revoking a phone must be the end of what it holds
                self._json({"error": "forbidden", "message": "pairing a phone takes this run's phone link"}, 403)
                return True
            redeem = method == "POST" and route == "/webpush/pair/redeem"
            if redeem and not self.server.redeem_allowed():
                self._json({"error": "busy", "message": "too many pairing attempts; try again in a minute"}, 429)
                return True
            query = parse_qs(self.path.partition("?")[2])
            got = push_routes.handle(method, route, query, body, queue_dir=self.server.queue_dir,
                                     authorized=cred is not None, device_id=cred[1] if cred else None)
            if redeem and (got is None or got[0] != 200):
                self.server.redeem_failed()
        except PhoneAppError as exc:
            self._json({"error": exc.error, "message": str(exc)}, exc.status, head=head)
            return True
        except PhoneError as exc:
            self._json({"error": exc.error, "message": exc.message}, exc.status, head=head)
            return True
        except Exception as exc:  # noqa: BLE001 - every failure is an answer the page can show
            self._json({"error": "internal", "message": type(exc).__name__}, 500, head=head)
            return True
        if got is None:
            return False
        status, payload, ctype = got
        if ctype.startswith("application/json"):
            self._send(payload, ctype, status=status, head=head, headers=[("Vary", PHONE_TOKEN_HEADER)],
                       cache_control="private, no-store")
        else:
            self._send(payload, ctype, status=status, head=head, cache_control="no-cache")
        return True

    def _app_post(self, route: str) -> bool:
        """A POST to a phone-app route: the same origin, JSON and size gates as the consent routes, then
        :meth:`_app_route`. False when the path is not one of them (nothing of the body is read)."""
        if not (route.startswith("/webpush/") or route in _APP_ROUTES or _cockpit_route("POST", route) is not None):
            return False
        if self._refuse_non_json():
            return True
        clen = self._declared_length()
        if clen is None or self._refuse_oversize(clen, _MAX_BODY, _MAX_BODY * 4):
            return True
        body = self.rfile.read(clen)
        if _cockpit_route("POST", route) is not None:
            return self._cockpit_write(route, body)
        if not self.server.request_gate.acquire(blocking=False):
            self._json({"error": "busy", "message": "the phone port is busy; try again"}, 503)
            return True
        try:
            return self._app_route("POST", route, body)
        finally:
            self.server.request_gate.release()

    def _cockpit_write(self, route: str, body: bytes) -> bool:
        """A cockpit write on the write gate (``_WRITE_WAIT``): it takes no read slot, so the board and pending reads
        answer while a signature's ssh-keygen runs; a signature carries a passkey ceremony the person just made, so a
        writer waits a bounded moment for the gate rather than throw it away, then hears busy."""
        busy = {"error": "busy", "message": "the phone port is busy with another write; try again"}
        if not self.server.write_waiters.acquire(blocking=False):
            self._json(busy, 503)
            return True
        try:
            if not self.server.write_gate.acquire(timeout=_WRITE_WAIT):
                self._json(busy, 503)
                return True
            try:
                return self._app_route("POST", route, body)
            finally:
                self.server.write_gate.release()
        finally:
            self.server.write_waiters.release()

    # -- writes ------------------------------------------------------------------------------------------

    def do_POST(self) -> None:  # noqa: N802 — BaseHTTPRequestHandler contract
        if self._refuse_write_origin():
            return
        route = self.path.split("?", 1)[0]
        consent = _consent_route(route)
        sign = _sign_route(route)
        question = _QUESTION_ROUTE.fullmatch(route)
        note = _NOTE_ROUTE.fullmatch(route)
        dispatch = _DISPATCH_ROUTE.fullmatch(route)
        coord = _COORD_ROUTE.fullmatch(route)
        coord_review = _COORD_REVIEW_ROUTE.fullmatch(route)
        grant = _grant_route(route)
        if (consent is None and sign is None and question is None and note is None and dispatch is None
                and coord is None and coord_review is None and grant is None and route != _KEY_LINE):
            if not self._app_post(route):
                self._reject(404, "not_found", "no such route")
            return
        try:
            cred = self._credential()          # resolved once: the coordinator routes are handed the device it names
        except PhoneError as exc:
            self._json({"error": exc.error, "message": exc.message}, exc.status)
            return
        if cred is None:
            # refused before a byte of the body is read
            self._reject(403, "credential_required", f"this page's token is needed, sent as {PHONE_TOKEN_HEADER}")
            return
        if coord is not None and cred[0] != "device":
            # the coordinator store takes drops from a paired device only, never this run's transient phone token
            self._reject(403, "device_required", "a drop is sent from a paired device; this run's phone link opens "
                                                 "the queue only")
            return
        if coord_review is not None and cred[0] != "device":
            self._reject(403, "device_required", "a review item is cleared from a paired device; this run's phone "
                                                 "link opens the queue only")
            return
        if self._refuse_non_json():
            return
        clen = self._declared_length()
        if clen is None or self._refuse_oversize(clen, _MAX_BODY, _MAX_BODY * 4):
            return
        try:
            req = json.loads(self.rfile.read(clen).decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            self._json({"error": "bad_request", "message": "the body is not JSON"}, 400)
            return
        # an answer carries a signature the person just made with a passkey: it waits a moment for a slot rather than
        # throw that ceremony away; everything else answers busy at once
        waits = ((consent is not None and consent[1] in ("answer", "rule")) or (sign is not None and sign[1] == "answer")
                 or (question is not None and question.group(2) == "rule")
                 or (dispatch is not None and dispatch.group(2) in ("answer", "cancel", "authorise"))
                 # a drop waits too: the outbox resends it anyway, but one carrying a signed envelope holds a ceremony
                 or (coord is not None and coord.group(1) == "drop")
                 or (grant is not None and grant[1] == "revoke")
                 # a signed review clear carries a signature too
                 or (coord_review is not None and isinstance(req, dict) and "sign_request" in req))
        if not (self.server.request_gate.acquire(timeout=5) if waits else self.server.request_gate.acquire(blocking=False)):
            self._json({"error": "busy", "message": "the phone port is busy; try again"}, 503)
            return
        try:
            if consent is not None:
                result = self.server.answer_step(consent[0], consent[1], req)
            elif sign is not None:
                result = self.server.sign_step(sign[0], sign[1], req)
            elif dispatch is not None:
                result = self.server.dispatch_step(dispatch.group(1), dispatch.group(2), req)
            elif coord is not None:
                result = self.server.coord_step(coord.group(1), req, device=cred[1])
            elif coord_review is not None:
                result = self.server.coord_review(coord_review.group(1), coord_review.group(2), req, device=cred[1])
            elif grant is not None:
                result = self.server.grant_step(grant[0], grant[1], req)
            elif question is not None and question.group(2) == "rule":
                result = self.server.question_rule(int(question.group(1)), req)
            elif question is not None:
                result = self.server.question_answer(int(question.group(1)), req)
            elif note is not None:
                result = self.server.note_dismiss(int(note.group(1)), req)
            else:
                result = self.server.key_line(req)
        except PhoneError as exc:
            self._json({"error": exc.error, "message": exc.message, **exc.extra}, exc.status)
            return
        except Exception as exc:  # noqa: BLE001 - every failure is an answer the page can show
            self._json({"error": "internal", "message": f"{type(exc).__name__}: {exc}"}, 500)
            return
        finally:
            self.server.request_gate.release()
        # a ruling's delivery runs on its own thread: wait for it here, the request slot already released
        waiter = result.pop(_AWAIT, None) if isinstance(result, dict) else None
        if waiter is not None:
            result["delivery"] = waiter(_DELIVERY_WAIT)
        self._json(result)


class _PhoneServer(ThreadingHTTPServer):
    request_queue_size = LISTEN_BACKLOG   # the stdlib's 5 resets a page load's burst on macOS (http_guards)
    daemon_threads = True

    def __init__(self, address: tuple[str, int], *, queue_dir: Path, rp_id: str, token: str,
                 sign_namespaces: "tuple[str, ...]" = (), dispatch_dir: Path) -> None:
        from levain.authority import ReplayCache
        from levain.consent.sign import Signer

        signer = Signer(queue_dir, sign_namespaces, rp_id, self._sign_keys)    # refuses a bad namespace before binding

        super().__init__(address, _PhoneHandler)
        self.queue_dir = queue_dir
        self.rp_id = rp_id
        self.phone_token = token
        self.allowed_hosts = _LOOPBACK_HOSTS | {str(self.server_address[0]).lower(), rp_id}
        self.request_gate = threading.BoundedSemaphore(_MAX_INFLIGHT)
        # the cockpit writes' own gate (one at a time) and the bound on writers holding or waiting for it (K8a K-3)
        self.write_gate = threading.BoundedSemaphore(1)
        self.write_waiters = threading.BoundedSemaphore(_MAX_INFLIGHT)
        # one replay cache for every request this server answers: a replay re-verifies only the links past what it
        # already holds, so a page that asks again costs one ssh-keygen run per new link, not one per link
        self.replay_cache = ReplayCache()
        # on localhost a passkey's page must be this port's own origin exactly: another program on this machine can
        # serve a page on localhost under another port (on an https name the proxy's origin carries no port)
        self.page_origin = f"http://localhost:{self.server_address[1]}" if rp_id == "localhost" else None
        signer.page_origin = self.page_origin
        signer.pin_page_origin(lambda _origin: None)     # this port's own page: no caller can move it afterwards
        self.signer = signer
        from levain.dispatch.phone import DispatchPhone

        self.dispatch_dir = dispatch_dir
        self.dispatch = DispatchPhone(dispatch_dir, rp_id, self.page_origin, self._sign_keys, self._sign_authority)
        from levain.consent.grant_phone import GrantPhone

        self.grants = GrantPhone(queue_dir, rp_id, self.page_origin, self._sign_keys, self._sign_root)
        from levain.coordinator.phone import CoordPhone

        self.coord = CoordPhone(dispatch_dir, rp_id, self.page_origin, self._sign_keys)
        self._phone_app: Any = None
        self._app_lock = threading.Lock()
        self._redeems: list[float] = []
        self._push_store: Any = None

    def phone_app(self) -> Any:
        """The state line and board over the install this queue's consent hook names (built on first use)."""
        from levain.phone_app import PhoneApp

        with self._app_lock:
            if self._phone_app is None:
                self._phone_app = PhoneApp(self.queue_dir, dispatch_dir=self.dispatch_dir)
            return self._phone_app

    def _push(self) -> Any:
        from levain.webpush.store import PushStore

        with self._app_lock:
            if self._push_store is None:
                self._push_store = PushStore(self.queue_dir)
            return self._push_store

    def device_live(self, device_id: str) -> bool:
        """Whether ``device_id`` is still a paired, unrevoked device: asked again at a review write, inside the
        coordinator store's write transaction, and immediately before a sign request is filed (a revoke that landed
        after the request's token was checked refuses the write). A store that cannot be read raises :class:`levain.writes.EditError` (503)."""
        from levain.webpush import devices
        from levain.writes import EditError

        store = self._push()
        try:
            try:
                store.path.lstat()
            except FileNotFoundError:
                return False
            conn = store.connection()
            try:
                return any(d["id"] == device_id and d["revoked_at"] is None for d in devices.list_devices(conn))
            finally:
                conn.close()
        except Exception as exc:  # noqa: BLE001 - every store fault is the same answer: not known now
            raise EditError("source_unavailable", 503, f"the paired-device store cannot be read now "
                            f"({type(exc).__name__}); try again") from None

    def device_for(self, token: str) -> "str | None":
        """The paired device a token belongs to, or None for a token no live device holds. No push store yet means no
        device was ever paired, and none is created here. A store that cannot be read raises :class:`PhoneError`
        (503), so a paired phone hears "try again", never "unpaired"."""
        from levain.webpush import devices

        store = self._push()
        try:
            try:
                store.path.lstat()
            except FileNotFoundError:
                return None
            conn = store.connection()
            try:
                return devices.verify(conn, token, datetime.now(timezone.utc))
            finally:
                conn.close()
        except Exception as exc:  # noqa: BLE001 - every store fault is the same answer to the page
            raise PhoneError(503, "unavailable", f"the paired-device store cannot be read now ({type(exc).__name__}); "
                             "try again") from None

    def _recent_redeems(self) -> list[float]:
        import time

        now = time.monotonic()
        self._redeems = [t for t in self._redeems if now - t < 60.0]
        return self._redeems

    def redeem_allowed(self) -> bool:
        """Whether a pairing redeem may try now: only failed redeems count against the minute's allowance."""
        with self._app_lock:
            return len(self._recent_redeems()) < _REDEEMS_PER_MINUTE

    def redeem_failed(self) -> None:
        import time

        with self._app_lock:
            self._recent_redeems().append(time.monotonic())

    # -- what the routes do (here, not on the handler, so tests can drive them without a socket) --------------

    def _source(self, root: Path) -> Any:
        from levain.authority import anchor_path
        from levain.consent.queue import authority_source

        return authority_source(root / "cockpit.db", anchor=anchor_path(root), cache=self.replay_cache)

    def _keys_at(self, root: Path, memo: "dict[Path, list[str] | str]") -> "list[str] | str":
        """The fingerprints of the active passkey keys enrolled on this name in the cockpit at ``root``, or why they
        cannot be read. ``memo`` holds one listing's answers, so a listing replays each cockpit once however many
        requests it shows."""
        from levain.authority import AuthorityError
        from levain.consent.answering import passkey_keys

        if root not in memo:
            try:
                memo[root] = [k.fingerprint for k in passkey_keys(self._source(root)(), self.rp_id)]
            except (AuthorityError, OSError, sqlite3.Error) as exc:
                memo[root] = str(exc)
        return memo[root]

    def _signers(self, request: Any, memo: "dict[Path, list[str] | str]") -> "tuple[list[str], str]":
        """The passkeys enrolled on this name in the cockpit that verifies ``request``'s answer, and a note when
        there are none."""
        from levain.consent.answering import answer_root
        from levain.consent.queue import AnswerRefused

        try:
            root = answer_root(self.queue_dir, request)
        except (AnswerRefused, OSError) as exc:
            return [], str(exc)
        keys = self._keys_at(root, memo)
        if isinstance(keys, str):
            return [], keys
        if not keys:
            return [], f"no passkey is enrolled for {self.rp_id} in {root}"
        return keys, ""

    def listing(self) -> dict[str, Any]:
        """``GET /consent.json``: each pending request as the cockpit's consent body shows it, plus ``signers``; and
        each undecided one with its gap, its ``gap_label`` and whether its display was ``cut``."""
        from levain.consent.listing import UNSHOWN, entry_body, filed_input, read_listing
        from levain.consent.push_gate import PUSH_TIER, PUSH_TOOL
        from levain.consent.queue import gap_label

        listing = read_listing(self.queue_dir, now=datetime.now(timezone.utc))
        memo: dict[Path, list[str] | str] = {}
        rows = []
        for entry in listing.pending:
            body = entry_body(entry)
            body.pop("answer_allow", None)
            body.pop("answer_deny", None)
            body["signers"], body["signer_note"] = self._signers(entry.request, memo)
            rows.append(body)
        questions, questions_overflow, questions_note = self._questions()
        # a request filed for an open question's option (Q-3) is shown inside that question, signed from there
        linked = {row["link"]["request_id"]: row for row in questions if row.get("link")}
        undecided = []
        for entry in listing.undecided:
            body = entry_body(entry)
            body.pop("answer_allow", None)
            body.pop("answer_deny", None)
            body["gap"] = entry.gap
            body["gap_label"] = gap_label(entry.gap, entry.request.agent_session_id)
            # a display cut at its limit: only a deny is signed for it (the page offers no allow)
            body["cut"] = filed_input(entry.request) is UNSHOWN
            body["signers"], body["signer_note"] = self._signers(entry.request, memo)
            row = linked.get(entry.request.id)
            if (row is not None and entry.request.tool_name == PUSH_TOOL and entry.request.tier == PUSH_TIER
                    and (entry.request.agent, entry.request.agent_session_id) == (row["agent"], row["session"])):
                row["link"]["card"] = body          # a link naming any other request leaves it in the plain list
                continue
            undecided.append(body)
        sign_pending, sign_note, sign_overflow = [], "", 0
        if self.signer.namespaces:
            from levain.consent.sign import SignRefused

            try:
                sign_signers = list(self._sign_keys())
            except SignRefused as exc:
                sign_signers, sign_note = [], str(exc)
            if not sign_signers and not sign_note:
                sign_note = f"no passkey is enrolled for {self.rp_id} in the cockpit the consent hook names"
            page = self.signer.page(sign_signers)
            sign_pending, sign_overflow = page["rows"], page["overflow"]
        dispatch_pending, dispatch_note, dispatch_overflow = self.dispatch.pending()
        notes, notes_overflow, notes_note = self._notes()
        return {"rp_id": self.rp_id, "questions": questions, "questions_overflow": questions_overflow,
                "questions_note": questions_note, "notes": notes, "notes_overflow": notes_overflow,
                "notes_note": notes_note,
                "pending": rows, "undecided": undecided, "overflow": listing.overflow,
                "enrolled": self._enrolled(memo), "sign_pending": sign_pending, "sign_note": sign_note,
                "sign_overflow": sign_overflow, "dispatch_pending": dispatch_pending, "dispatch_note": dispatch_note,
                "dispatch_overflow": dispatch_overflow, **self.grants.listing(), **self.coord.listing()}

    def _questions(self) -> "tuple[list[dict[str, Any]], int, str]":
        """The open questions (oldest first, capped like the pending list), how many were left out, and a note when
        the store could not be read (the page says so rather than showing none). A queue with no ``questions.db``
        lists none and creates none."""
        from levain.consent.listing import PENDING_CAP
        from levain.consent.questions import QuestionStore, QuestionStoreError

        try:
            open_ = QuestionStore(self.queue_dir).open_questions()
        except (QuestionStoreError, OSError, sqlite3.Error) as exc:
            return [], 0, f"{type(exc).__name__}: {exc}"
        rows = [{"n": q.n, "id": q.id, "agent": q.agent, "session": q.agent_session_id, "what": q.what,
                 "options": [{"key": k, "label": label} for k, label in q.options], "rec": q.rec, "blocks": q.blocks,
                 "filed_at": q.filed_at,
                 # the push an option leads to; ``card`` (the undecided request, as the undecided list shows it) is
                 # added by the listing only while that request is undecided
                 "link": None if q.link is None else {"option": q.link.option, "kind": q.link.kind,
                                                      "request_id": q.link.request_id, "card": None}}
                for q in open_[:PENDING_CAP]]
        return rows, max(len(open_) - PENDING_CAP, 0), ""

    def _notes(self) -> "tuple[list[dict[str, Any]], int, str]":
        """The open notes (oldest first, capped like the pending list), how many were left out, and a note when the
        store could not be read. A queue with no ``notes.db`` lists none and creates none."""
        from levain.consent.listing import PENDING_CAP
        from levain.consent.notes import NoteStore, NoteStoreError

        try:
            open_, total = NoteStore(self.queue_dir).open_page(PENDING_CAP)   # never more than the page is read
        except (NoteStoreError, OSError, sqlite3.Error) as exc:
            return [], 0, f"{type(exc).__name__}: {exc}"
        rows = [{"n": n.n, "id": n.id, "agent": n.agent, "what": n.what, "detail": n.detail, "filed_at": n.filed_at}
                for n in open_]
        return rows, max(total - len(open_), 0), ""

    def note_dismiss(self, n: int, req: Any) -> dict[str, Any]:
        """``POST /note/<n>/dismiss`` (``{}``): mark the note read, once, channel ``phone``. Unsigned, and it delivers
        NOTHING to the agent: no delivery thread and no ``_AWAIT``, unlike an answer."""
        from levain.cockpit.consent import ConsentError
        from levain.consent.notes import NoteStore, NoteStoreError

        if req != {}:
            raise PhoneError(400, "bad_request", "a dismiss takes an empty object")
        store = NoteStore(self.queue_dir)
        try:
            note = store.get(n)
            if note is None:
                raise PhoneError(404, "not_found", f"no note #{n}")
            if note.dismissed is not None:
                raise PhoneError(409, "conflict", f"note #{n} was already dismissed")
            try:
                store.dismiss(note.id, channel="phone")
            except ConsentError as exc:
                raise PhoneError(409 if "already dismissed" in str(exc) else 422, "dismiss_refused", str(exc)) from None
        except (NoteStoreError, OSError, sqlite3.Error) as exc:
            raise PhoneError(503, "unavailable", f"the note store cannot dismiss now: {exc}") from None
        return {"dismissed": note.n}

    def question_answer(self, n: int, req: Any) -> dict[str, Any]:
        """``POST /question/<n>/answer`` ({option} or {text}): record the answer (unsigned, once), then deliver it
        to the agent's session on its own thread. Nothing here authorises anything."""
        from levain.cockpit.consent import ConsentError
        from levain.consent.questions import QuestionStore, QuestionStoreError

        if (not isinstance(req, dict) or len(req) != 1 or not set(req) <= {"option", "text"}
                or not isinstance(next(iter(req.values())), str)):
            raise PhoneError(400, "bad_request", 'send exactly one of {"option": key} or {"text": words}')
        store = QuestionStore(self.queue_dir)
        try:
            q = store.get(n)
            if q is None:
                raise PhoneError(404, "not_found", f"no question #{n}")
            if q.answer is not None:
                raise PhoneError(409, "conflict", f"question #{n} was already answered")
            try:
                store.answer(q.id, channel="phone", **req)
            except ConsentError as exc:
                raise PhoneError(409 if "already answered" in str(exc) else 422, "answer_refused", str(exc)) from None
        except (QuestionStoreError, OSError, sqlite3.Error) as exc:
            raise PhoneError(503, "unavailable", f"the question store cannot answer now: {exc}") from None
        return {"answered": q.n, "delivery": {"route": "pending", "reason": "delivery is running"},
                _AWAIT: self._deliver_answer_later(store, q.id)}

    def question_rule(self, n: int, req: Any) -> dict[str, Any]:
        """``POST /question/<n>/rule`` ({option, payload, authenticator_data, client_data_json, signature}): the ONE
        tap on an option tied to a push (Q-3). Two records, in this order: the late ruling ALLOW signed on the linked
        consent request, applied by exactly :meth:`rule_step` (the queue verifies it as any ruling), and only once it
        verified, the answer (unsigned, words only). The ruling is the only authority here; the answer never is. A
        refused ruling records no answer. One delivery runs, the answer's (a resume of an ended session carries it);
        the ruling's text reaches the session at its next hook (or ``levain consent deliver``): the tap starts one
        resume at most."""
        from levain.authority import AuthorityError, VerifierUnavailable
        from levain.cockpit.consent import ConsentError
        from levain.consent.queue import QueueStoreError
        from levain.consent.questions import QuestionStore, QuestionStoreError

        need = {"option", "payload", "authenticator_data", "client_data_json", "signature"}
        if not isinstance(req, dict) or set(req) != need or not isinstance(req["option"], str):
            raise PhoneError(400, "bad_request", f"request keys must be {sorted(need)}")
        store = QuestionStore(self.queue_dir)
        try:
            q = store.get(n)
        except (QuestionStoreError, OSError, sqlite3.Error) as exc:
            raise PhoneError(503, "unavailable", f"the question store cannot answer now: {exc}") from None
        if q is None:
            raise PhoneError(404, "not_found", f"no question #{n}")
        if q.answer is not None:
            raise PhoneError(409, "conflict", f"question #{n} was already answered")
        link = q.link
        if link is None or req["option"] != link.option:
            raise PhoneError(422, "answer_refused", f"option {req['option'][:20]!r} of question #{n} leads to no push: "
                                                    "answer it without a passkey")
        try:
            matches = self._link_matches(q)
        except (AuthorityError, VerifierUnavailable, QueueStoreError, OSError, sqlite3.Error) as exc:
            raise PhoneError(503, "unavailable", f"the consent queue cannot answer now: {exc}") from None
        if not matches:
            raise PhoneError(422, "answer_refused", f"question #{n}'s link does not name a push of its own session; "
                                                    "rule the request from its own card")
        payload = req["payload"]
        if (not isinstance(payload, dict) or payload.get("request_id") != link.request_id
                or payload.get("queue_id") != link.queue_id or payload.get("decision") != "allow"):
            raise PhoneError(422, "answer_refused", f"the ruling is not an allow of the push question #{n} leads to")
        ruled = self.rule_step(link.request_id, "rule", {k: req[k] for k in need - {"option"}}, deliver=False)
        try:
            store.answer(q.id, option=link.option, channel="phone")
        except (ConsentError, QuestionStoreError, OSError, sqlite3.Error) as exc:
            return {"ruling": ruled, "answered": None,
                    "answer_note": f"the push is allowed (ruled), but the answer was not recorded: {exc}",
                    "delivery": {"route": "hook", "reason": "the ruling reaches the session at its next step"}}
        return {"ruling": ruled, "answered": q.n, "decision": ruled["decision"], "signer_fp": ruled["signer_fp"],
                "delivery": {"route": "pending", "reason": "delivery is running"},
                _AWAIT: self._deliver_answer_later(store, q.id)}

    def _link_matches(self, q: Any, request: Any = None) -> bool:
        """The question's link names a ``levain push`` request at its tier, of the question's own agent and session:
        a link is unsigned (any process of the uid can write one), so it is routing and display only, and one that
        names any other request is never shown inside the question or ruled from it."""
        from levain.consent.push_gate import PUSH_TIER, PUSH_TOOL
        from levain.consent.queue import ConsentQueue, authority_source

        if q.link is None or q.link.kind != "push":
            return False
        if request is None:
            got = ConsentQueue(self.queue_dir, home=Path.home(), authority=authority_source(self.queue_dir, anchor=None)
                               ).get(q.link.request_id)
            request = None if got is None else got[0]
        return (request is not None and request.tool_name == PUSH_TOOL and request.tier == PUSH_TIER
                and (request.agent, request.agent_session_id) == (q.agent, q.agent_session_id))

    def _deliver_answer_later(self, store: Any, question_id: str) -> Any:
        """As :meth:`_deliver_later`, for a question's answer (:func:`levain.consent.band.deliver_answer`)."""
        from levain.consent import band

        box: dict[str, Any] = {}
        done = threading.Event()

        def run() -> None:
            try:
                d = band.deliver_answer(store, question_id)
                box["d"] = {"route": d.route, "reason": d.reason}
            except Exception as exc:  # noqa: BLE001 - the answer stands; `levain question deliver` retries it
                box["d"] = {"route": "failed", "reason": f"delivery raised {type(exc).__name__}; the answer stands: "
                                                         "`levain question deliver` delivers it"}
            finally:
                done.set()

        threading.Thread(target=run, name="levain-answer-delivery", daemon=True).start()

        def wait(timeout: float) -> dict[str, Any]:
            if done.wait(timeout):
                return box["d"]
            return {"route": "pending", "reason": "delivery is still running; the answer is recorded either way "
                                                  "(`levain question list --all` shows it)"}

        return wait

    def _sign_root(self) -> Path:
        """The ONE cockpit the queue's consent hooks name, whose passkeys sign a sign request or a dispatch job
        (:func:`levain.consent.sign.sign_root`)."""
        from levain.consent.sign import sign_root

        return sign_root(self.queue_dir)

    def _sign_authority(self) -> Any:
        """The authority source of :meth:`_sign_root`'s cockpit: what a dispatch signature is verified against."""
        return self._source(self._sign_root())

    def _sign_keys(self) -> "dict[str, Any]":
        """The active passkey keys enrolled on this name in the install's cockpit (the one the queue's consent hook
        verifies against): what may sign a sign request. A laptop key never does. The cockpit's own Signer reads the
        same keys through the same function (:func:`levain.consent.sign.passkey_key_source`)."""
        from levain.consent.sign import passkey_key_source

        return passkey_key_source(self.queue_dir, self.rp_id, cache=self.replay_cache)()

    def sign_step(self, request_id: str, action: str, req: Any) -> dict[str, Any]:
        """``/sign/<id>/challenge``, ``answer`` and ``deny`` (:class:`levain.consent.sign.Signer`)."""
        from levain.consent.sign import SignRefused, error_word

        try:
            return getattr(self.signer, action)(request_id, req)
        except SignRefused as exc:
            raise PhoneError(exc.status, error_word(exc.status), str(exc)) from None

    def grant_step(self, grant_id: str, action: str, req: Any) -> dict[str, Any]:
        """``/grant/<id>/<action>`` (:meth:`levain.consent.grant_phone.GrantPhone.step`)."""
        from levain.consent.grant_phone import GrantRefused

        try:
            return self.grants.step(grant_id, action, req)
        except GrantRefused as exc:
            raise PhoneError(exc.status, exc.error, exc.message) from None

    def dispatch_step(self, job_id: str, action: str, req: Any) -> dict[str, Any]:
        """``/dispatch/<job>/<action>`` (:meth:`levain.dispatch.phone.DispatchPhone.step`)."""
        from levain.dispatch.phone import DispatchRefused

        try:
            return self.dispatch.step(job_id, action, req)
        except DispatchRefused as exc:
            raise PhoneError(exc.status, exc.error, exc.message) from None

    def coord_step(self, action: str, req: Any, *, device: "str | None") -> dict[str, Any]:
        """``/coord/<action>`` (:meth:`levain.coordinator.phone.CoordPhone.step`), for the paired ``device``."""
        from levain.coordinator.phone import CoordRefused

        try:
            return self.coord.step(action, req, device=device)
        except CoordRefused as exc:
            raise PhoneError(exc.status, exc.error, exc.message) from None

    def coord_review(self, item_id: str, action: str, req: Any, *, device: "str | None") -> dict[str, Any]:
        """``/coord/review/<item_id>/(clear|reopen|sign-request)`` for the paired ``device``.

        A plain clear or reopen (body ``{"version": ...}``, the version the Review tab drew) is only a transport into
        the phone app's one kernel write (:meth:`levain.phone_app.PhoneApp.fire`, the verbs ``review_clear`` /
        ``review_reopen``), which reads the row, compares its version, tiers the verb under the install's signed
        standing policy and fires only a direct gesture; the device is asked again inside the store's write
        (:meth:`device_live`). A "yours to clear" item is never cleared there: the kernel offers no plain Clear on
        its row, and the store refuses one (``plain_only``).

        ``sign-request`` (``{}``) files levain's own sign request for the signed clear of a "yours to clear" item
        (:func:`levain.coordinator.review.file_clear_request`; the open one over the item as it is now, while half its
        time is left, is answered again instead of a second filed) and answers its listing row and the passkeys that may
        sign it; ``clear`` with ``{"sign_request": ...}`` clears it on that signature, verified at the store's door
        against the passkeys this port signs with (:func:`levain.coordinator.review.clear_signed`, mode ``signed``).
        The device is asked again (:meth:`device_live`) immediately before the request is filed and inside the
        clear's write, so a device revoked since its token was checked files and clears nothing (403
        ``device_revoked``). Before either, the verb ``review_clear_signed`` is tiered under the signed standing policy of the cockpit those
        passkeys are enrolled in, and refused as the cockpit's kernel refuses it when a policy raised it above C1
        (:func:`levain.cockpit.verbs.refuse_raised_review`); a request is filed only while a passkey is enrolled to sign
        it, and a missing, closed or plain item is its own 404 / 409 before that. A directory holding no coordinator
        store is 404, and nothing is made; a store or sign directory that cannot be read or written now is 503."""
        from levain.authority import AuthorityError, VerifierUnavailable
        from levain.cockpit.coordinator_panels import REVIEW_ID, REVIEW_ROW_PREFIX
        from levain.cockpit.verbs import refuse_raised_review
        from levain.consent.answering import passkey_keys
        from levain.consent.sign import SignRefused, error_word, sign_root
        from levain.coordinator import review
        from levain.coordinator.store import CoordError, CoordStore, StoreAbsent
        from levain.phone_app import PhoneAppError
        from levain.writes import EditError

        if device is None:
            raise PhoneError(403, "device_required", "a review item is cleared from a paired device")
        plain = action in ("clear", "reopen") and isinstance(req, dict) and set(req) == {"version"} \
            and isinstance(req["version"], str)
        signed = action == "clear" and isinstance(req, dict) and set(req) == {"sign_request"}
        filing = action == "sign-request" and req == {}
        if not (plain or signed or filing):
            raise PhoneError(400, "bad_request", "a review clear or reopen carries only the row's 'version' (the one "
                                                 "the queue showed); a signed clear only 'sign_request'; a "
                                                 "sign-request nothing")
        if plain:
            body = {"verb": "review_clear" if action == "clear" else "review_reopen", "panel_id": REVIEW_ID,
                    "row_id": REVIEW_ROW_PREFIX + item_id, "row_version": req["version"], "params": {}}
            try:
                out = self.phone_app().fire(body, paired=lambda: self.device_live(device))
            except PhoneAppError as exc:
                if exc.error == "not_offered":         # the panel is drawn only while the coordinator's tables exist
                    raise PhoneError(404, "not_found", "there is no coordinator review queue here") from None
                raise PhoneError(exc.status, exc.error, str(exc)) from None
            return {"item_id": out["item"], "state": out["review_state"], "mode": "manual"}
        # one snapshot of the signing cockpit's authority log: the standing policy the verb is tiered under, and the
        # passkeys enrolled there (the door re-reads its keys inside the clear)
        try:
            snap = self._source(sign_root(self.queue_dir))()
        except SignRefused as exc:
            raise PhoneError(exc.status, error_word(exc.status), str(exc)) from None
        except (AuthorityError, VerifierUnavailable, OSError, sqlite3.Error) as exc:
            raise PhoneError(503, "unavailable", f"the cockpit's passkeys and standing policy cannot be read now "
                                                 f"({exc}): nothing was filed or cleared") from None
        try:
            refuse_raised_review("review_clear_signed", item_id, snap.policy)
        except EditError as exc:
            raise PhoneError(exc.http_status, exc.code, str(exc)) from None
        store = CoordStore(self.coord.dispatch_dir)

        def still() -> None:
            # the plain path's rule: a device revoked since its token was checked writes nothing
            if not self.device_live(device):
                raise review.SignedClearRefused(403, "device_revoked", "this device is no longer paired, so it "
                                                "files and clears nothing")
        try:
            if filing:
                signers = [k.fingerprint for k in passkey_keys(snap, self.rp_id)]
                if not signers:
                    # the item's own refusal (404 / 409) outranks "enrol a passkey"; the filing below validates again
                    item = review.get_item(store, item_id)
                    if item.state != "open":
                        raise CoordError(f"item {item_id} is already {item.state}")
                    if not item.authority_relevant:
                        raise CoordError(f"item {item_id} is not yours to clear: it is cleared as it is, with no "
                                         "signature")
                    raise PhoneError(422, "no_passkey", f"no passkey is enrolled for {self.rp_id} to sign the clear of "
                                                        f"item {item_id}: enrol one first; nothing was filed")
                # levain's own request over the item as it is now; the page signs it on /sign/<id>/ and then clears
                still()
                rid = review.file_clear_request(store, item_id, queue_dir=self.queue_dir, reuse=True)
                return {"request": self.signer.row(rid, signers), "signers": signers}
            item = review.clear_signed(store, item_id, queue_dir=self.queue_dir, request_id=req["sign_request"],
                                       keys=self._sign_keys, still=still)
        except review.SignedClearRefused as exc:
            raise PhoneError(exc.status, exc.error, str(exc)) from None
        except EditError as exc:                       # the paired-device store cannot be read now (device_live)
            raise PhoneError(exc.http_status, exc.code, str(exc)) from None
        except (review.CoordNotFound, StoreAbsent, FileNotFoundError) as exc:
            raise PhoneError(404, "not_found", str(exc) or "there is no coordinator review queue here") from None
        except SignRefused as exc:
            raise PhoneError(exc.status, error_word(exc.status), str(exc)) from None
        except (CoordError, ValueError) as exc:
            raise PhoneError(409, "refused", str(exc)) from None
        except (OSError, sqlite3.Error) as exc:
            raise PhoneError(503, "source_unavailable", f"the coordinator store cannot be read or written now: {exc}"
                             ) from None
        return {"item_id": item.item_id, "state": item.state, "mode": item.last[1] if item.last else None}

    def _enrolled(self, memo: "dict[Path, list[str] | str]") -> list[str]:
        """The passkey keys enrolled on this name in every cockpit a consent hook names (what the page's enrol
        section reports with nothing pending)."""
        from levain.consent.answering import hook_roots

        found: list[str] = []
        for root in hook_roots(self.queue_dir):
            keys = self._keys_at(root, memo)
            found += [] if isinstance(keys, str) else [fp for fp in keys if fp not in found]
        return found

    def answer_step(self, request_id: str, action: str, req: Any) -> dict[str, Any]:
        """``challenge`` ({decision, signer_fp}) -> the answer record and its WebAuthn challenge; ``answer``
        ({payload, authenticator_data, client_data_json, signature}, base64url) -> the queue's verdict. The
        ``rule-`` pair does the same for a late ruling on an undecided request (:meth:`rule_step`)."""
        if action in ("rule-challenge", "rule"):
            return self.rule_step(request_id, action, req)
        from levain.authority import AuthorityError, VerifierUnavailable
        from levain.cockpit.consent import ConsentError
        from levain.consent.answering import answer_root, passkey_keys, refuse_cut_display
        from levain.consent.passkey import PasskeyError, b64url_decode, challenge, to_sshsig
        from levain.consent.queue import AnswerRefused, ConsentQueue, QueueStoreError, authority_source, class_covers
        from levain.consent.record import DECISIONS, answer_message

        need = {"challenge": {"decision", "signer_fp"},
                "answer": {"payload", "authenticator_data", "client_data_json", "signature"}}[action]
        if not isinstance(req, dict) or set(req) != need:
            raise PhoneError(400, "bad_request", f"request keys must be {sorted(need)}")
        try:
            got = ConsentQueue(self.queue_dir, home=Path.home(),
                               authority=authority_source(self.queue_dir, anchor=None)).get(request_id)
            if got is None:
                raise PhoneError(404, "not_found", "no such consent request")
            request, state = got
            if state != "pending":
                raise PhoneError(409, "conflict", f"request {request_id} is already {state}")
            refuse_cut_display(request, "from a phone")
            source = self._source(answer_root(self.queue_dir, request))
            keys = {k.fingerprint: k for k in passkey_keys(source(), self.rp_id)}
            if action == "challenge":
                decision, fp = req["decision"], req["signer_fp"]
                if decision not in DECISIONS:
                    raise PhoneError(400, "bad_request", f"decision must be one of {list(DECISIONS)}")
                if not isinstance(fp, str) or fp not in keys:
                    raise PhoneError(422, "no_passkey", f"{str(fp)[:80]} is not an active passkey enrolled for "
                                                        f"{self.rp_id}")
                if decision == "allow" and not class_covers(keys[fp].key_class, request.tier):
                    raise PhoneError(422, "answer_refused", f"a {keys[fp].key_class} passkey cannot allow a "
                                                            f"{request.tier} request: allow it from the laptop")
                payload = ConsentQueue(self.queue_dir, home=Path.home(),
                                       authority=source).answer_payload(request_id, decision, fp)
                return {"payload": payload, "rp_id": self.rp_id, "challenge": _b64u(challenge(answer_message(payload)))}
            payload = req["payload"]
            fp = payload.get("signer_fp") if isinstance(payload, dict) else None
            key = keys.get(fp) if isinstance(fp, str) else None
            if key is None:
                raise PhoneError(422, "no_passkey", f"the answer's signer is not an active passkey enrolled for "
                                                    f"{self.rp_id}")
            signature = to_sshsig(key.public_key, b64url_decode(req["authenticator_data"], "authenticator_data"),
                                  b64url_decode(req["client_data_json"], "client_data_json"),
                                  b64url_decode(req["signature"], "signature"), page_origin=self.page_origin)
            decided = ConsentQueue(self.queue_dir, home=Path.home(),
                                   authority=source).answer(request_id, payload, signature)
        except (AnswerRefused, PasskeyError, ConsentError) as exc:
            raise PhoneError(422, "answer_refused", str(exc)) from None
        except (AuthorityError, VerifierUnavailable, QueueStoreError, OSError, sqlite3.Error) as exc:
            raise PhoneError(503, "unavailable", f"the consent queue cannot answer now: {exc}") from None
        return {"request_id": decided.request_id, "decision": decided.decision, "signer_fp": decided.signer_fp,
                "decided_at": decided.decided_at}

    def rule_step(self, request_id: str, action: str, req: Any, *, deliver: bool = True) -> dict[str, Any]:
        """``rule-challenge`` ({decision, signer_fp}) -> the ruling record and its WebAuthn challenge, in the ruling
        namespace; ``rule`` ({payload, authenticator_data, client_data_json, signature}) -> the queue's verdict on the
        ruling (:meth:`ConsentQueue.answer_late`: only an undecided request takes one), then its delivery (with
        ``deliver`` false none is started: the ruling's text then reaches the session at its next hook)."""
        from levain.authority import AuthorityError, VerifierUnavailable
        from levain.cockpit.consent import ConsentError
        from levain.consent.answering import answer_root, passkey_keys, refuse_cut_display
        from levain.consent.passkey import PasskeyError, b64url_decode, challenge, to_sshsig
        from levain.consent.queue import AnswerRefused, ConsentQueue, QueueStoreError, authority_source, class_covers
        from levain.consent.record import DECISIONS, RULING_NAMESPACE, ruling_message

        need = {"rule-challenge": {"decision", "signer_fp"},
                "rule": {"payload", "authenticator_data", "client_data_json", "signature"}}[action]
        if not isinstance(req, dict) or set(req) != need:
            raise PhoneError(400, "bad_request", f"request keys must be {sorted(need)}")
        try:
            got = ConsentQueue(self.queue_dir, home=Path.home(),
                               authority=authority_source(self.queue_dir, anchor=None)).get(request_id)
            if got is None:
                raise PhoneError(404, "not_found", "no such consent request")
            request, state = got
            if state != "undecided":
                raise PhoneError(409, "conflict", f"request {request_id} is {state}, not undecided: a ruling answers "
                                                  "only an undecided request")
            decision = req["decision"] if action == "rule-challenge" else (
                req["payload"].get("decision") if isinstance(req["payload"], dict) else None)
            if decision != "deny":
                refuse_cut_display(request, "from a phone")   # a deny is signed even over a cut display
            source = self._source(answer_root(self.queue_dir, request))
            keys = {k.fingerprint: k for k in passkey_keys(source(), self.rp_id)}
            queue = ConsentQueue(self.queue_dir, home=Path.home(), authority=source)
            if action == "rule-challenge":
                decision, fp = req["decision"], req["signer_fp"]
                if decision not in DECISIONS:
                    raise PhoneError(400, "bad_request", f"decision must be one of {list(DECISIONS)}")
                if not isinstance(fp, str) or fp not in keys:
                    raise PhoneError(422, "no_passkey", f"{str(fp)[:80]} is not an active passkey enrolled for "
                                                        f"{self.rp_id}")
                if decision == "allow" and not class_covers(keys[fp].key_class, request.tier):
                    raise PhoneError(422, "answer_refused", f"a {keys[fp].key_class} passkey cannot allow a "
                                                            f"{request.tier} request: rule it from the laptop")
                payload = queue.ruling_payload(request_id, decision, fp)
                return {"payload": payload, "rp_id": self.rp_id,
                        "challenge": _b64u(challenge(ruling_message(payload), RULING_NAMESPACE))}
            payload = req["payload"]
            fp = payload.get("signer_fp") if isinstance(payload, dict) else None
            key = keys.get(fp) if isinstance(fp, str) else None
            if key is None:
                raise PhoneError(422, "no_passkey", f"the ruling's signer is not an active passkey enrolled for "
                                                    f"{self.rp_id}")
            signature = to_sshsig(key.public_key, b64url_decode(req["authenticator_data"], "authenticator_data"),
                                  b64url_decode(req["client_data_json"], "client_data_json"),
                                  b64url_decode(req["signature"], "signature"), namespace=RULING_NAMESPACE,
                                  page_origin=self.page_origin)
            ruled = queue.answer_late(request_id, payload, signature)
        except (AnswerRefused, PasskeyError, ConsentError) as exc:
            raise PhoneError(422, "answer_refused", str(exc)) from None
        except (AuthorityError, VerifierUnavailable, QueueStoreError, OSError, sqlite3.Error) as exc:
            raise PhoneError(503, "unavailable", f"the consent queue cannot answer now: {exc}") from None
        out = {"request_id": ruled.request_id, "decision": ruled.ruling, "signer_fp": ruled.signer_fp,
               "decided_at": ruled.decided_at, "gap": ruled.gap}
        if not deliver:
            return out
        return {**out, "delivery": {"route": "pending", "reason": "delivery is running"},
                _AWAIT: self._deliver_later(queue, ruled.request_id, request.agent)}

    def _deliver_later(self, queue: Any, request_id: str, agent: str) -> Any:
        """Start the ruling's delivery on its own thread; returns ``wait(timeout)`` -> its route and reason, or that it
        is still running. A delivery that raises is reported ``failed``; the ruling stands either way."""
        from levain.consent.delivery import agent_notify, deliver

        box: dict[str, Any] = {}
        done = threading.Event()

        def run() -> None:
            try:
                d = deliver(queue, request_id, notify=agent_notify(self.queue_dir, agent, request_id))
                box["d"] = {"route": d.route, "reason": d.reason}
            except Exception as exc:  # noqa: BLE001 - the ruling stands; `levain consent deliver` retries it
                box["d"] = {"route": "failed", "reason": f"delivery raised {type(exc).__name__}; the ruling stands: "
                                                         "`levain consent deliver` delivers it"}
            finally:
                done.set()

        threading.Thread(target=run, name="levain-ruling-delivery", daemon=True).start()

        def wait(timeout: float) -> dict[str, Any]:
            if done.wait(timeout):
                return box["d"]
            return {"route": "pending", "reason": "delivery is still running; the ping says where it lands"}

        return wait

    def key_line(self, req: Any) -> dict[str, Any]:
        """``POST /passkey/key-line`` ({spki}: the new credential's ``getPublicKey()``, base64url; optionally
        ``authenticator_data``, its creation's ``getAuthenticatorData()``) -> its OpenSSH public key on this server's
        name, its fingerprint, and the laptop commands that enrol it. Nothing is enrolled or recorded here: a creation
        that attests this key rides the enrol command as ``--attested``, which the laptop's signed enrolment checks
        again and records (:mod:`levain.cockpit.keyfacts`); one that does not is refused (422)."""
        from levain.cockpit import keyfacts
        from levain.cockpit.keys import fingerprint_of
        from levain.consent.answering import hook_roots
        from levain.consent.passkey import PasskeyError, b64url_decode, public_key_line

        if not isinstance(req, dict) or not {"spki"} <= set(req) <= {"spki", "authenticator_data"}:
            raise PhoneError(400, "bad_request", "request keys must be ['spki'], and optionally 'authenticator_data'")
        try:
            line = public_key_line(b64url_decode(req["spki"], "spki"), self.rp_id)
        except PasskeyError as exc:
            raise PhoneError(422, "bad_passkey", str(exc)) from None
        attested = ""
        if "authenticator_data" in req:
            try:
                raw = b64url_decode(req["authenticator_data"], "authenticator_data")
                keyfacts.attested(raw, line)
            except (PasskeyError, keyfacts.KeyFactsError) as exc:
                raise PhoneError(422, "bad_passkey", str(exc)) from None
            # re-encoded from the bytes checked, so the command carries nothing but base64url; joined by "=" since
            # the value starts with "-" for some rp_ids, which argparse would read as an option
            attested = " --attested=" + base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
        # one file per passkey: two devices enrolled on one name must not overwrite each other's file
        name = f"passkey-{self.rp_id.split('.')[0]}-{hashlib.sha256(line.encode('ascii')).hexdigest()[:8]}.pub"
        roots = hook_roots(self.queue_dir)
        enrol = [f"levain cockpit enroll {name} --class display-phone --custody passkey --data-root {shlex.quote(str(r))} "
                 f"--signing-key YOUR_LAPTOP_PRESENCE_KEY{attested}" for r in roots]
        return {"public_key": line, "fingerprint": fingerprint_of(line), "file": name,
                "commands": [f"printf '%s\\n' '{line}' > {name}", *enrol],
                "note": None if roots else ("no consent hook names a cockpit yet (run `levain init` with the consent "
                                            "hook first); then enrol into the cockpit it names")}


def make_phone_server(queue_dir: Path, rp_id: str, *, host: str = "127.0.0.1", port: int = DEFAULT_PHONE_PORT,
                      token: str | None = None, sign_namespaces: "tuple[str, ...]" = (),
                      dispatch_dir: Path | None = None) -> _PhoneServer:
    """A bound (not yet serving) phone port over the consent queue at ``queue_dir``, answering for the https name
    ``rp_id``, and over the dispatch store in ``dispatch_dir`` (default: ``queue_dir`` itself, since the dispatch
    directory IS the consent directory, :func:`levain.dispatch.jobs.default_dispatch_dir`; so the keys an authorise
    card offers and the keys its land verifies come from one directory's consent settings). Loopback only: the proxy
    in front of it is what reaches the phone."""
    from levain.consent.passkey import is_host_name

    if host != "localhost" and not _is_loopback_address(host):
        raise ValueError(f"the phone port binds loopback only (a proxy you run puts it on https), not {host!r}")
    if not is_host_name(rp_id):
        raise ValueError(f"refusing {str(rp_id)[:80]!r}: the https name must be a lowercase DNS name a proxy serves "
                         "this port on (no scheme, port or path, not an address); `localhost` answers from this "
                         "machine's own browser")
    return _PhoneServer((host, port), queue_dir=Path(queue_dir), rp_id=rp_id, token=token or secrets.token_urlsafe(32),
                        sign_namespaces=tuple(sign_namespaces),
                        dispatch_dir=Path(dispatch_dir) if dispatch_dir is not None else Path(queue_dir))


def _is_loopback_address(host: str) -> bool:
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


_TAILSCALE_SOCKETS = (None, "/var/run/tailscale/tailscaled.sock")   # the CLI's default, then the open-source daemon's


def tailscale_name(timeout: float = 5.0) -> "tuple[str | None, str]":
    """``(this machine's Tailscale DNS name, "")``, or ``(None, why not)``: read from ``tailscale status --json``
    (the default socket, then ``/var/run/tailscale/tailscaled.sock``). Nothing is changed in Tailscale."""
    import shutil
    import subprocess

    from levain.consent.passkey import is_host_name
    from levain.launch import child_env

    exe = shutil.which("tailscale")
    if exe is None:
        return None, "no `tailscale` command on PATH; pass --name for any https proxy instead"
    why = "tailscale is not running"
    for sock in _TAILSCALE_SOCKETS:
        argv = [exe, *([] if sock is None else [f"--socket={sock}"]), "status", "--json"]
        try:
            cp = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, env=child_env())
            data = json.loads(cp.stdout) if cp.returncode == 0 else None
        except (OSError, subprocess.TimeoutExpired, ValueError):
            data = None
        if not isinstance(data, dict):
            continue
        if data.get("BackendState") != "Running":
            why = f"tailscale is {data.get('BackendState')!r}, not Running"
            continue
        name = str((data.get("Self") or {}).get("DNSName") or "").rstrip(".").lower()
        if is_host_name(name):
            return name, ""
        why = "tailscale reports no DNS name for this machine (MagicDNS off?)"
    return None, why


def _kept_token(path: Path) -> str:
    """``--token-file``: the token kept in ``path`` (a regular file, never through a symlink), else a new one written
    there (0600, whole, never over a file that appeared meanwhile). :class:`ValueError` for a file holding no
    usable token."""
    from levain.consent.sign import _read_bytes, _write_new

    try:
        st = os.lstat(path)
        if stat.S_ISREG(st.st_mode) and st.st_mode & 0o077:
            raise ValueError(f"--token-file: {path} is readable by others (mode {st.st_mode & 0o777:o}): chmod 600 it")
        token = _read_bytes(path).decode("utf-8").strip()
    except FileNotFoundError:
        token = secrets.token_urlsafe(32)
        _write_new(path, token + "\n")
        return token
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError(f"--token-file: cannot read {path}: {exc}") from None
    if re.fullmatch(r"[A-Za-z0-9_-]{32,128}", token) is None:
        raise ValueError(f"--token-file: {path} holds no token (32-128 of [A-Za-z0-9_-])")
    return token


def run_phone_server(queue_dir: Path, *, name: str | None, tailscale: bool, port: int,
                     sign_namespaces: "tuple[str, ...]" = (), link_file: Path | None = None,
                     token_file: Path | None = None) -> int:
    """``levain consent phone``: serve the phone port until interrupted."""
    if name is None:
        if not tailscale:
            print("name the https name the phone reaches this on: --name NAME, or --tailscale", file=sys.stderr)
            return 2
        name, why = tailscale_name()
        if name is None:
            print(f"--tailscale: {why}", file=sys.stderr)
            return 2
    if link_file is not None and token_file is not None and link_file.resolve() == token_file.resolve():
        print("--link-file and --token-file are one file: the link would overwrite the kept token", file=sys.stderr)
        return 2
    try:
        token = _kept_token(token_file) if token_file is not None else None
        httpd = make_phone_server(queue_dir, name, port=port, token=token, sign_namespaces=sign_namespaces)
    except (ValueError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    bound = httpd.server_address[1]
    print(f"levain phone port on http://127.0.0.1:{bound}/ for https://{name}/ (consent queue {queue_dir})", flush=True)
    print(f"  put it on https: tailscale serve --bg --https=443 http://127.0.0.1:{bound}   (stop: tailscale serve reset)"
          if tailscale else f"  point your https proxy for {name} at http://127.0.0.1:{bound}", flush=True)
    if sign_namespaces:
        from levain.consent.sign import sign_dir

        print(f"  signs requests in {sign_dir(queue_dir)} in namespace(s) {', '.join(sign_namespaces)}", flush=True)
    page = f"http://localhost:{bound}/" if name == "localhost" else f"https://{name}/"
    lives = (f"it is kept in {token_file} across restarts: delete that file to make a new one" if token_file is not None
             else "it dies when this stops")
    if link_file is None:
        print(f"  open on the phone (a secret; {lives}): {page}#phone_token={httpd.phone_token}", flush=True)
    import signal

    def _stop(signum: int, frame: Any) -> None:
        raise KeyboardInterrupt        # a service stop (TERM) unwinds like Ctrl+C, so the link file goes

    # TERM only, and only on the main thread (the only one signal handlers run on); HUP is left as inherited, so a
    # server started under nohup keeps running when its terminal closes. Installed before the link file is written.
    before = (signal.signal(signal.SIGTERM, _stop)
              if link_file is not None and threading.current_thread() is threading.main_thread() else None)
    try:
        if link_file is not None:
            # for a caller that links straight onto one sign request's card (levain.consent.sign.deep_link)
            from levain.consent.sign import _write_over

            try:
                _write_over(link_file, json.dumps({"page": page, "phone_token": httpd.phone_token,
                                                   "pid": os.getpid()}) + "\n")
            except OSError as exc:
                print(f"--link-file: cannot write {link_file}: {exc.strerror or exc}", file=sys.stderr)
                return 1
            # stdout is a log under the LaunchAgent: the link (token included) goes only to the 0600 link file, and
            # this line is printed only once that file holds it
            print(f"  the phone link (a secret; {lives}) is in {link_file}: open {page}#phone_token=<its phone_token>",
                  flush=True)
        ticking = threading.Event()
        threading.Thread(target=held_tick, args=(queue_dir, ticking), daemon=True).start()
        try:
            httpd.serve_forever()
        finally:
            ticking.set()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
        if link_file is not None:
            _drop_link_file(link_file)
        if before is not None:
            signal.signal(signal.SIGTERM, before)
    return 0


def _drop_link_file(path: Path) -> None:
    """``--link-file`` removed when this run stops, so no caller builds a link to a server that is gone; left alone
    when it names another run (a later server wrote it). A kill -9 or a crash leaves it: a caller checks its pid."""
    try:
        if json.loads(path.read_text(encoding="utf-8")).get("pid") == os.getpid():
            path.unlink()
    except (OSError, ValueError, AttributeError):
        pass


# -- ONESURFACE OS-4: the old phone origin as a redirect only ---------------------------------------------------------

_REDIRECT_PAGES = frozenset({"/", "/phone.html"})
_REDIRECT_SCRIPT = "/phone_redirect.js"


def redirect_origin(text: object) -> str:
    """``--redirect-to``: the cockpit's bare origin by :func:`levain.consent.sign.cockpit_origin`'s one rule (the
    rule sign links are built by). The page sends every link it is opened with there, so it is configured whole,
    never read from a link."""
    from levain.consent.sign import cockpit_origin

    try:
        return cockpit_origin(text)
    except ValueError:
        raise ValueError(f"--redirect-to: {str(text)[:80]!r} is not the cockpit's origin (https://<name>[:<port>], "
                         "nothing after it)") from None


def _redirect_page(origin: str) -> str:
    """The page the old phone link opens: no data, one script, the target in a meta the script reads."""
    import html

    esc = html.escape(origin, quote=True)
    return ('<!doctype html>\n<html lang="en"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1">'
            '<meta name="referrer" content="no-referrer">'
            f'<meta name="levain-redirect-to" content="{esc}">'
            f'<title>levain</title><script src="{_REDIRECT_SCRIPT}"></script></head>'
            f'<body><p>This page moved to the cockpit.</p><p><a href="{esc}/">Open the cockpit</a></p></body></html>\n')


class _RedirectHandler(GuardedHandler):
    """``/`` and ``/phone.html`` (the page), ``/phone_redirect.js`` (its script); 404 for everything else, and no
    credential of any kind is read or minted."""

    server_version = "levain-phone"

    def _cross_site_read(self) -> bool:
        """The cross-site refusal, less a top-level navigation to the page: the old link opened from another app
        arrives cross-site. Its script and any fetch of the page stay refused."""
        if not super()._cross_site_read():
            return False
        return not (self.command == "GET" and self.path.split("?", 1)[0] in _REDIRECT_PAGES
                    and self.headers.get("Sec-Fetch-Mode") == "navigate"
                    and self.headers.get("Sec-Fetch-Dest") == "document")

    def _route(self, *, head: bool) -> None:
        if self._refuse_read(head=head):
            return
        route = self.path.split("?", 1)[0]
        no_referrer = [("Referrer-Policy", "no-referrer")]
        if route in _REDIRECT_PAGES:
            self._send(self.server.page, "text/html; charset=utf-8", head=head, headers=no_referrer)
        elif route == _REDIRECT_SCRIPT:
            self._send(self.server.script, "text/javascript; charset=utf-8", head=head, headers=no_referrer)
        else:
            self._send(b"not found: this port only opens the cockpit\n", "text/plain; charset=utf-8", status=404,
                       head=head)

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler contract
        self._reject(404, "not_found", "this port only opens the cockpit")


class _RedirectServer(ThreadingHTTPServer):
    request_queue_size = LISTEN_BACKLOG
    daemon_threads = True

    def __init__(self, address: tuple[str, int], *, name: str, origin: str) -> None:
        from levain.web_server import load_web_asset

        # both built before binding: a fault leaves no socket
        script = load_web_asset("phone_redirect.js").encode("utf-8")
        page = _redirect_page(origin).encode("utf-8")
        super().__init__(address, _RedirectHandler)
        self.allowed_hosts = _LOOPBACK_HOSTS | {str(self.server_address[0]).lower(), name}
        self.origin = origin
        self.page = page
        self.script = script


def make_redirect_server(name: str, redirect_to: str, *, host: str = "127.0.0.1",
                         port: int = DEFAULT_PHONE_PORT) -> _RedirectServer:
    """A bound (not yet serving) redirect-only phone port for the https name ``name``, sending every old phone link to
    the cockpit at ``redirect_to`` (:func:`redirect_origin`). Loopback only, as the full port."""
    from levain.consent.passkey import is_host_name

    origin = redirect_origin(redirect_to)
    if host != "localhost" and not _is_loopback_address(host):
        raise ValueError(f"the phone port binds loopback only (a proxy you run puts it on https), not {host!r}")
    if not is_host_name(name):
        raise ValueError(f"refusing {str(name)[:80]!r}: the https name must be a lowercase DNS name a proxy serves "
                         "this port on (no scheme, port or path, not an address)")
    return _RedirectServer((host, port), name=name, origin=origin)


def run_phone_redirect(redirect_to: str, *, name: str | None, tailscale: bool, port: int,
                       link_file: Path | None = None) -> int:
    """``levain consent phone --redirect-to <origin>``: serve only the redirect page until interrupted. With
    ``link_file``, that file (0600) holds ``{"page": "<origin>/", "pid"}``, no token, and is removed when this run
    stops."""
    if name is None:
        if not tailscale:
            print("name the https name the old phone links name: --name NAME, or --tailscale", file=sys.stderr)
            return 2
        name, why = tailscale_name()
        if name is None:
            print(f"--tailscale: {why}", file=sys.stderr)
            return 2
    try:
        httpd = make_redirect_server(name, redirect_to, port=port)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except OSError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    bound = httpd.server_address[1]
    print(f"levain phone port (redirect only) on http://127.0.0.1:{bound}/ for https://{name}/: every old phone "
          f"link opens on {httpd.origin}/", flush=True)
    import signal

    def _stop(signum: int, frame: Any) -> None:
        raise KeyboardInterrupt

    before = (signal.signal(signal.SIGTERM, _stop)
              if link_file is not None and threading.current_thread() is threading.main_thread() else None)
    try:
        if link_file is not None:
            from levain.consent.sign import _write_over

            try:
                _write_over(link_file, json.dumps({"page": httpd.origin + "/", "pid": os.getpid()}) + "\n")
            except OSError as exc:
                print(f"--link-file: cannot write {link_file}: {exc.strerror or exc}", file=sys.stderr)
                return 1
            print(f"  the sign links' origin is in {link_file}", flush=True)
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
        if link_file is not None:
            _drop_link_file(link_file)
        if before is not None:
            signal.signal(signal.SIGTERM, before)
    return 0

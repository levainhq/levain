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
- **write auth (Slice 2a, then K2a)** — Slice 2a made the loopback bind the write
  auth, with no token. K2a (design §4.3) reversed that: 127.0.0.1 is reachable by every
  local uid, so every write carries a per-surface write token on every bind
  (``_WRITE_TOKEN_HEADER``; a writable serve with none given mints a per-launch one). The
  ``/chat`` routes take their own per-launch token (``_CHAT_TOKEN_HEADER``; spore-1310).
  Reads on a loopback bind stay token-free unless the request came through a proxy (an
  ``extra_hosts`` Host or a proxy header: then it is off-box and every route needs the token). Around the token, a write still passes the
  Host allowlist and two CSRF layers: (1) ``Sec-Fetch-Site`` must be absent (a non-browser client like the
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
import hashlib
import hmac
import re
import urllib.parse
import secrets
import select
import time
import socket
import ipaddress
import logging
import json
import sys
import threading
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterable

from levain.cockpit.broker import Broker, BrokerError, Lifecycle
from levain.cockpit.firejournal import OFFER_TTL, PAIRED_PREFIX, DeviceRevoked, PairingError
from levain.cockpit.anneal_effects import (
    EPISODES,
    SECTIONS,
    EpisodeEffectStore,
    SectionEffectStore,
    section_paths_agree,
)
from levain.cockpit.effects import SPORES, DownstreamEffect, SporeEffectStore
from levain.cockpit.engine import Cockpit
from levain.cockpit.external import ExternalPanels, discoverer
from levain.cockpit.routes import CACHE_CONTROL as COCKPIT_CACHE_CONTROL
from levain.cockpit.routes import VARY as COCKPIT_VARY
from levain.cockpit.routes import handle_get as handle_cockpit_get
from levain.cockpit.routes import EVENTS_PATH, is_cockpit_path
from levain.cockpit import stream as cockpit_stream
from levain.cockpit import board as cockpit_board
from levain.cockpit import threads as cockpit_threads
from levain.cockpit.verbs import (SERVED_VERBS_NAME, VerbRegistry, VerbSpec, VerbView, dispatch as dispatch_verb,
                                  record_served_verbs, retier)

from levain.chat import DEFAULT_TURN_SECONDS, ChatError, ChatHost, chat_refusal
from levain.consent.grant_phone import grant_route
from levain.dashboard import SubstrateSource, _resolve_source, recall_episode_rows
from levain.http_guards import _CSP, _WRITE_SEC_FETCH_ALLOWED as _SAME_ORIGIN, LISTEN_BACKLOG, GuardedHandler
from levain.sign_request_id import ID as _SIGN_REQUEST_ID
from levain.http_guards import host_header_allowed  # the off-box read gate; importable here since before 2026-10-03
from levain.jobs import JobRuntime, JobStore, JobStoreCorruptError
from levain.writes import (
    MAX_BODY_BYTES,
    ActionVerb,
    EditError,
)

if TYPE_CHECKING:
    from levain.consent.grant_phone import GrantPhone
    from levain.consent.sign import Signer

# The one write route of the shared cockpit (design §4.1). ``/edit`` and ``/action`` are its aliases:
# all three end in ``levain.cockpit.verbs.dispatch``.
_COCKPIT_VERB_PATH = "/cockpit/verb"
# One agent consent request's body (Q6a): GET /cockpit/consent/<request_id>.json. The decisions panel is
# token-free on loopback, so it carries no request body; this route serves it, and requires a surface token on
# EVERY bind, loopback included, for the reason /chat does (the CHAT factor below): another OS user, a sandboxed
# app or a container can reach loopback, and a Bash command's text is not something redaction masks.
_CONSENT_BODY_PREFIX = "/cockpit/consent/"
_CONSENT_BODY_SUFFIX = ".json"
_CONSENT_ID = re.compile(r"[A-Za-z0-9._:-]{1,160}")
# One agent question's body (Q-2b): GET /cockpit/question/<n>.json. The question's words, its option labels and what
# it blocks are the agent's, as unmaskable as a Bash command's text, so the decisions panel's question row carries
# none of them and this route serves them behind EXACTLY the consent body's gate (a surface token on every bind).
_QUESTION_BODY_PREFIX = "/cockpit/question/"
_QUESTION_ID = re.compile(r"[1-9][0-9]{0,11}")
# One agent note's body: GET /cockpit/note/<n>.json. Its words are the agent's, so the row carries none of them and this
# route serves them behind the question body's gate exactly (a surface token on every bind).
_NOTE_BODY_PREFIX = "/cockpit/note/"
PROPOSALS_ID = "proposals"      # GET /cockpit/consent/proposals.json: the standing-rule proposals, same token gate
GRANTS_ID = "grants"            # GET /cockpit/consent/grants.json: the standing grants and revoke lines, same gate
# One agent session's transcript (Q8): GET /cockpit/session/<stem>.json. It names the session id and full cwd and
# carries the operator's prompts, so it rides the consent body's gate: a surface token on EVERY bind, loopback included.
_SESSION_LOG_PREFIX = "/cockpit/session/"
_SESSION_LOG_SUFFIX = ".json"
# A host session reader's stem (SubstrateSource.session_reader): a shape only, the host judges the id. No "/" by the
# class; the route refuses ".." besides.
_HOST_SESSION_STEM = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")
_WRITE_ROUTES = ("/edit", "/action", _COCKPIT_VERB_PATH)
# The broker's pending routes (K2b design §1, §3.2, §3.5): POST /cockpit/pending/<id>/signature and
# POST /cockpit/pending/<id>/cancel. They ride the same write gate as _WRITE_ROUTES.
_PENDING_PREFIX = "/cockpit/pending/"
_PENDING_ACTIONS = ("signature", "cancel")

# Per-device pairing (PAIR, levain project_memory/1024-18/PAIR_DESIGN.md, rev 6). Route classes (RP-4): the AUTHED
# three need a surface or paired token on EVERY bind, loopback included (a local uid must not mint a device); the
# claim is the one credential-less POST, matched exactly after the query strip, after the Host + CSRF layer 1 check.
_PAIR_OFFER = "/cockpit/pair/offer"
_PAIR_REVOKE = "/cockpit/pair/revoke"
_PAIR_LIST = "/cockpit/pair/devices.json"
_PAIR_CLAIM = "/cockpit/pair/claim"
_PAIR_AUTHED_POST = frozenset({_PAIR_OFFER, _PAIR_REVOKE})
_PAIR_BODY_MAX = 1024             # every pair POST's body cap (the claim's per RP-4; offer and revoke carry less)
_PAIR_CLAIM_SLOTS = 2             # the claim's own semaphore, not the shared request gate (RP-4)
_PAIR_VERIFY_SLOTS = 4            # paired lookups in flight, taken before the broker start / lookup (RT-3)
_PAIR_WAIT_S = 3.0                # how long a paired request waits on a starting broker before its 503 (RT-4, RU-4)
_PAIR_TOKEN_LEN = (16, 128)       # a header outside this length (or the header-safe shape) is never looked up (RT-3)
_UNSET: Any = object()            # the per-request credential cache's empty mark (RS-1)


def _journal_holds_paired_devices(data_root: Path) -> bool:
    """Whether the cockpit journal under ``data_root`` holds any unrevoked paired device, read through a read-only
    connection that never creates the file (the broker is disabled, so nothing else opens it). Unreadable is True:
    an answer the page may drop a good token on is never given on a guess."""
    import sqlite3

    from levain.cockpit.firejournal import DB_NAME

    try:
        path = (Path(data_root) / DB_NAME).resolve()
        if not path.exists():
            return False
    except (OSError, ValueError):
        return True
    try:
        conn = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True, timeout=0.2)
        try:
            row = conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'paired_devices'").fetchone()
            if row is None:
                return False
            return conn.execute("SELECT 1 FROM paired_devices WHERE revoked_at IS NULL LIMIT 1").fetchone() is not None
        finally:
            conn.close()
    except sqlite3.Error:
        return True


class _CredentialUnavailable(Exception):
    """A token header was supplied, the surface pass missed it, and the broker that could verify it as a paired
    device's is in a TRANSIENT state (STANDBY, ACTIVATING, DRAINING) after the bounded wait, or the paired-lookup
    semaphore is full (PAIR RT-1, RT-3, RU-1). Never cached, never folded into a gate's False: the one catch in
    ``_Handler.handle_one_request`` answers 503 ``broker_unavailable``. Every inner broad ``except`` on a dispatch
    path re-raises it first (RU-5)."""


def _pending_route(route: str) -> "tuple[str, str] | None":
    """``(pending_id, action)`` for a broker pending route, else None."""
    if not route.startswith(_PENDING_PREFIX):
        return None
    pid, sep, action = route[len(_PENDING_PREFIX):].partition("/")
    if not sep or not pid or "/" in action or action not in _PENDING_ACTIONS:
        return None
    return pid, action


def _is_write_route(route: str) -> bool:
    return route in _WRITE_ROUTES or _pending_route(route) is not None


# levain's sign-request ceremony hosted by the cockpit (DESIGN_SIGN D4): GET /cockpit/sign.json lists the requests and
# POST /cockpit/sign/<id>/{challenge,answer,deny} runs the caller's levain.consent.sign.Signer, each behind the write
# gate (a surface token on every bind, the Host allowlist, same-origin only, the request gate). Served only when
# make_server was given a Signer: without one every path here is 404.
_SIGN_LIST_PATH = "/cockpit/sign.json"
_SIGN_PREFIX = "/cockpit/sign/"
_SIGN_ACTIONS = ("challenge", "answer", "deny")
# POST /cockpit/sign/compose (DESIGN_SIGN D5, the compose contract): {verb, params, client_txn} -> {request}, the caller's
# compose_signer filing a sign request for a signed verb's params (one per draft while it is open); the page then runs the
# ceremony on that id. Which request each draft filed is recorded durably beside the Signer's requests
# (levain.consent.sign.bind_compose), the record the kernel's sign check reads. Served beside a signer (the
# ceremony): levain files the request for its own kernel signed verbs (verbs.KERNEL_COMPOSERS, c4-mut R1), the caller's
# compose_signer for every other; without a signer it is 404.
_SIGN_COMPOSE = "/cockpit/sign/compose"
# ONESURFACE OS-2, the key-line family: POST /cockpit/passkey/key-line is the phone port's /passkey/key-line (a new
# credential's SPKI -> its OpenSSH line and the laptop commands that enrol it), by the phone port's OWN method, over the
# Signer's queue and rpId, behind the sign routes' gate. Nothing is enrolled or recorded. Served beside a Signer only.
_KEY_LINE_ROUTE = "/cockpit/passkey/key-line"
_COMPOSE_WAIT_S = 30.0        # how long a second compose of one draft waits for the first's compose_signer call
# GET /cockpit/sign/<id>.json: ONE request's listing row, behind the listing's gate, so a page never reads "no longer
# open" off a listing that stopped at its cap (Signer.row)
_SIGN_ONE_SUFFIX = ".json"
# PLAN M17 "9a": the grants' passkey revoke, the phone's own ceremony (levain.consent.grant_phone.GrantPhone, built by
# make_server from the caller's Signer): GET /cockpit/grant/list.json lists each grant with the passkeys that may revoke
# it, and POST /cockpit/grant/<id>/{revoke-challenge,revoke} runs it, behind the sign routes' gate exactly. Served only
# beside a Signer: without one every path here is 404, and the page's revoke button copies the terminal line.
_GRANT_LIST_PATH = "/cockpit/grant/list.json"
_GRANT_PREFIX = "/cockpit/grant/"
# ONESURFACE OS-1: the sign sheet, GET /sign/<id> (a sign request id), a static shell whose one script is /sign.js; what
# it shows and signs are the routes above. Served only beside a Signer. It alone may be framed, and only by its own
# origin (the cockpit's overlay, Q2): the one policy http_guards stamps, with frame-ancestors 'self' in place of 'none'.
_SHEET_PREFIX = "/sign/"
_SHEET_PAGE = "sign.html"
_SHEET_CSP = _CSP.replace("frame-ancestors 'none'", "frame-ancestors 'self'")
if _SHEET_CSP == _CSP:     # pragma: no cover - a reworded policy must not leave the sheet unframable by surprise
    raise ImportError("levain.web_server: the shared CSP names no frame-ancestors 'none' for the sheet to replace")
# The cockpit page that shows the sheet over itself may frame its own origin (default-src 'none' refuses any frame);
# it stays unframable itself.
_COCKPIT_PAGE = "/"
if "frame-src" in _CSP:     # pragma: no cover - the cockpit page's frame-src is appended once, never beside another
    raise ImportError("levain.web_server: the shared CSP already names a frame-src")
_COCKPIT_PAGE_CSP = _CSP + "; frame-src 'self'"


def _sheet_id(path: str) -> "str | None":
    """The request id of ``GET /sign/<id>``, else None."""
    rid = path[len(_SHEET_PREFIX):] if path.startswith(_SHEET_PREFIX) else ""
    return rid if _SIGN_REQUEST_ID.fullmatch(rid) is not None else None


# one extra Host value (make_server extra_hosts): a DNS name or IPv4 literal and an optional trailing dot, never a port
_EXTRA_HOST = re.compile(r"(?=[^:]{1,253}\Z)[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
                         r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*\.?")


def _params_fingerprint(params: "dict[str, Any]") -> str:
    """The fingerprint a compose binds a signed draft's params under, and the sign check compares: the SHA-256 of
    their canonical JSON (sorted keys, no spaces). ``TypeError``/``ValueError`` for params JSON cannot hold."""
    canonical = json.dumps(params, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _sign_route(route: str) -> "tuple[str, str] | None":
    """``(request_id, action)`` for a sign route, else None. The id is passed on as it came (the Signer refuses
    anything that is not a request id)."""
    if not route.startswith(_SIGN_PREFIX):
        return None
    rid, sep, action = route[len(_SIGN_PREFIX):].partition("/")
    if not sep or not rid or action not in _SIGN_ACTIONS:
        return None
    return rid, action


def _sign_one(route: str) -> "str | None":
    """The request id of ``GET /cockpit/sign/<id>.json``, else None (the Signer reads only a request id)."""
    if not route.startswith(_SIGN_PREFIX) or not route.endswith(_SIGN_ONE_SUFFIX):
        return None
    rid = route[len(_SIGN_PREFIX):-len(_SIGN_ONE_SUFFIX)]
    return rid if rid and "/" not in rid else None


# ONESURFACE OS-2, the consent family: a consent request answered with a passkey on the cockpit (a pending one), or ruled
# (an undecided one), through the phone port's OWN step code (ConsentSheet below), over the Signer's queue, rpId and pinned
# page. GET /cockpit/consent/<id>/sheet.json is the sign sheet's read of one request, POST
# /cockpit/consent/<id>/{challenge,answer,rule-challenge,rule} its ceremony, both behind the sign routes' gate exactly, and
# GET /sign/consent/<id> the sheet's page. Served only beside the grant routes (make_server's same-queue condition): the
# card a button sits on and the request it answers come from one queue. Else every path here is 404.
_CONSENT_SIGN_ACTIONS = ("challenge", "answer", "rule-challenge", "rule")
_CONSENT_SHEET_READ = "/sheet.json"
_CONSENT_SHEET_PREFIX = "/sign/consent/"


def _consent_sign_route(route: str) -> "tuple[str, str] | None":
    """``(request_id, action)`` for ``POST /cockpit/consent/<id>/<action>``, else None."""
    if not route.startswith(_CONSENT_BODY_PREFIX):
        return None
    seg, sep, action = route[len(_CONSENT_BODY_PREFIX):].partition("/")
    rid = urllib.parse.unquote(seg)     # the page encodes the id (a ":" in it), as the consent body route reads it
    if not sep or _CONSENT_ID.fullmatch(rid) is None or action not in _CONSENT_SIGN_ACTIONS:
        return None
    return rid, action


def _consent_sheet_read(path: str) -> "str | None":
    """The request id of ``GET /cockpit/consent/<id>/sheet.json``, else None."""
    if not path.startswith(_CONSENT_BODY_PREFIX) or not path.endswith(_CONSENT_SHEET_READ):
        return None
    rid = urllib.parse.unquote(path[len(_CONSENT_BODY_PREFIX):-len(_CONSENT_SHEET_READ)])
    return rid if _CONSENT_ID.fullmatch(rid) is not None else None


def _consent_sheet_id(path: str) -> "str | None":
    """The request id of ``GET /sign/consent/<id>`` (the sheet's page), else None."""
    rid = urllib.parse.unquote(path[len(_CONSENT_SHEET_PREFIX):]) if path.startswith(_CONSENT_SHEET_PREFIX) else ""
    return rid if _CONSENT_ID.fullmatch(rid) is not None else None


class ConsentRefused(Exception):
    """A consent step's refusal: an HTTP status, an error word and the words the page shows (the phone port's)."""

    def __init__(self, status: int, error: str, message: str) -> None:
        super().__init__(message)
        self.status, self.error, self.message = status, error, message


class ConsentSheet:
    """The consent family on the cockpit (OS-2): the phone port's answer and ruling steps
    (:meth:`levain.phone_server._PhoneServer.answer_step`, ``rule_step``, ``_source``, ``_deliver_later``), called on
    this object, so the checks a cockpit answer passes are the phone's own code, not a copy: the request's state, a cut
    display, an active passkey enrolled on ``rp_id`` in the cockpit the request's hook names, the key's class against
    the tier for an allow, the assertion turned into an SSHSIG against ``page_origin``, and the queue's own verification
    (ssh-keygen and the passkey checks it runs at every delivery). ``signer`` supplies only display facts (a credential
    id per key, :meth:`levain.consent.sign.Signer.key_facts`), never who may sign.

    The queue re-verifies a passkey's answer against ``https://<rp_id>`` with no port (``localhost``: any port), never
    the page's origin, so a cockpit pinned elsewhere (``https://<name>:8443``) would have every answer refused after
    the ceremony. There it lists no signer and refuses each step before any ceremony, saying where it can be answered."""

    def __init__(self, queue_dir: Path, rp_id: str, page_origin: "str | None", signer: "Signer") -> None:
        from levain.authority import ReplayCache

        self.queue_dir, self.rp_id, self.page_origin, self.signer = Path(queue_dir), rp_id, page_origin, signer
        self.replay_cache = ReplayCache()      # one per sheet, as the phone port keeps one per server

    # the phone port's steps read these on their `self` -------------------------------------------------------------

    def _source(self, root: Path) -> Any:
        from levain.phone_server import _PhoneServer

        return _PhoneServer._source(self, root)    # type: ignore[arg-type]

    def rule_step(self, request_id: str, action: str, req: Any, *, deliver: bool = True) -> "dict[str, Any]":
        from levain.phone_server import _PhoneServer

        return _PhoneServer.rule_step(self, request_id, action, req, deliver=deliver)    # type: ignore[arg-type]

    def _deliver_later(self, queue: Any, request_id: str, agent: str) -> Any:
        from levain.phone_server import _PhoneServer

        return _PhoneServer._deliver_later(self, queue, request_id, agent)    # type: ignore[arg-type]

    # ----------------------------------------------------------------------------------------------------------------

    def elsewhere(self) -> "str | None":
        """Why no consent answer made on this cockpit's page can verify, or None."""
        if self.rp_id == "localhost" or self.page_origin == f"https://{self.rp_id}":
            return None
        return (f"a consent answer verifies only when signed on https://{self.rp_id} (no port), and this cockpit is "
                f"served on {self.page_origin}: answer it on that page or from the terminal")

    def step(self, request_id: str, action: str, req: Any) -> "dict[str, Any]":
        """``POST /cockpit/consent/<id>/<action>``: the phone port's :meth:`answer_step` reply; a challenge's also
        carries ``line``, the exact text signed (the record's JCS bytes). A ruling is answered as soon as the queue has
        recorded it, with ``delivery`` ``pending``: its delivery runs on its own thread and is never waited on here;
        where it landed is the queue's own record. :class:`ConsentRefused` with the phone port's status, word and words."""
        from levain.phone_server import _AWAIT, PhoneError, _PhoneServer

        problem = self.elsewhere()
        if problem is not None:
            raise ConsentRefused(422, "answer_refused", problem + "; nothing was signed")
        try:
            out = _PhoneServer.answer_step(self, request_id, action, req)    # type: ignore[arg-type]
        except PhoneError as exc:
            raise ConsentRefused(exc.status, exc.error, exc.message) from None
        if out.pop(_AWAIT, None) is not None:       # the delivery thread is started; the ruling stands either way
            out["delivery"] = {"route": "pending", "reason": "its delivery to the session is running"}
        if action in ("challenge", "rule-challenge"):
            from levain.consent.record import answer_message, ruling_message

            encode = answer_message if action == "challenge" else ruling_message
            out["line"] = encode(out["payload"]).decode("utf-8")
        return out

    @staticmethod
    def version(row: "dict[str, Any]", digest: str) -> str:
        """The version a sheet row and a card's link name one request by: over the request's digest and every field
        the row shows as the thing decided (its state, title, requester, message, hold and each option offered whole:
        its key, its shown text and the exact line it signs), so a request whose shown words, offered answers or signed
        lines changed reads as changed."""
        shown = [row["id"], digest, row["state"], row["title"], row["requester"], row["message"], row["expires_at"],
                 [[o["key"], o["text"], o["line"]] for o in row["options"]], row.get("refused"), row.get("note")]
        return hashlib.sha256(json.dumps(shown, ensure_ascii=False, separators=(",", ":")).encode("utf-8")).hexdigest()

    def row(self, request_id: str) -> "dict[str, Any] | None":
        """The sheet's row for one request that is open to a passkey here (pending: an answer; undecided: a ruling),
        else None. ``signers``: the active passkeys enrolled on ``rp_id`` in the cockpit the request's hook names; an
        allow is offered only when one of them may allow the request's tier (``class_covers``, the phone's own check),
        and only over a display shown whole (a deny is still ruled over a cut one, as on the phone). ``sha256``: the
        version the sheet signs on (:meth:`version`), so a request that moved from pending to undecided reads as
        changed. A pending request whose hold has ended is not open here (the queue applies no answer to it)."""
        import sqlite3

        from levain.authority import AuthorityError, VerifierUnavailable
        from levain.cockpit.consent import utc_stamp
        from levain.cockpit.hintview import visible
        from levain.consent.answering import answer_root, passkey_keys
        from levain.consent.listing import UNSHOWN, filed_input
        from levain.consent.queue import AnswerRefused, ConsentQueue, authority_source, class_covers

        got = ConsentQueue(self.queue_dir, home=Path.home(),
                           authority=authority_source(self.queue_dir, anchor=None)).get(request_id)
        if got is None or got[1] not in ("pending", "undecided"):
            return None
        request, state = got
        # the queue's own rule: no answer applies once the hold has ended; inside UNDECIDED_GRACE the request still
        # reads pending (for the hook's undecided mark), but it is not open to an answer, so not to this sheet
        if state == "pending" and request.expires_at <= utc_stamp(datetime.now(timezone.utc)):
            return None
        mode = "answer" if state == "pending" else "rule"
        cut = filed_input(request) is UNSHOWN
        row: dict[str, Any] = {
            "id": request.id, "kind": "consent", "mode": mode, "state": state,
            "title": f"{visible(request.tool_name)} · {request.tier}", "requester": visible(request.agent),
            "message": visible(request.display), "expires_at": request.expires_at if state == "pending" else None,
            "options": [], "signers": [], "recommended": None}
        problem = self.elsewhere()
        try:
            keys = [] if problem is not None else passkey_keys(self._source(answer_root(self.queue_dir, request))(),
                                                               self.rp_id)
        except (AnswerRefused, AuthorityError, VerifierUnavailable, OSError, sqlite3.Error) as exc:
            keys, problem = [], str(exc)
        if problem is None and not keys:
            problem = f"no passkey is enrolled for {self.rp_id} in the cockpit this request's hook names"
        if problem is not None:
            row["note"] = problem
            return self._versioned(row, request)
        verb = "answer" if mode == "answer" else "rule"
        if cut and mode == "answer":
            row["refused"] = ("its input is too long to show here, so it is not signed from this page; the agent's own "
                              "dialog stands")
            return self._versioned(row, request)
        allow = [k.fingerprint for k in keys if class_covers(k.key_class, request.tier)]
        if allow and not cut:
            row["options"].append({"key": "allow", "text": "Allow", "line": f"{verb} allow: request {request.id}"})
        elif not cut:
            row["note"] = (f"a passkey cannot allow a {request.tier} request: allow it from the laptop "
                           f"(levain consent {verb} {request.id} allow --signing-key <laptop key>)")
        row["options"].append({"key": "deny", "text": "Deny", "line": f"{verb} deny: request {request.id}"})
        row["signers"] = [k.fingerprint for k in keys]
        return self._versioned(row, request)

    def _versioned(self, row: "dict[str, Any]", request: Any) -> "dict[str, Any]":
        row["sha256"] = self.version(row, request.digest)
        return row


# ONESURFACE OS-2, the dispatch family: a dispatch job signed, cancelled, or (once recommended) authorised with a passkey on
# the cockpit, through the phone port's OWN class (levain.dispatch.phone.DispatchPhone, the very object phone_server builds),
# over the Signer's queue (the dispatch directory IS the consent directory), rpId, keys and pinned page. GET
# /cockpit/dispatch.json lists the live jobs as sheet rows, GET /cockpit/dispatch/<job>/sheet.json is the sign sheet's read
# of one, POST /cockpit/dispatch/<job>/{challenge,answer,cancel-challenge,cancel,authorise-challenge,authorise} its
# ceremony (the phone's /dispatch/<job>/<action> by name), all behind the sign routes' gate exactly, and GET
# /sign/dispatch/<job> the sheet's page. Served beside a Signer only; else every path here is 404.
_DISPATCH_JOB = re.compile(r"dj-[0-9a-f]{32}")      # phone_server._DISPATCH_ROUTE's job id
_DISPATCH_ACTIONS = ("challenge", "answer", "cancel-challenge", "cancel", "authorise-challenge", "authorise")
_DISPATCH_PREFIX = "/cockpit/dispatch/"
_DISPATCH_LIST = "/cockpit/dispatch.json"
_DISPATCH_SHEET_READ = "/sheet.json"
_DISPATCH_SHEET_PREFIX = "/sign/dispatch/"


def _dispatch_sign_route(route: str) -> "tuple[str, str] | None":
    """``(job_id, action)`` for ``POST /cockpit/dispatch/<job>/<action>``, else None."""
    if not route.startswith(_DISPATCH_PREFIX):
        return None
    job, sep, action = route[len(_DISPATCH_PREFIX):].partition("/")
    if not sep or _DISPATCH_JOB.fullmatch(job) is None or action not in _DISPATCH_ACTIONS:
        return None
    return job, action


def _dispatch_sheet_read(path: str) -> "str | None":
    """The job id of ``GET /cockpit/dispatch/<job>/sheet.json``, else None."""
    if not path.startswith(_DISPATCH_PREFIX) or not path.endswith(_DISPATCH_SHEET_READ):
        return None
    job = path[len(_DISPATCH_PREFIX):-len(_DISPATCH_SHEET_READ)]
    return job if _DISPATCH_JOB.fullmatch(job) is not None else None


def _dispatch_sheet_id(path: str) -> "str | None":
    """The job id of ``GET /sign/dispatch/<job>`` (the sheet's page), else None."""
    job = path[len(_DISPATCH_SHEET_PREFIX):] if path.startswith(_DISPATCH_SHEET_PREFIX) else ""
    return job if _DISPATCH_JOB.fullmatch(job) is not None else None


class DispatchSheet:
    """The dispatch family on the cockpit (OS-2): :class:`levain.dispatch.phone.DispatchPhone` itself (its ``step`` is the
    phone port's ``dispatch_step``), built as phone_server builds it: the dispatch directory is the Signer's queue, the keys
    the Signer's own, the authority the source of the ONE cockpit that queue's hooks name (as ``_PhoneServer._source`` makes
    it), the page origin the one the Signer is pinned to. So the checks a cockpit signature passes are the phone's: the job's
    state, a passkey enrolled on ``rp_id`` whose class covers the tier, the assertion turned into an SSHSIG against
    ``page_origin``, and the job store's own verification against that authority. ``signer`` supplies only display facts.

    The queue's and the store's verification takes a passkey answer made on ``https://<rp_id>`` (no port; ``localhost``: any
    port), never a pinned ``https://<name>:8443``: a cockpit pinned elsewhere lists no signer and refuses each step before
    any ceremony, saying where it can be answered (as the consent family does)."""

    def __init__(self, queue_dir: Path, rp_id: str, page_origin: "str | None", signer: "Signer") -> None:
        from levain.authority import ReplayCache
        from levain.consent.sign import sign_root
        from levain.dispatch.phone import DispatchPhone

        self.queue_dir, self.rp_id, self.page_origin, self.signer = Path(queue_dir), rp_id, page_origin, signer
        self.replay_cache = ReplayCache()
        self.phone = DispatchPhone(self.queue_dir, rp_id, page_origin, signer.keys,
                                   lambda: self._source(sign_root(self.queue_dir)))

    def _source(self, root: Path) -> Any:
        from levain.phone_server import _PhoneServer

        return _PhoneServer._source(self, root)    # type: ignore[arg-type]

    def elsewhere(self) -> "str | None":
        """Why no dispatch signature made on this cockpit's page can verify, or None. No pinned page is checked first:
        ``localhost`` takes any port, never no page at all."""
        if self.page_origin is None:
            return ("a dispatch signature verifies only against the page this cockpit is pinned to, and it is not "
                    "pinned to one: sign it from the terminal (levain dispatch sign)")
        if self.rp_id == "localhost" or self.page_origin == f"https://{self.rp_id}":
            return None
        return (f"a dispatch signature verifies only when signed on https://{self.rp_id} (no port), and this cockpit is "
                f"served on {self.page_origin}: sign it on that page or from the terminal")

    _CHALLENGES = ("challenge", "cancel-challenge", "authorise-challenge")

    def shown(self, job_id: str, action: str, version: str) -> None:
        """Refuse a challenge for job ``job_id`` unless its sheet row's version (the ``sha256`` the sheet read showed) is
        ``version``: :meth:`levain.dispatch.phone.DispatchPhone.shown`, the phone port's own check, over :meth:`row`."""
        self.phone.shown(job_id, action, version, self.row)

    def step(self, job_id: str, action: str, req: Any) -> "dict[str, Any]":
        """``POST /cockpit/dispatch/<job>/<action>``: :meth:`DispatchPhone.step`'s reply (a challenge's also carries
        ``line``, the exact text signed). :class:`levain.dispatch.phone.DispatchRefused` with the phone port's status, word
        and words.

        A challenge (sign, cancel, authorise) also carries ``request_sha256``, the row's ``sha256`` the sheet showed, as a
        plain sign request's does: it is taken off before the phone port's body is checked, and the job's version now is
        compared with it before the payload is built and again after (an amendment while it was built), each a 409 that
        says the job changed (:meth:`shown`). One that names none (a body that is not an object included) is the plain
        route's 409 too, before the phone port is asked anything."""
        from levain.dispatch.phone import DispatchRefused
        from levain.dispatch.sign import sign_message

        problem = self.elsewhere()
        if problem is not None:
            raise DispatchRefused(422, "sign_refused", problem + "; nothing was signed")
        out = self.phone.step_shown(job_id, action, req, self.row)
        if action in self._CHALLENGES:
            out["line"] = sign_message(out["payload"]).decode("utf-8")   # all three sign the payload's canonical JSON
        return out

    @staticmethod
    def version(rec: "Mapping[str, Any]") -> str:
        """The version a sheet row and a card's link name one job by (:meth:`levain.dispatch.phone.DispatchPhone.version`)."""
        from levain.dispatch.phone import DispatchPhone

        return DispatchPhone.version(dict(rec))

    @staticmethod
    def _contract_lines(rec: "Mapping[str, Any]") -> "list[str]":
        """The job as the sheet shows it, every field the phone's card shows (``levain dispatch show``'s): the goal in
        full, each digest-bound contract field, EVERY read and write entry (never cut, never an ellipsis), then the
        job's lease, signer, filer and history. The strings are the record's, already made visible."""
        def q(v: Any) -> str:
            return "?" if v is None or v == "" else str(v)

        def entries(rows: Any) -> "list[str]":
            return [f"  {q(e.get('path'))}  " + ("(new file)" if e.get("mode") == "new"
                                                  else f"{q(e.get('mode'))} {q(e.get('blob'))}")
                    for e in (rows if isinstance(rows, list) else []) if isinstance(e, dict)]

        t = rec.get("template") if isinstance(rec.get("template"), dict) else {}
        b = rec.get("budget") if isinstance(rec.get("budget"), dict) else {}
        listed = lambda v: ", ".join(map(str, v)) if isinstance(v, (list, tuple)) else q(v)  # noqa: E731
        tools = ", ".join(" ".join(map(str, p)) if isinstance(p, (list, tuple)) else str(p)
                          for p in (t.get("tool_versions") or []))
        reads, writes = rec.get("read") or [], rec.get("write") or []
        history = [h for h in (rec.get("history") or []) if isinstance(h, dict)]
        return [str(rec.get("goal", "")), "",
                f"job       {q(rec.get('id'))}  [{q(rec.get('state'))}]",
                f"repo      {q(rec.get('repo'))}",
                f"base      {q(rec.get('base'))} (tree {q(rec.get('base_tree'))})",
                f"digest    {q(rec.get('digest'))}",
                f"template  {q(t.get('id'))}  tier {q(t.get('tier'))}  ({q(t.get('content_hash'))})",
                f"executor  {q(t.get('executor'))}, models {listed(t.get('models'))} (k={q(t.get('k'))}), "
                f"provider {q(t.get('provider'))} via {q(t.get('route'))}",
                f"verifier  {q(t.get('verifier_id'))} ({q(t.get('verifier_kind'))}, {q(t.get('verifier_hash'))})",
                f"tools     {tools or '(none)'}",
                f"budget    {q(b.get('tool_calls'))} tool calls, {q(b.get('tokens'))} tokens, "
                f"{q(b.get('dollars_cents'))} cents (executor)",
                f"grant     {listed(rec.get('grant'))} over the write list",
                f"expires   {q(rec.get('expires_at'))}",
                f"lease     {q(rec.get('lease_end'))}",
                f"policy    {q(rec.get('policy_hash'))}",
                f"signer    {rec.get('signer_fp') or '(not signed)'}",
                f"filed by  {rec.get('agent') or '(no agent named)'}, session {q(rec.get('session'))} "
                f"at {q(rec.get('filed_at'))}",
                f"read ({len(reads)}, what the executor sees and the provider receives):", *entries(reads),
                f"write ({len(writes)}, the only files a result may change):", *entries(writes),
                f"template write paths: {listed(t.get('write_paths'))}",
                "history:", *[f"  {q(h.get('seq'))} {q(h.get('to_state'))} {q(h.get('cause'))} {q(h.get('at'))}"
                              + (f"  {h['evidence']}" if h.get("evidence") else "") for h in history]]

    @staticmethod
    def _change_line(c: "Mapping[str, Any]") -> str:
        """One change of an authorise card: its status, its line counts (a binary or gitlink has none) and its path."""
        a, d = c.get("added"), c.get("deleted")
        counts = f"+{a} -{d}" if isinstance(a, int) and isinstance(d, int) else "no line count"
        flags = "".join(f" ({w})" for w in ("binary", "gitlink") if c.get(w))
        return f"  {c.get('status')}  {counts}  {c.get('path')}{flags}"

    def _row(self, rec: "Mapping[str, Any]", problem: "str | None") -> "dict[str, Any]":
        """One record (:meth:`DispatchPhone.record`) as a sheet row: ``options`` the acts open to a passkey (sign, cancel,
        authorise), each with the ``signers`` that may do THAT act (the sign tier's and the landing tier's differ).

        The message is the whole job (:meth:`_contract_lines`) and, for an authorise card, EVERY change it lands with
        its status and line counts. A card that does not hold every change (``change_overflow``: past the phone's own
        cap) offers NO act: the row says it is reviewed on the laptop (``review``), as a cockpit pinned elsewhere does.
        ``authorise``: the card's run, branch and tier, which the sheet checks the authorise challenge against."""
        from levain.dispatch.sign import FIELD_MAX

        job_id, state = str(rec.get("id")), str(rec.get("state"))
        lines = self._contract_lines(rec)
        card = rec.get("authorise") if isinstance(rec.get("authorise"), dict) else None
        changes = [c for c in (card.get("changes") or []) if isinstance(c, dict)] if card is not None else []
        over = card.get("change_overflow") if card is not None else 0
        over = over if isinstance(over, int) and over > 0 else 0
        if card is not None and card.get("onto"):
            lines += ["", f"Authorise run {card.get('run')} onto {card.get('onto')} · tier {card.get('tier')} · "
                          f"{card.get('files')} file(s), {card.get('changed_lines')} changed line(s)"
                          + (f" · staged {card.get('staged')} on {card.get('base')}" if card.get("staged") else ""),
                      *[self._change_line(c) for c in changes]]
            if over:
                lines.append(f"  ({over} more change{'s are' if over != 1 else ' is'} not listed here)")
        row: dict[str, Any] = {
            "id": job_id, "kind": "dispatch", "mode": "dispatch", "state": state, "sha256": self.version(rec),
            "title": f"dispatch · {state}", "requester": rec.get("agent"), "message": "\n".join(lines),
            "expires_at": rec.get("lease_end") if state == "filed" else None, "options": [], "signers": [],
            "recommended": None, "agent": rec.get("agent"), "goal": str(rec.get("goal", ""))[:300]}
        if card is not None and card.get("onto"):
            row["authorise"] = {"run": card.get("run"), "onto": card.get("onto"), "tier": card.get("tier")}
        if problem is None and over:
            # never signed or authorised from a summary: the set the passkey would land is not all on this page
            row["review"] = True
            problem = (f"this run changes {card.get('files')} files and this page lists {len(changes)} of them, so "
                       f"nothing is signed from this page: review it on the laptop (levain dispatch show {job_id}; "
                       f"levain dispatch authorise {job_id} --run {card.get('run')} --onto {card.get('onto')})")
        if problem is not None:
            row["note"] = problem
            return row
        signers = [fp for fp in rec.get("signers", []) if isinstance(fp, str)]
        notes: list[str] = []
        if rec.get("can_sign"):
            row["options"].append({"key": "sign", "text": "Sign", "signers": signers,
                                   "line": f"sign dispatch job {job_id} (its contract's digest)"})
        elif state == "filed" and signers:
            notes.append("this job's lease has ended: it can no longer be signed")
        if rec.get("can_cancel") and signers:
            row["options"].append({"key": "cancel", "text": "Cancel the job", "signers": signers,
                                   "line": f"cancel dispatch job {job_id}"})
            row["reason"] = {"max": FIELD_MAX}
        if card is not None:
            if card.get("problem"):
                notes.append(str(card["problem"]))
            elif card.get("signers"):
                row["options"].append({"key": "authorise", "signers": [s for s in card["signers"] if isinstance(s, str)],
                                       "text": f"Authorise run {card.get('run')} onto {card.get('onto')}",
                                       "line": f"authorise dispatch job {job_id} run {card.get('run')} onto "
                                               f"{card.get('onto')}"})
            elif card.get("note"):
                notes.append(str(card["note"]))
        if not signers and not row["options"]:
            notes.insert(0, str(rec.get("signer_note") or f"no passkey is enrolled for {self.rp_id}"))
        row["signers"] = sorted({fp for o in row["options"] for fp in o["signers"]})
        if notes:
            row["note"] = "; ".join(notes)
        return row

    def listing(self) -> "tuple[list[dict[str, Any]], str | None, int]":
        """``(rows, note, overflow)``: every live job as a sheet row (:meth:`DispatchPhone.pending`'s listing)."""
        records, note, overflow = self.phone.pending()
        problem = self.elsewhere()
        return [self._row(r, problem) for r in records], note, overflow

    def row(self, job_id: str) -> "dict[str, Any] | None":
        """The sheet's row for one job that is still live, else None (no store, no such job, or a final state)."""
        from levain.cockpit.consent import utc_stamp
        from levain.dispatch import DispatchError
        from levain.dispatch.jobs import TERMINAL

        store = self.phone._store()
        job = None if store is None else store.get(job_id)
        if job is None:
            return None
        # the job's state and its record are read under pending()'s own guard: a job whose stored row cannot be read
        # is a row that offers nothing and says why, never a 500 and never a ceremony
        try:
            if job.state in TERMINAL:
                return None
            signers, note = self.phone._signers()
            rec = self.phone.record(job, utc_stamp(self.phone._clock()), list(signers), note)
        except (DispatchError, ValueError, KeyError, TypeError, IndexError, AttributeError) as exc:
            why = (f"dispatch job {job_id} could not be read ({type(exc).__name__}), so nothing is signed from this page: "
                   f"read it on the laptop (levain dispatch show {job_id})")
            return {"id": job_id, "kind": "dispatch", "mode": "dispatch", "state": "unreadable",
                    "sha256": hashlib.sha256(json.dumps([job_id, "unreadable", type(exc).__name__]).encode()).hexdigest(),
                    "title": "dispatch · could not be read", "requester": None, "message": "", "expires_at": None,
                    "options": [], "signers": [], "recommended": None, "agent": None, "goal": "", "review": True,
                    "note": why}
        return self._row(rec, self.elsewhere())


# ONESURFACE OS-2, the coordinator drop family: a drop (words, optionally with an envelope signed by a passkey) sent from the
# cockpit, through the phone port's OWN class (levain.coordinator.phone.CoordPhone, the very object phone_server builds), over
# the Signer's queue (the dispatch directory IS the consent directory), rpId, keys and pinned page. GET /cockpit/coord/drops.json
# is the sheet's read (the phone's coord_* listing keys), POST /cockpit/coord/{drop,envelope-review,envelope-challenge} the
# phone's /coord/<action> by name, behind the sign routes' gate exactly, and GET /sign/drop the sheet's page, whose envelope
# signing is the sign sheet's own passkey ceremony. Served beside a Signer only; else every path here is 404.
_COORD_ACTIONS = ("drop", "envelope-review", "envelope-challenge")
_COORD_PREFIX = "/cockpit/coord/"
_COORD_LIST = "/cockpit/coord/drops.json"
_COORD_SHEET = "/sign/drop"
# The phone's coordinator routes take a drop from a PAIRED DEVICE only (its token's device id is handed to CoordPhone.step,
# which binds a kept envelope review to it). The cockpit has no device id: its credential is the surface token every sign
# route needs (the same gate as every cockpit passkey ceremony), so this one name stands for "this cockpit's page".
_COORD_DEVICE = "cockpit"


def _coord_route(route: str) -> "str | None":
    """The action of ``POST /cockpit/coord/<action>``, else None."""
    if not route.startswith(_COORD_PREFIX):
        return None
    action = route[len(_COORD_PREFIX):]
    return action if action in _COORD_ACTIONS else None


class CoordSheet:
    """The coordinator drop family on the cockpit (OS-2): :class:`levain.coordinator.phone.CoordPhone` itself, built as
    phone_server builds it over the Signer's own values (the dispatch directory is the Signer's queue, the keys the
    Signer's, the page origin the one the Signer is pinned to). A drop is delivered by the same store call ``levain drop``
    makes; an envelope signature passes the phone's checks (a passkey enrolled on ``rp_id`` whose class covers the
    envelope's floor, the assertion turned into an SSHSIG against ``page_origin``, the store's own verification against the
    authority read right before the write). A step that carries or asks for a signature is refused before any ceremony on a
    page the verification would refuse (as the dispatch family's is); a plain drop signs nothing and is never refused for it."""

    def __init__(self, queue_dir: Path, rp_id: str, page_origin: "str | None", signer: "Signer") -> None:
        from levain.coordinator.phone import CoordPhone

        self.queue_dir, self.rp_id, self.page_origin, self.signer = Path(queue_dir), rp_id, page_origin, signer
        self.phone = CoordPhone(self.queue_dir, rp_id, page_origin, signer.keys)
        self._gate = threading.Condition()     # close(): no new step once closed; the steps already running are waited for
        self._closed = False
        self._running = 0

    def close(self, timeout: float = 30.0) -> None:
        """Shutdown: refuse every new step (so no new wake starts), wait for the steps in flight, then join the wake
        threads they started (:meth:`CoordPhone.wait_wakes`, which the phone port names for a server shutting down)."""
        with self._gate:
            self._closed = True
            end = time.monotonic() + timeout
            while self._running and time.monotonic() < end:
                self._gate.wait(max(0.0, end - time.monotonic()))
        self.phone.wait_wakes(timeout)

    def elsewhere(self) -> "str | None":
        """Why no envelope signature made on this cockpit's page can verify, or None (as :meth:`DispatchSheet.elsewhere`)."""
        if self.page_origin is None:
            return ("an envelope signature verifies only against the page this cockpit is pinned to, and it is not "
                    "pinned to one: sign the envelope from the terminal (levain drop --envelope)")
        if self.rp_id == "localhost" or self.page_origin == f"https://{self.rp_id}":
            return None
        return (f"an envelope signature verifies only when signed on https://{self.rp_id} (no port), and this cockpit is "
                f"served on {self.page_origin}: sign it on that page or from the terminal")

    def step(self, action: str, req: Any) -> "dict[str, Any]":
        """``POST /cockpit/coord/<action>``: :meth:`CoordPhone.step`'s reply (a challenge's also carries ``line``, the exact
        text signed). :class:`levain.coordinator.phone.CoordRefused` with the phone port's status, word and words."""
        from levain.coordinator import envelope as env
        from levain.coordinator.phone import CoordRefused

        signs = action in ("envelope-review", "envelope-challenge") or (
            action == "drop" and isinstance(req, dict) and req.get("envelope") is not None)
        problem = self.elsewhere() if signs else None
        if problem is not None:
            raise CoordRefused(422, "sign_refused", problem + ("; nothing was signed" if action != "drop"
                                                               else "; the drop was not delivered"))
        with self._gate:
            if self._closed:
                raise CoordRefused(503, "closing", "the cockpit is shutting down; nothing was delivered")
            self._running += 1
        try:
            out = self.phone.step(action, req, device=_COORD_DEVICE)
        finally:
            with self._gate:
                self._running -= 1
                self._gate.notify_all()
        if action == "envelope-challenge":
            out["line"] = env.sign_message(out["payload"]).decode("utf-8")
        return out

    def listing(self) -> "dict[str, Any]":
        """The sheet's read: the phone's ``coord_*`` listing keys (the newest phone-surface drops with their receipts, the
        repos an envelope may name, the store identity, who may sign one)."""
        out = self.phone.listing()
        problem = self.elsewhere()
        if problem is not None:
            out["elsewhere"] = problem
        return out


def _extra_hosts(hosts: object) -> frozenset[str]:
    """``extra_hosts`` as the Host gate compares names (:func:`levain.http_guards.host_header_allowed`: lowercase, no
    trailing dot, no port, since the gate compares host names only, for loopback too). ValueError for anything that
    is not a bare ``name``: a port, a scheme, a path, userinfo, a wildcard, whitespace, an IPv6 literal, or a bare
    string where a list belongs (its letters would each become a host). The port is the request's (a proxied Host
    ``name:8443`` is admitted by name); an entry carrying one would read as a pin the gate never enforces."""
    if hosts is None:
        return frozenset()
    if isinstance(hosts, (str, bytes)):
        raise ValueError("refusing extra_hosts: pass a list of host names, not one string")
    try:
        items = list(hosts)        # type: ignore[call-overload]
    except TypeError:
        raise ValueError("refusing extra_hosts: pass a list of host names") from None
    out = set()
    for h in items:
        m = _EXTRA_HOST.fullmatch(h) if isinstance(h, str) else None
        if m is None:
            raise ValueError(f"refusing extra_hosts entry {str(h)[:80]!r}: it must be a bare host name (no port, "
                             "scheme, path, userinfo, wildcard or whitespace)")
        out.add(h.rstrip(".").lower())
    return frozenset(out)


class _ConsentReader:
    """The consent queue's listing reader for the decisions panel (Q6a): ``reader(now)`` over one queue directory,
    read-only; no queue directory means no rows and no note. ``directory`` is explicit (None is the default queue) and
    is what the rows and the proposals route both read; an object offering a reader without the attribute gets no
    proposals from the route, never the default queue's."""

    def __init__(self, directory: Any = None) -> None:
        self.directory = directory

    def __call__(self, now: datetime) -> Any:
        from levain.consent.listing import read_listing

        return read_listing(self.directory, now=now)


def _consent_reader() -> Any:
    return _ConsentReader()


def broker_install_class(httpd: Any) -> str:
    """The install class gestures are decided under, for a render and a dispatch alike: the broker's
    own while it is write-enabled; a broker that is disabled, on standby or stopped (and no broker)
    is "none", so no gesture renders that a write would then refuse."""
    broker = getattr(httpd, "broker", None)
    return (broker.install_class if broker is not None and broker.enabled else None) or "none"

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
# pile up unbounded store reads / file writes. Static assets (cached bytes) bypass the gate.
# Past the cap the POSTs, the substrate read and a consent body get a clean instant 503; the
# cockpit GET reads (below) wait instead. The cap of 8 holds for every gated route together.
_MAX_INFLIGHT = 8
# The cockpit reads also share a smaller pool, so a read burst can never hold every slot:
# this many stay free for the instant-503 routes (a write must not 503 for a whole burst).
_RESERVED_SLOTS = 2
_MAX_READ_INFLIGHT = _MAX_INFLIGHT - _RESERVED_SLOTS

# The cockpit's GET reads (the manifest and one request per panel; a page load bursts ~28 of them, two
# open pages double it) wait this long for a slot before the 503, and at most ``_MAX_READ_WAITERS`` of them
# wait at once, so a flood still cannot pile up threads without bound. Every other gated route (the POSTs,
# the substrate read, a consent body) keeps the instant 503 past the cap, but reads hold at most
# ``_MAX_READ_INFLIGHT`` slots, so those routes always find ``_RESERVED_SLOTS`` free of a read burst.
_READ_WAIT_SECONDS = 5.0
_MAX_READ_WAITERS = 64

_log = logging.getLogger(__name__)


def _store_fault(what: str, exc: BaseException) -> dict[str, str]:
    """The 500 body of an agent-text route whose store could not be read: the exception's type only. Its message is
    logged, never sent: a store error names the store's absolute path."""
    _log.warning("cockpit: a %s body could not be read: %s: %s", what, type(exc).__name__, exc)
    return {"error": "internal", "message": f"the {what} store could not be read ({type(exc).__name__})"}


# The write token header. spore-129 introduced it for an off-loopback writable bind; since K2a
# (design §4.3) every write carries a surface token in it on EVERY bind, loopback included,
# constant-time compared against the surface tokens ``make_server`` holds. Off-box, the same
# token also gates the substrate-bearing reads (spore-220), and every route of a PROXIED request (an extra host or a
# proxy header) whatever the source; loopback reads with no proxy header stay token-free.
_WRITE_TOKEN_HEADER = "X-Levain-Write-Token"
_HEADER_SAFE_TOKEN = re.compile(r"[\x21-\x7e]+")   # printable ASCII, no spaces: it travels in a header

# The CHAT factor (spore-1310, rec B, ruled 2026-10-03). Slice 2a's token-free loopback write
# rested on "anything that can reach loopback can already edit the files" (K2a later dropped it for
# writes too). It always failed for /chat, which makes an entity execute and spend the model endpoint: a container reaching host
# loopback through host.docker.internal, a sandboxed app with a network entitlement, or another OS
# user can reach loopback without being able to edit your files. So every /chat route requires a
# per-launch token, generated at startup, held only in this process and printed once to the
# terminal that started it (never written to a file an entity's floor could read).
_CHAT_TOKEN_HEADER = "X-Levain-Chat-Token"

# Loopback names a request's Host header may legitimately carry. A DNS-rebinding
# page rebinds its OWN name to 127.0.0.1, so its requests still arrive with
# ``Host: evil.com`` — rejecting any non-loopback Host closes that disclosure.
# The actual bound address is added to this set in ``make_server``. (Lowercase —
# the Host check normalizes case before comparing, per RFC 7230 §2.7.3.)
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
# A request carrying one of these came through a proxy (tailscale serve adds X-Forwarded-Host and Tailscale-User-*,
# measured 10-23), so it is off-box whatever its Host says: a tailnet peer can send ``Host: 127.0.0.1`` through one
_PROXY_HEADERS = frozenset({"x-forwarded-for", "x-forwarded-host", "forwarded"})
_PROXY_HEADER_PREFIX = "tailscale-user-"


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
    "/dashboard_chat.js": ("dashboard_chat.js", "text/javascript; charset=utf-8"),
    # The manifest mapper (levain.cockpit; design §7): builds the dashboard's view from
    # /cockpit/manifest.json, so the dashboard page is the cockpit renderer (the default page).
    "/cockpit_view.js": ("cockpit_view.js", "text/javascript; charset=utf-8"),
    # The topology's renderer (F12b): the work view drawn in the Build tab's Sessions card from the topology panel's value.
    "/topology_view.js": ("topology_view.js", "text/javascript; charset=utf-8"),
    # The ambient panel's sky: its own asset so the renderer and the sky evolve apart.
    "/sky.js": ("sky.js", "text/javascript; charset=utf-8"),
    # The change stream's client: one fetch-read stream per page, through the page's own read transport.
    "/cockpit_stream.js": ("cockpit_stream.js", "text/javascript; charset=utf-8"),
    # Per-device pairing's browser half (RP-4: the static-shell exception serves these token-free; the claim script is an
    # external file for the CSP): the new device's page, its script, and the cockpit's Devices panel.
    "/pair": ("pair.html", "text/html; charset=utf-8"),
    "/pair.js": ("pair.js", "text/javascript; charset=utf-8"),
    "/devices_panel.js": ("devices_panel.js", "text/javascript; charset=utf-8"),
    # The sign sheet's script (OS-1): its page is GET /sign/<id>, served by _route beside a Signer.
    "/sign.js": ("sign.js", "text/javascript; charset=utf-8"),
    # The thread view (CH-U1a): the Build tab's Threads zone, over GET /cockpit/threads.json and /cockpit/thread/<id>.json.
    "/thread_view.js": ("thread_view.js", "text/javascript; charset=utf-8"),
    # The read-only ready-queue board (C12): the Board tab's zone, over GET /cockpit/board.json.
    "/board_view.js": ("board_view.js", "text/javascript; charset=utf-8"),
}


# ONESURFACE OS-4: web push on this origin (levain.webpush.routes), served beside a Signer, whose queue holds the push
# store. The worker, manifest and icons are app shell (token-free, no data); the two reads and three writes need a
# token on every bind; the phone port's own pairing (/webpush/pair, /webpush/pair/redeem) is not served here: this
# origin pairs through /pair.
_PUSH_GET = frozenset({"/webpush/key", "/webpush/record"})
_PUSH_POST = frozenset({"/webpush/subscribe", "/webpush/unsubscribe", "/webpush/opened"})
_PUSH_SUBSCRIBE = "/webpush/subscribe"
_PUSH_PREFIX = "/webpush/"


def _push_static() -> frozenset[str]:
    from levain.webpush.routes import STATIC_PATHS

    return STATIC_PATHS


# The chat routes (K1 part 2, `levain serve --chat`). Served only when the server holds a ChatHost;
# reserved either way, so a downstream route can never claim them.
_CHAT_GET_ROUTES = ("/chat.json", "/chat/session.json", "/chat/job.json")
_CHAT_POST_ROUTES = ("/chat/open", "/chat/turn", "/chat/approve", "/chat/reject", "/chat/close")


# Paths a downstream-registered extra route may NEVER shadow: every built-in route this module
# serves (the set below). A control plane (the flow Bridge's FleetView) registers ADDITIONAL
# read-only views; it must not be able to override the dashboard, its reads, the write routes or the
# chat routes. make_server refuses a collision loudly (a packaging-class bug, not runtime).
_RESERVED_PATHS: frozenset[str] = frozenset(_ASSETS) | {
    "/substrate.json",
    "/recall.json",
    "/job.json",
    "/edit",
    "/action",
    "/cockpit/manifest.json", "/cockpit/freshness.json", EVENTS_PATH, _COCKPIT_VERB_PATH, _SIGN_LIST_PATH,
    _GRANT_LIST_PATH, cockpit_threads.THREADS_LIST_PATH, _COORD_LIST, _COORD_SHEET,
    cockpit_board.BOARD_PATH, _DISPATCH_LIST, _KEY_LINE_ROUTE, _PAIR_OFFER, _PAIR_REVOKE, _PAIR_LIST, _PAIR_CLAIM,
} | frozenset(_CHAT_GET_ROUTES) | frozenset(_CHAT_POST_ROUTES) | _push_static()
# Prefixes no downstream extra route may live under (the consent body route is one path per request id).
_RESERVED_PREFIXES: tuple[str, ...] = (_CONSENT_BODY_PREFIX, _SESSION_LOG_PREFIX, _QUESTION_BODY_PREFIX, _SIGN_PREFIX,
                                       _GRANT_PREFIX, _SHEET_PREFIX, _COORD_PREFIX, _PUSH_PREFIX,
                                       cockpit_threads.THREAD_PREFIX, _NOTE_BODY_PREFIX, _DISPATCH_PREFIX, _PENDING_PREFIX)


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
    if "writable" in payload:
        raise RuntimeError("SubstrateView.to_dict() collided with transport `writable`")
    payload["writable"] = source.write_scope is not None
    # OFF-BOX write signal (spore-129): true iff this surface is writable AND bound off-loopback,
    # i.e. the server's POST /edit will REQUIRE the X-Levain-Write-Token header (see do_POST).
    # It tells the frontend to attach the device-held token. The handler computes the predicate
    # (it owns the bound-address fact) and, since K2a, also sets it on any writable surface,
    # because every write needs the token there; default False here.
    if "write_token_required" in payload:
        raise RuntimeError("to_dict() collided with transport `write_token_required`")
    payload["write_token_required"] = write_token_required
    # Inline external panels (the read-only extra-PANEL seam): a per-request provider returns
    # full panel descriptors+data; split each into a layout entry (kind:"external") + its data
    # under payload["extra_panels"][id], so the frontend renders them inline among the substrate
    # panels. The provider is a DOWNSTREAM read (e.g. the Bridge's inbox) — gated like this whole
    # route. FAIL-SOFT: a provider fault degrades to no extra panels, never breaks the cockpit.
    if extra_panels is not None:
        if "extra_panels" in payload:
            raise RuntimeError("to_dict() collided with transport `extra_panels`")
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
                    # OPTIONAL markdown body (prose external panels): an alternative to `lines` —
                    # the frontend renders it via the shipped CSP-safe markdown renderer when
                    # present, else falls back to the lines model. Passed through like lines/note.
                    "markdown": p.get("markdown"),
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
        if "action_verbs" in payload:
            raise RuntimeError("to_dict() collided with transport `action_verbs`")
        payload["action_verbs"] = {
            # `idempotent` tells the frontend to mint + send a client `idempotency_key` for this
            # verb (the at-most-once retry token); `job` tells it the POST returns a job HANDLE to
            # POLL (GET /job.json) rather than a final result — both sourced from the registry,
            # never drift-prone.
            name: {"confirm_required": bool(spec.confirm_required),
                   "idempotent": bool(spec.idempotent), "job": bool(spec.job),
                   "sign": bool(getattr(spec, "sign", False)), "label": spec.label}
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
    request_queue_size = LISTEN_BACKLOG   # the stdlib's 5 resets a page load's burst on macOS (http_guards)

    levain_source: SubstrateSource
    levain_assets: dict[str, bytes]
    allowed_hosts: frozenset[str]
    request_gate: threading.BoundedSemaphore
    read_gate: threading.BoundedSemaphore
    read_waiters: int
    read_waiters_lock: threading.Lock
    # ``is_loopback_bind`` is computed from the ACTUAL bound address (un-foolable) and gates
    # whether the substrate READS need a token (spore-220). ``write_token`` is the ``browser``
    # surface's token: minted on a writable server given no tokens at all, None when the caller's
    # ``surface_tokens`` name no browser surface, and None on a read-only server.
    is_loopback_bind: bool
    # the make_server extra_hosts: a request whose Host names one came through a proxy on this machine (from
    # loopback, so is_loopback_bind cannot see it) and is off-box for the token gate (DESIGN_SIGN D3, L3 r2)
    proxied_hosts: frozenset[str]
    write_token: str | None
    # K2a (design §4.3, §9): one write token per surface (``browser``, ``flowconnect``), required on
    # EVERY bind for every write, since 127.0.0.1 is reachable by every local uid. Name -> token.
    surface_tokens: dict[str, str]
    # Q6a: ``fn(now)`` -> the consent queue's read-only listing, shared by the decisions panel and the body route
    consent_reader: Callable[[datetime], Any]
    # Q-2b: the question band's store over the same queue directory, shared by the decisions panel, the body route
    # and the answer verb; a queue with no questions.db lists none and creates none
    question_store: Any
    note_store: Any       # notes over the same queue directory: the panel's rows, the body route and note_dismiss
    verb_registry: VerbRegistry
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
    external_panels: "ExternalPanels | None" = None   # the manifest's adapter over extra_panels, built with the cockpit
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
    # The chat host (K1 part 2): live entity conversations, driven by jobs. None unless the operator
    # passed `--chat`; then the /chat routes are served, and make_server keeps the bind loopback-only.
    chat_host: "ChatHost | None"
    cockpit: "Cockpit | None" = None   # class defaults: server_close() runs even when the bind failed
    # The cockpit broker (K2b): None on a read-only source; else started with the cockpit and drained
    # by server_close. It is write-disabled unless the install class is declared (design §6).
    broker: "Broker | None" = None
    # The caller's sign-request Signer (DESIGN_SIGN D4), its page_origin pinned to the page this cockpit is served on;
    # None serves no sign route.
    signer: "Signer | None" = None
    # that pinned page_origin (the Signer refuses any other once make_server pins it), for what the page is told
    signer_page_origin: "str | None" = None
    # (queue dir, origin) this server published for a band reader; server_close withdraws it only if still its own
    published_origin: "tuple[Any, Any] | None" = None
    # the grants' passkey revoke (PLAN M17 "9a"), built by make_server from that Signer once it is pinned; None serves no
    # grant route
    grant_phone: "GrantPhone | None" = None
    # the consent family's answer and ruling (OS-2), built by make_server beside grant_phone, under its condition; None
    # serves no consent signing route
    consent_sheet: "ConsentSheet | None" = None
    # the dispatch family's sign, cancel and authorise (OS-2), built by make_server beside the Signer; None serves no
    # dispatch signing route
    dispatch_sheet: "DispatchSheet | None" = None
    # the coordinator drop family's drop and envelope signing (OS-2), built beside the Signer; None serves no drop route
    coord_sheet: "CoordSheet | None" = None
    # the caller's compose_signer (DESIGN_SIGN D5): (verb, params) -> the id of the sign request it filed; None serves no
    # compose route
    compose_signer: "Callable[[str, dict[str, Any]], str] | None" = None
    # The change stream (levain.cockpit.stream): set per instance in __init__, before the socket binds,
    # because a failed bind runs server_close and that reads these.
    stream_gate: threading.BoundedSemaphore
    stream_heartbeat_s: float
    stream_stop: threading.Event
    stream_watcher: "cockpit_stream.Watcher"

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.stream_gate = threading.BoundedSemaphore(cockpit_stream.DEFAULT_MAX_STREAMS)
        self.stream_heartbeat_s = cockpit_stream.DEFAULT_HEARTBEAT_S
        self.stream_stop = threading.Event()
        self.stream_watcher = cockpit_stream.Watcher(self._watch_manifest, epoch=secrets.token_hex(4))
        # PAIR: the paired lookups' and the claim's own semaphores (RT-3, RP-4), and the in-process revocation epoch
        # a revoke bumps AFTER its transaction commits, which every open stream compares before each frame (RU-3)
        self.pair_verify_gate = threading.BoundedSemaphore(_PAIR_VERIFY_SLOTS)
        self.pair_claim_gate = threading.BoundedSemaphore(_PAIR_CLAIM_SLOTS)
        self.pair_epoch = 0
        self.pair_epoch_lock = threading.Lock()
        # (verb, client_txn) -> params fingerprint, for each draft whose compose_signer call is in flight here
        # (_sign_compose): a second compose of the same draft waits on compose_wake for it (so two at once file one),
        # and a compose of any other draft never waits. A concurrency primitive only, never the record: which request a
        # draft filed is bound durably (levain.consent.sign.bind_compose) before its entry leaves. The lock is never
        # held across compose_signer.
        self.compose_inflight: "dict[tuple[str, str], str]" = {}
        self.compose_lock = threading.Lock()
        self.compose_wake = threading.Condition(self.compose_lock)
        super().__init__(*args, **kwargs)

    def _watch_manifest(self) -> dict[str, Any]:
        # the watcher reads as ONE fixed credential, the class a loopback page read carries (no token), so the
        # etags it announces are class-none ones; a page reading under a token holds class-token etags and
        # records the one its own panel read returned, never the announced one
        return self.get_cockpit().manifest({"class": "none", "device_class": None})

    _cockpit_lock = threading.Lock()

    def get_cockpit(self) -> "Cockpit":
        with self._cockpit_lock:
            if self.cockpit is None:
                from levain.cockpit.providers import build_default_cockpit

                built = build_default_cockpit(
                    self.levain_source,
                    job_store=self.job_runtime.store if self.job_runtime is not None else None)
                # the one attach list a server makes, the same call make_server makes: authority, labels and tiers per
                # request, Recent edits' broker rows (§0.6), the decisions panel (§3), agents waiting on the operator
                # (Q6a), the questions they ask him (Q-2b) and what they tell him (notes)
                broker = self.broker
                built.attach_server(
                    VerbView(self.verb_registry, built, lambda: broker_install_class(self)), self.consent_reader,
                    capture=broker.capture if broker is not None else None,
                    edits=broker.edits if broker is not None else None,
                    decision_rows=broker.decision_rows if broker is not None else None,
                    questions=getattr(self, "question_store", None), notes=getattr(self, "note_store", None))
                if self.extra_panels is not None:
                    # one callable call per refresh feeds the discovery and every adapted panel (design §6.2)
                    self.external_panels = ExternalPanels(self.extra_panels)
                    built.discover(discoverer(self.external_panels))
                try:
                    built.start()
                except BaseException:
                    built.stop()    # a half-started cockpit must not leave refreshers running
                    raise
                self.cockpit = built   # published only once started: a failed start retries next request
            cockpit = self.cockpit
        if self.broker is not None:
            # started with the cockpit, outside the cockpit's lock: its start recovery is a settler
            # and may wait on a store lock (K2b §4.2, §9); a second call returns at once
            self.broker.start()
        return cockpit

    def server_close(self) -> None:
        # K2b §4.2: the broker drains first (write admission closes; an admitted settler is waited
        # for before its lock is released), then the cockpit's refreshers stop
        if self.published_origin is not None:      # first: a teardown step that raises below must not leave a link to this server
            from levain.consent.cockpit_origin import withdraw_origin

            withdraw_origin(*self.published_origin)
            self.published_origin = None
        self.stream_stop.set()      # open streams end first: nothing is left holding a thread
        self.stream_watcher.stop()
        if self.broker is not None:
            self.broker.stop()
        if self.coord_sheet is not None:
            self.coord_sheet.close()    # no new wake; the wake threads a drop started are joined before teardown
        if self.cockpit is not None:
            self.cockpit.stop()
        super().server_close()

    def handle_error(self, request: Any, client_address: Any) -> None:
        """Swallow the benign client-disconnect family instead of dumping a traceback.

        ``protocol_version = "HTTP/1.1"`` (set on :class:`~levain.http_guards.GuardedHandler`) keeps connections alive, so a
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


class _Handler(GuardedHandler):
    """Serves the read-only routes (GET + HEAD) and the write routes (POST). Store paths, cached
    assets, and the Host allowlist live on the typed server instance (``self.server``); the shared
    guards (security headers, Host, cross-site, the write preamble) come from
    :class:`~levain.http_guards.GuardedHandler`."""

    server_version = "levain-serve"
    server: _LevainHTTPServer  # narrow the type for typed attribute access
    _credential_cache: Any = _UNSET
    _pair_epoch_seen = 0       # the revocation epoch read just before this request's paired lookup (RU-3)
    # (Content-Security-Policy, X-Frame-Options) in place of http_guards' for this one response (send_header), else None
    _frame_headers: "tuple[str, str] | None" = None

    def send_header(self, keyword: str, value: str) -> None:
        """The security headers as http_guards stamps them, except on the two responses of the sign sheet's overlay
        (OS-1, Q2): the sheet's own page, which its own origin may frame (``frame-ancestors 'self'``, ``SAMEORIGIN``),
        and the cockpit page that frames it (``frame-src 'self'``, itself still unframable)."""
        if self._frame_headers is not None:
            if keyword == "Content-Security-Policy":
                value = self._frame_headers[0]
            elif keyword == "X-Frame-Options":
                value = self._frame_headers[1]
        super().send_header(keyword, value)

    def _send_framing(self, frame_headers: "tuple[str, str]", *args: Any, **kw: Any) -> None:
        """``_send`` with ``frame_headers`` on this response only."""
        self._frame_headers = frame_headers
        try:
            self._send(*args, **kw)
        finally:
            self._frame_headers = None

    def _cross_site_pair_navigation(self) -> bool:
        """P2 L3 r1 F1: a pair link opened from webmail or another site is a top-level ``GET /pair`` navigation and
        arrives ``Sec-Fetch-Site: cross-site``, which the shared read preamble refuses. That page is a static shell
        with no data (its code rides the fragment, its claim is a same-origin POST), so ``_route`` lets exactly this
        request past the cross-site refusal, Host check kept. Exact raw path, GET only, a document navigation only: a
        cross-site fetch, subresource or frame of it, HEAD, ``/pair?..`` and every other path stay refused; framing is
        refused anyway (X-Frame-Options DENY). The sign sheet ``GET /sign/<id>`` is the same class (OS-1: a sign link
        opened from a message or a mail page; a static shell whose reads are same-origin and gated), exact raw path too;
        a cross-site frame of it is refused here, its own-origin frame is what its headers admit. The consent sheet
        ``GET /sign/consent/<id>`` (OS-2) is the same shell over a consent request, admitted alike."""
        page = self.path.split("?", 1)[0]
        return (self.command == "GET"
                and (self.path == "/pair" or _sheet_id(page) is not None or _consent_sheet_id(page) is not None
                     or _dispatch_sheet_id(page) is not None or page == _COORD_SHEET)
                and self.headers.get("Sec-Fetch-Site") == "cross-site"
                and self.headers.get("Sec-Fetch-Mode") == "navigate"
                and self.headers.get("Sec-Fetch-Dest") == "document")

    def handle_one_request(self) -> None:
        """One request on this (possibly kept-alive) connection. PAIR RS-1: the credential cache is reset here, at the
        top of EVERY request, so a revoke is seen by the next request on the same connection. RT-1: the one catch of
        :class:`_CredentialUnavailable`, answered 503 ``broker_unavailable`` with ``Retry-After`` and the
        connection closed (RU-2: like every refusal sent before a body is read). The body never says "token": the
        dashboard's token-drop paths match that word."""
        self._credential_cache = _UNSET
        try:
            super().handle_one_request()
        except _CredentialUnavailable:
            self.close_connection = True
            try:
                self._send(json.dumps({"error": "broker_unavailable",
                                       "message": "the cockpit is starting or busy; try again shortly"}).encode("utf-8"),
                           "application/json; charset=utf-8", status=503, head=self.command == "HEAD",
                           headers=[("Retry-After", "2")])
                self.wfile.flush()
            except (OSError, ValueError):
                pass        # the client went away first

    def _write_token_required(self) -> bool:
        """True iff this request needs a surface token on EVERY route, the substrate-bearing READS included
        (spore-220, spore-129): it is PROXIED, or it reaches a WRITABLE surface on an off-loopback bind.

        PROXIED (L3 r2 on 0867f2c0, whatever the source's write scope): its Host names an ``extra_hosts`` entry, or
        it carries a proxy header (``X-Forwarded-For``, ``X-Forwarded-Host``, ``Forwarded``, any
        ``Tailscale-User-*``). A proxy on this machine delivers a tailnet peer's request from loopback, with whatever
        Host the peer sent, so neither the bind nor the Host alone can tell it from a local one. make_server refuses
        extra_hosts on a server with no surface token; a proxy header reaching a loopback server with no token at all
        is refused (fail closed: a proxy nobody named gets nothing).

        The spore-220 carve-out stays: a READ-ONLY source on an off-loopback bind, not proxied, serves reads
        token-free (iPad/iPhone viewing; make_server allows that bind only for a no-install source). Writes are also
        gated by ``_writes_need_credential`` on every bind (K2a); the frontend's ``write_token_required`` signal is
        the OR of the two. Plain loopback with no proxy header stays token-free."""
        if host_header_allowed(self.headers.get("Host"), self.server.proxied_hosts) or any(
                k.lower() in _PROXY_HEADERS or k.lower().startswith(_PROXY_HEADER_PREFIX)
                for k in self.headers.keys()):
            return True
        return not self.server.is_loopback_bind and self.server.levain_source.write_scope is not None

    def _credential(self, *, required: "bool | None" = None) -> dict[str, Any]:
        """The credential THIS request carries, verified here and never claimed (design §5.1), computed once per
        request (RS-1: the cache is reset in ``handle_one_request``). First a surface token, compared in constant
        time against every surface's token (no early exit, so timing does not say which surface matched); on a
        miss with a header supplied, the PAIRED pass (``_paired_name``): a paired device's token, named
        ``paired-<device id>`` (RP-1). Else nothing. Raises :class:`_CredentialUnavailable` (never cached) per RU-1.

        F2(c), amending RT-1 / RU-1: where a credential is NOT required (``required`` False, or None and
        :meth:`_credential_required` says so), "unavailable" is answered as class ``none`` instead, NOT cached, so a
        gate later in the same request (``_off_box_token_valid`` passes ``required=True``) asks again and keeps its
        503."""
        cached = self._credential_cache
        if cached is not _UNSET:
            return cached
        try:
            cached = self._resolve_credential()
        except _CredentialUnavailable:
            if required is None:
                required = self._credential_required()
            if required:
                raise
            return {"class": "none", "device_class": None}
        self._credential_cache = cached
        return cached

    def _credential_required(self) -> bool:
        """Whether THIS request needs a credential at all (F2(c)): False only for a loopback-bound, unproxied
        (``not _write_token_required()``) GET / HEAD, which reaches no write route. Every route that requires a token
        on every bind asks through ``_off_box_token_valid`` (``required=True``) instead, so it keeps its 503."""
        return (not self.server.is_loopback_bind or self._write_token_required()
                or self.command not in ("GET", "HEAD"))

    def _resolve_credential(self) -> dict[str, Any]:
        raw = self.headers.get(_WRITE_TOKEN_HEADER, "")
        supplied = raw.encode("utf-8")
        name = None
        for n, tok in sorted(self.server.surface_tokens.items()):
            # RQ-6: make_server refuses a surface name with the paired prefix; this holds it at the point of use
            if n.startswith(PAIRED_PREFIX):
                raise RuntimeError(f"surface token name {n!r} is in the paired namespace")
            if tok and hmac.compare_digest(supplied, tok.encode("utf-8")) and name is None:
                name = n
        if name is not None:
            return {"class": "token", "name": name, "device_class": None}
        if raw:
            device = self._paired_device(raw)
            if device is not None:
                return {"class": "token", "name": PAIRED_PREFIX + device, "device_class": None}
        return {"class": "none", "device_class": None}

    def _pairing_db(self) -> Any:
        """This server's journal for pairing, the broker started asynchronously and waited on for at most
        ``_PAIR_WAIT_S`` (RU-4). None where paired tokens cannot exist (no broker: a read-only source with no
        ``ledger_root``; ``INSTALL_DISABLED`` over a journal with no unrevoked paired device), RU-1. Raises
        :class:`_CredentialUnavailable` for a transient state, and for ``INSTALL_DISABLED`` over a journal that does hold
        one (L3 r2: such a journal can be restarted without an install class, and a definite miss would make the page
        drop a good token)."""
        broker = self.server.broker
        if broker is None:
            return None
        try:
            broker.start_async()
        except Exception:  # noqa: BLE001 - G5: a starter thread that cannot start is the transient 503 (RT-1)
            raise _CredentialUnavailable("starter") from None
        state = broker.wait_settled(_PAIR_WAIT_S)
        if state is Lifecycle.INSTALL_DISABLED and not _journal_holds_paired_devices(broker.data_root):
            return None         # no paired token can be valid here: a wrong token stays the definite 403
        db = broker.active_db() if state is Lifecycle.ACTIVE else None
        if db is None:
            # STANDBY (another process owns the ledger), ACTIVATING past the wait, DRAINING; CLOSED (the server is
            # closing) is answered the same: never a definite miss, so a page never drops a good token on it
            raise _CredentialUnavailable(state.value)
        return db

    def _paired_device(self, raw: str) -> "str | None":
        """The id of the unrevoked paired device ``raw`` is the token of, or None for a definite miss. Bounded (RT-3):
        a header outside the header-safe shape or ``_PAIR_TOKEN_LEN`` is never looked up, and a lookup takes one of
        ``_PAIR_VERIFY_SLOTS`` BEFORE the broker start / the lookup (a full set is the 503 of RT-1)."""
        srv = self.server
        if srv.broker is None:
            return None         # RU-1: no ledger, no paired tokens: skip the pass entirely
        lo, hi = _PAIR_TOKEN_LEN
        if not (lo <= len(raw) <= hi) or not _HEADER_SAFE_TOKEN.fullmatch(raw):
            return None
        if not srv.pair_verify_gate.acquire(blocking=False):
            raise _CredentialUnavailable("paired lookups are full")
        try:
            # read BEFORE the lookup, so a revoke committing after it is one a stream of this request still sees
            self._pair_epoch_seen = srv.pair_epoch
            db = self._pairing_db()
            if db is None:
                return None
            now = datetime.now(timezone.utc)
            try:
                device = db.verify_device(raw, now=now)
            except Exception as exc:  # noqa: BLE001 - a journal fault is no definite "revoked": 503, the token kept
                raise _CredentialUnavailable(f"{type(exc).__name__}") from exc
        finally:
            srv.pair_verify_gate.release()
        # F1: the last_seen stamp runs AFTER the answer and after the verify slot is released, with no busy wait
        # (busy_timeout 0), and nothing it does reaches the answer
        if device is not None:
            try:
                db.stamp_last_seen(device, now=now)
            except Exception:  # noqa: BLE001 - a display stamp never fails a request
                _log.debug("paired device last_seen stamp skipped", exc_info=True)
        return device

    def _transport(self) -> dict[str, bool]:
        """The manifest's ``transport``: the same two flags ``/substrate.json`` carries."""
        return {"writable": self.server.levain_source.write_scope is not None,
                "write_token_required": self._write_token_required() or self._writes_need_credential()}

    def _writes_need_credential(self) -> bool:
        """K2a: a writable surface needs a credential on every write, on every bind."""
        return self.server.levain_source.write_scope is not None

    def _off_box_token_valid(self) -> bool:
        """True iff the request carries any surface's token (``_credential``). The SINGLE compare
        shared by the read gate (``_route``) and the write gate (``do_POST``) so the two can never
        diverge on the auth check (``structural_invariants_beat_discipline``); each caller gates on
        its own predicate first. An empty surface token never matches."""
        return self._credential(required=True)["class"] == "token"

    def _cockpit(self) -> "Cockpit":
        """The server's cockpit; the kernel's default over this substrate is built on first use
        (a server nobody asks for a manifest never reads the substrate for one)."""
        return self.server.get_cockpit()

    def _chat_token_valid(self) -> bool:
        """True iff the request carries this launch's chat token (constant-time compare). A server with
        no token, or an empty supplied one, fails closed."""
        expected = getattr(self.server, "chat_token", None) or ""
        supplied = self.headers.get(_CHAT_TOKEN_HEADER, "")
        return bool(expected) and hmac.compare_digest(
            supplied.encode("utf-8"), expected.encode("utf-8")
        )

    def _egress_lookup(self) -> Any:
        """The read-only egress reading for a consent body, over the queue the decisions panel reads
        (``listing.egress_reading``, the one the panel's rows use too)."""
        from levain.consent.listing import egress_reading

        return egress_reading(self.server.consent_reader)

    def _serve_consent_body(self, path: str, *, head: bool) -> None:
        """``GET /cockpit/consent/<request_id>.json``: one listed consent request's body (``entry_body``). Reached
        only through ``_route`` (Host allowlist, cross-site refusal, off-box gate), then a surface token is
        required on every bind, judged by the one compare every token check here uses (``_credential``).
        Read-only: the id is looked up in the read-only listing over the queue the decisions panel reads;
        an unknown id is 404 and nothing is written."""
        from urllib.parse import unquote

        if not self._off_box_token_valid():
            self._send(json.dumps({"error": "forbidden", "message": (
                f"a consent request's details need this page's write token, sent as {_WRITE_TOKEN_HEADER}")}
            ).encode("utf-8"), "application/json; charset=utf-8", status=403, head=head,
                headers=[("Vary", _WRITE_TOKEN_HEADER)], cache_control="private, no-store")
            return
        request_id = unquote(path[len(_CONSENT_BODY_PREFIX):-len(_CONSENT_BODY_SUFFIX)])
        status, payload = 404, {"error": "not_found", "message": "no such consent request listed"}
        if _CONSENT_ID.fullmatch(request_id):
            if not self.server.request_gate.acquire(blocking=False):
                self._send(b"busy\n", "text/plain; charset=utf-8", status=503, head=head)
                return
            try:
                from levain.consent.listing import entry_body, find_entry

                if request_id == PROPOSALS_ID:      # the standing-rule cards: not a request, the id no request carries
                    from levain.cockpit.proposals import MIN_HAND_ALLOWS, propose
                    from levain.consent.listing import read_evidence
                    from levain.consent.policy import AGENT_CONFIG_TIER

                    reader = self.server.consent_reader
                    if not hasattr(reader, "directory"):     # a reader naming no queue: nothing, never the default
                        status, payload = 200, {"proposals": []}
                    else:
                        found = read_evidence(reader.directory, now=datetime.now(timezone.utc),
                                              exclude_tier=AGENT_CONFIG_TIER, min_hand=MIN_HAND_ALLOWS)
                        status, payload = 200, {"proposals": [p.as_dict() for p in propose(found)]}
                elif request_id == GRANTS_ID:       # the standing grants, what each answered, each revoke line
                    from levain.consent.grants import INERT_NOTE, grant_view
                    from levain.consent.queue import default_queue_dir

                    reader = self.server.consent_reader
                    if not hasattr(reader, "directory"):     # a reader naming no queue: nothing, never the default
                        status, payload = 200, {"root": "", "note": "", "grants": [], "inert_note": INERT_NOTE}
                    else:
                        status, payload = 200, grant_view(reader.directory if reader.directory is not None
                                                          else default_queue_dir(), now=datetime.now(timezone.utc))
                else:
                    entry = find_entry(self.server.consent_reader(datetime.now(timezone.utc)), request_id)
                    if entry is not None:
                        status, payload = 200, entry_body(entry, settled_lookup=self._egress_lookup())
                        # OS-2: the sheet's own row: `sheet` when it is open to the sheet here (it shows the
                        # reason when no passkey may sign it), `sheet_choices` the answers the sheet offers (the
                        # card routes only those to it), `sheet_v` the version the card's link carries
                        row = None
                        if self.server.consent_sheet is not None and entry.state in ("pending", "undecided"):
                            try:
                                row = self.server.consent_sheet.row(request_id)
                            except _CredentialUnavailable:
                                raise
                            except Exception:  # noqa: BLE001 - the card keeps its terminal lines
                                row = None
                        payload["sheet"] = row is not None
                        payload["sheet_choices"] = ([o["key"] for o in row["options"]]
                                                    if row is not None and row["signers"] else [])
                        if row is not None:
                            payload["sheet_v"] = row["sha256"]
            except _CredentialUnavailable:   # RU-5: only the outer catch answers it
                raise
            except Exception as exc:  # noqa: BLE001 - a queue that cannot be read is an answer, never a dead connection
                status, payload = 500, {"error": "internal", "message": f"{type(exc).__name__}: {exc}"}
            finally:
                self.server.request_gate.release()
            if request_id == PROPOSALS_ID and status == 200:
                # 14b S4: the graduation cards and steps beside the proposals, read-only, built after the gate is
                # released (RULINGS_R1 item 7: a slow authority log never holds it). The store and the engine are the
                # binding driver's to wire (S6); with neither the payload is the proposals alone, as before
                grad_store = getattr(self.server, "autonomic_store_dir", None)
                grad_engine = getattr(self.server, "graduation_engine", None)
                if grad_store is not None or grad_engine is not None:
                    from levain.cockpit.graduation_cards import graduation_view

                    try:
                        payload["graduations"] = graduation_view(grad_store, grad_engine)
                    except Exception:  # noqa: BLE001 - steps that cannot be read are not shown; the proposals stand
                        payload["graduations"] = {"steps": [], "cards": []}
        self._send(json.dumps(payload).encode("utf-8"), "application/json; charset=utf-8", status=status, head=head,
                   headers=[("Vary", _WRITE_TOKEN_HEADER)], cache_control="private, no-store")

    def _serve_question_body(self, path: str, *, head: bool) -> None:
        """``GET /cockpit/question/<n>.json``: one question's body (what, options, rec, blocks, agent, filed_at, and
        its answer if any). The consent body's gate, exactly: reached only through ``_route``, then a surface token
        on every bind, judged by the one compare every token check here uses, before the id is looked at. Read-only:
        an id that is not 1 to 12 digits, or names no question, is 404, and no ``questions.db`` is ever created."""
        if not self._off_box_token_valid():
            self._send(json.dumps({"error": "forbidden", "message": (
                f"a question's details need this page's write token, sent as {_WRITE_TOKEN_HEADER}")}
            ).encode("utf-8"), "application/json; charset=utf-8", status=403, head=head,
                headers=[("Vary", _WRITE_TOKEN_HEADER)], cache_control="private, no-store")
            return
        n = urllib.parse.unquote(path[len(_QUESTION_BODY_PREFIX):-len(_CONSENT_BODY_SUFFIX)])
        status, payload = 404, {"error": "not_found", "message": "no such question"}
        store = getattr(self.server, "question_store", None)
        if store is not None and _QUESTION_ID.fullmatch(n):
            if not self.server.request_gate.acquire(blocking=False):
                self._send(b"busy\n", "text/plain; charset=utf-8", status=503, head=head)
                return
            try:
                q = store.get(int(n))
                if q is not None:
                    a = q.answer
                    status, payload = 200, {
                        "n": q.n, "agent": q.agent, "what": q.what,
                        "options": [{"key": k, "label": label} for k, label in q.options],
                        "rec": q.rec, "blocks": q.blocks, "filed_at": q.filed_at,
                        "answer": None if a is None else {"kind": a.kind, "value": a.value, "channel": a.channel,
                                                          "answered_at": a.answered_at}}
            except _CredentialUnavailable:   # RU-5: only the outer catch answers it
                raise
            except Exception as exc:  # noqa: BLE001 - a store that cannot be read is an answer, never a dead connection
                status, payload = 500, _store_fault("question", exc)
            finally:
                self.server.request_gate.release()
        self._send(json.dumps(payload).encode("utf-8"), "application/json; charset=utf-8", status=status, head=head,
                   headers=[("Vary", _WRITE_TOKEN_HEADER)], cache_control="private, no-store")

    def _serve_sign_list(self, *, head: bool, one: "str | None" = None) -> None:
        """``GET /cockpit/sign.json``: ``{requests, signers, credentials, origin}``, the Signer's own listing rows
        (sha256, options, write_in, recommended) for the active passkeys that may sign, those keys' fingerprints, the
        credential id and provider the cockpit holds for each of them (:meth:`levain.consent.sign.Signer.key_facts`:
        display facts a page builds ``allowCredentials`` from, never a widening of who signs), and the page origin
        the ceremony is pinned to (a page served elsewhere sends the operator there). Reached only
        through ``_route`` (Host allowlist, cross-site refusal), then the write gate's other checks before the
        directory is read: same-origin only (``Sec-Fetch-Site`` absent or ``same-origin``), a surface token on every
        bind, the request gate. Without a signer, 404. Keys that cannot be read list no signers and say why in
        ``note``; the rows still list, as on the phone page.

        ``GET /cockpit/sign/<id>.json`` (``one``), behind the same gate: ``{request, signers, credentials, origin}``
        with that one
        request's row (:meth:`levain.consent.sign.Signer.row`), or 404 when it is not open, whatever the listing's
        cap."""
        from levain.consent.sign import SignRefused

        signer = self.server.signer
        hdrs = [("Vary", _WRITE_TOKEN_HEADER)]
        if signer is None:
            self._send(json.dumps({"error": "not_found", "message": "no such route"}).encode("utf-8"),
                       "application/json; charset=utf-8", status=404, head=head)
            return
        if not self._sign_read_admitted(head=head, what="the sign requests"):
            return
        status: int
        try:
            payload: dict[str, Any]
            try:
                signers = signer.signers()
                note = None
            except SignRefused as exc:
                signers, note = [], str(exc)
            # `origin`: the one page the ceremony signs on (make_server checked it), so a page served anywhere else says
            # where to sign rather than offering a gesture its browser would refuse
            origin = self.server.signer_page_origin
            credentials = signer.key_facts(signers)
            if one is None:
                payload, status = {"requests": signer.listing(signers), "signers": signers, "credentials": credentials,
                                   "origin": origin}, 200
            else:
                row = signer.row(one, signers)
                # `rp_id`: the name a passkey record in the browser is kept under (the sign sheet writes and reads it there)
                payload, status = (({"request": row, "signers": signers, "credentials": credentials, "origin": origin,
                                     "rp_id": signer.rp_id}, 200) if row is not None
                                   else ({"error": "not_open", "message": "this sign request is not open"}, 404))
            if note is not None and status == 200:
                payload["note"] = note
        except _CredentialUnavailable:   # RU-5: only the outer catch answers it
            raise
        except Exception as exc:  # noqa: BLE001 - a directory that cannot be read is an answer, never a dead connection
            status, payload = 500, _store_fault("sign request", exc)
        finally:
            self.server.request_gate.release()
        self._send(json.dumps(payload).encode("utf-8"), "application/json; charset=utf-8", status=status, head=head,
                   headers=hdrs, cache_control="private, no-store")

    def _serve_sheet(self, *, head: bool, consent: bool = False, dispatch: bool = False, coord: bool = False) -> None:
        """``GET /sign/<id>`` (OS-1): the sign sheet's page, the same bytes for every id (the id is read from the path
        by ``/sign.js``), framable only by this origin. Without a signer, 404. ``GET /sign/consent/<id>`` (``consent``,
        OS-2) serves the same bytes beside a consent sheet, ``GET /sign/dispatch/<job>`` (``dispatch``) beside a dispatch
        sheet, else 404."""
        beside = (self.server.consent_sheet if consent else self.server.dispatch_sheet if dispatch
                  else self.server.coord_sheet if coord else self.server.signer)
        if beside is None:
            self._send(b"not found\n", "text/plain; charset=utf-8", status=404, head=head)
            return
        self._send_framing((_SHEET_CSP, "SAMEORIGIN"), self.server.levain_assets[_SHEET_PAGE], "text/html; charset=utf-8",
                           head=head)

    def _sign_read_admitted(self, *, head: bool, what: str) -> bool:
        """The sign reads' gate past ``_route`` (the sign listing's and the grant listing's, one code path): same-origin
        only (``Sec-Fetch-Site`` absent or ``same-origin``), a surface token on every bind, then the request gate,
        which True leaves HELD for the caller to release. False: the refusal was sent."""
        hdrs = [("Vary", _WRITE_TOKEN_HEADER)]
        if self.headers.get("Sec-Fetch-Site") not in (None, _SAME_ORIGIN):
            self._send(json.dumps({"error": "forbidden", "message": "cross-origin read refused"}).encode("utf-8"),
                       "application/json; charset=utf-8", status=403, head=head, headers=hdrs,
                       cache_control="private, no-store")
            return False
        if not self._off_box_token_valid():
            self._send(json.dumps({"error": "forbidden", "message": (
                f"{what} need this page's write token, sent as {_WRITE_TOKEN_HEADER}")}).encode("utf-8"),
                "application/json; charset=utf-8", status=403, head=head, headers=hdrs,
                cache_control="private, no-store")
            return False
        if not self.server.request_gate.acquire(blocking=False):
            self._send(b"busy\n", "text/plain; charset=utf-8", status=503, head=head)
            return False
        return True

    def _serve_push(self, path: str, *, head: bool, read: bool = False) -> None:
        """A web push GET (OS-4) through :func:`levain.webpush.routes.handle` over the Signer's queue: an app-shell
        file (no query reaches it, so the manifest never carries a pairing code), or with ``read`` the key or the
        record, behind :meth:`_sign_read_admitted`."""
        from levain.webpush import routes as push_routes

        queue = self.server.signer.queue_dir
        if not read:
            status, body, ctype = push_routes.handle("GET", path, {}, b"", queue_dir=queue, authorized=False,
                                                     device_id=None) or (404, b"not found\n", "text/plain")
            self._send(body, ctype, status=status, head=head)
            return
        if not self._sign_read_admitted(head=head, what="push reads"):
            return
        try:
            try:
                got = push_routes.handle("GET", path, {}, b"", queue_dir=queue, authorized=True,
                                         device_id=str(self._credential()["name"]))
            except _CredentialUnavailable:   # RU-5: only the outer catch answers it
                raise
            except Exception as exc:  # noqa: BLE001 - a store that cannot be read is an answer
                got = 500, json.dumps({"error": "internal", "message": type(exc).__name__}).encode("utf-8"), "json"
            status, body, _ = got
            self._send(body, "application/json; charset=utf-8", status=status, head=head,
                       headers=[("Vary", _WRITE_TOKEN_HEADER)], cache_control="private, no-store")
        finally:
            self.server.request_gate.release()

    def _push_post(self, route: str, raw: bytes) -> "tuple[dict[str, Any] | bytes, int]":
        """A web push POST (OS-4), its credential already checked: only a PAIRED device subscribes (a surface token
        has no revoke that could stop the pings), and every subscription is stored under the device's credential
        name, with this cockpit's journal as its device's authority: the send asks the journal whether the device is
        live (:func:`levain.webpush.store.live_rows`), so a revoke stops its pings whether or not :meth:`_retire_push`
        ran, and the subscribe asks it again inside its own write."""
        from levain.webpush import routes as push_routes

        name = str(self._credential()["name"])
        if route == _PUSH_SUBSCRIBE and not name.startswith(PAIRED_PREFIX):
            return {"error": "device_required", "message": "turn pings on from a paired device: pair it from the "
                    "Devices panel first"}, 403
        broker = self.server.broker
        got = push_routes.handle("POST", route, {}, raw, queue_dir=self.server.signer.queue_dir, authorized=True,
                                 device_id=name, journal=broker.data_root if broker is not None else None)
        if got is None:                 # every route in _PUSH_POST is one of handle's
            raise RuntimeError(f"push route {route} not handled")
        return got[1], got[0]

    def _retire_push(self, device_ids: "list[str]", now: datetime) -> "str | None":
        """Tidy-up after a revoke (OS-4): the revoked paired devices' push subscriptions marked retired. Not the
        guard: every send asks the journal, so a revoked device gets no ping either way. A note when the push store
        could not be written (the revoke itself has committed), else None."""
        if self.server.signer is None or not device_ids:
            return None
        from levain.webpush.store import PushStore, retire_device_subscriptions

        try:
            conn = PushStore(self.server.signer.queue_dir).connection()
        except Exception as exc:  # noqa: BLE001 - the revoke stands and the send refuses the device
            _log.warning("paired device revoke: push store not opened", exc_info=True)
            return f"its push subscriptions were not tidied ({type(exc).__name__}); no ping reaches it"
        try:
            with conn:
                for device in device_ids:
                    retire_device_subscriptions(conn, PAIRED_PREFIX + str(device), now)
        except Exception as exc:  # noqa: BLE001 - the revoke stands and the send refuses the device
            _log.warning("paired device revoke: push subscriptions not tidied", exc_info=True)
            return f"its push subscriptions were not tidied ({type(exc).__name__}); no ping reaches it"
        finally:
            try:
                conn.close()
            except Exception:  # noqa: BLE001 - a close fault after the commit says nothing about the retirement
                _log.warning("paired device revoke: push store close failed", exc_info=True)
        return None

    def _serve_grant_list(self, *, head: bool) -> None:
        """``GET /cockpit/grant/list.json``: :meth:`levain.consent.grant_phone.GrantPhone.listing` (each grant with
        ``instance`` and ``signers``, the passkeys that may revoke it, and the notes) plus ``origin``, the page the
        ceremony is pinned to, behind the sign listing's gate exactly. Without a Signer, 404."""
        grants = self.server.grant_phone
        hdrs = [("Vary", _WRITE_TOKEN_HEADER)]
        if grants is None:
            self._send(json.dumps({"error": "not_found", "message": "no such route"}).encode("utf-8"),
                       "application/json; charset=utf-8", status=404, head=head)
            return
        if not self._sign_read_admitted(head=head, what="the grants"):
            return
        try:
            payload, status = {**grants.listing(), "origin": self.server.signer_page_origin}, 200
        except _CredentialUnavailable:   # RU-5: only the outer catch answers it
            raise
        except Exception as exc:  # noqa: BLE001 - listing() says its own faults; anything else is an answer too
            status, payload = 500, _store_fault("grant", exc)
        finally:
            self.server.request_gate.release()
        self._send(json.dumps(payload).encode("utf-8"), "application/json; charset=utf-8", status=status, head=head,
                   headers=hdrs, cache_control="private, no-store")

    def _grant_post(self, grant_id: str, action: str, req: Any) -> "tuple[dict[str, Any], int]":
        """``POST /cockpit/grant/<id>/<action>``: the GrantPhone's own reply, or its refusal as ``{error, message}``
        with the refusal's status (the phone port's words)."""
        from levain.consent.grant_phone import GrantRefused

        grants = self.server.grant_phone
        if grants is None:     # the route is not served without one; an explicit check survives `python -O`
            return {"error": "not_found", "message": "no such route"}, 404
        try:
            return grants.step(grant_id, action, req), 200
        except GrantRefused as exc:
            return {"error": exc.error, "message": exc.message}, exc.status

    def _serve_consent_sheet(self, request_id: str, *, head: bool) -> None:
        """``GET /cockpit/consent/<id>/sheet.json`` (OS-2): ``{request, signers, credentials, origin, rp_id}`` with the
        request's sheet row (:meth:`ConsentSheet.row`), or 404 ``not_open`` once it is no longer open to a passkey here,
        behind the sign listing's gate exactly. Without a consent sheet, 404 ``not_found``."""
        sheet = self.server.consent_sheet
        hdrs = [("Vary", _WRITE_TOKEN_HEADER)]
        if sheet is None:
            self._send(json.dumps({"error": "not_found", "message": "no such route"}).encode("utf-8"),
                       "application/json; charset=utf-8", status=404, head=head, cache_control="private, no-store")
            return
        if not self._sign_read_admitted(head=head, what="the consent requests"):
            return
        try:
            payload: dict[str, Any]
            row = sheet.row(request_id)
            if row is None:
                payload, status = {"error": "not_open", "message": "this consent request is not open"}, 404
            else:
                try:
                    credentials = sheet.signer.key_facts(row["signers"])
                except Exception:  # noqa: BLE001 - display facts only: the browser's own list stands in for them
                    credentials = {}
                payload, status = {"request": row, "signers": row["signers"], "credentials": credentials,
                                   "origin": self.server.signer_page_origin, "rp_id": sheet.rp_id}, 200
                if row.get("note"):
                    payload["note"] = row["note"]
        except _CredentialUnavailable:   # RU-5: only the outer catch answers it
            raise
        except Exception as exc:  # noqa: BLE001 - a queue that cannot be read is an answer, never a dead connection
            status, payload = 500, _store_fault("consent request", exc)
        finally:
            self.server.request_gate.release()
        self._send(json.dumps(payload).encode("utf-8"), "application/json; charset=utf-8", status=status, head=head,
                   headers=hdrs, cache_control="private, no-store")

    def _consent_post(self, request_id: str, action: str, req: Any) -> "tuple[dict[str, Any], int]":
        """``POST /cockpit/consent/<id>/<action>`` (OS-2): the consent sheet's reply and status, or its refusal as
        ``{error, message}`` with the phone port's status."""
        sheet = self.server.consent_sheet
        if sheet is None:      # the route is not served without one; an explicit check survives `python -O`
            return {"error": "not_found", "message": "no such route"}, 404
        try:
            return sheet.step(request_id, action, req), 200
        except ConsentRefused as exc:
            return {"error": exc.error, "message": exc.message}, exc.status

    def _serve_dispatch_sheet(self, job_id: "str | None", *, head: bool) -> None:
        """``GET /cockpit/dispatch/<job>/sheet.json`` (OS-2): ``{request, signers, credentials, origin, rp_id}`` with the
        job's sheet row (:meth:`DispatchSheet.row`), or 404 ``not_open`` once it is final or unknown;
        ``GET /cockpit/dispatch.json`` (``job_id`` None): ``{requests, note, overflow, origin, rp_id}``, every live job as
        a row. Behind the sign listing's gate exactly. Without a dispatch sheet, 404 ``not_found``."""
        sheet = self.server.dispatch_sheet
        hdrs = [("Vary", _WRITE_TOKEN_HEADER)]
        if sheet is None:
            self._send(json.dumps({"error": "not_found", "message": "no such route"}).encode("utf-8"),
                       "application/json; charset=utf-8", status=404, head=head, cache_control="private, no-store")
            return
        if not self._sign_read_admitted(head=head, what="the dispatch jobs"):
            return
        try:
            payload: dict[str, Any]
            if job_id is None:
                rows, note, overflow = sheet.listing()
                try:
                    credentials = sheet.signer.key_facts(sorted({fp for r in rows for fp in r["signers"]}))
                except Exception:  # noqa: BLE001 - display facts only
                    credentials = {}
                payload, status = {"requests": rows, "note": note, "overflow": overflow, "credentials": credentials,
                                   "origin": self.server.signer_page_origin, "rp_id": sheet.rp_id}, 200
                problem = sheet.elsewhere()
                if problem is not None:
                    payload["elsewhere"] = problem
            else:
                row = sheet.row(job_id)
                if row is None:
                    payload, status = {"error": "not_open", "message": "this dispatch job is not open"}, 404
                else:
                    try:
                        credentials = sheet.signer.key_facts(row["signers"])
                    except Exception:  # noqa: BLE001 - display facts only
                        credentials = {}
                    payload, status = {"request": row, "signers": row["signers"], "credentials": credentials,
                                       "origin": self.server.signer_page_origin, "rp_id": sheet.rp_id}, 200
                    if row.get("note"):
                        payload["note"] = row["note"]
        except _CredentialUnavailable:   # RU-5: only the outer catch answers it
            raise
        except Exception as exc:  # noqa: BLE001 - a store that cannot be read is an answer, never a dead connection
            status, payload = 500, _store_fault("dispatch job", exc)
        finally:
            self.server.request_gate.release()
        self._send(json.dumps(payload).encode("utf-8"), "application/json; charset=utf-8", status=status, head=head,
                   headers=hdrs, cache_control="private, no-store")

    def _serve_coord_sheet(self, *, head: bool) -> None:
        """``GET /cockpit/coord/drops.json`` (OS-2): the drop sheet's read, ``{coord_drops, coord_store_id, coord_repos,
        coord_signers, coord_note, ..., credentials, origin, rp_id}`` (:meth:`CoordSheet.listing`), behind the sign listing's
        gate exactly. Without a coord sheet, 404 ``not_found``."""
        sheet = self.server.coord_sheet
        hdrs = [("Vary", _WRITE_TOKEN_HEADER)]
        if sheet is None:
            self._send(json.dumps({"error": "not_found", "message": "no such route"}).encode("utf-8"),
                       "application/json; charset=utf-8", status=404, head=head, cache_control="private, no-store")
            return
        if not self._sign_read_admitted(head=head, what="the coordinator drops"):
            return
        try:
            payload: dict[str, Any] = sheet.listing()
            try:
                credentials = sheet.signer.key_facts(list(payload.get("coord_signers") or []))
            except Exception:  # noqa: BLE001 - display facts only
                credentials = {}
            payload.update(credentials=credentials, origin=self.server.signer_page_origin, rp_id=sheet.rp_id)
            status = 200
        except _CredentialUnavailable:   # RU-5: only the outer catch answers it
            raise
        except Exception as exc:  # noqa: BLE001 - a store that cannot be read is an answer, never a dead connection
            status, payload = 500, _store_fault("coordinator drops", exc)
        finally:
            self.server.request_gate.release()
        self._send(json.dumps(payload).encode("utf-8"), "application/json; charset=utf-8", status=status, head=head,
                   headers=hdrs, cache_control="private, no-store")

    def _coord_post(self, action: str, req: Any) -> "tuple[dict[str, Any], int]":
        """``POST /cockpit/coord/<action>`` (OS-2): the coord sheet's reply and status, or its refusal as
        ``{error, message}`` with the phone port's status."""
        from levain.coordinator.phone import CoordRefused

        sheet = self.server.coord_sheet
        if sheet is None:      # the route is not served without one; an explicit check survives `python -O`
            return {"error": "not_found", "message": "no such route"}, 404
        try:
            return sheet.step(action, req), 200
        except CoordRefused as exc:
            return {"error": exc.error, "message": exc.message}, exc.status

    def _key_line_post(self, req: Any) -> "tuple[dict[str, Any], int]":
        """``POST /cockpit/passkey/key-line`` (OS-2): :meth:`levain.phone_server._PhoneServer.key_line` itself, run over
        the Signer's queue and rpId (all it reads), or its refusal as ``{error, message}`` with the phone port's status."""
        from types import SimpleNamespace

        from levain.phone_server import PhoneError, _PhoneServer

        signer = self.server.signer
        if signer is None:     # the route is not served without one; an explicit check survives `python -O`
            return {"error": "not_found", "message": "no such route"}, 404
        try:
            return _PhoneServer.key_line(SimpleNamespace(rp_id=signer.rp_id, queue_dir=signer.queue_dir), req), 200
        except PhoneError as exc:
            return {"error": exc.error, "message": exc.message}, exc.status

    def _dispatch_post(self, job_id: str, action: str, req: Any) -> "tuple[dict[str, Any], int]":
        """``POST /cockpit/dispatch/<job>/<action>`` (OS-2): the dispatch sheet's reply and status, or its refusal as
        ``{error, message}`` with the phone port's status."""
        from levain.dispatch.phone import DispatchRefused

        sheet = self.server.dispatch_sheet
        if sheet is None:      # the route is not served without one; an explicit check survives `python -O`
            return {"error": "not_found", "message": "no such route"}, 404
        try:
            return sheet.step(job_id, action, req), 200
        except DispatchRefused as exc:
            return {"error": exc.error, "message": exc.message}, exc.status

    def _sign_post(self, request_id: str, action: str, req: Any) -> "tuple[dict[str, Any], int]":
        """``POST /cockpit/sign/<id>/<action>``: the Signer's own reply, or its refusal as ``{error, message}``
        with the refusal's status, in the words of :func:`levain.consent.sign.error_word` (the phone port's table too)."""
        from levain.consent.sign import SignRefused, error_word

        signer = self.server.signer
        if signer is None:     # the route is not served without one; an explicit check survives `python -O`
            return {"error": "not_found", "message": "no such route"}, 404
        # the Signer verifies against its own page_origin, which make_server pinned: nothing can move it under this call
        try:
            return getattr(signer, action)(request_id, req), 200
        except SignRefused as exc:
            return {"error": error_word(exc.status), "message": str(exc)}, exc.status

    def _sign_compose(self, req: Any) -> "tuple[dict[str, Any], int]":
        """``POST /cockpit/sign/compose`` ``{verb, params, client_txn}`` -> ``{request}`` (DESIGN_SIGN D5): the
        caller's ``compose_signer`` files a sign request for a SIGNED verb's params and names it; the page runs the
        ceremony on that id and only then posts the verb with ``sign_request``. The verb must be one this server serves
        with ``sign`` set, and the params its fields; a ``ValueError`` from the compose_signer is a 400 carrying its
        words (the reason the operator reads). A KERNEL signed verb's request is filed by levain's own composer
        (``levain.cockpit.verbs.KERNEL_COMPOSERS``) ahead of the compose_signer; the kernel signs nothing.

        ``client_txn`` is the draft's one identity (the page mints it when the signed draft is first sent and keeps it
        with the draft): the same ``(verb, client_txn)`` names the SAME request while that request is open, so a lost
        reply, a retry, a reload or a second send of one draft files one request. The same id with other params is
        refused (409): an identity names one draft. Which request a draft filed is bound durably beside the Signer's
        requests (``levain.consent.sign.bind_compose``) before the answer is sent, so this holds across a restart and
        any number of other drafts, and the kernel's sign check (:meth:`_signed_for`) reads that binding; a request
        filed but not bound is a 503 (the draft is sent again; the request it filed stays open until it expires). Two
        composes of one draft at once file one: the second waits for the first's compose_signer call (up to
        _COMPOSE_WAIT_S, then 503). Composes of different drafts never wait on each other: the compose_signer runs
        outside the server's lock. A draft with no live binding while ``COMPOSE_LIVE_MAX`` bindings are live is
        refused 429 ``too_many_open`` before anything is filed (``levain.consent.sign.draft_bindings``); a draft
        already bound is answered whatever the count."""
        from levain.consent.sign import SignRefused, bind_compose, draft_bindings, error_word, request_id_ok, sign_dir
        from levain.cockpit.verbs import KERNEL_COMPOSERS, SIGN_PARAM

        if self.server.signer is None or (self.server.compose_signer is None and not (
                isinstance(req, dict) and isinstance(req.get("verb"), str) and req["verb"] in KERNEL_COMPOSERS)):
            # no Signer: the route is not served (an explicit check survives `python -O`); no compose_signer: it files
            # only levain's own kernel requests, and is the 404 it always was for anything else
            return {"error": "not_found", "message": "no such route"}, 404
        if not isinstance(req, dict) or set(req) != {"verb", "params", "client_txn"}:
            return {"error": "bad_request", "message": "request keys must be ['client_txn', 'params', 'verb']"}, 400
        name, params, txn = req["verb"], req["params"], req["client_txn"]
        if not request_id_ok(txn):
            return {"error": "bad_request", "message": "'client_txn' must be 8 to 64 of A-Z a-z 0-9 _ -"}, 400
        spec = self.server.verb_registry.get(name) if isinstance(name, str) else None
        if spec is None:
            return {"error": "unknown_verb", "message": f"no such verb: {str(name)[:80]!r}"}, 404
        if not spec.sign:
            return {"error": "not_signed", "message": f"{spec.name!r} is not a signed verb: it is sent as it is"}, 400
        # c4-mut R1: a KERNEL signed verb's request is levain's own to file (its namespace no caller may name); every
        # other signed verb's is the caller's compose_signer's, and without one this route files nothing for it
        kernel = KERNEL_COMPOSERS.get(spec.name) if not self.server.verb_registry.is_downstream(spec.name) else None
        compose: "Callable[[str, dict[str, Any]], str] | None" = self.server.compose_signer
        if kernel is not None:
            cockpit, own = self._cockpit(), self.server.signer
            compose = lambda _verb, p: kernel(p, cockpit, own)   # noqa: E731
            try:
                # levain's own request may be the first this queue ever holds: the bindings' lock lives in its directory
                sign_dir(own.queue_dir).mkdir(mode=0o700, parents=True, exist_ok=True)
            except OSError as exc:
                return {"error": "unavailable", "message": f"the sign directory cannot be made ({exc}): nothing was "
                        "filed; send again"}, 503
        if compose is None:
            return {"error": "not_found", "message": f"this cockpit files no sign request for {spec.name!r}"}, 404
        if not isinstance(params, dict):
            return {"error": "bad_request", "message": "'params' must be a JSON object"}, 400
        bad = sorted(set(params) - set(spec.fields)) if spec.fields is not None else []
        if SIGN_PARAM in params:
            bad = sorted(set(bad) | {SIGN_PARAM})      # what is signed is the message, never a request id
        if bad:
            return {"error": "field_not_allowed", "message": f"{spec.name!r} does not take {bad}"}, 400
        try:
            fp = _params_fingerprint(params)
        except (TypeError, ValueError):
            return {"error": "bad_request", "message": "'params' must be plain JSON (no NaN or Infinity)"}, 400
        signer = self.server.signer
        if signer is None:      # make_server refuses a compose_signer without one; an explicit check survives `python -O`
            return {"error": "not_found", "message": "no such route"}, 404
        inflight = self.server.compose_inflight
        wake = self.server.compose_wake
        key = (spec.name, txn)
        conflict = {"error": "txn_conflict", "message": "this send's id already names other words: nothing was "
                    "filed; send again"}, 409
        deadline = time.monotonic() + _COMPOSE_WAIT_S
        with wake:
            while True:
                held = inflight.get(key)
                if held is None:
                    break
                if held != fp:
                    return conflict
                left = deadline - time.monotonic()      # the same draft's compose is filing: wait for its answer
                if left <= 0:
                    return {"error": "busy", "message": "this draft is still being filed: nothing more was filed; "
                            "send again"}, 503
                wake.wait(left)
            try:
                bound = draft_bindings(signer.queue_dir, spec.name, txn)     # 429 for a new draft past the cap
            except SignRefused as exc:
                message = str(exc) if exc.status == 429 else f"{exc}: nothing was filed; send again"
                return {"error": error_word(exc.status), "message": message}, exc.status
            if any(b["params"] != fp for b in bound):
                return conflict
            for b in reversed(bound):           # the newest of this draft's requests that is still open
                row = signer.row(b["request"], [])
                if row is not None and not row.get("refused"):
                    return {"request": b["request"]}, 200
            inflight[key] = fp      # this compose's in-flight entry; only its owner removes it (the finally below)

        try:
            try:
                rid = compose(spec.name, dict(params))
            except ValueError as exc:
                return {"error": "bad_request", "message": str(exc) or "the message cannot be signed"}, 400
            if not request_id_ok(rid):
                return {"error": "internal", "message": "the compose signer named no sign request"}, 500
            try:
                bind_compose(signer.queue_dir, spec.name, txn, fp, rid)
            except SignRefused as exc:
                return {"error": error_word(exc.status), "message": f"sign request {rid} was filed but which draft "
                        f"filed it could not be recorded ({exc}): it will not be sent; send again"}, exc.status
            return {"request": rid}, 200
        finally:
            with wake:              # after the bind (or its refusal): the same draft's waiters then read the bindings
                del inflight[key]
                wake.notify_all()

    def _signed_for(self, verb: str, request_id: str, params: "dict[str, Any]") -> "str | None":
        """The kernel's sign check (``levain.cockpit.verbs.SignedFor``): the draft id (``client_txn``) under which a
        compose on this Signer's queue filed ``request_id`` for ``verb`` and exactly ``params``, when the Signer holds
        that request signed; else None. It reads the durable binding (``levain.consent.sign.compose_bindings``), so a
        restart or any number of other drafts since never turns the retry of a send that landed into a refusal: the
        at-most-once gate behind this check replays it. A binding file that cannot be read is an ``unavailable``
        (503), never a refusal: a refusal drops the page's signed draft, and its re-sign would send a second time."""
        from levain.consent.sign import SignRefused, compose_bindings, state_of

        signer = self.server.signer
        if signer is None or not isinstance(request_id, str):
            return None
        try:
            fp = _params_fingerprint(params)
        except (TypeError, ValueError):
            return None
        try:
            bindings = compose_bindings(signer.queue_dir)
        except SignRefused as exc:
            raise EditError("unavailable", 503, f"which sign request this draft filed could not be read ({exc}): "
                            "nothing was sent; send again") from None
        txn = next((b["txn"] for b in bindings
                    if b["verb"] == verb and b["request"] == request_id and b["params"] == fp), None)
        return txn if txn is not None and state_of(signer.queue_dir, request_id) == "signed" else None

    def _serve_note_body(self, path: str, *, head: bool) -> None:
        """``GET /cockpit/note/<n>.json``: one note's body (what, detail, agent, filed_at, and its dismissal if any),
        behind exactly the question body's gate. Read-only: a bad id or no such note is 404, no ``notes.db`` is made."""
        if not self._off_box_token_valid():
            self._send(json.dumps({"error": "forbidden", "message": (
                f"a note's words need this page's write token, sent as {_WRITE_TOKEN_HEADER}")}
            ).encode("utf-8"), "application/json; charset=utf-8", status=403, head=head,
                headers=[("Vary", _WRITE_TOKEN_HEADER)], cache_control="private, no-store")
            return
        n = urllib.parse.unquote(path[len(_NOTE_BODY_PREFIX):-len(_CONSENT_BODY_SUFFIX)])
        status, payload = 404, {"error": "not_found", "message": "no such note"}
        store = getattr(self.server, "note_store", None)
        if store is not None and _QUESTION_ID.fullmatch(n):
            if not self.server.request_gate.acquire(blocking=False):
                self._send(b"busy\n", "text/plain; charset=utf-8", status=503, head=head)
                return
            try:
                note = store.get(int(n))
                if note is not None:
                    d = note.dismissed
                    status, payload = 200, {
                        "n": note.n, "agent": note.agent, "what": note.what, "detail": note.detail,
                        "filed_at": note.filed_at,
                        "dismissed": None if d is None else {"channel": d.channel, "dismissed_at": d.dismissed_at}}
            except _CredentialUnavailable:   # RU-5: only the outer catch answers it
                raise
            except Exception as exc:  # noqa: BLE001 - a store that cannot be read is an answer, never a dead connection
                status, payload = 500, _store_fault("note", exc)
            finally:
                self.server.request_gate.release()
        self._send(json.dumps(payload).encode("utf-8"), "application/json; charset=utf-8", status=status, head=head,
                   headers=[("Vary", _WRITE_TOKEN_HEADER)], cache_control="private, no-store")

    def _serve_threads(self, path: str, *, head: bool) -> None:
        """``GET /cockpit/threads.json`` and ``GET /cockpit/thread/<th-id>.json`` (CH-U1a): the coordinator's threads and
        one thread's stream, read-only (:func:`levain.cockpit.threads.serve_threads`). A thread carries the operator's
        lines and the agent's words and tool calls, so it rides the consent body's gate: reached only through
        ``_route``, then a surface token on every bind, before the store is opened. ``dispatch.db`` lives in the consent
        directory, resolved as the egress reading resolves it: a reader whose ``directory`` is None reads the default
        queue, as the egress reading does; a reader with no ``directory`` attribute reads nothing."""
        if not self._off_box_token_valid():
            self._send(json.dumps({"error": "forbidden", "message": (
                f"the thread record needs this page's write token, sent as {_WRITE_TOKEN_HEADER}")}
            ).encode("utf-8"), "application/json; charset=utf-8", status=403, head=head,
                headers=[("Vary", _WRITE_TOKEN_HEADER)], cache_control="private, no-store")
            return
        reader = self.server.consent_reader
        if not hasattr(reader, "directory"):
            status, payload = ((200, {"threads": [], "truncated": False}) if path == cockpit_threads.THREADS_LIST_PATH
                               else (404, {"error": "not_found", "message": "no such thread recorded"}))
        else:
            from levain.consent.queue import default_queue_dir

            if not self.server.request_gate.acquire(blocking=False):
                self._send(b"busy\n", "text/plain; charset=utf-8", status=503, head=head)
                return
            try:
                status, payload = cockpit_threads.serve_threads(
                    path, "", directory=reader.directory if reader.directory is not None else default_queue_dir())
            finally:
                self.server.request_gate.release()
        self._send(cockpit_threads.encode(payload), "application/json; charset=utf-8", status=status, head=head,
                   headers=[("Vary", _WRITE_TOKEN_HEADER)], cache_control="private, no-store")

    def _serve_board(self, *, head: bool) -> None:
        """``GET /cockpit/board.json`` (C12): the ready queue's read-only board (:func:`levain.cockpit.board.build`), behind
        the thread record's gate (a surface token on every bind). Read-only: the coordinator store is opened read-only and
        no live coordinator is needed; a store never made is ``queue: "none"``, one that cannot be read is named in
        ``unreadable``. No POST reaches it (the write routes are a closed set)."""
        if not self._off_box_token_valid():
            self._send(json.dumps({"error": "forbidden", "message": (
                f"the board needs this page's write token, sent as {_WRITE_TOKEN_HEADER}")}
            ).encode("utf-8"), "application/json; charset=utf-8", status=403, head=head,
                headers=[("Vary", _WRITE_TOKEN_HEADER)], cache_control="private, no-store")
            return
        if not self.server.request_gate.acquire(blocking=False):
            self._send(b"busy\n", "text/plain; charset=utf-8", status=503, head=head)
            return
        try:
            reader = self.server.consent_reader
            directory = None
            if hasattr(reader, "directory"):
                from levain.consent.queue import default_queue_dir

                directory = reader.directory if reader.directory is not None else default_queue_dir()
            sheet = getattr(self.server, "coord_sheet", None)
            payload = cockpit_board.build(directory, getattr(self.server, "question_store", None),
                                          drop_dir=sheet.queue_dir if sheet is not None else None)
        except Exception as exc:  # noqa: BLE001 - never a dead connection
            payload = {"error": "board_unreadable", "message": f"the board could not be built ({type(exc).__name__})"}
        finally:
            self.server.request_gate.release()
        self._send(json.dumps(payload).encode("utf-8"), "application/json; charset=utf-8",
                   status=500 if "error" in payload else 200, head=head,
                   headers=[("Vary", _WRITE_TOKEN_HEADER)], cache_control="private, no-store")

    def _serve_session_log(self, path: str, *, head: bool) -> None:
        """``GET /cockpit/session/<stem>.json``: one agent session's transcript (``session_log.read_session``). The
        consent body's gate: reached only through ``_route``, then a surface token on every bind, judged by
        ``_credential``, before the stem is even looked at. Read-only; a bad stem, a missing or foreign record and a
        server with no install root are all 404."""
        if not self._off_box_token_valid():
            self._send(json.dumps({"error": "forbidden", "message": (
                f"a session's transcript needs this page's write token, sent as {_WRITE_TOKEN_HEADER}")}
            ).encode("utf-8"), "application/json; charset=utf-8", status=403, head=head,
                headers=[("Vary", _WRITE_TOKEN_HEADER)], cache_control="private, no-store")
            return
        stem = path[len(_SESSION_LOG_PREFIX):-len(_SESSION_LOG_SUFFIX)]
        status, payload = 404, {"error": "not_found", "message": "no such session recorded"}
        blob: bytes | None = None     # a host reader's body, serialized once; the reader owns the object, not us
        source = self.server.levain_source
        root = source.install_root
        reader = getattr(source, "session_reader", None)
        # A host reader wins over install_root when both are set: a host that supplies one has sessions the
        # install's own log does not hold, and one route must give one answer.
        if reader is not None:
            if _HOST_SESSION_STEM.fullmatch(stem) and ".." not in stem:
                if not self.server.request_gate.acquire(blocking=False):
                    self._send(b"busy\n", "text/plain; charset=utf-8", status=503, head=head)
                    return
                try:
                    # The reader runs inside the request gate, as read_session does, so a host reader must be bounded.
                    body = reader(stem, datetime.now(timezone.utc))
                    if body is not None:
                        if not isinstance(body, dict) or not isinstance(body.get("events"), list):
                            raise TypeError("a session reader's body must be a dict whose events is a list")
                        # These bytes are what goes out: the object may change after this, and NaN or Infinity
                        # would make a body a browser's JSON.parse rejects. Either failure is a failed read.
                        blob = json.dumps(body, allow_nan=False).encode("utf-8")
                        status = 200
                except _CredentialUnavailable:   # RU-5: only the outer catch answers it
                    raise
                except Exception as exc:  # noqa: BLE001 - a log that cannot be read is an answer, never a dead connection
                    _log.warning("session transcript read failed: %s: %s", type(exc).__name__, exc)
                    status, payload = 500, {"error": "internal", "message": "the session's transcript could not be read"}
                finally:
                    self.server.request_gate.release()
        elif root is not None:     # the stem is checked by read_session (its STEM), the one place it is
            if not self.server.request_gate.acquire(blocking=False):
                self._send(b"busy\n", "text/plain; charset=utf-8", status=503, head=head)
                return
            try:
                from levain.cockpit.session_log import read_session

                body = read_session(root, stem, datetime.now(timezone.utc), self.server.consent_reader)
                if body is not None:
                    status, payload = 200, body
            except _CredentialUnavailable:   # RU-5: only the outer catch answers it
                raise
            except Exception as exc:  # noqa: BLE001 - a log that cannot be read is an answer, never a dead connection
                _log.warning("session transcript read failed: %s: %s", type(exc).__name__, exc)
                status, payload = 500, {"error": "internal", "message": "the session's transcript could not be read"}
            finally:
                self.server.request_gate.release()
        if blob is None:
            blob = json.dumps(payload).encode("utf-8")
        self._send(blob, "application/json; charset=utf-8", status=status, head=head,
                   headers=[("Vary", _WRITE_TOKEN_HEADER)], cache_control="private, no-store")

    def _acquire_read_slot(self) -> bool:
        """Take a read-pool slot and a ``request_gate`` slot for a cockpit read, waiting up to
        ``_READ_WAIT_SECONDS`` behind at most ``_MAX_READ_WAITERS`` other waiting reads. A read does not take
        the fast path while others wait, so it cannot barge past them. False (the caller answers 503) when
        the wait times out or the waiting room is full. Pair with ``_release_read_slot``."""
        srv = self.server
        deadline = time.monotonic() + _READ_WAIT_SECONDS
        with srv.read_waiters_lock:
            if srv.read_waiters == 0 and srv.read_gate.acquire(blocking=False):
                queued = False
            elif srv.read_waiters >= _MAX_READ_WAITERS:
                return False
            else:
                srv.read_waiters += 1
                queued = True
        if queued:
            try:
                got = srv.read_gate.acquire(timeout=_READ_WAIT_SECONDS)
            finally:
                with srv.read_waiters_lock:
                    srv.read_waiters -= 1
            if not got:
                return False
        if srv.request_gate.acquire(timeout=max(0.0, deadline - time.monotonic())):
            return True
        srv.read_gate.release()
        return False

    def _release_read_slot(self) -> None:
        self.server.request_gate.release()
        self.server.read_gate.release()

    def _peer_open(self) -> bool:
        """False once the client has closed its end. A stream never reads the request body, so any readable
        byte-less state is the peer hanging up."""
        try:
            readable, _, _ = select.select([self.connection], [], [], 0)
            return not readable or self.connection.recv(1, socket.MSG_PEEK) != b""
        except (OSError, ValueError):
            return False

    def _stream_allowed(self) -> "Callable[[bool], bool] | None":
        """The change stream's per-pass check for a PAIRED principal (RU-3, RT-2 as amended): the revocation epoch is
        compared on every pass (an int read); a changed one re-runs the device check, as does every pass once a
        heartbeat interval has passed since the last full check, the data path included (G2; so a revoke made by
        another process, ``levain cockpit pair --revoke-all``, ends the stream within one heartbeat even under a
        steady event flow). At most the one frame already past its check when the revoke commits is written (G3).
        Any other principal: None (a surface token is never revoked from the panel)."""
        cred = self._credential()
        name = cred.get("name") if cred.get("class") == "token" else None
        if not isinstance(name, str) or not name.startswith(PAIRED_PREFIX):
            return None
        srv, device = self.server, name[len(PAIRED_PREFIX):]
        seen = [self._pair_epoch_seen]

        def allowed(recheck: bool) -> bool:
            epoch = srv.pair_epoch
            if epoch == seen[0] and not recheck:
                return True
            seen[0] = epoch
            broker = srv.broker
            db = broker.active_db() if broker is not None else None
            try:
                return db is not None and db.device_live(device)
            except Exception:  # noqa: BLE001 - a check that cannot run ends the stream; the page reconnects
                return False

        return allowed

    def _serve_pair_list(self, *, head: bool) -> None:
        """``GET /cockpit/pair/devices.json``: the paired devices (never a hash), behind a token on every bind."""
        if not self._off_box_token_valid():
            self.close_connection = True
            self._send(json.dumps({"error": "credential_required", "message": "missing or invalid write token"})
                       .encode("utf-8"), "application/json; charset=utf-8", status=403, head=head)
            return
        payload, status = self._pair_call(lambda db: {"devices": db.list_devices()})
        self._pair_send(payload, status, head=head)

    def _pair_send(self, payload: "dict[str, Any]", status: int, *, head: bool = False,
                   cache_control: "str | None" = None) -> None:
        """A pair route's JSON answer; every 503 carries ``Retry-After: 2`` (F5)."""
        self._send(json.dumps(payload).encode("utf-8"), "application/json; charset=utf-8", status=status, head=head,
                   headers=[("Retry-After", "2")] if status == 503 else None, cache_control=cache_control)

    def _pair_refuse(self, status: int, error: str, message: str) -> None:
        """A pair POST refused before its body is read: closed at once, the body never read (G1: draining an
        unauthenticated or busy request's body outside any gate let a slow-body client hold a server thread; a
        client may see a TCP RST before the refusal, and the page's one retry covers it), ``Retry-After`` on a 503
        (F5)."""
        self.close_connection = True
        self._pair_send({"error": error, "message": message}, status)

    def _pair_call(self, fn: "Callable[[Any], dict[str, Any]]") -> "tuple[dict[str, Any], int]":
        """Run ``fn(journal)`` for a pair route: 404 where pairing cannot exist (RP-3: no ledger_root, a read-only
        source), the broker's refusal where it is disabled, 503 while it is transient (after the RU-4 wait), 500
        ``internal`` on any other fault (F7), like every other handler."""
        broker = self.server.broker
        if broker is None:
            return {"error": "not_found", "message": "pairing needs a writable cockpit"}, 404
        try:
            broker.start_async()
        except Exception:  # noqa: BLE001 - G5: a starter thread that cannot start is the controlled 503
            return {"error": "broker_unavailable", "message": "the cockpit broker could not start; try again"}, 503
        state = broker.wait_settled(_PAIR_WAIT_S)
        db = broker.active_db() if state is Lifecycle.ACTIVE else None
        if db is None:
            if state is Lifecycle.INSTALL_DISABLED:
                return {"error": "install_disabled", "message": f"write-disabled: {broker.disabled_reason}"}, 403
            return {"error": "broker_unavailable", "message": f"the cockpit broker is {state.value}"}, 503
        try:
            return fn(db), 200
        except PairingError as exc:
            return {"error": "bad_request", "message": str(exc)}, 400
        except DeviceRevoked as exc:
            return {"error": "credential_required", "message": str(exc)}, 403
        except _CredentialUnavailable:   # RU-5: only the outer catch answers it
            raise
        except Exception as exc:  # noqa: BLE001 - F7: a journal fault is an answer, never a dead connection
            _log.warning("pair route failed: %s: %s", type(exc).__name__, exc)
            return {"error": "internal", "message": type(exc).__name__}, 500

    def _serve_stream(self, *, head: bool) -> None:
        """``GET /cockpit/events``: reached only through ``_route``, so the Host allowlist, the
        cross-site refusal and the off-box token gate have already answered. Past the stream cap: 503."""
        srv = self.server
        # the credential is resolved (and may 503, RT-1) BEFORE any byte of the response is sent
        allowed = self._stream_allowed()
        if head:
            self._send(b"", cockpit_stream.CONTENT_TYPE, head=True)
            return
        if not srv.stream_gate.acquire(blocking=False):
            self._send(b"busy\n", "text/plain; charset=utf-8", status=503)
            return
        sub = None
        try:
            try:
                sub = srv.stream_watcher.subscribe(self.headers.get("Last-Event-ID"))
            except _CredentialUnavailable:   # RU-5: only the outer catch answers it
                raise
            except Exception as exc:  # noqa: BLE001 - the cockpit could not start: an answer, not a dead connection
                self._send(json.dumps({"error": "internal", "message": f"{type(exc).__name__}: {exc}"}).encode("utf-8"),
                           "application/json; charset=utf-8", status=500)
                return
            self.close_connection = True
            self.send_response(200)
            self.send_header("Content-Type", cockpit_stream.CONTENT_TYPE)
            self.send_header("Vary", COCKPIT_VARY)
            self.send_header("X-Accel-Buffering", "no")
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.flush()

            def write(chunk: bytes) -> None:
                self.wfile.write(chunk)
                self.wfile.flush()

            try:
                cockpit_stream.run(write, sub, stop=srv.stream_stop, heartbeat_s=srv.stream_heartbeat_s,
                                   alive=self._peer_open, allowed=allowed)
            except (OSError, ValueError):
                pass        # the client went away (or the socket closed under a shutdown): this stream is over
        finally:
            if sub is not None:
                srv.stream_watcher.unsubscribe(sub)
            srv.stream_gate.release()

    def _route(self, *, head: bool) -> None:
        # Host allowlist (DNS rebinding), then the cross-site refusal: a cross-origin page
        # cannot even TRIGGER the per-request store read. The one exception is the pair page opened as a cross-site
        # top-level navigation (F1): it skips only the cross-site refusal, never the Host allowlist.
        if self._cross_site_pair_navigation():
            if not self._host_ok():
                self._send(b"forbidden\n", "text/plain; charset=utf-8", status=403, head=head)
                return
        elif self._refuse_read(head=head):
            return

        # Strip any query string; route on the bare path against the allowlist.
        path = self.path.split("?", 1)[0]

        # The sign sheet: a static shell, token-free like the app shell (its reads carry the token), and like every
        # sign route 404 without a signer.
        if _sheet_id(path) is not None:
            self._serve_sheet(head=head)
            return
        if _consent_sheet_id(path) is not None:     # the same sheet over a consent request (OS-2)
            self._serve_sheet(head=head, consent=True)
            return
        consent_read = _consent_sheet_read(path)
        if consent_read is not None:                # its read rides the sign listing's gate exactly
            self._serve_consent_sheet(consent_read, head=head)
            return
        if _dispatch_sheet_id(path) is not None:    # the same sheet over a dispatch job (OS-2)
            self._serve_sheet(head=head, dispatch=True)
            return
        if path == _COORD_SHEET:                    # the drop sheet's page (OS-2): the same shell
            self._serve_sheet(head=head, coord=True)
            return
        if path == _COORD_LIST:                     # its read rides the sign listing's gate exactly
            self._serve_coord_sheet(head=head)
            return
        dispatch_read = _dispatch_sheet_read(path)
        if dispatch_read is not None or path == _DISPATCH_LIST:     # its reads ride the sign listing's gate exactly
            self._serve_dispatch_sheet(dispatch_read, head=head)
            return
        # Web push (OS-4), beside a Signer only: the app shell token-free, the reads on the sign reads' own gate
        if self.server.signer is not None and path in _push_static():
            self._serve_push(path, head=head)
            return
        if self.server.signer is not None and path in _PUSH_GET:
            self._serve_push(path, head=head, read=True)
            return
        # The sign listing runs the write gate's own checks (a token on every bind), stricter than the off-box gate
        # below, so it is routed before it; without a signer it is 404 to everyone (the route's name is public).
        if path == _SIGN_LIST_PATH or _sign_one(path) is not None:
            self._serve_sign_list(head=head, one=_sign_one(path))
            return
        if path == _GRANT_LIST_PATH:      # the grants' revoke listing rides that gate exactly (PLAN M17 "9a")
            self._serve_grant_list(head=head)
            return
        if path == _PAIR_LIST:            # PAIR RP-4: an AUTHED pair route, a token on every bind
            self._serve_pair_list(head=head)
            return

        # OFF-BOX READ GATE (spore-220): the substrate-bearing reads must carry the SAME device-held
        # token the writes do WHEN ``_write_token_required``: a PROXIED request (an extra host or a
        # proxy header; writable source or not, L3 r2), or a writable surface bound off-loopback —
        # else any tailnet node reads the full store (``/substrate.json`` / ``/recall.json`` /
        # ``/job.json`` / a downstream ``extra_json`` view) token-free. Same predicate + constant-time
        # compare as ``do_POST`` (the two shared helpers), so a read is never served under weaker auth
        # than a write. Two deliberate carve-outs stay token-free: (1) a read-ONLY mesh bind, not
        # proxied (spore-220: iPad/iPhone VIEWING); (2) the APP-SHELL static assets (the built-in html/css/js + any
        # downstream ``extra_asset``) — they carry NO substrate data, and the browser must load them
        # to supply the token on the data fetches (the bootstrap). Everything else off-box requires
        # the token, so a 404-class path returns a uniform 403 to an unauthed node (no path probing).
        # One exception: the chat routes on a server with no chat host answer 404 here. dashboard_chat.js
        # probes /chat.json with its chat token only and reads 404 as "no chat panel"; chat is never
        # served off loopback (make_server refuses it), and the route names are public, so the 404 tells
        # an unauthed node nothing a probe could learn.
        if path in _CHAT_GET_ROUTES and self.server.chat_host is None:
            self._send(b"not found\n", "text/plain; charset=utf-8", status=404, head=head)
            return
        if (
            self._write_token_required()
            and path not in _ASSETS
            and path not in self.server.extra_assets
            and not self._off_box_token_valid()
        ):
            # JSON body carrying a token message so the frontend distinguishes a token-403 from a
            # Host/cross-site-403 (plain text) and prompts for the token — mirroring the write path's
            # 403-retry (dashboard_boot.js). Served via _send (GET has no unread body to drain).
            # ⚠ CROSS-REPO COUPLING: the frontends (dashboard_boot.js + an embedder's own frontend) match
            # ``/token/i`` on this "token" wording to trigger the prompt — keep the word "token" here
            # AND in do_POST's identical message if ever reworded (spore-129/220, two repos).
            self._send(
                json.dumps(
                    {"error": "forbidden", "message": "missing or invalid write token"}
                ).encode("utf-8"),
                "application/json; charset=utf-8",
                status=403,
                head=head,
            )
            return

        if path.startswith(_CONSENT_BODY_PREFIX) and path.endswith(_CONSENT_BODY_SUFFIX):
            self._serve_consent_body(path, head=head)
            return
        if path.startswith(_QUESTION_BODY_PREFIX) and path.endswith(_CONSENT_BODY_SUFFIX):
            self._serve_question_body(path, head=head)
            return
        if path.startswith(_NOTE_BODY_PREFIX) and path.endswith(_CONSENT_BODY_SUFFIX):
            self._serve_note_body(path, head=head)
            return
        if path.startswith(_SESSION_LOG_PREFIX) and path.endswith(_SESSION_LOG_SUFFIX):
            self._serve_session_log(path, head=head)
            return
        if cockpit_threads.is_threads_path(path):
            self._serve_threads(path, head=head)
            return
        if path == cockpit_board.BOARD_PATH:
            self._serve_board(head=head)
            return

        if is_cockpit_path(path):
            # The shared cockpit's two read routes (levain.cockpit, design §3): they ride this same
            # envelope, so the Host allowlist, cross-site refusal, the off-box token gate above and
            # the concurrency gate all apply with no second path. The change stream holds its slot for
            # its whole life, so it takes its own cap (stream_gate) in place of the request gate.
            if path == EVENTS_PATH:
                self._serve_stream(head=head)
                return
            if not self._acquire_read_slot():
                self._send(b"busy\n", "text/plain; charset=utf-8", status=503, head=head)
                return
            if not self._peer_open():
                # the client gave up during its wait: skip the store read nobody will receive
                self._release_read_slot()
                self.close_connection = True
                return
            try:
                status, body, hdrs = handle_cockpit_get(
                    self._cockpit(), self.path, credential=self._credential(),
                    if_none_match=self.headers.get("If-None-Match"), transport=self._transport(),
                )
            except _CredentialUnavailable:   # RU-5: only the outer catch answers it
                raise
            except Exception as exc:  # noqa: BLE001 - never 500 with a dead connection
                status, hdrs = 500, [("Vary", COCKPIT_VARY)]
                body = json.dumps({"error": "internal", "message": f"{type(exc).__name__}: {exc}"}).encode("utf-8")
            finally:
                self._release_read_slot()
            self._send(body, "application/json; charset=utf-8", status=status, head=head,
                       headers=hdrs, cache_control=COCKPIT_CACHE_CONTROL)
            return

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
                    write_token_required=self._write_token_required() or self._writes_need_credential(),
                    extra_verbs=self.server.extra_verbs)
            except _CredentialUnavailable:   # RU-5: only the outer catch answers it
                raise
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
                    "write_token_required": self._write_token_required() or self._writes_need_credential(),
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
            except _CredentialUnavailable:   # RU-5: only the outer catch answers it
                raise
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

        if path in _CHAT_GET_ROUTES and self.server.chat_host is not None:
            if not self._chat_token_valid():
                self._send_chat(({"error": "chat_token", "message": (
                    f"this route needs the chat token printed when `levain serve --chat` started, "
                    f"sent as {_CHAT_TOKEN_HEADER}")}, 403), head=head)
                return
            self._send_chat(self._chat_get(path, self.server.chat_host), head=head)
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
            except _CredentialUnavailable:   # RU-5: only the outer catch answers it
                raise
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
            except _CredentialUnavailable:   # RU-5: only the outer catch answers it
                raise
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
            if path == _COCKPIT_PAGE:
                self._send_framing((_COCKPIT_PAGE_CSP, "DENY"), self.server.levain_assets[filename], content_type,
                                   head=head)
            else:
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

    def _chat_get(self, path: str, host: ChatHost) -> tuple[dict[str, Any], int]:
        """The chat READ routes. In-memory reads of the host's registry; never raise."""
        from urllib.parse import parse_qs, urlsplit

        try:
            if path == "/chat.json":
                return host.listing(), 200
            ident = parse_qs(urlsplit(self.path).query).get("id", [""])[0].strip()
            if not ident:
                return {"error": "bad_request", "message": "id is required"}, 400
            if path == "/chat/session.json":
                return host.session_status(ident), 200
            return host.job_status(ident), 200
        except ChatError as exc:
            return {"error": exc.code, "message": str(exc)}, exc.http_status
        except _CredentialUnavailable:   # RU-5: only the outer catch answers it
            raise
        except Exception as exc:  # noqa: BLE001 — never leak a traceback to the client
            return {"error": "internal", "message": type(exc).__name__}, 500

    def _chat_post(self, route: str, host: ChatHost, req: Any) -> tuple[dict[str, Any], int]:
        """The chat OPERATIONS. The request names an entity the operator registered, a session id,
        message text or a refusal reason, and nothing else is read from it: no agent, tool spec,
        model or mode is accepted from a client (the binding design's section 7.1)."""
        if not isinstance(req, dict):
            return {"error": "bad_request", "message": "body must be a JSON object"}, 400
        try:
            if route == "/chat/open":
                return host.open(req.get("entity")), 202
            sid = req.get("session_id")
            if route == "/chat/turn":
                return host.turn(sid, req.get("message")), 202
            if route == "/chat/approve":
                return host.approve(sid, req.get("expect")), 202
            if route == "/chat/reject":
                return host.reject(sid, req.get("reason"), req.get("expect")), 202
            return host.close(sid), 200
        except ChatError as exc:
            return {"error": exc.code, "message": str(exc)}, exc.http_status
        except _CredentialUnavailable:   # RU-5: only the outer catch answers it
            raise
        except Exception as exc:  # noqa: BLE001 — never leak a traceback to the client
            return {"error": "internal", "message": type(exc).__name__}, 500

    def _send_chat(self, out: tuple[dict[str, Any], int], *, head: bool = False) -> None:
        payload, status = out
        self._send(json.dumps(payload).encode("utf-8"), "application/json; charset=utf-8",
                   status=status, head=head)

    def _install_class(self) -> str:
        return broker_install_class(self.server)

    def _pending_post(self, pending_id: str, action: str, req: Any) -> dict[str, Any]:
        """``POST /cockpit/pending/<id>/signature`` (K2b §3.2: a signature over the stored bytes) and
        ``.../cancel`` (§3.5: C1 on your own pending, a pending of its tier on another's, each lifted to the
        standing policy's ``pending_cancel`` floor)."""
        if not isinstance(req, dict):
            raise EditError("bad_request", 400, "request must be a JSON object")
        unknown = sorted(set(req) - _PENDING_KEYS[action])
        if unknown:
            raise EditError("bad_request", 400, f"unknown request keys {unknown}")
        credential = self._credential()
        if credential.get("class") not in ("token", "device"):
            raise EditError("credential_required", 403, "a write needs a credential (the write token)")
        self._cockpit()      # the broker starts with the cockpit
        broker = self.server.broker
        if broker is None:
            raise EditError("needs_broker", 403, "this server has no cockpit broker")
        try:
            if action == "signature":
                return broker.submit(pending_id, req.get("signature"))
            return broker.cancel(pending_id, credential=credential, intent=req.get("intent"),
                                 idempotency_key=req.get("idempotency_key"))
        except BrokerError as exc:
            raise EditError(exc.code, exc.status, str(exc), exc.extra) from exc

    def do_POST(self) -> None:  # noqa: N802 — BaseHTTPRequestHandler contract
        """The POST routes — the governed writes ``/edit`` + ``/action``, and the ``/chat`` operations
        when a chat host is attached — behind one auth boundary. They fork only at dispatch, AFTER the
        shared checks. The two writes refuse with 422 ``read_only`` when the source carries no
        ``write_scope``; chat does not, because it acts through the entity's floor, not the substrate.

        The cheap fail-closed checks (Host → CSRF → Content-Type → route →
        Content-Length) run BEFORE any body read and each closes the connection on
        rejection — so a wrong-Host / wrong-route client never reads a body and can't
        stall a thread there [L3 MED], and no unread body dangles on a kept-alive
        socket. The rate-gate is acquired BEFORE the body read, so the bounded
        read+drain+write phase is itself concurrency-bounded; a slowloris is capped by
        the gate + the 30s socket timeout. The write layer does the actual edit."""
        # Same Host allowlist as reads (DNS rebinding), then CSRF layer 1: a write must come from
        # our own page (same-origin) or a non-browser client (no Sec-Fetch-Site at all).
        if self._refuse_write_origin():
            return
        # The write gate: a write route on a writable surface needs a surface token on every bind
        # (K2a). Every POST for which _write_token_required holds needs it too (spore-129/220; a proxied
        # one whatever the source, L3 r2), with the same constant-time compare as the read gate in _route,
        # so a write is never served under weaker auth than a read. A READ-ONLY surface not proxied (loopback,
        # or its token-free mesh bind) falls through to the 422 'read_only' refusal below, NOT a token-403
        # [codex L3]; a proxied one needs the token first.
        route = self.path.split("?", 1)[0]
        # PAIR (RP-4): the claim is the one credential-less POST, carved out by exact path BEFORE the token gates (on
        # his proxied path every POST would otherwise need a token it cannot have yet); the live code is its
        # credential. The authed pair POSTs need a token on every bind, loopback included.
        if route == _PAIR_CLAIM:
            return self._pair_claim()
        if route in _PAIR_AUTHED_POST:
            if not self._off_box_token_valid():
                return self._pair_refuse(403, "credential_required", "missing or invalid write token")
            return self._pair_authed_post(route)
        if _is_write_route(route) and self._writes_need_credential() and not self._off_box_token_valid():
            # K2a (design §4.3): every write carries a credential on every bind, loopback included;
            # refused before a byte of the body is read
            return self._reject(403, "credential_required", "missing or invalid write token")
        # A sign route (DESIGN_SIGN D4) needs a surface token on every bind whatever the source's write scope: its
        # gesture is the operator's passkey, and the page that carries it must be this cockpit's own.
        sign = _sign_route(route) if self.server.signer is not None else None
        # the compose route (DESIGN_SIGN D5) rides exactly that gate: it files a request the operator's passkey then signs
        # (served beside a Signer, where levain files the request for its own kernel signed verbs (c4-mut R1), or a
        # compose_signer)
        compose = route == _SIGN_COMPOSE and (self.server.signer is not None or self.server.compose_signer is not None)
        # and the grants' passkey revoke (PLAN M17 "9a"): the same ceremony class, the same gate
        grant = grant_route(route, _GRANT_PREFIX) if self.server.grant_phone is not None else None
        # and the consent family's answer and ruling (OS-2): the same ceremony class, the same gate
        consent = _consent_sign_route(route) if self.server.consent_sheet is not None else None
        # and the dispatch family's sign, cancel and authorise (OS-2): the same ceremony class, the same gate
        dispatch = _dispatch_sign_route(route) if self.server.dispatch_sheet is not None else None
        # and the coordinator drop family's drop and envelope review/challenge (OS-2): the same gate
        coord = _coord_route(route) if self.server.coord_sheet is not None else None
        # and the key-line family's key line (OS-2): the same gate
        key_line = route == _KEY_LINE_ROUTE and self.server.signer is not None
        # and web push (OS-4): a token on every bind
        push = route in _PUSH_POST and self.server.signer is not None
        if (sign is not None or compose or grant is not None or consent is not None or dispatch is not None
                or coord is not None or push or key_line) and not self._off_box_token_valid():
            return self._reject(403, "credential_required", "missing or invalid write token")
        if self._write_token_required() and not self._off_box_token_valid():
            return self._reject(403, "forbidden", "missing or invalid write token")
        # CSRF layer 2: require application/json (a cross-origin page cannot send it
        # without a CORS preflight this server never answers).
        if self._refuse_non_json():
            return
        # The write routes: substrate edits (/edit) + governed channel actions (/action).
        # Both ride this ONE auth gate above (Host → CSRF → write token → Content-Type) —
        # /action carries no second, weaker auth path. The write-token check keys on
        # write_scope, and action verbs REQUIRE a write_scope (make_server enforces it), so
        # it already covers /action with no change here.
        is_chat = route in _CHAT_POST_ROUTES and self.server.chat_host is not None
        if (not _is_write_route(route) and not is_chat and sign is None and not compose and grant is None
                and consent is None and dispatch is None and coord is None and not push and not key_line):
            return self._reject(404, "not_found", "no such route")
        if is_chat and not self._chat_token_valid():   # before a byte of the body is read
            return self._reject(403, "chat_token", (
                f"this route needs the chat token printed when `levain serve --chat` started, "
                f"sent as {_CHAT_TOKEN_HEADER}"))
        # Content-Length: required + ASCII digits (checked before reading a byte).
        clen = self._declared_length()
        if clen is None:
            return

        # Acquire the rate-gate BEFORE any body read/drain so the read+write phase is
        # concurrency-bounded — a stalled body can't pile up unbounded threads. [L3 MED]
        # a consent answer or ruling carries a signature the person just made with a passkey: it waits a moment for a
        # slot rather than throw that ceremony away, as on the phone port; everything else answers busy at once
        waits = ((consent is not None and consent[1] in ("answer", "rule"))
                 or (dispatch is not None and dispatch[1] in ("answer", "cancel", "authorise"))
                 # a drop carrying a signed envelope holds a ceremony the person just made, as on the phone port
                 or (coord is not None and coord == "drop"))
        if not (self.server.request_gate.acquire(timeout=5) if waits
                else self.server.request_gate.acquire(blocking=False)):
            return self._reject(503, "busy", "server busy")
        result: dict[str, object] | None = None
        try:
            if self._refuse_oversize(clen, _MAX_POST_BYTES, _DRAIN_CAP):
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
            if is_chat:
                # Chat acts through the ENTITY's own governed floor, not the substrate's write
                # scope, so the read-only refusal below does not apply to it. It rode every
                # check above (Host, CSRF, Content-Type, length, the rate gate).
                host = self.server.chat_host
                if host is None:  # is_chat required it; an explicit check survives `python -O`
                    self._send_json({"error": "not_found", "message": "no such route"}, 404)
                    return
                self._send_chat(self._chat_post(route, host, req))
                return
            if push:
                try:
                    out, status = self._push_post(route, raw)
                except _CredentialUnavailable:   # RU-5: only the outer catch answers it
                    raise
                except Exception as exc:  # noqa: BLE001 — never leak a traceback to the client
                    out, status = {"error": "internal", "message": type(exc).__name__}, 500
                if isinstance(out, bytes):
                    self._send(out, "application/json; charset=utf-8", status=status)
                else:
                    self._send_json(out, status)
                return
            if compose:
                # the caller's compose_signer files the request: like the ceremony, no read-only refusal applies to it
                try:
                    payload, status = self._sign_compose(req)
                except _CredentialUnavailable:   # RU-5: only the outer catch answers it
                    raise
                except Exception as exc:  # noqa: BLE001 — never leak a traceback to the client
                    payload, status = {"error": "internal", "message": type(exc).__name__}, 500
                self._send_json(payload, status)
                return
            if sign is not None:
                # the Signer's own ceremony, not the substrate's write scope: no read-only refusal applies to it
                try:
                    payload, status = self._sign_post(sign[0], sign[1], req)
                except _CredentialUnavailable:   # RU-5: only the outer catch answers it
                    raise
                except Exception as exc:  # noqa: BLE001 — never leak a traceback to the client
                    payload, status = {"error": "internal", "message": type(exc).__name__}, 500
                self._send_json(payload, status)
                return
            if grant is not None:
                # the phone's grant ceremony (the passkey's signature is the authority): no read-only refusal applies
                try:
                    payload, status = self._grant_post(grant[0], grant[1], req)
                except _CredentialUnavailable:   # RU-5: only the outer catch answers it
                    raise
                except Exception as exc:  # noqa: BLE001 — never leak a traceback to the client
                    payload, status = {"error": "internal", "message": type(exc).__name__}, 500
                self._send_json(payload, status)
                return
            if consent is not None:
                # the phone's consent ceremony (the passkey's signature, verified by the queue, is the authority): no
                # read-only refusal applies to it
                try:
                    payload, status = self._consent_post(consent[0], consent[1], req)
                except _CredentialUnavailable:   # RU-5: only the outer catch answers it
                    raise
                except Exception as exc:  # noqa: BLE001 — never leak a traceback to the client
                    payload, status = {"error": "internal", "message": type(exc).__name__}, 500
                self._send_json(payload, status)
                return
            if dispatch is not None:
                # the phone's dispatch ceremony (the passkey's signature, verified by the job store, is the authority):
                # no read-only refusal applies to it
                try:
                    payload, status = self._dispatch_post(dispatch[0], dispatch[1], req)
                except _CredentialUnavailable:   # RU-5: only the outer catch answers it
                    raise
                except Exception as exc:  # noqa: BLE001 — never leak a traceback to the client
                    payload, status = {"error": "internal", "message": type(exc).__name__}, 500
                self._send_json(payload, status)
                return
            if coord is not None:
                # the phone's coordinator routes (the passkey's signature, verified by the coordinator store, is the
                # authority for an envelope): no read-only refusal applies to it
                try:
                    payload, status = self._coord_post(coord, req)
                except _CredentialUnavailable:   # RU-5: only the outer catch answers it
                    raise
                except Exception as exc:  # noqa: BLE001 — never leak a traceback to the client
                    payload, status = {"error": "internal", "message": type(exc).__name__}, 500
                self._send_json(payload, status)
                return
            if key_line:
                # the phone's key-line (records nothing, enrols nothing): no read-only refusal applies to it
                try:
                    payload, status = self._key_line_post(req)
                except _CredentialUnavailable:   # RU-5: only the outer catch answers it
                    raise
                except Exception as exc:  # noqa: BLE001 — never leak a traceback to the client
                    payload, status = {"error": "internal", "message": type(exc).__name__}, 500
                self._send_json(payload, status)
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
                pending = _pending_route(route)
                if pending is not None:
                    result = self._pending_post(pending[0], pending[1], req)
                else:
                    # one dispatch for the cockpit route and both legacy aliases (design §4.3)
                    result = dispatch_verb(
                        registry=self.server.verb_registry, cockpit=self._cockpit(), scope=scope, req=req,
                        credential=self._credential(), install_class=self._install_class(),
                        route={"/edit": "edit", "/action": "action"}.get(route, "cockpit"),
                        job_runtime=self.server.job_runtime, broker=self.server.broker,
                        signed_for=self._signed_for, signer=self.server.signer)
            except EditError as exc:
                self._send_json({**exc.extra, "error": exc.code, "message": str(exc)}, exc.http_status)
            except _CredentialUnavailable:   # RU-5: only the outer catch answers it
                raise
            except Exception as exc:  # noqa: BLE001 — never leak a traceback to the client
                self._send_json({"error": "internal", "message": type(exc).__name__}, 500)
        finally:
            self.server.request_gate.release()
        if result is not None:
            self._send_json(result, 200)

    # -- per-device pairing (PAIR RP-4..RP-7, RQ-7) ------------------------------------------------------------------

    def _pair_body(self) -> "dict[str, Any] | None":
        """A pair POST's JSON object body, read under the caller's semaphore with the 1 KiB cap; None after
        sending the refusal. CSRF layer 2 (JSON only) and the length checks run before a byte is read."""
        if self._refuse_non_json():
            return None
        clen = self._declared_length()
        if clen is None:
            return None
        if self._refuse_oversize(clen, _PAIR_BODY_MAX, _PAIR_BODY_MAX):
            return None
        try:
            raw = self.rfile.read(clen)
        except OSError:
            self.close_connection = True
            return None
        try:
            req = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            self._send_json({"error": "bad_json", "message": "body is not valid JSON"}, 400)
            return None
        if not isinstance(req, dict):
            self._send_json({"error": "bad_request", "message": "request must be a JSON object"}, 400)
            return None
        return req

    def _pair_claim(self) -> None:
        """``POST /cockpit/pair/claim {code}``: a new device spends a live one-time code and is handed its token ONCE
        (``Cache-Control: no-store``, RQ-7). No credential is read (a supplied header is ignored, never verified);
        the code is the credential. Its own 2-slot semaphore (RP-4); the label is the offer's (RP-6); no lockout
        (RP-5); nothing of the body is ever logged."""
        if not self.server.pair_claim_gate.acquire(blocking=False):
            return self._pair_refuse(503, "busy", "server busy")
        try:
            req = self._pair_body()
            if req is None:
                return
            unknown = sorted(set(req) - {"code"})
            if unknown:
                self._send_json({"error": "bad_request", "message": f"unknown request keys {unknown}"}, 400)
                return
            now = datetime.now(timezone.utc)
            payload, status = self._pair_call(lambda db: {"claimed": db.redeem_offer(req.get("code"), now=now)})
            if status == 200:
                claimed = payload["claimed"]
                if claimed is None:
                    payload, status = {"error": "pair_refused",
                                       "message": "this pairing code is not valid: used, expired or unknown"}, 403
                else:
                    payload = {"device_id": claimed[0], "token": claimed[1]}
            if status == 503:
                self.close_connection = True     # F5: the claim's 503 closes, like its pre-body one
            self._pair_send(payload, status, cache_control="no-store")
        finally:
            self.server.pair_claim_gate.release()

    def _pair_authed_post(self, route: str) -> None:
        """``POST /cockpit/pair/offer {label?}`` mints a one-time code minted by THIS credential (RQ-2); ``POST
        /cockpit/pair/revoke {id}`` revokes a paired device and its family (RP-7; any credential, surface or paired,
        may revoke any paired device; the surface tokens are never revocable here). The revocation epoch moves only
        after the revoke's transaction commits (RU-3)."""
        srv = self.server
        if not srv.request_gate.acquire(blocking=False):
            return self._pair_refuse(503, "busy", "server busy")
        try:
            req = self._pair_body()
            if req is None:
                return
            cred = self._credential()
            minted_by = str(cred["name"])
            now = datetime.now(timezone.utc)
            if route == _PAIR_OFFER:
                unknown = sorted(set(req) - {"label"})
                if unknown:
                    self._send_json({"error": "bad_request", "message": f"unknown request keys {unknown}"}, 400)
                    return

                def mint(db: Any) -> dict[str, Any]:
                    code = db.mint_offer(label=req.get("label"), minted_by=minted_by, now=now)
                    return {"code": code, "pair_path": f"/pair#code={code}",
                            "expires_in_s": int(OFFER_TTL.total_seconds())}

                payload, status = self._pair_call(mint)
            else:
                unknown = sorted(set(req) - {"id"})
                if unknown:
                    self._send_json({"error": "bad_request", "message": f"unknown request keys {unknown}"}, 400)
                    return
                payload, status = self._pair_call(
                    lambda db: {"revoked": db.revoke_devices([req.get("id")], by=minted_by, now=now)})
                if status == 200:
                    with srv.pair_epoch_lock:       # after the commit, never inside it (RU-3)
                        srv.pair_epoch += 1
                    note = self._retire_push(list(payload.get("revoked") or []), now)
                    if note is not None:
                        payload["push_note"] = note
                elif payload.get("error") == "bad_request" and "no such device" in payload.get("message", ""):
                    status = 404
            self._pair_send(payload, status, cache_control="no-store")
        finally:
            srv.request_gate.release()


_PENDING_KEYS = {"signature": frozenset({"signature"}),
                 "cancel": frozenset({"intent", "idempotency_key"})}


def _build_broker(source: SubstrateSource, install_class: str | None,
                  httpd: "_LevainHTTPServer",
                  downstream: "Mapping[str, DownstreamEffect] | None" = None) -> "Broker | None":
    """The cockpit broker over a writable source (K2b §4.2, §6): its data root is
    ``<ledger_root>/cockpit``, its stores the source's spores and episodes plus each downstream
    verb's, its tier recompute the kernel's."""
    scope = source.write_scope
    if scope is None:
        return None
    spores, episodes = scope.anneal.spores_json, scope.anneal.episodic_db
    return Broker(scope.ledger_root / "cockpit",
                  stores={SPORES: SporeEffectStore(spores), EPISODES: EpisodeEffectStore(episodes),
                          **({SECTIONS: SectionEffectStore(episodes)} if section_paths_agree(scope.anneal)
                             else {})},
                  install_class=install_class, store_paths=[spores, episodes], downstream=downstream,
                  tier_of=lambda verb, params, target: retier(
                      httpd.verb_registry, httpd.get_cockpit(), verb, dict(params), dict(target)))


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
    chat_host: "ChatHost | None" = None,
    surface_tokens: "Mapping[str, str] | None" = None,
    chat_token: str | None = None,
    cockpit: "Cockpit | None" = None,
    install_class: str | None = None,
    extra_effects: "Mapping[str, DownstreamEffect] | None" = None,
    signer: "Signer | None" = None,
    extra_hosts: "Iterable[str] | None" = None,
    compose_signer: "Callable[[str, dict[str, Any]], str] | None" = None,
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
    ``(content_type, body_bytes)`` (cached, ungated — like the built-in assets); ⚠ SECURITY
    (spore-220): an ``extra_asset`` is served TOKEN-FREE even on an off-box writable bind (it is the
    app shell — the browser must load it to supply the off-box token on the data fetches), so it MUST
    NOT carry substrate data — put any per-request substrate behind ``extra_json``, which the off-box
    read gate covers. This is a REGISTRANT contract the kernel can't enforce on static bytes.
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

    ``extra_effects`` maps a verb in ``extra_verbs`` to its ``DownstreamEffect`` (the EffectStore a
    downstream T2/T3 verb applies through, and its ``canonical_effect``), which the broker fires. One
    naming a verb outside ``extra_verbs`` or a kernel verb, or failing ``Broker.check_downstream``,
    raises ``ValueError`` before binding. A T2/T3 downstream verb with none answers ``not_built``.

    Non-loopback binding (a LAN / Tailscale IP) is governed by the source's nature:
    - a READ-ONLY, NO-INSTALL source (``write_scope is None`` AND ``install_root is None`` —
      e.g. the embedder's self-ops cockpit over a bare anneal store) MAY bind off-loopback: the write
      route 422s and there is no seed/config to expose, so the private mesh (Tailscale's
      WireGuard) is the access boundary and its reads stay token-free (spore-220: iPad viewing).
      That carve-out is for the bare bind only: a PROXIED request (an ``extra_hosts`` Host or a
      proxy header) needs the token whatever the source (L3 r2 on 0867f2c0);
    - a WRITABLE source MAY bind off-loopback ONLY with ``write_token`` (spore-129) — the
      off-box governance factor that replaces loopback-is-auth. Then POST /edit requires the
      ``X-Levain-Write-Token`` header (constant-time compared). Since K2a every write needs
      a surface token on a loopback bind too; there a writable source given neither
      ``write_token`` nor ``surface_tokens`` gets a per-launch browser token. Without a token
      a writable source stays loopback-only;
    - an INSTALL-bearing source stays loopback-only UNCONDITIONALLY (its seed/config is
      operator-private; a token does not relax this — a different concern than spore-129).
    Raises ``ValueError`` (before binding) on a disallowed non-loopback bind. [codex L3 MED]

    ``signer`` (DESIGN_SIGN D4) is a :class:`levain.consent.sign.Signer` the cockpit page signs sign requests
    through: ``GET /cockpit/sign.json`` and ``POST /cockpit/sign/<id>/{challenge,answer,deny}``, each behind the
    write gate on every bind. The caller sets ``signer.page_origin`` to the page this cockpit is served on (an
    ``https://<rp_id>[:port]``, or ``http://localhost:<port>`` on localhost), and a missing or malformed one is a
    ``ValueError`` before binding: an assertion from any other page is refused. The kernel reads no signers file
    and writes no trust root; the Signer's keys are the caller's.

    ``extra_hosts`` names Host values the gate admits beside loopback and the bound address: the https name a
    reverse proxy on this machine forwards (tailscale serve passes the browser's ``Host`` through, port included).
    Each is a bare ``name`` (a port in an entry is refused); the gate compares names only, so a request Host of
    ``name:8443`` is admitted. Anything else is a ``ValueError`` before binding, and so is any entry beside a
    ``chat_host`` (chat is loopback-only), any loopback name or the bound name (already admitted, and naming one would
    make every local request off-box), and extra_hosts on a server with no surface token. A request whose Host names
    one, or that carries a proxy header, is OFF-BOX: it needs the token exactly as on an off-box bind, reads included
    (the app shell and the static assets stay open: they carry no substrate data and no token, and the page asks for
    the token), since the proxy delivers every tailnet request from loopback.

    ``compose_signer`` (DESIGN_SIGN D5) is ``(verb, params) -> request id``: for a verb whose spec declares ``sign``,
    the page posts ``{verb, params, client_txn}`` to ``POST /cockpit/sign/compose`` (the write gate, as the sign
    routes), the callable files a sign request for those params and names it, the page runs the ``signer``'s
    ceremony on it, and only after a signed result posts the verb with the params plus ``sign_request: <id>``. The
    kernel, not the callable, keeps a draft to one request: the same ``(verb, client_txn)`` returns the request it
    filed while that request is open, without calling the callable again. A ``ValueError`` it raises is a 400
    carrying its words. It needs a ``signer`` (else ``ValueError``); without it the route files a request only for a
    kernel signed verb (levain's own composer, ``levain.cockpit.verbs.KERNEL_COMPOSERS``) and is 404 for every other."""
    # Wildcard / public binds are refused for ANY source — a non-loopback bind is for
    # ONE specific private/mesh interface, never every interface or the internet.
    reason = _rejected_bind_host(host)
    if reason:
        raise ValueError(
            f"refusing to bind {host!r}: {reason}. Pass a SPECIFIC private interface "
            "IP (e.g. your Tailscale IP), not a wildcard or a public address."
        )
    if chat_host is not None and not _is_loopback_host(host):
        # A chat host drives entities with hands. Its routes have no off-box auth factor (the write
        # token governs the substrate's write scope, not an entity's), so it is loopback-only.
        raise ValueError(
            f"refusing to bind {host!r}: a server with --chat is loopback-only "
            "(127.0.0.1 / localhost) — chat drives an entity's hands and has no off-box auth."
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
        # the off-box factor that also gates its reads. Without one it stays loopback-only, where
        # make_server mints a per-launch token for its writes.
        # `not write_token` (not `is None`): an EMPTY-string token must also refuse the bind, not
        # bind-then-brick. The per-request gate already treats "" as no-token (`not expected` →
        # 403), so without this an off-box writable bind with write_token="" would pass here and
        # then 403 every write — a silently bricked surface instead of a clean refusal [L2 LOW].
        if source.write_scope is not None and not (write_token or surface_tokens):
            raise ValueError(
                f"refusing to bind {host!r}: a WRITABLE substrate may bind a non-loopback "
                "(private-mesh / Tailscale) address ONLY with a write_token — the off-box "
                "governance factor that replaces the loopback bind as the write auth. Pass "
                "write_token=<shared secret the device holds>, or bind loopback "
                "(127.0.0.1 / localhost), where the server mints a per-launch write token."
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
        if path.lower() in _reserved_lower or path.lower().startswith(_RESERVED_PREFIXES):
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
            if isinstance(spec, VerbSpec):
                continue    # a downstream cockpit verb carrying its own tier_fn; the registry validates it
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

    # A downstream T2/T3 verb's broker effect (K2b §13): an EffectStore and its canonical_effect, per
    # verb in extra_verbs. Refused here, pre-bind; a T2/T3 downstream verb without one is not_built.
    extra_effects = dict(extra_effects or {})
    # The Team zone's verbs (TEAM_VIEW_PLAN T3): a fetch and a join accept over the ledger found from the install
    # root, as the Team zone's panels are. Their names are the kernel's: a caller's verb or effect under one is
    # refused, never overwritten by the merge (nor overwriting it).
    from levain.team.cockpit_verbs import TEAM_VERB_NAMES, team_verbs

    taken = sorted(n for n in TEAM_VERB_NAMES if n in extra_verbs or n in extra_effects)
    if taken:
        raise ValueError(f"refusing extra_verbs/extra_effects {taken}: those names are the Team zone's verbs")
    if source.write_scope is not None and source.install_root is not None:
        team_v, team_e = team_verbs(source.install_root)
        extra_verbs.update(team_v)
        extra_effects.update(team_e)
    stray = sorted(v for v in extra_effects if v not in extra_verbs)
    if stray:
        raise ValueError(f"refusing extra_effects {stray}: each must name a verb in extra_verbs")
    Broker.check_downstream(extra_effects)

    # The sign ceremony's Signer and the proxied names (DESIGN_SIGN D3, D4): refused here, pre-bind.
    if signer is not None:
        from levain.consent.passkey import page_origin_problem
        from levain.consent.sign import Signer as _Signer

        if not isinstance(signer, _Signer):
            raise ValueError("refusing signer: it must be a levain.consent.sign.Signer")
        rp_id = signer.rp_id

        def problem(origin: "str | None") -> "str | None":
            found = (page_origin_problem(rp_id, origin) if origin is not None
                     else "its page_origin is not set (the page this cockpit is served on, which the caller pins)")
            return None if found is None else f"refusing signer: {found}"

        # checked here, pre-bind; pinned (checked again under the Signer's own lock, L3 r2 item 2) only once every
        # other check and the bind have passed, so a refused server leaves the caller's Signer unpinned
        found = problem(signer.page_origin)
        if found is not None:
            raise ValueError(found)
    proxied_hosts = _extra_hosts(extra_hosts)
    redundant = proxied_hosts & (_LOOPBACK_HOSTS | {str(host).rstrip(".").lower()})
    if redundant:
        # the gate admits loopback and the bound name already; named here, every local request would read as off-box
        raise ValueError(f"refusing extra_hosts {sorted(redundant)}: the Host gate already admits loopback and the "
                         "bound name; extra_hosts names only what a proxy on this machine forwards")
    if proxied_hosts and chat_host is not None:
        # chat is loopback-only (above); a name a proxy forwards puts this loopback server on another network
        raise ValueError("refusing extra_hosts with --chat: a proxied name reaches this server from off the box, "
                         "and chat drives an entity's hands with no off-box auth")
    if compose_signer is not None and (signer is None or not callable(compose_signer)):
        # the page signs what compose files through the Signer's ceremony: a request with no ceremony to answer it
        # would leave every signed send waiting on nothing
        raise ValueError("refusing compose_signer: it must be a callable (verb, params) -> request id, beside a signer")

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

    # The verb registry (K2a, design §4.1): the kernel's verbs plus the downstream's. A name that
    # collides with a kernel verb, or a broker head (``harness_approve``), is refused here, pre-bind.
    verb_registry = VerbRegistry(extra_verbs)
    # One write token per surface, required on every bind (design §4.3). ``write_token`` is the
    # ``browser`` surface's for callers that pass one token. A writable source given none gets a
    # per-launch ``browser`` token; ``run_web_server`` prints it and puts it in the unlocked link.
    tokens = {str(k): str(v) for k, v in (surface_tokens or {}).items()}
    if write_token:
        if tokens.get("browser", write_token) != write_token:
            raise ValueError("write_token and surface_tokens['browser'] disagree")
        tokens["browser"] = write_token
    # checked on the merged set, so write_token cannot equal another surface's token
    for name, tok in tokens.items():
        if not name or not tok:
            raise ValueError(f"refusing surface token {name!r}: name and token must be non-empty")
        if name.startswith(PAIRED_PREFIX):
            # PAIR RQ-6: a paired device's credential is named paired-<id>; a surface may never hold that namespace
            raise ValueError(f"refusing surface token {name!r}: the {PAIRED_PREFIX!r} prefix names paired devices")
        if not _HEADER_SAFE_TOKEN.fullmatch(tok):
            # a token travels in a request header and the page stores what this rule admits
            raise ValueError(f"refusing surface token {name!r}: printable ASCII without spaces only")
    if len(set(tokens.values())) != len(tokens):
        raise ValueError("refusing surface tokens: two surfaces share one token")
    if source.write_scope is not None and not tokens:
        tokens["browser"] = secrets.token_urlsafe(32)
    if proxied_hosts and not tokens:
        # a request through a proxied name is off-box and needs a surface token on every route (L3 r2)
        raise ValueError("refusing extra_hosts: every request through a proxied name needs a surface token, reads "
                         "included, and this server has no surface token (a read-only source given no write_token "
                         "or surface_tokens); pass a write_token")
    if signer is not None and not tokens:
        # every sign route needs a surface token (DESIGN_SIGN D4), so a signer here would be a permanent 403
        raise ValueError("refusing signer: this server has no surface token for its sign routes to require (a "
                         "read-only source given no write_token or surface_tokens); pass a write_token")

    assets = {fn: load_web_asset(fn).encode("utf-8") for fn, _ in _ASSETS.values()}
    assets[_SHEET_PAGE] = load_web_asset(_SHEET_PAGE).encode("utf-8")
    httpd = _LevainHTTPServer((host, port), _Handler)
    httpd.levain_source = source
    httpd.levain_assets = assets
    httpd.extra_assets = extra_assets
    httpd.extra_json = extra_json
    httpd.extra_panels = extra_panels
    httpd.extra_verbs = extra_verbs
    httpd.job_runtime = job_runtime
    httpd.chat_host = chat_host
    httpd.signer = signer
    # the page_origin make_server pins (below); nothing is served before then
    httpd.signer_page_origin = None
    httpd.compose_signer = compose_signer
    # The shared cockpit (levain.cockpit). A downstream passes its own (the kernel's providers plus
    # its panels); None means the kernel's default over this substrate, built on first request.
    # Its start() (refresher threads) is the caller's when it passes its own. Nothing is attached to it
    # until every refusal below has passed and the Signer is pinned.
    httpd.cockpit = cockpit
    httpd.verb_registry = verb_registry
    httpd._cockpit_lock = threading.Lock()
    # A chat surface always has a token: the caller's, or a fresh per-launch one.
    httpd.chat_token = (chat_token or secrets.token_urlsafe(32)) if chat_host is not None else None
    # OFF-BOX write auth (spore-129): key the token requirement on the ACTUAL bound address
    # (un-foolable — the real socket, not the requested ``host`` string). It decides whether the
    # READS need a token; every write needs one on every bind (K2a). The bind-refusal above
    # already guaranteed a writable off-loopback source was given one.
    httpd.is_loopback_bind = _is_loopback_host(str(httpd.server_address[0]))
    httpd.write_token = tokens.get("browser")
    httpd.surface_tokens = tokens
    # GENERAL post-bind reality check (codex L3): the pre-bind refusal rejects a wildcard/PUBLIC
    # *requested* host, but the ACTUAL bound address can still be wildcard/public via a resolver
    # surprise (a hosts-file/DNS mapping of a loopback-CLASSIFIED name to such an address) — for
    # ANY source class, including a read-only or token'd one the specific checks below don't cover.
    # Serving the embedder's substrate on 0.0.0.0 / a public IP is the catastrophic case the whole bind
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
    #   (b) a WRITABLE (non-install) source given NO token → it was meant to be a loopback
    #       serve with a minted per-launch token, but resolved off-box, so its substrate reads (and the
    #       /action verbs) would be served off-box without the token the off-box factor requires
    #       (writes still token-gate / 422, but the GET read path does not) [L2 LOW]. A writable
    #       source WITH a token is a legitimate off-box mesh bind — not refused here.
    if not httpd.is_loopback_bind and chat_host is not None:
        bound = str(httpd.server_address[0])
        httpd.server_close()
        raise ValueError(
            f"refusing to serve: requested host {host!r} bound a non-loopback address ({bound}), "
            "and a server with --chat is loopback-only."
        )
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
    if str(httpd.server_address[0]).lower() in proxied_hosts:
        bound = str(httpd.server_address[0])
        httpd.server_close()
        raise ValueError(f"refusing extra_hosts {[bound]}: the Host gate already admits the bound address ({bound})")
    # Allow the loopback names + the exact bound address (covers a 127.0.0.x bind) + the caller's extra_hosts,
    # normalized lowercase to match the Host check; any other Host is refused.
    httpd.allowed_hosts = _LOOPBACK_HOSTS | {str(httpd.server_address[0]).lower()} | proxied_hosts
    httpd.proxied_hosts = proxied_hosts
    # Bound the expensive routes (store read + writes). Cockpit GET reads wait briefly for a read-pool slot (see
    # _acquire_read_slot) and leave _RESERVED_SLOTS free; every other gated route answers 503 past the cap.
    httpd.request_gate = threading.BoundedSemaphore(_MAX_INFLIGHT)
    httpd.read_gate = threading.BoundedSemaphore(max(1, _MAX_INFLIGHT - _RESERVED_SLOTS))
    httpd.read_waiters = 0
    httpd.read_waiters_lock = threading.Lock()
    view = VerbView(verb_registry, cockpit, lambda: broker_install_class(httpd)) if cockpit is not None else None
    pinned = False
    try:
        # K2b §6: `--install-class reported` is the only way to reported; no flag (and no governed
        # marker) leaves the broker write-disabled, answering "run the installer"
        httpd.broker = _build_broker(source, install_class, httpd, extra_effects)
        # one reader over one queue directory for the decisions panel's rows and the consent body route alike
        httpd.consent_reader = _consent_reader()
        from levain.consent.questions import QuestionStore

        # over the reader's own directory only (None is the default queue): a reader without the attribute gets no
        # question store, never the default queue's (as the proposals route, _ConsentReader)
        httpd.question_store = (QuestionStore(httpd.consent_reader.directory)
                                if hasattr(httpd.consent_reader, "directory") else None)
        from levain.consent.notes import NoteStore

        httpd.note_store = (NoteStore(httpd.consent_reader.directory)
                            if hasattr(httpd.consent_reader, "directory") else None)
        if view is not None and callable(getattr(cockpit, "check_verbs", None)):
            cockpit.check_verbs(view)       # the attach's own refusal, run before anything is committed
        if signer is not None:
            # after every refusal and the bind, before the cockpit is touched: pinned under the Signer's own lock with
            # the check above run again there (a page_origin set since is caught), after which no other page_origin
            # can be set while this server holds the pin; refused, nothing is served and the cockpit is as it was
            signer.pin_page_origin(problem)
            pinned = True
            httpd.signer_page_origin = signer.page_origin
            from levain.consent.grant_phone import GrantPhone
            from levain.consent.sign import sign_root

            # the grants' passkey revoke over the Signer's own values: its queue (whose consent hook names the ONE
            # cockpit whose log takes the revoke, as on the phone port), rpId, the page origin just pinned, its keys
            sign_queue = signer.queue_dir
            # the dispatch family (OS-2) over the same values: the dispatch directory IS the consent directory, and the
            # jobs a card lists are read from that store, so no second queue can disagree with it
            httpd.dispatch_sheet = DispatchSheet(sign_queue, signer.rp_id, signer.page_origin, signer)
            httpd.coord_sheet = CoordSheet(sign_queue, signer.rp_id, signer.page_origin, signer)   # and the drop family
            # only when the grant cards this page shows come from that same queue (/cockpit/consent/grants.json reads
            # the consent reader's): two queues could name two cockpits whose grants share ids and instances, and a
            # button on one card must never revoke the other's. Different queues: no grant route, the button copies.
            from levain.consent.queue import default_queue_dir

            reader = httpd.consent_reader
            card_queue = (reader.directory if reader.directory is not None else default_queue_dir()) \
                if hasattr(reader, "directory") else None
            if card_queue is not None and Path(card_queue).resolve() == Path(sign_queue).resolve():
                # the cards' queue is PINNED to the one just compared: a default queue re-resolved per request (a
                # LEVAIN_HOME changed after start) could otherwise move the cards off the queue the revoke is bound to
                reader.directory = Path(card_queue).resolve()
                httpd.grant_phone = GrantPhone(sign_queue, signer.rp_id, signer.page_origin, signer.keys,
                                               lambda: sign_root(sign_queue))
                # the consent family's answer and ruling (OS-2), on the same condition and for the same reason: the
                # consent card a button sits on is read from this queue, the one the answer is verified in
                httpd.consent_sheet = ConsentSheet(sign_queue, signer.rp_id, signer.page_origin, signer)
        if cockpit is not None:
            # one transaction: an attach that raises (a panel registered since check_verbs; a decisions panel the
            # registry refuses) undoes this server's attaches and keeps any other registration
            broker = httpd.broker
            cockpit.attach_server(
                view, httpd.consent_reader,                     # agents waiting on the operator (Q6a)
                capture=broker.capture if broker is not None else None,
                edits=broker.edits if broker is not None else None,
                # a downstream that registered its own "decisions" keeps exactly that panel (K2b-3b §0.4)
                decision_rows=broker.decision_rows if broker is not None else None,
                questions=httpd.question_store,                 # and the questions they ask him (Q-2b)
                notes=httpd.note_store)                         # and what they tell him (notes)
    except BaseException:
        # the cockpit put itself back (attach_server); this server's pin is released: it never served
        try:
            if pinned and signer is not None:
                signer.release_pin()
        finally:
            httpd.server_close()
        raise
    if signer is not None:
        # every fallible startup step has passed: this start REPLACES the published origin (an ineligible one removes
        # the file), kept where a band reader (another process) finds it; server_close withdraws it if still ours
        from levain.consent.cockpit_origin import write_origin

        httpd.published_origin = (signer.queue_dir, signer.page_origin)
        write_origin(signer.queue_dir, signer.page_origin)
    # The names this server serves, beside its cockpit.db, for `levain cockpit policy raise` to check a
    # downstream verb against (levain.cockpit.verbs.SERVED_VERBS_NAME). Written last, so a server refused
    # above records nothing. Unwritable, an earlier server's record is removed so the raise of a downstream
    # verb is refused rather than checked against verbs this server may not serve (the safe direction).
    if httpd.broker is not None:
        try:
            record_served_verbs(httpd.broker.data_root, verb_registry, datetime.now(timezone.utc))
        except OSError as exc:
            _log.warning("cockpit: served verbs not recorded under %s: %s", httpd.broker.data_root, exc)
            try:
                (httpd.broker.data_root / SERVED_VERBS_NAME).unlink(missing_ok=True)
            except OSError as stale:
                _log.warning("cockpit: an earlier served-verbs record may remain: %s", stale)
    return httpd


def _open_browser(url: str, unlocked: str) -> None:
    """Open the cockpit: ``unlocked`` (the URL with the chat token in its fragment) only through the controller that
    keeps it off every command line, else the plain URL (:func:`levain._browser.open_url` holds the rule). Run in a
    child with an allowlisted environment, :func:`levain.launch.open_browser`."""
    from levain.launch import open_browser

    open_browser(url, unlocked)


def run_web_server(
    path: Path,
    *,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    open_browser: bool = True,
    write: bool = False,
    chat: "list[Path] | tuple[Path, ...]" = (),
    model: str = "glm-5.2:cloud",
    base_url: str = "http://localhost:11434",
    api_key: str | None = None,
    max_iterations: int | None = None,
    turn_seconds: float | None = None,
    install_class: str | None = None,
) -> int:
    """``levain serve`` entry point — serve the substrate dashboard on localhost.

    READ-ONLY by default (``write=False``): the served source carries no write scope,
    so every edit affordance is dark and ``POST /edit`` 422s — a pure inspection
    cockpit. ``write=True`` (``levain serve --write``) serves the GOVERNED WRITABLE
    cockpit — State / spore / Tray-Keep / episode edits through the same ``apply_edit``
    seam ``levain tui`` uses. Every write needs the write token this function prints at start
    (with a link that carries it), loopback included, behind the Host/CSRF guards. An install-bearing
    substrate is loopback-only either way (its seed/config is operator-private), so
    ``serve`` never binds off-box — a posture that fits a network surface: read-only
    is the safe default, writes are an explicit opt-in (mirrors the embedder's bridge cockpit).

    ``chat`` (``--chat``, repeatable) also serves the chat routes over those entities
    (:class:`levain.chat.ChatHost`); unlike the dashboard, that drives an agent with hands, read-only
    or not.

    Returns nonzero only if the store is unreachable before the server starts, a ``--chat``
    entity is refused, or the bind fails (e.g. the port is in use) — mirroring ``levain dashboard`` /
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

    chat_host = None
    if chat:
        chat_host, chat_err = _build_chat_host(
            chat, model=model, base_url=base_url, api_key=api_key, max_iterations=max_iterations,
            turn_seconds=turn_seconds)
        if chat_host is None:
            print(chat_err, file=sys.stderr)
            return 1

    try:
        httpd = make_server(source, host=host, port=port, chat_host=chat_host, install_class=install_class)
    except ValueError as exc:  # bind refused — wildcard/public, an install-bearing/writable source off-loopback, or --chat off-loopback
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
    if chat_host is not None:
        names = ", ".join(chat_host.listing()["entities"])
        print(f"  chat: {names} · model {model} · POST /chat/open, /chat/turn; poll /chat/job.json")
        # Flushed: this terminal is where the token is published (and, on macOS, the browser it opens), and stdout is block-buffered
        # when it is not a terminal (a supervisor, a log file), where it would otherwise not appear
        # until the buffer filled. RUN 2026-10-03: piped to a file, the token never showed.
        print(f"  chat token (send as {_CHAT_TOKEN_HEADER}; valid until this server stops): "
              f"{httpd.chat_token}", flush=True)
        # The link carries the token in the URL FRAGMENT, which a browser keeps to itself: it is never sent to a
        # server, so it reaches no access log and no Referer. The panel reads it and strips it from the address bar.
        unlocked = f"{url}#chat_token={httpd.chat_token}"
    else:
        unlocked = url
    if httpd.write_token:
        # K2a: every write carries the browser surface's token, loopback included. Same fragment
        # discipline as the chat token: never sent to the server, taken and stripped by the page.
        print(f"  write token (send as {_WRITE_TOKEN_HEADER}; valid until this server stops): "
              f"{httpd.write_token}", flush=True)
        unlocked += ("&" if "#" in unlocked else "#") + "write_token=" + urllib.parse.quote(httpd.write_token, safe="")
    if unlocked != url:
        print(f"  open the cockpit, unlocked: {unlocked}", flush=True)

    if open_browser:
        # The listening socket is already bound (ThreadingHTTPServer binds in
        # __init__), so the browser's connection queues until serve_forever
        # accepts it — opening before the blocking call is correct.
        _open_browser(url, unlocked)

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped.")
    finally:
        httpd.server_close()
        if chat_host is not None:
            chat_host.shutdown()
    return 0


def _build_chat_host(
    paths: "list[Path] | tuple[Path, ...]",
    *,
    model: str,
    base_url: str,
    api_key: str | None,
    max_iterations: int | None,
    turn_seconds: float | None = None,
) -> "tuple[ChatHost | None, str]":
    """The operator's ``--chat`` entities, checked before the socket is bound. Each must be a clean
    OpenHands entity that chat may host (:func:`levain.chat.chat_refusal`); a client addresses it by
    its directory name, so two with the same name are refused rather than one silently shadowing the
    other."""
    from levain.session import require_openhands_entity

    entities: dict[str, Path] = {}
    for raw in paths:
        entity_dir = Path(raw).expanduser().resolve()
        err = require_openhands_entity(entity_dir)
        if err:
            return None, f"--chat {raw}: {err}"
        refusal = chat_refusal(entity_dir)
        if refusal is not None:
            return None, f"--chat {raw}: {refusal}"
        if entities.get(entity_dir.name) == entity_dir:
            continue   # the same entity passed twice (another spelling of one path)
        if entity_dir.name in entities:
            return None, (
                f"--chat: two entities are named {entity_dir.name!r} ({entities[entity_dir.name]} "
                f"and {entity_dir}); a client addresses an entity by its directory name."
            )
        entities[entity_dir.name] = entity_dir
    return ChatHost(entities, model=model, base_url=base_url, api_key=api_key,
                    max_iterations=max_iterations,
                    turn_seconds=DEFAULT_TURN_SECONDS if turn_seconds is None else turn_seconds), ""

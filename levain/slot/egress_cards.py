"""Host cards (board levain#66, design ASK2_HOST_GRANTS_DESIGN.md, slice S4a): the PURE half of how a refused host
becomes one passkey-signed card and then a grant.

What this module is: a reducer, :meth:`CardFlow.advance` ``(event, wall, mono) -> (flow, [commands])``, plus the words,
the answer reader and the open-card predicate. It holds no process, no file and no clock: the supervisor (slice S4b)
owns every pty, helper run and queue file, turns what happened into an event, and carries out each command the reducer
returns. ``tests/test_slot_egress_cards.py`` pins that with an AST check: this file imports no ``subprocess`` or ``os``
and calls no ``open``.

The flow, as events and commands:

  - events: :class:`Tick`, :class:`Cockpit`, :class:`Polled`, :class:`Listed`, :class:`Scanned`, :class:`Filed`,
    :class:`Answer`, :class:`Delivered`, :class:`RunFailed`.
  - commands: :class:`Poll`, :class:`ListGrants`, :class:`Scan`, :class:`File`, :class:`Push`, :class:`ReadAnswer`,
    :class:`Deliver`. ``Poll``, ``ListGrants`` and ``Deliver`` are root helper runs (``egress-refusals``,
    ``egress-grants-list``, ``egress-grant``); the flow has at most one in flight and a ``Deliver`` preempts a read
    (the supervisor ends the read run first; its :class:`RunFailed` is then ignored). ``Scan``, ``File``, ``Push`` and
    ``ReadAnswer`` are local queue work.

Four concepts, kept apart (plan hgs4_plan_v2 section 4 item 5):

  - proposal provenance: the host comes from root's log of what the front refused, through an honest supervisor. It is
    NOT root-attested: nothing in the signed message says the host was refused.
  - human authorization: the enrolled key's signature over the exact host, the choice and the risk words.
  - root admission: session, store, state, replay, freshness (``egress_verbs.egress_grant``).
  - front confinement: port, SNI, address, expiry, revoke (``levain.broker.front``).

S4 adds no root admission authority. It adds a trusted proposer and delivery implementation and a new attention
surface. Refusal provenance, card authorship, title, requester and the message's ``rp`` are not root-attested. The one
open-card rule below protects the operator's attention; it grants no containment.

THE ONE RULE (it replaces every cap): a card is filed only when no host card is open anywhere in the signing queue,
:func:`open_card_scan`. A denied, removed-early or lapsed-twice host is never carded again this session; a host
is carded again only after its own grant ran out.

THREE FRESHNESS BOUNDS (root's ``FRESH_S`` is measured from the message's ``issued``, not from the tap):

  - B1 signing availability: the request's ttl is :data:`CARD_TTL_S`, so the Signer refuses a tap after filing + 480 s.
  - B2 delivery policy: no ``egress-grant`` STARTS after wall ``issued`` + :data:`START_CUTOFF_S` (570 s); its helper
    deadline of 30 s then ends it by 600 s, and a root process that outlives a killed sudo refuses ``fresh`` itself.
  - B3 root authorization: ``check_fresh`` twice, before the locks and under the ledger lock.

The cutoff is the WALL clock (root's measure); cadence is the monotonic clock. After a sleep the wall cutoff wins.
Every host shown comes from root's log through a host-name check, and every sentence about it is a fixed template: the
module takes no free text, so a card can say only what this file says plus a host, a slot number and a time.

Operator-side only: root runs none of it.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, Mapping, Optional, Tuple

from levain.contracts.namespaces import EGRESS_HOST_NAMESPACE
from levain.slot import egress_grants as eg

NAMESPACE = EGRESS_HOST_NAMESPACE

CARD_TTL_S = 480.0                  # B1: the request's life; well under root's FRESH_S (600)
START_CUTOFF_S = 570.0              # B2: no delivery starts after issued + this (wall)
DELIVER_MAX = 3                     # attempts per card (X5: bounds ssh-keygen verifications)
RETRY_AFTER_S = (10.0, 20.0)        # wall delay before attempt 2 and 3
ANSWER_POLL_S = 2.0                 # a local stat of the queue, no sudo
POLL_S = 10.0                       # the refusals poll
POLL_SLOW_S = 30.0                  # after EMPTY_POLLS_SLOW empty polls in a row (never when a card is open)
EMPTY_POLLS_SLOW = 3
LIST_S = 30.0                       # the grants list
SCAN_RETRY_S = 10.0                 # a scan that found an open card is not repeated sooner
REFUSALS_HOSTS_MAX = 8              # egress_verbs.REFUSALS_HOSTS_MAX: a poll that returned this many polls again at once
LAPSES_MAX = 2                      # a card that lapsed twice is not raised a third time
TITLE_HOST_MAX = 48                 # a longer host shows its last 48 characters, tail first (the part that owns the name)
NOTES_MAX = 32                      # hosts named in the end-notes; the rest as a count
READ_STUCK_S = 30.0                 # a card past its ttl whose answer read has gone unanswered this long has lapsed
CLOCK_DRIFT_S = 60.0                # wall elapsed beyond mono elapsed by this much since filing is "the clock moved"
SESSION_DEADLINE_S = eg.SESSION_TTL_MAX_S   # no interactive wall deadline exists: build_message's min() takes this

_HOST = re.compile(r"[a-z0-9][a-z0-9.-]{2,252}")
_FP = re.compile(r"SHA256:[A-Za-z0-9+/]{43}")
_SIG = re.compile(r"[\n\x20-\x7e]{1,8000}")
_RECORD_MAX = 16 * 1024
_SIG_MAX = 8000

# --- refusal names -------------------------------------------------------------------------------------------------
RETRY, SPENT, ALREADY, OFF, SKEW, OPERATOR, UNSURE, POLL, FRESH, UNENROLLED = (
    "retry", "spent", "already", "off", "skew", "operator", "unsure", "poll", "fresh", "unenrolled")

#: Every name root's host-grant verbs can answer, by class, with the fixed sentence the operator reads. A name that is
#: not here is shown as its (validated) name and treated as spent. The test derives the expected set from the verbs'
#: exported constants plus the literal names, so a new name fails until it is mapped here.
NAMES: Dict[str, Tuple[str, str]] = {
    # retry: the signed answer was not burned
    "egress-grant-not-active": (RETRY, "the session was paused when you tapped"),
    "session-busy": (RETRY, "root was busy with this session"),
    "egress-grant-busy": (RETRY, "root was busy checking another signature"),
    "egress-grant-write-failed": (RETRY, "root could not write the grant file"),
    # spent: this card can never succeed
    "egress-grant-expired": (SPENT, "the grant was already over when it arrived"),
    "egress-grant-used": (SPENT, "root had already used that card"),
    "egress-grant-revoked": (SPENT, "that grant was revoked before it arrived"),
    "egress-grant-sig": (SPENT, "root did not accept the signature"),
    "egress-grant-verifier": (SPENT, "root could not run its signature check"),
    "egress-grant-session": (SPENT, "the card was for another session or slot"),
    "egress-grant-store": (SPENT, "the card named another store"),
    "no-session": (SPENT, "root does not know this session any more"),
    # already allowed another way
    "egress-grant-host-routed": (ALREADY, "that host already has a route, so no grant is needed"),
    "egress-grant-superseded": (ALREADY, "a newer decision for this host stands"),
    # off for the session
    "egress-grant-kind": (OFF, "this session is not one host cards can allow"),
    "egress-grant-class": (OFF, "this session is not one host cards can allow"),
    "egress-grant-not-ready": (OFF, "host grants are not set up for this install"),
    "egress-grant-dir-untrusted": (OFF, "the host-grant setup is damaged: run the install helper's repair"),
    # root is older than this levain (the signed words or the upload list differ): one wasted tap per session at most
    "choice": (SKEW, "root is older than this levain: run the install helper's repair (or setup)"),
    "writable-stale": (SKEW, "root is older than this levain: run the install helper's repair (or setup)"),
    # the operator has something to do
    "egress-grant-ledger-full": (OPERATOR, "root's grant ledger is full"),
    "egress-grant-ledger": (OPERATOR, "root's grant ledger could not be used"),
    "egress-grant-clock": (OPERATOR, "root's clock is behind its own ledger"),
    "too-many": (OPERATOR, "this session already holds the most host grants allowed"),
    "grants-file": (OPERATOR, "root's host grants file is not in its own format"),
    # the grant is in force
    "egress-grant-unsure": (UNSURE, "in force, but it may not survive a power loss"),
    # the refusals poll
    "egress-refusals-log": (POLL, "root could not read the front's log"),
    "egress-refusals-cursor": (POLL, "the log cursor was past the end"),
    # the signed message was not fresh
    "fresh": (FRESH, "root found the card too old"),
    # root's key list
    "egress-grant-unenrolled": (UNENROLLED, "root does not list your key for host grants"),
    # S1 reasons that mean "this card is bad", never retried
    "shape": (SPENT, "root did not accept the card's shape"),
    "host": (SPENT, "root did not accept the host name"),
    "never-grant": (SPENT, "that host is never allowed"),
    "stamp": (SPENT, "a time on the card was not valid"),
    "bound": (SPENT, "the card's times were outside what one tap may allow"),
    "writable": (SPENT, "the card's upload flag was not what root's list says"),
    "build": (SPENT, "the card could not be built"),
}

_NAME_OK = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")


def refusal_class(name: Optional[str]) -> str:
    """The class of a root refusal name: one of the module's class constants; an unmapped name is ``spent``."""
    got = NAMES.get(name) if isinstance(name, str) else None
    return got[0] if got else SPENT


def refusal_words(name: Optional[str]) -> str:
    """The fixed sentence for a root refusal name. An unmapped name is shown as its own name only when it is a plain
    lowercase token (root's names are), else as nothing: never a byte root or the slot supplied beyond that."""
    if isinstance(name, str) and name in NAMES:
        return NAMES[name][1]
    if isinstance(name, str) and _NAME_OK.fullmatch(name):
        return "root answered " + name
    return "root answered something unreadable"


# --- the words -----------------------------------------------------------------------------------------------------
def shown_host(host: str) -> str:
    """The host as the title and the push's first line show it: whole when short, else an ellipsis and its LAST
    :data:`TITLE_HOST_MAX` characters (a prefix cut would drop the domain that owns the name). The full host is in the
    signed message the Sign sheet shows."""
    return host if len(host) <= TITLE_HOST_MAX else "…" + host[-TITLE_HOST_MAX:]


def title(host: str, slot: int) -> str:
    """The sign request's title (at most 200 characters; ``sender.spawn`` cuts a push title at 300)."""
    return f"Allow {shown_host(host)}? (egress slot {int(slot)})"


def push_body(host: str, slot: int, expires: datetime) -> str:
    """The push's body: fixed lines, the host tail-first, the slot, the option texts as a reading aid and the time the
    card stops being signable. It claims no authorship: the card is unsigned by its filer and attests nothing about
    who raised it."""
    when = expires.astimezone(timezone.utc).strftime("%H:%M UTC")
    return "\n".join((
        f"An agent in egress slot {int(slot)} was refused {shown_host(host)}.",
        "Allowing a host lets the agent send anything it can read to any account on it; Levain cannot see or limit "
        "what is sent. An allow-list names hosts, not accounts.",
        "(s) " + eg.option_text(eg.CHOICE_SESSION),
        "(d) " + eg.option_text(eg.CHOICE_DAY),
        f"Tap by {when}. After you allow it the agent has to try again itself.",
    ))


def queue_rp(origin: Optional[str]) -> str:
    """The passkey site of the cockpit a queue has published: ``origin`` is ``cockpit_origin.read_origin``'s result (a
    URL or None). :class:`ValueError` when there is none. ``tests/test_slot_egress_cards.py`` pins this equal to
    ``phone_config.queue_rp`` over a table of origins, so the two cannot drift."""
    from urllib.parse import urlsplit

    host = urlsplit(origin).hostname if origin else None
    if not _passkey_host(host):
        raise ValueError("the signing queue has published no cockpit origin, so no passkey site is known "
                         "(start the cockpit)")
    return str(host)


_RP_ID = re.compile(r"(?=.{1,253}\Z)[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*")


def _passkey_host(host: object) -> bool:
    # levain.consent.passkey.is_host_name restated (this module keeps the cockpit and passkey code out of its imports);
    # tests/test_slot_egress_cards.py compares it to is_host_name over a table, so the two cannot drift.
    return isinstance(host, str) and _RP_ID.fullmatch(host) is not None and not host.rsplit(".", 1)[-1].isdigit()


# --- reading the answer --------------------------------------------------------------------------------------------
def read_signed(record_bytes: bytes, sig_bytes: bytes, message: str) -> Optional[Dict[str, str]]:
    """``{message, sig, signer_fp, choice}`` (what ``egress-grant`` reads on stdin) from a card's ``<rid>.signed.json``
    and ``<rid>.sig`` bytes, or None. The supervisor reads the two files (never through a symlink, bounded); this is
    pure over the bytes. A cheap guard before a sudo call, NOT the trust gate (root verifies the signature):

      - the record is in this namespace and its ``signed_message`` is exactly ``eg.signed_bytes(message, choice)`` for
        the card's own ``message`` and the choice it names (a record signed over other bytes is not delivered), and
        its ``message_sha256``, when present, is that text's;
      - the choice is ``s`` or ``d``, the fingerprint and the signature have the shapes root's grammar takes."""
    try:
        if len(record_bytes) > _RECORD_MAX or len(sig_bytes) > _SIG_MAX:
            return None
        record = json.loads(record_bytes.decode("utf-8"))
        sig = sig_bytes.decode("ascii")
        if not isinstance(record, dict) or record.get("namespace") != NAMESPACE:
            return None
        choice, fp, signed_message = record.get("choice"), record.get("signer_fp"), record.get("signed_message")
        if choice not in eg.CHOICES or not isinstance(fp, str) or _FP.fullmatch(fp) is None:
            return None
        want = eg.signed_bytes(message, choice)
        if signed_message != want.decode("utf-8"):
            return None
        digest = record.get("message_sha256")
        if digest is not None and digest != hashlib.sha256(want).hexdigest():
            return None
        if _SIG.fullmatch(sig) is None:
            return None
    except (ValueError, UnicodeError, RecursionError, eg.GrantRefused):
        return None
    return {"message": message, "sig": sig, "signer_fp": fp, "choice": str(choice)}


def stdin_bytes(payload: Mapping[str, str]) -> bytes:
    """The canonical stdin for ``egress-grant``: exactly ``message``, ``sig``, ``signer_fp`` and ``choice``."""
    return json.dumps({k: payload[k] for k in ("message", "sig", "signer_fp", "choice")}, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")


# --- the open-card predicate ---------------------------------------------------------------------------------------
def open_card_scan(entries: Iterable[Tuple[str, float, Any, str]], wall: float, ttl_s: float = CARD_TTL_S) -> bool:
    """True when a host card is open anywhere in the signing queue: the ONE rule that bounds attention.

    ``entries`` are ``(name, mtime, request, state)`` the supervisor read from the sign dir: the request file's name
    and mtime (epoch seconds), its :class:`levain.consent.sign.SignRequest` (or None when unreadable) and
    ``sign.state_of``'s answer. A card counts when it is in this namespace, filed by levain, not refused, ``open`` and
    not past its ``expires_at``. Only files modified within the last ``ttl_s`` are looked at: S4 is the only filer of
    this namespace and its ttl is fixed, so an open card is never older. A refused request is not counted: it can
    never be signed, so it asks nothing of the operator."""
    now = datetime.fromtimestamp(wall, tz=timezone.utc)
    for _name, mtime, request, state in entries:
        if wall - float(mtime) > ttl_s or request is None or state != "open":
            continue
        if (getattr(request, "namespace", None) == NAMESPACE and getattr(request, "requester", None) == "levain"
                and getattr(request, "refused", "x") is None):
            expires = getattr(request, "expires_at", None)
            if expires is not None and expires > now:
                return True
    return False


# --- events and commands -------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class Tick:
    """Time passed: start whatever is due."""


@dataclass(frozen=True)
class Cockpit:
    """The queue's passkey site now (None: no cockpit is running, so no card can be filed)."""
    rp: Optional[str]


@dataclass(frozen=True)
class Polled:
    """``egress-refusals`` answered: the hosts (at most 8) and the cursor to send next."""
    hosts: Tuple[str, ...]
    cursor: object


@dataclass(frozen=True)
class Listed:
    """``egress-grants-list`` answered ok: root's store id and the live entries ``(grant, host, expires, choice)``."""
    store: str
    entries: Tuple[Tuple[str, str, datetime, str], ...]


@dataclass(frozen=True)
class Scanned:
    """The open-card scan's answer (:func:`open_card_scan` over the queue)."""
    open_anywhere: bool


@dataclass(frozen=True)
class Filed:
    """The card is in the queue; ``phone_live`` is ``webpush.live`` at filing."""
    rid: str
    phone_live: bool


@dataclass(frozen=True)
class Answer:
    """What the queue says of card ``rid`` now: ``open``, ``denied`` or ``signed`` (``payload`` is
    :func:`read_signed`'s result, None if the signed record is not usable)."""
    rid: str
    state: str
    payload: Optional[Dict[str, str]] = None


@dataclass(frozen=True)
class Delivered:
    """``egress-grant`` answered. ``name`` None is an ok answer and ``result`` its dict ``{grant, host, expires,
    choice}``; a refusal carries its name."""
    name: Optional[str]
    result: Optional[Dict[str, Any]] = None


@dataclass(frozen=True)
class RunFailed:
    """A run or a local action ended without an answer. ``verb`` is the helper verb or ``file``; ``name`` is the root
    refusal name when root refused, None when the run crashed, timed out or was ended."""
    verb: str
    name: Optional[str] = None


@dataclass(frozen=True)
class Poll:
    cursor: object


@dataclass(frozen=True)
class ListGrants:
    pass


@dataclass(frozen=True)
class Scan:
    pass


@dataclass(frozen=True)
class File:
    message: str
    title: str
    ttl_s: float


@dataclass(frozen=True)
class Push:
    rid: str
    title: str
    body: str
    phone_live: bool


@dataclass(frozen=True)
class ReadAnswer:
    rid: str


@dataclass(frozen=True)
class Deliver:
    payload: Dict[str, str]
    start_by: float                  # wall epoch seconds: it must START by then (B2)


Command = object
REFUSALS, GRANTS_LIST, GRANT = "egress-refusals", "egress-grants-list", "egress-grant"

# --- per-host and per-card state ------------------------------------------------------------------------------------
WAITING, CARDED, GRANTED, DENIED, REMOVED, LAPSED, DEAD = (
    "waiting", "carded", "granted", "denied", "removed", "lapsed", "dead")


@dataclass
class _Host:
    state: str = WAITING
    polls: int = 0                     # polls this host was seen in (the order the waiting queue takes)
    seq: int = 0                       # first-seen order, the tie-break
    lapses: int = 0
    expires: Optional[datetime] = None
    outcome: Optional[str] = None      # a short key the end-notes render
    detail: str = ""                   # a fixed sentence (a refusal_words text), never anything supplied


@dataclass
class _Card:
    host: str
    grant: str
    message: str
    issued: datetime
    stage: str                         # scan | file | open | delivering
    filed_mono: float = 0.0
    rid: Optional[str] = None
    phone_live: Optional[bool] = None
    payload: Optional[Dict[str, str]] = None
    attempts: int = 0
    retry_at: Optional[float] = None   # wall
    next_answer: float = 0.0           # mono
    read_since: Optional[float] = None  # mono: when the oldest unanswered ReadAnswer was asked
    started: bool = False              # a delivery has begun (a timeout then leaves the outcome unknown)
    unknown: bool = False

    @property
    def issued_ts(self) -> float:
        return self.issued.timestamp()


@dataclass
class CardFlow:
    """One session's card flow. Immutable from outside: :meth:`advance` returns a NEW flow and the commands.

    ``session`` and ``slot`` are the supervisor's own (``s-<16 hex>``, the slot index); ``rp`` is the queue's passkey
    site (None: no cockpit, so hosts wait with a note)."""
    session: str
    slot: int
    rp: Optional[str] = None
    store: Optional[str] = None
    cursor: object = 0
    off: Optional[str] = None
    hosts: Dict[str, _Host] = field(default_factory=dict)
    card: Optional[_Card] = None
    inflight: Optional[str] = None
    live: Dict[str, datetime] = field(default_factory=dict)
    prior: Dict[str, datetime] = field(default_factory=dict)
    listed_mono: Optional[float] = None
    next_poll: float = 0.0
    next_scan: float = 0.0
    empty_polls: int = 0
    seq: int = 0
    pushed: Optional[bool] = None      # whether any card reached a live phone (None: none was filed)
    poll_errors: Dict[str, int] = field(default_factory=dict)
    cards_filed: int = 0

    # -- the reducer ---------------------------------------------------------------------------------------------
    def advance(self, event: object, wall: float, mono: float) -> Tuple["CardFlow", list[Command]]:
        """``(flow, commands)`` after ``event`` at wall time ``wall`` (epoch seconds, UTC: root's measure) and
        monotonic time ``mono``. Never mutates ``self``, never raises for an event it does not know, and returns no
        command once the flow is off."""
        new = copy.deepcopy(self)
        cmds: list[Command] = []
        if isinstance(event, Cockpit):
            new.rp = event.rp
        elif isinstance(event, Polled):
            new._polled(event, wall, mono)
        elif isinstance(event, Listed):
            new._listed(event, wall, mono)
        elif isinstance(event, Scanned):
            new._scanned(event, wall, mono, cmds)
        elif isinstance(event, Filed):
            new._filed(event, mono, cmds)
        elif isinstance(event, Answer):
            new._answer(event, wall, mono, cmds)
        elif isinstance(event, Delivered):
            new._delivered(event, wall, mono, cmds)
        elif isinstance(event, RunFailed):
            new._failed(event, wall, mono, cmds)
        elif isinstance(event, Tick):
            pass
        if new.off is not None:
            return new, []
        new._lapse(wall, mono)
        new._due(wall, mono, cmds)
        return new, cmds

    # -- events ----------------------------------------------------------------------------------------------------
    def _turn_off(self, why: str) -> None:
        if self.off is None:
            self.off = why

    def _polled(self, ev: Polled, wall: float, mono: float) -> None:
        if self.inflight == REFUSALS:
            self.inflight = None
        self.cursor = ev.cursor
        seen = []
        for host in ev.hosts:
            if not isinstance(host, str) or _HOST.fullmatch(host) is None or host in seen:
                continue
            seen.append(host)
            self._saw(host, wall)
        self.empty_polls = 0 if ev.hosts else self.empty_polls + 1
        if len(ev.hosts) >= REFUSALS_HOSTS_MAX:
            self.next_poll = mono                           # a full poll: the rest is waiting, go again at once
        elif self.empty_polls >= EMPTY_POLLS_SLOW and self.card is None:
            self.next_poll = mono + POLL_SLOW_S
        else:
            self.next_poll = mono + POLL_S

    def _saw(self, host: str, wall: float) -> None:
        h = self.hosts.get(host)
        if h is None:
            self.seq += 1
            h = self.hosts[host] = _Host(polls=0, seq=self.seq)
            if eg.never_grant(host):
                h.state, h.outcome = DEAD, "never"
                return
        if h.state == GRANTED and h.expires is not None and datetime.fromtimestamp(wall, tz=timezone.utc) >= h.expires:
            h.state, h.polls, h.outcome = WAITING, 0, None   # its grant ran out: refused again, it may be carded again
        if h.state == WAITING:
            h.polls += 1

    def _listed(self, ev: Listed, wall: float, mono: float) -> None:
        if self.inflight == GRANTS_LIST:
            self.inflight = None
        self.listed_mono = mono
        now = datetime.fromtimestamp(wall, tz=timezone.utc)
        if self.store is None:
            self.store = ev.store
        same = ev.store == self.store
        live = {host: expires for _g, host, expires, _c in ev.entries if expires > now}
        if same:
            # removed EARLY: the list answered ok, with the card's own store, a non-empty prior set held the host, and
            # it is gone before its expiry (revoked or repaired: root's list cannot tell which)
            for host, expires in self.prior.items():
                h = self.hosts.get(host)
                if self.prior and host not in live and expires > now and h is not None and h.state == GRANTED:
                    h.state, h.outcome = REMOVED, "removed-early"
            self.prior = dict(live)
        self.live = live
        for host, expires in live.items():
            h = self.hosts.get(host)
            if h is not None and h.state in (WAITING, GRANTED):      # allowed already (another way, or by this card)
                h.state, h.expires = GRANTED, expires

    def _scanned(self, ev: Scanned, wall: float, mono: float, cmds: list[Command]) -> None:
        c = self.card
        if c is None or c.stage != "scan":
            return
        if ev.open_anywhere:
            self.card, self.next_scan = None, mono + SCAN_RETRY_S
            return
        if self.rp is None or self.store is None:
            self.card = None
            return
        issued = datetime.fromtimestamp(wall, tz=timezone.utc)
        grant = eg.new_grant_id()
        try:
            message = eg.build_message(self.store, grant, self.session, self.slot, c.host, issued,
                                       issued + timedelta(seconds=SESSION_DEADLINE_S), self.rp)
        except eg.GrantRefused:
            h = self.hosts[c.host]
            h.state, h.outcome = DEAD, "never"
            self.card = None
            return
        c.grant, c.message, c.issued, c.stage = grant, message, issued, "file"
        c.filed_mono = mono
        self.hosts[c.host].state = CARDED
        cmds.append(File(message, title(c.host, self.slot), CARD_TTL_S))

    def _filed(self, ev: Filed, mono: float, cmds: list[Command]) -> None:
        c = self.card
        if c is None or c.stage != "file":
            return
        c.rid, c.stage, c.phone_live, c.filed_mono = ev.rid, "open", ev.phone_live, mono
        c.next_answer = mono + ANSWER_POLL_S
        self.cards_filed += 1
        self.pushed = bool(ev.phone_live) if self.pushed is None else (self.pushed or bool(ev.phone_live))
        expires = c.issued + timedelta(seconds=CARD_TTL_S)
        cmds.append(Push(ev.rid, title(c.host, self.slot), push_body(c.host, self.slot, expires), bool(ev.phone_live)))

    def _answer(self, ev: Answer, wall: float, mono: float, cmds: list[Command]) -> None:
        c = self.card
        if c is None or c.stage != "open" or ev.rid != c.rid:
            return
        h = self.hosts[c.host]
        c.read_since = None
        if ev.state == "open":
            if wall >= c.issued_ts + CARD_TTL_S:
                self._lapsed(c)                             # read AFTER the deadline: a tap before it would be here
        elif ev.state == "denied":
            h.state, h.outcome = DENIED, "denied"
            self.card = None
        elif ev.state == "signed":
            if h.state != CARDED:                           # removed or suppressed while the card was open
                self.card = None
            elif ev.payload is None:
                self._dead(c, "unreadable", "the signed answer could not be read")
            elif wall > c.issued_ts + START_CUTOFF_S:
                self._dead(c, *self._late(c, wall, mono))
            else:
                c.payload, c.stage = ev.payload, "delivering"
                self._start(c, wall, cmds)

    def _late(self, c: _Card, wall: float, mono: float) -> Tuple[str, str]:
        """``too-late`` unless the wall clock ran ahead of the monotonic one since filing (a sleep or a correction)."""
        if (wall - c.issued_ts) - (mono - c.filed_mono) > CLOCK_DRIFT_S:
            return "clock", "the clock moved while the card was open (the laptop slept or was corrected)"
        return "late", "the tap arrived too late"

    def _start(self, c: _Card, wall: float, cmds: list[Command]) -> None:
        c.attempts += 1
        c.retry_at, c.started = None, True
        self.inflight = GRANT
        cmds.append(Deliver(dict(c.payload or {}), c.issued_ts + START_CUTOFF_S))

    def _dead(self, c: _Card, outcome: str, detail: str = "") -> None:
        h = self.hosts[c.host]
        h.state, h.outcome, h.detail = DEAD, outcome, detail
        self.card = None
        if self.inflight == GRANT:
            self.inflight = None

    def _delivered(self, ev: Delivered, wall: float, mono: float, cmds: list[Command]) -> None:
        c = self.card
        if self.inflight == GRANT:
            self.inflight = None
        if c is None or c.stage != "delivering":
            return
        h = self.hosts[c.host]
        if ev.name is None:
            r = ev.result or {}
            if r.get("grant") == c.grant and r.get("host") == c.host and r.get("choice") == (c.payload or {}).get("choice"):
                self._granted(c, h, wall, "granted")
            else:
                self._dead(c, "mismatch", "root answered ok for a different grant, host or choice, so nothing is "
                                          "reported as allowed")
            return
        cls = refusal_class(ev.name)
        words = refusal_words(ev.name)
        if cls == UNSURE:
            self._granted(c, h, wall, "granted-unsure")
        elif cls == RETRY:
            self._retry_or_dead(c, wall, ev.name, words)
        elif cls == OFF:
            self._turn_off(words)
            self._dead(c, "off", words)
        elif cls == SKEW:
            self._turn_off(words)
            self._dead(c, "skew", words)
        elif cls == FRESH:
            self._dead(c, "clock", "the clock moved while the card was open (the laptop slept or was corrected)")
        elif cls == UNENROLLED:
            self._dead(c, "unenrolled", words)
        elif ev.name == "egress-grant-used" and c.unknown:
            self._dead(c, "used-after-timeout", "an earlier attempt timed out and root had already used the card: "
                                                "the host may be allowed, check the host grants list")
        elif cls == ALREADY:
            self._dead(c, "already", words)
        else:
            self._dead(c, "refused", words)

    def _granted(self, c: _Card, h: _Host, wall: float, outcome: str) -> None:
        try:
            grant = eg.parse_signed(eg.signed_bytes(c.message, (c.payload or {})["choice"]))
            expires: Optional[datetime] = grant.expires
        except (eg.GrantRefused, KeyError):
            expires = None
        h.state, h.outcome, h.expires = GRANTED, outcome, expires
        self.card = None

    def _retry_or_dead(self, c: _Card, wall: float, name: str, words: str) -> None:
        at = wall + RETRY_AFTER_S[min(c.attempts - 1, len(RETRY_AFTER_S) - 1)]
        if c.attempts >= DELIVER_MAX or at > c.issued_ts + START_CUTOFF_S:
            self._dead(c, "retries-ended", words + ("; the host may be allowed, check the host grants list"
                                                    if c.unknown else "; no grant was made"))
        else:
            c.retry_at = at

    def _failed(self, ev: RunFailed, wall: float, mono: float, cmds: list[Command]) -> None:
        verb, name = ev.verb, ev.name
        cls = refusal_class(name) if name is not None else None
        if verb == "file":
            c = self.card
            if c is not None and c.stage == "file":
                self._dead(c, "not-filed", "the card could not be filed")
            return
        if verb == "scan":
            if self.card is not None and self.card.stage == "scan":
                self.card, self.next_scan = None, mono + SCAN_RETRY_S
            return
        if self.inflight != verb:
            return                                           # a preempted read: its end is not news
        self.inflight = None
        if cls in (OFF, SKEW) and verb != GRANT and name is not None:
            self._turn_off(refusal_words(name))
            return
        if verb == GRANT:
            c = self.card
            if c is None or c.stage != "delivering":
                return
            if name is None:                                  # a crash or a timeout: root may have published
                c.unknown = True
                self._retry_or_dead(c, wall, "egress-grant-write-failed",
                                    "the delivery ended without an answer")
            else:
                self._delivered(Delivered(name), wall, mono, cmds)
            return
        if verb == REFUSALS:
            if name == "egress-refusals-cursor":
                self.cursor, self.next_poll = 0, mono
            else:
                self.next_poll = mono + POLL_SLOW_S
                key = name or "ended"
                self.poll_errors[key] = self.poll_errors.get(key, 0) + 1
            return
        if verb == GRANTS_LIST:
            self.listed_mono = mono
            key = name or "ended"
            self.poll_errors[GRANTS_LIST + ":" + key] = self.poll_errors.get(GRANTS_LIST + ":" + key, 0) + 1

    # -- the clock -------------------------------------------------------------------------------------------------
    def _lapse(self, wall: float, mono: float) -> None:
        c = self.card
        if c is None:
            return
        if (c.stage == "open" and wall >= c.issued_ts + CARD_TTL_S and c.read_since is not None
                and mono - c.read_since >= READ_STUCK_S):
            self._lapsed(c)                                 # the reader is stuck: do not hold the queue's one slot

    def _lapsed(self, c: _Card) -> None:
        """Nobody tapped in time: the host goes to the back of the queue once, then is not carded again."""
        h = self.hosts[c.host]
        h.lapses += 1
        if h.lapses >= LAPSES_MAX:
            h.state, h.outcome = LAPSED, "lapsed"
        else:
            self.seq += 1
            h.state, h.polls, h.seq, h.outcome = WAITING, 0, self.seq, "lapsed-once"
        self.card = None

    def _due(self, wall: float, mono: float, cmds: list[Command]) -> None:
        c = self.card
        if c is not None and c.stage == "open" and mono >= c.next_answer:
            c.next_answer = mono + ANSWER_POLL_S
            c.read_since = mono if c.read_since is None else c.read_since
            cmds.append(ReadAnswer(str(c.rid)))
        if self.inflight is None:
            if c is not None and c.stage == "delivering" and c.retry_at is not None and wall >= c.retry_at:
                if wall > c.issued_ts + START_CUTOFF_S:
                    self._dead(c, *self._late(c, wall, mono))
                else:
                    self._start(c, wall, cmds)
            elif self.listed_mono is None or mono - self.listed_mono >= LIST_S:
                self.inflight = GRANTS_LIST
                cmds.append(ListGrants())
            elif mono >= self.next_poll:
                self.inflight = REFUSALS
                cmds.append(Poll(self.cursor))
        if (self.card is None and self.rp is not None and self.store is not None and mono >= self.next_scan
                and self._next_host() is not None):
            host = self._next_host()
            self.card = _Card(host=str(host), grant="", message="", issued=datetime.fromtimestamp(wall, tz=timezone.utc),
                              stage="scan")
            cmds.append(Scan())

    def _next_host(self) -> Optional[str]:
        """The waiting host to card next: the most polls it was refused in, then first seen. A host with a live grant
        is never one."""
        ranked = [(-h.polls, h.seq, host) for host, h in self.hosts.items()
                  if h.state == WAITING and host not in self.live]
        return min(ranked)[2] if ranked else None

    # -- what is left to say ---------------------------------------------------------------------------------------
    def end_notes(self) -> list[str]:
        """The lines the operator reads at the end of the session (each a plain sentence the supervisor shows through
        ``unshowable.mark``): what was allowed, what was not and why, which hosts waited without a card, and what was
        left open. At most :data:`NOTES_MAX` hosts are named; the rest are counted."""
        notes: list[str] = []
        if self.off is not None:
            notes.append("host cards stopped for this session: " + self.off)
        rows = [(h.seq, host, h) for host, h in self.hosts.items() if h.outcome in _OUTCOME_WORDS]
        rows.sort()
        for _seq, host, h in rows[:NOTES_MAX]:
            notes.append(_OUTCOME_WORDS[str(h.outcome)](host, h))
        if len(rows) > NOTES_MAX:
            notes.append(f"{len(rows) - NOTES_MAX} more hosts have a note not shown here")
        waited = sorted((h.seq, host) for host, h in self.hosts.items() if h.state == WAITING and host not in self.live)
        if waited:
            why = ("the cockpit was not running: no card was filed for " if self.rp is None
                   else "was refused and waited without a card: ")
            names = ", ".join(host for _s, host in waited[:NOTES_MAX])
            more = f" and {len(waited) - NOTES_MAX} more" if len(waited) > NOTES_MAX else ""
            notes.append(("host cards " if self.rp is not None else "") + why + names + more)
        suppressed = sorted(host for host, h in self.hosts.items() if h.state in (DENIED, REMOVED, LAPSED))
        if suppressed:
            names = ", ".join(suppressed[:NOTES_MAX])
            more = f" and {len(suppressed) - NOTES_MAX} more" if len(suppressed) > NOTES_MAX else ""
            notes.append("no more cards for these hosts this session: " + names + more)
        c = self.card
        if c is not None and c.stage in ("open", "file"):
            notes.append(f"host card for {c.host} was left open; it expires on its own and tapping it now does nothing")
        if self.cards_filed:
            notes.append("host cards were pushed to your phone" if self.pushed else
                         "no phone was subscribed: the host cards waited in the cockpit's notification center")
        for name, n in sorted(self.poll_errors.items()):
            notes.append(f"reading root's host-grant state failed {n} time(s): {name}")
        return notes


def _until(h: _Host) -> str:
    return h.expires.astimezone(timezone.utc).strftime("%H:%M UTC") if h.expires is not None else "the end of the session"


_OUTCOME_WORDS = {
    "granted": lambda host, h: f"host {host} was allowed until {_until(h)} (card tapped, root answered ok)",
    "granted-unsure": lambda host, h: (f"host {host} was allowed until {_until(h)} (card tapped); it is in force and "
                                       "may not survive a power loss"),
    "denied": lambda host, h: f"host {host} was denied on the card",
    "lapsed": lambda host, h: f"the host card for {host} lapsed twice unsigned; no grant was made",
    "removed-early": lambda host, h: f"host {host} was removed before its expiry (revoked or repaired)",
    "never": lambda host, h: f"host {host} is never allowed, so no card was filed",
    "mismatch": lambda host, h: f"host {host}: {h.detail}",
    "refused": lambda host, h: f"host {host}: {h.detail}; no grant was made",
    "off": lambda host, h: f"host {host}: {h.detail}; no grant was made",
    "skew": lambda host, h: f"host {host}: {h.detail}; no grant was made",
    "unenrolled": lambda host, h: (f"host {host}: {h.detail}; enrol your key with the `--egress-host` flag of "
                                   "`enrol-authority-signer` (levain doctor prints the line); no grant was made"),
    "already": lambda host, h: f"host {host}: {h.detail}",
    "unreadable": lambda host, h: f"host {host}: {h.detail}; no grant was made",
    "clock": lambda host, h: f"host {host}: {h.detail}; no grant was made",
    "late": lambda host, h: f"host {host}: {h.detail}; no grant was made",
    "retries-ended": lambda host, h: f"host {host}: {h.detail}",
    "used-after-timeout": lambda host, h: f"host {host}: {h.detail}",
    "not-filed": lambda host, h: f"host {host}: {h.detail}; no grant was made",
}

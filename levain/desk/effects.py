"""levain.desk.effects: the night desk's typed effect verbs (ND-3a, board levain#11; night desk design 2.6, 3.2, 8.1).

TRUST-BOUNDARY CODE. A standing order (:mod:`levain.desk.standing_orders`) says which effects the desk may use; this
module is the ONLY place an effect runs, and it runs two verbs and no others:

* ``restart_service(order_id, host, service, expect_sha256=...)``: ``host`` is one of :data:`HOSTS` and ``service``
  one of :data:`SERVICES`. The command is a HARD-CODED argv (:func:`restart_argv`) built from those two enum members and run
  with ``shell=False`` (:func:`subprocess_runner`); the verb returns the exit code and never the server's output.
* ``send_ack(order_id, event_id, expect_sha256=...)``: the recipient is NEVER an argument. It is derived here, from
  the raw message the event's ``body_ref`` digest names (:func:`inbound_from`), and must be an address the order's ``known_from`` lists.
  The sealed drafter's text (ND-4) is not an argument either: it is fetched inside the verb after every gate has
  passed, and a lexical check (:func:`ack_text_refusal`) runs inside the verb that holds the transport.

NO VERB ACCEPTS FREE TEXT AS AN ARGUMENT. Every argument is an exact member of an enum (a ``str`` that IS in a closed
tuple) or an id of a fixed shape (``so-`` and 12 hex; ``evt-`` and 32 hex; a digest of 64 hex); anything else is
refused by a fixed sentence that never echoes the argument. Extra or missing arguments are refused.

EVERY CALL names the order's version too (``expect_sha256``, the whole-record digest the desk brief prints beside the
order, required): a record revoked and signed again differently under the same id is refused. EVERY CALL replays the
authority log itself, at execution time (:meth:`Effects.gate`; the desk brief's quote is the desk's reading, never the
verb's permission): the order must answer now (in the verified replay, signers active, not expired, not revoked or
stopped, and its widening's base too), be that version, and carry an effect line whose enums cover this call. A log
that cannot be read, or any fault, refuses. Refusals come before any event read, drafter call, send or process; and
the gate runs AGAIN immediately before the process or the send, which go ahead only if it finds the same record and
the same incarnation as the first.

NOT HERE (the next rows): the per-incident and per-night budgets, the arm record, ``NIGHT_ARMING_CLEARED`` and the
``effect_attempts`` journal are ND-3b; the night key and its forced-command list are ND-8 (:data:`FORCED_WORDS` names
the words it must accept); the live mail credential is open (ND_BUILD_PLAN O8) and the sealed drafter is ND-4. Until
those land the executors are injected: an :class:`Effects` with no runner, transport, event lookup, raw source or
drafter refuses with :data:`R_NOT_WIRED`. This module imports no mail library and holds no credential.
"""
from __future__ import annotations

import hashlib
import hmac
import re
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from email.parser import BytesHeaderParser
from email.utils import getaddresses
from pathlib import Path
from typing import Any, Protocol

from levain.authority import _SO_VERBS, STANDING_ORDER_VOICES, _require_aware
from levain.desk import standing_orders
from levain.desk.events import Event

__all__ = ["ACK_FROM_LOCAL", "ACK_SUBJECT", "ACK_WORDS_MAX", "AckMessage", "AckTransport", "DIAGNOSIS_WORDS",
           "EffectRefused", "EffectResult", "Effects", "FORCED_WORDS", "HOSTS", "NIGHT_ALIAS", "NIGHT_SSH", "REFUSALS",
           "SERVICES", "ack_text_refusal", "diagnosis_argv", "inbound_from", "refusal_text", "restart_argv",
           "subprocess_runner"]

# --- the enums: the log's own verb table and this module's are the same closed sets (checked at import) -------------
HOSTS = ("taoserv",)
SERVICES = ("apache2", "php8.1-fpm")
if (_SO_VERBS["restart_service"]["host"][0] != HOSTS or _SO_VERBS["restart_service"]["service"][0] != SERVICES
        or _SO_VERBS["send_ack"]["voice"][0] != STANDING_ORDER_VOICES):      # pragma: no cover - a drift is a build error
    raise AssertionError("levain.desk.effects enums drifted from levain.authority._SO_VERBS")

#: the ssh program and the ssh-config alias of the night key (ND-8 installs the alias: forced command, one key, no PTY,
#: no forwarding). Nothing the seat or an event says reaches either.
NIGHT_SSH = "/usr/bin/ssh"
NIGHT_ALIAS = {"taoserv": "taoserv-night"}
#: the words the night key's forced command must accept (ND-8's list): the one effect word and the read-only diagnosis
#: words. A word and at most one enum argument; never a path, a flag or free text.
DIAGNOSIS_WORDS = {"status": SERVICES, "error_log_tail": SERVICES, "disk": (), "memory": ()}
FORCED_WORDS = {"restart_service": SERVICES, **DIAGNOSIS_WORDS}

_ORDER_ID = re.compile(r"so-[0-9a-f]{12}")
_EVENT_ID = re.compile(r"evt-[0-9a-f]{32}")
_SHA = re.compile(r"[0-9a-f]{64}")
_BODY_REF = re.compile(r"sha256:[0-9a-f]{64}")
#: an address, stricter than the log's (ND-2 L3 recorded the log's domain class admits a leading dot or dash): labels of
#: letters, digits and inner hyphens, no empty label, no dot or hyphen at a label edge in the local part.
_ADDR = re.compile(r"[a-z0-9](?:[a-z0-9._%+-]{0,62}[a-z0-9])?@(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,24}")
ACK_FROM_LOCAL = "thirdmind"
_SENDER = re.compile(ACK_FROM_LOCAL + r"@(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,24}")
ACK_SUBJECT = "Your message arrived"
ACK_WORDS_MAX = 60                      # "under 60 words" (O9): at most 59
_ACK_CHARS_MAX = 600
_DISCLOSURE = {
    "stand_in": "This acknowledgement was written automatically on his behalf. He has been alerted to your message.",
    "flow": "This acknowledgement was written automatically by flow, an AI assistant. He has been alerted to your message.",
}
_REMOTE_TIMEOUT = 60

# --- fixed refusal sentences: nothing an argument, an event or a server said is ever echoed ----------------------------
R_ARGS = "an argument is not one of the verb's allowed values, or one is missing or extra: nothing was done"
R_ORDER = "no standing order in force covers this: nothing was done"
R_LINE = "the order has no effect line whose allowed values cover this call: nothing was done"
R_VERSION = "the order in force is not the version asked for: nothing was done"
R_NOT_WIRED = "this effect is not wired in this process: nothing was done"
R_EVENT = "the event is not an authenticated mail from a known sender with its message: nothing was done"
R_FROM = "the message's sender cannot be derived, is not on the order's list, or the message is an auto-reply, a list " \
         "mail or has a Reply-To that differs: nothing was done"
R_DRAFT = "there is no draft to send: nothing was done"
R_FAILED = "the effect did not complete"
# the lexical check's fixed reasons (:func:`ack_text_refusal`)
R_TEXT_SHAPE = "the draft is not one short text"
R_TEXT_CHARS = "the draft holds a character outside printable ASCII"
R_TEXT_WORDS = f"the draft is not under {ACK_WORDS_MAX} words"
R_TEXT_DIGIT = "the draft holds a digit (a date, time, price or number)"
R_TEXT_SYMBOL = "the draft holds a symbol that makes a link, address, markup or amount"
R_TEXT_DOTTED = "the draft holds a dotted name (a link or address)"
R_TEXT_WORD = "the draft holds a date, time, price, commitment, attachment or link word"
R_TEXT_PHRASE = "the draft holds a commitment phrase"
#: every sentence this module refuses with, and the only text a refusal may carry out (:func:`refusal_text`)
REFUSALS = frozenset({R_ARGS, R_ORDER, R_LINE, R_VERSION, R_NOT_WIRED, R_EVENT, R_FROM, R_DRAFT, R_FAILED,
                      R_TEXT_SHAPE, R_TEXT_CHARS, R_TEXT_WORDS, R_TEXT_DIGIT, R_TEXT_SYMBOL, R_TEXT_DOTTED,
                      R_TEXT_WORD, R_TEXT_PHRASE})


class EffectRefused(Exception):
    """A verb refused: the public base class callers catch. This module itself raises only :class:`_OwnRefusal` (a
    subclass), whose ``str`` is one member of :data:`REFUSALS` and never an argument. An executor can raise this class,
    or a subclass of it, with any text, even a member of :data:`REFUSALS`: that is never trusted (:func:`_trusted`), so
    the verb answers its fixed fallback instead (the clock, in the gate: :data:`R_ORDER`; the event lookup, raw source
    or drafter: :data:`R_FAILED`; the runner or ``transport.send``: a failed :class:`EffectResult`). Only
    :func:`refusal_text` decides what a refusal may say outside this module."""


class _OwnRefusal(EffectRefused):
    """A refusal raised by a raise site in this module (gate, argument shape, argv, :func:`inbound_from`,
    :func:`subprocess_runner`, the lexical check's reason, the verbs). Module-private: it marks where a refusal came
    from, so an executor's ``EffectRefused(<a member of REFUSALS>)`` is not mistaken for the module's own. It is a
    provenance marker against a wrong or forged reason, not a capability: in-process code that imports this name on
    purpose can raise it, and even then only a member of :data:`REFUSALS` is carried out."""


def _trusted(exc: BaseException) -> bool:
    """True only for a refusal raised by this module's own code: exactly :class:`_OwnRefusal` (not the public base an
    executor can raise, and no further subclass, which could carry its own ``__str__``) holding exactly one ``str``
    (no subclass) that is a member of :data:`REFUSALS`. An executor (runner, transport, event lookup, raw source,
    drafter, clock) that raises :class:`EffectRefused` with any text, a member of :data:`REFUSALS` included, is not
    trusted: neither its text nor its choice of reason is carried out."""
    try:
        args = exc.args
        return (type(exc) is _OwnRefusal and type(args) is tuple and len(args) == 1 and type(args[0]) is str
                and args[0] in REFUSALS)
    except Exception:                                       # noqa: BLE001 - a fault is "not ours"
        return False


def refusal_text(exc: BaseException) -> str:
    """What a refusal may say outside this module: its fixed sentence if :func:`_trusted`, else :data:`R_FAILED`."""
    return exc.args[0] if _trusted(exc) else R_FAILED


@dataclass(frozen=True)
class EffectResult:
    verb: str
    ok: bool
    text: str


@dataclass(frozen=True)
class AckMessage:
    """What the transport sends: ONE recipient, a fixed subject, the checked body and the fixed headers. There is no
    Cc, Bcc, Reply-To or attachment field."""
    sender: str
    recipient: str
    subject: str
    body: str
    headers: Mapping[str, str]


class AckTransport(Protocol):
    sender: str

    def send(self, message: AckMessage) -> None: ...


Runner = Callable[[Sequence[str]], int]


# --- restart_service --------------------------------------------------------------------------------------------------
def restart_argv(host: str, service: str) -> list[str]:
    """The hard-coded argv of a restart: the ssh program, fixed options, the host's fixed alias, the one remote word
    and the service. Only members of the enums reach it (anything else raises here, the last line of defence)."""
    if type(host) is not str or host not in HOSTS or type(service) is not str or service not in SERVICES:
        raise _OwnRefusal(R_ARGS)
    return _night_argv(host, "restart_service", service)


def diagnosis_argv(host: str, word: str, service: str | None = None) -> list[str]:
    """The argv of a read-only diagnosis word of :data:`DIAGNOSIS_WORDS` (the forced-command list ND-8 installs).
    Not a verb the seat can call here: it is listed so the forced command and the verbs' argv are one table."""
    allowed = DIAGNOSIS_WORDS.get(word) if type(word) is str else None
    if type(host) is not str or host not in HOSTS or allowed is None:
        raise _OwnRefusal(R_ARGS)
    if allowed:
        if type(service) is not str or service not in allowed:
            raise _OwnRefusal(R_ARGS)
    elif service is not None:
        raise _OwnRefusal(R_ARGS)
    return _night_argv(host, word, service)


def _night_argv(host: str, word: str, arg: str | None) -> list[str]:
    argv = [NIGHT_SSH, "-T", "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes", "-o", "ClearAllForwardings=yes",
            "-o", "StrictHostKeyChecking=yes", NIGHT_ALIAS[host], word]
    if arg is not None:
        argv.append(arg)
    return argv


def subprocess_runner(argv: Sequence[str]) -> int:
    """Run ``argv`` with ``shell=False``, no stdin, no output kept (a server's output is untrusted text this module
    never returns), a minimal environment (no agent socket, no inherited variable) and a timeout. Returns the exit
    code; -1 for a timeout or a program that could not start."""
    import os

    if not isinstance(argv, (list, tuple)) or not argv or any(type(a) is not str for a in argv):
        raise _OwnRefusal(R_ARGS)
    try:
        done = subprocess.run(list(argv), shell=False, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL, timeout=_REMOTE_TIMEOUT, check=False,
                              env={"PATH": "/usr/bin:/bin", "HOME": os.environ.get("HOME", "/")})
    except (OSError, subprocess.SubprocessError):
        return -1
    return done.returncode


# --- send_ack: the sender, derived by code, and the lexical check ----------------------------------------------------
_AUTO_MARKERS = ("x-autoreply", "x-autorespond", "x-auto-response-suppress")   # Auto-Submitted is judged by value
#: a Return-Path, the WHOLE field: ``<`` addr-spec ``>`` and nothing else. The local part is an RFC 5322 dot-atom
#: (atext runs joined by single dots; looser than :data:`_ADDR` on purpose: a bounce or SRS address carries ``=`` and
#: ``+``); the domain is two or more RFC 1035 labels (letters, digits, inner hyphens; no empty label, none starting or
#: ending in a hyphen). DELIBERATELY REFUSED, fail closed (no ack, never a wrong one): a quoted local part
#: (``"a b"@x.example``), a single-label domain (``root@localhost``), an address literal, a source route
#: (``<@relay:u@h.example>``), a comment (``<a@b.example> (via relay)``) and a folded field. The delivering MTA stamps
#: the plain ``<addr>`` form.
_ATEXT = r"[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+"
_LABEL = r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
_RETURN_PATH = re.compile(r"<(" + _ATEXT + r"(?:\." + _ATEXT + r")*)@(" + _LABEL + r"(?:\." + _LABEL + r")+)>",
                          re.ASCII)
#: RFC 5321 4.5.3.1.3: a reverse-path is at most this many octets, the ``<`` and ``>`` included
_REVERSE_PATH_MAX = 256
_LOCAL_PART_MAX = 64                    # RFC 5321 4.5.3.1.1
_DOMAIN_MAX = 253                       # RFC 1035 2.3.4's 255 octets on the wire, written as text


def _return_path_ok(value: Any) -> bool:
    """The one Return-Path field holds exactly one nonempty mailbox, the WHOLE field and nothing else (no space before
    or after it). No lenient parser runs: a field longer than :data:`_REVERSE_PATH_MAX` refuses before any pattern
    does; then any control character (NUL, CR, LF, TAB, DEL: so a folded or smuggled field), any byte outside printable
    ASCII, or anything outside :data:`_RETURN_PATH` refuses."""
    try:
        text = str(value)
    except Exception:                                       # noqa: BLE001 - an undecodable field is no path
        return False
    if len(text) > _REVERSE_PATH_MAX or any(not " " <= c <= "~" for c in text):
        return False
    m = _RETURN_PATH.fullmatch(text)
    return m is not None and len(m.group(1)) <= _LOCAL_PART_MAX and len(m.group(2)) <= _DOMAIN_MAX


def inbound_from(raw: bytes) -> str:
    """The ONE address a reply may go to, derived from the raw message's headers: exactly one ``From`` header holding
    exactly one address of the strict shape (lowercased); a ``Reply-To``, if present, names that same one address; and
    no auto-reply or list marker (``Auto-Submitted`` other than ``no``, ``Precedence`` bulk/list/junk/auto_reply, any
    ``List-*``, ``X-Autoreply``/``X-Autorespond``/``X-Auto-Response-Suppress``, a ``multipart/report``); and EXACTLY
    ONE ``Return-Path`` header holding exactly one nonempty mailbox (:func:`_return_path_ok`): none, two or more, or a
    null path in any form refuses; and a header block that parses whole, with no defect, up to the blank line or the
    end of input. Raises :class:`EffectRefused` (this module's own, :data:`R_FROM`) otherwise.

    WHAT THE RAW STORE MUST GUARANTEE (ND-4): ``raw`` is the message exactly as final delivery handed it over, with
    the one ``Return-Path`` the delivering MTA stamped from the SMTP envelope sender (RFC 5321 4.4), so a bounce or a
    DSN shows as ``<>``. A store that drops that header, never receives it, or lets a second one through makes every
    ack refuse: fail closed, never an ack to a message whose envelope sender is unknown."""
    if not isinstance(raw, (bytes, bytearray)):
        raise _OwnRefusal(R_FROM)
    msg = BytesHeaderParser().parsebytes(bytes(raw))
    # A headers-only parse records a defect only for the header block itself (the body is never parsed): a line that is
    # not a header before the blank line ends the block there and the rest reads as body, so a later Auto-Submitted,
    # List-Id, Reply-To or second Return-Path would be invisible. Any defect refuses: the block must parse whole.
    if msg.defects:
        raise _OwnRefusal(R_FROM)
    names = [str(k).lower() for k in msg.keys()]
    froms = msg.get_all("From") or []
    if len(froms) != 1:
        raise _OwnRefusal(R_FROM)
    found = getaddresses([str(froms[0])])
    if len(found) != 1 or not found[0][1]:
        raise _OwnRefusal(R_FROM)
    sender = found[0][1].strip().lower()
    if _ADDR.fullmatch(sender) is None:
        raise _OwnRefusal(R_FROM)
    reply_to = msg.get_all("Reply-To") or []
    if reply_to:
        if len(reply_to) != 1:
            raise _OwnRefusal(R_FROM)
        rt = getaddresses([str(reply_to[0])])
        if len(rt) != 1 or rt[0][1].strip().lower() != sender:
            raise _OwnRefusal(R_FROM)
    if any(n.startswith("list-") or n in _AUTO_MARKERS for n in names):
        raise _OwnRefusal(R_FROM)
    for value in msg.get_all("Auto-Submitted") or []:
        if str(value).strip().lower() != "no":
            raise _OwnRefusal(R_FROM)
    for value in msg.get_all("Precedence") or []:
        if str(value).strip().lower() in ("bulk", "list", "junk", "auto_reply", "auto-reply"):
            raise _OwnRefusal(R_FROM)
    return_paths = msg.get_all("Return-Path") or []
    if len(return_paths) != 1 or not _return_path_ok(return_paths[0]):     # absent is refused: no envelope sender
        raise _OwnRefusal(R_FROM)
    if str(msg.get_content_type()).lower() == "multipart/report":
        raise _OwnRefusal(R_FROM)
    return sender


# the words a short ack has no business holding: dates and times, prices, commitments, attachments. Conservative on
# purpose: a refused draft is a ping to him; a sent one cannot be recalled. (A lexical check cannot catch a semantic
# commitment: design 3.2 says so; this is the floor, not the proof.)
_WORD = re.compile(r"[a-z]+(?:'[a-z]+)*")
_REFUSED_WORDS = frozenset("""
january february march april june july august september october november december jan feb mar apr jun jul aug sep sept
oct nov dec monday tuesday wednesday thursday friday saturday sunday tue tues wed thu thur thurs fri sat sun
today tomorrow tonight yesterday weekend week weeks month months year years day days hour hours minute minutes
morning afternoon evening noon midnight soon shortly immediately asap deadline later
price prices cost costs fee fees invoice invoices quote quotation payment pay paid refund discount dollar dollars euro
euros usd gbp eur pound pounds charge charges budget
will won't shall promise promised promises guarantee guaranteed commit committed commitment agree agreed approve
approved approval confirm confirmed accept accepted authorize authorized authorise authorised ensure consent sign
signed contract agreement yes sure certainly definitely done deal
attach attached attaching attachment attachments enclosed enclosure
http https www ftp mailto click link links url
""".split())
_REFUSED_PHRASES = ("going to", "of course", "no problem", "get back", "look into", "taken care", "take care of")
_DOTTED = re.compile(r"[A-Za-z0-9-]\.[A-Za-z]{2,}")
_ACK_ALLOWED = re.compile(r"[ -~\n]*")
_SYMBOLS = frozenset("@<>$%/\\#[]{}|`^~*_=+&")


def ack_text_refusal(text: Any) -> str | None:
    """Why a drafted ack may not be sent, as a fixed sentence, or None when it passes the lexical check: text only; 1
    to 59 words; printable ASCII and newlines only; no digit (dates, times, prices, phone numbers), no symbol that
    makes an address, link, markup or amount, no dotted domain, no word of the refused list (dates and times, prices,
    commitments, attachments, links) and no refused phrase; no contraction in ``'ll`` or ``'d``."""
    if type(text) is not str or not text.strip() or len(text) > _ACK_CHARS_MAX:
        return R_TEXT_SHAPE
    if _ACK_ALLOWED.fullmatch(text) is None:
        return R_TEXT_CHARS
    if len(text.split()) >= ACK_WORDS_MAX:
        return R_TEXT_WORDS
    if any(c.isdigit() for c in text):
        return R_TEXT_DIGIT
    if any(c in _SYMBOLS for c in text):
        return R_TEXT_SYMBOL
    if _DOTTED.search(text):
        return R_TEXT_DOTTED
    lowered = text.lower()
    for token in _WORD.findall(lowered):
        if token in _REFUSED_WORDS or token.endswith(("'ll", "'d")):
            return R_TEXT_WORD
    squashed = " ".join(lowered.split())
    if any(p in squashed for p in _REFUSED_PHRASES):
        return R_TEXT_PHRASE
    return None


# --- the verbs --------------------------------------------------------------------------------------------------------
def _exact(args: Mapping[str, Any], names: tuple[str, ...]) -> None:
    if not isinstance(args, Mapping) or set(args) != set(names):
        raise _OwnRefusal(R_ARGS)


def _enum(value: Any, allowed: Sequence[str]) -> str:
    if type(value) is not str or value not in allowed:         # exact str: no subclass, no coercion, no normalising
        raise _OwnRefusal(R_ARGS)
    return value


def _shaped(value: Any, rule: re.Pattern[str]) -> str:
    if type(value) is not str or rule.fullmatch(value) is None:
        raise _OwnRefusal(R_ARGS)
    return value


@dataclass(frozen=True)
class Effects:
    """The verbs, with their executors injected. ``clock`` returns an aware ``datetime`` (read once per call);
    ``data_root`` is the cockpit whose authority log the verbs replay at every call. ``runner`` runs an argv;
    ``transport`` sends one :class:`AckMessage`; ``events`` returns the :class:`Event` for an id (or None);
    ``raw_source`` returns the raw message bytes for a ``body_ref`` (ND-4's store); ``drafter`` returns the sealed
    drafter's text for an event id. A missing executor refuses (:data:`R_NOT_WIRED`) after the gate, never acts."""
    data_root: Path
    clock: Callable[[], datetime]
    runner: Runner | None = None
    transport: AckTransport | None = None
    events: Callable[[str], Event | None] | None = None
    raw_source: Callable[[str], bytes | None] | None = None
    drafter: Callable[[str], str] | None = None

    def gate(self, order_id: str, verb: str, *, expect_sha256: str) -> tuple[Mapping[str, Any], list[Any]]:
        """Replay the authority log NOW and return (the order, its effect lines for ``verb``). Refuses unless the
        order answers at this instant: in the verified replay, signers active, unexpired, not revoked, not stopped,
        its widening's base too (:func:`levain.desk.standing_orders.orders_in_force`), and its whole-record digest is
        ``expect_sha256`` (the version the desk read in its brief; a different record under the same id is refused).
        Every fault refuses."""
        try:
            now = self.clock()
            _require_aware(now)
            state = standing_orders.read_state(self.data_root)
            if state is None:
                raise _OwnRefusal(R_ORDER)
            order = next((o for o in standing_orders.orders_in_force(state, now) if o["id"] == order_id), None)
            if order is None:
                raise _OwnRefusal(R_ORDER)
            if type(expect_sha256) is not str or not hmac.compare_digest(standing_orders.order_sha256(order),
                                                                         expect_sha256):
                raise _OwnRefusal(R_VERSION)
            lines = [ln for ln in order["effect_lines"] if ln["verb"] == verb]
        except Exception as exc:                            # noqa: BLE001 - any fault reading the log is "no order"
            if _trusted(exc):                               # ours; a clock's EffectRefused, any reason, is a fault
                raise
            raise _OwnRefusal(R_ORDER) from None
        if not lines:
            raise _OwnRefusal(R_LINE)
        return order, lines

    def _still(self, order_id: str, verb: str, sha: str, first: Mapping[str, Any]) -> None:
        """The gate once more, immediately before the effect: the order must still answer, as the same record (the
        same digest) and the same incarnation (not revoked and signed again under its id meanwhile) as the first
        replay found. A second replay narrows the window to the dispatch itself; the reservation that closes it is
        ND-3b's attempts journal."""
        again, _lines = self.gate(order_id, verb, expect_sha256=sha)
        try:
            same = (standing_orders.order_sha256(again) == standing_orders.order_sha256(first)
                    and again.get("incarnation") == first.get("incarnation"))
        except Exception:                                   # noqa: BLE001 - a fault is "not the same order"
            same = False
        if not same:
            raise _OwnRefusal(R_VERSION)

    def restart_service(self, order_id: Any, host: Any, service: Any, *, expect_sha256: Any) -> EffectResult:
        """Restart ``service`` on ``host`` under order ``order_id`` at version ``expect_sha256`` (the digest the
        desk's brief prints beside the order)."""
        order_id = _shaped(order_id, _ORDER_ID)
        host, service = _enum(host, HOSTS), _enum(service, SERVICES)
        sha = _shaped(expect_sha256, _SHA)
        order, lines = self.gate(order_id, "restart_service", expect_sha256=sha)
        if not any(host in ln["args"]["host"] and service in ln["args"]["service"] for ln in lines):
            raise _OwnRefusal(R_LINE)
        if self.runner is None:
            raise _OwnRefusal(R_NOT_WIRED)
        argv = restart_argv(host, service)
        self._still(order_id, "restart_service", sha, order)
        try:
            code = self.runner(argv)
        except Exception as exc:                            # noqa: BLE001 - the runner's fault never carries text out
            if _trusted(exc):                               # e.g. subprocess_runner's own R_ARGS; a runner's
                raise                                       # EffectRefused, any reason, is a failed run
            return EffectResult("restart_service", False, f"restart_service {host} {service}: {R_FAILED}")
        ok = type(code) is int and code == 0
        return EffectResult("restart_service", ok, f"restart_service {host} {service}: "
                            + ("exit 0" if ok else R_FAILED))

    def send_ack(self, order_id: Any, event_id: Any, *, expect_sha256: Any) -> EffectResult:
        """Send the ack for ``event_id`` under order ``order_id`` at version ``expect_sha256``. The recipient is
        derived here (never an argument) from the raw message the event's digest names, and must be on the order
        line's ``known_from``. The order is replayed again after the slow part (event, message, drafter) and
        immediately before the send."""
        order_id, event_id = _shaped(order_id, _ORDER_ID), _shaped(event_id, _EVENT_ID)
        sha = _shaped(expect_sha256, _SHA)
        order, lines = self.gate(order_id, "send_ack", expect_sha256=sha)
        if self.events is None or self.raw_source is None or self.drafter is None or self.transport is None:
            raise _OwnRefusal(R_NOT_WIRED)
        try:
            sender = getattr(self.transport, "sender", None)
        except Exception:                                   # noqa: BLE001 - a faulting property says nothing either
            sender = None
        if type(sender) is not str or _SENDER.fullmatch(sender) is None:
            raise _OwnRefusal(R_NOT_WIRED)                # the transport speaks only as thirdmind@
        try:
            recipient, voice = self._recipient(event_id, lines)
            draft = self.drafter(event_id)
        except Exception as exc:                            # noqa: BLE001 - an executor's fault never carries text out
            if _trusted(exc):                               # only this module's own refusals (inbound_from, the
                raise                                       # event checks) pass; an executor's is R_FAILED
            raise _OwnRefusal(R_FAILED) from None
        why = ack_text_refusal(draft)
        if why is not None:
            raise _OwnRefusal(why)
        message = AckMessage(sender=sender, recipient=recipient, subject=ACK_SUBJECT,
                             body=draft.strip() + "\n\n" + _DISCLOSURE[voice],
                             headers={"Auto-Submitted": "auto-replied"})
        self._still(order_id, "send_ack", sha, order)
        try:
            self.transport.send(message)
        except Exception:                                   # noqa: BLE001 - a transport fault says nothing it was told
            # KNOWN EDGE: a fault after the message was delivered (a timeout on the reply) also reads as not sent, so a
            # retry could send twice; the once-per-event guard is ND-3b's attempts journal, not this verb.
            return EffectResult("send_ack", False, f"send_ack: {R_FAILED}")
        return EffectResult("send_ack", True, "send_ack: sent")

    def _recipient(self, event_id: str, lines: list[Any]) -> tuple[str, str]:
        assert self.events is not None and self.raw_source is not None
        event = self.events(event_id)
        if (not isinstance(event, Event) or event.event_id != event_id or event.kind != "mail"
                or event.trust != "authenticated" or event.fields.get("auth") != "pass"
                or event.fields.get("sender_class") != "known" or type(event.body_ref) is not str
                or _BODY_REF.fullmatch(event.body_ref) is None):
            raise _OwnRefusal(R_EVENT)
        raw = self.raw_source(event.body_ref)
        if not isinstance(raw, (bytes, bytearray)) or not hmac.compare_digest(
                "sha256:" + hashlib.sha256(bytes(raw)).hexdigest(), event.body_ref):
            raise _OwnRefusal(R_EVENT)                    # the bytes are not the ones the event's digest names
        recipient = inbound_from(bytes(raw))
        voices: set[Any] = set()
        for ln in lines:
            if recipient in {a.strip().lower() for a in ln["args"]["known_from"]}:
                named = ln["args"]["voice"]
                if not isinstance(named, (list, tuple)) or len(named) != 1:
                    raise _OwnRefusal(R_LINE)
                voices.add(named[0])
        if not voices:
            raise _OwnRefusal(R_FROM)
        if len(voices) != 1 or next(iter(voices)) not in _DISCLOSURE:   # one voice, or the disclosure could be false
            raise _OwnRefusal(R_LINE)
        return recipient, next(iter(voices))

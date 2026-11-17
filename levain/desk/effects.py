"""levain.desk.effects: the night desk's typed effect verbs (ND-3a, board levain#11; night desk design 2.6, 3.2, 8.1).

TRUST-BOUNDARY CODE. A standing order (:mod:`levain.desk.standing_orders`) says which effects the desk may use; this
module is the ONLY place an effect runs, and it runs two verbs and no others:

* ``restart_service(order_id, host, service, expect_sha256=..., event_id=...)``: ``host`` is one of :data:`HOSTS` and
  ``service`` one of :data:`SERVICES`; ``event_id`` is the monitor event it answers. The command is a HARD-CODED argv (:func:`restart_argv`) built from those two enum members and run
  with ``shell=False`` (:func:`subprocess_runner`); the verb returns an ``EffectResult`` whose ``ok`` is true only on exit 0, its text built from the verb, host and service plus a fixed status (``exit 0``, :data:`R_FAILED` or :data:`R_UNKNOWN`), and never the server's output or a failing exit code.
* ``send_ack(order_id, event_id, expect_sha256=...)``: the recipient is NEVER an argument. It is derived here, from
  the raw message the event's ``body_ref`` digest names (:func:`inbound_from`), and must be an address the order's ``known_from`` lists.
  The sealed drafter's :class:`Draft` (ND-4) is not an argument either: it is fetched inside the verb after every gate
  has passed; its voice is checked here against the voice the order names, a lexical check (:func:`ack_text_refusal`)
  runs on its text, and only then the fixed disclosure line for the voice (:data:`DISCLOSURES`) is appended.

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

EXECUTION-TIME BUDGETS AND THE JOURNAL (ND-3b, board levain#12, :mod:`levain.desk.effect_attempts`): an effect is
reserved on the journal BEFORE it runs, in one transaction that also checks the order line's per-incident and per-night
budgets against every order's attempts in that incident or night (held to the strictest limit among them), and consumes
them (never check-then-act); the reservation is written ``unknown``, so a process that dies mid-effect leaves one
``unknown`` row, never retried and never sent twice. A retry of a settled or unknown effect (the same event, verb and
arguments, under any order) answers the recorded status and runs nothing, and needs no runner, drafter or transport
wired. Anything the runner or the transport raises (a refusal of this module's own included), a timeout, a signal
death, a runner that returns no exit code, and a result the journal could not write all answer and stay ``unknown``. An
ack is reserved before it is drafted. After the reservation the order is replayed one more time and the event's age is
checked again, and the effect runs only if the order is the same version and the event still in the window.
``restart_service`` takes the monitor event it answers (its incident is derived from it); every event must be within
:data:`EVENT_MAX_AGE`; an ack whose journal row records another recipient than the one derived now refuses
(:data:`R_SENDER`), on a retry, a race and a retake alike. A refusal this module raises is recorded with its fixed
reason. ``EffectResult.verb`` is the verb's name alone; the host and service are in its text.

THE FALLBACK (ND-3c, board levain#13, :mod:`levain.desk.would_have_acted`): a refusal that says no order line covers the
call (no order in force, no covering line, a spent budget, a sender on no line) is also written as a would-have-acted
row with an open ping when an :class:`Effects` is given one and the event the call names is found and passes the
verb's own event checks (a made-up or stale event id writes nothing); nothing runs and the refusal is unchanged.

NOT HERE (the next rows): the arm record (O1); the night key and its forced-command list are ND-8 (:data:`FORCED_WORDS` names
the words it must accept); the live mail credential is open (ND_BUILD_PLAN O8). Until
those land the executors are injected: an :class:`Effects` with no runner, transport, event lookup, raw source or
drafter refuses with :data:`R_NOT_WIRED`. This module imports no mail library and holds no credential.
"""
from __future__ import annotations

import hashlib
import hmac
import itertools
import re
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from email.parser import BytesHeaderParser
from email.utils import getaddresses
from pathlib import Path
from types import MappingProxyType
from typing import Any, Protocol

from levain.authority import _SO_VERBS, STANDING_ORDER_VOICES, _require_aware
from levain.cockpit.consent import parse_stamp, utc_stamp
from levain.desk import arming_gate, standing_orders
from levain.desk.effect_attempts import BudgetSpent, Journal, idem_key, night_key
from levain.desk.events import Event
from levain.desk.would_have_acted import WouldHaveActed

__all__ = ["ACK_FROM_LOCAL", "ACK_SUBJECT", "ACK_WORDS_MAX", "AckMessage", "AckTransport", "DIAGNOSIS_WORDS", "DISCLOSURES",
           "Draft", "EVENT_FUTURE_SKEW", "EVENT_MAX_AGE", "EffectRefused", "EffectResult", "Effects", "FORCED_WORDS", "HOSTS", "NIGHT_ALIAS", "NIGHT_ARMING_FACTS", "NIGHT_CUT_HOUR_UTC", "NIGHT_SSH", "REFUSALS",
           "SERVICES", "VOICES", "ack_text_refusal", "diagnosis_argv", "inbound_from", "refusal_text", "restart_argv",
           "subprocess_runner"]

# --- the enums: the log's own verb table and this module's are the same closed sets (checked at import) -------------
HOSTS = ("taoserv",)
SERVICES = ("apache2", "php8.3-fpm")
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
_INCIDENT_ID = re.compile(r"inc-[0-9a-f]{32}")
#: an address, stricter than the log's (ND-2 L3 recorded the log's domain class admits a leading dot or dash): labels of
#: letters, digits and inner hyphens, no empty label, no dot or hyphen at a label edge in the local part.
_ADDR = re.compile(r"[a-z0-9](?:[a-z0-9._%+-]{0,62}[a-z0-9])?@(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,24}")
ACK_FROM_LOCAL = "thirdmind"
_SENDER = re.compile(ACK_FROM_LOCAL + r"@(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,24}")
ACK_SUBJECT = "Your message arrived"
ACK_WORDS_MAX = 60                      # "under 60 words" (O9): at most 59
_ACK_CHARS_MAX = 600
#: the most lines a draft may span, with no blank line: newlines that pad a draft would push the disclosure line the
#: send tool appends below it out of the reader's view
_ACK_LINES_MAX = 3
#: the fixed disclosure line the send tool appends for each VOICE (ND-4); the drafter names a voice, never the wording
DISCLOSURES: Mapping[str, str] = MappingProxyType({
    "stand_in": "This acknowledgement was written automatically on his behalf by an AI assistant. He has been alerted "
                "to your message.",
    "flow": "This acknowledgement was written automatically by flow, an AI assistant. He has been alerted to your "
            "message.",
})
VOICES = tuple(DISCLOSURES)
_REMOTE_TIMEOUT = 60
#: the oldest an event may be when an effect runs for it (the night's length, the same bound an incident stays open), and
#: the most it may be in the future (clock skew): an event outside either is refused. The order's own window is the arm
#: record's (O1); until that lands this is the window.
EVENT_MAX_AGE = timedelta(hours=12)
EVENT_FUTURE_SKEW = timedelta(minutes=5)
#: a night's budget counts from one cut to the next (UTC hour); see :func:`levain.desk.effect_attempts.night_key`
NIGHT_CUT_HOUR_UTC = 12
#: the kinds of event a restart may answer: a monitor's own report on an incident, never mail
_RESTART_KINDS = ("site_status", "heartbeat", "report")

# --- fixed refusal sentences: nothing an argument, an event or a server said is ever echoed ----------------------------
R_ARGS = "an argument is not one of the verb's allowed values, or one is missing or extra: nothing was done"
R_ORDER = "no standing order in force covers this: nothing was done"
R_LINE = "the order has no effect line whose allowed values cover this call: nothing was done"
R_VERSION = "the order in force is not the version asked for: nothing was done"
#: the arming gate's facts (ND-11, board levain#20; ND_BUILD_PLAN O12): the np ids of his ND-7 ruling, ND-8 done and ND-9
#: read, by the keys of :data:`levain.desk.arming_gate.FACTS`. Closed as shipped. It is a source constant on purpose: no
#: argument, environment variable, file, store row or verb sets it, so the only way to open it is a reviewed commit that
#: writes the three np ids here. :meth:`Effects.gate` reads it by name at every call.
NIGHT_ARMING_FACTS: Mapping[str, str] = arming_gate.CLOSED_FACTS
_FACT_NAMES = {"nd7_ruled": "the ND-7 ruling", "nd8_done": "ND-8 done", "nd9_read": "the ND-9 read"}


def _armed_sentence(open_keys: tuple[str, ...]) -> str:
    return ("the night is not armed for effect lines; still open: " + ", ".join(_FACT_NAMES[k] for k in open_keys)
            + ": nothing was done")


#: one fixed sentence per set of open items (a refusal never carries computed text); the keys are tuples in gate order
R_ARMING = {keys: _armed_sentence(keys) for n in (1, 2, 3) for keys in itertools.combinations(_FACT_NAMES, n)}
R_ARMING_TWIN = "the night is not armed for effect lines; one np id is given for more than one item: nothing was done"
R_NOT_WIRED = "this effect is not wired in this process: nothing was done"
R_EVENT = "the event is not an authenticated mail from a known sender with its message: nothing was done"
R_FROM = "the message's sender cannot be derived, is not on the order's list, or the message is an auto-reply, a list " \
         "mail or has a Reply-To that differs: nothing was done"
R_DRAFT = "there is no draft to send: nothing was done"
R_VOICE = "the draft's voice is missing, is not a known voice, or is not the voice the order names: nothing was done"
R_DISCLOSURE = "the draft holds the disclosure line itself, which only the send tool writes: nothing was done"
R_FAILED = "the effect did not complete"
R_UNKNOWN = "the effect may have run; its outcome is unknown and it is not retried"
R_BUDGET_INCIDENT = "the order's budget for this incident is spent: nothing was done"
R_BUDGET_NIGHT = "the order's budget for this night is spent: nothing was done"
R_EVENT_AGE = "the event is older than the order's window, or from the future: nothing was done"
R_SENDER = "the sender recorded for this effect is not the order's pinned recipient: nothing was done"
# the lexical check's fixed reasons (:func:`ack_text_refusal`)
R_TEXT_SHAPE = "the draft is not one short text"
R_TEXT_CHARS = "the draft holds a character outside printable ASCII"
R_TEXT_LINES = f"the draft holds a blank line or spans more than {_ACK_LINES_MAX} lines"
R_TEXT_WORDS = f"the draft is not under {ACK_WORDS_MAX} words"
R_TEXT_DIGIT = "the draft holds a digit (a date, time, price or number)"
R_TEXT_SYMBOL = "the draft holds a symbol that makes a link, address, markup or amount"
R_TEXT_DOTTED = "the draft holds a dotted name (a link or address)"
R_TEXT_WORD = "the draft holds a date, time, price, commitment, attachment or link word"
R_TEXT_PHRASE = "the draft holds a commitment phrase"
#: every sentence this module refuses with, and the only text a refusal may carry out (:func:`refusal_text`)
REFUSALS = frozenset({*R_ARMING.values(), R_ARMING_TWIN, R_ARGS, R_ORDER, R_LINE, R_VERSION, R_NOT_WIRED, R_EVENT, R_FROM, R_DRAFT, R_VOICE, R_DISCLOSURE, R_FAILED,
                      R_UNKNOWN, R_BUDGET_INCIDENT, R_BUDGET_NIGHT, R_EVENT_AGE, R_SENDER,
                      R_TEXT_SHAPE, R_TEXT_CHARS, R_TEXT_LINES, R_TEXT_WORDS, R_TEXT_DIGIT, R_TEXT_SYMBOL, R_TEXT_DOTTED,
                      R_TEXT_WORD, R_TEXT_PHRASE})


class EffectRefused(Exception):
    """A verb refused: the public base class callers catch. This module itself raises only :class:`_OwnRefusal` (a
    subclass), whose ``str`` is one member of :data:`REFUSALS` and never an argument. An executor can raise this class,
    or a subclass of it, with any text, even a member of :data:`REFUSALS`: that is never trusted (:func:`_trusted`), so
    the verb answers its fixed fallback instead (the clock, in the gate: :data:`R_ORDER`; the event lookup, raw source
    or drafter: :data:`R_FAILED`; the runner or ``transport.send``: an :class:`EffectResult` that answers
    :data:`R_UNKNOWN`, the attempt recorded ``unknown``, since the effect may have run, and so too for a refusal of this
    module's own raised from inside the runner, such as :func:`subprocess_runner`'s). Only :func:`refusal_text`
    decides what a refusal may say outside this module."""


class _OwnRefusal(EffectRefused):
    """A refusal raised by a raise site in this module (gate, argument shape, argv, :func:`inbound_from`,
    :func:`subprocess_runner`, the lexical check's reason, the verbs). Module-private: it marks where a refusal came
    from, so an executor's ``EffectRefused(<a member of REFUSALS>)`` is not mistaken for the module's own. It is a
    provenance marker against a wrong or forged reason, not a capability: in-process code that imports this name on
    purpose can raise it, and even then only a member of :data:`REFUSALS` is carried out."""


def _coverage_reason(exc: BaseException) -> str | None:
    """The would-have-acted reason a trusted refusal writes (ND-3c), or None when it is not a coverage refusal. Covered
    here: no order answering (:data:`R_ORDER`; KNOWN EDGE: the gate answers it for any fault reading the log too), no
    line covering the call, a spent budget, and a sender on no covering line (:data:`R_FROM` carrying ``coverage``,
    set where :meth:`Effects._recipient` finds the sender read fine and listed nowhere; the same sentence for a
    malformed or auto-sent message does not carry it). Every other refusal is noise or a fault, not a gap in the order's
    vocabulary: argument shapes are checked before the gate, so a bad argument never reaches a record."""
    text = exc.args[0]
    table = {R_ORDER: "no_order", R_LINE: "no_line", R_BUDGET_INCIDENT: "budget_incident", R_BUDGET_NIGHT: "budget_night"}
    if text in table:
        return table[text]
    return "sender_not_listed" if text == R_FROM and getattr(exc, "coverage", False) is True else None


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
class Draft:
    """What the sealed drafter returns (ND-4): one short text and a typed VOICE (a member of :data:`VOICES`). The
    send tool checks the voice itself and appends the fixed disclosure for it; the text is checked by
    :func:`ack_text_refusal` before that line is added."""
    text: str
    voice: str


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
    argv = [NIGHT_SSH, "-F", f"{_runner_env()['HOME'].rstrip('/')}/.levain/night/ssh_config", "-T", "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes", "-o", "ClearAllForwardings=yes",
            "-o", "StrictHostKeyChecking=yes", NIGHT_ALIAS[host], word]
    if arg is not None:
        argv.append(arg)
    return argv


def subprocess_runner(argv: Sequence[str]) -> int:
    """Run ``argv`` with ``shell=False``, no stdin, no output kept (a server's output is untrusted text this module
    never returns), a minimal environment (no agent socket, no inherited variable) and a timeout. Returns the exit
    code; -1 for a timeout or a program that could not start."""
    if not isinstance(argv, (list, tuple)) or not argv or any(type(a) is not str for a in argv):
        raise _OwnRefusal(R_ARGS)
    try:
        done = subprocess.run(list(argv), shell=False, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL, timeout=_REMOTE_TIMEOUT, check=False, env=_runner_env())
    except (OSError, subprocess.SubprocessError):
        return -1
    return done.returncode


def _runner_env() -> dict[str, str]:
    """The ssh child's whole environment: a fixed PATH and HOME, and nothing inherited (no ``SSH_AUTH_SOCK``; not
    :func:`levain.launch.child_env`, whose allowlist carries it). An empty or absent HOME is ``/``. KNOWN EDGE: the
    ssh config the child reads is HOME's, so an ``IdentityAgent`` or other setting there still applies; ND-8 must pin
    it (``-F`` a fixed config, its own known-hosts file, an explicit key, no ``IdentityAgent``)."""
    import os

    return {"PATH": "/usr/bin:/bin", "HOME": os.environ.get("HOME") or "/"}


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
    to 59 words; printable ASCII and newlines only; at most :data:`_ACK_LINES_MAX` lines and no blank line (padding
    must not push the appended disclosure line out of view); no digit (dates, times, prices, phone numbers), no symbol that
    makes an address, link, markup or amount, no dotted domain, no word of the refused list (dates and times, prices,
    commitments, attachments, links) and no refused phrase; no contraction in ``'ll`` or ``'d``."""
    if type(text) is not str or not text.strip() or len(text) > _ACK_CHARS_MAX:
        return R_TEXT_SHAPE
    if _ACK_ALLOWED.fullmatch(text) is None:
        return R_TEXT_CHARS
    lines = text.strip().split("\n")
    if "\n\n" in text or len(lines) > _ACK_LINES_MAX or any(not line.strip() for line in lines):
        return R_TEXT_LINES
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


def _squash(text: str) -> str:
    return " ".join(text.lower().split())


def _holds_disclosure(text: str) -> bool:
    """True when the drafter's text carries, in any case or spacing, the first sentence of any disclosure line: the
    line is the send tool's alone, so a draft that writes it would duplicate it or pre-empt its wording."""
    squashed = _squash(text)
    return any(_squash(line).split(". ")[0] in squashed for line in DISCLOSURES.values())


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
    drafter's :class:`Draft` (text and voice) for an event id; a bare ``str`` has no voice and is refused; ``journal`` is the
    :class:`~levain.desk.effect_attempts.Journal` every effect is reserved on before it runs. A missing executor or journal refuses (:data:`R_NOT_WIRED`) after the gate, never acts;
    the runner, drafter and transport are needed only for an effect that would run now (a recorded one is answered
    without them)."""
    data_root: Path
    clock: Callable[[], datetime]
    runner: Runner | None = None
    transport: AckTransport | None = None
    events: Callable[[str], Event | None] | None = None
    raw_source: Callable[[str], bytes | None] | None = None
    drafter: Callable[[str], Draft] | None = None
    journal: Journal | None = None
    would_have_acted: WouldHaveActed | None = None
    night_cut_hour_utc: int = NIGHT_CUT_HOUR_UTC

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
            if arming_gate.arming_refusal(order, NIGHT_ARMING_FACTS) is not None:
                # the order is in force and is the version asked for, but the night is not armed for its effect lines
                open_keys = arming_gate.missing_facts(NIGHT_ARMING_FACTS)
                raise _OwnRefusal(R_ARMING.get(open_keys, R_ARMING_TWIN))
            lines = [ln for ln in order["effect_lines"] if ln["verb"] == verb]
        except Exception as exc:                            # noqa: BLE001 - any fault reading the log is "no order"
            if _trusted(exc):                               # ours; a clock's EffectRefused, any reason, is a fault
                raise
            raise _OwnRefusal(R_ORDER) from None
        if not lines:
            raise _OwnRefusal(R_LINE)
        return order, lines

    def _still(self, order_id: str, verb: str, sha: str, first: Mapping[str, Any]) -> None:
        """The gate once more, AFTER the reservation and immediately before the effect: the order must still answer,
        as the same record (the same digest) and the same incarnation (not revoked and signed again under its id
        meanwhile) as the first replay found. The effect is bound to the version it was requested under."""
        again, _lines = self.gate(order_id, verb, expect_sha256=sha)
        try:
            same = (standing_orders.order_sha256(again) == standing_orders.order_sha256(first)
                    and again.get("incarnation") == first.get("incarnation"))
        except Exception:                                   # noqa: BLE001 - a fault is "not the same order"
            same = False
        if not same:
            raise _OwnRefusal(R_VERSION)

    # --- the journal: refusals are recorded, a reservation is written before any effect --------------------------
    def _now(self) -> datetime:
        now = self.clock()
        _require_aware(now)
        return now

    def _noted(self, order_id: str, verb: str, event_id: str, call: Callable[[], EffectResult],
               args: Mapping[str, str] | None = None) -> EffectResult:
        """Run ``call``; a refusal this module raised is recorded (order, verb, event, its fixed sentence) and re-raised
        unchanged. A refusal for COVERAGE (:func:`_coverage_reason`) is also written as a would-have-acted row with an
        open ping, but only when ``event_id`` is a real event that passes the verb's own event checks
        (:meth:`_event_verified`): the gate and the line check run before the event is looked up, so a made-up id would
        otherwise earn a row and a push. Recording is best effort and never changes the refusal."""
        try:
            return call()
        except EffectRefused as exc:
            if _trusted(exc) and self.journal is not None:
                try:
                    self.journal.note_refusal(order_id, verb, event_id, exc.args[0], utc_stamp(self._now()))
                except Exception:                           # noqa: BLE001 - a clock or file fault changes nothing
                    pass
            if _trusted(exc) and self.would_have_acted is not None:
                why = _coverage_reason(exc)
                if why is not None and self._event_verified(verb, event_id):
                    try:
                        now = self._now()
                        self.would_have_acted.record(verb=verb, reason=why, event_id=event_id, order_id=order_id,
                                                     args=args or {}, night=night_key(now, self.night_cut_hour_utc),
                                                     now=utc_stamp(now))
                    except Exception:                       # noqa: BLE001 - the record never changes the refusal
                        pass
            raise

    def _event_verified(self, verb: str, event_id: str) -> bool:
        """True only when ``event_id`` is looked up now and passes the checks ``verb`` itself makes of its event
        (:meth:`_incident` for a restart; for an ack :meth:`_mail_event` and then :func:`inbound_from` on its raw
        message, so an auto-reply or a malformed message is never a gap; the age window included). Any refusal, fault
        or missing executor is False: an event nobody can find is noise, never a coverage gap."""
        try:
            if self.events is None:
                return False
            if verb == "restart_service":
                self._incident(event_id)
            elif verb == "send_ack" and self.raw_source is not None:
                _event, raw = self._mail_event(event_id)
                inbound_from(raw)
            else:
                return False
            return True
        except Exception:                                   # noqa: BLE001 - unverified is not recorded
            return False

    def _event_age(self, event: Event) -> None:
        """An event older than :data:`EVENT_MAX_AGE`, or more than :data:`EVENT_FUTURE_SKEW` ahead of the clock, or whose
        arrival time cannot be read, is refused: an old event is not a reason to act now."""
        try:
            age = self._now() - parse_stamp(event.received_at)
        except Exception:                                   # noqa: BLE001 - an unreadable stamp is no age
            raise _OwnRefusal(R_EVENT_AGE) from None
        if age > EVENT_MAX_AGE or -age > EVENT_FUTURE_SKEW:
            raise _OwnRefusal(R_EVENT_AGE)

    @staticmethod
    def _recorded(verb: str, what: str, ok_text: str, status: str) -> EffectResult:
        """What an attempt answers: its recorded status, in this module's own words (a settled or unknown effect is never
        run again). ``verb`` is the verb's name alone (a caller routes on it); ``what`` (the verb and its enum arguments)
        leads the text: ``ok_text`` when ``ok``, else :data:`R_UNKNOWN` or :data:`R_FAILED`."""
        if status == "ok":
            return EffectResult(verb, True, f"{what}: {ok_text}")
        return EffectResult(verb, False, f"{what}: " + (R_UNKNOWN if status == "unknown" else R_FAILED))

    def _reserve(self, *, key: str, order: Mapping[str, Any], verb: str, event_id: str, args_digest: str,
                 recipient: str | None, incident_scope: str, night_scope: str, covering: Sequence[Any]) -> Any:
        """The atomic reservation: the budgets are counted and the ``unknown`` row written in ONE transaction. Returns
        ``(attempt, fresh)``; a spent budget refuses, and any fault in the journal refuses (nothing runs on an
        effect that could not be recorded)."""
        assert self.journal is not None
        try:
            per_incident = min(int(ln["per_incident"]) for ln in covering)
            per_night = min(int(ln["per_night"]) for ln in covering)
            return self.journal.reserve(
                key=key, order_id=order["id"], order_sha256=standing_orders.order_sha256(order),
                incarnation=int(order.get("incarnation", 0)), verb=verb, event_id=event_id, args_digest=args_digest,
                recipient=recipient, incident_scope=incident_scope, night_scope=night_scope, per_incident=per_incident,
                per_night=per_night, now=utc_stamp(self._now()))
        except BudgetSpent as spent:
            raise _OwnRefusal(R_BUDGET_INCIDENT if spent.which == "incident" else R_BUDGET_NIGHT) from None
        except Exception as exc:                            # noqa: BLE001 - an unrecorded effect never runs
            if _trusted(exc):
                raise
            raise _OwnRefusal(R_FAILED) from None

    def _settle(self, attempt: Any, status: str) -> bool:
        """Write the result; True only when the row was ``unknown`` and now holds ``status``. A fault here leaves the
        row ``unknown``, the safe reading (never a retry), and answers False."""
        assert self.journal is not None
        try:
            return self.journal.settle(attempt.attempt_id, status, utc_stamp(self._now())) is True
        except Exception:                                   # noqa: BLE001
            return False

    def _dispatch(self, attempt: Any, order_id: str, verb: str, sha: str, order: Mapping[str, Any],
                  go: Callable[[Any], str], *, event: Event, compose: Callable[[], Any] | None = None) -> str:
        """After the reservation, before anything leaves the process: ``compose()`` (the ack's draft and its checks),
        the order replayed ONE more time (a revoke, a widening's lapse or a new signing between the request and now
        refuses) and ``event``'s age checked again at this instant. Any refusal or fault there settles the attempt
        ``refused`` (nothing ran) and refuses (an executor's fault as :data:`R_FAILED`). Then ``go(prepared)``, which runs
        the effect and returns ``ok``, ``failed`` or ``unknown``: ANYTHING escaping it, a refusal this module raised
        included, is ``unknown`` (the runner or the transport may have started). The answer is the status the journal
        now holds: a result that could not be written is ``unknown``, never ``ok``."""
        try:
            prepared = compose() if compose is not None else None
            self._still(order_id, verb, sha, order)
            self._event_age(event)
        except Exception as exc:                            # noqa: BLE001 - nothing ran: the budget is given back
            self._settle(attempt, "refused")
            if _trusted(exc):
                raise
            raise _OwnRefusal(R_FAILED) from None
        try:
            status = go(prepared)
        except Exception:                                   # noqa: BLE001 - may have run: unknown, never retried
            status = "unknown"
        if status not in ("ok", "failed"):
            return "unknown"                                # the row was reserved unknown and stays so
        return status if self._settle(attempt, status) else "unknown"

    # --- the verbs --------------------------------------------------------------------------------------------
    def restart_service(self, order_id: Any, host: Any, service: Any, *, expect_sha256: Any,
                        event_id: Any) -> EffectResult:
        """Restart ``service`` on ``host`` under order ``order_id`` at version ``expect_sha256`` (the digest the
        desk's brief prints beside the order), answering the incident of the monitor event ``event_id`` (the incident is
        derived from the event, never an argument). Idempotent on (event, verb, host, service), whichever order asks."""
        order_id = _shaped(order_id, _ORDER_ID)
        host, service = _enum(host, HOSTS), _enum(service, SERVICES)
        sha, event_id = _shaped(expect_sha256, _SHA), _shaped(event_id, _EVENT_ID)
        return self._noted(order_id, "restart_service", event_id,
                           lambda: self._restart(order_id, host, service, sha, event_id),
                           {"host": host, "service": service})

    def _restart(self, order_id: str, host: str, service: str, sha: str, event_id: str) -> EffectResult:
        order, lines = self.gate(order_id, "restart_service", expect_sha256=sha)
        covering = [ln for ln in lines if host in ln["args"]["host"] and service in ln["args"]["service"]]
        if not covering:
            raise _OwnRefusal(R_LINE)
        if self.events is None or self.journal is None:
            raise _OwnRefusal(R_NOT_WIRED)
        argv = restart_argv(host, service)
        event = self._incident(event_id)
        args_digest = hashlib.sha256(f"{host}|{service}".encode()).hexdigest()
        key = idem_key(event_id, "restart_service", args_digest)
        what = f"restart_service {host} {service}"
        done = self._lookup(key)                            # a recorded result answers with no runner wired
        if done is not None and done.status != "refused":
            return self._recorded("restart_service", what, "exit 0", done.status)
        if self.runner is None:                             # needed only for an effect that would run now
            raise _OwnRefusal(R_NOT_WIRED)
        night = night_key(self._now(), self.night_cut_hour_utc)
        attempt, fresh = self._reserve(key=key, order=order, verb="restart_service", event_id=event_id,
                                       args_digest=args_digest, recipient=None,
                                       incident_scope=f"{service}|{event.incident_id}", night_scope=night,
                                       covering=covering)
        if not fresh:
            return self._recorded("restart_service", what, "exit 0", attempt.status)

        def go(_prepared: Any) -> str:
            assert self.runner is not None
            code = self.runner(argv)
            if type(code) is not int or code < 0:          # no exit code, a timeout, no start or a signal death: it
                return "unknown"                            # may have run
            return "ok" if code == 0 else "failed"

        status = self._dispatch(attempt, order_id, "restart_service", sha, order, go, event=event)
        return self._recorded("restart_service", what, "exit 0", status)

    def _lookup(self, key: str) -> Any:
        """The journal's row for ``key``, or None; a fault reading it refuses (:data:`R_FAILED`)."""
        assert self.journal is not None
        try:
            return self.journal.lookup(key)
        except Exception:                                   # noqa: BLE001 - an unreadable journal runs nothing
            raise _OwnRefusal(R_FAILED) from None

    def _incident(self, event_id: str) -> Event:
        """The monitor event ``event_id``, checked: a non-mail event of a monitor's own, authenticated or first party, in
        an incident, not older than the window. Anything else refuses."""
        assert self.events is not None
        try:
            event = self.events(event_id)
        except Exception:                                   # noqa: BLE001 - an executor's fault never carries text out
            raise _OwnRefusal(R_FAILED) from None
        if (not isinstance(event, Event) or event.event_id != event_id or event.kind not in _RESTART_KINDS
                or event.trust not in ("authenticated", "first_party") or type(event.incident_id) is not str
                or _INCIDENT_ID.fullmatch(event.incident_id) is None):
            raise _OwnRefusal(R_EVENT)
        self._event_age(event)
        return event

    def send_ack(self, order_id: Any, event_id: Any, *, expect_sha256: Any) -> EffectResult:
        """Send the ack for ``event_id`` under order ``order_id`` at version ``expect_sha256``. The recipient is
        derived here (never an argument) from the raw message the event's digest names, and must be on the order
        line's ``known_from``. The order is replayed again after the reservation and immediately before the send.
        Idempotent on the event, whichever order asks: a retry returns the recorded result and never sends twice."""
        order_id, event_id = _shaped(order_id, _ORDER_ID), _shaped(event_id, _EVENT_ID)
        sha = _shaped(expect_sha256, _SHA)
        return self._noted(order_id, "send_ack", event_id, lambda: self._ack(order_id, event_id, sha))

    def _ack(self, order_id: str, event_id: str, sha: str) -> EffectResult:
        order, lines = self.gate(order_id, "send_ack", expect_sha256=sha)
        if self.events is None or self.raw_source is None or self.journal is None:
            raise _OwnRefusal(R_NOT_WIRED)
        sender = None
        if self.transport is not None:                      # a wired transport speaks only as thirdmind@
            try:
                sender = getattr(self.transport, "sender", None)
            except Exception:                               # noqa: BLE001 - a faulting property says nothing either
                sender = None
            if type(sender) is not str or _SENDER.fullmatch(sender) is None:
                raise _OwnRefusal(R_NOT_WIRED)
        try:
            recipient, voice, covering, event = self._recipient(event_id, lines)
        except Exception as exc:                            # noqa: BLE001 - an executor's fault never carries text out
            if _trusted(exc):                               # only this module's own refusals (inbound_from, the
                raise                                       # event checks) pass; an executor's is R_FAILED
            raise _OwnRefusal(R_FAILED) from None
        incident = event.incident_id if type(event.incident_id) is str and _INCIDENT_ID.fullmatch(event.incident_id) else None
        key = idem_key(event_id, "send_ack", hashlib.sha256(b"").hexdigest())
        # the row's recorded recipient is checked on EVERY path that could answer or send: a recorded row (this lookup),
        # a row a racer reserved first, and a refused row taken again (both after the reservation, below)
        done = self._lookup(key)
        if done is not None and done.recipient != recipient:
            raise _OwnRefusal(R_SENDER)
        if done is not None and done.status != "refused":   # answered from the record: no drafter or transport needed
            return self._recorded("send_ack", "send_ack", "sent", done.status)
        if self.drafter is None or self.transport is None:
            raise _OwnRefusal(R_NOT_WIRED)
        assert sender is not None
        night = night_key(self._now(), self.night_cut_hour_utc)
        # reserved BEFORE any drafting: of two racing calls only the one holding the reservation drafts or sends
        attempt, fresh = self._reserve(key=key, order=order, verb="send_ack", event_id=event_id,
                                       args_digest=hashlib.sha256(b"").hexdigest(), recipient=recipient,
                                       incident_scope=f"{recipient}|{incident or event_id}",
                                       night_scope=f"{recipient}|{night}", covering=covering)
        if attempt.recipient != recipient:
            if fresh:
                self._settle(attempt, "refused")            # nothing ran: the retake is given back
            raise _OwnRefusal(R_SENDER)
        if not fresh:
            return self._recorded("send_ack", "send_ack", "sent", attempt.status)

        def compose() -> AckMessage:
            assert self.drafter is not None
            try:
                draft = self.drafter(event_id)
            except Exception as exc:                        # noqa: BLE001
                if _trusted(exc):
                    raise
                raise _OwnRefusal(R_FAILED) from None
            if draft is None:                               # the drafter wrote nothing; any other non-Draft has no voice
                raise _OwnRefusal(R_DRAFT)
            if (type(draft) is not Draft or type(draft.voice) is not str or draft.voice not in DISCLOSURES
                    or draft.voice != voice):               # the voice the order signed is the only one disclosed
                raise _OwnRefusal(R_VOICE)
            why = ack_text_refusal(draft.text)              # on the drafter's text alone, before any disclosure is added
            if why is not None:
                raise _OwnRefusal(why)
            if _holds_disclosure(draft.text):
                raise _OwnRefusal(R_DISCLOSURE)
            return AckMessage(sender=sender, recipient=recipient, subject=ACK_SUBJECT,
                              body=draft.text.strip() + "\n\n" + DISCLOSURES[voice],
                              headers={"Auto-Submitted": "auto-replied"})

        def go(message: AckMessage) -> str:
            assert self.transport is not None
            self.transport.send(message)                    # a fault here (a timeout) may have delivered: unknown
            return "ok"

        status = self._dispatch(attempt, order_id, "send_ack", sha, order, go, event=event, compose=compose)
        return self._recorded("send_ack", "send_ack", "sent", status)

    def _mail_event(self, event_id: str) -> tuple[Event, bytes]:
        """The mail event ``event_id`` and the raw message its digest names, checked; anything else refuses."""
        assert self.events is not None and self.raw_source is not None
        event = self.events(event_id)
        if (not isinstance(event, Event) or event.event_id != event_id or event.kind != "mail"
                or event.trust != "authenticated" or event.fields.get("auth") != "pass"
                or event.fields.get("sender_class") != "known" or type(event.body_ref) is not str
                or _BODY_REF.fullmatch(event.body_ref) is None):
            raise _OwnRefusal(R_EVENT)
        self._event_age(event)
        raw = self.raw_source(event.body_ref)
        if not isinstance(raw, (bytes, bytearray)) or not hmac.compare_digest(
                "sha256:" + hashlib.sha256(bytes(raw)).hexdigest(), event.body_ref):
            raise _OwnRefusal(R_EVENT)                    # the bytes are not the ones the event's digest names
        return event, bytes(raw)

    def _recipient(self, event_id: str, lines: list[Any]) -> tuple[str, str, list[Any], Event]:
        event, raw = self._mail_event(event_id)
        recipient = inbound_from(raw)
        voices: set[Any] = set()
        covering: list[Any] = []
        for ln in lines:
            if recipient in {a.strip().lower() for a in ln["args"]["known_from"]}:
                named = ln["args"]["voice"]
                if not isinstance(named, (list, tuple)) or len(named) != 1:
                    raise _OwnRefusal(R_LINE)
                voices.add(named[0])
                covering.append(ln)
        if not voices:
            unlisted = _OwnRefusal(R_FROM)
            unlisted.coverage = True                        # the sender read fine and is on no line: a gap
            raise unlisted
        if len(voices) != 1 or next(iter(voices)) not in DISCLOSURES:   # one voice, or the disclosure could be false
            raise _OwnRefusal(R_LINE)
        return recipient, next(iter(voices)), covering, event

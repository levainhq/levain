"""levain.consent.sign — a statement signed on the phone with the operator's passkey, for a caller outside levain to
verify with stock ``ssh-keygen -Y verify``.

The signature is a user-verified passkey signature (Face ID, Touch ID or the device passcode, as the authenticator
reports it): levain refuses one without the user-verified flag before it records anything. A stock verify proves
that the key signed these bytes in this namespace; it does not check how the user was verified.

A caller (a script on the laptop) writes a sign request into ``<queue_dir>/sign/<id>.json``::

    {"format": "levain-sign-request/1", "id": "<id>", "namespace": "<ns>", "message": "<text>",
     "title": "<text>", "requester": "<text>", "expires_at": "<UTC timestamp>"}

(written whole: a temporary file, then a rename). Or a CHOICE request, one card with several options, of which the
operator signs exactly one::

    {"format": "levain-sign-choice/1", ... the same seven keys ...,
     "options": [{"key": "a", "text": "<text>"}, {"key": "b", "text": "<text>"}],
     "recommended": "a", "write_in": true}

``recommended`` (optional) names one of the option keys: the page marks that option recommended and never selects
it, and it is not signed (the signature covers what the operator chose, not what was suggested). ``write_in``
(optional, default false) offers the operator a text box beside the options: the words typed there sign as the
choice line with the reserved key :data:`WRITE_IN_KEY` (``*``), outside the option-key grammar, so no option collides
with it and a verifier built before write-ins refuses it.

A choice request's signature is over the message with the chosen option appended as its last line, so a verifier reads
the decision from the bytes that verified, never from a file beside them (:func:`choice_line`, :func:`signed_bytes`,
:func:`parse_choice`)::

    <message> + "\n" + "levain-sign-choice/1 chosen (<key>): <text>"
    <message> + "\n" + "levain-sign-choice/1 chosen (*): <the operator's words>"     (a write-in)

``levain consent phone --sign-namespace NS`` lists the unexpired
requests in the namespaces it was given; the operator reads the whole message on the phone and signs it with an
enrolled passkey, or denies it. A file in the sign directory that is not a well-formed sign request is listed as
refused, with the reason, never skipped. The result lands beside the request:

  - ``<id>.sig``: the armored SSHSIG over the message's UTF-8 bytes, in the request's namespace;
  - ``<id>.signed.json``: ``{signer_fp, public_key, namespace, message_sha256, signed_at}`` (for a choice request also
    ``choice``, the key (``*`` for a write-in), and ``signed_message``, the exact text signed; ``message_sha256`` is that text's), written
    last: it is the
    completion marker, and only its presence makes a request signed. A ``.sig`` without it is the orphan of an
    answer that did not finish, and the next successful answer replaces it;
  - ``<id>.denied``: ``{at, request_sha256}``, when the operator denied it (for a choice request: none of its
    options): the version of the request the page
    showed (the file's SHA-256, or for a file that could not be read a version taken from its metadata), sent back
    with the deny, which a request changed since is refused. The request file is never deleted here, so a caller
    can tell a denial from an expiry.

The rules, each a refusal before anything is signed:

  - a namespace is ``[a-z0-9][a-z0-9.-]{2,63}``, never begins ``levain``, is none of :data:`REFUSED_NAMESPACES`
    (ssh's conventional ``git``, ``file`` and ``email``) and is no entry or family of levain's namespace registry
    (:mod:`levain.contracts.namespaces`): a passkey's challenge is the SSHSIG signed-data blob of the message
    (:func:`levain.consent.passkey.challenge`), so a request in a namespace levain itself verifies (a consent
    answer's, a ruling's, a team ledger commit's in ``git``) would make a phone signature that levain accepts as one.
    Refused at the flag and again at every signing, whatever the flags say;
  - what the operator reads is what is signed: the message is UTF-8 text of at most :data:`MESSAGE_MAX` bytes, and a
    message, title or requester holding a control character other than newline and tab, a Unicode format character
    (bidi controls, zero-width characters), a line or paragraph separator, a space other than U+0020, or a
    private-use or unassigned code point is listed as refused and never signed, since each can render differently
    from its bytes; no message holds :data:`CHOICE_MARK` (only levain writes the choice line, so a plain statement
    can never verify as a choice); a choice request has 2 to :data:`OPTIONS_MAX` options, each key
    ``[a-z0-9]{1,8}`` and each text one line (no newline or tab, no space at either end) of at most 200 characters,
    keys and texts distinct; a ``recommended`` that is not one of those keys, or a ``write_in`` that is not a
    boolean, refuses the request; a write-in obeys the rules an option's text does (:func:`write_in_problem`), at
    the challenge and again in :func:`parse_choice`, and only on a request filed with ``write_in: true``;
  - the operator signs the option tapped: the challenge names the option's key, and the challenge, the answer and the
    files written are bound to that key; a challenge for a key the request does not offer, or a choice request's
    challenge naming none, is refused;
  - the operator signs what was shown: the listing carries each request file's SHA-256, the page sends it back with
    the challenge, and a challenge that names another (the file changed since it was listed) or none is refused;
  - the signer is an active passkey enrolled on this server's https name in the cockpit the queue's consent hook
    verifies against (a laptop key never signs here: the point is the operator's own passkey);
  - the challenge is bound to the request file's SHA-256: an answer for a file whose bytes changed since its
    challenge, or that expired, is refused, checked again after the signature verifies and just before the result
    is written (the verify runs outside the lock, so a file changed or expired during it is refused too);
  - the signature passes the same checks as a consent answer's: user verification and a page whose origin is
    exactly ``https://<rp id>``, or exactly the Signer's ``page_origin`` when one is set (the phone port's own
    ``http://localhost:<port>`` on ``localhost``; the https name and port a cockpit is served on)
    (:func:`levain.consent.passkey.answer_signature_problem`), then ``ssh-keygen`` in the request's namespace
    (:func:`levain.authority.verify_link_sig`).

One bad file never takes the listing down: a file that cannot be read, is not a regular file (a FIFO is opened
non-blocking and refused, never waited on) or holds a time that cannot be converted is a refused row, and the
consent requests beside it still list. A file whose namespace is malformed or one no signer may claim is listed as
refused (and may be denied); with no ``--sign-namespace`` nothing in the directory is listed. The listing reads at
most :data:`LIST_MAX` requests and says how many it did not read.

Nothing here touches the consent queue's database or the authority log: the files live beside them.

Known edges:

  - two Signers on one queue dir (the phone port's, and the one a cockpit is given as
    :func:`levain.web_server.make_server`'s ``signer``) may each challenge
    one request; the last check and the writes of an answer or a deny run under an ``flock`` on ``sign/.lock``
    held across processes, so the first decision wins whole and the other is refused (409), and a ``.sig`` always
    belongs to the ``.signed.json`` beside it;
  - identical messages in one namespace share a challenge and so a signature: the caller embeds a unique id (a
    report id, a period) in the message, and its verifier checks it. A choice is no exception: "release 1.0?" with
    "(a): yes" signed once verifies on every later request with the same message;
  - a choice's verifier compares the whole message AND the chosen option's key and text with its own copy: a
    same-uid writer can reorder or swap option texts before the listing, and the signature is then real over the
    text the operator tapped, under a key the caller filed for another text;
  - the title and requester are shown on the card but are not signed: the page says so;
  - a deep link to a request past the first :data:`LIST_MAX` in name order finds no card (the page says it is not
    open here);
  - look-alike letters and NFC vs NFD forms are not refused: a caller ships the exact message bytes as a file, never
    re-typed or normalised.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import secrets
import stat
import threading
import unicodedata
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

from levain import unshowable
from levain.sign_request_id import ID as _ID, request_id_ok

__all__ = ["CHOICE_FORMAT", "CHOICE_MARK", "ERROR_WORDS", "LIST_MAX", "LOCK_WAIT_S", "MESSAGE_MAX", "OPTIONS_MAX",
           "REFUSED_NAMESPACES", "REQUEST_FORMAT", "SIGN_DIR", "WRITE_IN_KEY", "SignRefused", "SignRequest", "Signer",
           "choice_line", "deep_link", "error_word", "namespace_problem", "parse_choice", "passkey_key_source",
           "read_request", "request_id_ok", "sign_dir", "sign_root", "signed_bytes", "state_of", "write_in_problem"]

REQUEST_FORMAT = "levain-sign-request/1"
CHOICE_FORMAT = "levain-sign-choice/1"
CHOICE_MARK = CHOICE_FORMAT + " chosen "   # opens the choice line; refused inside any message, so only levain writes it
OPTIONS_MAX = 8                    # buttons on one card
SIGN_DIR = "sign"
MESSAGE_MAX = 8 * 1024             # bytes of UTF-8: a statement a person reads whole on a phone
_TEXT_MAX = 200                    # characters of a title or a requester
_FILE_MAX = 64 * 1024              # a request file past this is not read
LIST_MAX = 50                      # open requests one listing shows; past it the page shows how many were left
_SCAN_MAX = 4096                   # directory entries one listing looks at
_NAMESPACE = re.compile(r"[a-z0-9][a-z0-9.-]{2,63}")
_KEYS = frozenset({"format", "id", "namespace", "message", "title", "requester", "expires_at"})
_CHOICE_KEYS = _KEYS | {"options"}
_CHOICE_OPTIONAL = frozenset({"recommended", "write_in"})
_OPTION_KEY = re.compile(r"[a-z0-9]{1,8}")
WRITE_IN_KEY = "*"                 # the key a write-in signs under: never an option key, which is [a-z0-9]{1,8}
# ssh's conventional namespaces: git's commit signing (levain's team ledger verifies in it), file and email
REFUSED_NAMESPACES = frozenset({"git", "file", "email"})
# how long an answer or a deny waits for the queue lock another Signer holds (its writes take milliseconds), then a 503
LOCK_WAIT_S = 5.0
_LOCK_POLL_S = 0.02


class SignRefused(Exception):
    """A sign request, challenge or answer refused, with the words the page shows. ``status`` is the HTTP status the
    phone port answers with."""

    def __init__(self, message: str, status: int = 422) -> None:
        super().__init__(message)
        self.status = status


# the error word a port answers a SignRefused with, by its status; any other status is "sign_refused". ONE table: the
# phone port (levain.phone_server) and the cockpit (levain.web_server) both import it, so the two cannot drift
ERROR_WORDS = {400: "bad_request", 404: "not_found", 409: "conflict", 503: "unavailable"}


def error_word(status: int) -> str:
    """The error word for a :class:`SignRefused` of ``status`` (:data:`ERROR_WORDS`, else ``sign_refused``)."""
    return ERROR_WORDS.get(status, "sign_refused")


def sign_root(queue_dir: Path) -> Path:
    """The ONE cockpit the queue's consent hooks name, whose passkeys sign a sign request or a dispatch job.
    SignRefused (503) when no hook names one, or when they name several (which passkeys sign is then not one
    answer)."""
    from levain.consent.answering import hook_roots

    roots = hook_roots(queue_dir)
    if not roots:
        raise SignRefused("no consent hook names a cockpit, so no passkey can sign here: this queue dir names no "
                          "cockpit: write claude_code.json with cockpit_data_root", 503)
    if len(roots) > 1:
        raise SignRefused("the consent hooks name more than one cockpit, so which passkeys sign is not one "
                          "answer: nothing is signed", 503)
    return roots[0]


def passkey_key_source(queue_dir: Path, rp_id: str, *, cache: Any = None) -> "Callable[[], dict[str, Any]]":
    """What may sign a sign request, as the ``keys`` a :class:`Signer` takes: a callable returning the active passkey
    keys (fingerprint -> :class:`levain.authority.AuthorityKey`) enrolled on ``rp_id`` in :func:`sign_root`'s
    cockpit, re-read on every call (the hooks and the authority log both, never a snapshot). A laptop key never
    signs. SignRefused (503) when no single cockpit is named or its keys cannot be read now. ``cache`` is a
    :class:`levain.authority.ReplayCache` shared with the caller's other replays; None keeps one for this source."""
    import sqlite3

    from levain.authority import AuthorityError, ReplayCache, anchor_path
    from levain.consent.answering import passkey_keys
    from levain.consent.queue import authority_source

    replay = ReplayCache() if cache is None else cache

    def keys() -> "dict[str, Any]":
        root = sign_root(queue_dir)
        try:
            source = authority_source(root / "cockpit.db", anchor=anchor_path(root), cache=replay)
            return {k.fingerprint: k for k in passkey_keys(source(), rp_id)}
        except (AuthorityError, OSError, sqlite3.Error) as exc:
            raise SignRefused(f"the cockpit's keys cannot be read now: {exc}", 503) from None

    return keys


def namespace_problem(namespace: object) -> str | None:
    """Why ``namespace`` is not one the phone may sign in, or None."""
    from levain.contracts.namespaces import FAMILIES, REGISTRY

    if not isinstance(namespace, str) or _NAMESPACE.fullmatch(namespace) is None:
        return f"namespace {str(namespace)[:80]!r} is not [a-z0-9][a-z0-9.-]{{2,63}}"
    if namespace.startswith("levain"):
        return f"namespace {namespace!r} begins 'levain': levain's own namespaces are never signed on request"
    if namespace in REFUSED_NAMESPACES or namespace in REGISTRY or namespace.startswith(tuple(FAMILIES)):
        return f"namespace {namespace!r} is one levain or ssh verifies signatures in: never signed on request"
    return None


def _text_problem(text: str, what: str) -> str | None:
    # characters that can render differently from their bytes (levain.unshowable); newline and tab are text
    found = unshowable.first_hidden(text, allow="\n\t")
    if found is not None:
        ch = found[1]
        return (f"the {what} holds U+{ord(ch):04X} ({unicodedata.name(ch, 'unnamed')}), which can show differently "
                "from what is signed")
    return None


def choice_line(key: str, text: str) -> str:
    """The line a choice request's signature appends to its message for option ``key`` with text ``text``."""
    return f"{CHOICE_MARK}({key}): {text}"


def parse_choice(signed: bytes, *, write_in: bool = False) -> "tuple[str, str, str]":
    """``(message, key, text)`` read back from the bytes a choice signature covers (the verified bytes, never a file
    beside them). :class:`ValueError` when they are not a message followed by one choice line. A write-in comes back
    as key :data:`WRITE_IN_KEY` with the operator's words, and only when the caller's request offered one
    (``write_in=True``); its words are refused here on the same rules as at the challenge (:func:`write_in_problem`),
    so a hand-built signature over words the page would never sign does not parse."""
    text = signed.decode("utf-8")
    message, sep, last = text.rpartition("\n")
    m = re.fullmatch(re.escape(CHOICE_MARK) + r"\(([a-z0-9]{1,8}|\*)\): ([^\n]+)", last)
    if not sep or m is None or CHOICE_MARK in message:
        raise ValueError("not a message followed by one levain choice line")
    if m.group(1) == WRITE_IN_KEY:
        if not write_in:
            raise ValueError("a write-in choice line on a request that offered no write-in")
        problem = write_in_problem(m.group(2))
        if problem is not None:
            raise ValueError(problem)
    return message, m.group(1), m.group(2)


def deep_link(page: str, request_id: str, token: str | None = None) -> str:
    """The phone page link that opens straight onto sign request ``request_id``'s card: ``page`` (the printed
    ``https://<name>/``) with ``#sign=<id>``, and this run's ``phone_token`` first when given (a browser that has not
    opened this run's link needs it; the fragment never reaches the server or its logs)."""
    from urllib.parse import quote

    if _ID.fullmatch(request_id or "") is None:
        raise ValueError(f"{str(request_id)[:80]!r} is not a sign request id")
    parts = ([f"phone_token={quote(token, safe='')}"] if token else []) + [f"sign={request_id}"]
    return page.split("#", 1)[0] + "#" + "&".join(parts)


def _option_text_problem(text: object, what: str) -> str | None:
    """Why ``text`` cannot be one choice line's text (an option's or a write-in's, named ``what``), or None: one
    line of 1 to :data:`_TEXT_MAX` characters, no space at either end, nothing that can show differently from its
    bytes (no newline or tab either), and no :data:`CHOICE_MARK`."""
    if not isinstance(text, str) or not text or len(text) > _TEXT_MAX:
        return f"{what} is empty or over {_TEXT_MAX} characters"
    if text != text.strip(" "):
        return f"{what} begins or ends with a space, which a button does not show"
    hidden = unshowable.first_hidden(text, allow="")
    if hidden is not None:
        return (f"{what} holds U+{ord(hidden[1]):04X} ({unicodedata.name(hidden[1], 'unnamed')}), which can show "
                "differently from what is signed")
    if CHOICE_MARK in text:
        return f"{what} holds {CHOICE_MARK.strip()!r}"
    return None


def write_in_problem(text: object) -> str | None:
    """Why ``text`` cannot be signed as a write-in, or None: the rules an option's text obeys."""
    return _option_text_problem(text, "the write-in")


def _options_problem(options: object) -> "tuple[tuple[tuple[str, str], ...], str | None]":
    """``(options, None)`` as ``((key, text), ...)``, or ``((), why)`` for a choice request's options that are not
    2 to :data:`OPTIONS_MAX` distinct one-line options."""
    if not isinstance(options, list) or not 2 <= len(options) <= OPTIONS_MAX:
        return (), f"a choice request has a list of 2 to {OPTIONS_MAX} options"
    got: list[tuple[str, str]] = []
    for o in options:
        if not isinstance(o, dict) or set(o) != {"key", "text"} or not all(isinstance(o[k], str) for k in o):
            return (), 'each option is {"key": <text>, "text": <text>}'
        key, text = o["key"], o["text"]
        if _OPTION_KEY.fullmatch(key) is None:
            return (), f"option key {key[:20]!r} is not [a-z0-9]{{1,8}}"
        problem = _option_text_problem(text, f"option {key}'s text")
        if problem is not None:
            return (), problem
        got.append((key, text))
    if len({k for k, _ in got}) != len(got) or len({t for _, t in got}) != len(got):
        return (), "a choice request's option keys and texts are each distinct"
    return tuple(got), None


@dataclass(frozen=True)
class SignRequest:
    """One sign request file as read: its version (its bytes' SHA-256, or for a file that could not be read one
    taken from its metadata) and, unless ``refused``, its fields. A refused request keeps what could be read of it:
    ``namespace`` is ``""`` when the file names no well-formed one, and ``expires_at`` is None when its expiry could
    not be read."""
    id: str
    sha256: str
    namespace: str
    message: str
    title: str
    requester: str
    expires_at: "datetime | None"
    refused: str | None = None          # why it is never signable, shown to the operator in place of its text
    options: "tuple[tuple[str, str], ...]" = ()   # a choice request's (key, text) in file order; () for a plain one
    recommended: str | None = None      # a choice request's suggested option key: shown, never selected, never signed
    write_in: bool = False              # a choice request that also takes the operator's own words (key "*")


def signed_bytes(request: SignRequest, choice: str | None, write_in: str | None = None) -> bytes:
    """The bytes a signature on ``request`` covers: a plain request's message, or a choice request's message with the
    line for option ``choice`` appended, or for ``choice`` :data:`WRITE_IN_KEY` the line holding ``write_in`` (the
    operator's words). :class:`SignRefused` for a choice that does not fit the request."""
    if not request.options:
        if choice is not None or write_in is not None:
            raise SignRefused(f"sign request {request.id} offers no options: its challenge names none", 400)
        return request.message.encode("utf-8")
    if choice == WRITE_IN_KEY:
        if not request.write_in:
            raise SignRefused(f"sign request {request.id} offers no write-in", 400)
        problem = write_in_problem(write_in)
        if problem is not None:
            raise SignRefused(problem, 400)
        return (request.message + "\n" + choice_line(WRITE_IN_KEY, write_in)).encode("utf-8")
    if write_in is not None:
        raise SignRefused(f"sign request {request.id}: words go only with the write-in choice ({WRITE_IN_KEY})", 400)
    text = dict(request.options).get(choice) if isinstance(choice, str) else None
    if text is None:
        raise SignRefused(f"sign request {request.id} offers options "
                          f"{', '.join(k for k, _ in request.options)}: the challenge names one of them", 400)
    return (request.message + "\n" + choice_line(choice, text)).encode("utf-8")


def sign_dir(queue_dir: Path) -> Path:
    return Path(queue_dir) / SIGN_DIR


def _read_bytes(path: Path) -> bytes:
    """The regular file at ``path`` (never through a symlink), at most :data:`_FILE_MAX` bytes. Opened non-blocking,
    so a FIFO in its place is refused, never waited on."""
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0))
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise SignRefused("the request is not a regular file")
        with os.fdopen(fd, "rb") as fh:
            fd = -1
            data = fh.read(_FILE_MAX + 1)
    finally:
        if fd >= 0:
            os.close(fd)
    if len(data) > _FILE_MAX:
        raise SignRefused(f"the request file is over {_FILE_MAX} bytes")
    return data


def _parse_expiry(text: object) -> datetime:
    from levain.cockpit.consent import ConsentError, parse_stamp

    try:
        return parse_stamp(text)                     # levain's own stamp format
    except ConsentError:
        pass
    try:
        when = datetime.fromisoformat(text) if isinstance(text, str) else None
        if when is None or when.tzinfo is None or when.utcoffset() is None:
            raise ValueError("no zone")
        return when.astimezone(timezone.utc)
    except (OverflowError, ValueError):              # an in-range local time can overflow on conversion to UTC
        raise SignRefused(f"expires_at {str(text)[:40]!r} is not an ISO 8601 time with a zone") from None


def read_request(queue_dir: Path, request_id: str) -> SignRequest:
    """The request ``request_id`` as its file holds it now. :class:`SignRefused` (404) only when there is no such
    file; any file there that cannot be shown truthfully, or is not a well-formed sign request, comes back with
    ``refused`` set, so the operator sees it and the caller can be told."""
    if _ID.fullmatch(request_id or "") is None:
        raise SignRefused("no such sign request", 404)

    path = sign_dir(queue_dir) / f"{request_id}.json"

    def refused(why: str, digest: str = "", data: Any = None) -> SignRequest:
        ns = data.get("namespace") if isinstance(data, dict) else None
        return SignRequest(id=request_id, sha256=digest or _stat_version(path),
                           namespace=ns if isinstance(ns, str) and _NAMESPACE.fullmatch(ns) else "",
                           message="", title="", requester="", expires_at=None, refused=f"not a sign request: {why}")

    try:
        raw = _read_bytes(path)
    except FileNotFoundError:
        raise SignRefused("no such sign request", 404) from None
    except SignRefused as exc:
        return refused(str(exc))
    except OSError as exc:
        return refused(f"it cannot be read: {exc.strerror or exc}")
    digest = hashlib.sha256(raw).hexdigest()
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        return refused("it is not UTF-8 JSON", digest)
    if not isinstance(data, dict) or not (set(data) == _KEYS or _CHOICE_KEYS <= set(data) <= _CHOICE_KEYS
                                          | _CHOICE_OPTIONAL):
        return refused(f"a sign request has exactly the keys {sorted(_KEYS)}, and a choice request also "
                       f"'options' and may have {sorted(_CHOICE_OPTIONAL)}", digest, data)
    want = CHOICE_FORMAT if "options" in data else REQUEST_FORMAT
    if data["format"] != want:
        return refused(f"format {str(data['format'])[:40]!r} is not {want!r}", digest, data)
    if data["id"] != request_id:
        return refused("its id is not its file name", digest, data)
    if not all(isinstance(data[k], str) for k in ("namespace", "message", "title", "requester")):
        return refused("namespace, message, title and requester are strings", digest, data)
    try:
        expires_at = _parse_expiry(data["expires_at"])
    except SignRefused as exc:
        return refused(str(exc), digest, data)
    namespace, message, title, requester = data["namespace"], data["message"], data["title"], data["requester"]
    options, problem = _options_problem(data["options"]) if "options" in data else ((), None)
    recommended, write_in = data.get("recommended"), data.get("write_in", False)
    if problem is None and "recommended" in data and (not isinstance(recommended, str)
                                                      or recommended not in {k for k, _ in options}):
        problem = f"recommended {str(recommended)[:20]!r} is not one of the request's option keys"
    if problem is None and not isinstance(write_in, bool):
        problem = "write_in is true or false"
    problem = namespace_problem(namespace) or problem
    longest = max((len(("\n" + choice_line(k, t)).encode("utf-8")) for k, t in options), default=0)
    if write_in is True:                # the longest write-in: _TEXT_MAX characters of at most 4 UTF-8 bytes each
        longest = max(longest, len(("\n" + choice_line(WRITE_IN_KEY, "")).encode("utf-8")) + 4 * _TEXT_MAX)
    if problem is None and len(message.encode("utf-8", "surrogatepass")) + longest > MESSAGE_MAX:
        problem = f"the message{' with its longest choice line' if options else ''} is over {MESSAGE_MAX} bytes"
    if problem is None and not message:
        problem = "the message is empty"
    if problem is None and CHOICE_MARK in message:
        problem = f"the message holds {CHOICE_MARK.strip()!r}, which only levain writes"
    if problem is None and (len(title) > _TEXT_MAX or len(requester) > _TEXT_MAX):
        problem = f"the title or requester is over {_TEXT_MAX} characters"
    for text, what in ((message, "message"), (title, "title"), (requester, "requester")):
        problem = problem or _text_problem(text, what)
    return SignRequest(id=request_id, sha256=digest, namespace=namespace if _NAMESPACE.fullmatch(namespace) else "",
                       message=message, title=title, requester=requester, expires_at=expires_at, refused=problem,
                       options=options if problem is None else (),
                       recommended=recommended if problem is None else None,
                       write_in=write_in is True and problem is None)


def _stat_version(path: Path) -> str:
    """The version of a request file that could not be read: what its metadata says (never followed through a
    symlink), so a deny names the file the page showed and a file replaced since is refused. ``""`` when it is gone."""
    try:
        st = os.lstat(path)
    except OSError:
        return ""
    seen = f"{st.st_dev}:{st.st_ino}:{st.st_mode}:{st.st_size}:{st.st_mtime_ns}:{st.st_ctime_ns}"
    return "stat:" + hashlib.sha256(seen.encode("ascii")).hexdigest()


def state_of(queue_dir: Path, request_id: str) -> str:
    """``signed``, ``denied`` or ``open``: what the files beside the request say. ``<id>.signed.json`` is the
    completion marker: a ``.sig`` alone is an orphan and leaves the request open."""
    d = sign_dir(queue_dir)
    if (d / f"{request_id}.signed.json").exists():
        return "signed"
    if (d / f"{request_id}.denied").exists():
        return "denied"
    return "open"


def _write_tmp(path: Path, text: str) -> Path:
    """``text`` in a new 0600 temporary file beside ``path``, flushed to disk."""
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(6)}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return tmp


def _fsync_dir(directory: Path) -> None:
    """The directory's entries to disk, so a result reported written survives a crash."""
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_new(path: Path, text: str) -> None:
    """``text`` at ``path``, mode 0600, whole or not at all, and never over a file already there."""
    tmp = _write_tmp(path, text)
    try:
        os.link(tmp, path)             # fails when path exists: a result is never overwritten
    finally:
        tmp.unlink(missing_ok=True)
    _fsync_dir(path.parent)


def _write_over(path: Path, text: str) -> None:
    """``text`` at ``path``, mode 0600, whole or not at all, replacing what is there (an orphan ``.sig``)."""
    tmp = _write_tmp(path, text)
    try:
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)
    _fsync_dir(path.parent)


def _publish(write: Any, path: Path, text: str, exists: str) -> None:
    """``write(path, text)``: a result already at ``path`` is a 409 saying ``exists``, any other failure to write is a
    503 naming the path and the error, never an internal error the page cannot read."""
    try:
        write(path, text)
    except FileExistsError:
        raise SignRefused(exists, 409) from None
    except OSError as exc:
        raise SignRefused(f"could not write {path}: [Errno {exc.errno}] {exc.strerror or exc}", 503) from None


@contextmanager
def _queue_lock(queue_dir: Path) -> Iterator[None]:
    """An exclusive ``flock`` on ``<queue_dir>/sign/.lock`` around the last check and the writes of an answer or a
    deny, held across every Signer on the queue in any process (a Signer's own lock orders only its own threads).
    A fresh open per acquisition, so two Signers in one process contend too; closing the descriptor releases it. A
    sign directory that cannot hold the lock (flock's own OSError: ENOLCK, ENOTSUP) is a 503, never an unlocked
    write, and so is a lock another holder keeps past :data:`LOCK_WAIT_S`; the descriptor is closed on every path."""
    import time

    path = sign_dir(queue_dir) / ".lock"
    try:
        fd = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    except OSError as exc:
        raise SignRefused(f"could not lock {path}: [Errno {exc.errno}] {exc.strerror or exc}", 503) from None
    try:
        deadline = time.monotonic() + LOCK_WAIT_S
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise SignRefused(f"could not lock {path}: another signer has held it past {LOCK_WAIT_S:g}s; "
                                      "nothing was written, try again", 503) from None
                time.sleep(_LOCK_POLL_S)
            except OSError as exc:
                raise SignRefused(f"could not lock {path}: [Errno {exc.errno}] {exc.strerror or exc}", 503) from None
        yield
    finally:
        os.close(fd)


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Signer:
    """The phone port's sign side over the queue at ``queue_dir``: lists, challenges, verifies and records. ``keys()``
    returns the active passkey keys that may sign (fingerprint -> :class:`levain.authority.AuthorityKey`), or raises
    :class:`SignRefused` saying why there are none to ask. ``page_origin`` is the page a passkey's assertion must
    come from exactly, set by the caller: the phone port's own origin when its name is ``localhost`` (else None,
    which holds the page to ``https://<rp_id>``), or the https name and port a cockpit serves the ceremony on
    (:func:`levain.web_server.make_server` refuses a missing or malformed one). A server that takes the Signer pins
    it (:meth:`pin_page_origin`): from then on a different ``page_origin`` is refused, so the origin every
    verification reads is the one the server checked, and nothing can move it under a ceremony in flight."""

    def __init__(self, queue_dir: Path, namespaces: "tuple[str, ...]", rp_id: str, keys: Any) -> None:
        for ns in namespaces:
            problem = namespace_problem(ns)
            if problem is not None:
                raise ValueError(f"--sign-namespace: {problem}")
        self.queue_dir, self.namespaces, self.rp_id, self._keys = Path(queue_dir), tuple(namespaces), rp_id, keys
        self._page_origin: str | None = None
        self._pinned = False
        self._lock = threading.Lock()
        # request id -> (file sha256, signer fp, chosen option key or None, the write-in's words or None) at challenge
        self._challenged: dict[str, tuple[str, str, str | None, str | None]] = {}

    @property
    def page_origin(self) -> str | None:
        """The page a passkey's assertion must come from exactly (see the class doc)."""
        return self._page_origin

    @page_origin.setter
    def page_origin(self, value: str | None) -> None:
        with self._lock:
            if self._pinned and value != self._page_origin:
                raise ValueError(f"refusing page_origin {str(value)[:120]!r}: a server holds this Signer pinned to "
                                 f"{self._page_origin!r}; build a new Signer for another page")
            self._page_origin = value

    def pin_page_origin(self, problem: "Callable[[str | None], str | None]") -> None:
        """Pin ``page_origin`` as it is now, if ``problem`` (called on it under the same lock a set takes) says
        nothing is wrong with it; else raise ``ValueError`` with the problem and pin nothing. Pinning an already
        pinned Signer checks it again and changes nothing."""
        with self._lock:
            found = problem(self._page_origin)
            if found is not None:
                raise ValueError(found)
            self._pinned = True

    def _ours(self, request: SignRequest) -> bool:
        """A request in a namespace this port signs, or a refused one whose namespace no signer could own (malformed,
        or one levain or ssh verifies in): the operator still sees it, and may deny it. With no namespaces at all,
        nothing is ours."""
        if not self.namespaces:
            return False
        return request.namespace in self.namespaces or (
            request.refused is not None and namespace_problem(request.namespace) is not None)

    def _open(self, request_id: str) -> SignRequest:
        """The request, refused unless it is open, unexpired, signable and in a namespace this port signs."""
        request = read_request(self.queue_dir, request_id)
        if not self._ours(request):
            raise SignRefused("no such sign request", 404)        # outside the flags: never shown, never signed
        state = state_of(self.queue_dir, request_id)
        if state != "open":
            raise SignRefused(f"sign request {request_id} is already {state}", 409)
        if request.expires_at is not None and request.expires_at <= _now():
            raise SignRefused(f"sign request {request_id} expired at {request.expires_at.isoformat()}", 409)
        if request.refused is not None:
            raise SignRefused(f"sign request {request_id} is refused: {request.refused}")
        return request

    def signers(self) -> list[str]:
        """The fingerprints of the active passkeys that may sign here (what a listing names as its signers), or
        :class:`SignRefused` saying why there are none to ask."""
        return list(self._keys())

    def listing(self, signers: "list[str]") -> list[dict[str, Any]]:
        """The rows of :meth:`page`."""
        return self.page(signers)["rows"]

    def page(self, signers: "list[str]") -> dict[str, Any]:
        """``{rows, overflow}``: every open, unexpired request in an allowed namespace, with its file's ``sha256``
        (what the page sends back with a challenge or a deny), read in name order up to :data:`LIST_MAX` files;
        ``overflow`` counts the request files not read (at least that many when the directory holds more than one
        listing looks at). A refused one carries ``refused``, its ``sha256`` and none of its text: what cannot be
        shown truthfully is not shown, and a file that is not a sign request is listed as refused, never skipped.
        A challenge whose request is no longer listed (gone, expired, answered or past the cap) is dropped."""
        if not self.namespaces:
            return {"rows": [], "overflow": 0}
        names: list[str] = []
        more = False
        try:
            with os.scandir(sign_dir(self.queue_dir)) as entries:
                for n, entry in enumerate(entries):
                    if n >= _SCAN_MAX:
                        more = True
                        break
                    rid = entry.name[:-5] if entry.name.endswith(".json") else ""
                    if _ID.fullmatch(rid) is not None:
                        names.append(rid)      # not a result file (<id>.signed.json) or a temporary one
        except OSError:
            names = []
        names.sort()
        now, rows, read = _now(), [], 0
        for rid in names:
            if len(rows) >= LIST_MAX:
                break
            read += 1
            try:
                if state_of(self.queue_dir, rid) != "open":   # a decided request is never parsed again
                    continue
                request = read_request(self.queue_dir, rid)
                if not self._ours(request) or (request.expires_at is not None and request.expires_at <= now):
                    continue
            except SignRefused:
                continue                      # gone since the directory was read
            except Exception as exc:          # one file never takes the listing (and the consent page) down
                rows.append({"id": rid, "namespace": "", "signers": [],
                             "sha256": _stat_version(sign_dir(self.queue_dir) / f"{rid}.json"),
                             "refused": f"not a sign request: it cannot be read ({type(exc).__name__})"})
                continue
            if request.refused is not None:
                rows.append({"id": rid, "namespace": request.namespace, "refused": request.refused,
                             "sha256": request.sha256, "signers": []})
                continue
            row = {"id": rid, "title": request.title, "message": request.message, "namespace": request.namespace,
                   "requester": request.requester, "expires_at": request.expires_at.isoformat(),
                   "sha256": request.sha256, "signers": signers}
            if request.options:     # the exact line each option's signature appends, so the page shows what is signed
                row["options"] = [{"key": k, "text": t, "line": choice_line(k, t)} for k, t in request.options]
                if request.recommended is not None:
                    row["recommended"] = request.recommended      # marked on the page, never selected there
                if request.write_in:   # the line a write-in's signature appends, the words in place of the empty end
                    row["write_in"] = {"key": WRITE_IN_KEY, "line": choice_line(WRITE_IN_KEY, ""), "max": _TEXT_MAX}
            rows.append(row)
        listed = {r["id"] for r in rows if not r.get("refused")}
        with self._lock:
            for rid in [rid for rid in self._challenged if rid not in listed]:
                del self._challenged[rid]
        return {"rows": rows, "overflow": len(names) - read + (1 if more else 0)}

    def challenge(self, request_id: str, req: Any) -> dict[str, Any]:
        """``{signer_fp, request_sha256}`` (and for a choice request ``choice``, the option's key, or
        :data:`WRITE_IN_KEY` with ``write_in``, the operator's words, when the request offers a write-in) -> the
        WebAuthn challenge for the bytes signed (:func:`signed_bytes`), bound to the file as it is now and to that
        option (and those words). A choice request's reply also carries ``line``, the exact line the signature
        appends, cut from those bytes, so the page shows what is signed rather than its own copy of it.
        ``request_sha256`` is the listing's ``sha256`` for the request the page showed: a file that changed since it
        was listed is refused (409), so the operator never signs bytes the page did not show."""
        from levain.consent.passkey import challenge

        if not isinstance(req, dict) or "signer_fp" not in req or not set(req) <= {"signer_fp", "request_sha256",
                                                                                   "choice", "write_in"}:
            raise SignRefused("request keys must be ['request_sha256', 'signer_fp'] and, for a choice request, "
                              "'choice' (with 'write_in' for the write-in choice)", 400)
        request = self._open(request_id)
        message = signed_bytes(request, req.get("choice"), req.get("write_in"))
        if not req.get("request_sha256"):
            raise SignRefused(f"the challenge for sign request {request_id} names no listed version: reload and "
                              "read it again", 409)
        if req["request_sha256"] != request.sha256:
            raise SignRefused(f"sign request {request_id} changed since it was shown: reload and read it again; "
                              "nothing was signed", 409)
        fp = req["signer_fp"]
        if not isinstance(fp, str) or fp not in self._keys():
            raise SignRefused(f"{str(fp)[:80]} is not an active passkey enrolled for {self.rp_id}")
        with self._lock:
            self._challenged[request_id] = (request.sha256, fp, req.get("choice"), req.get("write_in"))
        reply = {"challenge": _b64u(challenge(message, request.namespace)), "rp_id": self.rp_id}
        if request.options:   # the bytes are the message, a newline, then the choice line (signed_bytes)
            reply["line"] = message[len(request.message.encode("utf-8")) + 1:].decode("utf-8")
        return reply

    def answer(self, request_id: str, req: Any) -> dict[str, Any]:
        """``{authenticator_data, client_data_json, signature}`` (base64url) -> the signature verified and recorded:
        the ``.sig`` (over an orphan one), then ``.signed.json`` last, never over one already there. The key read
        and the verify run outside the lock; the request is read again under it just before anything is written,
        and one that changed, expired or was answered meanwhile is refused (409)."""
        from levain.authority import VerifierUnavailable, verify_link_sig
        from levain.cockpit.consent import utc_stamp
        from levain.consent.passkey import PasskeyError, answer_signature_problem, b64url_decode, to_sshsig

        need = {"authenticator_data", "client_data_json", "signature"}
        if not isinstance(req, dict) or set(req) != need:
            raise SignRefused(f"request keys must be {sorted(need)}", 400)
        with self._lock:
            bound = self._challenged.get(request_id)
        if bound is None:
            raise SignRefused(f"no challenge is open for sign request {request_id}: ask for one first", 409)
        changed = f"sign request {request_id} changed after its challenge: nothing was signed"
        request = self._open(request_id)
        if request.sha256 != bound[0]:
            with self._lock:
                if self._challenged.get(request_id) == bound:
                    del self._challenged[request_id]
            raise SignRefused(changed, 409)
        key = self._keys().get(bound[1])
        if key is None:
            raise SignRefused(f"{bound[1]} is no longer an active passkey enrolled for {self.rp_id}")
        message = signed_bytes(request, bound[2], bound[3])
        page_origin = self.page_origin
        try:
            signature = to_sshsig(key.public_key, b64url_decode(req["authenticator_data"], "authenticator_data"),
                                  b64url_decode(req["client_data_json"], "client_data_json"),
                                  b64url_decode(req["signature"], "signature"), namespace=request.namespace,
                                  page_origin=page_origin)
        except PasskeyError as exc:
            raise SignRefused(str(exc)) from None
        problem = answer_signature_problem(signature, key.public_key, page_origin)
        if problem is not None:
            raise SignRefused(problem)
        try:
            ok = verify_link_sig(message, signature, public_key=key.public_key, fingerprint=key.fingerprint,
                                 namespace=request.namespace)
        except VerifierUnavailable as exc:
            raise SignRefused(f"the signature could not be verified now: {exc}", 503) from None
        if not ok:
            raise SignRefused("the passkey's signature does not verify over this message")
        with self._lock, _queue_lock(self.queue_dir):
            # what was verified is what is still there: the same challenge, the same bytes, open and unexpired now,
            # with every other Signer on the queue held off until both files are written (a .sig always belongs to
            # the .signed.json beside it)
            if self._challenged.get(request_id) != bound:
                raise SignRefused(f"sign request {request_id} was answered or re-challenged meanwhile: nothing was "
                                  "signed", 409)
            now = self._open(request_id)
            if now.sha256 != bound[0] or now.namespace != request.namespace:
                del self._challenged[request_id]
                raise SignRefused(changed, 409)
            d, signed_at = sign_dir(self.queue_dir), utc_stamp(_now())
            record = {"signer_fp": key.fingerprint, "public_key": key.public_key, "namespace": request.namespace,
                      "message_sha256": hashlib.sha256(message).hexdigest(), "signed_at": signed_at}
            if bound[2] is not None:
                record.update(choice=bound[2], signed_message=message.decode("utf-8"))
            _publish(_write_over, d / f"{request_id}.sig", signature, "")   # over an orphan: never a 409
            _publish(_write_new, d / f"{request_id}.signed.json", json.dumps(record) + "\n",
                     f"sign request {request_id} is already signed")
            del self._challenged[request_id]
        done = {"id": request_id, "signer_fp": key.fingerprint, "namespace": request.namespace, "signed_at": signed_at}
        return done if bound[2] is None else {**done, "choice": bound[2]}

    def deny(self, request_id: str, req: Any) -> dict[str, Any]:
        """``{request_sha256}`` (the listing's ``sha256`` for the row the page showed) -> ``<id>.denied``, which
        records it. A request changed since it was shown is refused (409). A refused request may be denied too
        (that is how the operator clears it)."""
        from levain.cockpit.consent import utc_stamp

        if not isinstance(req, dict) or set(req) != {"request_sha256"} or not isinstance(req["request_sha256"], str):
            raise SignRefused("request keys must be ['request_sha256']", 400)
        if not self._ours(read_request(self.queue_dir, request_id)):
            raise SignRefused("no such sign request", 404)
        with self._lock, _queue_lock(self.queue_dir):      # what any Signer on the queue decided last is seen here
            request = read_request(self.queue_dir, request_id)
            if not self._ours(request):
                raise SignRefused("no such sign request", 404)
            state = state_of(self.queue_dir, request_id)
            if state != "open":
                raise SignRefused(f"sign request {request_id} is already {state}", 409)
            if not req["request_sha256"] or req["request_sha256"] != request.sha256:
                raise SignRefused(f"sign request {request_id} changed since it was shown: reload and read it again; "
                                  "nothing was denied", 409)
            at = utc_stamp(_now())
            _publish(_write_new, sign_dir(self.queue_dir) / f"{request_id}.denied",
                     json.dumps({"at": at, "request_sha256": request.sha256}) + "\n",
                     f"sign request {request_id} is already denied")
            self._challenged.pop(request_id, None)
        return {"id": request_id, "denied_at": at}


def _b64u(data: bytes) -> str:
    import base64

    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")

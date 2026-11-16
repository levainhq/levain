"""Host grants (board levain#66, design ASK2_HOST_GRANTS_DESIGN.md, slice S1): the PURE format.

One unlisted host (port 443) allowed to one egress-slot session, for this session or 24 hours within it, by one
passkey tap. Three things live here and nowhere else:

  - the signed message (``levain-egress-host/1``): :func:`build_message` writes it, :func:`parse_signed` reads back
    the bytes a signature covers (the message, a newline and ONE levain choice line) and refuses, by a fixed
    sentence, everything out of shape. The grammar's shape fence is ``LEVAIN_SIGN_REQUESTS`` in
    :mod:`levain.contracts.namespaces`; this parser re-validates every field, the bounds and the host grammar.
  - the shipped lists: :func:`never_grant` (hosts no tap can allow) and :func:`writable_host` (hosts that accept
    uploads: a words-and-recommended-key change on the card, never an allow-list).
  - the ``grants.json`` file root writes beside a session's front policy and the front reads at every refused
    CONNECT: :func:`dump_grants` / :func:`parse_grants` (one canonical format, exactly its keys) and
    :func:`read_grants_file` (a bounded, owner- and mode-checked read that returns no grants for anything wrong).

Root's ``/usr/bin/python3`` (3.9) imports this module: stdlib and :mod:`levain.contracts.namespaces` and
:mod:`levain.broker.policy` only, no match statement, no ``X | Y`` outside annotations.

Every refusal is a :class:`GrantRefused` whose text is a FIXED sentence chosen by its name. Nothing a caller supplied
(a host, a field, a byte of a signed message) is ever put in one: the message crosses a trust boundary and an echo
would carry an attacker's text onto a card or a log line.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import stat as _stat
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from levain.broker.policy import is_host_name
from levain.contracts.namespaces import EGRESS_HOST_NAMESPACE, LEVAIN_SIGN_CHOICES, LEVAIN_SIGN_REQUESTS

NAMESPACE = EGRESS_HOST_NAMESPACE
FORMAT_LINE = "levain-egress-host/1"
GRANTS_FORMAT = "levain-egress-grants/1"
PORT = 443
INDEX_SPACE = 64                             # levain.slot.grammar.INDEX_SPACE: a slot index is 0..63 (that module is
                                             # not imported: it needs more). Not MAX_SLOTS: indices pass 31 after retires
SESSION_TTL_MAX_S = 8 * 3600                 # the one-tap "this session" grant: at most issued + 8 h
DAY_TTL_S = 24 * 3600                        # the "24 hours" grant: at most issued + 24 h (still this session only)
FRESH_S = 600                                # a signed message is delivered within 10 min of its issue
SKEW_S = 300                                 # and no more than 5 min in root's future
SIGNED_MAX = 4096                            # the bytes of a signed message and choice line
GRANTS_MAX = 16                              # entries in one grants.json
GRANTS_FILE_MAX = 16 * 1024                  # bytes of one grants.json
CHOICE_SESSION = "s"
CHOICE_DAY = "d"
CHOICES = (CHOICE_SESSION, CHOICE_DAY)

# the choice line consent.sign.choice_line writes (CHOICE_FORMAT + " chosen "); copied, because levain.consent.sign
# is not importable by root's interpreter. tests/test_egress_grants_format.py pins both against sign.py.
CHOICE_MARK = "levain-sign-choice/1 chosen "
_CHOICE_LINE = re.compile(re.escape(CHOICE_MARK) + r"\(([a-z0-9]{1,8}|\*)\): ([^\n]+)")
_MESSAGE = re.compile(LEVAIN_SIGN_REQUESTS[NAMESPACE])
_GRANT_ID = re.compile(r"eh-[0-9a-f]{24}")
_SESSION = re.compile(r"s-[0-9a-f]{16}")
_STAMP = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{6}Z")
_STAMP_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"
_FIELDS = ("store", "grant", "session", "slot", "host", "port", "issued", "expires-session", "expires-day", "rp",
           "writable")

#: every refusal, by name: the ONLY text a :class:`GrantRefused` carries.
REASONS: Dict[str, str] = {
    "shape": "the host grant is not in the one shape levain files",
    "choice": "the host grant's choice is not one of the two levain offers, or is not signed with its own words",
    "host": "the host is not a plain lowercase host name on port 443",
    "never-grant": "that host is never granted, whatever is signed",
    "stamp": "a time in the host grant is not a valid UTC time",
    "bound": "the host grant's times are outside what one tap may allow",
    "writable": "the host grant's upload flag is not what levain's list says",
    "fresh": "the host grant was not issued recently enough or is dated in the future",
    "grants-file": "the host grants file is not in the one format root writes",
    "too-many": "a session holds no more host grants than this",
    "build": "levain cannot file that host grant",
}


class GrantRefused(Exception):
    """A refusal by name. ``name`` is a key of :data:`REASONS`; the text is that entry, never anything supplied."""

    def __init__(self, name: str) -> None:
        self.name = name if name in REASONS else "shape"
        super().__init__(REASONS[self.name])


# --- the shipped lists -------------------------------------------------------------------------------------------------
#: a host equal to or under one of these is never granted (design 2.3: ``localhost``, ``*.local``, ``*.internal``,
#: ``metadata.*``, plus the other names that are never the public internet). The front's address check still runs.
NEVER_GRANT_SUFFIXES = ("localhost", "local", "internal", "localdomain", "home.arpa")
NEVER_GRANT_FIRST_LABELS = ("metadata",)

#: a host equal to or under one of these accepts uploads (code hosting, paste, file storage, request bins), so its
#: card says so and recommends No. Words only: never an allow-list. S3 in any region is :func:`_is_s3`.
WRITABLE_SUFFIXES = ("github.com", "gist.github.com", "gitlab.com", "bitbucket.org", "pastebin.com",
                     "storage.googleapis.com", "drive.google.com", "dropbox.com", "transfer.sh", "webhook.site",
                     "requestbin.com", "requestbin.net", "requestcatcher.com", "pipedream.net", "hookbin.com",
                     "beeceptor.com")


def _under(host: str, suffixes: Iterable[str]) -> bool:
    return any(host == s or host.endswith("." + s) for s in suffixes)


def never_grant(host: object) -> bool:
    """True for a host no tap may allow (and for anything that is not a string: the safe answer)."""
    if not isinstance(host, str) or not host:
        return True
    return _under(host, NEVER_GRANT_SUFFIXES) or host.split(".", 1)[0] in NEVER_GRANT_FIRST_LABELS


def _is_s3(host: str) -> bool:
    return host.endswith(".amazonaws.com") and any(
        label == "s3" or label.startswith("s3-") for label in host.split("."))


def writable_host(host: object) -> bool:
    """True for a host on the writable list. Only the words on the card and the recommended key depend on it."""
    return isinstance(host, str) and (_under(host, WRITABLE_SUFFIXES) or _is_s3(host))


# --- stamps ------------------------------------------------------------------------------------------------------------
def _aware(when: object) -> bool:
    return isinstance(when, datetime) and when.tzinfo is not None and when.utcoffset() is not None


def stamp(when: datetime) -> str:
    """``when`` (an aware datetime) as the one fixed-width UTC stamp the message carries; :class:`GrantRefused`
    ``stamp`` for anything else, a time whose UTC form is past the calendar, or one that would not read back."""
    if not _aware(when):
        raise GrantRefused("stamp")
    try:
        text = when.astimezone(timezone.utc).strftime(_STAMP_FORMAT)
    except (OverflowError, ValueError):
        raise GrantRefused("stamp") from None
    if _STAMP.fullmatch(text) is None:          # a year below 1000 is not zero-padded by every platform's strftime
        raise GrantRefused("stamp")
    return text


def parse_stamp(text: object) -> datetime:
    """The aware UTC datetime a :func:`stamp` string names; :class:`GrantRefused` ``stamp`` for anything else."""
    if not isinstance(text, str) or _STAMP.fullmatch(text) is None:
        raise GrantRefused("stamp")
    try:
        return datetime.strptime(text, _STAMP_FORMAT).replace(tzinfo=timezone.utc)
    except ValueError:
        raise GrantRefused("stamp") from None


# --- the signed message ------------------------------------------------------------------------------------------------
def new_grant_id() -> str:
    """A levain-minted, single-use grant id."""
    return "eh-" + secrets.token_hex(12)


def option_text(key: str) -> str:
    """The one text levain files for choice ``key`` (``s`` or ``d``)."""
    return dict(LEVAIN_SIGN_CHOICES[NAMESPACE])[key]


@dataclass(frozen=True)
class HostGrant:
    """A signed host grant as read back: the message's fields and the choice that was signed."""
    store: str
    grant: str
    session: str
    slot: int
    host: str
    port: int
    issued: datetime
    expires_session: datetime
    expires_day: datetime
    rp: str
    writable: bool
    choice: str

    @property
    def expires(self) -> datetime:
        """When the CHOSEN grant ends: ``expires-session`` for ``s``, ``expires-day`` for ``d``."""
        return self.expires_session if self.choice == CHOICE_SESSION else self.expires_day


def _fields(message: str) -> Dict[str, str]:
    out = {}  # type: Dict[str, str]
    for line in message.split("\n")[1:-1]:
        key, _, value = line.partition(": ")
        out[key] = value
    return out


def _check(fields: Dict[str, str]) -> Tuple[int, datetime, datetime, datetime]:
    """The slot and the three times of a message whose shape already matched, after every semantic rule."""
    if tuple(fields) != _FIELDS:
        raise GrantRefused("shape")
    slot = int(fields["slot"])
    host = fields["host"]
    if not 0 <= slot < INDEX_SPACE:
        raise GrantRefused("shape")
    if fields["port"] != str(PORT) or not is_host_name(host):
        raise GrantRefused("host")
    if never_grant(host):
        raise GrantRefused("never-grant")
    issued = parse_stamp(fields["issued"])
    exp_s = parse_stamp(fields["expires-session"])
    exp_d = parse_stamp(fields["expires-day"])
    try:
        inside = (issued < exp_s <= issued + timedelta(seconds=SESSION_TTL_MAX_S)
                  and exp_s <= exp_d <= issued + timedelta(seconds=DAY_TTL_S))
    except OverflowError:               # a stamp in year 9999: its bound is past the calendar
        inside = False
    if not inside:
        raise GrantRefused("bound")
    if fields["writable"] != ("yes" if writable_host(host) else "no"):
        raise GrantRefused("writable")
    return slot, issued, exp_s, exp_d


def build_message(store: str, grant: str, session: str, slot: int, host: str, issued: datetime,
                  session_deadline: datetime, rp: str) -> str:
    """The message levain files: ``expires-session`` is the earlier of ``session_deadline`` and ``issued`` + 8 h,
    ``expires-day`` is ``issued`` + 24 h, ``writable`` is levain's own lookup. :class:`GrantRefused` ``build`` for a
    field out of shape or a host that is never granted (levain files no card for one), with nothing echoed."""
    try:
        exp_s = min(session_deadline, issued + timedelta(seconds=SESSION_TTL_MAX_S))
        exp_d = issued + timedelta(seconds=DAY_TTL_S)
        if (not isinstance(slot, int) or isinstance(slot, bool) or not isinstance(host, str) or never_grant(host)
                or not isinstance(session, str) or _SESSION.fullmatch(session) is None
                or not isinstance(grant, str) or _GRANT_ID.fullmatch(grant) is None):
            raise GrantRefused("build")
        message = "".join((
            FORMAT_LINE, "\n", "store: ", str(store), "\n", "grant: ", grant, "\n", "session: ", session, "\n",
            "slot: ", str(slot), "\n", "host: ", host, "\n", "port: ", str(PORT), "\n", "issued: ", stamp(issued),
            "\n", "expires-session: ", stamp(exp_s), "\n", "expires-day: ", stamp(exp_d), "\n", "rp: ", str(rp), "\n",
            "writable: ", "yes" if writable_host(host) else "no", "\n"))
        if _MESSAGE.fullmatch(message) is None:
            raise GrantRefused("build")
        _check(_fields(message))
    except GrantRefused:
        raise GrantRefused("build") from None
    except (TypeError, ValueError, OverflowError):
        raise GrantRefused("build") from None
    return message


def signed_bytes(message: str, choice: str) -> bytes:
    """The bytes a signature for ``choice`` covers, as ``levain.consent.sign.signed_bytes`` builds them for this
    namespace's request: the message, a newline and the choice line for that key."""
    if choice not in CHOICES:
        raise GrantRefused("choice")
    return (message + "\n" + CHOICE_MARK + "(" + choice + "): " + option_text(choice)).encode("utf-8")


def parse_signed(signed: object) -> HostGrant:
    """The :class:`HostGrant` the verified bytes ``signed`` carry, or :class:`GrantRefused`. The bytes are exactly a
    message in this grammar, a newline and one choice line whose key is ``s`` or ``d`` and whose text is levain's
    fixed text FOR THAT KEY: a signature for ``s`` is never read as ``d``, and a key with the other key's words, an
    unknown key, a write-in or a second choice line is refused. Freshness against root's clock is
    :func:`check_fresh`; the signature, the single-use ledger and the brokered-host check are root's."""
    if not isinstance(signed, (bytes, bytearray)) or not 0 < len(signed) <= SIGNED_MAX:
        raise GrantRefused("shape")
    try:
        text = bytes(signed).decode("utf-8")
    except UnicodeDecodeError:
        raise GrantRefused("shape") from None
    message, sep, last = text.rpartition("\n")
    choice_line = _CHOICE_LINE.fullmatch(last)
    if not sep or choice_line is None:
        raise GrantRefused("choice")
    key, words = choice_line.group(1), choice_line.group(2)
    if key not in CHOICES or words != option_text(key):
        raise GrantRefused("choice")
    if _MESSAGE.fullmatch(message) is None:
        raise GrantRefused("shape")
    fields = _fields(message)
    slot, issued, exp_s, exp_d = _check(fields)
    return HostGrant(store=fields["store"], grant=fields["grant"], session=fields["session"], slot=slot,
                     host=fields["host"], port=PORT, issued=issued, expires_session=exp_s, expires_day=exp_d,
                     rp=fields["rp"], writable=fields["writable"] == "yes", choice=key)


def check_fresh(grant: HostGrant, now: datetime) -> None:
    """:class:`GrantRefused` ``fresh`` unless ``grant`` was issued no more than :data:`FRESH_S` before ``now`` and
    no more than :data:`SKEW_S` after it (root's clock, never the message's). A ``now`` that is not an aware datetime
    is refused the same way (the message's stamps are always aware UTC, so only a caller's naive clock could differ)."""
    if not _aware(now):
        raise GrantRefused("fresh")
    age = (now - grant.issued).total_seconds()
    if not -SKEW_S <= age <= FRESH_S:
        raise GrantRefused("fresh")


# --- grants.json -------------------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class Entry:
    """One live grant in a session's ``grants.json``."""
    grant: str
    host: str
    port: int
    expires: datetime
    choice: str


@dataclass(frozen=True)
class GrantsFile:
    session: str
    slot: int
    entries: Tuple[Entry, ...]


_FILE_KEYS = frozenset({"format", "session", "slot", "grants"})
_ENTRY_KEYS = frozenset({"grant", "host", "port", "expires", "choice"})


def _int(v: object) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def entry_from_grant(grant: HostGrant) -> Entry:
    """The entry root writes for a verified :class:`HostGrant`."""
    return Entry(grant=grant.grant, host=grant.host, port=grant.port, expires=grant.expires, choice=grant.choice)


def dump_grants(session: str, slot: int, entries: Sequence[Entry]) -> bytes:
    """The canonical bytes of a grants file (sorted keys, no spaces, one trailing newline). Refuses, with the same
    rules :func:`parse_grants` applies, whatever that reader would not accept: always :class:`GrantRefused`
    ``grants-file``, never another exception, for any argument out of shape."""
    try:
        if not isinstance(entries, (list, tuple)) or not all(isinstance(e, Entry) for e in entries):
            raise ValueError
        body = {"format": GRANTS_FORMAT, "session": session, "slot": slot,
                "grants": [{"grant": e.grant, "host": e.host, "port": e.port, "expires": stamp(e.expires),
                            "choice": e.choice} for e in entries]}
        raw = (json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=True) + "\n").encode("ascii")
        parse_grants(raw)
    except (GrantRefused, TypeError, ValueError, OverflowError, RecursionError):
        raise GrantRefused("grants-file") from None
    return raw


def _no_duplicates(pairs: List[Tuple[str, object]]) -> Dict[str, object]:
    out = {}  # type: Dict[str, object]
    for k, v in pairs:
        if k in out:
            raise ValueError("duplicate key")
        out[k] = v
    return out


def _no_constant(_name: str) -> object:
    raise ValueError("not JSON")


def parse_grants(raw: object) -> GrantsFile:
    """The :class:`GrantsFile` ``raw`` holds, or :class:`GrantRefused` ``grants-file``: UTF-8 JSON of at most
    :data:`GRANTS_FILE_MAX` bytes, exactly the keys of this format, no duplicate key, no NaN, a session and slot in
    their grammar, at most :data:`GRANTS_MAX` entries each with exactly its keys, a host that passes
    :func:`levain.broker.policy.is_host_name` and is not never-granted, port 443, a valid stamp, choice ``s`` or
    ``d``, and no grant id or host named twice."""
    try:
        if not isinstance(raw, (bytes, bytearray)) or not 0 < len(raw) <= GRANTS_FILE_MAX:
            raise ValueError
        data = json.loads(bytes(raw).decode("utf-8"), object_pairs_hook=_no_duplicates, parse_constant=_no_constant)
        if (not isinstance(data, dict) or set(data) != _FILE_KEYS or data["format"] != GRANTS_FORMAT
                or not isinstance(data["session"], str) or _SESSION.fullmatch(data["session"]) is None
                or not _int(data["slot"]) or not 0 <= data["slot"] < INDEX_SPACE
                or not isinstance(data["grants"], list) or len(data["grants"]) > GRANTS_MAX):
            raise ValueError
        entries = []  # type: List[Entry]
        for e in data["grants"]:
            if (not isinstance(e, dict) or set(e) != _ENTRY_KEYS or not isinstance(e["grant"], str)
                    or _GRANT_ID.fullmatch(e["grant"]) is None or not isinstance(e["host"], str)
                    or not is_host_name(e["host"]) or never_grant(e["host"]) or not _int(e["port"])
                    or e["port"] != PORT or e["choice"] not in CHOICES):
                raise ValueError
            entries.append(Entry(grant=e["grant"], host=e["host"], port=e["port"], expires=parse_stamp(e["expires"]),
                                 choice=e["choice"]))
        if (len({e.grant for e in entries}) != len(entries) or len({e.host for e in entries}) != len(entries)):
            raise ValueError
        return GrantsFile(session=data["session"], slot=data["slot"], entries=tuple(entries))
    except (ValueError, RecursionError, GrantRefused, UnicodeDecodeError):
        raise GrantRefused("grants-file") from None


def read_grants_file(path: str, *, owner_uid: int = 0, group_gid: Optional[int] = None) -> Optional[GrantsFile]:
    """The :class:`GrantsFile` at ``path``, or None for anything wrong with it: missing, a symlink or not a regular
    file, owned by another uid than ``owner_uid`` (root's, by default), writable by its group or readable or writable by
    others, owned by
    another group than ``group_gid`` when one is given, over :data:`GRANTS_FILE_MAX`, or not in the one format. A
    file that does not read is no grants, never an error: the front reads this at every refused CONNECT and one bad
    file must not close it. It is read from the one descriptor it was checked on (never its name twice)."""
    fd = -1
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
        st = os.fstat(fd)
        if (not _stat.S_ISREG(st.st_mode) or st.st_uid != owner_uid or st.st_mode & 0o027
                or (group_gid is not None and st.st_gid != group_gid) or st.st_size > GRANTS_FILE_MAX):
            return None
        chunks = []  # type: List[bytes]
        size = 0
        while size <= GRANTS_FILE_MAX:                 # to the end: one read may return less than the file holds
            chunk = os.read(fd, GRANTS_FILE_MAX + 1 - size)
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
        return parse_grants(b"".join(chunks))
    except (OSError, GrantRefused):
        return None
    finally:
        if fd >= 0:
            os.close(fd)


def live_entry(grants: Optional[GrantsFile], session: str, slot: int, host: str, port: int,
               now: datetime) -> Optional[Entry]:
    """The entry that admits a CONNECT to ``host``:``port`` for ``session`` in ``slot`` at ``now``, or None: the file
    names this session and this slot (both are in the signed message, so both bind at read), the entry names exactly
    this host and port 443, and it ends after ``now``. Nothing else admits, and a ``now`` that is not an aware
    datetime admits nothing."""
    if (grants is None or grants.session != session or not _int(slot) or grants.slot != slot or port != PORT
            or not isinstance(host, str) or not _aware(now)):
        return None
    for e in grants.entries:
        if e.host == host and e.port == port and e.expires > now:
            return e
    return None

"""The cockpit's Desk view (levain#2 lane B): the two routes behind it, as JSON. The port is landed code this module
only calls (``levain.desk.desk_port``); no authority, fence or delivery rule lives here.

* ``GET /cockpit/desk.json[?thread=<id>&after=<cursor>]``: what the port says it can do, who holds the lease, and, when
  a thread is named, the receipts after the cursor. Not wired: ``{"wired": false}``. Every string in a wired answer is
  the HOST'S text (a reference, a reason, a receipt): the page draws it as text only.
* ``POST /cockpit/desk/send {words, outbox_id}``: one line filed through the port. The owner is the CREDENTIAL'S
  (``cockpit:<name>`` or ``phone:<id>``, derived by the mount), never anything the body says. A refusal writes nothing:
  no credential 403, a port with no post command 404 (and its read says not wired), a bad body (an unsendable character
  included, whatever the length) 400, a stored ``outbox_id`` with other words 409, words over the cap 422 (refused,
  never cut). The port's own result (``accepted`` / ``ambiguous`` / ``not_delivered``, ``replayed``) is a 200: a retry
  of the same ``outbox_id`` is answered from the port's stored claim and never run twice.

Nothing here claims more than the port does. A send is "filed" at best: this module cannot observe that a line was
fenced, taken or acted on, and says none of it."""
from __future__ import annotations

import dataclasses
import json
import urllib.parse
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from levain.desk import chat
from levain.desk.chat_lease import check_owner
from levain.desk.desk_port import (DESKCMD_ENV, DeskCommands, DeskPort, DeskPortError, HostCommandDeskPort, OutboxConflict,
                                   check_words)
from levain.desk.store import CoordError

DESK_PATH = "/cockpit/desk.json"
SEND_PATH = "/cockpit/desk/send"
CLAIMS_DIR = "desk-port"             # under the consent queue's directory, beside the room's ``room-run``
CLAIMS_NAME = "claims.db"
_QUERY_KEYS = ("thread", "after")
_QUERY_VALUE_MAX = 200


class DeskQueryError(ValueError):
    """A read's query string the route will not take (a repeated, unknown or oversized parameter)."""


def is_desk_path(path: str) -> bool:
    return path == DESK_PATH


def is_send_path(path: str) -> bool:
    return path == SEND_PATH


def _refusal(error: str, message: str, status: int, **extra: Any) -> tuple[int, dict[str, Any]]:
    return status, {"error": error, "message": message, **extra}


def encode(body: Mapping[str, Any]) -> bytes:
    return json.dumps(body).encode("utf-8")


# --- the port from the host's environment -------------------------------------------------------------------------------
def claims_path(queue_dir: Path | str) -> Path:
    """Where the port keeps its post claims: a file under the cockpit's own state directory (the consent queue's), so a
    restart keeps every claim and a stored ``outbox_id`` is still answered from the record."""
    return Path(queue_dir) / CLAIMS_DIR / CLAIMS_NAME


def port_from_env(environ: Mapping[str, str], queue_dir: Path | str) -> HostCommandDeskPort | None:
    """The :class:`HostCommandDeskPort` the environment's :data:`DESKCMD_ENV` names, with a DURABLE claims file under
    ``queue_dir``; None when the variable is unset or empty. A malformed value raises
    :class:`levain.desk.desk_port.DeskCommandError` (the serve refuses to start on it: a desk wired wrongly must not
    serve as if it were not wired)."""
    commands = DeskCommands.from_env(environ.get(DESKCMD_ENV))
    if commands is None:
        return None
    path = claims_path(queue_dir)
    # ``mode`` binds only the leaf, so the queue directory itself is made first: a fresh home's ``consent`` is created
    # 0700 here (the dispatch store refuses one at the umask), and one already there is never touched
    Path(queue_dir).mkdir(parents=True, exist_ok=True, mode=0o700)
    path.parent.mkdir(exist_ok=True, mode=0o700)
    return HostCommandDeskPort(commands, path)


# --- the read -----------------------------------------------------------------------------------------------------------
def parse_query(query: str) -> tuple[str | None, str | None]:
    """``(thread, after)`` from a read's query string. Each may appear once; no other parameter is taken; ``after`` names
    a cursor ``thread`` it was read from, so it needs ``thread``. An empty value is the same as absent."""
    values = urllib.parse.parse_qs(query, keep_blank_values=True)
    unknown = sorted(set(values) - set(_QUERY_KEYS))
    if unknown:
        raise DeskQueryError("the desk read takes thread and after, nothing else")
    got: dict[str, str | None] = {}
    for key in _QUERY_KEYS:
        seen = values.get(key, [])
        if len(seen) > 1:
            raise DeskQueryError(f"{key} is given once")
        value = seen[0] if seen else ""
        if len(value) > _QUERY_VALUE_MAX:
            raise DeskQueryError(f"{key} is at most {_QUERY_VALUE_MAX} characters")
        got[key] = value or None
    if got["after"] is not None and got["thread"] is None:
        raise DeskQueryError("after is a cursor of one thread: name that thread")
    return got["thread"], got["after"]


def serve_desk(port: DeskPort | None, query: str = "") -> tuple[int, dict[str, Any]]:
    """The body of ``GET /cockpit/desk.json``. ``port`` None is the unwired answer. The thread and cursor reach the port
    only when a thread is named (``readable`` false means the receipts could not be read, never "no events yet")."""
    try:
        thread, after = parse_query(query)
    except DeskQueryError as exc:
        return _refusal("bad_request", str(exc), 400)
    if port is None or not _can_post(port):
        return 200, {"wired": False}
    try:
        caps: dict[str, Any] | None = dataclasses.asdict(port.capabilities())
    except Exception:       # noqa: BLE001 - caps that cannot be asked: wired, but nothing is readable this poll
        caps = None
    try:
        lease = port.lease()
        lease_view = {"state": lease.state, "holder": lease.holder, "age_s": lease.age_s}
    except Exception:       # noqa: BLE001 - a lease that cannot be asked is not known: say so, never a 500 on a poll
        lease_view = {"state": "unknown", "holder": None, "age_s": None}
    events: list[dict[str, str]] = []
    cursor: str | None = None
    readable = True
    if thread is not None:
        try:
            batch = port.events(thread, after)
            events = [{"by": e.by, "kind": e.kind, "text": e.text, "at": e.at} for e in batch.events]
            cursor, readable = batch.cursor, batch.readable
        except Exception:   # noqa: BLE001 - the same: unreadable, never "none yet"
            events, cursor, readable = [], None, False
    if caps is None:        # the page draws no cap and no slot from {}, and "could not be fetched" from readable false
        return 200, {"wired": True, "caps": {}, "lease": lease_view, "thread_id": thread, "events": [], "cursor": None,
                     "readable": False}
    return 200, {"wired": True, "caps": caps, "lease": lease_view, "thread_id": thread,
                 "events": events, "cursor": cursor, "readable": readable}


# --- the write ----------------------------------------------------------------------------------------------------------
def _can_post(port: DeskPort) -> bool:
    """False for a port that says it has no post command (``can_post`` is False): a desk nothing can be filed with is not
    wired. A port that says nothing of it (the double, a host's own) is taken as able."""
    return getattr(port, "can_post", True) is not False


def _owner(owner: str | None) -> str | None:
    if owner is None:
        return None
    try:
        return check_owner(owner)
    except CoordError:
        return None


def post_desk_send(port: DeskPort, owner: str | None, req: Any) -> tuple[int, dict[str, Any]]:
    """``POST /cockpit/desk/send {words, outbox_id}`` for the authenticated ``owner``. The credential is checked first,
    then the whole local shape of the body, then the cap; the port is called only when every one passes. Fields the
    body carries beyond ``words`` and ``outbox_id`` are ignored: in particular it can name no owner."""
    origin = _owner(owner)
    if origin is None:
        return _refusal("forbidden", "a send needs a named credential", 403)
    if not _can_post(port):
        return _refusal("not_found", "no desk is wired", 404)
    if not isinstance(req, dict):
        return _refusal("bad_request", "body must be a JSON object", 400)
    words, outbox = req.get("words"), req.get("outbox_id")
    if not isinstance(words, str) or not words.strip():
        return _refusal("bad_request", "words is a non-empty string", 400)
    if not isinstance(outbox, str) or chat._OUTBOX_ID.fullmatch(outbox) is None:
        return _refusal("bad_request", "a message carries the client's own outbox_id: 8 to 128 of A-Z a-z 0-9 . _ - "
                        "(starting with a letter or digit)", 400)
    try:
        words_max = port.capabilities().words_max
    except Exception:        # noqa: BLE001 - nothing was posted: say so, never a dropped connection
        return _refusal("send_failed", "The desk port could not say what it takes, so nothing was filed. Sending it again "
                        "with the same outbox_id files it once.", 500)
    # a character the port cannot carry is refused for that, with its own words, whatever the length: only a line that is
    # otherwise whole and over the cap is the 422 (the page draws that one as "over the limit")
    other = check_words(words, max(words_max, len(words)))
    if other is not None:
        return _refusal("bad_request", other, 400)
    if len(words) > words_max:
        # refused whole, never cut: a cut instruction is a different instruction. The port's own words say how far over.
        return _refusal("words_over_cap", check_words(words, words_max) or "That message is over the cap.", 422,
                        words_max=words_max)
    try:
        result = port.post(words, outbox_id=outbox, origin=origin)
    except OutboxConflict as exc:
        return _refusal("outbox_conflict", str(exc), 409)
    except DeskPortError as exc:
        # the id and the words passed above, so what the port refused is the sender's name (a credential whose name is
        # longer than the port records). The credential check above is its own layer: this one never stands in for it.
        return _refusal("bad_request", str(exc), 400)
    except Exception:        # noqa: BLE001 - a port that failed before it could answer: nothing it stored is lost
        return _refusal("send_failed", "The desk port failed before it could answer. The message may or may not have "
                        "been filed; sending it again with the same outbox_id never files it twice.", 500)
    return 200, {"outcome": result.outcome, "ref": result.ref, "thread_id": result.thread_id, "reason": result.reason,
                 "replayed": result.replayed}

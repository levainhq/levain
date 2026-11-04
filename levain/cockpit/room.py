"""The cockpit's one Claude room (RM-F, DESIGN_ROOM sections 3, 4 and 6): the read of the open room and the operator's
routes into it, as JSON. The coordinator half is landed code this module only calls (``levain.coordinator.room``,
``room_launch``, ``chat``); no authority, consent or containment rule lives here.

Reads (``GET /cockpit/room.json``) open ``<directory>/dispatch.db`` ``mode=ro`` and write nothing: the open room thread,
its newest room launch's composer, its stream in ``gseq`` order (the thread view's own item shape), and the drop proposals
its session made, each with the state the RECORD holds (``open`` / ``committed`` / ``dismissed`` / ``expired``). A card's
state comes from ``proposal_events``, never from anything the room said.

Writes act through a :class:`RoomHost` the mount was given, for an authenticated owner (``cockpit:<name>`` or
``phone:<id>``, derived from the credential by the mount, never from a body):

* ``send`` (``send_room``): the line is the operator's, queued, no turn lease, no epoch. A stored ``outbox_id`` is answered
  from the record BEFORE any launch is opened, so a resend after a restart opens nothing. A receipt that moved nothing is
  a 409 named by its state; ``pending`` and ``attempted`` are never a 200; ``accepted`` and ``ambiguous`` are.
* ``open``: the first-need open. With no room open the page shows an Open button and every other control dark; an
  open room is answered as it stands, never opened twice.
* ``stop`` (unfenced, B5), ``mark`` (an ambiguous line, delivered or not), ``rotate`` (the host's own verb, fenced by
  the room the page drew).
* ``drop``: the composer's text, verbatim, as a drop from this surface. No model turn and no note.
* ``proposal/<id>/commit`` and ``/dismiss``: his tap on a card. The drop's words are the bytes the card's field held
  (his adopted quote or his edit), the room's summary only ever rides as the labelled note
  (:func:`levain.coordinator.room.commit`).

Every write names the room THREAD the page drew (``room_thread_id``) where it names a conversation, and a thread that is
not the open room is a 409 ``room_rotated`` with nothing written. A non-CoordError out of a send is answered by reading
the outbox, never by a bare 500 and never by assuming the line was not written."""
from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from levain.cockpit import threads
from levain.cockpit.consent import utc_stamp
from levain.coordinator import chat
from levain.coordinator import room as coordroom
from levain.coordinator.chat_lease import check_owner
from levain.coordinator.room_launch import RoomLaunchError
from levain.coordinator.room_serve import RoomServeError
from levain.coordinator.store import SURFACES, WORDS_MAX, CoordError, DropConflict

ROOM_PATH = "/cockpit/room.json"
ROOM_PREFIX = "/cockpit/room/"
ROOM_ACTIONS = ("send", "stop", "mark", "drop", "rotate", "open")
PROPOSAL_ACTIONS = ("commit", "dismiss")
READ_OWNER = "cockpit:room-read"        # a room composer ignores its owner (no lease); the read still needs a well-formed one
PROPOSALS_MAX = 50
_THREAD_ID = re.compile(r"th-[0-9a-f]{32}")
_MESSAGE_ID = re.compile(r"cm-[0-9a-f]{32}")
_PROPOSAL_ID = re.compile(r"pr-[0-9a-f]{32}")
# a receipt in one of these states moved nothing: the answer is a 409 named by the state, never a success
_UNDELIVERED = ("refused", "cancelled", "not_delivered")


@dataclass
class RoomHost:
    """What the mount hands the room routes. ``service`` is the ChatService and ``store`` the CoordStore over the same
    ``dispatch.db`` (for a drop). ``ensure()`` opens or reopens the room's running launch (``RoomLauncher.ensure``) and
    raises a CoordError when it cannot; None means the launch is kept elsewhere. ``rotate(expected_room)`` makes the next
    room thread the open room when ``expected_room`` (the thread the page drew) still is the open one, and raises
    ``RoomRotated`` when it is not (the check and the act are one step, so two stale taps rotate once); None serves no
    rotation. Its return value is NOT read: the landed ``coordroom.rotate`` returns the OLD thread and another host may
    return the new one, so the route answers with the open room it re-reads.

    A host built from a :class:`~levain.coordinator.room_serve.RoomServe` (:func:`host_from`) also carries ``send``
    (``RoomServe.send``: ordered against recover, the open and the launch, called with ``room_thread=`` the page's),
    ``stop(launch_id)`` (``RoomServe.interrupt``), ``open()`` (the first-need open, ``RoomServe.ensure_room``) and
    ``view()`` (the pump's last error; :func:`host_from` gives ``RoomServe.status``, which takes no lock, so a page poll
    never waits for a send in flight). Without ``send`` a send is ``ensure()`` then ``service.send_room``; without
    ``stop`` a stop is ``service.interrupt``; without ``open`` the route serves none."""
    service: Any
    store: Any
    ensure: Callable[[], Any] | None = None
    rotate: Callable[[str], Any] | None = None
    send: Callable[..., Any] | None = None
    stop: Callable[[str], Any] | None = None
    open: Callable[[], Any] | None = None
    view: Callable[[], Mapping[str, Any]] | None = None


def host_from(rs: Any) -> RoomHost:
    """The host a ``levain serve`` mounts over its :class:`~levain.coordinator.room_serve.RoomServe`: ONE ChatService
    (``rs.service``, the one ``make_server`` reads through), the store, and every verb the serve orders."""
    return RoomHost(rs.service, rs.store, rotate=rs.rotate, send=rs.send, stop=rs.interrupt, open=rs.ensure_room,
                    view=rs.status)


def is_room_path(path: str) -> bool:
    return path == ROOM_PATH


def post_route(path: str) -> tuple[str, str | None] | None:
    """``(action, proposal_id)`` for ``POST /cockpit/room/<action>`` or ``/cockpit/room/proposal/<pr-id>/<commit|dismiss>``,
    else None."""
    if not path.startswith(ROOM_PREFIX):
        return None
    rest = path[len(ROOM_PREFIX):]
    if rest in ROOM_ACTIONS:
        return rest, None
    head, _, tail = rest.partition("/")
    if head != "proposal":
        return None
    pid, sep, action = tail.partition("/")
    if not sep or action not in PROPOSAL_ACTIONS or _PROPOSAL_ID.fullmatch(pid) is None:
        return None
    return action, pid


def _refusal(error: str, message: str, status: int, **extra: Any) -> tuple[int, dict[str, Any]]:
    return status, {"error": error, "message": message, **extra}


# --- reads ------------------------------------------------------------------------------------------------------------

def _open_room(conn: sqlite3.Connection) -> str | None:
    """The open room thread read from a read-only connection (the coordinator's own reader would create its tables)."""
    if not threads._has(conn, "room_events"):
        return None
    row = conn.execute("SELECT room_thread, kind FROM room_events ORDER BY seq DESC LIMIT 1").fetchone()
    return row[0] if row is not None and row[1] == "opened" else None


def _room_launch(conn: sqlite3.Connection, room_thread: str) -> str | None:
    if not threads._has(conn, "launches"):
        return None
    row = conn.execute("SELECT l.launch_id FROM launches l, json_each(l.threads) j WHERE j.value = ? AND l.role = 'room' "
                       "ORDER BY l.gseq DESC LIMIT 1", (room_thread,)).fetchone()
    return None if row is None else row[0]


def _composer(conn: sqlite3.Connection, launch: str | None, service: Any) -> dict[str, Any] | None:
    if launch is None or not (threads._has(conn, "chat_messages") and threads._has(conn, "chat_outbox_events")):
        return None
    try:
        if service is not None:
            c = service.composer(launch, READ_OWNER)
        else:
            c = chat.composer_state(conn, utc_stamp(datetime.now(timezone.utc)), launch, READ_OWNER, attached=False)
    except (sqlite3.OperationalError, CoordError):
        return None
    return {"state": c.state, "why": c.why, "reenable": c.reenable, "unresolved": list(c.unresolved)}


def _proposals(conn: sqlite3.Connection, room_thread: str) -> list[dict[str, Any]]:
    """The open room's drop proposals, each in the state the record holds. Read-only SQL, never the coordinator's
    ``get_proposal`` (it creates a missing room table, which a ``mode=ro`` connection cannot): a store missing a table
    degrades only this part. A committed card carries the words of the DROP it made (his bytes), never the proposal's."""
    if not (threads._has(conn, "drop_proposals") and threads._has(conn, "proposal_events")):
        return []
    has_drops = threads._has(conn, "drops")
    out = []
    rows = conn.execute("SELECT proposal_id, quoted, words, summary, about, at FROM drop_proposals WHERE room_thread = ? "
                        "ORDER BY gseq DESC LIMIT ?", (room_thread, PROPOSALS_MAX)).fetchall()
    for pid, quoted, words, summary, about, at in reversed(rows):
        event = conn.execute("SELECT kind, drop_id FROM proposal_events WHERE proposal_id = ? ORDER BY seq LIMIT 1",
                             (pid,)).fetchone()
        state, drop_id = (event[0], event[1] if event[0] == "committed" else None) if event else ("open", None)
        if drop_id is not None and has_drops:
            made = conn.execute("SELECT words FROM drops WHERE drop_id = ?", (drop_id,)).fetchone()
            if made is not None:
                words = made[0]
        out.append({"proposal_id": pid, "state": state, "quoted": json.loads(quoted), "words": words,
                    "summary": summary, "about": about, "at": at, "drop_id": drop_id})
    return out


def room_read(directory: Path | str, service: Any = None) -> dict[str, Any]:
    """The open room as one body: ``room_thread`` (None when no room is open), ``launch_id`` (its newest room launch),
    ``composer`` (None when it has no launch or the record cannot say), ``model``, ``gap``, ``stream`` and
    ``proposals``. A store missing a table degrades only the part that table feeds."""
    empty: dict[str, Any] = {"room_thread": None, "launch_id": None, "composer": None, "model": None, "gap": False,
                             "stream": [], "proposals": []}
    conn = threads._open(directory)
    if conn is None:
        return empty
    try:
        conn.execute("BEGIN")
        try:
            room_thread = _open_room(conn) if threads._has(conn, "threads") else None
            if room_thread is None:
                return empty
            launch = _room_launch(conn, room_thread)
            try:
                _before, merged = chat.timeline(conn, room_thread)
            except (CoordError, sqlite3.OperationalError):
                merged = []
            launches = {r[0] for r in conn.execute(
                "SELECT l.launch_id FROM launches l, json_each(l.threads) j WHERE j.value = ?",
                (room_thread,))} if threads._has(conn, "launches") else set()
            facts, gap = threads._event_facts(conn, launches)
            done = {(lid, c) for (lid, k), (c, _t, _b) in facts.items() if k.startswith("tool_result:") and c}
            return {"room_thread": room_thread, "launch_id": launch, "composer": _composer(conn, launch, service),
                    "model": threads.model_in_use(service, launch), "gap": gap,
                    "stream": [threads._item(r, facts, done) for r in merged],
                    "proposals": _proposals(conn, room_thread)}
        finally:
            conn.execute("COMMIT")
    finally:
        conn.close()


def serve_room(path: str, *, directory: Path | str, service: Any = None,
               view: Callable[[], Mapping[str, Any]] | None = None) -> tuple[int, dict[str, Any]]:
    """``(status, body)`` for ``GET /cockpit/room.json``: a store that cannot be read is a 500 with a fixed sentence.
    ``view`` (the host's, when a serve holds the room) adds ``room_error``: the pump's or the launch's last error, or
    None, so a room that cannot start says why on the page instead of staying silent."""
    if path != ROOM_PATH:
        return 404, {"error": "not_found", "message": "no such room route"}
    try:
        body = room_read(directory, service)
    except Exception:       # the boundary: whatever broke, the caller gets the fixed sentence and no detail
        return 500, {"error": "internal", "message": "the room record could not be read"}
    if view is not None:
        try:
            err = view().get("last_error")
        except Exception:   # a serve that is stopped or not started has no error to report
            err = None
        body["room_error"] = err if isinstance(err, str) and err else None
    return 200, body


# --- writes -----------------------------------------------------------------------------------------------------------

def _owner_parts(owner: str | None) -> tuple[str, str] | None:
    if owner is None:
        return None
    try:
        surface, _, client = check_owner(owner).partition(":")
    except CoordError:
        return None
    return surface, client


def _need_room(directory: Path | str, req: Mapping[str, Any]) -> tuple[str | None, tuple[int, dict[str, Any]] | None]:
    """``(room_thread, refusal)``: the request's ``room_thread_id`` must be a well-formed thread id and the open room."""
    named = req.get("room_thread_id")
    if not isinstance(named, str) or _THREAD_ID.fullmatch(named) is None:
        return None, _refusal("bad_request", "a request names the room thread the page shows (room_thread_id)", 400)
    conn = threads._open(directory)
    if conn is None:
        return None, _refusal("not_found", "no room is recorded", 404)
    try:
        current = _open_room(conn)
    finally:
        conn.close()
    if current != named:
        return None, _refusal("room_rotated", "the room changed since this page drew it; nothing was done, and the page "
                              "shows the open room once it reads it again", 409)
    return named, None


def _stored_outbox(directory: Path | str, outbox_id: str) -> bool:
    conn = threads._open(directory)
    if conn is None:
        return False
    try:
        return threads._has(conn, "chat_messages") and conn.execute(
            "SELECT 1 FROM chat_messages WHERE outbox_id = ?", (outbox_id,)).fetchone() is not None
    finally:
        conn.close()


def _answer(got: Any) -> tuple[int, dict[str, Any]]:
    receipt = {"message_id": got.message_id, "outbox_id": got.outbox_id, "state": got.state, "cause": got.cause,
               "created": got.created}
    if got.state in _UNDELIVERED:
        return _refusal(got.state, got.cause or "the line was not delivered", 409, receipt=receipt)
    if got.state == "pending":
        # stored, never attempted (a crash before the attempt): recover() turns it into not_delivered; until then it is
        # not a success (L3_chatstack_r1 codex-3)
        return _refusal("undelivered", "the line is stored but was not attempted; it is not a sent line", 409,
                        receipt=receipt)
    if got.state == "attempted":
        return _refusal("unresolved", "the line was attempted and its outcome is not settled; it is not sent again. "
                        "Read the room again before sending it as a new line", 202, receipt=receipt)
    return 200, {"receipt": receipt}


def _check_send(req: Any) -> tuple[int, dict[str, Any]] | None:
    """The whole local shape of a send, checked before anything is read or written."""
    if not isinstance(req, dict):
        return _refusal("bad_request", "body must be a JSON object", 400)
    text, outbox = req.get("body"), req.get("outbox_id")
    if not isinstance(text, str) or not text.strip():
        return _refusal("bad_request", "a message is a non-empty string", 400)
    if not isinstance(outbox, str) or chat._OUTBOX_ID.fullmatch(outbox) is None:
        return _refusal("bad_request", "a message carries the client's own outbox_id: 8 to 128 of A-Z a-z 0-9 . _ - "
                        "(starting with a letter or digit)", 400)
    try:
        chat._text("a message", text, chat.WORDS_MAX)
    except CoordError as exc:
        return _refusal("bad_request", str(exc), 400)
    if any(k in req for k in ("mode", "expected_turn_id", "expected_epoch", "expected_launch_id")):
        return _refusal("bad_request", "a room line is queued: it names no mode, turn, epoch or launch", 400)
    return None


def _settled(directory: Path | str, outbox_id: str) -> tuple[int, dict[str, Any]]:
    """After a non-CoordError out of a send: what the record says the line's state is (it may have been written)."""
    conn = threads._open(directory)
    state = None
    if conn is not None:
        try:
            if threads._has(conn, "chat_messages") and threads._has(conn, "chat_outbox_events"):
                row = conn.execute(
                    "SELECT o.state FROM chat_messages m JOIN chat_outbox_events o ON o.message_id = m.message_id "
                    "WHERE m.outbox_id = ? ORDER BY o.seq DESC LIMIT 1", (outbox_id,)).fetchone()
                state = row[0] if row else None
        finally:
            conn.close()
    if state is None:
        return _refusal("send_failed", "the line was not written; send it again", 500)
    if state in _UNDELIVERED:       # the same state answers 409 on the replay path (`_answer`): one answer, not two
        return _refusal(state, f"the line is stored ({state.replace('_', ' ')}) and was not delivered", 409)
    return _refusal("unresolved", f"the line is stored ({state.replace('_', ' ')}) but the send did not finish; it is "
                    "not sent again", 202)


def post_room_send(host: RoomHost, directory: Path | str, owner: str | None, req: Any) -> tuple[int, dict[str, Any]]:
    """``POST /cockpit/room/send {room_thread_id, body, outbox_id}``."""
    bad = _check_send(req)
    if bad is not None:
        return bad
    who = _owner_parts(owner)
    if who is None:
        return _refusal("forbidden", "a send needs a named credential", 403)
    room_thread, refused = _need_room(directory, req)
    if refused is not None:
        # a stored outbox_id is still answered from the record: a resend after a rotation sends nothing again
        if refused[1].get("error") != "room_rotated" or not _stored_outbox(directory, req["outbox_id"]):
            return refused
        room_thread = req["room_thread_id"]
    kw = dict(author_surface=who[0], client_id=who[1], outbox_id=req["outbox_id"], body=req["body"])
    try:
        if host.send is not None:
            # the serve orders recover, the open and the line itself (and answers a stored outbox_id before opening)
            return _answer(host.send(room_thread=room_thread, **kw))
        if host.ensure is not None and not _stored_outbox(directory, req["outbox_id"]):
            try:
                host.ensure()
            except CoordError as exc:
                return _refusal("room_unavailable", str(exc), 409)
        return _answer(host.service.send_room(room_thread, **kw))
    except chat.RoomRotated as exc:
        return _refusal("room_rotated", str(exc), 409)
    except chat.MessageConflict as exc:
        return _refusal("outbox_conflict", str(exc), 409)
    except (RoomServeError, RoomLaunchError) as exc:
        return _refusal("room_unavailable", str(exc), 409)
    except CoordError as exc:
        return _refusal("send_refused", str(exc), 400)
    except Exception:       # noqa: BLE001 - never a bare 500 for a line that may be written: read the outbox
        return _settled(directory, req["outbox_id"])


def post_room_stop(host: RoomHost, directory: Path | str, owner: str | None, req: Any) -> tuple[int, dict[str, Any]]:
    """``POST /cockpit/room/stop {room_thread_id}``: the operator's interrupt of whatever the room is running. Unfenced
    (B5): it names no turn and takes no lease; a stop that lands on a replaced launch interrupts the same conversation.
    It acts only for a named credential, as the work-thread stop does."""
    if not isinstance(req, dict):
        return _refusal("bad_request", "body must be a JSON object", 400)
    if _owner_parts(owner) is None:
        return _refusal("forbidden", "a stop needs a named credential", 403)
    room_thread, refused = _need_room(directory, req)
    if refused is not None:
        return refused
    conn = threads._open(directory)
    try:
        launch = _room_launch(conn, room_thread) if conn is not None else None
    finally:
        if conn is not None:
            conn.close()
    if launch is None:
        return _refusal("no_session", "the room has no launch to stop", 409)
    try:
        got = host.stop(launch) if host.stop is not None else host.service.interrupt(launch, None)
    except CoordError as exc:
        return _refusal("no_session", str(exc), 409)
    return (409 if got.outcome == "rejected" else 200), {"outcome": got.outcome, "detail": got.detail}


def post_room_mark(host: RoomHost, directory: Path | str, owner: str | None, req: Any) -> tuple[int, dict[str, Any]]:
    """``POST /cockpit/room/mark {room_thread_id, message_id, delivered}``: the operator resolves an ambiguous line of the
    room, delivered or not. ``delivered`` is a JSON boolean and nothing else; the message must be one of this room
    thread's; the body names no owner, state or author."""
    if not isinstance(req, dict):
        return _refusal("bad_request", "body must be a JSON object", 400)
    message, delivered = req.get("message_id"), req.get("delivered")
    if not isinstance(message, str) or _MESSAGE_ID.fullmatch(message) is None:
        return _refusal("bad_request", "message_id names one of the room's messages", 400)
    if type(delivered) is not bool:
        return _refusal("bad_request", "delivered is true or false", 400)
    if _owner_parts(owner) is None:
        return _refusal("forbidden", "a mark needs a named credential", 403)
    room_thread, refused = _need_room(directory, req)
    if refused is not None:
        return refused
    conn = threads._open(directory)
    try:
        row = (conn.execute("SELECT thread_id FROM chat_messages WHERE message_id = ?", (message,)).fetchone()
               if conn is not None and threads._has(conn, "chat_messages") else None)
    finally:
        if conn is not None:
            conn.close()
    if row is None or row[0] != room_thread:
        return _refusal("not_found", "no such message in the room", 404)
    try:
        state = host.service.mark(message, delivered, owner)
    except chat.OutboxRefused as exc:
        return _refusal("not_ambiguous", str(exc), 409)
    except CoordError as exc:
        return _refusal("mark_refused", str(exc), 400)
    return 200, {"message_id": message, "state": state}


def post_room_rotate(host: RoomHost, directory: Path | str, owner: str | None, req: Any) -> tuple[int, dict[str, Any]]:
    """``POST /cockpit/room/rotate {room_thread_id}``: a new conversation, his control, fenced like every room write: a
    page that drew a room that is no longer the open one is a 409 ``room_rotated`` and nothing rotates (two taps on one
    stale page rotate once). The host's own verb makes the new room thread the open one; a host with none serves none.
    The answer is the open room READ AFTER the rotation, never the host's return value."""
    if not isinstance(req, dict):
        return _refusal("bad_request", "body must be a JSON object", 400)
    if _owner_parts(owner) is None:
        return _refusal("forbidden", "a rotation needs a named credential", 403)
    if host.rotate is None:
        return _refusal("not_found", "this server serves no room rotation", 404)
    room_thread, refused = _need_room(directory, req)
    if refused is not None:
        return refused
    try:
        host.rotate(room_thread)      # the host checks the page's room again inside the act: two stale taps rotate once
    except chat.RoomRotated as exc:
        return _refusal("room_rotated", str(exc), 409)
    except CoordError as exc:
        return _refusal("rotate_refused", str(exc), 409)
    conn = threads._open(directory)
    try:
        opened = _open_room(conn) if conn is not None else None
    finally:
        if conn is not None:
            conn.close()
    if opened is None:
        return _refusal("rotate_unsettled", "the rotation ran but no open room is recorded; read the room again", 409)
    return 200, {"room_thread": opened}


def post_room_open(host: RoomHost, owner: str | None, req: Any) -> tuple[int, dict[str, Any]]:
    """``POST /cockpit/room/open {}``: open the room (a thread and its running launch) when none is open. It names no
    room: an already open room is answered as it stands (two tabs opening at once open one room), nothing is opened
    twice. A page with no room has every other control dark, so this is how a first room starts. A launch that cannot
    open is a 409 ``room_unavailable`` carrying the reason."""
    if not isinstance(req, dict):
        return _refusal("bad_request", "body must be a JSON object", 400)
    if _owner_parts(owner) is None:
        return _refusal("forbidden", "opening the room needs a named credential", 403)
    if host.open is None:
        return _refusal("not_found", "this server serves no room opening", 404)
    try:
        handle = host.open()
    except (RoomServeError, RoomLaunchError) as exc:
        return _refusal("room_unavailable", str(exc), 409)
    except CoordError as exc:
        return _refusal("open_refused", str(exc), 409)
    thread = getattr(handle, "room_thread", None)
    if not isinstance(thread, str):
        return _refusal("open_unsettled", "the room opened but did not name its thread; read the room again", 409)
    return 200, {"room_thread": thread}


def _surface(owner: str | None) -> str | None:
    who = _owner_parts(owner)
    if who is None:
        return None
    return who[0] if who[0] in SURFACES else None


def post_room_drop(host: RoomHost, owner: str | None, req: Any) -> tuple[int, dict[str, Any]]:
    """``POST /cockpit/room/drop {words, outbox_id}``: the composer's text, verbatim, as a drop from the credential's
    surface. It involves no model and attaches no note; a resend of the same ``outbox_id`` and words writes nothing."""
    if not isinstance(req, dict):
        return _refusal("bad_request", "body must be a JSON object", 400)
    words, outbox = req.get("words"), req.get("outbox_id")
    if not isinstance(words, str) or not words.strip() or len(words) > WORDS_MAX:
        return _refusal("bad_request", f"a drop is a non-empty string of at most {WORDS_MAX} characters", 400)
    if not isinstance(outbox, str) or chat._OUTBOX_ID.fullmatch(outbox) is None:
        return _refusal("bad_request", "a drop carries the client's own outbox_id: 8 to 128 of A-Z a-z 0-9 . _ - "
                        "(starting with a letter or digit)", 400)
    surface = _surface(owner)
    if surface is None:
        return _refusal("forbidden", "a drop needs a named credential from the cockpit or the phone", 403)
    try:
        drop, created = host.store.accept_drop(words, surface=surface, outbox_id=outbox)
    except DropConflict as exc:
        return _refusal("outbox_conflict", str(exc), 409)
    except CoordError as exc:
        return _refusal("drop_refused", str(exc), 409)
    return 200, {"drop_id": drop.drop_id, "created": created}


def post_proposal(host: RoomHost, owner: str | None, proposal_id: str, action: str, req: Any) -> tuple[int, dict[str, Any]]:
    """``POST /cockpit/room/proposal/<pr-id>/commit {words, outbox_id}`` and ``.../dismiss {}``: his tap on a card. A
    commit's ``words`` are what the card's field held; a settled or expired proposal is a 409 and nothing is written."""
    if not isinstance(req, dict):
        return _refusal("bad_request", "body must be a JSON object", 400)
    surface = _surface(owner)
    if surface is None:
        return _refusal("forbidden", f"a {action} needs a named credential from the cockpit or the phone", 403)
    if action == "dismiss":
        try:
            # the coordinator exposes no public write context for a dismiss: the store's own transaction, as its tests use
            with host.store._write() as (conn, now):
                coordroom.dismiss(conn, proposal_id, now=now)
        except CoordError as exc:
            return _proposal_refusal(exc)
        return 200, {"proposal_id": proposal_id, "state": "dismissed"}
    words, outbox = req.get("words"), req.get("outbox_id")
    if not isinstance(words, str) or not words.strip() or len(words) > WORDS_MAX:
        return _refusal("bad_request", f"a drop is a non-empty string of at most {WORDS_MAX} characters", 400)
    if not isinstance(outbox, str) or chat._OUTBOX_ID.fullmatch(outbox) is None:
        return _refusal("bad_request", "a commit carries the client's own outbox_id: 8 to 128 of A-Z a-z 0-9 . _ - "
                        "(starting with a letter or digit)", 400)
    try:
        drop, created = coordroom.commit(host.store, proposal_id, words, outbox, surface)
    except DropConflict as exc:
        return _refusal("outbox_conflict", str(exc), 409)
    except CoordError as exc:
        return _proposal_refusal(exc)
    return 200, {"proposal_id": proposal_id, "drop_id": drop.drop_id, "created": created}


def _proposal_refusal(exc: CoordError) -> tuple[int, dict[str, Any]]:
    """A named answer per case. The coordinator raises one error type for these, so the case is read from its sentence;
    a sentence it does not know is a generic 409 ``proposal_refused``, never a wrong specific name."""
    text = str(exc)
    if text.startswith("no proposal"):
        return _refusal("no_proposal", text, 404)
    if text.startswith("a drop "):      # `_check_words`: needs words, too long, NUL, not UTF-8
        return _refusal("bad_words", text, 400)
    if text.startswith("outbox_id ") and "not this proposal's" in text:
        return _refusal("outbox_conflict", text, 409)
    if " is expired" in text:
        return _refusal("proposal_expired", text, 409)
    if " is already " in text or " is committed" in text or " is dismissed" in text:
        return _refusal("proposal_settled", text, 409)
    return _refusal("proposal_refused", text, 409)


def encode(body: Mapping[str, Any]) -> bytes:
    """The bytes a mount sends: JSON a browser's ``JSON.parse`` accepts."""
    return json.dumps(body, allow_nan=False).encode("utf-8")


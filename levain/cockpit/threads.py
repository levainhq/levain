"""The cockpit's thread view, read half (CH-U1a): the desk's threads and one thread's stream as JSON.

Pure readers over ``<directory>/dispatch.db`` opened ``mode=ro``: this module never creates the file, a table or a
trigger, and writes nothing. A thread's stream is :func:`levain.desk.chat.timeline`: the "before the chat
record" block, then the merged stream in ``gseq`` order, the only order a thread view promises (no ordering by
``at`` across it). A tool call stays open until its own completion event. A driver without ``catch_up`` shows as a
``gap``. An approval event is an inert line: the record holds no correlation from a vendor control-request id to a
queue request, so the view draws no link and names no card. An operator line is ``unverified``: no signature check
exists on the record. A store missing a table degrades only the part that table feeds.

``serve_threads`` is the handler a server mount calls; it holds no HTTP of its own.

The write half is one route: :func:`post_stop`, the operator's interrupt of a work thread's running turn, over a
:class:`~levain.desk.chat.ChatService` the mount was given. It reads no owner, author or epoch from a request body
and acts on no launch the body names: the launch is the thread's latest worker launch, and the body's
``expected_launch_id`` (the launch the page drew) only fences it. A work thread carries no operator composer in 1.0:
the operator talks in the room (:mod:`levain.cockpit.room`), and a work thread's steering is a drop."""
from __future__ import annotations

import json
import re
import sqlite3
import urllib.parse
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from levain.cockpit.consent import ConsentError, parse_stamp, utc_stamp
from levain.desk import chat
from levain.desk import envelope as env
from levain.desk.chat_approvals import APPROVAL_EVENT_KINDS
from levain.desk.legacy import role_word
from levain.desk.store import CoordError

THREADS_LIST_PATH = "/cockpit/threads.json"
THREAD_PREFIX = "/cockpit/thread/"
THREAD_SUFFIX = ".json"
THREAD_POST_ACTIONS = ("stop",)
LIST_MAX = 200
_THREAD_ID = re.compile(r"th-[0-9a-f]{32}")
_LAUNCH_ID = re.compile(r"ln-[0-9a-f]{32}")
_APPROVAL_KINDS = ("vendor_approval_request", "vendor_approval_resolved")
# chat_timeline's own approval arms (chat_approvals.APPROVAL_VIEW_ARMS): 'approval', and 'approval_' + each event kind
_VIEW_APPROVAL_KINDS = frozenset(("approval", *("approval_" + k for k in APPROVAL_EVENT_KINDS)))


def is_threads_path(path: str) -> bool:
    """Whether ``path`` is one of this module's routes (the mount's dispatch test)."""
    return path == THREADS_LIST_PATH or (path.startswith(THREAD_PREFIX) and path.endswith(THREAD_SUFFIX))


def post_route(path: str) -> tuple[str, str] | None:
    """``(thread_id, action)`` for ``POST /cockpit/thread/<th-id>/stop``, else None."""
    if not path.startswith(THREAD_PREFIX):
        return None
    thread, sep, action = path[len(THREAD_PREFIX):].partition("/")
    if not sep or action not in THREAD_POST_ACTIONS or _THREAD_ID.fullmatch(thread) is None:
        return None
    return thread, action


def _open(directory: Path | str) -> sqlite3.Connection | None:
    path = Path(directory) / "dispatch.db"
    if not path.is_file():
        return None
    return sqlite3.connect(path.absolute().as_uri() + "?mode=ro", uri=True, timeout=2.0, isolation_level=None)


def _has(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE name = ?", (name,)).fetchone() is not None


def _worker_launch(conn: sqlite3.Connection, thread_id: str) -> str | None:
    if not _has(conn, "launches"):
        return None
    row = conn.execute(
        "SELECT l.launch_id FROM launches l, json_each(l.threads) j WHERE j.value = ? AND l.role = 'worker' "
        "ORDER BY l.gseq DESC LIMIT 1", (thread_id,)).fetchone()
    return None if row is None else row[0]


# --- the authority header (CH-U1b slice 2) ----------------------------------------------------------------------------
# What stands behind a thread's worker, as ONE server-made line drawn from the dispatch records only: the envelope most
# recently bound to the thread (``envelope_binds``), its signed caps and effects (``envelopes.body``), its stop with any
# ``stop_earlier`` narrowing, and a kill or a done thread (``envelope_attenuations``). The page draws ``line`` and
# computes nothing. No bound envelope reads "no standing authority"; a record this reader cannot read says so and never
# reads as "none". ``IN_FORCE`` is the one fact that is not a record: no envelope row grants anything until the
# desk's S1 cutover (``EnvelopeStore.authority_refusal`` answers NOT_IN_FORCE for every effect), and a test pins
# that so the day S1 lands this line is revisited.
IN_FORCE = False
_NOT_IN_FORCE_WORDS = "Not in force yet: every land, push and publish still asks you separately."
_BUDGET_WORDS = (("tool_calls", "tool calls"), ("tokens", "tokens"), ("hours", "hours"))


def _authority(state: str, line: str, envelope_id: str | None = None, **more: Any) -> dict[str, Any]:
    return {"state": state, "line": line, "envelope_id": envelope_id, "in_force": IN_FORCE, **more}


def _count(value: Any) -> int:
    if type(value) is not int or value < 0:
        raise ValueError("not a count")
    return value


def _caps_words(budget: Any) -> str:
    if not isinstance(budget, dict):
        raise ValueError("no budget")
    parts = []
    for key, word in _BUDGET_WORDS:
        if key in budget:
            parts.append(f"{_count(budget[key]):,} {word}")
    if "dollars_cents" in budget:
        cents = _count(budget["dollars_cents"])
        parts.append(f"${cents // 100:,}.{cents % 100:02d}")
    return "up to " + ", ".join(parts) if parts else "no caps named"


def _effects_words(body: dict[str, Any], stop: Any = None) -> str:
    n = {k: len(body[k]) if isinstance(body.get(k), list) else _bad() for k in ("lands", "pushes", "publishes")}
    words = f"effects named: {n['lands']} land, {n['pushes']} push, {n['publishes']} publish"
    launches = body.get(env.LAUNCH_LIST)          # absent = no grant, and no words about it
    if launches is None:
        return words
    if not isinstance(launches, list):
        _bad()
    until = f" until {_clock_words(stop)}" if stop is not None else ""
    for e in launches:
        # parse_body's own bounds, so a line it would refuse is never worded (fail closed upstream)
        if (not isinstance(e, dict) or set(e) != {"repo", "count"} or not isinstance(e["repo"], str)
                or type(e["count"]) is not int or not 1 <= e["count"] <= env.LAUNCH_MAX):
            _bad()
        words += f"; may launch up to {e['count']} seats in {e['repo']}{until}"
    return words


def _bad() -> int:
    raise ValueError("an effect list is a list")


def _clock_words(stamp: Any) -> str:
    """A record's stamp as ``2026-10-23 18:00 UTC``; a stamp that is not the one form raises."""
    return parse_stamp(stamp).strftime("%Y-%m-%d %H:%M UTC")


def authority_header(conn: sqlite3.Connection, thread_id: str, now: datetime | None = None) -> dict[str, Any]:
    """The thread's authority line: ``{state, line, envelope_id, in_force, stop_at?}``. ``state`` is ``none``, ``bound``,
    ``ended`` (killed, or its thread done), ``expired`` (its effective stop has passed) or ``unreadable``. Never raises."""
    try:
        if not (_has(conn, "envelope_binds") and _has(conn, "envelopes")):
            return _authority("none", "No standing authority: no envelope is bound to this thread.")
        head = conn.execute("SELECT e.envelope_id, e.body, e.stop_at FROM envelope_binds b JOIN envelopes e ON "
                            "e.envelope_id = b.envelope_id WHERE b.thread_id = ? ORDER BY b.gseq DESC LIMIT 1",
                            (thread_id,)).fetchone()
        if head is None:
            return _authority("none", "No standing authority: no envelope is bound to this thread.")
        envelope_id, raw, stop_at = head
        short = str(envelope_id)[:11]
        body = json.loads(raw)
        if not isinstance(body, dict):
            raise ValueError("envelope body")
        rows = conn.execute("SELECT kind, value, by, at FROM envelope_attenuations WHERE envelope_id = ? AND "
                            "(thread_id IS NULL OR thread_id IS ?) ORDER BY gseq", (envelope_id, thread_id)).fetchall() \
            if _has(conn, "envelope_attenuations") else []
        for kind, _value, by, at in rows:
            if kind in ("kill", "thread_done"):
                why = "killed" if kind == "kill" else "its thread is done"
                return _authority("ended", f"No standing authority: envelope {short} ended ({why}, by {role_word(by)}, "
                                  f"{_clock_words(at)}).", envelope_id)
        stop = parse_stamp(stop_at)
        for kind, value, _by, _at in rows:
            if kind == "stop_earlier":
                try:
                    stop = min(stop, parse_stamp(value))
                except ConsentError:
                    raise ValueError("a stop_earlier stamp") from None
        stamp = utc_stamp(stop)
        dropped = {v for k, v, _by, _at in rows if k == "drop_effect"}
        for kind in env.GRANT_LISTS:        # the effective body's rule (EnvelopeStore._effective): a drop names an identity
            if isinstance(body.get(kind), list):
                body[kind] = [e for e in body[kind] if env.effect_identity(kind, e) not in dropped]
        clock = now if now is not None else datetime.now(timezone.utc)
        if stop <= clock:
            return _authority("expired", f"No standing authority: envelope {short} stopped {_clock_words(stamp)}.",
                              envelope_id, stop_at=stamp)
        line = (f"Standing authority: envelope {short}, {_caps_words(body.get('budget'))}; {_effects_words(body, stamp)}; "
                f"stops {_clock_words(stamp)}. {_NOT_IN_FORCE_WORDS if not IN_FORCE else ''}").rstrip()
        return _authority("bound", line, envelope_id, stop_at=stamp)
    except Exception:       # noqa: BLE001 - fail closed: a record this reader cannot read is never "no authority"
        return _authority("unreadable", "The standing authority for this thread could not be read.")


def model_in_use(service: Any, launch: str | None) -> str | None:
    """The model the attached session reported in its init (``SessionMeta.model``), or None: no service, no launch, no
    session, a meta that was not read at attach, or a driver that named none. A dict read: the service caches the meta
    once at attach (``ChatService.session_meta``) and no driver method runs here, so a poll never reaches a session."""
    if service is None or launch is None:
        return None
    try:
        meta = service.session_meta(launch)
    except Exception:       # noqa: BLE001 - a model that cannot be read is shown as absent, never guessed
        return None
    model = getattr(meta, "model", None)
    return model if isinstance(model, str) and model else None


def stoppable(service: Any, launch: str | None) -> bool:
    """Whether the page's Stop for this launch can reach a session: the server mounts a ChatService (the stop route is
    served only then) AND that service holds a usable session for the launch (``ChatService.has_session``). False for a
    server with no service (the stop route is a 404), for the room's service under ``levain serve --room`` (it holds
    room launches only, so a work-thread stop is a 409 ``no_session``), and for a service lacking the method. A dict
    read, never a driver call. The page draws its Stop only when this is true (roomfix1 R3)."""
    if service is None or launch is None:
        return False
    try:
        return service.has_session(launch) is True
    except Exception:       # noqa: BLE001 - a service that cannot say is a Stop that is not drawn
        return False


def _chat_readable(conn: sqlite3.Connection) -> bool:
    """Whether ``chat_timeline`` can be read: the view exists and every table it joins is present."""
    try:
        conn.execute("SELECT 1 FROM chat_timeline LIMIT 1").fetchall()
    except sqlite3.OperationalError:
        return False
    return True


def thread_list(directory: Path | str) -> dict[str, Any]:
    """Every work thread with a worker launch or a chat record, newest activity first (at most ``LIST_MAX``); the
    room's threads are not listed (the room has its own read). Activity is
    the larger of the thread's latest chat ``gseq`` and its latest worker launch's ``gseq``; ``latest_gseq`` and
    ``latest_at`` report that activity, so a launch-only thread is not "no record yet"."""
    conn = _open(directory)
    if conn is None:
        return {"threads": [], "truncated": False}
    try:
        conn.execute("BEGIN")
        try:
            if not _has(conn, "threads"):
                return {"threads": [], "truncated": False}
            chat_ok = _chat_readable(conn)
            chat_g = "(SELECT MAX(c.gseq) FROM chat_timeline c WHERE c.thread_id = t.thread_id)" if chat_ok else "NULL"
            launch_g = ("(SELECT MAX(l.gseq) FROM launches l, json_each(l.threads) j WHERE j.value = t.thread_id "
                        "AND l.role = 'worker')") if _has(conn, "launches") else "NULL"
            not_room = ("NOT EXISTS (SELECT 1 FROM launches r, json_each(r.threads) rj WHERE rj.value = t.thread_id "
                        "AND r.role = 'room')") if _has(conn, "launches") else "1"
            rows = conn.execute(
                f"SELECT thread_id, cg, lg FROM (SELECT t.thread_id AS thread_id, {chat_g} AS cg, {launch_g} AS lg "
                f"FROM threads t WHERE {not_room}) WHERE cg IS NOT NULL OR lg IS NOT NULL "
                "ORDER BY MAX(COALESCE(cg, 0), COALESCE(lg, 0)) DESC, thread_id LIMIT ?", (LIST_MAX + 1,)).fetchall()
            out = []
            for thread_id, cg, lg in rows[:LIST_MAX]:
                latest = max(g for g in (cg, lg) if g is not None)
                # the row that supplied the activity gseq also supplies its time
                at = (conn.execute("SELECT at FROM chat_timeline WHERE thread_id = ? AND gseq = ?",
                                   (thread_id, latest)).fetchone() if cg == latest
                      else conn.execute("SELECT at FROM launches WHERE gseq = ?", (latest,)).fetchone())
                launch = _worker_launch(conn, thread_id)
                out.append({"thread_id": thread_id, "latest_gseq": latest, "latest_at": at[0] if at else None,
                            "launch_id": launch})
            return {"threads": out, "truncated": len(rows) > LIST_MAX}
        finally:
            conn.execute("COMMIT")
    finally:
        conn.close()


_Fact = tuple[str | None, int, int, str | None]       # (call_id, truncated, backfill, tool_name)


def _event_facts(conn: sqlite3.Connection, launches: set[str]) -> tuple[dict[tuple[str, str], _Fact], bool]:
    """``({(launch_id, key): (call_id, truncated, backfill, tool_name)}, gap)``; ``gap`` is true when any launch's latest
    cursor row says the driver could not catch up, or the stream holds a ``gap`` event. A table the store lacks adds
    nothing, and a store this reader opens read-only before the writer has added ``tool_name`` gives every name None."""
    facts: dict[tuple[str, str], _Fact] = {}
    gap = False
    has_events, has_cursors = _has(conn, "chat_events"), _has(conn, "chat_cursors")
    named = has_events and any(r[1] == "tool_name" for r in conn.execute("PRAGMA table_info(chat_events)"))
    for launch in launches:
        if has_events:
            for key, call_id, truncated, backfill, kind, tool_name in conn.execute(
                    "SELECT vendor_event_key, call_id, truncated, backfill, kind, %s FROM chat_events WHERE launch_id = ?"
                    % ("tool_name" if named else "NULL"), (launch,)):
                facts[(launch, key)] = (call_id, truncated, backfill, tool_name)
                gap = gap or kind == "gap"
        if has_cursors:
            last = conn.execute("SELECT gap FROM chat_cursors WHERE launch_id = ? ORDER BY seq DESC LIMIT 1",
                                (launch,)).fetchone()
            gap = gap or bool(last and last[0])
    return facts, gap


def _item(row: chat.TimelineRow, facts: Mapping[tuple[str, str], _Fact],
          done: set[tuple[str, str]]) -> dict[str, Any]:
    call_id, truncated, backfill, tool_name = facts.get((row.launch_id or "", row.ref_id), (None, 0, 0, None))
    # an approval's ref is the kind alone and its body is dropped: the vendor's control-request id is not a queue request
    # id, and a driver may put the tool name, its input or that id in the body, so none of it is offered
    view_approval = row.kind in _VIEW_APPROVAL_KINDS
    approval = view_approval or row.kind in _APPROVAL_KINDS
    # the view arms' ref is the whole durable key (spawn nonce, vendor tool_use_id): a constant, never derived from it
    ref = row.kind if view_approval else row.ref_id.partition(":")[0] if approval else row.ref_id
    item: dict[str, Any] = {"gseq": row.gseq, "kind": row.kind, "source": row.source, "ref": ref,
                            "launch_id": row.launch_id, "status": row.status, "body": None if approval else row.body,
                            "at": row.at,
                            "echo_of": row.echo_of, "truncated": bool(truncated), "backfill": bool(backfill)}
    if row.kind == "message" and row.source == "operator":
        item["unverified"] = True       # no signature check exists on the record, so the view never says "verified"
    if row.kind == "vendor_tool_call" and call_id:
        item["tool"] = {"call_id": call_id, "open": (row.launch_id or "", call_id) not in done}
    if row.kind in ("vendor_tool_call", "vendor_tool_result"):
        # the vendor's own name for the tool (None for a row stored before the column existed): a page strips a Read's line
        # numbers only for "Read", and keeps its shape rule for None
        item["tool_name"] = tool_name
    return item


def thread_stream(directory: Path | str, thread_id: str, service: Any = None) -> dict[str, Any] | None:
    """One thread: ``before`` (older replies, by ``at`` among themselves), ``stream`` (``gseq`` order), the ``gap``
    flag, the model and the authority line; None for a thread the store does not hold. A completion belongs to its own launch: two
    launches may reuse a ``call_id``."""
    if _THREAD_ID.fullmatch(thread_id) is None:
        return None
    conn = _open(directory)
    if conn is None:
        return None
    try:
        conn.execute("BEGIN")
        try:
            if not _has(conn, "threads"):
                return None
            try:
                before, merged = chat.timeline(conn, thread_id)
            except CoordError:
                return None
            except sqlite3.OperationalError:    # a table the timeline reads is missing: no lines, the rest still answers
                before, merged = [], []
            launches = {r[0] for r in conn.execute(
                "SELECT l.launch_id FROM launches l, json_each(l.threads) j WHERE j.value = ?",
                (thread_id,))} if _has(conn, "launches") else set()
            facts, gap = _event_facts(conn, launches)
            done = {(lid, f[0]) for (lid, k), f in facts.items() if k.startswith("tool_result:") and f[0]}
            launch = _worker_launch(conn, thread_id)
            return {"thread_id": thread_id, "launch_id": launch, "gap": gap, "model": model_in_use(service, launch),
                    "stoppable": stoppable(service, launch),
                    "authority": authority_header(conn, thread_id),
                    "before": [_item(r, facts, done) for r in before],
                    "stream": [_item(r, facts, done) for r in merged]}
        finally:
            conn.execute("COMMIT")
    finally:
        conn.close()


def serve_threads(path: str, query: str, *, directory: Path | str, service: Any = None) -> tuple[int, dict[str, Any]]:
    """``(status, body)`` for a GET on one of this module's routes. ``query`` is accepted and ignored: the routes
    take none. A store that cannot be read is a 500 with a fixed sentence, never a traceback."""
    del query
    try:
        if path == THREADS_LIST_PATH:
            return 200, thread_list(directory)
        if path.startswith(THREAD_PREFIX) and path.endswith(THREAD_SUFFIX):
            body = thread_stream(directory, urllib.parse.unquote(path[len(THREAD_PREFIX):-len(THREAD_SUFFIX)]),
                                 service)
            if body is None:
                return 404, {"error": "not_found", "message": "no such thread recorded"}
            return 200, body
    except Exception:      # the boundary: whatever broke, the caller gets the fixed sentence and no detail
        return 500, {"error": "internal", "message": "the thread record could not be read"}
    return 404, {"error": "not_found", "message": "no such thread route"}


def _launch_of(directory: Path | str, thread_id: str) -> str | None:
    conn = _open(directory)
    if conn is None:
        return None
    try:
        return _worker_launch(conn, thread_id)
    finally:
        conn.close()


def _refusal(error: str, message: str, status: int, **extra: Any) -> tuple[int, dict[str, Any]]:
    return status, {"error": error, "message": message, **extra}


def _expected_launch(req: dict[str, Any]) -> str | None:
    """The request's ``expected_launch_id`` (the launch the page drew), or None when it is absent or malformed."""
    want = req.get("expected_launch_id")
    return want if isinstance(want, str) and _LAUNCH_ID.fullmatch(want) is not None else None


def _replaced(want: str, launch: str) -> tuple[int, dict[str, Any]] | None:
    if want == launch:
        return None
    return _refusal("launch_replaced", "the thread's worker launch changed since this page drew it; nothing was "
                    "done, and the page shows the new launch once it reads the thread again", 409)


def post_stop(service: Any, directory: Path | str, thread_id: str, req: Any) -> tuple[int, dict[str, Any]]:
    """``(status, body)`` for ``POST /cockpit/thread/<th-id>/stop {expected_launch_id, turn_id?}``: the operator's
    interrupt of the launch's turn. It is not lease-fenced (a stop never waits on the turn lease), but it is fenced on
    the launch: ``expected_launch_id`` must equal the thread's latest worker launch, else a 409 ``launch_replaced`` and
    nothing is interrupted. ``outcome`` is the driver's answer: ``confirmed`` and ``no_such_request`` are 200,
    ``ambiguous`` is a 200 the page shows as not confirmed, ``rejected`` is a 409; a launch with no usable session is a
    409."""
    if not isinstance(req, dict):
        return _refusal("bad_request", "body must be a JSON object", 400)
    turn = req.get("turn_id")
    if turn is not None:
        try:
            chat._line("an interrupt's turn id", turn)       # the shape the service itself refuses: a 400, never a 409
        except CoordError as exc:
            return _refusal("bad_request", str(exc), 400)
    want = _expected_launch(req)
    if want is None:
        return _refusal("bad_request", "a stop names the launch the page shows (expected_launch_id)", 400)
    launch = _launch_of(directory, thread_id)
    if launch is None:
        return _refusal("not_found", "no such thread recorded", 404)
    fenced = _replaced(want, launch)
    if fenced is not None:
        return fenced
    try:
        got = service.interrupt(launch, turn)
    except CoordError as exc:
        return _refusal("no_session", str(exc), 409)
    body = {"outcome": got.outcome, "detail": got.detail}
    return (409 if got.outcome == "rejected" else 200), body


def encode(body: Mapping[str, Any]) -> bytes:
    """The bytes a mount sends: JSON a browser's ``JSON.parse`` accepts."""
    return json.dumps(body, allow_nan=False).encode("utf-8")

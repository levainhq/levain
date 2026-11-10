"""RM-F: the cockpit's one Claude room, through the real ``levain serve`` envelope (make_server) beside a ChatService over
a ScriptedSession and a RoomHost over a real CoordStore. The gate is the sign/consent family's; the owner is the
credential's; a line is the operator's, queued, with no turn lease and no epoch; a drop is only his tap or his verbatim
words; the room's summary is a labelled note, never his words. The live child (a real ``claude``) is RM-G's."""
from __future__ import annotations

import json
import sqlite3
import urllib.error
import urllib.request

import pytest

from levain.cockpit import room as cockpit_room
from levain.desk import chat, room
from levain.desk.chat import ChatService, ChatStore
from levain.desk.envelopes import EnvelopeStore
from levain.desk.store import CoordError
from tests.test_chat_room_send import LIVE, Clock, Counting, _attach, _open_room_thread, _room_launch, _thread
from tests.test_cockpit_flip_parity import _drive, needs_browser
from tests.test_cockpit_k2a import TOKEN, _install, _serve
from tests.test_cockpit_threads_mount import qdir  # noqa: F401  (fixture: the consent directory the server reads)
from levain.desk.legacy import ROLE_DESK

def scripted(*sends):
    """A room session reporting the room's own mode (dontAsk) whose sends follow ``sends`` (a session of any other mode
    latches the room, as the desk's room tests pin)."""
    s = Counting()
    s._sends = list(sends)
    return s


OWNER = ("cockpit", "browser")        # the owner the write token's credential names


def _call(base, path, body=None, token=TOKEN, method="POST", ctype="application/json"):
    headers = {"Content-Type": ctype}
    if token is not None:
        headers["X-Levain-Write-Token"] = token
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:  # noqa: S310 - loopback
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


class World:
    """An open room with a running launch and a session attached, and the host the routes act through."""

    def __init__(self, qdir, session=None):
        self.store = EnvelopeStore(qdir, clock=Clock())
        self.store.coord_set_agent("none")
        self.cs = ChatStore(self.store)
        self.cs._connect().close()
        self.thread = _thread(self.store)
        _open_room_thread(self.store, self.thread)
        self.launch = _room_launch(self.store, self.thread, "room-sid-1")
        self.svc = ChatService(self.cs, **LIVE)
        self.session = _attach(self.svc, self.cs, self.launch, session)
        self.ensured = 0
        self.ensure_error = None
        self.host = cockpit_room.RoomHost(self.svc, self.store, ensure=self._ensure, rotate=self._rotate)

    def _ensure(self):
        self.ensured += 1
        if self.ensure_error is not None:
            raise self.ensure_error

    def _rotate(self, expected=None):
        new = _thread(self.store, title="the next room")
        with self.store._write() as (conn, _now):
            room.rotate(conn, new)
        return new

    def lines(self):
        return self.cs._read(lambda c: c.execute(
            "SELECT author_kind, author_surface, client_id, body, mode, owner_epoch FROM chat_messages ORDER BY gseq").fetchall())

    def drops(self):
        c = sqlite3.connect(self.store.path)
        try:
            return c.execute("SELECT words, surface FROM drops WHERE surface != 'cli' ORDER BY gseq").fetchall()
        finally:
            c.close()

    def notes(self):
        c = sqlite3.connect(self.store.path)
        try:
            return c.execute("SELECT author, body FROM drop_notes").fetchall()
        finally:
            c.close()

    def body(self, text="go", outbox="ob-00000001", **kw):
        return {"room_thread_id": self.thread, "body": text, "outbox_id": outbox, **kw}

    def propose(self, message_id, summary="the room's own summary"):
        with self.store._write() as (conn, now):
            return room.propose(conn, self.launch, [message_id], summary, now=now)


@pytest.fixture
def w(qdir):
    return World(qdir)


def _serve_room(tmp_path, w, name="entity", **kw):
    _root, src, _ids = _install(tmp_path / name)
    return _serve(src, write_token=TOKEN, chat_service=w.svc, room_host=w.host, **kw)


def _first_message_id(w):
    return w.cs._read(lambda c: c.execute("SELECT message_id FROM chat_messages ORDER BY gseq").fetchone()[0])


# --- the gate and the mount ---------------------------------------------------------------------------------------

def test_no_credential_or_a_wrong_one_is_refused_before_anything_is_read_or_written(tmp_path, w):
    paths = ["send", "stop", "mark", "drop", "rotate", "proposal/pr-" + "0" * 32 + "/commit",
             "proposal/pr-" + "0" * 32 + "/dismiss"]
    with _serve_room(tmp_path, w) as (base, _h):
        for token in (None, "wrong-token"):
            status, body = _call(base, cockpit_room.ROOM_PATH, token=token, method="GET")
            assert status == 403 and w.thread not in str(body), (token, body)
            for p in paths:
                status, body = _call(base, "/cockpit/room/" + p, w.body(), token)
                assert status == 403, (p, token, body)
        status, _b = _call(base, "/cockpit/room/send", w.body(), TOKEN, ctype="text/plain")
        assert status in (400, 403, 415), status          # CSRF layer 2: JSON only
    assert w.lines() == [] and w.drops() == [] and w.session.written == []


def test_the_routes_are_absent_without_a_room_host(tmp_path, w):
    _root, src, _ids = _install(tmp_path)
    with _serve(src, write_token=TOKEN, chat_service=w.svc) as (base, _h):
        for p in ("send", "stop", "drop"):
            status, body = _call(base, "/cockpit/room/" + p, w.body())
            assert status == 404 and body["error"] == "not_found", (p, body)


def test_a_malformed_route_is_not_a_route(tmp_path, w):
    with _serve_room(tmp_path, w) as (base, _h):
        for p in ("launch", "proposal/not-an-id/commit", "proposal/pr-" + "0" * 32 + "/explode", "proposal"):
            status, _b = _call(base, "/cockpit/room/" + p, {})
            assert status == 404, p
    assert w.lines() == []


def test_no_dispatch_store_is_an_empty_room_and_nothing_is_created(tmp_path, qdir):
    _root, src, _ids = _install(tmp_path)
    with _serve(src, write_token=TOKEN) as (base, _h):
        status, body = _call(base, cockpit_room.ROOM_PATH, method="GET")
    assert status == 200 and body["room_thread"] is None and body["stream"] == []
    assert not (qdir / "dispatch.db").exists()


# --- the read -----------------------------------------------------------------------------------------------------

def test_the_read_names_the_open_room_its_launch_composer_model_and_no_turn(tmp_path, w):
    with _serve_room(tmp_path, w) as (base, _h):
        status, got = _call(base, cockpit_room.ROOM_PATH, method="GET")
        assert status == 200 and got["room_thread"] == w.thread and got["launch_id"] == w.launch
        assert got["composer"]["state"] == "writable" and "turn" not in got["composer"]
        assert got["model"] == "claude-room-model" and got["proposals"] == []
        _call(base, "/cockpit/room/send", w.body("hello <b>room</b>"))
        status, got = _call(base, cockpit_room.ROOM_PATH, method="GET")
        [item] = [i for i in got["stream"] if i["kind"] == "message"]
        assert item["body"] == "hello <b>room</b>" and item["source"] == "operator" and item["unverified"] is True
        gseqs = [i["gseq"] for i in got["stream"]]
        assert gseqs == sorted(gseqs) and len(set(gseqs)) == len(gseqs)


def test_a_store_missing_a_table_degrades_that_part_not_the_read(tmp_path, w):
    with _serve_room(tmp_path, w) as (base, _h):
        _call(base, "/cockpit/room/send", w.body())
        part = tmp_path / "part"
        part.mkdir()
        for missing in ("chat_events", "chat_cursors", "drop_proposals"):
            src = sqlite3.connect(w.store.path)
            dst = sqlite3.connect(part / "dispatch.db")
            src.backup(dst)
            src.close()
            if missing == "drop_proposals":
                dst.execute("DROP TABLE drop_notes")
            dst.execute(f"DROP TABLE {missing}")
            dst.commit(); dst.close()
            status, got = cockpit_room.serve_room(cockpit_room.ROOM_PATH, directory=part)
            assert status == 200 and got["room_thread"] == w.thread, missing
            (part / "dispatch.db").unlink()


# --- send ---------------------------------------------------------------------------------------------------------

def test_a_send_is_the_operators_queued_under_the_credentials_owner_with_no_epoch(tmp_path, w):
    with _serve_room(tmp_path, w) as (base, _h):
        status, body = _call(base, "/cockpit/room/send", w.body("fix the build"))
        assert status == 200 and body["receipt"]["state"] == "accepted" and body["receipt"]["created"] is True
        assert w.lines() == [("operator", OWNER[0], OWNER[1], "fix the build", "queue", None)]
        assert len(w.session.written) == 1
        # the same outbox id again sends nothing again
        status, body = _call(base, "/cockpit/room/send", w.body("fix the build"))
        assert status == 200 and body["receipt"]["created"] is False and len(w.session.written) == 1
        # the same id saying something else is a conflict and writes nothing
        status, body = _call(base, "/cockpit/room/send", w.body("something else"))
        assert status == 409 and body["error"] == "outbox_conflict" and len(w.lines()) == 1


@pytest.mark.parametrize("extra", [{"mode": "steer"}, {"mode": "queue"}, {"expected_turn_id": "t-1"},
                                   {"expected_epoch": 3}, {"expected_launch_id": "ln-" + "0" * 32}])
def test_a_room_line_names_no_mode_turn_epoch_or_launch(tmp_path, w, extra):
    with _serve_room(tmp_path, w) as (base, _h):
        status, body = _call(base, "/cockpit/room/send", w.body(**extra))
    assert status == 400 and body["error"] == "bad_request", body
    assert w.lines() == [] and w.session.written == []


def test_a_body_that_names_an_author_is_not_read_for_one(tmp_path, w):
    with _serve_room(tmp_path, w) as (base, _h):
        status, _b = _call(base, "/cockpit/room/send", w.body("hi", author_kind=ROLE_DESK, author_surface="phone",
                                                                client_id="someone-else"))
    assert status == 200
    assert w.lines() == [("operator", "cockpit", "browser", "hi", "queue", None)]


@pytest.mark.parametrize("req", [
    {"outbox_id": "ob-00000001"}, {"body": "   ", "outbox_id": "ob-00000001"}, {"body": "go"}, {"body": "go", "outbox_id": ""},
    {"body": "go", "outbox_id": "x"}, {"body": "go", "outbox_id": "ob 0000001"}, {"body": "x" * 20_001, "outbox_id": "ob-00000001"},
    {"body": "go\x00", "outbox_id": "ob-00000001"}, {"body": 7, "outbox_id": "ob-00000001"}, [1, 2],
])
def test_a_malformed_send_is_a_400_and_writes_nothing(tmp_path, w, req):
    if isinstance(req, dict):
        req = {**req, "room_thread_id": w.thread}
    with _serve_room(tmp_path, w) as (base, _h):
        status, body = _call(base, "/cockpit/room/send", req)
    assert status == 400 and body["error"] == "bad_request", body
    assert w.lines() == [] and w.ensured == 0


@pytest.mark.parametrize("named", [None, "", "th-short", 7, "th-" + "0" * 32])
def test_a_send_naming_no_open_room_is_refused_and_writes_nothing(tmp_path, w, named):
    req = w.body()
    if named is None:
        req.pop("room_thread_id")
    else:
        req["room_thread_id"] = named
    with _serve_room(tmp_path, w) as (base, _h):
        status, body = _call(base, "/cockpit/room/send", req)
    assert (status, body["error"]) in ((400, "bad_request"), (409, "room_rotated")), body
    assert w.lines() == [] and w.session.written == []


def test_a_send_the_driver_did_not_deliver_is_a_409_carrying_its_cause(tmp_path, qdir):
    w = World(qdir, scripted("not_sent"))
    with _serve_room(tmp_path, w) as (base, _h):
        status, body = _call(base, "/cockpit/room/send", w.body())
        assert status == 409 and body["error"] == "not_delivered" and body["receipt"]["state"] == "not_delivered", body
        assert body["message"] == body["receipt"]["cause"] and "not sent" in body["message"]
        # its resend answers the same stored refusal, never a success
        status, body = _call(base, "/cockpit/room/send", w.body())
        assert status == 409 and body["error"] == "not_delivered" and body["receipt"]["created"] is False


def test_a_stored_pending_or_attempted_receipt_is_never_a_200(tmp_path, w, monkeypatch):
    # L3_chatstack_r1 codex-3: a receipt that never settled must not clear the page's draft and say "sent"
    class Got:
        message_id, outbox_id, cause, created = "cm-" + "0" * 32, "ob-00000001", "", False
        state = "pending"

    for state, want in (("pending", (409, "undelivered")), ("attempted", (202, "unresolved"))):
        Got.state = state
        monkeypatch.setattr(w.svc, "send_room", lambda *a, **k: Got)
        with _serve_room(tmp_path, w, state) as (base, _h):
            status, body = _call(base, "/cockpit/room/send", w.body())
        assert (status, body["error"]) == want, (state, body)
        assert body["receipt"]["state"] == state


def test_ambiguous_is_a_200_the_page_draws_as_may_not_have_arrived(tmp_path, qdir):
    w = World(qdir, scripted("ambiguous"))
    with _serve_room(tmp_path, w) as (base, _h):
        status, body = _call(base, "/cockpit/room/send", w.body())
    assert status == 200 and body["receipt"]["state"] == "ambiguous"


def test_a_resend_after_a_rotation_is_answered_from_the_record_and_a_new_line_is_refused(tmp_path, w):
    with _serve_room(tmp_path, w) as (base, _h):
        status, first = _call(base, "/cockpit/room/send", w.body())
        assert status == 200
        w._rotate()
        ensured = w.ensured
        status, again = _call(base, "/cockpit/room/send", w.body())          # same outbox id, old room thread
        assert status == 200 and again["receipt"]["message_id"] == first["receipt"]["message_id"]
        assert again["receipt"]["created"] is False and w.ensured == ensured     # no launch opened for a replay
        status, body = _call(base, "/cockpit/room/send", w.body("new line", "ob-00000002"))
        assert status == 409 and body["error"] == "room_rotated"
    assert len(w.lines()) == 1 and len(w.session.written) == 1


def test_the_room_launch_is_ensured_for_a_new_line_and_not_for_a_replay(tmp_path, w):
    with _serve_room(tmp_path, w) as (base, _h):
        _call(base, "/cockpit/room/send", w.body())
        assert w.ensured == 1
        _call(base, "/cockpit/room/send", w.body())
        assert w.ensured == 1


def test_a_room_that_cannot_open_refuses_the_line_in_words_and_writes_nothing(tmp_path, w):
    w.ensure_error = CoordError("the room could not open with its read-only tools: Bash")
    with _serve_room(tmp_path, w) as (base, _h):
        status, body = _call(base, "/cockpit/room/send", w.body())
    assert status == 409 and body["error"] == "room_unavailable" and "read-only tools" in body["message"]
    assert w.lines() == [] and w.session.written == []


def test_a_non_coorderror_after_the_write_is_answered_from_the_outbox_never_a_bare_500(tmp_path, w, monkeypatch):
    real = w.svc.send_room

    def boom(*a, **k):
        real(*a, **k)
        raise RuntimeError("the driver blew up after the write")

    monkeypatch.setattr(w.svc, "send_room", boom)
    with _serve_room(tmp_path, w) as (base, _h):
        status, body = _call(base, "/cockpit/room/send", w.body())
        assert status == 202 and body["error"] == "unresolved", body        # stored: not "not written", not a 500
        monkeypatch.setattr(w.svc, "send_room", real)
        status, body = _call(base, "/cockpit/room/send", w.body())          # the page's resend answers the record
        assert status == 200 and body["receipt"]["created"] is False
    assert len(w.lines()) == 1


def test_a_non_coorderror_before_any_write_says_so(tmp_path, w, monkeypatch):
    monkeypatch.setattr(w.svc, "send_room", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("early")))
    with _serve_room(tmp_path, w) as (base, _h):
        status, body = _call(base, "/cockpit/room/send", w.body())
    assert status == 500 and body["error"] == "send_failed" and "not written" in body["message"] and w.lines() == []


# --- stop and mark ------------------------------------------------------------------------------------------------

def test_stop_interrupts_the_room_unfenced_and_a_stale_page_is_refused(tmp_path, w):
    with _serve_room(tmp_path, w) as (base, _h):
        status, body = _call(base, "/cockpit/room/stop", {"room_thread_id": w.thread})
        assert status == 200 and body["outcome"] in ("confirmed", "no_such_request"), body
        status, body = _call(base, "/cockpit/room/stop", {"room_thread_id": "th-" + "0" * 32})
        assert status == 409 and body["error"] == "room_rotated"
        status, body = _call(base, "/cockpit/room/stop", {})
        assert status == 400
        w.svc.detach(w.launch)
        status, body = _call(base, "/cockpit/room/stop", {"room_thread_id": w.thread})
        assert status == 409 and body["error"] == "no_session"


@pytest.fixture
def amb(qdir):
    w = World(qdir, scripted("ambiguous"))
    return w


def _states(w, message):
    return w.cs._read(lambda c: [r[0] for r in c.execute(
        "SELECT state FROM chat_outbox_events WHERE message_id = ? ORDER BY seq", (message,))])


def test_marking_resolves_the_ambiguous_line_under_the_credentials_owner(tmp_path, amb):
    w = amb
    with _serve_room(tmp_path, w) as (base, _h):
        _call(base, "/cockpit/room/send", w.body())
        message = _first_message_id(w)
        status, got = _call(base, cockpit_room.ROOM_PATH, method="GET")
        assert got["composer"]["unresolved"] == [message]
        # a hostile body names an owner, a state and an author: none is read
        status, body = _call(base, "/cockpit/room/mark", {
            "room_thread_id": w.thread, "message_id": message, "delivered": True, "owner": "phone:attacker",
            "state": "refused", "by": "service"})
        assert status == 200 and body == {"message_id": message, "state": "accepted"}, body
        cause, by = w.cs._read(lambda c: c.execute(
            "SELECT cause, by FROM chat_outbox_events WHERE message_id = ? ORDER BY seq DESC LIMIT 1", (message,)).fetchone())
        assert by == "operator" and cause == "marked by cockpit:browser"
        status, got = _call(base, cockpit_room.ROOM_PATH, method="GET")
        assert got["composer"]["unresolved"] == []
        status, body = _call(base, "/cockpit/room/mark", {"room_thread_id": w.thread, "message_id": message, "delivered": False})
        assert status == 409 and body["error"] == "not_ambiguous" and _states(w, message)[-1] == "accepted"


@pytest.mark.parametrize("delivered", ["true", 1, 0, None, "yes", [], {}])
def test_delivered_is_a_json_boolean_and_nothing_else(tmp_path, amb, delivered):
    w = amb
    with _serve_room(tmp_path, w) as (base, _h):
        _call(base, "/cockpit/room/send", w.body())
        message = _first_message_id(w)
        status, _b = _call(base, "/cockpit/room/mark", {"room_thread_id": w.thread, "message_id": message, "delivered": delivered})
    assert status == 400 and _states(w, message)[-1] == "ambiguous"


def test_a_message_of_another_thread_or_none_is_not_markable_and_a_stale_page_is_refused(tmp_path, amb):
    w = amb
    with _serve_room(tmp_path, w) as (base, _h):
        _call(base, "/cockpit/room/send", w.body())
        message = _first_message_id(w)
        status, _b = _call(base, "/cockpit/room/mark", {"room_thread_id": w.thread, "message_id": "cm-" + "0" * 32, "delivered": True})
        assert status == 404
        status, body = _call(base, "/cockpit/room/mark", {"room_thread_id": "th-" + "0" * 32, "message_id": message, "delivered": True})
        assert status == 409 and body["error"] == "room_rotated"
        w._rotate()                                   # the line now belongs to a rotated room
        status, body = _call(base, "/cockpit/room/mark", {"room_thread_id": w.thread, "message_id": message, "delivered": True})
        assert status == 409 and body["error"] == "room_rotated"
    assert _states(w, message)[-1] == "ambiguous"


# --- the drop door ------------------------------------------------------------------------------------------------

def test_the_drop_control_sends_the_composer_text_verbatim_from_the_credentials_surface_with_no_note(tmp_path, w):
    words = "  keep   my <b>spacing</b>\n and my lines  "
    with _serve_room(tmp_path, w) as (base, _h):
        status, body = _call(base, "/cockpit/room/drop", {"words": words, "outbox_id": "ob-drop0001"})
        assert status == 200 and body["created"] is True and body["drop_id"]
        status, again = _call(base, "/cockpit/room/drop", {"words": words, "outbox_id": "ob-drop0001"})
        assert status == 200 and again["created"] is False and again["drop_id"] == body["drop_id"]
        status, _b = _call(base, "/cockpit/room/drop", {"words": "other words", "outbox_id": "ob-drop0001"})
        assert status == 409                                          # an outbox id is one drop
    assert w.drops() == [(words, "cockpit")] and w.notes() == []
    assert w.lines() == [] and w.session.written == []                 # no model turn, no chat line


@pytest.mark.parametrize("req", [{"words": "", "outbox_id": "ob-drop0001"}, {"words": "  ", "outbox_id": "ob-drop0001"},
                                 {"outbox_id": "ob-drop0001"}, {"words": "x"}, {"words": "x", "outbox_id": "x"},
                                 {"words": "x" * 20_001, "outbox_id": "ob-drop0001"}, {"words": 5, "outbox_id": "ob-drop0001"}, [1]])
def test_a_malformed_drop_is_a_400_and_writes_nothing(tmp_path, w, req):
    with _serve_room(tmp_path, w) as (base, _h):
        status, body = _call(base, "/cockpit/room/drop", req)
    assert status == 400 and body["error"] == "bad_request" and w.drops() == []


# --- proposals: his tap -------------------------------------------------------------------------------------------

def test_a_card_commits_the_bytes_the_field_held_with_the_summary_as_a_labelled_note(tmp_path, w):
    with _serve_room(tmp_path, w) as (base, _h):
        _call(base, "/cockpit/room/send", w.body("tell the desk to run the suite"))
        p = w.propose(_first_message_id(w), "He asked for the suite to be run.")
        status, got = _call(base, cockpit_room.ROOM_PATH, method="GET")
        [card] = got["proposals"]
        assert card["state"] == "open" and card["words"] == "tell the desk to run the suite"
        assert card["summary"] == "He asked for the suite to be run." and card["drop_id"] is None
        assert w.drops() == []                                    # a proposal is not a drop
        edited = "tell the desk to run the whole suite"
        status, body = _call(base, f"/cockpit/room/proposal/{p.proposal_id}/commit", {"words": edited, "outbox_id": "ob-card0001"})
        assert status == 200 and body["created"] is True and body["drop_id"], body
        assert w.drops() == [(edited, "cockpit")]                 # his edited bytes, nothing of the room's
        assert w.notes() == [("room", "He asked for the suite to be run.")]
        status, again = _call(base, f"/cockpit/room/proposal/{p.proposal_id}/commit", {"words": edited, "outbox_id": "ob-card0001"})
        assert status == 200 and again["created"] is False and again["drop_id"] == body["drop_id"]
        status, body2 = _call(base, f"/cockpit/room/proposal/{p.proposal_id}/commit", {"words": edited, "outbox_id": "ob-card0002"})
        assert status == 409 and body2["error"] == "proposal_settled"
        status, got = _call(base, cockpit_room.ROOM_PATH, method="GET")
        assert got["proposals"][0]["state"] == "committed" and got["proposals"][0]["drop_id"] == body["drop_id"]
    assert len(w.drops()) == 1


def test_a_dismissed_card_is_settled_and_a_card_of_a_rotated_room_cannot_be_committed(tmp_path, w):
    with _serve_room(tmp_path, w) as (base, _h):
        _call(base, "/cockpit/room/send", w.body("one"))
        p1 = w.propose(_first_message_id(w))
        status, body = _call(base, f"/cockpit/room/proposal/{p1.proposal_id}/dismiss", {})
        assert status == 200 and body["state"] == "dismissed"
        status, body = _call(base, f"/cockpit/room/proposal/{p1.proposal_id}/dismiss", {})
        assert status == 409 and body["error"] == "proposal_settled"
        status, body = _call(base, f"/cockpit/room/proposal/{p1.proposal_id}/commit", {"words": "one", "outbox_id": "ob-card0001"})
        assert status == 409
        _call(base, "/cockpit/room/send", w.body("two", "ob-00000002"))
        message2 = w.cs._read(lambda c: c.execute("SELECT message_id FROM chat_messages ORDER BY gseq DESC").fetchone()[0])
        p2 = w.propose(message2)
        w._rotate()
        status, body = _call(base, f"/cockpit/room/proposal/{p2.proposal_id}/commit", {"words": "two", "outbox_id": "ob-card0003"})
        assert status == 409 and body["error"] == "proposal_expired"
        status, body = _call(base, "/cockpit/room/proposal/pr-" + "0" * 32 + "/commit", {"words": "two", "outbox_id": "ob-card0004"})
        assert status == 404
    assert w.drops() == []


@pytest.mark.parametrize("req", [{"outbox_id": "ob-card0001"}, {"words": "", "outbox_id": "ob-card0001"},
                                 {"words": "x"}, {"words": "x", "outbox_id": "x"}, {"words": 3, "outbox_id": "ob-card0001"}, [1]])
def test_a_malformed_commit_is_a_400_and_the_card_stays_open(tmp_path, w, req):
    with _serve_room(tmp_path, w) as (base, _h):
        _call(base, "/cockpit/room/send", w.body())
        p = w.propose(_first_message_id(w))
        status, body = _call(base, f"/cockpit/room/proposal/{p.proposal_id}/commit", req)
        assert status == 400 and body["error"] == "bad_request"
        status, got = _call(base, cockpit_room.ROOM_PATH, method="GET")
        assert got["proposals"][0]["state"] == "open"
    assert w.drops() == []


# --- rotation -----------------------------------------------------------------------------------------------------

def test_rotation_opens_a_new_conversation_and_the_old_page_is_refused(tmp_path, w):
    with _serve_room(tmp_path, w) as (base, _h):
        status, body = _call(base, "/cockpit/room/rotate", {"room_thread_id": w.thread})
        assert status == 200 and body["room_thread"] != w.thread
        status, got = _call(base, cockpit_room.ROOM_PATH, method="GET")
        assert got["room_thread"] == body["room_thread"] and got["stream"] == []
        status, body = _call(base, "/cockpit/room/send", w.body())
        assert status == 409 and body["error"] == "room_rotated"


def test_a_host_with_no_rotation_serves_none(tmp_path, w):
    w.host.rotate = None
    with _serve_room(tmp_path, w) as (base, _h):
        status, body = _call(base, "/cockpit/room/rotate", {})
    assert status == 404 and body["error"] == "not_found"


# --- the page (structural only: headless Chrome clamps width, spore-1066; the glass is Phill's) ------------------------

_PAGE = ('<!doctype html><meta charset="utf-8"><link rel="stylesheet" href="/dashboard.css"><div id="host"></div>'
         '<script src="/room_view.js"></script><script src="/rvc.js"></script>')


def _assets(boot):
    return {"/rvc.html": ("text/html; charset=utf-8", _PAGE.encode()), "/rvc.js": ("text/javascript; charset=utf-8", boot.encode())}


def _boot(extra=""):
    return ('window.__rv = LevainRoomView.mount(document.getElementById("host"), {pollMs: 150, speech: null, storage: null, %s'
            'headers: () => ({"X-Levain-Write-Token": "%s"})});' % (extra, TOKEN))


@needs_browser
def test_the_page_sends_draws_text_never_markup_and_has_no_mic_without_speech(tmp_path, w):
    with _serve_room(tmp_path, w, extra_assets=_assets(_boot())) as (base, _h):
        out = _drive(tmp_path, base + "/rvc.html", [
            {"until": "document.querySelector('.rv-input') && !document.querySelector('.rv-send').disabled"},
            {"eval": "document.querySelector('.rv-input').value = 'hello <b>there</b>'; document.querySelector('.rv-send').click(); 1"},
            {"until": "document.querySelector('.rv-sendnote').textContent === 'Sent.' && document.querySelector('.rv-input').value === ''"},
            {"until": "[...document.querySelectorAll('.rv-text')].some(n => n.textContent === 'hello <b>there</b>')"},
            {"eval": "JSON.stringify({bold: document.querySelectorAll('.rv-text b').length, mic: document.querySelectorAll('.rv-mic').length,"
                     " you: document.querySelectorAll('.rv-you .rv-who')[0].textContent})"},
        ])
    got = json.loads(out["values"][4])
    assert got == {"bold": 0, "mic": 0, "you": "You"}
    assert [r[3] for r in w.lines()] == ["hello <b>there</b>"]


@needs_browser
def test_a_line_the_driver_did_not_deliver_keeps_the_draft_and_says_so(tmp_path, qdir):
    w = World(qdir, scripted("not_sent"))
    with _serve_room(tmp_path, w, extra_assets=_assets(_boot())) as (base, _h):
        out = _drive(tmp_path, base + "/rvc.html", [
            {"until": "document.querySelector('.rv-input') && !document.querySelector('.rv-send').disabled"},
            {"eval": "document.querySelector('.rv-input').value = 'a line that never lands'; document.querySelector('.rv-send').click(); 1"},
            {"until": "document.querySelector('.rv-sendnote').textContent.indexOf('Not sent') === 0"},
            {"eval": "JSON.stringify({draft: document.querySelector('.rv-input').value, note: document.querySelector('.rv-sendnote').textContent})"},
        ])
    got = json.loads(out["values"][3])
    assert got["draft"] == "a line that never lands" and "not sent" in got["note"] and "Sent." not in got["note"]


@needs_browser
def test_a_card_shows_editable_words_a_labelled_summary_and_commits_only_on_the_tap(tmp_path, w):
    with _serve_room(tmp_path, w, "first") as (base0, _h):
        _call(base0, "/cockpit/room/send", w.body("tell the desk hello"))
    p = w.propose(_first_message_id(w), "He greeted the desk.")
    with _serve_room(tmp_path, w, extra_assets=_assets(_boot())) as (base, _h):
        out = _drive(tmp_path, base + "/rvc.html", [
            {"until": "document.querySelector('.rv-card textarea')"},
            {"eval": "JSON.stringify({words: document.querySelector('.rv-cardwords').value, label: document.querySelector('.rv-summary-label').textContent,"
                     " summary: document.querySelector('.rv-summary').textContent})"},
            {"eval": "new Promise(r => setTimeout(() => r(1), 500))"},         # polls pass: nothing is sent by itself
            {"eval": "document.querySelector('.rv-cardwords').value = 'tell the desk hello, kindly'; document.querySelector('.rv-commit').click(); 1"},
            {"until": "document.querySelector('.rv-card-committed')"},
        ])
        assert w.drops() == [("tell the desk hello, kindly", "cockpit")]
    got = json.loads(out["values"][1])
    assert got == {"words": "tell the desk hello", "label": "The room's summary (information, not your words)",
                   "summary": "He greeted the desk."}
    assert p.proposal_id and w.notes() == [("room", "He greeted the desk.")]


def test_the_room_dock_has_styles_for_every_control_it_draws():
    import re
    from pathlib import Path
    css = (Path(cockpit_room.__file__).resolve().parents[1] / "templates" / "web" / "dashboard.css").read_text()
    for cls in ("rv", "rv-dock", "rv-input", "rv-actions", "rv-send", "rv-compose", "rv-pending", "rv-stop", "rv-mic", "rv-rotate", "rv-sendnote",
                "rv-card", "rv-summary", "rv-mark", "rv-line", "rv-you", "rv-room"):
        assert re.search(r"\.%s\b" % cls, css), cls
    assert not re.search(r"\.rv[^{]*\{[^}]*#[0-9a-fA-F]{3,6}\b", css)       # tokens only: both themes follow


def test_the_page_script_never_says_restricted_or_limited():
    from pathlib import Path
    js = (Path(cockpit_room.__file__).resolve().parents[1] / "templates" / "web" / "room_view.js").read_text().lower()
    for word in ("talk-only", "restricted", "limited", "read-only"):
        assert word not in js, word


# --- RM-F fix round 1 (FINDINGS_rmf_r1): reproductions first --------------------------------------------------------------

def test_f1_a_committed_card_shows_the_words_that_were_dropped_not_the_proposals(tmp_path, w):
    with _serve_room(tmp_path, w) as (base, _h):
        _call(base, "/cockpit/room/send", w.body("hello there"))
        p = w.propose(_first_message_id(w))
        status, _b = _call(base, f"/cockpit/room/proposal/{p.proposal_id}/commit",
                           {"words": "COMPLETELY DIFFERENT", "outbox_id": "ob-card0001"})
        assert status == 200
        status, got = _call(base, cockpit_room.ROOM_PATH, method="GET")
    [card] = got["proposals"]
    assert card["state"] == "committed" and card["words"] == "COMPLETELY DIFFERENT"
    assert w.drops() == [("COMPLETELY DIFFERENT", "cockpit")]


def test_f2_a_rotation_names_the_room_it_rotates_and_a_stale_page_is_refused(tmp_path, w):
    with _serve_room(tmp_path, w) as (base, _h):
        old = w.thread
        status, first = _call(base, "/cockpit/room/rotate", {"room_thread_id": old})
        assert status == 200 and first["room_thread"] != old
        status, body = _call(base, "/cockpit/room/rotate", {"room_thread_id": old})      # the second tap, same stale page
        assert status == 409 and body["error"] == "room_rotated", body
        status, got = _call(base, cockpit_room.ROOM_PATH, method="GET")
        assert got["room_thread"] == first["room_thread"]                                # nothing rotated again
        for req in ({}, {"room_thread_id": "th-short"}, {"room_thread_id": 7}):
            status, body = _call(base, "/cockpit/room/rotate", req)
            assert status == 400 and body["error"] == "bad_request", (req, body)
    opened = sqlite3.connect(w.store.path)
    try:
        assert opened.execute("SELECT count(*) FROM room_events WHERE kind = 'rotated'").fetchone()[0] == 1
    finally:
        opened.close()


def _each_post_route(w):
    """(path, body) for every room POST route, so a class-wide refusal is exercised on all of them."""
    pid = "pr-" + "0" * 32
    return [("send", w.body()), ("stop", {"room_thread_id": w.thread}),
            ("mark", {"room_thread_id": w.thread, "message_id": "cm-" + "0" * 32, "delivered": True}),
            ("drop", {"words": "x", "outbox_id": "ob-drop0001"}), ("rotate", {"room_thread_id": w.thread}),
            (f"proposal/{pid}/commit", {"words": "x", "outbox_id": "ob-card0001"}), (f"proposal/{pid}/dismiss", {})]


@pytest.mark.parametrize("owner", [None, "cockpit:bad name", "cockpit:", "phone:", "nobody:x", "paired-"])
def test_f3_every_room_post_route_refuses_an_unnamed_credential_before_acting(w, owner):
    host = w.host
    for path, req in _each_post_route(w):
        route = cockpit_room.post_route("/cockpit/room/" + path)
        action, pid = route
        if pid is not None:
            status, body = cockpit_room.post_proposal(host, owner, pid, action, req)
        elif action == "send":
            status, body = cockpit_room.post_room_send(host, w.store.path.parent, owner, req)
        elif action == "stop":
            status, body = cockpit_room.post_room_stop(host, w.store.path.parent, owner, req)
        elif action == "mark":
            status, body = cockpit_room.post_room_mark(host, w.store.path.parent, owner, req)
        elif action == "drop":
            status, body = cockpit_room.post_room_drop(host, owner, req)
        else:
            status, body = cockpit_room.post_room_rotate(host, w.store.path.parent, owner, req)
        assert status == 403 and body["error"] == "forbidden", (path, owner, status, body)
    assert w.lines() == [] and w.drops() == [] and w.session.written == []


def test_f3_the_mount_hands_a_credential_with_no_client_name_to_every_route(tmp_path, w, monkeypatch):
    from levain import web_server
    monkeypatch.setattr(web_server._Handler, "_thread_owner", lambda self: None)
    with _serve_room(tmp_path, w) as (base, _h):
        for path, req in _each_post_route(w):
            status, body = _call(base, "/cockpit/room/" + path, req)
            assert status == 403 and body["error"] == "forbidden", (path, status, body)
    assert w.lines() == [] and w.drops() == [] and w.session.written == []


def test_f4_the_rotation_answers_the_open_room_whatever_the_host_returns(tmp_path, w):
    # the landed coordroom.rotate returns the OLD thread; the route must answer the room it re-reads
    old = w.thread
    real = w._rotate

    def returns_old(_expected=None):
        real()
        return old

    w.host.rotate = returns_old
    with _serve_room(tmp_path, w) as (base, _h):
        status, body = _call(base, "/cockpit/room/rotate", {"room_thread_id": old})
        assert status == 200 and body["room_thread"] != old
        status, got = _call(base, cockpit_room.ROOM_PATH, method="GET")
        assert got["room_thread"] == body["room_thread"]


def test_f5_each_proposal_refusal_has_its_own_name_and_status(tmp_path, w):
    pid0 = "pr-" + "0" * 32
    with _serve_room(tmp_path, w) as (base, _h):
        _call(base, "/cockpit/room/send", w.body("one"))
        p = w.propose(_first_message_id(w))
        got = _call(base, f"/cockpit/room/proposal/{pid0}/dismiss", {})
        assert got[0] == 404 and got[1]["error"] == "no_proposal", got
        got = _call(base, f"/cockpit/room/proposal/{pid0}/commit", {"words": "x", "outbox_id": "ob-card0001"})
        assert got[0] == 404 and got[1]["error"] == "no_proposal", got
        got = _call(base, f"/cockpit/room/proposal/{p.proposal_id}/commit", {"words": "x\x00", "outbox_id": "ob-card0001"})
        assert got[0] == 400 and got[1]["error"] == "bad_words", got
        # a drop made elsewhere under an outbox id: this proposal cannot reuse it
        status, _b = _call(base, "/cockpit/room/drop", {"words": "one", "outbox_id": "ob-shared01"})
        assert status == 200
        got = _call(base, f"/cockpit/room/proposal/{p.proposal_id}/commit", {"words": "one", "outbox_id": "ob-shared01"})
        assert got[0] == 409 and got[1]["error"] == "outbox_conflict", got
        got = _call(base, f"/cockpit/room/proposal/{p.proposal_id}/commit", {"words": "other", "outbox_id": "ob-shared01"})
        assert got[0] == 409 and got[1]["error"] == "outbox_conflict", got
        assert _call(base, f"/cockpit/room/proposal/{p.proposal_id}/commit", {"words": "one", "outbox_id": "ob-card0002"})[0] == 200
        got = _call(base, f"/cockpit/room/proposal/{p.proposal_id}/dismiss", {})
        assert got[0] == 409 and got[1]["error"] == "proposal_settled", got
        # the drop door: an outbox id reused for other words is the same named conflict
        got = _call(base, "/cockpit/room/drop", {"words": "different", "outbox_id": "ob-shared01"})
        assert got[0] == 409 and got[1]["error"] == "outbox_conflict", got
        # a rotated room's card is expired, named so
        _call(base, "/cockpit/room/send", w.body("two", "ob-00000002"))
        m2 = w.cs._read(lambda c: c.execute("SELECT message_id FROM chat_messages ORDER BY gseq DESC").fetchone()[0])
        p2 = w.propose(m2)
        w._rotate()
        got = _call(base, f"/cockpit/room/proposal/{p2.proposal_id}/commit", {"words": "two", "outbox_id": "ob-card0003"})
        assert got[0] == 409 and got[1]["error"] == "proposal_expired", got


def test_f8_a_store_missing_drop_notes_degrades_nothing_and_the_read_creates_nothing(tmp_path, w):
    with _serve_room(tmp_path, w) as (base, _h):
        _call(base, "/cockpit/room/send", w.body("one"))
    p = w.propose(_first_message_id(w))
    part = tmp_path / "part8"
    part.mkdir()
    src = sqlite3.connect(w.store.path)
    dst = sqlite3.connect(part / "dispatch.db")
    src.backup(dst)
    src.close()
    dst.execute("DROP TABLE drop_notes")
    dst.commit(); dst.close()
    status, got = cockpit_room.serve_room(cockpit_room.ROOM_PATH, directory=part)
    assert status == 200 and got["room_thread"] == w.thread
    assert [c["proposal_id"] for c in got["proposals"]] == [p.proposal_id] and got["proposals"][0]["state"] == "open"
    again = sqlite3.connect(part / "dispatch.db")
    try:
        assert again.execute("SELECT count(*) FROM sqlite_master WHERE name = 'drop_notes'").fetchone()[0] == 0
    finally:
        again.close()


def test_f9_a_stored_undelivered_line_answers_409_on_the_unresolved_path_as_on_the_replay_path(tmp_path, qdir):
    w = World(qdir, scripted("not_sent"))
    real = w.svc.send_room

    def boom(*a, **k):
        real(*a, **k)
        raise RuntimeError("after the write")

    w.svc.send_room = boom
    with _serve_room(tmp_path, w) as (base, _h):
        status, body = _call(base, "/cockpit/room/send", w.body())
        assert status == 409 and body["error"] == "not_delivered", body
        w.svc.send_room = real
        status2, body2 = _call(base, "/cockpit/room/send", w.body())
        assert (status2, body2["error"]) == (status, body["error"])


# --- RM-F fix round 1: the page (F6, F7, F11) -----------------------------------------------------------------------------

_UNTIL_READY = {"until": "document.querySelector('.rv-input') && !document.querySelector('.rv-send').disabled"}


def _post_from_page(path, body):
    """A page-side POST with the write token, for a test that changes the room while the page polls."""
    return ("fetch('/cockpit/room/%s', {method: 'POST', headers: {'Content-Type': 'application/json', 'X-Levain-Write-Token': '%s'},"
            " body: JSON.stringify(%s)}).then(r => r.status)" % (path, TOKEN, json.dumps(body)))


@needs_browser
def test_f11_the_stop_button_interrupts_the_room(tmp_path, w):
    # Stop is for a running turn (1105-49), and this World's session is idle: the read carries one added turn-start row, so the
    # page offers Stop, and the tap goes to the real stop route, which answers for the idle session (the raced-stop words).
    running = ("fetch: (u, o) => fetch(u, o).then(r => !/room\\.json/.test(u) ? r : r.text().then(t => { const b = JSON.parse(t); "
               "b.stream.push({gseq: 99999, kind: 'vendor_turn_started', source: 'vendor', ref: 'turn_started:x', launch_id: b.launch_id, body: null, at: ''}); "
               "return {status: r.status, ok: r.ok, text: () => Promise.resolve(JSON.stringify(b))}; })), ")
    with _serve_room(tmp_path, w, extra_assets=_assets(_boot(running))) as (base, _h):
        out = _drive(tmp_path, base + "/rvc.html", [
            _UNTIL_READY,
            {"until": "!document.querySelector('.rv-stop').disabled"},
            {"eval": "document.querySelector('.rv-stop').click(); 1"},
            {"until": "['Stopped.', 'Nothing was running to stop.'].includes(document.querySelector('.rv-sendnote').textContent) && "
                      "document.querySelector('.rv-sendnote').textContent"},
        ])
    assert out["values"][3] in ("Stopped.", "Nothing was running to stop.")


@needs_browser
def test_f11_the_rotate_button_opens_a_new_conversation_naming_the_room_it_shows(tmp_path, w):
    old = w.thread
    with _serve_room(tmp_path, w, extra_assets=_assets(_boot())) as (base, _h):
        _drive(tmp_path, base + "/rvc.html", [
            _UNTIL_READY,
            {"eval": "document.querySelector('.rv-rotate').click(); 1"},
            {"until": "document.querySelector('.rv-sendnote').textContent === 'A new conversation is open.' && window.__rv.model().thread"},
        ])
        status, got = _call(base, cockpit_room.ROOM_PATH, method="GET")
    assert got["room_thread"] != old


@needs_browser
def test_f11_the_mark_buttons_resolve_an_ambiguous_line(tmp_path, amb):
    w = amb
    with _serve_room(tmp_path, w, extra_assets=_assets(_boot())) as (base, _h):
        _drive(tmp_path, base + "/rvc.html", [
            _UNTIL_READY,
            {"eval": "document.querySelector('.rv-input').value = 'maybe'; document.querySelector('.rv-send').click(); 1"},
            {"until": "document.querySelector('.rv-mark-yes')"},
            {"eval": "document.querySelector('.rv-mark-yes').click(); 1"},
            {"until": "!document.querySelector('.rv-mark')"},
        ])
        message = _first_message_id(w)
    assert _states(w, message)[-1] == "accepted"


@needs_browser
def test_f6_a_poll_that_changes_the_room_keeps_a_cards_focus_caret_and_draft(tmp_path, w):
    with _serve_room(tmp_path, w, "first") as (base0, _h):
        _call(base0, "/cockpit/room/send", w.body("tell the desk hello"))
    w.propose(_first_message_id(w), "He greeted the desk.")
    more = _post_from_page("send", w.body("a later line", "ob-00000002"))
    with _serve_room(tmp_path, w, extra_assets=_assets(_boot())) as (base, _h):
        out = _drive(tmp_path, base + "/rvc.html", [
            {"until": "document.querySelector('.rv-cardwords')"},
            {"eval": "var t = document.querySelector('.rv-cardwords'); t.value = 'edited words'; t.dispatchEvent(new Event('input', {bubbles: true}));"
                     " t.focus(); t.setSelectionRange(3, 6); window.__ta = t; 1"},
            {"eval": more},
            {"until": "[...document.querySelectorAll('.rv-text')].some(n => n.textContent === 'a later line')"},
            {"eval": "new Promise(r => setTimeout(() => r(1), 500))"},
            {"eval": "JSON.stringify({same: document.querySelector('.rv-cardwords') === window.__ta, focus: document.activeElement === window.__ta,"
                     " caret: [window.__ta.selectionStart, window.__ta.selectionEnd], value: window.__ta.value,"
                     " cards: document.querySelectorAll('.rv-card').length})"},
        ])
    got = json.loads(out["values"][5])
    assert got == {"same": True, "focus": True, "caret": [3, 6], "value": "edited words", "cards": 1}


@needs_browser
def test_f7_an_older_poll_response_never_redraws_over_a_newer_one(tmp_path, w):
    slow = ("fetch: (function () { var n = 0, real = window.fetch.bind(window); return function (u, o) {"
            " if (String(u).indexOf('room.json') < 0) return real(u, o); var k = ++n;"
            " return real(u, o).then(function (res) { return k === 2 ? new Promise(function (r) { setTimeout(function () { r(res); }, 1200); }) : res; }); }; })(), ")
    with _serve_room(tmp_path, w, extra_assets=_assets(_boot(slow))) as (base, _h):
        out = _drive(tmp_path, base + "/rvc.html", [
            _UNTIL_READY,
            {"eval": "new Promise(r => setTimeout(() => r(1), 450))"},        # poll 2 is now out, answering late with the old room
            {"eval": "document.querySelector('.rv-input').value = 'fresh line'; document.querySelector('.rv-send').click(); 1"},
            {"until": "[...document.querySelectorAll('.rv-text')].some(n => n.textContent === 'fresh line')"},
            {"eval": "new Promise(r => { var gone = 0, t0 = Date.now(), iv = setInterval(() => {"
                     " if (![...document.querySelectorAll('.rv-text')].some(n => n.textContent === 'fresh line')) gone++;"
                     " if (Date.now() - t0 > 1600) { clearInterval(iv); r(gone); } }, 15); })"},
        ])
    assert out["values"][4] == 0

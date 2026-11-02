"""The notification center's backend (S4.1): one list of Levain's pings and the notices posted at the operator, with the
operator's read/unread/delete marks. Eight behaviours, each over a real server on a scratch queue (the center's routes are
the full credential's), each a thing the cockpit page (S4.2) will lean on."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from levain.consent.queue import ConsentQueue, authority_source
from levain.consent.questions import QuestionStore
from levain.webpush import center, held, needs, sender
from levain.webpush import notify_tokens as nt
from levain.webpush.store import PushStore
from tests._h4 import h4_home
from tests.test_cockpit_k2a import _install
from tests.test_cockpit_pairing_http import T, _call
from tests.test_cockpit_webpush import _signed

LIST = "/webpush/center"
ITEM = "/webpush/center/item?id="
READ, READ_ALL, DELETE, CLEAR = (f"/webpush/center/{x}" for x in ("read", "read-all", "delete", "clear"))


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    from levain.consent import band

    monkeypatch.setattr(band, "agent_process", lambda: None)
    monkeypatch.setattr("levain.consent.auto_away.input_idle_seconds", lambda: 9999.0)
    monkeypatch.setattr(held, "flush_if_due", lambda *a, **k: False)
    nt.MINUTE.clear()
    yield
    nt.MINUTE.clear()


@pytest.fixture
def rig(tmp_path):
    q = tmp_path / "signq"
    q.mkdir(mode=0o700)
    return _install(tmp_path)[1], q


def _post(port, token, payload):
    import http.client

    c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    c.request("POST", "/notify", body=json.dumps(payload).encode(),
              headers={"Content-Type": "application/json", nt.HEADER: token})
    r = c.getresponse()
    return r.status, json.loads(r.read() or b"{}")


def _get(port, path):
    return _call(port, "GET", path, token=T)


def _write(port, path, body):
    return _call(port, "POST", path, body, token=T)


def _ids(doc):
    return [i["id"] for i in doc["items"]]


def _consent(q):
    cq = ConsentQueue(q, authority=authority_source(q, anchor=None), home=h4_home(), clock=lambda: datetime.now(timezone.utc))
    return cq, cq.file(agent="claude_code", agent_session_id="s", tool_name="Write",
                       tool_input={"file_path": "/w/a", "content": "x"}, cwd="/w", hold=timedelta(minutes=30), event_id="e1")


def test_the_read_toggle_round_trips_and_a_failed_toggle_is_a_real_http_error(rig, monkeypatch):
    """The per-item toggle: read, then unread again, the count following; an item that is not there is a 404 with
    ``ok: false``, a body that is not the shape is a 400, and a store that cannot be written is a 503 (never a 200)."""
    src, q = rig
    store = PushStore(q)
    store.add_ping("test", None, "levain", "A test ping")
    with _signed(src, q) as (port, _):
        assert _get(port, LIST)[1]["unread"] == 1
        st, doc, _ = _write(port, READ, {"id": "p1", "read": True})
        assert (st, doc) == (200, {"ok": True, "unread": 0})
        st, doc, _ = _get(port, ITEM + "p1")
        assert st == 200 and doc["item"]["read"] is True and doc["item"]["read_at"] and doc["unread"] == 0
        st, doc, _ = _write(port, READ, {"id": "p1", "read": False})
        assert (st, doc) == (200, {"ok": True, "unread": 1})
        assert _get(port, ITEM + "p1")[1]["item"]["read"] is False
        for body, status in (({"id": "n99", "read": True}, 404), ({"id": "p1"}, 400), ({"id": "p1", "read": "yes"}, 400),
                             ({"id": "../x", "read": True}, 400)):
            st, doc, _ = _write(port, READ, body)
            assert st == status and doc["ok"] is False and doc["message"], (body, st, doc)
        assert _call(port, "POST", READ, {"id": "p1", "read": True})[0] == 403          # no credential: refused
        with monkeypatch.context() as m:
            m.setattr(center, "set_read", lambda *a, **k: (_ for _ in ()).throw(OSError("disk")))
            st, doc, _ = _write(port, READ, {"id": "p1", "read": True})
        assert st == 503 and doc["ok"] is False and "nothing was changed" in doc["message"]
        assert _get(port, ITEM + "p1")[1]["item"]["read"] is False                       # and nothing was changed


def test_history_before_the_center_is_read_and_mark_all_read_takes_the_count_to_zero(rig):
    """What existed when the center first came reads as read (an upgrade does not make a month unread); an arrival after
    is unread; mark-all-read answers the new count, which is the list's own."""
    src, q = rig
    store = PushStore(q)
    store.add_ping("consent", "request:old", "levain", "an old ping")
    with store.write() as c:                                  # the store as it was before the center: no floors yet
        c.execute("DELETE FROM meta WHERE key LIKE 'center_floor_%'")
    store = PushStore(q)                                       # a fresh open sets the floors
    store.add_ping("question", "question:7", "levain", "a new ping")
    with _signed(src, q) as (port, _):
        _, doc, _ = _get(port, LIST)
        assert doc["unread"] == 1 and doc["total"] == 2
        assert {i["id"]: i["read"] for i in doc["items"]} == {"p1": True, "p2": False}
        assert _post(port, nt.create(q, "Backup")[1], {"title": "Disk", "body": "90%"})[0] == 200
        assert _get(port, LIST)[1]["unread"] == 2
        assert _write(port, READ_ALL, {})[1] == {"ok": True, "unread": 0}
        _, doc, _ = _get(port, LIST)
        assert doc["unread"] == 0 and all(i["read"] for i in doc["items"]) and doc["total"] == 3
        assert _write(port, READ, {"id": "p1", "read": False})[1]["unread"] == 1          # an old one can be kept unread


def test_delete_and_clear_never_close_a_needs_you_item(rig):
    """A ping about a waiting request is removed from the list and the request still waits: the badge count, the item's
    ``needs_open`` and the queue's own state are untouched by delete and by clear. The record keeps every ping."""
    src, q = rig
    cq, req = _consent(q)
    store = PushStore(q)
    store.add_ping("consent", f"request:{req.id}", "levain", "Write /w/a waits on you")
    store.add_ping("test", None, "levain", "A test ping")
    with _signed(src, q) as (port, _):
        _, doc, _ = _get(port, LIST)
        mine = next(i for i in doc["items"] if i["needs_ref"])
        assert mine["needs_ref"] == f"consent:{req.id}" and mine["needs_open"] is True and doc["needs_count"] == 1
        assert _write(port, DELETE, {"id": mine["id"]})[0] == 200
        _, doc, _ = _get(port, LIST)
        assert _ids(doc) == ["p2"] and doc["needs_count"] == 1                        # the request still waits
        assert _write(port, CLEAR, {})[1] == {"ok": True, "unread": 0}
        _, doc, _ = _get(port, LIST)
        assert doc["items"] == [] and doc["total"] == 0 and doc["needs_count"] == 1
    assert cq.get(req.id)[1] == "pending" and needs.open_needs_count(q) == 1
    assert len(store.record()) == 2                                                    # the append-only record is whole


def test_a_needs_ping_says_whether_its_item_still_waits(rig):
    """``needs_open`` is read at display time from the queue: answered or withdrawn, the same ping stops saying it waits."""
    src, q = rig
    cq, req = _consent(q)
    asked = QuestionStore(q).file(agent="claude_code", agent_session_id="s2", what="ship?", options=[("a", "y"), ("b", "n")],
                                  rec="a", blocks="x", cwd="/w")
    store = PushStore(q)
    store.add_ping("consent", f"request:{req.id}", "levain", "waits")
    store.add_ping("question", f"question:{asked.n}", "levain", "asks")
    with _signed(src, q) as (port, _):
        by_id = {i["id"]: i for i in _get(port, LIST)[1]["items"]}
        assert (by_id["p1"]["needs_open"], by_id["p2"]["needs_open"]) == (True, True)
        assert by_id["p2"]["needs_ref"] == f"question:{asked.n}"
        assert cq.withdraw(req.id)
        QuestionStore(q).answer(asked.n, option="a", channel="cockpit")
        by_id = {i["id"]: i for i in _get(port, LIST)[1]["items"]}
        assert (by_id["p1"]["needs_open"], by_id["p2"]["needs_open"]) == (False, False)
        assert all(i["needs_ref"] for i in by_id.values())                             # still linked, no longer waiting


def test_a_push_carries_its_items_id_and_the_needs_count(rig, monkeypatch):
    """A tap opens the center on that item: a notice's push names ``n<notice id>``, a seat's ping ``p<ping id>``, and the
    payload carries the Needs-you count for the app-icon badge. A held summary is no item and keeps its link."""
    src, q = rig
    cq, req = _consent(q)
    seen = []
    real = sender.build_payload
    monkeypatch.setattr(sender, "build_payload",
                        lambda title, body, tag, url, ping, badge=None: seen.append((url, ping, badge)) or real(title, body, tag, url, ping, badge))
    store = PushStore(q)
    pid = store.add_ping("consent", f"request:{req.id}", "levain", "waits")
    ping_of_notice, nid = store.record_notice("notify", "notify:backup", "[backup] Disk", "backup", "Disk", "90%", "normal",
                                              "Backup", "pushed")
    monkeypatch.setattr(PushStore, "live_subscriptions", lambda self, **k: [])
    sender.deliver(q, store, pid, kind="consent", ref=f"request:{req.id}", title="levain", body="waits", url="/?view=needs")
    sender.deliver(q, store, ping_of_notice, kind="notify", ref="notify:backup", title="[backup] Disk", body="90%", url="/?view=notifications")
    sender.deliver(q, store, pid, kind="summary", ref=None, title="levain", body="held", url="/?view=needs")
    assert seen[0] == (f"/?view=notifications&n=p{pid}", pid, 1)
    assert seen[1] == (f"/?view=notifications&n=n{nid}", ping_of_notice, 1)
    assert seen[2][0] == "/?view=needs"


def test_a_deleted_item_says_it_was_deleted_and_an_unknown_one_says_it_is_not_there(rig):
    """The deep-link line: a link to an item the operator deleted answers ``gone`` with the words the page shows; one that
    never was (or aged out of the record) answers ``not_found``; the page tells the two apart."""
    src, q = rig
    store = PushStore(q)
    store.add_ping("test", None, "levain", "one")
    store.add_ping("test", None, "levain", "two")
    with _signed(src, q) as (port, _):
        assert _write(port, DELETE, {"id": "p1"})[0] == 200
        st, doc, _ = _get(port, ITEM + "p1")
        assert (st, doc["error"], doc["message"], doc["ok"]) == (404, "gone", "That notification was deleted.", False)
        st, doc, _ = _get(port, ITEM + "p42")
        assert (st, doc["error"]) == (404, "not_found")
        assert _write(port, DELETE, {"id": "p1"})[0] == 404                            # deleting twice is not a success
        assert _ids(_get(port, LIST)[1]) == ["p2"] and _get(port, ITEM + "p2")[0] == 200


def test_posted_sources_land_in_the_list_with_their_origin_and_the_raw_markdown(rig):
    """A token's notice keeps its token name and its Markdown body whole (the page enforces the full-URL rule from
    ``origin``); flow's own token may name the source and a title-only post is fine; any other token naming a source is a
    bad request; a plain-text preview is made by the server and never carries a link address."""
    src, q = rig
    backup = nt.create(q, "Backup", urgent=True)[1]
    flow = nt.create(q, "flow poster", "flow", origin="flow")[1]
    body = "see [the board](https://evil.example/x) and **now**\n\n- one\n- two"
    with _signed(src, q) as (port, _):
        assert _post(port, backup, {"title": "Disk 90%", "body": body, "priority": "urgent"})[0] == 200
        st, got = _post(port, flow, {"title": "Email digest", "body": "3 unread", "source": "email"})
        assert st == 200 and got["item"] == f"n{got['id']}"
        assert _post(port, flow, {"title": "Title only"})[0] == 200
        st, got = _post(port, backup, {"title": "spoof", "body": "x", "source": "email"})
        assert st == 400 and got["error"] == "bad_request"
        _, doc, _ = _get(port, LIST)
    by_title = {i["title"]: i for i in doc["items"]}
    disk, mail, bare = by_title["Disk 90%"], by_title["Email digest"], by_title["Title only"]
    assert disk["origin"] == "token" and disk["token_name"] == "Backup" and disk["source"] == "backup"
    assert disk["body"] == body and disk["priority"] == "urgent"                      # the raw Markdown, nothing rendered
    assert disk["preview"] == "see the board and now one two" and "evil.example" not in disk["preview"]
    assert (mail["origin"], mail["source"], mail["token_name"]) == ("flow", "email", None)
    assert bare["body"] == "" and bare["origin"] == "flow" and bare["source"] == "flow"
    for item in doc["items"]:
        assert set(item) == {"id", "origin", "source", "title", "body", "preview", "at", "priority", "read", "read_at",
                             "needs_ref", "needs_open", "token_name", "image_url", "delivery"}
        assert item["at"].endswith("+00:00")


def test_the_list_pages_newest_first_and_a_list_that_cannot_be_read_is_not_an_empty_one(rig, monkeypatch):
    """``next_before`` walks the list; the counts are the whole list's, not the page's; and a store that cannot be read is
    a 503, never ``{items: [], unread: 0}``."""
    src, q = rig
    store = PushStore(q)
    for n in range(5):
        store.add_ping("test", None, "levain", f"ping {n}")
    with _signed(src, q) as (port, _):
        _, first, _ = _get(port, LIST + "?limit=2")
        assert _ids(first) == ["p5", "p4"] and first["next_before"] == "p4" and (first["unread"], first["total"]) == (5, 5)
        _, second, _ = _get(port, LIST + "?limit=2&before=p4")
        assert _ids(second) == ["p3", "p2"] and second["next_before"] == "p2"
        _, last, _ = _get(port, LIST + "?limit=2&before=p2")
        assert _ids(last) == ["p1"] and last["next_before"] is None
        assert _get(port, LIST + "?before=p999")[0] == 404 and _get(port, LIST + "?limit=x")[0] == 400 and _get(port, LIST + "?limit=" + "9" * 5000)[0] == 400
        monkeypatch.setattr(center, "list_items", lambda *a, **k: (_ for _ in ()).throw(OSError("db")))
        st, doc, _ = _get(port, LIST)
        assert st == 503 and doc["ok"] is False and "items" not in doc


# --- S4.1 L3 r1 fixes: each built from its reproduction ---------------------------------------------------------------
def test_600_unread_pings_are_all_reachable_and_bulk_writes_reach_them_all(tmp_path):
    """600 unread pings (past the old per-source 500 cap): paging reaches every one once, mark-all-read takes the badge to 0,
    and clear empties the list (the count and the rows agree)."""
    q = tmp_path / "q"
    q.mkdir(mode=0o700)
    s = PushStore(q)
    for i in range(600):
        s.add_ping("question", f"question:{i + 1}", "levain", f"ping {i}")
    seen, nb = [], None
    while True:
        doc = center.list_items(s, limit=200, before=nb)
        seen += _ids(doc)
        nb = doc["next_before"]
        if not nb:
            break
    assert len(seen) == len(set(seen)) == doc["total"] == 600
    assert center.mark_all_read(s) == 0
    center.clear(s)
    assert center.list_items(s)["total"] == 0


def test_a_cursor_whose_row_was_deleted_or_pruned_still_pages(tmp_path):
    q = tmp_path / "q"
    q.mkdir(mode=0o700)
    s = PushStore(q)
    for i in range(5):
        s.add_ping("question", f"question:{i + 1}", "levain", f"p{i}")
    cur = center.list_items(s, limit=2)["next_before"]
    center.delete(s, cur)
    assert _ids(center.list_items(s, limit=2, before=cur)) == ["p3", "p2"]
    center.clear(s)                                       # and a clear: the cursor row is hidden, still in the record
    assert center.list_items(s, limit=2, before=cur)["items"] == []


def test_a_held_notify_ping_with_no_notice_row_keeps_its_link(tmp_path):
    q = tmp_path / "q"
    q.mkdir(mode=0o700)
    s = PushStore(q)
    pid = s.add_ping("notify", None, "levain", "x")
    assert center.push_url(s, pid, "notify", "/?view=notices") == "/?view=notices"
    assert center.push_url(s, pid, "question", "/x") == f"/?view=notifications&n=p{pid}"


def test_one_unreadable_store_or_ref_degrades_that_item_not_the_page(tmp_path, monkeypatch):
    from levain.webpush import record

    q = tmp_path / "q"
    q.mkdir(mode=0o700)
    s = PushStore(q)
    s.add_ping("question", "question:1", "levain", "z")
    monkeypatch.setattr(record, "outcome_of", lambda *a, **k: (_ for _ in ()).throw(ValueError("bad ref")))
    assert center.list_items(s)["total"] == 1
    monkeypatch.undo()
    monkeypatch.setattr(QuestionStore, "open_questions", lambda self: (_ for _ in ()).throw(RuntimeError("x")))
    monkeypatch.setattr("levain.consent.listing.read_listing",
                        lambda *a, **k: type("L", (), {"pending": [type("E", (), {"state": "pending"})()] * 3})())
    assert needs.open_needs_count(q) is None       # three consents readable, the questions not: unknown, never a 3 that could lower a badge of 6
    monkeypatch.undo()
    monkeypatch.setattr(QuestionStore, "open_questions", lambda self: (_ for _ in ()).throw(RuntimeError("x")))
    assert needs.open_needs_count(q) is None       # nothing counted and a half unreadable: unknown, never 0

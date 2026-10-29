"""The phone's read-only view of the coordinator's review queue and morning card (levain.coordinator.phone, C4-c/C6-c).

The listing's ``coord_review`` and ``coord_morning`` are the queue's own ``list_items`` and the card's own ``card`` /
``render``: nothing here recomputes "waiting on you", and nothing on the phone port clears or reopens an item."""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from levain.coordinator import morning, review
from levain.coordinator.envelopes import EnvelopeStore
from levain.coordinator.phone import REVIEW_CLEARED_SHOWN, REVIEW_OPEN_SHOWN
from tests.test_coordinator_phone import coord_for, device_token, sid
from tests.test_dispatch_phone import repo, world  # noqa: F401  (fixtures)
from tests.test_phone_dispatch import _serve

pytestmark = pytest.mark.skipif(shutil.which("ssh-keygen") is None, reason="needs ssh-keygen")


def _item(store, n: int, feed: str = "seat", text: str | None = None, **kw):
    return review.file_item(store, feed, {"text": text or f"item {n}"}, dedupe_key=f"{feed}:{n}", **kw)[0]


def test_no_store_is_no_panels_and_no_notes(world) -> None:
    out = coord_for(world).coord_listing()
    assert out["coord_review"] is None and out["coord_morning"] is None
    assert out["coord_review_note"] is None and out["coord_morning_note"] is None
    assert coord_for(world).listing()["coord_store_read"] is True and sid(coord_for(world)) is None   # a read made no store


def test_the_queue_is_the_stores_own_list_with_open_first_and_cleared_newest_first(world) -> None:
    store = EnvelopeStore(world["qdir"])
    a, b, c = _item(store, 1), _item(store, 2, "end", authority_relevant=True), _item(store, 3, "know")
    review.clear_item(store, a.item_id)
    review.clear_item(store, c.item_id)
    rv = coord_for(world).coord_listing()["coord_review"]
    want_open = [i.item_id for i in review.list_items(store)]
    assert [i["item_id"] for i in rv["open"]] == want_open == [b.item_id]
    assert rv["open_total"] == 1
    assert rv["open"][0]["feed"] == "end" and rv["open"][0]["authority_relevant"] is True
    assert rv["open"][0]["state"] == "open" and rv["open"][0]["cleared_at"] is None
    assert [i["item_id"] for i in rv["cleared"]] == [c.item_id, a.item_id]       # newest clear first
    assert all(i["state"] == "cleared" and i["cleared_at"] for i in rv["cleared"])
    assert rv["now"] and rv["open"][0]["at"] <= rv["now"]


def test_a_reopened_item_is_open_again(world) -> None:
    store = EnvelopeStore(world["qdir"])
    a = _item(store, 1)
    review.clear_item(store, a.item_id)
    review.reopen_item(store, a.item_id)
    rv = coord_for(world).coord_listing()["coord_review"]
    assert [i["item_id"] for i in rv["open"]] == [a.item_id] and rv["cleared"] == []


def test_the_lists_are_capped_and_the_open_total_is_the_whole(world) -> None:
    store = EnvelopeStore(world["qdir"])
    for n in range(REVIEW_OPEN_SHOWN + 3):
        _item(store, n)
    for n in range(100, 100 + REVIEW_CLEARED_SHOWN + 2):
        review.clear_item(store, _item(store, n).item_id)
    rv = coord_for(world).coord_listing()["coord_review"]
    assert len(rv["open"]) == REVIEW_OPEN_SHOWN and rv["open_total"] == REVIEW_OPEN_SHOWN + 3
    assert len(rv["cleared"]) == REVIEW_CLEARED_SHOWN


def test_item_text_is_display_safe(world) -> None:
    store = EnvelopeStore(world["qdir"])
    _item(store, 1, "know", text="a‮evil\u0007 text")
    shown = coord_for(world).coord_listing()["coord_review"]["open"][0]["text"]
    assert "‮" not in shown and "\u0007" not in shown and "U+202E" in shown


def test_the_morning_card_is_the_cards_own_data_and_render(world) -> None:
    store = EnvelopeStore(world["qdir"])
    _item(store, 1)
    _item(store, 2, "end", authority_relevant=True)
    # a SIGN record whose request is not in the queue: the card reports it unreadable, and the phone repeats that
    review.push_sign(store, None, "np-not-in-the-queue", "sign the thing", rec=review.NO_REC)
    got = coord_for(world).coord_listing()["coord_morning"]
    card = morning.card(store)
    waiting = card["waiting"]
    assert got["waiting"] == sum(len(v) for v in waiting["threads"].values()) + len(waiting["loose"])
    assert got["waiting_unreadable"] == len(waiting["unresolved"]) == 1
    assert got["review_open"] == len(card["review"]) == 2
    assert got["lines"][1:] == morning.render(card).split("\n")[1:]        # line 0 is the card's own clock
    assert got["away"] == [] and got["away_unknown"] == []


def test_a_queue_or_card_that_cannot_be_read_is_its_own_note_and_never_an_empty_panel(world, monkeypatch) -> None:
    store = EnvelopeStore(world["qdir"])
    _item(store, 1)

    def boom(*_a, **_k):
        raise RuntimeError("unreadable on purpose")

    monkeypatch.setattr(review, "queue_page", boom)
    out = coord_for(world).coord_listing()
    assert out["coord_review"] is None and "unreadable on purpose" in out["coord_review_note"]
    # the queue is read once and the card is made from that read, so it is a note too; the drops and the store identity still answer
    assert out["coord_morning"] is None and "unreadable on purpose" in out["coord_morning_note"]
    listed = coord_for(world).listing()
    assert listed["coord_store_read"] is True and listed["coord_store_id"] == store.store_id()


def test_the_phone_port_writes_the_queue_only_through_its_device_route() -> None:
    """The one review write on the phone port is ``coord_review`` (a paired device: c4mut,
    tests/test_review_clear_paths.py), and it is only a transport: the clear and the reopen are the cockpit verb
    kernel's (``levain.cockpit.verbs``, through ``PhoneApp.fire``: c4mut fix r1 F1). Neither the phone port, the phone
    app nor the coordinator routes' own module calls the store's transitions."""
    import inspect

    from levain import phone_app, phone_server
    from levain.cockpit import verbs
    from levain.coordinator import phone

    for mod in (phone, phone_server, phone_app):
        src = Path(mod.__file__).read_text(encoding="utf-8")
        assert "clear_item" not in src and "reopen_item" not in src, mod.__name__
    assert "fire(" in inspect.getsource(phone_server._PhoneServer.coord_review)
    assert "plain_only=True" in inspect.getsource(verbs._review_fire)


# --- L3 r1 fixes (c4c-fix) -----------------------------------------------------------------------------------------


def test_the_consent_poll_carries_no_coordinator_view_and_the_queue_is_read_on_its_own_route(world) -> None:
    """Poll cost (codex HIGH): /consent.json is what it was; /coord.json is asked for only while the tab is shown."""
    store = EnvelopeStore(world["qdir"])
    _item(store, 1)
    dev = device_token(world["qdir"])
    with _serve(world["qdir"], world["qdir"]) as (base, _):
        import json
        import urllib.error
        import urllib.request

        def get(route: str, token: str | None):
            req = urllib.request.Request(base + route, headers={"X-Levain-Phone-Token": token} if token else {})
            try:
                with urllib.request.urlopen(req, timeout=10) as r:
                    return r.status, json.loads(r.read())
            except urllib.error.HTTPError as exc:
                return exc.code, json.loads(exc.read() or b"{}")

        status, consent = get("/consent.json", dev)
        assert status == 200 and not [k for k in consent if k.startswith(("coord_review", "coord_morning"))], list(consent)
        status, coord = get("/coord.json", dev)
        assert status == 200 and coord["coord_review"]["open_total"] == 1 and coord["coord_morning"]["review_open"] == 1
        assert get("/coord.json", None)[0] == 403 and get("/coord.json", "not-a-token")[0] == 403


def test_the_listing_does_not_read_the_queue(world, monkeypatch) -> None:
    store = EnvelopeStore(world["qdir"])
    _item(store, 1)

    reads: list[str] = []
    monkeypatch.setattr(review, "list_items", lambda *_a, **_k: reads.append("list_items") or [])
    monkeypatch.setattr(morning, "card", lambda *_a, **_k: reads.append("card") or {})
    real, probed = EnvelopeStore.store_id, []
    monkeypatch.setattr(EnvelopeStore, "store_id", lambda self: probed.append(1) or real(self))
    listed = coord_for(world).listing()
    assert listed["coord_store_read"] is True
    assert reads == []
    assert probed == [1] and listed["coord_store_id"] == real(store)    # the identity probe ran once, on the real store


def test_one_response_reads_the_queue_once_and_the_card_agrees_with_it(world, monkeypatch) -> None:
    """One snapshot (codex 2): an item cleared between two reads must not make the response disagree with itself."""
    store = EnvelopeStore(world["qdir"])
    a, _b = _item(store, 1), _item(store, 2)
    real = review.queue_page
    calls: list[int] = []

    def spy(conn, **kw):
        out = real(conn, **kw)
        calls.append(1)
        if len(calls) == 1:
            review.clear_item(store, a.item_id)       # the world moves right after the first read
        return out

    monkeypatch.setattr(review, "queue_page", spy)
    got = coord_for(world).coord_listing()
    assert calls == [1]
    assert got["coord_review"]["open_total"] == got["coord_morning"]["review_open"] == 2
    assert "review queue: 2 open" in "\n".join(got["coord_morning"]["lines"])


def _statements_of(world, monkeypatch) -> tuple[list[str], dict]:
    """Every SQL statement the listing runs on any connection of the coordinator's own opener."""
    from levain.coordinator.store import CoordStore

    seen: list[str] = []
    real = CoordStore.open_readonly

    def traced(self):
        conn = real(self)
        conn.set_trace_callback(seen.append)
        return conn

    monkeypatch.setattr(CoordStore, "open_readonly", traced)
    return seen, coord_for(world).coord_listing()


def test_the_listing_reads_a_bounded_set_of_statements_whatever_the_history(world, monkeypatch) -> None:
    """C4 r1 item 2: a long-lived store keeps every cleared item; the listing's reads do not grow with them."""
    store = EnvelopeStore(world["qdir"])
    for n in range(5):
        review.clear_item(store, _item(store, n).item_id)
    small, _ = _statements_of(world, monkeypatch)
    with store._write() as (conn, now):
        for n in range(5, 2005):
            item, _new = review._insert(conn, now, "seat", {"text": f"old {n}"}, dedupe_key=f"seat:{n}")
            conn.execute("INSERT INTO review_events (item_id, seq, op, mode, by, at) VALUES (?, 1, 'cleared', "
                         "'manual', 'operator', ?)", (item.item_id, now))
    big, got = _statements_of(world, monkeypatch)
    assert len(big) == len(small), (len(small), len(big))
    assert got["coord_review"]["open_total"] == 0 and len(got["coord_review"]["cleared"]) == REVIEW_CLEARED_SHOWN
    # newest clear first: the last one cleared is shown first
    assert got["coord_review"]["cleared"][0]["text"] == "old 2004"


def _card_with(monkeypatch, world, **changes):
    """The phone's morning view over the real card with `changes` laid over it (a section the real store makes hard to
    break on demand): the mapping from the card to the phone's keys is what these hold."""
    real = morning.card

    def card(store, **kw):
        c = real(store, **kw)
        c.update(changes)
        return c

    monkeypatch.setattr(morning, "card", card)
    return coord_for(world).coord_listing()


def _budget(thread: str, *, crossed=(), unreadable=None) -> dict:
    refusal = "refused" if crossed or unreadable else None
    return {"thread_id": thread, "envelope_id": "env-1", "budget": {"tool_calls": 5, "tokens": 5, "hours": 1},
            "used": {"tool_calls": 0, "tokens": 0, "hours": 0}, "uncounted": [], "sign": None,
            "crossed": list(crossed), "unreadable": unreadable, "refusal": refusal}


def test_an_unreadable_undecided_section_is_none_and_never_zero(world, monkeypatch) -> None:
    EnvelopeStore(world["qdir"]).store_id()
    out = _card_with(monkeypatch, world, undecided=[], errors=["undecided: database is locked"])
    assert out["coord_morning"]["undecided"] is None, out
    assert out["coord_morning"]["errors"] == ["undecided: database is locked"]
    ok = _card_with(monkeypatch, world, undecided=[], errors=[])
    assert ok["coord_morning"]["undecided"] == 0          # read and empty is a real zero


def test_an_unreadable_budget_is_counted_apart_from_a_crossed_one(world, monkeypatch) -> None:
    EnvelopeStore(world["qdir"]).store_id()
    budgets = [_budget("t-a", crossed=("tool_calls",)), _budget("t-b", unreadable="no sessions table"), _budget("t-c")]
    got = _card_with(monkeypatch, world, budgets=budgets)["coord_morning"]
    assert (got["budgets"], got["budgets_crossed"], got["budgets_unreadable"]) == (3, 1, 1), got
    only = _card_with(monkeypatch, world, budgets=[_budget("t-b", unreadable="x")])["coord_morning"]
    assert (only["budgets_crossed"], only["budgets_unreadable"]) == (0, 1), only


def test_a_none_away_entry_is_unknown_and_never_blanks_the_card(world, monkeypatch) -> None:
    EnvelopeStore(world["qdir"]).store_id()
    monkeypatch.setattr(morning, "_away", lambda _q: {"claude_code": {"away": True}, "codex": None})
    out = coord_for(world).coord_listing()
    assert out["coord_morning_note"] is None, out
    got = out["coord_morning"]
    assert got["away"] == ["claude_code"] and got["away_unknown"] == ["codex"], got

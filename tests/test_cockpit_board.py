"""C12: the read-only board. Columns derived from C13's list (+ its journal) and the band; every card carries its journal
line; no write route; the route passes the header walk; unreadable sources are reported, never hidden."""
from __future__ import annotations

import http.client
import json
from pathlib import Path

import pytest

from levain.cockpit import board
from levain.consent.questions import QuestionStore
from levain.coordinator import ready
from levain.coordinator.store import CoordStore
from tests.test_cockpit_k2a import TOKEN, _install, _serve
from tests.test_cockpit_questions import OPTIONS, _file
from tests.test_cockpit_questions import lh  # noqa: F401  (fixture)

ORDER = ["backlog", "ready", "running", "waiting", "needs-you", "done"]


def _cards(out):
    return {c["id"]: (col["category"], c) for col in out["columns"] for c in col["cards"]}


@pytest.fixture
def qdir(tmp_path):
    d = tmp_path / "q"
    d.mkdir(mode=0o700)
    return d


@pytest.fixture
def filled(qdir):
    st = CoordStore(qdir)
    tok = st.coord_take()
    kw = dict(repo="levain", who="desk", why="queued it.")
    ready.add(st, tok, "levain:a", title="Land A", to="ready", tier="sonnet", done_line="green", **kw)
    ready.add(st, tok, "levain:b", title="Land B", to="ready", tier="sonnet", done_line="green",
              depends_on=["levain:a"], **kw)
    ready.add(st, tok, "levain:c", title="Later C", to="backlog", **kw)
    ready.move(st, tok, "levain:a", to="running", who="seat-9", why="the seat took it.")
    return qdir


def test_empty_store_is_no_queue_yet_with_six_columns(qdir):
    out = board.build(qdir, QuestionStore(qdir))
    assert [c["category"] for c in out["columns"]] == ORDER and all(c["cards"] == [] for c in out["columns"])
    assert out["queue"] == "none" and out["note"] == board.NO_QUEUE and out["unreadable"] == []
    assert not (qdir / "dispatch.db").exists()            # reading made nothing
    assert board.build(None, None)["queue"] == "none"


def test_columns_are_derived_in_order_from_the_lists_categories(filled):
    out = board.build(filled, QuestionStore(filled))
    by = _cards(out)
    assert by["levain:a"][0] == "running" and by["levain:b"][0] == "backlog" and by["levain:c"][0] == "backlog"
    assert by["levain:b"][1]["held_reason"] == "depends on levain:a"
    assert out["queue"] == "ok" and out["unreadable"] == []


def test_each_card_carries_its_last_journal_line(filled):
    by = _cards(board.build(filled, QuestionStore(filled)))
    a = by["levain:a"][1]["journal"]
    assert (a["who"], a["why"], a["source"], a["to_category"], a["from_category"]) == \
        ("seat-9", "the seat took it.", "move", "running", "ready") and a["iso"]
    c = by["levain:c"][1]["journal"]
    assert (c["who"], c["source"], c["from_category"]) == ("desk", "add", None)
    assert all(card["journal"] is not None for _, card in by.values())


def test_needs_you_is_the_bands_items_and_nothing_else(filled):
    qs = QuestionStore(filled)
    _file(qs)
    out = board.build(filled, qs)
    col = next(c for c in out["columns"] if c["category"] == "needs-you")
    assert col["count"] == 1 and col["cards"][0]["band"] is True and "Ship the sweep fix" in col["cards"][0]["title"]
    assert col["cards"][0]["journal"] is None
    assert not any(cat == "needs-you" for cat, c in _cards(out).values() if not c.get("band"))


def test_an_unreadable_store_is_reported_not_empty(qdir):
    (qdir / "dispatch.db").write_bytes(b"not a database at all, sixteen+ bytes")
    out = board.build(qdir, QuestionStore(qdir))
    assert "coordinator" in out["unreadable"] and out["queue"] == "unreadable" and out["note"] != board.NO_QUEUE


def test_an_unreadable_band_is_reported_with_the_hint(filled):
    out = board.build(filled, None)
    assert "band" in out["unreadable"] and "levain band" in out["note"]
    (filled / "questions.db").write_bytes(b"garbage garbage garbage garbage")
    out = board.build(filled, QuestionStore(filled))
    assert "band" in out["unreadable"]


def test_an_unreadable_journal_is_reported_and_cards_stay(filled, monkeypatch):
    def boom(*a, **k):
        raise OSError("gone")
    monkeypatch.setattr(ready, "journal", boom)
    out = board.build(filled, QuestionStore(filled))
    assert "journal" in out["unreadable"] and _cards(out)["levain:a"][1]["journal"] is None


def test_build_writes_nothing(filled):
    before = (filled / "dispatch.db").read_bytes()
    board.build(filled, QuestionStore(filled))
    assert (filled / "dispatch.db").read_bytes() == before


# ------------------------------------------------------------------------------------------ the route
def _raw(base, path, method="GET", token=TOKEN, body=None):
    host, port = base.removeprefix("http://").split(":")
    c = http.client.HTTPConnection(host, int(port), timeout=10)
    h = {"X-Levain-Write-Token": token} if token else {}
    if body is not None:
        h["Content-Type"] = "application/json"
    c.request(method, path, body=body, headers=h)
    r = c.getresponse()
    return r.status, {k.lower(): v for k, v in r.getheaders()}, r.read()


def test_route_serves_the_board_behind_the_token(tmp_path, lh):
    lh.mkdir(parents=True, exist_ok=True, mode=0o700)
    st = CoordStore(lh)
    tok = st.coord_take()
    ready.add(st, tok, "levain:a", repo="levain", title="Land A", to="ready", tier="sonnet", done_line="g",
              who="desk", why="queued it.")
    _file(QuestionStore(lh))
    _root, src, _ids = _install(tmp_path)
    with _serve(src, write_token=TOKEN) as (base, _h):
        status, _h1, _b = _raw(base, board.BOARD_PATH, token=None)
        assert status == 403
        status, hdrs, data = _raw(base, board.BOARD_PATH)
        assert status == 200 and hdrs["cache-control"] == "private, no-store"
        body = json.loads(data)
        by = _cards(body)
        assert by["levain:a"][0] == "ready" and by["levain:a"][1]["journal"]["why"] == "queued it."
        assert sum(c["count"] for c in body["columns"] if c["category"] == "needs-you") == 1


def test_route_with_no_store_is_200_no_queue_and_creates_nothing(tmp_path, lh):
    _root, src, _ids = _install(tmp_path)
    with _serve(src, write_token=TOKEN) as (base, _h):
        status, _hd, data = _raw(base, board.BOARD_PATH)
    assert status == 200 and json.loads(data)["queue"] == "none"
    assert not (lh / "dispatch.db").exists()


@pytest.mark.parametrize("method", ["POST", "PUT", "DELETE", "PATCH"])
def test_no_write_reaches_the_board(tmp_path, lh, method):
    _root, src, _ids = _install(tmp_path)
    with _serve(src, write_token=TOKEN) as (base, _h):
        status, _hd, data = _raw(base, board.BOARD_PATH, method=method, body=b"{}")
    assert status in (404, 405, 422, 501) or 400 <= status < 500      # refused (an unhandled verb is 501), never a 200/500
    assert b'"columns"' not in data


def test_the_view_script_is_app_shell_with_no_edit_controls(tmp_path):
    _root, src, _ids = _install(tmp_path)
    with _serve(src, write_token=TOKEN) as (base, _h):
        status, hdrs, data = _raw(base, "/board_view.js", token=None)
        _s, _h2, page = _raw(base, "/", token=None)
    assert status == 200 and hdrs["content-type"].startswith("text/javascript")
    assert b'<script src="/board_view.js"></script>' in page
    js = data.decode()
    for banned in ("draggable", "dragstart", "contenteditable", "innerHTML"):
        assert banned not in js
    assert "/cockpit/coord/drop" in js and "outbox_id" in js
    assert js.count('method: "POST"') == 1

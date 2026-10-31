"""C12: the read-only board. Columns derived from C13's list (+ its journal) and the band; every card carries its journal
line; no write route; the route passes the header walk; unreadable sources are reported, never hidden."""
from __future__ import annotations

import http.client
import json
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

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


def test_no_question_store_is_silent_not_unreadable(filled, tmp_path):
    out = board.build(filled, None)
    assert "band" not in out["unreadable"] and "levain band" not in (out["note"] or "")
    out = board.build(tmp_path / "empty", None)
    assert out["queue"] == "none" and out["note"] == board.NO_QUEUE and out["unreadable"] == []


def test_an_unreadable_band_is_reported_with_the_hint(filled):
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


def _snapshot(d):
    return sorted((str(p.relative_to(d)), p.stat().st_mtime_ns, p.read_bytes() if p.is_file() else None) for p in d.rglob("*"))


def test_route_with_no_store_is_200_no_queue_and_creates_nothing(tmp_path, lh):
    lh.mkdir(parents=True, exist_ok=True, mode=0o700)
    (lh / "marker").write_text("x")
    before = _snapshot(lh)
    _root, src, _ids = _install(tmp_path)
    with _serve(src, write_token=TOKEN) as (base, _h):
        status, _hd, data = _raw(base, board.BOARD_PATH)
        assert status == 200 and json.loads(data)["queue"] == "none"
        assert _snapshot(lh) == before and not (lh / "dispatch.db").exists()
        # control: the server really reads `lh` (a store made there is seen), so the assertion above is not vacuous
        st = CoordStore(lh)
        ready.add(st, st.coord_take(), "levain:a", repo="levain", title="A", to="backlog", who="desk", why="w.")
        assert json.loads(_raw(base, board.BOARD_PATH)[2])["queue"] == "ok"


@pytest.mark.parametrize("method", ["POST", "PUT", "DELETE", "PATCH"])
def test_no_write_reaches_the_board(tmp_path, lh, method):
    lh.mkdir(parents=True, exist_ok=True, mode=0o700)
    st = CoordStore(lh)
    ready.add(st, st.coord_take(), "levain:a", repo="levain", title="A", to="backlog", who="desk", why="w.")
    before = _snapshot(lh)
    _root, src, _ids = _install(tmp_path)
    with _serve(src, write_token=TOKEN) as (base, _h):
        status, _hd, data = _raw(base, board.BOARD_PATH, method=method, body=b"{}")
    assert _snapshot(lh) == before
    assert status in (404, 405, 422, 501) or 400 <= status < 500      # refused (an unhandled verb is 501), never a 200/500
    assert b'"columns"' not in data


def test_the_view_script_is_served_as_app_shell(tmp_path):
    _root, src, _ids = _install(tmp_path)
    with _serve(src, write_token=TOKEN) as (base, _h):
        status, hdrs, data = _raw(base, "/board_view.js", token=None)
        _s, _h2, page = _raw(base, "/", token=None)
    assert status == 200 and hdrs["content-type"].startswith("text/javascript")
    assert b'<script src="/board_view.js"></script>' in page
    assert b"innerHTML" not in data and b"draggable" not in data


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_the_tab_lists_cards_grouped_under_column_headings_and_the_modal_draws_the_columns():
    """One model feeds both: `renderGrouped` (the Board tab) draws one `grp-head` per column with its count and the cards as
    rows, no `bd-cols` body; `renderBoard` (the modal) still draws the six `bd-col`."""
    g = _harness_run()["grouped"]
    assert g["heads"] == ["backlog 1", "ready 0", "running 1", "waiting 0", "needs-you 1", "done 0"]
    assert g["rowIds"] == ["levain:c", "levain:a", "q-1"]
    assert g["groupedColsBody"] == 0                                              # no columns body in the panel
    assert g["boardCols"] == ORDER and g["boardColsBody"] == 1                    # the modal's renderer is unchanged
    assert any("held: depends on levain:a" in t and "levain · sonnet · levain:c" in t for t in g["rowText"])
    assert any("seat-9" in t and "ready → running" in t for t in g["rowText"])    # a card's journal line
    assert any(t.startswith("3. Ship the sweep fix") for t in g["rowText"])       # the band card's number leads
    note = "This column is the Needs you band. Answer there (`levain band`)."     # ONE sentence, both renderers draw the same one
    assert g["glossGrouped"] == [note] and g["glossBoard"] == [note]


def _harness_run():
    root = Path(__file__).resolve().parent.parent
    run = subprocess.run(["node", str(root / "tests" / "js_board_view_harness.js"),
                          str(root / "levain" / "templates" / "web" / "board_view.js")],
                         capture_output=True, text=True, timeout=60, check=True)
    return json.loads(run.stdout)


def test_a_card_field_is_made_display_safe_and_bounded_at_the_source_so_the_kernel_validator_never_refuses_the_board():
    """L3 r2 (complement MED). A projected row's title or a long done line carries a bidi override or runs past the kernel's
    limit: `_card` shows the character marked and cuts the text, and the board value the kernel then checks is accepted."""
    from levain.cockpit import engine
    assert board.TEXT_MAX == engine.BOARD_TEXT_MAX
    card = board._card({"id": "np-1", "repo": "flow", "title": "re‮gnihtemos", "done_line": "x" * (board.TEXT_MAX + 50),
                        "status_category": "ready"}, None)
    assert "‮" not in card["title"] and "<U+202E>" in card["title"].replace("⟨", "<").replace("⟩", ">")
    assert len(card["done_line"]) == board.TEXT_MAX and card["repo"] == "flow" and card["id"] == "np-1"
    spec = SimpleNamespace(id="b", kind="board")
    value = {"schema": 1, "generated": "2026-10-29T20:00:00-04:00", "queue": "ok", "unreadable": [], "note": None,
             "columns": [{"category": c, "count": 1 if c == "ready" else 0, "cards": [card] if c == "ready" else []}
                         for c in board.CATEGORIES]}
    assert engine._v_board(spec, value) is value


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_the_filter_matches_folded_text_with_every_term_and_counts_cards_and_a_card_with_no_journal_says_nothing():
    """The filter narrows cards (never columns) by case- and accent-folded text, every term must hit; its count is the board's
    size, or "k of n" under a query; a blank query is no query; a card with no journal draws no journal line."""
    f = _harness_run()["filtering"]
    assert f["none"] == {"label": "3 cards", "ids": ["levain:c", "levain:a", "q-1"], "cols": 6, "same": True}
    assert f["blank"]["label"] == "3 cards" and f["blank"]["same"] is True
    assert f["folded"]["ids"] == ["levain:c"] and f["folded"]["label"] == "1 of 3" and f["folded"]["cols"] == 6
    assert f["journalWords"]["ids"] == ["levain:a"]                   # a card's journal words are searchable
    assert f["and"]["ids"] == ["levain:a"]                             # every term must hit: "land" AND "levain"
    assert f["miss"]["ids"] == [] and f["miss"]["label"] == "0 of 3" and f["miss"]["cols"] == 6
    assert f["band"]["ids"] == ["q-1"]                                 # a Needs-you band card is filtered like any other
    assert f["noJournal"] == "" and f["journal"].startswith("as of read: desk")
    g = f["folded2"]
    assert g["marks"] == 1 and g["deps"] == ["x", "5"] and g["label"] == 1 and g["after"] == 1, g
    assert g["colKeys"] == ["cards", "category"], g                    # a filtered column keeps every key the model gave it


def test_the_board_value_carries_no_tell_field_and_the_page_has_no_ready_queue_zone(tmp_path, lh):
    """One board per cockpit: the board's read no longer offers a second messaging box, and the script that painted the
    standalone zone is gone (what is left is the painters the board kind uses)."""
    lh.mkdir(parents=True, exist_ok=True, mode=0o700)
    _root, src, _ids = _install(tmp_path)
    with _serve(src, write_token=TOKEN) as (base, _h):
        body = json.loads(_raw(base, board.BOARD_PATH)[2])
        js = _raw(base, "/board_view.js", token=None)[2].decode()
    assert "tell" not in body
    for gone in ("ready-board", "Ready queue", "bd-tell", "mountTell", "/cockpit/coord/drop", "bd-zone"):
        assert gone not in js, gone


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_every_card_has_a_drop_button_that_asks_the_cockpits_box_for_its_words():
    """`Drop about this card` is drawn on every card of both bodies when the cockpit offers its drop box, hands that box
    `About card <id> (<title>): ` on a click, and reads or posts nothing itself; a page with no such box draws none."""
    d = _harness_run()["drop"]
    assert d["grouped"] == [["levain:c", "Drop about this card", "button"], ["levain:a", "Drop about this card", "button"],
                            ["q-1", "Drop about this card", "button"]] and d["cols"] == 3
    assert d["asked"] == ["About card levain:a (Land A): ", "About card q-1 (Ship the sweep fix): "]
    assert d["fetched"] == 0 and d["withoutCockpit"] == 0 and d["words"] == "About card x:1 (T): "


def test_the_panels_read_of_the_board_carries_no_clock_stamp_so_its_etag_moves_only_with_the_work(filled):
    """The board.json route stamps the read; the Work board panel must not: a changing stamp would change the panel's etag on
    every read and announce a change on the change stream while nothing had moved."""
    from levain.web_server import _board_value
    server = SimpleNamespace(consent_reader=SimpleNamespace(directory=filled), question_store=None)
    assert _board_value(server)["generated"]                                  # the route's read is stamped
    a, b = _board_value(server, stamped=False), _board_value(server, stamped=False)
    assert a["generated"] is None and a == b                                  # the panel's is not, and is stable
    assert sum(len(c["cards"]) for c in a["columns"]) == 3                    # and it still holds the work

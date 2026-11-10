"""C12: the read-only board. Columns derived from C13's list (+ its journal) and the band; every card carries its journal
line; no write route; the route passes the header walk; unreadable sources are reported, never hidden."""
from __future__ import annotations

import http.client
import json
import shutil
import subprocess
import warnings
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


def _sql(qdir, stmt):
    import sqlite3
    c = sqlite3.connect(str(qdir / "dispatch.db"))
    c.execute(stmt)
    c.commit()
    c.close()


_JOURNAL_V2 = ("INSERT INTO ready_journal (id, ver, gseq, ts, iso, at, repo, from_status, to_status, from_category, "
               "to_category, who, why, source, applied_by, lease_token) VALUES ('levain:c', 2, 99999, 0, 'i', 'a', "
               "'levain', 'backlog', 'running', 'backlog', 'running', 'sql', 'ahead', 'move', NULL, 1)")


def test_the_cards_and_their_journal_lines_come_from_one_read_transaction(filled, monkeypatch):
    """The snapshot is ONE read transaction: a journal record committed by another connection AFTER the items were read, inside
    the same snapshot, is not seen (a per-statement autocommit read would see it and attach a line newer than the column)."""
    st = CoordStore(filled)
    real = ready._lifecycle_present
    fired = []
    calls = []

    def late_commit(conn):
        assert conn.in_transaction
        calls.append(1)
        if len(calls) == 3 and not fired:          # _current, _diverged, then the journal read: the items are already read
            fired.append(1)
            ready.move(st, st.coord_take(), "levain:c", to="ready", who="late", why="after the items were read.")
        return real(conn)

    monkeypatch.setattr(ready, "_lifecycle_present", late_commit)
    out = board.build(filled, QuestionStore(filled))
    assert fired and out["unreadable"] == []
    col, card = _cards(out)["levain:c"]
    assert col == "backlog" and card["journal"]["who"] == "desk"        # the add's line, not the later move
    col, card = _cards(out)["levain:a"]
    assert col == "running" and card["journal"]["who"] == "seat-9" and card["journal"]["to_category"] == "running"


def test_a_live_launch_is_a_running_seat_card_with_no_journal_line_and_an_ended_one_is_not(filled):
    """SCHEMA section 5: one `running` projection per live seat (`seat:<token>`), read-only, never writable. A worker is
    running while active; a coordinator with no live lease of its own is `waiting`, never `running`; no repo is guessed."""
    st = CoordStore(filled)
    tok = st.coord_take()
    from levain.coordinator.store import Part

    drop = st.accept_drop("fix the build", surface="cli")[0]
    thread = st.split(drop.drop_id, [Part("work", "fix the build")])[0]
    live = st.launch_request(tok, harness="claude", role="worker", threads=[thread.thread_id], cwd=str(filled / "levain-wt"))
    assert st.launch_activate(live.launch_id, harness_session_id="sess-live")[0]
    stale = st.launch_request(tok, harness="claude", role="coordinator", cwd=str(filled))
    assert st.launch_activate(stale.launch_id, harness_session_id="sess-stale")[0]
    out = board.build(filled, QuestionStore(filled))
    col, card = _cards(out)[f"seat:{live.launch_id}"]
    assert col == "running" and card["journal"] is None and card["repo"] == "" and "claude worker" in card["title"]
    col, card = _cards(out)[f"seat:{stale.launch_id}"]
    assert col == "waiting" and "no live lease" in card["title"]
    assert out["unreadable"] == []
    with pytest.raises(Exception):
        ready.move(st, tok, f"seat:{live.launch_id}", to="done", who="desk", why="no.")     # a projection is never writable


def test_a_journal_record_ahead_of_its_state_row_is_not_attached_to_the_card(filled):
    _sql(filled, _JOURNAL_V2)
    out = board.build(filled, QuestionStore(filled))
    col, card = _cards(out)["levain:c"]
    assert col == "backlog" and card["journal"]["who"] == "desk" and card["journal"]["to_category"] == "backlog"
    assert "levain:c" in str(out["unreadable"])


def test_an_unreadable_journal_is_named_and_the_cards_stay(filled, monkeypatch):
    real = ready._lifecycle_present
    calls = []

    def flaky(conn):
        calls.append(1)
        if len(calls) == 3:          # _current, _diverged, then the journal read
            raise ready.CoordError("the journal is damaged")
        return real(conn)

    monkeypatch.setattr(ready, "_lifecycle_present", flaky)
    out = board.build(filled, QuestionStore(filled))
    assert out["queue"] == "ok" and "journal" in out["unreadable"] and "coordinator" not in out["unreadable"]
    col, card = _cards(out)["levain:a"]
    assert col == "running" and card["journal"] is None


def test_a_damaged_journal_that_breaks_the_divergence_read_still_keeps_the_cards(filled, monkeypatch):
    def broken(conn, item_id=None):
        raise ready.CoordError("the journal has no ver column")

    monkeypatch.setattr(ready, "_diverged", broken)
    out = board.build(filled, QuestionStore(filled))
    assert out["queue"] == "ok" and "journal" in out["unreadable"] and "coordinator" not in out["unreadable"]
    col, card = _cards(out)["levain:a"]
    assert col == "running" and card["journal"] is None


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
    import hashlib   # the page names the script by the digest of the bytes served (1105-17)
    assert b'<script src="/board_view.js?v=%s"></script>' % hashlib.sha256(data).hexdigest()[:12].encode() in page
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


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_the_needs_you_column_leaves_out_a_desk_owed_row_the_strip_leaves_out():
    """The strip's rule (a desk row counts only when carried over to him) is the column's: the fresh desk row is out, a carried
    one, his own and a row with no section stay. The python half of the same fixture is test_needs_count_desk_owed."""
    got = _harness_run()["deskOwed"]
    assert got["ids"] == ["band:old-desk", "band:phill", "band:plain"], got
    assert got["rows"] == got["ids"] and "needs-you 3" in got["heads"], got


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


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_an_empty_column_of_an_unreadable_source_says_unknown_not_nothing():
    """W2 (read in a browser): beside the "queue could not be read" banner six columns said "nothing", which reads as an empty,
    healthy board. A column whose source could not be read says so; a read source says "nothing"."""
    e = _harness_run()["emptyWords"]
    nothing, unknown = "nothing", "unknown (could not read)"
    assert e["ok"]["grouped"] == e["ok"]["cols"] == [nothing] * 6
    assert e["queue"]["grouped"] == e["queue"]["cols"] == [unknown] * 4 + [nothing, unknown]   # the queue feeds every column but needs-you
    assert e["band"]["grouped"] == e["band"]["cols"] == [nothing] * 4 + [unknown, nothing]   # the band is needs-you's alone
    # the seat roster feeds running and waiting only; a queue state that is not ok/none, with no unreadable key at all, fails closed
    assert e["seats"]["grouped"] == e["seats"]["cols"] == [nothing, nothing, unknown, unknown, nothing, nothing]
    assert e["noKey"]["grouped"] == e["noKey"]["cols"] == [unknown] * 4 + [nothing, unknown]


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


# --- the drop route and the board: one coordinator -----------------------------------------------------

def test_drop_here_is_true_only_when_the_drop_route_files_to_the_directory_the_board_reads(filled, tmp_path):
    from levain.web_server import _board_value
    other = tmp_path / "other-queue"
    other.mkdir()

    def value(sheet_dir, **kw):
        sheet = SimpleNamespace(queue_dir=sheet_dir) if sheet_dir is not None else None
        server = SimpleNamespace(consent_reader=SimpleNamespace(directory=filled), question_store=None, coord_sheet=sheet)
        return _board_value(server, **kw)["drop_here"]

    assert value(filled) is True
    assert value(str(filled) + "/../" + filled.name) is True       # the same directory spelled another way
    assert value(other) is False                                   # a different queue: the drop would file elsewhere
    assert value(None) is False                                    # no drop route at all
    assert value(filled, stamped=False) is True                    # the panel's read says it too
    bare = SimpleNamespace(consent_reader=SimpleNamespace(), question_store=None, coord_sheet=SimpleNamespace(queue_dir=filled))
    assert _board_value(bare)["drop_here"] is False                # no board directory to compare


def test_the_board_route_carries_drop_here(tmp_path, lh):
    lh.mkdir(parents=True, exist_ok=True, mode=0o700)
    _root, src, _ids = _install(tmp_path)
    with _serve(src, write_token=TOKEN) as (base, httpd):
        assert json.loads(_raw(base, board.BOARD_PATH)[2])["drop_here"] is False       # no coord sheet here
        httpd.coord_sheet = SimpleNamespace(queue_dir=lh, close=lambda: None)
        assert json.loads(_raw(base, board.BOARD_PATH)[2])["drop_here"] is True
        httpd.coord_sheet = SimpleNamespace(queue_dir=tmp_path / "elsewhere", close=lambda: None)
        assert json.loads(_raw(base, board.BOARD_PATH)[2])["drop_here"] is False


def test_build_still_accepts_the_removed_drop_dir_and_ignores_it_with_a_warning(qdir):
    plain = board.build(qdir, generated="t")
    with pytest.warns(DeprecationWarning, match="drop_dir"):
        got = board.build(qdir, generated="t", drop_dir=qdir)
    assert got == plain and "tell" not in got
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        board.build(qdir, generated="t")                           # no warning without it


def _panel_kinds(base):
    got = json.loads(_raw(base, "/cockpit/manifest.json")[2])
    return {pid: m["kind"] for pid, m in got["panels"].items()}


def test_make_server_draws_the_work_board_unless_told_not_to(tmp_path, lh):
    lh.mkdir(parents=True, exist_ok=True, mode=0o700)
    _root, src, _ids = _install(tmp_path)
    with _serve(src, write_token=TOKEN) as (base, _h):
        assert _panel_kinds(base).get("work_board") == "board"
    with _serve(src, write_token=TOKEN, work_board=False) as (base, _h):
        assert [p for p, k in _panel_kinds(base).items() if k == "board"] == []


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_no_drop_button_is_drawn_when_the_board_says_the_drop_files_elsewhere():
    d = _harness_run()["dropHere"]
    assert d == {"absent": 3, "true": 3, "false": 0, "falseCols": 0}


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_a_1000_character_title_cannot_push_a_cards_drop_past_the_drop_routes_cap():
    from levain.coordinator.store import WORDS_MAX, check_words
    words = _harness_run()["longTitleWords"]
    assert len(words) >= 1000 and len(words) <= 2 * board.TEXT_MAX + 100
    assert check_words(words) == words and len(words) < WORDS_MAX


def test_a_snoozed_question_is_one_later_card_not_an_actionable_one(filled):
    """Row 131: ONE predicate (band_view.later_of) marks a snoozed question LATER on the board card, with the payload."""
    from datetime import datetime, timedelta

    from levain import band_view
    from levain.consent.questions import local_today
    qs = QuestionStore(filled)
    _file(qs, what="Due question")
    snz = _file(qs, what="Snoozed question", agent_session_id="sess-2")
    day = (datetime.fromisoformat(local_today(datetime.now().astimezone())) + timedelta(days=2)).date().isoformat()
    qs.snooze(snz.id, until=day, channel="cli", via="cli")
    payload = band_view.band_payload(qs, "test")                 # numbers the rows (the board read numbers nothing)
    out = board.build(filled, qs)
    col = next(c for c in out["columns"] if c["category"] == "needs-you")
    by = {c["title"]: c for c in col["cards"]}
    assert "later" not in by["Due question"]
    lat = by["Snoozed question"]
    assert lat["later"] == day and lat["later_label"].endswith(day[5:]) and lat["blocks"] == ""
    assert lat["band"] is True                                   # still an open, numbered band card (answerable by number)
    item = next(i for i in payload["items"] if i["what"] == "Snoozed question")
    assert item["later"] == day and item["later_label"] == lat["later_label"] and item["options"] == []


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_later_cards_keep_their_drop_and_are_recognised_only_as_band_cards_with_a_real_day():
    """L3 later round: a LATER card keeps 'Drop about this card' in both bodies; `later` is read only for band === true with
    an ISO day; the label is bounded; the filter finds `later` and the day."""
    r = _harness_run()["laterCards"]
    assert r["later"] == ["2026-11-09", "", "", "2026-11-09"]
    assert r["labelLen"] == 80
    assert r["groupedDrops"] == ["band:a", "band:b", "band:c", "band:d"] and r["colDrops"] == r["groupedDrops"]
    assert r["found"] == 2


def test_a_malformed_snooze_is_due_on_every_surface_and_a_foreign_row_agrees_with_the_board(filled):
    """L3 later round: one fail-open path (`_later_pair`); a foreign store's future snooze is LATER in the payload too."""
    from datetime import datetime, timedelta
    from types import SimpleNamespace

    from levain import band_view
    from levain.consent.questions import local_today
    q = SimpleNamespace(snoozed_until=object(), dated=None)
    assert band_view._later_pair(q, 1, "2026-11-07") is None
    day = (datetime.fromisoformat(local_today(datetime.now().astimezone())) + timedelta(days=3)).date().isoformat()
    ok = SimpleNamespace(snoozed_until=day, dated=None)
    assert band_view._later_pair(ok, 4, local_today(datetime.now().astimezone())) == band_view.later_of(
        SimpleNamespace(), ok, 4)
    qs = QuestionStore(filled)
    _file(qs, what="Bad", agent_session_id="s1")
    item = band_view._question_item(next(iter(band_view._read(qs)[0]))[1], 1, None, True, "2026-11-07")[1]
    assert "later" not in item

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


def test_the_tell_box_is_offered_only_when_the_drop_writes_where_the_board_reads(tmp_path, qdir):
    other = tmp_path / "other"
    other.mkdir()
    assert board.build(qdir, None, drop_dir=qdir)["tell"]["offered"] is True
    off = board.build(qdir, None, drop_dir=other)["tell"]
    assert off["offered"] is False and "different queue" in off["why"]
    assert board.build(qdir, None)["tell"]["offered"] is False
    assert board.build(None, None, drop_dir=qdir)["tell"]["offered"] is False


def test_route_pins_the_tell_to_the_board_directory(tmp_path, lh):
    lh.mkdir(parents=True, exist_ok=True, mode=0o700)
    other = tmp_path / "other"
    other.mkdir()
    _root, src, _ids = _install(tmp_path)
    with _serve(src, write_token=TOKEN) as (base, httpd):
        assert json.loads(_raw(base, board.BOARD_PATH)[2])["tell"]["offered"] is False       # no drop route here
        httpd.coord_sheet = SimpleNamespace(queue_dir=other, close=lambda: None)
        assert json.loads(_raw(base, board.BOARD_PATH)[2])["tell"]["offered"] is False       # a different queue
        httpd.coord_sheet = SimpleNamespace(queue_dir=lh, close=lambda: None)
        assert json.loads(_raw(base, board.BOARD_PATH)[2])["tell"]["offered"] is True        # the same one


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
def test_the_tell_box_behaves_under_node():
    """The box's behaviour, run: only the drop endpoint is posted to, and only on a click that passes every gate."""
    root = Path(__file__).resolve().parent.parent
    run = subprocess.run(["node", str(root / "tests" / "js_board_tell_harness.js"),
                          str(root / "levain" / "templates" / "web" / "board_view.js")],
                         capture_output=True, text=True, timeout=60, check=True)
    out = json.loads(run.stdout)
    assert out["not_offered"]["posts"] == [] and out["not_offered"]["reads"] == 0 and out["not_offered"]["wordsHidden"]
    assert out["unreadable"]["posts"] == [] and "store locked" in out["unreadable"]["say"]      # unreadable is not "no store"
    assert len(out["double_click"]["posts"]) == 1 and out["double_click"]["reads"] == 1          # locked before the await
    assert len(out["no_store_yet"]["posts"]) == 1 and out["no_store_yet"]["posts"][0]["store_id"] is None
    edited = out["edited_pending"]["posts"]
    assert len(edited) == 1 and edited[0]["words"] == "B" and edited[0]["outbox_id"] != "ph-old"   # edited words: new id
    same = out["same_pending"]["posts"]
    assert len(same) == 1 and same[0]["outbox_id"] == "ph-old" and out["same_pending"]["reads"] == 0


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_the_tab_lists_cards_grouped_under_column_headings_and_the_modal_draws_the_columns():
    """One model feeds both: `renderGrouped` (the Board tab) draws one `grp-head` per column with its count and the cards as
    rows, no `bd-cols` body; `renderBoard` (the modal) still draws the six `bd-col`. The mounted tab holds the grouped list."""
    g = _tell_run()["grouped"]
    assert g["heads"] == ["backlog 1", "ready 0", "running 1", "waiting 0", "needs-you 1", "done 0"]
    assert g["rowIds"] == ["levain:c", "levain:a", "q-1"]
    assert g["groupedColsBody"] == 0                                              # no columns body in the panel
    assert g["boardCols"] == ORDER and g["boardColsBody"] == 1                    # the modal's renderer is unchanged
    assert any("held: depends on levain:a" in t and "levain · sonnet · levain:c" in t for t in g["rowText"])
    assert any("seat-9" in t and "ready → running" in t for t in g["rowText"])    # a card's journal line
    assert any(t.startswith("3. Ship the sweep fix") for t in g["rowText"])       # the band card's number leads
    assert g["mountedGroupHeads"] == 6 and g["mountedColsBody"] == 0              # the mounted tab is the grouped list
    note = "This column is the Needs you band. Answer there (`levain band`)."     # ONE sentence, both renderers draw the same one
    assert g["glossGrouped"] == [note] and g["glossBoard"] == [note]
    assert g["mountedButtons"] == 1                                               # the tell box's Send is the only button in the host
    assert g["modelBefore"] is None and g["modelAfter"] == ORDER_COUNTS           # the handle's model() is the last pull


ORDER_COUNTS = ["backlog:1", "ready:0", "running:1", "waiting:0", "needs-you:1", "done:0"]


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_the_watchers_belong_to_the_page_and_a_failed_read_tells_them():
    """L3 r1 (session A). The page's watchers and last model live on a bus every mounted view shares: stopping one view drops
    no watcher, and the next view's pull reaches them. A failed read (403, 5xx, network) tells every watcher `(null, true)`
    and clears the model; the next good pull restores it; a stopped view tells nobody; a watcher that throws wipes nothing."""
    b = _tell_run()["bus"]
    cols = ",".join(ORDER_COUNTS)
    assert b["afterFirst"] == [cols]                      # the throwing watcher did not stop the first one hearing it
    assert b["watchersAfterStop"] == 2                    # stop() dropped no watcher
    assert b["afterRemount"] == [cols, cols]              # the new view's pull reached the old subscription
    for bad in ("403", "500", "net"):
        assert b["steps"][bad]["last"] == "FAILED" and b["steps"][bad]["model"] is None and b["steps"][bad]["failed"] is True, b["steps"][bad]
    assert b["steps"]["ok"]["last"] == cols and b["steps"]["ok"]["failed"] is False and b["steps"]["ok"]["heads"] == 6, b["steps"]["ok"]
    assert b["stoppedPullCalls"] == 0
    assert b["stoppedPullSeqMoved"] is False              # L3 r2: a stopped view's pull takes no sequence number


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_a_failed_read_retires_the_send_box_keeps_the_typed_words_and_the_next_good_read_offers_it_again():
    """L3 r1 (codex MED). The Send box is offered only over a board that was just read: a 403, a 5xx or no network hides it with
    the reason, the words already typed stay in the field, and the next good read offers it again."""
    t = _tell_run()["tellFollows"]
    assert t["good"]["hidden"] is False and t["good"]["sendHidden"] is False and t["good"]["gateHidden"] is True
    for bad in ("403", "500", "net"):
        s = t[bad]
        assert s["hidden"] is True and s["sendHidden"] is True and s["gateHidden"] is False, (bad, s)
        assert "the board could not be read just now" in s["gate"], (bad, s)
        assert s["typed"] == "re levain:a: please hold", (bad, s)                      # the operator's words are kept
    assert t["again"]["hidden"] is False and t["again"]["sendHidden"] is False and t["again"]["typed"] == "re levain:a: please hold"


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_a_late_response_never_overwrites_a_newer_one():
    """L3 r1 (complement LOW #1). Two overlapping pulls resolve out of order: the older response (good or failed) is dropped, so a
    late 403/500 does not call `busFail` over a newer good read, and a late good read does not undo a newer failure."""
    late = _tell_run()["late"]
    newer = "backlog:0,ready:1,running:0,waiting:0,needs-you:0,done:0"        # the newer body's one card; the older BODY draws ORDER_COUNTS
    for name in ("lateGood", "lateFail403", "lateFail500"):
        r = late[name]
        assert r["mid"]["model"] == newer and r["mid"]["failed"] is False, (name, r["mid"])
        assert r["end"]["model"] == newer and r["end"]["failed"] is False, (name, r["end"])      # the late one changed nothing
        assert r["end"]["calls"] == r["mid"]["calls"] == [newer], (name, r["end"])             # and told nobody
        assert r["end"]["hidden"] is False, (name, r["end"])                                   # the box stays offered
    g = late["lateGoodAfterFail"]
    assert g["mid"]["failed"] is True and g["end"]["failed"] is True and g["end"]["model"] == "none", g
    assert g["end"]["calls"] == ["FAILED"] and g["end"]["hidden"] is True, g


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_the_token_removed_fails_the_bus_and_its_return_clears_it():
    """L3 r1 (complement LOW #2). Removing the write token fails the page's bus (an open modal says so, once); giving it back
    clears `failed` and tells the watchers `(null, false)` (the modal says it has not read yet, never the stale line); a
    failure that came from a bad READ is not cleared by a sync that merely sees the token."""
    t = _tell_run()["token"]
    assert t["start"]["failed"] is False and t["start"]["calls"] == ["model"]
    assert t["removed"]["failed"] is True and t["removed"]["model"] is None and t["removed"]["calls"] == ["model", "FAILED"]
    assert t["removedAgain"]["calls"] == ["model", "FAILED"]
    assert t["returned"]["failed"] is False and t["returned"]["model"] is None and t["returned"]["calls"] == ["model", "FAILED", "null"]
    assert t["readFailureKept"] == {"failed": True, "calls": []}
    assert t["readFailureSurvivesTokenCycle"] == {"failed": True, "calls": []}      # L3 r2: the token did not cause that failure


def _tell_run():
    root = Path(__file__).resolve().parent.parent
    run = subprocess.run(["node", str(root / "tests" / "js_board_tell_harness.js"),
                          str(root / "levain" / "templates" / "web" / "board_view.js")],
                         capture_output=True, text=True, timeout=60, check=True)
    return json.loads(run.stdout)


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_a_compose_fragment_prefills_the_words_and_posts_nothing_by_itself():
    """`#board&compose=re%20<id>%3A%20` starts the box's words: editable, focused, cursor at the end, nothing posted or read."""
    out = _tell_run()
    p = out["prefill"]
    assert p["pre"]["value"] == "re c-12: " and p["pre"]["focused"] == 1 and p["pre"]["sel"] == [9, 9]
    assert p["posts"] == [] and p["reads"] == 0 and p["pre"]["posts"] == 0 and p["pre"]["reads"] == 0   # no send by itself
    assert out["prefill_plus"]["pre"]["value"] == "re c-12: "          # the address rewritten with "+" for spaces reads the same
    e = out["prefill_edit"]                                             # the operator's edit goes out through the one drop route
    assert len(e["posts"]) == 1 and e["posts"][0]["words"] == "re c-12: do it" and e["posts"][0]["outbox_id"].startswith("ph-")
    assert out["no_hash"]["pre"]["value"] == "" and out["no_hash"]["pre"]["focused"] == 0
    assert out["kept_wins"]["pre"]["value"] == "A"                      # an undelivered earlier drop is never overwritten


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_the_prefill_reads_no_storage_and_no_token():
    """A token beside the fragment is not read specially; the prefill path touches only the box's own pending key."""
    out = _tell_run()
    t = out["prefill_token"]
    assert t["pre"]["value"] == "re x: " and "SECRET123" not in json.dumps({k: v for k, v in t.items() if k not in ("hash", "hashAfterMount")})
    assert t["hash"] == "#write_token=SECRET123"          # the token part of the address is left exactly as it was
    assert set(t["keysRead"]) <= {"levain-board-drop-pending"} and t["posts"] == []


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_a_hostile_fragment_stays_inert_plain_text():
    """Markup is a value, never parsed; a long value is cut; a malformed % and control characters do not throw."""
    out = _tell_run()
    assert out["hostile_markup"]["pre"]["value"] == "<img src=x onerror=alert(1)>" and out["hostile_markup"]["posts"] == []
    assert len(out["hostile_long"]["pre"]["value"]) == 300
    assert isinstance(out["hostile_malformed"]["pre"]["value"], str) and out["hostile_malformed"]["posts"] == []
    assert out["hostile_control"]["pre"]["value"] == "a b c"
    src = (Path(__file__).resolve().parent.parent / "levain" / "templates" / "web" / "board_view.js").read_text()
    assert "innerHTML" not in src


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_the_compose_fragment_is_forgotten_only_when_the_box_is_offered_and_only_that_segment():
    """A hidden box keeps the link in the address; an offered one drops just `compose=...`, byte-identical elsewhere."""
    out = _tell_run()
    assert out["board_fails"]["hash"] == "#board&compose=re%20x%3A%20"          # a 403 board read: the words are not lost
    assert out["not_offered_hash"]["hash"] == "#board&compose=re%20x%3A%20"     # telling off: the link stays for later
    assert out["hash_other_params"]["hash"] == "#board&write_token=a%2Bb+c&z"   # nothing else re-encoded, no `board=`
    assert out["hash_bare_board"]["hash"] == "#board"
    assert out["hash_only_compose"]["hash"] == ""
    assert out["hash_bare_board"]["hashAfterMount"] == "#board"                 # forgotten once offered (the mount's await)
    assert out["kept_wins_hash"]["hash"] == "#board" and out["kept_wins_hash"]["pre"]["value"] == "A"   # beaten by kept words: spent too


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_the_prefill_does_not_steal_focus_and_never_splits_a_surrogate_pair():
    out = _tell_run()
    assert out["focus_elsewhere"]["pre"]["focused"] == 0 and out["focus_elsewhere"]["pre"]["value"] == "re x: "
    assert out["focus_body"]["pre"]["focused"] == 1
    v = out["surrogate"]["pre"]["value"]
    assert v == "a" * 299 + "\U0001F600"          # 300 code points, the emoji whole (301 UTF-16 units)


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_a_compose_link_followed_in_an_open_cockpit_fills_only_an_idle_empty_box():
    out = _tell_run()
    assert out["hashchange_fill"]["pre"]["value"] == "re y: " and out["hashchange_fill"]["hash"] == "#board"
    assert out["hashchange_edited"]["pre"]["value"] == "mine"                   # unsent edits are never overwritten
    assert out["hashchange_kept"]["pre"]["value"] == "A"                        # nor kept words
    assert out["hashchange_fill"]["handlers"] == 1


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
    f = _tell_run()["filtering"]
    assert f["none"] == {"label": "3 cards", "ids": ["levain:c", "levain:a", "q-1"], "cols": 6, "same": True}
    assert f["blank"]["label"] == "3 cards" and f["blank"]["same"] is True
    assert f["folded"]["ids"] == ["levain:c"] and f["folded"]["label"] == "1 of 3" and f["folded"]["cols"] == 6
    assert f["journalWords"]["ids"] == ["levain:a"]                   # a card's journal words are searchable
    assert f["and"]["ids"] == ["levain:a"]                             # every term must hit: "land" AND "levain"
    assert f["miss"]["ids"] == [] and f["miss"]["label"] == "0 of 3" and f["miss"]["cols"] == 6
    assert f["band"]["ids"] == ["q-1"]                                 # a Needs-you band card is filtered like any other
    assert f["noJournal"] == "" and f["journal"].startswith("as of read: desk")

"""``levain board open`` (levain#179): the open items of the derived board, a read verb that stores nothing.

The set is taken from ``board_state.STATE_COLUMN``, so these tests name the constants and never restate a list of states. The
mutation tests run the real verb over a real git world and a scratch store: a close that lands drops an item from the view, an
unsigned band clear does not, a decision to cut does, and the verb writes neither the store nor the repo."""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from levain.cli import main
from levain.desk import board_cli, board_producers
from levain.desk import board_store as bs
from levain.desk.board_state import COLUMNS, STATE_COLUMN, STATE_KIND, STATES
from tests._board_git_world import OLD, World
from tests.test_board_cli import AT, item, write_items

SINCE = "2026-11-12T00:00:00Z"


@pytest.fixture(autouse=True)
def _registry_not_under_test(monkeypatch):
    monkeypatch.setattr(bs, "_check_repo_mode", lambda store_dir, repo, key: None)


@pytest.fixture
def band(monkeypatch) -> list:
    """The native producer, whole and quiet except for the band rows a test appends to the returned list."""
    rows: list = []

    def stub(*, now, store_id, no_land_waiter=False, **_kw):
        out = {"seats": board_producers._block(now, store_id, [], None), "band": board_producers._block(now, store_id, rows, None)}
        if no_land_waiter:
            out["waiters"] = board_producers._block(now, store_id, [], None)
        return out

    monkeypatch.setattr(board_producers, "native_blocks", stub)
    return rows


@pytest.fixture
def world(tmp_path: Path, monkeypatch, band) -> World:
    w = World(tmp_path)
    w.commit("root", date=OLD)
    w.push_main()
    monkeypatch.chdir(w.w)          # a store with no registry reads the repository here
    return w


def open_run(capsys, items: Path, *more: str, verb: str = "open"):
    rc = main(["board", verb, "--items", str(items), "--since", SINCE, "--no-land-waiter", *more])
    out = capsys.readouterr()
    return rc, out.out, out.err


def open_ids(capsys, items: Path, *more: str) -> tuple[int, list[str], dict]:
    rc, out, err = open_run(capsys, items, "--json", *more)
    assert err == "", err
    doc = json.loads(out)
    return rc, [i["id"] for i in doc["items"]], doc


def snapshot(items: Path, world: World) -> list:
    out = [(str(items), items.stat().st_size, items.stat().st_mtime_ns, items.read_bytes())]
    for base, _d, files in os.walk(world.w):
        for f in files:
            p = Path(base) / f
            st = p.stat()
            out.append((str(p), st.st_size, st.st_mtime_ns))
    return sorted(out)


# ------------------------------------------------------------------------------------------------ the open set
def _doc(states=STATES, **extra) -> dict:
    return {"schema": "ready-v3", "as_of": "2026-11-12T20:00:00Z", "audit": [], "problems": [], "unattributed": [],
            "reader": {"repo": "levain", "diverged": []}, "items": [
                {"id": f"levain#{n}", "repo": "levain", "title": f"title of {s}", "tier": None, "state": s, "column": STATE_COLUMN[s],
                 "reason": f"why {s}", "refs": [], "as_of": None, "kind": STATE_KIND[s]} for n, s in enumerate(states, 1)], **extra}


def test_the_open_states_are_every_state_outside_the_off_and_done_columns():
    closed = {s for s, c in STATE_COLUMN.items() if c in board_cli.CLOSED_COLUMNS}
    assert set(board_cli.OPEN_STATES) == set(STATES) - closed
    assert closed and set(board_cli.OPEN_STATES) and not closed & set(board_cli.OPEN_STATES)
    assert set(board_cli.CLOSED_COLUMNS) <= set(COLUMNS)
    assert "unknown" in board_cli.OPEN_STATES                  # fail closed: an item the board cannot read is never "not open"


def test_the_view_holds_exactly_the_open_states_whatever_a_state_is_named():
    doc = _doc()
    view = board_cli.open_view(doc)
    assert {i["state"] for i in view["items"]} == set(board_cli.OPEN_STATES)
    assert all(i in doc["items"] for i in view["items"])                       # the document's own dicts: no key added
    assert set(view) == {"schema", "as_of", "items", "audit", "problems", "reader"}


def test_a_state_this_reader_does_not_know_is_open_and_drawn_with_unknown():
    doc = _doc(("ready", "unknown"))
    doc["items"].append({**doc["items"][0], "id": "levain#9", "state": "from_the_future", "column": "future"})
    view = board_cli.open_view(doc)
    assert [i["state"] for i in view["items"]][:2] != ["ready", "ready"] and {i["id"] for i in view["items"]} == {"levain#1", "levain#2", "levain#9"}
    assert [i["state"] for i in view["items"]][-1] == "ready"


def test_unknown_prints_first_then_the_boards_column_order():
    lines = board_cli.open_lines(_doc(tuple(reversed(STATES))))
    heads = [ln for ln in lines if not ln.startswith("  ")][1:]
    assert heads[0].startswith("unknown")
    columns = list(dict.fromkeys(h.split(" / ")[0].split(":")[0].split(" ")[0] for h in heads))
    order = [c for c in COLUMNS if c in columns]
    assert [c for c in columns if c != "unknown"] == [c for c in order if c != "unknown"]


def test_each_item_line_carries_its_id_title_and_reason_and_the_header_its_counts():
    lines = board_cli.open_lines(_doc())
    n_open = len(board_cli.OPEN_STATES)
    assert lines[0] == f"open: {n_open} of {len(STATES)} items, as of 2026-11-12T20:00:00Z"
    ready = [ln for ln in lines if "title of ready_with_start" in ln]
    assert ready and "why ready_with_start" in ready[0] and ready[0].lstrip().startswith("levain#")
    assert "ready / ready_with_start (start)" in "\n".join(lines) and "ready / landed_not_closed (close)" in "\n".join(lines)


def test_an_item_text_with_control_characters_prints_as_one_clean_line():
    doc = _doc(("ready",))
    doc["items"][0]["title"] = "two\nlines\x1b[31m"
    line = board_cli.open_lines(doc)[2]
    assert "\n" not in line and "\x1b" not in line and "two lines" in line


def test_the_tail_names_landed_not_closed_first_the_problems_and_the_diverged_branches():
    doc = _doc(("ready",), audit=[{"kind": "unknown", "id": "levain#1", "why": "x"},
                                  {"kind": "landed_not_closed", "id": "levain#2", "why": "main holds it"}])
    doc["problems"] = ["line 4: not json"]
    doc["reader"]["diverged"] = ["seat/x"]
    tail = [ln for ln in board_cli.open_lines(doc) if "audit" in ln or "problem" in ln or "diverged" in ln]
    assert tail[0].startswith("  audit landed_not_closed: levain#2: main holds it") and "close it or name what remains" in tail[0]
    assert tail[1].startswith("  audit unknown: levain#1")
    assert tail[2] == "  problem: line 4: not json" and tail[3].startswith("  diverged: seat/x")


def test_a_board_that_read_no_item_and_says_why_is_unreadable_never_an_empty_list():
    doc = _doc(())
    doc["problems"] = ["line 1: not json"]
    lines = board_cli.open_lines(doc)
    assert lines[0] == "open: unreadable: no item of the board could be read" and "  problem: line 1: not json" in lines
    assert board_cli.open_lines(_doc(()))[0] == "open: 0 of 0 items, as of 2026-11-12T20:00:00Z"


def test_a_repo_the_board_holds_no_item_of_is_one_line():
    assert board_cli.open_lines(_doc(("ready",)), "ghost") == ["ghost: the board holds no items for it"]
    assert board_cli.open_lines(_doc(("ready",), audit=[{"kind": "unknown", "id": "levain#1", "why": "x"}]), "ghost") == \
        ["ghost: the board holds no items for it"]


def test_a_repo_filter_keeps_that_repos_items_and_says_the_reader_is_the_whole_boards():
    doc = _doc(("ready", "ready"))
    doc["items"][1]["id"] = "other#1"
    doc["audit"] = [{"kind": "unknown", "id": None, "why": "store-wide"}]
    lines = board_cli.open_lines(doc, "levain")
    assert lines[0].endswith("(levain)") and lines[0].startswith("open: 1 of 1 items")
    assert not any("other#1" in ln for ln in lines) and "  audit (whole board) unknown: -: store-wide" in lines


def test_an_unreadable_board_says_unreadable_and_exits_as_state_does(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("LEVAIN_HOME", str(tmp_path / "lh"))
    monkeypatch.delenv(bs.ITEMS_ENV, raising=False)
    rc_state = main(["board", "state"])
    capsys.readouterr()
    rc = main(["board", "open"])
    out = capsys.readouterr()
    assert rc == rc_state == 2 and out.out.startswith("open: unreadable: ") and "no items store" in out.out
    rc = main(["board", "open", "--json"])
    assert rc == 2 and "no items store" in json.loads(capsys.readouterr().out)["unreadable"]


# ------------------------------------------------------------------------------------------------ the real verb, mutated
def test_the_verb_lists_what_the_board_reads_open_and_a_landing_close_drops_the_item(world, tmp_path, capsys):
    items = write_items(tmp_path, item(1), item(2), item(3))
    rc, ids, doc = open_ids(capsys, items)
    assert rc == 0 and sorted(ids) == ["levain#1", "levain#2", "levain#3"] and doc["audit"] == []
    world.commit("land one", trailers=("Board-Item: levain#1", "Board-Closes: levain#1"))
    world.push_main()
    rc, ids, _ = open_ids(capsys, items)
    assert rc == 0 and sorted(ids) == ["levain#2", "levain#3"]


def test_a_signed_band_clear_drops_its_item_and_an_unsigned_one_keeps_it_as_landed_not_closed(world, tmp_path, capsys, band):
    items = write_items(tmp_path, item(1, done="np-1", kind="band"), item(2, done="np-2", kind="band"))
    band += [{"id": "np-1", "state": "open", "items": ["levain#1"], "by": "", "grade": "unknown"},
             {"id": "np-2", "state": "open", "items": ["levain#2"], "by": "", "grade": "unknown"}]
    _, ids, _ = open_ids(capsys, items)
    assert sorted(ids) == ["levain#1", "levain#2"]
    band[:] = [{"id": "np-1", "state": "resolved", "items": ["levain#1"], "by": "phone", "grade": "signed-phone"},
               {"id": "np-2", "state": "resolved", "items": ["levain#2"], "by": "cli", "grade": "unsigned-terminal"}]
    rc, ids, doc = open_ids(capsys, items)
    assert ids == ["levain#2"] and rc == 3
    assert [(i["state"], i["kind"]) for i in doc["items"]] == [("landed_not_closed", "close")]
    assert [(a["kind"], a["id"]) for a in doc["audit"]] == [("landed_not_closed", "levain#2")]
    _, text, _ = open_run(capsys, items)
    assert "audit landed_not_closed: levain#2" in text and "close it or name what remains" in text


def test_a_decision_to_cut_drops_the_item_and_a_reopen_brings_it_back(world, tmp_path, capsys):
    cut = json.dumps({"v": 1, "op": "decide", "id": "levain#2", "at": AT, "by": "t", "decision": "cut", "words": "no", "date": "2026-11-12"})
    reopen = json.dumps({"v": 1, "op": "decide", "id": "levain#2", "at": AT, "by": "t", "decision": "reopen", "words": "yes", "date": "2026-11-13"})
    _, ids, _ = open_ids(capsys, write_items(tmp_path, item(1), item(2), cut))
    assert ids == ["levain#1"]
    _, ids, _ = open_ids(capsys, write_items(tmp_path, item(1), item(2), cut, reopen))
    assert sorted(ids) == ["levain#1", "levain#2"]


def test_an_item_the_board_cannot_read_is_open_and_first(world, tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)                                # not a repository: every item reads unknown (the failure board)
    rc, out, _ = open_run(capsys, write_items(tmp_path, item(1), item(2)))
    assert rc == 3 and out.splitlines()[0].startswith("open: 2 of 2 items") and out.splitlines()[1].startswith("unknown: 2")


def test_the_verb_writes_neither_the_store_nor_the_repository(world, tmp_path, capsys):
    world.branch("seat/1112-40-i2-lane")
    world.commit("lane work", trailers=("Board-Item: levain#2",))
    items = write_items(tmp_path, item(2), item(3))
    before = snapshot(items, world)
    open_run(capsys, items)
    open_run(capsys, items, "--json", "--repo", "levain")
    assert snapshot(items, world) == before


def test_an_untracked_repo_is_one_line_and_the_exit_code_is_states(world, tmp_path, capsys):
    world.commit("someone forgot the trailer")
    world.push_main()
    items = write_items(tmp_path, item(1))
    rc_state, _, _ = open_run(capsys, items, verb="state")
    rc, out, _ = open_run(capsys, items, "--repo", "ghost")
    assert rc == rc_state == 3 and out == "ghost: the board holds no items for it\n"


def test_the_json_is_the_open_selection_with_the_documents_own_items_and_no_new_field(world, tmp_path, capsys):
    items = write_items(tmp_path, item(1), item(2))
    world.commit("land one", trailers=("Board-Item: levain#1", "Board-Closes: levain#1"))
    world.push_main()
    _, out_state, _ = open_run(capsys, items, "--json", verb="state")
    _, out_open, _ = open_run(capsys, items, "--json")
    state, opened = json.loads(out_state), json.loads(out_open)
    assert set(opened) == {"schema", "as_of", "items", "audit", "problems", "reader"}
    assert opened["items"] == [i for i in state["items"] if i["state"] in board_cli.OPEN_STATES]
    assert opened["audit"] == state["audit"] and opened["problems"] == state["problems"]

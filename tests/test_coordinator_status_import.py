"""Row 156 step 2C: the status-file importer. A synthetic file with the real row shape (numbered, bold title, tier
parenthetical, bracketed history, a ``DONE:`` marker); a scratch store, never a live one."""
from __future__ import annotations

import re

import pytest

from levain.coordinator import ready, status_import
from levain.coordinator.store import CoordStore

SHA = re.compile(r"[0-9a-f]{40}")


def _row(n, title, tier="sonnet", lead="", done="the thing works. More words follow here."):
    return f"{n}. **{title}** (tier: {tier}){lead} [re-derived 2026-11-08 by a lane: a check] DONE: {done}"


def _file(tmp_path, rows, name="status.md"):
    p = tmp_path / name
    p.write_text("# head\n\n## LANE MENU (synthetic)\n\nBoard refresh 1 (notes)\n\n**Open rows:**\n\n"
                 + "\n".join(rows) + "\n\n## NEXT SECTION\n\n99. **not a lane row** (tier: opus) DONE: no.\n")
    return p


@pytest.fixture
def board(tmp_path):
    d = tmp_path / "board"
    d.mkdir(mode=0o700)
    store = CoordStore(d)
    return store, store.coord_take()


def _journal(store):
    return [(r["id"], r["source"]) for r in ready.journal(store)]


def test_reimport_of_an_unchanged_file_writes_nothing_and_a_changed_row_one_correction(board, tmp_path):
    store, token = board
    path = _file(tmp_path, [_row(1, "First item. Second sentence."), _row(2, "Another item")])
    first = status_import.import_status(store, token, path)
    assert first.added == ["levain:row-1", "levain:row-2"] and first.corrected == []
    before = _journal(store)
    again = status_import.import_status(store, token, path)
    assert again.added == [] and again.corrected == [] and again.unchanged == ["levain:row-1", "levain:row-2"]
    assert _journal(store) == before
    path2 = _file(tmp_path, [_row(1, "First item. Second sentence."), _row(2, "Another item", done="a changed finish.")])
    third = status_import.import_status(store, token, path2)
    assert third.corrected == ["levain:row-2"] and third.unchanged == ["levain:row-1"]
    assert _journal(store) == before + [("levain:row-2", "correction")]
    assert ready.list_ready(store, all=True, repo="levain")["items"][1]["done_line"] == "a changed finish."


def test_a_prose_hold_lands_in_hold_and_holds_the_item_back(board, tmp_path):
    store, token = board
    path = _file(tmp_path, [_row(3, "Waiting thing", lead=" HELD until Phill rules on the plan. Other prose np-0123abcd."),
                            _row(4, "Free thing", lead=" a plain lead.")])
    status_import.import_status(store, token, path)
    assert ready.item_facts(store, "levain:row-3")["hold"] == "HELD until Phill rules on the plan."
    assert ready.item_facts(store, "levain:row-3")["residue"] == "np-0123abcd"
    assert ready.item_facts(store, "levain:row-4")["hold"] is None
    by_id = {i["id"]: i for i in ready.list_ready(store, all=True, repo="levain")["items"]}
    assert by_id["levain:row-3"]["held_reason"] == "HELD until Phill rules on the plan."
    assert by_id["levain:row-3"]["status_category"] == "backlog" and by_id["levain:row-4"]["independent"]
    # the hold leaves the text -> a correction clears it
    status_import.import_status(store, token, _file(tmp_path, [_row(3, "Waiting thing", lead=" a plain lead."),
                                                                  _row(4, "Free thing", lead=" a plain lead.")]))
    assert ready.item_facts(store, "levain:row-3")["hold"] is None


def test_caps_cut_at_the_sentence_and_the_full_text_stays_reachable_through_ref(board, tmp_path):
    store, token = board
    long_title = "word " * 60                      # 300 characters, no sentence end
    long_done = ("A first sentence that is long enough " + "padding " * 40 + "ends here. ") + "Second sentence. " * 80
    rows = [_row(5, "Short head. Then a second sentence.", done=long_done), _row(6, long_title.strip())]
    path = _file(tmp_path, rows)
    status_import.import_status(store, token, path)
    items = {i["id"]: i for i in ready.list_ready(store, all=True, repo="levain")["items"]}
    assert items["levain:row-5"]["title"] == "Short head."
    done = items["levain:row-5"]["done_line"]
    assert done.endswith("ends here.") and len(done) <= status_import.DONE_MAX
    assert len(items["levain:row-6"]["title"]) <= status_import.TITLE_MAX
    # nothing was lost: the ref names the row, and the row's own line (full text) hashes to the ref's sha
    n, sha = re.fullmatch(r"STATUS_1_0\.md#(\d+)@([0-9a-f]{40})", ready.item_facts(store, "levain:row-5")["ref"]).groups()
    line = next(ln for ln in path.read_text().split("\n") if ln.startswith(f"{n}. "))
    assert status_import.blob_sha(line.encode()) == sha and "Second sentence." in line


def test_a_ref_carries_the_blob_sha_of_the_row_and_changes_with_it(board, tmp_path):
    store, token = board
    status_import.import_status(store, token, _file(tmp_path, [_row(7, "Seven"), _row(8, "Eight")]))
    r7, r8 = (ready.item_facts(store, f"levain:row-{n}")["ref"] for n in (7, 8))
    assert SHA.search(r7) and r7.startswith("STATUS_1_0.md#7@") and r8.startswith("STATUS_1_0.md#8@")
    assert r7.split("@")[1] != r8.split("@")[1]
    status_import.import_status(store, token, _file(tmp_path, [_row(7, "Seven", done="changed."), _row(8, "Eight")]))
    assert ready.item_facts(store, "levain:row-7")["ref"] != r7 and ready.item_facts(store, "levain:row-8")["ref"] == r8


def test_the_section_bounds_the_parse_a_done_title_enters_done_and_a_coordinator_move_survives(board, tmp_path):
    store, token = board
    path = _file(tmp_path, [_row(9, "DONE 2026-11-08 the shipped thing"), _row(10, "Moving thing")])
    got = status_import.import_status(store, token, path)
    assert "levain:row-99" not in got.added and len(got.added) == 2
    items = {i["id"]: i for i in ready.list_ready(store, all=True, repo="levain")["items"]}
    assert items["levain:row-9"]["declared_category"] == "done" and items["levain:row-10"]["declared_category"] == "ready"
    ready.move(store, token, "levain:row-10", to="running", who="desk", why="started")
    status_import.import_status(store, token, _file(tmp_path, [_row(9, "DONE 2026-11-08 the shipped thing"),
                                                                  _row(10, "Moving thing", done="new finish.")]))
    items = {i["id"]: i for i in ready.list_ready(store, all=True, repo="levain")["items"]}
    assert items["levain:row-10"]["declared_category"] == "running" and items["levain:row-10"]["done_line"] == "new finish."
    gone = status_import.import_status(store, token, _file(tmp_path, [_row(9, "DONE 2026-11-08 the shipped thing")]))
    assert gone.absent == ["levain:row-10"]


def test_hold_is_uppercase_held_or_on_hold_in_any_case_as_whole_words_outside_the_history(board, tmp_path):
    # The board marks a hold as uppercase HELD; a lowercase "held" is ordinary prose ("a held consent request").
    store, token = board
    rows = [_row(5, "Lower thing", lead=" a held consent request becomes one row."),
            _row(6, "Upheld thing", lead=" the earlier ruling was upheld and withheld nothing; beholden to none."),
            _row(7, "Quiet thing", lead=" the panel's N HELD BACK chip and the HELD BRANCHES list."),
            _row(8, "Mixed thing", lead=" it is on hold for the plan.")]
    rows[2] = rows[2].replace("[re-derived", "[re-derived HELD then freed;")
    status_import.import_status(store, token, _file(tmp_path, rows))
    assert ready.item_facts(store, "levain:row-5")["hold"] is None
    assert ready.item_facts(store, "levain:row-6")["hold"] is None
    assert ready.item_facts(store, "levain:row-7")["hold"] is None
    assert ready.item_facts(store, "levain:row-8")["hold"] == "it is on hold for the plan."


def _cli(qdir, capsys, *argv):
    from levain.cli import main
    code = main(["coord", "import-status", *argv, "--queue-dir", str(qdir)])
    cap = capsys.readouterr()
    return code, cap.out, cap.err


def test_cli_import_status_prints_a_line_per_bucket_and_a_rerun_adds_nothing(tmp_path, capsys):
    qdir = tmp_path / "q"
    qdir.mkdir(mode=0o700)
    token = CoordStore(qdir).coord_take()
    path = _file(tmp_path, [_row(1, "First item"), _row(2, "Second item")])
    code, out, err = _cli(qdir, capsys, str(path), "--token", str(token))
    assert (code, err) == (0, "")
    assert out.splitlines() == ["added: 2 (levain:row-1, levain:row-2)", "corrected: 0", "unchanged: 0", "absent: 0",
                                "unparsed: 0"]
    code, out, _ = _cli(qdir, capsys, str(path), "--token", str(token))
    assert code == 0 and out.splitlines()[0] == "added: 0" and "unchanged: 2 (levain:row-1, levain:row-2)" in out


def test_cli_import_status_exits_1_and_writes_nothing_without_the_live_lease(tmp_path, capsys):
    qdir = tmp_path / "q"
    qdir.mkdir(mode=0o700)
    store = CoordStore(qdir)
    token = store.coord_take()
    path = _file(tmp_path, [_row(1, "First item")])
    code, out, err = _cli(qdir, capsys, str(path), "--token", str(token + 1))
    assert (code, out) == (1, "") and "not held" in err
    with pytest.raises(SystemExit) as no_token:                  # --token is required (row 156 L3 R3-C)
        _cli(qdir, capsys, str(path))
    assert no_token.value.code == 2
    assert ready.journal(store) == []


def test_a_row_the_store_refuses_goes_to_unparsed_and_the_import_goes_on(board, tmp_path):
    # row 3 fits the row shape but its title is blank, which the ready API refuses; rows 1, 2 and 4 still import
    store, token = board
    path = _file(tmp_path, [_row(1, "First item"), _row(2, "Second item"), _row(3, " "), _row(4, "Fourth item")])
    got = status_import.import_status(store, token, path)
    assert got.added == ["levain:row-1", "levain:row-2", "levain:row-4"] and got.unparsed == [3]
    # a correction the store refuses is the same: the row is named, the rest stand
    path2 = _file(tmp_path, [_row(1, "First item"), _row(2, "Second​ item"), _row(4, "Fourth item")])
    again = status_import.import_status(store, token, path2)
    assert again.unparsed == [2] and again.unchanged == ["levain:row-1", "levain:row-4"] and again.corrected == []

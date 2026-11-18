"""T6 (levain#99): the cockpit board and the List read the v2 board (T3's ``levain board state``, every tracked repo of the
registry from one snapshot). Unknown is shown as its own column and never folded; off is a count; needs-you is still the band's."""
from __future__ import annotations

import json
import shutil
import subprocess
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from levain.cockpit import board, list_panel
from levain.desk import board_cli
from tests import _board_v2_seed as seed

NODE = shutil.which("node")


@pytest.fixture(scope="module")
def store(tmp_path_factory):
    """One scratch board store for the module: two tracked repos, one untracked, items in every git-derivable state."""
    root = tmp_path_factory.mktemp("v2")
    mp = pytest.MonkeyPatch()
    mp.setenv("LEVAIN_HOME", str(root / "lhome"))
    mp.delenv("LEVAIN_LAUNCH_KEY", raising=False)
    ids = seed.seed(root)
    mp.setenv("LEVAIN_BOARD_ITEMS", str(root / "store"))
    mp.setenv(board.NO_LAND_WAITER_ENV, "1")
    yield ids
    mp.undo()


def _by_col(value):
    return {c["category"]: [k["id"] for k in c["cards"]] for c in value["columns"]}


def test_unknown_is_its_own_column_first_and_never_folded_into_another(store):
    v = board.build(None, None)
    assert v["queue"] == "ok" and v["unreadable"] == []
    cols = _by_col(v)
    assert [c["category"] for c in v["columns"]][0] == "unknown"
    assert cols["unknown"] == [store["unknown"]]
    assert all(store["unknown"] not in ids for c, ids in cols.items() if c != "unknown")      # folded nowhere
    (card,) = v["columns"][0]["cards"]
    assert card["status"] == "unknown" and "is not in the store" in card["held_reason"]      # and it says why


def test_every_tracked_repo_is_read_and_the_untracked_one_is_not(store):
    v = board.build(None, None)
    seen = {k["repo"] for c in v["columns"] for k in c["cards"] if k["id"] != "band"}
    assert seen == {"alpha", "beta"}                                                        # gamma is registered, never tracked
    cols = _by_col(v)
    assert store["done"] in cols["done"] and store["ready"] in cols["ready"] and store["landed"] in cols["ready"]
    assert store["groom"] in cols["backlog"] and store["blocked"] in cols["backlog"]


def test_off_is_a_count_and_never_a_column_of_cards(store):
    v = board.build(None, None)
    assert v["off_count"] == 3                                                              # cut, parked, out of 1.0
    assert "off" not in [c["category"] for c in v["columns"]]
    shown = {i for c in v["columns"] for i in (k["id"] for k in c["cards"])}
    assert not shown & {store["cut"], store["parked"], store["out"]}


def test_an_empty_unknown_column_is_left_out_and_the_rest_is_the_six(store, monkeypatch):
    doc = {"as_of": "2026-11-17T00:00:00Z", "items": [{"id": "a#1", "repo": "a", "title": "T", "tier": None, "state": "ready", "column": "ready",
                                                      "reason": "r", "refs": [], "as_of": None, "kind": "work"}]}
    monkeypatch.setattr(board_cli, "state_doc", lambda args, say=None: (0, doc))
    v = board.build(None, None)
    assert [c["category"] for c in v["columns"]] == list(board.CATEGORIES) and v["off_count"] == 0


def test_every_state_lands_in_its_columns_and_needs_you_stays_the_bands(store, monkeypatch):
    from levain.desk.board_state import STATE_COLUMN

    items = [{"id": f"a#{n}", "repo": "a", "title": s, "tier": None, "state": s, "column": col, "reason": f"{s} reason", "refs": [], "as_of": None,
              "kind": None} for n, (s, col) in enumerate(STATE_COLUMN.items(), 1)]
    monkeypatch.setattr(board_cli, "state_doc", lambda args, say=None: (3, {"as_of": None, "items": items}))
    v = board.build(None, None)
    cols = {c["category"]: [k["title"] for k in c["cards"]] for c in v["columns"]}
    assert cols["unknown"] == ["unknown"] and cols["running"] == ["in_flight"] and cols["waiting"] == ["landing"]
    assert cols["done"] == ["done"] and sorted(cols["ready"]) == ["landed_not_closed", "ready", "ready_with_start"]
    assert sorted(cols["backlog"]) == ["blocked", "needs_grooming"]
    assert cols["needs-you"] == []                                                          # a needs_you item is NOT drawn from the board
    assert v["off_count"] == 3 and sum(c["count"] for c in v["columns"]) == len(items) - 3 - 1


def test_the_needs_you_column_is_the_bands_alone(store, monkeypatch, tmp_path):
    from levain.consent.questions import QuestionStore

    monkeypatch.setenv("LEVAIN_HOME", str(tmp_path / "lh2"))
    (tmp_path / "lh2" / "consent").mkdir(parents=True, mode=0o700)
    monkeypatch.setattr(board_cli, "state_doc", lambda args, say=None: (0, {"as_of": None, "items": [
        {"id": "a#1", "repo": "a", "title": "in the band", "tier": None, "state": "needs_you", "column": "needs_you", "reason": "band 1",
         "refs": [], "as_of": None, "kind": None}]}))
    v = board.build(None, QuestionStore(tmp_path / "lh2" / "consent"))
    assert _by_col(v)["needs-you"] == []                                                    # the board's needs_you item adds nothing here


def test_a_board_store_that_cannot_be_read_is_named_not_shown_empty(tmp_path, monkeypatch):
    monkeypatch.setenv("LEVAIN_HOME", str(tmp_path / "lh"))
    monkeypatch.delenv("LEVAIN_BOARD_ITEMS", raising=False)
    (tmp_path / "lh" / "board").mkdir(parents=True)
    (tmp_path / "lh" / "board" / "STORE").write_text(str(tmp_path / "gone") + "\n")                  # the pointer names a store that is not there
    v = board.build(None, None)
    assert v["queue"] == "unreadable" and "desk" in v["unreadable"]
    assert "unknown" not in [c["category"] for c in v["columns"]]


def test_with_no_board_store_the_dispatch_queue_is_read_as_before(tmp_path, monkeypatch):
    monkeypatch.setenv("LEVAIN_HOME", str(tmp_path / "lh"))
    monkeypatch.delenv("LEVAIN_BOARD_ITEMS", raising=False)
    v = board.build(None, None)
    assert v["queue"] == "none" and v["off_count"] == 0 and [c["category"] for c in v["columns"]] == list(board.CATEGORIES)


def test_the_list_says_unknown_and_never_folds_it_into_the_ready_rows(store):
    out = list_panel._board_part(None, None, date(2026, 11, 17), datetime(2026, 11, 17, tzinfo=timezone.utc))
    ids = {r.id for r in out.rows}
    assert f"board:{store['ready']}" in ids and f"board:{store['unknown']}" not in ids
    assert any("read unknown" in why for _n, why in out.skipped)


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_the_page_draws_the_unknown_column_while_it_holds_a_card_and_the_off_count():
    root = Path(__file__).resolve().parent
    res = subprocess.run([NODE, str(root / "js_board_unknown_harness.js"), str(root.parent / "levain/templates/web/board_view.js")],
                         capture_output=True, text=True, timeout=60)
    assert res.returncode == 0, res.stderr
    got = json.loads(res.stdout)
    assert got["with"]["heads"][:2] == ["unknown 1", "backlog 0"] and "ready 1" in got["with"]["heads"] and "3 off the board (cut, parked or out of 1.0)." in got["with"]["notes"][0]
    assert got["with"]["held"] == ["held: item:a#9 is not in the store"]
    assert "unknown" not in " ".join(got["without"]["heads"]) and "ready 1" in got["without"]["heads"] and got["without"]["notes"] == []

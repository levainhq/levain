"""levain#188: the operator-declared horizon. ``out-of-horizon`` is the stored decision word and carries ``horizon``; the legacy
word ``out-of-1.0`` is read as ``out-of-horizon`` with horizon "1.0" and is never written; the emitted state is ``out_of_horizon``.

The migration proof is two checks: ``before.json`` holds what the code before this change derived over the real stored records
beside it, and the alias check re-spells every legacy record by hand and demands an identical board. The writer's tests are in
test_board_store.py (they need its signing stand-in)."""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

import pytest

from levain.desk import board_state as bs

REPO = Path(__file__).resolve().parents[1]
REAL = REPO / "tests" / "fixtures" / "board_state" / "horizon_real"
NOW = datetime(2026, 11, 20, 11, 0, tzinfo=timezone.utc)
EVIDENCE = {"band": {"open": {}, "resolved": {}}, "branches": [], "chain": [], "documents": {}, "evidence_v": 1,
            "fetched_at": "2026-11-20T10:00:00Z", "main": [], "merged": [], "refs": [], "seats": [], "unattributed": [],
            "unreadable": {}, "waiters": []}
LEGACY = '"decision":"out-of-1.0"'
NEW_WORD = '"decision":"out-of-horizon","horizon":"1.0"'


def _board(lines):
    folded = bs.fold(lines)
    return folded, bs.derive_all(folded, bs.Evidence.from_dict(EVIDENCE, now=None), now=NOW)


def _real_lines():
    return REAL.joinpath("items.jsonl").read_text("utf-8").split("\n")


def _add(iid="levain#1", **kw):
    rec = {"v": 1, "op": "add", "id": iid, "at": "2026-11-20T09:00:00Z", "by": "desk 1", "repo": iid.split("#")[0], "title": "t",
           "origin": "desk 1", "done": "d", "done_kind": "merged", "tier": "sonnet", "depends_on": []}
    return json.dumps({**rec, **kw})


def _decide(iid="levain#1", **kw):
    rec = {"v": 1, "op": "decide", "id": iid, "at": "2026-11-20T09:30:00Z", "by": "desk 1", "decision": "out-of-horizon",
           "words": "later", "date": "2026-11-20"}
    return json.dumps({**rec, **kw})


# ---------------------------------------------------------------------------------------------------- (a) the real records
def test_the_stored_out_of_1_0_records_read_as_out_of_horizon_1_0_in_the_off_column():
    """The fixture is a SYNTHETIC board (invented ids and words) shaped like a real stored one: four legacy decide records over
    three items (one decided, reopened and decided again) and a fourth item that depends on one of them."""
    lines = _real_lines()
    assert sum(LEGACY in line for line in lines) == 4, "the fixture is the four stored legacy records"
    folded, board = _board(lines)
    assert not folded.torn and folded.problems == ()
    got = {s.id: s for s in board.items}
    assert sorted(got) == ["app#11", "app#12", "app#13", "app#14"]
    off = [got[i] for i in ("app#11", "app#12", "app#13")]
    for s in off:
        assert (s.state, s.column, s.horizon) == ("out_of_horizon", "off", "1.0")
    assert got["app#12"].reason == "sample ruling B: the exporter rewrite waits for the next release (2026-03-03)"
    assert all(d.decision == "out-of-horizon" and d.horizon == "1.0" for d in (folded.items[s.id].decision for s in off))
    # the dependent of an out-of-horizon item stays held by the cut, as it was held by the old spelling
    assert (got["app#14"].state, got["app#14"].column, got["app#14"].horizon) == ("blocked", "backlog", None)
    assert got["app#14"].reason.startswith("waits on app#12: dependency cut: decide")


def test_the_state_object_carries_horizon_only_for_an_out_of_horizon_state():
    _, board = _board([_add("levain#1"), _add("levain#2"), _decide("levain#1", horizon="launch"),
                       _decide("levain#2", decision="cut")])
    rows = {s.id: s.to_dict() for s in board.items}
    assert rows["levain#1"]["horizon"] == "launch" and rows["levain#2"]["horizon"] is None


# ---------------------------------------------------------------------------------------------------- (b) the new word
def test_the_new_word_folds_to_the_horizon_it_carries():
    folded, board = _board([_add(), _decide(horizon="launch")])
    s = board.items[0]
    assert (s.state, s.column, s.horizon, s.reason) == ("out_of_horizon", "off", "launch", "later (2026-11-20)")
    assert folded.items["levain#1"].decision.horizon == "launch"


@pytest.mark.parametrize("horizon", ["1.1", "launch", "Q4 2026", "v2_beta-3", "a", "9", "x" * 40],
                         ids=["dotted", "word", "inner_space", "mixed_punctuation", "one_letter", "digit_first", "longest"])
def test_a_well_formed_horizon_is_taken(horizon):
    folded, _ = _board([_add(), _decide(horizon=horizon)])
    assert not folded.torn and folded.items["levain#1"].decision.horizon == horizon


@pytest.mark.parametrize("horizon", ["", None, "two\nlines", "x" * 41, "-lead", ".lead", " lead", "trail ", "a/b", "café",
                                     "a\tb", 5, ["1.1"], True],
                         ids=["empty", "null", "multiline", "too_long", "dash_first", "dot_first", "space_first", "space_last", "slash",
                              "non_ascii", "tab", "number", "list", "bool"])
def test_a_bad_horizon_tears_the_file_as_any_bad_record_does(horizon):
    rec = json.loads(_decide())
    rec["horizon"] = horizon
    folded, board = _board([_add(), json.dumps(rec)])
    assert folded.torn and "line 2" in folded.torn_why and "horizon" in folded.torn_why
    assert {s.state for s in board.items} == {"unknown"}


def test_a_missing_horizon_tears_the_file():
    folded, _ = _board([_add(), _decide()])
    assert folded.torn and "line 2" in folded.torn_why and "horizon" in folded.torn_why


def test_a_horizon_on_another_word_is_not_read():
    folded, board = _board([_add(), _decide(decision="cut", horizon="launch")])
    assert not folded.torn and (board.items[0].state, board.items[0].horizon) == ("cut", None)


def test_the_horizon_rule_is_one_constant_pair():
    assert bs.HORIZON_MAX == 40 and bs.HORIZON_RE.fullmatch("1.0") and not bs.HORIZON_RE.fullmatch("")


# ---------------------------------------------------------------------------------------------------- (f) the legacy word
def test_the_legacy_word_still_folds_and_is_not_a_decision_word():
    assert bs.LEGACY_OUT_OF == "out-of-1.0" and bs.LEGACY_OUT_OF not in bs.DECISIONS and "out-of-horizon" in bs.DECISIONS
    folded, _ = _board([_add(), _decide(decision="out-of-1.0")])
    assert not folded.torn and folded.items["levain#1"].decision.decision == "out-of-horizon"


def test_a_legacy_record_reads_horizon_1_0_whatever_horizon_key_it_carries():
    folded, _ = _board([_add(), _decide(decision="out-of-1.0", horizon="launch")])
    assert folded.items["levain#1"].decision.horizon == "1.0"


def test_reopen_clears_either_spelling_and_the_last_decide_wins():
    folded, board = _board([_add(), _decide(decision="out-of-1.0"), _decide(decision="reopen", words="back"),
                            _decide(horizon="launch")])
    assert (board.items[0].state, board.items[0].horizon) == ("out_of_horizon", "launch")
    folded, board = _board([_add(), _decide(decision="out-of-1.0"), _decide(decision="reopen", words="back")])
    assert folded.items["levain#1"].decision is None and board.items[0].state != "out_of_horizon"


def test_an_unknown_word_still_tears_the_file_and_names_the_legacy_word_as_readable():
    folded, _ = _board([_add(), _decide(decision="out-of-2.0")])
    assert folded.torn and "out-of-horizon" in folded.torn_why


def test_out_of_1_0_is_no_longer_an_emitted_state():
    assert "out_of_1_0" not in bs.STATE_COLUMN and bs.STATE_COLUMN["out_of_horizon"] == "off"
    assert set(bs._DECISION_STATE) == set(bs.DECISIONS)


# ---------------------------------------------------------------------------------------------------- (d) the migration proof
def test_the_board_over_the_stored_records_is_what_the_code_before_this_change_derived():
    """``before.json`` was recorded from the unchanged code (the fold at the commit before this change) over the same synthetic
    file: the columns and the reasons are identical and the only state that changed is ``out_of_1_0`` to ``out_of_horizon``."""
    before = json.loads(REAL.joinpath("before.json").read_text("utf-8"))
    _, board = _board(_real_lines())
    now = [[s.id, s.state, s.column, s.reason] for s in board.items]
    assert [(i, c, r) for i, _s, c, r in now] == [(i, c, r) for i, _s, c, r in before]
    renamed = {"out_of_1_0": "out_of_horizon"}
    assert [(i, renamed.get(s, s)) for i, s, _c, _r in before] == [(i, s) for i, s, _c, _r in now]
    assert {s for _i, s, _c, _r in before} == {"out_of_1_0", "blocked"}
    assert [s for _i, s, _c, _r in before].count("out_of_1_0") == 3


def _respelled(lines):
    return [line.replace(LEGACY, NEW_WORD) for line in lines]


def _same_board(lines):
    folded_a, a = _board(lines)
    folded_b, b = _board(_respelled(lines))
    assert not folded_a.torn and not folded_b.torn, (folded_a.torn_why, folded_b.torn_why)
    assert [s.to_dict() for s in a.items] == [s.to_dict() for s in b.items]
    assert a.audit == b.audit and a.problems == b.problems


def test_respelling_every_legacy_record_with_the_new_word_and_horizon_1_0_changes_no_item():
    _same_board(_real_lines())


LIVE_ITEMS = Path(os.environ.get("STORED_BOARD_ITEMS", "") or "/nonexistent")


@pytest.mark.skipif(not LIVE_ITEMS.is_file(), reason="no real stored board on this machine (set STORED_BOARD_ITEMS to a copy)")
def test_respelling_a_real_whole_board_changes_no_item():
    """The same check over a whole stored board (read only), when one is named."""
    _same_board(LIVE_ITEMS.read_text("utf-8").split("\n"))


# ---------------------------------------------------------------------------------------------------- (g) the schema document
def test_the_schema_document_and_fixture_say_horizon_not_1_0():
    doc = (REPO / "docs" / "ready-schema-v2.md").read_text("utf-8")
    fixture = json.loads((REPO / "tests" / "fixtures" / "ready_schema" / "ready_v2.json").read_text("utf-8"))
    assert {r["state"]: r["column"] for r in fixture["states"]} == bs.STATE_COLUMN
    assert "`out_of_horizon`" in doc and "`out-of-horizon`" in doc
    assert all("legacy" in line for line in doc.splitlines() if "out_of_1_0" in line), "the old state name appears only in a legacy note"
    assert all("legacy" in line for line in doc.splitlines() if "out-of-1.0" in line), "the old decision word appears only in a legacy note"
    assert fixture["state_keys"][-1] == "horizon"

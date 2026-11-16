"""A ``document``/``run`` item whose ``done`` is a sentence can never be closed by its evidence (the evidence IS a path), so
with commits on main it read landed_not_closed forever and no audit kind named why. ``prose_done`` names it."""
from __future__ import annotations

import json

import pytest

from levain.desk import board_state as bs

NOW = bs.parse_timestamp("2026-11-16T20:00:00Z")
SENTENCE = "the reader can run it and sees a green board"


def _item(kind: str, done: str, n: int = 7) -> str:
    return json.dumps({"v": 1, "op": "add", "id": f"flow#{n}", "at": "2026-11-16T10:00:00Z", "by": "t", "repo": "flow",
                       "title": "t", "origin": "o", "done": done, "done_kind": kind, "tier": "sonnet", "depends_on": []})


def _board(lines, documents=None, landed=True):
    main = [{"sha": "a" * 40, "date": "2026-11-16T12:00:00Z", "items": ["flow#7"], "closes": ["flow#7"]}] if landed else []
    ev = {"evidence_v": 1, "fetched_at": "2026-11-16T19:00:00Z", "unreadable": {}, "main": main, "chain": [], "refs": [],
          "merged": [], "branches": [], "seats": [], "band": {"open": {}, "resolved": {}}, "waiters": [],
          "documents": documents or {}, "unattributed": []}
    return bs.derive_all(bs.fold(lines), bs.Evidence.from_dict(ev), now=NOW)


@pytest.mark.parametrize("text", ["docs/x.md", "x.md", "~/a/b", "projects/flow/reference/x/", "a/b", "report.v2.json"])
def test_a_path_looks_like_a_path(text):
    assert bs.looks_like_path(text)


@pytest.mark.parametrize("text", [SENTENCE, "done.", "see docs/x.md for it", "a b.md", "docs", "", "   ", "v1.", "x. y"])
def test_prose_does_not_look_like_a_path(text):
    assert not bs.looks_like_path(text)


@pytest.mark.parametrize("kind", ["document", "run"])
def test_a_prose_done_with_a_closing_trailer_on_main_is_named_not_landed_not_closed_forever(kind):
    board = _board([_item(kind, SENTENCE)], {SENTENCE: False})
    (entry,) = board.audit
    assert entry.kind == "prose_done" and entry.id == "flow#7"
    assert kind in entry.why and "path" in entry.why
    assert board.items[0].state == "landed_not_closed"        # the state is unchanged: only the audit names the cause


@pytest.mark.parametrize("kind", ["document", "run"])
def test_a_prose_done_with_no_commits_is_named_too(kind):
    board = _board([_item(kind, SENTENCE)], {SENTENCE: False}, landed=False)
    assert board.items[0].state == "ready"
    assert [(a.kind, a.id) for a in board.audit] == [("prose_done", "flow#7")]


def test_a_real_path_is_unchanged():
    board = _board([_item("document", "docs/x.md")], {"docs/x.md": False})
    assert [(a.kind, a.id) for a in board.audit] == [("landed_not_closed", "flow#7")]
    done = _board([_item("document", "docs/x.md")], {"docs/x.md": True})
    assert done.items[0].state == "done" and not done.audit


def test_a_merged_item_with_a_sentence_done_is_unchanged():
    board = _board([_item("merged", SENTENCE)])
    assert board.items[0].state == "done" and not board.audit


def test_a_band_item_and_a_decided_prose_item_are_unchanged():
    decide = json.dumps({"v": 1, "op": "decide", "id": "flow#7", "at": "2026-11-16T11:00:00Z", "by": "t",
                         "decision": "cut", "words": "no", "date": "2026-11-16"})
    board = _board([_item("document", SENTENCE), decide], {SENTENCE: False})
    assert board.items[0].state == "cut" and not board.audit

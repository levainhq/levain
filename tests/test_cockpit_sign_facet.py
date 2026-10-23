"""S1b (DESIGN_SIGN D5, head 1023+21): a question or note row carries ``sign: {"request": "<id>"}`` exactly when its
``sign_request`` is set. The facet is an id, never the request's words: the decisions panel is token-free on loopback.
The stores here are the shape flow's desk reader is (``Question`` objects built with ``sign_request``)."""
from __future__ import annotations

import dataclasses
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from levain.cockpit import Cockpit
from levain.cockpit.registry import FacetValueError, validate_facets
from levain.consent.notes import NoteStore
from levain.consent.questions import QuestionStore

RID = "np-sign-facet-0001"


class _Signed:
    """A store answering as ``inner`` does, with ``sign_request`` set on the rows whose number is in ``ids``."""

    def __init__(self, inner: Any, ids: "dict[int, str]") -> None:
        self.inner, self.ids = inner, ids

    def _mark(self, rows: Any) -> Any:
        return [dataclasses.replace(r, sign_request=self.ids[r.n]) if r.n in self.ids else r for r in rows]

    def open_questions(self) -> Any:
        return self._mark(self.inner.open_questions())

    def recent(self, limit: int) -> Any:
        return self._mark(self.inner.recent(limit))

    def open_page(self, limit: int) -> Any:
        rows, total = self.inner.open_page(limit)
        return self._mark(rows), total

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)


def _rows(ck: Cockpit) -> "dict[str, dict[str, Any]]":
    p = ck.panel("decisions", credential_class="token")
    assert not p.get("skipped"), p
    return {r["id"]: r for r in p["rows"]}


def test_a_question_row_carries_the_sign_facet_only_when_its_question_names_a_request(tmp_path: Path) -> None:
    store = QuestionStore(tmp_path / "q")
    a, b = (store.file(agent="claude_code", agent_session_id="s", what=f"which {i}?", options=[("x", "X"), ("y", "Y")],
                       rec="x", blocks="nothing", cwd="/w") for i in (1, 2))
    assert a.sign_request is None                          # the kernel's own store files none
    ck = Cockpit(clock=lambda: datetime.now(timezone.utc))
    ck.attach_question_rows(_Signed(store, {a.n: RID}))
    rows = _rows(ck)
    assert rows[f"question:{a.n}"]["facets"]["sign"] == {"request": RID}
    assert "sign" not in rows[f"question:{b.n}"]["facets"]
    assert "which 1?" not in str(rows)                     # still no words on the token-free panel


def test_a_note_row_carries_the_sign_facet_only_when_its_note_names_a_request(tmp_path: Path) -> None:
    store = NoteStore(tmp_path / "q")
    a = store.file(agent="claude_code", agent_session_id="s", what="one", detail=None, cwd="/w")
    b = store.file(agent="claude_code", agent_session_id="s", what="two", detail=None, cwd="/w")
    assert a.sign_request is None
    ck = Cockpit(clock=lambda: datetime.now(timezone.utc))
    ck.attach_note_rows(_Signed(store, {b.n: RID}))
    rows = _rows(ck)
    assert rows[f"note:{b.n}"]["facets"]["sign"] == {"request": RID}
    assert "sign" not in rows[f"note:{a.n}"]["facets"]


@pytest.mark.parametrize("bad", [RID, {"request": 7}, {"request": "a b c d e f"}, {"request": "short"},
                                 {"request": RID, "choice": "a"}, {}, {"request": None}, {"request": "x" * 65}])
def test_a_sign_facet_is_exactly_one_request_id(bad: Any) -> None:
    validate_facets({"sign": {"request": RID}})
    with pytest.raises(FacetValueError, match="sign"):
        validate_facets({"sign": bad})


def test_a_row_naming_a_malformed_request_is_refused_and_counted_never_drawn_unsigned(tmp_path: Path) -> None:
    """A caller's bad id is the engine's wrong-type row: left out and counted on the panel, so the page never draws
    the question with no sign facet (an unsigned answer) for a question its caller meant to be signed."""
    store = QuestionStore(tmp_path / "q")
    a = store.file(agent="claude_code", agent_session_id="s", what="which?", options=[("x", "X"), ("y", "Y")],
                   rec="x", blocks="nothing", cwd="/w")
    ck = Cockpit(clock=lambda: datetime.now(timezone.utc))
    ck.attach_question_rows(_Signed(store, {a.n: "not a request id"}))
    p = ck.panel("decisions", credential_class="token")
    assert f"question:{a.n}" not in {r["id"] for r in p["rows"]}
    assert p["skipped"] == [{"count": 1, "reason": "row refused: a facet value of the wrong type"}], p

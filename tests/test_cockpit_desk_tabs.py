"""The Decisions card's Needs you / Desk owes tabs: the engine passes the host's section and carried flag through as facets
(a Question without them draws none), and the page's split (card_format.js deskTabs) tags and counts rows."""
from __future__ import annotations

import dataclasses
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from levain.cockpit.engine import _question_row_in
from levain.cockpit.registry import validate_facets
from levain.consent.questions import Question

HERE = Path(__file__).parent


def _q(n: int, **kw) -> Question:
    base = dict(n=n, id=f"np-{n}", agent="desk", agent_session_id="s", what="w", options=(("a", "A"),), rec="a",
                blocks="b", cwd="/w", filed_at="2026-11-06T10:00:00+00:00", answer=None)
    base.update(kw)
    return Question(**base)


def test_engine_rows_carry_section_and_carried_and_a_legacy_question_draws_none() -> None:
    desk = _question_row_in(_q(1, section="desk", carried=True)).facets
    you = _question_row_in(_q(2, section="you")).facets
    legacy = _question_row_in(_q(3)).facets
    assert (desk["question_section"], desk["question_carried"]) == ("desk", True)
    assert (you["question_section"], you["question_carried"]) == ("you", False)
    assert "question_section" not in legacy and "question_carried" not in legacy
    for f in (desk, you, legacy):
        validate_facets(f)
    # a foreign store's question type with neither field still builds a row
    foreign = SimpleNamespace(**{f.name: getattr(_q(4), f.name) for f in dataclasses.fields(Question)
                                 if f.name not in ("section", "carried")})
    assert "question_section" not in _question_row_in(foreign).facets


def test_a_non_boolean_carried_drops_the_facet_instead_of_coercing() -> None:
    f = _question_row_in(_q(5, section="desk", carried="false")).facets
    assert "question_carried" not in f and "question_section" not in f
    validate_facets(f)


def test_the_page_split_tags_and_counts_open_rows() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("needs node")
    out = subprocess.run([node, str(HERE / "desk_tabs_check.js")], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0 and "ALL PASS" in out.stdout, out.stdout + out.stderr

"""The Decisions card's Needs you / Desk owes tabs: the engine passes the host's section and carried flag through as facets
(a Question without them draws none), and the page's split (card_format.js deskTabs) tags and counts rows."""
from __future__ import annotations

import dataclasses
import json
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from levain.cockpit.engine import _question_row_in
from levain.cockpit.registry import validate_facets
from levain.consent.questions import Question
from tests.test_cockpit_flip_parity import _drive, needs_browser
from tests.test_cockpit_k2a import _install, _serve
from tests.test_cockpit_needs_rows import READY, _file

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


def _ql(n: int, **facets) -> dict:
    return {"question": {"id": f"question:{n}", "n": n, "state": "open", "agent": "desk", **facets}}


@needs_browser
def test_the_needs_strip_leaves_out_an_uncarried_desk_question(tmp_path: Path, monkeypatch) -> None:
    """An open question the desk alone owes (section desk, not carried) is the Decisions card's Desk owes tab, not his strip;
    a carried desk question shows in both, and a legacy question with no section facet still shows. The count is the items."""
    _file(tmp_path, monkeypatch)
    _root, src, _ids = _install(tmp_path)
    lines = [_ql(1, section="you"), _ql(2, section="desk", carried=False), _ql(3, section="desk", carried=True), _ql(4)]
    probe = (f"(() => {{ const it = window.LevainNeeds.items({json.dumps(lines)});"
             " return JSON.stringify({ids: it.map(x => x.q.id), n: it.length}); })()")
    with _serve(src, surface_tokens={"browser": "T"}, install_class="reported") as (url, _httpd):
        vals = _drive(tmp_path, f"{url}/#write_token=T", [{"until": READY, "ms": 20000}, {"eval": probe}])["values"]
    got = [json.loads(v) for v in vals if isinstance(v, str) and v.startswith("{")]
    assert got, vals
    assert sorted(got[-1]["ids"]) == ["question:1", "question:3", "question:4"] and got[-1]["n"] == 3, got[-1]
    assert "question:2" not in got[-1]["ids"]

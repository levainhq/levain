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


@needs_browser
def test_a_dated_question_is_one_later_line_after_the_due_rows_and_is_not_counted(tmp_path: Path, monkeypatch) -> None:
    """The Needs you strip (not only the board card) draws a question dated past today as one LATER line at the end, with no
    answer controls, and the strip's count and `LevainNeeds.items` leave it out. The facet comes from the engine's rows."""
    from datetime import datetime, timedelta

    from levain.consent.questions import QuestionStore, local_today
    _file(tmp_path, monkeypatch)                      # three due questions in LEVAIN_HOME/consent
    qs = QuestionStore(tmp_path / "levain_home" / "consent")
    later = qs.file(agent="claude_code", agent_session_id="s9", what="Dated next week?", options=[("a", "Yes"), ("b", "No")], rec="a", blocks="b", cwd="/w")
    day = (datetime.fromisoformat(local_today(datetime.now().astimezone())) + timedelta(days=3)).date().isoformat()
    qs.snooze(later.id, until=day, channel="cli", via="cli")
    _root, src, _ids = _install(tmp_path)
    probe = ("(() => JSON.stringify({"
             " later: Array.from(document.querySelectorAll('#ck-needs .needs-later-line')).map(e => e.textContent),"
             " rows: document.querySelectorAll('#ck-needs .needs-row').length,"
             " tab: (document.querySelector('#ck-needs [data-needs-tab=you]') || {}).textContent,"
             " lastIsLater: !!(document.querySelector('#ck-needs').lastElementChild && document.querySelector('#ck-needs').lastElementChild.classList.contains('needs-later')),"
             " controls: document.querySelectorAll('#ck-needs .needs-later-line button').length}))()")
    with _serve(src, surface_tokens={"browser": "T"}, install_class="reported") as (url, _httpd):
        vals = _drive(tmp_path, f"{url}/#write_token=T", [{"until": READY, "ms": 20000}, {"until": "!!document.querySelector('#ck-needs .needs-later-line')", "ms": 20000}, {"eval": probe}])["values"]
    got = [json.loads(v) for v in vals if isinstance(v, str) and v.startswith("{")]
    assert got, vals
    g = got[-1]
    assert g["rows"] == 3 and g["tab"] == "Needs you (3)", g
    assert len(g["later"]) == 1 and g["later"][0].startswith("LATER") and datetime.fromisoformat(day).strftime("%a") in g["later"][0], g
    assert g["lastIsLater"] is True and g["controls"] == 0, g


def test_engine_rows_carry_the_later_facets_from_the_one_predicate(tmp_path: Path) -> None:
    """`_question_rows_in` adds question_later / question_later_words to an open question dated after the store's local
    today (band_view.later_of) and none to one due today or earlier; both validate."""
    from datetime import datetime, timedelta

    from levain.cockpit.engine import _question_rows_in
    from levain.consent.questions import QuestionStore, local_today
    store = QuestionStore(tmp_path)
    now = datetime.now().astimezone()
    today = datetime.fromisoformat(local_today(now)).date()
    a = store.file(agent="claude_code", agent_session_id="s1", what="Due", options=[("a", "A"), ("b", "B")], rec="a", blocks="b", cwd="/w")
    b = store.file(agent="claude_code", agent_session_id="s2", what="Tomorrow", options=[("a", "A"), ("b", "B")], rec="a", blocks="b", cwd="/w")
    c = store.file(agent="claude_code", agent_session_id="s3", what="Later", options=[("a", "A"), ("b", "B")], rec="a", blocks="b", cwd="/w")
    d = store.file(agent="claude_code", agent_session_id="s4", what="Today", options=[("a", "A"), ("b", "B")], rec="a", blocks="b", cwd="/w")
    store.snooze(b.id, until=(today + timedelta(days=1)).isoformat(), channel="cli", via="cli")
    store.snooze(c.id, until=(today + timedelta(days=2)).isoformat(), channel="cli", via="cli")
    rows, _left = _question_rows_in(store, now)
    f = {r.title.split("#")[-1]: r.facets for r in rows}
    wd = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
    assert "question_later" not in f[str(a.n)] and "question_later" not in f[str(d.n)]
    assert f[str(b.n)]["question_later"] == (today + timedelta(days=1)).isoformat()
    assert f[str(b.n)]["question_later_words"] == wd[(today + timedelta(days=1)).weekday()]
    t2 = today + timedelta(days=2)
    assert f[str(c.n)]["question_later_words"] == f"{wd[t2.weekday()]} {t2.strftime('%m-%d')}"
    for facets in f.values():
        validate_facets(facets)

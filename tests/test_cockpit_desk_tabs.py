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
    assert len(g["later"]) == 1 and datetime.fromisoformat(day).strftime("%a") in g["later"][0], g
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


# ---- LATER strip, fix round 1 (L3 r1 of e54b9ee1) ----

READY_ANY = "!!document.getElementById('ck-needs')"


def _later_home(tmp_path: Path, monkeypatch, specs: list[tuple[str, int]], desk: tuple[str, ...] = ()) -> list:
    """A question store holding only the given questions, each snoozed `days` ahead; questions whose `what` is in `desk` read
    as section desk, not carried (the kernel's store has no section, so the read is wrapped)."""
    from datetime import datetime, timedelta

    from levain.consent.questions import QuestionStore, local_today
    home = tmp_path / "levain_home"
    monkeypatch.setenv("LEVAIN_HOME", str(home))
    (home / "consent").mkdir(parents=True, mode=0o700)
    qs = QuestionStore(home / "consent")
    out = []
    for i, (what, days) in enumerate(specs):
        q = qs.file(agent="claude_code", agent_session_id=f"s{i}", what=what, options=[("a", "Yes"), ("b", "No")], rec="a", blocks="b", cwd="/w")
        day = (datetime.fromisoformat(local_today(datetime.now().astimezone())) + timedelta(days=days)).date().isoformat()
        qs.snooze(q.id, until=day, channel="cli", via="cli")
        out.append(q)
    orig = QuestionStore.open_questions

    def open_questions(self, *a, **kw):
        return [dataclasses.replace(q, section="desk", carried=False) if q.what in desk else q for q in orig(self, *a, **kw)]
    monkeypatch.setattr(QuestionStore, "open_questions", open_questions)
    return out


@needs_browser
def test_a_dated_desk_question_is_a_desk_later_line_and_the_head_stays(tmp_path: Path, monkeypatch) -> None:
    """F1 + F2 + F3: a dated desk question is no actionable card and no count on Desk owes; each tab draws only its own LATER
    lines; a strip of LATER rows alone keeps its head (tabs); the LATER lines carry their band number."""
    _later_home(tmp_path, monkeypatch, [("You later", 3), ("Desk later", 2)], desk=("Desk later",))
    _root, src, _ids = _install(tmp_path)
    lines = "Array.from(document.querySelectorAll('#ck-needs .needs-later-line'))"
    probe = ("(() => JSON.stringify({"
             " tabs: Array.from(document.querySelectorAll('#ck-needs [data-needs-tab]')).map(e => e.textContent),"
             f" later: {lines}.map(e => e.dataset.questionN),"
             f" bands: {lines}.map(e => e.querySelector('.needs-later-band').textContent),"
             " rows: document.querySelectorAll('#ck-needs .needs-row').length,"
             " empty: (document.querySelector('#ck-needs .needs-empty') || {}).textContent}))()")
    ready = f"{lines}.length === 1 && {lines}.every(e => e.querySelector('.needs-later-band').textContent !== '')"
    with _serve(src, surface_tokens={"browser": "T"}, install_class="reported") as (url, _httpd):
        vals = _drive(tmp_path, f"{url}/#write_token=T", [
            {"until": READY_ANY, "ms": 20000}, {"until": ready, "ms": 20000}, {"eval": probe},
            {"eval": "document.querySelector('#ck-needs [data-needs-tab=desk]').click()"},
            {"until": f"document.querySelector('#ck-needs [data-needs-tab=desk]').classList.contains('is-on') && {ready}", "ms": 20000},
            {"eval": probe}])["values"]
    got = [json.loads(v) for v in vals if isinstance(v, str) and v.startswith("{")]
    you, dsk = got[0], got[-1]
    assert you["tabs"] == ["Needs you (0)", "Desk owes (0)"] and you["later"] == ["1"] and you["rows"] == 0, you
    assert dsk["later"] == ["2"] and dsk["rows"] == 0 and dsk["empty"] == "The desk owes you nothing right now.", dsk
    assert all(b for b in you["bands"] + dsk["bands"]), (you, dsk)


@needs_browser
def test_equal_day_later_lines_follow_the_band_number(tmp_path: Path, monkeypatch) -> None:
    """F3: two lines of the same day are ordered by their band number, not by the order the rows came."""
    from levain.band_view import number_for_ping
    from levain.consent.questions import QuestionStore
    a, b = _later_home(tmp_path, monkeypatch, [("First filed", 3), ("Second filed", 3)])
    qs = QuestionStore(tmp_path / "levain_home" / "consent")
    assert number_for_ping(qs, b.id) is not None and number_for_ping(qs, a.id) is not None     # the second-filed takes the lower number
    _root, src, _ids = _install(tmp_path)
    lines = "Array.from(document.querySelectorAll('#ck-needs .needs-later-line'))"
    ready = f"{lines}.length === 2 && {lines}.every(e => e.querySelector('.needs-later-band').textContent !== '')"
    with _serve(src, surface_tokens={"browser": "T"}, install_class="reported") as (url, _httpd):
        vals = _drive(tmp_path, f"{url}/#write_token=T", [{"until": READY_ANY, "ms": 20000}, {"until": ready, "ms": 20000},
                      {"eval": f"JSON.stringify({lines}.map(e => [e.dataset.questionN, e.querySelector('.needs-later-band').textContent]))"}])["values"]
    got = json.loads(vals[-1])
    assert [n for n, _b in got] == [str(b.n), str(a.n)] and int(got[0][1]) < int(got[1][1]), got


@needs_browser
def test_the_page_never_rejudges_the_day_with_its_own_clock(tmp_path: Path, monkeypatch) -> None:
    """L3 r2 (codex, complement): the page's clock is not the install's zone, so a phone elsewhere would disagree with flow. A
    row is LATER exactly while the server sends the facet, whatever the page's clock says; the etag's install-zone date
    refreshes the view at midnight."""
    _file(tmp_path, monkeypatch)
    _root, src, _ids = _install(tmp_path)
    lines = [_ql(1, section="you", later="2026-11-10", laterWords="Tue 11-10")]
    probe = ("(() => { const K = window.LevainCockpitKinds; const ids = () => window.LevainNeeds.items(" + json.dumps(lines) + ").map(x => x.q.id);"
             " const at = (y, m, d) => { K.serverNow = () => new Date(y, m - 1, d, 12).getTime(); return ids(); };"
             " return JSON.stringify({before: at(2026, 11, 1), past: at(2026, 11, 12)}); })()")
    with _serve(src, surface_tokens={"browser": "T"}, install_class="reported") as (url, _httpd):
        vals = _drive(tmp_path, f"{url}/#write_token=T", [{"until": READY, "ms": 20000}, {"eval": probe}])["values"]
    got = json.loads(vals[-1])
    assert got == {"before": [], "past": []}, got


def test_the_strip_signature_carries_the_later_words() -> None:
    """F4: the words flip at midnight ("Tue 11-10" -> "Tue") with the same day, so the signature must hold them."""
    src = (HERE.parent / "levain" / "templates" / "web" / "dashboard_core.js").read_text(encoding="utf-8")
    sig = next(ln for ln in src.splitlines() if ln.lstrip().startswith("const sig = all.map(needsKey)"))
    assert sig.count("laterWords") == 2, sig         # the Needs you lines and the Desk owes lines


def test_the_later_facets_are_a_real_day_and_bounded_words() -> None:
    """F6: question_later is a real calendar day, question_later_words exactly the shape band_view.later_words writes."""
    from levain.cockpit.registry import FacetValueError
    validate_facets({"question_later": "2026-11-10", "question_later_words": "Tue 11-10"})
    for bad in ("2026-99-99", "2026-11-31", "11-10-2026", "2026-1-5", "", " 2026-11-10", "2026-11-10\n", 20261110):
        with pytest.raises(FacetValueError):
            validate_facets({"question_later": bad})
    for good in ("Mon", "Sun", "Tue 11-10", "Sat 01-03"):
        validate_facets({"question_later_words": good})
    for bad in ("", "x" * 41, "Tue\n11-10", "\x00", 5, "not a weekday", "Tue 13-01", "tue", "Tue 11-10 "):
        with pytest.raises(FacetValueError):
            validate_facets({"question_later_words": bad})


# ---- LATER on a phone (Phill 11-08, iPhone screenshot): one header, then number | day | headline, nothing wrapped mid-token ----

REAL_LATER = [
    ("TALK: trans-allies-ohio: Mon 11-09 before 20:00: send Jeanne the upgrade notice on Signal, then take the pre-2404 Linode snapshot. Draft and steps are in RUNBOOK 5.", 1),
    ("DECISION: flow: Friction day + prompts-logger session (also Levain C9 one-real-day run, C7 closed-lid test), moved to Mon 11-09 (469c). Start it?", 1),
    ("levain: TUE 11-10: read Levain 1.0's release essay draft 2 and fill its 17 [[PHILL]] slots; it ships with 1.0. Your word 14:59: (a)", 2),
    ("TALK: flow: Tue 11-10: open https://example.com/" + "a-very-long-path-segment-with-no-spaces-" * 6 + "end then report", 1),
]
LATER_PROBE = """(() => { const rows = Array.from(document.querySelectorAll('#ck-needs .needs-later-line'));
  const lines = (e) => Math.round(e.getBoundingClientRect().height / parseFloat(getComputedStyle(e).lineHeight || 16));
  const sec = document.querySelector('#ck-needs .needs-later'), head = sec && sec.querySelector('.needs-later-head');
  const lefts = rows.map(r => Math.round(r.querySelector('.needs-later-title').getBoundingClientRect().left));
  return JSON.stringify({ marks: document.querySelectorAll('#ck-needs .needs-later-mark').length, head: head ? head.textContent : null,
    n: rows.length, docW: document.documentElement.scrollWidth, winW: window.innerWidth,
    oneLine: rows.every(r => [r.querySelector('.needs-later-band'), r.querySelector('.needs-later-text')].every(e => e.getBoundingClientRect().height < 1.6 * parseFloat(getComputedStyle(e).fontSize) * 1.5)),
    lefts: lefts, titles: rows.map(r => r.querySelector('.needs-later-title').textContent) }); })()"""


@needs_browser
def test_later_on_a_phone_is_one_header_and_aligned_rows(tmp_path: Path, monkeypatch) -> None:
    import os
    _later_home(tmp_path, monkeypatch, REAL_LATER)
    _root, src, _ids = _install(tmp_path)
    shots = os.environ.get("LEVAIN_LATER_SHOTS")
    shot = lambda n, w: ([{"eval": "document.querySelector('#ck-needs .needs-later').scrollIntoView({block: 'center'}); 1"},
                          {"eval": "new Promise(r => setTimeout(r, 300)).then(() => 1)"}, {"shot": str(Path(shots) / n), "full": False}] if shots else [])
    if shots:
        Path(shots).mkdir(parents=True, exist_ok=True)
    ready = "document.querySelectorAll('#ck-needs .needs-later-line').length === 4 && Array.from(document.querySelectorAll('#ck-needs .needs-later-title')).every(e => e.textContent)"
    with _serve(src, surface_tokens={"browser": "T"}, install_class="reported") as (url, _httpd):
        vals = _drive(tmp_path, f"{url}/#write_token=T", [
            {"emulate": {"w": 390, "h": 844, "dpr": 2, "mobile": True}}, {"until": READY_ANY, "ms": 20000}, {"until": ready, "ms": 20000},
            {"eval": LATER_PROBE}, *shot("narrow.png", 390),
            {"emulate": {"w": 1440, "h": 900, "dpr": 1}}, {"until": ready, "ms": 20000}, *shot("wide.png", 1440)])["values"]
    g = json.loads(next(v for v in vals if isinstance(v, str) and v.startswith('{"marks"')))
    assert g["marks"] == 0 and g["n"] == 4 and g["head"] and g["head"].lower().startswith("later") and g["head"].endswith("4"), g
    assert g["oneLine"] and g["docW"] <= g["winW"], g                       # the number and the day never wrap, and the page does not scroll sideways
    assert len(set(g["lefts"])) == 1, g                                    # the headline column lines up across rows
    for t in g["titles"]:                                                  # a cut is on a word boundary, never inside "[[PHILL]]"
        assert not t.rstrip("…").endswith("[[PHI") and ("[[" not in t or "]]" in t), t
    long = next(t for t in g["titles"] if t.endswith("open…"))          # a headline whose only early space precedes a long URL cuts at that space, never mid-token
    assert "-spa" not in long and len(long) <= 161, long

"""`/?view=needs&item=<id>`: a deep link to one Needs-you card that does not depend on a sign request (a band row with fewer than
two options files no sign card, so its ping could only open the home view). The page opens the card's focus view and scrolls to
the strip's card; an id that is not waiting falls back to the plain Needs view with a one-line notice; the address is cleaned
and the link, like the other two, survives the write-token strip (the token rides the fragment)."""
from __future__ import annotations

import json
from pathlib import Path

from tests.test_cockpit_band_title import TITLE, _two_questions
from tests.test_cockpit_flip_parity import _drive, needs_browser
from tests.test_cockpit_k2a import _install, _serve
from tests.test_cockpit_k4a_needs import DESKTOP

READ = ("JSON.stringify({ title: " + TITLE + " || '', modal: !!document.getElementById('levain-modal'), search: location.search, y: Math.round(window.scrollY),"
        " note: (document.querySelector('.needs-gone-note') || {}).textContent || '' })")
ROWS = "document.querySelectorAll('#ck-needs .needs-row').length === 2 && 1"
SETTLE = {"eval": "new Promise(r => setTimeout(() => r(1), 2500))"}


def _open_link(tmp_path: Path, monkeypatch, query: str) -> dict:
    _two_questions(tmp_path, monkeypatch)
    _root, src, _ids = _install(tmp_path)
    with _serve(src, surface_tokens={"browser": "T"}, install_class="reported") as (url, _httpd):
        out = _drive(tmp_path, f"{url}/?{query}#write_token=T", [DESKTOP, {"until": ROWS, "ms": 40000}, SETTLE, {"eval": READ}])["values"]
    return json.loads(out[-1])


@needs_browser
def test_an_item_link_opens_that_card_and_scrolls_to_it_through_the_token_strip(tmp_path: Path, monkeypatch) -> None:
    got = _open_link(tmp_path, monkeypatch, "view=needs&item=question:2")
    assert got["modal"] and got["title"].endswith("· codex"), got      # question 2 is codex's, not the first card
    assert got["search"] == "" and got["note"] == "", got              # the address is cleaned, nothing said
    assert got["y"] > 100, got                                          # the strip's card was scrolled to


@needs_browser
def test_flows_np_id_names_the_same_card_as_its_card_number(tmp_path: Path, monkeypatch) -> None:
    got = _open_link(tmp_path, monkeypatch, "view=needs&item=np-00000001")     # card_number("np-00000001") == 2
    assert got["modal"] and got["title"].endswith("· codex") and got["search"] == "", got


@needs_browser
def test_an_unknown_item_falls_back_to_the_needs_view_with_a_notice(tmp_path: Path, monkeypatch) -> None:
    got = _open_link(tmp_path, monkeypatch, "view=needs&item=np-deadbeef")
    assert got["modal"] is False and got["search"] == "", got
    assert got["note"] == "That item is no longer waiting.", got
    assert got["y"] > 100, got                                          # still at the Needs strip, as the plain link goes


def _eleven_questions(tmp_path: Path, monkeypatch) -> None:
    from levain.consent.questions import QuestionStore    # as tests/test_cockpit_band_title.py builds its store
    home = tmp_path / "levain_home"
    monkeypatch.setenv("LEVAIN_HOME", str(home))
    (home / "consent").mkdir(parents=True, mode=0o700)
    store = QuestionStore(home / "consent")
    for i in range(11):
        store.file(agent="codex" if i == 10 else "claude_code", agent_session_id=f"s{i}", what=f"Question {i}",
                   options=[("a", "A"), ("b", "B")], rec="a", blocks="x", cwd="/w")


@needs_browser
def test_an_uppercase_np_id_names_the_same_card_as_its_lowercase_one(tmp_path: Path, monkeypatch) -> None:
    """The guard admits A-F, so `np-0000000A` is a link that reaches the page; card_number gives 1 + 0xA = 11, the codex card."""
    _eleven_questions(tmp_path, monkeypatch)
    _root, src, _ids = _install(tmp_path)
    with _serve(src, surface_tokens={"browser": "T"}, install_class="reported") as (url, _httpd):
        out = _drive(tmp_path, f"{url}/?view=needs&item=np-0000000A#write_token=T", [
            DESKTOP, {"until": "document.querySelectorAll('#ck-needs .needs-row').length >= 11 && 1", "ms": 40000}, SETTLE, {"eval": READ}])["values"]
    got = json.loads(out[-1])
    assert got["modal"] and "codex" in got["title"] and got["note"] == "", got


def test_a_consent_card_is_named_by_its_exact_id_only() -> None:
    """The card is `consent:<request id>`; a prefix clause (`consent:<item>-...`) let a short or truncated item open another request's card."""
    from tests._ke_dom import WEB
    js = (WEB / "dashboard_core.js").read_text(encoding="utf-8")
    body = js[js.index("function needsItemMatch"):js.index("function needsGoneNote")]
    assert "indexOf" not in body and 'item + "-"' not in body, body


# the page before it is paired: its Decisions panel read does not answer until the pairing reload (a read the box refuses does the same to the jump)
OFF_BOX = """(() => { const f = window.fetch; window.fetch = async (...a) => {
  if (String(a[0]).indexOf('/cockpit/panel/decisions.json') === 0 && !window.sessionStorage.getItem('paired')) return new Promise(() => {});
  return f.apply(window, a); }; })();"""
PENDING = "window.sessionStorage.getItem('levain_needs_item_pending')"


@needs_browser
def test_a_pending_item_survives_the_pairing_reload(tmp_path: Path, monkeypatch) -> None:
    """An unpaired page strips the query at once and pair_card.js reloads the clean `/`: the validated id rides sessionStorage across it."""
    _two_questions(tmp_path, monkeypatch)
    _root, src, _ids = _install(tmp_path)
    with _serve(src, surface_tokens={"browser": "T"}, install_class="reported") as (url, _httpd):
        out = _drive(tmp_path, f"{url}/?view=needs&item=question:2", [
            {"init": OFF_BOX}, DESKTOP, SETTLE, {"eval": "JSON.stringify({ search: location.search, pending: " + PENDING + " })"},
            {"eval": "window.sessionStorage.setItem('paired', '1'); location.reload(); 1"},
            {"until": ROWS, "ms": 40000}, SETTLE, {"eval": READ}, {"eval": PENDING}])["values"]
    before, after = json.loads(out[-6]), json.loads(out[-2])
    assert before["search"] == "" and json.loads(before["pending"])["id"] == "question:2", before
    assert after["modal"] and "codex" in after["title"], after       # the card opened on the clean address
    assert out[-1] is None, out[-1]                                             # and the pending id was used up


SLOW_DECISIONS = """(() => { const f = window.fetch; window.fetch = async (...a) => {
  if (String(a[0]).indexOf('/cockpit/panel/decisions.json') === 0) await new Promise((r) => setTimeout(r, 6000));
  return f.apply(window, a); }; })();"""


def _slow_decisions(tmp_path: Path, monkeypatch, press: bool) -> dict:
    _two_questions(tmp_path, monkeypatch)
    _root, src, _ids = _install(tmp_path)
    steps = [{"init": SLOW_DECISIONS}, DESKTOP]
    if press:
        steps.append({"tap": "document.querySelector('[data-theme-set=\"dark\"]')", "ms": 40000})
    steps += [{"until": ROWS, "ms": 40000}, {"eval": "new Promise(r => setTimeout(() => r(1), 9000))"}, {"eval": READ}]
    with _serve(src, surface_tokens={"browser": "T"}, install_class="reported") as (url, _httpd):
        out = _drive(tmp_path, f"{url}/?view=needs&item=question:2#write_token=T", steps)["values"]
    return json.loads(out[-1])


@needs_browser
def test_a_late_decisions_read_never_replaces_the_view_the_reader_chose(tmp_path: Path, monkeypatch) -> None:
    """Decisions is slow. Control: left alone, the late lines open the linked card. The reader presses something on the page that is not
    a touch or a wheel (the theme switch stands for any panel or nav control): that press ended the pending jump, so no card is opened."""
    assert _slow_decisions(tmp_path / "a", monkeypatch, press=False)["modal"] is True
    got = _slow_decisions(tmp_path / "b", monkeypatch, press=True)
    assert got["modal"] is False and got["note"] == "", got

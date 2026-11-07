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

# the open sheet locks the page (body position:fixed, top:-Y), so window.scrollY reads 0 behind it; the held place is -body.style.top
READ = ("JSON.stringify({ title: " + TITLE + " || '', modal: !!document.getElementById('levain-modal'), search: location.search, y: Math.round(window.scrollY || -parseFloat(document.body.style.top) || 0),"
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


@needs_browser
def test_an_answered_item_with_nothing_else_waiting_says_so_and_lands_on_needs(tmp_path: Path, monkeypatch) -> None:
    """The item was answered and the AUTHORITATIVE Decisions listing is empty: that is an answer, not a transient. The reader gets the
    "no longer waiting" note (never silence while the jump polls out an expired deadline)."""
    home = tmp_path / "levain_home"
    monkeypatch.setenv("LEVAIN_HOME", str(home))
    (home / "consent").mkdir(parents=True, mode=0o700)
    _root, src, _ids = _install(tmp_path)
    with _serve(src, surface_tokens={"browser": "T"}, install_class="reported") as (url, _httpd):
        out = _drive(tmp_path, f"{url}/?view=needs&item=question:2#write_token=T", [
            DESKTOP, {"until": "!!document.getElementById('ck-needs') && 1", "ms": 40000}, SETTLE, {"eval": READ}])["values"]
    got = json.loads(out[-1])
    assert got["note"] == "That item is no longer waiting." and got["modal"] is False and got["search"] == "", got


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
    """The guard admits A-F, so `np-0000000A` is a link that reaches the page; card_number gives 1 + 0xA = 11, the codex card.
    The strip folds after 6 rows (the approved layout), so the page DRAWS 6 rows and a "Show 5 more" row; card 11 is not drawn, and the
    link still opens its sheet (the read is of the sheet, not of a row)."""
    _eleven_questions(tmp_path, monkeypatch)
    _root, src, _ids = _install(tmp_path)
    with _serve(src, surface_tokens={"browser": "T"}, install_class="reported") as (url, _httpd):
        out = _drive(tmp_path, f"{url}/?view=needs&item=np-0000000A#write_token=T", [
            DESKTOP, {"until": "document.querySelectorAll('#ck-needs .needs-row').length === 6 && !!document.querySelector('#ck-needs .needs-more-row') && 1", "ms": 40000}, SETTLE, {"eval": READ}])["values"]
    got = json.loads(out[-1])
    assert got["modal"] and "codex" in got["title"] and got["note"] == "", got


def test_a_consent_card_is_named_by_its_exact_id_only() -> None:
    """The card is `consent:<request id>`; a prefix clause (`consent:<item>-...`) let a short or truncated item open another request's card."""
    from tests._ke_dom import WEB
    js = (WEB / "dashboard_core.js").read_text(encoding="utf-8")
    body = js[js.index("function needsItemMatch"):js.index("function needsGoneNote")]
    assert "indexOf" not in body and 'item + "-"' not in body, body


# the harness's init step reloads the page once, which would drop the link: open the link after it
GO_LINK = {"eval": "location.assign('/?view=needs&item=question:2#write_token=T'); 1"}

def _jump_body() -> str:
    from tests._ke_dom import WEB
    js = (WEB / "dashboard_core.js").read_text(encoding="utf-8")
    return js[js.index("function needsItemJump"):js.index("function needsDeepJump")]


def test_an_empty_or_partial_listing_never_says_no_longer_waiting() -> None:
    """The transient-empty case that destroyed the link before: an EMPTY listing is not an answer (the jump keeps polling until a listing
    holds the item or the deadline), and the notice is said only of a whole listing (no panel error, no note). The page does not re-read
    the panel on a timer inside the harness's window, so this is pinned on the code, not driven."""
    body = _jump_body()
    assert "lines.length > 0 || (dec.clean === true" in body and "needsDeepJump(run, deadline)" not in body, body
    assert "!dec.error && !dec.note" in body and body.index("!dec.error") < body.index("needsGoneNote()"), body


def test_the_link_keeps_nothing_across_a_reload() -> None:
    from tests._ke_dom import WEB
    js = (WEB / "dashboard_core.js").read_text(encoding="utf-8")
    assert "levain_needs_item_pending" not in js and "needsPending" not in js and "NEEDS_PENDING" not in js


SLOW_DECISIONS = """(() => { const f = window.fetch; window.fetch = async (...a) => {
  if (String(a[0]).indexOf('/cockpit/panel/decisions.json') === 0) await new Promise((r) => setTimeout(r, 6000));
  return f.apply(window, a); }; })();"""


def _slow_decisions(tmp_path: Path, monkeypatch, press: bool) -> dict:
    _two_questions(tmp_path, monkeypatch)
    _root, src, _ids = _install(tmp_path)
    steps = [{"init": SLOW_DECISIONS}, DESKTOP, GO_LINK]
    if press:
        steps.append({"tap": "document.querySelector('[data-theme-set=\"dark\"]')", "ms": 40000})
    steps += [{"until": ROWS, "ms": 40000}, {"eval": "new Promise(r => setTimeout(() => r(1), 9000))"}, {"eval": READ}]
    with _serve(src, surface_tokens={"browser": "T"}, install_class="reported") as (url, _httpd):
        out = _drive(tmp_path, f"{url}/#write_token=T", steps)["values"]
    return json.loads(out[-1])


@needs_browser
def test_a_late_decisions_read_never_replaces_the_view_the_reader_chose(tmp_path: Path, monkeypatch) -> None:
    """Decisions is slow. Control: left alone, the late lines open the linked card. The reader presses something on the page that is not
    a touch or a wheel (the theme switch stands for any panel or nav control): that press ended the pending jump, so no card is opened."""
    assert _slow_decisions(tmp_path / "a", monkeypatch, press=False)["modal"] is True
    got = _slow_decisions(tmp_path / "b", monkeypatch, press=True)
    assert got["modal"] is False and got["note"] == "", got


@needs_browser
def test_a_link_naming_the_items_own_id_opens_the_card_the_band_numbers_for_it(tmp_path: Path, monkeypatch) -> None:
    """11-06, np-0b62cd3b: flow numbers its cards with the BAND number (desk_cards.display_numbers), no longer 1 + the id's hex, so the ping
    `item=np-<hex>` named no card and the tap landed on the plain strip with "no longer waiting". The page now resolves the id through
    the band read (an item's `subject.id` is the id the store filed it under, its `question` the card's number). A foreign store's id is
    `np-...`; the local store's is `q-...`, the same path: codex's card (question 2) is named by its own id, which is no card number."""
    _home, store = _two_questions(tmp_path, monkeypatch)
    qid = store.get(2).id
    _root, src, _ids = _install(tmp_path)
    with _serve(src, surface_tokens={"browser": "T"}, install_class="reported") as (url, _httpd):
        out = _drive(tmp_path, f"{url}/?view=needs&item={qid}#write_token=T", [DESKTOP, {"until": ROWS, "ms": 40000}, SETTLE, {"eval": READ}])["values"]
    got = json.loads(out[-1])
    assert got["modal"] and got["title"].endswith("· codex") and got["search"] == "" and got["note"] == "", got


# the FIRST band read of the document is stale (a cached read from just before the item appeared) or refused (transient); later ones are real
FIRST_BAND = """(() => { const f = window.fetch; let n = 0; window.__bandCalls = 0; window.fetch = async (...a) => {
  if (String(a[0]).indexOf('/cockpit/band.json') === 0) {
    window.__bandCalls = ++n;
    if (n === 1 || (MODE === 'refuse' && n === 2)) {   // a refusal outlasts the panel's own retry, so only the jump's retry can recover
      if (MODE === 'refuse') throw new TypeError('network');
      const r = await f.apply(window, a); const j = await r.json(); j.items = [];
      return new Response(JSON.stringify(j), { status: 200, headers: { 'Content-Type': 'application/json' } });
    } }
  return f.apply(window, a); }; })();"""


def _first_band_bad(tmp_path: Path, monkeypatch, mode: str) -> dict:
    _home, store = _two_questions(tmp_path, monkeypatch)
    qid = store.get(2).id
    _root, src, _ids = _install(tmp_path)
    go = {"eval": f"location.assign('/?view=needs&item={qid}#write_token=T'); 1"}
    with _serve(src, surface_tokens={"browser": "T"}, install_class="reported") as (url, _httpd):
        out = _drive(tmp_path, f"{url}/#write_token=T", [{"init": FIRST_BAND.replace("MODE", json.dumps(mode))}, DESKTOP, go, {"until": ROWS, "ms": 40000}, SETTLE, {"eval": READ}])["values"]
    return json.loads(out[-1])


@needs_browser
def test_a_stale_cached_band_read_never_makes_a_waiting_card_gone(tmp_path: Path, monkeypatch) -> None:
    """L3 r1 MED: the band read the jump asks (bandFetch, cached ~2 s) may predate the item; a miss from it re-asks FRESH before the page
    says "no longer waiting". The first band read of the document here lacks every item; the card must still open, with no notice."""
    got = _first_band_bad(tmp_path, monkeypatch, "stale")
    assert got["modal"] and got["title"].endswith("· codex") and got["note"] == "", got


@needs_browser
def test_a_refused_band_read_is_retried_once_not_taken_for_gone(tmp_path: Path, monkeypatch) -> None:
    got = _first_band_bad(tmp_path, monkeypatch, "refuse")
    assert got["modal"] and got["title"].endswith("· codex") and got["note"] == "", got


def test_only_a_fresh_successful_band_read_concludes_no_longer_waiting() -> None:
    """Pinned on the code beside the driven cases: the notice is said only from the second (fresh) read's success; a refusal says nothing."""
    body = _jump_body()
    assert "bandFetch(fresh)" in body and "read(true)" in body and "gone(true)" in body and "gone(false)" in body, body
    assert body.count("gone(true)") == 2 and "else gone(true)" in body, body

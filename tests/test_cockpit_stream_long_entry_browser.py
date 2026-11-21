"""levain#227: the Stream panel with a LONG latest entry, in a real headless Chrome.

Phill: "the Stream panel: right now it is blank unless you click the Read All thing and I think this is because the latest entry
is too long to show in the panel". The Stream is a downstream `log` panel (flow registers it: rows of verb / target / actor / at,
newest first). The panel is a bounded card: a list that does not fit folds the rows below the fold and ends in a "Read all N"
foot. What the operator needs is that the TOP of the newest entry is always on the card, however long it is: the body is never
blank, the first entry's box starts inside the card and has height, and the "Read all" foot is there to open the rest.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.test_cockpit_flip_parity import _drive, needs_browser
from tests.test_cockpit_k2a import _install
from tests.test_cockpit_scroll_keep_page import _Served

PANEL = "document.querySelector('[data-ck-panel=\"stream\"]')"
SETTLE = {"eval": "new Promise(r => setTimeout(r, 900)).then(() => 1)"}
VIEWPORTS = {"phone390x800": {"w": 390, "h": 800, "dpr": 2, "mobile": True},
             "desk1280x1000": {"w": 1280, "h": 1000, "dpr": 1},
             "desk1280x620": {"w": 1280, "h": 620, "dpr": 1}}

PARA = ("The constellation settled the overnight reconciliation and the seat that owned the board row wrote its whole account "
        "of what happened, why the first attempt was refused, what the second attempt changed, and what is left for the "
        "morning, in one long paragraph that keeps going well past anything a card could hold in its own height. ")
SHAPES = {
    "one_long_paragraph": PARA * 14,
    "many_short_lines": " ".join(f"line {i}: a short step." for i in range(1, 160)),
    "long_unbroken_token": "tok-" + "x" * 3000,
    "markdown_code_block": "Report:\n```\n" + "\n".join(f"step {i}: ok" for i in range(1, 120)) + "\n```\nDone.",
}
ROWS = 6        # the long entry is the newest; five ordinary ones follow


def _episodes(long_text: str) -> list[dict]:
    rows = [{"verb": "wrap", "target": long_text, "actor": "desk", "at": "2026-11-20T12:00:00Z"}]
    for i in range(ROWS - 1):
        rows.append({"verb": "episode", "target": f"an ordinary earlier entry number {i}", "actor": "flow",
                     "at": f"2026-11-20T11:{50 - i:02d}:00Z"})
    return rows


def _serve(tmp_path: Path, rows: list[dict]) -> _Served:
    from levain.cockpit import ProviderSpec, Read
    from levain.cockpit.providers import build_default_cockpit

    _root, src, _ids = _install(tmp_path)
    ck = build_default_cockpit(src)
    ck.register(ProviderSpec("stream", "log", "Stream", "feed", lambda c: Read(value={"rows": rows}), region="operate", rank=26,
                             search_fields=("verb", "target", "actor"), empty="no constellation episodes."))
    return _Served(src, cockpit=ck, surface_tokens={"browser": "T"}, install_class="reported")


READ = ("(() => { const p = " + PANEL + "; if (!p) return null; const b = p.querySelector(':scope > .pbody'); if (!b) return null;"
        " const rows = [...b.querySelectorAll('.lrow')]; if (!rows.length) return null;"
        " const pr = p.getBoundingClientRect(), br = b.getBoundingClientRect(), r0 = rows[0].getBoundingClientRect();"
        " const foot = p.querySelector(':scope > .more-foot'); const fr = foot && foot.getBoundingClientRect();"
        " const shown = rows.filter(r => r.style.display !== 'none' && r.getBoundingClientRect().height > 0);"
        " const first = rows[0].querySelector('.lsent');"
        " return JSON.stringify({panelH: Math.round(pr.height), panelTop: Math.round(pr.top), bodyTop: Math.round(br.top),"
        " bodyH: Math.round(br.height), bodyBottom: Math.round(br.bottom), bodyScroll: b.scrollTop, bodyScrollH: b.scrollHeight,"
        " bodyClientH: b.clientHeight, rows: rows.length, shown: shown.length, firstDisplay: rows[0].style.display,"
        " firstFolded: !!rows[0].dataset.folded, firstTop: Math.round(r0.top), firstBottom: Math.round(r0.bottom),"
        " firstH: Math.round(r0.height), firstText: first ? first.textContent.length : 0,"
        " foot: foot ? foot.textContent : null, footHidden: foot ? foot.hidden : null, footH: fr ? Math.round(fr.height) : 0,"
        " bodyText: b.innerText.trim().length, clipped: p.classList.contains('is-clipped'), stackCap: p.dataset.stackCap || null}); })()")


def _top_visible(g: dict) -> list[str]:
    """What is wrong with the card, in words (empty list: the top of the newest entry is on the card)."""
    bad = []
    if g["shown"] == 0 or g["firstFolded"] or g["firstDisplay"] == "none":
        bad.append(f"the newest entry is folded away (shown rows {g['shown']} of {g['rows']}), the body draws no entry")
    if g["firstH"] <= 0:
        bad.append("the newest entry has no height")
    if g["firstTop"] < g["bodyTop"] - 1 or g["firstTop"] >= g["bodyBottom"] - 1:
        bad.append(f"the newest entry starts outside the body (top {g['firstTop']}, body {g['bodyTop']}..{g['bodyBottom']})")
    if g["bodyH"] <= 0 or g["bodyText"] == 0:
        bad.append("the body is blank")
    if g["foot"] is None or g["footHidden"] or g["footH"] <= 0:
        bad.append(f"no visible 'Read all' foot (foot={g['foot']!r})")
    elif not (g["foot"].startswith("Read all") or g["foot"].endswith(" more")):
        bad.append(f"the foot says {g['foot']!r}: neither 'Read all' nor 'N more'")
    return bad


@needs_browser
@pytest.mark.parametrize("vp", sorted(VIEWPORTS))
@pytest.mark.parametrize("shape", sorted(SHAPES))
def test_a_long_newest_entry_never_leaves_the_stream_panel_blank(shape, vp, tmp_path: Path) -> None:
    with _serve(tmp_path, _episodes(SHAPES[shape])) as url:
        out = _drive(tmp_path, f"{url}/", [
            {"emulate": VIEWPORTS[vp]},
            {"until": READ},
            SETTLE,
            {"eval": READ},
        ])
    g = json.loads(out["values"][-1])
    assert not _top_visible(g), (shape, vp, _top_visible(g), g)


# the page's CSP is style-src 'self' (no inline <style>): a constructed sheet is the way a test lays a rule over the served CSS
UNCLAMP = ("const s = new CSSStyleSheet(); s.replaceSync('#board .lrow .lsent { display: block !important; -webkit-line-clamp: unset !important;"
           " overflow: visible !important; }'); document.adoptedStyleSheets = [s];")


@needs_browser
@pytest.mark.parametrize("vp", sorted(VIEWPORTS))
def test_the_fold_alone_keeps_the_first_entry_when_the_entry_is_not_clamped(vp, tmp_path: Path) -> None:
    """The fold's own rule, with the per-entry clamp taken away: a first entry taller than the whole body stays (clipped under the
    fade), and the foot is there; the fold never takes the first row."""
    with _serve(tmp_path, _episodes(SHAPES["one_long_paragraph"])) as url:
        out = _drive(tmp_path, f"{url}/", [
            {"emulate": VIEWPORTS[vp]},
            {"init": UNCLAMP},
            {"until": READ},
            SETTLE,
            {"eval": READ},
        ])
    g = json.loads(out["values"][-1])
    assert g["firstH"] > g["bodyH"], g            # the entry really is taller than the card's body (the case under test)
    assert not _top_visible(g), (vp, _top_visible(g), g)


@needs_browser
def test_the_clamp_is_on_the_card_only_the_focus_view_holds_the_whole_entry(tmp_path: Path) -> None:
    long_text = SHAPES["one_long_paragraph"]
    probe = ("(() => { const m = document.querySelector('#levain-modal'); if (!m) return null; const s = m.querySelector('.lrow .lsent');"
             " if (!s) return null; return JSON.stringify({len: s.textContent.length, clamp: getComputedStyle(s).webkitLineClamp,"
             " cut: s.scrollHeight > s.clientHeight + 1}); })()")
    with _serve(tmp_path, _episodes(long_text)) as url:
        out = _drive(tmp_path, f"{url}/", [
            {"emulate": VIEWPORTS["desk1280x1000"]},
            {"until": READ},
            SETTLE,
            {"eval": READ},
            {"eval": f"{PANEL}.querySelector('.pexpand').click(); 1"},
            {"until": probe},
        ])
    card = json.loads(out["values"][-3])
    assert not _top_visible(card), card
    assert card["firstText"] == len("wrap · " + long_text), card     # the card holds the whole entry, clamped by style only
    modal = json.loads(out["values"][-1])
    assert modal["clamp"] in ("none", "", None) and not modal["cut"], modal              # the focus view shows all of it


# ---- fix round 1: the clamp is guarded, a cut entry has its own caret, an ordinary entry is not cut, the "Read all 50" foot, a search hit ----
CLAMP_LINES = 8     # the card's per-entry clamp (dashboard.css): the tests below fail when the rule is gone, loses its scope, or changes depth

WORDS = ("the seat wrote its account of what changed and why the first attempt was refused so the second one waited for the "
         "review lane to finish and the desk then closed the board row with a note for the morning").split()


def _words(n: int) -> str:
    """Ordinary prose of exactly n characters."""
    out, i = "", 0
    while len(out) < n:
        out += (" " if out else "") + WORDS[i % len(WORDS)]
        i += 1
    return out[:n].rstrip()


def _log(texts: list[str], actor: str = "desk") -> list[dict]:
    return [{"verb": "wrap", "target": t, "actor": actor, "at": f"2026-11-20T11:{59 - i:02d}:00Z"} for i, t in enumerate(texts)]


STATE = ("(() => { const p = " + PANEL + "; if (!p) return null; const rows = [...p.querySelectorAll('.lrow')]; if (!rows.length) return null;"
         " return JSON.stringify(rows.map(r => { const s = r.querySelector('.lsent'), t = r.querySelector(':scope > .clause-toggle');"
         " const sr = s.getBoundingClientRect(), lh = parseFloat(getComputedStyle(s).lineHeight);"
         " return {folded: !!r.dataset.folded, open: r.classList.contains('row-expanded'), hasMore: r.classList.contains('has-more'),"
         " lines: lh ? Math.round(sr.height / lh * 10) / 10 : 0, h: sr.height, lh: lh, cut: s.scrollHeight > s.clientHeight, len: s.textContent.length,"
         " toggle: t ? {tag: t.tagName, shown: getComputedStyle(t).display !== 'none' && t.getBoundingClientRect().height > 0,"
         " expanded: t.getAttribute('aria-expanded'), label: t.getAttribute('aria-label')} : null}; })); })()")
ENTER = {"key": "Enter", "vk": 13, "text": "\r"}
ROW0_TOGGLE = PANEL + ".querySelector('.lrow > .clause-toggle')"


def _state(out: dict, at: int = -1) -> list[dict]:
    return json.loads(out["values"][at])


@needs_browser
@pytest.mark.parametrize("vp", ["phone390x800", "desk1280x1000"])
def test_the_card_clamp_bounds_a_long_entry_and_the_focus_view_does_not(vp, tmp_path: Path) -> None:
    """Guards the stylesheet rule itself: an entry's rendered height on the card is at most CLAMP_LINES of its line height, and
    in the focus view the same entry is not bounded. Deleting the rule, scoping it past #board, or changing its depth fails this."""
    long_text = SHAPES["one_long_paragraph"]
    modal_probe = ("(() => { const m = document.querySelector('#levain-modal'); if (!m) return null; const s = m.querySelector('.lrow .lsent');"
                   " if (!s) return null; return JSON.stringify({h: s.getBoundingClientRect().height, lh: parseFloat(getComputedStyle(s).lineHeight)}); })()")
    with _serve(tmp_path, _episodes(long_text)) as url:
        out = _drive(tmp_path, f"{url}/", [
            {"emulate": VIEWPORTS[vp]},
            {"until": STATE},
            SETTLE,
            {"eval": STATE},
            {"eval": f"{PANEL}.querySelector('.pexpand').click(); 1"},
            {"until": modal_probe},
            {"eval": modal_probe},
        ])
    first = _state(out, -4)[0]
    assert first["lh"] > 0 and first["h"] <= CLAMP_LINES * first["lh"] + 1, first         # bounded on the card
    assert first["h"] >= (CLAMP_LINES - 1) * first["lh"], first                            # and it is the clamp holding it, not an empty row
    modal = json.loads(out["values"][-1])
    assert modal["h"] > 2 * CLAMP_LINES * modal["lh"], modal                               # the focus view shows the entry, not its first lines


@needs_browser
@pytest.mark.parametrize("chars", [120, 160, 200])
def test_an_ordinary_entry_is_not_cut_on_a_phone(chars, tmp_path: Path) -> None:
    """The card is narrow, so the clamp is sized for a phone: an ordinary entry (120 to 200 characters of target) fills at most the clamp.
    A line that is cut shows its caret and a line that is whole shows none."""
    with _serve(tmp_path, _log([_words(chars)] + [f"an ordinary earlier entry number {i}" for i in range(4)])) as url:
        out = _drive(tmp_path, f"{url}/", [{"emulate": VIEWPORTS["phone390x800"]}, {"until": STATE}, SETTLE, {"eval": STATE}])
    first = _state(out)[0]
    print(f"ordinary entry of {chars} target characters (+ verb): {first['lines']} lines at 390 px, clamp {CLAMP_LINES}")
    assert first["toggle"] and first["toggle"]["shown"] == first["cut"], first             # a caret exactly when the text is cut
    if chars <= 160:
        assert not first["cut"] and first["lines"] <= CLAMP_LINES, first


@needs_browser
def test_a_cut_entry_has_its_own_caret_that_opens_and_closes_that_one_row(tmp_path: Path) -> None:
    long_text = _words(450)
    with _serve(tmp_path, _log([long_text] + [f"an ordinary earlier entry number {i}" for i in range(4)])) as url:
        out = _drive(tmp_path, f"{url}/", [
            {"emulate": VIEWPORTS["phone390x800"]},
            {"until": f"{PANEL} && {PANEL}.querySelector('.lrow.has-more') && 1"},
            SETTLE,
            {"eval": STATE},
            {"eval": f"{ROW0_TOGGLE}.focus(); document.activeElement === {ROW0_TOGGLE} && 1"},     # reachable by the keyboard
            ENTER,
            {"until": f"{PANEL}.querySelector('.lrow.row-expanded') && 1"},
            {"eval": STATE},
            ENTER,
            {"until": f"!{PANEL}.querySelector('.lrow.row-expanded') && 1"},
            {"eval": STATE},
        ])
    closed, opened, again = _state(out, -6), _state(out, -3), _state(out, -1)
    assert closed[0]["toggle"]["tag"] == "BUTTON" and closed[0]["toggle"]["shown"], closed[0]
    assert closed[0]["toggle"]["expanded"] == "false" and closed[0]["cut"], closed[0]
    assert all(not r["toggle"]["shown"] for r in closed[1:] if not r["folded"]), closed       # an entry that fits shows none
    assert opened[0]["open"] and opened[0]["toggle"]["expanded"] == "true" and not opened[0]["cut"], opened[0]
    assert opened[0]["len"] == len("wrap · " + long_text), opened[0]                            # the whole entry is on the row
    assert not any(r["open"] for r in opened[1:]), opened                                      # only that one row opened
    assert not again[0]["open"] and again[0]["toggle"]["expanded"] == "false" and again[0]["cut"], again[0]
    assert again[0]["h"] <= CLAMP_LINES * again[0]["lh"] + 1, again[0]


@needs_browser
def test_an_opened_entry_stays_open_when_the_panel_is_read_again(tmp_path: Path) -> None:
    mark = f"window.__old = {PANEL}.querySelector('.lrow'); 1"
    again = ("new Promise(r => window.LevainCockpitView.refetchPanel('stream').then(x => r(x === 'applied' && window.__old.isConnected === false"
             " ? 'repainted' : String(x) + ' old row still on the page: ' + window.__old.isConnected)))")
    with _serve(tmp_path, _log([_words(450)] + [f"an ordinary earlier entry number {i}" for i in range(4)])) as url:
        out = _drive(tmp_path, f"{url}/", [
            {"emulate": VIEWPORTS["phone390x800"]},
            {"until": f"{PANEL} && {PANEL}.querySelector('.lrow.has-more') && 1"},
            SETTLE,
            {"eval": f"{ROW0_TOGGLE}.click(); 1"},
            {"until": f"{PANEL}.querySelector('.lrow.row-expanded') && 1"},
            {"eval": mark},
            {"eval": again},
            SETTLE,
            {"eval": STATE},
        ])
    assert out["values"][-3] == "repainted", out["values"][-3]
    assert _state(out)[0]["open"] and _state(out)[0]["toggle"]["expanded"] == "true", _state(out)[0]


@needs_browser
@pytest.mark.parametrize("vp", ["phone390x800", "desk1280x1000"])
def test_fifty_rows_and_a_long_first_entry_read_read_all_50_with_the_first_entry_on_the_card(vp, tmp_path: Path) -> None:
    """The foot Phill saw: a list with its own "show all 50" link, folded, ends in "Read all 50"; the newest entry's top is on the card."""
    texts = [SHAPES["one_long_paragraph"]] + [f"an ordinary earlier entry number {i}" for i in range(49)]
    with _serve(tmp_path, _log(texts)) as url:
        out = _drive(tmp_path, f"{url}/", [{"emulate": VIEWPORTS[vp]}, {"until": READ}, SETTLE, {"eval": READ}])
    g = json.loads(out["values"][-1])
    assert g["foot"] == "Read all 50", g
    assert not _top_visible(g), (vp, _top_visible(g), g)


@needs_browser
def test_a_search_hit_past_the_clamp_opens_its_row(tmp_path: Path) -> None:
    """A row the card's filter matches, and the clamp would cut, opens whole (twelve rows: the card draws a filter box for them)."""
    texts = [f"an ordinary earlier entry number {i}" for i in range(12)]
    texts[1] = _words(450) + " quokka"
    typed = (f"(() => {{ const i = {PANEL}.querySelector('.xfilter-input'); i.value = 'quokka'; i.dispatchEvent(new Event('input', {{bubbles: true}})); return 1; }})()")
    with _serve(tmp_path, _log(texts)) as url:
        out = _drive(tmp_path, f"{url}/", [
            {"emulate": VIEWPORTS["phone390x800"]},
            {"until": f"{PANEL} && {PANEL}.querySelector('.xfilter-input') && {PANEL}.querySelector('.lrow') && 1"},
            SETTLE,
            {"eval": STATE},
            {"eval": typed},
            {"until": f"{PANEL}.querySelectorAll('.lrow').length === 1 && 1"},
            SETTLE,
            {"eval": STATE},
        ])
    before, after = _state(out, -5), _state(out)
    assert not before[1]["open"], before                                                       # closed before the search
    assert len(after) == 1 and after[0]["open"] and not after[0]["cut"], after                 # the search opened the row: its end is on the card


@needs_browser
def test_a_cut_entry_the_fold_hides_is_still_measured_for_its_caret(tmp_path: Path) -> None:
    """The fold hides a row that does not fit; a row is measured before that, so it has its caret when a later fold shows it again."""
    texts = ["an ordinary earlier entry", _words(450)] + [f"an ordinary later entry number {i}" for i in range(4)]
    with _serve(tmp_path, _log(texts)) as url:
        out = _drive(tmp_path, f"{url}/", [{"emulate": VIEWPORTS["phone390x800"]}, {"until": STATE}, SETTLE, {"eval": STATE}])
    rows = _state(out)
    assert rows[1]["folded"] and rows[1]["hasMore"], rows[1]


# ---- fix round 2: a search hit can be closed, rows keep distinct identities, carried state is spent, the caret follows the width ----
def _typed(q: str) -> str:
    return (f"(() => {{ const i = {PANEL}.querySelector('.xfilter-input'); i.value = '{q}'; "
            "i.dispatchEvent(new Event('input', {bubbles: true})); return 1; })()")


def _hits(n: int) -> list[str]:
    """Twelve rows: the first n long and matching 'quokka', the rest ordinary."""
    return [f"{_words(450)} quokka" if i < n else f"an ordinary earlier entry number {i}" for i in range(12)]


@needs_browser
@pytest.mark.parametrize("vp", ["phone390x800", "desk1280x1000"])
def test_a_search_hit_row_can_be_closed_and_stays_closed(vp, tmp_path: Path) -> None:
    """With a query active, every long match opens once; its caret then closes it, and a re-measure or a resize never opens it again."""
    smaller = dict(VIEWPORTS[vp], h=VIEWPORTS[vp]["h"] - 100)
    with _serve(tmp_path, _log(_hits(3))) as url:
        out = _drive(tmp_path, f"{url}/", [
            {"emulate": VIEWPORTS[vp]},
            {"until": f"{PANEL} && {PANEL}.querySelector('.xfilter-input') && {PANEL}.querySelector('.lrow') && 1"},
            SETTLE,
            {"eval": _typed("quokka")},
            {"until": f"{PANEL}.querySelectorAll('.lrow').length === 3 && 1"},
            SETTLE,
            {"eval": STATE},
            {"eval": f"{ROW0_TOGGLE}.click(); 1"},
            SETTLE,
            {"eval": STATE},
            {"emulate": smaller},
            SETTLE,
            {"eval": STATE},
        ])
    opened, closed, resized = _state(out, -6), _state(out, -3), _state(out, -1)
    assert all(r["open"] for r in opened), opened                                   # every match opened once, on the search
    for rows in (closed, resized):
        assert not rows[0]["open"] and rows[0]["toggle"]["expanded"] == "false", rows[0]   # the reader's close stands
        assert rows[0]["cut"] and rows[0]["toggle"]["shown"], rows[0]                       # and the caret to open it again is there
        assert rows[1]["open"] and rows[2]["open"], rows                                    # the other matches are as they were


@needs_browser
def test_a_search_hit_opened_by_the_search_says_so_on_its_caret(tmp_path: Path) -> None:
    with _serve(tmp_path, _log(_hits(1))) as url:
        out = _drive(tmp_path, f"{url}/", [
            {"emulate": VIEWPORTS["phone390x800"]},
            {"until": f"{PANEL} && {PANEL}.querySelector('.xfilter-input') && {PANEL}.querySelector('.lrow') && 1"},
            SETTLE,
            {"eval": _typed("quokka")},
            {"until": f"{PANEL}.querySelectorAll('.lrow').length === 1 && 1"},
            SETTLE,
            {"eval": STATE},
        ])
    row = _state(out)[0]
    assert row["open"] and row["toggle"]["expanded"] == "true" and row["toggle"]["label"] == "collapse full text", row


def _same_time(texts: list[str]) -> list[dict]:
    return [{"verb": "wrap", "target": t, "actor": "desk", "at": "2026-11-20T11:59:00Z"} for t in texts]


@needs_browser
def test_two_entries_of_one_second_actor_and_length_open_one_at_a_time(tmp_path: Path) -> None:
    """Same timestamp, actor and length used to share one keep key: opening one opened both after a redraw."""
    alpha = ("alpha " * 100)[:450].strip()
    bravo = ("bravo " * 100)[:450].strip()
    again = "new Promise(r => window.LevainCockpitView.refetchPanel('stream').then(x => r(String(x))))"
    keys = f"JSON.stringify([...{PANEL}.querySelectorAll('.lrow')].map(r => r.dataset.keepKey))"
    with _serve(tmp_path, _same_time([alpha, bravo, bravo])) as url:
        out = _drive(tmp_path, f"{url}/", [
            {"emulate": VIEWPORTS["phone390x800"]},
            {"until": f"{PANEL} && {PANEL}.querySelectorAll('.lrow.has-more').length === 3 && 1"},
            SETTLE,
            {"eval": keys},
            {"eval": f"{PANEL}.querySelectorAll('.lrow > .clause-toggle')[1].click(); 1"},
            {"until": f"{PANEL}.querySelector('.lrow.row-expanded') && 1"},
            {"eval": again},
            SETTLE,
            {"eval": STATE},
        ])
    k = json.loads(out["values"][-6])
    assert len(set(k)) == 3, k                                                      # three entries, three identities (the two identical ones too)
    assert [r["open"] for r in _state(out)] == [False, True, False], _state(out)    # only the one the reader opened


@needs_browser
def test_a_tall_card_with_one_long_entry_has_its_caret(tmp_path: Path) -> None:
    """The body is taller than its content (nothing overflows): the long entry is still cut by the clamp and still has its caret."""
    body = (f"(() => {{ const b = {PANEL}.querySelector(':scope > .pbody'); return JSON.stringify({{over: b.scrollHeight > b.clientHeight + 1,"
            " h: b.clientHeight, sh: b.scrollHeight}); })()")
    with _serve(tmp_path, _log([_words(450), "a short entry"])) as url:
        out = _drive(tmp_path, f"{url}/", [
            {"emulate": {"w": 1280, "h": 2400, "dpr": 1}},
            {"until": STATE},
            SETTLE,
            {"eval": body},
            {"eval": STATE},
        ])
    geo = json.loads(out["values"][-2])
    rows = _state(out)
    assert not geo["over"], geo                                                     # the geometry under test: the body does not overflow
    assert rows[0]["cut"] and rows[0]["hasMore"] and rows[0]["toggle"]["shown"], rows[0]
    assert not rows[1]["cut"] and not rows[1]["toggle"]["shown"], rows[1]


@needs_browser
def test_a_cut_entry_has_a_working_caret_at_desktop_width(tmp_path: Path) -> None:
    """At 1280 px the card is narrow and an ordinary entry is cut: its caret shows and opens that one row, as on a phone."""
    with _serve(tmp_path, _log([_words(200)] + [f"an ordinary earlier entry number {i}" for i in range(4)])) as url:
        out = _drive(tmp_path, f"{url}/", [
            {"emulate": VIEWPORTS["desk1280x1000"]},
            {"until": STATE},
            SETTLE,
            {"eval": STATE},
            {"eval": f"{ROW0_TOGGLE}.focus(); document.activeElement === {ROW0_TOGGLE} && 1"},
            ENTER,
            {"until": f"{PANEL}.querySelector('.lrow.row-expanded') && 1"},
            {"eval": STATE},
        ])
    closed, opened = _state(out, -4), _state(out)
    assert closed[0]["cut"] and closed[0]["toggle"]["shown"], closed[0]
    assert not any(r["toggle"]["shown"] for r in closed[1:] if not r["folded"] and not r["cut"]), closed
    assert opened[0]["open"] and not opened[0]["cut"] and opened[0]["toggle"]["expanded"] == "true", opened[0]


@needs_browser
def test_a_closed_entry_stays_closed_when_the_panel_is_read_again_after_a_redraw(tmp_path: Path) -> None:
    """Open a row, let a whole-board redraw restore it, close it: the next panel read within the carry window must not reopen it."""
    again = "new Promise(r => window.LevainCockpitView.refetchPanel('stream').then(x => r(String(x))))"
    with _serve(tmp_path, _log([_words(450)] + [f"an ordinary earlier entry number {i}" for i in range(4)])) as url:
        out = _drive(tmp_path, f"{url}/", [
            {"emulate": VIEWPORTS["phone390x800"]},
            {"until": f"{PANEL} && {PANEL}.querySelector('.lrow.has-more') && 1"},
            SETTLE,
            {"eval": f"{ROW0_TOGGLE}.click(); 1"},
            {"until": f"{PANEL}.querySelector('.lrow.row-expanded') && 1"},
            {"eval": "window.LevainDashboard.redraw(); 1"},
            SETTLE,
            {"eval": STATE},
            {"eval": f"{ROW0_TOGGLE}.click(); 1"},
            {"until": f"!{PANEL}.querySelector('.lrow.row-expanded') && 1"},
            {"eval": again},
            SETTLE,
            {"eval": STATE},
        ])
    restored, closed_again = _state(out, -6), _state(out)
    assert restored[0]["open"], restored[0]                                         # the redraw gave the open row back
    assert not closed_again[0]["open"] and closed_again[0]["toggle"]["expanded"] == "false", closed_again[0]   # the close stands


@needs_browser
def test_the_caret_follows_the_width_after_the_first_paint(tmp_path: Path) -> None:
    """An entry that fits one width and exceeds the clamp at another gets (and loses) its caret when the window changes."""
    wide, narrow = {"w": 1920, "h": 1000, "dpr": 1}, VIEWPORTS["desk1280x1000"]
    rows = _log([_words(200)] + [f"an ordinary earlier entry number {i}" for i in range(4)])
    with _serve(tmp_path, rows) as url:
        out = _drive(tmp_path, f"{url}/", [
            {"emulate": wide},
            {"until": STATE},
            SETTLE,
            {"eval": STATE},
            {"emulate": narrow},
            SETTLE,
            {"eval": STATE},
            {"emulate": wide},
            SETTLE,
            {"eval": STATE},
        ])
    fits, cut, fits_again = _state(out, -5), _state(out, -3), _state(out, -1)
    assert not fits[0]["cut"] and not fits[0]["toggle"]["shown"], fits[0]           # fits at 1920: no caret
    assert cut[0]["cut"] and cut[0]["toggle"]["shown"], cut[0]                      # narrowed: cut, so the caret appears
    assert not fits_again[0]["cut"] and not fits_again[0]["toggle"]["shown"], fits_again[0]   # widened: the caret goes

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

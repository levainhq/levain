"""levain#182: a collapsed card on Home shrinks to its header.

Home gives its cards fixed heights with ``!important``, and a folded card's plain ``height: auto`` could not outrank them. The fold also
hides every child but the header with ``display: none``, which a child's own display rule can outrank. The cascade check needs no browser;
the measured check drives the served page at the WIDTHS below and needs Chrome."""

from __future__ import annotations

import json
import re
from pathlib import Path

from tests.test_cockpit_desk_owes_count import _band
from tests.test_cockpit_flip_parity import BOARD_SETTLED, _drive, needs_browser
from tests.test_cockpit_k2a import _install, _serve

CSS = Path(__file__).resolve().parent.parent / "levain" / "templates" / "web" / "dashboard.css"
HOME = 'body.ia[data-ia-place="home"]'
FOLD_HIDE = ".panel.ck-collapsed > :not(.phead)"
WIDTHS = (390, 820, 1440)
HEIGHT = 900
SLACK = 4                  # the card's own border and rounding, with room
READY = {"until": "document.body.dataset.iaPlace === 'home' && document.querySelector('.ck-zgrid .panel[data-ck-panel=\"tray\"] .ck-lay-fold')"
                  " && document.querySelector('[data-ck-panel=\"tray\"] > .more-foot[data-list-more]') && 1", "ms": 30000}


def _rules() -> list[tuple[str, str, tuple[str, ...]]]:
    """Every ``selector { declarations }`` of the stylesheet in source order, with the at-rule preludes it sits inside."""
    css = re.sub(r"/\*.*?\*/", "", CSS.read_text(encoding="utf-8"), flags=re.S)
    out: list[tuple[str, str, tuple[str, ...]]] = []
    stack: list[str] = []
    buf = ""
    for ch in css:
        if ch == "{":
            stack.append(buf.strip())
            buf = ""
        elif ch == "}":
            if buf.strip():
                out.append((stack[-1], buf, tuple(stack[:-1])))
            buf = ""
            stack.pop()
        else:
            buf += ch
    return out


def test_a_folded_card_outranks_every_fixed_home_height_and_the_fold_hides_with_importance() -> None:
    """Proves the stylesheet's shape: the Home fold rule is top level and comes after every fixed ``px !important`` Home height, and the
    fold's hide rule is ``!important``. It does not prove the cascade outcome (specificity, a height of another unit, a child's own display);
    the browser test measures that."""
    rules = _rules()
    fixed = [i for i, (sel, decl, _at) in enumerate(rules)
             if sel.startswith(HOME) and ".panel" in sel and "ck-collapsed" not in sel and re.search(r"(?<![-\w])height:\s*[\d.]+px\s*!important", decl)]
    assert fixed, "Home draws no fixed-height card, so this check would prove nothing"
    folded = [i for i, (sel, decl, _at) in enumerate(rules)
              if any(s.strip() == f"{HOME} .panel.ck-collapsed" for s in sel.split(",")) and re.search(r"(?<![-\w])height:\s*auto\s*!important", decl)]
    assert folded, "no Home rule lets a folded card size to its header (height: auto !important)"
    assert min(folded) > max(fixed), "the folded-card rule must come after every fixed Home height: equal specificity, the later wins"
    assert all(not rules[i][2] for i in folded), ("the folded-card rule sits inside an at-rule, so it does not apply at every width", [rules[i][2] for i in folded])
    hide = [decl for sel, decl, _at in rules if any(s.strip() == FOLD_HIDE for s in sel.split(","))]
    assert hide, "the fold's hide rule is gone"
    assert all(re.search(r"(?<![-\w])display:\s*none\s*!important", d) for d in hide), "a child's own display rule can outrank a fold hide rule that is not !important"


# Every kind of fixed Home height is measured on a card the fixture draws. The heights are keyed by a card's data-ck-panel, so a card the fixture
# lacks is read under the key whose height is being measured ("as"), for the length of one synchronous read: its real key is put back before
# the page can redraw. A card whose height cannot be read this way fails the test rather than going unmeasured.
CASES = [
    {"label": "list", "card": "tray", "as": None},
    {"label": "own", "card": "edits", "as": None},
    {"label": "machine", "card": "edits", "as": "agents"},
    {"label": "work", "card": "work_board", "as": "board"},
]
MEASURE = """((CASES) => {
  const grid = '.ck-zgrid', page = () => document.documentElement.scrollHeight;
  const head = (p) => p.querySelector(':scope > .phead'), fold = (p) => p.querySelector(':scope > .phead .ck-lay-fold');
  const kids = (p) => [...p.children].filter((c) => !c.classList.contains('phead'));
  const shownIn = (p) => kids(p).filter((c) => getComputedStyle(c).display !== 'none').map((c) => (p.dataset.ckPanel || '?') + ' > ' + c.tagName.toLowerCase() + '.' + c.className);
  const read = (p) => ({ folded: p.classList.contains('ck-collapsed'), panel: Math.round(p.getBoundingClientRect().height), head: Math.round(head(p).getBoundingClientRect().height),
    kids: kids(p).length, shown: shownIn(p), more: !!p.querySelector(':scope > .more-foot[data-list-more]') });
  const cards = {};
  for (const c of CASES) {
    const p = document.querySelector(grid + ' .panel[data-ck-panel="' + c.card + '"]');
    if (!p || !fold(p) || !p.getClientRects().length) { cards[c.label] = null; continue; }
    const real = p.dataset.ckPanel;
    if (c.as) p.dataset.ckPanel = c.as;
    try { const open = read(p); fold(p).click(); const shut = read(p); fold(p).click(); cards[c.label] = { open, shut, reopened: !p.classList.contains('ck-collapsed') }; }
    finally { p.dataset.ckPanel = real; }
  }
  const openPage = page();
  [...document.querySelectorAll(grid + ' .panel:not(.ck-collapsed) > .phead .ck-lay-fold')].forEach((x) => x.click());
  const folded = [...document.querySelectorAll(grid + ' .panel.ck-collapsed')];
  const out = { facts: true, w: innerWidth, h: innerHeight, openPage, foldedPage: page(), cards, nfolded: folded.length,
    headless: folded.filter((p) => !head(p)).map((p) => p.dataset.ckPanel || '?'), shown: folded.flatMap(shownIn) };
  [...document.querySelectorAll(grid + ' .panel.ck-collapsed > .phead .ck-lay-fold')].forEach((x) => x.click());
  out.stillFolded = document.querySelectorAll(grid + ' .panel.ck-collapsed').length;
  return JSON.stringify(out);
})(%s)""" % json.dumps(CASES)


@needs_browser
def test_a_collapsed_card_on_home_is_only_its_header_and_the_page_shortens_at_the_named_widths(tmp_path: Path, monkeypatch) -> None:
    _band(tmp_path, monkeypatch)                    # a band with desk rows, so the List card draws its "desk owes" button under the rows
    _root, src, _ids = _install(tmp_path)
    steps: list = [{"emulate": {"w": 1440, "h": HEIGHT, "dpr": 1}}, READY, BOARD_SETTLED]
    for w in WIDTHS:
        steps += [{"emulate": {"w": w, "h": HEIGHT, "dpr": 1}}, BOARD_SETTLED, {"eval": MEASURE}]
    with _serve(src, surface_tokens={"browser": "T"}, install_class="reported") as (url, _httpd):
        out = _drive(tmp_path, f"{url}/#write_token=T", steps)
    rows = [json.loads(v) for v in out["values"] if isinstance(v, str) and v.startswith('{"facts"')]
    assert [r["w"] for r in rows] == list(WIDTHS), out["values"]
    for r in rows:
        w = r["w"]
        assert r["openPage"] > r["h"] + 200, (w, "the open page must be taller than the viewport for its height to be measured", r["openPage"])
        for label, c in r["cards"].items():
            assert c, (w, label, "the fixture does not draw this card, so its fixed height is not measured")
            o, f = c["open"], c["shut"]
            assert not o["folded"] and f["folded"] and c["reopened"], (w, label, "the fold control did not fold and unfold the card", o, f)
            assert o["panel"] > o["head"] + 100, (w, label, "the open card is not taller than its header, so the fold proves nothing", o)
            assert f["panel"] <= f["head"] + SLACK, (w, label, "a collapsed card is taller than its header", f)
            assert o["kids"] and not f["shown"], (w, label, "a collapsed card draws more than its header", f["shown"])
        assert r["cards"]["list"]["open"]["more"], (w, "the List card draws no desk-owes button, so the fold could not hide it")
        assert r["nfolded"] > 0 and not r["headless"], (w, "a folded card has no header", r["nfolded"], r["headless"])
        assert not r["shown"], (w, "a folded card draws more than its header", r["shown"])
        assert r["stillFolded"] == 0, (w, "a card did not unfold", r["stillFolded"])
        assert r["foldedPage"] < r["openPage"] - 200, (w, "folding every card did not shorten the page: the grid did not reflow", r["openPage"], r["foldedPage"])



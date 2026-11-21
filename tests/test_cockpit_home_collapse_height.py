"""levain#182: a collapsed card on Home shrinks to its header.

Home gives its cards a fixed height with ``!important`` (the work row, the machine row, the phone sizes). A collapsed card hid its body but
kept that height, so it stayed a blank full-size panel. The fix is one later rule that lets a folded card size to its header. The cascade
check below needs no browser; the measured check drives the served page at 390, 820 and 1440 and needs Chrome."""

from __future__ import annotations

import json
import re
from pathlib import Path

from tests.test_cockpit_flip_parity import _drive, needs_browser
from tests.test_cockpit_k2a import _install, _serve

CSS = Path(__file__).resolve().parent.parent / "levain" / "templates" / "web" / "dashboard.css"
HOME = 'body.ia[data-ia-place="home"]'
WIDTHS = (390, 820, 1440)
READY = {"until": "document.body.dataset.iaPlace === 'home' && document.querySelector('.ck-zgrid .panel[data-ck-panel=\"tray\"] .ck-lay-fold') && 1"}
SETTLE = {"eval": "new Promise(r => setTimeout(r, 600)).then(() => 1)"}


def _rules() -> list[tuple[str, str]]:
    """Every ``selector { declarations }`` of the stylesheet in source order (an @media prelude is not a rule and is skipped)."""
    css = re.sub(r"/\*.*?\*/", "", CSS.read_text(encoding="utf-8"), flags=re.S)
    return [(m.group(1).strip(), m.group(2)) for m in re.finditer(r"([^{}]+)\{([^{}]*)\}", css)]


def test_a_folded_card_outranks_every_fixed_home_height() -> None:
    rules = _rules()
    fixed = [i for i, (sel, decl) in enumerate(rules)
             if sel.startswith(HOME) and ".panel" in sel and "ck-collapsed" not in sel and re.search(r"(?<![-\w])height:\s*[\d.]+px\s*!important", decl)]
    assert fixed, "Home draws no fixed-height card, so this check would prove nothing"
    folded = [i for i, (sel, decl) in enumerate(rules)
              if any(s.strip() == f"{HOME} .panel.ck-collapsed" for s in sel.split(",")) and re.search(r"(?<![-\w])height:\s*auto\s*!important", decl)]
    assert folded, "no Home rule lets a folded card size to its header (height: auto !important)"
    assert min(folded) > max(fixed), "the folded-card rule must come after every fixed Home height: equal specificity, the later wins"


# height of a card against its own header, and the page's height, read through the page's own fold control
FACTS = """(() => {
  const p = document.querySelector('.ck-zgrid .panel[data-ck-panel="tray"]');
  const h = p.querySelector(':scope > .phead');
  return JSON.stringify({ facts: true, w: innerWidth, folded: p.classList.contains('ck-collapsed'), panel: Math.round(p.getBoundingClientRect().height),
    head: Math.round(h.getBoundingClientRect().height), page: document.documentElement.scrollHeight });
})()"""
FOLD_ALL = """(() => { const b = [...document.querySelectorAll('.ck-zgrid .panel:not(.ck-collapsed) > .phead .ck-lay-fold, .ck-zgrid .panel:not(.ck-collapsed) .ck-lay-fold')];
  b.forEach((x) => x.click()); return b.length; })()"""
FOLD_ONE = "document.querySelector('.ck-zgrid .panel[data-ck-panel=\"tray\"] .ck-lay-fold').click(); 1"


@needs_browser
def test_a_collapsed_card_on_home_takes_only_its_header_and_the_page_shortens_at_the_named_widths(tmp_path: Path) -> None:
    """Browser test: written here, run on CI (not run on the machine it was written on)."""
    _root, src, _ids = _install(tmp_path)
    steps: list = [{"emulate": {"w": 1440, "h": 900, "dpr": 1}}, READY, SETTLE]
    for w in WIDTHS:
        steps += [{"emulate": {"w": w, "h": 900, "dpr": 1}}, SETTLE, {"eval": FACTS},
                  {"eval": FOLD_ONE}, SETTLE, {"eval": FACTS},
                  {"eval": FOLD_ALL}, SETTLE, {"eval": FACTS},
                  {"eval": "[...document.querySelectorAll('.ck-zgrid .panel.ck-collapsed')].map(p => p.querySelector(':scope > .phead') ? 1 : 0).every(Boolean)"},
                  # leave every card open for the next width
                  {"eval": "[...document.querySelectorAll('.ck-zgrid .panel.ck-collapsed .ck-lay-fold')].forEach((x) => x.click()); 1"}, SETTLE]
    with _serve(src, surface_tokens={"browser": "T"}, install_class="reported") as (url, _httpd):
        out = _drive(tmp_path, f"{url}/#write_token=T", steps)
    rows = [json.loads(v) for v in out["values"] if isinstance(v, str) and v.startswith('{"facts"')]
    assert len(rows) == 3 * len(WIDTHS), out["values"]
    for i, w in enumerate(WIDTHS):
        open_, one, every = rows[3 * i: 3 * i + 3]
        assert open_["w"] == w and not open_["folded"], (w, open_)
        assert open_["panel"] > open_["head"] + 100, (w, "the open List card is not taller than its header, so the fold proves nothing", open_)
        assert one["folded"], (w, "the fold control did not fold the card", one)
        assert one["panel"] <= one["head"] + 2, (w, "a collapsed card is taller than its header", one)
        assert every["page"] < open_["page"] - 200, (w, "folding every card did not shorten the page: the grid did not reflow", open_["page"], every["page"])

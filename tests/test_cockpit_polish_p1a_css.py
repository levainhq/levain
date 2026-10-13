"""Cockpit polish P1a (1012 spec D1/D4): no `font:` shorthand may name a colour variable, and every
filter box draws one look (control height, radius, type)."""
import re
from pathlib import Path

CSS = (Path(__file__).resolve().parent.parent / "levain" / "templates" / "web" / "dashboard.css").read_text()

COLOUR = re.compile(r"^(#[0-9a-f]{3,8}\b|(color-mix|oklch|oklab|rgba?|hsla?|hwb|lch|lab|color)\(|transparent$|currentcolor$)", re.I)


def _props():
    return dict((m.group(1), m.group(2).strip()) for m in re.finditer(r"(--[\w-]+)\s*:\s*([^;]+);", CSS))


def _colour_vars():
    props = _props()
    out = {n for n, v in props.items() if COLOUR.match(v)}
    changed = True
    while changed:  # an alias of a colour variable is a colour variable
        changed = False
        for n, v in props.items():
            m = re.fullmatch(r"var\((--[\w-]+)\)", v)
            if n not in out and m and m.group(1) in out:
                out.add(n); changed = True
    return out


def test_no_font_shorthand_names_a_colour_variable():
    colours = _colour_vars()
    assert "--label" in colours  # the detector sees the variable D1 tripped on
    bad = []
    for m in re.finditer(r"(?<![\w-])font\s*:\s*([^;}]+)", CSS):
        for v in re.findall(r"var\((--[\w-]+)", m.group(1)):
            if v in colours:
                bad.append(m.group(0).strip())
    assert not bad, bad


def _rule(sel):
    m = re.search(r"(?m)^\s*" + re.escape(sel) + r"\s*\{([^}]*)\}", CSS)
    assert m, sel
    return m.group(1)


def test_both_filter_boxes_share_one_look():
    for sel in (".xfilter", ".ep-search-input"):
        r = _rule(sel)
        assert "height: var(--ctl-md)" in r, sel
        assert "border-radius: var(--r-md)" in r, sel
    for sel in (".xfilter-input", ".ep-search-input"):
        assert "var(--fs-meta)" in _rule(sel) and "var(--sans)" in _rule(sel), sel
    assert "--ctl-md: 28px" in CSS and "--r-md: 8px" in CSS


def test_unavailable_is_not_the_accent_orange():
    r = re.search(r"(?m)^\s*p\.err::before\s*\{([^}]*)\}", CSS).group(1)
    assert "var(--warn)" in r and "--hot" not in r

"""D3/D5: one row anatomy. The verbs sit on the meta line, show by opacity without moving the row, at most three are
visible and the rest sit behind a keyboard-reachable "···" menu. Driven in headless Chrome over a writable page."""

from __future__ import annotations

from pathlib import Path

from tests.test_cockpit_flip_parity import _drive, needs_browser
from tests.test_cockpit_k2a import _install, _serve

ROW = ('[...document.querySelectorAll(".row")].find(r => r.querySelector(".clause") && '
       'r.querySelector(".clause").textContent === "a tray seed" && r.querySelector(".verb-actions"))')


def _page(tmp_path: Path, steps: list[dict]) -> list:
    _root, src, _ids = _install(tmp_path)
    with _serve(src, surface_tokens={"browser": "T"}, install_class="reported") as (url, _httpd):
        return _drive(tmp_path, f"{url}/#write_token=T", [{"until": f"{ROW} && {ROW}.querySelector('.verb-btn')"}, *steps])["values"]


@needs_browser
def test_hovering_a_row_never_changes_its_height(tmp_path: Path) -> None:
    vals = _page(tmp_path, [
        {"eval": f"(() => {{ const r = {ROW}; const v = r.querySelector('.verb-actions'); return [r.getBoundingClientRect().height, getComputedStyle(v).opacity]; }})()"},
        {"eval": f"(async () => {{ const r = {ROW}; r.focus(); await new Promise(x => setTimeout(x, 400)); const v = r.querySelector('.verb-actions'); return [r.getBoundingClientRect().height, getComputedStyle(v).opacity]; }})()"},
    ])
    before, after = vals[1], vals[2]
    assert before[0] == after[0], vals
    assert float(before[1]) == 0.0 and float(after[1]) > 0.0, vals


@needs_browser
def test_at_most_three_verbs_show_and_the_rest_sit_in_the_overflow_menu(tmp_path: Path) -> None:
    vals = _page(tmp_path, [{"eval": f"""(() => {{ const v = {ROW}.querySelector('.verb-actions');
      const vis = [...v.querySelectorAll(':scope > .verb-btn')].map(b => b.textContent);
      const menu = [...v.querySelectorAll('.verb-menu .verb-btn:not(.verb-dup)')].map(b => b.textContent);
      const dups = [...v.querySelectorAll('.verb-menu .verb-dup')].map(b => b.textContent + ':' + getComputedStyle(b).display);
      const more = v.querySelector('.verb-more');
      const b = v.querySelector(':scope > .verb-btn'); const cs = getComputedStyle(b);
      return {{vis, menu, dups, haspopup: more.getAttribute('aria-haspopup'), expanded: more.getAttribute('aria-expanded'),
        fs: cs.fontSize, h: b.getBoundingClientRect().height, border: cs.borderTopWidth,
        rowfs: getComputedStyle({ROW}.querySelector('.clause')).fontSize, menuHidden: v.querySelector('.verb-menu').hidden}}; }})()"""}])
    got = vals[1]
    assert got["vis"] == ["edit", "schedule", "dismiss", "···"], got
    assert got["menu"] == ["reclassify", "metabolize"], got
    # a copy of each inline verb waits in the menu for a narrow row (container query); it shows nowhere on a wide one
    assert got["dups"] == ["edit:none", "schedule:none", "dismiss:none"], got
    assert (got["haspopup"], got["expanded"], got["menuHidden"]) == ("menu", "false", True), got
    assert got["fs"] == "12px" and got["h"] == 24 and got["border"] == "0px" and got["rowfs"] == "13px", got


@needs_browser
def test_the_overflow_menu_opens_and_closes_by_keyboard(tmp_path: Path) -> None:
    vals = _page(tmp_path, [
        {"eval": f"(() => {{ const v = {ROW}.querySelector('.verb-actions'); const m = v.querySelector('.verb-more'); m.focus(); m.click(); "
                 "return [m.getAttribute('aria-expanded'), v.querySelector('.verb-menu').hidden, document.activeElement.textContent]; })()"},
        {"eval": f"(() => {{ const v = {ROW}.querySelector('.verb-actions'); "
                 "v.dispatchEvent(new KeyboardEvent('keydown', {key: 'ArrowDown', bubbles: true})); return document.activeElement.textContent; })()"},
        {"eval": f"(() => {{ const v = {ROW}.querySelector('.verb-actions'); "
                 "document.activeElement.dispatchEvent(new KeyboardEvent('keydown', {key: 'Escape', bubbles: true})); "
                 "return [v.querySelector('.verb-more').getAttribute('aria-expanded'), v.querySelector('.verb-menu').hidden, document.activeElement.textContent]; })()"},
    ])
    assert vals[1] == ["true", False, "reclassify"], vals
    assert vals[2] == "metabolize", vals
    assert vals[3] == ["false", True, "···"], vals


@needs_browser
def test_a_press_that_moves_focus_to_the_row_does_not_close_the_menu_before_the_click(tmp_path: Path) -> None:
    """Safari/WebKit: a mouse press on a menu button does not focus the button, so focus goes to the nearest focusable
    ancestor (the row), and the browser fires focusout with relatedTarget = the row. That must not close the menu, or the
    click lands on whatever sits under it (found in real WebKit 2026-10-14; headless Chrome focuses the button itself)."""
    vals = _page(tmp_path, [
        {"eval": f"(() => {{ const v = {ROW}.querySelector('.verb-actions'); const m = v.querySelector('.verb-more'); m.focus(); m.click(); "
                 "return v.querySelector('.verb-menu').hidden; })()"},
        {"eval": f"(() => {{ const r = {ROW}; const v = r.querySelector('.verb-actions'); const a = document.activeElement; "
                 "a.dispatchEvent(new FocusEvent('focusout', {bubbles: true, relatedTarget: r})); "
                 "return v.querySelector('.verb-menu').hidden; })()"},
        {"eval": f"(() => {{ const v = {ROW}.querySelector('.verb-actions'); const a = document.activeElement; "
                 "const o = document.body.appendChild(document.createElement('button')); "
                 "a.dispatchEvent(new FocusEvent('focusout', {bubbles: true, relatedTarget: o})); "
                 "return v.querySelector('.verb-menu').hidden; })()"},
    ])
    assert vals[1] is False, vals
    assert vals[2] is False, vals    # relatedTarget = the row: the menu stays open for the click
    assert vals[3] is True, vals     # a keyboard move to an unrelated element still closes it


@needs_browser
def test_a_press_inside_the_menu_keeps_focus_on_the_item(tmp_path: Path) -> None:
    """The primary half of the Safari fix: mousedown inside the open menu is default-prevented, so the browser does not move
    focus to the row at all (the keyboard stays inside the group after a mouse press)."""
    vals = _page(tmp_path, [
        {"eval": f"(() => {{ const v = {ROW}.querySelector('.verb-actions'); const m = v.querySelector('.verb-more'); m.focus(); m.click(); return v.querySelector('.verb-menu').hidden; }})()"},
        {"eval": f"(() => {{ const v = {ROW}.querySelector('.verb-actions'); const it = v.querySelector('.verb-menu .verb-btn:not(.verb-dup)'); "
                 "const ev = new MouseEvent('mousedown', {bubbles: true, cancelable: true}); it.dispatchEvent(ev); return ev.defaultPrevented; })()"},
    ])
    assert vals[1] is False and vals[2] is True, vals

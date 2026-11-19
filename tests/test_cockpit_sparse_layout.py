"""A sparse install (every new user's) keeps the designed shape (K2_L1L2 L2-1 / L2-7): an empty card is its header and one line, not a
populated height. (The full-width Morning Tray went with the Morning mode: Home places the Tray as one of three work columns; the glance tiles
still fill their row, on Home.)"""

from __future__ import annotations

from pathlib import Path

from tests.test_cockpit_flip_parity import _drive, needs_browser
from tests.test_cockpit_k2a import _install, _serve


def _page(tmp_path: Path, steps: list[dict]) -> list:
    _root, src, _ids = _install(tmp_path)
    with _serve(src, surface_tokens={"browser": "T"}, install_class="reported") as (url, _httpd):
        return _drive(tmp_path, f"{url}/#write_token=T", [{"until": "document.querySelector('.ck-widgets .ck-card.w')"}, *steps])["values"]


@needs_browser
def test_a_sparse_glance_shares_the_row_between_its_tiles(tmp_path: Path) -> None:
    """Restored from Morning onto Home, where the glance lives now: the tiles fill the row (an auto-fit grid, not an auto-fill one that
    leaves its unused tracks empty), never a lone small tile and a void."""
    vals = _page(tmp_path, [{"eval": """(async () => { await new Promise(r => setTimeout(r, 400));
      const w = document.querySelector('.ck-widgets');
      const cs = [...w.querySelectorAll(':scope > .ck-card.w')].filter(c => getComputedStyle(c).display !== 'none');
      const W = w.getBoundingClientRect().width;
      return { n: cs.length, shape: w.dataset.shape, place: document.body.dataset.iaPlace, shares: cs.map(c => c.getBoundingClientRect().width / W) }; })()"""}])
    got = vals[1]
    n, shares = got["n"], got["shares"]
    assert got["place"] == "home" and n >= 1, got
    assert got["shape"] == ("strip" if n < 4 else "tiles"), got                # the shape follows the count
    assert max(shares) - min(shares) < 0.03, got                                # equal tiles
    assert sum(shares) > 0.85, got                                              # together they fill the row: no lone small tile, no void


@needs_browser
def test_an_empty_card_is_its_header_and_one_line_not_a_populated_height(tmp_path: Path) -> None:
    # on Memory: Home gives its machine panels fixed heights by design, so the empty-card rule is read where the zone grid keeps it
    vals = _page(tmp_path, [{"eval": "document.querySelector('[data-mode-tab=memory]').click(); 1"}, {"eval": """(async () => { await new Promise(r => setTimeout(r, 400));
      const empties = [...document.querySelectorAll('.ck-zgrid .panel')].filter(p => p.querySelector('.ck-count[hidden]'));   // descendants of a column included
      const out = empties.map(p => [p.dataset.ckPanel, p.classList.contains('ck-is-empty') || p.classList.contains('ck-dash'), p.getBoundingClientRect().height, getComputedStyle(p).display]);
      const tall = [...document.querySelectorAll('.ck-zgrid .panel.ck-h-tall')].filter(p => !p.classList.contains('ck-is-empty') && !p.classList.contains('ck-dash') && p.getBoundingClientRect().height > 0).map(p => p.getBoundingClientRect().height);
      return { out, tall: tall.length ? Math.min(...tall) : 0 }; })()"""}])
    got = vals[2]
    assert got["out"], vals
    for name, marked, h, display in got["out"]:
        assert marked, (name, got)                                              # an empty card is marked empty (or dashed), whatever tab shows it
        if display != "none":
            assert h < 90, (name, h, got["tall"])                               # a header and one line; Memory's populated cards are not a fixed measure to halve


@needs_browser
def test_a_text_panel_reads_first_and_opens_an_editor_holding_its_text(tmp_path: Path) -> None:
    vals = _page(tmp_path, [{"eval": """(async () => { await new Promise(r => setTimeout(r, 400));
      const card = [...document.querySelectorAll('.panel')].find(p => /Operator · Identity/.test((p.querySelector('h2') || {}).textContent || ''));
      const box = card.querySelector('.tray-dump.text-editor'); const hidden = box.hidden;
      card.querySelector('.edit-btn').click(); const ta = box.querySelector('textarea');
      return { hidden, open: !box.hidden, value: ta.value, titles: (card.innerText.match(/Operator · Identity/g) || []).length, label: ta.getAttribute('aria-label') }; })()"""}])
    got = vals[1]
    assert got["hidden"] is True and got["open"] is True and got["value"].strip() != "" and got["titles"] == 1, got
    assert got["label"] == "Edit this text", got


@needs_browser
def test_a_closed_text_editor_does_not_hold_off_a_refresh_and_a_changed_one_does(tmp_path: Path) -> None:
    vals = _page(tmp_path, [{"eval": """(async () => { await new Promise(r => setTimeout(r, 400));
      const card = [...document.querySelectorAll('.panel')].find(p => /Operator · Identity/.test((p.querySelector('h2') || {}).textContent || ''));
      const ta = card.querySelector('.text-editor textarea'); const D = window.LevainDashboard;
      const closed = D.hasUnsavedEdit();
      card.querySelector('.edit-btn').click(); const opened = D.hasUnsavedEdit();
      const orig = ta.value; ta.value = ''; const emptied = D.hasUnsavedEdit();
      ta.value = orig + ' more'; const typed = D.hasUnsavedEdit();
      card.querySelector('.verb-cancel').click(); const cancelled = D.hasUnsavedEdit();
      return { closed, opened, emptied, typed, cancelled }; })()"""}])
    assert vals[1] == {"closed": False, "opened": False, "emptied": True, "typed": True, "cancelled": False}, vals


@needs_browser
def test_a_card_with_a_filter_keeps_its_height_even_with_nothing_to_list(tmp_path: Path) -> None:
    # no fixture card is both empty and filterable, so two stand-in cards are placed in a zone grid and a mode switch re-marks them
    vals = _page(tmp_path, [{"eval": """(async () => { await new Promise(r => setTimeout(r, 400));
      const g = document.querySelector('.ck-zgrid'); const mk = (filter) => { const p = document.createElement('section'); p.className = 'panel';
        p.innerHTML = '<div class="phead"><h2>x</h2></div>' + (filter ? '<div class="xfilter"><input class="xfilter-input"></div>' : '') + '<div class="pbody"><p class="empty">none</p></div>';
        g.appendChild(p); return p; };
      const plain = mk(false), filtered = mk(true);
      document.querySelector('[data-mode-tab="memory"]').click(); document.querySelector('[data-mode-tab="board"]').click();
      await new Promise(r => setTimeout(r, 100));
      return [plain.classList.contains('ck-is-empty'), filtered.classList.contains('ck-is-empty')]; })()"""}])
    assert vals[1] == [True, False], vals

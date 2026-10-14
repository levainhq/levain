"""A sparse install (every new user's) keeps the designed widths: one glance widget is one of four tracks, and Morning's lone
Work card is one of three, not stretched across the page (Phill 10-14: "stretches full screen and looks like shit")."""

from __future__ import annotations

from pathlib import Path

from tests.test_cockpit_flip_parity import _drive, needs_browser
from tests.test_cockpit_k2a import _install, _serve


def _page(tmp_path: Path, steps: list[dict]) -> list:
    _root, src, _ids = _install(tmp_path)
    with _serve(src, surface_tokens={"browser": "T"}, install_class="reported") as (url, _httpd):
        return _drive(tmp_path, f"{url}/#write_token=T", [{"until": "document.querySelector('.ck-widgets .ck-card.w')"}, *steps])["values"]


@needs_browser
def test_a_lone_glance_widget_keeps_one_of_four_tracks(tmp_path: Path) -> None:
    vals = _page(tmp_path, [{"eval": """(() => { const w = document.querySelector('.ck-widgets'); const c = w.querySelector('.ck-card.w');
      return [w.querySelectorAll(':scope > .ck-card.w').length, c.getBoundingClientRect().width / w.getBoundingClientRect().width]; })()"""}])
    n, share = vals[1]
    assert n == 1 and 0.2 < share < 0.3, vals


@needs_browser
def test_morning_does_not_stretch_a_lone_work_card_across_the_page(tmp_path: Path) -> None:
    vals = _page(tmp_path, [
        {"eval": "(() => { document.querySelector('[data-mode-tab=\"morning\"]').click(); return document.body.dataset.mode; })()"},
        {"eval": """(async () => { await new Promise(r => setTimeout(r, 300));
          const g = [...document.querySelectorAll('.ck-zgrid')].find(z => [...z.children].some(p => getComputedStyle(p).display !== 'none' && p.querySelector('.tray-dump')));
          const shown = [...g.children].filter(p => getComputedStyle(p).display !== 'none');
          return [shown.length, shown[0].getBoundingClientRect().width / g.getBoundingClientRect().width]; })()"""},
    ])
    assert vals[1] == "morning", vals
    n, share = vals[2]
    assert n == 1 and 0.25 < share < 0.4, vals

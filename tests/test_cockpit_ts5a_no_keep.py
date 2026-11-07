"""TS-5a (Phill 11-07, np-721ce711 (b)): the cockpit has no Keep panel. A parked loop is an Open loops row with a visible
"parked" marker (and the inverse verb, "reactivate"); a legacy ``note`` row still in a store shows in the Tray, badged
"note", never dropped. No surface (kernel manifest, page at desktop and phone width, TUI, text render) titles a "Keep" panel."""
from __future__ import annotations

from pathlib import Path

from levain.cockpit.providers import build_default_cockpit
from levain.dashboard import render_text
from tests.test_cockpit_flip_parity import _drive, needs_browser
from tests.test_cockpit_k2a import CRED, _install, _serve

BOARD = "#write_token=T&board"
READY = {"until": "document.querySelector('.ck-zgrid[data-ck-zone=\"operate\"] .panel[data-ck-panel=\"tray\"] .pbody .row')"}
SETTLE = {"eval": "new Promise(r => setTimeout(r, 500)).then(() => 1)"}

# [panel ids and titles that name Keep, the badge words of the row for a spore id in a panel]
KEEP_PANELS = ("[...document.querySelectorAll('[data-ck-panel]')].filter(p => p.dataset.ckPanel === 'keep' "
               "|| /^\\s*keep\\b/i.test((p.querySelector('h2') || {}).textContent || '')).length")


def _row(panel: str, sid: str) -> str:
    return (f"(() => {{ const r = document.querySelector('[data-ck-panel=\"{panel}\"] .row[data-spore=\"{sid}\"]');"
            " return r && { tags: [...r.querySelectorAll('[data-ck-tag]')].map(t => t.textContent),"
            " verbs: [...r.querySelectorAll('.verb-btn')].map(b => b.textContent) }; })()")


def test_the_kernel_cockpit_has_no_keep_panel_and_files_both_halves_where_they_belong(tmp_path: Path) -> None:
    _root, src, ids = _install(tmp_path)
    ck = build_default_cockpit(src)
    heads = ck.manifest(CRED)["panels"]
    assert "keep" not in heads and not any(h.get("title", "").startswith("Keep") for h in heads.values())
    loops = {r["id"]: r for r in ck.panel("loops")["rows"]}
    parked = loops[f"spore:{ids['parked']}"]
    assert parked["facets"]["tier"] == "parked"                      # the parked marker rides the tier facet
    tray = {r["id"]: r for r in ck.panel("tray")["rows"]}
    assert tray[f"spore:{ids['note']}"]["facets"]["disposition"] == "note"      # a legacy note: visible, not dropped
    assert f"spore:{ids['note']}" not in loops and f"spore:{ids['parked']}" not in tray


def test_the_text_render_and_the_substrate_view_name_no_keep(tmp_path: Path) -> None:
    root, src, ids = _install(tmp_path)
    view = src.build()
    text = render_text(view)
    assert "Keep" not in text
    assert "[parked] a parked loop" in text and "[note] a keep note" in text
    assert "keep" not in view.to_dict() and not any(p["kind"] == "keep" for p in view.layout())


@needs_browser
def test_the_page_draws_a_parked_loop_in_open_loops_marked_and_a_legacy_note_in_the_tray(tmp_path: Path) -> None:
    _root, src, ids = _install(tmp_path)
    with _serve(src, surface_tokens={"browser": "T"}, install_class="reported") as (url, _httpd):
        for width in (1440, 390):          # the desktop board and the phone
            (tmp_path / f"w{width}").mkdir()
            out = _drive(tmp_path / f"w{width}", f"{url}/{BOARD}", [
                {"emulate": {"w": width, "h": 900, "dpr": 1}}, READY, SETTLE,
                {"eval": KEEP_PANELS},
                {"eval": _row("loops", ids['parked'])},
                {"eval": _row("tray", ids["note"])},
                {"eval": "[...document.querySelectorAll('.ck-zgrid .panel h2')].map(h => h.textContent)"},
            ])["values"]
            keep_n, parked, note, titles = out[2], out[3], out[4], out[5]
            assert keep_n == 0, (width, titles)
            assert not any(t.strip().lower().startswith("keep") for t in titles), (width, titles)
            assert parked and "parked" in parked["tags"], (width, parked)                 # the visible marker
            assert "reactivate" in parked["verbs"] and "park" not in parked["verbs"], (width, parked)
            assert note and "note" in note["tags"], (width, note)                         # the legacy badge

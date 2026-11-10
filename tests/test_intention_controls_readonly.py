"""X2 (codex MED, land 3 round 1): a read-only render (`levain web` without --write: no injected `commit`) draws no intention
controls, at the List band row nor in the Decisions / Needs card; the page's write predicate (`commit`) gates them as it gates every
other write control."""
from __future__ import annotations

import dataclasses
from pathlib import Path

from tests.test_cockpit_flip_parity import _drive, needs_browser
from tests.test_cockpit_k2a import _serve
from tests.test_intention_controls_page import DESKTOP, _rig, lh  # noqa: F401  (lh is a fixture)

COUNT = ("JSON.stringify({k: 'ro', sets: document.querySelectorAll('.intention-actions').length,"
         " verbs: document.querySelectorAll('.intention-actions button').length,"
         " needs: document.querySelectorAll('[data-needs-id]').length,"
         " list: document.querySelectorAll('[data-surface=band]').length})")
OPEN_NEEDS = "(() => { const e = document.querySelector('.needs-row[data-needs-id]'); if (!e) return 'none'; e.click(); return 'clicked'; })()"
WAIT = {"eval": "new Promise(r => setTimeout(() => r(1), 2500))"}


@needs_browser
def test_read_only_render_draws_no_intention_controls(tmp_path: Path, lh: Path) -> None:  # noqa: F811
    src, _store, _sp, _sid, _ring = _rig(tmp_path, lh)
    ro = dataclasses.replace(src, write_scope=None)       # the shape `levain web` serves without --write
    with _serve(ro, surface_tokens={"browser": "T"}, install_class="reported") as (url, _h):
        out = _drive(tmp_path, f"{url}/#write_token=T", [
            DESKTOP,
            {"until": "document.querySelector('.needs-row[data-needs-id]') && 1", "ms": 30000}, WAIT,
            {"eval": COUNT},                       # the page: the List band row and the Needs strip
            {"eval": OPEN_NEEDS},
            {"until": "document.querySelector('#levain-modal [data-needs-card]') && 1", "ms": 30000}, WAIT,
            {"eval": COUNT},                       # the Decisions card, open
        ])
    reads = [v for v in out["values"] if isinstance(v, str) and '"ro"' in v]
    assert len(reads) == 2, out["values"]
    for r in reads:
        assert '"sets":0' in r and '"verbs":0' in r, r
    assert '"needs":0' not in reads[0], reads[0]          # the rows themselves are still shown, only the controls are not


@needs_browser
def test_writable_render_still_draws_them(tmp_path: Path, lh: Path) -> None:  # noqa: F811
    src, _store, _sp, _sid, _ring = _rig(tmp_path, lh)
    with _serve(src, surface_tokens={"browser": "T"}, install_class="reported") as (url, _h):
        out = _drive(tmp_path, f"{url}/#write_token=T", [
            DESKTOP,
            {"until": "document.querySelector('.needs-row[data-needs-id]') && 1", "ms": 30000}, WAIT,
            {"eval": COUNT},
        ])
    reads = [v for v in out["values"] if isinstance(v, str) and '"ro"' in v]
    assert reads and '"sets":0' not in reads[-1], reads

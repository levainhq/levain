"""Screenshots of the agents graph's header for the desk to look at (row 19), taken with the page driver. Off unless
``LEVAIN_GRAPHWAIT_SHOTS`` names a folder; ``LEVAIN_GRAPHWAIT_SHOTS_TAG`` (before / after) prefixes each file name. The same
literal value and the same fixture go through both trees, so a pair differs only in what the tree draws."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from tests.test_cockpit_f12b_topology_view import _paint, _run, _value
from tests.test_cockpit_k7_build import _sessions_fixture

SHOTS = os.environ.get("LEVAIN_GRAPHWAIT_SHOTS")
TAG = os.environ.get("LEVAIN_GRAPHWAIT_SHOTS_TAG", "shot")
WIDTHS = {"1280": {"emulate": {"w": 1280, "h": 900, "dpr": 1}}, "390": {"emulate": {"w": 390, "h": 800, "dpr": 1, "mobile": True}}}


@pytest.mark.skipif(not SHOTS, reason="LEVAIN_GRAPHWAIT_SHOTS names the folder the shots go to")
@pytest.mark.parametrize("width", sorted(WIDTHS))
def test_the_graph_header_shots(tmp_path, monkeypatch, width):
    out_dir = Path(SHOTS)
    out_dir.mkdir(parents=True, exist_ok=True)
    _root, src = _sessions_fixture(tmp_path, monkeypatch)
    first = WIDTHS[width]
    for lens in ("work", "agents"):
        _run(tmp_path, src, [{"eval": _paint(_value(), lens)}, {"eval": "new Promise(r => setTimeout(r, 600))"},
                             {"shot": str(out_dir / f"{TAG}_{lens}_literal_{width}.png"), "full": True}], first=first)
    # the real page and the real provider, the Work lens: the sessions fixture has a pending consent request
    _run(tmp_path, src, [{"eval": "document.querySelector('#ck-hlens [data-tv=work]').click(); 1"},
                         {"until": "document.querySelectorAll('#ck-needs .needs-row').length >= 1 && 1", "ms": 30000},
                         {"eval": "new Promise(r => setTimeout(r, 600))"},
                         {"shot": str(out_dir / f"{TAG}_work_page_{width}.png"), "full": True}], first=first)

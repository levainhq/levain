"""The page does not draw the brief region (the Next-from-the-List card and its heading); the state line still reaches the now band,
and a brief-kind panel placed in a zone still draws."""
from __future__ import annotations

import json
from pathlib import Path

from levain.cockpit import ProviderSpec, Read
from levain.cockpit.providers import build_default_cockpit
from tests.test_cockpit_flip_parity import _drive, needs_browser
from tests.test_cockpit_k2a import _install, _serve
from tests.test_cockpit_k4a_needs import DESKTOP

CRED = {"class": "token", "name": "browser", "device_class": None}

_PROBE = r"""(() => {
  const zoneBrief = document.querySelector('.panel[data-ck-panel="zone-brief"]');
  return JSON.stringify({
    regionNode: !!document.getElementById('ck-brief'), headingNode: !!document.getElementById('ck-h-brief'),
    card: !!document.querySelector('.brief-card'), briefRegionSection: !!document.querySelector('[data-ck-region="brief"]'),
    stateInSlot: !!document.querySelector('#ck-state-slot #state'), stateText: (document.getElementById('state') || {}).textContent || null,
    zoneKind: zoneBrief ? zoneBrief.dataset.ckKind : null,
    zoneLines: zoneBrief ? [...zoneBrief.querySelectorAll('.brief-line p')].map((p) => p.textContent) : null,
    zoneHeadline: zoneBrief ? (zoneBrief.querySelector('.q') || {}).textContent || null : null,
    zoneInBoard: !!(zoneBrief && zoneBrief.closest('#board'))});
})()"""


@needs_browser
def test_the_page_draws_no_brief_region_but_keeps_the_state_line_and_a_zone_brief_panel(tmp_path: Path) -> None:
    _root, src, _ids = _install(tmp_path)
    ck = build_default_cockpit(src)
    value = {"headline": "a zone brief", "lines": [{"label": "first", "text": "a line in a zone"}]}
    ck.register(ProviderSpec("zone-brief", "brief", "Zone brief", "gauge", lambda c: Read(value=value), region="operate"))
    assert "begin" in ck.manifest(CRED)["regions"]["brief"]   # the served manifest still names a brief region; the page just does not draw it
    with _serve(src, cockpit=ck, surface_tokens={"browser": "T"}, install_class="reported") as (url, _httpd):
        out = _drive(tmp_path, f"{url}/#write_token=T", [DESKTOP, {"until": "document.querySelector('.panel[data-ck-panel=\"zone-brief\"] .brief-line')"},
                                                          {"until": "document.querySelector('#ck-state-slot #state')"},
                                                          {"eval": _PROBE}])["values"]
    got = json.loads(out[-1])
    assert not got["regionNode"] and not got["headingNode"] and not got["card"] and not got["briefRegionSection"], got
    assert got["stateInSlot"], got                                                   # the state line is still in the now band
    assert got["zoneKind"] == "brief" and got["zoneInBoard"], got
    assert got["zoneHeadline"] == "a zone brief" and got["zoneLines"] == ["a line in a zone"], got

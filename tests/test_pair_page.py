"""PAIR P2, the browser half: the /pair page, its script and the Devices panel (levain project_memory/1024-18/PAIR_DESIGN.md
rev 6). Each assertion is one the real-browser run (project_memory/1024-20/pair_p2/RETURN.md) showed."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from levain.dashboard import SubstrateSource
from levain.web_server import load_web_asset
from tests.test_cockpit_k2a import _install
from tests.test_cockpit_pairing_http import PROXY, T, _call, _serve


@pytest.fixture
def src(tmp_path: Path) -> SubstrateSource:
    return _install(tmp_path)[1]


def test_the_pair_page_and_its_scripts_are_served_without_a_credential_proxied_and_with_no_inline_script(src) -> None:
    with _serve(src) as (port, _h):
        for path, kind in (("/pair", "text/html"), ("/pair.js", "text/javascript"), ("/devices_panel.js", "text/javascript")):
            for headers in (None, PROXY):                    # off-box (his :8443 path) is the one that matters
                st, out, hdrs = _call(port, "GET", path, headers=headers)
                assert st == 200 and hdrs["content-type"].startswith(kind), (path, headers, st)
                assert hdrs["cache-control"] == "no-store" and "script-src 'self'" in hdrs["content-security-policy"]
        html = load_web_asset("pair.html")
        assert '<script src="/pair.js"></script>' in html and not re.search(r"<script(?![^>]*\bsrc=)", html)
        assert '<script src="/devices_panel.js"></script>' in load_web_asset("dashboard.html")


def test_the_pair_script_and_the_panel_use_the_dashboards_own_storage_key() -> None:
    boot = re.search(r'WRITE_TOKEN_KEY = "([^"]+)"', load_web_asset("dashboard_boot.js"))
    assert boot, "dashboard_boot.js no longer names its key"
    for name in ("pair.js", "devices_panel.js"):
        mine = re.search(r'WRITE_TOKEN_KEY = "([^"]+)"', load_web_asset(name))
        assert mine and mine.group(1) == boot.group(1), name
    # L3 r1 F6: the "paired by a link" mark pair.js sets is the one dashboard_boot.js reads to show the unpaired card
    marks = [re.search(r'PAIRED_MARK_KEY = "([^"]+)"', load_web_asset(n)) for n in ("pair.js", "dashboard_boot.js")]
    assert all(marks) and marks[0].group(1) == marks[1].group(1)
    # the claim sends the code and nothing else (RP-6: the label is the offer's; the server answers 400 to a label)
    assert re.search(r"JSON\.stringify\(\{ code: code \}\)", load_web_asset("pair.js"))


def test_the_device_row_carries_the_fields_the_panel_reads(src) -> None:
    with _serve(src) as (port, _h):
        st, off, _ = _call(port, "POST", "/cockpit/pair/offer", {"label": "Run iPad"}, token=T)
        assert st == 200 and "code" in off, (st, off)
        st, cl, _ = _call(port, "POST", "/cockpit/pair/claim", {"code": off["code"]})
        assert st == 200
        st, out, _ = _call(port, "GET", "/cockpit/pair/devices.json", token=T)
        (row,) = out["devices"]
        js = load_web_asset("devices_panel.js")
        for field in ("id", "label", "created", "last_seen", "revoked_at", "paired_by"):
            assert field in row and re.search(r"\bd\.%s\b" % field, js), field
        assert row["label"] == "Run iPad" and row["revoked_at"] is None
        assert off["pair_path"] == "/pair#code=" + off["code"]     # the panel prefixes location.origin to this


NAV = {"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": "navigate", "Sec-Fetch-Dest": "document"}


def test_a_pair_link_opened_from_another_site_loads_and_nothing_else_widens(src) -> None:
    # L3 r1 F1: a link clicked in webmail is a cross-site top-level navigation; the page must load (it carries no data).
    with _serve(src) as (port, _h):
        for extra in (None, PROXY):
            st, _o, hdrs = _call(port, "GET", "/pair", headers={**NAV, **(extra or {})})
            assert st == 200 and hdrs["content-type"].startswith("text/html"), (extra, st)
            assert hdrs["x-frame-options"] == "DENY"
        # a cross-site fetch / subresource load of the same page stays refused
        for mode, dest in (("cors", "empty"), ("no-cors", "empty"), ("no-cors", "iframe"), ("navigate", "iframe")):
            st, _o, _h2 = _call(port, "GET", "/pair", headers={**NAV, "Sec-Fetch-Mode": mode, "Sec-Fetch-Dest": dest})
            assert st == 403, (mode, dest, st)
        # no other route is widened by a navigation's headers: the cockpit page, the claim script, a data read, HEAD /pair
        for method, path in (("GET", "/"), ("GET", "/pair.js"), ("GET", "/cockpit/manifest.json"),
                             ("GET", "/pair/"), ("GET", "/pair?x=1"), ("HEAD", "/pair")):
            st, _o, _h2 = _call(port, method, path, headers=NAV)
            assert st == 403, (method, path, st)
        # the Host check still holds for the carve-out
        st, _o, _h2 = _call(port, "GET", "/pair", headers={**NAV, "Host": "evil.example"})
        assert st == 403
        # the claim POST is unchanged: cross-site refused
        st, _o, _h2 = _call(port, "POST", "/cockpit/pair/claim", {"code": "x"}, headers={**NAV, "Sec-Fetch-Mode": "cors", "Sec-Fetch-Dest": "empty"})
        assert st == 403

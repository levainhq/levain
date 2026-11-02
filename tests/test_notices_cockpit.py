"""The cockpit's Notices section and the Notify tokens block (N3), in devices_panel.js. Source checks (a notice is drawn with
textContent only; no link, no badge) and a real-browser drive over a scratch server: the deep link ``/?view=notices`` lands on
the section, a notice's markup and URL stay inert text, dismiss is the one action, a revoked source is tagged, and a notify
token is minted once with its exact curl, listed without a hash, and revoked. Phone width and desktop, light and dark."""
from __future__ import annotations

import re

import pytest

from levain.web_server import load_web_asset
from levain.webpush import notify_tokens as nt
from levain.webpush.store import PushStore
from tests.test_cockpit_presence import _asset
from tests.test_cockpit_k2a import _install
from tests.test_cockpit_pairing_http import T
from tests.test_cockpit_webpush import _signed
from tests.test_pair_card_w1b import _drive, needs_browser

SEC = "document.getElementById('notices')"


def _section_source() -> str:
    js = _asset("devices_panel.js")
    start = js.index("// ---- Notices: what your own services")
    end = js.index("// ---- notify tokens: what lets")
    return js[start:end]


def test_the_notices_section_is_drawn_with_text_only_and_carries_no_link_or_count():
    src = "\n".join(ln for ln in _section_source().splitlines() if not ln.lstrip().startswith("//"))
    assert "innerHTML" not in src and "insertAdjacentHTML" not in src and "outerHTML" not in src
    assert "createElement(\"a\")" not in src and 'el("a"' not in src and ".href" not in src
    assert "badge" not in src.lower() and "count" not in src.lower()
    # every field a service chose is set through el()'s textContent
    for field in ("n.title", "n.body", "n.source"):
        assert field in src
    # the one action is Dismiss; the row's other control only expands
    assert src.count("dev-btn nt-dismiss") == 1 and "nt-toggle" in src


def test_no_innerhtml_anywhere_in_the_files_this_slice_touches():
    for name in ("devices_panel.js",):
        assert "innerHTML" not in _asset(name), name


def test_the_notices_section_is_in_no_needs_board_tray_or_agents_view():
    for name in ("dashboard_core.js", "board_view.js", "cockpit_view.js", "topology_view.js", "dashboard_boot.js"):
        js = _asset(name)
        assert "/webpush/notices" not in js, name


def test_the_deep_link_and_the_sheet_link_reach_the_section():
    js = _asset("devices_panel.js")
    assert '"view") === "notices"' in js and 'ntLink.href = "#notices"' in js and 'ntSec.id = "notices"' in js


def test_the_server_deep_link_is_the_one_the_page_reads():
    from levain.webpush import notify
    assert notify.NOTICES_URL == "/?view=notices"


def _seed(q):
    nt.create(q, "Backup", urgent=True)
    nt.create(q, "Gone")
    st = PushStore(q)
    st.add_notice("Backup", "Nightly backup done", "ok https://example.test/x?a=1 <b>bold</b> <img src=x onerror=alert(1)>",
                  "normal", "Backup", "recorded")
    st.add_notice("Gone", "Old source", "stays with its label", "low", "Gone", "recorded")
    st.add_notice("Backup", "Disk almost full", "line one\nline two", "urgent", "Backup", "recorded")
    nt.revoke(q, "Gone")


@pytest.fixture
def rig(tmp_path):
    q = tmp_path / "signq"
    q.mkdir(mode=0o700)
    return _install(tmp_path)[1], q


@needs_browser
@pytest.mark.parametrize("size,scheme", [((1280, 900), "light"), ((390, 844), "dark"), ((390, 844), "light"), ((1280, 900), "dark")])
def test_the_notices_card_renders_inert_dismisses_and_stays_in_bounds(rig, tmp_path, monkeypatch, size, scheme):
    src, q = rig
    _seed(q)
    w, h = size
    with _signed(src, q) as (port, _):
        out = _drive(tmp_path, monkeypatch, f"http://127.0.0.1:{port}/#write_token={T}", [
            {"emulate": {"w": w, "h": h, "dpr": 2 if w < 500 else 1, "mobile": w < 500}},
            {"scheme": scheme},
            {"until": f"{SEC} && !{SEC}.hidden", "ms": 20000},        # the token is kept; now follow a notice's own link
            {"eval": "location.assign('/?view=notices') || 1"},
            {"until": f"{SEC} && !{SEC}.hidden && {SEC}.querySelectorAll('.nt-row').length === 3", "ms": 20000},
            {"eval": f"[{SEC}.querySelector('.zone-title').textContent, document.activeElement.id, location.search, "
                     f"[...{SEC}.querySelectorAll('.nt-row')].map(r => r.querySelector('.nt-title').textContent), "
                     f"[...{SEC}.querySelectorAll('.nt-row')].map(r => !!r.querySelector('.nt-revoked'))]"},
            # the markup in a body is text: no element was made from it, and no link exists in the section
            {"eval": f"[{SEC}.querySelectorAll('a, img, b, input, form').length, {SEC}.querySelectorAll('button').length, "
                     f"{SEC}.textContent.includes('<img src=x onerror=alert(1)>'), {SEC}.textContent.includes('https://example.test/x?a=1')]"},
            {"eval": f"getComputedStyle({SEC}.querySelector('.zone-head')).display"},
            {"eval": "[document.documentElement.scrollWidth <= window.innerWidth, getComputedStyle(document.body).backgroundColor]"},
            # expanding only toggles the text
            {"do": f"{SEC}.querySelectorAll('.nt-toggle')[1].click()", "until": f"{SEC}.querySelector('.nt-open')", "ms": 4000},
            {"eval": f"[{SEC}.querySelectorAll('.nt-open').length, {SEC}.querySelectorAll('.nt-row').length]"},
            {"do": f"{SEC}.querySelector('.nt-dismiss').click()", "until": f"{SEC}.querySelectorAll('.nt-row').length === 2", "ms": 8000},
        ])
    v = out["values"]
    head, inert, heading, bounds, expanded = v[3], v[4], v[5], v[6], v[8]
    assert heading == "flex"                                                        # the section's title is on screen
    assert head[0] == "Notices" and head[1] == "notices" and head[2] == ""          # focus landed, the query was dropped
    assert head[3] == ["Disk almost full", "Old source", "Nightly backup done"]      # newest first
    assert head[4] == [False, True, False]                                          # the revoked source is tagged
    assert inert == [0, 6, True, True]                                              # a toggle and a dismiss per row, nothing else
    assert bounds[0] is True                                                        # no sideways scroll
    assert expanded == [1, 3]
    assert [n["title"] for n in PushStore(q).notices()] == ["Old source", "Nightly backup done"]   # the newest was dismissed


@needs_browser
def test_a_notice_is_hidden_from_a_page_with_no_credential_and_empty_says_so(rig, tmp_path, monkeypatch):
    src, q = rig
    with _signed(src, q) as (port, _):
        out = _drive(tmp_path, monkeypatch, f"http://127.0.0.1:{port}/?view=notices#write_token={T}", [
            {"until": f"{SEC} && !{SEC}.hidden", "ms": 20000},
            {"eval": f"{SEC}.querySelector('.nt-empty').textContent"},
            {"eval": f"[document.querySelectorAll('.badge, .count').length, {SEC}.querySelectorAll('[class*=badge]').length]"},
        ])
    assert out["values"][1] == "No notices right now."
    assert out["values"][2][1] == 0


@needs_browser
def test_a_notify_token_is_minted_once_with_its_curl_listed_without_a_hash_and_revoked(rig, tmp_path, monkeypatch):
    src, q = rig
    with _signed(src, q) as (port, _):
        out = _drive(tmp_path, monkeypatch, f"http://127.0.0.1:{port}/#write_token={T}", [
            {"until": "document.querySelector('.nf-panel') && !document.querySelector('.nf-panel').hidden", "ms": 20000},
            {"eval": "document.querySelector('.nf-panel h2').textContent"},
            {"eval": "document.querySelector('.nf-urgent').parentElement.textContent"},
            {"do": "document.querySelector('.nf-name').value='Backup'; document.querySelector('.nf-source').value='nightly'; "
                   "document.querySelector('.nf-urgent').click(); document.querySelector('.nf-mint').click()",
             "until": "!document.querySelector('.nf-new').hidden", "ms": 8000},
            {"eval": "[document.querySelector('.nf-token').value, document.querySelector('.nf-curl').textContent]"},
            {"eval": "document.querySelector('.nf-row .dev-name').textContent + ' | ' + document.querySelector('.nf-row .dev-meta').textContent"},
            {"eval": "location.reload() || 1"},
            {"until": "document.querySelector('.nf-row')", "ms": 20000},
            {"eval": "[document.querySelector('.nf-new').hidden, document.querySelector('.nf-new').textContent, document.body.textContent.includes('lvn_')]"},
            {"do": "document.querySelector('.nf-revoke').click()", "until": "!document.querySelector('.nf-row')", "ms": 8000},
        ])
    v = out["values"]
    assert v[1] == "Notify tokens"
    assert "can wake you through sleep" in v[2]
    tok, curl = v[4]
    assert tok.startswith("lvn_") and len(tok) > 40
    assert curl == ("curl -H \"Authorization: Bearer " + tok + "\" -H \"Content-Type: application/json\" "
                    "-d '{\"title\":\"..\",\"body\":\"..\"}' http://127.0.0.1:%d/notify" % port)
    assert v[5].startswith("Backup (nightly) | May wake you through sleep") and "sha256" not in v[5]
    assert v[8][0] is True and v[8][1] == "" and v[8][2] is False               # gone after a reload, and no token text left anywhere
    assert nt.list_public(q) == []

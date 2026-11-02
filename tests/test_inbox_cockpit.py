"""The cockpit Inbox view (1101-6) and the ONE deep-link reader, in devices_panel.js. Source checks (text only, no link, the
reader is single and ignores an unknown view) and a real-browser drive over a scratch server: a push url
``/?view=inbox&n=<id>`` opens THAT item highlighted and read (not the cockpit home), ``/?view=needs`` reaches the Needs-you
area, an unknown view is ignored, mark all read, dismiss on a notice, the empty and gone states, phone width and desktop, light
and dark."""
from __future__ import annotations

import pytest

from levain.webpush import notify
from levain.webpush.store import PushStore
from tests.test_cockpit_presence import _asset
from tests.test_cockpit_k2a import _install
from tests.test_cockpit_pairing_http import T
from tests.test_cockpit_webpush import _signed
from tests.test_pair_card_w1b import _drive, needs_browser

SEC = "document.getElementById('inbox-view')"


def _source() -> str:
    js = _asset("devices_panel.js")
    return js[js.index("// ---- Inbox: what Levain pinged you"):js.index("// ---- Notices: what your own services")]


def test_the_inbox_is_drawn_with_text_only_and_has_no_link_and_no_badge_call():
    src = "\n".join(ln for ln in _source().splitlines() if not ln.lstrip().startswith("//"))
    assert "innerHTML" not in src and "insertAdjacentHTML" not in src and "outerHTML" not in src
    assert 'el("a"' not in src and ".href" not in src and "createElement(\"a\")" not in src
    assert "setAppBadge" not in _asset("devices_panel.js")          # the badge is a later lane: unread_count is only ready
    assert src.count("dev-btn ib-dismiss") == 1 and "ib-toggle" in src
    for field in ("it.title", "it.body", "it.source"):
        assert field in src


def test_there_is_one_deep_link_reader_and_it_ignores_an_unknown_view():
    js = _asset("devices_panel.js")
    assert js.count('get("view")') == 1 and js.count("get(\"n\")") == 1
    assert 'DEEP_VIEWS = ["needs", "notices", "inbox"]' in js
    assert "if (DEEP_VIEWS.indexOf(v) >= 0)" in js         # anything else leaves deep null


def test_the_needs_landing_rides_the_narrow_pages_own_jump_and_waits_for_the_area():
    js = _asset("devices_panel.js")
    body = js[js.index("function landNeeds"):js.index("function landNeeds") + 600]
    assert "[data-phone-jump=\"needs\"]" in body and "#ck-needs:not([hidden])" in body and "btn.click()" in body and "tries >= 20" in body


def test_the_entry_sits_beside_needs_and_jumps_to_the_section():
    html = _asset("dashboard.html")
    assert html.index('data-phone-jump="needs"') < html.index('data-phone-jump="inbox"') < html.index('data-phone-jump="board"')
    assert '#inbox-view:not([hidden])' in _asset("dashboard_core.js")


def test_the_server_links_are_the_ones_the_page_reads():
    assert notify.INBOX_URL == "/?view=inbox" and notify.inbox_url(7) == "/?view=inbox&n=n7" and notify.inbox_url(None) == "/?view=inbox"


@pytest.fixture
def rig(tmp_path):
    q = tmp_path / "signq"
    q.mkdir(mode=0o700)
    return _install(tmp_path)[1], q


def _seed(q):
    st = PushStore(q)
    p = st.add_ping("attention", "digest", "Daily Email Digest", "first line\nsecond <b>line</b> https://example.test/x")
    _, a = st.record_notice("notify", "notify:Backup", "[Backup] done", "Backup", "done", "ok <img src=x onerror=alert(1)>", "normal",
                            "Backup", "pushed")
    _, b = st.record_notice("notify", "notify:Backup", "[Backup] disk", "Backup", "disk", "almost full", "urgent", "Backup", "pushed")
    return p, a, b


def _rows(js_sec=SEC):
    return (f"[...{js_sec}.querySelectorAll('.ib-row')].map(r => [r.dataset.id, r.classList.contains('ib-unread'), "
            f"r.classList.contains('ib-hl'), r.classList.contains('ib-open')])")


@needs_browser
@pytest.mark.parametrize("size,scheme", [((390, 844), "dark"), ((1280, 900), "light")])
def test_a_push_link_opens_that_item_highlighted_and_read_not_home(rig, tmp_path, monkeypatch, size, scheme):
    src, q = rig
    p, a, b = _seed(q)
    w, h = size
    with _signed(src, q) as (port, _):
        out = _drive(tmp_path, monkeypatch, f"http://127.0.0.1:{port}/#write_token={T}", [
            {"emulate": {"w": w, "h": h, "dpr": 2 if w < 500 else 1, "mobile": w < 500}},
            {"scheme": scheme},
            {"until": f"{SEC} && !{SEC}.hidden", "ms": 20000},
            {"eval": f"location.assign('/?view=inbox&n=n{a}') || 1"},
            {"until": f"{SEC} && !{SEC}.hidden && {SEC}.querySelector('.ib-hl')", "ms": 20000},
            {"eval": f"[{_rows()}, location.search, document.activeElement.closest('.ib-row') && document.activeElement.closest('.ib-row').dataset.id, "
                     f"{SEC}.querySelector('.ib-count').textContent]"},
            {"eval": f"[{SEC}.querySelectorAll('a, img, b, input, form').length, {SEC}.textContent.includes('<img src=x onerror=alert(1)>')]"},
            {"eval": "document.documentElement.scrollWidth <= window.innerWidth"},
        ])
    rows, search, focused, count = out["values"][3]
    assert [r[0] for r in rows] == [f"n{b}", f"n{a}", f"p{p}"]                  # newest first
    assert [r[2] for r in rows] == [False, True, False]                          # the linked item, and only it, is highlighted
    assert rows[1][3] is True and rows[1][1] is False                            # open and READ
    assert rows[0][1] is True and rows[2][1] is True                             # the others stay unread
    assert search == "" and focused == f"n{a}" and count == "2 unread"
    assert out["values"][4] == [0, True] and out["values"][5] is True
    assert PushStore(q).inbox_counts()["unread"] == 2                           # the server marked it too
    assert {r["id"]: r["read"] for r in PushStore(q).inbox()}[f"n{a}"] is True


@needs_browser
def test_gone_item_empty_state_mark_all_read_dismiss_and_the_other_views(rig, tmp_path, monkeypatch):
    src, q = rig
    p, a, b = _seed(q)
    with _signed(src, q) as (port, _):
        out = _drive(tmp_path, monkeypatch, f"http://127.0.0.1:{port}/?view=inbox&n=n99999#write_token={T}", [
            {"until": f"{SEC} && !{SEC}.hidden && {SEC}.querySelector('.ib-row')", "ms": 20000},
            {"eval": f"[{SEC}.querySelector('.ib-note').textContent, {SEC}.querySelector('.ib-note').hidden, {SEC}.querySelector('.ib-hl')]"},
            {"do": f"{SEC}.querySelector('.ib-dismiss').click()", "until": f"{SEC}.querySelectorAll('.ib-row').length === 2", "ms": 8000},
            {"do": f"{SEC}.querySelector('.ib-all').click()", "until": f"!{SEC}.querySelector('.ib-unread')", "ms": 8000},
            {"eval": f"[{SEC}.querySelector('.ib-count').textContent, {SEC}.querySelector('.ib-all').hidden]"},
            # an unknown view is ignored: the page stays where it was and the address is untouched
            {"eval": "location.assign('/?view=nonsense') || 1"},
            {"until": f"{SEC} && !{SEC}.hidden", "ms": 20000},
            {"eval": "location.search"},
            # needs reaches the Needs-you area through its own jump, and the query goes
            {"eval": "location.assign('/?view=needs') || 1"},
            {"until": "location.search === ''", "ms": 20000},      # nothing needs him here, so the area is hidden: the reader gives up and clears
            {"eval": "location.search === ''"},
        ])
    v = out["values"]
    assert v[1][0] == "That one is gone." and v[1][1] is False and v[1][2] is None
    assert v[4] == ["", True]
    assert v[7] == "?view=nonsense" and v[10] is True
    assert [n["id"] for n in PushStore(q).notices()] == [a]                      # the dismissed notice (b) left the record's list
    assert PushStore(q).inbox_counts()["unread"] == 0


@needs_browser
def test_empty_inbox_says_so(rig, tmp_path, monkeypatch):
    src, q = rig
    with _signed(src, q) as (port, _):
        out = _drive(tmp_path, monkeypatch, f"http://127.0.0.1:{port}/?view=inbox#write_token={T}", [
            {"until": f"{SEC} && !{SEC}.hidden", "ms": 20000},
            {"eval": f"[{SEC}.querySelector('.ib-empty').textContent, {SEC}.querySelector('.ib-count').textContent]"},
        ])
    assert out["values"][1] == ["Nothing has pinged you.", ""]



def test_a_tap_is_a_mutation_a_read_that_began_before_it_cannot_paint_over():
    src = _source()
    body = src[src.index("async function markRead"):src.index("async function markAllRead")]
    assert "ibPending++; ibGen++" in body and "ibPending--; ibGen++" in body       # the same gen discipline dismiss and mark-all use
    assert "ibPending > 0" in src[src.index("async function readInbox"):src.index("async function landInbox")]


def test_only_a_literal_404_latches_the_inbox_off_and_a_403_waits_for_a_new_token():
    for fn, flag, pair in (("readInbox", "ibGone", "ibRefused"), ("readNotices", "ntGone", "ntRefused")):
        js = _asset("devices_panel.js")
        body = js[js.index(f"async function {fn}"):js.index(f"async function {fn}") + 2200]
        assert f"res.status === 404) {{ " in body and f"{flag} = true" in body
        assert "res.status === 404 || res.status === 403" not in body
        assert f"{pair} = token()" in body and f"{pair} === token()" in body


def test_the_gone_note_clears_on_the_next_read_and_the_deep_link_asks_for_its_item_by_id():
    src = _source()
    body = src[src.index("async function readInbox"):src.index("async function landInbox")]
    assert 'ibKind === "gone"' in body and '"?n=" + encodeURIComponent' in body and "res.data.item === null" in body


def test_the_inbox_tab_before_the_first_read_reads_and_says_loading_and_the_open_ids_sort_in_the_signature():
    src = _source()
    body = src[src.index("function toInbox"):]
    assert 'ibSay("Loading…"' in body and "readInbox(true)" in body
    assert "[...ibOpen].sort().join" in src


def test_the_needs_landing_scrolls_to_the_area_where_the_phone_tab_is_not_displayed():
    js = _asset("devices_panel.js")
    body = js[js.index("function landNeeds"):js.index("function landNeeds") + 900]
    assert "getClientRects().length" in body and "host.scrollIntoView" in body and "btn.click()" in body


@needs_browser
def test_a_push_link_to_a_retained_item_past_the_newest_page_opens_it_and_is_not_called_gone(rig, tmp_path, monkeypatch):
    import levain.web_server as ws

    src, q = rig
    st = PushStore(q)
    first = st.add_ping("attention", "digest", "the oldest ping", "kept, but past the page")
    for i in range(3):
        st.record_notice("notify", "notify:Backup", f"b{i}", "Backup", f"b{i}", "x", "normal", "Backup", "pushed")
    monkeypatch.setattr(ws, "_INBOX_LIMIT", 2)
    with _signed(src, q) as (port, _):
        out = _drive(tmp_path, monkeypatch, f"http://127.0.0.1:{port}/?view=inbox&n=p{first}#write_token={T}", [
            {"until": f"{SEC} && !{SEC}.hidden && {SEC}.querySelector('.ib-hl')", "ms": 20000},
            {"eval": f"[{_rows()}, {SEC}.querySelector('.ib-note').hidden, {SEC}.querySelector('.ib-count').textContent]"},
        ])
    rows, note_hidden, count = out["values"][1]
    hl = [r for r in rows if r[2]]
    assert [r[0] for r in hl] == [f"p{first}"] and hl[0][1] is False and hl[0][3] is True      # found, highlighted, open, READ
    assert note_hidden is True and count == "3 unread"
    assert {r["id"]: r["read"] for r in PushStore(q).inbox()}[f"p{first}"] is True

"""The notification center's page (S4.2): the strip's unread pill, the compact panel and the focus view, in real headless Chrome over a
real server on a scratch queue (the backend is S4.1, tests/test_webpush_center.py). Eight behaviours, each one a thing the operator
does or reads. What stays undischarged here is how it feels in his hands on desktop, iPad and iPhone, and a real push tap."""
from __future__ import annotations

import http.client
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from levain.consent.queue import ConsentQueue, authority_source
from levain.consent.questions import QuestionStore
from levain.webpush import held
from levain.webpush import notify_tokens as nt
from levain.webpush.store import PushStore
from tests._h4 import h4_home
from tests.test_cockpit_flip_parity import _drive, needs_browser
from tests.test_cockpit_k2a import _install
from tests.test_cockpit_pairing_http import T, _call
from tests.test_cockpit_webpush import _signed

MODAL = "document.getElementById('levain-modal')"
ROWS = f"{MODAL}.querySelectorAll('.nc-row')"
PILL = "document.querySelector('[data-nc-unread]')"
TOKEN_BODY = ("Nightly sync done.\n\nFull log: [log](https://argus.example/backups/1.log)\n\n"
              "Hostile: [click](javascript:alert(1)) and [mail](mailto:a@b.c) and [rel](/cockpit/x).\n\n"
              "Bare: https://example.org/p?a=1&b=2.")
FLOW_BODY = "**Want a reply (1)**\n\n1. Rocket.net: [Open the thread](https://mail.example/t/1)\n\n<b>raw html</b> <script>1</script>"


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    from levain.consent import band

    monkeypatch.setattr(band, "agent_process", lambda: None)
    monkeypatch.setattr("levain.consent.auto_away.input_idle_seconds", lambda: 9999.0)
    monkeypatch.setattr(held, "flush_if_due", lambda *a, **k: False)
    nt.MINUTE.clear()
    yield
    nt.MINUTE.clear()


def _post_notice(port: int, token: str, payload: dict) -> str:
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    c.request("POST", "/notify", body=json.dumps(payload).encode(), headers={"Content-Type": "application/json", nt.HEADER: token})
    r = c.getresponse()
    doc = json.loads(r.read() or b"{}")
    assert r.status == 200, doc
    return doc["item"]


class Rig:
    """A queue with one waiting approval (a ping that is about a Needs-you item), a dated ping, a token notice and a flow notice."""

    def __init__(self, tmp_path: Path) -> None:
        self.src = _install(tmp_path)[1]
        self.q = tmp_path / "signq"
        self.q.mkdir(mode=0o700)
        cq = ConsentQueue(self.q, authority=authority_source(self.q, anchor=None), home=h4_home(), clock=lambda: datetime.now(timezone.utc))
        self.req = cq.file(agent="claude_code", agent_session_id="s", tool_name="Write", tool_input={"file_path": "/w/a", "content": "x"},
                           cwd="/w", hold=timedelta(minutes=300), event_id="e1")
        qn = QuestionStore(self.q).file(agent="claude_code", agent_session_id="s2", what="Ship it?", options=[("a", "yes"), ("b", "no")],
                                        rec="a", blocks="x", cwd="/w")
        st = PushStore(self.q)
        st.add_ping("consent", f"request:{self.req.id}", "levain", "Write /w/a waits on you\n\nanswer on the phone")
        st.add_ping("question", f"question:{qn.n}", "levain", "claude_code asks: Ship it?")
        st.add_ping("dated", None, "levain", "Due today: hosting.com reschedule")
        _, self.token = nt.create(self.q, "Backup")
        _, self.flow = nt.create(self.q, "flow poster", "flow", origin="flow")

    def seed(self, port: int) -> dict[str, str]:
        ids = {"token": _post_notice(port, self.token, {"title": "B2 backup finished", "body": TOKEN_BODY}),
               "flow": _post_notice(port, self.flow, {"title": "Inbox digest", "body": FLOW_BODY, "source": "email"}),
               "low": _post_notice(port, self.flow, {"title": "Diogenes daily", "body": "one LOW", "priority": "low", "source": "diogenes"})}
        return ids


def _center(port: int) -> dict:
    return _call(port, "GET", "/webpush/center", token=T)[1]


def _url(port: int, query: str = "") -> str:
    return f"http://127.0.0.1:{port}/{query}#write_token={T}"


def _ready(count: int) -> dict:
    return {"until": f"(() => {{ const p = {PILL}; return p && !p.hidden && p.textContent === '{count}' && 1; }})()", "ms": 40000}


OPEN = {"eval": "document.querySelector('[data-phone-jump=notifications]').click(); 1"}
OPENED = {"until": f"{ROWS}.length >= 6 && 1", "ms": 30000}
SETTLE = {"eval": "new Promise(r => setTimeout(r, 400)).then(() => 1)"}


def _row(title: str, sub: str = "") -> str:
    return f"[...{ROWS}].find(r => r.querySelector('.nc-title').textContent === {json.dumps(title)})" + (f".querySelector('{sub}')" if sub else "")


def _json(out: dict) -> list:
    return [json.loads(v) for v in out["values"] if isinstance(v, str) and v.startswith(("{", "["))]


@needs_browser
def test_the_strip_draws_at_every_width_with_its_unread_count_and_opens_the_center(tmp_path: Path) -> None:
    """Notifications is on the strip at a wide and a narrow width, its pill is the server's unread count (the other buttons carry none),
    and a tap opens the focus view listing every item, the filter narrowing it, with no sideways scroll."""
    r = Rig(tmp_path)
    layout = ("(() => { const s = document.querySelector('#ck-phonetabs'), c = getComputedStyle(s); return JSON.stringify({ d: c.display, pos: c.position,"
              " tabs: [...s.querySelectorAll('button')].map(b => b.dataset.phoneJump), vw: document.documentElement.scrollWidth <= innerWidth + 1,"
              " pill: s.querySelector('[data-nc-unread]').textContent }); })()")
    opened = (f"JSON.stringify({{ rows: {ROWS}.length, sw: {MODAL}.scrollWidth <= {MODAL}.clientWidth + 1, title: {MODAL}.querySelector('h2').textContent }})")
    filtered = (f"(() => {{ const i = {MODAL}.querySelector('.nc-filter'); i.value = 'backup'; i.dispatchEvent(new Event('input', {{bubbles: true}}));"
                f" return JSON.stringify({{ narrowed: [...{ROWS}].map(r => r.querySelector('.nc-title').textContent) }}); }})()")
    with _signed(r.src, r.q) as (port, _):
        r.seed(port)
        total, unread = len(_center(port)["items"]), _center(port)["unread"]
        out = _drive(tmp_path, _url(port), [
            {"emulate": {"w": 1280, "h": 900, "dpr": 1}}, _ready(unread), {"eval": layout},
            {"emulate": {"w": 390, "h": 844, "dpr": 2, "mobile": True}}, SETTLE, {"eval": layout},
            OPEN, OPENED, {"eval": opened}, {"eval": filtered}])
    wide, narrow, op, fl = _json(out)
    for lay in (wide, narrow):
        assert lay["d"] == "flex" and lay["pos"] == "sticky" and lay["tabs"] == ["needs", "board", "notifications", "grants", "drop"], lay
        assert lay["vw"] is True and lay["pill"] == str(unread), lay
    assert op["rows"] == total and op["sw"] is True and op["title"] == "Notifications", op
    assert fl["narrowed"] == ["B2 backup finished"], fl


@needs_browser
def test_the_dot_toggles_read_and_unread_and_the_pill_follows_what_the_server_holds(tmp_path: Path) -> None:
    """The dot is the per-item toggle: one tap marks the row read (the server's mark, then the strip's count drops), the next marks it unread."""
    r = Rig(tmp_path)
    dot = _row("Inbox digest", ".nc-dot-btn")
    with _signed(r.src, r.q) as (port, _):
        ids = r.seed(port)
        n = _center(port)["unread"]
        out = _drive(tmp_path, _url(port), [{"emulate": {"w": 1280, "h": 900, "dpr": 1}}, _ready(n), OPEN, OPENED,
                                            {"eval": f"{dot}.click(); 1"}, _ready(n - 1), {"eval": "new Promise(r => setTimeout(r, 300)).then(() => 1)"}])
        read = {i["id"]: i["read"] for i in _center(port)["items"]}
        out2 = _drive(tmp_path, _url(port), [{"emulate": {"w": 1280, "h": 900, "dpr": 1}}, _ready(n - 1), OPEN, OPENED,
                                             {"eval": f"{dot}.click(); 1"}, _ready(n)])
        back = {i["id"]: i["read"] for i in _center(port)["items"]}
    assert read[ids["flow"]] is True and back[ids["flow"]] is False, (read, back)
    assert out["values"] and out2["values"], (out, out2)


@needs_browser
def test_mark_all_read_empties_the_pill_and_the_button_and_the_server_agrees(tmp_path: Path) -> None:
    """Mark all read is shown only while something is unread; one tap and the pill hides, the button goes, the server holds zero unread."""
    r = Rig(tmp_path)
    with _signed(r.src, r.q) as (port, _):
        r.seed(port)
        n = _center(port)["unread"]
        out = _drive(tmp_path, _url(port), [
            {"emulate": {"w": 1280, "h": 900, "dpr": 1}}, _ready(n), OPEN, OPENED,
            {"eval": f"JSON.stringify({{ before: !{MODAL}.querySelector('.nc-markall').hidden }})"},
            {"eval": f"{MODAL}.querySelector('.nc-markall').click(); 1"},
            {"until": f"{PILL}.hidden && {MODAL}.querySelector('.nc-markall').hidden && 1"},
            {"eval": f"JSON.stringify({{ unread: {MODAL}.querySelector('.nc-unread').hidden }})"}])
        after = _center(port)
    before, now = _json(out)
    assert before == {"before": True} and now == {"unread": True}, (before, now)
    assert after["unread"] == 0 and after["total"] == len(after["items"]), after


@needs_browser
def test_delete_and_clear_never_close_a_needs_you_item_and_the_page_says_so(tmp_path: Path) -> None:
    """A ping about a waiting item says deleting it does not close the item; delete removes the row, clear empties the list, and through
    both the server still counts the waiting item and the page still offers the way to its card."""
    r = Rig(tmp_path)
    needs_row = "[...%s].find(r => /waits on you/.test(r.querySelector('.nc-title').textContent))" % ROWS
    with _signed(r.src, r.q) as (port, _):
        r.seed(port)
        n = _center(port)["unread"]
        before = _center(port)["needs_count"]
        out = _drive(tmp_path, _url(port), [
            {"emulate": {"w": 1280, "h": 900, "dpr": 1}}, _ready(n), OPEN, OPENED,
            {"eval": f"{needs_row}.querySelector('.nc-main').click(); 1"},
            {"until": f"{MODAL}.querySelector('.nc-needsbox') && 1"},
            {"eval": f"JSON.stringify({{ note: {MODAL}.querySelector('.nc-needsbox .nc-note').textContent, go: {MODAL}.querySelector('.nc-needsgo').textContent }})"},
            {"eval": f"{MODAL}.querySelector('.nc-delete').click(); 1"},
            {"until": f"!({needs_row}) && 1"},
            {"eval": f"JSON.stringify({{ line: {MODAL}.querySelector('.nc-needsline').textContent }})"},
            {"eval": f"{MODAL}.querySelector('.nc-morebtn').click(); {MODAL}.querySelector('.nc-menu-clear').click(); 1"},
            {"until": f"{MODAL}.querySelector('.nc-state h3') && 1"},
            {"eval": f"JSON.stringify({{ empty: {MODAL}.querySelector('.nc-state h3').textContent, line: !{MODAL}.querySelector('.nc-needsline').hidden }})"}])
        after = _center(port)
    note, line, empty = _json(out)
    assert "does not close the item" in note["note"] and note["go"].startswith("Answer on the Needs-you card"), note
    assert "Needs you (%d)" % before in line["line"], (line, before)
    assert empty == {"empty": "No notifications", "line": True}, empty
    assert after["items"] == [] and after["needs_count"] == before, (after, before)


@needs_browser
def test_a_push_tap_opens_that_item_marked_read_and_a_deleted_one_says_so(tmp_path: Path) -> None:
    """The tap's url (/?view=notifications&n=<id>) opens the focus view on that item and marks it read (the strip count drops, the address
    is cleaned); the same link to an item since deleted opens the list and says it was deleted."""
    r = Rig(tmp_path)
    with _signed(r.src, r.q) as (port, _):
        ids = r.seed(port)
        n = _center(port)["unread"]
        out = _drive(tmp_path, _url(port, f"?view=notifications&n={ids['flow']}"), [
            {"emulate": {"w": 1280, "h": 900, "dpr": 1}},
            {"until": f"{MODAL} && {MODAL}.querySelector('.nc-dtitle') && 1", "ms": 40000}, _ready(n - 1),
            {"eval": f"JSON.stringify({{ title: {MODAL}.querySelector('.nc-dtitle').textContent, url: location.search }})"}])
        read = {i["id"]: i["read"] for i in _center(port)["items"]}
        _call(port, "POST", "/webpush/center/delete", {"id": ids["low"]}, token=T)
        gone = _drive(tmp_path, _url(port, f"?view=notifications&n={ids['low']}"), [
            {"emulate": {"w": 1280, "h": 900, "dpr": 1}},
            {"until": f"{MODAL} && {MODAL}.querySelector('.nc-gone') && 1", "ms": 40000},
            {"eval": f"JSON.stringify({{ line: {MODAL}.querySelector('.nc-gone').textContent, rows: {ROWS}.length }})"}])
    got = _json(out)[0]
    assert got == {"title": "Inbox digest", "url": ""}, got
    assert read[ids["flow"]] is True
    line = _json(gone)[0]
    assert line["line"] == "That notification was deleted." and line["rows"] >= 1, line


@needs_browser
def test_a_token_senders_links_show_their_full_address_and_a_hostile_link_is_only_text(tmp_path: Path) -> None:
    """A notify-token sender's link shows its full URL beside the anchor text; a javascript:, mailto: or relative link is plain text, never
    a link; a flow notice's link is a normal link that shows its address too; raw HTML paints as literal text; every live link is _blank with rel noopener."""
    r = Rig(tmp_path)

    def detail(title: str) -> list:
        return [{"eval": f"{_row(title, '.nc-main')}.click(); 1"}, {"until": f"{MODAL}.querySelector('.nc-dtitle').textContent === {json.dumps(title)} && 1"},
                {"eval": (f"JSON.stringify({{ links: [...{MODAL}.querySelectorAll('.nc-body a')].map(a => [a.getAttribute('href'), a.target, a.rel, a.textContent]),"
                          f" full: [...{MODAL}.querySelectorAll('.nc-fullurl')].map(x => x.textContent), text: {MODAL}.querySelector('.nc-body').textContent,"
                          f" scripts: {MODAL}.querySelectorAll('.nc-body script, .nc-body b').length, note: !!{MODAL}.querySelector('.nc-tokennote') }})")}]
    with _signed(r.src, r.q) as (port, _):
        r.seed(port)
        out = _drive(tmp_path, _url(port), [{"emulate": {"w": 1280, "h": 900, "dpr": 1}}, {"until": f"{PILL} && !{PILL}.hidden && 1", "ms": 40000},
                                            OPEN, OPENED, *detail("B2 backup finished"), *detail("Inbox digest")])
    tok, flow = _json(out)
    assert tok["links"] == [["https://argus.example/backups/1.log", "_blank", "noopener noreferrer", "log"],
                            ["https://example.org/p?a=1&b=2", "_blank", "noopener noreferrer", "https://example.org/p?a=1&b=2"]], tok
    assert tok["full"] == [" (https://argus.example/backups/1.log)"] and tok["note"] is True, tok          # the bare URL is its own address
    assert "click" in tok["text"] and "mail" in tok["text"] and "rel" in tok["text"], tok                    # the hostile links keep their words
    assert flow["links"] == [["https://mail.example/t/1", "_blank", "noopener noreferrer", "Open the thread"]], flow
    assert flow["full"] == [" (https://mail.example/t/1)"], flow                                             # the address shows for every sender, not only a token's
    assert flow["scripts"] == 0 and "<b>raw html</b>" in flow["text"] and flow["note"] is False, flow


@needs_browser
def test_a_failed_write_and_a_failed_refresh_say_what_happened_and_keep_the_servers_list(tmp_path: Path) -> None:
    """A delete the server refuses says Delete didn't reach Levain and the row is back from the server's list; a refresh that cannot reach the
    server keeps the list and says it is showing the list as of a time, with Retry."""
    r = Rig(tmp_path)
    fail_delete = ("(() => { const f = window.fetch; window.__fail = 'delete'; window.fetch = (u, o) => (window.__fail === 'delete' && String(u).includes('/center/delete')"
                   " ? Promise.resolve(new Response(JSON.stringify({ok: false, message: 'no'}), {status: 503})) : window.__fail === 'read' && String(u).startsWith('/webpush/center?')"
                   " ? Promise.reject(new TypeError('offline')) : f(u, o)); return 1; })()")
    with _signed(r.src, r.q) as (port, _):
        r.seed(port)
        total = len(_center(port)["items"])
        out = _drive(tmp_path, _url(port), [
            {"emulate": {"w": 1280, "h": 900, "dpr": 1}}, {"until": f"{PILL} && !{PILL}.hidden && 1", "ms": 40000}, OPEN, OPENED, {"eval": fail_delete},
            {"eval": "window.__fail = 'read'; window.LevainNotifyCenter.read(true).then(() => 1)"},
            {"until": f"/as of/.test({MODAL}.querySelector('.nc-banner-t').textContent) && 1"},
            {"eval": f"JSON.stringify({{ r: {MODAL}.querySelector('.nc-banner-t').textContent, rows: {ROWS}.length, retry: !!{MODAL}.querySelector('.nc-banner .nc-btn') }})"},
            {"eval": "window.__fail = 'delete'; 1"},
            {"eval": f"{_row('Inbox digest')}.querySelector('.nc-main').click(); 1"}, {"until": f"{MODAL}.querySelector('.nc-delete') && 1"},
            {"eval": f"{MODAL}.querySelector('.nc-delete').click(); 1"},
            {"until": f"{MODAL}.querySelector('.nc-banner') && !{MODAL}.querySelector('.nc-banner').hidden && /Delete/.test({MODAL}.querySelector('.nc-banner').textContent) && {ROWS}.length === {total} && 1"},
            {"eval": f"JSON.stringify({{ w: {MODAL}.querySelector('.nc-banner-t').textContent, rows: {ROWS}.length }})"},
            {"eval": "window.__fail = 'read'; window.LevainNotifyCenter.read(true).then(() => 1)"}, SETTLE,
            {"eval": f"JSON.stringify({{ k: {MODAL}.querySelector('.nc-banner-t').textContent, rows: {ROWS}.length }})"}])
        held_rows = len(_center(port)["items"])
    rd, wr, kept = _json(out)
    assert wr["w"] == "Delete didn’t reach Levain. Nothing was changed." and wr["rows"] == total, wr
    assert rd["r"].startswith("Couldn’t refresh. Showing the list as of ") and rd["rows"] == total and rd["retry"] is True, rd
    assert kept["k"] == wr["w"] and kept["rows"] == total                              # a refresh that also fails never replaces the standing write failure
    assert held_rows == total                                                         # the server never lost the item either


@needs_browser
def test_the_phone_opens_a_pushed_detail_with_a_back_and_the_wide_page_shows_both_panes(tmp_path: Path) -> None:
    """At a phone's width the list is a full-screen sheet and a row pushes a detail with a back control (the list hidden meanwhile); at a
    desktop's width the list and the detail stand side by side and nothing is selected until a row is."""
    r = Rig(tmp_path)
    state = (f"JSON.stringify({{ list: getComputedStyle({MODAL}.querySelector('.nc-listpane')).display !== 'none',"
             f" detail: getComputedStyle({MODAL}.querySelector('.nc-detailpane')).display !== 'none', none: !!{MODAL}.querySelector('.nc-none'),"
             f" back: getComputedStyle({MODAL}.querySelector('.nc-back')).display !== 'none' }})")
    with _signed(r.src, r.q) as (port, _):
        r.seed(port)
        n = _center(port)["unread"]
        out = _drive(tmp_path, _url(port), [
            {"emulate": {"w": 1280, "h": 900, "dpr": 1}}, _ready(n), OPEN, OPENED, {"eval": state},
            {"key": "Escape"}, {"until": f"!{MODAL} && 1"},
            {"emulate": {"w": 390, "h": 844, "dpr": 2, "mobile": True}}, SETTLE, OPEN, OPENED, {"eval": state},
            {"eval": f"{_row('Inbox digest', '.nc-main')}.click(); 1"}, {"until": f"{MODAL}.querySelector('.nc-dtitle') && 1"}, SETTLE, {"eval": state},
            {"eval": f"{MODAL}.querySelector('.nc-back').click(); 1"}, SETTLE, {"eval": state}])
    wide, p_list, p_detail, p_back = _json(out)
    assert wide == {"list": True, "detail": True, "none": True, "back": False}, wide
    assert p_list["list"] is True and p_list["detail"] is False, p_list
    assert p_detail["list"] is False and p_detail["detail"] is True and p_detail["back"] is True, p_detail
    assert p_back["list"] is True and p_back["detail"] is False, p_back

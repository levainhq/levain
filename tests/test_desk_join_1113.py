"""The Desk view, JOINED (levain 33, lane DESK-C2): the real page (desk_tab_view.js mounted by dashboard_boot.js) in headless Chrome
at a phone's size against the REAL cockpit server (``make_server`` with a ``ScriptedDeskPort``). No stub stands between them.

Lanes B (the routes) and C (the page) built to ``desk_wire.md`` in parallel, with a stub on one side and a hand-made fixture on the
other, and had never run together. These tests are the first run that does: every state is drawn from the real JSON, a line is
filed through the real send route, and the port's own record is what the assertions read.

What this holds: the page against the server's REAL shapes, through a real browser engine. What it does not hold: a real flow
script behind the port (the port is the in-memory double), a real phone, a person's look."""
from __future__ import annotations

import json
import threading
import time
import urllib.request
from contextlib import contextmanager

import pytest

from levain import web_server
from levain.desk.desk_port import DeskCaps, ScriptedDeskPort
from tests.test_cockpit_flip_parity import _drive, needs_browser
from tests.test_cockpit_k2a import TOKEN, _install, _serve
from tests.test_cockpit_threads_mount import qdir  # noqa: F401  (fixture: the consent directory the server reads)
from tests.test_desk_pane_page import PHONE, _OPEN, _TAP_DESK, _TOKENED, _no_ban_words, typed, until

ASSET = "/desk_tab_view.js"
HOLDER = "desk 25, renewed 3 min ago"

# one JSON snapshot of the Desk view, with the buttons the ambiguous line offers
_SNAP = """(() => { const z = document.querySelector('.dv-zone'), q = s => z.querySelector(s), all = s => [...z.querySelectorAll(s)];
  const bar = q('.dv-state'), send = q('.rv-send'), ta = q('.rv-input');
  return JSON.stringify({ joined: 1, text: z.innerText, bar: bar ? bar.innerText : null, barCls: bar ? bar.className : null, drop: !!q('.dv-droplink'),
    lines: all('.rv-line').map(l => ({cls: l.className, text: l.innerText, marks: [...l.querySelectorAll('.dv-mark')].map(b => b.innerText)})),
    sendDisabled: send.disabled, taDisabled: ta.disabled, taValue: ta.value, unreadable: !!q('.dv-unreadable'),
    errorBar: !!q('.dv-state-error'), on: document.body.classList.contains('dv-on'),
    st: window.__st || null }); })()"""
SNAP = {"eval": _SNAP}
SEND = "document.querySelector('.dv-zone .rv-send')"


def snaps(out) -> list[dict]:
    return [json.loads(v) for v in out["values"] if isinstance(v, str) and v.startswith("{") and '"joined"' in v]


class Replying(ScriptedDeskPort):
    """A port whose desk answers: a send files the line, then the desk takes it and replies, as events on the thread."""

    def post(self, words, *, outbox_id, origin):
        result = super().post(words, outbox_id=outbox_id, origin=origin)
        if result.thread_id and not getattr(result, "replayed", False) and result.outcome == "accepted":
            self.script_event(result.thread_id, "script", "took", "", "09:01:02")
            self.script_event(result.thread_id, "desk", "reply", "on it, back by noon", "09:03:00")
        return result


@contextmanager
def join_page(tmp_path, port, **kw):
    """The real cockpit page and the real server with ``port`` wired (None = no desk wired). Yields the base URL."""
    _root, src, _ids = _install(tmp_path)
    opts = dict(write_token=TOKEN)
    if port is not None:
        opts["desk_port"] = port
    opts.update(kw)
    with _serve(src, **opts) as (base, _httpd):
        yield base


def _go(tmp_path, base, steps, wrap_fetch=False):
    pre = [{"eval": "window.__st = []; const of = window.fetch.bind(window); window.fetch = (u, o) => of(u, o).then(r => { window.__st.push([String(u), r.status]); return r; }); 1"}] if wrap_fetch else []
    return _drive(tmp_path, base + "/", [PHONE, *_TOKENED, *pre, _TAP_DESK, _OPEN, *steps])


# --- the asset -----------------------------------------------------------------------------------------------------------------

def test_the_real_server_serves_the_desk_script_the_page_names(tmp_path):
    assert ASSET in web_server._ASSETS and web_server._ASSETS[ASSET][0] == "desk_tab_view.js"
    with join_page(tmp_path, None) as base:
        with urllib.request.urlopen(base + ASSET, timeout=10) as r:  # noqa: S310 - loopback
            assert r.status == 200 and "javascript" in r.headers["Content-Type"]
            assert b"LevainDeskView" in r.read()


# --- every state, from the real JSON ------------------------------------------------------------------------------------------

@needs_browser
def test_a_line_is_filed_through_the_real_route_and_the_deskss_answers_draw_from_the_real_read(tmp_path, qdir):  # noqa: F811
    port = Replying()
    port.script_lease("held", "desk 25", 190)
    with join_page(tmp_path, port) as base:
        out = _go(tmp_path, base, [
            until("document.querySelector('.dv-lease-held')"), SNAP,
            typed("hello desk"), {"tap": SEND},
            until("document.querySelector('.dv-zone .rv-line.rv-room')"), SNAP])
    before, after = snaps(out)
    for s in (before, after):
        _no_ban_words(s)
        assert s["on"] and not s["errorBar"], s
    assert before["bar"] == HOLDER and not before["unreadable"] and not before["drop"], before     # readable with no thread: never an alarm
    assert before["sendDisabled"] is False and not before["taDisabled"], before
    assert [p.words for p in port.posts] == ["hello desk"] and port.posts[0].origin.startswith("cockpit:"), port.posts
    you = after["lines"][0]
    assert you["text"].startswith("hello desk") and "Filed: Filed (thread th-sc1)." in you["text"], you
    texts = [l["text"] for l in after["lines"][1:]]
    assert texts[0] == "Desk took it at 09:01:02 (script)", texts
    assert texts[1].startswith("Desk · 09:03:00") and "on it, back by noon" in texts[1], texts
    assert after["taValue"] == "", after                                                         # a filed line leaves the draft empty
    assert len(port.posts) == 1


@needs_browser
@pytest.mark.parametrize("lease, want, drop", [(("none", None, None), "held for the next desk", True), (("unknown", None, None), "will queue", False)])
def test_the_lease_states_the_real_server_names(tmp_path, qdir, lease, want, drop):  # noqa: F811
    port = ScriptedDeskPort()
    port.script_lease(*lease)
    with join_page(tmp_path, port) as base:
        out = _go(tmp_path, base, [until(f"document.querySelector('.dv-lease-{lease[0]}')"), SNAP])
    (s,) = snaps(out)
    _no_ban_words(s)
    assert want in s["bar"] and s["drop"] is drop and not s["taDisabled"] and s["sendDisabled"] is False, s
    if drop:
        assert "never starts a desk" in s["bar"], s


@needs_browser
def test_no_desk_wired_is_the_real_servers_wired_false(tmp_path, qdir):  # noqa: F811
    with join_page(tmp_path, None) as base:
        out = _go(tmp_path, base, [until("document.querySelector('.dv-state-unwired')"), SNAP])
    (s,) = snaps(out)
    assert s["bar"] == "This cockpit has no desk wired." and s["sendDisabled"] and s["taDisabled"], s


class GoesDark(ScriptedDeskPort):
    """A port whose receipts cannot be fetched once a line is filed."""

    def post(self, words, *, outbox_id, origin):
        result = super().post(words, outbox_id=outbox_id, origin=origin)
        self.script_unreadable()
        return result


@needs_browser
def test_an_unreadable_thread_is_could_not_be_fetched_never_no_events_yet(tmp_path, qdir):  # noqa: F811
    port = GoesDark()
    port.script_lease("held", "desk 25", 190)
    with join_page(tmp_path, port) as base:
        out = _go(tmp_path, base, [until("document.querySelector('.dv-lease-held')"), typed("one"), {"tap": SEND},
                                  until("document.querySelector('.dv-unreadable')"), SNAP])
    (s,) = snaps(out)
    assert "could not be fetched" in s["text"] and not s["errorBar"], s


@needs_browser
def test_the_servers_refusals_draw_as_the_servers_words(tmp_path, qdir):  # noqa: F811
    port = ScriptedDeskPort(caps=DeskCaps("scripted", "event", True, True, True, 20))
    port.script_lease("held", "desk 25", 190)
    port.script_post("not_delivered", reason="The host refused: nobody is at the desk.")
    with join_page(tmp_path, port) as base:
        out = _go(tmp_path, base, [
            until("document.querySelector('.dv-lease-held')"),
            typed("x" * 30), {"tap": SEND}, until("document.querySelector('.dv-zone .dv-refused')"), SNAP,
            typed("short line"), {"tap": SEND}, until("document.querySelectorAll('.dv-zone .dv-refused').length >= 2"), SNAP])
    cap, host = snaps(out)
    assert "over the limit of 20 characters" in cap["lines"][0]["text"] and "Nothing was cut" in cap["lines"][0]["text"], cap
    assert cap["taValue"] == "x" * 30, cap                                                 # the words stay for the person
    assert "The host refused: nobody is at the desk." in host["lines"][1]["text"], host
    assert [p.words for p in port.posts] == ["short line"], port.posts                     # the over-cap line wrote nothing
    for s in (cap, host):
        _no_ban_words(s)


@needs_browser
def test_a_400_from_the_real_route_draws_its_message_not_its_code(tmp_path, qdir):  # noqa: F811
    port = ScriptedDeskPort()
    port.script_lease("held", "desk 25", 190)
    with join_page(tmp_path, port) as base:
        out = _go(tmp_path, base, [until("document.querySelector('.dv-lease-held')"), typed("a\u0000b"), {"tap": SEND},
                                  until("document.querySelector('.dv-zone .dv-refused')"), SNAP])
    (s,) = snaps(out)
    assert "Not sent: " in s["lines"][0]["text"] and "bad_request" not in s["lines"][0]["text"], s
    assert port.posts == [], port.posts
    _no_ban_words(s)


# --- the ambiguous line: the page's own two marks, no route -------------------------------------------------------------------

@needs_browser
def test_it_did_not_arrive_sends_the_same_words_again_under_a_new_id_once_per_tap(tmp_path, qdir):  # noqa: F811
    port = ScriptedDeskPort()
    port.script_lease("held", "desk 25", 190)
    port.script_post("ambiguous", reason="the host command timed out")
    with join_page(tmp_path, port) as base:
        out = _go(tmp_path, base, [
            until("document.querySelector('.dv-lease-held')"), typed("line one"), {"tap": SEND},
            until("document.querySelector('.dv-zone .dv-ambiguous .dv-mark')"), SNAP,
            {"eval": "[...document.querySelectorAll('.dv-zone .dv-mark')].find(b => /did not arrive/i.test(b.innerText)).click(); 1"},
            until("document.querySelector('.dv-zone .dv-notarrived')"), SNAP,
            # two taps in one tick: the second finds the button spent
            {"eval": f"(() => {{ const b = {SEND}; b.click(); b.click(); return 1; }})()"},
            until("document.querySelector('.dv-zone .dv-filed')"), {"eval": "new Promise(r => setTimeout(r, 1500)).then(() => 1)"}, SNAP])
    asked, again, filed = snaps(out)
    assert asked["lines"][0]["marks"] == ["It arrived", "It did not arrive"], asked
    assert "It may not have arrived. It is not sent again; say whether it did." in asked["lines"][0]["text"], asked
    assert again["lines"][0]["marks"] == [] and again["taValue"] == "line one", again          # the same words wait in the box
    assert len(port.posts) == 2, port.posts                                                   # one for the ambiguous try, ONE for the tap
    assert port.posts[0].words == port.posts[1].words == "line one"
    assert port.posts[0].outbox_id != port.posts[1].outbox_id, port.posts                      # a NEW id: never the claim the server holds
    assert filed["lines"][-1]["text"].count("Filed") >= 1
    for s in (asked, again, filed):
        _no_ban_words(s)


@needs_browser
def test_it_arrived_settles_the_line_and_nothing_is_sent(tmp_path, qdir):  # noqa: F811
    port = ScriptedDeskPort()
    port.script_lease("held", "desk 25", 190)
    port.script_post("ambiguous", reason="the host command timed out")
    with join_page(tmp_path, port) as base:
        out = _go(tmp_path, base, [
            until("document.querySelector('.dv-lease-held')"), typed("line one"), {"tap": SEND},
            until("document.querySelector('.dv-zone .dv-ambiguous .dv-mark')"),
            {"eval": "[...document.querySelectorAll('.dv-zone .dv-mark')].find(b => b.innerText === 'It arrived').click(); 1"},
            until("document.querySelector('.dv-zone .dv-arrived')"), {"eval": "new Promise(r => setTimeout(r, 1500)).then(() => 1)"}, SNAP])
    (s,) = snaps(out)
    assert s["lines"][0]["marks"] == [] and "not confirmed" in s["lines"][0]["text"], s
    assert len(port.posts) == 1, port.posts
    _no_ban_words(s)



# --- a busy poll is a retry --------------------------------------------------------------------------------------------------

class Hogged(ScriptedDeskPort):
    """The desk's two poll slots are taken by someone else (a second device, a slow host command): the page's first poll answers,
    and the lease calls after it (the two hogs below) hang for a few seconds. The page's next poll is then the server's real 503.
    The hogs are python threads, not the page: a first attempt with two hung fetches from the page did not make the server
    answer the page's poll 503 (only three lease calls reached the server; the cause was not isolated)."""

    def __post_init__(self):
        super().__post_init__()
        self.base = ""
        self.calls = 0
        self.hogs: list[threading.Thread] = []
        self._lock = threading.Lock()

    def _hog(self):
        req = urllib.request.Request(self.base + "/cockpit/desk.json", headers={"X-Levain-Write-Token": TOKEN})
        try:
            urllib.request.urlopen(req, timeout=20).read()  # noqa: S310 - loopback
        except OSError:
            pass

    def lease(self):
        with self._lock:
            self.calls += 1
            n = self.calls
        if n == 1:                                   # the page's first poll answered: now take the two slots
            self.hogs = [threading.Thread(target=self._hog, daemon=True) for _ in range(2)]
            for t in self.hogs:
                t.start()
        elif n in (2, 3):
            time.sleep(4)
        return super().lease()


@needs_browser
def test_a_poll_answered_503_keeps_the_last_view_and_polls_again(tmp_path, qdir):  # noqa: F811
    port = Hogged()
    port.script_lease("held", "desk 25", 190)
    only = "window.__st.filter(x => x[0] === '/cockpit/desk.json')"
    with join_page(tmp_path, port) as base:
        port.base = base
        out = _go(tmp_path, base, [
            until("document.querySelector('.dv-lease-held')"),
            until(f"{only}.some(x => x[1] === 503)", 12000), SNAP,
            until(f"(() => {{ const s = {only}; const i = s.findIndex(x => x[1] === 503); return i >= 0 && s.slice(i).some(x => x[1] === 200); }})()", 20000), SNAP],
            wrap_fetch=True)
        for t in port.hogs:
            t.join(timeout=10)
    busy, later = snaps(out)
    assert busy["bar"] == HOLDER and not busy["errorBar"] and busy["barCls"].endswith("dv-lease-held"), busy      # the last view stays
    assert later["bar"] == HOLDER and not later["errorBar"], later
    assert any(x[1] == 503 for x in busy["st"]), busy["st"]

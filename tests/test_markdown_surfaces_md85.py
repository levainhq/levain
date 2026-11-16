"""levain#85: every cockpit surface that shows an agent's reply draws it as markdown through the one shared renderer.

Driven in headless Chrome over the REAL cockpit page (its own dashboard_core.js, room_view.js, desk_tab_view.js, thread_view.js,
dashboard_chat.js and dashboard.css). One reply holds the four constructs a person writes (a heading, a list, a code fence, a link)
and each surface must show them as ELEMENTS, with no marker character left in the text. The Room and the notification detail are
held by tests/test_room_markdown_1112.py and tests/test_notify_center_markdown.py.

STRUCTURAL: what is measured is the built page; how it reads on his screens is his look."""
from __future__ import annotations

import json

from tests.test_cockpit_flip_parity import _drive, needs_browser
from tests.test_desk_pane_page import (
    _OPEN, _TAP_DESK, _TOKENED, DeskStub, desk_page, put, until, wired,  # noqa: F401
)

REPLY = ("## The plan\n"
         "Here is [a link](https://example.com/docs) and `inline`.\n"
         "\n"
         "- first item\n"
         "- second item\n"
         "\n"
         "```py\n"
         "def f(x):\n"
         "    return x * 2\n"
         "```\n")
_THREAD_T9 = {"eval": "sessionStorage.setItem('levain.desk.thread', JSON.stringify('t9')); 1"}
CODE = "def f(x):\n    return x * 2"

# what one reply box holds, read off the page: structure as elements and no marker characters in the text
_LOOK = """((sel) => { const n = document.querySelector(sel); if (!n) return JSON.stringify({found: false});
  return JSON.stringify({found: true, cls: n.className, tag: n.tagName,
    heads: [...n.querySelectorAll('.md-h')].map(h => h.textContent),
    items: [...n.querySelectorAll('ul.md-ul > li.md-li')].map(li => li.textContent),
    pre: [...n.querySelectorAll('pre.md-pre code')].map(c => c.textContent),
    links: [...n.querySelectorAll('a.md-link')].map(a => ({href: a.getAttribute('href'), text: a.textContent})),
    text: n.textContent, white: getComputedStyle(n).whiteSpace}); })(%s)"""


def _look(out, _sel=None):
    marker = '"found"'
    vals = [v for v in out["values"] if isinstance(v, str) and v.startswith("{") and marker in v]
    return json.loads(vals[-1])


def _elements(g):
    assert g["found"], g
    assert g["heads"] == ["The plan"], g
    assert g["items"] == ["first item", "second item"], g
    assert g["pre"] == [CODE], g
    assert g["links"] == [{"href": "https://example.com/docs", "text": "a link"}], g
    for raw in ("## ", "```", "](https", "- first"):
        assert raw not in g["text"], (raw, g["text"])
    assert g["white"] == "normal", g          # the box no longer preserves source newlines: the blocks carry their own breaks


@needs_browser
def test_the_desk_view_draws_the_desks_reply_as_markdown_elements_and_keeps_its_speaker(tmp_path):
    sel = ".dv-zone .rv-room .rv-text"
    with desk_page(tmp_path) as (stub, base):
        out = _drive(tmp_path, base + "/", [*_TOKENED, _THREAD_T9, put(get={"status": 200, "json": wired(thread_id="t9", cursor="c1", events=[
            {"by": "desk", "kind": "reply", "text": REPLY, "at": "12:00:00"},
            {"by": "script", "kind": "reply", "text": "**not** markdown from a script", "at": "12:00:01"}])}),
            _TAP_DESK, _OPEN, until("document.querySelector('.dv-zone .rv-room .rv-text')"),
            {"eval": _LOOK % json.dumps(sel)},
            {"eval": "JSON.stringify({voice: document.querySelectorAll('.dv-zone .rv-room .voice-btn').length,"
                     " other: (document.querySelectorAll('.dv-zone .rv-room .rv-text')[1] || {}).textContent,"
                     " otherEls: document.querySelectorAll('.dv-zone .rv-room .rv-text')[1].querySelectorAll('strong').length})"}])
    g = _look(out, sel)
    _elements(g)
    assert "rv-md" in g["cls"], g
    rest = json.loads(out["values"][-1])
    assert rest["voice"] >= 1, rest                                   # the speaker control is still on the desk's reply
    assert rest["other"] == "**not** markdown from a script" and rest["otherEls"] == 0, rest   # only the desk's own reply is markdown


@needs_browser
def test_the_desk_reply_the_voice_reads_is_the_plain_words_not_the_drawn_nodes(tmp_path):
    with desk_page(tmp_path) as (stub, base):
        out = _drive(tmp_path, base + "/", [*_TOKENED, _THREAD_T9, put(get={"status": 200, "json": wired(thread_id="t9", cursor="c1", events=[
            {"by": "desk", "kind": "reply", "text": REPLY, "at": "12:00:00"}])}),
            {"eval": "window.__said = []; const real = window.LevainVoice.tracker; window.LevainVoice.tracker = function () {"
                     " const T = real.apply(this, arguments), sync = T.sync; T.sync = function (o) {"
                     " (o.replies || []).forEach(r => window.__said.push(r.text)); return sync.apply(this, arguments); }; return T; }; 1"},
            _TAP_DESK, _OPEN, until("document.querySelector('.dv-zone .rv-room .rv-text .md-h')"),
            until("window.__said.length >= 1"),
            {"eval": "JSON.stringify({said: window.__said})"}])
    said = json.loads(out["values"][-1])["said"]
    assert said and said[-1] == REPLY, said          # the words handed to the voice are the reply as written


# --- the thread view's agent line -----------------------------------------------------------------------------------

_THREAD = """(() => { const host = document.createElement('div'); host.id = 'md85-thread'; document.body.appendChild(host);
  const TV = window.LevainThreadView;
  const body = {thread_id: 't1', launch_id: 'L1', stream: [
    {gseq: 1, kind: 'vendor_agent_text', source: 'vendor', ref: 'a:1', launch_id: 'L1', body: %s, at: ''},
    {gseq: 2, kind: 'message', source: 'operator', ref: 'm:2', launch_id: 'L1', status: 'accepted', body: '**mine** stays text', at: ''}]};
  TV.renderThread(document, host, TV.threadModel(body), {open: {}});
  const mine = [...host.querySelectorAll('.tv-message .tv-body')][0];
  host.id = 'md85-thread-done'; document.body.classList.add('md85-thread-ready');
  return JSON.stringify({ok: 1, mineEls: mine ? mine.querySelectorAll('strong').length : -1}); })()""" % json.dumps(REPLY)


@needs_browser
def test_the_thread_view_draws_the_agents_line_as_markdown_elements_and_leaves_the_rest_plain(tmp_path):
    sel = "#md85-thread-done .tv-vendor_agent_text .tv-body"
    with desk_page(tmp_path) as (stub, base):
        out = _drive(tmp_path, base + "/", [{"until": "window.LevainThreadView && window.LevainMD"},
                                           {"eval": _THREAD}, {"eval": _LOOK % json.dumps(sel)}])
    g = _look(out, sel)
    _elements(g)
    assert g["tag"] == "DIV" and "rv-md" in g["cls"], g
    first = json.loads(next(v for v in out["values"] if isinstance(v, str) and v.startswith('{"ok"')))
    assert first["mineEls"] == 0, first             # a line that is not the agent's is not drawn as markdown


# --- the chat panel's reply -------------------------------------------------------------------------------------------

# the chat routes, answered in the page. The shipped page carries no nav.tabs (the strip the panel mounts on), so the test adds the strip: the panel is the page's own dashboard_chat.js, run again over this fetch
_CHAT_WITH = """(() => { const real = window.fetch.bind(window); const J = (s, o) => Promise.resolve(new Response(JSON.stringify(o), {status: s, headers: {'Content-Type': 'application/json'}}));
  window.fetch = (u, o) => { const p = String(u).split('?')[0];
    if (p === '/chat.json') return J(200, {entities: ['ent'], model: 'm'});
    if (p === '/chat/open') return J(202, {session_id: 's1', job_id: 'j1'});
    if (p === '/chat/turn') return J(202, {job_id: 'j2'});
    if (p === '/chat/job.json') return J(200, String(u).indexOf('j1') >= 0 ? {status: 'done', result: {session: {state: 'idle', workspace: 'w'}}}
                                                                        : {status: 'done', result: {reply: %s, tool_activity: []}});
    return real(u, o); };
  sessionStorage.setItem('levain.chat_token', 'tok');
  if (!document.querySelector('nav.tabs')) { const nav = document.createElement('nav'); nav.className = 'tabs'; document.body.appendChild(nav); }
  const s = document.createElement('script'); s.src = document.querySelector('script[src*="dashboard_chat.js"]').getAttribute('src'); document.body.appendChild(s); return 1; })()"""
_CHAT = _CHAT_WITH % json.dumps(REPLY)


@needs_browser
def test_the_chat_panel_draws_the_entitys_reply_as_markdown_elements(tmp_path):
    sel = ".chat-panel .chat-msg.them .chat-text"
    with desk_page(tmp_path) as (stub, base):
        out = _drive(tmp_path, base + "/", [{"until": "window.LevainRoomView && document.getElementById('board')"},
                                           {"eval": _CHAT},
                                           {"tap": "[...document.querySelectorAll('.chat-btn')].find(b => b.textContent === 'Start session')"},
                                           {"until": "document.querySelector('.chat-area')"},
                                           {"eval": "(() => { const a = document.querySelector('.chat-area'); a.value = 'hello'; return 1; })()"},
                                           {"tap": "[...document.querySelectorAll('.chat-btn')].find(b => b.textContent === 'Send')"},
                                           {"until": "document.querySelector('.chat-msg.them .chat-text')"},
                                           {"eval": _LOOK % json.dumps(sel)}])
    g = _look(out, sel)
    _elements(g)
    assert "rv-md" in g["cls"], g


_NO_REPLY_LOOK = """JSON.stringify({found: true, them: [...document.querySelectorAll('.chat-panel .chat-msg.them .chat-text')].map(n => n.textContent),
  err: [...document.querySelectorAll('.chat-panel .chat-msg.err .chat-text')].map(n => n.textContent)})"""


@needs_browser
def test_the_chat_panel_with_no_reply_never_draws_the_word_null(tmp_path):
    # a job whose result carries reply: null. The panel's callers draw a reply only when it is truthy, so this turn shows the no-reply
    # notice; replyText itself draws "" for a null/undefined reply (never String(null)), so no bubble anywhere reads "null"/"undefined"
    with desk_page(tmp_path) as (stub, base):
        out = _drive(tmp_path, base + "/", [{"until": "window.LevainRoomView && document.getElementById('board')"},
                                           {"eval": _CHAT_WITH % "null"},
                                           {"tap": "[...document.querySelectorAll('.chat-btn')].find(b => b.textContent === 'Start session')"},
                                           {"until": "document.querySelector('.chat-area')"},
                                           {"eval": "(() => { const a = document.querySelector('.chat-area'); a.value = 'hello'; return 1; })()"},
                                           {"tap": "[...document.querySelectorAll('.chat-btn')].find(b => b.textContent === 'Send')"},
                                           {"until": "document.querySelector('.chat-panel .chat-msg.err .chat-text')"},
                                           {"eval": _NO_REPLY_LOOK}])
    g = _look(out)
    assert g["err"] == ["The entity returned no reply."], g
    for t in g["them"]:
        assert t.strip() not in ("null", "undefined"), g


# --- the Sessions transcript's agent lines ----------------------------------------------------------------------------------

@needs_browser
def test_the_sessions_transcript_draws_an_agents_reply_as_markdown_elements_and_leaves_the_rest_text(tmp_path, monkeypatch):
    from tests.test_cockpit_k2a import _serve
    from tests.test_cockpit_session_page import ROW_CC, _log, _open_modal
    from tests.test_cockpit_sessions import _fixture

    root, src = _fixture(tmp_path, monkeypatch)
    _log(root, [
        {"at": "2026-10-15T10:00:00Z", "kind": "prompt", "text": "**mine** stays text"},
        {"at": "2026-10-15T10:00:05Z", "kind": "reply", "text": REPLY},
        {"at": "2026-10-15T10:00:09Z", "kind": "wake_reply", "text": "wake: **nothing** needs you"}])
    with _serve(src, surface_tokens={"browser": "T"}, install_class="reported") as (url, _httpd):
        out = _open_modal(f"{url}/#write_token=T", tmp_path, [
            {"eval": f"{ROW_CC}.querySelector('.sess-tx-btn').click(); 1"},
            {"until": f"{ROW_CC}.querySelector('.sess-ev')"},
            {"eval": f"JSON.stringify({{kinds: [...{ROW_CC}.querySelectorAll('.sess-kind')].map(k => k.textContent),"
                     f" strong: [...{ROW_CC}.querySelectorAll('.sess-ev')].map(e => e.querySelectorAll('strong').length)}})"},
            {"eval": _LOOK % json.dumps("#levain-modal .sess-reply .sess-text")}])
    g = json.loads(out[-1])
    _elements(g)
    assert "rv-md" in g["cls"], g
    kinds = json.loads(out[-2])
    assert kinds["kinds"][:3] == ["you", "agent", "wake reply"], kinds
    assert kinds["strong"][0] == 0 and kinds["strong"][2] == 1, kinds    # his own line stays text; the wake reply is drawn

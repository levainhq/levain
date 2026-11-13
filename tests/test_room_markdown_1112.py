"""1112-36 (gap 9, Phill: the Room chat "just shows it raw"): the Room's own replies are drawn by the cockpit's one reviewed
markdown renderer, the one every other panel of the page already uses. Driven in headless Chrome over the REAL cockpit page
(the Room tab, the page's own dashboard_core.js and dashboard.css), at a desktop and a phone width.

The read of the room is the real one with rows appended in the page, because a scripted seat cannot be made to say a chosen
markdown body. A body is untrusted text (the seat reads web pages), so the safety tests here are about what a hostile body
becomes on the page: inert text, never an element, never a live link, never a script run.

STRUCTURAL: the numbers are measured from the built page; how it reads on his iPhone and iPad is his look."""
from __future__ import annotations

import json
import os
from pathlib import Path

from tests.test_cockpit_flip_parity import _drive, needs_browser
from tests.test_cockpit_k2a import TOKEN, _serve
from tests.test_cockpit_room_join import _rig
from tests.test_cockpit_threads_mount import qdir  # noqa: F401  (fixture)
from tests.test_room_fulltab_1106 import _ENTER

WEB = Path(__file__).resolve().parents[1] / "levain" / "templates" / "web"

YOURS = "my own line with **not bold** and `not code`"
MD = ("## The plan\n"
      "Here is **bold**, `inline` and a [link](https://example.com/docs).\n"
      "\n"
      "- first item\n"
      "- second item\n"
      "- third item\n"
      "\n"
      "```py\n"
      "def f(x):\n"
      "    return x * 2\n"
      "```\n"
      "\n"
      "| name | value |\n"
      "|------|-------|\n"
      "| a    | 1     |\n")
CODE = "def f(x):\n    return x * 2"
HOSTILE = ("Look: <script>window.pwned = 1</script> <img src=x onerror=\"window.pwned = 2\"> "
           "[click](javascript:window.pwned=3) [tab](java\tscript:window.pwned=4) [up](JAVASCRIPT:window.pwned=5) "
           "[ok](https://example.com/ok) [rel](/relative) [proto](//evil.example/x) [d](data:text/html,<b>x</b>)\n"
           "<iframe src=\"https://evil.example\"></iframe> and a **bold** word")
LONG = "**b** " * 4000        # one line past the plain-text bound: drawn as recorded, never parsed

_ROWS = [
    {"gseq": 9001, "kind": "message", "source": "operator", "ref": "cm-9001", "launch_id": None, "status": "accepted", "body": YOURS,
     "at": "2026-11-12T10:00:00+00:00"},
    {"gseq": 9002, "kind": "vendor_agent_text", "source": "vendor", "ref": "agent_text:9002", "launch_id": "L1", "status": None,
     "body": MD, "at": "2026-11-12T10:00:01+00:00"},
    {"gseq": 9003, "kind": "vendor_agent_text", "source": "vendor", "ref": "agent_text:9003", "launch_id": "L1", "status": None,
     "body": HOSTILE, "at": "2026-11-12T10:00:02+00:00"},
    {"gseq": 9004, "kind": "vendor_agent_text", "source": "vendor", "ref": "agent_text:9004", "launch_id": "L1", "status": None,
     "body": LONG, "at": "2026-11-12T10:00:03+00:00"},
]

# every read of the room on the page gets the rows appended; set before the Room tab mounts, so the mount's fetch is the wrapped one
_WRAP = ("(() => { window.__roomRows = %s; const real = window.fetch.bind(window);"
         " window.fetch = async (u, o) => { const r = await real(u, o); if (String(u).split('?')[0] !== '/cockpit/room.json') return r;"
         " const j = await r.json(); j.stream = (j.stream || []).concat(window.__roomRows);"
         " return new Response(JSON.stringify(j), {status: 200, headers: {'Content-Type': 'application/json'}}); }; return 1; })()"
         % json.dumps(_ROWS))
_LOOK = """(() => {
  const all = s => [...document.querySelectorAll(s)];
  const lines = all('.rv-line'), you = all('.rv-you .rv-text')[0], room = all('.rv-room .rv-text');
  const md = room[0], bad = room[1], long = room[2];
  const body = document.querySelector('.rv-body');
  const hrefs = [...bad.querySelectorAll('a')].map(a => ({href: a.getAttribute('href'), rel: a.getAttribute('rel'), target: a.getAttribute('target'), text: a.textContent}));
  return JSON.stringify({
    n: lines.length, rooms: room.length, vw: innerWidth,
    yourStrong: you.querySelectorAll('strong, code, em').length, yourText: you.textContent,
    mdClass: md.className, heads: [...md.querySelectorAll('.md-h')].map(h => h.textContent), headTags: [...md.querySelectorAll('.md-h')].map(h => h.tagName),
    items: [...md.querySelectorAll('ul.md-ul > li.md-li')].map(li => li.textContent),
    pre: [...md.querySelectorAll('pre.md-pre code')].map(c => c.textContent),
    strong: [...md.querySelectorAll('strong')].map(s => s.textContent), codes: [...md.querySelectorAll('code.md-code-i')].map(c => c.textContent),
    links: [...md.querySelectorAll('a.md-link')].map(a => ({href: a.getAttribute('href'), rel: a.getAttribute('rel'), target: a.getAttribute('target'), text: a.textContent})),
    mdText: md.textContent,
    badEls: bad.querySelectorAll('script, img, iframe, object, embed, [onerror], [onclick]').length, badText: bad.textContent,
    badStrong: [...bad.querySelectorAll('strong')].map(s => s.textContent), pwned: typeof window.pwned,
    allHrefs: hrefs,
    longMd: long.querySelectorAll('strong, p, br').length, longClass: long.className, longSame: long.textContent === %s,
    overflowX: body.scrollWidth > body.clientWidth + 1, pageX: document.documentElement.scrollWidth > innerWidth + 1,
  });
})()""" % json.dumps(LONG)


def _shot(name):
    d = os.environ.get("LEVAIN_ROOMC_SHOTS")
    return [{"shot": str(Path(d) / name), "full": False}] if d else []


def _page(tmp_path, qdir, emulate, shot=None):
    rs, _launcher, src = _rig(tmp_path, qdir)
    enter = list(_ENTER[:3]) + [{"eval": _WRAP}] + list(_ENTER[3:])
    with _serve(src, write_token=TOKEN, room_serve=rs) as (base, _h):
        out = _drive(tmp_path, base + "/", [{"emulate": emulate}] + enter + [
            {"until": "document.querySelectorAll('.rv-line').length >= 4"},
            {"eval": "new Promise(r => setTimeout(r, 400)).then(() => 1)"},
            {"eval": _LOOK}] + ([{"eval": "document.querySelectorAll('.rv-room .rv-text')[0].scrollIntoView({block: 'start'}); 1"}] + _shot(shot) if shot else []))
    return json.loads(next(v for v in out["values"] if isinstance(v, str) and v.startswith("{") and '"rooms"' in v))


def _drawn(g):
    """The reply drawn by the markdown renderer: structure as elements, and the page shows no marker characters."""
    assert g["n"] >= 4 and g["rooms"] == 3, g
    assert len(g["heads"]) == 1 and g["heads"][0] == "The plan", g
    assert g["items"] == ["first item", "second item", "third item"], g
    assert g["pre"] == [CODE], g
    assert g["strong"] == ["bold"] and g["codes"] == ["inline"], g
    assert g["links"] == [{"href": "https://example.com/docs", "rel": "noopener noreferrer nofollow", "target": "_blank", "text": "link"}], g
    for marker in ("## ", "**", "```", "](", "- first"):
        assert marker not in g["mdText"].replace("| ", ""), (marker, g["mdText"])
    assert "rv-md" in g["mdClass"], g
    # a table is not a shape the shared renderer draws: its rows stay readable as the lines they were
    assert "| name | value |" in g["mdText"], g


def _safe(g):
    """A hostile body is inert: its markup is text, its unsafe links are not links, nothing ran."""
    assert g["badEls"] == 0 and g["pwned"] == "undefined", g
    assert "<script>window.pwned = 1</script>" in g["badText"] and "<img src=x" in g["badText"] and "<iframe" in g["badText"], g
    assert [h["href"] for h in g["allHrefs"]] == ["https://example.com/ok", "/relative"], g      # the allow-listed schemes and a relative path only
    for h in g["allHrefs"]:
        assert h["rel"] == "noopener noreferrer nofollow" and h["target"] == "_blank", g
    assert g["badStrong"] == ["bold"], g                                                          # the safe markdown beside it still draws


def _plain_where_it_must_be(g):
    assert g["yourStrong"] == 0 and g["yourText"] == YOURS, g                                    # his own line is plain, as he typed it
    assert g["longMd"] == 0 and g["longSame"] and "rv-md" not in g["longClass"], g              # a line past the bound is drawn as recorded


def _fits(g):
    assert not g["overflowX"] and not g["pageX"], g


@needs_browser
def test_the_rooms_reply_is_drawn_as_markdown_on_a_desktop(tmp_path, qdir):
    g = _page(tmp_path, qdir, {"w": 1280, "h": 900, "dpr": 1}, shot="room_md_1280.png")
    _drawn(g)
    _fits(g)
    _plain_where_it_must_be(g)


@needs_browser
def test_the_rooms_reply_is_drawn_as_markdown_on_a_phone(tmp_path, qdir):
    g = _page(tmp_path, qdir, {"w": 390, "h": 844, "dpr": 3, "mobile": True}, shot="room_md_390.png")
    _drawn(g)
    _fits(g)
    _plain_where_it_must_be(g)


@needs_browser
def test_a_hostile_body_is_inert_text_never_an_element_a_live_link_or_a_script(tmp_path, qdir):
    _safe(_page(tmp_path, qdir, {"w": 1280, "h": 900, "dpr": 1}))


# --- gap 4: Start desk, a signed /fanin through the cockpit's own signing sheet -----------------------------------------------------

_DESK_LOOK = """(() => { const q = s => document.querySelector(s), r = n => { const b = n.getBoundingClientRect(); return {l: b.left, t: b.top, w: b.width, h: b.height, r: b.right}; };
  const d = q('.rv-desk'), n = q('.rv-rotate'), top = q('.rv-top'), sheet = document.getElementById('levain-sign-sheet');
  return JSON.stringify({desk: !!d, deskText: d && d.textContent, deskBox: d && r(d), rotBox: n && r(n), topBox: r(top), vw: innerWidth,
    drops: window.__drops || null, note: q('.rv-sendnote') && q('.rv-sendnote').textContent,
    sheet: !!sheet, frame: sheet && sheet.querySelector('iframe') ? sheet.querySelector('iframe').getAttribute('src') : null,
    pageX: document.documentElement.scrollWidth > innerWidth + 1, topOverflow: top.scrollWidth > top.clientWidth + 1}); })()"""
_STUB = "(() => { window.__drops = []; const S = window.LevainSignSheet, o = S.openDrop; S.openDrop = (w) => { window.__drops.push(w); return %s; }; return 1; })()"


def _desk(tmp_path, qdir, emulate, stub=None, shot=None):
    rs, _launcher, src = _rig(tmp_path, qdir)
    steps = ([{"eval": _STUB % stub}] if stub is not None else []) + [
        {"eval": "document.querySelector('.rv-desk').click(); 1"},
        {"eval": "new Promise(r => setTimeout(r, 300)).then(() => 1)"}, {"eval": _DESK_LOOK}]
    with _serve(src, write_token=TOKEN, room_serve=rs) as (base, _h):
        out = _drive(tmp_path, base + "/", [{"emulate": emulate}] + list(_ENTER) + steps + (_shot(shot) if shot else []))
    return json.loads(next(v for v in out["values"] if isinstance(v, str) and v.startswith("{") and '"deskText"' in v))


def _bar_fits(g):
    # the control is a real touch target, sits inside the header beside New without pushing it off, and widens nothing
    assert g["desk"] and g["deskText"] == "Start desk", g
    assert g["deskBox"]["w"] >= 44 and g["deskBox"]["h"] >= 44, g
    assert g["deskBox"]["r"] <= g["rotBox"]["l"] + 0.5 and g["rotBox"]["r"] <= g["vw"] + 0.5, g
    assert not g["pageX"] and not g["topOverflow"], g


@needs_browser
def test_start_desk_hands_the_signing_sheet_the_fanin_words_and_starts_nothing_itself(tmp_path, qdir):
    g = _desk(tmp_path, qdir, {"w": 1280, "h": 900, "dpr": 1}, stub="{}", shot="room_desk_1280.png")
    _bar_fits(g)
    assert g["drops"] == ["/fanin broad"], g
    assert "Sign it there" in g["note"] and "nothing starts until you do" in g["note"], g


@needs_browser
def test_start_desk_fits_the_phone_header(tmp_path, qdir):
    g = _desk(tmp_path, qdir, {"w": 390, "h": 844, "dpr": 3, "mobile": True}, stub="{}", shot="room_desk_390.png")
    _bar_fits(g)
    assert g["topBox"]["h"] == 56 and g["drops"] == ["/fanin broad"], g


@needs_browser
def test_start_desk_opens_the_real_drop_sheet(tmp_path, qdir):
    g = _desk(tmp_path, qdir, {"w": 1280, "h": 900, "dpr": 1})
    assert g["sheet"] and g["frame"] == "/sign/drop", g                      # the cockpit's own signed-drop path, not a second one


@needs_browser
def test_a_refused_open_says_no_desk_was_started(tmp_path, qdir):
    g = _desk(tmp_path, qdir, {"w": 1280, "h": 900, "dpr": 1}, stub="null")
    assert g["drops"] == ["/fanin broad"] and g["note"] == "The signing sheet did not open, so no desk was started.", g

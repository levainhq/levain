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
    assert [h["href"] for h in g["allHrefs"]] == ["https://example.com/ok"], g                    # an http(s) link only; the relative path is plain text
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
    # the sheet also carries "Send words only", which wakes the desk with no limits: the line says both, never "only a signature starts it"
    assert "Sign it there" in g["note"] and "Send words only" in g["note"] and "nothing starts until you press one of them" in g["note"], g


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


# --- a seat's link is an http(s) or mailto link, nothing the cockpit itself would answer (L2 MED, ROOMC-FIX1) ---------------------------
# The renderer keeps a cockpit-relative link, and `/#write_token=X` on the cockpit's own origin is the page's token intake: a reply the
# seat wrote after reading a hostile web page could wipe the owner's stored write token, or open /sign/consent or /sign/drop preselected.
# So drawMarkdown unwraps, after the render, every anchor that is not http(s)/mailto, and every one that resolves to the cockpit's own origin.

_UNSAFE = [
    ("tok", "[tok](/#write_token=EVILTOKEN123)"), ("consent", "[consent](/sign/consent?choice=allow#choice=allow)"),
    ("drop", "[drop](/sign/drop)"), ("rel", "[rel](/relative)"), ("dots", "[dots](../up)"), ("frag", "[frag](#top)"),
    ("query", "[query](?x=1)"), ("bare", "[bare](path/only)"), ("proto", "[proto](//evil.example/x)"),
    ("bs1", r"[bs1](\\evil.example/x)"), ("bs2", r"[bs2](/\evil.example/x)"), ("bs3", r"[bs3](\/evil.example/x)"), ("bs4", r"[bs4](\evil.example/x)"),
    ("padded", "[padded](  /sign/drop)"),
    ("self", "[self](@ORIGIN@/#write_token=EVILTOKEN123)"), ("selfpath", "[selfpath](@ORIGIN@/sign/drop)"),
    # the same host on another port or scheme is the cockpit too (L3 r1, deepseek MED): an http link to an https cockpit follows its redirect
    ("selfport", "[selfport](http://@HOST@:9/sign/drop)"), ("selfscheme", "[selfscheme](https://@HOST@/sign/drop)"),
    ("js", "[js](javascript:window.pwned=1)"), ("data", "[data](data:text/html,x)"),
]
_SAFE = [("ok", "[ok](https://example.org/x)", "https://example.org/x"), ("up", "[up](HTTPS://EXAMPLE.ORG/up)", "https://example.org/up"),
         ("mail", "[mail](mailto:a@b.example)", "mailto:a@b.example"), ("local", "[local](http://localhost:9/x)", "http://localhost:9/x")]
_LINK_ROWS = [{"gseq": 9100 + i, "kind": "vendor_agent_text", "source": "vendor", "ref": "agent_text:%d" % (9100 + i), "launch_id": "L1", "status": None,
               "body": "see " + md, "at": "2026-11-12T10:01:%02d+00:00" % i} for i, md in enumerate([m for _n, m in _UNSAFE] + [m for _n, m, _h in _SAFE])]


def _wrap(rows):
    """Reads of the room get `rows` appended; @ORIGIN@ in a body becomes the page's own origin."""
    return ("(() => { window.__roomRows = JSON.parse(%s.split('@ORIGIN@').join(location.origin).split('@HOST@').join(location.hostname)); const real = window.fetch.bind(window);"
            " window.fetch = async (u, o) => { const r = await real(u, o); if (String(u).split('?')[0] !== '/cockpit/room.json') return r;"
            " const j = await r.json(); j.stream = (j.stream || []).concat(window.__roomRows);"
            " return new Response(JSON.stringify(j), {status: 200, headers: {'Content-Type': 'application/json'}}); }; return 1; })()" % json.dumps(json.dumps(rows)))


def _run(tmp_path, qdir, emulate, rows, look, marker, lines, pre=(), shot=None):
    rs, _launcher, src = _rig(tmp_path, qdir)
    enter = list(_ENTER[:3]) + [{"eval": _wrap(rows)}] + [{"eval": e} for e in pre] + list(_ENTER[3:])
    with _serve(src, write_token=TOKEN, room_serve=rs) as (base, _h):
        out = _drive(tmp_path, base + "/", [{"emulate": emulate}] + enter + [
            {"until": "document.querySelectorAll('.rv-room .rv-text').length >= %d" % lines},
            {"eval": "new Promise(r => setTimeout(r, 400)).then(() => 1)"},
            {"eval": look}] + ([{"eval": "document.querySelectorAll('.rv-room .rv-text')[0].scrollIntoView({block: 'start'}); 1"}] + _shot(shot) if shot else []))
    return json.loads(next(v for v in out["values"] if isinstance(v, str) and v.startswith("{") and marker in v))


_LINKS_LOOK = """(() => { const rows = [...document.querySelectorAll('.rv-room .rv-text')];
  return JSON.stringify({linkrows: rows.map(r => ({text: r.textContent, links: [...r.querySelectorAll('a')].map(a => ({href: a.getAttribute('href'), rel: a.getAttribute('rel'),
    target: a.getAttribute('target'), text: a.textContent, bidi: getComputedStyle(a).unicodeBidi}))})), origin: location.origin, pwned: typeof window.pwned}); })()"""


@needs_browser
def test_a_seats_link_is_http_or_mailto_never_something_the_cockpit_answers(tmp_path, qdir):
    g = _run(tmp_path, qdir, {"w": 390, "h": 844, "dpr": 3, "mobile": True}, _LINK_ROWS, _LINKS_LOOK, '"linkrows"', len(_LINK_ROWS), shot="room_links_390.png")
    rows = g["linkrows"]
    assert len(rows) == len(_LINK_ROWS) and g["pwned"] == "undefined", g
    for (name, _md), row in zip(_UNSAFE, rows):
        assert row["links"] == [], (name, row)                                       # no anchor at all: the link text stays, as text
        assert row["text"].startswith("see " + name) and "](" not in row["text"], (name, row)
    for (name, _md, href), row in zip(_SAFE, rows[len(_UNSAFE):]):
        assert len(row["links"]) == 1, (name, row)
        link = row["links"][0]
        assert link["href"].lower() == href and link["text"] == name, (name, row)
        assert link["rel"] == "noopener noreferrer nofollow" and link["target"] == "_blank", (name, row)
        assert link["bidi"] == "isolate", (name, row)                                  # a bidi control in the link text cannot reorder what follows it


# --- the other gaps the L1 review named ---------------------------------------------------------------------------------------------------

_THROW = "window.__thrown = 0; window.LevainMD.renderMarkdown = function () { window.__thrown++; throw new Error('boom'); }; 1"
_THROW_LOOK = """(() => { const t = document.querySelector('.rv-room .rv-text');
  return JSON.stringify({thrown: window.__thrown, cls: t.className, strong: [...t.querySelectorAll('strong')].map(s => s.textContent),
    ic: [...t.querySelectorAll('code.rv-ic')].map(c => c.textContent), pre: [...t.querySelectorAll('pre.rv-code code')].map(c => c.textContent),
    anchors: t.querySelectorAll('a').length, mdEls: t.querySelectorAll('[class^=md-], [class*=" md-"]').length, text: t.textContent, probe: 'throwlook'}); })()"""


@needs_browser
def test_a_renderer_that_throws_leaves_the_reply_drawn_the_plain_way(tmp_path, qdir):
    g = _run(tmp_path, qdir, {"w": 1280, "h": 900, "dpr": 1}, [_ROWS[1]], _THROW_LOOK, "throwlook", 1, pre=[_THROW])
    assert g["thrown"] >= 1, g                                                           # the stub really ran: the branch was taken
    assert "rv-md" not in g["cls"] and g["mdEls"] == 0 and g["anchors"] == 0, g
    assert g["strong"] == ["bold"] and g["ic"] == ["inline"] and g["pre"] == [CODE], g   # the small pass drew it
    assert "[link](https://example.com/docs)" in g["text"] and "## The plan" in g["text"], g


_WIDE = [{"gseq": 9201, "kind": "vendor_agent_text", "source": "vendor", "ref": "agent_text:9201", "launch_id": "L1", "status": None,
          "body": "Wide:\n\n```\n" + "x" * 300 + "\n```\n\nand [a long link](https://example.org/" + "a" * 200 + ") after it", "at": "2026-11-12T10:02:00+00:00"}]
_WIDE_LOOK = """(() => { const b = document.querySelector('.rv-body'), t = document.querySelector('.rv-room .rv-text'), p = t.querySelector('pre.md-pre'),
  a = t.querySelector('a'); const ar = a.getBoundingClientRect(), tr = t.getBoundingClientRect();
  return JSON.stringify({bodyScroll: b.scrollWidth, bodyClient: b.clientWidth, pageScroll: document.documentElement.scrollWidth, vw: innerWidth,
    preScroll: p.scrollWidth, preClient: p.clientWidth, preOverflow: getComputedStyle(p).overflowX, linkRight: ar.right, textRight: tr.right, probe: 'widelook'}); })()"""


@needs_browser
def test_wide_code_scrolls_inside_its_block_and_never_widens_the_phone_page(tmp_path, qdir):
    g = _run(tmp_path, qdir, {"w": 390, "h": 844, "dpr": 3, "mobile": True}, _WIDE, _WIDE_LOOK, "widelook", 1, shot="room_wide_390.png")
    assert g["preScroll"] > g["preClient"] + 100 and g["preOverflow"] == "auto", g         # the 300-character line scrolls in its block
    assert g["bodyScroll"] <= g["bodyClient"] + 1 and g["pageScroll"] <= g["vw"] + 1, g    # and the page is no wider than the phone
    assert g["linkRight"] <= g["textRight"] + 1, g                                          # a 200-character link wraps inside the column


_GUARD_LOOK = """(() => { const d2 = document.implementation.createHTMLDocument('other'), host = d2.createElement('div'), V = window.LevainRoomView;
  const m = V.roomModel({stream: [{gseq: 1, kind: 'vendor_agent_text', source: 'vendor', ref: 'a:1', body: '## h **b** [l](https://example.org)', at: '2026-11-12T10:00:00+00:00'}], proposals: []});
  V.renderRoom(d2, host, m, {}, {open: {}}, {}); const t = host.querySelector('.rv-text');
  return JSON.stringify({cls: t.className, ownerIsOther: t.ownerDocument === d2, anchors: t.querySelectorAll('a').length, strong: t.querySelectorAll('strong').length, mdEls: t.querySelectorAll('[class^=md-]').length, probe: 'guardlook'}); })()"""


@needs_browser
def test_a_document_that_is_not_the_renderers_own_gets_the_plain_pass(tmp_path, qdir):
    # renderRoom is exported and takes any document; the markdown renderer builds in the page's own, so for another document the
    # reply is drawn by the small pass (so the guard in drawMarkdown is reachable, and kept)
    g = _run(tmp_path, qdir, {"w": 1280, "h": 900, "dpr": 1}, [], _GUARD_LOOK, "guardlook", 0)
    assert g["ownerIsOther"] and "rv-md" not in g["cls"] and g["mdEls"] == 0 and g["anchors"] == 0 and g["strong"] == 1, g


# --- the link filter fails closed, and a reply's total size is bounded (L3 r1 complement, ROOMC-FIX2) ------------------------------------

_ORIGIN_STUB = ("(() => { const real = window.LevainMD.renderMarkdown; window.__stubbed = 0;"
                " window.LevainMD.renderMarkdown = function (t) { const f = real.call(this, t); window.__stubbed++;"
                " f.querySelectorAll('a').forEach(a => { const w = a.textContent; Object.defineProperty(a, 'origin', {configurable: true, get: () => {"
                " if (%s) throw new Error('no origin'); return w === 'undef' ? undefined : w === 'num' ? 5 : w === 'str' ? 'https://example.org' : a.href; }}); });"
                " return f; }; return 1; })()")
_ORIGIN_ROW = [{"gseq": 9301, "kind": "vendor_agent_text", "source": "vendor", "ref": "agent_text:9301", "launch_id": "L1", "status": None,
                "body": "see [undef](https://example.org/u) [num](https://example.org/n) [str](https://example.org/s) and **bold**",
                "at": "2026-11-12T10:03:00+00:00"}]
_ORIGIN_LOOK = """(() => { const t = document.querySelector('.rv-room .rv-text');
  return JSON.stringify({stubbed: window.__stubbed, cls: t.className, links: [...t.querySelectorAll('a')].map(a => a.getAttribute('href')),
    strong: [...t.querySelectorAll('strong')].map(s => s.textContent), mdEls: t.querySelectorAll('[class^=md-], [class*=" md-"]').length, rows: document.querySelectorAll('.rv-line').length,
    text: t.textContent, probe: 'originlook'}); })()"""


@needs_browser
def test_a_link_whose_origin_cannot_be_read_as_a_string_is_unwrapped(tmp_path, qdir):
    # fail CLOSED: an anchor survives only when its origin is a string that differs from the cockpit's; undefined or a number unwraps
    g = _run(tmp_path, qdir, {"w": 1280, "h": 900, "dpr": 1}, _ORIGIN_ROW, _ORIGIN_LOOK, "originlook", 1, pre=[_ORIGIN_STUB % "false"])
    assert g["stubbed"] >= 1 and "rv-md" in g["cls"], g                                  # the stub ran and the renderer drew it
    assert g["links"] == ["https://example.org/s"], g                                      # a string origin that differs: kept; undefined and 5: words only
    assert "undef" in g["text"] and "num" in g["text"] and g["strong"] == ["bold"], g


@needs_browser
def test_a_filter_that_throws_leaves_the_reply_drawn_the_plain_way(tmp_path, qdir):
    # the unwrap pass runs inside the renderer's try: an exception in it falls back to the small pass, with no half-drawn tree
    g = _run(tmp_path, qdir, {"w": 1280, "h": 900, "dpr": 1}, _ORIGIN_ROW, _ORIGIN_LOOK, "originlook", 1, pre=[_ORIGIN_STUB % "true"])
    assert g["stubbed"] >= 1 and g["rows"] >= 1, g                                       # the branch was taken and the room still rendered
    assert "rv-md" not in g["cls"] and g["mdEls"] == 0 and g["links"] == [], g
    assert g["strong"] == ["bold"] and "[str](https://example.org/s)" in g["text"], g    # the small pass drew it: markers stay literal


_BIG = [{"gseq": 9400 + i, "kind": "vendor_agent_text", "source": "vendor", "ref": "agent_text:%d" % (9400 + i), "launch_id": "L1", "status": None,
         "body": body, "at": "2026-11-12T10:04:%02d+00:00" % i} for i, body in enumerate([
             "## ok\n" + "- **b** item\n" * 100,                  # under both bounds: drawn as markdown
             "x\n" * 50000,                                       # 50,000 short lines, 100,000 characters: past the line cap only
             ("y" * 10000 + "\n") * 25,                           # 250,000 characters in 26 lines: past the total-size cap only
         ])]
_BIG_LOOK = """(() => { const rows = [...document.querySelectorAll('.rv-room .rv-text')];
  return JSON.stringify({rows: rows.map(r => ({cls: r.className, mdEls: r.querySelectorAll('[class^=md-], [class*=" md-"]').length, len: r.textContent.length})), probe: 'biglook'}); })()"""


@needs_browser
def test_a_reply_past_the_total_size_or_line_bound_is_drawn_plain(tmp_path, qdir):
    g = _run(tmp_path, qdir, {"w": 1280, "h": 900, "dpr": 1}, _BIG, _BIG_LOOK, "biglook", 3)
    ok, many, wide = g["rows"]
    assert "rv-md" in ok["cls"] and ok["mdEls"] > 100, g                                   # the control is drawn by the renderer
    assert "rv-md" not in many["cls"] and many["mdEls"] == 0 and many["len"] >= 100000, g  # 50,000 lines: the plain draw, text intact
    assert "rv-md" not in wide["cls"] and wide["mdEls"] == 0 and wide["len"] >= 250000, g  # 250,000 characters: the plain draw, text intact

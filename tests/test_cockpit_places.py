"""The cockpit page's places bar (levain#141): Home, Desk, Room, Memory, Settings, and nothing else in the bar.

What these hold: the page's one bar holds places only, the Needs-you count rides Home on that bar at every width, the page runs its
scripts in order with dashboard_places.js last, and a real browser lands on a place, moves between places, takes the 1-4 keys, and
sends a stored retired mode (Morning, Build) to Home. What they cannot hold (judged by looking at the page on his phone and laptop):
whether the arrangement of Home feels right on his real data."""

from __future__ import annotations

import hashlib
import json
import re
from html.parser import HTMLParser
from pathlib import Path

from tests._ke_dom import NODE, run_node
from tests.test_cockpit_flip_parity import _drive, needs_browser
from tests.test_cockpit_k2a import _install, _serve
from tests.test_cockpit_narrow_layout_rig import drive_touch

WEB = Path(__file__).resolve().parent.parent / "levain" / "templates" / "web"
PLACES = {"home", "desk", "room", "memory", "settings"}


class _Tags(HTMLParser):
    """Every start tag, in order, with its attributes and the stack of open tags around it."""

    VOID = {"meta", "link", "br", "img", "input", "hr", "source", "area", "base", "col", "embed", "param", "track", "wbr"}

    def __init__(self) -> None:
        super().__init__()
        self.tags: list[tuple[str, dict, tuple]] = []
        self.stack: list[tuple[str, dict]] = []

    def handle_starttag(self, tag, attrs):
        a = {k: (v or "") for k, v in attrs}
        self.tags.append((tag, a, tuple(self.stack)))
        if tag not in self.VOID:
            self.stack.append((tag, a))

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i][0] == tag:
                del self.stack[i:]
                break


def _parse() -> _Tags:
    p = _Tags()
    p.feed((WEB / "dashboard.html").read_text())
    return p


def _in_bar(stack: tuple) -> bool:
    return any(s[1].get("id") == "ck-phonetabs" for s in stack)


def test_the_page_runs_the_places_script_after_the_board_painters() -> None:
    """The places script reads the page's own state, so it loads after every script that draws or wires it; the page links one stylesheet."""
    p = _parse()
    scripts = [a["src"] for t, a, _ in p.tags if t == "script" and a.get("src")]
    assert scripts[-2:] == ["/board_view.js", "/dashboard_places.js"]
    assert scripts.count("/dashboard_places.js") == 1
    sheets = [a["href"] for t, a, _ in p.tags if t == "link" and a.get("rel") == "stylesheet"]
    assert sheets == ["/dashboard.css"]


def test_the_tagline_is_static_text_in_the_visible_element() -> None:
    """The line under the wordmark sits in its own element (the page hides `.model`, which a manifest's brand also overwrites)."""
    p = _parse()
    html = (WEB / "dashboard.html").read_text()
    assert [a for t, a, _ in p.tags if "ia-tagline" in a.get("class", "").split()] == [{"class": "ia-tagline", "id": "ia-tagline"}]
    assert '<div class="ia-tagline" id="ia-tagline">what needs you, what is moving, what it remembers</div>' in html


def test_the_bar_holds_places_only() -> None:
    """One bar, places only; the Modes bar is gone; Drop, Grants and Notifications are buttons outside the bar."""
    p = _parse()
    in_bar = [a for t, a, stack in p.tags if t == "button" and _in_bar(stack)]
    assert {a.get("data-ia-place-btn") for a in in_bar} == PLACES
    assert not [a for a in in_bar if "data-phone-jump" in a or "data-mode-tab" in a]
    assert not [a for t, a, _ in p.tags if t == "nav" and "seg" in a.get("class", "").split()], "the Modes bar is back"
    visible_mode_tabs = [a for t, a, stack in p.tags if "data-mode-tab" in a and not any("hidden" in s[1] for s in stack)]
    assert not visible_mode_tabs, "a mode tab is drawn outside the hidden drivers"
    actions = {a.get("data-phone-jump") for t, a, stack in p.tags
               if t == "button" and any("ia-actions" in s[1].get("class", "") for s in stack)}
    assert actions == {"drop", "grants", "notifications"}


def test_needs_count_rides_home_at_every_width() -> None:
    """The Needs-you count is on the Home place in the bar, and the bar is drawn at every width (no media query hides it)."""
    p = _parse()
    home = [a for t, a, stack in p.tags if a.get("data-ia-place-btn") == "home" and _in_bar(stack)]
    assert len(home) == 1
    counts = [stack for t, a, stack in p.tags if "data-nc-needs" in a]
    assert len(counts) == 1 and any(s[1].get("data-ia-place-btn") == "home" for s in counts[0])
    css = (WEB / "dashboard.css").read_text()
    bare = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    assert "body.ia .ia-places { display: flex !important; }" in re.sub(r"@media[^{]*\{(?:[^{}]*\{[^}]*\})*[^{}]*\}", "", bare)
    assert not re.search(r"\.ia-places[^{]*\{[^}]*display:\s*none", bare)
    # the count's own pill hides only when the count itself is hidden (nothing waits), never by width
    hides = [m.group(0) for m in re.finditer(r"[^{}]*(?:\.ia-needs|data-nc-needs)[^{]*\{[^}]*display:\s*none[^}]*\}", bare)]
    assert all(":has([data-nc-needs][hidden])" in h for h in hides), hides


def test_the_places_script_is_served_as_app_shell(tmp_path: Path) -> None:
    """The page names the script by the digest of the bytes served (1105-17), as it does every shell script."""
    from tests.test_cockpit_board import TOKEN, _raw
    _root, src, _ids = _install(tmp_path)
    with _serve(src, write_token=TOKEN) as (base, _h):
        status, hdrs, data = _raw(base, "/dashboard_places.js", token=None)
        _s, _h2, page = _raw(base, "/", token=None)
    assert status == 200 and hdrs["content-type"].startswith("text/javascript")
    assert b'<script src="/dashboard_places.js?v=%s"></script>' % hashlib.sha256(data).hexdigest()[:12].encode() in page
    assert b"innerHTML" not in data


STATE = """(() => ({
  place: document.body.dataset.iaPlace,
  bar: [...document.querySelectorAll('.ia-places [data-ia-place-btn]')].map(b => b.dataset.iaPlaceBtn),
  current: [...document.querySelectorAll('[aria-current="page"]')].map(b => b.dataset.iaPlaceBtn || b.id || b.tagName),
  mode: document.body.dataset.mode || '',
  tagline: Math.round(document.querySelector('.ia-tagline').getBoundingClientRect().height),
  modeTabsShown: [...document.querySelectorAll('[data-mode-tab]')].filter(b => b.getClientRects().length).length,
}))()"""
PRESS = "document.querySelector('.ia-places [data-ia-place-btn=\"%s\"]').click(); 1"
SETTLE = {"eval": "new Promise(r => setTimeout(r, 400)).then(() => 1)"}
READY = {"until": "document.body.dataset.iaPlace && document.querySelector('.ia-places [data-ia-place-btn]') && 1"}


@needs_browser
def test_a_browser_lands_on_home_moves_between_places_and_takes_the_keys(tmp_path: Path) -> None:
    _root, src, _ids = _install(tmp_path)
    with _serve(src, surface_tokens={"browser": "T"}, install_class="reported") as (url, _httpd):
        out = _drive(tmp_path, f"{url}/#write_token=T", [
            {"emulate": {"w": 1440, "h": 900, "dpr": 1}},
            READY, SETTLE,
            {"eval": STATE},                                    # 0: the landing
            {"eval": PRESS % "memory"}, SETTLE,
            {"eval": STATE},                                    # 1: Memory by the bar
            {"eval": PRESS % "home"}, SETTLE,
            {"eval": STATE},                                    # 2: Home again
            {"eval": "document.dispatchEvent(new KeyboardEvent('keydown', {key: '4', bubbles: true, cancelable: true})); 1"}, SETTLE,
            {"eval": STATE},                                    # 3: key 4 is Memory
            {"eval": "document.dispatchEvent(new KeyboardEvent('keydown', {key: '1', bubbles: true, cancelable: true})); 1"}, SETTLE,
            {"eval": STATE},                                    # 4: key 1 is Home
            {"emulate": {"w": 390, "h": 800, "dpr": 1, "mobile": True}}, SETTLE,
            {"eval": STATE},                                    # 5: the phone width
        ])
    states = [v for v in out["values"] if isinstance(v, dict) and "place" in v]
    assert len(states) == 6, out["values"]
    landing, memory, home, key4, key1, phone = states
    assert landing["bar"] == ["home", "desk", "room", "memory", "settings"]
    assert landing["place"] == "home" and landing["current"] == ["home"]
    assert landing["modeTabsShown"] == 0 and landing["tagline"] > 0
    assert memory["place"] == "memory" and memory["current"] == ["memory"] and memory["mode"] == "memory"
    assert home["place"] == "home" and home["current"] == ["home"]
    assert key4["place"] == "memory" and key4["mode"] == "memory"
    assert key1["place"] == "home"
    assert phone["tagline"] > 0 and phone["bar"] == ["home", "desk", "room", "memory", "settings"], phone


MODE_KEY = "levain_cockpit_mode"   # cockpit_view.js MODE_KEY: the page keeps the chosen mode in sessionStorage under this key
# every value the body's data-mode takes from here on, in order: each record's old value, then the value at the callback. A mode the page
# writes and another script replaces in the same turn is still seen, as the old value of the next record.
WATCH = ("window.__modes = []; new MutationObserver((rs) => { for (const r of rs) window.__modes.push(r.oldValue); window.__modes.push(document.body.dataset.mode); })"
         ".observe(document.body, {attributes: true, attributeFilter: ['data-mode'], attributeOldValue: true}); 1")
KEY = "document.dispatchEvent(new KeyboardEvent('keydown', {key: '%s', bubbles: true, cancelable: true})); 1"


@needs_browser
def test_a_stored_retired_mode_lands_on_home(tmp_path: Path) -> None:
    """Morning and Build are retired; a mode kept by an older page (read from sessionStorage under the page's own key) opens Home."""
    _root, src, _ids = _install(tmp_path)
    with _serve(src, surface_tokens={"browser": "T"}, install_class="reported") as (url, _httpd):
        out = _drive(tmp_path, f"{url}/#write_token=T", [
            {"emulate": {"w": 1440, "h": 900, "dpr": 1}},
            READY,
            {"eval": f"sessionStorage.setItem('{MODE_KEY}', 'morning'); history.replaceState(null, '', location.pathname); window.__old = 1; location.reload(); 1"},
            {"until": "!window.__old && document.body.dataset.iaPlace && 1"}, SETTLE,
            {"eval": STATE},
            {"eval": f"sessionStorage.getItem('{MODE_KEY}')"},
            {"eval": WATCH},
            {"eval": "location.hash = '#build'; 1"}, SETTLE,
            {"eval": STATE},
            {"eval": "JSON.stringify([window.__modes, location.hash, sessionStorage.getItem('%s')])" % MODE_KEY},
        ])
    states = [v for v in out["values"] if isinstance(v, dict) and "place" in v]
    assert [s["place"] for s in states] == ["home", "home"], out["values"]
    assert all(s["mode"] == "board" for s in states), states
    assert "board" in out["values"], out["values"]                                  # the kept value was read, then replaced by Home's
    modes, frag, kept = json.loads(out["values"][-1])
    assert "morning" not in modes and "build" not in modes, modes                  # the retired mode was never written, not even for a moment
    assert frag == "#board" and kept == "board", (frag, kept)


@needs_browser
def test_the_keys_and_the_jump_never_pass_through_a_retired_mode(tmp_path: Path) -> None:
    """Keys 1 and 3 under the presence sheet, and the address, used to open Morning and Build for a moment: the page's own mode setter is the gate."""
    _root, src, _ids = _install(tmp_path)
    with _serve(src, surface_tokens={"browser": "T"}, install_class="reported") as (url, _httpd):
        out = _drive(tmp_path, f"{url}/#write_token=T", [
            {"emulate": {"w": 1440, "h": 900, "dpr": 1}},
            READY, SETTLE,
            {"eval": WATCH},
            {"eval": "(() => { const s = document.getElementById('pr-sheet'); s.hidden = false; s.style.display = 'block'; return s.getClientRects().length; })()"},
            {"eval": KEY % "3"}, {"eval": KEY % "1"}, {"eval": KEY % "4"}, SETTLE,
            {"eval": "(() => { document.getElementById('pr-sheet').hidden = true; return 1; })()"},
            {"eval": "window.LevainCockpitKinds.applyMode('build', true); window.LevainCockpitKinds.applyMode('morning'); 1"}, SETTLE,
            {"eval": STATE},
            {"eval": "JSON.stringify(window.__modes)"},
        ])
    vals = out["values"]
    sheet_shown = [v for v in vals if isinstance(v, int) and not isinstance(v, bool)]
    assert sheet_shown and sheet_shown[0] > 0, vals                                 # the sheet was really drawn
    modes = json.loads(vals[-1])
    assert "morning" not in modes and "build" not in modes, modes
    state = [v for v in vals if isinstance(v, dict) and "place" in v][-1]
    assert state["place"] == "home" and state["mode"] == "board", state             # nothing the sheet was open over moved the page to Memory either


def test_the_page_resolves_a_retired_mode_before_it_writes_any_attribute(tmp_path: Path) -> None:
    out = run_node(r"""
const modes = [], written = [];
document.body.dataset = new Proxy({}, { set(t, k, v) { if (k === "mode") modes.push(v); t[k] = v; return true; } });
globalThis.location = { hash: "#build", search: "", pathname: "/" };
globalThis.history = { replaceState: (a, b, url) => written.push(url) };
const kept = {};
globalThis.sessionStorage = { getItem: (k) => kept[k] || null, setItem: (k, v) => { kept[k] = v; }, removeItem: (k) => { delete kept[k]; } };
for (const m of ["morning", "build", "board", "memory", "bogus"]) K.applyMode(m);
out.modes = modes;
out.stored = kept.levain_cockpit_mode;
out.written = written;
""", tmp_path)
    assert out["modes"] == ["board", "board", "board", "memory", "board"], out
    assert out["written"] == ["/#board", "/#board"], out                            # a retired name is rewritten out of the address; a plain first paint writes nothing


def test_the_jump_box_reveals_episodes_in_memory_and_lists_no_panel_that_no_place_draws(tmp_path: Path) -> None:
    out = run_node(r"""
const modes = [];
document.body.dataset = new Proxy({ mode: "board" }, { set(t, k, v) { if (k === "mode") modes.push(v); t[k] = v; return true; } });
globalThis.location = { hash: "", search: "", pathname: "/" };
const written = [];
globalThis.history = { replaceState: (a, b, url) => written.push(url) };
const panel = (id, title, modes) => { const p = new N("section"); p.dataset.ckPanel = id; if (modes) p.dataset.modes = modes; p.title = title; return p; };
const ids = [["episodes", "Recent episodes", "board"], ["tray", "List", "morning board"], ["notifications", "Notifications", "board"],
  ["topology", "Topology", "build"], ["agents", "Agents", "build"], ["crystals", "Crystals", "memory"]];
const panels = ids.map(([id, t, m]) => panel(id, t, m));
const heads = panels.map((p) => Object.assign(new N("h2"), { text: p.title, closest: () => p }));
globalThis.document.querySelectorAll = (sel) => (sel === "#board .panel > .phead h2" ? heads : []);
out.listed = K.jumpItems("").map((i) => i.label);
const by = (id) => panels.find((p) => p.dataset.ckPanel === id);
out.fromHome = [K.revealTab(by("episodes")), document.body.dataset.mode];
out.fromMemory = [K.revealTab(by("episodes")), document.body.dataset.mode];       // already there: no change
document.body.dataset.mode = "board";
written.length = 0;
out.agents = [K.revealTab(by("agents")), document.body.dataset.mode];             // its tab is retired: Home already draws it, so nothing moves
out.agentsWrote = written.slice();
out.crystals = [K.revealTab(by("crystals")), document.body.dataset.mode];
""", tmp_path)
    assert "Recent episodes" in out["listed"] and "Agents" in out["listed"], out
    assert "Notifications" not in out["listed"] and "Topology" not in out["listed"], out
    assert out["fromHome"] == [True, "memory"] and out["fromMemory"] == [False, "memory"], out
    assert out["agents"] == [False, "board"] and out["agentsWrote"] == [], out         # choosing Agents on Home writes no address and remounts nothing
    assert out["crystals"] == [True, "memory"], out


@needs_browser
def test_choosing_recent_episodes_in_the_jump_box_ends_on_the_drawn_panel(tmp_path: Path) -> None:
    """From Home and from Memory the choice lands on Memory with the panel drawn (it ended on Home, with the panel hidden, from Memory)."""
    SHOWN = "(() => { const p = document.querySelector('.panel[data-ck-panel=\"episodes\"]'); return JSON.stringify({place: document.body.dataset.iaPlace, shown: !!p && p.getClientRects().length > 0}); })()"
    CHOOSE = ("(() => { document.getElementById('jumpbtn').click(); const i = document.querySelector('.palette-input'); i.value = 'recent episodes';"
              " i.dispatchEvent(new Event('input')); const items = [...document.querySelectorAll('.palette-list li .pl')].map(n => n.textContent);"
              " i.dispatchEvent(new KeyboardEvent('keydown', {key: 'Enter', bubbles: true})); return JSON.stringify(items); })()")
    LISTED = ("(() => { document.getElementById('jumpbtn').click(); const i = document.querySelector('.palette-input');"
              " const t = [...document.querySelectorAll('.palette-list li .pl')].map(n => n.textContent); i.dispatchEvent(new KeyboardEvent('keydown', {key: 'Escape', bubbles: true})); return JSON.stringify(t); })()")
    _root, src, _ids = _install(tmp_path)
    with _serve(src, surface_tokens={"browser": "T"}, install_class="reported") as (url, _httpd):
        out = _drive(tmp_path, f"{url}/#write_token=T", [
            {"emulate": {"w": 1440, "h": 900, "dpr": 1}},
            READY, SETTLE,
            {"eval": LISTED},
            {"eval": CHOOSE}, SETTLE, {"eval": SHOWN},                                # 2: chosen from Home
            {"eval": PRESS % "home"}, SETTLE,
            {"eval": PRESS % "memory"}, SETTLE,
            {"eval": CHOOSE}, SETTLE, {"eval": SHOWN},                                # 3: chosen from Memory
        ])
    strs = [json.loads(v) for v in out["values"] if isinstance(v, str) and v.startswith(("{", "["))]
    listed, chosen1, shown1, chosen2, shown2 = strs
    assert any("episodes" in t.lower() for t in chosen1), chosen1                  # the fixture offers the panel
    assert not [t for t in listed if t in ("Notifications", "Topology")], listed
    assert shown1 == {"place": "memory", "shown": True}, shown1
    assert shown2 == {"place": "memory", "shown": True}, shown2


@needs_browser
def test_choosing_a_panel_in_the_jump_box_on_the_desk_leaves_the_desk(tmp_path: Path) -> None:
    """Memory under the Desk already is the mode Recent episodes needs, so no attribute changes; the page's own Memory tab is what leaves the Desk."""
    SHOWN = "(() => { const p = document.querySelector('.panel[data-ck-panel=\"episodes\"]'); return JSON.stringify({place: document.body.dataset.iaPlace, shown: !!p && p.getClientRects().length > 0}); })()"
    CHOOSE = ("(() => { document.getElementById('jumpbtn').click(); const i = document.querySelector('.palette-input'); i.value = 'recent episodes';"
              " i.dispatchEvent(new Event('input')); i.dispatchEvent(new KeyboardEvent('keydown', {key: 'Enter', bubbles: true})); return 1; })()")
    _root, src, _ids = _install(tmp_path)
    with _serve(src, surface_tokens={"browser": "T"}, install_class="reported") as (url, _httpd):
        out = _drive(tmp_path, f"{url}/#write_token=T", [
            {"emulate": {"w": 1440, "h": 900, "dpr": 1}},
            READY, SETTLE,
            {"eval": PRESS % "memory"}, SETTLE,
            {"eval": PRESS % "desk"}, SETTLE,
            {"eval": SHOWN},                                                            # 0: on the Desk, the panel hidden
            {"eval": CHOOSE}, SETTLE, SETTLE,
            {"eval": SHOWN},                                                            # 1: chosen from the Desk
        ])
    on_desk, chosen = [json.loads(v) for v in out["values"] if isinstance(v, str) and v.startswith('{"place"')]
    assert on_desk == {"place": "desk", "shown": False}, on_desk
    assert chosen == {"place": "memory", "shown": True}, chosen


@needs_browser
def test_drop_on_memory_goes_home_and_reaches_the_box(tmp_path: Path) -> None:
    """The masthead Drop button was a no-op on Memory (the box is not drawn there): it goes Home, then scrolls to and focuses the box."""
    ST = ("(() => { const d = document.getElementById('ck-drop'); const a = document.activeElement;"
          " return JSON.stringify({place: document.body.dataset.iaPlace, drawn: d.getClientRects().length > 0, top: Math.round(d.getBoundingClientRect().top), mast: Math.round(document.querySelector('.masthead').getBoundingClientRect().bottom), y: Math.round(scrollY), inBox: !!a && d.contains(a)}); })()")
    _root, src, _ids = _install(tmp_path)
    with _serve(src, surface_tokens={"browser": "T"}, install_class="reported") as (url, _httpd):
        out = _drive(tmp_path, f"{url}/#write_token=T", [
            {"emulate": {"w": 1440, "h": 900, "dpr": 1}},
            READY, {"until": "document.getElementById('ck-drop') && !document.getElementById('ck-drop').hidden && 1", "ms": 30000}, SETTLE,
            {"eval": PRESS % "memory"}, SETTLE,
            {"eval": ST},
            {"eval": "document.querySelector('.ia-act[data-phone-jump=\"drop\"]').click(); 1"}, SETTLE, SETTLE,
            {"eval": ST},
        ])
    on_memory, after = [json.loads(v) for v in out["values"] if isinstance(v, str) and v.startswith("{")]
    assert on_memory["place"] == "memory" and not on_memory["drawn"] and on_memory["y"] == 0, on_memory
    assert after["place"] == "home" and after["drawn"], after
    assert after["y"] > 0 and after["mast"] - 1 <= after["top"] <= after["mast"] + 40, after   # the page's own jump ran: the box is scrolled to just under the sticky masthead (levain#141 polish)


@needs_browser
def test_grants_and_notifications_keep_the_desk_they_were_pressed_on(tmp_path: Path) -> None:
    """Both buttons only open a dialog: the Desk and the Room they were pressed on are never left (no mount is torn down and re-entered)."""
    ST = "JSON.stringify({place: document.body.dataset.iaPlace, sheet: !!document.getElementById('levain-modal'), left: window.__left})"
    # whether either view's class was ever off the body after this point
    WATCH_VIEW = ("window.__left = 0; new MutationObserver(() => { if (!document.body.classList.contains('dv-on') && !document.body.classList.contains('rv-on')) window.__left++; })"
                  ".observe(document.body, {attributes: true, attributeFilter: ['class']}); 1")
    CLOSE = "(() => { const m = document.getElementById('levain-modal'); if (m) m.remove(); document.body.classList.remove('modal-open'); return 1; })()"
    CLICK = "document.querySelector('.ia-act[data-phone-jump=\"%s\"]').click(); 1"
    _root, src, _ids = _install(tmp_path)
    with _serve(src, surface_tokens={"browser": "T"}, install_class="reported") as (url, _httpd):
        out = _drive(tmp_path, f"{url}/#write_token=T", [
            {"emulate": {"w": 1440, "h": 900, "dpr": 1}},
            READY, SETTLE,
            {"eval": PRESS % "desk"}, SETTLE,
            {"eval": WATCH_VIEW},
            {"eval": CLICK % "notifications"}, SETTLE, SETTLE,
            {"eval": ST},                                                               # 0: Desk, notifications
            {"eval": CLOSE}, {"eval": PRESS % "room"}, SETTLE,
            {"eval": CLICK % "grants"}, SETTLE, SETTLE,
            {"eval": ST},                                                               # 1: Room, grants
        ])
    desk, room = [json.loads(v) for v in out["values"] if isinstance(v, str) and v.startswith("{")]
    assert (desk["place"], desk["sheet"]) == ("desk", True) and desk["left"] == 0, desk
    assert room["place"] == "room" and room["sheet"] is True and room["left"] == 0, room


@needs_browser
def test_the_places_keys_under_an_open_dialog_leave_the_desk_alone(tmp_path: Path) -> None:
    """With the presence sheet open on the Desk, 1 to 4 pressed on a button inside it belong to the sheet: the boot handler no longer leaves the Desk first."""
    SHEET = ("(() => { const s = document.getElementById('pr-sheet'); s.hidden = false; s.style.display = 'block';"
             " let b = s.querySelector('button'); if (!b) { b = document.createElement('button'); b.textContent = 'x'; s.appendChild(b); } b.focus(); return s.getClientRects().length; })()")
    PRESSED = ("(() => { const b = document.getElementById('pr-sheet').querySelector('button'); for (const k of ['1', '2', '3', '4']) b.dispatchEvent(new KeyboardEvent('keydown', {key: k, bubbles: true, cancelable: true}));"
               " return JSON.stringify({place: document.body.dataset.iaPlace, desk: document.body.classList.contains('dv-on')}); })()")
    _root, src, _ids = _install(tmp_path)
    with _serve(src, surface_tokens={"browser": "T"}, install_class="reported") as (url, _httpd):
        out = _drive(tmp_path, f"{url}/#write_token=T", [
            {"emulate": {"w": 1440, "h": 900, "dpr": 1}},
            READY, SETTLE,
            {"eval": PRESS % "desk"}, SETTLE,
            {"eval": SHEET},
            {"eval": PRESSED}, SETTLE,
            {"eval": STATE},
        ])
    shown = [v for v in out["values"] if isinstance(v, int) and not isinstance(v, bool)]
    assert shown and shown[0] > 0, out["values"]                                       # the sheet was really drawn
    pressed = json.loads(next(v for v in out["values"] if isinstance(v, str) and v.startswith('{"place"')))
    assert pressed == {"place": "desk", "desk": True}, pressed


@needs_browser
def test_drop_on_the_desk_over_memory_goes_home_and_reaches_the_box(tmp_path: Path) -> None:
    """Memory, then the Desk (the page still holds Memory underneath), then Drop: the box is not drawn under Memory, so Drop goes Home."""
    ST = ("(() => { const d = document.getElementById('ck-drop'); return JSON.stringify({place: document.body.dataset.iaPlace, mode: document.body.dataset.mode,"
          " drawn: d.getClientRects().length > 0, top: Math.round(d.getBoundingClientRect().top), mast: Math.round(document.querySelector('.masthead').getBoundingClientRect().bottom)}); })()")
    _root, src, _ids = _install(tmp_path)
    with _serve(src, surface_tokens={"browser": "T"}, install_class="reported") as (url, _httpd):
        out = _drive(tmp_path, f"{url}/#write_token=T", [
            {"emulate": {"w": 1440, "h": 900, "dpr": 1}},
            READY, {"until": "document.getElementById('ck-drop') && !document.getElementById('ck-drop').hidden && 1", "ms": 30000}, SETTLE,
            {"eval": PRESS % "memory"}, SETTLE,
            {"eval": PRESS % "desk"}, SETTLE,
            {"eval": ST},
            {"eval": "document.querySelector('.ia-act[data-phone-jump=\"drop\"]').click(); 1"}, SETTLE, SETTLE,
            {"eval": ST},
        ])
    on_desk, after = [json.loads(v) for v in out["values"] if isinstance(v, str) and v.startswith('{"place"')]
    assert on_desk["place"] == "desk" and on_desk["mode"] == "memory" and not on_desk["drawn"], on_desk
    assert after["place"] == "home" and after["mode"] == "board" and after["drawn"] and after["mast"] - 1 <= after["top"] <= after["mast"] + 40, after   # just under the sticky masthead


@needs_browser
def test_the_notifications_button_name_carries_the_unread_count(tmp_path: Path) -> None:
    """Its aria-label replaces its content for assistive tech, so the pill's count is carried into the label (the page writes the pill)."""
    NAME = "document.querySelector('.ia-act[data-phone-jump=\"notifications\"]').getAttribute('aria-label')"
    SET = ("(() => { const p = document.querySelector('[data-nc-unread]'); p.hidden = %s; p.textContent = '%s'; return new Promise(r => setTimeout(r, 100)).then(() => 1); })()")
    _root, src, _ids = _install(tmp_path)
    with _serve(src, surface_tokens={"browser": "T"}, install_class="reported") as (url, _httpd):
        out = _drive(tmp_path, f"{url}/#write_token=T", [
            {"emulate": {"w": 1440, "h": 900, "dpr": 1}},
            READY, SETTLE,
            {"eval": NAME},
            {"eval": SET % ("false", "3")}, {"eval": NAME},
            {"eval": SET % ("false", "99+")}, {"eval": NAME},
            {"eval": SET % ("true", "0")}, {"eval": NAME},
            {"eval": SET % ("false", "0")}, {"eval": NAME},
        ])
    names = [v for v in out["values"] if isinstance(v, str) and v.startswith("Notifications")]
    assert names == ["Notifications", "Notifications, 3 unread", "Notifications, 99+ unread", "Notifications", "Notifications"], out["values"]


def test_the_places_buttons_name_their_key() -> None:
    p = _parse()
    got = {a["data-ia-place-btn"]: (a.get("title", ""), a.get("aria-keyshortcuts")) for t, a, stack in p.tags if "data-ia-place-btn" in a and _in_bar(stack)}
    assert {k: v[1] for k, v in got.items()} == {"home": "1", "desk": "2", "room": "3", "memory": "4", "settings": "5"}, got
    assert all(v[0].endswith("(key %s)" % v[1]) for v in got.values()), got
    keys = re.search(r"var KEYS = \{([^}]*)\}", (WEB / "dashboard_places.js").read_text()).group(1)
    assert {m[1]: m[0] for m in re.findall(r"(\d): \"(\w+)\"", keys)} == {"home": "1", "desk": "2", "room": "3", "memory": "4", "settings": "5"}   # the markup says what the script binds


@needs_browser
def test_a_coarse_pointer_gets_44_px_actions_and_places(tmp_path: Path) -> None:
    H = ("JSON.stringify({coarse: matchMedia('(pointer: coarse)').matches, w: innerWidth,"
         " acts: [...document.querySelectorAll('.ia-act')].map(b => Math.round(b.getBoundingClientRect().height)),"
         " places: [...document.querySelectorAll('.ia-places [data-ia-place-btn]')].map(b => Math.round(b.getBoundingClientRect().height))})")
    _root, src, _ids = _install(tmp_path)
    got = []
    for w in (390, 820):
        (tmp_path / str(w)).mkdir()
        _r, s2, _i = _install(tmp_path / str(w))
        with _serve(s2, surface_tokens={"browser": "T"}, install_class="reported") as (url, _httpd):
            out = drive_touch(tmp_path / str(w), f"{url}/#write_token=T", [{"emulate": {"w": w, "h": 900, "dpr": 2, "mobile": True}}, {"touch": True}, READY, SETTLE, {"eval": H}])
        got.append(json.loads([v for v in out["values"] if isinstance(v, str) and v.startswith("{")][-1]))
    for g in got:
        assert g["coarse"] is True, g                                              # the emulation is a touch device, so the rule under test applies
        assert len(g["acts"]) == 3 and len(g["places"]) == 5, g
        assert min(g["acts"] + g["places"]) >= 44, g


PANELS = {"until": "document.querySelector('.panel[data-ck-panel=\"tray\"]') && 1", "ms": 30000}
# this fixture's kernel draws the List only; the Board and the Inbox are a manifest's panels, so two empty ones stand beside it, placed by the same attributes
WORK_ROW = ("(() => { const t0 = document.querySelector('.panel[data-ck-panel=\"tray\"]');"
            " for (const k of ['board', 'inbox']) { if (!document.querySelector('.panel[data-ck-panel=\"' + k + '\"]')) { const p = document.createElement('section'); p.className = 'panel'; p.dataset.ckPanel = k; t0.parentNode.appendChild(p); } }"
            " const r =(n) => { const b = n.getBoundingClientRect(); return {x: Math.round(b.left), y: Math.round(b.top + scrollY), w: Math.round(b.width), h: Math.round(b.height), shown: n.getClientRects().length > 0}; };"
            " const q = (s) => document.querySelector(s);"
            " return JSON.stringify({head: r(q('.ia-head[data-ia-head=\"work\"]')), tray: r(q('.panel[data-ck-panel=\"tray\"]')), board: r(q('.panel[data-ck-panel=\"board\"]')),"
            " inbox: r(q('.panel[data-ck-panel=\"inbox\"]')), reset: !!q('.ck-zone[data-zone=\"operate\"] > .zone-head > [data-layout-reset]')}); })()")
# the Reset layout control, put where the page puts it (the operate zone head) while a layout is saved
ADD_RESET = ("(() => { const h = document.querySelector('.ck-zone[data-zone=\"operate\"] > .zone-head'); const b = document.createElement('button'); b.type = 'button';"
             " b.setAttribute('data-layout-reset', ''); b.textContent = 'Reset layout'; h.appendChild(b); return 1; })()")


@needs_browser
def test_the_work_row_is_one_row_with_the_head_above_the_three_panels(tmp_path: Path) -> None:
    """With no Reset layout control (the default) the Work head spans the row and the List starts below it, beside Board and Inbox; with the control, it takes the head's row end."""
    _root, src, _ids = _install(tmp_path)
    steps: list = []
    for w in (800, 1180, 1440, 1920):
        steps += [{"emulate": {"w": w, "h": 900, "dpr": 1}}, SETTLE, {"eval": WORK_ROW}]
    steps += [{"eval": ADD_RESET}, SETTLE]
    for w in (800, 1180, 1440):
        steps += [{"emulate": {"w": w, "h": 900, "dpr": 1}}, SETTLE, {"eval": WORK_ROW}]
    with _serve(src, surface_tokens={"browser": "T"}, install_class="reported") as (url, _httpd):
        out = _drive(tmp_path, f"{url}/#write_token=T", [{"emulate": {"w": 1440, "h": 900, "dpr": 1}}, READY, PANELS, SETTLE] + steps)
    rows = [json.loads(v) for v in out["values"] if isinstance(v, str) and v.startswith("{") and '"head"' in v]
    assert len(rows) == 7, out["values"]
    for g, w in zip(rows, (800, 1180, 1440, 1920, 800, 1180, 1440)):
        assert g["head"]["shown"] and g["tray"]["shown"] and g["board"]["shown"] and g["inbox"]["shown"], (w, g)
        assert g["tray"]["y"] >= g["head"]["y"] + g["head"]["h"], (w, g)               # the List is below the head, never beside it
        if w >= 1101:
            assert g["board"]["y"] == g["tray"]["y"] and g["inbox"]["y"] == g["tray"]["y"], (w, g)   # three across, one row
    assert not rows[0]["reset"] and rows[4]["reset"], (rows[0], rows[4])


@needs_browser
def test_the_keys_reach_every_place_and_leave_the_desk_and_room_without_a_retired_mode(tmp_path: Path) -> None:
    """Keys 2 and 3 reach the Desk and the Room, and from either one the Home click, key 1 and key 4 leave it; no mode but board and memory is ever written."""
    def at(step: dict) -> list:
        return [step, SETTLE, {"eval": STATE}]
    _root, src, _ids = _install(tmp_path)
    with _serve(src, surface_tokens={"browser": "T"}, install_class="reported") as (url, _httpd):
        out = _drive(tmp_path, f"{url}/#write_token=T", [
            {"emulate": {"w": 1440, "h": 900, "dpr": 1}},
            READY, SETTLE,
            {"eval": WATCH},
            *at({"eval": KEY % "2"}),                                    # 0: Desk
            *at({"eval": KEY % "3"}),                                    # 1: Room
            *at({"eval": PRESS % "home"}),                               # 2: Home, left by the click
            *at({"eval": KEY % "2"}),                                    # 3: Desk
            *at({"eval": KEY % "1"}),                                    # 4: Home, left by key 1
            *at({"eval": KEY % "2"}),                                    # 5: Desk
            *at({"eval": KEY % "4"}),                                    # 6: Memory, left by key 4
            *at({"eval": KEY % "3"}),                                    # 7: Room
            *at({"eval": KEY % "4"}),                                    # 8: Memory, from the Room
            {"eval": "JSON.stringify(window.__modes)"},
        ])
    states = [v for v in out["values"] if isinstance(v, dict) and "place" in v]
    assert [s["place"] for s in states] == ["desk", "room", "home", "desk", "home", "desk", "memory", "room", "memory"], states
    modes = json.loads(out["values"][-1])
    assert modes and set(modes) <= {"board", "memory"}, modes


@needs_browser
def test_a_hidden_hero_draws_no_band_and_no_graph_button_on_home(tmp_path: Path) -> None:
    """The page hides the hero region and drops body.ke when its page is not drawn (keShell); Home's grid rules must not draw the band or the Show-the-graph button of a hidden hero."""
    HIDE = "(() => { document.getElementById('ck-hero').hidden = true; document.body.classList.remove('ke'); return 1; })()"
    DRAWN = ("(() => { const n = (s) => [...document.querySelectorAll(s)].filter(e => e.getClientRects().length > 0).length;"
             " return JSON.stringify({hero: document.getElementById('ck-hero').hidden, band: n('#ck-band'), skycol: n('#ck-skycol'), toggle: n('[data-ia-graph-toggle]')}); })()")
    _root, src, _ids = _install(tmp_path)
    with _serve(src, surface_tokens={"browser": "T"}, install_class="reported") as (url, _httpd):
        out = _drive(tmp_path, f"{url}/#write_token=T", [
            {"emulate": {"w": 1440, "h": 900, "dpr": 1}},
            READY, SETTLE,
            {"eval": DRAWN},                                                            # 0: the hero drawn
            {"eval": HIDE}, SETTLE,
            {"eval": DRAWN},                                                            # 1: the hero hidden
        ])
    drawn, hidden = [json.loads(v) for v in out["values"] if isinstance(v, str) and v.startswith('{"hero"')]
    assert drawn["hero"] is False and drawn["band"] == 1 and drawn["toggle"] == 1, drawn
    assert hidden == {"hero": True, "band": 0, "skycol": 0, "toggle": 0}, hidden


@needs_browser
def test_the_collapsed_graph_strip_hides_the_event_graph_too(tmp_path: Path) -> None:
    """With the Event lens (or no topology at all) the graph is the page's #ck-sky: collapsed, it is not drawn, and the toggle opens it."""
    M = ("(() => { const q = (s) => document.querySelector(s); const r = (n) => n ? {shown: n.getClientRects().length > 0, h: Math.round(n.getBoundingClientRect().height), kids: n.children.length} : null;"
         " const sk = q('#ck-sky'); return JSON.stringify({open: document.body.classList.contains('ia-graph-open'), lens: q('#ck-skycard').dataset.hlens, sky: r(sk), legend: r(q('#ck-legend')),"
         " hlens: r(q('#ck-hlens')), card: Object.assign(r(q('#ck-skycard')), {y: Math.round(q('#ck-skycard').getBoundingClientRect().top)}),"
         " toggle: Object.assign(r(q('[data-ia-graph-toggle]')), {y: Math.round(q('[data-ia-graph-toggle]').getBoundingClientRect().top)}), canvas: sk ? sk.querySelectorAll('svg, canvas').length : 0}); })()")
    _root, src, _ids = _install(tmp_path)
    with _serve(src, surface_tokens={"browser": "T"}, install_class="reported") as (url, _httpd):
        out = _drive(tmp_path, f"{url}/#write_token=T", [
            {"emulate": {"w": 1440, "h": 900, "dpr": 1}},
            READY, SETTLE, SETTLE,
            {"eval": M},                                                                # 0: collapsed
            {"eval": "document.querySelector('[data-ia-graph-toggle]').click(); 1"}, SETTLE,
            {"eval": M},                                                                # 1: opened
        ])
    shut, opened = [json.loads(v) for v in out["values"] if isinstance(v, str) and v.startswith('{"open"')]
    assert shut["open"] is False and opened["open"] is True
    assert shut["lens"] == "events" and shut["canvas"] > 0, shut                       # the fixture has an Event graph to hide
    assert not shut["sky"]["shown"] and not shut["legend"]["shown"], shut              # collapsed: it is not drawn
    assert opened["sky"]["shown"] and opened["sky"]["h"] > 0, opened                   # the toggle opens it
    assert shut["toggle"]["h"] > 0 and shut["toggle"]["y"] + shut["toggle"]["h"] <= shut["card"]["y"] + 1, shut   # the toggle sits in the Machine control row above the card (levain#141 polish: it no longer floats over the card)

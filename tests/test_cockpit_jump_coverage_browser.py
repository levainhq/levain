"""levain#181 S1b, every visible location is a Jump target (headless Chrome on a scratch home, the real cockpit, a write token set).

The expectation is derived from the page by the DOM rule, never from a hand-copied list: every element with ``data-ck-panel``, every
section.ck-zone, ``#ck-needs``, ``#ck-drop``, every board and settings ``.panel``, the five places-bar buttons and the masthead's
Grants and Notifications buttons must have a Jump entry carrying the element's own label. "Must" is read from what a person sees: a
node painted at some place (it has a box there) is expected, whatever attribute it carries; the page's own ``[hidden]`` test is not
shared. A new panel therefore fails this file until
it has a title, and an element with no label is named here instead of being silently unreachable. The Desk and Room views mount only
at their place, so their entry is the place's.
Held: (a) from each of the five places the empty-query list covers the DOM rule, (b) Enter on a place, a Home card, a Settings panel, the
Desk and Room views and Grants lands focus where it should and switches the place, (c) typed "notifications" lists the Notifications
action, (d) the label-less guard fires on an untitled panel the test injects, (e) a node the page redrew between the list and Enter is
found again and focused, for each kind of target, and keeps focus through a redraw after landing, (f) a target that paints late is
waited for, (g) Needs you and Drop are reached from a place other than Home, (h) Ctrl+K and the Jump button do nothing over an open
dialog."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

from tests.test_cockpit_flip_parity import _drive, needs_browser
from tests.test_cockpit_k2a import _install, _serve
from tests.test_cockpit_places import READY, SETTLE
from tests.test_list_page_browser import _seed_more, _seed_phone

CTRL = 2
ENTER = {"key": "Enter", "vk": 13, "text": "\r"}
ESC = {"key": "Escape", "vk": 27}
CTRL_K = {"key": "k", "modifiers": CTRL, "vk": 75}
PLACES = ("home", "desk", "room", "memory", "settings")
NOT_DRAWN = ("notifications", "topology")


def _wait(ms: int) -> dict:
    return {"eval": f"new Promise(r => setTimeout(r, {ms})).then(() => 1)"}


def _go(place: str) -> list[dict]:
    return [{"eval": f"document.querySelector('[data-ia-place-btn={place}]').click(); 1"}, _wait(1500)]


# The page's own label for a node when it ships one (the shared labeller), else the same chain read here: the first h2, h3, .rv-title,
# then a card's .k, then the node's own aria-label.
LAB = ("const K = window.LevainCockpitKinds || {}; const sq = (s) => String(s || '').replace(/\\s+/g, ' ').trim();"
       " const lab = (n) => { if (K.jumpLabel) return K.jumpLabel(n);"
       " for (const q of ['h2', 'h3', '.rv-title', '.k']) { const h = n.querySelector(q); if (h && sq(h.textContent)) return sq(h.textContent); }"
       " return sq(n.getAttribute('aria-label')); };")
SEL = "[data-ck-panel], section.ck-zone, #ck-needs, #ck-drop, #board .panel, .dev-zone .panel"
COLLECT = ("(() => { " + LAB + " const NOT = " + json.dumps(list(NOT_DRAWN)) + ";"
           f" const cands = [...document.querySelectorAll({json.dumps(SEL)})].filter(n => !NOT.includes(n.dataset.ckPanel));"
           " const rows = cands.map(n => ({label: lab(n), id: n.dataset.ckPanel || n.id || lab(n) + '|' + n.className.toString().slice(0, 30), vis: n.getClientRects().length > 0,"
           "   dupZone: n.matches('.ck-zone') && cands.some(m => m !== n && n.contains(m) && lab(m) === lab(n))}));"
           " const places = [...document.querySelectorAll('[data-ia-place-btn]')].map(b => { const c = b.cloneNode(true);"
           "   for (const x of c.querySelectorAll('.ia-needs')) x.remove(); return sq(c.textContent); });"
           " const acts = [...document.querySelectorAll('.ia-act[data-phone-jump=grants], .ia-act[data-phone-jump=notifications]')].map(b => sq((b.querySelector('.ia-lbl') || b).textContent));"
           " const entries = [...document.querySelectorAll('.palette-list li[role=option]')].map(li => ({kind: li.dataset.kind || '', label: li.querySelector('.pl').textContent}));"
           " return JSON.stringify({probe: 'cov', place: document.body.dataset.iaPlace, rows, places, acts, entries}); })()")
INJECT = ("(() => { const b = document.getElementById('board'); const p = document.createElement('div'); p.className = 'panel'; p.dataset.ckPanel = 'zz-untitled'; p.dataset.modes = 'board';"
          " const body = document.createElement('div'); body.className = 'pbody'; body.textContent = 'a panel with no title'; p.appendChild(body); b.appendChild(p); return 1; })()")


# The injection and the collection are ONE evaluation: the cockpit rebuilds #board shortly after load, and a wait between the two would
# let that rebuild take the injected panel away. Opening Jump is the button's own click, which draws the list synchronously.
INJECT_AND_COLLECT = "(() => { " + INJECT + "; document.getElementById('jumpbtn').click(); return " + COLLECT + "; })()"
LANDED = "!document.querySelector('.palette') && document.activeElement && document.activeElement !== document.body"


def _until(cond: str, ms: int = 4000) -> dict:
    """Wait for a condition (polled), up to ms, then go on: the assertion after it is the judge, not the wait."""
    return {"eval": ("new Promise(res => { const t0 = Date.now(); const f = () => { let ok = false; try { ok = !!(" + cond + "); } catch (e) {}"
                     f" if (ok || Date.now() - t0 > {ms}) return res(ok ? 1 : 0); setTimeout(f, 25); }}; f(); }})")}


def _pick(kind: str, label: str) -> dict:
    """ArrowDown to the row of this kind and label (the key presses stay the reader's: Enter follows as a trusted key)."""
    return {"eval": ("(() => { const lis = [...document.querySelectorAll('.palette-list li[role=option]')];"
                     f" const i = lis.findIndex(li => li.dataset.kind === {json.dumps(kind)} && li.querySelector('.pl').textContent === {json.dumps(label)});"
                     " const inp = document.querySelector('.palette-input');"
                     " for (let k = 0; k < i; k++) inp.dispatchEvent(new KeyboardEvent('keydown', {key: 'ArrowDown', bubbles: true}));"
                     " return JSON.stringify({probe: 'pick', i, n: lis.length}); })()")}


def _at(match: str) -> dict:
    return {"eval": ("(() => { const a = document.activeElement;"
                     f" return JSON.stringify({{probe: 'at', place: document.body.dataset.iaPlace, open: !!document.querySelector('.palette'),"
                     f" tag: a.tagName, cls: String(a.className).slice(0, 60), tab: a.getAttribute('tabindex'), ok: a.matches({json.dumps(match)}),"
                     " drawn: a.getClientRects().length > 0}); })()")}


def _vals(out: dict, probe: str) -> list[dict]:
    got = [json.loads(v) for v in out["values"] if isinstance(v, str) and v.startswith('{"probe"')]
    return [g for g in got if g["probe"] == probe]


def _run(tmp_path: Path, steps: list[dict], w: int = 1280, h: int = 1000) -> dict:
    tmp_path.mkdir(parents=True, exist_ok=True)
    root, src, _ids = _install(tmp_path)
    _seed_more(root)
    _seed_phone(root)
    with _serve(src, write_token="T", surface_tokens={"browser": "T"}, install_class="reported") as (url, _httpd):
        return _drive(tmp_path, f"{url}/#write_token=T", [{"emulate": {"w": w, "h": h, "dpr": 1}}, READY, SETTLE, *steps])


def _expected(cov: dict, ever: set) -> Counter:
    want = Counter(cov["places"]) + Counter(cov["acts"])
    for r in cov["rows"]:
        if r["label"] and not r["dupZone"] and r["id"] in ever:
            want[r["label"]] += 1
    return want


def _unlabelled(cov: dict) -> list[str]:
    return [r["id"] for r in cov["rows"] if not r["label"] and r["vis"]]


@needs_browser
def test_from_every_place_the_empty_query_lists_every_location_the_dom_holds(tmp_path: Path) -> None:
    steps: list[dict] = [{"eval": "1"}]
    for p in PLACES:
        steps += [*_go(p), CTRL_K, _wait(150), {"eval": COLLECT}, ESC, _wait(80)]
    covs = _vals(_run(tmp_path, steps), "cov")
    assert [c["place"] for c in covs] == list(PLACES), covs
    ever = {r["id"] for c in covs for r in c["rows"] if r["vis"]}                            # visible in some place
    seen_unlabelled: set[str] = set()
    for c in covs:
        got = Counter(e["label"] for e in c["entries"])
        missing = _expected(c, ever) - got
        assert not missing, (c["place"], dict(missing), [e["label"] for e in c["entries"]])
        kinds = [e["kind"] for e in c["entries"]]
        assert kinds[:5] == ["place"] * 5, (c["place"], kinds)                                # the places come first
        assert [e["label"] for e in c["entries"] if e["kind"] == "action"] == ["Grants", "Notifications"], c["place"]
        seen_unlabelled.update(_unlabelled(c))
    assert not seen_unlabelled, f"visible with no label, so not a Jump target: {sorted(seen_unlabelled)}"
    # Notify tokens carries the hidden attribute yet is painted in Settings (an author rule gives it a box), so a person sees it there
    painted = [r for c in covs for r in c["rows"] if r["label"] == "Notify tokens" and r["vis"]]
    assert painted, "the fixture no longer paints Notify tokens: this check lost its subject"
    for c in covs:
        assert "Notify tokens" in [e["label"] for e in c["entries"]], (c["place"], [e["label"] for e in c["entries"]])


@needs_browser
def test_enter_on_each_kind_lands_focus_and_switches_the_place(tmp_path: Path) -> None:
    def choose(kind: str, label: str, match: str) -> list[dict]:
        return [CTRL_K, _until("document.querySelectorAll('.palette-list li').length"), _pick(kind, label), ENTER, _until(LANDED), _at(match)]
    steps: list[dict] = [
        *choose("place", "Memory", ".panel, [data-ck-panel]"),                               # 0 a place, from Home
        *choose("card", "Levain health", ".ck-card[data-ck-panel=doctor]"),                  # 1 a Home widget card, from Memory
        *choose("card", "Needs you", "#ck-needs"),                                           # 2 a Home section, from Home
        *choose("panel", "Paired devices", ".dev-panel"),                                    # 3 a Settings panel, from Home
        *choose("place", "Desk", "section.dv-zone[data-zone=desk]"),                         # 4 the Desk view, from Settings
        *choose("place", "Room", "section.rv-zone[data-zone=room]"),                         # 5 the Room view, from the Desk
        *choose("panel", "Notify tokens", ".nf-panel"),                                      # 6 a panel the page marks hidden but paints, from the Room
        *choose("action", "Grants", "#levain-modal *, #levain-modal"),                       # 7 an action: the dialog's own focus, from Settings
    ]
    run = _run(tmp_path, steps)
    picks, ats = _vals(run, "pick"), _vals(run, "at")
    assert all(p["i"] >= 0 for p in picks), picks                                           # every kind has a row
    want = ["memory", "home", "home", "settings", "desk", "room", "settings", "settings"]
    assert [a["place"] for a in ats] == want, ats
    for i, a in enumerate(ats):
        assert not a["open"] and a["ok"] and a["drawn"], (i, a)
    for i in (0, 1, 2, 3, 4, 5, 6):
        assert ats[i]["tab"] == "-1", (i, ats[i])                                           # the target itself, made focusable


@needs_browser
def test_typed_notifications_lists_the_notifications_action_and_enter_opens_its_dialog(tmp_path: Path) -> None:
    typed = {"eval": ("(() => { const i = document.querySelector('.palette-input'); i.value = 'notifications'; i.dispatchEvent(new Event('input', {bubbles: true}));"
                      " return 1; })()")}
    steps = [CTRL_K, _wait(100), typed, _wait(100), {"eval": COLLECT}, ENTER, _wait(1200),
             {"eval": ("(() => { const a = document.activeElement; return JSON.stringify({probe: 'dlg', open: !!document.querySelector('.palette'),"
                       " inDialog: !!a.closest('#levain-modal, .modal-overlay, [role=dialog]'), place: document.body.dataset.iaPlace}); })()")}]
    run = _run(tmp_path, steps)
    (cov,) = _vals(run, "cov")
    assert [(e["kind"], e["label"]) for e in cov["entries"]] == [("action", "Notifications")], cov["entries"]
    (dlg,) = _vals(run, "dlg")
    assert not dlg["open"] and dlg["inDialog"] and dlg["place"] == "home", dlg


@needs_browser
def test_a_visible_panel_with_no_title_is_named_by_the_guard(tmp_path: Path) -> None:
    (cov,) = _vals(_run(tmp_path, [{"eval": INJECT_AND_COLLECT}]), "cov")
    assert _unlabelled(cov) == ["zz-untitled"], _unlabelled(cov)                           # exactly the injected one, and nothing real
    assert "zz-untitled" not in [e["label"] for e in cov["entries"]], cov["entries"]


# ---- a target the page redrew between the list and Enter, and a redraw after landing ----------------------------------------------------
# A redraw is made here as the page makes one: the node is replaced by a fresh copy of itself (same markup, a new node). The Jump list
# holds the old node.
SWAP = ("(label) => { " + LAB + " const hit = [...document.querySelectorAll(" + json.dumps(SEL) + ")].find(n => n.isConnected && lab(n) === label);"
        " if (!hit) return false; hit.replaceWith(hit.cloneNode(true)); return true; }")
SWAP_FOCUSED = ("() => { const a = document.activeElement; const t = a && a.closest ? a.closest(" + json.dumps(SEL) + ") : null;"
                " if (!t) return false; t.replaceWith(t.cloneNode(true)); return true; }")
AT_LABEL = ("(() => { " + LAB + " const a = document.activeElement; const t = a && a.closest ? a.closest(" + json.dumps(SEL) + ") : null;"
            " return JSON.stringify({probe: 'atl', place: document.body.dataset.iaPlace, open: !!document.querySelector('.palette'), label: t ? lab(t) : '',"
            " body: a === document.body, drawn: !!a && a.getClientRects().length > 0}); })()")
# one case per kind of target: (kind, label, the place it is reached from, the place Enter must leave us at)
CASES = [
    ("panel", "Work board", "settings", "home"),        # a data-ck-panel panel
    ("panel", "Neocortex", "home", "memory"),           # a panel with no data-ck-panel
    ("panel", "Paired devices", "home", "settings"),    # the Settings panel the devices code builds
    ("card", "Levain health", "memory", "home"),        # a Home widget card
]


@needs_browser
def test_a_target_the_page_redrew_after_the_list_was_drawn_is_found_again_and_focused(tmp_path: Path) -> None:
    steps: list[dict] = [{"eval": "1"}]
    for kind, label, start, _want in CASES:
        steps += [*_go(start), CTRL_K, _until("document.querySelectorAll('.palette-list li').length"), _pick(kind, label),
                  {"eval": f"JSON.stringify({{probe: 'swap', done: ({SWAP})({json.dumps(label)})}})"},
                  ENTER, _until(LANDED), {"eval": AT_LABEL}]
    run = _run(tmp_path, steps)
    assert [s["done"] for s in _vals(run, "swap")] == [True] * len(CASES), _vals(run, "swap")
    for (kind, label, _start, want), got in zip(CASES, _vals(run, "atl")):
        assert got["place"] == want and got["label"] == label and not got["open"] and not got["body"] and got["drawn"], (kind, label, got)


@needs_browser
def test_a_redraw_after_landing_hands_focus_to_the_successor_for_every_kind_of_target(tmp_path: Path) -> None:
    steps: list[dict] = [{"eval": "1"}]
    for kind, label, start, _want in CASES:
        steps += [*_go(start), CTRL_K, _until("document.querySelectorAll('.palette-list li').length"), _pick(kind, label),
                  ENTER, _until(LANDED),
                  {"eval": "JSON.stringify({probe: 'swap', done: (" + SWAP_FOCUSED + ")()})"},
                  _until(LANDED), {"eval": AT_LABEL}]
    run = _run(tmp_path, steps)
    assert [s["done"] for s in _vals(run, "swap")] == [True] * len(CASES), _vals(run, "swap")
    for (kind, label, _start, want), got in zip(CASES, _vals(run, "atl")):
        assert got["place"] == want and got["label"] == label and not got["body"] and got["drawn"], (kind, label, got)


@needs_browser
def test_a_target_that_paints_after_enter_is_waited_for_and_then_focused(tmp_path: Path) -> None:
    """A place paints a few frames after Enter. Here a panel with no box (display: none) gets one four frames after the Enter key
    reaches the page, and focus is read by waiting for it, with no fixed sleep after Enter. A choice that lands at once focuses a node with
    no box, which the browser refuses, and nothing tries again. The panel sits on <body> so the board's own rebuild cannot take it."""
    inject = ("(() => { const p = document.createElement('div'); p.className = 'panel'; p.dataset.ckPanel = 'zz-late'; p.style.display = 'none';"
              " const h = document.createElement('h2'); h.textContent = 'Zz late panel'; p.appendChild(h); document.body.appendChild(p); window.__late = p;"
              " document.addEventListener('keydown', function f(e) { if (e.key !== 'Enter') return; document.removeEventListener('keydown', f, true);"
              "   let k = 0; const t = () => { if (++k < 4) requestAnimationFrame(t); else p.style.display = ''; }; requestAnimationFrame(t); }, true);"
              " document.getElementById('jumpbtn').click(); return 1; })()")
    steps = [{"eval": inject}, _until("document.querySelectorAll('.palette-list li').length"), _pick("panel", "Zz late panel"), ENTER,
             _until(LANDED), _at("[data-ck-panel=zz-late]")]
    (got,) = _vals(_run(tmp_path, steps), "at")
    assert got["ok"] and got["drawn"] and not got["open"], got


@needs_browser
def test_needs_you_and_drop_to_the_desk_are_reached_from_a_place_other_than_home(tmp_path: Path) -> None:
    def choose(label: str, start: str) -> list[dict]:
        return [*_go(start), CTRL_K, _until("document.querySelectorAll('.palette-list li').length"), _pick("card", label), ENTER, _until(LANDED)]
    steps = [{"eval": "1"}, *choose("Needs you", "settings"), _at("#ck-needs"), *choose("Drop to the desk", "memory"), _at("#ck-drop")]
    got = _vals(_run(tmp_path, steps), "at")
    assert [(g["place"], g["ok"], g["drawn"]) for g in got] == [("home", True, True)] * 2, got


@needs_browser
def test_ctrl_k_and_the_jump_button_do_nothing_over_an_open_dialog(tmp_path: Path) -> None:
    state = {"eval": ("JSON.stringify({probe: 'dlg', palette: !!document.querySelector('.palette'), modal: !!document.querySelector('.modal-overlay'),"
                      " place: document.body.dataset.iaPlace})")}
    open_grants = {"eval": "document.querySelector('.ia-act[data-phone-jump=grants]').click(); 1"}
    steps = [{"eval": "1"}, open_grants, _until("document.querySelector('.modal-overlay')"), CTRL_K, _wait(150), state,
             {"eval": "document.getElementById('jumpbtn').click(); 1"}, _wait(150), state]
    keyed, clicked = _vals(_run(tmp_path, steps), "dlg")
    assert keyed["modal"] and not keyed["palette"], keyed
    assert clicked["modal"] and not clicked["palette"], clicked


@needs_browser
def test_a_titled_panel_the_hidden_attribute_really_hides_is_not_listed(tmp_path: Path) -> None:
    inject = ("(() => { const mk = (label, hidden) => { const w = document.createElement('div'); w.hidden = hidden; const p = document.createElement('div');"
              " p.dataset.ckPanel = 'zz-' + label; const h = document.createElement('h2'); h.textContent = label; p.appendChild(h); w.appendChild(p);"
              " document.body.appendChild(w); }; mk('Zz hidden one', true); mk('Zz shown one', false);"
              " document.getElementById('jumpbtn').click(); return 1; })()")
    listed = {"eval": "JSON.stringify({probe: 'ls', labels: [...document.querySelectorAll('.palette-list li .pl')].map(n => n.textContent)})"}
    (got,) = _vals(_run(tmp_path, [{"eval": inject}, listed]), "ls")
    assert "Zz shown one" in got["labels"] and "Zz hidden one" not in got["labels"], got["labels"]

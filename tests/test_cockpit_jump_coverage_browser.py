"""levain#181 S1b, every visible location is a Jump target (headless Chrome on a scratch home, the real cockpit, a write token set).

The expectation is derived from the page by the DOM rule, never from a hand-copied list: every element with ``data-ck-panel``, every
section.ck-zone, ``#ck-needs``, ``#ck-drop``, every board and settings ``.panel``, the five places-bar buttons and the masthead's
Grants and Notifications buttons must have a Jump entry carrying the element's own label. A new panel therefore fails this file until
it has a title, and an element with no label is named here instead of being silently unreachable. The Desk and Room views mount only
at their place, so their entry is the place's.
Held: (a) from each of the five places the empty-query list covers the DOM rule, (b) Enter on a place, a Home card, a Settings panel, the
Desk and Room views and Grants lands focus where it should and switches the place, (c) typed "notifications" lists the Notifications
action, (d) the label-less guard fires on an untitled panel the test injects."""

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
           f" const cands = [...document.querySelectorAll({json.dumps(SEL)})].filter(n => !n.closest('[hidden]') && !NOT.includes(n.dataset.ckPanel));"
           " const rows = cands.map(n => ({label: lab(n), id: n.dataset.ckPanel || n.id || lab(n) + '|' + n.className.toString().slice(0, 30), vis: n.getClientRects().length > 0,"
           "   dupZone: n.matches('.ck-zone') && cands.some(m => m !== n && n.contains(m) && lab(m) === lab(n))}));"
           " const places = [...document.querySelectorAll('[data-ia-place-btn]')].map(b => { const c = b.cloneNode(true);"
           "   for (const x of c.querySelectorAll('.ia-needs')) x.remove(); return sq(c.textContent); });"
           " const acts = [...document.querySelectorAll('.ia-act[data-phone-jump=grants], .ia-act[data-phone-jump=notifications]')].map(b => sq((b.querySelector('.ia-lbl') || b).textContent));"
           " const entries = [...document.querySelectorAll('.palette-list li[role=option]')].map(li => ({kind: li.dataset.kind || '', label: li.querySelector('.pl').textContent}));"
           " return JSON.stringify({probe: 'cov', place: document.body.dataset.iaPlace, rows, places, acts, entries}); })()")
INJECT = ("(() => { const b = document.getElementById('board'); const p = document.createElement('div'); p.className = 'panel'; p.dataset.ckPanel = 'zz-untitled'; p.dataset.modes = 'board';"
          " const body = document.createElement('div'); body.className = 'pbody'; body.textContent = 'a panel with no title'; p.appendChild(body); b.appendChild(p); return 1; })()")


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


@needs_browser
def test_enter_on_each_kind_lands_focus_and_switches_the_place(tmp_path: Path) -> None:
    def choose(kind: str, label: str, match: str) -> list[dict]:
        return [CTRL_K, _wait(150), _pick(kind, label), ENTER, _wait(1200), _at(match)]
    steps: list[dict] = [
        *choose("place", "Memory", ".panel, [data-ck-panel]"),                               # 0 a place, from Home
        *choose("card", "Levain health", ".ck-card[data-ck-panel=doctor]"),                  # 1 a Home widget card, from Memory
        *choose("card", "Needs you", "#ck-needs"),                                           # 2 a Home section, from Home
        *choose("panel", "Paired devices", ".dev-panel"),                                    # 3 a Settings panel, from Home
        *choose("place", "Desk", "section.dv-zone[data-zone=desk]"),                         # 4 the Desk view, from Settings
        *choose("place", "Room", "section.rv-zone[data-zone=room]"),                         # 5 the Room view, from the Desk
        *choose("action", "Grants", "#levain-modal *, #levain-modal"),                       # 6 an action: the dialog's own focus, from the Room
    ]
    run = _run(tmp_path, steps)
    picks, ats = _vals(run, "pick"), _vals(run, "at")
    assert all(p["i"] >= 0 for p in picks), picks                                           # every kind has a row
    want = ["memory", "home", "home", "settings", "desk", "room", "room"]
    assert [a["place"] for a in ats] == want, ats
    for i, a in enumerate(ats):
        assert not a["open"] and a["ok"] and a["drawn"], (i, a)
    for i in (0, 1, 2, 3, 4, 5):
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
    steps = [{"eval": INJECT}, _wait(100), CTRL_K, _wait(150), {"eval": COLLECT}]
    (cov,) = _vals(_run(tmp_path, steps), "cov")
    assert _unlabelled(cov) == ["zz-untitled"], _unlabelled(cov)                           # exactly the injected one, and nothing real
    assert "zz-untitled" not in [e["label"] for e in cov["entries"]], cov["entries"]

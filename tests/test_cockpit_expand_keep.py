"""An expanded row survives the board being drawn again (reload, refresh, a stream event naming a core panel), a typed
panel repainted in place, and the focus modal rebuilt: still open, still closable, in the right panel, with its scroll."""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any

from anneal_memory import Store
from anneal_memory.spores import SporeStore
from tests.test_cockpit_flip_parity import _drive, needs_browser
from tests.test_cockpit_k2a import TODAY, _install
from tests.test_cockpit_scroll_keep_page import _Served, _serve_plain

LONG = "words long enough to run past the three line clamp of a row on any screen width " * 12


def _install_long(tmp_path: Path) -> tuple[Path, Any]:
    """The k2a install, plus episodes whose text overflows the clamp (and carries a search word)."""
    root, src, _ids = _install(tmp_path)
    with Store(root / ".levain" / "memory.db") as st:
        for i in range(14):
            st.record(f"needle episode {i} " + LONG, "observation", origin_key=f"expand.keep.{i}")
    return root, src


# the rows a reader could open: clamped text with the control shown
OPENABLE = ("(ROOT) => [...ROOT.querySelectorAll('.row.has-more[data-row-key], .row.has-more[data-keep-key]')].filter(r => { const t = r.querySelector(':scope > .clause-toggle');"
            " return t && getComputedStyle(t).display !== 'none'; })")
PANEL_OF = "(n) => { const p = n.closest('[data-ck-panel], [data-panel-id]'); return p ? (p.dataset.ckPanel || p.dataset.panelId) : null; }"


def _held(root_sel: str) -> str:
    """Every expanded row under root_sel as [key, has-more, aria-expanded, collapse control visible, panel id]."""
    return (f"(() => {{ const panelOf = {PANEL_OF}; return [...document.querySelectorAll('{root_sel} .row.row-expanded')].filter(r => r.dataset.rowKey || r.dataset.keepKey).map(r => {{"
            " const t = r.querySelector(':scope > .clause-toggle'); return [r.dataset.rowKey || r.dataset.keepKey, r.classList.contains('has-more'),"
            " t && t.getAttribute('aria-expanded'), !!t && getComputedStyle(t).display !== 'none', panelOf(r)]; }); })()")


def _open_in(root_sel: str, panel: str | None = None, key: str | None = None, nth: int = 0) -> str:
    """Open one openable row (by clicking the control a reader sees) and scroll its body; returns [key, panel, scrolled-to]."""
    pick = f"({OPENABLE})(document.querySelector('{root_sel}'))" + (f".filter(r => ({PANEL_OF})(r) === '{panel}')" if panel else "")
    if key:
        pick += f".filter(r => (r.dataset.rowKey || r.dataset.keepKey) === '{key}')"
    return (f"(() => {{ const panelOf = {PANEL_OF}; const r = ({pick})[{nth}]; if (!r) return null; r.querySelector(':scope > .clause-toggle').click();"
            f" const sc = ({SCROLLER})(r); if (sc) sc.scrollTop = 40;"
            " window.__sc = sc ? sc.scrollTop : 0; return [r.dataset.rowKey || r.dataset.keepKey, panelOf(r), window.__sc]; })()")


def _scroll_of(root_sel: str) -> str:
    """The scroll of the body that holds the opened row."""
    return (f"(() => {{ const r = [...document.querySelectorAll('{root_sel} .row.row-expanded')][0];"
            f" const sc = r && ({SCROLLER})(r); return sc ? sc.scrollTop : null; }})()")


# the body that scrolls the row: the nearest bounded ancestor that has somewhere to scroll
SCROLLER = ("(r) => { for (let n = r.parentElement; n; n = n.parentElement) "
            "if (n.matches('.pbody, .modal-body') && n.scrollHeight - n.clientHeight > 40) return n; return null; }")
COUNT_RENDERS = ("window.__r = 0; const L = window.LevainDashboard, o = L.render; "
                 "L.render = function () { try { return o.apply(this, arguments); } finally { window.__r++; } }; 1")
BOARD = "#board"
# the fixture rows are capped to a 160-character title: they clamp only on a narrow card (a lone card now stretches to the page width)
NARROW = {"emulate": {"w": 390, "h": 844, "dpr": 2, "mobile": True}}


def _assert_held(v: list[Any], opened: list[Any], held: list[Any], scroll: Any, why: str) -> None:
    key, panel, scrolled = opened
    assert held == [[key, True, "true", True, panel]], f"{why}: the row is not open and closable in its panel: {held} (opened {opened})"
    assert scrolled > 0, f"the body was never scrolled, the scroll check proves nothing: {opened}"
    assert abs(scroll - scrolled) <= 2, f"{why}: the scroll moved from {scrolled} to {scroll}"


@needs_browser
def test_refresh_keeps_an_expanded_row(tmp_path: Path) -> None:
    _root, src = _install_long(tmp_path)
    with _serve_plain(src) as url:
        out = _drive(tmp_path, f"{url}/", [
            {"until": f"({OPENABLE})(document.querySelector('{BOARD}')).length > 0 && 1"},
            {"eval": COUNT_RENDERS},
            {"eval": _open_in(BOARD, "episodes")},
            {"eval": _held(BOARD)},
            {"eval": "document.getElementById('refresh').click(); 1"},
            {"until": "window.__r >= 1 && 1"},
            {"eval": "new Promise(r => setTimeout(r, 600))"},
            {"eval": _held(BOARD)},
            {"eval": _scroll_of(BOARD)},
        ])
    v = out["values"]
    assert v[3] == [[v[2][0], True, "true", True, "episodes"]], v
    _assert_held(v, v[2], v[7], v[8], "the refresh")


@needs_browser
def test_a_store_write_keeps_an_expanded_row(tmp_path: Path) -> None:
    root, src = _install_long(tmp_path)
    store = SporeStore(root / ".levain" / "memory.spores.json")
    stop = threading.Event()

    def writer() -> None:    # a seat writing spores while the page is open: each write moves the panels' etags
        n = 0
        while not stop.wait(1.5):
            n += 1
            store.add(type="task", text=f"written while open {n}", tier="hot", today=TODAY)

    t = threading.Thread(target=writer, daemon=True)
    with _serve_plain(src) as url:
        t.start()
        try:
            out = _drive(tmp_path, f"{url}/", [
                {"until": f"({OPENABLE})(document.querySelector('{BOARD}')).length > 0 && 1"},
                {"eval": COUNT_RENDERS},
                {"eval": _open_in(BOARD, "episodes")},
                {"until": "window.__r >= 1 && 1", "ms": 30000},
                {"eval": "new Promise(r => setTimeout(r, 600))"},
                {"eval": _held(BOARD)},
                {"eval": _scroll_of(BOARD)},
            ])
        finally:
            stop.set()
            t.join(timeout=5)
    v = out["values"]
    assert v[3] is True or v[3] == 1, f"the board was never drawn again: {v}"
    _assert_held(v, v[2], v[5], v[6], "a store write")


def _things(state: dict[str, Any], names: list[str], search: bool = False) -> Any:
    from levain.cockpit.engine import ProviderSpec, StoreMatches
    from levain.cockpit.results import Read, RowIn

    def body(n: str, moved: bool) -> str:
        return f"{n} " + ("MMMMMMMM WWWWWWWW " if not moved else "mmmmmmmm wwwwwwww ") * 40

    def read(_c: object) -> Read:
        state["first"] = state["first"] or time.monotonic()
        # the data moves 3s after the first read: well after the draw, before the drive's explicit refetch
        moved = time.monotonic() - state["first"] >= 3
        return Read(rows=tuple(RowIn(n, body(n, moved and n == "alpha"), stored={"id": n}) for n in names))

    def hook(q: str, limit: int) -> Any:
        return StoreMatches(rows=tuple(RowIn(f"old{i}", f"old{i} " + "MMMMMMMM WWWWWWWW " * 40, stored={"id": f"old{i}"}) for i in range(4)), total=4)

    extra: dict[str, Any] = {"window": 2, "search_scope": "store", "search": hook} if search else {}
    return ProviderSpec(id="things", kind="list", title="Things", priority="feed", read=read, order="time.desc",
                        version_fields=("id",), region="operate", **extra)


@needs_browser
def test_a_panel_repainted_in_place_keeps_an_expanded_row(tmp_path: Path) -> None:
    """A typed panel whose data moved repaints its body without a board render: the row the reader opened stays open
    and closable, found by its id (the other row moving must not shift the open state onto it)."""
    from levain.cockpit.providers import build_default_cockpit

    _root, src = _install_long(tmp_path)
    ck = build_default_cockpit(src)
    ck.register(_things({"first": 0.0}, ["alpha", "beta"]))
    panel = "document.querySelector('[data-ck-panel=things]')"
    rows = f"[...{panel}.querySelectorAll('.row')]"
    with _Served(src, cockpit=ck, surface_tokens={"browser": "T"}, install_class="reported") as url:
        out = _drive(tmp_path, f"{url}/", [
            NARROW,
            {"until": f"{panel} && {rows}.filter(r => r.classList.contains('has-more')).length === 2 && 1"},
            {"eval": _open_in("[data-ck-panel=things]", key="beta")},
            {"eval": COUNT_RENDERS},
            {"eval": "new Promise(r => setTimeout(r, 3500)).then(() => window.LevainCockpitView.refetchPanel('things'))"},
            {"eval": f"[{rows}.length, window.__r, {rows}.map(r => r.textContent.slice(0, 5))]"},
            {"eval": _held("[data-ck-panel=things]")},
        ])
    v = out["values"]
    assert v[1] and v[1][0] == "beta", v
    assert v[3] == "applied", f"the refetch was not applied in place: {v}"
    assert v[4][0] == 2 and v[4][1] == 0, f"the repaint was not in place: {v}"
    assert v[5] == [["beta", True, "true", True, "things"]], f"the repaint closed or moved the open row: {v}"


@needs_browser
def test_a_store_search_answer_keeps_an_expanded_row(tmp_path: Path) -> None:
    """A searchable typed panel repaints a second time when its store search answers, after a refetch has restored its
    rows: the row open among the answers stays open."""
    from levain.cockpit.providers import build_default_cockpit

    _root, src = _install_long(tmp_path)
    ck = build_default_cockpit(src)
    ck.register(_things({"first": 0.0}, ["alpha", "beta", "gamma"], search=True))
    sel = "[data-ck-panel=things]"
    panel = f"document.querySelector('{sel}')"
    with _Served(src, cockpit=ck, surface_tokens={"browser": "T"}, install_class="reported") as url:
        out = _drive(tmp_path, f"{url}/", [
            NARROW,
            {"until": f"{panel} && {panel}.querySelector('.xfilter-input') && 1"},
            {"eval": f"(() => {{ const i = {panel}.querySelector('.xfilter-input'); i.value = 'old'; i.dispatchEvent(new Event('input', {{bubbles: true}})); return 1; }})()"},
            {"until": f"{panel}.querySelectorAll('.row.has-more[data-keep-key^=old]').length > 0 && 1", "ms": 20000},
            {"eval": _open_in(sel, nth=1)},
            {"eval": "new Promise(r => setTimeout(r, 300)).then(() => window.LevainCockpitView.refetchPanel('things'))"},
            {"eval": "new Promise(r => setTimeout(r, 1500))"},
            {"eval": _held(sel)},
        ])
    v = out["values"]
    assert v[3] and v[3][0] == "old1", v
    assert v[4] == "applied", v
    assert v[6] == [[v[3][0], True, "true", True, "things"]], f"the search answer closed the open row: {v}"


@needs_browser
def test_the_episodes_modal_search_keeps_an_open_row_across_two_rebuilds(tmp_path: Path) -> None:
    """The focus modal's keyword search fills its rows after a round-trip. Rebuilt twice before the answer lands, the
    modal must still hand back the row the reader had open and the place they were scrolled to."""
    _root, src = _install_long(tmp_path)
    modal = "#levain-modal"
    search = ("(() => { window.__delay = 0; const f = window.fetch; window.fetch = (u, ...a) => /recall\\.json/.test(String(u)) && window.__delay"
              " ? new Promise(r => setTimeout(r, window.__delay)).then(() => f(u, ...a)) : f(u, ...a);"
              " const i = document.querySelector('#levain-modal .ep-search-input'); i.value = 'needle';"
              " i.dispatchEvent(new KeyboardEvent('keydown', {key: 'Enter', bubbles: true})); return 1; })()")
    with _serve_plain(src) as url:
        out = _drive(tmp_path, f"{url}/", [
            {"until": f"({OPENABLE})(document.querySelector('{BOARD}')).length > 0 && 1"},
            {"eval": COUNT_RENDERS},
            {"eval": "[...document.querySelectorAll('#board .panel')].find(p => p.dataset.panelId === 'episodes' || /^Recent episodes/.test(p.querySelector('h2').textContent)).querySelector('.pexpand').click(); 1"},
            {"until": f"document.querySelector('{modal} .ep-search-input') && 1"},
            {"eval": search},
            {"until": f"/matches/.test((document.querySelector('{modal} .ep-search-status') || {{}}).textContent || '') && 1"},
            {"eval": _open_in(modal)},
            # the answer is now held back for 2s; two rebuilds arrive before it lands
            {"eval": "window.__delay = 2000; window.__r = 0; document.getElementById('refresh').click(); 1"},
            {"until": "window.__r >= 1 && 1"},
            {"eval": "document.getElementById('refresh').click(); 1"},
            {"until": "window.__r >= 2 && 1"},
            {"eval": f"new Promise(r => setTimeout(r, 3500)).then(() => [{_held(modal)}, {_scroll_of(modal)}])"},
        ])
    v = out["values"]
    opened = v[6]
    assert opened and opened[2] > 0, f"the modal body could not be scrolled, the check proves nothing: {v}"
    held, scroll = v[11]
    assert held and held[0][:4] == [opened[0], True, "true", True], f"the modal lost the open row: {v}"
    assert abs(scroll - opened[2]) <= 2, f"the modal lost its place: {v}"

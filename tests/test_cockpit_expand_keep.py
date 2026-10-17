"""An expanded row survives the board being drawn again (reload, refresh, a stream event naming a core panel)."""

from __future__ import annotations

import threading
import time
from pathlib import Path

from anneal_memory.spores import SporeStore
from tests.test_cockpit_flip_parity import _drive, needs_browser
from tests.test_cockpit_k2a import TODAY, _install
from tests.test_cockpit_scroll_keep_page import _Served, _serve_plain

ROW = "[...document.querySelectorAll('#board .row[data-row-key]')]"
EXPANDED = f"{ROW}.filter(r => r.classList.contains('row-expanded')).map(r => r.dataset.rowKey)"
OPEN_ONE = (f"(() => {{ const r = {ROW}.find(r => r.querySelector('.clause-toggle')); r.querySelector('.clause-toggle').click();"
            f" window.__key = r.dataset.rowKey; return window.__key; }})()")


@needs_browser
def test_refresh_keeps_an_expanded_row(tmp_path: Path) -> None:
    _root, src, _ids = _install(tmp_path)
    with _serve_plain(src) as url:
        out = _drive(tmp_path, f"{url}/", [
            {"until": f"{ROW}.length > 0 && 1"},
            {"eval": "window.__marker = 1; 1"},
            {"eval": OPEN_ONE},
            {"eval": EXPANDED},
            {"eval": "window.__r = 0; const L = window.LevainDashboard, o = L.render; L.render = function () { try { return o.apply(this, arguments); } finally { window.__r++; } }; document.getElementById('refresh').click(); 1"},
            {"until": "window.__r >= 1 && 1"},
            {"eval": "new Promise(r => setTimeout(() => r(" + EXPANDED + "), 500))"},
        ])
    v = out["values"]
    assert v[3] == [v[2]], v
    assert v[6] == [v[2]], f"the refresh closed the row: {v}"


@needs_browser
def test_a_store_write_keeps_an_expanded_row(tmp_path: Path) -> None:
    root, src, _ids = _install(tmp_path)
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
                {"until": f"{ROW}.length > 0 && 1"},
                {"eval": "window.__r = 0; const L = window.LevainDashboard, o = L.render; L.render = function () { try { return o.apply(this, arguments); } finally { window.__r++; } }; 1"},
                {"eval": OPEN_ONE},
                {"until": "window.__r >= 1 && 1", "ms": 30000},
                {"eval": "new Promise(r => setTimeout(() => r(" + EXPANDED + "), 500))"},
            ])
        finally:
            stop.set()
            t.join(timeout=5)
    v = out["values"]
    assert v[3] is True or v[3] == 1, f"the board was never drawn again: {v}"
    assert v[4] == [v[2]], f"a store write closed the row: {v}"


@needs_browser
def test_a_panel_repainted_in_place_keeps_an_expanded_row(tmp_path: Path) -> None:
    """A typed panel whose data moved repaints its body without a board render: the row the reader opened stays open,
    found by its id (the other row moving must not shift the open state onto it)."""
    from levain.cockpit.engine import ProviderSpec
    from levain.cockpit.providers import build_default_cockpit
    from levain.cockpit.results import Read, RowIn

    _root, src, _ids = _install(tmp_path)
    state = {"names": ["alpha", "beta"], "first": 0.0}

    def read(_c: object) -> Read:
        state["first"] = state["first"] or time.monotonic()
        # the data moves 3s after the first read: well after the draw, before the drive's explicit refetch
        moved = time.monotonic() - state["first"] >= 3
        # the count is in the panel's title, so a changed count is a changed shape (a board render): only the words move
        return Read(rows=tuple(RowIn(n, f"{n} " + ("moved words " if moved and n == "alpha" else "long words ") * 40,
                                     stored={"id": n}) for n in state["names"]))

    ck = build_default_cockpit(src)
    ck.register(ProviderSpec(id="things", kind="list", title="Things", priority="feed", read=read, order="time.desc",
                             version_fields=("id",), region="operate"))
    panel = "document.querySelector('[data-ck-panel=things]')"
    rows = f"[...{panel}.querySelectorAll('.row')]"
    open_ids = f"{rows}.filter(r => r.classList.contains('row-expanded')).map(r => r.textContent.slice(0, 5))"
    with _Served(src, cockpit=ck, surface_tokens={"browser": "T"}, install_class="reported") as url:
        out = _drive(tmp_path, f"{url}/", [
            {"until": f"{panel} && {rows}.length === 2 && 1"},
            {"eval": f"{rows}.find(r => r.textContent.startsWith('beta')).querySelector('.clause-toggle').click(); {open_ids}"},
            {"eval": "window.__renders = 0; const L = window.LevainDashboard, o = L.render; L.render = function () { window.__renders++; return o.apply(this, arguments); }; 1"},
            {"eval": "new Promise(r => setTimeout(r, 3500)).then(() => window.LevainCockpitView.refetchPanel('things'))"},
            {"eval": f"[{rows}.length, {open_ids}, window.__renders]"},
        ])
    v = out["values"]
    assert v[1] == ["beta "], v
    assert v[3] == "applied", f"the refetch was not applied in place: {v}"
    assert v[4] == [2, ["beta "], 0], f"the repaint closed or moved the open row: {v}"

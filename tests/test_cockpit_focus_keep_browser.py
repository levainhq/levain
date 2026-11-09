"""A re-read of the board never takes the keyboard from a field the person is typing in (real headless Chrome, a served
cockpit whose store changes under the typist, so the page re-reads while keys go in). Observed in use: the box lost focus
mid-word and the rest of the typing went nowhere. Two places are pinned: the box on the home screen, and the same box in
the List's focus view. A field the person has left idle, or one outside the redrawn regions, never freezes the board."""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

from anneal_memory.spores import SporeStore

from tests.test_cockpit_flip_parity import _drive, needs_browser
from tests.test_cockpit_k2a import _install, _serve
from tests.test_list_page_browser import INPUT, _reload, _seed_more

KEYS = 45          # the key step waits 200 ms after each press: several churn ticks pass while typing
MODAL_INPUT = "document.querySelector('#levain-modal .list-intake-input')"
READ = ("JSON.stringify({kept: document.activeElement.classList.contains('list-intake-input'), "
        "len: %s.value.length, board_redraws: window.__redraws, deferred: window.__deferred})")
# the board's own redraws, and every passive read the page refused for the field (the status line says so each time)
COUNT = ("window.__redraws = 0; window.__deferred = 0; "
         "new MutationObserver(() => { window.__redraws++; }).observe(document.getElementById('board'), {childList: true}); "
         "new MutationObserver(() => { if (/deferred/.test(document.getElementById('stamp').textContent)) window.__deferred++; })"
         ".observe(document.getElementById('stamp'), {childList: true, characterData: true, subtree: true}); 1")
REDRAWN = "window.__redraws > 0 && 1"


def _start(tmp_path: Path, open_modal: bool, box: str, churn_on: bool = True):
    root, src, _ids = _install(tmp_path)
    _seed_more(root)
    steps = [{"until": "document.querySelector('[data-ck-panel=tray]') && 1"}, *_reload(),
             {"until": f"{INPUT} && 1"}, {"eval": COUNT}]
    if open_modal:
        steps += [{"eval": "document.querySelector('[data-ck-panel=tray]').querySelector('.pexpand').click(); 1"},
                  {"until": f"{MODAL_INPUT} && 1"}]
    return root, src, steps


class _Churn:
    """The store changing under the typist; a failure in the thread is raised where the test joins it."""

    def __init__(self, root: Path, on: bool = True) -> None:
        self.root, self.on, self.stop, self.err = root, on, threading.Event(), []
        self.th = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        try:
            store = SporeStore(self.root / ".levain" / "memory.spores.json")
            n = 0
            while self.on and not self.stop.is_set():
                n += 1
                store.add(type="task", text=f"churn {n}", disposition="seed")
                ctx = self.root / ".levain" / "context.json"
                tmp = ctx.with_name("context.json.tmp")
                tmp.write_text(json.dumps({"state": f"s{n}", "state_set_at": "2026-11-09T12:00:00+00:00", "state_source": "cli"}))
                os.replace(tmp, ctx)
                time.sleep(0.7)
        except BaseException as e:  # noqa: BLE001
            self.err.append(e)

    def __enter__(self) -> "_Churn":
        self.th.start()
        return self

    def __exit__(self, *a: object) -> None:
        self.stop.set()
        self.th.join(timeout=5)
        if self.err:
            raise self.err[0]


def _run_steps(tmp_path: Path, root: Path, src, steps: list, churn: bool = True) -> list:
    with _serve(src, surface_tokens={"browser": "T"}, install_class="reported") as (url, _h):
        with _Churn(root, churn):
            out = _drive(tmp_path, f"{url}/#write_token=T", steps)
    return out["values"]


def _typing(tmp_path: Path, box: str, open_modal: bool) -> list:
    root, src, steps = _start(tmp_path, open_modal, box)
    steps += [{"eval": f"{box}.focus(); 1"}, *({"key": "a"} for _ in range(KEYS)), {"eval": READ % box},
              # leaving the field lets the deferred read land: the board is drawn again
              {"eval": f"{box}.blur(); 1"}, {"until": REDRAWN}, {"eval": "JSON.stringify({after: window.__redraws})"}]
    return _run_steps(tmp_path, root, src, steps)


def _check_typing(vals: list) -> None:
    got = json.loads(vals[-4])
    assert got["kept"] and got["len"] == KEYS, got
    assert got["deferred"] > 0, got           # the page tried to re-read and was refused (a pass is not a run where nothing re-read)
    assert got["board_redraws"] == 0, got     # no destructive redraw while typing
    assert json.loads(vals[-1])["after"] > 0  # the owed read landed once the field was left


@needs_browser
def test_typing_on_the_home_screen_survives_a_reread(tmp_path: Path) -> None:
    _check_typing(_typing(tmp_path, INPUT, open_modal=False))


@needs_browser
def test_typing_in_a_panel_modal_survives_a_reread(tmp_path: Path) -> None:
    _check_typing(_typing(tmp_path, MODAL_INPUT, open_modal=True))


@needs_browser
def test_an_idle_empty_focused_field_does_not_freeze_the_board(tmp_path: Path) -> None:
    root, src, steps = _start(tmp_path, False, INPUT)
    # the counter is reset AFTER the focus step, so a redraw that landed before the focus cannot satisfy the wait
    steps += [{"eval": f"{INPUT}.focus(); window.__redraws = 0; JSON.stringify({{on: document.activeElement === {INPUT}}})"},
              {"until": REDRAWN}, {"eval": "JSON.stringify({r: window.__redraws})"}]
    vals = _run_steps(tmp_path, root, src, steps)
    assert json.loads(vals[-3])["on"], vals[-3]
    assert json.loads(vals[-1])["r"] > 0


@needs_browser
def test_typing_elsewhere_does_not_hold_a_fresh_empty_board_field(tmp_path: Path) -> None:
    # recency is per element: keys typed in a field the draw does not rebuild never defer a read for a different empty field
    root, src, steps = _start(tmp_path, False, INPUT)
    steps += [{"eval": "const i = document.createElement('input'); i.id = 'elsewhere'; document.body.appendChild(i); i.focus(); 1"},
              *({"key": "a"} for _ in range(3)),
              {"eval": f"{INPUT}.focus(); window.__redraws = 0; window.__t0 = Date.now(); JSON.stringify({{on: document.activeElement === {INPUT}}})"},
              {"until": REDRAWN}, {"eval": "JSON.stringify({r: window.__redraws, ms: Date.now() - window.__t0})"}]
    vals = _run_steps(tmp_path, root, src, steps)
    assert json.loads(vals[-3])["on"], vals[-3]
    got = json.loads(vals[-1])
    assert got["r"] > 0 and got["ms"] < 9000, got   # a held read would wait out the whole idle window


@needs_browser
def test_a_field_outside_the_redrawn_regions_never_blocks(tmp_path: Path) -> None:
    root, src, steps = _start(tmp_path, False, INPUT)
    # the fixture serves no chat page, so a plain body input stands in for the chat input (the same: outside every rebuilt region)
    out = "document.getElementById('outside-field')"
    steps += [{"eval": "const i = document.createElement('input'); i.id = 'outside-field'; document.body.appendChild(i); i.focus(); 1"},
              *({"key": "a"} for _ in range(10)), {"until": REDRAWN},
              {"eval": f"JSON.stringify({{kept: document.activeElement === {out}, len: {out}.value.length}})"}]
    got = json.loads(_run_steps(tmp_path, root, src, steps)[-1])
    assert got == {"kept": True, "len": 10}, got


@needs_browser
def test_a_refused_visibility_read_is_owed_and_lands_on_blur(tmp_path: Path) -> None:
    root, src, steps = _start(tmp_path, False, INPUT)
    steps += [{"eval": f"{INPUT}.focus(); 1"}, {"key": "a"},
              {"eval": "document.dispatchEvent(new Event('visibilitychange')); 1"},
              {"until": "window.__deferred > 0 && 1"},
              {"eval": "JSON.stringify({r: window.__redraws})"},
              {"eval": f"{INPUT}.blur(); 1"}, {"until": REDRAWN}, {"eval": "JSON.stringify({r: window.__redraws})"}]
    vals = _run_steps(tmp_path, root, src, steps, churn=False)
    assert json.loads(vals[-4])["r"] == 0 and json.loads(vals[-1])["r"] > 0, vals[-5:]


@needs_browser
def test_a_focused_field_does_not_count_as_an_unsaved_edit(tmp_path: Path) -> None:
    root, src, steps = _start(tmp_path, False, INPUT)
    steps += [{"eval": f"{INPUT}.focus(); 1"}, {"key": "a"},
              {"eval": "JSON.stringify({edit: window.LevainDashboard.hasUnsavedEdit(), hold: window.LevainDashboard.editHoldsRefresh()})"}]
    got = json.loads(_run_steps(tmp_path, root, src, steps, churn=False)[-1])
    assert got == {"edit": False, "hold": True}, got


@needs_browser
def test_a_blur_during_a_press_does_not_redraw_under_the_click(tmp_path: Path) -> None:
    root, src, steps = _start(tmp_path, False, INPUT)
    steps += [{"eval": f"{INPUT}.focus(); 1"}, {"key": "a"},
              {"eval": "document.dispatchEvent(new Event('visibilitychange')); 1"},
              {"until": "window.__deferred > 0 && 1"},
              {"eval": "document.dispatchEvent(new Event('pointerdown')); document.activeElement.blur(); window.__t0 = Date.now(); 1"},
              {"until": "Date.now() - window.__t0 > 600"},
              {"eval": "JSON.stringify({r: window.__redraws})"},
              {"eval": "document.dispatchEvent(new Event('pointerup')); document.body.click(); 1"},
              {"until": REDRAWN}]
    vals = _run_steps(tmp_path, root, src, steps, churn=False)
    assert json.loads(vals[-3])["r"] == 0, vals

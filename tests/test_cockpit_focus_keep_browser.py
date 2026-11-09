"""A re-read of the board never takes the keyboard from a field the person is typing in (real headless Chrome, a served
cockpit whose store changes under the typist, so the page re-reads while keys go in). Observed in use: the box lost focus
mid-word and the rest of the typing went nowhere. Two places are pinned: the box on the home screen, and the same box in
the List's focus view."""
from __future__ import annotations

import json
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
        "len: %s.value.length, board_redraws: window.__redraws})")


def _typing(tmp_path: Path, box: str, open_modal: bool) -> dict:
    root, src, _ids = _install(tmp_path)
    _seed_more(root)
    stop = threading.Event()

    def churn() -> None:
        store = SporeStore(root / ".levain" / "memory.spores.json")
        n = 0
        while not stop.is_set():
            n += 1
            store.add(type="task", text=f"churn {n}", disposition="seed")
            (root / ".levain" / "context.json").write_text(json.dumps(
                {"state": f"s{n}", "state_set_at": "2026-11-09T12:00:00+00:00", "state_source": "cli"}))
            time.sleep(0.7)

    steps = [{"until": "document.querySelector('[data-ck-panel=tray]') && 1"}, *_reload(),
             {"until": f"{INPUT} && 1"},
             # count the board's own redraws, so a pass cannot be a run in which nothing ever re-read
             {"eval": "window.__redraws = 0; new MutationObserver(() => { window.__redraws++; })"
                      ".observe(document.getElementById('board'), {childList: true}); 1"}]
    if open_modal:
        steps += [{"eval": "document.querySelector('[data-ck-panel=tray]').querySelector('.pexpand').click(); 1"},
                  {"until": f"{MODAL_INPUT} && 1"}]
    steps += [{"eval": f"{box}.focus(); 1"}, *({"key": "a"} for _ in range(KEYS)), {"eval": READ % box}]
    with _serve(src, surface_tokens={"browser": "T"}, install_class="reported") as (url, _h):
        th = threading.Thread(target=churn, daemon=True)
        th.start()
        try:
            out = _drive(tmp_path, f"{url}/#write_token=T", steps)
        finally:
            stop.set()
            th.join(timeout=5)
    return json.loads(out["values"][-1])


@needs_browser
def test_typing_on_the_home_screen_survives_a_reread(tmp_path: Path) -> None:
    got = _typing(tmp_path, INPUT, open_modal=False)
    assert got["kept"] and got["len"] == KEYS, got


@needs_browser
def test_typing_in_a_panel_modal_survives_a_reread(tmp_path: Path) -> None:
    got = _typing(tmp_path, MODAL_INPUT, open_modal=True)
    assert got["kept"] and got["len"] == KEYS, got

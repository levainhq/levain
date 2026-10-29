"""C12 follow-up: the Board tab lists the ready-queue cards GROUPED UNDER COLUMN HEADINGS, and the six columns live only in the
⤢ focus modal. Real headless Chrome over a seeded coordinator store: the panel holds six `.grp-head` and no `.bd-cols`, exactly
one tell box with one button; ⤢ opens a modal holding the six `.bd-col` (and no tell box), which stays open and repaints on the
board's next poll; Esc closes it. `LEVAIN_BOARD_SHOTS=<dir>` keeps the screenshots there (the default is the test's tmp dir)."""
from __future__ import annotations

import json
import os
from pathlib import Path

from levain.consent.questions import QuestionStore
from levain.coordinator import ready
from levain.coordinator.store import CoordStore
from tests.test_board_compose_prefill_browser import POLL_MS, _reload_on, _Sheet
from tests.test_cockpit_flip_parity import _drive, needs_browser
from tests.test_cockpit_k2a import TOKEN, _install, _serve
from tests.test_cockpit_questions import _file
from tests.test_cockpit_questions import lh  # noqa: F401  (fixture)

PANEL = "document.querySelector('.bd-panel')"
MODAL = "document.getElementById('levain-modal')"
# nothing inside the open modal is still animating in (a finished fill-forwards animation stays listed, so: not running)
SETTLED = (f"{MODAL} && {MODAL}.getAnimations({{subtree: true}}).every(a => a.playState !== 'running')"
           f" && getComputedStyle({MODAL}.querySelector('.modal')).opacity === '1'")
ORDER = ["backlog", "ready", "running", "waiting", "needs-you", "done"]


def _seed(lh: Path) -> None:
    """Cards in five columns: two in the backlog (one held: it waits on a card not done), a ready one, a running one, a done one,
    and one Needs you band card."""
    st = CoordStore(lh)
    tok = st.coord_take()
    kw = dict(repo="levain", who="desk", why="queued it.")
    ready.add(st, tok, "levain:a", title="Land the board follow-up", to="ready", tier="sonnet", done_line="green", **kw)
    ready.add(st, tok, "levain:b", title="Cut the release after A", to="ready", tier="sonnet", done_line="green",
              depends_on=["levain:a"], **kw)
    ready.add(st, tok, "levain:c", title="Review the outbox", to="ready", tier="sonnet", done_line="green", **kw)
    ready.add(st, tok, "levain:d", title="Later D", to="backlog", tier="sonnet", done_line="green", **kw)
    ready.add(st, tok, "levain:e", title="Shipped E", to="ready", tier="sonnet", done_line="green", **kw)
    ready.move(st, tok, "levain:c", to="running", who="seat-9", why="the seat took it.")
    ready.move(st, tok, "levain:e", to="done", who="seat-3", why="merged.")
    _file(QuestionStore(lh))


@needs_browser
def test_the_tab_is_grouped_and_the_columns_live_in_the_expand_modal(tmp_path: Path, lh) -> None:  # noqa: F811
    lh.mkdir(parents=True, exist_ok=True, mode=0o700)
    _seed(lh)
    shots = Path(os.environ.get("LEVAIN_BOARD_SHOTS") or tmp_path)
    shots.mkdir(parents=True, exist_ok=True)
    _root, src, _ids = _install(tmp_path)
    facts = (f"JSON.stringify({{heads: [...{PANEL}.querySelectorAll('.grp-head')].map(h => h.textContent), "
             f"cols: {PANEL}.querySelectorAll('.bd-cols').length, tells: {PANEL}.querySelectorAll('.bd-tell').length, "
             f"tellButtons: [...{PANEL}.querySelectorAll('.bd-tell button')].map(b => b.textContent), "
             f"rows: {PANEL}.querySelectorAll('.bd-row').length, held: [...{PANEL}.querySelectorAll('.bd-held')].map(h => h.textContent), "
             f"expand: [...{PANEL}.querySelectorAll('.phead .pexpand')].length, "
             f"expandInHost: {PANEL}.querySelectorAll('.bd-view .pexpand, .bd-tellhost .pexpand').length}})")
    modal = (f"JSON.stringify({{cols: [...{MODAL}.querySelectorAll('.bd-col')].map(c => c.dataset.category), "
             f"tells: {MODAL}.querySelectorAll('.bd-tell, textarea').length, title: {MODAL}.querySelector('h2').textContent, "
             f"cards: {MODAL}.querySelectorAll('.bd-card').length}})")
    with _serve(src, write_token=TOKEN) as (url, httpd):
        httpd.coord_sheet = _Sheet(lh)
        out = _drive(tmp_path, f"{url}/", [
            {"emulate": {"w": 1440, "h": 1000, "dpr": 1}},
            *_reload_on(""),
            {"until": f"{PANEL} && {PANEL}.querySelectorAll('.grp-head').length === 6 && {PANEL}.querySelectorAll('.bd-tell button').length === 1 "
                      f"&& !{PANEL}.querySelector('.bd-tell textarea').hidden"},
            {"eval": facts},
            {"eval": f"{PANEL}.scrollIntoView(); 1"},
            {"shot": str(shots / "board_tab_grouped_desktop.png")},
            {"eval": f"{PANEL}.querySelector('.phead .pexpand').click(); 1"},
            {"until": f"{MODAL} && {MODAL}.querySelectorAll('.bd-col').length === 6"},
            {"eval": modal},
            {"until": SETTLED},                                          # the open animation is done: the picture is the settled modal
            {"shot": str(shots / "board_modal_columns_desktop.png")},
            # live: the board's next poll repaints the open modal (its columns body is a new node), and it is still open
            {"eval": f"window.__cols = {MODAL}.querySelector('.bd-cols'); 1"},
            {"until": f"{MODAL} && {MODAL}.querySelector('.bd-cols') !== window.__cols && {MODAL}.querySelectorAll('.bd-col').length === 6", "ms": POLL_MS * 3},
            {"key": "Escape"},
            {"until": f"!{MODAL} && !document.body.classList.contains('modal-open')"},
            {"eval": facts},
            {"emulate": {"w": 390, "h": 844, "dpr": 2, "mobile": True}},
            {"eval": f"{PANEL}.scrollIntoView(); 1"},
            {"until": f"{PANEL}.querySelectorAll('.grp-head').length === 6 && document.documentElement.scrollWidth <= window.innerWidth + 1"},
            {"shot": str(shots / "board_tab_grouped_phone.png")},
            {"eval": f"{PANEL}.querySelector('.phead .pexpand').click(); 1"},
            {"until": f"{MODAL} && {MODAL}.querySelectorAll('.bd-col').length === 6"},
            {"eval": f"JSON.stringify([{MODAL}.scrollWidth <= window.innerWidth + 1, document.documentElement.scrollWidth <= window.innerWidth + 1])"},
            {"until": SETTLED},
            {"shot": str(shots / "board_modal_columns_phone.png")},
        ])
    v = out["values"]
    first = json.loads(v[next(i for i, x in enumerate(v) if isinstance(x, str) and x.startswith('{"heads"'))])
    assert first["heads"] == ["backlog 2", "ready 1", "running 1", "waiting 0", "needs-you 1", "done 1"], first
    assert first["cols"] == 0, first                                       # no columns body in the panel
    assert first["tells"] == 1 and first["tellButtons"] == ["Send"], first   # the one tell box, the one button
    assert first["rows"] == 6 and first["held"] == ["held: depends on levain:a"], first
    assert first["expand"] == 1 and first["expandInHost"] == 0, first      # the focus button is in the panel head, not the host
    opened = next(json.loads(x) for x in v if isinstance(x, str) and x.startswith('{"cols":["'))
    assert opened["cols"] == ORDER and opened["cards"] == 6 and opened["tells"] == 0 and opened["title"] == "Board", opened
    closed = [json.loads(x) for x in v if isinstance(x, str) and x.startswith('{"heads"')]
    assert closed[-1]["heads"] == first["heads"], closed[-1]               # Esc closed the modal; the tab is as it was
    assert json.loads(v[-3]) == [True, True], v[-3]                     # the modal and the page fit the phone width
    assert not [p for p in out["posts"]], out["posts"]                      # nothing in all of this posted

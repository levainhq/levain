"""One Work board per cockpit, in real headless Chrome. The page has no standalone "Ready queue" zone and no tell box; with no
board provider registered the cockpit draws its own Work board from the coordinator store (as an ordinary board-kind panel);
with one registered only the provider's board draws; and every card's "Drop about this card" button opens the cockpit's drop box
with the card's words in it, the cursor after them, posting nothing. `LEVAIN_BOARD_SHOTS=<dir>` keeps the screenshots there
(the default is the test's tmp dir)."""
from __future__ import annotations

import json
import os
import time
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

from levain.consent.questions import QuestionStore
from levain.coordinator import ready
from levain.coordinator.store import CoordStore
from tests.test_cockpit_flip_parity import _drive, needs_browser
from tests.test_cockpit_k2a import TOKEN, _install, _serve
from tests.test_cockpit_questions import _file
from tests.test_cockpit_questions import lh  # noqa: F401  (fixture)

MODAL = "document.getElementById('levain-modal')"
# nothing inside the open modal is still animating in (a finished fill-forwards animation stays listed, so: not running)
SETTLED = (f"{MODAL} && {MODAL}.getAnimations({{subtree: true}}).every(a => a.playState !== 'running')"
           f" && getComputedStyle({MODAL}.querySelector('.modal')).opacity === '1'")
ORDER = ["backlog", "ready", "running", "waiting", "needs-you", "done"]
BUILTIN = "document.querySelector('[data-ck-panel=work_board]')"
HEADS = ["backlog 2", "ready 1", "running 1", "waiting 0", "needs-you 1", "done 1"]


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


class _Sheet:
    """The drop sheet as the page sees it: an identity listing and the one drop route, recording each drop it is handed."""
    rp_id = "localhost"
    STORE_ID = "S1"
    signer = SimpleNamespace(key_facts=lambda fps: {})

    def __init__(self, queue_dir):
        self.queue_dir, self.dropped = queue_dir, []

    def close(self):
        pass

    def listing(self):
        return {"coord_store_read": True, "coord_store_id": self.STORE_ID, "coord_drops": [], "coord_signers": []}

    def step(self, action, req):
        assert action == "drop", action
        datetime.fromisoformat(req["sent_at"].replace("Z", "+00:00"))
        self.dropped.append((action, req))
        return {"created": True, "drop": {"drop_id": "d-1"}}


def _reload() -> list:
    """Storage as an operator's browser already holds it (the write token), then the page fresh."""
    return [{"eval": f"localStorage.setItem('levain_write_token', {json.dumps(TOKEN)}); "
                     "sessionStorage.setItem('t0', String(performance.timeOrigin)); 1"},
            {"eval": "location.reload(); 1"},
            {"until": "String(performance.timeOrigin) !== sessionStorage.getItem('t0')"}]


def _facts(panel: str) -> str:
    return (f"JSON.stringify({{heads: [...{panel}.querySelectorAll('.grp-head')].map(h => h.textContent), "
            f"title: ({panel}.querySelector('h2') || {{}}).textContent, rows: {panel}.querySelectorAll('.bd-row').length, "
            f"drops: {panel}.querySelectorAll('.bd-drop').length, expand: {panel}.querySelectorAll('.phead .pexpand').length}})")


@needs_browser
def test_the_page_has_no_ready_queue_zone_and_no_tell_box_and_draws_the_work_board_from_levains_store(tmp_path: Path, lh) -> None:  # noqa: F811
    lh.mkdir(parents=True, exist_ok=True, mode=0o700)
    _seed(lh)
    shots = Path(os.environ.get("LEVAIN_BOARD_SHOTS") or tmp_path)
    shots.mkdir(parents=True, exist_ok=True)
    _root, src, _ids = _install(tmp_path)
    page = ("JSON.stringify({zones: document.querySelectorAll('[data-zone=ready-board], .bd-zone, .bd-tell, .bd-tellhost').length, "
            "ready: document.body.innerText.includes('Ready queue'), tell: document.body.innerText.includes('tell the coordinator'), "
            "boards: [...document.querySelectorAll('[data-ck-panel]')].filter(p => p.querySelector('.bd-groups')).map(p => p.dataset.ckPanel)})")
    modal = (f"JSON.stringify({{cols: [...{MODAL}.querySelectorAll('.bd-col')].map(c => c.dataset.category), "
             f"cards: {MODAL}.querySelectorAll('.bd-card').length, title: {MODAL}.querySelector('h2').textContent}})")
    with _serve(src, write_token=TOKEN) as (url, httpd):
        httpd.coord_sheet = _Sheet(lh)
        out = _drive(tmp_path, f"{url}/", [
            {"emulate": {"w": 1280, "h": 1000, "dpr": 1}},
            *_reload(),
            {"until": f"{BUILTIN} && {BUILTIN}.querySelectorAll('.grp-head').length === 6 && 1"},
            {"eval": page},
            {"eval": _facts(BUILTIN)},
            {"eval": f"{BUILTIN}.scrollIntoView(); 1"},
            {"shot": str(shots / "board_1280_after.png")},
            {"eval": f"{BUILTIN}.querySelector('.phead .pexpand').click(); 1"},
            {"until": f"{MODAL} && {MODAL}.querySelectorAll('.bd-col').length === 6"},
            {"eval": modal},
            {"until": SETTLED},
            {"key": "Escape"},
            {"until": f"!{MODAL} && !document.body.classList.contains('modal-open')"},
            {"emulate": {"w": 390, "h": 844, "dpr": 2, "mobile": True}},
            {"eval": f"{BUILTIN}.scrollIntoView(); 1"},
            {"until": f"{BUILTIN}.querySelectorAll('.grp-head').length === 6 && document.documentElement.scrollWidth <= window.innerWidth + 1"},
            {"shot": str(shots / "board_390_after.png")},
        ])
    v = out["values"]
    seen = json.loads(next(x for x in v if isinstance(x, str) and x.startswith('{"zones"')))
    assert seen["zones"] == 0 and seen["ready"] is False and seen["tell"] is False, seen      # no standalone zone, no tell box
    assert seen["boards"] == ["work_board"], seen                                               # one board, the built-in
    first = json.loads(next(x for x in v if isinstance(x, str) and x.startswith('{"heads"')))
    assert first["heads"] == HEADS and first["title"] == "Work board", first                 # levain's own columns
    assert first["rows"] == 6 and first["drops"] == 6 and first["expand"] == 1, first
    opened = json.loads(next(x for x in v if isinstance(x, str) and x.startswith('{"cols":["')))
    assert opened["cols"] == ORDER and opened["cards"] == 6 and opened["title"] == "Work board", opened
    assert not out["posts"], out["posts"]


@needs_browser
def test_with_a_board_provider_registered_only_its_board_draws(tmp_path: Path, lh) -> None:  # noqa: F811
    from levain.cockpit import ProviderSpec, Read
    from levain.cockpit import board
    from levain.cockpit.providers import build_default_cockpit
    from tests.test_cockpit_scroll_keep_page import _Served

    lh.mkdir(parents=True, exist_ok=True, mode=0o700)
    _seed(lh)
    value = board.build(lh, QuestionStore(lh))
    value["columns"][1]["cards"][0]["title"] = "The provider's own card"      # told apart from levain's store, which holds none by this title
    _root, src, _ids = _install(tmp_path)
    ck = build_default_cockpit(src)
    ck.register(ProviderSpec(id="boardkind", kind="board", title="Provider board", priority="feed",
                             read=lambda c: Read(value=value), region="operate"))
    page = ("JSON.stringify({boards: [...document.querySelectorAll('[data-ck-panel]')].filter(p => p.querySelector('.bd-groups')).map(p => p.dataset.ckPanel), "
            "builtin: document.querySelectorAll('[data-ck-panel=work_board]').length, "
            "titles: [...document.querySelectorAll('.bd-groups .bd-title')].map(t => t.textContent).filter(t => t.includes('provider'))})")
    with _Served(src, cockpit=ck, surface_tokens={"browser": "T"}, install_class="reported") as url:
        out = _drive(tmp_path, f"{url}/", [
            {"until": "document.querySelector('[data-ck-panel=boardkind] .grp-head') && 1"},
            {"eval": "new Promise(r => setTimeout(r, 1500))"},          # a second board would have drawn by now
            {"eval": page},
        ])
    got = json.loads(out["values"][-1])
    assert got["boards"] == ["boardkind"] and got["builtin"] == 0, got
    assert got["titles"] == ["The provider's own card"], got
    # registered AFTER the server attached its own: the provider replaces it (the registry, not the page, keeps it to one)
    from levain.cockpit.engine import BOARD_DEFAULT_ID
    assert BOARD_DEFAULT_ID not in ck.panel_ids


@needs_browser
def test_drop_about_this_card_opens_the_drop_box_with_that_cards_words(tmp_path: Path, lh) -> None:  # noqa: F811
    lh.mkdir(parents=True, exist_ok=True, mode=0o700)
    _seed(lh)
    _root, src, _ids = _install(tmp_path)
    box = "document.getElementById('ck-drop-words')"
    want = "About card levain:a (Land the board follow-up): "
    txt = f"window.LevainDashboard.dropBoxText({box})"      # the hero box is a contenteditable div: words through the helper, the caret from the selection
    caret = ("(() => { const b = " + box + "; const s = getSelection(); if (!s.rangeCount) return [-1, -1]; const r = s.getRangeAt(0); const at = (n, o) => { const p = document.createRange();"
             " p.selectNodeContents(b); p.setEnd(n, o); return p.toString().length; }; return [at(r.startContainer, r.startOffset), at(r.endContainer, r.endOffset)]; })()")
    state = (f"JSON.stringify([{txt}, document.activeElement === {box}, ...{caret}, "
             f"document.querySelector('[data-drop-about=\"levain:a\"]').textContent])")
    with _serve(src, write_token=TOKEN) as (url, httpd):
        httpd.coord_sheet = sheet = _Sheet(lh)
        out = _drive(tmp_path, f"{url}/", [
            {"emulate": {"w": 1280, "h": 1000, "dpr": 1}},
            *_reload(),
            {"until": f"{BUILTIN} && {BUILTIN}.querySelector('[data-drop-about=\"levain:a\"]') && {box} && !{box}.matches('[aria-disabled=true]')"},
            {"eval": txt},                                                       # empty before
            {"eval": f"{BUILTIN}.querySelector('[data-drop-about=\"levain:a\"]').click(); 1"},
            {"until": f"{txt} === {json.dumps(want)} && document.activeElement === {box} && 1"},
            {"eval": state},
            # a second card with words already in the box: they stay, the new words follow on a new paragraph
            {"eval": f"{BUILTIN}.querySelector('[data-drop-about=\"levain:d\"]').click(); {txt}"},
        ])
    v = out["values"]
    assert v[-5] == ""                                                           # the box was empty
    value, focused, start, end, label = json.loads(v[-2])
    assert value == want and focused is True and start == end == len(want) and label == "Drop about this card"
    assert v[-1] == want.rstrip() + "\n\nAbout card levain:d (Later D): "       # earlier words kept, the new ones after them
    assert out["posts"] == [] and sheet.dropped == []                            # a click posts nothing: it is a drop the operator sends


class _SlowSheet(_Sheet):
    """A drop sheet whose first read is slow, as on a loaded runner: the page is up and the board drawn before the drop box has
    learned whether it can take words."""
    delay = 1.5

    def listing(self):
        time.sleep(self.delay)
        return super().listing()


@needs_browser
def test_a_card_clicked_before_the_drop_box_has_read_is_not_lost(tmp_path: Path, lh) -> None:  # noqa: F811
    """The drop textarea is drawn enabled while `can` is still false until its first read lands. A "Drop about this card" click in
    that window used to be refused and the refusal wiped by the read, so the words never arrived (the CI flake: step 7, the box
    empty and unfocused). The words now wait for the read and go in, with focus, when it says the box can take them; and are
    refused with the reason when it says it cannot."""
    lh.mkdir(parents=True, exist_ok=True, mode=0o700)
    _seed(lh)
    _root, src, _ids = _install(tmp_path)
    box, host = "document.getElementById('ck-drop-words')", "document.getElementById('ck-drop')"
    txt = f"window.LevainDashboard.dropBoxText({box})"
    want = "About card levain:a (Land the board follow-up): "
    click = lambda k: f"{BUILTIN}.querySelector('[data-drop-about=\"{k}\"]').click(); 1"
    with _serve(src, write_token=TOKEN) as (url, httpd):
        httpd.coord_sheet = sheet = _SlowSheet(lh)
        out = _drive(tmp_path, f"{url}/", [
            {"emulate": {"w": 1280, "h": 1000, "dpr": 1}},
            *_reload(),
            {"until": f"{BUILTIN} && {BUILTIN}.querySelector('[data-drop-about=\"levain:a\"]') && {box} && 1"},
            {"eval": f"[{host}._drop.read, {host}._drop.can]"},          # the click below is before the read: the window under test
            {"eval": click("levain:a")},
            {"until": f"{txt} === {json.dumps(want)} && document.activeElement === {box} && 1"},
            {"eval": f"JSON.stringify([{txt}, (() => {{ const s = getSelection(), r = s.getRangeAt(0), p = document.createRange(); p.selectNodeContents({box}); p.setEnd(r.startContainer, r.startOffset); return p.toString().length; }})(), {host}._drop.pending, {host}._drop.say.textContent])"},
        ])
    v = out["values"]
    assert v[-4] == [None, False], v                                           # no read had landed when the card was clicked
    value, caret, pending, say = json.loads(v[-1])
    assert value == want and caret == len(want) and pending is None and say == ""
    assert out["posts"] == [] and sheet.dropped == []


@needs_browser
def test_a_board_whose_drop_files_to_another_coordinator_draws_no_drop_button(tmp_path: Path, lh) -> None:  # noqa: F811
    lh.mkdir(parents=True, exist_ok=True, mode=0o700)
    _seed(lh)
    _root, src, _ids = _install(tmp_path)
    with _serve(src, write_token=TOKEN) as (url, httpd):
        httpd.coord_sheet = _Sheet(tmp_path / "another-queue")                  # the drop route files elsewhere than the board reads
        out = _drive(tmp_path, f"{url}/", [
            {"emulate": {"w": 1280, "h": 1000, "dpr": 1}},
            *_reload(),
            {"until": f"{BUILTIN} && {BUILTIN}.querySelectorAll('.bd-row').length === 6 && 1"},
            {"eval": f"{BUILTIN}.querySelectorAll('.bd-drop').length"},
        ])
    assert out["values"][-1] == 0


@needs_browser
def test_compose_drop_refuses_a_box_that_is_busy_or_off_and_clears_a_stale_handoff_on_success(tmp_path: Path, lh) -> None:  # noqa: F811
    lh.mkdir(parents=True, exist_ok=True, mode=0o700)
    _seed(lh)
    _root, src, _ids = _install(tmp_path)
    host, box = "document.getElementById('ck-drop')", "document.getElementById('ck-drop-words')"
    txt = f"window.LevainDashboard.dropBoxText({box})"
    state = (f"JSON.stringify([{txt}, {host}._drop.say.textContent, sessionStorage.getItem('levain-drop-prefill')])")
    with _serve(src, write_token=TOKEN) as (url, httpd):
        httpd.coord_sheet = _Sheet(lh)
        out = _drive(tmp_path, f"{url}/", [
            {"emulate": {"w": 1280, "h": 1000, "dpr": 1}},
            *_reload(),
            # the box starts enabled and off: it is ready to be driven by hand only once its first read has landed
            {"until": f"{BUILTIN} && {host} && {host}._drop && {host}._drop.read && {host}._drop.can && !{box}.matches('[aria-disabled=true]') && 1"},
            {"eval": "sessionStorage.setItem('levain-drop-prefill', 'stale'); 1"},
            {"eval": f"{host}._drop.working = true; [LevainDashboard.composeDrop('x'), {state}]"},        # a send in flight
            {"eval": f"{host}._drop.working = false; {host}._drop.can = false; {host}._drop.note = 'no store'; "
                     f"[LevainDashboard.composeDrop('x'), {state}]"},                                       # the box is off
            {"eval": f"{host}._drop.can = true; {host}._drop.note = 'queue is read-only'; [LevainDashboard.composeDrop('About: '), {state}]"},       # a box that takes words
        ])
    busy, off, ok = (json.loads(x) if isinstance(x, str) else x for x in out["values"][-3:])
    assert busy[0] is False and json.loads(busy[1])[0] == "" and json.loads(busy[1])[1] == "The drop box is busy"
    assert json.loads(busy[1])[2] == "stale"                                   # a refusal leaves the handoff alone
    assert off[0] is False and json.loads(off[1])[1] == "The drop box is off: no store" and json.loads(off[1])[0] == ""
    assert ok[0] is True and json.loads(ok[1])[0] == "About: " and json.loads(ok[1])[2] is None   # the stale handoff is cleared
    assert json.loads(ok[1])[1] == "queue is read-only"                    # a coordinator note is not hidden by a successful compose

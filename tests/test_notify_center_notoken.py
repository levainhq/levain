"""A push tap on a page that holds NO write token yet (a Home Screen app opened right after a device was re-paired): the deep link
(/?view=notifications&n=<id>) waits for the first authoritative read of the center and is consumed once, then; it is never dropped
by the answer "no token" and never started on a timer. Real headless Chrome over a real server on a scratch queue."""
from __future__ import annotations

import json
from pathlib import Path

from tests.test_cockpit_flip_parity import _drive, needs_browser
from tests.test_cockpit_webpush import _signed
from tests.test_cockpit_pairing_http import T
from tests.test_notify_center_page import MODAL, ROWS, Rig, _center, _json

WIDE = {"emulate": {"w": 1280, "h": 900, "dpr": 1}}


def _bare(port: int, query: str = "") -> str:
    """The page's address with NO write token in the fragment."""
    return f"http://127.0.0.1:{port}/{query}"


def _wait(ms: int) -> dict:
    return {"eval": f"new Promise(r => setTimeout(r, {ms})).then(() => 1)"}


# the token arrives the way pairing and the owner prompt hand it over: stored, then announced (neither re-reads the center by itself)
GIVE_TOKEN = {"eval": f"localStorage.setItem('levain_write_token', {json.dumps(T)}); window.dispatchEvent(new Event('levain:write-token-change')); 1"}

STATE = (f"JSON.stringify({{ modal: !!{MODAL}, title: ({MODAL} && {MODAL}.querySelector('.nc-dtitle') || {{}}).textContent || '',"
         f" gone: ({MODAL} && {MODAL}.querySelector('.nc-gone') || {{}}).textContent || '', rows: {MODAL} ? {MODAL}.querySelectorAll('.nc-row').length : 0,"
         f" status: window.LevainNotifyCenter.state().status, url: location.search,"
         f" aside: !!document.getElementById('levain-modal') && !!document.querySelector('.pair-veil, #ck-unpaired') }})")
PROBE = {"eval": STATE}


NEXT_READ = {"eval": "window.LevainNotifyCenter.read(true).then(() => 1)"}   # what the 30 s pulse (or the next focus) does
TITLE = f"({MODAL} && {MODAL}.querySelector('.nc-dtitle') || {{}}).textContent || ''"
OPENED = {"until": f"{MODAL} && {MODAL}.querySelector('.nc-dtitle') && 1", "ms": 40000}


@needs_browser
def test_a_tap_on_a_page_with_no_token_opens_its_item_at_the_first_read_that_holds_one_and_only_once(tmp_path: Path) -> None:
    """The center opens at once without the item (no token: the read is refused client-side, status noauth). The deep link is kept, not
    dropped by that answer and not given a timer: when a token is stored and the next read lands, the item opens; the reader then opens
    another row, and further reads never bring the deep-linked one back (consumed once)."""
    r = Rig(tmp_path)
    with _signed(r.src, r.q) as (port, _):
        ids = r.seed(port)
        out = _drive(tmp_path, _bare(port, f"?view=notifications&n={ids['flow']}"), [
            WIDE, {"until": f"{MODAL} && window.LevainNotifyCenter.state().status === 'noauth' && 1", "ms": 40000}, _wait(800), PROBE,
            GIVE_TOKEN, NEXT_READ, OPENED, PROBE,
            {"eval": f"[...{ROWS}].find(r => r.querySelector('.nc-title').textContent === 'B2 backup finished').querySelector('.nc-main').click(); 1"},
            {"until": f"{TITLE} === 'B2 backup finished' && 1", "ms": 30000},
            NEXT_READ, NEXT_READ, _wait(600), {"eval": f"JSON.stringify({{ title: {TITLE} }})"}])
    before, opened, after = _json(out)
    assert before["modal"] is True and before["title"] == "" and before["rows"] == 0 and before["status"] == "noauth", before
    assert opened["title"] == "Inbox digest" and opened["rows"] >= 6 and opened["status"] == "ok" and opened["url"] == "", opened
    assert after == {"title": "B2 backup finished"}, after


@needs_browser
def test_a_tap_on_a_page_that_holds_a_token_opens_its_item(tmp_path: Path) -> None:
    """The token is already stored (the common tap): the same address opens the item without any later read."""
    r = Rig(tmp_path)
    with _signed(r.src, r.q) as (port, _):
        ids = r.seed(port)
        out = _drive(tmp_path, f"http://127.0.0.1:{port}/?view=notifications&n={ids['flow']}#write_token={T}", [WIDE, OPENED, PROBE])
    got = _json(out)[0]
    assert got["title"] == "Inbox digest" and got["status"] == "ok" and got["url"] == "", got


@needs_browser
def test_an_unknown_or_malformed_n_selects_nothing_with_or_without_a_token(tmp_path: Path) -> None:
    """`n=` that is not an id (malformed) opens the list and selects nothing; a well-formed id the store does not hold says so and selects
    nothing; the same unknown id on a page with no token yet says so once a token and a read arrive (it neither selects nor lingers)."""
    r = Rig(tmp_path)
    gone = f"({MODAL} && {MODAL}.querySelector('.nc-gone') || {{}}).textContent || ''"
    banner = f"({MODAL} && {MODAL}.querySelector('.nc-banner-t') || {{}}).textContent || ''"
    with _signed(r.src, r.q) as (port, _):
        r.seed(port)
        bad = _drive(tmp_path, f"http://127.0.0.1:{port}/?view=notifications&n=not-an-id#write_token={T}",
                     [WIDE, {"until": f"{ROWS}.length >= 6 && 1", "ms": 40000}, _wait(800), PROBE, {"eval": f"JSON.stringify({{ gone: {gone}, banner: {banner} }})"}])
        unknown = _drive(tmp_path, f"http://127.0.0.1:{port}/?view=notifications&n=p99999999#write_token={T}",
                         [WIDE, {"until": f"{gone} && 1", "ms": 40000}, PROBE, {"eval": f"JSON.stringify({{ gone: {gone} }})"}])
        late = _drive(tmp_path, _bare(port, "?view=notifications&n=p99999999"), [
            WIDE, {"until": f"{MODAL} && window.LevainNotifyCenter.state().status === 'noauth' && 1", "ms": 40000}, _wait(800),
            {"eval": f"JSON.stringify({{ gone: {gone} }})"}, GIVE_TOKEN, NEXT_READ, {"until": f"{gone} && 1", "ms": 40000}, PROBE])
    b, bg = _json(bad)
    assert b["title"] == "" and b["rows"] >= 6 and bg == {"gone": "", "banner": ""}, (b, bg)   # no lookup was made for it, so no word of any kind
    u, ug = _json(unknown)
    assert u["title"] == "" and ug["gone"] != "", (u, ug)
    pre, post = _json(late)
    assert pre["gone"] == "" and post["title"] == "" and post["status"] == "ok", (pre, post)


# the token's own announce, and another tab's change: the two ways a token reaches a page that is already open
STORE_ONLY = {"eval": f"localStorage.setItem('levain_write_token', {json.dumps(T)}); 1"}
FIRE_STORAGE = {"eval": "window.dispatchEvent(new StorageEvent('storage', { key: 'levain_write_token' })); 1"}
OTHER_KEY = {"eval": "window.dispatchEvent(new StorageEvent('storage', { key: 'something_else' })); 1"}
# long enough for the 600 ms and 3 s boot reads to be over before the token comes, and well short of the 30 s pulse
SETTLE = _wait(3800)


def _late_token(tmp_path: Path, name: str, give: list[dict]) -> tuple[dict, dict]:
    tmp_path = tmp_path / name
    tmp_path.mkdir()
    r = Rig(tmp_path)
    with _signed(r.src, r.q) as (port, _):
        ids = r.seed(port)
        out = _drive(tmp_path, _bare(port, f"?view=notifications&n={ids['flow']}"), [
            WIDE, {"until": f"{MODAL} && window.LevainNotifyCenter.state().status === 'noauth' && 1", "ms": 40000}, SETTLE, PROBE,
            *give, _wait(2000), PROBE])
    before, after = _json(out)
    return before, after


@needs_browser
def test_a_token_that_arrives_after_the_tap_opens_its_item_at_once_not_at_the_next_pulse(tmp_path: Path) -> None:
    """The same page announces the token it just stored (pairing, the owner prompt): the center reads then, with no other read
    and no tap, so the kept deep link opens within about two seconds and not at the 30 s pulse."""
    before, after = _late_token(tmp_path, "announce", [GIVE_TOKEN])
    assert before["modal"] is True and before["title"] == "" and before["status"] == "noauth", before
    assert after["title"] == "Inbox digest" and after["status"] == "ok" and after["rows"] >= 6, after


@needs_browser
def test_a_token_stored_by_another_tab_opens_the_item_at_once_and_an_unrelated_storage_event_reads_nothing(tmp_path: Path) -> None:
    """Another tab's change arrives as a `storage` event for the token key: the same prompt read. A storage event for a different
    key does not read (the token is stored but nothing announced it, so the page is still empty)."""
    before, still = _late_token(tmp_path, "other", [STORE_ONLY, OTHER_KEY])
    assert before["title"] == "" and still["title"] == "" and still["status"] == "noauth", (before, still)
    before, after = _late_token(tmp_path, "storage", [STORE_ONLY, FIRE_STORAGE])
    assert before["title"] == "" and after["title"] == "Inbox digest" and after["status"] == "ok", (before, after)

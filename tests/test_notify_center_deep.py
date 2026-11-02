"""The notification center with a long list (S4 L3 r2): 120 seeded items, real headless Chrome over a real server on a scratch queue.
A refresh keeps the pages the reader loaded; a pinned (deep-linked) item survives a failed delete and a mark-all."""
from __future__ import annotations

import json
from pathlib import Path

from tests.test_cockpit_flip_parity import _drive, needs_browser
from tests.test_cockpit_webpush import _signed
from tests.test_cockpit_pairing_http import T, _call
from tests.test_notify_center_page import (MODAL, OPEN, PILL, ROWS, Rig, _center, _json, _url)
from levain.webpush.store import PushStore

N = 120


def _seed_many(r: Rig) -> None:
    st = PushStore(r.q)
    for i in range(N):
        st.add_ping("dated", None, "levain", f"Deep item {i:03d}\n\nbody {i}")


READY = {"until": f"{PILL} && !{PILL}.hidden && 1", "ms": 40000}
WIDE = {"emulate": {"w": 1280, "h": 900, "dpr": 1}}
SETTLE = {"eval": "new Promise(r => setTimeout(r, 700)).then(() => 1)"}


@needs_browser
def test_opening_an_older_item_after_load_older_keeps_the_loaded_pages_and_the_detail(tmp_path: Path) -> None:
    """After Load older, opening an unread item beyond the first page marks it read and the refresh that follows keeps every loaded row and
    the open detail (it used to replace the list with page one and close the detail); the 30 s refresh does the same."""
    r = Rig(tmp_path)
    _seed_many(r)
    state = (f"JSON.stringify({{ rows: {ROWS}.length, title: ({MODAL}.querySelector('.nc-dtitle') || {{}}).textContent || '',"
             f" older: !!{MODAL}.querySelector('.nc-older') }})")
    with _signed(r.src, r.q) as (port, _):
        r.seed(port)
        out = _drive(tmp_path, _url(port), [WIDE, READY, OPEN, {"until": f"{ROWS}.length >= 50 && 1", "ms": 30000},
                                            {"eval": f"{MODAL}.querySelector('.nc-older').click(); 1"}, {"until": f"{ROWS}.length >= 100 && 1", "ms": 30000},
                                            {"eval": f"JSON.stringify({{ before: {ROWS}.length }})"},
                                            {"eval": f"{ROWS}[90].querySelector('.nc-main').click(); 1"}, SETTLE, {"eval": state},
                                            {"eval": "window.LevainNotifyCenter.read(false).then(() => 1)"}, SETTLE, {"eval": state}])
        server = _call(port, "GET", "/webpush/center?limit=200", token=T)[1]
    before, opened, polled = _json(out)
    assert before["before"] >= 100, before
    for s in (opened, polled):
        assert s["rows"] >= 100 and s["title"].startswith("Deep item") and s["older"] is True, s
    assert sum(1 for i in server["items"] if i["read"]) >= 1                          # the open really marked something read


@needs_browser
def test_a_failed_delete_keeps_a_deep_linked_item_and_mark_all_read_marks_it(tmp_path: Path) -> None:
    """A deep-linked item beyond page one: a delete the server refuses leaves its detail on screen to retry (with the write banner kept even
    when the refresh also fails); Mark all read then shows it read."""
    r = Rig(tmp_path)
    _seed_many(r)
    fail = ("(() => { const f = window.fetch; window.__fail = 'delete'; window.fetch = (u, o) => (window.__fail === 'delete' && String(u).includes('/center/delete')"
            " ? Promise.resolve(new Response(JSON.stringify({ok: false}), {status: 503})) : window.__fail === 'delete' && String(u).startsWith('/webpush/center?')"
            " ? Promise.reject(new TypeError('offline')) : f(u, o)); return 1; })()")
    detail = (f"JSON.stringify({{ title: ({MODAL}.querySelector('.nc-dtitle') || {{}}).textContent || '', tg: ({MODAL}.querySelector('.nc-toggle') || {{}}).textContent || '',"
              f" banner: ({MODAL}.querySelector('.nc-banner-t') || {{}}).textContent || '' }})")
    with _signed(r.src, r.q) as (port, _):
        r.seed(port)
        oldest = _center(port)
        # the oldest ping's id: walk to the end of the list
        ids = [i["id"] for i in oldest["items"] if i["title"] == "Deep item 000"]
        nxt = oldest["next_before"]
        while nxt:
            page = _call(port, "GET", f"/webpush/center?limit=200&before={nxt}", token=T)[1]
            ids += [i["id"] for i in page["items"] if i["title"] == "Deep item 000"]
            nxt = page["next_before"]
        assert len(ids) == 1, ids
        target = ids[0]
        out = _drive(tmp_path, _url(port, f"?view=notifications&n={target}"), [
            WIDE, {"until": f"{MODAL} && {MODAL}.querySelector('.nc-dtitle') && 1", "ms": 40000}, SETTLE, {"eval": fail},
            {"eval": f"{MODAL}.querySelector('.nc-delete').click(); 1"}, SETTLE, {"eval": detail},
            {"eval": "window.__fail = ''; 1"},
            {"eval": f"{MODAL}.querySelector('.nc-morebtn').click(); {MODAL}.querySelector('.nc-menu-mark').click(); 1"}, SETTLE, {"eval": detail}])
    failed, marked = _json(out)
    assert failed["title"].startswith("Deep item 000") and failed["banner"].startswith("Delete didn’t reach Levain"), failed
    assert marked["tg"].strip() == "Mark unread", marked


@needs_browser
def test_a_load_older_in_flight_is_dropped_when_a_delete_lands_first(tmp_path: Path) -> None:
    """Load older is slow, a delete lands, then the older page arrives: that stale page must be dropped, not appended (S4 L3 r3)."""
    r = Rig(tmp_path)
    _seed_many(r)
    hold = ("(() => { const f = window.fetch; window.__held = null; window.fetch = (u, o) => String(u).includes('before=')"
            " ? new Promise((res) => { window.__held = () => f(u, o).then(res); }) : f(u, o); return 1; })()")
    state = f"JSON.stringify({{ rows: {ROWS}.length }})"
    with _signed(r.src, r.q) as (port, _):
        r.seed(port)
        out = _drive(tmp_path, _url(port), [WIDE, READY, OPEN, {"until": f"{ROWS}.length >= 50 && 1", "ms": 30000}, {"eval": hold},
                                            {"eval": f"{MODAL}.querySelector('.nc-older').click(); 1"},
                                            {"eval": f"{ROWS}[0].querySelector('.nc-main').click(); 1"}, SETTLE,
                                            {"eval": f"{MODAL}.querySelector('.nc-delete').click(); 1"}, SETTLE,
                                            {"eval": "window.__held(); 1"}, SETTLE, {"eval": state}])
    assert _json(out)[-1]["rows"] < 90, out                                  # page one plus the refresh margin (~70); the stale older page would make it 99+


@needs_browser
def test_an_item_at_index_49_stays_open_when_a_new_arrival_shifts_it_past_the_refresh_limit(tmp_path: Path) -> None:
    """Open the 50th row; a new notification arrives before the refresh; the detail must stay (S4 L3 r3)."""
    r = Rig(tmp_path)
    _seed_many(r)
    arrive = ("(() => { const f = window.fetch; window.fetch = (u, o) => !String(u).startsWith('/webpush/center?limit=') ? f(u, o)"
              " : f(u, o).then(async (resp) => { const d = await resp.clone().json(); const n = +(String(u).match(/limit=(\\d+)/) || [0, 50])[1];"
              " d.items = [Object.assign({}, d.items[0], { id: 'p99999', title: 'Fresh' })].concat(d.items).slice(0, n);"
              " return new Response(JSON.stringify(d), { status: 200 }); }); return 1; })()")
    state = f"JSON.stringify({{ title: ({MODAL}.querySelector('.nc-dtitle') || {{}}).textContent || '' }})"
    with _signed(r.src, r.q) as (port, _):
        r.seed(port)
        out = _drive(tmp_path, _url(port), [WIDE, READY, OPEN, {"until": f"{ROWS}.length == 50 && 1", "ms": 30000}, {"eval": arrive},
                                            {"eval": f"{ROWS}[49].querySelector('.nc-main').click(); 1"}, SETTLE, {"eval": state}])
    assert _json(out)[-1]["title"].startswith("Deep item"), out

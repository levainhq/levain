"""The drop card is the one conversation with the coordinator: its thread merges the operator's drops with the rows of the desk-thread
panel (a provider's side of the conversation), newest first, each row a one-line preview that opens to its whole text. The panel is
drawn nowhere else. These drive the real page in headless Chrome over the narrow-layout rig, with the manifest, the desk panel and the
drops read answered by an init script, so the real view mapper and the real renderer both run."""
from __future__ import annotations

import json
from pathlib import Path

from tests.test_cockpit_narrow_layout_rig import NEEDS, PHONE_TOUCH, SETTLE, TOK, FullRig, drive_touch, needs_browser, needs_ssh
from tests.test_msgslot_hero import READY

LONG = "The long reply begins here. " + ("It keeps going with more words so the preview must cut it. " * 9) + "THE VERY END."
DROPS = {"coord_store_read": True, "coord_store_id": "s1", "coord_drops": [
    {"drop_id": "d1", "words": "first drop of mine " + "and on it goes " * 12, "sent_at": "2026-10-30T12:00:00.000000Z", "receipts": []},
    {"drop_id": "d2", "words": "second drop xyz marker\nwith a second line", "sent_at": "2026-10-30T12:30:00.000000Z", "receipts": [{"stage": "landed"}]},
]}
DESK_ROWS = [
    {"id": "r-long", "title": "The long reply begins here.", "body": LONG, "facets": {"at": "2026-10-30T12:10:00Z", "source": "desk", "for": []}},
    {"id": "r-short", "title": "short answer", "body": "short answer", "facets": {"at": "2026-10-30T12:20:00Z", "source": "seat-7", "for": []}},
]
PANEL_ID = "desk-thread"


def _init(rows, *, drops=DROPS, panel=True, panel_error=None) -> str:
    """An init script: the manifest gains the desk panel in a zone of its own, the Work zone, and the widgets; its payload and the
    drops read are answered here."""
    head = {"id": PANEL_ID, "kind": "list", "title": "Desk thread", "priority": "normal", "rank": 5, "status": "ok", "count": len(rows),
            "filtered": [], "skipped": [], "as_of": "2026-10-31T00:00:00+00:00", "stale_after_s": 0, "refresh_every_s": None, "etag": "e-desk",
            "error": None, "note": "", "empty": "no messages", "groups": [], "facets": ["for"], "actions": []}
    body = {**head, "rows": rows, "degraded": [], "held": []}
    if panel_error:
        head = {**head, "status": "error", "error": panel_error}
        body = {**head, "rows": []}
    return ("(() => { const f = window.fetch; const J = (o) => new Response(JSON.stringify(o), {status: 200, headers: {'Content-Type': 'application/json'}});"
            f" const PID = {json.dumps(PANEL_ID)}, HEAD = {json.dumps(head)}, BODY = {json.dumps(body)}, DROPS = {json.dumps(drops)}, PANEL = {json.dumps(panel)};"
            " window.fetch = function (u, o) { const url = typeof u === 'string' ? u : (u && u.url) || '';"
            " if (url.indexOf('/cockpit/coord/drops.json') >= 0) return Promise.resolve(J(DROPS));"
            " if (PANEL && url.indexOf('/cockpit/panel/' + PID + '.json') >= 0) return Promise.resolve(J(BODY));"
            " if (PANEL && url.indexOf('/cockpit/manifest.json') >= 0) return f.apply(this, arguments).then((r) => r.json()).then((m) => {"
            "  m.panels[PID] = HEAD; const R = m.regions; R.widgets.push(PID); R.header.push(PID);"
            "  const op = R.zones.find((z) => z.id === 'operate'); op.panels.push(PID); op.cards.push({panels: [PID], size: 'medium', span: 1, tabs: null});"
            "  R.zones.push({id: 'deskonly', title: 'Desk only', grid: 3, panels: [PID], cards: [{panels: [PID], size: 'medium', span: 1, tabs: null}]});"
            "  return J(m); });"
            " return f.apply(this, arguments); }; })();")


def _thread() -> str:
    return ("JSON.stringify({t: 'thread', rows: [...document.querySelectorAll('#ck-drop .drop-recent-row')].map(r => ({key: r.dataset.dropKey,"
            " who: r.querySelector('.drop-who').textContent, preview: r.querySelector('.drop-words-first').textContent,"
            " open: r.querySelector('.drop-row-head').getAttribute('aria-expanded'), full: (r.querySelector('.drop-full') || {}).textContent || null,"
            " mine: r.classList.contains('drop-mine')})), fault: (document.querySelector('#ck-drop .drop-fault:not([hidden])') || {}).textContent || null,"
            " filterShown: !document.querySelector('#ck-drop .drop-filter').hidden,"
            " over: document.documentElement.scrollWidth > document.documentElement.clientWidth,"
            " panelNodes: document.querySelectorAll('[data-ck-panel=\"desk-thread\"]').length,"
            " outside: [...document.querySelectorAll('body *')].filter(n => !n.closest('#ck-drop') && !n.querySelector('*') && /seat-7|THE VERY END|short answer/.test(n.textContent) && n.tagName !== 'SCRIPT').length,"
            " zones: [...document.querySelectorAll('.ck-zone')].map(z => z.dataset.zone), composeBoxes: document.querySelectorAll('.capture-box, [data-capture-box]').length})")


def _click(key: str) -> dict:
    return {"eval": f"(() => {{ document.querySelector('#ck-drop .drop-row-head[data-drop-key=\"{key}\"]').click(); return 1; }})()"}


def _boot(init: str, extra: list, *, w: int = 1280, touch: bool = False) -> list:
    base = list(PHONE_TOUCH) if touch else [{"emulate": {"w": w, "h": 900, "dpr": 1}}]
    return base + [{"init": init}, {"until": NEEDS, "ms": 30000}, {"until": READY, "ms": 30000}, SETTLE, *extra]


def _run(tmp_path: Path, monkeypatch, steps: list) -> dict:
    rig = FullRig(tmp_path, monkeypatch)
    with rig.serving():
        return drive_touch(tmp_path, f"{rig.origin}/#write_token={TOK}", steps)       # the touch driver is the one that answers {"init": ...}


def _all(out: dict) -> list:
    return [json.loads(v) for v in out["values"] if isinstance(v, str) and v.startswith("{") and '"t":"thread"' in v]


@needs_browser
@needs_ssh
def test_the_thread_merges_both_sides_expands_collapses_and_filters(tmp_path: Path, monkeypatch) -> None:
    key = "desk:r-long"
    steps = _boot(_init(DESK_ROWS), [{"eval": _thread()}, _click(key), {"eval": _thread()}, _click(key), {"eval": _thread()},
                                      {"eval": "(() => { const q = document.querySelector('#ck-drop .drop-filter'); q.value = 'xyz'; q.dispatchEvent(new Event('input', {bubbles: true})); return 1; })()"},
                                      {"eval": _thread()}])
    first, opened, closed, filtered = _all(_run(tmp_path, monkeypatch, steps))
    assert [r["key"] for r in first["rows"]] == ["drop:d2", "desk:r-short", "desk:r-long", "drop:d1"], first["rows"]     # one list, newest first
    assert [r["who"] for r in first["rows"]] == ["you", "seat-7", "desk", "you"] and [r["mine"] for r in first["rows"]] == [True, False, False, True]
    cut = first["rows"][3]                                                                                                # his long single-line drop: cut to a fixed length
    assert cut["open"] == "false" and cut["full"] is None and len(cut["preview"]) <= 100 and cut["preview"].endswith("…"), cut
    assert first["rows"][2]["preview"] == "The long reply begins here."                                                  # a desk row's preview is its title
    assert first["rows"][0]["preview"] == "second drop xyz marker"                                                      # one line of his own words
    assert first["filterShown"] and first["fault"] is None
    assert first["panelNodes"] == 0 and first["outside"] == 0 and "deskonly" not in first["zones"], first   # the panel is drawn nowhere else, and its zone draws no empty card
    assert opened["rows"][2]["open"] == "true" and opened["rows"][2]["full"] == LONG, opened["rows"][2]
    assert closed["rows"][2]["open"] == "false" and closed["rows"][2]["full"] is None
    assert [r["key"] for r in filtered["rows"]] == ["drop:d2"], filtered["rows"]


@needs_browser
@needs_ssh
def test_an_expanded_row_stays_open_across_a_refresh_redraw(tmp_path: Path, monkeypatch) -> None:
    key = "desk:r-long"
    steps = _boot(_init(DESK_ROWS), [_click(key), {"eval": "document.dispatchEvent(new Event('visibilitychange')); 1"}, SETTLE, {"eval": _thread()}])
    (state,) = _all(_run(tmp_path, monkeypatch, steps))
    row = next(r for r in state["rows"] if r["key"] == key)
    assert row["open"] == "true" and row["full"] == LONG, row


@needs_browser
@needs_ssh
def test_a_desk_panel_that_failed_leaves_his_drops_and_one_plain_line(tmp_path: Path, monkeypatch) -> None:
    steps = _boot(_init([], panel_error="the provider timed out"), [{"eval": _thread()}])
    (state,) = _all(_run(tmp_path, monkeypatch, steps))
    assert [r["key"] for r in state["rows"]] == ["drop:d2", "drop:d1"], state["rows"]
    assert state["fault"].startswith("The desk's replies could not be read: ") and "provider timed out" in state["fault"], state["fault"]
    assert state["panelNodes"] == 0


@needs_browser
@needs_ssh
def test_without_a_desk_panel_his_drops_show_and_no_error_line(tmp_path: Path, monkeypatch) -> None:
    steps = _boot(_init([], panel=False), [{"eval": _thread()}])
    (state,) = _all(_run(tmp_path, monkeypatch, steps))
    assert [r["key"] for r in state["rows"]] == ["drop:d2", "drop:d1"] and state["fault"] is None, state


@needs_browser
@needs_ssh
def test_a_row_with_no_readable_instant_sorts_oldest_and_a_bad_row_is_skipped(tmp_path: Path, monkeypatch) -> None:
    rows = [{"id": "r-nodate", "title": "undated", "body": "undated", "facets": {"at": "not a time", "source": "desk"}},
            {"id": "r-ok", "title": "dated", "body": "dated", "facets": {"at": "2026-10-30T11:00:00Z", "source": "desk"}}]
    steps = _boot(_init(rows), [{"eval": _thread()}])
    (state,) = _all(_run(tmp_path, monkeypatch, steps))
    assert [r["key"] for r in state["rows"]] == ["drop:d2", "drop:d1", "desk:r-ok", "desk:r-nodate"], state["rows"]


@needs_browser
@needs_ssh
def test_at_390_an_expanded_long_row_wraps_without_a_sideways_scroll(tmp_path: Path, monkeypatch) -> None:
    key = "desk:r-long"
    steps = _boot(_init(DESK_ROWS), [_click(key), SETTLE, {"eval": _thread()}], touch=True)
    (state,) = _all(_run(tmp_path, monkeypatch, steps)) 
    assert next(r for r in state["rows"] if r["key"] == key)["full"] == LONG
    assert state["over"] is False, state

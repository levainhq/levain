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


def _init(rows, *, drops=DROPS, panel=True, panel_error=None, ke=True) -> str:
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
            f" const PID = {json.dumps(PANEL_ID)}, HEAD = {json.dumps(head)}, PANEL = {json.dumps(panel)}; window.__body = {json.dumps(body)}; window.__drops = {json.dumps(drops)};"
            " window.fetch = function (u, o) { const url = typeof u === 'string' ? u : (u && u.url) || '';"
            " if (url.indexOf('/cockpit/coord/drops.json') >= 0) return Promise.resolve(window.__fail ? new Response('{}', {status: window.__fail === true ? 500 : window.__fail}) : (window.__hold ? new Promise((res) => { window.__release = () => res(J(window.__drops)); }) : J(window.__drops)));"
            " if (PANEL && url.indexOf('/cockpit/panel/' + PID + '.json') >= 0) return Promise.resolve(J(window.__body));"
            " if (PANEL && url.indexOf('/cockpit/manifest.json') >= 0) return f.apply(this, arguments).then((r) => r.json()).then((m) => {"
            "  m.panels[PID] = HEAD; const R = m.regions; R.widgets.push(PID); R.header.push(PID);"
            "  const op = R.zones.find((z) => z.id === 'operate'); op.panels.push(PID); op.cards.push({panels: [PID], size: 'medium', span: 1, tabs: null});"
            "  R.zones.push({id: 'deskonly', title: 'Desk only', grid: 3, panels: [PID], cards: [{panels: [PID], size: 'medium', span: 1, tabs: null}]});"
            f"  if (!{json.dumps(ke)}) m.regions.hero = null;"
            "  return J(m); });"
            " return f.apply(this, arguments); }; })();")


def _thread() -> str:
    return ("JSON.stringify({t: 'thread', rows: [...document.querySelectorAll('#ck-drop .drop-recent-row')].map(r => ({key: r.dataset.dropKey,"
            " who: r.querySelector('.drop-who').textContent, preview: r.querySelector('.drop-words-first').textContent,"
            " open: r.querySelector('.drop-row-head').getAttribute('aria-expanded'), full: (r.querySelector('.drop-full:not([hidden]) .drop-full-text') || {}).textContent || null,"
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


# a refresh that really reads again: the page's clock jumps past the read's hold, then the page is shown again
REDRAW = {"eval": "(() => { const n = Date.now; Date.now = () => n() + 120000; document.dispatchEvent(new Event('visibilitychange')); return 1; })()"}
SETTLE2 = SETTLE


def _more_drop(did: str, words: str, at: str) -> str:
    """Puts one more drop in the next drops read, so the thread's rows really change (a redraw that changes nothing rebuilds nothing)."""
    return ("(() => { window.__drops = Object.assign({}, window.__drops, {coord_drops: window.__drops.coord_drops.concat([{drop_id: %s, words: %s, sent_at: %s, receipts: []}])}); return 1; })()"
            % (json.dumps(did), json.dumps(words), json.dumps(at)))


@needs_browser
@needs_ssh
def test_an_expanded_row_stays_open_across_a_redraw_that_rebuilds_the_rows(tmp_path: Path, monkeypatch) -> None:
    key = "desk:r-long"
    steps = _boot(_init(DESK_ROWS), [_click(key), {"eval": "window.__row = document.querySelector('#ck-drop .drop-recent-row[data-drop-key=\"desk:r-long\"]'); 1"},
                                      {"eval": _more_drop("d3", "a newer drop", "2026-10-30T13:00:00.000000Z")}, REDRAW, SETTLE2,
                                      {"eval": "JSON.stringify({t: 'thread', rebuilt: window.__row !== document.querySelector('#ck-drop .drop-recent-row[data-drop-key=\"desk:r-long\"]'), keys: [...document.querySelectorAll('#ck-drop .drop-recent-row')].map(r => r.dataset.dropKey)})"},
                                      {"eval": _thread()}])
    mid, state = _all(_run(tmp_path, monkeypatch, steps))
    assert mid["rebuilt"] is True and mid["keys"][0] == "drop:d3", mid        # the rows really were redrawn: the new drop is the newest
    row = next(r for r in state["rows"] if r["key"] == key)
    assert row["open"] == "true" and row["full"] == LONG, row


@needs_browser
@needs_ssh
def test_words_typed_and_focus_in_the_composer_and_the_filter_survive_a_redraw(tmp_path: Path, monkeypatch) -> None:
    probe = ("JSON.stringify({t: 'thread', words: document.querySelector('#ck-drop-words').value, rows: [], active: document.activeElement.className || document.activeElement.id,"
             " filter: document.querySelector('#ck-drop .drop-filter').value, keys: [...document.querySelectorAll('#ck-drop .drop-recent-row')].map(r => r.dataset.dropKey)})")
    typed = {"eval": "(() => { const w = document.querySelector('#ck-drop-words'); w.value = 'half a thought'; w.dispatchEvent(new Event('input', {bubbles: true})); w.focus(); return 1; })()"}
    filt = {"eval": "(() => { const q = document.querySelector('#ck-drop .drop-filter'); q.focus(); q.value = 'drop'; q.dispatchEvent(new Event('input', {bubbles: true})); return 1; })()"}
    steps = _boot(_init(DESK_ROWS), [typed, {"eval": _more_drop("d3", "a newer drop", "2026-10-30T13:00:00.000000Z")}, REDRAW, SETTLE2, {"eval": probe},
                                      filt, {"eval": _more_drop("d4", "yet another drop", "2026-10-30T13:30:00.000000Z")}, {"eval": "Date.now = ((n) => () => n() + 120000)(Date.now); 1"},
                                      {"eval": "document.dispatchEvent(new Event('visibilitychange')); 1"}, SETTLE2, {"eval": probe}])
    composer, filter = _all(_run(tmp_path, monkeypatch, steps))
    assert composer["words"] == "half a thought" and composer["active"] == "drop-words" and composer["keys"][0] == "drop:d3", composer
    assert filter["filter"] == "drop" and filter["active"] == "drop-filter" and filter["keys"][0] == "drop:d4", filter
    assert all(k.startswith("drop:") for k in filter["keys"]), filter


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
def test_a_row_with_no_readable_instant_sorts_oldest_and_a_malformed_row_is_skipped(tmp_path: Path, monkeypatch) -> None:
    rows = [{"id": "r-nodate", "title": "undated", "body": "undated", "facets": {"at": "not a time", "source": "desk"}},
            {"id": "r-nofacets", "title": "bare", "body": "bare"},
            {"id": "r-ok", "title": "dated", "body": "dated", "facets": {"at": "2026-10-30T11:00:00Z", "source": "desk"}}]
    drops = {**DROPS, "coord_drops": [None, 7, *DROPS["coord_drops"]]}
    steps = _boot(_init(rows, drops=drops), [{"eval": _thread()}])
    (state,) = _all(_run(tmp_path, monkeypatch, steps))
    assert [r["key"] for r in state["rows"]] == ["drop:d2", "drop:d1", "desk:r-ok", "desk:r-nodate", "desk:r-nofacets"], state["rows"]
    assert next(r for r in state["rows"] if r["key"] == "desk:r-nofacets")["who"] == "desk"


@needs_browser
@needs_ssh
def test_a_desk_row_with_no_id_keeps_its_open_state_when_newer_rows_arrive(tmp_path: Path, monkeypatch) -> None:
    rows = [{"title": "no id one", "body": "no id one", "facets": {"at": "2026-10-30T10:00:00Z", "source": "desk"}}]
    steps = _boot(_init(rows), [{"eval": _thread()}])
    first = _all(_run(tmp_path / "a", monkeypatch, steps))[0]
    key = first["rows"][-1]["key"]
    assert key.startswith("desk:h"), first["rows"]        # a content key, not a position
    steps = _boot(_init(rows), [_click(key), {"eval": _more_drop("d3", "newer", "2026-10-30T13:00:00.000000Z")}, REDRAW, SETTLE2, {"eval": _thread()}])
    (state,) = _all(_run(tmp_path / "b", monkeypatch, steps))
    row = next(r for r in state["rows"] if r["key"] == key)
    assert state["rows"][0]["key"] == "drop:d3" and row["open"] == "true", state["rows"]


@needs_browser
@needs_ssh
def test_a_row_whose_source_starts_with_phill_is_his_and_a_drop_with_other_words_shows_too(tmp_path: Path, monkeypatch) -> None:
    rows = [{"id": "r-a", "title": "from the cockpit", "body": "from the cockpit", "facets": {"at": "2026-10-30T12:40:00Z", "source": "phill (cockpit, signed)"}},
            {"id": "r-b", "title": "from the phone", "body": "from the phone", "facets": {"at": "2026-10-30T12:50:00Z", "source": "  PHILLIP  "}},
            {"id": "r-c", "title": "a seat", "body": "a seat", "facets": {"at": "2026-10-30T12:55:00Z", "source": "seat-7"}}]
    steps = _boot(_init(rows), [{"eval": _thread()}])
    (state,) = _all(_run(tmp_path, monkeypatch, steps))
    by = {r["key"]: r for r in state["rows"]}
    for k in ("desk:r-a", "desk:r-b"):
        assert by[k]["mine"] is True and by[k]["who"] == "you", by[k]
    assert by["desk:r-c"]["mine"] is False and by["desk:r-c"]["who"] == "seat-7"
    assert "drop:d1" in by and "drop:d2" in by and len(state["rows"]) == 5       # no dedupe against his drops: they carry other words


@needs_browser
@needs_ssh
def test_a_failed_drops_read_keeps_the_last_good_rows_and_says_it_failed(tmp_path: Path, monkeypatch) -> None:
    note = "JSON.stringify({t: 'thread', rows: [], note: document.querySelector('#ck-drop .drop-note').textContent, keys: [...document.querySelectorAll('#ck-drop .drop-recent-row')].map(r => r.dataset.dropKey)})"
    steps = _boot(_init(DESK_ROWS), [{"eval": "window.__fail = true; 1"}, REDRAW, SETTLE2, {"eval": note}])
    (state,) = _all(_run(tmp_path, monkeypatch, steps))
    assert state["keys"] == ["drop:d2", "desk:r-short", "desk:r-long", "drop:d1"], state
    assert "could not be" in state["note"] and "last read" in state["note"], state


@needs_browser
@needs_ssh
def test_a_row_names_its_panel_with_separators_and_shows_its_text_once(tmp_path: Path, monkeypatch) -> None:
    probe = ("JSON.stringify({t: 'thread', rows: [], a: (() => { const r = document.querySelector('#ck-drop .drop-recent-row[data-drop-key=\"desk:r-long\"]');"
             " const b = r.querySelector('.drop-row-head'), p = document.getElementById(b.getAttribute('aria-controls'));"
             " return {label: b.getAttribute('aria-label'), panel: !!p, hidden: p && p.hidden, previewHidden: r.querySelector('.drop-words-first').hidden,"
             " sent: (p.querySelector('.drop-sent') || {}).textContent, sub: document.querySelector('#ck-drop .drop-sub').textContent,"
             " head: document.querySelector('#ck-drop h3').textContent}; })()})")
    steps = _boot(_init(DESK_ROWS), [{"eval": probe}, _click("desk:r-long"), {"eval": probe}])
    closed, opened = [x["a"] for x in _all(_run(tmp_path, monkeypatch, steps))]
    assert closed["panel"] and closed["hidden"] is True and closed["previewHidden"] is False, closed
    assert closed["label"].startswith("desk, ") and closed["label"].endswith(": The long reply begins here."), closed
    assert opened["hidden"] is False and opened["previewHidden"] is True, opened
    assert opened["sent"].startswith("sent ") and "2026-10-30" in opened["sent"], opened
    assert closed["head"] == "Drop to the coordinator" and closed["sub"] == "Your drops and the coordinator's replies", closed


def _many(n: int) -> list:
    return [{"id": f"m{i:02d}", "title": f"reply number {i}", "body": f"reply number {i}", "facets": {"at": f"2026-10-30T{10 + i // 6:02d}:{(i % 6) * 10:02d}:00Z", "source": "desk"}} for i in range(n)]


@needs_browser
@needs_ssh
def test_show_more_keeps_its_focus_and_a_row_folded_away_hands_its_focus_to_it(tmp_path: Path, monkeypatch) -> None:
    probe = ("JSON.stringify({t: 'thread', rows: [], onMore: document.activeElement === window.__m, text: window.__m.textContent, sameNode: window.__m === document.querySelector('#ck-drop .drop-more'),"
             " n: document.querySelectorAll('#ck-drop .drop-recent-row').length})")
    steps = _boot(_init(_many(12)), [{"eval": "window.__m = document.querySelector('#ck-drop .drop-more'); window.__m.focus(); window.__m.click(); 1"}, {"eval": probe},
                                      {"eval": "document.querySelectorAll('#ck-drop .drop-row-head')[11].focus(); window.__m.click(); 1"}, {"eval": probe}])
    more, fewer = _all(_run(tmp_path, monkeypatch, steps))
    assert more["onMore"] and more["text"] == "show fewer" and more["sameNode"] and more["n"] == 14, more
    assert fewer["onMore"] and fewer["n"] == 8 and fewer["text"].startswith("show "), fewer


@needs_browser
@needs_ssh
def test_a_new_coordinator_reply_is_announced_once_and_nothing_else_is(tmp_path: Path, monkeypatch) -> None:
    live = "JSON.stringify({t: 'thread', rows: [], live: document.querySelector('#ck-drop .drop-live').textContent, role: document.querySelector('#ck-drop .drop-live').getAttribute('aria-live')})"
    add = lambda rid, src: {"eval": ("window.__body.rows.push({id: %s, title: 'new', body: 'new', facets: {at: '2026-10-31T09:00:00Z', source: %s}}); window.__body.count += 1; 1" % (json.dumps(rid), json.dumps(src)))}
    steps = _boot(_init(DESK_ROWS), [{"eval": live}, REDRAW, SETTLE2, {"eval": live},               # a refresh with nothing new
                                      add("r-mine", "phill (cockpit)"), REDRAW, SETTLE2, {"eval": live},       # his own row
                                      add("r-new", "desk"), REDRAW, SETTLE2, {"eval": live}])
    first, quiet, mine, new = [x["live"] for x in _all(_run(tmp_path, monkeypatch, steps))]
    assert (first, quiet, mine) == ("", "", ""), (first, quiet, mine)
    assert new == "The coordinator replied.", new


@needs_browser
@needs_ssh
def test_the_list_is_a_scroll_container_so_show_more_does_not_grow_the_page(tmp_path: Path, monkeypatch) -> None:
    probe = ("JSON.stringify({t: 'thread', rows: [], oy: getComputedStyle(document.querySelector('#ck-drop .drop-list')).overflowY, list: document.querySelector('#ck-drop .drop-list').clientHeight,"
             " vh: innerHeight, doc: document.documentElement.scrollHeight, scrolls: document.querySelector('#ck-drop .drop-list').scrollHeight > document.querySelector('#ck-drop .drop-list').clientHeight})")
    expand = {"eval": "document.querySelector('#ck-drop .drop-more').click(); 1"}
    for kw in ({"w": 1280}, {"touch": True}):
        steps = _boot(_init(_many(40)), [{"eval": probe}, expand, SETTLE, {"eval": probe}], **kw)
        before, after = _all(_run(tmp_path / ("touch" if kw.get("touch") else "wide"), monkeypatch, steps))
        assert before["oy"] == "auto" and after["oy"] == "auto", (kw, before)
        assert after["list"] <= after["vh"] * 0.6 + 1 and after["scrolls"] is True, (kw, after)
        assert after["doc"] - before["doc"] <= after["list"] - before["list"] + 2, (kw, before, after)       # the page grows no more than the list's own capped growth


@needs_browser
@needs_ssh
def test_the_time_names_today_yesterday_or_a_weekday_and_the_expanded_row_gives_the_full_instant(tmp_path: Path, monkeypatch) -> None:
    # the page's clock is pinned, so "today" and "yesterday" are the same whatever the real date and zone
    pin = "(() => { const T = Date.parse('2026-10-30T12:00:00Z'); Date.now = () => T; })();"
    rows = [{"id": "r-y", "title": "y", "body": "y", "facets": {"at": "2026-10-29T12:00:00Z", "source": "desk"}},
            {"id": "r-o", "title": "o", "body": "o", "facets": {"at": "2026-10-24T12:00:00Z", "source": "desk"}}]
    probe = "JSON.stringify({t: 'thread', rows: [], when: [...document.querySelectorAll('#ck-drop .drop-recent-row')].map(r => [r.dataset.dropKey, r.querySelector('.drop-when').textContent])})"
    sent = "JSON.stringify({t: 'thread', rows: [], sent: document.querySelector('#ck-drop .drop-recent-row[data-drop-key=\"desk:r-o\"] .drop-sent').textContent})"
    steps = _boot(pin + _init(rows), [{"eval": probe}, _click("desk:r-o"), {"eval": sent}])
    got, opened = _all(_run(tmp_path, monkeypatch, steps))
    when = dict((k, v) for k, v in got["when"])
    assert when["drop:d2"].startswith("today ") and when["desk:r-y"].startswith("yesterday "), when
    assert when["desk:r-o"].startswith(("Fri ", "Sat ")) and "10-2" in when["desk:r-o"], when       # six days back names its weekday and date
    assert opened["sent"].startswith("sent ") and opened["sent"].count(":") == 2 and "2026-10-2" in opened["sent"], opened


@needs_browser
@needs_ssh
def test_at_390_an_expanded_long_row_wraps_without_a_sideways_scroll(tmp_path: Path, monkeypatch) -> None:
    key = "desk:r-long"
    steps = _boot(_init(DESK_ROWS), [_click(key), SETTLE, {"eval": _thread()}], touch=True)
    (state,) = _all(_run(tmp_path, monkeypatch, steps)) 
    assert next(r for r in state["rows"] if r["key"] == key)["full"] == LONG
    assert state["over"] is False, state


@needs_browser
@needs_ssh
def test_at_390_a_long_unbroken_source_name_wraps_without_a_sideways_scroll(tmp_path: Path, monkeypatch) -> None:
    rows = [{"id": "r-w", "title": "wide", "body": "wide", "facets": {"at": "2026-10-30T12:20:00Z", "source": "W" * 40}}]
    steps = _boot(_init(rows), [SETTLE, {"eval": _thread()}], touch=True)
    (state,) = _all(_run(tmp_path, monkeypatch, steps))
    assert next(r for r in state["rows"] if r["key"] == "desk:r-w")["who"] == "W" * 40
    assert state["over"] is False, state


def _keys() -> str:
    return ("JSON.stringify({t: 'thread', rows: [...document.querySelectorAll('#ck-drop .drop-recent-row')].map(r => ({key: r.dataset.dropKey, id: r.dataset.dropId || null,"
            " open: r.querySelector('.drop-row-head').getAttribute('aria-expanded'), when: r.querySelector('.drop-when').textContent})),"
            " note: document.querySelector('#ck-drop .drop-note').textContent, older: !(document.querySelector('#ck-drop .drop-older-note') || {hidden: true}).hidden,"
            " filterShown: !document.querySelector('#ck-drop .drop-filter').hidden, filterValue: document.querySelector('#ck-drop .drop-filter').value,"
            " none: !!document.querySelector('#ck-drop .drop-none'), live: document.querySelector('#ck-drop .drop-live').textContent,"
            " panelNodes: document.querySelectorAll('[data-ck-panel=\"desk-thread\"]').length})")


def _set_q(q: str) -> dict:
    return {"eval": f"(() => {{ const q = document.querySelector('#ck-drop .drop-filter'); q.value = {json.dumps(q)}; q.dispatchEvent(new Event('input', {{bubbles: true}})); return 1; }})()"}


@needs_browser
@needs_ssh
def test_a_refused_token_clears_the_thread_but_a_transient_failure_keeps_it(tmp_path: Path, monkeypatch) -> None:
    steps = _boot(_init(DESK_ROWS), [{"eval": _keys()},
                                      {"eval": "window.__fail = 500; 1"}, REDRAW, SETTLE2, {"eval": _keys()},          # a server fault: the last read stays
                                      {"eval": "window.__fail = 403; 1"}, REDRAW, SETTLE2, {"eval": _keys()}])        # a refused token: nothing stays
    first, transient, refused = _all(_run(tmp_path / "a", monkeypatch, steps))
    assert len(first["rows"]) == 4 and len(transient["rows"]) == 4 and "last read" in transient["note"], transient
    assert refused["rows"] == [] and "last read" not in refused["note"] and "Sign in" in refused["note"], refused
    gone = "(() => { window.LevainCockpitTransport.headers = () => ({}); return 1; })()"
    steps = _boot(_init(DESK_ROWS), [{"eval": gone}, REDRAW, SETTLE2, {"eval": _keys()}])        # no token at all
    (nothing,) = _all(_run(tmp_path / "b", monkeypatch, steps))
    assert nothing["rows"] == [] and "no cockpit token" in nothing["note"], nothing


@needs_browser
@needs_ssh
def test_the_card_says_when_older_drops_are_not_in_the_read(tmp_path: Path, monkeypatch) -> None:
    for name, drops, want in (("more", {**DROPS, "coord_drops_more": True}, True), ("all", DROPS, False)):
        steps = _boot(_init(DESK_ROWS, drops=drops), [{"eval": _keys()}])
        (got,) = _all(_run(tmp_path / name, monkeypatch, steps))
        assert got["older"] is want, (name, got)


@needs_browser
@needs_ssh
def test_a_manifest_that_is_not_the_ke_page_still_draws_the_desk_thread_panel(tmp_path: Path, monkeypatch) -> None:
    # no KE regions, so no Needs row and no drop card to wait for: the page is settled once the page has drawn its panels
    steps = [{"emulate": {"w": 1280, "h": 900, "dpr": 1}}, {"init": _init(DESK_ROWS, ke=False)}, {"until": "document.querySelectorAll('.panel').length > 0", "ms": 30000}, SETTLE,
             {"eval": "JSON.stringify({t: 'thread', panelNodes: document.querySelectorAll('[data-panel-id=\"desk-thread\"]').length, ke: document.body.classList.contains('ke')})"}]
    (got,) = _all(_run(tmp_path, monkeypatch, steps))
    assert got["ke"] is False and got["panelNodes"] >= 1, got


@needs_browser
@needs_ssh
def test_two_identical_replies_keep_their_keys_and_open_state_when_another_row_arrives(tmp_path: Path, monkeypatch) -> None:
    twin = {"title": "same words", "body": "same words", "facets": {"at": "2026-10-30T10:00:00Z", "source": "desk"}}
    drops = {**DROPS, "coord_drops": [*DROPS["coord_drops"], {"drop_id": "dup", "words": "dup one", "sent_at": "2026-10-30T09:00:00.000000Z", "receipts": []},
                                       {"drop_id": "dup", "words": "dup two", "sent_at": "2026-10-30T09:01:00.000000Z", "receipts": []}]}
    steps = _boot(_init([twin, dict(twin)], drops=drops), [{"eval": _keys()}])
    (first,) = _all(_run(tmp_path / "a", monkeypatch, steps))
    twins = [r["key"] for r in first["rows"] if r["key"].startswith("desk:")]
    assert len(set(twins)) == 2 and twins[1] == twins[0] + "#2", first["rows"]
    assert [r["id"] for r in first["rows"] if r["key"].startswith("drop:dup")] == ["dup", "dup"], first["rows"]       # the real id, never a suffixed one
    steps = _boot(_init([twin, dict(twin)], drops=drops), [_click(twins[1]), {"eval": _more_drop("d3", "newer", "2026-10-30T13:00:00.000000Z")}, REDRAW, SETTLE2, {"eval": _keys()}])
    (after,) = _all(_run(tmp_path / "b", monkeypatch, steps))
    row = next(r for r in after["rows"] if r["key"] == twins[1])
    assert row["open"] == "true" and after["rows"][0]["key"] == "drop:d3" and after["live"] == "", after


@needs_browser
@needs_ssh
def test_a_filter_cannot_stay_set_while_its_box_is_hidden(tmp_path: Path, monkeypatch) -> None:
    one = {**DROPS, "coord_drops": DROPS["coord_drops"][:1]}
    steps = _boot(_init(DESK_ROWS), [_set_q("zzz-no-match"), {"eval": _keys()},
                                      {"eval": f"window.__drops = {json.dumps(one)}; window.__body.rows = []; 1"}, REDRAW, SETTLE2, {"eval": _keys()}])
    set_, after = _all(_run(tmp_path, monkeypatch, steps))
    assert set_["filterShown"] and set_["none"] and set_["rows"] == [], set_
    assert not after["filterShown"] and after["filterValue"] == "" and [r["key"] for r in after["rows"]] == ["drop:d1"] and not after["none"], after


@needs_browser
@needs_ssh
def test_a_rebuild_keeps_the_lists_scroll_position(tmp_path: Path, monkeypatch) -> None:
    probe = "JSON.stringify({t: 'thread', rows: [], top: document.querySelector('#ck-drop .drop-list').scrollTop})"
    add = {"eval": "window.__body.rows.push({id: 'r-late', title: 'late', body: 'late', facets: {at: '2026-10-31T09:00:00Z', source: 'desk'}}); window.__body.count += 1; 1"}
    steps = _boot(_init(_many(40)), [{"eval": "document.querySelector('#ck-drop .drop-more').click(); document.querySelector('#ck-drop .drop-list').scrollTop = 240; 1"}, {"eval": probe},
                                      add, REDRAW, SETTLE2, {"eval": probe}, {"eval": _keys()}])
    before, after, keys = _all(_run(tmp_path, monkeypatch, steps))
    assert before["top"] > 100 and keys["rows"][0]["key"] == "desk:r-late", (before, keys["rows"][:1])       # the rows really were rebuilt
    assert after["top"] == before["top"], (before, after)


@needs_browser
@needs_ssh
def test_the_filter_finds_a_row_by_its_receipt_stage_and_its_time_words(tmp_path: Path, monkeypatch) -> None:
    steps = _boot(_init(DESK_ROWS), [_set_q("landed"), {"eval": _keys()}])
    (got,) = _all(_run(tmp_path, monkeypatch, steps))
    assert [r["key"] for r in got["rows"]] == ["drop:d2"], got


@needs_browser
@needs_ssh
def test_today_becomes_yesterday_after_midnight_without_any_new_data(tmp_path: Path, monkeypatch) -> None:
    now = {"eval": ("window.__drops = Object.assign({}, window.__drops, {coord_drops: window.__drops.coord_drops.concat([{drop_id: 'dnow', words: 'just now', "
                    "sent_at: new Date().toISOString(), receipts: []}])}); 1")}
    day = {"eval": "(() => { const n = Date.now; Date.now = () => n() + 86400000; return 1; })()"}
    steps = _boot(_init(DESK_ROWS), [now, REDRAW, SETTLE2, {"eval": _keys()}, day, REDRAW, SETTLE2, {"eval": _keys()}])
    before, after = _all(_run(tmp_path, monkeypatch, steps))
    pick = lambda g: next(r["when"] for r in g["rows"] if r["key"] == "drop:dnow")
    assert pick(before).startswith("today") and pick(after).startswith("yesterday"), (pick(before), pick(after))


@needs_browser
@needs_ssh
def test_a_desk_row_with_no_text_is_skipped_and_the_others_still_show(tmp_path: Path, monkeypatch) -> None:
    blank = {"id": "r-blank", "title": "", "body": "", "facets": {"at": "2026-10-30T12:40:00Z", "source": "desk"}}
    steps = _boot(_init([*DESK_ROWS, blank]), [{"eval": _keys()}])
    (got,) = _all(_run(tmp_path, monkeypatch, steps))
    assert [r["key"] for r in got["rows"]] == ["drop:d2", "desk:r-short", "desk:r-long", "drop:d1"], got


def test_the_sheet_scroll_cap_is_scoped_to_the_page_it_belongs_to_and_the_announcement_timer_is_single() -> None:
    root = Path(__file__).resolve().parent.parent / "levain" / "templates" / "web"
    css = (root / "dashboard.css").read_text()
    lines = [ln for ln in css.splitlines() if "#drop-recent" in ln]
    assert lines and all(ln.startswith("body.ke #drop-recent") for ln in lines), lines
    media = css[css.index("@media (max-width: 720px) {\n  body.ke .ck-drop.frame"):]
    assert "#drop-recent" not in media.split("}\n", 3)[0]      # the phone query does not restate it
    core = (root / "dashboard_core.js").read_text()
    assert "clearTimeout(ui.liveTid)" in core


@needs_browser
@needs_ssh
def test_an_authorized_read_in_flight_cannot_repopulate_rows_after_the_token_is_removed(tmp_path: Path, monkeypatch) -> None:
    gone = "(() => { window.LevainCockpitTransport.headers = () => ({}); return 1; })()"
    steps = _boot(_init(DESK_ROWS), [{"eval": "window.__hold = true; 1"}, REDRAW, SETTLE2, {"eval": gone}, REDRAW, SETTLE2, {"eval": _keys()},
                                      {"eval": "window.__hold = false; window.__release(); 1"}, SETTLE2, {"eval": _keys()}])
    cleared, after = _all(_run(tmp_path, monkeypatch, steps))
    assert cleared["rows"] == [] and "no cockpit token" in cleared["note"], cleared
    assert after["rows"] == [] and "no cockpit token" in after["note"], after       # the late answer is discarded


@needs_browser
@needs_ssh
def test_a_real_key_that_looks_like_a_numbered_duplicate_never_shares_a_key(tmp_path: Path, monkeypatch) -> None:
    def row(i: str, t: str) -> dict:
        return {"id": i, "title": t, "body": t, "facets": {"at": "2026-10-30T10:00:00Z", "source": "desk"}}
    steps = _boot(_init([row("x", "one"), row("x", "two"), row("x#2", "three")]), [{"eval": _keys()}])
    (got,) = _all(_run(tmp_path, monkeypatch, steps))
    keys = [r["key"] for r in got["rows"] if r["key"].startswith("desk:")]
    assert len(keys) == 3 and len(set(keys)) == 3, keys
    assert "desk:x" in keys and "desk:x#2" in keys, keys

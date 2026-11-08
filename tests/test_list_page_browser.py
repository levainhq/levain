"""THE LIST's page (levain 64 L3), in real headless Chrome against a served cockpit with a real broker, spore store and coordinator
store in the scratch home (the conftest isolates HOME: nothing here touches an operator's live apps or queue).

Pinned: the intake box submits ONE line and prints the server's receipt sentence verbatim (a plain line, a ``?`` talk, an ``@`` drop
that commits, an ``@`` drop that waits for a signature); a refusal prints the server's reason in words; a 501 prints plainly; the
surface chips filter the rows; Open loops is off the default Board tab and on Build; a band or board row offers no verb.
``LEVAIN_LIST_SHOTS=<dir>`` keeps the desktop 1280 and phone 390 screenshots there."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from anneal_memory.spores import SporeStore

from levain.consent.queue import default_queue_dir
from levain.coordinator.store import CoordStore
from tests.test_cockpit_flip_parity import _drive, needs_browser
from tests.test_cockpit_k2a import TOKEN, _install, _serve

TRAY = "document.querySelector('[data-ck-panel=tray]')"
INPUT = f"{TRAY}.querySelector('.list-intake-input')"
RECEIPT = f"{TRAY}.querySelector('[data-list-receipt]')"


def _reload() -> list[dict[str, Any]]:
    """Storage as an operator's browser already holds it (the write token), then the page fresh."""
    return [{"eval": f"localStorage.setItem('levain_write_token', {json.dumps(TOKEN)}); "
                     "sessionStorage.setItem('t0', String(performance.timeOrigin)); 1"},
            {"eval": "location.reload(); 1"},
            {"until": "String(performance.timeOrigin) !== sessionStorage.getItem('t0')"}]


def _type_and_enter(line: str) -> list[dict[str, Any]]:
    """What a person does: focus the box, put a line in it (an input event, as typing makes), press Enter."""
    return [{"eval": f"(() => {{ const i = {INPUT}; i.focus(); i.value = {json.dumps(line)}; "
                     "i.dispatchEvent(new Event('input', {bubbles: true})); return 1; })()"},
            {"key": "Enter", "vk": 13}]


def _receipt_is(text_test: str) -> dict[str, Any]:
    return {"until": f"(() => {{ const r = {RECEIPT}; return !!r && ({text_test}) && r.textContent; }})()"}


def _seed_more(root: Path) -> None:
    store = SporeStore(root / ".levain" / "memory.spores.json")
    store.add(type="task", text="talk about the water", disposition="seed", attention="talk")
    store.add(type="task", text="call the dentist", disposition="agenda", target="desk", next="2020-01-07T09:00")
    qdir = default_queue_dir()
    qdir.mkdir(parents=True, exist_ok=True, mode=0o700)
    CoordStore(qdir).coord_take()          # the desk's store exists: a plain line has somewhere to land


def _posts(out: dict[str, Any]) -> list[dict[str, Any]]:
    posted = (json.loads(p.split(" ", 1)[1]) for p in out["posts"] if p.startswith("/cockpit/verb "))
    return [b for b in posted if not b.get("dry_run")]


@needs_browser
def test_the_intake_box_prints_the_servers_receipt_for_each_kind_and_a_refusal(tmp_path: Path) -> None:
    root, src, _ids = _install(tmp_path)
    _seed_more(root)
    shots = Path(os.environ.get("LEVAIN_LIST_SHOTS") or tmp_path)
    shots.mkdir(parents=True, exist_ok=True)
    with _serve(src, write_token=TOKEN, surface_tokens={"browser": TOKEN}, install_class="reported") as (url, _httpd):
        out = _drive(tmp_path, f"{url}/", [
            {"emulate": {"w": 1280, "h": 1000, "dpr": 1}},
            *_reload(),
            {"until": f"{INPUT} && 1"},
            {"eval": f"JSON.stringify({{enter: {INPUT}.getAttribute('enterkeyhint'), ac: {INPUT}.getAttribute('autocomplete'), "
                     f"lp: {INPUT}.getAttribute('data-lpignore'), h: {INPUT}.getBoundingClientRect().height}})"},
            # a plain line: answered at once, sent to the desk to sort
            *_type_and_enter("look at the sticky note"),
            _receipt_is("/desk to sort/.test(r.textContent)"),
            {"eval": f"{RECEIPT}.textContent"},
            # a talk line: a reported intent commits it
            *_type_and_enter("? the future of the List"),
            _receipt_is("/^talk spore-/.test(r.textContent)"),
            {"eval": f"{RECEIPT}.textContent"},
            # a drop: commits too, the receipt says when it fires
            *_type_and_enter("@+3h send the invoice"),
            _receipt_is("/^scheduled drop spore-.* fires /.test(r.textContent)"),
            {"eval": f"{RECEIPT}.textContent"},
            # a refusal: the parser's words, nothing written; the unsent line stays in the box
            *_type_and_enter("@yesterday 9am too late"),
            _receipt_is("r.classList.contains('err') && r.textContent.length > 4"),
            {"eval": f"JSON.stringify({{r: {RECEIPT}.textContent, kept: {INPUT}.value}})"},
            {"shot": str(shots / "list_1280.png")},
            {"emulate": {"w": 390, "h": 844, "dpr": 2, "mobile": True}},
            {"eval": f"{INPUT}.scrollIntoView(); 1"},
            {"eval": "JSON.stringify({w: document.documentElement.scrollWidth <= window.innerWidth + 1, "
                     f"h: {INPUT}.getBoundingClientRect().height, fs: getComputedStyle({INPUT}).fontSize}})"},
            {"shot": str(shots / "list_390.png")},
        ])
    v = out["values"]
    attrs = json.loads(next(x for x in v if isinstance(x, str) and x.startswith('{"enter"')))
    assert attrs["enter"] == "send" and attrs["ac"] == "off" and attrs["lp"] == "true" and attrs["h"] >= 44, attrs
    texts = [x for x in v if isinstance(x, str) and not x.startswith("{")]
    assert "sent to the desk to sort" in texts, v
    assert any(t.startswith("talk spore-") for t in texts), v
    assert any(t.startswith("scheduled drop spore-") and " fires " in t for t in texts), v
    refused = json.loads(next(x for x in v if isinstance(x, str) and x.startswith('{"r"')))
    assert refused["kept"] == "@yesterday 9am too late" and refused["r"], refused
    assert "intake_refused" not in refused["r"], refused           # the parser's sentence, not a code
    phone = json.loads(next(x for x in v if isinstance(x, str) and x.startswith('{"w"')))
    assert phone["w"] is True and phone["h"] >= 44 and float(phone["fs"][:-2]) >= 16, phone
    sent = _posts(out)
    assert [b["params"]["line"] for b in sent] == ["look at the sticky note", "? the future of the List",
                                                     "@+3h send the invoice", "@yesterday 9am too late"][:len(sent)], sent
    assert all(b.get("idempotency_key") for b in sent[:3]) and len({b["idempotency_key"] for b in sent[:3]}) == 3, sent


@needs_browser
def test_a_drop_waits_for_the_signature_and_says_so_where_nothing_signs(tmp_path: Path) -> None:
    root, src, _ids = _install(tmp_path)
    _seed_more(root)
    with _serve(src, write_token=TOKEN, surface_tokens={"browser": TOKEN}, install_class=None) as (url, _httpd):
        out = _drive(tmp_path, f"{url}/", [
            {"emulate": {"w": 1280, "h": 1000, "dpr": 1}},
            *_reload(),
            {"until": f"{INPUT} && 1"},
            *_type_and_enter("@+3h call X"),
            {"until": f"(() => {{ const r = {RECEIPT}; return !!r && r.textContent.length > 4 && r.textContent !== '…' && r.textContent; }})()"},
            {"eval": f"JSON.stringify({{r: {RECEIPT}.textContent, cls: {RECEIPT}.className}})"},
        ])
    got = json.loads(out["values"][-1])
    # either the server waits for a signature (the receipt line says so, claiming no fire) or it refuses plainly: never a silent nothing
    assert got["r"], got
    assert "awaiting your signature" in got["r"] or "err" in got["cls"], got


@needs_browser
def test_the_surface_chips_filter_and_no_band_or_board_row_has_a_verb(tmp_path: Path) -> None:
    root, src, _ids = _install(tmp_path)
    _seed_more(root)
    rows = f"[...{TRAY}.querySelectorAll('.sp-results .row')]"
    with _serve(src, write_token=TOKEN, surface_tokens={"browser": TOKEN}, install_class="reported") as (url, _httpd):
        out = _drive(tmp_path, f"{url}/", [
            {"emulate": {"w": 1280, "h": 1000, "dpr": 1}},
            *_reload(),
            {"until": f"{TRAY} && {TRAY}.querySelectorAll('.list-surface-chip').length > 1 && {rows}.length > 2"},
            {"eval": f"JSON.stringify({{chips: [...{TRAY}.querySelectorAll('.list-surface-chip')].map(c => c.dataset.surface), "
                     f"surfaces: {rows}.map(r => r.dataset.surface), n: {rows}.length}})"},
            {"eval": f"{TRAY}.querySelector('.list-surface-chip[data-surface=talk]').click(); 1"},
            {"eval": f"JSON.stringify({{surfaces: {rows}.map(r => r.dataset.surface), "
                     f"pressed: {TRAY}.querySelector('.list-surface-chip[aria-pressed=true]').dataset.surface}})"},
            {"eval": f"{TRAY}.querySelector('.list-surface-chip[data-surface=\"\"]').click(); {rows}.length"},
            {"eval": f"JSON.stringify({{foreignVerbs: {rows}.filter(r => ['band','board'].includes(r.dataset.surface) && r.querySelector('.verb-btn')).length}})"},
        ])
    v = out["values"]
    first = json.loads(next(x for x in v if isinstance(x, str) and x.startswith('{"chips"')))
    assert first["chips"][0] == "" and "talk" in first["chips"] and "drop" in first["chips"], first
    only = json.loads(next(x for x in v if isinstance(x, str) and x.startswith('{"surfaces"')))
    assert only["surfaces"] and set(only["surfaces"]) == {"talk"} and only["pressed"] == "talk", only
    assert v[-2] == first["n"], v                                      # All brings every row back
    assert json.loads(v[-1])["foreignVerbs"] == 0


@needs_browser
def test_open_loops_is_off_the_board_tab_and_on_build(tmp_path: Path) -> None:
    root, src, _ids = _install(tmp_path)
    _seed_more(root)
    loops = "document.querySelector('[data-ck-panel=loops]')"
    shown = f"(() => {{ const p = {loops}; return !!p && p.getClientRects().length > 0 && getComputedStyle(p).display !== 'none'; }})()"
    with _serve(src, write_token=TOKEN, surface_tokens={"browser": TOKEN}, install_class="reported") as (url, _httpd):
        out = _drive(tmp_path, f"{url}/", [
            {"emulate": {"w": 1280, "h": 1000, "dpr": 1}},
            *_reload(),
            {"eval": "location.hash = '#board'; 1"},
            {"until": "document.body.dataset.mode === 'board' && 1"},
            {"until": f"{TRAY} && {TRAY}.getClientRects().length > 0 && 1"},
            {"eval": f"JSON.stringify({shown})"},
            {"eval": "JSON.stringify({cols: document.querySelector('.ck-zgrid.ck-twocol') ? document.querySelector('.ck-zgrid.ck-twocol').dataset.cols : null, "
                     f"tray: {TRAY}.getBoundingClientRect().width, vw: window.innerWidth, hs: document.documentElement.scrollWidth <= window.innerWidth + 1}})"},
            {"eval": "location.hash = '#build'; 1"},
            {"until": "document.body.dataset.mode === 'build' && 1"},
            {"until": f"{shown} && 1"},
            {"eval": f"JSON.stringify({shown})"},
        ])
    v = out["values"]
    shown_values = [x == "true" for x in v if x in ("true", "false")]
    assert shown_values == [False, True], v                            # absent on Board, present on Build
    board = json.loads(next(x for x in v if isinstance(x, str) and x.startswith('{"cols"')))
    assert board["hs"] is True and board["cols"] in (None, "1"), board  # the right column is gone, not a hole

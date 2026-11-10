"""THE LIST's page (levain 64 L3), in real headless Chrome against a served cockpit with a real broker, spore store and desk
store in the scratch home (the conftest isolates HOME: nothing here touches an operator's live apps or queue).

Pinned: the intake box submits ONE line and prints the server's receipt sentence verbatim (a plain line, a ``?`` talk, an ``@`` drop
that commits, an ``@`` drop that waits for a signature); a refusal prints the server's reason in words; a 501 prints plainly; the
surface chips filter the rows; Open loops is off the default Board tab and on Build; a band or board row offers no verb.
``LEVAIN_LIST_SHOTS=<dir>`` keeps the desktop 1280 and phone 390 screenshots there."""
from __future__ import annotations

import json
import re
import os
from pathlib import Path
from typing import Any

import pytest
from anneal_memory.spores import SporeStore

from levain.consent.queue import default_queue_dir
from levain.desk.store import CoordStore
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


# --- the List at a true phone width, with real-shaped rows (levain 72, key 1108-34, lane listmobile) ---------------------------


def _seed_phone(root: Path) -> None:
    """Rows shaped like the ones on an operator's phone: band rows led by their own origin ("desk:", "levain:") with a long
    unbroken detail line, a spore that merely MENTIONS a repo, more spores than the first page, and held rows."""
    from datetime import date, timedelta

    from levain.consent.questions import QuestionStore

    qdir = default_queue_dir()
    qdir.mkdir(parents=True, exist_ok=True, mode=0o700)
    qs = QuestionStore(qdir)
    long_path = "/Users/someone/Briefcase/levain/project_memory/1108-34-handoff/a_very_long_unbroken_path_name_for_the_detail_line.md"
    qs.file(agent="claude_code", agent_session_id="s1", what="desk: Write the BJE 1.6.2 release plan Monday 11-09 morning, then file the go as a band row.",
            options=[("ship", "Ship it"), ("hold", "Hold")], rec="hold",
            blocks=f"Monday 11-09 morning (competitor research, harder-selling copy) {long_path}", cwd="/w/levain")
    qs.file(agent="claude_code", agent_session_id="s2", what="levain: TUE 11-10: read the release essay draft 2 and fill its 17 slots; it ships with a long tail of words after that",
            options=[("ship", "Ship it"), ("hold", "Hold")], rec="hold",
            blocks=f"Tuesday 11-10, your read (04_DRAFT.md; levain 59 surfaces the essay) {long_path}", cwd="/w/levain")
    store = SporeStore(root / ".levain" / "memory.spores.json")
    store.add(type="task", text="TALK: HACK THE ECHO: reflash an old Echo (routed by desk 26 on 11-07)", disposition="seed", attention="talk")
    for i in range(14):
        store.add(type="task", text=f"a plain seed number {i} with some words to fill the line a bit", disposition="seed")
    far = (date.today() + timedelta(days=30)).isoformat()
    store.add(type="task", text="a held seed", disposition="seed", next=far)


_PHONE_PROBE = f"""(() => {{
  const T = {TRAY}; const card = T.getBoundingClientRect(); const out = {{rows: [], over: [], pills: [], ph: []}};
  const clip = (n) => {{ const r = n.getBoundingClientRect(); return r.width > 0 && (r.right > card.right + 0.5 || r.left < card.left - 0.5); }};
  for (const row of T.querySelectorAll('.sp-results .row')) {{
    const clause = row.querySelector('.clause'); const txt = clause ? clause.textContent : '';
    const pills = [...row.querySelectorAll('.k7-repo,.rp-tag')];
    const det = row.querySelector('.list-support');
    const b = (n) => n.getBoundingClientRect();
    for (const p of pills) if (det) {{ const a = b(p), d = b(det);
      if (a.left < d.right && a.right > d.left && a.top < d.bottom && a.bottom > d.top) out.over.push(txt.slice(0, 30) + ' | ' + p.textContent); }}
    for (const n of [row, ...row.querySelectorAll('.list-meta, .list-meta > *, .clause')]) if (clip(n)) out.over.push('edge ' + txt.slice(0, 30) + ' | ' + n.className + ' ' + Math.round(b(n).left) + '-' + Math.round(b(n).right) + ' card ' + Math.round(card.left) + '-' + Math.round(card.right));
    out.rows.push({{t: txt.slice(0, 24), pills: pills.map(p => p.textContent), support: !!det}});
  }}
  for (const sel of ['.list-intake-input', '.ep-search-input']) {{
    const i = T.querySelector(sel); const cs = getComputedStyle(i); const c = document.createElement('canvas').getContext('2d');
    c.font = cs.font; const w = c.measureText(i.placeholder).width;
    out.ph.push({{sel, w, room: i.clientWidth - parseFloat(cs.paddingLeft) - parseFloat(cs.paddingRight), ph: i.placeholder}});
  }}
  out.hs = document.documentElement.scrollWidth <= window.innerWidth + 1;
  return JSON.stringify(out);
}})()"""


@needs_browser
def test_the_list_fits_a_390_phone_with_real_shaped_rows(tmp_path: Path) -> None:
    root, src, _ids = _install(tmp_path)
    _seed_more(root)
    _seed_phone(root)
    shots = Path(os.environ.get("LEVAIN_LIST_SHOTS") or tmp_path)
    shots.mkdir(parents=True, exist_ok=True)
    with _serve(src, write_token=TOKEN, surface_tokens={"browser": TOKEN}, install_class="reported") as (url, _httpd):
        out = _drive(tmp_path, f"{url}/", [
            {"emulate": {"w": 390, "h": 844, "dpr": 3, "mobile": True}},
            *_reload(),
            {"until": f"{TRAY} && {TRAY}.querySelectorAll('.sp-results .row[data-surface=band]').length > 1 && 1"},
            {"eval": f"{TRAY}.scrollIntoView(); 1"},
            {"eval": _PHONE_PROBE},
            {"shot": str(shots / "phone_390.png")},
        ])
    got = json.loads(next(x for x in out["values"] if isinstance(x, str) and x.startswith('{"rows"')))
    assert got["over"] == [], got["over"]                              # nothing overlaps the detail line or passes the card's edge
    assert got["hs"] is True, got                                      # and the page does not scroll sideways
    for ph in got["ph"]:
        assert ph["w"] <= ph["room"] + 0.5, ph                         # neither placeholder is cut at 390
    by = {r["t"]: r for r in got["rows"]}
    desk = next(r for t, r in by.items() if t.startswith("desk:"))
    assert desk["pills"] in ([], ["desk"]), desk                       # a "desk:" row names desk or nothing, never the band's store
    lev = next(r for t, r in by.items() if t.startswith("levain:"))
    assert lev["pills"] in ([], ["levain"]), lev
    mention = next(r for t, r in by.items() if "HACK THE ECHO" in r["t"] or t.startswith("TALK"))
    assert mention["pills"] == [], mention                             # a row that only MENTIONS a repo wears no pill


# --- listfix2 (levain 72, key 1108-34): one pill per row, one media listener, a lead word is an origin only if known -----------

_MQ_SPY = ("(function(){const o=window.matchMedia.bind(window);window.__mqAdds=0;"
           "window.matchMedia=function(q){const m=o(q);if(/max-width:\\s*720px/.test(q)){const a=m.addEventListener.bind(m);"
           "m.addEventListener=function(t,f,c){window.__mqAdds++;return a(t,f,c);};}return m;};})()")

_LISTFIX_PROBE = f"""(() => {{
  const T = document.querySelector('#levain-modal .modal-body') || {TRAY}; const rows = [];
  const grab = () => {{ for (const row of T.querySelectorAll('.sp-results .row')) {{
    const clause = row.querySelector('.clause');
    const meta = row.querySelector('.list-meta') || row;
    rows.push({{t: (clause ? clause.textContent : '').slice(0, 24), surface: row.dataset.surface || '',
      pills: [...meta.querySelectorAll('.k7-repo')].map(p => p.textContent), all: row.querySelectorAll('.k7-repo').length}});
  }} }};
  grab();                                                   // the rows under "all", then the later ones under their own time chip
  const held = T.querySelector('.list-time-chip[data-time="later"]');
  if (held) {{ held.click(); grab(); }}
  return JSON.stringify({{rows}});
}})()"""


@needs_browser
def test_listfix2_one_pill_one_listener_and_known_origins_only(tmp_path: Path) -> None:
    from levain.consent.questions import QuestionStore

    root, src, _ids = _install(tmp_path)
    _seed_more(root)
    _seed_phone(root)
    qs = QuestionStore(default_queue_dir())
    qs.file(agent="claude_code", agent_session_id="s3", what="note: call Alice about the thing on Friday",
            options=[("ship", "Ship it"), ("hold", "Hold")], rec="hold", blocks="detail", cwd="/w/levain")
    from datetime import date, timedelta
    SporeStore(root / ".levain" / "memory.spores.json").add(type="task", text="plain domain row zz", domain="life", disposition="seed",
                                                            next=(date.today() + timedelta(days=30)).isoformat())   # held: the path that draws a trailing chip
    ph = f"{TRAY}.querySelector('.list-intake-input').placeholder"
    with _serve(src, write_token=TOKEN, surface_tokens={"browser": TOKEN}, install_class="reported") as (url, _httpd):
        out = _drive(tmp_path, f"{url}/", [
            {"emulate": {"w": 390, "h": 844, "dpr": 3, "mobile": True}},
            {"init": _MQ_SPY},
            *_reload(),
            {"until": f"{TRAY} && {TRAY}.querySelectorAll('.sp-results .row[data-surface=band]').length > 1 && 1"},
            # the registry must know "levain" and that row must have drawn its pill before the probe reads it
            {"until": f"window.LevainSky && window.LevainSky.repos().includes('levain') && {TRAY} && "
                      f"[...{TRAY}.querySelectorAll('.sp-results .row')].some(r => (r.querySelector('.clause')||{{}}).textContent "
                      f"&& r.querySelector('.clause').textContent.startsWith('levain:') && r.querySelector('.k7-repo')) && 1"},
            # later rows are listed in the focus view only (the card names them in its one control): read the rows there
            {"eval": f"{TRAY}.querySelector('.pexpand').click(); 1"},
            {"until": "document.querySelector('#levain-modal .list-time-chip[data-time=\"later\"]') && 1"},
            {"eval": _LISTFIX_PROBE},
            {"eval": "document.querySelector('#levain-modal .modal-close').click(); 1"},
            {"eval": "JSON.stringify({adds: window.__mqAdds})"},
            # rebuild the List the way the page does on a refresh: the tab coming back
            *[{"eval": "document.dispatchEvent(new Event('visibilitychange')); 1"} for _ in range(6)],
            {"eval": "new Promise(r => setTimeout(() => r(1), 1500))"},
            {"eval": "JSON.stringify({adds: window.__mqAdds})"},
            {"eval": f"JSON.stringify({{ph: {ph}}})"},
            {"emulate": {"w": 1000, "h": 844, "dpr": 1}},
            {"until": f"{ph}.startsWith('@mon 9am call X  ') && 1"},
            {"eval": f"JSON.stringify({{ph: {ph}}})"},
            {"emulate": {"w": 390, "h": 844, "dpr": 3, "mobile": True}},
            {"until": f"{ph} === '@mon 9am call · ? talk Y' && 1"},
        ])
    vals = [json.loads(x) for x in out["values"] if isinstance(x, str) and x.startswith("{")]
    rows = vals[0]["rows"]
    by = {r["t"]: r for r in rows}
    bad: list[str] = []
    dom = next(r for r in rows if r["t"].startswith("plain domain row"))
    if not (dom["pills"] == ["life"] and dom["all"] == 1):
        bad.append(f"A: a domain row carries one chip, got {dom}")                # A: one chip, not two
    note = next(r for t, r in by.items() if t.startswith("note:"))
    if note["pills"] != []:
        bad.append(f"C: 'note:' is no origin, got {note}")                         # C: a lead word counts only if known
    for lead in ("desk", "levain"):
        got = next(r for t, r in by.items() if t.startswith(lead + ":"))["pills"]
        if got != [lead]:
            bad.append(f"C: '{lead}:' should show {lead}, got {got}")
    first, after = vals[1]["adds"], vals[2]["adds"]
    if first != 1:
        bad.append(f"B: expected exactly one media listener after the first build, got {first}")
    if after != first:
        bad.append(f"B: the media listener count grew {first} -> {after}")        # B: no listener per rebuild
    assert vals[3]["ph"].startswith("@mon 9am call · "), vals[3]                 # narrow at 390
    assert vals[4]["ph"].startswith("@mon 9am call X  ·  "), vals[4]               # wide again after the width change
    assert not bad, bad


# --- listpanel (levain 73, key 1108-37): the focus view's sticky filter bar is solid; the panel's held toggle shows the held rows ---

_BAR_PROBE = """(() => {
  const mb = document.querySelector('#levain-modal .modal-body');
  const bar = mb && mb.querySelector('.ep-search');
  if (!bar) return JSON.stringify({err: 'no bar'});
  const cs = getComputedStyle(bar);
  // a colour with an alpha channel names it after a "/" (oklch, color-mix) or as the 4th rgba part; none named is opaque
  const m = /(?:\\/\\s*|rgba\\([^,)]*,[^,)]*,[^,)]*,\\s*)([0-9.]+%?)/.exec(cs.backgroundColor);
  const alpha = m ? (m[1].endsWith('%') ? parseFloat(m[1]) / 100 : parseFloat(m[1])) : 1;
  return JSON.stringify({bg: cs.backgroundColor, alpha,
    pos: cs.position, bottom: cs.borderBottomWidth, scrolled: mb.scrollTop, max: mb.scrollHeight - mb.clientHeight,
    gap: bar.getBoundingClientRect().top - mb.getBoundingClientRect().top, pad: getComputedStyle(mb).paddingTop});
})()"""


@needs_browser
def test_listpanel_141_the_focus_views_sticky_filter_bar_is_opaque(tmp_path: Path) -> None:
    root, src, _ids = _install(tmp_path)
    _seed_more(root)
    _seed_phone(root)
    shots = Path(os.environ.get("LEVAIN_LIST_SHOTS") or tmp_path)
    shots.mkdir(parents=True, exist_ok=True)
    with _serve(src, write_token=TOKEN, surface_tokens={"browser": TOKEN}, install_class="reported") as (url, _httpd):
        out = _drive(tmp_path, f"{url}/", [
            {"emulate": {"w": 390, "h": 844, "dpr": 3, "mobile": True}},
            *_reload(),
            {"until": f"{TRAY} && {TRAY}.querySelectorAll('.sp-results .row[data-surface=band]').length > 1 && 1"},
            {"eval": f"{TRAY}.querySelector('.pexpand').click(); 1"},
            {"until": "document.querySelector('#levain-modal .modal-body .sp-results .row') && 1"},
            {"eval": "new Promise(r => setTimeout(() => r(1), 900))"},       # the dialog's fade settles before anything is read or shot
            {"eval": "(() => { const mb = document.querySelector('#levain-modal .modal-body'); mb.scrollTop = Math.floor((mb.scrollHeight - mb.clientHeight) / 2); return 1; })()"},
            {"eval": _BAR_PROBE},
            {"shot": str(shots / "listpanel_141_after.png")},
        ])
    got = json.loads(next(x for x in out["values"] if isinstance(x, str) and x.startswith("{") and "pos" in x or isinstance(x, str) and "err" in x))
    assert "err" not in got, got
    assert got["pos"] == "sticky", got
    assert got["max"] > 0 and got["scrolled"] > 0, got                 # rows really pass under the bar
    assert got["alpha"] == 1 and got["bg"] != "rgba(0, 0, 0, 0)" and got["bg"] != "transparent", got
    assert got["gap"] <= 0.5, got                                      # and it pins flush: no strip above it where a row shows


# --- 531 round 4 (levain 73, key 1108-37): desk-owed and LATER rows have their own groups; the card has ONE control ------------


def _host_band(monkeypatch: Any, *, all_desk: bool = False) -> None:
    """The band as flow's adapter fills it: a "desk:" row is the desk's (section desk; "desk: carried" is one open > 24h, flow's
    `carried`), a "levain: TUE" row comes back on a later day, every other row is the operator's (section you)."""
    from dataclasses import replace
    from datetime import date, timedelta

    from levain import band_view

    real = band_view._read
    later = (date.today() + timedelta(days=3)).isoformat()

    def read(store: Any) -> Any:
        items, numbers, closed = real(store)
        out = []
        for ref, q in items:
            if q is not None:
                what = str(q.what)
                q = replace(q, section="desk" if all_desk or what.startswith("desk:") else "you", carried=what.startswith("desk: carried") and not all_desk,
                            snoozed_until=later if what.startswith(("levain: TUE", "desk: carried")) else None)   # a carried desk row with a date is still due now
            out.append((ref, q))
        return out, numbers, closed

    monkeypatch.setattr(band_view, "_read", read)


_CARD_PROBE = f"""(() => {{
  const T = {TRAY};
  const rows = [...T.querySelectorAll('.sp-results .row')].filter(r => r.style.display !== 'none' && r.getClientRects().length);
  const text = (r) => (r.querySelector('.clause') || {{}}).textContent || '';
  const foot = [...T.querySelectorAll('.more-foot, .xmore')].filter(n => !n.hidden && n.getClientRects().length);
  return JSON.stringify({{shown: rows.map(text), more: foot.map(n => n.className + ' | ' + n.textContent.trim()),
    chips: [...T.querySelectorAll('.list-surface-chip')].map(c => c.textContent),
    head: (T.querySelector('.ck-count') || {{}}).textContent}});
}})()"""

_FOCUS_PROBE = """(() => {
  const mb = document.querySelector('#levain-modal .modal-body');
  const heads = [...mb.querySelectorAll('.grp-head')].map(h => h.textContent.trim());
  const group = (title) => { const h = [...mb.querySelectorAll('.grp-head')].find(x => x.textContent.trim().startsWith(title));
    const col = h && h.parentElement; return col ? [...col.querySelectorAll('.row .clause')].map(c => c.textContent.slice(0, 20)) : null; };
  return JSON.stringify({heads, desk: group('Desk owes'), later: group('Later'), needs: group('Needs you'),
    chips: [...mb.querySelectorAll('.list-surface-chip')].map(c => c.textContent),
    title: document.querySelector('#levain-modal-title').textContent});
})()"""


_BAND_CHIP_PROBE = """(() => {
  const mb = document.querySelector('#levain-modal .modal-body');
  const chip = mb.querySelector('.list-surface-chip[data-surface="band"]');
  const rows = [...mb.querySelectorAll('.sp-results .row')].filter(r => r.getClientRects().length);
  return JSON.stringify({count: parseInt(chip.textContent.trim().split(' ').pop(), 10), rows: rows.length,
    texts: rows.map(r => ((r.querySelector('.clause') || {}).textContent || '').slice(0, 20)),
    heads: [...mb.querySelectorAll('.grp-head')].map(h => h.textContent.trim())});
})()"""


@needs_browser
def test_r4_the_card_holds_due_rows_and_one_control_and_the_focus_view_every_group(tmp_path: Path, monkeypatch: Any) -> None:
    root, src, _ids = _install(tmp_path)
    _seed_more(root)
    _seed_phone(root)
    from levain.consent.questions import QuestionStore
    QuestionStore(default_queue_dir()).file(agent="claude_code", agent_session_id="s9", what="One look on your iPhone once the List is fixed.",
                                            options=[("ship", "Ship it"), ("hold", "Hold")], rec="hold", blocks="his look", cwd="/w/levain")
    QuestionStore(default_queue_dir()).file(agent="claude_code", agent_session_id="s8", what="desk: carried Launch the TAOSERV upgrade seat before Monday 20:00.",
                                            options=[("ship", "Ship it"), ("hold", "Hold")], rec="hold", blocks="the upgrade", cwd="/w/taoserv")
    _host_band(monkeypatch)
    with _serve(src, write_token=TOKEN, surface_tokens={"browser": TOKEN}, install_class="reported") as (url, _httpd):
        out = _drive(tmp_path, f"{url}/", [
            {"emulate": {"w": 390, "h": 844, "dpr": 3, "mobile": True}},
            *_reload(),
            {"until": f"{TRAY} && {TRAY}.querySelectorAll('.sp-results .row[data-surface=band]').length > 0 && 1"},
            {"eval": "new Promise(r => setTimeout(() => r(1), 900))"},
            {"eval": _CARD_PROBE},
            {"eval": f"{TRAY}.querySelector('[data-list-more]') ? ({TRAY}.querySelector('[data-list-more]').click(), 1) : 0"},
            {"until": "document.querySelector('#levain-modal .modal-body .sp-results .row') && 1"},
            {"eval": "new Promise(r => setTimeout(() => r(1), 900))"},
            {"eval": _FOCUS_PROBE},
            {"eval": "document.querySelector('#levain-modal .list-surface-chip[data-surface=\"band\"]').click(); 1"},
            {"eval": "new Promise(r => setTimeout(() => r(1), 500))"},
            {"eval": _BAND_CHIP_PROBE},
        ])
    vals = [json.loads(x) for x in out["values"] if isinstance(x, str) and x.startswith("{")]
    card, focus, band = vals[0], vals[1], vals[2]
    # the band chip's number is the number of rows its tap shows, the desk's and the later band rows included, and the carried row drawn under both of its headings
    assert band["rows"] == band["count"] and band["rows"] > 0, band
    assert any(t.startswith("desk: Write") for t in band["texts"]) and any(t.startswith("levain: TUE") for t in band["texts"]), band
    assert sum(t.startswith("desk: carried") for t in band["texts"]) == 2, band
    assert not any((t.startswith("desk:") and not t.startswith("desk: carried")) or t.startswith("levain: TUE") for t in card["shown"]), card   # not the operator's, not due
    assert any(t.startswith("desk: carried") for t in card["shown"]), card      # a CARRIED desk row is needs-you, as flow's footer lists it
    assert len(card["more"]) == 1, card                                                                  # ONE control
    assert "desk owes 2" in card["more"][0] and "later 2" in card["more"][0] and "held" not in card["more"][0], card   # the band's later row and the dated seed: one word
    assert focus["desk"] and focus["desk"][0].startswith("desk:"), focus                                 # the desk's row under its heading
    assert focus["later"] and focus["later"][0].startswith("levain: TUE"), focus                         # the later row under LATER
    assert focus["needs"] and not any(t.startswith(("levain: TUE", "desk: Write")) for t in focus["needs"]), focus
    assert any(t.startswith("desk: carried") for t in focus["needs"]), focus                             # the carried one is under Needs you
    # ONE rule for "all": each view's header and its "all" chip agree, the focus view's follows from the card's (+ the desk's and the
    # later rows the card names in its control), and held rows stand apart in their own chip
    num = lambda chips, word: next((int(c.rsplit(" ", 1)[1]) for c in chips if c.startswith(word + " ")), None)
    assert card["head"] and int(card["head"]) == num(card["chips"], "all"), card
    assert focus["title"].endswith(f"({num(focus['chips'], 'all')})"), focus
    desk, later = (int(re.search(rf"{w} (\d+)", card["more"][0]).group(1)) for w in ("desk owes", "later"))
    # the carried desk row is in the card's "all" already (needs-you) and in the control's desk owes; the focus view draws it a second time
    # under Desk owes, and its "all" counts the rows that tap draws, so that second drawing is in the number
    assert num(focus["chips"], "all") == num(card["chips"], "all") + desk + later, (card, focus)
    # the control's "desk owes N" is the rows a person sees under Desk owes: the carried row is listed there too, as the footer prints it
    assert len(focus["desk"]) == desk and len(focus["later"]) == later, focus
    assert any(t.startswith("desk: carried") for t in focus["desk"]), focus
    # the type chips cut across the time-states: the focus view's spores chip also holds the dated seed the card leaves to its control
    assert num(card["chips"], "spores") + 1 == num(focus["chips"], "spores"), (card["chips"], focus["chips"])
    assert num(card["chips"], "held") is None and num(focus["chips"], "held") is None, (card, focus)        # no held chip: Later is a time-state


# --- 531 r4 round 5 (levain 74, key 1108-39): L1+L2 fixes -----------------------------------------------------------------------

_CTL_PROBE = f"""(() => {{
  const T = {TRAY}; const c = T.querySelector('[data-list-more]');
  const tags = [...T.querySelectorAll('.sp-results .row')].filter(r => /desk: carried/.test(r.textContent)).map(r => !!r.querySelector('.list-desk-owed') && !/back /.test(r.textContent));
  return JSON.stringify({{h: c ? c.getBoundingClientRect().height : 0, label: c ? c.getAttribute('aria-label') : null, tags,
    empty: [...T.querySelectorAll('.sp-results .empty')].map(n => n.textContent)}});
}})()"""


def _carried_install(tmp_path: Path, monkeypatch: Any, *, all_desk: bool = False, spores: bool = True) -> Any:
    root, src, ids = _install(tmp_path)
    _seed_more(root)
    from levain.consent.questions import QuestionStore
    qs = QuestionStore(default_queue_dir())
    qs.file(agent="claude_code", agent_session_id="s7", what="One look on your iPhone once the List is fixed.",
            options=[("ship", "Ship it"), ("hold", "Hold")], rec="hold", blocks="his look", cwd="/w/levain")
    qs.file(agent="claude_code", agent_session_id="s8", what="desk: carried Launch the TAOSERV upgrade seat before Monday 20:00.",
            options=[("ship", "Ship it"), ("hold", "Hold")], rec="hold", blocks="the upgrade", cwd="/w/taoserv")
    qs.file(agent="claude_code", agent_session_id="s6", what="levain: TUE 11-10: read the release essay draft 2.",
            options=[("ship", "Ship it"), ("hold", "Hold")], rec="hold", blocks="the essay", cwd="/w/levain")
    qs.file(agent="claude_code", agent_session_id="s9", what="desk: Write the BJE plan.",
            options=[("ship", "Ship it"), ("hold", "Hold")], rec="hold", blocks="the plan", cwd="/w/bje")
    if not spores:       # nothing but band rows is left on the card
        store = SporeStore(root / ".levain" / "memory.spores.json")
        for sp in store.list_open():
            store.update(sp["id"], disposition="loop")
        for key in ("seed", "held", "note"):
            if store.get(ids[key]):
                store.update(ids[key], disposition="loop")
    _host_band(monkeypatch, all_desk=all_desk)
    return src


@needs_browser
def test_listpanel_r5_the_control_is_44px_at_every_width_has_its_label_and_closing_returns_focus_to_it(tmp_path: Path, monkeypatch: Any) -> None:
    src = _carried_install(tmp_path, monkeypatch)
    focus_is_control = "document.activeElement && document.activeElement.hasAttribute('data-list-more')"
    with _serve(src, write_token=TOKEN, surface_tokens={"browser": TOKEN}, install_class="reported") as (url, _httpd):
        out = _drive(tmp_path, f"{url}/", [
            {"emulate": {"w": 1024, "h": 800, "dpr": 1, "mobile": False}},
            *_reload(),
            {"until": f"{TRAY} && {TRAY}.querySelector('[data-list-more]') && 1"},
            {"eval": "new Promise(r => setTimeout(() => r(1), 700))"},
            {"eval": _CTL_PROBE},
            {"eval": f"{TRAY}.querySelector('[data-list-more]').click(); 1"},
            {"until": "document.querySelector('#levain-modal .modal-body .sp-results .row') && 1"},
            {"eval": "JSON.stringify([...document.querySelectorAll('#levain-modal .grp-head')].map(h => [h.textContent.trim().slice(0, 12), h.getAttribute('role'), h.getAttribute('aria-level')]))"},
            {"eval": "document.querySelector('#levain-modal .modal-close').click(); new Promise(r => setTimeout(() => r(1), 400))"},
            {"eval": f"!!({focus_is_control})"},
        ])
    wide = json.loads(out["values"][-6])
    assert wide["h"] >= 44, wide                                                   # a touch target on a tablet / landscape phone too
    assert wide["label"] and "desk owes 2" in wide["label"] and "open the full list" in wide["label"], wide
    assert wide["tags"] == [True], wide                                            # the carried row is tagged desk-owed, as the footer tags it
    heads = json.loads(out["values"][-3])
    assert heads and all(h[1] == "heading" and h[2] == "3" for h in heads), heads
    assert {h[0] for h in heads} >= {"Needs you (a", "Desk owes (t", "Later"}, heads
    assert out["values"][-1] is True, out["values"]                                # focus is back on the control that opened the view


@needs_browser
def test_listpanel_r5_a_card_with_only_desk_and_later_rows_says_nothing_is_due_and_a_search_finds_them_in_the_full_list(tmp_path: Path, monkeypatch: Any) -> None:
    src = _carried_install(tmp_path, monkeypatch, all_desk=True, spores=False)
    type_q = lambda q: (f"(() => {{ const i = {TRAY}.querySelector('.ep-search-input'); i.value = {json.dumps(q)}; "
                        "i.dispatchEvent(new Event('input', {bubbles: true})); return 1; })()")
    with _serve(src, write_token=TOKEN, surface_tokens={"browser": TOKEN}, install_class="reported") as (url, _httpd):
        out = _drive(tmp_path, f"{url}/", [
            {"emulate": {"w": 390, "h": 844, "dpr": 3, "mobile": True}},
            *_reload(),
            {"until": f"{TRAY} && {TRAY}.querySelector('[data-list-more]') && 1"},
            {"eval": "new Promise(r => setTimeout(() => r(1), 700))"},
            {"eval": _CTL_PROBE},
            {"eval": type_q("BJE")},
            {"eval": f"[{TRAY}.querySelector('.sp-results .empty').textContent, !!{TRAY}.querySelector('[data-list-open-matches]')]"},
            {"eval": f"{TRAY}.querySelector('[data-list-open-matches]').click(); 1"},
            {"until": "document.querySelector('#levain-modal .modal-body .sp-results .row') && 1"},
            {"eval": "[...document.querySelectorAll('#levain-modal .sp-results .row .clause')].map(c => c.textContent.slice(0, 18))"},
        ])
    card = json.loads(out["values"][-6])
    assert card["empty"] == ["Nothing due for you."], card                         # never "Nothing yet. Drop a line ..." over the control
    assert card["h"] > 0, card                                                     # the control is still there
    note, hint = out["values"][-4]
    assert "no items match" in note and "in the full list" in note and hint is True, (note, hint)
    assert any(t.startswith("desk: Write the BJ") for t in out["values"][-1]), out["values"][-1]


@needs_browser
def test_lpr3_one_carried_desk_row_only_is_counted_in_the_control_and_listed_under_desk_owes(tmp_path: Path, monkeypatch: Any) -> None:
    """A carried desk row is needs-you on the card and counted as ONE desk owes in the control; the focus view lists it under Desk owes
    too, so the number a person reads is the rows they can see there."""
    root, src, ids = _install(tmp_path)
    store = SporeStore(root / ".levain" / "memory.spores.json")
    for sp in store.list_open():
        store.update(sp["id"], disposition="loop")
    from levain.consent.questions import QuestionStore
    QuestionStore(default_queue_dir()).file(agent="claude_code", agent_session_id="s8", what="desk: carried Launch the TAOSERV upgrade seat.",
                                            options=[("ship", "Ship it"), ("hold", "Hold")], rec="hold", blocks="the upgrade", cwd="/w/taoserv")
    _host_band(monkeypatch)
    with _serve(src, write_token=TOKEN, surface_tokens={"browser": TOKEN}, install_class="reported") as (url, _httpd):
        out = _drive(tmp_path, f"{url}/", [
            {"emulate": {"w": 390, "h": 844, "dpr": 2, "mobile": True}},
            *_reload(),
            {"until": f"{TRAY} && {TRAY}.querySelector('[data-list-more]') && 1"},
            {"eval": "new Promise(r => setTimeout(() => r(1), 700))"},
            {"eval": f"{TRAY}.querySelector('[data-list-more]').textContent"},
            {"eval": f"{TRAY}.querySelector('[data-list-more]').click(); 1"},
            {"until": "document.querySelector('#levain-modal .modal-body .sp-results .row') && 1"},
            {"eval": "new Promise(r => setTimeout(() => r(1), 700))"},
            {"eval": _FOCUS_PROBE},
        ])
    control = next(x for x in out["values"] if isinstance(x, str) and "desk owes" in x)
    focus = json.loads(next(x for x in out["values"] if isinstance(x, str) and x.startswith('{"heads"')))
    assert "desk owes 1" in control, control
    assert focus["desk"] and len(focus["desk"]) == 1 and focus["desk"][0].startswith("desk: carried"), focus
    assert focus["needs"] and focus["needs"][0].startswith("desk: carried"), focus        # and it is still under Needs you


@needs_browser
def test_lpr3_the_cards_one_control_is_drawn_once_and_not_redrawn_by_its_own_layout(tmp_path: Path, monkeypatch: Any) -> None:
    """L3 r1 (gemini-2, checked): drawing the control changes the panel's children; nothing observing the board may fold the card again for it,
    or the button would be replaced on every pass. The same node is still there after the page has been idle."""
    src = _carried_install(tmp_path, monkeypatch)
    with _serve(src, write_token=TOKEN, surface_tokens={"browser": TOKEN}, install_class="reported") as (url, _httpd):
        out = _drive(tmp_path, f"{url}/", [
            {"emulate": {"w": 390, "h": 844, "dpr": 2, "mobile": True}},
            *_reload(),
            {"until": f"{TRAY} && {TRAY}.querySelector('[data-list-more]') && 1"},
            {"eval": f"window.__m = {TRAY}.querySelector('[data-list-more]'); window.__n = 0; "
                     f"new MutationObserver((r) => {{ window.__n += r.length; }}).observe({TRAY}, {{childList: true}}); 1"},
            {"eval": "new Promise(r => setTimeout(() => r(1), 2500))"},
            {"eval": f"JSON.stringify({{same: window.__m === {TRAY}.querySelector('[data-list-more]'), n: window.__n, count: {TRAY}.querySelectorAll('[data-list-more]').length}})"},
        ])
    got = json.loads(out["values"][-1])
    assert got == {"same": True, "n": 0, "count": 1}, got


# --- row 141 again: the bar is solid in BOTH themes, at the scroll where text sits behind it, and nothing of a row shows above it ---

_BAR_LAYERS = """(() => {
  const mb = document.querySelector('#levain-modal .modal-body');
  const bar = mb.querySelector('.ep-search'); const r = bar.getBoundingClientRect(); const mr = mb.getBoundingClientRect();
  const alphaOf = (c) => { const m = /(?:\\/\\s*|rgba\\([^,)]*,[^,)]*,[^,)]*,\\s*)([0-9.]+%?)/.exec(c);
    return m ? (m[1].endsWith('%') ? parseFloat(m[1]) / 100 : parseFloat(m[1])) : 1; };
  const cs = getComputedStyle(bar);
  // what is on top at points across the bar's height and in the strip between the modal body's top edge and the bar
  const owned = [];
  for (const y of [r.top + 2, r.top + r.height / 2, r.bottom - 2]) for (const x of [r.left + 6, r.left + r.width / 2, r.right - 6]) {
    const e = document.elementFromPoint(x, y); owned.push(!!e && bar.contains(e) || e === bar); }
  const above = mr.top < r.top - 0.5 ? document.elementFromPoint(r.left + r.width / 2, mr.top + (r.top - mr.top) / 2) : null;
  return JSON.stringify({bg: cs.backgroundColor, alpha: alphaOf(cs.backgroundColor), theme: document.documentElement.dataset.theme,
    owned, aboveRow: !!(above && above.closest('.row')), gap: r.top - mr.top, hair: cs.borderBottomWidth + ' ' + cs.borderBottomStyle,
    scrolled: mb.scrollTop, probe: 'bar'});
})()"""


@needs_browser
def test_listpanel_141_again_the_sticky_bar_is_opaque_and_ruled_in_both_themes_scrolled_at_390(tmp_path: Path) -> None:
    root, src, _ids = _install(tmp_path)
    _seed_more(root)
    _seed_phone(root)
    shots = Path(os.environ.get("LEVAIN_LIST_SHOTS") or tmp_path)
    shots.mkdir(parents=True, exist_ok=True)
    steps: list[dict[str, Any]] = [
        {"emulate": {"w": 390, "h": 844, "dpr": 2, "mobile": True}},
        *_reload(),
        {"until": f"{TRAY} && {TRAY}.querySelectorAll('.sp-results .row[data-surface=band]').length > 1 && 1"},
        {"eval": f"{TRAY}.querySelector('.pexpand').click(); 1"},
        {"until": "document.querySelector('#levain-modal .modal-body .sp-results .row') && 1"},
        {"eval": "new Promise(r => setTimeout(() => r(1), 900))"},
    ]
    for theme in ("light", "dark"):
        steps += [
            {"eval": f"document.documentElement.dataset.theme = '{theme}'; 1"},
            {"eval": "(() => { const mb = document.querySelector('#levain-modal .modal-body'); mb.scrollTop = 0; mb.scrollTop = 260; return 1; })()"},
            {"eval": "new Promise(r => setTimeout(() => r(1), 300))"},
            {"eval": _BAR_LAYERS},
            {"shot": str(shots / f"{theme}.png")},
        ]
    with _serve(src, write_token=TOKEN, surface_tokens={"browser": TOKEN}, install_class="reported") as (url, _httpd):
        out = _drive(tmp_path, f"{url}/", steps)
    probes = [json.loads(x) for x in out["values"] if isinstance(x, str) and '"probe"' in x]
    assert [p["theme"] for p in probes] == ["light", "dark"], probes
    for p in probes:
        assert p["scrolled"] > 0, p
        assert p["alpha"] == 1, p                                      # opaque, not a tint a row shows through
        assert all(p["owned"]), p                                      # at every point across the bar the bar is what is on top
        assert p["aboveRow"] is False and p["gap"] <= 0.5, p           # no row text in a strip above it
        assert p["hair"].startswith("1px") and "solid" in p["hair"], p # and a hairline under it


# --- the List goes time-first (plan 1109): Now / Later / Someday / Done, the type a tag on every row and a filter across them --------


def _seed_time(root: Path) -> None:
    """One row of each time-state a page must draw: a dated board drop, a dated talk, a dated band-bound drop, one resolved row."""
    from datetime import date, datetime, timedelta, timezone

    st = SporeStore(root / ".levain" / "memory.spores.json")
    d = date.today()
    st.add(type="task", text="draft the BOBS note for the board", disposition="agenda", target="board:levain",
           next=(d + timedelta(days=9)).isoformat() + "T09:00")
    st.add(type="task", text="talk about the dryer", disposition="seed", attention="talk", next=(d + timedelta(days=2)).isoformat())
    st.add(type="task", text="send Jeanne the invoice", disposition="agenda", target="band", next=(d + timedelta(days=1)).isoformat())
    done = st.add(type="task", text="water the plants", disposition="seed")["id"]
    st.descend(done, kind="done", now=datetime.now(timezone.utc) - timedelta(days=2))


_FOCUS = "document.querySelector('#levain-modal .modal-body')"
_FOCUS_ROWS = f"[...{_FOCUS}.querySelectorAll('.sp-results .row')]"


def _chip(kind: str, value: str) -> str:
    return f"{_FOCUS}.querySelector('.list-{kind}-chip[data-{kind}=\"{value}\"]')"


def _open_focus() -> list[dict[str, Any]]:
    return [{"eval": f"{TRAY}.querySelector('.pexpand').click(); 1"},
            {"until": f"{_FOCUS} && {_FOCUS}.querySelector('.sp-results .row') && 1"},
            {"eval": "new Promise(r => setTimeout(() => r(1), 900))"}]            # the dialog's fade settles


@needs_browser
def test_the_list_is_time_first_with_a_type_tag_on_every_row_and_the_type_chips_cut_across(tmp_path: Path) -> None:
    root, src, _ids = _install(tmp_path)
    _seed_more(root)
    _seed_phone(root)
    _seed_time(root)
    shots = Path(os.environ.get("LEVAIN_LIST_SHOTS") or tmp_path)
    shots.mkdir(parents=True, exist_ok=True)
    rows_of = (f"{_FOCUS_ROWS}.map(r => ({{t: r.dataset.timeState || '', s: r.dataset.surface || '', tag: !!r.querySelector('.list-surface'), "
               "verbs: r.querySelectorAll('.verb-btn').length, line: ((r.querySelector('[data-ck-tag-state=\"later\"]') || {}).textContent || '')}))")
    with _serve(src, write_token=TOKEN, surface_tokens={"browser": TOKEN}, install_class="reported") as (url, _httpd):
        out = _drive(tmp_path, f"{url}/", [
            {"emulate": {"w": 1280, "h": 1000, "dpr": 1}},
            *_reload(),
            {"until": f"{TRAY} && {TRAY}.querySelectorAll('.sp-results .row[data-surface=band]').length > 1 && 1"},
            *_open_focus(),
            {"shot": str(shots / "focus_time_first_1280.png")},
            {"eval": f"JSON.stringify({{rows: {rows_of}, chips: [...{_FOCUS}.querySelectorAll('.list-time-chip')].map(c => c.textContent)}})"},
            {"eval": f"{_chip('time', 'later')}.click(); 1"},
            {"eval": f"JSON.stringify({{later: {rows_of}, subs: [...{_FOCUS}.querySelectorAll('.grp-sub')].map(h => h.textContent)}})"},
            {"eval": f"{_chip('surface', 'talk')}.click(); 1"},               # a type chip cuts across the time-state
            {"eval": f"JSON.stringify({{talk_later: {rows_of}}})"},
            {"eval": f"{_chip('time', 'later')}.click(); {_chip('surface', '')}.click(); {_chip('time', 'done')}.click(); 1"},
            {"eval": f"JSON.stringify({{done: {rows_of}}})"},
            {"emulate": {"w": 390, "h": 844, "dpr": 2, "mobile": True}},
            {"eval": "new Promise(r => setTimeout(() => r(1), 900))"},
            {"shot": str(shots / "focus_time_first_390.png")},
        ])
    vals = {k: v for x in out["values"] if isinstance(x, str) and x.startswith("{") for k, v in json.loads(x).items()}
    allrows = vals["rows"]
    assert allrows and all(r["tag"] and r["s"] for r in allrows), allrows            # a type tag on every row, in the focus view
    assert [c.split(" ")[0] for c in vals["chips"]] == ["Now", "Later", "Someday", "Done"], vals["chips"]
    later = vals["later"]
    assert later and all(r["t"] == "later" for r in later), later                    # the chip narrows to Later, whatever the source
    assert {r["s"] for r in later} == {"drop", "talk", "spore"}, later               # a drop, a talk and a plain dated seed
    assert all(r["line"].startswith("wakes ") and "→" in r["line"] for r in later), later
    assert vals["subs"] == ["Tomorrow", "Next 7 days", "Later dates"], vals["subs"]
    assert vals["talk_later"] and all(r["s"] == "talk" and r["t"] == "later" for r in vals["talk_later"]), vals["talk_later"]
    done = vals["done"]
    assert len(done) == 1 and done[0]["t"] == "done" and done[0]["verbs"] == 0, done     # read-only: no verb on a closed row


@needs_browser
def test_a_snooze_moves_the_row_to_later_at_once_with_its_wake_line(tmp_path: Path) -> None:
    from datetime import date, timedelta

    root, src, _ids = _install(tmp_path)
    _seed_more(root)
    day = (date.today() + timedelta(days=4)).isoformat()
    row = f"[...{TRAY}.querySelectorAll('.sp-results .row')].find(r => (r.querySelector('.clause') || {{}}).textContent === 'call the dentist')"
    with _serve(src, write_token=TOKEN, surface_tokens={"browser": TOKEN}, install_class="reported") as (url, _httpd):
        out = _drive(tmp_path, f"{url}/", [
            {"emulate": {"w": 1280, "h": 1000, "dpr": 1}},
            *_reload(),
            {"until": f"{TRAY} && {row} && 1"},
            {"eval": f"JSON.stringify({{before: {row}.dataset.timeState}})"},
            {"eval": f"{row}.querySelector('.verb-btn[data-verb=\"spore_surface_at\"]').click(); 1"},
            {"eval": f"(() => {{ const i = {row}.querySelector('.verb-date'); i.value = {json.dumps(day)}; "
                     f"i.dispatchEvent(new Event('input', {{bubbles: true}})); {row}.querySelector('.verb-confirm').click(); return 1; }})()"},
            # the write re-reads the page: the row has left the card (the card holds what is due) with no wait for a poll
            {"until": f"!({row}) && 1"},
            *_open_focus(),
            {"eval": f"{_chip('time', 'later')}.click(); 1"},
            {"eval": f"JSON.stringify({_FOCUS_ROWS}.map(r => ({{text: (r.querySelector('.clause') || {{}}).textContent, t: r.dataset.timeState, "
                     "line: ((r.querySelector('[data-ck-tag-state=\"later\"]') || {}).textContent || '')})))"},
        ])
    moved = json.loads(out["values"][-1])
    assert next(json.loads(x) for x in out["values"] if isinstance(x, str) and x.startswith('{"before"'))["before"] == "now"
    mine = next(r for r in moved if r["text"] == "call the dentist")
    assert mine["t"] == "later" and mine["line"].startswith("wakes ") and mine["line"].endswith("→ desk"), mine


# --- the first fix round (levain 7, 1109-26): chips that add up, a stale type choice, two labelled rows --------------------------

_CHIP_SWEEP = f"""(async () => {{
  const mb = {_FOCUS}; const sleep = (ms) => new Promise(r => setTimeout(r, ms));
  const num = (c) => parseInt(c.textContent.trim().split(' ').pop(), 10);
  const shown = () => [...mb.querySelectorAll('.sp-results .row')].filter(r => r.getClientRects().length).length;
  const out = [];
  for (const t of ['', 'now', 'later', 'someday', 'done']) {{
    const on = mb.querySelector('.list-time-chip[aria-pressed="true"]');
    const want = t ? mb.querySelector('.list-time-chip[data-time="' + t + '"]') : null;
    if (t && !want) continue;
    if (on && on !== want) on.click();
    if (want && want.getAttribute('aria-pressed') !== 'true') want.click();
    await sleep(80);
    const chips = [...mb.querySelectorAll('.list-surface-chip')];
    const all = chips.find((c) => c.dataset.surface === '');
    const per = [];
    for (const c of chips.filter((x) => x.dataset.surface !== '')) {{ c.click(); await sleep(60); per.push([c.dataset.surface, num(c), shown()]); }}
    all.click(); await sleep(60);
    out.push({{t, timeCount: want ? num(want) : null, all: num(all), shownAll: shown(), per}});
  }}
  return JSON.stringify({{sweep: out}});
}})()"""


@needs_browser
def test_the_type_chips_add_up_to_all_in_every_time_state_and_each_count_is_the_rows_its_tap_shows(tmp_path: Path, monkeypatch: Any) -> None:
    root, src, _ids = _install(tmp_path)
    _seed_more(root)
    _seed_phone(root)
    _seed_time(root)
    from levain.consent.questions import QuestionStore
    QuestionStore(default_queue_dir()).file(agent="claude_code", agent_session_id="s8", what="desk: carried Launch the TAOSERV upgrade seat.",
                                            options=[("ship", "Ship it"), ("hold", "Hold")], rec="hold", blocks="the upgrade", cwd="/w/taoserv")
    _host_band(monkeypatch)    # a carried desk row: the focus view draws it under Needs you and again under Desk owes
    with _serve(src, write_token=TOKEN, surface_tokens={"browser": TOKEN}, install_class="reported") as (url, _httpd):
        out = _drive(tmp_path, f"{url}/", [
            {"emulate": {"w": 1280, "h": 1000, "dpr": 1}},
            *_reload(),
            {"until": f"{TRAY} && {TRAY}.querySelector('[data-list-more]') && 1"},
            *_open_focus(),
            {"eval": _CHIP_SWEEP},
        ])
    sweep = json.loads(out["values"][-1])["sweep"]
    assert [s["t"] for s in sweep] == ["", "now", "later", "someday", "done"], sweep
    for s in sweep:
        assert sum(n for _k, n, _shown in s["per"]) == s["all"], s                          # the type chips sum to all, exactly
        assert all(n == shown for _k, n, shown in s["per"]), s                              # each count is the rows its tap shows
        assert s["shownAll"] == s["all"], s
        if s["timeCount"] is not None:
            assert s["all"] == s["timeCount"], s                                            # and all is the time chip's own number
    assert any(k == "band" and n >= 2 for k, n, _s in sweep[0]["per"]), sweep[0]           # the band chip counts the desk's and the later band rows too


_TIME_UNDER_SURFACE = f"""(async () => {{
  const mb = {_FOCUS}; const sleep = (ms) => new Promise(r => setTimeout(r, ms));
  const num = (c) => parseInt(c.textContent.trim().split(' ').pop(), 10);
  const shown = () => [...mb.querySelectorAll('.sp-results .row')].filter(r => r.getClientRects().length).length;
  const out = [];
  for (const k of [...mb.querySelectorAll('.list-surface-chip')].map((c) => c.dataset.surface).filter((x) => x)) {{
    const surf = mb.querySelector('.list-surface-chip[data-surface="' + k + '"]'); if (!surf) continue;
    surf.click(); await sleep(80);
    for (const t of ['now', 'later', 'someday', 'done']) {{
      const chip = mb.querySelector('.list-time-chip[data-time="' + t + '"]');
      if (!chip) continue;
      const n = num(chip); chip.click(); await sleep(80);
      out.push([k, t, n, shown()]);
      const again = mb.querySelector('.list-time-chip[data-time="' + t + '"]');
      if (again) again.click(); await sleep(60);
    }}
    const on = mb.querySelector('.list-time-chip[aria-pressed="true"]'); if (on) {{ on.click(); await sleep(60); }}
    const allChip = mb.querySelector('.list-surface-chip[data-surface=""]'); if (allChip) {{ allChip.click(); await sleep(60); }}
  }}
  return JSON.stringify(out);
}})()"""


@needs_browser
def test_with_a_type_chip_chosen_each_time_chip_is_the_rows_its_tap_draws(tmp_path: Path, monkeypatch: Any) -> None:
    root, src, _ids = _install(tmp_path)
    _seed_more(root)
    _seed_phone(root)
    _seed_time(root)
    from levain.consent.questions import QuestionStore
    QuestionStore(default_queue_dir()).file(agent="claude_code", agent_session_id="s8", what="desk: carried Launch the TAOSERV upgrade seat.",
                                            options=[("ship", "Ship it"), ("hold", "Hold")], rec="hold", blocks="the upgrade", cwd="/w/taoserv")
    _host_band(monkeypatch)    # a carried desk row: drawn under Needs you and again under Desk owes, both of the band type
    with _serve(src, write_token=TOKEN, surface_tokens={"browser": TOKEN}, install_class="reported") as (url, _httpd):
        out = _drive(tmp_path, f"{url}/", [
            {"emulate": {"w": 1280, "h": 1000, "dpr": 1}},
            *_reload(),
            {"until": f"{TRAY} && {TRAY}.querySelector('[data-list-more]') && 1"},
            *_open_focus(),
            {"eval": _TIME_UNDER_SURFACE},
        ])
    got = json.loads(out["values"][-1])
    assert got and any(k == "band" for k, _t, _n, _s in got), got
    assert all(n == shown for _k, _t, n, shown in got), got                                 # a time chip is the rows its tap draws, a type chip chosen


_EVERY_STATE_SWEEP = f"""(async () => {{
  const mb = {_FOCUS}; const sleep = (ms) => new Promise(r => setTimeout(r, ms));
  const num = (c) => parseInt(c.textContent.trim().split(' ').pop(), 10);
  const shown = () => [...mb.querySelectorAll('.sp-results .row')].filter(r => r.getClientRects().length).length;
  const sel = mb.querySelector('select.tray-dump-type');
  const views = sel ? [...sel.options].map((o) => o.value) : [''];
  const out = []; const set = async (v) => {{ if (!sel) return; sel.value = v; sel.dispatchEvent(new Event('change', {{bubbles: true}})); await sleep(80); }};
  const clearTime = async () => {{ const on = mb.querySelector('.list-time-chip[aria-pressed="true"]'); if (on) {{ on.click(); await sleep(60); }} }};
  const clearSurface = async () => {{ const a = mb.querySelector('.list-surface-chip[data-surface=""]'); if (a) {{ a.click(); await sleep(60); }} }};
  for (const v of views) {{
    await set(v);
    for (const t of ['', 'now', 'later', 'someday', 'done']) {{
      await clearTime(); await clearSurface();
      if (t) {{ const w = mb.querySelector('.list-time-chip[data-time="' + t + '"]'); if (!w) continue; w.click(); await sleep(80); }}
      for (const c of [...mb.querySelectorAll('.list-surface-chip')]) {{
        const k = c.dataset.surface; const n = num(c); c.click(); await sleep(70);
        out.push({{view: v, time: t, surface: k, n, shown: shown()}});
      }}
      await clearSurface();
    }}
    await clearTime();
    for (const sk of [...mb.querySelectorAll('.list-surface-chip')].map((c) => c.dataset.surface).filter((x) => x)) {{
      await clearTime(); await clearSurface();
      const sc = mb.querySelector('.list-surface-chip[data-surface="' + sk + '"]'); if (!sc) continue;
      sc.click(); await sleep(70);
      for (const tk of [...mb.querySelectorAll('.list-time-chip')].map((c) => c.dataset.time)) {{
        const tc = mb.querySelector('.list-time-chip[data-time="' + tk + '"]'); if (!tc) continue;
        const n = num(tc); tc.click(); await sleep(70);
        out.push({{view: v, time: tk, surface: sk, n, shown: shown(), under: true}});
        const back = mb.querySelector('.list-time-chip[aria-pressed="true"]'); if (back) {{ back.click(); await sleep(60); }}
      }}
      await clearTime(); await clearSurface();
    }}
    await clearTime();
    for (const c of [...mb.querySelectorAll('.list-time-chip')]) {{
      const k = c.dataset.time; const n = num(c); c.click(); await sleep(70);
      out.push({{view: v, time: k, surface: '*', n, shown: shown()}});
      const again = mb.querySelector('.list-time-chip[data-time="' + k + '"]'); if (again) again.click(); await sleep(60);
    }}
  }}
  return JSON.stringify({{views, out}});
}})()"""


@needs_browser
def test_every_chip_in_every_state_is_the_rows_its_tap_draws_in_a_named_view_too(tmp_path: Path, monkeypatch: Any) -> None:
    root, src, _ids = _install(tmp_path)
    _seed_more(root)
    _seed_phone(root)
    _seed_time(root)
    from levain.consent.questions import QuestionStore
    QuestionStore(default_queue_dir()).file(agent="claude_code", agent_session_id="s8", what="desk: carried Launch the TAOSERV upgrade seat.",
                                            options=[("ship", "Ship it"), ("hold", "Hold")], rec="hold", blocks="the upgrade", cwd="/w/taoserv")
    _host_band(monkeypatch)    # a carried band row: a named view draws it once, the unnamed focus view twice
    with _serve(src, write_token=TOKEN, surface_tokens={"browser": TOKEN}, install_class="reported") as (url, _httpd):
        out = _drive(tmp_path, f"{url}/", [
            {"emulate": {"w": 1280, "h": 1000, "dpr": 1}},
            *_reload(),
            {"until": f"{TRAY} && {TRAY}.querySelector('[data-list-more]') && 1"},
            *_open_focus(),
            {"eval": _EVERY_STATE_SWEEP},
        ])
    got = json.loads(out["values"][-1])
    assert len([v for v in got["views"] if v]) >= 1, got                                    # a named view is in the sweep
    bad = [r for r in got["out"] if r["n"] != r["shown"]]
    assert not bad, bad
    assert any(r["view"] and r["surface"] == "band" for r in got["out"]), got               # the carried band row's chip was tapped inside a view
    assert any(r.get("under") and r["view"] for r in got["out"]) and any(r.get("under") and not r["view"] for r in got["out"]), got   # a time chip tapped under each chosen type, in a view and out


_KEYBOARD_FOCUS = f"""(async () => {{
  const mb = {_FOCUS}; const sleep = (ms) => new Promise(r => setTimeout(r, ms));
  const out = [];
  const same = (a) => !!a && a.dataset && (a.dataset.surface !== undefined ? '[data-surface="' + a.dataset.surface + '"]' : '[data-time="' + a.dataset.time + '"]');
  for (const sel of ['.list-surface-chip[data-surface="band"]', '.list-time-chip[data-time="now"]', '.list-surface-chip[data-surface=""]']) {{
    const c = mb.querySelector(sel); if (!c) {{ out.push([sel, 'absent']); continue; }}
    c.focus(); c.click(); await sleep(100);
    const a = document.activeElement;
    out.push([sel, a && a.matches ? a.matches(sel) : false, !!(a && mb.contains(a)), a === c]);
  }}
  return JSON.stringify(out);
}})()"""


@needs_browser
def test_a_chip_tap_leaves_keyboard_focus_on_the_replacement_chip(tmp_path: Path, monkeypatch: Any) -> None:
    root, src, _ids = _install(tmp_path)
    _seed_more(root)
    _seed_phone(root)
    _seed_time(root)
    _host_band(monkeypatch)
    with _serve(src, write_token=TOKEN, surface_tokens={"browser": TOKEN}, install_class="reported") as (url, _httpd):
        out = _drive(tmp_path, f"{url}/", [
            {"emulate": {"w": 1280, "h": 1000, "dpr": 1}},
            *_reload(),
            {"until": f"{TRAY} && {TRAY}.querySelector('[data-list-more]') && 1"},
            *_open_focus(),
            {"eval": _KEYBOARD_FOCUS},
        ])
    got = json.loads(out["values"][-1])
    assert got and all(r[1] is True and r[2] is True and r[3] is False for r in got), got     # the same chip, rebuilt, still holds focus


@needs_browser
def test_a_time_state_the_chosen_type_has_no_rows_in_offers_no_chip(tmp_path: Path, monkeypatch: Any) -> None:
    root, src, _ids = _install(tmp_path)
    _seed_more(root)
    _seed_phone(root)
    _seed_time(root)
    _host_band(monkeypatch)
    with _serve(src, write_token=TOKEN, surface_tokens={"browser": TOKEN}, install_class="reported") as (url, _httpd):
        out = _drive(tmp_path, f"{url}/", [
            {"emulate": {"w": 1280, "h": 1000, "dpr": 1}},
            *_reload(),
            {"until": f"{TRAY} && {TRAY}.querySelector('[data-list-more]') && 1"},
            *_open_focus(),
            {"eval": f"{_chip('time', 'now')}.click(); {_chip('surface', 'talk')}.click(); 1"},
            {"eval": f"JSON.stringify({{doneUnderTalk: !!{_chip('time', 'done')}}})"},
            {"eval": f"{_chip('surface', '')}.click(); {_chip('time', 'done')}.click(); 1"},
            {"eval": f"JSON.stringify({{rows: {_FOCUS_ROWS}.filter(r => r.getClientRects().length).length, "
                     f"done: parseInt({_chip('time', 'done')}.textContent.trim().split(' ').pop(), 10), "
                     f"talk: !!{_chip('surface', 'talk')}, pressed: [...{_FOCUS}.querySelectorAll('.list-surface-chip[aria-pressed=\"true\"]')].map(c => c.textContent)}})"},
        ])
    under = json.loads([v for v in out["values"] if isinstance(v, str) and "doneUnderTalk" in v][0])
    assert under["doneUnderTalk"] is False, under              # under a chosen type, a time-state it has no rows in has no chip to tap
    got = json.loads(out["values"][-1])
    assert got["talk"] is False and got["rows"] == got["done"] and got["rows"] > 0, got       # no talk rows are Done: the type chip is gone, all shows
    assert got["pressed"] and got["pressed"][0].startswith("all "), got


@needs_browser
def test_the_two_chip_rows_are_labelled_when_and_type_at_390_and_1280(tmp_path: Path, monkeypatch: Any) -> None:
    root, src, _ids = _install(tmp_path)
    _seed_more(root)
    _seed_phone(root)
    _seed_time(root)
    _host_band(monkeypatch)
    shots = Path(os.environ.get("LEVAIN_LIST_SHOTS") or tmp_path)
    shots.mkdir(parents=True, exist_ok=True)
    labels = f"[...{_FOCUS}.querySelectorAll('.list-chips-label')].map(n => n.textContent + '@' + n.parentElement.className)"
    with _serve(src, write_token=TOKEN, surface_tokens={"browser": TOKEN}, install_class="reported") as (url, _httpd):
        out = _drive(tmp_path, f"{url}/", [
            {"emulate": {"w": 1280, "h": 1000, "dpr": 1}},
            *_reload(),
            {"until": f"{TRAY} && {TRAY}.querySelector('[data-list-more]') && 1"},
            *_open_focus(),
            {"eval": f"JSON.stringify({{labels: {labels}}})"},
            {"shot": str(shots / "list_chips_1280.png")},
            {"emulate": {"w": 390, "h": 844, "dpr": 2, "mobile": True}},
            {"eval": "new Promise(r => setTimeout(() => r(1), 900))"},
            {"shot": str(shots / "list_chips_390.png")},
            {"eval": f"JSON.stringify({{narrow: {labels}, hs: document.documentElement.scrollWidth <= window.innerWidth + 1}})"},
        ])
    vals = {k: v for x in out["values"] if isinstance(x, str) and x.startswith("{") for k, v in json.loads(x).items()}
    assert vals["labels"] == ["When@list-times", "Type@list-surfaces"], vals
    assert vals["narrow"] == vals["labels"] and vals["hs"] is True, vals


@needs_browser
def test_a_done_row_says_its_kind_once_and_a_later_drop_says_where_it_lands_once(tmp_path: Path) -> None:
    root, src, _ids = _install(tmp_path)
    _seed_more(root)
    _seed_time(root)
    metas = f"{_FOCUS_ROWS}.map(r => [...r.querySelectorAll('.list-meta [data-ck-tag-state], .list-meta .tag, .list-meta > *')].map(n => n.textContent.trim()).join(' | '))"
    with _serve(src, write_token=TOKEN, surface_tokens={"browser": TOKEN}, install_class="reported") as (url, _httpd):
        out = _drive(tmp_path, f"{url}/", [
            {"emulate": {"w": 1280, "h": 1000, "dpr": 1}},
            *_reload(),
            {"until": f"{TRAY} && {TRAY}.querySelector('[data-list-more]') && 1"},
            *_open_focus(),
            {"eval": f"{_chip('time', 'done')}.click(); JSON.stringify({{done: {metas}}})"},
            {"eval": f"{_chip('time', 'later')}.click(); JSON.stringify({{later: {metas}}})"},
        ])
    vals = {k: v for x in out["values"] if isinstance(x, str) and x.startswith("{") for k, v in json.loads(x).items()}
    done = " ".join(vals["done"])
    assert re.search(r"\bdone \d\d-\d\d", done) and "done done" not in done, vals["done"]
    later = [m for m in vals["later"] if "board" in m]
    assert later and all("wakes " in m and "to board" not in m for m in later), vals["later"]


_CARD_FOOT = f"""(() => {{
  const f = {TRAY}.querySelector('[data-list-more]'); const t = f ? f.textContent : '';
  const n = (w) => {{ const m = new RegExp(w + ' (\\\\d+)').exec(t); return m ? parseInt(m[1], 10) : 0; }};
  return JSON.stringify({{kind: 'foot', text: t, desk: n('desk owes'), later: n('later'), done: n('done')}});
}})()"""

_FOCUS_GROUPS = f"""(() => {{
  const mb = {_FOCUS};
  const group = (title) => {{ const h = [...mb.querySelectorAll('.grp-head')].find(x => x.textContent.trim().toLowerCase().startsWith(title));
    const col = h && h.parentElement; return col ? col.querySelectorAll('.row').length : 0; }};
  return JSON.stringify({{kind: 'focus', desk: group('desk owes'), later: group('later'), done: group('done')}});
}})()"""


@needs_browser
def test_r5_with_a_type_chosen_on_the_card_each_footer_number_is_the_rows_its_tap_draws(tmp_path: Path, monkeypatch: Any) -> None:
    root, src, _ids = _install(tmp_path)
    _seed_more(root)
    _seed_phone(root)
    _seed_time(root)
    from levain.consent.questions import QuestionStore
    QuestionStore(default_queue_dir()).file(agent="claude_code", agent_session_id="s8", what="desk: carried Launch the TAOSERV upgrade seat.",
                                            options=[("ship", "Ship it"), ("hold", "Hold")], rec="hold", blocks="the upgrade", cwd="/w/taoserv")
    _host_band(monkeypatch)
    steps: list[dict[str, Any]] = []
    for surface in ("band", "spores", "talk"):
        steps += [
            {"eval": f"(() => {{ const c = {TRAY}.querySelector('.list-surface-chip[data-surface=\"{surface}\"]'); if (c) c.click(); return 1; }})()"},
            {"eval": "new Promise(r => setTimeout(() => r(1), 700))"},
            {"eval": _CARD_FOOT},
            {"eval": f"(() => {{ const f = {TRAY}.querySelector('[data-list-more]'); if (f) f.click(); return 1; }})()"},
            {"eval": "new Promise(r => setTimeout(() => r(1), 900))"},
            {"eval": _FOCUS_GROUPS},
            {"eval": "(() => { const x = document.querySelector('#levain-modal .modal-close'); if (x) x.click(); return 1; })()"},
            {"eval": "new Promise(r => setTimeout(() => r(1), 500))"},
        ]
    with _serve(src, write_token=TOKEN, surface_tokens={"browser": TOKEN}, install_class="reported") as (url, _httpd):
        out = _drive(tmp_path, f"{url}/", [
            {"emulate": {"w": 1280, "h": 1000, "dpr": 1}},
            *_reload(),
            {"until": f"{TRAY} && {TRAY}.querySelector('[data-list-more]') && 1"},
            *steps,
        ])
    seen = [json.loads(x) for x in out["values"] if isinstance(x, str) and x.startswith('{"kind"')]
    foots = [s for s in seen if s["kind"] == "foot"]
    focuses = [s for s in seen if s["kind"] == "focus"]
    assert foots and len(foots) == len(focuses) and any(f["desk"] or f["later"] or f["done"] for f in foots), seen   # a chosen type with something apart was tapped
    for foot, focus in zip(foots, focuses):
        assert (foot["desk"], foot["later"], foot["done"]) == (focus["desk"], focus["later"], focus["done"]), (foot, focus)


@needs_browser
def test_r5_a_focused_chip_with_no_replacement_hands_focus_to_the_first_chip_of_its_row(tmp_path: Path, monkeypatch: Any) -> None:
    root, src, _ids = _install(tmp_path)
    _seed_more(root)
    _seed_phone(root)
    _seed_time(root)
    _host_band(monkeypatch)
    probe = f"""(async () => {{
      const mb = {_FOCUS}; const sleep = (ms) => new Promise(r => setTimeout(r, ms));
      const c = mb.querySelector('.list-surface-chip[data-surface="band"]'); c.focus();
      c.dataset.surface = 'gone';        // a chip whose rebuilt row has no chip of its name
      c.click(); await sleep(120);
      const a = document.activeElement;
      return JSON.stringify({{inRow: !!(a && a.closest && a.closest('.list-surfaces')), first: a === mb.querySelector('.list-surfaces button'), tag: a && a.tagName}});
    }})()"""
    with _serve(src, write_token=TOKEN, surface_tokens={"browser": TOKEN}, install_class="reported") as (url, _httpd):
        out = _drive(tmp_path, f"{url}/", [
            {"emulate": {"w": 1280, "h": 1000, "dpr": 1}},
            *_reload(),
            {"until": f"{TRAY} && {TRAY}.querySelector('[data-list-more]') && 1"},
            *_open_focus(),
            {"eval": probe},
        ])
    got = json.loads(out["values"][-1])
    assert got["inRow"] is True and got["first"] is True, got


@needs_browser
def test_r6_a_time_chosen_in_the_focus_view_leaves_the_card_footer_counting_what_its_tap_draws(tmp_path: Path, monkeypatch: Any) -> None:
    root, src, _ids = _install(tmp_path)
    _seed_more(root)
    _seed_phone(root)
    _seed_time(root)
    from levain.consent.questions import QuestionStore
    QuestionStore(default_queue_dir()).file(agent="claude_code", agent_session_id="s8", what="desk: carried Launch the TAOSERV upgrade seat.",
                                            options=[("ship", "Ship it"), ("hold", "Hold")], rec="hold", blocks="the upgrade", cwd="/w/taoserv")
    _host_band(monkeypatch)
    close = {"eval": "(() => { const x = document.querySelector('#levain-modal .modal-close'); if (x) x.click(); return 1; })()"}
    settle = {"eval": "new Promise(r => setTimeout(() => r(1), 600))"}
    steps: list[dict[str, Any]] = []
    for time in ("later", "someday", "done", "now"):
        steps += [
            *_open_focus(),
            {"eval": f"(() => {{ const c = {_chip('time', time)}; if (c) c.click(); return 1; }})()"},
            {"eval": "new Promise(r => setTimeout(() => r(1), 400))"},
            close, settle,
            {"eval": _CARD_FOOT},                                  # what the card says after the time was chosen in the focus view
            *_open_focus(),
            {"eval": _FOCUS_GROUPS},                               # what its tap then draws
            close, settle,
        ]
    with _serve(src, write_token=TOKEN, surface_tokens={"browser": TOKEN}, install_class="reported") as (url, _httpd):
        out = _drive(tmp_path, f"{url}/", [
            {"emulate": {"w": 1280, "h": 1000, "dpr": 1}},
            *_reload(),
            {"until": f"{TRAY} && {TRAY}.querySelector('[data-list-more]') && 1"},
            *steps,
        ])
    seen = [json.loads(x) for x in out["values"] if isinstance(x, str) and x.startswith('{"kind"')]
    foots = [s for s in seen if s["kind"] == "foot"]
    focuses = [s for s in seen if s["kind"] == "focus"]
    assert len(foots) == 4 and len(focuses) == 4, seen
    assert len({(f["desk"], f["later"], f["done"]) for f in focuses}) > 1, seen          # the time choice changed what the focus view draws
    for foot, focus in zip(foots, focuses):
        assert (foot["desk"], foot["later"], foot["done"]) == (focus["desk"], focus["later"], focus["done"]), (foot, focus)


# --- board row 178 build 1: the "when?" form with a time, and who does it (derived from the target) -----------------------------

def _seed_when(root: Path) -> dict[str, str]:
    """Four DUE rows (a past day, so the card holds them): three plain seeds and the desk drop."""
    store = SporeStore(root / ".levain" / "memory.spores.json")
    ids = {k: store.add(type="task", text=f"when row {k}", disposition="seed", next="2020-01-01")["id"] for k in ("a", "b", "c")}
    ids["drop"] = store.add(type="task", text="who row drop", disposition="agenda", target="desk", next="2020-01-07T09:00")["id"]
    qdir = default_queue_dir()
    qdir.mkdir(parents=True, exist_ok=True, mode=0o700)
    CoordStore(qdir).coord_take()
    return ids


def _row_of(text: str) -> str:
    return f"[...{TRAY}.querySelectorAll('.sp-results .row')].find(r => (r.querySelector('.clause') || {{}}).textContent === {json.dumps(text)})"


def _open_when(text: str) -> str:
    return f"{_row_of(text)}.querySelector('.verb-btn[data-verb=\"spore_surface_at\"]').click(); 1"


@pytest.mark.parametrize("width,height,mobile", [(1280, 1000, False), (390, 844, True)])
@needs_browser
def test_when_form_writes_a_minute_from_a_quick_pick_or_a_date_and_time_and_the_who_control_sets_the_target(
        tmp_path: Path, width: int, height: int, mobile: bool) -> None:
    from datetime import date, timedelta

    root, src, _ids = _install(tmp_path)
    ids = _seed_when(root)
    tomorrow = (date.today() + timedelta(days=1)).isoformat()
    monday = date.today() + timedelta(days=((8 - ((date.today().weekday() + 1) % 7)) % 7) or 7)
    day = (date.today() + timedelta(days=9)).isoformat()
    emu = {"w": width, "h": height, "dpr": 2 if mobile else 1}
    if mobile:
        emu["mobile"] = True
    with _serve(src, write_token=TOKEN, surface_tokens={"browser": TOKEN}, install_class="reported") as (url, _httpd):
        out = _drive(tmp_path, f"{url}/", [
            {"emulate": emu},
            *_reload(),
            {"until": f"{TRAY} && {_row_of('when row a')} && {_row_of('when row b')} && {_row_of('when row c')} && 1"},
            # the form's own facts: picks, two inputs (no datetime-local), no who-chip on a seed, the who control with nothing chosen
            {"eval": _open_when("when row a")},
            {"eval": f"(() => {{ const f = {_row_of('when row a')}.querySelector('.when-form'); const b = f.getBoundingClientRect();"
                     "const q = (s) => f.querySelector(s); return JSON.stringify({form: {picks: [...f.querySelectorAll('.when-pick')].map(x => x.dataset.pick),"
                     "date: q('.verb-date').type, time: q('.verb-time').type, timeVal: q('.verb-time').value, dt: !!f.querySelector('input[type=datetime-local]'),"
                     "zone: (q('.when-zone') || {}).textContent || '',"
                     "pressed: [...f.querySelectorAll('.who-opt')].map(x => x.getAttribute('aria-pressed')), repoHidden: q('.who-repo').hidden,"
                     "noScroll: document.documentElement.scrollWidth <= window.innerWidth + 1, inside: b.right <= window.innerWidth + 1,"
                     "pickH: Math.min(...[...f.querySelectorAll('.when-pick')].map(x => x.getBoundingClientRect().height)),"
                     "timeFs: parseFloat(getComputedStyle(q('.verb-time')).fontSize)}}); })()"},
            {"shot": str(Path(os.environ.get("LEVAIN_LIST_SHOTS") or tmp_path) / f"when_form_{width}.png")},
            # one tap writes: Tomorrow 09:00
            {"eval": f"{_row_of('when row a')}.querySelector('.when-pick[data-pick=\"tomorrow\"]').click(); 1"},
            {"until": f"!({_row_of('when row a')}) && 1"},
            # a date and a time, then schedule
            {"eval": _open_when("when row b")},
            {"eval": f"(() => {{ const f = {_row_of('when row b')}.querySelector('.when-form'); const d = f.querySelector('.verb-date'); d.value = {json.dumps(day)};"
                     "d.dispatchEvent(new Event('input', {bubbles: true})); const t = f.querySelector('.verb-time'); t.value = '17:45';"
                     "t.dispatchEvent(new Event('input', {bubbles: true})); f.querySelector('.verb-confirm').click(); return 1; })()"},
            {"until": f"!({_row_of('when row b')}) && 1"},
            # a seed given a who (the machine, the desk) and a pick becomes a dated agenda on that target
            {"eval": _open_when("when row c")},
            {"eval": f"(() => {{ const f = {_row_of('when row c')}.querySelector('.when-form'); f.querySelector('.who-opt[data-who=\"the machine\"]').click();"
                     "return JSON.stringify({repo: {shown: !f.querySelector('.who-repo').hidden, val: f.querySelector('.who-repo').value}}); })()"},
            {"eval": f"{_row_of('when row c')}.querySelector('.when-pick[data-pick=\"monday\"]').click(); 1"},
            {"until": f"!({_row_of('when row c')}) && 1"},
            # the desk drop says who from its target, and the who verb hands it to you without touching its day
            {"eval": f"JSON.stringify({{chip: (({_row_of('who row drop')}.querySelector('.list-who')) || {{}}).textContent || ''}})"},
            {"eval": f"{_row_of('who row drop')}.querySelector('.verb-btn[data-verb=\"spore_retarget\"]').click(); 1"},
            {"eval": f"(() => {{ const f = {_row_of('who row drop')}.querySelector('.who-form'); const on = f.querySelector('.who-opt[aria-pressed=\"true\"]').dataset.who;"
                     "f.querySelector('.who-opt[data-who=\"you\"]').click(); f.querySelector('.verb-confirm').click(); return JSON.stringify({was: on}); })()"},
            {"until": f"(() => {{ const r = {_row_of('who row drop')}; return !!r && r.querySelector('.list-who').textContent === 'you' && 1; }})()"},
            {"eval": f"JSON.stringify({{chip2: {_row_of('who row drop')}.querySelector('.list-who').textContent}})"},
        ])
    vals = {k: v for x in out["values"] if isinstance(x, str) and x.startswith("{") for k, v in json.loads(x).items()}
    form = vals["form"]
    assert {"later", "tomorrow", "monday", "now"} <= set(form["picks"]), form
    assert (form["date"], form["time"], form["timeVal"], form["dt"]) == ("date", "time", "", False), form   # a date-only row: no time
    assert "host" in form["zone"], form                                                   # the times are the host's clock
    assert form["pressed"] == ["false", "false"] and form["repoHidden"] is True, form
    assert form["noScroll"] and form["inside"], form
    if mobile:
        assert form["pickH"] >= 44 and form["timeFs"] >= 16, form
    assert vals["repo"] == {"shown": True, "val": "desk"}, vals["repo"]
    assert vals["chip"] == "the machine" and vals["was"] == "the machine" and vals["chip2"] == "you", vals
    store = SporeStore(root / ".levain" / "memory.spores.json")
    a, b, c, drop = (store.get(ids[k]) for k in ("a", "b", "c", "drop"))
    assert (a["next"], a["disposition"]) == (tomorrow + "T09:00", "seed") and "target" not in a, a
    assert (b["next"], "target" in b) == (day + "T17:45", False), b
    assert (c["disposition"], c["target"], c["next"]) == ("agenda", "desk", monday.isoformat() + "T09:00"), c
    assert (drop["target"], drop["next"]) == ("band", "2020-01-07T09:00"), drop
    posts = [(p["verb"], p["params"]) for p in _posts(out)]
    assert ("spore_retarget", {"target": "desk", "surface_at": monday.isoformat() + "T09:00"}) in posts, posts
    assert ("spore_retarget", {"target": "band"}) in posts, posts


_CONFIRM_REACH = (
    "(() => { const f = %s.querySelector('.when-form'); const c = f.querySelector('.verb-confirm'); const r = c.getBoundingClientRect();"
    "const card = c.closest('.panel'); const body = c.closest('.pbody') || card; const v = body.getBoundingClientRect(); const cr = card.getBoundingClientRect();"
    "const top = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);"
    "return JSON.stringify({reach: {inBody: r.top >= v.top - 0.5 && r.bottom <= v.bottom + 0.5, inCard: r.top >= cr.top - 0.5 && r.bottom <= cr.bottom + 0.5,"
    "inView: r.top >= 0 && r.bottom <= window.innerHeight, topmost: top === c, topTag: top ? top.className : null,"
    "rows: [...f.querySelectorAll('.verb-confirm, .verb-cancel:not(.when-pick)')].map(x => Math.round(x.getBoundingClientRect().top)), formH: Math.round(f.getBoundingClientRect().height)}}); })()"
)


@needs_browser
def test_when_form_confirm_row_is_reachable_on_a_390_phone(tmp_path: Path) -> None:
    root, src, _ids = _install(tmp_path)
    _seed_when(root)
    with _serve(src, write_token=TOKEN, surface_tokens={"browser": TOKEN}, install_class="reported") as (url, _httpd):
        out = _drive(tmp_path, f"{url}/", [
            {"emulate": {"w": 390, "h": 844, "dpr": 2, "mobile": True}},
            *_reload(),
            {"until": f"{TRAY} && {_row_of('when row a')} && 1"},
            {"eval": _open_when("when row a")},
            {"until": f"{_row_of('when row a')}.querySelector('.when-form .verb-confirm') && 1"},
            {"eval": _CONFIRM_REACH % _row_of("when row a")},
            {"shot": str(Path(os.environ.get("LEVAIN_LIST_SHOTS") or tmp_path) / "when_form_reach_390.png")},
        ])
    reach = {k: v for x in out["values"] if isinstance(x, str) and x.startswith("{") for k, v in json.loads(x).items()}["reach"]
    assert reach["inBody"] and reach["inCard"] and reach["inView"] and reach["topmost"], reach


_FAKE_CLOCK = (
    "(() => { const Real = Date; const at = %d; const hh = %d, mm = %d; const now = new Real(2031, 4, 14, hh, mm, 0).getTime();"
    "window.Date = class extends Real { constructor(...a) { if (a.length) super(...a); else super(now); } static now() { return now; } };"
    "return 1; })()"
)


@needs_browser
def test_later_today_never_writes_tomorrow_and_is_gone_with_under_an_hour_of_today_left(tmp_path: Path) -> None:
    root, src, _ids = _install(tmp_path)
    _seed_when(root)
    later = "(() => { const b = %s.querySelector('.when-form .when-pick[data-pick=\"later\"]'); return JSON.stringify({later: b ? b.dataset.value : null}); })()"
    close = f"[...{_row_of('when row a')}.querySelectorAll('.when-form .verb-cancel')].find(x => x.textContent === 'cancel').click(); 1"
    with _serve(src, write_token=TOKEN, surface_tokens={"browser": TOKEN}, install_class="reported") as (url, _httpd):
        out = _drive(tmp_path, f"{url}/", [
            {"emulate": {"w": 1280, "h": 1000, "dpr": 1}},
            *_reload(),
            {"until": f"{TRAY} && {_row_of('when row a')} && 1"},
            {"eval": _FAKE_CLOCK % (0, 22, 0)},
            {"eval": _open_when("when row a")},
            {"eval": later % _row_of("when row a")},
            {"eval": f"[...{_row_of('when row a')}.querySelectorAll('.when-form .verb-cancel')].find(x => x.textContent === 'cancel').click(); 1"},
            {"eval": _FAKE_CLOCK % (0, 14, 10)},
            {"eval": _open_when("when row a")},
            {"eval": later.replace("later:", "early:") % _row_of("when row a")},
            {"eval": close},
            {"eval": _FAKE_CLOCK % (0, 23, 30)},
            {"eval": _open_when("when row a")},
            {"eval": later.replace("later:", "last:") % _row_of("when row a")},
        ])
    vals = {k: v for x in out["values"] if isinstance(x, str) and x.startswith("{") for k, v in json.loads(x).items()}
    assert vals["later"] == "2031-05-14T23:59", vals       # 22:00 + 3 h is clamped to today's last minute
    assert vals["early"] == "2031-05-14T17:10", vals       # an early clock keeps +3 h
    assert vals["last"] is None, vals                      # 23:30: under an hour left, no pick


@needs_browser
def test_when_form_focuses_the_day_on_a_pointer_device_and_not_on_a_touch_one(tmp_path: Path) -> None:
    root, src, _ids = _install(tmp_path)
    _seed_when(root)
    stub = ("(() => { const o = window.matchMedia.bind(window); window.matchMedia = (q) => /pointer:\\s*coarse/.test(q)"
            " ? {matches: %s, media: q, addEventListener() {}, removeEventListener() {}} : o(q); return 1; })()")
    active = f"(() => {{ const a = document.activeElement; return JSON.stringify({{%s: !!a && a.classList.contains('verb-date') && {_row_of('when row a')}.contains(a)}}); }})()"
    close = f"[...{_row_of('when row a')}.querySelectorAll('.when-form .verb-cancel')].find(x => x.textContent === 'cancel').click(); 1"
    with _serve(src, write_token=TOKEN, surface_tokens={"browser": TOKEN}, install_class="reported") as (url, _httpd):
        out = _drive(tmp_path, f"{url}/", [
            {"emulate": {"w": 1280, "h": 1000, "dpr": 1}},
            *_reload(),
            {"until": f"{TRAY} && {_row_of('when row a')} && 1"},
            {"eval": stub % "false"},
            {"eval": _open_when("when row a")},
            {"eval": active % "pointer"},
            {"eval": close},
            {"eval": stub % "true"},
            {"eval": _open_when("when row a")},
            {"eval": active % "touch"},
        ])
    vals = {k: v for x in out["values"] if isinstance(x, str) and x.startswith("{") for k, v in json.loads(x).items()}
    assert vals == {"pointer": True, "touch": False}, vals


@needs_browser
def test_when_form_keeps_a_date_only_row_date_only_and_prefills_a_timed_rows_minute(tmp_path: Path) -> None:
    """Opening "when?" on a date-only row and pressing schedule writes the day alone (no time is invented); a timed row opens
    on its own minute."""
    root, src, _ids = _install(tmp_path)
    ids = _seed_when(root)
    with _serve(src, write_token=TOKEN, surface_tokens={"browser": TOKEN}, install_class="reported") as (url, _httpd):
        out = _drive(tmp_path, f"{url}/", [
            {"emulate": {"w": 1280, "h": 1000, "dpr": 1}},
            *_reload(),
            {"until": f"{TRAY} && {_row_of('when row a')} && {_row_of('who row drop')} && 1"},
            {"eval": _open_when("who row drop")},
            {"eval": f"JSON.stringify({{dropTime: {_row_of('who row drop')}.querySelector('.when-form .verb-time').value}})"},
            {"eval": f"[...{_row_of('who row drop')}.querySelectorAll('.when-form .verb-cancel')].find(x => x.textContent === 'cancel').click(); 1"},
            {"eval": _open_when("when row a")},
            {"eval": f"(() => {{ const f = {_row_of('when row a')}.querySelector('.when-form'); const d = f.querySelector('.verb-date');"
                     "const v = d.value; f.querySelector('.verb-confirm').click(); return JSON.stringify({day: v}); })()"},
            {"until": f"(() => {{ const r = {_row_of('when row a')}; return (!r || !r.querySelector('.when-form')) && 1; }})()"},
        ])
    vals = {k: v for x in out["values"] if isinstance(x, str) and x.startswith("{") for k, v in json.loads(x).items()}
    assert vals == {"dropTime": "09:00", "day": "2020-01-01"}, vals
    posts = [(p["verb"], p["params"]) for p in _posts(out)]
    assert ("spore_surface_at", {"surface_at": "2020-01-01"}) in posts, posts
    assert SporeStore(root / ".levain" / "memory.spores.json").get(ids["a"])["next"] == "2020-01-01"

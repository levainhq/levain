"""K7, the Build tab: Sessions as a large card (who is working, what each is doing, how much context it holds, what its limits
say), the Q13 limits slot that draws only when `levain.consent.limits.panel` exists, Results beside Recent edits and Open
loops, and a bare install that shows one line per empty section and no empty card."""
from __future__ import annotations

import hashlib
import json
import re
import sys
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path

from levain.cockpit.providers import build_default_cockpit
from levain.consent.listing import read_listing
from tests.test_cockpit_flip_parity import _drive, needs_browser
from tests.test_cockpit_k2a import _install, _serve
from tests.test_cockpit_sessions import CWD_CX, SID_CC, SID_CX, _fixture, _put, _stamp

DESKTOP = {"emulate": {"w": 1440, "h": 900, "dpr": 1}}
BUILD = "#write_token=T&build"
ROWS = "[...document.querySelectorAll('#k7-sessions .k7-srow')]"


def _stem(agent: str, sid: str) -> str:
    return hashlib.sha1(f"{agent}:{sid}".encode()).hexdigest()[:24]


def _tool(root: Path, agent: str, sid: str, text: str, ago: timedelta) -> None:
    p = root / ".levain" / "sessions" / f"{_stem(agent, sid)}.log.jsonl"
    p.write_text(json.dumps({"v": 1, "at": _stamp(ago), "kind": "tool", "text": text, "cut": 0}) + "\n")


def _claude_tx(tmp_path: Path, name: str, tokens: int) -> str:
    p = tmp_path / f"{name}.jsonl"
    p.write_text(json.dumps({"type": "assistant", "message": {"model": "claude-sonnet-5-5", "usage": {
        "input_tokens": 10, "cache_read_input_tokens": tokens - 20, "cache_creation_input_tokens": 10}}}) + "\n")
    return str(p)


def _codex_tx(tmp_path: Path, name: str, tokens: int, window: int) -> str:
    p = tmp_path / f"{name}.jsonl"
    p.write_text(json.dumps({"type": "event_msg", "payload": {"type": "token_count", "info": {
        "last_token_usage": {"input_tokens": tokens}, "model_context_window": window}}}) + "\n")
    return str(p)


def _sessions_fixture(tmp_path: Path, monkeypatch):
    """The Sessions fixture, plus what a row draws: SID_CC waits on a consent request (Bash), SID_CX is live and ran Edit
    money.py a few seconds ago (Codex states its window), the quiet one last ran a tool at a known time."""
    root, src = _fixture(tmp_path, monkeypatch)
    _put(root, "claude_code", SID_CC, str(tmp_path / "work"), timedelta(minutes=3), transcript=_claude_tx(tmp_path, "cc", 124_000))
    _put(root, "codex", SID_CX, CWD_CX, timedelta(seconds=20), transcript=_codex_tx(tmp_path, "cx", 62_000, 248_000))
    _tool(root, "claude_code", SID_CC, "Write: CHANGELOG.md", timedelta(minutes=3))
    _tool(root, "codex", SID_CX, "Edit: money.py", timedelta(seconds=4))
    return root, src


def _views(src) -> dict[str, dict]:
    ck = build_default_cockpit(src)
    ck.attach_consent_rows(lambda now: read_listing(now=now))
    p = ck.panel("sessions", credential_class="none")
    return {r["facets"]["agent"] + ":" + r["facets"]["session_activity"]: r["facets"]["session_view"] for r in p["rows"]}


# --- the provider: what a row carries ------------------------------------------------------------


def test_a_session_row_carries_its_tool_what_it_waits_on_its_context_and_no_full_path(tmp_path, monkeypatch):
    _root, src = _sessions_fixture(tmp_path, monkeypatch)
    v = _views(src)
    cc, cx = v["claude_code:active"], v["codex:active"]
    assert cx["tool"] == "Edit money.py" and "waiting" not in cx                # the hook's "Tool: text" reads as one phrase
    assert cc["waiting"] == "Bash" and cc["tool"] == "Write CHANGELOG.md"       # the consent queue says what it waits on
    assert (cx["tokens"], cx["window"]) == (62_000, 248_000)                    # Codex states its window ...
    assert cc["tokens"] == 124_000 and cc["window"] is None and cc["warn"] == 200_000   # ... Claude Code does not: no invented %
    assert cc["first_seen"] and cc["last_seen"]
    blob = json.dumps(v)
    assert "/zq/full/path" not in blob and str(tmp_path) not in blob            # the panel is token-free: a hint of where, never the path
    assert cx["cwd"] == "/…/codex-work" and cc["cwd"].endswith("/work") and "…" in cc["cwd"]


def test_a_session_whose_context_cannot_be_read_says_so_and_has_no_number(tmp_path, monkeypatch):
    _root, src = _sessions_fixture(tmp_path, monkeypatch)
    q = _views(src)["claude_code:quiet"]
    assert "tokens" not in q and q["context"].startswith("context not read")


# --- the Q13 limits slot ---------------------------------------------------------------------------


def _limits_module(monkeypatch, payload):
    mod = types.ModuleType("levain.consent.limits")
    mod.panel = lambda queue_dir: payload(queue_dir) if callable(payload) else payload
    monkeypatch.setitem(sys.modules, "levain.consent.limits", mod)


def test_without_the_limits_panel_a_row_draws_no_limits_and_nothing_raises(tmp_path, monkeypatch):
    _root, src = _sessions_fixture(tmp_path, monkeypatch)
    monkeypatch.setitem(sys.modules, "levain.consent.limits", None)               # not importable here: this branch has no Q13
    assert all("limits" not in v for v in _views(src).values())


def test_the_limits_line_is_the_worst_metric_and_says_not_counting_and_halted(tmp_path, monkeypatch):
    _root, src = _sessions_fixture(tmp_path, monkeypatch)
    m = lambda key, label, value, limit, status: {"key": key, "label": label, "value": value, "limit": limit, "status": status}  # noqa: E731
    _limits_module(monkeypatch, {"as_of": datetime.now(timezone.utc).isoformat(), "sessions": [
        {"agent": "claude_code", "session": SID_CC, "last_at": None, "halted": None, "counting": True, "metrics": [
            m("active_hours", "active hours", 1.5, 5, "ok"), m("calls", "tool calls", 412, 500, "amber"), m("repeat", "repeats", 2, 10, "ok")]},
        {"agent": "codex", "session": SID_CX, "last_at": None, "halted": None, "counting": False, "metrics": []},
        {"agent": "claude_code", "session": "zq-session-id-q-8814", "last_at": None, "counting": True,
         "halted": {"kind": "tool calls", "gap": 3, "request_id": "r1"}, "metrics": [m("calls", "tool calls", 501, 500, "over")]}]})
    v = _views(src)
    assert v["claude_code:active"]["limits"] == {"counting": True, "metric": "tool calls 412/500", "status": "amber"}
    assert v["codex:active"]["limits"] == {"counting": False}
    assert v["claude_code:quiet"]["limits"]["halted"] == "tool calls"


def test_a_limits_panel_that_raises_or_returns_junk_draws_no_line(tmp_path, monkeypatch):
    _root, src = _sessions_fixture(tmp_path, monkeypatch)

    def boom(_q):
        raise RuntimeError("limits store unreadable")
    _limits_module(monkeypatch, boom)
    assert all("limits" not in v for v in _views(src).values())
    _limits_module(monkeypatch, {"sessions": "not a list"})
    assert all("limits" not in v for v in _views(src).values())


# --- the page ----------------------------------------------------------------------------------------


@needs_browser
def test_build_draws_sessions_large_with_who_what_context_and_since(tmp_path, monkeypatch):
    _root, src = _sessions_fixture(tmp_path, monkeypatch)
    probe = ("JSON.stringify({head: document.querySelector('#k7-sessions .k7-card-h').innerText.replace(/\\s+/g, ' '), "
             "gloss: document.querySelector('#k7-sessions .k7-head').innerText.replace(/\\s+/g, ' '), "
             "slot: !!document.querySelector('#k7-sessions .k7-card-h [data-k7-slot=mode]'), "
             "rows: " + ROWS + ".map(r => ({live: r.classList.contains('live'), waiting: r.classList.contains('waiting'), agent: r.querySelector('.k7-agent').textContent, "
             "repo: (r.querySelector('.k7-repo') || {}).textContent, cwd: r.querySelector('.k7-cwd').textContent, said: r.querySelector('.k7-tool').textContent, "
             "meter: r.querySelector('.k7-meter-v').textContent, fill: r.querySelector('.k7-bar > i').style.width, when: r.querySelector('.k7-when').textContent}))})")
    with _serve(src, surface_tokens={"browser": "T"}, install_class="reported") as (url, _httpd):
        out = _drive(tmp_path, f"{url}/{BUILD}", [DESKTOP, {"until": "document.querySelector('#k7-sessions .k7-srow')"}, {"eval": probe}])["values"]
    got = json.loads(out[-1])
    assert got["head"].startswith("Sessions 3 2 live · 1 quiet"), got
    assert "Who is working right now, and how much room they have left." in got["gloss"], got
    assert got["slot"] is True, got                         # the hook a later `list | topology` switch renders into
    by = {r["agent"] + ("-" + ("live" if r["live"] else "quiet")): r for r in got["rows"]}
    cx, cc, q = by["codex-live"], by["claude_code-live"], by["claude_code-quiet"]
    assert re.fullmatch(r"Edit money\.py · \d+ s ago", cx["said"]), cx
    assert cx["meter"] == "25%" and cx["fill"] == "25%" and not cx["waiting"], cx     # 62K of Codex's 248K window
    assert cc["waiting"] and cc["said"] == "waiting on a decision (Bash)" and cc["meter"] == "124K" and cc["fill"] == "62%", cc
    assert q["said"].startswith("last hook ") and q["meter"] == "not read" and q["when"].startswith("last active "), q
    assert cx["when"].startswith("since ") and cx["repo"] == "codex-work" and cx["cwd"] == "/…/codex-work", cx


@needs_browser
def test_the_limits_line_is_drawn_amber_over_and_red_not_counting(tmp_path, monkeypatch):
    _root, src = _sessions_fixture(tmp_path, monkeypatch)
    m = lambda status, value: {"key": "calls", "label": "tool calls", "value": value, "limit": 500, "status": status}  # noqa: E731
    _limits_module(monkeypatch, {"as_of": "x", "sessions": [
        {"agent": "claude_code", "session": SID_CC, "halted": None, "counting": True, "metrics": [m("amber", 412)]},
        {"agent": "codex", "session": SID_CX, "halted": None, "counting": False, "metrics": []},
        {"agent": "claude_code", "session": "zq-session-id-q-8814", "counting": True, "halted": {"kind": "tool calls"}, "metrics": []}]})
    with _serve(src, surface_tokens={"browser": "T"}, install_class="reported") as (url, _httpd):
        out = _drive(tmp_path, f"{url}/{BUILD}", [DESKTOP, {"until": "document.querySelector('#k7-sessions .k7-limits')"},
                     {"eval": "JSON.stringify([...document.querySelectorAll('#k7-sessions .k7-limits')].map(l => [l.textContent, l.className]))"}])["values"]
    assert sorted(json.loads(out[-1])) == [["halted: tool calls", "k7-limits over"], ["limits: not counting", "k7-limits over"],
                                           ["tool calls 412/500", "k7-limits amber"]]


@needs_browser
def test_without_the_limits_panel_the_page_draws_no_limits_line(tmp_path, monkeypatch):
    _root, src = _sessions_fixture(tmp_path, monkeypatch)
    monkeypatch.setitem(sys.modules, "levain.consent.limits", None)
    with _serve(src, surface_tokens={"browser": "T"}, install_class="reported") as (url, _httpd):
        out = _drive(tmp_path, f"{url}/{BUILD}", [DESKTOP, {"until": "document.querySelector('#k7-sessions .k7-srow')"},
                     {"eval": "JSON.stringify([document.querySelectorAll('#k7-sessions .k7-srow').length, document.querySelectorAll('#k7-sessions .k7-limits').length])"}])["values"]
    assert json.loads(out[-1]) == [3, 0]                 # the three rows are drawn, none carries a limits line


@needs_browser
def test_sessions_stays_a_count_tile_on_morning_and_is_not_one_on_build(tmp_path, monkeypatch):
    _root, src = _sessions_fixture(tmp_path, monkeypatch)
    tile = "[...document.querySelectorAll('.ck-widgets .ck-card.w')].find(c => /^Sessions/i.test(c.innerText))"
    probe = (f"JSON.stringify({{tile: !!{tile}, tileShown: getComputedStyle({tile}).display !== 'none', "
             "big: document.getElementById('k7-sessions') && getComputedStyle(document.getElementById('k7-sessions')).display})")
    with _serve(src, surface_tokens={"browser": "T"}, install_class="reported") as (url, _httpd):
        morning = _drive(tmp_path, f"{url}/#write_token=T&morning", [DESKTOP, {"until": tile}, {"eval": probe}])["values"][-1]
        build = _drive(tmp_path, f"{url}/{BUILD}", [DESKTOP, {"until": tile}, {"until": "document.querySelector('#k7-sessions .k7-srow')"}, {"eval": probe}])["values"][-1]
    assert json.loads(morning) == {"tile": True, "tileShown": True, "big": "none"}, morning
    assert json.loads(build) == {"tile": True, "tileShown": False, "big": "block"}, build


@needs_browser
def test_the_all_details_button_opens_the_full_list_behind_the_tile(tmp_path, monkeypatch):
    _root, src = _sessions_fixture(tmp_path, monkeypatch)
    with _serve(src, surface_tokens={"browser": "T"}, install_class="reported") as (url, _httpd):
        out = _drive(tmp_path, f"{url}/{BUILD}", [DESKTOP, {"until": "document.querySelector('#k7-sessions .k7-open')"},
                     {"eval": "document.querySelector('#k7-sessions .k7-open').click(); 1"},
                     {"until": "document.getElementById('levain-modal')"},
                     {"eval": "document.querySelectorAll('#levain-modal .sess-tx-btn').length"}])["values"]
    assert out[-1] >= 3                                   # every session, each with its transcript button


@needs_browser
def test_results_are_the_job_stores_finished_jobs_with_an_outcome_dot(tmp_path, monkeypatch):
    from levain.jobs import JobRuntime, JobStore

    root, src, _ids = _install(tmp_path)
    store = JobStore(root / ".levain" / "jobs.json")
    now = datetime.now(timezone.utc)
    for jid, verb, ok, mins in (("j1", "consult review", True, 8), ("j2", "guard test", False, 190)):
        t = (now - timedelta(minutes=mins)).isoformat()
        store.create(jid, verb, t)
        store.claim_running(jid, t)
        store.finish(jid, "done" if ok else "failed", {"ok": True} if ok else None, None if ok else "assert failed", t)
    probe = ("JSON.stringify({title: document.querySelector('[data-ck-panel=jobs] h2').textContent, "
             "rows: [...document.querySelectorAll('[data-ck-panel=jobs] .row.k7-res')].map(r => [r.querySelector('.k7-rdot').className, r.querySelector('.clause').textContent, r.querySelector('.ext-meta').textContent])})")
    with _serve(src, surface_tokens={"browser": "T"}, install_class="reported", job_runtime=JobRuntime(store)) as (url, _httpd):
        out = _drive(tmp_path, f"{url}/{BUILD}", [DESKTOP, {"until": "document.querySelector('[data-ck-panel=jobs] .row.k7-res')"}, {"eval": probe}])["values"]
    got = json.loads(out[-1])
    assert got["title"] == "Results", got
    rows = {r[1]: r for r in got["rows"]}
    assert rows["consult review finished"][0] == "k7-rdot ok" and rows["consult review finished"][2] == "8m ago", got
    assert rows["guard test failed"][0] == "k7-rdot bad", got
    assert not any("{" in r[1] for r in got["rows"]), got          # never the raw result body


@needs_browser
def test_build_places_results_left_and_edits_over_loops_right_with_loops_carrying_open_days_and_repo(tmp_path, monkeypatch):
    from levain.jobs import JobRuntime, JobStore
    from anneal_memory.spores import SporeStore

    root, src, _ids = _install(tmp_path)
    SporeStore(root / ".levain" / "memory.spores.json").add(type="task", text="loop with a repo", tier="hot", domain="ledgerline",
                                                            today=datetime.now(timezone.utc).date() - timedelta(days=3))
    store = JobStore(root / ".levain" / "jobs.json")
    t = datetime.now(timezone.utc).isoformat()
    store.create("j1", "consult review", t)
    store.claim_running("j1", t)
    store.finish("j1", "done", {"ok": True}, None, t)
    probe = ("(() => { const r = (s) => { const e = document.querySelector(s); const b = e.getBoundingClientRect(); return [Math.round(b.left), Math.round(b.top), Math.round(b.width)]; };"
             "const row = [...document.querySelectorAll('[data-ck-panel=loops] .row')].find(x => /loop with a repo/.test(x.innerText));"
             "return JSON.stringify({jobs: r('[data-ck-panel=jobs]'), edits: r('[data-ck-panel=edits]'), loops: r('[data-ck-panel=loops]'), "
             "zone: document.querySelector('.ck-zone[data-zone=operate] .zone-head').innerText.replace(/\\s+/g, ' '), "
             "open: row.querySelector('.k7-opend').textContent, chip: row.querySelector('.k7-repo').textContent, "
             "bar: getComputedStyle(row).borderLeftWidth, edits_text: document.querySelector('[data-ck-panel=edits]').innerText}); })()")
    with _serve(src, surface_tokens={"browser": "T"}, install_class="reported", job_runtime=JobRuntime(store)) as (url, _httpd):
        out = _drive(tmp_path, f"{url}/{BUILD}", [DESKTOP, {"until": "document.querySelector('[data-ck-panel=jobs] .row')"}, {"until": "document.querySelector('[data-ck-panel=loops] .row')"},
                     {"eval": "new Promise(r => setTimeout(r, 400))"}, {"eval": probe}])["values"]
    g = json.loads(out[-1])
    assert g["zone"].startswith("Results & edits") and "What the sessions produced." in g["zone"], g
    assert g["jobs"][0] < g["edits"][0] == g["loops"][0], g                       # Results is the left column; edits and loops share the right
    assert g["edits"][1] < g["loops"][1], g                                       # edits over loops
    assert abs(g["jobs"][2] - g["edits"][2]) <= 8, g                              # two halves
    assert g["open"] == "open 3 d" and g["chip"] == "ledgerline" and g["bar"] == "3px", g
    assert "Edits appear here once a session writes a file." in g["edits_text"], g


# --- a bare install --------------------------------------------------------------------------------


@needs_browser
def test_a_bare_install_says_one_line_per_section_and_draws_no_empty_card(tmp_path, monkeypatch):
    monkeypatch.setenv("LEVAIN_HOME", str(tmp_path / "levain_home"))
    _root, src, _ids = _install(tmp_path)
    probe = ("JSON.stringify({sess: document.querySelector('#k7-sessions').innerText.replace(/\\s+/g, ' '), rows: document.querySelectorAll('#k7-sessions .k7-srow').length, "
             "tabs: document.querySelectorAll('#k7-sessions .k7-srows').length, "
             "results: (document.querySelector('.k7-results-none') || {innerText: ''}).innerText.replace(/\\s+/g, ' '), "
             "zone: document.querySelector('.ck-zone[data-zone=operate] .zone-head').innerText.replace(/\\s+/g, ' ')})")
    with _serve(src, surface_tokens={"browser": "T"}, install_class="reported") as (url, _httpd):
        out = _drive(tmp_path, f"{url}/{BUILD}", [DESKTOP, {"until": "document.querySelector('#k7-sessions .k7-none')"}, {"eval": probe}])["values"]
    g = json.loads(out[-1])
    assert "No agent session yet: start Claude Code or Codex in a workspace." in g["sess"] and g["sess"].startswith("Sessions Who is working"), g
    assert g["rows"] == 0 and g["tabs"] == 0, g
    assert "No results yet" in g["results"], g
    assert g["zone"].startswith("Results & edits"), g

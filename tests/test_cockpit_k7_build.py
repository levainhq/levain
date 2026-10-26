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
from levain.consent.queue import ConsentQueue, authority_source
from tests.test_cockpit_flip_parity import _drive, needs_browser
from tests.test_cockpit_k2a import _install, _serve
from tests.test_cockpit_sessions import CWD_CC, CWD_CX, SID_CC, SID_CX, SID_Q, _fixture, _put, _stamp
from tests._h4 import h4_home

DESKTOP = {"emulate": {"w": 1440, "h": 900, "dpr": 1}}
BUILD = "#write_token=T&build"
ROWS = "[...document.querySelectorAll('#k7-sessions .k7-srow')]"
# F12b made the topology the default view of the Sessions card at 1440: these tests are about the list, so they choose it first
LIST_VIEW = {"eval": "(() => { try { localStorage.setItem('levain_cockpit_topology_mode', 'list'); } catch (_) {} return 1; })()"}


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


def test_the_token_free_panel_carries_no_path_and_no_command_text_anywhere(tmp_path, monkeypatch):
    """The privacy boundary: the panel is served without a token, so a tool event shows its NAME and, for a file tool, the
    BASENAME of the path; a shell or unknown tool shows its name alone. Serialise the whole panel and grep it."""
    root, src = _sessions_fixture(tmp_path, monkeypatch)
    _tool(root, "claude_code", SID_CC, "Bash: curl -H 'Authorization: Bearer sk-abc123' https://x", timedelta(seconds=3))
    _tool(root, "codex", SID_CX, "Edit: /Users/x/secret/money.py", timedelta(seconds=4))
    _tool(root, "claude_code", SID_Q, "Write: /Users/alice/private/key.txt", timedelta(minutes=3))
    ck = build_default_cockpit(src)
    ck.attach_consent_rows(lambda now: read_listing(now=now))
    blob = json.dumps(ck.panel("sessions", credential_class="none"))
    for leak in ("/Users", "sk-abc123", "curl", "Authorization", "secret", "private", "alice", "https://x"):
        assert leak not in blob, leak
    v = _views(src)
    assert v["codex:active"]["tool"] == "Edit money.py" and v["claude_code:quiet"]["tool"] == "Write key.txt"
    assert v["claude_code:active"]["tool"] == "Bash"


def test_a_shell_or_unknown_or_mcp_tool_shows_its_name_alone_and_a_path_shaped_name_shows_nothing(tmp_path, monkeypatch):
    root, src = _sessions_fixture(tmp_path, monkeypatch)
    for text, want in (("mcp__srv__lookup: {\"q\": \"/Users/x\"}", "mcp__srv__lookup"), ("apply_patch: /Users/x/a/b.py", "apply_patch b.py"),
                       ("shell: rm -rf /Users/x", "shell"), ("/Users/x/y: z", "a tool"), ("Read", "Read"),
                       ("Edit: C:\\Users\\x\\money.py", "Edit money.py"), ("Edit", "Edit"), ("N" * 200, "a tool")):
        _tool(root, "codex", SID_CX, text, timedelta(seconds=4))
        assert _views(src)["codex:active"]["tool"] == want, (text, want)


def test_two_pending_requests_on_one_session_carry_the_count_and_the_newest_name(tmp_path, monkeypatch):
    _root, src = _sessions_fixture(tmp_path, monkeypatch)
    from datetime import timedelta as td
    home = tmp_path / "levain_home"
    q = ConsentQueue(home / "consent", authority=authority_source(home / "consent", anchor=None), home=h4_home())
    q.file(agent="claude_code", agent_session_id=SID_CC, tool_name="Edit", tool_input={"file_path": "/x/y"}, cwd=CWD_CC,
           hold=td(minutes=5), event_id="e2")
    v = _views(src)["claude_code:active"]
    assert v["waiting"] == "Edit" and v["waiting_n"] == 2, v                 # the newest first; the other is counted


def test_a_waiting_tool_name_is_filtered_and_capped(tmp_path, monkeypatch):
    _root, src = _sessions_fixture(tmp_path, monkeypatch)
    home = tmp_path / "levain_home"
    q = ConsentQueue(home / "consent", authority=authority_source(home / "consent", anchor=None), home=h4_home())
    q.file(agent="codex", agent_session_id=SID_CX, tool_name="x" * 500 + "\x1b[31m", tool_input={}, cwd=CWD_CX,
           hold=timedelta(minutes=5), event_id="e3")
    w = _views(src)["codex:active"]["waiting"]
    assert len(w) <= 60 and w.isprintable(), w


def test_a_session_row_ships_its_view_once(tmp_path, monkeypatch):
    from levain.cockpit.engine import ReadContext
    from levain.cockpit.providers import _sessions

    _root, src = _sessions_fixture(tmp_path, monkeypatch)
    got = _sessions(src, lambda: (lambda now: read_listing(now=now)))(ReadContext(datetime.now(timezone.utc)))
    assert got.rows and all("session_view" in r.facets and "view" not in r.stored and r.stored["view_sig"] for r in got.rows)


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


def test_a_limits_panel_that_is_present_but_unreadable_says_every_row_is_not_counting(tmp_path, monkeypatch):
    """Absent module = no line (the first test above); a PRESENT panel that raises or returns junk is a budget nobody is
    watching, and it is said on every row, never drawn as health."""
    _root, src = _sessions_fixture(tmp_path, monkeypatch)

    def boom(_q):
        raise RuntimeError("limits store unreadable")
    _limits_module(monkeypatch, boom)
    assert all(v["limits"] == {"counting": False} for v in _views(src).values())
    _limits_module(monkeypatch, {"sessions": "not a list"})
    assert all(v["limits"] == {"counting": False} for v in _views(src).values())


def test_a_limits_entry_without_a_counting_key_is_not_counting_and_halted_comes_with_it(tmp_path, monkeypatch):
    _root, src = _sessions_fixture(tmp_path, monkeypatch)
    _limits_module(monkeypatch, {"sessions": [
        {"agent": "claude_code", "session": SID_CC, "metrics": []},                                  # no `counting` key
        {"agent": "codex", "session": SID_CX, "counting": False, "halted": {"kind": "tool calls"}, "metrics": []}]})
    v = _views(src)
    assert v["claude_code:active"]["limits"] == {"counting": False}
    assert v["codex:active"]["limits"] == {"counting": False, "halted": "tool calls"}     # the blocking fact AND the other


def test_a_limit_whose_value_is_none_reads_unknown_and_its_label_is_capped(tmp_path, monkeypatch):
    _root, src = _sessions_fixture(tmp_path, monkeypatch)
    _limits_module(monkeypatch, {"sessions": [{"agent": "claude_code", "session": SID_CC, "counting": True, "metrics": [
        {"key": "k", "label": "L" * 300, "value": None, "limit": 5, "status": "ok"}]}]})
    line = _views(src)["claude_code:active"]["limits"]
    assert "None" not in line["metric"] and line["metric"].endswith(" unknown/5") and len(line["metric"]) < 80, line


def test_results_are_worded_and_ordered_by_when_the_job_finished_not_when_it_was_filed(tmp_path, monkeypatch):
    from levain.jobs import JobStore

    root, src, _ids = _install(tmp_path)
    store = JobStore(root / ".levain" / "jobs.json")
    now = datetime.now(timezone.utc)
    iso = lambda mins: (now - timedelta(minutes=mins)).isoformat()   # noqa: E731
    store.create("slow", "long check", iso(200)); store.claim_running("slow", iso(200))
    store.finish("slow", "done", {"ok": True}, None, iso(1))                    # filed first, finished LAST
    store.create("fast", "short check", iso(10)); store.claim_running("fast", iso(10))
    store.finish("fast", "done", {"ok": True}, None, iso(5))
    store.create("wait", "queued check", iso(2))                                 # pending: not a result yet
    p = build_default_cockpit(src, job_store=store).panel("jobs", credential_class="none")
    at = {r["title"]: r["facets"]["at"] for r in p["rows"]}
    assert at["long check done"] == iso(1) and at["short check done"] == iso(5) and at["queued check pending"] == iso(2), at
    done = [r["title"] for r in p["rows"] if r["facets"]["job_status"] == "done"]
    assert done == ["long check done", "short check done"], done


def test_the_open_days_chip_reads_the_integer_the_kernel_sends_not_the_badge_words(tmp_path, monkeypatch):
    """The "open N d" chip once parsed the badge text ("3d old"), whose producer is cockpit_view.js `badgeText`; it now
    reads the row's integer `age_days` facet, which the spores provider computes from the spore's own created date."""
    from anneal_memory.spores import SporeStore

    root, src, _ids = _install(tmp_path)
    SporeStore(root / ".levain" / "memory.spores.json").add(type="task", text="aged loop", tier="hot", domain="ledgerline",
                                                            today=datetime.now().astimezone().date() - timedelta(days=3))
    rows = build_default_cockpit(src).panel("loops", credential_class="none")["rows"]
    mine = [r for r in rows if "aged loop" in (r["body"] or r["title"])]
    assert mine and mine[0]["facets"]["age_days"] == 3 and mine[0]["facets"]["domain"] == "ledgerline"
    js = (Path(__file__).resolve().parent.parent / "levain" / "templates" / "web" / "cockpit_view.js").read_text()
    assert "age_days: Number.isInteger(f.age_days)" in js             # the component receives the integer


# --- the page ----------------------------------------------------------------------------------------


@needs_browser
def test_build_draws_sessions_large_with_who_what_context_and_since(tmp_path, monkeypatch):
    _root, src = _sessions_fixture(tmp_path, monkeypatch)
    probe = ("JSON.stringify({head: document.querySelector('#k7-sessions .k7-card-h').innerText.replace(/\\s+/g, ' '), "
             "gloss: document.querySelector('#k7-sessions .k7-head').innerText.replace(/\\s+/g, ' '), "
             "rows: " + ROWS + ".map(r => ({live: r.classList.contains('live'), waiting: r.classList.contains('waiting'), agent: r.querySelector('.k7-agent').textContent, "
             "repo: (r.querySelector('.k7-repo') || {}).textContent, cwd: r.querySelector('.k7-cwd').textContent, said: r.querySelector('.k7-tool').textContent, "
             "meter: r.querySelector('.k7-meter-v').textContent, fill: r.querySelector('.k7-bar > i').style.width, when: r.querySelector('.k7-when').textContent}))})")
    with _serve(src, surface_tokens={"browser": "T"}, install_class="reported") as (url, _httpd):
        out = _drive(tmp_path, f"{url}/{BUILD}", [DESKTOP, LIST_VIEW, {"until": "document.querySelector('#k7-sessions .k7-srow')"}, {"eval": probe}])["values"]
    got = json.loads(out[-1])
    assert got["head"].startswith("Sessions 3 2 live · 1 quiet"), got
    assert "Who is working right now, and how much room they have left." in got["gloss"], got
    by = {r["agent"] + ("-" + ("live" if r["live"] else "quiet")): r for r in got["rows"]}
    cx, cc, q = by["codex-live"], by["claude_code-live"], by["claude_code-quiet"]
    assert re.fullmatch(r"Edit money\.py · \d+ s ago", cx["said"]), cx
    assert cx["meter"] == "25%" and cx["fill"] == "25%" and not cx["waiting"], cx     # 62K of Codex's 248K window
    assert cc["waiting"] and cc["said"] == "waiting on a decision (Bash)" and cc["meter"] == "124K of 200K warn" and cc["fill"] == "62%", cc
    assert q["said"].startswith("last hook ") and q["meter"] == "not read" and q["when"].startswith("last active "), q
    assert cx["when"].startswith("since ") and cx["repo"] == "codex-work" and cx["cwd"] == "/…/codex-work", cx


@needs_browser
def test_a_session_at_the_warn_line_is_marked_over_for_both_agents_and_the_label_says_what_it_is_measured_against(tmp_path, monkeypatch):
    root, src = _sessions_fixture(tmp_path, monkeypatch)
    _put(root, "claude_code", SID_CC, str(tmp_path / "work"), timedelta(minutes=3), transcript=_claude_tx(tmp_path, "cc2", 210_000))
    _put(root, "codex", SID_CX, CWD_CX, timedelta(seconds=20), transcript=_codex_tx(tmp_path, "cx2", 210_000, 258_000))
    probe = ("JSON.stringify(" + ROWS + ".map(r => [r.querySelector('.k7-agent').textContent, r.querySelector('.k7-meter').classList.contains('over'), "
             "(r.querySelector('.k7-over') || {textContent: ''}).textContent, r.querySelector('.k7-meter-v').textContent]))")
    with _serve(src, surface_tokens={"browser": "T"}, install_class="reported") as (url, _httpd):
        out = _drive(tmp_path, f"{url}/{BUILD}", [DESKTOP, LIST_VIEW, {"until": "document.querySelector('#k7-sessions .k7-srow')"}, {"eval": probe}])["values"]
    got = json.loads(out[-1])
    cx = [r for r in got if r[0] == "codex"][0]
    cc = [r for r in got if r[0] == "claude_code" and r[1]][0]                   # the live Claude Code row (the quiet one reads nothing)
    assert cx[1:] == [True, "OVER", "81%"], got                                  # 210K of a 258K window, past the 200K warn line
    assert cc[1:3] == [True, "OVER"] and cc[3] == "210K of 200K warn", got


@needs_browser
def test_a_quiet_session_with_a_pending_request_shows_waiting_and_two_pending_say_so(tmp_path, monkeypatch):
    root, src = _sessions_fixture(tmp_path, monkeypatch)
    home = tmp_path / "levain_home"
    q = ConsentQueue(home / "consent", authority=authority_source(home / "consent", anchor=None), home=h4_home())
    q.file(agent="claude_code", agent_session_id=SID_Q, tool_name="Edit", tool_input={}, cwd=CWD_CC, hold=timedelta(minutes=5), event_id="eq1")
    q.file(agent="claude_code", agent_session_id=SID_CC, tool_name="Write", tool_input={}, cwd=CWD_CC, hold=timedelta(minutes=5), event_id="e9")
    probe = "JSON.stringify(" + ROWS + ".map(r => [r.classList.contains('live'), r.classList.contains('waiting'), r.querySelector('.k7-tool').textContent]))"
    with _serve(src, surface_tokens={"browser": "T"}, install_class="reported") as (url, _httpd):
        out = _drive(tmp_path, f"{url}/{BUILD}", [DESKTOP, LIST_VIEW, {"until": "document.querySelector('#k7-sessions .k7-srow')"}, {"eval": probe}])["values"]
    rows = json.loads(out[-1])
    assert [False, True, "waiting on a decision (Edit)"] in rows, rows         # quiet, and it still waits
    assert [True, True, "waiting on a decision (Write +1)"] in rows, rows       # two pending: the newest, and one more


@needs_browser
def test_a_present_limits_panel_that_cannot_be_read_draws_not_counting_on_every_row(tmp_path, monkeypatch):
    _root, src = _sessions_fixture(tmp_path, monkeypatch)

    def boom(_q):
        raise RuntimeError("limits store unreadable")
    _limits_module(monkeypatch, boom)
    with _serve(src, surface_tokens={"browser": "T"}, install_class="reported") as (url, _httpd):
        out = _drive(tmp_path, f"{url}/{BUILD}", [DESKTOP, LIST_VIEW, {"until": "document.querySelector('#k7-sessions .k7-limits')"},
                     {"eval": "JSON.stringify([...document.querySelectorAll('#k7-sessions .k7-limits')].map(l => [l.textContent, l.className]))"}])["values"]
    assert json.loads(out[-1]) == [["limits: not counting", "k7-limits over"]] * 3


@needs_browser
def test_a_tick_moves_the_ago_words_in_place_and_keeps_the_focus_on_all_details(tmp_path, monkeypatch):
    _root, src = _sessions_fixture(tmp_path, monkeypatch)
    probe = ("JSON.stringify({focus: document.activeElement && document.activeElement.className, "
             "ago: [...document.querySelectorAll('#k7-sessions .k7-tool[data-k7-at]')].map(t => t.textContent)})")
    with _serve(src, surface_tokens={"browser": "T"}, install_class="reported") as (url, _httpd):
        out = _drive(tmp_path, f"{url}/{BUILD}", [
            DESKTOP, LIST_VIEW, {"until": "document.querySelector('#k7-sessions .k7-srow')"},   # the list is up before the focus is placed
            {"eval": "document.querySelector('#k7-sessions .k7-open').focus(); 1"}, {"eval": probe},
            {"eval": "new Promise(r => setTimeout(r, 7000))"}, {"eval": probe}])["values"]
    before, after = json.loads(out[-3]), json.loads(out[-1])
    assert before["focus"] == after["focus"] == "k7-open", (before, after)       # focus survives a tick longer than the old 5 s repaint
    assert before["ago"] != after["ago"], (before, after)                         # ...and the "N s ago" words still moved


@needs_browser
def test_the_no_results_placeholder_goes_when_a_jobs_panel_appears(tmp_path, monkeypatch):
    """The placeholder is drawn while no jobs panel exists; the moment one is on the page (a job store that appears while the
    page is open) the placeholder is removed, the same page, no reload."""
    _root, src, _ids = _install(tmp_path)
    count = "document.querySelectorAll('.k7-results-none').length"
    add = ("(() => { const g = document.querySelector('.ck-zone[data-zone=operate] .ck-zgrid'); const p = document.createElement('div'); "
           "p.className = 'panel'; p.dataset.ckPanel = 'jobs'; g.appendChild(p); return 1; })()")
    with _serve(src, surface_tokens={"browser": "T"}, install_class="reported") as (url, _httpd):
        out = _drive(tmp_path, f"{url}/{BUILD}", [DESKTOP, {"until": "document.querySelector('.k7-results-none')"}, {"eval": count},
                     {"eval": add}, {"eval": "new Promise(r => setTimeout(r, 2500))"}, {"eval": count}])["values"]
    assert (out[-4], out[-1]) == (1, 0), out


@needs_browser
def test_the_limits_line_is_drawn_amber_over_and_red_not_counting(tmp_path, monkeypatch):
    _root, src = _sessions_fixture(tmp_path, monkeypatch)
    m = lambda status, value: {"key": "calls", "label": "tool calls", "value": value, "limit": 500, "status": status}  # noqa: E731
    _limits_module(monkeypatch, {"as_of": "x", "sessions": [
        {"agent": "claude_code", "session": SID_CC, "halted": None, "counting": True, "metrics": [m("amber", 412)]},
        {"agent": "codex", "session": SID_CX, "halted": None, "counting": False, "metrics": []},
        {"agent": "claude_code", "session": "zq-session-id-q-8814", "counting": True, "halted": {"kind": "tool calls"}, "metrics": []}]})
    with _serve(src, surface_tokens={"browser": "T"}, install_class="reported") as (url, _httpd):
        out = _drive(tmp_path, f"{url}/{BUILD}", [DESKTOP, LIST_VIEW, {"until": "document.querySelector('#k7-sessions .k7-limits')"},
                     {"eval": "JSON.stringify([...document.querySelectorAll('#k7-sessions .k7-limits')].map(l => [l.textContent, l.className]))"}])["values"]
    assert sorted(json.loads(out[-1])) == [["halted: tool calls", "k7-limits over"], ["limits: not counting", "k7-limits over"],
                                           ["tool calls 412/500", "k7-limits amber"]]


@needs_browser
def test_without_the_limits_panel_the_page_draws_no_limits_line(tmp_path, monkeypatch):
    _root, src = _sessions_fixture(tmp_path, monkeypatch)
    monkeypatch.setitem(sys.modules, "levain.consent.limits", None)
    with _serve(src, surface_tokens={"browser": "T"}, install_class="reported") as (url, _httpd):
        out = _drive(tmp_path, f"{url}/{BUILD}", [DESKTOP, LIST_VIEW, {"until": "document.querySelector('#k7-sessions .k7-srow')"},
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
                                                            today=datetime.now().astimezone().date() - timedelta(days=3))
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
    src.anneal.spores_json.unlink(missing_ok=True)                  # K10: an open loop is work the Sessions card draws, so "bare" means no loop either
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


def test_results_list_newest_finish_first_even_when_the_store_orders_by_filing(tmp_path, monkeypatch):
    from levain.jobs import JobStore

    root, src, _ids = _install(tmp_path)
    store = JobStore(root / ".levain" / "jobs.json")
    now = datetime.now(timezone.utc)
    iso = lambda mins: (now - timedelta(minutes=mins)).isoformat()   # noqa: E731
    store.create("slow", "long check", iso(200)); store.claim_running("slow", iso(200))
    store.finish("slow", "done", {"ok": True}, None, iso(1))                    # filed first, finished last
    store.create("fast", "short check", iso(10)); store.claim_running("fast", iso(10))
    # finished 5 min ago but written in a +14:00 offset: a string sort puts it after the 1-minute-ago UTC one wrongly
    store.finish("fast", "done", {"ok": True}, None, (now - timedelta(minutes=5)).astimezone(timezone(timedelta(hours=14))).isoformat())
    store.create("mid", "mid check", iso(20)); store.claim_running("mid", iso(20))
    store.finish("mid", "done", {"ok": True}, None, (now - timedelta(minutes=2)).astimezone(timezone(timedelta(hours=-9))).isoformat())
    p = build_default_cockpit(src, job_store=store).panel("jobs", credential_class="none")
    assert [r["title"] for r in p["rows"]] == ["long check done", "mid check done", "short check done"], [r["title"] for r in p["rows"]]


def test_a_lease_expired_job_gets_the_instant_its_lease_ran_out_as_its_finish(tmp_path, monkeypatch):
    from levain.jobs import JobStore

    root, src, _ids = _install(tmp_path)
    store = JobStore(root / ".levain" / "jobs.json")
    now = datetime.now(timezone.utc)
    created = now - timedelta(seconds=store.lease_s + 3600)
    store.create("lost", "lost check", created.isoformat()); store.claim_running("lost", created.isoformat())
    store.create("ok", "ok check", (now - timedelta(minutes=90)).isoformat())
    store.claim_running("ok", (now - timedelta(minutes=90)).isoformat())
    store.finish("ok", "done", {"ok": True}, None, (now - timedelta(minutes=30)).isoformat())
    p = build_default_cockpit(src, job_store=store).panel("jobs", credential_class="none")
    by = {r["title"]: r for r in p["rows"]}
    lost = by["lost check failed"]
    assert datetime.fromisoformat(lost["facets"]["at"]) == created + timedelta(seconds=store.lease_s), lost["facets"]
    assert lost["facets"]["at"] != created.isoformat()
    assert [r["title"] for r in p["rows"]] == ["ok check done", "lost check failed"]


def test_an_import_error_from_inside_the_limits_module_is_present_and_broken_not_absent(tmp_path, monkeypatch):
    _root, src = _sessions_fixture(tmp_path, monkeypatch)
    import builtins
    import importlib

    real = importlib.import_module

    def fake(name, *a, **k):
        if name == "levain.consent.limits":
            raise ImportError("No module named 'some_dependency'", name="some_dependency")
        return real(name, *a, **k)
    monkeypatch.delitem(sys.modules, "levain.consent.limits", raising=False)
    monkeypatch.setattr(importlib, "import_module", fake)
    assert all(v["limits"] == {"counting": False} for v in _views(src).values())
    del builtins

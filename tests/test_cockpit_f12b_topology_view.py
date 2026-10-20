"""F12b, the work view: the topology panel's value drawn in the Build tab's Sessions card (topology_view.js). Two kinds of
test. Through the real server, the provider's own value: the Work lens is the default at 1440, the switch and what persists, a
bare install, a click on a waiting task. And a literal value handed to the renderer in the same page, for the shapes this
install's provider never sends yet (every state, a failed task, a cost, a conflict) and for the widths."""
from __future__ import annotations

import json
import re
from datetime import timedelta
from pathlib import Path

from levain.consent.listing import read_listing  # noqa: F401  (the fixtures below attach it)
from tests.test_cockpit_flip_parity import _drive, needs_browser
from tests.test_cockpit_k2a import _install, _serve
from tests.test_cockpit_k7_build import BUILD, DESKTOP, _sessions_fixture

NARROW = {"emulate": {"w": 390, "h": 800, "dpr": 1, "mobile": True}}
MID = {"emulate": {"w": 1200, "h": 900, "dpr": 1}}
TV = "document.querySelector('#k7-sessions .tv')"


def _spore(root: Path) -> None:
    from datetime import datetime

    from anneal_memory.spores import SporeStore
    SporeStore(root / ".levain" / "memory.spores.json").add(type="task", text="tighten the rounding guard", tier="hot", domain="ledgerline",
                                                            today=datetime.now().astimezone().date())


def _value() -> dict:
    """Every state the Work lens draws, a failed task with an outcome, a cost, two groups, a subagent, a conflict."""
    node = lambda i, g, state, **o: {"id": i, "group": g, "parent": o.pop("parent", None), "label": o.pop("label", f"agent {i}"),  # noqa: E731
                                     "agent": "claude_code", "model": o.pop("model", None), "state": state, "tool": None, "tokens": o.pop("tokens", None),
                                     "started": "2026-10-19T15:00:00Z", "last_event": "2026-10-19T15:10:00Z", "rate": o.pop("rate", None), "card": o.pop("card", None)}
    task = lambda i, kind, state, **o: {"id": i, "kind": kind, "label": o.pop("label", i), "state": state, "waits_on": o.pop("waits_on", []),  # noqa: E731
                                        "card": o.pop("card", None), "outcomes": o.pop("outcomes", []),
                                        "cost": o.pop("cost", {"usd": None, "tokens": None, "source": None}), "workers": o.pop("workers", [])}
    return {
        "hub": {"id": "hub", "label": "Sage"},
        "groups": [{"id": "g1", "label": "ledgerline", "repo": "ledgerline"}, {"id": "g2", "label": "tally", "repo": "tally"}],
        "nodes": [node("n1", "g1", "waiting", label="claude_code · ledgerline", card="consent:abc", model="claude-opus-5-5", tokens=412000, rate=3.25),
                  node("n2", "g1", "running", label="Explore round_cents", parent="n1", rate=0.0),
                  node("n3", "g1", "errored", label="test-runner"),
                  node("n4", "g2", "stalled", label="codex · tally", tokens=96000),
                  node("n5", "g2", "idle", label="claude_code · tally")],
        "tasks": [task("t-done", "dispatch", "done", label="tally 0.4.0 to PyPI", cost={"usd": 2.1, "tokens": None, "source": "dispatch"},
                       outcomes=[{"label": "0.4.0 published", "at": "2026-10-18T15:00:00Z", "ok": True}]),
                  task("t-run", "dispatch", "running", label="docs refresh", workers=["n2"], cost={"usd": 0.0, "tokens": 5000, "source": "ledger"}),
                  task("t-stuck", "spore", "stuck", label="constraint step"),
                  task("t-fail", "dispatch", "failed", label="guard test", outcomes=[{"label": "guard test failed", "at": "2026-10-19T15:04:00Z", "ok": False}]),
                  task("t-wait", "consent", "waiting", label="git push origin main", waits_on=["consent:abc"], card="consent:abc", workers=["n1", "n3"]),
                  task("unattached", "unattached", "running", label="unattached work", workers=["n4"])],
        "conflicts": [{"path": "money.py", "nodes": ["n1", "n4"]}], "counts": {"tokens_today": 2400000},
        "as_of": "2026-10-19T15:13:00Z", "lost_if_closed": [], "errors": [], "unreadable": [],
    }


def _paint(value: dict, lens: str = "work") -> str:
    """Replace the real body with the renderer's own output for a literal value (the page's repaint leaves it: the signature is unchanged)."""
    return ("(() => { try { localStorage.setItem('levain_cockpit_topology_lens', %s); } catch (_) {} const c = document.querySelector('#k7-sessions .k7-card'); "
            "for (const o of c.querySelectorAll('.tv, .k7-srows, .k7-none')) o.remove(); "
            "c.appendChild(window.LevainTopologyView.render(%s, {openCard: (id) => { window.__opened = id; }})); return 1; })()") % (json.dumps(lens), json.dumps(value))


def _run(tmp_path, src, steps, first=DESKTOP, until="document.querySelector('#k7-sessions .k7-card')"):
    with _serve(src, surface_tokens={"browser": "T"}, install_class="reported") as (url, _httpd):
        return _drive(tmp_path, f"{url}/{BUILD}", [first, {"until": until}, *steps])["values"]


# --- the page, through the real provider ---------------------------------------------------------------


@needs_browser
def test_the_work_lens_is_the_default_at_1440_and_draws_the_providers_own_nulls_as_not_tracked(tmp_path, monkeypatch):
    root, src = _sessions_fixture(tmp_path, monkeypatch)
    _spore(root)
    probe = ("JSON.stringify({lens: %s.dataset.lens, groups: [...%s.querySelectorAll('.tv-group')].map(g => g.dataset.group), "
             "strip: [...%s.querySelectorAll('.tv-stat')].map(s => s.dataset.k + '=' + s.textContent.replace(/\\s+/g, ' ')), "
             "cols: %s.querySelectorAll('.tv-col').length, "
             "tasks: [...%s.querySelectorAll('.tv-task')].map(t => [t.dataset.state, t.dataset.kind, "
             "(t.querySelector('.tv-cost') || {textContent: ''}).textContent, [...t.querySelectorAll('.tv-chip')].map(c => c.textContent)]), "
             "pressed: [...document.querySelectorAll('#k7-sessions .k7-mode-slot [aria-pressed=true]')].map(b => b.textContent)})") % ((TV,) * 5)
    out = _run(tmp_path, src, [{"until": TV + " && " + TV + ".querySelector('.tv-task')"}, {"eval": probe}])
    got = json.loads(out[-1])
    assert got["lens"] == "work" and got["cols"] == 0, got                              # Work, not Agents
    assert got["pressed"] == ["topology"], got                                          # topology is the default view at 1440
    assert got["groups"] == ["waiting", "stuck", "unattached"], got                     # loudest first; no task is running (a spore has no worker)
    by = {t[1]: t for t in got["tasks"]}
    assert by["consent"][0] == "waiting" and by["consent"][2] == "cost not tracked" and by["consent"][3], got   # a worker chip on it
    assert by["spore"][0] == "stuck" and by["spore"][2] == "cost not tracked" and by["spore"][3] == [], got  # a spore carries no session
    assert any(t[0] != "waiting" and t[1] == "unattached" for t in got["tasks"]), got                              # the codex session: no task, none guessed
    strip = dict(s.split("=", 1) for s in got["strip"])
    assert strip["conflicts"] == "conflicts not tracked" and strip["tokens"] == "tokens today not tracked", got
    assert strip["groups"] == "3 groups" and strip["nodes"] == "3 nodes", got


@needs_browser
def test_below_1440_the_list_is_the_default_and_the_switch_reaches_the_topology(tmp_path, monkeypatch):
    root, src = _sessions_fixture(tmp_path, monkeypatch)
    _spore(root)
    probe = ("JSON.stringify({tv: !!%s, rows: document.querySelectorAll('#k7-sessions .k7-srow').length, "
             "pressed: [...document.querySelectorAll('#k7-sessions .k7-mode-slot [aria-pressed=true]')].map(b => b.textContent)})") % TV
    out = _run(tmp_path, src, [{"until": "document.querySelector('#k7-sessions .k7-srow')"}, {"eval": probe},
                               {"eval": "document.querySelector('#k7-sessions [data-tv=topology]').click(); 1"},
                               {"until": TV}, {"eval": probe}], first=MID)
    before, after = json.loads(out[-4]), json.loads(out[-1])
    assert before == {"tv": False, "rows": 3, "pressed": ["list"]}, before
    assert after["tv"] is True and after["rows"] == 0 and after["pressed"] == ["topology"], after


@needs_browser
def test_the_view_and_the_lens_persist_across_a_reload_in_the_same_browser(tmp_path, monkeypatch):
    root, src = _sessions_fixture(tmp_path, monkeypatch)
    _spore(root)
    probe = ("JSON.stringify({tv: !!%s, rows: document.querySelectorAll('#k7-sessions .k7-srow').length, "
             "lens: %s && %s.dataset.lens})") % (TV, TV, TV)
    out = _run(tmp_path, src, [
        {"until": TV}, {"eval": "document.querySelector('#k7-sessions [data-tv=list]').click(); 1"},
        {"until": "document.querySelector('#k7-sessions .k7-srow')"},
        {"eval": "location.reload(); 1"}, {"eval": "new Promise(r => setTimeout(r, 1500))"},
        {"until": "document.querySelector('#k7-sessions .k7-card')"}, {"until": "document.querySelector('#k7-sessions .k7-srow')"}, {"eval": probe},
        {"eval": "document.querySelector('#k7-sessions [data-tv=topology]').click(); 1"}, {"until": TV},
        {"eval": "document.querySelector('#k7-sessions [data-tv=agents]').click(); 1"}, {"until": TV + ".dataset.lens == 'agents'"},
        {"eval": "location.reload(); 1"}, {"eval": "new Promise(r => setTimeout(r, 1500))"},
        {"until": TV + " && " + TV + ".querySelector('.tv-col')"}, {"eval": probe}])
    first, second = json.loads(out[-9]), json.loads(out[-1])
    assert first == {"tv": False, "rows": 3, "lens": None}, first                      # list survives a reload, at 1440
    assert second["tv"] is True and second["lens"] == "agents", second                  # topology and Agents survive one


@needs_browser
def test_a_bare_install_draws_no_switch_no_topology_and_keeps_the_one_line(tmp_path, monkeypatch):
    monkeypatch.setenv("LEVAIN_HOME", str(tmp_path / "levain_home"))
    _root, src, _ids = _install(tmp_path)
    probe = ("JSON.stringify({tv: !!%s, switch: document.querySelectorAll('#k7-sessions [data-tv]').length, "
             "quiet: !!document.querySelector('#k7-sessions .tv-quiet'), text: document.querySelector('#k7-sessions').innerText.replace(/\\s+/g, ' ')})") % TV
    out = _run(tmp_path, src, [{"until": "document.querySelector('#k7-sessions .k7-none')"}, {"eval": probe}])
    got = json.loads(out[-1])
    assert got["tv"] is False and got["switch"] == 0, got
    assert "No agent session yet: start Claude Code or Codex in a workspace." in got["text"], got
    assert got["quiet"] is True, got            # a source the provider could not read is said beside the empty line, not instead of it


@needs_browser
def test_a_click_on_a_waiting_task_opens_the_card_it_waits_on(tmp_path, monkeypatch):
    root, src = _sessions_fixture(tmp_path, monkeypatch)
    out = _run(tmp_path, src, [
        {"until": TV + " && " + TV + ".querySelector('.tv-task.waiting.tv-open')"},
        {"eval": TV + ".querySelector('.tv-task.waiting.tv-open').click(); 1"},
        {"until": "document.querySelector('.modal-overlay [data-card^=\"consent:\"].tv-lit')"},
        {"eval": "JSON.stringify([!!document.querySelector('.modal-overlay'), document.querySelector('[data-card^=\"consent:\"].tv-lit').className])"}])
    assert "timeout" not in json.dumps(out[-2]), out
    assert json.loads(out[-1])[0] is True, out


# --- a literal value ---------------------------------------------------------------------------------------


@needs_browser
def test_tasks_group_loudest_first_waiting_and_stuck_never_merge_and_unattached_is_last(tmp_path, monkeypatch):
    _root, src = _sessions_fixture(tmp_path, monkeypatch)
    probe = ("JSON.stringify({groups: [...%s.querySelectorAll('.tv-group')].map(g => g.dataset.group), "
             "tags: [...%s.querySelectorAll('.tv-task')].map(t => [t.dataset.task, t.dataset.state, (t.querySelector('.tv-tag') || {textContent: ''}).textContent]), "
             "waiting_open: !!%s.querySelector('.tv-task.waiting.tv-open'), stuck_open: !!%s.querySelector('.tv-task.stuck.tv-open')})") % ((TV,) * 4)
    out = _run(tmp_path, src, [{"eval": _paint(_value())}, {"eval": probe}])
    got = json.loads(out[-1])
    assert got["groups"] == ["waiting", "failed", "stuck", "running", "done", "unattached"], got
    tags = {t[0]: t for t in got["tags"]}
    assert tags["t-wait"][1:] == ["waiting", "waiting on you"] and tags["t-stuck"][1:] == ["stuck", "stuck"], got
    assert got["waiting_open"] is True and got["stuck_open"] is False, got              # only a record can be opened; silence has none
    out = _run(tmp_path, src, [{"eval": _paint(_value())}, {"eval": TV + ".querySelector('.tv-task.waiting').click(); window.__opened"}])
    assert out[-1] == "consent:abc", out                                                 # the click names the card the task waits on


@needs_browser
def test_null_is_drawn_as_unknown_and_a_real_zero_stays_a_zero(tmp_path, monkeypatch):
    _root, src = _sessions_fixture(tmp_path, monkeypatch)
    v = _value()
    probe = ("JSON.stringify({costs: Object.fromEntries([...%s.querySelectorAll('.tv-task')].map(t => [t.dataset.task, (t.querySelector('.tv-cost') || {textContent: null}).textContent])), "
             "chips: [...%s.querySelectorAll('.tv-chip')].map(c => [c.textContent, c.title]), "
             "nodes: Object.fromEntries([...%s.querySelectorAll('.tv-node')].map(n => [n.dataset.node, n.querySelector('.tv-node-m').textContent + ' | ' + n.querySelector('.tv-node-f').textContent]))})") % ((TV,) * 3)
    out = _run(tmp_path, src, [{"eval": _paint(v)}, {"eval": probe},
                               {"eval": _paint(v, "agents")}, {"eval": probe}])
    work, agents = json.loads(out[-3]), json.loads(out[-1])
    assert work["costs"]["t-stuck"] == "cost not tracked" and work["costs"]["t-fail"] == "cost not tracked", work
    assert work["costs"]["t-done"] == "$2.10 dispatch", work
    assert work["costs"]["t-run"] == "$0.00 · 5K tok ledger", work                # an explicit 0.0 is a zero, never "not tracked"
    assert work["costs"]["unattached"] is None, work                                    # no cost line on unattached work
    assert ["claude_code · ledgerline", "claude_code · claude-opus-5-5"] in work["chips"] or any("opus" in c[1] for c in work["chips"]), work
    assert any("model unknown" in c[1] for c in work["chips"]), work
    n1, n3, n4 = agents["nodes"]["n1"], agents["nodes"]["n3"], agents["nodes"]["n4"]
    assert "claude-opus-5-5" in n1 and "412K tok" in n1 and re.search(r"3\.[23] events/min", n1), n1
    assert "model unknown" in n3 and "tokens not tracked" in n3 and "rate not tracked" in n3, n3
    assert "model unknown" in n4 and "96K tok" in n4 and "rate not tracked" in n4, n4
    assert "0.0 events/min" in agents["nodes"]["n2"], agents                            # a log with no events is a real zero


@needs_browser
def test_the_agents_lens_is_columns_under_a_hub_with_subagents_indented(tmp_path, monkeypatch):
    _root, src = _sessions_fixture(tmp_path, monkeypatch)
    probe = ("JSON.stringify({hub: %s.querySelector('.tv-hub b').textContent, "
             "cols: [...%s.querySelectorAll('.tv-col')].map(c => [c.querySelector('.tv-col-h b').textContent, [...c.querySelectorAll('.tv-node')].map(n => n.dataset.node + (n.classList.contains('sub') ? '*' : ''))]), "
             "x: [...%s.querySelectorAll('.tv-col')].map(c => Math.round(c.getBoundingClientRect().left)), canvas: !!%s.querySelector('canvas'), "
             "waiting_open: !!%s.querySelector('.tv-node.waiting.tv-open')})") % ((TV,) * 5)
    out = _run(tmp_path, src, [{"eval": _paint(_value(), "agents")}, {"eval": probe}])
    got = json.loads(out[-1])
    assert got["hub"] == "Sage" and got["canvas"] is False, got                          # columns, not a force graph
    assert got["cols"] == [["ledgerline", ["n1", "n2*", "n3"]], ["tally", ["n4", "n5"]]], got
    assert got["x"][0] != got["x"][1], got                                               # side by side
    assert got["waiting_open"] is True, got


@needs_browser
def test_the_header_strip_counts_and_a_conflict_lists_its_file_and_both_sessions(tmp_path, monkeypatch):
    _root, src = _sessions_fixture(tmp_path, monkeypatch)
    probe = ("JSON.stringify({strip: Object.fromEntries([...%s.querySelectorAll('.tv-stat')].map(s => [s.dataset.k, s.textContent.replace(/\\s+/g, ' ')])), "
             "conf: (%s.querySelector('.tv-conf') || {innerText: ''}).textContent})") % (TV, TV)
    out = _run(tmp_path, src, [{"eval": _paint(_value())}, {"eval": probe}])
    got = json.loads(out[-1])["strip"]
    assert got == {"groups": "2 groups", "nodes": "5 nodes", "running": "1 running", "waiting": "1 waiting on you",
                   "conflicts": "1 file conflict", "tokens": "2.4M tokens today"}, got
    assert "money.py" in json.loads(out[-1])["conf"] and "claude_code · ledgerline" in json.loads(out[-1])["conf"] and "codex · tally" in json.loads(out[-1])["conf"]


@needs_browser
def test_what_the_provider_could_not_read_is_one_quiet_line_never_hidden(tmp_path, monkeypatch):
    _root, src = _sessions_fixture(tmp_path, monkeypatch)
    v = _value()
    v["errors"] = ["dispatch: no consent directory is attached, so no dispatch store to read", "spores: no spore store is attached"]
    v["unreadable"] = ["dispatch", "spores"]
    probe = "JSON.stringify([...%s.querySelectorAll('.tv-quiet')].map(q => q.textContent))" % TV
    out = _run(tmp_path, src, [{"eval": _paint(v)}, {"eval": probe}])
    lines = json.loads(out[-1])
    assert len(lines) == 1 and "dispatch: no consent directory" in lines[0] and "spores: no spore store" in lines[0], lines


@needs_browser
def test_no_horizontal_scroll_at_1440_and_at_390_in_either_lens(tmp_path, monkeypatch):
    _root, src = _sessions_fixture(tmp_path, monkeypatch)
    v = _value()
    v["tasks"][4]["label"] = "an unbroken " + "x" * 46                                   # the 48-character label the wire allows
    v["nodes"][0]["label"] = "claude_code · " + "y" * 33
    probe = ("JSON.stringify({page: document.documentElement.scrollWidth - document.documentElement.clientWidth, "
             "tv: %s.scrollWidth - %s.clientWidth, card: document.querySelector('#k7-sessions .k7-card').scrollWidth - document.querySelector('#k7-sessions .k7-card').clientWidth})") % (TV, TV)
    for size in (DESKTOP, NARROW):
        for lens in ("work", "agents"):
            out = _run(tmp_path, src, [{"eval": _paint(v, lens)}, {"eval": probe}], first=size)
            got = json.loads(out[-1])
            assert got["page"] <= 0 and got["tv"] <= 0 and got["card"] <= 0, (size, lens, got)


@needs_browser
def test_a_long_group_shows_its_first_four_and_one_button_for_the_rest(tmp_path, monkeypatch):
    root, src = _sessions_fixture(tmp_path, monkeypatch)
    _spore(root)
    v = _value()
    base = v["tasks"][2]
    v["tasks"] += [{**base, "id": f"t-more-{i}", "label": f"loop {i}"} for i in range(5)]          # six stuck in all
    probe = "JSON.stringify([...%s.querySelectorAll('.tv-group.stuck .tv-task')].filter(t => !t.hidden).length)" % TV
    # the real card repaints through cockpit_view.js; the button asks it to, so here the click is checked on the drawn state only
    out = _run(tmp_path, src, [{"eval": _paint(v)}, {"eval": probe}, {"eval": "(" + TV + ".querySelector('.tv-more') || {}).textContent"}])
    assert json.loads(out[-2]) == 4 and out[-1] == "show 2 more", out

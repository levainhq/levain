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
TV = "document.querySelector('#ck-hlens .tv')"


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
                  task("t-sign", "dispatch", "waiting", label="release 0.4.1", waits_on=["consent:abc"], card="dispatch:s1", workers=["n1"]),
                  task("unattached", "unattached", "running", label="unattached work", workers=["n4"])],
        "conflicts": [{"path": "money.py", "nodes": ["n1", "n4"]}], "counts": {"tokens_today": 2400000},
        "as_of": "2026-10-19T15:13:00Z", "lost_if_closed": [], "errors": [], "unreadable": [],
    }


def _paint(value: dict, lens: str = "work", strip: bool = True) -> str:
    """Replace the header's body with the renderer's own output for a literal value (the page's repaint leaves it: the signature is unchanged). The page's
    "go to the Needs strip" pair is stubbed: `strip` says whether the live strip holds a card (canOpenNeeds), and the jump counts itself in window.__first."""
    return ("(() => { const h = document.getElementById('ck-hlens'); h.hidden = false; "
            "h.replaceChildren(window.LevainTopologyView.render(%s, {lens: %s, openCard: (id) => { window.__opened = id; }, "
            "canOpenNeeds: () => %s, openNeeds: () => { window.__first = (window.__first || 0) + 1; return true; }})); return 1; })()") % (json.dumps(value), json.dumps(lens), json.dumps(strip))


def _paint_live(value: dict, lens: str = "work") -> str:
    """Like `_paint`, but the lens asks the page's own LevainNeeds (the live strip): the same two calls cockpit_view.js hands the real lens."""
    return ("(() => { const h = document.getElementById('ck-hlens'); h.hidden = false; "
            "h.replaceChildren(window.LevainTopologyView.render(%s, {lens: %s, openCard: (id) => { window.__opened = id; }, "
            "canOpenNeeds: () => window.LevainNeeds.canGoFirst(), openNeeds: () => window.LevainNeeds.goFirst()})); return 1; })()") % (json.dumps(value), json.dumps(lens))


def _run(tmp_path, src, steps, first=DESKTOP, until="document.querySelector('#k7-sessions .k7-card') && document.querySelector('#ck-hlens .tv')"):
    with _serve(src, surface_tokens={"browser": "T"}, install_class="reported") as (url, _httpd):
        return _drive(tmp_path, f"{url}/{BUILD}", [first, {"until": until}, *steps])["values"]


# --- the page, through the real provider ---------------------------------------------------------------


@needs_browser
def test_the_work_lens_draws_the_providers_own_nulls_as_not_tracked(tmp_path, monkeypatch):
    root, src = _sessions_fixture(tmp_path, monkeypatch)
    _spore(root)
    probe = ("JSON.stringify({lens: %s.dataset.lens, recs: %s.querySelectorAll('.tv-rec, .tv-wl').length, "
             "strip: [...%s.querySelectorAll('.tv-stat')].map(s => s.dataset.k + '=' + s.textContent.replace(/\\s+/g, ' ')), "
             "cols: %s.querySelectorAll('.tv-col').length, "
             "tasks: [...%s.querySelectorAll('.tv-task')].map(t => [t.dataset.state, t.dataset.kind, "
             "(t.querySelector('.tv-cost') || {textContent: ''}).textContent, [...t.querySelectorAll('.tv-chip')].map(c => c.textContent)]), "
             "pressed: [...document.querySelectorAll('#ck-hlens .tv-seg [aria-pressed=true]')].map(b => b.textContent)})") % ((TV,) * 5)
    out = _run(tmp_path, src, [{"until": TV}, {"eval": "document.querySelector('#ck-hlens [data-tv=work]').click(); 1"},
                               {"until": TV + " && " + TV + ".querySelector('.tv-task')"}, {"eval": probe}])
    got = json.loads(out[-1])
    assert got["lens"] == "work" and got["cols"] == 0, got                              # Work, not Agents
    assert got["pressed"] == ["Work"], got                                              # the header's switch says so
    assert got["recs"] == 0, got                                                        # the pending consents are listed in the Needs strip, not again here
    assert not any(t[1] in ("consent", "question") for t in got["tasks"]), got          # ... and are no task cards either
    by = {t[1]: t for t in got["tasks"]}
    assert by["spore"][0] == "open" and by["spore"][2] == "cost not tracked" and by["spore"][3] == [], got   # a spore carries no session: open, never stuck
    assert any(t[0] != "waiting" and t[1] == "unattached" for t in got["tasks"]), got                              # the codex session: no task, none guessed
    strip = dict(s.split("=", 1) for s in got["strip"])
    # K10 S5 item 2 (67d28166): both live sessions have a readable tool log and no path is shared, so the provider OBSERVED no conflict and
    # says 0; "not tracked" is the null it sends only when live nodes exist and none has a log (test_conflicts_are_null_when_no_live_node_has_a_log)
    assert strip["conflicts"] == "0 conflicts" and strip["tokens"] == "0 tokens today", got   # the fixture transcripts spent nothing today: a real zero
    assert list(strip) == ["tasks", "waiting", "tasks-waiting", "stuck", "open", "done", "conflicts", "tokens"] and strip["tasks"].endswith(" tasks") and strip["open"] == "2 open", got


@needs_browser
def test_a_cold_open_draws_the_header_on_the_agents_graph_lens(tmp_path, monkeypatch):
    """K11: no stored choice -> the header's default lens is the Agents graph, with the three-way switch in its order."""
    root, src = _sessions_fixture(tmp_path, monkeypatch)
    _spore(root)
    probe = ("JSON.stringify({lens: %s && %s.dataset.lens, hl: document.getElementById('ck-skycard').dataset.hlens, graph: !!%s.querySelector('svg.tv-graph'), "
             "stored: localStorage.getItem('levain_cockpit_header_lens'), "
             "buttons: [...document.querySelectorAll('#ck-hlens .tv-seg-b')].map(b => [b.textContent, b.getAttribute('aria-pressed')])})") % (TV, TV, TV)
    out = _run(tmp_path, src, [{"until": TV + " && " + TV + ".querySelector('svg.tv-graph')"}, {"eval": probe}])
    got = json.loads(out[-1])
    assert got["lens"] == "agents" and got["hl"] == "agents" and got["graph"] is True and got["stored"] is None, got
    assert got["buttons"] == [["Agents graph", "true"], ["Event graph", "false"], ["Work", "false"]], got


@needs_browser
def test_choosing_the_event_graph_survives_a_reload(tmp_path, monkeypatch):
    """K11: the header's choice is a per-browser view preference: Event graph, reload, still Event graph (the Activity band shown, no topology drawn)."""
    root, src = _sessions_fixture(tmp_path, monkeypatch)
    _spore(root)
    probe = ("JSON.stringify({hl: document.getElementById('ck-skycard').dataset.hlens, tv: !!%s, "
             "pressed: [...document.querySelectorAll('#ck-hlens .tv-seg [aria-pressed=true]')].map(b => b.textContent), "
             "sky: getComputedStyle(document.getElementById('ck-sky')).display})") % TV
    out = _run(tmp_path, src, [
        {"until": TV}, {"eval": "document.querySelector('#ck-hlens [data-tv=events]').click(); 1"},
        {"until": "document.getElementById('ck-skycard').dataset.hlens == 'events'"}, {"eval": probe},
        {"eval": "location.reload(); 1"}, {"eval": "new Promise(r => setTimeout(r, 1500))"},
        {"until": "document.querySelector('#ck-hlens .tv-seg-b')"}, {"eval": probe}])
    before, after = json.loads(out[-5]), json.loads(out[-1])
    assert before["hl"] == "events" and before["tv"] is False and before["pressed"] == ["Event graph"], before
    assert after == before and after["sky"] != "none", after


@needs_browser
def test_a_bare_install_draws_no_switch_no_topology_and_keeps_the_one_line(tmp_path, monkeypatch):
    monkeypatch.setenv("LEVAIN_HOME", str(tmp_path / "levain_home"))
    _root, src, _ids = _install(tmp_path)
    src.anneal.spores_json.unlink(missing_ok=True)                  # K10: an open loop is work, so "bare" means no loop either
    probe = ("JSON.stringify({tv: !!%s, switch: document.querySelectorAll('#ck-hlens [data-tv]').length, "
             "quiet: !!document.querySelector('#ck-hlens .tv-quiet'), text: document.querySelector('#k7-sessions').innerText.replace(/\\s+/g, ' ')})") % TV
    out = _run(tmp_path, src, [{"eval": probe}], until="document.querySelector('#k7-sessions .k7-none')")   # a bare install draws no card and no .tv: its one line is the signal
    got = json.loads(out[-1])
    assert got["tv"] is False and got["switch"] == 0, got
    assert "No agent session yet: start Claude Code or Codex in a workspace." in got["text"], got
    # K10 S5 item 1 (0c45fe2d): a default install now reads the default dispatch store, so nothing is unreadable here and no quiet line is
    # drawn (absence of a note is honest only because every source read); a source that could not be read is said beside the empty
    # line, not instead of it (held by test_what_the_provider_could_not_read_is_one_quiet_line_never_hidden)
    assert got["quiet"] is False, got


@needs_browser
def test_a_click_on_the_waiting_count_opens_the_card_it_waits_on(tmp_path, monkeypatch):
    root, src = _sessions_fixture(tmp_path, monkeypatch)
    out = _run(tmp_path, src, [
        {"until": TV}, {"eval": "document.querySelector('#ck-hlens [data-tv=work]').click(); 1"},
        {"until": TV + " && " + TV + ".querySelector('.tv-jump')"},
        {"eval": TV + ".querySelector('.tv-jump').click(); 1"},
        {"until": "document.querySelector('#ck-needs .needs-lit[data-needs-id^=consent]')"},      # K10 S3: the strip's card (Build: its chip) is lit, no modal
        {"eval": "JSON.stringify([!document.querySelector('.modal-overlay'), document.querySelector('#ck-needs .needs-lit').className])"}])
    assert "timeout" not in json.dumps(out[-2]), out
    assert json.loads(out[-1])[0] is True, out


# --- a literal value ---------------------------------------------------------------------------------------


@needs_browser
def test_two_columns_tasks_left_outcomes_right_on_the_tasks_own_row_and_no_waiting_records(tmp_path, monkeypatch):
    _root, src = _sessions_fixture(tmp_path, monkeypatch)
    probe = ("JSON.stringify({order: [...%s.querySelectorAll('.tv-task')].map(t => t.dataset.task), "
             "recs: [...%s.querySelectorAll('.tv-rec, .tv-wl')].map(r => r.className), heads: [...%s.querySelectorAll('.tv-colh')].filter(h => h.offsetParent).map(h => h.textContent), "
             "task_left: Math.round(%s.querySelector('.tv-task').getBoundingClientRect().left), "
             "tags: Object.fromEntries([...%s.querySelectorAll('.tv-task')].map(t => [t.dataset.task, (t.querySelector('.tv-tag') || {textContent: ''}).textContent])), "
             "outs: [...%s.querySelectorAll('.tv-outs')].map(o => [o.dataset.task, Math.round(o.getBoundingClientRect().left), "
             "Math.abs(o.getBoundingClientRect().top - %s.querySelector('.tv-task[data-task=' + o.dataset.task + ']').getBoundingClientRect().top) < 2]), "
             "legend: (%s.querySelector('.tv-legend') || {textContent: ''}).textContent, "
             "waiting_open: !!%s.querySelector('.tv-task.waiting.tv-open'), stuck_open: !!%s.querySelector('.tv-task.stuck.tv-open')})") % ((TV,) * 10)
    out = _run(tmp_path, src, [{"eval": _paint(_value())}, {"eval": probe}])
    got = json.loads(out[-1])
    assert got["order"] == ["t-sign", "t-fail", "t-stuck", "t-run", "t-done", "unattached"], got      # loudest first: waiting, failed, stuck, running, (open), done; unattached last; the consent is no centre card
    assert got["recs"] == [], got                                                                       # no per-item waiting record column (the Needs strip lists them)
    assert got["heads"] == ["Tasks · who is working on them", "Outcomes"], got
    assert got["outs"] and all(o[1] > got["task_left"] and o[2] for o in got["outs"]), got              # outcomes sit right of the tasks, on the task's own row
    assert got["tags"]["t-sign"] == "waiting on you" and got["tags"]["t-stuck"] == "stuck", got
    assert "waiting = a pending record you can answer; stuck = silence, no record" in got["legend"], got
    assert got["waiting_open"] is True and got["stuck_open"] is False, got                              # only a card can be opened; silence has none
    out = _run(tmp_path, src, [{"eval": _paint(_value())}, {"eval": TV + ".querySelector('.tv-jump').click(); JSON.stringify([window.__first, window.__opened])"}])
    assert json.loads(out[-1]) == [1, None], out                                                         # the one count asks the strip to go to ITS first card: no topology card id is used


@needs_browser
def test_a_line_joins_a_task_to_its_outcomes_and_no_line_joins_a_waiting_record(tmp_path, monkeypatch):
    _root, src = _sessions_fixture(tmp_path, monkeypatch)
    probe = "JSON.stringify([...%s.querySelectorAll('.tv-link')].map(p => p.getAttribute('class').replace('tv-link ', '')).sort())" % TV
    out = _run(tmp_path, src, [{"eval": _paint(_value())}, {"until": TV + ".querySelector('.tv-link')"}, {"eval": probe}])
    # t-done -> its check, t-fail -> its cross; the waiting records that used to be joined to t-sign are gone
    assert json.loads(out[-1]) == ["bad", "ok"], out
    out = _run(tmp_path, src, [{"eval": _paint(_value())}, {"eval": "new Promise(r => setTimeout(r, 400))"}, {"eval": probe}], first=NARROW)
    assert json.loads(out[-1]) == [], out                                                                # stacked: no side to draw lines on


@needs_browser
def test_below_the_column_width_the_tasks_then_each_tasks_outcomes_stack(tmp_path, monkeypatch):
    _root, src = _sessions_fixture(tmp_path, monkeypatch)
    probe = ("JSON.stringify({recs: %s.querySelectorAll('.tv-rec, .tv-wl').length, head: Math.round(%s.querySelector('.tv-colh1').getBoundingClientRect().top), "
             "task: Math.round(%s.querySelector('.tv-task').getBoundingClientRect().top), "
             "out_below: (() => { const o = %s.querySelector('.tv-outs'), t = %s.querySelector('.tv-task[data-task=' + o.dataset.task + ']'); "
             "return o.getBoundingClientRect().top >= t.getBoundingClientRect().bottom - 1; })()})") % ((TV,) * 5)
    out = _run(tmp_path, src, [{"eval": _paint(_value())}, {"eval": probe}], first=NARROW)
    got = json.loads(out[-1])
    assert got["recs"] == 0 and got["head"] < got["task"] and got["out_below"] is True, got


def _waiting_value(n: int) -> dict:
    """`_value()` with exactly `n` waiting asks: n waiting sessions, each carrying its own consent task (and its card)."""
    v = _value()
    v["tasks"] = [t for t in v["tasks"] if t["state"] != "waiting"]
    v["nodes"] = [{**nd, "state": "running" if nd["state"] == "waiting" else nd["state"], "card": None} for nd in v["nodes"]]
    base = next(nd for nd in _value()["nodes"] if nd["state"] == "waiting")
    for i in range(n):
        v["nodes"].append({**base, "id": f"w{i}", "label": f"seat {i}", "card": f"consent:w{i}"})
        v["tasks"].append({"id": f"t-w{i}", "kind": "consent" if i % 2 == 0 else "question", "label": f"ask number {i}", "state": "waiting", "waits_on": [f"consent:w{i}"],
                           "card": f"consent:w{i}", "outcomes": [], "cost": {"usd": None, "tokens": None, "source": None}, "workers": [f"w{i}"]})
    return v


@needs_browser
def test_the_work_lens_lists_no_waiting_items_only_one_count_that_jumps_to_the_needs_strip(tmp_path, monkeypatch):
    """Phill 11-11: the left column listed every waiting item with no organisation, beside the Needs strip that does it better. The lens now draws no
    per-item waiting record, only the one count (the existing "waiting on you" figure) as a button when something waits, and plain when nothing does."""
    _root, src = _sessions_fixture(tmp_path, monkeypatch)
    probe = ("JSON.stringify({recs: %s.querySelectorAll('.tv-rec, .tv-wl').length, jumps: [...%s.querySelectorAll('.tv-jump')].map(b => [b.tagName, b.textContent.replace(/\\s+/g, ' '), b.dataset.k]), "
             "stat: %s.querySelector('[data-k=waiting]').textContent.replace(/\\s+/g, ' '), tasks: %s.querySelectorAll('.tv-task').length, "
             "no_col: getComputedStyle(%s.querySelector('.tv-work')).gridTemplateColumns.split(' ').length})") % ((TV,) * 5)
    for n in (3, 1, 0):
        out = _run(tmp_path, src, [{"eval": _paint(_waiting_value(n))}, {"eval": probe}, {"eval": "window.__first = 0; (" + TV + ".querySelector('.tv-jump') || {click() {}}).click(); window.__first"}])
        got = json.loads(out[-2])
        assert got["recs"] == 0, (n, got)                                                                # never a per-item record, at any count
        assert got["stat"] == f"{n} waiting on you", (n, got)                                           # the one figure, from the same function as before
        assert got["tasks"] >= 4, (n, got)                                                              # tasks stay exactly as they were (the fixture's other states)
        if n:
            assert got["jumps"] == [["BUTTON", f"{n} waiting on you", "waiting"]], (n, got)           # one button, a native one: Enter and Space work
            assert out[-1] == 1, (n, out)                                                               # it asks the live strip to go to its first card, once
        else:
            assert got["jumps"] == [] and out[-1] == 0, (n, got, out)                                  # nothing waits: no count line to press, and nowhere asked to go


@needs_browser
def test_the_work_lens_reads_sensibly_with_tasks_and_no_outcomes_and_with_nothing_waiting_at_both_widths(tmp_path, monkeypatch):
    _root, src = _sessions_fixture(tmp_path, monkeypatch)
    v = _waiting_value(0)
    for t in v["tasks"]:
        t["outcomes"] = []
    probe = ("JSON.stringify({recs: %s.querySelectorAll('.tv-rec, .tv-wl').length, tasks: %s.querySelectorAll('.tv-task').length, "
             "colh: [...%s.querySelectorAll('.tv-colh')].filter(h => h.offsetParent).map(h => h.textContent), outs: %s.querySelectorAll('.tv-outs').length, "
             "page: document.documentElement.scrollWidth - document.documentElement.clientWidth, "
             "task_w: Math.round(%s.querySelector('.tv-task').getBoundingClientRect().width), work_w: Math.round(%s.querySelector('.tv-work').getBoundingClientRect().width)})") % ((TV,) * 6)
    for size in (DESKTOP, NARROW):
        out = _run(tmp_path, src, [{"eval": _paint(v)}, {"eval": probe}], first=size)
        got = json.loads(out[-1])
        assert got["recs"] == 0 and got["tasks"] >= 4 and got["outs"] == 0 and got["page"] <= 0, (size, got)
        assert got["colh"] == ["Tasks · who is working on them"], (size, got)                          # no Outcomes heading over nothing, no Waiting heading over nothing
        assert got["task_w"] >= got["work_w"] - 2, (size, got)                                         # the tasks take the whole width the records and outcomes gave back


@needs_browser
def test_null_is_drawn_as_unknown_and_a_real_zero_stays_a_zero(tmp_path, monkeypatch):
    _root, src = _sessions_fixture(tmp_path, monkeypatch)
    v = _value()
    probe = ("JSON.stringify({costs: Object.fromEntries([...%s.querySelectorAll('.tv-task')].map(t => [t.dataset.task, (t.querySelector('.tv-cost') || {textContent: null}).textContent])), "
             "chips: [...%s.querySelectorAll('.tv-chip')].map(c => [c.textContent, c.title]), "
             "nodes: Object.fromEntries([...%s.querySelectorAll('.tv-gnode')].map(n => [n.dataset.node, n.dataset.face]))})") % ((TV,) * 3)
    out = _run(tmp_path, src, [{"eval": _paint(v)}, {"eval": probe},
                               {"eval": _paint(v, "agents")}, {"eval": probe}])
    work, agents = json.loads(out[-3]), json.loads(out[-1])
    assert work["costs"]["t-stuck"] == "cost not tracked" and work["costs"]["t-fail"] == "cost not tracked", work
    assert "t-wait" not in work["costs"], work                                                           # a consent is a record on the left, not a task card
    assert work["costs"]["t-done"] == "$2.10 dispatch", work
    assert work["costs"]["t-run"] == "$0.00 · 5K tok ledger", work                # an explicit 0.0 is a zero, never "not tracked"
    assert work["costs"]["unattached"] is None, work                                    # no cost line on unattached work
    assert ["claude_code · ledgerline", "claude_code · claude-opus-5-5"] in work["chips"] or any("opus" in c[1] for c in work["chips"]), work
    assert any("model unknown" in c[1] for c in work["chips"]), work
    n1, n3, n4 = agents["nodes"]["n1"], agents["nodes"]["n3"], agents["nodes"]["n4"]
    assert "claude-opus-5-5" in n1 and "412K tok" in n1, n1
    assert "model unknown" in n3 and "tokens unknown" in n3, n3
    assert "model unknown" in n4 and "96K tok" in n4, n4



@needs_browser
def test_the_header_strip_counts_and_a_conflict_lists_its_file_and_both_sessions(tmp_path, monkeypatch):
    _root, src = _sessions_fixture(tmp_path, monkeypatch)
    probe = ("JSON.stringify({strip: Object.fromEntries([...%s.querySelectorAll('.tv-stat')].map(s => [s.dataset.k, s.textContent.replace(/\\s+/g, ' ')])), "
             "conf: (%s.querySelector('.tv-conf') || {innerText: ''}).textContent})") % (TV, TV)
    out = _run(tmp_path, src, [{"eval": _paint(_value())}, {"eval": probe}])
    got = json.loads(out[-1])["strip"]
    assert got == {"tasks": "5 tasks", "waiting": "1 waiting on you", "tasks-waiting": "1 task waiting", "failed": "1 failed", "stuck": "1 stuck", "done": "1 done",
                   "conflicts": "1 file conflict", "tokens": "2.4M tokens today"}, got                  # the tasks lead and count the cards drawn (the fixture's consent record t-wait is no card: 5 and 1); "waiting on you" is waitingOnYou(v), the same figure the Agents lens shows (the fixture's one waiting task rides the waiting session n1, so it is one)
    metrics = "JSON.stringify([...%s.querySelectorAll('.tv-metrics .tv-m')].map(m => m.dataset.k + '=' + m.firstChild.textContent))" % TV
    out = _run(tmp_path, src, [{"eval": _paint(_value(), "agents")}, {"eval": probe}, {"eval": metrics}])
    figs = json.loads(out[-1])
    assert figs[0].startswith("sessions=") and "tokens=2.4M" in figs, figs                               # the Agents lens draws the metrics block, not the count row
    conf = json.loads(out[-2])["conf"]
    assert "money.py" in conf and "claude_code · ledgerline" in conf and "codex · tally" in conf


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
    v["tasks"][5]["label"] = "an unbroken " + "z" * 46
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
    probe = "JSON.stringify([...%s.querySelectorAll('.tv-task[data-state=stuck]')].filter(t => !t.hidden).length)" % TV
    # the real card repaints through cockpit_view.js; the button asks it to, so here the click is checked on the drawn state only
    out = _run(tmp_path, src, [{"eval": _paint(v)}, {"eval": probe}, {"eval": "(" + TV + ".querySelector('.tv-more') || {}).textContent"}])
    assert json.loads(out[-2]) == 4 and out[-1] == "show 2 more", out


@needs_browser
def test_a_transcript_cost_reads_as_tokens_from_transcripts_never_a_price(tmp_path, monkeypatch):
    _root, src = _sessions_fixture(tmp_path, monkeypatch)
    v = _value()
    next(t for t in v["tasks"] if t["id"] == "t-stuck")["cost"] = {"usd": None, "tokens": 705544, "source": "transcript"}
    probe = ("JSON.stringify(Object.fromEntries([...%s.querySelectorAll('.tv-task')].map(t => [t.dataset.task, (t.querySelector('.tv-cost') || {textContent: null}).textContent])))") % TV
    out = _run(tmp_path, src, [{"eval": _paint(v)}, {"eval": probe}])
    assert json.loads(out[-1])["t-stuck"] == "706K tokens from transcripts", out[-1]


# --- the Work lens's click targets: a worker opens its page, an open loop opens its row -----------------------------------


def _work_value() -> dict:
    v = _value()
    v["tasks"] += [{"id": "spore:spore-017", "kind": "spore", "label": "tighten the rounding guard", "state": "open", "waits_on": [], "card": None,
                    "outcomes": [], "cost": {"usd": None, "tokens": None, "source": None}, "workers": ["n2"], "opened": None, "age_days": 3, "heat": "hot"}]
    return v


def _paint_with(value: dict, page: bool, loop: bool) -> str:
    """Draw the Work lens for a literal value with a stub agent page (or none) and an openLoop callback (or none); both record what they were handed."""
    opts = "{lens: 'work', openCard: (id) => { window.__opened = id; }" + (", openLoop: (id) => { window.__loop = id; }" if loop else "") + "}"
    stub = "window.LevainAgentPage = {open: (n, a) => { window.__page = n.id; }};" if page else "window.LevainAgentPage = undefined;"
    return ("(() => { %s window.__page = null; window.__loop = null; window.__opened = null; const h = document.getElementById('ck-hlens'); h.hidden = false; "
            "h.replaceChildren(window.LevainTopologyView.render(%s, %s)); return 1; })()") % (stub, json.dumps(value), opts)


CHIP = TV + ".querySelector('.tv-task[data-task=\"spore:spore-017\"] .tv-chip')"
LOOP = TV + ".querySelector('.tv-task[data-task=\"spore:spore-017\"]')"
LOOP_TITLE = LOOP + ".querySelector('.tv-task-t')"
KEY = "new KeyboardEvent('keydown', {key: 'Enter', bubbles: true, cancelable: true})"


@needs_browser
def test_a_worker_chip_opens_its_agent_page_and_not_its_cards_click(tmp_path, monkeypatch):
    _root, src = _sessions_fixture(tmp_path, monkeypatch)
    probe = ("JSON.stringify({role: %s.getAttribute('role'), tab: %s.tabIndex, open: %s.classList.contains('tv-open'), page: window.__page, "
             "loop: window.__loop, card: window.__opened})") % (CHIP, CHIP, CHIP)
    out = _run(tmp_path, src, [{"eval": _paint_with(_work_value(), True, True)}, {"eval": CHIP + ".click(); 1"}, {"eval": probe},
                               {"eval": "window.__page = null; " + CHIP + ".dispatchEvent(" + KEY + "); 1"}, {"eval": "window.__page"}])
    got = json.loads(out[-3])
    assert got == {"role": "button", "tab": 0, "open": True, "page": "n2", "loop": None, "card": None}, got    # the loop card's own click did not fire
    assert out[-1] == "n2", out                                                                                # Enter on the chip does the same as a click


@needs_browser
def test_without_the_agent_page_a_chip_stays_a_plain_span(tmp_path, monkeypatch):
    _root, src = _sessions_fixture(tmp_path, monkeypatch)
    probe = "JSON.stringify([...%s.querySelectorAll('.tv-chip')].map(c => [c.getAttribute('role'), c.tabIndex, c.classList.contains('tv-open')]))" % TV
    out = _run(tmp_path, src, [{"eval": _paint_with(_work_value(), False, True)}, {"eval": probe}])
    got = json.loads(out[-1])
    assert got and all(g == [None, -1, False] for g in got), got


@needs_browser
def test_an_open_loop_card_calls_open_loop_with_its_spore_id_by_click_and_by_key(tmp_path, monkeypatch):
    _root, src = _sessions_fixture(tmp_path, monkeypatch)
    probe = "JSON.stringify({role: %s.getAttribute('role'), tab: %s.tabIndex, title: %s.getAttribute('role'), loop: window.__loop})" % (LOOP, LOOP, LOOP_TITLE)
    out = _run(tmp_path, src, [{"eval": _paint_with(_work_value(), True, True)}, {"eval": LOOP + ".click(); 1"}, {"eval": probe},
                               {"eval": "window.__loop = null; " + LOOP_TITLE + ".dispatchEvent(" + KEY + "); 1"}, {"eval": "window.__loop"}])
    # the card is not itself a button (its chips would sit inside one): its title is the control
    assert json.loads(out[-3]) == {"role": None, "tab": -1, "title": "button", "loop": "spore-017"}, out
    assert out[-1] == "spore-017", out


@needs_browser
def test_an_open_loop_card_is_inert_when_the_page_passes_no_open_loop(tmp_path, monkeypatch):
    _root, src = _sessions_fixture(tmp_path, monkeypatch)
    probe = ("JSON.stringify({role: %s.getAttribute('role'), open: %s.classList.contains('tv-open'), "
             "any: !!%s.querySelector('.tv-task[data-kind=spore].tv-open')})") % (LOOP, LOOP, TV)
    out = _run(tmp_path, src, [{"eval": _paint_with(_work_value(), True, False)}, {"eval": LOOP + ".click(); 1"}, {"eval": probe}, {"eval": "window.__loop"}])
    assert json.loads(out[-2]) == {"role": None, "open": False, "any": False}, out
    assert out[-1] is None, out


@needs_browser
def test_a_waiting_card_still_opens_its_needs_you_card_and_the_loop_card_does_not_take_its_click(tmp_path, monkeypatch):
    _root, src = _sessions_fixture(tmp_path, monkeypatch)
    sel = TV + ".querySelector('.tv-task.waiting.tv-open')"
    out = _run(tmp_path, src, [{"eval": _paint_with(_work_value(), True, True)}, {"eval": sel + ".click(); 1"},
                               {"eval": "JSON.stringify([window.__opened, window.__loop, window.__page])"}])
    assert json.loads(out[-1]) == ["dispatch:s1", None, None], out


@needs_browser
def test_the_work_lens_open_loop_scrolls_to_and_lights_the_row_in_open_loops(tmp_path, monkeypatch):
    """Through the real page: the Work lens's loop card lights the row carrying that spore's data-spore in the Open loops panel."""
    root, src = _sessions_fixture(tmp_path, monkeypatch)
    _spore(root)
    row = "document.querySelector('[data-ck-panel=\"loops\"] .row[data-spore]')"
    out = _run(tmp_path, src, [
        {"until": TV}, {"eval": "document.querySelector('#ck-hlens [data-tv=work]').click(); 1"},
        {"until": TV + " && " + TV + ".querySelector('.tv-task[data-kind=spore].tv-open')"},
        {"until": row},
        {"eval": "JSON.stringify({spore: " + row + ".dataset.spore, lit: " + row + ".classList.contains('tv-lit'), "
                 "card: " + TV + ".querySelector('.tv-task[data-kind=spore]').dataset.loop})"},
        {"eval": TV + ".querySelector('.tv-task[data-kind=spore]').click(); 1"},
        {"until": row + ".classList.contains('tv-lit')"},
        {"eval": "JSON.stringify([" + row + ".dataset.spore, " + row + ".classList.contains('tv-lit')])"}])
    before = json.loads(out[-4])
    assert before["lit"] is False and before["spore"] and before["card"] == before["spore"], before
    assert "timeout" not in json.dumps(out[-2]), out
    assert json.loads(out[-1]) == [before["spore"], True], out


# --- the jump to an open loop: find before moving, unfold through the panel's own paths, land on the row --------------------


def _spores(root: Path, n: int) -> None:
    from datetime import datetime

    from anneal_memory.spores import SporeStore
    store = SporeStore(root / ".levain" / "memory.spores.json")
    for i in range(n):
        store.add(type="task", text=f"loop number {i} about the ledger rounding guard", tier="hot", domain="ledgerline", today=datetime.now().astimezone().date())


PAGE_ROW = "document.querySelector('[data-ck-panel=\"loops\"]:not(.modal *) .row[data-spore]')"
LOOPS = "document.querySelector('[data-ck-panel=\"loops\"]')"
CARD = TV + ".querySelector('.tv-task[data-kind=spore]')"


def _to_work(tmp_path, src, steps, first=DESKTOP):
    ready = [{"until": TV}, {"eval": "document.querySelector('#ck-hlens [data-tv=work]').click(); 1"},
             {"until": TV + " && " + TV + ".querySelector('.tv-task[data-kind=spore].tv-open')"}, {"until": PAGE_ROW}]
    return _run(tmp_path, src, ready + steps, first)[len(ready) + 1:]


@needs_browser
def test_a_collapsed_loops_card_is_expanded_through_its_own_control_and_the_row_lit(tmp_path, monkeypatch):
    root, src = _sessions_fixture(tmp_path, monkeypatch)
    _spore(root)
    out = _to_work(tmp_path, src, [
        {"eval": LOOPS + ".querySelector('.ck-lay-fold').click(); " + LOOPS + ".classList.contains('ck-collapsed')"},
        {"eval": "JSON.stringify([window.LevainLoops.go(" + PAGE_ROW + ".dataset.spore), " + LOOPS + ".classList.contains('ck-collapsed'), "
                 + PAGE_ROW + ".classList.contains('tv-lit'), !!" + PAGE_ROW + ".getClientRects().length])"}])
    assert out[0] is True, out
    assert json.loads(out[1]) == [True, False, True, True], out


@needs_browser
def test_a_card_that_will_not_unfold_lights_nothing_and_says_false(tmp_path, monkeypatch):
    root, src = _sessions_fixture(tmp_path, monkeypatch)
    _spore(root)
    # ONE synchronous eval: the fold click schedules a 60 ms ckRefreshAll, whose layoutControls re-enables the button; disabling it in
    # a later step let a loaded runner's refresh undo it (CI flake), so disable and go() share one task, before any refresh can run.
    out = _to_work(tmp_path, src, [
        {"eval": LOOPS + ".querySelector('.ck-lay-fold').click(); " + LOOPS + ".querySelector('.ck-lay-fold').disabled = true; "
                 "JSON.stringify([window.LevainLoops.go(" + PAGE_ROW + ".dataset.spore), " + LOOPS + ".classList.contains('ck-collapsed'), "
                 + PAGE_ROW + ".classList.contains('tv-lit')])"}])
    assert json.loads(out[0]) == [False, True, False], out


@needs_browser
def test_a_folded_row_is_shown_through_the_folds_own_pin_with_its_heading_and_the_count_right(tmp_path, monkeypatch):
    root, src = _sessions_fixture(tmp_path, monkeypatch)
    _spores(root, 40)
    rows = LOOPS + ".querySelectorAll('.row[data-spore]')"
    probe = ("(() => { const hid = [...%s].filter(r => r.dataset.folded); const t = hid[hid.length - 1]; if (!t) return null; "
             "window.__t = t.dataset.spore; return [hid.length, (%s.querySelector('.more-foot') || {}).textContent]; })()") % (rows, LOOPS)
    out = _to_work(tmp_path, src, [
        {"until": probe},
        {"eval": "JSON.stringify([window.LevainLoops.go(window.__t), [...%s].find(r => r.dataset.spore === window.__t).classList.contains('tv-lit'), "
                 "!![...%s].find(r => r.dataset.spore === window.__t).getClientRects().length, [...%s].filter(r => r.dataset.folded).length, "
                 "(%s.querySelector('.more-foot') || {}).textContent, [...%s].filter(r => r.dataset.spore === window.__t)[0].classList.contains('row-expanded')])"
                 % (rows, rows, rows, LOOPS, rows)}])
    first = json.loads(out[0]) if isinstance(out[0], str) else out[0]
    after = json.loads(out[1])
    assert after[:3] == [True, True, True], after
    assert after[3] < first[0], (first, after)                         # the pin stopped the fold cutting at or above the row: fewer are hidden
    assert after[5] is True, after


@needs_browser
def test_a_filter_that_hides_the_row_is_cleared_through_the_bar_and_the_row_lit(tmp_path, monkeypatch):
    root, src = _sessions_fixture(tmp_path, monkeypatch)
    _spore(root)
    inp = LOOPS + ".querySelector('.ep-search-input')"
    out = _to_work(tmp_path, src, [
        {"eval": "window.__id = " + PAGE_ROW + ".dataset.spore; " + inp + ".value = 'zzz-no-such'; " + inp + ".dispatchEvent(new Event('input', {bubbles: true})); " + "!!" + PAGE_ROW},
        {"eval": "JSON.stringify([window.LevainLoops.go(window.__id), " + inp + ".value, " + PAGE_ROW + ".classList.contains('tv-lit')])"},
        # a query that matches nothing and a row that is not there: the person's query stays
        {"eval": inp + ".value = 'keep me'; " + inp + ".dispatchEvent(new Event('input', {bubbles: true})); JSON.stringify([!!window.LevainLoops.find('spore-nope'), " + inp + ".value])"}])
    assert out[0] is False, out                       # the filter hid the row
    assert json.loads(out[1]) == [True, "", True], out
    assert json.loads(out[2]) == [False, "keep me"], out


@needs_browser
def test_an_absent_row_moves_nothing_and_the_card_says_so(tmp_path, monkeypatch):
    root, src = _sessions_fixture(tmp_path, monkeypatch)
    _spore(root)
    out = _to_work(tmp_path, src, [
        {"eval": "window.__mode = document.body.dataset.mode; " + PAGE_ROW + ".remove(); " + CARD + ".click(); "
                 "JSON.stringify([document.body.dataset.mode === window.__mode, " + CARD + ".title, !!document.querySelector('.tv-lit[data-spore]')])"}])
    assert json.loads(out[0]) == [True, "not in Open loops now", False], out


@needs_browser
def test_a_second_tap_keeps_the_full_highlight_and_focus_lands_on_the_row(tmp_path, monkeypatch):
    root, src = _sessions_fixture(tmp_path, monkeypatch)
    _spore(root)
    wait = "new Promise(r => setTimeout(r, %d))"
    out = _to_work(tmp_path, src, [
        {"eval": "(async () => { " + CARD + ".click(); await " + wait % 1300 + "; " + CARD + ".click(); await " + wait % 1400 + "; "
                 "return JSON.stringify([" + PAGE_ROW + ".classList.contains('tv-lit'), document.activeElement === " + PAGE_ROW + ", " + PAGE_ROW + ".tabIndex]); })()"},
        {"eval": "(async () => { await " + wait % 1500 + "; return " + PAGE_ROW + ".classList.contains('tv-lit'); })()"}])
    assert json.loads(out[0]) == [True, True, 0], out     # (a writable page's row keeps its own stop) lit at 2.7 s: the first tap's timer (2.6 s) did not end it
    assert out[1] is False, out                           # and it does end


@needs_browser
def test_enter_on_the_loop_title_opens_it_and_a_chip_tap_does_not_open_the_loop(tmp_path, monkeypatch):
    root, src = _sessions_fixture(tmp_path, monkeypatch)
    _spore(root)
    out = _to_work(tmp_path, src, [
        {"eval": CARD + ".querySelector('.tv-task-t').dispatchEvent(new KeyboardEvent('keydown', {key: 'Enter', bubbles: true, cancelable: true})); "
                 + PAGE_ROW + ".classList.contains('tv-lit')"},
        {"eval": PAGE_ROW + ".classList.remove('tv-lit'); " + CARD + ".querySelector('.tv-task-t').dispatchEvent(new KeyboardEvent('keydown', {key: 'Enter', repeat: true, bubbles: true})); "
                 + PAGE_ROW + ".classList.contains('tv-lit')"}])
    assert out == [True, False], out                       # a held key does not re-fire it


@needs_browser
def test_focus_returns_to_the_live_chip_when_the_agent_page_closes_after_a_repaint(tmp_path, monkeypatch):
    _root, src = _sessions_fixture(tmp_path, monkeypatch)
    chip = TV + ".querySelector('.tv-task[data-task=\"t-run\"] .tv-chip')"
    out = _run(tmp_path, src, [
        {"eval": _paint(_value())}, {"eval": "window.__old = " + chip + "; window.__old.click(); 1"},
        {"until": "document.querySelector('.agent-page')"},
        {"eval": _paint(_value())},                       # the repaint replaces the chip that opened the page
        {"eval": "(document.getElementById('levain-modal') || document).dispatchEvent(new KeyboardEvent('keydown', {key: 'Escape', bubbles: true})); "
                 "JSON.stringify([document.body.contains(window.__old), document.activeElement === " + chip + "])"}])
    assert json.loads(out[-1]) == [False, True], out


# --- round 2: the panel itself, the exact chip, the row's own tabindex, the filter kept on a failed jump -----------------


@needs_browser
def test_a_card_carrying_the_loops_panel_id_does_not_steal_the_jump(tmp_path, monkeypatch):
    root, src = _sessions_fixture(tmp_path, monkeypatch)
    _spore(root)
    out = _to_work(tmp_path, src, [
        {"eval": "window.__id = " + PAGE_ROW + ".dataset.spore; const c = document.createElement('div'); c.className = 'ck-card w'; "
                 "c.setAttribute('data-ck-panel', 'loops'); document.body.insertBefore(c, document.body.firstChild); "
                 "JSON.stringify([window.LevainLoops.go(window.__id), " + PAGE_ROW + ".classList.contains('tv-lit')])"}])
    assert json.loads(out[0]) == [True, True], out


@needs_browser
def test_a_rows_own_tabindex_is_kept_and_a_temporary_one_is_removed_on_blur(tmp_path, monkeypatch):
    root, src = _sessions_fixture(tmp_path, monkeypatch)
    _spore(root)
    out = _to_work(tmp_path, src, [
        {"eval": "window.__id = " + PAGE_ROW + ".dataset.spore; " + PAGE_ROW + ".tabIndex = 0; window.LevainLoops.go(window.__id); " + PAGE_ROW + ".tabIndex"},
        {"eval": "(async () => { const wait = () => new Promise(r => setTimeout(r, 120)); const r = " + PAGE_ROW + "; r.blur(); await wait(); r.removeAttribute('tabindex'); await wait(); "
                 "window.LevainLoops.go(window.__id); const during = r.getAttribute('tabindex'); const was = document.activeElement === r; r.dispatchEvent(new FocusEvent('blur')); await wait(); "
                 "return JSON.stringify([during, r.getAttribute('tabindex'), was]); })()"}])
    assert out[0] == 0, out
    assert json.loads(out[1]) == ["-1", None, True], out


@needs_browser
def test_a_filter_is_put_back_when_the_jump_fails_after_clearing_it(tmp_path, monkeypatch):
    root, src = _sessions_fixture(tmp_path, monkeypatch)
    _spore(root)
    inp = LOOPS + ".querySelector('.ep-search-input')"
    out = _to_work(tmp_path, src, [
        {"eval": "window.__id = " + PAGE_ROW + ".dataset.spore; " + LOOPS + ".style.display = 'none'; "   # a panel that cannot show the row once found
                 + inp + ".value = 'zzz-no-such'; " + inp + ".dispatchEvent(new Event('input', {bubbles: true})); 1"},
        {"eval": "JSON.stringify([window.LevainLoops.go(window.__id), " + inp + ".value])"}])
    assert json.loads(out[1]) == [False, "zzz-no-such"], out


@needs_browser
def test_the_card_says_the_edit_reason_only_for_the_edit_refusal(tmp_path, monkeypatch):
    root, src = _sessions_fixture(tmp_path, monkeypatch)
    _spore(root)
    out = _to_work(tmp_path, src, [
        {"eval": LOOPS + ".querySelector('.ck-lay-fold').click(); " + LOOPS + ".querySelector('.ck-lay-fold').disabled = true; " + CARD + ".click(); " + CARD + ".title"},
        {"eval": LOOPS + ".querySelector('.ck-lay-fold').disabled = false; " + LOOPS + ".querySelector('.ck-lay-fold').click(); "
                 "document.querySelector('.tv-task-t'); " + PAGE_ROW + ".style.display = 'none'; " + PAGE_ROW + ".dataset.folded = ''; delete " + PAGE_ROW + ".dataset.folded; "
                 + LOOPS + ".style.display = 'none'; " + CARD + ".click(); " + CARD + ".title"}])
    assert out[0] == "finish the open edit first", out
    assert out[1] == "could not open it in Open loops", out


@needs_browser
def test_a_card_inside_a_button_still_opens_and_the_chip_returns_to_its_own_card(tmp_path, monkeypatch):
    _root, src = _sessions_fixture(tmp_path, monkeypatch)
    spore_chip = TV + ".querySelector('.tv-task[data-task=\"spore:spore-017\"] .tv-chip')"
    out = _run(tmp_path, src, [
        {"eval": "window.LevainAgentPage = {open: (n) => { window.__pg = n.id; }}; " + _paint_with(_work_value(), True, True)},
        {"eval": "const h = document.getElementById('ck-hlens'); const b = document.createElement('div'); b.setAttribute('role', 'button'); "
                 "while (h.firstChild) b.appendChild(h.firstChild); h.appendChild(b); " + LOOP + ".click(); window.__loop"},
    ])
    assert out[-1] == "spore-017", out
    # the focus return: one worker on two cards, the later card's chip opened the page, a repaint replaced both
    out = _run(tmp_path, src, [
        {"eval": _paint(_work_value())}, {"eval": "window.__old = " + spore_chip + "; window.__old.click(); 1"},
        {"until": "document.querySelector('.agent-page')"},
        {"eval": _paint(_work_value())},
        {"eval": "(document.getElementById('levain-modal') || document).dispatchEvent(new KeyboardEvent('keydown', {key: 'Escape', bubbles: true})); "
                 "JSON.stringify([document.body.contains(window.__old), document.activeElement === " + spore_chip + "])"},
        {"eval": "document.querySelector('.agent-page') ? 1 : 0"},
    ])
    assert json.loads(out[-2]) == [False, True], out
    # and when that exact card is gone the focus goes to nothing, not to the other card's chip
    out = _run(tmp_path, src, [
        {"eval": _paint(_work_value())}, {"eval": "window.__old = " + spore_chip + "; window.__old.click(); 1"},
        {"until": "document.querySelector('.agent-page')"},
        {"eval": _paint(_value())},
        {"eval": "(document.getElementById('levain-modal') || document).dispatchEvent(new KeyboardEvent('keydown', {key: 'Escape', bubbles: true})); "
                 "JSON.stringify([document.activeElement && document.activeElement.classList.contains('tv-chip')])"}])
    assert json.loads(out[-1]) == [False], out


def _tokens_probe() -> str:
    """The Work strip's tokens stat (bold figure apart from the whole text, plus its tooltip) and the Agents metric (bold figure, sub-line, tooltip)."""
    return ("JSON.stringify({stat: (() => { const s = %s.querySelector('.tv-stat[data-k=tokens]'); return s ? {text: s.textContent.replace(/\\s+/g, ' '), "
            "bold: s.querySelector('b').textContent, title: s.title} : null; })(), "
            "metric: (() => { const m = %s.querySelector('.tv-m[data-k=tokens]'); return m ? {bold: m.querySelector('b').textContent, "
            "sub: (m.querySelector('.tv-msub') || {textContent: ''}).textContent, title: m.title} : null; })()})") % (TV, TV)


@needs_browser
def test_the_cache_reads_a_provider_reports_ride_beside_the_fresh_tokens_never_added_into_them(tmp_path, monkeypatch):
    _root, src = _sessions_fixture(tmp_path, monkeypatch)
    v = _value()
    v["counts"] = {"tokens_today": 19_700_000, "cache_read_today": 850_000_000}
    out = _run(tmp_path, src, [{"eval": _paint(v)}, {"eval": _tokens_probe()}, {"eval": _paint(v, "agents")}, {"eval": _tokens_probe()}])
    work, agents = json.loads(out[-3]), json.loads(out[-1])
    assert work["stat"]["bold"] == "19.7M", work                                          # the bold figure is the fresh figure alone
    assert "19.7M tokens today" in work["stat"]["text"] and "850.0M cache reads" in work["stat"]["text"], work
    assert "869.7M" not in work["stat"]["text"], work   # never the sum of the two
    assert "850.0M cache reads" in work["stat"]["title"], work
    assert agents["metric"]["bold"] == "19.7M" and "850.0M cache reads" in agents["metric"]["sub"], agents
    assert "the provider reports 850.0M cache reads beside it" in agents["metric"]["title"], agents


@needs_browser
def test_a_provider_that_sends_no_cache_reads_draws_none(tmp_path, monkeypatch):
    _root, src = _sessions_fixture(tmp_path, monkeypatch)
    v = _value()
    v["counts"] = {"tokens_today": 19_700_000}
    out = _run(tmp_path, src, [{"eval": _paint(v)}, {"eval": _tokens_probe()}, {"eval": _paint(v, "agents")}, {"eval": _tokens_probe()}])
    work, agents = json.loads(out[-3]), json.loads(out[-1])
    assert work["stat"]["bold"] == "19.7M" and agents["metric"]["bold"] == "19.7M", (work, agents)
    for text in (work["stat"]["text"], work["stat"]["title"], agents["metric"]["sub"], agents["metric"]["title"]):
        # the static sentence "Cache reads are not counted" is allowed; what a reported figure adds is not (case-insensitive, so a capital cannot hide it)
        low = text.lower()
        assert "cache reads beside it" not in low and "plus " not in low and " cache read" not in low.replace("cache reads are not counted", ""), (text, work, agents)


def _sig(v: dict, lens: str = "work") -> str:
    return "window.LevainTopologyView.signature(%s, %s)" % (json.dumps(v), json.dumps(lens))


@needs_browser
def test_the_repaint_signature_follows_the_displayed_cache_figure_not_the_raw_count(tmp_path, monkeypatch):
    _root, src = _sessions_fixture(tmp_path, monkeypatch)

    def with_cache(n):
        v = _value()
        v["counts"] = {"tokens_today": 19_700_000, "cache_read_today": n}
        return v
    out = _run(tmp_path, src, [{"eval": _sig(with_cache(850_000_000))}, {"eval": _sig(with_cache(850_000_001))}, {"eval": _sig(with_cache(900_000_000))}])
    same_a, same_b, other = out[-3:]
    assert same_a == same_b, (same_a, same_b)        # one display bucket (850.0M): no teardown
    assert same_a != other                            # a visible change repaints


@needs_browser
def test_a_cache_count_the_validator_would_refuse_draws_nothing_and_one_read_is_singular(tmp_path, monkeypatch):
    _root, src = _sessions_fixture(tmp_path, monkeypatch)
    steps = []
    for n in (-5, 1.5, "12", True, 1):
        v = _value()
        v["counts"] = {"tokens_today": 19_700_000, "cache_read_today": n}
        steps += [{"eval": _paint(v)}, {"eval": _tokens_probe()}, {"eval": _paint(v, "agents")}, {"eval": _tokens_probe()}]
    out = _run(tmp_path, src, steps)
    rows = [(json.loads(out[-20 + 4 * j + 1]), json.loads(out[-20 + 4 * j + 3])) for j in range(5)]
    for work, agents in rows[:4]:
        for text in (work["stat"]["text"], work["stat"]["title"], agents["metric"]["sub"], agents["metric"]["title"]):
            assert " cache read" not in text.lower().replace("cache reads are not counted", ""), (text, work, agents)
    work, agents = rows[4]
    assert "1 cache read" in work["stat"]["text"] and "1 cache reads" not in work["stat"]["text"], work
    assert "1 cache read" in agents["metric"]["sub"] and "1 cache reads" not in agents["metric"]["sub"], agents
    assert "reports 1 cache read beside it" in agents["metric"]["title"], agents


# --- the strip counts what the lens draws; the empty sentence; the jump control's affordance and focus -------------------


@needs_browser
def test_the_strips_task_counts_equal_the_task_cards_the_lens_draws_with_consent_and_question_records_hidden(tmp_path, monkeypatch):
    """Review finding (MED): consent / question task records are not drawn in the lens, so "N tasks" and "M tasks waiting" must not count them. Graded
    against the DOM: the strip's figures equal the cards on the page, while the separate "waiting on you" figure still counts the asks."""
    _root, src = _sessions_fixture(tmp_path, monkeypatch)
    v = _waiting_value(3)                              # three waiting asks, as consent / question records, beside the fixture's other tasks
    v["tasks"].append({**next(t for t in _value()["tasks"] if t["id"] == "t-sign"), "id": "t-wait-real"})        # and one waiting dispatch task, which IS drawn
    probe = ("JSON.stringify({drawn: [...%s.querySelectorAll('.tv-task')].filter(c => c.dataset.kind !== 'unattached').length, "
             "drawn_waiting: [...%s.querySelectorAll('.tv-task')].filter(c => c.dataset.kind !== 'unattached' && c.dataset.state === 'waiting').length, "
             "kinds: [...%s.querySelectorAll('.tv-task')].map(c => c.dataset.kind), "
             "tasks: +%s.querySelector('[data-k=tasks] b').textContent, tw: +%s.querySelector('[data-k=tasks-waiting] b').textContent, "
             "wy: +%s.querySelector('[data-k=waiting] b').textContent})") % ((TV,) * 6)
    got = json.loads(_run(tmp_path, src, [{"eval": _paint(v)}, {"eval": probe}])[-1])
    assert not any(k in ("consent", "question") for k in got["kinds"]), got                            # the hidden records are really not drawn
    assert got["tasks"] == got["drawn"] and got["tw"] == got["drawn_waiting"] == 1, got                # the strip counts the cards on the page
    assert got["wy"] >= 3, got                                                                         # the asks are still counted, in the figure that names them


@needs_browser
def test_with_only_hidden_asks_waiting_the_empty_sentence_does_not_deny_what_the_button_says(tmp_path, monkeypatch):
    """Review finding (LOW): nothing drawn but something waiting on you: the sentence under the button must say where the asks are, not that there are no
    records. With nothing waiting it stays the plain empty sentence."""
    _root, src = _sessions_fixture(tmp_path, monkeypatch)
    v = _waiting_value(2)
    v["tasks"] = [t for t in v["tasks"] if t["kind"] in ("consent", "question")]                       # only the asks: no task card is drawn
    v["nodes"] = [n for n in v["nodes"] if n["id"].startswith("w")]
    quiet = {**_waiting_value(0), "tasks": [], "nodes": []}
    probe = ("JSON.stringify({cards: %s.querySelectorAll('.tv-task').length, btn: !!%s.querySelector('.tv-jump'), "
             "none: (%s.querySelector('.tv-no-tasks') || {textContent: null}).textContent})") % ((TV,) * 3)
    out = _run(tmp_path, src, [{"eval": _paint(v)}, {"eval": probe}, {"eval": _paint(quiet)}, {"eval": probe}, {"eval": _paint(v, strip=False)}, {"eval": probe}])
    waiting, nothing, nostrip = json.loads(out[-5]), json.loads(out[-3]), json.loads(out[-1])
    assert waiting["cards"] == 0 and waiting["btn"] is True, waiting
    assert "no task records" not in waiting["none"].lower() and "2 waiting on you" in waiting["none"] and "Needs" in waiting["none"], waiting
    assert nostrip["btn"] is False and "2 waiting on you" in nostrip["none"] and "Needs" not in nostrip["none"], nostrip      # no strip card to go to: the sentence does not send him there
    assert nothing["cards"] == 0 and nothing["btn"] is False and nothing["none"] == "No task records and no active work.", nothing


@needs_browser
def test_the_jump_button_shows_it_is_a_button_without_hover_and_the_jump_lands_focus_on_the_lit_card(tmp_path, monkeypatch):
    """Review finding (LOW): hover-only affordance fails on touch, and the jump left keyboard focus behind. At rest the control has a border and an arrow
    (no hover state), at both widths; after the real jump through the page, focus is on the lit Needs card so Tab continues from it."""
    _root, src = _sessions_fixture(tmp_path, monkeypatch)
    probe = ("JSON.stringify((() => { const b = %s.querySelector('.tv-jump'), cs = getComputedStyle(b); "
             "return {tag: b.tagName, border: parseFloat(cs.borderTopWidth), arrow: getComputedStyle(b, '::after').content, "
             "tip: b.title, w: Math.round(b.getBoundingClientRect().width)}; })())") % TV
    for size in (DESKTOP, NARROW):
        got = json.loads(_run(tmp_path, src, [{"eval": _paint(_waiting_value(2))}, {"eval": probe}], first=size)[-1])
        assert got["tag"] == "BUTTON" and got["border"] >= 1 and got["arrow"] not in ("none", "normal", ""), (size, got)
        assert "session" in got["tip"] and "Needs" in got["tip"], (size, got)                          # the tooltip says what the figure counts and where it goes
    out = _run(tmp_path, src, [
        {"until": TV}, {"eval": "document.querySelector('#ck-hlens [data-tv=work]').click(); 1"},
        {"until": TV + " && " + TV + ".querySelector('.tv-jump')"},
        {"eval": TV + ".querySelector('.tv-jump').click(); 1"},
        {"until": "document.querySelector('#ck-needs .needs-lit[data-needs-id^=consent]')"},
        {"eval": "JSON.stringify({focus: document.activeElement === document.querySelector('#ck-needs .needs-lit'), lit: document.querySelectorAll('#ck-needs .needs-lit').length})"}])
    assert json.loads(out[-1]) == {"focus": True, "lit": 1}, out


# --- the button asks the LIVE Needs strip (L3 round 1: it used the topology's card ids) ---------------------------------------


def _strip_page(tmp_path, monkeypatch, questions: int = 2):
    """The sessions fixture (one pending consent) plus `questions` open questions: the Board's strip draws the questions first, then the consent."""
    from levain.consent.questions import QuestionStore
    _root, src = _sessions_fixture(tmp_path, monkeypatch)
    qs = QuestionStore(tmp_path / "levain_home" / "consent")
    for i in range(questions):
        qs.file(agent="claude_code", agent_session_id=f"qs{i}", what=f"Ship fix {i} tonight or hold it?", options=[("a", "Ship"), ("b", "Hold")], rec="a", blocks="the sweep", cwd="/w")
    return src


def _board(tmp_path, src, steps, rows: int = 3):
    """The Board (the strip drawn as cards) with the real Work lens up and `rows` cards in the strip."""
    ready = "document.querySelectorAll('#ck-needs .needs-row').length === %d && document.querySelector('#ck-hlens .tv')" % rows
    with _serve(src, surface_tokens={"browser": "T"}, install_class="reported") as (url, _httpd):
        return _drive(tmp_path, f"{url}/#write_token=T", [DESKTOP, {"until": ready, "ms": 30000},
                      {"eval": "document.querySelector('#ck-hlens [data-tv=work]').click(); 1"}, {"until": TV + " && " + TV + ".querySelector('.tv-jump')", "ms": 30000}, *steps])["values"]


FIRST_STATE = ("JSON.stringify((() => { const rows = [...document.querySelectorAll('#ck-needs .needs-grid .needs-row')], lit = [...document.querySelectorAll('#ck-needs .needs-lit')]; "
               "return {tab: (document.querySelector('#ck-needs .needs-tab.is-on') || {dataset: {}}).dataset.needsTab, first: rows.length ? rows[0].dataset.needsId : null, "
               "first_kind: rows.length ? rows[0].dataset.needsCard : null, lit: lit.map(n => n.dataset.needsId), "
               "focus: document.activeElement && document.activeElement.dataset ? document.activeElement.dataset.needsId || null : null, "
               "modal: !!document.querySelector('.modal-overlay, #levain-modal')}; })())")


@needs_browser
def test_the_button_lands_on_the_strips_own_first_card_not_the_first_waiting_task_in_topology_order(tmp_path, monkeypatch):
    """L3 r1 MED (all three seats): the strip draws questions first, then consents; the topology's first waiting card is the consent. The click must light and
    focus the strip's first card (a question), asked of the live strip."""
    src = _strip_page(tmp_path, monkeypatch)
    out = _board(tmp_path, src, [{"eval": TV + ".querySelector('.tv-jump').click(); 1"}, {"until": "document.querySelector('#ck-needs .needs-lit')", "ms": 10000}, {"eval": FIRST_STATE}])
    got = json.loads(out[-1])
    assert got["first_kind"] == "question" and got["first"], got                                       # the strip's order: a question leads
    assert got["lit"] == [got["first"]] and got["focus"] == got["first"] and got["modal"] is False, got   # lit, focused, no history sheet


@needs_browser
def test_with_the_desk_owes_tab_on_the_button_switches_to_needs_you_and_lands_on_its_first_card(tmp_path, monkeypatch):
    """L3 r1 MED (codex): on the Desk owes tab the waiting-on-you rows are detached, so the old jump opened Decisions history or lit a hidden chip and
    scrolled nowhere. It now switches the strip to Needs you (the strip's own tab helper) and goes to that tab's first card."""
    src = _strip_page(tmp_path, monkeypatch)
    out = _board(tmp_path, src, [{"eval": "document.querySelector('#ck-needs [data-needs-tab=desk]').click(); 1"},
                                 {"until": "document.querySelector('#ck-needs .needs-tab.is-on').dataset.needsTab === 'desk' && !document.querySelector('#ck-needs .needs-grid .needs-row')", "ms": 10000},
                                 {"eval": TV + ".querySelector('.tv-jump').click(); 1"}, {"until": "document.querySelector('#ck-needs .needs-lit')", "ms": 10000}, {"eval": FIRST_STATE}])
    got = json.loads(out[-1])
    assert got["tab"] == "you" and got["first"], got
    assert got["lit"] == [got["first"]] and got["focus"] == got["first"] and got["modal"] is False, got


@needs_browser
def test_a_strip_that_holds_nothing_or_a_replayed_frame_gives_a_plain_figure(tmp_path, monkeypatch):
    """L3 r1 MED (all three) + LOW (complement 2): the button is there only while the LIVE strip holds a card, and never over a replayed frame (the strip shows
    the present). The lens is handed the page's own LevainNeeds pair, the topology still names a waiting card, and: strip present -> button; strip gone ->
    plain figure and no "Needs strip" in the empty sentence; replay frame (strip present) -> plain figure."""
    _root, src = _sessions_fixture(tmp_path, monkeypatch)
    v = _waiting_value(2)
    v["tasks"] = [t for t in v["tasks"] if t["kind"] in ("consent", "question")]
    v["nodes"] = [n for n in v["nodes"] if n["id"].startswith("w")]
    past = {**v, "replay": True}
    probe = ("JSON.stringify({btn: !!%s.querySelector('.tv-jump'), tag: %s.querySelector('[data-k=waiting]').tagName, none: (%s.querySelector('.tv-no-tasks') || {textContent: ''}).textContent})") % ((TV,) * 3)
    out = _run(tmp_path, src, [{"until": "window.LevainNeeds && window.LevainNeeds.canGoFirst()", "ms": 30000},
                               {"eval": _paint_live(v)}, {"eval": probe},
                               {"eval": _paint_live(past)}, {"eval": probe},
                               {"eval": "document.getElementById('ck-needs').hidden = true; 1"}, {"eval": _paint_live(v)}, {"eval": probe}])
    live, replay, gone = json.loads(out[-6]), json.loads(out[-4]), json.loads(out[-1])
    assert live["btn"] is True and "in the Needs strip" in live["none"], live
    assert replay["btn"] is False and replay["tag"] == "SPAN" and "Needs strip" not in replay["none"], replay
    assert gone["btn"] is False and gone["tag"] == "SPAN" and "2 waiting on you" in gone["none"] and "Needs strip" not in gone["none"], gone


@needs_browser
def test_a_click_that_cannot_reach_the_strip_moves_nothing_and_the_lens_draws_the_figure_plain(tmp_path, monkeypatch):
    """The strip can change between the lens's draw and the click (the topology and Decisions refresh separately): the jump then moves nothing, opens no
    history, and the lens repaints without the button."""
    src = _strip_page(tmp_path, monkeypatch)
    out = _board(tmp_path, src, [{"eval": "document.getElementById('ck-needs').hidden = true; " + TV + ".querySelector('.tv-jump').click(); 1"},
                                 {"until": "!document.querySelector('#ck-hlens .tv-jump')", "ms": 10000},
                                 {"eval": "JSON.stringify({modal: !!document.querySelector('.modal-overlay, #levain-modal'), lit: document.querySelectorAll('.needs-lit').length})"}])
    assert json.loads(out[-1]) == {"modal": False, "lit": 0}, out


@needs_browser
def test_a_waiting_node_click_in_the_agents_lens_lights_its_card_but_does_not_take_focus(tmp_path, monkeypatch):
    """L3 r1 LOW (complement 6): focus moving is the Work lens's jump alone. Every other caller of openNeedsYouCard (here a waiting node's click) lights the card
    exactly as before and leaves the keyboard where it was."""
    src = _strip_page(tmp_path, monkeypatch, questions=0)
    ready = "document.querySelectorAll('#ck-needs .needs-row').length === 1 && document.querySelector('#ck-hlens .tv')"
    with _serve(src, surface_tokens={"browser": "T"}, install_class="reported") as (url, _httpd):
        out = _drive(tmp_path, f"{url}/#write_token=T", [DESKTOP, {"until": ready, "ms": 30000},
                     {"eval": "document.querySelector('#ck-hlens [data-tv=agents]').click(); 1"}, {"until": "document.querySelector('#ck-hlens .tv-open[data-card]')", "ms": 30000},
                     {"eval": "document.querySelector('#ck-hlens .tv-open[data-card]').dispatchEvent(new MouseEvent('click', {bubbles: true})); 1"},
                     {"until": "document.querySelector('#ck-needs .needs-lit')", "ms": 10000},
                     {"eval": "JSON.stringify({lit: document.querySelectorAll('#ck-needs .needs-lit').length, focus_in_strip: !!(document.activeElement && document.activeElement.closest && document.activeElement.closest('#ck-needs'))})"}])["values"]
    assert json.loads(out[-1]) == {"lit": 1, "focus_in_strip": False}, out


@needs_browser
def test_unattached_work_alone_is_drawn_and_no_empty_sentence_denies_it(tmp_path, monkeypatch):
    """Review claim (complement LOW 4A, deepseek 3) that the empty sentence sits under the unattached block: refuted by this test, which passes on the
    first run (the lens's `centre` includes the unattached work)."""
    _root, src = _sessions_fixture(tmp_path, monkeypatch)
    v = _value()
    v["tasks"] = [t for t in v["tasks"] if t["kind"] == "unattached"]
    assert v["tasks"], "the fixture carries an unattached task"
    probe = "JSON.stringify({cards: %s.querySelectorAll('.tv-task').length, none: !!%s.querySelector('.tv-no-tasks')})" % (TV, TV)
    got = json.loads(_run(tmp_path, src, [{"eval": _paint(v)}, {"eval": probe}])[-1])
    assert got["cards"] == 1 and got["none"] is False, got


# --- L3 round 2: one predicate, a button that follows the strip, a jump that keeps the saved tab ----------------------------


@needs_browser
def test_a_visible_empty_strip_with_a_stale_waiting_flag_gives_no_button(tmp_path, monkeypatch):
    """L3 r2 (codex + deepseek): the button's availability is the same visible-row predicate the jump uses. A visible grid with no row left and the waiting
    flag still "1" is not a card to go to: canGoFirst is false, goFirst moves nothing, and a lens drawn from it is a plain figure."""
    src = _strip_page(tmp_path, monkeypatch)
    out = _board(tmp_path, src, [
        {"eval": "document.querySelectorAll('#ck-needs .needs-grid .needs-row').forEach(r => r.remove()); 1"},
        {"eval": "JSON.stringify({flag: document.getElementById('ck-needs').dataset.waiting, grid: !!document.querySelector('#ck-needs .needs-grid').offsetParent, can: window.LevainNeeds.canGoFirst(), went: window.LevainNeeds.goFirst()})"},
        {"eval": _paint_live(_waiting_value(2))}, {"eval": "JSON.stringify({btn: !!%s.querySelector('.tv-jump')})" % TV}])
    state, lens = json.loads(out[-3]), json.loads(out[-1])
    assert state == {"flag": "1", "grid": True, "can": False, "went": False}, state
    assert lens == {"btn": False}, lens


@needs_browser
def test_a_refresh_that_empties_the_strip_takes_the_button_off_at_once(tmp_path, monkeypatch):
    """L3 r2 (codex F1 skew, complement 1): a Decisions-only refresh (adoptPanel) that leaves the strip empty repaints the header without the button, with no
    click and no timer to wait for."""
    src = _strip_page(tmp_path, monkeypatch)
    fresh = json.dumps({"layout": [{"id": "decisions"}], "extra_panels": {"decisions": {"lines": []}}, "targets": {"rows": {}, "panels": {}}})
    # read in the SAME turn as the refresh: the header's own 1.5 s repaint timer would otherwise hide whether the strip told it
    out = _board(tmp_path, src, [{"eval": "(() => { window.LevainDashboard.adoptPanel('decisions', %s); return JSON.stringify({flag: document.getElementById('ck-needs').dataset.waiting || null, "
                                          "hidden: document.getElementById('ck-needs').hidden, btn: !!%s.querySelector('.tv-jump'), tag: %s.querySelector('[data-k=waiting]').tagName}); })()" % (fresh, TV, TV)}])
    assert json.loads(out[-1]) == {"flag": None, "hidden": True, "btn": False, "tag": "SPAN"}, out


@needs_browser
def test_the_jump_from_desk_owes_does_not_overwrite_the_saved_tab(tmp_path, monkeypatch):
    """L3 r2 (complement LOW 2): the jump shows Needs you for this view; the reader's saved choice (Desk owes) is not rewritten."""
    src = _strip_page(tmp_path, monkeypatch)
    stored = "(() => { try { return window.localStorage.getItem('levain.needs.tab'); } catch (e) { return 'unreadable'; } })()"
    out = _board(tmp_path, src, [{"eval": "document.querySelector('#ck-needs [data-needs-tab=desk]').click(); 1"},
                                 {"until": "document.querySelector('#ck-needs .needs-tab.is-on').dataset.needsTab === 'desk'", "ms": 10000},
                                 {"eval": TV + ".querySelector('.tv-jump').click(); 1"}, {"until": "document.querySelector('#ck-needs .needs-lit')", "ms": 10000},
                                 {"eval": "JSON.stringify({tab: document.querySelector('#ck-needs .needs-tab.is-on').dataset.needsTab, stored: %s})" % stored}])
    assert json.loads(out[-1]) == {"tab": "you", "stored": "desk"}, out

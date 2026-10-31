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


def _paint(value: dict, lens: str = "work") -> str:
    """Replace the header's body with the renderer's own output for a literal value (the page's repaint leaves it: the signature is unchanged)."""
    return ("(() => { const h = document.getElementById('ck-hlens'); h.hidden = false; "
            "h.replaceChildren(window.LevainTopologyView.render(%s, {lens: %s, openCard: (id) => { window.__opened = id; }})); return 1; })()") % (json.dumps(value), json.dumps(lens))


def _run(tmp_path, src, steps, first=DESKTOP, until="document.querySelector('#k7-sessions .k7-card') && document.querySelector('#ck-hlens .tv')"):
    with _serve(src, surface_tokens={"browser": "T"}, install_class="reported") as (url, _httpd):
        return _drive(tmp_path, f"{url}/{BUILD}", [first, {"until": until}, *steps])["values"]


# --- the page, through the real provider ---------------------------------------------------------------


@needs_browser
def test_the_work_lens_draws_the_providers_own_nulls_as_not_tracked(tmp_path, monkeypatch):
    root, src = _sessions_fixture(tmp_path, monkeypatch)
    _spore(root)
    probe = ("JSON.stringify({lens: %s.dataset.lens, recs: %s.querySelectorAll('.tv-wl .tv-rec').length, "
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
    assert got["recs"] >= 1, got                                                        # the pending consents are records in the left column
    assert not any(t[1] in ("consent", "question") for t in got["tasks"]), got          # ... and not centre cards
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
def test_a_click_on_a_waiting_record_opens_the_card_it_waits_on(tmp_path, monkeypatch):
    root, src = _sessions_fixture(tmp_path, monkeypatch)
    out = _run(tmp_path, src, [
        {"until": TV}, {"eval": "document.querySelector('#ck-hlens [data-tv=work]').click(); 1"},
        {"until": TV + " && " + TV + ".querySelector('.tv-rec.tv-open')"},
        {"eval": TV + ".querySelector('.tv-rec.tv-open').click(); 1"},
        {"until": "document.querySelector('#ck-needs .needs-lit[data-needs-id^=consent]')"},      # K10 S3: the strip's card (Build: its chip) is lit, no modal
        {"eval": "JSON.stringify([!document.querySelector('.modal-overlay'), document.querySelector('#ck-needs .needs-lit').className])"}])
    assert "timeout" not in json.dumps(out[-2]), out
    assert json.loads(out[-1])[0] is True, out


# --- a literal value ---------------------------------------------------------------------------------------


@needs_browser
def test_three_columns_records_left_tasks_centre_outcomes_right_on_the_tasks_own_row(tmp_path, monkeypatch):
    _root, src = _sessions_fixture(tmp_path, monkeypatch)
    probe = ("JSON.stringify({order: [...%s.querySelectorAll('.tv-task')].map(t => t.dataset.task), "
             "recs: [...%s.querySelectorAll('.tv-rec')].map(r => [r.dataset.rec, r.dataset.card, Math.round(r.getBoundingClientRect().right)]), "
             "task_left: Math.round(%s.querySelector('.tv-task').getBoundingClientRect().left), "
             "tags: Object.fromEntries([...%s.querySelectorAll('.tv-task')].map(t => [t.dataset.task, (t.querySelector('.tv-tag') || {textContent: ''}).textContent])), "
             "outs: [...%s.querySelectorAll('.tv-outs')].map(o => [o.dataset.task, Math.round(o.getBoundingClientRect().left), "
             "Math.abs(o.getBoundingClientRect().top - %s.querySelector('.tv-task[data-task=' + o.dataset.task + ']').getBoundingClientRect().top) < 2]), "
             "legend: (%s.querySelector('.tv-legend') || {textContent: ''}).textContent, "
             "waiting_open: !!%s.querySelector('.tv-task.waiting.tv-open'), stuck_open: !!%s.querySelector('.tv-task.stuck.tv-open')})") % ((TV,) * 9)
    out = _run(tmp_path, src, [{"eval": _paint(_value())}, {"eval": probe}])
    got = json.loads(out[-1])
    assert got["order"] == ["t-sign", "t-fail", "t-stuck", "t-run", "t-done", "unattached"], got      # loudest first: waiting, failed, stuck, running, (open), done; unattached last; the consent is no centre card
    assert [r[0] for r in got["recs"]] == ["t-wait", "t-sign"], got                                    # the consent and the contract waiting on a signature
    assert all(r[2] <= got["task_left"] for r in got["recs"]), got                                     # records sit left of the tasks
    assert got["outs"] and all(o[1] > got["task_left"] and o[2] for o in got["outs"]), got              # outcomes sit right of them, on the task's own row
    assert got["tags"]["t-sign"] == "waiting on you" and got["tags"]["t-stuck"] == "stuck", got
    assert "waiting = a pending record you can answer; stuck = silence, no record" in got["legend"], got
    assert got["waiting_open"] is True and got["stuck_open"] is False, got                              # only a record can be opened; silence has none
    out = _run(tmp_path, src, [{"eval": _paint(_value())}, {"eval": TV + ".querySelector('.tv-rec').click(); window.__opened"}])
    assert out[-1] == "consent:abc", out                                                                 # the click names the card the record is


@needs_browser
def test_a_line_joins_a_record_to_the_task_waiting_on_it_and_a_task_to_its_outcomes(tmp_path, monkeypatch):
    _root, src = _sessions_fixture(tmp_path, monkeypatch)
    probe = "JSON.stringify([...%s.querySelectorAll('.tv-link')].map(p => p.getAttribute('class').replace('tv-link ', '')).sort())" % TV
    out = _run(tmp_path, src, [{"eval": _paint(_value())}, {"until": TV + ".querySelector('.tv-link')"}, {"eval": probe}])
    # record -> t-sign (it waits on the consent), the contract's own record -> t-sign, t-done -> its check, t-fail -> its cross
    assert json.loads(out[-1]) == ["bad", "ok", "waiting", "waiting"], out
    out = _run(tmp_path, src, [{"eval": _paint(_value())}, {"eval": "new Promise(r => setTimeout(r, 400))"}, {"eval": probe}], first=NARROW)
    assert json.loads(out[-1]) == [], out                                                                # stacked: no side to draw lines on


@needs_browser
def test_below_the_column_width_the_records_then_tasks_then_outcomes_stack(tmp_path, monkeypatch):
    _root, src = _sessions_fixture(tmp_path, monkeypatch)
    probe = ("JSON.stringify({rec: Math.round(%s.querySelector('.tv-rec').getBoundingClientRect().top), task: Math.round(%s.querySelector('.tv-task').getBoundingClientRect().top), "
             "out_below: (() => { const o = %s.querySelector('.tv-outs'), t = %s.querySelector('.tv-task[data-task=' + o.dataset.task + ']'); "
             "return o.getBoundingClientRect().top >= t.getBoundingClientRect().bottom - 1; })()})") % ((TV,) * 4)
    out = _run(tmp_path, src, [{"eval": _paint(_value())}, {"eval": probe}], first=NARROW)
    got = json.loads(out[-1])
    assert got["rec"] < got["task"] and got["out_below"] is True, got


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
    assert got == {"tasks": "6 tasks", "waiting": "1 waiting on you", "tasks-waiting": "2 tasks waiting", "failed": "1 failed", "stuck": "1 stuck", "done": "1 done",
                   "conflicts": "1 file conflict", "tokens": "2.4M tokens today"}, got                  # the tasks lead; "waiting on you" is waitingOnYou(v), the same figure the Agents lens shows (the fixture's one waiting task rides the waiting session n1, so it is one)
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

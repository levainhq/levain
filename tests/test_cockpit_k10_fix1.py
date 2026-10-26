"""K10 FIX1 (L3 r1 findings): the Needs-you strip keeps a card whose item still waits (a typed answer survives a new consent)."""
from __future__ import annotations

import json
import threading
import time
from datetime import timedelta
from pathlib import Path

from levain.consent.queue import ConsentQueue, authority_source
from tests.test_cockpit_flip_parity import _drive, needs_browser
from tests.test_cockpit_k2a import _install, _serve
from tests.test_cockpit_k4a_needs import DESKTOP, READY, _waiting
from tests._h4 import h4_home


@needs_browser
def test_a_typed_question_answer_survives_a_new_consent_arriving(tmp_path: Path, monkeypatch) -> None:
    home = _waiting(tmp_path, monkeypatch)
    _root, src, _ids = _install(tmp_path)

    errs: list[BaseException] = []

    def late() -> None:
      try:
        time.sleep(4.0)
        q = ConsentQueue(home / "consent", authority=authority_source(home / "consent", anchor=None), home=h4_home())
        assert q.withdraw(q.pending()[0].id)   # one leaves as one arrives: the count the manifest holds does not move
        q.file(agent="codex", agent_session_id="s3", tool_name="Edit", tool_input={"file_path": "/w/x.py"}, cwd="/w",
               hold=timedelta(minutes=40), event_id="c")
      except BaseException as e:  # noqa: BLE001
        errs.append(e)

    with _serve(src, surface_tokens={"browser": "T"}, install_class="reported") as (url, _httpd):
        t = threading.Thread(target=late, daemon=True)
        t.start()
        out = _drive(tmp_path, f"{url}/#write_token=T", [DESKTOP, {"until": READY},
            {"until": "document.querySelector('#ck-needs .question-words')"},
            {"eval": "(() => { const ta = document.querySelector('#ck-needs .question-words'); ta.value = 'my half-typed ruling';"
                     " ta.dispatchEvent(new Event('input', {bubbles: true}));"
                     " window.__ta = ta; return document.querySelectorAll('#ck-needs .needs-card').length; })()"},
            {"eval": "new Promise((res) => { const t0 = Date.now(); const tick = async () => {"
                     " try { window.__r = String(await window.LevainCockpitView.refetchPanel('decisions')); } catch (e) { window.__r = String(e); }"
                     " const n = document.querySelectorAll('#ck-needs .needs-card').length;"
                     " if ([...document.querySelectorAll('#ck-needs .consent-tool')].some((e) => e.textContent === 'Edit') || Date.now() - t0 > 20000) return res(JSON.stringify({n,"
                     " same: document.querySelector('#ck-needs .question-words') === window.__ta,"
                     " value: (document.querySelector('#ck-needs .question-words') || {}).value, r: window.__r, tools: [...document.querySelectorAll('#ck-needs .consent-tool')].map((e) => e.textContent)}));"
                     " setTimeout(tick, 250); }; tick(); })"}])["values"]
        t.join(timeout=5)
    assert not errs, errs
    typed_with = out[-2]
    got = json.loads(out[-1])
    assert typed_with == 3, typed_with           # the answer was typed before the swap landed
    assert got["n"] == 3, got
    assert got["value"] == "my half-typed ruling" and got["same"] is True, got


# --- items 3+4: the server decides the calendar day, the page draws it ---------------------------------------------------

def test_a_spore_task_carries_the_server_s_age_in_calendar_days():
    from datetime import date, datetime, timezone
    from types import SimpleNamespace

    from levain.cockpit import topology as topo

    now = datetime(2026, 10, 20, 15, 0, tzinfo=timezone.utc)
    today = now.astimezone().date()
    dated = SimpleNamespace(id="spore-1", text="a", tier="hot", created="2026-10-18")
    stamped = SimpleNamespace(id="spore-2", text="b", tier="hot", created="2026-10-01T09:30:00+00:00")
    undated = SimpleNamespace(id="spore-3", text="c", tier="hot", created="")
    tasks = {t["id"]: t for t in topo._spore_tasks([dated, stamped, undated], now)}
    assert tasks["spore:spore-1"]["age_days"] == (today - date(2026, 10, 18)).days
    assert tasks["spore:spore-2"]["age_days"] == (today - datetime(2026, 10, 1, 9, 30, tzinfo=timezone.utc).astimezone().date()).days
    assert tasks["spore:spore-3"]["age_days"] is None                    # unknown is null, never a guess


def _node(js: str) -> str:
    import shutil
    import subprocess

    node = shutil.which("node")
    if node is None:
        import pytest
        pytest.skip("node is needed")
    web = Path(__file__).resolve().parents[1] / "levain" / "templates" / "web"
    done = subprocess.run([node, "-e", f"const TV = require({json.dumps(str(web / 'topology_view.js'))}); {js}"],
                          capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    return done.stdout.strip()


def _value_for_signature() -> dict:
    return {"hub": {"id": "hub", "label": "Sage"}, "groups": [], "conflicts": None, "counts": {"tokens_today": 10}, "as_of": "2026-10-20T12:00:00+00:00",
            "lost_if_closed": [], "errors": [], "unreadable": [],
            "nodes": [{"id": "n1", "group": None, "parent": None, "label": "a", "agent": "claude_code", "model": None, "state": "running", "tool": None,
                       "tokens": 1200, "started": "2026-10-20T11:00:00Z", "last_event": "2026-10-20T11:59:00Z", "rate": 3.1, "card": None}],
            "tasks": [{"id": "spore:a", "kind": "spore", "label": "a loop", "state": "open", "waits_on": [], "card": None, "outcomes": [],
                       "cost": {"usd": None, "tokens": None, "source": None}, "workers": [], "heat": "hot", "opened": "2026-10-18T00:00:00+00:00", "age_days": 2}]}


def test_the_repaint_signature_follows_what_is_drawn_to_the_decimal_and_the_day():
    base = _value_for_signature()
    rate, age, clock = _value_for_signature(), _value_for_signature(), _value_for_signature()
    cent, subcent = _value_for_signature(), _value_for_signature()
    for v in (base, rate, age, clock):
        v["tasks"][0]["cost"] = {"usd": 1.234, "tokens": 5000, "source": "ledger"}
    cent["tasks"][0]["cost"] = {"usd": 1.24, "tokens": 5000, "source": "ledger"}        # $1.23 -> $1.24 is drawn
    subcent["tasks"][0]["cost"] = {"usd": 1.2341, "tokens": 5000, "source": "ledger"}   # $1.23 either way: not drawn
    rate["nodes"][0]["rate"] = 3.4                      # the Agents lens draws one decimal: 3.1 -> 3.4 is visible
    age["tasks"][0]["age_days"] = 3                     # "open 2 d" -> "open 3 d" after midnight, the server's day
    clock["as_of"] = "2026-10-20T12:05:00+00:00"        # the clock alone repaints nothing
    clock["nodes"][0]["last_event"] = "2026-10-20T12:04:00Z"
    clock["tasks"][0]["opened"] = "2026-10-18T00:00:01+00:00"   # not drawn any more: the server's age_days is
    sigs = json.loads(_node("const j = JSON.parse(process.argv[1]); console.log(JSON.stringify(j.map((v) => TV.signature(v, 'agents'))));"
                            .replace("process.argv[1]", json.dumps(json.dumps([base, rate, age, clock, cent, subcent])))))
    assert sigs[0] != sigs[1], "a rate that moved 0.3 events/min is drawn and must repaint"
    assert sigs[0] != sigs[2], "an age that moved a day is drawn and must repaint"
    assert sigs[0] == sigs[3], "the clock and the undrawn opened stamp must not repaint"
    assert sigs[0] != sigs[4], "a task cost that moved a cent is drawn and must repaint"
    assert sigs[0] == sigs[5], "a task cost below the drawn cent must not repaint"


_DAY_JS = r"""
const row = (id, due) => ({ id, title: id, body: id, facets: due ? { due } : {}, group: null, badges: [], actions: [] });
const m = wm("now", "list", null, { rows: [row("late", "2026-03-04"), row("same", "2026-03-05"), row("next", "2026-03-06"), row("none", null)], today: "2026-03-05" });
m.manifest.panels = { now: head("now", "list", "now", {}) };
const v = V.fromManifest(m);
out.now = Object.fromEntries(v.extra_panels.now.lines.map((l) => [l.text, l.grp]));
const tm = {
  manifest: Object.assign(manifest({ hero: [], brief: [], widgets: [], header: [], zones: [{ id: "operate", title: "Work", grid: 3, panels: ["tray"], cards: [{ panels: ["tray"], size: "tall", span: 1, tabs: null }] }] }), {}),
  panels: { tray: head("tray", "list", "Tray", { today: "2026-03-05", rows: [Object.assign(row("spore:spore-1", "2026-03-04"), { facets: { due: "2026-03-04", disposition: "seed" } })], actions: [] }) } };
tm.manifest.panels = { tray: head("tray", "list", "Tray", {}) };
const tv = V.fromManifest(tm);
out.tray = (tv.tray || []).map((s) => [s.next, s.today]);
console.log(JSON.stringify(out));
"""


def test_the_page_counts_today_and_overdue_in_the_servers_day_not_its_own(tmp_path: Path) -> None:
    import shutil
    import subprocess

    from tests.test_cockpit_ke2_page import DRIVER2, NODE, WEB

    if NODE is None or shutil.which("node") is None:
        import pytest
        pytest.skip("node is needed")
    body = DRIVER2.split("// W1:")[0] + _DAY_JS      # the fixtures, then this probe (the browser's real clock says October 2026)
    driver = tmp_path / "driver.js"
    driver.write_text(body)
    src = tmp_path / "view.js"
    src.write_text((WEB / "cockpit_view.js").read_text())
    run = subprocess.run([NODE, str(driver), str(src), "[]"], capture_output=True, text=True, timeout=60)
    assert run.returncode == 0, run.stderr
    out = json.loads(run.stdout.strip().splitlines()[-1])
    assert out["now"] == {"late": "OVERDUE", "same": "TODAY", "next": "FRI 03-06", "none": "TODAY"}, out
    assert out["tray"] == [["2026-03-04", "2026-03-05"]], out


def test_a_panel_read_carries_the_servers_calendar_day():
    from tests.test_cockpit_topology import _good, _panel

    p = _panel(_good())
    assert isinstance(p.get("today"), str) and len(p["today"]) == 10 and p["today"][4] == "-", p.get("today")


# --- items 5+6: every patch path is logged and read; one conflict per path, with its true total ----------------------

def test_a_multi_file_patch_logs_every_path_bounded_with_the_count_of_the_rest():
    import importlib.util

    from levain.cockpit.session_log import _event  # noqa: F401  (the reader the log lines must stay valid for)

    root = Path(__file__).resolve().parents[1] / "levain" / "templates"
    for rel in ("activation/hooks", "adapters/codex/activation/hooks"):
        import sys

        d = str(root / rel)
        sys.path.insert(0, d)
        try:
            sys.modules.pop("_levain_hook", None)
            spec = importlib.util.spec_from_file_location(f"ptu_{abs(hash(rel))}", root / rel / "post_tool_use.py")
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
        finally:
            sys.path.remove(d)
        patch = "*** Begin Patch\n" + "".join(f"*** Update File: src/f{i}.py\n@@\n-a\n+b\n" for i in range(3)) + "*** End Patch"
        assert mod._summary("apply_patch", {"command": patch}) == "src/f0.py\nsrc/f1.py\nsrc/f2.py", rel
        one = "*** Begin Patch\n*** Add File: only.py\n+x\n*** End Patch"
        assert mod._summary("apply_patch", {"command": one}) == "only.py", rel          # a one-file patch logs as it always did
        many = "*** Begin Patch\n" + "".join(f"*** Update File: d/f{i}.py\n" for i in range(35)) + "*** End Patch"
        got = mod._summary("apply_patch", {"command": many}).split("\n")
        assert got[:32] == [f"d/f{i}.py" for i in range(32)] and got[32] == "(+3 more)" and len(got) == 33, rel


def _conf_rig(tmp_path, monkeypatch):
    from tests.test_cockpit_topology import _rig

    return _rig(tmp_path, monkeypatch)


def test_a_patch_conflict_on_its_second_path_is_found(tmp_path, monkeypatch):
    from tests.test_cockpit_topology import _stem
    from tests.test_cockpit_topology_s5 import _tools

    ck, root, _src, _cdir, work = _conf_rig(tmp_path, monkeypatch)
    m = timedelta(minutes=1)
    shared = work / "shared"
    _tools(root, "claude_code", "s-run", [(2 * m, f"apply_patch: {shared}/a.py\n{shared}/b.py\n(+4 more)", 0)])
    _tools(root, "claude_code", "s-stall", [(3 * m, f"Edit: {shared}/b.py", 0)])
    v = ck.panel("topology", credential_class="none")["value"]
    assert v["conflicts"] == [{"path": "b.py", "nodes": sorted([_stem("claude_code", "s-run"), _stem("claude_code", "s-stall")])}], v["conflicts"]


def test_three_sessions_on_one_file_are_one_conflict_naming_all_three(tmp_path, monkeypatch):
    from tests.test_cockpit_topology import _stem
    from tests.test_cockpit_topology_s5 import _tools

    ck, root, _src, _cdir, work = _conf_rig(tmp_path, monkeypatch)
    m = timedelta(minutes=1)
    shared = str(work / "shared" / "money.py")
    for sid in ("s-run", "s-stall", "s-done"):
        _tools(root, "claude_code", sid, [(2 * m, f"Edit: {shared}", 0)])
    v = ck.panel("topology", credential_class="none")["value"]
    assert v["conflicts"] == [{"path": "money.py", "nodes": sorted(_stem("claude_code", s) for s in ("s-run", "s-stall", "s-done"))}], v["conflicts"]
    assert v["conflicts_total"] == 1


def test_the_conflict_cap_is_said_in_a_total_not_hidden(tmp_path, monkeypatch):
    from tests.test_cockpit_topology_s5 import _tools

    ck, root, _src, _cdir, work = _conf_rig(tmp_path, monkeypatch)
    m = timedelta(minutes=1)
    paths = [f"{work}/shared/f{i:02d}.py" for i in range(21)]
    for sid in ("s-run", "s-stall"):
        _tools(root, "claude_code", sid, [(2 * m, f"Edit: {p}", 0) for p in paths])
    v = ck.panel("topology", credential_class="none")["value"]
    assert len(v["conflicts"]) == 20 and v["conflicts_total"] == 21, (len(v["conflicts"]), v.get("conflicts_total"))


def test_the_validator_takes_two_or_more_distinct_known_nodes_and_a_total_not_below_the_list():
    from tests.test_cockpit_topology import _good, _panel

    def with_(conf, total=None):
        v = _good()
        ids = [n["id"] for n in v["nodes"]]
        v["conflicts"] = [{"path": "money.py", "nodes": ids[:conf]}]
        if total is not None:
            v["conflicts_total"] = total
        return v

    assert _panel(with_(3))["status"] == "ok" and _panel(with_(2, 1))["status"] == "ok"
    assert _panel(with_(1))["status"] == "error"
    bad = _panel(with_(2, 0))
    assert bad["status"] == "error" and "conflicts_total" in bad["error"], bad


@needs_browser
def test_the_header_counts_files_from_the_servers_total_and_names_every_session_on_one(tmp_path, monkeypatch):
    from tests.test_cockpit_f12b_topology_view import TV, _paint, _run, _value
    from tests.test_cockpit_k7_build import _sessions_fixture

    _root, src = _sessions_fixture(tmp_path, monkeypatch)
    v = _value()
    v["conflicts"] = [{"path": "money.py", "nodes": ["n1", "n2", "n4"]}, {"path": "tax.py", "nodes": ["n1", "n4"]}]
    v["conflicts_total"] = 25                       # the list is cut at 20: the header says the true count
    probe = ("JSON.stringify({stat: (document.querySelector('[data-k=conflicts]') || {}).textContent, sum: (%s.querySelector('.tv-conf summary') || {}).textContent,"
             " rows: [...%s.querySelectorAll('.tv-conf-row')].map(r => r.textContent)})") % (TV, TV)
    out = _run(tmp_path, src, [{"eval": _paint(v)}, {"eval": probe}])
    got = json.loads(out[-1])
    assert got["stat"] == "25 file conflicts", got
    assert got["sum"].startswith("25 files") and "showing 2" in got["sum"], got
    assert got["rows"][0] == "money.py — claude_code · ledgerline, Explore round_cents and codex · tally", got
    v["conflicts_total"] = 2
    got = json.loads(_run(tmp_path, src, [{"eval": _paint(v)}, {"eval": probe}])[-1])
    assert got["stat"] == "2 file conflicts" and "showing" not in got["sum"], got


# --- item 7: Where to begin leaves out an expired handoff, by the rule the Tray uses ---------------------------------

def test_where_to_begin_does_not_lead_with_an_expired_undated_handoff():
    from datetime import date
    from types import SimpleNamespace

    from levain.cockpit.providers import HANDOFF_FRESH_DAYS, _where_to_begin

    today = date(2026, 10, 20)
    sp = lambda i, text, seen, **o: SimpleNamespace(id=i, text=text, disposition="handoff", next=o.get("next", ""), seen=seen,  # noqa: E731
                                                    tier="warm", type="task", domain="", salience=1, created="")
    old = sp("spore-1", "a handoff nobody has touched for 26 days", "2026-09-24")
    fresh = sp("spore-2", "a handoff touched yesterday", "2026-10-19")
    edge = sp("spore-3", "a handoff on the last fresh day", (today - timedelta(days=HANDOFF_FRESH_DAYS)).isoformat())
    view = SimpleNamespace(tray=[old, fresh, edge], open_spores=[], errors={})
    ctx = SimpleNamespace(today=today, memo=lambda k, f: view)
    source = SimpleNamespace(build=lambda **kw: view)
    got = _where_to_begin(source)(ctx).value
    texts = [ln["text"] for ln in got["lines"]]
    assert "a handoff nobody has touched for 26 days" not in texts, texts
    assert texts == ["a handoff touched yesterday", "a handoff on the last fresh day"], texts


# --- item 8: a repo tag in a clause is not matched on its own text ------------------------------------------------------

@needs_browser
def test_a_stale_repo_tag_inside_a_clause_clears_when_the_rows_words_name_no_repo(tmp_path) -> None:
    _root, src, _ids = _install(tmp_path)
    mk = ("(() => { window.LevainSky.noteRepo('alphaproj'); const host = document.createElement('div'); host.dataset.ckPanel = 'episodes';"
          " host.innerHTML = '<div class=\"ck-zgrid\"><div class=\"row\" id=\"fx-row\"><span class=\"clause\"><span class=\"rp-tag\" data-repo=\"x\">alphaproj</span> plain words about nothing</span></div></div>';"
          " document.body.appendChild(host); return 1; })()")
    with _serve(src, surface_tokens={"browser": "T"}, install_class="reported") as (url, _httpd):
        out = _drive(tmp_path, f"{url}/#write_token=T", [DESKTOP, {"until": "document.querySelector('.ck-widgets .ck-card.w')"}, {"eval": mk},
            {"eval": "new Promise(r => setTimeout(() => r([document.querySelectorAll('#fx-row').length, document.querySelectorAll('#fx-row .rp-tag').length]), 3500))"}])["values"]
    assert out[-1] == [1, 0], out[-1]          # the row is still on the page, and its stale tag is gone


# --- item 9: a fold is measured per column where the columns are side by side -------------------------------------------

@needs_browser
def test_morning_s_second_column_is_not_folded_because_the_first_one_overflows(tmp_path) -> None:
    _root, src, _ids = _install(tmp_path)
    rows = lambda n, tag: "".join(f'<div class="row" data-r="{tag}{i}" data-s="height:30px;display:block">{tag}{i}</div>' for i in range(n))  # noqa: E731
    html = ('<div class="panel" data-ck-panel="tray" id="fx-panel" data-s="position:fixed;top:0;left:0;width:600px"><div class="fxhead"></div>'
            '<div id="fx-body" data-s="height:200px !important;max-height:200px !important;overflow:hidden !important;display:flex;gap:8px">'
            f'<div class="grp-col" data-s="display:block;flex:1"><div class="grp-head" data-s="height:20px">Overdue</div>{rows(12, "a")}</div>'
            f'<div class="grp-col" data-s="display:block;flex:1"><div class="grp-head" data-s="height:20px">Today</div>{rows(2, "b")}</div></div></div>')
    mk = f"(() => {{ const h = document.createElement('div'); h.innerHTML = {json.dumps(html)}; document.body.appendChild(h.firstChild); for (const n of document.querySelectorAll('[data-s]')) n.style.cssText = n.dataset.s; return 1; }})()"
    probe = ("(() => { window.LevainDashboard.measureOverflow(document.getElementById('fx-body'));"
             " const hid = (t) => [...document.querySelectorAll('#fx-body [data-r^=' + t + ']')].filter(n => n.style.display === 'none').length;"
             " return JSON.stringify({a: hid('a'), b: hid('b'), foot: (document.querySelector('#fx-panel .more-foot') || {}).textContent}); })()")
    with _serve(src, surface_tokens={"browser": "T"}, install_class="reported") as (url, _httpd):
        out = _drive(tmp_path, f"{url}/#write_token=T", [DESKTOP, {"until": "document.querySelector('.ck-widgets .ck-card.w')"}, {"eval": mk}, {"eval": probe}])["values"]
    got = json.loads(out[-1])
    assert got["b"] == 0, got                      # Today fits its column: nothing in it is folded
    assert got["a"] > 0 and got["foot"] == f"{got['a']} more", got


# --- item 10: the Decisions tile does not draw the generic first line when its glance face draws the tile ----------------

@needs_browser
def test_the_decisions_tile_draws_the_glance_lines_and_not_a_generic_first_line_before_them(tmp_path, monkeypatch) -> None:
    from tests.test_cockpit_k4a_needs import TILE

    _waiting(tmp_path, monkeypatch)
    _root, src, _ids = _install(tmp_path)
    with _serve(src, surface_tokens={"browser": "T"}, install_class="reported") as (url, _httpd):
        out = _drive(tmp_path, f"{url}/#write_token=T", [DESKTOP, {"until": READY},
            {"eval": f"JSON.stringify([...{TILE}.querySelectorAll('.sub')].map(e => [e.className, e.textContent]))"}])["values"]
    subs = json.loads(out[-1])
    assert [c for c, _t in subs] == ["sub glance-top", "sub glance-sum"], subs


def test_the_two_server_views_count_a_spores_age_from_the_same_local_day(monkeypatch) -> None:
    """L3 r2 (codex): `2026-10-20T00:30:00Z` is 10-19 in New York; topology and the Open loops row must both say 1 day old."""
    import os
    from datetime import date, datetime, timezone
    from types import SimpleNamespace

    from levain.cockpit import providers as prov
    from levain.cockpit import topology as topo

    old = os.environ.get("TZ")
    monkeypatch.setenv("TZ", "America/New_York")
    time.tzset()
    try:
        s = SimpleNamespace(id="spore-9", text="t", tier="hot", created="2026-10-20T00:30:00Z", disposition="loop", domain="d",
                            salience=1, type="x", seen="", next=None, pointer=None, origin_key="")
        now = datetime(2026, 10, 20, 16, 0, tzinfo=timezone.utc)      # 12:00 in New York
        row = prov._spore_row(s, date(2026, 10, 20))
        task = topo._spore_tasks([s], now)[0]
        assert row.facets["age_days"] == task["age_days"] == 1, (row.facets["age_days"], task["age_days"])
    finally:
        if old is None:
            monkeypatch.delenv("TZ", raising=False)
        else:
            monkeypatch.setenv("TZ", old)
        time.tzset()


@needs_browser
def test_a_kept_consent_card_takes_the_new_expiry_when_the_same_id_comes_back_extended(tmp_path: Path, monkeypatch) -> None:
    """L3 r2 (complement LOW): the card is kept by id, but its countdown must follow a consent whose expiry moved."""
    _waiting(tmp_path, monkeypatch)
    _root, src, _ids = _install(tmp_path)
    new = "2099-01-01T00:00:00+00:00"
    with _serve(src, surface_tokens={"browser": "T"}, install_class="reported") as (url, _httpd):
        out = _drive(tmp_path, f"{url}/#write_token=T", [DESKTOP, {"until": READY},
            {"until": "document.querySelector('#ck-needs .consent-left')"},
            {"eval": "(() => { window.__card = document.querySelector('#ck-needs .needs-consent');"
                     " window.__before = window.__card.querySelector('.consent-left').dataset.consentExpires;"
                     " const f = window.fetch; window.fetch = async (...a) => { const r = await f(...a);"
                     " const t = await r.clone().text(); if (!/expires_at/.test(t)) return r;"
                     f" return new Response(t.replace(/\"expires_at\":\\s*\"[^\"]*\"/g, '\"expires_at\": \"{new}\"'), {{status: r.status, headers: r.headers}}); }};"
                     " return window.__before; })()"},
            {"eval": "window.LevainCockpitView.refetchPanel('decisions').then((r) => JSON.stringify({ r: r, "
                     " same: document.querySelector('#ck-needs .needs-consent') === window.__card,"
                     " before: window.__before, after: document.querySelector('#ck-needs .needs-consent .consent-left').dataset.consentExpires,"
                     " all: [...document.querySelectorAll('#ck-needs .needs-consent .consent-left')].map((e) => e.dataset.consentExpires)}))"}])["values"]
    got = json.loads(out[-1])
    assert got["before"] != new and got["after"] == new, got
    assert got["same"] is True, got              # the card was kept, not rebuilt

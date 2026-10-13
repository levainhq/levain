"""KE3 L3 r1, page side: the compose card posts to its own panel, and the page refuses what it cannot honour.

The page code (cockpit_view.js + dashboard_boot.js) runs under node against a fake server, as in
test_cockpit_r1_page.py. Each case names the finding it was written from (KE3/L3_r1/TRIAGE.md):

* cx2: two panels offer `send_inbox` (flow draws the inbox list and its separate inbox-compose form); a send from
  one card must post to THAT panel, and a card whose panel no longer offers the verb refuses, never falls
  through to another panel;
* cx1: a recipient enum that is blank or repeated after the page's trim refuses the panel; a chosen value that
  normalises to nothing refuses the send (never a silent broadcast);
* cx4 / comp4: a job verb or a second recipient field refuses the panel;
* comp3: an addressed row keeps its meta (badges, provenance) after the address line;
* comp6: a writable surface whose panel does not offer the compose verb reads unavailable with the reason; a
  read-only one stays silent.

Fixtures are synthetic; no kernel, store or browser is involved."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

WEB = Path(__file__).resolve().parent.parent / "levain" / "templates" / "web"
NODE = shutil.which("node")

DRIVER = r"""
const fs = require("fs");
const View = require(process.argv[2]);
const bootSrc = fs.readFileSync(process.argv[3], "utf8");
const only = process.argv[4] || null;

const SEND = { label: "Send to inbox", tier: "T2", confirm: true, idempotent: false, job: false, fields: ["message", "for"], effect: "sends a note", gesture: "direct-reported" };
const COMPOSE = () => ({ verb: "send_inbox", fields: [{ name: "message", type: "text", label: "Message" },
  { name: "for", type: "recipient", label: "To", enum: ["cli", "daemon"] }] });
const ACT = (pid) => ({ verb: "send_inbox", tier: "T2", gesture: "direct-reported", escalates: [], target: { panel_id: pid } });

function fixture(over) {
  over = over || {};
  const head = (id, kind, extra) => Object.assign({ id, kind, title: id, status: "ok", as_of: "2026-10-12T12:00:00+00:00" }, extra || {});
  const compose = over.compose === undefined ? COMPOSE() : over.compose;
  const inboxExtra = compose ? { compose } : {};
  const manifest = { schema: "levain.cockpit/1", entity: { name: "t", governance: "reported", brand: {} },
    generated_at: "2026-10-12T12:00:00+00:00", regions: { header: [], zones: [{ id: "operate", title: "Operate", panels: ["inbox", "inbox-compose"] }] },
    panels: { inbox: head("inbox", "list", inboxExtra), "inbox-compose": head("inbox-compose", "form") },
    verbs: { send_inbox: Object.assign({}, SEND, over.head || {}) }, errors: [],
    transport: over.transport || { writable: true, write_token_required: false } };
  const panels = {
    inbox: Object.assign(head("inbox", "list", inboxExtra), {
      rows: [{ id: "m1", version: "v1", title: "hello", body: "hello", group: null, badges: [{ kind: "age_days", value: 3 }],
               facets: { source: "bridge", for: "cli", at: "2026-10-12T11:00:00+00:00" } }],
      actions: over.inboxActions === undefined ? [ACT("inbox")] : over.inboxActions }),
    "inbox-compose": Object.assign(head("inbox-compose", "form"), { value: COMPOSE(), actions: [ACT("inbox-compose")] }),
  };
  return { manifest, panels };
}

function makeEnv(over) {
  over = over || {};
  const fx = fixture(over);
  const posts = [], renders = [];
  const store = () => { const m = {}; return { getItem: (k) => (k in m ? m[k] : null), setItem: (k, v) => { m[k] = String(v); }, removeItem: (k) => { delete m[k]; } }; };
  let n = 0;
  const resp = (status, body) => ({ status, ok: status >= 200 && status < 300, json: async () => body, clone() { return this; } });
  async function fetchFn(url, init) {
    if (init && init.method === "POST") {
      const body = JSON.parse(init.body);
      posts.push({ url, body });
      if (body.dry_run) return resp(200, { ok: true, dry_run: true, tier: "T2", rendered_tier: "T2", gesture: "direct-reported" });
      return resp(200, { ok: true, state: "COMMITTED" });
    }
    if (url.indexOf("/cockpit/manifest.json") >= 0) return resp(200, fx.manifest);
    const m = /\/cockpit\/panel\/([^?]+)\.json/.exec(url);
    const pid = m ? decodeURIComponent(m[1]) : null;
    if (pid && fx.panels[pid]) return resp(200, fx.panels[pid]);
    return resp(404, {});
  }
  const ls = store(); ls.setItem("levain_write_token", "tok");
  const win = { LevainCockpitView: View, localStorage: ls, sessionStorage: store(), location: { search: "?source=manifest", hash: "", pathname: "/" },
    crypto: { randomUUID: () => "key-" + (++n) }, prompt: () => "",
    LevainDashboard: { render: (view, opts) => renders.push({ view, opts }), hasUnsavedEdit: () => false, jobsActive: () => false } };
  const doc = { getElementById: () => null, addEventListener() {}, querySelectorAll: () => [], createElement: () => ({ className: "", textContent: "" }), visibilityState: "visible" };
  const env = { posts, renders, fx };
  env.start = async () => {
    new Function("window", "document", "location", "history", "fetch", "console", bootSrc)(win, doc, win.location, { replaceState() {} }, fetchFn, console);
    await new Promise((r) => setTimeout(r, 30));
    return env;
  };
  env.opts = () => renders[renders.length - 1].opts;
  env.view = () => renders[renders.length - 1].view;
  return env;
}

let failed = 0;
function ok(cond, what) { if (!cond) { failed++; console.log("FAIL " + what); } }
const errOf = (v, pid) => (v.extra_panels[pid] || {}).error || "";
const cases = {};

cases.cx2_each_card_action_names_its_own_panel = async () => {
  const v = (await makeEnv().start()).view();
  ok(v.extra_panels.inbox.action && v.extra_panels.inbox.action.panel === "inbox", "the list's compose names inbox: " + JSON.stringify(v.extra_panels.inbox.action));
  ok(v.extra_panels["inbox-compose"].action && v.extra_panels["inbox-compose"].action.panel === "inbox-compose", "the form names inbox-compose");
};

cases.cx2_a_send_posts_to_the_panel_whose_card_it_is = async () => {
  const env = await makeEnv().start();
  const r = await env.opts().commitAction("send_inbox", { message: "x" }, true, "k1", "inbox-compose");
  ok(r && r.ok === true, "one click fires: " + JSON.stringify(r));
  ok(env.posts.length > 0 && env.posts.every((p) => p.body.panel_id === "inbox-compose"), "every post names inbox-compose: " + JSON.stringify(env.posts.map((p) => p.body.panel_id)));
  const env2 = await makeEnv().start();
  await env2.opts().commitAction("send_inbox", { message: "x" }, true, "k1", "inbox");
  ok(env2.posts.length > 0 && env2.posts.every((p) => p.body.panel_id === "inbox"), "every post names inbox");
};

cases.cx2_a_card_whose_panel_no_longer_offers_the_verb_refuses_and_never_falls_through = async () => {
  const env = await makeEnv({ inboxActions: [] }).start();
  const r = await env.opts().commitAction("send_inbox", { message: "x" }, true, "k1", "inbox");
  ok(r && r.ok === false && r.error === "not_offered", "refused: " + JSON.stringify(r));
  ok(/inbox/.test(r.message || ""), "the refusal names the panel");
  ok(env.posts.length === 0, "nothing posted (inbox-compose still offers it, and was not used)");
  const r2 = await env.opts().commitAction("send_inbox", { message: "x" }, true, "k2", "gone");
  ok(r2 && r2.ok === false && r2.error === "not_offered" && env.posts.length === 0, "an unknown panel refuses");
};

cases.cx1_a_blank_or_repeated_recipient_enum_refuses_the_panel = async () => {
  for (const en of [["   "], ["cli", " cli"]]) {
    const c = COMPOSE(); c.fields[1].enum = en;
    const v = (await makeEnv({ compose: c }).start()).view();
    ok(/recipient/.test(errOf(v, "inbox")), JSON.stringify(en) + " refuses inbox: " + errOf(v, "inbox"));
  }
};

cases.comp4_a_second_recipient_field_refuses_the_panel = async () => {
  const c = COMPOSE(); c.fields.push({ name: "cc", type: "recipient", enum: ["cli"] });
  const v = (await makeEnv({ compose: c }).start()).view();
  ok(/one recipient/.test(errOf(v, "inbox")), "refused: " + errOf(v, "inbox"));
};

cases.cx4_a_job_verb_refuses_the_compose_panel = async () => {
  const v = (await makeEnv({ head: { job: true } }).start()).view();
  ok(/job/.test(errOf(v, "inbox")), "refused: " + errOf(v, "inbox"));
};

cases.comp3_an_addressed_row_keeps_its_meta_after_the_address = async () => {
  const v = (await makeEnv().start()).view();
  const line = v.extra_panels.inbox.lines[0];
  ok(line.addr && line.addr.source === "bridge" && line.addr.to.join() === "cli", "address kept: " + JSON.stringify(line.addr));
  ok(typeof line.meta === "string" && line.meta.length > 0, "meta kept: " + JSON.stringify(line.meta));
};

cases.comp6_writable_and_the_compose_verb_not_offered_reads_unavailable = async () => {
  const v = (await makeEnv({ inboxActions: [] }).start()).view();
  ok(/send_inbox/.test(errOf(v, "inbox")) && /offer/.test(errOf(v, "inbox")), "unavailable with the reason: " + errOf(v, "inbox"));
};

cases.comp6_read_only_stays_silent = async () => {
  const v = (await makeEnv({ inboxActions: [], transport: { writable: false } }).start()).view();
  ok(!errOf(v, "inbox"), "no error on a read-only surface: " + errOf(v, "inbox"));
  ok(!v.extra_panels.inbox.action, "and no card");
};

cases.a_valid_compose_still_draws_its_card = async () => {
  const v = (await makeEnv().start()).view();
  const a = v.extra_panels.inbox.action;
  ok(!errOf(v, "inbox") && a && a.card === true && a.fields.some((f) => f.chips), "card with chips: " + JSON.stringify(a));
};

(async () => {
  for (const name of Object.keys(cases)) {
    if (only && name !== only) continue;
    const before = failed;
    try { await cases[name](); } catch (e) { failed++; console.log("FAIL " + name + " threw " + (e && e.stack || e)); }
    if (failed === before) console.log("OK " + name);
  }
  process.exit(failed ? 1 : 0);
})();
"""

PARAMS = r"""
const fs = require("fs");
const text = fs.readFileSync(process.argv[2], "utf8");
const a = text.indexOf("ACTION-EXTRACT-START"), b = text.indexOf("ACTION-EXTRACT-END");
const block = text.slice(text.lastIndexOf("\n", a) + 1, text.indexOf("\n", b) + 1);
const { collectActionParams } = new Function(block + "\nreturn { collectActionParams };")();
const out = {
  blank: collectActionParams([{ name: "for", kind: "multiselect", required: false, label: "To", value: ["   "] }]),
  nonstr: collectActionParams([{ name: "for", kind: "multiselect", required: false, label: "To", value: [7] }]),
  none: collectActionParams([{ name: "for", kind: "multiselect", required: false, label: "To", value: [] }]),
  one: collectActionParams([{ name: "for", kind: "multiselect", required: false, label: "To", value: [" cli "] }]),
};
process.stdout.write(JSON.stringify(out));
"""


def _run(case: str | None, tmp_path: Path) -> subprocess.CompletedProcess[str]:
    drv = tmp_path / "ke3_page_driver.js"
    drv.write_text(DRIVER, encoding="utf-8")
    args = [NODE, str(drv), str(WEB / "cockpit_view.js"), str(WEB / "dashboard_boot.js")]
    if case:
        args.append(case)
    return subprocess.run(args, capture_output=True, text=True, timeout=60)


def _cases() -> list[str]:
    import re

    return re.findall(r"^cases\.(\w+) = ", DRIVER, flags=re.M)


@pytest.mark.skipif(NODE is None, reason="node is not installed")
@pytest.mark.parametrize("case", _cases())
def test_ke3_page_case(case: str, tmp_path: Path) -> None:
    r = _run(case, tmp_path)
    assert r.returncode == 0 and f"OK {case}" in r.stdout, r.stdout + r.stderr


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_cx1_a_chosen_recipient_that_normalises_to_nothing_refuses_the_send(tmp_path: Path) -> None:
    import json

    drv = tmp_path / "params.js"
    drv.write_text(PARAMS, encoding="utf-8")
    r = subprocess.run([NODE, str(drv), str(WEB / "dashboard_core.js")], capture_output=True, text=True, timeout=15)
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    assert "error" in out["blank"] and "error" in out["nonstr"], out       # never {params: {}} = a broadcast
    assert out["none"] == {"params": {}}                                   # nothing chosen IS everyone
    assert out["one"] == {"params": {"for": ["cli"]}}


def test_cx2_the_card_send_carries_its_panel_to_commit_action() -> None:
    """Structural (buildActionBox needs a DOM this suite does not fake): both non-job sends from the compose box
    pass the action's own panel as commitAction's fifth argument; the behaviour of that argument is driven above."""
    core = (WEB / "dashboard_core.js").read_text(encoding="utf-8")
    box = core[core.index("function buildActionBox"):core.index("function buildPendingCancel")]
    sends = [ln for ln in box.splitlines() if "commitAction(action.verb" in ln]
    assert len(sends) == 2 and all("action.panel" in ln for ln in sends), sends

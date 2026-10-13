"""R1 write half, page side (`R1_WRITE_PLAN.md` W1 decisions, rev 2, design r2 triage): the manifest source
writes through POST /cockpit/verb, and the page decides no tier.

The page code runs under node against a fake server that answers what the kernel answers. Each case names
the failure it was written from:

* an escalating write sent with no intent reaches the kernel's `needs_intent` (the pre-R1 page's failure);
* a fire's intent and tier come from the DRY RUN, never from the rendered row action;
* a lost answer is replayed under the same key, and an authoritative answer clears it;
* a refused gesture never posts; a downstream verb the broker cannot fire shows the server's text.

Fixtures are synthetic; no kernel, store or browser is involved."""

from __future__ import annotations

import json
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

const ROW = (verb, tier, gesture, escalates, extra) => Object.assign({ verb, tier, gesture, escalates: escalates || [],
  target: { panel_id: "loops", row_id: "spore:spore-1", row_version: "v1" } }, extra || {});
const UPDATE_ESC = [{ param: "text", op: "present", tier: "T2" }, { param: "domain", op: "present", tier: "T2" }];
const HEADS = {
  spore_touch: { label: "touch", tier: "C1", confirm: false, idempotent: false, fields: [], effect: "marks seen", gesture: "direct" },
  spore_update: { label: "edit", tier: "C1", confirm: false, idempotent: false, fields: ["text", "domain"], effect: "changes the text", gesture: "direct" },
  spore_surface_at: { label: "snooze", tier: "C1", confirm: false, idempotent: true, fields: ["surface_at"], effect: "sets the date", gesture: "direct" },
  spore_descend: { label: "resolve", tier: "C1", confirm: true, idempotent: false, fields: ["spore_kind"], effect: "resolves", gesture: "direct" },
  spore_set_disposition: { label: "move", tier: "T2", confirm: false, idempotent: false, fields: ["disposition"], effect: "moves", gesture: "direct-reported" },
  operator_state: { label: "set state", tier: "C1", confirm: false, idempotent: false, fields: ["text"], effect: "sets state", gesture: "direct" },
  send_inbox: { label: "Send to inbox", tier: "T2", confirm: true, idempotent: false, fields: ["message", "for"], effect: "sends a note", gesture: "direct-reported" },
};

function fixture(over) {
  over = over || {};
  const acts = over.actions || [ROW("spore_touch", "C1", "direct"), ROW("spore_update", "C1", "direct", UPDATE_ESC),
    ROW("spore_surface_at", "C1", "direct", [{ param: "surface_at", op: "before", value: "2026-12-01", tier: "T2" }]),
    ROW("spore_set_disposition", "T2", "direct-reported"),
    ROW("spore_descend", "C1", "direct", [], { options: { spore_kind: ["composted", "completed"] } })];
  const head = (id, kind) => ({ id, kind, title: id, status: "ok", as_of: "2026-10-11T12:00:00+00:00" });
  const manifest = { schema: "levain.cockpit/1", entity: { name: "t", governance: "reported", brand: {} },
    generated_at: "2026-10-11T12:00:00+00:00", regions: { header: ["state"], zones: [{ id: "operate", title: "Operate", panels: ["loops", "decisions", "inbox"] }] },
    panels: { state: head("state", "line"), loops: head("loops", "triage-list"), decisions: head("decisions", "triage-list"), inbox: head("inbox", "triage-list") },
    verbs: HEADS, errors: [], transport: over.transport || { writable: true, write_token_required: false } };
  const panels = {
    state: Object.assign(head("state", "line"), { value: { lines: [{ label: "state", text: "hi", at: "2026-10-11T11:00:00.000Z", source: "web" }], value_version: "sv1" },
      actions: [{ verb: "operator_state", tier: "C1", gesture: "direct", escalates: [], target: { panel_id: "state", panel_version: "sv1" } }] }),
    loops: Object.assign(head("loops", "triage-list"), { rows: [{ id: "spore:spore-1", version: "v1", title: "one", body: "one", group: null,
      facets: { disposition: "loop", domain: "apps", tier: "hot", spore_type: "task" }, actions: acts }], actions: [] }),
    decisions: Object.assign(head("decisions", "triage-list"), { groups: [{ id: "waiting", title: "Waiting on a signature", count: 1 }],
      rows: [{ id: "pend:cp1", version: "pv", title: "move on spore:spore-1", group: "waiting", facets: { pending_tier: "T2" },
        actions: [{ verb: "pending_cancel", tier: "T2", gesture: "direct-reported", escalates: [], target: { kind: "pending", pending_id: "cp1" }, route: "/cockpit/pending/cp1/cancel" }] }], actions: [] }),
    inbox: Object.assign(head("inbox", "triage-list"), { rows: [], actions: over.inboxActions === undefined
      ? [{ verb: "send_inbox", tier: "T2", gesture: "direct-reported", escalates: [], target: { panel_id: "inbox" } }] : over.inboxActions }),
  };
  return { manifest, panels };
}

function makeEnv(over) {
  over = over || {};
  const fx = fixture(over);
  const posts = [], reads = [], renders = [];
  const ss = Object.assign({}, over.session || {});
  const sessionStorage = { getItem: (k) => (k in ss ? ss[k] : null), setItem: (k, v) => { ss[k] = String(v); }, removeItem: (k) => { delete ss[k]; } };
  const ls = Object.assign({}, over.local || { levain_write_token: "tok" });
  const localStorage = { getItem: (k) => (k in ls ? ls[k] : null), setItem: (k, v) => { ls[k] = String(v); }, removeItem: (k) => { delete ls[k]; } };
  let n = 0;
  const resp = (status, body) => ({ status, ok: status >= 200 && status < 300, json: async () => body, clone() { return this; } });
  const env = { posts, reads, renders, ss, fx };
  env.verbAnswers = over.verbAnswers || [];
  async function fetchFn(url, init) {
    if (init && init.method === "POST") {
      const body = JSON.parse(init.body);
      posts.push({ url, body, headers: init.headers });
      const a = env.verbAnswers.length ? env.verbAnswers.shift() : null;
      if (a === "net") throw new Error("connection reset");
      if (a) return resp(a[0], a[1]);
      return resp(200, { ok: true, state: "COMMITTED", label: "claims cockpit token" });
    }
    if (env.holdReads && url.indexOf("/cockpit/panel/") >= 0) await env.holdReads;
    reads.push(url);
    if (url.indexOf("/cockpit/manifest.json") >= 0) return resp(200, fx.manifest);
    const m = /\/cockpit\/panel\/([^/.]+)\.json/.exec(url);
    if (m && fx.panels[m[1]]) return resp(200, fx.panels[m[1]]);
    return resp(404, {});
  }
  const win = { LevainCockpitView: View, localStorage, sessionStorage, location: { search: "?source=manifest", hash: "", pathname: "/" },
    crypto: { randomUUID: () => "key-" + (++n) }, prompt: over.prompt ? () => over.prompt(env) : () => "",
    LevainDashboard: { render: (view, opts) => renders.push({ view, opts }), hasUnsavedEdit: () => false, jobsActive: () => false } };
  env.listeners = {};
  const doc = { getElementById: () => null, addEventListener(t, f) { (env.listeners[t] = env.listeners[t] || []).push(f); }, querySelectorAll: (q) => (env.rows && q === "[data-row-key]" ? env.rows : []), createElement: () => ({ className: "", textContent: "" }), visibilityState: "visible" };
  env.start = async () => {
    new Function("window", "document", "location", "history", "fetch", "console", bootSrc)(win, doc, win.location, { replaceState() {} }, fetchFn, console);
    await new Promise((r) => setTimeout(r, 30));
    return env;
  };
  env.opts = () => renders[renders.length - 1].opts;
  env.view = () => renders[renders.length - 1].view;
  env.settle = () => new Promise((r) => setTimeout(r, 30));
  return env;
}

let failed = 0;
function ok(cond, what) { if (!cond) { failed++; console.log("FAIL " + what); } }
const cases = {};

cases.mapper_carries_the_write_surface = async () => {
  const env = await makeEnv().start();
  const v = env.view();
  const s = v.open_spores[0];
  ok(s.panel_id === "loops" && s.row_id === "spore:spore-1" && s.row_version === "v1", "row target kept");
  ok(Array.isArray(s.actions) && s.actions.length === 5, "row actions kept");
  ok(s.domain === "apps", "domain kept");
  ok(JSON.stringify(s.descend_kinds) === '["composted","completed"]' && s.ascend_kinds.length === 0, "kind options from the action");
  ok(v.writable === true && v.write_token_required === false, "writable from transport");
  ok(v.layout.find((e) => e.id === "loops").edit_class === "B", "edit_class B where rows carry actions");
  ok(v.state.version === "sv1" && v.state.can_edit === true, "state keeps its panel version");
  ok(typeof env.opts().commit === "function", "a writable transport renders with a commit");
};

cases.no_transport_means_read_only = async () => {
  const env = makeEnv();
  delete env.fx.manifest.transport;
  await env.start();
  ok(env.view().writable === false, "no transport, not writable");
  ok(env.opts().commit === undefined, "no commit offered");
};

cases.direct_c1_with_no_escalation_fires_once_with_no_intent = async () => {
  const env = await makeEnv().start();
  const r = await env.opts().commit({ kind: "spore_touch", spore_id: "spore-1" });
  ok(r.ok === true, "touch landed");
  ok(env.posts.length === 1 && env.posts[0].url === "/cockpit/verb", "one post to /cockpit/verb");
  const b = env.posts[0].body;
  ok(b.verb === "spore_touch" && b.panel_id === "loops" && b.row_id === "spore:spore-1" && b.row_version === "v1", "target named");
  ok(!("intent" in b) && !("tier_seen" in b) && !("idempotency_key" in b) && !("dry_run" in b), "no intent, no key, no dry run");
  ok(!env.posts.some((p) => p.url === "/edit" || p.url === "/action"), "never the legacy routes");
};

cases.an_escalating_write_fires_on_one_click_with_the_dry_run_tier = async () => {
  const env = makeEnv();
  env.verbAnswers = [[200, { ok: true, dry_run: true, tier: "T2", rendered_tier: "C1", gesture: "direct-reported" }]];
  await env.start();
  const req = { kind: "spore_update", spore_id: "spore-1", text: "new text" };
  const r = await env.opts().commit(req);
  ok(r.ok === true && !r.preview, "one click fires: " + JSON.stringify(r));
  ok(env.posts.length === 2 && env.posts[0].body.dry_run === true, "a dry run, then the fire");
  ok(!("idempotency_key" in env.posts[0].body) && !("tier_seen" in env.posts[0].body) && !("intent" in env.posts[0].body), "a dry run carries no key, no tier_seen, no intent");
  const f = env.posts[1].body;
  ok(f.intent === "reported" && f.tier_seen === "T2" && f.gesture_seen === "direct-reported", "intent and seen tier come from the dry run");
  ok(typeof f.idempotency_key === "string" && f.idempotency_key, "a T2 fire carries a key");
  ok(f.params.text === "new text" && !("dry_run" in f), "exactly the dry run's params");
};

cases.the_fire_uses_the_dry_run_gesture_not_the_rendered_one = async () => {
  // the row rendered `direct` (C1); the kernel's dry run says T2 sign: the fire sends sign, never reported or nothing
  const env = makeEnv();
  env.verbAnswers = [[200, { ok: true, dry_run: true, tier: "T2", rendered_tier: "C1", gesture: "sign" }]];
  await env.start();
  const req = { kind: "spore_update", spore_id: "spore-1", text: "x" };
  await env.opts().commit(req);
  ok(env.posts[1].body.intent === "sign" && env.posts[1].body.gesture_seen === "sign", "sign from the dry run");
};

cases.a_dry_run_that_stays_c1_fires_at_once = async () => {
  const env = makeEnv();
  env.verbAnswers = [[200, { ok: true, dry_run: true, tier: "C1", rendered_tier: "C1", gesture: "direct" }]];
  await env.start();
  const r = await env.opts().commit({ kind: "spore_surface_at", spore_id: "spore-1", surface_at: "2026-12-24" });
  ok(r.ok === true && env.posts.length === 2, "dry run then fire in one click");
  const f = env.posts[1].body;
  ok(!("intent" in f) && !("tier_seen" in f), "C1 sends no intent");
  ok(typeof f.idempotency_key === "string", "an idempotent C1 verb sends a key");
};

cases.stale_and_tier_moved_reread_and_never_retry_with_an_intent = async () => {
  for (const [code, status] of [["stale", 409], ["tier_moved", 409], ["gesture_moved", 409], ["needs_intent", 409]]) {
    const env = makeEnv();
    env.verbAnswers = [[200, { ok: true, dry_run: true, tier: "T2", rendered_tier: "C1", gesture: "direct-reported" }],
                       [status, { error: code, message: "the server says " + code }]];
    await env.start();
    const req = { kind: "spore_update", spore_id: "spore-1", text: "x" };
    const before = env.reads.length;
    const r = await env.opts().commit(req);
    await env.settle();
    ok(r.ok === false && r.error === code && /the server says/.test(r.message), code + " answer shown");
    ok(env.posts.length === 2, code + ": no automatic retry");
    ok(env.reads.length > before, code + ": the page re-read");
    env.verbAnswers = [[200, { ok: true, dry_run: true, tier: "T3", rendered_tier: "C1", gesture: "sign" }]];
    await env.opts().commit(req);
    ok(env.posts[2].body.dry_run === true && env.posts[3].body.tier_seen === "T3" && env.posts[3].body.gesture_seen === "sign",
       code + ": the next click asks again and fires the tier it is told now");
  }
};

cases.a_lost_answer_replays_under_the_same_key_and_an_answer_clears_it = async () => {
  const env = makeEnv();
  env.verbAnswers = [[200, { ok: true, dry_run: true, tier: "T2", rendered_tier: "C1", gesture: "direct-reported" }], "net"];
  await env.start();
  const req = { kind: "spore_update", spore_id: "spore-1", text: "x" };
  const r = await env.opts().commit(req);
  ok(r.ok === false && r.error === "network", "a lost answer is reported as such");
  const stored = JSON.parse(env.ss.levain_cockpit_keys);
  const keys = Object.keys(stored);
  ok(keys.length === 1, "the key is kept");
  const first = env.posts[1].body;
  ok(stored[keys[0]].envelope.idempotency_key === first.idempotency_key, "the whole envelope is kept with it");
  env.verbAnswers = [[200, { ok: true, dry_run: true, tier: "T2", rendered_tier: "C1", gesture: "direct-reported" }]];
  const r2 = await env.opts().commit(req);
  ok(r2.ok === true, "the retry lands");
  ok(env.posts[2].body.dry_run === true, "the retry asks the tier again");
  ok(JSON.stringify(env.posts[3].body) === JSON.stringify(first), "the retry sends exactly the same envelope and key");
  ok(Object.keys(JSON.parse(env.ss.levain_cockpit_keys)).length === 0, "an answer clears the key");
};

cases.a_replay_survives_a_reload = async () => {
  // a first page's lost fire leaves its envelope in sessionStorage; a fresh page replays it after its own dry run
  const env1 = makeEnv();
  env1.verbAnswers = [[200, { ok: true, dry_run: true, tier: "T2", rendered_tier: "C1", gesture: "direct-reported" }], "net"];
  await env1.start();
  const req = { kind: "spore_update", spore_id: "spore-1", text: "x" };
  await env1.opts().commit(req);
  const lost = env1.posts[1].body;
  const env2 = makeEnv({ session: { levain_cockpit_keys: env1.ss.levain_cockpit_keys } });
  env2.verbAnswers = [[200, { ok: true, dry_run: true, tier: "T2", rendered_tier: "C1", gesture: "direct-reported" }]];
  await env2.start();
  await env2.opts().commit(req);
  ok(JSON.stringify(env2.posts[1].body) === JSON.stringify(lost), "the reloaded page replays the stored envelope");
};

cases.refused_and_laptop_only_never_post = async () => {
  for (const g of ["refused", "laptop-only"]) {
    const env = makeEnv({ actions: [ROW("spore_touch", "C1", g)] });
    await env.start();
    const r = await env.opts().commit({ kind: "spore_touch", spore_id: "spore-1" });
    ok(r.ok === false && /refused|laptop-only/.test(r.message) && env.posts.length === 0, g + " is not posted");
  }
};

cases.a_dry_run_that_turns_refused_does_not_fire = async () => {
  const env = makeEnv();
  env.verbAnswers = [[200, { ok: true, dry_run: true, tier: "T3", rendered_tier: "C1", gesture: "laptop-only" }]];
  await env.start();
  const r = await env.opts().commit({ kind: "spore_update", spore_id: "spore-1", text: "x" });
  ok(r.ok === false && !r.preview && env.posts.length === 1, "the T3 laptop-only answer stops the click");
};

cases.a_missing_action_is_not_offered = async () => {
  const env = await makeEnv({ actions: [ROW("spore_touch", "C1", "direct")] }).start();
  const r = await env.opts().commit({ kind: "spore_update", spore_id: "spore-1", text: "x" });
  ok(r.ok === false && r.error === "not_offered" && env.posts.length === 0, "a verb the kernel did not offer is not posted");
};

cases.confirm_is_sent_only_when_the_head_says_confirm_and_the_operator_confirmed = async () => {
  const env = await makeEnv().start();
  await env.opts().commit({ kind: "spore_descend", spore_id: "spore-1", spore_kind: "composted", confirm: true, expect_disposition: "loop" });
  const b = env.posts[0].body;
  ok(b.confirm === true && b.params.spore_kind === "composted" && !("expect_disposition" in b.params), "confirm sent, legacy CAS key not a param");
  const env2 = await makeEnv().start();
  await env2.opts().commit({ kind: "spore_touch", spore_id: "spore-1", confirm: true });
  ok(!("confirm" in env2.posts[0].body), "a verb that does not confirm gets no confirm");
};

cases.a_fire_shows_its_real_state = async () => {
  const env = makeEnv({ verbAnswers: [[200, { ok: true, state: "WAITING", label: null }]] });
  await env.start();
  const r = await env.opts().commit({ kind: "spore_touch", spore_id: "spore-1" });
  ok(r.ok === true && r.data.state === "WAITING", "the answer's state reaches the caller");
};

cases.value_write_names_the_rendered_panel_version = async () => {
  const env = await makeEnv().start();
  await env.opts().commit({ kind: "operator_state", text: "new", panel_version: env.view().state.version });
  const b = env.posts[0].body;
  ok(b.verb === "operator_state" && b.panel_id === "state" && b.panel_version === "sv1" && b.params.text === "new", "state write binds to the rendered version");
};

cases.write_token_requirement_comes_from_the_transport = async () => {
  const env = makeEnv({ transport: { writable: true, write_token_required: true } });
  await env.start();
  ok(env.view().write_token_required === true, "the transport flag reaches the view");
  env.stored = true;
  const r = await env.opts().commit({ kind: "spore_touch", spore_id: "spore-1" });
  ok(r.ok === true && env.posts[0].headers["X-Levain-Write-Token"] === "tok", "the stored credential rides the write");
  const off = await makeEnv({ transport: { writable: true, write_token_required: false } }).start();
  await off.opts().commit({ kind: "spore_touch", spore_id: "spore-1" });
  ok(!("X-Levain-Write-Token" in off.posts[0].headers), "no credential when the transport asks for none");
};

cases.pending_cancel_posts_its_own_route_with_the_gesture_intent_and_a_fresh_key = async () => {
  const env = await makeEnv().start();
  const line = env.view().extra_panels.decisions.lines[0];
  ok(line.cancel && line.cancel.pending_id === "cp1", "a pend: row carries a cancel");
  ok(/Waiting on a signature/.test(line.meta), "a pend: row names its state");
  const r = await env.opts().cancelPending(line.cancel);
  ok(r.ok === true, "cancel landed");
  ok(env.posts.length === 1 && env.posts[0].url === "/cockpit/pending/cp1/cancel", "its own route, no dry run");
  ok(env.posts[0].body.intent === "reported" && typeof env.posts[0].body.idempotency_key === "string", "intent and a fresh key");
};

cases.send_inbox_501_shows_the_server_text = async () => {
  const env = makeEnv();
  env.verbAnswers = [[200, { ok: true, dry_run: true, tier: "T2", rendered_tier: "T2", gesture: "direct-reported" }],
                     [501, { error: "not_built", message: "send_inbox has no broker effect store yet" }]];
  await env.start();
  const ext = env.view().extra_panels.inbox;
  ok(ext.action && ext.action.verb === "send_inbox", "the panel's own verb becomes the compose box");
  ok(env.view().action_verbs.send_inbox.confirm_required === true, "the verb head's confirm rides along");
  ok(ext.action.fields.map((f) => f.name + ":" + f.kind).join() === "message:textarea,for:csv", "fields from the head's names");
  const params = { message: "hi" };
  const p2 = await env.opts().commitAction("send_inbox", params, true, "ignored-key");
  ok(env.posts[0].body.dry_run === true, "the compose asks the tier first");
  ok(p2.ok === false && /no broker effect store/.test(p2.message), "the page shows the server's text, not a swallow");
  ok(env.posts[1].body.idempotency_key !== "ignored-key", "the page mints its own key");
};

// the Tray offers the create; the Keep is empty and offers nothing itself
function withTrayAndKeep(env) {
  const head = (id) => ({ id, kind: "triage-list", title: id, status: "ok", as_of: "2026-10-11T12:00:00+00:00" });
  for (const id of ["tray", "keep"]) {
    env.fx.manifest.panels[id] = head(id);
    env.fx.manifest.regions.zones[0].panels.push(id);
    env.fx.panels[id] = Object.assign(head(id), { rows: [], actions: [] });
  }
  env.fx.panels.tray.actions = [{ verb: "spore_seed", tier: "C1", gesture: "direct", escalates: [], target: { panel_id: "tray", panel_version: "tv" } }];
  env.fx.manifest.verbs.spore_seed = { label: "capture", tier: "C1", confirm: false, idempotent: false, fields: ["text", "type", "disposition"], effect: "adds a seed", gesture: "direct" };
}

cases.a_note_capture_posts_the_trays_create_with_the_disposition = async () => {
  const env = makeEnv();
  withTrayAndKeep(env);
  await env.start();
  const r = await env.opts().commit({ kind: "spore_seed", text: "keep this", disposition: "note" });
  ok(r.ok === true, "the note capture landed");
  const b = env.posts[0].body;
  ok(b.verb === "spore_seed" && b.panel_id === "tray" && b.params.disposition === "note" && b.params.text === "keep this", "the tray's create, disposition in params");
  const keep = env.view().layout.find((e) => e.id === "keep");
  ok(keep && keep.edit_class === "B", "an empty Keep shows its capture box while the Tray offers the create");
};

cases.without_a_tray_create_the_keep_has_no_capture_box = async () => {
  const env = makeEnv();
  withTrayAndKeep(env);
  env.fx.panels.tray.actions = [];
  await env.start();
  const keep = env.view().layout.find((e) => e.id === "keep");
  ok(keep && keep.edit_class !== "B", "no create offered, no capture box");
};

cases.a_5xx_cancel_answer_keeps_its_envelope_and_a_4xx_clears_it = async () => {
  const env = makeEnv();
  env.verbAnswers = [[503, { error: "unavailable", message: "try later" }], [409, { error: "gone", message: "no" }]];
  await env.start();
  const c = env.view().extra_panels.decisions.lines[0].cancel;
  await env.opts().cancelPending(c);
  ok(Object.keys(JSON.parse(env.ss.levain_cockpit_keys)).length === 1, "a 5xx is indeterminate: the envelope stays");
  await env.opts().cancelPending(c);
  ok(env.posts[1].body.idempotency_key === env.posts[0].body.idempotency_key, "the retry replays the same key");
  ok(Object.keys(JSON.parse(env.ss.levain_cockpit_keys)).length === 0, "a 4xx answer clears it");
};

cases.a_declined_token_prompt_stays_quiet_on_refresh_and_a_passive_load_never_asks = async () => {
  let asked = 0;
  const env = makeEnv({ local: {}, transport: { writable: true, write_token_required: true }, prompt: () => { asked++; return ""; } });
  env.fx.manifest.credential = { class: "none" };
  await env.start();
  ok(asked === 1, "the first load asks once");
  for (const f of env.listeners.visibilitychange || []) f();
  await env.settle();
  ok(asked === 1, "a passive refresh after a decline does not ask again");
  await env.opts().commit({ kind: "spore_touch", spore_id: "spore-1" });
  ok(asked === 2, "a write gesture asks for the token");
};

cases.the_keep_capture_box_needs_a_postable_tray_create = async () => {
  const env = makeEnv();
  withTrayAndKeep(env);
  env.fx.panels.tray.actions[0].gesture = "laptop-only";
  await env.start();
  const keep = env.view().layout.find((e) => e.id === "keep");
  ok(keep && keep.edit_class !== "B", "a laptop-only create gives the Keep no capture box");
};

cases.an_undefined_param_is_dropped_and_never_throws = async () => {
  const env = makeEnv();
  env.verbAnswers = [[200, { ok: true, dry_run: true, tier: "T2", rendered_tier: "C1", gesture: "direct-reported" }]];
  await env.start();
  const r = await env.opts().commit({ kind: "spore_update", spore_id: "spore-1", text: "x", domain: undefined });
  ok(r.ok === true, "the write lands");
  ok(env.posts.every((p) => !("domain" in p.body.params)), "an undefined value is not sent");
};

cases.a_cancel_after_a_lost_answer_replays_its_envelope_and_an_answer_clears_it = async () => {
  const env = makeEnv();
  env.verbAnswers = ["net"];
  await env.start();
  const c = env.view().extra_panels.decisions.lines[0].cancel;
  const r1 = await env.opts().cancelPending(c);
  ok(r1.ok === false && r1.error === "network", "a lost answer is reported");
  ok(!/same key/.test(r1.message), "the message promises nothing a path does not do");
  ok(Object.keys(JSON.parse(env.ss.levain_cockpit_keys)).length === 1, "the envelope is kept");
  const r2 = await env.opts().cancelPending(c);
  ok(r2.ok === true, "the second click lands");
  ok(JSON.stringify(env.posts[1].body) === JSON.stringify(env.posts[0].body), "the same envelope and key");
  ok(Object.keys(JSON.parse(env.ss.levain_cockpit_keys)).length === 0, "an answer clears it");
  await env.opts().cancelPending(c);
  ok(env.posts[2].body.idempotency_key !== env.posts[0].body.idempotency_key, "a later cancel mints a new key");
};

cases.a_token_required_manifest_read_without_a_token_prompts_then_rereads = async () => {
  let asked = 0;
  const env = makeEnv({ local: {}, transport: { writable: true, write_token_required: true },
    prompt: (e) => { asked++; e.fx.manifest.credential = { class: "token" }; return "newtok"; } });
  env.fx.manifest.credential = { class: "none" };
  await env.start();
  ok(asked === 1, "one prompt");
  ok(env.reads.filter((u) => u.indexOf("/cockpit/manifest.json") >= 0).length === 2, "the manifest is read again");
  ok(env.view().writable === true, "drawn writable");
  const r = await env.opts().commit({ kind: "spore_touch", spore_id: "spore-1" });
  ok(r.ok === true && env.posts[0].headers["X-Levain-Write-Token"] === "newtok", "the entered token rides the write");
};

cases.a_hidden_character_row_offers_no_inline_text_edit = async () => {
  const env = makeEnv();
  env.fx.panels.loops.rows[0].body = "one​two";
  env.fx.panels.loops.rows.push(Object.assign({}, env.fx.panels.loops.rows[0], { id: "spore:spore-2", body: "plain" }));
  await env.start();
  const [a, b] = env.view().open_spores;
  ok(/hidden characters/.test(a.text_locked || ""), "the altered row is locked with a reason");
  ok(a.text.indexOf("<U+200B>") >= 0, "and still shows the marker");
  ok(b.text_locked === undefined, "a plain row is not");
};

cases.offered_verbs_lists_only_what_a_postable_action_offers = async () => {
  const env = await makeEnv({ actions: [ROW("spore_touch", "C1", "direct"), ROW("spore_update", "T3", "laptop-only")] }).start();
  const o = env.view().offered_verbs;
  ok(o.spore_touch === true && o.operator_state === true && o.entity_name === undefined, "touch and the state line, not rename");
  ok(o.spore_update === undefined, "a laptop-only action is not postable");
};


// A tiny row in a fake document: the answer must reach the row before the page's re-read resolves.
function fakeRow(key) {
  const mk = () => ({ disabled: false });
  const row = { dataset: { rowKey: key }, btns: [mk(), mk()], msg: null };
  const host = { appendChild(m) { row.msg = m; },
    querySelector: (q) => (q === ".edit-msg" ? row.msg : null) };
  row.querySelector = (q) => (q === ".verb-actions" ? host : null);
  row.querySelectorAll = (q) => (/button/.test(q) ? row.btns : []);
  return row;
}
cases.the_answer_reaches_the_row_before_the_reread_resolves = async () => {
  const env = makeEnv();
  await env.start();
  const row = fakeRow("spore:spore-1");
  // the page's doc is the one handed to the boot function: patch its row query
  env.rows = [row];
  let release;
  const gate = new Promise((r) => { release = r; });
  env.holdReads = gate;
  const p = env.opts().commit({ kind: "spore_touch", spore_id: "spore-1" });
  await env.settle();
  ok(row.msg && /COMMITTED/.test(row.msg.textContent) && /claims cockpit token/.test(row.msg.textContent), "committed text on the row before the re-read: " + (row.msg && row.msg.textContent));
  // not locked: a re-read that defers (a dirty form) or fails would leave a locked row stuck; the kernel
  // refuses an edit against the old row version "stale"
  ok(row.btns.every((b) => b.disabled !== true), "the row stays usable while the re-read is pending");
  release();
  await p;
};

cases.a_refusal_shows_its_text_at_once = async () => {
  const env = makeEnv();
  env.verbAnswers = [[409, { ok: false, error: "stale", message: "the row moved" }]];
  await env.start();
  const row = fakeRow("spore:spore-1");
  env.rows = [row];
  let release;
  env.holdReads = new Promise((r) => { release = r; });
  const p = env.opts().commit({ kind: "spore_touch", spore_id: "spore-1" });
  await env.settle();
  ok(row.msg && /the row moved/.test(row.msg.textContent), "refusal text on the row before the re-read: " + (row.msg && row.msg.textContent));
  release();
  await p;
};

(async () => {
  for (const name of Object.keys(cases)) {
    if (only && only !== name) continue;
    const before = failed;
    try { await cases[name](); } catch (e) { failed++; console.log("FAIL " + name + " threw " + (e && e.stack || e)); }
    if (failed === before) console.log("OK " + name);
  }
  process.exit(failed ? 1 : 0);
})();
"""


def _run(case: str | None, tmp_path: Path) -> subprocess.CompletedProcess[str]:
    drv = tmp_path / "page_driver.js"
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
def test_page_write_case(case: str, tmp_path: Path) -> None:
    r = _run(case, tmp_path)
    assert r.returncode == 0 and f"OK {case}" in r.stdout, r.stdout + r.stderr


def test_every_case_is_listed() -> None:
    assert len(_cases()) >= 25


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_the_manifest_path_never_posts_a_legacy_route() -> None:
    boot = (WEB / "dashboard_boot.js").read_text(encoding="utf-8")
    manifest_part = boot[boot.index("MANIFEST WRITES (R1)"):boot.index("async function loadManifestView")]
    assert '"/edit"' not in manifest_part and '"/action"' not in manifest_part
    assert json.dumps("/cockpit/verb") in manifest_part

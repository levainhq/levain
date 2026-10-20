// levain topology_view.js — the live work view (F12b): the `topology` panel value drawn in the Build tab's Sessions card.
//
// It is a renderer and nothing else. cockpit_view.js parses the panel value (the one shape check) and hands it here with a
// repaint hook and a card opener; this file draws two lenses over the same value and owns the `list | topology` switch (the
// slot K7 left in the Sessions card head) and the choices that persist. Nothing here assumes Levain's own ids or words: the
// hub is whatever the provider names (Levain: the install; flow: the desk), groups are repos or seats, nodes are sessions or
// lanes. Everything is built with textContent: a wire string never reaches an HTML sink.
//
//   Work lens (the default): tasks, grouped by state, loudest first (waiting > failed > stuck > running > done). A task's state
//   is the provider's, its loudest open record. Agents are small chips on the task they work. A session with no task object
//   sits under "unattached work", never a guessed task. Waiting (a pending record the operator can answer) and stuck (silence,
//   no record) never merge.
//   Agents lens: the hub, then one column per group, one row per node.
//
// Unknown is drawn as unknown: a null cost, token count, rate, conflict list or model reads "not tracked" / "model unknown",
// never 0. No particles are drawn: the feed has no per-edge events, only a node's rate, and a column layout has no edge to
// carry them.
(function (root) {
  "use strict";

  const MODE_KEY = "levain_cockpit_topology_mode";
  const LENS_KEY = "levain_cockpit_topology_lens";
  const WIDE_PX = 1440;                    // the width from which the topology is the default view on Build
  // loudest first: the order the Work lens lists its groups in
  const TASK_ORDER = ["waiting", "failed", "stuck", "running", "done"];
  const TASK_WORDS = {
    waiting: ["Waiting on you", "a pending record you can answer"],
    failed: ["Failed", "the work ended without its outcome"],
    stuck: ["Stuck", "silence, and no record behind it"],
    running: ["Running", "a worker or a live job is on it"],
    done: ["Done", "finished"],
  };
  const NODE_WORDS = { waiting: "waiting on you", errored: "errored", stalled: "stalled", running: "running", idle: "idle" };
  const KIND_WORDS = { dispatch: "dispatch", spore: "open loop", consent: "consent", question: "question", unattached: "unattached" };
  const CARD_WORDS = { consent: "a consent request", question: "a question", dispatch: "a dispatch contract" };

  const GROUP_CAP = 4;                     // tasks a Work group shows before "show N more"
  const shownAll = new Set();              // the groups the operator opened in full
  const isObj = (x) => x !== null && typeof x === "object" && !Array.isArray(x);
  const isNum = (x) => typeof x === "number" && Number.isFinite(x);
  const arr = (x) => (Array.isArray(x) ? x : []);

  function el(tag, cls, text) {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined && text !== null) n.textContent = String(text);
    return n;
  }
  function stored(key) { try { return root.localStorage.getItem(key); } catch (_) { return null; } }
  function keep(key, val) { try { root.localStorage.setItem(key, val); } catch (_) { /* no storage: the choice lasts until the page does */ } }

  // ---- what persists: the view (list | topology) and the lens (work | agents) ----
  function mode(width) {
    const m = stored(MODE_KEY);
    if (m === "list" || m === "topology") return m;
    return isNum(width) && width >= WIDE_PX ? "topology" : "list";
  }
  function lens() { return stored(LENS_KEY) === "agents" ? "agents" : "work"; }

  function seg(name, items, current, onPick) {
    const g = el("span", "tv-seg");
    g.setAttribute("role", "group");
    g.setAttribute("aria-label", name);
    for (const [id, text] of items) {
      const b = el("button", "tv-seg-b", text);
      b.type = "button";
      b.dataset.tv = id;
      b.setAttribute("aria-pressed", id === current ? "true" : "false");
      b.addEventListener("click", () => { if (id !== current) onPick(id); });
      g.appendChild(b);
    }
    return g;
  }
  // the `list | topology` switch, for the slot in the Sessions card head
  function switchNode(current, onChange) {
    return seg("Sessions view", [["list", "list"], ["topology", "topology"]], current, (id) => { keep(MODE_KEY, id); onChange(id); });
  }

  // does the value hold anything to draw? Sessions, or a task that is more than an open loop: loops alone are the Open loops
  // card's, and a wall of "stuck" with no agent anywhere is not a work view. An install with neither keeps the card's empty line
  // (with the quiet line beside it when a source could not be read).
  function hasWork(v) { return isObj(v) && (arr(v.nodes).length > 0 || arr(v.tasks).some((t) => t.kind !== "spore")); }

  // what the quiet line says: what the provider could not read, never hidden
  function quietLine(v) {
    if (!isObj(v)) return null;
    const errs = arr(v.errors).filter((x) => typeof x === "string" && x);
    const names = arr(v.unreadable).filter((x) => typeof x === "string" && x);
    const parts = errs.length ? errs : (names.length ? ["not read: " + names.join(", ")] : []);
    return parts.length ? el("p", "tv-quiet", parts.join(" · ")) : null;
  }

  // the value's content, for the card's repaint signature (the clock is not in it)
  function signature(v, l) { return JSON.stringify([l, v, Array.from(shownAll)]); }

  // ---- small formatters ----
  function fmtTokens(n) {
    if (!isNum(n)) return null;
    if (n >= 1e6) return (n / 1e6).toFixed(1) + "M";
    if (n >= 1e3) return Math.round(n / 1e3) + "K";
    return String(n);
  }
  function fmtUsd(n) { return isNum(n) ? "$" + n.toFixed(2) : null; }
  function clock(iso) {
    const t = Date.parse(iso);
    if (isNaN(t)) return "";
    return new Date(t).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
  }
  function cardWords(id) {
    const head = typeof id === "string" ? id.split(":", 1)[0] : "";
    return CARD_WORDS[head] || "a decision";
  }
  function costText(c) {
    if (!isObj(c)) return "cost not tracked";
    const usd = fmtUsd(c.usd), tok = fmtTokens(c.tokens);
    if (usd === null && tok === null) return "cost not tracked";
    const src = typeof c.source === "string" && c.source ? " " + c.source : "";
    return [usd, tok === null ? null : tok + " tok"].filter(Boolean).join(" · ") + src;
  }

  function openable(node, card, opts) {
    if (typeof card !== "string" || !card || !opts || typeof opts.openCard !== "function") return false;
    node.classList.add("tv-open");
    node.setAttribute("role", "button");
    node.tabIndex = 0;
    node.dataset.card = card;
    node.title = "open the card this waits on";
    const go = () => opts.openCard(card);
    node.addEventListener("click", (ev) => { if (!(ev.target.closest && ev.target.closest("a,button:not(.tv-open)"))) go(); });
    node.addEventListener("keydown", (ev) => { if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); go(); } });
    return true;
  }

  function dot(state) { const d = el("i", "tv-dot " + state); d.setAttribute("aria-hidden", "true"); return d; }
  function tag(state, text) { return el("span", "tv-tag " + state, text); }

  // ---- the header strip ----
  function strip(v, which) {
    const nodes = arr(v.nodes), tasks = arr(v.tasks);
    const running = nodes.filter((n) => n.state === "running").length;
    const waiting = which === "agents" ? nodes.filter((n) => n.state === "waiting").length : tasks.filter((t) => t.state === "waiting").length;
    const row = el("div", "tv-strip");
    const stat = (k, n, word, cls) => {
      const s = el("span", "tv-stat" + (cls ? " " + cls : ""));
      s.dataset.k = k;
      s.append(el("b", null, n), " " + word);
      return s;
    };
    row.append(stat("groups", arr(v.groups).length, "groups"), stat("nodes", nodes.length, "nodes"), stat("running", running, "running"),
               stat("waiting", waiting, "waiting on you", waiting ? "hot" : ""));
    const conflicts = v.conflicts;
    let c;
    if (conflicts === null || conflicts === undefined) { c = el("span", "tv-stat dim", "conflicts not tracked"); }
    else if (!conflicts.length) { c = el("span", "tv-stat"); c.append(el("b", null, "0"), " conflicts"); }
    else { c = el("span", "tv-stat warn"); c.append(el("b", null, conflicts.length), conflicts.length === 1 ? " file conflict" : " file conflicts"); }
    c.dataset.k = "conflicts";
    row.appendChild(c);
    const tk = v.counts && isNum(v.counts.tokens_today) ? fmtTokens(v.counts.tokens_today) : null;
    const t = el("span", "tv-stat tv-right" + (tk === null ? " dim" : ""));
    t.dataset.k = "tokens";
    if (tk === null) t.textContent = "tokens today not tracked"; else t.append(el("b", null, tk), " tokens today");
    row.appendChild(t);
    return row;
  }
  function conflictList(v) {
    const list = arr(v.conflicts);
    if (!list.length) return null;
    const label = {};
    for (const n of arr(v.nodes)) label[n.id] = n.label;
    const d = el("details", "tv-conf");
    d.appendChild(el("summary", null, list.length === 1 ? "1 file two live sessions are both writing" : list.length + " files two live sessions are both writing"));
    for (const c of list) d.appendChild(el("div", "tv-conf-row", c.path + " — " + arr(c.nodes).map((id) => label[id] || id).join(" and ")));
    return d;
  }

  // ---- the Work lens ----
  function workerChip(n) {
    const c = el("span", "tv-chip");
    c.dataset.node = n.id;
    c.title = [n.agent, n.model || "model unknown"].filter(Boolean).join(" · ");
    c.append(dot(n.state), n.label);
    return c;
  }
  function taskCard(t, byId, opts) {
    const c = el("article", "tv-task " + t.state + (t.kind === "unattached" ? " tv-loose" : ""));
    c.dataset.task = t.id;
    c.dataset.kind = t.kind;
    c.dataset.state = t.state;
    const main = el("div", "tv-task-main");
    const head = el("div", "tv-task-h");
    // unattached work is not a task: its group heading says so, and its card is the chips (and a stuck tag, when silence is the news)
    if (t.kind !== "unattached") head.append(el("span", "tv-kind", KIND_WORDS[t.kind] || t.kind), el("b", "tv-task-t", t.label));
    if (t.kind !== "unattached" || t.state !== "running") head.appendChild(tag(t.state, t.state === "waiting" ? "waiting on you" : t.state));
    if (head.children.length) main.appendChild(head);
    const workers = arr(t.workers).map((id) => byId[id]).filter(Boolean);
    if (workers.length) {
      const wr = el("div", "tv-workers");
      for (const n of workers) wr.appendChild(workerChip(n));
      main.appendChild(wr);
    }
    const foot = el("div", "tv-task-f");
    const waits = arr(t.waits_on);
    if (!workers.length && t.kind !== "unattached") foot.appendChild(el("span", "tv-none", "no agent attached"));
    if (waits.length) {
      const w = el("span", "tv-waits", "waits on " + waits.map(cardWords).join(", "));
      w.title = waits.join(", ");
      foot.appendChild(w);
    } else if (t.state === "waiting") foot.appendChild(el("span", "tv-waits", "waits on a decision"));
    if (t.kind !== "unattached") foot.appendChild(el("span", "tv-cost" + (costText(t.cost) === "cost not tracked" ? " dim" : ""), costText(t.cost)));
    main.appendChild(foot);
    c.appendChild(main);
    const outs = arr(t.outcomes);
    if (outs.length) {
      const o = el("ul", "tv-outs");
      for (const x of outs) {
        const li = el("li", "tv-out " + (x.ok ? "ok" : "bad"));
        li.append(el("span", "tv-out-m", x.ok ? "✓" : "✕"), el("span", "tv-out-l", x.label), el("span", "tv-out-at", clock(x.at)));
        o.appendChild(li);
      }
      c.appendChild(o);
    }
    if (t.state === "waiting") openable(c, t.card, opts);
    return c;
  }
  function workLens(v, opts) {
    const byId = {};
    for (const n of arr(v.nodes)) byId[n.id] = n;
    const tasks = arr(v.tasks);
    const wrap = el("div", "tv-work");
    for (const st of TASK_ORDER) {
      const here = tasks.filter((t) => t.state === st && t.kind !== "unattached");
      if (!here.length) continue;
      const g = el("section", "tv-group " + st);
      g.dataset.group = st;
      const h = el("div", "tv-group-h");
      h.append(dot(st), el("b", null, TASK_WORDS[st][0]), el("span", "tv-n", String(here.length)), el("span", "tv-gloss", TASK_WORDS[st][1]));
      g.appendChild(h);
      // a long group (a Tray's open loops) shows its first few and one button for the rest; the choice outlives a redraw
      // waiting and failed are the loud ones and are never folded away
      const folds = st !== "waiting" && st !== "failed" && here.length > GROUP_CAP;
      const open = shownAll.has(st);
      here.forEach((t, i) => { const c = taskCard(t, byId, opts); if (folds && i >= GROUP_CAP && !open) c.hidden = true; g.appendChild(c); });
      if (folds) {
        const more = el("button", "tv-more", open ? "show fewer" : "show " + (here.length - GROUP_CAP) + " more");
        more.type = "button";
        more.addEventListener("click", () => { if (shownAll.has(st)) shownAll.delete(st); else shownAll.add(st); if (opts && opts.repaint) opts.repaint(); });
        g.appendChild(more);
      }
      wrap.appendChild(g);
    }
    const loose = tasks.filter((t) => t.kind === "unattached");
    if (loose.length) {
      const g = el("section", "tv-group loose");
      g.dataset.group = "unattached";
      const h = el("div", "tv-group-h");
      h.append(el("b", null, "Unattached work"), el("span", "tv-gloss", "sessions with no task record behind them"));
      g.appendChild(h);
      for (const t of loose) g.appendChild(taskCard(t, byId, opts));
      wrap.appendChild(g);
    }
    if (!wrap.children.length) wrap.appendChild(el("p", "tv-none", "No task records and no active work."));
    return wrap;
  }

  // ---- the Agents lens ----
  function nodeRow(n, opts, depth) {
    const r = el("div", "tv-node " + n.state + (depth ? " sub" : ""));
    r.dataset.node = n.id;
    r.append(dot(n.state));
    const body = el("div", "tv-node-b");
    const top = el("div", "tv-node-t");
    top.appendChild(el("b", null, n.label));
    if (n.state !== "running") top.appendChild(tag(n.state, NODE_WORDS[n.state] || n.state));
    body.appendChild(top);
    body.appendChild(el("div", "tv-node-m", [n.agent, n.model || "model unknown"].filter(Boolean).join(" · ")));
    const tok = fmtTokens(n.tokens);
    const bits = [tok === null ? "tokens not tracked" : tok + " tok", isNum(n.rate) ? n.rate.toFixed(1) + " events/min" : "rate not tracked"];
    if (typeof n.tool === "string" && n.tool) bits.push(n.tool);
    const since = clock(n.started);
    if (since) bits.push("since " + since);
    body.appendChild(el("div", "tv-node-f", bits.join(" · ")));
    r.appendChild(body);
    if (n.state === "waiting") openable(r, n.card, opts);
    return r;
  }
  function agentsLens(v, opts) {
    const wrap = el("div", "tv-agents");
    const hub = el("div", "tv-hub");
    const hubLabel = isObj(v.hub) && typeof v.hub.label === "string" ? v.hub.label : "";
    hub.append(el("b", null, hubLabel), el("span", "tv-gloss", arr(v.groups).length + " groups · " + arr(v.nodes).length + " nodes"));
    wrap.appendChild(hub);
    const cols = el("div", "tv-cols");
    const nodes = arr(v.nodes);
    for (const g of arr(v.groups)) {
      const mine = nodes.filter((n) => n.group === g.id);
      const col = el("section", "tv-col");
      col.dataset.group = g.id;
      const h = el("div", "tv-col-h");
      h.append(el("b", null, g.label), el("span", "tv-n", String(mine.length)));
      col.appendChild(h);
      const ids = new Set(mine.map((n) => n.id));
      // a node whose parent is in this group sits under it, indented; the rest are the column's own rows
      const roots = mine.filter((n) => !n.parent || !ids.has(n.parent));
      const put = (n, depth) => {
        col.appendChild(nodeRow(n, opts, depth));
        for (const k of mine.filter((m) => m.parent === n.id)) put(k, depth + 1);
      };
      for (const n of roots) put(n, 0);
      if (!mine.length) col.appendChild(el("p", "tv-none", "no nodes"));
      cols.appendChild(col);
    }
    wrap.appendChild(cols);
    return wrap;
  }

  // ---- the body ----
  // v: the topology value. opts: {repaint(): redraw the card, openCard(id): open the Needs-you row a waiting item links to}
  function render(v, opts) {
    const o = opts || {};
    const which = lens();
    const w = el("div", "tv");
    w.dataset.lens = which;
    const bar = el("div", "tv-bar");
    bar.append(seg("Lens", [["work", "Work"], ["agents", "Agents"]], which, (id) => { keep(LENS_KEY, id); if (o.repaint) o.repaint(); }), strip(v, which));
    w.appendChild(bar);
    const conf = conflictList(v);
    if (conf) w.appendChild(conf);
    w.appendChild(which === "agents" ? agentsLens(v, o) : workLens(v, o));
    const q = quietLine(v);
    if (q) w.appendChild(q);
    return w;
  }

  root.LevainTopologyView = { WIDE_PX, mode, lens, switchNode, hasWork, quietLine, signature, render };
  if (typeof module !== "undefined" && module.exports) module.exports = root.LevainTopologyView;
})(typeof globalThis !== "undefined" ? globalThis : this);

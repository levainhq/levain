// levain topology_view.js — the live work view (F12b): the `topology` panel value drawn in the Build tab's Sessions card.
//
// It is a renderer and nothing else. cockpit_view.js parses the panel value (the one shape check) and hands it here with a
// repaint hook and a card opener; this file draws two lenses over the same value and owns the `list | topology` switch (the
// slot K7 left in the Sessions card head) and the choices that persist. Nothing here assumes Levain's own ids or words: the
// hub is whatever the provider names (Levain: the install; flow: the desk), groups are repos or seats, nodes are sessions or
// lanes. Everything is built with textContent: a wire string never reaches an HTML sink.
//
//   Work lens (the default): three columns as drawn. LEFT, the waiting records (one per pending consent / question, plus a
//   dispatch contract waiting on a signature). CENTRE, the tasks as compact cards, loudest state first; waiting-orange is a
//   border and a tag, stuck is dashed amber, done is quiet. RIGHT, each task's outcomes on the task's own row. A line joins a
//   record to the task that waits on it and a task to its outcomes (the lines are ornament: the columns are the grammar).
//   Agents are small chips on the task they work. A session with no task object sits in an "unattached work" block, never a
//   guessed task. Waiting (a pending record the operator can answer) and stuck (silence, no record) never merge. In a
//   narrow card the columns stack: records, tasks, each task's outcomes beneath it.
//   Agents lens: the graph as the F12 mock draws it. One hub (the install), the groups on its two sides, each group's sessions beside it
//   with a subagent under its parent, every node with its face and a state ring, edges hub -> group -> node -> child that carry particle
//   streams whose density is the node's rate. A file conflict is the file's name on a chip between its nodes.
//
// Unknown is drawn as unknown: a null cost, token count, rate, conflict list or model reads "not tracked" / "model unknown",
// never 0. The Work lens draws no particles (it has no edge to carry them). The Agents lens draws a stream only on an edge whose node has a
// real positive rate and is not idle: a null rate, a rate of 0 and an idle node are still, and under prefers-reduced-motion nothing moves.
(function (root) {
  "use strict";

  const MODE_KEY = "levain_cockpit_topology_mode";
  const LENS_KEY = "levain_cockpit_topology_lens";
  const WIDE_PX = 1440;                    // the width from which the topology is the default view on Build
  // the order the Work lens lists its groups in (K10: what needs you, what is moving, what went quiet, the open loops, then what ended)
  const TASK_ORDER = ["waiting", "failed", "stuck", "running", "open", "done"];
  const HEAT_ORDER = ["hot", "warm", "cold", "parked"];      // an open loop's heat, hottest first; none last
  const TASK_WORDS = {
    waiting: ["Waiting on you", "a pending record you can answer"],
    failed: ["Failed", "the work ended without its outcome"],
    stuck: ["Stuck", "a live worker gone silent, and no record behind it"],
    open: ["Open", "a loop nobody is working yet"],
    running: ["Running", "a worker or a live job is on it"],
    done: ["Done", "finished"],
  };
  const NODE_WORDS = { waiting: "waiting on you", errored: "errored", stalled: "stalled", running: "running", idle: "idle" };
  const KIND_WORDS = { dispatch: "dispatch", spore: "open loop", consent: "consent", question: "question", unattached: "unattached" };
  const CARD_WORDS = { consent: "a consent request", question: "a question", dispatch: "a dispatch contract" };

  const GROUP_CAP = 4;                     // tasks of one state the Work stack shows before "show N more"
  const SVG_NS = "http://www.w3.org/2000/svg";
  const shownAll = new Set();              // the groups the operator opened in full
  let linkObserver = null;                 // the one ResizeObserver the Work lens's lines use: a new render replaces it, the card releases it
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

  // does the value hold anything to draw? Sessions or any task, an open loop included (it is a task with an age, as the flow cockpit
  // shows it). An install with neither keeps the card's empty line (with the quiet line beside it when a source could not be read).
  function hasWork(v) { return isObj(v) && (arr(v.nodes).length > 0 || arr(v.tasks).length > 0); }
  // "open N d": the whole calendar days the provider counted, in its own day (the Open loops panel's count). The page never
  // recomputes a calendar day from a timestamp: its zone and clock are not the server's. No count is unknown, drawn as "open".
  function openDays(t) { return Number.isInteger(t.age_days) && t.age_days >= 0 ? t.age_days : null; }

  // what the quiet line says: what the provider could not read, never hidden
  function quietLine(v) {
    if (!isObj(v)) return null;
    const errs = arr(v.errors).filter((x) => typeof x === "string" && x);
    const names = arr(v.unreadable).filter((x) => typeof x === "string" && x);
    const parts = errs.length ? errs : (names.length ? ["not read: " + names.join(", ")] : []);
    return parts.length ? el("p", "tv-quiet", parts.join(" · ")) : null;
  }

  // the value's content, for the card's repaint signature: exactly what is drawn, each number as the display writes it (a rate at
  // its one decimal, tokens as K / M, an open loop's age as the provider's day count). The clock (as_of, a node's last_event) and the
  // `opened` stamp are not in it, so a poll that moved nothing the operator can see repaints nothing and a visible change always does
  function signature(v, l) {
    // The Agents lens also draws an idle node's "quiet since" and "ran", and an errored node's "failed at": those stamps are drawn, so they are in
    // the signature (as the clock text the face writes); a running node's run time ticks on its own and is not.
    const stamp = (n) => (l === "agents" && isObj(n) && (n.state === "idle" || n.state === "errored") ? [clock(n.last_event), fmtDur(span(n.started, n.last_event))] : undefined);
    const lean = (n) => (isObj(n) ? Object.assign({}, n, { last_event: undefined, stamp: stamp(n), rate: isNum(n.rate) ? n.rate.toFixed(1) : n.rate, tokens: fmtTokens(n.tokens) }) : n);
    const task = (t) => (isObj(t) ? Object.assign({}, t, { opened: undefined, cost: isObj(t.cost) ? Object.assign({}, t.cost, { usd: fmtUsd(t.cost.usd), tokens: fmtTokens(t.cost.tokens) }) : t.cost }) : t);
    const sane = isObj(v) ? Object.assign({}, v, { as_of: undefined, nodes: Array.isArray(v.nodes) ? v.nodes.map(lean) : v.nodes,
                                                   tasks: Array.isArray(v.tasks) ? v.tasks.map(task) : v.tasks,
                                                   counts: isObj(v.counts) ? Object.assign({}, v.counts, { tokens_today: fmtTokens(v.counts.tokens_today) }) : v.counts }) : v;
    return JSON.stringify([l, sane, Array.from(shownAll)]);
  }
  // one OS-level observer at a time: the card calls this before it replaces its children
  function release() {
    if (linkObserver) { linkObserver.disconnect(); linkObserver = null; }
    if (streamStop) { streamStop(); streamStop = null; }
    if (ticker) { root.clearInterval(ticker); ticker = null; }
  }

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
  // what closing a live node would lose (the value's lost_if_closed), by node id; null counts are unknown, never zero
  function lostMap(v) {
    const m = new Map();
    for (const l of arr(v.lost_if_closed)) if (isObj(l) && typeof l.node === "string") m.set(l.node, l);
    return m;
  }
  function riskTitle(l) {
    if (!isObj(l)) return null;
    const c = (x) => (isNum(x) ? x : null);
    const d = c(l.dirty), u = c(l.unpushed), r = c(l.running_children), e = c(l.uncaptured_episodes);
    if (!((d || 0) > 0 || (u || 0) > 0 || (r || 0) > 0 || (e || 0) > 0)) return null;      // nothing known to be at risk: no mark
    const parts = [d === null ? "dirty not tracked" : d + " dirty", u === null ? "unpushed not tracked" : u + " unpushed", r === null ? "running not tracked" : r + " running"];
    if (e) parts.push(e + " uncaptured");
    return parts.join(" · ");
  }
  function riskMark(l) {
    const t = riskTitle(l);
    if (t === null) return null;
    const m = el("i", "tv-risk");
    m.title = t;
    m.setAttribute("role", "img");
    m.setAttribute("aria-label", "at risk: " + t);
    return m;
  }

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
    const conflicts = Array.isArray(v.conflicts) ? v.conflicts : null;       // anything but a list is "not tracked", never a zero
    let c;
    if (conflicts === null) { c = el("span", "tv-stat dim", "conflicts not tracked"); }
    else if (!conflicts.length) { c = el("span", "tv-stat"); c.append(el("b", null, "0"), " conflicts"); }
    else { const n = conflictCount(v); c = el("span", "tv-stat warn"); c.append(el("b", null, n), n === 1 ? " file conflict" : " file conflicts"); }
    c.dataset.k = "conflicts";
    row.appendChild(c);
    const tk = v.counts && isNum(v.counts.tokens_today) ? fmtTokens(v.counts.tokens_today) : null;
    const t = el("span", "tv-stat tv-right" + (tk === null ? " dim" : ""));
    t.dataset.k = "tokens";
    if (tk === null) t.textContent = "tokens today not tracked"; else t.append(el("b", null, tk), " tokens today");
    row.appendChild(t);
    return row;
  }
  // the Work lens's strip, as drawn: the tasks lead, then the loud states, then conflicts and tokens
  function stripWork(v) {
    const tasks = arr(v.tasks).filter((t) => t.kind !== "unattached");
    const n = (st) => tasks.filter((t) => t.state === st).length;
    const row = el("div", "tv-strip");
    const stat = (k, num, word, cls) => {
      const s = el("span", "tv-stat" + (cls ? " " + cls : ""));
      s.dataset.k = k;
      s.append(el("b", null, num), " " + word);
      return s;
    };
    row.append(stat("tasks", tasks.length, tasks.length === 1 ? "task" : "tasks"), stat("waiting", n("waiting"), "waiting on you", n("waiting") ? "hot" : ""));
    if (n("failed")) row.appendChild(stat("failed", n("failed"), "failed", "bad"));
    row.append(stat("stuck", n("stuck"), "stuck"));
    if (n("open")) row.appendChild(stat("open", n("open"), "open"));
    row.appendChild(stat("done", n("done"), "done"));
    const rest = strip(v, "work");
    for (const k of ["conflicts", "tokens"]) row.appendChild(rest.querySelector('[data-k="' + k + '"]'));
    return row;
  }
  // the files in conflict: the provider's own count of them, which exceeds the list when the list was cut (never fewer than the list)
  function conflictCount(v) {
    const list = arr(v.conflicts);
    return Number.isInteger(v.conflicts_total) && v.conflicts_total >= list.length ? v.conflicts_total : list.length;
  }
  function conflictList(v) {
    const list = arr(v.conflicts);
    if (!list.length) return null;
    const total = conflictCount(v);
    const label = new Map();
    for (const n of arr(v.nodes)) label.set(n.id, n.label);
    const d = el("details", "tv-conf");
    d.appendChild(el("summary", null, (total === 1 ? "1 file" : total + " files") + " live sessions are both writing" + (total > list.length ? " (showing " + list.length + ")" : "")));
    // one file, every session on it: "a and b", "a, b and c"
    const names = (c) => { const w = arr(c.nodes).map((id) => label.get(id) || id); return w.length > 2 ? w.slice(0, -1).join(", ") + " and " + w[w.length - 1] : w.join(" and "); };
    for (const c of list) d.appendChild(el("div", "tv-conf-row", c.path + " — " + names(c)));
    return d;
  }

  // ---- the Work lens ----
  function workerChip(n, lost) {
    const c = el("span", "tv-chip");
    c.dataset.node = n.id;
    c.title = [n.agent, n.model || "model unknown"].filter(Boolean).join(" · ");
    c.append(dot(n.state), n.label);
    const risk = riskMark(lost.get(n.id));
    if (risk) c.appendChild(risk);
    return c;
  }
  function taskCard(t, byId, lost, opts) {
    const c = el("article", "tv-task " + t.state + (t.kind === "unattached" ? " tv-loose" : ""));
    c.dataset.task = t.id;
    c.dataset.kind = t.kind;
    c.dataset.state = t.state;
    const head = el("div", "tv-task-h");
    // unattached work is not a task: its heading says so, and its card is the chips (and a stuck tag, when silence is the news)
    if (t.kind === "unattached") head.appendChild(el("span", "tv-loose-h", "unattached work"));
    else { const ti = el("b", "tv-task-t", t.label); ti.title = t.label; head.appendChild(ti); }
    if (t.state === "open") {
      // an open loop is quiet: its age and its heat, never a tag that looks like a state change
      const days = openDays(t);
      if (t.heat) { const h = el("i", "tv-heat " + t.heat); h.title = t.heat; h.setAttribute("role", "img"); h.setAttribute("aria-label", "heat: " + t.heat); head.appendChild(h); }
      head.appendChild(el("span", "tv-age", days === null ? "open" : "open " + days + " d"));
    } else if (t.kind !== "unattached" || t.state !== "running") head.appendChild(tag(t.state, t.state === "waiting" ? "waiting on you" : t.state));
    c.appendChild(head);
    const foot = el("div", "tv-task-f");
    if (t.kind !== "unattached") foot.appendChild(el("span", "tv-kind", KIND_WORDS[t.kind] || t.kind));
    const workers = arr(t.workers).map((id) => byId.get(id)).filter(Boolean);
    for (const n of workers) foot.appendChild(workerChip(n, lost));
    const waits = arr(t.waits_on);
    if (!workers.length && t.kind !== "unattached") foot.appendChild(el("span", "tv-none", "no agent attached"));
    if (t.kind === "unattached") foot.appendChild(el("span", "tv-none", "no task record, none guessed"));
    if (waits.length) {
      const w = el("span", "tv-waits", "waits on " + waits.map(cardWords).join(", "));
      w.title = waits.join(", ");
      foot.appendChild(w);
    } else if (t.state === "waiting") foot.appendChild(el("span", "tv-waits", "waits on a decision"));
    if (t.kind !== "unattached") foot.appendChild(el("span", "tv-cost" + (costText(t.cost) === "cost not tracked" ? " dim" : ""), costText(t.cost)));
    if (foot.children.length) c.appendChild(foot);
    if (t.state === "waiting") openable(c, t.card, opts);
    return c;
  }
  function outcomeList(t) {
    const outs = arr(t.outcomes);
    if (!outs.length) return null;
    const o = el("ul", "tv-outs");
    o.dataset.task = t.id;
    for (const x of outs) {
      const li = el("li", "tv-out " + (x.ok ? "ok" : "bad"));
      li.append(el("span", "tv-out-m", x.ok ? "✓" : "✕"), el("span", "tv-out-l", x.label), el("span", "tv-out-at", clock(x.at)));
      o.appendChild(li);
    }
    return o;
  }
  // a waiting record on the left: the title, then what it is and who is stopped on it
  function recordCard(t, byId, opts) {
    const r = el("div", "tv-rec waiting");
    r.dataset.rec = t.id;
    const ti = el("b", "tv-rec-t", t.label);
    ti.title = t.label;
    const who = arr(t.workers).map((id) => byId.get(id)).filter(Boolean).map((n) => n.label);
    const m = el("span", "tv-rec-m", [KIND_WORDS[t.kind] || cardWords(t.card)].concat(who).join(" · "));
    r.append(ti, m);
    openable(r, t.card, opts);
    return r;
  }
  // one line joining two drawn things, in the wrap's own coordinates; none when the columns are stacked
  function drawLinks(wrap, svg, links) {
    while (svg.firstChild) svg.removeChild(svg.firstChild);
    // stacked is a one-column grid: the container's own tracks say so, whichever card happens to come first
    if (getComputedStyle(wrap).gridTemplateColumns.split(" ").filter(Boolean).length < 2) return;
    const box = wrap.getBoundingClientRect();
    for (const [from, to, cls] of links) {
      if (!from.isConnected || !to.isConnected || from.offsetParent === null || to.offsetParent === null) continue;
      const a = from.getBoundingClientRect(), b = to.getBoundingClientRect();
      const x1 = a.right - box.left, y1 = a.top + a.height / 2 - box.top, x2 = b.left - box.left, y2 = b.top + b.height / 2 - box.top;
      const mid = (x1 + x2) / 2;
      const p = document.createElementNS(SVG_NS, "path");
      p.setAttribute("d", "M" + x1 + " " + y1 + " C" + mid + " " + y1 + " " + mid + " " + y2 + " " + x2 + " " + y2);
      p.setAttribute("class", "tv-link " + cls);
      svg.appendChild(p);
    }
  }
  function workLens(v, opts) {
    const byId = new Map();
    for (const n of arr(v.nodes)) byId.set(n.id, n);
    const lost = lostMap(v);
    const tasks = arr(v.tasks);
    release();
    const wrap = el("div", "tv-work");
    const heads = ["Waiting on you", "Tasks · who is working on them", "Outcomes"].map((txt, i) => { const h = el("div", "tv-colh tv-colh" + i, txt); wrap.appendChild(h); return h; });
    // the left column: a record for every pending consent / question, and for a dispatch contract waiting on its signature
    const left = el("div", "tv-wl");
    // waiting is the task's state, whether or not it has a card to open: only the click needs one
    const recs = tasks.filter((t) => t.state === "waiting");
    const recBy = new Map();
    for (const t of recs) {
      const r = recordCard(t, byId, opts);
      left.appendChild(r);
      recBy.set(t.id, r);
      if (typeof t.card === "string" && t.card) recBy.set(t.card, r);
    }
    if (!recs.length) left.appendChild(el("p", "tv-none", "nothing is waiting on you"));
    wrap.appendChild(left);
    // the centre: every other task, loudest state first; the right: its outcomes, on the same row
    const centre = [];
    const heatRank = (t) => { const i = HEAT_ORDER.indexOf(t.heat); return i < 0 ? HEAT_ORDER.length : i; };
    for (const st of TASK_ORDER) {
      const group = tasks.filter((t) => t.state === st && t.kind !== "unattached" && t.kind !== "consent" && t.kind !== "question");
      if (st === "open") group.sort((a, b) => heatRank(a) - heatRank(b));       // hot first; the store's order within a heat (the sort is stable)
      centre.push(...group);
    }
    for (const t of tasks) if (t.kind === "unattached") centre.push(t);
    const links = [];
    let row = 2;
    const seen = new Map(), total = new Map();
    for (const t of centre) if (t.kind !== "unattached") total.set(t.state, (total.get(t.state) || 0) + 1);
    const place = (node, col) => { node.style.setProperty("--tv-r", String(row)); node.style.setProperty("--tv-c", String(col)); };
    for (const t of centre) {
      const folds = t.kind !== "unattached" && t.state !== "waiting" && t.state !== "failed";
      const idx = folds ? (seen.set(t.state, (seen.get(t.state) || 0) + 1), seen.get(t.state)) : 0;
      const hide = folds && idx > GROUP_CAP && !shownAll.has(t.state);
      const c = taskCard(t, byId, lost, opts);
      const o = outcomeList(t);
      c.hidden = hide;
      if (o) o.hidden = hide;
      if (!hide) { place(c, 2); if (o) place(o, 3); }
      wrap.appendChild(c);
      if (o) wrap.appendChild(o);
      for (const id of new Set(arr(t.waits_on).concat(t.state === "waiting" && t.card ? [t.card] : []))) if (recBy.has(id) && !hide) links.push([recBy.get(id), c, "waiting"]);
      if (o && !hide) for (const li of o.children) links.push([c, li, li.classList.contains("bad") ? "bad" : "ok"]);
      if (!hide) row += 1;
      // a long run of one state (a Tray's open loops) shows its first few and one button for the rest; the choice outlives a redraw
      if (folds && idx === total.get(t.state) && idx > GROUP_CAP) {
        const st = t.state, open = shownAll.has(st);
        const more = el("button", "tv-more", open ? "show fewer" : "show " + (idx - GROUP_CAP) + " more");
        more.type = "button";
        more.dataset.more = st;
        more.addEventListener("click", () => { if (shownAll.has(st)) shownAll.delete(st); else shownAll.add(st); if (opts && opts.repaint) opts.repaint(); });
        place(more, 2);
        wrap.appendChild(more);
        row += 1;
      }
    }
    left.style.gridRow = "2 / " + Math.max(row, 3);
    if (!centre.length) wrap.appendChild(el("p", "tv-none tv-no-tasks", "No task records and no active work."));
    heads[2].hidden = !centre.some((t) => arr(t.outcomes).length);
    wrap.classList.toggle("tv-noouts", heads[2].hidden);        // no outcomes anywhere: the third column gives its width back
    // the lines: drawn after layout, again when the card is resized; a wrap with no lines to draw has no overlay
    if (links.length) {
      const svg = document.createElementNS(SVG_NS, "svg");
      svg.setAttribute("class", "tv-links");
      svg.setAttribute("aria-hidden", "true");
      wrap.insertBefore(svg, wrap.firstChild);
      const redraw = () => drawLinks(wrap, svg, links);
      if (typeof ResizeObserver === "function") { linkObserver = new ResizeObserver(redraw); linkObserver.observe(wrap); }
      else if (root.requestAnimationFrame) root.requestAnimationFrame(redraw);
    }
    return wrap;
  }

  // ---- the Agents lens: the graph as the mock draws it (F12 mock v2, `topology/mock/src/index.tpl.html`) ----
  // One hub, the groups on its two sides, each group's sessions stacked outward of it with a subagent indented under its parent, the
  // face of every node on its outward side. The layout is columns, not a physics pass (the mock's own buildLayout). Nodes, edges, rings and
  // faces are SVG (every one a real element a test or a screen reader can reach); only the particle streams are a canvas under it.
  // A stream's density is the node's rate (events per minute). A null rate, a rate of 0 and an idle node have NO stream, never a decoration;
  // the loop runs only while some edge streams, adapts to the frame time, and draws nothing under prefers-reduced-motion.
  const MIN_LW = 760;                      // the graph is laid out at no less than this width and scaled down to fit a narrower card
  const MIN_H = 540;
  const RING_H = [0.2, 0.34];
  const PARTICLE_CAP = 1500;
  let ticker = null;                       // the one interval that keeps run times honest between repaints (no repaint, no signature change)
  let streamStop = null;                   // stops the one particle loop

  const sv = (tag, attrs, cls, text) => {
    const n = document.createElementNS(SVG_NS, tag);
    if (cls) n.setAttribute("class", cls);
    if (attrs) for (const k of Object.keys(attrs)) n.setAttribute(k, String(attrs[k]));
    if (text !== undefined && text !== null) n.textContent = String(text);
    return n;
  };
  const num = (x) => Math.round(x * 10) / 10;
  function fmtDur(ms) {
    if (!isNum(ms)) return null;
    const m = Math.max(0, Math.round(ms / 60000));
    if (m < 1) return Math.max(0, Math.round(ms / 1000)) + " s";
    return m >= 60 ? Math.floor(m / 60) + " h " + String(m % 60).padStart(2, "0") + " m" : m + " m";
  }
  const span = (a, b) => { const x = Date.parse(a), y = Date.parse(b); return isNaN(x) || isNaN(y) ? null : y - x; };
  // what a node's face says. `dur` pieces carry their millisecond value so the ticker can keep them moving without a repaint.
  function faceOf(n, asOf) {
    const model = n.model || "model unknown";
    const tk = fmtTokens(n.tokens);
    const l2 = model + " · " + (tk === null ? "tokens unknown" : tk + " tok");
    const tool = typeof n.tool === "string" && n.tool ? n.tool : null;
    const run = span(n.started, asOf), quiet = span(n.last_event, asOf), ran = span(n.started, n.last_event);
    const f = { l1: n.label, l2, unknownModel: !n.model, tag: null, tagDur: null, l3: "", l3Dur: null };
    if (n.state === "idle") {
      const at = clock(n.last_event);
      f.l3 = (at ? "quiet since " + at : "idle") + (fmtDur(ran) ? " · ran " + fmtDur(ran) : "");
    } else if (n.state === "errored") {
      const at = clock(n.last_event);
      f.l3 = (tool || "tool") + " · failed" + (at ? " " + at : "");
    } else {
      f.l3 = tool || "no tool";
      if (run !== null) { f.l3Dur = run; f.l3 += " · " + fmtDur(run); }
    }
    f.tag = { waiting: "WAITING ON YOU", errored: "ERRORED", stalled: "STALLED", idle: "IDLE", running: null }[n.state] || null;
    if (n.state === "stalled" && quiet !== null) { f.tag = "STALLED · no event " + fmtDur(quiet); f.tagDur = quiet; }
    return f;
  }
  // the geometry of the graph (pure: positions only, no DOM). `measure(text, font)` returns a pixel width.
  function layoutGraph(v, LW, measure) {
    const nodes = arr(v.nodes), groups = arr(v.groups).slice();
    const gids = new Set(groups.map((g) => g.id));
    if (nodes.some((n) => !gids.has(n.group))) groups.push({ id: null, label: "ungrouped", repo: null, loose: true });   // a node with no group of its own is still drawn
    const groupOf = (n) => (gids.has(n.group) ? n.group : null);
    const mineOf = (g) => nodes.filter((n) => groupOf(n) === g.id);
    const live = groups.filter((g) => mineOf(g).length);
    const rowsOf = (g) => mineOf(g).length;
    const order = live.slice().sort((a, b) => rowsOf(b) - rowsOf(a));
    const side = { L: [], R: [] }, load = { L: 0, R: 0 };
    for (const g of order) { const s = load.L <= load.R ? "L" : "R"; side[s].push(g); load[s] += rowsOf(g); }
    const gapOf = (l) => Math.max(0, l.length - 1) * 26;
    const need = (s) => load[s] * 64 + gapOf(side[s]);
    const H = Math.max(MIN_H, Math.round(70 + Math.max(need("L"), need("R"))));
    const hub = { x: LW / 2, y: H / 2 + 6, r: 30 };
    const gx0 = Math.max(76, Math.min(190, LW / 2 - 262 - 120));
    const pos = {}, gpos = {};
    const placed = new Set();
    for (const sd of ["L", "R"]) {
      const dir = sd === "L" ? -1 : 1, list = side[sd], gaps = gapOf(list);
      const pitch = Math.max(64, Math.min(86, (H - 70 - gaps) / Math.max(1, load[sd])));
      let y = hub.y - (load[sd] * pitch + gaps) / 2 + pitch / 2;
      for (const g of list) {
        const mine = mineOf(g), ids = new Set(mine.map((n) => n.id)), ys = [];
        const put = (n, depth) => {
          if (placed.has(n.id)) return;
          placed.add(n.id);
          const x = hub.x + dir * (gx0 + 120) - dir * Math.min(30 * depth, 90);
          pos[n.id] = { id: n.id, x, y, side: dir, depth }; ys.push(y); y += pitch;
          for (const k of mine) if (k.parent === n.id) put(k, depth + 1);
        };
        for (const n of mine) if (!n.parent || !ids.has(n.parent)) put(n, 0);
        for (const n of mine) put(n, 0);                 // a parent cycle has no root: the rest sit at the column's own level
        const gy = ys.reduce((s, q) => s + q, 0) / (ys.length || 1);
        gpos[g.id === null ? "" : g.id] = { id: g.id, label: g.label, x: hub.x + dir * gx0, y: gy, r: 16 };
        y += 26;
      }
    }
    for (const n of nodes) {
      const f = faceOf(n, v.as_of), p = pos[n.id];
      let w = Math.max(measure(f.l1, "l1"), measure(f.l2, "l2"), measure(f.l3, "l2"));
      if (f.tag) w = Math.max(w, measure(f.tag, "tag") + 16);
      p.tw = w + 6;
    }
    return { LW, H, hub, gpos, pos, gkey: (n) => (gids.has(n.group) ? n.group : "") };
  }
  // an edge: far end `a` (the event source) to near end `b` (its parent), as the mock bends it
  function mkEdge(a, b, bend) {
    const dx = b.x - a.x, dy = b.y - a.y, len = Math.hypot(dx, dy) || 1, nx = -dy / len, ny = dx / len, off = len * bend;
    return { a, b, len, nx, ny, c1: { x: a.x + dx * 0.33 + nx * off, y: a.y + dy * 0.33 + ny * off }, c2: { x: a.x + dx * 0.68 + nx * off * 0.6, y: a.y + dy * 0.68 + ny * off * 0.6 } };
  }
  const edgePath = (e) => "M" + num(e.a.x) + " " + num(e.a.y) + " C" + num(e.c1.x) + " " + num(e.c1.y) + " " + num(e.c2.x) + " " + num(e.c2.y) + " " + num(e.b.x) + " " + num(e.b.y);
  function bez(e, t) {
    const u = 1 - t;
    return { x: u * u * u * e.a.x + 3 * u * u * t * e.c1.x + 3 * u * t * t * e.c2.x + t * t * t * e.b.x, y: u * u * u * e.a.y + 3 * u * u * t * e.c1.y + 3 * u * t * t * e.c2.y + t * t * t * e.b.y };
  }
  // the edges the stream runs on, each with its rate (null / 0 / idle = no stream). A group's stream is its nodes' rates summed (events roll up).
  function graphEdges(v, L) {
    const byId = new Map(arr(v.nodes).map((n) => [n.id, n]));
    const streams = (n) => n.state !== "idle" && isNum(n.rate) && n.rate > 0;
    const gRate = new Map();
    for (const n of arr(v.nodes)) { const k = L.gkey(n); gRate.set(k, (gRate.get(k) || 0) + (streams(n) ? n.rate : 0)); }
    const out = [];
    let i = 0;
    for (const k of Object.keys(L.gpos)) {
      const gp = L.gpos[k];
      out.push({ kind: "hub", id: k, rate: gRate.get(k) || 0, state: null, e: mkEdge({ x: gp.x, y: gp.y }, { x: L.hub.x, y: L.hub.y }, (gp.x < L.hub.x ? -1 : 1) * 0.1 * (i++ % 2 ? 1 : -1)) });
    }
    for (const n of arr(v.nodes)) {
      const a = L.pos[n.id], to = n.parent && L.pos[n.parent] && byId.has(n.parent) ? L.pos[n.parent] : L.gpos[L.gkey(n)];
      out.push({ kind: "node", id: n.id, rate: streams(n) ? n.rate : 0, state: n.state, e: mkEdge({ x: a.x, y: a.y }, { x: to.x, y: to.y }, 0.12 * (a.y < to.y ? 1 : -1)) });
    }
    return out;
  }
  // a conflict is drawn between the first node on the file and each other one, the file's name on a chip on the first arc
  function conflictArcs(v, L) {
    const out = [], byId = new Map(arr(v.nodes).map((n) => [n.id, n]));
    for (const c of arr(v.conflicts)) {
      if (!isObj(c) || typeof c.path !== "string") continue;
      const ids = arr(c.nodes).filter((id) => L.pos[id] && byId.has(id));
      for (let k = 1; k < ids.length; k++) {
        const A = L.pos[ids[0]], B = L.pos[ids[k]];
        const mx = (A.x + B.x) / 2, my = (A.y + B.y) / 2, dx = B.x - A.x, dy = B.y - A.y, ln = Math.hypot(dx, dy) || 1;
        const gp = L.gpos[L.gkey(byId.get(ids[0]))] || L.hub;
        const sg = ((-dy / ln) * (mx - gp.x) + (dx / ln) * (my - gp.y)) >= 0 ? -1 : 1;
        const cx = mx + (-dy / ln) * 62 * sg, cy = my + (dx / ln) * 62 * sg;
        out.push({ path: c.path, A, B, cx, cy, chip: k === 1, lx: 0.25 * A.x + 0.5 * cx + 0.25 * B.x, ly: 0.25 * A.y + 0.5 * cy + 0.25 * B.y, ids: [ids[0], ids[k]] });
      }
    }
    return out;
  }

  // ---- the SVG ----
  function drawGraph(v, L, edges, measure, opts) {
    const svg = sv("svg", { viewBox: "0 0 " + L.LW + " " + L.H, role: "group", "aria-label": "Agents graph", preserveAspectRatio: "xMidYMid meet" }, "tv-graph");
    const nodes = arr(v.nodes), byId = new Map(nodes.map((n) => [n.id, n]));
    const lost = lostMap(v);
    for (const k of RING_H) svg.appendChild(sv("ellipse", { cx: num(L.hub.x), cy: num(L.hub.y), rx: num(L.LW * k * 1.15), ry: num(L.H * k * 1.05) }, "tv-gring"));
    const eg = sv("g", null, "tv-edges");
    for (const ed of edges) {
      const live = ed.rate > 0, waiting = ed.kind === "node" && ed.state === "waiting";
      const p = sv("path", { d: edgePath(ed.e), "data-edge": ed.kind + ":" + ed.id, "data-rate": live ? String(ed.rate) : "none" }, "tv-edge" + (live ? " live" : "") + (ed.kind === "hub" ? " hub" : "") + (waiting ? " waiting" : "") + (!live && !waiting ? " still" : ""));
      eg.appendChild(p);
    }
    svg.appendChild(eg);
    // file conflicts: the file on the arc
    for (const a of conflictArcs(v, L)) {
      const g = sv("g", { "data-conflict": a.path, "data-nodes": a.ids.join(",") }, "tv-gconf");
      g.appendChild(sv("path", { d: "M" + num(a.A.x) + " " + num(a.A.y) + " Q" + num(a.cx) + " " + num(a.cy) + " " + num(a.B.x) + " " + num(a.B.y) }, "tv-confline"));
      if (a.chip) {
        const w = measure(a.path, "chip") + 16;
        g.appendChild(sv("rect", { x: num(a.lx - w / 2), y: num(a.ly - 10), width: num(w), height: 20, rx: 6 }, "tv-confchip"));
        g.appendChild(sv("text", { x: num(a.lx), y: num(a.ly + 4), "text-anchor": "middle" }, "tv-conftext", a.path));
      }
      svg.appendChild(g);
    }
    // the hub: the install, "N sessions · M running"
    const running = nodes.filter((n) => n.state === "running").length, h = L.hub;
    const hubLabel = isObj(v.hub) && typeof v.hub.label === "string" ? v.hub.label : "";
    const hg = sv("g", { "data-hub": isObj(v.hub) && typeof v.hub.id === "string" ? v.hub.id : "" }, "tv-ghub");
    hg.appendChild(sv("circle", { cx: num(h.x), cy: num(h.y), r: h.r + 8 }, "tv-hubhalo"));
    hg.appendChild(sv("circle", { cx: num(h.x), cy: num(h.y), r: h.r }, "tv-hubbody"));
    hg.appendChild(sv("circle", { cx: num(h.x), cy: num(h.y), r: 9 }, "tv-hubcore" + (running ? " run" : "")));
    const hsub = nodes.length + (nodes.length === 1 ? " session" : " sessions") + " · " + running + " running";
    const hw = Math.max(measure(hsub, "l2"), 70);
    hg.appendChild(sv("rect", { x: num(h.x - hw / 2 - 7), y: num(h.y - h.r - 42), width: num(hw + 14), height: 40, rx: 7 }, "tv-lblbg"));
    hg.appendChild(sv("text", { x: num(h.x), y: num(h.y - h.r - 26), "text-anchor": "middle" }, "tv-hubname", hubLabel));
    hg.appendChild(sv("text", { x: num(h.x), y: num(h.y - h.r - 10), "text-anchor": "middle" }, "tv-hubsub", hsub));
    svg.appendChild(hg);
    // the groups: "N agents · M running"
    for (const k of Object.keys(L.gpos)) {
      const gp = L.gpos[k], mem = nodes.filter((n) => L.gkey(n) === k);
      const run = mem.filter((n) => n.state === "running").length, wait = mem.some((n) => n.state === "waiting");
      const gg = sv("g", { "data-group": k }, "tv-ggroup" + (wait ? " waiting" : ""));
      gg.appendChild(sv("circle", { cx: num(gp.x), cy: num(gp.y), r: gp.r }, "tv-gbody"));
      gg.appendChild(sv("circle", { cx: num(gp.x), cy: num(gp.y), r: 4 }, "tv-gcore" + (run ? " run" : "")));
      const sub = mem.length + (mem.length === 1 ? " agent" : " agents") + " · " + run + " running";
      const lw = Math.max(measure(gp.label, "group"), measure(sub, "l2")), ly = gp.y + gp.r + 18;
      gg.appendChild(sv("rect", { x: num(gp.x - lw / 2 - 7), y: num(ly - 14), width: num(lw + 14), height: 36, rx: 7 }, "tv-lblbg"));
      gg.appendChild(sv("text", { x: num(gp.x), y: num(ly), "text-anchor": "middle" }, "tv-gname", gp.label));
      gg.appendChild(sv("text", { x: num(gp.x), y: num(ly + 15), "text-anchor": "middle" }, "tv-gsub", sub));
      svg.appendChild(gg);
    }
    // the nodes, each with its face
    const tickers = [];
    for (const n of nodes) {
      const P = L.pos[n.id], f = faceOf(n, v.as_of), dir = P.side, wait = n.state === "waiting", r = wait ? 11 : 9;
      const g = sv("g", { "data-node": n.id, "data-state": n.state }, "tv-gnode " + n.state);
      g.appendChild(sv("circle", { cx: num(P.x), cy: num(P.y), r: 17 }, "tv-hit"));
      if (wait) {
        for (let i = 6; i >= 1; i--) g.appendChild(sv("circle", { cx: num(P.x), cy: num(P.y), r: r + i * 6, "fill-opacity": num((0.05 + 0.03 * 0.5) * (7 - i) / 6) }, "tv-glow"));
        g.appendChild(sv("circle", { cx: num(P.x), cy: num(P.y), r: r + 10 }, "tv-pulse"));
      }
      g.appendChild(sv("circle", { cx: num(P.x), cy: num(P.y), r }, "tv-ring"));
      if (n.state === "errored") g.appendChild(sv("path", { d: "M" + num(P.x - 3.2) + " " + num(P.y - 3.2) + " L" + num(P.x + 3.2) + " " + num(P.y + 3.2) + " M" + num(P.x + 3.2) + " " + num(P.y - 3.2) + " L" + num(P.x - 3.2) + " " + num(P.y + 3.2) }, "tv-x"));
      else g.appendChild(sv("circle", { cx: num(P.x), cy: num(P.y), r: wait ? 5.5 : n.state === "idle" ? 3.2 : 4.4 }, "tv-core"));
      // the face sits outward of the node
      const tx = P.x + dir * (wait ? 26 : 17), anchor = dir === 1 ? "start" : "end", top = P.y - 22;
      const bw = P.tw + 8, bh = f.tag ? 66 : 50, bx = dir === 1 ? tx - 5 : tx - P.tw - 3;
      g.appendChild(sv("rect", { x: num(bx), y: num(P.y - (f.tag ? 30 : 13)), width: num(bw), height: bh, rx: 6 }, "tv-facebg"));
      if (f.tag) {
        const tw = measure(f.tag, "tag") + 14, px = dir === 1 ? tx - 3 : tx - tw + 3;
        if (wait) g.appendChild(sv("rect", { x: num(px), y: num(top - 8), width: num(tw), height: 16, rx: 8 }, "tv-tagpill"));
        const t = sv("text", { x: num(dir === 1 ? px + 7 : px + tw - 7), y: num(top + 4), "text-anchor": anchor }, "tv-facetag", f.tag);
        if (f.tagDur !== null) tickers.push({ el: t, ms: f.tagDur, text: (ms) => "STALLED · no event " + fmtDur(ms) });
        g.appendChild(t);
      }
      g.appendChild(sv("text", { x: num(tx), y: num(top + 21), "text-anchor": anchor }, "tv-face1", f.l1));
      g.appendChild(sv("text", { x: num(tx), y: num(top + 36), "text-anchor": anchor }, "tv-face2" + (f.unknownModel ? " unk" : ""), f.l2));
      const l3 = sv("text", { x: num(tx), y: num(top + 50), "text-anchor": anchor }, "tv-face3", f.l3);
      if (f.l3Dur !== null) { const base = f.l3.slice(0, f.l3.length - fmtDur(f.l3Dur).length); tickers.push({ el: l3, ms: f.l3Dur, text: (ms) => base + fmtDur(ms) }); }
      g.appendChild(l3);
      const risk = riskTitle(lost.get(n.id));
      if (risk) g.appendChild(sv("circle", { cx: num(P.x + dir * 12), cy: num(P.y - 12), r: 3.5 }, "tv-gnode-risk"));
      g.dataset.face = [f.tag, f.l1, f.l2, f.l3].filter(Boolean).join(" | ");
      if (wait && typeof n.card === "string" && n.card && opts && typeof opts.openCard === "function") {
        g.classList.add("tv-open");
        g.setAttribute("role", "button");
        g.setAttribute("tabindex", "0");
        g.setAttribute("aria-label", n.label + ", waiting on you: open its card");
        g.dataset.card = n.card;
        const go = () => opts.openCard(n.card);
        g.addEventListener("click", go);
        g.addEventListener("keydown", (ev) => { if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); go(); } });
      }
      svg.appendChild(g);
    }
    return { svg, tickers };
  }

  // the hover card: the whole record of a node, local counts only
  function tipRows(n, asOf) {
    const run = fmtDur(span(n.started, asOf)), tk = fmtTokens(n.tokens);
    const rows = [["state", NODE_WORDS[n.state] || n.state], ["agent", n.agent || "unknown"], ["model", n.model || "model unknown"],
                  ["tokens", tk === null ? "tokens unknown" : tk + " tok"], ["tool", typeof n.tool === "string" && n.tool ? n.tool : "none"],
                  ["run time", run || "unknown"], ["last event", clock(n.last_event) || "unknown"],
                  ["rate", isNum(n.rate) ? n.rate.toFixed(1) + " events/min" : "rate not tracked"]];
    if (typeof n.card === "string" && n.card) rows.push(["waiting on", n.card]);
    return rows;
  }

  // ---- the particles: a canvas under the graph, drawn only while some edge streams ----
  function startStreams(canvas, stage, edges, getScale) {
    const reduced = !!(root.matchMedia && root.matchMedia("(prefers-reduced-motion: reduce)").matches);
    const live = edges.filter((e) => e.rate > 0);
    stage.dataset.streams = String(live.length);
    stage.dataset.particles = "0";
    stage.dataset.motion = reduced ? "off" : "on";
    if (reduced || !live.length || !canvas.getContext || typeof root.requestAnimationFrame !== "function") return null;
    const ctx = canvas.getContext("2d");
    if (!ctx) return null;
    const PART = [];
    let quality = 1, ema = 16.7, last = 0, n = 0, stopped = false, raf = 0, color = "";
    const step = (dt) => {
      for (const ed of live) {
        ed.acc = (ed.acc || 0) + ed.rate * (ed.kind === "hub" ? 5 : 10) * quality * dt;
        while (ed.acc >= 1) { ed.acc -= 1; PART.push({ ed, t: 0, sp: 0.22 + Math.random() * 0.22, off: Math.random() + Math.random() - 1, sz: 1.3 + Math.random() * 1.3, hub: ed.kind === "hub" }); }
      }
      for (let i = PART.length - 1; i >= 0; i--) { const p = PART[i]; p.t += p.sp * dt * (p.ed.e.len > 200 ? 0.8 : 1.15); if (p.t >= 1) PART.splice(i, 1); }
      const cap = Math.round(PARTICLE_CAP * quality);
      if (PART.length > cap) PART.splice(0, PART.length - cap);
    };
    const draw = () => {
      const s = getScale(), dpr = canvas.width / Math.max(1, canvas.clientWidth || canvas.width);
      ctx.setTransform(dpr * s, 0, 0, dpr * s, 0, 0);
      ctx.clearRect(0, 0, canvas.width / (dpr * s) + 1, canvas.height / (dpr * s) + 1);
      ctx.fillStyle = color || "#999";
      for (const p of PART) {
        const e = p.ed.e, pt = bez(e, p.t), spread = (p.hub ? 7 : 4.2) * (0.25 + p.t) * p.off;
        ctx.globalAlpha = Math.pow(Math.sin(Math.PI * Math.min(1, p.t)), 0.7) * (p.hub ? 0.62 : 0.8);
        ctx.fillRect(pt.x + e.nx * spread - p.sz / 2, pt.y + e.ny * spread - p.sz / 2, p.sz, p.sz);
      }
      ctx.globalAlpha = 1;
    };
    const loop = (ts) => {
      if (stopped) return;
      if (!canvas.isConnected) { stopped = true; return; }
      const dt = Math.min(0.05, last ? (ts - last) / 1000 : 0.016);
      last = ts;
      ema = ema * 0.9 + dt * 1000 * 0.1;                                  // frame-time adaptive: long frames thin the stream
      if (ema > 24) quality = Math.max(0.15, quality * 0.93); else if (ema < 18) quality = Math.min(1, quality * 1.01);
      if (n % 60 === 0) color = root.getComputedStyle(canvas).color;
      step(dt);
      draw();
      if (++n % 20 === 0) { stage.dataset.particles = String(PART.length); stage.dataset.quality = quality.toFixed(2); }
      raf = root.requestAnimationFrame(loop);
    };
    for (let i = 0; i < 260; i++) step(0.03);                             // the stream is already flowing when the lens opens
    stage.dataset.particles = String(PART.length);
    raf = root.requestAnimationFrame(loop);
    return () => { stopped = true; if (raf && root.cancelAnimationFrame) root.cancelAnimationFrame(raf); PART.length = 0; };
  }

  function agentsLens(v, opts) {
    release();
    const wrap = el("div", "tv-agents");
    const stage = el("div", "tv-stage");
    stage.appendChild(el("span", "tv-hint", "Click a node for its card. Hover for the face."));
    const canvas = el("canvas", "tv-particles");
    canvas.setAttribute("aria-hidden", "true");
    stage.appendChild(canvas);
    const tip = el("div", "tv-tip");
    tip.hidden = true;
    tip.setAttribute("aria-hidden", "true");
    wrap.appendChild(stage);
    let LW = 0, svgNode = null, scale = 1, built = false;
    const mctx = document.createElement("canvas").getContext ? document.createElement("canvas").getContext("2d") : null;
    const build = (W) => {
      const cs = root.getComputedStyle(stage);
      const fonts = { l1: "600 12px " + (cs.getPropertyValue("--round").trim() || "sans-serif"), l2: "500 11px " + (cs.getPropertyValue("--mono").trim() || "monospace"),
                      tag: "700 10px " + (cs.getPropertyValue("--round").trim() || "sans-serif"), group: "700 13px " + (cs.getPropertyValue("--round").trim() || "sans-serif"),
                      chip: "600 11px " + (cs.getPropertyValue("--mono").trim() || "monospace") };
      const measure = (text, k) => { if (mctx) { mctx.font = fonts[k]; return mctx.measureText(String(text)).width; } return String(text).length * 6.4; };
      LW = Math.max(MIN_LW, Math.round(W));
      scale = Math.min(1, W / LW);
      const L = layoutGraph(v, LW, measure);
      const edges = graphEdges(v, L);
      if (streamStop) { streamStop(); streamStop = null; }
      if (svgNode) svgNode.remove();
      const g = drawGraph(v, L, edges, measure, opts);
      svgNode = g.svg;
      stage.insertBefore(svgNode, canvas);
      // the canvas keeps the graph's own proportions: CSS size follows the SVG, the backing store follows the device
      const cssW = Math.round(W), cssH = Math.round(L.H * scale), dpr = Math.min(2, root.devicePixelRatio || 1);
      canvas.width = Math.max(1, Math.round(cssW * dpr)); canvas.height = Math.max(1, Math.round(cssH * dpr));
      canvas.style.width = cssW + "px"; canvas.style.height = cssH + "px";
      if (ticker) { root.clearInterval(ticker); ticker = null; }
      if (g.tickers.length) {
        const t0 = (root.performance && root.performance.now ? root.performance.now() : Date.now());
        const nowMs = () => (root.performance && root.performance.now ? root.performance.now() : Date.now());
        ticker = root.setInterval(() => {
          if (!stage.isConnected) { root.clearInterval(ticker); ticker = null; return; }
          const d = nowMs() - t0;
          for (const t of g.tickers) t.el.textContent = t.text(t.ms + d);
        }, 20000);
      }
      streamStop = startStreams(canvas, stage, edges, () => scale);
      stage.dataset.nodes = String(arr(v.nodes).length);
      built = true;
    };
    build(920);                                                           // a width to draw at before the card has one; the observer redraws at the real one
    let seen = 0;
    if (typeof ResizeObserver === "function") {
      linkObserver = new ResizeObserver(() => {
        const w = Math.round(stage.getBoundingClientRect().width);
        if (w > 0 && (w !== seen || !built)) { const first = seen === 0; seen = w; if (first || Math.abs(w - LW) > 1 || scale < 1) build(w); }
      });
      linkObserver.observe(stage);
    }
    // the hover card follows the pointer over a node, inside the stage
    const nodeOf = (t) => (t && t.closest ? t.closest("[data-node]") : null);
    const byId = new Map(arr(v.nodes).map((n) => [n.id, n]));
    stage.addEventListener("pointermove", (ev) => {
      const g = nodeOf(ev.target), n = g ? byId.get(g.dataset.node) : null;
      if (!n) { tip.hidden = true; return; }
      if (tip.dataset.node !== n.id) {
        tip.dataset.node = n.id;
        while (tip.firstChild) tip.removeChild(tip.firstChild);
        tip.appendChild(el("b", null, n.label));
        for (const [k, val] of tipRows(n, v.as_of)) { const r = el("div"); r.append(el("span", "tv-tl", k), val); tip.appendChild(r); }
      }
      tip.hidden = false;
      const box = stage.getBoundingClientRect(), x = ev.clientX - box.left, y = ev.clientY - box.top;
      tip.style.left = Math.max(4, Math.min(box.width - tip.offsetWidth - 4, x + 16)) + "px";
      tip.style.top = Math.max(4, Math.min(box.height - tip.offsetHeight - 4, y + 14)) + "px";
    });
    stage.addEventListener("pointerleave", () => { tip.hidden = true; tip.dataset.node = ""; });
    stage.appendChild(tip);
    return wrap;
  }

  // the legend under the Agents graph: the five states and what a stream is
  function legendAgents() {
    const l = el("div", "tv-legend tv-legend-agents");
    for (const st of ["waiting", "errored", "stalled", "running", "idle"]) {
      const i = el("span", "tv-leg");
      const k = el("i", "tv-lg " + st);
      k.setAttribute("aria-hidden", "true");
      i.append(k, st === "waiting" ? "waiting on you" : st);
      l.appendChild(i);
    }
    l.appendChild(el("span", "tv-legdef", "streams carry events per minute; none when idle"));
    return l;
  }

  // ---- the legend under the Work columns: the five states and the two definitions that keep waiting and stuck apart ----
  function legend() {
    const l = el("div", "tv-legend");
    for (const st of ["waiting", "stuck", "failed", "running", "open", "done"]) {
      const i = el("span", "tv-leg");
      i.append(dot(st), st === "waiting" ? "waiting on you" : st);
      l.appendChild(i);
    }
    l.appendChild(el("span", "tv-legdef", "waiting = a pending record you can answer; stuck = silence, no record"));
    return l;
  }

  // ---- Lost if closed (F12c): its own panel beside the Sessions card; per live session, what closing it now would lose ----
  // Built only from `lost_if_closed`. Unknown is drawn as unknown: a null count is a dashed chip that says so, a session whose git state
  // was not read never claims "nothing at risk".
  const plural = (n, one, many) => n + " " + (n === 1 ? one : many);
  function lostChip(text, unknown) { return el("span", "lc-chip" + (unknown ? " unk" : ""), text); }
  function lostHead(count, losers) {
    const h = el("div", "lc-h");
    h.appendChild(el("h2", null, "Lost if closed"));
    if (count !== null) {
      h.appendChild(el("span", "ck-count", String(count)));
      h.appendChild(el("span", "lc-sum", losers + " would lose work"));
    }
    return h;
  }
  function lostShell(head) {
    const p = el("section", "panel lc");
    p.dataset.lc = "1";
    p.appendChild(head);
    return p;
  }
  // the panel's own unavailable line (the topology provider failed: the same fault the Sessions card says)
  function renderLostFault(text) {
    const p = lostShell(lostHead(null, 0));
    p.appendChild(el("p", "lc-none err", "Unavailable: " + text));
    return p;
  }
  function renderLost(v) {
    const byId = new Map();
    for (const n of arr(v.nodes)) byId.set(n.id, n);
    const rows = arr(v.lost_if_closed).filter(isObj);
    const known = (x) => isNum(x);
    const risky = (l) => [l.dirty, l.unpushed, l.running_children, l.uncaptured_episodes].some((x) => known(x) && x > 0);
    const losers = rows.filter(risky);
    const p = lostShell(lostHead(rows.length ? rows.length : null, losers.length));
    if (!rows.length) { p.appendChild(el("p", "lc-none", "No live sessions, so nothing to lose.")); }
    const name = (l) => { const n = byId.get(l.node); return n ? n.label : String(l.node); };
    const dotFor = (l) => { const n = byId.get(l.node); return dot(n ? n.state : "idle"); };
    for (const l of losers) {
      const s = el("div", "lc-sess");
      s.dataset.node = l.node;
      const top = el("div", "lc-top");
      top.append(dotFor(l), el("b", null, name(l)));
      s.appendChild(top);
      const chips = el("div", "lc-chips");
      if (l.dirty === null || l.dirty === undefined) chips.appendChild(lostChip("dirty files not read", true));
      else if (l.dirty > 0) chips.appendChild(lostChip(plural(l.dirty, "dirty file", "dirty files")));
      if (l.unpushed === null || l.unpushed === undefined) chips.appendChild(lostChip("unpushed not read", true));
      else if (l.unpushed > 0) chips.appendChild(lostChip(plural(l.unpushed, "unpushed", "unpushed")));
      if (l.uncaptured_episodes === null || l.uncaptured_episodes === undefined) chips.appendChild(lostChip("uncaptured unknown", true));
      else if (l.uncaptured_episodes > 0) chips.appendChild(lostChip(plural(l.uncaptured_episodes, "uncaptured episode", "uncaptured episodes")));
      if (l.running_children === null || l.running_children === undefined) chips.appendChild(lostChip("running lanes unknown", true));
      else if (l.running_children > 0) chips.appendChild(lostChip(plural(l.running_children, "running lane", "running lanes")));
      s.appendChild(chips);
      const names = arr(l.dirty_names).filter((x) => typeof x === "string" && x);
      const branches = arr(l.branches).filter((x) => typeof x === "string" && x);
      const mono = names.join(", ") + (branches.length ? (names.length ? " · " : "") + "branch " + branches.join(", ") : "");
      if (mono) s.appendChild(el("div", "lc-mono", mono));
      p.appendChild(s);
    }
    const quiet = [];
    for (const l of rows) {
      if (risky(l)) continue;
      const r = el("div", "lc-line");
      r.dataset.node = l.node;
      r.append(dotFor(l), el("span", "lc-name", name(l)));
      // clear only when git was read and found nothing: an unread tree says so instead
      if (known(l.dirty) && known(l.unpushed)) r.appendChild(el("span", "lc-ok", "✓ nothing at risk"));
      else r.appendChild(el("span", "lc-unread", "git state not read"));
      quiet.push(r);
    }
    if (quiet.length) { const q = el("div", "lc-quiet"); for (const r of quiet) q.appendChild(r); p.appendChild(q); }
    if (rows.length) p.appendChild(el("p", "lc-foot", "Read-only: git, the session registry and the episodic store. Closing a session never runs anything."));
    return p;
  }

  // ---- the body ----
  // v: the topology value. opts: {repaint(): redraw the card, openCard(id): open the Needs-you row a waiting item links to}
  function render(v, opts) {
    const o = opts || {};
    const which = lens();
    const w = el("div", "tv");
    w.dataset.lens = which;
    const bar = el("div", "tv-bar");
    bar.append(seg("Lens", [["work", "Work"], ["agents", "Agents"]], which, (id) => { keep(LENS_KEY, id); if (o.repaint) o.repaint(); }), which === "agents" ? strip(v, which) : stripWork(v));
    w.appendChild(bar);
    const conf = conflictList(v);
    if (conf) w.appendChild(conf);
    w.appendChild(which === "agents" ? agentsLens(v, o) : workLens(v, o));
    w.appendChild(which === "agents" ? legendAgents() : legend());
    const q = quietLine(v);
    if (q) w.appendChild(q);
    return w;
  }

  root.LevainTopologyView = { WIDE_PX, mode, lens, switchNode, hasWork, quietLine, signature, release, render, renderLost, renderLostFault };
  if (typeof module !== "undefined" && module.exports) module.exports = root.LevainTopologyView;
})(typeof globalThis !== "undefined" ? globalThis : this);

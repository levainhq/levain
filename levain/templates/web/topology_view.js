// levain topology_view.js — the live work view (F12b): the `topology` panel value drawn in the page header (the Activity slot).
//
// It is a renderer and nothing else. cockpit_view.js parses the panel value (the one shape check) and hands it here with a
// repaint hook and a card opener; this file draws two lenses over the same value and owns the header's three-way switch
// [Agents graph | Event graph | Work] and the choice that persists (the Event graph is the page's Activity band, drawn by sky.js, not here). Nothing here assumes Levain's own ids or words: the
// hub is whatever the provider names (Levain: the install; flow: the desk), groups are repos or seats, nodes are sessions or
// lanes. Everything is built with textContent: a wire string never reaches an HTML sink.
//
//   Work lens: three columns as drawn. LEFT, the waiting records (one per pending consent / question, plus a
//   dispatch contract waiting on a signature). CENTRE, the tasks as compact cards, loudest state first; waiting-orange is a
//   border and a tag, stuck is dashed amber, done is quiet. RIGHT, each task's outcomes on the task's own row. A line joins a
//   record to the task that waits on it and a task to its outcomes (the lines are ornament: the columns are the grammar).
//   Agents are small chips on the task they work. A session with no task object sits in an "unattached work" block, never a
//   guessed task. Waiting (a pending record the operator can answer) and stuck (silence, no record) never merge. In a
//   narrow card the columns stack: records, tasks, each task's outcomes beneath it.
//   Agents lens (the header's default on a cold open): the graph as the F12 mock draws it. One hub (the install), the groups on its two sides, each group's sessions beside it
//   with a subagent under its parent, every node with its face and a state ring, edges hub -> group -> node -> child that carry particle
//   streams whose density is the node's rate. A file conflict is the file's name on a chip between its nodes.
//
// Unknown is drawn as unknown: a null cost, token count, rate, conflict list or model reads "not tracked" / "model unknown",
// never 0. The Work lens draws no particles (it has no edge to carry them). The Agents lens draws a stream only on an edge whose node has a
// real positive rate and is not idle: a null rate, a rate of 0 and an idle node are still, and under prefers-reduced-motion nothing moves.
(function (root) {
  "use strict";

  const HEADER_KEY = "levain_cockpit_header_lens";      // the header's three-way choice: agents | events | work (a view preference, never data)
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

  // ---- what persists: the header lens (agents graph | event graph | work). A cold open, or a store that throws or holds anything else, is the Agents graph ----
  const HEADER_LENSES = [["agents", "Agents graph"], ["events", "Event graph"], ["work", "Work"]];
  // A phone-width card with no stored choice opens on Work: the graph draws at a fraction of its scale there, its text unreadable. An explicit
  // pick (including "agents") is kept; the graph stays one tap away.
  const NARROW_PX = 560;
  function headerLens() {
    const v = stored(HEADER_KEY);
    if (v === "events" || v === "work" || v === "agents") return v;
    return typeof root.innerWidth === "number" && root.innerWidth > 0 && root.innerWidth < NARROW_PX ? "work" : "agents";
  }
  function setHeaderLens(id) { keep(HEADER_KEY, id); }

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
  // the header's lens switch: buttons with aria-pressed, as the page's other toggles. The Agents and Work lenses carry it in their own bar (render's
  // `lensSeg`), the Event graph (the page's Activity band) under it; the click changes the view only, never what is read.
  function headerToggle(current, onPick) {
    return seg("Header view", HEADER_LENSES, current, (id) => { setHeaderLens(id); onPick(id); });
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
    return JSON.stringify([l, sane, Array.from(shownAll), replaySig()]);       // T is in it: two instants repaint even when the value reads alike
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
    if (c.source === "transcript" && usd === null && tok !== null) return tok + " tokens from transcripts";   // spend read off the workers' transcripts: tokens, never a price
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

  // The decision mark (Phill 10-23): the desk (the hub) or a seat node that carries needs_open > 0 is drawn with a red ring and a "!" badge holding
  // the count. The keys are optional: absent, zero, non-integer or negative draws nothing and is never an error. needs_first_card (the card id the
  // cockpit resolves, never derived from needs_first) makes the badge a button that opens that card; without it the badge is display-only.
  // needs_first_what and needs_first are clipped at 120 code points (never refused) for the title and a data attribute; the card id is never cut: one that is not a
  // non-empty string or is longer than 120 is refused (display-only), never rewritten.
  const NEED_CAP = 120;
  // what each marked owner ("hub", "node:<id>") last showed on a live lens: a mark plays its motion when it arrives or its count changes (a seat's: rises)
  const needSeen = new Map();
  // a seat with no id of its own (not a non-empty string) has no owner key to be remembered by: it gets no mark
  const nodeNeed = (n) => (isObj(n) && typeof n.id === "string" && n.id ? needsOf(n) : null);
  const needsSeenNow = (v) => { const m = new Map(); const h = needsOf(v.hub); if (h) m.set("hub", h.n); for (const n of arr(v.nodes)) { const k = nodeNeed(n); if (k) m.set("node:" + n.id, k.n); } return m; };
  const capStr = (x) => (typeof x === "string" ? Array.from(x).slice(0, NEED_CAP).join("") : "");
  function needsOf(o) {
    if (!isObj(o) || !Number.isInteger(o.needs_open) || o.needs_open <= 0) return null;
    return { n: o.needs_open, first: capStr(o.needs_first), card: typeof o.needs_first_card === "string" && Array.from(o.needs_first_card).length <= NEED_CAP ? o.needs_first_card : "", what: capStr(o.needs_first_what) };
  }
  // ring radius rr around (cx, cy), the badge on the ring at angle ang (radians); owner is the typed key "hub" or "node:<id>". Returns {inner, outer, under}:
  // inner goes inside the node (or hub) group; outer, set only when the badge is a button, is a sibling group at the same coordinates (no interactive
  // role nested in the node's role=button) drawn ABOVE every node, holding the pill; under is the transparent 44 CSS px tap pad (at the graph's scale),
  // drawn BENEATH every node so a tap on a node's own shape reaches the node (message the seat) and only the margin around the pill reaches the card.
  // deco, set only for the desk: {art, bx, by, rx}: the desk's own drawing (a group already placed at the hub) in place of the plain ring, and the badge's
  // centre as an offset from the hub, with its corner radius. A seat's mark passes none and is the red ring it always was.
  // the badge's geometry, one function for the drawing and for the conflict chips' placement: centre, pill width, the tap pad's side (44 CSS px at the graph's scale)
  function needGeom(nd, cx, cy, rr, ang, scale, spot) {
    const bx = spot ? cx + spot.bx : cx + Math.cos(ang) * rr, by = spot ? cy + spot.by : cy + Math.sin(ang) * rr, txt = "!" + (nd.n > 9 ? "9+" : String(nd.n));
    return { bx, by, txt, w: txt.length > 2 ? 30 : 24, side: Math.max(44, Math.ceil(44 / (scale > 0 ? Math.min(1, scale) : 1))) };
  }
  // a tap in the overlap of pads goes to the badge whose centre is nearest the tap (in the graph's own coordinates); a tap with no usable point goes to the pad it hit
  function nearestPad(pad, ev) {
    try {
      const svg = pad.ownerSVGElement, ctm = svg && svg.getScreenCTM && svg.getScreenCTM();
      if (!ctm || typeof svg.createSVGPoint !== "function") return pad;
      const pt = svg.createSVGPoint(); pt.x = ev.clientX; pt.y = ev.clientY;
      const q = pt.matrixTransform(ctm.inverse());
      let best = pad, bd = Infinity;
      for (const o of Array.from(pad.parentNode.children)) {
        const x = parseFloat(o.getAttribute("x")), y = parseFloat(o.getAttribute("y")), s = parseFloat(o.getAttribute("width"));
        if (!(q.x >= x && q.x <= x + s && q.y >= y && q.y <= y + s) || typeof o.needGo !== "function") continue;
        const d = Math.hypot(q.x - (x + s / 2), q.y - (y + s / 2));
        if (d < bd) { bd = d; best = o; }
      }
      return best;
    } catch (_) { return pad; }
  }
  // The open mark breathes (Phill 10-25), once per NEW open decision: the badge (and the desk's leader line) carry .tv-needpulse for the 8 cycles of the keyframe in dashboard.css (the cost is
  // measured beside it), counted from the paint where the decision first showed; after that the mark is still until the next new one. A repaint rebuilds the nodes, so the cycles already played
  // ride in as a negative delay (the breath continues in phase and ends on schedule instead of restarting). New = the owner had no mark, its count rose, or its first card changed without the count
  // falling (answering the first of several is not new). PULSE_MS is the keyframe period in dashboard.css (tv-needpulse); PULSE_CYCLES its iteration count.
  const PULSE_MS = 4000, PULSE_CYCLES = 8;
  const nowMs = () => (typeof performance !== "undefined" && performance.now ? performance.now() : Date.now());
  const pulseAt = new Map();                                                // owner -> {n, card, t}: committed by the lens once it is the visible build, like needSeen
  let pulseCtx = null;                                                      // the draw in progress (drawGraph is synchronous): its tentative records
  function pulseOf(owner, nd) {
    const t = nowMs();
    let rec = pulseCtx && pulseCtx.pulse.get(owner);
    if (!rec) {
      const prev = pulseAt.get(owner);
      const fresh = !prev || nd.n > prev.n || (nd.card !== prev.card && nd.n >= prev.n);
      rec = { n: nd.n, card: nd.card, t: fresh ? t : prev.t };
      if (pulseCtx) pulseCtx.pulse.set(owner, rec);
    }
    const played = Math.max(0, Math.floor(t - rec.t));
    return played < PULSE_MS * PULSE_CYCLES ? { on: true, delay: "-" + played + "ms" } : { on: false, delay: "" };
  }
  function needMark(nd, cx, cy, rr, ang, opts, label, owner, scale, deco, calm) {
    const g = sv("g", { "data-needs": String(nd.n) }, "tv-need");
    if (deco) g.appendChild(deco.art); else g.appendChild(sv("circle", { cx: num(cx), cy: num(cy), r: num(rr) }, "tv-needring" + (calm ? " calm" : "")));
    const { bx, by, txt, w, side } = needGeom(nd, cx, cy, rr, ang, scale, deco);
    const pu = pulseOf(owner, nd);
    const b = sv("g", { "data-needbadge": nd.card, "data-needfirst": nd.first, "data-needfor": owner }, "tv-needbadge" + (pu.on ? " tv-needpulse" : ""));
    if (pu.on) b.style.animationDelay = pu.delay;
    const tip = nd.what || (nd.n + " decision" + (nd.n === 1 ? "" : "s") + " waiting on you");
    const openable = !!nd.card && !!opts && typeof opts.openCard === "function";
    b.appendChild(sv("rect", { x: num(bx - w / 2), y: num(by - 10), width: w, height: 20, rx: deco ? deco.rx : 10 }, "tv-needpill"));
    b.appendChild(sv("text", { x: num(bx), y: num(by + 5), "text-anchor": "middle" }, "tv-needtxt", txt));
    b.appendChild(sv("title", null, null, tip));
    if (openable) {
      const go = (ev) => { ev.stopPropagation(); opts.openCard(nd.card); };
      const who = typeof label === "string" && label ? label : "a seat";
      b.classList.add("tv-needopen");
      b.setAttribute("role", "button");
      b.setAttribute("tabindex", "0");
      b.setAttribute("aria-label", who + ": " + tip + ": open its card");
      b.addEventListener("click", go);
      b.addEventListener("keydown", (ev) => { if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); if (!ev.repeat) go(ev); } });
      const out = sv("g", null, "tv-needout");
      out.appendChild(b);
      const pad = sv("rect", { x: num(bx - side / 2), y: num(by - side / 2), width: side, height: side, "data-needhitfor": owner, "aria-hidden": "true" }, "tv-needhit");
      pad.needGo = go;
      pad.addEventListener("click", (ev) => nearestPad(pad, ev).needGo(ev));
      return { inner: g, outer: out, under: pad };
    }
    g.appendChild(b);
    return { inner: g, outer: null, under: null };
  }


  // The desk's mark, direction M1 (bezel + callout): 60 engraved ticks (a major every 5) in the alert colour on a thin track hugging the core, a lubber notch at 12
  // o'clock, the "!N" readout on a short leader line (C's aviation callout) to the lower right, clear of every label. On arrival the ticks light in sequence once (CSS: 60 ticks 20 ms apart, each .2 s), then hold; the label
  // rides HUB_LIFT px higher while the desk is marked. Every part is inert to the pointer: the desk's own tap is untouched.
  const HUB_LIFT = 9, HUB_CALLOUT_X = 82, HUB_CALLOUT_Y = 54, HUB_BADGE = { bx: 64, by: 42 };           // the callout pill's far edge from the hub's centre
  const pol = (r, deg) => [num(r * Math.sin(deg * Math.PI / 180)), num(-r * Math.cos(deg * Math.PI / 180))];     // clockwise from 12 o'clock, relative to the centre
  function hubDeco(nd, cx, cy, sweep) {
    const art = sv("g", { transform: "translate(" + num(cx) + " " + num(cy) + ")" }, "tv-hubmark tv-bezel" + (sweep ? " sweep" : ""));
    art.appendChild(sv("circle", { cx: 0, cy: 0, r: 38.5 }, "tv-bz-track"));
    for (let i = 0; i < 60; i++) {
      const maj = i % 5 === 0, a = pol(maj ? 31.6 : 33.4, i * 6), b = pol(37.2, i * 6);
      const t = sv("line", { x1: a[0], y1: a[1], x2: b[0], y2: b[1] }, "tv-bz-tick" + (maj ? " maj" : ""));
      t.style.setProperty("--i", String(i));
      art.appendChild(t);
    }
    art.appendChild(sv("path", { d: "M -3 -43.4 L 3 -43.4 L 0 -38.8 Z" }, "tv-bz-lub"));
    const s = pol(39.6, 135);
    const pu = pulseOf("hub", nd);
    const lead = sv("path", { d: "M " + s + " L 42 42 L 52 42" }, "tv-bz-lead" + (pu.on ? " tv-needpulse" : ""));
    if (pu.on) lead.style.animationDelay = pu.delay;
    art.appendChild(lead);
    return { art, bx: HUB_BADGE.bx, by: HUB_BADGE.by, rx: 4 };
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
  // "waiting on you" is ONE figure from ONE function: every distinct thing he can answer, counted once. A session in state waiting is one; so is
  // a waiting task that no waiting session carries (a seat-less ask arrives as a task with no workers), while a waiting task whose worker session
  // is itself waiting is that same ask and adds nothing. Every lens's headline figure and the Work lens's empty sentence read it; the Work lens's
  // own count of task records says "tasks waiting" and is never the headline.
  const waitingOnYou = (v) => {
    const nodes = arr(v.nodes), waitingIds = new Set(nodes.filter((n) => n.state === "waiting").map((n) => n.id));
    const alone = arr(v.tasks).filter((t) => t.kind !== "unattached" && t.state === "waiting" && !arr(t.workers).some((id) => waitingIds.has(id)));
    return waitingIds.size + alone.length;
  };
  function strip(v, which) {
    const nodes = arr(v.nodes);
    const running = nodes.filter((n) => n.state === "running").length;
    const waiting = waitingOnYou(v);
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
    if (tk === null) { t.textContent = "tokens today \u2014"; t.title = "no session transcript could be read"; } else t.append(el("b", null, tk), " tokens today");
    row.appendChild(t);
    return row;
  }
  // The Agents lens's metrics block (the reference's top-left): a big figure over a small-caps label. A figure with no data is an em dash and
  // one line of why in its title, never a word that reads like a bug. It replaces the inline count row (strip) on this lens; the Work lens keeps its own.
  function metricsStrip(v) {
    const nodes = arr(v.nodes), counts = isObj(v.counts) ? v.counts : {};
    const live = nodes.filter((n) => n.state !== "idle").length;
    const running = nodes.filter((n) => n.state === "running").length, waiting = waitingOnYou(v);
    const row = el("div", "tv-metrics");
    row.setAttribute("role", "group");
    row.setAttribute("aria-label", "Header figures");
    const past = v.replay === true;
    const m = (k, label, fig, why, cls, sub) => {
      const c = el("div", "tv-m" + (cls ? " " + cls : "") + (fig === null ? " dim" : ""));
      c.dataset.k = k;
      c.title = why;
      c.append(el("b", null, fig === null ? "\u2014" : fig), el("span", "tv-ml", label));
      if (sub) c.appendChild(el("i", "tv-msub", sub));
      return c;
    };
    const evN = isNum(counts.events_today) ? counts.events_today : null;
    const tkN = fmtTokens(counts.tokens_today), unread = isNum(counts.tokens_unread) ? counts.tokens_unread : 0;
    const noTk = past ? "not kept for the past: a session's tokens are read from its transcript as it is now"
      : "no session transcript could be read" + (unread ? " (" + unread + " not read)" : "");
    row.append(
      m("sessions", "sessions", live + " / " + nodes.length, live + " live of " + nodes.length + " sessions listed (live = not idle)"),
      m("events", "events", evN === null ? null : fmtTokens(evN) + (counts.events_partial ? "+" : ""),
        evN === null ? (past ? "not kept for the past" : "no session has an event log")
          : "hook events logged since local midnight" + (counts.events_partial ? " (a log was rotated past midnight, so at least this many)" : "")),
      m("running", "running", String(running), "sessions running now"),
      m("waiting", "waiting on you", String(waiting), "sessions waiting on a decision or an answer from you", waiting ? "hot" : ""),
      m("tokens", "tokens today", tkN, tkN === null ? noTk
        : "Fresh tokens since local midnight, from the message timestamps in " + (isNum(counts.tokens_read) ? counts.tokens_read : "the") + " session transcripts: input + output + cache writes (Codex: input - cached + output). Cache reads are not counted.",
        "", unread ? unread + " not read" : ""));
    const conflicts = Array.isArray(v.conflicts) ? v.conflicts : null;
    if (conflicts && conflicts.length) { const n = conflictCount(v); row.appendChild(m("conflicts", n === 1 ? "file conflict" : "file conflicts", String(n), "files two live sessions are both writing", "warn")); }
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
    row.append(stat("tasks", tasks.length, tasks.length === 1 ? "task" : "tasks"), stat("waiting", waitingOnYou(v), "waiting on you", waitingOnYou(v) ? "hot" : ""), stat("tasks-waiting", n("waiting"), n("waiting") === 1 ? "task waiting" : "tasks waiting"));
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
    const sessWait = waitingOnYou(v);
    if (!recs.length) left.appendChild(el("p", "tv-none", sessWait ? "no record is waiting \u00b7 " + sessWait + (sessWait === 1 ? " session is" : " sessions are") + " waiting on you" : "nothing is waiting on you"));
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
  const PHONE_SCALE = 0.75;                // under NARROW_PX the graph keeps this scale and the stage pans sideways, instead of shrinking to unreadable text
  const MIN_LW = 920;                      // the graph is laid out at no less than this width and scaled down to fit a narrower card
  const MIN_H = 540;
  const RING_H = [0.2, 0.34];
  const PARTICLE_CAP = 1500;
  const EDGE_STEP_MAX = 8;                 // particles one edge may spawn in one step (the cap above takes the rest of the way)
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
      f.l3 = (tool || "no tool") + (n.rate_source === "transcript" ? " · rate from transcript" : "");
      if (run !== null) { f.l3Dur = run; f.l3 += " · " + fmtDur(run); }
    }
    f.tag = { waiting: "WAITING ON YOU", errored: "ERRORED", stalled: "STALLED", idle: "IDLE", running: null }[n.state] || null;
    if (n.state === "stalled" && quiet !== null) { f.tag = "STALLED · no event " + fmtDur(quiet); f.tagDur = quiet; }
    return f;
  }
  // the geometry of the graph (pure: positions only, no DOM). `measure(text, font)` returns a pixel width.
  // A seat with more than ROW_CAP lanes wraps into two ranks (row-major in document order, so a subagent stays beside or under its
  // parent) when the card has the room; a narrow card keeps today's single column.
  const ROW_CAP = 7;
  // A seat's block that finds no free row at this height asks for more: the graph is laid out again, taller, until every block has its place.
  function layoutGraph(v, LW, measure) {
    let L = layoutOnce(v, LW, measure, 0);
    for (let i = 0; i < 6 && L.short > 0; i++) L = layoutOnce(v, LW, measure, L.H + L.short);
    return L;
  }
  function layoutOnce(v, LW, measure, hFloor) {
    const nodes = arr(v.nodes), groups = arr(v.groups).slice();
    const gids = new Set(groups.map((g) => g.id));
    if (nodes.some((n) => !gids.has(n.group))) groups.push({ id: null, label: "ungrouped", repo: null, loose: true });   // a node with no group of its own is still drawn
    const groupOf = (n) => (gids.has(n.group) ? n.group : null);
    const mineOf = (g) => nodes.filter((n) => groupOf(n) === g.id);
    const live = groups.filter((g) => mineOf(g).length);
    // each face's width, measured once: the ranks need the widest face of a column before anything is placed
    const tws = new Map();
    for (const n of nodes) {
      const f = faceOf(n, v.as_of);
      let w = Math.max(measure(f.l1, "l1"), measure(f.l2, "l2"), measure(f.l3, "l2"));
      if (f.tag) w = Math.max(w, measure(f.tag, "tag") + 16);
      tws.set(n.id, w + 6);
    }
    const hubX = LW / 2;
    // the widest face in a seat's first rank (the first of each pair in document order), and whether a second rank fits beside it
    const faceWOf = (g, rk) => mineOf(g).reduce((m, n, i) => (i % 2 === rk ? Math.max(m, tws.get(n.id) || 0) : m), 0);
    // room for a second rank: its faces must still end inside the graph. The seat nodes' column draws in toward the hub, as far as 76, to make it.
    // EDGE_PAD is what a face's box runs past its measured text: the offset from its node, the box's own padding, a waiting node's wider
    // offset and a margin to the graph's edge.
    const RANK_GAP = 52, EDGE_PAD = 36;
    const roomFor = (g, col) => hubX - col - (faceWOf(g, 0) + RANK_GAP) - faceWOf(g, 1) - EDGE_PAD >= 0;
    // a seat's lanes stand COL_GAP outward of its node. A subagent steps back toward the node (30 a depth), and its ring must still clear the
    // seat's label plate (half-width + the ring's 17 + 6 of air), so the gap grows with the widest label and the step is capped per seat.
    const lwOf = (g) => Math.max(measure(g.label, "group") + 16, measure("00 agents \u00b7 00 running", "l2")) / 2 + 7;
    // (never past what lets the widest face still end inside the graph beside the nearest column; 120 is the floor)
    // and never past what keeps a seat of more than ROW_CAP lanes in two ranks: that fold is worth more than a wider gap
    const twoRankRoom = live.filter((g) => mineOf(g).length > ROW_CAP).map((g) => Math.floor(hubX - 76 - (faceWOf(g, 0) + faceWOf(g, 1)) - RANK_GAP - EDGE_PAD));
    const COL_GAP = Math.max(120, Math.min(Math.ceil(Math.max(0, ...live.map(lwOf))) + 23 + 30, Math.floor(hubX - 76 - Math.max(0, ...Array.from(tws.values())) - 36), ...twoRankRoom));
    const gxFull = Math.max(76, Math.min(190, LW / 2 - 262 - COL_GAP));
    const tall = live.filter((g) => mineOf(g).length > ROW_CAP);
    let gx0 = gxFull;
    if (tall.length && !tall.every((g) => roomFor(g, gxFull + COL_GAP))) {
      const need = Math.max(...tall.map((g) => faceWOf(g, 0) + faceWOf(g, 1)));
      const tight = Math.max(76, Math.min(gxFull, Math.floor(hubX - need - RANK_GAP - EDGE_PAD - COL_GAP)));
      if (tall.every((g) => roomFor(g, tight + COL_GAP))) gx0 = tight;
    }
    const col0 = gx0 + COL_GAP;
    const ranksOf = (g) => (mineOf(g).length <= ROW_CAP ? 1 : roomFor(g, col0) ? 2 : 1);
    const rowsOf = (g) => Math.ceil(mineOf(g).length / ranksOf(g));
    const order = live.slice().sort((a, b) => rowsOf(b) - rowsOf(a));
    const side = { L: [], R: [] }, load = { L: 0, R: 0 };
    for (const g of order) { const s = load.L <= load.R ? "L" : "R"; side[s].push(g); load[s] += rowsOf(g); }
    const gapOf = (l) => Math.max(0, l.length - 1) * 26;
    // a second rank sits half a pitch lower, so its last row hangs half a pitch below the first rank's
    const halfOf = (l) => (l.some((g) => ranksOf(g) === 2) ? 32 : 0);
    const need = (s) => load[s] * 64 + gapOf(side[s]) + halfOf(side[s]);
    const H = Math.max(MIN_H, hFloor, Math.round(70 + Math.max(need("L"), need("R"))));
    const hub = { x: hubX, y: H / 2 + 6, r: 30 };
    const hubName = isObj(v.hub) && typeof v.hub.label === "string" ? v.hub.label : "";
    const hubHalf = Math.max(measure(hubName, "group"), measure(nodes.length + " sessions \u00b7 " + nodes.length + " running", "l2"), 70) / 2 + 7;
    const pos = new Map(), gpos = new Map();               // keyed by wire id in a Map: an id such as "__proto__" is an id
    const placed = new Set();
    for (const sd of ["L", "R"]) {
      const dir = sd === "L" ? -1 : 1, list = side[sd], gaps = gapOf(list), half = halfOf(list);
      const pitch = Math.max(64, Math.min(86, (H - 70 - gaps - half) / Math.max(1, load[sd])));
      let y = hub.y - (load[sd] * pitch + gaps + half) / 2 + pitch / 2;
      for (const g of list) {
        const mine = mineOf(g), ids = new Set(mine.map((n) => n.id)), ys = [];
        const ranks = ranksOf(g), rank1 = hub.x + dir * col0 + dir * (faceWOf(g, 0) + RANK_GAP);
        let i = 0;
        const put = (n, depth) => {
          if (placed.has(n.id)) return;
          placed.add(n.id);
          const rk = ranks === 2 ? i % 2 : 0, row = ranks === 2 ? Math.floor(i / 2) : i;
          i++;
          const base = rk === 1 ? rank1 : hub.x + dir * col0;
          const x = base - dir * (rk === 1 ? 0 : Math.min(30 * depth, 90, Math.max(0, COL_GAP - lwOf(g) - 23))), ny = y + row * pitch + (rk === 1 ? pitch / 2 : 0);
          pos.set(n.id, { id: n.id, x, y: ny, side: dir, depth, rank: rk, tw: tws.get(n.id) }); ys.push(ny);
          for (const k of mine) if (k.parent === n.id) put(k, depth + 1);
        };
        for (const n of mine) if (!n.parent || !ids.has(n.parent)) put(n, 0);
        for (const n of mine) put(n, 0);                 // a parent cycle has no root: the rest sit at the column's own level
        const gy = ys.reduce((s, q) => s + q, 0) / (ys.length || 1);
        const lw = lwOf(g);
        gpos.set(g.id === null ? "" : g.id, { id: g.id, label: g.label, repo: g.repo, x: hub.x + dir * gx0, y: gy, r: 16, ranks, hw: lw, size: mine.length });
        y += Math.ceil(mine.length / ranks) * pitch + 26;
      }
    }
    const short = settleGroups(gpos, hub, hubHalf, H, !!needsOf(v.hub));
    return { LW, H, hub, gpos, pos, short, gkey: (n) => (gids.has(n.group) ? n.group : "") };
  }
  // A seat's node sits at the middle of its lanes, but its block (the node, then its two label lines) is 72 tall and must not lie on another
  // seat's block, on the hub's label or on the hub: where the middle is taken it slides along its own column to the nearest free row. The
  // biggest seats keep their middle (they have the most lanes tied to it); the smaller ones give way.
  const GROUP_BLOCK_H = 72, BLOCK_GAP = 6;
  function settleGroups(gpos, hub, hubHalf, H, marked) {
    const blockOf = (gp, y) => ({ x: gp.x - gp.hw, y: y - gp.r, w: 2 * gp.hw, h: GROUP_BLOCK_H });
    // a marked desk's label rides HUB_LIFT higher and its callout reaches right and below the hub: the block holds what is drawn
    const up = marked ? HUB_LIFT : 0, right = marked ? Math.max(hubHalf, HUB_CALLOUT_X) : hubHalf, down = marked ? Math.max(hub.r + 16, HUB_CALLOUT_Y) : hub.r + 16;
    const hubBlock = { x: hub.x - hubHalf, y: hub.y - hub.r - 44 - up, w: hubHalf + right, h: 44 + up + hub.r + down };
    const clear = (b, others) => others.every((o) => !(b.x < o.x + o.w + BLOCK_GAP && o.x < b.x + b.w + BLOCK_GAP && b.y < o.y + o.h + BLOCK_GAP && o.y < b.y + b.h + BLOCK_GAP));
    const placed = [hubBlock];
    const lo = 24, hi = Math.max(lo, H - GROUP_BLOCK_H - 8);
    let short = 0;
    for (const gp of Array.from(gpos.values()).sort((a, b) => b.size - a.size || a.y - b.y)) {
      const want = Math.min(hi, Math.max(lo, gp.y));
      let best = null;
      for (let k = 0; k <= Math.ceil(H / 8) && best === null; k++) {
        for (const y of k === 0 ? [want] : [want - k * 8, want + k * 8]) {
          if (y < lo || y > hi) continue;
          if (clear(blockOf(gp, y), placed)) { best = y; break; }
        }
      }
      if (best === null) short += GROUP_BLOCK_H + BLOCK_GAP;
      gp.y = best === null ? want : best;
      placed.push(blockOf(gp, gp.y));
    }
    return short;
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
    for (const [k, gp] of L.gpos) {
      out.push({ kind: "hub", id: k, seat: gp.seat || 0, rate: gRate.get(k) || 0, state: null, e: mkEdge({ x: gp.x, y: gp.y }, { x: L.hub.x, y: L.hub.y }, (gp.x < L.hub.x ? -1 : 1) * 0.1 * (i++ % 2 ? 1 : -1)) });
    }
    for (const n of arr(v.nodes)) {
      const a = L.pos.get(n.id), to = n.parent && L.pos.has(n.parent) && byId.has(n.parent) ? L.pos.get(n.parent) : L.gpos.get(L.gkey(n));
      out.push({ kind: "node", id: n.id, seat: (L.gpos.get(L.gkey(n)) || {}).seat || 0, rate: streams(n) ? n.rate : 0, state: n.state, e: mkEdge({ x: a.x, y: a.y }, { x: to.x, y: to.y }, 0.12 * (a.y < to.y ? 1 : -1)) });
    }
    return out;
  }
  // The boxes the graph draws text and nodes in, in graph coordinates: one place computes them, the drawing and the conflict chip's placement
  // both read them (two pieces of code never restate a label's size). `hub` and each group's label, each node's face, each node's ring.
  const box = (x, y, w, h) => ({ x, y, w, h });
  const nodeSpot = (P) => ({ ang: P.side === 1 ? Math.PI * 0.75 : Math.PI * 0.25 });
  const nodeR = (n) => (n.state === "waiting" ? 11 : 9);
  function graphBoxes(v, L, measure, scale) {
    const nodes = arr(v.nodes), running = nodes.filter((n) => n.state === "running").length, h = L.hub;
    const hsub = nodes.length + (nodes.length === 1 ? " session" : " sessions") + " \u00b7 " + running + " running";
    const hw = Math.max(measure(hsub, "l2"), 70);
    const marked = !!needsOf(v.hub), lift = marked ? HUB_LIFT : 0;
    const out = { hubText: hsub, hub: box(h.x - hw / 2 - 7, h.y - h.r - 42 - lift, hw + 14, 40), hubMark: marked ? [box(h.x - 44, h.y - 44, 88, 88), box(h.x + 42, h.y + 30, HUB_CALLOUT_X - 38, HUB_CALLOUT_Y - 30)] : [], hubBody: box(h.x - h.r - 8, h.y - h.r - 8, 2 * (h.r + 8), 2 * (h.r + 8)), pills: [], groups: new Map(), faces: new Map(), rings: [] };
    if (marked) { const hn = needsOf(v.hub), g = needGeom(hn, h.x, h.y, h.r + 14, Math.PI / 4, scale, HUB_BADGE); out.pills.push(box(g.bx - g.w / 2, g.by - 10, g.w, 20)); out.hubMark.push(box(g.bx - g.side / 2, g.by - g.side / 2, g.side, g.side)); }
    for (const [k, gp] of L.gpos) {
      const mem = nodes.filter((n) => L.gkey(n) === k), run = mem.filter((n) => n.state === "running").length;
      const sub = mem.length + (mem.length === 1 ? " agent" : " agents") + " \u00b7 " + run + " running";
      const lw = Math.max(measure(gp.label, "group") + (gp.seat ? 16 : 0), measure(sub, "l2")), ly = gp.y + gp.r + 18;
      out.groups.set(k, { sub, ly, rect: box(gp.x - lw / 2 - 7, ly - 14, lw + 14, 36) });
    }
    for (const n of nodes) {
      const P = L.pos.get(n.id), f = faceOf(n, v.as_of), dir = P.side, wait = n.state === "waiting";
      const tx = P.x + dir * (wait ? 26 : 17), bw = P.tw + 8, bh = f.tag ? 64 : 46, bx = dir === 1 ? tx - 5 : tx - P.tw - 3;
      out.faces.set(n.id, box(bx, P.y - (f.tag ? 30 : 13), bw, bh));
      out.rings.push(box(P.x - 17, P.y - 17, 34, 34));
      const nn = nodeNeed(n);
      if (nn) { const g = needGeom(nn, P.x, P.y, nodeR(n) + 8, nodeSpot(P).ang, scale); out.pills.push(box(g.bx - g.w / 2, g.by - 10, g.w, 20)); out.rings.push(box(g.bx - g.side / 2, g.by - g.side / 2, g.side, g.side)); }
    }
    return out;
  }
  const overlap = (a, b, pad) => Math.max(0, Math.min(a.x + a.w + pad, b.x + b.w) - Math.max(a.x - pad, b.x)) * Math.max(0, Math.min(a.y + a.h + pad, b.y + b.h) - Math.max(a.y - pad, b.y));
  // a conflict is drawn between the first node on the file and each other one, the file's name on a chip on the first arc. The chip sits on
  // the arc where it covers no label, face or node: the middle first, then along the arc each way; with none free, where it covers least.
  const CHIP_AT = [0.5, 0.4, 0.6, 0.3, 0.7, 0.22, 0.78, 0.15, 0.85];
  const CHIP_OFF = [0, 16, -16, 32, -32];
  // a long path keeps its tail (the file's own name) on the chip, so a chip stays small enough to sit clear of the labels; the title keeps the whole path
  function chipText(path, measure) {
    if (measure(path, "chip") <= 150) return path;
    let tail = path;
    while (tail.length > 4 && measure("…" + tail, "chip") > 150) tail = tail.slice(1);
    return "…" + tail;
  }
  function conflictArcs(v, L, measure, boxes, scale) {
    const out = [], byId = new Map(arr(v.nodes).map((n) => [n.id, n]));
    const bx = boxes || graphBoxes(v, L, measure, scale);
    const obstacles = [bx.hub, bx.hubBody, ...(bx.hubMark || []), ...Array.from(bx.groups.values()).map((g) => g.rect), ...Array.from(bx.faces.values()), ...bx.rings, ...bx.pills];
    for (const c of arr(v.conflicts)) {
      if (!isObj(c) || typeof c.path !== "string") continue;
      const ids = arr(c.nodes).filter((id) => L.pos.has(id) && byId.has(id));
      for (let k = 1; k < ids.length; k++) {
        const A = L.pos.get(ids[0]), B = L.pos.get(ids[k]);
        const mx = (A.x + B.x) / 2, my = (A.y + B.y) / 2, dx = B.x - A.x, dy = B.y - A.y, ln = Math.hypot(dx, dy) || 1;
        const gp = L.gpos.get(L.gkey(byId.get(ids[0]))) || L.hub;
        const sg = ((-dy / ln) * (mx - gp.x) + (dx / ln) * (my - gp.y)) >= 0 ? -1 : 1;
        const cx = mx + (-dy / ln) * 62 * sg, cy = my + (dx / ln) * 62 * sg;
        const at = (t) => ({ x: (1 - t) * (1 - t) * A.x + 2 * t * (1 - t) * cx + t * t * B.x, y: (1 - t) * (1 - t) * A.y + 2 * t * (1 - t) * cy + t * t * B.y });
        let lp = at(0.5);
        if (k === 1) {
          const w = measure(chipText(c.path, measure), "chip") + 16;
          let best = null;
          // on the arc first (the mock's place), then just off it either side: the chip still reads as the arc's
          search: for (const off of CHIP_OFF) {
            for (const t of CHIP_AT) {
              const q = at(t), p = { x: q.x + (-dy / ln) * off, y: q.y + (dx / ln) * off }, r = box(p.x - w / 2, p.y - 10, w, 20);
              const cost = obstacles.reduce((s, o) => s + overlap(r, o, 3), 0) + 40 * bx.pills.reduce((s, o) => s + overlap(r, o, 3), 0);     // a pill outweighs any label: a chip never sits on a badge while a free place exists
              if (best === null || cost < best.cost) best = { cost, p };
              if (cost === 0) break search;
            }
          }
          lp = best.p;
        }
        out.push({ path: c.path, A, B, cx, cy, chip: k === 1, lx: lp.x, ly: lp.y, ids: [ids[0], ids[k]] });
      }
    }
    return out;
  }

  // ---- seat colour: one hue per seat, carried by the group node, its sessions' cores, its edges, and its streams ----
  // A seat's slot comes from the page's one repo-hue table (sky.js hueKey, the same slot the row tags use). Slots 6-8 are tokens a
  // stylesheet may not carry yet: a slot past 5 with no --hue-N falls back to its slot mod 5, so a missing token is a shared hue and
  // never a missing one. A group with no repo of its own (the loose "ungrouped" block) has no seat and stays neutral.
  const HUE_SLOTS = 8, HUE_BASE = 5;
  const hueName = (slot, have8) => "--hue-" + (have8 || slot <= HUE_BASE ? slot : ((slot - 1) % HUE_BASE) + 1);
  // A seat's colour must never read as a state: a slot whose hue lies within SEAT_CLEAR degrees of the waiting, stalled or errored hue (the
  // tokens' own, read here, so a retuned theme is followed) is not given to a seat, and the two biggest seats sit at least BIG_APART apart.
  // A hue the browser cannot report (no oklch) is taken as clear: the colour is then the stylesheet's alone.
  const SEAT_CLEAR = 25, BIG_APART = 40;
  const hueGap = (a, b) => { const d = Math.abs(a - b) % 360; return d > 180 ? 360 - d : d; };
  function tokenHue(name) {
    let raw = "";
    try { raw = root.getComputedStyle(root.document.documentElement).getPropertyValue(name); } catch (_) { return null; }
    const m = /oklch\(\s*[\d.]+%?\s+[\d.]+\s+([\d.]+)/.exec(raw || "");
    return m ? parseFloat(m[1]) : null;
  }
  function seatSlots() {
    const states = ["--accent", "--warn", "--bad"].map(tokenHue).filter((h) => h !== null);
    const hues = new Map();
    const open = [];
    for (let k = 1; k <= HUE_SLOTS; k++) {
      const h = tokenHue("--hue-" + k);
      hues.set(k, h);
      if (h === null || states.every((sh) => hueGap(h, sh) >= SEAT_CLEAR)) open.push(k);
    }
    return { hues, open: open.length >= 2 ? open : Array.from(hues.keys()) };
  }
  // Every seat in one graph gets its own slot (up to the slots open to a seat; past that slots repeat). A seat's preferred slot is its repo's
  // (the slot the row tags use, so a repo-per-seat graph matches its tags) or, with no repo, its label's hash; a preferred slot that reads as
  // a state gives way to the nearest open one. Several seats on one repo (flow's lanes on levain) all prefer the same slot: the first, in
  // group-id order, keeps it and the rest take the next free one. Only real repos are noted into the page's repo table, all before any slot
  // is read: a slot read mid-noting can move.
  function assignSeats(L) {
    const S = root.LevainSky, sky = !!(S && typeof S.hueKey === "function");
    const gps = Array.from(L.gpos.values()).filter((gp) => gp.id !== null && gp.id !== undefined);
    if (sky && typeof S.noteRepo === "function") for (const gp of gps) if (gp.repo) S.noteRepo(gp.repo);
    const { hues, open } = seatSlots();
    const pref = (gp) => {
      const k = sky && gp.repo ? parseInt(S.hueKey(gp.repo), 10) : 0;
      const want = k >= 1 && k <= HUE_SLOTS ? k : (fnv1a(String(gp.label || gp.id)) % HUE_SLOTS) + 1;
      if (open.includes(want)) return want;
      return open.slice().sort((a, b) => ((a - want + HUE_SLOTS) % HUE_SLOTS) - ((b - want + HUE_SLOTS) % HUE_SLOTS))[0];
    };
    const next = (k) => { const i = open.indexOf(k); return open[(i + 1) % open.length]; };
    gps.sort((a, b) => (String(a.id) < String(b.id) ? -1 : String(a.id) > String(b.id) ? 1 : 0));
    const used = new Set(), rest = [];
    for (const gp of L.gpos.values()) gp.seat = 0;
    for (const gp of gps) { const k = pref(gp); if (used.has(k)) rest.push(gp); else { used.add(k); gp.seat = k; } }
    for (const gp of rest) {
      let k = pref(gp);
      for (let t = 0; t < open.length && used.has(k); t++) k = next(k);
      used.add(k); gp.seat = k;
      if (used.size >= open.length) used.clear();                   // past the open slots they go round again
    }
    // the two biggest seats are the two the eye compares first: if their hues sit closer than BIG_APART, the second takes the first open
    // slot that clears the gap (a free one first), keeping every other seat where it is
    const big = gps.slice().sort((a, b) => b.size - a.size).slice(0, 2);
    if (big.length === 2) {
      const [a, b] = big, ha = hues.get(a.seat), hb = hues.get(b.seat);
      if (ha !== null && ha !== undefined && hb !== null && hb !== undefined && hueGap(ha, hb) < BIG_APART) {
        const taken = new Set(gps.filter((g) => g !== b).map((g) => g.seat));
        const fits = (k) => { const h = hues.get(k); return h === null || h === undefined || hueGap(h, ha) >= BIG_APART; };
        const pick = open.filter((k) => fits(k) && !taken.has(k))[0] || open.filter(fits)[0];
        if (pick) b.seat = pick;
      }
    }
  }
  // the attribute that hands an element its seat hue (the rules below turn it into a custom property); none for a neutral one
  function seatAttrs(slot) { return slot ? { "data-seat": String(slot) } : null; }
  // These rules belong to the graph's drawing, so they travel with it, adopted once as a constructable stylesheet: the page's policy
  // (style-src 'self') refuses an inline <style> and a style="" attribute, and a stylesheet built here is neither.
  const GRAPH_CSS = [
    '[data-seat="1"] { --seat: var(--hue-1); } [data-seat="2"] { --seat: var(--hue-2); } [data-seat="3"] { --seat: var(--hue-3); } [data-seat="4"] { --seat: var(--hue-4); }',
    '[data-seat="5"] { --seat: var(--hue-5); } [data-seat="6"] { --seat: var(--hue-6, var(--hue-1)); } [data-seat="7"] { --seat: var(--hue-7, var(--hue-2)); } [data-seat="8"] { --seat: var(--hue-8, var(--hue-3)); }',
    ".tv-graph .tv-edge[data-seat]:not(.waiting) { stroke: color-mix(in oklch, var(--seat) 35%, transparent); stroke-width: 1px; opacity: 1; }",
    ".tv-graph .tv-ggroup[data-seat] .tv-gbody { fill: color-mix(in oklch, var(--seat) 22%, var(--base)); stroke: var(--seat); stroke-width: 1.5px; }",
    ".tv-graph .tv-ggroup[data-seat].waiting .tv-gbody { stroke: var(--accent-line); }",
    ".tv-graph .tv-gdot { fill: var(--seat); }",
    ".tv-graph .tv-gnode[data-seat]:not(.waiting) .tv-core { fill: var(--seat); opacity: 1; }",
    ".tv-graph .tv-face1, .tv-graph .tv-face2, .tv-graph .tv-face3, .tv-graph .tv-gname, .tv-graph .tv-gsub, .tv-graph .tv-gnode:not(.waiting) .tv-facetag { paint-order: stroke; stroke: var(--base); stroke-width: 3px; stroke-linejoin: round; }",
    // a label's plate stands over the streams and the other seats' edges that run beneath it, so no line is drawn through
    // text; its edge is feathered by a soft shadow of the same base colour, so it reads as a pool of quiet, not a block
    ".tv-graph .tv-facebg { fill-opacity: .84; } .tv-graph .tv-lblbg { fill-opacity: .88; }",
    ".tv-graph .tv-facebg, .tv-graph .tv-lblbg { filter: drop-shadow(0 0 6px var(--base)); }",
    ".tv-graph .tv-gnode:focus-visible .tv-facebg { stroke: var(--accent); stroke-width: 2; }",
    ".tv-graph .tv-gring { opacity: .5; }",
    // the theme token sets the seat glow's strength (a light stage takes less)
    ".tv-glow { --_na: calc(var(--glow-a, .35) * .4 + .07); pointer-events: none; }",
    ".tv-glow .tv-neb stop { stop-color: var(--seat, var(--meta)); stop-opacity: 0; } .tv-glow .tv-neb stop.c { stop-opacity: var(--_na); }",
    ".tv-glow .tv-neb stop.m { stop-opacity: calc(var(--_na) * .35); }",
    "@media (prefers-reduced-motion: reduce) { .tv-graph .tv-edge.live { stroke-dasharray: 5 4; stroke-linecap: round; opacity: 1; }",
    "  .tv-graph .tv-edge.live[data-seat]:not(.waiting) { stroke: var(--seat); stroke-opacity: .8; } }",
    "html.calm .tv-graph .tv-edge.live { stroke-dasharray: 5 4; stroke-linecap: round; opacity: 1; }",
    "html.calm .tv-graph .tv-edge.live[data-seat]:not(.waiting) { stroke: var(--seat); stroke-opacity: .8; }",
  ].join("\n");
  let graphSheet = null;
  function ensureGraphCss() {
    const d = root.document;
    if (!d || graphSheet || typeof root.CSSStyleSheet !== "function" || !("adoptedStyleSheets" in d)) return;
    try {
      const sh = new root.CSSStyleSheet();
      sh.replaceSync(GRAPH_CSS);
      d.adoptedStyleSheets = Array.from(d.adoptedStyleSheets).concat([sh]);
      graphSheet = sh;
    } catch (_) { /* no constructable sheets: the graph is drawn with the page's own rules, seat hue left out */ }
  }

  // ---- the seat glow: a faint pool of each seat's hue behind its lanes, generated from the topology value alone ----
  // Drawn once per layout (zero per-frame cost). It carries no stars and no lines: nothing behind the graph may run through a label.
  let skySeq = 0;                                                         // gradient ids stay unique across redraws
  function fnv1a(str) {
    let h = 2166136261;
    for (let i = 0; i < str.length; i++) { h ^= str.charCodeAt(i); h = Math.imul(h, 16777619) >>> 0; }
    return h >>> 0;
  }
  function drawGlow(v, L) {
    const g = sv("g", { "aria-hidden": "true" }, "tv-glow");
    const nodes = arr(v.nodes);
    const defs = sv("defs"), nebs = sv("g");
    const seq = ++skySeq;
    let gi = 0;
    for (const [k, gp] of L.gpos) {
      if (!gp.seat) continue;
      gi++;
      const ps = [gp].concat(nodes.filter((n) => L.gkey(n) === k).map((n) => L.pos.get(n.id)).filter(Boolean));
      const x0 = Math.min(...ps.map((p) => p.x)), x1 = Math.max(...ps.map((p) => p.x)), y0 = Math.min(...ps.map((p) => p.y)), y1 = Math.max(...ps.map((p) => p.y));
      const gid = "tvneb-" + seq + "-" + gi;
      const gr = sv("radialGradient", Object.assign({ id: gid }, seatAttrs(gp.seat)), "tv-neb");
      gr.appendChild(sv("stop", { offset: "0" }, "c"));
      gr.appendChild(sv("stop", { offset: "0.55" }, "m"));
      gr.appendChild(sv("stop", { offset: "1" }));
      defs.appendChild(gr);
      nebs.appendChild(sv("ellipse", { cx: num((x0 + x1) / 2), cy: num((y0 + y1) / 2), rx: num((x1 - x0) / 2 + 150), ry: num((y1 - y0) / 2 + 90), fill: "url(#" + gid + ")" }));
    }
    g.appendChild(defs);
    g.appendChild(nebs);
    return g;
  }

  // ---- the SVG ----
  function drawGraph(v, L, edges, measure, opts, scale, fresh) {
    fresh = fresh || { prev: new Map(), still: true };
    fresh.pulse = new Map(); pulseCtx = fresh;
    const shown = new Map();                                                // what this draw puts on screen, committed by the lens once it is the visible build
    fresh.shown = shown;
    const svg = sv("svg", { viewBox: "0 0 " + L.LW + " " + L.H, role: "group", "aria-label": "Agents graph", preserveAspectRatio: "xMidYMid meet" }, "tv-graph");
    const nodes = arr(v.nodes), byId = new Map(nodes.map((n) => [n.id, n]));
    const lost = lostMap(v), bxs = graphBoxes(v, L, measure, scale);
    svg.appendChild(drawGlow(v, L));
    for (const k of RING_H) svg.appendChild(sv("ellipse", { cx: num(L.hub.x), cy: num(L.hub.y), rx: num(L.LW * k * 1.15), ry: num(L.H * k * 1.05) }, "tv-gring"));
    const eg = sv("g", null, "tv-edges");
    for (const ed of edges) {
      const live = ed.rate > 0, waiting = ed.kind === "node" && ed.state === "waiting";
      const p = sv("path", Object.assign({ d: edgePath(ed.e), "data-edge": ed.kind + ":" + ed.id, "data-rate": live ? String(ed.rate) : "none" }, seatAttrs(ed.seat)), "tv-edge" + (live ? " live" : "") + (ed.kind === "hub" ? " hub" : "") + (waiting ? " waiting" : "") + (!live && !waiting ? " still" : ""));
      eg.appendChild(p);
    }
    svg.appendChild(eg);
    // file conflicts: the arc runs under the plates, but its chip (the file's name) is drawn after the nodes, so a plate never hides it
    const chips = [];
    for (const a of conflictArcs(v, L, measure, bxs, scale)) {
      svg.appendChild(sv("path", { d: "M" + num(a.A.x) + " " + num(a.A.y) + " Q" + num(a.cx) + " " + num(a.cy) + " " + num(a.B.x) + " " + num(a.B.y), "data-conflict": a.path }, "tv-confline"));
      if (a.chip) {
        const g = sv("g", { "data-conflict": a.path, "data-nodes": a.ids.join(",") }, "tv-gconf");
        const label = chipText(a.path, measure), w = measure(label, "chip") + 16;
        g.appendChild(sv("rect", { x: num(a.lx - w / 2), y: num(a.ly - 10), width: num(w), height: 20, rx: 6 }, "tv-confchip"));
        g.appendChild(sv("text", { x: num(a.lx), y: num(a.ly + 4), "text-anchor": "middle" }, "tv-conftext", label));
        if (label !== a.path) g.appendChild(sv("title", null, null, a.path));
        chips.push(g);
      }
    }
    // the hub: the install, "N sessions · M running"
    const running = nodes.filter((n) => n.state === "running").length, h = L.hub;
    const hubLabel = isObj(v.hub) && typeof v.hub.label === "string" ? v.hub.label : "";
    const under = sv("g", null, "tv-needunder"), outers = [];            // the badges' tap pads, beneath the hub and every node; their pills, above them all
    svg.appendChild(under);
    const hg = sv("g", { "data-hub": isObj(v.hub) && typeof v.hub.id === "string" ? v.hub.id : "" }, "tv-ghub");
    hg.appendChild(sv("circle", { cx: num(h.x), cy: num(h.y), r: h.r + 8 }, "tv-hubhalo"));
    hg.appendChild(sv("circle", { cx: num(h.x), cy: num(h.y), r: h.r }, "tv-hubbody"));
    hg.appendChild(sv("circle", { cx: num(h.x), cy: num(h.y), r: 9 }, "tv-hubcore" + (running ? " run" : "")));
    const hsub = bxs.hubText, hr = bxs.hub;
    const hubNeed = needsOf(v.hub), lift = hubNeed ? HUB_LIFT : 0;          // a marked desk's label rides a few px higher so the mark never touches it
    hg.appendChild(sv("rect", { x: num(hr.x), y: num(hr.y), width: num(hr.w), height: hr.h, rx: 7 }, "tv-lblbg"));
    hg.appendChild(sv("text", { x: num(h.x), y: num(h.y - h.r - 26 - lift), "text-anchor": "middle" }, "tv-hubname", hubLabel));
    hg.appendChild(sv("text", { x: num(h.x), y: num(h.y - h.r - 10 - lift), "text-anchor": "middle" }, "tv-hubsub", hsub));
    const hubMark = hubNeed ? needMark(hubNeed, h.x, h.y, h.r + 14, Math.PI / 4, opts, hubLabel || "the desk", "hub", scale, hubDeco(hubNeed, h.x, h.y, !fresh.still && fresh.prev.get("hub") !== hubNeed.n), false) : null;
    if (hubNeed) shown.set("hub", hubNeed.n);
    if (hubMark) hg.appendChild(hubMark.inner);
    svg.appendChild(hg);
    if (hubMark && hubMark.under) under.appendChild(hubMark.under);
    if (hubMark && hubMark.outer) outers.push(hubMark.outer);
    // the groups: "N agents · M running"
    for (const [k, gp] of L.gpos) {
      const mem = nodes.filter((n) => L.gkey(n) === k);
      const run = mem.filter((n) => n.state === "running").length, wait = mem.some((n) => n.state === "waiting");
      const gg = sv("g", Object.assign({ "data-group": k }, seatAttrs(gp.seat)), "tv-ggroup" + (wait ? " waiting" : ""));
      gg.appendChild(sv("circle", { cx: num(gp.x), cy: num(gp.y), r: gp.r }, "tv-gbody"));
      gg.appendChild(sv("circle", { cx: num(gp.x), cy: num(gp.y), r: 4 }, "tv-gcore" + (run ? " run" : "")));
      const bg = bxs.groups.get(k), sub = bg.sub, ly = bg.ly, gr = bg.rect;
      gg.appendChild(sv("rect", { x: num(gr.x), y: num(gr.y), width: num(gr.w), height: gr.h, rx: 7 }, "tv-lblbg"));
      gg.appendChild(sv("text", { x: num(gp.x), y: num(ly), "text-anchor": "middle" }, "tv-gname", gp.label));
      if (gp.seat) gg.appendChild(sv("circle", { cx: num(gp.x - measure(gp.label, "group") / 2 - 8), cy: num(ly - 4), r: 3 }, "tv-gdot"));
      gg.appendChild(sv("text", { x: num(gp.x), y: num(ly + 15), "text-anchor": "middle" }, "tv-gsub", sub));
      svg.appendChild(gg);
    }
    // the nodes, each with its face
    const tickers = [];
    for (const n of nodes) {
      const P = L.pos.get(n.id), f = faceOf(n, v.as_of), dir = P.side, wait = n.state === "waiting", r = wait ? 11 : 9;
      const g = sv("g", Object.assign({ "data-node": n.id, "data-state": n.state }, seatAttrs((L.gpos.get(L.gkey(n)) || {}).seat)), "tv-gnode " + n.state);
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
      const fr = bxs.faces.get(n.id);
      g.appendChild(sv("rect", { x: num(fr.x), y: num(fr.y), width: num(fr.w), height: fr.h, rx: 6 }, "tv-facebg"));
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
      // every node is a keyboard stop: Enter or Space opens its page as a click does (agentsLens binds the keys)
      g.setAttribute("role", "button");
      g.setAttribute("tabindex", "0");
      g.setAttribute("aria-label", (typeof n.label === "string" && n.label ? n.label : n.id || "a seat") + ", " + (NODE_WORDS[n.state] || n.state) + ": open its page");
      if (wait && typeof n.card === "string" && n.card && opts && typeof opts.openCard === "function") {
        g.classList.add("tv-open");
        g.setAttribute("aria-label", (typeof n.label === "string" && n.label ? n.label : n.id || "a seat") + ", waiting on you: open its card");
        g.dataset.card = n.card;
        const go = () => opts.openCard(n.card);
        g.addEventListener("click", go);
        g.addEventListener("keydown", (ev) => { if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); go(); } });
      }
      const need = nodeNeed(n);
      const owner = "node:" + n.id, prevN = fresh.prev.get(owner);
      const mark = need ? needMark(need, P.x, P.y, r + 8, dir === 1 ? Math.PI * 0.75 : Math.PI * 0.25, opts, n.label, owner, scale, undefined, fresh.still || !(prevN === undefined || need.n > prevN)) : null;
      if (need) shown.set(owner, need.n);
      if (mark) g.appendChild(mark.inner);
      svg.appendChild(g);
      if (mark && mark.under) under.appendChild(mark.under);
      if (mark && mark.outer) outers.push(mark.outer);
    }
    for (const g of chips) svg.appendChild(g);
    for (const o of outers) svg.appendChild(o);                              // the badges last: where a chip still overlaps one, the badge wins the tap
    return { svg, tickers };
  }

  // the hover card: the whole record of a node, local counts only
  function tipRows(n, asOf) {
    const run = fmtDur(span(n.started, asOf)), tk = fmtTokens(n.tokens);
    const rows = [["state", NODE_WORDS[n.state] || n.state], ["agent", n.agent || "unknown"], ["model", n.model || "model unknown"],
                  ["tokens", tk === null ? "tokens unknown" : tk + " tok"], ["tool", typeof n.tool === "string" && n.tool ? n.tool : "none"],
                  ["run time", run || "unknown"], ["last event", clock(n.last_event) || "unknown"],
                  ["rate", isNum(n.rate) ? n.rate.toFixed(1) + " events/min" + (n.rate_source === "transcript" ? " (from the transcript)" : "") : "rate unknown (no event log, no readable transcript)"]];
    if (typeof n.card === "string" && n.card) rows.push(["waiting on", n.card]);
    return rows;
  }

  // ---- the particles: a canvas under the graph, drawn only while some edge streams ----
  // Each particle wears its edge's seat hue. On a dark stage a particle is a pre-rendered glow sprite drawn additively
  // ("lighter" sums colour: light that glows); on a light stage additive light washes out, so it is a solid dot drawn plainly.
  // The sprites are built per hue and rebuilt only when the theme or a hue token changes (no shadowBlur: that costs per draw).
  let probeCtx = null;
  function cssRGB(str) {                                                  // any CSS colour the browser reads -> [r, g, b]
    if (!probeCtx) { const c = document.createElement("canvas"); c.width = c.height = 1; probeCtx = c.getContext("2d", { willReadFrequently: true }); }
    if (!probeCtx) return [153, 153, 153];
    probeCtx.clearRect(0, 0, 1, 1);
    probeCtx.fillStyle = "#999";
    probeCtx.fillStyle = String(str);
    probeCtx.fillRect(0, 0, 1, 1);
    const d = probeCtx.getImageData(0, 0, 1, 1).data;
    return [d[0], d[1], d[2]];
  }
  function mkSprite(rgb, dark, glowA) {
    const m = 2, c = document.createElement("canvas");                    // a 16-unit box at 2x, so a particle stays sharp on a dense screen
    c.width = c.height = 16 * m;
    const x = c.getContext("2d");
    if (!x) return c;
    const col = (a) => "rgba(" + rgb[0] + "," + rgb[1] + "," + rgb[2] + "," + a + ")";
    if (dark) {
      const gr = x.createRadialGradient(8 * m, 8 * m, 0, 8 * m, 8 * m, 8 * m);
      gr.addColorStop(0, col(1)); gr.addColorStop(1.5 / 8, col(1)); gr.addColorStop(3 / 8, col(glowA)); gr.addColorStop(1, col(0));
      x.fillStyle = gr;
      x.fillRect(0, 0, 16 * m, 16 * m);
    } else {
      x.fillStyle = col(0.9);
      x.beginPath(); x.arc(8 * m, 8 * m, 1.6 * m, 0, Math.PI * 2); x.fill();
    }
    return c;
  }
  function startStreams(canvas, stage, edges, getScale, still) {
    const calmOn = () => !!(root.document && root.document.documentElement && root.document.documentElement.classList && root.document.documentElement.classList.contains("calm"));   // the page's Calm control: the same as the OS asking
    const reduced = !!(root.matchMedia && root.matchMedia("(prefers-reduced-motion: reduce)").matches);
    const live = still ? [] : edges.filter((e) => e.rate > 0);        // a frame from the ledger draws no stream
    stage.dataset.streams = String(live.length);
    stage.dataset.particles = "0";
    stage.dataset.motion = reduced || still || calmOn() ? "off" : "on";
    if (reduced || !live.length || !canvas.getContext || typeof root.requestAnimationFrame !== "function") return null;
    const ctx = canvas.getContext("2d");
    if (!ctx) return null;
    const PART = [];
    let quality = 1, ema = 16.7, last = 0, n = 0, stopped = false, raf = 0;
    let sprites = new Map(), spriteKey = "", dark = true;
    const refresh = () => {                                               // theme and hue tokens, re-read now and then: the toggle has no event for the canvas
      const cs = root.getComputedStyle(canvas);
      const base = cssRGB(cs.getPropertyValue("--base") || "#000");
      const isDark = (0.2126 * base[0] + 0.7152 * base[1] + 0.0722 * base[2]) / 255 < 0.5;
      const have8 = cs.getPropertyValue("--hue-6").trim() !== "";
      const glowRaw = parseFloat(cs.getPropertyValue("--glow-a")), glowA = Number.isFinite(glowRaw) && glowRaw > 0 ? glowRaw : 0.35;
      const names = new Map();
      for (const e of live) names.set(e.seat || 0, e.seat ? cs.getPropertyValue(hueName(e.seat, have8)).trim() : cs.color);
      const key = [isDark, glowA, ...Array.from(names.entries()).map((kv) => kv.join("="))].join("|");
      if (key === spriteKey) return;
      spriteKey = key; dark = isDark; sprites = new Map();
      for (const [slot, str] of names) sprites.set(slot, mkSprite(cssRGB(str || "#999"), isDark, glowA));
    };
    const step = (dt) => {
      for (const ed of live) {
        // bounded BEFORE it accumulates: a finite wire rate can scale to Infinity, and an unbounded accumulator is an unbounded loop
        const inc = ed.rate * (ed.kind === "hub" ? 5 : 10) * quality * dt;
        ed.acc = Math.min(EDGE_STEP_MAX, (ed.acc || 0) + (Number.isFinite(inc) ? Math.max(0, inc) : EDGE_STEP_MAX));
        for (let spawned = 0; ed.acc >= 1 && spawned < EDGE_STEP_MAX; spawned++) { ed.acc -= 1; PART.push({ ed, t: 0, sp: 0.22 + Math.random() * 0.22, off: Math.random() + Math.random() - 1, k: Math.random(), hub: ed.kind === "hub" }); }
      }
      for (let i = PART.length - 1; i >= 0; i--) { const p = PART[i]; p.t += p.sp * dt * (p.ed.e.len > 200 ? 0.8 : 1.15); if (p.t >= 1) PART.splice(i, 1); }
      const cap = Math.round(PARTICLE_CAP * quality);
      if (PART.length > cap) PART.splice(0, PART.length - cap);
    };
    const draw = () => {
      const s = getScale(), dpr = canvas.width / Math.max(1, canvas.clientWidth || canvas.width);
      ctx.setTransform(dpr * s, 0, 0, dpr * s, 0, 0);
      ctx.globalCompositeOperation = "source-over";
      ctx.clearRect(0, 0, canvas.width / (dpr * s) + 1, canvas.height / (dpr * s) + 1);
      if (dark) ctx.globalCompositeOperation = "lighter";
      for (const p of PART) {
        const e = p.ed.e, pt = bez(e, p.t), spread = (p.hub ? 7 : 4.2) * (0.25 + p.t) * p.off;
        const sp = sprites.get(p.ed.seat || 0);
        if (!sp) continue;
        const d = dark ? 1.6 + 1.4 * p.k : 2.0 + 1.2 * p.k;                // the core's diameter: dark 1.6-3.0, light 2.0-3.2
        const w = 16 * d / (dark ? 3.0 : 3.2);
        // additive light sums: a dense edge at full alpha burns to a white bar, so a dark stage draws each particle at about half
        ctx.globalAlpha = Math.pow(Math.sin(Math.PI * Math.min(1, p.t)), 0.7) * (dark ? (p.hub ? 0.36 : 0.5) : (p.hub ? 0.62 : 0.8));
        ctx.drawImage(sp, pt.x + e.nx * spread - w / 2, pt.y + e.ny * spread - w / 2, w, w);
      }
      ctx.globalAlpha = 1;
      ctx.globalCompositeOperation = "source-over";
    };
    const loop = (ts) => {
      if (stopped) return;
      if (!canvas.isConnected) { stopped = true; return; }
      if (calmOn()) { park(); return; }
      const dt = Math.min(0.05, last ? (ts - last) / 1000 : 0.016);
      last = ts;
      ema = ema * 0.9 + dt * 1000 * 0.1;                                  // frame-time adaptive: long frames thin the stream
      if (ema > 24) quality = Math.max(0.15, quality * 0.93); else if (ema < 18) quality = Math.min(1, quality * 1.01);
      if (n % 60 === 0) refresh();
      step(dt);
      draw();
      if (++n % 20 === 0) { stage.dataset.particles = String(PART.length); stage.dataset.quality = quality.toFixed(2); }
      raf = root.requestAnimationFrame(loop);
    };
    // Calm turned on mid-stream: the loop stops and the canvas clears; turned off again, the same loop resumes from a flowing stream
    const park = () => { raf = 0; PART.length = 0; stage.dataset.motion = "off"; stage.dataset.particles = "0"; ctx.setTransform(1, 0, 0, 1, 0, 0); ctx.clearRect(0, 0, canvas.width, canvas.height); };
    const onCalm = () => {
      if (stopped || calmOn() || raf) return;
      stage.dataset.motion = "on"; last = 0;
      for (let i = 0; i < 260; i++) step(0.03);
      stage.dataset.particles = String(PART.length);
      raf = root.requestAnimationFrame(loop);
    };
    if (typeof root.addEventListener === "function") root.addEventListener("levain-calm", onCalm);
    refresh();
    if (calmOn()) return () => { stopped = true; if (typeof root.removeEventListener === "function") root.removeEventListener("levain-calm", onCalm); };   // drawn under Calm: no loop until the control turns it off
    for (let i = 0; i < 260; i++) step(0.03);                             // the stream is already flowing when the lens opens
    stage.dataset.particles = String(PART.length);
    raf = root.requestAnimationFrame(loop);
    return () => { stopped = true; if (typeof root.removeEventListener === "function") root.removeEventListener("levain-calm", onCalm); if (raf && root.cancelAnimationFrame) root.cancelAnimationFrame(raf); PART.length = 0; };
  }

  let pinned = null;                                                      // the fallback pin (no agent page in the page): {id, x, y, dx, dy} in stage px
  let kbFocus = null;                                                     // the control the keyboard focus sits on ("n:<node id>", or "b:hub" / "b:node:<node id>" for a need badge (the owner is typed, so a node whose id is "hub" is not the desk)), kept across a redraw
  function agentsLens(v, opts) {
    release();
    const wrap = el("div", "tv-agents");
    // what the desk's mark last showed: undefined = it just arrived, a number = the count it had (a rebuild at the same data replays nothing)
    const still = !!(opts && opts.still);
    const fresh = { prev: new Map(needSeen), still, shown: null };
    const stage = el("div", "tv-stage");
    const hint = el("span", "tv-hint", "Click a node for its page. Hover for the face.");
    stage.appendChild(hint);
    const canvas = el("canvas", "tv-particles");
    canvas.setAttribute("aria-hidden", "true");
    stage.appendChild(canvas);
    const tip = el("div", "tv-tip");
    tip.hidden = true;
    tip.setAttribute("aria-hidden", "true");
    wrap.appendChild(stage);
    let LW = 0, svgNode = null, scale = 1, built = false;
    // keyboard focus follows its control across every rebuild of the graph (the card's own redraw, and a resize that redraws it after): the key is
    // "n:<node id>", or "b:hub" / "b:node:<node id>" for a need badge (the owner is typed, so a node whose id is "hub" is not the desk); pending holds the key until a rebuilt element takes the focus
    let pending = kbFocus && (kbFocus.startsWith("b:") || arr(v.nodes).some((n) => n.id === kbFocus.slice(2))) ? kbFocus : null;
    const focusKey = (t) => { const bd = t && t.closest ? t.closest("[data-needbadge][tabindex]") : null, g = t && t.closest ? t.closest("[data-node]") : null;
                              return bd ? "b:" + bd.getAttribute("data-needfor") : g ? "n:" + g.dataset.node : null; };
    const restoreFocus = () => {
      if (!pending || typeof root.requestAnimationFrame !== "function") return;
      root.requestAnimationFrame(() => {
        const d = root.document;
        if (!pending || !stage.isConnected) return;
        if (d.activeElement && d.activeElement !== d.body) { pending = null; return; }
        const isBadge = pending.startsWith("b:"), key = pending.slice(2);
        const g = svgNode && (isBadge ? Array.from(svgNode.querySelectorAll("[data-needbadge][tabindex]")).find((x) => x.getAttribute("data-needfor") === key)
                                      : Array.from(svgNode.querySelectorAll("[data-node]")).find((x) => x.dataset.node === key));
        if (g && g.focus) g.focus({ preventScroll: true });
        else if (kbFocus === pending) kbFocus = null;          // its control is gone from this build: forget it, so a later rebuild that brings it back does not take the focus
        pending = null;
      });
    };
    let reanchorPin = null;                                               // set once the card helpers exist: re-seats a pinned card on its node after every layout
    const mctx = document.createElement("canvas").getContext ? document.createElement("canvas").getContext("2d") : null;
    const build = (W) => {
      const cs = root.getComputedStyle(stage);
      const fonts = { l1: "600 12px " + (cs.getPropertyValue("--round").trim() || "sans-serif"), l2: "500 11px " + (cs.getPropertyValue("--mono").trim() || "monospace"),
                      tag: "700 10px " + (cs.getPropertyValue("--round").trim() || "sans-serif"), group: "700 13px " + (cs.getPropertyValue("--round").trim() || "sans-serif"),
                      chip: "600 11px " + (cs.getPropertyValue("--mono").trim() || "monospace") };
      const measure = (text, k) => { if (mctx) { mctx.font = fonts[k]; return mctx.measureText(String(text)).width; } return String(text).length * 6.4; };
      const phone = W < NARROW_PX;
      LW = phone ? MIN_LW : Math.max(MIN_LW, Math.round(W));
      scale = phone ? PHONE_SCALE : Math.min(1, W / LW);
      stage.classList.toggle("tv-phone", phone);
      ensureGraphCss();
      const L = layoutGraph(v, LW, measure);
      assignSeats(L);
      const edges = graphEdges(v, L);
      hint.textContent = phone ? "Swipe sideways to pan. Tap a node for its page." : "Click a node for its page. Hover for the face.";
      if (streamStop) { streamStop(); streamStop = null; }
      if (svgNode && root.document.activeElement && svgNode.contains(root.document.activeElement)) pending = focusKey(root.document.activeElement) || pending;
      if (svgNode) svgNode.remove();
      const g = drawGraph(v, L, edges, measure, opts, scale, fresh);
      svgNode = g.svg;
      // only a build the person can see (attached, with a width) has played its motion: it REPLACES the seen snapshot with what it showed, so a mark that
      // vanished is forgotten and one that returns arrives anew; a hidden or pre-attach build commits nothing, and the lens still plays when first shown
      if (fresh.shown && stage.isConnected && stage.getBoundingClientRect().width > 0) {
        fresh.prev = new Map(fresh.shown);
        if (!still) { needSeen.clear(); for (const [k, n] of fresh.shown) needSeen.set(k, n); pulseAt.clear(); for (const [k, r] of fresh.pulse) pulseAt.set(k, r); }
      }
      if (phone) svgNode.style.width = Math.round(LW * scale) + "px";
      stage.insertBefore(svgNode, canvas);
      // the canvas keeps the graph's own proportions: CSS size follows the SVG, the backing store follows the device
      const cssW = phone ? Math.round(LW * scale) : Math.round(W), cssH = Math.round(L.H * scale), dpr = Math.min(2, root.devicePixelRatio || 1);
      canvas.width = Math.max(1, Math.round(cssW * dpr)); canvas.height = Math.max(1, Math.round(cssH * dpr));
      canvas.style.width = cssW + "px"; canvas.style.height = cssH + "px";
      if (ticker) { root.clearInterval(ticker); ticker = null; }
      if (g.tickers.length && !(opts && opts.still)) {          // a frame from the ledger is still: its durations are computed at T and do not advance
        const t0 = (root.performance && root.performance.now ? root.performance.now() : Date.now());
        const nowMs = () => (root.performance && root.performance.now ? root.performance.now() : Date.now());
        ticker = root.setInterval(() => {
          if (!stage.isConnected) { root.clearInterval(ticker); ticker = null; return; }
          const d = nowMs() - t0;
          for (const t of g.tickers) t.el.textContent = t.text(t.ms + d);
        }, 20000);
      }
      streamStop = startStreams(canvas, stage, edges, () => scale, !!(opts && opts.still));
      stage.dataset.nodes = String(arr(v.nodes).length);
      built = true;
      if (reanchorPin) reanchorPin();
      restoreFocus();
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
    // a click opens the node's page (the dashboard's modal); a page without the dashboard shows the card at the click point instead
    const showTip = (n, x, y) => {
      if (tip.dataset.node !== n.id) {
        tip.dataset.node = n.id;
        while (tip.firstChild) tip.removeChild(tip.firstChild);
        tip.appendChild(el("b", null, n.label));
        for (const [k, val] of tipRows(n, v.as_of)) { const r = el("div"); r.append(el("span", "tv-tl", k), val); tip.appendChild(r); }
      }
      tip.hidden = false;
      const place = () => {
        const box = stage.getBoundingClientRect();
        tip.style.left = Math.max(4, Math.min(Math.max(box.width, stage.scrollWidth) - tip.offsetWidth - 4, x + 16)) + "px";
        tip.style.top = Math.max(4, Math.min(box.height - tip.offsetHeight - 4, y + 14)) + "px";
      };
      // a pin restored by the live redraw runs before the new stage is in the page: measured then, the card clamps to the corner,
      // so it is placed on the next frame, once the stage has its size
      if (stage.isConnected || typeof root.requestAnimationFrame !== "function") place();
      else root.requestAnimationFrame(() => { if (stage.isConnected && tip.dataset.node === n.id && !tip.hidden) place(); });
    };
    const hideTip = () => { tip.hidden = true; tip.dataset.node = ""; };
    stage.addEventListener("pointermove", (ev) => {
      if (pinned) return;
      const g = nodeOf(ev.target), n = g ? byId.get(g.dataset.node) : null;
      if (!n) { tip.hidden = true; return; }
      const box = stage.getBoundingClientRect();
      showTip(n, ev.clientX - box.left + stage.scrollLeft, ev.clientY - box.top);
    });
    stage.addEventListener("pointerleave", () => { if (!pinned) hideTip(); });
    const syncPins = () => {
      if (!svgNode) return;
      for (const g of svgNode.querySelectorAll("[data-node]")) g.setAttribute("aria-pressed", pinned && pinned.id === g.dataset.node ? "true" : "false");
    };
    // the one pin/let-go move, for a click and for a key: the same node again, or no node, lets go
    // a node's ring centre in stage px, or null while it has no box (the stage is not in the page yet)
    const ringCentre = (id) => {
      const g = svgNode && Array.from(svgNode.querySelectorAll("[data-node]")).find((e) => e.dataset.node === id);
      if (!g) return null;
      const rc = (g.querySelector(".tv-ring") || g).getBoundingClientRect(), box = stage.getBoundingClientRect();
      return rc.width ? { x: rc.left + rc.width / 2 - box.left + stage.scrollLeft, y: rc.top + rc.height / 2 - box.top } : null;
    };
    const toggle = (n, x, y) => {
      if (!n || (pinned && pinned.id === n.id)) { pinned = null; hideTip(); }
      else {
        const c = ringCentre(n.id);
        pinned = { id: n.id, x, y, dx: c ? x - c.x : 0, dy: c ? y - c.y : 0 };   // where the card sits relative to its node, kept across layouts
        showTip(n, x, y);
      }
      syncPins();
    };
    // the one open move, for a click and for a key. A waiting node with a needs card keeps its own handler (opts.openCard).
    // With the dashboard's agent page present a click opens it (K13-7); a page without the dashboard falls back to pinning the card.
    const openPage = (g, n, x, y) => {
      if (!n) { toggle(null); return; }
      if (g.classList.contains("tv-open")) return;
      // a seat's own node names its seat to the page, which offers a message box addressed to it: the graph passes the full roster name,
      // which is its GROUP's label (the node's own label is only the tail). Only the seat's own node does: its lanes sit in the seat's group
      // with that node as parent, and a label shared by two groups names no one seat. Each of those opens its page with no box.
      const grp = arr(v.groups).find((q) => q && q.id === n.group);
      const own = n.parent == null || n.parent === "";
      const unique = grp && typeof grp.label === "string" && arr(v.groups).filter((q) => q && typeof q.label === "string" && q.label.trim() === grp.label.trim()).length === 1;
      const seat = own && unique ? grp.label.trim() : "";
      const A = root.LevainAgentPage;
      if (A && typeof A.open === "function") { hideTip(); A.open({ id: n.id, label: n.label, rows: tipRows(n, v.as_of), session: typeof n.session === "string" ? n.session : "", seat: seat }, g); }
      else toggle(n, x, y);
    };
    // the card stays on its node through every layout (a live relayout moves the node, a resize moves the whole graph), not only a stage replacement
    reanchorPin = () => {
      if (!pinned || !stage.isConnected) return;
      const c = ringCentre(pinned.id), n = byId.get(pinned.id);
      if (!c || !n) return;
      pinned.x = c.x + (pinned.dx || 0); pinned.y = c.y + (pinned.dy || 0);
      showTip(n, pinned.x, pinned.y);
    };
    stage.addEventListener("click", (ev) => {
      const g = nodeOf(ev.target), n = g ? byId.get(g.dataset.node) : null;
      const box = stage.getBoundingClientRect();
      openPage(g, n, ev.clientX - box.left + stage.scrollLeft, ev.clientY - box.top);
    });
    // keyboard: a focused node opens its page on Enter or Space as a click does
    stage.addEventListener("keydown", (ev) => {
      if (ev.key !== "Enter" && ev.key !== " ") return;
      const g = nodeOf(ev.target), n = g ? byId.get(g.dataset.node) : null;
      if (!n) return;
      ev.preventDefault();
      const ring = g.querySelector(".tv-ring") || g, rc = ring.getBoundingClientRect(), box = stage.getBoundingClientRect();
      openPage(g, n, rc.left + rc.width / 2 - box.left + stage.scrollLeft, rc.top + rc.height / 2 - box.top);
    });
    // a redraw replaces the focused node: keyboard focus follows the id to its new element (focus that leaves while the node is still there clears it)
    stage.addEventListener("focusin", (ev) => { const k = focusKey(ev.target); if (k) kbFocus = k; });
    stage.addEventListener("focusout", (ev) => { const t = ev.target; root.setTimeout(() => { if (t.isConnected && !focusKey(root.document.activeElement)) kbFocus = null; }, 0); });
    stage.appendChild(tip);
    if (pinned && byId.has(pinned.id)) { showTip(byId.get(pinned.id), pinned.x, pinned.y); root.requestAnimationFrame ? root.requestAnimationFrame(() => reanchorPin && reanchorPin()) : null; } else pinned = null;
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

  // ---- Replay (K10 S8b): the page half of the topology's past read (`GET /cockpit/panel/topology.json?at=T`) ----
  // The strip is drawn only when the caller hands `opts.replay` (the panel head said `replay: true`). T is minute-aligned and bounded by the
  // LIVE read's as_of (the server's clock, never the browser's): [as_of - 14 h, as_of]. A scrub fetches on RELEASE (a keyboard step after a
  // quiet 250 ms), with at most one past read in flight (a newer one aborts the older), and never with an empty T. While T is set the topology's
  // live refetch is paused (`paused()`, read by cockpit_view.refetchPanel) and the badge says so; Live (or Esc) drops T and resumes it.
  // What the past read could not rebuild (`unreadable`) is drawn as unknown. The histogram is the activity panel's events bucketed by `at`.
  const WINDOW_MS = 14 * 3600e3, STEP_MS = 5 * 60e3, MIN_MS = 60e3, KEY_MS = 250, LO_MARGIN_MS = 2 * MIN_MS, HIST_STALE_MS = 60e3, RETURN_MS = 10e3;
  const UNKNOWN_WORDS = { spores: "open loops", lost_if_closed: "what closing would lose", context: "model and tokens", session_logs: "rate and state from the session logs",
                          conflicts: "file conflicts", decisions: "decisions", questions: "questions" };
  // Two times are kept apart. `at` is the REQUESTED T (set when a scrub goes out; non-null = the live refetch is paused); `cat` is the COMMITTED T (the
  // T whose answer is `value`). The badge says "live paused" only over `value` with cat === at; while a read is out it says "loading" and the
  // frame drawn (the previous committed one, or the live value) is named. `tok` is a monotonic request token: an answer whose token is not the
  // latest is dropped (no AbortController needed). `ret`: Live was asked and the frame stays, labelled, until the live read lands.
  const R = { at: null, cat: null, value: null, err: "", busy: false, ret: false, anchor: null, anchoredAt: 0, tok: 0, rtok: 0, ctl: null, timer: null, pend: null, rtimer: null,
              refocus: false, keyed: false, opts: null, cur: null, hist: null, histAt: 0, histBusy: false, escBound: false };
  const mono = () => (root.performance && typeof root.performance.now === "function" ? root.performance.now() : Date.now());
  const minuteOf = (ms) => Math.floor(ms / MIN_MS) * MIN_MS;
  const isoMin = (ms) => new Date(minuteOf(ms)).toISOString().replace(/\.\d{3}Z$/, "Z");
  // paused only while the strip that owns the replay is on the page: a strip that is gone (the card was replaced, the tab left) pauses nothing
  function paused() { return R.at !== null && !!(R.cur && R.cur.bars && R.cur.bars.isConnected); }
  function replaying() { return R.at !== null || R.ret; }
  function replayValue() { return replaying() ? R.value : null; }
  const replaySig = () => [R.at, R.cat, R.err, R.busy, R.ret];
  function abortFlight() { R.tok++; if (R.ctl) { try { R.ctl.abort(); } catch (_) { /* gone */ } R.ctl = null; } if (R.timer) { root.clearTimeout(R.timer); R.timer = null; } if (R.rtimer) { root.clearTimeout(R.rtimer); R.rtimer = null; } R.pend = null; }
  function clearReplay() { R.at = null; R.cat = null; R.value = null; R.anchor = null; R.busy = false; R.err = ""; R.ret = false; R.keyed = false; R.refocus = false; R.rtok++; if (R.rtimer) { root.clearTimeout(R.rtimer); R.rtimer = null; } }
  // the live read behind the paused one: the page's refetch (cockpit_view.refetchPanel). Resolves {ok: true} once the live value is drawn ("applied", or
  // "fault", which draws the fault as live does), else {ok: false, msg}: a rejection, a throw, a `false` (the board was redrawn under the read) or a
  // "superseded" that no newer read settled. A superseded read is followed by asking again (the newer owner is superseded in turn), at most 3 times.
  function resumeLive() {
    const rp = R.opts && R.opts.replay;
    if (!rp || typeof rp.resume !== "function") return Promise.resolve({ ok: true });
    const msgOf = (e) => (e && e.message ? e.message : String(e));
    const attempt = (n) => {
      let p;
      try { p = rp.resume(); } catch (e) { return Promise.resolve({ ok: false, msg: msgOf(e) }); }
      if (!p || typeof p.then !== "function") return Promise.resolve({ ok: true });
      return p.then((res) => {
        if (res === false) return { ok: false, msg: "the live read was not drawn" };
        if (res === "superseded") return n < 3 ? attempt(n + 1) : { ok: false, msg: "the live read was superseded" };
        return { ok: true };
      }, (e) => ({ ok: false, msg: msgOf(e) }));
    };
    return attempt(1);
  }
  // ONE way back to the live topology (K10 fix7): the Live pill, Back to live, Esc, a Work<->Agents lens switch and the retry after a failure all call it.
  // The committed frame (if any) stays drawn, labelled "returning to live…", while the live read goes out (bounded by RETURN_MS); replay is cleared only
  // when that read is drawn ("applied", or "fault", which draws as live draws it). A false / rejection / timeout keeps the frame, labelled "could not
  // return to live — Live tries again"; calling this again, while returning or failed, starts a new attempt (a new token). With no committed frame (a first
  // scrub still loading) the scrub is cancelled by the token bump and the live value drawn is the one that was never replaced: live never left.
  function returnToLive() {
    abortFlight();
    const rt = ++R.rtok;
    if (R.rtimer) { root.clearTimeout(R.rtimer); R.rtimer = null; }
    // R.anchor stays until the live read lands: a scrub made while returning is windowed by the live clock, never by the historical frame's as_of
    R.at = null; R.busy = false; R.err = ""; R.ret = true; R.keyed = false; R.refocus = false;
    const settle = (res) => {
      if (R.rtok !== rt || !R.ret) return;
      if (R.rtimer) { root.clearTimeout(R.rtimer); R.rtimer = null; }
      if (res.ok) clearReplay();
      else R.err = "could not return to live: " + res.msg + " (Live tries again)";   // the frame stays, labelled; Live asks again
      if (R.opts && R.opts.repaint) R.opts.repaint();
    };
    R.rtimer = root.setTimeout(() => { R.rtimer = null; settle({ ok: false, msg: "the live read timed out" }); }, RETURN_MS);
    if (R.opts && R.opts.repaint) R.opts.repaint();
    resumeLive().then(settle);
  }
  // teardown only: the view went away (tab left, no card, list mode, replay flag gone). Nothing is drawn, so nothing is kept; every other exit is returnToLive.
  function leaveReplay(silent) {
    if (!silent) { if (replaying() || R.err) returnToLive(); return; }
    abortFlight();
    if (R.rtimer) { root.clearTimeout(R.rtimer); R.rtimer = null; }
    const had = R.at !== null;
    if (!had && !R.ret && R.err === "") return;
    clearReplay();
    if (had) resumeLive();
  }
  // one past read: T goes out once, the answer is kept only if no newer request superseded it (the token), and the frame already committed keeps its
  // own label while this one loads. A 400 that names the window re-anchors on a fresh live read and retries once.
  function replayTo(T, anchor, opt) {
    const o = R.opts, rp = o && o.replay;
    if (!rp || typeof rp.readJson !== "function" || !isNum(T)) return;
    abortFlight();
    const tok = ++R.tok, keyed = !!(opt && opt.keyed);
    R.rtok++; R.ret = false;
    R.err = ""; R.at = T; R.busy = true; R.keyed = keyed; R.refocus = keyed;
    if (R.anchor === null) { R.anchor = anchor; R.anchoredAt = mono(); }
    const ctl = typeof root.AbortController === "function" ? new root.AbortController() : null;
    R.ctl = ctl;
    if (o.repaint) o.repaint();
    const fail = (msg) => {
      if (R.tok !== tok) return;
      R.ctl = null; R.busy = false;
      R.err = "replay refused: " + msg;
      if (R.value !== null && R.cat !== null) R.at = R.cat;                // back to the frame that is committed
      else { R.at = null; R.value = null; R.cat = null; R.anchor = null; resumeLive(); }
      R.refocus = keyed;
      if (o.repaint) o.repaint();
    };
    const reanchor = (msg) => {
      // the page's estimate of the server clock drifted: one fresh live read, then the same ask again inside the real window
      rp.readJson("/cockpit/panel/topology.json?profile=full", ctl ? ctl.signal : undefined).then((pl) => {
        if (R.tok !== tok) return;
        const a = isObj(pl) && isObj(pl.value) ? Date.parse(pl.value.as_of) : NaN;
        if (!isNum(a)) { fail(msg); return; }
        R.anchor = a; R.anchoredAt = mono();
        const hi = minuteOf(a), lo = hi - WINDOW_MS + LO_MARGIN_MS;
        if (T >= hi) { fail(msg); return; }
        replayTo(Math.max(T, lo), a, { keyed, retried: true });
      }, (e) => { if (e && e.name === "AbortError") return; fail(msg); });
    };
    let p;
    try { p = rp.readJson("/cockpit/panel/topology.json?profile=full&at=" + encodeURIComponent(isoMin(T)), ctl ? ctl.signal : undefined); } catch (e) { fail(e && e.message ? e.message : String(e)); return; }
    p.then((pl) => {
      if (R.tok !== tok) return;                                          // superseded: it changes nothing
      const val = isObj(pl) ? pl.value : null;
      if (!isObj(val) || !Array.isArray(val.nodes) || !Array.isArray(val.tasks) || !Array.isArray(val.groups) || !isObj(val.hub)) { fail("the past read was not a topology"); return; }
      R.ctl = null; R.busy = false; R.value = val; R.cat = T; R.refocus = keyed;
      if (o.repaint) o.repaint();
    }, (e) => {
      if (e && e.name === "AbortError") return;
      const msg = e && e.message ? e.message : String(e);
      if (R.tok === tok && !(opt && opt.retried) && /inside the last/.test(msg)) { reanchor(msg); return; }
      fail(msg);
    });
  }
  // activity per bucket, from the activity panel's events (the sky's own source). Read only for a strip that is on the page and visible, and only when
  // the data is older than a minute (never per repaint); the strip is not yet attached when render returns, so the look is taken after it is.
  function ensureHist() {
    root.setTimeout(() => {
      const rp = R.opts && R.opts.replay, c = R.cur;
      if (!rp || typeof rp.readJson !== "function" || R.histBusy || !c || !c.bars.isConnected) return;
      if (c.bars.offsetParent === null || (root.document && root.document.hidden)) return;
      if (R.hist !== null && (R.at !== null || Date.now() - R.histAt < HIST_STALE_MS)) return;
      R.histBusy = true; R.histAt = Date.now();
      rp.readJson("/cockpit/panel/activity.json?profile=full").then((pl) => {
        const val = isObj(pl) ? pl.value : null;
        R.hist = isObj(val) && Array.isArray(val.events) ? { events: val.events.filter(isObj).map((e) => Date.parse(e.at)).filter(isNum), unreadable: arr(val.unreadable).filter((x) => typeof x === "string") }
                                                          : { fail: "no activity source" };
      }, () => { R.hist = { fail: "activity not read" }; }).then(() => { R.histBusy = false; R.histAt = Date.now(); paintBars(R.cur); });
    }, 0);
  }
  function paintBars(cur, fresh) {
    if (!cur || (!fresh && !cur.bars.isConnected)) return;
    const h = R.hist, n = cur.bars.children.length;
    cur.note.textContent = "";
    cur.bars.classList.toggle("unk", !h || !!h.fail || h.unreadable.length > 0);
    if (!h) return;
    if (h.fail) { cur.note.textContent = h.fail; return; }
    if (h.unreadable.length) cur.note.textContent = "activity not read: " + h.unreadable.join(", ");
    const counts = new Array(n).fill(0), lo = cur.hi - WINDOW_MS;
    for (const t of h.events) { if (t >= lo && t <= cur.hi) counts[Math.min(n - 1, Math.floor((t - lo) / WINDOW_MS * n))]++; }
    const top = Math.max(1, ...counts);
    counts.forEach((c, i) => { cur.bars.children[i].style.height = (c ? 3 + 24 * c / top : 2) + "px"; cur.bars.children[i].classList.toggle("on", c > 0); });
  }
  // Esc ends a replay only when it is meant for the card: focus inside the topology card, or no dialog / overlay / palette open on the page.
  // A text field, a select and a contenteditable keep their own Esc.
  function bindEsc() {
    if (R.escBound || !root.document || !root.document.addEventListener) return;
    R.escBound = true;
    root.document.addEventListener("keydown", (ev) => {
      if (ev.key !== "Escape" || (!replaying() && !R.err) || ev.defaultPrevented) return;
      const t = ev.target, tag = t && t.tagName;
      if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT" || (t && t.isContentEditable)) return;
      const d = root.document, inCard = [t, d.activeElement].some((n) => n && n.closest && n.closest(".k7-card, .tv"));
      // checkVisibility takes the visibility option only where the browser reads it (probed with a getter); anywhere else the computed style is asked
      let vp = false;
      try { d.documentElement.checkVisibility({ get visibilityProperty() { vp = true; return true; } }); } catch (_) { vp = false; }
      const shown = (n) => (vp ? n.checkVisibility({ visibilityProperty: true })
        : n.getClientRects().length > 0 && !(root.getComputedStyle && ["hidden", "collapse"].includes(root.getComputedStyle(n).visibility)));
      if (!inCard && Array.from(d.querySelectorAll(".modal-overlay, dialog[open], [role=\"dialog\"], [aria-modal=\"true\"]")).some(shown)) return;
      returnToLive();
    });
  }
  // the two-hour marks on the LOCAL clock: the first even local hour at or after lo, stepping a local hour at a time (any zone, any offset)
  function hourMarks(lo, hi) {
    const out = [], d = new Date(lo);
    d.setMinutes(0, 0, 0);
    if (d.getTime() < lo) d.setHours(d.getHours() + 1);
    for (let k = 0; d.getTime() <= hi && k < 40; k++) {
      if (d.getHours() % 2 === 0 && d.getMinutes() === 0) out.push({ ms: d.getTime(), label: String(d.getHours()).padStart(2, "0") + ":00" });
      d.setHours(d.getHours() + 1);
    }
    return out;
  }
  function replayStrip(v) {
    // the server's clock, estimated: the live as_of (kept as the anchor once a scrub is out) plus the page's own monotonic time since it was read.
    // The browser's wall clock is never used, and the window does not freeze: the floor and the ceiling move with the estimate.
    const A = R.anchor !== null ? { t: R.anchor, at: R.anchoredAt } : { t: Date.parse(v.as_of), at: mono() };
    if (!isNum(A.t)) return null;
    const est = () => A.t + (mono() - A.at);
    const hi = minuteOf(est());
    const lo0 = hi - WINDOW_MS;
    const loNow = () => minuteOf(est()) - WINDOW_MS + LO_MARGIN_MS;
    const strip = el("div", "tv-replay");
    const cap = el("div", "tv-rcap");
    cap.append(el("b", null, "Replay"), el("span", null, "last 14 h, from the ledger"));
    const track = el("div", "tv-rtrack");
    track.tabIndex = 0;
    track.setAttribute("role", "slider");
    track.setAttribute("aria-label", "Replay time, last 14 hours");
    track.setAttribute("aria-valuemin", String(lo0)); track.setAttribute("aria-valuemax", String(hi));
    const bars = el("div", "tv-rbars");
    for (let i = 0; i < 168; i++) bars.appendChild(el("i"));
    track.appendChild(bars);
    hourMarks(lo0, hi).forEach((m, i) => {
      const h = el("span", "tv-rhr" + (i % 2 ? " tv-rhr-alt" : ""), m.label);   // a narrow strip keeps every other label (the stylesheet hides the alternates)
      h.style.left = ((m.ms - lo0) / WINDOW_MS * 100) + "%";
      track.appendChild(h);
    });
    const head = el("div", "tv-rhead" + (R.at === null ? " live" : ""));
    const tlabel = el("span", "tv-rtime");
    head.appendChild(tlabel);
    const place = (ms) => { head.style.left = ((ms - lo0) / WINDOW_MS * 100) + "%"; tlabel.textContent = clock(new Date(ms).toISOString()); head.classList.toggle("live", ms >= hi); };
    place(R.pend !== null ? R.pend : R.at !== null ? R.at : hi);
    track.appendChild(head);
    const pill = el("button", "tv-live");
    pill.type = "button";
    pill.setAttribute("aria-pressed", R.at === null ? "true" : "false");
    pill.append(el("i", "tv-livedot"), "Live");
    pill.addEventListener("click", () => { if (replaying() || R.err) returnToLive(); });
    // T from a pointer, minute-aligned and inside the window; the right edge is live
    const tAt = (clientX) => { const r = track.getBoundingClientRect(); const k = Math.max(0, Math.min(1, (clientX - r.left) / Math.max(1, r.width))); return minuteOf(lo0 + k * WINDOW_MS); };
    const commit = (T, keyed) => { if (T >= hi) { leaveReplay(false); return; } replayTo(Math.max(loNow(), T), est(), { keyed }); };
    let down = false;
    track.addEventListener("pointerdown", (ev) => { down = true; track.classList.add("drag"); try { track.setPointerCapture(ev.pointerId); } catch (_) { /* no capture */ } place(tAt(ev.clientX)); });
    track.addEventListener("pointermove", (ev) => { if (down) place(tAt(ev.clientX)); });
    track.addEventListener("pointerup", (ev) => { if (!down) return; down = false; track.classList.remove("drag"); commit(tAt(ev.clientX), false); });
    track.addEventListener("pointercancel", () => { down = false; track.classList.remove("drag"); place(R.at !== null ? R.at : hi); });
    track.addEventListener("keydown", (ev) => {
      const dir = ev.key === "ArrowLeft" || ev.key === "ArrowDown" ? -1 : ev.key === "ArrowRight" || ev.key === "ArrowUp" ? 1 : 0;
      if (ev.key === "Escape") { ev.preventDefault(); leaveReplay(false); return; }
      if (!dir && ev.key !== "Home" && ev.key !== "End") return;
      ev.preventDefault();
      const lo = loNow(), base = R.pend !== null ? R.pend : R.at !== null ? R.at : hi;
      const T = ev.key === "Home" ? lo : ev.key === "End" ? hi : Math.max(lo, Math.min(hi, base + dir * STEP_MS));
      R.pend = T; place(T);
      if (R.timer) root.clearTimeout(R.timer);
      R.timer = root.setTimeout(() => { R.timer = null; const t = R.pend; R.pend = null; if (t !== null) commit(t, true); }, KEY_MS);
    });
    const note = el("span", "tv-rnote");
    strip.append(cap, track, pill, note);
    if (R.busy) cap.lastChild.textContent = "reading the past…";
    if (R.err) { const e = el("p", "tv-rerr", R.err); e.setAttribute("role", "status"); strip.appendChild(e); }
    R.cur = { bars, note, hi };
    if (R.refocus) { R.refocus = false; root.setTimeout(() => { if (track.isConnected) track.focus({ preventScroll: true }); }, 0); }
    paintBars(R.cur, true);
    return strip;
  }
  // what the past read could not rebuild: said in words, never drawn as none
  function unknownLines(v) {
    const names = arr(v.unreadable).filter((x) => typeof x === "string" && x);
    if (v.replay !== true || !names.length) return [];
    return names.map((nm) => el("p", "tv-unk", (UNKNOWN_WORDS[nm] || nm) + ": not kept for the past"));
  }

  // ---- the body ----
  // v: the topology value. opts: {repaint(): redraw the card, openCard(id): open the Needs-you row a waiting item links to,
  // replay: {readJson(url, signal), resume()} when the panel head says `replay: true`}
  function render(v, opts) {
    const o0 = opts || {};
    R.opts = o0;
    const which = o0.lens === "work" ? "work" : "agents";       // the header's choice, handed in; the Event graph is not drawn here
    // a frame from the ledger: a value the server marked `replay`, or the value this module committed. It is STILL (no stream, no ticker, no breathe)
    // and, when it is drawn through the strip, it carries its badge.
    const past = isObj(v) && (v.replay === true || (R.value !== null && v === R.value));
    const o = past ? Object.assign({}, o0, { still: true }) : o0;
    const w = el("div", "tv");
    w.dataset.lens = which;
    if (past) w.classList.add("tv-replaying");
    if (o.replay && R.at !== null) w.dataset.rat = isoMin(R.at);
    if (o.replay && replaying()) w.dataset.rshown = past && R.cat !== null ? isoMin(R.cat) : "live";
    const bar = el("div", "tv-bar");
    bar.append(o0.lensSeg || el("span"), which === "agents" ? metricsStrip(v) : stripWork(v));
    w.appendChild(bar);
    const conf = conflictList(v);
    if (conf) w.appendChild(conf);
    const body = el("div", "tv-lensbody");
    body.appendChild(which === "agents" ? agentsLens(v, o) : workLens(v, o));
    // the paused line, said plainly where the graph is (the mock: top of the Agents stage, foot of the Work stage)
    if (o.replay && replaying()) {
      const at = (ms) => clock(new Date(ms).toISOString());
      // requested is not committed: "live paused" only over the value whose as_of is T; while a read is out the badge says what is drawn
      const text = R.ret && R.err ? "could not return to live — Live tries again" + (past && R.cat !== null ? " · showing replay " + at(R.cat) : "")
        : R.ret ? "returning to live…"
        : R.busy ? "loading replay " + at(R.at) + "… " + (past && R.cat !== null ? "showing replay " + at(R.cat) : "showing live")
        : "replay " + at(R.at) + " — live paused";
      const b = el("div", "tv-rbadge", text);
      b.setAttribute("role", "status");
      const wrapB = el("div", "tv-rbadgewrap");
      wrapB.append(b);
      if (!R.ret || R.err) {
        const back = el("button", "tv-rback", "Back to live");
        back.type = "button";
        back.addEventListener("click", returnToLive);
        wrapB.append(back);
      }
      body.appendChild(wrapB);
    }
    w.appendChild(body);
    w.appendChild(which === "agents" ? legendAgents() : legend());
    const unk = unknownLines(v);
    for (const u of unk) w.appendChild(u);
    const q = unk.length ? null : quietLine(v);        // the unknown lines already say what the past could not rebuild
    if (q) w.appendChild(q);
    if (o.replay) {
      bindEsc();
      const s = replayStrip(v);
      if (s) { w.appendChild(s); ensureHist(); }
    }
    return w;
  }

  root.LevainTopologyView = { headerLens, setHeaderLens, headerToggle, hasWork, quietLine, signature, release, render, renderLost, renderLostFault, paused, waitingOnYou, replaying, replayValue, returnToLive, leaveReplay, hourMarks };
  if (typeof module !== "undefined" && module.exports) module.exports = root.LevainTopologyView;
})(typeof globalThis !== "undefined" ? globalThis : this);

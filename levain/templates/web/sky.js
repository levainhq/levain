/* sky.js: the cockpit's ambient panel. Typed events (kind, at, repo, agent, outcome) drawn as a rolling
   window of rows by time, with new commits and failed reviews arriving as meteors.

     LevainSky.mount(el, value, opts) -> { update(value), destroy() }
       value  {events: [{kind, at, repo, agent, outcome}], legend}   (the `ambient` kind's contract)
       opts   windowHours (14)   topRows (5)   pinnedRows ([])   single (a repo name, or auto)
              ambient (true)     now (() => ms; a replay supplies its own clock)

   Design rules this file keeps:
   - The motion is the data itself, never a mascot of it: between events nothing moves.
   - The frame loop runs only while a meteor is in flight. Meteor state is derived from the clock, so any
     draw is correct however few frames the machine gives us.
   - Colour comes only from CSS custom properties (--base, --accent, --contrast and their derivatives), so the
     theme owns it; the fallbacks below are used only when a property is absent. */
(function () {
  'use strict';

  var FLY = 600, LIFE = 750;        // ms to land / ms until gone
  var NOW_AT = 0.85;                // where "now" sits across the plot
  var TICK_MS = 30000;              // the window slides on a timer, not on frames
  var ROW_H_MIN = 26, PAD_T = 28, PAD_B = 12, PAD_R = 24, GUTTER_PAD = 16, GUTTER_MIN = 56;
  var FONT_MONO = '11px ui-monospace, "SF Mono", Menlo, monospace';
  var FONT_SEAT = '600 11.5px ui-rounded, -apple-system, sans-serif';

  // Diagnostics for a test driver: how many animation frames are scheduled right now, and how many ran.
  var stats = { pending: 0, frames: 0 };

  function hash01(n) { var x = Math.sin(n * 12.9898) * 43758.5453; return x - Math.floor(x); }
  function pad2(n) { return (n < 10 ? '0' : '') + n; }
  function hhmm(ms) { var d = new Date(ms); return pad2(d.getHours()) + ':' + pad2(d.getMinutes()); }
  function el(tag, attrs, text) {
    var n = document.createElement(tag);
    if (attrs) for (var k in attrs) n.setAttribute(k, attrs[k]);
    if (text != null) n.textContent = text;
    return n;
  }

  // An `at` with no offset is UTC, as the kernel reads it; Date.parse would take it as browser-local.
  var NAIVE = /^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(:\d{2}(\.\d+)?)?$/;
  function parseAt(at) {
    var s = String(at);
    return Date.parse(NAIVE.test(s) ? s.replace(' ', 'T') + 'Z' : s);
  }

  function normalise(value) {
    var out = [];
    var src = (value && value.events) || [];
    for (var i = 0; i < src.length; i++) {
      var e = src[i], t = parseAt(e.at);
      if (!e || isNaN(t) || !e.kind) continue;
      out.push({ t: t, kind: String(e.kind), repo: e.repo == null ? null : String(e.repo),
        agent: e.agent == null ? null : String(e.agent), ok: e.outcome !== 'failed',
        key: e.at + '|' + e.kind + '|' + e.repo + '|' + e.agent + '|' + e.outcome });
    }
    out.sort(function (a, b) { return a.t - b.t; });
    return out;
  }

  // Which events are new since the last feed. `state` is {seen: {key: count}, loaded}. Equal events are
  // counted as occurrences, so a second identical event is new; the first feed is history, however empty.
  function diffFresh(evs, state) {
    var cnt = {}, fresh = [];
    evs.forEach(function (e) {
      cnt[e.key] = (cnt[e.key] || 0) + 1;
      if (cnt[e.key] > (state.seen[e.key] || 0) && state.loaded) fresh.push(e);
    });
    for (var k in cnt) if (cnt[k] > (state.seen[k] || 0)) state.seen[k] = cnt[k];
    state.loaded = true;
    return fresh;
  }

  // A repo-less commit or review belongs to no single repo, so the single-repo view leaves it out.
  function offView(e, single) { return single && e.repo !== single && (e.kind === 'commit' || e.kind === 'review'); }

  // Which rows, and which row each event lands on. Pure: no DOM, so it is testable under node.
  function buildModel(events, opts, nowMs) {
    var start = nowMs - opts.windowHours * 3600000;
    var inWin = events.filter(function (e) { return e.t >= start && e.t <= nowMs; });
    var repos = {};
    inWin.forEach(function (e) { if (e.repo && (e.kind === 'commit' || e.kind === 'review')) repos[e.repo] = (repos[e.repo] || 0) + 1; });
    var names = Object.keys(repos);
    var single = opts.single || (names.length <= 1 && !opts.pinnedRows.length ? (names[0] || '') : '');
    var rows, rowOf, folded = [], noRepo = 0;
    if (single) {
      rows = [{ id: 'commit', label: 'commits' }, { id: 'review', label: 'reviews' }, { id: 'memory', label: 'memory' }, { id: 'decisions', label: 'decisions' }];
      rowOf = function (e) { return e.kind === 'commit' ? 'commit' : e.kind === 'review' ? 'review' : e.kind === 'decision' ? 'decisions' : 'memory'; };
    } else {
      var main = opts.pinnedRows.length ? opts.pinnedRows.slice()
        : names.sort(function (a, b) { return repos[b] - repos[a] || (a < b ? -1 : 1); }).slice(0, opts.topRows);
      rows = main.map(function (r) { return { id: 'r:' + r, label: r }; });
      var mainSet = {}; main.forEach(function (r) { mainSet[r] = 1; });
      var foldedSet = {};
      inWin.forEach(function (e) {
        if (e.kind !== 'commit' && e.kind !== 'review') return;
        if (e.repo && mainSet[e.repo]) return;
        if (e.repo) foldedSet[e.repo] = 1; else noRepo++;
      });
      folded = Object.keys(foldedSet).sort();
      if (folded.length || noRepo) rows.push({ id: 'other', label: 'other', folded: folded, noRepo: noRepo });
      rows.push({ id: 'memory', label: 'memory' }, { id: 'decisions', label: 'decisions' });
      rowOf = function (e) {
        if (e.kind === 'decision') return 'decisions';
        if (e.kind !== 'commit' && e.kind !== 'review') return 'memory';
        return e.repo && mainSet[e.repo] ? 'r:' + e.repo : 'other';
      };
    }
    var counts = { commit: 0, review: 0, failed: 0, memory: 0 };
    inWin.forEach(function (e) {
      if (offView(e, single)) return;
      if (e.kind === 'commit') counts.commit++;
      else if (e.kind === 'review') { counts.review++; if (!e.ok) counts.failed++; }
      else if (e.kind !== 'decision') counts.memory++;
    });
    return { rows: rows, rowOf: rowOf, single: single, counts: counts, start: start, hasDecisions: inWin.some(function (e) { return e.kind === 'decision'; }) };
  }

  function mount(host, value, userOpts) {
    var o = userOpts || {};
    var opts = { windowHours: o.windowHours || 14, topRows: o.topRows || 5, pinnedRows: o.pinnedRows || [], single: o.single || '' };
    var clock = o.now || function () { return Date.now(); };
    var reduce = window.matchMedia ? window.matchMedia('(prefers-reduced-motion: reduce)') : null;
    var ambientOn = o.ambient !== false && !(reduce && reduce.matches);

    var root = el('div', { class: 'sky-panel' });
    root.style.position = 'relative';
    var bar = el('div', { class: 'sky-bar' });
    var summary = el('span', { class: 'sky-summary' });
    var toggle = el('button', { type: 'button', class: 'sky-toggle' });
    bar.append(summary, toggle);
    var plot = el('div', { class: 'sky-plot' });
    plot.style.position = 'relative';
    var cv = el('canvas', { class: 'sky-canvas', role: 'img' });
    cv.style.display = 'block'; cv.style.width = '100%'; cv.style.height = '320px';
    var axis = el('div', { class: 'sky-axis' });
    axis.style.position = 'relative'; axis.style.height = '24px';
    var tip = el('div', { class: 'sky-tip', role: 'status' });
    tip.style.cssText = 'position:absolute;pointer-events:none;display:none;z-index:2;white-space:nowrap;padding:6px 10px;border-radius:10px;font-size:12px;border:1px solid var(--hair-2,#28324a);background:var(--base,#060912);color:var(--contrast,var(--text,#f3f5f9))';
    var showLegend = o.legend !== false;    // a host that draws its own legend passes legend:false
    var legend = el('div', { class: 'sky-legend' });
    legend.hidden = !showLegend;   // the host's own legend stands in; an empty strip would still take its padding
    plot.append(cv, axis, tip);
    root.append(bar, plot, legend);
    host.appendChild(root);

    var g = cv.getContext('2d');
    var W = 0, H = 0, DPR = Math.min(2, window.devicePixelRatio || 1), gutter = GUTTER_MIN;
    var events = [], model = null, meteors = [], feed = { seen: {}, loaded: false }, raf = 0, timer = 0, ro = null, dead = false, hits = [];

    function css(name, fallback) {
      var v = getComputedStyle(root).getPropertyValue(name).trim();
      return v || fallback;
    }
    function palette() {
      return { base: css('--base', '#060912'), accent: css('--accent', '#ff8a3d'), contrast: css('--contrast', css('--text', '#f3f5f9')),
        label: css('--label', '#9ba6ba'), meta: css('--meta', '#7d889c'), hair: css('--hair', '#1c2435'), hair2: css('--hair-2', '#121a2a') };
    }

    function setToggle() {
      toggle.textContent = ambientOn ? 'Ambient on' : 'Ambient paused';
      toggle.setAttribute('aria-pressed', ambientOn ? 'true' : 'false');
      toggle.setAttribute('aria-label', 'Ambient motion, currently ' + (ambientOn ? 'on' : 'paused'));
    }
    toggle.addEventListener('click', function () {
      ambientOn = !ambientOn; setToggle();
      if (!ambientOn) { meteors = []; draw(); }
    });
    function onReduce() { if (reduce && reduce.matches && ambientOn) { ambientOn = false; setToggle(); meteors = []; draw(); } }
    if (reduce && reduce.addEventListener) reduce.addEventListener('change', onReduce);
    setToggle();

    var xOf = function (t, now) { var span = opts.windowHours * 3600000 / NOW_AT; return gutter + (W - gutter - PAD_R) * ((t - (now - opts.windowHours * 3600000)) / span); };
    var rowY = function (id) {
      for (var i = 0; i < model.rows.length; i++) if (model.rows[i].id === id) return PAD_T + (H - PAD_T - PAD_B) * (i + 0.5) / model.rows.length;
      return PAD_T;
    };

    function measureGutter() {
      g.font = FONT_MONO;
      var w = GUTTER_MIN;
      model.rows.forEach(function (r) { w = Math.max(w, g.measureText(r.label).width + GUTTER_PAD * 2); });
      gutter = Math.min(w, Math.max(GUTTER_MIN, W * 0.4));
    }
    function fit(label, maxW) {
      if (g.measureText(label).width <= maxW) return label;
      while (label.length > 1 && g.measureText(label + '…').width > maxW) label = label.slice(0, -1);
      return label + '…';
    }

    function resize() {
      var r = cv.getBoundingClientRect();
      W = r.width; H = r.height;
      if (!W) return;
      cv.width = Math.round(W * DPR); cv.height = Math.round(H * DPR);
      draw(); drawAxis();
    }

    function rebuild() {
      var now = clock();
      model = buildModel(events, opts, now);
      var narrow = host.getBoundingClientRect().width < 520;   // a phone: taller rows and a taller floor, so the plot is usable
      var rowsH = Math.max(narrow ? 240 : 160, PAD_T + PAD_B + model.rows.length * (narrow ? 36 : ROW_H_MIN));
      cv.style.height = rowsH + 'px';
      var c = model.counts;
      summary.textContent = 'Last ' + opts.windowHours + ' hours: ' + c.commit + ' commits, ' + c.review + ' reviews (' + c.failed + ' failed), ' + c.memory + ' memory writes';
      cv.setAttribute('aria-label', summary.textContent + '. ' + (value && value.legend ? value.legend : ''));
      legend.textContent = showLegend && value && value.legend ? value.legend : '';
      var r = cv.getBoundingClientRect(); W = r.width; H = r.height;
      if (W) { measureGutter(); cv.width = Math.round(W * DPR); cv.height = Math.round(H * DPR); }
    }

    function advanceMeteors(now) {
      var clk = clock();
      meteors.forEach(function (m) {
        var p = Math.min(1, (now - m.born) / FLY), e = 1 - Math.pow(1 - p, 3);
        m.tx = xOf(m.e.t, clk); m.ty = rowY(m.row);
        m.x = m.tx + 120 * (1 - e); m.y = m.ty - 48 * (1 - e); m.len = 60 * (1 - e) + 4;
        m.a = Math.max(0, 1 - Math.max(0, now - m.born - FLY) / (LIFE - FLY));
      });
      meteors = meteors.filter(function (m) { return now - m.born < LIFE; });
    }

    function draw() {
      if (model) advanceMeteors(performance.now());   // before the width guard: a hidden mount must still let meteors expire
      if (!model || !W) return;
      var P = palette(), now = clock();
      g.setTransform(DPR, 0, 0, DPR, 0, 0); g.clearRect(0, 0, W, H);
      var nowX = xOf(now, now);
      hits = [];
      g.font = FONT_MONO; g.textAlign = 'right';
      model.rows.forEach(function (r) {
        var y = rowY(r.id);
        g.globalAlpha = 1; g.fillStyle = P.hair; g.fillRect(gutter, y, W - PAD_R - gutter, 1);
        g.fillStyle = P.meta;
        var shown = fit(r.label, gutter - GUTTER_PAD * 2 + 8);
        g.fillText(shown, gutter - GUTTER_PAD, y + 4);
        hits.push({ label: true, row: r, x0: 0, x1: gutter, y: y });
      });
      var h0 = Math.ceil(model.start / 3600000) * 3600000;
      for (var t = h0; t < now; t += 3600000) {
        var x = xOf(t, now); if (x < gutter || x > W - PAD_R) continue;
        g.fillStyle = (new Date(t).getHours() % 3 === 0) ? P.hair : P.hair2; g.fillRect(x, PAD_T - 10, 1, H - PAD_T);
      }
      events.forEach(function (e) {
        if (e.t < model.start || e.t > now) return;
        if (offView(e, model.single)) return;
        var row = model.rowOf(e), x = xOf(e.t, now), y = rowY(row) + (hash01(e.t + e.kind.length) - 0.5) * 12;
        var a = Math.max(0.35, 1 - ((now - e.t) / 3600000) / (opts.windowHours * 1.15));
        if (e.kind === 'commit') { g.globalAlpha = a; g.fillStyle = P.contrast; g.beginPath(); g.arc(x, y, 1.3 + hash01(e.t) * 0.9, 0, 7); g.fill(); }
        else if (e.kind === 'review') { g.globalAlpha = e.ok ? a * 0.8 : 1; g.strokeStyle = e.ok ? P.label : P.accent; g.lineWidth = e.ok ? 1 : 1.5; g.beginPath(); g.arc(x, y, e.ok ? 2.6 : 3.2, 0, 7); g.stroke(); }
        else if (e.kind === 'decision') { g.globalAlpha = 1; g.fillStyle = P.contrast; g.beginPath(); g.moveTo(x, y - 4); g.lineTo(x + 4, y); g.lineTo(x, y + 4); g.lineTo(x - 4, y); g.closePath(); g.fill(); }
        else { g.globalAlpha = a * 0.55; g.fillStyle = P.contrast; g.fillRect(x, y, 1.4, 1.4); }
        g.globalAlpha = 1;
        hits.push({ e: e, x: x, y: y });
      });
      if (!model.hasDecisions) { g.fillStyle = P.meta; g.textAlign = 'left'; g.fillText('none in this window', gutter + 8, rowY('decisions') + 4); }
      var grad = g.createLinearGradient(0, PAD_T - 10, 0, H);
      grad.addColorStop(0, 'transparent'); grad.addColorStop(0.25, P.accent); grad.addColorStop(1, P.accent);
      g.globalAlpha = 0.5; g.fillStyle = grad; g.fillRect(nowX, PAD_T - 10, 1.5, H - PAD_T + 10); g.globalAlpha = 1;
      meteors.forEach(function (m) {
        var tail = g.createLinearGradient(m.x, m.y, m.x + m.len, m.y - m.len * 0.4);
        tail.addColorStop(0, m.fail ? P.accent : P.contrast); tail.addColorStop(1, 'transparent');
        g.globalAlpha = m.a; g.strokeStyle = tail; g.lineWidth = 1.4; g.beginPath(); g.moveTo(m.x, m.y); g.lineTo(m.x + m.len, m.y - m.len * 0.4); g.stroke(); g.globalAlpha = 1;
      });
    }

    function drawAxis() {
      if (!model || !W) return;
      var now = clock(), nowX = xOf(now, now);
      axis.replaceChildren();
      var h0 = Math.ceil(model.start / 3600000) * 3600000;
      // label every Nth hour, N the smallest of 2/3/4/6/12 that leaves ~56px between labels at this width
      var perHour = xOf(now, now) - xOf(now - 3600000, now), step = 12;
      [2, 3, 4, 6, 12].some(function (n) { if (perHour * n >= 56) { step = n; return true; } return false; });
      for (var t = h0; t < now; t += 3600000) {
        var x = xOf(t, now); if (x < gutter || x > W - PAD_R - 20 || Math.abs(x - nowX) < 34) continue;
        var hr = new Date(t).getHours(); if (hr % step) continue;
        var s = el('span', null, pad2(hr) + ':00');
        s.style.cssText = 'position:absolute;top:6px;transform:translateX(-50%);font:11px ui-monospace,"SF Mono",Menlo,monospace;color:var(--meta,#7d889c);left:' + x + 'px';
        axis.appendChild(s);
      }
      var n = el('span', null, 'now');
      n.style.cssText = 'position:absolute;top:6px;transform:translateX(-50%);font:11px ui-monospace,"SF Mono",Menlo,monospace;color:var(--accent,#ff8a3d);left:' + nowX + 'px';
      axis.appendChild(n);
    }

    function step() {
      stats.pending--; stats.frames++;
      raf = 0;
      if (dead) return;
      draw();
      if (meteors.length) schedule();
    }
    function schedule() {
      if (raf || dead) return;
      raf = requestAnimationFrame(step); stats.pending++;
    }

    function launch(e) {
      if (!ambientOn || !model || (!e.repo && offView(e, model.single))) return;
      if (e.kind !== 'commit' && !(e.kind === 'review' && !e.ok)) return;   // commits and failures streak; the rest just appear
      meteors.push({ e: e, row: model.rowOf(e), born: performance.now(), x: 0, y: 0, tx: 0, ty: 0, len: 60, a: 1, fail: e.kind === 'review' });
      schedule();
    }

    function tipFor(h) {
      if (h.label) {
        var r = h.row;
        if (r.id !== 'other') return null;
        var lines = [];
        if (r.folded.length) lines.push('Folded repos: ' + r.folded.join(', '));
        if (r.noRepo) lines.push(r.noRepo + (r.noRepo === 1 ? ' review' : ' reviews') + ' with no repo recorded');
        return lines.join('\n');
      }
      var e = h.e;
      return e.kind + (e.repo ? ' · ' + e.repo : '') + (e.agent ? ' · ' + e.agent : '') + (e.kind === 'review' ? (e.ok ? ' · ok' : ' · failed') : '') + ' · ' + hhmm(e.t);
    }
    cv.addEventListener('mousemove', function (ev) {
      var b = cv.getBoundingClientRect(), x = ev.clientX - b.left, y = ev.clientY - b.top, best = null, bd = 100;
      for (var i = 0; i < hits.length; i++) {
        var h = hits[i];
        if (h.label) { if (x < h.x1 && Math.abs(y - h.y) < 10) { best = h; bd = 0; } continue; }
        var d = Math.abs(h.x - x) + Math.abs(h.y - y);
        if (d < bd && d < 10) { best = h; bd = d; }
      }
      var text = best && tipFor(best);
      if (!text) { tip.style.display = 'none'; return; }
      tip.textContent = text; tip.style.whiteSpace = 'pre'; tip.style.display = 'block';
      tip.style.left = Math.min(Math.max(4, x + 12), Math.max(4, b.width - tip.offsetWidth - 4)) + 'px';
      tip.style.top = Math.max(4, y - 34) + 'px';
    });
    cv.addEventListener('mouseleave', function () { tip.style.display = 'none'; });

    function update(next) {
      if (dead) return;
      value = next;
      var evs = normalise(next), fresh = diffFresh(evs, feed);
      events = evs;
      rebuild();
      draw(); drawAxis();
      var now = clock();
      fresh.forEach(function (e) { if (e.t <= now && e.t >= model.start) launch(e); });
    }

    // The window slides on a slow timer, so the panel stays honest about "now" with no frame loop running.
    timer = setInterval(function () { if (!dead) { rebuild(); draw(); drawAxis(); } }, TICK_MS);
    if (typeof ResizeObserver === 'function') { ro = new ResizeObserver(function () { if (!dead) { rebuild(); draw(); drawAxis(); } }); ro.observe(plot); }
    else window.addEventListener('resize', resize);

    update(value);

    return {
      update: update,
      destroy: function () {
        dead = true;
        if (raf) { cancelAnimationFrame(raf); stats.pending--; raf = 0; }
        clearInterval(timer); if (ro) ro.disconnect(); else window.removeEventListener('resize', resize);
        if (reduce && reduce.removeEventListener) reduce.removeEventListener('change', onReduce);
        root.remove();
      },
    };
  }

  var api = { mount: mount, buildModel: buildModel, normalise: normalise, diffFresh: diffFresh, stats: stats };
  if (typeof window !== 'undefined') window.LevainSky = api;
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
})();

/* sky.js: the cockpit's ambient panel. Typed events (kind, at, repo, agent, outcome) drawn as a rolling
   window of rows by time, with new commits and failed reviews arriving as meteors.

     LevainSky.mount(el, value, opts) -> { update(value), destroy() }
       value  {events: [{kind, at, repo, agent, outcome, end?, label?}], legend}   (the `ambient` kind's contract)
       opts   windowHours (14)   topRows (5)   pinnedRows ([])   single (a repo name, or auto)
              ambient (true)     now (() => ms; a replay supplies its own clock)
              size ('compact' | 'large' | 'strip'; default and 'compact' are today's size, 'large' is the tall chart,
                    'strip' is the phone's and the Build tab's: merged lanes, no gutter, no tooltip, no summary)
              head (true: a strip carries its own header row, "Activity  N commits · ...  last 14 h", as the Build tab draws it)
              onHead (fn(parts, hours): called with each [count, label] pair every time the head row is rebuilt, so a host can draw the figures in its own bar)
     LevainSky.legendNode() -> a DOM key for the Levain marks (the host may place it, as the hero does with its own).

   A feed is Levain-shaped when it carries a session or question, a decision with an outcome of allowed / denied / waiting
   (never `failed` alone: flow's feed carries it), or any labelled event; it then gets the lanes repos, sessions, decisions, memory, questions. Any other
   feed (flow's) keeps its rows and its marks exactly.

   Design rules this file keeps:
   - The motion is the data itself, never a mascot of it: between events nothing moves.
   - The frame loop runs only while a meteor is in flight. Meteor state is derived from the clock, so any
     draw is correct however few frames the machine gives us.
   - Colour comes only from CSS custom properties (--base, --accent, --contrast and their derivatives), so the
     theme owns it; the fallbacks below are used only when a property is absent. */
(function () {
  'use strict';

  var FLY = 600, LIFE = 750;        // ms to land / ms until gone
  var PACK_GAP_MS = 6 * 60000;      // two sessions closer than this never share a track
  var NOW_AT = 0.85;                // where "now" sits across the plot (flow's feed keeps it)
  var NOW_AT_LEV = 0.96;            // a Levain feed has no future to leave room for: "now" sits near the right edge
  var MAX_TRACKS = 8;               // session tracks shown; further overlapping sessions share the last one
  var MAX_WAITING_ARIA = 5;         // waiting items the canvas's aria-label names before "and N more"
  var TICK_MS = 30000;              // the window slides on a timer, not on frames
  var ROW_H_MIN = 26, PAD_T = 28, PAD_B = 12, PAD_R = 24, GUTTER_PAD = 16, GUTTER_MIN = 56;
  // A repo's hue slot, shared with the row tags in cockpit_view.js. The repos are the install's own: the names its data
  // carries (an event's or a row's `repo`), noted as they are read; the package knows none. Slots go to the known repos in
  // name order, each to its name's hash slot or the next free one, so up to HUE_SLOTS repos never share a hue; past that a
  // repo takes its hash slot. A repo not noted yet gets its hash slot.
  var HUE_SLOTS = 8;   // --hue-1 .. --hue-8 in dashboard.css (a stylesheet without 6-8 shows slot k as k mod 5)
  var MAX_KNOWN = 64;
  var known = Object.create(null), nKnown = 0, slotOf = null;
  function slotHash(r) {
    var h = 2166136261;   // FNV-1a, 32 bit
    for (var i = 0; i < r.length; i++) { h ^= r.charCodeAt(i); h = Math.imul(h, 16777619) >>> 0; }
    return h % HUE_SLOTS;
  }
  function norm(repo) { return String(repo == null ? '' : repo).trim().toLowerCase(); }
  function noteRepo(repo) {
    var r = norm(repo);
    if (!r || r.length > 100 || r in known || nKnown >= MAX_KNOWN) return;
    known[r] = String(repo).trim(); nKnown++; slotOf = null;
  }
  // the repos noted so far, as the data first spelled them (the row tags match these)
  function repos() { return Object.keys(known).map(function (k) { return known[k]; }); }
  function hueKey(repo) {
    var r = norm(repo);
    if (!r) return '';
    if (!slotOf) {
      slotOf = Object.create(null);
      var used = {}, names = Object.keys(known).sort();
      for (var i = 0; i < names.length; i++) {
        var h = slotHash(names[i]), s = h;
        for (var k = 0; k < HUE_SLOTS && used[s]; k++) s = (h + k + 1) % HUE_SLOTS;
        if (used[s]) s = h;
        used[s] = true; slotOf[names[i]] = s;
      }
    }
    return String((r in slotOf ? slotOf[r] : slotHash(r)) + 1);
  }
  var SHORT_LABELS = { memory: 'mem', decisions: 'dec' };   // by row id
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

  var LEVAIN_OUT = Object.assign(Object.create(null), { allowed: 1, denied: 1, waiting: 1 });   // not 'failed': flow's feed carries that outcome too
  function normalise(value) {
    var out = [];
    var src = (value && value.events) || [];
    for (var i = 0; i < src.length; i++) {
      var e = src[i];
      if (!e) continue;
      var t = parseAt(e.at), t2 = e.end == null ? NaN : parseAt(e.end);
      if (isNaN(t) || !e.kind) continue;
      if (e.repo != null) noteRepo(e.repo);
      out.push({ t: t, kind: String(e.kind), repo: e.repo == null ? null : String(e.repo),
        agent: e.agent == null ? null : String(e.agent), ok: e.outcome !== 'failed',
        out: e.outcome == null ? '' : String(e.outcome), t2: isNaN(t2) ? null : t2,
        label: e.label == null || e.label === '' ? null : String(e.label).slice(0, 160),
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

  // A session bar ends at its `end` (never past now); a live one runs to now; one with no end is a point.
  function sessEnd(e, nowMs) {
    if (e.out === 'live') return nowMs;
    return e.t2 == null ? e.t : Math.min(Math.max(e.t2, e.t), nowMs);
  }
  // The kinds, outcomes and labels only a Levain feed carries; flow's feed has none of them (never `failed` alone: flow's review and decision carry it).
  function isLev(e) {
    return e.kind === 'session' || e.kind === 'question' || (e.kind === 'decision' && !!LEVAIN_OUT[e.out]) || e.label != null;
  }

  // Which rows, and which row each event lands on. Pure: no DOM, so it is testable under node.
  function buildModel(events, opts, nowMs) {
    var start = nowMs - opts.windowHours * 3600000, strip = opts.size === 'strip';
    var inWin = events.filter(function (e) {
      if (e.kind === 'session') return e.t <= nowMs && sessEnd(e, nowMs) >= start;
      return e.t >= start && e.t <= nowMs;
    });
    // the payload's own `dialect` decides when it carries one; only a feed without the field is inferred, and an inferred mount keeps its lanes as events age out (opts.lev)
    var lev = opts.dialect === 'levain' ? true : opts.dialect === 'flow' ? false : (!!opts.lev || inWin.some(isLev));
    var repos = Object.create(null);
    inWin.forEach(function (e) { if (e.repo && (e.kind === 'commit' || e.kind === 'review')) repos[e.repo] = (repos[e.repo] || 0) + 1; });
    var names = Object.keys(repos);
    var single = lev || strip ? '' : opts.single || (names.length <= 1 && !opts.pinnedRows.length ? (names[0] || '') : '');
    var rows, rowOf, folded = [], noRepo = 0;
    if (strip) {
      // the phone's strip: one lane per kind of thing, the repos merged
      rows = [{ id: 'commit', label: 'commits' }, { id: 'sessions', label: 'sessions' }, { id: 'memory', label: 'memory' }, { id: 'decisions', label: 'decisions' }];
      rowOf = function (e) {
        return e.kind === 'commit' || e.kind === 'review' ? 'commit' : e.kind === 'session' ? 'sessions' : e.kind === 'decision' || e.kind === 'question' ? 'decisions' : 'memory';
      };
    } else if (single) {
      rows = [{ id: 'commit', label: 'commits' }, { id: 'review', label: 'reviews' }, { id: 'memory', label: 'memory' }, { id: 'decisions', label: 'decisions' }];
      rowOf = function (e) { return e.kind === 'commit' ? 'commit' : e.kind === 'review' ? 'review' : e.kind === 'decision' ? 'decisions' : 'memory'; };
    } else {
      var main = opts.pinnedRows.length ? opts.pinnedRows.slice()
        : names.sort(function (a, b) { return repos[b] - repos[a] || (a < b ? -1 : 1); }).slice(0, opts.topRows);
      rows = main.map(function (r) { return { id: 'r:' + r, label: r }; });
      var mainSet = Object.create(null); main.forEach(function (r) { mainSet[r] = 1; });
      var foldedSet = Object.create(null);
      inWin.forEach(function (e) {
        if (e.kind !== 'commit' && e.kind !== 'review') return;
        if (e.repo && mainSet[e.repo]) return;
        if (e.repo) foldedSet[e.repo] = 1; else noRepo++;
      });
      folded = Object.keys(foldedSet).sort();
      if (folded.length || noRepo) rows.push({ id: 'other', label: 'other', folded: folded, noRepo: noRepo });
      if (lev) rows.push({ id: 'sessions', label: 'sessions' }, { id: 'decisions', label: 'decisions' }, { id: 'memory', label: 'memory' }, { id: 'questions', label: 'questions' });
      else rows.push({ id: 'memory', label: 'memory' }, { id: 'decisions', label: 'decisions' });
      rowOf = function (e) {
        if (lev && e.kind === 'session') return 'sessions';
        if (lev && e.kind === 'question') return 'questions';
        if (e.kind === 'decision') return 'decisions';
        if (e.kind !== 'commit' && e.kind !== 'review') return 'memory';
        return e.repo && mainSet[e.repo] ? 'r:' + e.repo : 'other';
      };
    }
    // a phone (compact) or the strip keeps only the rows an event landed on, and compact shortens the names of memory and
    // decisions; if nothing landed anywhere the rows stay, so the plot is never a bare frame
    if (opts.compact || strip) {
      var used = Object.create(null);
      inWin.forEach(function (e) { if (!offView(e, single)) used[rowOf(e)] = 1; });
      var kept = rows.filter(function (r) { return used[r.id]; });
      if (kept.length) rows = kept;
      if (opts.compact) rows.forEach(function (r) { if (Object.prototype.hasOwnProperty.call(SHORT_LABELS, r.id)) r.label = SHORT_LABELS[r.id]; });
    }
    var waiting = [];
    var counts = { commit: 0, review: 0, failed: 0, memory: 0, sessions: 0, decisions: 0, questions: 0, waiting: 0 };
    inWin.forEach(function (e) {
      if (offView(e, single)) return;
      if (e.kind === 'commit') counts.commit++;
      else if (e.kind === 'review') { counts.review++; if (!e.ok) counts.failed++; }
      else if (e.kind === 'session') counts.sessions++;
      else if (e.kind === 'question') { counts.questions++; if (e.out === 'open') { counts.waiting++; waiting.push(e); } }
      else if (e.kind === 'decision') { counts.decisions++; if (e.out === 'waiting') { counts.waiting++; waiting.push(e); } }
      else counts.memory++;
    });
    // sessions are packed into tracks: a bar starting within PACK_GAP_MS of a track's last end takes the next track; past
    // MAX_TRACKS the rest share the last one (`overflow` counts them). The track lives in the model, never on the event.
    var ends = [], trackOf = new Map(), overflow = 0, bumped = [];
    inWin.filter(function (e) { return e.kind === 'session'; }).sort(function (a, b) { return a.t - b.t; }).forEach(function (e) {
      var i = 0;
      while (i < ends.length && e.t < ends[i] + PACK_GAP_MS) i++;
      if (i >= MAX_TRACKS) { i = MAX_TRACKS - 1; overflow++; bumped.push(e); }
      ends[i] = Math.max(ends[i] || 0, sessEnd(e, nowMs)); trackOf.set(e, i);
    });
    // a bar on the shared track says how many bumped sessions overlap it; a bar no bumped one overlaps says nothing
    var sharedWith = new Map();
    bumped.forEach(function (b) {
      inWin.forEach(function (e) {
        if (e.kind !== 'session' || e === b || trackOf.get(e) !== MAX_TRACKS - 1) return;
        if (e.t < sessEnd(b, nowMs) && b.t < sessEnd(e, nowMs)) sharedWith.set(e, (sharedWith.get(e) || 0) + 1);
      });
      sharedWith.set(b, sharedWith.get(b) || 0);
    });
    return { rows: rows, rowOf: rowOf, single: single, counts: counts, start: start, lev: lev, strip: strip, empty: !inWin.some(function (e) { return !offView(e, single); }), tracks: ends.length,
      trackOf: trackOf, overflow: overflow, sharedWith: sharedWith, waiting: waiting,
      hasDecisions: inWin.some(function (e) { return e.kind === 'decision'; }) };
  }

  // The hover text for one mark: "commit · ledgerline · 09:41"; a labelled event reads "decision · <label> · waiting · 13:20".
  // A session's end reads as the bar is drawn: never before its start, and with `nowMs` given never past now.
  function eventTip(e, nowMs, lev) {
    if (e.kind === 'session') {
      var end = e.out === 'live' ? 'now' : hhmm(nowMs == null ? Math.max(e.t2 == null ? e.t : e.t2, e.t) : sessEnd(e, nowMs));
      return 'session · ' + (e.agent || 'agent') + (e.repo ? ' · ' + e.repo : '') + ' · ' + hhmm(e.t) + '–' + end;
    }
    if (e.label != null) return (e.kind === 'memory' ? 'memory write' : e.kind) + ' · ' + e.label + (e.out && e.out !== 'ok' ? ' · ' + e.out : '') + ' · ' + hhmm(e.t);
    if (lev && e.out === 'failed' && e.kind !== 'review') return e.kind + (e.repo ? ' · ' + e.repo : '') + ' · failed · ' + hhmm(e.t);
    return e.kind + (e.repo ? ' · ' + e.repo : '') + (e.agent ? ' · ' + e.agent : '') + (e.kind === 'review' ? (e.ok ? ' · ok' : ' · failed') : '') + ' · ' + hhmm(e.t);
  }

  // The key to the Levain marks, drawn with the same custom properties as the marks. `review` is reserved for DISPATCH.
  function legendNode() {
    var box = el('div', { class: 'sky-keys' });
    box.style.cssText = 'display:flex;flex-wrap:wrap;align-items:center;gap:6px 16px';
    function item(glyphCss, text, extra) {
      var it = el('span', { class: 'sky-key' });
      it.style.cssText = 'display:inline-flex;align-items:center;gap:6px;white-space:nowrap';
      var gl = el('i'); gl.style.cssText = 'display:inline-block;flex:none;' + glyphCss;
      it.append(gl, document.createTextNode(text));
      if (extra) it.style.opacity = extra;
      box.appendChild(it);
    }
    var dia = 'width:8px;height:8px;transform:rotate(45deg) scale(.85);';
    item('width:6px;height:6px;border-radius:50%;background:var(--contrast,#f3f5f9)', 'commit');
    item('width:14px;height:5px;border-radius:3px;background:var(--label,#9ba6ba);opacity:.55', 'session');
    item('width:2px;height:11px;border-radius:1px;background:var(--info,#5aa9ff)', 'memory write');
    item(dia + 'background:var(--ok,#3ecf8e)', 'decision');
    item('width:6px;height:6px;border-radius:50%;background:var(--violet,#b48cff)', 'question');
    item('width:6px;height:6px;border-radius:50%;background:var(--violet,#b48cff);outline:1.5px solid var(--accent,#ff8a3d);outline-offset:1.5px', 'open question');
    item(dia + 'background:var(--accent,#ff8a3d);box-shadow:0 0 0 1.5px var(--base,#060912),0 0 0 3px var(--accent,#ff8a3d)', 'waiting on you');
    item(dia + 'border:1.6px solid var(--bad,#e5484d);background:transparent', 'denied');
    item('width:7px;height:7px;border-radius:50%;border:1.6px solid var(--bad,#e5484d)', 'failed');
    item('width:7px;height:7px;border-radius:50%;border:1.4px dashed var(--meta,#7d889c)', 'review: when DISPATCH lands', '.8');
    return box;
  }

  function mount(host, value, userOpts) {
    var o = userOpts || {};
    var size = o.size === 'large' || o.size === 'strip' ? o.size : 'compact';
    var strip = size === 'strip', sc = size === 'large' ? 1.4 : strip ? 0.8 : 1;   // mark scale
    var padT = strip ? 6 : PAD_T, padB = strip ? 4 : PAD_B, padR = strip ? 4 : PAD_R;
    var trackH = size === 'large' ? 11 : strip ? 5 : 7, trackGap = size === 'large' ? 4 : 3;
    var opts = { windowHours: o.windowHours || 14, topRows: o.topRows || 5, pinnedRows: o.pinnedRows || [], single: o.single || '', size: size, dialect: dialectOf(value) };
    var clock = o.now || function () { return Date.now(); };
    var reduce = window.matchMedia ? window.matchMedia('(prefers-reduced-motion: reduce)') : null;
    var calmOn = function () { var h = document.documentElement; return !!(h && h.classList && h.classList.contains('calm')); };   // the page's Calm control: the same as the OS asking
    var reduceOn = function () { return !!(reduce && reduce.matches); };
    // ambientOn is the user's Ambient preference; neither Calm nor the OS reduced-motion setting writes it. The motion that
    // runs is the preference AND not reduced motion AND not Calm.
    var ambientOn = o.ambient !== false && !strip;
    var motionOn = function () { return ambientOn && !reduceOn() && !calmOn(); };

    var root = el('div', { class: 'sky-panel' });
    root.style.position = 'relative';
    var bar = el('div', { class: 'sky-bar' });
    var summary = el('span', { class: 'sky-summary' });
    var toggle = el('button', { type: 'button', class: 'sky-toggle' });
    // a host that owns a control row (the hero's strip) takes the toggle there, so the row is one row
    var head = strip && !!o.head;   // the Build tab's strip: a one-row header, no toggle
    if (strip && !head) bar.style.display = 'none';   // the phone's strip is a glance: no summary line, no toggle (inline: .sky-bar's display would beat `hidden`)
    if (head) {
      bar.classList.add('sky-head'); bar.append(summary);
      // the strip has no Ambient key, but the masthead's row must not move when it is absent: an invisible twin holds the key's place
      if (o.toggleHost) {
        Array.prototype.forEach.call(o.toggleHost.querySelectorAll(':scope > .sky-toggle'), function (t) { t.remove(); });
        toggle.classList.add('sky-toggle-ghost'); toggle.textContent = 'Ambient'; toggle.tabIndex = -1; toggle.disabled = true; toggle.setAttribute('aria-hidden', 'true');
        o.toggleHost.insertBefore(toggle, o.toggleBefore && o.toggleBefore.parentNode === o.toggleHost ? o.toggleBefore : null);
      }
    }
    else if (o.toggleHost && !strip) { Array.prototype.forEach.call(o.toggleHost.querySelectorAll(':scope > .sky-toggle'), function (t) { t.remove(); }); o.toggleHost.insertBefore(toggle, o.toggleBefore && o.toggleBefore.parentNode === o.toggleHost ? o.toggleBefore : null); bar.append(summary); } else bar.append(summary, toggle);
    var plot = el('div', { class: 'sky-plot' });
    plot.style.position = 'relative';
    var cv = el('canvas', { class: 'sky-canvas', role: 'img' });
    cv.style.display = 'block'; cv.style.width = '100%'; cv.style.height = '320px';
    var axis = el('div', { class: 'sky-axis' });
    axis.style.position = 'relative'; axis.style.height = strip ? '18px' : '24px';
    var tip = el('div', { class: 'sky-tip', role: 'status' });
    tip.style.cssText = 'position:absolute;pointer-events:none;display:none;z-index:2;white-space:nowrap;padding:6px 10px;border-radius:10px;font-size:12px;border:1px solid var(--hair-2,#28324a);background:var(--base,#060912);color:var(--contrast,var(--text,#f3f5f9))';
    var showLegend = o.legend !== false && !strip;    // a host that draws its own legend passes legend:false
    var legend = el('div', { class: 'sky-legend' });
    legend.hidden = !showLegend;   // the host's own legend stands in; an empty strip would still take its padding
    plot.append(cv, axis, tip);
    root.append(bar, plot, legend);
    host.appendChild(root);

    var g = cv.getContext('2d');
    var W = 0, H = 0, DPR = Math.min(2, window.devicePixelRatio || 1), gutter = strip ? 6 : GUTTER_MIN, rowBase = ROW_H_MIN, sumW = 1, wOf = Object.create(null), cumOf = Object.create(null), nowAt = NOW_AT;
    var events = [], model = null, meteors = [], feed = { seen: {}, loaded: false }, raf = 0, timer = 0, ro = null, dead = false, hits = [];
    var themeObs = null, scheme = null;

    function css(name, fallback) {
      var v = getComputedStyle(root).getPropertyValue(name).trim();
      return v || fallback;
    }
    function palette() {
      return { base: css('--base', '#060912'), accent: css('--accent', '#ff8a3d'), contrast: css('--contrast', css('--text', '#f3f5f9')),
        label: css('--label', '#9ba6ba'), meta: css('--meta', '#7d889c'), hair: css('--hair', '#1c2435'), hair2: css('--hair-2', '#121a2a'),
        bad: css('--bad', '#e5484d'), ok: css('--ok', '#3ecf8e'), info: css('--info', '#5aa9ff'), violet: css('--violet', '#b48cff'),
        hues: (function () { var o = {}; for (var i = 1; i <= HUE_SLOTS; i++) o[String(i)] = css('--hue-' + i, i > 5 ? css('--hue-' + (((i - 1) % 5) + 1), '') : ''); return o; })() };
    }

    function setToggle() {
      toggle.textContent = o.toggleHost ? 'Ambient' : (ambientOn ? 'Ambient on' : 'Ambient paused');
      toggle.setAttribute('aria-pressed', ambientOn ? 'true' : 'false');
      toggle.setAttribute('aria-label', 'Ambient motion, currently ' + (ambientOn ? 'on' : 'paused'));
    }
    toggle.addEventListener('click', function () {
      ambientOn = !ambientOn; setToggle();
      if (!motionOn()) { meteors = []; draw(); }
    });
    function onReduce() { if (reduceOn() && meteors.length) { meteors = []; draw(); } }
    if (reduce && reduce.addEventListener) reduce.addEventListener('change', onReduce);
    function onCalm() { if (calmOn() && meteors.length) { meteors = []; draw(); } }
    window.addEventListener('levain-calm', onCalm);
    setToggle();

    var xOf = function (t, now) { var span = opts.windowHours * 3600000 / nowAt; return gutter + (W - gutter - padR) * ((t - (now - opts.windowHours * 3600000)) / span); };
    // each row has a weight (1 = a plain row; the sessions row grows with its tracks), so the plain rows are spaced as ever
    var rowY = function (id) {
      if (!(id in wOf)) return padT;
      return padT + (H - padT - padB) * (cumOf[id] + wOf[id] / 2) / sumW;
    };
    var rowPx = function (id) { return (H - padT - padB) * wOf[id] / sumW; };

    function measureGutter() {
      if (strip) { gutter = 6; return; }   // the strip's lanes are told apart by their marks, not labelled
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
      DPR = Math.min(2, window.devicePixelRatio || 1);
      cv.width = Math.round(W * DPR); cv.height = Math.round(H * DPR);
      draw(); drawAxis();
    }

    function rebuild() {
      var now = clock();
      var hw = host.getBoundingClientRect().width, narrow = !strip && hw > 0 && hw < 520;   // a phone: only the rows with events, taller rows, so the plot is usable
      opts.compact = narrow;
      model = buildModel(events, opts, now);
      if (model.lev) opts.lev = true;   // latch: once the lanes are Levain's they stay so, however the window ages
      nowAt = model.lev || strip ? NOW_AT_LEV : NOW_AT;
      rowBase = size === 'large' ? 44 : strip ? 18 : narrow ? 48 : ROW_H_MIN;
      sumW = 0; wOf = Object.create(null); cumOf = Object.create(null);
      model.rows.forEach(function (r) {
        var w = 1;
        if (r.id === 'sessions' && model.tracks) w = Math.max(1, (model.tracks * (trackH + trackGap) + (strip ? 4 : size === 'large' ? 16 : 10)) / rowBase);
        wOf[r.id] = w; cumOf[r.id] = sumW; sumW += w;
      });
      if (!sumW) sumW = 1;
      var rowsH = Math.max(size === 'large' ? 240 : strip ? 60 : narrow ? 200 : 160, padT + padB + sumW * rowBase);
      cv.style.height = rowsH + 'px';
      var c = model.counts, lev = model.lev;
      var line = 'Last ' + opts.windowHours + ' hours: ' + c.commit + ' commits, ' + (lev ? c.sessions + ' sessions, ' + c.decisions + ' decisions, ' + c.memory + ' memory writes, ' + c.questions + ' questions, ' + c.waiting + ' waiting on you'
        : c.review + ' reviews (' + c.failed + ' failed), ' + c.memory + ' memory writes');
      if (head) {
        var parts = lev ? [[c.commit, 'commits'], [c.sessions, 'sessions'], [c.decisions, 'decisions'], [c.memory, 'memory writes'], [c.questions, 'questions']]
          : [[c.commit, 'commits'], [c.review, 'reviews'], [c.memory, 'memory writes']];
        var hb = document.createElement('b'); hb.className = 'sky-head-title'; hb.textContent = 'Activity';
        var hc = document.createElement('span'); hc.className = 'sky-head-counts';
        hc.textContent = parts.map(function (x) { return x[0] + ' ' + x[1]; }).join(' · ');
        var hw2 = document.createElement('span'); hw2.className = 'sky-head-win'; hw2.textContent = 'last ' + opts.windowHours + ' h';
        summary.replaceChildren(hb, hc, hw2);
        if (typeof o.onHead === 'function') { try { o.onHead(parts, opts.windowHours); } catch (e) { /* the host's figures are its own; a throw there must not stop the chart */ } }
      } else if (!strip) {   // the phone's strip has no summary bar, so none is built
        var bold = function (n) { var e = document.createElement('b'); e.className = 'num'; e.textContent = String(n); return e; };
        var txt = function (x) { return document.createTextNode(x); };
        if (lev) summary.replaceChildren(txt('Last ' + opts.windowHours + ' hours: '), bold(c.commit), txt(' commits · '), bold(c.sessions), txt(' sessions · '), bold(c.decisions), txt(' decisions · '), bold(c.memory), txt(' memory writes · '), bold(c.questions), txt(' questions · '), bold(c.waiting), txt(' waiting on you'));
        else summary.replaceChildren(txt('Last ' + opts.windowHours + ' hours: '), bold(c.commit), txt(' commits · '), bold(c.review), txt(' reviews (' + c.failed + ' failed) · '), bold(c.memory), txt(' memory writes'));
      }
      // the canvas is the only place the marks live, so its label names what is waiting on the reader
      var wl = model.waiting.slice().sort(function (a, b) { return a.t - b.t; }), more = '';
      if (wl.length) {
        more = ' Waiting on you: ' + wl.slice(0, MAX_WAITING_ARIA).map(function (e) { return e.kind + ' ' + (e.label || e.repo || 'unlabelled') + ' at ' + hhmm(e.t); }).join('; ')
          + (wl.length > MAX_WAITING_ARIA ? '; and ' + (wl.length - MAX_WAITING_ARIA) + ' more' : '') + '. Details in Needs you.';
      }
      cv.setAttribute('aria-label', line + '.' + more + ' ' + (value && value.legend ? value.legend : ''));
      if (showLegend && lev) legend.replaceChildren(legendNode());
      else legend.textContent = showLegend && value && value.legend ? value.legend : '';
      var r = cv.getBoundingClientRect(); W = r.width; H = r.height;
      DPR = Math.min(2, window.devicePixelRatio || 1);
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
        g.globalAlpha = 1; g.fillStyle = P.hair; g.fillRect(gutter, y, W - padR - gutter, 1);
        g.fillStyle = P.meta;
        var shown = strip ? '' : fit(r.label, gutter - GUTTER_PAD * 2 + 8);
        if (!strip) g.fillText(shown, gutter - GUTTER_PAD, y + 4);
        var sw = !strip && P.hues && r.id.indexOf('r:') === 0 ? P.hues[hueKey(r.id.slice(2))] : '';
        if (sw) { g.fillStyle = sw; g.beginPath(); g.arc(gutter - GUTTER_PAD - g.measureText(shown).width - 8, y, 3, 0, 7); g.fill(); g.fillStyle = P.meta; }
        hits.push({ label: true, row: r, x0: 0, x1: gutter, y: y });
      });
      var h0 = Math.ceil(model.start / 3600000) * 3600000;
      for (var t = h0; t < now; t += 3600000) {
        var x = xOf(t, now); if (x < gutter || x > W - padR) continue;
        g.fillStyle = (new Date(t).getHours() % 3 === 0) ? P.hair : P.hair2; g.fillRect(x, padT - 10, 1, H - padT);
      }
      var lev = model.lev, R = 4 * sc;
      function diamond(x, y, h) { g.beginPath(); g.moveTo(x, y - h); g.lineTo(x + h, y); g.lineTo(x, y + h); g.lineTo(x - h, y); g.closePath(); }
      function pill(x, y, w, h) {
        var r = h / 2; w = Math.max(w, h);
        g.beginPath(); g.moveTo(x + r, y); g.lineTo(x + w - r, y); g.arc(x + w - r, y + r, r, -Math.PI / 2, Math.PI / 2);
        g.lineTo(x + r, y + h); g.arc(x + r, y + r, r, Math.PI / 2, Math.PI * 1.5); g.closePath();
      }
      var late = [];   // the marks that want the reader, drawn after the rest so a crowd never buries them
      function drawWaiting(x, y, h) {
        g.globalAlpha = 0.22; g.fillStyle = P.accent; diamond(x, y, h + 4); g.fill();   // orange is spent only on "waiting on you"
        g.globalAlpha = 1; g.fillStyle = P.accent; diamond(x, y, h); g.fill();
        g.strokeStyle = P.accent; g.lineWidth = 1.4; diamond(x, y, h + 3.5); g.stroke();   // the ring is the shape cue: allowed is the plain diamond
      }
      function drawOpenQuestion(x, y) {
        g.globalAlpha = 1; g.fillStyle = P.violet; g.beginPath(); g.arc(x, y, R * 0.95, 0, 7); g.fill();
        g.strokeStyle = P.accent; g.lineWidth = 1.5; g.beginPath(); g.arc(x, y, R * 0.95 + 3, 0, 7); g.stroke();
      }
      events.forEach(function (e) {
        if (e.kind === 'session') {
          if (!('sessions' in wOf) || e.t > now) return;
          var sEnd = sessEnd(e, now); if (sEnd < model.start) return;
          var x1 = Math.max(gutter, xOf(Math.max(e.t, model.start), now)), x2 = Math.max(x1 + 3, xOf(sEnd, now));
          var top = rowY('sessions') - rowPx('sessions') / 2 + (strip ? 2 : size === 'large' ? 8 : 5), yy = top + (model.trackOf.get(e) || 0) * (trackH + trackGap);
          g.globalAlpha = e.out === 'live' ? 0.85 : 0.5; g.fillStyle = e.out === 'live' ? P.ok : P.label;
          pill(x1, yy, x2 - x1, trackH); g.fill(); g.globalAlpha = 1;
          hits.push({ e: e, x: x1, x2: x2, y: yy + trackH / 2 });
          return;
        }
        if (e.t < model.start || e.t > now) return;
        if (offView(e, model.single)) return;
        var row = model.rowOf(e), x = xOf(e.t, now), y = rowY(row) + (lev || strip ? 0 : (hash01(e.t + e.kind.length) - 0.5) * 12);
        var a = Math.max(0.35, 1 - ((now - e.t) / 3600000) / (opts.windowHours * 1.15));
        var hue = !P.hues || (lev && e.kind !== 'commit') ? '' : P.hues[hueKey(e.repo)];   // a levain feed hues its commits by repo; its own marks keep their kind colours
        if (lev && e.out === 'failed' && e.kind !== 'review') {   // a failure is a red ring, whatever failed
          g.globalAlpha = 1; g.strokeStyle = P.bad; g.lineWidth = 1.6; g.beginPath(); g.arc(x, y, R + 0.5, 0, 7); g.stroke();
        }
        else if (e.kind === 'commit') {
          g.globalAlpha = lev || hue ? Math.max(a, 0.6) : a; g.fillStyle = hue || P.contrast; g.beginPath();
          g.arc(x, y, lev ? 2.6 * sc : ((hue ? 2 : 1.3) + hash01(e.t) * 0.9) * sc, 0, 7); g.fill();
        }
        else if (e.kind === 'review') {
          g.globalAlpha = e.ok ? a * 0.8 : 1; g.lineWidth = e.ok ? 1.2 : 1.5;
          var glow = !e.ok;   // a failed review glows red
          g.strokeStyle = e.ok ? (hue || P.label) : (glow ? P.bad : P.accent);
          if (glow) { g.shadowColor = P.bad; g.shadowBlur = 12; g.lineWidth = 2; }
          g.beginPath(); g.arc(x, y, (e.ok ? 2.8 : 3.6) * sc, 0, 7); g.stroke(); g.shadowBlur = 0;
        }
        else if (e.kind === 'decision') {
          g.globalAlpha = 1;
          var h = R + (lev ? 1.5 : 0);
          if (e.out === 'waiting') late.push(function () { drawWaiting(x, y, h); });
          else if (e.out === 'denied') { g.fillStyle = P.base; g.strokeStyle = P.bad; g.lineWidth = 1.6; diamond(x, y, h); g.fill(); g.stroke(); }
          else if (e.out === 'allowed') { g.fillStyle = P.ok; diamond(x, y, h); g.fill(); }
          else { g.fillStyle = P.contrast; diamond(x, y, h); g.fill(); }
        }
        else if (e.kind === 'question') {
          if (e.out === 'open') late.push(function () { drawOpenQuestion(x, y); });
          else { g.globalAlpha = 1; g.fillStyle = P.violet; g.beginPath(); g.arc(x, y, R * 0.95, 0, 7); g.fill(); }
        }
        else if (lev) {   // a memory write: a blue tick
          var th = (strip ? 10 : size === 'large' ? 18 : 12);
          g.globalAlpha = 1; g.fillStyle = P.info; g.fillRect(x - 1, y - th / 2, 2.2, th);
        }
        else { g.globalAlpha = a * 0.55; g.fillStyle = P.contrast; g.fillRect(x, y, 1.4, 1.4); }
        g.globalAlpha = 1;
        hits.push({ e: e, x: x, y: y });
      });
      if (late.length) { late.forEach(function (f) { f(); }); g.globalAlpha = 1; }
      if (!model.lev && !strip && !model.empty && !model.hasDecisions && model.rows.some(function (r) { return r.id === 'decisions'; })) { g.fillStyle = P.meta; g.textAlign = 'left'; g.fillText('none in this window', gutter + 8, rowY('decisions') + 4); }
      if (model.empty) {   // the lanes stay (a bare frame reads as broken) and one line says why they are bare
        var msg = strip ? 'Nothing yet' : 'No activity in the last ' + opts.windowHours + ' h', mx = gutter + (W - gutter - padR) / 2, my = padT + (H - padT - padB) / 2;
        g.font = FONT_MONO; g.textAlign = 'center';
        g.globalAlpha = 1; g.fillStyle = P.label; g.fillText(msg, mx, my + 4);   // text only: no box over the grid
      }
      var grad = g.createLinearGradient(0, padT - 10, 0, H);
      grad.addColorStop(0, 'transparent'); grad.addColorStop(0.25, P.accent); grad.addColorStop(1, P.accent);
      g.globalAlpha = 0.5; g.fillStyle = grad; g.fillRect(nowX, padT - 10, 1.5, H - padT + 10); g.globalAlpha = 1;
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
        var x = xOf(t, now); if (x < gutter || x > W - padR - 20 || Math.abs(x - nowX) < (strip ? 12 : 34)) continue;
        var hr = new Date(t).getHours(); if (hr % step) continue;
        var s = el('span', null, pad2(hr) + ':00');
        s.style.cssText = 'position:absolute;top:6px;transform:translateX(-50%);font:11px ui-monospace,"SF Mono",Menlo,monospace;color:var(--meta,#7d889c);left:' + x + 'px';
        axis.appendChild(s);
      }
      if (strip) return;
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
      if (!motionOn() || !model || (!e.repo && offView(e, model.single))) return;
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
      var t = eventTip(h.e, clock(), model.lev);
      var also = h.e.kind === 'session' ? model.sharedWith.get(h.e) : 0;
      if (also) t += ' · this row also holds ' + also + (also === 1 ? ' other session' : ' other sessions');
      return t;
    }
    cv.addEventListener('mousemove', function (ev) {
      if (strip) return;   // the strip is a glance: no tooltip
      var b = cv.getBoundingClientRect(), x = ev.clientX - b.left, y = ev.clientY - b.top, best = null, bd = 100;
      for (var i = 0; i < hits.length; i++) {
        var h = hits[i];
        if (h.label) { if (x < h.x1 && Math.abs(y - h.y) < 10) { best = h; bd = 0; } continue; }
        var d = (h.x2 === undefined ? Math.abs(h.x - x) : x < h.x ? h.x - x : x > h.x2 ? x - h.x2 : 0) + Math.abs(h.y - y);
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
      opts.dialect = dialectOf(next);
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

    // the canvas holds resolved colours, so a theme switch (the html[data-theme] attribute, or the OS scheme) repaints it
    function repaint() { if (!dead) draw(); }
    if (typeof MutationObserver === 'function' && document.documentElement) {
      themeObs = new MutationObserver(repaint);
      themeObs.observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme'] });
    }
    scheme = window.matchMedia ? window.matchMedia('(prefers-color-scheme: dark)') : null;
    if (scheme && scheme.addEventListener) scheme.addEventListener('change', repaint);

    update(value);

    return {
      update: update,
      destroy: function () {
        dead = true;
        if (raf) { cancelAnimationFrame(raf); stats.pending--; raf = 0; }
        clearInterval(timer); if (ro) ro.disconnect(); else window.removeEventListener('resize', resize);
        if (reduce && reduce.removeEventListener) reduce.removeEventListener('change', onReduce);
        window.removeEventListener('levain-calm', onCalm);
        if (themeObs) themeObs.disconnect();
        if (scheme && scheme.removeEventListener) scheme.removeEventListener('change', repaint);
        root.remove();
        if (o.toggleHost) toggle.remove();
      },
    };
  }

  // true when a feed carries what only a Levain feed carries (the host picks the Levain key over flow's)
  function dialectOf(value) { var d = value && value.dialect; return d === 'levain' || d === 'flow' ? d : ''; }
  function isLevainFeed(value) { var d = dialectOf(value); return d ? d === 'levain' : normalise(value).some(isLev); }

  var api = { mount: mount, legendNode: legendNode, isLevainFeed: isLevainFeed, eventTip: eventTip, hueKey: hueKey, noteRepo: noteRepo, repos: repos, buildModel: buildModel, normalise: normalise, diffFresh: diffFresh, stats: stats };
  if (typeof window !== 'undefined') window.LevainSky = api;
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
})();

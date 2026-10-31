// levain envelope form: the one form a person fills in to sign an envelope. Its one consumer is the sign sheet (sign.js): the
// cockpit's "Add an envelope" modal, and /sign/drop as its own page, which is also what a phone sees. It owns the size presets,
// the repo picker, the landing choice, the Custom fields, the spec the server reads, and the debounced review whose reading is
// the summary the person signs. A page passes in what differs: where the fields go, how a review is asked for, and the button
// it signs with. It decides nothing about signing: the spec keys and shapes (where, lands, stop, budget) are the ones the review
// route has always taken, and the text shown is the server's own `view` (which carries the envelope id and digest), never
// rebuilt here.
//
// A review can create the store: on a fresh install the first review makes it, and only its reply carries the store's identity.
// So while o.storeId() is null reviews are asked one at a time (the next goes out only after the one in flight returned), and a
// 409 other_store to a review sent with NO store id is asked once more after o.refreshStore() has let the page adopt the identity
// it was missing. A review sent WITH a store id that draws other_store is never retried: the page was opened for another store,
// and taking the one answering now would sign into a store the person never chose. The form says so and stops asking.
//
// The hard stop is computed by the server when it reads the review, so the summary says the time it lands on, and a summary that
// has stood for REVIEW_MAX_AGE_MS since the server read it (body.reviewed_at) is read again (never while signing, and never by
// the Sign tap itself: an await before the passkey call can cost the browser's user activation). A reply that does not say when
// it stops is not signable. Once the sheet is stopped (another store, or none named) nothing is asked again, waiting edits included.
//
// The page passes:
//   o.words        the textarea of the person's words (the page owns it); o.wordsGroup: draw it inside a "Your words" group here
//   o.left, o.right  where the groups go (the same element for one column)
//   o.repos        the known folders; setRepos() takes a later listing
//   o.review({words, spec})  a promise of {ok, status, body}, the server's reading (body.view, body.stop_at, body.reviewed_at); o.why(reply): its refusal in words
//   o.storeId()    the store identity the page holds now, or null on a fresh install; o.refreshStore(): a promise, the page re-reads it
//   o.blocked()    "" or the reason nothing can be signed (shown as the summary)
//   o.sign         the sign button. It is enabled only while ALL of these hold: no signing is under way (lock(true) not called),
//                  o.blocked() is empty, and the form's current review is the review of exactly the fields on screen (any edit
//                  retires it at once, a reply to an older form is dropped, a refusal leaves none). The button signs form.current(),
//                  never the live fields.
//   o.signWhy      a paragraph beside the button: while the button is disabled it says why (the button is tied to it by aria-describedby)
//   o.lockRoots    the elements whose inputs, selects and textareas lock(true) holds still during a signing (default: left and right)
//   o.summaryId    the summary box's id; o.debounce: ms the typing settles for before a review is asked
// create() returns {foot, current, queue, changed, lock, syncSign, setRepos, start, stop}; stop(text) halts the sheet as a store
// mismatch does, with the page's words; `foot` is the element holding the summary, where
// the page puts the buttons beneath it so they stay together.
(function () {
  "use strict";
  // The sizes an envelope is offered in. These numbers are starting points judged by levain 7 on 2026-10-31, with no
  // measurement behind them yet: tune them here, they are the only place a preset's numbers live. `stop` is the hard end;
  // `hours` are ACTIVE agent hours (not the clock); `tool_calls` and `tokens` (fresh: input + output + cache writes) are the
  // budget gauge, which pauses new work when crossed and asks for an extension. Every `stop` must be one of STOPS (checked at load).
  const SIZES = {
    quick:    { label: "Quick",    stop: "2h",  hours: 1, tool_calls: 200,  tokens: 2000000 },
    standard: { label: "Standard", stop: "8h",  hours: 3, tool_calls: 800,  tokens: 8000000 },
    long:     { label: "Long",     stop: "24h", hours: 8, tool_calls: 2500, tokens: 25000000 },
  };
  const STOPS = ["1h", "2h", "4h", "8h", "12h", "24h"];
  // the server's bounds on each budget count (levain/coordinator/envelope.py's BUDGET_MAX): a bigger number is refused there, so it is not asked for here
  const BUDGET_MAX = { tool_calls: 100000, tokens: 1000000000, hours: 24 };
  for (const [k, p] of Object.entries(SIZES)) if (STOPS.indexOf(p.stop) < 0) throw new Error("the " + k + " size's stop " + p.stop + " is not one of the Custom stops");
  const el = (tag, cls, text) => {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  };
  const OTHER = "\u0000other";
  const baseName = (p) => p.replace(/\/+$/, "").split("/").pop() || p;
  const whyDefault = (r) => ((r.body && typeof r.body.message === "string" && r.body.message.replace(/[.\s]+$/, "")) || ("refused (HTTP " + r.status + ")"));
  // the server ends a refusal with what did not happen; before any signing that is always so, and the form says what to do instead
  const bare = (m) => String(m).replace(/[.\s]+$/, "").replace(/\s*[:;,]\s*(nothing was signed|the drop was not delivered)$/i, "").replace(/[.\s]+$/, "");
  const REVIEW_MAX_AGE_MS = 5 * 60000;
  const AGE_CHECK_MS = 10000;
  const MOVED = "This sheet was opened for a different Levain store than the one answering now. Reload the page to see the current store; nothing was signed.";
  const NO_STORE = "Levain has not said which store answers here yet. Reload the page to read it again; nothing was signed.";
  const EMPTY = "The server sent an empty summary; nothing can be signed. Change anything to ask again.";
  const NO_ANSWER = "The cockpit did not answer; change anything to ask again.";
  const NO_STOP = "The server did not say when this envelope would stop; nothing can be signed. Change anything to ask again.";
  const STALE = "The server's summary was over 5 minutes old when it arrived, twice (this device's clock may be off); nothing can be signed. Change anything to ask again.";
  // a promise of what f returns; a throw becomes a rejection, so a caller's handlers see it
  const attempt = (f) => { try { return Promise.resolve(f()); } catch (e) { return Promise.reject(e); } };
  // the server's fixed-width UTC stamp ("2026-10-31T12:00:00.000000Z") as a Date, read field by field so no browser's parser decides
  const stampDate = (t) => {
    const m = typeof t === "string" ? /^(\d{4})-(\d\d)-(\d\d)T(\d\d):(\d\d):(\d\d)(?:\.\d+)?Z$/.exec(t) : null;
    return m ? new Date(Date.UTC(+m[1], +m[2] - 1, +m[3], +m[4], +m[5], +m[6])) : null;
  };
  // the stop in the person's own clock: "14:05", or "14:05 tomorrow"
  const localStop = (d) => {
    const hm = d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
    return d.toDateString() === new Date(Date.now()).toDateString() ? hm : hm + (d.getTime() > Date.now() ? " tomorrow" : "");
  };
  // the server's reading as lines to show, or null when there is nothing to show (an empty, null or missing view)
  const viewText = (view) => {
    if (typeof view === "string") return view.trim() ? view : null;
    if (!Array.isArray(view) || !view.length || !view.every((x) => typeof x === "string")) return null;
    const t = view.join("\n");
    return t.trim() ? t : null;
  };

  function create(o) {
    const words = o.words;
    const why = o.why || whyDefault;
    const blocked = o.blocked || (() => "");
    const sign = o.sign;
    const lockRoots = o.lockRoots || [o.left, o.right];
    const storeNull = () => typeof o.storeId === "function" && o.storeId() === null;
    let current = null;             // the review the button signs: {id, body_text, digest, words, spec, reply}; null whenever the form has changed since
    let state = { kind: "bad", text: "" };      // why there is no current review: bad (the form is incomplete), reading, or refused
    let working = false;
    let seq = 0, timer = null;
    let inflight = false, waiting = null;
    let stuck = "";                 // set once the store answering is not the one this sheet was opened for, or none is named: nothing is asked again
    let reasked = false;            // the last review was re-asked because it arrived already too old: a second such one is not re-asked
    let ageTimer = null;            // the one timer that re-reads a summary older than REVIEW_MAX_AGE_MS
    let started = false;
    let touchedRepo = false;
    let n = 0;
    const uid = (p) => "env-" + p + "-" + (n += 1);

    // a group: a legend, one hint line, a slot for the server's refusal when it names this group, then its controls
    const group = (gid, title, hint) => {
      const g = el("fieldset", "env-group");
      g.id = gid;
      g.appendChild(el("legend", "env-label", title));
      if (hint) {
        const h = el("p", "env-hint", hint);
        h.id = uid("hint");
        g.setAttribute("aria-describedby", h.id);
        g.appendChild(h);
      }
      const err = el("p", "env-err");
      err.hidden = true;
      err.setAttribute("role", "alert");
      g.appendChild(err);
      g.err = err;
      return g;
    };
    const describe = (node, ...ids) => node.setAttribute("aria-describedby", ids.join(" "));
    const reasonNow = () => {
      if (working) return "Signing is under way.";
      const b = blocked();
      if (b) return b;
      if (current) return "";
      if (state.kind === "bad") return state.text;
      if (state.kind === "refused") return "The server refused this envelope; its message is on the form.";
      if (state.kind === "stuck") return stuck;
      return "Waiting for the server's reading of what you would sign.";
    };
    const syncSign = () => {
      if (sign) sign.disabled = working || !!blocked() || !current;
      if (o.signWhy) o.signWhy.textContent = sign && sign.disabled ? reasonNow() : "";
    };
    if (sign && o.signWhy) sign.setAttribute("aria-describedby", o.signWhy.id);
    function changed() { queue(); }

    o.left.appendChild(el("p", "env-intro", "An envelope is a permission slip you sign: which folder it may work in, whether it may land, for how long, and how much it may do."));
    let gWords = null;
    if (o.wordsGroup) {
      gWords = group("env-words", "Your words");
      gWords.appendChild(words);
      o.left.appendChild(gWords);
    }
    words.addEventListener("input", () => changed());

    // ---- where it may work: a picker over the folders this store knows, "Another folder…" last ----
    const gRepo = group("env-repo", "Where it may work", "The repo the coordinator may change. Nothing outside it.");
    const repo = el("input");           // the typed path: shown only for "Another folder…" (or when no folder is known)
    repo.id = "drop-repo";
    repo.type = "text";
    repo.placeholder = "/Users/you/project";
    repo.setAttribute("aria-label", "the full path of a folder");
    repo.setAttribute("autocapitalize", "off");
    repo.setAttribute("autocorrect", "off");
    repo.spellcheck = false;
    const repoOther = el("div", "env-other");
    const repoHint = el("p", "env-hint", "the full path of a folder on this machine, e.g. /Users/you/project");
    repoHint.id = uid("hint");
    describe(repo, repoHint.id);
    repoOther.append(repo, repoHint);
    const pickHolder = el("div", "");
    // the pre-pick (the listing's first folder: the most recently active thread's) is named aloud until the person chooses or types
    const repoDefault = el("p", "env-hint", "");
    repoDefault.id = "env-repo-default";
    gRepo.append(pickHolder, repoDefault, repoOther);
    let repoList = [];
    let repoSel = OTHER;
    let repoPaint = () => undefined;
    const paintDefault = () => {
      repoDefault.hidden = touchedRepo || !repoList.length || repoSel === OTHER;
      repoDefault.textContent = repoDefault.hidden ? "" : "Pre-picked: " + baseName(repoSel) + " (" + repoSel + "), the folder of your most recently active thread. Change it if that's wrong.";
    };
    const setRepo = (v) => { repoSel = v; repoOther.hidden = v !== OTHER; repoPaint(); paintDefault(); };
    const pick = (v) => { touchedRepo = true; setRepo(v); changed(); };
    // the folders again (a page that reads its listing more than once): drawn only when they changed, the choice kept while it
    // is still offered, and the first folder taken until the person has chosen
    function setRepos(list) {
      const next = (Array.isArray(list) ? list : []).filter((r) => typeof r === "string" && r);
      if (JSON.stringify(next) === JSON.stringify(repoList) && pickHolder.firstChild) return;
      repoList = next;
      pickHolder.replaceChildren();
      repoPaint = () => undefined;
      if (repoList.length) {
        const choices = repoList.concat([OTHER]);
        if (repoList.length <= 4) {
          const grp = el("div", "env-opts");
          grp.id = "drop-repo-pick";
          grp.setAttribute("role", "radiogroup");
          const radios = [];
          for (const v of choices) {
            const lab = el("label", "env-opt");
            const rb = el("input");
            rb.type = "radio";
            rb.name = "drop-repo-pick";
            rb.value = v;
            rb.addEventListener("change", () => { if (rb.checked) pick(v); });
            const t = el("span", "env-opt-t");
            t.appendChild(el("span", "env-opt-name", v === OTHER ? "Another folder…" : baseName(v)));
            if (v !== OTHER) t.appendChild(el("span", "ext-meta env-opt-sub", v));
            lab.append(rb, t);
            grp.appendChild(lab);
            radios.push(rb);
          }
          repoPaint = () => { for (const rb of radios) rb.checked = rb.value === repoSel; };
          pickHolder.appendChild(grp);
        } else {
          const sel = el("select");
          sel.id = "drop-repo-pick";
          sel.setAttribute("aria-label", "where it may work");
          for (const v of choices) { const op = el("option", "", v === OTHER ? "Another folder…" : baseName(v) + " — " + v); op.value = v; sel.appendChild(op); }
          sel.addEventListener("change", () => pick(sel.value));
          repoPaint = () => { sel.value = repoSel; };
          pickHolder.appendChild(sel);
        }
      }
      // the listing puts the threads' own folders first, the most recently active first; the pick is the first
      const keep = repoSel === OTHER ? touchedRepo : repoList.indexOf(repoSel) >= 0;
      const before = repoSel;
      setRepo(keep ? repoSel : (repoList.length ? repoList[0] : OTHER));
      if (started && before !== repoSel) changed();
    }
    // typing a path is the person's own choice: a later listing never moves it
    repo.addEventListener("input", () => { touchedRepo = true; paintDefault(); changed(); });

    // ---- landing: radios, never on by default ----
    const gLand = group("env-land", "Landing", "Landing lets it merge finished, reviewed work onto that branch by itself.");
    let landSel = "none";
    const onto = el("input");           // a bare branch name, for "Land onto another branch…"
    onto.id = "drop-onto";
    onto.type = "text";
    onto.placeholder = "branch name, e.g. release";
    onto.setAttribute("aria-label", "the branch to land onto");
    onto.setAttribute("autocapitalize", "off");
    onto.setAttribute("autocorrect", "off");
    onto.spellcheck = false;
    const ontoWrap = el("div", "env-other");
    ontoWrap.hidden = true;
    ontoWrap.appendChild(onto);
    for (const [v, text] of [["none", "Don't land — it works on a branch and you merge"], ["main", "Land finished work onto main"], ["other", "Land onto another branch…"]]) {
      const lab = el("label", "env-opt");
      const rb = el("input");
      rb.type = "radio";
      rb.name = "drop-land";
      rb.id = "drop-land-" + v;
      rb.value = v;
      rb.checked = v === landSel;
      rb.addEventListener("change", () => { if (rb.checked) { landSel = v; ontoWrap.hidden = v !== "other"; changed(); } });
      lab.append(rb, el("span", "env-opt-t", text));
      gLand.appendChild(lab);
    }
    gLand.appendChild(ontoWrap);
    onto.addEventListener("input", () => changed());

    // ---- size: presets (native radios drawn as a segmented control), and Custom (the one fold) ----
    const gSize = group("env-size", "Size", "How long it may run and how much it may do. The limits count across the whole thread, all its workers together; extending sets a new total.");
    const seg = el("div", "env-seg");
    seg.id = "drop-size";
    seg.setAttribute("role", "radiogroup");
    const sizeRadios = {};
    for (const k of Object.keys(SIZES).concat(["custom"])) {
      const lab = el("label", "env-seg-opt");
      const rb = el("input");
      rb.type = "radio";
      rb.name = "drop-size";
      rb.id = "drop-size-" + k;
      rb.value = k;
      // a radio that is already chosen fires no change, and selectSize ignores the size it is already on
      rb.addEventListener("change", () => { if (rb.checked) selectSize(k); });
      lab.append(rb, el("span", "env-seg-t", k === "custom" ? "Custom" : SIZES[k].label));
      sizeRadios[k] = rb;
      seg.appendChild(lab);
    }
    const sizeLine = el("p", "env-hint");
    sizeLine.id = "drop-size-line";
    const custom = el("div", "env-custom");
    custom.id = "drop-custom";
    const stop = el("select");
    const nums = {};
    const warns = {};
    const infield = (fid, label, hint, node) => {
      const w = el("div", "env-field");
      const lab = el("label", "env-flabel", label);
      lab.htmlFor = fid;
      node.id = fid;
      const h = el("span", "env-hint", hint);
      h.id = fid + "-hint";
      describe(node, h.id);
      w.append(lab, node, h);
      custom.appendChild(w);
      return w;
    };
    infield("drop-stop", "Hard stop after", "counted from when the summary is read, not from when you sign; it stops no matter what", stop);
    for (const h of STOPS) stop.appendChild(el("option", "", h));
    for (const [k, key, label, noun, hint] of [
      ["hours", "hours", "Active hours", "active hours", "time agents are actually working, not the clock"],
      ["calls", "tool_calls", "Tool calls", "tool calls", "each command, file read or edit counts one"],
      ["tokens", "tokens", "Fresh tokens", "fresh tokens", "fresh tokens: input, output and new cache writes (cache reads don't count)"]]) {
      const node = el("input");
      const w = infield("drop-" + k, label, hint, node);
      node.type = "number";
      node.step = "1";
      node.min = "0";
      node.max = String(BUDGET_MAX[key]);
      node.setAttribute("inputmode", "numeric");
      const warn = el("span", "env-warn");
      warn.id = "drop-" + k + "-warn";
      warn.hidden = true;
      w.appendChild(warn);
      node.addEventListener("input", () => {
        const zero = node.value.trim() === "0";
        warn.hidden = !zero;
        warn.textContent = zero ? "0 " + noun + " means nothing can start" : "";
        describe(node, node.id + "-hint", ...(zero ? [warn.id] : []));
        changed();
      });
      node.zeroText = () => (node.value.trim() === "0" ? "0 " + noun + " means nothing can start" : "");
      node.warnEl = warn;
      node.key = key;
      node.noun = noun;
      nums[k] = node;
    }
    stop.addEventListener("change", () => changed());
    let size = null;
    function selectSize(k) {
      if (k === size) return;
      size = k;
      if (k !== "custom") {
        // the raw fields follow the preset chosen, so Custom opens on real numbers and never on blanks
        const p = SIZES[k];
        stop.value = p.stop;
        nums.hours.value = String(p.hours);
        nums.calls.value = String(p.tool_calls);
        nums.tokens.value = String(p.tokens);
        for (const x of Object.values(nums)) { x.warnEl.hidden = true; x.warnEl.textContent = ""; describe(x, x.id + "-hint"); }
        sizeLine.textContent = p.label + ": hard stop after " + p.stop + ", " + p.hours + (p.hours === 1 ? " active hour" : " active hours") + ", "
          + p.tool_calls.toLocaleString("en-US") + " tool calls, " + (p.tokens / 1e6) + "M fresh tokens. These numbers are starting points; Custom sets your own.";
      } else sizeLine.textContent = "Set each limit yourself; the boxes start from the size you last picked.";
      custom.hidden = k !== "custom";
      for (const [bk, rb] of Object.entries(sizeRadios)) rb.checked = bk === k;
      if (started) changed();
    }
    const limitNotes = el("div", "env-notes");
    limitNotes.append(el("p", "env-hint", "Reaching a limit: no new workers start until you extend. Nothing running is killed."),
      el("p", "env-hint", "Hard stop: it ends at this time regardless."));
    gSize.append(seg, sizeLine, custom, limitNotes);

    // ---- what you sign: the server's own reading of the envelope, fetched as the form changes ----
    const gSum = group("env-sum", "What you sign");
    const summary = el("pre", "sheet-message env-summary");
    summary.id = o.summaryId || "drop-review";
    // only a settled reading is announced; "Reading…" is not, so a screen reader hears one result per change
    const live = el("div", "env-sr");
    live.id = "env-live";
    live.setAttribute("aria-live", "polite");
    gSum.append(summary, live);
    gSum.appendChild(gSum.err);        // a refusal that names no field sits under the summary, beside the sign button
    const foot = el("div", "env-foot");
    foot.appendChild(gSum);

    o.left.append(gRepo, gLand);
    o.right.append(gSize, foot);
    setRepos(o.repos);
    selectSize("standard");

    // ---- the form as the spec the server reads: the same four keys (where, lands, stop, budget) and shapes as ever ----
    // {spec, words} when the form is complete, else {bad: what is missing}; the preset's numbers come from SIZES, Custom's from its boxes
    function specNow() {
      const w = words.value;
      if (!w.trim()) return { bad: "Add your words to see what you would sign." };
      const r = repoSel === OTHER ? repo.value.trim() : repoSel;
      if (!r) return { bad: "Name the folder it may work in (its full path)." };
      if (r.charAt(0) !== "/") return { bad: "The folder is a full path, starting with /." };
      let lands = [];
      if (landSel === "main") lands = [{ repo: r, onto: "main" }];
      else if (landSel === "other") {
        const b = onto.value.trim();
        if (!b) return { bad: "Name the branch it may land onto." };
        lands = [{ repo: r, onto: b }];
      }
      let stopV, budget;
      if (size === "custom") {
        const vals = {};
        for (const x of Object.values(nums)) {
          const t = x.value.trim();
          if (t === "") return { bad: "Fill in every limit under Custom (a typed 0 means none allowed)." };
          // the server's own words for a count it will not take
          if (!/^\d+$/.test(t) || Number(t) > BUDGET_MAX[x.key]) return { bad: "budget." + x.key + " is an integer from 0 to " + BUDGET_MAX[x.key] };
          vals[x.key] = Number(t);
        }
        stopV = stop.value;
        budget = { tool_calls: vals.tool_calls, tokens: vals.tokens, hours: vals.hours };
      } else {
        const p = SIZES[size];
        stopV = p.stop;
        budget = { tool_calls: p.tool_calls, tokens: p.tokens, hours: p.hours };
      }
      return { words: w, spec: { where: [{ repo: r, paths: ["."] }], lands: lands, stop: stopV, budget: budget } };
    }
    const groups = [gWords, gRepo, gLand, gSize, gSum].filter((g) => g);
    const clearErrs = () => { for (const g of groups) { g.err.hidden = true; g.err.textContent = ""; } };
    // the field a refusal names, only when the server's reply carries one as a structured key; never guessed from its words
    const FIELD_GROUP = { where: gRepo, lands: gLand, stop: gSize, budget: gSize, words: gWords };
    const namedGroup = (body) => {
      for (const k of ["field", "key"]) {
        if (body && typeof body[k] === "string") {
          const g = FIELD_GROUP[body[k].split(/[.\[]/)[0]];
          if (g) return g;
        }
      }
      return null;
    };
    const zeroLines = () => (size === "custom" ? Object.values(nums).map((x) => x.zeroText()).filter(Boolean) : []);
    const showReview = (text, wait) => {
      const z = zeroLines();
      summary.textContent = z.length ? text + "\n\nWarning: " + z.join("; ") + "." : text;
      summary.classList.toggle("env-wait", !!wait);
    };
    const noAge = () => { clearInterval(ageTimer); ageTimer = null; };
    // a summary over REVIEW_MAX_AGE_MS old is read again, so the hard stop it states is never long past; never while signing
    const watchAge = () => {
      noAge();
      ageTimer = setInterval(() => {
        if (working || !current) { noAge(); return; }
        if (Date.now() - current.readAt >= REVIEW_MAX_AGE_MS) { noAge(); queue(0); }
      }, AGE_CHECK_MS);
    };
    window.addEventListener("pagehide", noAge);
    // only called on a reply whose stop_at reads as a time
    const stopLine = (c, v) => "Hard stop at " + localStop(stampDate(c.body.stop_at)) + ": set when this summary was read, " + v.spec.stop
      + " from then (a summary over 5 minutes old is read again).";
    const say = (kind, text) => { state = { kind: kind, text: text }; showReview(text, true); syncSign(); };
    // any change to the form: the review in hand is no longer the form's, and a new one is asked for once the typing settles
    function queue(ms) {
      current = null;
      noAge();
      seq += 1;
      waiting = null;
      clearTimeout(timer);
      clearErrs();
      live.textContent = "";
      if (stuck) { say("stuck", stuck); return; }
      const b = blocked();
      if (working || b) { state = { kind: "bad", text: "" }; showReview(b, true); syncSign(); return; }
      const v = specNow();
      if (v.bad) { say("bad", v.bad); return; }
      state = { kind: "reading" };
      showReview("Reading what you would sign…", true);
      syncSign();
      const mineSeq = seq;
      timer = setTimeout(() => { if (!working && mineSeq === seq) ask(v, mineSeq); }, ms === undefined ? (o.debounce === undefined ? 500 : o.debounce) : ms);
    }
    // the sheet stops for good: whatever was in hand or waiting is dropped, and nothing is asked again until a reload
    function halt(text) {
      stuck = text;
      current = null;
      waiting = null;
      clearTimeout(timer);
      noAge();
      say("stuck", stuck);
    }
    function next() {
      inflight = false;
      if (stuck) { waiting = null; return; }
      if (waiting) { const w = waiting; waiting = null; ask(w.v, w.mineSeq); }
    }
    function ask(v, mineSeq) {
      if (stuck) { waiting = null; return; }
      if (mineSeq !== seq) { if (!inflight && waiting) next(); return; }
      if (inflight && storeNull()) { waiting = { v: v, mineSeq: mineSeq }; return; }      // one at a time until the store has an identity
      inflight = true;
      // the slot is released once, after every way this request (and its one retry) can end, a throw included
      send(v, mineSeq, false).catch(() => { if (mineSeq === seq && !stuck) say("bad", NO_ANSWER); }).finally(next);
    }
    // one review request and what its reply means; resolves when it (and any retry) is done
    function send(v, mineSeq, retried) {
      const sentNull = storeNull();         // read in the same turn the page reads its store id for the request
      return attempt(() => o.review({ words: v.words, spec: v.spec })).then((c) => {
        if (stuck) return undefined;                    // the sheet has stopped: no reply speaks after that
        // which store answers is a fact about the sheet, not about one form: it is acted on even for a reply the form has moved past
        if (c && typeof c === "object" && !c.ok && c.status === 409 && c.body && typeof c.body === "object" && c.body.error === "other_store") {
          // sent with a store id: the store answering is another one. Refused, and never asked again from this sheet
          if (!sentNull) { halt(MOVED); return undefined; }
          // sent with none (a fresh install whose store was made meanwhile): read the store's identity, then ask once more
          if (retried) { halt(NO_STORE); return undefined; }
          const read = typeof o.refreshStore === "function" ? attempt(() => o.refreshStore()) : Promise.resolve();
          return read.then(() => undefined, () => undefined).then(() => {
            if (stuck) return undefined;
            if (storeNull()) { halt(NO_STORE); return undefined; }      // asking again, for this form or a newer one, would draw the same 409
            if (mineSeq !== seq) return undefined;                       // the newer form is asked for next, with the identity now held
            return send(v, mineSeq, true);
          });
        }
        if (mineSeq !== seq) return undefined;          // the form moved on while this was read: the newer one speaks
        if (!c || typeof c !== "object" || !c.body || typeof c.body !== "object") { say("bad", c && c.ok ? EMPTY : NO_ANSWER); return undefined; }
        if (!c.ok) {
          const m = bare(why(c));
          state = { kind: "refused" };
          showReview("This cannot be signed yet.", true);
          const g = namedGroup(c.body) || gSum;
          const down = c.status === 0 || c.status >= 500;      // the cockpit's own fault: asking again may work
          g.err.textContent = down ? "The cockpit could not read this envelope (" + m + "); change anything to ask again."
            : (g === gSum ? "The server refused this envelope: " : "") + m;
          g.err.hidden = false;
          syncSign();
          if (typeof g.scrollIntoView === "function") g.scrollIntoView({ block: "nearest" });
          return undefined;
        }
        // nothing is signed on a reading the person was not shown
        const text = viewText(c.body.view);
        if (text === null || typeof c.body.review_id !== "string" || !c.body.review_id) { say("bad", EMPTY); return undefined; }
        // nor on one that does not say when it stops: the stop is part of what is signed
        if (!stampDate(c.body.stop_at)) { say("bad", NO_STOP); return undefined; }
        // aged from when the server read it (a slow reply arrives already old); from its arrival only when the server did not say
        const readOn = stampDate(c.body.reviewed_at);
        const readAt = readOn ? readOn.getTime() : Date.now();
        if (Date.now() - readAt >= REVIEW_MAX_AGE_MS) {
          // too old to sign on arrival: asked again at once, but only once in a row, so a device whose clock is off never loops
          if (reasked) { reasked = false; say("bad", STALE); return undefined; }
          reasked = true;
          queue(0);
          return undefined;
        }
        reasked = false;
        current = { id: c.body.review_id, body_text: c.body.body_text, digest: c.body.digest, words: v.words, spec: v.spec, reply: c, readAt: readAt };
        const sl = stopLine(c, v);
        showReview(text + "\n\n" + sl, false);
        live.textContent = summary.textContent;
        syncSign();
        watchAge();
        return undefined;
      }, () => { if (mineSeq === seq && !stuck) say("bad", NO_ANSWER); return undefined; });
    }
    // a control held still while a signing is under way; the page says when
    function lock(on) {
      working = on;
      if (on) noAge();
      else if (current) watchAge();
      for (const root of lockRoots) for (const x of root.querySelectorAll("input, select, textarea")) x.disabled = on;
      syncSign();
    }
    return {
      words: words, foot: foot, current: () => current, queue: queue, changed: changed, lock: lock, syncSign: syncSign, setRepos: setRepos, stop: halt,
      start: () => { started = true; queue(); },
    };
  }

  window.LevainEnvelopeForm = { SIZES: SIZES, create: create };
})();

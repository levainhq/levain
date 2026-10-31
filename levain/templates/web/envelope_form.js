// levain envelope form: the one form a person fills in to sign an envelope, shared by the sign sheet (sign.js, the cockpit's
// "Add an envelope" and the sheet's own page) and the phone app (phone.js). It owns the size presets, the repo picker, the
// landing choice, the Custom fields, the spec the server reads, and the debounced review whose reading is the summary the
// person signs. A page passes in what differs: where the fields go, how a review is asked for, and the button it signs with.
// It decides nothing about signing: the spec keys and shapes (where, lands, stop, budget) are the ones the review route has
// always taken, and the text shown is the server's own `view`, never rebuilt here.
(function () {
  "use strict";
  // The sizes an envelope is offered in. These numbers are starting points judged by levain 7 on 2026-10-31, with no
  // measurement behind them yet: tune them here, they are the only place a preset's numbers live. `stop` is the hard end;
  // `hours` are ACTIVE agent hours (not the clock); `tool_calls` and `tokens` (fresh: input + output + cache writes) are the
  // budget gauge, which pauses new work when crossed and asks for an extension.
  const SIZES = {
    quick:    { label: "Quick",    stop: "2h",  hours: 1, tool_calls: 200,  tokens: 2000000 },
    standard: { label: "Standard", stop: "8h",  hours: 3, tool_calls: 800,  tokens: 8000000 },
    long:     { label: "Long",     stop: "24h", hours: 8, tool_calls: 2500, tokens: 25000000 },
  };
  const el = (tag, cls, text) => {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  };
  const OTHER = "\u0000other";
  const baseName = (p) => p.replace(/\/+$/, "").split("/").pop() || p;
  const whyDefault = (r) => ((r.body && typeof r.body.message === "string" && r.body.message.replace(/[.\s]+$/, "")) || ("refused (HTTP " + r.status + ")"));

  // o.words: the textarea of the person's words (the page owns it); o.wordsGroup: draw it inside a "Your words" group here
  // o.left / o.right: where the groups go (the same element for one column); o.repos: the known folders
  // o.review({words, spec}): a promise of {ok, status, body}, the server's reading; o.why(reply): its refusal in words
  // o.blocked(): "" or the reason nothing can be signed (shown as the summary); o.sign: the button, enabled only while a review is current
  // o.summaryId: the summary box's id; o.debounce: ms the typing settles for before a review is asked
  function create(o) {
    const words = o.words;
    const why = o.why || whyDefault;
    const blocked = o.blocked || (() => "");
    const sign = o.sign;
    const lockRoots = o.lockRoots || [o.left, o.right];
    let current = null;             // the review the button signs: {id, body_text, digest, words, spec, reply}; null whenever the form has changed since
    let working = false;
    let seq = 0, timer = null;
    let started = false;
    let touchedRepo = false;

    // a group: a legend, one hint line, a slot for the server's refusal when it names this group, then its controls
    const group = (gid, title, hint) => {
      const g = el("fieldset", "env-group");
      g.id = gid;
      g.appendChild(el("legend", "env-label", title));
      if (hint) g.appendChild(el("p", "env-hint", hint));
      const err = el("p", "env-err");
      err.hidden = true;
      err.setAttribute("role", "alert");
      g.appendChild(err);
      g.err = err;
      return g;
    };
    const syncSign = () => { if (sign) sign.disabled = working || !!blocked() || !current; };
    function changed() { queue(); }

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
    repoOther.append(repo, el("p", "env-hint", "the full path of a folder on this machine, e.g. /Users/you/project"));
    const pickHolder = el("div", "");
    gRepo.append(pickHolder, repoOther);
    let repoList = [];
    let repoSel = OTHER;
    let repoPaint = () => undefined;
    const setRepo = (v) => { repoSel = v; repoOther.hidden = v !== OTHER; repoPaint(); };
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
          grp.setAttribute("aria-label", "where it may work");
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
      // the listing puts the threads' own folders first; the only one is the first
      const keep = repoSel === OTHER ? touchedRepo : repoList.indexOf(repoSel) >= 0;
      const before = repoSel;
      setRepo(keep ? repoSel : (repoList.length ? repoList[0] : OTHER));
      if (started && before !== repoSel) changed();
    }
    repo.addEventListener("input", () => changed());

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

    // ---- size: presets, and Custom (the one fold) ----
    const gSize = group("env-size", "Size", "How long it may run and how much it may do.");
    const seg = el("div", "env-seg");
    seg.id = "drop-size";
    seg.setAttribute("role", "radiogroup");
    seg.setAttribute("aria-label", "size");
    const sizeBtns = {};
    for (const k of Object.keys(SIZES).concat(["custom"])) {
      const b = el("button", "env-seg-btn", k === "custom" ? "Custom" : SIZES[k].label);
      b.type = "button";
      b.id = "drop-size-" + k;
      b.setAttribute("role", "radio");
      b.addEventListener("click", () => { selectSize(k); });
      sizeBtns[k] = b;
      seg.appendChild(b);
    }
    const sizeLine = el("p", "env-hint");
    sizeLine.id = "drop-size-line";
    const custom = el("div", "env-custom");
    custom.id = "drop-custom";
    const stop = el("select");
    const nums = {};
    const infield = (fid, label, hint, node) => {
      const w = el("label", "env-field");
      node.id = fid;
      w.append(el("span", "env-flabel", label), node, el("span", "env-hint", hint));
      custom.appendChild(w);
      return node;
    };
    infield("drop-stop", "Ends by", "a hard end: after this the envelope is void", stop);
    for (const h of ["1h", "2h", "4h", "8h", "12h", "24h"]) stop.appendChild(el("option", "", h));
    for (const [k, label, hint] of [["hours", "Active hours", "time agents are actually working, not the clock"], ["calls", "Tool calls", "each command, file read or edit counts one"],
                                    ["tokens", "Fresh tokens", "model input + output; cache reads are not counted"]]) {
      const n = infield("drop-" + k, label, hint, el("input"));
      n.type = "number";
      n.min = "0";
      n.setAttribute("inputmode", "numeric");
      n.setAttribute("aria-label", label.toLowerCase());
      n.addEventListener("input", () => changed());
      nums[k] = n;
    }
    stop.addEventListener("change", () => changed());
    let size = "standard";
    function selectSize(k) {
      size = k;
      if (k !== "custom") {
        // the raw fields follow the preset chosen, so Custom opens on real numbers and never on blanks
        const p = SIZES[k];
        stop.value = p.stop;
        nums.hours.value = String(p.hours);
        nums.calls.value = String(p.tool_calls);
        nums.tokens.value = String(p.tokens);
        sizeLine.textContent = p.label + ": ends by " + p.stop + " at the latest, " + p.hours + (p.hours === 1 ? " active hour" : " active hours") + ", "
          + p.tool_calls.toLocaleString("en-US") + " tool calls, " + (p.tokens / 1e6) + "M fresh tokens.";
      } else sizeLine.textContent = "Set each limit yourself; the boxes start from the size you last picked.";
      custom.hidden = k !== "custom";
      for (const [bk, b] of Object.entries(sizeBtns)) b.setAttribute("aria-checked", bk === k ? "true" : "false");
      if (started) changed();
    }
    gSize.append(seg, sizeLine, custom, el("p", "env-hint", "Reaching a limit pauses new work and asks you to extend it; nothing running is killed."));

    // ---- what you sign: the server's own reading of the envelope, fetched as the form changes ----
    const gSum = group("env-sum", "What you sign");
    const summary = el("pre", "sheet-message env-summary");
    summary.id = o.summaryId || "drop-review";
    summary.setAttribute("aria-live", "polite");
    gSum.appendChild(summary);

    o.left.append(gRepo, gLand);
    o.right.append(gSize, gSum);
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
        for (const k of ["hours", "calls", "tokens"]) {
          const t = nums[k].value.trim();
          if (t === "" || !Number.isFinite(Number(t)) || Number(t) < 0) return { bad: "Fill in every limit under Custom (a typed 0 means none allowed)." };
          vals[k] = Number(t);
        }
        stopV = stop.value;
        budget = { tool_calls: vals.calls, tokens: vals.tokens, hours: vals.hours };
      } else {
        const p = SIZES[size];
        stopV = p.stop;
        budget = { tool_calls: p.tool_calls, tokens: p.tokens, hours: p.hours };
      }
      return { words: w, spec: { where: [{ repo: r, paths: ["."] }], lands: lands, stop: stopV, budget: budget } };
    }
    const groups = [gWords, gRepo, gLand, gSize, gSum].filter((g) => g);
    const clearErrs = () => { for (const g of groups) { g.err.hidden = true; g.err.textContent = ""; } };
    // the server's refusal beside the field it names (its own words); one that names none sits under the summary
    const groupFor = (m) => /\bland|onto|branch|merge/i.test(m) ? gLand : /repo|path|folder|where|absolute|outside/i.test(m) ? gRepo
      : /budget|token|hours|tool.?calls|stop|lifetime|span|expire|too long/i.test(m) ? gSize : (gWords && /words|drop|empty/i.test(m)) ? gWords : gSum;
    const showReview = (text, wait) => { summary.textContent = text; summary.classList.toggle("env-wait", !!wait); };
    // any change to the form: the review in hand is no longer the form's, and a new one is asked for once the typing settles
    function queue(ms) {
      current = null;
      seq += 1;
      clearTimeout(timer);
      clearErrs();
      syncSign();
      const b = blocked();
      if (working || b) { showReview(b, true); return; }
      const v = specNow();
      if (v.bad) { showReview(v.bad, true); return; }
      showReview("Reading what you would sign…", true);
      const mineSeq = seq;
      timer = setTimeout(() => {
        if (working || mineSeq !== seq) return;
        o.review({ words: v.words, spec: v.spec }).then((c) => {
          if (mineSeq !== seq) return;              // the form moved on while this was read: the newer one speaks
          if (!c.ok) {
            const m = why(c);
            showReview("This cannot be signed yet.", true);
            const g = groupFor(m);
            g.err.textContent = m;
            g.err.hidden = false;
            return;
          }
          current = { id: c.body.review_id, body_text: c.body.body_text, digest: c.body.digest, words: v.words, spec: v.spec, reply: c };
          showReview(Array.isArray(c.body.view) ? c.body.view.join("\n") : "", false);
          syncSign();
        }, () => { if (mineSeq === seq) showReview("The cockpit did not answer; change anything to ask again.", true); });
      }, ms === undefined ? (o.debounce === undefined ? 500 : o.debounce) : ms);
    }
    // a control held still while a signing is under way; the page says when
    function lock(on) {
      working = on;
      for (const root of lockRoots) for (const n of root.querySelectorAll("input, select, textarea, .env-seg button")) n.disabled = on;
      syncSign();
    }
    return {
      words: words, current: () => current, queue: queue, changed: changed, lock: lock, syncSign: syncSign, setRepos: setRepos,
      start: () => { started = true; queue(); },
    };
  }

  window.LevainEnvelopeForm = { SIZES: SIZES, create: create };
})();

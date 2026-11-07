// The agents panel's page half (P9b): a row of the Agents card in the Build tab opens a detail view offering the verbs the
// cockpit offered on that row, and only those. The panel itself is the generic list the manifest mapper draws
// (cockpit_view.js); this script adds the row's open gesture and the detail, and never writes a verb of its own: every act
// goes through the page's one write transport (LevainCockpitTransport.rowVerb), which posts the row's own offered action.
// A row with no offered verb (the operator row, a retired agent, an agent whose state allows none) says so and offers nothing.
(function (root) {
  "use strict";
  const doc = root.document;
  if (!doc) return;
  const PANEL = "agents";
  // the verbs a row can offer, in the order the detail draws them. `sure` is the second tap a verb that cannot be undone asks.
  const VERBS = [
    { verb: "agent_pause", label: "pause", gloss: "stop this agent firing until it is resumed" },
    { verb: "agent_resume", label: "resume", gloss: "let this agent fire again" },
    { verb: "agent_renew", label: "renew", gloss: "move the expiry of its signed link", sure: "renew the link" },
    { verb: "agent_retire", label: "retire", gloss: "retire it for good: it never fires again", sure: "retire for good" },
  ];

  function el(tag, cls, text) {
    const n = doc.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined && text !== null) n.textContent = text;
    return n;
  }

  // the Agents card's rows are found by comparing dataset values, never by building a selector from a row id
  function rows() {
    const card = Array.from(doc.querySelectorAll("[data-ck-panel]")).find((p) => p.dataset.ckPanel === PANEL);
    return card ? Array.from(card.querySelectorAll(".row[data-keep-key]")) : [];
  }
  function rowFor(id) { return rows().find((r) => r.dataset.keepKey === id) || null; }

  function wordsOf(row) {
    const clause = row.querySelector(".clause");
    const meta = row.querySelector(".ext-meta");
    return { group: meta ? meta.textContent.trim() : "", body: clause ? clause.textContent.trim() : "" };
  }
  const nameOf = (body) => (body.split(" · ")[0] || body).trim();

  function offered(id) {
    const T = root.LevainCockpitTransport;
    const acts = T && typeof T.rowActions === "function" ? T.rowActions(id) : [];
    const have = new Map();
    for (const a of acts) if (a && typeof a.verb === "string" && a.gesture !== "refused" && a.gesture !== "laptop-only") have.set(a.verb, a);
    return VERBS.filter((v) => have.has(v.verb));
  }

  function build(id) {
    const row = rowFor(id);
    if (!row) return null;                       // the row is gone from the page: the view closes
    const w = wordsOf(row);
    const card = el("div", "agent-detail");
    card.setAttribute("data-agent-detail", id);
    card.appendChild(el("h3", "agent-detail-h", nameOf(w.body)));
    card.appendChild(el("p", "agent-detail-group", w.group));
    card.appendChild(el("p", "agent-detail-body", w.body));
    const verbs = offered(id);
    const msg = el("p", "edit-msg", "");
    msg.setAttribute("role", "status");
    if (!verbs.length) {
      card.appendChild(el("p", "xnote agent-detail-none", "Nothing can be done to this row from here."));
      return card;
    }
    const bar = el("div", "verb-actions agent-detail-verbs");
    for (const v of verbs) {
      const b = el("button", "verb-btn", v.label);
      b.type = "button";
      b.title = v.gloss;
      b.setAttribute("data-agent-verb", v.verb);
      let armed = false;
      b.addEventListener("click", async () => {
        if (v.sure && !armed) {                  // a verb that cannot be undone asks for a second tap, in words
          armed = true;
          b.textContent = "confirm: " + v.sure;
          msg.className = "edit-msg";
          msg.textContent = v.gloss + ". Tap again to do it.";
          return;
        }
        for (const x of bar.querySelectorAll("button")) x.disabled = true;
        msg.className = "edit-msg busy";
        msg.textContent = v.label + "…";
        let res;
        try { res = await root.LevainCockpitTransport.rowVerb(id, v.verb, {}, v.sure ? true : false); }
        catch (e) { res = { ok: false, message: "the " + v.label + " could not be sent" }; }
        if (res && res.ok) { msg.className = "edit-msg"; msg.textContent = v.label + " done"; return; }   // the board re-reads and the view rebuilds
        for (const x of bar.querySelectorAll("button")) x.disabled = false;
        armed = false;
        b.textContent = v.label;
        msg.className = "edit-msg err";
        msg.textContent = (res && (res.message || res.error)) || (v.label + " failed");
      });
      bar.appendChild(b);
    }
    card.append(bar, msg);
    return card;
  }

  function open(row) {
    const D = root.LevainDashboard;
    if (!D || typeof D.openCustom !== "function") return;
    const id = row.dataset.keepKey;
    D.openCustom({ id: "agent:" + id, title: nameOf(wordsOf(row).body), live: true, opener: row, build: () => build(id) });
  }

  function decorate() {
    for (const r of rows()) {
      if (r.dataset.agentRow === "1") continue;
      r.dataset.agentRow = "1";
      r.setAttribute("tabindex", "0");
      r.setAttribute("role", "button");
      r.setAttribute("aria-haspopup", "dialog");
      r.style.cursor = "pointer";
    }
  }

  function rowOf(ev) {
    const t = ev.target && ev.target.closest ? ev.target : null;
    if (!t || t.closest(".clause-toggle")) return null;     // the text's own expand control keeps its job
    const r = t.closest(".row[data-keep-key]");
    return r && r.dataset.agentRow === "1" ? r : null;
  }
  doc.addEventListener("click", (ev) => { const r = rowOf(ev); if (r) open(r); });
  doc.addEventListener("keydown", (ev) => {
    if (ev.key !== "Enter" && ev.key !== " ") return;
    const r = rowOf(ev);
    if (r && ev.target === r) { ev.preventDefault(); open(r); }
  });
  const watch = () => { decorate(); };
  if (typeof root.MutationObserver === "function") new root.MutationObserver(watch).observe(doc.documentElement, { childList: true, subtree: true });
  watch();
  root.LevainAgentsView = { open, build, offered, rows };
})(window);

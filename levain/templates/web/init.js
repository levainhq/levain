// levain init.js — the onboarding form builder + submit transport.
//
// Builds the one-page interview form from /init-plan.json (the shared
// build_field_plan projection) using createElement/textContent ONLY — no
// innerHTML, so the page's CSP can be `script-src 'self'; style-src 'self'`
// with no inline exception and no injection sink. On submit it POSTs
// {adapter, answers} to /init (same-origin fetch, application/json — exactly
// what the server's write/auth boundary requires) and renders the result.

(function () {
  "use strict";

  var STYLE_HINTS = {
    "line": "one line",
    "optional-line": "one line · optional",
    "prose": "a few sentences",
    "bullet": "one item per line",
  };

  // A friendly header for a title-less section (the origin.md preamble carries
  // no `## Header` — it's the AI partner's own identity). Presentation-layer
  // labeling; falls back to the raw spec name for any future template.
  var SECTION_FALLBACK_LABELS = {
    "origin.md": "Your AI partner",
    "world.md": "About you",
  };

  var statusEl = document.getElementById("status");
  var bannerEl = document.getElementById("target-banner");
  var targetEl = document.getElementById("target");
  var packsNoteEl = document.getElementById("packs-note");
  var adaptersEl = document.getElementById("adapters");
  var sectionsEl = document.getElementById("sections");
  var submitBtn = document.getElementById("submit");
  var submitNote = document.getElementById("submit-note");
  var resultEl = document.getElementById("result");
  var formEl = document.getElementById("init-form");

  var plan = null;         // the loaded /init-plan.json
  var fieldControls = [];  // [{slot, style, getEl}] for collectAnswers
  var installing = false;

  // ---- tiny DOM helper (textContent only, never innerHTML) ----
  function el(tag, opts, kids) {
    var node = document.createElement(tag);
    opts = opts || {};
    if (opts.class) node.className = opts.class;
    if (opts.text != null) node.textContent = opts.text;
    if (opts.type) node.type = opts.type;
    if (opts.name) node.name = opts.name;
    if (opts.value != null) node.value = opts.value;
    if (opts.id) node.id = opts.id;
    if (opts.attrs) {
      for (var k in opts.attrs) if (opts.attrs.hasOwnProperty(k)) node.setAttribute(k, opts.attrs[k]);
    }
    (kids || []).forEach(function (c) { if (c) node.appendChild(c); });
    return node;
  }

  function clear(node) {
    while (node.firstChild) node.removeChild(node.firstChild);
  }

  // ---- per-style value transforms (keep parity with the CLI's stored format) ----
  // A bullet answer is stored as "- item\n- item" by the CLI; the textarea shows
  // bare lines, so strip "- " for display and re-add on collect.
  function displayValue(field) {
    var cur = field.current || "";
    if (field.style === "bullet" && cur) {
      return cur.split("\n").map(function (l) { return l.replace(/^- /, ""); }).join("\n");
    }
    return cur;
  }
  function collectValue(field, raw) {
    if (field.style === "bullet") {
      return raw.split("\n")
        // strip a leading "- " the operator may have typed, so a manually-bulleted
        // line doesn't become "- - foo" after we re-add the marker (codex / nemotron)
        .map(function (s) { return s.trim().replace(/^- /, ""); })
        .filter(function (s) { return s.length; })
        .map(function (s) { return "- " + s; })
        .join("\n");
    }
    return raw.trim();
  }

  // ---- target-dir banner ----
  function renderBanner(p) {
    targetEl.textContent = p.install;
    // Composed pack layers (base excluded) — so the operator SEES the doctrine
    // that will load, not just the base fields. Hidden when base-only.
    if (Array.isArray(p.packs) && p.packs.length) {
      packsNoteEl.textContent = "composing packs: " + p.packs.join(", ");
      packsNoteEl.hidden = false;
    } else {
      packsNoteEl.hidden = true;
    }
    var st = p.target_status;
    if (st === "nonempty" && !p.force) {
      bannerEl.className = "banner bad";
      bannerEl.textContent =
        "The install directory is not empty. Restart `levain init --web --force` to install over it, or point --path at an empty directory.";
      bannerEl.hidden = false;
    } else if (st === "nonempty" && p.force) {
      bannerEl.className = "banner warn";
      bannerEl.textContent =
        "The install directory is not empty — --force is set, so its contents will be installed over (operator-edited activation files are backed up; an existing store's memory is preserved).";
      bannerEl.hidden = false;
    } else if (st === "not_a_directory") {
      bannerEl.className = "banner bad";
      bannerEl.textContent = "The install path exists but is not a directory. Stop the server and pass --path to a directory.";
      bannerEl.hidden = false;
    } else if (st === "nonexistent") {
      bannerEl.className = "banner info";
      bannerEl.textContent = "The install directory will be created.";
      bannerEl.hidden = false;
    } else {
      bannerEl.hidden = true;
    }
  }

  // ---- adapters ----
  function renderAdapters(p) {
    var descs = {
      "claude-code": "Anthropic's Claude Code CLI — settings, MCP registration, hooks.",
      "codex": "OpenAI Codex CLI — AGENTS.md, global ~/.codex hooks + config.",
    };
    clear(adaptersEl);
    var selected = p.default_adapter || p.adapters[0];
    p.adapters.forEach(function (name) {
      var radio = el("input", { type: "radio", name: "adapter", value: name });
      if (name === selected) radio.checked = true;
      var opt = el("label", { class: "adapter-opt" }, [
        radio,
        el("div", {}, [
          el("div", { class: "opt-name", text: name }),
          el("div", { class: "opt-desc", text: descs[name] || "" }),
        ]),
      ]);
      function paint() {
        Array.prototype.forEach.call(adaptersEl.children, function (c) { c.classList.remove("selected"); });
        opt.classList.add("selected");
      }
      radio.addEventListener("change", paint);
      if (radio.checked) opt.classList.add("selected");
      adaptersEl.appendChild(opt);
    });
  }

  // ---- field sections ----
  function controlFor(field) {
    var val = displayValue(field);
    if (field.style === "prose" || field.style === "bullet") {
      var ta = el("textarea", { value: val });
      ta.rows = field.style === "bullet" ? 4 : 4;
      return ta;
    }
    return el("input", { type: "text", value: val });
  }

  function renderSections(p) {
    clear(sectionsEl);
    fieldControls = [];
    // Group the flat field list into contiguous sections by section_index
    // (stable — never section_title, which can collide across duplicate headers).
    var groups = [];           // [{si, fields:[...]}], in order
    var byIndex = {};
    p.fields.forEach(function (field) {
      var si = field.section_index;
      if (!byIndex.hasOwnProperty(si)) {
        byIndex[si] = { si: si, fields: [] };
        groups.push(byIndex[si]);
      }
      byIndex[si].fields.push(field);
    });

    groups.forEach(function (g) {
      var first = g.fields[0];
      var title = first.section_title
        || SECTION_FALLBACK_LABELS[first.spec_name]
        || first.spec_name || "Section";
      var head = el("div", { class: "section-head" }, [
        el("h2", { text: title }),
        el("span", { class: "section-source", text: first.spec_name || "" }),
      ]);
      if (first.optional) {
        head.appendChild(el("span", {
          class: "optional-tag",
          text: first.optional_reason ? "optional · " + first.optional_reason : "optional",
        }));
      }
      var card = el("section", { class: "card" }, [head]);

      // Guidance lives in ONE of two places, never both (the redundancy that read
      // as "weird"): a multi-slot section splits its guidance into a per-field
      // clause under each field; a single-slot section shows its whole guidance
      // once, under the header. `field.guidance` (the split sub-clause) is
      // non-empty exactly when the section split, so it's the discriminator.
      var hasPerField = g.fields.some(function (f) { return f.guidance; });
      if (!hasPerField && first.section_guidance) {
        card.appendChild(el("p", { class: "section-guidance", text: first.section_guidance }));
      }

      var body = el("div", {});
      card.appendChild(body);

      g.fields.forEach(function (field) {
        var label = el("label", { text: field.slot });
        var fieldDiv = el("div", { class: "field" }, [label]);
        if (hasPerField && field.guidance) {
          fieldDiv.appendChild(el("p", { class: "field-guidance", text: field.guidance }));
        }
        var control = controlFor(field);
        var inputId = "f_" + field.slot;
        control.id = inputId;
        label.setAttribute("for", inputId);
        fieldDiv.appendChild(control);
        var sh = STYLE_HINTS[field.style];
        if (sh) fieldDiv.appendChild(el("div", { class: "style-hint", text: sh }));
        body.appendChild(fieldDiv);

        fieldControls.push({ field: field, control: control });
      });

      sectionsEl.appendChild(card);
    });
  }

  function collectAnswers() {
    var answers = {};
    fieldControls.forEach(function (fc) {
      answers[fc.field.slot] = collectValue(fc.field, fc.control.value);
    });
    return answers;
  }

  function selectedAdapter() {
    var checked = formEl.querySelector('input[name="adapter"]:checked');
    return checked ? checked.value : null;
  }

  // ---- result rendering ----
  function badge(text, cls) { return el("span", { class: "badge " + cls, text: text }); }

  function renderResult(data) {
    clear(resultEl);
    var ok = data.ok === true;
    var partial = data.partial === true;
    var heading = el("h2", { text: "Install" });
    if (ok) heading.appendChild(badge("complete", "good"));
    else if (partial) heading.appendChild(badge("partial", "warn"));
    else heading.appendChild(badge("failed", "bad"));
    resultEl.appendChild(heading);

    if (data.install) {
      resultEl.appendChild(el("p", { class: "hint", text: data.install + "  ·  " + (data.adapter || "") }));
    }

    if (Array.isArray(data.files) && data.files.length) {
      resultEl.appendChild(el("div", { class: "result-block-label", text: "files created (hand-editable)" }));
      data.files.forEach(function (f) {
        resultEl.appendChild(el("div", { class: "file-row" }, [
          el("span", { class: "file-label", text: f.label }),
          el("span", { class: "file-path", text: f.path }),
        ]));
      });
    }

    if (Array.isArray(data.next_steps) && data.next_steps.length) {
      resultEl.appendChild(el("div", { class: "result-block-label", text: "next steps" }));
      resultEl.appendChild(el("pre", { text: trimBlanks(data.next_steps).join("\n") }));
    }

    if (Array.isArray(data.messages) && data.messages.length) {
      resultEl.appendChild(el("div", { class: "result-block-label", text: "install log" }));
      resultEl.appendChild(el("pre", { text: trimBlanks(data.messages).join("\n") }));
    }

    resultEl.hidden = false;
    resultEl.scrollIntoView({ behavior: "smooth", block: "start" });
  }

  function trimBlanks(lines) {
    var out = lines.slice();
    while (out.length && out[0].trim() === "") out.shift();
    while (out.length && out[out.length - 1].trim() === "") out.pop();
    return out;
  }

  function renderError(data) {
    clear(resultEl);
    resultEl.appendChild(el("h2", {}, [
      document.createTextNode("Install "),
      badge(data && data.partial ? "partial" : "failed", data && data.partial ? "warn" : "bad"),
    ]));
    var msg = (data && (data.message || data.error)) || "unknown error";
    resultEl.appendChild(el("p", { class: "hint", text: msg }));
    if (data && Array.isArray(data.messages) && data.messages.length) {
      resultEl.appendChild(el("div", { class: "result-block-label", text: "install log" }));
      resultEl.appendChild(el("pre", { text: trimBlanks(data.messages).join("\n") }));
    }
    resultEl.hidden = false;
    resultEl.scrollIntoView({ behavior: "smooth", block: "start" });
  }

  // ---- submit ----
  async function submit(ev) {
    ev.preventDefault();
    if (installing) return;
    var adapter = selectedAdapter();
    if (!adapter) { setNote("pick an adapter first", true); return; }
    installing = true;
    submitBtn.disabled = true;
    setNote("installing…", false);
    try {
      var res = await fetch("/init", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ adapter: adapter, answers: collectAnswers() }),
      });
      var data = {};
      try { data = await res.json(); } catch (_) { /* tolerate a non-JSON body */ }
      if (res.ok) {
        renderResult(data);
        setNote(data.ok ? "done" : "partial — see below", !data.ok);
      } else {
        renderError(data);
        setNote("HTTP " + res.status + " — see below", true);
      }
    } catch (e) {
      setNote("network error: " + (e && e.message ? e.message : e), true);
    } finally {
      installing = false;
      submitBtn.disabled = false;
    }
  }

  function setNote(msg, bad) {
    submitNote.textContent = msg;
    submitNote.className = "submit-note" + (bad ? " bad" : "");
  }

  // ---- load ----
  async function load() {
    try {
      var res = await fetch("/init-plan.json", { cache: "no-store" });
      if (!res.ok) throw new Error("HTTP " + res.status);
      plan = await res.json();
      if (plan.errors) throw new Error(plan.errors.server || "plan error");
      renderBanner(plan);
      renderAdapters(plan);
      renderSections(plan);
      submitBtn.disabled = false;
      statusEl.textContent = plan.fields.length + " fields · fill what you know, submit when ready";
    } catch (e) {
      statusEl.textContent = "could not load the interview: " + (e && e.message ? e.message : e);
    }
  }

  formEl.addEventListener("submit", submit);
  load();
})();

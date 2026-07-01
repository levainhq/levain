// levain docs.js — the thin client for `levain docs`.
//
// Fetches the composed chapters (/docs.json), builds the contents nav, and renders
// each chapter's markdown through the SHARED reviewed renderer (window.LevainMD,
// from markdown.js). Vanilla, no deps. Every value reaches the DOM via textContent/
// createElement or the safe renderer — never innerHTML — so manual content can
// never become markup. Section anchors are assigned to already-rendered heading
// nodes (a DOM-node .id set, not markup), so in-page nav stays CSP-safe.

(function () {
  "use strict";

  var MD = window.LevainMD;

  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = String(text);
    return n;
  }

  // A stable, collision-free anchor id — the index guarantees uniqueness even if
  // two chapters/sections share a title; the slug is cosmetic (readable URLs).
  function anchorId(prefix, i, title) {
    var slug = String(title == null ? "" : title)
      .toLowerCase()
      .replace(/[^a-z0-9]+/g, "-")
      .replace(/^-+|-+$/g, "")
      .slice(0, 40);
    return prefix + i + (slug ? "-" + slug : "");
  }

  function closeNavOnNarrow() {
    // On a narrow viewport the nav is an overlay; collapse it after a jump.
    var toggle = document.getElementById("nav-toggle");
    if (toggle && toggle.getAttribute("aria-expanded") === "true") {
      document.body.classList.remove("nav-open");
      toggle.setAttribute("aria-expanded", "false");
    }
  }

  function render(data) {
    var chapters = (data && data.chapters) || [];
    var tocList = document.getElementById("toc-list");
    var content = document.getElementById("chapters");
    var status = document.getElementById("status");
    tocList.textContent = "";
    content.textContent = "";

    if (!chapters.length) {
      status.textContent = "This install has no manual chapters.";
      return;
    }
    status.parentNode && status.parentNode.removeChild(status);

    chapters.forEach(function (ch, ci) {
      var chapId = anchorId("ch", ci, ch.title);
      var isPack = ch.source && ch.source !== "base";

      // --- the chapter body ---
      var sec = el("section", "chapter");
      sec.id = chapId;
      if (isPack) sec.appendChild(el("div", "chapter-source", ch.source));
      sec.appendChild(MD.renderMarkdown(ch.markdown || ""));
      content.appendChild(sec);

      // --- contents entry for the chapter ---
      var chapLi = el("li", "toc-chapter");
      var chapLink = el("a", "toc-link", ch.title || "Chapter " + (ci + 1));
      chapLink.setAttribute("href", "#" + chapId);
      chapLink.addEventListener("click", closeNavOnNarrow);
      chapLi.appendChild(chapLink);
      if (isPack) chapLi.appendChild(el("span", "toc-badge", ch.source));

      // --- section sub-nav from the chapter's ## sections (the .md-h2 headings) ---
      // markdown.js tags a `## ` heading with class "md-h2". Anchoring THESE (rather
      // than "every heading after the first") means a chapter with no H1 title never
      // drops its first section, and the chapter title (.md-h1) and deeper
      // subsections are correctly excluded (codex/L1). Post-render id assignment on
      // the live heading nodes — a DOM .id set, not markup, so it stays CSP-safe.
      var sections = sec.querySelectorAll(".md-h2");
      if (sections.length) {
        var subList = el("ol", "toc-sections");
        for (var si = 0; si < sections.length; si++) {
          var hEl = sections[si];
          var secId = anchorId(chapId + "-s", si, hEl.textContent);
          hEl.id = secId;
          var subLi = el("li", "toc-section");
          var subLink = el("a", "toc-sublink", hEl.textContent);
          subLink.setAttribute("href", "#" + secId);
          subLink.addEventListener("click", closeNavOnNarrow);
          subLi.appendChild(subLink);
          subList.appendChild(subLi);
        }
        chapLi.appendChild(subList);
      }
      tocList.appendChild(chapLi);
    });
  }

  function fail(msg) {
    var status = document.getElementById("status");
    if (!status) return;
    status.textContent = msg;
    status.className = "status-error";
  }

  function initNavToggle() {
    var toggle = document.getElementById("nav-toggle");
    if (!toggle) return;
    toggle.addEventListener("click", function () {
      var open = document.body.classList.toggle("nav-open");
      toggle.setAttribute("aria-expanded", open ? "true" : "false");
    });
  }

  initNavToggle();
  fetch("/docs.json", { headers: { Accept: "application/json" } })
    .then(function (r) {
      if (!r.ok) throw new Error("HTTP " + r.status);
      return r.json();
    })
    .then(render)
    .catch(function (e) {
      fail("Could not load the manual: " + (e && e.message ? e.message : e));
    });
})();

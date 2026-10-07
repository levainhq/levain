"""``levain team view``: the team view, a read-only localhost page over the ledger (DESIGN section 13).

Four panes, one per question a lead asks: what waits on me, where were agents stopped, what is held by
nobody, what is in force. The view only SHOWS. It serves GET and HEAD and nothing else (every other method
is a 405), has no form and no write path: every change still goes through ``levain team record`` and the
owner-only commands. Privacy line: only entries someone recorded in the ledger appear here (no engineer's
own memory), and ack counts are per PATH, never per person, so the page cannot answer "who was stopped".

Denies are not in the ledger (the hook keeps them in a per-clone session file), so pane 2 counts the
acknowledgements, which are. The page at ``/`` is a shell with no ledger data in it; its script asks ``/view.json``
for one ledger snapshot per request and draws the panes with createElement and textContent only. Every route but the
shell and its static assets needs the launch token (``levain.http_guards``). The snapshot is this clone's own
ledger, as ``levain team sync`` last left it: the view never fetches, rebases, merges or pushes.

Stdlib only; the guards are the same ``levain.http_guards`` the cockpit and the docs server ride.
"""
from __future__ import annotations

import hashlib
import json
import ipaddress
import os
import socket
import sys
import threading
from urllib.parse import parse_qs
from collections import defaultdict
from datetime import datetime, timezone
from http.server import ThreadingHTTPServer
from typing import Any

from levain.http_guards import (
    GuardedHandler,
    SigtermStop,
    arm_launch_token,
    check_launch_token,
    new_launch_token,
    open_unlocked,
    publish_launch_token,
    stop_on_sigterm,
)
from levain.web_server import load_web_asset

from . import canon as C
from . import index as I
from . import roles as R
from . import verify as VF
from .transport import GitLedger, git

DEFAULT_PORT = 7450
DEFAULT_COCKPIT_URL = "http://127.0.0.1:7420/"
DEFAULT_RECHECK_DAYS = 30   # a recheck has no due date in the schema: an entry carrying one is overdue past this age
DEFAULT_ACK_FLAG = 3        # acks on one path before the page suggests its ruling may be stale or too broad
BUSY_RETRY = 2              # seconds: what a busy answer tells the browser to wait before asking again
IDLE_TIMEOUT = 5            # seconds a connection may sit idle, or stall a read or write, before it is closed
MAX_WORKERS = 32            # connections served at once (a browser keeps about 6 per host open); more are closed
REQUEST_DEADLINE = 10.0     # seconds a request may take to arrive (line, headers, any body the guards read)
_LOOPBACK = frozenset({"127.0.0.1", "localhost", "::1"})


def _days(ts: str, now: datetime) -> int | None:
    try:
        then = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None
    return max(0, (now - then).days)


def _card(e: dict, now: datetime) -> dict:
    """The fields a pane shows for one entry. No counts, nothing keyed by person."""
    return {"id": e["id"], "type": e.get("type"), "kind": e.get("kind"), "owner": e.get("owner") or "",
            "words": I.oneline(e.get("words") or ""), "summary": I.oneline(e.get("summary") or ""),
            "reason": I.oneline(e.get("reason") or ""), "recheck": I.oneline(e.get("recheck") or ""),
            "paths": list(dict.fromkeys(e.get("paths") or [])), "recorded_by": e.get("author", "?"),
            "age": I.age(e.get("ts", ""), now), "age_days": _days(e.get("ts", ""), now),
            "pack": e.get("pack") or ""}


def build_model(team: R.Team, ledger: I.Ledger, handle: str | None, canon_text: str | None, state: str, *,
                now: datetime | None = None, recheck_days: int = DEFAULT_RECHECK_DAYS,
                ack_flag: int = DEFAULT_ACK_FLAG, path_filter: str = "", problems: list[str] | None = None) -> dict:
    """The four panes as plain data, from one ledger snapshot. Pure: no I/O.

    ``problems`` is the snapshot's integrity problems as ``verify.problems`` reports them (the server passes it, so the
    page's warning counts what ``levain team verify`` counts); without it only the ledger build's own are counted.

    ``path_filter`` narrows all four panes to what governs one path: an entry stays when one of its globs matches the
    path (``index.matches``) or contains it as text (so ``src/tax`` finds ``src/tax/**``). Project-wide entries carry
    no path and drop out of a filtered view.
    """
    now = now or datetime.now(timezone.utc)
    pf = path_filter.strip()

    def hit(glob: str) -> bool:
        return not pf or I.matches(glob, pf) or pf.casefold() in glob.casefold()

    def keep(e: dict) -> bool:
        return not pf or any(hit(g) for g in e.get("paths") or [])

    live = [e for e in ledger.in_force if keep(e)]

    # 1. waiting on the viewer: open questions and tensions they own
    waiting = [_card(e, now) for e in live
               if e.get("type") in ("question", "tension") and team.owns(handle, e.get("owner", ""))]
    # Pane 1 is only this. There is deliberately no "replacement awaiting your words" half: the ledger cannot express a
    # wordless replacement, and a supersede refused by authority (index.may_link) leaves BOTH entries in force, which
    # is a ledger-level semantic that `levain team verify` reports. It is not the view's to narrate.

    # 2. where agents were stopped: acks by path of the ruling they acknowledge; never by who acked.
    # One ack ENTRY adds at most 1 to a path, however many refs or duplicate globs reach it; only a ref that resolves
    # to a ruling counts. ack_flag is compared PER PATH. ack_total (the pane's header number) is the count of
    # distinct ack entries that counted on at least one path shown, so it is never a sum of rows.
    by_path: dict[str, dict] = defaultdict(lambda: {"acks": 0, "rulings": {}})
    ack_total = 0
    for e in ledger.entries:
        if e.get("type") != "ack":
            continue
        seen: set[str] = set()
        for rid in dict.fromkeys(e.get("refs", [])):
            r = ledger.by_id.get(rid)
            if r is None or r.get("kind") != "ruling":
                continue
            # Under a filter a project-wide ruling drops out, as in every other pane: test its real paths, never the
            # "(project-wide)" label, which a filter like "project" would match as text.
            if pf and not keep(r):
                continue
            for g in dict.fromkeys(r.get("paths") or ["(project-wide)"]):
                if pf and not hit(g):
                    continue
                slot = by_path[g]
                if g not in seen:
                    seen.add(g)
                    slot["acks"] += 1
                slot["rulings"][rid] = I.oneline(r.get("words") or r.get("summary") or "")
        ack_total += bool(seen)
    stopped = [{"path": g, "acks": s["acks"], "ruling_ids": sorted(s["rulings"]),
                "ruling_words": [s["rulings"][k] for k in sorted(s["rulings"])],
                "review": s["acks"] >= ack_flag, "in_force": any(k not in ledger.superseded for k in s["rulings"])}
               for g, s in by_path.items()]
    stopped.sort(key=lambda r: (-r["acks"], r["path"]))

    # 3. held by nobody
    def holder_gone(owner: str) -> bool:
        if not owner:
            return True
        return not team.owner_ok(owner)
    held_by: dict[str, dict] = {}
    for e in live:
        why = []
        if (e.get("kind") == "ruling" or e.get("type") in ("question", "tension")) and holder_gone(e.get("owner") or ""):
            why.append("no owner" if not e.get("owner") else f"owner {I.oneline(e['owner'])} is not on team.toml")
        if e.get("recheck") and (_days(e.get("ts", ""), now) or 0) > recheck_days:
            why.append(f"recheck is {_days(e.get('ts', ''), now)} days old")
        if why:
            held_by[e["id"]] = dict(_card(e, now), why="; ".join(why))
    held = list(held_by.values())

    # 4. in force: the canon by path
    paths: dict[str, list[dict]] = defaultdict(list)
    for e in live:
        for g in dict.fromkeys(e.get("paths") or ["(project-wide)"]):
            if not pf or hit(g):
                paths[g].append(_card(e, now))
    canon = [{"path": g, "entries": paths[g]} for g in sorted(paths)]

    return {"project": team.project, "owner": team.owner, "mode": team.mode, "you": handle,
            "canon_status": C.staleness(canon_text, state),
            "problems": len(ledger.problems if problems is None else problems),
            "recheck_days": recheck_days, "ack_flag": ack_flag,
            "waiting": waiting, "stopped": stopped,
            "ack_total": ack_total, "held": held, "held_count": len(held), "in_force": canon,
            "in_force_count": len(live), "path_filter": pf,
            "generated": now.strftime("%Y-%m-%dT%H:%M:%SZ")}


# ---- the page ----------------------------------------------------------------------------------------------
# The page rides the cockpit's own stylesheet (served byte-for-byte as /dashboard.css from the packaged asset) and
# the cockpit's markup vocabulary: rings, .deck, .masthead, .tabs, .grid, .panel[data-clamp] > .phead + .pbody.clamped
# (max-height 480 + internal scroll), the .ep-search sticky filter bar with .ep-search-input / .ep-search-status, and
# .row / .etype / .sid / .tier / .clause. team_view.css only adds what the cockpit has no class for. The shell carries
# no ledger data (it is served without the launch token, so token.js can trade a link's code there); JS draws it all.

SHELL = ("<!DOCTYPE html><html lang=\"en\"><head><meta charset=\"utf-8\">"
         "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
         "<title>team view</title>"
         "<link rel=\"stylesheet\" href=\"/dashboard.css\"><link rel=\"stylesheet\" href=\"/team_view.css\"></head>"
         "<body data-vital=\"live\"><div class=\"rings\" aria-hidden=\"true\">"
         + "".join(f'<div class="ring ring-{i}"></div>' for i in range(13))
         + '<div class="core">' + "".join(f'<span class="hue h{i}"></span>' for i in range(1, 7)) + "</div>"
         + '<div class="nucleus">' + "".join(f'<span class="hue h{i}"></span>' for i in range(1, 7)) + "</div></div>"
         "<div class=\"deck\"><p class=\"note\" id=\"status\" role=\"status\">Loading the team view\u2026</p>"
         "<div id=\"app\"></div></div>"
         "<script src=\"/token.js\"></script><script src=\"/team_view.js\"></script></body></html>")

CSS = """\
/* team view additions: only what the cockpit stylesheet has no class for. Everything else is /dashboard.css. */
.tabs a.tab { text-decoration: none; display: inline-block; }
.sub.teamline::before { content: "◇ team "; }
.sub.teamline { word-break: normal; }
.warn { color: var(--hot); }
.grid.team { grid-template-columns: minmax(0, 1fr); }
@media (min-width: 760px) { .grid.team { grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); } }
@media (min-width: 1180px) { .grid.team { grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); } }
.panel[data-zone="held"] { --accent: var(--hot); }
.phead .src { margin-right: 14px; font-size: 11px; }
.pbody .ask { margin: 0 0 8px; }
.pbody .ep-search + .ask, .pbody .ep-search + .sp-results { margin-top: 12px; }
.row .note { flex: 1 0 100%; margin: 0; }
.row .why { color: var(--hot); font-size: 11px; }
.row code, .group code, .pathbar code { color: var(--identity); }
.group h3 { margin: 10px 0 0; font: 700 11px/1.4 var(--mono); color: var(--identity); letter-spacing: .02em; }
.group:first-child h3 { margin-top: 0; }
.pathfilter { position: static; background: none; border: 0; box-shadow: none; margin: 0 0 12px; padding: 0; }
.pathbar { margin: -4px 2px 12px; color: var(--operate); font-size: 11px; letter-spacing: .04em; }
.pathbar a { color: var(--mind); }
.row[hidden], .group[hidden] { display: none; }
"""

JS = r"""(function () {
  "use strict";
  // The team view's one renderer. The page at / is a shell with no ledger data in it: this script asks /view.json
  // for the panes (with the launch token, through token.js) and builds every node with createElement and
  // textContent, so nothing from the ledger is ever parsed as markup.
  var auth = window.LevainToken;
  var app = document.getElementById("app"), status = document.getElementById("status");
  var seq = 0;                 // a response is drawn only if no later request has been made
  var retry = null;            // the timer of a busy answer's retry

  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined && text !== null) n.textContent = String(text);
    return n;
  }
  function add(parent) {
    for (var i = 1; i < arguments.length; i++) {
      var c = arguments[i];
      if (c === null || c === undefined || c === "") continue;
      parent.appendChild(typeof c === "string" ? document.createTextNode(c) : c);
    }
    return parent;
  }
  function code(text) { return el("code", null, text); }
  function empty(msg) { return el("p", "empty", msg); }
  function clear(n) { while (n.firstChild) n.removeChild(n.firstChild); }
  function say(msg) { status.textContent = msg || ""; status.hidden = !msg; }

  function row(c, why, paths) {
    var label = c.kind ? c.type + " · " + c.kind : String(c.type);
    var q = [c.id, String(c.type), String(c.kind || ""), c.owner, c.words, c.summary, c.reason, c.recheck,
             c.paths.join(" ")].join(" ").toLowerCase();
    var r = el("div", "row");
    r.setAttribute("data-q", q);
    add(r, el("span", "etype", label), el("span", "sid", c.id));
    if (c.owner) add(r, el("span", "tier", "owner " + c.owner));
    if (why) add(r, el("span", "why", why));
    var body = c.words || c.summary;
    if (body) add(r, el("span", "clause", body));
    var note = el("div", "note"), parts = [];
    if (c.words && c.summary) parts.push(["summary by " + c.recorded_by + ": " + c.summary]);
    if (c.reason) parts.push(["why: " + c.reason]);
    if (paths !== false && c.paths.length) {
      var g = ["governs "];
      c.paths.forEach(function (p, i) { if (i) g.push(" "); g.push(code(p)); });
      parts.push(g);
    }
    if (c.recheck) parts.push(["re-check: ", code(c.recheck)]);
    parts.push(["recorded by " + (c.pack ? "pack " + c.pack : c.recorded_by) + " · " + c.age]);
    parts.forEach(function (p, i) { if (i) add(note, " · "); add.apply(null, [note].concat(p)); });
    return add(r, note);
  }

  function panel(n, zone, title, ask, count, body, extra) {
    var s = el("section", "panel");
    s.id = "pane" + n;
    s.setAttribute("data-zone", zone);
    s.setAttribute("data-clamp", "1");
    add(s, add(el("div", "phead"), el("h2", null, n + " · " + title), el("span", "src", count)));
    var b = add(el("div", "pbody clamped"), extra, el("p", "note ask", ask));
    body.forEach(function (x) { add(b, x); });
    return add(s, b);
  }

  function searchBar(placeholder, label, id) {
    var i = el("input", "ep-search-input");
    i.id = id; i.type = "search";
    i.setAttribute("placeholder", placeholder);
    i.setAttribute("aria-label", label);
    var st = el("span", "ep-search-status");
    st.id = id + "-status";
    return add(el("div", "ep-search"), i, st);
  }

  // The panes are this clone's own copy of the ledger; the button asks for it again (levain team sync brings in
  // what the team has pushed; the view never fetches).
  function reloadButton(box) {
    var b = el("button", null, "⟳ reload");
    b.id = "refresh"; b.type = "button";
    return add(box, el("span", "stamp", "this clone's copy"), b);
  }

  function render(m) {
    document.title = m.project + " · team view";
    var pf = m.path_filter || "";

    var p1 = m.waiting.map(function (c) { return row(c); });
    var pane1 = panel(1, "operate", "Waiting on you", "Questions and tensions where you are the owner.",
                      m.waiting.length, p1.length ? p1 : [empty("Nothing is waiting on you.")]);

    var rows = m.stopped.map(function (r) {
      var d = add(el("div", "row"), el("span", "sid", r.path),
                  el("span", "tier", r.acks + " ack" + (r.acks !== 1 ? "s" : "")));
      if (r.review) add(d, el("span", "why", "review: " + r.acks + " acks, this ruling may be stale or too broad"));
      if (!r.in_force) add(d, el("span", "note", "ruling no longer in force"));
      return add(d, el("span", "clause", r.ruling_words.filter(function (w) { return w; }).join(" ")),
                 el("div", "note", r.ruling_ids.join(", ")));
    });
    var pane2 = panel(2, "identity", "Where agents were stopped",
                      "Acknowledgements by path: where recorded knowledge is doing work. Counted per path, never per " +
                      "person. Denies live in each clone's hook state, not in the ledger, so they are not here.",
                      m.ack_total, rows.length ? rows : [empty("No agent has acknowledged a ruling yet.")]);

    var p3 = m.held.map(function (c) { return row(c, c.why); });
    var pane3 = panel(3, "held", "Held by nobody",
                      "Rulings whose owner left team.toml, entries with no owner, rechecks older than " +
                      m.recheck_days + " days.",
                      m.held_count, p3.length ? p3 : [empty("Everything in force has a holder and a fresh recheck.")]);

    var results = el("div", "sp-results");
    results.id = "inforce-results";
    m.in_force.forEach(function (g) {
      var grp = add(el("div", "group"), el("h3", null, g.path));
      grp.setAttribute("data-g", g.path.toLowerCase());
      g.entries.forEach(function (c) { add(grp, row(c, "", false)); });
      add(results, grp);
    });
    if (!m.in_force.length) add(results, empty("Nothing is in force yet."));
    var pane4 = panel(4, "mind", "In force", "The canon, by path: every entry no one has superseded.",
                      m.in_force_count, [results],
                      searchBar("filter in force by path or text…", "filter in-force entries by path or text",
                                "inforce-q"));

    var head = el("header", "masthead");
    add(head, add(el("div", "brand"), el("div", "wordmark", "Levain"),
                  el("div", "model", "the team's shared decisions, from outside the session")));
    var ind = el("div", "indicators");
    add(ind, el("span", "stamp", m.generated));
    reloadButton(ind);
    add(head, add(el("div", "readout"),
                  add(el("div", "unit"), el("span", "unit-label", "Team"), el("span", "entity", m.project)), ind));

    var line = el("p", "sub teamline");
    add(line, (m.you ? "you are " + m.you : "your git user.email maps to no member") + " · canon owner " +
        m.owner + " · mode " + m.mode + " · " + m.canon_status);
    if (m.problems) add(line, " · ", el("span", "warn", m.problems + " integrity problem(s): run levain team verify"));
    if (m.warning_count) add(line, " · ", el("span", "warn", m.warning_count + " warning" +
                                                   (m.warning_count !== 1 ? "s" : "") +
                                                   " from reading the ledger (see the terminal running the view)"));

    var tabs = el("nav", "tabs");
    var cockpit = el("a", "tab", "◍ Cockpit");
    if (/^https?:\/\//.test(m.cockpit_url || "")) cockpit.setAttribute("href", m.cockpit_url);
    var self = el("a", "tab active", "▣ Team view");
    self.setAttribute("href", "/");
    add(tabs, cockpit, self);

    var pq = el("input", "ep-search-input");
    pq.id = "path-q"; pq.type = "search"; pq.value = pf;
    pq.setAttribute("value", pf);
    pq.setAttribute("data-current", pf);
    pq.setAttribute("placeholder", "what governs…  a file path or glob, then Enter (narrows all four panes)");
    pq.setAttribute("aria-label", "narrow all four panes to one path or glob");
    var pathfilter = add(el("div", "ep-search pathfilter"), pq);
    var pathbar = null;
    if (pf) {
      var clr = el("a", null, "clear");
      clr.setAttribute("href", "/");
      pathbar = add(el("p", "pathbar active"), "narrowed to what governs ", code(pf), " · ", clr);
    }

    var board = add(el("div", "grid team"), pane1, pane2, pane3, pane4);
    board.id = "board";
    var foot = el("footer", "deck-foot", "owners rule, everyone sees, nobody is watched · read-only: change " +
                  "anything with levain team record");

    clear(app);
    add(app, head, line, tabs, pathfilter, pathbar, board, foot);
    wire();
  }

  // measureOverflow, as in the cockpit: a focusable scroll region plus the "more below" chevron cue.
  function measure(body) {
    if (!body) return;
    var over = body.scrollHeight > body.clientHeight + 1;
    body.classList.toggle("has-overflow", over);
    if (over) {
      body.tabIndex = 0; body.setAttribute("role", "region");
      var h = body.parentElement.querySelector(".phead h2");
      body.setAttribute("aria-label", (h ? h.textContent + " — " : "") + "scrollable");
      if (!body.dataset.scrollWired) {
        body.dataset.scrollWired = "1";
        body.addEventListener("scroll", function () { body.classList.toggle("at-top", body.scrollTop <= 1); }, { passive: true });
      }
      body.classList.toggle("at-top", body.scrollTop <= 1);
    } else { body.removeAttribute("tabindex"); body.removeAttribute("role"); body.removeAttribute("aria-label"); body.classList.remove("at-top"); }
  }
  function measureAll() { document.querySelectorAll(".pbody.clamped").forEach(measure); }

  function wire() {
    measureAll();
    // In Force filter: client-side over the drawn rows, case-insensitive substring over path + text, like the
    // cockpit's Open Loops filter (same bar, same status line).
    var q = document.getElementById("inforce-q"), st = document.getElementById("inforce-q-status");
    var res = document.getElementById("inforce-results");
    if (q && res) {
      q.addEventListener("input", function () {
        var needle = q.value.trim().toLowerCase(), shown = 0, rows = res.querySelectorAll(".row");
        rows.forEach(function (r) {
          var g = r.parentElement.getAttribute("data-g") || "";
          var hit = !needle || r.getAttribute("data-q").indexOf(needle) !== -1 || g.indexOf(needle) !== -1;
          r.hidden = !hit; if (hit) shown++;
        });
        res.querySelectorAll(".group").forEach(function (g) { g.hidden = !g.querySelector(".row:not([hidden])"); });
        var none = res.querySelector(".nomatch");
        if (needle && !shown) {
          if (!none) { none = el("p", "empty nomatch"); res.appendChild(none); }
          none.textContent = "no entries match “" + q.value.trim() + "”";
        } else if (none) none.remove();
        st.textContent = needle ? shown + (shown === 1 ? " match" : " matches") + " · ⌫ to clear" : "";
        measure(res.closest(".pbody"));
      });
      // #q=text in the URL prefills the In Force filter (a shareable, read-only link).
      var h = /^#q=(.*)$/.exec(location.hash);
      if (h) { try { q.value = decodeURIComponent(h[1]); } catch (e) { q.value = h[1]; } q.dispatchEvent(new Event("input")); }
    }
    // Path filter: a navigation to /?path=..., which this script reads; nothing is written anywhere.
    var pq = document.getElementById("path-q");
    if (pq) pq.addEventListener("keydown", function (ev) {
      if (ev.key !== "Enter") return;
      var v = pq.value.trim();
      location.href = v ? "/?path=" + encodeURIComponent(v) : "/";
    });
    var rf = document.getElementById("refresh");
    if (rf) rf.addEventListener("click", function () { load(); });
  }

  function load() {
    var mine = ++seq;
    if (retry) { clearTimeout(retry); retry = null; }
    var sent = auth ? auth.get() : null;
    var headers = { Accept: "application/json" };
    if (auth) auth.headers(headers);
    var path = new URLSearchParams(location.search).get("path") || "";
    var qs = [];
    if (path) qs.push("path=" + encodeURIComponent(path));
    fetch("/view.json" + (qs.length ? "?" + qs.join("&") : ""), { headers: headers, cache: "no-store" })
      .then(function (r) {
        if (r.status === 403 && auth) {
          return r.json().catch(function () { return {}; }).then(function (j) {
            if (!auth.isRefusal(r.status, j)) throw new Error("HTTP " + r.status);
            auth.lock(sent ? "That token was not accepted." : null, sent);   // shows the one unlock form
            return { locked: true };
          });
        }
        if (r.status === 503) {
          return r.json().catch(function () { return {}; }).then(function (j) {
            return { unavailable: (j && j.error) || "unavailable", wait: parseInt(r.headers.get("Retry-After"), 10) || 2 };
          });
        }
        if (!r.ok) throw new Error("HTTP " + r.status);
        return r.json().then(function (m) { return { model: m }; });
      })
      .then(function (a) {
        if (mine !== seq) return;                  // a later request was made: its answer is the one to draw
        if (a.locked) { say("Locked: this page needs its token."); return; }
        if (a.unavailable === "busy") {
          // The server is reading the ledger for another request: ask again after the wait it named.
          say("The team view is reading the ledger. Asking again in " + a.wait + " seconds.");
          retry = setTimeout(load, a.wait * 1000);
          return;
        }
        if (a.unavailable) {
          clear(app);                              // a ledger that cannot be read must not leave older panes up
          say("The ledger is unavailable: see the terminal running levain team view.");
          return;
        }
        say("");
        render(a.model);
      })
      .catch(function (e) {
        if (mine !== seq) return;
        say("Could not load the team view: " + (e && e.message ? e.message : e));
      });
  }

  window.addEventListener("resize", measureAll);
  if (auth) auth.onUnlock(function () { load(); });
  load();
})();
"""

# ---- the server --------------------------------------------------------------------------------------------

class _ViewServer(ThreadingHTTPServer):
    # Explicit: if a supported Python ever defaulted this to True, two views could bind one port and both publish.
    allow_reuse_port = False
    allowed_hosts: frozenset[str]
    ledger_reader: Any
    recheck_days: int
    ack_flag: int
    cockpit_url: str
    assets: dict
    # The launch token GuardedHandler enforces on every path but the page shell and its static assets (``token_free_paths``).
    launch_token: str | None
    token_free_paths: frozenset[str]
    model_lock: threading.Lock
    workers: threading.BoundedSemaphore
    problems_cache: tuple[tuple, list[str]] | None = None   # (commit, history boundary, team): the history walk
    shallow_path: str | None = None
    registration: Any = None   # holds the registry lock fd for the server's life; see registry.Registration

    def handle_error(self, request: Any, client_address: Any) -> None:
        if isinstance(sys.exc_info()[1], (ConnectionError, TimeoutError)):
            return
        super().handle_error(request, client_address)

    # ThreadingHTTPServer starts a thread per CONNECTION, and an idle keep-alive connection keeps its thread until the
    # handler's socket timeout. The slot is taken before the thread exists and given back when the thread ends, so
    # no burst of connections can grow the thread count past MAX_WORKERS; a connection over the bound is closed at
    # once, unanswered (writing a 503 from this accepting thread could block it on a slow client).
    def process_request(self, request: Any, client_address: Any) -> None:
        if not self.workers.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self.workers.release()             # no thread was started, so nothing else will give the slot back
            raise

    def process_request_thread(self, request: Any, client_address: Any) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.workers.release()


_WARNINGS_LOCK = threading.Lock()   # one per process, as transport.WARNINGS is; taken with a server's model_lock
_CUT_LOCK = threading.Lock()        # a request's deadline cut and its disarm


class _Busy(Exception):
    pass


class _ViewHandler(GuardedHandler):
    """GET and HEAD only. GuardedHandler runs the Host allowlist, the cross-site read refusal and the launch token
    before ``_route``; every other method gets the guards and then 405 with ``Allow: GET, HEAD``."""

    server_version = "levain-team-view"
    allow = "GET, HEAD"
    server: _ViewServer
    # A connection holds one of MAX_WORKERS slots for as long as it is open, idle keep-alives included: this closes an
    # idle one (and bounds every read and write on its socket) after IDLE_TIMEOUT, so a browser's idle connections
    # cannot hold every slot and lock the page out for the guard's 30 seconds.
    timeout = IDLE_TIMEOUT

    # IDLE_TIMEOUT bounds each read, not a request: a client sending one byte every few seconds would hold its slot
    # forever, and MAX_WORKERS of them would close the page to everyone. A request must reach _route within
    # REQUEST_DEADLINE of the connection opening, or of the last routed request's answer on it, or the socket is shut
    # down; _route disarms the deadline, so a slow ledger read is never cut. (The request loop itself is
    # GuardedHandler's, the one shared copy, so the deadline is armed from setup and _route, not from that loop.)
    # Timer.cancel() cannot stop a timer whose wait has already ended, so the cut and the disarm also agree under a
    # lock on one flag per arming: once disarmed, that arming's cut does nothing, however late it runs.
    def setup(self) -> None:
        super().setup()
        self._arm()

    def finish(self) -> None:
        try:
            super().finish()
        finally:
            self._disarm()

    def _arm(self) -> None:
        self._armed = [True]
        self._deadline = threading.Timer(REQUEST_DEADLINE, self._cut, args=(self._armed,))
        self._deadline.daemon = True
        self._deadline.start()

    def _disarm(self) -> None:
        with _CUT_LOCK:
            self._armed[0] = False
        self._deadline.cancel()

    def _cut(self, armed: list) -> None:
        with _CUT_LOCK:
            if not armed[0]:
                return
            try:
                self.connection.shutdown(socket.SHUT_RDWR)   # the blocked read returns at once and the handler ends
            except OSError:
                pass

    def _model(self, path_filter: str = "") -> dict:
        gl: GitLedger = self.server.ledger_reader
        # Serializes model generation WITHIN this process only (it says nothing about other processes' git use).
        # A cold history read can take a minute, so nobody waits for the lock: a second request answers 503 "busy"
        # at once and its thread ends, instead of parking a handler thread per request. Static assets never come
        # through here.
        if not self.server.model_lock.acquire(blocking=False):
            raise _Busy()
        # transport.WARNINGS is one list per process: what a request appends is taken off it as that request ends, so
        # two servers in one process take turns, or one would show and drop the other's. In `levain team view` the
        # view's requests are the only readers of the ledger in the process, so nothing else appends meanwhile.
        if not _WARNINGS_LOCK.acquire(blocking=False):
            self.server.model_lock.release()
            raise _Busy()
        mark = len(gl.warnings)
        try:
            sha, team, ledger = gl.snapshot()
            m = build_model(team, ledger, gl.handle(team), gl.read_canon(sha), gl.state_hash(ledger, team),
                            recheck_days=self.server.recheck_days, ack_flag=self.server.ack_flag,
                            path_filter=path_filter, problems=self._problems(gl, sha, team, ledger))
            m["cockpit_url"] = self.server.cockpit_url
            m["warning_count"] = _drain_warnings(gl, mark)
            return m
        finally:
            _drain_warnings(gl, mark)        # a read that raised still says what it warned about
            _WARNINGS_LOCK.release()
            self.server.model_lock.release()

    def _problems(self, gl: GitLedger, sha: str, team: R.Team, ledger: I.Ledger) -> list[str]:
        """verify.problems for this snapshot. The ledger's own problems and the per-entry checks are computed from each
        read; only the team.toml/PROJECT.md history walk is kept, for the same commit and the same history (deepening a shallow clone reveals older history without
        moving the tip, so the shallow boundary is part of the key). The model lock (held by the caller) makes the
        cache safe."""
        boundary = _digest_or_none(self.server.shallow_path)
        return VF.problems(_HistoryCache(gl, self.server, boundary), sha, team, ledger)

    def _route(self, *, head: bool) -> None:
        # GuardedHandler has run the Host allowlist, the cross-site read refusal and the launch token.
        self._disarm()
        try:
            self._answer(head=head)
        finally:
            self._arm()          # the next request on this connection gets its own deadline

    def _answer(self, *, head: bool) -> None:
        path, _, query = self.path.partition("?")
        asset = self.server.assets.get(path)
        if asset is not None:
            return self._send(asset[0], asset[1], head=head)
        if path == "/view.json":
            qs = parse_qs(query)
            pf = (qs.get("path") or [""])[0][:300]
            try:
                model = self._model(pf)
            except _Busy:
                return self._send_busy(head=head)
            except Exception as exc:  # a broken ledger must say so, not draw an empty page; the detail stays local
                _log(f"ledger unavailable: {exc!r}")
                return self._send(json.dumps({"error": "ledger_unavailable"}).encode("utf-8"),
                                  "application/json; charset=utf-8", status=503, head=head)
            return self._send(json.dumps(model, ensure_ascii=False).encode("utf-8"),
                              "application/json; charset=utf-8", head=head)
        self._send(b"not found\n", "text/plain; charset=utf-8", status=404, head=head)

    def _send_busy(self, *, head: bool) -> None:
        """503 at once, with Retry-After. The page's script asks again after that long."""
        body = json.dumps({"error": "busy"}).encode("utf-8")
        self.send_response(503)
        self.send_header("Retry-After", str(BUSY_RETRY))
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if not head:
            self.wfile.write(body)


def _drain_warnings(gl: GitLedger, mark: int) -> int:
    """What the transport wanted a person to hear since ``mark`` (a team.toml it had to fall back from, say) goes to
    the terminal, word for word (it can carry local paths or git's text), and is taken off the process-wide list, which
    would otherwise grow with every read. Returns how many distinct warnings there were, for the page to count."""
    new = list(dict.fromkeys(gl.warnings[mark:]))
    for w in new:
        _log(f"ledger warning: {w!r}")
    del gl.warnings[mark:]
    return len(new)


def _team_logging_warnings(gl: GitLedger, rev: str | None = None) -> R.Team:
    """``gl.team(rev)`` outside a request (at start and for the cockpit's name), its warnings said in the terminal."""
    with _WARNINGS_LOCK:
        mark = len(gl.warnings)
        try:
            return gl.team(rev)
        finally:
            _drain_warnings(gl, mark)


def _log(msg: str) -> None:
    """One line to this server's terminal. Best effort: a closed stderr must not stop the answer, and callers pass
    repr() so a message is always one unforgeable line."""
    try:
        print(f"levain team view: {msg}"[:600], file=sys.stderr, flush=True)
    except Exception:  # noqa: BLE001 — no stream at all (pythonw, a closed pipe) is not a reason to drop the answer
        pass


class _HistoryCache:
    """The GitLedger as verify.problems sees it, with ``team_history_problems`` answered from the server's cache."""

    def __init__(self, gl: GitLedger, server: Any, boundary: bytes | None):
        self._gl, self._server, self._boundary = gl, server, boundary

    def team_history_problems(self, team: R.Team, rev: str | None = None) -> list[str]:
        key = (rev, self._boundary, R.dump_team(team))
        cached = self._server.problems_cache
        if cached is None or cached[0] != key:
            cached = self._server.problems_cache = (key, self._gl.team_history_problems(team, rev))
        return list(cached[1])

    def __getattr__(self, name: str) -> Any:
        return getattr(self._gl, name)


def _digest_or_none(path: str | None) -> bytes | None:
    """The sha256 of a file, read in chunks (git's shallow file grows with the boundary and nothing bounds it), or
    None when it is absent or unreadable."""
    if not path:
        return None
    h = hashlib.sha256()
    try:
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 16), b""):
                h.update(chunk)
    except OSError:
        return None
    return h.digest()


def _shallow_path(gl: GitLedger) -> str | None:
    """Where git records this clone's shallow boundary (absent when the clone has full history), or None."""
    try:
        top = gl.repo.toplevel
        cp = git(["rev-parse", "--git-path", "shallow"], top, check=False, timeout=10)
    except Exception:  # noqa: BLE001 - no boundary known: the cache is then keyed by the commit alone
        return None
    rel = cp.stdout.strip() if cp.returncode == 0 else ""
    return os.path.join(str(top), rel) if rel else None   # relative to the working tree unless git made it absolute


def _ipv4_loopback(host: str) -> bool:
    """127.0.0.1, any 127.x.y.z, or ``localhost``. IPv6 is refused: the server is AF_INET and would die in bind."""
    if host.lower() == "localhost":
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return ip.version == 4 and ip.is_loopback


def make_view_server(gl: GitLedger, *, host: str = "127.0.0.1", port: int = DEFAULT_PORT,
                     recheck_days: int = DEFAULT_RECHECK_DAYS, ack_flag: int = DEFAULT_ACK_FLAG,
                     cockpit_url: str = DEFAULT_COCKPIT_URL, read_token: str | None = None) -> _ViewServer:
    """A bound, not-yet-serving server. Loopback only: refused before binding, and the bound address is checked again.
    Every route but the page shell and its static assets needs the launch token: ``read_token``, else a fresh one
    (the ledger is the team's own material, and a caller that can reach loopback without being this user must not
    read it)."""
    if not _ipv4_loopback(host):
        raise ValueError(f"refusing to bind {host!r}: `levain team view` serves IPv4 loopback only "
                         "(127.0.0.1 or localhost)")
    if not isinstance(port, int) or not 0 <= port <= 65535:
        raise ValueError(f"port must be 0..65535, got {port!r}")
    # Fail now, with the ledger's own message, if this clone has no ledger. Only the tip and its team.toml are read:
    # the full read (a cold history walk can take a minute) waits for the first page.
    _team_logging_warnings(gl, gl.head())
    check_launch_token(read_token)   # before the bind, so a bad token leaves no socket behind
    httpd = _ViewServer((host, port), _ViewHandler)
    bound = str(httpd.server_address[0])
    if not _ipv4_loopback(bound):
        httpd.server_close()
        raise ValueError(f"refusing to serve: {host!r} bound a non-loopback address ({bound})")
    httpd.allowed_hosts = _LOOPBACK | {bound.lower()}
    httpd.ledger_reader = gl
    httpd.model_lock = threading.Lock()
    httpd.shallow_path = _shallow_path(gl)
    httpd.workers = threading.BoundedSemaphore(MAX_WORKERS)
    httpd.recheck_days = recheck_days
    httpd.ack_flag = ack_flag
    httpd.cockpit_url = cockpit_url if cockpit_url.startswith(("http://", "https://")) else DEFAULT_COCKPIT_URL
    # The shell and its static assets: the only paths served without the launch token, so none carries ledger data.
    httpd.assets = {"/": (SHELL.encode("utf-8"), "text/html; charset=utf-8"),
                    "/dashboard.css": (load_web_asset("dashboard.css").encode("utf-8"), "text/css; charset=utf-8"),
                    "/team_view.css": (CSS.encode("utf-8"), "text/css; charset=utf-8"),
                    "/team_view.js": (JS.encode("utf-8"), "text/javascript; charset=utf-8"),
                    "/token.js": (load_web_asset("token.js").encode("utf-8"), "text/javascript; charset=utf-8")}
    arm_launch_token(httpd, new_launch_token() if read_token is None else read_token, frozenset(httpd.assets))
    return httpd


def serve(gl: GitLedger, *, host: str, port: int, recheck_days: int, ack_flag: int,
          cockpit_url: str = DEFAULT_COCKPIT_URL, open_browser: bool = False) -> int:
    from . import registry
    httpd = make_view_server(gl, host=host, port=port, recheck_days=recheck_days, ack_flag=ack_flag,
                             cockpit_url=cockpit_url)
    bh, bp = str(httpd.server_address[0]), httpd.server_address[1]
    url = f"http://{bh}:{bp}/"
    print(f"Levain team view -> {url}")
    print("  loopback-only · read-only (GET and HEAD only) · Ctrl+C to stop", flush=True)
    try:
        published = publish_launch_token(httpd, url, port=bp, kind="team-view")
    except OSError as exc:
        print(f"Could not write the launch token ({exc}). This output is not a terminal, so there is no "
              "other place to hand it over; not serving.", file=sys.stderr)
        httpd.server_close()
        return 1
    restore_sigterm = SigtermStop()
    try:
        restore_sigterm = stop_on_sigterm()   # inside the try, so a SIGTERM that lands at once still runs the cleanup
        # Back-link: tell the cockpit this view exists (see registry.py). Best effort: a registry that cannot be
        # written costs the cockpit's Team tab, never the view. Pruning and registering are separate steps, so a prune
        # failure cannot skip the registration; both are inside the try, so a Ctrl+C during either still runs the
        # cleanup. The socket is already bound, so a published entry always has its listener behind it. The entry
        # holds the plain URL, never the token: the page asks for it.
        try:
            registry.prune_dead()
        except Exception as exc:  # noqa: BLE001
            print(f"  (registry prune failed: {type(exc).__name__}: {exc})", file=sys.stderr, flush=True)
        try:
            httpd.registration = registry.register(str(gl.repo.toplevel), url, _team_logging_warnings(gl).project)
        except Exception as exc:  # noqa: BLE001
            print(f"  (not registered with the cockpit: {type(exc).__name__}: {exc})", file=sys.stderr, flush=True)
        if open_browser:   # inside it too: a Ctrl+C while the browser opens still removes the runtime file
            open_unlocked(url, published.unlocked)
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        restore_sigterm.hold()   # a SIGTERM during the cleanup must not cut it short
        # The token's runtime file; then unpublish, release the registry lock, close the socket, each in its own
        # finally so a failure in one never skips the next. This order keeps "lock held implies socket held" true.
        try:
            published.close()
        finally:
            try:
                if httpd.registration is not None:
                    httpd.registration.unpublish()
            finally:
                try:
                    if httpd.registration is not None:
                        httpd.registration.close()
                finally:
                    try:
                        httpd.server_close()
                    finally:
                        restore_sigterm()
    return 0

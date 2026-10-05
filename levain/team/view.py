"""``levain team view``: the team view, a read-only localhost page over the ledger (DESIGN section 13).

Four panes, one per question a lead asks: what waits on me, where were agents stopped, what is held by
nobody, what is in force. The view only SHOWS. It serves GET routes and nothing else (every other method
is a 405), has no form and no write path: every change still goes through ``levain team record`` and the
owner-only commands. Privacy line: only entries someone recorded in the ledger appear here (no engineer's
own memory), and ack counts are per PATH, never per person, so the page cannot answer "who was stopped".

Denies are not in the ledger (the hook keeps them in a per-clone session file), so pane 2 counts the
acknowledgements, which are. The page is rendered on the server from one ledger snapshot per request;
the stylesheet is a second GET route because the shared CSP forbids inline styles.

Stdlib only; the guards are the same ``levain.http_guards`` the cockpit and the docs server ride.
"""
from __future__ import annotations

import html
import json
import ipaddress
import sys
import threading
from urllib.parse import parse_qs
from collections import defaultdict
from datetime import datetime, timezone
from http.server import ThreadingHTTPServer
from typing import Any

from levain.http_guards import GuardedHandler
from levain.web_server import load_web_asset

from . import canon as C
from . import index as I
from . import roles as R
from .transport import GitLedger

DEFAULT_PORT = 7450
DEFAULT_COCKPIT_URL = "http://127.0.0.1:7420/"
DEFAULT_RECHECK_DAYS = 30   # a recheck has no due date in the schema: an entry carrying one is overdue past this age
DEFAULT_ACK_FLAG = 3        # acks on one path before the page suggests its ruling may be stale or too broad
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
                ack_flag: int = DEFAULT_ACK_FLAG, path_filter: str = "") -> dict:
    """The four panes as plain data, from one ledger snapshot. Pure: no I/O.

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
            "canon_status": C.staleness(canon_text, state), "problems": len(ledger.problems),
            "recheck_days": recheck_days, "ack_flag": ack_flag,
            "waiting": waiting, "stopped": stopped,
            "ack_total": ack_total, "held": held, "held_count": len(held), "in_force": canon,
            "in_force_count": len(live), "path_filter": pf,
            "generated": now.strftime("%Y-%m-%dT%H:%M:%SZ")}


# ---- rendering ---------------------------------------------------------------------------------------------
# The page rides the cockpit's own stylesheet (served byte-for-byte as /dashboard.css from the packaged asset) and
# the cockpit's markup vocabulary: rings, .deck, .masthead, .tabs, .grid, .panel[data-clamp] > .phead + .pbody.clamped
# (max-height 480 + internal scroll), the .ep-search sticky filter bar with .ep-search-input / .ep-search-status, and
# .row / .etype / .sid / .tier / .clause. team_view.css only adds what the cockpit has no class for.

def _e(s: Any) -> str:
    return html.escape(str(s), quote=True)


def _row(c: dict, why: str = "", paths: bool = True) -> str:
    label = f"{c['type']} · {c['kind']}" if c.get("kind") else str(c["type"])
    q = " ".join([c["id"], str(c["type"]), str(c.get("kind") or ""), c["owner"], c["words"], c["summary"],
                  c["reason"], c["recheck"], " ".join(c["paths"])]).lower()
    body = c["words"] or c["summary"]
    note = []
    if c["words"] and c["summary"]:
        note.append(f"summary by {_e(c['recorded_by'])}: {_e(c['summary'])}")
    if c["reason"]:
        note.append(f"why: {_e(c['reason'])}")
    if paths and c["paths"]:
        note.append("governs " + " ".join(f"<code>{_e(p)}</code>" for p in c["paths"]))
    if c["recheck"]:
        note.append(f"re-check: <code>{_e(c['recheck'])}</code>")
    src = f"pack {c['pack']}" if c["pack"] else c["recorded_by"]
    note.append(f"recorded by {_e(src)} · {_e(c['age'])}")
    return (f'<div class="row" data-q="{_e(q)}"><span class="etype">{_e(label)}</span>'
            f'<span class="sid">{_e(c["id"])}</span>'
            + (f'<span class="tier">owner {_e(c["owner"])}</span>' if c["owner"] else "")
            + (f'<span class="why">{_e(why)}</span>' if why else "")
            + (f'<span class="clause">{_e(body)}</span>' if body else "")
            + f'<div class="note">{" · ".join(note)}</div></div>')


def _panel(n: int, zone: str, title: str, ask: str, count: int, body: str, extra: str = "") -> str:
    return (f'<section class="panel" id="pane{n}" data-zone="{zone}" data-clamp="1">'
            f'<div class="phead"><h2>{n} · {_e(title)}</h2><span class="src">{count}</span></div>'
            f'<div class="pbody clamped">{extra}<p class="note ask">{_e(ask)}</p>{body}</div></section>')


def _empty(msg: str) -> str:
    return f'<p class="empty">{_e(msg)}</p>'


def _search_bar(placeholder: str, label: str, input_id: str) -> str:
    return (f'<div class="ep-search"><input class="ep-search-input" id="{input_id}" type="search" '
            f'placeholder="{_e(placeholder)}" aria-label="{_e(label)}"><span class="ep-search-status" '
            f'id="{input_id}-status"></span></div>')


def render_html(m: dict, cockpit_url: str = DEFAULT_COCKPIT_URL) -> str:
    you = m["you"]
    who = f"you are {_e(you)}" if you else "your git user.email maps to no member"
    pf = m.get("path_filter") or ""

    p1 = "".join(_row(c) for c in m["waiting"])
    pane1 = _panel(1, "operate", "Waiting on you", "Questions and tensions where you are the owner.",
                   len(m["waiting"]), p1 or _empty("Nothing is waiting on you."))

    rows = []
    for r in m["stopped"]:
        flag = (f'<span class="why">review: {r["acks"]} acks, this ruling may be stale or too broad</span>'
                if r["review"] else "")
        gone = "" if r["in_force"] else '<span class="note">ruling no longer in force</span>'
        words = " ".join(w for w in r["ruling_words"] if w)
        rows.append(f'<div class="row"><span class="sid">{_e(r["path"])}</span>'
                    f'<span class="tier">{r["acks"]} ack{"s" if r["acks"] != 1 else ""}</span>{flag}{gone}'
                    f'<span class="clause">{_e(words)}</span>'
                    f'<div class="note">{_e(", ".join(r["ruling_ids"]))}</div></div>')
    pane2 = _panel(2, "identity", "Where agents were stopped",
                   "Acknowledgements by path: where recorded knowledge is doing work. Counted per path, never per "
                   "person. Denies live in each clone's hook state, not in the ledger, so they are not here.",
                   m["ack_total"],
                   "".join(rows) or _empty("No agent has acknowledged a ruling yet."))

    p3 = "".join(_row(c, c["why"]) for c in m["held"])
    pane3 = _panel(3, "held", "Held by nobody",
                   f"Rulings whose owner left team.toml, entries with no owner, rechecks older than "
                   f"{m['recheck_days']} days.",
                   m["held_count"],
                   p3 or _empty("Everything in force has a holder and a fresh recheck."))

    groups = "".join(f'<div class="group" data-g="{_e(g["path"].lower())}"><h3>{_e(g["path"])}</h3>'
                     f'{"".join(_row(c, paths=False) for c in g["entries"])}</div>' for g in m["in_force"])
    total = m["in_force_count"]
    pane4 = _panel(4, "mind", "In force", "The canon, by path: every entry no one has superseded.", total,
                   f'<div class="sp-results" id="inforce-results">'
                   f'{groups or _empty("Nothing is in force yet.")}</div>',
                   extra=_search_bar("filter in force by path or text…", "filter in-force entries by path or text",
                                     "inforce-q"))

    if pf:
        pathbar = (f'<p class="pathbar active">narrowed to what governs <code>{_e(pf)}</code> · '
                   f'<a href="/">clear</a></p>')
    else:
        pathbar = ""
    pathfilter = (f'<div class="ep-search pathfilter">{"" if not pf else ""}'
                  f'<input class="ep-search-input" id="path-q" type="search" data-current="{_e(pf)}" '
                  f'value="{_e(pf)}" placeholder="what governs…  a file path or glob, then Enter (narrows all four panes)" '
                  f'aria-label="narrow all four panes to one path or glob"></div>')

    return ("<!DOCTYPE html><html lang=\"en\"><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
            f"<title>{_e(m['project'])} · team view</title>"
            "<link rel=\"stylesheet\" href=\"/dashboard.css\"><link rel=\"stylesheet\" href=\"/team_view.css\"></head>"
            "<body data-vital=\"live\"><div class=\"rings\" aria-hidden=\"true\">"
            + "".join(f'<div class="ring ring-{i}"></div>' for i in range(13))
            + '<div class="core">' + "".join(f'<span class="hue h{i}"></span>' for i in range(1, 7)) + "</div>"
            + '<div class="nucleus">' + "".join(f'<span class="hue h{i}"></span>' for i in range(1, 7)) + "</div></div>"
            "<div class=\"deck\"><header class=\"masthead\"><div class=\"brand\"><div class=\"wordmark\">Levain</div>"
            "<div class=\"model\">the team's shared decisions, from outside the session</div></div>"
            f"<div class=\"readout\"><div class=\"unit\"><span class=\"unit-label\">Team</span>"
            f"<span class=\"entity\">{_e(m['project'])}</span></div><div class=\"indicators\">"
            f"<span class=\"stamp\">{_e(m['generated'])}</span><button id=\"refresh\" type=\"button\">⟳ sync</button>"
            "</div></div></header>"
            f"<p class=\"sub teamline\">{who} · canon owner {_e(m['owner'])} · mode {_e(m['mode'])} · "
            f"{_e(m['canon_status'])}"
            + (f" · <span class=\"warn\">{m['problems']} integrity problem(s): run levain team verify</span>"
               if m["problems"] else "") + "</p>"
            f"<nav class=\"tabs\"><a class=\"tab\" href=\"{_e(cockpit_url)}\">◍ Cockpit</a>"
            "<a class=\"tab active\" href=\"/\">▣ Team view</a></nav>"
            + pathfilter + pathbar
            + f"<div class=\"grid team\" id=\"board\">{pane1}{pane2}{pane3}{pane4}</div>"
            "<footer class=\"deck-foot\">owners rule, everyone sees, nobody is watched · read-only: change anything "
            "with levain team record</footer></div><script src=\"/team_view.js\"></script></body></html>")


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

JS = """\
(function () {
  "use strict";
  // measureOverflow, as in the cockpit: a focusable scroll region plus the "more below" chevron cue.
  function measure(body) {
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
  measureAll(); window.addEventListener("resize", measureAll);

  // In Force filter: client-side over the rendered rows, case-insensitive substring over path + text, like the
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
        if (!none) { none = document.createElement("p"); none.className = "empty nomatch"; res.appendChild(none); }
        none.textContent = "no entries match \u201c" + q.value.trim() + "\u201d";
      } else if (none) none.remove();
      st.textContent = needle ? shown + (shown === 1 ? " match" : " matches") + " \u00b7 \u232b to clear" : "";
      measure(res.closest(".pbody"));
    });
  }
  // #q=text in the URL prefills the In Force filter (a shareable, read-only link).
  var m = /^#q=(.*)$/.exec(location.hash);
  if (q && m) { try { q.value = decodeURIComponent(m[1]); } catch (e) { q.value = m[1]; } q.dispatchEvent(new Event("input")); }
  // Path filter: a plain GET navigation (?path=...), answered by the server; nothing is written anywhere.
  var pq = document.getElementById("path-q");
  if (pq) pq.addEventListener("keydown", function (ev) {
    if (ev.key !== "Enter") return;
    var v = pq.value.trim();
    location.href = v ? "/?path=" + encodeURIComponent(v) : "/";
  });
  var rf = document.getElementById("refresh");
  if (rf) rf.addEventListener("click", function () { location.reload(); });
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
    model_lock: threading.Lock
    model_lock_timeout: float
    registration: Any = None   # holds the registry lock fd for the server's life; see registry.Registration

    def handle_error(self, request: Any, client_address: Any) -> None:
        if isinstance(sys.exc_info()[1], (ConnectionError, TimeoutError)):
            return
        super().handle_error(request, client_address)


class _Busy(Exception):
    pass


class _ViewHandler(GuardedHandler):
    """GET only. Any other method, known or not, is answered 405 (with the guard headers, via end_headers)."""

    server_version = "levain-team-view"
    server: _ViewServer

    def _model(self, path_filter: str = "") -> dict:
        gl: GitLedger = self.server.ledger_reader
        # Serializes model generation WITHIN this process only (it says nothing about other processes' git use).
        # A cold history read can take a minute, so a waiter gives up after model_lock_timeout and the caller
        # answers 503 "busy" rather than piling up handler threads. Static assets never come through here.
        if not self.server.model_lock.acquire(timeout=self.server.model_lock_timeout):
            raise _Busy()
        try:
            sha, team, ledger = gl.snapshot()
            return build_model(team, ledger, gl.handle(team), gl.read_canon(sha), gl.state_hash(ledger, team),
                               recheck_days=self.server.recheck_days, ack_flag=self.server.ack_flag,
                               path_filter=path_filter)
        finally:
            self.server.model_lock.release()

    def do_GET(self) -> None:
        if self._refuse_read(head=False):
            return
        path, _, query = self.path.partition("?")
        if path in self.server.assets:
            body, ctype = self.server.assets[path]
            return self._send(body, ctype)
        if path in ("/", "/view.json"):
            pf = (parse_qs(query).get("path") or [""])[0][:300]
            try:
                model = self._model(pf)
            except _Busy:
                return self._send(b"busy, retry in a moment\n", "text/plain; charset=utf-8", status=503)
            except Exception as exc:  # a broken ledger must say so, not draw an empty page; the detail stays local
                try:  # best effort: a closed stderr must not stop the 503, and repr() keeps it one unforgeable line
                    print(f"levain team view: ledger unavailable: {exc!r}"[:600], file=sys.stderr, flush=True)
                except Exception:  # noqa: BLE001 — no stream at all (pythonw, a closed pipe) is not a reason to drop the 503
                    pass
                return self._send(b"ledger unavailable: see the terminal running `levain team view`\n",
                                  "text/plain; charset=utf-8", status=503)
            if path == "/view.json":
                return self._send(json.dumps(model, ensure_ascii=False).encode("utf-8"),
                                  "application/json; charset=utf-8")
            return self._send(render_html(model, self.server.cockpit_url).encode("utf-8"), "text/html; charset=utf-8")
        self._send(b"not found\n", "text/plain; charset=utf-8", status=404)

    def send_error(self, code: int, message: str | None = None, explain: str | None = None) -> None:
        if code == 501:  # the stdlib's answer to a method with no do_ handler: here every other method is a 405
            self.send_response(405, "Method Not Allowed")
            body = b"read-only: GET only\n"
            self.send_header("Allow", "GET")
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.close_connection = True
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)
            return
        super().send_error(code, message, explain)

    def _not_allowed(self) -> None:
        self.send_error(501)

    do_HEAD = do_POST = do_PUT = do_DELETE = do_PATCH = do_OPTIONS = _not_allowed


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
                     cockpit_url: str = DEFAULT_COCKPIT_URL) -> _ViewServer:
    """A bound, not-yet-serving server. Loopback only: refused before binding, and the bound address is checked again."""
    if not _ipv4_loopback(host):
        raise ValueError(f"refusing to bind {host!r}: `levain team view` serves IPv4 loopback only "
                         "(127.0.0.1 or localhost)")
    if not isinstance(port, int) or not 0 <= port <= 65535:
        raise ValueError(f"port must be 0..65535, got {port!r}")
    gl.snapshot()  # fail now, with the ledger's own message, if this clone has no ledger
    httpd = _ViewServer((host, port), _ViewHandler)
    bound = str(httpd.server_address[0])
    if not _ipv4_loopback(bound):
        httpd.server_close()
        raise ValueError(f"refusing to serve: {host!r} bound a non-loopback address ({bound})")
    httpd.allowed_hosts = _LOOPBACK | {bound.lower()}
    httpd.ledger_reader = gl
    httpd.model_lock = threading.Lock()
    httpd.model_lock_timeout = 10.0
    httpd.recheck_days = recheck_days
    httpd.ack_flag = ack_flag
    httpd.cockpit_url = cockpit_url if cockpit_url.startswith(("http://", "https://")) else DEFAULT_COCKPIT_URL
    httpd.assets = {"/dashboard.css": (load_web_asset("dashboard.css").encode("utf-8"), "text/css; charset=utf-8"),
                    "/team_view.css": (CSS.encode("utf-8"), "text/css; charset=utf-8"),
                    "/team_view.js": (JS.encode("utf-8"), "text/javascript; charset=utf-8")}
    return httpd


def serve(gl: GitLedger, *, host: str, port: int, recheck_days: int, ack_flag: int,
          cockpit_url: str = DEFAULT_COCKPIT_URL) -> int:
    from . import registry
    httpd = make_view_server(gl, host=host, port=port, recheck_days=recheck_days, ack_flag=ack_flag,
                             cockpit_url=cockpit_url)
    previous = None
    installed = False
    try:
        bh, bp = str(httpd.server_address[0]), httpd.server_address[1]
        url = f"http://{bh}:{bp}/"
        print(f"Levain team view -> {url}")
        print("  loopback-only · read-only (GET only) · Ctrl+C to stop", flush=True)
        # Back-link: tell the cockpit this view exists (see registry.py). Best effort: a registry that cannot be
        # written costs the cockpit's Team tab, never the view. Pruning and registering are separate steps, so a
        # prune failure cannot skip the registration. The socket is already bound, so a published entry always has
        # its listener behind it.
        try:
            registry.prune_dead()
        except Exception as exc:  # noqa: BLE001
            print(f"  (registry prune failed: {type(exc).__name__}: {exc})", file=sys.stderr, flush=True)
        try:
            httpd.registration = registry.register(str(gl.repo.toplevel), url, gl.team().project)
        except Exception as exc:  # noqa: BLE001
            print(f"  (not registered with the cockpit: {type(exc).__name__}: {exc})", file=sys.stderr, flush=True)
        try:
            if threading.current_thread() is threading.main_thread():
                import signal
                previous = signal.signal(signal.SIGTERM, _on_sigterm)   # SIGTERM exits as cleanly as Ctrl+C
                installed = True
            httpd.serve_forever()
        except KeyboardInterrupt:
            pass
    finally:
        # Unpublish, then release the lock, then close the socket, each in its own finally so a failure in one never
        # skips the next. This order keeps "lock held implies socket held" true on every clean exit.
        try:
            if installed:
                import signal
                signal.signal(signal.SIGTERM, previous if previous is not None else signal.SIG_DFL)
        except (ValueError, OSError):
            pass
        try:
            if httpd.registration is not None:
                httpd.registration.unpublish()
        finally:
            try:
                if httpd.registration is not None:
                    httpd.registration.close()
            finally:
                httpd.server_close()
    return 0


def _on_sigterm(signum, frame) -> None:
    raise KeyboardInterrupt

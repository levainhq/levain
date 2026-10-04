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
import sys
from collections import defaultdict
from datetime import datetime, timezone
from http.server import ThreadingHTTPServer
from typing import Any

from levain.http_guards import GuardedHandler

from . import canon as C
from . import index as I
from . import roles as R
from .transport import GitLedger

DEFAULT_PORT = 7450
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
            "paths": list(e.get("paths") or []), "recorded_by": e.get("author", "?"),
            "age": I.age(e.get("ts", ""), now), "age_days": _days(e.get("ts", ""), now),
            "pack": e.get("pack") or ""}


def build_model(team: R.Team, ledger: I.Ledger, handle: str | None, canon_text: str | None, state: str, *,
                now: datetime | None = None, recheck_days: int = DEFAULT_RECHECK_DAYS,
                ack_flag: int = DEFAULT_ACK_FLAG) -> dict:
    """The four panes as plain data, from one ledger snapshot. Pure: no I/O."""
    now = now or datetime.now(timezone.utc)
    live = ledger.in_force

    # 1. waiting on the viewer: open questions and tensions they own, and replacements of their rulings that lack words
    waiting = [_card(e, now) for e in live
               if e.get("type") in ("question", "tension") and team.owns(handle, e.get("owner", ""))]
    awaiting_words = []
    for e in ledger.entries:
        if e.get("type") == "ack":
            continue
        for sid in e.get("supersedes", []):
            t = ledger.by_id.get(sid)
            if (t and t.get("kind") == "ruling" and sid not in ledger.superseded
                    and team.owns(handle, t.get("owner", "")) and not (e.get("words") or "").strip()):
                awaiting_words.append({"replacement": _card(e, now), "ruling": _card(t, now)})

    # 2. where agents were stopped: acks by path of the ruling they acknowledge; never by who acked
    by_path: dict[str, dict] = defaultdict(lambda: {"acks": 0, "rulings": {}})
    for e in ledger.entries:
        if e.get("type") != "ack":
            continue
        for rid in e.get("refs", []):
            r = ledger.by_id.get(rid)
            if r is None:
                continue
            for g in (r.get("paths") or ["(project-wide)"]):
                slot = by_path[g]
                slot["acks"] += 1
                slot["rulings"][rid] = I.oneline(r.get("words") or r.get("summary") or "")
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
    orphaned = [dict(_card(e, now), why=("no owner" if not e.get("owner") else
                                          f"owner {I.oneline(e['owner'])} is not on team.toml"))
                for e in live if e.get("kind") == "ruling" or e.get("type") in ("question", "tension")
                if holder_gone(e.get("owner") or "")]
    overdue = [dict(_card(e, now), why=f"recheck is {_days(e.get('ts', ''), now)} days old")
               for e in live if e.get("recheck") and (_days(e.get("ts", ""), now) or 0) > recheck_days]

    # 4. in force: the canon by path
    paths: dict[str, list[dict]] = defaultdict(list)
    for e in live:
        for g in (e.get("paths") or ["(project-wide)"]):
            paths[g].append(_card(e, now))
    canon = [{"path": g, "entries": paths[g]} for g in sorted(paths)]

    return {"project": team.project, "owner": team.owner, "mode": team.mode, "you": handle,
            "canon_status": C.staleness(canon_text, state), "problems": len(ledger.problems),
            "recheck_days": recheck_days, "ack_flag": ack_flag,
            "waiting": waiting, "awaiting_words": awaiting_words, "stopped": stopped,
            "orphaned": orphaned, "overdue": overdue, "in_force": canon,
            "generated": now.strftime("%Y-%m-%dT%H:%M:%SZ")}


# ---- rendering ---------------------------------------------------------------------------------------------

def _e(s: Any) -> str:
    return html.escape(str(s), quote=True)


def _entry_html(c: dict, why: str = "", paths: bool = True) -> str:
    label = f"{c['type']} · {c['kind']}" if c.get("kind") else str(c["type"])
    out = [f'<li class="entry"><div class="meta"><span class="tag">{_e(label)}</span>'
           f'<span class="id">{_e(c["id"])}</span>']
    if c.get("owner"):
        out.append(f'<span class="owner">owner {_e(c["owner"])}</span>')
    out.append(f'<span class="age">{_e(c["age"])}</span></div>')
    if why:
        out.append(f'<div class="why">{_e(why)}</div>')
    if c.get("words"):
        out.append(f'<blockquote>{_e(c["words"])}</blockquote>')
    if c.get("summary"):
        out.append(f'<div class="sub">summary by {_e(c["recorded_by"])}: {_e(c["summary"])}</div>')
    if c.get("reason"):
        out.append(f'<div class="sub">why: {_e(c["reason"])}</div>')
    if paths and c.get("paths"):
        out.append(f'<div class="paths">{" ".join(f"<code>{_e(p)}</code>" for p in c["paths"])}</div>')
    if c.get("recheck"):
        out.append(f'<div class="sub">re-check: <code>{_e(c["recheck"])}</code></div>')
    out.append(f'<div class="by">recorded by {_e(c["pack"] and "pack " + c["pack"] or c["recorded_by"])}</div></li>')
    return "".join(out)


def _pane(n: int, title: str, ask: str, count: int, body: str) -> str:
    return (f'<section class="pane" id="pane{n}"><header><span class="num">{n}</span>'
            f'<h2>{_e(title)}</h2><span class="count">{count}</span></header>'
            f'<p class="ask">{_e(ask)}</p>{body}</section>')


def _empty(msg: str) -> str:
    return f'<p class="empty">{_e(msg)}</p>'


def render_html(m: dict) -> str:
    you = m["you"]
    who = f"you are <b>{_e(you)}</b>" if you else "your git user.email maps to no member: panes 1 shows nothing for you"

    p1 = "".join(_entry_html(c) for c in m["waiting"])
    p1 += "".join(_entry_html(a["replacement"], "replaces the ruling below without the decider's words; "
                              "it is ignored until it carries them") + _entry_html(a["ruling"])
                  for a in m["awaiting_words"])
    pane1 = _pane(1, "Waiting on you", "Questions and tensions you own; replacements of your rulings that await your words.",
                  len(m["waiting"]) + len(m["awaiting_words"]),
                  f'<ul>{p1}</ul>' if p1 else _empty("Nothing is waiting on you."))

    rows = []
    for r in m["stopped"]:
        flag = (f'<span class="flag">review: {r["acks"]} acks, this ruling may be stale or too broad</span>'
                if r["review"] else "")
        words = "".join(f"<div class='sub'>{_e(w)}</div>" for w in r["ruling_words"] if w)
        gone = "" if r["in_force"] else '<span class="sub"> (ruling no longer in force)</span>'
        rows.append(f'<li class="stop"><div class="meta"><code>{_e(r["path"])}</code>'
                    f'<span class="n">{r["acks"]}</span><span class="sub">ack{"s" if r["acks"] != 1 else ""}</span>'
                    f'{flag}{gone}</div>{words}<div class="id">{_e(", ".join(r["ruling_ids"]))}</div></li>')
    pane2 = _pane(2, "Where agents were stopped",
                  "Acknowledgements by path: where recorded knowledge is doing work. Counted per path, never per person. "
                  "(Denies live in each clone's hook state, not in the ledger, so they are not here.)",
                  sum(r["acks"] for r in m["stopped"]),
                  f'<ul>{"".join(rows)}</ul>' if rows else _empty("No agent has acknowledged a ruling yet."))

    p3 = "".join(_entry_html(c, c["why"]) for c in m["orphaned"]) + "".join(_entry_html(c, c["why"]) for c in m["overdue"])
    pane3 = _pane(3, "Held by nobody",
                  f"Rulings whose owner left team.toml, entries with no owner, rechecks older than {m['recheck_days']} days.",
                  len(m["orphaned"]) + len(m["overdue"]),
                  f'<ul>{p3}</ul>' if p3 else _empty("Everything in force has a holder and a fresh recheck."))

    groups = "".join(f'<div class="group"><h3><code>{_e(g["path"])}</code></h3><ul>'
                     f'{"".join(_entry_html(c, paths=False) for c in g["entries"])}</ul></div>' for g in m["in_force"])
    pane4 = _pane(4, "In force", "The canon, by path: every entry no one has superseded.",
                  sum(len(g["entries"]) for g in m["in_force"]),
                  groups or _empty("Nothing is in force yet."))

    return ("<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
            "<meta http-equiv=\"refresh\" content=\"60\">"
            f"<title>{_e(m['project'])} · team view</title><link rel=\"stylesheet\" href=\"/view.css\"></head><body>"
            f"<header class=\"top\"><div><div class=\"brand\">LEVAIN · TEAM VIEW</div><h1>{_e(m['project'])}</h1></div>"
            f"<div class=\"status\"><div>{who}</div><div>canon owner <b>{_e(m['owner'])}</b> · mode {_e(m['mode'])}</div>"
            f"<div>{_e(m['canon_status'])}</div>"
            + (f"<div class=\"warn\">{m['problems']} ledger integrity problem(s): run <code>levain team verify</code></div>"
               if m["problems"] else "")
            + "</div></header><main>" + pane1 + pane2 + pane3 + pane4 + "</main>"
            "<footer>owners rule, everyone sees, nobody is watched · read-only: change anything with "
            f"<code>levain team record</code> · snapshot {_e(m['generated'])}</footer></body></html>")


CSS = """\
:root{--bg:#080b0b;--panel:#0e1514;--panel-hi:#131d1b;--line:#243130;--ink:#d6e0dc;--dim:#7a8884;--faint:#4c5854;
--mind:#5ce6a8;--operate:#f5b14e;--identity:#3fd6cf;--hot:#f5806e;
--mono:ui-monospace,"SF Mono","JetBrains Mono",Menlo,Consolas,monospace;
--label:"Helvetica Neue","Arial Narrow","Roboto Condensed",system-ui,sans-serif}
*{box-sizing:border-box}html,body{margin:0}
body{background:radial-gradient(120% 70% at 50% -8%,rgba(63,214,207,.05),transparent 58%),var(--bg);color:var(--ink);
font:14px/1.5 var(--label);padding:0 16px 24px}
code,.id{font-family:var(--mono);font-size:12px}
.top{display:flex;justify-content:space-between;gap:24px;flex-wrap:wrap;max-width:1280px;margin:0 auto;padding:22px 0 14px;
border-bottom:1px solid var(--line)}
.brand{font-size:11px;letter-spacing:.22em;color:var(--identity)}
h1{margin:2px 0 0;font-size:26px;font-weight:600}
.status{font-size:12px;color:var(--dim);text-align:right}.status b{color:var(--ink)}
.warn{color:var(--hot)}
main{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,520px),1fr));gap:14px;max-width:1280px;margin:16px auto}
.pane{background:var(--panel);border:1px solid var(--line);border-radius:6px;padding:14px 16px;min-width:0}
.pane header{display:flex;align-items:baseline;gap:10px}
.pane h2{margin:0;font-size:13px;letter-spacing:.14em;text-transform:uppercase;flex:1}
.num{font-family:var(--mono);color:var(--faint)}
.count{font-family:var(--mono);font-size:18px}
#pane1 .count,#pane1 h2{color:var(--operate)}#pane2 h2{color:var(--identity)}
#pane3 h2,#pane3 .count{color:var(--hot)}#pane4 h2{color:var(--mind)}
.ask{margin:4px 0 10px;color:var(--dim);font-size:12px}
ul{list-style:none;margin:0;padding:0}
li.entry,li.stop{border-top:1px solid var(--line);padding:9px 0}
.meta{display:flex;flex-wrap:wrap;gap:4px 10px;align-items:baseline}
.tag{font-size:11px;text-transform:uppercase;letter-spacing:.08em;color:var(--mind)}
.owner{color:var(--operate);font-size:12px}.age,.by,.sub{color:var(--dim);font-size:12px}
.id{color:var(--faint)}.why{color:var(--hot);font-size:12px;margin-top:2px}
blockquote{margin:5px 0;padding:2px 0 2px 10px;border-left:2px solid var(--identity);color:var(--ink)}
.paths code{margin-right:6px;color:var(--identity)}
.n{font-family:var(--mono);font-size:20px;color:var(--operate)}
.flag{color:var(--hot);font-size:12px}
.group{margin-top:10px}.group h3{margin:0 0 2px;font-size:13px;font-weight:500}.group h3 code{color:var(--identity);font-size:13px}
.empty{color:var(--faint);font-style:italic;margin:6px 0 0}
footer{max-width:1280px;margin:0 auto;color:var(--faint);font-size:11px}
"""


# ---- the server --------------------------------------------------------------------------------------------

class _ViewServer(ThreadingHTTPServer):
    allowed_hosts: frozenset[str]
    ledger_reader: Any
    recheck_days: int
    ack_flag: int

    def handle_error(self, request: Any, client_address: Any) -> None:
        if isinstance(sys.exc_info()[1], (ConnectionError, TimeoutError)):
            return
        super().handle_error(request, client_address)


class _ViewHandler(GuardedHandler):
    """GET only. Any other method, known or not, is answered 405 (with the guard headers, via end_headers)."""

    server_version = "levain-team-view"
    server: _ViewServer

    def _model(self) -> dict:
        gl: GitLedger = self.server.ledger_reader
        sha, team, ledger = gl.snapshot()
        return build_model(team, ledger, gl.handle(team), gl.read_canon(sha), gl.state_hash(ledger, team),
                           recheck_days=self.server.recheck_days, ack_flag=self.server.ack_flag)

    def do_GET(self) -> None:
        if self._refuse_read(head=False):
            return
        path = self.path.split("?", 1)[0]
        if path == "/view.css":
            return self._send(CSS.encode("utf-8"), "text/css; charset=utf-8")
        if path in ("/", "/view.json"):
            try:
                model = self._model()
            except Exception as exc:  # a broken ledger must say so, not draw an empty page
                return self._send(f"ledger unavailable: {exc}\n".encode("utf-8"), "text/plain; charset=utf-8", status=503)
            if path == "/view.json":
                return self._send(json.dumps(model, ensure_ascii=False).encode("utf-8"),
                                  "application/json; charset=utf-8")
            return self._send(render_html(model).encode("utf-8"), "text/html; charset=utf-8")
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


def make_view_server(gl: GitLedger, *, host: str = "127.0.0.1", port: int = DEFAULT_PORT,
                     recheck_days: int = DEFAULT_RECHECK_DAYS, ack_flag: int = DEFAULT_ACK_FLAG) -> _ViewServer:
    """A bound, not-yet-serving server. Loopback only: refused before binding, and the bound address is checked again."""
    if host.lower() not in _LOOPBACK and not host.startswith("127."):
        raise ValueError(f"refusing to bind {host!r}: `levain team view` is loopback-only")
    gl.snapshot()  # fail now, with the ledger's own message, if this clone has no ledger
    httpd = _ViewServer((host, port), _ViewHandler)
    bound = str(httpd.server_address[0])
    if bound.lower() not in _LOOPBACK and not bound.startswith("127."):
        httpd.server_close()
        raise ValueError(f"refusing to serve: {host!r} bound a non-loopback address ({bound})")
    httpd.allowed_hosts = _LOOPBACK | {bound.lower()}
    httpd.ledger_reader = gl
    httpd.recheck_days = recheck_days
    httpd.ack_flag = ack_flag
    return httpd


def serve(gl: GitLedger, *, host: str, port: int, recheck_days: int, ack_flag: int) -> int:
    httpd = make_view_server(gl, host=host, port=port, recheck_days=recheck_days, ack_flag=ack_flag)
    bh, bp = str(httpd.server_address[0]), httpd.server_address[1]
    print(f"Levain team view -> http://{f'[{bh}]' if ':' in bh else bh}:{bp}/")
    print("  loopback-only · read-only (GET only) · Ctrl+C to stop", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0

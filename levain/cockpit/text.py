"""levain.cockpit.text: the terminal renderer of the shared cockpit (design §7, R2).

A renderer, not a second cockpit: it takes the manifest and its panels exactly as the kernel
serves them and turns them into styled lines. It never sorts, regroups, computes a count or
formats a facet; it draws the kernel's order, the kernel's groups and the kernel's badges, shows
the four statuses distinctly, and may FILTER (a top-N cut per group) and say how many rows its own
filter hid. ``run_curses`` paints these lines; tests read them without a terminal.

``snapshot`` reads in-process (the TUI runs on the machine that holds the store), so no token or
network is involved. A caller that holds a manifest from elsewhere passes its own ``snapshot``-shaped
dict to ``render_lines``."""

from __future__ import annotations

import unicodedata
from datetime import datetime, timezone
from typing import Any

from levain.cockpit.engine import ALIASES, ROW_ENGINE_KINDS, Cockpit

LOCAL_CREDENTIAL = {"class": "none", "device_class": None}
DEFAULT_TOP_N = 12   # rows drawn per group before "+N more"; the kernel's group count says the rest

# Line styles, mapped to terminal attributes by the curses driver.
HEAD, PANEL, GROUP, ROW, NOTE, ERROR, STALE, PARTIAL, DIM = (
    "head", "panel", "group", "row", "note", "error", "stale", "partial", "dim")


def snapshot(cockpit: Cockpit, credential: dict[str, Any] | None = None, profile: str = "full") -> dict[str, Any]:
    """The manifest plus every panel it lists, read once. A panel the kernel cannot produce is
    absent from ``panels`` and drawn as an error, never skipped."""
    cred = credential or LOCAL_CREDENTIAL
    manifest = cockpit.manifest(cred)   # a fault here propagates: the caller decides what the screen shows
    panels: dict[str, Any] = {}
    for pid in manifest["panels"]:
        try:
            got = cockpit.panel(pid, profile=profile, credential_class=cred["class"])
        except Exception:  # noqa: BLE001 - one panel's fault must not blank the screen
            got = None
        if got is not None:
            panels[pid] = got
    return {"manifest": manifest, "panels": panels}


def visible(text: Any) -> str:
    """Control and bidi characters shown as ``<U+XXXX>``: a field the kernel sent can never move
    the cursor or reorder the line (design §7)."""
    out = []
    for ch in str(text):
        cat = unicodedata.category(ch)
        if cat in ("Cc", "Cf", "Cs", "Zl", "Zp"):
            out.append(f"<U+{ord(ch):04X}>")
        else:
            out.append(ch)
    return "".join(out)


def _age(iso: str | None, now: datetime) -> str:
    if not iso:
        return "never"
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError:
        return visible(iso)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    secs = max(0, int((now - dt).total_seconds()))
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if secs >= size:
            return f"{secs // size}{unit} ago"
    return f"{secs}s ago"


def _badges(row: dict[str, Any]) -> str:
    return " ".join(f"[{visible(b.get('kind'))} {visible(b.get('value'))}]" for b in row.get("badges") or [])


def _provenance(row: dict[str, Any]) -> str:
    p = row.get("provenance")
    return f" ({visible(p['label'])})" if p else ""


def _fit(text: str, width: int) -> str:
    return text if len(text) <= width else text[: max(0, width - 1)] + "…"


def _panel_lines(head: dict[str, Any], panel: dict[str, Any] | None, now: datetime, width: int,
                 top_n: int) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    status = head["status"]
    title = visible(head["title"])
    out.append((PANEL, _fit(f"{title}  [{status}]", width)))
    if status == "error":
        out.append((ERROR, _fit(f"  ERROR: {visible(head.get('error') or 'unknown')}"
                                f" — last good {_age(head.get('as_of'), now)}", width)))
        return out
    if panel is None:
        out.append((ERROR, "  ERROR: the kernel returned no payload for this panel"))
        return out
    if status == "stale":
        out.append((STALE, _fit(f"  STALE: last read {_age(head.get('as_of'), now)}", width)))
    if status == "partial":
        for s in head.get("skipped") or []:
            out.append((PARTIAL, _fit(f"  PARTIAL: {s['count']} unreadable — {visible(s['reason'])}", width)))
    for f in head.get("filtered") or []:
        out.append((DIM, _fit(f"  {f['count']} held back — {visible(f['reason'])}", width)))
    if head.get("note"):
        out.append((NOTE, _fit(f"  {visible(head['note'])}", width)))
    searched = bool(panel.get("search_error") or panel.get("match_label"))
    if status == "empty":
        if not searched:
            out.append((DIM, _fit(f"  {visible(head.get('empty') or 'Nothing here.')}", width)))
        out.extend(_search_lines(panel, width))
        return out
    kind = ALIASES.get(head["kind"], head["kind"])      # a levain.cockpit/1 kernel's names degrade too
    if panel.get("next") and panel.get("rows") is None and panel.get("value") is None:
        out.append((DIM, _fit(f"  (summary only — open {visible(panel['next'])} for the content)", width)))
    elif kind in ROW_ENGINE_KINDS:
        out.extend(_rows(head, panel, width, top_n))
    else:
        draw = _VALUE_TEXT.get(kind)
        if draw is None:
            out.append((DIM, _fit(f"  (a {visible(kind)} panel: this view has no text form for it)", width)))
        else:
            out.extend(draw(panel.get("value") or {}, now, width, top_n))
    out.extend(_search_lines(panel, width))
    return out


def _search_lines(panel: dict[str, Any], width: int) -> list[tuple[str, str]]:
    if panel.get("search_error"):
        return [(ERROR, _fit(f"  SEARCH FAILED: {visible(panel['search_error'])}", width))]
    if panel.get("match_label"):
        return [(DIM, _fit(f"  {visible(panel['match_label'])}", width))]
    return []


def _row(text: str, width: int) -> tuple[str, str]:
    return (ROW, _fit(text, width))


def _t_line(v: dict[str, Any], now: datetime, width: int, top_n: int) -> list[tuple[str, str]]:
    out = []
    for ln in v.get("lines", []):
        at = f" ({_age(ln.get('at'), now)})" if ln.get("at") else ""
        label = f"{visible(ln.get('label'))}: " if ln.get("label") else ""
        out.append(_row(f"  {label}{visible(ln.get('text'))}{at}", width))
    return out


def _t_status_dots(v: dict[str, Any], now: datetime, width: int, top_n: int) -> list[tuple[str, str]]:
    out = []
    for m in v.get("metrics", []):
        unit = f" {visible(m['unit'])}" if m.get("unit") else ""
        read = f" — {visible(m['read'])}" if m.get("read") else ""
        out.append(_row(f"  [{m.get('status', 'unknown')}] {visible(m.get('label'))}: {visible(m.get('value'))}{unit}{read}", width))
    for a in v.get("alerts", []):
        out.append((ERROR if a.get("severity") == "bad" else STALE, _fit(f"  ! {visible(a.get('message'))}", width)))
    return out


def _t_ambient(v: dict[str, Any], now: datetime, width: int, top_n: int) -> list[tuple[str, str]]:
    if "visual" in v:       # a levain.cockpit/1 visual carries its own text projection
        return [_row(f"  {visible(ln.get('label'))}: {visible(ln.get('text'))}", width) for ln in v.get("text") or []]
    events = v.get("events") or []
    out = [_row(f"  {visible(v.get('legend') or '')}", width)]
    by_repo: dict[str, int] = {}
    failed = 0
    for e in events:
        by_repo[e.get("repo") or "(no repo)"] = by_repo.get(e.get("repo") or "(no repo)", 0) + 1
        failed += e.get("outcome") == "failed"
    out.append(_row(f"  {len(events)} events" + (f", {failed} failed" if failed else ""), width))
    for repo, n in sorted(by_repo.items(), key=lambda kv: (-kv[1], kv[0]))[:top_n]:
        out.append(_row(f"    {visible(repo)}: {n}", width))
    return out


def _t_brief(v: dict[str, Any], now: datetime, width: int, top_n: int) -> list[tuple[str, str]]:
    out = []
    if v.get("headline"):
        out.append(_row(f"  {visible(v['headline'])}", width))
    for ln in v.get("lines") or []:
        label = f"{visible(ln.get('label'))}: " if ln.get("label") else ""
        out.append(_row(f"  {label}{visible(ln.get('text'))}", width))
    if v.get("markdown"):
        for ln in str(v["markdown"]).replace("\r\n", "\n").split("\n"):   # only \n breaks a line; every other separator stays visible
            out.append(_row(f"    {visible(ln)}", width))
    return out


def _t_reading(v: dict[str, Any], now: datetime, width: int, top_n: int) -> list[tuple[str, str]]:
    unit = f" {visible(v['unit'])}" if v.get("unit") else ""
    cond = f", {visible(v['condition'])}" if v.get("condition") else ""
    bounds = [f"{name} {visible(v[key])}" for name, key in (("hi", "hi"), ("lo", "lo")) if v.get(key) is not None]
    span = f" ({' / '.join(bounds)})" if bounds else ""
    out = [_row(f"  {visible(v.get('value'))}{unit}{cond}{span} — {_age(v.get('as_of'), now)}", width)]
    for sr in v.get("sub") or []:
        band = f" [{visible(sr['band'])}]" if sr.get("band") else ""
        out.append(_row(f"    {visible(sr.get('label'))}: {visible(sr.get('value'))}{band}", width))
    return out


def _t_metric_grid(v: dict[str, Any], now: datetime, width: int, top_n: int) -> list[tuple[str, str]]:
    return [_row(f"  {visible(n.get('label'))}: {visible(n.get('value'))}" + (f" {visible(n['unit'])}" if n.get("unit") else ""), width)
            for n in v.get("numbers") or []]


def _t_roster(v: dict[str, Any], now: datetime, width: int, top_n: int) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    group: Any = None
    for r in v.get("rows") or []:
        if r.get("group") != group:
            had_group = bool(group)      # an ungrouped row after a group needs its own header
            group = r.get("group")
            if group:
                out.append((GROUP, _fit(f"  {visible(group)}", width)))
            elif had_group:
                out.append((GROUP, "  (no group)"))
        call = r.get("last_call")
        tail = (f"last call {_age(call.get('at'), now)}: {visible(call.get('outcome'))}" if call
                else ("no data" if r.get("digest") is None else "digest ok"))
        out.append(_row(f"    {visible(r.get('name'))} — {visible(r.get('memory'))}, {visible(r.get('cadence'))}; {tail}", width))
    return out


def _t_form(v: dict[str, Any], now: datetime, width: int, top_n: int) -> list[tuple[str, str]]:
    out = []
    for f in v.get("fields") or []:
        opts = f" one of: {', '.join(visible(x) for x in f['enum'])}" if f.get("enum") else ""
        out.append(_row(f"  {visible(f.get('label') or f.get('name'))} ({visible(f.get('type'))}){opts}", width))
    return out


def _t_log(v: dict[str, Any], now: datetime, width: int, top_n: int) -> list[tuple[str, str]]:
    out = []
    rows = v.get("rows") or []
    for r in rows[:top_n * 4]:
        sentence = r.get("sentence") or f"{r.get('verb')} \u00b7 {r.get('target')}"
        out.append(_row(f"  {visible(sentence)} — {visible(r.get('actor'))}, {_age(r.get('at'), now)}", width))
    if len(rows) > top_n * 4:
        out.append((DIM, _fit(f"  +{len(rows) - top_n * 4} more (cut by this view)", width)))
    return out


_VALUE_TEXT = {"line": _t_line, "status-dots": _t_status_dots, "ambient": _t_ambient, "brief": _t_brief,
               "reading": _t_reading, "metric-grid": _t_metric_grid, "roster": _t_roster, "form": _t_form,
               "log": _t_log}


def _rows(head: dict[str, Any], panel: dict[str, Any], width: int, top_n: int) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    group_counts = {g["id"]: g for g in head.get("groups") or []}
    # the queue only: an uncounted group (a panel's history, K2b-3b §3) is not drawn here
    rows = [r for r in panel.get("rows") or [] if not (group_counts.get(r.get("group")) or {}).get("uncounted")]
    drawn: dict[Any, int] = {}
    current: Any = object()
    for r in rows:
        g = r.get("group")
        if g != current:
            current = g
            if g is not None:
                gh = group_counts.get(g)
                label = visible(gh["title"]) if gh else visible(g)
                n = f" ({gh['count']})" if gh else ""
                out.append((GROUP, _fit(f"  {label}{n}", width)))
        if drawn.get(g, 0) >= top_n:
            continue
        drawn[g] = drawn.get(g, 0) + 1
        line = f"    {visible(r.get('title'))}{_provenance(r)} {_badges(r)}".rstrip()
        out.append((ROW, _fit(line, width)))
    # the renderer's own top-N filter says what it hid: the kernel's group count minus rows drawn
    for g, n in list(drawn.items()):
        total = group_counts[g]["count"] if g in group_counts else sum(1 for r in rows if r.get("group") == g)
        if total > n:
            out.append((DIM, _fit(f"    +{total - n} more (cut by this view)", width)))
    return out


def render_lines(snap: dict[str, Any], width: int = 100, *, now: datetime | None = None,
                 top_n: int = DEFAULT_TOP_N) -> list[tuple[str, str]]:
    """``(style, text)`` lines for the whole cockpit: the entity masthead, the hero, brief and widgets panels, then
    each zone's panels, all in the manifest's own order."""
    manifest, panels = snap["manifest"], snap["panels"]
    now = now or datetime.now(timezone.utc)
    ent = manifest.get("entity") or {}
    out: list[tuple[str, str]] = [(HEAD, _fit(f"{visible(ent.get('name') or 'levain')} — cockpit "
                                              f"({visible(manifest.get('credential', {}).get('class', 'none'))})", width))]
    for err in manifest.get("errors") or []:
        out.append((ERROR, _fit(f"ERROR {visible(err.get('source'))}: {visible(err.get('message'))}", width)))

    def draw(pid: str) -> None:
        head = manifest["panels"].get(pid)
        if head is None:
            out.append((ERROR, f"{pid}: not in the manifest"))
            return
        # The panel payload is its own PanelHead plus rows/value, read in the same call: when it exists
        # its status and error describe the rows below it. The manifest head was read earlier and may
        # disagree (a source that failed in between), so it is only the fallback.
        panel = panels.get(pid)
        out.extend(_panel_lines(panel if panel is not None else head, panel, now, width, top_n))

    regions = manifest["regions"]
    if "hero" in regions:       # a manifest from a kernel that predates the hero carries only the compat key
        top = [*regions["hero"], *regions.get("brief", []), *regions.get("widgets", [])]
    else:
        top = regions.get("header", [])
    for pid in top:
        draw(pid)
    for zone in manifest["regions"].get("zones", []):
        out.append((HEAD, ""))
        out.append((HEAD, _fit(f"== {visible(zone['title'])} ==", width)))
        for pid in zone.get("panels", []):
            draw(pid)
    return out

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

from levain.cockpit.engine import Cockpit

LOCAL_CREDENTIAL = {"class": "none", "device_class": None}
DEFAULT_TOP_N = 12   # rows drawn per group before "+N more"; the kernel's group count says the rest

# Line styles, mapped to terminal attributes by the curses driver.
HEAD, PANEL, GROUP, ROW, NOTE, ERROR, STALE, PARTIAL, DIM = (
    "head", "panel", "group", "row", "note", "error", "stale", "partial", "dim")


def snapshot(cockpit: Cockpit, credential: dict[str, Any] | None = None, profile: str = "full") -> dict[str, Any]:
    """The manifest plus every panel it lists, read once. A panel the kernel cannot produce is
    absent from ``panels`` and drawn as an error, never skipped."""
    cred = credential or LOCAL_CREDENTIAL
    manifest = cockpit.manifest(cred)
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
        if cat in ("Cc", "Cf") or ch in "  ":
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
        return iso
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
    if status == "empty":
        out.append((DIM, _fit(f"  {visible(head.get('empty') or 'Nothing here.')}", width)))
        return out
    kind = head["kind"]
    if kind == "triage-list":
        out.extend(_rows(head, panel, width, top_n))
    elif kind == "line":
        for ln in (panel.get("value") or {}).get("lines", []):
            at = f" ({_age(ln.get('at'), now)})" if ln.get("at") else ""
            out.append((ROW, _fit(f"  {visible(ln.get('label'))}: {visible(ln.get('text'))}{at}", width)))
    elif kind == "metric":
        v = panel.get("value") or {}
        for m in v.get("metrics", []):
            unit = f" {visible(m['unit'])}" if m.get("unit") else ""
            read = f" — {visible(m['read'])}" if m.get("read") else ""
            out.append((ROW, _fit(f"  [{m.get('status', 'unknown')}] {visible(m.get('label'))}: "
                                  f"{visible(m.get('value'))}{unit}{read}", width)))
        for a in v.get("alerts", []):
            out.append((ERROR if a.get("severity") == "bad" else STALE,
                        _fit(f"  ! {visible(a.get('message'))}", width)))
    elif kind == "visual":
        v = panel.get("value") or {}
        for ln in v.get("text") or []:
            out.append((ROW, _fit(f"  {visible(ln.get('label'))}: {visible(ln.get('text'))}", width)))
    elif kind == "prose":
        v = panel.get("value") or {}
        out.append((ROW, _fit(f"  {visible(v.get('headline') or '')}", width)))
    return out


def _rows(head: dict[str, Any], panel: dict[str, Any], width: int, top_n: int) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    rows = panel.get("rows") or []
    group_counts = {g["id"]: g for g in head.get("groups") or []}
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
    """``(style, text)`` lines for the whole cockpit: the entity masthead, the header panels, then
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
        out.extend(_panel_lines(head, panels.get(pid), now, width, top_n))

    for pid in manifest["regions"].get("header", []):
        draw(pid)
    for zone in manifest["regions"].get("zones", []):
        out.append((HEAD, ""))
        out.append((HEAD, _fit(f"== {visible(zone['title'])} ==", width)))
        for pid in zone.get("panels", []):
            draw(pid)
    return out

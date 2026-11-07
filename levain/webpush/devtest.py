"""The notification-action device test (band 299): ``/devtest`` is a page Phill opens in the Home Screen app; it shows one
test notification WITH action buttons and reports what iOS did (``shown``) and what a tap did (``click``) to
``POST /webpush/devtest``. This module keeps those reports (the newest :data:`KEEP`, one file beside the push store) and
prints them for the desk: ``levain ping devtest [--json]``.

A report is a tiny allowlisted record, nothing else is stored: ``{event: shown|click, action: str<=20, state: str<=20,
maxActions: int|null, ua: str<=80}``. A device posts at most :data:`RATE_MAX` reports a minute.
"""
from __future__ import annotations

import fcntl
import json
import os
import tempfile
import time
from pathlib import Path

FILE = "devtest_events.jsonl"
KEEP = 100
RATE_MAX = 12        # reports one device may post inside RATE_WINDOW seconds
RATE_WINDOW = 60.0
LOCK_WAIT_SECS = 2.0
_KEYS = frozenset({"event", "action", "state", "maxActions", "ua"})
_EVENTS = frozenset({"shown", "click"})
_LIMITS = {"action": 20, "state": 20, "ua": 80}


class Busy(Exception):
    """The report file's lock was not free in time; the page posts again."""


class RateLimited(Exception):
    """This device posted more than :data:`RATE_MAX` reports inside :data:`RATE_WINDOW` seconds."""


def _text_ok(v: object, limit: int) -> bool:
    return isinstance(v, str) and len(v) <= limit and v.isprintable()


def clean(obj: object) -> dict[str, object] | None:
    """The allowlisted report, or None when ``obj`` is not exactly such a record (an unknown key, a wrong type, an
    over-long or non-printable string, an event outside ``shown``/``click``)."""
    if not isinstance(obj, dict) or not set(obj) <= _KEYS or obj.get("event") not in _EVENTS:
        return None
    out: dict[str, object] = {"event": obj["event"]}
    for k, limit in _LIMITS.items():
        v = obj.get(k, "")
        if not _text_ok(v, limit):
            return None
        out[k] = v
    m = obj.get("maxActions")
    if m is not None and (isinstance(m, bool) or not isinstance(m, int) or not 0 <= m <= 1000):
        return None
    out["maxActions"] = m
    return out


def _lines(d: Path) -> list[str]:
    try:
        return [ln for ln in (d / FILE).read_text(encoding="utf-8", errors="replace").splitlines() if ln.strip()]
    except FileNotFoundError:
        return []


def record(queue_dir: str | os.PathLike[str], device_id: str | None, report: dict[str, object]) -> int:
    """Add one cleaned report and return how many are kept. The file is replaced whole under a sidecar lock, so a reader
    sees the old file or the new one. Raises :class:`RateLimited`, :class:`Busy`, or ``OSError``."""
    d = Path(queue_dir)
    now = time.time()
    lock = os.open(d / (FILE + ".lock"), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        deadline = time.monotonic() + LOCK_WAIT_SECS
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise Busy from None
                time.sleep(0.02)
        old = _lines(d)
        recent = 0
        for ln in old[-RATE_MAX * 2:]:
            try:
                row = json.loads(ln)
            except ValueError:
                continue
            if isinstance(row, dict) and row.get("device") == device_id and now - float(row.get("ts") or 0) < RATE_WINDOW:
                recent += 1
        if recent >= RATE_MAX:
            raise RateLimited
        row = json.dumps({"ts": round(now, 3), "at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "device": device_id, **report},
                         separators=(",", ":"))
        lines = old[-(KEEP - 1):] + [row]
        fd, tmp = tempfile.mkstemp(dir=d, prefix=".devtest_events.tmp-")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write("\n".join(lines) + "\n")
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, d / FILE)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        return len(lines)
    finally:
        os.close(lock)


def events(queue_dir: str | os.PathLike[str]) -> list[dict[str, object]]:
    """Every kept report, oldest first; an unreadable line is skipped."""
    out = []
    for ln in _lines(Path(queue_dir)):
        try:
            row = json.loads(ln)
        except ValueError:
            continue
        if isinstance(row, dict):
            out.append(row)
    return out


def _plain(text: str) -> str:
    return "".join(c if c.isprintable() else "?" for c in text)


def summary(rows: list[dict[str, object]]) -> dict[str, object]:
    """What the desk wants in one look: whether actions were ever reported as supported, and what each tap did."""
    shown = [r for r in rows if r.get("event") == "shown"]
    clicks = [r for r in rows if r.get("event") == "click"]
    return {
        "shown": len(shown), "clicks": len(clicks),
        "maxActions": shown[-1].get("maxActions") if shown else None,
        "action_taps": [r for r in clicks if r.get("action") in ("approve", "deny")],
        "body_taps": [r for r in clicks if r.get("action") == "(body tap)"],
        "verdict": ("no tap reported yet" if not clicks else
                    "ACTION BUTTONS WORKED: an approve/deny tap was reported" if any(r.get("action") in ("approve", "deny") for r in clicks)
                    else "only body taps reported: no action button was usable"),
    }


def render(rows: list[dict[str, object]]) -> str:
    out = [_plain(f"{r.get('at')} device={r.get('device')} {r.get('event')} action={r.get('action')!r} "
                  f"state={r.get('state')!r} maxActions={r.get('maxActions')} ua={r.get('ua')!r}") for r in rows]
    s = summary(rows)
    out.append(f"-- shown={s['shown']} clicks={s['clicks']} maxActions={s['maxActions']}: {s['verdict']}")
    return "\n".join(out)

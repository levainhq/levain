"""The tap trace reports itself: a paired device's page posts the on-device trace ring (see ``static/sw.js`` and
``templates/web/sw_open.js``) to ``POST /webpush/trace`` after it opens, and the operator reads the newest report here, so
a tap test needs only the tap. The ring holds step names, counts and urls reduced to a path; this module keeps only
short scalars out of whatever arrives, and keeps the newest :data:`KEEP` reports in one file beside the push store.

Read the newest report: ``python3 -m levain.webpush.tap_report <queue_dir> [--all]``.
"""
from __future__ import annotations

import fcntl
import json
import os
import sys
import time
from pathlib import Path

FILE = "tap_trace_reports.jsonl"
KEEP = 40            # reports kept; each is at most the push family's body cap
MAX_ENTRIES = 60     # the ring's own cap
_VALUE_CLIP = 200


def _scalar(v: object) -> object:
    if v is None or isinstance(v, (bool, int, float)):
        return v
    if isinstance(v, str):
        return v[:_VALUE_CLIP]
    return json.dumps(v, separators=(",", ":"), default=str)[:_VALUE_CLIP]   # a list or object: its short text


def clean(entries: object) -> list[dict[str, object]] | None:
    """The ring entries reduced to short scalars, or None when ``entries`` is not a list of objects."""
    if not isinstance(entries, list):
        return None
    out: list[dict[str, object]] = []
    for e in entries[:MAX_ENTRIES]:
        if not isinstance(e, dict):
            return None
        out.append({str(k)[:30]: _scalar(v) for k, v in list(e.items())[:20]})
    return out


def record(queue_dir: str | os.PathLike[str], device_id: str | None, entries: list[dict[str, object]],
           *, page: object = None) -> int:
    """Append one report (the newest :data:`KEEP` stay); returns how many reports are kept."""
    path = Path(queue_dir) / FILE
    row = json.dumps({"at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "device": device_id, "page": _scalar(page),
                      "entries": entries}, separators=(",", ":"))
    with open(path, "a+", encoding="utf-8") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        f.seek(0)
        lines = [ln for ln in f.read().splitlines() if ln.strip()][-(KEEP - 1):] + [row]
        f.seek(0)
        f.truncate()
        f.write("\n".join(lines) + "\n")
        return len(lines)


def reports(queue_dir: str | os.PathLike[str]) -> list[dict[str, object]]:
    """Every kept report, oldest first; an unreadable line is skipped."""
    try:
        text = (Path(queue_dir) / FILE).read_text(encoding="utf-8")
    except OSError:
        return []
    out = []
    for ln in text.splitlines():
        try:
            out.append(json.loads(ln))
        except ValueError:
            continue
    return out


def _clock(ms: object) -> str:
    try:
        return time.strftime("%H:%M:%S", time.localtime(float(ms) / 1000.0)) + f".{int(float(ms)) % 1000:03d}"
    except (TypeError, ValueError, OverflowError, OSError):
        return "?"


def render(report: dict[str, object]) -> str:
    """One report as plain lines, steps in time order."""
    rows = [f"report {report.get('at')} device={report.get('device')} page={report.get('page')}"]
    entries = [e for e in report.get("entries", []) if isinstance(e, dict)]
    for e in sorted(entries, key=lambda e: e.get("t") if isinstance(e.get("t"), (int, float)) else 0):
        rest = " ".join(f"{k}={v}" for k, v in e.items() if k not in ("t", "src", "step"))
        rows.append(f"  {_clock(e.get('t'))} {e.get('src')}/{e.get('step')} {rest}".rstrip())
    return "\n".join(rows)


def main(argv: list[str]) -> int:
    args = [a for a in argv if not a.startswith("--")]
    if len(args) != 1:
        print("usage: python3 -m levain.webpush.tap_report <queue_dir> [--all]", file=sys.stderr)
        return 2
    got = reports(args[0])
    if not got:
        print("no trace report yet: the paired app posts one after it opens")
        return 1
    for r in (got if "--all" in argv else got[-1:]):
        print(render(r))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

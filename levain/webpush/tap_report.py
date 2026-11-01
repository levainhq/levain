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
import tempfile
import time
from pathlib import Path

FILE = "tap_trace_reports.jsonl"
KEEP = 40            # reports kept; each is at most the push family's body cap (the route reads no more than that)
LOCK_WAIT_SECS = 2.0 # how long a post waits for the file's lock before answering busy
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
    for e in entries[-MAX_ENTRIES:]:   # the newest steps
        if not isinstance(e, dict):
            return None
        out.append({str(k)[:30]: _scalar(v) for k, v in list(e.items())[:20]})
    return out


class TraceBusy(Exception):
    """The report file's lock was not free within :data:`LOCK_WAIT_SECS`; the page posts again later."""


def record(queue_dir: str | os.PathLike[str], device_id: str | None, entries: list[dict[str, object]],
           *, page: object = None) -> int:
    """Add one report (the newest :data:`KEEP` stay) and return how many reports are kept. The file is replaced whole
    (a temp file in the same directory, then ``os.replace``), so a reader sees the old file or the new one, never a half
    written one; the lock is a sidecar file taken without blocking past :data:`LOCK_WAIT_SECS`. Raises
    :class:`TraceBusy` on a lock timeout and ``OSError`` when the directory cannot be written."""
    d = Path(queue_dir)
    row = json.dumps({"at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "device": device_id, "page": _scalar(page),
                      "entries": entries}, separators=(",", ":"))
    lock = os.open(d / (FILE + ".lock"), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        deadline = time.monotonic() + LOCK_WAIT_SECS
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise TraceBusy from None
                time.sleep(0.02)
        old = [ln for ln in _text(d).splitlines() if ln.strip()]
        lines = (old[-(KEEP - 1):] if KEEP > 1 else []) + [row]
        fd, tmp = tempfile.mkstemp(dir=d, prefix=FILE + ".")        # mode 0600
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


def _text(d: Path) -> str:
    try:
        return (d / FILE).read_text(encoding="utf-8")
    except OSError:
        return ""


def reports(queue_dir: str | os.PathLike[str]) -> list[dict[str, object]]:
    """Every kept report, oldest first; an unreadable line is skipped."""
    out = []
    for ln in _text(Path(queue_dir)).splitlines():
        try:
            row = json.loads(ln)
        except ValueError:
            continue
        if isinstance(row, dict):
            out.append(row)
    return out


def _clock(ms: object) -> str:
    try:
        return time.strftime("%H:%M:%S", time.localtime(float(ms) / 1000.0)) + f".{int(float(ms)) % 1000:03d}"
    except (TypeError, ValueError, OverflowError, OSError):
        return "?"


def render(report: dict[str, object]) -> str:
    """One report as plain lines, steps in time order."""
    rows = [f"report {report.get('at')} device={report.get('device')} page={report.get('page')}"]
    raw = report.get("entries")
    entries = [e for e in (raw if isinstance(raw, list) else []) if isinstance(e, dict)]
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

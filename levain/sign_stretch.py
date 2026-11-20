"""levain#208: a read-only count of the terminal key's sign failures, per day, from ``<key dir>/sign-failures``.

Reads the evidence files :func:`levain.cockpit.keys._write_sign_failure` leaves; writes nothing and never opens a key.
The directory holds FAILURES only: a good signature leaves no file there. The good signs are counted from a ledger the
caller names (``statements``): a JSONL file whose good-sign lines carry ``"event": "statement"``, a ``"signed"`` object
and ``"ts"`` in epoch seconds. Without one, ``signs`` and ``clean_run`` print as unavailable, never guessed. Whether a
stretch of signs is long enough is the person's to judge from the numbers; nothing here decides it.
"""
from __future__ import annotations

import json
import math
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

NO_SUCCESS_LEDGER = "n/a (no success ledger)"
_NAME = re.compile(r"^sigfail-\d{8}T\d{12}-\d+-\d+\.json$")


@dataclass(frozen=True)
class Day:
    day: str                 # UTC date of the record's own time
    failed_signs: int        # distinct processes that left a record that day: one sign's attempts share one, and a pruned attempt 1 does not hide it
    empty_failures: int      # records whose signature carries no ECDSA signature (one per attempt)
    other_failures: int      # records of a signature that was not empty
    signs: int | None = None  # good signs in the statement ledger that day; None with no ledger


@dataclass(frozen=True)
class Report:
    days: list[Day]
    unreadable: int          # ledger lines skipped
    clean_run: int | None    # good signs after the latest empty failure; None with no ledger
    bad_failures: int = 0    # evidence records that could not be read or classified: clean_run is then unverified
    at_cap: bool = False     # the evidence directory holds as many files as it keeps: older failures may be pruned


def _is_empty(rec: dict) -> bool:
    from levain.authority_sig import sk_ecdsa_signature_empty

    sig = rec.get("signature")
    if isinstance(sig, str) and sig:
        return sk_ecdsa_signature_empty(sig)
    return bool(rec.get("reason"))


def _day(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%d")


def _read_statements(path: Path) -> tuple[list[float], int]:
    """``(ts of each good-sign line, unreadable lines)``. A line that is not a JSON object, or whose ts is not a finite
    number a UTC day can be made from (a bool, a string, NaN, infinity, one too large), is counted unreadable. A ledger
    that is not UTF-8 raises UnicodeDecodeError (a ValueError); the caller reports it."""
    times: list[float] = []
    bad = 0
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
            if rec.get("event") == "statement" and isinstance(rec.get("signed"), dict) and rec["signed"]:
                ts = rec["ts"]
                if isinstance(ts, bool) or not isinstance(ts, (int, float)) or not math.isfinite(ts):
                    raise ValueError("ts")
                _day(ts)
                times.append(float(ts))
        except (ValueError, AttributeError, KeyError, TypeError, OverflowError, OSError, RecursionError):
            bad += 1
    return times, bad


def read(directory: Path, statements: Path | None = None) -> Report:
    """The report for the evidence files in ``directory`` (a directory that is not there is none; any other failure to
    list it raises OSError) and, when given, the good signs in the ``statements`` ledger (an unreadable ledger raises
    OSError or UnicodeDecodeError, so a wrong path is not a silent zero)."""
    from levain.cockpit import keys
    per: dict[str, list[int]] = {}
    pids: dict[str, set[str]] = {}
    empties: list[float] = []
    unreadable = 0
    bad_failures = 0
    try:
        names = sorted(n for n in os.listdir(directory) if _NAME.match(n))
    except FileNotFoundError:
        names = []
    for name in names:
        try:
            rec = json.loads((Path(directory) / name).read_text(encoding="utf-8"))
            moment = datetime.fromisoformat(str(rec["time"]))
            if moment.tzinfo is None:
                moment = moment.replace(tzinfo=timezone.utc)      # a time with no zone is read as UTC, as the writer's is
            stamp = moment.timestamp()
            day = _day(stamp)
            if not rec.get("signature") and not rec.get("reason"):
                raise ValueError("neither a signature nor a reason")
            empty = _is_empty(rec)
        except (OSError, ValueError, KeyError, TypeError, AttributeError, OverflowError, RecursionError):
            bad_failures += 1
            continue
        row = per.setdefault(day, [0, 0, 0])
        pids.setdefault(day, set()).add(name.split("-")[2])
        row[1] += 1 if empty else 0
        row[2] += 0 if empty else 1
        if empty:
            empties.append(stamp)
    good: dict[str, int] | None = None
    clean: int | None = None
    if statements is not None:
        times, bad = _read_statements(statements)
        unreadable += bad
        good = {}
        for t in times:
            good[_day(t)] = good.get(_day(t), 0) + 1
        last = max(empties, default=float("-inf"))
        clean = sum(1 for t in times if t > last)
    at_cap = len(names) >= keys._SIGN_FAILURE_KEEP
    all_days = sorted(set(per) | set(good or {}))
    return Report([Day(d, len(pids.get(d, ())), per.get(d, [0, 0, 0])[1], per.get(d, [0, 0, 0])[2],
                       None if good is None else good.get(d, 0)) for d in all_days], unreadable, clean, bad_failures, at_cap)


def render(report: Report) -> str:
    lines = []
    for d in report.days:
        signs = NO_SUCCESS_LEDGER if d.signs is None else str(d.signs)
        lines.append(f"{d.day}  signs={signs} empty_failures={d.empty_failures} "
                     f"failed_signs={d.failed_signs} other_failures={d.other_failures}")
    if not report.days:
        lines.append("no sign-failure records")
    if report.unreadable:
        lines.append(f"note: {report.unreadable} unreadable record(s) skipped")
    if report.bad_failures:
        lines.append(f"note: {report.bad_failures} unreadable failure record(s) skipped")
    if report.clean_run is None:
        lines.append(f"clean_run={NO_SUCCESS_LEDGER}")
    elif report.bad_failures or report.at_cap:
        why = []
        if report.at_cap:
            why.append("at most: the evidence directory is at its cap")
        if report.bad_failures:
            why.append(f"unverified: {report.bad_failures} unreadable failure records")
        lines.append(f"clean_run={report.clean_run} ({'; '.join(why)})")
    else:
        lines.append(f"clean_run={report.clean_run}")
    return "\n".join(lines)

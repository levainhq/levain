#!/usr/bin/env python3
"""Per-day terminal-sign tally and the clean run since the last sign that answered empty (levain#208). Read-only.

Prints one line per local day, ``YYYY-MM-DD signs=N signs_with_empty=M other_failures=F``, then ``clean_run=K`` (the
signs since the newest sign-with-empty, counted back from the newest event) and where each count came from. It judges
nothing: what counts as a long enough stretch is the operator's to accept. ``clean_run`` is over all retained events,
not only the days shown.

``signs_with_empty`` counts SIGNS (attempts grouped, below) in which at least one attempt answered empty, recovered by
a retry or not: an empty answer is the defect even when a retry rescues the sign. A sign that failed without an empty
answer is an ``other_failures``. Apple-path cancels, timeouts and verifier errors write no evidence file, so
``other_failures`` counts only refused signatures.

Inputs, every one opened read-only (nothing is created, locked, chmodded or deleted; a missing one is "no data"):
  * ``<key dir>/sign-failures/sigfail-*.json``: the evidence the Apple CTK path writes for a refused sk signature
    (``levain.cockpit.keys._write_sign_failure``: ``time``, ``attempt``, ``reason``, ``signature``). Attempts share one
    pid and one deadline (``keys._SIGN_TIMEOUT``), so a run of files with one pid is one sign (a new sign starts when
    ``attempt`` does not rise or the gap passes that deadline). It has an empty answer when any attempt's ``reason``
    says empty, or its armored body is empty. A record from the levain-sign helper (``signer`` "levain-sign") is
    skipped: that sign is already one line of the sign log.
  * ``<key dir>/sign-log.jsonl``: one JSON line per sign from the Secure Enclave helper
    (``levain.consent.se_signer.sign_log_record``: ``time``, ``outcome``). ``signed`` / ``ok`` is a sign, ``no-signature``
    / ``empty`` a sign with an empty answer, any other outcome an other failure counted under its own name.
  * the statement ledger, read only when named (``--ledger`` or ``NEEDS_PHILL_STATEMENTS``; flow keeps one as
    ``needs_phill_statements.jsonl``, written by its ``termsign_statements.py``): one line per terminal
    `sign:` statement signed with Touch ID, ``ts`` epoch seconds. It is the only record of a SUCCESSFUL Apple-path sign
    that exists today, and it holds statements only: a terminal option answer is not in it, so a count from it is a
    lower bound (``signs>=N``, ``signs_lower_bound`` in JSON).

One source per time, never both (no double count): when a sign-log exists, its first line's time T0 is the switch.
Ledger statements before T0 count as signs; from T0 on the signs come from the sign-log only. Without a sign-log the
ledger alone counts. A day with any part before T0 (or with no sign-log) and no ledger has an unknown sign count: it
prints ``signs=?``, and ``clean_run=?`` when the run spans such a day. A day wholly from T0 on prints an exact ``signs=N``.
"""
from __future__ import annotations

import argparse
import collections
import datetime
import glob
import json
import math
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from levain.cockpit import keys as _keys   # noqa: E402
from levain.cockpit.keys import _SIGN_TIMEOUT as SIGN_GAP_S   # noqa: E402  one sign's attempts all fall inside it
SIGNED_OUTCOMES = {"signed", "ok"}
EMPTY_OUTCOMES = {"no-signature", "empty"}
MAX_TS = 32503680000.0     # year 3000: later than any real record, well inside what datetime handles in every zone
MAX_DAYS = 366


def _sign_log_keep() -> int | None:
    """The writer's own prune bound for the sign log, or None where the helper module is not in this tree."""
    try:
        from levain.consent.se_signer import SIGN_LOG_KEEP
    except ImportError:
        return None
    return SIGN_LOG_KEEP


def default_key_dir() -> Path:
    from levain.cockpit.keys import pwd_key_dir
    return pwd_key_dir()


def default_ledger() -> Path | None:
    """The statement ledger only when named (``NEEDS_PHILL_STATEMENTS``); levain never assumes where another tool keeps it."""
    env = os.environ.get("NEEDS_PHILL_STATEMENTS")
    return Path(env) if env else None


def _read_text(path: Path) -> str | None:
    try:
        with open(path, "rb") as fh:
            return fh.read().decode("utf-8", errors="replace")
    except OSError:
        return None


def _valid_ts(value) -> float | None:
    """An epoch second that is finite, not negative and inside the range datetime handles, else None."""
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    try:
        t = float(value)
    except (OverflowError, ValueError):
        return None
    if not math.isfinite(t) or t < 0 or t > MAX_TS:
        return None
    return t


def _parse_iso(value) -> float | None:
    try:
        return _valid_ts(datetime.datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp())
    except (ValueError, OverflowError, OSError):
        return None


def _empty_body(signature: str) -> bool:
    lines = [ln.strip() for ln in str(signature or "").splitlines()]
    body = [ln for ln in lines if ln and not ln.startswith("-----")]
    return not body


def read_sigfails(key_dir: Path) -> dict:
    """The sigfail evidence: ``signs`` (time of the first attempt, 'empty' | 'other'), ``files`` (all globbed),
    ``attempts`` read, ``unreadable`` (corrupt, no usable time, or unreadable) and ``helper`` (levain-sign records,
    skipped because the sign log holds those signs)."""
    attempts: list[tuple[str, float, int, bool]] = []
    unreadable = helper = 0
    names = sorted(glob.glob(os.path.join(glob.escape(str(key_dir)), "sign-failures", "sigfail-*.json")))
    for name in names:
        text = _read_text(Path(name))
        try:
            rec = json.loads(text) if text is not None else None
        except ValueError:
            rec = None
        t = _parse_iso(rec.get("time")) if isinstance(rec, dict) else None
        if t is None:
            unreadable += 1
            continue
        if rec.get("signer") == "levain-sign":
            helper += 1
            continue
        pid = os.path.basename(name).split("-")[2] if os.path.basename(name).count("-") >= 3 else ""
        reason, sig = rec.get("reason"), rec.get("signature")
        empty = (isinstance(reason, str) and reason.lower().startswith("empty")) or \
                (isinstance(sig, str) and sig.strip() != "" and _empty_body(sig))
        attempt = rec.get("attempt") if isinstance(rec.get("attempt"), int) else 1
        attempts.append((pid, t, attempt, empty))
    attempts.sort()
    signs: list[tuple[float, str]] = []
    prev = None
    group: list[tuple[str, float, int, bool]] = []

    def flush() -> None:
        if group:
            signs.append((group[0][1], "empty" if any(g[3] for g in group) else "other"))

    for a in attempts:
        if prev is not None and (a[0] != prev[0] or a[2] <= prev[2] or a[1] - prev[1] > SIGN_GAP_S):
            flush()
            group = []
        group.append(a)
        prev = a
    flush()
    return {"signs": signs, "files": len(names), "attempts": len(attempts), "unreadable": unreadable, "helper": helper}


def read_sign_log(key_dir: Path) -> dict:
    """``events`` as (time, 'signed' | 'empty' | the outcome's own name), ``present``, ``bad`` (lines or the file
    unreadable) and ``lines`` (non-blank)."""
    path = key_dir / "sign-log.jsonl"
    text = _read_text(path)
    if text is None:
        return {"events": [], "present": False, "bad": 1 if os.path.lexists(path) else 0, "lines": 0}
    out, bad, lines = [], 0, 0
    for line in text.splitlines():
        if not line.strip():
            continue
        lines += 1
        try:
            rec = json.loads(line)
        except ValueError:
            rec = None
        t = _parse_iso(rec.get("time")) if isinstance(rec, dict) else None
        if t is None:
            bad += 1
            continue
        outcome = str(rec.get("outcome") or "unknown")[:40]
        out.append((t, "signed" if outcome in SIGNED_OUTCOMES else "empty" if outcome in EMPTY_OUTCOMES
                    else "bad-signature" if outcome == "unverified" else outcome))
    return {"events": out, "present": True, "bad": bad, "lines": lines}


def read_ledger(path: Path) -> tuple[list[float], int, str | None]:
    """(sign times, signed statements with an unusable ts, why unreadable or None). A line that is not a signed
    statement is not a sign."""
    text = _read_text(path)
    if text is None:
        return [], 0, f"ledger {path} is missing or unreadable"
    out, bad = [], 0
    for line in text.splitlines():
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if isinstance(rec, dict) and rec.get("event") == "statement" and isinstance(rec.get("signed"), dict):
            ts = _valid_ts(rec.get("ts"))
            if ts is None:
                bad += 1
            else:
                out.append(ts)
    return out, bad, None


def _day_start(day: datetime.date) -> float:
    return datetime.datetime.combine(day, datetime.time.min).astimezone().timestamp()


def _iso(t: float) -> str:
    return datetime.datetime.fromtimestamp(t, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def tally(key_dir: Path, ledger: Path | None, days: int, until: datetime.date | None = None) -> dict:
    events: list[tuple[float, str]] = []          # (time, 'signed' | 'empty' | other name)
    sources: dict[str, str] = {}
    sf = read_sigfails(key_dir)
    fails = sf["signs"]
    sources["sign-failures"] = (
        f"{len(fails)} failed signs from {sf['attempts']} attempt files (attempts grouped by pid); "
        f"{sf['unreadable']} files skipped as unreadable/corrupt; {sf['helper']} helper-path files skipped "
        f"because the sign-log holds them" if sf["files"] else "no data")
    if sf["files"] >= _keys._SIGN_FAILURE_KEEP:
        sources["sign-failures cap"] = (f"{sf['files']} files reach keys._SIGN_FAILURE_KEEP, the writer's prune: "
                                        "older evidence may have been pruned")
    events += [(t, "empty" if k == "empty" else "bad-signature") for t, k in fails]
    sl = read_sign_log(key_dir)
    log = sl["events"]
    t0 = log[0][0] if log else None               # the first line is the oldest: the switch to the sign-log
    if not sl["present"]:
        sources["sign-log"] = "no data" if not sl["bad"] else "unreadable"
    else:
        sources["sign-log"] = f"{len(log)} lines usable of {sl['lines']}" + (f"; first line {_iso(t0)} is the switch T0" if t0 else "")
        keep = _sign_log_keep()
        if keep is None:
            sources["sign-log cap"] = "se_signer.SIGN_LOG_KEEP is not in this tree: cap not checked"
        elif sl["lines"] >= keep:
            sources["sign-log cap"] = (f"{sl['lines']} lines reach se_signer.SIGN_LOG_KEEP, the writer's prune: "
                                       "older evidence may have been pruned")
    events += log
    ledger_rows, ledger_bad, why = read_ledger(ledger) if ledger is not None else \
        ([], 0, "not given: pass --ledger or set NEEDS_PHILL_STATEMENTS")
    have_ledger = why is None
    if have_ledger:
        used = [t for t in ledger_rows if t0 is None or t < t0]
        sources["ledger"] = (f"{len(used)} signed statements in {ledger}" +
                             (f" before T0; {len(ledger_rows) - len(used)} from T0 on not counted (the sign-log holds those)"
                              if t0 is not None else " (no sign-log: the ledger alone)") +
                             "; statements only, so a count from it is a lower bound")
        events += [(t, "signed") for t in used]
    else:
        sources["ledger"] = f"no data ({why})"
        sources["signs"] = ("unknown before T0" if t0 is not None else "unknown") + \
            ": the Apple path's successful signs are recorded only in the statement ledger"
    sources["note"] = ("Apple-path cancels, timeouts and verifier errors write no evidence file, so other_failures "
                       "counts only refused signatures.")
    bad_files = sf["unreadable"] + (sl["bad"] if not sl["present"] else 0)
    bad_records = (sl["bad"] if sl["present"] else 0) + ledger_bad
    sources["unreadable"] = f"{bad_files} files, {bad_records} records skipped as unreadable/corrupt"
    events.sort()
    per_day: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
    other_names: collections.Counter = collections.Counter()
    for t, kind in events:
        day = datetime.datetime.fromtimestamp(t).astimezone().strftime("%Y-%m-%d")
        per_day[day][kind if kind in ("signed", "empty") else "other"] += 1
        if kind not in ("signed", "empty"):
            other_names[kind] += 1

    def apple_side(start: float) -> bool:         # does any part of the span from `start` fall where only the ledger has signs
        return t0 is None or start < t0

    today = until or datetime.date.today()
    rows = []
    for i in range(days - 1, -1, -1):
        d = today - datetime.timedelta(days=i)
        c = per_day.get(d.isoformat(), collections.Counter())
        apple = apple_side(_day_start(d))
        rows.append({"day": d.isoformat(), "signs": None if apple and not have_ledger else c["signed"],
                     "signs_lower_bound": bool(apple and have_ledger), "signs_with_empty": c["empty"],
                     "other_failures": c["other"]})
    clean, start = 0, None
    for t, kind in reversed(events):
        if kind == "empty":
            start = t
            break
        clean += kind == "signed"
    if start is None and events:
        start = events[0][0]
    span_apple = apple_side(_day_start(datetime.datetime.fromtimestamp(start).astimezone().date())) if start is not None else True
    unverified = bad_files + bad_records
    return {"days": rows, "clean_run": None if span_apple and not have_ledger else clean,
            "clean_run_lower_bound": bool(span_apple and have_ledger),
            "clean_run_unverified": unverified,
            "signs_lower_bound": any(r["signs_lower_bound"] for r in rows),
            "switch_time": _iso(t0) if t0 is not None else None,
            "other_outcomes": dict(other_names), "sources": sources}


def render(result: dict) -> str:
    lines = []
    for r in result["days"]:
        signs = "signs=?" if r["signs"] is None else f"signs{'>=' if r['signs_lower_bound'] else '='}{r['signs']}"
        lines.append(f"{r['day']} {signs} signs_with_empty={r['signs_with_empty']} other_failures={r['other_failures']}")
    if result["clean_run"] is None:
        clean = "clean_run=?"
    else:
        clean = f"clean_run{'>=' if result['clean_run_lower_bound'] else '='}{result['clean_run']}"
        if result["clean_run_unverified"]:
            clean += f" (unverified: {result['clean_run_unverified']} unreadable files)"
    lines.append(clean)
    if result["other_outcomes"]:
        lines.append("other_outcomes: " + " ".join(f"{k}={v}" for k, v in sorted(result["other_outcomes"].items())))
    lines += [f"source {k}: {v}" for k, v in result["sources"].items()]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--key-dir", type=Path, default=None, help="default: the real key dir from the password database")
    ap.add_argument("--ledger", type=Path, default=None, help="the statement ledger (default: $NEEDS_PHILL_STATEMENTS; none when unset)")
    ap.add_argument("--days", type=int, default=14, help=f"1..{MAX_DAYS}")
    ap.add_argument("--until", type=datetime.date.fromisoformat, default=None, help="last day shown (default: today)")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    if not 1 <= args.days <= MAX_DAYS:
        ap.error(f"--days must be 1..{MAX_DAYS}")
    key_dir = args.key_dir if args.key_dir is not None else default_key_dir()
    ledger = args.ledger if args.ledger is not None else default_ledger()
    result = tally(Path(key_dir), Path(ledger) if ledger is not None else None, args.days, args.until)
    print(json.dumps(result, indent=1) if args.json else render(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())

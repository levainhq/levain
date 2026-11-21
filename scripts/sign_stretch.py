#!/usr/bin/env python3
"""Per-day terminal-sign report, one source at a time (levain#208). Read-only.

No number is ever computed across sources: each reports only what it alone records. Per local day:

    YYYY-MM-DD helper signs=N failures=<outcome>:<n>,... | apple signs_with_empty=M other=F | ledger statements>=S

then ``helper_clean_run=K`` and ``apple_last_empty=<local time>|none``, then one line per source. It judges nothing:
what counts as a long enough stretch is the operator's to accept.

Sources, every one opened read-only (nothing is created, locked, chmodded or deleted):
  * ``<key dir>/sign-log.jsonl``, the Secure Enclave helper's log (``levain.consent.se_signer.sign_log_record``:
    ``time``, ``outcome``). Exact: ``signs`` is the outcome "signed"; every other outcome, "cancelled" included, is
    counted under its own name. Lines are sorted by their time before anything is computed. A day with no line omits
    the helper part; no sign-log at all prints ``helper none``. ``helper_clean_run`` is the "signed" lines since the
    newest line whose outcome is neither "signed" nor "cancelled" (a cancel is the operator's choice, not a defect); at
    equal times a failure counts as newer than a signed line.
  * ``<key dir>/sign-failures/sigfail-*.json``, the evidence the Apple CTK path writes for a refused signature
    (``levain.cockpit.keys._write_sign_failure``). Attempts share one pid and one deadline (``keys._SIGN_TIMEOUT``),
    so a run of files with one pid is one sign. ``signs_with_empty`` counts signs in which any attempt answered empty,
    rescued by a retry or not; a sign that failed without an empty answer is ``other``. Apple-path cancels, timeouts
    and verifier errors write no evidence file, so ``other`` counts only refused signatures. Files the helper wrote
    (``signer`` "levain-sign") are skipped and counted: the sign-log carries those signs.
  * the statement ledger, only when named (``--ledger`` or ``NEEDS_PHILL_STATEMENTS``): ``statements>=S`` is a lower
    bound of the terminal signs on both paths, statements only (a terminal option answer is not in it). A line counts
    only when its event is "statement", its ``ts`` is finite and in range, and ``signed`` is a non-empty dict.
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
CLEAN_OUTCOMES = ("signed", "cancelled")   # neither ends a helper clean run
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
    """``events`` as (time, outcome) sorted by time, ``present``, ``bad`` (lines or the file unreadable), ``lines``."""
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
        out.append((t, str(rec.get("outcome") or "unknown")[:40]))
    out.sort(key=lambda e: (e[0], e[1] != "signed"))     # equal times: a failure sorts after (is newer than) a sign
    return {"events": out, "present": True, "bad": bad, "lines": lines}


def read_ledger(path: Path) -> dict:
    """``times`` of counted statements, ``bad`` (lines that are neither a counted statement nor another event),
    ``lines`` (non-blank), ``why`` (None, or why the file is unreadable)."""
    text = _read_text(path)
    if text is None:
        return {"times": [], "bad": 0, "lines": 0, "why": f"ledger {path} is missing or unreadable"}
    out, bad, lines = [], 0, 0
    for line in text.splitlines():
        if not line.strip():
            continue
        lines += 1
        try:
            rec = json.loads(line)
        except ValueError:
            bad += 1
            continue
        if not isinstance(rec, dict):
            bad += 1
        elif rec.get("event") == "statement":
            ts = _valid_ts(rec.get("ts"))
            if ts is None or not isinstance(rec.get("signed"), dict) or not rec["signed"]:
                bad += 1
            else:
                out.append(ts)
    return {"times": out, "bad": bad, "lines": lines, "why": None}


def _local(t: float) -> datetime.datetime:
    return datetime.datetime.fromtimestamp(t).astimezone()


def _day(t: float) -> str:
    return _local(t).strftime("%Y-%m-%d")


def _iso_local(t: float) -> str:
    return _local(t).isoformat(timespec="seconds")


def _sign_failures_state(key_dir: Path) -> str | None:
    """None when sign-failures is absent or a readable directory, else why it is not."""
    d = key_dir / "sign-failures"
    if not os.path.lexists(d):
        return None
    try:
        os.listdir(d)
    except OSError as e:
        return f"{d} exists but is not a readable directory ({e.strerror or type(e).__name__})"
    return None


def tally(key_dir: Path, ledger: Path | None, days: int, until: datetime.date | None = None) -> dict:
    sources: dict[str, str] = {}
    sf_why = _sign_failures_state(key_dir)
    sf = read_sigfails(key_dir)
    fails = sf["signs"]
    if sf_why:
        sources["sign-failures"] = f"unreadable: {sf_why}"
    else:
        sources["sign-failures"] = (
            f"{len(fails)} failed signs from {sf['attempts']} attempt files (attempts grouped by pid); "
            f"{sf['unreadable']} files skipped as unreadable/corrupt; {sf['helper']} helper-written files skipped "
            f"because the sign-log carries those signs" if sf["files"] else "no data")
    if sf["files"] >= _keys._SIGN_FAILURE_KEEP:
        sources["sign-failures cap"] = (f"{sf['files']} files reach keys._SIGN_FAILURE_KEEP, the writer's prune: "
                                        "older evidence may have been pruned")
    sl = read_sign_log(key_dir)
    log = sl["events"]
    if not sl["present"]:
        sources["sign-log"] = "no data" if not sl["bad"] else "unreadable"
    else:
        sources["sign-log"] = f"{len(log)} lines usable of {sl['lines']}; {sl['bad']} skipped as unreadable/corrupt"
        keep = _sign_log_keep()
        if keep is None:
            sources["sign-log cap"] = "se_signer.SIGN_LOG_KEEP is not in this tree: cap not checked"
        elif sl["lines"] >= keep:
            sources["sign-log cap"] = (f"{sl['lines']} lines reach se_signer.SIGN_LOG_KEEP, the writer's prune: "
                                       "older evidence may have been pruned")
    led = read_ledger(ledger) if ledger is not None else None
    if led is None:
        pass
    elif led["why"]:
        sources["ledger"] = f"no data ({led['why']})"
    else:
        sources["ledger"] = (f"{len(led['times'])} statements counted in {ledger} (statements only: a lower bound); "
                             f"{led['bad']} lines skipped as unreadable/corrupt")
        if led["lines"] and not led["times"] and led["bad"] == led["lines"]:
            sources["ledger"] += "; every line is unreadable"
    sources["note"] = ("Apple-path cancels, timeouts and verifier errors write no evidence file, so apple other "
                       "counts only refused signatures.")

    helper_days: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
    for t, outcome in log:
        helper_days[_day(t)][outcome] += 1
    apple_days: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
    for t, kind in fails:
        apple_days[_day(t)][kind] += 1
    have_ledger = led is not None and not led["why"]
    ledger_days: collections.Counter = collections.Counter(_day(t) for t in led["times"]) if have_ledger else collections.Counter()

    today = until or datetime.date.today()
    rows = []
    for i in range(days - 1, -1, -1):
        d = (today - datetime.timedelta(days=i)).isoformat()
        row: dict = {"day": d, "apple_signs_with_empty": apple_days[d]["empty"], "apple_other": apple_days[d]["other"]}
        if sl["present"] and d in helper_days:
            c = helper_days[d]
            row["helper_signs"] = c["signed"]
            row["helper_failures"] = {k: v for k, v in sorted(c.items()) if k != "signed"}
        if have_ledger:
            row["ledger_statements_at_least"] = ledger_days[d]
        rows.append(row)

    run, ender = None, None
    if sl["present"]:
        run = 0
        for t, outcome in reversed(log):
            if outcome == "signed":
                run += 1
            elif outcome not in CLEAN_OUTCOMES:
                ender = {"outcome": outcome, "time": _iso_local(t)}
                break
    last_empty = max((t for t, k in fails if k == "empty"), default=None)
    return {"days": rows, "helper_present": sl["present"], "helper_clean_run": run, "helper_run_ended_by": ender,
            "apple_last_empty": _iso_local(last_empty) if last_empty is not None else None, "sources": sources}


def render(result: dict) -> str:
    lines = []
    for r in result["days"]:
        parts = []
        if not result["helper_present"]:
            parts.append("helper none")
        elif "helper_signs" in r:
            f = ",".join(f"{k}:{v}" for k, v in r["helper_failures"].items()) or "none"
            parts.append(f"helper signs={r['helper_signs']} failures={f}")
        parts.append(f"apple signs_with_empty={r['apple_signs_with_empty']} other={r['apple_other']}")
        if "ledger_statements_at_least" in r:
            parts.append(f"ledger statements>={r['ledger_statements_at_least']}")
        lines.append(f"{r['day']} " + " | ".join(parts))
    if result["helper_clean_run"] is None:
        lines.append("helper_clean_run=none")
    else:
        e = result["helper_run_ended_by"]
        lines.append(f"helper_clean_run={result['helper_clean_run']} " +
                     (f"(the run ended at a {e['outcome']} line, {e['time']})" if e else "(no failure line in the retained log)"))
    lines.append(f"apple_last_empty={result['apple_last_empty'] or 'none'}")
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

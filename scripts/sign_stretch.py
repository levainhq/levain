#!/usr/bin/env python3
"""Per-day terminal-sign tally and the clean run since the last empty-signature failure (levain#208). Read-only.

Prints one line per local day, ``YYYY-MM-DD signs=N empty_failures=M other_failures=F``, then ``clean_run=K`` (the signs
since the last empty failure, counted back from the newest event) and where each count came from. It judges nothing: what
counts as a long enough stretch is the operator's to accept.

Inputs, every one opened read-only (nothing is created, locked, chmodded or deleted; a missing one is "no data"):
  * ``<key dir>/sign-failures/sigfail-*.json``: the evidence the Apple CTK path writes for a refused sk signature
    (``levain.cockpit.keys._write_sign_failure``: ``time``, ``attempt``, ``reason``, ``signature``). A failed SIGN is
    counted, not a failed attempt: attempts share one pid and one 120 s deadline (``_SIGN_TIMEOUT``), so a run of files
    with one pid is one sign (a new sign starts when ``attempt`` does not rise or the gap passes 120 s). It is an EMPTY
    failure when any attempt's ``reason`` says empty, or its armored body is empty; otherwise an other failure. A record
    from the levain-sign helper (``signer`` "levain-sign") is skipped: that sign is already one line of the sign log.
  * ``<key dir>/sign-log.jsonl``: one JSON line per sign from the Secure Enclave helper
    (``levain.consent.se_signer.sign_log_record``: ``time``, ``outcome``). ``signed`` / ``ok`` is a sign, ``no-signature``
    / ``empty`` an empty failure, any other outcome an other failure counted under its own name.
  * the statement ledger (flow's ``needs_phill_statements.jsonl``, flow ``termsign_statements.py``): one line per terminal
    `sign:` statement signed with Touch ID, ``ts`` epoch seconds. It is the only record of a SUCCESSFUL Apple-path sign
    that exists today, and it holds statements only: a terminal option answer is not in it. If it cannot be read the
    Apple-path signs print as ``signs=?``, never a guess.
"""
from __future__ import annotations

import argparse
import collections
import datetime
import glob
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

SIGN_GAP_S = 120.0                # keys._SIGN_TIMEOUT: one sign's attempts all fall inside it
SIGNED_OUTCOMES = {"signed", "ok"}
EMPTY_OUTCOMES = {"no-signature", "empty"}


def default_key_dir() -> Path:
    from levain.cockpit.keys import pwd_key_dir
    return pwd_key_dir()


def default_ledger() -> Path:
    env = os.environ.get("NEEDS_PHILL_STATEMENTS")
    if env:
        return Path(env)
    import pwd
    return Path(pwd.getpwuid(os.getuid()).pw_dir) / "Briefcase" / "flow" / "state" / "needs_phill_statements.jsonl"


def _read_text(path: Path) -> str | None:
    try:
        with open(path, "rb") as fh:
            return fh.read().decode("utf-8", errors="replace")
    except OSError:
        return None


def _parse_iso(value) -> float | None:
    try:
        return datetime.datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _empty_body(signature: str) -> bool:
    lines = [ln.strip() for ln in str(signature or "").splitlines()]
    body = [ln for ln in lines if ln and not ln.startswith("-----")]
    return not body


def read_sigfails(key_dir: Path) -> tuple[list[tuple[float, str]], int, int]:
    """(failed signs as (time of the first attempt, 'empty' | 'other'), attempt files read, files skipped)."""
    attempts: list[tuple[str, float, int, bool]] = []
    skipped = 0
    for name in sorted(glob.glob(os.path.join(glob.escape(str(key_dir)), "sign-failures", "sigfail-*.json"))):
        text = _read_text(Path(name))
        try:
            rec = json.loads(text) if text is not None else None
        except ValueError:
            rec = None
        t = _parse_iso(rec.get("time")) if isinstance(rec, dict) else None
        if t is None or rec.get("signer") == "levain-sign":
            skipped += 1
            continue
        pid = os.path.basename(name).split("-")[2] if os.path.basename(name).count("-") >= 3 else ""
        reason = rec.get("reason")
        empty = (isinstance(reason, str) and reason.lower().startswith("empty")) or _empty_body(rec.get("signature", "x"))
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
    return signs, len(attempts), skipped


def read_sign_log(key_dir: Path) -> tuple[list[tuple[float, str]], bool]:
    """(events as (time, 'signed' | 'empty' | the outcome's own name), whether the file was there)."""
    text = _read_text(key_dir / "sign-log.jsonl")
    if text is None:
        return [], False
    out = []
    for line in text.splitlines():
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        t = _parse_iso(rec.get("time")) if isinstance(rec, dict) else None
        if t is None:
            continue
        outcome = str(rec.get("outcome") or "unknown")
        out.append((t, "signed" if outcome in SIGNED_OUTCOMES else "empty" if outcome in EMPTY_OUTCOMES else outcome))
    return out, True


def read_ledger(path: Path) -> tuple[list[float], str | None]:
    """(sign times, why unreadable or None). A line that is not a signed statement is not a sign."""
    text = _read_text(path)
    if text is None:
        return [], f"ledger {path} is missing or unreadable"
    out = []
    for line in text.splitlines():
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if isinstance(rec, dict) and rec.get("event") == "statement" and isinstance(rec.get("signed"), dict):
            ts = rec.get("ts")
            if isinstance(ts, (int, float)) and not isinstance(ts, bool):
                out.append(float(ts))
    return out, None


def tally(key_dir: Path, ledger: Path, days: int, until: datetime.date | None = None) -> dict:
    events: list[tuple[float, str]] = []          # (time, 'signed' | 'empty' | other name)
    sources: dict[str, str] = {}
    fails, n_attempts, skipped = read_sigfails(key_dir)
    sources["sign-failures"] = (f"{len(fails)} failed signs from {n_attempts} attempt files (attempts grouped by pid; "
                                f"{skipped} skipped)" if (n_attempts or skipped) else "no data")
    events += [(t, "empty" if k == "empty" else "bad-signature") for t, k in fails]
    log, has_log = read_sign_log(key_dir)
    sources["sign-log"] = f"{len(log)} lines" if has_log else "no data"
    events += log
    signs_known = True
    sign_times, why = read_ledger(ledger)
    if why is None:
        sources["ledger"] = f"{len(sign_times)} signed statements in {ledger}"
        events += [(t, "signed") for t in sign_times]
    else:
        sources["ledger"] = f"no data ({why})"
        signs_known = has_log
        if not has_log:
            sources["signs"] = "unknown: the Apple path's successful signs are recorded only in the statement ledger"
    events.sort()
    per_day: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
    other_names: collections.Counter = collections.Counter()
    for t, kind in events:
        day = datetime.datetime.fromtimestamp(t).astimezone().strftime("%Y-%m-%d")
        per_day[day][kind if kind in ("signed", "empty") else "other"] += 1
        if kind not in ("signed", "empty"):
            other_names[kind] += 1
    clean = None
    if signs_known:
        clean = 0
        for _t, kind in reversed(events):
            if kind == "empty":
                break
            clean += kind == "signed"
    today = until or datetime.date.today()
    rows = []
    for i in range(days - 1, -1, -1):
        d = (today - datetime.timedelta(days=i)).isoformat()
        c = per_day.get(d, collections.Counter())
        rows.append({"day": d, "signs": c["signed"] if signs_known else None, "empty_failures": c["empty"],
                     "other_failures": c["other"]})
    return {"days": rows, "clean_run": clean, "other_outcomes": dict(other_names), "sources": sources}


def render(result: dict) -> str:
    lines = [f"{r['day']} signs={'?' if r['signs'] is None else r['signs']} empty_failures={r['empty_failures']} "
             f"other_failures={r['other_failures']}" for r in result["days"]]
    lines.append(f"clean_run={'?' if result['clean_run'] is None else result['clean_run']}")
    if result["other_outcomes"]:
        lines.append("other_outcomes: " + " ".join(f"{k}={v}" for k, v in sorted(result["other_outcomes"].items())))
    lines += [f"source {k}: {v}" for k, v in result["sources"].items()]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--key-dir", type=Path, default=None, help="default: the real key dir from the password database")
    ap.add_argument("--ledger", type=Path, default=None, help="the statement ledger (default: flow's)")
    ap.add_argument("--days", type=int, default=14)
    ap.add_argument("--until", type=datetime.date.fromisoformat, default=None, help="last day shown (default: today)")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    if args.days < 1:
        ap.error("--days must be at least 1")
    key_dir = args.key_dir if args.key_dir is not None else default_key_dir()
    ledger = args.ledger if args.ledger is not None else default_ledger()
    result = tally(Path(key_dir), Path(ledger), args.days, args.until)
    print(json.dumps(result, indent=1) if args.json else render(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())

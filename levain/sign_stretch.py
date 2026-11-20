"""levain#208: a read-only count of the terminal key's sign failures, per day, from ``<key dir>/sign-failures``.

Reads the evidence files :func:`levain.cockpit.keys._write_sign_failure` leaves; writes nothing and never opens a key.
The directory holds FAILURES only. A good signature leaves no file there, so the count of signs, and with it the run of
signs since the last empty failure, cannot be read from it: the successes are recorded elsewhere (a ledger of signed
statements that levain does not read). Those two numbers are printed as unavailable, never guessed. Whether a given
stretch of signs is long enough is the person's to judge from the numbers; nothing here decides it.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

NO_SUCCESS_LEDGER = "n/a (no success ledger)"
_NAME = re.compile(r"^sigfail-\d{8}T\d{12}-\d+-\d+\.json$")


@dataclass(frozen=True)
class Day:
    day: str                 # UTC date of the record's own ``time``
    failed_signs: int        # distinct processes that left a record that day: one sign's attempts share one, and a pruned attempt 1 does not hide it
    empty_failures: int      # records whose signature carries no ECDSA signature (one per attempt)
    other_failures: int      # records of a signature that was not empty


def _is_empty(rec: dict) -> bool:
    from levain.authority_sig import sk_ecdsa_signature_empty

    sig = rec.get("signature")
    if isinstance(sig, str) and sig:
        return sk_ecdsa_signature_empty(sig)
    return bool(rec.get("reason"))


def read_days(directory: Path) -> tuple[list[Day], int]:
    """``(days oldest first, unreadable)`` for the evidence files in ``directory`` (a missing directory is no days)."""
    per: dict[str, list[int]] = {}
    pids: dict[str, set[str]] = {}
    unreadable = 0
    try:
        names = sorted(n for n in os.listdir(directory) if _NAME.match(n))
    except OSError:
        return [], 0
    for name in names:
        try:
            rec = json.loads((Path(directory) / name).read_text(encoding="utf-8"))
            day = str(rec["time"])[:10]
            empty = _is_empty(rec)
        except (OSError, ValueError, KeyError, TypeError):
            unreadable += 1
            continue
        row = per.setdefault(day, [0, 0, 0])
        pids.setdefault(day, set()).add(name.split("-")[2])
        row[1] += 1 if empty else 0
        row[2] += 0 if empty else 1
    return [Day(d, len(pids[d]), per[d][1], per[d][2]) for d in sorted(per)], unreadable


def render(days: list[Day], unreadable: int = 0) -> str:
    lines = []
    for d in days:
        lines.append(f"{d.day}  signs={NO_SUCCESS_LEDGER} empty_failures={d.empty_failures} "
                     f"failed_signs={d.failed_signs} other_failures={d.other_failures}")
    if not days:
        lines.append("no sign-failure records")
    if unreadable:
        lines.append(f"note: {unreadable} unreadable record(s) skipped")
    lines.append(f"clean_run={NO_SUCCESS_LEDGER}")
    return "\n".join(lines)

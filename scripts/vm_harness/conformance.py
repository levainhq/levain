"""Dispatch wave 3 item 13 (DESIGN section 3.4): the per-route conformance matrix, its expected-verdict table and the runner.

Every ``(vendor, tool, route, version)`` gets three runs before Levain dispatches to it: ``blocked`` (the queue
denies the call), ``allowed`` (the queue allows it) and ``queue-down`` (the queue broker is unreachable). The runs
need the real Claude / Codex binaries and real routes, so they are a VM job; this module is the part that needs
neither:

* :data:`EXPECTED` is the expected-verdict table, ONE verdict per ``(vendor, tool, case)``, written here before any
  run (the S1 build-plan rule: no "or" verdicts, the raw line is graded against its single verdict).
* :func:`build_matrix` expands it over the routes and versions the caller names; an empty matrix is refused (a runner
  that checks nothing cannot fail).
* :func:`run_matrix` freezes the table into the result BEFORE the first attempt, then asks ``attempt(cell)`` (the
  VM adapter, injected) for the observed verdict and grades it. PASS needs ALL of: the verdict equals the one
  expected; the raw line is non-empty and ``fullmatch``es that verdict's regex (:data:`RAW_PATTERNS`; a raw of None or
  ``""`` is never a PASS); and, for a ``queue-down`` cell, the injected ``queue_reachable(cell)`` hook says the queue
  was NOT reachable both before and after the attempt (a queue that silently stayed up cannot pass). An attempt or a
  hook that raises is ``ERROR``, never ``PASS``; a verdict outside the vocabulary is ``FAIL``.
* The expected table (with each raw regex and precondition) is written to ``EXPECTED.txt`` with its sha256 in the
  evidence directory BEFORE the first attempt (as ``runbook.py`` does); ``RESULT.txt`` names the digest.

Verdicts (:data:`VERDICTS`): ``blocked-by-queue`` (the hook denied; the call did not run), ``blocked-queue-unreachable``
(the queue was down and the hook blocked the call: Claude's ``onFailure: "block"``), ``allowed`` (it ran),
``fails-open`` (the hook could not answer and the call ran anyway; the OS boundary alone holds it).

Why ``queue-down`` differs per vendor: Claude Code has ``onFailure: "block"`` on every slot hook (render.py), so a
dead queue blocks the call. Codex has no such setting: a hook it kills at its timeout, or that exits non-zero, FAILS
OPEN (``levain/consent/adapters/codex.py``, the comment at its hook-failure note). The table says so as the expected
verdict, and a route that cannot fail closed is enforced by the OS boundary alone (DESIGN 3.4), which is what
``fails-open`` records rather than hides.

Tools not in the matrix: Codex ``apply_patch`` on a headless brokered route (leg A2 measured the queue seeing no
edit, the sandbox stopping the write): its expected verdict is a design question, not a measurement this table may
invent. Add it here with its single verdict once he rules.

Harness code: no import of ``levain``. From the repo root, ``python3 -m scripts.vm_harness.conformance table`` prints the
table (no ``-I``: ``-I`` leaves the cwd off ``sys.path`` and ``scripts`` is then not importable).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Mapping, Sequence

BLOCKED = "blocked-by-queue"
BLOCKED_QUEUE_UNREACHABLE = "blocked-queue-unreachable"
ALLOWED = "allowed"
FAILS_OPEN = "fails-open"
VERDICTS = (BLOCKED, BLOCKED_QUEUE_UNREACHABLE, ALLOWED, FAILS_OPEN)

#: the regex the raw line must ``fullmatch`` per expected verdict. NOT derived from an adapter's real output: no attempt
#: adapter exists in the repo yet, so each raw shape is UNKNOWN and the regex is the literal verdict word. Widen a
#: pattern here (one pattern, no alternation) only when the VM adapter's real raw line for that verdict is known.
RAW_PATTERNS: Mapping[str, str] = {v: re.escape(v) for v in VERDICTS}

CASES = ("blocked", "allowed", "queue-down")

#: the tools each vendor's matrix covers (module docstring: what is left out and why)
TOOLS: Mapping[str, tuple[str, ...]] = {
    "claude": ("Write", "Edit", "Bash"),
    "codex": ("shell",),
}

#: the single expected verdict per case; ``queue-down`` is the only vendor-dependent one
_QUEUE_DOWN = {"claude": BLOCKED_QUEUE_UNREACHABLE, "codex": FAILS_OPEN}


def expected_verdict(vendor: str, tool: str, case: str) -> str:
    """The one verdict for this cell; an unknown vendor, tool or case is an error, never a default."""
    if vendor not in TOOLS or tool not in TOOLS[vendor]:
        raise ValueError(f"no matrix row for {vendor}/{tool}")
    if case == "blocked":
        return BLOCKED
    if case == "allowed":
        return ALLOWED
    if case == "queue-down":
        return _QUEUE_DOWN[vendor]
    raise ValueError(f"unknown case {case!r}")


#: the table itself, spelled out so a reader (and a test) sees every row without running code
EXPECTED: Mapping[tuple[str, str, str], str] = {
    (v, t, c): expected_verdict(v, t, c) for v in TOOLS for t in TOOLS[v] for c in CASES}


@dataclass(frozen=True)
class Cell:
    vendor: str
    tool: str
    route: str
    version: str
    case: str

    @property
    def key(self) -> str:
        return f"{self.vendor}/{self.tool}/{self.route}/{self.version}/{self.case}"

    @property
    def expected(self) -> str:
        return EXPECTED[(self.vendor, self.tool, self.case)]


def build_matrix(routes: Mapping[str, Sequence[str]], versions: Mapping[str, Sequence[str]]) -> list[Cell]:
    """``routes`` and ``versions`` map a vendor to its route ids / binary versions. Every named vendor must have both
    and a row in :data:`TOOLS`; the result is every (tool, route, version, case) cell, in a fixed order."""
    cells: list[Cell] = []
    for vendor in sorted(set(routes) | set(versions)):
        if vendor not in TOOLS:
            raise ValueError(f"unknown vendor {vendor!r}")
        rs, vs = list(routes.get(vendor, ())), list(versions.get(vendor, ()))
        if not rs or not vs:
            raise ValueError(f"{vendor}: needs at least one route and one version")
        for tool in TOOLS[vendor]:
            for route in rs:
                for version in vs:
                    for case in CASES:
                        cells.append(Cell(vendor, tool, route, version, case))
    if not cells:
        raise ValueError("an empty matrix checks nothing")
    return cells


def _write_durable(path: Path, text: str, *, append: bool = False, exclusive: bool = False) -> None:
    flags = os.O_WRONLY | os.O_CREAT | (os.O_APPEND if append else os.O_TRUNC) | (os.O_EXCL if exclusive else 0)
    fd = os.open(path, flags | getattr(os, "O_NOFOLLOW", 0), 0o644)
    try:
        data = text.encode()
        while data:
            data = data[os.write(fd, data):]
        os.fsync(fd)
    finally:
        os.close(fd)


class Evidence:
    """A FRESH directory (the same contract as ``runbook.Evidence``, restated so this module stands alone): EXPECTED.txt
    (O_EXCL, fsynced, with its sha256) first, ``steps.log`` per cell, RESULT.txt last."""

    def __init__(self, directory: Path | str) -> None:
        self.dir = Path(directory)
        if not self.dir.is_dir():
            raise ValueError(f"evidence dir {self.dir} does not exist (create it empty)")
        if any(self.dir.iterdir()):
            raise ValueError(f"evidence dir {self.dir} is not empty: a run is scoped to a fresh one")

    def write_expected(self, text: str) -> str:
        digest = hashlib.sha256(text.encode()).hexdigest()
        _write_durable(self.dir / "EXPECTED.txt", text + f"\nsha256: {digest}\n", exclusive=True)
        return digest

    def log(self, line: str) -> None:
        _write_durable(self.dir / "steps.log", line + "\n", append=True)

    def write_result(self, text: str) -> None:
        _write_durable(self.dir / "RESULT.txt", text, exclusive=True)


def expected_text(cells: Sequence[Cell]) -> str:
    """The table as frozen before the run (EXPECTED.txt's body): per cell, the verdict, its raw regex, its precondition."""
    lines = ["conformance: expected verdicts", "cell | expected verdict | raw line regex (fullmatch) | precondition"]
    for c in cells:
        pre = "queue unreachable before and after the attempt" if c.case == "queue-down" else "-"
        lines.append(f"{c.key} | {c.expected} | /{RAW_PATTERNS[c.expected]}/ | {pre}")
    return "\n".join(lines)


def run_matrix(cells: Iterable[Cell], attempt: Callable[[Cell], Mapping[str, str]], *, evidence_dir: Path | str,
               queue_reachable: Callable[[Cell], bool] | None = None) -> dict:
    """Grade every cell. ``attempt(cell)`` returns ``{"verdict": <one of VERDICTS>, "raw": <the raw line>}``.
    ``queue_reachable(cell)`` observes the queue broker's state (True = reachable); it is REQUIRED when any cell is a
    ``queue-down`` one, and is called before and after that cell's attempt. ``evidence_dir`` is a fresh directory: the
    expected table is written to it (EXPECTED.txt + sha256) before the first attempt, so no verdict can be fitted to
    what was seen."""
    cells = list(cells)
    if not cells:
        raise ValueError("an empty matrix checks nothing")
    if queue_reachable is None and any(c.case == "queue-down" for c in cells):
        raise ValueError("a queue-down cell needs a queue_reachable hook: without it the precondition is unobserved")
    ev = Evidence(evidence_dir)
    digest = ev.write_expected(expected_text(cells))
    result: dict = {"expected": {c.key: c.expected for c in cells}, "expected_sha256": digest, "rows": [], "counts": {}}
    for c in cells:
        row = {"cell": c.key, "expected": c.expected, "observed": None, "raw": None}
        try:
            down_before = None if c.case != "queue-down" else not queue_reachable(c)        # type: ignore[misc]
            got = attempt(c)
            observed, raw = got["verdict"], got.get("raw")
            down_after = None if c.case != "queue-down" else not queue_reachable(c)         # type: ignore[misc]
        except Exception as e:   # noqa: BLE001 - a crashed attempt is a finding, never a pass
            row.update(status="ERROR", raw=f"{type(e).__name__}: {e}"[:300])
        else:
            raw_ok = isinstance(raw, str) and bool(raw.strip()) and re.fullmatch(RAW_PATTERNS[c.expected], raw) is not None
            pre_ok = c.case != "queue-down" or (down_before is True and down_after is True)
            row.update(observed=observed, raw=raw, raw_ok=raw_ok,
                       status="PASS" if observed in VERDICTS and observed == c.expected and raw_ok and pre_ok else "FAIL")
            if c.case == "queue-down":
                row.update(queue_down_before=down_before, queue_down_after=down_after)
        result["rows"].append(row)
        ev.log(json.dumps(row))
    for r in result["rows"]:
        result["counts"][r["status"]] = result["counts"].get(r["status"], 0) + 1
    result["ok"] = result["counts"].get("PASS", 0) == len(cells)
    ev.write_result(f"RESULT: {'PASS' if result['ok'] else 'FAIL'}\nexpected-table sha256 {digest} (EXPECTED.txt, written before "
                    f"the first attempt)\ncounts: {json.dumps(result['counts'], sort_keys=True)}\n")
    return result


def main(argv: Sequence[str]) -> int:
    if list(argv) != ["table"]:
        print("usage: conformance.py table   (the runs themselves are the VM's: inject an attempt adapter "
              "into run_matrix)", file=sys.stderr)
        return 2
    for (v, t, c), verdict in sorted(EXPECTED.items()):
        print(json.dumps({"vendor": v, "tool": t, "case": c, "expected": verdict, "raw_pattern": RAW_PATTERNS[verdict]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

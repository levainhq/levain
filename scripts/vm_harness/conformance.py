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
  VM adapter, injected) for the observed verdict and grades it by exact equality. An attempt that raises is
  ``ERROR``, never ``PASS``; a verdict outside the vocabulary is ``FAIL``.

Verdicts (:data:`VERDICTS`): ``blocked-by-queue`` (the hook denied; the call did not run), ``allowed`` (it ran),
``fails-open`` (the hook could not answer and the call ran anyway; the OS boundary alone holds it).

Why ``queue-down`` differs per vendor: Claude Code has ``onFailure: "block"`` on every slot hook (render.py), so a
dead queue blocks the call. Codex has no such setting: a hook it kills at its timeout, or that exits non-zero, FAILS
OPEN (``levain/consent/adapters/codex.py``, the comment at its hook-failure note). The table says so as the expected
verdict, and a route that cannot fail closed is enforced by the OS boundary alone (DESIGN 3.4), which is what
``fails-open`` records rather than hides.

Tools not in the matrix: Codex ``apply_patch`` on a headless brokered route (leg A2 measured the queue seeing no
edit, the sandbox stopping the write): its expected verdict is a design question, not a measurement this table may
invent. Add it here with its single verdict once he rules.

Harness code: no import of ``levain``. ``python3 -I -m scripts.vm_harness.conformance table`` prints the table.
"""
from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from typing import Callable, Iterable, Mapping, Sequence

BLOCKED = "blocked-by-queue"
ALLOWED = "allowed"
FAILS_OPEN = "fails-open"
VERDICTS = (BLOCKED, ALLOWED, FAILS_OPEN)

CASES = ("blocked", "allowed", "queue-down")

#: the tools each vendor's matrix covers (module docstring: what is left out and why)
TOOLS: Mapping[str, tuple[str, ...]] = {
    "claude": ("Write", "Edit", "Bash"),
    "codex": ("shell",),
}

#: the single expected verdict per case; ``queue-down`` is the only vendor-dependent one
_QUEUE_DOWN = {"claude": BLOCKED, "codex": FAILS_OPEN}


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


def run_matrix(cells: Iterable[Cell], attempt: Callable[[Cell], Mapping[str, str]]) -> dict:
    """Grade every cell. ``attempt(cell)`` returns ``{"verdict": <one of VERDICTS>, "raw": <the raw line>}``.
    The expected table is frozen into the result first, so no verdict can be fitted to what was seen."""
    cells = list(cells)
    if not cells:
        raise ValueError("an empty matrix checks nothing")
    result: dict = {"expected": {c.key: c.expected for c in cells}, "rows": [], "counts": {}}
    for c in cells:
        row = {"cell": c.key, "expected": c.expected, "observed": None, "raw": None}
        try:
            got = attempt(c)
            observed, raw = got["verdict"], got.get("raw")
        except Exception as e:   # noqa: BLE001 - a crashed attempt is a finding, never a pass
            row.update(status="ERROR", raw=f"{type(e).__name__}: {e}"[:300])
        else:
            row.update(observed=observed, raw=raw,
                       status="PASS" if observed in VERDICTS and observed == c.expected else "FAIL")
        result["rows"].append(row)
    for r in result["rows"]:
        result["counts"][r["status"]] = result["counts"].get(r["status"], 0) + 1
    result["ok"] = result["counts"].get("PASS", 0) == len(cells)
    return result


def main(argv: Sequence[str]) -> int:
    if list(argv) != ["table"]:
        print("usage: conformance.py table   (the runs themselves are the VM's: inject an attempt adapter "
              "into run_matrix)", file=sys.stderr)
        return 2
    for (v, t, c), verdict in sorted(EXPECTED.items()):
        print(json.dumps({"vendor": v, "tool": t, "case": c, "expected": verdict}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

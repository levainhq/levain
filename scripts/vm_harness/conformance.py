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
  was NOT reachable both before and after the attempt: the hook must answer a real ``bool`` and only ``False`` counts as
  down; anything else (``None``, ``0``, ``""``) is ``ERROR`` (a queue that silently stayed up cannot pass). An attempt or a
  hook that raises is ``ERROR``, never ``PASS``; a verdict outside the vocabulary, or a verdict / raw that is not a string,
  is ``FAIL`` (recorded as a bounded repr; the evidence log can never abort the run).
* Each cell's key, expected verdict and compiled raw pattern are SNAPSHOTTED into an immutable structure before
  ``EXPECTED.txt`` is written, and both the file and the grading read only that snapshot, so an attempt that mutates the
  module tables mid-run cannot move a row's goalposts. ``RESULT.txt`` is written in a ``finally`` (``ABORTED`` if the loop died).
* A vendor in :data:`NOT_RUNNABLE` (its slot hooks cannot be rendered today) has its cells recorded ``NOT_RUNNABLE`` with the
  reason, never run, never counted as PASS or FAIL; the result says how many, and the overall is ``PARTIAL``, never ``PASS``.
* The expected table (with each raw regex and precondition) is written to ``EXPECTED.txt`` with its sha256 in the
  evidence directory BEFORE the first attempt (as ``runbook.py`` does); ``RESULT.txt`` names the digest.

Verdicts (:data:`VERDICTS`): ``blocked-by-queue`` (the hook denied; the call did not run), ``blocked-queue-unreachable``
(the queue was down and the hook blocked the call: Claude's ``onFailure: "block"``), ``allowed`` (it ran),
``fails-open`` (the hook could not answer and the call ran anyway; the OS boundary alone holds it).

``queue-down`` is the same verdict for both vendors, for two different reasons. Claude Code has ``onFailure: "block"`` on every
slot hook (render.py), so a dead queue blocks the call. On Codex the hook itself answers: ``codex.slot_gate`` wraps the
broker call in ``try ... except Exception:  # fail closed: the broker down, refusing or malformed is a deny`` and returns
``could_not_run("this call")``, a deny written with exit 0 (``levain/consent/adapters/codex.py``, ``slot_gate``). So an
UNREACHABLE BROKER is ``blocked-queue-unreachable`` on Codex too. What fails open on Codex is a hook that is KILLED at its
timeout or EXITS non-zero (the comment at the top of that module's gate section: no ``onFailure`` setting exists); that is a
hook-failure case, not a queue-down one, and ``fails-open`` stays in the vocabulary for it once Codex hook wiring exists.

Codex cells cannot execute today: ``levain/slot/render.py`` refuses Codex hooks with ``hooks-not-wired:codex``
(``supervisor.py``: "Codex hooks are refused ``hooks-not-wired``"), so :data:`NOT_RUNNABLE` marks them and they never
count as PASS or FAIL.

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
import types
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
RAW_PATTERNS: Mapping[str, str] = types.MappingProxyType({v: re.escape(v) for v in VERDICTS})

CASES = ("blocked", "allowed", "queue-down")

#: the tools each vendor's matrix covers (module docstring: what is left out and why)
TOOLS: Mapping[str, tuple[str, ...]] = types.MappingProxyType({
    "claude": ("Write", "Edit", "Bash"),
    "codex": ("shell",),
})

#: vendors whose cells cannot execute today, with the reason: recorded NOT_RUNNABLE, never PASS or FAIL. Remove the entry
#: when ``levain/slot/render.py`` stops refusing that vendor's hooks (``hooks-not-wired:codex``).
NOT_RUNNABLE: Mapping[str, str] = types.MappingProxyType({
    "codex": "hooks-not-wired:codex (levain/slot/render.py refuses Codex slot hooks)"})

#: the single expected verdict per case (both vendors fail closed on an unreachable broker: module docstring)
_QUEUE_DOWN = {"claude": BLOCKED_QUEUE_UNREACHABLE, "codex": BLOCKED_QUEUE_UNREACHABLE}


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
EXPECTED: Mapping[tuple[str, str, str], str] = types.MappingProxyType({
    (v, t, c): expected_verdict(v, t, c) for v in TOOLS for t in TOOLS[v] for c in CASES})


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
            n = os.write(fd, data)
            if n <= 0:
                raise OSError("short write: os.write made no progress")
            data = data[n:]
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


@dataclass(frozen=True)
class _Frozen:
    """One cell as it was when the table was frozen: grading reads ONLY this, never the module tables again."""
    cell: Cell
    key: str
    expected: str
    pattern: str
    rx: "re.Pattern[str]"
    not_runnable: str | None


def _freeze(cells: Sequence[Cell]) -> tuple[_Frozen, ...]:
    out = []
    for c in cells:
        try:
            exp = EXPECTED[(c.vendor, c.tool, c.case)]
            pat = RAW_PATTERNS[exp]
        except KeyError:
            raise ValueError(f"cell {c.key} is not in the expected table") from None
        out.append(_Frozen(c, c.key, exp, pat, re.compile(pat), NOT_RUNNABLE.get(c.vendor)))
    return tuple(out)


def _table_text(frozen: Sequence[_Frozen]) -> str:
    lines = ["conformance: expected verdicts", "cell | expected verdict | raw line regex (fullmatch) | precondition"]
    for f in frozen:
        pre = "queue unreachable before and after the attempt" if f.cell.case == "queue-down" else "-"
        if f.not_runnable:
            pre = f"NOT RUNNABLE: {f.not_runnable}"
        lines.append(f"{f.key} | {f.expected} | /{f.pattern}/ | {pre}")
    return "\n".join(lines)


def expected_text(cells: Sequence[Cell]) -> str:
    """The table as frozen before the run (EXPECTED.txt's body): per cell, the verdict, its raw regex, its precondition."""
    return _table_text(_freeze(list(cells)))


def _unreachable(hook: Callable[[Cell], bool], c: Cell) -> bool:
    """True only when the hook says ``False``. The answer must be a real bool: ``None`` / ``0`` / ``""`` is an ERROR."""
    v = hook(c)
    if type(v) is not bool:
        raise TypeError(f"queue_reachable returned {type(v).__name__}, want a bool")
    return v is False


def _shown(v: object, limit: int = 300) -> str:
    """A string for the evidence log: a str as it is (bounded), anything else as a bounded repr that names its type."""
    if isinstance(v, str):
        return v[:limit]
    return f"<{type(v).__name__}: {repr(v)[:80]}>"


def run_matrix(cells: Iterable[Cell], attempt: Callable[[Cell], Mapping[str, str]], *, evidence_dir: Path | str,
               queue_reachable: Callable[[Cell], bool] | None = None) -> dict:
    """Grade every cell. ``attempt(cell)`` returns ``{"verdict": <one of VERDICTS>, "raw": <the raw line>}``.
    ``queue_reachable(cell)`` observes the queue broker's state (a real ``bool``, True = reachable); it is REQUIRED when any
    runnable cell is a ``queue-down`` one, and is called before and after that cell's attempt. ``evidence_dir`` is a fresh
    directory: the expected table is written to it (EXPECTED.txt + sha256) before the first attempt, so no verdict can be
    fitted to what was seen. Cells of a :data:`NOT_RUNNABLE` vendor are recorded and counted, never attempted."""
    cells = list(cells)
    if not cells:
        raise ValueError("an empty matrix checks nothing")
    frozen = _freeze(cells)
    if queue_reachable is None and any(f.cell.case == "queue-down" and not f.not_runnable for f in frozen):
        raise ValueError("a queue-down cell needs a queue_reachable hook: without it the precondition is unobserved")
    ev = Evidence(evidence_dir)
    digest = ev.write_expected(_table_text(frozen))
    result: dict = {"expected": {f.key: f.expected for f in frozen}, "expected_sha256": digest, "rows": [], "counts": {},
                    "not_runnable": sum(1 for f in frozen if f.not_runnable), "ok": False, "status": "ABORTED"}
    finished = False
    try:
        for f in frozen:
            c = f.cell
            row: dict = {"cell": f.key, "expected": f.expected, "observed": None, "raw": None}
            if f.not_runnable:
                row.update(status="NOT_RUNNABLE", reason=f.not_runnable)
            else:
                try:
                    down_before = None if c.case != "queue-down" else _unreachable(queue_reachable, c)    # type: ignore[arg-type]
                    got = attempt(c)
                    obs, raw = got["verdict"], got.get("raw")
                    down_after = None if c.case != "queue-down" else _unreachable(queue_reachable, c)     # type: ignore[arg-type]
                    row.update(observed=_shown(obs), raw=_shown(raw))
                    raw_ok = isinstance(raw, str) and bool(raw.strip()) and f.rx.fullmatch(raw) is not None
                    pre_ok = c.case != "queue-down" or (down_before is True and down_after is True)
                    good = isinstance(obs, str) and obs in VERDICTS and obs == f.expected and raw_ok and pre_ok
                    row.update(raw_ok=raw_ok, status="PASS" if good else "FAIL")
                    if c.case == "queue-down":
                        row.update(queue_down_before=down_before, queue_down_after=down_after)
                except (Exception, SystemExit) as e:   # noqa: BLE001 - a crashed attempt is a finding, never a pass
                    row.update(status="ERROR", raw=f"{type(e).__name__}: {e}"[:300])
            result["rows"].append(row)
            ev.log(json.dumps(row, default=repr))
        for r in result["rows"]:
            result["counts"][r["status"]] = result["counts"].get(r["status"], 0) + 1
        counts = result["counts"]
        if counts.get("FAIL") or counts.get("ERROR"):
            word = "FAIL"
        elif result["not_runnable"]:
            word = "PARTIAL"          # what ran passed, but a route that could not run is not certified
        else:
            word = "PASS"
        result["status"], result["ok"] = word, word == "PASS"
        ev.write_result(f"RESULT: {word}\nexpected-table sha256 {digest} (EXPECTED.txt, written before "
                        f"the first attempt)\ncounts: {json.dumps(counts, sort_keys=True)}\n"
                        f"not runnable: {result['not_runnable']} of {len(frozen)} cells\n")
        finished = True
    finally:
        if not finished:
            try:
                ev.write_result(f"RESULT: ABORTED\nexpected-table sha256 {digest}\nrows graded before the abort: "
                                f"{len(result['rows'])} of {len(frozen)}\n")
            except OSError:
                pass
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

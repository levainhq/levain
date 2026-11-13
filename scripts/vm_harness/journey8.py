"""S1 build-plan step 8 (3b-8): the VM journey, macOS + Linux, as a table plus a runner. Host side. usage:
  python3 -m scripts.vm_harness.journey8 --print-table [--profile darwin|linux]            (no VM, no guest call)
  python3 -m scripts.vm_harness.journey8 <vm> <evidence-dir> --session s-... --repo /path [--profile linux]
                                            [--levain-root /tmp/lvclean] [--step J06 ...]
(run from the repo root, WITHOUT -I: -I leaves the cwd off sys.path and `scripts` is not importable then; ``<vm>`` is a booted ``vend-*`` clone that holds the retained session ``--session`` and a work
tree ``--repo``; the same ssh / ``tart exec`` transport as ``vm_run.py``).

The rule (BUILD_PLAN row 8): "every step's raw line MATCHES its single expected verdict, listed per step and per OS before
the run (no 'or' verdicts)". :data:`STEPS` IS that list: per step and per OS, one cell. A cell is
  * ``vm``      the runner runs ``probe`` in the guest and the one raw line must match ``verdict`` (exit code and regex);
  * ``record``  a record already discharges it (cited; the runner does not re-run it);
  * ``pending`` a named LATER sitting step discharges it (cited; the runner reports it, never counts it);
  * ``na``      the step does not exist on that OS (said why).
A ``vm`` cell with no ``probe`` / ``verdict`` is UNBOUND with the reason written down: the runner reports it UNBOUND and the run
ends ``RESULT: INCOMPLETE``. It is never a pass. ``EXPECTED.txt`` (this table for the chosen OS, with its sha256) is written
to the evidence dir before the first guest call.

WHAT IS BOUND AND WHAT IS NOT, stated plainly: the operator-side verbs (``levain session adopt|discard|restore``, §4.8) print
raw lines the code defines (``adopt_view.store_verb``: ``<verb> <session>: done (<state>)``; ``adopt_view.adopt``: ``took
<which>: <ref> -> <oid>``), so those three are bound. Every other ``vm`` step happens INSIDE a running slot (edit, commit,
fetch, named-root reads, sockets, concurrency, the verifier), and the repo holds no in-slot executor: the compiled fake vendor
(``tests/vm_fakes/fire_fakeclaude.c``) sends a fixed hello, it does not run a command. Those cells stay UNBOUND until an in-slot
probe exists to give them a raw line; inventing one here would be a verdict nobody observed.

Nothing here is run by the tests against a VM: ``tests/test_vm_harness_journey8.py`` drives it with a fake guest.
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence

from scripts.vm_harness.driver import GuestError
from scripts.vm_harness.host_profile import DARWIN, PROFILES, HostProfile, profile_named
from scripts.vm_harness.runbook import (Evidence, RunbookError, Verdict, capture, judge, overall, render_placeholders)

OS_NAMES = ("darwin", "linux")
STATES = ("vm", "record", "pending", "na")
LEVAIN_ROOT = "/tmp/lvclean"      # where the rig stages the levain source (HostProfile.levain_roots[0])


@dataclass(frozen=True)
class Probe:
    """``argv`` words (``{session}`` / ``{repo}`` are filled from the command line; the levain prefix is added by
    :func:`levain_argv`). ``select`` picks the raw line (a regex searched per line; none = the last non-empty line)."""
    argv: tuple[str, ...]
    select: str | None = None
    timeout: float = 300


@dataclass(frozen=True)
class Cell:
    state: str
    verdict: Verdict | None = None
    probe: Probe | None = None
    note: str = ""            # the record, the later step, or the reason this cell is unbound / not applicable


@dataclass(frozen=True)
class Step:
    id: str
    name: str
    cells: Mapping[str, Cell]


def _unbound(why: str) -> Cell:
    return Cell("vm", note="UNBOUND: " + why)


NEEDS_INSLOT = "no in-slot executor in the repo (the fake vendor sends a fixed hello); needs a probe that runs a command inside the slot"
V1 = "V1 sitting step 2 (PLAN_490 section 5)"
ADOPT = Probe(("session", "adopt", "{session}", "--repo", "{repo}", "--take", "head"), select=r"^took ")
DISCARD = Probe(("session", "discard", "{session}"), select=r"^discard ")
RESTORE = Probe(("session", "restore", "{session}"), select=r"^restore ")
V_ADOPT = Verdict(0, r"took head: \S+ -> [0-9a-f]{40,64}")
V_DISCARD = Verdict(0, r"discard <session>: done \([A-Za-z][A-Za-z-]*\)")
V_RESTORE = Verdict(0, r"restore <session>: done \([A-Za-z][A-Za-z-]*\)")


def _both(cell: Cell) -> dict[str, Cell]:
    return {"darwin": cell, "linux": cell}


STEPS: tuple[Step, ...] = (
    Step("J01", "login, passthrough", {
        "darwin": Cell("pending", note=V1 + ": 2a-2b (his signed-in slot; identity step, row 51)"),
        "linux": _unbound("the plan names a macOS V1 only; a Linux vendor sign-in is not scheduled")}),
    Step("J02", "login, brokered", _both(Cell(
        "pending", note="3c-10 (threec10.py): needs a provider credential of his; the brokered half of the login step"))),
    Step("J03", "edit", _both(_unbound(NEEDS_INSLOT))),
    Step("J04", "commit", _both(_unbound(NEEDS_INSLOT))),
    Step("J05", "end, retained", {
        "darwin": Cell("record", note="row 27 (V2, 10 of 10: END RETAINED, slot back to reusable; 1105-36/RETURN_v2vm.md)"),
        "linux": Cell("record", note="row 118 (the Linux L2 rows), as PLAN_490 section 5 attributes it; not re-read at the source")}),
    Step("J06", "adopt", _both(Cell("vm", V_ADOPT, ADOPT, "needs a delta session: an edit and a commit happened (J03, J04)"))),
    Step("J07", "discard", _both(Cell("vm", V_DISCARD, DISCARD))),
    Step("J08", "restore", _both(Cell("vm", V_RESTORE, RESTORE, "restores what J07 discarded"))),
    Step("J09", "the sanitized dispatch artifact", _both(_unbound(NEEDS_INSLOT))),
    Step("J10", "escape to the queue", _both(_unbound(NEEDS_INSLOT))),
    Step("J11", "off-list fetch refused", _both(_unbound(NEEDS_INSLOT))),
    Step("J12", "named roots unreadable", _both(_unbound(NEEDS_INSLOT))),
    Step("J13", "created discovery names recorded touched", _both(_unbound(NEEDS_INSLOT))),
    Step("J14", "concurrent sessions isolated", _both(_unbound(NEEDS_INSLOT))),
    Step("J15", "sandbox-unavailable refuses", {
        "darwin": Cell("na", note="the row says (Linux): bwrap / namespaces are the Linux sandbox"),
        "linux": _unbound("needs a Linux clone with the sandbox made unavailable (a fault the repo has no verb for)")}),
    Step("J16", "each R-D socket on the slot's OS refused", _both(_unbound(NEEDS_INSLOT))),
    Step("J17", "a kill failure leaves the slot leased and doctor red, the tree untouched",
         _both(_unbound("needs a kill-failure fault injected into a live session; the repo has no verb for it"))),
    Step("J18", "broker misuse refused", _both(_unbound(NEEDS_INSLOT))),
    Step("J19", "no-egress verifier refused", _both(_unbound(NEEDS_INSLOT))),
)


def validate(steps: Sequence[Step] = STEPS) -> None:
    """The table's own invariants, checked before any run (and by a test): ids unique; every step has both OSes; a state is
    one of :data:`STATES`; a bound ``vm`` cell has BOTH probe and verdict, one pattern (no alternation); a ``record`` /
    ``pending`` / ``na`` cell says why; an unbound ``vm`` cell says what is missing."""
    seen: set[str] = set()
    for s in steps:
        if s.id in seen:
            raise RunbookError(f"duplicate step id {s.id}")
        seen.add(s.id)
        if set(s.cells) != set(OS_NAMES):
            raise RunbookError(f"{s.id}: one cell per OS ({OS_NAMES}), got {sorted(s.cells)}")
        for os_name, c in s.cells.items():
            where = f"{s.id}/{os_name}"
            if c.state not in STATES:
                raise RunbookError(f"{where}: state {c.state!r}")
            if c.state == "vm":
                if (c.probe is None) != (c.verdict is None):
                    raise RunbookError(f"{where}: a probe and a verdict come together")
                if c.verdict is None and not c.note.startswith("UNBOUND: "):
                    raise RunbookError(f"{where}: an unbound cell says what is missing")
                if c.verdict is not None:
                    c.verdict.check()
            else:
                if c.verdict is not None or c.probe is not None:
                    raise RunbookError(f"{where}: a {c.state} cell runs nothing")
                if not c.note:
                    raise RunbookError(f"{where}: a {c.state} cell cites its record / later step / reason")


def levain_argv(profile: HostProfile, argv: Sequence[str], levain_root: str = LEVAIN_ROOT) -> tuple[str, ...]:
    """A levain verb as the guest's operator runs it: the profile's interpreter over the staged source."""
    return ("/usr/bin/env", f"PYTHONPATH={levain_root}", profile.python, "-m", "levain", *argv)


def verdict_for(cell: Cell, session: str) -> Verdict:
    assert cell.verdict is not None
    return Verdict(cell.verdict.rc, cell.verdict.pattern.replace("<session>", re.escape(session)))


def expected_text(os_name: str, steps: Sequence[Step] = STEPS) -> str:
    """The per-step, per-OS expectations as written down BEFORE the run (this is EXPECTED.txt's body)."""
    lines = [f"3b-8 journey: expected verdicts, os={os_name}", "step | name | state | expected | probe / note"]
    for s in steps:
        c = s.cells[os_name]
        if c.state == "vm" and c.verdict is not None:
            probe = "levain " + " ".join(c.probe.argv)               # type: ignore[union-attr]
            lines.append(f"{s.id} | {s.name} | vm | rc={c.verdict.rc} /{c.verdict.pattern}/ | {probe}")
        elif c.state == "vm":
            lines.append(f"{s.id} | {s.name} | UNBOUND | - | {c.note}")
        else:
            lines.append(f"{s.id} | {s.name} | {c.state} | - | {c.note}")
    return "\n".join(lines)


@dataclass
class StepResult:
    id: str
    name: str
    status: str               # PASS | FAIL | UNBOUND | SKIP | NA | ERROR (timed out / no verdict observed)
    raw: str = ""
    expected: str = ""
    detail: str = ""

    def line(self) -> str:
        tail = f" raw={self.raw!r} expected={self.expected}" if self.status in ("PASS", "FAIL") else ""
        return f"{self.id} {self.name}: {self.status}{tail}" + (f" ({self.detail})" if self.detail else "")


@dataclass
class JourneyResult:
    status: str
    steps: list[StepResult]
    result_path: Path

    @property
    def ok(self) -> bool:
        return self.status == "PASS"


def run_journey(guest, profile: HostProfile, evidence_dir, *, session: str = "", repo: str = "",
                levain_root: str = LEVAIN_ROOT, only: Sequence[str] = (), steps: Sequence[Step] = STEPS) -> JourneyResult:
    """Run every bound ``vm`` cell of ``profile``'s OS through ``guest``; report the rest. Refuses (before any guest call)
    on a bad table, an unknown ``only`` id, a missing placeholder, or an evidence dir that is not fresh."""
    validate(steps)
    os_name = profile.name
    ids = {s.id for s in steps}
    bad = [i for i in only if i not in ids]
    if bad:
        raise RunbookError(f"unknown step id(s) {bad}")
    chosen = [s for s in steps if not only or s.id in only]
    ctx = {"session": session, "repo": repo}
    plan: dict[str, tuple[str, ...]] = {}
    for s in chosen:                                           # render every bound probe first: a missing arg fails now
        c = s.cells[os_name]
        if c.state == "vm" and c.probe is not None:
            plan[s.id] = levain_argv(profile, render_placeholders(c.probe.argv, **ctx), levain_root)
    ev = Evidence(evidence_dir)
    digest = ev.write_expected(expected_text(os_name, steps))
    results: list[StepResult] = []
    for s in chosen:
        c = s.cells[os_name]
        if c.state == "na":
            r = StepResult(s.id, s.name, "NA", detail=c.note)
        elif c.state in ("record", "pending"):
            r = StepResult(s.id, s.name, "SKIP", detail=f"{c.state}: {c.note}")
        elif c.probe is None:
            r = StepResult(s.id, s.name, "UNBOUND", detail=c.note.removeprefix("UNBOUND: "))
        else:
            v = verdict_for(c, session)
            try:
                lines, rc = capture(guest, plan[s.id], c.probe.timeout)
                ok, raw = judge(lines, rc, v, c.probe.select)
                r = StepResult(s.id, s.name, "PASS" if ok else "FAIL", raw=raw, expected=f"rc={v.rc} /{v.pattern}/",
                               detail="" if ok else f"rc={rc}")
            except (GuestError, RunbookError, OSError) as e:
                r = StepResult(s.id, s.name, "FAIL", expected=f"rc={v.rc} /{v.pattern}/", detail=f"transport: {e}")
            except subprocess.SubprocessError as e:       # TimeoutExpired included: no verdict was observed, so not a FAIL
                r = StepResult(s.id, s.name, "ERROR", expected=f"rc={v.rc} /{v.pattern}/",
                               detail=f"{type(e).__name__}: {e}")
        results.append(r)
        ev.log(r.line())
    omitted = [s.id for s in steps if s not in chosen]
    status = overall([r.status for r in results], omitted)
    body = [f"RESULT: {status}", f"journey 3b-8, os={os_name}, expected-table sha256 {digest} (EXPECTED.txt, written before the first step)"]
    if omitted:
        body.append("PARTIAL: a --step subset ran; omitted: " + ", ".join(omitted))
    body += [r.line() for r in results]
    counts = {k: sum(r.status == k for r in results) for k in ("PASS", "FAIL", "UNBOUND", "ERROR", "SKIP", "NA")}
    body.append("counts: " + " ".join(f"{k}={v}" for k, v in counts.items()))
    path = ev.write_result("\n".join(body) + "\n")
    return JourneyResult(status, results, path)


def main(argv: Sequence[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("vm", nargs="?")
    ap.add_argument("evidence", nargs="?")
    ap.add_argument("--profile", default=DARWIN.name, choices=sorted(PROFILES))
    ap.add_argument("--session", default="")
    ap.add_argument("--repo", default="")
    ap.add_argument("--levain-root", default=LEVAIN_ROOT)
    ap.add_argument("--step", action="append", default=[], help="run only this step id (repeatable)")
    ap.add_argument("--print-table", action="store_true", help="print the expected-verdict table for --profile and exit")
    a = ap.parse_args(argv)
    profile = profile_named(a.profile)
    validate()
    if a.print_table:
        print(expected_text(profile.name))
        return 0
    if not a.vm or not a.evidence:
        ap.error("vm and evidence-dir are required unless --print-table")
    from scripts.vm_harness import vm_run
    from scripts.vm_harness.host_profile import ProfileMismatch
    from scripts.vm_harness.driver import AdapterError, VmAdapters

    vm_run._clone_guard(a.vm)
    with tempfile.TemporaryDirectory() as td:
        guest, spawn = vm_run.make_transport(a.vm, profile, Path(td))
        try:
            VmAdapters(guest, spawn, profile=profile).verify_host()      # refuses a profile / guest OS mismatch
        except (ProfileMismatch, AdapterError) as e:
            print(f"journey8: refusing: {e}", file=sys.stderr)
            return 2
        try:
            res = run_journey(guest, profile, a.evidence, session=a.session, repo=a.repo, levain_root=a.levain_root,
                              only=a.step)
        except RunbookError as e:
            print(f"journey8: refusing: {e}", file=sys.stderr)
            return 2
    print(res.result_path.read_text())
    return {"PASS": 0, "FAIL": 1}.get(res.status, 3)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

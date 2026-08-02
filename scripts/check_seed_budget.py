#!/usr/bin/env python3
"""
Structural guard on the EAGER context surface every fresh Levain install inherits.

WHY THIS EXISTS — it is the half of the fix that a trim does not deliver.

On 2026-07-30 the first installer outside this machine, Alex De Groodt, reported:
*"I'm wondering if levain isn't filling too much context actually."* He was right,
by feel, with no instrumentation: 48,451 bytes `@import`ed into EVERY session, 65%
of it documentation about the machinery versus 17% who the operator and entity
actually are. It took an outside installer to see it, because a harness author
measures the carrier they READ and never the one the install LOADS.

flow hit this same class in its own carriers and learned that trimming does not
hold: six hand-trims in two months with the interval SHRINKING (13 days, then 12,
then 5), because each trim reset the size and left the growth rate alone. What
fixed it was a CONTAINER change plus `scripts/check_carrier_size.py` — a guard that
refuses the commit. Levain inherited the container change on 2026-08-01
(`ON_DEMAND_SEED`) and did NOT inherit the guard. This is the guard.

`structural_invariants_beat_discipline`: without it the surface regrows and the
next outside installer finds it the same way, by feel.

WHAT IT MEASURES — the BASE install, i.e. what an operator gets before they have
written a word: the shipped seed files that load eagerly, plus the adapter carrier.
Pack layers and the operator's own rendered answers are deliberately excluded; a
long `world.md` is the system WORKING, and this guard must never create pressure
against a rich operator profile.

TWO BUDGETS, and the ratio is the load-bearing one:

  MECHANISM SHARE   the fraction of the eager surface that is documentation ABOUT
                    the substrate (`memory.md`, `spore_instructions.md`) rather
                    than identity or method. THIS is the defect Alex found. A big
                    surface that is mostly identity is healthy; a big surface that
                    is mostly machinery is the bug.

  TOTAL BYTES       a backstop, so the composition cannot grow without bound even
                    while staying proportional.

⚠ THE MECHANISM BUDGET IS SET ABOVE WHERE THE BASE SITS TODAY, ON PURPOSE. F2 shipped
its FIRST CUT only (`spore_instructions.md`), and `memory.md` at 21,347 B is the bigger
half and still loads eagerly. Setting the budget at today's number would freeze the
defect in place and call it compliance. It is set where it is to stop REGRESSION now,
and it should be RATCHETED DOWN when the `memory.md` half lands (`spore-451`).

⚠ THE CURRENT SHARE IS NOT RESTATED HERE — RUN THE SCRIPT (0.4.1). This paragraph used
to say "~60% mechanism as of 2026-08-01" while `measure()` five lines down printed 55%,
`packs.py` said ~57%, and `doctor` on a rendered install said 59%: FOUR numbers for ONE
quantity, all introduced in one commit set. The argument the paragraph is making —
that the 0.62 limit sits just above today's number — was also wrong in the direction
that matters: at 55% the headroom is 7 points, not 2. A prose copy of a measured value
is a description that rots against correct code, so there is now exactly one authority
and it is `measure()`. Run `python3 scripts/check_seed_budget.py` for today's figure.

WHEN THIS FIRES, DO NOT JUST SHAVE WORDS. The durable fix is the container move:
add the file to `levain.packs.ON_DEMAND_SEED` with a RETENTION SUMMARY in
`ON_DEMAND_SUMMARY` — the part the entity must hold without opening the file — so
it is reached by a pointer instead of a load. A bare filename pointer is not
enough; it silently kills the layer it points at.

Usage:  python3 scripts/check_seed_budget.py [--verbose]
Exit 1 on breach. Wire into CI alongside the test suite.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from levain.packs import (  # noqa: E402
    BASE_IMPORT_ORDER,
    MECHANISM_SHARE_WARN,
    seed_role,
)

# The ceiling for the eager MECHANISM share of a base install. Above today's ~60%
# so this catches regression rather than blessing the status quo; ratchet toward
# MECHANISM_SHARE_WARN (0.50) once memory.md moves.
MECHANISM_SHARE_LIMIT = 0.62

# Backstop on the raw eager surface of a base install, in bytes.
TOTAL_BYTES_LIMIT = 40_000


def measure() -> tuple[int, dict[str, int], list[tuple[str, int, str]]]:
    """(total_bytes, bytes_by_role, [(name, size, role)]) for the BASE eager set."""
    seed_root = REPO / "levain" / "templates" / "seed"
    rows: list[tuple[str, int, str]] = []
    by_role: dict[str, int] = {}
    total = 0
    for name in BASE_IMPORT_ORDER:
        f = seed_root / name
        if not f.is_file():
            # A base seed missing from the tree is the install honesty floor's job
            # to fail loudly; this guard measures what is there.
            continue
        size = f.stat().st_size
        role = seed_role(name)
        rows.append((name, size, role))
        by_role[role] = by_role.get(role, 0) + size
        total += size

    # The carrier itself loads too. Measure the TEMPLATE, minus its placeholders —
    # the rendered block sizes vary per install, and this guard is about the base
    # composition, not any one render.
    carrier = REPO / "levain" / "templates" / "adapters" / "claude-code" / "CLAUDE.md.template"
    if carrier.is_file():
        total += len(carrier.read_text(encoding="utf-8").encode("utf-8"))
    return total, by_role, rows


def main(argv: list[str]) -> int:
    verbose = "--verbose" in argv or "-v" in argv
    total, by_role, rows = measure()
    mechanism = by_role.get("mechanism", 0)
    share = mechanism / total if total else 0.0

    if verbose:
        print("Base eager context surface (what every fresh install loads):")
        for name, size, role in sorted(rows, key=lambda r: -r[1]):
            print(f"  {name:26} {size:>7,} B  {role}")
        print(f"  {'TOTAL (incl. carrier)':26} {total:>7,} B")
        print()

    failed = False
    if share > MECHANISM_SHARE_LIMIT:
        print(
            f"FAIL: eager surface is {share:.0%} mechanism docs "
            f"(limit {MECHANISM_SHARE_LIMIT:.0%}). "
            f"mechanism={mechanism:,}B of {total:,}B."
        )
        print(
            "  Fix by MOVING a file, not by trimming words: add it to "
            "levain.packs.ON_DEMAND_SEED with a retention summary in "
            "ON_DEMAND_SUMMARY. A bare pointer kills the layer it points at."
        )
        failed = True
    if total > TOTAL_BYTES_LIMIT:
        print(f"FAIL: eager surface is {total:,} B (limit {TOTAL_BYTES_LIMIT:,} B).")
        failed = True

    if failed:
        return 1
    print(
        f"check_seed_budget: clean — {total:,} B eager, {share:.0%} mechanism "
        f"(limits {TOTAL_BYTES_LIMIT:,} B / {MECHANISM_SHARE_LIMIT:.0%}; "
        f"target {MECHANISM_SHARE_WARN:.0%} once memory.md moves)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

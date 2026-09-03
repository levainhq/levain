#!/usr/bin/env bash
# ⛔⛔⛔ THE LANE GUARD — a lane may READ anything and WRITE only what it owns.
#
# ## Why this is a script and not a rule
#
# `spore-504` set the parallel protocol as a discipline: lanes coordinate, assumption changes stop
# and route through the ruling seat. **The protocol is right and discipline is the wrong substrate
# for it.** On 2026-08-11 this project reintroduced a `grep`-under-`pipefail` bug into a new harness
# **hours after fixing the identical bug and writing a commit message naming the class** — with the
# reason written at the site of the previous fix. Naming a class does not prevent its next instance.
# `structural_invariants_beat_discipline`.
#
# ## What it actually prevents
#
# The 2026-08-10 wisp failure at n=2: one lane retired a mechanic, another kept building on it, and
# a document enforced a dead rule for 16 days. At n=5 in one codebase that stops being an incident
# and becomes the default. **The cheapest possible detection is "you just edited a file you do not
# own"**, and it fires before the commit rather than after the merge.
#
# ## ⛔ WHAT IT DOES NOT DO, so a green run is not read as more than it is
#
#   · It does not check that the CHANGE is correct, only that it is in bounds. A lane can be
#     catastrophically wrong inside its own files and this passes.
#   · It cannot see ASSUMPTION coupling — a lane that reads another lane's design and builds on a
#     premise about to be retired touches none of its files and passes clean. **That is the
#     08-10 failure and it remains a protocol problem, not a path problem.** The freeze rule and the
#     broadcast-AND-write rule are what cover it; this covers only the file half.
#   · It says nothing about the shared APPEND surfaces (`debts.md`, `testing.md`, the archive), which
#     are deliberately unowned — a hard lock there would stop a lane recording what it learned.
#
# ## ⚠ THE AUDIT WAS BLIND TO project_memory UNTIL 2026-08-12, AND REPORTED GREEN OVER IT
#
# §1 enumerated only `Wisp/* tools/* project.yml CLAUDE.md`, so it printed *"every tracked
# source/harness file has exactly one owner"* — a TRUE sentence — while **36 of 51 tracked
# `project_memory/` files were owned by no lane and absent from `SHARED_APPEND`**, the spine among
# them. Run-verified: appending one line to `project_memory/the_walk.md` made `verify_lane.sh
# levain-walk` exit 1, and `--audit` stayed green. The map's own auditor structurally could not see a
# hole in the map, and it would have fired on the first commit of launch day, since `next_steps.md`
# tells every lane to run this before every commit and recording what you learned in a design doc is
# this project's documented practice. `absence_of_signal_rendered_as_health` — the guard was correct
# and the surface it read did not exist. **§1 now scans EVERY tracked file.**
#
# Usage:
#   tools/verify_lane.sh levain-walk        # check the working tree against a lane's ownership
#   tools/verify_lane.sh --list           # print the lane roster
#   tools/verify_lane.sh --audit          # every owned path exists, and no file has two owners

set -uo pipefail

# ⛔⛔⛔ `set -f` — NOGLOB — AND IT IS THE FIX FOR A DEFECT THAT MADE THIS ENTIRE GUARD LIE.
#
# ▶ **WHAT WAS BROKEN, MEASURED 2026-08-22 IN A DETACHED WORKTREE.** Every ownership test in this
# file is `for g in $(globs_for "$l")` or `for g in $OWNED` — **UNQUOTED**, so the shell performed
# PATHNAME EXPANSION on the map's globs before the loop ever ran. `Wisp/Resources/boss_*` was
# expanded into the list of boss files THAT CURRENTLY EXIST, and the loop then compared a path
# against filenames instead of against a pattern.
# ⚡ **SO THE OWNERSHIP CHECK WAS NEVER GLOB-MATCHING. It was matching against the state of the disk.**
# `bash -x` trace, verbatim: `[[ Wisp/Resources/boss_ism.mp3 == tools/beatmaps/screen/lo_fi_spy___bridge_eb33ca03.json ]]`
# — a glob row from the map arriving at the comparison as one expanded filename.
#
# ⛔ **TWO CONSEQUENCES, AND THE SECOND ONE HAS BEEN MISREAD AS PHYSICS FOR TEN DAYS:**
#   1. **A LANE COULD NOT DELETE A FILE IT OWNS.** The deleted path matches no existing filename, so
#      it was refused as *"unowned — ask the seat for a grant; this is a hole in LANES.tsv"* — a
#      FALSE diagnosis that sent the lane to the seat to fix a map that was already correct. Proven
#      with a control: the same path, same lane, same map grades `1 owned` with the file present and
#      `1 out of bounds` with it deleted. Found by `levain-boss` while executing Phill's item-10 ruling.
#   2. ⛔⛔ **THE "CREATE-THEN-GRANT WINDOW" IS NOT STRUCTURAL. IT IS THIS BUG.** `LANES.tsv` and
#      `FLOW_DEV_PROTOCOL.md` both describe a NEW file necessarily reading as unowned as a property
#      of enumerating tracked files. Measured: a brand-new path covered by an existing glob grades
#      `owned=0` under the unquoted loop and `owned=1` without pathname expansion. Every "ask for a
#      grant before your first commit" ceremony for a file ALREADY COVERED BY A GLOB was unnecessary.
#      ⚠ The seat recorded one of those refusals THIS MORNING as *"the check working exactly as
#      designed."* It was not. It was this.
#
# ▶ **WHY `set -f` AND NOT FIVE QUOTED LOOPS.** There are five such loops today and the sixth is one
# edit away; a fix that must be remembered at each new loop is the discipline this file exists to
# replace. `structural_invariants_beat_discipline`. ⚠ `set -f` disables PATHNAME expansion only —
# `[[ "$f" == $g ]]` pattern matching is a different mechanism and is UNAFFECTED, which is the whole
# reason this works: the globs reach the comparison intact and are matched as patterns, once.
# ⚠ **AND THE COMMAND ITSELF IS ON THE NEXT LINE, WHICH IS NOT AS OBVIOUS AS IT SOUNDS.** The first
# version of this fix was THIS COMMENT AND NOTHING ELSE — the reasoning shipped, the `set -f` did
# not, and a three-case mutation test read 2-of-3 correct because one case passed for an unrelated
# reason. A fix that is a description is the defect class this whole day has been about.
set -f
# ⛔⛔ **THE SENTINEL PRINTS ON AN HONEST RED TOO — ⚖ Phill, 2026-08-21.** `verify_all.sh` glossed a
# non-zero exit as *"the harness said no"*, which an ABORTED harness also produces without having
# said anything. The sentinel now discriminates at every exit status, not only at zero.
# ⚠ **NO `ERR` TRAP HERE, DELIBERATELY, AND THE REASON IS THIS FILE'S OWN `set -uo pipefail`:**
# it drops `-e` on purpose so a failed check reaches the verdict block, which means bare non-zero
# commands are ROUTINE here. An ERR trap would read those as a death and suppress the sentinel on
# a perfectly honest red — the exact mislabel this change exists to remove, inverted.
trap 'echo "HARNESS-END verify_lane.sh"' EXIT

cd "$(dirname "$0")/.."

MAP="LANES.tsv"
[[ -f "$MAP" ]] || { echo "⛔ no $MAP — the ownership map is the whole mechanism"; exit 1; }

# ⛔⛔⛔ WHO AM I — AND WHY IT IS A FILE AND NOT AN ENVIRONMENT VARIABLE.
#
# This shipped as `export LEVAIN_LANE=<lane>` on 2026-08-14 and that was WRONG FOR THE ONLY CALLER
# THAT MATTERS. An agent lane's every Bash call is a FRESH SHELL — measured, not assumed: `export
# LEVAIN_LANE=levain-walk` in one call, `${LEVAIN_LANE:-<UNSET>}` in the next, prints `<UNSET>`. So the
# spill check (check 3 in --staged) would have been silently DEAD in real use, firing only when a
# caller happened to chain `export … && git commit` into a single command — which is exactly and
# only how it was TESTED. **The check was verified; the way it is reached was not.**
# `wiring_checked_content_unchecked_passes_green`, aimed at a guard written that morning.
#
# ▶ THE LANE LIVES ON DISK, in `.levain-lane` (gitignored, one word). It survives across shells
#   because it is a file, which is the whole point.
#       echo levain-walk > .levain-lane        # once, at the start of a lane session
#   `LEVAIN_LANE` is still honoured and OVERRIDES the file — useful for a one-off command and for the
#   seat stepping into a lane's shoes deliberately.
# ⛔⛔⛔ AND THE FILE IS A SINGLE GLOBAL SLOT FOR A PER-SESSION FACT — BROKEN AT N>1 LANES,
# MEASURED 2026-08-17, and the block above is correct about the shell and wrong about the world.
# Trading a per-PROCESS slot for a per-REPO one is a fix only while exactly one lane exists. Every
# lane shares this worktree and each writes its own name here at session start, so LAST WRITER WINS
# and every other live lane is then graded against a stranger's row. Reported UP by `levain-world`
# after the seat clobbered it twice with `echo levain-seat > .levain-lane` before its own commits.
# ⛔ BOTH DIRECTIONS REPRODUCED on a throwaway worktree at 223045d, and only ONE of them is loud:
#   · A lane committing ITS OWN file while the slot names someone else → REFUSED. Loud, survivable,
#     costs a cycle. This is what was noticed.
#   · A lane committing a file owned by WHOEVER THE SLOT NAMES → **PASSES, printing `ok`.** With the
#     slot reading `levain-seat`, a bare `git commit -- project_memory/decisions.md` from any lane was
#     accepted. **That is the design-layer freeze — the load-bearing rule of the whole parallel
#     protocol — silently disabled inside the guard that exists to enforce it.**
# ▶ THE FIX IS THAT IDENTITY RIDES THE COMMAND, NOT THE DISK: `--commit` now exports `LEVAIN_LANE`
#   from the lane NAMED ON ITS OWN COMMAND LINE into the `git commit` child, so the sanctioned path
#   cannot be mis-graded no matter what the slot says. See the `git commit` call at the foot.
# ⚠ The file SURVIVES as a fallback for a hand-typed bare `git commit` — deleting it would cost the
#   single-lane case its spill check — but it is now ADVISORY and it SAYS SO when it is what got
#   used. A gate that discloses when it is guessing cannot render absence as coverage; a silent one
#   always will. ⛔ THE SEAT NO LONGER WRITES THIS FILE AT ALL.
LANE_ID="${LEVAIN_LANE:-}"
LANE_SRC="env"
if [[ -z "$LANE_ID" && -f .levain-lane ]]; then
    LANE_ID="$(tr -d '[:space:]' < .levain-lane)"
    LANE_SRC="file"
fi

# ⛔⛔ SELF-INSTALL THE HOOK, because "run `install_hooks.sh` once per clone" is a MEMORY ITEM and
# this file exists to replace those. `core.hooksPath` is local config git cannot ship, so a fresh
# clone — or a `git worktree add` you then commit from — is UNGATED until something sets it. Every
# lane runs this script constantly and `verify_all.sh` runs it too, so the first invocation in any
# clone installs the gate and nobody has to remember.
# ⚠ ONLY WHEN UNSET. If someone has deliberately pointed `core.hooksPath` elsewhere, clobbering it
# would be this script deciding it outranks the operator — announce and leave it alone.
if [[ -z "$(git config --get core.hooksPath 2>/dev/null || true)" ]] && [[ -d tools/hooks ]]; then
    git config core.hooksPath tools/hooks 2>/dev/null && \
        echo "  ⚡ installed the lane pre-commit gate (core.hooksPath -> tools/hooks) — first run in this clone"
fi

# ⛔⛔ **WHO THE INTEGRATOR IS. Declared HERE, above every consumer, on purpose — 2026-08-27.**
# The ownership test exists at TWO sites in this file (the pre-commit hook and GATE mode) and this
# name is read by both. A first pass declared it beside the GATE consumer only, ~300 lines below the
# pre-commit path, which `exit`s long before that line is ever reached — so the hook still refused
# with an unbound name in scope. One rule, two enforcement sites, and only one of them fixed: the
# same shape as the defect this whole change is about.
SEAT_LANE="levain-seat"

# Shared append surfaces: unowned by design. See the note at the foot of LANES.tsv.
# ⛔ EVERY ENTRY HERE IS AN *OBSERVATION* SURFACE. Under parallel-hands/serial-head (2026-08-12) a
# lane may record what it saw and may never record what it decided — the seat writes every ruling.
# Adding a file here is therefore a real decision: ask whether a lane could use it to change what
# another lane is building toward. If yes, it belongs to the seat.
SHARED_APPEND=(
  # ⚡ THE LANE→SEAT CHANNEL, and the file that makes the design-layer lock survivable. A lane that
  # learns something about the design appends a timestamped block here instead of editing the doc.
  # Append-only means it cannot merge-conflict and cannot lose content; the seat integrates at fan-in.
  "project_memory/lane_log.md"
  "project_memory/COMPLETED_SESSIONS_ARCHIVE.md"
  "project_memory/debts.md"
  # ⛔ `testing.md` LEFT THIS LIST 2026-08-12 — the SECOND `retired_premises.md`, found the same day
  # and by the same argument. A STANDING CHECK IS A RULING: it governs what every lane may build,
  # and five sites of one dead premise had already survived three sweeps inside it, one of them
  # actively forbidding the screen work two lanes now owe. A lane could have amended any of them
  # with this guard silent. The original rationale — *a lane must be able to record a harness blind
  # spot it discovered* — is real and is now served by `lane_log.md`, which is where an OBSERVATION
  # belongs anyway. Lanes log blind spots; the seat writes checks.
  # ⚡ THE DEVICE QUEUE IS DELIBERATELY SHARED. Every lane must be able to add a row the moment it
  # discovers it needs a device answer — a lock here would make lanes either block on the seat or,
  # far worse, keep the question in their head, which is the invisible-queue failure the file exists
  # to end.
  "project_memory/device_queue.md"
  # ⛔ `retired_premises.md` WAS HERE UNTIL 2026-08-12 and is now seat-owned. A retirement is a
  # RULING, not an observation — this was the one append surface on which a lane could have killed
  # a premise another lane was actively building on, silently and in bounds.
)

lanes()      { grep -v '^#' "$MAP" | grep -v '^[[:space:]]*$' | cut -f1; }
globs_for()  { grep -v '^#' "$MAP" | awk -F'\t' -v L="$1" '$1==L {print $2}'; }

# ⛔⛔⛔ --staged — THE PRE-COMMIT GATE, AND THE ONLY CHECK IN THIS FILE THAT FIRES IN TIME.
#
# ## The hole it closes, which this repo ruled unfixable and which was not
#
# `LANES.tsv` (the ChartStore block) ruled: *"the check that catches this structurally cannot run
# before the action that causes it… the guard cannot fire in time."* That is TRUE of §1 — it
# enumerates TRACKED files, and a new file is untracked right up until the commit that creates it,
# so the audit is GREEN when you run it and RED the moment you act. Measured: `verify_all.sh` at
# 31/31 with `ChartStore.swift` untracked, then `17677cf`, and the suite went 30/31 — unnoticed
# until `levain-instrument` reported it as not its own red.
#
# It is FALSE of git. A pre-commit hook runs with `GIT_INDEX_FILE` pointing at the index for THIS
# commit — including a path-scoped `git commit -- <paths>` and `git add -N` entries — so it sees
# exactly the set about to land, before it lands. What used to be *"remember to ask for a grant
# before your first commit"* is now a refusal. `structural_invariants_beat_discipline`.
#
# ## What it checks, and it needs NO lane argument for the first two
#
#   1. every path in this commit is owned by SOME lane, or is shared-append  → the new-file/orphan
#      hole, caught at the only moment it is catchable
#   2. no path is owned by TWO lanes                                         → map collision
#   3. IF the lane is known (`.levain-lane` file, or `LEVAIN_LANE`), every path belongs to THAT lane
#
# ⚠ 3 IS OPT-IN ON PURPOSE. The hook cannot know which lane you are, and INFERRING it would be a
# guess in the one place a guess is fatal. With the lane unknown you still get 1 and 2, which are
# the checks that fire in time and that no other mode in this file can run at all. Write
# `.levain-lane` once at the start of a lane session and the spill check rides every commit for free.
#
# Usage:  tools/verify_lane.sh --staged        (normally invoked by tools/hooks/pre-commit)
if [[ "${1:-}" == "--staged" ]]; then
    # ⛔⛔ **`D` IS INCLUDED — ADDED 2026-08-22 — AND ITS ABSENCE WAS THE MOST DESTRUCTIVE HOLE IN
    # THIS FILE.** This read `--diff-filter=ACMR` with a documented rationale: *"a DELETION cannot
    # spill content into another lane's file, and blocking one would stop the seat retiring a path it
    # owns."*
    # ⚡ **BOTH HALVES OF THAT REASONING WERE WRONG, AND THE FIRST IS THE DANGEROUS ONE.** This guard
    # adjudicates OWNERSHIP, not spillage. Deleting a file you do not own is the most destructive
    # cross-lane act available, and it was the one act the gate could not see: a staged deletion
    # matched no filter, `staged` came back EMPTY, and the hook printed **"nothing to grade"** and
    # exited 0. **PROVEN, not read off the filter string** — in a detached worktree, `levain-boss`
    # staged the deletion of `Wisp/Chart.swift` (levain-seat's) and the gate reported nothing to grade.
    # ⚠ `levain-boss` found this by READING and correctly refused to test it: staging another lane's
    # deletion in a SHARED tree is the one experiment that cannot be run safely. It ran here because
    # a detached worktree makes it safe, which is what that tool is for.
    # ▶ The second half is answered by grading rather than by skipping: an owned path being retired
    # now grades as OWNED and passes, because the glob fix above lets a deleted path match its own
    # glob. Only an unowned or another lane's deletion is refused — which is the point.
    staged=$(git diff --cached --name-only --diff-filter=ACMRD)
    if [[ -z "$staged" ]]; then
        echo "verify_lane --staged: nothing to grade"
        exit 0
    fi
    sfail=0; sok=0; sshared=0; sseat=0
    while IFS= read -r f; do
        [[ -n "$f" ]] || continue
        for s in "${SHARED_APPEND[@]}"; do
            [[ "$f" == "$s" ]] && { sshared=$((sshared+1)); continue 2; }
        done
        # ⚠ counter + string rather than an array: macOS ships bash 3.2, where "${arr[@]}" on an
        # EMPTY array is an unbound-variable error under `set -u`. §1 uses this shape for the same
        # reason — do not "modernise" it.
        owners=0; ownerlane=""
        while read -r l; do
            for g in $(globs_for "$l"); do
                # shellcheck disable=SC2053
                [[ "$f" == $g ]] && { owners=$((owners+1)); ownerlane="${ownerlane:+$ownerlane }$l"; break; }
            done
        done < <(lanes)
        # ⛔⛔ **THE SEAT PASSES, AND IT IS TESTED FIRST BECAUSE IT MUST COVER ALL THREE REFUSAL
        # SHAPES.** This is the second copy of a rule whose first copy lives in GATE mode at the
        # `$LANE == $SEAT_LANE` carve-out, and being a second copy is the whole hazard: the gate-mode
        # rule was fixed on 2026-08-27 and this hook still refused, so the fix was half-changed within
        # one commit. ⛔ **IT WAS THEN HALF-CHANGED AGAIN, AND THAT IS WHAT THIS BLOCK REPAIRS
        # (2026-08-28, Diogenes HIGH, run-verified):** the seat test had been added as the THIRD elif,
        # guarded by `"$ownerlane" != "$LANE_ID"`, so it was unreachable when `owners -eq 0` (a brand
        # new file) or `owners -gt 1` (a map collision) — and `verify_lane.sh levain-seat --commit
        # <new file>` printed ALL LANE CHECKS PASSED and then refused, telling the seat to ask itself
        # for a grant and wait for itself. That deadlock is the exact thing the ruling was written to
        # end, reproduced on the command CLAUDE.md tells every lane to use.
        # ⚡ **ONE PREDICATE, TESTED BEFORE THE CLASSIFICATION BRANCHES, mirroring gate mode's
        # position** — where owned-by-self and shared-append have already `continue`d out, so the
        # carve-out needs no ownership qualifier at all. `structural_invariants_beat_discipline`:
        # three branches guarded by three copies of a predicate is a rule that can be half-changed a
        # third time; one predicate above them cannot.
        seatcarve=0
        if [[ -n "$LANE_ID" && "$LANE_ID" == "$SEAT_LANE" ]] \
           && ! [[ $owners -eq 1 && "$ownerlane" == "$LANE_ID" ]]; then
            seatcarve=1
        fi
        if [[ $seatcarve -eq 1 ]]; then
            echo "  ⚡ $f"
            if [[ $owners -eq 0 ]]; then
                echo "       owned by NO lane — SEAT OVERRIDE (you are $LANE_ID: you grant the rows)."
                echo "       ▶ Give it a row in LANES.tsv. The commit proceeds either way."
            elif [[ $owners -gt 1 ]]; then
                echo "       owned by $owners lanes ($ownerlane) — SEAT OVERRIDE on a map collision."
                echo "       ▶ The SEAT fixes LANES.tsv. A grant is TWO row edits, and neither is the comment."
            else
                echo "       owned by $ownerlane — SEAT OVERRIDE (you are $LANE_ID: you grant the rows)."
            fi
            echo "       ⚖ Phill 2026-08-27: lanes ENABLE optional parallel work; they are not prisons."
            echo "       ⚠ If a parallel run is live, check the lane is not mid-edit on this file."
            sseat=$((sseat+1))
        elif [[ $owners -eq 0 ]]; then
            echo "  ⛔ $f"
            echo "       owned by NO lane — you are at the far edge of the create→grant window."
            echo "       ▶ Ask the SEAT for a grant, let it land in LANES.tsv, then commit."
            sfail=$((sfail+1))
        elif [[ $owners -gt 1 ]]; then
            echo "  ⛔ $f"
            echo "       owned by $owners lanes ($ownerlane) — a map collision, not your mistake."
            echo "       ▶ The SEAT fixes LANES.tsv. A grant is TWO row edits, and neither is the comment."
            sfail=$((sfail+1))
        elif [[ -n "$LANE_ID" && "$ownerlane" != "$LANE_ID" ]]; then
            echo "  ⛔ $f"
            echo "       owned by $ownerlane, and you are $LANE_ID."
            echo "       ▶ STOP. Message the owning lane or the seat and WAIT. One message beats a bad merge."
            sfail=$((sfail+1))
        else
            sok=$((sok+1))
        fi
    done <<< "$staged"

    if [[ $sfail -gt 0 ]]; then
        echo ""
        echo "⛔ PRE-COMMIT REFUSED — $sfail path(s) out of bounds. NOTHING was committed."
        # ⚠ PRINTED UNCONDITIONALLY, LIKE $sok AND $sshared BESIDE IT. It read
        # `${sseat:+ · $sseat seat-override}`, which tests for EMPTY and not for zero — and `sseat`
        # is initialised to the STRING `0`, so the field always printed and always said 0. It was
        # copied from `${LANE_ID:+…}` two fields to its left, where the guard is correct because
        # LANE_ID is genuinely unset outside a lane. Consistency with its neighbours is cheaper than
        # a correct conditional (Diogenes LOW, 2026-08-28).
        echo "   ${LANE_ID:+lane=$LANE_ID · }$sok owned ok · $sshared shared-append · $sseat seat-override"
        echo "   Override is --no-verify. You had better be the seat, and you had better be right."
        exit 1
    fi
    # ⚠ SAY IT WHEN THE LANE IS UNKNOWN. A silent pass here reads as "all three checks green" when
    # only two ran, which is `absence_of_signal_rendered_as_health` on the guard's own report line.
    # ⛔ THE SEAT-OVERRIDE COUNT RIDES BOTH SUMMARY LINES, AND IT USED TO RIDE ONLY THE FAIL ONE.
    # Measured 2026-09-03: a seat commit over `levain-linux`'s file printed the per-file
    # `⚡ SEAT OVERRIDE` block correctly and then closed with `ok — 0 owned · 0 shared-append` —
    # a summary accounting for NONE of the files in the commit. The per-file block is the loud
    # half; this line is the half a CI log, a `tail`, or a scrolling operator actually reads, and
    # it under-reported in the direction that looks clean. `⚖ named loudly and never fatal` needs
    # both halves: we had never-fatal without the naming that pays for it.
    # ⚡ SIBLING-FIX CLASS, and the evidence is three lines up: the FAIL line at the top of this
    # block already carries `$sseat seat-override`, added when someone hit this once and corrected
    # exactly one of the two sites. A correction that landed where it was written and not at the
    # twin its own reason reaches.
    if [[ -z "$LANE_ID" ]]; then
        echo "verify_lane --staged: ok — $sok owned · $sshared shared-append · $sseat seat-override"
        echo "   ⚠ LANE UNKNOWN — the spill check did NOT run (only 2 of 3). \`echo <lane> > .levain-lane\`"
    else
        echo "verify_lane --staged: ok — lane=$LANE_ID · $sok owned · $sshared shared-append · $sseat seat-override"
        # ⚠ DISCLOSE A GUESSED IDENTITY. `.levain-lane` is ONE slot every lane in this worktree writes,
        # so a name read from it is the LAST WRITER's, not necessarily yours — and the dangerous
        # direction is the one that passes: paths owned by whoever the slot names sail through under
        # a stranger's identity. Saying so is the difference between a wrong grade and a silent one.
        if [[ "$LANE_SRC" == "file" ]]; then
            echo "   ⚠ identity came from the SHARED .levain-lane slot, not from LEVAIN_LANE."
            echo "     Every lane in this worktree writes that one file — if you are not '$LANE_ID',"
            echo "     this grade is WRONG and it just passed you. Commit via"
            echo "     \`tools/verify_lane.sh <your-lane> --commit …\`, which carries identity itself."
        fi
    fi
    exit 0
fi

if [[ "${1:-}" == "--list" ]]; then
    echo "LANES (roster names — start a session under one of these):"
    while read -r l; do
        n=$(globs_for "$l" | tr ' ' '\n' | grep -c . || true)
        printf '  %-18s %s path pattern(s)\n' "$l" "$n"
    done < <(lanes)
    echo ""
    : # sentinel printed by the EXIT trap (pass AND honest red)
    exit 0
fi

# ⛔ THE AUDIT IS THE GUARD'S OWN GUARD. Two failure modes make this script silently useless: a path
# that no lane owns (a hole — edits there are never questioned) and a path owned twice (a collision —
# two lanes both believe they may write it). Neither is visible from a passing lane check.
# ⚠ NO ARGUMENT = --audit, ON PURPOSE. `tools/verify_all.sh` runs every `tools/verify_*.sh` with no
# arguments, so a bare invocation must be a real check rather than a usage error — otherwise adding
# this guard to the tree would have turned the whole suite red. And the audit IS the right default:
# the per-lane check grades a working tree, which is a moment; the audit grades the MAP, which is an
# invariant, and a map with a hole makes every per-lane check quietly weaker.
if [[ "${1:-}" == "--audit" || -z "${1:-}" ]]; then
    fail=0
    echo "§1 COVERAGE — EVERY TRACKED FILE is owned by exactly one lane, or shared-append"
    declare -a orphans=()
    shared=0
    # ⛔ `git ls-files` WITH NO PATHSPEC, ON PURPOSE. Any narrowing here re-creates the 2026-08-12
    # blindness: a scan that omits a directory reports green over it, and the omission is invisible
    # precisely because the check it skips is the one that would have named it.
    while IFS= read -r f; do
        for s in "${SHARED_APPEND[@]}"; do
            [[ "$f" == "$s" ]] && { shared=$((shared+1)); continue 2; }
        done
        owners=0
        while read -r l; do
            for g in $(globs_for "$l"); do
                # shellcheck disable=SC2053
                [[ "$f" == $g ]] && { owners=$((owners+1)); break; }
            done
        done < <(lanes)
        if [[ $owners -eq 0 ]]; then orphans+=("$f")
        elif [[ $owners -gt 1 ]]; then echo "  FAIL $f is owned by $owners lanes"; fail=$((fail+1)); fi
    done < <(git ls-files)

    if [[ ${#orphans[@]} -gt 0 ]]; then
        echo "  FAIL ${#orphans[@]} tracked file(s) owned by NO lane and not shared-append —"
        echo "       every one is a hole in the guard, and a lane touching it gets a STOP it cannot act on:"
        printf '         %s\n' "${orphans[@]}"
        fail=$((fail + 1))
    else
        echo "  ok   all $(git ls-files | wc -l | tr -d ' ') tracked files covered ($shared shared-append, rest owned)"
    fi

    echo ""
    echo "§2 THE MAP DOES NOT NAME FILES THAT DO NOT EXIST"
    # ⛔⛔ THE DISCRIMINATOR IS WHETHER GIT HAS EVER SEEN THE PATH, AND IT WAS FORCED BY THE FIRST
    # REAL GRANT (2026-08-12). §1 requires every tracked file to be owned; §2 required every named
    # path to exist. A lane about to create a file must be granted ownership BEFORE its first
    # commit — otherwise the file lands untracked-and-unowned and the lane cannot commit at all —
    # and that forward grant then failed §2. The two checks were in a deadlock nobody had hit
    # because every pattern in this map was a literal path to an ALREADY-EXISTING file.
    #   · absent AND git has history for it  → STALE. A deleted or renamed file the map still
    #     claims. This is the rot §2 was written for. HARD FAIL.
    #   · absent AND git has never seen it   → PENDING GRANT. The seat granted a lane a file it is
    #     about to write. WARN, do not fail.
    # ⚠ THE COST, STATED: a TYPO in a grant (`Wisp/Chrat.swift`) is indistinguishable from a pending
    # grant and gets only a warning. It does not escape — the real file lands untracked and unowned,
    # and §1 fails on it the moment the lane tracks it. The typo self-corrects one commit later, at
    # the cost of one confusing message. Accepted deliberately over deadlocking every forward grant.
    stale=0; pending=0
    while read -r l; do
        for g in $(globs_for "$l"); do
            # A glob is fine if it matches nothing only when it contains a wildcard.
            [[ "$g" == *[\*\?]* || -e "$g" ]] && continue
            if [[ -n "$(git log --all --oneline -- "$g" 2>/dev/null)" ]]; then
                echo "  FAIL $l claims $g, which git has seen before and which is now gone (stale entry)"
                stale=$((stale+1))
            else
                echo "  ⚠ warn $l claims $g, which git has never seen — reads as a PENDING GRANT."
                echo "         If that file never lands, this line is a typo or a dropped task."
                pending=$((pending+1))
            fi
        done
    done < <(lanes)
    [[ $stale -eq 0 && $pending -eq 0 ]] && echo "  ok   every literal path in the map exists"
    [[ $stale -eq 0 && $pending -gt 0 ]] && echo "  ok   no stale entries ($pending pending grant(s) above)"
    fail=$((fail + stale))

    echo ""
    if [[ $fail -eq 0 ]]; then
        echo "ALL LANE-MAP AUDIT CHECKS PASSED"
    else
        echo "$fail LANE-MAP AUDIT CHECK(S) FAILED"
        exit 1
    fi
    : # sentinel printed by the EXIT trap (pass AND honest red)
    exit 0
fi

# ⛔⛔ TWO MODES, AND THE SECOND ONE IS THE COMMIT GATE. Added 2026-08-12 after `levain-world` measured
# that ONE lane's new, not-yet-granted file hard-failed EVERY OTHER lane's guard.
#
#   tools/verify_lane.sh <lane>                 SURVEY — grade the whole tree. Unowned files are
#                                               NAMED but do not halt a lane that did not make them.
#   tools/verify_lane.sh <lane> <paths...>      GATE — grade exactly the paths you are about to
#                                               commit. This is the one to run before `git commit`.
#
# THE DEFECT THIS FIXES, AND IT IS THE SEAT'S: the guard graded the TREE while rule 3 makes a lane
# commit a PATHSPEC. Those are different sets, and the gap is where the false STOP lived. Measured:
# `levain-world` creates `Wisp/Beacon.swift` and has not yet asked for its grant — the NORMAL sequence,
# and by the morning's own finding the normal state of every lane's first file — and
# `verify_lane.sh levain-instrument` exits 1 naming a file that lane has never heard of. Sole cause;
# exit 0 the moment the file is removed. `--audit` is unaffected (it scans tracked files only,
# verified).
#
# ⚠ IT IS A FALSE *STOP*, NOT A FALSE PASS, WHICH IS WHY IT SURVIVED THE DAY. The commit it forbids
# is safe: rule 3 (`git commit -- <own paths>`) sweeps none of the other lane's file. So the guard
# told a lane to halt and escalate over work that could not affect it — and a lane that obeys,
# correctly, because it is told to obey, blocks on someone else's pending grant.
#
# 📐 WHY n=2 DID NOT SHOW IT AND n=6 WOULD. The window is *file created → grant lands*, and every
# lane opens one on its first task. At two lanes it fired once and the grant happened to land first.
# At six, any one lane's pending grant blocks the other five, and their symptom is a `⛔ STOP` naming
# a file they never touched. **This is the finding that would have made a six-lane launch look like
# a coordination failure when it was a guard artifact.**
#
# ⚡ THE FIX IS NOT SYMMETRY. `levain-world` observed that the staging argument reads across unchanged
# to the unowned branch, and it does — but applying it would ALSO silence the lane that CREATED the
# file, which is the one session that must be told before it commits. So the discriminator is not
# who staged it; it is **whether you are asking about the tree or about your own commit.** In GATE
# mode an unowned path is a hard fail: you named it, you are committing it. In SURVEY mode it is a
# warning, because you may simply be standing next to someone else's work.
# ⛔ Unowned AND STAGED stays fatal in BOTH modes — under rule 1 nobody stages anything, so a staged
# unowned file is anomalous however you got there.
LANE="${1:-}"
if ! lanes | grep -qx "$LANE"; then
    echo "⛔ unknown lane '$LANE'. Known lanes:"; lanes | sed 's/^/     /'; exit 1
fi

OWNED=$(globs_for "$LANE")
[[ -n "$OWNED" ]] || { echo "⛔ lane '$LANE' owns nothing — a typo in $MAP"; exit 1; }

# Everything this working tree would commit: modified, staged, and untracked. Untracked matters most
# — a NEW file in someone else's directory is the commonest way a lane spills.
#
# ⛔⛔ THE WORKING TREE IS SHARED BETWEEN ALL LIVE LANES, AND UNTIL 2026-08-12 THAT MADE THIS CHECK
# LIE. Caught by `levain-world` fifteen minutes into the first parallel trial, from reading the script
# rather than mutating the tree — *"writing a probe file outside my lane to test the lane guard is
# the thing itself."* `git status` has no pathspec (deliberately — the same no-narrowing rule as
# §1), so it reports the WHOLE repo. The moment any other lane has uncommitted work, every lane's
# check reports it as that lane's own violation. **PROVEN LIVE, and the SEAT was the first
# contaminator:** with only the seat's in-progress `the_instrument.md` edit present,
# `verify_lane.sh levain-world` printed "⛔ levain-world WROTE OUTSIDE ITS LANE" and exited 1.
#
# ⚠ WHY THAT WAS DANGEROUS RATHER THAN ANNOYING: the false positive is worded identically to the
# trial's kill condition (a), *"a lane wrote outside its lane."* The trial could have been killed at
# fan-in by an artifact of the guard's scope instead of by anything a lane did.
#
# ⚡ THE FIX IS ATTRIBUTION, NOT NARROWING. A file cannot tell you who edited it — but STAGING is an
# intentional act by THIS session, and a merely-dirty file in a shared checkout is not. So:
#   · you own it                          → fine
#   · shared-append                       → fine
#   · owned by ANOTHER lane, and STAGED   → ⛔ HARD FAIL. You are about to commit someone else's file.
#   · owned by ANOTHER lane, not staged   → ⚠ NOISE. Their work, in a tree you share. Not yours.
#   · owned by NOBODY                     → ⛔ HARD FAIL. A hole in the map; ask the seat for a grant.
# THE RULE THIS IMPLIES, AND IT IS NOW LOAD-BEARING: **commit with an explicit pathspec**
# (`git commit -- <your files>`), never `git add -A`. In a shared tree `-A` sweeps up your
# teammates' work, and no guard can distinguish that from a spill after the fact.
shift || true

# ⛔⛔ `--commit -m "msg" <paths...>` — GATE AND COMMIT AS ONE ACT, BECAUSE TWO TYPED LISTS CANNOT BE
# TRUSTED TO MATCH. Found by `levain-world` immediately after GATE shipped, and it is a residual IN the
# gate every lane now runs before every commit:
#
#     verify_lane.sh levain-world Wisp/Chart.swift            # passes
#     git commit -- Wisp/Chart.swift Wisp/SweepCore.swift   # commits a spill the gate never saw
#
# The gate graded one list; git committed a different one; nothing bound them. **Same class as the
# seat's own `| tail` near-miss an hour earlier, one layer worse — there the check's VERDICT was
# discarded, here its SUBJECT is.** Both are two-step checks whose second step can silently differ
# from the first, which is the shape to watch for in anything this apparatus adds next.
# ⚡ The close is not a warning, it is arity: the paths are typed ONCE and this script performs the
# commit, so passing the gate and committing are one act. A lane that uses `--commit` cannot desync
# them because there is no second list to desync.
COMMIT_MODE=0; COMMIT_MSG=""
if [[ "${1:-}" == "--commit" ]]; then
    COMMIT_MODE=1; shift
    if [[ "${1:-}" == "-m" ]]; then COMMIT_MSG="${2:-}"; shift 2; fi
    [[ -n "$COMMIT_MSG" ]] || { echo "⛔ --commit needs -m \"message\""; exit 1; }
    [[ $# -gt 0 ]] || { echo "⛔ --commit needs at least one path — it is the whole point"; exit 1; }
fi
GATE_PATHS=("$@")
if [[ ${#GATE_PATHS[@]} -gt 0 ]]; then
    MODE="GATE"
    CHANGED=$(printf '%s\n' "${GATE_PATHS[@]}")
    echo "  GATE MODE — grading only the ${#GATE_PATHS[@]} path(s) you named, which is what"
    echo "  \`git commit -- …\` will actually commit. Other lanes' work in the tree is not your business."
else
    MODE="SURVEY"
    CHANGED=$(git status --porcelain --untracked-files=all | sed 's/^...//' | sed 's/.* -> //')
fi
STAGED=$(git diff --cached --name-only)
is_staged() { grep -qxF "$1" <<<"$STAGED"; }

if [[ -z "$CHANGED" ]]; then
    echo "  ok   $LANE: working tree clean, nothing to check"
    echo ""
    echo "ALL LANE CHECKS PASSED"
    : # sentinel printed by the EXIT trap (pass AND honest red)
    exit 0
fi

# ⛔⛔⛔ **THE SEAT IS NEVER OUT OF BOUNDS — RULED BY PHILL, 2026-08-27.**
#
# ⚖ His words, and they are the whole specification: *"The lanes were meant to ENABLE OPTIONAL
# PARALLEL WORK NOT BECOME FUCKING WORK PRISONS that become an excuse for you to NOT DO THE FUCKING
# WORK."*
#
# **WHAT WENT WRONG, live, the morning this was added.** The seat — the only session running, with no
# other lane alive anywhere — fixed a ONE-LINE stale comment in `AntagonistStore.swift`, was refused
# by this gate because that file is `levain-world`'s, **backed the fix out, and filed a written request
# addressed to a lane that does not exist and was never going to read it.** The defect stayed on
# disk. Nothing was protected. A guard whose whole purpose is to stop two live agents colliding
# spent its authority stopping the integrator from fixing a comment alone in an empty repo.
#
# ⛔ **AND THE AUTHORITY WAS INVERTED, WHICH IS THE PART THAT MAKES IT STRUCTURAL RATHER THAN A BAD
# CALL.** `LANES.tsv` states: *"an ownership grant is a RULING. A lane asks; it does not edit"* — and
# the grant comes FROM the seat. So the refusal told the seat to go ask the seat for permission, and
# then to wait for itself. There is no state in which that resolves. `a_policy_is_not_real_until_the_
# thing_that_enforces_it_exists`, fired in reverse: an enforcement with no policy behind it.
#
# ▶ **THE RULE: ownership binds LANES. It does not bind the SEAT.** A non-seat lane still may not
# touch another lane's file — that is the collision this gate exists to prevent and it is unchanged.
# The seat is the integrator: it grants ownership, it holds the map, it runs the fan-in, and it
# already merges every lane's work by design. A seat commit outside its own rows is NAMED, loudly,
# so a genuinely parallel run is still visible in the output — but it is **never fatal**.
# ⚠ **THE COST, NAMED AND ACCEPTED:** during a real N>1 run the seat can now commit over a live
# lane's file without being stopped. That is a judgement the seat was always trusted with (it is the
# one that ASSIGNS the rows), and the warning below is what makes it visible. The alternative —
# refusing — has now demonstrably cost a real fix and produced a written request to nobody.
violations=0; allowed=0; appended=0; noise=0; stagednote=0
seatoverride=0
declare -a ungranted=()
declare -a noisy=()
declare -a seattook=()
while IFS= read -r f; do
    [[ -z "$f" ]] && continue
    ok=0
    for g in $OWNED; do
        # shellcheck disable=SC2053
        [[ "$f" == $g ]] && { ok=1; break; }
    done
    if [[ $ok -eq 1 ]]; then
        allowed=$((allowed+1)); continue
    fi
    for s in "${SHARED_APPEND[@]}"; do
        [[ "$f" == "$s" ]] && { ok=2; break; }
    done
    if [[ $ok -eq 2 ]]; then
        appended=$((appended+1)); continue
    fi
    # Name the actual owner — "you may not touch this" is half an answer; the other half is who to ask.
    ownerlane=""
    while read -r l; do
        for g in $(globs_for "$l"); do
            # shellcheck disable=SC2053
            [[ "$f" == $g ]] && { ownerlane="$l"; break 2; }
        done
    done < <(lanes)

    # ⚡ OWNED BY SOMEONE ELSE AND NOT STAGED = a teammate's live work in a tree you share. Reporting
    # it as YOUR violation is what made this check lie before 2026-08-12. Named, never fatal.
    # ⛔⛔ SURVEY ONLY. In GATE mode you NAMED this path, so you are committing it, and whether anyone
    # staged it is irrelevant — the noise bucket is a fact about a tree you are standing in, not about
    # a list you wrote. Caught by this file's own mutation test #4, which passed when it must fail:
    # `verify_lane.sh levain-instrument Wisp/Chart.swift` exited 0 because Chart.swift was committed and
    # therefore unstaged. **Third time in one day that a heuristic correct in one mode was applied to
    # the other** — the same shape as grading the tree when the commit is a pathspec.
    if [[ "$MODE" == "SURVEY" && -n "$ownerlane" ]] && ! is_staged "$f"; then
        noisy+=("$(printf '%-46s owned by %s' "$f" "$ownerlane")")
        noise=$((noise+1)); continue
    fi

    # ⛔ UNOWNED IN SURVEY MODE IS A WARNING, NOT A HALT — it may be another lane's first file,
    # sitting in the window between "created" and "granted", which every lane opens on its first
    # task. In GATE mode you NAMED it, so you are committing it, and it is fatal.
    if [[ -z "$ownerlane" && "$MODE" == "SURVEY" ]] && ! is_staged "$f"; then
        ungranted+=("$(printf '%-46s %s' "$f" "unowned — YOURS? ask the seat for a grant before you commit it")")
        continue
    fi

    # ⛔⛔ THE SEAT PASSES. Named, never fatal — see the ruling block at the top of this section.
    #    Kept BEFORE the `$violations` header so a seat run never prints "MAY NOT COMMIT".
    if [[ "$LANE" == "$SEAT_LANE" ]]; then
        if [[ -n "$ownerlane" ]]; then
            seattook+=("$(printf '%-46s %s' "$f" "owned by $ownerlane — SEAT OVERRIDE")")
        elif git check-ignore -q -- "$f" 2>/dev/null; then
            seattook+=("$(printf '%-46s %s' "$f" "GENERATED + gitignored — ownership does not apply")")
        else
            seattook+=("$(printf '%-46s %s' "$f" "unowned — SEAT OVERRIDE; give it a row in LANES.tsv")")
        fi
        seatoverride=$((seatoverride+1)); continue
    fi

    if [[ $violations -eq 0 ]]; then
        echo "  ⛔ $LANE MAY NOT COMMIT THIS:"
    fi
    if [[ -n "$ownerlane" ]]; then
        # ⚠ SAY WHICH REASON ACTUALLY FIRED. This printed "AND STAGED" unconditionally, so a GATE
        # refusal over a clean index asserted a staging that had not happened — verified live, the
        # index was empty. In GATE the fatal reason is that you NAMED it; staging is irrelevant and
        # claiming it is a false statement in the one message a blocked lane will act on.
        if [[ "$MODE" == "GATE" ]] && ! is_staged "$f"; then
            printf '       %-46s %s\n' "$f" "owned by $ownerlane — and you NAMED it in this commit"
        else
            printf '       %-46s %s\n' "$f" "owned by $ownerlane — AND STAGED (see note below)"
            stagednote=1
        fi
    elif git check-ignore -q -- "$f" 2>/dev/null; then
        # ⛔⛔ **A GITIGNORED PATH IS GENERATED STATE AND OWNERSHIP DOES NOT APPLY TO IT — 2026-08-19.**
        # ⚡ **THIS BRANCH EXISTS BECAUSE THE GUARD ACTIVELY CORROBORATED A WRONG DIAGNOSIS.** A lane
        # hit a stale `Wisp.xcodeproj/project.pbxproj`, asked this guard about it, and was told
        # *"unowned — ask the seat for a grant; this is a hole in LANES.tsv."* From that it concluded
        # the lane protocol was structurally incomplete: that *"the second half of adding a file lives
        # in a path no lane owns."* **There is no second half.** `*.xcodeproj` is gitignored and
        # GENERATED by XcodeGen from `project.yml`, which sweeps `Wisp/` wholesale — a new `.swift`
        # needs no project edit, no grant, and no commit. The file was never red; a generated artifact
        # in one working tree was stale, and `xcodegen` fixed it in a second.
        # ⛔ **THE GUARD DID NOT MERELY FAIL TO HELP — IT SUPPLIED THE CONFIRMING EVIDENCE**, and it
        # would have manufactured the identical diagnosis for the next lane. That is worse than
        # silence: four independent probes agreed, and all four were reading downstream of one
        # unexamined premise (*the project file is source*), which never surfaced as a claim anybody
        # could check. `spore-507`'s class — generated state treated as tracked state — arriving
        # through the instrument built to adjudicate ownership.
        printf '       %-46s %s\n' "$f" "(GENERATED + gitignored — ownership does not apply; run \`xcodegen\` if it is stale)"
    else
        printf '       %-46s %s\n' "$f" "(unowned — ask the seat for a grant; this is a hole in LANES.tsv)"
    fi
    violations=$((violations+1))
done <<< "$CHANGED"

if [[ ${#ungranted[@]} -gt 0 ]]; then
    echo "  ⚠ UNOWNED AND UNSTAGED — a new file nobody has been granted yet:"
    printf '       %s\n' "${ungranted[@]}"
    echo "       If it is YOURS: message the seat for a grant, then \`verify_lane.sh $LANE <paths>\`."
    echo "       If it is NOT: ignore it. It is another lane inside the create→grant window and it"
    echo "       cannot reach your commit — this used to halt you, which was the guard's bug."
fi

if [[ ${#seattook[@]} -gt 0 ]]; then
    echo "  ⚡ SEAT OVERRIDE — committing outside your own rows, which the SEAT is allowed to do:"
    printf '       %s\n' "${seattook[@]}"
    echo "       ⚖ Phill 2026-08-27: lanes ENABLE optional parallel work; they are not work prisons."
    echo "       Ownership binds LANES, not the SEAT — you grant the rows, you hold the map, you run"
    echo "       the fan-in. Asking yourself for a grant and then waiting for yourself never resolves."
    echo "       ⚠ IF A PARALLEL RUN IS GENUINELY LIVE: check the lane is not mid-edit on these before"
    echo "       you commit. That is a judgement, not a refusal — this line is the whole safeguard."
    echo "       ▶ If a file above is UNOWNED, give it a row in LANES.tsv while you are here."
fi

if [[ ${#noisy[@]} -gt 0 ]]; then
    echo "  ⚠ NOT YOURS, NOT YOUR PROBLEM — another lane's uncommitted work in the shared tree:"
    printf '       %s\n' "${noisy[@]}"
fi
if [[ $stagednote -eq 1 ]]; then
    cat <<'NOTE'
  ⛔ ABOUT "AND STAGED", AND THE HONEST VERSION IS NARROWER THAN THE FIRST FIX CLAIMED:
     THE GIT INDEX IS SHARED TOO. Staging narrows the false-positive window from "constantly, while
     anyone works" to "briefly, while someone is committing" — it does NOT attribute the act to your
     session. Another lane running `git add` on its OWN files puts them in the index YOU read.
     ⚡ THE COMMIT PROTOCOL, ALL FOUR LINES OF IT (measured 2026-08-12, see LANES.tsv):
        1. NEVER `git add <path>`      — puts content in the index every lane reads.
        2. NEW file → `git add -N <path>`, intent-to-add. Registers the path; the index stays
           contentless, so `git diff --cached` stays EMPTY and it cannot trip this check.
        3. `git commit -- <your paths>` — always. Commits from the working tree.
        4. ⛔ NEVER `git commit -a`     — it SWEEPS other lanes' `-N` entries. A plain untracked
           file is safe from `-a`; an `-N` one is not. That is the price of step 2, and step 3
           is what pays it.
     If these are another lane's files mid-commit and yours are clean, you are not the problem —
     but do not commit through it: `git commit -- <your paths>` and this goes quiet.
NOTE
fi

echo ""
echo "LANE $LANE [$MODE]: $allowed owned · $appended shared-append · $noise other-lane (ignored) · ${#ungranted[@]} ungranted (named) · $seatoverride seat-override (named) · $violations out of bounds"
if [[ $violations -gt 0 ]]; then
    echo ""
    echo "⛔ STOP. Do not commit. Per spore-504: message the owning lane or the ruling seat and WAIT."
    echo "   One message beats a bad merge, and a ruling must be BROADCAST *and* WRITTEN — broadcast"
    echo "   so live lanes stop now, written so tomorrow's lane inherits it."
    exit 1
fi
echo "ALL LANE CHECKS PASSED"

if [[ $COMMIT_MODE -eq 1 ]]; then
    # ⛔⛔ THE PATHSPEC RULE PROTECTS AN OWNED FILE AND NOT A SHARED-APPEND ONE. Found by
    # `levain-instrument` 2026-08-12, sixth shared-tree hazard of the day: `git commit -- <path>`
    # commits that path's FULL working-tree content, which is real protection on a file only you
    # write — and on the six shared surfaces both lanes legitimately write the SAME file, so the
    # pathspec form sweeps the other lane's uncommitted appends. Measured: `467272f`, titled
    # "levain-world's observations", contains two of levain-instrument's blocks.
    #
    # ⚠ NOT FATAL, AND THE GUARD IS RIGHT NOT TO FIRE — committing a shared-append surface is
    # PERMITTED, nothing is lost, and this is not kill-condition (a). What breaks is ATTRIBUTION: a
    # reader asking why a thing was built finds the reasoning in a commit labelled another lane's.
    # The sharper risk is TIMING, not credit — an append written over several editor saves can be
    # committed half-finished and the block will look complete.
    # ⚡ SO IT IS NAMED RATHER THAN BLOCKED, and it is checkable because the block format already
    # carries the answer: `## <timestamp> · <lane> · <kind>`. The proposed mitigation was a reading
    # habit; a habit that must fire on every commit is the substrate this project has repeatedly
    # proved wrong. `structural_invariants_beat_discipline` — so the machine reads the headers.
    for p in "${GATE_PATHS[@]}"; do
        for s in "${SHARED_APPEND[@]}"; do
            [[ "$p" == "$s" ]] || continue
            foreign=$( { git diff -- "$p" 2>/dev/null || true; } \
                | { grep -E '^\+## .* · .* · ' || true; } \
                | { grep -vF " · $LANE · " || true; } )
            if [[ -n "$foreign" ]]; then
                echo ""
                echo "  ⚠ $p CARRIES ANOTHER LANE'S UNCOMMITTED BLOCKS — they will ride YOUR commit:"
                sed 's/^+/       /' <<<"$foreign"
                echo "       Permitted, and nothing is lost. But the commit will be labelled yours."
                echo "       ⛔ WHEN READING THESE FILES, TRUST THE '· <lane> ·' HEADER, NEVER THE COMMIT AUTHOR."
                echo "       ⚠ And a block still being written can be committed looking finished."
            fi
        done
    done
    # ⚠ `git add -N` FIRST, for exactly the paths just graded — rule 2. `git commit --` refuses an
    # UNTRACKED path outright, so without this the one case the gate exists for (a lane's first new
    # file) would pass the check and then fail the commit. Intent-to-add leaves the shared index
    # contentless, so this cannot trip another lane's staged check.
    for p in "${GATE_PATHS[@]}"; do [[ -e "$p" ]] && git add -N "$p" 2>/dev/null || true; done
    echo ""
    echo "  → committing exactly these ${#GATE_PATHS[@]} path(s), the same list just graded:"
    printf '       %s\n' "${GATE_PATHS[@]}"
    # ⛔⛔ IDENTITY RIDES THE COMMAND — `LEVAIN_LANE="$LANE"` IS THE WHOLE FIX FOR THE N>1 SLOT BUG.
    # The caller NAMED its lane in argv and the gate just graded against that name; the pre-commit
    # hook then re-resolved identity independently and, finding only the shared `.levain-lane` slot,
    # graded the SAME commit against whoever wrote that file last. Two layers of one gate disagreeing
    # about who you are. Passing it down makes the sanctioned path immune to the slot entirely, and
    # an inline prefix survives where an `export` cannot because it rides the same command as the
    # child. ⚠ This is also why a lane must never "fix" a clobbered slot by writing its own name in:
    # that makes IT the last writer and breaks everyone else. The slot is the bug; do not fight over it.
    LEVAIN_LANE="$LANE" git commit -m "$COMMIT_MSG" -- "${GATE_PATHS[@]}" || { echo "⛔ git commit failed — nothing was committed"; exit 1; }
fi

: # sentinel printed by the EXIT trap (pass AND honest red)

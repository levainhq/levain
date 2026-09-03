#!/usr/bin/env bash
# ⛔⛔ GENERATE A LANE BRIEF **FROM THE MAP**, NEVER FROM A PLAN DOCUMENT.
#
# ## Why this is a script
#
# The LAUNCH is a transition with no artifact, so it ran on the seat's memory and failed the same
# way three times:
#   · 2026-08-12 — briefs said *"STOP and message the seat"* and never said HOW. No address, no
#     tool, no mention that `ListAgents` exists. An instruction to escalate with no escalation path
#     is a lane that guesses instead.
#   · 2026-08-13 — briefs said *"use ListAgents to find levain-seat and SendMessage"*. One indirection
#     short of an address, in a file that already carried the rule. And the lookup was AMBIGUOUS:
#     the listing carried both `0813+3 levain-seat` (live) and `0813+2 levain-seat-old1` (dead). Caught
#     by Phill minutes after launch: *"did you let them know how to message you back?"*
#   · The same launch briefed a lane straight from a stale plan table that assigned it a file
#     `LANES.tsv` gives to another lane. The LANE caught it before its first commit.
#
# All three are the same defect: the brief was composed by hand from memory or from a document that
# is not the map. This script cannot make either mistake — the paths come from `LANES.tsv`, and it
# REFUSES to emit a brief with no seat address rather than emitting a thinner one.
#
# Usage:
#   tools/lane_brief.sh <lane> "<seat name [ref]>" [decoy ...]
#
#   tools/lane_brief.sh levain-walk "0814+3 levain-seat [a1b2c3]" "0814+1 levain-seat-old"
#
# ⛔⛔ THE PARAGRAPH BELOW IS RETIRED — MEASURED FALSE 2026-08-19, TWICE IN ONE EVENING. It said the
# ref is REQUIRED and a bare name is rejected outright. Both lanes launched that night (levain-instrument
# and a flow-side session) reported UNPROMPTED that they reached the seat with the BARE NAME
# `0819+5 levain-seat`, no ref. Two for two, on the one question this file makes every lane report.
# ⚠ The body already got this right — the emitted brief has said "send the bare name FIRST, and if it
# is refused retry with the ref" since earlier the same day. Only this header kept asserting the old
# measurement, so the file disagreed with the text it generates. A stale measurement pinned as a rule
# is the exact trap the retired paragraph itself warns about, one layer up.
# ▶ THE RULE NOW: pass the seat's NAME. The ref is optional and only disambiguates a collision.
# ⚠ AND THE SEAT CANNOT READ ITS OWN REF ANYWAY — `ListAgents` does not list self, and the ref is an
# internal registry id, NOT derivable from the session id on disk (measured: `0819+0 main` is
# sessionId `60cd08c1` and lists as `[8811dd]`). A brief generator that hard-required the ref would
# be unrunnable by the only role that runs it.
#
# ~~COPY THE SEAT'S NAME **AND** ` [ref]` VERBATIM FROM `ListAgents`. The ref is REQUIRED, measured
# 2026-08-13: a bare name is rejected outright (`'0813+4 levain-instrument' is not an agent in this
# conversation`), costing one round trip on the channel whose whole value is that a round trip is
# cheap.
set -uo pipefail
cd "$(dirname "$0")/.."

LANE="${1:-}"
SEAT="${2:-}"
shift 2 2>/dev/null || true
DECOYS=("$@")

if [[ -z "$LANE" ]]; then
    echo "usage: tools/lane_brief.sh <lane> \"<seat name [ref]>\" [decoy ...]" >&2
    echo "" >&2
    tools/verify_lane.sh --list >&2 || true
    exit 1
fi

# ⛔⛔ HERESTRING, NOT A PIPE, AND THIS IS THE SECOND TIME THIS REPO HAS BEEN BITTEN BY THE SAME
# CLASS. `tools/verify_lane.sh --list | grep -q "..."` FAILS ON A MATCH: `grep -q` exits the instant
# it matches, the producer takes SIGPIPE, and `set -o pipefail` reports the whole pipeline non-zero
# — so the check inverts and every valid lane is rejected. Measured here 2026-08-14 (`levain-walk`
# rejected while `--list` printed it one line later). `verify_lane.sh`'s own header records this
# project reintroducing a grep-under-pipefail bug hours after fixing it; naming the class did not
# prevent the next instance, which is the argument for the guard and now for this comment.
ROSTER=$(tools/verify_lane.sh --list 2>/dev/null || true)
if ! grep -q "  $LANE " <<< "$ROSTER"; then
    echo "⛔ '$LANE' is not a lane in LANES.tsv." >&2
    echo "$ROSTER" >&2
    exit 1
fi

# ⛔ THE REFUSAL IS THE POINT. A brief with no address is the 08-12 failure; a brief telling the
# lane to go look is the 08-13 one. This script will not emit either, so the seat cannot ship one
# by forgetting.
if [[ -z "$SEAT" ]]; then
    cat >&2 <<'REFUSE'
⛔ NO SEAT ADDRESS — REFUSING TO GENERATE A BRIEF.

A lane brief MUST carry the seat's literal address. Twice now this project shipped a brief without
one (no escalation path at all, then an escalation path with a lookup in it), and both times the
lane's only remaining option was to guess.

  1. Run `ListAgents` in the seat session.
  2. Copy the seat's row VERBATIM — name AND the ` [ref]` suffix.
  3. tools/lane_brief.sh <lane> "0814+3 levain-seat [a1b2c3]" "<any dead lookalike rows>"

Name the decoys too: a listing routinely carries dead sessions with similar names, and a lane told
to "find levain-seat" gets two rows and no way to choose.
REFUSE
    exit 1
fi

PATHS=$(grep -v '^#' LANES.tsv | awk -F'\t' -v L="$LANE" '$1==L {print $2}' | tr ' ' '\n' | grep -c .)

cat <<BRIEF
════════════════════════════════════════════════════════════════════════════════
 LANE BRIEF — $LANE
 generated from LANES.tsv @ $(git rev-parse --short HEAD) · $(date '+%Y-%m-%d %H:%M %Z')
════════════════════════════════════════════════════════════════════════════════

YOU ARE \`$LANE\`. Name this session after the lane — the roster IS the ownership map.

⛔ YOU MAY READ ANYTHING. YOU MAY WRITE ONLY THE $PATHS PATH PATTERN(S) BELOW.

$(grep -v '^#' LANES.tsv | awk -F'\t' -v L="$LANE" '$1==L {print $2}' | tr ' ' '\n' | grep . | sed 's/^/    /')

── HOW TO REACH THE SEAT ───────────────────────────────────────────────────────
  SendMessage({to: "$SEAT", message: "..."})

  ▶ SEND THE BARE NAME FIRST — everything up to the space before the bracket. If that is refused,
    retry with the full string above, ref included.
  ⛔⛔ A REFUSAL HERE MEANS THE ADDRESS FORM IS WRONG. IT DOES NOT MEAN THE SEAT IS GONE. Try the
     other form before concluding anything, and tell the seat which one worked.
     ⚠ This line said the opposite until 2026-08-19 — "copy it EXACTLY, a bare name is rejected
     outright" — from a real measurement taken 2026-08-13. \`levain-world\` hit the exact reverse that
     afternoon: the ref form was refused as unreachable and the error told it to use the bare name.
     A measurement was pinned as a rule, the harness moved under it, and the instruction became a
     trap of precisely the kind this file exists to prevent. Both forms are named now, and which one
     works is something a lane REPORTS rather than something this file asserts.
$(if [[ ${#DECOYS[@]} -gt 0 ]]; then
    echo "  ⚠ DECOYS — these rows are DEAD. Do not message them:"
    printf '        %s\n' "${DECOYS[@]}"
  else
    echo "  ⚠ No decoys declared. If ListAgents shows another row with a similar name, it is NOT"
    echo "        the seat — ask before messaging it."
  fi)

  FIRE IT THE MOMENT YOU ARE BLOCKED OR NEED A RULING: a brief naming a file you do not own · a
  premise that looks amended · a NEW FILE needing an ownership grant · a design question · anything
  you would otherwise GUESS. An unblock costs one round trip; a wrong guess costs a rebuild.

── WORKING A CARRIED FINDING: GREP THE COORDINATE, **READ THE EPISODE** ────────
  ⛔ RE-DERIVE AGAINST DISK before fixing — line numbers drift and a carried block is honest about
     the tree it READ, never the tree you are standing in.
  ⛔⛔ AND THAT IS ONLY HALF. **READ THE ORIGINAL EPISODE BY ID BEFORE YOU TOUCH IT:**
        python3 ~/Briefcase/flow/scripts/episodic.py search "<the finding>" --since 30d
     A grep recovers COORDINATES. It cannot recover the ANALYSIS that produced the finding, because
     the analysis was never on disk — it is in the episode, and what reaches you in a brief is a
     summary of it.

  ⚡ MEASURED 2026-08-19, and it cost a false justification that shipped. A lane worked three
  \`| grep -q\` sites from the brief's summary, re-derived every coordinate correctly, and wrote
  *"180,981 bytes, 0/1000 SIGPIPE"* as the reason the conversion was safe. The original 08-15
  episode had isolated BOTH governing variables and named a \`tr '\n' ' '\` / exit-status coupling a
  hundred lines away. Re-measured against the real episode:
        single-line  180,981 B, 1 line       →    0/200 SIGPIPE
        newlines     200,552 B, 2,709 lines  →  200/200 SIGPIPE
  **Line structure, not size** — so the shipped comment placed a size next to a zero and implied the
  exact inverse of the truth. A number that is accurate and implies the opposite of what it measures
  survives review, which is what makes this worse than a wrong number.

  ⚠ THE CLASS, because it is bigger than one brief: **a findings pipeline that summarises is a
  description-drift generator pointed at the immune system's own output.** The summary is ACCURATE,
  just thinner, so nothing looks wrong at any step. The data was one \`episodic.py\` call away the
  entire time and nothing told anyone to make it. Now something does.

  ⚠⚡ **AND THIS VERY PARAGRAPH WAS BEING EATEN BY THE GENERATOR THAT PRINTS IT — FIXED 2026-08-20.**
  \`cat <<BRIEF\` is an UNQUOTED heredoc, so a markdown backtick in this prose is a command
  substitution: bash RAN the text between the backticks and substituted its output, which is empty.
  Three pairs were unescaped, so every brief this tool has ever emitted silently lost the words
  \`| grep -q\`, \`tr '\n' ' '\` and \`episodic.py\` — leaving holes mid-sentence in the paragraph
  whose whole subject is a summary that dropped the governing detail. **The generator was committing
  its own described failure, in the text describing it.** The two visible \`command not found\`
  lines on stderr were the only tell, and stderr is not where anyone reads a brief.
  ⛔ **KEEP EVERY LITERAL BACKTICK IN THIS HEREDOC ESCAPED (\\\`).** The delimiter cannot simply be
  quoted instead — \$LANE and \$PATHS above are real interpolations this brief depends on.

── AND WRITE IT DOWN: project_memory/lane_log.md ───────────────────────────────
  The message is how TODAY's lanes stop. The log is how TOMORROW's session inherits it.
  A BLOCKER GETS BOTH. ⛔ Logging is NOT stopping.

  ## YYYY-MM-DD HH:MM · $LANE · <BLOCKING | observation>
  WHAT I SAW: <the thing, with file:line, checked against disk>
  WHY IT MATTERS: <what it would change, and for whom>
  WHAT I DID: <kept building on X | stopped and messaged the seat>

  ⛔ ONE BLOCK = ONE ATOMIC WRITE. Never build a block incrementally — every lane writes this file
     by design, so a path-scoped commit sweeps whatever anyone else has half-appended.
  ⛔ YOU RECORD AN OBSERVATION. ONLY THE SEAT WRITES A RULING. If your block contains "so we should"
     or "this is now", rewrite it as what you saw. You write NO design document at all.

── COMMITTING (the tree AND the index are shared) ──────────────────────────────
  ▶ THE GUARD COMMITS FOR YOU, GATED, AND IT CARRIES YOUR IDENTITY ITSELF:
      tools/verify_lane.sh $LANE --commit -m "msg" <your paths>

    ⚡ \`--commit\` exports \`LEVAIN_LANE\` from the lane you named in argv into the \`git commit\` child,
    so the pre-commit hook grades you as who you SAID you are. For a hand-typed bare commit,
    prefix it:  LEVAIN_LANE=$LANE git commit -- <your paths>

    ⛔⛔ DO NOT \`echo $LANE > .levain-lane\` WHEN OTHER LANES ARE LIVE. That was this brief's own
    instruction until 2026-08-17 and it is WRONG AT N>1: the file is ONE slot at the repo root and
    every lane shares the worktree, so last writer wins and the others are graded against a
    stranger's row. Both directions were reproduced on a throwaway worktree and only ONE is loud —
    your own file under a stranger's name is REFUSED, but a file owned by *whoever the slot names*
    PASSES, printing \`ok\`. ⚠ If you find the slot already wrong, do NOT write your own name in:
    that makes YOU the last writer and breaks everyone else. The slot is the bug; do not fight
    over it.

  ▶ ⚡⚡ EXERCISE THE GATE IN ITS REFUSING DIRECTION BEFORE YOU HAVE ANYTHING TO LOSE.
    Run it against a real path with NO \`--commit\`, in your first minutes:
      tools/verify_lane.sh $LANE <a path you own>      # and one you do NOT own
    ⚖ Learned 2026-08-17 from two lanes on the same afternoon, and the contrast is the whole point:
    one met the guard for the first time AT its first commit and lost a cycle to a refusal it had
    to diagnose under pressure; the other had probed it early, hit the same class of refusal when
    stopping was FREE, and paid nothing. **A gate you first meet at your first commit teaches you
    its failure modes at the most expensive moment available.**
    ⚠ Refusal text can mislead — a path-form mismatch reports "a hole in LANES.tsv", which reads as
    a grant problem the seat must fix. Learn which is which while it costs nothing.

  By hand, if you must:
      1. ⛔ NEVER \`git add <path>\`     — the index is shared; your add lands in everyone's guard.
      2. NEW file → \`git add -N <path>\` — intent-to-add, then commit.
      3. \`git commit -m "..." -- <your paths>\`  ⚠ options BEFORE the \`--\`, never after.
      4. ⛔ NEVER \`git commit -a\`      — it sweeps other lanes' \`-N\` entries into your commit.

  ⚡ A NEW FILE IS ALWAYS UNOWNED. Ask the seat for a grant BEFORE the commit. The pre-commit hook
     will refuse it otherwise — that refusal is the protocol, not a bug.
     (The hook installs itself the first time you run \`verify_lane.sh\` in a clone. Nothing to do.)

── BEFORE YOU HAND ANYTHING TO PHILL ───────────────────────────────────────────
  ⛔ AN INSTALL BUILDS WHATEVER IS IN THE SHARED TREE — including other lanes' uncommitted,
     ungraded work, under the name of the commit at HEAD. Anything the operator GRADES is built
     from a CLEAN WORKTREE at a named SHA:
       git worktree add -q --detach /tmp/levain_clean <sha>
       cd /tmp/levain_clean && git status --short     # must be EMPTY before you build
     It disturbs no lane — no stash, no checkout in the shared tree.

  ⛔⛔ AND FOR ANYTHING TOUCHING \`doctor\`, \`init\` OR THE ACTIVATION LAYER, A GREEN SUITE IS NOT
     EVIDENCE. Doctor has gone RED FOR A WHOLE OPERATOR CLASS THREE TIMES (0.4.0 codex, 0.4.2's
     first draft, 0.4.3) and the suite was green for all three. The ONLY thing that has ever
     caught it is running \`levain doctor\` against a REAL INSTALL OF EACH ADAPTER, in an
     ISOLATED \$HOME — \`levain init --adapter codex\` writes \`~/.codex/hooks.json\` and will
     clobber the operator's real wiring if HOME leaks:
       ( unset CLAUDE_CONFIG_DIR LEVAIN_SCOPE; HOME=/tmp/lev_probe PYTHONPATH=\$PWD \
         python3 -m levain init --path /tmp/lev_probe/e --adapter claude-code --answers A.json )
     Falsify against a CONTROL: run it with your change and without it, on the same install.

── READ THESE ──────────────────────────────────────────────────────────────────
  1. project_memory/lane_log.md         its header is the lane protocol
  2. ~/Briefcase/flow/projects/levain/next.md   the keystone tracker ("▶ YOU ARE HERE")
  3. project_memory/diogenes_*.md       the carried findings for any file you touch
  Full protocol: ~/Briefcase/flow/FLOW_DEV_PROTOCOL.md → "## Parallel Lanes"

── YOUR SCOPE ──────────────────────────────────────────────────────────────────
  ⚠ THE SEAT FILLS THIS IN BY HAND, FROM \`next_steps.md\` — and checks each file it names against
    the paths above before sending. A stale plan table once assigned a lane a file the map gives to
    another lane, and the LANE caught it. The map is the authority; the plan is not.

  [ ... ]

════════════════════════════════════════════════════════════════════════════════
BRIEF

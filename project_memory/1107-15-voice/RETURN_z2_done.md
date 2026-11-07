# RETURN: PATHZERO Z1/Z2 DONE line, live run (Sat 11-07)

Observations only. Nothing worked around; no authority/grant state edited. Tool: /Users/phillipclapham/Briefcase/flow/venv/bin/levain (`L`), D=/Users/phillipclapham/Briefcase/flow/state/bridge/cockpit. Origin/main = 649b82f6263b40b4957290215217249f15279973.
Note: `--data-root` is a per-subcommand flag (after `seat lease list`), not global; first attempt with it before `seat` gave `invalid choice` (argparse error, rc=2, no state touched).

## (1) lease + egress live: PASS
`L seat lease list --data-root $D` (rc=0):
`sl-44a87da62b28  lanes /Users/phillipclapham/Briefcase/.seat-lanes  returns /Users/phillipclapham/Briefcase/.seat-returns  until 2026-11-21T17:36:37.662422Z`
`L seat egress list --data-root $D` (rc=0):
`se-4cd96fbf807a  lanes /Users/phillipclapham/Briefcase/.seat-lanes  remote https://github.com/levainhq/levain.git  domains -  until 2026-11-21T17:36:48.788356Z`

## (2) launch z2-done-1107, push with zero dialogs: FAIL (launch refused; never started)
`L seat launch z2-done-1107 --remote https://github.com/levainhq/levain.git --base 649b82f6263b40b4957290215217249f15279973 --prompt "Create a file NOTE.txt containing one line of text, then git add and git commit it on the current branch. Do nothing else." --dry-run --data-root $D` (rc=1), verbatim:
`seat launch refused: the launcher key is missing: the lease cannot be used; sign a new one`
Per instructions: dry-run refused, so no real launch was attempted; step stopped. No lane/RETURN dir minted (`.seat-lanes` and `.seat-returns` both empty).
Push evidence: `git ls-remote https://github.com/levainhq/levain.git 'refs/heads/seat/z2-done-1107'` -> empty output, rc=0 (branch does not exist). seat_egress.jsonl not checked: no seat ran. No dialogs occurred (no seat).
Reading: the lease row is live in the journal but the launcher key it was signed against is not found where launch looks. Not diagnosed further (that would mean touching authority/key state). Possibly the lease was signed under a different key dir / --data-root / --path than this invocation resolves; the desk should say which install dir Phill's signing used.

## (3) canary: FAIL (refused before any probe ran; no usage spent)
Attempt 1: `L seat canary --claude-bin claude` (rc=1), tail: `levain.seat_profile.SeatRefusal: the claude binary is not an absolute path: 'claude'` (my input error).
Attempt 2: `L seat canary --claude-bin /Users/phillipclapham/.local/bin/claude` (rc=1; symlink -> /Users/phillipclapham/.local/share/claude/versions/2.1.319), tail verbatim:
`levain.seat_profile.SeatRefusal: /Library/Application Support/ClaudeCode/managed-settings.json: PreToolUse hook is not in the operator's trust pin (run: python -m levain.host_guard trust-managed): 'G="/Library/Application Support/ClaudeCode/flow-guard/sandbox_guard.py"; if [ -r "$G" ]; then exec /usr/bin/python3 -I "$G" hook; else echo "flow sandbox guard missing: $G" >&2; exit 0; fi'`
I did NOT run `host_guard trust-managed` (it edits operator trust state, Phill's call). Both profile builds refuse on this laptop's managed-settings flow-guard hook, so the canary (and any real launch under this profile) is blocked until he pins it or the managed hook is reconciled.

## Branch cleanup
Docs (seat-profile.md) say nothing about deleting test branches. No branch was created. Nothing to delete.

## Blockers for Phill / desk
1. Launcher key missing for lease sl-44a87da62b28 (message above).
2. Managed-settings PreToolUse flow-guard hook not in the host_guard trust pin (`python -m levain.host_guard trust-managed`, his to run).

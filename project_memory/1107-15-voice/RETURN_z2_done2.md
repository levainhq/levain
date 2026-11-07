# RETURN: PATHZERO Z2 DONE line, rerun (Sat 11-07)

Observations only. No merge, release, signing, authority/grant edit, or `host_guard trust-managed`.
Tool: /Users/phillipclapham/Briefcase/flow/venv/bin/levain. Data root: /Users/phillipclapham/Briefcase/flow/state/bridge/cockpit.
Origin/main (git ls-remote, this run) = 649b82f6263b40b4957290215217249f15279973.

## Step 1: dry-run with the corrected install path
Command (rc=1):
`levain seat launch z2-done-1107 --remote https://github.com/levainhq/levain.git --base 649b82f6263b40b4957290215217249f15279973 --prompt "Create a file NOTE.txt containing one line of text, then git add and git commit it on the current branch. Do nothing else." --dry-run --path /Users/phillipclapham --data-root <cockpit root>`

Output verbatim:
`seat launch refused: /Library/Application Support/ClaudeCode/managed-settings.json: PreToolUse hook is not in the operator's trust pin (run: python -m levain.host_guard trust-managed): 'G="/Library/Application Support/ClaudeCode/flow-guard/sandbox_guard.py"; if [ -r "$G" ]; then exec /usr/bin/python3 -I "$G" hook; else echo "flow sandbox guard missing: $G" >&2; exit 0; fi'`

## Diagnosis confirmed
The earlier "launcher key is missing" refusal is GONE with `--path /Users/phillipclapham`: the key lookup now resolves and launch proceeds to the next gate, the managed-settings trust pin. So the --path diagnosis was right.

## Stopped per brief (item 2)
The managed-settings PreToolUse flow-guard hook is not in the operator's trust pin. That pin is Phill's to run (`python -m levain.host_guard trust-managed`). I did not run it. No real launch was attempted, no lane/RETURN dir minted, no seat ran.

## DONE clauses
- Lane pushes seat/* branch: FAIL (not run; blocked at the trust pin). `git ls-remote ... refs/heads/seat/z2-done-1107` not checked, no seat ran; branch cannot exist from this run.
- Zero permission dialogs: NOT MEASURED (no seat ran; dialog count would have been read from the seat's log in its RETURN dir and seat_egress.jsonl).
- Test branch left in place: none created.

## Blocker for Phill
Run `python -m levain.host_guard trust-managed` (or reconcile the managed hook), then rerun the same launch with `--path /Users/phillipclapham`.

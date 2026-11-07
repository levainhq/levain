# RETURN S7 END 3 (levain lane, 11-07 ~15:15-15:30 local, guest clock UTC 20:1x-20:2x)

Tree: origin/main 6924d8d9 (instrumented by the rig: `vend_instr.py` appended to `levain/slot/start.py`, the same measurement hook every earlier END lane used).
Raw root: `project_memory/1107-15-voice/s7end3_results/` (call it R). Nothing pushed, no product code edited, vend-real untouched.

## The headline
The generation under test was written by the PRODUCT (`install-helper setup`, 613 steps, SETUP_RC=0), not placed by hand. Installed `current -> gen-43d3c6659940ed76`, install-shape `generation: gen-43d3c6659940ed76`, `completed_txn bae39bd5...` (`R/gen_proof.txt`). `diff -rq` of the staged instrumented tree vs `/usr/local/lib/levain-root/current/levain`: only `__pycache__` differs; h11 identical; the installed start.py carries the instrumentation, the clean copy does not.

## Start / end checks
- Before: `tart list` 9 VMs (vend-real-7a6895a7 running, 8 stopped); `uptime` load 7.5-8.3 on 10 cores, top consumers single-thread Pythons plus a restic burst (no saturation consumer from my side). During runs load 9.8-17 (two VMs + other lanes).
- One clone only: `vend-s7e` of vend-real-7a6895a7, stopped, deleted by exact name. After: `tart list` back to the same 9 VMs (vend-real still running). `R/vend-s7e/up` (6 MB tarball) and the askpass file removed from R.

## Why `repair --source` was not the route (the exact gap, reproduced)
`sudo /usr/local/libexec/levain/install-helper repair --source /Users/admin/lvsrc/levain` on the clone: `refused account-missing` for `_levain_s0..s7, gw0..7, v0, c0, v1, c1` (`R/repair_out.txt`, `R/repair_err.txt`).
Cause, read in the guest: those accounts exist but carry the label `Levain session account (retired)` (dscl RealName); `L/slots/e0..e7` directories survive with no `state` file, and repair's `_repair_slots` still counts them as slots in service, so `_check_rendered_accounts` demands live accounts for them. The image went through undo+setup over an older install and the old slot dirs stayed. The refusal's own remedy ("run undo, then setup") is what I did (below). Observation only: whether repair should ignore slot dirs whose account is retired is a product question, not decided here. Same refusal as 1107-57 and 1106-16 lane 55.

## What I ran (rig `vmend.py` from 1105-17, on the clone)
1. clone, boot, `stage --rev origin/main` (instrumented), copy the staged tree to `/Users/admin/lvsrc`.
2. Saved the two live credential files (root-only copy inside the clone), then `install --slots 4` = fixsudo, `undo`, `setup` (product-written generation above). New slots e12..e15, v3.
3. Restored the saved Claude credential + identity seed + baseline into `e12` and the Codex auth + baseline into `e13` (chown to the new slot accounts); deleted the saved copies. This restore is a rig act (a credential cannot be product-written without Phill's login); it is NOT the generation.
4. `seed --mode real`; X1 `session` (codex); C1p/C1s/C1pong `run --mode claude --slot 12 --production-blob`.

## Per-row results
| Row | Verdict | Evidence |
|---|---|---|
| X1 real Codex END, instrumented tree step in the product-written generation | **PASS: 16 PASS, 0 FAIL, 0 AMBER, 2 INFO** | `R/verdict_X1p.txt`, `R/session_X1p.txt`, raw `R/vend-s7e/X1p/`. Session s-7925c502b72e62b6: R.1 exit 0; R.2 rollout jsonl; R.4 README probe line in retained repo; R.5 commit `8327b9a vmend-probe` in retained repo (AMBER R.5 from before does not recur with Codex); L.1 354 deny lines, 0 under var/folders; S.1 END RETAINED (`tunnel_bytes 307919150`); S.2 raised nothing; S.3 tree_step.entry 1; S.4 S.5 S.6; K.1 folder holds exactly `0 C T X`; K.2 sunlnk flags 0x100000 on folder and kids; K.3 K.6. |
| 39 / 31 / 38 real Claude END (haiku, production blob), run C1p | **PASS on END rows; 14 PASS, 0 FAIL, 2 AMBER (R.4, R.5)** | `R/verdict_C1p.txt`, raw `R/vend-s7e/C1p/`. S.1 RETAINED (`tunnel_bytes 601371`), S.2, S.3 (entry 1), S.4-S.6, K.1 `0 C T X`, K.2, K.3, K.6, L.1 (244 deny lines, 0 under var/folders), P.1 all PASS. R.4/R.5 AMBER: haiku bundled echo+git add+git commit into one Bash call; the production blob held it for approval and the non-interactive dispatch has no approver (stream-json `permission_denied`, in `observe.json` `vendor_out`). Repo unchanged. A vendor-behaviour/approval-policy result, not an END result. |
| same, sonnet, run C1s | **PASS on END rows; 15 PASS, 0 FAIL, 1 AMBER (R.5)** | `R/verdict_C1s.txt`, raw `R/vend-s7e/C1s/`. S.1 RETAINED (`tunnel_bytes 4357885`), S.2, S.3 entry 1, K.1, K.2, L.1 (234 deny lines, 0 under var/folders), R.4 PASS (probe line landed). R.5 AMBER: `echo >> README.md` ran, `git add README.md` came back `This command requires approval` (production blob), so no commit. So AMBER R.5 is the production blob gating `git add`/`git commit` in an unattended dispatch, in both Claude runs; with real Codex interactive (X1) the commit lands. |
| row 7 "pong" text read-back (vendor reply) | **PASS for the read-back half** | `R/pong_readback.txt`, `R/vend-s7e/C1pong/`: brief "Reply with exactly the word: pong. Use no tools.", haiku, production blob, session s-e9bee2b996ca36e4; stream-json result text `'pong'`, is_error False, cost 0.0014 USD. END on that run: verdict `R/verdict_C1pong.txt` 14 PASS 0 FAIL 2 AMBER (R.4/R.5 are probe-brief checks that do not apply to a no-tools brief). |
| row 7 `levain agents show <scratch> --json` with ONE product-written `done` | **NOT RUN** | The `done` row comes from the 1105-36 fire rig's `rig.finish` (1106-16 row103 r7). I did not rebuild that rig (tests tree, pytest, cryptography, new world) on this clone. UNVERIFIED whether the product itself writes `done` from the vendor's exit. |
| Z0 U5 gh half / Z2b VM half | **SKIPPED** | need the throwaway GitHub token; none in play. |

## Residue / UNVERIFIED
- **The Claude and Codex credentials in the clone are copies.** The runs used them; I did not run a sign-in check against vend-real or Phill's accounts afterwards (no `check_signins`; vend-real was not touched). 1107-57 found the Claude token revoked after its run; whether this run moved it is unknown. Re-check before relying on the slot.
- `tunnel_bytes 307919150` (~308 MB) for a short scripted Codex session, vs 601k / 4.4M for Claude: unexplained again (also 307 MB in 1107-57). Not investigated.
- The instrumentation (rig `vend_instr`) is inside the generation the product wrote; a clean production tree has not been END-verified by this rig (S.3 `tree_step.entry` needs the points).
- Setup renumbered slots (e12/e13), so e8/e9 labels in older RETURNs map to e12/e13 here.
- The ~31 s HELD-retry shape did not occur (S.3 entry count 1 on all four runs).
- The undo+setup replaced vend-s7e's old slot state; vend-real itself still carries the stale e0..e7 shape.

## Files
`R/verdict_{X1p,C1p,C1s,C1pong}.txt` · `R/session_X1p.txt` · `R/run_{C1p,C1s,C1pong}.txt` · `R/repair_{out,err}.txt` · `R/gen_proof.txt` · `R/pong_readback.txt` · `R/vend-s7e/{X1p,C1p,C1s,C1pong}/` (observe.json, points, denials, profile) · `R/vend-s7e/host_logs/` (every host command, incl. the 613-step setup).

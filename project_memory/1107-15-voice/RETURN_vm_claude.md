# RETURN VM Claude reruns (11-07 ~12:4x local, guest UTC 17:4x)

Observations only, no rulings. R = `/Users/phillipclapham/Briefcase/.levain-1107-15/project_memory/1107-15-voice/vm_results/` (call it R). VM `vend-real-7a6895a7`. Nothing pushed, merged, signed or deleted; `tart list` after: same 9 VMs, same states (this one running).

## Checks
- `check_signins.sh` before: `claude slot e8: credentials nonempty=yes baseline=yes`, `codex slot e9: auth.json nonempty=yes baseline=yes`, `claude -p probe answered: ok`, `codex login status: Logged in using ChatGPT`, `RESULT: both signed in (probes passed)`. Same RESULT again after both runs.
- Load: `uptime` first read 4.98 (10 cores), a spike to 10.4 then; `ps -Ao %cpu,comm -r` top consumer 16% (decay, nothing saturating); ran anyway.
- Tree under test: guest gen `gen-0251acf027f404fa` (lane 57's hand-placed instrumented generation, `diff -rq` vs `gen-99c06e6bc767a574` = only `levain/slot/start.py`). `current` symlinked to it for each run and back to `gen-99c06e6bc767a574` after (confirmed by `ls -l`). Hand placement, not `repair`/`setup`; the earlier caveat stands.

## Rows chosen
Waiting on a working Claude slot, from RETURN_s7_end2 + RETURN_s7_end: (a) real Claude model reply through the product dispatch path (C1 family, rows 39/31/38 real Claude END with the instrumented tree step), (b) X1 for Claude (scripted interactive session, END). Row 7 and fakehello were not Claude-slot-dependent (lane 55 / 57 ran them); not re-run.

## Results
| Row | Verdict | Verdict file | Raw line |
|---|---|---|---|
| C1 real model reply, product dispatch, slot e8, haiku (run label C1v, session s-790460ccaf5567aa) | PASS | `R/verdict_C1v_stdout.txt` | `15 PASS, 0 FAIL, 1 AMBER (read these), 2 INFO` ; `PASS R.2 claude's stream-json ends in a successful result` (stop_reason end_turn, total_cost_usd 0.00304, 1271 output tokens) |
| 39/31/38 END tree step, real Claude, instrumented runtime (same C1v run) | PASS | same | `PASS S.1 teardown.exit ans: {'state': 'RETAINED', 'tunnel_bytes': 708352}`; `S.2 tree_step.exc count 0`; `S.3 tree_step.entry count 1`; `K.1 entries ['0','C','T','X']`; `K.2 flags 0x100000 on folder and kids`; `K.3 K.6 S.4 S.5 S.6 P.1 PASS` |
| 53 var-folders denials (C1v) | PASS (no control re-run, so L.1 alone proves little) | same | `PASS L.1 292 deny line(s) in all, none there` |
| C1v R.4 README probe line in retained repo | PASS | same | `PASS R.4 ... README.md has the probe line` |
| C1v R.5 commit in retained repo | AMBER | same | `AMBER R.5 ... 1abd5cd rig` (the stand-in commit, no new commit; same shape as before) |
| X1 for Claude (scripted typist, `--vendor claude`, label X1c, session s-7c1a54077841a5b3) | AMBER (verdict kind X1 is codex-shaped: 1 FAIL is the codex-only check R.2; the rest pass) | `R/verdict_X1c.txt`, session `R/session_X1c.txt` | `15 PASS, 1 FAIL, 0 AMBER (read these), 2 INFO`; `FAIL R.2 codex wrote its session log (it really ran a conversation) :: []`; `PASS R.1 ... levain: claude exited 0 ... ended RETAINED`; `PASS S.1 {'state': 'RETAINED', 'tunnel_bytes': 918971}`; `PASS R.4`, `PASS R.5 fdd318d vmend-probe`, `S.3 count 1`, `K.1 K.2 PASS` |

Notes: R.2 in the X1 kind looks for a codex rollout log, so it cannot pass for a Claude session; I did not check where the Claude session jsonl landed or run a Claude-specific R.2 check. Raw dirs: `R/vend-real-7a6895a7/C1v/`, `R/vend-real-7a6895a7/X1c/`. X1c ran after C1v, both on the same slot e8; both sign-in checks read signed in at the end.

## Not run / needs Phill's hands
- Z0 U5 gh half and Z2b VM half: not run (no throwaway GitHub token).
- Row 7 `done` read-back of the vendor's text ("pong") and product-written `done` vs rig `finish`: not run (not a Claude-slot item; lane 55's clone is gone).
- Residue: two more RETAINED test records (s-790460ccaf5567aa, s-7c1a54077841a5b3) under `/private/var/levain/retained` in the guest, plus the earlier extra generation dir and `/Users/admin/lvsrc` left by lane 57.

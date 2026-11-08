# RETURN driver (3b-8a): real VM adapters, id journaled before the vendor runs, one clean fresh-VM run

Worktree `.levain-1107-20-driver`, branch `seat/1107-20-driver` off origin/main 6924d8d9. Committed, not pushed.

## Built
- `scripts/vm_harness/driver.py`: `VmAdapters` (alloc / mint / render / revoke / slot_check / lister / resolve against a clone VM, guest and spawn injected) and `run_on_vm`. Seam order reshaped to the runtime's: `intent` journaled before `prepare`; `mint` = `prepare` (the id is born only there); the id is fsynced into the journal by `start_session`; only then does `render` write the ack that lets the guest run `render`+`start` (the vendor). Docstring updated.
- `scripts/vm_harness/guest_dispatch.py` (guest, `_levain_dispatch`): the product's RealLauncher with a rendezvous at the `render` verb (writes `<tag>.prepared.json`, blocks on `<tag>.journaled`, 60 s, refuses to render without it). `guest_ops.py` (guest, root): slotstate / slotof / revoke / resolve / put / get. `vm_run.py` (host CLI; refuses a VM not named `vend-*`).
- `cleanup.py` unchanged (its `start_session` already journals between mint and render).
- Framing: `driver_FRAME.md`.

## Run (the DONE line)
Fresh clone `vend-drv2` of p72-vm at origin/main 6924d8d9 (vmend rig: clone, boot, stage, install 4 slots, seed fake). `vm_run.py` -> **RESULT: PASS** (`driver_vm/RESULT.txt`), journal `driver_vm/cleanup.jsonl` (intent line, then the session line), uid scan of `_levain_gw8` clean, slot e8 reusable after, REVOKE of the real id accepted. Guest timeline (`guest_fh_listing.txt`): prepared 20:12:03, ack 20:12:04, vendor released 20:12:05, final 20:12:35, so the vendor ran only after the ack. Clone stopped and deleted; `tart list` before/after: the same 9 VMs (`tart_list_*.txt`; vend-real-7a6895a7 untouched and running). Free disk 52 GiB. Load was 7-8 from other lanes throughout (not saturation by a single consumer).

## Tests
`tests/test_vm_harness_adapters.py` 8 new; with `test_vm_harness_driver.py` and `test_vm_harness_cleanup.py`: 67 passed. Reverts that turn red (mutation-checked): `start_session` journals after render -> `test_id_is_journaled_before_the_vendor_runs`; remove the slot-mismatch check -> `test_slot_mismatch_blocks...`; remove the not-reusable check -> `test_alloc_refuses...`; remove the empty-id check -> prepared-record test (hang/red); remove child-exited check -> silent-death test (hang/red); remove the deadline check -> timeout test (hang/red); remove the guest `error.json` read -> `test_guest_error...` (hang/red); remove `adapters.close()` -> `terminated` assertion red. The "hang" ones are infinite loops under a fake sleep, killed by a 25 s subprocess timeout and counted red.

## Residue
- A crash INSIDE `prepare` (id born, reply lost) is still the documented ambiguous edge; backstops are the uid scan and the slot check. Not exercised.
- Rig stand-ins, same two as the 1105-17 hold rig: render blob names no hooks; workspace seam given `stage.make_bundle`. Production wiring of both is not what ran.
- Rig defects worked around by hand, not fixed (they live in the 1105-17 rig, outside this repo): `seed` writes no `keep/identity-seed.json` (I ran `1106-16/vm_driver/g_seed_identity.py`); the rig ships an old `fire_fakeclaude.c` (I copied main's `tests/vm_fakes/` file over it before seeding). A rig-fix lane is still owed.
- Fake vendor only; ran once (no repeat-run statistic). Whether this discharges whole row 9 is Phill's/the board's call; it discharges the journal-before-vendor ordering and a clean fresh-VM RESULT PASS.

## UNVERIFIED
- The prior-art URLs in `driver_FRAME.md` were cited from knowledge, not re-fetched.
- `guest_ops.py slotof` relies on the slot state file naming the session while it is live (it did, in this run); no test of the guest scripts exists beyond the VM run.
- A second run on a different slot, or a run with a live REVOKE of a still-running vendor, was not done.

---
# L3 r1 fix round (commit 54fdfc4a on top of 66b5f465)

Four reviewers (complement, codex, deepseek-openrouter, gemini@3.8-flash-low), verdict per finding, resolved against disk. Tests: `tests/test_vm_harness_adapters.py` now 33; with the driver and cleanup tests 92 passed. Mutation-checked (each revert turns >=1 test red, all 12 run): final-poll check (a), nonce -> constant (b), fhclean leftover check (b), SESSION_RE check (e), `shlex.join` (e), put O_EXCL|O_NOFOLLOW (f), get O_NOFOLLOW (f), claim stale check, prepare-time publish (g), `egress=False` (d), close cancel loop (j), strict slotof (c).

| # | Finding (who) | Verdict | Fix / test |
|---|---|---|---|
| a | vendor exit 2 / GATE_UNPROVEN / poll None reported PASS (codex 5, deepseek 2, complement 7) | CONFIRMED, fail-open | `VmAdapters.render` reads the final record and fails unless `poll == 0` and `session` matches; `test_a_vendor_that_did_not_exit_cleanly_is_not_a_pass` (3 cases) |
| b | stale rendezvous files, tags restart at drv8x1 (all four but gemini) | CONFIRMED | per-run uuid nonce in the tag; `alloc` runs `guest_ops fhclean` (drv-pattern entries only; brief.txt, knobs and the repo checkout stay) and refuses if any survive; `guest_dispatch.claim()` refuses any pre-existing `<tag>.*` and takes `<tag>.pid` O_EXCL. Tests: `test_tags_are_unique...`, `test_stale_rendezvous_entries...`, `test_a_stale_ack_or_final...`, `test_a_fresh_tag_is_claimed_once` |
| c | slot validation by substring (codex 6, complement 9, deepseek 4, gemini 1) | CONFIRMED | `slotstate`/`slotof` use `levain.slot.records.parse_slot_state`, check the directory name equals the record's own slot tag, match `state == leased` and `session` exactly; any malformed record exits 4 (fail closed). Tests: `test_slotof_is_the_strict...`, `test_a_malformed_slot_record...`, `test_a_record_in_the_wrong_directory...` |
| d | ticket-routes mismatch (codex 1) | CONFIRMED against `HelperSlotRuntime._render_blob` | `pick_routes(..., egress=False)`; `test_the_render_blob_lists_no_egress_route...` (a unit pin, not a product ticket-validation run: the fresh VM run is the integration proof) |
| e | session id into a remote shell argv (complement 2, deepseek 3, codex 7) | CONFIRMED | host: mint refuses a session not matching `s-[0-9a-f]{16}` before it is stored or journaled; `ops` refuses any argv token outside `[A-Za-z0-9_][A-Za-z0-9._-]*`; `vm_run` sends ONE `shlex.join`ed command to ssh. Tests: `test_a_malformed_session_id_never_reaches...`, `test_ops_refuses...`, `test_ssh_sends_one_quoted_command` |
| f | root put/get follow symlinks in a dispatch-owned dir (complement 3, codex 3) | CONFIRMED | guest_ops opens the dir with O_NOFOLLOW|O_DIRECTORY and every file by dir fd: put O_EXCL|O_NOFOLLOW (exit 6 if present), get O_NOFOLLOW + fstat regular + 1 MiB cap (exit 5 if not), names `[A-Za-z0-9][A-Za-z0-9._-]*`. Tests: `test_put_refuses_a_planted_symlink...`, `test_get_refuses_a_symlink_and_a_fifo...`. Not done: a root-owned exchange dir (the ack is still creatable by the dispatch user; with unique tags + O_EXCL a pre-planted ack for an unknown tag is the only residue) |
| g | session not published until render (codex 4, gemini 4 in part) | CONFIRMED | `RendezvousRuntime._run("prepare")` publishes `prepared` (session + slot) the moment prepare returns, before staging/bundle; the ack gate stays at `render`. `test_the_session_is_published_when_prepare_returns...` |
| h | alloc/slot mismatch, actual slot not carried (codex 8) | CONFIRMED | prepared carries `slot` (from `prepare_answer`); render requires slotof == prepared slot AND == plan; `slot_check` also checks the actual slot. Tests: `test_the_actual_slot_prepare_reports...`, `test_slot_check_also_checks...` |
| i | `_get` swallows ssh/sudo failure; probe unbounded (codex 9, complement 6, deepseek 7) | CONFIRMED | only guest rc 3 is "absent" (`GuestError.rc`); any other failure or timeout is an AdapterError at once; each probe timeout = min(20 s, remaining deadline), floor 1 s. Tests: `test_a_non_absent_guest_failure_fails_fast...`, `test_every_wait_probe_is_bounded...` |
| j | close() does not wait/kill/cancel (complement 4, codex 10, deepseek 6, gemini 3) | CONFIRMED | close(): `guest_ops cancel <tag>` for EVERY spawned tag (pid from `<tag>.pid`, checked against `ps` for guest_dispatch.py + tag, then SIGTERM), then terminate -> wait 5 -> kill -> wait. A minted-failed tag is cancelled too. `test_close_cancels_every_remote_dispatcher...` |
| 11 | rendezvous JSON torn in place (codex 11) | CONFIRMED, cheap | `say()` writes temp, fsync, `os.replace`. Untested (guest code needing no unit), covered by the VM run |
| - | vm_run `vend-` prefix guard (complement 8, deepseek 9) | CONFIRMED, low | `re.fullmatch(vend-[A-Za-z0-9_][A-Za-z0-9._-]*)`; `ip()` cached and empty-output guarded (deepseek 8); TART path overridable by `TART_BIN` |
| - | deepseek 5 / gemini 4 (args[0] assumed to be the session on render; prepared written only at render entry) | REFUTED as stated, superseded | `HelperSlotRuntime.start` calls `self._run("render", session, ...)` (dispatch_runtime.py:287) so args[0] is the session; moot now since the id comes from `prepare`'s own answer via `prepare_answer` |
| - | gemini 2 (askpass dir deleted while children run; hardcoded TART) | REFUTED | `run_on_vm` finishes (close() reaps children) inside the `with tempfile.TemporaryDirectory()`; TART override added anyway |
| - | gemini 5 (hardcoded CLT git path) | REFUTED | the path is the guest image's fixed CLT location (the rig that installs it), not a portable-host assumption; same constant the 1105-17 hold rig used |
| - | gemini 6 (`assert name == "final"`) | FIXED by removal | the assert is gone with the payload check |
| - | complement 8 / deepseek: spawn stderr to DEVNULL | NOT DONE | ssh failures during a spawned dispatch still leave no diagnostics (the guest `error.json` and exit code do); named residue |

## Fresh-VM re-run (the budget allowed it)
`vend-drv3` cloned from p72-vm, staged at 54fdfc4a, 4 slots installed, seeded fake (the same rig, with `g_seed_identity.py` for e8 and the two guest scripts copied to /tmp/fire). TWO consecutive `vm_run` invocations on the same VM, same slot: **both RESULT: PASS** (`driver_vm_r1fix/RESULT_run1.txt`, `RESULT_run2.txt`; journals `cleanup_run*.jsonl`). The second run is the stale-file proof: after it the exchange dir holds only run 2's tag (`guest_fh_after_run2.txt`), run 1's files were cleaned and tags differ. The final record passed the new strict check (poll 0 and the session id equal to prepare's). Clone stopped and deleted by exact name; `tart list` back to the same 9 VMs (`tart_list_after.txt`); vend-real-7a6895a7 untouched and running.

## Residue / UNVERIFIED (this round)
- The seeded rig still bakes the OLD `fire_fakeclaude.c` (I did not recompile it this time); the fake vendor exited 0 anyway, so (a) is exercised on the pass side only. The fail side (exit 2, None) is unit-tested with the fake guest, NOT run in a VM.
- `cancel` is unit-tested only against the host fake; its guest half (pid file read, `ps` check, SIGTERM) ran in the VM as a no-op path (finished dispatcher: `close()` found a dead pid or an absent pid file). A hung-dispatcher cancel was not run.
- The guest `claim()` and `say()` ran in the VM; their refusal paths are unit tests with a temp dir.
- A pre-planted ack for an unknown tag by the dispatch user is still possible (no root-owned exchange dir); nothing the host does reads it.

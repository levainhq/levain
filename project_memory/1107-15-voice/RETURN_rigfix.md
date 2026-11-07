# RETURN rigfix: the 1105-17 VM rig's two hand-patched defects

Worktree `.levain-1107-20-rigfix`, branch `seat/1107-20-rigfix` off origin/main 6924d8d9. Committed, not pushed. No VM run.

## Where the rig lives (the catch)
`project_memory/1105-17/vmend/` in the main levain checkout is GITIGNORED (public repo; project memory is untracked), so a
commit on a branch cannot carry an edit to it. Fix shape: the logic is tracked under `scripts/vm_harness/`, and a patch wires
the untracked rig to it.

## Built
- `scripts/vm_harness/rig_seed.py` (guest, root): `write_identity_seed(store, tag)` writes `slots/<tag>/keep/identity-seed.json`
  via the product's own `levain.slot.end.identity_seed_bytes` (O_EXCL, no-follow, 0600, root). Same fake account the rig's
  baseline digest is made from.
- `scripts/vm_harness/rig_stage.py` (host): the rig ships NO `fire_fakeclaude.c`. `stage_rig_files` skips any copy in the rig
  dir, writes the bytes of `git show <rev>:tests/vm_fakes/fire_fakeclaude.c` (the tree being staged), adds `rig_seed.py`, and
  `check_staged` refuses a differing/missing source or a missing seed writer.
- `scripts/vm_harness/rig_1105_17.patch` (`patch -p1` from `vmend/`): `rig/g_vm.py cmd_seed` (fake mode) calls
  `rig_seed.write_identity_seed`; `vmend.py step_stage` stages through `rig_stage`. After applying, the stale
  `rig/fire_fakeclaude.c` is ignored and can be deleted.

## NOT DONE (needs the desk; I may not edit outside the worktree)
Apply the patch to the real rig: `patch -p1 -i <worktree>/scripts/vm_harness/rig_1105_17.patch` from
`levain/project_memory/1105-17/vmend/`, AFTER this branch is on levain main (vmend imports `rig_stage` from
`LEVAIN/scripts/vm_harness`). Until then the rig is unfixed.

## Tests
`tests/test_vm_harness_rig.py` (7): seed file exists in the product shape (passes `check_identity_seed`, 0600), refuses overwrite,
account matches baseline values; stage never ships a stale C file; `check_staged` red on differing / missing source / missing
seed writer; canonical comes from git; the patch applies to a copy of the real rig and wires both fixes (skips if the rig is
absent). With `test_vm_harness_driver/cleanup` and `test_vm_fake_vendor_hello`: 67 passed. Mutation not run.

## Other "placed by hand" rig steps (listed, not built)
From RETURN_s7end3 and RETURN_driver:
1. Credential restore: saved Claude/Codex credential + identity seed + baseline copied into the new slots (e12/e13) and chowned, by hand (the S7 END note calls it a rig act).
2. Instrumentation: `vend_instr.py` appended to `levain/slot/start.py` in the staged tree (so no clean production tree is END-verified).
3. Staged tree copied to `/Users/admin/lvsrc` by hand before `install`.
4. Row 7 `done` row: needs the 1105-36 `rig.finish` fire rig, not rebuilt (NOT RUN).
5. Driver lane stand-ins: render blob names no hooks; workspace seam given `stage.make_bundle`.
6. Stale slot dirs e0..e7 on vend-real make `repair --source` refuse `account-missing` (product question; undo+setup used instead).
UNVERIFIED: the patch was applied only to a temp copy; not run in a VM.

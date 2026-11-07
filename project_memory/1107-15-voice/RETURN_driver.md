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

# RETURN: dispatch wave 3 (row 27), the brokered launcher's mint and spend (S-B2 and S-B3)

Branch `seat/1107-20-wave3` off origin/main `6924d8d9`. Not pushed. Design: `project_memory/1105-49/DESIGN_brokered_launcher.md` (main checkout). S-B1 (controller `run_budget`, verify step 4) was already on main; this is the rest.

## Scope note (read first)

Phill's 11-06 answer "152a" put S-B2 and S-B3 in 1.1. The brief (row 27, READY) assigned them to this lane, so they are built as GROUNDWORK: reachable from no `levain` command (the production `build_server` still builds the passthrough launcher only). CHANGELOG says so. If the 152a ruling is meant to hold, this commit is inert and can wait.

## What was built

- `levain/agents/session.py`: `SessionLaunch.usd_micros` (default None). On no wire (`session_client._LAUNCH_KEYS` is untouched), so a fire cannot send one. (Superseded 11-08 by the finding-1 fix below: the fire now sends it, as a figure the server only compares.)
- `levain/slot/control.py`: new read-only dispatch-peer verb `ROUTE-RESOLVE {model}` -> `{route, routemap_generation, route_policy_digest}`, under the activation lock, from the ACTIVE generation. `no-brokered-route`, `route-ambiguous` (refused, never chosen), `bad-value`.
- `levain/agents/session_launcher.py`: `RealLauncher(auth="brokered")` no longer refuses `brokered-route-not-built`. It refuses by name before allocating (`no-dollar-share`, `exposure-on-a-brokered-launch`, `launch-auth-not-the-launchers`), resolves the route, mints with `reserved_microdollars = launch.usd_micros` (the hard-coded 0 is now passthrough-only), no passthrough facts. A mint refused because the map moved (`ticket-field:routemap_generation|route_policy_digest|route|model_requested`) is re-resolved and sent ONCE more; the two generation/digest names read TRANSIENT, so a later fire retakes the key. `SlotStart.auth`. `usage()` for brokered = the injected `spend(run_id)` reader, int64-bounded, only once END is proven; absent or unreadable = `usd_micros=None` = the whole reservation debited.
- `levain/slot/dispatch_runtime.py`: `ROUTE-RESOLVE` in `PORT_OPS`; `req.auth` replaces the two hard-coded `"passthrough"` (route pick, helper `prepare` argv).
- `levain/slot/services/dispatch_run.py`: `verify_point_of_use` / `replica_verifier` now RETURN the link's signed `budget_run_usd_micros` for an api_key link (None for subscription); `replica_verifier(billing=...)`; the LAUNCH op stamps it onto the launch when the launcher is brokered (`dataclasses.replace`).
- `levain/slot/dispatch_usage.py`: `lineage_spend(path, run_id)` / `lineage_spend_reader(path)`: the run's rows in `lineage.jsonl` summed (`yes` = cost, `uncertain` = max(cost, hold), `no` = 0), fail-closed to None on an unopenable sink, a symlink, a torn or over-long or non-JSON line, ANY quarantine row, a bad figure, or NO row for the run.
- CHANGELOG [Unreleased] Added line.

## Tests

- New `tests/test_agents_session_launcher_brokered.py`: 53 tests (fakes for the seams; the REAL `Control.handle` via the tickets rig for `ROUTE-RESOLVE`, the priced mint, the ceiling refusal, an activation between resolve and mint, replay idempotence).
- `tests/test_slot_dispatch_run_server.py`: +3 tests, 2 edited (`Env` now returns the verifier's figure and carries a `billing`).
- `tests/test_agents_session_launcher.py`: the two tests that pinned `brokered-route-not-built` / "no flag can make a brokered launcher mint" now pin the new refusals (`no-dollar-share`).
- `tests/test_slot_s2a_control.py`: the dispatch peer's verb table gains `ROUTE-RESOLVE`.
- Ran green before the last edits: 15 files that import the touched modules (788 passed after 3 expected reds were updated), plus the new file (53) and the run-server subset (14). The wider 75-file import-closure run is in RESIDUE below.
- Mutation check: 18 reverts, 17 killed (each test names its revert line in a `REVERT:` comment). The survivor is `entry.policy.type == "brokered"` in `_op_route_resolve`: UNREACHABLE (`routemaps` refuses any non-brokered dispatch route at submit, `routemap-invalid:body: a dispatch route must be brokered`); kept as the same belt `_check_route_facts` wears.

## Findings to route (not built)

1. **Share can exceed the reservation.** The server mints the link's FULL figure. S-B1 only requires exposure <= figure, and the design assumed equality. A fire that reserved less than the figure runs under a keys cap above what the window was debited. DORMANT today (no brokered launcher is wired), but it must be closed before wiring: either `controller._admit` demands exposure == figure on an api_key link, or the fire sends its reserved dollars and the server refuses a mismatch (the design's rejected option B, as a check not a source).
   **CLOSED 11-08 on `seat/1108-23-wave3` by option B** (option A would refuse every api_key run whose executor states less than its link signs, the dream's R included): the fire sends `usd_micros` = its reserved dollars (optional wire member), `RunServer._priced` refuses `dollars-not-the-links-share` unless it equals the link's figure; tests in `tests/test_slot_dispatch_run_server.py` (finding 1 section).
2. **A no-call brokered run settles at the whole share.** `lineage_spend` returns None for a run with no rows (it cannot tell "no calls" from "sink lags"). Safe side, costs his window the full share for a run that did nothing. A distinguishing signal (the ticket's call count, or keys' outbox drained) would fix it.
3. **A hold still outstanding at END has no lineage row yet** (the row arrives at resolution). If keys has an unresolved hold when the slot group is gone, the sum undercounts it. Needs keys' view (outstanding total) or a wait for the outbox, V1 territory.

## RESIDUE and UNVERIFIED (no instrument here holds it)

- NOT RUN: L3 review (no mesh was dispatched; this lane ran L0 and tests only). The row's "L3 clean at HIGH" is NOT met.
- NOT BUILT: the doctor row of design section 4 (per-run vs ceiling, no brokered route for model); production wiring (`build_server` builds `RealLauncher` passthrough only, no `auth` setting, no `spend=lineage_spend_reader(...)`); `SlotStart.auth` reaching the root helper's `prepare ... claude brokered` is passed but the HELPER side was not exercised (`pick_routes(.., "brokered")` for a claude vendor and `render`'s brokered path are existing code, untested here under this launcher).
- NOT HELD: keys actually booking calls under `("run", run_id)` with `cap == share`: asserted only as the journaled ticket fact keys reads (`reserved_microdollars`); `tests/test_dispatch_run_caps.py` (not in the main worktree) is where the per-call stop lives.
- The lineage sink's group-read by `_levain_dispatch` (modes, group) is not exercised: the reader is tested on a tmp file.
- V1 with his OpenRouter credential: one real provider call through a real brokered slot, settled from real usage, lineage dollars against the provider bill, an overrun stop on a real key.
- The wider import-closure test run (75 files that import the touched modules) did not finish inside this lane's foreground bound: only the 15 files listed above plus the new ones were run to completion.

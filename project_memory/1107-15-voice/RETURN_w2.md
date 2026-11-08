# RETURN w2: workspace stager W2, the snapshot's withhold bound to the signed role (levain, seat 1107-20 w2)

Branch seat/1107-20-w2 (off origin/main 6924d8d9). Committed, not pushed.

## Built
- `levain/agents/role.py`: `ceiling.withhold` (list of withhold patterns, grammar = `policy._pattern_problem`, max 256, no duplicates, stored sorted).
  Absent key is not written, so every existing role keeps its role_hash. `envelope_ceiling()` drops it before the coordinator's `narrow_scope`
  (`levain/coordinator/lease.py` _complete_spawn; without that a withhold role could not spawn a worker). `role_withhold()` reads it.
- `levain/agents/signed_role.py` (new, pure: hashlib/json/authority._canonical): `SignedRole(agent_id, spec, role)` and `signed_withhold()` re-derive
  spec_hash -> role_hash -> role.ceiling.withhold, each text its own canonical form. `withhold_problem()` is the ONE comparison both uids use
  (`withhold-loosened` = a signed pattern missing; `withhold-mismatch` = a pattern the role never declared). The two hash prefixes now live here
  (spec.py/role.py import them), so writer and verifier cannot drift.
- Wire: `LinkProof.signed` (optional, rides only a launch that stages a workspace; LAUNCH frames without a workspace are byte-identical).
  `signed_role_of(store, occurrence)` builds it on the operator side. `DispatchSessionLauncher(..., signed_of=)`; `build_launcher` passes the store's.
- Operator uid (`levain/dispatch/stager.py`, `session_client.build_workspace`): the snapshot is cut under `ws.withhold`, and `build_upload(ws, signed_withhold=)`
  refuses a member that is not the signed role's before git is touched; a client with no signed-role source builds nothing.
  `session_wire._workspace_kwargs` takes the member's withhold from the signed role (the install and repo cannot set it).
- Run server (`levain/slot/workspace.py`, `services/dispatch_run.py`): `W1_WITHHOLD_ALLOWED` is retired. A workspace LAUNCH whose proof has no evidence is
  `session_refused:workspace` `signed-role-missing`; evidence that does not derive the link's verified `spec_hash` is `signed-role-spec-hash` / `-role-hash` /
  etc.; member withhold and manifest withhold must both be the signed role's; and no manifest entry may match a signed pattern (`withheld-path-carried`),
  so the withhold is honored in the bytes, not only named.
- Loosening of the repo's own `.claude/settings*` was already refused on both uids in W1 (stager `_layer_check`, server `_clone_check` via
  `repo_loosening_refusals`); unchanged and still held by the existing tests. `levain/slot/render.py` needed NO change (the brief listed it; nothing
  there carries a withhold, the server-side verify is where the snapshot is judged).

## Tests (all green in this worktree)
tests/test_agents_role.py (+withhold: sorted/hash, absent unchanged, malformed refusals, envelope view), tests/test_agents_signed_role.py (new, chain + each
broken link + both comparison branches), tests/test_slot_dispatch_workspace.py and tests/test_dispatch_stager_send.py (round trip with a signed withhold,
loosening, extra withhold, lying manifest, missing/forged evidence, factory hand-off, client refusals), tests/test_agents_spawn.py (a withhold role spawns).
Mutation-checked, each reverted line turned its named test red: lease.py `envelope_ceiling`; workspace.py `withhold_problem` loop; workspace.py withheld-path
check; dispatch_run.py missing-evidence refusal; dispatch_run.py `proof.spec_hash` binding; stager.py `withhold_problem`; stager.py `list(ws.withhold)` into
build_snapshot; session_wire.py `role_withhold`.

## Residue (named)
- (closed in the r1 fix round: a signed-role refusal of this connection's upload now tombstones, see below)
- The evidence adds the spec and role texts (each <= 64 KiB) to a workspace LAUNCH frame; a very large brief template could push the frame past
  RUN_FRAME_MAX and be refused `launch-frame-refused` before connecting (named, not measured).
- No migration: a role with a withhold is a NEW role_hash, so the agent that should use it must be re-registered and re-signed by him (his np-b978efbc ruling).
- No operator CLI doc/doctor line for `ceiling.withhold` was written.

## UNVERIFIED
- Closure test: first run failed (4 new function-local imports unclassified); classified all four as `operator` in tests/ci/root_closure.py; the
  classification test and the lazy-site tests pass (7). The full closure file was not re-run after the fix (first run: 17 pass, that 1 fail).
- Sibling suites (spec, session launcher/launch, run server, replica, refuse, lease): 644 passed.
- Not run on the VM: the unit as `_levain_dispatch` under the root Python 3.9 (signed_role.py has no 3.10-only runtime syntax, not executed on 3.9).
- The three prior-art URLs in w2_FRAME.md were not fetched.

## L3 r1 fix round (findings: w2_r1.out; 3 lineages: complement, deepseek, codex; gemini errored, a gap, not a pass)

Each finding resolved against disk first. Verdicts:

1. **codex HIGH, dispatch_run.py:738: CONFIRMED, FIXED.** On disk the server derived only spec_hash -> the spec's pin and never
   compared that pin with the link's own `role_hash`, nor the evidence's agent with the link's `agent_id`; the client passed `None`
   and compared no link at all. Worse than described: the test fixture's links all carried `role_hash = "c"*64`, never the spec's pin,
   so every W2 workspace test ran on exactly the mismatched shape and passed. Fix at the point of use, both uids:
   `signed_role.SignedLink(agent_id, spec_hash, role_hash)`; `signed_withhold(signed, link)` now REQUIRES one and refuses
   `link` (none), `link-agent`, `spec-hash`, `link-role` ("the role the spec pins is not the role the verified link names"), `role-hash`.
   Server: `verify_point_of_use` returns the grant from its own replay; LAUNCH hands it to `_workspace_of`; the withhold is derived
   against `SignedLink.of_grant(grant)` (refused `signed-role-link-role` / `signed-role-link-agent`). Client: `signed_link_of(store,
   cockpit_root, occ)` (same verified replay as `link_proof`, shared `_standing_link`), `DispatchSessionLauncher(link_of=)`,
   `build_launcher` wires it; refused before git. Fixture `_occurrence` now signs the role the spec pins (`link_role_hash=` forges).
   Tests: `test_a_link_naming_a_locked_role_whose_spec_pins_an_open_one_derives_nothing` (signed_role),
   `test_a_link_naming_a_locked_role_beside_a_spec_pinning_an_open_one_never_stages_the_secret` (real server, secrets/token in the
   snapshot), `test_the_client_refuses_..._before_git` and `test_the_real_client_refuses_a_forged_link_with_no_connection_opened`.
2. **complement + deepseek MED, lone surrogate: CLAIM REFUTED ON THE WIRE, guard ADDED in process.** Over the wire the frame reader's
   `canon.parse` refuses an escaped `\ud800` and the server hangs up unanswered (never internal-error/transient); held with raw bytes
   in `test_a_lone_surrogate_in_the_role_evidence_is_a_plain_refusal_never_a_transient_one`. In process, `SignedRole.__post_init__`
   now encodes strict: "signed.<member> is not UTF-8 text" (ValueError, so `launch_from_wire` makes it a plain WireError);
   `signed_withhold` keeps any encode failure a `SignedRoleRefused`.
3. **codex MED, role/spec over SIGNED_TEXT_MAX: CONFIRMED (a 70 x 1 KiB `lands` role registered), FIXED at the source.** `role.add_role`
   and `spec._register_in` refuse a canonical text over SIGNED_TEXT_MAX bytes: "the role's (spec's) canonical text is over 65536 bytes,
   the most a launch can carry as signed-role evidence: shorten it". Already-stored oversized roles (none expected) stay a standing
   `signed-role-unavailable` at launch. Inline spawn roles are not registered and never travel (`signed_role_of` reads the registry).
4. **complement LOW, no tombstone: CONFIRMED, FIXED.** `WorkspaceUploads.verify` takes the derivation as a callable and runs it inside
   its judgment (after the own-upload check): a standing signed-role refusal of this connection's upload discards it and tombstones,
   like every other. The missing-evidence test now asserts the tombstone.
5. **complement LOW, `RoleError` from `role_withhold` in `_workspace_kwargs`: REFUTED.** `role` is `_pinned_role` ->
   `role_by_hash` -> `_check_row` -> `parse_role`; re-parsing an already-parsed role cannot raise.
6. **complement LOW, entry check casefolds patterns per entry: FIXED** (folded once, skipped when the withhold is empty).
7. **complement LOW (test), either name accepted: FIXED**, pinned to `withhold-loosened` (the member is judged first).
8. **deepseek LOW, server does not re-run `_pattern_problem` on signed patterns: REFUTED as a confinement break.** The patterns are what
   his signature covers; a pattern that matches less is a looser withhold he could equally sign as `[]`. Registration validates them.
9. **deepseek LOW, `signed_role_of`'s broad except is transient: NOT CHANGED.** Same convention as `link_proof` (a store that does
   not read may read next time); enumerating "real" faults is a separate design call.
10. **codex LOW, protocol version for the new `proof.signed` field: KNOWN EDGE, not fixed.** A client/daemon version skew fails CLOSED
    (`bad-request` / `signed-role-missing`), never loose; a `levain-run/2` bump is a protocol decision for the head.

Mutation-checked (each revert turned its named tests red, 0 survivors): `role_hash != link.role_hash`; `signed.agent_id != link.agent_id`;
server deriving the link's role from the spec instead of the grant; `add_role` bound and its `>` vs `>=`; spec bound; SignedRole strict
encode; derivation evaluated outside `verify` (tombstone); client `link_of is None` refusal.
/usr/bin/python3 3.9.6: `py_compile` OK on signed_role.py, session_client.py, dispatch_run.py, workspace.py, role.py, spec.py; signed_role imports.
Root closure: `link_proof`'s two moved imports reclassified under `_standing_link` (operator).
Suites run after the fix: every test file importing a touched module (46 files: 1878 passed) and tests/test_ci_root_closure.py whole
(18 passed; this also closes the r0 UNVERIFIED line about the closure file not being re-run). Still not run: the unit on the VM under
the root Python 3.9 (compile + import only); no L3 r2 yet on this round's diff.

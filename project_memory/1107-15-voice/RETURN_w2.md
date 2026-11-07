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
- A signed-role refusal is raised before `verify`, so it writes NO zero-usage tombstone (same as `no-upload`); the fire records the refusal itself.
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

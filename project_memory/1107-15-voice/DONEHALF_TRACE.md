# DONEHALF_TRACE: does the product write `done` from the vendor's exit?

Question (S7 END rerun, row 7 "agents show done half NOT run"): in the 1105-36 fire the `done` in `levain agents show <scratch> --json` came from the fire rig's `rig.finish`. Does the PRODUCT write it from the vendor session's exit?

**Answer: yes, for every exit path I could trace; none writes the wrong state.** One path writes nothing on purpose (unproven exit). No defect found, so no fix; the claim is now pinned by `tests/test_agents_session_vendor_exit.py`.

## The chain (vendor exit to the row)
1. Vendor (claude/codex in the slot) exits -> the inner `GatedChild` gate (`levain/agents/controller.py` `_gate`) exits with the executor's status, "a signal death as 128 + the signal", `GATE_NO_EXEC` 127 if it could not exec, `GATE_UNPROVEN` 76 "when it cannot vouch that the executor's group is gone".
2. `levain/agents/session_launcher.py` `_SlotProc._settle`: `return rc if self._l.end_once(polling=polling) else _ctl.GATE_UNPROVEN` (the gate's status is answered only after S1 END proved the slot uid's group gone).
3. Run server `levain/slot/services/dispatch_run.py` `op == "WAIT"`: `rc = mine.child.process.wait(...)`, `C.ok_answer(rid, exited=True, returncode=rc)`; a timeout answers `exited=False, returncode=None`.
4. Operator side `levain/agents/session_client.py` `_RemoteProcess._slice`: the server's `returncode` is published once; any wire fault is `self._lose()` -> `_publish(_ctl.GATE_UNPROVEN)` ("monotonic: a recorded server exit is never downgraded").
5. `levain/agents/session.py` `run_session`, inside the journaled effect: `rc = waiter(started.value)` -> `{"outcome": "exited", "returncode": rc, ..., "proven": _ctl.gate_proves_termination(rc)}`. `gate_proves_termination` (controller.py) is `returncode >= 0 and returncode != GATE_UNPROVEN`, False for None, bool, non-int.
6. After the effect: `if outcome == "exited" and not result.get("proven"): return SessionReport(1, outcome=outcome, state="launched", ...)`; else `ok = outcome == "exited" and result.get("returncode") == 0`, `reason = None if ok else (f"session_exit_{...}" if outcome == "exited" else outcome)`, `end = sup.finish(occurrence_id, executor, ok=ok, reason=reason)`.
7. `levain/agents/supervise.py` `Supervisor.finish`: `state, why = "done", "completed"` if ok else `"failed", reason or run_failed`; then `verify = self._hooks.verify or self._win_verify` (`gauges.win_verifier`); a `done` the verifier rejects becomes `failed` with its reason (`win_unverified: ...`); `_settle_held` -> `controller._end_launched` writes `UPDATE agent_occurrences SET state = ?, reason = ?, ended_at = ?` (a budget breach found at settle overrides to `failed`/`budget_breach`); then `on_end`.
8. `on_end` in production (`session_wire._episode_hooks`): one episode `session run <oid> of agent <aid> ended <state>: <reason>` (type `outcome`, source `levain-agents`) in the install's episodic store, only when the committed end wrote.
9. `levain agents show <agent> --json` (`levain/agents/cli.py` `_show_json`) reads `SELECT occurrence_id, state, reason, ... FROM agent_occurrences` and `recall_agent_episodes_at(...)`. It writes nothing; the `done` it prints is exactly the row state from step 7.

## Exit path table
| Vendor / gate outcome | Gate or poll answer | Row after the fire | Reason | Episode | Slot / reservation |
|---|---|---|---|---|---|
| clean exit 0, END proven, win check passes | `0` | **done** | `completed` | `ended done: completed` | freed, settled |
| clean exit 0, END proven, spec's win check unregistered or failing | `0` | **failed** | `win_unverified: ...` (fail closed, `Supervisor.finish`) | `ended failed: win_unverified...` | freed |
| clean exit 0 but END NOT proven | `76` (`_SlotProc._settle`) | **nothing**: stays `launched` | none | none | held for the reaper |
| exit N (1, 3, 127) | `N` | **failed** | `session_exit_N` | `ended failed: session_exit_N` | freed |
| killed by signal S | `128+S` (137 for SIGKILL) | **failed** | `session_exit_137` | yes | freed |
| vendor itself exits 76 (collides with `GATE_UNPROVEN`) | `76` | **nothing**: stays `launched` | none | none | held (documented: "An executor that itself exits with this code reads the same: unproven, the safe side") |
| gate killed outright / negative / `None` / non-int | negative / None | **nothing**: stays `launched` | none | none | held |
| wire lost to the run server (`_Down`, `_Refused`, `WireError`) | `76` via `_lose` | **nothing**: stays `launched` | none | none | held; reaper's END (`RowSessionExecutor`) settles it later |
| `waiter` raises | exception propagates | **nothing**: stays `launched` (child stopped, aborted) | none | none | held for reaper / lapse recovery |
| `launcher.spawn` raises | n/a | **failed** | `session_spawn_failed` (`WORKSPACE_REFUSED` if so named) | yes | freed (zero usage if `nothing_sent`) |
| `start_run` did not start | n/a | **nothing**: `launched` | none | none | held |
| a refusal (`launch_refusal`, brief, workspace) | n/a | **failed** | `session_refused:<why>` | yes | freed, settled at zero |
| row not `launched` at this epoch (fenced) | n/a | **nothing** (`not_held`) | none | none | untouched |

"Nothing" is never a `done`: every path that writes `done` requires `returncode == 0` AND `gate_proves_termination` AND a passing verify hook; the only way a nonzero or crashed vendor reads clean would be a flip of `ok`, which the new tests pin.

## What was already pinned and what was not
Already pinned (fake gate objects, `tests/test_agents_session_launch.py`, `test_agents_session_register_done.py`): `run_session` mapping from a fake `rc` (clean/nonzero/unproven), the production factory with the real `session_completed` check, `agents show --json` reading one done row plus its episode. Not pinned before: (a) a REAL gate process exiting with real statuses (0, 1, 3, 127, SIGKILL=137, 76) through the production fire; (b) `_SlotProc`'s END-proof gate on the exit; (c) the clean-exit-but-win-unregistered demotion through the production fire.

## Pinned now: `tests/test_agents_session_vendor_exit.py` (12 cases, all pass)
Production fire (`ctl.fire` -> `run_agent` -> `session_wire.fire_factory`), real `GatedChild` running `python -c "<vendor>"`, real `session_completed` check, real episodic store (anneal `Store`), read back through `levain agents show <agent> --json`. Mutation checks run (each reverted after):
| Test | Line whose change turns it red | Observed |
|---|---|---|
| exit 0 -> done + episode | `session.py` `ok = outcome == "exited" and result.get("returncode") == 0` -> `ok = False` | red (failed/session_exit_0) |
| exit 1/3/127/SIGKILL -> failed `session_exit_N` | same line -> `ok = outcome == "exited"` | 4 red (a crashed vendor recorded `done`) |
| exit 76 -> row stays `launched`, slot held, no episode | `session.py` `if outcome == "exited" and not result.get("proven"):` -> `if False:` | red (failed/session_exit_76) |
| clean exit, win check unregistered -> failed `win_unverified` | `supervise.py` `if verdict is not None and state == "done":` -> `if False:` | red (done) |
| `_SlotProc` answers 76 when END unproven | `session_launcher.py` `return rc if self._l.end_once(polling=polling) else _ctl.GATE_UNPROVEN` -> `return rc` | 2 red |

## Residue (not claimed)
- A real vendor (`claude`/`codex`) in a real slot with real S1 END and the wire server: the VM's, not run here (no VM allowed). The wire leg (steps 3-4) is pinned by `tests/test_agents_session_client.py`/`test_slot_dispatch_run_server.py`, not re-run end to end.
- A clean exit with a vendor-reported error (stream-json `is_error`, a `permission_denied` inside a run that still exits 0) is `done`: exit status is the only signal, as the S7 AMBERs (R.4/R.5 probe lines not landing) already showed. Product-semantics question, not a defect in this path.
- A lapse-recovery/reaper settle of the "nothing" rows is the supervisor's (`_stop_and_settle`, `_settle_stopped`), unchanged and not traced here beyond the table.

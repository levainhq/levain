# RETURN: hands-MCP M2 (row 1 of the 11-07 board audit), lane seat/1107-20-m2

Base origin/main 6924d8d9. One code commit 4c00d226 (this file in a second). Not pushed. No L3 run (the head runs it).

## What the row meant, and the reading I took
STATUS/audit: "the hands chat launch writes the registry-rendered claude.json / codex.toml into the hands home, with callers of both renderers". On main nothing calls `render_claude_json` / `render_codex_toml`, AND no hands-uid Claude/Codex chat spawn exists in the tree (the room is "no hands"; the openhands session uses the hands user for bash only). So M2 as built is the launch's input side: the write step and the argv builders a launcher uses. The spawn that consumes them does not exist yet (see UNVERIFIED 1).

## Built (all in `levain/firing/hands_mcp_login.py`, plus a 4-line verb in `levain/cli.py`)
- `prepare_launch(hands, entity_dir, cockpit_root, now=, held=)`: reads the live registry (`load_registry`, the verified log), renders BOTH vendors through `render_launch` (the one caller of `hands_mcp.render_claude_json` and `render_codex_toml`), then has the HANDS USER write (tee, chmod 600, rename):
  - Claude: `<hands home>/.claude/levain-mcp.json`, whole file Levain's, `{"mcpServers": {}}` when nothing is live.
  - Codex: `<hands home>/.codex/config.toml`, the registry tables between two marker lines; `cli_auth_credentials_store = "file"` and `mcp_oauth_credentials_store = "file"` at the top level; every other line kept (`merge_codex_config`).
- An entry that expired, was revoked, or fails digest/command/name leaves BOTH files at the next call (rendered from live entries only, never from a prior launch's file). Hand edits inside the block are overwritten.
- Refusals, each leaving the account as found (including the Codex pin, which `ensure_config_home` would have added): operator-home collision / root hands (`store_problem`), unreadable authority log, renderer refusal (digest, reserved or repeated name, repeated callback port, untrusted stdio command), non-TOML config, unbalanced/repeated marker, either store key set to anything but "file", a foreign `[mcp_servers.<rendered name>]` outside the block, a launch file that is not a plain file, a session holding the hands lock (unless the launcher passes `held=True`, for a launcher that already holds it). Reads and the merge happen before any directory or file is made.
- `claude_chat_argv(hands, *extra)` = hands prefix + `claude --strict-mcp-config --mcp-config <launch file> ...`; `codex_chat_argv`. Verb: `levain hands mcp prepare [--path] [--data-root]` (prints paths, names, "written"/"unchanged", skipped entries).
- Docstring in `levain/hands_mcp.py` updated (it said M2 was a later slice).

## Tests
- New `tests/test_hands_mcp_launch.py`: 27 tests, 27 pass. Neighbours (every file importing a touched module plus the scanner set and test_cli, test_docs): 492 passed in one run (test_hands_mcp_launch, test_hands_mcp, test_hands_mcp_verbs, test_doctor_hands_mcp, test_hands_login, test_hands_logout, test_ci_root_closure, test_contracts_namespaces, test_w1b_retirements, test_contracts_docs, test_root_py39_floor, test_seam_classes_census, test_door_census, test_markdown_parity, test_docs, test_cli); `tests/ci/root_closure.py lazy` shows no UNCLASSIFIED.
- Mutation checks (each reverted by git checkout; script `jobs/0df76cb7/tmp/mut_m2.sh`): disable foreign-server clash -> `test_a_foreign_codex_server_of_a_rendered_name...` red; store-key check off -> both `test_a_store_key_set_to_anything_but_file` cases red; marker balance off -> `test_an_unbalanced_or_repeated_marker` red; `ensure_config_home` before the merge (the pin-before-refusal bug) -> store-key and marker tests red; unconditional write -> `test_a_second_call_changes_nothing` red; lock dropped -> `test_a_session_holding_the_hands_lock...` red; top-level pin dropped -> 4 tests red; renderer call removed before writes -> 4 refusal tests red. One further mutation (empty-tables guard) is an equivalent mutant and was not counted.
- Found by the tests, fixed: `hands_mcp.CODEX_STORE_LINE` has no trailing newline (merged file did not parse).

## Residue (only a real run / device shows)
1. No hands-uid chat spawn exists in the tree, so nothing yet calls `prepare_launch` or `claude_chat_argv`. Wiring it into a launcher (and `ps -o uid=` of the spawned child on the VM, the 1103-27 DONE line) is the next slice. A design question for Phill/the head, not built: should the hands Claude chat run `--strict-mcp-config` (registry only, so a server signed in through M3 into `.claude.json` is read from the launch file instead) or without it?
2. That claude/codex accept the files and that an OAuth token M3 stored in `.credentials.json` is used for a server declared via `--mcp-config` (the token key may depend on how the server was added): needs the VM with a real signed-in hands account. Hands account does not exist on this Mac (`levain hands status`).
3. Real sudo to a different uid: the rig runs the hands steps as the current user, so file owner = hands is asserted as "the hands identity's uid" only.
4. Linux: rendering is allowed (no network needed); a Linux hands login/egress remains the stated edge.

## UNVERIFIED
Everything under Residue. No L3 mesh run. The doctor (M4) does not yet compare the launch files with the registry (drift between a file and the log is invisible until the next `prepare`); not built here.

# RETURN readme (O21 whole-README currency read), branch seat/1107-20-readme

**Currency record (one line):** README.md read start to end against origin/main 6924d8d9 (`git rev-parse --short=8 origin/main` at branch point; main version `0.11.29.dev0`, last tag v0.11.28); every command and flag in a span or code block resolved against `levain.cli.build_parser`; stale claims fixed below; the rest not re-derived is under UNVERIFIED.

## Starting point
The research half (`project_memory/1105-36/RETURN_readme_currency.md` in the main levain checkout, untracked, read there) was written against a 604-line README at 3e2aef8d. The README is now 660 lines. Its rows were re-derived, not trusted:
- S1 (ping policy / policy.json), S2 (sleep half), S3 (ping devices), S4 (case-alias locator): ALREADY FIXED on main (line 475, 505, 409). Checked by reading the lines and `PRESET_TABLE` in `levain/webpush/intent.py` (sleep = needs, until input).
- Section 5 omissions: install front door (pipx/uv, Node 26, Python check), the "All commands" table (test_readme_commands) are in. `levain upgrade` is a HIDDEN verb (no help line), so the table test rightly skips it and the README does not name it.
- S5 (fleet / "none of it is released") was still stale. Fixed.

## What changed (README.md, 5 edits)
1. Lines ~618, ~650: "Coordinating several seats as a fleet" now says several *scheduled entities* (the thing `daemon install-seat` runs). The coordinator and the Agents graph do ship; the sentence claimed they did not.
2. Line ~647: "nothing here has an install command, because none of it is released" was false: `levain binding policy create|renew|revoke|list` is released (CHANGELOG 0.11.27). Now says none of it *runs* yet and names the one released piece. Evidence that nothing fires: CHANGELOG "Bindings do not fire yet in this build (no binding driver is wired)" and `levain/cli.py:4250` "no executor can run a binding effect, so no binding fires".
3. Lines ~259, ~478: the extras installs (`levain[openhands]`, `levain[app]`) said `pip install` while the front door now says pipx/uv tool. A bare pip would put the extra in a different environment. Added the pipx / uv tool form.

## Tests
- NEW `tests/test_readme_spans.py` (4 tests): every `levain ...` span and fenced line resolves in the real parser (verb path and every `--flag` on the node or an ancestor); every `tests/<file>.py::<test>` locator names a test defined in that file; names GONE verbs (`ping revoke`, `ping pair`, `cockpit ledger re-genesis`) and fails if one returns to the parser.
- Mutation proof, run for real: pointing the case-alias locator back at `tests/test_cockpit_room_join.py` (the S4 stale state) turned the locator test red; restoring turned it green. The doctored-copy test also proves a bogus flag, a bogus verb, a bogus fenced command and a stale GONE sentence each go red. Line whose revert turns the locator test red: README.md line 409, `tests/test_room_dir_case_alias.py::`.
- Ran: tests/test_readme_spans.py, test_readme_commands.py, test_readme_install.py, test_contracts_docs.py -> 31 passed. Files touched: README.md (docs), the new test.

## Residue (named)
- `pip install 'levain[openhands]'` is also the text in `levain/cli.py:684,841`, `levain/dream/executor.py:719` and `levain/firing/openhands/*`. Product error messages still say bare pip. Code not touched (lane = README); a pipx user who follows the product message hits the wrong-environment trap.
- `levain upgrade` (hidden verb) is the right word for "after you upgrade Levain" but is not named in the README; whether to un-hide it and name it is a product call.
- README says nothing at the top about which release it describes (H4 in the research half); main differs from v0.11.28 in the room section and the `phone path` doctor row until the next cut. His and the desk's call.
- Sections 6 and 7 of the research half are unchanged by this lane: his words (pitch, Why Levain, Audience, the S5 section's argument) were not edited.

## UNVERIFIED
- `pipx install --force 'levain[openhands]'` and `uv tool install --force 'levain[app]'` were not run (would mutate his installed tools); they follow the documented flags (`uv tool install --help` lists `--force`; pipx is installed).
- Not re-derived this pass (carried from the research half as true on 11-05, README text since edited by other lanes): numbers and defaults in the consent, team, OpenHands, seat, serve and room sections other than those spot-checked here (TESTED_VERSIONS, Node 26, `phone path` row, key create default, room test names and files, extras in pyproject, presets).
- Linux sandbox prose (lines ~524-550) needs a Linux host; the phone/iPhone and Touch ID/passkey behaviours need a device; the 6/10 vs 0/10 probe numbers and the 96 -> 549 growth line were not re-derived.
- Not pushed.

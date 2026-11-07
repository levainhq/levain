# RETURN: envelope `launches` grant (lane seat/1107-15-launches)

Implemented per ENVELOPE_LAUNCHES_SHAPE.md. Worktree /Users/phillipclapham/Briefcase/.levain-1107-15-launches, branch seat/1107-15-launches. Not pushed, not merged.

## Changed
- levain/coordinator/envelope.py: LAUNCH_LIST, OPTIONAL_KEYS, GRANT_LISTS, LAUNCH_DEFAULT=2, LAUNCH_MAX=4; parse_body accepts BODY_KEYS or BODY_KEYS+launches only and validates the list; widens() (bigger count / new repo); effect_key/effect_identity for launches; pure `launch_allowance(envelope, where_repo, now, used)`. EFFECT_LISTS unchanged (three).
- levain/coordinator/envelopes.py: narrow_scope zeroes launches; _effective honours drop_effect/kill for launches; drop_effect accepts a launches identity.
- levain/coordinator/cli.py: card section "launches (seats) it may do:" with "may launch up to N seats in <repo> until <time>" (only when a grant exists; +/~/-/= marks work); spec accepts `launches`; body emits the key only when non-empty; `extend` carries the head's grant.
- levain/cockpit/threads.py: authority line adds "; may launch up to N seats in <repo> until <stop UTC>" only when the key is present.
- levain/coordinator/envelope_draft.py: proposal may carry the optional key; empty_base/against_diff handle it.
- levain/coordinator/phone.py (+ web_server.py comment): COCKPIT_DEVICE; `_spec` allows `launches` only for device "cockpit"; a paired phone is refused ("sets only ...").
- levain/templates/web/envelope_form.js, sign.js, dashboard.css: toggle "Launch seats without asking again", off by default, count field default 2 clamped 1..4, spec.launches only when on; drawn only when the page passes `launches` (sign.js passes it for the embedded cockpit modal, not the standalone /sign/drop page). CSS: `.env-field[hidden]` (a real bug the browser test caught: the hidden count box still showed).
- CHANGELOG.md [Unreleased] Added (one entry); docs/contracts.md new section "The thread envelope's launches grant" (no other docs page describes envelope grants; `git grep publishes docs/` finds only an unrelated line).
- Tests: tests/test_envelope_launches.py (new, 49 tests incl. digest literal computed by pre-change code, old-reader refusal via emptied OPTIONAL_KEYS, tamper 2->4, narrowing, widening, allowance, card words), tests/test_envelope_launches_phone.py (new, 2), tests/test_envelope_sheet.py (+2 browser tests; the a11y count test moved 4->5 fields, 3->4 hinted groups for the new field/group).

## Tests run
Failing first: new files against origin/main source (stashed levain/) failed at import (no GRANT_LISTS/COCKPIT_DEVICE); earlier body-shape failures seen while writing. Final: 22 touched/related files, 662 passed + the sheet file rerun (39 passed after the a11y fix; the 1 earlier failure was the count update). Browser tests ran (headless Chrome available).

## Deviations
1. Drafts: the design put `launches` in the draft EDITABLE set. I made it an OPTIONAL proposal key instead (not EDITABLE). Reason: EDITABLE keys are required in every stored proposal, need a cite each, are carried by renew/replace, and always present, which would break stored proposals and put the key in every drafted body (changing bytes and making old levains refuse them). Effect: a renewal or replacement never carries a launch grant forward; it must be signed on purpose each time. `_EDIT_SPEC` untouched.
2. Card time is the machine's local zone abbreviation (e.g. EST), not a literal "ET".
3. Store-side wrapper `EnvelopeStore.launch_allowance` NOT added (design section 7 offered it; the task named only the pure helper). The desk gets the effective body from store.effective(...) and calls env.launch_allowance.
4. The worded `echo` CLI builder takes no launches (only the structured spec and `extend` do).

## RESIDUE (cannot be seen until the desk installs)
How the toggle and the signed card LOOK on the live cockpit: open the cockpit, tap "Add an envelope" (the drop modal), find "Launching seats" under Landing in the left column: confirm the toggle is off, turn it on, the "Seats, 1 to 4" box appears with 2, the summary under "What you sign" shows "launches (seats) it may do: ... may launch up to 2 seats in <repo> until <time>", sign with the passkey, then open that envelope's card. Also open /sign/drop on the phone: no "Launching seats" group should appear.

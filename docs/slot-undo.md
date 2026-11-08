# What `install_helper undo` leaves behind

`python -m levain.slot.install_helper undo` (installed as `/usr/libexec/levain/install-helper undo` on Linux,
`/usr/local/libexec/levain/install-helper undo` on macOS) takes the slot install apart: units, sudoers rules,
pool config, policy limits, ceiling and signers, the confinement (AppArmor, bwrap copy, netns, pf), the group
memberships and groups, the install record and the operator record. Each `_levain_*` account is retired in
place (no login, no home), and its id is written to the retired-ids list, so no later account reuses it.

Three things stay on purpose, because a re-run of `setup` (or `repair`) reuses them and undo must never delete
what it did not create per record:

| Kept | Linux | macOS |
|---|---|---|
| the store, including the `reserve` file (1,342,177,280 bytes of written zeros) and the session records | `/var/levain` | `/private/var/levain` |
| the root import tree | `/usr/lib/levain-root` | `/usr/local/lib/levain-root` |
| the helper entries | `/usr/libexec/levain` | `/usr/local/libexec/levain` |

Inside the store, undo removes `policy/pool`, `policy/install-shape` and `policy/operator`, plus `policy/limits`,
`policy/dispatch_ceiling` and `policy/authority_signers` when they exist; nothing else. That set is derived from
`plan_undo`: `tests/test_undo_keeps_store.py` runs the plan against a disposable root (every program recorded,
none run) and compares the tree before and after, so the removals made by call steps are seen too. The retired-ids
lists live outside the store (`/var/lib/levain` on Linux, `/var/db/levain` on macOS) and also stay.

To see what undo does before you run it, read `plan_undo`'s docstring in `levain/slot/install_helper.py`.
To free the space of a finished install, delete the store and the two trees by hand after undo reports `ok`.

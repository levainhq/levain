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

Inside the store, undo removes only `policy/pool`, `policy/install-shape` and `policy/operator` (plus the limits,
dispatch-ceiling and allowed-signers files when they exist). The retired-ids lists live outside the store
(`/var/lib/levain` on Linux, `/var/db/levain` on macOS) and also stay.

To list what undo will do before you run it, read the plan: `tests/test_undo_keeps_store.py` pins the kept set
against `plan_undo` for both hosts, and `plan_undo`'s docstring in `levain/slot/install_helper.py` is the source.
To free the space of a finished install, delete the store and the two trees by hand after undo reports `ok`.

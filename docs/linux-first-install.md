# First install of slot isolation on Linux

Slot isolation is the Linux service install: the `_levain_*` system accounts, the systemd units and the slot pool. It is not `levain setup-isolation`, which creates one dedicated user for one entity's bash (see the README). The supported way to run the slot install is a module, `levain.slot.install_helper`, run as root from the Python environment that has Levain installed. Derive that with `git grep -l install_helper -- levain ':!levain/slot'`: the files it lists build the installed helper's path or a `repair` line, and none runs `setup`.

What was run: a person drove one fresh Ubuntu 24.04 aarch64 guest (systemd 255, Python 3.12.3) through a source-tree install, `setup`, `repair` and `undo` (this page calls that run L2; its record is `project_memory/1107-26-linuxvm/L2_RUN.md`, and `project_memory/` is ignored by git, so the record is not in a clone). The index install in step 1 was not run. Ubuntu 24.04 on arm64 (aarch64) is therefore the tested Linux target. Nothing here has been run on another distribution, on x86, or on a machine with a prior install.

## 1. Install Levain with the isolation extra

The helper imports `h11`, which the `isolation` extra pulls and the base install does not (`git grep -n -A1 '^isolation' -- pyproject.toml`). Put Levain in a virtual environment whose path you will name in step 2:

```sh
python3 -m venv ~/venv
~/venv/bin/pip install 'levain[isolation]'
```

L2 installed from a source tree instead: `pip install -e .`, then `pip install -e '.[isolation]'`. On the plain install, setup refused before changing anything:

```
{"ok":false,"refused":"bad-root"}
```

with, on stderr, a message ending `install levain[isolation]` (the text is in `levain/slot/root_tree.py`: `git grep -n 'install levain\[isolation\]' -- levain/slot/root_tree.py`). The remedy is the install above with the extra, then step 2 again.

The L2 guest also had `python3-venv python3-pip git` (apt), the userns sysctl lifted, and `acl`, `bubblewrap` and `sudo` (`l0_stage.sh`). Which of those setup itself requires was not isolated.

## 2. Run setup as root

```sh
sudo ~/venv/bin/python -m levain.slot.install_helper setup --slots <N> --verifier-slots <M> --operator <user>
```

- `--slots` takes 3 to 32 and `--verifier-slots` 1 to 16. The caps are `MAX_SLOTS` and `MAX_VERIFIER_SLOTS` in `levain/slot/grammar.py`; the floor of 3 is `MIN_SETUP_SLOTS` there (the dispatch slot, the operator's reserve and one free slot), which `install_helper.plan_setup` applies to a fresh setup. The test file pins each number on this page to those constants.
- `--root <absolute path>` is optional, may repeat up to 16 times, and each path must differ. The other three flags are required and none may repeat.
- `<user>` must be your own login name, and you must reach root through `sudo` as that user. The helper refuses `caller-not-operator` when the `SUDO_UID` user is not the `--operator` user, and a root shell has no such uid (`git grep -n 'caller-not-operator' -- levain/slot/install_helper.py`).
- Every verb of the helper, `repair` and `undo` included, refuses `not-root` unless it runs as root (`git grep -n 'not-root' -- levain/slot/install_helper.py`).
- A venv in your home is a writable tree. For `setup`, the only source checks `root_tree.check_sources` makes are that the package directories are named `levain` and `h11` and are not links. The ownership and group-writable check, `root_tree.check_source_writers`, runs, inside the helper, for `repair --source` only (`git grep -n 'check_source_writers' -- levain/slot`). Whether a user-owned venv passes everything else setup does was shown by one run only: L2's was owned by the operator and passed.
- L2 ran it from `/`. This page does not show that the working directory matters.

L2 ran `--slots 4 --verifier-slots 1 --operator admin` and got, with rc=0:

```
{"ok":true,"slots":["e0","e1","e2","e3","v0"],"steps":364,"verb":"setup"}
```

Progress goes to stderr, one numbered line per step. L2 also saw a stderr line `pending: ... verb-unbuilt for: ...` naming helper verbs the build lacked; the list changes as verbs land, and in L2 it was informational.

After it, L2 saw six systemd units active (`levain-control`, `levain-dispatch`, `levain-dispatch_run`, `levain-keys`, `levain-qbroker`, `levain-reaper`). Those six are what L2 saw; the units the code installs on Linux are the entries of `SERVICES` in `levain/slot/units.py` whose hosts include `linux`, and that list is the one to trust, the store under `/var/levain`, and the helpers under `/usr/libexec/levain/`.

## 3. Repair

`repair` re-applies setup's steps to an existing install; steps already done report "already done". L2 ran it on the live install: rc=0, 252 steps, all six units still active. It needs root, and the same operator as `setup`.

```sh
sudo ~/venv/bin/python -m levain.slot.install_helper repair
```

After a setup, the installed helper is the shorter form that Levain's own refusals name (`repair_line` in `levain/slot/layout.py`): `sudo /usr/libexec/levain/install-helper repair`. `repair` also takes `--source <absolute path>`, the operator's installed levain package directory; that is the form the writer check in step 2 applies to.

## 4. Undo

```sh
sudo ~/venv/bin/python -m levain.slot.install_helper undo
```

The installed-helper form works here too, which matters if you no longer have the venv: `sudo /usr/libexec/levain/install-helper undo` (not run in L2, which used the venv form). `undo` also needs root and the recorded operator.

L2 ran it: rc=0, 146 steps. The units were stopped, disabled and removed, `/run/levain` and the sudoers drop-ins went, and the operator record went last. It does not delete everything. The `_levain_*` accounts stay, retired (nologin, locked, no home), so a user id is never reused, and `/var/levain`, `/usr/lib/levain-root`, `/usr/libexec/levain` and `/var/lib/levain` were still present afterwards (the run record, step 10). [slot-undo.md](slot-undo.md) says of the store, the root import tree and the helper entries that they "stay on purpose, because a re-run of `setup` (or `repair`) reuses them and undo must never delete what it did not create per record", and that the retired-ids lists in `/var/lib/levain` also stay. It gives the way to delete them by hand.

## What this page cannot tell you

- Whether the released package (`pip install 'levain[isolation]'` from the index) contains `levain.slot.install_helper` and the extra at the version you get: L2 used a source tree.
- Whether a group-writable or differently owned venv passes `setup` (see step 2).
- Ctrl-C part-way through `setup` on an interactive terminal, then re-running.
- `pipx install 'levain[isolation]'`. The interpreter in the `sudo` line must be the one that has Levain and `h11`; for pipx that is the tool's own environment, and its path was not measured.
- Real vendor logins, dispatch and fire paths, and reboot persistence.

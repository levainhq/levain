# First install of slot isolation on Linux

Slot isolation is the Linux service install: the `_levain_*` system accounts, the systemd units and the slot pool. It is not `levain setup-isolation`, which creates one dedicated user for one entity's bash (see the README). The slot install has no `levain` verb. The first one runs a module as root from the Python environment that has Levain installed.

[judged by levain 60 lane, 11-07, against project_memory/1107-26-linuxvm/L2_RUN.md, step 5 and Surprises: "there is NO `levain` CLI verb for it"; checked here with `git grep -l install_helper -- levain ':!levain/slot'`, which lists `consent/limits_cli.py`, `dispatch/cli.py`, `doctor.py` and `upgrade.py`; the first, second and fourth only build the installed helper's path or its `repair --source` line, and `doctor.py` was not read in full]

Everything below was run once, on a fresh Ubuntu 24.04 aarch64 guest (systemd 255, Python 3.12.3), by a person driving the guest shell. Nothing here has been run on another distribution, on x86, or on a machine that had a prior install.

## 1. Install Levain with the isolation extra

The helper imports `h11`, which only the `isolation` extra pulls (`git grep -n -A1 '^isolation' -- pyproject.toml`). Install it in a virtual environment you will name in step 2:

```sh
python3 -m venv ~/venv
~/venv/bin/pip install 'levain[isolation]'
```

A plain `pip install levain` has no `h11`. Setup then refuses before it changes anything:

```
{"ok":false,"refused":"bad-root"}
levain install-helper: refused bad-root: 'h11 is not importable by the installing Levain (...); install levain[isolation]'
```

The remedy is the same command with the extra, then run step 2 again (L2 did this: `pip install -e '.[isolation]'` from a source tree, then setup succeeded). The refusal text is `git grep -n 'install levain\[isolation\]' -- levain/slot/root_tree.py`.

The L2 guest also had `python3-venv python3-pip git` (apt), and the userns sysctl lifted, `acl`, `bubblewrap` and `sudo` from `l0_stage.sh`. Which of those setup itself requires was not isolated.

## 2. Run setup as root

```sh
sudo ~/venv/bin/python -m levain.slot.install_helper setup --slots <N> --verifier-slots <M> --operator <user>
```

- `--slots` is 1 to 32 and `--verifier-slots` is 1 to 16 (`_parse_setup` and `MAX_SLOTS`, `MAX_VERIFIER_SLOTS` in `levain/slot/grammar.py`). A fresh setup needs at least 3 slots (`MIN_SETUP_SLOTS`: one dispatch slot, one reserve, one free).
- `--operator` is the login name that becomes the machine's one operator. L2 used `admin`.
- `--root <absolute path>` may repeat, up to 16 times. All three of the other flags are required, and none may repeat.
- Run it from `/`. L2 did (`cd /`).
- The `sudo` is the part that needs root: the helper refuses with `not-root` when it does not run as root, and it reads `SUDO_UID`, which `sudo` sets (`git grep -n 'not-root' -- levain/slot/install_helper.py`). Nothing else in this document needs root.

L2 ran `--slots 4 --verifier-slots 1 --operator admin` and got, with rc=0:

```
{"ok":true,"slots":["e0","e1","e2","e3","v0"],"steps":364,"verb":"setup"}
```

Progress goes to stderr, one numbered line per step. A stderr line reading `pending: ... verb-unbuilt for: set-retention, set-dispatch-retain, set-signers, force-retire` was printed on every run and was informational in L2.

After it, L2 saw six systemd units active (`levain-control`, `levain-dispatch`, `levain-dispatch_run`, `levain-keys`, `levain-qbroker`, `levain-reaper`), the store under `/var/levain`, and the helpers under `/usr/libexec/levain/`.

## 3. Repair

`repair` re-applies setup's steps to an existing install. Steps already done report "already done". L2 ran it on the live install: rc=0, 252 steps, all six units still active.

```sh
sudo ~/venv/bin/python -m levain.slot.install_helper repair
```

Once a setup has run, the installed helper is the shorter form that Levain's own refusals name: `sudo /usr/libexec/levain/install-helper repair` (`repair_line` in `levain/slot/layout.py`). `repair` also takes `--source <absolute path>`, the operator's installed levain package directory.

## 4. Undo

```sh
sudo ~/venv/bin/python -m levain.slot.install_helper undo
```

L2 ran it: rc=0, 146 steps. The units were stopped, disabled and removed, `/run/levain` and the sudoers drop-ins went, and the operator record went last. It does not delete everything. The `_levain_*` accounts stay, retired (nologin, locked, no home), so a user id is never reused, and `/var/levain`, `/usr/lib/levain-root`, `/usr/libexec/levain` and `/var/lib/levain` were still present afterwards (L2_RUN.md, step 10). Whether that is intended for the store and code tree was not settled.

## What this page cannot tell you

- Ctrl-C part-way through `setup` on an interactive terminal, then re-running, was not run.
- `pipx install 'levain[isolation]'` was not run. The interpreter in the `sudo` line must be the one that has Levain and `h11`; for a pipx install that is the tool's own environment, and its path was not measured.
- Real vendor logins, dispatch and fire paths, and reboot persistence were not part of this run.

# Contributing to Levain

Thanks for looking. This file points at where each thing is written down rather than repeating it.

## Before you start

- **What Levain is and how it is put together:** the [README](README.md). *Build on it* covers `levain.kernel`, the
  published seam for writing your own surface.
- **A security problem:** report it privately, not in a public issue or discussion. [SECURITY.md](SECURITY.md) names the
  channel and what is in scope.

## Setting up a clone

Run the hooks script once per clone. The README explains what it installs and why, under *Working on levain itself*:

```
bash scripts/install_hooks.sh
```

## Running the tests

CI is the recipe: [`.github/workflows/test.yml`](.github/workflows/test.yml) installs with the line below and runs `pytest`
(in shards; the workflow splits the suite). The script installs from the committed `uv.lock`, hash-checked, then levain
itself editable; it needs `python` with `pip`, and installs `uv` and `pip` at pinned versions first.

```
scripts/ci_install_locked.sh dev openhands
```

Changing a dependency in `pyproject.toml` means regenerating the lock in the same commit: run `uv lock` with the `uv`
version the script pins, and `uv lock --check` (which CI runs first) confirms the two agree. A nightly job, `dep-drift`,
re-resolves every package to its newest allowed release in a scratch copy, runs the suite on that, and keeps one open
`dep-drift` issue while it is red; `python scripts/dep_drift.py` runs the same check on your machine.

Some tests need something your machine may not have and skip themselves where it is absent: macOS's `sandbox-exec`, a
live Ollama, the Linux image, a real Chrome (the `needs_browser` tests), Node with `srt` and `uv` (the chat egress
tests). CI runs several of these in their own jobs, and one more job runs root-run code on Python 3.9; plain `pytest`
runs none of those jobs for you. A skip is not a pass, so read the skip count in the output.

## Changes

- A change a user would notice gets an entry under `## [Unreleased]` in [CHANGELOG.md](CHANGELOG.md), in the format of
  the entries around it: a bold sentence saying what changed, then why and how it was checked.
- Release commits and version stamps are the maintainers'. See *Versioning* at the foot of the changelog before you touch
  a version number.

## License

Levain is Apache 2.0 ([LICENSE](LICENSE)). By contributing you agree your contribution is licensed under it.

# Contributing to Levain

Thanks for looking. This file points at where each thing is written down rather than repeating it.

## Before you start

- **What Levain is and how it is put together:** the [README](README.md). *Build on it* covers `levain.kernel`, the
  published seam for writing your own surface.

## Setting up a clone

Run the hooks script once per clone. The README explains what it installs and why, under *Working on levain itself*:

```
bash scripts/install_hooks.sh
```

## Running the tests

CI is the recipe: [`.github/workflows/test.yml`](.github/workflows/test.yml) installs the package with its test extras
and runs `pytest`. Some tests need macOS's `sandbox-exec`, a live Ollama or the Linux image and skip themselves where
those are absent; a skip is not a pass, so read the skip count in the output.

## Changes

- A change a user would notice gets an entry under `## [Unreleased]` in [CHANGELOG.md](CHANGELOG.md), in the format of
  the entries around it: a bold sentence saying what changed, then why and how it was checked.
- Release commits and version stamps are the maintainers'. See *Versioning* at the foot of the changelog before you touch
  a version number.

## License

Levain is Apache 2.0 ([LICENSE](LICENSE)). By contributing you agree your contribution is licensed under it.

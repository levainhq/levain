#!/usr/bin/env bash
# Install levain's git hooks. Run ONCE PER CLONE.
#
# ⚠ core.hooksPath, NOT .git/hooks/ — `.git/` is not tracked, so a hook dropped
# there dies with the clone and is invisible to every other machine and to
# review. Pointing at a TRACKED directory means the hook ships with the repo and
# there is exactly one copy of it.
# ⚠ It is still LOCAL config that git cannot ship, which is why this script
# exists rather than the hook being automatic. A fresh clone is UNGATED until
# someone runs this.
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"
git config core.hooksPath scripts/hooks
chmod +x scripts/hooks/* 2>/dev/null || true
echo "installed: core.hooksPath -> scripts/hooks"
echo "  pre-push → the release-stamp gate (fails closed; override is --no-verify)"

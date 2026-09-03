#!/usr/bin/env bash
# Install the lane pre-commit gate. Run ONCE PER CLONE (and per `git worktree add` that you intend
# to commit from).
#
# ⚠ WHY `core.hooksPath` AND NOT `.git/hooks/`: `.git/` is not tracked, so a hook dropped there dies
# with the clone and is invisible to every other machine. Pointing `core.hooksPath` at a TRACKED
# directory means the hook ships with the repo and there is exactly one copy of it.
# ⚠ IT IS STILL PER-CLONE — `core.hooksPath` is local config, which git cannot ship. That is a git
# constraint, not a choice, and it is why this script exists at all rather than the hook being
# automatic. A fresh clone with no hooks is UNGATED; run this before your first commit.
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"

git config core.hooksPath tools/hooks
chmod +x tools/hooks/* 2>/dev/null || true

echo "installed: core.hooksPath -> tools/hooks"
echo "  pre-commit → tools/verify_lane.sh --staged"
echo ""
echo "⚡ AND EXPORT YOUR LANE, which buys the third check the hook cannot infer:"
echo "     export LEVAIN_LANE=levain-walk      # or whichever lane this session owns"
echo "  Without it you still get: every committed path is owned, and owned exactly once."
echo "  With it you also get: every committed path is owned BY YOU."
echo ""
git config --get core.hooksPath >/dev/null && echo "verified: $(git config --get core.hooksPath)"

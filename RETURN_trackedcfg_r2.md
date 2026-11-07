# RETURN: tracked-activation L3 r2 fixes (seat/1107-15-trackedcfg)

Findings source: `tracked_r2.out` (codex OPEN #8 + residue, codex/deepseek introduced MEDs). All six brief items are fixed. Not pushed.

## Changes
1. **Tree refusal is told once** (`levain/seat_push.py`, `_tell_tree`, `_TREE_TOLD`). `tree_refusal()`'s check records `check.hit = (first path, judged tip)` only on an activation refusal. `run_hook` logs the refusal as before, then on a first `Stop` (not `stop_hook_active`, never `SessionEnd`) it remembers the judged tip against the grant (`egress_pushed/<lane>`, same mark as a rejection) and exits 2 with: "seat-egress: the lane branch changes an activation directory, which a seat may read and never change: <path>. Nothing was pushed. Drop the commits ...; this message appears once per stop." Other refusals (no base, non-descendant, too many paths, late) stay exit 0. The docs now say "two exceptions" (`docs/seat-profile.md`, seat egress), and so does the module docstring.
2. **The diff-tree reads are bounded** (`_mirror_paths`). stdin goes in from a temp file and stderr out to a temp file, so neither pipe can deadlock. stdout is streamed NUL-delimited with a byte count and a path count shared across both reads, and only activation hits are kept. Over `_TREE_MAX_PATHS` (200,000) or `_TREE_MAX_BYTES` (64 MiB), the refusal is "the lane branch changes too many paths to judge ...". git is killed on every early exit.
3. **One deadline** (`_JUDGE_BUDGET` = 30 s from the check's start, or the hook's own deadline, whichever comes first). The hook's deadline is the event ceiling (SessionEnd 60 / Stop 180) minus `_HOOK_RESERVE` (10 s). Every judge git call gets the remaining time (`_left`). On expiry or `TimeoutExpired` the check returns "the lane branch could not be judged in time (30 s): nothing is pushed", and run_hook writes the log line. `_tip`'s ls-remote timeout is also clamped to what remains.
4. **The walk counts candidates** (`levain/seat_profile.py`). `seen +=` now runs before the filtering, so candidates, protected names and the skipped `.git` all count, and the walk-cap check runs right after it. New `_CANDIDATE_CAP` = 256: over it the refusal is "<root> holds more than 256 activation directories to vet".
5. **The `.git` report is single.** The root `.git` is skipped in both the dirnames and filenames branches. The final check, `islink or not is_dir`, is the sole reporter. Wording change: a top-level `.git` that is a symlink to a directory is now reported as "<root>/.git is not a real directory" instead of as the bare path. It still refuses.
6. **Docs residue** (`docs/seat-profile.md`, "Where it stops"). It now says the deny on this path is UNMEASURED and that the machine guard forbids ad-hoc sandbox testing (never a hand-written sandbox profile; one kernel-panicked the operator's Mac). The cheapest proof is a real `levain seat launch` with a canary write probe into a tracked activation file, and Z2 owns that run after landing. The egress section also documents the path/byte caps and the 30 s deadline.

## Tests
- New in `tests/test_seat_push_tracked.py`:
  - told once, and never on SessionEnd or under stop_hook_active
  - dropping the commits then pushes
  - other refusals stay silent
  - the path cap, and the byte cap
  - the judge budget expiring
  - a git TimeoutExpired becomes "late"
  - the hook deadline bounds the judge
  - the streamed read honours the deadline
  - `assert_refused` now asserts exit 2 plus the fixed words plus the path, then a silent second Stop with no new log row.
- New in `tests/test_seat_profile_tracked.py`:
  - candidates count against the walk cap
  - the candidate cap
  - a top-level `.git` reported once, for 4 shapes
  - a real `.git` is not reported
- Mutation check (`tmp/mutate_r2.py`): 11/11 killed. Each mutation was a revert of one guard: the tell guard, the tell, the remember, the path cap, the byte cap, `_left`'s expiry, TimeoutExpired to late, the hook deadline min, the walk count position, the candidate cap, and the `.git` filename skip.
- Runs:
  - test_seat_push_tracked + test_seat_profile_tracked + test_seat_lease + every test importing seat_push/seat_profile (test_docs_seat_profile, test_doctor_seat_profile, test_receipt_and_upgrade_exit, test_seat_launch, test_seat_profile, test_seat_push) + test_slot_root_tree_vendor: **288 passed**.
  - Canary tests: **230 passed**.

## Judged not reachable / residue
- None of the six items was judged unreachable.
- Residue, outside the judge (pre-existing, not in this brief): `push.execute`'s fetch and push use `push._git`'s default 600 s timeout. A very slow fetch can therefore still outlast SessionEnd's 60 s ceiling before the judge starts, and the vendor would then kill the hook with no log line. Bounding it means passing a deadline into `push.execute`, which is shared with the consent push, so it is the desk's call.
- Complement's three LOWs were left as they are: the merge-of-upstream trap (documented as intended), the glob message wording, and the `.git/shallow` hardening (UNSETTLED; the likely outcome fails closed).

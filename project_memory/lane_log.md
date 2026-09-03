
## 2026-09-03 12:55 · levain-seat · observation
WHAT I SAW: K4c's cheap de-risk step (`spore-418`: *"a BASH-FREE entity is ALREADY cross-platform…
Verify Linux bash-free EARLY and CHEAP — verification, not building"*) is VERIFIED on the code
axis. `crown_jewel_reason`, `build_policy`, `_ci_within` and `_canon` contain no reference to
`platform.`, `sys.platform`, `Darwin` or `sandbox_exec`; the only platform branch in
`confinement.py` is `select_provider`. `tests/test_firing_confinement.py` → **92 passed, 1 skipped,
1 failed**, the failure being the known absent-`openhands`-extra ImportError, not ours.
⚠ Verified STATICALLY and by the macOS suite. It has NOT been run on an actual Linux host.

⚡ AND A PRE-EMPTIVE L0 FINDING FOR WHOEVER TAKES `levain-linux`, filed before the code exists
rather than after: `_ci_within`'s docstring closes with *"Over-matching here is FAIL-CLOSED …
`crown_jewel_reason` is a pure denylist, so there is no legitimate path this wrongly rejects."*
That sentence is TRUE on macOS/APFS, where a case-variant of a crown jewel IS the same on-disk
file. It becomes **FALSE on Linux/ext4**, where `~/.Anneal-Memory` and `~/.anneal-memory` are two
genuinely different files and the second would be denied on account of the first. The BEHAVIOUR
stays correct — over-denying is the safe direction and the floor is a denylist — but the CLAIM
stops holding the moment a Linux provider ships.
WHY IT MATTERS: this is the house class (a description that disagrees with correct code) arriving
on a schedule we can see. The fix is not to change `_ci_within` — it is to qualify the sentence
per-platform in the same commit that lands the bwrap provider, so the claim never spends a day
being false.
WHAT I DID: recorded it here rather than fixing it now — `confinement.py` is `levain-linux`'s file
and the correction belongs in the commit that makes it necessary. No lane is running, so this is
an observation for the launch, not an orphan: it is carried in the lane's SCOPE.

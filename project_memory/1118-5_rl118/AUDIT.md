# levain#118 audit: snapshot-plus-margin RLIMIT_NPROC (and other count limits) siblings

Result: no affected site beyond the one r117 fixed. 0 (a), 13 (b), 0 (c). No code changed.

Method: `git grep -n -i -E "RLIMIT_|setrlimit|nproc|pids.max|pids_max|clone_limits|_run_bounded|_clone_under_limits|ulimit|prlimit|preexec_fn"` over `levain` and `tests` at b91469486, then every caller of `clone_limits` / `_run_bounded` followed. Reading only; nothing was run against a real key, and no failure was reproduced because no site has the shape.

The shape that fails: a relative limit (count measured now + margin) applied in a child, with a window in which a same-uid process can take the headroom. `_uid_task_count` is the only measurement of a live count in the package; it has one consumer, `clone_limits`, and that has one caller, `_clone_under_limits`.

| # | file | symbol (quoted) | verdict | reason | checked |
|---|------|-----------------|---------|--------|---------|
| 1 | levain/dispatch/readset.py | `_clone_under_limits` / `clone_limits` | fixed by r117 | the snapshot-plus-margin site; retries only the refused fork, 3 tries, re-measured each time | read r117 diff and code |
| 2 | levain/dispatch/readset.py | `limits=(("RLIMIT_FSIZE", cap + BUNDLE_FSIZE_SLACK),)` (bundle create) | b | FSIZE is a byte cap from the fixed `cap`, not a measured state; no NPROC set, so no fork refusal from it | read; the only other `_run_bounded` caller |
| 3 | levain/dispatch/readset.py | `("RLIMIT_CPU", CLONE_CPU_S)`, `("RLIMIT_AS", CLONE_AS_BYTES)` | b | constants, not snapshots | read |
| 4 | levain/slot/wall.py | `NPROC = 2048` via `drop_and_assert(... nproc=...)` | b | absolute per-slot limit, set once by the stage after the drop; not derived from a count | read `_nproc_target`: min of constant, hard limit, `kern.maxprocperuid`, none a task count |
| 5 | levain/slot/wall.py | `EXPORT_NPROC = 64` | b | absolute constant; "its principal owns no other process", so no sibling to take it | read |
| 6 | levain/slot/wall.py | `_nproc_target` / `_darwin_maxprocperuid` | b | reads the system ceiling, a static sysctl, not the current task count | read |
| 7 | levain/slot/adopt_verify.py | `"pack.threads=2"` (GIT_C, "§6.3's RLIMIT_NPROC") | b | threads pinned to a constant so the fixed 64 holds; no measuring, no margin | read |
| 8 | levain/slot/runtime_helper.py | `ADOPT_PIDS_MAX = wall.EXPORT_NPROC` and `export_rlimits` | b | cgroup `pids.max` from a constant; `export_rlimits` is CORE/FSIZE/CPU only (test `test_the_export_rlimits_are_core_fsize_cpu...` asserts RLIMIT_NPROC absent) | read |
| 9 | levain/slot/export_run.py | `write("pids.max", str(pids_max))` | b | cgroup pids.max, not an rlimit; value from the fixed spec, not a snapshot | read |
| 10 | levain/slot/probe.py | `a fork failure (RLIMIT_NPROC, §8) is one unknown, never the wall` | b | no limit is derived here; a refused spawn is already mapped to a bounded UNKNOWN verdict (fail-safe by design) | read |
| 11 | levain/slot/proc.py | `A ``fork`` refused with EAGAIN` (`signal_child`) | b | already retries EAGAIN until its deadline; limit not derived from a snapshot | read |
| 12 | levain/slot/end.py, install_helper.py, stage.py, proc.py | `nproc=None` ("no RLIMIT_NPROC (the reader never forks)") | b | no NPROC set, child never forks | read |
| 13 | levain/broker/keys.py | `resource.setrlimit(resource.RLIMIT_NOFILE, (want, hard))` | b | NOFILE raised to a constant target (min(hard, NOFILE_TARGET)), not count-plus-margin, and not applied to a child | read |

Other `preexec_fn` users (canary, hands_chat, ws_git, supervisor, tests) set ctty, cwd or signals, no limits. Test helpers that set rlimits (`test_repair_handover`, `test_slot_s2c_end`) squeeze NOFILE in the test's own process and restore it; they start no child under a measured count. tests/test_shell_oob_status.py mentions RLIMIT_NPROC only in a docstring about zombie reaping.

Reachability note: row 5 and 8 depend on one principal never running two exports at once (the comment says it owns no other process). That is a leasing property, not this class; if it ever broke, the 64 would be exhausted by the principal's own tasks, and neither the limit nor a retry would be the fix.

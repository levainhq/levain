"""The local-run policy for the pytest suite: what a run off CI is allowed to cost the machine it runs on.

11-20 (np-b454f2f6): three full suites at once on the laptop (about 27 xdist workers) put the load at 118 and starved the
cockpit. CI runs the whole suite for free, so a run off CI (i) is refused when it is a bare full collection (the one-command
way to run it in CI is scripts/ci_full.py), (ii) runs its xdist workers at most LOCAL_WORKERS_MAX wide, and (iii) runs the
test process, and so every worker and child it spawns, at background QoS so it schedules on the efficiency cores.
Targeted runs (a path, a node id, -k, -m, --collect-only) stay allowed. CI is told by the environment the runner sets.

Overrides: LEVAIN_LOCAL_FULL=1 allows a bare full collection; LEVAIN_TEST_QOS=normal leaves the priority alone;
LEVAIN_LOCAL_WORKERS_MAX=N moves the worker cap. None of them applies on CI, where nothing here acts at all.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

LOCAL_WORKERS_MAX = 4
_PRIO_DARWIN_PROCESS = 4
_PRIO_DARWIN_BG = 0x1000


def _truthy(value):
    return str(value or "").strip().lower() not in ("", "0", "false", "no", "off")


def on_ci(env=None):
    """CI is told by the variables a runner sets; `CI=false` or `CI=0` (a shell exporting the name off) is not CI."""
    env = os.environ if env is None else env
    return _truthy(env.get("CI")) or _truthy(env.get("GITHUB_ACTIONS"))


def worker_cap(env=None):
    env = os.environ if env is None else env
    try:
        n = int(env.get("LEVAIN_LOCAL_WORKERS_MAX", LOCAL_WORKERS_MAX))
    except ValueError:
        return LOCAL_WORKERS_MAX
    return n if n >= 1 else LOCAL_WORKERS_MAX


def names_whole_tree(positional, invocation_dir, root_dir):
    """True when a positional argument is the whole suite: the tests directory, the rootdir, or a directory above them
    (a node id, a file, or a directory below tests/ narrows the run; `pytest tests/` does not)."""
    tests = (root_dir / "tests").resolve()
    for arg in positional:
        path = Path(str(arg).split("::", 1)[0])
        if not path.is_absolute():
            path = Path(invocation_dir) / path
        path = path.resolve()
        if path == tests or path in tests.parents:
            return True
    return False


def has_last_failed(root_dir):
    """True when pytest's cache holds failures to rerun: `--lf` on an empty or unreadable cache runs everything, so only then
    it narrows."""
    try:
        data = json.loads(Path(root_dir, ".pytest_cache", "v", "cache", "lastfailed").read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        return False
    return bool(data)


def inside_tests_subdir(invocation_dir, root_dir):
    """True when the run starts in a directory below tests/ with no path given: pytest then collects that directory only."""
    tests = (root_dir / "tests").resolve()
    here = Path(invocation_dir).resolve()
    return here != tests and tests in here.parents


def is_bare_full(positional, keyword, markexpr, collect_only, last_failed=False, whole_tree=False, in_subdir=False):
    """True when nothing narrows the run: no path or node id below tests/, no -k, no -m, no --collect-only, no --lf with
    failures to rerun, and not started from inside a tests/ subdirectory."""
    if keyword or markexpr or collect_only or last_failed:
        return False
    if not positional:
        return not in_subdir
    return whole_tree


def _is_info_run(ns):
    """--help, --version, --markers and --fixtures collect nothing."""
    return any(getattr(ns, name, False) for name in ("help", "version", "markers", "showfixtures", "fixtures"))


REFUSAL = (
    "a bare full-suite run is refused off CI (11-20, np-b454f2f6): it starves the cockpit. Run the full suite in CI on your "
    "pushed branch with `python3 scripts/ci_full.py`; run one file or one test here (`pytest tests/test_x.py`); or set "
    "LEVAIN_LOCAL_FULL=1 when you need the full suite on this machine."
)


def refuse_bare_full(config, env=None):
    """A message when this run is a bare full collection that nothing allows, else None. Never fires on CI, on an xdist
    worker (the controller already judged the run), or with LEVAIN_LOCAL_FULL=1."""
    env = os.environ if env is None else env
    if on_ci(env) or env.get("LEVAIN_LOCAL_FULL") == "1" or hasattr(config, "workerinput"):
        return None
    ns = config.known_args_namespace
    if _is_info_run(ns):
        return None
    positional = list(getattr(ns, "file_or_dir", None) or [])
    whole = names_whole_tree(positional, config.invocation_params.dir, config.rootpath)
    if is_bare_full(
        positional,
        getattr(ns, "keyword", ""),
        getattr(ns, "markexpr", ""),
        bool(getattr(ns, "collectonly", False)),
        bool(getattr(ns, "lf", False)) and not getattr(ns, "cacheclear", False) and has_last_failed(config.rootpath),
        whole,
        inside_tests_subdir(config.invocation_params.dir, config.rootpath),
    ):
        return REFUSAL
    return None


def cap_workers(config, env=None):
    """Hold xdist's own --maxprocesses at the local cap when xdist is loaded; a lower value the caller gave stays."""
    env = os.environ if env is None else env
    if on_ci(env) or not hasattr(config.option, "maxprocesses"):
        return
    cap = worker_cap(env)
    cur = config.option.maxprocesses
    if not cur or cur > cap:
        config.option.maxprocesses = cap


def background_qos(env=None, platform=None):
    """Put this process at background QoS (macOS: setpriority PRIO_DARWIN_BG, inherited by what it spawns). True when set.
    No-op on CI, off macOS, and with LEVAIN_TEST_QOS=normal."""
    env = os.environ if env is None else env
    platform = sys.platform if platform is None else platform
    if on_ci(env) or platform != "darwin" or env.get("LEVAIN_TEST_QOS") == "normal":
        return False
    try:
        import ctypes

        libc = ctypes.CDLL(None, use_errno=True)
        return libc.setpriority(_PRIO_DARWIN_PROCESS, 0, _PRIO_DARWIN_BG) == 0
    except Exception:
        return False     # a priority that cannot be set must never fail the run it was meant to quiet

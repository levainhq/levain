"""The launch re-exec and the children's environments (lane P2's research note, items 2a and 2b).

A process's environment as reported by the kernel (``/proc/<pid>/environ``, macOS ``KERN_PROCARGS2``
as ``ps -E`` prints it) is the one it was ``execve``'d with, so a token exported in the operator's
shell stayed readable to every process of the user, the entity's sandbox included, for the whole
session. These tests start real interpreters and read the kernel's report of them; nothing here
runs a sandbox.
"""
from __future__ import annotations

import ast
import os
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path

import pytest

from levain import launch

ROOT = Path(__file__).resolve().parent.parent
PKG = ROOT / "levain"
SECRET = "levain-test-secret-6f1d"


@pytest.fixture(autouse=True)
def _launch_state_is_per_test(monkeypatch):
    """launch keeps process-wide state (the secret-file registry, a lifted key). A test that adds to
    it left an API key file in every later build_policy's deny_files, which failed
    test_build_policy_does_not_guess_cred_files whenever this file ran first."""
    monkeypatch.setattr(launch, "_secret_files", [])
    monkeypatch.setattr(launch, "_lifted_api_key", None)
# The probes run with `python -c <source>`, and the source is itself in the argv the kernel reports,
# so the secret is spelled there in two halves.
_SPLIT = repr(SECRET[:9]) + " + " + repr(SECRET[9:])


def _kernel_view(pid_expr: str) -> str:
    """Python source that prints the kernel's report of process ``pid_expr``'s exec-time env + argv."""
    return (
        "import subprocess, sys\n"
        "if sys.platform.startswith('linux'):\n"
        f"    pid = {pid_expr}\n"
        "    view = open(f'/proc/{pid}/environ','rb').read() + open(f'/proc/{pid}/cmdline','rb').read()\n"
        "    view = view.decode('utf-8', 'replace')\n"
        "else:\n"
        f"    view = subprocess.run(['ps', '-E', '-ww', '-o', 'command=', '-p', str({pid_expr})],\n"
        "                          capture_output=True, text=True, env={'PATH': '/bin:/usr/bin'}).stdout\n"
    )


def _run(code: str, *args: str, env: dict[str, str]) -> subprocess.CompletedProcess:
    full = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "PYTHONPATH": str(ROOT), **env}
    return subprocess.run([sys.executable, "-c", code, *args], capture_output=True, text=True,
                          timeout=60, env=full)


_PROBE = (
    "import os, sys\n"
    "from levain import launch\n"
    # On Linux the restored image marks itself not dumpable, after which not even it can read its
    # own /proc/<pid>/environ; that has its own test. Here the re-exec alone is measured.
    "launch.set_not_dumpable = lambda: False\n"
    "before = os.getpid()\n"
    "launch.reexec_if_needed()\n"
    + _kernel_view("os.getpid()") +
    "print('REEXEC', launch.reexecuted)\n"
    "print('ENV', os.environ.get('GH_TOKEN'))\n"
    "print('FD', os.environ.get('LEVAIN_CARRY_FD'))\n"
    "print('KEY', launch.take_lifted_api_key())\n"
    "print('ARGS', sys.argv[1:])\n"
    "print('VISIBLE', " + _SPLIT + " in view)\n"
)


def test_a_token_in_the_launch_environment_leaves_the_kernel_view() -> None:
    r = _run(_PROBE, "run", "--model", "m", env={"GH_TOKEN": SECRET, "HOME": "/tmp"})
    assert r.returncode == 0, r.stderr
    out = dict(line.split(" ", 1) for line in r.stdout.splitlines())
    assert out["REEXEC"] == "True"
    assert out["ENV"] == SECRET, "the operator's environment is still levain's, in memory"
    assert out["FD"] == "None", "the carry descriptor's name is not left for children"
    assert out["ARGS"] == "['run', '--model', 'm']"
    assert out["VISIBLE"] == "False", "the kernel still reports the token"


def test_the_control_without_the_re_exec_shows_the_token() -> None:
    """The probe can fail: the same report, read without the re-exec, contains the token."""
    code = "import os\n" + _kernel_view("os.getpid()") + "print(" + _SPLIT + " in view)\n"
    r = _run(code, env={"GH_TOKEN": SECRET})
    assert r.stdout.strip() == "True", r.stderr


def test_an_api_key_on_the_command_line_is_lifted_out(capfd) -> None:
    r = _run(_PROBE, "run", "--api-key", SECRET, "--model", "m", env={"HOME": "/tmp"})
    out = dict(line.split(" ", 1) for line in r.stdout.splitlines())
    assert out["KEY"] == SECRET
    assert out["ARGS"] == "['run', '--model', 'm']"
    assert out["VISIBLE"] == "False"
    assert "--api-key-file" in r.stderr and r.stderr.count("\n") == 1


def test_the_equals_form_is_lifted_and_a_double_dash_ends_the_search() -> None:
    assert launch._lift_api_key(["run", f"--api-key={SECRET}", "x"]) == (["run", "x"], SECRET)
    assert launch._lift_api_key(["run", "--", "--api-key", "v"]) == (["run", "--", "--api-key", "v"], None)


def test_nothing_to_carry_means_no_re_exec() -> None:
    r = _run(_PROBE, "run", env={"HOME": "/tmp", "LANG": "C"})
    out = dict(line.split(" ", 1) for line in r.stdout.splitlines())
    assert out["REEXEC"] == "False", r.stderr


def test_a_carry_that_cannot_be_read_stops_levain_rather_than_run_without_it(monkeypatch) -> None:
    monkeypatch.setenv(launch.CARRY_FD_ENV, "999999")
    with pytest.raises(SystemExit) as exc:
        launch.reexec_if_needed()
    assert exc.value.code == 2


def test_child_env_is_the_allowlist_plus_what_was_asked(monkeypatch) -> None:
    monkeypatch.setenv("GH_TOKEN", SECRET)
    monkeypatch.setenv("HTTPS_PROXY", "http://u:p@proxy:3128")
    monkeypatch.setenv("PIP_INDEX_URL", "https://u:p@idx/simple")
    monkeypatch.setenv(launch.API_KEY_ENV, SECRET)
    monkeypatch.setenv("LC_ALL", "C")
    base = launch.child_env()
    assert "GH_TOKEN" not in base and "HTTPS_PROXY" not in base and launch.API_KEY_ENV not in base
    assert base["LC_ALL"] == "C" and "PATH" in base
    net = launch.child_env(*launch.NETWORK, prefixes=("PIP_",))
    assert net["HTTPS_PROXY"].startswith("http://") and "PIP_INDEX_URL" in net
    assert "GH_TOKEN" not in net and launch.API_KEY_ENV not in net


# --- every child levain starts gets an explicit, allowlisted environment ----------------------------
#
# 1023+2 head ruling (scan2a): outside levain/launch.py every reference to a spawner is the callee of a direct call
# that passes an explicit env; ANY other reference (a value, an alias, a default, a class attribute, an attribute
# store, an argument) is refused by file:line, except a parameter default named in _HELD_SEAMS: inside its function
# every call of that parameter passes env, and it goes nowhere but a parameter of a same-module helper, which the
# same rule covers (_seam_uses). No other alias is followed.
# This scan is a TRIPWIRE over the spawn shapes levain uses today, not a proof that no spawn escapes it. Known edges
# (a shape nobody writes; each measured by the grep named, run over levain/ on 1023, 0 hits unless stated):
# - asyncio spawners: grep -rn "create_subprocess_exec\|subprocess_exec" levain
# - pty spawners: grep -rn "pty\.spawn\|pty\.fork" levain
# - the posix module's exec/spawn: grep -rn "posix\.exec\|posix\.posix_spawn" levain
# - a spawner function aliased by assignment: grep -rnE "^\s*[A-Za-z_.]+ *= *subprocess\.[A-Za-z_]+ *(#.*)?$" levain
# - the os or subprocess MODULE bound to another name: grep -rnE "^\s*[A-Za-z_]+ *= *(subprocess|os) *(#.*)?$" levain
# - a spawner CALLED inside an annotation (runs at definition time):
#   grep -rnE "(:|->) *(subprocess|os)\.(run|Popen|call|check_[a-z]+|exec[a-z]*|spawn[a-z]*|posix_spawnp?)\(" levain
# - subprocess reached by getattr: grep -rn "getattr(subprocess" levain
# - the whole environment as a default: grep -rn "env=os\.environ" levain
# - a spawner reached through `getattr(os, name)` with a non-literal name: grep -rn "getattr(os, *[a-z_]" levain
#   (1 hit: _FlooredOs.__getattr__ in the OpenHands tools, a proxy, not a spawn; getattr(os, "O_NOFOLLOW", 0) is a constant)
# Two edges carried over from main unchanged, with their size, not 0:
# - env is judged by its TEXT: a name bound to the whole environment and later passed as env= is not traced.
#   Names bound to it: grep -rnE "= *(dict\()?os\.environ *(\)|$|#| if )" levain (16 on 1023); whether any reaches a
#   spawn's env is not checked here (a scan2b candidate).
# - levain/launch.py's direct os exec-family calls are not env-checked (_LAUNCH): grep -nE "os\.(exec|spawn|posix_spawn)"
#   levain/launch.py (1 on 1023).

_SUBPROCESS_ENV = {"run", "Popen", "call", "check_call", "check_output"}     # env= keyword
_SUBPROCESS_NO_ENV = {"getoutput", "getstatusoutput"}
# The env-taking os exec family: env's positional slot, and whether the API also takes it as `env=`
# (os.posix_spawn and os.posix_spawnp are positional-only) ...
_OS_ENV_AT = {"execve": (2, True), "execvpe": (2, True), "spawnve": (3, True), "spawnvpe": (3, True),
              "posix_spawn": (2, False), "posix_spawnp": (2, False)}
# ... and the `l` wrappers, which take it as their LAST positional, after this many leading ones (never `env=`).
_OS_ENV_LAST_AFTER = {"execle": 2, "execlpe": 2, "spawnle": 3, "spawnlpe": 3}
_OS_NO_ENV = {"execv", "execvp", "execl", "execlp", "spawnv", "spawnvp", "spawnl", "spawnlp", "system", "popen"}
_OS_SPAWNERS = set(_OS_ENV_AT) | set(_OS_ENV_LAST_AFTER) | _OS_NO_ENV
_LAUNCH = "levain/launch.py"     # the re-exec's home: its direct os exec-family calls are not env-checked (as on main)

# Spawners still held as parameter defaults (scan2 step 0, `1023-2/scan2/HELD_SITES.md`; scan2b converts them and
# empties this table): (module, function, parameter) -> the spawner it defaults to. Each must match exactly once.
_HELD_SEAMS = {
    ("levain/consent/publish.py", "execute", "run"): "subprocess.run",
    ("levain/host_canary.py", "_drive", "popen"): "subprocess.Popen",
    ("levain/slot/fsck_ids.py", "git_version_text", "run"): "subprocess.run",
    ("levain/slot/fsck_ids.py", "check_config_ids", "run"): "subprocess.run",
    ("levain/slot/install_helper.py", "_act", "run"): "subprocess.run",
    ("levain/slot/install_helper.py", "read_directory", "run"): "subprocess.run",
    ("levain/slot/install_helper.py", "read_primary_groups", "run"): "subprocess.run",
    ("levain/slot/install_helper.py", "gather_facts", "run"): "subprocess.run",
    ("levain/slot/pf.py", "release_enable_reference", "run"): "subprocess.run",
    ("levain/slot/pf.py", "take_enable_reference", "run"): "subprocess.run",
    ("levain/slot/pf.py", "load_recorded_anchor", "run"): "subprocess.run",
    ("levain/slot/stage.py", "make_bundle", "run"): "subprocess.run",
    ("levain/slot/supervisor.py", "repo_toplevel", "run"): "subprocess.run",
    ("levain/slot/supervisor.py", "operator_clone", "run"): "subprocess.run",
    ("levain/slot/supervisor.py", "repo_layer_docs", "run"): "subprocess.run",
    ("levain/slot/supervisor.py", "git_identity", "run"): "subprocess.run",
    ("levain/slot/units.py", "_launchd_job_absent", "run"): "subprocess.run",          # `run or subprocess.run`
    ("levain/slot/units.py", "_unload_launchd_if_changed", "run"): "subprocess.run",   # `run or subprocess.run`
    ("levain/slot/units.py", "_stop_unit_if_changed", "run"): "subprocess.run",        # `run = run or ...`
    ("levain/slot/wall.py", "read_netns_names", "run"): "subprocess.run",
    ("levain/slot/wall.py", "macos_egress_ready", "run"): "subprocess.run",
}

# The one named pass-through: (module, function, spawner, the env text it passes) -> why. Must match exactly once.
_PASS_THROUGH = {
    ("levain/slot/wall.py", "trampoline_main", "execve", "os.environb if environ is None else environ"):
        "the macOS last hop, run by the Seatbelt launcher after the profile: its env IS the one the stage built "
        "explicitly, because the stage execs it with an explicit env",
}


def _sources() -> list[Path]:
    # levain/templates/ holds hook scripts copied into other harnesses, which run them; they are not
    # processes levain starts.
    return [p for p in PKG.rglob("*.py") if "templates" not in p.relative_to(PKG).parts]


def _spawner(n: ast.AST) -> tuple[str, str] | None:
    """``("os", "execve")`` for a reference to a spawner (``os.execve``, ``x.os.execve``), else None."""
    if not isinstance(n, ast.Attribute):
        return None
    v = n.value
    mod = v.id if isinstance(v, ast.Name) else v.attr if isinstance(v, ast.Attribute) else None
    if mod == "subprocess" and n.attr in _SUBPROCESS_ENV | _SUBPROCESS_NO_ENV:
        return ("subprocess", n.attr)
    if mod == "os" and n.attr in _OS_SPAWNERS:
        return ("os", n.attr)
    if n.attr.startswith("create_subprocess"):
        return ("asyncio", n.attr)
    return None


def _env_problem(call: ast.Call, spawner: tuple[str, str], what: str) -> tuple[str | None, str]:
    """(the problem with ``call``'s env, or None; the env text found, "" if none)."""
    mod, name = spawner
    if (mod, name) in {("subprocess", n) for n in _SUBPROCESS_NO_ENV} or mod == "os" and name in _OS_NO_ENV:
        return f"{what} cannot take an environment", ""
    kw = next((k for k in call.keywords if k.arg == "env"), None)
    starred = [i for i, a in enumerate(call.args) if isinstance(a, ast.Starred)]
    env = None
    if mod == "os" and name in _OS_ENV_AT:
        at, keyword_ok = _OS_ENV_AT[name]
        if kw is not None and not keyword_ok:
            return f"{what} with env=: os.{name} takes it positionally only", ""
        if kw is not None:
            env = kw.value
        elif starred and starred[0] <= at:
            return f"{what} with a starred argument: the scan cannot find its env", ""
        elif len(call.args) > at:
            env = call.args[at]
    elif mod == "os":
        if kw is not None:
            return f"{what} with env=: os.{name} takes it as its last argument", ""
        if starred:
            return f"{what} with a starred argument: the scan cannot find its env", ""
        if len(call.args) > _OS_ENV_LAST_AFTER[name]:
            env = call.args[-1]
    elif kw is not None:
        env = kw.value
    if env is None:
        return f"{what} without env", ""
    text = ast.unparse(env)
    if text == "None" or "os.environ" in text:
        return f"{what} with env={text}", text
    return None, text


def _site_count_problems(matched: Counter, sites) -> list[str]:
    return [f"{site} matched {matched[site]} time(s), want exactly 1" for site in sites if matched[site] != 1]


def _spawn_problems(path: Path, root: Path = ROOT, held: dict | None = None, passthrough: dict | None = None,
                    matched: Counter | None = None) -> list[str]:
    held = _HELD_SEAMS if held is None else held
    passthrough = _PASS_THROUGH if passthrough is None else passthrough
    matched = Counter() if matched is None else matched
    rel = path.relative_to(root).as_posix()
    tree = ast.parse(path.read_text(encoding="utf-8"))
    parent = {c: p for p in ast.walk(tree) for c in ast.iter_child_nodes(p)}
    annotations: set[int] = set()   # a type annotation is not a reference
    for n in ast.walk(tree):
        for a in (getattr(n, "annotation", None), getattr(n, "returns", None)):
            if isinstance(a, ast.AST):
                annotations.update(id(x) for x in ast.walk(a))
    # module-level functions defined once and never rebound by an assignment: a hand-on can be resolved to them
    defined = Counter(n.name for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)))
    defined.update(t.id for n in tree.body if isinstance(n, (ast.Assign, ast.AnnAssign, ast.AugAssign))
                   for t in (n.targets if isinstance(n, ast.Assign) else [n.target]) if isinstance(t, ast.Name))
    funcs = {n.name: n for n in tree.body
             if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and defined[n.name] == 1}

    def func_of(n: ast.AST):
        while n in parent:
            n = parent[n]
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                return n
        return None

    out: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            names = {a.name for a in node.names}
            if (node.module in ("subprocess", "webbrowser")
                    or (node.module == "os" and names & (_OS_SPAWNERS | {"*"}))
                    or (node.module == "asyncio" and any(n.startswith("create_subprocess") for n in names))):
                out.append(f"{rel}:{node.lineno} `from {node.module} import`: "
                           "call it through the module so the scan sees the call")
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.asname and (a.name in ("subprocess", "webbrowser") or a.name == "os" and rel != _LAUNCH):
                    out.append(f"{rel}:{node.lineno} `import {a.name} as {a.asname}`")
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                and isinstance(node.func.value, ast.Name) and node.func.value.id == "webbrowser" \
                and path.name != "_browser.py":
            out.append(f"{rel}:{node.lineno} webbrowser.{node.func.attr}: open it through levain.launch.open_browser")
        sp = _spawner(node)
        if sp is None or id(node) in annotations:
            continue
        where = f"{rel}:{node.lineno} {sp[0]}.{sp[1]}"
        p = parent.get(node)
        if isinstance(p, ast.Call) and p.func is node:          # a direct call
            if rel == _LAUNCH and sp[0] == "os" and sp[1] not in _OS_NO_ENV:
                continue
            problem, env_text = _env_problem(p, sp, where)
            fn = func_of(p)
            site = (rel, getattr(fn, "name", ""), sp[1], env_text)
            if problem and site in passthrough:
                matched[site] += 1
                continue
            if problem:
                out.append(problem)
            continue
        seam = _held_site(node, p, parent, func_of)
        if seam is not None:
            fn, param = seam
            site = (rel, fn.name, param)
            if held.get(site) == f"{sp[0]}.{sp[1]}":
                matched[site] += 1
                out += _seam_uses(fn, param, sp, rel, held, parent, funcs)
                continue
        out.append(f"{where} referenced as a value: call it directly, with env")
    return out


def _held_site(node: ast.AST, p: ast.AST, parent: dict, func_of) -> tuple[ast.AST, str] | None:
    """(function, parameter) when ``node`` is a parameter's default (``run=subprocess.run``) or the right of
    ``run or subprocess.run`` on a parameter, called or rebound to that parameter; else None."""
    if isinstance(p, ast.arguments) and isinstance(parent.get(p), (ast.FunctionDef, ast.AsyncFunctionDef)):
        positional = p.posonlyargs + p.args
        if node in p.defaults:
            return parent[p], positional[len(positional) - len(p.defaults) + p.defaults.index(node)].arg
        if node in p.kw_defaults:
            return parent[p], p.kwonlyargs[p.kw_defaults.index(node)].arg
        return None
    if not (isinstance(p, ast.BoolOp) and isinstance(p.op, ast.Or) and len(p.values) == 2 and p.values[1] is node
            and isinstance(p.values[0], ast.Name)):
        return None
    fn, param = func_of(p), p.values[0].id
    if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return None
    a = fn.args
    if param not in {x.arg for x in a.posonlyargs + a.args + a.kwonlyargs}:
        return None
    q = parent.get(p)
    if isinstance(q, ast.Call) and q.func is p:
        return fn, param
    if isinstance(q, ast.Assign) and len(q.targets) == 1 and isinstance(q.targets[0], ast.Name) \
            and q.targets[0].id == param:
        return fn, param
    return None


def _seam_uses(fn: ast.AST, param: str, sp: tuple[str, str], rel: str, held: dict, parent: dict,
               funcs: dict, seen: set | None = None) -> list[str]:
    """Inside ``fn``, the table parameter ``param`` is only ever called with env (directly, or as ``(param or
    <spawner>)(...)``), rebound by ``param = param or <spawner>``, or handed to a parameter of a module-level
    function of the same module (defined once): a table entry is checked by its own row, any other such parameter
    by this same rule, once. Every other use is refused: stored, aliased, returned, passed anywhere else."""
    seen = set() if seen is None else seen
    seen.add((id(fn), param, sp))
    out: list[str] = []
    what = f"(table seam {param}, holds {sp[0]}.{sp[1]})"
    for u in ast.walk(fn):
        if not (isinstance(u, ast.Name) and u.id == param):
            continue
        p = parent[u]
        site_rebind = (isinstance(p, ast.Assign) and p.targets == [u] and isinstance(p.value, ast.BoolOp)
                       and _spawner(p.value.values[-1]) == sp)
        if not isinstance(u.ctx, ast.Load):
            if not site_rebind:
                out.append(f"{rel}:{u.lineno} `{param}` rebound {what}")
            continue
        call = p if isinstance(p, ast.Call) and p.func is u else None
        if isinstance(p, ast.BoolOp) and p.values[0] is u and len(p.values) == 2 and _spawner(p.values[1]) == sp:
            q = parent.get(p)
            if isinstance(q, ast.Assign) and len(q.targets) == 1 and isinstance(q.targets[0], ast.Name) \
                    and q.targets[0].id == param:
                continue   # `run = run or subprocess.run`: the site itself
            call = q if isinstance(q, ast.Call) and q.func is p else None
        if call is not None:
            problem, _ = _env_problem(call, sp, f"{rel}:{u.lineno} {param}(...) {what}")
            if problem:
                out.append(problem)
            continue
        handed = _handed_on(u, p, parent, funcs)
        if handed is None:
            out.append(f"{rel}:{u.lineno} `{param}` used other than as a call {what}")
            continue
        callee, q = handed
        if (rel, callee.name, q) not in held and (id(callee), q, sp) not in seen:
            out += _seam_uses(callee, q, sp, rel, held, parent, funcs, seen)
    return out


def _handed_on(u: ast.Name, p: ast.AST, parent: dict, funcs: dict) -> tuple[ast.AST, str] | None:
    """(callee, parameter) when ``u`` is an argument of a call to a module-level function of the same module (defined
    once), landing on a named parameter; else None."""
    if isinstance(p, ast.keyword) and p.arg is not None:
        call, name = parent[p], p.arg
    elif isinstance(p, ast.Call) and u in p.args:
        call, name = p, None
        if any(isinstance(a, ast.Starred) for a in p.args[:p.args.index(u)]):
            return None
    else:
        return None
    callee = funcs.get(call.func.id) if isinstance(call.func, ast.Name) else None
    if callee is None:
        return None
    a = callee.args
    if name is None:
        params = a.posonlyargs + a.args
        i = call.args.index(u)
        return (callee, params[i].arg) if i < len(params) else None
    return (callee, name) if name in {x.arg for x in a.args + a.kwonlyargs} else None


def test_every_child_levain_starts_is_given_an_explicit_environment() -> None:
    matched: Counter = Counter()
    problems = [p for src in _sources() for p in _spawn_problems(src, matched=matched)]
    problems += _site_count_problems(matched, [*_HELD_SEAMS, *_PASS_THROUGH])
    assert problems == [], "\n".join(problems)


def test_no_child_environment_is_built_from_a_copy_of_os_environ() -> None:
    pattern = re.compile(r"os\.environ\.(items|copy)\(\)|dict\(os\.environ|\*\*os\.environ")
    hits = [f"{p.relative_to(ROOT)}:{i}" for p in _sources() if p.name != "launch.py"
            for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1)
            if pattern.search(line) and not line.lstrip().startswith("#")]
    assert hits == [], "use levain.launch.child_env: " + ", ".join(hits)


def _scan(tmp_path: Path, source: str, held: dict | None = None, passthrough: dict | None = None):
    bad = tmp_path / "bad.py"
    bad.write_text(source)
    matched: Counter = Counter()
    problems = _spawn_problems(bad, tmp_path, held or {}, passthrough or {}, matched)
    return problems, matched


def _lines(problems: list[str]) -> set[int]:
    return {int(p.split(":")[1].split()[0]) for p in problems}


def test_the_scan_catches_a_call_without_env(tmp_path: Path) -> None:
    problems, _ = _scan(tmp_path, "import subprocess, os\nsubprocess.run(['x'])\nos.system('x')\n"
                        "subprocess.Popen(['x'], env=os.environ)\nos.execv('x', ['x'])\n"
                        "subprocess.run(['x'], env={})\nsubprocess.run(['x'], env=None)\n"
                        "subprocess.run(['x'], env=os.environ | {'A': 'b'})\nimport webbrowser\n"
                        "webbrowser.open('u')\nfrom subprocess import run\nimport subprocess as sp\n")
    assert _lines(problems) == {2, 3, 4, 5, 7, 8, 10, 11, 12}, "\n".join(problems)


def test_the_scan_refuses_every_spawner_held_as_a_value(tmp_path: Path) -> None:
    problems, _ = _scan(tmp_path, (
        "import subprocess, os\n"
        "def g(run=subprocess.run):\n"                        # 2: a default not in the table
        "    return run(['x'], env={})\n"
        "class A:\n"
        "    ex: object = os.execve\n"                         # 5: annotated class attribute
        "    run = subprocess.run\n"                           # 6: unannotated class attribute
        "R = subprocess.Popen\n"                               # 7: alias
        "def h(self):\n"
        "    self.run = subprocess.run\n"                      # 9: attribute store
        "    print(subprocess.run)\n"                          # 10: passed as an argument
        "import os as o\n"                                     # 11: import alias
        "from subprocess import run\n"                         # 12: from-import
        "from os import execve\n"                              # 13: from-import of an os spawner
        "def k(run=None):\n"
        "    return (run or subprocess.run)(['x'], env={})\n"  # 15: `or` on a parameter not in the table
        "def ann(p: subprocess.Popen) -> subprocess.Popen:\n"  # 16: annotations are not references
        "    return p\n"))
    assert _lines(problems) == {2, 5, 6, 7, 9, 10, 11, 12, 13, 15}, "\n".join(problems)


def test_the_env_is_found_where_each_api_takes_it(tmp_path: Path) -> None:
    problems, _ = _scan(tmp_path, (
        "import os, subprocess\n"
        "subprocess.run(['x'], env={})\n"                      # 2: clean
        "os.execve('x', ['x'], {})\n"                          # 3: clean (positional slot)
        "os.execve('x', ['x'], env={})\n"                      # 4: clean (execve takes env=)
        "os.posix_spawn('x', ['x'], {})\n"                     # 5: clean
        "os.execle('x', 'x', {})\n"                            # 6: clean (last positional)
        "os.spawnve(0, 'x', ['x'], {})\n"                      # 7: clean
        "os.posix_spawn('x', ['x'], env={})\n"                 # 8: positional-only API given env=
        "os.execle('x', 'x', env={})\n"                        # 9: an `l` wrapper given env=
        "os.execve('x', ['x'])\n"                              # 10: no env
        "os.execve('x', *a)\n"                                 # 11: starred: env not findable
        "os.execve('x', ['x'], os.environ)\n"                  # 12: the inherited env
        "subprocess.run(['x'], **kw)\n"))                      # 13: env not findable
    assert _lines(problems) == {8, 9, 10, 11, 12, 13}, "\n".join(problems)


def test_a_table_seam_is_called_with_env_inside_its_function_and_goes_nowhere_else(tmp_path: Path) -> None:
    names = ("f", "g", "h", "u", "s", "onward")
    held = {("bad.py", n, "run"): "subprocess.run" for n in names}
    problems, matched = _scan(tmp_path, (
        "import subprocess\n"
        "def f(run=subprocess.run):\n"
        "    return run(['x'], env={})\n"                      # 3: clean
        "def g(run=subprocess.run):\n"
        "    return run(['x'])\n"                              # 5: called without env
        "def h(run=subprocess.run):\n"
        "    return helper(run)\n"                             # 7: handed to a same-module helper: followed
        "def helper(run):\n"
        "    return run\n"                                     # 9: returned, in the helper
        "def u(run=None):\n"
        "    run = run or subprocess.run\n"                    # 11: the site itself
        "    run(['x'], env={})\n"                             # 12: clean
        "    return run\n"                                     # 13: returned
        "def s(self, run=subprocess.run):\n"
        "    self.r = run\n"                                   # 15: stored
        "def onward(run=subprocess.run):\n"
        "    f(run=run)\n"                                     # 17: handed to a seam: clean
        "    alias = run\n"                                    # 18: aliased
        "    _go(['x'], run=run)\n"                            # 19: handed to a helper that calls it with env
        "    twice(run)\n"                                     # 20: a helper name bound twice: not followed
        "def _go(argv, *, run):\n"
        "    return run(argv, env={})\n"
        "def twice(run):\n"
        "    return run(['x'], env={})\n"
        "twice = None\n"),
        held)
    assert _lines(problems) == {5, 9, 13, 15, 18, 20}, "\n".join(problems)
    assert _site_count_problems(matched, held) == []


def test_the_pass_through_matches_one_named_call(tmp_path: Path) -> None:
    src = "import os\ndef t(environ=None):\n    os.execve('x', ['x'], os.environb if environ is None else environ)\n"
    site = ("bad.py", "t", "execve", "os.environb if environ is None else environ")
    problems, matched = _scan(tmp_path, src, passthrough={site: "why"})
    assert problems == [] and _site_count_problems(matched, [site]) == []
    problems, _ = _scan(tmp_path, src)
    assert _lines(problems) == {3}, "without the table entry it is an inherited env"
    _, matched = _scan(tmp_path, src + src.replace("import os\n", ""), passthrough={site: "why"})
    assert len(_site_count_problems(matched, [site])) == 1, "two calls"


def test_the_site_count_wants_exactly_one() -> None:
    site = ("m.py", "f", "run")
    assert _site_count_problems(Counter({site: 1}), [site]) == []
    assert len(_site_count_problems(Counter(), [site])) == 1
    assert len(_site_count_problems(Counter({site: 2}), [site])) == 1


# --- the console entry --------------------------------------------------------------------------


def test_both_entries_go_through_launch() -> None:
    assert 'levain = "levain.launch:main"' in (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert "from levain.launch import main" in (PKG / "__main__.py").read_text(encoding="utf-8")


# --- the API key off the command line ---------------------------------------------------------------


def _args(**kw):
    import argparse
    return argparse.Namespace(**{"api_key": None, "api_key_file": None, **kw})


def test_api_key_file_is_read_and_a_loose_one_refused(tmp_path: Path, capsys) -> None:
    from levain.cli import _resolve_api_key

    f = tmp_path / "key"
    f.write_text(SECRET + "\n")
    f.chmod(0o644)
    a = _args(api_key_file=str(f))
    assert _resolve_api_key(a) is False
    assert "chmod 600" in capsys.readouterr().err
    f.chmod(0o600)
    a = _args(api_key_file=str(f))
    assert _resolve_api_key(a) is True and a.api_key == SECRET
    assert f in launch.secret_files(), "the floor denies the key file to the entity"


def test_api_key_from_the_environment_is_used_once(monkeypatch) -> None:
    from levain.cli import _resolve_api_key

    monkeypatch.setenv(launch.API_KEY_ENV, SECRET)
    a = _args()
    assert _resolve_api_key(a) and a.api_key == SECRET
    assert launch.API_KEY_ENV not in os.environ, "removed, so no child inherits it"


def test_a_lifted_key_is_used_and_two_sources_refuse(monkeypatch, tmp_path: Path) -> None:
    from levain.cli import _resolve_api_key

    monkeypatch.setattr(launch, "_lifted_api_key", SECRET)
    a = _args()
    assert _resolve_api_key(a) and a.api_key == SECRET
    f = tmp_path / "key"
    f.write_text("x")
    f.chmod(0o600)
    assert _resolve_api_key(_args(api_key="y", api_key_file=str(f))) is False


def test_the_cli_takes_api_key_file_on_run_wrap_and_serve() -> None:
    from levain import cli

    src = (PKG / "cli.py").read_text(encoding="utf-8")
    for var in ("run_p", "wrap_p", "web_p"):
        assert re.search(rf'{var}\.add_argument\(\s*"--api-key-file"', src), var
    for fn in ("_cmd_run", "_cmd_wrap", "_cmd_serve"):
        body = ast.get_source_segment(src, next(n for n in ast.parse(src).body
                                                if isinstance(n, ast.FunctionDef) and n.name == fn))
        assert "_resolve_api_key(args)" in body, fn
    assert cli._resolve_api_key


def test_the_browser_opens_from_a_child_with_the_allowlist_and_the_url_on_stdin(monkeypatch) -> None:
    import levain.web_server as ws

    monkeypatch.setenv("GH_TOKEN", SECRET)
    monkeypatch.setenv("BROWSER", "firefox")
    seen = []
    monkeypatch.setattr(launch.subprocess, "run", lambda argv, **kw: seen.append((argv, kw)))
    ws._open_browser("http://h/", "http://h/#chat_token=t")
    [(argv, kw)] = seen
    assert argv[-2:] == ["-m", "levain._browser"] and not any("chat_token" in a for a in argv)
    assert "chat_token=t" in kw["input"]
    assert "GH_TOKEN" not in kw["env"] and kw["env"]["BROWSER"] == "firefox"
    assert kw["stdout"] is launch.subprocess.DEVNULL, "a browser inheriting a pipe would hold the call"


def test_token_shaped_names_in_an_allowed_namespace_are_carried() -> None:
    for name in ("LEVAIN_TEAM_TOKEN", "ANNEAL_API_KEY", "VAGUS_SECRET", "LEVAIN_SERVER_PASSWORD"):
        assert not launch.allowed(name), name
    for name in ("LEVAIN_HOME", "LEVAIN_SCOPE", "ANNEAL_MEMORY_DERIVE_TRUST", "LD_LIBRARY_PATH", "PWD"):
        assert launch.allowed(name), name
    assert launch.allowed_env({b"PATH": b"/bin", b"GH_TOKEN": b"x"}) == {b"PATH": b"/bin"}


def test_an_unrebuildable_command_line_refuses_to_start(monkeypatch, capsys) -> None:
    monkeypatch.setenv("GH_TOKEN", SECRET)
    monkeypatch.delenv(launch.CARRY_FD_ENV, raising=False)
    monkeypatch.setattr(launch.sys, "argv", ["levain", "run"])
    monkeypatch.setattr(launch.sys, "orig_argv", ["python", "something-else"])
    with pytest.raises(SystemExit) as exc:
        launch.reexec_if_needed()
    assert exc.value.code == 2 and "not starting" in capsys.readouterr().err


def test_the_macos_carry_file_has_no_name_while_it_holds_data(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(launch, "CARRY_DIR", tmp_path / "carry")
    monkeypatch.delattr(launch.os, "memfd_create", raising=False)
    fd = launch._carry_fd(b"payload")
    try:
        assert list((tmp_path / "carry").iterdir()) == []
        assert (os.stat(tmp_path / "carry").st_mode & 0o777) == 0o700
        assert os.read(fd, 100) == b"payload"
    finally:
        os.close(fd)


def test_the_carry_dir_is_under_a_crown_jewel(tmp_path: Path, monkeypatch) -> None:
    from levain.firing.confinement import build_policy, crown_jewel_reason

    monkeypatch.setenv("HOME", str(tmp_path))
    ent = tmp_path / "ent"
    (ent / ".levain").mkdir(parents=True)
    assert crown_jewel_reason(build_policy(ent), launch.CARRY_DIR.expanduser() / "c-x") is not None


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="prctl is Linux")
def test_the_restored_carry_arrives_in_a_process_already_not_dumpable() -> None:
    code = ("import ctypes, os\nfrom levain import launch\nlaunch.reexec_if_needed()\n"
            "print(ctypes.CDLL(None).prctl(3, 0, 0, 0, 0), launch.reexecuted)\n")
    r = _run(code, env={"GH_TOKEN": SECRET, "HOME": "/tmp"})
    assert r.stdout.split() == ["0", "True"], r.stderr


def test_a_lifted_key_on_a_command_without_the_option_is_rejected(monkeypatch, capsys) -> None:
    """L3 r2 (complement): the re-exec lifts --api-key before argparse runs, so a command that has no
    such option dropped the key silently instead of rejecting it."""
    from levain import cli

    monkeypatch.setattr(launch, "_lifted_api_key", SECRET)
    with pytest.raises(SystemExit) as exc:
        cli.main(["doctor"])
    assert exc.value.code == 2
    assert "--api-key" in capsys.readouterr().err

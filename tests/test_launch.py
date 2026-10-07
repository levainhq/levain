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
from pathlib import Path

import pytest

from levain import launch

ROOT = Path(__file__).resolve().parent.parent
PKG = ROOT / "levain"
SECRET = "levain-test-secret-6f1d"
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

_SPAWN = {("subprocess", n) for n in ("run", "Popen", "call", "check_call", "check_output")}
_ENV_EXEC = {"execve", "execvpe", "execle", "execlpe", "spawnve", "spawnvpe", "spawnle", "spawnlpe",
             "posix_spawn", "posix_spawnp"}
_NO_ENV = {("subprocess", "getoutput"), ("subprocess", "getstatusoutput"), ("os", "system"),
           ("os", "popen")} | {("os", n) for n in ("execv", "execvp", "execl", "execlp", "spawnv",
                                                    "spawnvp", "spawnl", "spawnlp")}


def _sources() -> list[Path]:
    # levain/templates/ holds hook scripts copied into other harnesses, which run them; they are not
    # processes levain starts.
    return [p for p in PKG.rglob("*.py") if "templates" not in p.relative_to(PKG).parts]


def _spawn_problems(path: Path, root: Path = ROOT) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out = []
    for node in ast.walk(tree):
        # Aliases would hide a call from the scan below: `from subprocess import run`, `import
        # subprocess as sp`, `from webbrowser import open`.
        if isinstance(node, ast.ImportFrom):
            names = {a.name for a in node.names}
            spawners = {n for _, n in _NO_ENV} | _ENV_EXEC
            if (node.module in ("subprocess", "webbrowser")
                    or (node.module == "os" and names & spawners)
                    or (node.module == "asyncio" and any(n.startswith("create_subprocess") for n in names))):
                out.append(f"{path.relative_to(root)}:{node.lineno} `from {node.module} import`: "
                           "call it through the module so the scan sees the call")
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name in ("subprocess", "webbrowser") and a.asname:
                    out.append(f"{path.relative_to(root)}:{node.lineno} `import {a.name} as {a.asname}`")
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        if isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name):
            key = (f.value.id, f.attr)
        elif isinstance(f, ast.Attribute) and isinstance(f.value, ast.Attribute) and f.attr.startswith("create_subprocess"):
            key = ("asyncio", f.attr)
        else:
            continue
        where = f"{path.relative_to(root)}:{node.lineno}"
        if key[0] == "webbrowser" and path.name != "_browser.py":
            out.append(f"{where} webbrowser.{key[1]}: open it through levain.launch.open_browser")
            continue
        if key in _NO_ENV:
            out.append(f"{where} {key[0]}.{key[1]} cannot take an environment")
        elif key in _SPAWN or key[0] == "asyncio" and key[1].startswith("create_subprocess"):
            env = next((k.value for k in node.keywords if k.arg == "env"), None)
            if env is None:
                out.append(f"{where} {key[0]}.{key[1]} without env=")
            elif ast.unparse(env) == "None" or "os.environ" in ast.unparse(env):
                out.append(f"{where} {key[0]}.{key[1]} with env={ast.unparse(env)}")
        elif key[0] == "os" and key[1] in _ENV_EXEC and path.name != "launch.py":
            out.append(f"{where} os.{key[1]}: only levain/launch.py re-executes")
    return out


def test_every_child_levain_starts_is_given_an_explicit_environment() -> None:
    problems = [p for src in _sources() for p in _spawn_problems(src)]
    assert problems == [], "\n".join(problems)


def test_no_child_environment_is_built_from_a_copy_of_os_environ() -> None:
    pattern = re.compile(r"os\.environ\.(items|copy)\(\)|dict\(os\.environ|\*\*os\.environ")
    hits = [f"{p.relative_to(ROOT)}:{i}" for p in _sources() if p.name != "launch.py"
            for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1)
            if pattern.search(line) and not line.lstrip().startswith("#")]
    assert hits == [], "use levain.launch.child_env: " + ", ".join(hits)


def test_the_scan_catches_a_call_without_env(tmp_path: Path) -> None:
    bad = tmp_path / "bad.py"
    bad.write_text("import subprocess, os\nsubprocess.run(['x'])\nos.system('x')\n"
                   "subprocess.Popen(['x'], env=os.environ)\nos.execv('x', ['x'])\n"
                   "subprocess.run(['x'], env={})\nsubprocess.run(['x'], env=None)\n"
                   "subprocess.run(['x'], env=os.environ | {'A': 'b'})\nimport webbrowser\n"
                   "webbrowser.open('u')\nfrom subprocess import run\nimport subprocess as sp\n")
    assert len(_spawn_problems(bad, tmp_path)) == 9


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

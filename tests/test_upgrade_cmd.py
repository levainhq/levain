"""``levain upgrade`` (design 1028-7 §8). Every path runs through the seams: no root, installer or sudo is invoked,
and each test builds the failing state first, then checks the text, the exit code and the runner/exec call lists."""
from __future__ import annotations

import json
import os
import re
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

from levain import cli, upgrade as U
from levain.slot import Refused
from levain.slot import install_helper as IH

OP_UID = 501
GEN = "gen-" + "a" * 16
HELPER = Path("/usr/libexec/levain/install-helper")
RECORD = Path("/var/levain/policy/operator")


class Runner:
    def __init__(self, sudo=(0, '{"ok":true}\n', ""), pip=(0, "", "")):
        self.calls, self.kw, self.sudo, self.pip = [], [], sudo, pip

    def __call__(self, argv, *, capture_stderr=True, capture_stdout=False, env=None, cwd=None):
        self.calls.append(list(argv))
        self.kw.append({"env": env, "cwd": cwd})
        return self.sudo if argv[0] == U.SUDO else self.pip


class Dist:
    def __init__(self, root: Path, installer="pip", direct=None, owns=True):
        self.root, self.installer, self.direct, self.owns = root, installer, direct, owns

    def locate_file(self, rel):
        return (self.root / rel) if self.owns else Path("/nonexistent") / rel

    def read_text(self, name):
        if name == "INSTALLER":
            return None if self.installer is None else self.installer + "\n"
        if name == "direct_url.json":
            return None if self.direct is None else json.dumps(self.direct)
        return None


class Env:
    def __init__(self, tmp: Path):
        self.tmp = tmp
        self.parent = tmp / "site"
        for rel in ("levain/__init__.py", "h11/_version.py"):
            (self.parent / rel).parent.mkdir(parents=True, exist_ok=True)
        (self.parent / "levain/__init__.py").write_text("")
        (self.parent / "h11/_version.py").write_text('__version__ = "0.16.0"\n')
        self.pkg = str(self.parent / "levain")
        self.fs = {str(RECORD): (0, 0o644, stat.S_IFREG), str(HELPER): (0, 0o755, stat.S_IFREG)}
        self.runner, self.execs, self.exec_envs = Runner(), [], []
        self.euid, self.uid, self.tty = 1000 + 0, OP_UID, True
        self.installer, self.direct, self.owns = "pip", None, True
        self.markers: set[str] = set()
        self.prefix = str(tmp / "venv")
        self.base_prefix = "/usr"
        self.user_site = str(tmp / "usersite")
        self.stdlib = str(tmp / "stdlib")
        self.held = None
        self.held_lines = lambda h, p: [f"cmd{i} {p}" for i in range(1, 6)]
        self.read_dir = lambda host: SimpleNamespace(groups={})
        self.writers = lambda parent, uid, d: None
        self.current = "gen-" + "0" * 16
        self.op_uid = OP_UID
        self.which_map = {"pipx": "/bin/pipx", "uv": "/bin/uv"}
        self.exe_exists = True

    def lstat(self, p):
        if p not in self.fs:
            raise FileNotFoundError(p)
        uid, mode, kind = self.fs[p]
        return SimpleNamespace(st_uid=uid, st_mode=kind | mode, st_gid=0)

    def exec_(self, path, argv, env=None):
        self.execs.append((path, argv))
        self.exec_envs.append(env)

    def seams(self) -> U.Seams:
        return U.Seams(
            host="linux", geteuid=lambda: self.euid, getuid=lambda: self.uid, has_tty=lambda: self.tty,
            record_path=RECORD, helper_path=HELPER, lstat=self.lstat,
            read_record=lambda p: IH.operator_record_text("op", self.op_uid).encode(),
            pkg=self.pkg, dist=lambda: Dist(self.parent, self.installer, self.direct, self.owns),
            prefix=self.prefix, base_prefix=self.base_prefix, executable="/py/bin/python",
            exe_exists=lambda p: self.exe_exists, user_site=lambda: self.user_site, stdlib=self.stdlib,
            marker_exists=lambda p: os.path.basename(p) in self.markers, which=self.which_map.get,
            run=self.runner, execv=self.exec_,
            held_row=lambda: self.held, held_lines=lambda h, p: self.held_lines(h, p),
            read_directory=lambda host: self.read_dir(host), check_writers=lambda *a: self.writers(*a),
            gen_id=lambda a, b: GEN, current_gen=lambda h: self.current, version="9.9.9")

    def run(self, capsys, **kw):
        rc = U.run_upgrade(seams=self.seams(), **kw)
        return rc, capsys.readouterr().out


@pytest.fixture
def env(tmp_path):
    return Env(tmp_path)


def sudo_calls(env):
    return [c for c in env.runner.calls if c[0] == U.SUDO]


def nothing_ran(env):
    assert env.runner.calls == [] and env.execs == []


# 1
def test_root_is_refused(env, capsys):
    env.euid = 0
    rc, out = env.run(capsys, no_pip=True)
    assert rc == 1 and "not with sudo" in out
    nothing_ran(env)


# 2
def test_another_operators_install_names_them(env, capsys):
    env.op_uid = OP_UID + 1
    rc, out = env.run(capsys, no_pip=True)
    assert rc == 1 and "belongs to op" in out and "Run this as op" in out
    nothing_ran(env)


# 3
def test_no_isolation_install_runs_pip_and_stops(env, capsys):
    env.fs.clear()
    env.markers = set()
    rc, out = env.run(capsys)
    assert rc == 0
    assert env.runner.calls == [["/py/bin/python", "-m", "pip", "install", "-U", "levain[isolation]"]]
    assert sudo_calls(env) == []
    assert env.execs and env.execs[0][1][-2:] == ["upgrade", "--no-pip"]
    env.runner.calls.clear()
    rc, out = env.run(capsys, no_pip=True)
    assert rc == 0 and "No Levain isolation install" in out and env.runner.calls == []


# 4
@pytest.mark.parametrize("drop, which", [(str(HELPER), "install-helper"), (str(RECORD), "operator")])
def test_half_set_up_is_refused(env, capsys, drop, which):
    del env.fs[drop]
    rc, out = env.run(capsys)
    assert rc == 1 and "incomplete" in out and which in out and "levain doctor" in out
    nothing_ran(env)


# 5
@pytest.mark.parametrize("entry", [(0, 0o777, stat.S_IFLNK), (1000, 0o755, stat.S_IFREG), (0, 0o775, stat.S_IFREG),
                                   (0, 0o757, stat.S_IFREG)])
def test_an_unsafe_helper_is_refused(env, capsys, entry):
    env.fs[str(HELPER)] = entry
    rc, out = env.run(capsys)
    assert rc == 1 and str(HELPER) in out and "root-owned file only root can change" in out
    nothing_ran(env)


# 6
def test_no_tty_is_refused_before_pip_and_dry_run_is_exempt(env, capsys):
    env.tty = False
    rc, out = env.run(capsys)
    assert rc == 1 and "no terminal" in out
    nothing_ran(env)
    rc, out = env.run(capsys, dry_run=True)
    assert rc == 0 and "dry run: nothing was changed" in out
    nothing_ran(env)


# 7
PY = "/py/bin/python"
SPEC = "levain[isolation]"


@pytest.mark.parametrize("name, setup, argv", [
    ("pipx", lambda e: e.markers.add("pipx_metadata.json"), ["/bin/pipx", "upgrade", "levain"]),
    ("uv-tool", lambda e: e.markers.add("uv-receipt.toml"), ["/bin/uv", "tool", "upgrade", "levain"]),
    ("uv-pip", lambda e: setattr(e, "installer", "uv"),
     ["/bin/uv", "pip", "install", "-U", SPEC, "--python", PY]),
    ("pip-venv", lambda e: None, [PY, "-m", "pip", "install", "-U", SPEC]),
])
def test_installer_rows_give_their_argv(env, capsys, name, setup, argv):
    setup(env)
    rc, out = env.run(capsys)
    assert rc == 0 and env.runner.calls == [argv]
    assert env.execs == [(PY, [PY, "-P", "-m", "levain", "upgrade", "--no-pip"])]


def test_installer_pip_user_adds_user(tmp_path, capsys):
    e = Env(tmp_path)
    e.prefix = e.base_prefix
    e.user_site = str(e.parent)          # the running package sits under the user site
    rc, _ = e.run(capsys)
    assert rc == 0 and e.runner.calls == [[PY, "-m", "pip", "install", "-U", SPEC, "--user"]]


@pytest.mark.parametrize("name, setup, text", [
    ("editable", lambda e: setattr(e, "direct", {"url": "file:///src", "dir_info": {"editable": True}}),
     "source tree"),
    ("file-url", lambda e: setattr(e, "direct", {"url": "file:///src", "dir_info": {}}), "source tree"),
    ("externally-managed", lambda e: (setattr(e, "prefix", e.base_prefix), e.markers.add("EXTERNALLY-MANAGED")),
     "externally managed"),
    ("pip-no-venv-elsewhere", lambda e: setattr(e, "prefix", e.base_prefix), "outside your user site"),
    ("conda", lambda e: setattr(e, "installer", "conda"), "conda"),
    ("poetry", lambda e: setattr(e, "installer", "poetry"), "poetry"),
    ("no-installer", lambda e: setattr(e, "installer", None), "no INSTALLER"),
    ("dist-not-owner", lambda e: setattr(e, "owns", False), "does not own"),
    ("pipx-absent", lambda e: (e.markers.add("pipx_metadata.json"), e.which_map.pop("pipx")), "pipx is not on PATH"),
    ("uv-absent", lambda e: (setattr(e, "installer", "uv"), e.which_map.pop("uv")), "uv is not on PATH"),
    ("two-markers", lambda e: (e.markers.add("pipx_metadata.json"), e.markers.add("uv-receipt.toml")),
     "more than one"),
])
def test_installer_refusals_run_nothing(env, capsys, name, setup, text):
    setup(env)
    rc, out = env.run(capsys)
    assert rc == 1 and text in out and "levain upgrade --no-pip" in out
    nothing_ran(env)


# 8
def test_package_not_named_levain(env, capsys):
    env.pkg = str(env.parent / "notlevain")
    rc, out = env.run(capsys, no_pip=True)
    assert rc == 1 and env.pkg in out
    nothing_ran(env)


def test_path_with_a_quote_is_not_takeable(tmp_path, capsys):
    e = Env(tmp_path / 'we"ird')
    rc, out = e.run(capsys, no_pip=True)
    assert rc == 1 and "the helper cannot take this path" in out
    nothing_ran(e)


def test_h11_missing_link_or_below_floor(env, capsys):
    h11 = env.parent / "h11"
    (h11 / "_version.py").write_text('__version__ = "0.15.0"\n')
    rc, out = env.run(capsys, no_pip=True)
    assert rc == 1 and str(h11) in out and "older than" in out and "run `levain upgrade` again" in out
    nothing_ran(env)
    (h11 / "_version.py").unlink()
    h11.rmdir()
    rc, out = env.run(capsys, no_pip=True)
    assert rc == 1 and "missing" in out and str(h11) in out
    nothing_ran(env)
    real = env.tmp / "elsewhere"
    real.mkdir()
    (real / "_version.py").write_text('__version__ = "0.16.0"\n')
    h11.symlink_to(real)
    rc, out = env.run(capsys, no_pip=True)
    assert rc == 1 and "a link" in out
    nothing_ran(env)


# 9
def test_group_writable_source_shows_the_chmod_remedy_and_no_sudo(env, capsys):
    from levain.slot import root_tree

    def lstat(p):
        gw = p.startswith(str(env.parent / "levain")) and p.endswith("levain")
        kind = stat.S_IFDIR if os.path.isdir(p) else stat.S_IFREG
        return SimpleNamespace(st_uid=OP_UID, st_gid=3999, st_mode=kind | (0o775 if gw else 0o755))

    def writers(parent, uid, d):
        root_tree.check_source_writers(parent, operator_uid=uid, operator_gids={1501}, lstat=lstat)

    env.writers = writers
    rc, out = env.run(capsys, no_pip=True)
    assert rc == 1 and "chmod -R g-w" in out and "levain upgrade` again" in out
    assert sudo_calls(env) == []


def test_a_passing_precheck_still_gives_the_exact_repair_argv(env, capsys):
    rc, out = env.run(capsys, no_pip=True)
    assert rc == 0 and sudo_calls(env) == [[U.SUDO, str(HELPER), "repair", "--source", env.pkg]]


def test_a_failing_directory_read_skips_the_precheck_and_sudo_still_runs(env, capsys):
    def boom(host):
        raise Refused("host-read-failed", "no")

    def never(*a):
        raise AssertionError("pre-check must be skipped")

    env.read_dir, env.writers = boom, never
    rc, out = env.run(capsys, no_pip=True)
    assert rc == 0 and len(sudo_calls(env)) == 1


# 10
def test_held_sessions_stop_before_sudo_with_commands_naming_pkg(env, capsys):
    env.held = SimpleNamespace(name="agent held sessions", ok=False, detail="1 stuck mid-end: s1 at stop")
    rc, out = env.run(capsys, no_pip=True)
    assert rc == 3 and "s1 at stop" in out
    for i in range(1, 6):
        assert f"{i}. cmd{i} {env.pkg}" in out
    assert "If 3 fails, still run 4 and 5" in out and "cmd6" not in out
    nothing_ran(env)


def test_held_without_printed_commands_still_stops(env, capsys):
    env.held = SimpleNamespace(name="agent held sessions", ok=False, detail="stuck")
    env.held_lines = lambda h, p: None
    rc, out = env.run(capsys, no_pip=True)
    assert rc == 3 and "no printed clean-up commands" in out
    nothing_ran(env)


def test_a_warn_held_row_does_not_stop(env, capsys):
    env.held = SimpleNamespace(name="agent held sessions", ok=True, detail="unknown", warn=True)
    rc, out = env.run(capsys, no_pip=True)
    assert rc == 0 and len(sudo_calls(env)) == 1


# 11: superseded by L3 r1 (test_already_current_still_runs_the_repair): there is no "already current" skip.


# 12
def test_dry_run_prints_argvs_and_runs_nothing(env, capsys):
    rc, out = env.run(capsys, dry_run=True)
    assert rc == 0 and "dry run: nothing was changed" in out
    assert str([PY, "-m", "pip", "install", "-U", SPEC]) in out
    assert str([U.SUDO, str(HELPER), "repair", "--source", env.pkg]) in out
    assert env.pkg in out and "h11" in out and GEN in out
    nothing_ran(env)


def test_dry_run_reports_a_refusal_code(env, capsys):
    env.held = SimpleNamespace(name="agent held sessions", ok=False, detail="stuck")
    rc, out = env.run(capsys, dry_run=True, no_pip=True)
    assert rc == 3
    nothing_ran(env)


# 13
def test_the_sudo_argv_is_exact(tmp_path, capsys):
    e = Env(tmp_path)
    link = tmp_path / "via"
    link.symlink_to(e.parent)
    e.pkg = str(link / "levain")                      # a path that realpath would change
    rc, _ = e.run(capsys, no_pip=True)
    argv = sudo_calls(e)[0]
    assert argv == ["/usr/bin/sudo", str(HELPER), "repair", "--source", e.pkg]
    assert argv[-1] != os.path.realpath(e.pkg)
    assert not {"-n", "-A", "-S", "-E"} & set(argv)


def test_the_module_never_uses_a_shell():
    src = Path(U.__file__).read_text()
    assert "shell=True" not in src and "os.system" not in src


# 14
def _ran(env, capsys, sudo, **kw):
    env.runner = Runner(sudo=sudo)
    rc, out = env.run(capsys, no_pip=True, **kw)
    return rc, out


def test_refusal_names_shown_once_no_retry(env, capsys):
    err = "levain install-helper: refused live-lease: 'session s1 holds a lease'\n"
    rc, out = _ran(env, capsys, (1, '{"ok":false,"refused":"live-lease"}\n', err))
    assert rc == 1 and "refused live-lease" in out and "session s1 holds a lease" in out
    assert "Fix that, then run `levain upgrade` again." in out and len(sudo_calls(env)) == 1


def test_bad_source_shows_the_chmod_text(env, capsys):
    err = "levain install-helper: refused bad-source: 'x is writable by group g. Run `chmod -R g-w /p`'\n"
    rc, out = _ran(env, capsys, (1, '{"ok":false,"refused":"bad-source"}\n', err))
    assert rc == 1 and "refused bad-source" in out and "chmod -R g-w /p" in out


def test_step_failed(env, capsys):
    err = "\n".join(f"line{i}" for i in range(30)) + "\n"
    rc, out = _ran(env, capsys, (1, '{"ok":false,"refused":"step-failed"}\n', err))
    assert rc == 1 and "refused step-failed" in out and "line29" in out and "line9\n" not in out


def test_ok_prints_done_and_pending_lines(env, capsys):
    err = "progress\nlevain install-helper: pending: runtime-helper verbs\n"
    rc, out = _ran(env, capsys, (0, '{"ok":true}\n', err))
    assert rc == 0 and "Done: the root side runs 9.9.9" in out and GEN in out
    assert out.index("Done:") < out.index("pending: runtime-helper verbs") and "levain update" in out
    assert "progress" not in out


# 15
def test_exec_after_pip_and_flags_pass_through(env, capsys):
    rc, _ = env.run(capsys, verbose=True)
    assert rc == 0 and env.execs == [(PY, [PY, "-P", "-m", "levain", "upgrade", "--no-pip", "--verbose"])]
    assert sudo_calls(env) == []


def test_missing_executable_after_pip_refuses(env, capsys):
    env.exe_exists = False
    rc, out = env.run(capsys)
    assert rc == 1 and "levain upgrade --no-pip" in out and env.execs == []


def test_pip_failure_does_not_exec(env, capsys):
    env.runner = Runner(pip=(1, "", ""))
    rc, out = env.run(capsys)
    assert rc == 1 and env.execs == [] and sudo_calls(env) == []


# 16
def test_upgrade_is_hidden_from_help_but_has_its_own(capsys):
    with pytest.raises(SystemExit):
        cli.main(["--help"])
    out = capsys.readouterr().out
    assert out.startswith("usage: levain") and "init" in out
    assert not re.search(r"^\s*upgrade\b", out, re.M) and "SUPPRESS" not in out
    assert "upgrade" not in out.split("\n\n")[0]
    with pytest.raises(SystemExit) as e:
        cli.main(["upgrade", "--help"])
    assert e.value.code == 0
    h = capsys.readouterr().out
    assert "--no-pip" in h and "--dry-run" in h and "levain update" in h


def test_the_default_held_lines_come_from_doctor_and_are_absent_without_it(monkeypatch):
    from levain import doctor

    monkeypatch.delattr(doctor, "reap_held_lines", raising=False)
    assert U._held_lines("darwin", "/p") is None
    monkeypatch.setattr(doctor, "reap_held_lines", lambda h, p: (f"run {p}",), raising=False)
    assert U._held_lines("darwin", "/p") == ("run /p",)


# --- L3 r1 -------------------------------------------------------------------------------------------------------

SECRETS = {"GH_TOKEN": "ghp_x", "AWS_SECRET_ACCESS_KEY": "aws_x", "AWS_SESSION_TOKEN": "aws_t",
           "ANTHROPIC_API_KEY": "sk_x", "AWS_ACCESS_KEY_ID": "AKIA"}
KEPT = {"PATH": "/bin:/usr/bin", "HOME": "/home/op", "USER": "op", "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8",
        "TERM": "xterm", "TMPDIR": "/tmp/op"}
INSTALLER_CFG = {"PIP_INDEX_URL": "https://mirror/simple", "UV_INDEX_URL": "https://mirror/simple",
                 "PIPX_HOME": "/home/op/.pipx", "HTTPS_PROXY": "http://proxy:3128",
                 "SSL_CERT_FILE": "/etc/ca.pem", "REQUESTS_CA_BUNDLE": "/etc/ca.pem"}


@pytest.fixture
def planted(monkeypatch):
    for k, v in {**SECRETS, **KEPT, **INSTALLER_CFG}.items():
        monkeypatch.setenv(k, v)


def test_no_secret_reaches_the_installer_the_reexec_or_sudo(env, capsys, planted):
    rc, _ = env.run(capsys)                                   # installer, then the re-exec
    assert rc == 0 and len(env.runner.kw) == 1 and len(env.exec_envs) == 1
    pip_env, exec_env = env.runner.kw[0]["env"], env.exec_envs[0]
    env.runner = Runner()
    rc, _ = env.run(capsys, no_pip=True)                      # stage 2: sudo
    assert rc == 0
    sudo_env = env.runner.kw[0]["env"]
    for e in (pip_env, exec_env, sudo_env):
        assert type(e) is dict and not set(SECRETS) & set(e), e
        assert {k: e.get(k) for k in ("PATH", "HOME", "USER", "LANG", "LC_ALL")} == \
            {k: KEPT[k] for k in ("PATH", "HOME", "USER", "LANG", "LC_ALL")}
    assert {k: pip_env.get(k) for k in INSTALLER_CFG} == INSTALLER_CFG    # the installer keeps its own config
    assert not set(INSTALLER_CFG) & set(sudo_env)                          # sudo needs none of it


def test_the_real_runner_and_exec_pass_the_env_they_are_given(monkeypatch):
    import subprocess

    seen = {}
    monkeypatch.setattr(subprocess, "run", lambda argv, **kw: (seen.update(kw), SimpleNamespace(
        returncode=0, stdout="", stderr=""))[1])
    U._run(["x"], env={"PATH": "/bin"}, cwd="/")
    assert seen["env"] == {"PATH": "/bin"} and seen["cwd"] == "/" and seen["shell"] is False
    with pytest.raises(TypeError):
        U._run(["x"])                                          # no default: a caller must choose the env
    got = []
    monkeypatch.setattr(os, "execve", lambda *a: got.append(a))
    assert U.Seams().execv is not os.execv
    U.Seams().execv("/py", ["/py"], {"PATH": "/bin"})
    assert got == [("/py", ["/py"], {"PATH": "/bin"})]


def test_children_run_from_a_neutral_directory(env, capsys):
    env.run(capsys)
    env.runner = Runner()
    env.run(capsys, no_pip=True)
    assert env.runner.kw[0]["cwd"] == "/"


def test_the_reexec_cannot_be_shadowed_from_cwd_and_keeps_the_user_site(env, capsys, tmp_path):
    import subprocess
    import sys

    env.run(capsys)
    argv = env.execs[0][1]
    assert argv[1:4] == ["-P", "-m", "levain"]
    shadow = tmp_path / "cwd"
    (shadow / "levain").mkdir(parents=True)
    (shadow / "levain" / "__init__.py").write_text("SHADOW = True\n")
    flags = argv[1:argv.index("-m")]
    probe = [sys.executable, *flags, "-c",
             "import sys, levain; print(hasattr(levain, 'SHADOW'), sys.flags.no_user_site, sys.flags.isolated)"]
    out = subprocess.run(probe, cwd=shadow, capture_output=True, text=True,
                         env={"PATH": "/usr/bin:/bin", "PYTHONPATH": str(Path(U.__file__).parents[1])}).stdout
    assert out.split() == ["False", "0", "0"]     # not the cwd copy; the user site (pip --user) still on sys.path


def test_already_current_still_runs_the_repair(env, capsys):
    env.current = GEN                                          # root's current already names the package's generation
    rc, out = env.run(capsys, no_pip=True)
    assert rc == 0 and len(sudo_calls(env)) == 1 and "Already up to date" not in out


@pytest.mark.parametrize("rc_", [1, 0])
def test_empty_stdout_is_an_unknown_outcome_never_nothing_changed(env, capsys, rc_):
    rc, out = _ran(env, capsys, (rc_, "", "sudo: 3 incorrect password attempts\n"))
    assert rc == 1
    assert "Nothing on the root side changed" not in out and "did not run" not in out
    assert "unknown" in out and "resumable" in out and "levain upgrade --no-pip" in out
    assert f"exit {rc_}" in out or f"exited {rc_}" in out
    assert "sudo: 3 incorrect password attempts" in out
    assert "password not accepted" not in out


def test_an_unreadable_last_line_is_unknown_and_resumable(env, capsys):
    rc, out = _ran(env, capsys, (1, "Traceback\n", "boom\n"))
    assert rc == 1 and "unknown" in out and "levain upgrade --no-pip" in out and "boom" in out


def test_held_fallback_says_what_to_do(env, capsys):
    env.held = SimpleNamespace(name="agent held sessions", ok=False, detail="stuck")
    env.held_lines = lambda h, p: None
    rc, out = env.run(capsys, no_pip=True)
    assert rc == 3 and "Run `levain doctor`" in out and "'agent held sessions' row" in out


def test_has_tty_without_stdin_or_tty_is_false_and_closes(monkeypatch):
    import builtins
    import sys

    monkeypatch.setattr(sys, "stdin", None)
    assert U._has_tty() is False
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(isatty=lambda: True))
    closed = []

    class F:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            closed.append(True)

    monkeypatch.setattr(builtins, "open", lambda *a, **k: F())
    assert U._has_tty() is True and closed == [True]

    def boom(*a, **k):
        raise OSError("no tty")

    monkeypatch.setattr(builtins, "open", boom)
    assert U._has_tty() is False


# --- L3 r2 -------------------------------------------------------------------------------------------------------

INTERPRETER_SWITCHES = {"PYTHONPATH": ".", "PYTHONHOME": "/x", "PYTHONSTARTUP": "./s.py", "PYTHONINSPECT": "1",
                        "PYTHONSAFEPATH": "", "PYTHONWARNINGS": "error"}


def _both_envs(env, capsys):
    rc, _ = env.run(capsys)                                   # installer, then the re-exec
    assert rc == 0 and len(env.runner.kw) == 1 and len(env.exec_envs) == 1
    return env.runner.kw[0]["env"], env.exec_envs[0]


@pytest.mark.parametrize("pythonpath", [".", ":/x", "/x:", "levain-shadow"])
def test_no_interpreter_switch_reaches_the_reexec_or_the_installer(env, capsys, monkeypatch, pythonpath):
    for k, v in {**INTERPRETER_SWITCHES, "PYTHONPATH": pythonpath}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("PYTHONUSERBASE", "/home/op/.ub")
    monkeypatch.delenv("PYTHONNOUSERSITE", raising=False)
    pip_env, exec_env = _both_envs(env, capsys)
    for e in (exec_env, pip_env):
        assert not set(INTERPRETER_SWITCHES) & set(e), e
        assert e["PYTHONUSERBASE"] == "/home/op/.ub"           # a --user install and its re-exec agree on the base
        assert "PYTHONNOUSERSITE" not in e                     # not invented when unset
    assert [k for k in exec_env if k.startswith("PYTHON")] == ["PYTHONUSERBASE"]


def test_pythonnousersite_reaches_the_reexec_only_when_set(env, capsys, monkeypatch):
    monkeypatch.setenv("PYTHONNOUSERSITE", "1")
    _, exec_env = _both_envs(env, capsys)
    assert exec_env["PYTHONNOUSERSITE"] == "1"


def test_ok_with_a_nonzero_exit_is_unknown_not_done(env, capsys):
    rc, out = _ran(env, capsys, (1, '{"ok":true}\n', "sudo: killed after the reply\n"))
    assert rc == 1 and "Done" not in out and "levain update" not in out
    assert "unknown" in out and "exited 1" in out and "resumable" in out and "levain upgrade --no-pip" in out
    assert "sudo: killed after the reply" in out


def test_stdout_is_flushed_before_the_exec(env, capsys, monkeypatch):
    import io
    import sys

    order = []

    class Out(io.StringIO):
        def flush(self):
            order.append(("flush", self.getvalue()))

    monkeypatch.setattr(sys, "stdout", Out())
    s = env.seams()
    s.execv = lambda p, a, e: order.append(("exec", None))
    U.run_upgrade(seams=s)
    kinds = [k for k, _ in order]
    assert "exec" in kinds and "flush" in kinds[:kinds.index("exec")]
    assert "Upgrading levain with pip" in [v for k, v in order if k == "flush"][-1]


def test_relative_config_paths_resolve_from_the_operators_directory(env, capsys, monkeypatch, tmp_path):
    cwd = tmp_path / "work"
    cwd.mkdir()
    (cwd / "pip.conf").write_text("")
    (cwd / "netrc").write_text("")
    (cwd / "ub").mkdir()
    monkeypatch.chdir(cwd)
    monkeypatch.setenv("PIP_CONFIG_FILE", "./pip.conf")
    monkeypatch.setenv("NETRC", "netrc")
    monkeypatch.setenv("PYTHONUSERBASE", "ub")
    monkeypatch.setenv("UV_CONFIG_FILE", "missing.toml")       # relative and absent: still anchored, the child runs from /
    monkeypatch.setenv("SSL_CERT_FILE", "/etc/ca.pem")         # absolute: left as given
    monkeypatch.setenv("PIP_INDEX_URL", "pip.conf")            # not a path-valued name: never rewritten
    pip_env, exec_env = _both_envs(env, capsys)
    assert pip_env["PIP_CONFIG_FILE"] == str(cwd / "pip.conf")
    assert pip_env["NETRC"] == str(cwd / "netrc")
    assert pip_env["PYTHONUSERBASE"] == exec_env["PYTHONUSERBASE"] == str(cwd / "ub")
    assert pip_env["UV_CONFIG_FILE"] == str(cwd / "missing.toml")
    assert pip_env["SSL_CERT_FILE"] == "/etc/ca.pem" and pip_env["PIP_INDEX_URL"] == "pip.conf"


def test_the_session_bus_reaches_the_installer_and_never_sudo(env, capsys, monkeypatch):
    monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", "unix:path=/run/user/501/bus")
    monkeypatch.setenv("XDG_RUNTIME_DIR", "/run/user/501")
    pip_env, _ = _both_envs(env, capsys)
    assert pip_env["DBUS_SESSION_BUS_ADDRESS"] == "unix:path=/run/user/501/bus"
    assert pip_env["XDG_RUNTIME_DIR"] == "/run/user/501"
    env.runner = Runner()
    assert env.run(capsys, no_pip=True)[0] == 0
    sudo = env.runner.kw[0]["env"]
    assert "DBUS_SESSION_BUS_ADDRESS" not in sudo and "XDG_RUNTIME_DIR" not in sudo


def test_a_closed_stdout_pipe_does_not_stop_the_exec(env, monkeypatch):
    import io
    import sys

    execed = []

    class Out(io.StringIO):
        def flush(self):
            raise BrokenPipeError

    monkeypatch.setattr(sys, "stdout", Out())
    s = env.seams()
    s.execv = lambda p, a, e: execed.append(a)
    U.run_upgrade(seams=s)
    assert execed


def test_missing_dist_info_refuses_not_internal_error(env, capsys):
    import importlib.metadata as md

    def no_dist():
        raise md.PackageNotFoundError("levain")

    seams = env.seams()
    seams.dist = no_dist
    rc = U.run_upgrade(seams=seams)
    out = capsys.readouterr().out
    assert rc == 1 and "Could not tell how Levain was installed" in out and "internal error" not in out

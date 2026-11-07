"""The VM harness's host profile: DARWIN reproduces the values the harness ran with before the profile existed, LINUX is
the Ubuntu 24.04 aarch64 clone's, the ``tart exec`` transport builds argv lists (no shell strings), a profile/guest OS
mismatch is refused, and no host-bound literal lives outside ``host_profile.py``."""
from __future__ import annotations

import ast
import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

from levain.slot import layout
from scripts.vm_harness import host_profile as HP
from scripts.vm_harness import vm_run
from scripts.vm_harness.driver import AdapterError, GuestError, VmAdapters
from scripts.vm_harness.host_profile import DARWIN, LINUX, ProfileMismatch

HARNESS = Path(__file__).resolve().parent.parent / "scripts" / "vm_harness"


# ---- DARWIN: the values the harness had before the profile (pinned as literals on purpose) ------------------------

def test_darwin_profile_values_are_the_pre_profile_literals():
    # revert: change any DARWIN field in host_profile.py
    assert (DARWIN.name, DARWIN.host_os, DARWIN.uname) == ("darwin", "darwin", "Darwin")
    assert DARWIN.store == "/private/var/levain"
    assert DARWIN.run_dir == "/private/var/run/levain"
    assert DARWIN.ops_control_sock == "/var/run/levain/control/control.sock"
    assert DARWIN.exchange_dir == "/private/tmp/fh"
    assert DARWIN.tools_dir == "/tmp/fire"
    assert DARWIN.python == "/usr/local/bin/python3.13"
    assert DARWIN.git == "/Library/Developer/CommandLineTools/usr/bin/git"
    assert DARWIN.ps_argv() == ["/bin/ps", "-Ao", "pid,etime,pcpu,ucomm,uid"]
    assert (DARWIN.dispatch_user, DARWIN.ssh_user) == ("_levain_dispatch", "admin")
    assert (DARWIN.transport, DARWIN.ssh_fallback, DARWIN.service_manager) == ("ssh", False, "launchd")


def test_darwin_argv_prefixes_are_the_old_class_constants():
    assert VmAdapters.OPS == ("sudo", "-n", "/usr/local/bin/python3.13", "-I", "/tmp/fire/guest_ops.py")
    assert VmAdapters.DISPATCH == ("sudo", "-n", "-u", "_levain_dispatch", "/usr/bin/env", "PATH=/usr/bin:/bin",
                                   "/usr/local/bin/python3.13", "-I", "/tmp/fire/guest_dispatch.py")


def test_the_default_adapter_profile_is_darwin():
    ad = VmAdapters(lambda argv, t=60: "", lambda argv: None)
    assert ad.profile is DARWIN and ad.OPS == VmAdapters.OPS and ad.DISPATCH == VmAdapters.DISPATCH


def test_darwin_guest_script_constants_are_the_old_literals(monkeypatch):
    # loaded as isolated modules with sys.platform pinned: on a Linux runner a plain import would select LINUX
    guest_ops = _load_as_package_module("guest_ops", "darwin", monkeypatch)
    guest_dispatch = _load_as_package_module("guest_dispatch", "darwin", monkeypatch)
    assert guest_ops.SLOTS == "/private/var/levain/slots" and guest_ops.FH == "/private/tmp/fh"
    assert guest_ops.PROFILE is DARWIN
    assert str(guest_dispatch.FH) == "/private/tmp/fh" and str(guest_dispatch.STORE) == "/private/var/levain"
    assert str(guest_dispatch.CONTROL) == "/private/var/run/levain/control/control.sock"


# ---- LINUX ----------------------------------------------------------------------------------------------------

def test_linux_profile_values():
    assert (LINUX.name, LINUX.host_os, LINUX.uname) == ("linux", "linux", "Linux")
    assert LINUX.python == "/usr/bin/python3.12" and LINUX.git == "/usr/bin/git"
    assert LINUX.exchange_dir == "/tmp/fh" and LINUX.tools_dir == "/tmp/fire"
    assert (LINUX.transport, LINUX.ssh_fallback) == ("tart_exec", True)
    assert (LINUX.service_manager, LINUX.account_tool) == ("systemd", "useradd --system")
    assert LINUX.ops_argv() == ("sudo", "-n", "/usr/bin/python3.12", "-I", "/tmp/fire/guest_ops.py")
    assert LINUX.dispatch_argv()[:5] == ("sudo", "-n", "-u", "_levain_dispatch", "/usr/bin/env")
    assert LINUX.ps_argv() == ["/bin/ps", "-Ao", "pid,etime,pcpu,comm,uid"]


@pytest.mark.parametrize("profile", [DARWIN, LINUX], ids=lambda p: p.name)
def test_store_and_run_paths_are_the_products_own(profile):
    assert Path(profile.store) == layout.STORE[profile.host_os]
    assert Path(profile.run_dir) == layout.RUN[profile.host_os]
    assert profile.ops_control_sock.endswith("/levain/control/control.sock")
    assert profile.ops_control_sock.startswith("/run/") or profile.ops_control_sock.startswith("/var/run/")


def test_adapters_with_the_linux_profile_carry_its_prefixes_and_ps():
    seen = []
    ad = VmAdapters(lambda argv, t=60: seen.append(list(argv)) or "", lambda argv: None, profile=LINUX)
    ad.lister()
    ad.ops("resolve", "root")
    assert seen[0] == ["/bin/ps", "-Ao", "pid,etime,pcpu,comm,uid"]
    assert seen[1][:5] == ["sudo", "-n", "/usr/bin/python3.12", "-I", "/tmp/fire/guest_ops.py"]
    assert VmAdapters.OPS[2] == "/usr/local/bin/python3.13"      # the class default is untouched by an instance


def test_profile_lookup():
    assert HP.profile_named("linux") is LINUX and HP.profile_named("darwin") is DARWIN
    with pytest.raises(ValueError):
        HP.profile_named("freebsd")
    assert HP.profile_for_platform("darwin") is DARWIN
    assert HP.profile_for_platform("linux") is LINUX
    with pytest.raises(ProfileMismatch):
        HP.profile_for_platform("win32")


def _load_as_package_module(name: str, platform: str, monkeypatch):
    monkeypatch.setattr(sys, "platform", platform)
    spec = importlib.util.spec_from_file_location(f"scripts.vm_harness.{name}_{platform}", HARNESS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_guest_scripts_pick_the_linux_profile_on_a_linux_platform(monkeypatch):
    ops = _load_as_package_module("guest_ops", "linux", monkeypatch)
    assert ops.PROFILE is LINUX and ops.SLOTS == "/var/levain/slots" and ops.FH == "/tmp/fh"
    disp = _load_as_package_module("guest_dispatch", "linux", monkeypatch)
    assert str(disp.STORE) == "/var/levain" and str(disp.FH) == "/tmp/fh"
    assert str(disp.CONTROL) == "/run/levain/control/control.sock"


def test_a_guest_script_run_as_a_script_imports_the_profile_from_its_own_dir(tmp_path):
    # the clone runs ``python -I <tools dir>/guest_ops.py``: -I leaves that dir off sys.path, the script puts it back
    for n in ("guest_ops.py", "host_profile.py"):
        (tmp_path / n).write_bytes((HARNESS / n).read_bytes())
    r = subprocess.run([sys.executable, "-I", str(tmp_path / "guest_ops.py"), "resolve", "root"],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0 and r.stdout.strip() == "0", r.stderr


# ---- refusal of a profile/host mismatch ---------------------------------------------------------------------

def test_check_guest_os_accepts_its_own_and_refuses_the_other():
    HP.check_guest_os(LINUX, "Linux\n")
    HP.check_guest_os(DARWIN, "Darwin\n")
    with pytest.raises(ProfileMismatch, match="expects a Linux guest"):
        HP.check_guest_os(LINUX, "Darwin\n")
    with pytest.raises(ProfileMismatch):
        HP.check_guest_os(DARWIN, "Linux")
    with pytest.raises(ProfileMismatch):
        HP.check_guest_os(DARWIN, "")


def test_verify_host_asks_uname_and_refuses_a_mismatch():
    calls = []

    def guest_for(answer):
        def g(argv, timeout=60):
            calls.append(list(argv))
            return answer
        return g
    VmAdapters(guest_for("Linux\n"), lambda a: None, profile=LINUX).verify_host()
    assert calls[-1] == ["/usr/bin/uname", "-s"]
    with pytest.raises(ProfileMismatch):
        VmAdapters(guest_for("Darwin\n"), lambda a: None, profile=LINUX).verify_host()
    with pytest.raises(ProfileMismatch):
        VmAdapters(guest_for("Linux\n"), lambda a: None).verify_host()      # the default darwin profile, a linux guest


def test_verify_host_turns_an_unanswering_guest_into_a_refusal():
    def dead(argv, timeout=60):
        raise GuestError(255, "no agent")
    with pytest.raises(AdapterError, match="cannot ask the guest its OS"):
        VmAdapters(dead, lambda a: None, profile=LINUX).verify_host()


def test_main_refuses_a_linux_profile_against_a_darwin_guest(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(vm_run, "make_transport", lambda vm, profile, d: ((lambda argv, timeout=60: "Darwin\n"), lambda a: None))
    assert vm_run.main(["vend-lin-x", str(tmp_path / "ev"), "--profile", "linux"]) == 2      # a clean refusal, no traceback
    assert "expects a Linux guest" in capsys.readouterr().err
    assert not (tmp_path / "ev").exists()


# ---- the tart exec transport ------------------------------------------------------------------------------------

class _Run:
    def __init__(self, rc=0, out="", err=""):
        self.returncode, self.stdout, self.stderr = rc, out, err


def _record_runs(monkeypatch, result=None):
    seen = []

    def fake_run(argv, **kw):
        seen.append((list(argv), kw))
        return result or _Run(out="ok\n")
    monkeypatch.setattr(subprocess, "run", fake_run)
    return seen


def test_tart_exec_guest_is_an_argv_list_with_no_shell_string(monkeypatch):
    seen = _record_runs(monkeypatch)
    guest, _ = vm_run.make_tart_exec("vend-lin-1")
    assert guest(["/bin/echo", "a; touch /pwned"]) == "ok\n"
    argv, kw = seen[-1]
    assert argv == [vm_run.TART, "exec", "vend-lin-1", "/bin/echo", "a; touch /pwned"]    # one token, never joined
    assert "shell" not in kw or kw["shell"] is False


def test_tart_exec_script_form_pipes_the_script_on_stdin(monkeypatch):
    seen = _record_runs(monkeypatch)
    guest, _ = vm_run.make_tart_exec("vend-lin-1")
    guest.script("echo hi\nid\n")
    argv, kw = seen[-1]
    assert argv == [vm_run.TART, "exec", "-i", "vend-lin-1", *vm_run.SCRIPT_ARGV]
    assert vm_run.SCRIPT_ARGV[:2] == ("sudo", "-n")      # no password prompt eating the script
    assert kw["input"] == "echo hi\nid\n"


@pytest.mark.skipif(sys.platform == "win32", reason="needs bash")
def test_the_script_form_keeps_trailing_lines_when_a_command_reads_stdin(tmp_path):
    # reproduced shape: ``bash -s`` lets ``cat`` swallow the lines after it; the staged form must not
    argv = [a for a in vm_run.SCRIPT_ARGV if a != "sudo" and a != "-n"]
    argv = ["bash", *argv[1:]] if argv[0] == "bash" else argv
    out = tmp_path / "out"
    text = f"cat >/dev/null\necho after >{out}\n"
    r = subprocess.run(argv, input=text, capture_output=True, text=True, timeout=30)
    assert r.returncode == 0 and out.read_text() == "after\n"
    naive = tmp_path / "naive"
    subprocess.run(["bash", "-s"], input=f"cat >/dev/null\necho after >{naive}\n", capture_output=True, text=True, timeout=30)
    assert not naive.exists()       # the old form really did lose the tail


def test_tart_exec_nonzero_exit_is_a_guest_error_with_the_rc(monkeypatch):
    _record_runs(monkeypatch, _Run(rc=3, err="absent"))
    guest, _ = vm_run.make_tart_exec("vend-lin-1")
    with pytest.raises(GuestError) as e:
        guest(["x"])
    assert e.value.rc == 3
    with pytest.raises(GuestError):
        guest.script("false")


def test_tart_exec_spawn_is_a_detached_argv_popen(monkeypatch):
    seen = []

    class P:
        def __init__(self, argv, **kw):
            seen.append((list(argv), kw))
    monkeypatch.setattr(subprocess, "Popen", P)
    _, spawn = vm_run.make_tart_exec("vend-lin-1")
    spawn(["sudo", "-n", "x"])
    assert seen[0][0] == [vm_run.TART, "exec", "vend-lin-1", "sudo", "-n", "x"]
    assert seen[0][1]["stdin"] == subprocess.DEVNULL


@pytest.mark.parametrize("name", ["vdis-mac", "ubuntu", "vend", "lin-l0-852be50c", "vend-x y"])
def test_tart_exec_only_addresses_vend_clones(name, monkeypatch):
    seen = _record_runs(monkeypatch)
    with pytest.raises(SystemExit):
        vm_run.make_tart_exec(name)
    with pytest.raises(SystemExit):
        vm_run.make_transport(name, LINUX, Path("."))
    assert not seen


@pytest.fixture(autouse=True)
def _fast_probe(monkeypatch):
    t = [0.0]
    monkeypatch.setattr(vm_run, "_now", lambda: t[0])
    monkeypatch.setattr(vm_run, "_nap", lambda s: t.__setitem__(0, t[0] + s))


def test_the_agent_probe_retries_a_freshly_booted_guest(monkeypatch):
    rcs = iter([1, 1, 0])
    monkeypatch.setattr(subprocess, "run", lambda argv, **kw: _Run(rc=next(rcs)))
    assert vm_run.agent_answers("vend-lin-1") is True


def test_the_agent_probe_gives_up_after_its_wait(monkeypatch):
    n = []
    monkeypatch.setattr(subprocess, "run", lambda argv, **kw: n.append(1) or _Run(rc=1))
    assert vm_run.agent_answers("vend-lin-1", wait=10) is False and 2 <= len(n) <= 6


def test_transport_choice_follows_the_profile(monkeypatch, tmp_path):
    seen = _record_runs(monkeypatch)       # tart exec /usr/bin/true answers rc 0
    guest, _ = vm_run.make_transport("vend-lin-1", LINUX, tmp_path)
    guest(["/bin/true"])
    assert seen[0][0] == [vm_run.TART, "exec", "vend-lin-1", "/usr/bin/true"]       # the probe
    assert seen[-1][0][:3] == [vm_run.TART, "exec", "vend-lin-1"]
    assert hasattr(guest, "script")
    # darwin never probes the agent: ssh only
    seen.clear()
    monkeypatch.setattr(subprocess, "run", lambda argv, **kw: seen.append(list(argv)) or _Run(out="10.0.0.5\n"))
    guest, _ = vm_run.make_transport("vend-x", DARWIN, tmp_path)
    guest(["/bin/echo", "x"])
    assert seen[-1][0] == vm_run.SSH and seen[-1][-2] == "admin@10.0.0.5"
    assert not any(a[:2] == [vm_run.TART, "exec"] for a in seen)


def test_linux_falls_back_to_ssh_when_the_agent_does_not_answer(monkeypatch, tmp_path):
    seen = []

    def fake_run(argv, **kw):
        seen.append(list(argv))
        if argv[:2] == [vm_run.TART, "exec"]:
            return _Run(rc=1, err="no agent")
        return _Run(out="10.0.0.9\n")
    monkeypatch.setattr(subprocess, "run", fake_run)
    guest, _ = vm_run.make_transport("vend-lin-1", LINUX, tmp_path)
    guest(["/bin/echo", "x"])
    assert seen[-1][0] == vm_run.SSH and seen[-1][-2] == "admin@10.0.0.9" and seen[-1][-1] == "/bin/echo x"
    assert not hasattr(guest, "script")


def test_a_linux_profile_without_the_fallback_stays_on_the_agent(monkeypatch, tmp_path):
    import dataclasses
    seen = []
    monkeypatch.setattr(subprocess, "run", lambda argv, **kw: seen.append(list(argv)) or _Run(rc=1))
    strict = dataclasses.replace(LINUX, ssh_fallback=False)
    guest, _ = vm_run.make_transport("vend-lin-1", strict, tmp_path)
    assert hasattr(guest, "script")


def test_ssh_transport_is_unchanged_for_darwin(monkeypatch, tmp_path):
    seen = _record_runs(monkeypatch, _Run(out="10.0.0.5\n"))
    guest, _ = vm_run.make_ssh("vend-x", tmp_path)
    guest(["/bin/echo", "a; touch /pwned"])
    assert seen[-1][0][-1] == "/bin/echo 'a; touch /pwned'" and seen[-1][0][-2] == "admin@10.0.0.5"
    assert (tmp_path / "askpass.sh").read_text() == "#!/bin/sh\necho admin\n"
    import dataclasses
    other = dataclasses.replace(LINUX, ssh_user="ubuntu", ssh_password="pw")
    vm_run.make_ssh("vend-x", tmp_path, other)
    assert (tmp_path / "askpass.sh").read_text() == "#!/bin/sh\necho pw\n"      # the password, not the login name


# ---- no host-bound literal outside the profile ---------------------------------------------------------------

GUARDED = ("guest_ops.py", "guest_dispatch.py", "vm_run.py", "driver.py")
#: a value a profile owns; every one of these must also appear in host_profile.py (the scan is not vacuous)
HOST_BOUND = ("/private", "/var/levain", "/var/run/levain", "/run/levain", "/tmp/fh", "/tmp/fire", "python3.1",
              "/usr/local/bin", "/Library/Developer", "/usr/bin/git", "darwin", "Darwin", "linux", "Linux",
              "launchd", "systemd", "dscl", "useradd", "_levain_dispatch", "ucomm", "/tmp/lv")


def _string_literals(source: str) -> list[tuple[int, str]]:
    """Every string constant of ``source`` that is code, not a docstring (comments are not tokens of the tree)."""
    tree = ast.parse(source)
    doc_ids = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.body:
            first = node.body[0]
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) and isinstance(first.value.value, str):
                doc_ids.add(id(first.value))
    return [(n.lineno, n.value) for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in doc_ids]


def _offenders(source: str) -> list[tuple[int, str, str]]:
    return [(ln, lit, bad) for ln, lit in _string_literals(source) for bad in HOST_BOUND if bad in lit]


def test_no_host_bound_literal_outside_the_profile_definition():
    # revert: hardcode "/private/tmp/fh" (or any value in HOST_BOUND) as a string in one of the four files
    found = {n: _offenders((HARNESS / n).read_text()) for n in GUARDED}
    assert found == {n: [] for n in GUARDED}


def test_the_scan_is_not_vacuous():
    profile_src = (HARNESS / "host_profile.py").read_text()
    literal_text = "\n".join(lit for _, lit in _string_literals(profile_src))
    for bad in HOST_BOUND:
        assert bad in literal_text, f"{bad!r} is not defined in host_profile.py: stale HOST_BOUND entry"
    assert _offenders('x = "/private/tmp/fh"\ny = f"{a}/usr/local/bin/python3.13"')       # the scanner sees it
    assert not _offenders('"""docstring naming /private/tmp/fh"""\n# comment /private\nx = 1')

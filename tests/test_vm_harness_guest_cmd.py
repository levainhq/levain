"""1111-32 v1: ``guest_cmd`` (run ONE argv in a vend- clone, evidence to a file, rc propagated) and ``guest_asuid`` (the
guest-side setgroups wrapper for the F10 wrong-uid-but-filesystem-permitted case). No VM, no ssh: ``subprocess.run`` is
the fake guest, the same seam ``test_vm_harness_profile.py`` uses. Graded on behaviour: what reached the transport, what
reached the evidence file, what the process returned."""
from __future__ import annotations

import json
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.vm_harness import guest_asuid, guest_cmd, vm_run


class _Run:
    def __init__(self, rc=0, out="", err=""):
        self.returncode, self.stdout, self.stderr = rc, out, err


class Guest:
    """subprocess.run replaced: ``tart ip`` answers an address, ``tart exec`` / ssh answer ``reply``."""

    def __init__(self, monkeypatch, reply=None, agent=True):
        self.calls: list[tuple[list[str], dict]] = []
        self.reply = reply or _Run(0, "out\n", "")
        self.agent = agent
        monkeypatch.setattr(subprocess, "run", self)

    def __call__(self, argv, **kw):
        argv = list(argv)
        self.calls.append((argv, kw))
        if argv[:2] == [vm_run.TART, "ip"]:
            return _Run(0, "10.0.0.5\n")
        if argv[:4] == [vm_run.TART, "exec", "vend-lin", "/usr/bin/true"]:       # the agent probe
            return _Run(0 if self.agent else 1)
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply

    def commands(self):
        """The calls that were not the ip lookup or the agent probe: what the guest was actually asked."""
        return [c for c in self.calls if c[0][:2] != [vm_run.TART, "ip"] and c[0][-1:] != ["/usr/bin/true"]]


def go(tmp_path, *args, name="ev.txt"):
    out = tmp_path / name
    i = args.index("--")
    rc = guest_cmd.main([*args[:i], "--out", str(out), *args[i:]])
    return rc, out


def test_argv_with_shell_syntax_reaches_the_guest_as_single_words_over_ssh(monkeypatch, tmp_path):
    g = Guest(monkeypatch)
    nasty = ["/bin/echo", "a; touch /pwned", "$(id)", "it's", "x y", "`id`", "*"]
    rc, _ = go(tmp_path, "vend-mac", "--profile", "darwin", "--", *nasty)
    (argv, kw), = g.commands()
    assert argv[0] == vm_run.SSH and kw.get("shell") in (None, False)
    assert shlex.split(argv[-1]) == nasty            # one joined, quoted command: the remote parse gives the words back
    assert argv[-2] == "admin@10.0.0.5" and rc == 0


def test_argv_reaches_the_tart_agent_as_a_list_never_a_string_on_linux(monkeypatch, tmp_path):
    g = Guest(monkeypatch)
    nasty = ["/bin/echo", "a; touch /pwned", "$(id)"]
    rc, _ = go(tmp_path, "vend-lin", "--profile", "linux", "--", *nasty)
    (argv, kw), = g.commands()
    assert argv == [vm_run.TART, "exec", "vend-lin", *nasty]
    assert kw.get("shell") in (None, False) and rc == 0


def test_a_name_that_is_not_a_vend_clone_is_refused_and_nothing_runs_or_is_written(monkeypatch, tmp_path):
    g = Guest(monkeypatch)
    for name in ("vdis-mac", "ubuntu", "vend", "lin-l0-1", "vend-x y", "vend-"):
        out = tmp_path / f"{name.replace(' ', '_')}.txt"
        with pytest.raises(SystemExit) as e:
            guest_cmd.main([name, "--profile", "darwin", "--out", str(out), "--", "/bin/true"])
        assert e.value.code not in (0, None)
        assert not out.exists()
    assert g.calls == []


def test_the_guest_rc_is_the_process_rc_and_the_failing_streams_are_still_the_evidence(monkeypatch, tmp_path):
    Guest(monkeypatch, _Run(3, "partial out\n", "boom\n"))
    rc, out = go(tmp_path, "vend-mac", "--profile", "darwin", "--", "/usr/bin/false")
    assert rc == 3
    text = out.read_text()
    assert "partial out\n" in text and "boom\n" in text and text.rstrip().endswith("rc=3")


def test_rc_zero_writes_evidence_naming_what_ran_where(monkeypatch, tmp_path):
    g = Guest(monkeypatch, _Run(0, "hello\n", ""))
    rc, out = go(tmp_path, "vend-mac", "--profile", "darwin", "--sudo", "--", "/sbin/pfctl", "-s", "info")
    assert rc == 0
    text = out.read_text()
    head = text.split("--- stdout")[0]
    assert "vm: vend-mac" in head and "profile: darwin" in head and "transport: ssh" in head
    assert json.dumps(["/sbin/pfctl", "-s", "info"]) in head           # the caller's argv, exact
    assert json.dumps(["sudo", "-n", "/sbin/pfctl", "-s", "info"]) in head     # and what was sent
    assert "hello\n" in text and text.rstrip().endswith("rc=0")
    (argv, _), = g.commands()
    assert shlex.split(argv[-1]) == ["sudo", "-n", "/sbin/pfctl", "-s", "info"]


def test_without_sudo_nothing_is_prefixed(monkeypatch, tmp_path):
    g = Guest(monkeypatch)
    go(tmp_path, "vend-mac", "--profile", "darwin", "--", "/usr/bin/id")
    (argv, _), = g.commands()
    assert shlex.split(argv[-1]) == ["/usr/bin/id"]


def test_the_guest_streams_are_echoed_to_the_callers_streams(monkeypatch, tmp_path, capsys):
    Guest(monkeypatch, _Run(0, "to-stdout\n", "to-stderr\n"))
    go(tmp_path, "vend-mac", "--profile", "darwin", "--", "/bin/x")
    cap = capsys.readouterr()
    assert "to-stdout" in cap.out and "to-stderr" in cap.err


def test_an_existing_evidence_file_is_never_overwritten(monkeypatch, tmp_path):
    g = Guest(monkeypatch)
    out = tmp_path / "ev.txt"
    out.write_text("earlier evidence")
    with pytest.raises(SystemExit):
        guest_cmd.main(["vend-mac", "--profile", "darwin", "--out", str(out), "--", "/bin/true"])
    assert out.read_text() == "earlier evidence" and g.calls == []


def test_no_command_or_no_separator_is_refused_before_anything_runs(monkeypatch, tmp_path):
    g = Guest(monkeypatch)
    for args in (["vend-mac", "--profile", "darwin"], ["vend-mac", "--profile", "darwin", "--"],
                 ["vend-mac", "--profile", "darwin", "/bin/true"]):
        with pytest.raises(SystemExit):
            guest_cmd.main([*args, "--out", str(tmp_path / "e.txt")])
    assert g.calls == [] and not (tmp_path / "e.txt").exists()


def test_a_missing_profile_or_out_is_refused(monkeypatch, tmp_path):
    Guest(monkeypatch)
    with pytest.raises(SystemExit):
        guest_cmd.main(["vend-mac", "--out", str(tmp_path / "e.txt"), "--", "/bin/true"])         # profile is required
    with pytest.raises(SystemExit):
        guest_cmd.main(["vend-mac", "--profile", "darwin", "--", "/bin/true"])                    # out is required


def test_a_timeout_is_recorded_and_returned_as_124(monkeypatch, tmp_path):
    Guest(monkeypatch, subprocess.TimeoutExpired(cmd="ssh", timeout=1))
    rc, out = go(tmp_path, "vend-mac", "--profile", "darwin", "--timeout", "1", "--", "/bin/sleep", "9")
    assert rc == 124
    assert "TIMEOUT" in out.read_text() and out.read_text().rstrip().endswith("rc=124")


def test_a_host_side_transport_failure_is_recorded_and_returned_as_125_not_a_guest_rc(monkeypatch, tmp_path):
    def boom(argv, **kw):
        raise subprocess.CalledProcessError(1, argv, stderr="no such vm")
    monkeypatch.setattr(subprocess, "run", boom)
    rc, out = go(tmp_path, "vend-mac", "--profile", "darwin", "--", "/bin/true")
    assert rc == 125
    text = out.read_text()
    assert "TRANSPORT-ERROR" in text and text.rstrip().endswith("rc=125")


def test_the_timeout_reaches_the_transport(monkeypatch, tmp_path):
    g = Guest(monkeypatch)
    go(tmp_path, "vend-mac", "--profile", "darwin", "--timeout", "17", "--", "/bin/true")
    (_, kw), = g.commands()
    assert kw["timeout"] == 17


def test_a_negative_or_huge_rc_stays_a_valid_exit_status(monkeypatch, tmp_path):
    Guest(monkeypatch, _Run(-9, "", "killed"))
    rc, _ = go(tmp_path, "vend-mac", "--profile", "darwin", "--", "/bin/x")
    assert rc == 128 + 9


def test_the_guest_module_has_no_shell_escape():
    src = Path(guest_cmd.__file__).read_text()
    assert "shell=True" not in src and "os.system" not in src and "bash" not in src


# --- guest_asuid -----------------------------------------------------------------------------------------------------

class FakeOS:
    """The slice of ``os`` the wrapper touches, recording the order, with a configurable privilege model."""

    def __init__(self, euid=0, setuid_zero_works=False):
        self.calls: list = []
        self.uid = self.euid_ = euid
        self.gid = 0
        self.groups: list[int] = []
        self.setuid_zero_works = setuid_zero_works
        self.raise_on: str | None = None

    def geteuid(self): return self.euid_
    def getuid(self): return self.uid
    def getgid(self): return self.gid
    def getegid(self): return self.gid
    def getgroups(self): return list(self.groups)

    def setgroups(self, g):
        self.calls.append(("setgroups", list(g)))
        if self.raise_on == "setgroups":
            raise PermissionError(1, "Operation not permitted")
        self.groups = list(g)

    def setgid(self, g):
        self.calls.append(("setgid", g))
        self.gid = g

    def setuid(self, u):
        self.calls.append(("setuid", u))
        if u == 0 and not self.setuid_zero_works and self.uid != 0:
            raise PermissionError(1, "Operation not permitted")
        self.uid = self.euid_ = u

    def execv(self, path, argv):
        self.calls.append(("execv", path, list(argv)))
        raise _Exec


class _Exec(BaseException):
    pass


def test_asuid_sets_groups_then_gid_then_uid_then_execs_the_command():
    f = FakeOS()
    with pytest.raises(_Exec):
        guest_asuid.drop_and_exec(631, 632, [633, 634], ["/usr/bin/id", "-a"], os_=f, err=_Sink())
    kinds = [c[0] for c in f.calls]
    assert kinds.index("setgroups") < kinds.index("setgid") < kinds.index("setuid") < kinds.index("execv")
    assert ("setgroups", [633, 634]) in f.calls and ("setgid", 632) in f.calls and ("setuid", 631) in f.calls
    assert f.calls[-1] == ("execv", "/usr/bin/id", ["/usr/bin/id", "-a"])
    # after the drop the privilege cannot come back (the wrapper proved it by trying)
    assert ("setuid", 0) in f.calls and f.uid == 631


class _Sink:
    def __init__(self): self.text = ""
    def write(self, s): self.text += s
    def flush(self): pass


def test_asuid_reports_the_credentials_it_ended_with_on_stderr():
    f, sink = FakeOS(), _Sink()
    with pytest.raises(_Exec):
        guest_asuid.drop_and_exec(631, 632, [633], ["/usr/bin/id"], os_=f, err=sink)
    assert "uid=631" in sink.text and "gid=632" in sink.text and "633" in sink.text


def test_asuid_never_execs_when_not_root():
    f = FakeOS(euid=501)
    with pytest.raises(SystemExit) as e:
        guest_asuid.drop_and_exec(631, 632, [], ["/usr/bin/id"], os_=f, err=_Sink())
    assert e.value.code == guest_asuid.EX_NOPERM and not f.calls


def test_asuid_never_execs_when_the_drop_can_be_undone():
    f = FakeOS(setuid_zero_works=True)
    with pytest.raises(SystemExit) as e:
        guest_asuid.drop_and_exec(631, 632, [], ["/usr/bin/id"], os_=f, err=_Sink())
    assert e.value.code == guest_asuid.EX_SOFTWARE and not any(c[0] == "execv" for c in f.calls)


def test_asuid_never_execs_when_a_step_of_the_drop_fails():
    f = FakeOS()
    f.raise_on = "setgroups"
    with pytest.raises(SystemExit) as e:
        guest_asuid.drop_and_exec(631, 632, [633], ["/usr/bin/id"], os_=f, err=_Sink())
    assert e.value.code == guest_asuid.EX_OSERR and not any(c[0] == "execv" for c in f.calls)


@pytest.mark.parametrize("argv", [
    ["0", "20", "-", "--", "/usr/bin/id"],                  # to root: pointless, refused
    ["x", "20", "-", "--", "/usr/bin/id"],
    ["631", "-1", "-", "--", "/usr/bin/id"],
    ["631", "20", "a,b", "--", "/usr/bin/id"],
    ["631", "20", "1,,2", "--", "/usr/bin/id"],
    ["631", "20", "-", "/usr/bin/id"],                      # no separator
    ["631", "20", "-", "--"],                               # no command
    ["631", "20", "-", "--", "id"],                         # relative command: no PATH search under root
    ["631", "20"],
])
def test_asuid_refuses_a_malformed_invocation_before_touching_credentials(argv):
    f = FakeOS()
    with pytest.raises(SystemExit) as e:
        guest_asuid.main(argv, os_=f, err=_Sink())
    assert e.value.code == guest_asuid.EX_USAGE and f.calls == []


def test_asuid_main_parses_the_group_list_and_dash_means_none():
    f = FakeOS()
    with pytest.raises(_Exec):
        guest_asuid.main(["631", "632", "633,634", "--", "/usr/bin/id", "-a"], os_=f, err=_Sink())
    assert ("setgroups", [633, 634]) in f.calls
    f = FakeOS()
    with pytest.raises(_Exec):
        guest_asuid.main(["631", "632", "-", "--", "/usr/bin/id"], os_=f, err=_Sink())
    assert ("setgroups", []) in f.calls


def test_asuid_run_for_real_as_a_non_root_user_refuses_and_runs_nothing(tmp_path):
    import os
    if os.geteuid() == 0:
        pytest.skip("must not run as root: it would really drop privileges")
    marker = tmp_path / "ran"
    r = subprocess.run([sys.executable, "-I", guest_asuid.__file__, "631", "632", "-", "--",
                        "/usr/bin/touch", str(marker)], capture_output=True, text=True, timeout=30)
    assert r.returncode == guest_asuid.EX_NOPERM and not marker.exists()
    assert "root" in r.stderr


def test_asuid_is_a_self_contained_guest_script():
    # the sitting copies this one file into the guest tools dir: it may not import the repo
    src = Path(guest_asuid.__file__).read_text()
    assert "from scripts" not in src and "import scripts" not in src
    assert "from levain" not in src and "import levain" not in src

"""S1 session 2d: the second wall (CONTRACT §8, §9; levain/slot/wall.py). Plan-level only: every argv is text,
every OS act a fake that records its order. Nothing here applies a profile, drops a uid or enters a namespace."""
from __future__ import annotations

import errno
import resource
import sys
from pathlib import PurePosixPath

import pytest

from levain.slot import Refused
from levain.slot import wall
from levain.slot.ids import SlotId
from levain.slot.pf import anchor_rules
from tests.execve_guard import refuse_real_execve, using

SESSION = "s-0123456789abcdef"
OTHER = "s-fedcba9876543210"
E0, E1, V0 = SlotId("egress", 0), SlotId("egress", 1), SlotId("none", 0)

#: The forbidden Seatbelt operation family, assembled at runtime so no file spells it (trg-069 guard).
SC = "sys" + "call"

IDS = {"darwin": {"_levain_s0": 3999, "_levain_gw0": 3998, "_levain_s1": 3997, "_levain_gw1": 3996,
                  "_levain_v0": 3995, "_levain_c0": 3994},
       "linux": {"_levain_s0": 899, "_levain_gw0": 898, "_levain_s1": 897, "_levain_gw1": 896,
                 "_levain_v0": 895, "_levain_c0": 894}}
SHARED = {"darwin": {"levain-slots": 3789, "levain-gwfronts": 3788},
          "linux": {"levain-slots": 789, "levain-gwfronts": 788}}
S = {"darwin": "/private/var/levain/live/" + SESSION, "linux": "/var/levain/live/" + SESSION}
VENDOR = {"darwin": "/usr/local/lib/levain-vendor/bin/claude", "linux": "/usr/lib/levain-vendor/bin/claude"}
LINKS = {"/bin": "usr/bin", "/sbin": "usr/sbin", "/lib": "usr/lib", "/lib64": None}   # b1c arm64: no /lib64


def spec(host, target, slot=E0, **kw):
    kw.setdefault("argv", [VENDOR[host], "--resume"] if target == "vendor" else
                  ["pytest", "-q"] if target == "step" else
                  ["/usr/bin/python3", "-IS", "/usr/lib/levain-root/drv.py" if host == "linux"
                   else "/usr/local/lib/levain-root/drv.py"] if target == "driver" else [])
    if target == "front":
        kw.setdefault("front_module", "levain.broker.front")
    if target == "clone":
        kw.setdefault("kind", "verifier" if slot is V0 else "interactive")
    return wall.launch_spec(host, target, slot=slot, session=kw.pop("session", SESSION), ids=IDS[host],
                            shared=SHARED[host], **kw)


# --- a fake kernel for the stage ----------------------------------------------------------------------------


class FakeOS:
    """Just enough credential semantics to make a drop real: a non-root caller cannot setgroups, cannot raise
    a hard rlimit, and seteuid(0) / setegid(0) fail once no id is 0. ``faults``: step -> "raise" (EPERM),
    "noop" (returns, changes nothing: a lying call), or a callable."""

    def __init__(self, faults=None, *, linux=False, files=None, fds=(0, 1, 2), spec_bytes=b"", path=()):
        self.faults = dict(faults or {})
        self.r = [0, 0, 0]       # ruid, euid, suid
        self.g = [0, 0, 0]
        self.groups = [0, 1, 2]
        self.nproc = (resource.RLIM_INFINITY, resource.RLIM_INFINITY)
        self.log: list = []
        self.files = dict(files or {})
        self.open_fds = set(fds)
        self.spec_bytes = spec_bytes
        self.path = set(path)
        self.inheritable: set = set()
        self.calls = wall.OsCalls(
            setgroups=self._w("setgroups", self._setgroups), setgid=self._w("setgid", self._setgid),
            setuid=self._w("setuid", self._setuid), getuid=lambda: self.r[0], geteuid=lambda: self.r[1],
            getgid=lambda: self.g[0], getegid=lambda: self.g[1],
            getgroups=self._w("getgroups", lambda: list(self.groups)),
            seteuid=self._w("seteuid", self._seteuid), setegid=self._w("setegid", self._setegid),
            getrlimit=self._w("getrlimit", lambda which: self.nproc),
            setrlimit=self._w("setrlimit", self._setrlimit),
            getresuid=self._w("getresuid", lambda: tuple(self.r)) if linux else None,
            getresgid=self._w("getresgid", lambda: tuple(self.g)) if linux else None,
            capabilities=self._w("capabilities", self._caps) if linux else None,
            read_fd=self._w("read_fd", lambda fd, cap: self.spec_bytes),
            read_file=self._w("read_file", self._read_file), chdir=self._w("chdir", lambda p: None),
            list_fds=lambda: sorted(self.open_fds), close=self._w("close", self.open_fds.discard),
            set_inheritable=self._inh, access_x=lambda p: p in self.path,
            dup2=self._w("dup2", lambda a, b: None),
            getpid=lambda: 4242, write_file=self._w("write_file", lambda p, t: None),
            read_text=self._w("read_text", lambda p: self.files.get(p, "")))
        # s5 §5b A6: the fake kernel's session keyring (an export stage on Linux joins a fresh one before the drop)
        self.session_keyring = 100
        self.calls.keyctl = self._w("keyctl", self._keyctl)
        # wall calls os.execve itself: the module's guard routes it here (tests/execve_guard.py)
        self.execve = self._w("execve", lambda p, a, e: None)

    def _keyctl(self, op, arg2, arg3=0):
        if op == 1 and arg2 == 0:                  # KEYCTL_JOIN_SESSION_KEYRING, anonymous
            self.session_keyring += 1
            return self.session_keyring
        if op == 0 and arg2 == -3:                 # KEYCTL_GET_KEYRING_ID of KEY_SPEC_SESSION_KEYRING
            return self.session_keyring
        raise OSError(errno.EINVAL, "keyctl")

    def _inh(self, fd, v):
        using(self)
        self.inheritable.add(fd)

    def _w(self, name, fn):
        def call(*a):
            using(self)
            self.log.append((name,) + a)
            f = self.faults.get(name)
            if f == "raise":
                raise PermissionError(errno.EPERM, name)
            if f == "noop":
                return None
            if callable(f):
                return f(*a)
            return fn(*a)
        return call

    def names(self):
        return [c[0] for c in self.log]

    def _root(self):
        if self.r[1] != 0:
            raise PermissionError(errno.EPERM, "not root")

    def _setgroups(self, gs):
        self._root()
        self.groups = list(gs)

    def _setgid(self, g):
        self._root()
        self.g = [g, g, g]

    def _setuid(self, u):
        self._root()
        self.r = [u, u, u]

    def _seteuid(self, u):
        if self.r[1] != 0 and u not in (self.r[0], self.r[2]):
            raise PermissionError(errno.EPERM, "seteuid")
        self.r[1] = u

    def _setegid(self, g):
        if self.r[1] != 0 and g not in (self.g[0], self.g[2]):
            raise PermissionError(errno.EPERM, "setegid")
        self.g[1] = g

    def _setrlimit(self, which, lim):
        if self.r[1] != 0 and self.nproc[1] != resource.RLIM_INFINITY and lim[1] > self.nproc[1]:
            raise PermissionError(errno.EPERM, "setrlimit")
        self.nproc = tuple(lim)

    def _caps(self):
        z = 0 if self.r[1] != 0 else 0x1FF
        return {"CapInh": 0, "CapPrm": z, "CapEff": z, "CapAmb": 0}

    def _read_file(self, path, cap):
        if path not in self.files:
            raise FileNotFoundError(errno.ENOENT, path)
        return self.files[path]


ENV_TEXT = (f"HOME={S['darwin']}\nPATH=/usr/local/lib/levain-vendor/bin:/usr/bin:/bin\nTMPDIR={S['darwin']}/tmp\n"
            "HTTPS_PROXY=http://127.0.0.1:47100\n").encode()
PROFILE_TEXT = b"(version 1)\n; the forbidden family is never written (trg-069)\n(deny default)\n(allow file-read*)\n"


def fake_for(sp, *, linux=False, faults=None, extra_files=None, fds=(0, 1, 2), path=()):
    files = {}
    if sp.env_file:
        files[sp.env_file] = ENV_TEXT
    if sp.profile:
        files[sp.profile] = PROFILE_TEXT
    files.update(extra_files or {})
    return FakeOS(faults, linux=linux, files=files, fds=fds, spec_bytes=sp.to_json(), path=path)


@pytest.fixture(autouse=True)
def _no_real_execve(monkeypatch):
    yield from refuse_real_execve(monkeypatch, wall.os, route=True)


@pytest.fixture
def darwin(monkeypatch):
    monkeypatch.setattr(wall.sys, "platform", "darwin")


@pytest.fixture
def linux(monkeypatch):
    monkeypatch.setattr(wall.sys, "platform", "linux")


# --- T:2/test_vendor_id_is_slot (plan level) ------------------------------------------------------------------


@pytest.mark.parametrize("target,slot,account", [("vendor", E0, "_levain_s0"), ("clone", E0, "_levain_s0"),
                                                 ("clone", V0, "_levain_c0"), ("step", V0, "_levain_c0"),
                                                 ("driver", V0, "_levain_v0")])
def test_vendor_id_is_slot_macos(darwin, target, slot, account):
    """§8.2 on every macOS launch path: asuser <the slot's uid> -> the stage; the stage's first acts are the
    drop, in order, then the asserts, RLIMIT_NPROC, the profile guard, and only then the exec, which runs
    the target under sandbox-exec -f <S>/root/profile.sb."""
    sp = spec("darwin", target, slot)
    uid = IDS["darwin"][account]
    assert (sp.uid, sp.gid, sp.groups) == (uid, uid, (uid, SHARED["darwin"]["levain-slots"]))
    argv = wall.macos_launch_argv(sp)
    assert argv[:3] == ("/bin/launchctl", "asuser", str(uid))
    assert argv[3:] == wall.stage_argv("darwin") and argv[-3:] == ("levain.slot.wall", "--spec-fd", "3")
    f = fake_for(sp, path={"/usr/bin/pytest"} if target == "step" else ())
    assert wall.stage_main(3, f.calls) == wall.STAGE_EXIT   # a fake execve returns
    n = f.names()
    order = [n.index(x) for x in ("setgroups", "setgid", "setuid", "seteuid", "setegid", "setrlimit", "execve")]
    assert order == sorted(order)
    assert n.index("read_file") > n.index("setrlimit")      # env + profile read AS the slot, after the drop
    path, xargv, env = f.log[-1][1:]
    assert path == "/usr/bin/sandbox-exec"
    assert xargv[:3] == ["/usr/bin/sandbox-exec", "-f", f"{S['darwin']}/root/profile.sb"]
    assert (f.r, f.g, sorted(f.groups)) == ([uid] * 3, [uid] * 3, sorted(sp.groups))
    assert f.nproc == (wall.NPROC, wall.NPROC)


@pytest.mark.parametrize("target,slot", [("vendor", E0), ("clone", E0), ("forwarder", E0), ("clone", V0),
                                         ("step", V0), ("driver", V0)])
def test_vendor_id_is_slot_linux(linux, target, slot):
    """§9 on every Linux launch path: nsenter --net -> Levain's bwrap (root) -> the stage, which drops before
    exec (saved ids and capabilities asserted too); the target is exec'd directly, never under a profile."""
    sp = spec("linux", target, slot)
    leaf = {"step": "step0", "driver": "driver"}.get(target)
    la = wall.linux_launch_argv(sp, slot=slot, session=SESSION, kind="verifier" if slot is V0 else "interactive",
                                netns_names=lambda: {"levain-e0", "levain-v0"}, host_links=LINKS, cgroup_leaf=leaf)
    assert la.argv[:4] == ("/usr/bin/nsenter", f"--net=/run/netns/{slot.netns}", "--", "/usr/libexec/levain/bwrap")
    assert la.argv[-len(wall.stage_argv("linux")):] == wall.stage_argv("linux")
    f = fake_for(sp, linux=True, path={"/usr/bin/pytest"})
    wall.stage_main(3, f.calls)
    n = f.names()
    assert n.index("setgroups") < n.index("setgid") < n.index("setuid") < n.index("getresuid") \
        < n.index("capabilities") < n.index("seteuid") < n.index("execve")
    assert f.log[-1][1] != "/usr/bin/sandbox-exec" and f.r == [sp.uid] * 3


def test_front_same_drop_no_profile(darwin):
    """§8.2 last sentence: the front starts by the same drop + assert, with no asuser, no profile and no
    RLIMIT_NPROC (V3 is per slot); its env is exactly what the helper passes, with no env file."""
    sp = spec("darwin", "front", extra_env={"LEVAIN_FRONT_SESSION": SESSION}, keep_fds=[5])
    assert (sp.uid, sp.groups) == (3998, (3998, SHARED["darwin"]["levain-gwfronts"]))
    assert sp.profile is None and sp.env_file is None and sp.nproc is None
    assert wall.macos_launch_argv(sp) == wall.stage_argv("darwin")
    f = fake_for(sp, fds=(0, 1, 2, 5, 6))
    wall.stage_main(3, f.calls)
    assert "setrlimit" not in f.names() and f.names().index("setuid") < f.names().index("execve")
    path, xargv, env = f.log[-1][1:]
    assert path == "/usr/bin/python3" and env == {"LEVAIN_FRONT_SESSION": SESSION}
    assert f.open_fds == {0, 1, 2, 5}
    assert wall.linux_launch_argv(spec("linux", "front"), slot=E0, session=SESSION, kind="interactive",
                                  netns_names=lambda: set()).cgroup is None


def test_launch_spec_paths_derived_from_session(darwin):
    sp = spec("darwin", "vendor", extra_env={"ANTHROPIC_API_KEY": "tok", "TERM": "xterm"}, keep_fds=[7])
    assert sp.cwd == S["darwin"] + "/repo" and sp.env_file == S["darwin"] + "/root/env"
    assert sp.argv == (VENDOR["darwin"], "--resume") and not sp.resolve
    assert "tok" not in " ".join(wall.macos_launch_argv(sp))          # a handle never rides argv (§3.2)
    assert spec("darwin", "clone").argv == ("/usr/bin/git", "-c", "core.hooksPath=/dev/null", "-c", "gc.auto=0",
                                            "-c", "maintenance.auto=false", "clone", "--template=",
                                            S["darwin"] + "/in/repo.bundle", S["darwin"] + "/repo")
    drv = spec("darwin", "driver", V0)
    assert drv.cwd == S["darwin"] + "/driver" and drv.env_file == S["darwin"] + "/root/driver.env"
    assert spec("darwin", "step", V0).env_file == S["darwin"] + "/root/env"
    assert spec("darwin", "step", V0).resolve
    fw = spec("linux", "forwarder")
    assert fw.argv[-6:] == ("--port", "47100", "--unix", "/run/levain/gw/slot0/gw.sock", "--ready-fd", "4")
    assert fw.keep_fds == (wall.READY_FD,) and fw.exec_fd is None
    assert fw.cwd == S["linux"] + "/tmp" and fw.env_file is None and not fw.extra_env


@pytest.mark.parametrize("kw,name", [
    (dict(target="vendor", argv=["/usr/local/bin/claude"]), "target-off-list"),
    (dict(target="vendor", argv=["/usr/local/lib/levain-vendor/../../bin/sh"]), "target-off-list"),
    (dict(target="driver", slot=V0, argv=["/bin/sh", "-c", "x"]), "target-off-list"),
    (dict(target="step", slot=E0), "bad-token"),
    (dict(target="vendor", slot=V0), "bad-token"),
    (dict(target="forwarder"), "unsupported-host"),
    (dict(target="clone", argv=["/usr/bin/git", "fetch"]), "spec-malformed"),
    (dict(target="clone", extra_env={"ANTHROPIC_API_KEY": "t"}), "spec-malformed"),
    (dict(target="vendor", keep_fds=[7, 9]), "fd-off-list"),
    (dict(target="clone", keep_fds=[7]), "fd-off-list"),
    (dict(target="vendor", keep_fds=[3]), "spec-malformed"),
    (dict(target="vendor", env_file="/private/var/levain/live/" + OTHER + "/root/env"), "view-path-off-list"),
    (dict(target="vendor", profile="/tmp/profile.sb"), "view-path-off-list"),
    (dict(target="vendor", session="../x"), "bad-token"),
    (dict(target="step", slot=V0, argv=["x"] * 65), "spec-malformed"),
])
def test_launch_spec_refusals(kw, name):
    target, slot = kw.pop("target"), kw.pop("slot", E0)
    with pytest.raises(Refused) as e:
        spec("darwin", target, slot, **kw)
    assert e.value.name == name


def test_principal_refuses_out_of_range_and_singletons():
    with pytest.raises(Refused):
        wall.principal("darwin", "_levain_s0", {"_levain_s0": 501}, SHARED["darwin"])
    with pytest.raises(Refused):
        wall.principal("darwin", "_levain_keys", {"_levain_keys": 3799}, SHARED["darwin"])
    with pytest.raises(Refused):
        wall.principal("darwin", "_levain_s0", IDS["darwin"], {"levain-slots": 20})


# --- drop_and_assert failure matrix: each step fails -> no exec ----------------------------------------------------


def _einval(*a):
    raise OSError(errno.EINVAL, "x")


MATRIX = [
    ("setgroups", "raise", "drop-failed"), ("setgid", "raise", "drop-failed"), ("setuid", "raise", "drop-failed"),
    ("setgroups", "noop", "drop-assert"), ("setgid", "noop", "drop-assert"), ("setuid", "noop", "drop-assert"),
    ("getgroups", lambda: [3999, 3789, 80], "drop-assert"),       # one group too many
    ("getgroups", lambda: [3999], "drop-assert"),                 # one too few
    ("seteuid", lambda u: None, "privilege-regained"), ("setegid", lambda g: None, "privilege-regained"),
    ("seteuid", _einval, "drop-assert"),                          # a failure that is not EPERM proves nothing
    ("setrlimit", "raise", "drop-failed"), ("setrlimit", "noop", "drop-assert"),
]


@pytest.mark.parametrize("step,fault,name", MATRIX)
def test_drop_failure_matrix_no_exec(darwin, step, fault, name):
    sp = spec("darwin", "vendor")
    with pytest.raises(Refused) as e:
        wall.drop_and_assert(sp.uid, sp.gid, sp.groups, os_calls=fake_for(sp, faults={step: fault}).calls)
    assert e.value.name == name
    f = fake_for(sp, faults={step: fault})
    assert wall.stage_main(3, f.calls) == wall.STAGE_EXIT
    assert "execve" not in f.names()


@pytest.mark.parametrize("step,fault", [("getresuid", lambda: (899, 899, 0)), ("getresgid", lambda: (899, 0, 899)),
                                        ("capabilities", lambda: {"CapPrm": 0, "CapEff": 0x20, "CapAmb": 0})])
def test_drop_failure_linux_saved_ids_and_caps(linux, step, fault):
    sp = spec("linux", "vendor")
    f = fake_for(sp, linux=True, faults={step: fault})
    assert wall.stage_main(3, f.calls) == wall.STAGE_EXIT and "execve" not in f.names()


@pytest.mark.parametrize("uid,gid,groups", [(0, 899, (899,)), (899, 0, (0,)), (899, 899, (899, 0)),
                                            (899, 899, (789,)), (899, 899, (899, 899))])
def test_drop_refuses_root_or_bad_set_before_any_call(uid, gid, groups):
    f = FakeOS()
    with pytest.raises(Refused):
        wall.drop_and_assert(uid, gid, groups, os_calls=f.calls)
    assert f.log == []


def test_nproc_never_above_the_hard_limit():
    """V3 'macOS min with kern.maxprocperuid': a hard limit below 2048 is kept, never raised."""
    f = FakeOS()
    f.nproc = (1000, 1333)
    wall.drop_and_assert(3999, 3999, (3999, 3789), os_calls=f.calls)
    assert f.nproc == (1333, 1333)


@pytest.mark.parametrize("bad", [b"", b"{}", b'{"format":"x"}', b"not json"])
def test_stage_refuses_a_bad_spec_before_the_drop(darwin, bad):
    f = FakeOS(spec_bytes=bad)
    assert wall.stage_main(3, f.calls) == wall.STAGE_EXIT
    assert "setuid" not in f.names() and "execve" not in f.names()


def test_stage_refuses_a_noncanonical_spec(darwin):
    sp = spec("darwin", "vendor")
    f = FakeOS(spec_bytes=sp.to_json().replace(b",", b", ", 1))
    assert wall.stage_main(3, f.calls) == wall.STAGE_EXIT and "execve" not in f.names()


# --- the env: exactly the rendered file + the helper's extras ------------------------------------------------------


def test_env_is_exactly_the_file_plus_extras(darwin):
    sp = spec("darwin", "vendor", extra_env={"ANTHROPIC_API_KEY": "tok", "TERM": "xterm-256color"})
    f = fake_for(sp)
    wall.stage_main(3, f.calls)
    env = f.log[-1][3]
    assert env == {**wall.parse_env(ENV_TEXT), "ANTHROPIC_API_KEY": "tok", "TERM": "xterm-256color"}


def test_extra_env_cannot_override_the_rendered_env(darwin):
    sp = spec("darwin", "vendor", extra_env={"HOME": "/Users/phill"})
    f = fake_for(sp)
    assert wall.stage_main(3, f.calls) == wall.STAGE_EXIT and "execve" not in f.names()


@pytest.mark.parametrize("text", [b"A=1\nA=2\n", b"A=1", b"A=1\r\n", b"A\n", b"1A=x\n", b"A=\x00\n", b"\n",
                                  b"A=1\n\nB=2\n", b"\xff=1\n", b"export A=1\n"])
def test_env_file_refusals(text):
    with pytest.raises(Refused) as e:
        wall.parse_env(text)
    assert e.value.name == "env-malformed"


def test_env_file_value_keeps_every_byte_after_the_first_equals():
    assert wall.parse_env(b"A=b=c d\nnpm_config_cache=/x\nE=\n") == {"A": "b=c d", "npm_config_cache": "/x", "E": ""}
    assert wall.parse_env(b"") == {}


def test_missing_env_file_no_exec(darwin):
    sp = spec("darwin", "vendor")
    f = FakeOS(files={sp.profile: PROFILE_TEXT}, spec_bytes=sp.to_json())
    assert wall.stage_main(3, f.calls) == wall.STAGE_EXIT and "execve" not in f.names()


# --- T:2/test_lease_fd_absent_in_vendor (plan half; the real-process half is test_slot_s2d_stage_exec.py) -------


def test_lease_fd_absent_in_vendor(darwin):
    """§3.1 step 4: the vendor's fds are 0, 1, 2 + its pty. A lease fd (or any inherited fd) is closed before
    exec, the pty is made inheritable, and the spec fd never survives."""
    sp = spec("darwin", "vendor", keep_fds=[7])
    f = fake_for(sp, fds=(0, 1, 2, 3, 7, 9, 11))
    wall.stage_main(3, f.calls)
    assert f.names()[-1] == "execve"
    assert f.open_fds == {0, 1, 2, 7} and {7, 0, 1, 2} <= f.inheritable
    assert all(("close", fd) in f.log for fd in (3, 9, 11))


def test_fd_still_open_after_close_refused(darwin):
    sp = spec("darwin", "vendor")
    f = fake_for(sp, fds=(0, 1, 2, 9), faults={"close": "noop"})
    assert wall.stage_main(3, f.calls) == wall.STAGE_EXIT and "execve" not in f.names()


def test_step_resolved_on_session_path_as_the_child(darwin):
    sp = spec("darwin", "step", V0, argv=["pytest", "-q"])
    f = fake_for(sp, path={"/usr/bin/pytest"})
    wall.stage_main(3, f.calls)
    assert f.log[-1][2] == ["/usr/bin/sandbox-exec", "-f", sp.profile, "/usr/bin/pytest", "-q"]
    f = fake_for(sp, path=set())
    assert wall.stage_main(3, f.calls) == wall.STAGE_EXIT and "execve" not in f.names()


def test_cwd_failure_no_exec(darwin):
    sp = spec("darwin", "vendor")
    f = fake_for(sp, faults={"chdir": "raise"})
    assert wall.stage_main(3, f.calls) == wall.STAGE_EXIT and "execve" not in f.names()
    assert ("chdir", S["darwin"] + "/repo") in f.log


# --- T:2/test_profile_has_no_syscall_rule: the apply-time guard ------------------------------------------------


@pytest.mark.parametrize("text", [
    f"(version 1)\n(deny {SC}-unix)\n",
    f"(version 1)\n(allow {SC}-mach ({'mach' + 'trap'}-number 31))\n",
    f"(version 1)\n({SC.upper()}-UNIX)\n",
    f'(version 1)\n(allow (string->symbol "{SC}-unix"))\n',
    f"(version 1)\n#| comment |# (deny {SC}-mig)\n",
])
def test_profile_has_no_syscall_rule(text):
    with pytest.raises(Refused) as e:
        wall.check_profile_text(text)
    assert e.value.name == "profile-syscall-rule"


@pytest.mark.parametrize("text", [f'(version 1)\n(allow file-read* (literal "/a;b"))\n; {SC} rules are never written\n',
                                  f"(version 1)\n#| {SC}-unix is forbidden (trg-069) |#\n(deny default)\n"])
def test_profile_comments_may_name_the_rule(text):
    wall.check_profile_text(text)


@pytest.mark.parametrize("text", ['(allow file-read* (literal "/a))\n', "#| never closed\n(deny default)"])
def test_profile_unreadable_refused(text):
    with pytest.raises(Refused) as e:
        wall.check_profile_text(text)
    assert e.value.name == "profile-unreadable"


def test_stage_refuses_a_syscall_profile_before_apply(darwin):
    sp = spec("darwin", "vendor")
    f = fake_for(sp, extra_files={sp.profile: f"(version 1)\n(deny {SC}-unix)\n(deny default)\n".encode()})
    assert wall.stage_main(3, f.calls) == wall.STAGE_EXIT
    assert "execve" not in f.names()


def test_stage_refuses_a_profile_off_macos(linux):
    sp = spec("darwin", "vendor")
    f = fake_for(sp)
    assert wall.stage_main(3, f.calls) == wall.STAGE_EXIT and "execve" not in f.names()


# --- §9.2: the Linux view, golden + off-list ----------------------------------------------------------------------


GOLDEN_E0 = (
    "--die-with-parent", "--unshare-ipc", "--unshare-pid", "--unshare-uts",
    "--ro-bind", "/usr", "/usr", "--ro-bind", "/etc", "/etc",
    "--symlink", "usr/bin", "/bin", "--symlink", "usr/sbin", "/sbin", "--symlink", "usr/lib", "/lib",
    "--proc", "/proc", "--dev-bind", "/dev", "/dev", "--tmpfs", "/dev/shm",
    "--tmpfs", "/tmp", "--tmpfs", "/var/tmp", "--tmpfs", "/run",
    # each bind's ancestors, parents first, with §2.1 / §2.2's modes (VM run s2-linux F4)
    "--perms", "0755", "--dir", "/var", "--perms", "0755", "--dir", "/var/levain",
    "--perms", "0711", "--dir", "/var/levain/live",
    "--perms", "0755", "--dir", "/run", "--perms", "0755", "--dir", "/run/levain",
    "--perms", "0755", "--dir", "/run/levain/gw", "--perms", "0755", "--dir", "/run/levain/qb",
    "--bind", "/var/levain/live/" + SESSION, "/var/levain/live/" + SESSION,
    "--bind", "/run/levain/gw/slot0", "/run/levain/gw/slot0",
    "--bind", "/run/levain/qb/slot0", "/run/levain/qb/slot0",
    "--chdir", "/",
)


def test_linux_view_golden_argv():
    sp = spec("linux", "vendor", extra_env={"TERM": "xterm"}, keep_fds=[7])
    la = wall.linux_launch_argv(sp, slot=E0, session=SESSION, kind="interactive", queue_grant=True,
                                netns_names=lambda: ["levain-e0", "levain-v0"], host_links=LINKS)
    assert la.argv == (("/usr/bin/nsenter", "--net=/run/netns/levain-e0", "--", "/usr/libexec/levain/bwrap")
                       + GOLDEN_E0 + ("--",) + wall.stage_argv("linux"))
    assert la.cgroup == "/sys/fs/cgroup/levain.slice/e0.scope/" + SESSION
    assert "/sys" not in la.argv and "--unshare-net" not in la.argv


def test_linux_view_grants_by_class_and_kind():
    def binds(slot, **kw):
        a = wall.linux_view_args(slot, SESSION, host_links=LINKS, **kw)
        return [a[i + 1] for i, x in enumerate(a) if x == "--bind"]
    assert binds(E0, kind="interactive", queue_grant=False) == ["/var/levain/live/" + SESSION, "/run/levain/gw/slot0"]
    assert binds(E1, kind="planner", queue_grant=True) == ["/var/levain/live/" + SESSION, "/run/levain/gw/slot1",
                                                           "/run/levain/qb/slot1", "/run/levain/planner/slot1"]
    assert binds(V0, kind="verifier", queue_grant=False) == ["/var/levain/live/" + SESSION]


@pytest.mark.parametrize("slot,kw", [
    (E0, dict(kind="interactive", queue_grant=False, ro_prefixes=["/opt/homebrew"])),
    (E0, dict(kind="interactive", queue_grant=False, ro_prefixes=["/home"])),
    (V0, dict(kind="verifier", queue_grant=True)),
    (E0, dict(kind="interactive", queue_grant=False, host_links={"/bin": "/opt/bin"})),
    (E0, dict(kind="interactive", queue_grant=False, host_links={"/lib": "usr/../etc"})),
    (E0, dict(kind="interactive", queue_grant=False, host_links={"/bin": ""})),   # a real dir, not a link
])
def test_linux_view_off_list_refused_by_name(slot, kw):
    kw.setdefault("host_links", LINKS)
    with pytest.raises(Refused) as e:
        wall.linux_view_args(slot, SESSION, **kw)
    assert e.value.name == "view-path-off-list"


def test_linux_view_prefix_outside_usr_is_bound_ro(monkeypatch):
    from levain.slot import root_tree
    monkeypatch.setitem(root_tree.VENDOR_PREFIX, "linux", PurePosixPath("/opt/levain-vendor"))
    a = wall.linux_view_args(E0, SESSION, kind="interactive", queue_grant=False, host_links=LINKS)
    assert ("--ro-bind", "/opt/levain-vendor", "/opt/levain-vendor") == a[10:13]


def test_netns_missing_refused():
    sp = spec("linux", "vendor")
    with pytest.raises(Refused) as e:
        wall.linux_launch_argv(sp, slot=E0, session=SESSION, kind="interactive",
                               netns_names=lambda: ["levain-e1", "levain-v0"], host_links=LINKS)
    assert (e.value.name, e.value.detail) == ("netns-missing", "levain-e0")
    # egress 0 and verifier 0 are distinct namespaces (§1.1): v0 present never satisfies e0
    with pytest.raises(Refused):
        wall.require_netns(E0, ["levain-v0"])


def test_read_netns_names_fails_loudly():
    class R:
        returncode, stdout, stderr = 1, "", "boom"
    with pytest.raises(Refused) as e:
        wall.read_netns_names(run=lambda *a, **k: R())
    assert e.value.name == "netns-missing"

    class OK:
        returncode, stdout, stderr = 0, "levain-e0 (id: 1)\nlevain-v0\n", ""
    assert wall.read_netns_names(run=lambda *a, **k: OK()) == {"levain-e0", "levain-v0"}


def test_verifier_cgroups_one_per_step():
    sp = spec("linux", "step", V0)
    with pytest.raises(Refused):
        wall.linux_launch_argv(sp, slot=V0, session=SESSION, kind="verifier", netns_names=lambda: ["levain-v0"],
                               host_links=LINKS)
    la = wall.linux_launch_argv(sp, slot=V0, session=SESSION, kind="verifier", netns_names=lambda: ["levain-v0"],
                                host_links=LINKS, cgroup_leaf="step3")
    assert la.cgroup == "/sys/fs/cgroup/levain.slice/v0.scope/" + SESSION + "/step3"
    with pytest.raises(Refused):
        wall.cgroup_dir(V0, SESSION, "../x")


def test_exec_launch_joins_and_proves_the_cgroup_before_exec():
    cg = "/sys/fs/cgroup/levain.slice/e0.scope/" + SESSION
    f = FakeOS(files={"/proc/self/cgroup": f"0::/levain.slice/e0.scope/{SESSION}\n"})
    wall.exec_launch(("/usr/bin/nsenter", "x"), 8, cgroup=cg, os_calls=f.calls)
    assert f.names() == ["dup2", "close", "write_file", "read_text", "execve"]
    assert f.log[2] == ("write_file", cg + "/cgroup.procs", "4242\n") and f.log[-1][3] == {}
    f = FakeOS(files={"/proc/self/cgroup": "0::/user.slice\n"})
    with pytest.raises(Refused) as e:
        wall.exec_launch(("/usr/bin/nsenter", "x"), 3, cgroup=cg, os_calls=f.calls)
    assert e.value.name == "cgroup-not-joined" and "execve" not in f.names()


# --- T:2/test_other_slot_cannot_list_session: what wall.py decides --------------------------------------------------


def test_other_slot_cannot_list_session_plan_half():
    """The modes that keep a slot out of another session (``<S>`` root : _levain_s<n> 0750) are 2d's apply. What
    the wall decides: each slot runs with ONLY its own group + levain-slots, never another slot's group; its
    Linux view binds only its own ``<S>`` (no ``L/live``, no other session) and only its own socket dirs."""
    for host in ("darwin", "linux"):
        a, b = spec(host, "vendor", E0), spec(host, "vendor", E1)
        assert set(a.groups) & set(b.groups) == {SHARED[host]["levain-slots"]}
        assert b.gid not in a.groups and a.gid not in b.groups
    a = wall.linux_view_args(E0, SESSION, kind="interactive", queue_grant=True, host_links=LINKS)
    b = wall.linux_view_args(E1, OTHER, kind="interactive", queue_grant=True, host_links=LINKS)
    bound = {a[i + 1] for i, x in enumerate(a) if x in ("--bind", "--ro-bind", "--dev-bind")}
    assert "/var/levain/live" not in bound and "/var/levain" not in bound   # only made empty in the view (F4)
    made = {a[i + 1] for i, x in enumerate(a) if x == "--dir"}
    assert not any(SESSION in d for d in made)                               # <S> is the one bind of the store
    assert not any(OTHER in x or "slot1" in x for x in a) and not any(SESSION in x or "slot0" in x for x in b)


# --- carried item F5: macOS egress start needs the live anchor + its alias ------------------------------------------

#: The live readback of the sub-anchor in the 1c VM, verbatim (runs/s1-1c-macos/raw/C08_C09_pf_lo0.txt lines 2-27:
#: egress 0 = 3999, egress 1 = 3997, verifier pair 0 = 3995 / 3994).
LIVE_1C = """No ALTQ support in kernel
ALTQ related functions disabled
block drop in quick on lo0 inet proto tcp from any to 127.0.1.1 user != 3999 flags S/SA
block drop in quick on lo0 inet proto tcp from any to 127.0.1.2 user != 3997 flags S/SA
block drop in quick inet proto tcp from any to ! 127.0.1.1 user = 3999 flags S/SA
block drop in quick inet proto tcp from any to ! 127.0.1.2 user = 3997 flags S/SA
block drop in quick inet6 proto tcp all user = 3999 flags S/SA
block drop in quick inet6 proto tcp all user = 3997 flags S/SA
pass out quick on lo0 inet proto tcp from any to 127.0.0.1 port = 47100 user = 3999 flags S/SA keep state
pass out quick on lo0 inet proto tcp from any to 127.0.0.1 port = 47101 user = 3997 flags S/SA keep state
pass out quick on lo0 inet proto tcp from any to 127.0.1.1 user = 3999 flags S/SA keep state
pass out quick on lo0 inet proto tcp from any to 127.0.1.2 user = 3997 flags S/SA keep state
pass in quick on lo0 inet proto tcp from any to 127.0.1.1 user = 3999 flags S/SA keep state
pass in quick on lo0 inet proto tcp from any to 127.0.1.2 user = 3997 flags S/SA keep state
block drop out quick on lo0 inet proto tcp from any to 127.0.1.1
block drop out quick on lo0 inet proto tcp from any to 127.0.1.2
block drop out quick proto tcp all user = 3999
block drop out quick proto tcp all user = 3997
block drop out quick proto tcp all user = 3995
block drop out quick proto tcp all user = 3994
block drop out quick on lo0 inet proto udp from any to 127.0.1.1
block drop out quick on lo0 inet proto udp from any to 127.0.1.2
block drop out quick proto udp all user = 3999
block drop out quick proto udp all user = 3997
block drop out quick proto udp all user = 3995
block drop out quick proto udp all user = 3994
"""

LO0 = """lo0: flags=8049<UP,LOOPBACK,RUNNING,MULTICAST> mtu 16384
\toptions=1203<RXCSUM,TXCSUM,TXSTATUS,SW_TIMESTAMP>
\tinet 127.0.0.1 netmask 0xff000000
\tinet6 ::1 prefixlen 128
\tinet 127.0.1.1 netmask 0xffffffff
\tinet 127.0.1.2 netmask 0xffffffff
"""


def test_pf_printed_equals_the_measured_live_readback():
    """pf.py's rules for the 1c slots, in pfctl's printed form, ARE the 24 lines the VM's live anchor held."""
    rules = anchor_rules([E0, E1, V0], IDS["darwin"])
    mine = {line for r in rules for line in wall.pf_printed(r)}
    live = {line for line in LIVE_1C.splitlines() if not line.startswith(("No ALTQ", "ALTQ"))}
    assert mine == live


def test_macos_egress_ready_both_signs():
    wall.check_egress_ready(E0, IDS["darwin"], pf_text=LIVE_1C, ifconfig_text=LO0)
    wall.check_egress_ready(E1, IDS["darwin"], pf_text=LIVE_1C, ifconfig_text=LO0)
    one_gone = "\n".join(line for line in LIVE_1C.splitlines() if "port = 47100" not in line)
    for pf_text, lo0, name in [
        ("", LO0, "pf-anchor-not-loaded"),
        (one_gone, LO0, "pf-anchor-not-loaded"),
        (LIVE_1C.replace("3999", "3001"), LO0, "pf-anchor-not-loaded"),
        (LIVE_1C + "block drop out quick on lo0 inet proto tcp from any to <__automatic_0>\n", LO0,
         "pf-anchor-not-loaded"),
        (LIVE_1C, LO0.replace("\tinet 127.0.1.1 netmask 0xffffffff\n", ""), "loopback-alias-down"),
        (LIVE_1C, LO0.replace("inet 127.0.1.1 ", "inet 127.0.1.11 "), "loopback-alias-down"),
        (LIVE_1C, LO0.replace("<UP,LOOPBACK", "<LOOPBACK"), "loopback-alias-down"),
    ]:
        with pytest.raises(Refused) as e:
            wall.check_egress_ready(E0, IDS["darwin"], pf_text=pf_text, ifconfig_text=lo0)
        assert e.value.name == name


def test_macos_egress_ready_reads_the_sub_anchor():
    seen = []

    class R:
        def __init__(self, out):
            self.returncode, self.stdout, self.stderr = 0, out, ""

    def run(argv, **kw):
        seen.append(argv)
        if argv[:3] == ["/sbin/pfctl", "-s", "info"]:
            return R(INFO_ENABLED)
        return R(LIVE_1C if argv[0] == "/sbin/pfctl" else LO0)
    wall.macos_egress_ready(E0, IDS["darwin"], run=run)
    assert seen == [["/sbin/pfctl", "-s", "info"], ["/sbin/pfctl", "-a", "com.apple/levain", "-sr"],
                    ["/sbin/ifconfig", "lo0"]]


#: ``pfctl -s info``'s status line, measured after a reboot in r3c10-vm 10-26 (levain project_memory
#: 1026-8/pfboot raw/r2_after_reboot.txt).
INFO_ENABLED = "Status: Enabled for 0 days 00:00:10           Debug: Urgent\n\nState Table   Total  Rate\n"
#: The status before the boot one-shot existed (same dir, raw/m1.txt: ``Status: Disabled``).
INFO_DISABLED = INFO_ENABLED.replace("Enabled", "Disabled")


def test_check_pf_enabled_both_signs():
    wall.check_pf_enabled(INFO_ENABLED)
    for text in (INFO_DISABLED, "", "Status:Enabled\n", "Debug: Urgent Status: Enabled\n"):
        with pytest.raises(Refused) as e:
            wall.check_pf_enabled(text)
        assert e.value.name == "pf-disabled"


def test_macos_egress_ready_refuses_while_pf_is_disabled():
    """L3 r1 codex HIGH (lane pfboot): the anchor holds every rule and lo0 the alias, but pf is off, so nothing is
    enforced and the start is refused."""
    class R:
        def __init__(self, out):
            self.returncode, self.stdout, self.stderr = 0, out, ""

    def run(info):
        def r(argv, **kw):
            if argv[:3] == ["/sbin/pfctl", "-s", "info"]:
                return R(info)
            return R(LIVE_1C if argv[0] == "/sbin/pfctl" else LO0)
        return r
    wall.macos_egress_ready(E0, IDS["darwin"], run=run(INFO_ENABLED))
    with pytest.raises(Refused) as e:
        wall.macos_egress_ready(E0, IDS["darwin"], run=run(INFO_DISABLED))
    assert e.value.name == "pf-disabled"


@pytest.mark.parametrize("rule", ["pass out quick proto tcp from 10.0.0.1 to any", "nat on en0 from any to any",
                                  "block out quick proto { tcp udp user 5", "block out quick proto tcp rdr-to 1.2.3.4"])
def test_pf_printed_refuses_a_shape_it_does_not_know(rule):
    with pytest.raises(Refused) as e:
        wall.pf_printed(rule)
    assert e.value.name == "pf-rule-unparsed"


# --- the stage's own argv ---------------------------------------------------------------------------------------


def test_stage_argv_runs_the_root_tree_isolated():
    a = wall.stage_argv("darwin")
    assert a[:3] == ("/usr/bin/python3", "-IS", "-c") and "/usr/local/lib/levain-root" in a[3]
    assert wall.stage_argv("linux")[3].count("/usr/lib/levain-root") == 1
    assert wall.main(["--spec-fd", "x"]) == wall.STAGE_EXIT
    with pytest.raises(Refused):
        wall.module_argv("darwin", "os; import x")


@pytest.mark.skipif(sys.platform != "darwin", reason="libSystem's getgroups")
def test_darwin_kernel_groups_reads_this_process():
    import os
    assert os.getegid() in wall._darwin_kernel_groups()

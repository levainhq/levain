"""S1 session 5c-i: the export wall's builders and targets (s5adopt DESIGN §5b, §4.9 principals). Plan-level only:
every profile and view is text, every OS act the s2d fake kernel. Nothing here applies a profile or runs bwrap."""
from __future__ import annotations

import dataclasses

import pytest

from levain.slot import Refused
from levain.slot import render as rd
from levain.slot import wall
from tests.execve_guard import refuse_real_execve
from tests.test_slot_s2d_wall import E0, FakeOS
from tests.test_slot_s2d_wall import spec as slot_spec

@pytest.fixture(autouse=True)
def _no_real_execve(monkeypatch):
    yield from refuse_real_execve(monkeypatch, wall.os, route=True)


#: The forbidden Seatbelt operation family, assembled at runtime so no file spells it (trg-069 guard).
SC = "sys" + "call"
LINKS = {"/bin": "usr/bin", "/sbin": "usr/sbin", "/lib": "usr/lib", "/lib64": None}
IDS = {"darwin": {"_levain_export": 3798, "_levain_verify": 3797},
       "linux": {"_levain_export": 798, "_levain_verify": 797}}
SHARED = {"darwin": {"levain-export": 3784, "levain-dispatch-export": 3783, "levain-verify": 3781,
                     "levain-slots": 3789},
          "linux": {"levain-export": 784, "levain-dispatch-export": 783, "levain-verify": 781, "levain-slots": 789}}
ROLES = {"importer": ("_levain_export", ".work", "operator"), "verifier": ("_levain_verify", ".verify", None)}


@pytest.mark.parametrize("role", sorted(ROLES))
def test_export_walls_render_their_capability_list(role):
    """§5b: each export profile and view is the shared builder over the role's facts, exactly (no syscall rule, one
    rw path, the three user-data subpaths denied, /dev/null + /dev/zero + /dev/urandom the only devices on macOS; on
    Linux a minimal /dev, no --dev-bind, capabilities dropped but the two the stage's drop spends)."""
    account, d, _ = ROLES[role]
    profile = (rd.render_importer_profile if role == "importer" else rd.render_verifier_profile)("darwin")
    devs = '(literal "/dev/null") (literal "/dev/zero") (literal "/dev/urandom")'
    assert profile.decode() == "\n".join([
        "(version 1)", f";; Levain export wall profile, target {role}, principal {account}.", "(allow default)",
        '(deny file-read* (subpath "/Users") (subpath "/System/Volumes/Data/Users") (subpath "/Volumes"))',
        "(deny file-write*)", "(allow file-write*", f'  (subpath "/private/var/levain/export/{d}")',
        f"  {devs})", '(deny file-read* file-ioctl (subpath "/dev"))',
        f"(allow file-read* file-ioctl {devs})", "(deny network*)",
        "(deny ipc-posix*)", "(deny ipc-sysv*)", "(deny mach-lookup)", "(deny mach-register)",
        '(allow mach-lookup (global-name "com.apple.system.opendirectoryd.libinfo"))']) + "\n"
    assert SC not in profile.decode().lower()
    wall.check_profile_text(profile.decode())
    for bad in ("x\n(allow file-write* (subpath \"/\"))", "x\ry", "x\x00y"):     # L3 r1: the label is a comment line
        with pytest.raises(Refused) as ei:
            rd.profile_lines(dataclasses.replace(wall.export_wall_facts("darwin", role), label=bad))
        assert ei.value.name == "internal"
    p = f"/var/levain/export/{d}"
    view = (wall.importer_view_args if role == "importer" else wall.verifier_view_args)(LINKS)
    assert view == tuple((
        "--die-with-parent --unshare-net --unshare-ipc --unshare-pid --unshare-uts --new-session --cap-drop ALL "
        "--cap-add CAP_SETGID --cap-add CAP_SETUID --ro-bind /usr /usr --ro-bind /etc /etc --symlink usr/bin /bin "
        "--symlink usr/sbin /sbin --symlink usr/lib /lib --proc /proc --dev /dev --tmpfs /dev/shm --tmpfs /tmp "
        "--tmpfs /var/tmp --tmpfs /run --perms 0755 --dir /var --perms 0755 --dir /var/levain --perms 0755 --dir "
        f"/var/levain/export --bind {p} {p} --chdir {p}").split())
    sp = wall.export_launch_spec("linux", role, ids=IDS["linux"], shared=SHARED["linux"],
                                 export_class=ROLES[role][2])
    launch = wall.export_linux_launch_argv(sp, host_links=LINKS)
    scope = "export.scope" if role == "importer" else "verify.scope"
    assert launch == wall.LinuxLaunch(f"/sys/fs/cgroup/levain.slice/{scope}/run",
                                      ("/usr/libexec/levain/bwrap",) + view + ("--",) + wall.stage_argv("linux"))
    with pytest.raises(Refused) as ei:
        wall.launch_spec("linux", role, slot=None, session="s-0123456789abcdef", ids=IDS["linux"],
                         shared=SHARED["linux"])
    assert ei.value.name == "bad-token"      # the slot path never builds an export target


@pytest.mark.parametrize("role", sorted(ROLES))
def test_export_walls_let_the_interpreter_read_urandom(role):
    """1026+8 wallexec, measured in r3c10-vm: the export target is ``/usr/bin/python3 -IS`` (the entry script's
    shebang), and that interpreter reads ``/dev/urandom`` before any Levain code runs. With the profile's ``/dev``
    read denial covering it, the importer died ``Fatal Python error: _Py_HashRandomization_Init: failed to get
    random numbers`` (Seatbelt: ``deny(1) file-read-data /dev/urandom``) and every adopt refused
    ``importer-failed:result-malformed: 'no result line'``. The device must be on the profile's read allow-list."""
    profile = (rd.render_importer_profile if role == "importer" else rd.render_verifier_profile)("darwin").decode()
    allow_read = [ln for ln in profile.splitlines() if ln.startswith("(allow file-read* file-ioctl ")]
    assert len(allow_read) == 1 and '(literal "/dev/urandom")' in allow_read[0]
    assert profile.splitlines().index(allow_read[0]) > profile.splitlines().index(
        '(deny file-read* file-ioctl (subpath "/dev"))')       # the allow follows the deny, so it wins


@pytest.mark.parametrize("host", ["darwin", "linux"])
def test_export_principal_and_root_launched_verifier_stage(host, monkeypatch):
    """§4.9 (A16, S2): ``principal()`` keeps refusing both singletons; ``export_principal`` builds each with its own
    group set and refuses a slot-range uid or the other role's uid; the verifier's spec, fed to the stage as root,
    reaches ``drop_and_assert`` (codex r4 HIGH: the operator-uid launch failed at setgroups)."""
    for name in ("_levain_export", "_levain_verify"):
        with pytest.raises(Refused) as ei:
            wall.principal(host, name, IDS[host], SHARED[host])
        assert ei.value.name == "bad-token"
    ids, shared = IDS[host], SHARED[host]
    assert wall.export_principal(host, "importer", ids, shared, export_class="dispatch").groups == \
        (ids["_levain_export"], shared["levain-dispatch-export"])
    v = wall.export_principal(host, "verifier", ids, shared)
    assert v.groups == (ids["_levain_verify"], shared["levain-verify"])
    slot_uid = 3990 if host == "darwin" else 890
    single = 3792 if host == "darwin" else 792       # a free singleton-range uid, then any other singleton holding it
    for bad_ids in ({**ids, "_levain_verify": slot_uid}, {**ids, "_levain_verify": ids["_levain_export"]},
                    {**ids, "_levain_verify": single, "_levain_control": single},
                    {**ids, "_levain_verify": single, "_levain_keys": single}):
        with pytest.raises(Refused) as ei:
            wall.export_principal(host, "verifier", bad_ids, shared)
        assert ei.value.name == "export-account-bad"
    free = 3780 if host == "darwin" else 780          # L3 r1: a collision with ANY other shared group refuses
    for role, cls, bad_shared in (("verifier", None, {**shared, "levain-verify": free, "levain-keysadmin": free}),
                                  ("verifier", None, {**shared, "levain-verify": free, "levain-control": free}),
                                  ("importer", "operator", {**shared, "levain-export": free, "levain-queue": free})):
        with pytest.raises(Refused) as ei:
            wall.export_principal(host, role, ids, bad_shared, export_class=cls)
        assert ei.value.name == "export-account-bad"
    sp = wall.export_launch_spec(host, "verifier", ids=ids, shared=shared)
    monkeypatch.setattr(wall.sys, "platform", host)
    files = {sp.profile: rd.render_verifier_profile("darwin")} if sp.profile else {}
    fake = FakeOS(linux=host == "linux", files=files, fds=(0, 1, 2, 3, 4), spec_bytes=sp.to_json())
    wall.stage_main(os_calls=fake.calls)
    names = fake.names()
    assert ("setgroups", [v.uid, v.groups[1]]) in fake.log
    assert names.index("setgroups") < names.index("setuid") < names.index("execve")
    assert fake.r == [v.uid] * 3 and fake.nproc == (64, 64) and 4 in fake.open_fds
    entry = f"{'/usr/local/libexec' if host == 'darwin' else '/usr/libexec'}/levain/export-verifier"
    want = ["/usr/bin/sandbox-exec", "-f", sp.profile, entry] if host == "darwin" else [entry]
    assert fake.log[-1] == ("execve", want[0], want, {})


def test_each_launcher_refuses_the_other_walls_target():
    """L3 r1 (codex HIGH): a spec runs only under its own wall. An export spec handed to the slot launcher is
    refused before any netns is read (never nsenter + --dev-bind + the slot cgroup); a slot spec handed to the
    export launcher is refused; on macOS a target's profile must be its own class's."""
    def no_netns():
        raise AssertionError("the slot launcher read the netns list for an export spec")

    for role, cls in (("importer", "operator"), ("verifier", None)):
        sp = wall.export_launch_spec("linux", role, ids=IDS["linux"], shared=SHARED["linux"], export_class=cls)
        with pytest.raises(Refused) as ei:
            wall.linux_launch_argv(sp, slot=E0, session="s-0123456789abcdef", kind="interactive",
                                   netns_names=no_netns, host_links=LINKS)
        assert ei.value.name == "spec-malformed"
        dsp = wall.export_launch_spec("darwin", role, ids=IDS["darwin"], shared=SHARED["darwin"], export_class=cls)
        assert wall.macos_launch_argv(dsp)[:3] == ("/bin/launchctl", "asuser", str(dsp.uid))
        for bad in (dataclasses.replace(dsp, profile=slot_spec("darwin", "vendor").profile),
                    dataclasses.replace(slot_spec("darwin", "vendor"), profile=dsp.profile)):
            with pytest.raises(Refused) as ei:
                wall.macos_launch_argv(bad)
            assert ei.value.name == "spec-malformed"
    with pytest.raises(Refused) as ei:
        wall.export_linux_launch_argv(slot_spec("linux", "vendor"), host_links=LINKS)
    assert ei.value.name == "spec-malformed"


class BwrapCapsOS(FakeOS):
    """bwrap run by root with ``--cap-add``: its ``drop_all_caps`` sets the permitted, effective AND inheritable
    sets to the requested caps, and ``set_ambient_capabilities`` raises them ambient. ``setuid`` away from 0
    clears permitted, effective and ambient (no keep-caps), never inheritable."""

    ADDED = (1 << 6) | (1 << 7)      # CAP_SETGID, CAP_SETUID

    def __init__(self, faults=None, **kw):
        super().__init__(faults, linux=True, **kw)
        self.inh = self.prm = self.amb = self.ADDED
        self.calls.capabilities = self._w("capabilities", self._caps)
        self.calls.clear_inheritable = self._w("clear_inheritable", self._clear_inh)

    def _setuid(self, u):
        super()._setuid(u)
        if u != 0:
            self.prm = self.amb = 0

    def _caps(self):
        return {"CapInh": self.inh, "CapPrm": self.prm, "CapEff": self.prm, "CapAmb": self.amb}

    def _clear_inh(self):
        self.inh = 0
        self.amb &= self.prm & self.inh


def test_export_stage_clears_the_inheritable_set(monkeypatch):
    """L3 r1 LOW: under bwrap's --cap-add the added caps sit in CapInh, which setuid does not clear. The export
    stage clears it before the drop and asserts it empty after; a clear that does not take is refused with no
    exec. The slot stage is unchanged: it makes no such call."""
    monkeypatch.setattr(wall.sys, "platform", "linux")
    sp = wall.export_launch_spec("linux", "verifier", ids=IDS["linux"], shared=SHARED["linux"])
    fake = BwrapCapsOS(fds=(0, 1, 2, 3, 4), spec_bytes=sp.to_json())
    wall.stage_main(os_calls=fake.calls)
    names = fake.names()
    assert (fake.inh, fake.prm, fake.amb) == (0, 0, 0) and "execve" in names
    assert names.index("clear_inheritable") < names.index("setgroups")
    lying = BwrapCapsOS(faults={"clear_inheritable": "noop"}, fds=(0, 1, 2, 3, 4), spec_bytes=sp.to_json())
    assert wall.stage_main(os_calls=lying.calls) == wall.STAGE_EXIT and "execve" not in lying.names()
    from tests.test_slot_s2d_wall import fake_for
    slot = fake_for(slot_spec("linux", "vendor"), linux=True, fds=(0, 1, 2, 3, 4))
    slot.calls.clear_inheritable = slot._w("clear_inheritable", lambda: None)
    wall.stage_main(3, slot.calls)
    assert "execve" in slot.names() and "clear_inheritable" not in slot.names()

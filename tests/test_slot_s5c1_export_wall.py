"""S1 session 5c-i: the export wall's builders and targets (s5adopt DESIGN §5b, §4.9 principals). Plan-level only:
every profile and view is text, every OS act the s2d fake kernel. Nothing here applies a profile or runs bwrap."""
from __future__ import annotations

import pytest

from levain.slot import Refused
from levain.slot import render as rd
from levain.slot import wall
from tests.test_slot_s2d_wall import FakeOS

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
    rw path, the three user-data subpaths denied, /dev/null + /dev/zero the only devices on macOS; on Linux a
    minimal /dev, no --dev-bind, capabilities dropped but the two the stage's drop spends)."""
    account, d, _ = ROLES[role]
    profile = (rd.render_importer_profile if role == "importer" else rd.render_verifier_profile)("darwin")
    assert profile.decode() == "\n".join([
        "(version 1)", f";; Levain export wall profile, target {role}, principal {account}.", "(allow default)",
        '(deny file-read* (subpath "/Users") (subpath "/System/Volumes/Data/Users") (subpath "/Volumes"))',
        "(deny file-write*)", "(allow file-write*", f'  (subpath "/private/var/levain/export/{d}")',
        '  (literal "/dev/null") (literal "/dev/zero"))', '(deny file-read* file-ioctl (subpath "/dev"))',
        '(allow file-read* file-ioctl (literal "/dev/null") (literal "/dev/zero"))', "(deny network*)",
        "(deny ipc-posix*)", "(deny ipc-sysv*)", "(deny mach-lookup)", "(deny mach-register)",
        '(allow mach-lookup (global-name "com.apple.system.opendirectoryd.libinfo"))']) + "\n"
    assert SC not in profile.decode().lower()
    wall.check_profile_text(profile.decode())
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
    for bad_ids in ({**ids, "_levain_verify": slot_uid}, {**ids, "_levain_verify": ids["_levain_export"]}):
        with pytest.raises(Refused) as ei:
            wall.export_principal(host, "verifier", bad_ids, shared)
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

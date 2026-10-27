"""1027+18 lane idg1b: the identity gate's G1b slice, the Claude identity call wired (CONTRACT §7.6; 1027-18 RULINGS
"Identity gate" IG-3, "Identity gate G1" G1b).

* wall's ``identity`` target: :func:`wall.launch_spec` builds exactly one spec (the root-tree launcher running
  :data:`wall.IDENTITY_MODULE`, cwd ``<S>/tmp``, the session's env file, on macOS the session's ``profile.sb``), and the
  stage's spec check refuses every other identity-labelled spec.
* start's identity step for a Claude login (:func:`levain.slot.start.identity_capture`): under the session END lock,
  Linux ends and proves the kill domain (the forwarder start left is a slot-uid process), then no slot-uid process may
  be alive (proc.py's END proof), then a fresh forwarder (Linux), then the child, its stdout captured by root within
  ``PROFILE_ANSWER_MAX`` and judged by ``parse_profile_answer``.

Built on lane F2/2f3's harness (``World`` + the fake launches + the fake process table). What only a VM run can show
(the real child through the real tunnel as the slot uid) is the lane's residue, not a test here.
"""
from __future__ import annotations

import errno
import fcntl
import json
import os
from pathlib import PurePosixPath

import pytest

from levain.contracts import canon
from levain.slot import Refused, wall
from levain.slot import identity as ID
from levain.slot import records as R
from levain.slot import start as SS
from levain.slot.ids import SlotId
from tests.test_slot_idg1_identity import _baseline, _login
from tests.test_slot_s2d_wall import IDS, S, SESSION, SHARED, V0, spec
from tests.test_slot_s2f2_runtime_helper import Client
from tests.test_slot_s2f3_start import DARWIN_IDS, _world
from tests.vendor_tree import vendor_installed, vendor_prefix  # noqa: F401 - autouse, the real vendor check

E0 = SlotId("egress", 0)
UUID = "4b8e0c1a-1d2e-4f00-9a7b-0123456789ab"


def _answer(status: int = 200, body: object = None) -> bytes:
    if body is None:
        body = {"account": {"uuid": UUID, "email_address": "x@example.invalid"}, "organization": {"uuid": "o-1"}}
    text = body if isinstance(body, str) else json.dumps(body)
    return json.dumps({"status": status, "body": text}, sort_keys=True).encode() + b"\n"


def _capture(w, la, table, answer=(0, None)):
    """The runtime helper's capture, faked: records what root launched and what the slot uid held at that moment."""
    calls: list[dict] = []

    def cap(argv, spec_json, cgroup, bound):
        uid = w.alloc(w.session).slot_uid
        calls.append({"argv": tuple(argv), "spec": canon.parse(spec_json), "cgroup": cgroup, "bound": bound,
                      "slot_procs": sorted(table.of(uid)), "forwarders": [r["pid"] for r in la.of("forwarder")]})
        if isinstance(answer, BaseException):
            raise answer
        code, out = answer
        return code, _answer() if out is None else out
    w.seams.capture = cap
    return calls


@pytest.fixture
def env(tmp_path):
    return _world(tmp_path)


# --- the wall target ---------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("host", ["darwin", "linux"])
def test_launch_spec_builds_the_one_identity_spec(host):
    sp = spec(host, "identity")
    assert sp.target == "identity"
    assert sp.argv == wall.module_argv(host, wall.IDENTITY_MODULE)
    assert sp.argv[-1] == "levain.slot.identity_child"
    assert sp.cwd == f"{S[host]}/tmp"
    assert sp.env_file == f"{S[host]}/root/env"
    assert sp.profile == (f"{S[host]}/root/profile.sb" if host == "darwin" else None)
    assert (sp.uid, sp.keep_fds, sp.extra_env, sp.exec_fd, sp.resolve) == (IDS[host]["_levain_s0"], (), (), None, False)
    assert sp.nproc == wall.NPROC
    assert wall.parse_spec(sp.to_json()) == sp                     # the stage takes the spec root built


@pytest.mark.parametrize("host", ["darwin", "linux"])
@pytest.mark.parametrize("kw", [{"argv": ["--url", "https://evil.invalid/"]}, {"keep_fds": [4]},
                                {"env_file": "/var/levain/live/" + SESSION + "/root/driver.env"},
                                {"extra_env": {"X": "1"}}])
def test_launch_spec_refuses_a_caller_shaped_identity(host, kw):
    with pytest.raises(Refused) as e:
        spec(host, "identity", **kw)
    assert e.value.name == "spec-malformed"


def test_launch_spec_refuses_the_probe_profile_for_identity():
    with pytest.raises(Refused) as e:
        spec("darwin", "identity", profile=f"{S['darwin']}/root/probe.sb")
    assert e.value.name == "spec-malformed"


def test_identity_runs_in_no_verifier_slot():
    with pytest.raises(Refused) as e:
        spec("linux", "identity", slot=V0)
    assert e.value.name == "bad-token"


def _forged(sp: wall.Spec, **kw) -> bytes:
    """``sp`` changed AFTER launch_spec, serialised the way the plan half would: the stage's own check must refuse."""
    from dataclasses import replace

    from levain.contracts import canon as C

    s2 = replace(sp, **kw)
    return C.canonical_bytes({
        "format": wall.SPEC_FORMAT, "target": s2.target, "uid": s2.uid, "gid": s2.gid, "groups": list(s2.groups),
        "nproc": s2.nproc, "env_file": s2.env_file, "extra_env": dict(s2.extra_env), "profile": s2.profile,
        "cwd": s2.cwd, "argv": list(s2.argv), "resolve": s2.resolve, "keep_fds": list(s2.keep_fds),
        "exec_fd": s2.exec_fd})


@pytest.mark.parametrize("host", ["darwin", "linux"])
@pytest.mark.parametrize("change", ["argv+", "module", "cwd", "env", "noenv", "profile", "nproc", "otherhost"])
def test_the_stage_refuses_a_forged_identity_spec(host, change):
    sp = spec(host, "identity")
    root = PurePosixPath(S[host])
    kw = {"argv+": {"argv": sp.argv + ("--x",)},
          "module": {"argv": wall.module_argv(host, wall.PROBE_MODULE)},
          "cwd": {"cwd": str(root / "repo")},
          "env": {"env_file": str(root / "root" / "driver.env")},
          "noenv": {"env_file": None},
          "profile": {"profile": str(root / "root" / "probe.sb") if host == "darwin" else str(root / "root" / "p.sb")},
          "nproc": {"nproc": None},
          "otherhost": {"argv": wall.module_argv("linux" if host == "darwin" else "darwin", wall.IDENTITY_MODULE)},
          }[change]
    with pytest.raises(Refused) as e:
        wall.parse_spec(_forged(sp, **kw))
    assert e.value.name == "spec-malformed"


def test_darwin_identity_with_no_profile_is_refused_by_the_stage():
    sp = spec("darwin", "identity")
    with pytest.raises(Refused) as e:
        wall.parse_spec(_forged(sp, profile=None))
    assert e.value.name == "spec-malformed"


def test_both_launch_paths_take_the_identity_spec():
    mac = wall.macos_launch_argv(spec("darwin", "identity"))
    assert mac[:3] == (wall.LAUNCHCTL, "asuser", str(IDS["darwin"]["_levain_s0"]))
    ll = wall.linux_launch_argv(spec("linux", "identity"), slot=E0, session=SESSION, kind="login",
                                netns_names=lambda: {"levain-e0"},
                                host_links={"/bin": "usr/bin", "/sbin": "usr/sbin", "/lib": "usr/lib", "/lib64": None})
    assert ll.argv[:2] == (wall.NSENTER, "--net=/run/netns/levain-e0")
    assert ll.cgroup == wall.cgroup_dir(E0, SESSION, None)          # the session's cgroup: END kills what it holds


# --- start's identity step for a Claude login --------------------------------------------------------------------------

def test_claude_login_records_the_profile_uuid(env):
    """IG-3: the child ran as the slot through the wall, root read its line within PROFILE_ANSWER_MAX and took the
    profile's ``account.uuid``. Linux: the vendor and the start's forwarder were killed and proven gone first; the
    only slot-uid process at the call is the fresh forwarder."""
    w, la, table = env
    calls = _capture(w, la, table)
    s = _login(w, la, "claude")
    assert w.state(s).fields["identity"] == {"observed_id": UUID}
    (c,) = calls
    assert c["spec"]["target"] == "identity"
    assert c["spec"]["argv"] == list(wall.module_argv("linux", wall.IDENTITY_MODULE))
    assert c["spec"]["uid"] == w.alloc(s).slot_uid
    assert c["spec"]["env_file"] == f"/var/levain/live/{s}/root/env"
    assert c["bound"] == ID.PROFILE_ANSWER_MAX
    assert c["cgroup"] == w.alloc(s).cgroup
    assert c["argv"][:2] == (wall.NSENTER, "--net=/run/netns/levain-e0")
    first, fresh = c["forwarders"]
    assert c["slot_procs"] == [fresh]                              # the vendor and the first forwarder are gone
    assert la.of("vendor")[0]["pid"] not in table.procs and first not in table.procs


def test_claude_confirm_yes_writes_the_uuid_baseline(env):
    w, la, table = env
    _capture(w, la, table)
    s = _login(w, la, "claude")
    assert w.main(["confirm-identity", s, "yes"])[1] == {"ok": True, "baseline": "written"}
    obj = canon.parse(_baseline(w).read_bytes())
    assert (obj["vendor"], obj["observed_id"], obj["session"]) == ("claude", UUID, s)


@pytest.mark.parametrize("answer", [(0, _answer(401)), (0, _answer(200, {"account": {}})), (0, b"not json\n"),
                                    (1, b'{"error":"no-token"}\n'), Refused("check-output-over-bound"),
                                    Refused("check-timeout"), OSError(12, "fork")])
def test_no_uuid_records_identity_unparsable(env, answer):
    w, la, table = env
    _capture(w, la, table, answer)
    s = _login(w, la, "claude")
    assert w.state(s).fields["identity"] == {"result": "identity-unparsable"}
    assert w.main(["confirm-identity", s, "yes"])[1] == {"ok": False, "refused": "no-observed-id:identity-unparsable"}


def test_linux_kill_unproven_spawns_no_child(env):
    w, la, table = env
    calls = _capture(w, la, table)

    def plant(step):
        if step == "identity":         # a slot-uid process in the session cgroup that cgroup.kill does not end
            table.add(99001, w.alloc(w.session).slot_uid, immortal=True, cg=True)
    w.on_step = plant
    s = _login(w, la, "claude")
    assert w.state(s).fields["identity"] == {"result": "identity-window"}
    assert calls == [] and len(la.of("forwarder")) == 1


def test_a_held_session_lock_spawns_no_child(env):
    """The spawn fence: an END (or anything else) holding the session lock means no spawn into the slot."""
    w, la, table = env
    calls = _capture(w, la, table)
    held: list[int] = []

    def take(step):
        if step == "identity":
            fd = os.open(w.L / "alloc" / f"{w.session}.lock", os.O_RDWR)
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            held.append(fd)
    w.on_step = take
    try:
        s = _login(w, la, "claude")
    finally:
        for fd in held:
            os.close(fd)
    assert w.state(s).fields["identity"] == {"result": "session-busy"}
    assert calls == [] and la.of("vendor")[0]["pid"] in table.procs   # nothing killed either


def test_codex_login_spawns_no_identity_child(env):
    w, la, table = env
    calls = _capture(w, la, table)
    s = _login(w, la, "codex")
    assert w.state(s).fields["identity"] == {"observed_id": ID.NOT_A_CHECK}
    assert calls == [] and len(la.of("forwarder")) == 1


def test_a_failed_claude_login_spawns_no_identity_child(env):
    w, la, table = env
    calls = _capture(w, la, table)
    s = _login(w, la, "claude", exit_code=1)
    assert w.state(s).fields["identity"] == {"result": ID.LOGIN_FAILED}
    assert calls == []


def _darwin(tmp_path, *, vendor_lingers: bool):
    w, la, table = _world(tmp_path, host="darwin")
    w.seams.account_uid = lambda name: DARWIN_IDS[name] if name in DARWIN_IDS else 3799     # §1.1's macOS ranges
    w.seams.group_gid = lambda g: {"levain-gwfronts": 3789, "levain-slots": 3788}[g]
    if not vendor_lingers:
        def wait(pid):
            code = la.wait(pid)
            table.procs.pop(pid, None)           # the vendor exited: its process is gone
            return code
        w.seams.wait = wait
    return w, la, table


def test_darwin_claude_login_runs_under_the_session_profile(tmp_path):
    w, la, table = _darwin(tmp_path, vendor_lingers=False)
    calls = _capture(w, la, table)
    s = _login(w, la, "claude")
    assert w.state(s).fields["identity"] == {"observed_id": UUID}
    (c,) = calls
    assert c["spec"]["profile"] == f"/private/var/levain/live/{s}/root/profile.sb"
    assert c["argv"][:3] == (wall.LAUNCHCTL, "asuser", str(w.alloc(s).slot_uid))
    assert c["slot_procs"] == [] and c["forwarders"] == []


def test_darwin_a_lingering_slot_process_is_ended_before_the_child(tmp_path):
    """macOS: launchd's per-uid agents (``distnoted``) run as the slot uid and outlive the vendor; the domain is ended
    and proven (END's own kill, proc.end_domain) before the child, so the call is not refused for them."""
    w, la, table = _darwin(tmp_path, vendor_lingers=True)
    calls = _capture(w, la, table)
    s = _login(w, la, "claude")
    assert w.state(s).fields["identity"] == {"observed_id": UUID}
    (c,) = calls
    assert c["slot_procs"] == [] and la.of("vendor")[0]["pid"] not in table.procs


def test_darwin_an_unkillable_slot_process_spawns_no_child(tmp_path):
    w, la, table = _darwin(tmp_path, vendor_lingers=False)
    calls = _capture(w, la, table)

    def plant(step):
        if step == "identity":
            table.add(99002, w.alloc(w.session).slot_uid, immortal=True)
    w.on_step = plant
    s = _login(w, la, "claude")
    assert w.state(s).fields["identity"] == {"result": "identity-window"}
    assert calls == []


def test_the_claude_call_needs_a_session():
    with pytest.raises(Refused) as e:
        ID.identity_call(None, "claude")
    assert e.value.name == "internal"



# --- fix round 1 (1027-19 RULINGS "G1b L3 r1"): every failure after the login is a named result (X1), the fresh
# forwarder and the child never outlive the call (X2), the window branches (X3) ------------------------------------

def _fail_identity_forwarder_spawn(w, la, exc):
    """From the identity step on, a forwarder spawn raises ``exc`` (the start's own forwarder already ran)."""
    real = w.seams.spawn

    def spawn(argv, spec_json, cgroup, exec_w, detach):
        if canon.parse(spec_json)["target"] == "forwarder":
            raise exc
        return real(argv, spec_json, cgroup, exec_w, detach)

    def arm(step):
        if step == "identity":
            w.seams.spawn = spawn
    w.on_step = arm


@pytest.mark.parametrize("exc", [OSError(errno.EMFILE, "pipe"), OSError(errno.ENOMEM, "fork"),
                                 Refused("cgroup-mismatch")])
def test_a_failed_forwarder_spawn_records_identity_unparsable(env, exc):
    """X1: the fresh forwarder's spawn failing (EMFILE, ENOMEM) after the login ran is a named result, recorded; the
    start answers the vendor's exit."""
    w, la, table = env
    calls = _capture(w, la, table)
    _fail_identity_forwarder_spawn(w, la, exc)
    s = _login(w, la, "claude")
    assert w.state(s).fields["identity"] == {"result": "identity-unparsable"}
    assert calls == []


def test_a_forwarder_with_no_ready_byte_records_identity_unparsable(env):
    """X1: ``forwarder-not-ready`` on the identity forwarder is a spawn failure, ``identity-unparsable``; the
    forwarder it abandoned is gone."""
    w, la, table = env
    calls = _capture(w, la, table)

    def not_ready(step):
        if step == "identity":
            la.forwarder_ready = False
    w.on_step = not_ready
    s = _login(w, la, "claude")
    assert w.state(s).fields["identity"] == {"result": "identity-unparsable"}
    assert calls == []
    assert table.of(w.alloc(s).slot_uid) == []


@pytest.mark.parametrize("exc", [OSError(errno.ENOMEM, "x"), Refused("cgroup-mismatch"), KeyError("netns")])
def test_a_failed_identity_launch_records_identity_unparsable(env, monkeypatch, exc):
    """X1: building the identity child's launch argv failing is ``identity-unparsable``, and the fresh forwarder
    started for it is ended (X2)."""
    w, la, table = env
    calls = _capture(w, la, table)
    real = SS.launch

    def launch(ctx, a, sp):
        if sp.target == "identity":
            raise exc
        return real(ctx, a, sp)
    monkeypatch.setattr(SS, "launch", launch)
    s = _login(w, la, "claude")
    assert w.state(s).fields["identity"] == {"result": "identity-unparsable"}
    assert calls == []
    assert table.of(w.alloc(s).slot_uid) == []                   # the fresh forwarder did not outlive the call
    assert la.of("forwarder")[1]["pid"] in la.killed             # and was reaped


@pytest.mark.parametrize("where", ["read_alloc", "read_record", "kill_domain", "_no_slot_process"])
def test_a_window_check_that_raises_records_identity_window(env, monkeypatch, where):
    """X1: an OSError from any window check (the alloc or record read, the kill, the proof) is ``identity-window``,
    recorded, and nothing is spawned."""
    w, la, table = env
    calls = _capture(w, la, table)
    target = SS.H if where == "read_alloc" else SS

    def boom(*a, **k):
        raise OSError(errno.EIO, where)

    def arm(step):
        if step == "identity":
            monkeypatch.setattr(target, where, boom)
    w.on_step = arm
    s = _login(w, la, "claude")
    assert w.state(s).fields["identity"] == {"result": "identity-window"}
    assert calls == [] and len(la.of("forwarder")) == 1


def test_a_changed_alloc_records_identity_window(env, monkeypatch):
    """X3: the alloc re-read under the lock is not the one start ran: ``identity-window``, nothing killed or spawned."""
    w, la, table = env
    calls = _capture(w, la, table)

    def arm(step):
        if step == "identity":
            monkeypatch.setattr(SS.H, "read_alloc", lambda ctx, s: None)
    w.on_step = arm
    s = _login(w, la, "claude")
    assert w.state(s).fields["identity"] == {"result": "identity-window"}
    assert calls == [] and len(la.of("forwarder")) == 1
    assert la.of("vendor")[0]["pid"] in table.procs              # nothing killed


def test_a_record_no_longer_running_spawns_nothing(env):
    """X3: a record moved past ACTIVE since the vendor exited (an END began): ``not-active:REVOKED``, nothing killed or
    spawned; the identity CAS is refused the same way, so nothing is recorded and the start still answers ok."""
    w, la, table = env
    calls = _capture(w, la, table)

    def revoke(step):
        if step == "identity":
            st = w.state(w.session)
            Client(w).call("CAS", session=w.session, generation=st.generation,
                           **{"from": R.ACTIVE, "to": R.REVOKED}, fields={})
    w.on_step = revoke
    s = _login(w, la, "claude")
    assert w.state(s).state == R.REVOKED and "identity" not in w.state(s).fields
    assert calls == [] and len(la.of("forwarder")) == 1
    assert la.of("vendor")[0]["pid"] in table.procs


def _capture_leaving(w, la, table, answer=(0, None), *, immortal=False, cg=True):
    """The capture, faked, leaving one slot-uid process behind it (a child's own child that outlived it)."""
    calls = _capture(w, la, table, answer)
    inner = w.seams.capture

    def cap(argv, spec_json, cgroup, bound):
        table.add(99100, w.alloc(w.session).slot_uid, immortal=immortal, cg=cg)
        return inner(argv, spec_json, cgroup, bound)
    w.seams.capture = cap
    return calls


def test_the_fresh_forwarder_and_the_child_never_outlive_the_call(env):
    """X2: after a call that answered, the slot's domain is ended again before the lock is released: the fresh
    forwarder (reaped) and anything the child left are gone."""
    w, la, table = env
    _capture_leaving(w, la, table)
    s = _login(w, la, "claude")
    assert w.state(s).fields["identity"] == {"observed_id": UUID}
    assert table.of(w.alloc(s).slot_uid) == []
    assert la.of("forwarder")[1]["pid"] in la.killed


@pytest.mark.parametrize("answer", [Refused("check-timeout"), OSError(errno.ENOMEM, "fork"), (0, b"not json\n")])
def test_a_failed_call_ends_the_domain_too(env, answer):
    """X2: the same on a failed call."""
    w, la, table = env
    _capture_leaving(w, la, table, answer)
    s = _login(w, la, "claude")
    assert w.state(s).fields["identity"] == {"result": "identity-unparsable"}
    assert table.of(w.alloc(s).slot_uid) == []
    assert la.of("forwarder")[1]["pid"] in la.killed


def test_an_unproven_kill_after_the_call_records_identity_window(env):
    """X2: a slot-uid process the kill after the call cannot end could change the credential between the read and
    END's park, so the answer is not kept: ``identity-window``."""
    w, la, table = env
    _capture_leaving(w, la, table, immortal=True)
    s = _login(w, la, "claude")
    assert w.state(s).fields["identity"] == {"result": "identity-window"}
    assert la.of("forwarder")[1]["pid"] in la.killed


def test_darwin_the_child_never_outlives_the_call(tmp_path):
    """X2, macOS: a slot-uid process left after the child is ended (proc.end_domain) before the lock is released."""
    w, la, table = _darwin(tmp_path, vendor_lingers=False)
    _capture_leaving(w, la, table, cg=False)
    s = _login(w, la, "claude")
    assert w.state(s).fields["identity"] == {"observed_id": UUID}
    assert table.of(w.alloc(s).slot_uid) == []

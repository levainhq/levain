"""M2: `levain setup-isolation` plans, the hands keys in confinement.json, and the doctor check.

Every test here is pure or runs harmless commands (`true`, `false`, `visudo -cf` on a temp file); none
needs root and none starts a sandbox. The real setup/undo runs on Linux and macOS CI runners
(tests/ci/isolation_e2e.sh) and was measured on macOS in a VM (2026-10-07).
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from levain.firing import hands
from levain.firing.confinement import ConfinementError, load_confinement_config
from levain.firing.hands import (
    HANDS_MARKER,
    HANDS_USER_RE,
    HandsSetupError,
    Step,
    choose_id,
    hands_user_name,
    hands_workspace,
    plan_setup,
    plan_undo,
    run_plan,
    sudoers_text,
)


def _entity(tmp_path: Path, name: str = "coyote") -> Path:
    d = tmp_path / name
    (d / ".levain").mkdir(parents=True)
    return d


def _setup(tmp_path: Path, host: str = "darwin", **kw):
    kw.setdefault("hands_id", 499 if host == "darwin" else 999)
    if host == "linux":
        kw.setdefault("net_gid", 998)
    return plan_setup(_entity(tmp_path), operator="alice", host=host, **kw)


def _undo(tmp_path: Path, host: str = "darwin", **kw):
    ed = _entity(tmp_path)
    kw.setdefault("hands_user", hands_user_name(ed))
    kw.setdefault("hands_id", 499)
    return plan_undo(ed, operator="alice", host=host, operator_gid=20, **kw)


def _argvs(plan) -> list[tuple[str, ...]]:
    return [s.argv for s in plan.steps if s.argv]


def _joined(plan) -> str:
    return "\n".join(" ".join(a) for a in _argvs(plan))


# --- names and ids ---------------------------------------------------------------------------------


def test_hands_user_name_is_valid_deterministic_and_per_path(tmp_path: Path) -> None:
    a, b = _entity(tmp_path / "x"), _entity(tmp_path / "y")  # same dir name, different paths
    na, nb = hands_user_name(a), hands_user_name(b)
    assert HANDS_USER_RE.match(na) and HANDS_USER_RE.match(nb)
    assert na == hands_user_name(a) and na != nb
    assert len(na) <= 31  # macOS short names; Linux allows 32


def test_hands_user_name_survives_a_name_with_no_usable_characters(tmp_path: Path) -> None:
    assert hands_user_name(_entity(tmp_path, "ÉÉ--")).startswith("_levain_entity_")


@pytest.mark.parametrize("host,top", [("darwin", 499), ("linux", 999)])
def test_ids_come_from_the_top_of_the_range_skipping_used_and_retired(host, top) -> None:
    assert choose_id(host, set(), set()) == top
    assert choose_id(host, {top}, {top - 1}) == top - 2


def test_darwin_ids_never_reach_the_range_macos_claims_for_daemons() -> None:
    # macOS 15 took 301-304 and deletes colliding accounts on upgrade (NixOS/nix#10892).
    with pytest.raises(HandsSetupError, match="no free"):
        choose_id("darwin", set(range(400, 500)), set())


# --- the setup plan --------------------------------------------------------------------------------


def test_darwin_plan_makes_a_hidden_disabled_passwordless_user_outside_staff(tmp_path: Path) -> None:
    plan = _setup(tmp_path, hands_id=480)
    u = f"/Users/{plan.hands_user}"
    argvs = _argvs(plan)
    for attr, value in [("UniqueID", "480"), ("PrimaryGroupID", "480"), ("UserShell", "/usr/bin/false"),
                        ("Password", "*"), ("IsHidden", "1"), ("RealName", HANDS_MARKER),
                        ("AuthenticationAuthority", ";DisabledUser;")]:
        assert ("/usr/bin/dscl", ".", "-create", u, attr, value) in argvs, attr
    assert not any("dseditgroup" in a[0] for a in argvs)          # nobody is put in a group
    assert " staff" not in _joined(plan) and " admin" not in _joined(plan)


def test_linux_plan_makes_a_system_user_with_explicit_ids_and_no_login(tmp_path: Path) -> None:
    plan = _setup(tmp_path, host="linux", hands_id=950)
    useradd = next(a for a in _argvs(plan) if a[0].endswith("useradd"))
    assert "--system" in useradd
    assert useradd[useradd.index("--uid") + 1] == "950" and useradd[useradd.index("--gid") + 1] == "950"
    assert useradd[useradd.index("--shell") + 1] in ("/usr/sbin/nologin", "/sbin/nologin", "/bin/false")
    assert useradd[useradd.index("--comment") + 1] == HANDS_MARKER
    groupadd = next(a for a in _argvs(plan) if a[0].endswith("groupadd"))
    assert groupadd[groupadd.index("--gid") + 1] == "950"
    assert not any(a[0].endswith(("usermod", "gpasswd")) for a in _argvs(plan))


@pytest.mark.parametrize("host", ["darwin", "linux"])
def test_the_workspace_is_outside_home_owned_by_the_hands_user_and_never_entity_workspace(tmp_path: Path, host) -> None:
    # Ruling A (Phill, 2026-10-07): the workspace and everything in it belong to the hands user.
    plan = _setup(tmp_path, host=host)
    assert plan.workspace == hands_workspace(host, plan.hands_user)
    assert plan.workspace.is_relative_to(hands.WORKSPACE_ROOT[host])
    assert str(plan.entity_dir / "workspace") not in _joined(plan)
    chowns = [a for a in _argvs(plan) if a[0].endswith("chown") and str(plan.workspace) in a[-1]]
    assert chowns == [(chowns[0][0], f"{plan.hands_user}:{plan.hands_id}", str(plan.workspace))]
    # the entity's directory above it stays root's: the hands user cannot swap the workspace out
    assert not any(a[0].endswith("chown") and a[-1] == str(plan.workspace.parent) for a in _argvs(plan))
    assert "alice:20" not in _joined(plan) and not any(a[0].endswith("chown") and "alice" in a[1] for a in _argvs(plan))
    mkdirs = {a[-1]: a[a.index("-m") + 1] for a in _argvs(plan) if a[0].endswith("mkdir")
              and a[-1] in (str(plan.workspace), str(plan.workspace.parent))}
    assert mkdirs == {str(plan.workspace.parent): "755", str(plan.workspace): "700"}


@pytest.mark.parametrize("host", ["darwin", "linux"])
def test_the_operators_acl_on_the_workspace_reads_and_never_writes(tmp_path: Path, host) -> None:
    plan = _setup(tmp_path, host=host)
    acls = [a for a in _argvs(plan) if "chmod" in a[0] and "+a" in a or "setfacl" in a[0]]
    assert acls and all(a[-1] == str(plan.workspace) for a in acls)
    text = "\n".join(" ".join(a[:-1]) for a in acls)                       # the specs, not the path
    assert plan.hands_user not in text and f"u:{plan.hands_id}" not in text   # the owner needs no entry
    if host == "darwin":
        (spec,) = [a[a.index("+a") + 1] for a in acls]
        who, _, perms = spec.partition(" allow ")
        assert who == "user:alice"
        assert set(perms.split(",")) == {"list", "search", "readattr", "readextattr", "readsecurity", "read",
                                         "file_inherit", "directory_inherit"}
    else:
        assert [a[-2] for a in acls] == ["u:alice:rX", "u:alice:rX"] and any("-d" in a for a in acls)
    for word in ("write", "add_file", "add_subdirectory", "delete", "append", "rw"):
        assert word not in text, word


@pytest.mark.parametrize("host", ["darwin", "linux"])
def test_setup_writes_no_safe_directory_on_either_side(tmp_path: Path, host) -> None:
    # Phill 2026-10-07, "go with H": every workspace repository belongs to the hands user, so neither
    # side needs an exception, and the operator's git refuses them by its own ownership check.
    assert "safe.directory" not in _joined(_setup(tmp_path, host=host))


@pytest.mark.parametrize("host", ["darwin", "linux"])
def test_the_sudoers_step_is_validated_and_scoped_to_the_hands_user(tmp_path: Path, host) -> None:
    plan = _setup(tmp_path, host=host)
    (step,) = [s for s in plan.steps if s.write is not None and "sudoers" in str(s.write[0])]
    path, content, mode = step.write
    assert path == Path("/etc/sudoers.d") / f"levain-{plan.hands_user}" and "." not in path.name
    assert mode == 0o440 and step.validate[-1] == "-cf" and step.validate[0].endswith("visudo")
    rules = [ln for ln in content.splitlines() if ln and not ln.startswith("#")]
    h = plan.hands_user
    # The I/O options pinned off (S2 L2b L3): a log_input pipe or a pty on stdin would replace the
    # socket levain reads the shell's state back over.
    # Linux (D, Phill 2026-10-09): the runas group is the net group, the one gid the egress boundary lets out.
    runas = f"{h} : {h}_net" if host == "linux" else h
    assert rules == [f"Defaults>{h} !requiretty", f"Defaults>{h} env_reset",
                     f"Defaults>{h} !log_input, !log_output, !use_pty", f"alice ALL=({runas}) NOPASSWD: ALL"]


@pytest.mark.skipif(shutil.which("visudo") is None, reason="needs visudo")
def test_the_sudoers_text_parses(tmp_path: Path) -> None:
    import subprocess

    f = tmp_path / "rule"
    for net in (None, "_levain_coyote_abcdef_net"):
        f.write_text(sudoers_text("alice", "_levain_coyote_abcdef", net))
        r = subprocess.run(["visudo", "-cf", str(f)], capture_output=True, text=True)
        assert r.returncode == 0, r.stdout + r.stderr


@pytest.mark.parametrize("bad", ["a,b", "a b", "a:b", "a\\b", "-x", "1abc", "x" * 40])
def test_setup_refuses_an_operator_name_that_could_change_a_sudoers_rule(tmp_path: Path, bad) -> None:
    with pytest.raises(HandsSetupError, match="refusing the account name"):
        plan_setup(_entity(tmp_path), operator=bad, host="linux", hands_id=999)


def test_remote_login_is_refused_only_through_a_checked_sshd_drop_in(tmp_path: Path) -> None:
    assert not [s for s in _setup(tmp_path / "a").steps if s.write and "sshd" in str(s.write[0])]
    plan = _setup(tmp_path / "b", sshd_dropins=True)
    (step,) = [s for s in plan.steps if s.write and "sshd" in str(s.write[0])]
    assert step.write[0].name == f"levain-{plan.hands_user}.conf"
    assert f"DenyUsers {plan.hands_user}" in step.write[1]
    assert step.call is not None


def test_the_sshd_check_removes_a_drop_in_that_breaks_sshd_and_keeps_one_that_does_not(tmp_path: Path, monkeypatch) -> None:
    dropin = tmp_path / "levain-x.conf"
    dropin.write_text("DenyUsers x\n")
    results = iter([(False, "bad"), (True, "")])           # fails with it, passes without it
    monkeypatch.setattr(hands, "_run_ok", lambda argv, **kw: next(results))
    assert hands._sshd_config_check(dropin) == (False, "bad") and not dropin.exists()
    dropin.write_text("DenyUsers x\n")
    results = iter([(False, "no hostkeys"), (False, "no hostkeys")])  # fails either way
    monkeypatch.setattr(hands, "_run_ok", lambda argv, **kw: next(results))
    ok, why = hands._sshd_config_check(dropin)
    assert ok and "without it too" in why and dropin.read_text() == "DenyUsers x\n"


def test_only_existing_cron_and_at_deny_lists_are_extended(tmp_path: Path) -> None:
    deny = tmp_path / "cron.deny"
    deny.write_text("daemon\n")
    plan = _setup(tmp_path, deny_lists=(deny,))
    (step,) = [s for s in plan.steps if "scheduled jobs" in s.why]
    assert step.call() == (True, "")
    assert deny.read_text() == f"daemon\n{plan.hands_user}\n"
    assert step.call() == (True, "already so")


@pytest.mark.parametrize("host", ["darwin", "linux"])
def test_setup_generates_the_entity_key_as_the_hands_user(tmp_path: Path, host) -> None:
    plan = _setup(tmp_path, host=host)
    keygen = next(s for s in plan.steps if any(x.endswith("ssh-keygen") for x in s.argv))
    argv = keygen.argv
    assert argv[:3] == ("/usr/bin/sudo", "-u", plan.hands_user) and "-i" in argv  # owned by the hands
    assert argv[argv.index("-t") + 1] == "ed25519" and argv[argv.index("-N") + 1] == ""
    key = argv[argv.index("-f") + 1]
    assert key.endswith("/.ssh/id_ed25519") and keygen.skip_if == ("/bin/test", "-e", key)


def test_the_operators_git_identity_is_copied_to_the_hands_user(tmp_path: Path) -> None:
    ident = {"user.name": "Alice A", "user.email": "a@example.invalid"}
    plan = _setup(tmp_path / "a", host="linux", git_identity=ident)
    sets = {a[-2]: a[-1] for a in _argvs(plan) if a[-2] in ident}
    assert sets == ident
    assert not [a for a in _argvs(_setup(tmp_path / "b", host="linux")) if "user.name" in a]


def test_every_root_command_is_an_absolute_path_or_resolved_on_the_secure_path(tmp_path: Path) -> None:
    for host in ("darwin", "linux"):
        for a in _argvs(_setup(tmp_path / host, host=host)):
            first = a[0]
            on_this_host = shutil.which(Path(first).name, path=hands.SECURE_PATH)
            assert first.startswith("/") or on_this_host is None, a  # bare only when absent here


# --- the undo plan ---------------------------------------------------------------------------------


@pytest.mark.parametrize("bad", ["root", "alice", "_levain_", "_levain_x_abcdeg", "_levain_X_abcdef"])
def test_undo_refuses_a_name_setup_could_not_have_written(tmp_path: Path, bad) -> None:
    with pytest.raises(HandsSetupError, match="refusing"):
        _undo(tmp_path, hands_user=bad)


@pytest.mark.parametrize("host", ["darwin", "linux"])
def test_undo_order_rule_retire_jobs_kill_then_files_then_account(tmp_path: Path, host) -> None:
    plan = _undo(tmp_path, host=host)
    why = [s.why for s in plan.steps]
    first = lambda text: next(i for i, w in enumerate(why) if text in w)  # noqa: E731
    assert first("sudoers") == 0
    order = ["sudoers", "retire the user id", "cron jobs", "at jobs", "stop every process", "your group (its owner stays",
             "remove the workspace ACLs", "let your group read", "keep the account as a tombstone"]
    assert [first(t) for t in order] == sorted(first(t) for t in order)


@pytest.mark.parametrize("host", ["darwin", "linux"])
def test_undo_keeps_the_account_as_a_disabled_tombstone_and_never_deletes_it(tmp_path: Path, host) -> None:
    # Head ruling (a), 2026-10-07: the id stays reserved where the OS allocator looks.
    plan = _undo(tmp_path, host=host)
    joined = _joined(plan)
    assert "-delete /Users" not in joined and "-delete /Groups" not in joined
    assert "userdel" not in joined and "groupdel" not in joined
    h = plan.hands_user
    if host == "darwin":
        for attr in (("UserShell", "/usr/bin/false"), ("Password", "*"), ("AuthenticationAuthority", ";DisabledUser;"),
                     ("IsHidden", "1"), ("RealName", hands.RETIRED_MARKER["darwin"])):
            assert f"-create /Users/{h} {attr[0]} {attr[1]}" in joined, attr
        assert any(s.call is not None and "every group" in s.why for s in plan.steps)
    else:
        (usermod,) = [a for a in _argvs(plan) if a[0].endswith("usermod")]
        assert "--lock" in usermod and usermod[usermod.index("--groups") + 1] == ""
        assert usermod[usermod.index("--shell") + 1].endswith(("nologin", "false"))
        assert any(a[0].endswith("chage") and a[1:3] == ("--expiredate", "0") for a in _argvs(plan))
    assert not any(s.allow_fail for s in plan.steps if "tombstone" in s.why)


@pytest.mark.parametrize("host", ["darwin", "linux"])
def test_undo_of_a_deleted_account_makes_the_tombstone_again(tmp_path: Path, host) -> None:
    plan = _undo(tmp_path, host=host, account_gone=True)
    joined = _joined(plan)
    if host == "darwin":
        assert f"-create /Users/{plan.hands_user} UniqueID 499" in joined
    else:
        assert "useradd --system --uid 499" in joined and hands.RETIRED_MARKER["linux"] in joined


def _setup_dry(tmp_path: Path, monkeypatch, capsys, *, retired: bool, reenable: bool, host: str = "linux"):
    ed = _entity(tmp_path)
    monkeypatch.setattr(hands, "host_os", lambda: host)
    monkeypatch.setattr(hands, "_user_exists", lambda n: n == hands_user_name(ed) and retired is not None)
    monkeypatch.setattr(hands, "user_record_is_ours", lambda n, h: True)
    monkeypatch.setattr(hands, "user_is_retired", lambda n, h: bool(retired))
    real = hands.pwd.getpwnam
    monkeypatch.setattr(hands.pwd, "getpwnam", lambda n: type("E", (), {"pw_uid": 450})() if n == hands_user_name(ed) else real(n))
    monkeypatch.setattr(hands, "shared_root_problem", lambda h: None)
    monkeypatch.setattr(hands, "operator_git_identity", lambda op: {})
    monkeypatch.setattr(hands, "used_ids", lambda h: set())
    monkeypatch.setattr(hands, "retired_ids", lambda h: set())
    rc = hands.cmd_setup_isolation(ed, undo=False, dry_run=True, reenable=reenable)
    return rc, capsys.readouterr().out


@pytest.mark.parametrize("host", ["darwin", "linux"])
def test_setup_refuses_a_retired_tombstone_unless_told_to_reenable_it_with_its_own_id(tmp_path: Path, monkeypatch, capsys, host) -> None:
    rc, out = _setup_dry(tmp_path, monkeypatch, capsys, retired=True, reenable=False, host=host)
    assert rc == 1 and "--reenable" in out
    rc, out = _setup_dry(tmp_path / "b", monkeypatch, capsys, retired=True, reenable=True, host=host)
    assert rc == 0 and "(id 450)" in out
    if host == "linux":
        assert "useradd" not in out and "bring the retired hands user back" in out and "--expiredate -1" in out


def test_reenable_with_nothing_retired_refuses(tmp_path: Path, monkeypatch, capsys) -> None:
    rc, out = _setup_dry(tmp_path, monkeypatch, capsys, retired=None, reenable=True)
    assert rc == 1 and "no retired hands user" in out


def test_undo_without_a_verified_id_kills_nothing_and_reowns_only_ownerless_files(tmp_path: Path) -> None:
    plan = _undo(tmp_path, hands_id=None)
    why = " ".join(s.why for s in plan.steps)
    assert "stop every process" not in why and "retire" not in why
    assert hands._owned_selector(None)[-1] == "-nouser"


# --- step bodies -----------------------------------------------------------------------------------


def test_ensure_line_adds_once_removes_and_keeps_other_lines(tmp_path: Path) -> None:
    f = tmp_path / "deny"
    f.write_text("a\nb\n")
    assert hands._ensure_line(f, "h", present=True) == (True, "")
    assert hands._ensure_line(f, "h", present=True) == (True, "already so")
    assert f.read_text() == "a\nb\nh\n"
    assert hands._ensure_line(f, "h", present=False) == (True, "")
    assert f.read_text() == "a\nb\n"
    missing = tmp_path / "nope"
    assert hands._ensure_line(missing, "h", present=True)[0] and not missing.exists()  # never created...
    assert hands._ensure_line(missing, "7", present=True, create=True)[0] and missing.read_text() == "7\n"  # ...unless asked


def test_remove_if_empty_keeps_a_workspace_with_files(tmp_path: Path) -> None:
    ws = tmp_path / "h" / "workspace"
    ws.mkdir(parents=True)
    (ws / "f").write_text("x")
    ok, why = hands._remove_if_empty(ws)
    assert ok and "kept" in why and ws.exists()
    (ws / "f").unlink()
    assert hands._remove_if_empty(ws) == (True, "") and not ws.parent.exists()


def _plan(*steps: Step):
    return hands.Plan("linux", "alice", "_levain_x_abcdef", 999, Path("/e"), Path("/e/w"), steps)


def test_dry_run_executes_nothing(tmp_path: Path) -> None:
    marker = tmp_path / "ran"
    called = []
    plan = _plan(Step("touch", ("touch", str(marker))), Step("call", call=lambda: called.append(1) or (True, "")))
    assert run_plan(plan, dry_run=True, emit=lambda _: None) == 0
    assert not marker.exists() and not called


def test_run_plan_stops_at_the_first_failure(tmp_path: Path) -> None:
    marker = tmp_path / "ran"
    plan = _plan(Step("fail", call=lambda: (False, "no")), Step("touch", ("touch", str(marker))))
    assert run_plan(plan, dry_run=False, emit=lambda _: None) == 1
    assert not marker.exists()


def test_run_plan_continues_past_an_allowed_failure_and_honours_skip_if(tmp_path: Path) -> None:
    marker, skipped = tmp_path / "ran", tmp_path / "skipped"
    plan = _plan(
        Step("gone already", ("false",), allow_fail=True),
        Step("present already", ("touch", str(skipped)), skip_if=("true",)),
        Step("touch", ("touch", str(marker))),
    )
    assert run_plan(plan, dry_run=False, emit=lambda _: None) == 0
    assert marker.exists() and not skipped.exists()


def test_a_drop_in_that_fails_validation_is_never_installed(tmp_path: Path) -> None:
    target = tmp_path / "sudoers.d" / "levain-x"
    ok, _why = hands._install_file(target, "garbage", 0o440, validate=("false",))
    assert not ok and not target.exists()
    assert list(target.parent.iterdir()) == []  # the temp copy is gone too


# --- the record and the config keys ---------------------------------------------------------------


def _record(ed: Path) -> dict:
    return {"hands_user": hands_user_name(ed), "hands_uid": 499,
            "hands_workspace": str(hands_workspace("darwin", hands_user_name(ed)))}


def test_record_hands_sets_and_removes_the_keys_keeping_others(tmp_path: Path) -> None:
    ed = _entity(tmp_path)
    cfg = ed / ".levain" / "confinement.json"
    cfg.write_text(json.dumps({"ssh_mode": "raw"}))
    hands.record_hands(ed, _record(ed), owner_uid=os.getuid(), owner_gid=os.getgid())
    loaded = load_confinement_config(ed)
    assert (loaded.hands_user, loaded.hands_uid) == (hands_user_name(ed), 499)
    assert loaded.hands_workspace == hands_workspace("darwin", hands_user_name(ed))
    assert json.loads(cfg.read_text())["ssh_mode"] == "raw"
    hands.record_hands(ed, None, owner_uid=os.getuid(), owner_gid=os.getgid())
    assert json.loads(cfg.read_text()) == {"ssh_mode": "raw"}


def test_record_hands_refuses_a_symlinked_config(tmp_path: Path) -> None:
    ed = _entity(tmp_path)
    target = tmp_path / "elsewhere.json"
    target.write_text("{}")
    (ed / ".levain" / "confinement.json").symlink_to(target)
    with pytest.raises(HandsSetupError, match="symlink"):
        hands.record_hands(ed, _record(ed), owner_uid=os.getuid(), owner_gid=os.getgid())
    assert target.read_text() == "{}"


@pytest.mark.parametrize("mutate", [
    {"hands_workspace": "/Users/Shared/levain/x"}, {"hands_workspace": "/Users/Shared/levain"},
    {"hands_user": "root"}, {"hands_user": 5}, {"hands_user": "_levain_coyote_ABCDEF"},
    {"hands_uid": 0}, {"hands_uid": True}, {"hands_uid": "499"},
    {"hands_workspace": "relative/ws"}, {"hands_workspace": "/Users/Shared/levain/x/../../etc"},
    {"hands_workspace": "/tmp/ws"}, {"hands_workspace": None},
])
def test_the_loader_refuses_hands_values_setup_could_not_have_written(tmp_path: Path, mutate) -> None:
    ed = _entity(tmp_path)
    (ed / ".levain" / "confinement.json").write_text(json.dumps({**_record(ed), **mutate}))
    with pytest.raises(ConfinementError, match="hands_"):
        load_confinement_config(ed)


@pytest.mark.parametrize("drop", ["hands_user", "hands_uid", "hands_workspace"])
def test_the_loader_refuses_a_partial_hands_record(tmp_path: Path, drop) -> None:
    ed = _entity(tmp_path)
    rec = _record(ed)
    del rec[drop]
    (ed / ".levain" / "confinement.json").write_text(json.dumps(rec))
    with pytest.raises(ConfinementError, match="come together"):
        load_confinement_config(ed)


def test_the_loader_refuses_a_workspace_under_home(tmp_path: Path, monkeypatch) -> None:
    ed = _entity(tmp_path)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: Path("/Users/Shared/levain")))
    (ed / ".levain" / "confinement.json").write_text(json.dumps(_record(ed)))
    with pytest.raises(ConfinementError, match="hands_workspace"):
        load_confinement_config(ed)


def test_hands_keys_are_appended_last_to_the_config_dataclass() -> None:
    from dataclasses import fields

    from levain.firing.confinement import ConfinementConfig

    assert [f.name for f in fields(ConfinementConfig)][-4:] == ["hands_user", "hands_uid", "hands_workspace",
                                                                "hands_egress_ports"]


# --- preconditions ---------------------------------------------------------------------------------


def test_setup_refuses_without_root(monkeypatch) -> None:
    monkeypatch.setattr(os, "geteuid", lambda: 1000)
    with pytest.raises(HandsSetupError, match="sudo"):
        hands.invoking_operator()


@pytest.mark.parametrize("sudo_user", [None, "root"])
def test_setup_refuses_root_without_a_sudo_user(monkeypatch, sudo_user) -> None:
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    if sudo_user is None:
        monkeypatch.delenv("SUDO_USER", raising=False)
    else:
        monkeypatch.setenv("SUDO_USER", sudo_user)
    with pytest.raises(HandsSetupError, match="your own account"):
        hands.invoking_operator()


def test_setup_refuses_an_operator_name_from_sudo_that_could_change_a_rule(monkeypatch) -> None:
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setenv("SUDO_USER", "a,b")
    with pytest.raises(HandsSetupError, match="refusing the account name"):
        hands.invoking_operator()


def test_setup_refuses_an_entity_already_set_up(tmp_path: Path, monkeypatch, capsys) -> None:
    ed = _entity(tmp_path)
    (ed / ".levain" / "confinement.json").write_text(json.dumps(_record(ed)))
    monkeypatch.setattr(hands, "invoking_operator", lambda: os.environ.get("USER") or "root")
    monkeypatch.setattr(hands.pwd, "getpwnam", lambda n: hands.pwd.getpwuid(os.getuid()))
    monkeypatch.setattr(hands, "host_os", lambda: "darwin")   # on Linux this is the linger repair
    assert hands.cmd_setup_isolation(ed, undo=False, dry_run=False) == 1
    assert "already set up" in capsys.readouterr().out


# --- doctor ----------------------------------------------------------------------------------------


def test_doctor_warns_loudly_but_does_not_fail_when_not_set_up(tmp_path: Path, monkeypatch) -> None:
    from levain import doctor

    monkeypatch.setattr(hands, "host_os", lambda: "darwin")
    (r,) = doctor._check_hands_isolation(_entity(tmp_path))
    assert r.ok and r.warn and "NOT SET UP" in r.detail
    assert r.hint and r.hint.startswith("sudo levain setup-isolation --path ")


def test_doctor_fails_when_the_recorded_user_is_gone(tmp_path: Path, monkeypatch) -> None:
    from levain import doctor

    monkeypatch.setattr(hands, "host_os", lambda: "linux")
    ed = _entity(tmp_path)
    (ed / ".levain" / "confinement.json").write_text(json.dumps(_record(ed)))
    (r,) = doctor._check_hands_isolation(ed)
    assert not r.ok and "does not exist" in r.detail


def test_doctor_fails_when_the_os_took_the_recorded_id(tmp_path: Path, monkeypatch) -> None:
    from levain import doctor

    monkeypatch.setattr(hands, "host_os", lambda: "darwin")
    ed = _entity(tmp_path)
    (ed / ".levain" / "confinement.json").write_text(json.dumps(_record(ed)))
    me = hands.pwd.getpwuid(os.getuid())
    monkeypatch.setattr(doctor, "_probe", lambda cmd: (True, ""))
    import pwd as _pwd
    monkeypatch.setattr(_pwd, "getpwnam", lambda n: me)
    (r,) = doctor._check_hands_isolation(ed)
    assert not r.ok and "not the recorded 499" in r.detail


@pytest.mark.parametrize("system", ["Darwin", "Linux"])
def test_doctor_says_who_bash_runs_as(tmp_path: Path, monkeypatch, system) -> None:
    """M2 S2 slice 2: on macOS headless bash runs as the hands user (a pass); on Linux it does not
    yet, which stays a warning."""
    from levain import doctor
    from levain.firing import confinement

    monkeypatch.setattr(confinement.platform, "system", lambda: system)
    monkeypatch.setattr(hands, "host_os", lambda: "darwin")
    ed = _entity(tmp_path)
    me = hands.pwd.getpwuid(os.getuid())
    rec = {**_record(ed), "hands_uid": me.pw_uid}
    (ed / ".levain" / "confinement.json").write_text(json.dumps(rec))
    probes = []
    monkeypatch.setattr(doctor, "_probe", lambda cmd: probes.append(cmd) or (True, ""))
    from levain.firing import ws_git

    # pinned: a runner image's own safe.directory=* would add a second (correct) warning
    monkeypatch.setattr(ws_git, "wildcard_safe_directory", lambda roots=(): [])
    monkeypatch.setattr(ws_git, "foreign_entries", lambda h: [])
    monkeypatch.setattr(ws_git, "bare_repository_explicit", lambda: True)
    import pwd as _pwd
    monkeypatch.setattr(_pwd, "getpwnam", lambda n: me)
    (r,) = doctor._check_hands_isolation(ed)
    if system == "Darwin":
        assert r.ok and not r.warn and "headless chat, --task and seats run the entity's bash" in r.detail
    else:
        assert r.ok and r.warn and "still runs the entity's bash as you" in r.detail
    assert probes == [["sudo", "-n", "-u", rec["hands_user"], "/bin/test", "-w", rec["hands_workspace"]]]


def test_the_warn_badge_prints_its_hint(capsys) -> None:
    from levain import doctor

    doctor._emit(doctor.CheckResult("x", True, "detail", hint="do this", warn=True))
    out = capsys.readouterr().out
    assert "do this" in out and "[OK]" not in out


def test_undo_gives_nothing_to_the_operator_or_root_and_never_follows_a_hard_link(tmp_path: Path, monkeypatch) -> None:
    # Head ruling, 2026-10-07: nothing to root (root's git trusts a root-owned repository, so a later
    # `sudo git` would run what the entity planted). The owner stays the retired id; only the group moves.
    calls = []
    monkeypatch.setattr(hands, "_run_ok", lambda argv, **kw: calls.append(argv) or (True, ""))
    assert hands._to_operator_group(tmp_path, 499, 20) == (True, "")
    (argv,) = calls
    assert argv[argv.index("-exec") + 1].endswith("chgrp") and argv[argv.index("-exec") + 2:-2] == ("-h", "20")
    assert not any("chown" in a for a in argv) and "alice" not in " ".join(argv)
    assert ("-links", "1") == argv[argv.index("-links"):argv.index("-links") + 2]
    assert argv[argv.index("-uid") + 1] == "499"


def test_a_failing_step_body_is_a_failed_step_not_a_crash(tmp_path: Path) -> None:
    def boom():
        raise PermissionError("nope")
    assert run_plan(_plan(Step("boom", call=boom)), dry_run=False, emit=lambda _: None) == 1


def test_undo_empties_the_hooks_and_cuts_the_config_of_the_entitys_repositories(tmp_path: Path, monkeypatch) -> None:
    import subprocess as sp

    if shutil.which("git") is None:
        pytest.skip("needs git")
    monkeypatch.setattr(hands, "_run_ok", lambda argv, **kw: (True, ""))
    tree = tmp_path / "h"
    for repo in (tree / "workspace" / "r", tree / "workspace" / "bare.d"):
        repo.mkdir(parents=True)
    sp.run(["git", "init", "-q", str(tree / "workspace" / "r")], check=True)
    sp.run(["git", "init", "-q", "--bare", str(tree / "workspace" / "bare.d")], check=True)
    for g in (tree / "workspace" / "r" / ".git", tree / "workspace" / "bare.d"):
        (g / "hooks" / "pre-commit").write_text("#!/bin/sh\nexit 0\n")
        sp.run(["git", "config", "--file", str(g / "config"), "core.fsmonitor", "evil"], check=True)
        sp.run(["git", "config", "--file", str(g / "config"), "remote.origin.url", "git@h:o/r.git"], check=True)
    ok, why = hands._readable_and_sanitised(tree, os.getgid(), owner_uid=os.getuid())
    assert ok and "retired id as owner" in why
    for g in (tree / "workspace" / "r" / ".git", tree / "workspace" / "bare.d"):
        assert list((g / "hooks").iterdir()) == []
        text = (g / "config").read_text()
        assert "fsmonitor" not in text and "git@h:o/r.git" in text


@pytest.mark.parametrize("host", ["darwin", "linux"])
def test_undo_makes_the_repositories_readable_only_after_clearing_the_acls(tmp_path: Path, host) -> None:
    # On Linux a group chmod on a file with an ACL edits the mask, which clearing the ACL discards.
    why = [s.why for s in _undo(tmp_path, host=host).steps]
    assert why.index("remove the workspace ACLs") < next(i for i, w in enumerate(why) if w.startswith("let your group read"))


@pytest.mark.parametrize("version,ok", [
    ("git version 2.50.1 (Apple Git-155)", True), ("git version 2.37.1", True), ("git version 2.37.0", False),
    ("git version 2.34.4", True), ("git version 2.34.1", False), ("git version 2.29.9", False), ("garbage", False),
])
def test_the_git_version_floor_for_gitdir_ownership(version, ok) -> None:
    assert hands.git_checks_gitdir_ownership(version) is ok


def test_the_shared_root_must_be_roots_and_not_writable_by_others(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "levain"
    monkeypatch.setitem(hands.WORKSPACE_ROOT, "darwin", root)
    assert hands.shared_root_problem("darwin") is None            # absent: setup creates it as root
    root.mkdir()
    problem = hands.shared_root_problem("darwin")                 # owned by the test user, not root
    assert problem and "owned by root" in problem
    root.rmdir()
    root.symlink_to(tmp_path)
    assert "not a plain directory" in hands.shared_root_problem("darwin")


def test_record_hands_refuses_a_symlinked_store_directory(tmp_path: Path) -> None:
    ed = tmp_path / "e"
    ed.mkdir()
    real = tmp_path / "elsewhere"
    real.mkdir()
    (ed / ".levain").symlink_to(real)
    with pytest.raises(HandsSetupError, match="refusing"):
        hands.record_hands(ed, _record(ed), owner_uid=os.getuid(), owner_gid=os.getgid())
    assert list(real.iterdir()) == []


# --- L3 round 1 (e09dc54) ---------------------------------------------------------------------------


def test_the_loader_refuses_a_hands_record_copied_from_another_entity(tmp_path: Path) -> None:
    a, b = _entity(tmp_path, "a"), _entity(tmp_path, "b")
    (b / ".levain" / "confinement.json").write_text(json.dumps(_record(a)))
    with pytest.raises(ConfinementError, match="another entity directory"):
        load_confinement_config(b)
    (a / ".levain" / "confinement.json").write_text(json.dumps(_record(a)))
    assert load_confinement_config(a).hands_user == hands_user_name(a)          # control


@pytest.mark.skipif(shutil.which("git") is None, reason="needs git")
def test_undo_never_changes_the_mode_of_what_an_entity_link_points_at(tmp_path: Path) -> None:
    # undo runs this as root; chmod follows a link, so a link to /etc/shadow would have gained group read
    target = tmp_path / "secret"
    target.write_text("k")
    target.chmod(0o600)
    tree = tmp_path / "h" / "workspace"
    tree.mkdir(parents=True)
    (tree / "link").symlink_to(target)
    (tree / "own").write_text("x")
    (tree / "own").chmod(0o600)
    ok, _ = hands._readable_and_sanitised(tree.parent, os.getgid(), owner_uid=os.getuid())
    assert ok
    assert target.stat().st_mode & 0o777 == 0o600                    # untouched
    assert (tree / "own").stat().st_mode & 0o777 == 0o640              # the entity's own file: group read


def _undo_dry(tmp_path: Path, monkeypatch, capsys, uid: int) -> tuple[int, str]:
    ed = _entity(tmp_path)
    (ed / ".levain" / "confinement.json").write_text(json.dumps({**_record(ed), "hands_uid": uid}))
    monkeypatch.setattr(hands, "host_os", lambda: "darwin")
    rc = hands.cmd_setup_isolation(ed, undo=True, dry_run=True)
    return rc, capsys.readouterr().out


def test_undo_of_an_account_someone_else_deleted_works_on_its_id_only_when_the_workspace_vouches(tmp_path: Path, monkeypatch, capsys) -> None:
    rc, out = _undo_dry(tmp_path, monkeypatch, capsys, 4_000_017)    # no account, and no workspace of that id
    assert rc == 1 and "not owned by the recorded id" in out
    monkeypatch.setitem(hands.WORKSPACE_ROOT, "darwin", tmp_path / "root")
    again = tmp_path / "again"
    (tmp_path / "root" / hands_user_name(again / "coyote") / "workspace").mkdir(parents=True)  # owned by this test's uid
    monkeypatch.setenv("SUDO_USER", hands.pwd.getpwuid(os.getuid()).pw_name)   # the dry run's operator, by name
    real = hands.pwd.getpwuid
    monkeypatch.setattr(hands.pwd, "getpwuid", lambda uid: (_ for _ in ()).throw(KeyError(uid)) if uid == os.getuid() else real(uid))
    rc, out = _undo_dry(tmp_path / "again", monkeypatch, capsys, os.getuid())
    assert rc == 0 and "retire the user id" in out
    assert "stop every process" not in out                            # root kills nothing by a config-named id


def test_undo_refuses_when_the_recorded_id_now_belongs_to_another_account(tmp_path: Path, monkeypatch, capsys) -> None:
    rc, out = _undo_dry(tmp_path, monkeypatch, capsys, os.getuid())
    assert rc == 1 and "now belongs to" in out and "retire the user id" not in out


def test_undo_takes_the_hands_lock_exclusive_creating_it_for_the_operator(tmp_path: Path) -> None:
    from levain.firing import ws_git

    ed = _entity(tmp_path)
    fd = hands._undo_lock(ed, os.getuid(), os.getgid())        # absent: created, the operator's, and held
    try:
        lock = ed / ".levain" / ws_git.HANDS_LOCK
        assert fd >= 0 and lock.stat().st_uid == os.getuid()
        with pytest.raises(ws_git.WsGitError):                  # nothing starts under the undo
            ws_git.hold_session_lock(ed, wait=0)
    finally:
        os.close(fd)
    sfd = ws_git.hold_session_lock(ed)                          # a session is open: undo refuses
    try:
        assert hands._undo_lock(ed, os.getuid(), os.getgid()) == -1
    finally:
        os.close(sfd)


def test_setup_isolation_can_still_undo_an_entity_that_was_moved(tmp_path: Path, monkeypatch, capsys) -> None:
    old, moved = _entity(tmp_path, "old"), _entity(tmp_path, "moved")
    (moved / ".levain" / "confinement.json").write_text(json.dumps({**_record(old), "hands_uid": 4_000_017}))
    monkeypatch.setattr(hands, "host_os", lambda: "darwin")
    hands.cmd_setup_isolation(moved, undo=True, dry_run=True)
    out = capsys.readouterr().out
    assert "another entity directory" not in out and hands_user_name(old) in out


def test_undo_with_nothing_set_up_makes_no_tombstone(tmp_path: Path) -> None:
    for host in ("darwin", "linux"):
        joined = _joined(_undo(tmp_path / host, host=host, hands_id=None))
        assert "UserShell" not in joined and "usermod" not in joined and "useradd" not in joined


def test_the_undo_lock_is_made_only_inside_the_operators_own_store_directory(tmp_path: Path) -> None:
    ed = _entity(tmp_path)
    elsewhere = tmp_path / "privileged"
    elsewhere.mkdir()
    (ed / ".levain").rename(tmp_path / "moved-store")
    (ed / ".levain").symlink_to(elsewhere)                  # swapped for a link to somewhere else
    with pytest.raises(OSError):
        hands._undo_lock(ed, os.getuid(), os.getgid())
    assert list(elsewhere.iterdir()) == []
    (ed / ".levain").unlink()
    (ed / ".levain").mkdir()
    with pytest.raises(HandsSetupError, match="not yours"):
        hands._undo_lock(ed, os.getuid() + 1, os.getgid())


def test_undo_of_another_paths_hands_user_refuses_while_it_runs_anything(tmp_path: Path, monkeypatch, capsys) -> None:
    from levain.firing import ws_git

    old, here = _entity(tmp_path, "old"), _entity(tmp_path, "here")
    (here / ".levain" / "confinement.json").write_text(json.dumps({**_record(old), "hands_uid": 4_000_017}))
    monkeypatch.setattr(hands, "host_os", lambda: "darwin")
    monkeypatch.setattr(hands, "_user_exists", lambda n: n == hands_user_name(old))
    monkeypatch.setattr(hands.pwd, "getpwnam", lambda n: type("E", (), {"pw_uid": 4_000_017, "pw_gid": 20})()
                        if n == hands_user_name(old) else hands.pwd.getpwuid(os.getuid()))
    monkeypatch.setattr(ws_git, "entity_session_live", lambda uid, **kw: True)
    assert hands.cmd_setup_isolation(here, undo=True, dry_run=True) == 1
    assert "processes running" in capsys.readouterr().out
    monkeypatch.setattr(ws_git, "entity_session_live", lambda uid, **kw: False)
    assert hands.cmd_setup_isolation(here, undo=True, dry_run=True) == 0
    assert "a setup here makes a new hands user" in capsys.readouterr().out


def test_a_remade_macos_group_holds_its_id_and_a_group_with_another_id_is_refused(monkeypatch) -> None:
    import subprocess as sp

    made = []
    monkeypatch.setattr(hands, "_run_ok", lambda argv, **kw: made.append(argv) or (True, ""))
    for existing, expect_ok in (("PrimaryGroupID: 499\n", True), ("PrimaryGroupID: 20\n", False), (None, True)):
        made.clear()
        monkeypatch.setattr(hands.subprocess, "run", lambda argv, **kw: sp.CompletedProcess(
            argv, 0 if existing else 56, existing or "", ""))
        ok, _ = hands._darwin_group_with_id("_levain_x_abcdef", 499)
        assert ok is expect_ok, existing
        if existing is None:
            assert made[-1][-2:] == ("PrimaryGroupID", "499")
        else:
            assert made == []


def test_linger_repair_on_an_existing_linux_install_succeeds(tmp_path: Path, monkeypatch, capsys) -> None:
    """L3 r10 (codex 4, complement 2): the repair ran and worked, then setup printed "--undo first" and
    exited 1, so a script or an operator read a successful repair as a failure."""
    ed = _entity(tmp_path)
    (ed / ".levain" / "confinement.json").write_text(json.dumps(_record(ed)))
    monkeypatch.setattr(hands, "invoking_operator", lambda: os.environ.get("USER") or "root")
    me = hands.pwd.getpwuid(os.getuid())
    monkeypatch.setattr(hands.pwd, "getpwnam", lambda n: me if n != hands_user_name(ed) else
                        hands.pwd.struct_passwd((n, "*", 499, 499, "", "/nonexistent", "/bin/false")))
    monkeypatch.setattr(hands, "host_os", lambda: "linux")
    monkeypatch.setattr(hands, "egress_unavailable", lambda: None)
    monkeypatch.setattr(hands, "_group_exists", lambda n: False)
    monkeypatch.setattr(hands, "used_ids", lambda h: set(range(901, 1000)))
    monkeypatch.setattr(hands, "retired_ids", lambda h: set())
    written: list[Path] = []
    monkeypatch.setattr(hands, "_install_file", lambda path, *a, **kw: written.append(path) or (True, ""))
    ran: list[list[str]] = []
    monkeypatch.setattr(hands.subprocess, "run", lambda argv, **kw: ran.append(argv)
                        or subprocess.CompletedProcess(argv, 0, "", ""))
    assert hands.cmd_setup_isolation(ed, undo=False, dry_run=False) == 0
    out = capsys.readouterr().out
    assert any("enable-linger" in a for a in ran) and "linger is on" in out and "--undo" not in out
    # P-1 (a): the repair also loads the network boundary (an install from before it has none).
    h = hands_user_name(ed)
    assert written == [hands.egress_rules_path(h), hands.egress_unit_path(h), hands.sudoers_path(h)]
    assert any(list(a[-3:]) == ["--gid", "900", f"{h}_net"] for a in ran)   # pre-D install: the group is made
    assert any(list(a[-2:]) == ["restart", hands.egress_unit_name(hands_user_name(ed))] for a in ran)


# --- the Linux egress boundary (P-1 (a), Phill 2026-10-08) ------------------------------------------


def test_linux_setup_loads_the_boundary_before_anything_runs_as_the_hands_user(tmp_path: Path) -> None:
    plan = _setup(tmp_path, "linux", hands_id=999, egress_ports=(18080,))
    whys = [s.why for s in plan.steps]
    loaded = whys.index("check the boundary is loaded")
    first_as_hands = next(i for i, s in enumerate(plan.steps) if s.argv[:3] == ("/usr/bin/sudo", "-u", plan.hands_user))
    assert loaded < first_as_hands
    ruleset = next(s.write[1] for s in plan.steps if s.write and s.write[0] == hands.egress_rules_path(plan.hands_user))
    assert "meta skuid 999 ip daddr 127.0.0.1 tcp dport { 18080 } accept" in ruleset
    assert ruleset.rstrip().splitlines()[-3].strip() == "meta skuid 999 reject with icmpx admin-prohibited"


def test_linux_undo_lifts_the_boundary_only_after_the_hands_processes_are_stopped(tmp_path: Path) -> None:
    ed = _entity(tmp_path)
    plan = _undo(ed, "linux", hands_user=hands_user_name(ed), hands_id=999)
    whys = [s.why for s in plan.steps]
    assert whys.index("stop every process of the hands user, and check they are gone") < whys.index(
        "remove the boundary's table")


def test_linux_setup_refuses_by_name_without_nft(monkeypatch) -> None:
    monkeypatch.setattr(hands.shutil, "which", lambda name, path=None: None if name == "nft" else f"/usr/bin/{name}")
    assert hands.egress_unavailable() == (
        "nftables is not installed (no `nft` on /usr/sbin:/usr/bin:/sbin:/bin); the hands user's network boundary "
        "needs it. Install nftables, then run setup again")


def test_linux_setup_refuses_by_name_when_the_kernel_refuses_nftables(monkeypatch) -> None:
    monkeypatch.setattr(hands.shutil, "which", lambda name, path=None: f"/usr/sbin/{name}")
    monkeypatch.setattr(hands.subprocess, "run", lambda argv, **kw: subprocess.CompletedProcess(
        argv, 1, "", "Error: Operation not permitted (you must be root)\n"))
    problem = hands.egress_unavailable()
    assert problem is not None and problem.startswith("the kernel refused nftables (Error: Operation not permitted")

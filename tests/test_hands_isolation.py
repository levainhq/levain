"""M2: `levain setup-isolation` plans, the `hands_user` config key, and the doctor check.

Every test here is pure or runs harmless commands (`true`, `false`, `visudo -cf` on a temp file); none
needs root and none starts a sandbox. The real setup/undo runs on Linux CI (tests/ci/linux_isolation.sh)
and was measured on macOS in a VM (2026-10-07).
"""
from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest

from levain.firing import hands
from levain.firing.confinement import ConfinementError, load_confinement_config
from levain.firing.hands import (
    HANDS_MARKER,
    HANDS_USER_RE,
    HandsSetupError,
    Step,
    hands_user_name,
    plan_setup,
    plan_undo,
    run_plan,
    sudoers_text,
)


def _entity(tmp_path: Path, name: str = "coyote") -> Path:
    d = tmp_path / name
    (d / ".levain").mkdir(parents=True)
    return d


def _argvs(plan) -> list[tuple[str, ...]]:
    return [s.argv for s in plan.steps if s.argv]


@pytest.fixture(autouse=True)
def _operator_home(monkeypatch, tmp_path):
    # plan_setup looks up the operator's home for its git config step; tests use a fake operator.
    real = hands._home_of
    monkeypatch.setattr(hands, "_home_of", lambda user, default=None: str(tmp_path / f"home-{user}")
                        if user in ("alice",) else real(user, default))


# --- the name -------------------------------------------------------------------------------------


def test_hands_user_name_is_valid_deterministic_and_per_path(tmp_path: Path) -> None:
    a, b = _entity(tmp_path / "x"), _entity(tmp_path / "y")  # same dir name, different paths
    na, nb = hands_user_name(a), hands_user_name(b)
    assert HANDS_USER_RE.match(na) and HANDS_USER_RE.match(nb)
    assert na == hands_user_name(a)
    assert na != nb
    assert len(na) <= 31  # macOS short names; Linux allows 32


def test_hands_user_name_survives_a_name_with_no_usable_characters(tmp_path: Path) -> None:
    assert hands_user_name(_entity(tmp_path, "ÉÉ--")).startswith("_levain_entity_")


# --- the setup plan --------------------------------------------------------------------------------


def test_darwin_plan_makes_a_hidden_passwordless_user_outside_staff(tmp_path: Path) -> None:
    ed = _entity(tmp_path)
    plan = plan_setup(ed, operator="alice", host="darwin", used_ids={300, 301})
    u = f"/Users/{plan.hands_user}"
    argvs = _argvs(plan)
    assert ("/usr/bin/dscl", ".", "-create", u, "UniqueID", "302") in argvs   # first free id
    assert ("/usr/bin/dscl", ".", "-create", u, "PrimaryGroupID", "302") in argvs  # its own group
    assert ("/usr/bin/dscl", ".", "-create", u, "UserShell", "/usr/bin/false") in argvs
    assert ("/usr/bin/dscl", ".", "-create", u, "Password", "*") in argvs
    assert ("/usr/bin/dscl", ".", "-create", u, "IsHidden", "1") in argvs
    assert ("/usr/bin/dscl", ".", "-create", u, "RealName", HANDS_MARKER) in argvs
    # never added to staff (20) or admin: those are what would reopen the operator's home
    joined = " ".join(" ".join(a) for a in argvs)
    assert " staff" not in joined and " admin" not in joined and '"20"' not in joined
    assert not any(a[:2] == ("/usr/sbin/dseditgroup", "-o") and a[-1] in ("staff", "admin") for a in argvs)


def test_darwin_plan_skips_every_used_id_in_users_and_groups(tmp_path: Path) -> None:
    plan = plan_setup(_entity(tmp_path), operator="alice", host="darwin", used_ids=set(range(300, 450)))
    assert ("/usr/bin/dscl", ".", "-create", f"/Users/{plan.hands_user}", "UniqueID", "450") in _argvs(plan)


def test_darwin_plan_refuses_when_no_id_is_free(tmp_path: Path) -> None:
    with pytest.raises(HandsSetupError, match="no free"):
        plan_setup(_entity(tmp_path), operator="alice", host="darwin", used_ids=set(range(300, 500)))


def test_linux_plan_makes_a_system_user_with_no_login_and_its_own_group(tmp_path: Path) -> None:
    plan = plan_setup(_entity(tmp_path), operator="alice", host="linux")
    useradd = next(a for a in _argvs(plan) if a and a[0] == "useradd")
    assert "--system" in useradd and useradd[useradd.index("--gid") + 1] == plan.group
    assert useradd[useradd.index("--shell") + 1] == "/usr/sbin/nologin"
    assert useradd[useradd.index("--comment") + 1] == HANDS_MARKER
    assert ("usermod", "--append", "--groups", plan.group, "alice") in _argvs(plan)


@pytest.mark.parametrize("host", ["darwin", "linux"])
def test_the_sudoers_step_is_validated_and_names_only_the_hands_user(tmp_path: Path, host) -> None:
    plan = plan_setup(_entity(tmp_path), operator="alice", host=host, used_ids=set())
    (step,) = [s for s in plan.steps if s.write is not None]
    path, content, mode = step.write
    assert path == Path("/etc/sudoers.d") / f"levain-{plan.hands_user}" and "." not in path.name
    assert mode == 0o440
    assert step.validate == ("visudo", "-cf")
    rules = [ln for ln in content.splitlines() if ln and not ln.startswith("#")]
    assert rules == ["Defaults:alice !requiretty", f"alice ALL=({plan.hands_user}) NOPASSWD: ALL"]


@pytest.mark.skipif(shutil.which("visudo") is None, reason="needs visudo")
def test_the_sudoers_text_parses(tmp_path: Path) -> None:
    import subprocess

    f = tmp_path / "rule"
    f.write_text(sudoers_text("alice", "_levain_coyote_abcdef"))
    r = subprocess.run(["visudo", "-cf", str(f)], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr


@pytest.mark.parametrize("host", ["darwin", "linux"])
def test_both_users_trust_the_workspace_in_git_and_it_is_idempotent(tmp_path: Path, host) -> None:
    plan = plan_setup(_entity(tmp_path), operator="alice", host=host, used_ids=set())
    git = [s for s in plan.steps if "safe.directory" in s.argv]
    assert {s.argv[2] for s in git} == {"alice", plan.hands_user}
    assert all(s.argv[-1] == f"{plan.workspace}/*" for s in git)
    assert all(s.skip_if and "--get-all" in s.skip_if for s in git)


# --- the undo plan ---------------------------------------------------------------------------------


@pytest.mark.parametrize("bad", ["root", "alice", "_levain_", "_levain_x_abcdeg", "_levain_X_abcdef"])
def test_undo_refuses_a_name_setup_could_not_have_written(tmp_path: Path, bad) -> None:
    with pytest.raises(HandsSetupError, match="refusing"):
        plan_undo(_entity(tmp_path), operator="alice", host="linux", hands_user=bad)


@pytest.mark.parametrize("host", ["darwin", "linux"])
def test_undo_removes_the_sudoers_rule_first_and_tolerates_missing_pieces(tmp_path: Path, host) -> None:
    ed = _entity(tmp_path)
    name = hands_user_name(ed)
    plan = plan_undo(ed, operator="alice", host=host, hands_user=name)
    assert plan.steps[0].argv == ("rm", "-f", f"/etc/sudoers.d/levain-{name}")
    assert all(s.allow_fail for s in plan.steps[1:])
    # what the hands user created goes back to the operator BEFORE the user is deleted
    order = [s.argv[0] for s in plan.steps]
    chown = next(i for i, s in enumerate(plan.steps) if s.argv[:1] == ("find",) and "-user" in s.argv)
    delete = next(i for i, s in enumerate(plan.steps) if s.argv[:1] in (("userdel",), ("/usr/bin/dscl",)))
    assert chown < delete, order
    assert plan.steps[chown].argv[plan.steps[chown].argv.index("-user") + 1] == name


# --- running a plan --------------------------------------------------------------------------------


def _plan(*steps: Step):
    return hands.Plan("linux", "alice", "_levain_x_abcdef", "_levain_x_abcdef", Path("/e"), Path("/e/w"), steps)


def test_dry_run_executes_nothing(tmp_path: Path) -> None:
    marker = tmp_path / "ran"
    assert run_plan(_plan(Step("touch", ("touch", str(marker)))), dry_run=True, emit=lambda _: None) == 0
    assert not marker.exists()


def test_run_plan_stops_at_the_first_failure(tmp_path: Path) -> None:
    marker = tmp_path / "ran"
    plan = _plan(Step("fail", ("false",)), Step("touch", ("touch", str(marker))))
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


# --- the record and the config key ----------------------------------------------------------------


def test_record_hands_user_sets_and_removes_the_key_keeping_others(tmp_path: Path) -> None:
    ed = _entity(tmp_path)
    cfg = ed / ".levain" / "confinement.json"
    cfg.write_text(json.dumps({"ssh_mode": "raw"}))
    name = hands_user_name(ed)
    hands.record_hands_user(ed, name, owner_uid=os.getuid(), owner_gid=os.getgid())
    assert json.loads(cfg.read_text()) == {"ssh_mode": "raw", "hands_user": name}
    assert load_confinement_config(ed).hands_user == name
    hands.record_hands_user(ed, None, owner_uid=os.getuid(), owner_gid=os.getgid())
    assert json.loads(cfg.read_text()) == {"ssh_mode": "raw"}
    assert load_confinement_config(ed).hands_user is None


@pytest.mark.parametrize("bad", ["root", "", 5, None, "_levain_coyote_ABCDEF"])
def test_the_loader_refuses_a_hands_user_setup_could_not_have_written(tmp_path: Path, bad) -> None:
    ed = _entity(tmp_path)
    (ed / ".levain" / "confinement.json").write_text(json.dumps({"hands_user": bad}))
    with pytest.raises(ConfinementError, match="hands_user"):
        load_confinement_config(ed)


def test_hands_user_is_appended_last_to_the_config_dataclass() -> None:
    from dataclasses import fields

    from levain.firing.confinement import ConfinementConfig

    assert [f.name for f in fields(ConfinementConfig)][-1] == "hands_user"


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


def test_setup_refuses_an_entity_already_set_up(tmp_path: Path, monkeypatch, capsys) -> None:
    ed = _entity(tmp_path)
    (ed / ".levain" / "confinement.json").write_text(json.dumps({"hands_user": hands_user_name(ed)}))
    monkeypatch.setattr(hands, "invoking_operator", lambda: "alice")
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
    (ed / ".levain" / "confinement.json").write_text(json.dumps({"hands_user": "_levain_nobody_000000"}))
    (r,) = doctor._check_hands_isolation(ed)
    assert not r.ok and "does not exist" in r.detail


def test_the_warn_badge_prints_its_hint(capsys) -> None:
    from levain import doctor

    doctor._emit(doctor.CheckResult("x", True, "detail", hint="do this", warn=True))
    out = capsys.readouterr().out
    assert "do this" in out and "[OK]" not in out

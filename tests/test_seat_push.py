"""The seat's lane-branch egress (PATHZERO Z2a, ruling H2 a): a lane's commits reach ``seat/<lane_key>`` with no
dialog, a seat has no path to main, and nothing pushes without a live signed ``seat-egress`` grant (Q1 is Phill's;
the build is lane-branch-only and grant-required). Real path throughout: a real signed authority log, a real mint
chain and clone, a real git, a LOCAL bare ``file://`` remote. Never GitHub."""
from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
from datetime import timedelta
from pathlib import Path

import pytest

pytest.importorskip("cryptography")
from levain import seat_lease as sl  # noqa: E402
from levain import seat_push as sp  # noqa: E402
from levain.consent import push  # noqa: E402
from levain.seat_profile import SeatRefusal, build_seat_profile  # noqa: E402
from tests.test_seat_lease import _git, rig  # noqa: E402,F401  (rig is a fixture)

pytestmark = pytest.mark.skipif(shutil.which("ssh-keygen") is None or shutil.which("git") is None,
                                reason="needs ssh-keygen and git")


class Egress:
    """The rig plus a bare remote, a levain home and the helpers one hook run needs."""

    def __init__(self, rig, tmp_path, monkeypatch):
        self.rig, self.world, self.tmp = rig, rig.world, tmp_path
        self.remote = tmp_path / "remote.git"
        subprocess.run(["git", "init", "-q", "--bare", str(self.remote)], check=True, env={**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null"})
        self.url = f"file://{self.remote}"
        self.home = tmp_path / "levain-home"
        self.home.mkdir()
        monkeypatch.setenv("LEVAIN_HOME", str(self.home))
        monkeypatch.setenv("HOME", str(tmp_path / "home"))
        (tmp_path / "home").mkdir()

    def now(self):
        return self.world.clock.now + timedelta(minutes=5)

    def sign(self, remote=None, domains=()):
        self.world.clock.now += timedelta(minutes=1)
        fp, sign = self.world.keys["laptop"]
        return sp.create_egress(self.world.root, lanes_parent=self.rig.lanes, push_remote=remote or self.url,
                                allowed_domains=domains, signer_fp=fp, sign=sign, clock=self.world.clock)

    def lane(self, name="lane-a", branch=True):
        plan = self.rig.launch(name)
        clone = Path(plan.profile.clone)
        if branch:
            _git(clone, "checkout", "-q", "-b", f"seat/{name}")
        return plan, clone

    def commit(self, clone, msg):
        _git(clone, "commit", "-q", "--allow-empty", "-m", msg)
        return _git(clone, "rev-parse", "HEAD")

    def hook(self, clone, event="Stop", stdin=None, err=None):
        import io
        err = err or io.StringIO()
        code = sp.run_hook(event, json.dumps(stdin if stdin is not None else {"stop_hook_active": False}), clone=clone,
                           key_dir=self.rig.key_dir, cockpit_root=self.world.root, home=self.home, err=err,
                           now=self.now)
        return code, err.getvalue()

    def remote_ref(self, ref):
        r = subprocess.run(["git", f"--git-dir={self.remote}", "rev-parse", "--verify", "--quiet", ref],
                           capture_output=True, text=True, env={**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null"})
        return r.stdout.strip() or None

    def log(self):
        p = self.home / "push" / sp.LOG_NAME
        return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []


@pytest.fixture
def eg(rig, tmp_path, monkeypatch):
    return Egress(rig, tmp_path, monkeypatch)


# --- the push, for real -----------------------------------------------------------------------------------------

def test_a_lane_commit_reaches_its_branch_with_no_dialog_and_a_second_stop_is_silent(eg):
    eg.rig.sign_lease()
    eg.sign()
    _plan, clone = eg.lane()
    tip = eg.commit(clone, "work one")
    assert eg.hook(clone) == (0, "")
    assert eg.remote_ref("refs/heads/seat/lane-a") == tip
    rows = eg.log()
    assert [(r["outcome"], r["status"], r["new_sha"]) for r in rows] == [("pushed", "new", tip)]
    assert rows[0]["via"] == "seat-egress" and rows[0]["branch"] == "seat/lane-a"
    assert eg.hook(clone) == (0, "") and len(eg.log()) == 1            # tip did not move: no git, no record
    nxt = eg.commit(clone, "work two")
    assert eg.hook(clone, "SessionEnd") == (0, "")
    assert eg.remote_ref("refs/heads/seat/lane-a") == nxt and eg.log()[-1]["status"] == "fast-forward"


def test_a_seat_has_no_path_to_main(eg):
    """The seat commits on main and on a stranger's branch too: only seat/<its lane_key> is ever named."""
    eg.rig.sign_lease()
    eg.sign()
    _plan, clone = eg.lane(branch=False)
    _git(clone, "checkout", "-q", "-B", "main")
    eg.commit(clone, "straight to main")
    _git(clone, "checkout", "-q", "-b", "seat/lane-b")
    eg.commit(clone, "another lane's name")
    assert eg.hook(clone) == (0, "")                                   # no seat/lane-a: nothing to push
    assert eg.remote_ref("refs/heads/main") is None and eg.remote_ref("refs/heads/seat/lane-b") is None
    assert eg.log() == []


@pytest.mark.parametrize("branch", ["main", "seat/lane-b", "seat/lane-a/x", "refs/heads/main"])
def test_the_judge_refuses_any_branch_but_the_lanes_own(eg, branch):
    eg.rig.sign_lease()
    grant = eg.sign()
    _plan, clone = eg.lane()
    eg.commit(clone, "x")
    judge = sp.egress_refusal(eg.world.root, eg.rig.key_dir, now=eg.now)
    real = os.path.realpath(clone)
    assert judge(path=real, remote=grant["push_remote"], branch="seat/lane-a")[0] == grant["id"]
    said = judge(path=real, remote=grant["push_remote"], branch=branch)
    assert isinstance(said, str) and "lane's seat/lane-a" in said
    # and through the executor itself: the remote stays empty
    with pytest.raises(push.PushRefused):
        push.execute({"id": grant["id"]}, repo_path=real, remote=grant["push_remote"], branch=branch,
                     levain_home=eg.home, refusal=judge, receipt=lambda r: None)
    assert eg.remote_ref("refs/heads/main") is None and eg.remote_ref(f"refs/heads/{branch}") is None


def test_the_judge_refuses_another_remote_a_foreign_clone_and_a_replaced_git(eg):
    eg.rig.sign_lease()
    grant = eg.sign()
    _plan, clone = eg.lane()
    eg.commit(clone, "x")
    judge = sp.egress_refusal(eg.world.root, eg.rig.key_dir, now=eg.now)
    real = os.path.realpath(clone)
    assert "pinned remote" in judge(path=real, remote="file:///elsewhere/r.git", branch="seat/lane-a")
    stranger = eg.rig.lanes / "stranger"                              # below the lanes parent, but never minted
    shutil.copytree(clone, stranger)
    assert "no verified mint row" in judge(path=os.path.realpath(stranger), remote=grant["push_remote"],
                                           branch="seat/lane-a")
    shutil.copytree(clone / ".git", clone / ".git.copy")             # a .git swapped for a copy: another inode
    shutil.rmtree(clone / ".git")
    os.rename(clone / ".git.copy", clone / ".git")
    assert "not the one the mint pinned" in judge(path=real, remote=grant["push_remote"], branch="seat/lane-a")
    assert eg.hook(clone) == (0, "")
    assert eg.remote_ref("refs/heads/seat/lane-a") is None
    assert "not the one the mint pinned" in eg.log()[-1]["detail"]


def test_a_clone_outside_the_grants_lanes_parent_is_refused(eg, tmp_path):
    eg.rig.sign_lease()
    eg.sign()
    _plan, clone = eg.lane()
    eg.commit(clone, "x")
    other = eg.rig.tmp / "other-lanes"
    other.mkdir()
    # a second grant pinned to another parent: the clone is below the LEASE's parent, not this grant's
    from levain.consent.grants import revoke_grant
    fp, sign = eg.world.keys["laptop"]
    live = sp.live_egress(sl._replay(eg.world.root), eg.now())
    eg.world.clock.now += timedelta(minutes=1)
    revoke_grant(eg.world.root, live["id"], signer_fp=fp, sign=sign, clock=eg.world.clock)
    eg.world.clock.now += timedelta(minutes=1)
    sp.create_egress(eg.world.root, lanes_parent=other, push_remote=eg.url, signer_fp=fp, sign=sign, clock=eg.world.clock)
    assert eg.hook(clone) == (0, "")
    assert eg.remote_ref("refs/heads/seat/lane-a") is None
    assert "not below the grant's lanes parent" in eg.log()[-1]["detail"]


# --- the grant is required ----------------------------------------------------------------------------------------

def test_no_grant_no_push_and_a_record(eg):
    eg.rig.sign_lease()
    _plan, clone = eg.lane()
    eg.commit(clone, "x")
    assert eg.hook(clone) == (0, "")
    assert eg.remote_ref("refs/heads/seat/lane-a") is None
    assert [(r["outcome"], r["detail"][:19]) for r in eg.log()] == [("refused", "no live seat-egress")]


def test_a_revoked_grant_stops_the_next_push_and_leaves_a_record(eg):
    from levain.consent.grants import revoke_grant
    eg.rig.sign_lease()
    grant = eg.sign()
    _plan, clone = eg.lane()
    first = eg.commit(clone, "one")
    assert eg.hook(clone) == (0, "")
    assert eg.remote_ref("refs/heads/seat/lane-a") == first
    fp, sign = eg.world.keys["laptop"]
    eg.world.clock.now += timedelta(minutes=1)
    revoke_grant(eg.world.root, grant["id"], signer_fp=fp, sign=sign, clock=eg.world.clock)
    eg.commit(clone, "two, after the revoke")
    assert eg.hook(clone) == (0, "")
    assert eg.remote_ref("refs/heads/seat/lane-a") == first          # the remote did not move
    assert eg.log()[-1]["outcome"] == "refused" and "no live seat-egress" in eg.log()[-1]["detail"]


def test_an_expired_grant_pushes_nothing(eg):
    eg.rig.sign_lease()
    eg.sign()
    _plan, clone = eg.lane()
    eg.commit(clone, "x")
    import io
    code = sp.run_hook("Stop", "{}", clone=clone, key_dir=eg.rig.key_dir, cockpit_root=eg.world.root, home=eg.home,
                       err=io.StringIO(), now=lambda: eg.world.clock.now + timedelta(days=15))
    assert code == 0 and eg.remote_ref("refs/heads/seat/lane-a") is None
    assert "no live seat-egress" in eg.log()[-1]["detail"]


def test_a_grant_revoked_between_the_hooks_check_and_the_lock_is_caught_by_the_executors_own_judgment(eg):
    """The executor calls the judge under the per-repo lock against a FRESH replay: a grant that vanished after the
    hook's first look still pushes nothing."""
    from levain.consent.grants import revoke_grant
    eg.rig.sign_lease()
    grant = eg.sign()
    _plan, clone = eg.lane()
    eg.commit(clone, "x")
    real = sp.egress_refusal(eg.world.root, eg.rig.key_dir, now=eg.now)
    fp, sign = eg.world.keys["laptop"]

    def racing(**kw):
        eg.world.clock.now += timedelta(minutes=1)
        revoke_grant(eg.world.root, grant["id"], signer_fp=fp, sign=sign, clock=eg.world.clock)
        return real(**kw)
    with pytest.raises(push.PushRefused, match="no live seat-egress"):
        push.execute({"id": grant["id"]}, repo_path=os.path.realpath(clone), remote=grant["push_remote"],
                     branch="seat/lane-a", levain_home=eg.home, refusal=racing, receipt=lambda r: None)
    assert eg.remote_ref("refs/heads/seat/lane-a") is None


def test_a_broken_mint_chain_pushes_nothing(eg):
    eg.rig.sign_lease()
    eg.sign()
    _plan, clone = eg.lane()
    eg.commit(clone, "x")
    mints = eg.rig.key_dir / "mints.jsonl"
    row = json.loads(mints.read_text().splitlines()[0])
    row["lane_key"] = "lane-z"                                         # a forged lane key: the signature no longer holds
    mints.write_text(json.dumps(row) + "\n")
    assert eg.hook(clone) == (0, "")
    assert eg.remote_ref("refs/heads/seat/lane-z") is None and eg.remote_ref("refs/heads/seat/lane-a") is None
    assert "mint chain" in eg.log()[-1]["detail"]


# --- the repository is the seat's, so nothing in it is run ---------------------------------------------------------

def test_a_planted_pre_push_hook_hookspath_and_config_never_run(eg):
    eg.rig.sign_lease()
    eg.sign()
    _plan, clone = eg.lane()
    tip = eg.commit(clone, "x")
    marks = eg.tmp / "marks"
    marks.mkdir()
    for d in (clone / ".git" / "hooks", eg.tmp / "hp"):
        d.mkdir(exist_ok=True)
        for h in ("pre-push", "reference-transaction", "post-update", "pre-receive", "update", "post-receive",
                  "fsmonitor-watchman", "pre-commit"):
            f = d / h
            f.write_text(f"#!/bin/sh\necho x >> {marks}/{d.name}-{h}.fired\nexit 0\n")
            f.chmod(0o755)
    bin_ = eg.tmp / "bin-mark"
    bin_.write_text(f"#!/bin/sh\necho \"$0\" >> {marks}/cfg.fired\nexit 0\n")
    bin_.chmod(0o755)
    evil = eg.tmp / "evil.git"
    subprocess.run(["git", "init", "-q", "--bare", str(evil)], check=True, env={**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null"})
    for k, v in (("core.hooksPath", str(eg.tmp / "hp")), ("core.fsmonitor", str(bin_)), ("core.sshCommand", str(bin_)),
                 ("uploadpack.packObjectsHook", str(bin_)), ("remote.origin.url", f"file://{evil}"),
                 ("remote.origin.pushurl", f"file://{evil}"), (f"url.file://{evil}.insteadOf", eg.url),
                 (f"url.file://{evil}.pushInsteadOf", eg.url), ("credential.helper", str(bin_)),
                 ("gpg.program", str(bin_)), ("push.gpgSign", "true"), ("core.alternateRefsCommand", str(bin_))):
        _git(clone, "config", k, v)
    assert eg.hook(clone) == (0, "")
    assert eg.remote_ref("refs/heads/seat/lane-a") == tip              # the pinned remote, not the planted one
    assert subprocess.run(["git", f"--git-dir={evil}", "for-each-ref"], capture_output=True, text=True).stdout == ""
    assert sorted(p.name for p in marks.glob("*.fired")) == []         # nothing the repo carries ever ran


def test_a_non_fast_forward_is_rejected_never_forced_and_the_model_hears_it_once(eg):
    eg.rig.sign_lease()
    eg.sign()
    _plan, clone = eg.lane()
    first = eg.commit(clone, "one")
    assert eg.hook(clone) == (0, "")
    _git(clone, "commit", "-q", "--amend", "--allow-empty", "-m", "one, rewritten")
    code, err = eg.hook(clone)
    assert code == 2 and "rejected your lane branch" in err and "Nothing was forced" in err
    assert eg.remote_ref("refs/heads/seat/lane-a") == first
    assert eg.log()[-1]["outcome"] == "not-pushed" and eg.log()[-1]["status"] == "rejected"
    assert eg.hook(clone) == (0, "")                                   # the rejected tip is not retried or repeated
    _git(clone, "commit", "-q", "--amend", "--allow-empty", "-m", "rewritten again")
    assert eg.hook(clone, stdin={"stop_hook_active": True}) == (0, "")  # a second Stop never loops the model
    assert eg.remote_ref("refs/heads/seat/lane-a") == first


# --- the hook as the profile carries it ----------------------------------------------------------------------------

def test_the_profile_without_a_grant_is_todays_profile(eg):
    eg.rig.sign_lease()
    plan = eg.rig.launch("lane-a")
    s = plan.profile.settings
    assert set(s["hooks"]) == {"PermissionRequest", "PreToolUse", "PostToolUse", "PostToolUseFailure"}
    assert s["sandbox"]["network"]["allowedDomains"] == []


def test_the_profile_with_a_live_grant_carries_the_hooks_with_timeouts_and_the_grants_domains(eg):
    eg.rig.sign_lease()
    eg.sign(domains=["docs.example.com", "api.example.com"])
    plan = eg.rig.launch("lane-a")
    s = plan.profile.settings
    assert s["sandbox"]["network"]["allowedDomains"] == ["api.example.com", "docs.example.com"]
    stop = s["hooks"]["Stop"][0]["hooks"][0]
    end = s["hooks"]["SessionEnd"][0]["hooks"][0]
    assert stop["timeout"] == sp.STOP_TIMEOUT and end["timeout"] == sp.SESSION_END_TIMEOUT   # U9: SessionEnd needs one
    for entry, event in ((stop, "Stop"), (end, "SessionEnd")):
        argv = shlex.split(entry["command"])
        assert argv[1:6] == ["-EP", "-B", "-m", "levain.seat_push", event]
        assert argv[argv.index("--clone") + 1] == str(plan.profile.clone)
        assert argv[argv.index("--key-dir") + 1] == str(eg.rig.key_dir)
        assert argv[argv.index("--cockpit-root") + 1] == str(eg.world.root)
        assert argv[argv.index("--home") + 1] == str(eg.home)
    assert plan.mint["profile_digest"] == plan.profile.digest()


def test_a_grant_on_another_lanes_parent_than_the_lease_refuses_the_launch(eg):
    eg.rig.sign_lease()
    other = eg.rig.tmp / "other"
    fp, sign = eg.world.keys["laptop"]
    eg.world.clock.now += timedelta(minutes=1)
    sp.create_egress(eg.world.root, lanes_parent=other, push_remote=eg.url, signer_fp=fp, sign=sign, clock=eg.world.clock)
    with pytest.raises(SeatRefusal, match="not (the lease's|below the egress)"):
        eg.rig.launch("lane-a")
    assert not (eg.rig.lanes / "lane-a").exists()                     # the mint was unwound


def test_a_canary_profile_never_takes_a_grant(eg):
    grant = {"id": "se-000000000001", "kind": "seat-egress", "agent": "claude_code", "lanes_parent": "/x",
             "push_remote": "file:///r.git", "allowed_domains": []}
    with pytest.raises(SeatRefusal, match="needs a lease launch"):
        build_seat_profile(Path("/nonexistent/c"), Path("/nonexistent/r"), claude_bin="/bin/true",
                           python=sys.executable, lease=None, key_dir=Path("/nonexistent/k"),
                           canary_root=Path("/nonexistent"), egress=grant)


def test_every_seat_hook_keeps_the_user_site_on_the_import_path(eg, tmp_path):
    """Reproduced 11-07: a levain installed with ``pip install --user`` was invisible to the seat hooks, because ``-I``
    also turns the user site off, and the seat's Stop push, its tool-call records and its observe hook then failed with
    ModuleNotFoundError. The consent hooks already run ``-EP`` for this reason. Takes the interpreter flags from the
    commands the profile really carries and runs them on a non-venv interpreter against a stub package in a throwaway
    user site."""
    eg.rig.sign_lease()
    eg.sign()
    plan = eg.rig.launch("lane-a")
    hooks = plan.profile.settings["hooks"]
    cmds = [h["command"] for event in ("Stop", "SessionEnd", "PreToolUse", "PostToolUse", "PermissionRequest")
            for g in hooks[event] for h in g["hooks"]]
    assert len(cmds) == 5
    base = getattr(sys, "_base_executable", sys.executable)
    ub = tmp_path / "userbase"
    probe = subprocess.run([base, "-c", "import site; print(int(site.ENABLE_USER_SITE)); print(site.getusersitepackages())"],
                           capture_output=True, text=True, env={**os.environ, "PYTHONUSERBASE": str(ub)}, timeout=60)
    enabled, _, usersite = probe.stdout.partition("\n")
    if probe.returncode != 0 or enabled != "1":
        pytest.skip("the base interpreter has no user site here")
    (Path(usersite.strip()) / "levainprobe").mkdir(parents=True)
    (Path(usersite.strip()) / "levainprobe" / "__init__.py").write_text("")
    (Path(usersite.strip()) / "levainprobe" / "mod.py").write_text("print('PROBE-RAN')\n")
    for cmd in cmds:
        argv = shlex.split(cmd)
        flags = argv[1:argv.index("-m")]
        assert "-I" not in flags, cmd
        r = subprocess.run([base, *flags, "-m", "levainprobe.mod"], capture_output=True, text=True, timeout=60,
                           env={"PATH": "/usr/bin:/bin", "PYTHONUSERBASE": str(ub), "HOME": str(tmp_path)})
        assert "PROBE-RAN" in r.stdout, (cmd, r.stderr)


def test_the_hook_command_runs_as_a_process_and_pushes(eg):
    """The exact command the profile carries, run through a shell with the Stop payload on stdin. (`-EP` ignores
    PYTHONPATH and the installed levain is not this tree, so the one flag differs here: RESIDUE for the VM run.)"""
    eg.rig.sign_lease()
    eg.sign()
    plan, clone = eg.lane()
    tip = eg.commit(clone, "via a process")
    cmd = plan.profile.settings["hooks"]["Stop"][0]["hooks"][0]["command"].replace(" -EP ", " ", 1)
    src = str(Path(sp.__file__).resolve().parents[1])
    r = subprocess.run(cmd, shell=True, input=json.dumps({"stop_hook_active": False, "cwd": "/anywhere"}), text=True,
                       capture_output=True, env={"PATH": "/usr/bin:/bin", "PYTHONPATH": src, "HOME": str(eg.tmp / "home"),
                                                 "LEVAIN_HOME": str(eg.home)}, timeout=120)
    assert r.returncode == 0, r.stderr
    assert eg.remote_ref("refs/heads/seat/lane-a") == tip


def test_a_malformed_command_line_or_stdin_never_blocks_the_seat(eg, capsys):
    assert sp.main(["Stop"]) == 0                                      # argparse errors end 0, not 2
    assert sp.main(["Bogus", "--clone", "x"]) == 0
    eg.rig.sign_lease()
    _plan, clone = eg.lane()
    assert sp.run_hook("Stop", "not json {", clone=clone, key_dir=eg.rig.key_dir, cockpit_root=eg.world.root,
                       home=eg.home) == 0


def test_the_cli_parses_egress_commands(eg, monkeypatch):
    from levain import cli
    assert cli.main(["seat", "egress", "list", "--data-root", str(eg.world.root)]) == 0


# --- L3 zc r1 fixes ----------------------------------------------------------------------------------------------

def _revoke_live(eg):
    from levain.consent.grants import revoke_grant
    fp, sign = eg.world.keys["laptop"]
    live = sp.live_egress(sl._replay(eg.world.root), eg.now())
    eg.world.clock.now += timedelta(minutes=1)
    revoke_grant(eg.world.root, live["id"], signer_fp=fp, sign=sign, clock=eg.world.clock)


def _bare(eg, name):
    p = eg.tmp / name
    subprocess.run(["git", "init", "-q", "--bare", str(p)], check=True, env={**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null"})
    return p


def _ref(bare, ref):
    r = subprocess.run(["git", f"--git-dir={bare}", "rev-parse", "--verify", "--quiet", ref], capture_output=True,
                       text=True, env={**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null"})
    return r.stdout.strip() or None


def test_an_unmoved_tip_reaches_a_new_grants_remote_after_a_revoke(eg):
    """codex MED 2: the pushed-tip mark is keyed by the grant (id, incarnation, remote), not the lane alone."""
    eg.rig.sign_lease()
    eg.sign()
    _plan, clone = eg.lane()
    tip = eg.commit(clone, "one")
    assert eg.hook(clone) == (0, "") and eg.remote_ref("refs/heads/seat/lane-a") == tip
    _revoke_live(eg)
    other = _bare(eg, "remote-b.git")
    eg.sign(remote=f"file://{other}")
    assert eg.hook(clone) == (0, "")                                   # the tip did NOT move
    assert _ref(other, "refs/heads/seat/lane-a") == tip
    rows = eg.log()
    assert (rows[-1]["outcome"], rows[-1]["remote"]) == ("pushed", f"file://{other}")
    n = len(rows)
    assert eg.hook(clone) == (0, "") and len(eg.log()) == n           # and now silent under THIS grant


def test_a_rejected_tip_is_offered_again_under_a_new_grant(eg):
    eg.rig.sign_lease()
    eg.sign()
    _plan, clone = eg.lane()
    eg.commit(clone, "one")
    assert eg.hook(clone) == (0, "")
    _git(clone, "commit", "-q", "--amend", "--allow-empty", "-m", "rewritten")
    rewritten = _git(clone, "rev-parse", "HEAD")
    assert eg.hook(clone)[0] == 2                                      # rejected by remote A
    assert eg.hook(clone) == (0, "")                                   # not retried under A
    _revoke_live(eg)
    fresh = _bare(eg, "remote-c.git")
    eg.sign(remote=f"file://{fresh}")
    assert eg.hook(clone) == (0, "")
    assert _ref(fresh, "refs/heads/seat/lane-a") == rewritten


def test_a_rejection_nobody_heard_on_session_end_is_retried_at_the_next_hook(eg):
    """gemini MED 4: SessionEnd cannot reach the model, so its rejection is not remembered as settled; the next Stop
    tells the model once."""
    eg.rig.sign_lease()
    eg.sign()
    _plan, clone = eg.lane()
    eg.commit(clone, "one")
    assert eg.hook(clone) == (0, "")
    _git(clone, "commit", "-q", "--amend", "--allow-empty", "-m", "rewritten")
    assert eg.hook(clone, event="SessionEnd") == (0, "")               # rejected, no one to tell
    code, err = eg.hook(clone)
    assert code == 2 and "rejected your lane branch" in err            # the next Stop still tells the model


def test_a_transport_error_is_not_told_as_a_rejection_and_is_retried(eg):
    """codex MED 3 / complement 2: git failing with no ref line (here: no repository at the pinned path, the shape of
    DNS / auth / transport) is status "error": recorded, exit 0, no rebase text, tried again at the next Stop."""
    eg.rig.sign_lease()
    eg.sign(remote=f"file://{eg.tmp}/no-such-remote.git")
    _plan, clone = eg.lane()
    eg.commit(clone, "one")
    code, err = eg.hook(clone)
    assert (code, err) == (0, "")
    rows = eg.log()
    assert (rows[-1]["outcome"], rows[-1]["status"]) == ("not-pushed", "error")
    assert eg.hook(clone) == (0, "") and len(eg.log()) == len(rows) + 1   # retried, not remembered


def test_a_mint_read_waits_for_an_append_in_flight_and_never_sees_half_a_row(eg):
    """codex MED 4 / deepseek: read_mints takes the lock append_mint holds, so a SessionEnd reading while a launch
    appends sees the whole row (or none of it), never a broken chain."""
    import fcntl
    import threading
    eg.rig.sign_lease()
    eg.rig.launch("lane-a")
    path = eg.rig.key_dir / "mints.jsonl"
    whole = path.read_bytes()
    fd = os.open(path, os.O_RDWR | os.O_APPEND)
    out: dict = {}
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)                                 # what append_mint holds across its write
        os.ftruncate(fd, 0)
        os.write(fd, whole[: len(whole) // 2])                         # half a row on disk
        t = threading.Thread(target=lambda: out.update(rows=_try(lambda: sl.read_mints(eg.rig.key_dir))))
        t.start()
        t.join(0.5)
        assert t.is_alive(), f"the read did not wait for the lock: {out}"
        os.write(fd, whole[len(whole) // 2:])
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
    t.join(10)
    assert [r["lane_key"] for r in out["rows"]] == ["lane-a"]


def _try(fn):
    try:
        return fn()
    except Exception as exc:  # noqa: BLE001
        return exc


def test_a_record_incomplete_push_is_remembered_and_logged(eg, monkeypatch):
    """gemini HIGH 1: PushRecordIncomplete is settled like any reported push (the lane comes from the mint row, never
    re-parsed from the branch); a rejection inside it still reaches the model."""
    real = push._record
    monkeypatch.setattr(push, "_record", lambda *a, **k: real(*a, **k) + ["done journal: injected"])
    eg.rig.sign_lease()
    eg.sign()
    _plan, clone = eg.lane()
    tip = eg.commit(clone, "one")
    assert eg.hook(clone) == (0, "")
    assert eg.remote_ref("refs/heads/seat/lane-a") == tip
    assert [r["outcome"] for r in eg.log()] == ["pushed", "record-incomplete"]
    assert eg.hook(clone) == (0, "") and len(eg.log()) == 2            # remembered: silent
    _git(clone, "commit", "-q", "--amend", "--allow-empty", "-m", "rewritten")
    code, err = eg.hook(clone)
    assert code == 2 and "rejected your lane branch" in err


def test_a_mark_that_cannot_be_written_never_swallows_the_rejection(eg):
    """deepseek MED-1: a failed remember after a rejection still exits 2 with the text, and leaves a record."""
    eg.rig.sign_lease()
    eg.sign()
    _plan, clone = eg.lane()
    eg.commit(clone, "one")
    assert eg.hook(clone) == (0, "")
    _git(clone, "commit", "-q", "--amend", "--allow-empty", "-m", "rewritten")
    marks = eg.rig.key_dir / sp.PUSHED_DIR
    marks.chmod(0o500)
    try:
        code, err = eg.hook(clone)
    finally:
        marks.chmod(0o700)
    assert code == 2 and "rejected your lane branch" in err
    assert "pushed-tip mark was not written" in eg.log()[-1]["detail"]


def test_a_grant_signed_while_the_clone_runs_reaches_the_profile(eg, monkeypatch):
    """deepseek MED-3 (refuted on disk, pinned): the egress is read from the replay taken AFTER the clone."""
    eg.rig.sign_lease()
    real = sl.mint_lane

    def slow_clone(*a, **k):
        out = real(*a, **k)
        eg.sign()                                                       # signed during the clone
        return out
    monkeypatch.setattr(sl, "mint_lane", slow_clone)
    plan = eg.rig.launch("lane-a", now=eg.world.clock.now + timedelta(minutes=3))
    assert "Stop" in plan.profile.settings["hooks"]


def _cli_signing(eg, monkeypatch):
    from levain import cli
    from levain.cockpit import keys
    monkeypatch.setattr(keys, "ssh_signer", lambda path: eg.world.keys["laptop"])
    monkeypatch.setattr(cli, "_utc_now", lambda: eg.world.clock.now)
    return cli


def test_egress_renew_refuses_with_a_reason_and_never_prints_success(eg, monkeypatch, capsys):
    """gemini HIGH 2 / complement 6: a refused renew exits non-zero naming why; a lease id is never renewed here."""
    cli = _cli_signing(eg, monkeypatch)
    lease = eg.rig.sign_lease()
    grant = eg.sign()
    args = ["--data-root", str(eg.world.root), "--signing-key", str(eg.tmp / "unused")]
    before = sl._replay(eg.world.root).policy.grants[lease["id"]]["expires_at"]
    eg.world.clock.now += timedelta(minutes=1)
    assert cli.main(["seat", "egress", "renew", lease["id"], *args]) == 1
    out = capsys.readouterr()
    assert "renewed" not in out.out and "not a seat-egress grant" in out.err
    assert sl._replay(eg.world.root).policy.grants[lease["id"]]["expires_at"] == before
    eg.world.clock.now += timedelta(minutes=1)
    assert cli.main(["seat", "egress", "renew", grant["id"], *args]) == 0
    out = capsys.readouterr()
    held = sl._replay(eg.world.root).policy.grants[grant["id"]]["expires_at"]
    assert out.out.strip() == f"renewed: {grant['id']} until {held}" and held > grant["expires_at"]
    _revoke_live(eg)
    eg.world.clock.now += timedelta(minutes=1)
    assert cli.main(["seat", "egress", "renew", grant["id"], *args]) == 1
    out = capsys.readouterr()
    assert "renewed" not in out.out and "seat egress refused" in out.err


def test_egress_revoke_never_revokes_a_lease_or_another_kind(eg, monkeypatch, capsys):
    """codex MED (bundle3 final): `levain seat egress revoke <lease id>` revoked the lease and printed an egress line."""
    cli = _cli_signing(eg, monkeypatch)
    lease = eg.rig.sign_lease()
    grant = eg.sign()
    args = ["--data-root", str(eg.world.root), "--signing-key", str(eg.tmp / "unused")]
    assert cli.main(["seat", "egress", "revoke", lease["id"], *args]) == 1
    out = capsys.readouterr()
    assert "revoked" not in out.out and "not a seat-egress grant" in out.err
    assert sl._replay(eg.world.root).policy.grants[lease["id"]]["expires_at"] == lease["expires_at"]   # untouched
    assert "revoked_at" not in sl._replay(eg.world.root).policy.grants[lease["id"]]
    assert cli.main(["seat", "egress", "revoke", grant["id"], *args]) == 0
    assert capsys.readouterr().out.startswith(f"revoked: {grant['id']}")


def test_a_relative_cockpit_root_is_made_absolute_for_the_profile(eg, monkeypatch):
    """codex MED / gemini HIGH 2: the hooks run from the clone, so a relative --data-root must not reach the profile."""
    eg.rig.sign_lease()
    eg.sign()
    monkeypatch.chdir(eg.world.root.parent)
    rel = Path(eg.world.root.name)
    plan = eg.rig.launch("lane-rel", cockpit_root=rel)
    cmd = plan.profile.settings["hooks"]["Stop"][0]["hooks"][0]["command"]
    assert str(eg.world.root.resolve()) in cmd or str(eg.world.root) in cmd


def test_a_lapsed_grant_is_not_renewed_beside_a_live_one(eg, monkeypatch, capsys):
    """gemini MED 3 (refuted: the log never renews a lapsed grant, and a loosen is judged on the policy AFTER it)."""
    cli = _cli_signing(eg, monkeypatch)
    eg.rig.sign_lease()
    old = eg.sign()
    eg.world.clock.now += timedelta(days=15)                          # the first grant lapses
    eg.rig.sign_lease()
    eg.sign()
    assert cli.main(["seat", "egress", "renew", old["id"], "--data-root", str(eg.world.root),
                     "--signing-key", str(eg.tmp / "unused")]) == 1
    assert "expired" in capsys.readouterr().err

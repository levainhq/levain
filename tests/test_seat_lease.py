"""The seat-root lease and the clone launch (PATHZERO Z1d, ruling D2 a): a launch needs a live signed lease, mints
exactly a lane clone plus its RETURN directory, and never executes claude. Real logs (the graduation tests' ``World``)
and a real local git source; the host's key directory is a tmp dir and the managed settings are empty."""
from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
from datetime import timedelta
from pathlib import Path

import pytest

pytest.importorskip("cryptography")
from levain import seat_lease as sl  # noqa: E402
from levain.seat_profile import SeatRefusal  # noqa: E402
from tests.test_authority_graduation_kind import World  # noqa: E402

pytestmark = pytest.mark.skipif(shutil.which("ssh-keygen") is None or shutil.which("git") is None,
                                reason="needs ssh-keygen and git")


def _git(cwd: Path, *args: str) -> str:
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@t", "GIT_CONFIG_GLOBAL": "/dev/null"}
    return subprocess.run(["git", *args], cwd=cwd, env=env, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def rig(tmp_path, monkeypatch):
    from levain.cockpit.firejournal import CockpitDB
    world = World(tmp_path)
    CockpitDB(world.root).set_install_class("reported")
    src = tmp_path / "source"
    src.mkdir()
    _git(src, "init", "-q")
    (src / "a.txt").write_text("hello\n")
    _git(src, "add", "a.txt")
    _git(src, "commit", "-q", "-m", "one")
    sha = _git(src, "rev-parse", "HEAD")
    key_dir = tmp_path / "keys" / "seat_launcher"
    key_dir.mkdir(parents=True, mode=0o700)
    monkeypatch.setattr(sl, "key_dir_for", lambda install, roots: key_dir)
    claude = tmp_path / "bin" / "claude"
    claude.parent.mkdir()
    claude.write_text("#!/bin/sh\nexit 0\n")
    claude.chmod(0o755)

    class Rig:
        pass
    r = Rig()
    r.world, r.src, r.sha, r.key_dir, r.claude, r.tmp = world, src, sha, key_dir, str(claude), tmp_path
    r.lanes, r.returns = tmp_path / "lanes", tmp_path / "returns"

    def sign_lease():
        world.clock.now += timedelta(minutes=1)
        fp, sign = world.keys["laptop"]
        return sl.create_lease(world.root, lanes_parent=r.lanes, returns_parent=r.returns, key_dir=key_dir,
                               signer_fp=fp, sign=sign, clock=world.clock)

    def launch(lane="lane-a", *, now=None, cockpit_root=None):
        return sl.prepare_launch(lane, str(src), sha, claude_bin=r.claude, python=sys.executable,
                                 cockpit_root=cockpit_root or world.root, install=tmp_path,
                                 now=now or world.clock.now + timedelta(minutes=1), managed=())
    r.sign_lease, r.launch = sign_lease, launch
    return r


def test_a_launch_with_no_lease_refuses_and_mints_nothing(rig):
    with pytest.raises(sl.SeatLaunchRefused, match="no live seat-root lease") as exc:
        rig.launch()
    assert "levain seat lease create" in str(exc.value)          # the one sign command, never a prompt fallback
    assert not rig.lanes.exists() and not rig.returns.exists()


def test_an_expired_lease_refuses(rig):
    rig.sign_lease()
    with pytest.raises(sl.SeatLaunchRefused, match="no live seat-root lease"):
        rig.launch(now=rig.world.clock.now + timedelta(days=14, minutes=1))
    assert list(rig.lanes.iterdir()) == []


def test_a_minted_root_is_exactly_the_lane_clone_and_its_return_dir_and_the_mint_row_pins_git(rig):
    lease = rig.sign_lease()
    plan = rig.launch("lane-a")
    clone, ret = Path(plan.profile.clone), Path(plan.profile.return_dir)
    assert [p.name for p in rig.lanes.iterdir()] == ["lane-a"] and [p.name for p in rig.returns.iterdir()] == ["lane-a"]
    assert clone == (rig.lanes / "lane-a").resolve() and ret == (rig.returns / "lane-a").resolve()
    assert plan.profile.roots == (clone, ret) and list(ret.iterdir()) == []
    assert (clone / "a.txt").read_text() == "hello\n" and _git(clone, "rev-parse", "HEAD") == rig.sha
    assert not (clone / ".git").is_symlink() and list((clone / ".git" / "hooks").glob("*")) == []   # --template= : no hooks
    st = os.lstat(clone / ".git")
    mint = plan.mint
    assert mint["git"] == [st.st_dev, st.st_ino] and mint["lease_id"] == lease["id"]
    assert mint["profile_digest"] == plan.profile.digest() and mint["base_sha"] == rig.sha
    rows = sl.read_mints(rig.key_dir)                              # chain + launcher-key signature verify
    assert [r["hash"] for r in rows] == [mint["hash"]]
    assert stat.S_IMODE(os.stat(rig.key_dir / "launcher.ed25519").st_mode) == 0o600


def test_a_mint_never_adopts_an_existing_path(rig):
    rig.sign_lease()
    (rig.lanes / "lane-a").mkdir()
    with pytest.raises(sl.SeatLaunchRefused, match="never adopts"):
        rig.launch("lane-a")
    assert not (rig.returns / "lane-a").exists()
    with pytest.raises(sl.SeatLaunchRefused, match="lane key"):
        rig.launch("../escape")


def test_a_revoked_lease_refuses_later_launches_but_a_running_seats_root_and_record_stay(rig):
    from levain.consent.grants import revoke_grant
    lease = rig.sign_lease()
    first = rig.launch("lane-a")
    fp, sign = rig.world.keys["laptop"]
    rig.world.clock.now += timedelta(minutes=1)
    revoke_grant(rig.world.root, lease["id"], signer_fp=fp, sign=sign, clock=rig.world.clock)
    with pytest.raises(sl.SeatLaunchRefused, match="no live seat-root lease"):
        rig.launch("lane-b")
    assert not (rig.lanes / "lane-b").exists()
    assert Path(first.profile.clone).is_dir() and Path(first.profile.return_dir).is_dir()   # the running seat's roots
    assert [r["hash"] for r in sl.read_mints(rig.key_dir)] == [first.mint["hash"]]           # and its record


def test_a_profile_refusal_removes_what_the_mint_made_and_writes_no_row(rig):
    rig.sign_lease()
    src2 = rig.tmp / "dirty"
    src2.mkdir()
    _git(src2, "init", "-q")
    (src2 / ".claude").mkdir()
    (src2 / ".claude" / "settings.json").write_text("{}")
    _git(src2, "add", "-f", ".claude/settings.json")
    _git(src2, "commit", "-q", "-m", "x")
    sha = _git(src2, "rev-parse", "HEAD")
    with pytest.raises(SeatRefusal, match="agent-config"):
        sl.prepare_launch("lane-d", str(src2), sha, claude_bin=rig.claude, python=sys.executable,
                          cockpit_root=rig.world.root, install=rig.tmp, now=rig.world.clock.now + timedelta(minutes=1),
                          managed=())
    assert list(rig.lanes.iterdir()) == [] and list(rig.returns.iterdir()) == [] and sl.read_mints(rig.key_dir) == []


def test_validate_launch_prints_the_argv_and_digest_and_never_executes(rig, monkeypatch):
    rig.sign_lease()
    for name in ("execv", "execvp", "execve", "execvpe"):
        monkeypatch.setattr(os, name, lambda *a, **k: pytest.fail("a launch must not exec claude"))
    ran = []
    real_run = subprocess.run
    monkeypatch.setattr(subprocess, "run", lambda argv, *a, **k: (ran.append(argv[0]), real_run(argv, *a, **k))[1])
    import io
    out, err = io.StringIO(), io.StringIO()
    rc = sl.validate_launch("lane-p", str(rig.src), rig.sha, claude_bin=rig.claude, python=sys.executable,
                        cockpit_root=rig.world.root, install=rig.tmp, out=out, err=err,
                        now=rig.world.clock.now + timedelta(minutes=1), managed=())
    assert rc == 0 and "not started" in out.getvalue() and "digest" in out.getvalue() and err.getvalue() == ""
    assert rig.claude not in ran


def test_a_lease_whose_key_is_not_the_launchers_refuses(rig):
    rig.sign_lease()
    (rig.key_dir / "launcher.ed25519").unlink()
    sl.ensure_launcher_key(rig.key_dir)                              # a different key under the same dir
    with pytest.raises(sl.SeatLaunchRefused, match="not the one the lease pins"):
        rig.launch()


def test_the_lease_kind_has_one_definition_authority_owns():
    import levain.authority as authority
    import levain.seat_lease as seat_lease
    import levain.seat_profile as seat_profile
    assert seat_profile.SEAT_LEASE_KIND is authority.SEAT_LEASE_KIND
    assert seat_lease.SEAT_LEASE_KIND is authority.SEAT_LEASE_KIND


# --- L3 z1 r1 fixes ------------------------------------------------------------------------------------------------

class _Ticking:
    """A real clock's shape: every read is a microsecond later than the last (codex 4: a renew read it twice)."""

    def __init__(self, world):
        self.world = world

    def __call__(self):
        self.world.clock.now += timedelta(microseconds=1)
        return self.world.clock.now


def test_a_renew_on_a_clock_that_moves_between_reads_signs_and_extends(rig):
    lease = rig.sign_lease()
    rig.world.clock.now += timedelta(days=1)
    fp, sign = rig.world.keys["laptop"]
    expires = sl.renew_lease(rig.world.root, lease["id"], signer_fp=fp, sign=sign, clock=_Ticking(rig.world))
    state = sl._replay(rig.world.root)
    assert state.policy.grants[lease["id"]]["expires_at"] == expires


def test_a_renew_of_a_lapsed_lease_is_refused_so_two_never_go_live(rig):
    from levain.cockpit.keys import KeyError_
    first = rig.sign_lease()
    rig.world.clock.now += timedelta(days=15)
    rig.sign_lease()                                                   # the second, after the first lapsed
    fp, sign = rig.world.keys["laptop"]
    with pytest.raises(KeyError_, match="never renewed"):
        sl.renew_lease(rig.world.root, first["id"], signer_fp=fp, sign=sign, clock=rig.world.clock)


def test_an_empty_launcher_key_is_a_refusal_not_a_traceback(rig):
    (rig.key_dir / "launcher.ed25519").write_bytes(b"")
    os.chmod(rig.key_dir / "launcher.ed25519", 0o600)
    with pytest.raises(sl.SeatLaunchRefused, match="launcher key"):
        sl.ensure_launcher_key(rig.key_dir)


def test_the_launcher_key_is_never_visible_half_written(rig, monkeypatch):
    seen = []
    real_link = os.link
    monkeypatch.setattr(os, "link", lambda a, b, **k: (seen.append(os.path.exists(b)), real_link(a, b, **k))[1])
    sl.ensure_launcher_key(rig.key_dir)
    assert seen == [False]                                             # published whole by one link
    assert [p.name for p in rig.key_dir.iterdir()] == ["launcher.ed25519"]


def test_a_torn_mint_chain_refuses_the_launch_and_a_failed_append_leaves_no_partial_row(rig, monkeypatch):
    rig.sign_lease()
    rig.launch("lane-a")
    path = rig.key_dir / "mints.jsonl"
    good = path.read_bytes()
    path.write_bytes(good + b'{"v": 1, "torn')
    with pytest.raises(sl.SeatLaunchRefused, match="mint chain"):
        rig.launch("lane-b")
    assert not (rig.lanes / "lane-b").exists()
    path.write_bytes(good)
    real_fsync = os.fsync
    monkeypatch.setattr(os, "fsync", lambda fd: (_ for _ in ()).throw(OSError(28, "No space left on device")))
    with pytest.raises(OSError):
        rig.launch("lane-c")
    monkeypatch.setattr(os, "fsync", real_fsync)
    assert path.read_bytes() == good and not (rig.lanes / "lane-c").exists()


def test_a_lease_revoked_while_the_clone_runs_rolls_the_roots_back(rig, monkeypatch):
    from levain.consent.grants import revoke_grant
    lease = rig.sign_lease()
    real_mint = sl.mint_lane

    def slow_mint(*a, **k):
        out = real_mint(*a, **k)
        fp, sign = rig.world.keys["laptop"]
        rig.world.clock.now += timedelta(minutes=1)
        revoke_grant(rig.world.root, lease["id"], signer_fp=fp, sign=sign, clock=rig.world.clock)
        return out
    monkeypatch.setattr(sl, "mint_lane", slow_mint)
    with pytest.raises(sl.SeatLaunchRefused, match="no longer live"):
        rig.launch("lane-a", now=rig.world.clock.now + timedelta(minutes=1))
    assert list(rig.lanes.iterdir()) == [] and list(rig.returns.iterdir()) == []
    assert sl.read_mints(rig.key_dir) == []


def test_a_parent_others_can_write_refuses_the_mint(rig):
    rig.sign_lease()
    rig.lanes.chmod(0o777)
    try:
        with pytest.raises(sl.SeatLaunchRefused, match="others can write"):
            rig.launch("lane-a")
    finally:
        rig.lanes.chmod(0o700)
    assert list(rig.lanes.iterdir()) == []


def test_levain_seat_canary_passes_its_own_flags_through(monkeypatch):
    import levain.seat_canary as sc
    from levain import cli
    seen = []
    monkeypatch.setattr(sc, "main", lambda argv: seen.append(list(argv)) or 0)
    assert cli.main(["seat", "canary", "--claude-bin", "/x/claude", "--swap-attempts", "3"]) == 0
    assert cli.main(["seat", "canary", "-h"]) == 0
    assert seen == [["--claude-bin", "/x/claude", "--swap-attempts", "3"], ["-h"]]

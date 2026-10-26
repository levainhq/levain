"""The dispatch service (CONTRACT §1.3): its unit's on-failure restart policy, its store row (``L/dispatch``), and its
program ``levain/slot/services/dispatch.py``: the supervisor lock (W2-d DESIGN U5), the follower as its body, and the
exits each unit manager reads (the lock loser's EX_TEMPFAIL, a SIGTERM death, EX_SOFTWARE for a 0 with no stop signal,
EX_CONFIG for a usage error or a store of the wrong shape). The service account resolves to this test's own uid here
(:func:`_as_the_service_account`); the refusal of root and of every other user is
``test_slot_service_dispatch_user.py``'s. The program refuses root before its body, so as root the tests that run the
body skip (:func:`_body`)."""
from __future__ import annotations

import errno
import fcntl
import hashlib
import os
import plistlib
import signal
import stat
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from levain.slot import layout, service_boot, units
from levain.slot import runtime_helper as H
from levain.slot.services import dispatch as SD

ROOT = Path(__file__).resolve().parents[1]
ME = os.geteuid()

#: the program as its own process, with ``_levain_dispatch`` resolved to this uid (the subprocess twin of the
#: autouse fixture below); argv after ``-c`` reaches the program as its own argv.
_AS_ACCOUNT = ("import os, runpy\n"
               "from levain.slot import runtime_helper as H\n"
               "real, me = H.Seams, os.geteuid()\n"
               "H.Seams = lambda: real(account_uid=lambda n: me)\n"
               "runpy.run_module('levain.slot.services.dispatch', run_name='__main__', alter_sys=True)\n")


@pytest.fixture(autouse=True)
def _as_the_service_account(monkeypatch):
    real = H.Seams
    monkeypatch.setattr(SD.H, "Seams", lambda: real(account_uid=lambda n: ME))


def _body() -> None:
    """Skip, at run time, a test that runs the program past its root refusal when pytest itself runs as root: as root
    the program exits EX_CONFIG before its body."""
    if os.geteuid() == 0:
        pytest.skip("the program refuses root before its body; this test needs a non-root euid")

#: sha256 of each OTHER service's render on main bcb41e68 (``launchd_plist_text``, ``systemd_unit_text``; pf has no
#: Linux unit): the on-failure change must leave every one byte-unchanged. Since then each Linux unit's
#: ``RestartPreventExitStatus`` also lists EX_NOPERM 77, and that line is the only change (``_at_bcb41e68``).
_BASE_RENDERS = {
    "control": ("83f103c1d60e52f419f385bd32937619f331debbf01ea5683df517af3c8dbc50",
                "231c2096278f0f51ed339d15dc8b8fdfafa7989e0e2e616e267995e275909c2d"),
    "keys": ("1b6690acd5459a61b7f5661ad848c2022fc5cdd7abe9944dd823248ef2f1c242",
             "ea1c8d3db8cf129979306b769467116cf432abbfd41bc01790103f5ed4aa8752"),
    "qbroker": ("ba7065b38fcc837b669c64022aaf074f61121f7e1a2a73da038cde787cc95f52",
                "f57330c86d79cf0968eadc1c8722f7fbcf749d8ca0b040875137b948c1d1f103"),
    "reaper": ("0e43ef1cd8e4169f92da574c9ed5a7636fabe2875e47c0e8b15ae9d32474f074",
               "bba7ea59e0cfd3ec98488124db756571e22c2cd14f05e05613866750e453292b"),
    "pf": ("bf226f0434c4c2598d98d34c84a254c1ed9d137812e9d78fb5002a30d4ee82d7", None),  # pfboot: the boot log paths (logs=True)
}


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _at_bcb41e68(unit: str) -> str:
    """A Linux unit text with its one line since bcb41e68 put back (the line must be there to be put back)."""
    now, then = "\nRestartPreventExitStatus=78 77\n", "\nRestartPreventExitStatus=78\n"
    assert unit.count(now) == 1, unit
    return unit.replace(now, then)


def test_dispatch_unit_is_on_failure_and_every_other_render_is_unchanged():
    d = units.by_name("dispatch")
    assert d.restart == "on-failure"
    assert plistlib.loads(units.launchd_plist_text(d).encode())["KeepAlive"] == {"SuccessfulExit": False}
    text = units.systemd_unit_text(d)
    assert "Restart=on-failure\nRestartSec=5\n" in text and "Restart=always" not in text
    others = {s.name: s for s in units.SERVICES if s.name != "dispatch"}
    assert set(others) == set(_BASE_RENDERS)
    for name, (plist, unit) in _BASE_RENDERS.items():
        assert _sha(units.launchd_plist_text(others[name])) == plist, name
        if unit is not None:
            assert _sha(_at_bcb41e68(units.systemd_unit_text(others[name]))) == unit, name


@pytest.mark.parametrize("host", ("darwin", "linux"))
def test_dispatch_store_row_and_program_install(host):
    rows = {r.path: r for r in layout.base_store_rows(host, "op")}
    row = rows[layout.STORE[host] / "dispatch"]
    assert (row.owner, row.group, row.mode, row.kind) == ("_levain_dispatch", "_levain_dispatch", 0o700, "dir")
    assert layout.dispatch_store_dir(host) == row.path
    # the program exists in this package, so setup installs the unit (no pending line names it)
    have, pending = units.built_services(host, ROOT / "levain")
    assert "dispatch" in {s.name for s in have}
    assert not [p for p in pending if "dispatch" in p]


@pytest.fixture
def sdir(tmp_path: Path) -> Path:
    d = tmp_path / "dispatch"
    d.mkdir(mode=0o700)
    return d


def _main(sdir: Path, *, wait_s: float = 0.2, rc: int = 0, signalled: bool = True):
    """main() with a fake follower that, like the real one, takes SIGTERM on its own handler when ``signalled``."""
    _body()
    calls: list = []
    died: list = []

    def follow(argv) -> int:
        calls.append(list(argv))
        if signalled:
            old = signal.signal(signal.SIGTERM, lambda s, f: None)
            try:
                os.kill(os.getpid(), signal.SIGTERM)
            finally:
                signal.signal(signal.SIGTERM, old)
        return rc
    out = SD.main(["--store-dir", str(sdir), "--socket", "/x/control.sock", "--export-dir", "/x/export"],
                  wait_s=wait_s, follow=follow, die=lambda: died.append(True) or -15)
    return out, calls, died


def test_holder_runs_the_follower_over_its_store_then_dies_by_sigterm(sdir):
    out, calls, died = _main(sdir)
    assert calls == [["--store-dir", str(sdir), "--socket", "/x/control.sock", "--export-dir", "/x/export"]]
    assert died == [True] and out == -15
    lock = sdir / SD.LOCK_NAME
    assert lock.is_file() and (lock.stat().st_mode & 0o777) == 0o600
    # a follower that exits non-zero (another runner holds follow.lock: 75) is that exit, relaunched by on-failure
    out, _, died = _main(sdir, rc=75)
    assert out == 75 and died == []


def test_lock_loser_exits_tempfail_and_runs_nothing(sdir, capfd):
    fd = os.open(str(sdir / SD.LOCK_NAME), os.O_RDWR | os.O_CREAT, 0o600)     # another open file description
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        out, calls, died = _main(sdir, wait_s=0.3)
    finally:
        os.close(fd)
    assert (out, calls, died) == (SD.EX_TEMPFAIL, [], [])
    assert f"another dispatch supervisor held {sdir / SD.LOCK_NAME}" in capfd.readouterr().err
    # released: the next start takes it
    out, calls, _ = _main(sdir)
    assert len(calls) == 1


def test_a_restart_race_hands_over_within_the_bound(sdir):
    fd = os.open(str(sdir / SD.LOCK_NAME), os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    threading.Timer(0.3, os.close, (fd,)).start()        # the old holder finishes shutting down
    out, calls, _ = _main(sdir, wait_s=5.0)
    assert len(calls) == 1 and out == -15


def test_a_store_of_the_wrong_shape_is_ex_config(tmp_path, sdir):
    os.chmod(sdir, 0o750)
    assert _main(sdir)[0] == SD.EX_CONFIG                  # group-readable store dir
    os.chmod(sdir, 0o700)
    lock = sdir / SD.LOCK_NAME
    lock.write_text("")
    os.chmod(lock, 0o644)
    assert _main(sdir)[0] == SD.EX_CONFIG                  # a lockfile other than 0600
    lock.unlink()
    (tmp_path / "elsewhere").write_text("")
    os.chmod(tmp_path / "elsewhere", 0o600)                # a target of the right shape: only O_NOFOLLOW refuses it
    lock.symlink_to(tmp_path / "elsewhere")
    assert _main(sdir)[0] == SD.EX_CONFIG                  # a link at the lock's path is never followed
    lock.unlink()
    link = tmp_path / "linked"
    link.symlink_to(sdir)
    assert _main(link)[0] == SD.EX_CONFIG                  # nor a link as the store dir
    assert _main(tmp_path / "absent")[0] == SD.EX_CONFIG


def test_a_holder_released_after_the_last_try_leaves_the_loser_relaunchable(sdir, capfd):
    """S1 (L3 r1, codex): the holder releases between the contender's last failed ``flock`` and its deadline check. The
    contender gives up with nobody holding the lock, so its exit must be one the unit manager relaunches (75), never
    0."""
    held = os.open(str(sdir / SD.LOCK_NAME), os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(held, fcntl.LOCK_EX)
    clock = iter([0.0])                                   # the deadline is computed at 0.0

    def monotonic() -> float:
        for t in clock:
            return t
        os.close(held)                                    # the old holder exits right after the failed try
        return 31.0
    assert SD.take_supervisor_lock(sdir, wait_s=30.0, monotonic=monotonic, sleep=lambda s: None) is None
    probe = os.open(str(sdir / SD.LOCK_NAME), os.O_RDWR)
    try:
        fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)  # nobody holds it now
    finally:
        os.close(probe)
    # and main maps a timed-out wait to EX_TEMPFAIL
    fd = os.open(str(sdir / SD.LOCK_NAME), os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        out, calls, died = _main(sdir, wait_s=0.2)
    finally:
        os.close(fd)
    assert (out, calls, died) == (SD.EX_TEMPFAIL, [], []) and SD.EX_TEMPFAIL == 75


def test_a_follower_return_0_with_no_signal_is_not_a_signal_death(sdir, capfd):
    """S2 (L3 r1, complement): an rc 0 the program saw no SIGTERM/SIGINT for is a distinct non-zero exit, logged; it
    never dies by SIGTERM (which systemd would not restart)."""
    out, calls, died = _main(sdir, rc=0, signalled=False)
    assert len(calls) == 1 and died == []
    assert out == SD.EX_SOFTWARE == 70
    assert "returned 0 with no stop signal" in capfd.readouterr().err


@pytest.mark.parametrize("signum", (signal.SIGTERM, signal.SIGINT))
def test_a_signal_the_follower_handled_itself_still_ends_by_sigterm(sdir, signum):
    """The real follower installs its own handlers over the program's and restores them after; the program still sees
    a stop signal delivered while the follower's handler was in place."""
    def follow(_argv) -> int:
        got: list = []
        old = signal.signal(signum, lambda s, f: got.append(s))
        try:
            os.kill(os.getpid(), signum)
        finally:
            signal.signal(signum, old)
        assert got == [signum]
        return 0
    _body()
    died: list = []
    out = SD.main(["--store-dir", str(sdir), "--socket", "/x/c.sock", "--export-dir", "/x/e"], wait_s=0.2,
                  follow=follow, die=lambda: died.append(True) or -15)
    assert (out, died) == (-15, [True])


def test_service_entry_runs_the_program_on_the_layout_store(capfd, monkeypatch):
    """The unit's path: ``service_boot.main(["dispatch"])`` runs the program as ``__main__`` with its own name left in
    argv; it parses (no usage error) and reaches the store check of :func:`layout.dispatch_store_dir`, which on this
    machine is absent (no install), so the program exits EX_CONFIG naming that path."""
    _body()
    host = "darwin" if sys.platform == "darwin" else "linux"
    if layout.dispatch_store_dir(host).exists():
        pytest.skip("an installed L/dispatch on this machine")
    monkeypatch.setattr(sys, "argv", ["/usr/local/libexec/levain/service-entry", "dispatch"])   # the unit's argv
    with pytest.raises(SystemExit) as e:
        service_boot.main(["dispatch"], check=lambda h, n: (True, ""))
    assert e.value.code == SD.EX_CONFIG
    assert f"refused: {layout.dispatch_store_dir(host)}" in capfd.readouterr().err


def test_main_parses_the_entry_argv_with_layout_defaults(monkeypatch):
    _body()
    seen: dict = {}

    def fake_take(directory, *, wait_s):
        seen["dir"], seen["wait"] = directory, wait_s
        return None
    monkeypatch.setattr(SD, "take_supervisor_lock", fake_take)
    assert SD.main(["dispatch"]) == SD.EX_TEMPFAIL
    host = "darwin" if sys.platform == "darwin" else "linux"
    assert seen == {"dir": layout.dispatch_store_dir(host), "wait": SD.LOCK_WAIT_S}


def test_refuses_root(monkeypatch):
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    assert SD.main([]) == SD.EX_CONFIG


@pytest.mark.parametrize("argv", (["--store-dir", "relative"], ["--no-such-flag"]))
def test_a_usage_error_is_ex_config(argv, capfd):
    """S3: a unit line the program cannot parse stays down (78, RestartPreventExitStatus), never 2."""
    _body()
    assert SD.main(argv) == SD.EX_CONFIG
    assert "error:" in capfd.readouterr().err


@pytest.mark.parametrize("bit", (stat.S_ISGID, stat.S_ISVTX))
def test_a_store_dir_with_a_special_bit_is_ex_config(sdir, bit):
    """S5: the store dir is exactly 0700; a setgid or sticky bit is a drifted dir."""
    os.chmod(sdir, 0o700 | bit)
    if not os.stat(sdir).st_mode & bit:
        pytest.skip("this filesystem cleared the bit for a non-member")
    out, calls, _ = _main(sdir)
    assert (out, calls) == (SD.EX_CONFIG, [])


def test_a_flock_error_other_than_contention_is_ex_config(sdir, monkeypatch):
    """S4: ENOLCK (or any OSError but EWOULDBLOCK) from the lock path is a refusal, not a traceback the unit
    restarts."""
    def flock(fd, op):
        raise OSError(errno.ENOLCK, "No locks available")
    monkeypatch.setattr(SD.fcntl, "flock", flock)
    out, calls, _ = _main(sdir)
    assert (out, calls) == (SD.EX_CONFIG, [])


def test_the_lockfile_is_0600_under_a_narrow_umask(sdir):
    """S6: a umask that clears an owner bit still leaves the created lockfile 0600, so a fresh install starts."""
    old = os.umask(0o277)
    try:
        out, calls, _ = _main(sdir)
    finally:
        os.umask(old)
    assert len(calls) == 1 and out == -15
    assert stat.S_IMODE((sdir / SD.LOCK_NAME).stat().st_mode) == 0o600


def _env() -> dict:
    return {**os.environ, "PYTHONPATH": str(ROOT)}


def test_the_program_as_its_own_process_holds_the_lock_and_dies_by_sigterm(sdir, tmp_path):
    """The real program and the real follower runner: CONTROL absent (the follower logs control-down and waits). A
    second start in this process meets the first PROCESS's lock and exits EX_TEMPFAIL; SIGTERM, sent only once the
    follower's handler is known installed (its first stop line), reaches the program through that handler (the wakeup
    fd) and ends the process by SIGTERM."""
    _body()
    proc = subprocess.Popen([sys.executable, "-c", _AS_ACCOUNT, "--store-dir", str(sdir),
                             "--socket", str(tmp_path / "no-control.sock"), "--export-dir", str(tmp_path / "export")],
                            env=_env(), cwd=str(ROOT), stderr=subprocess.PIPE, text=True)
    lines: list = []
    ready = threading.Event()

    def read() -> None:
        assert proc.stderr is not None
        for line in proc.stderr:
            lines.append(line)
            if "the follower stopped" in line:
                ready.set()
    t = threading.Thread(target=read, daemon=True)
    t.start()
    try:
        if not ready.wait(20.0):
            pytest.fail(f"the follower never logged its first stop: {lines!r}")
        out, calls, _ = _main(sdir, wait_s=0.3)
        assert (out, calls) == (SD.EX_TEMPFAIL, [])        # a true duplicate: nothing run
        proc.send_signal(signal.SIGTERM)
        assert proc.wait(timeout=20.0) == -signal.SIGTERM
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10.0)
        t.join(timeout=5.0)
    text = "".join(lines)
    assert f"holding {sdir / SD.LOCK_NAME}" in text and "control-down" in text
    # the lock is free again and was never replaced
    out, calls, _ = _main(sdir)
    assert len(calls) == 1

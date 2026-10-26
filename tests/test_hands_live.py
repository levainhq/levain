"""``levain.hands_live``: the one home of the hands user's liveness check, its sweep and the hands lock (autogrant
DESIGN r2.3 / r3.6 / r4.4). ws-git, the session lifecycle and the automation executor reach the same bodies."""
from __future__ import annotations

import fcntl
import os
import py_compile
import shutil
import subprocess
from pathlib import Path

import pytest

from levain import hands_live
from levain.firing import confinement, hands, ws_git
from levain import slot


def _hands(tmp: Path) -> ws_git.Hands:
    return ws_git.Hands(user="_levain_x_000000", uid=4_000_017, home=str(tmp), workspace=tmp)


def test_a_stub_of_the_moved_check_bites_both_ws_git_and_the_sweep(monkeypatch, tmp_path):
    # r4.4's seam test: one stub on hands_live reaches _refuse_while_live (through ws_git's wrapper) and the sweep
    seen: list[str] = []
    monkeypatch.setattr(hands_live, "run_bounded", lambda argv, **kw: subprocess.CompletedProcess(argv, 0, b"", b""))
    monkeypatch.setattr(hands_live, "entity_session_live", lambda uid, **kw: seen.append("live") or True)
    with pytest.raises(ws_git.WsGitError, match="has processes running"):
        ws_git._refuse_while_live(_hands(tmp_path), "ws-git")
    said = confinement.sweep_hands_user("_levain_x_000000", uid=4_000_017, timeout=0.3)
    assert said is not None and "still running" in said
    assert seen.count("live") >= 2

    def cannot_tell(uid, **kw):
        raise ws_git.WsGitError("cannot tell whether the entity is running (stub); refusing")
    monkeypatch.setattr(hands_live, "entity_session_live", cannot_tell)
    said = confinement.sweep_hands_user("_levain_x_000000", uid=4_000_017, timeout=0.2)
    assert said is not None and "cannot tell" in said
    with pytest.raises(ws_git.WsGitError, match="cannot tell"):
        ws_git.entity_session_live(4_000_017)


def test_the_wrapper_raises_the_moved_errors_as_ws_git_error_with_the_same_text(monkeypatch):
    def broken(uid, **kw):
        raise hands_live.HandsLiveError("cannot tell whether the entity is running (x); refusing")
    monkeypatch.setattr(hands_live, "entity_session_live", broken)
    with pytest.raises(ws_git.WsGitError) as exc:
        ws_git.entity_session_live(4_000_017, timeout=1.0)
    assert str(exc.value) == "cannot tell whether the entity is running (x); refusing"
    assert issubclass(ws_git.WsGitError, hands_live.HandsLiveError)
    assert issubclass(hands_live.HandsLiveError, RuntimeError)


def test_the_moved_names_have_one_home():
    assert ws_git.LIVENESS_TIMEOUT is hands_live.LIVENESS_TIMEOUT
    assert ws_git.proc_hides_processes is hands_live.proc_hides_processes
    assert ws_git.HANDS_LOCK is hands_live.HANDS_LOCK and ws_git._open_lock is hands_live._open_lock
    assert confinement.sweep_hands_user is hands_live.sweep_hands_user
    assert hands.SECURE_PATH is hands_live.SECURE_PATH and ws_git.SECURE_PATH is hands_live.SECURE_PATH
    assert slot.SUDO is hands_live.SUDO


def test_the_children_get_the_secure_path_and_only_the_locale(monkeypatch):
    monkeypatch.setenv("LANG", "en_US.UTF-8")
    monkeypatch.setenv("LC_CTYPE", "UTF-8")
    monkeypatch.setenv("SECRET_TOKEN", "nope")
    envs: list[dict] = []

    def run(argv, **kw):
        envs.append(kw["env"])
        return subprocess.CompletedProcess(argv, 1, b"", b"")
    monkeypatch.setattr(hands_live, "run_bounded", run)
    assert hands_live.entity_session_live(4_000_017) is False
    locale = {k: v for k, v in os.environ.items() if k == "LANG" or k.startswith("LC_")}
    assert envs == [{"PATH": hands_live.SECURE_PATH, **locale}]
    assert locale["LC_CTYPE"] == "UTF-8" and "SECRET_TOKEN" not in envs[0]


def test_the_exclusive_take_is_refused_while_a_session_holds_the_lock_shared(tmp_path):
    (tmp_path / ".levain").mkdir()
    shared = hands_live._open_lock(tmp_path)
    try:
        fcntl.flock(shared, fcntl.LOCK_SH | fcntl.LOCK_NB)
        assert hands_live.take_exclusive(tmp_path) is None
        with pytest.raises(ws_git.WsGitError, match="a session of this entity is open"):
            ws_git._exclusive(tmp_path).__enter__()
    finally:
        os.close(shared)
    fd = hands_live.take_exclusive(tmp_path)
    assert fd is not None
    try:
        with pytest.raises(ws_git.WsGitError, match="or an automation is working in this entity's workspace"):
            ws_git.hold_session_lock(tmp_path, wait=0.0)
    finally:
        os.close(fd)


def test_a_lock_file_that_cannot_be_opened_raises(tmp_path):
    with pytest.raises(OSError):
        hands_live.take_exclusive(tmp_path / "no-such-entity")


@pytest.mark.skipif(not os.path.exists("/usr/bin/python3"), reason="root's interpreter is /usr/bin/python3")
def test_the_module_compiles_and_imports_under_roots_interpreter(tmp_path):
    # root's /usr/bin/python3 (3.9.6 on macOS) imports it through levain.firing.hands
    src = Path(hands_live.__file__)
    copy = tmp_path / "hands_live.py"
    shutil.copy(src, copy)
    out = subprocess.run(["/usr/bin/python3", "-I", "-c",
                          "import sys, py_compile; py_compile.compile(sys.argv[1], doraise=True); "
                          "sys.path.insert(0, sys.argv[2]); import hands_live; print(sys.version_info[:2])",
                          str(copy), str(tmp_path)], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    print(f"  /usr/bin/python3 {out.stdout.strip()}: compiled and imported")
    py_compile.compile(str(copy), doraise=True)


# --- B1-10: every failure to read the process table is a HandsLiveError; B1-11: no unbounded wait after a kill ----

def _rewire(monkeypatch, table: dict, *, kill_refused: bool = False) -> list:
    """Every child started from here runs ``table[basename(argv[0])]`` instead (``/usr/bin/true`` for any other, so
    no real sudo, pgrep or ps runs). Patched on the ``subprocess`` module, so ``subprocess.run`` and a direct
    ``Popen`` both reach it. With ``kill_refused``, ``kill()`` raises ``PermissionError``."""
    real = subprocess.Popen
    spawned: list = []

    class P(real):
        def __init__(self, argv, *a, **kw):
            super().__init__(table.get(os.path.basename(argv[0]), ["/usr/bin/true"]), *a, **kw)
            spawned.append(self)

        if kill_refused:
            def kill(self):
                raise PermissionError(1, "Operation not permitted")
    monkeypatch.setattr(subprocess, "Popen", P)
    return spawned


def _reap_all(spawned: list) -> None:
    for p in spawned:
        if p.poll() is None:
            p.send_signal(9)     # send_signal, not the patched kill()
        p.wait()


@pytest.mark.parametrize("which", ["pgrep", "ps"])
def test_a_process_table_that_does_not_decode_cannot_tell(monkeypatch, which):
    monkeypatch.setattr(hands_live.platform, "system", lambda: "Darwin")
    table = {"pgrep": ["/usr/bin/printf", "101\\n"], "ps": ["/usr/bin/printf", "  101     1 /usr/sbin/cfprefsd\\n"]}
    table[which] = ["/usr/bin/printf", "10\\377\\n"]
    spawned = _rewire(monkeypatch, table)
    try:
        with pytest.raises(hands_live.HandsLiveError, match="cannot tell"):
            hands_live.entity_session_live(4_000_017, timeout=5.0)
    finally:
        _reap_all(spawned)


def test_a_mountinfo_that_does_not_decode_cannot_tell(monkeypatch):
    monkeypatch.setattr(hands_live.platform, "system", lambda: "Linux")

    class Unreadable:
        def __init__(self, *a):
            pass

        def read_text(self, **kw):
            return b"\xff".decode("utf-8")
    monkeypatch.setattr(hands_live, "Path", Unreadable)
    with pytest.raises(hands_live.HandsLiveError, match="cannot tell"):
        hands_live.entity_session_live(4_000_017)


def test_the_sweep_reports_a_table_that_does_not_decode_and_never_raises_it(monkeypatch):
    monkeypatch.setattr(hands_live.platform, "system", lambda: "Darwin")
    spawned = _rewire(monkeypatch, {"pgrep": ["/usr/bin/printf", "10\\377\\n"]})
    try:
        said = hands_live.sweep_hands_user("_levain_x_000000", uid=4_000_017, timeout=0.5)
    finally:
        _reap_all(spawned)
    assert said is not None and "cannot tell" in said


def test_a_check_whose_child_cannot_be_killed_still_ends_within_its_bound(monkeypatch):
    # subprocess.run kills the child on a timeout and then waits for it WITHOUT a bound (RED on e5a09a46: 4.0 s for
    # a 4 s child); a child running as root under sudo can refuse the operator's kill
    monkeypatch.setattr(hands_live, "_REAP_TIMEOUT", 0.2, raising=False)
    spawned = _rewire(monkeypatch, {"pgrep": ["/bin/sleep", "4"]}, kill_refused=True)
    import time
    t0 = time.monotonic()
    try:
        with pytest.raises(hands_live.HandsLiveError, match="cannot tell"):
            hands_live.entity_session_live(4_000_017, timeout=0.3)
        took = time.monotonic() - t0
    finally:
        _reap_all(spawned)
    assert took < 2.0, took


def test_a_sweep_whose_children_cannot_be_killed_still_ends_within_its_bound(monkeypatch):
    monkeypatch.setattr(hands_live, "_REAP_TIMEOUT", 0.2, raising=False)
    spawned = _rewire(monkeypatch, {"sudo": ["/bin/sleep", "4"], "pgrep": ["/bin/sleep", "4"]}, kill_refused=True)
    import time
    t0 = time.monotonic()
    try:
        said = hands_live.sweep_hands_user("_levain_x_000000", uid=4_000_017, timeout=0.5)
        took = time.monotonic() - t0
    finally:
        _reap_all(spawned)
    assert said is not None and took < 2.5, (took, said)

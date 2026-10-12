"""Chat L3 r1: the canary's own failure paths (H1, M3, M4, complement LOW-2/3/5).

No test here launches `claude`. `_drive` takes its process factory as an argument, and the tests
hand it a factory that starts a small Python child instead, so the cleanup they check (process
group killed, leader reaped, pipes closed) is done on a real process.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

import pytest

from levain import host_canary as hc
from levain import host_guard as hg


# ---------------------------------------------------------------------------- helpers


def _child(code: str):
    """A process factory for `_drive` that starts ``python -c code`` with the kwargs `_drive`
    passes, and records the process so the test can inspect it afterwards."""
    started: list[subprocess.Popen] = []

    def factory(argv, **kw):
        p = subprocess.Popen([sys.executable, "-c", code], **kw)
        started.append(p)
        return p

    return factory, started


_RESULT_THEN_SLEEP = ("import json, sys, time\n"
                      "print(json.dumps({'type': 'result'})); sys.stdout.flush()\n"
                      "time.sleep(60)\n")

_SLEEP = "import time; time.sleep(60)\n"


def _drive(factory, tmp_path: Path, **kw) -> hc._Turn:
    return hc._drive(["claude-not-run"], cwd=tmp_path, env=dict(os.environ), prompt="p",
                     timeout=kw.pop("timeout", 30), decide=lambda req: {"behavior": "deny"},
                     popen=factory, **kw)


def _dead(p: subprocess.Popen) -> bool:
    return p.returncode is not None


# ---------------------------------------------------------------------------- M3 / LOW-2 / LOW-3


def test_an_unwritable_stream_log_never_leaves_the_child_running(tmp_path):
    # codex MED-3 / complement LOW-2: the log was opened after Popen, outside the cleanup try
    factory, started = _child(_SLEEP)
    with pytest.raises(OSError):
        _drive(factory, tmp_path, stream_log=tmp_path / "no-such-dir" / "log")
    assert all(_dead(p) for p in started), "claude left running after a setup failure"


def test_a_setup_failure_after_spawn_kills_and_reaps_the_group(tmp_path, monkeypatch):
    # codex MED-3: the stderr thread or the timer failing to start left no timer at all
    factory, started = _child(_SLEEP)

    class Boom(threading.Thread):
        def start(self):
            raise RuntimeError("cannot start a thread")

    monkeypatch.setattr(hc.threading, "Thread", Boom)
    with pytest.raises(RuntimeError):
        _drive(factory, tmp_path)
    assert started and all(_dead(p) for p in started)
    assert all(s.closed for p in started for s in (p.stdin, p.stdout, p.stderr))


def test_the_pipes_are_closed_after_a_normal_turn(tmp_path):
    # complement LOW-3: stdout and stderr were never closed, two fds leaked per canary
    factory, started = _child(_RESULT_THEN_SLEEP)
    turn = _drive(factory, tmp_path)
    assert turn.result == {"type": "result"}
    (p,) = started
    assert _dead(p)
    assert p.stdout.closed and p.stderr.closed and p.stdin.closed


def test_a_descendant_holding_stderr_does_not_hang_the_cleanup(tmp_path):
    # verify r2, complement LOW: a descendant that left the group (setsid) keeps stderr open, the drain
    # stays blocked in read, and close() on the buffered reader waited on the same lock forever
    pidfile = tmp_path / "grandchild.pid"
    code = ("import json, subprocess, sys, time\n"
            "g = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'],\n"
            "                     start_new_session=True, stdout=subprocess.DEVNULL, stdin=subprocess.DEVNULL)\n"
            f"open({str(pidfile)!r}, 'w').write(str(g.pid))\n"
            "print(json.dumps({'type': 'result'})); sys.stdout.flush()\n"
            "time.sleep(60)\n")
    factory, started = _child(code)
    done: list = []
    t = threading.Thread(target=lambda: done.append(_drive(factory, tmp_path)), daemon=True)
    try:
        t.start()
        t.join(15)
        assert done, "the canary's cleanup hung on a stderr a surviving descendant still holds"
        assert all(_dead(p) for p in started)
    finally:
        if pidfile.exists():
            try:
                os.kill(int(pidfile.read_text()), 9)
            except OSError:
                pass


# ---------------------------------------------------------------------------- M4


def test_stderr_is_kept_only_as_a_bounded_tail(tmp_path):
    # codex MED-4: every stderr line was kept in a list nobody read
    n = hc._STDERR_TAIL_LINES * 5
    code = ("import json, sys, time\n"
            f"for i in range({n}): sys.stderr.write('e%d\\n' % i)\n"
            "sys.stderr.flush()\n"
            "print(json.dumps({'type': 'result'})); sys.stdout.flush()\n"
            "time.sleep(60)\n")
    factory, _ = _child(code)
    turn = _drive(factory, tmp_path)
    assert len(turn.stderr_tail) == hc._STDERR_TAIL_LINES
    assert turn.stderr_tail[-1] == f"e{n - 1}\n"


# ---------------------------------------------------------------------------- H1 / LOW-5, the probe


def _probe_cfg(tmp_path: Path) -> dict:
    ent = tmp_path / "ent"
    hooks = ent / "activation" / "hooks"
    hooks.mkdir(parents=True)
    (hooks / "session_start.py").write_text("x")
    return {"nonce": "n0", "port": 1, "sock": str(tmp_path / "none.sock"),
            "outside": str(tmp_path / "nodir" / "f"), "hook": str(hooks / "h"), "hook_case": "",
            "claude": str(tmp_path / "c"), "mcp": "", "git": "",
            "link_target": str(hooks), "swap_link": str(tmp_path / "sl"), "swap_name": "via.txt",
            "link_src": str(hooks / "session_start.py"), "link_dst": str(tmp_path / "dst"),
            "activation": str(ent / "activation"),
            "activation_moved": str(ent / "activation-moved-n0")}


def _run_probe(tmp_path: Path, cfg: dict, *, block_second_rename: bool) -> dict[str, str]:
    script = tmp_path / "p.py"
    script.write_text(hc.probe_script(cfg))
    # The probe runs unsandboxed here. To stand in for a sandbox that let the first rename
    # through and refused the restore, a wrapper makes every rename after the first raise EPERM.
    wrapper = ("import errno, os, sys\n"
               "real = os.rename\n"
               "calls = []\n"
               "def rename(a, b, *k, **kw):\n"
               "    calls.append(a)\n"
               f"    if {block_second_rename!r} and len(calls) > 1:\n"
               "        raise PermissionError(errno.EPERM, 'Operation not permitted')\n"
               "    return real(a, b, *k, **kw)\n"
               "os.rename = rename\n"
               "sys.argv = [sys.argv[1]]\n"
               "exec(compile(open(sys.argv[0]).read(), sys.argv[0], 'exec'))\n")
    env = {k: v for k, v in os.environ.items() if k.lower() not in ("http_proxy", "https_proxy", "all_proxy")}
    out = subprocess.run([sys.executable, "-c", wrapper, str(script)], env=env,
                         capture_output=True, text=True, timeout=60).stdout
    return {ln.split(" ", 2)[1]: ln.split(" ", 2)[2] for ln in out.splitlines() if ln.count(" ") >= 2}


def test_a_rename_that_succeeds_is_open_even_when_the_restore_is_refused(tmp_path):
    # codex HIGH H1: the restore's EPERM printed "RENAME BLOCKED" over a successful rename
    cfg = _probe_cfg(tmp_path)
    lines = _run_probe(tmp_path, cfg, block_second_rename=True)
    assert lines["RENAME"] == "OPEN", lines
    os.rename(cfg["activation_moved"], cfg["activation"])  # tidy for tmp_path


def test_a_rename_that_succeeds_and_is_restored_is_open(tmp_path):
    cfg = _probe_cfg(tmp_path)
    lines = _run_probe(tmp_path, cfg, block_second_rename=False)
    assert lines["RENAME"] == "OPEN", lines
    assert os.path.isdir(cfg["activation"]) and not os.path.lexists(cfg["activation_moved"])


# ---------------------------------------------------------------------------- H1 / LOW-5, the host


def _profile(ent: Path) -> hg.CCHostProfile:
    run = Path(tempfile.mkdtemp(prefix="lvc", dir="/tmp"))  # AF_UNIX paths are capped
    return hg.CCHostProfile(entity=ent, claude_bin=sys.executable, settings={}, mcp_config={},
                            env={}, run_dir=run, state_dir=run, pin_status="ok")


@pytest.fixture
def entity(tmp_path: Path) -> Path:
    ent = tmp_path / "ent"
    (ent / "activation" / "hooks").mkdir(parents=True)
    (ent / ".levain").mkdir()
    return Path(os.path.realpath(ent))


def _nonce_from(prompt: str) -> str:
    return re.search(r"canary-([0-9a-f]+)", prompt).group(1)


def test_the_host_restores_activation_a_failed_sandbox_left_renamed(entity, monkeypatch):
    # codex HIGH H1 / complement LOW-5: a canary killed (or refused) between the two renames
    # left the entity's activation/ moved away, and the verdict could still read ok
    act = entity / "activation"
    ino = os.lstat(act).st_ino

    def fake_drive(argv, *, prompt, **kw):
        os.rename(act, entity / f"activation-moved-{_nonce_from(prompt)}")
        return hc._Turn(timed_out=True)

    monkeypatch.setattr(hc, "_drive", fake_drive)
    verdict = hc.run_cc_canary(_profile(entity))
    assert not verdict.ok
    c5 = [c for c in verdict.checks if c.name.startswith("C5")]
    assert c5 and c5[0].status == "fail", verdict.checks
    assert act.is_dir() and os.lstat(act).st_ino == ino
    assert not [p for p in entity.iterdir() if p.name.startswith("activation-moved")]


def test_a_failed_host_restore_raises_naming_the_moved_path(entity, monkeypatch):
    # verify r2, codex introduced HIGH + LOW-5: the host-side restore swallowed its own OSError and
    # left the live activation/ at activation-moved-*, behind a C5 line nobody had to read
    act = entity / "activation"
    real_rename = os.rename
    driven = []

    def fake_drive(argv, *, prompt, **kw):
        real_rename(act, entity / f"activation-moved-{_nonce_from(prompt)}")
        driven.append(True)
        return hc._Turn(timed_out=True)

    def refusing_rename(src, dst, *a, **kw):
        if driven:
            raise OSError(1, "Operation not permitted")
        return real_rename(src, dst, *a, **kw)

    monkeypatch.setattr(hc, "_drive", fake_drive)
    monkeypatch.setattr(hc.os, "rename", refusing_rename)
    with pytest.raises(hc.ActivationNotRestored) as ei:
        hc.run_cc_canary(_profile(entity))
    moved = [p for p in entity.iterdir() if p.name.startswith("activation-moved")]
    assert len(moved) == 1 and not act.exists()
    assert os.fspath(moved[0]) in str(ei.value)
    assert ei.value.moved == moved[0]
    verdict = ei.value.verdict
    assert not verdict.ok
    c5 = [c for c in verdict.checks if c.name.startswith("C5")]
    assert c5 and c5[0].status == "fail" and os.fspath(moved[0]) in c5[0].evidence, verdict.checks


def test_the_host_never_moves_a_directory_that_is_not_the_original(entity, monkeypatch):
    # The restore is by identity: a different directory left at the moved name stays where it is
    act = entity / "activation"

    def fake_drive(argv, *, prompt, **kw):
        moved = entity / f"activation-moved-{_nonce_from(prompt)}"
        os.rename(act, entity / "elsewhere")
        moved.mkdir()
        return hc._Turn(timed_out=True)

    monkeypatch.setattr(hc, "_drive", fake_drive)
    verdict = hc.run_cc_canary(_profile(entity))
    assert not verdict.ok
    assert not act.exists()
    c5 = [c for c in verdict.checks if c.name.startswith("C5")]
    assert c5 and c5[0].status == "fail" and "not restored" in c5[0].evidence, verdict.checks


def test_an_untouched_activation_passes_c5(entity, monkeypatch):
    monkeypatch.setattr(hc, "_drive", lambda argv, **kw: hc._Turn(timed_out=True))
    verdict = hc.run_cc_canary(_profile(entity))
    c5 = [c for c in verdict.checks if c.name.startswith("C5")]
    assert c5 and c5[0].status == "pass", verdict.checks


def test_a_sibling_of_activation_is_not_covered_by_deny_write(entity):
    # Why H1 probes activation/ itself and restores host-side instead of renaming a throwaway
    # sibling: denyWrite names activation/, so a sibling in the entity root is writable and its
    # rename would pass under a working sandbox too, proving nothing.
    deny = [Path(d) for d in hg.cc_sandbox_block(entity)["filesystem"]["denyWrite"]]
    sibling = entity / "activation-moved-x"
    assert entity / "activation" in deny
    assert not any(sibling == d or d in sibling.parents for d in deny)


def test_a_failed_host_restore_is_not_hidden_by_a_failure_in_the_run(entity, monkeypatch):
    # The stranded directory outranks whatever else went wrong: an exception escaping the run must
    # not carry the recovery error away with it
    act = entity / "activation"
    real_rename = os.rename
    driven = []

    def fake_drive(argv, *, prompt, **kw):
        real_rename(act, entity / f"activation-moved-{_nonce_from(prompt)}")
        driven.append(True)
        raise RuntimeError("the turn blew up")

    def refusing_rename(src, dst, *a, **kw):
        if driven:
            raise OSError(1, "Operation not permitted")
        return real_rename(src, dst, *a, **kw)

    monkeypatch.setattr(hc, "_drive", fake_drive)
    monkeypatch.setattr(hc.os, "rename", refusing_rename)
    with pytest.raises(hc.ActivationNotRestored) as ei:
        hc.run_cc_canary(_profile(entity))
    assert isinstance(ei.value.__context__, RuntimeError)
    assert "activation-moved-" in str(ei.value)


# ---------------------------------------------------------------------------- verify r3 R1, the cleanup construct


def _stranding_run(entity: Path, monkeypatch, *, plant_landed: bool = True):
    """Move activation/ away inside the turn, refuse the host's rename back, and plant one landed
    file. Returns (profile, record) where record collects the nonce and the planted path."""
    act = entity / "activation"
    real_rename = os.rename
    record: dict = {}

    def fake_drive(argv, *, prompt, **kw):
        nonce = _nonce_from(prompt)
        record["nonce"] = nonce
        if plant_landed:
            (entity / ".claude").mkdir(exist_ok=True)
            landed = entity / ".claude" / f"selftest-{nonce}.txt"
            landed.write_text("x")
            record["landed"] = landed
        real_rename(act, entity / f"activation-moved-{nonce}")
        record["driven"] = True
        return hc._Turn(timed_out=True)

    def refusing_rename(src, dst, *a, **kw):
        if record.get("driven"):
            raise OSError(1, "Operation not permitted")
        return real_rename(src, dst, *a, **kw)

    monkeypatch.setattr(hc, "_drive", fake_drive)
    monkeypatch.setattr(hc.os, "rename", refusing_rename)
    return _profile(entity), record


def _cleanup_ran(profile: hg.CCHostProfile, record: dict) -> None:
    nonce = record["nonce"]
    assert not (profile.run_dir / f"canary-{nonce}").exists(), "the canary work dir was not removed"
    assert not record["landed"].exists(), "a landed probe file was not removed"
    assert not (profile.run_dir / f"c{nonce[:8]}.s").exists(), "the listener socket was not removed"


def test_a_listener_close_failure_does_not_mask_the_stranded_activation(entity, monkeypatch):
    # verify r3, codex introduced HIGH: listener.close() raising after a failed restore left the
    # ordered finally before either raise, so ActivationNotRestored never reached the caller
    profile, record = _stranding_run(entity, monkeypatch)
    real_close = hc._Listener.close

    def failing_close(self):
        real_close(self)
        raise OSError(13, "Permission denied")

    monkeypatch.setattr(hc._Listener, "close", failing_close)
    with pytest.raises(hc.ActivationNotRestored) as ei:
        hc.run_cc_canary(profile)
    assert f"activation-moved-{record['nonce']}" in str(ei.value)
    # the secondary failure stays visible
    assert isinstance(ei.value.__context__, OSError) and "Permission denied" in str(ei.value.__context__)
    _cleanup_ran(profile, record)


def test_an_oserror_in_the_restores_identity_check_still_runs_the_cleanup(entity, monkeypatch):
    # verify r3, complement LOW: an OSError out of _restore_activation's own identity check escaped
    # the finally before the listener, landed-file and rmtree cleanup ran
    profile, record = _stranding_run(entity, monkeypatch)
    real_ident = hc._dir_ident

    def ident(p):
        if record.get("driven") and Path(p).name.startswith("activation-moved-"):
            raise OSError(5, "Input/output error")
        return real_ident(p)

    monkeypatch.setattr(hc, "_dir_ident", ident)
    with pytest.raises(OSError, match="Input/output error"):
        hc.run_cc_canary(profile)
    _cleanup_ran(profile, record)


def test_a_keyboard_interrupt_in_the_restore_still_runs_the_cleanup(entity, monkeypatch):
    # verify r3, complement LOW: a BaseException during the restore skipped the remaining cleanup
    profile, record = _stranding_run(entity, monkeypatch)

    def interrupted(*a, **kw):
        raise KeyboardInterrupt

    monkeypatch.setattr(hc, "_restore_activation", interrupted)
    with pytest.raises(KeyboardInterrupt):
        hc.run_cc_canary(profile)
    _cleanup_ran(profile, record)


def test_a_probe_file_inside_a_moved_activation_is_removed_after_the_restore(entity, monkeypatch):
    # The restore runs BEFORE the landed-file cleanup: a hooks/ file the probe wrote and then
    # carried away with the rename is only at its landed path once activation/ is back.
    act = entity / "activation"
    record: dict = {}

    def fake_drive(argv, *, prompt, **kw):
        nonce = _nonce_from(prompt)
        record["hook"] = act / "hooks" / f"selftest-{nonce}.txt"
        record["hook"].write_text("x")
        os.rename(act, entity / f"activation-moved-{nonce}")
        return hc._Turn(timed_out=True)

    monkeypatch.setattr(hc, "_drive", fake_drive)
    hc.run_cc_canary(_profile(entity))
    assert act.is_dir() and not record["hook"].exists()
